"""De hub tegen een nagebootste panel (echte UDP op localhost) en een nagebootste HA."""
import asyncio
import json
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer

from fakeha import FakeHa
from kkaddon import cards, panelhub, schema
from kkaddon.engine import Engine
from kkaddon.habridge import HaBridge
from kkaddon.model import Model, ModelError
from kkaddon.panelhub import PanelHub
from rig import KEY

KITCHEN, EAT = "switch.keuken", "switch.eettafel"
YAML = f"""
cards:
  - type: cardGrid
    title: Light
    entities:
      - entity: navigate.sub
  - type: cardPower
    title: Power
    entities:
      - entity: sensor.power
        name: power
        color: '{{{{iif(states("sensor.power") | float(1) < 0.3, "[0,255,0]", iif(states("sensor.power") | float(1) < 0.4, "[255,165,0]", "[255,0,0]"))}}}}'
hiddenCards:
  - type: cardGrid
    title: Main
    key: sub
    entities:
      - entity: {KITCHEN}
        name: Kitchen
      - entity: {EAT}
        name: Eating
screensaver:
  entities:
    - entity: weather.thuis
    - entity: weather.thuis
      type: 0
"""
TEMPLATE = cards._power_item({"entity": "sensor.power", "levels": [0.3, 0.4]})["color"]  # zo wordt de kleur van de YAML hierboven omgezet


class FakePanel(asyncio.DatagramProtocol):
    """Het Berry-script: luistert, bewaart wat het scherm moet doen, en kan 'aanrakingen' sturen."""

    def __init__(self):
        self.got: list[str] = []
        self.tp = None

    def connection_made(self, tp):
        self.tp = tp

    def datagram_received(self, data, addr):
        self.got.append(data.decode())

    def to_hub(self, hub_port, text):
        self.tp.sendto(text.encode(), ("127.0.0.1", hub_port))

    def screen(self):
        return [g[2:] for g in self.got if g.startswith("D|")]


class Env:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.offset = 0.0

    async def __aenter__(self):
        self.ha = FakeHa(entities={KITCHEN: "on", EAT: "off", "sensor.power": "0.35", "weather.thuis": "sunny"})
        self.ha.names[KITCHEN] = "Keuken"
        self.ha.attrs["weather.thuis"] = {"temperature": 18, "temperature_unit": "°C"}
        self.ha.attrs["sensor.power"] = {"device_class": "power", "unit_of_measurement": "kW"}
        self.ha.forecasts["weather.thuis"] = [{"datetime": "2026-11-01T12:00:00+00:00", "condition": "rainy", "temperature": 12}]
        self.ha.templates[TEMPLATE] = [255, 165, 0]
        self.server = TestServer(self.ha.app())
        await self.server.start_server()
        loop = asyncio.get_running_loop()
        self.fake = FakePanel()
        await loop.create_datagram_endpoint(lambda: self.fake, local_addr=("127.0.0.1", 0))
        self.engine = Engine(KEY, Model(), lambda raw: None)
        self.bridge = HaBridge(f"ws://127.0.0.1:{self.server.port}/api/websocket", "test", self.engine, retry=(0.05, 0.2))
        self.hub = self.new_hub()
        await self.hub.start()
        self.port = self.hub._tp.get_extra_info("sockname")[1]
        self.tasks = [asyncio.create_task(self._tick()), asyncio.create_task(self.bridge.run())]
        return self

    def new_hub(self):
        return PanelHub(self.tmp, self.bridge, self.engine, port=0, panel_port=self.fake.tp.get_extra_info("sockname")[1],
                        clock=lambda: time.monotonic() + self.offset)

    async def _tick(self):
        while True:
            self.engine.tick()
            self.hub.tick()
            await asyncio.sleep(0.01)

    async def __aexit__(self, *a):
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.hub.close()
        await self.server.close()

    async def until(self, pred, timeout=3.0, what="voorwaarde"):
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            if pred():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"time-out: {what}; panel kreeg {self.fake.got[-6:]}")

    def beat(self, **kw):
        self.fake.to_hub(self.port, "H|" + json.dumps({"p1": 0, "p2": 1, "t": 24.7, "rssi": -60, **kw}))

    def touch(self, text):
        self.fake.to_hub(self.port, "S|" + text)

    def add(self, host="127.0.0.1"):
        """Een paneel met de kaarten uit de YAML, zoals een paneel uit 0.10 na het omzetten (Engelse dagen: die YAML had geen locale)."""
        model = self.engine.model
        pid = model.add_panel("NSPanel 1", host)
        model.import_panel_yaml(pid, YAML)
        model.set_panel(pid, locale="en")
        return pid


def run(coro):
    return asyncio.run(coro)


def test_a_panel_that_reports_in_gets_its_screensaver_and_a_touch_opens_a_page(tmp_path):
    async def go():
        async with Env(tmp_path) as e:
            e.add()
            await e.until(lambda: e.bridge.connected, what="HA verbonden")
            assert e.fake.got == [] or all(g.startswith("Q|") for g in e.fake.got), "eerst stil: de panel is nog niet gezien"
            e.beat(boot=True)
            await e.until(lambda: any(s.startswith("weatherUpdate") for s in e.fake.screen()), what="screensaver")
            scr = e.fake.screen()
            assert scr[0].startswith("time~") and "pageType~screensaver" in scr
            weather = next(s for s in scr if s.startswith("weatherUpdate"))
            assert "~Thuis~18°C" in weather or "~weather.thuis~18°C" in weather or "~18°C" in weather
            assert "~Sun~12°C" in weather, "de voorspelling komt uit weather.get_forecasts"
            e.fake.got.clear()
            e.touch("event,buttonPress2,screensaver,bExit,2")
            e.touch("event,buttonPress2,navigate.main,button,")
            await e.until(lambda: any(s.startswith("entityUpd~Main~") for s in e.fake.screen()), what="verborgen pagina")
            page = next(s for s in e.fake.screen() if s.startswith("entityUpd~Main~"))
            assert "~switch~switch.keuken~" in page and "~Kitchen~1" in page and "~Eating~0" in page

    run(go())


def test_pressing_a_switch_goes_to_ha_and_the_new_state_comes_back_on_the_page(tmp_path):
    async def go():
        async with Env(tmp_path) as e:
            e.add()
            await e.until(lambda: e.bridge.connected)
            e.beat()
            await e.until(lambda: any(s.startswith("weatherUpdate") for s in e.fake.screen()))
            e.touch("event,buttonPress2,screensaver,bExit,2")
            e.touch("event,buttonPress2,navigate.main,button,")
            await e.until(lambda: any(s.startswith("entityUpd~Main~") for s in e.fake.screen()))
            e.fake.got.clear()
            e.touch(f"event,buttonPress2,{EAT},OnOff,1")
            await e.until(lambda: e.ha.calls == [(EAT, "turn_on")], what="turn_on bij HA")
            await e.until(lambda: any("~Eating~1" in s for s in e.fake.screen()), what="nieuwe stand op het scherm")
            e.fake.got.clear()
            e.touch(f"event,buttonPress2,{KITCHEN},button,")
            await e.until(lambda: ("switch", "toggle", {"entity_id": KITCHEN}) in e.ha.service_calls)
            await e.until(lambda: any("~Kitchen~0" in s for s in e.fake.screen()))

    run(go())


def test_a_template_answer_from_ha_recolours_the_page(tmp_path):
    async def go():
        async with Env(tmp_path) as e:
            e.add()
            await e.until(lambda: e.bridge.connected)
            e.beat()
            await e.until(lambda: any(s.startswith("weatherUpdate") for s in e.fake.screen()))
            e.touch("event,buttonPress2,screensaver,bExit,2")
            e.touch("event,buttonPress2,navigate.uuid.c1,button,")
            await e.until(lambda: any(s.startswith("entityUpd~Power~") and "~64800~power~" in s for s in e.fake.screen()), what="oranje")
            e.fake.got.clear()
            await e.ha.push_template(TEMPLATE, [0, 255, 0])
            await e.until(lambda: any(s.startswith("entityUpd~Power~") and "~2016~power~" in s for s in e.fake.screen()), what="groen")

    run(go())


def test_the_relays_are_set_and_reported_and_show_up_as_objects_for_ha(tmp_path):
    async def go():
        async with Env(tmp_path) as e:
            pid = e.add()
            e.beat(p1=0, p2=1)
            await e.until(lambda: e.hub.links[pid].online)
            assert e.hub.links[pid].relays == [False, True] and e.hub.links[pid].temp == 24.7 and e.hub.links[pid].rssi == -60
            assert e.hub.command(panelhub.t_relay_set(pid, 1), "ON") is True
            await e.until(lambda: "P|1|1" in e.fake.got)
            assert e.hub.command(panelhub.t_relay_set(pid, 2), "OFF") is True
            await e.until(lambda: "P|2|0" in e.fake.got)
            assert e.hub.command("kruisschakelaars/light/1/0/set", "ON") is False, "niet van ons"
            assert e.hub.states()[panelhub.t_relay(pid, 1)] == ("OFF", True) and e.hub.states()[panelhub.t_relay(pid, 2)] == ("ON", True)
            assert e.hub.states()[panelhub.t_temp(pid)] == ("24.7", True)
            cfg = e.hub.discovery()
            sw = cfg["homeassistant/switch/kk_panel_nspanel_1_relay1/config"]
            assert sw["command_topic"] == panelhub.t_relay_set(pid, 1) and sw["name"] == "Relais 1" and sw["has_entity_name"] is True
            assert sw["device"]["name"] == "NSPanel 1"
            assert cfg["homeassistant/sensor/kk_panel_nspanel_1_temp/config"]["device_class"] == "temperature"
            assert e.hub.set_relay("bestaat_niet", 1, True) is False and e.hub.set_relay(pid, 3, True) is False

    run(go())


def test_packets_from_other_addresses_are_ignored_and_a_silent_panel_goes_offline(tmp_path):
    async def go():
        async with Env(tmp_path) as e:
            pid = e.add("10.9.9.9")  # niet het adres van onze nepl panel
            e.beat()
            await asyncio.sleep(0.2)
            assert not e.hub.links[pid].online
            e.engine.model.set_panel(pid, host="127.0.0.1")
            e.beat()
            await e.until(lambda: e.hub.links[pid].online)
            e.offset = panelhub.STALE + 1
            await e.until(lambda: not e.hub.links[pid].online, what="offline")
            assert e.hub.states()[panelhub.t_status(pid)] == ("offline", True)
            e.offset = 0
            e.beat()
            await e.until(lambda: e.hub.links[pid].online)

    run(go())


def test_an_offline_panel_is_asked_for_a_heartbeat(tmp_path):
    async def go():
        async with Env(tmp_path) as e:
            e.add()
            await e.until(lambda: "Q|" in e.fake.got, what="vraag om een hartslag")

    run(go())


def test_the_cards_are_part_of_the_model_and_a_panel_without_pages_shows_why(tmp_path):
    async def go():
        async with Env(tmp_path) as e:
            pid = e.add()
            model = e.engine.model
            assert e.hub.links[pid].panel is not None
            with pytest.raises(ModelError, match="geldig"):
                model.set_panel(pid, host="1.2.3.4; rm -rf")
            with pytest.raises(ModelError, match="onbekende kaart"):
                model.set_panel(pid, cards=["bestaat_niet"])
            model.set_panel(pid, name="Gang")
            assert e.hub.links[pid].name == "Gang"
            exported = model.to_dict()
            assert exported["panels"][pid]["cards"] == ["screensaver", "light", "power"] and set(exported["cards"]) == {"screensaver", "light", "power", "main"}
            other = Model()
            other.load_dict(exported)
            assert other.panels == model.panels and other.cards == model.cards, "paneel en kaarten zitten in de back-up"
            model.set_panel(pid, cards=["screensaver"])
            assert e.hub.links[pid].panel is None and "minstens één kaart" in e.hub.links[pid].error
            model.set_panel(pid, cards=["main"])
            assert e.hub.links[pid].panel is not None, "een subpagina kan ook een hoofdpagina zijn"
            assert model.remove_card("main") is True and model.panels[pid]["cards"] == [] and model.cards["light"]["items"] == []
            assert model.remove_panel(pid) is True and model.remove_panel(pid) is False
            assert e.hub.links == {}, "de hub volgt het model"

    run(go())


def test_an_old_yaml_becomes_cards_at_startup_and_what_cannot_is_noted(tmp_path):
    async def go():
        async with Env(tmp_path) as e:
            model = e.engine.model
            d = model.to_dict()
            d["panels"] = {"oud": {"name": "Oud", "host": "127.0.0.1", "yaml": YAML.replace("cards:\n", "cards:\n  - type: cardThermo\n    title: Thermo\n", 1)}}
            model.load_dict(d, save=False)
            e.hub._sync()
            p = model.panels["oud"]
            assert p["cards"] == ["screensaver", "light", "power"] and p["locale"] == "en" and "yaml" not in p
            assert any("Thermo" in n and "cardThermo" in n for n in p["notes"]), p["notes"]
            assert model.cards["power"]["items"][0]["levels"] == [0.3, 0.4], "de kleur in drie stappen gaat mee als twee grenzen"
            assert e.hub.links["oud"].panel is not None and e.hub.links["oud"].error == ""
            d["panels"] = {"stuk": {"name": "Stuk", "host": "127.0.0.1", "yaml": "a: ["}}
            d.pop("cards", None)
            model.load_dict(d, save=False)
            assert model.panels["stuk"]["cards"] == [] and "niet worden omgezet" in model.panels["stuk"]["notes"][0]

    run(go())


def test_panels_json_of_version_0_8_is_taken_over_once(tmp_path):
    async def go():
        (tmp_path / "panels.json").write_text(json.dumps({"panels": {"nspanel_1": {"name": "NSPanel 1", "host": "127.0.0.1", "yaml": YAML}}}), encoding="utf-8")
        async with Env(tmp_path) as e:
            assert e.engine.model.panels["nspanel_1"]["cards"] == ["screensaver", "light", "power"] and e.hub.links["nspanel_1"].panel is not None
            assert not (tmp_path / "panels.json").exists() and (tmp_path / "panels.json.overgenomen").exists()

    run(go())


class FakeTuya:
    """Wat de laadpaalkaart van de TuyaHub gebruikt."""

    def __init__(self):
        self.devices = {"lader": {}}
        self.values = {"101": "connect", "124": "CloseCharging", "25": 3.5, "120": "01:02:03"}
        self.online = True
        self.sent = []

    def read(self, did):
        return dict(self.values) if did in self.devices else None

    def is_online(self, did):
        return self.online

    def set(self, did, dp, value):
        self.sent.append((did, dp, value))
        self.values[dp] = value


def test_a_lamp_of_the_addon_and_the_charger_are_shown_and_operated_from_the_panel(tmp_path):
    async def go():
        async with Env(tmp_path) as e:
            model = e.engine.model
            e.hub.tuya = FakeTuya()
            model.set_groups([{"id": "hal", "name": "Hal", "entities": [EAT, KITCHEN]}])
            model.set_group("hal", enabled=True, lamp=[KITCHEN])
            pid = model.add_panel("Gang", "127.0.0.1")
            model.add_card("grid", "Licht")
            model.set_card("licht", items=[{"ref": "g:hal"}])
            model.add_card("charger", "Laadpaal")
            model.set_card("laadpaal", tuya="lader", price=0.25)
            model.set_panel(pid, cards=["licht", "laadpaal"])
            await e.until(lambda: e.bridge.connected)
            await e.until(lambda: e.engine.groups.members["hal"][1].known is not None, what="stand van de kring")
            e.beat()
            await e.until(lambda: any(s.startswith("pageType~screensaver") for s in e.fake.screen()))
            e.touch("event,buttonPress2,screensaver,bExit,2")
            await e.until(lambda: any(s.startswith("entityUpd~Licht~") for s in e.fake.screen()), what="eerste pagina")
            page = next(s for s in e.fake.screen() if s.startswith("entityUpd~Licht~"))
            assert "~switch~kk.licht__0~" in page and "~Hal~1" in page, "de lamp heet zoals de kring en staat aan zoals haar lamp-relais"
            e.touch("event,buttonPress2,kk.licht__0,OnOff,0")
            await e.until(lambda: (KITCHEN, "turn_off") in e.ha.calls, what="de kring gaat uit")
            await e.until(lambda: any("~Hal~0" in s for s in e.fake.screen()), what="nieuwe stand op het scherm")
            e.fake.got.clear()
            e.touch("event,buttonPress2,navigate.uuid.c1,button,")
            await e.until(lambda: any(s.startswith("entityUpd~Laadpaal~") for s in e.fake.screen()), what="laadpaal")
            page = next(s for s in e.fake.screen() if s.startswith("entityUpd~Laadpaal~"))
            assert "~button~kk.laadpaal__run~" in page and "~Aangesloten~Start" in page
            assert "~Laadtijd~01:02:03" in page and "~Geladen~3.50 kWh" in page and "~Prijs~€ 0.88" in page
            e.touch("event,buttonPress2,kk.laadpaal__run,button")
            await e.until(lambda: e.hub.tuya.sent == [("lader", "124", "OpenCharging")], what="starten")
            await e.until(lambda: any("~Aangesloten~Stop" in s for s in e.fake.screen()), what="knop wordt Stop")
            e.hub.tuya.online = False
            await e.until(lambda: any("~Lader offline~" in s for s in e.fake.screen()), what="offline")
            e.touch("event,buttonPress2,kk.laadpaal__run,button")
            await asyncio.sleep(0.1)
            assert len(e.hub.tuya.sent) == 1, "een lader die weg is krijgt geen bevel"

    run(go())


def test_the_panel_is_a_block_on_the_schema_and_its_relay_can_be_the_output_of_a_lamp(tmp_path):
    async def go():
        async with Env(tmp_path) as e:
            pid = e.add()
            model = e.engine.model
            model.set_schema({**model.schema, "pages": [{"id": "hal", "name": "Hal"}]})
            schema.place(model, e.engine.snapshot(), f"p:{pid}", "hal", 40, 60)
            e.beat(p1=1, p2=0)
            await e.until(lambda: e.hub.links[pid].online)
            block = next(b for b in schema.view(model, e.engine.snapshot())["blocks"] if b["key"] == f"p:{pid}")
            assert block["kind"] == "panel" and block["page"] == "hal" and (block["x"], block["y"]) == (40, 60) and block["online"] and block["temperature"] == 24.7
            assert [(g["label"], g["on"], g["relay"], g["touch"]) for g in block["gangs"]] == [("Gang 1", True, True, True), ("Gang 2", False, True, True)]
            relay = block["gangs"][0]["entity"]
            assert relay == f"switch.kk_panel_{pid}_relay1"
            assert e.hub.discovery()[f"homeassistant/switch/kk_panel_{pid}_relay1/config"]["default_entity_id"] == relay, "dezelfde entiteit als in HA"
            model.set_groups([{"id": "hal", "name": "Hal", "entities": [EAT, KITCHEN]}])
            schema.wire(model, e.engine, "g:hal", "out", {"entity": relay})
            lamp = next(b for b in schema.view(model, e.engine.snapshot())["blocks"] if b["key"] == "g:hal")
            out = next(c for c in lamp["outputs"] if c["ref"]["entity"] == relay)
            assert out["sw"] == f"p:{pid}" and out["label"] == "Gang 1" and out["swName"] == "NSPanel 1"
            touch = block["gangs"][1]["entity"]
            schema.wire(model, e.engine, "g:hal", "in", {"entity": touch})
            lamp = next(b for b in schema.view(model, e.engine.snapshot())["blocks"] if b["key"] == "g:hal")
            inp = next(c for c in lamp["inputs"] if c["ref"]["entity"] == touch)
            assert inp["sw"] == f"p:{pid}" and inp["label"] == "Gang 2", "de knop als ingang van een lamp"
            model.remove_panel(pid)
            assert f"p:{pid}" not in model.schema["blocks"], "weg van het schema"

    run(go())


def test_the_web_api_adds_edits_and_removes_panels_and_serves_the_script(tmp_path):
    from aiohttp.test_utils import TestClient

    from kkaddon import web
    from kkaddon.settings import Settings

    async def go():
        async with Env(tmp_path) as e:
            ctx = web.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), e.engine, panels=e.hub)
            c = TestClient(TestServer(web.make_app(ctx)))
            await c.start_server()
            try:
                e.engine.model.set_schema({**e.engine.model.schema, "pages": [{"id": "hal", "name": "Hal"}]})
                r = await c.post("/api/panels", json={"page": "hal", "x": 40, "y": 60})
                j = await r.json()
                pid = j["id"]
                assert r.status == 200 and j["key"] == "p:nspanel"
                st = await (await c.get("/api/state")).json()
                assert next(b for b in st["schema"]["blocks"] if b["key"] == j["key"])["page"] == "hal"
                assert (await c.put(f"/api/panels/{pid}", json={"host": "127.0.0.1"})).status == 200
                assert (await c.put(f"/api/panels/{pid}", json={"yaml": YAML})).status == 400, "een YAML zetten kan niet meer: alleen omzetten"
                r = await c.post(f"/api/panels/{pid}/yaml", json={"yaml": YAML})
                j = await r.json()
                assert r.status == 200 and set(j["cards"]) == {"screensaver", "light", "power", "main"} and j["notes"] == []
                st = await (await c.get("/api/state")).json()
                blocks = {b["key"]: b for b in st["schema"]["blocks"]}
                assert blocks["c:light"]["page"] == "hal" and blocks["c:main"]["page"] == "hal", "de kaarten staan bij het paneel"
                assert [x["key"] for x in blocks[f"p:{pid}"]["cards"]] == ["c:screensaver", "c:light", "c:power"]
                assert blocks["c:main"]["users"] == [{"key": "c:light", "name": "Light", "i": 0, "as": "sub"}]
                assert blocks["c:light"]["items"][0]["target"] == "c:main" and blocks["c:light"]["items"][0]["label"] == "→ Main"
                assert (await c.post(f"/api/panels/{pid}/yaml", json={"yaml": "a: ["})).status == 400
                r = await c.post("/api/cards", json={"type": "charger", "page": "hal", "x": 0, "y": 400})
                assert r.status == 200 and (await r.json())["key"] == "c:laadpaal"
                assert (await c.put("/api/cards/laadpaal", json={"price": 0.31, "tuya": "lader"})).status == 200
                assert (await c.put("/api/cards/laadpaal", json={"price": -1})).status == 400
                assert (await c.put("/api/cards/laadpaal", json={"items": []})).status == 400, "een laadpaal heeft geen knoppen"
                assert (await c.put("/api/cards/light", json={"items": [{"ref": "c:screensaver"}]})).status == 400, "een screensaver is geen subpagina"
                assert (await c.put("/api/cards/light", json={"items": [{"name": "a~b"}]})).status == 400, "een ~ breekt het scherm"
                assert (await c.put(f"/api/panels/{pid}", json={"cards": ["screensaver", "laadpaal", "light"]})).status == 200
                assert (await c.post("/api/cards", json={"type": "saver"})).status == 200
                assert (await c.put(f"/api/panels/{pid}", json={"cards": ["screensaver", "screensaver_2"]})).status == 400, "één screensaver"
                assert (await c.delete("/api/cards/laadpaal")).status == 200 and e.engine.model.panels[pid]["cards"] == ["screensaver", "light"]
                assert (await c.put("/api/panels/nergens", json={"name": "x"})).status == 400
                assert (await c.put(f"/api/panels/{pid}", json={"onzin": 1})).status == 400
                assert (await c.post(f"/api/panels/{pid}/relay/1", json={"on": True})).status == 200
                await e.until(lambda: "P|1|1" in e.fake.got)
                assert (await c.post(f"/api/panels/{pid}/relay/7", json={"on": True})).status == 400
                assert (await c.get("/api/panels/script")).status == 400, "zonder adres van HA is het script nutteloos"
                assert (await c.get("/api/panels/script?hub=1.2.3.4;x")).status == 400
                script = await (await c.get("/api/panels/script?hub=192.168.0.180")).text()
                assert 'var HUB_HOST = "192.168.0.180"' in script and f"var HUB_PORT = {e.hub.port}" in script
                assert f"var LISTEN_PORT = {e.hub.panel_port}" in script and "@" not in script
                assert (await c.delete(f"/api/panels/{pid}")).status == 200 and (await c.delete(f"/api/panels/{pid}")).status == 404
            finally:
                await c.close()

    run(go())
