"""Kaarten voor het NSPanel: de omgezette config van het huis geeft hetzelfde scherm als de YAML, en de rijen uit de add-on."""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from kkaddon import cards
from kkaddon.chargehub import ChargeHub
from kkaddon.engine import Engine
from kkaddon.model import Model, ModelError
from kkaddon.panel import Panel

from rig import KEY
from test_panel import FakeEnv

YAML = (Path(__file__).parent / "data" / "nspanel1.yaml").read_text(encoding="utf-8")
OLD_TPL = '{{iif(states("sensor.power_usage") | float(1) < 0.3, "[0,255,0]", iif(states("sensor.power_usage") | float(1) < 0.4, "[255,165,0]", "[255,0,0]"))}}'
NEW_TPL = cards._power_item({"entity": "sensor.power_usage", "levels": [0.3, 0.4]})["color"]
KEYS = {"subMain": "main_room", "subQPhil": "quarto_philippe", "subQVis": "quarto_das_visitas", "subOut": "outside", "subOth": "other"}


def converted() -> Model:
    m = Model()
    m.load_dict({"version": 1, "panels": {"p": {"name": "P", "host": "1.2.3.4", "yaml": YAML}}}, save=False)
    return m


def screen(source, events):
    env, out = FakeEnv(), []
    env.templates[OLD_TPL] = env.templates[NEW_TPL] = [255, 165, 0]
    p = Panel(source, env, out.append)
    p.reset()
    for e in events:
        p.on_screen(e)
    return out, env


def test_the_converted_house_config_gives_the_same_screen_as_the_yaml():
    m = converted()
    assert m.panels["p"]["cards"] == ["screensaver", "light", "guest_wifi", "power_usage", "airco"]
    walk = ["event,startup,59,eu", "event,buttonPress2,screensaver,bExit,2", "event,buttonPress2,navigate.uuid.c1,button,",
            "event,buttonPress2,navigate.uuid.c2,button,", "event,buttonPress2,navigate.uuid.c3,button,", "event,buttonPress2,navigate.uuid.c0,button,"]
    old, _ = screen(YAML, walk + ["event,buttonPress2,navigate.subMain,button,", "event,buttonPress2,switch.dingtian_relay81038_r3_eat_room,OnOff,1"])
    new, env = screen(cards.config(m, "p"), walk + ["event,buttonPress2,navigate.main_room,button,", "event,buttonPress2,switch.dingtian_relay81038_r3_eat_room,OnOff,1"])
    norm = [re.sub(r"navigate\.(sub\w+)", lambda x: f"navigate.{KEYS[x[1]]}", s).replace("~SSID~", "~Network~").replace("~Pass~", "~Password~") for s in old]
    assert new == norm, "alleen de namen van de subpagina's en de labels op de QR-kaart verschillen"
    assert env.calls == [("homeassistant", "turn_on", {"entity_id": "switch.dingtian_relay81038_r3_eat_room"})]


def test_the_conversion_notes_what_it_leaves_out():
    text = re.sub(r"( +)name: Kitchen\n", r"\1name: Kitchen\n\1state: 'on'\n", YAML).replace("    model: eu\n", "    model: eu\n    sleepBrightness: input_number.dim\n")
    new, panel, notes = cards.from_yaml(text, set())
    assert any("'state' bij switch.dingtian_relay81038_r1_kitchen" in n for n in notes)
    assert any("sleepBrightness" in n for n in notes) and "dim" not in panel and "locale" not in panel
    assert new["main_room"]["items"][0]["entity"] == "switch.dingtian_relay81038_r1_kitchen", "de knop zelf gaat wel mee"
    _, _, notes = cards.from_yaml("cards:\n  - type: cardQR\n    title: Site\n    qrCode: https://example.org\n", set())
    assert notes == ["Site: alleen een wifi-QR-code kan worden overgenomen, deze kaart is weggelaten"]
    assert cards._wifi(r"WIFI:S:a\;b;T:WPA;P:p\:w;;") == {"ssid": "a;b", "password": "p:w", "security": "WPA"}
    assert cards.wifi_code("a;b", "p:w", "WPA") == r"WIFI:S:a\;b;T:WPA;P:p\:w;;"


def test_a_subpage_can_hold_further_subpages_and_a_loop_does_not_hang():
    m = Model()
    m.add_panel("P")
    for name in ("Boven", "Kamer", "Bad"):
        m.add_card("grid", name)
    m.set_card("boven", items=[{"ref": "c:kamer"}, {"ref": "c:bad"}])
    m.set_card("kamer", items=[{"ref": "c:boven", "name": "Terug naar boven"}])
    m.set_panel("p", cards=["boven"])
    saver, pages, hidden = cards.pages_of(m, "p")
    assert (saver, pages, hidden) == (None, ["boven"], ["kamer", "bad"])
    cfg = cards.config(m, "p")
    assert [c["key"] for c in cfg["hiddenCards"]] == ["kamer", "bad"] and cfg["cards"][0]["entities"][0] == {"entity": "navigate.kamer", "name": None, "icon": None}
    assert cards.users(m, "boven") == [{"key": "p:p", "name": "P", "i": 0, "as": "page"}, {"key": "c:kamer", "name": "Kamer", "i": 0, "as": "sub"}]


def test_cards_are_checked_on_the_way_in():
    m = Model()
    m.add_card("grid", "Raster")
    with pytest.raises(ModelError, match="maximaal 8"):
        m.set_card("raster", items=[{"entity": f"switch.x{i}"} for i in range(9)])
    with pytest.raises(ModelError, match="maximaal 4"):
        m.set_card("raster", layout="list", items=[{"entity": f"switch.x{i}"} for i in range(5)])
    with pytest.raises(ModelError, match="óf"):
        m.set_card("raster", items=[{"ref": "g:hal", "entity": "switch.x"}])
    with pytest.raises(ModelError, match="eigen kaart"):
        m.set_card("raster", items=[{"ref": "c:raster"}])
    with pytest.raises(ModelError, match="bestaat niet"):
        m.set_card("raster", items=[{"ref": "c:weg"}])
    with pytest.raises(ModelError, match="pictogram"):
        m.set_card("raster", items=[{"entity": "switch.x", "icon": "Lamp!"}])
    m.set_card("raster", items=[{"entity": "switch.x", "icon": "mdi:lamp"}, {}])
    assert m.cards["raster"]["items"] == [{"ref": None, "entity": "switch.x", "name": "", "icon": "lamp", "dp": ""}, {"ref": None, "entity": None, "name": "", "icon": "", "dp": ""}]
    with pytest.raises(ModelError, match="onbekend veld"):
        m.set_card("raster", tuya="lader")
    m.add_card("power")
    with pytest.raises(ModelError, match="oplopende"):
        m.set_card("energie", items=[{"entity": "sensor.p", "levels": [0.4, 0.3]}])
    m.add_card("qr")
    with pytest.raises(ModelError, match="WPA"):
        m.set_card("wifi_qr", security="WPA3")


class FakeTuya:
    def __init__(self, values, online=True):
        self.devices, self.values, self.online, self.sent = {"lader": {}}, values, online, []

    def read(self, did):
        return dict(self.values)

    def status(self):  # zoals TuyaHub.status: wat points.Points leest
        return [{"id": d, "name": d.capitalize(), "online": self.online, "points": [{"dp": dp, "name": f"Punt {dp}", "value": v, "unit": "", "rw": True}
                                                                             for dp, v in self.values.items()]} for d in self.devices]

    def is_online(self, did):
        return self.online

    def set(self, did, dp, value):
        self.sent.append((dp, value))


class Clock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def __call__(self):
        return self.t


def charger(tmp_path, tuya, clock=None, **cfg):
    """Een model met een laadpaalkaart die wijst naar een laadpaal-blok op het Tuya-toestel `lader`."""
    m = Model()
    hub = ChargeHub(tmp_path, Engine(KEY, m, lambda raw: None), tuya, clock=clock or Clock())
    cid = hub.add({"name": "Laadpaal", "device": "lader", "inputs": {"status": "101", "time": "120", "energy": "25"},
                   "target": {"dp": "124", "on": "OpenCharging", "off": "CloseCharging"}, **cfg})
    m.add_card("charger")
    m.set_card("laadpaal", charger=f"k:{cid}")
    return m, hub


def test_the_charger_rows(tmp_path):
    t = FakeTuya({"101": "connect", "124": "CloseCharging", "25": 10, "120": "02:00:00"})
    m, hub = charger(tmp_path, t, price=0.3, price_entity="sensor.tarief")
    hub.state = lambda e: ("0.2", {}) if e == "sensor.tarief" else None
    live = cards.Live(m, tuya=t, charge=hub)
    assert live.value("kk.laadpaal__run", "nl") == {"kind": "button", "name": "Aangesloten", "value": "Start", "color": cards.DEFAULT_COLOR}
    assert live.value("kk.laadpaal__energy", "nl")["value"] == "—", "buiten een sessie toont de kaart niets van de vorige"
    assert live.press("kk.laadpaal__run", "button", None) is True and t.sent == [("124", "OpenCharging")]
    t.values.update({"101": "charing", "25": 12.5})
    assert live.value("kk.laadpaal__run", "nl") == {"kind": "button", "name": "Aan het laden", "value": "Stop", "color": cards.CHARGE_COLOR}
    assert live.value("kk.laadpaal__run", "en")["name"] == "Charging"
    assert live.value("kk.laadpaal__energy", "nl")["value"] == "2.50 kWh", "de kWh van deze sessie, niet de meterstand"
    assert live.value("kk.laadpaal__time", "nl")["value"] == "02:00:00"
    assert live.value("kk.laadpaal__price", "nl") == {"kind": "text", "value": "€ 0.50"}, "de HA-entiteit wint van het vaste tarief"
    t.values["101"] = "iets_nieuws"
    assert live.value("kk.laadpaal__run", "nl")["name"] == "iets_nieuws", "een onbekende stand tonen we zoals ze komt"
    assert live.press("kk.laadpaal__run", "button", None) is True and t.sent[-1] == ("124", "CloseCharging")
    assert hub.log[-1]["kwh"] == 2.5 and hub.log[-1]["cost"] == 0.5 and hub.log[-1]["name"] == "Dit huis"
    assert live.value("kk.laadpaal__price", "nl")["value"] == "—"
    blank = Model()
    blank.add_card("charger")
    assert cards.Live(blank, charge=hub).value("kk.laadpaal__run", "nl")["name"] == "Geen laadpaal gekozen"
    assert cards.Live(m, charge=None).value("kk.laadpaal__run", "nl")["name"] == "Laadpaal offline"
    assert cards.Live(m, charge=hub).value("kk.weg__run", "nl") is None
    with pytest.raises(ModelError, match="ongeldige laadpaal"):
        m.set_card("laadpaal", charger="y:lader")


def test_the_status_decides_start_from_two_lists(tmp_path):
    t = FakeTuya({"101": "wait", "124": "CloseCharging"})
    m, hub = charger(tmp_path, t, start_when="connect, Finish", stop_when=["charing", "wait"])
    cid = next(iter(hub.chargers))
    assert hub.chargers[cid]["start_when"] == ["connect", "Finish"] and hub.chargers[cid]["stop_when"] == ["charing", "wait"]
    live = cards.Live(m, tuya=t, charge=hub)
    assert live.value("kk.laadpaal__run", "nl") == {"kind": "text", "name": "Wacht", "value": "", "color": cards.DEFAULT_COLOR}, "in de stoplijst en geen sessie: niets te starten"
    assert live.press("kk.laadpaal__run", "button", None) is False and t.sent == []
    t.values["101"] = "finish"  # hoofdletters tellen niet
    assert live.value("kk.laadpaal__run", "nl") == {"kind": "button", "name": "Klaar", "value": "Start", "color": cards.DEFAULT_COLOR}
    assert live.press("kk.laadpaal__run", "button", None) is True and t.sent == [("124", "OpenCharging")]
    t.values["101"] = "wait"
    assert live.value("kk.laadpaal__run", "nl")["value"] == "Stop", "in een sessie kun je altijd stoppen"
    hub.stop(cid, hub.who_self())
    t.values = {"124": "CloseCharging"}
    assert live.value("kk.laadpaal__run", "nl")["kind"] == "text", "status niet te lezen: niets"
    hub.update(cid, {"inputs": {"status": ""}})
    t.values = {"124": "CloseCharging"}
    assert live.value("kk.laadpaal__run", "nl")["value"] == "Start", "zonder status-ingang: starten zolang hij niet laadt"
    with pytest.raises(ModelError, match="starten én stoppen"):
        hub.update(cid, {"start_when": ["connect"], "stop_when": ["Connect"]})
    with pytest.raises(ModelError, match="40 tekens"):
        hub.update(cid, {"stop_when": ["x" * 41]})


def test_a_lamp_on_the_network_is_read_and_switched_through_the_engine():
    m = Model()
    m.set_light(5, 1, name="Plafond")
    m.add_card("grid", "Licht")
    m.set_card("licht", items=[{"ref": "l5.1"}])
    calls = []
    eng = SimpleNamespace(light_state=lambda n, r: True, command_light=lambda n, r, on: calls.append(("set", n, r, on)) or True,
                          toggle_light=lambda n, r: calls.append(("toggle", n, r)) or True)
    live = cards.Live(m, engine=eng)
    assert live.value("kk.licht__0", "nl")["value"] == 1
    assert live.press("kk.licht__0", "OnOff", "0") and live.press("kk.licht__0", "button", None)
    assert calls == [("set", 5, 1, False), ("toggle", 5, 1)]
    m.add_panel("P")
    m.set_panel("p", cards=["licht"])
    assert cards.config(m, "p")["cards"][0]["entities"][0] == {"entity": "kk.licht__0", "name": "Plafond", "icon": "mdi:lightbulb"}


def test_a_dynamic_row_on_the_screen_asks_the_env_and_a_press_goes_back_to_it(tmp_path):
    t = FakeTuya({"101": "connect", "25": 1.0, "124": "CloseCharging"})
    m, hub = charger(tmp_path, t)
    m.add_panel("P")
    m.set_panel("p", cards=["laadpaal"])
    env, out = FakeEnv(), []
    live = cards.Live(m, tuya=t, charge=hub)
    env.dynamic, env.press = live.value, lambda e, k, v: live.press(e, k, v)
    p = Panel(cards.config(m, "p"), env, out.append)
    assert p.dynamic and "kk.laadpaal__run" not in p.entities(), "niet aan HA vragen"
    p.reset()
    p.on_screen("event,startup,59,eu")
    p.on_screen("event,buttonPress2,screensaver,bExit,2")
    page = out[-1]
    assert page.startswith("entityUpd~Laadpaal~") and "~button~kk.laadpaal__run~~17299~Aangesloten~Start" in page
    p.on_screen("event,buttonPress2,kk.laadpaal__run,button")
    assert t.sent == [("124", "OpenCharging")]


class FakeAirco:
    """Zoals GreeHub voor de kaart: devices, state(), is_online() en set()."""

    def __init__(self, power=True, mode="cool", target=22, room=23.5, online=True):
        self.devices = {"living": {"name": "Living"}}
        self.s = {"power": power, "mode": mode, "target": target, "room": room}
        self.online, self.sent = online, []

    def state(self, did):
        return dict(self.s)

    def is_online(self, did):
        return self.online

    def set(self, did, field, value):
        if not self.online:
            raise ModelError("niet verstuurd: geen verbinding met de airco")
        self.sent.append((did, field, value))


def airco_screen(airco, **kw):
    m = Model()
    cid = m.add_card("airco")
    m.set_card(cid, airco="living", **kw)
    m.add_panel("P")
    m.set_panel("p", cards=[cid])
    env, out = FakeEnv(), []
    live = cards.Live(m, airco=airco)
    env.dynamic, env.press = live.value, lambda e, k, v: live.press(e, k, v)
    p = Panel(cards.config(m, "p"), env, out.append)
    p.reset()
    p.on_screen("event,startup,59,eu")
    p.on_screen("event,buttonPress2,screensaver,bExit,2")
    return m, p, out


def fields(page: str) -> dict:
    """De velden van een thermostaatpagina: entityUpd~titel~(2 x 6 voor de navigatie)~entiteit~huidig~doel~status~min~max~stap~(8 x 4 standknoppen)~woorden~icoon~doel2~detail."""
    f = page.split("~")
    cells = f[21:53]
    return {"title": f[1], "entity": f[14], "now": f[15], "target": f[16], "status": f[17], "range": f[18:21], "n": len(f),
            "modes": [(cells[i + 3], cells[i + 2], cells[i]) for i in range(0, 32, 4)], "words": f[53:57], "tail": f[57:]}


def test_the_airco_card_is_a_thermostat_page_in_the_format_of_nspanel_lovelace_ui():
    _, p, out = airco_screen(FakeAirco())
    assert "pageType~cardThermo" in out
    f = fields(out[-1])
    assert f["n"] == 59 and f["title"] == "Airco" and f["entity"] == "kk.airco__thermo"
    assert (f["now"], f["target"], f["status"], f["range"]) == ("23.5 °C", "220", "Koelen", ["160", "300", "10"])  # temperaturen ×10 naar het scherm
    # zes standen: meer dan vier staan links, daarna twee lege plekken; alleen de huidige is actief
    assert [(m, a) for m, a, _ in f["modes"]] == [("off", "0"), ("auto", "0"), ("cool", "1"), ("dry", "0"), ("fan_only", "0"), ("heat", "0"), ("", ""), ("", "")]
    assert all(icon for _, _, icon in f["modes"][:6]) and not any(icon for _, _, icon in f["modes"][6:])
    assert f["words"] == ["Momenteel", "Status", "Modus", ""] and f["tail"] == ["", "1"]
    assert p.dynamic and "kk.airco__thermo" not in p.entities()


def test_a_changed_temperature_and_mode_go_to_the_airco_and_the_page_follows_its_answer():
    a = FakeAirco()
    _, p, out = airco_screen(a)
    p.on_screen("event,buttonPress2,kk.airco__thermo,tempUpd,245")  # 24,5: de airco kent hele graden
    p.on_screen("event,buttonPress2,kk.airco__thermo,hvac_action,heat")
    p.on_screen("event,buttonPress2,kk.airco__thermo,hvac_action,off")
    assert a.sent == [("living", "target", "24"), ("living", "mode", "heat"), ("living", "mode", "off")]
    a.s.update(power=False)  # de airco antwoordt: uit
    p.refresh()
    f = fields(out[-1])
    assert f["status"] == "Uit" and [m for m, active, _ in f["modes"] if active == "1"] == ["off"]


def test_an_airco_that_is_offline_says_so_and_a_missing_one_is_not_found():
    a = FakeAirco(online=False)
    _, p, out = airco_screen(a)
    f = fields(out[-1])
    assert f["status"] == "Offline" and not [m for m, active, _ in f["modes"] if active == "1"]
    p.on_screen("event,buttonPress2,kk.airco__thermo,tempUpd,200")
    assert a.sent == [], "geen verbinding: niets gestuurd, geen fout"
    a2 = FakeAirco()
    a2.devices = {}  # de airco is verwijderd
    _, _, out = airco_screen(a2)
    assert out[-1].startswith("entityUpd~Niet gevonden~") and "controleer de config" in out[-1]
    assert out[-1].count("~") == 58, "even lang als een gewone pagina: het scherm leest vaste velden"


def test_an_airco_card_needs_a_valid_airco_id_and_is_not_for_a_screen():
    m = Model()
    cid = m.add_card("airco")
    assert m.cards[cid]["airco"] == "" and "airco" not in cards.SCREEN_TYPES
    with pytest.raises(ModelError):
        m.set_card(cid, airco="../x")
    m.set_card(cid, airco="living")
    assert m.cards[cid]["airco"] == "living"
