"""Go2rtc server."""

import asyncio
from contextlib import suppress
import logging
from tempfile import NamedTemporaryFile

from go2rtc_client import Go2RtcRestClient

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_URL

_LOGGER = logging.getLogger(__name__)
_TERMINATE_TIMEOUT = 5
_SETUP_TIMEOUT = 30
_RESPAWN_COOLDOWN = 1
_SUCCESSFUL_BOOT_MESSAGE = "INF [api] listen addr=127.0.0.1:1984"

# Default configuration for HA
# - Api is listening only on localhost
# - Disable rtsp listener
# - Clear default ice servers
_GO2RTC_CONFIG = """
api:
  listen: "127.0.0.1:1984"

rtsp:
  # ffmpeg needs rtsp for opus audio transcoding
  listen: "127.0.0.1:8554"

webrtc:
  ice_servers: []
"""


class Go2RTCServerStartError(HomeAssistantError):
    """Raised when server does not start."""

    _message = "Go2rtc server didn't start correctly"


class Go2RTCWatchdogError(HomeAssistantError):
    """Raised on watchdog error."""


def _create_temp_file() -> str:
    """Create temporary config file."""
    # Set delete=False to prevent the file from being deleted when the file is closed
    # Linux is clearing tmp folder on reboot, so no need to delete it manually
    with NamedTemporaryFile(prefix="go2rtc_", suffix=".yaml", delete=False) as file:
        file.write(_GO2RTC_CONFIG.encode())
        return file.name


class Server:
    """Go2rtc server."""

    def __init__(self, hass: HomeAssistant, binary: str) -> None:
        """Initialize the server."""
        self._hass = hass
        self._binary = binary
        self._process: asyncio.subprocess.Process | None = None
        self._startup_complete = asyncio.Event()
        self._stop_requested = False
        self._watchdog_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the server."""
        await self._start()
        self._watchdog_task = asyncio.create_task(
            self._watchdog(), name="Go2rtc respawn"
        )

    async def _start(self) -> None:
        """Start the server."""
        _LOGGER.debug("Starting go2rtc server")
        config_file = await self._hass.async_add_executor_job(_create_temp_file)

        self._startup_complete.clear()

        self._process = await asyncio.create_subprocess_exec(
            self._binary,
            "-c",
            config_file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            close_fds=False,  # required for posix_spawn on CPython < 3.13
        )

        self._hass.async_create_background_task(
            self._log_output(self._process), "Go2rtc log output"
        )

        try:
            async with asyncio.timeout(_SETUP_TIMEOUT):
                await self._startup_complete.wait()
        except TimeoutError as err:
            msg = "Go2rtc server didn't start correctly"
            _LOGGER.exception(msg)
            await self._stop()
            raise Go2RTCServerStartError from err

    async def _watchdog(self) -> None:
        """Keep respawning go2rtc servers.

        A new go2rtc server is spawned if the process terminates or the API
        stops responding.
        """
        while not self._stop_requested:
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._monitor_process())
                    tg.create_task(self._monitor_api())
            except ExceptionGroup as grp:
                if not grp.subgroup(Go2RTCWatchdogError):
                    _LOGGER.exception("Watchdog got unexpected exception")
                if self._stop_requested:
                    continue  # type: ignore[unreachable]
                await asyncio.sleep(_RESPAWN_COOLDOWN)
                try:
                    await self._stop()
                    _LOGGER.debug("Spawning new go2rtc server")
                    with suppress(Go2RTCServerStartError):
                        await self._start()
                except Exception:
                    _LOGGER.exception("Unexpected error when restarting go2rtc server")

    async def _monitor_process(self) -> None:
        """Raise if the go2rtc process terminates."""
        _LOGGER.debug("Monitoring go2rtc server process")
        if self._process:
            await self._process.wait()
        _LOGGER.debug("go2rtc server terminated")
        raise Go2RTCWatchdogError("Process ended")

    async def _monitor_api(self) -> None:
        """Raise if the go2rtc process terminates."""
        client = Go2RtcRestClient(async_get_clientsession(self._hass), DEFAULT_URL)

        _LOGGER.debug("Monitoring go2rtc API")
        try:
            while True:
                await client.streams.list()
                await asyncio.sleep(10)
        except Exception as err:
            _LOGGER.debug("go2rtc API did not reply")
            raise Go2RTCWatchdogError("API error") from err

    async def _log_output(self, process: asyncio.subprocess.Process) -> None:
        """Log the output of the process."""
        assert process.stdout is not None

        async for line in process.stdout:
            msg = line[:-1].decode().strip()
            _LOGGER.debug(msg)
            if not self._startup_complete.is_set() and msg.endswith(
                _SUCCESSFUL_BOOT_MESSAGE
            ):
                self._startup_complete.set()

    async def stop(self) -> None:
        """Stop the server and set the stop_requested flag."""
        self._stop_requested = True
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        await self._stop()

    async def _stop(self) -> None:
        """Stop the server."""
        if self._process:
            _LOGGER.debug("Stopping go2rtc server")
            process = self._process
            self._process = None
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_TERMINATE_TIMEOUT)
            except TimeoutError:
                _LOGGER.warning("Go2rtc server didn't terminate gracefully. Killing it")
                with suppress(ProcessLookupError):
                    process.kill()
            else:
                _LOGGER.debug("Go2rtc server has been stopped")
