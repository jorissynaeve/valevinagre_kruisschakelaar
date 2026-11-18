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


def calls(esp, op=None):
    return [(o, b) for o, b in esp.scene_calls if op is None or o == op]


def block(r, key):
    return next(x for x in schema.view(r.model, r.engine.snapshot())["blocks"] if x["key"] == key)


# ------------------------------------------------------------ de versiestand en de descriptor

def test_proto_state_upgrade_only_for_huisdev_devices():
    assert eh.CURRENT == (1, 1)
    assert [proto_state(x) for x in ("1.0", "1.1", "1.2", "2.0", "x", None)] == ["upgrade", "ok", "ok", "unsupported", "unsupported", "unsupported"]
    for driver in ("hue", "nuki", "brink", "smappee", "reolink", "esphome"):
        assert proto_state("1.0", driver) == "ok", driver
        assert proto_state("2.0", driver) == "unsupported", "een onbekende major blijft onbekend, voor elk soort"
    assert proto_state("1.0", "huisdev") == "upgrade"


def test_only_a_real_huisdev_device_shows_upgrade_in_the_status(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch, esp=pool(), manual=False)  # een 1.0-apparaat
        try:
            assert hub.status()[0]["proto_state"] == "upgrade" and hub.status()[0]["current"] == "1.1"
            for drv in ("hue", "nuki", "brink", "smappee", "esphome"):
                hub.devices[did]["driver"] = drv  # alle andere soorten zeggen vast 1.0
                assert hub.status()[0]["proto_state"] == "ok", drv
            hub.devices[did]["driver"] = "huisdev"
            hub.devices[did]["proto"] = "1.1"
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
            assert not [p for p in block(r, f"e:{did}")["ports"] if p["name"].startswith("scene:")]
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
            assert any(p["name"] == f"scene:{c}" for p in block(r, f"e:{did}")["ports"]), "de rij blijft"
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
            hub.scn.select(did, [a, b])
            src = switch(r)[1]
            hub.update(did, {"inputs": {f"scene:{b}": src}})
            hub.close()
            hub2 = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
            assert hub2.devices[did]["scn"] == {"sel": [a, b], "slots": {a: 1, b: 2}, "cap": {"formats": ["ledscene/1"], "slots": 8, "max_bytes": 768, "sched": 8}}
            assert hub2.devices[did]["inputs"] == {f"scene:{b}": src} and sorted(hub2.scn.lib.items) == [a, b]
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
            raw["devices"][did]["inputs"] = {"scene:s9": src, f"scene:{a}": src}
            (tmp_path / "espdev.json").write_text(json.dumps(raw), encoding="utf-8")
            h4 = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
            assert h4.devices[did]["scn"]["sel"] == [a] and h4.devices[did]["inputs"] == {f"scene:{a}": src}
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


# ------------------------------------------------------------ de bedrading en het blok

def test_scene_rows_on_the_block_and_the_wiring_is_checked(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            a, b = hub.scn.add({"name": "Nachtlicht", "def": NIGHT}), hub.scn.add({"name": "Wekker", "def": WAKE})
            hub.scn.select(did, [a, b])
            blk = block(r, f"e:{did}")
            rows = [p for p in blk["ports"] if p["name"].startswith("scene:")]
            assert [(p["name"], p["label"], p["type"], p["action"], p["scene"], p["ref"]) for p in rows] == [(f"scene:{a}", "Nachtlicht", "bool", True, True, None), (f"scene:{b}", "Wekker", "bool", True, True, None)]
            assert [p["row"] for p in rows] == [2, 3] and blk["rows"] == 4, "onder de punten (Helderheid en Wekker aan staan op het blok), een rij per scène"
            tm = r.model.save_timer(None, "Wekker", {"mon": [{"from": "07:00", "to": "08:00"}]})
            n = r.model.add_module("number", "Getal", 5)
            above = r.model.add_module("above", "Hoog", 1)
            r.model.set_module(above, inputs={"a": f"m:{n}"})
            hub.update(did, {"inputs": {f"scene:{a}": f"t:{tm}", f"scene:{b}": f"m:{above}"}})
            assert [p["ref"] for p in block(r, f"e:{did}")["ports"] if p["name"] == f"scene:{a}"] == [f"t:{tm}"] and hub.devices[did]["inputs"] == {f"scene:{a}": f"t:{tm}", f"scene:{b}": f"m:{above}"}
            for pid, ref, word in ((f"scene:{a}", f"m:{n}", "een 0 of 1"), (f"scene:{a}", "m:bestaat_niet", "niet bestaat"), (f"scene:{a}", 5, "ongeldige bron"),
                                   ("scene:s99", f"t:{tm}", "niet op dit apparaat"), (f"scene:{a}", f"e:{did}:armed", "zichzelf")):
                with pytest.raises(ModelError, match=word):
                    hub.update(did, {"inputs": {pid: ref}})
            hub.update(did, {"inputs": {f"scene:{a}": None, f"scene:{b}": ""}})
            assert hub.devices[did]["inputs"] == {}
            hub.update(did, {"inputs": {f"scene:{a}": f"t:{tm}"}})
            hub.scn.select(did, [b])  # a van het apparaat: de rij en de bedrading verdwijnen
            assert hub.devices[did]["inputs"] == {} and [p["name"] for p in block(r, f"e:{did}")["ports"] if p["name"].startswith("scene:")] == [f"scene:{b}"]
            hub.update(did, {"inputs": {"brightness": f"m:{n}"}})  # een gewoon punt werkt nog
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
    hub.update(did, {"inputs": {f"scene:{sid}": src}})
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
            assert calls(esp, "play") == [] and hub.seen[did][f"scene:{sid}"] is True, "de eerste stand wordt alleen onthouden: een herstart start niets"
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
            hub.update(did, {"inputs": {f"scene:{sid}": f"m:{above}"}})
            hub.tick()
            assert hub.seen[did][f"scene:{sid}"] is False
            r.model.set_module(above, inputs={"a": None})  # de bron wordt onbekend
            hub.tick()
            assert f"scene:{sid}" not in hub.seen[did]
            r.model.set_module(n, value=1)
            r.model.set_module(above, inputs={"a": f"m:{n}"})
            hub.tick()
            await asyncio.sleep(0.05)
            assert hub.seen[did][f"scene:{sid}"] is True and calls(esp) == [], "alleen onthouden"
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


def test_wiring_of_a_device_without_scenes_sleeps_and_wakes_up_again(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, sid, setter = await wired(tmp_path, monkeypatch, manual=False)
        try:
            key = f"scene:{sid}"
            hub.tick()
            esp.scene_cap = None  # het device verliest zijn scènes (firmware zonder)
            await eventually(lambda: hub.devices[did]["scn"]["cap"] is None)
            assert hub.devices[did]["inputs"][key] and hub.devices[did]["scn"]["sel"] == [sid], "de bedrading blijft bewaard"
            assert not [p for p in block(r, f"e:{did}")["ports"] if p["name"] == key], "maar slapend: geen rij"
            n = len(esp.scene_calls)
            setter(True)
            hub.tick()
            await asyncio.sleep(0.2)
            assert len(esp.scene_calls) == n and key not in hub.seen[did], "geen scenefunctionaliteit"
            esp.scenes()
            await eventually(lambda: hub.devices[did]["scn"]["cap"] is not None)
            assert hub.devices[did]["scn"]["slots"] == {sid: 1} and [p["name"] for p in block(r, f"e:{did}")["ports"] if p["name"] == key]
            hub.tick()
            assert hub.seen[did][key] is True and not calls(esp, "play"), "de eerste stand na het terugkomen wordt alleen onthouden"
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
    hub.update(did, {"inputs": {f"scene:{sid}": f"t:{tm}"}})
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
            hub.scn.select(did, [sid, night])
            hub.update(did, {"inputs": {f"scene:{night}": f"t:{tm2}"}})
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
            hub.update(did, {"inputs": {f"scene:{sid}": None}})  # de bron los
            await cycle(hub, did)
            assert esp.sched == [] and calls(esp, "schedule")[-1] == ("schedule", {"entries": []}), "de regels van die scène gaan weg"
            hub.update(did, {"inputs": {f"scene:{sid}": f"t:{tm}"}})
            await cycle(hub, did, 2)
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
            hub.update(did, {"inputs": {f"scene:{sid}": f"m:{above}"}})  # een module: niet vooruit te berekenen
            await cycle(hub, did, 4)
            assert calls(esp, "schedule") == [] and "geen timer" in hub.status()[0]["scn"]["sel"][0]["sched_note"], "geen schema, een melding"
            hub.update(did, {"inputs": {f"scene:{sid}": f"t:{tm}"}})
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
            hub.scn.select(did, [sid, other])
            await cycle(hub, did, 4)
            key = f"scene:{sid}"
            hub.tick()
            hub.last[did][key] = "iets"
            hub.scn.q[did] = __import__("collections").deque([{"kind": "stop", "sid": sid, "t": clock.mono, "tries": 0}])
            assert key in hub.seen[did] and len(esp.sched) == 8 and sorted(esp.slots) == [1, 2]
            assert hub.scn.delete(sid) is True and hub.scn.delete(sid) is False
            d = hub.devices[did]
            assert d["scn"]["sel"] == [other] and sid not in d["scn"]["slots"] and key not in d["inputs"] and key not in hub.seen[did] and key not in hub.last[did] and not hub.scn.q[did]
            saved = json.loads((tmp_path / "espdev.json").read_text(encoding="utf-8"))["devices"][did]
            assert saved["scn"]["sel"] == [other] and key not in saved["inputs"]
            assert sid not in json.loads((tmp_path / "scenes.json").read_text(encoding="utf-8"))["scenes"]
            await cycle(hub, did)
            assert sorted(esp.slots) == [2] and calls(esp, "store")[-1] == ("store", {"slot": 1, "data": ""}), "de sync wist het slot"
            await cycle(hub, did)
            assert esp.sched == [], "en de regels van die scène"
            assert hub.scn.add({"name": "Nieuw", "def": NIGHT}) not in (sid, other), "het id komt niet terug"
            assert [p["name"] for p in block(r, f"e:{did}")["ports"] if p["name"].startswith("scene:")] == [f"scene:{other}"]
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
            # op het apparaat
            res = await c.put(f"/api/espdev/{did}/scenes", json={"scenes": ["s1", "s2"]})
            assert res.status == 200 and hub.devices[did]["scn"]["slots"] == {"s1": 1, "s2": 2}
            assert (await c.put(f"/api/espdev/{did}/scenes", json={"scenes": ["s7"]})).status == 400
            assert (await c.put(f"/api/espdev/{did}/scenes", json={"scenes": "s1"})).status == 400
            st = await (await c.get("/api/state")).json()
            dev = next(x for x in st["espdev"] if x["id"] == did)
            assert [x["id"] for x in dev["scn"]["sel"]] == ["s1", "s2"] and dev["scn"]["cap"]["slots"] == 8 and "token" not in json.dumps(st)
            await fill(hub, did, esp)
            res = await c.post(f"/api/espdev/{did}/scene/play", json={"id": "s1"})
            assert res.status == 200 and esp.run["slot"] == 1
            assert (await c.post(f"/api/espdev/{did}/scene/play", json={"slot": 7})).status == 400
            res = await c.post(f"/api/espdev/{did}/scene/stop", json={"id": "s1"})
            assert res.status == 200 and esp.run is None
            assert (await c.post(f"/api/espdev/{did}/scene/play", json={"slot": 2})).status == 200 and esp.run["slot"] == 2
            assert (await c.post(f"/api/espdev/{did}/scene/stop", json={})).status == 200 and esp.run is None
            res = await c.delete("/api/scenes/s1")
            assert res.status == 200 and hub.devices[did]["scn"]["sel"] == ["s2"]
            assert (await c.delete("/api/scenes/s1")).status == 404
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
