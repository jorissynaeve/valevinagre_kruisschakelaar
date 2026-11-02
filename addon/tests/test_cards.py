"""Kaarten voor het NSPanel: de omgezette config van het huis geeft hetzelfde scherm als de YAML, en de rijen uit de add-on."""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from kkaddon import cards
from kkaddon.model import Model, ModelError
from kkaddon.panel import Panel

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


def charger(**kw):
    m = Model()
    m.add_card("charger")
    if kw:
        m.set_card("laadpaal", **kw)
    return m


def test_the_charger_rows():
    m = charger(tuya="lader", price=0.3, price_entity="sensor.tarief")
    t = FakeTuya({"101": "charing", "124": "OpenCharging", "25": 10, "120": "02:00:00"})
    live = cards.Live(m, tuya=t, state=lambda e: ("0.2", {}) if e == "sensor.tarief" else None)
    assert live.value("kk.laadpaal__run", "nl") == {"kind": "button", "name": "Aan het laden", "value": "Stop", "color": cards.CHARGE_COLOR}
    assert live.value("kk.laadpaal__price", "nl") == {"kind": "text", "value": "€ 2.00"}, "de HA-entiteit wint van het vaste tarief"
    assert live.value("kk.laadpaal__run", "en")["name"] == "Charging"
    assert live.press("kk.laadpaal__run", "button", None) is True and t.sent == [("124", "CloseCharging")]
    live = cards.Live(m, tuya=t, state=lambda e: ("unknown", {}))
    assert live.value("kk.laadpaal__price", "nl")["value"] == "€ 3.00", "geeft de entiteit geen getal, dan het vaste tarief"
    t.values = {"101": "iets_nieuws", "124": "CloseCharging"}
    assert live.value("kk.laadpaal__run", "nl")["name"] == "iets_nieuws", "een onbekende stand tonen we zoals ze komt"
    assert live.value("kk.laadpaal__energy", "nl")["value"] == "—" and live.value("kk.laadpaal__price", "nl")["value"] == "—"
    assert cards.Live(charger(), tuya=t).value("kk.laadpaal__run", "nl")["name"] == "Start/stop niet aangesloten"
    assert cards.Live(m, tuya=None).value("kk.laadpaal__run", "nl")["name"] == "Lader offline"
    assert cards.Live(m, tuya=t).value("kk.weg__run", "nl") is None


def test_the_status_decides_start_or_stop_from_two_lists():
    m = charger(tuya="lader", start_when="connect, Finish", stop_when=["charing", "wait"])
    assert m.cards["laadpaal"]["start_when"] == ["connect", "Finish"] and m.cards["laadpaal"]["stop_when"] == ["charing", "wait"]
    t = FakeTuya({"101": "wait", "124": "CloseCharging"})
    live = cards.Live(m, tuya=t)
    assert live.value("kk.laadpaal__run", "nl")["value"] == "Stop", "de status wint van het datapunt van start/stop"
    assert live.press("kk.laadpaal__run", "button", None) is True and t.sent == [("124", "CloseCharging")]
    t.values = {"101": "finish", "124": "OpenCharging"}  # hoofdletters tellen niet
    assert live.value("kk.laadpaal__run", "nl") == {"kind": "button", "name": "Klaar", "value": "Start", "color": cards.DEFAULT_COLOR}
    assert live.press("kk.laadpaal__run", "button", None) is True and t.sent[-1] == ("124", "OpenCharging")
    t.values = {"101": "fault", "124": "CloseCharging"}
    assert live.value("kk.laadpaal__run", "nl") == {"kind": "text", "name": "Storing", "value": "", "color": cards.DEFAULT_COLOR}, "in geen lijst: geen knop"
    assert live.press("kk.laadpaal__run", "button", None) is False and len(t.sent) == 2
    t.values = {"124": "CloseCharging"}
    assert live.value("kk.laadpaal__run", "nl")["kind"] == "text", "status niet te lezen: niets"
    m.set_card("laadpaal", inputs={})
    assert live.value("kk.laadpaal__run", "nl")["value"] == "Start", "zonder status-ingang zoals vroeger"
    with pytest.raises(ModelError, match="starten én stoppen"):
        m.set_card("laadpaal", start_when=["connect"], stop_when=["Connect"])
    with pytest.raises(ModelError, match="40 tekens"):
        m.set_card("laadpaal", stop_when=["x" * 41])


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


def test_a_dynamic_row_on_the_screen_asks_the_env_and_a_press_goes_back_to_it():
    m = charger(tuya="lader")
    m.add_panel("P")
    m.set_panel("p", cards=["laadpaal"])
    env, out = FakeEnv(), []
    t = FakeTuya({"101": "connect", "25": 1.0, "124": "CloseCharging"})
    live = cards.Live(m, tuya=t)
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
