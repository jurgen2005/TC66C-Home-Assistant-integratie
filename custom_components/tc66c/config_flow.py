"""Config flow: automatic through Bluetooth discovery, or pick manually."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

import re

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

from .const import (
    CONF_CHARGE_THRESHOLD,
    CONF_END_DELAY,
    CONF_RETRY_MINUTES,
    DEFAULT_CHARGE_THRESHOLD,
    DEFAULT_END_DELAY,
    DEFAULT_RETRY_MINUTES,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

NAME_PREFIXES = ("BT24", "TC66")
SERVICE_UUIDS = {"0000ffe0-0000-1000-8000-00805f9b34fb", "0000ffe5-0000-1000-8000-00805f9b34fb"}
CONF_MANUAL = "manual_address"
MAC_RE = re.compile(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}")


def _title(info: BluetoothServiceInfoBleak) -> str:
    return f"TC66C ({info.address})"


class TC66CConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TC66COptionsFlow()

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._candidates: dict[str, str] = {}

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": _title(discovery_info)}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._discovery is not None
        if user_input is not None:
            return self.async_create_entry(
                title=_title(self._discovery), data={CONF_ADDRESS: self._discovery.address}
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": self._discovery.name or "?", "address": self._discovery.address},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            address = (user_input.get(CONF_MANUAL) or user_input.get(CONF_ADDRESS) or "").strip().upper()
            if not MAC_RE.fullmatch(address):
                errors["base"] = "invalid_address"
            else:
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"TC66C ({address})", data={CONF_ADDRESS: address})

        current = self._async_current_ids(include_ignore=False)
        likely: list[BluetoothServiceInfoBleak] = []
        other: list[BluetoothServiceInfoBleak] = []
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in current:
                continue
            uuids = {u.lower() for u in info.service_uuids}
            if (info.name or "").startswith(NAME_PREFIXES) or uuids & SERVICE_UUIDS:
                likely.append(info)
            else:
                other.append(info)
        likely.sort(key=lambda i: -i.rssi)
        other.sort(key=lambda i: -i.rssi)

        options: dict[str, str] = {}
        for info in likely:
            options[info.address] = f"★ {info.name or 'unnamed'} ({info.address}, {info.rssi} dBm)"
        for info in other[:40]:
            options[info.address] = f"{info.name or 'unnamed'} ({info.address}, {info.rssi} dBm)"

        schema: dict[Any, Any] = {}
        if options:
            schema[vol.Optional(CONF_ADDRESS)] = vol.In(options)
        schema[vol.Optional(CONF_MANUAL)] = str
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"likely": str(len(likely)), "total": str(len(likely) + len(other))},
        )


class TC66COptionsFlow(OptionsFlow):
    """Options: polling interval, charge session and reconnecting."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        o = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=o.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): vol.All(
                        vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                    ),
                    vol.Required(
                        CONF_CHARGE_THRESHOLD, default=o.get(CONF_CHARGE_THRESHOLD, DEFAULT_CHARGE_THRESHOLD)
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.01, max=5)),
                    vol.Required(CONF_END_DELAY, default=o.get(CONF_END_DELAY, DEFAULT_END_DELAY)): vol.All(
                        vol.Coerce(int), vol.Range(min=10, max=3600)
                    ),
                    vol.Required(
                        CONF_RETRY_MINUTES, default=o.get(CONF_RETRY_MINUTES, DEFAULT_RETRY_MINUTES)
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=60)),
                }
            ),
        )
