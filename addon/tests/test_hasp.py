"""De schermen met openHASP: nagebootst scherm (de MQTT-berichten) tegen de echte hub en een nagebootste HA."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from fakeha import FakeHa
from kkaddon import cards, hasp, hasphub, schema, web
from kkaddon.engine import Engine
from kkaddon.habridge import HaBridge
from kkaddon.hasp import MARKER, Screen, chunks
from kkaddon.hasphub import HaspHub
from kkaddon.model import Model, ModelError
from kkaddon.settings import Settings
from rig import KEY

KITCHEN, EAT = "switch.keuken", "switch.eetplaats"
# zoals cards.screen_pages ze geeft
PAGES = [{"title": "Main room", "key": "main", "entities": [{"entity": KITCHEN, "name": "Kitchen"}, {"entity": EAT, "name": "Dinner room"}]},
         {"title": "Garden", "key": "garden", "entities": [{"entity": "switch.weg", "name": "Weg"}]}]


def with_cards(m, sid):
    """De twee kaarten van PAGES aan het scherm."""
    for c in PAGES:
        m.cards = {**m.cards, c["key"]: {"type": "grid", "name": c["title"], "layout": "grid",
                                          "items": [{"ref": None, "entity": e["entity"], "name": e["name"], "icon": "", "dp": ""} for e in c["entities"]]}}
    m.set_screen(sid, cards=[c["key"] for c in PAGES])
DOWN = json.dumps({"event": "down"})
UP = json.dumps({"event": "up"})


class Env:
    """De hub, met een klok in de hand en alles wat naar het scherm gaat in `sent`."""

    def __init__(self):
        self.offset = 0.0
        self.sent: list[tuple[str, str]] = []
        self.mqtt_up = True

    async def __aenter__(self):
        self.ha = FakeHa(entities={KITCHEN: "on", EAT: "off"})
        self.server = TestServer(self.ha.app())
        await self.server.start_server()
        self.engine = Engine(KEY, Model(), lambda raw: None)
        self.bridge = HaBridge(f"ws://127.0.0.1:{self.server.port}/api/websocket", "test", self.engine, retry=(0.05, 0.2))
        self.hub = HaspHub(self.bridge, self.engine, clock=lambda: time.monotonic() + self.offset, settle=(0, 0, 0))
        self.hub.publish = self._publish
        self.tasks = [asyncio.create_task(self.bridge.run()), asyncio.create_task(self._tick())]
        await self.until(lambda: self.bridge.connected and self.bridge.states, what="HA")
        return self

    async def __aexit__(self, *a):
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.hub.close()
        await self.server.close()

    def _publish(self, topic, payload):
        if self.mqtt_up:
            self.sent.append((topic, payload))
        return self.mqtt_up

    async def _tick(self):
        while True:
            self.hub.tick()
            await asyncio.sleep(0.005)

    async def until(self, pred, timeout=3.0, what="voorwaarde"):
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            if pred():
                return
            await asyncio.sleep(0.005)
        raise AssertionError(f"time-out: {what}; verstuurd: {self.sent[-6:]}")

    def add(self, plate="scherm01"):
        sid = self.engine.model.add_screen("Eetkamer", plate)
        with_cards(self.engine.model, sid)
        return sid

    def cmds(self, name):
        return [p for t, p in self.sent if t == f"hasp/scherm01/command/{name}"]

    def from_screen(self, sub, payload, plate="scherm01"):
        return self.hub.command(f"hasp/{plate}/{sub}", payload)

    async def online(self, sid):
        """Het scherm meldt zich, heeft geen ontwerp van ons en krijgt het dus."""
        self.from_screen("LWT", "online")
        self.offset += hasphub.QUERY_TIMEOUT
        await self.until(lambda: self.hub.links[sid].phase == "ready", what="ontwerp")


def run(coro):
    return asyncio.run(coro)


def objects(e):
    return [json.loads(line) for part in e.cmds("jsonl") for line in part.split("\n")]


def test_a_new_screen_gets_the_design_with_the_marker_last_and_the_current_colours():
    async def go():
        async with Env() as e:
            sid = e.add()
            await e.online(sid)
            assert e.cmds(MARKER + ".text") == [""], "eerst gevraagd of het ontwerp er al staat"
            assert e.cmds("clearpage") == ["all"] and e.cmds("page") == ["1"]
            objs = objects(e)
            assert objs[-1]["id"] == hasp.MARKER_ID and objs[-1]["text"] == e.hub.links[sid].screen.hash
            by = {(o["page"], o["id"]): o for o in objs}
            assert by[(1, 1)]["text"] == "Main room"
            assert by[(1, 2)]["text"].endswith("Kitchen") and by[(1, 2)]["bg_color"] == hasp.C_ON
            assert by[(1, 3)]["bg_color"] == hasp.C_OFF
            assert by[(2, 2)]["bg_color"] == hasp.C_UNKNOWN, "een entiteit die HA niet kent"
            assert all(len(p.encode()) <= hasp.MAX_CHUNK for p in e.cmds("jsonl"))
            assert e.hub.status()[0]["missing"] == ["switch.weg"], "zichtbaar gemeld"

    run(go())


def test_a_restart_of_the_addon_leaves_a_screen_with_the_right_marker_alone():
    async def go():
        async with Env() as e:
            sid = e.add()
            e.from_screen("LWT", "online")
            assert e.hub.links[sid].phase == "query"
            e.from_screen("state/" + MARKER, json.dumps({"text": e.hub.links[sid].screen.hash}))
            assert e.hub.links[sid].phase == "ready"
            assert not e.cmds("clearpage") and not e.cmds("jsonl"), "geen geknipper"
            assert (f"hasp/scherm01/command/p1b2.bg_color", hasp.C_ON) in e.sent, "wel de kleuren bijwerken"
            assert e.cmds("backlight") == [""], "en vragen of het scherm donker is"

    run(go())


def test_another_marker_means_a_new_design_and_a_changed_config_is_sent_right_away():
    async def go():
        async with Env() as e:
            sid = e.add()
            e.from_screen("LWT", "online")
            e.from_screen("state/" + MARKER, json.dumps({"text": "oud"}))
            await e.until(lambda: e.hub.links[sid].phase == "ready")
            assert e.cmds("clearpage") == ["all"]
            e.sent.clear()
            e.engine.model.set_screen(sid, name="Andere naam", timeout=60)
            await asyncio.sleep(0.02)
            assert not e.cmds("clearpage"), "naam en tijd staan niet op het scherm"
            e.engine.model.set_card("main", name="Nieuw")
            await e.until(lambda: e.hub.links[sid].phase == "ready" and e.cmds("clearpage") == ["all"], what="nieuw ontwerp")
            assert any(o.get("text") == "Nieuw" for o in objects(e))

    run(go())


def test_a_new_design_lights_a_screen_that_was_dark():
    """Gemeten op scherm03: `idle off` maakt een scherm dat met `backlight off` uitging niet wakker."""
    async def go():
        async with Env() as e:
            sid = e.add()
            await e.online(sid)
            assert ("hasp/scherm01/command/", "backlight on") in e.sent
            assert not e.hub.links[sid].screen.dark

    run(go())


def test_a_touch_toggles_in_ha_and_the_new_state_becomes_the_colour():
    async def go():
        async with Env() as e:
            sid = e.add()
            await e.online(sid)
            e.sent.clear()
            e.from_screen("state/p1b3", DOWN)
            await e.until(lambda: ("switch", "toggle", {"entity_id": EAT}) in e.ha.service_calls, what="toggle")
            await e.until(lambda: ("hasp/scherm01/command/p1b3.bg_color", hasp.C_ON) in e.sent, what="kleur")
            n = len(e.ha.service_calls)
            e.from_screen("state/p1b3", UP)
            e.from_screen("state/p1b1", DOWN)  # de titel
            e.from_screen("state/p9b9", DOWN)  # bestaat niet
            e.from_screen("state/p1b3", "geen json")
            await asyncio.sleep(0.05)
            assert len(e.ha.service_calls) == n

    run(go())


def test_the_nav_buttons_go_round():
    async def go():
        async with Env() as e:
            sid = e.add()
            await e.online(sid)
            e.sent.clear()
            for obj in ("p1b6", "p1b8", "p2b8", "p2b6", "p2b7"):
                e.from_screen(f"state/{obj}", DOWN)
            assert e.cmds("page") == ["2", "2", "1", "1", "1"]

    run(go())


def test_the_screen_goes_dark_and_the_touch_that_wakes_it_switches_nothing():
    async def go():
        async with Env() as e:
            sid = e.add()
            e.engine.model.set_screen(sid, timeout=30)
            await e.online(sid)
            e.offset += 31
            await e.until(lambda: ("hasp/scherm01/command/", "backlight off") in e.sent, what="donker")
            e.from_screen("state/p1b3", DOWN)
            await asyncio.sleep(0.05)
            assert not e.ha.service_calls, "wakker maken schakelt geen lamp"
            assert ("hasp/scherm01/command/", "backlight on") in e.sent
            e.from_screen("state/p1b3", UP)
            e.from_screen("state/p1b3", DOWN)
            await e.until(lambda: ("switch", "toggle", {"entity_id": EAT}) in e.ha.service_calls, what="tweede aanraking schakelt")

    run(go())


def test_when_the_firmware_wakes_first_the_touch_right_after_still_switches_nothing():
    calls, sent = [], []

    class Ha:
        def state(self, _e):
            return ("off", {})

        def call(self, *a):
            calls.append(a)

    s = Screen(PAGES, 30, Ha(), lambda c, p: sent.append((c, p)))
    s.ready(0.0)
    s.tick(31.0)
    assert s.dark
    s.on_backlight('{"state":"on","brightness":255}', 40.0)
    assert not s.dark
    s.on_event("p1b2", DOWN, 40.1)
    assert calls == []
    s.on_event("p1b2", DOWN, 41.0)
    assert calls == [("switch", "toggle", {"entity_id": KITCHEN})]


def test_timeout_zero_never_goes_dark_and_scripts_and_buttons_are_activated():
    calls, sent = [], []

    class Ha:
        def state(self, _e):
            return None

        def call(self, *a):
            calls.append(a)

    pages = [{"title": "x", "key": "x", "entities": [{"entity": "script.poort"}, {"entity": "button.bel"}, {"entity": "delete", "name": "Leeg"}, {"entity": "sensor.t"}]}]
    s = Screen(pages, 0, Ha(), lambda c, p: sent.append((c, p)))
    s.ready(0.0)
    sent.clear()
    s.tick(99999.0)
    assert sent == []
    s.on_event("p1b2", DOWN, 1.0)
    s.on_event("p1b3", DOWN, 2.0)
    s.on_event("p1b4", DOWN, 3.0)
    s.on_event("p1b5", DOWN, 4.0)  # een sensor toont alleen zijn waarde
    assert calls == [("script", "turn_on", {"entity_id": "script.poort"}), ("button", "press", {"entity_id": "button.bel"})]
    assert '"poort' not in s.design()[0] and any("poort" in line for line in s.design()), "zonder naam: de naam van de entiteit"


def test_the_status_follows_the_lwt_and_a_screen_that_returns_is_checked_again():
    async def go():
        async with Env() as e:
            sid = e.add()
            topic = hasphub.t_online(sid)
            assert e.hub.states()[topic] == ("OFF", True)
            await e.online(sid)
            assert e.hub.states()[topic] == ("ON", True)
            e.from_screen("state/sensors", json.dumps({"uptime": "22T23:03:41"}))
            assert e.hub.status()[0]["uptime"] == "22T23:03:41"
            e.from_screen("LWT", "offline")
            assert e.hub.states()[topic] == ("OFF", True) and e.hub.links[sid].phase == "idle"
            e.sent.clear()
            e.from_screen("state/p1b2", DOWN)  # iets horen betekent: het is er, maar eerst controleren
            assert e.hub.links[sid].online and e.hub.links[sid].phase == "query"
            assert not e.ha.service_calls and e.cmds(MARKER + ".text") == [""]

    run(go())


def test_an_lwt_from_before_the_screen_was_added_still_counts_and_others_are_ignored():
    async def go():
        async with Env() as e:
            assert e.from_screen("LWT", "online") is True
            assert e.hub.command("kruisschakelaars/light/1/0/set", "ON") is False, "niet van ons"
            sid = e.add()
            assert e.hub.links[sid].online, "de retained LWT komt geen tweede keer"
            assert e.hub.status()[0]["plates"] == ["scherm01"]
            e.sent.clear()
            e.from_screen("LWT", "online", plate="scherm03")
            assert e.sent == [], "een scherm zonder blok blijft van wie het nu is (pyscript)"
            assert e.hub.status()[0]["plates"] == ["scherm01", "scherm03"]

    run(go())


def test_without_mqtt_nothing_counts_as_sent_and_the_next_lwt_tries_again():
    async def go():
        async with Env() as e:
            sid = e.add()
            e.mqtt_up = False
            e.from_screen("LWT", "online")
            assert e.hub.links[sid].phase == "idle"
            e.mqtt_up = True
            e.from_screen("state/" + MARKER, json.dumps({"text": "oud"}))
            assert e.hub.links[sid].phase == "idle", "antwoord op een vraag die niet gesteld is"
            e.hub.links[sid].phase = "query"
            e.from_screen("state/" + MARKER, json.dumps({"text": "oud"}))
            await asyncio.sleep(0)
            e.mqtt_up = False
            await e.until(lambda: e.hub.links[sid].phase == "idle", what="push mislukt")
            e.mqtt_up = True
            e.from_screen("LWT", "online")
            assert e.hub.links[sid].phase == "query"

    run(go())


def test_the_screen_is_an_object_for_ha():
    async def go():
        async with Env() as e:
            sid = e.add()
            cfg = e.hub.discovery()[f"homeassistant/binary_sensor/kk_screen_{sid}_online/config"]
            assert cfg["device_class"] == "connectivity" and cfg["state_topic"] == hasphub.t_online(sid) and cfg["device"]["name"] == "Eetkamer"
            e.engine.model.remove_screen(sid)
            assert e.hub.links == {} and e.hub.discovery() == {}

    run(go())


def test_the_model_refuses_nonsense_and_keeps_the_config_in_the_backup():
    m = Model()
    sid = m.add_screen("Eetkamer", "scherm01")
    assert m.screens[sid] == {"name": "Eetkamer", "plate": "scherm01", "timeout": 30, "cards": [], "locale": "nl"}
    other = m.add_screen("Bureau")
    m.add_card("saver")
    for bad, match in (({"plate": "hasp/#"}, "firmware"), ({"plate": "scherm01"}, "eigenaar"), ({"cards": ["weg"]}, "onbekende kaart"), ({"timeout": -1}, "timeout"),
                       ({"cards": ["screensaver"]}, "screensaver"), ({"locale": "fr"}, "taal"), ({"pages": []}, "onbekend veld"), ({"onzin": 1}, "onbekend veld")):
        with pytest.raises(ModelError, match=match):
            m.set_screen(other, **bad)
    with pytest.raises(ModelError, match="eigenaar"):
        m.add_screen("Derde", "scherm01")
    with_cards(m, sid)
    copy = Model()
    copy.load_dict(m.to_dict())
    assert copy.screens == m.screens
    d = m.to_dict()
    d["screens"][other]["plate"] = "scherm01"
    with pytest.raises(ModelError, match="eigenaar"):
        Model().load_dict(d)
    assert m.remove_card("garden") and m.screens[sid]["cards"] == ["main"], "een kaart die weg is, gaat ook van het scherm"
    assert m.remove_screen(sid) is True and m.remove_screen(sid) is False


def test_the_old_pages_of_a_screen_become_cards_once():
    old = {"version": 1, "schema": {"pages": [{"id": "hal", "name": "Hal"}], "blocks": {"h:eet": {"page": "hal", "x": 600, "y": 40}}},
           "cards": {"main_room": {"type": "grid", "name": "Bestaat al"}},
           "screens": {"eet": {"name": "Eetkamer", "plate": "scherm01", "timeout": 30, "pages": [
               {"title": "Main room", "buttons": [{"label": "Kitchen", "entity": KITCHEN}, {"label": "", "entity": None}, {"label": "Slot", "entity": "lock.x"}]},
               {"title": "", "buttons": []}]}}}
    m = Model()
    m.load_dict(old, save=False)
    assert m.screens["eet"]["cards"] == ["main_room_2", "eetkamer_2"]
    assert m.cards["main_room"]["name"] == "Bestaat al", "een bestaande kaart blijft"
    c = m.cards["main_room_2"]
    assert c["name"] == "Main room" and c["layout"] == "grid"
    assert [(it["entity"], it["name"]) for it in c["items"]] == [(KITCHEN, "Kitchen"), (None, ""), (None, "Slot")], "wat een scherm niet kon bedienen wordt een lege knop"
    assert m.cards["eetkamer_2"]["name"] == "Eetkamer 2" and m.cards["eetkamer_2"]["items"] == []
    assert m.schema["blocks"]["c:main_room_2"]["page"] == "hal", "naast het scherm op het schema"
    again = Model()
    again.load_dict(m.to_dict())
    assert again.screens == m.screens and set(again.cards) == set(m.cards), "maar één keer"


def test_a_card_with_more_than_four_buttons_runs_over_two_pages():
    grid = {"title": "Licht", "key": "licht", "entities": [{"entity": f"switch.l{i}", "name": f"L{i}"} for i in range(6)] + [{"entity": "delete"}] * 2}
    s = Screen([grid, PAGES[1]], 0, SimpleNamespace(state=lambda e: ("off", {}), call=lambda *a: None), lambda c, p: None)
    assert [p["title"] for p in s.pages] == ["Licht (1/2)", "Licht (2/2)", "Garden"], "lege plekken achteraan maken geen pagina"
    by = {(o["page"], o["id"]): o for o in map(json.loads, s.design()) if "obj" in o}
    assert by[(2, 3)]["text"].endswith("L5") and (2, 4) not in by
    sent = []
    s._tx = lambda c, p: sent.append((c, p))
    for obj in ("p1b8", "p2b8", "p3b8", "p1b6"):
        s.on_event(obj, DOWN, 1.0)
    assert [p for c, p in sent if c == "page"] == ["2", "3", "1", "3"]
    with pytest.raises(hasp.LayoutError, match="13 pagina's"):
        Screen([grid] * 6 + [PAGES[1]], 0, None, None)
    with pytest.raises(hasp.LayoutError, match="minstens één kaart"):
        Screen([], 0, None, None)


def test_a_subpage_has_a_way_back_to_where_you_came_from():
    m = Model()
    m.add_screen("S")
    for name in ("Hal", "Boven", "Zolder"):
        m.add_card("grid", name)
    m.set_card("zolder", items=[{"entity": KITCHEN, "name": "Kitchen"}])
    m.set_card("hal", items=[{"ref": "c:zolder", "name": "Naar zolder"}])
    m.set_card("boven", items=[{"ref": "c:zolder"}])
    m.set_screen("s", cards=["hal", "boven"])
    pages = cards.screen_pages(m, "s")
    assert [(p["key"], p["sub"]) for p in pages] == [("hal", False), ("boven", False), ("zolder", True)]
    assert cards.users(m, "hal") == [{"key": "h:s", "name": "S", "i": 0, "as": "page"}]
    sent = []
    s = Screen(pages, 0, SimpleNamespace(state=lambda e: None, call=lambda *a: None), lambda c, p: sent.append((c, p)))
    by = {(o["page"], o["id"]): o for o in map(json.loads, s.design()) if "obj" in o}
    assert by[(1, 2)]["text"] == f"Naar zolder {hasp.ICON_NEXT}" and by[(2, 2)]["text"] == f"Zolder {hasp.ICON_NEXT}"
    assert by[(3, 7)]["text"] == "Terug" and (3, 6) not in by and (3, 8) not in by, "een subpagina: alleen terug"
    for obj in ("p2b2", "p3b7", "p1b2", "p3b7"):
        s.on_event(obj, DOWN, 1.0)
    assert [p for c, p in sent if c == "page"] == ["3", "2", "3", "1"]
    m.set_screen("s", locale="en")
    assert Screen(cards.screen_pages(m, "s"), 0, None, None, "en").hash != s.hash


def test_lamps_and_values_of_the_addon_on_a_screen():
    """Zoals op een NSPanel: de stand uit de add-on (cards.Live), ook voor een lamp van een andere installatie."""
    m = Model()
    m.set_light(5, 1, name="Plafond")
    m.add_card("grid", "Licht")
    m.set_card("licht", items=[{"ref": "l5.1"}, {"ref": "r:atelier:g:poort", "name": "Poort"}, {"entity": "sensor.t", "name": "Binnen"}])
    m.add_card("qr", "Wifi")
    m.set_card("wifi", ssid="Huis", password="geheim")
    m.add_screen("S")
    m.set_screen("s", cards=["licht", "wifi"])
    state, calls, remote = {"on": None}, [], {"name": "Poort atelier", "online": True, "on": True}
    eng = SimpleNamespace(light_state=lambda n, r: state["on"], toggle_light=lambda n, r: calls.append(("toggle", n, r)) or True)
    link = SimpleNamespace(object=lambda peer, obj: remote, send=lambda peer, obj, op: calls.append((peer, obj, op)) or True)
    live = cards.Live(m, engine=eng, link=link)
    env = SimpleNamespace(state=lambda e: ("21.5", {"unit_of_measurement": "°C"}) if e == "sensor.t" else None, call=lambda *a: calls.append(a),
                          dynamic=live.value, press=live.press)
    sent = []
    s = Screen(cards.screen_pages(m, "s"), 0, env, lambda c, p: sent.append((c, p)))
    by = {(o["page"], o["id"]): o for o in map(json.loads, s.design()) if "obj" in o}
    assert by[(1, 2)]["text"] == f"{hasp.ICON_LIGHT} Plafond" and by[(1, 2)]["bg_color"] == hasp.C_UNKNOWN, "stand nog niet bekend"
    assert by[(1, 3)]["text"] == f"{hasp.ICON_LIGHT} Poort atelier" and by[(1, 3)]["bg_color"] == hasp.C_ON, "de naam komt van de andere installatie"
    assert by[(1, 4)]["text"] == "Binnen\n21.5 °C"
    assert by[(2, 2)]["text"] == "Netwerk\nHuis" and by[(2, 3)]["text"] == "Wachtwoord\ngeheim"
    assert s.entities() == {"sensor.t"}, "alleen HA-entiteiten tellen als ontbrekend"
    s.ready(0.0)
    sent.clear()
    state["on"], remote["name"] = True, "Atelierpoort"
    s.sync()
    assert sent == [("p1b2.bg_color", hasp.C_ON), ("p1b3.text", f"{hasp.ICON_LIGHT} Atelierpoort")], "alleen wat veranderd is"
    for obj in ("p1b2", "p1b3", "p1b4", "p2b2"):
        s.on_event(obj, DOWN, 1.0)
    assert calls == [("toggle", 5, 1), ("atelier", "g:poort", "toggle")], "een sensor en een tekst doen niets"


def test_cards_that_do_not_fit_leave_the_screen_alone():
    async def go():
        async with Env() as e:
            sid = e.add()
            await e.online(sid)
            m = e.engine.model
            m.cards = {**m.cards, "groot": {"type": "grid", "name": "Groot", "layout": "grid",
                                             "items": [{"ref": None, "entity": KITCHEN, "name": str(i), "icon": "", "dp": ""} for i in range(8)]}}
            many = []
            for i in range(7):  # 14 pagina's
                cid = f"g{i}"
                m.cards = {**m.cards, cid: {**m.cards["groot"], "name": cid}}
                many.append(cid)
            e.sent.clear()
            m.set_screen(sid, cards=many)
            await asyncio.sleep(0.02)
            assert "12" in e.hub.status()[0]["error"] and e.hub.links[sid].screen is None
            assert not e.cmds("clearpage"), "het scherm houdt wat het heeft"
            e.from_screen("LWT", "online")
            assert not e.cmds(MARKER + ".text")

    run(go())


def test_chunks_stay_whole_and_small():
    lines = [f'{{"id":{i},"text":"{"é" * 50}"}}' for i in range(100)]
    parts = chunks(lines, 500)
    assert all(len(p.encode()) <= 500 for p in parts)
    assert [l for p in parts for l in p.split("\n")] == lines


def test_the_block_on_the_schema_and_the_web_api(tmp_path):
    async def go():
        async with Env() as e:
            ctx = web.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), e.engine)
            c = TestClient(TestServer(web.make_app(ctx)))
            await c.start_server()
            try:
                e.engine.model.set_schema({**e.engine.model.schema, "pages": [{"id": "hal", "name": "Hal"}]})
                r = await c.post("/api/screens", json={"page": "hal", "x": 40, "y": 60})
                j = await r.json()
                sid = j["id"]
                assert r.status == 200 and j["key"] == "h:scherm"
                with_cards(e.engine.model, sid)
                assert (await c.put(f"/api/screens/{sid}", json={"plate": "scherm01", "cards": ["garden", "main"]})).status == 200
                bad = await c.put(f"/api/screens/{sid}", json={"plate": "a/b"})
                assert bad.status == 400 and "firmware" in (await bad.json())["error"]
                assert (await c.put("/api/screens/nergens", json={"name": "x"})).status == 400
                st = await (await c.get("/api/state")).json()
                block = next(b for b in st["schema"]["blocks"] if b["key"] == "h:scherm")
                assert block["kind"] == "screen" and block["page"] == "hal" and (block["x"], block["y"]) == (40, 60)
                assert [c["name"] for c in block["cards"]] == ["Garden", "Main room"] and block["missing"] == ["switch.weg"] and block["gangs"] == []
                card = next(b for b in st["schema"]["blocks"] if b["key"] == "c:garden")
                assert card["users"] == [{"key": "h:scherm", "name": "Scherm", "i": 0, "as": "page"}]
                schema.view(e.engine.model, e.engine.snapshot())
                assert (await c.delete(f"/api/screens/{sid}")).status == 200 and (await c.delete(f"/api/screens/{sid}")).status == 404
                assert "h:scherm" not in e.engine.model.schema["blocks"], "weg van het schema"
            finally:
                await c.close()

    run(go())


def test_the_mqtt_bridge_refuses_to_publish_without_a_connection():
    from kkaddon.mqttbridge import MqttBridge

    bridge = MqttBridge(Settings(data_dir=None, key=KEY, key_generated=False), Engine(KEY, Model(), lambda raw: None))
    assert bridge.publish("hasp/x/command/page", "1") is False
    bridge.connected = True
    assert bridge.publish("hasp/x/command/page", "1") is True and bridge._queue.get_nowait() == ("hasp/x/command/page", "1", False)
