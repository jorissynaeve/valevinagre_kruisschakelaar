"""Airco's met een Gree-module: het protocol, de client tegen een nagebootste airco over echte UDP, de hub, zijn ingangen en
uitgangen, en de entiteiten in HA."""
import asyncio
import base64
import json
import os
import stat

import pytest

pytest.importorskip("cryptography")

from kkaddon import gree, schema  # noqa: E402
from kkaddon.greehub import GreeHub, room_temp  # noqa: E402
from kkaddon.model import ModelError  # noqa: E402
from tests.rig import Rig  # noqa: E402

MAC = "c8f742a1b2c3"
DEVKEY = b"Ab12Cd34Ef56Gh78"


# ------------------------------------------------------------ versleuteling


@pytest.mark.parametrize("version", ["1", "2"])
def test_pack_is_read_back(version):
    obj = {"t": "status", "cols": ["Pow"], "mac": MAC}
    pack, tag = gree.encrypt(version, DEVKEY, obj)
    assert (tag is None) == (version == "1")
    assert gree.decrypt(version, DEVKEY, pack, tag) == obj


def test_v1_matches_the_known_scan_answer_format():
    # zoals greeclimate: AES-ECB met de algemene sleutel, PKCS#7, base64
    pack, _ = gree.encrypt("1", gree.GENERIC["1"], {"t": "dev", "mac": MAC})
    raw = base64.b64decode(pack)
    assert len(raw) % 16 == 0
    assert gree.decrypt("1", gree.GENERIC["1"], pack) == {"t": "dev", "mac": MAC}


def test_v2_without_tag_is_still_read_and_a_wrong_tag_is_refused():
    pack, tag = gree.encrypt("2", DEVKEY, {"t": "dat"})
    assert gree.decrypt("2", DEVKEY, pack) == {"t": "dat"}, "GCM zonder tag: CTR vanaf teller 2"
    with pytest.raises(gree.GreeError):
        gree.decrypt("2", b"x" * 16, pack, tag)


@pytest.mark.parametrize("data", [b"", b"nonsense", b'{"t":"pack","pack":"!!"}', b'{"t":"scan"}', json.dumps({"t": "pack", "i": 1, "pack": base64.b64encode(b"x" * 16).decode()}).encode()])
def test_noise_is_not_a_scan_answer(data):
    assert gree.parse_scan(data, "192.0.2.9") is None


@pytest.mark.parametrize("version", ["1", "2"])
def test_scan_answer_is_understood(version):
    pack, tag = gree.encrypt(version, gree.GENERIC[version], {"t": "dev", "cid": MAC.upper(), "name": "1e6ab2c3", "ver": "V3.31", "brand": "gree"})
    msg = {"t": "pack", "i": 1, "uid": 0, "cid": MAC, "tcid": "", "pack": pack, **({"tag": tag} if tag else {})}
    f = gree.parse_scan(json.dumps(msg).encode(), "192.0.2.9")
    assert f == {"mac": MAC, "ip": "192.0.2.9", "name": "1e6ab2c3", "brand": "gree", "model": "", "firmware": "V3.31", "version": version}


def test_values_of_status_and_command_answers():
    assert gree.values_of({"t": "dat", "cols": ["Pow", "SetTem", "hid"], "dat": [1, 22, "x.bin"]}) == {"Pow": 1, "SetTem": 22, "hid": "x.bin"}
    assert gree.values_of({"t": "res", "opt": ["Pow"], "p": [0], "val": [0]}) == {"Pow": 0}
    assert gree.values_of({"t": "res", "opt": ["Pow"], "p": [1]}) == {"Pow": 1}
    assert gree.values_of({"t": "bindok"}) == {}


@pytest.mark.parametrize("raw,want", [(0, None), (61, 21), (62, 22), (21, 21), (40, 40), (None, None), (True, None)])
def test_room_temperature_with_and_without_the_offset(raw, want):
    assert room_temp(raw) == want


def test_mac_is_normalised():
    assert gree.norm_mac("C8:F7:42:A1:B2:C3") == MAC
    assert gree.norm_mac("") == ""


# ------------------------------------------------------------ een nagebootste airco


class FakeGree(asyncio.DatagramProtocol):
    """Een airco op UDP: antwoordt op scan, bind, status en cmd, met v1 of v2."""

    def __init__(self, version="1", key=DEVKEY, mute_bind=False):
        self.version, self.key, self.mute_bind = version, key, mute_bind
        self.props = {"Pow": 0, "Mod": 4, "SetTem": 21, "TemUn": 0, "TemRec": 0, "WdSpd": 0, "TemSen": 60, "Tur": 0, "Quiet": 0, "Lig": 1, "SwUpDn": 0, "SwingLfRig": 0}
        self.commands, self.binds = [], 0

    async def __aenter__(self):
        self.t, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: self, local_addr=("127.0.0.1", 0))
        self.port = self.t.get_extra_info("sockname")[1]
        return self

    async def __aexit__(self, *a):
        self.t.close()

    def _reply(self, addr, obj, generic=False):
        k = gree.GENERIC[self.version] if generic else self.key
        pack, tag = gree.encrypt(self.version, k, obj)
        msg = {"t": "pack", "i": 1 if generic else 0, "uid": 0, "cid": MAC, "tcid": "app", "pack": pack, **({"tag": tag} if tag else {})}
        self.t.sendto(json.dumps(msg).encode(), addr)

    def datagram_received(self, data, addr):
        msg = json.loads(data)
        if msg.get("t") == "scan":
            self._reply(addr, {"t": "dev", "cid": MAC, "mac": MAC, "name": "airco", "ver": "V3.31"}, generic=True)
            return
        if ("tag" in msg) != (self.version == "2"):
            return  # een andere versleuteling dan deze module kent: geen antwoord
        try:
            inner = gree.decrypt(self.version, gree.GENERIC[self.version] if msg["i"] == 1 else self.key, msg["pack"], msg.get("tag"))
        except gree.GreeError:
            return
        if inner["t"] == "bind":
            self.binds += 1
            if not self.mute_bind:
                self._reply(addr, {"t": "bindok", "mac": MAC, "key": self.key.decode(), "r": 200}, generic=True)
        elif inner["t"] == "status":
            cols = [c for c in inner["cols"] if c in self.props]
            self._reply(addr, {"t": "dat", "mac": MAC, "r": 200, "cols": cols, "dat": [self.props[c] for c in cols]})
        elif inner["t"] == "cmd":
            self.commands.append(dict(zip(inner["opt"], inner["p"])))
            self.props.update(dict(zip(inner["opt"], inner["p"])))
            self._reply(addr, {"t": "res", "mac": MAC, "r": 200, "opt": inner["opt"], "p": inner["p"], "val": inner["p"]})


async def _until(cond, what, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(what)


@pytest.mark.parametrize("version", ["1", "2"])
def test_client_finds_the_mac_binds_reads_and_commands(version):
    async def go():
        async with FakeGree(version) as dev:
            seen, bound = {}, []
            c = gree.GreeClient("127.0.0.1", port=dev.port, on_values=seen.update, on_bound=lambda *a: bound.append(a), poll=0.05, answer=0.3, retry=(0.01, 0.02))
            t = asyncio.create_task(c.run())
            await _until(lambda: c.connected, "verbonden")
            assert bound == [(MAC, DEVKEY.decode(), version)], "MAC opgezocht en de sleutel gevraagd, de versie gevonden"
            assert seen["SetTem"] == 21 and seen["Pow"] == 0
            assert c.set({"Pow": 1, "Mod": 1})
            await _until(lambda: seen.get("Pow") == 1, "het antwoord op het bevel")
            assert dev.commands == [{"Pow": 1, "Mod": 1}]
            t.cancel()
    asyncio.run(go())


def test_known_key_skips_binding():
    async def go():
        async with FakeGree("1") as dev:
            c = gree.GreeClient("127.0.0.1", MAC, DEVKEY.decode(), "1", port=dev.port, poll=0.05, answer=0.3)
            t = asyncio.create_task(c.run())
            await _until(lambda: c.connected, "verbonden")
            assert dev.binds == 0
            t.cancel()
    asyncio.run(go())


def test_a_silent_device_gives_a_clear_error_and_no_connection():
    async def go():
        async with FakeGree("1", mute_bind=True) as dev:
            c = gree.GreeClient("127.0.0.1", MAC, port=dev.port, poll=0.05, answer=0.1, retry=(0.05, 0.1))
            t = asyncio.create_task(c.run())
            await _until(lambda: "binden mislukt" in c.last_error, "fout gemeld")
            assert not c.connected and not c.set({"Pow": 1}), "zonder verbinding gaat er geen bevel weg"
            assert dev.binds >= 1, "de vraag kwam aan, maar er kwam geen sleutel terug"
            t.cancel()
    asyncio.run(go())


def test_a_wrong_key_is_not_a_connection():
    async def go():
        async with FakeGree("1") as dev:
            c = gree.GreeClient("127.0.0.1", MAC, "x" * 16, "1", port=dev.port, poll=0.02, answer=0.05)
            t = asyncio.create_task(c.run())
            await _until(lambda: "geen antwoord" in c.last_error, "fout gemeld")
            assert not c.connected
            t.cancel()
    asyncio.run(go())


def test_scan_socket_hears_an_answer():
    async def go():
        async with FakeGree("1") as dev:
            found = []
            s = await gree.scan_socket(found.append)
            gree.scan(s, "127.0.0.1", dev.port)
            await _until(lambda: found, "antwoord gehoord")
            assert found[0]["mac"] == MAC and found[0]["ip"] == "127.0.0.1"
            s.close()
    asyncio.run(go())


# ------------------------------------------------------------ de hub


class FakeClient:
    def __init__(self, ok=True):
        self.connected, self.ok, self.sent = True, ok, []
        self.mac, self.version, self.firmware, self.last_error = MAC, "1", "", ""

    def set(self, vals):
        if self.ok and self.connected:
            self.sent.append(vals)
        return self.ok and self.connected


def hub_with(tmp_path, vals=None, **dev):
    r = Rig(tmp_path)
    hub = GreeHub(tmp_path, r.engine)
    did = hub.add({"name": "Living", **dev})  # zonder IP: er start geen verbinding
    c = hub.clients[did] = FakeClient()
    hub._on_values(did, vals if vals is not None else {"Pow": 1, "Mod": 4, "SetTem": 21, "TemSen": 61, "WdSpd": 0, "Tur": 0, "Lig": 1, "SwUpDn": 1, "SwingLfRig": 0})
    return r, hub, did, c


def test_state_and_outputs(tmp_path):
    r, hub, did, c = hub_with(tmp_path)
    s = hub.state(did)
    assert (s["power"], s["mode"], s["target"], s["room"], s["fan"], s["swing_v"], s["turbo"], s["light"]) == (True, "heat", 21, 21, "auto", "swing", False, True)
    assert s["quiet"] is None, "wat de airco niet meldt, is onbekend"
    assert [hub.output(f"w:{did}:{n}") for n in ("on", "room", "set")] == [True, 21, 21]
    c.connected = False
    assert hub.output(f"w:{did}:room") is None, "offline: onbekend"
    assert hub.output("w:weg:on") is None


@pytest.mark.parametrize("field,value,want", [
    ("power", "OFF", {"Pow": 0}), ("mode", "cool", {"Pow": 1, "Mod": 1}), ("mode", "off", {"Pow": 0}), ("target", "22.0", {"SetTem": 22, "TemUn": 0, "TemRec": 0}),
    ("fan", "high", {"WdSpd": 5}), ("swing_h", "center", {"SwingLfRig": 4}), ("turbo", "ON", {"Tur": 1})])
def test_commands(tmp_path, field, value, want):
    _, hub, did, c = hub_with(tmp_path)
    hub.set(did, field, value)
    assert c.sent == [want]


@pytest.mark.parametrize("field,value", [("target", "35"), ("target", "warm"), ("mode", "boost"), ("power", "misschien"), ("room", "20"), ("Pow", "1")])
def test_bad_commands_are_refused(tmp_path, field, value):
    _, hub, did, c = hub_with(tmp_path)
    with pytest.raises(ModelError):
        hub.set(did, field, value)
    assert c.sent == []


def test_offline_command_is_refused(tmp_path):
    _, hub, did, c = hub_with(tmp_path)
    c.connected = False
    with pytest.raises(ModelError, match="geen verbinding"):
        hub.set(did, "power", "ON")


def test_climate_entity_and_mqtt_commands(tmp_path):
    _, hub, did, c = hub_with(tmp_path)
    cfg = hub.discovery()
    cl = cfg[f"homeassistant/climate/kk_gree_{did}/config"]
    assert cl["modes"] == ["off", "auto", "cool", "dry", "fan_only", "heat"] and cl["min_temp"] == 16 and cl["max_temp"] == 30
    assert f"homeassistant/switch/kk_gree_{did}_turbo/config" in cfg and f"homeassistant/sensor/kk_gree_{did}_room/config" in cfg
    st = hub.states()
    assert st[cl["mode_state_topic"]][0] == "heat" and st[cl["temperature_state_topic"]][0] == "21" and st[cl["current_temperature_topic"]][0] == "21"
    assert st[hub.t(did, "online")][0] == "online"
    assert hub.command(cl["mode_command_topic"], "off") and c.sent == [{"Pow": 0}]
    assert hub.command(cl["temperature_command_topic"], "23.5") and c.sent[-1]["SetTem"] == 24
    assert not hub.command("kruisschakelaars/tuya/x/1/set", "ON"), "niet van ons"
    hub._on_values(did, {"Pow": 0})
    assert hub.states()[cl["mode_state_topic"]][0] == "off"


def test_key_never_leaves_the_add_on_and_the_file_is_private(tmp_path):
    async def go():
        r = Rig(tmp_path)
        hub = GreeHub(tmp_path, r.engine)
        did = hub.add({"name": "Living", "host": "192.0.2.5", "mac": "C8:F7:42:A1:B2:C3", "key": DEVKEY.decode()})
        assert DEVKEY.decode() not in json.dumps(hub.status()) and hub.status()[0]["has_key"]
        hub.close()
        if os.name != "nt":
            assert stat.S_IMODE(os.stat(tmp_path / "gree.json").st_mode) == 0o600
        again = GreeHub(tmp_path, r.engine)
        assert again.devices[did]["key"] == DEVKEY.decode() and again.devices[did]["mac"] == MAC
        hub.update(did, {"key": ""})
        assert hub.devices[did]["key"] == DEVKEY.decode(), "leeg laten houdt de sleutel"
        hub.update(did, {"rebind": True})
        assert hub.devices[did]["key"] == "", "opnieuw binden vraagt een nieuwe sleutel"
        hub.close()
    asyncio.run(go())


@pytest.mark.parametrize("bad,word", [({"mac": "xyz"}, "MAC"), ({"host": "http://x"}, "adres"), ({"key": "kort"}, "16"), ({"name": " "}, "naam"), ({"version": "3"}, "versleuteling")])
def test_bad_input_is_refused(tmp_path, bad, word):
    _, hub, did, _ = hub_with(tmp_path)
    with pytest.raises(ModelError, match=word):
        hub.update(did, bad)


def test_found_airco_fills_in_the_ip(tmp_path):
    async def go():
        r = Rig(tmp_path)
        hub = GreeHub(tmp_path, r.engine)
        did = hub.add({"name": "Living", "mac": MAC})
        hub.on_found({"mac": "aabbccddeeff", "ip": "192.0.2.7", "name": "x", "firmware": "", "version": "1"})
        assert [f["mac"] for f in hub.status()[0]["unclaimed"]] == ["aabbccddeeff"]
        hub.on_found({"mac": MAC, "ip": "192.0.2.8", "name": "", "firmware": "", "version": "1"})
        assert hub.devices[did]["host"] == "192.0.2.8" and did in hub.clients, "IP ingevuld en verbinden gestart"
        assert [f["mac"] for f in hub.status()[0]["unclaimed"]] == ["aabbccddeeff"], "wat al een blok heeft, staat niet meer in de keuzelijst"
        hub.close()
    asyncio.run(go())


# ------------------------------------------------------------ ingangen: alleen bij een wijziging


def test_inputs_act_on_a_change_only(tmp_path):
    r, hub, did, c = hub_with(tmp_path)
    val = {"m:aan": None, "m:t": None, "m:raam": None}
    r.engine.ref_value = lambda ref: val.get(ref)
    hub.devices[did]["inputs"] = {"on": "m:aan", "temp": "m:t", "block": "m:raam"}
    hub.devices[did]["on_mode"] = "heat"
    val.update({"m:aan": False, "m:t": 20, "m:raam": False})
    hub.tick()
    assert c.sent == [], "de eerste stand wordt alleen onthouden: een herstart zet niets aan of uit"
    val["m:aan"] = True
    hub.tick()
    assert c.sent == [{"Pow": 1, "Mod": 4}], "aan, in de gekozen stand"
    hub.tick()
    assert len(c.sent) == 1, "geen wijziging: niets"
    val["m:t"] = 22.4
    hub.tick()
    assert c.sent[-1] == {"SetTem": 22, "TemUn": 0, "TemRec": 0}
    val["m:t"] = 99
    hub.tick()
    assert c.sent[-1]["SetTem"] == 30, "binnen het bereik van de airco"
    val["m:raam"] = True
    hub.tick()
    assert c.sent[-1] == {"Pow": 0}, "blokkeren zet uit"
    n = len(c.sent)
    val["m:aan"] = False
    hub.tick()
    val["m:aan"] = True
    hub.tick()
    assert len(c.sent) == n and "tegengehouden" in hub.status()[0]["last"]["on"], "geblokkeerd: aan doet niets"
    val["m:raam"] = False
    hub.tick()
    assert c.sent[-1] == {"Pow": 1, "Mod": 4}, "deblokkeren: weer aan, want aan is 1"
    val["m:aan"] = None
    hub.tick()
    val["m:aan"] = False
    hub.tick()
    assert c.sent[-1] == {"Pow": 1, "Mod": 4}, "na onbekend is de stand weer een eerste stand"


def test_a_failed_input_is_retried_later_not_every_second(tmp_path, monkeypatch):
    r, hub, did, c = hub_with(tmp_path)
    now = [100.0]
    monkeypatch.setattr("kkaddon.greehub.time.monotonic", lambda: now[0])
    val = {"m:aan": False}
    r.engine.ref_value = lambda ref: val.get(ref)
    hub.devices[did]["inputs"] = {"on": "m:aan"}
    hub.tick()
    c.ok = False
    val["m:aan"] = True
    hub.tick()
    now[0] += 1
    hub.tick()
    assert c.sent == [] and "niet verstuurd" in hub.status()[0]["last"]["on"]
    c.ok = True
    now[0] += 30
    hub.tick()
    assert c.sent == [{"Pow": 1}], "na de wachttijd opnieuw, en de laatste stand houden"


def test_input_sources_are_checked(tmp_path):
    r, hub, did, _ = hub_with(tmp_path)
    mid = r.model.add_module("number", "Doel", 21)
    with pytest.raises(ModelError, match="bestaat"):
        hub.update(did, {"inputs": {"on": "m:weg"}})
    with pytest.raises(ModelError, match="zichzelf"):
        hub.update(did, {"inputs": {"on": f"w:{did}:on"}})
    with pytest.raises(ModelError, match="0 of 1"):
        hub.update(did, {"inputs": {"on": f"w:ander:room"}})
    hub.update(did, {"inputs": {"block": "w:ander:on"}})
    assert hub.devices[did]["inputs"] == {"block": "w:ander:on"}
    hub.update(did, {"inputs": {"block": None}})
    assert hub.devices[did]["inputs"] == {}
    hub.update(did, {"inputs": {"temp": f"m:{mid}"}})
    assert hub.devices[did]["inputs"] == {"temp": f"m:{mid}"}
    with pytest.raises(ModelError, match="0 of 1"):
        hub.update(did, {"inputs": {"on": f"m:{mid}"}})


# ------------------------------------------------------------ uitgangen in de logica en het schema


def test_an_output_feeds_a_comparison(tmp_path):
    r, hub, did, c = hub_with(tmp_path)
    warm = r.model.add_module("above", "Warm", 23)
    r.model.set_module(warm, inputs={"a": f"w:{did}:room"})
    both = r.model.add_module("and", "Aan en warm")
    r.model.set_module(both, inputs={"a": f"w:{did}:on", "b": f"m:{warm}"})
    with pytest.raises(ModelError):
        r.model.set_module(warm, inputs={"a": f"w:{did}:on"})  # een 0/1 is geen getal
    assert r.engine.module_values()[warm] is False
    hub._on_values(did, {"TemSen": 65})
    v = r.engine.module_values()
    assert v[warm] is True and v[both] is True
    assert r.engine.ref_value(f"w:{did}:set") == 21


def test_block_on_the_schema(tmp_path):
    r, hub, did, _ = hub_with(tmp_path)
    b = next(x for x in schema.view(r.model, r.engine.snapshot())["blocks"] if x["key"] == f"w:{did}")
    assert b["kind"] == "airco" and [p["name"] for p in b["ports"]] == ["on", "temp", "block"]
    assert [(o["ref"], o["value"]) for o in b["outs"]] == [(f"w:{did}:on", True), (f"w:{did}:room", 21), (f"w:{did}:set", 21)]
    assert DEVKEY.decode() not in json.dumps(b)
    hub.remove(did)
    assert f"w:{did}" not in schema.existing_blocks(r.model, r.engine.snapshot())


# ------------------------------------------------------------ de web-API


def test_web_api_adds_places_sets_and_removes(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from kkaddon import web
    from kkaddon.settings import Settings
    from tests.rig import KEY

    async def go():
        r = Rig(tmp_path)
        hub = GreeHub(tmp_path, r.engine)
        ctx = web.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), r.engine, airco=hub)
        r.model.set_schema({**r.model.schema, "pages": [{"id": "living", "name": "Living"}]})
        c = TestClient(TestServer(web.make_app(ctx)))
        await c.start_server()
        try:
            res = await (await c.post("/api/airco", json={"name": "Living", "page": "living", "x": 40, "y": 30})).json()
            did = res["id"]
            assert res["key"] == f"w:{did}" and r.model.schema["blocks"][f"w:{did}"]["page"] == "living"
            hub.clients[did] = fc = FakeClient()
            assert (await c.post(f"/api/airco/{did}/set", json={"field": "mode", "value": "cool"})).status == 200 and fc.sent == [{"Pow": 1, "Mod": 1}]
            bad = await c.post(f"/api/airco/{did}/set", json={"field": "target", "value": "40"})
            assert bad.status == 400 and "16" in (await bad.json())["error"]
            mid = r.model.add_module("number", "Doel", 21)
            assert (await c.put(f"/api/airco/{did}", json={"inputs": {"temp": f"m:{mid}"}, "on_mode": "heat"})).status == 200
            assert hub.devices[did]["inputs"] == {"temp": f"m:{mid}"} and hub.devices[did]["on_mode"] == "heat"
            assert f"w:{did}" in schema.existing_blocks(r.model, r.engine.snapshot())
            assert (await c.delete(f"/api/airco/{did}")).status == 200 and did not in hub.devices
        finally:
            await c.close()
            hub.close()
    asyncio.run(go())