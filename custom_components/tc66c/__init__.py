"""TC66C USB meter over Bluetooth."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CHARGE_THRESHOLD,
    CONF_END_DELAY,
    CONF_RETRY_MINUTES,
    CONF_SCAN_INTERVAL,
    DEFAULT_CHARGE_THRESHOLD,
    DEFAULT_END_DELAY,
    DEFAULT_RETRY_MINUTES,
    DEFAULT_SCAN_INTERVAL,
)
from .coordinator import TC66CCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]

type TC66CConfigEntry = ConfigEntry[TC66CCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: TC66CConfigEntry) -> bool:
    interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator = TC66CCoordinator(
        hass,
        entry,
        entry.data[CONF_ADDRESS],
        interval,
        float(entry.options.get(CONF_CHARGE_THRESHOLD, DEFAULT_CHARGE_THRESHOLD)),
        int(entry.options.get(CONF_END_DELAY, DEFAULT_END_DELAY)),
        int(entry.options.get(CONF_RETRY_MINUTES, DEFAULT_RETRY_MINUTES)),
    )
    entry.runtime_data = coordinator
    # Restore a running charge session from before a restart or reload.
    await coordinator.async_load_session()
    coordinator.async_start()
    # Options changed: reload the integration (the running charge session is kept).
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # First reading in the background: the meter is often off and startup must not wait for it.
    # Until there is a reading, the sensors are simply unavailable.
    entry.async_create_background_task(hass, coordinator.async_refresh(), f"tc66c first refresh {entry.title}")
    return True


async def _async_reload(hass: HomeAssistant, entry: TC66CConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: TC66CConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_save_session()
        await entry.runtime_data.async_disconnect()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: TC66CConfigEntry) -> None:
    """Integration removed: also remove the stored charge session."""
    from homeassistant.helpers.storage import Store  # noqa: PLC0415

    from .const import DOMAIN  # noqa: PLC0415

    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.session").async_remove()
