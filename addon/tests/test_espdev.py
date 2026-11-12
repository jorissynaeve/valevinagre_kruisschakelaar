"""Zelfbouwapparaten met HUISDEV: lezen en controleren van wat een apparaat meldt, ontdekking over echte UDP, claimen, de state-poll over echte HTTP tegen een
nagebootst apparaat (tests/fakehuisdev.py, gebouwd naar de spec), uitgangen en ingangen in de logica, het blok op het schema, dynamische punten, de entiteiten
in HA met opruimen na een herstart, de web-API en de versiestanden."""
import asyncio
import json
import os
import socket
import stat
import time

import pytest

from kkaddon import espdevhub as eh
from kkaddon import inventory, model as kkmodel, schema
from kkaddon.espdevhub import EspDevHub, check_value, clean_descriptor, parse_discovery, proto_state
from kkaddon.model import ModelError
from tests.fakehuisdev import FakeEsp, pool
from tests.rig import KEY, Rig


async def eventually(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("duurde te lang")


async def rig(tmp_path, esp=None, claim=True, monkeypatch=None):
    if monkeypatch:
        monkeypatch.setattr(eh, "TICK", 3600)  # de tests tikken zelf
    r = Rig(tmp_path)
    hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
    esp = esp or pool()
    await esp.start()
    hub.start()
    await eventually(lambda: hub._ask and hub._listen)
    hub.ask("127.0.0.1", esp.udp_port)
    await eventually(lambda: esp.hid in hub.found)
    did = None
    if claim:
        did = await hub.claim(esp.hid)
        await eventually(lambda: did in hub.online)
    return r, hub, esp, did


async def shutdown(hub, *esps):
    hub.close()
    for e in esps:
        await e.stop()
    await asyncio.sleep(0.05)


def block(r, key):
    return next(x for x in schema.view(r.model, r.engine.snapshot())["blocks"] if x["key"] == key)


# ------------------------------------------------------------ lezen en controleren (pure functies)


def test_discovery_message_is_understood_and_noise_is_not():
    ok = parse_discovery(json.dumps(pool().discovery() | {"port": 80}).encode(), ("192.168.8.40", 4000))
    assert ok == {"hid": "pool-a1b2c3", "ip": "192.168.8.40", "port": 80, "proto": "1.0", "type": "poolcontroller", "name": "Zwembad", "fw": "0.3.1", "rev": ok["rev"], "claimed": False}
    good = {"huisdev": "1.0", "id": "x-1", "port": 80}
    for bad in (b"", b"HUISDEV?1.0", b"nonsense", b"[]", b"\xff\xfe", json.dumps({**good, "id": "Hoofdletter"}).encode(), json.dumps({**good, "id": "a_b"}).encode(),
                json.dumps({**good, "huisdev": "x"}).encode(), json.dumps({**good, "port": 0}).encode(), json.dumps({**good, "port": True}).encode(), json.dumps({"id": "x-1"}).encode()):
        assert parse_discovery(bad, ("1.2.3.4", 1)) is None, bad


def test_descriptor_is_cleaned_and_unknown_things_are_skipped():
    raw = pool().descriptor()
    raw["points"] += [{"id": "future", "label": "x", "kind": "complex"}, {"id": "Bad Id", "label": "x", "kind": "int"}, {"id": "ph", "label": "dubbel", "kind": "int"},
                      {"id": "keuze", "label": "leeg", "kind": "enum"}]  # een minor-versie mag een kind toevoegen; een enum zonder keuzes is niet te gebruiken
    raw["actions"].append({"id": "vreemd", "label": "x", "args": [{"id": "a", "label": "a", "kind": "complex"}]})
    raw["extra"] = "wordt genegeerd"
    d = clean_descriptor(raw)
    assert [p["id"] for p in d["points"]] == ["ph", "temp", "orp", "pump", "mode", "ph_set", "alarm_no_flow", "rssi", "note", "factory"]
    assert [a["id"] for a in d["actions"]] == ["boost", "refresh", "reset"], "een actie met een argument dat we niet begrijpen kunnen we niet veilig uitvoeren"
    ph = d["points"][0]
    assert (ph["decimals"], ph["cls"], ph["state"], ph["stale"], ph["writable"]) == (2, "ph", "measurement", 120000, False)
    assert d["points"][-1]["danger"] and d["points"][-1]["confirm"] == "Alles wissen?"
    assert d["actions"][0]["args"][0]["step"] == 5 and d["actions"][2]["danger"]
    for bad in (None, [], {}, {**raw, "rev": ""}, {**raw, "rev": "x" * 17}, {**raw, "proto": "een"}, {**raw, "device": {}}, {**raw, "device": {"id": "Fout"}}):
        assert clean_descriptor(bad) is None


def test_descriptor_is_capped():
    raw = pool().descriptor()
    raw["points"] = [{"id": f"p{i}", "label": "p", "kind": "int"} for i in range(200)]
    raw["actions"] = [{"id": f"a{i}", "label": "a"} for i in range(50)]
    d = clean_descriptor(raw)
    assert len(d["points"]) == eh.MAX_POINTS and len(d["actions"]) == eh.MAX_ACTIONS


def test_values_are_checked_against_type_options_range_and_step():
    d = clean_descriptor(pool().descriptor())
    p = {x["id"]: x for x in d["points"]}
    assert check_value(p["pump"], True) is True and check_value(p["pump"], "ON") is True and check_value(p["pump"], "off") is False
    assert check_value(p["mode"], 1) == 1 and check_value(p["mode"], "2") == 2
    assert check_value(p["ph_set"], 7.4) == 7.4 and check_value(p["ph_set"], "7.0") == 7.0
    assert check_value(p["note"], "tekst") == "tekst"
    for pid, v, word in [("pump", 3, "aan of uit"), ("mode", 5, "keuze"), ("mode", 1.5, "geheel"), ("ph_set", 9, "tussen"), ("ph_set", 7.25, "stappen"), ("ph_set", "veel", "getal"),
                         ("ph_set", True, "getal"), ("ph_set", float("nan"), "getal"), ("note", 5, "tekst"), ("note", "x" * 201, "tekst")]:
        with pytest.raises(ModelError, match=word):
            check_value(p[pid], v)


def test_versions(monkeypatch):
    assert [proto_state(x) for x in ("1.0", "1.7", "2.0", "0.9", "x", None)] == ["ok", "ok", "unsupported", "unsupported", "unsupported", "unsupported"]
    monkeypatch.setattr(eh, "CURRENT", (1, 2))
    assert [proto_state(x) for x in ("1.0", "1.1", "1.2", "1.3")] == ["upgrade", "upgrade", "ok", "ok"], "een lagere minor: upgrade beschikbaar; een nieuwere minor lezen we gewoon"


# ------------------------------------------------------------ ontdekking en claimen


def test_a_query_is_answered_and_an_announcement_is_heard(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, _ = await rig(tmp_path, claim=False, monkeypatch=monkeypatch)
        try:
            assert b"HUISDEV?1.0" in esp.udp_seen, "de vraag van de add-on"
            f = hub.found_view()
            assert f == [{"hid": "pool-a1b2c3", "type": "poolcontroller", "name": "Zwembad", "ip": "127.0.0.1", "port": esp.http_port, "fw": "0.3.1", "proto": "1.0", "proto_state": "ok",
                          "claimed": False, "claimable": True}]
            esp.name = "Nieuw"
            esp.announce(hub.udp_port)  # aankondiging, zonder dat er gevraagd is
            await eventually(lambda: hub.found["pool-a1b2c3"]["name"] == "Nieuw")
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for junk in (b"\x00\x01", b"{}", b'{"huisdev":"1.0"}', b"HUISDEV?1.0"):
                s.sendto(junk, ("127.0.0.1", hub.udp_port))
            s.close()
            await asyncio.sleep(0.2)
            assert len(hub.found) == 1, "ruis maakt geen apparaat"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_claim_makes_a_block_with_a_token_the_device_keeps(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            assert did == "pool_a1b2c3" and len(esp.token) == 64 and hub.devices[did]["token"] == esp.token
            assert hub.found_view() == [], "geclaimd: weg uit de lijst"
            if os.name != "nt":
                assert stat.S_IMODE(os.stat(tmp_path / "espdev.json").st_mode) == 0o600
            assert esp.token not in json.dumps(hub.status()) and esp.token not in json.dumps(r.engine.snapshot()), "nooit het token naar de pagina"
            assert esp.token not in json.dumps(r.model.to_dict()), "en niet in het model dat meegaat in de export"
            assert any(c[1] == "/huisdev/claim" for c in esp.calls) and all(c[3] == esp.token for c in esp.calls if c[1] == "/huisdev/state"), "de state vraagt het token"
            with pytest.raises(ModelError, match="staat er al"):
                await hub.claim(esp.hid)
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_claim_is_refused_for_every_wrong_case(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(eh, "TICK", 3600)
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        other = FakeEsp(hid="al-geclaimd", claimed_by_other=True)
        newer = FakeEsp(hid="te-nieuw", proto="2.0")
        gone = pool()
        for e in (other, newer, gone):
            await e.start()
        hub.start()
        await eventually(lambda: hub._ask)
        try:
            with pytest.raises(ModelError, match="niet .meer. gevonden"):
                await hub.claim("onbekend")
            for e in (other, newer, gone):
                hub.ask("127.0.0.1", e.udp_port)
            await eventually(lambda: len(hub.found) == 3)
            view = {f["hid"]: f for f in hub.found_view()}
            assert view["al-geclaimd"]["claimed"] and not view["al-geclaimd"]["claimable"], "door iets anders geclaimd: zichtbaar, niet te koppelen"
            assert view["te-nieuw"]["proto_state"] == "unsupported" and not view["te-nieuw"]["claimable"]
            with pytest.raises(ModelError, match="al geclaimd"):
                await hub.claim("al-geclaimd")
            with pytest.raises(ModelError, match="niet ondersteund"):
                await hub.claim("te-nieuw")
            assert newer.token == "" and not any(c[1] == "/huisdev/claim" for c in newer.calls), "een onbekende major wordt niet eens geclaimd"
            await gone.stop_http()
            with pytest.raises(ModelError, match="geen verbinding"):
                await hub.claim(gone.hid)
            assert gone.token == "" and not hub.devices
        finally:
            hub.close()
            for e in (other, newer, gone):
                await e.stop()
    asyncio.run(go())


def test_claim_refuses_a_device_that_answers_as_another(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, _ = await rig(tmp_path, claim=False, monkeypatch=monkeypatch)
        try:
            esp.hid = "andere-123456"  # op dit adres zit een ander apparaat dan het gevonden is
            with pytest.raises(ModelError, match="geldige beschrijving"):
                await hub.claim("pool-a1b2c3")
            assert not hub.devices
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


# ------------------------------------------------------------ de stand en de uitgangen


def test_state_gives_typed_outputs_and_unknown_is_none(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            out = lambda p: hub.output(f"e:{did}:{p}")  # noqa: E731
            assert (out("ph"), out("temp"), out("orp"), out("pump"), out("mode"), out("alarm_no_flow")) == (7.2, 24.5, 740, False, 2, False)
            assert isinstance(out("orp"), int) and isinstance(out("ph"), float) and out("pump") is False
            assert out("note") is None, "tekst is geen bron voor de logica"
            assert hub.output("e:weg:ph") is None and out("onzin") is None and hub.output("w:x:on") is None and hub.output("onzin") is None
            esp.set_value("temp", None)
            await eventually(lambda: out("temp") is None)
            esp.set_value("temp", 20)  # een int voor een float: gewoon een getal
            await eventually(lambda: out("temp") == 20.0)
            esp.set_value("ph", 7.0, age=130000)  # ouder dan `stale` (120 s): onbekend
            await eventually(lambda: out("ph") is None)
            esp.set_value("ph", 6.9, age=1000)
            await eventually(lambda: out("ph") == 6.9)
            esp.points["pump"]["v"] = "aan"  # een waarde van het verkeerde type is onbekend, geen gok
            await eventually(lambda: out("pump") is None)
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_offline_after_three_misses_and_back(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            hub._stop(did)  # zelf tellen
            hub._miss(did, "stil")
            hub._miss(did, "stil")
            assert did in hub.online and hub.output(f"e:{did}:ph") == 7.2, "twee keer stil is nog geen storing"
            hub._miss(did, "stil")
            assert did not in hub.online and hub.output(f"e:{did}:ph") is None, "offline: onbekend, nooit 0"
            hub._start(did)
            await eventually(lambda: did in hub.online)
            port = esp.http_port
            await esp.stop_http()
            await eventually(lambda: did not in hub.online)
            assert "geen verbinding" in hub.status()[0]["error"] and hub.states()[hub.t(did, "online")] == ("offline", True)
            assert hub.output(f"e:{did}:ph") is None
            await esp.start(http_port=port)
            await eventually(lambda: did in hub.online)
            assert hub.output(f"e:{did}:ph") == 7.2 and hub.status()[0]["error"] == ""
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_refused_token_is_a_clear_error(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            esp.token = "een-ander-token-dat-niet-klopt-1234"
            await eventually(lambda: did not in hub.online)
            assert "token" in hub.status()[0]["error"] and "wis de claim" in hub.status()[0]["error"]
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


# ------------------------------------------------------------ bedienen


def test_set_and_do_are_checked_here_and_on_the_device(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            assert (await hub.set(did, "pump", True))["result"] == "applied" and esp.points["pump"]["v"] is True
            await eventually(lambda: hub.output(f"e:{did}:pump") is True)  # de stand komt uit het antwoord van het apparaat
            await hub.set(did, "ph_set", 7.4)
            await hub.set(did, "mode", 1)
            assert (esp.points["ph_set"]["v"], esp.points["mode"]["v"]) == (7.4, 1)
            before = len(esp.calls)
            for pid, v, word in [("ph_set", 9, "tussen"), ("ph_set", 7.25, "stappen"), ("mode", 5, "keuze"), ("ph", 7, "niet instelbaar"), ("onzin", 1, "onbekend punt"), ("pump", 5, "aan of uit")]:
                with pytest.raises(ModelError, match=word):
                    await hub.set(did, pid, v)
            assert len(esp.calls) == before, "een foute waarde gaat niet eens naar het apparaat"
            with pytest.raises(ModelError, match="onbekend apparaat"):
                await hub.set("weg", "pump", True)
            esp.queue_sets = True
            assert (await hub.set(did, "pump", False))["result"] == "queued" and esp.points["pump"]["v"] is True, "queued: de waarde verandert pas als het apparaat het meldt"
            esp.queue_sets = False
            esp.reject = "pomp draait niet"
            with pytest.raises(ModelError, match="pomp draait niet"):
                await hub.set(did, "pump", False)
            esp.reject = None
            # gevaarlijk: alleen met bevestiging, en dan met confirm naar het apparaat
            with pytest.raises(ModelError, match="Alles wissen"):
                await hub.set(did, "factory", True)
            assert not any(c[2].get("id") == "factory" for c in esp.calls)
            await hub.set(did, "factory", True, confirm=True)
            assert esp.points["factory"]["v"] is True and [c for c in esp.calls if c[2].get("id") == "factory"][-1][2]["confirm"] is True
            # acties
            await hub.do(did, "boost", {"minutes": 30})
            assert [c for c in esp.calls if c[1] == "/huisdev/do"][-1][2] == {"id": "boost", "args": {"minutes": 30}}
            for a, args, word in [("boost", {}, "geen waarde"), ("boost", {"minutes": 7}, "stappen"), ("boost", {"minutes": 500}, "tussen"), ("weg", {}, "onbekende actie")]:
                with pytest.raises(ModelError, match=word):
                    await hub.do(did, a, args)
            with pytest.raises(ModelError, match="Doorgaan"):
                await hub.do(did, "reset")
            await hub.do(did, "reset", confirm=True)
            assert [c for c in esp.calls if c[1] == "/huisdev/do"][-1][2]["confirm"] is True
            esp.disabled["refresh"] = "bezig"
            await eventually(lambda: hub.status()[0]["actions"][1]["disabled"])
            with pytest.raises(ModelError, match="kan nu niet: bezig"):
                await hub.do(did, "refresh")
            del esp.disabled["refresh"]
            await eventually(lambda: not hub.status()[0]["actions"][1]["disabled"])
            await hub.do(did, "refresh")
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_errors_of_the_device_are_translated(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            esp.points["ph_set"]["def"]["max"] = 7.0  # het apparaat denkt er anders over dan wat wij nog wisten
            with pytest.raises(ModelError, match="buiten het bereik"):
                await hub.set(did, "ph_set", 7.4)
            esp.token = "gewijzigd-gewijzigd-gewijzigd"
            with pytest.raises(ModelError, match="token"):
                await hub.set(did, "pump", True)
            await esp.stop_http()
            hub.online.add(did)
            with pytest.raises(ModelError, match="geen verbinding"):
                await hub.set(did, "pump", True)
            hub.online.discard(did)
            with pytest.raises(ModelError, match="geen verbinding met het apparaat"):
                await hub.set(did, "pump", True)
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


# ------------------------------------------------------------ het blok, de logica en de ingangen


def test_block_has_a_row_per_shown_point_with_inputs_only_where_allowed(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            b = block(r, f"e:{did}")
            assert b["kind"] == "espdev" and b["online"] and b["rows"] == 8 and b["tech"] == "poolcontroller · HUISDEV"
            assert [o["name"] for o in b["outs"]] == ["ph", "temp", "orp", "pump", "mode", "ph_set", "alarm_no_flow", "factory"], "de eerste acht, zonder tekst en diagnose"
            assert [(p["name"], p["row"], p["type"]) for p in b["ports"]] == [("pump", 3, "bool"), ("mode", 4, "number"), ("ph_set", 5, "number")], "alleen instelbaar en niet gevaarlijk"
            ph = b["outs"][0]
            assert (ph["ref"], ph["value"], ph["type"], ph["row"]) == (f"e:{did}:ph", 7.2, "number", 0)
            assert next(o for o in b["outs"] if o["name"] == "mode")["text"] == "Auto", "een keuze toont zijn label"
            assert {o["name"] for o in b["all"]} == {"ph", "temp", "orp", "pump", "mode", "ph_set", "alarm_no_flow", "factory"}, "alles dat een bron kan zijn"
            hub.update(did, {"shown": ["temp", "pump"]})
            b = block(r, f"e:{did}")
            assert [o["name"] for o in b["outs"]] == ["temp", "pump"] and [(p["name"], p["row"]) for p in b["ports"]] == [("pump", 1)] and b["rows"] == 2
            for bad in (["onzin"], ["note"], ["rssi"], "ph", [1]):
                with pytest.raises(ModelError):
                    hub.update(did, {"shown": bad})
            hub.update(did, {"shown": []})
            assert block(r, f"e:{did}")["rows"] == 1
            hub.update(did, {"name": "Zwembad achter"})
            assert block(r, f"e:{did}")["name"] == "Zwembad achter"
            with pytest.raises(ModelError, match="naam"):
                hub.update(did, {"name": " "})
            assert f"e:{did}" in schema.existing_blocks(r.model, r.engine.snapshot())
            assert json.dumps(r.engine.snapshot()["espdev"]) and json.dumps(schema.view(r.model, r.engine.snapshot()))
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_outputs_feed_the_logic_and_types_are_checked(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            hoog = r.model.add_module("above", "pH hoog", 7.0)
            r.model.set_module(hoog, inputs={"a": f"e:{did}:ph"})
            alarm = r.model.add_module("and", "Alarm en pomp")
            r.model.set_module(alarm, inputs={"a": f"e:{did}:alarm_no_flow", "b": f"e:{did}:pump"})
            v = r.engine.module_values()
            assert v[hoog] is True and v[alarm] is False
            assert r.engine.ref_value(f"e:{did}:orp") == 740
            esp.set_value("alarm_no_flow", True)
            esp.set_value("pump", True)
            await eventually(lambda: r.engine.module_values()[alarm] is True)
            with pytest.raises(ModelError, match="een 0 of 1"):
                r.model.set_module(alarm, inputs={"a": f"e:{did}:ph"})  # een getal is geen 0/1
            with pytest.raises(ModelError, match="een getal"):
                r.model.set_module(hoog, inputs={"a": f"e:{did}:pump"})  # een 0/1 is geen getal
            r.model.set_module(hoog, inputs={"a": f"e:{did}:mode"})  # een keuze is een getal
            assert kkmodel.source_type(f"e:{did}:ph", {}, {}) == "number" and kkmodel.source_type(f"e:{did}:pump", {}, {}) == "bool"
            # een apparaat of punt dat niet bestaat: onbekend, nooit een fout
            r.model.set_module(alarm, inputs={"a": "e:weg:iets", "b": f"e:{did}:nieuw_punt"})
            assert r.engine.module_values()[alarm] is None
            hub._stop(did)
            hub.online.discard(did)
            assert r.engine.module_values()[hoog] is None, "offline: onbekend, nooit 0"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_ref_to_a_removed_device_never_breaks_the_model(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            m1 = r.model.add_module("above", "pH hoog", 7.0)
            r.model.set_module(m1, inputs={"a": f"e:{did}:ph"})
            m2 = r.model.add_module("and", "Een")
            r.model.set_module(m2, inputs={"a": f"e:{did}:pump"})
            assert await hub.remove(did)
            r.model.add_module("or", "Nog een")  # elke wijziging controleert alle modules: de verdwenen bron mag dat niet breken
            r.model.set_module(m2, name="Hernoemd")
            assert r.engine.module_values()[m1] is None and r.engine.module_values()[m2] is None
            model2 = kkmodel.Model(None)
            model2.load_dict(r.model.to_dict(), save=False)
            assert m1 in model2.modules, "ook opnieuw laden (herstart zonder hub) gaat goed"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_hook_is_released_on_close(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        assert kkmodel.esp_type is not None
        await shutdown(hub, esp)
        assert kkmodel.esp_type is None
    asyncio.run(go())


def wire(r, hub, did, pid, src):
    hub.update(did, {"inputs": {pid: src}})


def test_inputs_act_on_a_change_only_and_never_on_the_first_value(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            sets = lambda pid: [c for c in esp.calls if c[1] == "/huisdev/set" and c[2].get("id") == pid]  # noqa: E731
            hoog = r.model.add_module("above", "pH hoog", 7.5)
            r.model.set_module(hoog, inputs={"a": f"e:{did}:ph"})
            wire(r, hub, did, "pump", f"m:{hoog}")
            hub.tick()
            hub.tick()
            await asyncio.sleep(0.1)
            assert not sets("pump") and hub.seen[did]["pump"] is False, "de eerste stand wordt alleen onthouden: een herstart schakelt niets"
            esp.set_value("ph", 7.9)
            await eventually(lambda: hub.output(f"e:{did}:ph") == 7.9)
            hub.tick()
            await eventually(lambda: esp.points["pump"]["v"] is True)
            assert len(sets("pump")) == 1 and sets("pump")[0][2]["value"] is True, "absoluut: aan, geen toggle"
            assert hub.last[did]["pump"].startswith("Pomp → 1"), hub.last
            for _ in range(3):
                hub.tick()
            await asyncio.sleep(0.1)
            assert len(sets("pump")) == 1, "een ongewijzigde ingang stuurt niets opnieuw"
            esp.set_value("pump", False)  # iemand zette de pomp met de hand uit: de ingang wint pas als hij weer wisselt
            await eventually(lambda: hub.output(f"e:{did}:pump") is False)
            hub.tick()
            await asyncio.sleep(0.1)
            assert len(sets("pump")) == 1
            esp.set_value("ph", 7.0)
            await eventually(lambda: hub.output(f"e:{did}:ph") == 7.0)
            hub.tick()
            await eventually(lambda: len(sets("pump")) == 2 and sets("pump")[1][2]["value"] is False)
            # na onbekend telt de volgende stand weer als eerste stand
            esp.set_value("ph", None)
            await eventually(lambda: hub.output(f"e:{did}:ph") is None)
            hub.tick()
            assert "pump" not in hub.seen[did]
            esp.set_value("ph", 7.9)
            await eventually(lambda: hub.output(f"e:{did}:ph") == 7.9)
            hub.tick()
            await asyncio.sleep(0.1)
            assert len(sets("pump")) == 2 and hub.seen[did]["pump"] is True, "alleen onthouden"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_number_input_is_snapped_to_the_range_and_step(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            esp.point("level", "Niveau", "int", 50, min=0, max=100, step=5, writable=True)
            await eventually(lambda: hub.point_type(f"e:{did}:level") == "number")
            n = r.model.add_module("number", "Doel", 30)
            wire(r, hub, did, "level", f"m:{n}")
            hub.tick()
            assert hub.seen[did]["level"] == 30
            r.model.set_module(n, value=34)
            hub.tick()
            await eventually(lambda: esp.points["level"]["v"] == 35)
            await eventually(lambda: not hub._inflight)
            r.model.set_module(n, value=150)
            hub.tick()
            await eventually(lambda: esp.points["level"]["v"] == 100)
            await eventually(lambda: not hub._inflight)
            m = r.model.add_module("number", "Modus", 2)
            wire(r, hub, did, "mode", f"m:{m}")
            hub.tick()
            r.model.set_module(m, value=1)
            hub.tick()
            await eventually(lambda: esp.points["mode"]["v"] == 1)
            await eventually(lambda: not hub._inflight)
            r.model.set_module(m, value=9)
            hub.tick()
            await eventually(lambda: "geen keuze" in hub.last[did].get("mode", ""))
            assert esp.points["mode"]["v"] == 1, "geen toegestane keuze: niets verstuurd"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_input_waits_while_offline_and_retries_after_a_failure(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            monkeypatch.setattr(eh, "RETRY", 0.2)
            sets = lambda: [c for c in esp.calls if c[1] == "/huisdev/set" and c[2].get("id") == "pump"]  # noqa: E731
            n = r.model.add_module("number", "Drempel", 10)  # een bron die niet van het apparaat zelf komt: die zou offline ook onbekend zijn
            hoog = r.model.add_module("above", "Hoog", 20)
            r.model.set_module(hoog, inputs={"a": f"m:{n}"})
            wire(r, hub, did, "pump", f"m:{hoog}")
            hub.tick()
            assert hub.seen[did]["pump"] is False
            port = esp.http_port
            await esp.stop_http()
            await eventually(lambda: did not in hub.online)
            r.model.set_module(n, value=30)  # de ingang wordt 1 terwijl het apparaat weg is
            hub.tick()
            assert not hub._inflight and (did, "pump") not in hub._failed, "offline: wachten, geen mislukking"
            await esp.start(http_port=port)
            await eventually(lambda: did in hub.online)
            esp.reject = "pomp draait niet"
            hub.tick()
            await eventually(lambda: (did, "pump") in hub._failed)
            assert "niet verstuurd" in hub.last[did]["pump"] and "pomp draait niet" in hub.last[did]["pump"]
            n = len(sets())
            hub.tick()
            await asyncio.sleep(0.05)
            assert len(sets()) == n, "na een mislukking wachten"
            esp.reject = None
            await asyncio.sleep(0.25)
            hub.tick()
            await eventually(lambda: esp.points["pump"]["v"] is True)
            assert (did, "pump") not in hub._failed
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_inputs_are_checked_when_wired(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            hoog = r.model.add_module("above", "pH hoog", 7.0)
            r.model.set_module(hoog, inputs={"a": f"e:{did}:ph"})
            with pytest.raises(ModelError, match="niet te koppelen"):
                hub.update(did, {"inputs": {"factory": f"m:{hoog}"}})  # gevaarlijk: nooit aan de logica
            with pytest.raises(ModelError, match="niet te koppelen"):
                hub.update(did, {"inputs": {"ph": f"m:{hoog}"}})  # niet instelbaar
            with pytest.raises(ModelError, match="niet te koppelen"):
                hub.update(did, {"inputs": {"note": f"m:{hoog}"}})
            with pytest.raises(ModelError, match="een 0 of 1"):
                hub.update(did, {"inputs": {"pump": f"e:{did}:orp"}})  # een getal op een 0/1
            with pytest.raises(ModelError, match="een getal"):
                hub.update(did, {"inputs": {"ph_set": f"m:{hoog}"}})
            with pytest.raises(ModelError, match="niet bestaat"):
                hub.update(did, {"inputs": {"pump": "m:bestaat_niet"}})
            with pytest.raises(ModelError, match="zichzelf"):
                hub.update(did, {"inputs": {"pump": f"e:{did}:alarm_no_flow"}})
            with pytest.raises(ModelError, match="ongeldige bron"):
                hub.update(did, {"inputs": {"pump": 5}})
            hub.update(did, {"inputs": {"pump": f"m:{hoog}"}})
            assert block(r, f"e:{did}")["ports"][0]["ref"] == f"m:{hoog}"
            hub.update(did, {"inputs": {"pump": None}})
            assert hub.devices[did]["inputs"] == {}
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_an_output_of_one_device_can_drive_an_input_of_another(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        other = FakeEsp(hid="tuin-111111", type="tuin").point("wet", "Nat", "bool", False).point("valve", "Ventiel", "bool", False, writable=True)
        await other.start()
        try:
            hub.ask("127.0.0.1", other.udp_port)
            await eventually(lambda: other.hid in hub.found)
            d2 = await hub.claim(other.hid)
            await eventually(lambda: d2 in hub.online)
            hub.update(d2, {"inputs": {"valve": f"e:{did}:alarm_no_flow"}})
            hub.tick()
            esp.set_value("alarm_no_flow", True)
            await eventually(lambda: hub.output(f"e:{did}:alarm_no_flow") is True)
            hub.tick()
            await eventually(lambda: other.points["valve"]["v"] is True)
        finally:
            await shutdown(hub, esp, other)
    asyncio.run(go())


# ------------------------------------------------------------ punten die komen en gaan


def test_a_new_point_appears_when_the_rev_changes(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            rev = esp.rev
            assert hub.devices[did]["rev"] == rev
            esp.point("salt", "Zout", "float", 3.2, unit="g/L", decimals=1)
            assert esp.rev != rev
            await eventually(lambda: any(p["id"] == "salt" for p in hub.devices[did]["points"]))
            assert hub.devices[did]["rev"] == esp.rev
            await eventually(lambda: hub.output(f"e:{did}:salt") == 3.2)
            assert "salt" not in [o["name"] for o in block(r, f"e:{did}")["outs"]], "de eerste acht blijven staan"
            assert "salt" in [o["name"] for o in block(r, f"e:{did}")["all"]], "wel kiesbaar"
            assert f"homeassistant/sensor/kk_esp_{did}_salt/config" in hub.discovery()
            hub.update(did, {"shown": ["temp", "salt"]})
            assert [o["name"] for o in block(r, f"e:{did}")["outs"]] == ["temp", "salt"]
            # een nieuwe actie
            esp.action("flush", "Spoelen")
            await eventually(lambda: any(a["id"] == "flush" for a in hub.status()[0]["actions"]))
            assert f"homeassistant/button/kk_esp_{did}_do_flush/config" in hub.discovery()
            # een andere eenheid: zelfde id, nieuwe descriptor
            esp.points["temp"]["def"]["unit"] = "°F"
            await eventually(lambda: next(p for p in hub.devices[did]["points"] if p["id"] == "temp")["unit"] == "°F")
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_vanished_point_stays_as_not_available_for_30_days(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            m = r.model.add_module("above", "Redox hoog", 700)
            r.model.set_module(m, inputs={"a": f"e:{did}:orp"})
            assert r.engine.module_values()[m] is True
            esp.remove_point("orp")
            esp.remove_point("pump")
            await eventually(lambda: next(p for p in hub.devices[did]["points"] if p["id"] == "orp").get("gone"))
            assert hub.output(f"e:{did}:orp") is None and r.engine.module_values()[m] is None, "de verwijzing blijft staan en geeft onbekend"
            assert hub.point_type(f"e:{did}:orp") == "number", "het type blijft bekend: de module blijft geldig"
            r.model.set_module(m, value=650)  # het model blijft bewerkbaar
            b = block(r, f"e:{did}")
            assert next(o for o in b["outs"] if o["name"] == "orp")["gone"] is True and next(o for o in b["outs"] if o["name"] == "orp")["value"] is None
            st = hub.states()
            assert st[hub.t(did, "orp", "avail")] == ("offline", True) and st[hub.t(did, "ph", "avail")] == ("online", True)
            assert hub.t(did, "orp") not in st, "geen waarde van een verdwenen punt"
            cfg = hub.discovery()[f"homeassistant/sensor/kk_esp_{did}_orp/config"]
            assert {"topic": hub.t(did, "orp", "avail")} in cfg["availability"], "in HA: unavailable"
            await eventually(lambda: next(p for p in hub.devices[did]["points"] if p["id"] == "pump").get("gone"))
            with pytest.raises(ModelError, match="niet beschikbaar"):
                await hub.set(did, "pump", True)
            # terug: weer beschikbaar
            esp.point("orp", "Redox", "int", 741, unit="mV")
            await eventually(lambda: hub.output(f"e:{did}:orp") == 741)
            assert not next(p for p in hub.devices[did]["points"] if p["id"] == "orp").get("gone")
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_cleanup_after_30_days_survives_a_restart(tmp_path, monkeypatch):
    from kkaddon.engine import Engine
    from kkaddon.model import Model
    from kkaddon.mqttbridge import MqttBridge
    from kkaddon.settings import Settings

    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            esp.remove_point("orp")
            await eventually(lambda: next(p for p in hub.devices[did]["points"] if p["id"] == "orp").get("gone"))
            next(p for p in hub.devices[did]["points"] if p["id"] == "orp")["gone"] = time.time() - 31 * 86400  # al meer dan 30 dagen weg
            esp.point("extra", "Extra", "int", 1)  # een nieuwe descriptor: nu wordt opgeruimd
            await eventually(lambda: all(p["id"] != "orp" for p in hub.devices[did]["points"]))
            topics = hub.retire()
            assert f"homeassistant/sensor/kk_esp_{did}_orp/config" in topics and hub.t(did, "orp") in topics and hub.t(did, "orp", "avail") in topics
            assert f"homeassistant/sensor/kk_esp_{did}_orp/config" not in hub.discovery()
            hub.close()
            # herstart: een nieuwe hub, een nieuwe brug (met een leeg geheugen over wat er retained staat)
            r2 = Rig(tmp_path)
            hub2 = EspDevHub(tmp_path, r2.engine, port=0, poll=0.05)
            assert all(p["id"] != "orp" for p in hub2.devices[did]["points"])
            bridge = MqttBridge(Settings(data_dir=None, key=KEY, key_generated=False), r2.engine)
            bridge.extras.append(hub2)
            bridge._sync_discovery(force=True)
            sent = []
            while not bridge._queue.empty():
                sent.append(bridge._queue.get_nowait())
            assert (f"homeassistant/sensor/kk_esp_{did}_orp/config", "", True) in sent, "een leeg retained bericht wist de entiteit in HA, ook na een herstart"
            assert (hub2.t(did, "orp"), "", True) in sent
            assert not any(t.endswith("_extra/config") and p == "" for t, p, _ in sent), "een bestaand punt wordt niet gewist"
            bridge._sync_discovery()
            assert bridge._queue.empty(), "niet nog eens gewist zonder nieuwe volledige sync"
            hub2.close()
        finally:
            await esp.stop()
    asyncio.run(go())


def test_removing_a_device_retires_its_topics(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            assert await hub.remove(did)
            assert esp.token == "", "het apparaat is vrijgegeven"
            assert not hub.devices and not hub.discovery() and hub.states() == {}
            t = hub.retire()
            assert f"homeassistant/switch/kk_esp_{did}_pump/config" in t and f"homeassistant/sensor/kk_esp_{did}_ph/config" in t and f"homeassistant/button/kk_esp_{did}_do_refresh/config" in t
            assert await hub.remove(did) is False
            again = EspDevHub(tmp_path, Rig(tmp_path).engine, port=0)
            assert not again.devices and set(again.retire()) == set(t), "ook na een herstart"
            again.close()
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_removing_with_an_unreachable_device_still_removes(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            await esp.stop_http()
            assert await hub.remove(did)
            assert not hub.devices
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_point_that_changes_type_counts_as_new_and_loses_its_input(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            hoog = r.model.add_module("above", "pH hoog", 7.5)
            r.model.set_module(hoog, inputs={"a": f"e:{did}:ph"})
            wire(r, hub, did, "pump", f"m:{hoog}")
            esp.points["pump"] = {"def": {"id": "pump", "label": "Pomp", "kind": "float", "writable": True, "min": 0, "max": 100}, "v": 50.0, "age": 0}  # het protocol verbiedt dit
            await eventually(lambda: next(p for p in hub.devices[did]["points"] if p["id"] == "pump")["kind"] == "float")
            assert "pump" not in hub.devices[did]["inputs"], "de oude koppeling hoorde bij een 0/1"
            assert hub.point_type(f"e:{did}:pump") == "number"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_changed_address_is_followed_only_when_unreachable(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            f = {**parse_discovery(json.dumps(esp.discovery()).encode(), ("127.0.0.7", 1)), }
            hub.on_found(f)
            assert hub.devices[did]["host"] == "127.0.0.1", "bereikbaar: het IP blijft"
            hub._stop(did)
            hub.online.discard(did)
            hub.on_found(f)
            assert hub.devices[did]["host"] == "127.0.0.7", "onbereikbaar: het nieuwe DHCP-adres wordt overgenomen"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_everything_survives_a_restart(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            hoog = r.model.add_module("above", "pH hoog", 7.5)
            r.model.set_module(hoog, inputs={"a": f"e:{did}:ph"})
            wire(r, hub, did, "pump", f"m:{hoog}")
            hub.update(did, {"shown": ["ph", "pump"], "name": "Zwembad"})
            token = esp.token
            hub.close()
            r2 = Rig(tmp_path, model=r.model)
            hub2 = EspDevHub(tmp_path, r2.engine, port=0, poll=0.05)
            d = hub2.devices[did]
            assert d["token"] == token and d["inputs"] == {"pump": f"m:{hoog}"} and d["shown"] == ["ph", "pump"] and len(d["points"]) == 10 and d["rev"] == esp.rev
            assert hub2.point_type(f"e:{did}:ph") == "number", "het type is bekend nog voor het apparaat antwoordt"
            hub2.start()
            await eventually(lambda: did in hub2.online)
            assert hub2.output(f"e:{did}:ph") == 7.2
            hub2.close()
        finally:
            await esp.stop()
    asyncio.run(go())


def test_a_damaged_file_is_survived(tmp_path):
    (tmp_path / "espdev.json").write_text("{kapot")
    assert EspDevHub(tmp_path, Rig(tmp_path).engine).devices == {}
    (tmp_path / "espdev.json").write_text(json.dumps({"devices": {"x": {"hid": "Fout"}, "Hoofd": {"hid": "ok-1"}, "pool_1": {"hid": "pool-1", "points": [{"id": "a", "kind": "int"}, "rommel"], "inputs": "kapot", "shown": 3}}, "retired": [1]}))
    h = EspDevHub(tmp_path, Rig(tmp_path).engine)
    assert list(h.devices) == ["pool_1"] and [p["id"] for p in h.devices["pool_1"]["points"]] == ["a"] and h.devices["pool_1"]["inputs"] == {} and h.devices["pool_1"]["shown"] is None


# ------------------------------------------------------------ HA


def test_ha_entities_follow_kind_and_access(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            disc = hub.discovery()
            cfg = lambda dom, key: disc[f"homeassistant/{dom}/kk_esp_{did}_{key}/config"]  # noqa: E731
            ph = cfg("sensor", "ph")
            assert "unit_of_measurement" not in ph and ph["device_class"] == "ph" and ph["state_class"] == "measurement" and ph["suggested_display_precision"] == 2
            temp = cfg("sensor", "temp")
            assert (temp["unit_of_measurement"], temp["device_class"]) == ("°C", "temperature")
            orp = cfg("sensor", "orp")
            assert orp["unit_of_measurement"] == "mV" and "device_class" not in orp and "state_class" not in orp
            sw = cfg("switch", "pump")
            assert sw["command_topic"] == f"kruisschakelaars/esp/{did}/pump/set" and sw["payload_on"] == "ON"
            sel = cfg("select", "mode")
            assert sel["options"] == ["Uit", "Aan", "Auto"] and sel["command_topic"].endswith("/mode/set")
            num = cfg("number", "ph_set")
            assert (num["min"], num["max"], num["step"], num["mode"]) == (6.8, 7.6, 0.1, "box")
            assert "command_topic" not in cfg("binary_sensor", "alarm_no_flow")
            assert cfg("sensor", "rssi")["entity_category"] == "diagnostic" and cfg("sensor", "note")["state_topic"] == hub.t(did, "note")
            fb = cfg("binary_sensor", "factory")
            assert "command_topic" not in fb, "een gevaarlijk punt is in HA alleen te lezen"
            assert cfg("button", "do_refresh")["command_topic"] == f"kruisschakelaars/esp/{did}/do/refresh/press"
            assert f"homeassistant/button/kk_esp_{did}_do_boost/config" not in disc and f"homeassistant/button/kk_esp_{did}_do_reset/config" not in disc, "geen knop met een argument, geen gevaarlijke knop"
            assert ph["unique_id"] == f"kk_esp_{did}_ph" and ph["default_entity_id"] == f"sensor.kk_esp_{did}_ph" and ph["device"]["identifiers"] == [f"kk_esp_{did}"]
            assert [a["topic"] for a in ph["availability"]] == ["kruisschakelaars/status", hub.t(did, "online"), hub.t(did, "ph", "avail")] and ph["availability_mode"] == "all"
            st = hub.states()
            assert st[hub.t(did, "online")] == ("online", True)
            assert (st[hub.t(did, "ph")], st[hub.t(did, "pump")], st[hub.t(did, "mode")], st[hub.t(did, "orp")], st[hub.t(did, "alarm_no_flow")], st[hub.t(did, "note")]) == \
                (("7.20", True), ("OFF", True), ("Auto", True), ("740", True), ("OFF", True), ("hallo", True))
            assert hub.filters == ["kruisschakelaars/esp/+/+/set", "kruisschakelaars/esp/+/do/+/press"]
            assert json.dumps(disc)
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_ha_commands_use_the_same_checks_and_never_a_danger(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            assert hub.command("kruisschakelaars/solar/x/pv/set", "1") is False and hub.command("onzin", "1") is False
            assert hub.command(f"kruisschakelaars/esp/{did}/pump/set", "ON") is True
            await eventually(lambda: esp.points["pump"]["v"] is True)
            assert hub.command(f"kruisschakelaars/esp/{did}/mode/set", "Aan") is True
            await eventually(lambda: esp.points["mode"]["v"] == 1)
            assert hub.command(f"kruisschakelaars/esp/{did}/ph_set/set", "7.5") is True
            await eventually(lambda: esp.points["ph_set"]["v"] == 7.5)
            n = len(esp.calls)
            for topic, payload in [(f"kruisschakelaars/esp/{did}/factory/set", "ON"), (f"kruisschakelaars/esp/{did}/mode/set", "Onzin"), (f"kruisschakelaars/esp/{did}/ph_set/set", "99"),
                                   (f"kruisschakelaars/esp/{did}/ph/set", "1"), (f"kruisschakelaars/esp/{did}/do/reset/press", "PRESS"), (f"kruisschakelaars/esp/{did}/do/boost/press", "PRESS"),
                                   (f"kruisschakelaars/esp/weg/pump/set", "ON")]:
                assert hub.command(topic, payload) is True, "het onderwerp is van ons, ook al weigeren we het"
            await asyncio.sleep(0.3)
            assert len(esp.calls) == n or all(c[1] == "/huisdev/state" for c in esp.calls[n:]), "niets gevaarlijks of ongeldigs naar het apparaat"
            assert esp.points["factory"]["v"] is False
            assert hub.command(f"kruisschakelaars/esp/{did}/do/refresh/press", "PRESS") is True
            await eventually(lambda: any(c[1] == "/huisdev/do" and c[2]["id"] == "refresh" for c in esp.calls))
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


# ------------------------------------------------------------ de web-API


def test_web_api_claims_places_sets_and_removes(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from kkaddon import web
    from kkaddon.settings import Settings

    async def go():
        r, hub, esp, _ = await rig(tmp_path, claim=False, monkeypatch=monkeypatch)
        ctx = web.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), r.engine, espdev=hub)
        r.model.set_schema({**r.model.schema, "pages": [{"id": "zwembad", "name": "Zwembad"}]})
        c = TestClient(TestServer(web.make_app(ctx)))
        await c.start_server()
        try:
            st = await (await c.get("/api/state")).json()
            assert st["espdev"] == [] and [f["hid"] for f in st["espdev_found"]] == ["pool-a1b2c3"], "gevonden, nog niet geclaimd, ook zonder blok te zien"
            bad = await c.post("/api/espdev/claim", json={"hid": "onbekend"})
            assert bad.status == 400 and "gevonden" in (await bad.json())["error"]
            res = await (await c.post("/api/espdev/claim", json={"hid": "pool-a1b2c3", "page": "zwembad", "x": 40, "y": 30})).json()
            did = res["id"]
            assert res["key"] == f"e:{did}" and r.model.schema["blocks"][f"e:{did}"]["page"] == "zwembad"
            await eventually(lambda: did in hub.online)
            st = await (await c.get("/api/state")).json()
            assert st["espdev_found"] == [] and any(b["key"] == f"e:{did}" for b in st["schema"]["blocks"]) and esp.token not in json.dumps(st)
            assert (await c.put(f"/api/espdev/{did}", json={"name": "Achter"})).status == 200 and hub.devices[did]["name"] == "Achter"
            assert (await c.put(f"/api/espdev/{did}", json={"name": ""})).status == 400
            assert (await c.post(f"/api/espdev/{did}/set", json={"point": "pump", "value": True})).status == 200 and esp.points["pump"]["v"] is True
            bad = await c.post(f"/api/espdev/{did}/set", json={"point": "ph_set", "value": 9})
            assert bad.status == 400 and "tussen" in (await bad.json())["error"]
            bad = await c.post(f"/api/espdev/{did}/set", json={"point": "factory", "value": True})
            assert bad.status == 400 and "Alles wissen" in (await bad.json())["error"] and esp.points["factory"]["v"] is False
            assert (await c.post(f"/api/espdev/{did}/set", json={"point": "factory", "value": True, "confirm": True})).status == 200 and esp.points["factory"]["v"] is True
            assert (await c.post(f"/api/espdev/{did}/do", json={"action": "boost", "args": {"minutes": 15}})).status == 200
            bad = await c.post(f"/api/espdev/{did}/do", json={"action": "reset"})
            assert bad.status == 400 and "Doorgaan" in (await bad.json())["error"]
            assert (await c.post(f"/api/espdev/{did}/do", json={"action": "reset", "confirm": True})).status == 200
            assert (await c.post("/api/espdev/scan", json={})).status == 200
            assert (await c.post("/api/espdev/scan", json={"host": "http://x"})).status == 400
            ev = r.events_of("espdev")
            assert ev, "de pagina krijgt een gebeurtenis bij elke wijziging"
            assert (await c.delete(f"/api/espdev/{did}")).status == 200 and did not in hub.devices and esp.token == ""
            assert f"e:{did}" not in r.model.schema["blocks"]
            assert (await c.delete(f"/api/espdev/{did}")).status == 404
        finally:
            await c.close()
            await shutdown(hub, esp)
    asyncio.run(go())


def test_web_pushes_the_espdev_event_to_the_page():
    from kkaddon import web
    assert "espdev" in web.PUSH


def test_web_without_a_hub_says_so(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from kkaddon import web
    from kkaddon.settings import Settings

    async def go():
        r = Rig(tmp_path)
        c = TestClient(TestServer(web.make_app(web.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), r.engine))))
        await c.start_server()
        try:
            res = await c.post("/api/espdev/claim", json={"hid": "x"})
            assert res.status == 400 and "niet beschikbaar" in (await res.json())["error"]
            assert (await c.get("/api/state")).status == 200
        finally:
            await c.close()
    asyncio.run(go())


# ------------------------------------------------------------ de versiestanden en de Nodes-tab


def test_nodes_tab_row_and_version_states(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            row = next(x for x in inventory.build(r.engine, None) if x["key"] == f"e{did}")
            assert (row["name"], row["kind"], row["protocol"], row["ip"], row["version"], row["online"], row["note"]) == ("Zwembad", "poolcontroller", "HUISDEV 1.0", "127.0.0.1", "0.3.1", True, "")
            assert hub.status()[0]["proto_state"] == "ok"
            monkeypatch.setattr(eh, "CURRENT", (1, 1))
            row = next(x for x in inventory.build(r.engine, None) if x["key"] == f"e{did}")
            assert hub.status()[0]["proto_state"] == "upgrade" and "upgrade beschikbaar" in row["note"] and "1.1" in row["note"]
            hub.devices[did]["proto"] = "2.0"
            row = next(x for x in inventory.build(r.engine, None) if x["key"] == f"e{did}")
            assert hub.status()[0]["proto_state"] == "unsupported" and "niet ondersteund" in row["note"]
            hub._stop(did)
            hub.online.discard(did)
            hub.errors[did] = "geen verbinding"
            hub.devices[did]["proto"] = "1.1"
            assert next(x for x in inventory.build(r.engine, None) if x["key"] == f"e{did}")["note"] == "geen verbinding"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_device_that_upgrades_to_an_unknown_major_goes_offline_with_a_message(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did = await rig(tmp_path, monkeypatch=monkeypatch)
        try:
            esp.proto = "2.0"
            await eventually(lambda: did not in hub.online)
            assert "niet ondersteund" in hub.status()[0]["error"] and hub.output(f"e:{did}:ph") is None
            esp.proto = "1.0"
            await eventually(lambda: did in hub.online)
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_minor_version_that_adds_a_kind_is_read_without_breaking(tmp_path, monkeypatch):
    async def go():
        esp = pool()
        esp.proto = "1.4"
        esp.point("kleur", "Kleur", "rgb", "ff0000")  # een kind dat deze add-on niet kent
        r, hub, esp, did = await rig(tmp_path, esp=esp, monkeypatch=monkeypatch)
        try:
            assert hub.status()[0]["proto_state"] == "ok" and did in hub.online
            assert all(p["id"] != "kleur" for p in hub.devices[did]["points"]) and hub.output(f"e:{did}:ph") == 7.2
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())
