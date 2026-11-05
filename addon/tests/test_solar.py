"""Omvormers met een Solarman-stick: het V5-protocol, de client tegen een nagebootste stick over echt TCP, de hub, de uitgangen in de
logica, het blok op het schema, de entiteiten in HA en de web-API."""
import asyncio
import json
import os
import stat
import struct

import pytest
from aiohttp import web as aioweb

from kkaddon import schema, solarman as sm
from kkaddon.model import ModelError
from kkaddon.solarhub import SolarHub
from tests.rig import Rig

SERIAL = 2983112989
# registers zoals de Deye SG04LP3 ze op een zonnige dag meldt (gemeten): zon 2675+2486 W, accu laadt 4279 W, 43 %
REGS = {0x0202: 44, 0x0203: 35, 0x0208: 0, 0x0209: 0, 0x020E: 50, 0x0211: 63, 0x024A: 1250, 0x024B: 5389, 0x024C: 43, 0x024E: 65536 - 4279, 0x024F: 65000,
        0x0271: 65536 - 46, 0x028D: 529, 0x02A0: 2675, 0x02A1: 2486, 0x02A4: 5061, 0x02A5: 52, 0x02A6: 4774, 0x02A7: 52}


# ------------------------------------------------------------ het protocol


def test_crc_matches_the_known_modbus_value():
    assert sm.crc16(bytes.fromhex("010300000001")) == bytes.fromhex("840a")  # het standaardvoorbeeld


def test_a_request_is_a_valid_v5_frame():
    f = sm.frame(SERIAL, 7, sm.modbus_read(0x02A0, 8))
    assert f[0] == 0xA5 and f[-1] == 0x15 and sum(f[1:-2]) & 0xFF == f[-2]
    assert struct.unpack_from("<HHI", f, 3) == (0x4510, 7, SERIAL)
    buf = bytearray(f)
    [(code, seq, serial, body)] = sm.split(buf)
    assert (code, seq, serial) == (0x4510, 7, SERIAL) and not buf and body[15:] == sm.modbus_read(0x02A0, 8)


def answer(seq, regs, serial=SERIAL, high=0x30):
    mb = bytes([1, 3, len(regs) * 2]) + struct.pack(f">{len(regs)}H", *regs)
    body = bytes([2, 1]) + bytes(12) + mb + sm.crc16(mb)
    raw = struct.pack("<BHHHI", 0xA5, len(body), 0x1510, (high << 8) | (seq & 0xFF), serial) + body
    return raw + bytes([sum(raw[1:]) & 0xFF, 0x15])


def test_frames_are_cut_out_of_a_stream_with_noise_and_halves():
    a, b = answer(1, [5]), answer(2, [6, 7])
    buf = bytearray(b"\x00\x01junk" + a + b[:10])
    assert [s for _, s, _, _ in sm.split(buf)] == [0x3001]
    buf.extend(b[10:])
    assert [s for _, s, _, _ in sm.split(buf)] == [0x3002]
    bad = bytearray(answer(3, [1]))
    bad[-2] ^= 0xFF  # de som klopt niet: weggegooid
    assert sm.split(bad) == []


def test_modbus_answer_is_checked():
    assert sm.modbus_data(answer(1, [1, 2, 3])[11:-2]) == [1, 2, 3]
    broken = bytearray(answer(1, [1, 2, 3])[11:-2])
    broken[-1] ^= 1
    with pytest.raises(sm.SolarmanError, match="CRC"):
        sm.modbus_data(bytes(broken))
    mb = bytes([1, 0x83, 2])
    exc = bytes([2, 1]) + bytes(12) + mb + sm.crc16(mb)
    with pytest.raises(sm.SolarmanError, match="weigert"):
        sm.modbus_data(exc)
    with pytest.raises(sm.SolarmanError):
        sm.modbus_data(b"")


def test_registers_are_decoded_with_signs_and_units():
    v = sm.decode(REGS)
    assert (v["pv"], v["pv1"], v["pv2"], v["load"]) == (5161, 2675, 2486, 529)
    assert v["grid"] == -46, "negatief: teruglevering"
    assert v["batt"] == 4279, "de omvormer meldt laden negatief; wij draaien dat om"
    assert (v["soc"], v["batt_v"], v["batt_t"], v["today"]) == (43, 53.89, 25.0, 6.3)
    assert (v["pv1_v"], v["pv2_a"]) == (506.1, 5.2)


def test_every_value_is_decoded_with_scale_sign_and_32_bits():
    regs = {**REGS, 0x0216: 38673, 0x0217: 2, 0x0204: 45041, 0x0205: 0, 0x024F: 65536 - 7778, 0x0268: 4, 0x025E: 65532, 0x0256: 2379, 0x027E: 5004, 0x021C: 1250,
            0x029B: 7, 0x029F: 1}
    v = sm.decode(regs)
    assert v["total"] == 16974.5, "twee registers, het eerste is het lage"
    assert v["batt_in_tot"] == 4504.1 and v["batt_a"] == 77.78, "laden is positief"
    assert (v["grid_l1"], v["ict_l3"], v["vgrid_l1"], v["freq"], v["dc_t"]) == (4, -4, 237.9, 50.04, 25.0)
    assert v["gen"] == 65543
    assert v["inv_l1"] is None, "niet gemeld: onbekend"
    assert len(sm.FIELDS) == len({f[0] for f in sm.FIELDS}) == 54


def test_every_field_fits_a_block_and_a_ref():
    import re
    inside = lambda a: any(s <= a < s + n for s, n in sm.BLOCKS)
    for key, _g, _n, _u, addrs, *_ in sm.FIELDS:
        assert all(inside(a) for a in addrs), f"{key} wordt niet gelezen"
        assert re.fullmatch(r"[a-z0-9_]{1,12}", key), f"{key} past niet in een bron o:<id>:<naam>"


def test_missing_registers_are_unknown_not_zero():
    v = sm.decode({0x02A0: 100})
    assert v["pv"] == 100 and v["pv2"] is None and v["load"] is None and v["batt"] is None and v["soc"] is None


# ------------------------------------------------------------ de client tegen een nagebootste stick


class FakeStick:
    """Antwoordt als een stick: eerst een hartslag, dan het antwoord met de lage byte van het volgnummer gelijk aan de vraag."""

    def __init__(self, regs=REGS, serial=SERIAL, silent=False, exception=False):
        self.regs, self.serial, self.silent, self.exception, self.asked, self.connections = regs, serial, silent, exception, [], 0

    async def handle(self, r, w):
        self.connections += 1
        buf = bytearray()
        try:
            while data := await r.read(1024):
                buf.extend(data)
                for code, seq, _, body in sm.split(buf):
                    start, count = struct.unpack(">HH", body[15 + 2:15 + 6])
                    self.asked.append((start, count))
                    if self.silent:
                        continue
                    heartbeat = struct.pack("<BHHHI", 0xA5, 1, 0x4710, 9, self.serial) + b"\x00"
                    w.write(heartbeat + bytes([sum(heartbeat[1:]) & 0xFF, 0x15]))
                    if self.exception:
                        mb = bytes([1, 0x83, 2])
                        body = bytes([2, 1]) + bytes(12) + mb + sm.crc16(mb)
                        raw = struct.pack("<BHHHI", 0xA5, len(body), 0x1510, 0x3000 | (seq & 0xFF), self.serial) + body
                        w.write(raw + bytes([sum(raw[1:]) & 0xFF, 0x15]))
                    else:
                        w.write(answer(seq, [self.regs.get(start + i, 0) for i in range(count)], self.serial))
                    await w.drain()
        except ConnectionError:
            pass
        finally:
            w.close()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


def run_client(stick, **kw):
    async def go():
        port = await stick.start()
        got = {}
        links = []
        c = sm.SolarmanClient("127.0.0.1", kw.pop("serial", SERIAL), port=port, on_values=got.update, on_link=links.append)
        ok = await c.poll_once()
        stick.server.close()
        return c, ok, got, links
    return asyncio.run(go())


def test_client_reads_every_block_and_reports_values():
    stick = FakeStick()
    c, ok, got, links = run_client(stick)
    assert ok and c.connected and links == [True]
    assert stick.asked == list(sm.BLOCKS) and stick.connections == 1
    assert got["pv"] == 5161 and got["batt"] == 4279 and got["grid"] == -46


def test_a_silent_stick_is_not_a_connection_until_it_stays_silent(monkeypatch):
    monkeypatch.setattr(sm, "TIMEOUT", 0.3)
    c, ok, got, links = run_client(FakeStick(silent=True))
    assert not ok and not c.connected and "geen antwoord" in c.last_error and not got and links == []


def test_a_stick_with_another_serial_is_refused(monkeypatch):
    monkeypatch.setattr(sm, "TIMEOUT", 0.3)
    c, ok, _, _ = run_client(FakeStick(serial=1234567890))
    assert not ok and "dit is stick 1234567890" in c.last_error, "de waarden van een andere stick nemen we niet over"


def test_a_modbus_exception_is_an_error_not_values(monkeypatch):
    c, ok, got, _ = run_client(FakeStick(exception=True))
    assert not ok and "weigert" in c.last_error and not got


def test_unreachable_stick_gives_a_clear_error(monkeypatch):
    monkeypatch.setattr(sm, "TIMEOUT", 0.5)

    async def go():
        c = sm.SolarmanClient("127.0.0.1", SERIAL, port=1)  # niemand luistert
        return c, await c.poll_once()
    c, ok = asyncio.run(go())
    assert not ok and "geen verbinding" in c.last_error


def test_goes_offline_after_three_misses_and_back_online():
    links = []
    c = sm.SolarmanClient("x", SERIAL, on_link=links.append)
    c.connected = True
    results = iter([sm.SolarmanError("stil")] * 3)

    async def fail():
        raise next(results)
    c.read_all = fail

    async def go():
        for _ in range(2):
            await c.poll_once()
        assert c.connected, "twee keer stil is nog geen storing"
        await c.poll_once()
    asyncio.run(go())
    assert not c.connected and links == [False]


# ------------------------------------------------------------ de hub


def hub_with(tmp_path, **dev):
    r = Rig(tmp_path)
    hub = SolarHub(tmp_path, r.engine)
    did = hub.add({"name": "Deye", "host": "192.168.8.34", "serial": str(SERIAL), **dev})  # buiten een lus: er start geen client
    return r, hub, did


def test_outputs_are_the_values_and_unknown_when_offline_or_wrong(tmp_path):
    async def go():
        r, hub, did = hub_with(tmp_path)
        hub.clients[did].connected = True
        hub._on_values(did, sm.decode(REGS))
        try:
            assert did == "deye" and hub.devices[did]["serial"] == SERIAL
            assert [hub.output(f"o:{did}:{n}") for n in ("pv", "load", "grid", "batt", "soc", "today")] == [5161, 529, -46, 4279, 43, 6.3]
            hub.clients[did].connected = False
            assert hub.output(f"o:{did}:pv") is None, "offline: onbekend"
            assert hub.output("o:weg:pv") is None and hub.output(f"o:{did}:onzin") is None and hub.output("w:x:on") is None
        finally:
            hub.close()
    asyncio.run(go())


def test_bad_input_is_refused(tmp_path):
    async def go():
        r = Rig(tmp_path)
        hub = SolarHub(tmp_path, r.engine)
        try:
            for b, word in [({"name": "", "host": "1.2.3.4", "serial": "2983112989"}, "naam"), ({"name": "x", "host": "", "serial": "2983112989"}, "IP"),
                            ({"name": "x", "host": "http://1.2.3.4", "serial": "2983112989"}, "IP"), ({"name": "x", "host": "1.2.3.4", "serial": "abc"}, "serienummer"),
                            ({"name": "x", "host": "1.2.3.4", "serial": "99999999999"}, "serienummer"), ({"name": "x", "host": "1.2.3.4"}, "serienummer")]:
                with pytest.raises(ModelError, match=word):
                    hub.add(b)
            hub.add({"name": "a", "host": "1.2.3.4", "serial": "2983112989"})
            with pytest.raises(ModelError, match="staat er al"):
                hub.add({"name": "b", "host": "1.2.3.5", "serial": "2983112989"})
        finally:
            hub.close()
    asyncio.run(go())


def test_file_is_private_survives_a_restart_and_a_second_name_gets_a_suffix(tmp_path):
    async def go():
        r = Rig(tmp_path)
        hub = SolarHub(tmp_path, r.engine)
        a = hub.add({"name": "Deye", "host": "1.2.3.4", "serial": "2983112989"})
        b = hub.add({"name": "Deye", "host": "1.2.3.5", "serial": "2983112990"})
        hub.close()
        assert (a, b) == ("deye", "deye_2")
        if os.name != "nt":
            assert stat.S_IMODE(os.stat(tmp_path / "solar.json").st_mode) == 0o600
        again = SolarHub(tmp_path, Rig(tmp_path).engine)
        assert {k: v["serial"] for k, v in again.devices.items()} == {"deye": 2983112989, "deye_2": 2983112990}
    asyncio.run(go())


def test_update_reconnects_only_for_a_new_address(tmp_path):
    async def go():
        r, hub, did = hub_with(tmp_path)
        try:
            first = hub.clients[did]
            hub.update(did, {"name": "Dak"})
            assert hub.clients[did] is first and hub.devices[did]["name"] == "Dak"
            hub.update(did, {"host": "192.168.8.35"})
            assert hub.clients[did] is not first and hub.clients[did].host == "192.168.8.35"
            with pytest.raises(ModelError):
                hub.update("weg", {})
        finally:
            hub.close()
    asyncio.run(go())


# ------------------------------------------------------------ de logica en het schema


def test_an_output_feeds_a_comparison_and_a_sprinkler_style_chain(tmp_path):
    async def go():
        r, hub, did = hub_with(tmp_path)
        hub.clients[did].connected = True
        hub._on_values(did, sm.decode(REGS))
        try:
            sun = r.model.add_module("above", "Veel zon", 4000)
            r.model.set_module(sun, inputs={"a": f"o:{did}:pv"})
            full = r.model.add_module("above", "Accu vol", 90)
            r.model.set_module(full, inputs={"a": f"o:{did}:soc"})
            both = r.model.add_module("and", "Zon en vol")
            r.model.set_module(both, inputs={"a": f"m:{sun}", "b": f"m:{full}"})
            v = r.engine.module_values()
            assert v[sun] is True and v[full] is False and v[both] is False
            hub._on_values(did, {"soc": 95})
            assert r.engine.module_values()[both] is True
            assert r.engine.ref_value(f"o:{did}:batt") == 4279
            hub.clients[did].connected = False
            assert r.engine.module_values()[sun] is None, "offline: onbekend, nooit 0"
            with pytest.raises(ModelError):
                r.model.set_module(both, inputs={"a": f"o:{did}:pv"})  # een getal is geen 0/1
        finally:
            hub.close()
    asyncio.run(go())


def test_block_on_the_schema(tmp_path):
    async def go():
        r, hub, did = hub_with(tmp_path)
        hub.clients[did].connected = True
        hub._on_values(did, sm.decode(REGS))
        try:
            b = next(x for x in schema.view(r.model, r.engine.snapshot())["blocks"] if x["key"] == f"o:{did}")
            assert b["kind"] == "solar" and b["ports"] == [] and b["rows"] == 6 and b["online"] is True
            assert [o["name"] for o in b["outs"]] == ["pv", "today", "batt", "soc", "grid", "load"], "standaard zes rijen, in de volgorde van de tabel"
            assert len(b["all"]) == 54 and {o["group"] for o in b["all"]} == set(sm.GROUPS)
            hub.update(did, {"shown": ["soc", "pv1_v", "pv"]})
            b = next(x for x in schema.view(r.model, r.engine.snapshot())["blocks"] if x["key"] == f"o:{did}")
            assert [o["name"] for o in b["outs"]] == ["pv", "pv1_v", "soc"] and b["rows"] == 3 and len(b["all"]) == 54, "in de volgorde van de tabel; alles blijft kiesbaar"
            with pytest.raises(ModelError):
                hub.update(did, {"shown": ["onzin"]})
            hub.update(did, {"shown": []})
            assert next(x for x in schema.view(r.model, r.engine.snapshot())["blocks"] if x["key"] == f"o:{did}")["rows"] == 1
            hub.update(did, {"shown": ["pv", "load", "grid", "batt", "soc", "today"]})
            b = next(x for x in schema.view(r.model, r.engine.snapshot())["blocks"] if x["key"] == f"o:{did}")
            assert [(o["name"], o["ref"], o["value"], o["unit"]) for o in b["outs"] if o["name"] in ("pv", "load")] == [("pv", f"o:{did}:pv", 5161, "W"), ("load", f"o:{did}:load", 529, "W")]
            assert schema.view(r.model, r.engine.snapshot())["blocks"], "past in het schema"
            hub.remove(did)
            assert f"o:{did}" not in schema.existing_blocks(r.model, r.engine.snapshot())
        finally:
            hub.close()
    asyncio.run(go())


# ------------------------------------------------------------ HA


def test_ha_sensors_have_units_and_classes_and_go_unavailable_offline(tmp_path):
    async def go():
        r, hub, did = hub_with(tmp_path)
        hub.clients[did].connected = True
        hub._on_values(did, sm.decode(REGS))
        try:
            disc = hub.discovery()
            pv = disc[f"homeassistant/sensor/kk_solar_{did}_pv/config"]
            assert (pv["unit_of_measurement"], pv["device_class"], pv["state_class"]) == ("W", "power", "measurement")
            assert disc[f"homeassistant/sensor/kk_solar_{did}_today/config"]["state_class"] == "total_increasing"
            assert pv["device"]["identifiers"] == [f"kk_solar_{did}"]
            st = hub.states()
            assert st[hub.t(did, "online")] == ("online", True) and st[hub.t(did, "pv")] == ("5161", True) and st[hub.t(did, "batt_v")] == ("53.89", True)
            hub.clients[did].connected = False
            assert hub.states()[hub.t(did, "online")] == ("offline", True)
            assert hub.filters == [] and hub.command("kruisschakelaars/solar/x/pv/set", "1") is False
        finally:
            hub.close()
    asyncio.run(go())


# ------------------------------------------------------------ de web-API en het serienummer opzoeken


STATUS = 'var webdata_sn = "2408030142";\nvar cover_mid = "2983112989";\nvar cover_ver = "LSW3_32U_5406_1.06";\n'


async def fake_status_page(auth_ok=True):
    async def status(req):
        if req.headers.get("Authorization") != "Basic YWRtaW46YWRtaW4=":  # admin:admin
            return aioweb.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="x"'})
        return aioweb.Response(text=STATUS)
    app = aioweb.Application()
    app.router.add_get("/status.html", status)
    runner = aioweb.AppRunner(app)
    await runner.setup()
    site = aioweb.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


def test_serial_is_looked_up_on_the_status_page_with_the_default_login(tmp_path):
    async def go():
        runner, port = await fake_status_page()
        hub = SolarHub(tmp_path, Rig(tmp_path).engine)
        try:
            assert await hub.lookup("127.0.0.1", port=port) == "2983112989"
            with pytest.raises(ModelError, match="weigert"):
                await hub.lookup("127.0.0.1", "admin", "fout", port=port)
            with pytest.raises(ModelError, match="IP"):
                await hub.lookup("http://x/")
        finally:
            await runner.cleanup()
    asyncio.run(go())


def test_web_api_adds_places_updates_and_removes(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from kkaddon import web
    from kkaddon.settings import Settings
    from tests.rig import KEY

    async def go():
        r = Rig(tmp_path)
        hub = SolarHub(tmp_path, r.engine)
        ctx = web.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), r.engine, solar=hub)
        r.model.set_schema({**r.model.schema, "pages": [{"id": "dak", "name": "Dak"}]})
        c = TestClient(TestServer(web.make_app(ctx)))
        await c.start_server()
        try:
            res = await (await c.post("/api/solar", json={"name": "Deye", "host": "127.0.0.1", "serial": "2983112989", "page": "dak", "x": 40, "y": 30})).json()
            did = res["id"]
            assert res["key"] == f"o:{did}" and r.model.schema["blocks"][f"o:{did}"]["page"] == "dak"
            assert f"o:{did}" in schema.existing_blocks(r.model, r.engine.snapshot())
            assert (await c.put(f"/api/solar/{did}", json={"name": "Dak"})).status == 200 and hub.devices[did]["name"] == "Dak"
            bad = await c.put(f"/api/solar/{did}", json={"serial": "abc"})
            assert bad.status == 400 and "serienummer" in (await bad.json())["error"]
            assert json.dumps(r.engine.snapshot()["solar"])  # alleen gewone waarden
            assert (await c.delete(f"/api/solar/{did}")).status == 200 and did not in hub.devices
            assert f"o:{did}" not in r.model.schema["blocks"]
            assert (await c.delete(f"/api/solar/{did}")).status == 404
        finally:
            await c.close()
            hub.close()
    asyncio.run(go())
