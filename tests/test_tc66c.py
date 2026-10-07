import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from bleak.backends.device import BLEDevice

from custom_components.tc66c.const import AES_KEY, DOMAIN
from custom_components.tc66c.protocol import _crc16, decode
from custom_components.tc66c.coordinator import TC66CCoordinator as _Coord

ORIG_REFRESH = _Coord._refresh_last_seen  # saved before the autouse patch

ADDR = "AA:BB:CC:DD:EE:FF"


def make_packet(corrupt=False):
    d = bytearray(192)
    for i in range(3):
        d[i * 64 : i * 64 + 4] = f"pac{i+1}".encode()
    def put(o, v): struct.pack_into("<I", d, o, v)
    put(48, 199434); put(52, 42390); put(56, 84525); put(68, 470)
    put(72, 6); put(76, 31); put(80, 3126); put(84, 61595); put(88, 0); put(92, 47); put(96, 105); put(100, 93)
    for i in range(3):
        b = d[i * 64 : (i + 1) * 64]
        struct.pack_into("<I", d, i * 64 + 60, _crc16(bytes(b[:60])))
    if corrupt:
        d[50] ^= 0xFF
    enc = Cipher(algorithms.AES(AES_KEY), modes.ECB()).encryptor()
    return enc.update(bytes(d)) + enc.finalize()


def test_decode():
    v = decode(make_packet())
    assert v["voltage"] == 19.9434 and v["current"] == 0.4239 and v["power"] == 8.4525
    assert v["charge_1"] == 3126 and v["energy_1"] == 61595 and v["charge_0"] == 6
    assert v["temperature"] == 47 and v["data_plus"] == 1.05 and v["data_minus"] == 0.93
    assert v["resistance"] == 47.0


def test_decode_rejects_corrupt():
    import pytest
    with pytest.raises(ValueError):
        decode(make_packet(corrupt=True))


def service_info():
    return BluetoothServiceInfoBleak(
        name="BT24-M", address=ADDR, rssi=-60, manufacturer_data={}, service_data={},
        service_uuids=["0000ffe0-0000-1000-8000-00805f9b34fb"], source="local",
        device=BLEDevice(ADDR, "BT24-M", None), advertisement=None, connectable=True,
        time=0, tx_power=None,
    )


async def test_bluetooth_discovery_flow(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=service_info()
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "bluetooth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ADDRESS: ADDR}


class FakeClient:
    def __init__(self, uuids):
        self.is_connected = True
        self._cb = None
        self.services = MagicMock()
        chars = {}
        for u in uuids:
            c = MagicMock(); c.uuid = u; c.properties = ["write", "notify"]; chars[u] = c
        self.services.get_characteristic = lambda u: chars.get(u)
        svc = MagicMock(); svc.characteristics = list(chars.values())
        self.services.__iter__ = lambda s: iter([svc])
        self.writes = 0

    async def start_notify(self, char, cb):
        self._cb = cb

    async def write_gatt_char(self, char, data, response=True):
        assert data == b"bgetva\r\n"
        self.writes += 1
        pkt = make_packet()
        async def send():
            for i in range(0, 192, 20):
                await asyncio.sleep(0)
                self._cb(None, bytearray(pkt[i:i + 20]))
        asyncio.get_running_loop().create_task(send())

    async def disconnect(self):
        self.is_connected = False


async def setup(hass, client, device_present=True):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDR}, unique_id=ADDR, title="TC66C")
    entry.add_to_hass(hass)
    dev = BLEDevice(ADDR, "BT24-M", None) if device_present else None
    with patch("custom_components.tc66c.coordinator.bluetooth.async_ble_device_from_address", return_value=dev), \
         patch("custom_components.tc66c.coordinator.establish_connection", AsyncMock(return_value=client)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def test_sensors_ffe9_variant(hass):
    client = FakeClient(["0000ffe9-0000-1000-8000-00805f9b34fb", "0000ffe4-0000-1000-8000-00805f9b34fb"])
    entry = await setup(hass, client)
    states = {s.entity_id: s.state for s in hass.states.async_all("sensor")}
    print("EXC", repr(entry.runtime_data.last_exception))
    assert states["sensor.tc66c_voltage"] == "19.9434"
    assert states["sensor.tc66c_energy_group_1"] == "61595"
    assert "sensor.tc66c_data_plus" not in states  # disabled by default (fewer database writes)
    assert entry.runtime_data.data["data_plus"] == 1.05
    assert client.writes == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert client.is_connected is False


async def test_sensors_ffe1_variant(hass):
    client = FakeClient(["0000ffe2-0000-1000-8000-00805f9b34fb", "0000ffe1-0000-1000-8000-00805f9b34fb"])
    await setup(hass, client)
    assert hass.states.get("sensor.tc66c_current").state == "0.4239"


async def test_meter_off_unavailable(hass):
    await setup(hass, FakeClient([]), device_present=False)
    assert hass.states.get("sensor.tc66c_voltage").state == "unavailable"


async def test_reuses_connection_and_handles_timeout(hass):
    client = FakeClient(["0000ffe9-0000-1000-8000-00805f9b34fb", "0000ffe4-0000-1000-8000-00805f9b34fb"])
    est = AsyncMock(return_value=client)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDR}, unique_id=ADDR, title="TC66C")
    entry.add_to_hass(hass)
    with patch("custom_components.tc66c.coordinator.bluetooth.async_ble_device_from_address",
               return_value=BLEDevice(ADDR, "BT24-M", None)), \
         patch("custom_components.tc66c.coordinator.establish_connection", est), \
         patch("custom_components.tc66c.coordinator.RESPONSE_TIMEOUT", 0.2):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        coord = entry.runtime_data
        await coord.async_refresh()
        assert client.writes == 2 and est.await_count == 1
        # meter stops answering: keep previous values first, unavailable after 3 misses
        async def silent(*a, **k): client.writes += 1
        client.write_gatt_char = silent
        await coord.async_refresh()
        assert hass.states.get("sensor.tc66c_voltage").state == "19.9434"
        assert client.is_connected is False
        await coord.async_refresh()
        assert hass.states.get("sensor.tc66c_voltage").state == "19.9434"
        await coord.async_refresh()
        assert hass.states.get("sensor.tc66c_voltage").state == "unavailable"
        assert "no response" in str(coord.last_exception)
        # meter back: reconnects and becomes available again
        client2 = FakeClient(["0000ffe9-0000-1000-8000-00805f9b34fb", "0000ffe4-0000-1000-8000-00805f9b34fb"])
        est.return_value = client2
        await coord.async_refresh()
        assert hass.states.get("sensor.tc66c_voltage").state == "19.9434"


async def test_options_interval(hass):
    import pytest
    from homeassistant.data_entry_flow import InvalidData
    client = FakeClient(["0000ffe9-0000-1000-8000-00805f9b34fb", "0000ffe4-0000-1000-8000-00805f9b34fb"])
    with patch("custom_components.tc66c.coordinator.bluetooth.async_ble_device_from_address",
               return_value=BLEDevice(ADDR, "BT24-M", None)), \
         patch("custom_components.tc66c.coordinator.establish_connection", AsyncMock(return_value=client)):
        entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDR}, unique_id=ADDR, title="TC66C")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.runtime_data.update_interval.total_seconds() == 5
        r = await hass.config_entries.options.async_init(entry.entry_id)
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(r["flow_id"], {"scan_interval": 1})
        r = await hass.config_entries.options.async_init(entry.entry_id)
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"scan_interval": 30})
        assert r["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.runtime_data.update_interval.total_seconds() == 30
        assert hass.states.get("sensor.tc66c_voltage").state == "19.9434"


async def test_user_flow_lists_and_manual(hass):
    from custom_components.tc66c import config_flow as cf
    other = service_info()
    other2 = BluetoothServiceInfoBleak(
        name="Fridge", address="11:22:33:44:55:66", rssi=-80, manufacturer_data={}, service_data={},
        service_uuids=[], source="local", device=BLEDevice("11:22:33:44:55:66", "Fridge", None),
        advertisement=None, connectable=True, time=0, tx_power=None)
    with patch.object(cf, "async_discovered_service_info", return_value=[other2, other]):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert r["type"] is FlowResultType.FORM
        assert r["description_placeholders"] == {"likely": "1", "total": "2"}
        r2 = await hass.config_entries.flow.async_configure(r["flow_id"], {"manual_address": "zzz"})
        assert r2["errors"] == {"base": "invalid_address"}
        r3 = await hass.config_entries.flow.async_configure(r["flow_id"], {"manual_address": "aa:bb:cc:dd:ee:01"})
        assert r3["type"] is FlowResultType.CREATE_ENTRY and r3["data"] == {CONF_ADDRESS: "AA:BB:CC:DD:EE:01"}


async def test_advertisement_triggers_reconnect(hass):
    entry = await setup(hass, FakeClient([]), device_present=False)
    assert hass.states.get("sensor.tc66c_voltage").state == "unavailable"
    client = FakeClient(["0000ffe9-0000-1000-8000-00805f9b34fb", "0000ffe4-0000-1000-8000-00805f9b34fb"])
    with patch("custom_components.tc66c.coordinator.bluetooth.async_ble_device_from_address",
               return_value=BLEDevice(ADDR, "BT24-M", None)), \
         patch("custom_components.tc66c.coordinator.establish_connection", AsyncMock(return_value=client)):
        entry.runtime_data._on_advertisement(None, None)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.tc66c_voltage").state == "19.9434"


async def test_diagnostics(hass):
    from types import SimpleNamespace
    from custom_components.tc66c.diagnostics import async_get_config_entry_diagnostics
    fake = [SimpleNamespace(scanner=SimpleNamespace(name="proxy-kitchen", source="EC:DA"), advertisement=SimpleNamespace(rssi=-79)),
            SimpleNamespace(scanner=SimpleNamespace(name="proxy-mini-1", source="0C:B8"), advertisement=SimpleNamespace(rssi=-71))]
    with patch("custom_components.tc66c.coordinator.bluetooth.async_scanner_devices_by_address", return_value=fake):
        # meter off: diagnostics stay available
        entry = await setup(hass, FakeClient([]), device_present=False)
        st = lambda e: hass.states.get(e)
        assert st("sensor.tc66c_voltage").state == "unavailable"
        assert st("sensor.tc66c_connection_status").state == "out_of_range"
        # meter off is not an error: nothing counted, nothing logged
        assert st("sensor.tc66c_failures_in_a_row").state == "0"
        assert st("sensor.tc66c_last_error").state == "none"
        assert st("sensor.tc66c_signal_strength").state == "-71"
        assert st("sensor.tc66c_best_proxy").state == "proxy-mini-1"
        assert st("sensor.tc66c_proxies_in_range").state == "2"
        assert st("sensor.tc66c_proxies_in_range").attributes["proxy-kitchen"] == "-79 dBm"
        assert "ha_explanation" in st("sensor.tc66c_connection_status").attributes
        assert st("sensor.tc66c_connected_via").state == "unknown"
        # meter on: connected, times filled
        client = FakeClient(["0000ffe9-0000-1000-8000-00805f9b34fb", "0000ffe4-0000-1000-8000-00805f9b34fb"])
        with patch("custom_components.tc66c.coordinator.bluetooth.async_ble_device_from_address",
                   return_value=BLEDevice(ADDR, "BT24-M", None)), \
             patch("custom_components.tc66c.coordinator.establish_connection", AsyncMock(return_value=client)):
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()
        # while connected no proxy hears the meter
        from homeassistant.util import dt as dt_util
        entry.runtime_data.last_seen = {"rssi": -71, "proxy": "proxy-mini-1", "at": dt_util.utcnow()}
        from types import SimpleNamespace as NS
        fake_mgr = NS(async_current_allocations=lambda: [NS(source="0C:B8", allocated=[ADDR]), NS(source="EC:DA", allocated=[])])
        with patch("custom_components.tc66c.coordinator.bluetooth.async_scanner_devices_by_address", return_value=[]), \
             patch("habluetooth.get_manager", return_value=fake_mgr), \
             patch("custom_components.tc66c.coordinator.bluetooth.async_scanner_by_source", return_value=NS(name="proxy-mini-1")):
            entry.runtime_data.async_update_listeners(); await hass.async_block_till_done()
            # not measurable while connected: no stale value as state, but in the attributes
            assert st("sensor.tc66c_signal_strength").state == "unknown"
            assert st("sensor.tc66c_signal_strength").attributes["last_measured"] == "-71 dBm via proxy-mini-1"
            assert st("sensor.tc66c_best_proxy").state == "not measurable (connected)"
            assert st("sensor.tc66c_proxies_in_range").state == "unknown"
            assert st("sensor.tc66c_connected_via").state == "proxy-mini-1"
            assert "ha_explanation" not in st("sensor.tc66c_connection_status").attributes
        assert st("sensor.tc66c_connection_status").state == "connected"
        assert st("sensor.tc66c_failures_in_a_row").state == "0"
        assert st("sensor.tc66c_connections_made").state == "1"
        assert entry.runtime_data.stats.response_ms is not None
        assert st("sensor.tc66c_connected_since").state not in ("unknown", "unavailable")
        diag = await async_get_config_entry_diagnostics(hass, entry)
        assert diag["link"]["status"] == "connected" and diag["proxies_hearing"][0]["proxy"] == "proxy-mini-1"
        print({k: diag[k] for k in ("link", "ha_reachability")})



async def test_meter_off_is_silent(hass, caplog, meter_heard_recently):
    """Connection lost and meter not heard again: no connection attempts and no error."""
    import time as _t
    client = FakeClient(["0000ffe9-0000-1000-8000-00805f9b34fb", "0000ffe4-0000-1000-8000-00805f9b34fb"])
    est = AsyncMock(return_value=client)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDR}, unique_id=ADDR, title="TC66C")
    entry.add_to_hass(hass)
    with patch("custom_components.tc66c.coordinator.bluetooth.async_ble_device_from_address",
               return_value=BLEDevice(ADDR, "BT24-M", None)), \
         patch("custom_components.tc66c.coordinator.establish_connection", est):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        c = entry.runtime_data
        assert hass.states.get("sensor.tc66c_voltage").state == "19.9434"
        # meter off: connection lost, not heard afterwards
        heard = _t.monotonic() - 100
        with patch("custom_components.tc66c.coordinator.TC66CCoordinator._refresh_last_seen", lambda self: heard):
            await c.async_disconnect()
            caplog.clear()
            for _ in range(4):
                await c.async_refresh()
            assert est.await_count == 1  # no new attempts
            assert hass.states.get("sensor.tc66c_voltage").state == "unavailable"
            assert hass.states.get("sensor.tc66c_connection_status").state == "out_of_range"
            assert not [r for r in caplog.records if r.levelname == "ERROR"]
            # meter on again: new advertisement -> connect
        with patch("custom_components.tc66c.coordinator.TC66CCoordinator._refresh_last_seen",
                   lambda self: _t.monotonic() + 10):
            await c.async_refresh()
            assert est.await_count == 2
            assert hass.states.get("sensor.tc66c_voltage").state == "19.9434"


class Clock:
    def __init__(self):
        from datetime import datetime, timezone
        self.t = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    def __call__(self):
        return self.t
    def add(self, s):
        from datetime import timedelta
        self.t += timedelta(seconds=s)


def sample(i, p, e0=None, c0=None, e1=1000, c1=100):
    d = {"current": i, "power": p}
    if e0 is not None:
        d.update(energy_0=e0, charge_0=c0, energy_1=e1, charge_1=c1)
    return d


async def test_charge_session_and_event(hass):
    """Charge session without counters (calculated): starts, accumulates, ends with an event."""
    from custom_components.tc66c.coordinator import TC66CCoordinator
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDR}, unique_id=ADDR)
    entry.add_to_hass(hass)
    c = TC66CCoordinator(hass, entry, ADDR, 5, charge_threshold=0.1, end_delay_s=60)
    events = []
    hass.bus.async_listen("tc66c_charging_finished", lambda e: events.append(e.data))
    clock = Clock()
    with patch("custom_components.tc66c.coordinator._now", clock):
        for _ in range(721):  # 1 hour, 2 A at 20 V = 40 W
            c._session_sample(sample(2.0, 40.0)); clock.add(5)
        assert c.session.active
        assert abs(c.session.energy_wh - 40.0) < 0.01 and abs(c.session.charge_mah - 2000) < 1
        assert c.session.method == "calculated"
        for _ in range(13):
            c._session_sample(sample(0.02, 0.4)); clock.add(5)
        await hass.async_block_till_done()
        assert not c.session.active and c.session.end_reason == "current below threshold"
        assert len(events) == 1 and events[0]["energy_wh"] > 39.9 and events[0]["method"] == "calculated"
        # end = moment the current dropped, not 60 s later
        assert events[0]["duration_min"] == 60
        # new session, then meter gone: finished after 60 s without data
        c._session_sample(sample(1.0, 5.0)); clock.add(5)
        c._session_sample(sample(1.0, 5.0))
        assert c.session.active and c.session.energy_wh > 0
        clock.add(61)
        c._session_no_data()
        await hass.async_block_till_done()
        assert len(events) == 2 and events[1]["reason"] == "meter unreachable"
        # a gap in the readings (> max gap) does not count without counters
        c._session_sample(sample(1.0, 10.0)); clock.add(3600)
        c._session_sample(sample(1.0, 10.0))
        assert c.session.energy_wh == 0


async def test_session_uses_meter_counter(hass):
    """With counters: amount from the meter counter, also across a gap; calculate when the counter was cleared."""
    from custom_components.tc66c.coordinator import TC66CCoordinator
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDR}, unique_id=ADDR)
    entry.add_to_hass(hass)
    c = TC66CCoordinator(hass, entry, ADDR, 10)
    clock = Clock()
    e0, c0 = 50000, 2500
    with patch("custom_components.tc66c.coordinator._now", clock):
        # 40 W / 2 A, every 10 s: counter rises 111 mWh and 5.56 mAh per step (rounded)
        for k in range(361):
            c._session_sample(sample(2.0, 40.0, e0 + round(k * 40000 / 360), c0 + round(k * 2000 / 360)))
            clock.add(10)
        assert c.session.method == "meter counter"
        assert abs(c.session.energy_wh - 40.0) < 0.002 and abs(c.session.charge_mah - 2000) < 0.5
        # 2 minute gap (no readings): the counter covers the gap
        clock.add(110)
        c._session_sample(sample(2.0, 40.0, e0 + 40000 + 1444, c0 + 2000 + 72))
        assert abs(c.session.energy_wh - 41.444) < 0.002
        # counter cleared (K1): calculate that step
        clock.add(10)
        c._session_sample(sample(2.0, 40.0, 0, 0))
        assert c.session.steps_calc == 1 and abs(c.session.energy_wh - (41.444 + 40 / 360)) < 0.002
        assert c.session.method.startswith("mixed")
        # implausible jump (switched to a group with a high count): calculate as well
        clock.add(10)
        c._session_sample(sample(2.0, 40.0, 0, 0, e1=900000, c1=50000))
        assert c.session.steps_calc == 2


async def test_session_survives_reload(hass):
    """A running charge session is kept on reload (options changed) and continues."""
    client = FakeClient(["0000ffe9-0000-1000-8000-00805f9b34fb", "0000ffe4-0000-1000-8000-00805f9b34fb"])
    entry = await setup(hass, client)
    c = entry.runtime_data
    assert c.session.active
    c.session.energy_wh = 57.4
    start = c.session.start
    with patch("custom_components.tc66c.coordinator.bluetooth.async_ble_device_from_address",
               return_value=BLEDevice(ADDR, "BT24-M", None)), \
         patch("custom_components.tc66c.coordinator.establish_connection", AsyncMock(return_value=client)):
        hass.config_entries.async_update_entry(entry, options={"scan_interval": 10})
        await hass.async_block_till_done(wait_background_tasks=True)
    c2 = entry.runtime_data
    assert c2 is not c
    assert c2.session.active and c2.session.start == start and c2.session.energy_wh >= 57.4
    assert hass.states.get("sensor.tc66c_charge_session_energy").attributes["start"] == start.isoformat()


async def test_session_resume_window(hass):
    """After a restart: continues within 15 min, otherwise closed with the right reason."""
    from custom_components.tc66c.coordinator import ChargeSession, TC66CCoordinator
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDR}, unique_id=ADDR)
    entry.add_to_hass(hass)
    clock = Clock()
    events = []
    hass.bus.async_listen("tc66c_charging_finished", lambda e: events.append(e.data))
    with patch("custom_components.tc66c.coordinator._now", clock):
        c = TC66CCoordinator(hass, entry, ADDR, 10)
        c.session = ChargeSession(active=True, start=clock.t, energy_wh=10.0, last_at=clock.t, last_i=2, last_p=40)
        await c.async_save_session()
        # restart of 3 minutes
        c2 = TC66CCoordinator(hass, entry, ADDR, 10)
        await c2.async_load_session()
        assert c2.session.active and c2.session.resumed
        clock.add(120)
        c2._session_no_data()  # 2 min without data: do not close yet (not the normal 60 s)
        assert c2.session.active
        clock.add(60)
        c2._session_sample(sample(2.0, 40.0))
        assert c2.session.active and not c2.session.resumed and c2.session.energy_wh == 10.0  # gap without counters
        # second restart, now 20 minutes
        await c2.async_save_session()
        c3 = TC66CCoordinator(hass, entry, ADDR, 10)
        await c3.async_load_session()
        clock.add(1200)
        c3._session_sample(sample(2.0, 40.0))
        await hass.async_block_till_done()
        assert events and events[0]["reason"] == "interrupted: no readings for too long"
        assert c3.session.active and c3.session.energy_wh == 0  # new session


async def test_diag_while_connected(hass):
    client = FakeClient(["0000ffe9-0000-1000-8000-00805f9b34fb", "0000ffe4-0000-1000-8000-00805f9b34fb"])
    await setup(hass, client)
    assert hass.states.get("sensor.tc66c_connection_status").state == "connected"
    assert hass.states.get("sensor.tc66c_signal_strength").state == "unknown"
    assert hass.states.get("sensor.tc66c_proxies_in_range").state == "unknown"
    assert hass.states.get("sensor.tc66c_best_proxy").state == "not measurable (connected)"
    assert hass.states.get("sensor.tc66c_last_error").state == "none"


async def test_session_entities(hass):
    client = FakeClient(["0000ffe9-0000-1000-8000-00805f9b34fb", "0000ffe4-0000-1000-8000-00805f9b34fb"])
    await setup(hass, client)
    assert hass.states.get("binary_sensor.tc66c_charging").state == "on"   # 0.42 A > 0.1 A
    assert hass.states.get("sensor.tc66c_charge_session_energy").attributes["status"] == "charging"
    assert hass.states.get("sensor.tc66c_charge_session_start").state not in ("unknown", "unavailable")


async def test_refresh_last_seen_real(hass):
    """The real function (replaced in other tests) reads HA's advertisement history."""
    import time as _t
    from types import SimpleNamespace as NS
    from custom_components.tc66c.coordinator import TC66CCoordinator
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDR}, unique_id=ADDR)
    entry.add_to_hass(hass)
    with patch("custom_components.tc66c.coordinator.TC66CCoordinator._refresh_last_seen",
               ORIG_REFRESH):
        c = TC66CCoordinator(hass, entry, ADDR, 5)
        info = NS(time=_t.monotonic() - 3, rssi=-74, source="EC:DA")
        with patch("custom_components.tc66c.coordinator.bluetooth.async_last_service_info", return_value=info), \
             patch("custom_components.tc66c.coordinator.bluetooth.async_scanner_by_source", return_value=NS(name="proxy-kitchen")):
            t = c._refresh_last_seen()
        assert t == info.time and c.last_seen["rssi"] == -74 and c.last_seen["proxy"] == "proxy-kitchen"
        # an advertisement from before the integration started does not count as 'heard since'
        assert t <= c._lost_at


async def test_reconnect_button_and_retry(hass, meter_heard_recently):
    """Button connects without a new advertisement; the fallback retries after x minutes."""
    import time as _t
    client = FakeClient(["0000ffe9-0000-1000-8000-00805f9b34fb", "0000ffe4-0000-1000-8000-00805f9b34fb"])
    est = AsyncMock(return_value=client)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDR}, unique_id=ADDR, title="TC66C",
                            options={"retry_minutes": 5})
    entry.add_to_hass(hass)
    dev = BLEDevice(ADDR, "BT24-M", None)
    old_advert = lambda self: 0.0  # only an old advertisement: normally no attempt
    with patch("custom_components.tc66c.coordinator.bluetooth.async_ble_device_from_address", return_value=dev), \
         patch("custom_components.tc66c.coordinator.establish_connection", est):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        c = entry.runtime_data
        assert est.await_count == 1
        # connection lost, meter not heard again
        with patch("custom_components.tc66c.coordinator.TC66CCoordinator._refresh_last_seen", old_advert):
            await c.async_disconnect()
            await c.async_refresh()
            assert est.await_count == 1 and c.stats.status == "out_of_range"
            # button
            buttons = [s.entity_id for s in hass.states.async_all("button")]
            assert len(buttons) == 1
            await hass.services.async_call("button", "press", {"entity_id": buttons[0]}, blocking=True)
            await hass.async_block_till_done()
            assert est.await_count == 2 and c.stats.status == "connected"
            assert c.stats.extra["last_attempt_reason"] == "Reconnect button"
            # fallback: try after 5 min without an advertisement
            await c.async_disconnect()
            await c.async_refresh()
            assert est.await_count == 2
            later = _t.monotonic() + 301
            with patch("custom_components.tc66c.coordinator.time.monotonic", lambda: later):
                await c.async_refresh()
            assert est.await_count == 3 and c.stats.extra["last_attempt_reason"] == "fallback after 5 min"


async def test_corrupt_gatt_cache_cleared(hass):
    client = FakeClient(["0000ffe9-0000-1000-8000-00805f9b34fb", "0000ffe4-0000-1000-8000-00805f9b34fb"])
    client.clear_cache = AsyncMock(return_value=True)
    entry = await setup(hass, client)
    c = entry.runtime_data
    async def bad(*a, **k):
        raise ValueError("invalid literal for int() with base 16: '0000None00001000800000805f9b34fb'")
    client.write_gatt_char = bad
    await c.async_refresh()
    assert client.clear_cache.await_count == 1
