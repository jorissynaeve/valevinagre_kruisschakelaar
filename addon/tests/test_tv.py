"""De tv-pagina: configuratie, het lezen van bronnen (HA en blokken), de samengestelde gegevens, de server met geheim en de web-API."""
import asyncio
import json
import os
import re
import stat
import time
from datetime import timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from kkaddon import habridge, tvhub
from kkaddon.model import ModelError
from kkaddon.tvhub import TvHub, clean, defaults
from tests.rig import KEY, Rig


class StubHa:
    """Wat de hub van HaBridge gebruikt: standen, voorspelling, geschiedenis en kalenders."""

    def __init__(self):
        self.connected = True
        self.states: dict = {}
        self.fc: dict = {}
        self.hist: dict = {}
        self.cal: dict = {}
        self.asked: list = []

    def put(self, ent, state, **attrs):
        self.states[ent] = (str(state), attrs)

    async def forecast(self, ent, kind="daily"):
        self.asked.append(("forecast", ent, kind))
        return self.fc.get(kind, [])

    async def history(self, ent, hours=12.0):
        self.asked.append(("history", ent))
        return self.hist.get(ent, [])

    async def calendar_events(self, ent, days=21):
        self.asked.append(("calendar", ent))
        return self.cal.get(ent, [])


def make(tmp_path, **cfg):
    r = Rig(tmp_path)
    r.engine.set_tz("UTC")
    hub = TvHub(tmp_path, r.engine, port=0)
    hub.ha = StubHa()
    if cfg:
        hub.update({**hub.cfg, **cfg})
    return r, hub


# ------------------------------------------------------------ configuratie


def test_icons_in_the_config_exist_in_the_page():
    js = (Path(tvhub.STATIC) / "display.js").read_text(encoding="utf-8")
    block = js[js.index("const ICONS = {"):js.index("const ic = ")]
    names = set(re.findall(r"^  (\w+): '", block, re.M))
    assert set(tvhub.ICON_NAMES) <= names, set(tvhub.ICON_NAMES) - names


def test_defaults_keep_a_reserved_place_for_the_waste_calendar(tmp_path):
    r, hub = make(tmp_path)
    d = hub.data()
    assert d["waste"] == {"configured": False, "today": r.engine.local_now().date().isoformat(), "items": []}
    assert d["weather"] is None and d["energy"] is None and d["tiles"] == [] and d["climate"] is None and d["chips"] == [] and d["next"] is None
    assert d["status"]["ok"] is True


def test_config_is_saved_private_and_the_token_survives_a_restart(tmp_path):
    r, hub = make(tmp_path, title="Villa", weather="weather.thuis")
    tok = hub.cfg["token"]
    assert len(tok) >= 16 and hub.token_ok(tok) and not hub.token_ok(tok[:-1] + "x") and not hub.token_ok("")
    again = TvHub(tmp_path, Rig(tmp_path).engine, port=0)
    assert again.cfg["title"] == "Villa" and again.cfg["token"] == tok
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "tv.json").stat().st_mode) == 0o600


def test_the_token_cannot_be_set_from_outside_but_can_be_renewed(tmp_path):
    _, hub = make(tmp_path)
    tok = hub.cfg["token"]
    hub.update({"title": "x", "token": "evil" * 8})
    assert hub.cfg["token"] == tok
    new = hub.renew_token()
    assert new != tok and hub.token_ok(new) and not hub.token_ok(tok)


@pytest.mark.parametrize("bad,word", [
    ({"weather": "sensor.x"}, "weather"), ({"weather": "geen entiteit"}, "kies"), ({"photo": "javascript:alert(1)"}, "foto"),
    ({"energy": {"consumption": "x y"}}, "kies"), ({"tiles": [{"title": "a", "rows": [{"source": "sensor.x", "icon": "bestaatniet"}]}]}, "pictogram"),
    ({"tiles": [{"title": "a"}] * 4}, "hoogstens 3"), ({"climate": {"rows": [{"source": "sensor.x"}] * 5}}, "hoogstens 4"),
    ({"chips": [{"source": "sensor.x"}] * 6}, "hoogstens 5"), ({"waste": {"calendar": "sensor.x"}}, "calendar"), ({"next": {"calendar": "sensor.x"}}, "calendar"),
    ({"energy": {"max": "veel"}}, "getal"), ({"energy": {"max": 1000}}, "tussen")])
def test_bad_config_is_refused(tmp_path, bad, word):
    _, hub = make(tmp_path)
    with pytest.raises(ModelError, match=word):
        hub.update(bad)


def test_sources_may_be_block_outputs(tmp_path):
    _, hub = make(tmp_path)
    cfg = clean({"energy": {"solar": "o:deye:pv", "consumption": "sensor.verbruik"}, "chips": [{"source": "l:tv:watch", "label": "TV"}]}, "t" * 20)
    assert cfg["energy"]["solar"] == "o:deye:pv" and cfg["chips"][0]["source"] == "l:tv:watch"
    with pytest.raises(ModelError):
        clean({"energy": {"solar": "o:deye"}}, "t" * 20)


# ------------------------------------------------------------ bronnen lezen


def test_read_ha_numbers_words_and_unknowns(tmp_path):
    _, hub = make(tmp_path)
    hub.ha.put("sensor.t", "20.5", unit_of_measurement="°C", friendly_name="Woonkamer")
    hub.ha.put("switch.pomp", "on")
    hub.ha.put("sensor.weg", "unavailable", unit_of_measurement="W")
    assert hub.read("sensor.t")[:3] == (20.5, "°C", "20.5")
    assert hub.read("switch.pomp")[:3] == (None, "", "on")
    assert hub.read("sensor.weg")[0] is None and hub.read("sensor.weg")[2] is None
    assert hub.read("sensor.bestaatniet")[0] is None and hub.read("")[0] is None


def test_read_block_outputs_with_their_unit(tmp_path):
    r, hub = make(tmp_path)
    r.engine.ref_value = lambda ref: {"o:deye:pv": 3800, "l:tv:watch": True, "w:ac:room": 21}.get(ref)
    assert hub.read("o:deye:pv")[:2] == (3800.0, "W"), "een omvormer geeft W: dat staat in zijn velden"
    assert hub.read("l:tv:watch")[:3] == (None, "", "on")
    assert hub.read("w:ac:room")[0] == 21.0
    assert hub.read("o:weg:pv")[0] is None


# ------------------------------------------------------------ de gegevens


def test_energy_converts_watts_to_kilowatts_and_the_series_too(tmp_path):
    _, hub = make(tmp_path, energy={"consumption": "sensor.w", "solar": "sensor.zon", "battery": "sensor.soc", "battery_kwh": "sensor.kwh", "grid": "", "max": 6})
    hub.ha.put("sensor.w", 1240, unit_of_measurement="W")
    hub.ha.put("sensor.zon", 3.8, unit_of_measurement="kW")
    hub.ha.put("sensor.soc", 68, unit_of_measurement="%")
    hub.ha.put("sensor.kwh", 2.1, unit_of_measurement="kWh")
    now = time.time()
    hub.sample(now)
    hub.sample(now + 61)
    e = hub.data()["energy"]
    assert e["consumption"] == {"value": 1.24, "unit": "kW"} and e["solar"] == {"value": 3.8, "unit": "kW"} and e["max"] == 6
    assert e["battery"] == {"pct": 68.0, "detail": "2,1 kWh"}
    assert [p[1] for p in e["series"]["consumption"]] == [1.24, 1.24] and [p[1] for p in e["series"]["solar"]] == [3.8, 3.8]


def test_energy_is_hidden_until_a_source_is_chosen(tmp_path):
    _, hub = make(tmp_path)
    assert hub.data()["energy"] is None
    hub.update({**hub.cfg, "energy": {**hub.cfg["energy"], "solar": "sensor.zon"}})
    hub.ha.put("sensor.zon", "unavailable")
    e = hub.data()["energy"]
    assert e["solar"]["value"] is None and "consumption" not in e, "onbekend blijft onbekend, geen nul"


def test_samples_keep_one_point_per_minute_and_skip_unknowns(tmp_path):
    _, hub = make(tmp_path, climate={"title": "T", "rows": [], "chart": ["sensor.t"]})
    hub.ha.put("sensor.t", 20)
    t = time.time()
    hub.sample(t)
    hub.sample(t + 10)
    hub.ha.put("sensor.t", "unavailable")
    hub.sample(t + 70)
    hub.ha.put("sensor.t", 21)
    hub.sample(t + 130)
    assert [round(v) for _t, v in hub.hist["sensor.t"]] == [20, 21]


def test_weather_with_forecast_night_and_maximum(tmp_path):
    r, hub = make(tmp_path, weather="weather.thuis")
    hub.ha.put("weather.thuis", "sunny", temperature=8.4, apparent_temperature=6.2)
    now = time.time()
    iso = lambda h: time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + h * 3600))  # noqa: E731
    hub.forecast["hourly"] = [{"datetime": iso(-3), "condition": "rainy", "temperature": 1}, *[{"datetime": iso(i), "condition": "sunny", "temperature": 5 + i} for i in range(1, 8)]]
    hub.forecast["daily"] = [{"temperature": 10.2, "templow": 4.1}]
    w = hub.data()["weather"]
    assert w["state"] in ("sunny", "clear-night") and w["temp"] == 8.4 and w["feels"] == 6.2 and w["max"] == 10.2 and w["min"] == 4.1
    assert len(w["hours"]) == 5 and w["hours"][0]["temp"] == 6 and all(h["t"].endswith("u") for h in w["hours"]), "het verleden valt weg, vijf uur vooruit"
    hub.ha.states.pop("weather.thuis")
    assert hub.data()["weather"] is None


def test_clear_sky_at_night_is_shown_as_a_moon(tmp_path):
    r, hub = make(tmp_path, weather="weather.thuis")
    hub.ha.put("weather.thuis", "sunny", temperature=5)
    r.wall = 1_700_000_000 + 0  # 22:13 UTC, de tijdzone staat op UTC
    assert hub.data()["weather"]["state"] == "clear-night"
    r.wall = 1_700_000_000 - 8 * 3600  # 14:13
    assert hub.data()["weather"]["state"] == "sunny"


def test_tiles_and_climate_rows_show_numbers_or_words(tmp_path):
    _, hub = make(tmp_path, tiles=[{"title": "Zwembad", "icon": "waves", "tag": "sensor.water", "chart": "sensor.water",
                                    "rows": [{"label": "pH", "source": "sensor.ph", "icon": "drop"}, {"label": "Pomp", "source": "switch.pomp", "icon": "pump"},
                                             {"label": "Dekking", "source": "cover.dek", "on_text": "Open", "off_text": "Dicht"}, {"label": "Chloor", "source": "sensor.weg"}]}],
                  climate={"title": "Klimaat", "rows": [{"label": "Woonkamer", "source": "sensor.t", "unit": "°C"}, {"label": "Ventilatie", "source": "fan.v", "icon": "fan"}], "chart": []})
    hub.ha.put("sensor.water", 24.6, unit_of_measurement="°C")
    hub.ha.put("sensor.ph", 7.2)
    hub.ha.put("switch.pomp", "on")
    hub.ha.put("cover.dek", "closed")
    hub.ha.put("sensor.t", 20.5, unit_of_measurement="°C")
    hub.ha.put("fan.v", "off")
    d = hub.data()
    t = d["tiles"][0]
    assert t["title"] == "Zwembad" and t["tag"] == "24,6°C"
    assert t["rows"][0]["value"] == 7.2 and t["rows"][1] == {"label": "Pomp", "icon": "pump", "text": "Aan", "tone": "on"}
    assert t["rows"][2]["text"] == "Dicht" and t["rows"][3]["text"] == "–", "een onbekende bron toont een streepje"
    assert d["climate"]["rows"][0]["value"] == 20.5 and d["climate"]["rows"][1]["text"] == "Uit"


def test_a_tile_without_rows_is_left_out(tmp_path):
    _, hub = make(tmp_path, tiles=[{"title": "Leeg", "rows": []}])
    assert hub.data()["tiles"] == []


def test_chips_alerts_and_the_connection_status(tmp_path):
    _, hub = make(tmp_path, alerts=["binary_sensor.rook", "binary_sensor.water"],
                  chips=[{"label": "Eco", "source": "input_boolean.eco", "icon": "leaf", "on_text": "Actief", "off_text": "Uit"}, {"label": "Alarm", "source": "alarm_control_panel.a", "icon": "lock"},
                         {"label": "Weg", "source": "sensor.weg"}])
    hub.ha.put("input_boolean.eco", "on")
    hub.ha.put("alarm_control_panel.a", "disarmed")
    hub.ha.put("binary_sensor.rook", "off", friendly_name="Rookmelder")
    hub.ha.put("binary_sensor.water", "on", friendly_name="Waterlek kelder")
    d = hub.data()
    assert [c["text"] for c in d["chips"]] == ["Actief", "Uit", "Onbekend"] and d["chips"][0]["tone"] == "on" and d["chips"][2]["tone"] == "warn"
    assert d["status"] == {"ok": False, "alerts": ["Waterlek kelder"], "text": "1 melding"}
    hub.ha.put("binary_sensor.rook", "on", friendly_name="Rookmelder")
    assert hub.data()["status"]["text"] == "2 meldingen"
    hub.ha.connected = False
    assert hub.data()["status"]["text"] == "Geen verbinding met Home Assistant" and hub.data()["status"]["ok"] is False


def test_waste_calendar_lists_the_next_pickups_without_duplicates(tmp_path):
    r, hub = make(tmp_path, waste={"on": True, "calendar": "calendar.afval", "days": 21})
    today = r.engine.local_now().date()
    d = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731
    hub.events["waste"] = [{"start": d(4), "summary": "Restafval"}, {"start": d(1), "summary": "PMD"}, {"start": d(1), "summary": "PMD"}, {"start": d(-2), "summary": "Gisteren"},
                           {"start": d(9) + "T07:00:00+00:00", "summary": "GFT"}, {"start": "kapot", "summary": "x"}, {"start": d(2), "summary": " "}]
    w = hub.data()["waste"]
    assert w["configured"] is True and [(i["label"], i["days"]) for i in w["items"]] == [("PMD", 1), ("Restafval", 4), ("GFT", 9)]


def test_waste_can_be_switched_off_and_next_appointment_is_the_first_to_come(tmp_path):
    r, hub = make(tmp_path, waste={"on": False, "calendar": "", "days": 21}, next={"label": "Straks:", "calendar": "calendar.gezin"})
    assert hub.data()["waste"] is None
    now = r.engine.local_now()
    hub.events["next"] = [{"start": (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S+00:00"), "summary": "Later"},
                          {"start": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00"), "summary": "Zwembadpomp"},
                          {"start": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S+00:00"), "summary": "Voorbij"}]
    n = hub.data()["next"]
    assert n["label"] == "Straks:" and n["text"] == f"Zwembadpomp {(now + timedelta(hours=1)):%H:%M}"


# ------------------------------------------------------------ verversen en geschiedenis


def test_refresh_fetches_forecast_and_calendars_and_backfills_history(tmp_path):
    async def go():
        r, hub = make(tmp_path, weather="weather.thuis", waste={"on": True, "calendar": "calendar.afval", "days": 14},
                      climate={"title": "K", "rows": [], "chart": ["sensor.t", "o:deye:pv"]})
        now = time.time()
        hub.ha.fc = {"hourly": [{"datetime": "x"}], "daily": [{"temperature": 9}]}
        hub.ha.cal = {"calendar.afval": [{"start": "2030-01-01", "summary": "PMD"}]}
        hub.ha.hist = {"sensor.t": [(now - 3000, 19.0), (now - 1200, 20.0)]}
        hub.ha.put("sensor.t", 21)
        hub._poke = asyncio.Event()
        done: set = set()
        await hub._backfill(done)
        await hub._refresh()
        assert [v for _t, v in hub.hist["sensor.t"]] == [19.0, 20.0] and done == {"sensor.t"}, "een blok-uitgang heeft geen geschiedenis in HA"
        assert hub.forecast["hourly"] == [{"datetime": "x"}] and hub.events["waste"][0]["summary"] == "PMD"
        n = len(hub.ha.asked)
        await hub._refresh()
        assert len(hub.ha.asked) == n, "binnen de wachttijd niet opnieuw vragen"
        hub.update({**hub.cfg, "weather": "weather.ander"})
        await hub._refresh()
        assert ("forecast", "weather.ander", "hourly") in hub.ha.asked, "een andere entiteit wordt meteen opnieuw opgehaald"
    asyncio.run(go())


def test_the_loop_survives_a_failing_round(tmp_path):
    async def go():
        r, hub = make(tmp_path, climate={"title": "K", "rows": [], "chart": ["sensor.t"]})

        async def boom(*_a):
            raise RuntimeError("HA stuk")
        hub._backfill = boom
        hub.ha.put("sensor.t", 20)
        hub.start()
        await asyncio.sleep(0.3)
        assert hub.hist["sensor.t"], "het meetpunt kwam er toch"
        assert not hub.tasks[0].done(), "een fout in een ronde legt de lus niet stil"
        hub.close()
    asyncio.run(go())


def test_parse_history_averages_per_bucket_and_skips_non_numbers():
    rows = [{"s": "20", "lu": 1000.0}, {"s": "22", "lu": 1100.0}, {"s": "unavailable", "lu": 1200.0}, {"s": "x", "lu": 1300.0}, {"state": "30", "last_updated": "1970-01-01T00:30:00+00:00"}, "kapot"]
    assert habridge.parse_history(rows) == [(900.0, 21.0), (2100.0, 30.0)]
    assert habridge.parse_history(None) == [] and habridge.parse_history([{"s": "nan", "lu": 5}]) == []


# ------------------------------------------------------------ de server en de web-API


def test_the_page_is_served_only_with_the_secret_in_the_address(tmp_path):
    async def go():
        _, hub = make(tmp_path)
        app = web.Application()
        tvhub.routes(app, hub, "/tv/{token}", check=True)
        c = TestClient(TestServer(app))
        await c.start_server()
        try:
            tok = hub.cfg["token"]
            for path in (f"/tv/{tok[:-1]}x/", f"/tv/{tok[:-1]}x/data", "/tv/geheim/display.js", "/tv//"):
                assert (await c.get(path)).status == 404, path
            page = await c.get(f"/tv/{tok}/")
            assert page.status == 200 and "display.js" in await page.text() and page.headers["Cache-Control"] == "no-store"
            js = await c.get(f"/tv/{tok}/display.js")
            assert js.status == 200 and "javascript" in js.headers["Content-Type"] and (await js.text()).startswith("'use strict'")
            assert "text/css" in (await c.get(f"/tv/{tok}/display.css")).headers["Content-Type"]
            data = await (await c.get(f"/tv/{tok}/data")).json()
            assert data["title"] == "Mijn huis" and data["waste"]["configured"] is False
            assert (await c.get(f"/tv/{tok}/andere.js")).status == 404, "alleen de eigen bestanden"
            assert (await c.get(f"/tv/{tok}/../../etc/passwd")).status == 404
        finally:
            await c.close()
    asyncio.run(go())


def test_the_data_holds_only_what_was_chosen(tmp_path):
    _, hub = make(tmp_path, energy={"consumption": "sensor.w", "solar": "", "battery": "", "battery_kwh": "", "grid": "", "max": 5})
    hub.ha.put("sensor.w", 100, unit_of_measurement="W", friendly_name="Geheime teller")
    hub.ha.put("sensor.prive", 1, friendly_name="Privé")
    text = json.dumps(hub.data())
    assert "Privé" not in text and "sensor.prive" not in text and hub.cfg["token"] not in text and "Geheime teller" not in text


def test_the_listener_starts_and_serves_on_its_port(tmp_path):
    import socket
    async def go():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        r = Rig(tmp_path)
        hub = TvHub(tmp_path, r.engine, port=port)
        hub.start()
        for _ in range(100):
            if hub.listening:
                break
            await asyncio.sleep(0.05)
        import aiohttp
        async with aiohttp.ClientSession() as http:
            ok = await http.get(f"http://127.0.0.1:{port}/tv/{hub.cfg['token']}/data")
            assert ok.status == 200 and (await ok.json())["title"] == "Mijn huis"
            assert (await http.get(f"http://127.0.0.1:{port}/tv/nee/data")).status == 404
            assert (await http.get(f"http://127.0.0.1:{port}/api/tv")).status == 404, "alleen de tv-pagina, geen API"
            redirected = await http.get(f"http://127.0.0.1:{port}/tv/{hub.cfg['token']}")
            assert redirected.status == 200 and str(redirected.url).endswith("/")
        hub.close()
    asyncio.run(go())


def test_a_taken_port_is_logged_not_fatal(tmp_path):
    import socket
    async def go():
        s = socket.socket()
        s.bind(("0.0.0.0", 0))
        s.listen(1)
        hub = TvHub(tmp_path, Rig(tmp_path).engine, port=s.getsockname()[1])
        hub.start()
        await asyncio.sleep(0.3)
        assert hub.listening == 0
        hub.close()
        s.close()
    asyncio.run(go())


def test_web_api_reads_saves_renews_and_previews(tmp_path):
    from kkaddon import web as kweb
    from kkaddon.settings import Settings

    async def go():
        r = Rig(tmp_path)
        r.engine.set_tz("UTC")
        hub = TvHub(tmp_path, r.engine, port=0)
        hub.ha = StubHa()
        hub.ha.put("sensor.a", 5, friendly_name="Een", unit_of_measurement="W")
        hub.ha.put("light.zit", "on")
        ctx = kweb.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), r.engine, tv=hub)
        c = TestClient(TestServer(kweb.make_app(ctx)))
        await c.start_server()
        try:
            got = await (await c.get("/api/tv")).json()
            assert got["config"]["token"] == hub.cfg["token"] and got["port"] == 0 and got["ha"] is True
            assert got["host"] == "" or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", got["host"]) and not got["host"].startswith("127."), got["host"]
            ok = await c.put("/api/tv", json={"title": "Villa", "weather": "weather.x", "chips": [{"source": "sensor.a", "label": "A"}]})
            assert ok.status == 200 and hub.cfg["title"] == "Villa" and hub.cfg["chips"][0]["label"] == "A"
            bad = await c.put("/api/tv", json={"weather": "sensor.x"})
            assert bad.status == 400 and "weather" in (await bad.json())["error"]
            src = await (await c.get("/api/tv/sources")).json()
            assert ["sensor.a", "Een", "W", "5"] in src["ha"] and not any(e[0] == "light.zit" for e in src["addon"]), src
            assert any(e[0] == "sensor.a" for e in src["ha"]) and "addon" in src
            old = hub.cfg["token"]
            new = (await (await c.post("/api/tv/token")).json())["token"]
            assert new != old and hub.cfg["token"] == new
            page = await c.get("/tv/view/")
            assert page.status == 200 and "display.js" in await page.text()
            assert (await (await c.get("/tv/view/data")).json())["title"] == "Villa", "de voorvertoning gebruikt geen geheim: ze zit achter ingress"
            assert (await c.get("/tv/view/display.css")).status == 200
        finally:
            await c.close()
    asyncio.run(go())


def test_preview_is_refused_outside_ingress(tmp_path, monkeypatch):
    from kkaddon import web as kweb
    from kkaddon.settings import Settings
    monkeypatch.setattr(kweb, "INGRESS_PROXY", {"172.30.32.2"})  # de testclient komt van 127.0.0.1, dat normaal ook mag

    async def go():
        r = Rig(tmp_path)
        hub = TvHub(tmp_path, r.engine, port=0)
        ctx = kweb.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=False), r.engine, tv=hub)
        c = TestClient(TestServer(kweb.make_app(ctx)))
        await c.start_server()
        try:
            assert (await c.get("/tv/view/data")).status == 403 and (await c.get("/api/tv")).status == 403
        finally:
            await c.close()
    asyncio.run(go())


def test_block_outputs_are_offered_as_sources(tmp_path):
    _, hub = make(tmp_path)
    blocks = [{"name": "Deye", "outs": [{"ref": "o:deye:pv", "label": "PV", "unit": "W", "value": 3800}], "all": [{"ref": "o:deye:pv", "label": "PV", "unit": "W", "value": 3800}, {"ref": "o:deye:soc", "label": "Batterij", "unit": "%", "value": 68}]},
              {"name": "TV", "outs": [{"ref": "l:tv:watch", "label": "wordt bekeken", "value": False}]}]
    assert hub.sources(blocks)["addon"] == [["o:deye:pv", "Deye · PV", "W", "3800"], ["o:deye:soc", "Deye · Batterij", "%", "68"], ["l:tv:watch", "TV · wordt bekeken", "", "False"]]


def test_lan_ip_is_an_address_or_empty_and_never_loopback(monkeypatch):
    ip = tvhub.lan_ip()
    assert ip == "" or (re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip) and not ip.startswith(("127.", "0.")))

    class Broken:
        def __enter__(self):
            raise OSError("geen netwerk")

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(tvhub.socket, "socket", lambda *a, **k: Broken())
    assert tvhub.lan_ip() == "", "zonder netwerk valt de pagina terug op het adres van de browser"
