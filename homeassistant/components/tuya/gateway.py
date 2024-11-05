"""Support for Tuya device gateways."""

from __future__ import annotations

from dataclasses import dataclass

from tuya_sharing import CustomerDevice, Manager

# from homeassistant.components.light import (
#     ATTR_BRIGHTNESS,
#     ATTR_COLOR_TEMP,
#     ATTR_HS_COLOR,
#     ColorMode,
#     LightEntity,
#     LightEntityDescription,
#     filter_supported_color_modes,
# )
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TuyaConfigEntry
from .const import TUYA_DISCOVERY_NEW, DPCode
from .entity import TuyaEntity


@dataclass(frozen=True)
class TuyaGatewayEntityDescription:
    """Describe a Tuya gateway entity."""

    key: str


class TuyaGatewayEntity(TuyaEntity):
    """Tuya gateway device."""

    device: CustomerDevice
    manager: Manager
    description: TuyaGatewayEntityDescription


GATEWAYS: dict[str, tuple[TuyaGatewayEntityDescription, ...]] = {
    # https://developer.tuya.com/en/docs/iot/wg?id=Kbcdadk79ejok
    "gw2": (TuyaGatewayEntityDescription(key=DPCode.SWITCH_LOW_BATTERY),),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: TuyaConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up tuya gateway dynamically through tuya discovery."""
    hass_data = entry.runtime_data

    @callback
    def async_discover_device(device_ids: list[str]):
        """Discover and add a discovered tuya gateway."""
        entities: list[TuyaGatewayEntity] = []
        for device_id in device_ids:
            device = hass_data.manager.device_map[device_id]
            if descriptions := GATEWAYS.get(device.category):
                entities.extend(
                    TuyaGatewayEntity(device, hass_data.manager, description)
                    for description in descriptions
                    if description.key in device.status
                )

        async_add_entities(entities)

    async_discover_device([*hass_data.manager.device_map])

    entry.async_on_unload(
        async_dispatcher_connect(hass, TUYA_DISCOVERY_NEW, async_discover_device)
    )
