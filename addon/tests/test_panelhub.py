"""De hub tegen een nagebootste panel (echte UDP op localhost) en een nagebootste HA."""
import asyncio
import json
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer

from fakeha import FakeHa
from kkaddon import panelhub
from kkaddon.engine import Engine
from kkaddon.habridge import HaBridge
from kkaddon.model import Model
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
        color: '{{{{ states("sensor.power") }}}}'
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
TEMPLATE = '{{ states("sensor.power") }}'


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
        return PanelHub(self.tmp / "panels.json", self.bridge, self.engine, port=0, panel_port=self.fake.tp.get_extra_info("sockname")[1],
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

    def add(self):
        return self.hub.create("NSPanel 1", "127.0.0.1", YAML)


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
            e.touch("event,buttonPress2,screensaver,bExit,1")
            e.touch("event,buttonPress2,navigate.sub,button,")
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
            e.touch("event,buttonPress2,screensaver,bExit,1")
            e.touch("event,buttonPress2,navigate.sub,button,")
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
            e.touch("event,buttonPress2,screensaver,bExit,1")
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
            pid = e.hub.create("NSPanel 1", "10.9.9.9", YAML)  # niet het adres van onze nepl panel
            e.beat()
            await asyncio.sleep(0.2)
            assert not e.hub.links[pid].online
            e.hub.update(pid, host="127.0.0.1")
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


def test_a_bad_yaml_is_refused_and_the_old_config_stays_and_the_config_survives_a_restart(tmp_path):
    async def go():
        async with Env(tmp_path) as e:
            pid = e.add()
            with pytest.raises(ValueError, match="cardThermo"):
                e.hub.update(pid, yaml="cards:\n  - type: cardThermo\n    title: X\n")
            assert e.hub.links[pid].yaml == YAML and e.hub.links[pid].panel is not None
            with pytest.raises(ValueError, match="geldig"):
                e.hub.update(pid, host="1.2.3.4; rm -rf")
            e.hub.update(pid, name="Gang", host="127.0.0.1")
            again = e.new_hub()
            assert [(l.id, l.name, l.host, l.yaml == YAML) for l in again.links.values()] == [(pid, "Gang", "127.0.0.1", True)]
            assert again.links[pid].panel is not None
            assert e.hub.remove(pid) is True and e.hub.remove(pid) is False
            assert json.loads((tmp_path / "panels.json").read_text())["panels"] == {}

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
                r = await c.post("/api/panels", json={"name": "NSPanel 1", "host": "127.0.0.1", "yaml": YAML})
                pid = (await r.json())["id"]
                assert r.status == 200 and pid == "nspanel_1"
                bad = await c.put(f"/api/panels/{pid}", json={"yaml": "cards:\n  - type: cardThermo\n    title: X\n"})
                assert bad.status == 400 and "cardThermo" in (await bad.json())["error"]
                st = await (await c.get("/api/panels")).json()
                assert [p["id"] for p in st["panels"]] == [pid] and st["panels"][0]["yaml"] == YAML and st["panels"][0]["online"] is False
                assert (await c.put("/api/panels/nergens", json={"name": "x"})).status == 400
                assert (await c.post(f"/api/panels/{pid}/relay/1", json={"on": True})).status == 200
                await e.until(lambda: "P|1|1" in e.fake.got)
                assert (await c.post(f"/api/panels/{pid}/relay/7", json={"on": True})).status == 400
                assert (await c.get("/api/panels/script")).status == 400, "zonder adres van HA is het script nutteloos"
                assert (await c.get("/api/panels/script?hub=1.2.3.4;x")).status == 400
                script = await (await c.get("/api/panels/script?hub=192.168.0.180")).text()
                assert 'var HUB_HOST = "192.168.0.180"' in script and f"var HUB_PORT = {e.hub.port}" in script
                assert f"var LISTEN_PORT = {e.hub.panel_port}" in script and "@" not in script.replace("@HUB", "")
                assert (await c.delete(f"/api/panels/{pid}")).status == 200 and (await c.delete(f"/api/panels/{pid}")).status == 404
                html = await (await c.get("/")).text()
                assert "static/panel.js?v=" in html and 'data-tab="panel"' in html
            finally:
                await c.close()

    run(go())
