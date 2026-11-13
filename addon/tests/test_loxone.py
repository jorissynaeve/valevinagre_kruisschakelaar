"""De brug naar een Loxone Miniserver (loxone.py en loxonehub.py): het berichtformaat, de importbestanden voor Loxone Config (vergeleken met echte exports van de gebruiker
in tests/data/loxone/), en het verkeer: naar een nagebootste Miniserver (een UDP-socket) en terug over UDP en HTTP, met een nagebootst HUISDEV-apparaat als bron en doel."""
import asyncio
import json
import os
import socket
import xml.etree.ElementTree as ET
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from kkaddon import loxone as lx
from kkaddon import web
from kkaddon.espdevhub import check_value
from kkaddon.loxonehub import LoxoneHub
from kkaddon.model import ModelError
from kkaddon.settings import Settings
from tests.rig import KEY, Rig

DATA = Path(__file__).parent / "data" / "loxone"


def run(coro):
    return asyncio.run(coro)


async def eventually(cond, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    raise AssertionError("duurde te lang")


# ------------------------------------------------------------ het bericht


def test_a_message_is_one_signal_with_a_prefix_and_a_number():
    assert lx.message("tuin_motion", True) == b"kk.tuin_motion=1"
    assert lx.message("tuin_motion", False) == b"kk.tuin_motion=0"
    assert lx.message("vermogen", 3450.4) == b"kk.vermogen=3450"
    assert lx.message("temp", 21.456, 1) == b"kk.temp=21.5"
    assert lx.message("stand", 2) == b"kk.stand=2"
    assert lx.message("temp", -3.0, 1) == b"kk.temp=-3.0"


def test_parse_takes_every_valid_line_and_nothing_else():
    assert lx.parse(b"kk.a=1") == [("a", 1.0)]
    assert lx.parse("kk.a=1\nkk.b=-2.5\r\nkk.c=3;kk.d=4") == [("a", 1.0), ("b", -2.5), ("c", 3.0), ("d", 4.0)]
    assert lx.parse(b"kk.a=1 \n") == [("a", 1.0)]
    for bad in (b"a=1", b"kk.A=1", b"kk.a=", b"kk.a=x", b"kk.a=1e5", b"kk.a=1.2.3", b"kk.=1", b"RELAY-SET-255,1,1", b"", b"\xff\xfe", b"kk.a = 1"):
        assert lx.parse(bad) == [], bad
    assert lx.parse(b"kk.a=1\n" + b"x" * 600) == [], "te groot om van Loxone te zijn"
    assert lx.parse(b"kk." + b"a" * 41 + b"=1") == [], "een naam is hoogstens 40 tekens"


def test_a_check_for_one_name_cannot_match_another():
    """De herkenning in Config is `kk.<naam>=\\v`: de prefix vooraan en het = erachter, dus een naam die in een andere zit, matcht niet."""
    for a, b in (("tuin", "tuinhuis"), ("motion", "tgr_motion"), ("a", "aa")):
        assert f"kk.{a}=" not in lx.message(b, 1).decode()


# ------------------------------------------------------------ de importbestanden, tegen echte exports


def _tree(text: str):
    return ET.fromstring(text)


def _attrs(path: str, tag: str) -> list[set]:
    return [set(e.attrib) for e in ET.parse(DATA / path).getroot().iter(tag)]


SIG_OUT = [{"id": "tuin_motion", "label": "Tuin beweging", "kind": "bool", "decimals": 0, "min": 0, "max": 1},
           {"id": "smappee_power", "label": "Laadpaal vermogen", "kind": "float", "decimals": 1, "min": None, "max": None},
           {"id": "temp", "label": "", "kind": "float", "decimals": 2, "min": -40, "max": 85}]
SIG_IN = [{"id": "brink_level_set", "label": "Ventilatie", "kind": "int", "decimals": 0, "min": 0, "max": 100},
          {"id": "tuin_on_set", "label": "Lamp", "kind": "bool", "decimals": 0, "min": 0, "max": 1}]


def test_the_udp_input_has_the_shape_of_an_export_from_loxone_config():
    root = _tree(lx.viu_xml("Huisconfig", "192.168.1.5", 9100, SIG_OUT))
    ref = ET.parse(DATA / "VIU_MoestuinControl.xml").getroot()
    assert root.tag == ref.tag == "VirtualInUdp"
    assert set(root.attrib) == set(ref.attrib)
    assert root.attrib["Address"] == "192.168.1.5" and root.attrib["Port"] == "9100"
    assert root.find("Info").attrib == {"templateType": "1", "minVersion": ref.find("Info").attrib["minVersion"]}
    cmds = root.findall("VirtualInUdpCmd")
    assert len(cmds) == 3
    for c in cmds:
        assert set(c.attrib) == _attrs("VIU_MoestuinControl.xml", "VirtualInUdpCmd")[0]
    by = {c.attrib["Title"]: c.attrib for c in cmds}
    assert by["kk.smappee_power"]["Check"] == "kk.smappee_power=\\v" and by["kk.smappee_power"]["Unit"] == "<v.1>"
    assert by["kk.tuin_motion"]["Unit"] == "<v>" and by["kk.tuin_motion"]["Analog"] == "true", "ook een aan/uit komt als 0/1 binnen: zo blijft de stand staan"
    assert by["kk.temp"]["Signed"] == "true" and by["kk.smappee_power"]["Signed"] == "true" and by["kk.tuin_motion"]["Signed"] == "false"
    assert float(by["kk.smappee_power"]["MaxVal"]) >= 1_000_000, "een teller of een vermogen past erin"


def test_the_output_over_udp_has_the_shape_of_the_users_export():
    root = _tree(lx.vo_xml("Huisconfig", "192.168.1.5", 47510, "udp", SIG_IN))
    ref = ET.parse(DATA / "VO_Moestuincontrol.xml").getroot()
    assert root.attrib["Address"] == "/dev/udp/192.168.1.5/47510" and set(root.attrib) == set(ref.attrib)
    assert root.find("Info").attrib["templateType"] == "3"
    digital = next(c for c in root.findall("VirtualOutCmd") if c.attrib["Title"] == "kk.tuin_on_set")
    analog = next(c for c in root.findall("VirtualOutCmd") if c.attrib["Title"] == "kk.brink_level_set")
    assert set(digital.attrib) == _attrs("VO_Moestuincontrol.xml", "VirtualOutCmd")[0], "een aan/uit heeft precies de velden van de export"
    assert set(analog.attrib) == _attrs("VO_Kidswekker.xml", "VirtualOutCmd")[0], "een analoog commando heeft precies de velden van de export"
    assert (digital.attrib["CmdOn"], digital.attrib["CmdOff"], digital.attrib["Analog"]) == ("kk.tuin_on_set=1", "kk.tuin_on_set=0", "false")
    assert (analog.attrib["CmdOn"], analog.attrib["CmdOff"], analog.attrib["Analog"]) == ("kk.brink_level_set=<v>", "", "true")


def test_the_output_over_http_is_a_get_with_the_value_in_the_address():
    root = _tree(lx.vo_xml("Huisconfig", "192.168.1.5", 47510, "http", SIG_IN))
    assert root.attrib["Address"] == "http://192.168.1.5:47510"
    cmds = {c.attrib["Title"]: c.attrib for c in root.findall("VirtualOutCmd")}
    assert cmds["kk.brink_level_set"]["CmdOn"] == "loxone?kk.brink_level_set=<v>" and cmds["kk.brink_level_set"]["CmdOnMethod"] == "GET"
    assert (cmds["kk.tuin_on_set"]["CmdOn"], cmds["kk.tuin_on_set"]["CmdOff"]) == ("loxone?kk.tuin_on_set=1", "loxone?kk.tuin_on_set=0")


def test_xml_is_escaped_so_a_label_cannot_break_the_file():
    sig = [{"id": "x", "label": 'a "b" <c> & d', "kind": "int", "decimals": 0, "min": None, "max": None}]
    assert _tree(lx.viu_xml("T", "1.2.3.4", 9, sig)).find("VirtualInUdpCmd").attrib["Comment"] == 'a "b" <c> & d'
    assert _tree(lx.vo_xml("T", "h", 9, "udp", sig)).find("VirtualOutCmd").attrib["Comment"] == 'a "b" <c> & d'


# ------------------------------------------------------------ een nagebootst HUISDEV-apparaat


def pt(pid, kind="int", writable=False, **kw):
    d = {"id": pid, "label": pid.capitalize(), "kind": kind, "unit": "", "decimals": 1 if kind == "float" else 0, "min": None, "max": None, "step": None, "options": [],
         "writable": writable, "danger": False, "diag": False, "group": "", "cls": "", "state": "", "stale": 0, "confirm": ""}
    d.update(kw)
    return d


class StubEsp:
    """Net genoeg als EspDevHub voor de brug: apparaten met punten, een stand, bereikbaar of niet, en `set`."""

    def __init__(self):
        self.devices = {"tuin": {"name": "Tuin", "shown": None, "points": [
            pt("motion", "bool"), pt("lux", "int"), pt("power", "float", min=0, max=11000),
            pt("bri", "int", writable=True, min=0, max=100, step=5), pt("on", "bool", writable=True),
            pt("scene", "enum", writable=True, options=[{"v": 0, "l": "uit"}, {"v": 1, "l": "helder"}]),
            pt("unlock", "bool", writable=True, danger=True), pt("name", "text"), pt("rssi", "int", diag=True)]}}
        self.vals = {"motion": False, "lux": 12, "power": 3450.4, "bri": 50, "on": False, "scene": 0, "unlock": False}
        self.online = {"tuin"}
        self.sets = []

    def shown(self, dev):
        return [p["id"] for p in dev["points"] if p["kind"] != "text" and not p["diag"]][:8]

    def is_online(self, did):
        return did in self.online

    def output(self, ref):
        _, did, pid = ref.split(":")
        return self.vals.get(pid) if did in self.online else None

    async def set(self, did, pid, value, confirm=False):
        p = next(x for x in self.devices[did]["points"] if x["id"] == pid)
        value = check_value(p, value)  # zoals EspDevHub.set: bereik, stap en keuzes
        self.sets.append((did, pid, value))
        self.vals[pid] = value
        return {}


class Mini:
    """Een nagebootste Miniserver: een UDP-socket op 127.0.0.1 die opvangt wat de add-on stuurt."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]

    def got(self) -> list[str]:
        out = []
        while True:
            try:
                out.append(self.sock.recvfrom(1024)[0].decode())
            except BlockingIOError:
                return out

    def send(self, data: bytes, port: int) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        s.sendto(data, ("127.0.0.1", port))
        s.close()

    def close(self):
        self.sock.close()


def make(tmp_path, started=True, **kw):
    r = Rig(tmp_path)
    esp = StubEsp()
    r.engine.espdev_value = esp.output
    hub = LoxoneHub(tmp_path, r.engine, esp, port=0, gap=0.001, start_delay=0, tick=0.02, **kw)
    return r, esp, hub


async def up(hub, mini):
    hub.miniserver, hub.out_port = "127.0.0.1", mini.port
    await hub.start()


# ------------------------------------------------------------ signalen maken


def test_a_device_becomes_signals_in_both_directions(tmp_path):
    _, esp, hub = make(tmp_path)
    made = hub.add_device("tuin", "tuin")
    expected = {"tuin_motion", "tuin_lux", "tuin_power", "tuin_bri", "tuin_on", "tuin_scene", "tuin_unlock", "tuin_bri_set", "tuin_on_set", "tuin_scene_set", "tuin_ok"}
    assert set(made) == expected, "wat op het blok staat gaat naar Loxone; instelbare punten kan Loxone ook zetten; een gevaarlijk punt (unlock) alleen lezen; tekst en diagnose niet"
    s = hub.signals
    assert s["tuin_bri_set"]["dir"] == "in" and s["tuin_bri"]["dir"] == "out" and s["tuin_ok"]["ref"] == "online:tuin"
    assert s["tuin_power"]["kind"] == "float" and s["tuin_power"]["decimals"] == 1 and s["tuin_motion"]["kind"] == "bool" and s["tuin_scene"]["kind"] == "int"
    assert "1=helder" in s["tuin_scene"]["label"], "een keuze noemt zijn nummers, voor wie het in Loxone moet vertalen"
    assert hub.add_device("tuin", "tuin") == [], "twee keer toevoegen overschrijft niets"


def test_you_can_choose_the_points_and_the_direction(tmp_path):
    _, esp, hub = make(tmp_path)
    made = hub.add_device("tuin", "t", [{"pid": "bri", "out": False, "in": True}, {"pid": "motion", "out": True, "in": False}])
    assert set(made) == {"t_bri_set", "t_motion", "t_ok"}
    with pytest.raises(ModelError, match="niet zetten"):
        hub.add_device("tuin", "u", [{"pid": "motion", "in": True}])
    with pytest.raises(ModelError, match="onbekend punt"):
        hub.add_device("tuin", "v", [{"pid": "nope", "out": True}])
    with pytest.raises(ModelError, match="voorvoegsel"):
        hub.add_device("tuin", "Tuin Huis", None)
    with pytest.raises(ModelError, match="onbekend apparaat"):
        hub.add_device("weg", "w")


def test_loxone_never_gets_to_set_a_danger_point_or_something_read_only(tmp_path):
    _, esp, hub = make(tmp_path)
    with pytest.raises(ModelError, match="gevaarlijk"):
        hub.add_signal("deur", {"dir": "in", "ref": "e:tuin:unlock"})
    with pytest.raises(ModelError, match="niet instelbaar"):
        hub.add_signal("beweging", {"dir": "in", "ref": "e:tuin:motion"})
    with pytest.raises(ModelError, match="onbekend punt"):
        hub.add_signal("x", {"dir": "in", "ref": "e:tuin:nope"})
    with pytest.raises(ModelError, match="alleen een punt"):
        hub.add_signal("t", {"dir": "in", "ref": "t:abc"})


def test_names_and_sources_are_checked(tmp_path):
    _, esp, hub = make(tmp_path)
    for bad in ("Tuin", "a b", "", "x" * 41, "sync", "é"):
        with pytest.raises(ModelError):
            hub.add_signal(bad, {"dir": "out", "ref": "e:tuin:lux"})
    with pytest.raises(ModelError, match="richting"):
        hub.add_signal("a", {"dir": "sideways", "ref": "e:tuin:lux"})
    with pytest.raises(ModelError, match="onbekende bron"):
        hub.add_signal("a", {"dir": "out", "ref": "rm -rf"})
    hub.add_signal("a", {"dir": "out", "ref": "t:ochtend", "kind": "bool"})
    with pytest.raises(ModelError, match="bestaat al"):
        hub.add_signal("a", {"dir": "out", "ref": "e:tuin:lux"})


def test_settings_are_checked_and_survive_a_restart(tmp_path):
    r, esp, hub = make(tmp_path)
    for bad in ({"miniserver": "pi.local"}, {"miniserver": "300.1.1.1"}, {"out_port": 0}, {"out_port": 70000}, {"transport": "tcp"}, {"addon_host": "a b"}):
        with pytest.raises(ModelError):
            hub.configure(bad)
    hub.configure({"miniserver": "192.168.1.112", "out_port": 9100, "addon_host": "192.168.1.5", "transport": "udp"})
    hub.add_device("tuin", "tuin")
    hub.update_signal("tuin_lux", {"deadband": 5, "heartbeat": 30})
    if os.name != "nt":
        assert oct((tmp_path / "loxone.json").stat().st_mode & 0o777) == "0o600", "het adres van de Miniserver hoort niet in een export"
    again = LoxoneHub(tmp_path, r.engine, esp, port=0)
    assert (again.miniserver, again.out_port, again.addon_host, again.transport) == ("192.168.1.112", 9100, "192.168.1.5", "udp")
    assert again.signals == hub.signals and again.signals["tuin_lux"]["deadband"] == 5 and again.signals["tuin_lux"]["heartbeat"] == 30
    assert hub.remove_signal("tuin_lux") and not hub.remove_signal("tuin_lux")


# ------------------------------------------------------------ naar Loxone


def test_values_go_out_on_change_by_heartbeat_and_never_when_unknown(tmp_path):
    t = [1000.0]
    r, esp, hub = make(tmp_path, clock=lambda: t[0])
    hub.miniserver = "192.168.1.112"
    hub._t0 = t[0] - 100
    hub.add_signal("lux", {"dir": "out", "ref": "e:tuin:lux", "heartbeat": 60})
    hub.add_signal("pwr", {"dir": "out", "ref": "e:tuin:power", "deadband": 100})
    hub.add_signal("ok", {"dir": "out", "ref": "online:tuin"})

    def flush():
        hub.tick()
        out = list(hub.queue.values())
        hub.queue.clear()
        return sorted(x.decode() for x in out)

    assert flush() == ["kk.lux=12", "kk.ok=1", "kk.pwr=3450.4"], "de eerste stand gaat altijd"
    assert flush() == [], "ongewijzigd: niets"
    t[0] += 2
    esp.vals["lux"] = 13
    esp.vals["power"] = 3500
    assert flush() == ["kk.lux=13"], "een verandering binnen de dode band (100 W) gaat niet"
    t[0] += 0.4
    esp.vals["lux"] = 14
    assert flush() == [], "hoogstens één bericht per seconde per signaal"
    t[0] += 1
    assert flush() == ["kk.lux=14"]
    esp.vals["power"] = 3600
    t[0] += 2
    assert flush() == ["kk.pwr=3600.0"]
    t[0] += 61
    assert flush() == ["kk.lux=14", "kk.ok=1", "kk.pwr=3600.0"], "elke minuut de laatste stand opnieuw: zo herstelt een herstart van de Miniserver zich"
    esp.online.clear()
    t[0] += 61
    assert flush() == ["kk.ok=0"], "een apparaat dat offline is geeft een onbekende waarde en die wordt niet verzonnen; wel meldt `ok` dat het weg is"
    esp.online.add("tuin")
    t[0] += 2
    assert flush() == ["kk.lux=14", "kk.ok=1", "kk.pwr=3600.0"], "terug: alles weer"


def test_a_snapshot_waits_for_the_start_delay_and_loxone_can_ask_for_one(tmp_path):
    t = [1000.0]
    r, esp, hub = make(tmp_path, clock=lambda: t[0])
    hub.start_delay = 10
    hub._t0 = t[0]
    hub.miniserver = "192.168.1.112"
    hub.add_signal("lux", {"dir": "out", "ref": "e:tuin:lux"})
    hub.tick()
    assert not hub.queue, "de eerste 10 s na het starten wachten we"
    t[0] += 10
    hub.tick()
    assert list(hub.queue.values()) == [b"kk.lux=12"]
    hub.queue.clear()
    hub.request_snapshot()
    hub.tick()
    assert list(hub.queue.values()) == [b"kk.lux=12"], "op verzoek meteen, zonder wachten"


def test_nothing_goes_out_without_a_miniserver(tmp_path):
    _, esp, hub = make(tmp_path)
    hub.add_signal("lux", {"dir": "out", "ref": "e:tuin:lux"})
    hub.tick()
    assert not hub.queue


def test_values_arrive_at_the_miniserver_as_udp_one_per_message(tmp_path):
    async def go():
        r, esp, hub = make(tmp_path)
        mini = Mini()
        hub.add_device("tuin", "tuin")
        await up(hub, mini)
        try:
            await eventually(lambda: hub.stats["sent"] >= 8)
            got = mini.got()
            assert {"kk.tuin_motion=0", "kk.tuin_lux=12", "kk.tuin_power=3450.4", "kk.tuin_bri=50", "kk.tuin_ok=1"} <= set(got)
            assert all(len(g.encode()) < 40 and "\n" not in g for g in got), "kort, en één signaal per bericht: zo kan Loxone het herkennen"
            esp.vals["motion"] = True
            await eventually(lambda: "kk.tuin_motion=1" in mini.got())
        finally:
            await hub.close()
            mini.close()

    run(go())


# ------------------------------------------------------------ van Loxone


def _in(hub):
    hub.add_signal("bri", {"dir": "in", "ref": "e:tuin:bri"})
    hub.add_signal("on", {"dir": "in", "ref": "e:tuin:on"})
    hub.add_signal("scene", {"dir": "in", "ref": "e:tuin:scene"})


def test_a_udp_message_from_the_miniserver_sets_the_point(tmp_path):
    async def go():
        r, esp, hub = make(tmp_path)
        mini = Mini()
        _in(hub)
        await up(hub, mini)
        try:
            mini.send(b"kk.bri=80", hub.udp_port)
            await eventually(lambda: ("tuin", "bri", 80) in esp.sets)
            mini.send(b"kk.on=1", hub.udp_port)
            await eventually(lambda: ("tuin", "on", True) in esp.sets)
            mini.send(b"kk.bri=77", hub.udp_port)  # niet op de stap van 5: het dichtstbijzijnde
            await eventually(lambda: ("tuin", "bri", 75) in esp.sets)
            mini.send(b"kk.scene=1", hub.udp_port)
            await eventually(lambda: ("tuin", "scene", 1) in esp.sets)
            mini.send(b"kk.bri=500", hub.udp_port)  # buiten het bereik: naar het maximum
            await eventually(lambda: ("tuin", "bri", 100) in esp.sets)
        finally:
            await hub.close()
            mini.close()

    run(go())


def test_only_the_miniserver_may_set_things_and_only_known_signals(tmp_path):
    async def go():
        r, esp, hub = make(tmp_path)
        mini = Mini()
        _in(hub)
        await up(hub, mini)
        try:
            hub.miniserver = "10.9.9.9"  # een ander adres dan waar de datagrammen vandaan komen
            mini.send(b"kk.bri=80", hub.udp_port)
            await asyncio.sleep(0.2)
            assert esp.sets == [], "van een ander adres: genegeerd"
            hub.miniserver = "127.0.0.1"
            mini.send(b"kk.unknown=1\nkk.bri=60\nrandom text\nkk.unlock=1", hub.udp_port)
            await eventually(lambda: ("tuin", "bri", 60) in esp.sets)
            assert [s for s in esp.sets if s[1] != "bri"] == [], "een onbekend signaal en een gevaarlijk punt zonder signaal doen niets"
            assert hub.stats["unknown"] == 2
        finally:
            await hub.close()
            mini.close()

    run(go())


def test_the_same_value_again_does_nothing_and_skip_first_remembers_it(tmp_path):
    async def go():
        r, esp, hub = make(tmp_path)
        _in(hub)
        hub.update_signal("on", {"skip_first": True})
        assert await hub.apply([("bri", 50.0)]) == [("bri", "zelfde")], "staat al zo: niets sturen (spaart ook de grenzen van de clouds)"
        assert await hub.apply([("on", 1.0)]) == [("on", "onthouden")], "de eerste waarde na een start voert dit signaal niet uit, als de gebruiker dat kiest"
        assert esp.sets == []
        assert await hub.apply([("on", 1.0)]) == [("on", "ok")]
        assert esp.sets == [("tuin", "on", True)]
        assert await hub.apply([("on", 1.0)]) == [("on", "zelfde")]
        assert await hub.apply([("sync", 1.0)]) == [("sync", "ok")] and hub._t0 < 0

    run(go())


def test_a_refused_value_says_why(tmp_path):
    async def go():
        r, esp, hub = make(tmp_path)
        _in(hub)

        real = esp.set

        async def no(did, pid, value, confirm=False):
            if pid == "bri":
                raise ModelError("geen verbinding met het apparaat")
            return await real(did, pid, value, confirm)

        esp.set = no
        assert await hub.apply([("bri", 80.0)]) == [("bri", "geen verbinding met het apparaat")]
        assert hub.stats["refused"] == 1 and hub.view()["signals"][0]["result"] == "geen verbinding met het apparaat"
        assert await hub.apply([("scene", 7.0)]) == [("scene", "Scene: geen toegestane keuze")]

    run(go())


def test_http_get_and_post_set_points_and_answer_with_the_result(tmp_path):
    async def go():
        r, esp, hub = make(tmp_path)
        mini = Mini()
        _in(hub)
        await up(hub, mini)
        base = f"http://127.0.0.1:{hub.http_port}"
        try:
            async with aiohttp.ClientSession() as http:
                a = await http.get(f"{base}/loxone?kk.bri=85")
                assert a.status == 200 and (await a.text()).strip() == "ok" and ("tuin", "bri", 85) in esp.sets
                a = await http.post(f"{base}/loxone", data="kk.on=1\nkk.scene=1")
                assert a.status == 200 and ("tuin", "on", True) in esp.sets and ("tuin", "scene", 1) in esp.sets
                a = await http.get(f"{base}/loxone?kk.bri=85")
                assert a.status == 200, "dezelfde waarde nog eens is geen fout"
                a = await http.get(f"{base}/loxone?kk.nope=1")
                assert a.status == 400 and "onbekend signaal" in await a.text()
                a = await http.get(f"{base}/loxone?hello=1")
                assert a.status == 400 and "kk." in await a.text()
                assert (await http.get(f"{base}/other")).status == 404
                hub.miniserver = "10.9.9.9"
                a = await http.get(f"{base}/loxone?kk.bri=20")
                assert a.status == 403 and ("tuin", "bri", 20) not in esp.sets
        finally:
            await hub.close()
            mini.close()

    run(go())


def test_nothing_is_accepted_until_the_miniserver_is_set(tmp_path):
    async def go():
        r, esp, hub = make(tmp_path)
        _in(hub)
        await hub.start()
        try:
            async with aiohttp.ClientSession() as http:
                assert (await http.get(f"http://127.0.0.1:{hub.http_port}/loxone?kk.bri=85")).status == 403
            Mini().send(b"kk.bri=80", hub.udp_port)
            await asyncio.sleep(0.15)
            assert esp.sets == []
        finally:
            await hub.close()

    run(go())


# ------------------------------------------------------------ per apparaat

def _two_devices(tmp_path):
    r, esp, hub = make(tmp_path)
    esp.devices["bad"] = {"name": "Badkamer", "shown": None, "points": [pt("lux", "int"), pt("on", "bool", writable=True)]}
    esp.vals.update({"lux": 5})
    esp.online.add("bad")
    hub.add_device("tuin", "tuin", [{"pid": "bri", "out": True, "in": True}])
    hub.add_device("bad", "bad", [{"pid": "lux", "out": True}, {"pid": "on", "out": True, "in": True}])
    hub.configure({"miniserver": "192.168.1.112", "addon_host": "192.168.1.5"})
    return hub


def test_signals_are_grouped_by_device_and_ordered_by_device_name_then_name(tmp_path):
    hub = _two_devices(tmp_path)
    rows = hub.view()["signals"]
    assert [r["group_name"] for r in rows] == sorted(r["group_name"] for r in rows), "Badkamer vóór Tuin, ook al is tuin eerder gemaakt"
    assert {r["group"] for r in rows} == {"tuin", "bad"} and {r["group_name"] for r in rows} == {"Tuin", "Badkamer"}
    bad = [r["id"] for r in rows if r["group"] == "bad"]
    assert bad == sorted(bad) and "bad_ok" in bad, "binnen een apparaat op naam, de bereikbaarheid hoort erbij"


def test_a_file_can_hold_one_device_and_says_so_in_its_title(tmp_path):
    hub = _two_devices(tmp_path)
    everything = ET.fromstring(hub.viu())
    assert {e.attrib["Title"] for e in everything.iter("VirtualInUdpCmd")} >= {"kk.tuin_bri", "kk.bad_lux", "kk.bad_ok"} and everything.attrib["Title"] == "Huisconfig naar Loxone"
    one = ET.fromstring(hub.viu(["bad"]))
    assert {e.attrib["Title"] for e in one.iter("VirtualInUdpCmd")} == {"kk.bad_lux", "kk.bad_on", "kk.bad_ok"}
    assert one.attrib["Title"] == "Huisconfig naar Loxone - Badkamer", "twee imports in Config zijn zo uit elkaar te houden"
    vo = ET.fromstring(hub.vo(["tuin"]))
    assert {e.attrib["Title"] for e in vo.iter("VirtualOutCmd")} == {"kk.tuin_bri_set"} and vo.attrib["Title"] == "Huisconfig van Loxone - Tuin"
    assert "kk.bad_lux" not in hub.table(["tuin"]) and "kk.tuin_bri_set" in hub.table(["tuin"])
    assert list(ET.fromstring(hub.viu([])).iter("VirtualInUdpCmd")) == [], "een lege keuze is een leeg bestand, niet alles"
    assert "2 apparaten" not in hub.viu(["bad", "tuin"]) and "Badkamer, Tuin" in hub.viu(["bad", "tuin"])


def test_the_web_api_filters_the_downloads_by_device(tmp_path):
    async def go():
        r, esp, hub = make(tmp_path)
        esp.devices["bad"] = {"name": "Badkamer", "shown": None, "points": [pt("lux", "int")]}
        esp.online.add("bad")
        hub.add_device("tuin", "tuin", [{"pid": "bri", "out": True}])
        hub.add_device("bad", "bad", [{"pid": "lux", "out": True}])
        hub.configure({"miniserver": "192.168.1.112", "addon_host": "192.168.1.5"})
        s = Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True)
        c = TestClient(TestServer(web.make_app(web.Ctx(s, r.engine, espdev=esp, loxone=hub))))
        await c.start_server()
        try:
            allf = await c.get("/api/loxone/viu.xml")
            assert 'filename="VIU_Huisconfig.xml"' in allf.headers["Content-Disposition"]
            one = await c.get("/api/loxone/viu.xml?devices=bad")
            assert 'filename="VIU_Huisconfig_bad.xml"' in one.headers["Content-Disposition"]
            assert {e.attrib["Title"] for e in ET.fromstring(await one.text()).iter("VirtualInUdpCmd")} == {"kk.bad_lux", "kk.bad_ok"}
            both = ET.fromstring(await (await c.get("/api/loxone/viu.xml?devices=bad,tuin")).text())
            assert "kk.tuin_bri" in {e.attrib["Title"] for e in both.iter("VirtualInUdpCmd")}
            none = ET.fromstring(await (await c.get("/api/loxone/viu.xml?devices=")).text())
            assert list(none.iter("VirtualInUdpCmd")) == []
            assert "kk.bad_lux" not in await (await c.get("/api/loxone/table.txt?devices=tuin")).text()
        finally:
            await c.close()

    run(go())


# ------------------------------------------------------------ een slot vergrendelen en ontgrendelen vanuit Loxone


def lock_rig(tmp_path):
    """Een Nuki-slot met een gewone en een gevaarlijke actie, en een brug die over HTTP wordt aangeroepen."""
    r, esp, hub = make(tmp_path)
    esp.devices["slot"] = {"name": "Voordeur", "driver": "nuki", "shown": None, "points": [pt("locked", "bool")], "actions": [
        {"id": "lock", "label": "Vergrendelen", "danger": False, "args": [], "confirm": ""},
        {"id": "unlock", "label": "Ontgrendelen", "danger": True, "args": [], "confirm": "echt?"},
        {"id": "timer", "label": "Met argument", "danger": False, "args": [{"id": "n", "label": "N", "kind": "int"}], "confirm": ""}]}
    esp.online.add("slot")
    esp.did = []

    async def do(did, aid, args=None, confirm=False):
        esp.did.append((did, aid, confirm))
        return {"ok": True}

    esp.do = do
    return r, esp, hub


def test_a_lock_action_must_be_confirmed_and_only_a_nuki_action_without_arguments_can_be_made(tmp_path):
    _, esp, hub = lock_rig(tmp_path)
    hub.add_signal("deur_lock", {"dir": "in", "ref": "a:slot:lock"})
    with pytest.raises(ModelError, match="bevestig"):
        hub.add_signal("deur_unlock", {"dir": "in", "ref": "a:slot:unlock"})
    hub.add_signal("deur_unlock", {"dir": "in", "ref": "a:slot:unlock", "ack": True})
    for n, ref in enumerate(("a:slot:nope", "a:slot:timer", "a:tuin:lock", "a:weg:lock")):
        with pytest.raises(ModelError, match="onbekende actie"):
            hub.add_signal(f"x{n}", {"dir": "in", "ref": ref, "ack": True})
    with pytest.raises(ModelError):
        hub.add_signal("uit", {"dir": "out", "ref": "a:slot:lock"})
    s = hub.signals["deur_unlock"]
    assert s["kind"] == "bool" and s["ack"] is True and hub.signals["deur_lock"]["ack"] is False
    rows = {r["id"]: r for r in hub.view()["signals"]}
    assert rows["deur_unlock"]["action"] and rows["deur_unlock"]["danger"] and rows["deur_unlock"]["group"] == "slot" and rows["deur_unlock"]["group_name"] == "Voordeur"
    assert not rows["deur_lock"]["danger"]
    hub.configure({"miniserver": "192.168.1.112", "addon_host": "192.168.1.5"})
    vo = ET.fromstring(hub.vo())
    assert {e.attrib["Title"] for e in vo.iter("VirtualOutCmd")} == {"kk.deur_lock", "kk.deur_unlock"}, "ook acties staan in het bestand voor Loxone"


def test_unlocking_needs_http_a_rising_edge_and_a_confirmed_signal(tmp_path):
    async def go():
        _, esp, hub = lock_rig(tmp_path)
        hub.add_signal("deur_unlock", {"dir": "in", "ref": "a:slot:unlock", "ack": True})
        assert await hub.apply([("deur_unlock", 0.0)], "http") == [("deur_unlock", "ok")], "een 0 doet niets"
        assert (await hub.apply([("deur_unlock", 1.0)], "udp"))[0][1] == "deze actie opent de deur en wordt alleen over HTTP aangenomen"
        assert esp.did == []
        # de laatste waarde was de 1 van de UDP-poging: daarna is er geen overgang meer
        assert (await hub.apply([("deur_unlock", 1.0)], "http"))[0][1] == "geen overgang van 0 naar 1: niet uitgevoerd"
        await hub.apply([("deur_unlock", 0.0)], "http")
        assert await hub.apply([("deur_unlock", 1.0)], "http") == [("deur_unlock", "ok")]
        assert esp.did == [("slot", "unlock", True)]
        await hub.apply([("deur_unlock", 0.0)], "http")
        assert "te kort" in (await hub.apply([("deur_unlock", 1.0)], "http"))[0][1], "hoogstens eens per ACTION_GAP"
        assert len(esp.did) == 1
        assert hub.got["deur_unlock"]["result"].startswith("te kort")

    run(go())


def test_the_first_value_after_a_start_is_only_remembered_for_an_action_that_opens_the_door(tmp_path):
    async def go():
        _, esp, hub = lock_rig(tmp_path)
        hub.add_signal("deur_unlock", {"dir": "in", "ref": "a:slot:unlock", "ack": True})
        assert "geen overgang" in (await hub.apply([("deur_unlock", 1.0)], "http"))[0][1], "een herstarte Miniserver die zijn 1 nog eens stuurt opent niets"
        assert await hub.apply([("deur_unlock", 1.0)], "http") != [("deur_unlock", "ok")]
        assert esp.did == []

    run(go())


def test_an_action_that_opens_the_door_does_not_wait_and_is_not_retried_when_the_lock_is_away(tmp_path):
    async def go():
        _, esp, hub = lock_rig(tmp_path)
        hub.add_signal("deur_unlock", {"dir": "in", "ref": "a:slot:unlock", "ack": True})
        await hub.apply([("deur_unlock", 0.0)], "http")
        esp.online.discard("slot")
        assert "niet bereikbaar" in (await hub.apply([("deur_unlock", 1.0)], "http"))[0][1]
        esp.online.add("slot")
        await asyncio.sleep(0.05)
        assert esp.did == [], "een deur die later alsnog opengaat is erger dan een deur die dicht blijft"
        hub.signals["deur_unlock"]["ack"] = False  # ook als de bevestiging uit het bestand verdwijnt
        await hub.apply([("deur_unlock", 0.0)], "http")
        assert "niet bevestigd" in (await hub.apply([("deur_unlock", 1.0)], "http"))[0][1]
        assert esp.did == []

    run(go())


def test_locking_is_allowed_over_udp_but_not_more_than_once_every_few_seconds(tmp_path):
    async def go():
        _, esp, hub = lock_rig(tmp_path)
        hub.add_signal("deur_lock", {"dir": "in", "ref": "a:slot:lock"})
        assert await hub.apply([("deur_lock", 1.0)], "udp") == [("deur_lock", "ok")]
        assert esp.did == [("slot", "lock", True)]
        assert "te kort" in (await hub.apply([("deur_lock", 1.0)], "udp"))[0][1]
        assert await hub.apply([("deur_lock", 0.0)], "udp") == [("deur_lock", "ok")] and len(esp.did) == 1

    run(go())


def test_door_actions_force_http_and_follow_the_connection_setting(tmp_path):
    _, esp, hub = lock_rig(tmp_path)
    hub.configure({"transport": "udp"})
    with pytest.raises(ModelError, match="HTTP"):
        hub.add_signal("deur_unlock", {"dir": "in", "ref": "a:slot:unlock", "ack": True})
    hub.add_signal("deur_lock", {"dir": "in", "ref": "a:slot:lock"})
    hub.configure({"transport": "http"})
    hub.add_signal("deur_unlock", {"dir": "in", "ref": "a:slot:unlock", "ack": True})
    with pytest.raises(ModelError, match="gevaarlijke acties"):
        hub.configure({"transport": "udp"})
    assert hub.transport == "http"
    hub.remove_signal("deur_unlock")
    hub.configure({"transport": "udp"})


def test_an_action_arrives_over_the_real_http_and_udp_sockets(tmp_path):
    async def go():
        r, esp, hub = lock_rig(tmp_path)
        mini = Mini()
        _in(hub)
        hub.add_signal("deur_lock", {"dir": "in", "ref": "a:slot:lock"})
        hub.add_signal("deur_unlock", {"dir": "in", "ref": "a:slot:unlock", "ack": True})
        await up(hub, mini)
        base = f"http://127.0.0.1:{hub.http_port}"
        try:
            async with aiohttp.ClientSession() as http:
                assert (await http.get(f"{base}/loxone?kk.deur_unlock=0")).status == 200
                a = await http.get(f"{base}/loxone?kk.deur_unlock=1")
                assert a.status == 200 and esp.did == [("slot", "unlock", True)]
                a = await http.get(f"{base}/loxone?kk.deur_unlock=1")
                assert a.status == 400 and "overgang" in await a.text()
            mini.send(b"kk.deur_unlock=0", hub.udp_port)
            await asyncio.sleep(0.1)
            mini.send(b"kk.deur_unlock=1", hub.udp_port)
            await asyncio.sleep(0.2)
            assert esp.did == [("slot", "unlock", True)], "over UDP opent hij niet"
            mini.send(b"kk.deur_lock=1", hub.udp_port)
            await eventually(lambda: ("slot", "lock", True) in esp.did)
        finally:
            await hub.close()
            mini.close()

    run(go())


# ------------------------------------------------------------ kanalen: elk een eigen poort


def test_channels_have_their_own_port_and_a_name_and_are_checked(tmp_path):
    _, esp, hub = make(tmp_path)
    assert hub.channel_list() == [{"id": "main", "name": "Standaard", "port": 9100, "default": True}]
    cid = hub.add_channel("Tuin & Oprit", 9101)
    assert cid == "tuin_oprit" and hub.channel_port(cid) == 9101 and hub.channel_name(cid) == "Tuin & Oprit"
    assert hub.add_channel("Tuin & Oprit", 9102) == "tuin_oprit_2", "dezelfde naam geeft een ander id"
    for name, port, word in [("Nieuw", 9100, "al van een ander kanaal"), ("Nieuw", 9101, "al van een ander kanaal"), ("", 9105, "naam"), ("x" * 31, 9105, "naam"), ("Nieuw", 0, "poort"),
                             ("Nieuw", 70000, "poort"), ("Nieuw", "abc", "poort")]:
        with pytest.raises(ModelError, match=word):
            hub.add_channel(name, port)
    hub.update_channel(cid, {"name": "Buiten", "port": 9150})
    assert hub.channel_name(cid) == "Buiten" and hub.channel_port(cid) == 9150
    hub.update_channel(cid, {"port": 9150})  # dezelfde poort mag, het is zijn eigen
    with pytest.raises(ModelError, match="al van een ander"):
        hub.update_channel(cid, {"port": 9102})
    with pytest.raises(ModelError, match="onbekend kanaal"):
        hub.update_channel("main", {"port": 9200})
    with pytest.raises(ModelError, match="al van een ander"):
        hub.configure({"out_port": 9150})
    assert hub.remove_channel(cid) and not hub.remove_channel(cid)


def test_a_device_in_a_channel_goes_to_its_port_and_its_files_hold_only_its_signals(tmp_path):
    _, esp, hub = make(tmp_path)
    esp.devices["bad"] = {"name": "Badkamer", "shown": None, "points": [pt("lux", "int"), pt("on", "bool", writable=True)]}
    esp.online.add("bad")
    cid = hub.add_channel("Badkamer", 9101)
    hub.add_device("tuin", "tuin", [{"pid": "bri", "out": True, "in": True}])
    hub.add_device("bad", "bad", [{"pid": "lux", "out": True}, {"pid": "on", "out": True, "in": True}], channel=cid)
    hub.configure({"miniserver": "192.168.1.112", "addon_host": "192.168.1.5"})
    assert hub.assign == {"bad": cid}
    main = ET.fromstring(hub.viu())
    assert main.attrib["Port"] == "9100" and {e.attrib["Title"] for e in main.iter("VirtualInUdpCmd")} == {"kk.tuin_bri", "kk.tuin_ok"} and main.attrib["Title"] == "Huisconfig naar Loxone"
    bad = ET.fromstring(hub.viu(channel=cid))
    assert bad.attrib["Port"] == "9101" and {e.attrib["Title"] for e in bad.iter("VirtualInUdpCmd")} == {"kk.bad_lux", "kk.bad_on", "kk.bad_ok"}
    assert bad.attrib["Title"] == "Huisconfig naar Loxone - Badkamer", "twee imports in Config zijn zo uit elkaar te houden"
    assert {e.attrib["Title"] for e in ET.fromstring(hub.vo(channel=cid)).iter("VirtualOutCmd")} == {"kk.bad_on_set"}
    assert {e.attrib["Title"] for e in ET.fromstring(hub.vo()).iter("VirtualOutCmd")} == {"kk.tuin_bri_set"}
    assert "kk.bad_lux" in hub.table(channel=cid) and "kk.bad_lux" not in hub.table() and "poort 9101" in hub.table(channel=cid)
    with pytest.raises(ModelError, match="onbekend kanaal"):
        hub.viu(channel="nee")
    with pytest.raises(ModelError, match="onbekend kanaal"):
        hub.add_device("bad", "x", [{"pid": "lux", "out": True}], channel="nee")


def test_values_are_sent_to_the_port_of_the_channel_of_their_device(tmp_path):
    async def go():
        r, esp, hub = make(tmp_path)
        esp.devices["bad"] = {"name": "Badkamer", "shown": None, "points": [pt("lux", "int")]}
        esp.online.add("bad")
        esp.vals["lux"] = 5
        main, other = Mini(), Mini()
        cid = hub.add_channel("Badkamer", other.port)
        hub.add_device("tuin", "tuin", [{"pid": "lux", "out": True}])
        hub.add_device("bad", "bad", [{"pid": "lux", "out": True}], channel=cid)
        await up(hub, main)
        try:
            a, b = [], []

            def both():
                a.extend(main.got())
                b.extend(other.got())
                return "kk.tuin_lux=5" in a and "kk.bad_lux=5" in b

            await eventually(both)
            assert not any(x.startswith("kk.bad_") for x in a) and not any(x.startswith("kk.tuin_") for x in b)
            hub.set_channel("bad", "main")  # een apparaat verhuist: voortaan naar de andere poort, en meteen alles opnieuw
            moved = []

            def arrived():
                moved.extend(main.got())
                return "kk.bad_lux=5" in moved

            await eventually(arrived)
        finally:
            await hub.close()
            main.close()
            other.close()

    run(go())


def test_a_channel_in_use_cannot_be_removed_and_assignments_are_checked(tmp_path):
    _, esp, hub = make(tmp_path)
    cid = hub.add_channel("Tuin", 9101)
    hub.add_device("tuin", "tuin", [{"pid": "lux", "out": True}], channel=cid)
    with pytest.raises(ModelError, match="Tuin"):
        hub.remove_channel(cid)
    with pytest.raises(ModelError, match="onbekend apparaat"):
        hub.set_channel("weg", cid)
    with pytest.raises(ModelError, match="onbekend kanaal"):
        hub.set_channel("tuin", "nee")
    hub.set_channel("tuin", "main")
    assert hub.assign == {} and hub.remove_channel(cid)


def test_channels_and_assignments_survive_a_restart_and_a_broken_channel_is_skipped(tmp_path):
    r, esp, hub = make(tmp_path)
    cid = hub.add_channel("Tuin", 9101)
    hub.add_device("tuin", "tuin", [{"pid": "lux", "out": True}], channel=cid)
    raw = json.loads((tmp_path / "loxone.json").read_text(encoding="utf-8"))
    raw["channels"]["kapot"] = {"name": "", "port": 99999}
    raw["assign"]["x"] = "kapot"
    (tmp_path / "loxone.json").write_text(json.dumps(raw), encoding="utf-8")
    again = LoxoneHub(tmp_path, r.engine, esp, port=0)
    assert set(again.channels) == {cid} and again.assign == {"tuin": cid}
    assert again.channel_port(cid) == 9101


def test_the_web_api_manages_channels_and_filters_the_files_by_channel(tmp_path):
    async def go():
        r, esp, hub = make(tmp_path)
        s = Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True)
        c = TestClient(TestServer(web.make_app(web.Ctx(s, r.engine, espdev=esp, loxone=hub))))
        await c.start_server()
        try:
            assert (await c.put("/api/loxone", json={"miniserver": "192.168.1.112", "addon_host": "192.168.1.5", "out_port": 9100})).status == 200
            made = await (await c.post("/api/loxone/channels", json={"name": "Tuin", "port": 9101})).json()
            cid = made["id"]
            assert (await c.post("/api/loxone/channels", json={"name": "Dubbel", "port": 9101})).status == 400
            assert (await c.post("/api/loxone/device", json={"did": "tuin", "prefix": "tuin", "channel": cid})).status == 200
            view = await (await c.get("/api/loxone")).json()
            ch = {x["id"]: x for x in view["channels"]}
            assert ch[cid]["port"] == 9101 and ch[cid]["n_out"] > 0 and ch[cid]["devices"] == ["Tuin"] and ch["main"]["n_out"] == 0
            assert all(x["channel"] == cid for x in view["signals"])
            viu = await c.get(f"/api/loxone/viu.xml?channel={cid}")
            assert f'filename="VIU_Huisconfig_{cid}.xml"' in viu.headers["Content-Disposition"] and ET.fromstring(await viu.text()).attrib["Port"] == "9101"
            assert list(ET.fromstring(await (await c.get("/api/loxone/viu.xml")).text()).iter("VirtualInUdpCmd")) == []
            assert (await c.get("/api/loxone/viu.xml?channel=nee")).status == 400
            assert (await c.delete(f"/api/loxone/channels/{cid}")).status == 400, "in gebruik"
            assert (await c.post("/api/loxone/assign", json={"group": "tuin", "channel": "main"})).status == 200
            assert (await c.put(f"/api/loxone/channels/{cid}", json={"name": "Buiten", "port": 9200})).status == 200
            assert (await c.delete(f"/api/loxone/channels/{cid}")).status == 200 and (await c.delete(f"/api/loxone/channels/{cid}")).status == 404
        finally:
            await c.close()

    run(go())


# ------------------------------------------------------------ de web-API en de bestanden


def test_the_web_api_makes_signals_and_downloads_the_files_for_loxone_config(tmp_path):
    async def go():
        r, esp, hub = make(tmp_path)
        s = Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True)
        hub.port = 47510
        ctx = web.Ctx(s, r.engine, espdev=esp, loxone=hub)
        c = TestClient(TestServer(web.make_app(ctx)))
        await c.start_server()
        try:
            assert (await c.put("/api/loxone", json={"miniserver": "192.168.1.112", "addon_host": "192.168.1.5", "out_port": 9100, "transport": "http"})).status == 200
            assert (await c.put("/api/loxone", json={"miniserver": "nee"})).status == 400
            cand = await (await c.get("/api/loxone/device/tuin")).json()
            assert next(x for x in cand if x["pid"] == "bri") == {"pid": "bri", "label": "Bri", "kind": "int", "out": True, "in": True, "on": True}
            assert "name" not in [x["pid"] for x in cand] and "rssi" in [x["pid"] for x in cand]
            made = await (await c.post("/api/loxone/device", json={"did": "tuin", "prefix": "tuin"})).json()
            assert "tuin_bri_set" in made["made"]
            assert (await c.post("/api/loxone/signals", json={"id": "extra", "dir": "out", "ref": "e:tuin:lux", "label": "Lux"})).status == 200
            assert (await c.put("/api/loxone/signals/extra", json={"enabled": False})).status == 200
            assert (await c.post("/api/loxone/signals", json={"id": "deur", "dir": "in", "ref": "e:tuin:unlock"})).status == 400
            assert (await c.delete("/api/loxone/signals/nope")).status == 404
            view = await (await c.get("/api/loxone")).json()
            assert view["me"] == "192.168.1.5" and {x["id"] for x in view["signals"]} >= {"extra", "tuin_ok"} and "password" not in str(view)
            viu = await c.get("/api/loxone/viu.xml")
            assert viu.status == 200 and "VIU_Huisconfig.xml" in viu.headers["Content-Disposition"]
            root = ET.fromstring(await viu.text())
            titles = {e.attrib["Title"] for e in root.iter("VirtualInUdpCmd")}
            assert "kk.tuin_motion" in titles and "kk.tuin_bri_set" not in titles and "kk.extra" not in titles, "alleen wat naar Loxone gaat, en niet wat uit staat"
            assert root.attrib["Address"] == "192.168.1.5" and root.attrib["Port"] == "9100"
            vo = ET.fromstring(await (await c.get("/api/loxone/vo.xml")).text())
            assert vo.attrib["Address"] == "http://192.168.1.5:47510"
            assert {e.attrib["Title"] for e in vo.iter("VirtualOutCmd")} == {"kk.tuin_bri_set", "kk.tuin_on_set", "kk.tuin_scene_set"}
            table = await (await c.get("/api/loxone/table.txt")).text()
            assert "kk.tuin_motion=\\v" in table and "loxone?kk.tuin_bri_set=<v>" in table
            assert (await c.post("/api/loxone/sync")).status == 200
            hub.configure({"miniserver": "", "addon_host": ""})
            assert (await c.get("/api/loxone/viu.xml")).status == 400, "zonder adres een duidelijke fout, geen bestand met niets erin"
        finally:
            await c.close()

    run(go())
