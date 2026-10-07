"""Binary sensor: is a charge session running?"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TC66CConfigEntry
from .coordinator import TC66CCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: TC66CConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([TC66CCharging(entry.runtime_data)])


class TC66CCharging(CoordinatorEntity[TC66CCoordinator], BinarySensorEntity):
    """On while more than the threshold current flows (with end delay)."""

    _attr_has_entity_name = True
    _attr_name = "Charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator: TC66CCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_charging"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            name="TC66C",
            manufacturer="RuiDeng",
            model="TC66C",
        )

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.session.active
