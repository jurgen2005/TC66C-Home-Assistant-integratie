"""Button: reconnect to the meter right away."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TC66CConfigEntry
from .coordinator import TC66CCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: TC66CConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([TC66CReconnectButton(entry.runtime_data)])


class TC66CReconnectButton(ButtonEntity):
    """Drops any existing connection and reconnects right away, also without a new advertisement."""

    _attr_has_entity_name = True
    _attr_translation_key = "reconnect"
    _attr_icon = "mdi:bluetooth-connect"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: TC66CCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.address}_reconnect"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            name="TC66C",
            manufacturer="RuiDeng",
            model="TC66C",
        )

    async def async_press(self) -> None:
        await self.coordinator.async_force_reconnect()
