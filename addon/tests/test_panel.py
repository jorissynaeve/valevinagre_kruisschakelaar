"""Het NSPanel-scherm: wat de TFT te zien krijgt voor de config van het huis, en wat een druk doet."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from kkaddon.panel import Panel, PanelError
from kkaddon.panel_icons import ICONS as I

YAML = (Path(__file__).parent / "data" / "nspanel1.yaml").read_text(encoding="utf-8")
LISBON = ZoneInfo("Europe/Lisbon")
KITCHEN, EAT = "switch.dingtian_relay81038_r1_kitchen", "switch.dingtian_relay81038_r3_eat_room"


class FakeEnv:
    def __init__(self):
        self.states = {
            "weather.forecast_thuis": ("partlycloudy", {"temperature": 18, "temperature_unit": "°C", "friendly_name": "Thuis"}),
            "sensor.weather_forecast_daily": ("sunny", {"temperature_unit": "°C", "forecast": [
                {"datetime": "2026-10-31T12:00:00+00:00", "condition": "rainy", "temperature": 17},
                {"datetime": "2026-11-01T12:00:00+00:00", "condition": "sunny", "temperature": 19},
                {"datetime": "2026-11-02T12:00:00+00:00", "condition": "cloudy", "temperature": 16},
                {"datetime": "2026-11-03T12:00:00+00:00", "condition": "pouring", "temperature": 14}]}),
            "sensor.tasmota_analog_temperature1": ("24.74", {"device_class": "temperature", "unit_of_measurement": "°C", "friendly_name": "Temp"}),
            "sensor.power_usage": ("0.35", {"device_class": "power", "unit_of_measurement": "kW", "friendly_name": "Power"}),
            "sensor.current_energy_usage": ("1.234", {"device_class": "energy", "unit_of_measurement": "kWh"}),
            KITCHEN: ("on", {"friendly_name": "R1"}),
            EAT: ("off", {}),
            "switch.vvrelaisairco_vvrelaisairco_switch": ("off", {}),
        }
        self.templates: dict[str, object] = {}
        self.calls: list[tuple] = []
        self.when = datetime(2026, 10, 31, 23, 25, tzinfo=LISBON)  # een zaterdag

    def state(self, e):
        return self.states.get(e)

    def template(self, text):
        return self.templates.get(text)

    def forecast(self, e):
        return []

    def now(self):
        return self.when

    def call(self, domain, service, data):
        self.calls.append((domain, service, data))


@pytest.fixture
def rig():
    env, out = FakeEnv(), []
    p = Panel(YAML, env, out.append)
    env.templates[next(iter(p.templates()))] = [255, 165, 0]
    return p, env, out


def started(p, out):
    p.reset()
    p.on_screen("event,startup,59,eu")
    out.clear()


def wake(p, out):
    started(p, out)
    p.on_screen("event,buttonPress2,screensaver,bExit,1")
    out.clear()


def test_the_apps_yaml_of_the_house_is_read_as_it_is(rig):
    p, _, _ = rig
    assert [c.title for c in p.cards if not c.hidden] == ["Light", "Guest Wifi", "Power usage", "Airco"]
    assert [c.key for c in p.cards if c.hidden] == ["subMain", "subQPhil", "subQVis", "subOut", "subOth"]
    assert {KITCHEN, "weather.forecast_thuis", "sensor.power_usage", "sensor.weather_forecast_daily"} <= p.entities()
    assert not {e for e in p.entities() if e.startswith(("delete", "iText", "navigate"))}
    assert len(p.templates()) == 1


def test_startup_sends_the_clock_then_the_screensaver_with_weather_and_temperature(rig):
    p, _, out = rig
    p.reset()
    p.on_screen("event,startup,59,eu")
    assert out == [
        "pageType~pageStartup",
        "time~23:25~",
        "date~Saturday, October 31, 2026",
        "dimmode~20~100~6371~~0",
        "pageType~screensaver",
        "timeout~20",
        "weatherUpdate"
        f"~~~{I['weather-partly-cloudy']}~38066~Thuis~18°C"
        f"~~~{I['weather-rainy']}~25375~Sat~17°C"
        f"~~~{I['weather-sunny']}~65504~Sun~19°C"
        f"~~~{I['weather-cloudy']}~31728~Mon~16°C"
        f"~~~{I['weather-pouring']}~12703~Tue~14°C"
        f"~~~{I['home-thermometer-outline']}~17299~Temp~24.7°C",
        "statusUpdate~~~~~~",
    ]


def test_dutch_locale_gives_dutch_days_and_date():
    env, out = FakeEnv(), []
    p = Panel(YAML.replace("    model: eu", "    model: eu\n    locale: nl_NL"), env, out.append)
    p.reset()
    p.on_screen("event,startup,59,eu")
    assert "date~zaterdag 31 oktober 2026" in out
    assert "~za~17°C" in out[-2]


def test_the_grid_card_has_arrows_round_the_deck_and_buttons_to_the_hidden_pages(rig):
    p, _, out = rig
    started(p, out)
    p.on_screen("event,buttonPress2,screensaver,bExit,1")
    nav = lambda uid, arrow, title: f"button~navigate.{uid}~{I[arrow]}~65535~{title}~Press"
    sub = lambda key, title: f"~button~navigate.{key}~{I['gesture-tap-button']}~17299~{title}~Press"
    assert out == [
        "pageType~cardGrid",
        "timeout~20",
        f"entityUpd~Light~{nav('uuid.c3', 'arrow-left-bold', 'Airco')}~{nav('uuid.c1', 'arrow-right-bold', 'Guest Wifi')}"
        + sub("subMain", "Main room") + sub("subQPhil", "Quarto Philippe") + sub("subQVis", "Quarto Das Visitas")
        + sub("subOut", "Outside") + sub("subOth", "Other"),
    ]


def test_a_hidden_card_has_an_up_button_and_shows_the_switches_and_up_goes_back(rig):
    p, _, out = rig
    wake(p, out)
    p.on_screen("event,buttonPress2,navigate.subMain,button,")
    assert out == [
        "pageType~cardGrid",
        "timeout~20",
        f"entityUpd~Main room~x~navUp~{I['arrow-up-bold']}~65535~~~delete~~~~~"
        f"~switch~{KITCHEN}~{I['light-switch']}~65222~Kitchen~1"
        f"~switch~{EAT}~{I['light-switch']}~17299~Eating table~0",
    ]
    out.clear()
    p.on_screen("event,buttonPress2,navUp,button,")
    assert out[0] == "pageType~cardGrid" and out[2].startswith("entityUpd~Light~")


def test_pressing_a_switch_asks_ha_and_the_new_state_refreshes_the_page_once(rig):
    p, env, out = rig
    wake(p, out)
    p.on_screen("event,buttonPress2,navigate.subMain,button,")
    out.clear()
    p.on_screen(f"event,buttonPress2,{EAT},OnOff,1")
    assert env.calls == [("homeassistant", "turn_on", {"entity_id": EAT})]
    assert out == [], "het scherm wacht op HA"
    env.states[EAT] = ("on", {})
    p.refresh()
    assert len(out) == 1 and "~Eating table~1" in out[0] and not out[0].startswith("pageType")
    p.refresh()
    assert len(out) == 1, "een ongewijzigde pagina wordt niet opnieuw gestuurd"


def test_a_tap_on_a_button_tile_toggles_and_scenes_run(rig):
    p, env, out = rig
    wake(p, out)
    p.on_screen(f"event,buttonPress2,{KITCHEN},button,")
    p.on_screen("event,buttonPress2,scene.film,button,")
    p.on_screen("event,buttonPress2,script.alles_uit,button,")
    assert env.calls == [("switch", "toggle", {"entity_id": KITCHEN}), ("scene", "turn_on", {"entity_id": "scene.film"}),
                         ("script", "turn_on", {"entity_id": "script.alles_uit"})]


def test_the_qr_card_has_the_code_and_the_texts(rig):
    p, _, out = rig
    wake(p, out)
    p.on_screen("event,buttonPress2,navigate.uuid.c1,button,")
    assert out[0] == "pageType~cardQR"
    assert out[2] == (
        f"entityUpd~Guest Wifi~button~navigate.uuid.c0~{I['arrow-left-bold']}~65535~Light~Press"
        f"~button~navigate.uuid.c2~{I['arrow-right-bold']}~65535~Power usage~Press"
        "~WIFI:S:ValeVinagreGuest;T:WPA;P:Vale2020;;"
        f"~text~iText.ValeVinagreGuest~{I['wifi']}~17299~SSID~ValeVinagreGuest"
        f"~text~iText.Vale2020~{I['key']}~17299~Pass~Vale2020"
    )


def test_the_power_card_takes_its_colour_from_the_template_and_has_a_speed_per_row(rig):
    p, env, out = rig
    wake(p, out)
    p.on_screen("event,buttonPress2,navigate.uuid.c2,button,")
    assert out[0] == "pageType~cardPower"
    assert out[2].endswith(
        f"~text~sensor.power_usage~{I['home']}~64800~power~0.3 kW~0"
        "~delete~~~~~~0"
        f"~text~sensor.current_energy_usage~{I['battery']}~17299~usage~1.2 kWh~-20"
    )
    out.clear()
    env.templates.clear()  # HA heeft nog niet geantwoord: de standaardkleur, geen fout
    p.refresh()
    assert f"~text~sensor.power_usage~{I['home']}~17299~power" in out[0]


def test_sleeping_goes_to_the_screensaver_and_a_tap_returns_to_the_same_page(rig):
    p, _, out = rig
    wake(p, out)
    p.on_screen("event,buttonPress2,navigate.subOut,button,")
    out.clear()
    p.on_screen("event,sleepReached,cardGrid")
    assert out[0] == "pageType~screensaver" and out[2].startswith("weatherUpdate")
    out.clear()
    p.on_screen("event,buttonPress2,screensaver,bExit,1")
    assert out[0] == "pageType~cardGrid" and out[2].startswith("entityUpd~Outside~")


def test_changes_while_sleeping_update_the_screensaver_and_the_clock_only_when_the_minute_changes(rig):
    p, env, out = rig
    started(p, out)
    p.tick()
    assert out == []
    env.when = datetime(2026, 11, 1, 0, 0, tzinfo=LISBON)
    p.tick()
    assert out == ["time~00:00~", "date~Sunday, November 1, 2026"]
    out.clear()
    env.states["sensor.tasmota_analog_temperature1"] = ("25.10", {"device_class": "temperature", "unit_of_measurement": "°C", "friendly_name": "Temp"})
    p.refresh()
    assert len(out) == 2 and out[0].startswith("weatherUpdate") and out[0].endswith("~Temp~25.1°C") and out[1].startswith("statusUpdate")
    out.clear()
    p.refresh()
    assert out == [], "niets veranderd: niets sturen"


def test_nothing_is_sent_before_the_panel_has_said_it_started(rig):
    p, env, out = rig
    p.reset()
    out.clear()
    p.refresh()
    p.tick()
    assert out == []


def test_a_row_can_be_hidden_by_a_condition_and_a_missing_entity_is_shown_as_such():
    env, out = FakeEnv(), []
    p = Panel("cards:\n  - type: cardEntities\n    title: T\n    entities:\n"
              f"      - {{entity: {KITCHEN}, state: 'off'}}\n      - {{entity: {EAT}, state_not: 'on'}}\n      - {{entity: switch.weg}}\n", env, out.append)
    p.reset()
    p.on_screen("event,startup,59,eu")
    p.on_screen("event,buttonPress2,screensaver,bExit,1")
    page = out[-1]
    assert KITCHEN not in page and EAT in page
    assert f"~text~switch.weg~{I['alert-circle-outline']}~17299~Not found~check config" in page


def test_more_than_six_tiles_switches_to_the_wide_grid():
    env, out = FakeEnv(), []
    rows = "".join(f"      - {{entity: {EAT}}}\n" for _ in range(7))
    p = Panel(f"cards:\n  - type: cardGrid\n    title: T\n    entities:\n{rows}", env, out.append)
    p.reset()
    p.on_screen("event,startup,59,eu")
    p.on_screen("event,buttonPress2,screensaver,bExit,1")
    assert "pageType~cardGrid2" in out


def test_a_card_type_that_is_not_supported_is_refused_when_the_config_is_saved():
    with pytest.raises(PanelError, match="cardThermo"):
        Panel("cards:\n  - type: cardThermo\n    title: X\n    entity: climate.x\n", FakeEnv(), print)
    with pytest.raises(PanelError, match="cards"):
        Panel("foo: 1", FakeEnv(), print)
    with pytest.raises(PanelError, match="YAML"):
        Panel("cards: [", FakeEnv(), print)


def test_extra_icons_from_the_data_dir_are_used_and_unknown_names_get_the_question_mark():
    env, out = FakeEnv(), []
    cfg = "cards:\n  - type: cardEntities\n    title: T\n    entities:\n      - {entity: iText.a, name: A, icon: 'mdi:eigen'}\n      - {entity: iText.b, name: B, icon: 'mdi:bestaat-niet'}\n"
    p = Panel(cfg, env, out.append, icons={"eigen": "\ue001"})
    p.reset()
    p.on_screen("event,startup,59,eu")
    p.on_screen("event,buttonPress2,screensaver,bExit,1")
    assert "~text~iText.a~\ue001~17299~A~a" in out[-1]
    assert f"~text~iText.b~{I['alert-circle-outline']}~17299~B~b" in out[-1]
