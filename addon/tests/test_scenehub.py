"""HUISDEV-scènes aan de kant van de add-on (kkaddon/scenehub.py, protocol/huisdev/HUISDEV.md §16): de versiestand, de descriptor met en zonder `scenes`, de
bibliotheek (ids nooit hergebruikt), selectie en slots, de sync tegen een nagebootst device (tests/fakehuisdev.py, gebouwd naar de spec), de ingangen (eerste stand,
start, stop, 0-1-0, offline), de geplande starts van een wekker (weekdagen, aaneengesloten intervallen, zomertijd, laagwatermerk) en het opruimen bij verwijderen.

De tests tikken zelf: de poll van het apparaat staat uit (`hub._stop`) en `cycle()` doet een ronde (GET state, dan de sync), met een eigen klok (`Clock`)."""
import asyncio
import base64
import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from aiohttp.test_utils import TestClient, TestServer

from kkaddon import espdevhub as eh
from kkaddon import scenehub, scenes as sc, schema, web
from kkaddon.espdevhub import EspDevHub, clean_descriptor, proto_state
from kkaddon.model import ModelError
from kkaddon.settings import Settings
from tests.fakehuisdev import FakeEsp, kidswekker, pool
from tests.rig import KEY, Rig

AMS = ZoneInfo("Europe/Amsterdam")
NIGHT = {"kind": "static"}
WAKE = {"kind": "wake"}
RAINBOW = {"kind": "rainbow"}


def local(y, mo, d, h=0, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=AMS).timestamp())


def fmt(e) -> str:
    return datetime.fromtimestamp(e, AMS).strftime("%Y-%m-%d %H:%M")


class Clock:
    """De klok van de sync (monotoon en wandklok) en van het nagebootste apparaat: samen te verzetten."""

    def __init__(self, esp=None, epoch=None):
        self.mono = 5000.0
        self.epoch = float(epoch or local(2027, 1, 4, 12, 0))  # maandag
        self.esp = esp
        if esp:
            esp.now = self.epoch

    def advance(self, s):
        self.mono += s
        self.epoch += s
        if self.esp:
            self.esp.now = self.epoch


async def eventually(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    raise AssertionError("duurde te lang")


async def env(tmp_path, monkeypatch, esp=None, manual=True):
    """Een hub met een geclaimd apparaat (standaard een kidswekker met scènes), zonder eigen poll zodat de test de rondes telt, en met een eigen klok."""
    monkeypatch.setattr(eh, "TICK", 3600)  # de tests tikken zelf
    monkeypatch.setattr(scenehub, "WAIT", 0.02)
    monkeypatch.setattr(scenehub, "RETRY_GAP", 0.02)
    r = Rig(tmp_path)
    r.engine.set_tz("Europe/Amsterdam")
    hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
    esp = esp or kidswekker()
    await esp.start()
    hub.start()
    await eventually(lambda: hub._ask and hub._listen)
    hub.ask("127.0.0.1", esp.udp_port)
    await eventually(lambda: esp.hid in hub.found)
    did = await hub.claim(esp.hid)
    await eventually(lambda: did in hub.online)
    clock = Clock(esp)
    hub.scn.mono, hub.scn.wall = (lambda: clock.mono), (lambda: clock.epoch)
    if manual:
        hub._stop(did)
    return r, hub, esp, did, clock


async def shutdown(hub, *esps):
    hub.close()
    for e in esps:
        await e.stop()
    await asyncio.sleep(0.05)


async def cycle(hub, did, n=1):
    """Een poll en een sync, zoals `_run` ze doet."""
    for _ in range(n):
        status, data = await hub._call(hub.devices[did], "GET", "/huisdev/state")
        assert status == 200
        await hub._on_state(did, data)
        await hub.scn.sync(did)


async def fill(hub, did, esp, rounds=12):
    for _ in range(rounds):
        await cycle(hub, did)
    assert all(esp.slots.get(n) == hub.scn.lib.bytes[sid] for sid, n in hub.devices[did]["scn"]["slots"].items()), "alle slots gevuld"


async def settle(hub, did):
    """Wacht tot de verzender van het apparaat klaar is."""
    await eventually(lambda: not hub.scn.q.get(did) and f"scn:{did}" not in hub.tasks)


def switch(r):
    """Een 0/1-bron die de test met de hand zet: (zetter, bron)."""
    n = r.model.add_module("number", "Knop", 0)
    hi = r.model.add_module("above", "Aan", 0.5)
    r.model.set_module(hi, inputs={"a": f"m:{n}"})
    return (lambda on: r.model.set_module(n, value=1 if on else 0)), f"m:{hi}"


def wire(hub, did, sid, ref):
    """Een scène starten door een 0/1-bron (zoals voor 0.45 een ingang op het apparaatblok): een blok Scènes bij dit apparaat met een ingang en de regel `ingang = 1`
    voor deze scène. `ref` None haalt de ingang en de regel weg."""
    sh = hub.scn
    bid = sh.rules.device_owner().get(did) or sh.add_block({"name": "Test", "device": did})
    sh.update_block(bid, {"rules": [r for r in sh.rules.items[bid]["rules"] if r["scene"] != sid]})
    ins = [i for i in sh.rules.items[bid]["inputs"] if i["name"] != sid]
    if ref:
        ins.append({"name": sid, "kind": "bool", "ref": ref})
    sh.update_block(bid, {"inputs": ins})
    if ref:
        iid = next(i["id"] for i in sh.rules.items[bid]["inputs"] if i["name"] == sid)
        sh.update_block(bid, {"rules": sh.rules.items[bid]["rules"] + [{"scene": sid, "when": [{"op": "on", "in": iid}]}]})
    return bid


def mem(hub, did):
    """Wat de regels de vorige ronde kozen: True (een scène speelt), False (geen) of None (nog geen eerste stand of onbekend)."""
    bid = hub.scn.rules.device_owner().get(did)
    m = hub.scn.rmem.get(bid) if bid else None
    return None if m is None else m["scene"] is not None


def calls(esp, op=None):
    return [(o, b) for o, b in esp.scene_calls if op is None or o == op]


def block(r, key):
    return next(x for x in schema.view(r.model, r.engine.snapshot())["blocks"] if x["key"] == key)


# ------------------------------------------------------------ de versiestand en de descriptor

def test_proto_state_upgrade_only_for_huisdev_devices():
    assert eh.CURRENT == (1, 2)
    assert [proto_state(x) for x in ("1.0", "1.1", "1.2", "1.3", "2.0", "x", None)] == ["upgrade", "upgrade", "ok", "ok", "unsupported", "unsupported", "unsupported"]
    for driver in ("hue", "nuki", "brink", "smappee", "reolink", "esphome"):
        assert proto_state("1.0", driver) == "ok", driver
        assert proto_state("2.0", driver) == "unsupported", "een onbekende major blijft onbekend, voor elk soort"
    assert proto_state("1.0", "huisdev") == "upgrade"


def test_only_a_real_huisdev_device_shows_upgrade_in_the_status(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch, esp=pool(), manual=False)  # een 1.0-apparaat
        try:
            assert hub.status()[0]["proto_state"] == "upgrade" and hub.status()[0]["current"] == "1.2"
            for drv in ("hue", "nuki", "brink", "smappee", "esphome"):
                hub.devices[did]["driver"] = drv  # alle andere soorten zeggen vast 1.0
                assert hub.status()[0]["proto_state"] == "ok", drv
            hub.devices[did]["driver"] = "huisdev"
            hub.devices[did]["proto"] = "1.1"
            assert hub.status()[0]["proto_state"] == "upgrade", "1.1 mist `scene/seek` (1.2)"
            hub.devices[did]["proto"] = "1.2"
            assert hub.status()[0]["proto_state"] == "ok"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_descriptor_keeps_a_valid_scenes_object_and_drops_an_invalid_one():
    base = kidswekker().descriptor()
    ok = clean_descriptor(base)
    assert ok["scenes"] == {"formats": ["ledscene/1"], "slots": 8, "max_bytes": 768, "sched": 8}
    assert "scenes" not in clean_descriptor(pool().descriptor()), "een apparaat zonder scènes"
    for bad in (None, [], "x", {}, {"formats": "ledscene/1", "slots": 8, "max_bytes": 768, "sched": 8}, {"formats": [], "slots": 0, "max_bytes": 768, "sched": 8},
                {"formats": ["a"], "slots": 9, "max_bytes": 100, "sched": 8}, {"formats": ["a"], "slots": 8, "max_bytes": 1025, "sched": 8},
                {"formats": ["a"], "slots": 8, "max_bytes": 769, "sched": 8}, {"formats": ["a"], "slots": 8, "max_bytes": 768, "sched": 9},
                {"formats": ["a"], "slots": True, "max_bytes": 100, "sched": 1}, {"formats": ["a"], "slots": 1.5, "max_bytes": 100, "sched": 1}, {"formats": ["a"], "slots": 1, "max_bytes": 100}):
        d = clean_descriptor({**base, "scenes": bad})
        assert d is not None and "scenes" not in d, f"ongeldig = weglaten, niet falen: {bad!r}"
    assert clean_descriptor({**base, "scenes": {"formats": ["ledscene/1", 5, "x"], "slots": 6, "max_bytes": 1024, "sched": 0}})["scenes"] == {"formats": ["ledscene/1", "x"], "slots": 6, "max_bytes": 1024, "sched": 0}
    assert kidswekker().rev != kidswekker(slots=4).rev, "de rev rekent `scenes` mee"
    e = kidswekker()
    e.scene_cap = None
    assert e.rev != kidswekker().rev


def test_a_device_without_scenes_has_no_scene_features(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch, esp=pool(), manual=False)
        try:
            assert hub.devices[did]["scn"] == {"sel": [], "slots": {}, "cap": None}
            sid = hub.scn.add({"name": "Nacht", "def": NIGHT})
            with pytest.raises(ModelError, match="meldt geen scènes"):
                hub.scn.select(did, [sid])
            with pytest.raises(ModelError, match="meldt geen scènes"):
                await hub.scn.play(did, {"id": sid})
            assert not any("/scene/" in c[1] for c in esp.calls)
            v = hub.status()[0]["scn"]
            assert v["cap"] is None and v["sel"] == []
            with pytest.raises(ModelError):  # een blok Scènes voor een apparaat dat niet bestaat
                hub.scn.update_block(hub.scn.add_block({"name": "X"}), {"device": "bestaat_niet"})
            bid = hub.scn.add_block({"name": "Slaapt", "device": did})
            assert hub.scn.rules_view()[0]["ready"] is False
            hub.scn.tick_rules()
            assert bid not in hub.scn.rmem, "zonder scènes op het apparaat doet het blok niets"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_state_scenes_are_read_and_time_and_until_do_not_count_as_a_change(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            on = hub.scn.on_state
            assert on(did, None) is True and on(did, None) is False, "scènes die er waren en verdwijnen is een wijziging, daarna niet meer"
            assert on("onbekend", None) is False and "onbekend" not in hub.scn.rt, "een apparaat zonder scènes krijgt geen administratie"
            base = {"have": {"1": {"h": "a1b2c3d4", "n": 424}}, "run": {"slot": 1, "h": "a1b2c3d4", "until": 1_800_000_000}, "sched": [{"slot": 1, "at": 1_800_000_500}], "time": 1_799_000_000}
            assert on(did, base) is True
            rt = hub.scn.rt[did]
            assert rt["have"] == {1: {"h": "a1b2c3d4", "n": 424}} and rt["run"] == {"slot": 1, "h": "a1b2c3d4", "until": 1_800_000_000} and rt["sched"] == [{"slot": 1, "at": 1_800_000_500}] and rt["time"] == 1_799_000_000
            assert on(did, {**base, "time": 1_799_000_003}) is False, "time verandert elke poll"
            assert on(did, {**base, "time": 1_799_000_006, "run": {**base["run"], "until": 1_800_000_999}}) is False, "run.until telt niet"
            assert on(did, {**base, "run": {"slot": 0, "h": "a1b2c3d4", "until": None}}) is True, "run.slot wel"
            assert on(did, {**base, "have": {}}) is True and on(did, {"time": None}) is True and rt["time"] is None and rt["run"] is None and rt["sched"] == []
            assert on(did, {"have": {"9": {"h": "aa", "n": 1}, "x": 1, "2": "kapot"}, "run": "kapot", "sched": [1, {"slot": "a"}, {"slot": 1, "at": 2}], "time": 5}) is True
            assert rt["have"] == {} and rt["run"] is None and rt["sched"] == [{"slot": 1, "at": 2}] and rt["time"] is None, "ongeldig wordt genegeerd, een klok onder 1,7 miljard is niet geldig"
            assert on(did, "kapot") is True and rt["known"] is False
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_poll_with_only_a_new_time_does_not_poke_the_page(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            await cycle(hub, did)
            n = len(r.events_of("espdev"))
            for _ in range(3):
                clock.advance(3)
                await cycle(hub, did)
            assert len(r.events_of("espdev")) == n, "de klok van het apparaat loopt, de pagina hoeft er niet voor te verversen"
            assert hub.scn.rt[did]["time"] == int(clock.epoch)
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


# ------------------------------------------------------------ de bibliotheek

def test_library_crud_never_reuses_an_id_and_validates(tmp_path):
    lib = scenehub.Library(tmp_path / "scenes.json")
    a = lib.add({"name": " Nachtlicht ", "def": NIGHT})
    b = lib.add({"name": "Wekker", "def": WAKE, "release": "finish"})
    assert (a, b) == ("s1", "s2") and lib.items["s1"]["name"] == "Nachtlicht" and lib.items["s1"]["release"] == "stop" and lib.items["s2"]["release"] == "finish"
    raw = json.loads((tmp_path / "scenes.json").read_text(encoding="utf-8"))
    assert raw["next"] == 3 and raw["scenes"]["s2"] == {"name": "Wekker", "def": WAKE, "release": "finish"}
    assert lib.hashes["s1"] == sc.hash_hex(sc.compile_def(NIGHT)) and lib.bytes["s2"] == sc.compile_def(WAKE)
    assert lib.remove("s2") is True and lib.remove("s2") is False
    assert lib.add({"name": "Regenboog", "def": RAINBOW}) == "s3", "een id wordt nooit hergebruikt"
    lib.remove("s3")
    assert scenehub.Library(tmp_path / "scenes.json").add({"name": "Nog een", "def": NIGHT}) == "s4", "ook niet na een herstart, ook niet als de hoogste weg is"
    before = json.loads((tmp_path / "scenes.json").read_text(encoding="utf-8"))
    lib.update("s1", {"name": "Nachtlamp"})
    assert lib.items["s1"]["name"] == "Nachtlamp" and lib.items["s1"]["def"] == NIGHT and lib.hashes["s1"] == sc.hash_hex(sc.compile_def(NIGHT)), "naam vrij hernoembaar, de rest blijft"
    h = lib.hashes["s1"]
    lib.update("s1", {"def": {"kind": "static", "color": "#ff0000"}, "release": "finish"})
    assert lib.hashes["s1"] != h and lib.items["s1"]["release"] == "finish"
    for bad, word in (({"name": "", "def": NIGHT}, "naam"), ({"def": NIGHT}, "naam"), ({"name": "x"}, "scène"), ({"name": "x", "def": {"kind": "onzin"}}, "scène: kind"),
                      ({"name": "x", "def": {"kind": "static", "color": "rood"}}, "#rrggbb"), ({"name": "x", "def": {"kind": "fade", "c1": "#000000"}}, "scène"),
                      ({"name": "x", "def": NIGHT, "release": "nooit"}, "release"), ("geen object", "object")):
        with pytest.raises(ModelError, match=word):
            lib.add(bad)
    with pytest.raises(ModelError, match="onbekende scène"):
        lib.update("s99", {"name": "x"})
    items_before = json.dumps(lib.items, sort_keys=True)
    with pytest.raises(ModelError):
        lib.update("s1", {"def": {"kind": "static", "color": "nope"}})
    assert json.dumps(lib.items, sort_keys=True) == items_before, "een ongeldige wijziging verandert niets"
    del before
    # raw: een eigen definitie
    sid = lib.add({"name": "Raw", "def": {"kind": "raw", "header": {"loops": 1, "end": 1, "hold": 0}, "frames": [{"dt": 0, "spans": [{"c1": "#102030"}]}]}})
    assert lib.hashes[sid] and len(lib.bytes[sid]) == 16 + 7 + 9


def test_library_limit_and_a_broken_file(tmp_path):
    (tmp_path / "scenes.json").write_text("{kapot", encoding="utf-8")
    lib = scenehub.Library(tmp_path / "scenes.json")
    assert lib.items == {} and lib.next == 1
    (tmp_path / "scenes.json").write_text(json.dumps({"next": 7, "scenes": {"s3": {"name": "Goed", "def": NIGHT}, "s4": {"name": "Stuk", "def": {"kind": "onzin"}}, "x": {}, "s5": 5}}), encoding="utf-8")
    lib = scenehub.Library(tmp_path / "scenes.json")
    assert sorted(lib.items) == ["s3", "s4"] and "s3" in lib.bytes and "s4" not in lib.bytes and lib.next == 7, "wat niet meer compileert blijft staan maar speelt niet"
    assert [x["id"] for x in lib.view() if x["bytes"] is None] == ["s4"]
    lib.next = 100
    for i in range(scenehub.MAX_SCENES - 2):
        lib.add({"name": f"n{i}", "def": NIGHT})
    with pytest.raises(ModelError, match="maximaal"):
        lib.add({"name": "teveel", "def": NIGHT})


def test_library_view_has_what_the_page_needs(tmp_path):
    lib = scenehub.Library(tmp_path / "scenes.json")
    lib.add({"name": "Wekker", "def": {"kind": "wake", "sun_min": 5, "wake_min": 10}})
    lib.add({"name": "Regenboog", "def": RAINBOW})
    v = {x["id"]: x for x in lib.view()}
    assert v["s1"]["bounded"] is True and v["s1"]["total_ms"] == 5 * 60000 + 10 * 60000 + 5000 and v["s1"]["bytes"] == len(lib.bytes["s1"]) and v["s1"]["hash"] == lib.hashes["s1"]
    assert v["s2"]["bounded"] is False and v["s2"]["total_ms"] is None
    assert v["s1"]["window_ms"] == v["s1"]["total_ms"] and v["s2"]["window_ms"] == 5020, "de lengte van de tijdschuif: de duur, of een ronde van een lus"
    p = scenehub.preview({"kind": "static", "color": "#ff0000"}, 5, 3)
    assert p["n"] == 5 and len(p["frames"]) == len(p["times"]) and p["frames"][0][0][0] == 255 and p["frames"][0][0][1] == 0
    with pytest.raises(ModelError, match="scène"):
        scenehub.preview({"kind": "nope"})


# ------------------------------------------------------------ selectie en slots

def test_selection_assigns_stable_slots_that_are_never_reused_while_selected(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            ids = [hub.scn.add({"name": f"Scène {i}", "def": NIGHT if i else WAKE}) for i in range(4)]
            s1, s2, s3, s4 = ids
            hub.scn.select(did, [s1, s2])
            assert hub.devices[did]["scn"]["slots"] == {s1: 1, s2: 2}
            hub.scn.select(did, [s2, s3, s1])  # een andere volgorde en een erbij: niemand verhuist
            assert hub.devices[did]["scn"]["slots"] == {s1: 1, s2: 2, s3: 3}
            hub.scn.select(did, [s3, s2])  # s1 weg: zijn slot is vrij
            assert hub.devices[did]["scn"]["slots"] == {s2: 2, s3: 3}
            hub.scn.select(did, [s3, s2, s4, s1])  # het laagste vrije nummer
            assert hub.devices[did]["scn"]["slots"] == {s2: 2, s3: 3, s4: 1, s1: 4}
            assert [x["id"] for x in hub.status()[0]["scn"]["sel"]] == [s3, s2, s4, s1]
            for bad, word in (("s1", "lijst"), ([1], "lijst"), ([s1, s1], "hoogstens een keer"), (["s99"], "onbekende scène"), ([f"s{i}" for i in range(200)], "maximaal")):
                with pytest.raises(ModelError, match=word):
                    hub.scn.select(did, bad)
            assert hub.devices[did]["scn"]["slots"] == {s2: 2, s3: 3, s4: 1, s1: 4}, "een foutieve selectie verandert niets"
            saved = json.loads((tmp_path / "espdev.json").read_text(encoding="utf-8"))["devices"][did]["scn"]
            assert saved["sel"] == [s3, s2, s4, s1] and saved["slots"] == {s2: 2, s3: 3, s4: 1, s1: 4} and saved["cap"]["slots"] == 8
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_too_many_scenes_keep_their_row_with_a_message_until_a_slot_is_free(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch, esp=kidswekker(slots=2))
        try:
            a, b, c = (hub.scn.add({"name": n, "def": NIGHT}) for n in ("A", "B", "C"))
            hub.scn.select(did, [a, b, c])
            assert hub.devices[did]["scn"]["slots"] == {a: 1, b: 2}
            assert "geen vrij slot" in hub.last[did][f"scene:{c}"]
            v = {x["id"]: x for x in hub.status()[0]["scn"]["sel"]}
            assert v[c]["state"] == "noslot" and v[c]["slot"] is None and "geen vrij slot" in v[c]["note"]
            await fill(hub, did, esp)
            assert sorted(esp.slots) == [1, 2]
            hub.scn.select(did, [c, b])  # a weg: c krijgt zijn slot
            assert hub.devices[did]["scn"]["slots"] == {b: 2, c: 1} and f"scene:{c}" not in hub.last[did]
            await fill(hub, did, esp)
            assert esp.slots[1] == hub.scn.lib.bytes[c] and esp.slots[2] == hub.scn.lib.bytes[b]
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_scn_survives_a_restart_and_an_old_file_without_it_loads(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            a, b = (hub.scn.add({"name": n, "def": NIGHT}) for n in ("A", "B"))
            src = switch(r)[1]
            bid = wire(hub, did, b, src)
            hub.close()
            hub2 = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
            assert hub2.devices[did]["scn"] == {"sel": [b], "slots": {b: 1}, "cap": {"formats": ["ledscene/1"], "slots": 8, "max_bytes": 768, "sched": 8}}
            assert hub2.devices[did]["inputs"] == {} and sorted(hub2.scn.lib.items) == [a, b]
            assert hub2.scn.rules.items[bid]["rules"] == [{"scene": b, "when": [{"op": "on", "in": "i1"}]}] and hub2.scn.rules.items[bid]["inputs"][0]["ref"] == src
            (tmp_path / "scenerules.json").unlink()  # zonder blok kan de selectie uit een oud of stuk bestand niet worden afgeleid
            raw = json.loads((tmp_path / "espdev.json").read_text(encoding="utf-8"))
            for bad in (None, 5, [], {"sel": "x"}, {"sel": ["s1", "s1", "zzz", 7], "slots": {"s1": 99, "zzz": 1}, "cap": "stuk"}):
                if bad is None:
                    raw["devices"][did].pop("scn", None)
                else:
                    raw["devices"][did]["scn"] = bad
                (tmp_path / "espdev.json").write_text(json.dumps(raw), encoding="utf-8")
                h3 = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
                scn = h3.devices[did]["scn"]
                assert set(scn) == {"sel", "slots", "cap"} and scn["slots"] == {} and scn["cap"] is None and scn["sel"] in ([], ["s1"]), bad
            # een scène die uit de bibliotheek is verdwenen (bestand gewist): selectie en bedrading verdwijnen bij het laden
            raw["devices"][did]["scn"] = {"sel": [a, "s9"], "slots": {a: 1, "s9": 2}, "cap": None}
            (tmp_path / "espdev.json").write_text(json.dumps(raw), encoding="utf-8")
            h4 = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
            assert h4.devices[did]["scn"]["sel"] == [a] and h4.devices[did]["inputs"] == {} and not h4.scn.rules.items
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_scene_inputs_from_before_0_45_become_a_scene_block_with_the_same_behaviour(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            a, b, c = (hub.scn.add({"name": n, "def": NIGHT}) for n in ("Nachtlicht", "Wekker", "Zonder bron"))
            hub.scn.select(did, [a, b, c])
            tm = r.model.save_timer(None, "Wekker", {"mon": [{"from": "07:00", "to": "08:00"}]})
            src = switch(r)[1]
            hub.devices[did]["inputs"].update({f"scene:{a}": src, f"scene:{b}": f"t:{tm}", "scene:s99": src, "brightness": src})  # de oude bedrading, en een ingang van een gewoon punt
            r.model.set_schema({"pages": [{"id": "p1", "name": "Kamer"}], "blocks": {f"e:{did}": {"page": "p1", "x": 700, "y": 40}}, "switches": {}, "names": {}})
            hub._save()
            hub.close()
            h2 = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
            assert h2.devices[did]["inputs"] == {"brightness": src}, "de scène-ingangen zijn weg, een gewone ingang blijft"
            (bid, blk), = h2.scn.rules.items.items()
            assert blk["device"] == did and blk["name"] == f"Scènes {h2.devices[did]['name']}"
            assert [(i["name"], i["kind"], i["ref"]) for i in blk["inputs"]] == [("Nachtlicht", "bool", src), ("Wekker", "bool", f"t:{tm}")]
            assert blk["rules"] == [{"scene": a, "when": [{"op": "on", "in": "i1"}]}, {"scene": b, "when": [{"op": "on", "in": "i2"}]}], "een scène zonder bron krijgt geen regel"
            assert h2.devices[did]["scn"]["sel"] == [a, b] and c not in h2.devices[did]["scn"]["slots"]
            assert r.model.schema["blocks"][f"q:{bid}"] == {"page": "p1", "x": 400, "y": 40}, "het blok staat naast het apparaat"
            h2.close()
            h3 = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
            assert list(h3.scn.rules.items) == [bid], "opnieuw laden maakt er niet nog een"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


# ------------------------------------------------------------ de bedrading en het blok

def test_the_scene_block_shows_on_the_schema_and_its_inputs_are_checked(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            a, b = hub.scn.add({"name": "Nachtlicht", "def": NIGHT}), hub.scn.add({"name": "Wekker", "def": WAKE})
            assert not [p for p in block(r, f"e:{did}")["ports"] if p["name"].startswith("scene:")], "het apparaatblok heeft geen scène-rijen meer"
            bid = hub.scn.add_block({"name": "Kidswekker scènes", "device": did, "inputs": [{"name": "Nacht", "kind": "bool"}, {"name": "Wekker", "kind": "time"}]})
            blk = block(r, f"q:{bid}")
            assert (blk["kind"], blk["name"], blk["device"], blk["rows"], blk["active"]) == ("scenes", "Kidswekker scènes", did, 3, None)
            assert [(p["name"], p["label"], p["type"], p["ref"]) for p in blk["ports"]] == [("i1", "Nacht", "bool", None), ("i2", "Wekker", "time", None)]
            tm = r.model.save_timer(None, "Wekker", {"mon": [{"from": "07:00", "to": "08:00"}]})
            n = r.model.add_module("number", "Getal", 5)
            t7 = r.model.add_module("time", "Zeven", "07:00")
            hub.scn.update_block(bid, {"refs": {"i1": f"t:{tm}", "i2": f"m:{t7}"}})
            assert [p["ref"] for p in block(r, f"q:{bid}")["ports"]] == [f"t:{tm}", f"m:{t7}"]
            for refs, word in (({"i1": f"m:{n}"}, "een 0 of 1"), ({"i2": f"t:{tm}"}, "een tijd"), ({"i1": "m:bestaat_niet"}, "niet bestaat"), ({"i1": 5}, "ongeldige bron"), ({"i9": f"t:{tm}"}, "onbekende ingang")):
                with pytest.raises(ModelError, match=word):
                    hub.scn.update_block(bid, {"refs": refs})
            hub.scn.update_block(bid, {"refs": {"i1": None}})
            assert hub.scn.rules.items[bid]["inputs"][0]["ref"] is None
            # een bron die intussen weg is, blokkeert een andere wijziging niet (een ingang erbij) en de melding noemt de ingang
            r.model.remove_module(t7)
            hub.scn.update_block(bid, {"inputs": [{"id": "i1"}, {"id": "i2"}, {"name": "Extra", "kind": "bool"}]})
            assert hub.scn.rules.items[bid]["inputs"][1]["ref"] == f"m:{t7}" and len(hub.scn.rules.items[bid]["inputs"]) == 3
            with pytest.raises(ModelError, match="ingang Wekker: .*niet bestaat"):
                hub.scn.update_block(bid, {"refs": {"i2": "m:bestaat_niet"}})
            t7 = r.model.add_module("time", "Zeven", "07:00")
            with pytest.raises(ModelError, match="al een blok Scènes"):
                hub.scn.add_block({"device": did})
            with pytest.raises(ModelError, match="onbekend apparaat"):
                hub.scn.add_block({"device": "bestaat_niet"})
            # de selectie van het apparaat volgt de regels
            hub.scn.update_block(bid, {"rules": [{"scene": b, "when": [{"op": "on", "in": "i1"}]}, {"scene": a, "when": [{"op": "wake", "in": "i2"}]}]})
            assert hub.devices[did]["scn"]["sel"] == [b, a] and hub.devices[did]["scn"]["slots"] == {b: 1, a: 2}
            hub.scn.update_block(bid, {"rules": [{"scene": a, "when": [{"op": "on", "in": "i1"}]}]})
            assert hub.devices[did]["scn"]["sel"] == [a] and hub.devices[did]["scn"]["slots"] == {a: 2}, "een slot blijft bij zijn scène, een andere scène verhuist niet"
            assert hub.scn.delete_block(bid) and hub.devices[did]["scn"]["sel"] == [] and not hub.scn.delete_block(bid)
            assert hub.devices[did]["inputs"] == {}
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_rules_are_validated(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            a = hub.scn.add({"name": "Nachtlicht", "def": NIGHT})
            bid = hub.scn.add_block({"device": did, "inputs": [{"name": "Knop", "kind": "bool"}, {"name": "Van", "kind": "time"}, {"name": "Tot", "kind": "time"}]})
            ok = {"scene": a, "when": [{"op": "on", "in": "i1"}, {"op": "between", "from": "i2", "to": "i3"}]}
            hub.scn.update_block(bid, {"rules": [ok]})
            for rule, word in (({"scene": "s99", "when": ok["when"]}, "kies een scène"), ({"scene": a, "when": []}, "minstens een voorwaarde"), ({"scene": a, "when": [{"op": "xx"}]}, "kies een van"),
                               ({"scene": a, "when": [{"op": "on", "in": "i2"}]}, "geen 0/1"), ({"scene": a, "when": [{"op": "between", "from": "i1", "to": "i3"}]}, "geen tijd"),
                               ({"scene": a, "when": [{"op": "on", "in": "i9"}]}, "kies een ingang"), ({"scene": a, "when": [{"op": "wake", "in": "i2", "before": 721}]}, "minuten ervoor"),
                               ({"scene": a, "when": [{"op": "wake", "in": "i2", "hold": 0}]}, "minuten erna"), ({"scene": a, "when": [{"op": "on", "in": "i1"}] * 7}, "maximaal 6")):
                with pytest.raises(ModelError, match=word):
                    hub.scn.update_block(bid, {"rules": [ok, rule]})
            assert hub.scn.rules.items[bid]["rules"] == [ok], "een weigering verandert niets"
            with pytest.raises(ModelError, match="maximaal 24"):
                hub.scn.update_block(bid, {"rules": [ok] * 25})
            with pytest.raises(ModelError):  # een ingang die een regel gebruikt kan niet van soort veranderen of weg
                hub.scn.update_block(bid, {"inputs": [{"id": "i1", "kind": "time"}, {"id": "i2"}, {"id": "i3"}]})
            with pytest.raises(ModelError):
                hub.scn.update_block(bid, {"inputs": [{"id": "i2"}, {"id": "i3"}]})
            hub.scn.update_block(bid, {"inputs": [{"id": "i1", "name": "Anders"}, {"id": "i2"}, {"id": "i3"}, {"name": "Nieuw", "kind": "time"}]})
            assert [i["id"] for i in hub.scn.rules.items[bid]["inputs"]] == ["i1", "i2", "i3", "i4"] and hub.scn.rules.items[bid]["inputs"][0]["name"] == "Anders"
            hub.scn.update_block(bid, {"inputs": [{"id": "i1"}, {"id": "i2"}, {"id": "i3"}]})
            hub.scn.update_block(bid, {"inputs": [{"id": "i1"}, {"id": "i2"}, {"id": "i3"}, {"name": "Weer"}]})
            assert hub.scn.rules.items[bid]["inputs"][-1]["id"] == "i5", "ids worden nooit hergebruikt"
            # een wekker op een 0/1 vroeger laten beginnen kan alleen met een timer
            tm = r.model.save_timer(None, "Wekker", {"mon": [{"from": "07:00", "to": "08:00"}]})
            n = r.model.add_module("number", "Getal", 5)
            hi = r.model.add_module("above", "Hoog", 0.5)
            r.model.set_module(hi, inputs={"a": f"m:{n}"})
            hub.scn.update_block(bid, {"refs": {"i1": f"m:{hi}"}})
            with pytest.raises(ModelError, match="alleen met een timer"):
                hub.scn.update_block(bid, {"rules": [{"scene": a, "when": [{"op": "wake", "in": "i1", "before": 10}]}]})
            hub.scn.update_block(bid, {"refs": {"i1": f"t:{tm}"}})
            hub.scn.update_block(bid, {"rules": [{"scene": a, "when": [{"op": "wake", "in": "i1", "before": 10}]}]})
            with pytest.raises(ModelError, match="alleen met een timer"):
                hub.scn.update_block(bid, {"refs": {"i1": f"m:{hi}"}})
            # een scène weghalen haalt haar regels weg
            hub.scn.delete(a)
            assert hub.scn.rules.items[bid]["rules"] == [] and hub.devices[did]["scn"]["sel"] == []
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


# ------------------------------------------------------------ de sync

def test_sync_fills_one_slot_per_cycle_and_only_when_it_differs(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            ids = [hub.scn.add({"name": f"S{i}", "def": {"kind": "static", "color": [10 * i + 10, 0, 0]}}) for i in range(3)]
            hub.scn.select(did, ids)
            assert esp.writes == [], "kiezen schrijft nog niets: dat doet de sync"
            for n in (1, 2, 3):
                await cycle(hub, did)
                assert len(esp.writes) == n, "hoogstens een schrijfactie per ronde"
                assert esp.writes[-1] == ("store", n) and esp.slots[n] == hub.scn.lib.bytes[ids[n - 1]]
            posts = len(esp.scene_calls)
            await cycle(hub, did, 5)
            assert len(esp.scene_calls) == posts and len(esp.writes) == 3, "niets te doen: niets sturen"
            # een gewijzigde scène: precies dat slot, een keer
            hub.scn.update(ids[1], {"def": {"kind": "static", "color": [0, 0, 200]}})
            await cycle(hub, did, 4)
            assert esp.writes[3:] == [("store", 2)] and esp.slots[2] == hub.scn.lib.bytes[ids[1]]
            assert hub.last[did]["scenes"] == "slot 2: S1: opgeslagen"
            # een apparaat dat is teruggezet: alles opnieuw, een per ronde (dezelfde (slot, hash) hoogstens eens per 10 minuten, 6 schrijfacties per uur)
            esp.slots.clear()
            await cycle(hub, did, 3)
            assert esp.slots == {}, "net nog geschreven: wachten"
            clock.advance(3601)
            await cycle(hub, did)
            assert len(esp.slots) == 1
            await cycle(hub, did, 3)
            assert len(esp.slots) == 3
            v = hub.status()[0]["scn"]
            assert [x["state"] for x in v["sel"]] == ["ok", "ok", "ok"] and v["have"]["1"]["h"] == hub.scn.lib.hashes[ids[0]]
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_sync_does_not_loop_on_a_device_that_does_not_accept_the_hash(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            a = hub.scn.add({"name": "A", "def": NIGHT})
            hub.scn.select(did, [a])
            esp.wrong_hash = True
            await cycle(hub, did, 6)
            assert len(calls(esp, "store")) == 1, "dezelfde (slot, hash) hoogstens een keer per 10 minuten"
            assert "deadbeef" in hub.last[did]["scenes"] and hub.status()[0]["scn"]["sel"][0]["state"] == "wait"
            clock.advance(599)
            await cycle(hub, did)
            assert len(calls(esp, "store")) == 1
            clock.advance(2)
            await cycle(hub, did)
            assert len(calls(esp, "store")) == 2, "na tien minuten opnieuw"
            esp.wrong_hash = False
            clock.advance(601)
            await cycle(hub, did)
            assert esp.slots[1] == hub.scn.lib.bytes[a] and hub.status()[0]["scn"]["sel"][0]["state"] == "ok"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_sync_writes_at_most_six_times_an_hour(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            ids = [hub.scn.add({"name": f"S{i}", "def": {"kind": "static", "color": [i + 1, 0, 0]}}) for i in range(8)]
            hub.scn.select(did, ids)
            esp.wrong_hash = True  # blijft afwijken: elke ronde probeert het volgende slot
            await cycle(hub, did, 12)
            assert len(calls(esp, "store")) == 6 and "al 6 schrijfacties" in hub.last[did]["scenes"]
            clock.advance(3601)
            await cycle(hub, did)
            assert len(calls(esp, "store")) == 7, "een uur later weer ruimte"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_sync_clears_slots_that_no_selected_scene_owns(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            a, b = hub.scn.add({"name": "A", "def": NIGHT}), hub.scn.add({"name": "B", "def": RAINBOW})
            hub.scn.select(did, [a, b])
            await fill(hub, did, esp)
            hub.scn.select(did, [b])
            await cycle(hub, did)
            assert calls(esp, "store")[-1] == ("store", {"slot": 1, "data": ""}) and sorted(esp.slots) == [2], "weg uit de selectie: het slot wordt gewist"
            n = len(esp.scene_calls)
            await cycle(hub, did, 3)
            assert len(esp.scene_calls) == n
            esp.slots[6] = sc.compile_def(NIGHT)  # iets wat er niet hoort (met de hand of een oude add-on)
            await cycle(hub, did)
            assert sorted(esp.slots) == [2]
            hub.scn.select(did, [])
            await cycle(hub, did)
            assert esp.slots == {}
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_sync_refuses_an_unknown_format_and_a_too_large_scene(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch, esp=FakeEsp(hid="rgb-000001", type="rgb").point("x", "X", "int", 1).scenes(formats=("ledscene/2",)))
        try:
            a = hub.scn.add({"name": "A", "def": NIGHT})
            hub.scn.select(did, [a])
            await cycle(hub, did, 3)
            assert calls(esp) == [] and "ledscene/1 niet" in hub.last[did]["scenes"] and "ledscene/2" in hub.last[did]["scenes"], "geen bewerking, een melding"
            with pytest.raises(ModelError, match="ledscene/1 niet"):
                await hub.scn.play(did, {"id": a})
            assert calls(esp) == []
        finally:
            await shutdown(hub, esp)
        (tmp_path / "b").mkdir()
        r, hub, esp, did, clock = await env(tmp_path / "b", monkeypatch, esp=kidswekker(slots=2, max_bytes=100))
        try:
            big, small = hub.scn.add({"name": "Groot", "def": WAKE}), hub.scn.add({"name": "Klein", "def": NIGHT})
            hub.scn.select(did, [big, small])
            await cycle(hub, did, 4)
            assert sorted(esp.slots) == [2] and len(hub.scn.lib.bytes[big]) > 100, "de grote past niet: niet eens geprobeerd; de kleine wel"
            assert [x["state"] for x in hub.status()[0]["scn"]["sel"]] == ["toolarge", "ok"]
            with pytest.raises(ModelError, match="te groot|hoogstens 100"):
                await hub.scn.play(did, {"id": big})
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_the_poll_loop_syncs_by_itself(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch, manual=False)
        try:
            a, b = hub.scn.add({"name": "A", "def": NIGHT}), hub.scn.add({"name": "B", "def": WAKE})
            hub.scn.select(did, [a, b])
            await eventually(lambda: sorted(esp.slots) == [1, 2])
            assert esp.slots[1] == hub.scn.lib.bytes[a] and esp.slots[2] == hub.scn.lib.bytes[b]
            await asyncio.sleep(0.3)
            assert [w for w in esp.writes if w[0] == "store"] == [("store", 1), ("store", 2)], "daarna rustig"
            assert did in hub.online
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


# ------------------------------------------------------------ de ingangen

async def wired(tmp_path, monkeypatch, release="stop", **kw):
    r, hub, esp, did, clock = await env(tmp_path, monkeypatch, **kw)
    sid = hub.scn.add({"name": "Nachtlicht", "def": NIGHT, "release": release})
    hub.scn.select(did, [sid])
    setter, src = switch(r)
    wire(hub, did, sid, src)
    return r, hub, esp, did, clock, sid, setter


def test_first_value_is_only_remembered_then_0_to_1_starts_from_the_slot(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, setter = await wired(tmp_path, monkeypatch)
        try:
            await fill(hub, did, esp)
            setter(True)  # de bron staat al op 1 bij het begin
            hub.tick()
            hub.tick()
            await asyncio.sleep(0.05)
            assert calls(esp, "play") == [] and mem(hub, did) is True, "de eerste stand wordt alleen onthouden: een herstart start niets"
            setter(False)
            hub.tick()
            await asyncio.sleep(0.05)
            assert calls(esp, "stop") == [] and calls(esp, "play") == [], "1 naar 0 na de eerste stand: stop, maar er speelt niets"
            await settle(hub, did)
            setter(True)
            hub.tick()
            await settle(hub, did)
            assert calls(esp, "play") == [("play", {"slot": 1})], "een slot met de juiste hash: play met het slot"
            assert esp.run["h"] == hub.scn.lib.hashes[sid] and esp.run["slot"] == 1
            assert hub.last[did][f"scene:{sid}"] == "Nachtlicht: gestart"
            assert r.events_of("espdev", what="scene", op="start", device=did)
            for _ in range(3):
                hub.tick()
            await asyncio.sleep(0.05)
            assert len(calls(esp, "play")) == 1, "een ongewijzigde ingang stuurt niets opnieuw"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_after_unknown_the_next_value_is_a_first_value_again(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            sid = hub.scn.add({"name": "Nachtlicht", "def": NIGHT})
            hub.scn.select(did, [sid])
            n = r.model.add_module("number", "Waarde", 0)
            above = r.model.add_module("above", "Hoog", 0.5)
            r.model.set_module(above, inputs={"a": f"m:{n}"})
            wire(hub, did, sid, f"m:{above}")
            hub.tick()
            assert mem(hub, did) is False
            r.model.set_module(above, inputs={"a": None})  # de bron wordt onbekend
            hub.tick()
            assert mem(hub, did) is None, "onbekend en geen regel die klopt: niets onthouden"
            r.model.set_module(n, value=1)
            r.model.set_module(above, inputs={"a": f"m:{n}"})
            hub.tick()
            await asyncio.sleep(0.05)
            assert mem(hub, did) is True and calls(esp) == [], "alleen onthouden"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_start_goes_direct_with_the_bytes_when_the_slot_is_not_filled_yet(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, setter = await wired(tmp_path, monkeypatch)
        try:
            hub.tick()  # eerste stand 0
            setter(True)
            hub.tick()
            await settle(hub, did)
            assert calls(esp, "play") == [("play", {"data": base64.b64encode(hub.scn.lib.bytes[sid]).decode()})], "een live trigger wacht niet op de sync en gebruikt de actuele bibliotheek"
            assert esp.run["slot"] == 0 and hub.last[did][f"scene:{sid}"] == "Nachtlicht: gestart (rechtstreeks)"
            # de scène is intussen gewijzigd: het slot (nog de oude hash) wordt niet gebruikt
            await fill(hub, did, esp)
            hub.scn.update(sid, {"def": {"kind": "static", "color": "#00ff00"}})
            setter(False)
            hub.tick()
            await settle(hub, did)
            esp.run = None
            setter(True)
            hub.tick()
            await settle(hub, did)
            assert calls(esp, "play")[-1][1].keys() == {"data"} and base64.b64decode(calls(esp, "play")[-1][1]["data"]) == hub.scn.lib.bytes[sid]
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_1_to_0_stops_only_with_release_stop_and_only_what_it_started(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, setter = await wired(tmp_path, monkeypatch)
        try:
            await fill(hub, did, esp)
            hub.tick()
            setter(True)
            hub.tick()
            await settle(hub, did)
            setter(False)
            hub.tick()
            await settle(hub, did)
            assert calls(esp, "stop") == [("stop", {"slot": 1})] and esp.run is None and hub.last[did][f"scene:{sid}"] == "Nachtlicht: gestopt"
            # een rechtstreekse start (het slot is leeg): stop {} en alleen als dezelfde scène nog speelt
            esp.slots.clear()
            hub.scn.rt[did]["have"].clear()
            setter(True)
            hub.tick()
            await settle(hub, did)
            assert calls(esp, "play")[-1][1].keys() == {"data"} and esp.run["slot"] == 0
            setter(False)
            hub.tick()
            await settle(hub, did)
            assert calls(esp, "stop")[-1] == ("stop", {}) and esp.run is None, "direct gestart: stop zonder slot"
            # iets anders is intussen gaan spelen (met de hand): dat blijft ongemoeid
            setter(True)
            hub.tick()
            await settle(hub, did)
            esp.run = {"slot": 0, "h": "0badf00d", "until": None}
            before = len(calls(esp, "stop"))
            setter(False)
            hub.tick()
            await settle(hub, did)
            assert len(calls(esp, "stop")) == before and esp.run["h"] == "0badf00d" and "speelt niet meer" in hub.last[did][f"scene:{sid}"]
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_release_finish_never_stops(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, setter = await wired(tmp_path, monkeypatch, release="finish")
        try:
            await fill(hub, did, esp)
            hub.tick()
            setter(True)
            hub.tick()
            await settle(hub, did)
            setter(False)
            hub.tick()
            await asyncio.sleep(0.1)
            await settle(hub, did)
            assert len(calls(esp, "play")) == 1 and calls(esp, "stop") == [] and esp.run is not None, "laten uitlopen"
            hub.scn.update(sid, {"release": "stop"})  # de wijziging geldt vanaf de volgende flank
            setter(True)
            hub.tick()
            await settle(hub, did)
            setter(False)
            hub.tick()
            await settle(hub, did)
            assert len(calls(esp, "stop")) == 1
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_0_1_0_within_five_seconds_loses_no_stop_and_the_order_is_the_order_of_the_flanks(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, setter = await wired(tmp_path, monkeypatch)
        try:
            await fill(hub, did, esp)
            hub.tick()
            for on in (True, False, True, False):  # vier flanken zonder te wachten
                setter(on)
                hub.tick()
            await settle(hub, did)
            ops = [o for o, _ in calls(esp) if o in ("play", "stop")]
            assert ops == ["play", "stop", "play", "stop"] and esp.run is None, "een verzender: de volgorde van de flanken, de laatste wint"
            hub.tick()
            for on in (True, False, True):
                setter(on)
                hub.tick()
            await settle(hub, did)
            assert esp.run is not None and esp.run["h"] == hub.scn.lib.hashes[sid], "de laatste flank was een start"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_an_offline_start_expires_after_60_s(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, setter = await wired(tmp_path, monkeypatch)
        try:
            await fill(hub, did, esp)
            hub.tick()
            hub.online.discard(did)  # het apparaat valt weg
            setter(True)
            hub.tick()
            await asyncio.sleep(0.1)
            assert calls(esp, "play") == [] and hub.scn.q[did], "wacht op het apparaat"
            clock.advance(61)
            await settle(hub, did)  # de start vervalt
            hub.online.add(did)
            await asyncio.sleep(0.1)
            assert calls(esp, "play") == [] and esp.run is None, "te laat: niet meer starten"
            assert "vervallen" in hub.last[did][f"scene:{sid}"] and "60 s" in hub.last[did][f"scene:{sid}"]
            # een start die binnen 60 s kan, gaat wel
            hub.online.discard(did)
            setter(False)
            hub.tick()
            setter(True)
            hub.tick()
            clock.advance(30)
            await asyncio.sleep(0.05)
            hub.online.add(did)
            await settle(hub, did)
            assert len(calls(esp, "play")) == 1 and esp.run is not None
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_an_offline_stop_waits_and_is_sent_when_the_device_is_back(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, setter = await wired(tmp_path, monkeypatch)
        try:
            await fill(hub, did, esp)
            hub.tick()
            setter(True)
            hub.tick()
            await settle(hub, did)
            assert esp.run is not None
            hub.online.discard(did)
            setter(False)
            hub.tick()
            clock.advance(600)  # lang weg
            await asyncio.sleep(0.1)
            assert calls(esp, "stop") == [] and esp.run is not None
            hub.online.add(did)
            await settle(hub, did)
            assert calls(esp, "stop") == [("stop", {"slot": 1})] and esp.run is None, "een stop blijft geldig"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_an_offline_stop_is_dropped_when_a_newer_start_followed(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, setter = await wired(tmp_path, monkeypatch)
        try:
            await fill(hub, did, esp)
            hub.tick()
            setter(True)
            hub.tick()
            await settle(hub, did)
            hub.online.discard(did)
            setter(False)
            hub.tick()
            setter(True)
            hub.tick()  # intussen een nieuwere start
            clock.advance(10)
            await asyncio.sleep(0.05)
            hub.online.add(did)
            await settle(hub, did)
            assert calls(esp, "stop") == [] and esp.run is not None, "de stop is achterhaald door de nieuwere start"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_failure_does_not_wait_forever(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, setter = await wired(tmp_path, monkeypatch)
        try:
            await fill(hub, did, esp)
            hub.tick()
            esp.reject = "scène ongeldig"
            setter(True)
            hub.tick()
            await settle(hub, did)
            assert len(calls(esp, "play")) == 2, "geweigerd door het apparaat: het slot, dan eenmaal rechtstreeks, en niet opnieuw"
            await asyncio.sleep(0.15)
            assert len(calls(esp, "play")) == 2
            assert "niet verstuurd" in hub.last[did][f"scene:{sid}"] and "scène ongeldig" in hub.last[did][f"scene:{sid}"] and not hub.scn.q[did]
            esp.reject = None
            # een netwerkfout (het apparaat staat nog als online): een paar keer proberen, dan opgeven
            monkeypatch.setattr(scenehub, "MAX_TRIES", 3)

            async def down(*a, **k):
                raise eh.EspError(0, "net", "geen verbinding met 127.0.0.1: net weg")
            monkeypatch.setattr(hub, "_call", down)  # het apparaat staat nog als online, maar het netwerk valt weg (op Windows duurt een echte weigering twee seconden)
            setter(False)
            hub.tick()
            setter(True)
            hub.tick()
            await settle(hub, did)
            assert "niet verstuurd" in hub.last[did][f"scene:{sid}"] and "geen verbinding" in hub.last[did][f"scene:{sid}"] and not hub.scn.q[did]
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_the_rules_of_a_device_without_scenes_sleep_and_wake_up_again(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, setter = await wired(tmp_path, monkeypatch, manual=False)
        try:
            bid = hub.scn.rules.device_owner()[did]
            hub.tick()
            esp.scene_cap = None  # het device verliest zijn scènes (firmware zonder)
            await eventually(lambda: hub.devices[did]["scn"]["cap"] is None)
            assert hub.scn.rules.items[bid]["rules"] and hub.devices[did]["scn"]["sel"] == [sid], "de regels blijven bewaard"
            assert hub.scn.rules_view()[0]["ready"] is False, "maar slapend"
            n = len(esp.scene_calls)
            setter(True)
            hub.tick()
            await asyncio.sleep(0.2)
            assert len(esp.scene_calls) == n and bid not in hub.scn.rmem, "geen scenefunctionaliteit"
            esp.scenes()
            await eventually(lambda: hub.devices[did]["scn"]["cap"] is not None)
            assert hub.devices[did]["scn"]["slots"] == {sid: 1} and hub.scn.rules_view()[0]["ready"] is True
            hub.tick()
            assert mem(hub, did) is True and not calls(esp, "play"), "de eerste stand na het terugkomen wordt alleen onthouden"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


# ------------------------------------------------------------ handmatig bedienen

def test_manual_play_and_stop(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            a = hub.scn.add({"name": "A", "def": NIGHT})
            hub.scn.select(did, [a])
            await fill(hub, did, esp)
            assert (await hub.scn.play(did, {"id": a}))["ok"] and esp.run["slot"] == 1
            n = len(esp.play_started)
            await hub.scn.play(did, {"id": a})
            assert len(esp.play_started) == n, "dezelfde scène die al speelt: niets (idempotent)"
            assert (await hub.scn.stop(did, {"id": a}))["message"] == "A: gestopt" and esp.run is None
            assert (await hub.scn.stop(did, {"id": a}))["message"].endswith("niets te stoppen")
            await hub.scn.play(did, {"slot": 1})
            assert esp.run["slot"] == 1
            await hub.scn.stop(did, {"slot": 2})
            assert esp.run is not None, "een ander slot: laat staan"
            await hub.scn.stop(did, {})
            assert esp.run is None and calls(esp, "stop")[-1] == ("stop", {})
            for b, word in (({"slot": 5}, "leeg"), ({"slot": 9}, "nummer"), ({"slot": "1"}, "nummer"), ({"id": "s99"}, "onbekende scène"), ({}, "nummer")):
                with pytest.raises(ModelError, match=word):
                    await hub.scn.play(did, b)
            with pytest.raises(ModelError, match="nummer"):
                await hub.scn.stop(did, {"slot": 0})
            hub.online.discard(did)
            with pytest.raises(ModelError, match="geen verbinding"):
                await hub.scn.play(did, {"id": a})
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_the_device_errors_are_translated(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            a = hub.scn.add({"name": "A", "def": NIGHT})
            hub.scn.select(did, [a])
            dev = hub.devices[did]
            for path, body, word in (("/huisdev/scene/store", {"slot": 9, "data": ""}, "slot bestaat niet"), ("/huisdev/scene/store", {"slot": 1, "data": "@@@"}, "geen geldige base64"),
                                     ("/huisdev/scene/store", {"slot": 1, "data": base64.b64encode(b"x" * 2000).decode()}, "te groot")):
                with pytest.raises(scenehub._Fail, match=word):
                    await hub.scn._post(did, path, body)
            with pytest.raises(scenehub._Fail, match="ongeldig|lengte|magic"):
                await hub.scn._post(did, "/huisdev/scene/store", {"slot": 1, "data": base64.b64encode(b"kapot kapot kapot kapot").decode()})
            esp.token = "een-ander-token-dat-niet-klopt-1234"
            with pytest.raises(scenehub._Fail, match="token"):
                await hub.scn._post(did, "/huisdev/scene/stop", {})
            assert dev["token"] != esp.token
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


# ------------------------------------------------------------ geplande starts

def test_rising_edges_weekdays_intervals_and_contiguous_intervals(tmp_path):
    now = local(2027, 1, 4, 12, 0)  # maandag
    wk = {"days": {d: [{"from": "07:00", "to": "08:00"}] for d in ("mon", "tue", "wed", "thu", "fri")}}
    got = [fmt(e) for e in scenehub.rising_edges(wk, now, 8, AMS)]
    assert got == ["2027-01-05 07:00", "2027-01-06 07:00", "2027-01-07 07:00", "2027-01-08 07:00", "2027-01-11 07:00", "2027-01-12 07:00", "2027-01-13 07:00", "2027-01-14 07:00"], "het weekend blijft stil"
    assert [fmt(e) for e in scenehub.rising_edges(wk, now, 3, AMS)] == got[:3]
    # twee intervallen op een dag, en een andere tijd op zaterdag
    two = {"days": {"mon": [{"from": "06:00", "to": "07:00"}, {"from": "18:30", "to": "19:00"}], "sat": [{"from": "09:15", "to": "10:00"}]}}
    assert [fmt(e) for e in scenehub.rising_edges(two, now, 8, AMS)] == ["2027-01-04 18:30", "2027-01-09 09:15", "2027-01-11 06:00", "2027-01-11 18:30", "2027-01-16 09:15"]
    # een interval dat aansluit op het vorige is geen flank
    cont = {"days": {"mon": [{"from": "06:00", "to": "07:00"}, {"from": "07:00", "to": "08:00"}, {"from": "23:00", "to": "00:00"}], "tue": [{"from": "00:00", "to": "01:00"}]}}
    assert [fmt(e) for e in scenehub.rising_edges(cont, local(2027, 1, 4, 5, 0), 8, AMS)] == ["2027-01-04 06:00", "2027-01-04 23:00", "2027-01-11 06:00", "2027-01-11 23:00"], "07:00 en 00:00 sluiten aan"
    # wat al loopt, telt niet: die flank hebben we live gezien
    assert [fmt(e) for e in scenehub.rising_edges(wk, local(2027, 1, 5, 7, 30), 2, AMS)] == ["2027-01-06 07:00", "2027-01-07 07:00"]
    assert scenehub.rising_edges(wk, local(2027, 1, 5, 6, 59) + 30, 1, AMS) == [local(2027, 1, 5, 7, 0)], "in de laatste halve minuut ervoor"
    assert scenehub.rising_edges({"days": {}}, now, 8, AMS) == [] and scenehub.rising_edges({"days": {"mon": [{"from": "00:00", "to": "00:00"}], "tue": [{"from": "00:00", "to": "00:00"}], "wed": [{"from": "00:00", "to": "00:00"}], "thu": [{"from": "00:00", "to": "00:00"}], "fri": [{"from": "00:00", "to": "00:00"}], "sat": [{"from": "00:00", "to": "00:00"}], "sun": [{"from": "00:00", "to": "00:00"}]}}, now, 8, AMS) == [], "altijd aan: nooit een flank"
    weekly = {"days": {"wed": [{"from": "12:00", "to": "13:00"}]}}
    assert [fmt(e) for e in scenehub.rising_edges(weekly, now, 8, AMS)] == ["2027-01-06 12:00", "2027-01-13 12:00"], "hoogstens 13 dagen vooruit"


def test_rising_edges_across_daylight_saving_changes():
    utc = lambda *a: int(datetime(*a, tzinfo=timezone.utc).timestamp())  # noqa: E731
    daily = {"days": {d: [{"from": "07:00", "to": "08:00"}] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}}
    e = scenehub.rising_edges(daily, local(2027, 3, 26, 12, 0), 3, AMS)
    assert [fmt(x) for x in e] == ["2027-03-27 07:00", "2027-03-28 07:00", "2027-03-29 07:00"], "7 uur blijft 7 uur"
    assert e[1] - e[0] == 23 * 3600 and e[2] - e[1] == 24 * 3600, "de dag van de omschakeling is 23 uur lang"
    # lente: 02:30 bestaat niet; het interval loopt wel (tot 04:00) en begint dus bij de eerste minuut die er is
    spring = {"days": {"sun": [{"from": "02:30", "to": "04:00"}]}}
    assert scenehub.rising_edges(spring, local(2027, 3, 27, 12, 0), 2, AMS)[:1] == [utc(2027, 3, 28, 1, 0)]
    assert fmt(scenehub.rising_edges(spring, local(2027, 3, 27, 12, 0), 2, AMS)[0]) == "2027-03-28 03:00"
    assert fmt(scenehub.rising_edges(spring, local(2027, 3, 27, 12, 0), 2, AMS)[1]) == "2027-04-04 02:30"
    # herfst: 02:30 komt twee keer voor: zo ziet de engine het ook (twee keer 0 naar 1)
    autumn = {"days": {"sun": [{"from": "02:30", "to": "02:45"}]}}
    assert scenehub.rising_edges(autumn, local(2027, 10, 30, 12, 0), 3, AMS)[:2] == [utc(2027, 10, 31, 0, 30), utc(2027, 10, 31, 1, 30)]
    assert all(x % 60 == 0 for x in e)


def test_needs_write_has_a_low_water_mark_and_ignores_expired_lines():
    ent = lambda *xs: [{"slot": 1, "at": 1_800_000_000 + x * 86400} for x in xs]  # noqa: E731
    w = ent(1, 2, 3, 4, 5, 6, 7, 8)
    assert scenehub.needs_write(w, w) is False and scenehub.needs_write([], []) is False
    assert scenehub.needs_write(w[1:], w[1:] + ent(9)) is False, "een verlopen regel is geen reden: het apparaat haalt hem zelf weg"
    assert scenehub.needs_write(w[2:], w[2:] + ent(9, 10)) is False, "zes over: genoeg"
    assert scenehub.needs_write(w[5:], w[5:] + ent(9, 10, 11, 12, 13)) is False, "drie over is nog genoeg"
    assert scenehub.needs_write(ent(7, 8), ent(7, 8, 9, 10, 11, 12, 13, 14)) is True, "minder dan 3 over: aanvullen"
    assert scenehub.needs_write([], w) is True, "leeg terwijl het niet leeg hoort te zijn"
    assert scenehub.needs_write(w, []) is True, "leeg moet het zijn"
    changed = ent(1, 2, 3, 4, 5, 6, 7, 8)
    changed[2] = {"slot": 1, "at": changed[2]["at"] + 1800}
    assert scenehub.needs_write(w, changed) is True, "het overlappende deel verschilt"
    assert scenehub.needs_write(w, [{**e, "slot": 2} for e in w]) is True
    assert scenehub.needs_write(w[:5], w[:5] + ent(9, 10)) is False, "een verlengde lijst met hetzelfde begin, vijf over"
    assert scenehub.needs_write(w[:2], ent(1, 2, 9)) is True
    # een regel die bijna afgaat en die het apparaat nog niet haalde (zijn klok loopt iets achter): geen reden om te herschrijven
    just_now = w[0]["at"]
    assert scenehub.needs_write(w[:4], w[1:4] + ent(9), after=just_now + 90) is False
    assert scenehub.needs_write(w[:4], w[1:4] + ent(9)) is True


async def alarm(tmp_path, monkeypatch, days=None, **kw):
    r, hub, esp, did, clock = await env(tmp_path, monkeypatch, **kw)
    tm = r.model.save_timer(None, "Schooldagen", days or {d: [{"from": "07:00", "to": "08:00"}] for d in ("mon", "tue", "wed", "thu", "fri")})
    sid = hub.scn.add({"name": "Wekker", "def": WAKE, "release": "finish"})
    hub.scn.select(did, [sid])
    wire(hub, did, sid, f"t:{tm}")
    return r, hub, esp, did, clock, sid, tm


def test_the_schedule_is_sent_once_the_slot_is_filled_and_not_rewritten_when_equal(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, tm = await alarm(tmp_path, monkeypatch)
        try:
            await cycle(hub, did)
            assert [w[0] for w in esp.writes] == ["store"] and esp.sched == [], "eerst het slot: een schrijfactie per ronde"
            await cycle(hub, did)
            want = [{"slot": 1, "at": local(2027, 1, d, 7, 0)} for d in (5, 6, 7, 8, 11, 12, 13, 14)]
            assert esp.writes[-1] == ("schedule", want) and calls(esp, "schedule") == [("schedule", {"entries": want})]
            assert "8 geplande starts" in hub.last[did]["scenes"]
            v = hub.status()[0]["scn"]
            assert [(e["slot"], e["at"], e["id"], e["name"]) for e in v["sched"]] == [(1, x["at"], sid, "Wekker") for x in want]
            n = len(esp.scene_calls)
            for _ in range(6):
                clock.advance(3)
                await cycle(hub, did)
            assert len(esp.scene_calls) == n, "dezelfde lijst: niets sturen"
            clock.advance(86400 * 2)  # twee dagen verder: het apparaat heeft zes regels over (en is twee keer begonnen)
            await cycle(hub, did)
            assert len(esp.sched) == 6 and len(esp.scene_calls) == n, "laagwatermerk: niet herschrijven bij elke verlopen regel"
            assert esp.play_started and esp.play_started[0][0] == 1, "het apparaat startte zelf, zonder de add-on"
            clock.advance(86400 * 5)  # zondag 10 januari 12:00 + 1 dag: maandag 11 januari 12:00
            await cycle(hub, did)
            assert [fmt(e["at"]) for e in esp.sched] == ["2027-01-12 07:00", "2027-01-13 07:00", "2027-01-14 07:00"] and len(esp.scene_calls) == n, "drie over is nog genoeg"
            clock.advance(86400)
            await cycle(hub, did)
            assert len(calls(esp, "schedule")) == 2 and len(esp.sched) == 8 and esp.sched[0]["at"] == local(2027, 1, 13, 7, 0), "minder dan 3 regels: aanvullen"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_changed_timer_rewrites_the_overlapping_part(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, tm = await alarm(tmp_path, monkeypatch)
        try:
            await cycle(hub, did, 3)
            assert len(calls(esp, "schedule")) == 1
            r.model.save_timer(tm, "Schooldagen", {d: [{"from": "06:30", "to": "08:00"}] for d in ("mon", "tue", "wed", "thu", "fri")})
            await cycle(hub, did)
            assert len(calls(esp, "schedule")) == 2 and esp.sched[0]["at"] == local(2027, 1, 5, 6, 30), "een gewijzigde timer: de lijst wordt vervangen"
            await cycle(hub, did, 3)
            assert len(calls(esp, "schedule")) == 2
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_the_schedule_of_all_scenes_goes_in_one_sorted_list(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, tm = await alarm(tmp_path, monkeypatch)
        try:
            tm2 = r.model.save_timer(None, "Avond", {d: [{"from": "20:00", "to": "20:30"}] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")})
            night = hub.scn.add({"name": "Slaaplicht", "def": NIGHT})
            wire(hub, did, night, f"t:{tm2}")
            await cycle(hub, did, 4)
            sent = calls(esp, "schedule")[-1][1]["entries"]
            assert len(sent) == 8 and [e["at"] for e in sent] == sorted(e["at"] for e in sent), "samen, gesorteerd, hoogstens `sched` regels"
            assert sent[0] == {"slot": 2, "at": local(2027, 1, 4, 20, 0)} and sent[1] == {"slot": 1, "at": local(2027, 1, 5, 7, 0)} and {e["slot"] for e in sent} == {1, 2}
            assert esp.sched == sent
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_unwiring_or_removing_clears_the_schedule_on_the_next_sync(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, tm = await alarm(tmp_path, monkeypatch)
        try:
            await cycle(hub, did, 3)
            assert len(esp.sched) == 8
            wire(hub, did, sid, None)  # de bron los
            await cycle(hub, did, 2)
            assert esp.sched == [] and calls(esp, "schedule")[-1] == ("schedule", {"entries": []}), "de regels van die scène gaan weg"
            clock.advance(700)  # het slot is net gewist: dezelfde scène hoogstens eens per 10 minuten opnieuw schrijven
            wire(hub, did, sid, f"t:{tm}")
            await cycle(hub, did, 3)
            assert len(esp.sched) == 8
            hub.scn.select(did, [])  # de scène van het apparaat
            await cycle(hub, did, 3)
            assert esp.sched == [] and esp.slots == {}
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_no_schedule_for_a_source_that_cannot_be_computed_or_while_not_ready(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, tm = await alarm(tmp_path, monkeypatch)
        try:
            n = r.model.add_module("number", "Getal", 5)
            above = r.model.add_module("above", "Hoog", 1)
            r.model.set_module(above, inputs={"a": f"m:{n}"})
            wire(hub, did, sid, f"m:{above}")  # een module: niet vooruit te berekenen
            await cycle(hub, did, 4)
            assert calls(esp, "schedule") == [] and "geen timer" in hub.status()[0]["scn"]["sel"][0]["sched_note"], "geen schema, een melding"
            wire(hub, did, sid, f"t:{tm}")
            esp.time_valid = False  # de klok van het apparaat is nog niet gezet
            await cycle(hub, did, 3)
            assert calls(esp, "schedule") == [] and hub.scn.rt[did]["time"] is None
            esp.time_valid = True
            await cycle(hub, did)
            assert len(calls(esp, "schedule")) == 1 and hub.status()[0]["scn"]["sel"][0]["sched_note"] == ""
            # een slot dat nog niet klopt (de bibliotheek is net gewijzigd): wachten tot de sync het slot vult
            hub.scn.update(sid, {"def": {"kind": "wake", "sun_min": 10}})
            esp.wrong_hash = True
            await cycle(hub, did, 3)
            assert len(calls(esp, "schedule")) == 1, "geen schema voor een slot dat niet klopt"
            esp.wrong_hash = False
            clock.advance(700)
            await cycle(hub, did, 3)
            assert esp.slots[1] == hub.scn.lib.bytes[sid]
            # het schema bleef overeind (zelfde lijst): niet nog een keer
            assert len(calls(esp, "schedule")) == 1
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_missing_timer_gives_a_message_not_a_schedule(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, tm = await alarm(tmp_path, monkeypatch)
        try:
            await cycle(hub, did, 3)
            assert len(esp.sched) == 8
            r.model.remove_timer(tm)
            await cycle(hub, did)
            assert hub.status()[0]["scn"]["sel"][0]["sched_note"].startswith("geen geplande start: de timer bestaat niet")
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_the_schedule_is_limited_to_what_the_device_takes(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, tm = await alarm(tmp_path, monkeypatch, esp=kidswekker(sched=3))
        try:
            await cycle(hub, did, 3)
            assert len(esp.sched) == 3 and esp.sched[0]["at"] == local(2027, 1, 5, 7, 0)
        finally:
            await shutdown(hub, esp)
        (tmp_path / "b").mkdir()
        r, hub, esp, did, clock, sid, tm = await alarm(tmp_path / "b", monkeypatch, esp=kidswekker(sched=0))
        try:
            await cycle(hub, did, 3)
            assert calls(esp, "schedule") == []
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_starting_twice_is_harmless_a_scheduled_start_and_a_live_flank(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, tm = await alarm(tmp_path, monkeypatch)
        try:
            await cycle(hub, did, 3)
            clock.advance(local(2027, 1, 5, 7, 0) - clock.epoch)  # het apparaat start de wekker zelf
            await cycle(hub, did)
            assert len(esp.play_started) == 1 and esp.run["slot"] == 1
            await hub.scn.play(did, {"id": sid})  # en nu ook nog de add-on (de gewone flank)
            assert len(esp.play_started) == 1, "play van wat al speelt is een no-op"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


# ------------------------------------------------------------ opruimen bij verwijderen

def test_deleting_a_scene_cleans_up_everywhere_and_the_slot_is_cleared(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, tm = await alarm(tmp_path, monkeypatch)
        try:
            other = hub.scn.add({"name": "Rest", "def": NIGHT})
            wire(hub, did, other, switch(r)[1])
            await cycle(hub, did, 4)
            key = f"scene:{sid}"
            hub.tick()
            hub.last[did][key] = "iets"
            hub.scn.q[did] = __import__("collections").deque([{"kind": "stop", "sid": sid, "t": clock.mono, "tries": 0}])
            assert mem(hub, did) is not None and len(esp.sched) == 8 and sorted(esp.slots) == [1, 2]
            assert hub.scn.delete(sid) is True and hub.scn.delete(sid) is False
            d = hub.devices[did]
            assert d["scn"]["sel"] == [other] and sid not in d["scn"]["slots"] and key not in d["inputs"] and key not in hub.last[did] and not hub.scn.q[did]
            saved = json.loads((tmp_path / "espdev.json").read_text(encoding="utf-8"))["devices"][did]
            assert saved["scn"]["sel"] == [other] and key not in saved["inputs"]
            assert sid not in json.loads((tmp_path / "scenes.json").read_text(encoding="utf-8"))["scenes"]
            await cycle(hub, did)
            assert sorted(esp.slots) == [2] and calls(esp, "store")[-1] == ("store", {"slot": 1, "data": ""}), "de sync wist het slot"
            await cycle(hub, did)
            assert esp.sched == [], "en de regels van die scène"
            assert hub.scn.add({"name": "Nieuw", "def": NIGHT}) not in (sid, other), "het id komt niet terug"
            assert [x["scene"] for x in hub.scn.rules.items[hub.scn.rules.device_owner()[did]]["rules"]] == [other], "de regels van de gewiste scène zijn weg, de rest blijft"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_removing_the_device_cleans_up_its_scene_state(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, setter = await wired(tmp_path, monkeypatch)
        try:
            await fill(hub, did, esp)
            hub.tick()
            setter(True)
            hub.tick()
            await settle(hub, did)
            await hub.remove(did)
            assert did not in hub.scn.rt and did not in hub.scn.q and did not in hub.scn.locks and f"scn:{did}" not in hub.tasks
            assert sid in hub.scn.lib.items, "de bibliotheek blijft"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


# ------------------------------------------------------------ de tijdschuif: scene/seek (protocol 1.2) en helderheid

SUN = {"kind": "sunrise", "minutes": 20}


async def seeker(tmp_path, monkeypatch, gap=0.03, **kw):
    """Een kidswekker met `scene/seek` (1.2) en een korte seek-pauze (de echte is 0,125 s)."""
    monkeypatch.setattr(scenehub, "SEEK_GAP", gap)
    return await env(tmp_path, monkeypatch, esp=kidswekker(seek=True), **kw)


def seeks(esp):
    return [b for _, b in calls(esp, "seek")]


def test_descriptor_keeps_seek_only_when_it_is_true():
    ok = clean_descriptor(kidswekker(seek=True).descriptor())
    assert ok["scenes"] == {"formats": ["ledscene/1"], "slots": 8, "max_bytes": 768, "sched": 8, "seek": True} and ok["proto"] == "1.2"
    assert "seek" not in clean_descriptor(kidswekker().descriptor())["scenes"], "een 1.1-apparaat verandert niet"
    base = kidswekker().descriptor()
    for bad in ("true", 1, "yes", [], None, False):
        assert "seek" not in clean_descriptor({**base, "scenes": {**base["scenes"], "seek": bad}})["scenes"], bad
    assert kidswekker(seek=True).rev != kidswekker().rev, "de rev rekent `seek` mee"
    assert scenehub.clean_scn({"sel": [], "slots": {}, "cap": ok["scenes"]})["cap"]["seek"] is True, "en het blijft bewaard"


def test_run_seek_is_read_and_only_its_presence_counts(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await seeker(tmp_path, monkeypatch)
        try:
            on = hub.scn.on_state
            base = {"have": {}, "run": {"slot": 0, "h": "a1b2c3d4", "until": None}, "sched": [], "time": 1_799_000_000}
            assert on(did, base) is True
            assert hub.scn.rt[did]["run"] == {"slot": 0, "h": "a1b2c3d4", "until": None}, "zonder seek verandert de vorm niet"
            assert on(did, {**base, "run": {**base["run"], "seek": 1000}}) is True and hub.scn.rt[did]["run"]["seek"] == 1000, "een seek-beeld verschijnt"
            assert on(did, {**base, "run": {**base["run"], "seek": 5000}}) is False and hub.scn.rt[did]["run"]["seek"] == 5000, "het tijdstip telt niet (de pagina ververst niet bij elke poll)"
            assert on(did, base) is True and "seek" not in hub.scn.rt[did]["run"], "en verdwijnt"
            for bad in (-1, "5", 1.5, True, None):
                on(did, {**base, "run": {**base["run"], "seek": bad}})
                assert "seek" not in hub.scn.rt[did]["run"], bad
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_seek_sends_the_bytes_first_and_then_only_t_and_again_the_bytes_on_a_change(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await seeker(tmp_path, monkeypatch)
        try:
            got = await hub.scn.seek(did, {"def": SUN, "t_ms": 60_000})
            assert got == {"ok": True, "message": "toont 1:00"}
            data = base64.b64encode(sc.compile_def(SUN)).decode()
            assert seeks(esp) == [{"data": data, "t": 60_000}], "de eerste keer met de bytes"
            assert esp.seeking["t"] == 60_000 and esp.run["h"] == sc.hash_hex(sc.compile_def(SUN)) and esp.run["until"] is None
            for t in (61_000, 90_000, 0):
                assert (await hub.scn.seek(did, {"def": SUN, "t_ms": t}))["ok"]
                await asyncio.sleep(scenehub.SEEK_GAP)
            assert seeks(esp)[1:] == [{"t": 61_000}, {"t": 90_000}, {"t": 0}], "daarna alleen het tijdstip (spaart het wifi-verkeer)"
            await cycle(hub, did)  # een poll ertussen: de state toont het seek-beeld, dus nog steeds alleen t
            await hub.scn.seek(did, {"def": SUN, "t_ms": 5})
            assert seeks(esp)[-1] == {"t": 5}
            await asyncio.sleep(scenehub.SEEK_GAP)
            await hub.scn.seek(did, {"def": {**SUN, "brightness": 50}, "t_ms": 5})
            assert seeks(esp)[-1] == {"data": base64.b64encode(sc.compile_def({**SUN, "brightness": 50})).decode(), "t": 5}, "een gewijzigde definitie: weer met de bytes"
            await asyncio.sleep(scenehub.SEEK_GAP)
            await hub.scn.seek(did, {"def": {**SUN, "brightness": 50}, "t_ms": 6})
            assert seeks(esp)[-1] == {"t": 6}
            v = hub.status()[0]["scn"]
            assert v["run"]["seek"] == 6 and v["cap"]["seek"] is True
            assert not any(c[0] in ("store", "schedule") for c in esp.scene_calls), "een seek raakt de slots en de geplande starts niet"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_seek_from_the_library_or_a_slot(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await seeker(tmp_path, monkeypatch)
        try:
            a = hub.scn.add({"name": "Zon", "def": SUN})
            hub.scn.select(did, [a])
            await fill(hub, did, esp)
            await hub.scn.seek(did, {"id": a, "t_ms": 1000})
            assert seeks(esp)[-1] == {"data": base64.b64encode(hub.scn.lib.bytes[a]).decode(), "t": 1000}
            v = hub.status()[0]["scn"]["run"]
            assert v["seek"] == 1000 and v["id"] == a and v["name"] == "Zon", "de pagina weet welke scène"
            await asyncio.sleep(scenehub.SEEK_GAP)
            await hub.scn.seek(did, {"slot": 1, "t_ms": 2000})
            assert seeks(esp)[-1] == {"slot": 1, "t": 2000}, "een slot: met het slotnummer"
            await asyncio.sleep(scenehub.SEEK_GAP)
            await hub.scn.seek(did, {"slot": 1, "t_ms": 3000})
            assert seeks(esp)[-1] == {"t": 3000}
            assert hub.status()[0]["scn"]["run"]["name"] == "Zon"
            for body, word in (({"slot": 2, "t_ms": 0}, "leeg"), ({"slot": 9, "t_ms": 0}, "nummer"), ({"id": "s99", "t_ms": 0}, "onbekende scène"), ({"t_ms": 0}, "precies"),
                               ({"id": a, "def": SUN, "t_ms": 0}, "precies"), ({"def": {"kind": "nope"}, "t_ms": 0}, "kind"), ({"def": {**SUN, "brightness": 0}, "t_ms": 0}, "brightness")):
                with pytest.raises(ModelError, match=word):
                    await hub.scn.seek(did, body)
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_seek_validates_t_and_the_size(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await seeker(tmp_path, monkeypatch)
        try:
            for t in (-1, 86_400_001, 1.5, "0", None, True):
                with pytest.raises(ModelError, match="t_ms"):
                    await hub.scn.seek(did, {"def": SUN, "t_ms": t})
            assert (await hub.scn.seek(did, {"def": SUN, "t_ms": 86_400_000.0}))["ok"], "een heel getal als float mag"
            assert seeks(esp)[-1]["t"] == 86_400_000
            hub.devices[did]["scn"]["cap"] = {**hub.devices[did]["scn"]["cap"], "max_bytes": 100}
            with pytest.raises(ModelError, match="hoogstens 100"):
                await hub.scn.seek(did, {"def": SUN, "t_ms": 0})
            assert len(seeks(esp)) == 1, "wat niet past wordt niet gestuurd"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_the_device_refusing_t_alone_gets_the_bytes_again(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await seeker(tmp_path, monkeypatch)
        try:
            await hub.scn.seek(did, {"def": SUN, "t_ms": 1000})
            esp.seeking = esp.run = None  # het apparaat hield het niet meer vast (de 300 s verliep, of het werd herstart), de add-on weet dat nog niet
            await asyncio.sleep(scenehub.SEEK_GAP)
            assert (await hub.scn.seek(did, {"def": SUN, "t_ms": 2000}))["ok"]
            assert [("data" in b, b["t"]) for b in seeks(esp)] == [(True, 1000), (False, 2000), (True, 2000)], "t alleen werd geweigerd, dan met de bytes"
            assert esp.seeking["t"] == 2000
            # zo ook als het apparaat zelf het vasthouden liet verlopen (300 s na de laatste seek) en de add-on het in een poll zag
            clock.advance(301)
            await cycle(hub, did)
            assert esp.seeking is None and hub.scn.rt[did]["run"] is None
            await asyncio.sleep(scenehub.SEEK_GAP)
            await hub.scn.seek(did, {"def": SUN, "t_ms": 3000})
            assert "data" in seeks(esp)[-1]
            # een echte weigering (een ongeldige scène op het apparaat) blijft een foutmelding
            esp.reject = "kapot"
            await asyncio.sleep(scenehub.SEEK_GAP)
            with pytest.raises(ModelError, match="kapot"):
                await hub.scn.seek(did, {"def": {**SUN, "minutes": 30}, "t_ms": 0})
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_seeks_are_merged_the_newest_wins_and_at_most_8_a_second(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await seeker(tmp_path, monkeypatch, gap=0.1)
        try:
            await hub.scn.seek(did, {"def": SUN, "t_ms": 0})
            # een schuif: twintig seeks in 0,2 s
            tasks, t0 = [], time.monotonic()
            for i in range(1, 21):
                tasks.append(asyncio.ensure_future(hub.scn.seek(did, {"def": SUN, "t_ms": i * 1000})))
                await asyncio.sleep(0.01)
            res = await asyncio.gather(*tasks)
            most = int((time.monotonic() - t0) / 0.1) + 1  # hoogstens een per pauze van 0,1 s (op een trage machine duurt de schuif langer, dus mogen het er meer zijn)
            sent = [r for r in res if not r.get("superseded")]
            assert 1 <= len(sent) <= most and res[-1] == {"ok": True, "message": "toont 0:20"}, "de laatste (het nieuwste tijdstip) is wat bleef staan"
            assert all(r["ok"] for r in res) and sum(1 for r in res if r.get("superseded")) >= 20 - most, "wat tussendoor vervangen werd, kreeg een gewoon antwoord"
            assert esp.seeking["t"] == 20_000 and seeks(esp)[-1] == {"t": 20_000}
            assert len(seeks(esp)) <= most + 1 and len(seeks(esp)) < 21, "0,2 s bij een pauze van 0,1 s: een paar, geen twintig"
            assert [b["t"] for b in seeks(esp)] == sorted(b["t"] for b in seeks(esp)), "nooit een oudere na een nieuwere"
            await eventually(lambda: f"seek:{did}" not in hub.tasks)
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_one_seek_request_at_a_time_and_the_newest_is_sent_after_a_slow_one(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await seeker(tmp_path, monkeypatch, gap=0.001)
        try:
            real, state = hub.scn._post, {"now": 0, "max": 0, "order": []}

            async def slow(did_, path, body):
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
                state["order"].append(body.get("t"))
                await asyncio.sleep(0.05)
                try:
                    return await real(did_, path, body)
                finally:
                    state["now"] -= 1
            hub.scn._post = slow
            tasks = [asyncio.ensure_future(hub.scn.seek(did, {"def": SUN, "t_ms": 1000}))]
            await asyncio.sleep(0.02)  # de eerste is onderweg
            for t in (2000, 3000, 4000, 5000):
                tasks.append(asyncio.ensure_future(hub.scn.seek(did, {"def": SUN, "t_ms": t})))
                await asyncio.sleep(0.005)
            res = await asyncio.gather(*tasks)
            assert state["max"] == 1, "nooit twee verzoeken tegelijk"
            assert state["order"] == [1000, 5000], "wat tijdens het sturen binnenkwam: alleen het nieuwste"
            assert [bool(x.get("superseded")) for x in res] == [False, True, True, True, False]
            assert esp.seeking["t"] == 5000
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_seeks_do_not_touch_sync_schedule_slots_or_the_hourly_limits(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, tm = await alarm(tmp_path, monkeypatch, esp=kidswekker(seek=True))
        monkeypatch.setattr(scenehub, "SEEK_GAP", 0.0)
        try:
            await cycle(hub, did, 3)
            rt = hub.scn.rt[did]
            before = (list(esp.writes), dict(esp.slots), list(esp.sched), len(rt["stores"]), len(rt["sched_tries"]), dict(hub.devices[did]["scn"]["slots"]), dict(rt["tried"]), rt["sched_sent"])
            assert before[0] == [("store", 1), ("schedule", esp.sched)] and esp.sched
            for i in range(40):
                await hub.scn.seek(did, {"def": SUN, "t_ms": i * 1000})
                if i % 5 == 0:
                    await cycle(hub, did)  # de sync loopt ertussen
            await hub.scn.seek(did, {"id": sid, "t_ms": 1000})
            await cycle(hub, did, 2)
            assert before == (list(esp.writes), dict(esp.slots), list(esp.sched), len(rt["stores"]), len(rt["sched_tries"]), dict(hub.devices[did]["scn"]["slots"]), dict(rt["tried"]), rt["sched_sent"])
            assert not [c for c in esp.scene_calls if c[0] in ("store", "schedule")][2:], "geen enkele schrijfactie erbij"
            assert esp.seeking and hub.scn.rt[did]["run"]["seek"] == esp.seeking["t"]
            # en het plafond van 6 schrijfacties per uur is nog helemaal vrij voor wie het nodig heeft
            for n in range(2, 7):
                other = hub.scn.add({"name": f"Extra {n}", "def": {"kind": "static", "color": [n, 2, 3]}})
                hub.scn.select(did, hub.devices[did]["scn"]["sel"] + [other])
                await cycle(hub, did)
            assert len([w for w in esp.writes if w[0] == "store"]) == 6 == len(rt["stores"]), "de sync schreef zijn zes slots: het plafond telde alleen die, niet de seeks"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_device_without_seek_gets_a_clear_message_and_no_request(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)  # een 1.1-kidswekker
        try:
            for body in ({"def": SUN, "t_ms": 0}, {}):
                with pytest.raises(ModelError, match="kan niet door een scène scrollen.*ouder dan 1.2"):
                    await hub.scn.seek(did, body)
            with pytest.raises(ModelError, match="firmware ouder dan 1.2"):
                await hub.scn.seek_stop(did)
            assert not any("/seek" in c[1] for c in esp.calls) and not hub.scn.rt[did]["seek_q"]
            assert f"seek:{did}" not in hub.tasks
            with pytest.raises(ModelError, match="onbekend apparaat"):
                await hub.scn.seek("nee", {"def": SUN, "t_ms": 0})
        finally:
            await shutdown(hub, esp)
        (tmp_path / "pool").mkdir()
        r, hub, esp, did, clock = await env(tmp_path / "pool", monkeypatch, esp=pool(), manual=False)  # een apparaat zonder scènes
        try:
            with pytest.raises(ModelError, match="scrollen"):
                await hub.scn.seek(did, {"def": SUN, "t_ms": 0})
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_seek_on_an_offline_device_is_refused_and_not_queued(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await seeker(tmp_path, monkeypatch)
        try:
            hub.online.discard(did)
            with pytest.raises(ModelError, match="geen verbinding"):
                await hub.scn.seek(did, {"def": SUN, "t_ms": 0})
            with pytest.raises(ModelError, match="geen verbinding"):
                await hub.scn.seek_stop(did)
            assert hub.scn.rt.get(did, {}).get("seek_q") is None and f"seek:{did}" not in hub.tasks and not seeks(esp)
            hub.online.add(did)
            assert (await hub.scn.seek(did, {"def": SUN, "t_ms": 0}))["ok"], "er staat niets te wachten: de eerstvolgende gaat gewoon"
            assert len(seeks(esp)) == 1
            # het apparaat valt weg tussen het verzoek en het sturen: een melding, geen wachtrij
            hub.online.discard(did)
            req = {"target": {"key": ("x",), "h": "x", "slot": None, "data": b"x"}, "t": 1, "fut": asyncio.get_running_loop().create_future(), "dead": False}
            with pytest.raises(scenehub._Fail, match="geen verbinding"):
                await hub.scn._do_seek(did, hub.scn.rt[did], req)
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_play_stop_and_an_input_edge_win_over_a_seek(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, setter = await wired(tmp_path, monkeypatch, esp=kidswekker(seek=True))
        monkeypatch.setattr(scenehub, "SEEK_GAP", 0.05)
        try:
            await fill(hub, did, esp)
            await hub.scn.seek(did, {"def": SUN, "t_ms": 1000})
            assert esp.seeking
            # play vanaf de pagina: vervangt het vasthouden
            await hub.scn.play(did, {"id": sid})
            assert esp.seeking is None and "seek" not in esp.state()["scenes"]["run"] and esp.run["slot"] == 1
            await cycle(hub, did)
            assert "seek" not in hub.scn.rt[did]["run"]
            await asyncio.sleep(0.06)
            await hub.scn.seek(did, {"def": SUN, "t_ms": 2000})
            assert "data" in seeks(esp)[-1], "na een play weet de add-on dat er niets meer vastgehouden wordt: weer met de bytes"
            # een stop vanaf de pagina
            await hub.scn.stop(did, {})
            assert esp.seeking is None and esp.run is None
            await asyncio.sleep(0.06)
            # een wachtende seek wordt door een play weggegooid (en komt dus nooit meer na de play aan)
            await hub.scn.seek(did, {"def": SUN, "t_ms": 3000})
            n = len(seeks(esp))
            pending = asyncio.ensure_future(hub.scn.seek(did, {"def": SUN, "t_ms": 4000}))  # wacht op de pauze
            await asyncio.sleep(0.01)
            await hub.scn.play(did, {"id": sid})
            assert (await pending) == {"ok": True, "message": "vervangen door een start", "superseded": True}
            await asyncio.sleep(0.15)
            assert len(seeks(esp)) == n and esp.seeking is None and esp.run["slot"] == 1, "de wachtende seek is vervallen"
            # een flank van een echte scène-ingang
            await asyncio.sleep(0.06)
            await hub.scn.seek(did, {"def": SUN, "t_ms": 5000})
            assert esp.seeking
            setter(False)
            hub.tick()  # eerste stand
            await asyncio.sleep(0.05)
            setter(True)
            hub.tick()
            await settle(hub, did)
            assert esp.seeking is None and esp.run["slot"] == 1 and esp.run["h"] == hub.scn.lib.hashes[sid], "de flank startte de scène: het vasthouden is voorbij"
            await cycle(hub, did)
            assert hub.status()[0]["scn"]["run"]["seek"] is None
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_scheduled_start_replaces_the_hold_and_the_next_seek_sends_the_bytes(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, tm = await alarm(tmp_path, monkeypatch, esp=kidswekker(seek=True))
        monkeypatch.setattr(scenehub, "SEEK_GAP", 0.0)
        try:
            await cycle(hub, did, 3)
            await hub.scn.seek(did, {"def": SUN, "t_ms": 1000})
            clock.advance(local(2027, 1, 5, 7, 0) - clock.epoch)  # het apparaat start de wekker zelf
            await cycle(hub, did)
            assert esp.seeking is None and esp.run["slot"] == 1 and "seek" not in hub.scn.rt[did]["run"]
            await hub.scn.seek(did, {"def": SUN, "t_ms": 2000})
            assert "data" in seeks(esp)[-1] and esp.seeking["t"] == 2000
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_seek_stop_only_stops_a_held_picture(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await seeker(tmp_path, monkeypatch)
        try:
            a = hub.scn.add({"name": "Zon", "def": SUN})
            hub.scn.select(did, [a])
            await fill(hub, did, esp)
            await hub.scn.play(did, {"id": a})  # een gewone scène speelt
            assert (await hub.scn.seek_stop(did))["message"].startswith("het apparaat toont geen beeld"), "iets anders dat speelt blijft ongemoeid"
            assert esp.run and not calls(esp, "stop")
            await hub.scn.seek(did, {"def": SUN, "t_ms": 1000})
            assert esp.seeking
            assert await hub.scn.seek_stop(did) == {"ok": True, "message": "gestopt"}
            assert esp.seeking is None and esp.run is None and calls(esp, "stop") == [("stop", {})]
            assert hub.scn.rt[did]["run"] is None
            assert (await hub.scn.seek_stop(did))["message"].startswith("het apparaat toont geen beeld"), "twee keer is onschadelijk"
            # een seek die nog wacht, vervalt bij de stop
            await asyncio.sleep(0.05)
            await hub.scn.seek(did, {"def": SUN, "t_ms": 1000})
            n = len(seeks(esp))
            pending = asyncio.ensure_future(hub.scn.seek(did, {"def": SUN, "t_ms": 2000}))
            await asyncio.sleep(0.005)
            assert (await hub.scn.seek_stop(did))["message"] == "gestopt"
            assert (await pending)["superseded"] and len(seeks(esp)) == n and esp.seeking is None
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_the_hold_ends_by_itself_after_300_s_and_a_stop_after_that_does_nothing(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await seeker(tmp_path, monkeypatch)
        try:
            await hub.scn.seek(did, {"def": SUN, "t_ms": 1000})
            clock.advance(299)
            await cycle(hub, did)
            assert esp.seeking and hub.scn.rt[did]["run"]["seek"] == 1000
            clock.advance(2)
            await cycle(hub, did)
            assert esp.seeking is None and hub.scn.rt[did]["run"] is None
            assert (await hub.scn.seek_stop(did))["message"].startswith("het apparaat toont geen beeld")
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_removing_the_device_drops_a_waiting_seek(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await seeker(tmp_path, monkeypatch, gap=0.2)
        try:
            await hub.scn.seek(did, {"def": SUN, "t_ms": 0})
            pending = asyncio.ensure_future(hub.scn.seek(did, {"def": SUN, "t_ms": 1000}))
            await asyncio.sleep(0.01)
            hub.scn.drop_device(did)
            assert (await pending)["superseded"] and f"seek:{did}" not in hub.tasks and did not in hub.scn.rt
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_web_api_frame_and_seek(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await seeker(tmp_path, monkeypatch)
        c = await Env(tmp_path, hub, r).client()
        try:
            sid = (await (await c.post("/api/scenes", json={"name": "Zon", "def": {"kind": "wake", "sun_min": 2, "wake_min": 1}})).json())["id"]
            res = await c.post("/api/scenes/frame", json={"def": {"kind": "wake", "sun_min": 2, "wake_min": 1, "brightness": 50}, "t_ms": 60_000, "n": 5})
            body = await res.json()
            assert res.status == 200 and body["t_ms"] == 60_000 and body["window_ms"] == body["total_ms"] == 185_000 and body["bounded"] is True and len(body["frame"]) == 5
            assert all(len(px) == 3 and all(isinstance(v, int) and 0 <= v <= 255 for v in px) for px in body["frame"])
            full = await (await c.post("/api/scenes/frame", json={"def": {"kind": "wake", "sun_min": 2, "wake_min": 1}, "t_ms": 60_000, "n": 5})).json()
            assert max(map(max, body["frame"])) < max(map(max, full["frame"])), "de helderheid telt mee"
            byid = await (await c.post("/api/scenes/frame", json={"id": sid, "t_ms": 60_000, "n": 5})).json()
            assert byid == full, "op id: de scène uit de bibliotheek"
            assert len((await (await c.post("/api/scenes/frame", json={"def": NIGHT, "t_ms": 0})).json())["frame"]) == 24, "n is standaard 24"
            for bad, word in (({"def": NIGHT}, "t_ms"), ({"def": NIGHT, "t_ms": -1}, "t_ms"), ({"def": {"kind": "x"}, "t_ms": 0}, "kind"), ({"t_ms": 0}, "def"), ({"id": "s99", "t_ms": 0}, "onbekende"),
                              ({"def": NIGHT, "t_ms": 0, "n": 0}, "n:"), ({"def": {**NIGHT, "brightness": 0}, "t_ms": 0}, "brightness")):
                res = await c.post("/api/scenes/frame", json=bad)
                assert res.status == 400 and word in (await res.json())["error"], bad
            # seek
            res = await c.post(f"/api/espdev/{did}/scene/seek", json={"def": SUN, "t_ms": 90_000})
            assert res.status == 200 and (await res.json()) == {"ok": True, "message": "toont 1:30"} and esp.seeking["t"] == 90_000
            st = await (await c.get("/api/state")).json()
            dev = next(x for x in st["espdev"] if x["id"] == did)
            assert dev["scn"]["run"]["seek"] == 90_000 and dev["scn"]["cap"]["seek"] is True
            res = await c.post(f"/api/espdev/{did}/scene/seek", json={"def": SUN, "t_ms": -5})
            assert res.status == 400 and "t_ms" in (await res.json())["error"]
            res = await c.post(f"/api/espdev/{did}/scene/seek/stop", json={})
            assert res.status == 200 and (await res.json())["message"] == "gestopt" and esp.seeking is None
            res = await c.post(f"/api/espdev/{did}/scene/seek/stop")
            assert res.status == 200 and "niets te stoppen" in (await res.json())["message"]
            assert (await c.post("/api/espdev/nee/scene/seek", json={"def": SUN, "t_ms": 0})).status == 400
        finally:
            await c.close()
            await shutdown(hub, esp)
        (tmp_path / "old").mkdir()
        r, hub, esp, did, clock = await env(tmp_path / "old", monkeypatch)  # een 1.1-kidswekker
        c = await Env(tmp_path / "old", hub, r).client()
        try:
            for path in ("scene/seek", "scene/seek/stop"):
                res = await c.post(f"/api/espdev/{did}/{path}", json={"def": SUN, "t_ms": 0})
                assert res.status == 400 and "firmware ouder dan 1.2" in (await res.json())["error"]
        finally:
            await c.close()
            await shutdown(hub, esp)
    asyncio.run(go())


def test_web_api_frame_without_a_hub(tmp_path):
    async def go():
        r = Rig(tmp_path)
        s = Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True)
        c = TestClient(TestServer(web.make_app(web.Ctx(s, r.engine))))
        await c.start_server()
        try:
            res = await c.post("/api/scenes/frame", json={"def": NIGHT, "t_ms": 0})
            assert res.status == 400 and "niet beschikbaar" in (await res.json())["error"]
        finally:
            await c.close()
    asyncio.run(go())


def test_the_fake_device_follows_the_spec_for_seek():
    """De nagebouwde `scene/seek` zelf (tests/fakehuisdev.py): de drie bodyvormen, 422/rejected, wissen bij play en stop."""
    async def go():
        from aiohttp import ClientSession
        esp = kidswekker(seek=True)
        await esp.start()
        esp.token = "t" * 16
        data = sc.gen_static()
        b64 = base64.b64encode(data).decode()
        try:
            async with ClientSession() as s:
                async def post(path, body):
                    async with s.post(f"http://127.0.0.1:{esp.http_port}/huisdev/{path}", json=body, headers={"X-Huisdev-Token": esp.token}) as r:
                        return r.status, await r.json()
                assert (await post("scene/seek", {"t": 5}))[1] == {"ok": False, "result": "rejected", "reason": "geen scène om te tonen"}
                st, j = await post("scene/seek", {"data": b64, "t": 5})
                assert (st, j["result"], j["hash"]) == (200, "applied", sc.hash_hex(data)) and esp.state()["scenes"]["run"] == {"slot": 0, "h": sc.hash_hex(data), "until": None, "seek": 5}
                assert (await post("scene/seek", {"t": 9}))[1]["result"] == "applied" and esp.seeking["t"] == 9
                for t in (-1, 86_400_001, 1.5, "1", None, True):
                    assert (await post("scene/seek", {"t": t}))[0] == 422, t
                assert (await post("scene/seek", {"slot": 1, "t": 5}))[1]["reason"] == "slot is leeg"
                assert (await post("scene/seek", {"slot": 9, "t": 5}))[0] == 422
                assert (await post("scene/seek", {"slot": 1, "data": b64, "t": 5}))[0] == 422
                assert (await post("scene/seek", {"data": "@@@", "t": 5}))[0] == 422
                assert (await post("scene/store", {"slot": 1, "data": b64}))[0] == 200
                assert (await post("scene/seek", {"slot": 1, "t": 7}))[1]["result"] == "applied" and esp.state()["scenes"]["run"]["seek"] == 7
                assert (await post("scene/play", {"slot": 1}))[0] == 200 and esp.seeking is None and "seek" not in esp.state()["scenes"]["run"], "play vervangt"
                assert (await post("scene/seek", {"t": 1}))[1]["result"] == "rejected", "na een play is er geen seek-beeld meer om te verplaatsen"
                await post("scene/seek", {"data": b64, "t": 3})
                assert (await post("scene/stop", {}))[0] == 200 and esp.seeking is None and esp.run is None
                await post("scene/seek", {"data": b64, "t": 3})
                esp.now = esp.epoch() + 301
                assert esp.state()["scenes"].get("run") is None, "300 s na de laatste seek"
        finally:
            await esp.stop()
        old = kidswekker()  # 1.1: geen seek in de descriptor, het endpoint bestaat niet
        await old.start()
        old.token = "t" * 16
        try:
            assert "seek" not in old.descriptor()["scenes"]
            async with ClientSession() as s:
                async with s.post(f"http://127.0.0.1:{old.http_port}/huisdev/scene/seek", json={"t": 1}, headers={"X-Huisdev-Token": old.token}) as r:
                    assert r.status == 404 and (await r.json())["error"] == "unknown_path"
        finally:
            await old.stop()
    asyncio.run(go())


# ------------------------------------------------------------ de web-API

class Env:
    def __init__(self, tmp_path, hub, rig):
        s = Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True)
        self.ctx = web.Ctx(s, rig.engine, espdev=hub)

    async def client(self):
        c = TestClient(TestServer(web.make_app(self.ctx)))
        await c.start_server()
        return c


def test_web_api(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        c = await Env(tmp_path, hub, r).client()
        try:
            assert await (await c.get("/api/scenes")).json() == []
            res = await c.post("/api/scenes", json={"name": "Nachtlicht", "def": {"kind": "static"}})
            assert res.status == 200 and (await res.json()) == {"ok": True, "id": "s1"}
            res = await c.post("/api/scenes", json={"name": "Wekker", "def": {"kind": "wake", "sun_min": 2}, "release": "finish"})
            assert (await res.json())["id"] == "s2"
            lst = await (await c.get("/api/scenes")).json()
            assert [(x["id"], x["name"], x["release"], x["bounded"]) for x in lst] == [("s1", "Nachtlicht", "stop", False), ("s2", "Wekker", "finish", True)] and lst[0]["hash"] == hub.scn.lib.hashes["s1"]
            bad = await c.post("/api/scenes", json={"name": "Stuk", "def": {"kind": "static", "color": "rood"}})
            assert bad.status == 400 and "#rrggbb" in (await bad.json())["error"]
            assert (await c.put("/api/scenes/s1", json={"name": "Nachtlamp"})).status == 200
            assert (await c.put("/api/scenes/s9", json={"name": "x"})).status == 400
            pr = await c.post("/api/scenes/preview", json={"def": {"kind": "static", "color": "#00ff00"}, "n": 4, "samples": 2})
            body = await pr.json()
            assert pr.status == 200 and body["n"] == 4 and body["frames"][0][0] == [0, 255, 0]
            assert (await c.post("/api/scenes/preview", json={"def": {"kind": "nope"}})).status == 400
            assert (await c.post("/api/scenes/preview", json={"def": NIGHT, "n": 0})).status == 400
            # een blok Scènes op het apparaat
            assert await (await c.get("/api/scenerules")).json() == []
            res = await c.post("/api/scenerules", json={"name": "Kidswekker", "device": did, "inputs": [{"name": "Nacht", "kind": "bool"}, {"name": "Wekker", "kind": "time"}]})
            assert res.status == 200 and (await res.json()) == {"ok": True, "id": "q1"}
            res = await c.put("/api/scenerules/q1", json={"rules": [{"scene": "s1", "when": [{"op": "on", "in": "i1"}]}, {"scene": "s2", "when": [{"op": "wake", "in": "i2", "before": 15}]}]})
            assert res.status == 200 and hub.devices[did]["scn"]["slots"] == {"s1": 1, "s2": 2}
            bad = await c.put("/api/scenerules/q1", json={"rules": [{"scene": "s7", "when": [{"op": "on", "in": "i1"}]}]})
            assert bad.status == 400 and "kies een scène" in (await bad.json())["error"]
            assert (await c.put("/api/scenerules/q1", json={"rules": "s1"})).status == 400
            assert (await c.put("/api/scenerules/q9", json={"name": "x"})).status == 400
            assert (await c.post("/api/scenerules", json={"device": did})).status == 400, "een apparaat heeft hoogstens een blok"
            lst = await (await c.get("/api/scenerules")).json()
            assert lst[0]["id"] == "q1" and lst[0]["device_name"] == hub.devices[did]["name"] and [x["text"] for x in lst[0]["rules"]] == [["Nacht = 1"], ["wekker Wekker, 15 min ervoor, 60 min erna"]]
            st = await (await c.get("/api/state")).json()
            dev = next(x for x in st["espdev"] if x["id"] == did)
            assert [x["id"] for x in dev["scn"]["sel"]] == ["s1", "s2"] and dev["scn"]["cap"]["slots"] == 8 and "token" not in json.dumps(st)
            blk = next(x for x in st["schema"]["blocks"] if x["key"] == "q:q1")
            assert blk["kind"] == "scenes" and st["scenerules"][0]["id"] == "q1"
            await fill(hub, did, esp)
            res = await c.post(f"/api/espdev/{did}/scene/play", json={"id": "s1"})
            assert res.status == 200 and esp.run["slot"] == 1
            assert (await c.post(f"/api/espdev/{did}/scene/play", json={"slot": 7})).status == 400
            res = await c.post(f"/api/espdev/{did}/scene/stop", json={"id": "s1"})
            assert res.status == 200 and esp.run is None
            assert (await c.post(f"/api/espdev/{did}/scene/play", json={"slot": 2})).status == 200 and esp.run["slot"] == 2
            assert (await c.post(f"/api/espdev/{did}/scene/stop", json={})).status == 200 and esp.run is None
            res = await c.delete("/api/scenes/s1")
            assert res.status == 200 and hub.devices[did]["scn"]["sel"] == ["s2"] and len(hub.scn.rules.items["q1"]["rules"]) == 1
            assert (await c.delete("/api/scenes/s1")).status == 404
            assert (await c.delete("/api/scenerules/q1")).status == 200 and hub.devices[did]["scn"]["sel"] == []
            assert (await c.delete("/api/scenerules/q1")).status == 404
            res = await c.put(f"/api/espdev/{did}", json={"name": "Slaapkamer"})  # de gewone route werkt nog
            assert res.status == 200 and hub.devices[did]["name"] == "Slaapkamer"
        finally:
            await c.close()
            await shutdown(hub, esp)
    asyncio.run(go())


def test_web_api_without_a_hub(tmp_path):
    async def go():
        r = Rig(tmp_path)
        s = Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True)
        c = TestClient(TestServer(web.make_app(web.Ctx(s, r.engine))))
        await c.start_server()
        try:
            for method, path in (("get", "/api/scenes"), ("post", "/api/scenes/preview")):
                res = await getattr(c, method)(path) if method == "get" else await c.post(path, json={})
                assert res.status == 400 and "niet beschikbaar" in (await res.json())["error"]
        finally:
            await c.close()
    asyncio.run(go())
