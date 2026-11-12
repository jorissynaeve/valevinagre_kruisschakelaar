"""ESPHome-toestellen als HUISDEV-apparaat (esphome.py en de ESPHome-kant van espdevhub.py): de eventstroom lezen, entiteiten omzetten naar punten en acties,
stabiele ids, opdrachten over echte HTTP tegen een nagebootst toestel (tests/fakeesphome.py), offline en terug, de entiteiten in HA (standaard uit),
opslag en herstart, het blok op het schema en de foutmeldingen."""
import asyncio
import json
import time

import pytest

from kkaddon import esphome as esp
from kkaddon import espdevhub as eh
from kkaddon import schema
from kkaddon.espdevhub import EspDevHub
from kkaddon.model import ModelError
from tests.fakeesphome import FakeEsphome, plug
from tests.rig import Rig

DID = "esphome_powerplug04tv"


async def eventually(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("duurde te lang")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(eh, "ES_SETTLE", 0.1)
    monkeypatch.setattr(eh, "ES_FLUSH", 0.05)
    monkeypatch.setattr(eh, "ES_PROBE", 3.0)
    monkeypatch.setattr(eh, "TICK", 3600)


async def rig(tmp_path, fake=None, **add):
    r = Rig(tmp_path)
    hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
    fake = fake or plug()
    await fake.start()
    did = await hub.add_esphome("127.0.0.1", fake.port, **add)
    await eventually(lambda: did in hub.online)
    return r, hub, fake, did


async def shutdown(hub, *fakes):
    hub.close()
    for f in fakes:
        await f.stop()
    await asyncio.sleep(0.05)


def pts(hub, did=DID):
    return {p["id"]: p for p in hub.devices[did]["points"]}


def status(hub, did=DID):
    return next(x for x in hub.status() if x["id"] == did)


# ------------------------------------------------------------ de eventstroom lezen


def test_sse_is_read_across_chunk_boundaries_and_noise_is_ignored():
    raw = (b'retry: 30000\nid: 1\nevent: ping\ndata: {"title":"plug"}\n\n'
           b'event: state\ndata: {"id":"sensor-a","name":"A","value":1}\n\n'
           b"id: 2\nevent: log\ndata: \x1b[0;36m[D][x]: hallo\x1b[0m\n\n"
           b"event: state\r\ndata: niet json\r\n\r\n"
           b"event: ping\ndata: \n\n")
    for size in (1, 7, len(raw)):  # een ESP stuurt over wifi in stukjes van willekeurige lengte
        sse, got = esp.Sse(), []
        for i in range(0, len(raw), size):
            for ev, data in sse.feed(raw[i:i + size]):
                p = esp.parse_event(ev, data)
                if p:
                    got.append(p)
        assert got == [("ping", "plug"), ("state", {"id": "sensor-a", "name": "A", "value": 1}), ("ping", "")], size
    with pytest.raises(ValueError):
        esp.Sse().feed(b"x" * (1 << 20 + 1))


def test_ids_are_split_like_esphome_does():
    assert esp.split_id("sensor-power") == ("sensor", "power")
    assert esp.split_id("text_sensor-reset_reason") == ("text_sensor", "reset_reason")
    assert esp.split_id("sensor-a-b") == ("sensor", "a-b")
    assert esp.split_id("power") is None and esp.split_id("-x") is None and esp.split_id("Sensor-x") is None


# ------------------------------------------------------------ van entiteit naar punt of actie


def built(fake=None, emap=None, prev=None):
    return esp.build({k: dict(v) for k, v in (fake or plug()).ents.items()}, emap or {}, prev or [])


def test_the_plug_becomes_points_and_actions():
    b = built()
    p = {x["id"]: x for x in b["points"]}
    assert sorted(p) == ["apparent_power", "current", "energy", "power", "power_factor", "powerplug04tv", "red_led", "reset_reason", "status_led_mode", "uptime", "voltage"]
    assert [a["id"] for a in b["actions"]] == ["restart"]
    assert (p["power"]["kind"], p["power"]["unit"], p["power"]["decimals"], p["power"]["cls"], p["power"]["state"], p["power"]["writable"]) == ("float", "W", 1, "power", "measurement", False)
    assert (p["current"]["decimals"], p["current"]["cls"]) == (3, "current"), "decimalen zoals ESPHome ze toont (0.046 A)"
    assert (p["energy"]["cls"], p["energy"]["state"]) == ("energy", "total_increasing")
    assert (p["voltage"]["cls"], p["apparent_power"]["cls"]) == ("voltage", "apparent_power")
    assert (p["power_factor"]["unit"], p["power_factor"]["cls"]) == ("", "")
    assert (p["powerplug04tv"]["kind"], p["powerplug04tv"]["writable"], p["red_led"]["kind"], p["red_led"]["writable"]) == ("bool", True, "bool", True)
    assert (p["uptime"]["diag"], p["reset_reason"]["diag"], p["reset_reason"]["kind"], p["reset_reason"]["writable"]) == (True, True, "text", False)
    s = p["status_led_mode"]
    assert (s["kind"], s["writable"], s["group"], s["options"]) == ("enum", True, "instellingen", [{"v": 0, "l": "Normal"}, {"v": 1, "l": "Invert"}, {"v": 2, "l": "Off"}])
    assert b["actions"][0]["danger"] and b["actions"][0]["confirm"], "een restart is niet iets voor een draad in de logica"
    assert b["skipped"] == []


def test_every_built_point_is_a_valid_huisdev_point():
    d = eh.clean_descriptor({"proto": "1.0", "rev": built()["rev"], "device": {"id": "esphome-x"}, "points": [{**x, "class": x["cls"]} for x in built()["points"]],
                             "actions": built()["actions"]})
    assert len(d["points"]) == 11 and len(d["actions"]) == 1, "niets wordt door de descriptor-controle weggegooid"


def test_other_domains_numbers_and_text():
    f = FakeEsphome()
    f.number("brightness", "Helderheid", 50, 0, 100, 5, "%", entity_category=1)
    f.number("offset", "Offset", 1.5, -5, 5, 0.1, "°C")
    f.add("text-note", "Notitie", value="hoi", state="hoi", mode=0)
    f.add("fan-vent", "Ventilator", state="OFF")
    f.add("binary_sensor-door", "Deur", value=True, state="ON")
    f.add("cover-garage", "Garage", state="OPEN")
    f.add("lock-front", "Voordeur", state="LOCKED")
    f.add("select-leeg", "Leeg", value="", state="", option=[])
    b = built(f)
    p = {x["id"]: x for x in b["points"]}
    assert (p["brightness"]["kind"], p["brightness"]["min"], p["brightness"]["max"], p["brightness"]["step"], p["brightness"]["writable"]) == ("int", 0, 100, 5, True)
    assert (p["offset"]["kind"], p["offset"]["decimals"], p["offset"]["cls"], p["offset"]["unit"]) == ("float", 1, "temperature", "°C")
    assert (p["note"]["kind"], p["note"]["writable"], p["vent"]["kind"], p["vent"]["writable"], p["door"]["kind"], p["door"]["writable"]) == ("text", True, "bool", True, "bool", False)
    assert b["skipped"] == ["cover-garage", "lock-front", "select-leeg"], "wat we niet kennen (of niet kunnen gebruiken) wordt gemeld, niet stil weggelaten"


def test_values_follow_the_type_of_the_point():
    b = built()
    p = {x["id"]: x for x in b["points"]}
    e = plug().ents
    assert esp.value_of(p["power"], e["sensor-power"]) == pytest.approx(0.685099)
    assert esp.value_of(p["powerplug04tv"], e["switch-powerplug04tv"]) is True
    assert esp.value_of(p["red_led"], e["light-red_led"]) is False, "een licht heeft alleen een tekst ON/OFF"
    assert esp.value_of(p["status_led_mode"], {**e["select-status_led_mode"], "value": "Invert"}) == 1
    assert esp.value_of(p["status_led_mode"], {**e["select-status_led_mode"], "value": "Onbekend"}) is None
    assert esp.value_of(p["reset_reason"], e["text_sensor-reset_reason"]) == "SW Reboot"
    for bad in (None, {}, {"value": None, "state": "NA"}, {"value": float("nan")}, {"value": True}, {"value": "tekst"}):
        assert esp.value_of(p["power"], bad) is None, bad
    assert esp.value_of(p["reset_reason"], {"state": "NA"}) is None


def test_ids_are_valid_unique_and_never_change():
    f = FakeEsphome()
    f.sensor("power", "Power", 1.0, "W")
    f.switch("power", "Power schakelaar")  # dezelfde naam in een ander domein
    f.sensor("123abc", "Cijfers", 1.0)
    f.sensor("a_very_long_entity_name_that_goes_on_and_on", "Lang", 1.0)
    f.sensor("a_very_long_entity_name_that_goes_on_and_on_2", "Lang 2", 1.0)
    f.sensor("café", "Accent", 1.0)
    b = built(f)
    ids = [x["id"] for x in b["points"]]
    assert len(ids) == len(set(ids)) == 6
    assert all(eh.PID_RE.match(i) for i in ids), ids
    emap = b["emap"]
    # er komt iets bij, iets valt weg, de volgorde verandert: wat er was houdt zijn id
    f.sensor("aaa", "Eerder in het alfabet", 1.0)
    f.sensor("zzz_power", "Later", 1.0)
    f.remove("sensor-123abc")
    b2 = esp.build({k: dict(v) for k, v in reversed(list(f.ents.items()))}, emap, b["points"])
    for pid, eid in emap.items():
        if eid != "sensor-123abc":
            assert b2["emap"][pid] == eid, f"{eid} kreeg een ander id"
    assert "aaa" in b2["emap"] and set(b2["emap"].values()) >= {"sensor-power", "switch-power"}
    assert len({*b2["emap"]}) == len(b2["emap"])


def test_rev_changes_only_when_the_description_changes():
    f = plug()
    b = built(f)
    assert built(plug())["rev"] == b["rev"]
    f.push("sensor-power", value=5.0, state="5.0 W")  # een nieuwe waarde is geen nieuwe beschrijving
    assert built(f, b["emap"], b["points"])["rev"] == b["rev"]
    f.sensor("temperature", "Temperature", 21.0, "°C")
    assert built(f, b["emap"], b["points"])["rev"] != b["rev"]


def test_decimals_are_kept_when_the_device_has_no_number_yet():
    f = FakeEsphome().sensor("current", "Current", 0.046, "A", 3)
    b = built(f)
    f.ents["sensor-current"].update(value=None, state="NA")
    assert built(f)["points"][0]["decimals"] == 1, "zonder getal en zonder voorgeschiedenis: de standaard"
    assert built(f, b["emap"], b["points"])["points"][0]["decimals"] == 3, "met voorgeschiedenis: wat het was"


def test_commands_are_absolute_and_use_esphomes_urls():
    p = {x["id"]: x for x in built()["points"]}
    ents = plug().ents
    assert esp.point_request("switch-powerplug04tv", p["powerplug04tv"], None, True) == ("/switch/powerplug04tv/turn_on", {})
    assert esp.point_request("light-red_led", p["red_led"], None, False) == ("/light/red_led/turn_off", {})
    assert esp.point_request("select-status_led_mode", p["status_led_mode"], ents["select-status_led_mode"], 2) == ("/select/status_led_mode/set", {"option": "Off"})
    assert esp.action_request("button-restart") == ("/button/restart/press", {})
    n = built(FakeEsphome().number("x", "X", 1, 0, 10, 1))["points"][0]
    assert esp.point_request("number-x", n, None, 7) == ("/number/x/set", {"value": "7"})
    with pytest.raises(ValueError):
        esp.point_request("select-status_led_mode", p["status_led_mode"], ents["select-status_led_mode"], 9)
    with pytest.raises(ValueError):
        esp.point_request("select-status_led_mode", p["status_led_mode"], None, 0)


# ------------------------------------------------------------ toevoegen en volgen


def test_add_shows_the_device_with_values_and_no_claim(tmp_path):
    async def go():
        r, hub, fake, did = await rig(tmp_path)
        assert did == DID
        s = status(hub)
        assert (s["driver"], s["online"], s["name"], s["type"], s["ha"], s["skipped"]) == ("esphome", True, "powerplug04tv", "esphome", False, [])
        assert s["host"] == "127.0.0.1" and s["rev"] and "token" not in json.dumps(s) and "auth" not in s
        by = {p["id"]: p for p in s["points"]}
        assert by["voltage"]["value"] == pytest.approx(232.1882) and by["powerplug04tv"]["value"] is True and by["status_led_mode"]["value_label"] == "Normal"
        assert by["reset_reason"]["value"] == "SW Reboot" and by["power"]["unit"] == "W"
        assert r.engine.ref_value(f"e:{DID}:power") == pytest.approx(0.685099) and r.engine.ref_value(f"e:{DID}:powerplug04tv") is True
        assert r.engine.ref_value(f"e:{DID}:reset_reason") is None, "tekst is geen bron"
        assert fake.hits == 2, "een keer om te kijken wat er is, een keer voor het volgen"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_pushed_changes_arrive_and_logs_do_not_disturb(tmp_path):
    async def go():
        r, hub, fake, did = await rig(tmp_path)
        rev = hub.devices[did]["rev"]
        fake.push("sensor-power", value=42.5, state="42.5 W")
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:power") == 42.5)
        fake._send("[D][hlw8012:083]: Got power=2.7W", "log")
        fake.push("switch-powerplug04tv", value=False, state="OFF")
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:powerplug04tv") is False)
        assert hub.devices[did]["rev"] == rev
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_changed_description_updates_the_block_and_keeps_ids(tmp_path):
    async def go():
        r, hub, fake, did = await rig(tmp_path)
        before = {p["id"]: p for p in hub.devices[did]["points"]}
        rev = hub.devices[did]["rev"]
        fake.sensor("temperature", "Temperature", 21.0, "°C")
        fake.remove("sensor-apparent_power")
        fake.drop()  # een nieuwe entiteit komt pas bij het volgende verbinden (herstart van het toestel)
        await eventually(lambda: hub.devices[did]["rev"] != rev and "temperature" in pts(hub))
        now = pts(hub)
        assert now["temperature"]["cls"] == "temperature" and now["apparent_power"].get("gone"), "een verdwenen punt blijft staan als niet beschikbaar"
        assert all(now[i]["cls"] == before[i]["cls"] for i in before), "de overige punten bleven gelijk"
        assert hub.output(f"e:{DID}:apparent_power") is None and hub.output(f"e:{DID}:temperature") == 21.0
        assert hub.devices[did]["emap"]["apparent_power"] == "sensor-apparent_power", "zijn id wordt niet aan iets anders gegeven"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_offline_when_the_stream_drops_and_back_when_it_returns(tmp_path):
    async def go():
        r, hub, fake, did = await rig(tmp_path)
        port = fake.port
        await fake.stop()
        await eventually(lambda: did not in hub.online)
        assert hub.output(f"e:{DID}:power") is None, "offline: elke waarde is onbekend, nooit de laatste gok"
        assert status(hub)["error"]
        await fake.start(port)
        await eventually(lambda: did in hub.online, 8)
        assert hub.output(f"e:{DID}:power") == pytest.approx(0.685099) and status(hub)["error"] == ""
        await shutdown(hub, fake)
    asyncio.run(go())


# ------------------------------------------------------------ bedienen


def test_set_and_do_reach_the_device_and_the_state_comes_back_over_the_stream(tmp_path):
    async def go():
        r, hub, fake, did = await rig(tmp_path)
        assert (await hub.set(did, "powerplug04tv", False))["result"] == "applied"
        await eventually(lambda: hub.output(f"e:{DID}:powerplug04tv") is False)
        await hub.set(did, "red_led", True)
        await hub.set(did, "status_led_mode", 2)
        await eventually(lambda: pts(hub) and hub.output(f"e:{DID}:status_led_mode") == 2)
        await hub.set(did, "powerplug04tv", True)
        await eventually(lambda: hub.output(f"e:{DID}:powerplug04tv") is True)
        assert [(c[1], c[2]) for c in fake.calls] == [("/switch/powerplug04tv/turn_off", {}), ("/light/red_led/turn_on", {}), ("/select/status_led_mode/set", {"option": "Off"}),
                                                       ("/switch/powerplug04tv/turn_on", {})]
        assert all(c[0] == "POST" for c in fake.calls)
        await hub.do(did, "restart", confirm=True)
        assert fake.calls[-1][1] == "/button/restart/press"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_what_must_not_be_sent_is_refused_before_it_leaves(tmp_path):
    async def go():
        r, hub, fake, did = await rig(tmp_path)
        for pid, v, word in [("voltage", 1, "niet instelbaar"), ("reset_reason", "x", "niet instelbaar"), ("nergens", 1, "onbekend"), ("powerplug04tv", 5, "aan of uit"),
                             ("status_led_mode", 7, "keuze")]:
            with pytest.raises(ModelError, match=word):
                await hub.set(did, pid, v)
        with pytest.raises(ModelError, match="Doorgaan|bevestiging"):
            await hub.do(did, "restart")  # een restart vraagt altijd om bevestiging
        with pytest.raises(ModelError, match="onbekende actie"):
            await hub.do(did, "nergens")
        assert fake.calls == []
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_command_the_device_refuses_or_cannot_get_is_reported(tmp_path):
    async def go():
        r, hub, fake, did = await rig(tmp_path)
        fake.fail_commands = 1
        with pytest.raises(ModelError, match="HTTP 500"):
            await hub.set(did, "powerplug04tv", False)
        assert hub.output(f"e:{DID}:powerplug04tv") is True, "de stand blijft wat het toestel meldt"
        fake.remove("switch-powerplug04tv")  # het toestel kent hem niet meer (404)
        with pytest.raises(ModelError, match="kent dit niet"):
            await hub.set(did, "powerplug04tv", False)
        await fake.stop()
        await eventually(lambda: did not in hub.online)
        with pytest.raises(ModelError, match="geen verbinding"):
            await hub.set(did, "red_led", True)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_an_input_drives_a_switch_only_on_a_change(tmp_path):
    async def go():
        r, hub, fake, did = await rig(tmp_path)
        hoog = r.model.add_module("above", "Spanning hoog", 235)
        r.model.set_module(hoog, inputs={"a": f"e:{did}:voltage"})
        hub.update(did, {"inputs": {"powerplug04tv": f"m:{hoog}"}})
        await eventually(lambda: r.engine.ref_value(f"m:{hoog}") is False)  # 232 V is niet hoger dan 235 V
        hub.tick()
        await asyncio.sleep(0.1)
        assert fake.calls == [], "de eerste stand wordt alleen onthouden: een herstart schakelt niets"
        fake.push("sensor-voltage", value=240.0, state="240.0 V")
        await eventually(lambda: r.engine.ref_value(f"m:{hoog}") is True)
        hub.tick()
        await eventually(lambda: hub.output(f"e:{DID}:powerplug04tv") is True and len(fake.calls) == 1)
        assert fake.calls[0][1] == "/switch/powerplug04tv/turn_on", "absoluut: aan, geen toggle"
        for _ in range(3):
            hub.tick()
        await asyncio.sleep(0.1)
        assert len(fake.calls) == 1, "een ongewijzigde ingang stuurt niets opnieuw"
        await shutdown(hub, fake)
    asyncio.run(go())


# ------------------------------------------------------------ Home Assistant


def test_ha_entities_are_off_by_default_and_exact_when_wanted(tmp_path):
    async def go():
        r, hub, fake, did = await rig(tmp_path)
        assert hub.discovery() == {} and hub.states() == {}, "het toestel staat al in HA via zijn eigen integratie: geen dubbele entiteiten tenzij je dat wilt"
        hub.update(did, {"ha": True})
        disc = hub.discovery()
        base = "kk_esp_" + DID
        power = next(v for k, v in disc.items() if k.endswith(f"/sensor/{base}_power/config"))
        assert (power["unit_of_measurement"], power["device_class"], power["state_class"], power["suggested_display_precision"]) == ("W", "power", "measurement", 1)
        energy = next(v for k, v in disc.items() if k.endswith(f"/sensor/{base}_energy/config"))
        assert (energy["device_class"], energy["state_class"]) == ("energy", "total_increasing")
        assert any(k.endswith(f"/switch/{base}_powerplug04tv/config") for k in disc) and any(k.endswith(f"/select/{base}_status_led_mode/config") for k in disc)
        assert not any("restart" in k for k in disc), "een restart is gevaarlijk: geen knop in HA"
        assert next(iter(disc.values()))["device"]["manufacturer"] == "ESPHome"
        assert hub.states()[hub.t(did, "power")][0] == "0.7"
        hub.update(did, {"ha": False})
        assert hub.discovery() == {}
        assert any(f"/sensor/{base}_power/config" in t for t in hub.retire()), "wat al in HA stond, wordt gewist"
        with pytest.raises(ModelError):
            hub.update(did, {"ha": "ja"})
        await shutdown(hub, fake)
    asyncio.run(go())


# ------------------------------------------------------------ opslag, schema en foutmeldingen


def test_it_survives_a_restart_with_its_ids_and_classes(tmp_path):
    async def go():
        r, hub, fake, did = await rig(tmp_path)
        hub.update(did, {"ha": True, "shown": ["power", "voltage"]})
        emap, rev = dict(hub.devices[did]["emap"]), hub.devices[did]["rev"]
        hub.close()
        hub2 = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        d2 = hub2.devices[did]
        assert (d2["driver"], d2["emap"], d2["rev"], d2["ha"], d2["shown"]) == ("esphome", emap, rev, True, ["voltage", "power"])
        assert {p["id"]: p["cls"] for p in d2["points"]}["power"] == "power", "de klasse voor HA gaat niet verloren bij een herstart"
        hub2.start()
        await eventually(lambda: did in hub2.online)
        assert hub2.devices[did]["rev"] == rev and hub2.output(f"e:{DID}:voltage") == pytest.approx(232.1882)
        raw = (tmp_path / "espdev.json").read_text(encoding="utf-8")
        assert json.loads(raw)["devices"][did]["emap"] == emap
        await shutdown(hub2, fake)
    asyncio.run(go())


def test_the_block_on_the_schema(tmp_path):
    async def go():
        r, hub, fake, did = await rig(tmp_path)
        hub.update(did, {"shown": ["power", "powerplug04tv", "status_led_mode"]})
        b = next(x for x in schema.view(r.model, r.engine.snapshot())["blocks"] if x["key"] == f"e:{DID}")
        assert (b["kind"], b["tech"], b["driver"], b["online"]) == ("espdev", "ESPHome · webserver", "esphome", True)
        assert [o["name"] for o in b["outs"]] == ["power", "powerplug04tv", "status_led_mode"]
        assert [p["name"] for p in b["ports"]] == ["powerplug04tv", "status_led_mode"], "alleen instelbare punten hebben een ingang"
        assert next(o for o in b["outs"] if o["name"] == "status_led_mode")["text"] == "Normal"
        assert "reset_reason" not in {o["name"] for o in b["all"]} and "uptime" not in {o["name"] for o in b["all"]}
        await shutdown(hub, fake)
    asyncio.run(go())


def test_remove_forgets_everything_and_asks_nothing_of_the_device(tmp_path):
    async def go():
        r, hub, fake, did = await rig(tmp_path)
        hub.update(did, {"ha": True})
        n = len(fake.calls)
        assert await hub.remove(did)
        assert did not in hub.devices and did not in hub.es and fake.calls[n:] == [], "er is niets te claimen en dus niets in te trekken"
        assert (tmp_path / "espdev.json").exists() and json.loads((tmp_path / "espdev.json").read_text(encoding="utf-8"))["devices"] == {}
        assert hub.retire(), "zijn entiteiten in HA moeten weg"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_add_errors_say_what_is_wrong(tmp_path):
    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        for bad in ("", "http://x", "a b", "x/y"):
            with pytest.raises(ModelError, match="ongeldig adres"):
                await hub.add_esphome(bad)
        with pytest.raises(ModelError, match="poort"):
            await hub.add_esphome("127.0.0.1", 0)
        with pytest.raises(ModelError, match="geen verbinding"):
            await hub.add_esphome("127.0.0.1", 9)  # er luistert niets
        locked = await FakeEsphome(auth=("jan", "geheim")).sensor("a", "A", 1.0).start()
        with pytest.raises(ModelError, match="gebruikersnaam en wachtwoord"):
            await hub.add_esphome("127.0.0.1", locked.port)
        did = await hub.add_esphome("127.0.0.1", locked.port, user="jan", password="geheim")  # met het wachtwoord lukt het
        assert hub.devices[did]["auth"] == "jan:geheim" and "geheim" not in json.dumps(hub.status())
        assert oct((tmp_path / "espdev.json").stat().st_mode & 0o777) in ("0o600", "0o666"), "het wachtwoord staat alleen in een bestand voor de add-on (op Windows bestaan geen modi)"
        with pytest.raises(ModelError, match="staat er al"):
            await hub.add_esphome("127.0.0.1", locked.port, user="jan", password="geheim")
        empty = await FakeEsphome(title="leeg").start()
        with pytest.raises(ModelError, match="geen entiteiten|niets wat we kennen"):
            await hub.add_esphome("127.0.0.1", empty.port)
        weird = await FakeEsphome(title="vreemd").add("cover-x", "Rolluik", state="OPEN").start()
        with pytest.raises(ModelError, match="niets wat we kennen"):
            await hub.add_esphome("127.0.0.1", weird.port)
        hub.close()
        for f in (locked, empty, weird):
            await f.stop()
    asyncio.run(go())


def test_the_inventory_lists_it(tmp_path):
    async def go():
        from kkaddon import inventory
        r, hub, fake, did = await rig(tmp_path)
        row = next(x for x in inventory.build(r.engine, None) if x["key"] == f"e{did}")
        assert (row["name"], row["kind"], row["protocol"], row["ip"], row["online"], row["note"]) == ("powerplug04tv", "ESPHome-apparaat", "ESPHome (webserver)", "127.0.0.1", True, "")
        await shutdown(hub, fake)
    asyncio.run(go())
