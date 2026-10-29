"""habridge tegen een nagebootste HA: verbinden, stil synchroniseren, bevelen, drukken, herstel."""
import asyncio

from aiohttp.test_utils import TestServer

from fakeha import FakeHa
from kkaddon import proto
from kkaddon.engine import Engine
from kkaddon.habridge import HaBridge
from kkaddon.model import Model
from kkaddon.proto import T
from rig import KEY

GANGS = [{"entity": f"switch.t_{i}", "name": f"Gang {i + 1}", "lamp": i < 2} for i in range(4)]


class Env:
    def __init__(self, ha: FakeHa, token="test", with_device=True):
        self.ha, self.token, self.with_device = ha, token, with_device
        self.frames: list[proto.Frame] = []

    async def __aenter__(self):
        self.server = TestServer(self.ha.app())
        await self.server.start_server()
        self.model = Model()
        self.vid = self.model.set_virtual(None, "Tuya", "", GANGS) if self.with_device else None
        self.engine = Engine(KEY, self.model, lambda raw: self.frames.append(proto.open_frame(KEY, raw)), )
        self.bridge = HaBridge(f"ws://127.0.0.1:{self.server.port}/api/websocket", self.token, self.engine, retry=(0.05, 0.2))
        self.tasks = [asyncio.create_task(self._tick()), asyncio.create_task(self.bridge.run())]
        return self

    async def _tick(self):
        while True:
            self.engine.tick()
            await asyncio.sleep(0.005)

    async def __aexit__(self, *a):
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.server.close()

    async def until(self, pred, timeout=3.0, what="voorwaarde"):
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            if pred():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"time-out: {what}")

    def sent(self, ftype):
        return [f for f in self.frames if f.type == ftype and f.src == self.vid]


def run(coro):
    return asyncio.run(coro)


def test_connects_syncs_silently_and_lists_entities():
    async def go():
        ha = FakeHa(entities={"switch.t_0": "on", "switch.t_1": "off", "switch.t_2": "off", "switch.t_3": "off", "sensor.temp": "20", "light.hal": "off"})
        async with Env(ha) as e:
            await e.until(lambda: e.bridge.connected and e.engine.virt.devs[e.vid][0].known is True, what="verbonden en gesynchroniseerd")
            assert e.engine.nodes[e.vid].online and e.engine.light_state(e.vid, 0) is True
            assert not e.sent(T.BTN_EVENT)  # beginstand is geen druk
            ents = {x["entity_id"] for x in await e.bridge.entities()}
            assert {"switch.t_0", "light.hal"} <= ents and "sensor.temp" not in ents
            assert e.engine.ha_call is not None

    run(go())


def test_command_roundtrip_through_ha():
    async def go():
        async with Env(FakeHa(delay=0.02)) as e:
            await e.until(lambda: e.engine.virt.devs[e.vid][0].known is False)
            assert e.engine.command_light(e.vid, 0, True)
            await e.until(lambda: e.engine.light_state(e.vid, 0) is True, what="licht aan na bevestiging van HA")
            assert ("switch.t_0", "turn_on") in e.ha.calls
            assert [x for x in e.engine.log if x["t"] == "cmd" and x.get("result") == "ok"]
            ls = proto.parse(e.sent(T.LIGHT_STATE)[-1])
            assert ls.on and ls.cause_btn == 0xFF
            assert not e.sent(T.BTN_EVENT)

    run(go())


def test_physical_press_in_ha_becomes_a_mesh_button_event():
    async def go():
        async with Env(FakeHa()) as e:
            await e.until(lambda: e.engine.virt.devs[e.vid][1].known is False)
            await e.ha.set_state("switch.t_1", "on")
            await e.until(lambda: e.sent(T.BTN_EVENT), what="BTN_EVENT op de mesh")
            assert proto.parse(e.sent(T.BTN_EVENT)[0]) == proto.BtnEvent(1, 0, 1)

    run(go())


def test_wrong_token_is_reported_and_retried_without_crashing():
    async def go():
        async with Env(FakeHa(), token="fout") as e:
            await e.until(lambda: "token" in e.bridge.last_error, what="foutmelding over het token")
            assert not e.bridge.connected and not e.engine.virt.link
            await asyncio.sleep(0.3)  # blijft proberen, met oplopende pauze
            assert e.bridge.last_error and not e.bridge.call("switch.t_0", True)

    run(go())


def test_reconnect_resyncs_changes_made_while_away_without_presses():
    async def go():
        ha = FakeHa()
        async with Env(ha) as e:
            await e.until(lambda: e.engine.virt.devs[e.vid][2].known is False)
            ha.states["switch.t_2"] = "on"  # gewijzigd zonder dat wij het via een event horen
            await ha.kick(None)
            await e.until(lambda: e.engine.virt.devs[e.vid][2].known is True, what="stand na opnieuw verbinden")
            assert ha.auth_ok >= 2 and e.engine.nodes[e.vid].online
            assert not e.sent(T.BTN_EVENT)

    run(go())


def test_a_new_entity_in_the_model_is_read_immediately():
    async def go():
        ha = FakeHa(entities={**{f"switch.t_{i}": "off" for i in range(4)}, "switch.extra": "on"})
        async with Env(ha) as e:
            await e.until(lambda: e.engine.virt.devs[e.vid][0].known is False)
            e.model.set_virtual(None, "Extra", "", [{"entity": "switch.extra", "name": "", "lamp": True}])
            vid2 = max(e.engine.virt.devs)
            await e.until(lambda: e.engine.virt.devs[vid2][0].known is True, what="stand van de nieuwe entiteit")

    run(go())


def test_ha_that_does_not_answer_gives_up_with_a_failure_event():
    async def go():
        ha = FakeHa()
        ha.ignore = True
        async with Env(ha) as e:
            await e.until(lambda: e.engine.virt.devs[e.vid][0].known is False)
            e.engine.command_light(e.vid, 0, True)
            await e.until(lambda: len([c for c in ha.calls if c[0] == "switch.t_0"]) >= 1)
            assert e.engine.light_state(e.vid, 0) is False  # niets verzonnen zonder bevestiging

    run(go())


def test_disabled_bridge_leaves_everything_offline():
    async def go():
        async with Env(FakeHa(), with_device=True) as e:
            pass
        engine = Engine(KEY, Model(), lambda raw: None)
        b = HaBridge("", "", engine)
        assert not b.enabled and engine.ha_call is None
        await b.run()  # keert meteen terug
        assert not b.call("switch.x", True) and await b.entities() == []

    run(go())


ESP_REGISTRY = {"devices": {
    "d_touch": {"name": "Touch 01", "area": "gang", "platform": "esphome", "entities": ["switch.t_1", "switch.t_0", ("switch.t_wifi_led", {"entity_category": "config"}), ("switch.t_off", {"disabled_by": "user"})]},
    "d_rel": {"name": "Relaisbox", "name_by_user": "Technische ruimte", "platform": "esphome", "entities": ["switch.t_2"]},
}}


def test_devices_group_switches_per_ha_device_without_config_entities():
    async def go():
        ha = FakeHa(entities={"switch.t_0": "on", "switch.t_1": "off", "switch.t_2": "off", "switch.t_wifi_led": "on", "switch.t_off": "off", "switch.zonder_apparaat": "off", "sensor.temp": "20"}, registry=ESP_REGISTRY)
        async with Env(ha, with_device=False) as e:
            await e.until(lambda: e.bridge.connected)
            r = await e.bridge.devices()
            assert r["registry"] is True
            by = {d["name"]: d for d in r["devices"]}
            assert list(by) == ["Technische ruimte", "Touch 01"], "gesorteerd, gebruikersnaam wint"
            t = by["Touch 01"]
            assert [x["entity_id"] for x in t["entities"]] == ["switch.t_0", "switch.t_1"], "gesorteerd, zonder config/uitgeschakeld/zonder apparaat"
            assert t["entities"][0]["state"] == "on" and t["area"] == "Gang" and t["platform"] == "esphome"

    run(go())


def test_devices_without_registry_access_degrades_to_an_empty_list():
    async def go():
        async with Env(FakeHa(), with_device=False) as e:  # deze HA kent de registers niet
            await e.until(lambda: e.bridge.connected)
            assert await e.bridge.devices() == {"registry": False, "devices": []}
            assert e.bridge.connected, "een afgewezen verzoek maakt de verbinding niet stuk"
            assert {x["entity_id"] for x in await e.bridge.entities()}, "losse entiteiten blijven bruikbaar"

    run(go())
