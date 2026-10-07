"""Sensors of the TC66C."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTime,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TC66CConfigEntry
from .coordinator import STATUS_CONNECTED, STATUS_OPTIONS, TC66CCoordinator

M = SensorStateClass.MEASUREMENT
TI = SensorStateClass.TOTAL_INCREASING

SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(key="voltage", name="Voltage", device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT, state_class=M, suggested_display_precision=2),
    SensorEntityDescription(key="current", name="Current", device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, state_class=M, suggested_display_precision=2),
    SensorEntityDescription(key="power", name="Power", device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT, state_class=M, suggested_display_precision=2),
    SensorEntityDescription(key="charge_1", name="Charge group 1", native_unit_of_measurement="mAh",
        state_class=TI, icon="mdi:battery-charging"),
    SensorEntityDescription(key="energy_1", name="Energy group 1", device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.MILLIWATT_HOUR, state_class=TI, suggested_display_precision=0),
    SensorEntityDescription(key="charge_0", name="Charge group 0", native_unit_of_measurement="mAh",
        state_class=TI, icon="mdi:battery-charging"),
    SensorEntityDescription(key="energy_0", name="Energy group 0", device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.MILLIWATT_HOUR, state_class=TI, suggested_display_precision=0),
    SensorEntityDescription(key="resistance", name="Load resistance", entity_registry_enabled_default=False, native_unit_of_measurement="Ω",
        state_class=M, icon="mdi:omega", suggested_display_precision=1),
    SensorEntityDescription(key="temperature", name="Temperature", device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS, state_class=M,
        entity_category=EntityCategory.DIAGNOSTIC),
    SensorEntityDescription(key="data_plus", name="Data plus", entity_registry_enabled_default=False, device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT, state_class=M, suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC),
    SensorEntityDescription(key="data_minus", name="Data minus", entity_registry_enabled_default=False, device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT, state_class=M, suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC),
)


# ---------- Diagnostics: connection details (under 'Diagnostic' on the device page) ----------
@dataclass(frozen=True, kw_only=True)
class DiagDescription(SensorEntityDescription):
    value_fn: Callable[[TC66CCoordinator], Any]
    attrs_fn: Callable[[TC66CCoordinator], dict] | None = None


def _connected(c: TC66CCoordinator) -> bool:
    """A connected meter does not advertise: signal and proxies cannot be measured then."""
    return c.stats.status == STATUS_CONNECTED


def _best(c: TC66CCoordinator) -> dict | None:
    """Strongest proxy hearing the meter now, otherwise the last one that heard it."""
    p = c.proxies_hearing()
    return p[0] if p else c.last_seen


def _seen_attrs(c: TC66CCoordinator) -> dict:
    last = c.last_seen
    if _connected(c):
        return {
            "note": "not measurable while connected: the meter does not advertise then",
            "last_measured": f"{last['rssi']} dBm via {last['proxy']}" if last else None,
            "measured_at": last["at"].isoformat() if last else None,
        }
    live = bool(c.proxies_hearing())
    return {"source": "heard now" if live else "last heard",
            "measured_at": last["at"].isoformat() if last else None}


D = EntityCategory.DIAGNOSTIC
DIAG_SENSORS: tuple[DiagDescription, ...] = (
    DiagDescription(
        key="link_status", name="Connection status", device_class=SensorDeviceClass.ENUM,
        translation_key="link_status",
        options=STATUS_OPTIONS, entity_category=D, icon="mdi:bluetooth-connect",
        value_fn=lambda c: c.stats.status,
        attrs_fn=lambda c: {
            "command": c.stats.write_mode,
            "characteristics": c.stats.characteristics,
            # HA's explanation always says 'not seen' while connected; only show it when it means something
            **({} if _connected(c) else {"ha_explanation": c.reachability_text()}),
        },
    ),
    DiagDescription(
        key="rssi", name="Signal strength", device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT, state_class=M, entity_category=D,
        value_fn=lambda c: None if _connected(c) else ((b := _best(c)) and b["rssi"]),
        attrs_fn=_seen_attrs,
    ),
    DiagDescription(
        key="best_proxy", name="Best proxy", entity_category=D, icon="mdi:router-wireless",
        value_fn=lambda c: "not measurable (connected)" if _connected(c) else ((b := _best(c)) and b["proxy"]),
        attrs_fn=_seen_attrs,
    ),
    DiagDescription(
        key="connected_via", name="Connected via", entity_category=D, icon="mdi:bluetooth-transfer",
        value_fn=lambda c: c.connected_via(),
    ),
    DiagDescription(
        key="proxies_in_range", name="Proxies in range", state_class=M, entity_category=D,
        icon="mdi:access-point-network",
        value_fn=lambda c: None if _connected(c) else len(c.proxies_hearing()),
        attrs_fn=lambda c: (
            {"note": "not measurable while connected"}
            if _connected(c)
            else {p["proxy"]: f"{p['rssi']} dBm" for p in c.proxies_hearing()}
        ),
    ),
    DiagDescription(
        key="last_success", name="Last successful reading", entity_registry_enabled_default=False, device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=D, value_fn=lambda c: c.stats.last_success,
    ),
    DiagDescription(
        key="connected_since", name="Connected since", device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=D, value_fn=lambda c: c.stats.connected_since,
    ),
    DiagDescription(
        key="response_time", name="Response time", entity_registry_enabled_default=False, device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS, state_class=M, entity_category=D,
        value_fn=lambda c: c.stats.response_ms,
    ),
    DiagDescription(
        key="connect_time", name="Connect time", device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS, state_class=M, entity_category=D,
        value_fn=lambda c: c.stats.connect_ms,
    ),
    DiagDescription(
        key="failures_in_row", name="Failures in a row", state_class=M, entity_category=D,
        icon="mdi:alert-circle-outline", value_fn=lambda c: c.stats.failures_in_row,
    ),
    DiagDescription(
        key="failures_total", name="Failed readings", entity_category=D, icon="mdi:counter",
        value_fn=lambda c: c.stats.failures_total,
        attrs_fn=lambda c: {"successful": c.stats.readings_total, "since": "last (re)start of the integration"},
    ),
    DiagDescription(
        key="connections_total", name="Connections made", entity_category=D, icon="mdi:counter",
        value_fn=lambda c: c.stats.connections_total,
    ),
    DiagDescription(
        key="last_error", name="Last error", entity_category=D, icon="mdi:message-alert-outline",
        value_fn=lambda c: c.stats.last_error or "none",
        attrs_fn=lambda c: {"time": c.stats.last_error_at.isoformat() if c.stats.last_error_at else None},
    ),
)


# ---------- Charge session (from the meter counter, calculated when that is unusable) ----------
def _ses_attrs(c: TC66CCoordinator) -> dict:
    s = c.session
    return {
        "status": "charging" if s.active else ("finished" if s.end else "no session yet"),
        "start": s.start.isoformat() if s.start else None,
        "end": s.end.isoformat() if s.end and not s.active else None,
        "end_reason": s.end_reason if not s.active else None,
        "method": s.method,
        "resumed_after_restart": s.resumed,
        "threshold_a": c.charge_threshold,
        "end_delay_s": c.end_delay_s,
    }


SESSION_SENSORS: tuple[DiagDescription, ...] = (
    DiagDescription(
        key="session_energy", name="Charge session energy", device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR, state_class=TI, suggested_display_precision=2,
        value_fn=lambda c: round(c.session.energy_wh, 3) if c.session.start else None, attrs_fn=_ses_attrs,
    ),
    DiagDescription(
        key="session_charge", name="Charge session charge", native_unit_of_measurement="mAh",
        state_class=TI, icon="mdi:battery-charging-medium", suggested_display_precision=0,
        value_fn=lambda c: round(c.session.charge_mah, 1) if c.session.start else None,
    ),
    DiagDescription(
        key="session_duration", name="Charge session duration", device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES, icon="mdi:timer-outline",
        value_fn=lambda c: c.session.duration_min,
    ),
    DiagDescription(
        key="session_start", name="Charge session start", device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: c.session.start,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: TC66CConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [TC66CSensor(coordinator, d) for d in SENSORS]
        + [TC66CDiagSensor(coordinator, d) for d in DIAG_SENSORS]
        + [TC66CDiagSensor(coordinator, d) for d in SESSION_SENSORS]
    )


class TC66CSensor(CoordinatorEntity[TC66CCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: TC66CCoordinator, description: SensorEntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            name="TC66C",
            manufacturer="RuiDeng",
            model="TC66C",
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def native_value(self) -> float | int | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self.entity_description.key)


class TC66CDiagSensor(TC66CSensor):
    """Diagnostic and charge session sensor: stays available, also when the meter is off."""

    entity_description: DiagDescription

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator) if fn else None
