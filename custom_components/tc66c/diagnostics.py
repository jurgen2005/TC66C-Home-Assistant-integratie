"""Diagnostics download (device page, three dots, Download diagnostics)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from . import TC66CConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: TC66CConfigEntry) -> dict[str, Any]:
    c = entry.runtime_data
    stats = asdict(c.stats)
    for k, v in list(stats.items()):
        if hasattr(v, "isoformat"):
            stats[k] = v.isoformat()
    return {
        "address": c.address,
        "interval_s": c.update_interval.total_seconds() if c.update_interval else None,
        "options": dict(entry.options),
        "last_update_success": c.last_update_success,
        "last_exception": repr(c.last_exception) if c.last_exception else None,
        "link": stats,
        "proxies_hearing": c.proxies_hearing(),
        "learned_advertising_interval_s": bluetooth.async_get_learned_advertising_interval(hass, c.address),
        "ha_reachability": c.reachability_text(),
        "last_values": c.data,
        "charge_session": {
            "active": c.session.active,
            "start": c.session.start.isoformat() if c.session.start else None,
            "end": c.session.end.isoformat() if c.session.end else None,
            "energy_wh": round(c.session.energy_wh, 3),
            "charge_mah": round(c.session.charge_mah, 1),
            "end_reason": c.session.end_reason,
            "method": c.session.method,
            "steps_counter": c.session.steps_counter,
            "steps_calc": c.session.steps_calc,
            "resumed": c.session.resumed,
            "last_sample": c.session.last_at.isoformat() if c.session.last_at else None,
            "threshold_a": c.charge_threshold,
            "end_delay_s": c.end_delay_s,
        },
    }
