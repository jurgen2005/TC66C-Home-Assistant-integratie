"""Reading the TC66C over Bluetooth (directly or through ESPHome Bluetooth proxies)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import time

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CHAR_PAIRS,
    COMMAND,
    DOMAIN,
    EVENT_CHARGING_FINISHED,
    MAX_SOFT_FAILURES,
    RESPONSE_LEN,
    RESPONSE_TIMEOUT,
    RESUME_WINDOW_S,
)
from .protocol import decode

_LOGGER = logging.getLogger(__name__)

STATUS_CONNECTED = "connected"
STATUS_CONNECTING = "connecting"
STATUS_DISCONNECTED = "disconnected"
STATUS_OUT_OF_RANGE = "out_of_range"
STATUS_OPTIONS = [STATUS_CONNECTED, STATUS_CONNECTING, STATUS_DISCONNECTED, STATUS_OUT_OF_RANGE]

# Reasons a charge session ends (also sent in the tc66c_charging_finished event)
REASON_BELOW_THRESHOLD = "current below threshold"
REASON_UNREACHABLE = "meter unreachable"
REASON_INTERRUPTED = "interrupted: no readings for too long"


@dataclass
class LinkStats:
    """Connection details for the diagnostic sensors and the diagnostics download."""

    status: str = STATUS_DISCONNECTED
    connected_since: datetime | None = None
    last_success: datetime | None = None
    response_ms: int | None = None
    connect_ms: int | None = None
    failures_in_row: int = 0
    failures_total: int = 0
    readings_total: int = 0
    connections_total: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None
    write_mode: str | None = None
    characteristics: str | None = None
    extra: dict = field(default_factory=dict)


class MeterAbsent(Exception):
    """The meter is not advertising (off, out of range or connected to something else). Not an error."""


def _now() -> datetime:
    """Current time (separate function so tests can move the clock)."""
    return dt_util.utcnow()


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _dt(v: str | None) -> datetime | None:
    return dt_util.parse_datetime(v) if v else None


@dataclass
class ChargeSession:
    """Running or last finished charge session.

    The amount preferably comes from the meter's own running counter (group 0 or 1, whichever is
    counting). If that is unusable (cleared, jumped), HA calculates it from voltage and current.
    """

    active: bool = False
    start: datetime | None = None
    end: datetime | None = None
    energy_wh: float = 0.0
    charge_mah: float = 0.0
    end_reason: str | None = None
    last_at: datetime | None = None       # time of the last processed reading
    last_i: float = 0.0
    last_p: float = 0.0
    below_since: datetime | None = None   # since when the current is below the threshold
    steps_counter: int = 0                # steps taken from the meter counter
    steps_calc: int = 0                   # steps calculated by HA
    resumed: bool = False                 # restored after a restart or reload, no new reading yet

    @property
    def duration_min(self) -> int | None:
        if self.start is None:
            return None
        end = self.end if not self.active and self.end else _now()
        return int((end - self.start).total_seconds() // 60)

    @property
    def method(self) -> str | None:
        total = self.steps_counter + self.steps_calc
        if not total:
            return None
        if not self.steps_calc:
            return "meter counter"
        if not self.steps_counter:
            return "calculated"
        return f"mixed ({round(100 * self.steps_counter / total)}% from meter counter)"

    def to_dict(self) -> dict:
        return {
            "active": self.active, "start": _iso(self.start), "end": _iso(self.end),
            "energy_wh": self.energy_wh, "charge_mah": self.charge_mah, "end_reason": self.end_reason,
            "last_at": _iso(self.last_at), "last_i": self.last_i, "last_p": self.last_p,
            "below_since": _iso(self.below_since),
            "steps_counter": self.steps_counter, "steps_calc": self.steps_calc,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ChargeSession:
        return cls(
            active=bool(d["active"]), start=_dt(d.get("start")), end=_dt(d.get("end")),
            energy_wh=float(d.get("energy_wh", 0)), charge_mah=float(d.get("charge_mah", 0)),
            end_reason=d.get("end_reason"), last_at=_dt(d.get("last_at")),
            last_i=float(d.get("last_i", 0)), last_p=float(d.get("last_p", 0)),
            below_since=_dt(d.get("below_since")),
            steps_counter=int(d.get("steps_counter", 0)), steps_calc=int(d.get("steps_calc", 0)),
        )


COUNTER_KEYS = ("energy_0", "energy_1", "charge_0", "charge_1")


def _counters(data: dict) -> dict[str, int] | None:
    try:
        return {k: int(data[k]) for k in COUNTER_KEYS}
    except (KeyError, TypeError, ValueError):
        return None


class TC66CCoordinator(DataUpdateCoordinator[dict[str, float | int]]):
    """Keeps the BLE connection open while the meter is reachable and polls it periodically.

    Robustness:
    - a single failed reading keeps the previous values; only after MAX_SOFT_FAILURES failures
      in a row do the sensors become unavailable;
    - a lost or garbled response is requested again on the same connection first;
    - every connection attempt picks the proxy with the best signal again;
    - as soon as the meter advertises again (after switching it on) it reconnects right away;
    - as a fallback it also retries every few minutes without a new advertisement, and the
      Reconnect button forces an attempt.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        address: str,
        interval_s: int,
        charge_threshold: float = 0.10,
        end_delay_s: int = 60,
        retry_minutes: int = 5,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {address}",
            update_interval=timedelta(seconds=interval_s),
        )
        self.address = address
        self.stats = LinkStats()
        self._client: BleakClientWithServiceCache | None = None
        self._write_char: BleakGATTCharacteristic | None = None
        self._write_response = False
        self._buffer = bytearray()
        self._done = asyncio.Event()
        self._lock = asyncio.Lock()
        self._soft_failures = 0
        # Last advertisement heard (kept during a connection, when the meter is silent)
        self.last_seen: dict | None = None
        # Only (re)connect when the meter was heard after this moment. Avoids pointless
        # connection attempts and error messages when the meter is simply switched off.
        self._lost_at = time.monotonic()
        # Fallback: also retry now and then without a new advertisement (0 = off).
        self.retry_s = max(0, int(retry_minutes)) * 60
        self._last_attempt = float("-inf")
        self._force = False  # Reconnect button: next attempt without conditions
        # Charge sessions
        self.session = ChargeSession()
        self.charge_threshold = charge_threshold
        self.end_delay_s = end_delay_s
        self._max_gap_s = max(3 * interval_s, 30)
        self._prev_counters: dict[str, int] | None = None
        self._store: Store[dict] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.session")

    # ---- advertisements: the meter is (back) on ----
    @callback
    def async_start(self) -> None:
        """Listen for advertisements of the meter, to reconnect quickly after it is switched on."""
        assert self.config_entry is not None
        self.config_entry.async_on_unload(
            bluetooth.async_register_callback(
                self.hass,
                self._on_advertisement,
                bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=True),
                bluetooth.BluetoothScanningMode.PASSIVE,
            )
        )

    @callback
    def _remember(self, rssi: int, source: str) -> None:
        scanner = bluetooth.async_scanner_by_source(self.hass, source)
        self.last_seen = {
            "rssi": rssi,
            "proxy": scanner.name if scanner else source,
            "at": dt_util.utcnow(),
        }

    @callback
    def _on_advertisement(
        self, info: bluetooth.BluetoothServiceInfoBleak, _change: bluetooth.BluetoothChange
    ) -> None:
        if info is not None:
            self._remember(info.rssi, info.source)
        # A connected BT24 module does not advertise; if we see it, there is no connection.
        if self._client is None and not self._lock.locked():
            self.hass.async_create_task(self.async_request_refresh())

    # ---- which proxies hear the meter (for diagnostics) ----
    @callback
    def proxies_hearing(self) -> list[dict]:
        """All proxies/adapters that recently saw the meter, strongest signal first."""
        now = time.monotonic()
        result = []
        for dev in bluetooth.async_scanner_devices_by_address(self.hass, self.address, connectable=True):
            result.append(
                {
                    "proxy": dev.scanner.name,
                    "source": dev.scanner.source,
                    "rssi": dev.advertisement.rssi,
                }
            )
        result.sort(key=lambda r: r["rssi"], reverse=True)
        info = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
        self.stats.extra["last_advert_s_ago"] = round(now - info.time, 1) if info else None
        self._refresh_last_seen()
        return result

    @callback
    def _refresh_last_seen(self) -> float | None:
        """Last advertisement from HA's history (updated on every advertisement, even when the
        content is unchanged; the advertisement callback is not). Returns the monotonic time."""
        info = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
        if info is None:
            return None
        if not self.last_seen or self.last_seen.get("_t") != info.time:
            scanner = bluetooth.async_scanner_by_source(self.hass, info.source)
            self.last_seen = {
                "rssi": info.rssi,
                "proxy": scanner.name if scanner else info.source,
                "at": dt_util.utcnow() - timedelta(seconds=max(0.0, time.monotonic() - info.time)),
                "_t": info.time,
            }
        return info.time

    @callback
    def connected_via(self) -> str | None:
        """Which proxy/adapter holds the connection right now (from the allocated connection slots).

        Uses HA's internal BluetoothManager; returns None if that fails.
        """
        if self._client is None:
            return None
        try:
            from habluetooth import get_manager  # noqa: PLC0415 - internal, deliberately local

            for alloc in get_manager().async_current_allocations() or []:
                if self.address in alloc.allocated:
                    scanner = bluetooth.async_scanner_by_source(self.hass, alloc.source)
                    return scanner.name if scanner else alloc.source
        except Exception:  # noqa: BLE001 - diagnostics only
            _LOGGER.debug("TC66C %s: cannot determine the proxy of the connection", self.address, exc_info=True)
        return None

    @callback
    def reachability_text(self) -> str:
        """HA's own explanation of why the meter is (not) reachable for a connection."""
        try:
            return bluetooth.async_address_reachability_diagnostics(
                self.hass, self.address, bluetooth.BluetoothReachabilityIntent.CONNECTION
            )
        except Exception as err:  # noqa: BLE001 - diagnostics only
            return f"not available: {err}"

    # ---- connection ----
    def _ble_device(self) -> BLEDevice | None:
        return bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)

    def _make_disconnect_cb(self):
        def _on_disconnect(client: BleakClientWithServiceCache) -> None:
            # Only react to the current connection, not to an old one reporting late.
            if client is self._client:
                _LOGGER.debug("TC66C %s: disconnected", self.address)
                self._lost_at = time.monotonic()
                self._client = None
                self._write_char = None
                self.stats.status = STATUS_DISCONNECTED
                self.stats.connected_since = None

        return _on_disconnect

    def _on_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        self._buffer.extend(data)
        if len(self._buffer) >= RESPONSE_LEN:
            self._done.set()

    async def _ensure_connected(self) -> BleakClientWithServiceCache:
        if self._client is not None and self._client.is_connected:
            return self._client

        heard = self._refresh_last_seen()
        ble_device = self._ble_device()
        now = time.monotonic()
        # Normally: only connect when the meter was heard after the previous connection was lost.
        allowed = heard is not None and heard > self._lost_at
        reason = "new advertisement"
        if not allowed and self._force:
            allowed, reason = True, "Reconnect button"
        elif not allowed and self.retry_s and min(now - self._lost_at, now - self._last_attempt) >= self.retry_s:
            allowed, reason = True, f"fallback after {self.retry_s // 60} min"
        self._force = False
        if ble_device is None or not allowed:
            self.stats.status = STATUS_OUT_OF_RANGE
            raise MeterAbsent(
                "no proxy knows the meter (off or out of range)"
                if ble_device is None
                else "meter not heard since the last connection (off or out of range)"
            )
        self._last_attempt = now
        self.stats.extra["last_attempt_reason"] = reason
        _LOGGER.debug("TC66C %s: connecting (%s)", self.address, reason)

        self.stats.status = STATUS_CONNECTING
        started = time.monotonic()
        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            self.address,
            disconnected_callback=self._make_disconnect_cb(),
            max_attempts=3,
            ble_device_callback=lambda: self._ble_device() or ble_device,
        )
        try:
            write_char = notify_char = None
            for write_uuid, notify_uuid in CHAR_PAIRS:
                w = client.services.get_characteristic(write_uuid)
                n = client.services.get_characteristic(notify_uuid)
                if w is not None and n is not None:
                    write_char, notify_char = w, n
                    break
            if write_char is None or notify_char is None:
                found = sorted(c.uuid for s in client.services for c in s.characteristics)
                raise UpdateFailed(f"TC66C characteristics not found, found: {found}")
            await client.start_notify(notify_char, self._on_notify)
        except BaseException:
            await self._safe_disconnect(client)
            raise

        self._write_char = write_char
        # Write without response is faster and less sensitive to GATT errors through a proxy.
        self._write_response = "write-without-response" not in write_char.properties
        self._client = client
        self.stats.status = STATUS_CONNECTED
        self.stats.connected_since = dt_util.utcnow()
        self.stats.connect_ms = round((time.monotonic() - started) * 1000)
        self.stats.connections_total += 1
        self.stats.write_mode = "with response" if self._write_response else "without response"
        self.stats.characteristics = f"{write_char.uuid[4:8]} / {notify_char.uuid[4:8]}"
        _LOGGER.debug("TC66C %s: connected in %s ms", self.address, self.stats.connect_ms)
        return client

    async def _safe_disconnect(self, client: BleakClientWithServiceCache | None) -> None:
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - cleaning up must never fail itself
            _LOGGER.debug("TC66C %s: error while disconnecting ignored", self.address, exc_info=True)

    async def async_disconnect(self) -> None:
        self._lost_at = time.monotonic()
        client, self._client = self._client, None
        self._write_char = None
        if self.stats.status in (STATUS_CONNECTED, STATUS_CONNECTING):
            self.stats.status = STATUS_DISCONNECTED
        self.stats.connected_since = None
        await self._safe_disconnect(client)

    async def _clear_cache_if_corrupt(self, err: Exception) -> None:
        """'invalid literal for int() ... 0000None...': the cached GATT layout no longer matches.
        Clear it, so the next connection fetches the services again."""
        client = self._client
        if client is None or "invalid literal" not in str(err):
            return
        try:
            await client.clear_cache()
            _LOGGER.debug("TC66C %s: GATT cache cleared after: %s", self.address, err)
        except Exception:  # noqa: BLE001 - cleaning up must not fail itself
            _LOGGER.debug("TC66C %s: clearing the GATT cache failed", self.address, exc_info=True)

    async def async_force_reconnect(self) -> None:
        """Button: drop the current connection (if any) and reconnect right away."""
        _LOGGER.debug("TC66C %s: reconnect requested", self.address)
        await self.async_disconnect()
        self._force = True
        self._soft_failures = 0
        await self.async_refresh()

    # ---- reading ----
    async def _request(self, client: BleakClientWithServiceCache) -> dict[str, float | int]:
        self._buffer.clear()
        self._done.clear()
        started = time.monotonic()
        await client.write_gatt_char(self._write_char, COMMAND, response=self._write_response)
        await asyncio.wait_for(self._done.wait(), RESPONSE_TIMEOUT)
        data = decode(bytes(self._buffer))
        self.stats.response_ms = round((time.monotonic() - started) * 1000)
        return data

    async def _read(self) -> dict[str, float | int]:
        client = await self._ensure_connected()
        try:
            return await self._request(client)
        except (TimeoutError, ValueError):
            # Response lost or garbled: ask once more on the same connection.
            return await self._request(client)

    async def _async_update_data(self) -> dict[str, float | int] | None:
        async with self._lock:
            try:
                data = await self._read()
            except MeterAbsent as err:
                # Not an error: the meter is off or out of range. Log nothing, no attempts.
                _LOGGER.debug("TC66C %s: %s", self.address, err)
                self._session_no_data()
                self._soft_failures += 1
                if self.data is not None and self._soft_failures < MAX_SOFT_FAILURES:
                    return self.data
                return None
            except UpdateFailed as err:
                failure: UpdateFailed = err
            except TimeoutError:
                await self.async_disconnect()
                failure = UpdateFailed("no response from the meter")
            except (BleakError, ValueError) as err:
                await self._clear_cache_if_corrupt(err)
                await self.async_disconnect()
                failure = UpdateFailed(f"reading failed: {err}")
            else:
                self._soft_failures = 0
                self.stats.failures_in_row = 0
                self.stats.readings_total += 1
                self.stats.last_success = dt_util.utcnow()
                self._session_sample(data)
                return data

            self._soft_failures += 1
            self.stats.failures_in_row = self._soft_failures
            self.stats.failures_total += 1
            self.stats.last_error = str(failure)[:250]
            self.stats.last_error_at = dt_util.utcnow()
            self._session_no_data()

            # Single miss: keep the previous values. Only unavailable when it repeats.
            if self.data is not None and self._soft_failures < MAX_SOFT_FAILURES:
                _LOGGER.debug(
                    "TC66C %s: reading failed (%s/%s): %s",
                    self.address, self._soft_failures, MAX_SOFT_FAILURES, failure,
                )
                return self.data
            raise failure

    # ---- charge sessions ----
    async def async_load_session(self) -> None:
        """Restore the charge session from before a restart or reload."""
        try:
            stored = await self._store.async_load()
            if not stored:
                return
            self.session = ChargeSession.from_dict(stored["session"])
            self._prev_counters = stored.get("counters")
        except Exception:  # noqa: BLE001 - a broken file must not block startup
            _LOGGER.debug("TC66C %s: stored charge session unreadable, ignored", self.address, exc_info=True)
            self.session = ChargeSession()
            self._prev_counters = None
            return
        if self.session.active:
            self.session.resumed = True
            _LOGGER.debug("TC66C %s: running charge session restored (%.2f Wh)", self.address, self.session.energy_wh)

    @callback
    def _session_data(self) -> dict:
        return {"session": self.session.to_dict(), "counters": self._prev_counters}

    @callback
    def _save_session(self) -> None:
        # Batched writes (at most every 30 s); HA writes pending data on shutdown.
        self._store.async_delay_save(self._session_data, 30)

    async def async_save_session(self) -> None:
        await self._store.async_save(self._session_data())

    async def async_remove_session(self) -> None:
        await self._store.async_remove()

    def _counter_delta(
        self, counters: dict[str, int] | None, dt_s: float, current: float, power: float
    ) -> tuple[float, float] | None:
        """Increase according to the meter counter (Wh, mAh), or None if it cannot be trusted."""
        prev, ses = self._prev_counters, self.session
        if not counters or not prev:
            return None
        diffs = {k: counters[k] - prev[k] for k in COUNTER_KEYS}
        if min(diffs.values()) < 0:  # counter cleared or new session in the meter
            return None
        d_wh = max(diffs["energy_0"], diffs["energy_1"]) / 1000
        d_mah = float(max(diffs["charge_0"], diffs["charge_1"]))
        dt_h = dt_s / 3600
        # Must match what voltage and current say (wide margin for fluctuations and rounding).
        p_lo, p_hi = min(ses.last_p, power), max(ses.last_p, power)
        i_lo, i_hi = min(ses.last_i, current), max(ses.last_i, current)
        if not (p_lo * 0.5 * dt_h - 0.02 <= d_wh <= p_hi * 1.5 * dt_h + 0.02):
            return None
        if not (i_lo * 500 * dt_h - 2 <= d_mah <= i_hi * 1500 * dt_h + 2):
            return None
        return d_wh, d_mah

    def _session_sample(self, data: dict[str, float | int]) -> None:
        """New reading: add the amount, detect start and end of charging."""
        now, ses = _now(), self.session
        current, power = float(data["current"]), float(data["power"])
        counters = _counters(data)

        # Restored after a restart, but the interruption was too long: close that session first.
        if ses.active and ses.resumed and ses.last_at and (now - ses.last_at).total_seconds() > RESUME_WINDOW_S:
            self._session_finish(REASON_INTERRUPTED, ses.last_at)
            ses = self.session

        if ses.active and ses.last_at is not None:
            dt_s = (now - ses.last_at).total_seconds()
            if dt_s > 0:
                delta = self._counter_delta(counters, dt_s, current, power)
                if delta is not None:
                    ses.energy_wh += delta[0]
                    ses.charge_mah += delta[1]
                    ses.steps_counter += 1
                elif dt_s <= self._max_gap_s:  # calculate, but not across a gap
                    dt_h = dt_s / 3600
                    ses.energy_wh += (ses.last_p + power) / 2 * dt_h
                    ses.charge_mah += (ses.last_i + current) / 2 * 1000 * dt_h
                    ses.steps_calc += 1
        ses.resumed = False

        if current >= self.charge_threshold:
            ses.below_since = None
            if not ses.active:
                self.session = ses = ChargeSession(active=True, start=now)
                _LOGGER.debug("TC66C %s: charging started (%.2f A)", self.address, current)
        elif ses.active:
            ses.below_since = ses.below_since or now
            if (now - ses.below_since).total_seconds() >= self.end_delay_s:
                self._session_finish(REASON_BELOW_THRESHOLD, ses.below_since)

        ses.last_at, ses.last_i, ses.last_p = now, current, power
        self._prev_counters = counters
        self._save_session()

    def _session_no_data(self) -> None:
        """No reading: after the end delay (after a restart: the resume window) charging is over."""
        ses = self.session
        if not ses.active or ses.last_at is None:
            return
        limit = RESUME_WINDOW_S if ses.resumed else self.end_delay_s
        if (_now() - ses.last_at).total_seconds() >= limit:
            self._session_finish(REASON_UNREACHABLE, ses.last_at)

    def _session_finish(self, reason: str, end: datetime | None = None) -> None:
        ses = self.session
        ses.active = False
        ses.resumed = False
        ses.end = end or _now()
        ses.end_reason = reason
        event = {
            "address": self.address,
            "energy_wh": round(ses.energy_wh, 2),
            "charge_mah": round(ses.charge_mah),
            "duration_min": ses.duration_min,
            "start": _iso(ses.start),
            "end": _iso(ses.end),
            "reason": reason,
            "method": ses.method,
        }
        _LOGGER.debug("TC66C %s: charging finished: %s", self.address, event)
        self.hass.bus.async_fire(EVENT_CHARGING_FINISHED, event)
        self._save_session()
