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
    def __init__(self, ha: FakeHa, token="test", with_device=True, **bridge_kw):
        self.ha, self.token, self.with_device, self.bridge_kw = ha, token, with_device, bridge_kw
        self.frames: list[proto.Frame] = []

    async def __aenter__(self):
        self.server = TestServer(self.ha.app())
        await self.server.start_server()
        self.model = Model()
        self.vid = self.model.set_virtual(None, "Tuya", "", GANGS) if self.with_device else None
        self.engine = Engine(KEY, self.model, lambda raw: self.frames.append(proto.open_frame(KEY, raw)), )
        self.bridge = HaBridge(f"ws://127.0.0.1:{self.server.port}/api/websocket", self.token, self.engine, retry=(0.05, 0.2), **self.bridge_kw)
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


INV_REGISTRY = {"devices": {
    "d_touch": {"name": "Touch 01", "area": "gang", "platform": "esphome", "sw_version": "2021.8.0 (compiled Aug 2021)", "configuration_url": "http://192.168.1.50:80",
                "entities": ["switch.t_0", ("switch.t_wifi_led", {"entity_category": "config"})]},
    "d_zon": {"name": "Zon", "platform": "sun", "entities": ["sensor.temp"]},
}}


def test_inventory_gives_platform_version_and_address_for_all_devices():
    async def go():
        ha = FakeHa(entities={"switch.t_0": "on", "switch.t_wifi_led": "on", "sensor.temp": "20"}, registry=INV_REGISTRY)
        async with Env(ha, with_device=False) as e:
            await e.until(lambda: e.bridge.connected)
            r = await e.bridge.inventory()
            by = {d["name"]: d for d in r["devices"]}
            t = by["Touch 01"]
            assert r["registry"] is True and list(by) == ["Touch 01", "Zon"]
            assert t["platforms"] == ["esphome"] and t["version"].startswith("2021.8.0") and t["ip"] == "192.168.1.50" and t["area"] == "Gang"
            assert t["entities"] == ["switch.t_0", "switch.t_wifi_led"], "ook config-entiteiten: het gaat om het apparaat, niet om de bediening"
            assert by["Zon"]["ip"] == "" and by["Zon"]["version"] == ""

    run(go())


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


def test_group_members_follow_each_other_through_ha():
    """Een kring tegen de nagebootste HA: een druk op de ene touch zet de andere leden aan, zonder heen-en-weer."""
    async def go():
        ha = FakeHa(delay=0.03, entities={"switch.t_0": "off", "switch.t_1": "off", "switch.t_2": "off", "switch.t_3": "off"})
        async with Env(ha, with_device=False) as e:
            e.model.set_groups([{"id": "gang", "name": "Gang", "entities": ["switch.t_0", "switch.t_1", "switch.t_2"]}])
            await e.until(lambda: e.bridge.connected)
            await asyncio.sleep(0.5)  # momentopname na de modelwijziging
            await ha.set_state("switch.t_0", "on")  # kring uit: niets volgt
            await asyncio.sleep(0.3)
            assert ha.calls == [] and ha.states["switch.t_1"] == "off"
            await ha.set_state("switch.t_0", "off")
            e.model.set_group("gang", enabled=True)
            await asyncio.sleep(0.5)
            await ha.set_state("switch.t_1", "on")
            await e.until(lambda: ha.states["switch.t_0"] == "on" and ha.states["switch.t_2"] == "on", what="de andere leden volgen")
            await asyncio.sleep(0.4)
            assert sorted(ha.calls) == [("switch.t_0", "turn_on"), ("switch.t_2", "turn_on")], f"precies een bevel per lid: {ha.calls}"
            assert ha.states["switch.t_3"] == "off", "een entiteit buiten de kring blijft ongemoeid"
            assert [x for x in e.engine.log if x["t"] == "sync" and x["what"] == "follow"]

    run(go())


def _view(ha):
    return next((v for v in (ha.overview or {}).get("views", []) if v.get("path") == "huisconfig-lampen"), None)


def _cards(ha):
    v = _view(ha)
    return {c["title"]: [x["entity"] for x in c["entities"]] for c in v["cards"]} if v else {}


def _reg():
    return {"devices": {
        "d1": {"name": "Relaisbox", "area": "keuken", "platform": "mqtt", "entities": [("light.plafond", {"unique_id": "kk_2_0"}), ("light.spots", {"unique_id": "kk_2_1"})]},
        "d2": {"name": "Hal", "area": None, "platform": "mqtt", "entities": [("light.hal", {"unique_id": "kk_3_0"})]},
    }}


def _place(model, key, room):
    """Een lampblok op een pagina (kamer) van het schema zetten."""
    pid = room.lower()
    if not any(p["id"] == pid for p in model.schema["pages"]):
        model.schema["pages"].append({"id": pid, "name": room})
    model.schema["blocks"][key] = {"page": pid, "x": 0, "y": 0}


def test_the_lights_tab_joins_the_overview_without_touching_the_other_tabs():
    from kkaddon.model import Light

    async def go():
        # een extern apparaat (zoals de Dingtian): de gangen zijn HA-schakelaars, het licht van de add-on is verborgen
        ha = FakeHa(entities={"switch.t_0": "off", "switch.t_1": "off", "switch.t_2": "off", "switch.t_3": "off", "light.plafond": "off"}, registry=_reg())
        async with Env(ha, dashboard_delay=0.05) as e:
            e.model.lights[(2, 0)] = Light(2, 0, "Plafond")  # een licht van een node: via MQTT in HA
            _place(e.model, f"l{e.vid}.0", "Keuken")
            _place(e.model, f"l{e.vid}.1", "Bureau")
            _place(e.model, "l2.0", "Keuken")
            await e.until(lambda: e.bridge.connected, what="verbonden")
            await e.bridge.sync_dashboard()
            assert [v["path"] for v in ha.overview["views"]] == ["zwembad", "huisconfig-lampen"]  # de eigen tab komt erbij
            v = _view(ha)
            assert {c["title"]: [(x["entity"], x["name"]) for x in c["entities"]] for c in v["cards"]} == {
                "Bureau": [("switch.t_1", "Gang 2")],
                "Keuken": [("switch.t_0", "Gang 1"), ("light.plafond", "Plafond")],
            }
            assert ha.saves == 1

            assert await e.bridge.sync_dashboard() is False  # niets veranderd: niet opnieuw schrijven
            e.model.schema["blocks"].pop(f"l{e.vid}.1")  # van het schema af: geen kamer meer
            await e.bridge.sync_dashboard()
            assert _cards(ha)["Zonder kamer"] == ["switch.t_1"] and list(_cards(ha))[-1] == "Zonder kamer"
            e.model.lights.pop((2, 0))  # verwijderd
            await e.bridge.sync_dashboard()
            assert _cards(ha)["Keuken"] == ["switch.t_0"] and ha.saves == 3
            assert [v["path"] for v in ha.overview["views"]] == ["zwembad", "huisconfig-lampen"]  # niet dubbel

    run(go())


def test_after_reconnecting_an_unchanged_tab_is_not_written_again_and_it_can_be_switched_off():
    async def go():
        ha = FakeHa(registry=_reg())
        async with Env(ha, dashboard_delay=0.05) as e:
            await e.until(lambda: ha.saves == 1, what="eerste keer geschreven")
            await e.ha.kick(None)
            await e.until(lambda: ha.auth_ok == 2, what="opnieuw verbonden")
            await asyncio.sleep(0.4)
            assert ha.saves == 1
        ha2 = FakeHa(entities={"light.plafond": "off"}, registry=_reg())
        async with Env(ha2, with_device=False, dashboard=False, dashboard_delay=0.05) as e:
            await e.until(lambda: e.bridge.connected)
            await asyncio.sleep(0.3)
            assert ha2.saves == 0

    run(go())


def test_an_automatic_overview_is_left_alone():
    async def go():
        ha = FakeHa(entities={"light.plafond": "off"}, registry=_reg())
        ha.overview = None  # niet overgenomen: er is geen opgeslagen config om bij te werken
        async with Env(ha, with_device=False, dashboard_delay=0.05) as e:
            await e.until(lambda: e.bridge.connected)
            await asyncio.sleep(0.3)
            assert ha.overview is None and ha.saves == 0

    run(go())


def test_the_old_sidebar_dashboard_of_0_18_0_is_removed_but_other_dashboards_stay():
    async def go():
        ha = FakeHa(registry=_reg())
        ha.dashboards = {"d1": "huisconfig-lampen", "d2": "mijn-eigen"}
        async with Env(ha, dashboard_delay=0.05) as e:
            await e.until(lambda: ha.saves == 1, what="tab geschreven")
            assert ha.dashboards == {"d2": "mijn-eigen"}

    run(go())


def test_an_entity_that_ha_does_not_know_stays_out_of_the_tab_until_it_appears():
    async def go():
        ha = FakeHa(entities={"switch.t_0": "off"}, registry=_reg())  # switch.t_1 bestaat (nog) niet in HA
        async with Env(ha, dashboard_delay=0.05) as e:
            _place(e.model, f"l{e.vid}.0", "Keuken")
            _place(e.model, f"l{e.vid}.1", "Bureau")
            await e.until(lambda: ha.saves == 1, what="tab geschreven")
            assert _cards(ha) == {"Keuken": ["switch.t_0"]}  # geen waarschuwingsrij voor een onbekende entiteit
            await ha.set_state("switch.t_1", "off")
            await e.until(lambda: ha.saves == 2, what="opnieuw opgebouwd")
            assert _cards(ha) == {"Bureau": ["switch.t_1"], "Keuken": ["switch.t_0"]}

    run(go())


def test_the_airco_tab_follows_the_airco_s_and_goes_away_with_the_last_one():
    async def go():
        reg = _reg()
        reg["devices"]["d3"] = {"name": "Airco living", "area": None, "platform": "mqtt", "entities": [("climate.kk_gree_a1", {"unique_id": "kk_gree_a1"}),
                                                                                                         ("climate.kk_gree_b2", {"unique_id": "kk_gree_b2"})]}
        ha = FakeHa(entities={"switch.t_0": "off", "switch.t_1": "off", "climate.kk_gree_a1": "cool", "climate.kk_gree_b2": "off"}, registry=reg)
        names = {"a1": "Living", "b2": "Bureau"}
        async with Env(ha, dashboard_delay=0.05) as e:
            e.bridge.airco_names = lambda: names
            await e.until(lambda: e.bridge.connected, what="verbonden")
            await e.bridge.sync_dashboard()
            assert [v["path"] for v in ha.overview["views"]] == ["zwembad", "huisconfig-lampen", "huisconfig-airco"]  # naast de lampen
            air = next(v for v in ha.overview["views"] if v["path"] == "huisconfig-airco")
            assert air["cards"] == [{"type": "thermostat", "entity": "climate.kk_gree_b2", "name": "Bureau"},
                                    {"type": "thermostat", "entity": "climate.kk_gree_a1", "name": "Living"}]
            names.pop("b2")  # een airco verwijderd of hernoemd
            e.bridge.refresh_dashboard()
            await e.until(lambda: ha.saves == 2, what="tab bijgewerkt")
            assert [c["entity"] for c in next(v for v in ha.overview["views"] if v["path"] == "huisconfig-airco")["cards"]] == ["climate.kk_gree_a1"]
            names.clear()  # geen airco's meer: de tab verdwijnt, de rest blijft
            e.bridge.refresh_dashboard()
            await e.until(lambda: ha.saves == 3, what="tab weg")
            assert [v["path"] for v in ha.overview["views"]] == ["zwembad", "huisconfig-lampen"]

    run(go())
