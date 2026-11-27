"""De tv-pagina met bronnen buiten HA: een eigen weerstation (JSON) en een kalenderlink (ICS)."""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from kkaddon import tvhub
from kkaddon.model import ModelError
from kkaddon.tvhub import TvHub, parse_ics, reading
from tests.rig import Rig
from tests.test_tv import StubHa, make


def station_json(now, **over):
    """De vorm van een echt weerstation: een meting is {time, value}; sommige velden zijn al jaren oud."""
    iso = lambda dt: datetime.fromtimestamp(now - dt, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")  # noqa: E731
    d = {"outHumidity": {"time": iso(60), "value": 83.0}, "barometer_mbar": {"time": iso(100), "value": 1022.4}, "outTemp_C": {"time": iso(30), "value": 1.6},
         "rainTotal": {"time": iso(200), "value": 2536.1}, "windchill_c": {"time": "2020-06-09T11:55:00.000+00:00", "value": 20.9},
         "windSpeed_mps": {"time": "2024-12-31T03:58:40.000+00:00", "value": 1.94}}
    d.update(over)
    return d


async def serve(handlers: dict):
    app = web.Application()
    for path, h in handlers.items():
        app.router.add_get(path, h)
    srv = TestServer(app)
    await srv.start_server()
    return srv


# ------------------------------------------------------------ een meting lezen


def test_reading_takes_value_and_time_and_refuses_old_or_odd_ones():
    now = 1_800_000_000.0
    fresh = datetime.fromtimestamp(now - 120, timezone.utc).isoformat()
    old = datetime.fromtimestamp(now - 7200, timezone.utc).isoformat()
    future = datetime.fromtimestamp(now + 7200, timezone.utc).isoformat()
    obj = {"t": {"time": fresh, "value": 1.6}, "old": {"time": old, "value": 5}, "future": {"time": future, "value": 5}, "bare": 7, "nested": {"a": {"time": fresh, "value": 3}},
           "bad": {"time": "gisteren", "value": 1}, "text": {"time": fresh, "value": "warm"}, "bool": True, "nan": {"time": fresh, "value": float("nan")}}
    assert reading(obj, "t", now, 60) == 1.6 and reading(obj, "bare", now, 60) == 7.0 and reading(obj, "nested.a", now, 60) == 3.0
    for k in ("old", "future", "bad", "text", "bool", "nan", "missing", "nested.b"):
        assert reading(obj, k, now, 60) is None, k
    assert reading(obj, "old", now, 180) == 5.0, "een ruimere grens laat hem toe"


# ------------------------------------------------------------ ICS


ICS = "\r\n".join(["BEGIN:VCALENDAR", "BEGIN:VEVENT", "DTSTART;VALUE=DATE:20261201", "SUMMARY:PMD\\, plastic", "END:VEVENT",
                   "BEGIN:VEVENT", "DTSTART:20261203T233000Z", "SUMMARY:Rest", " afval", "END:VEVENT",
                   "BEGIN:VEVENT", "DTSTART;TZID=Europe/Brussels:20261205T060000", "SUMMARY:GFT", "END:VEVENT",
                   "BEGIN:VEVENT", "DTSTART;VALUE=DATE:20261207", "SUMMARY:Geannuleerd", "STATUS:CANCELLED", "END:VEVENT",
                   "BEGIN:VEVENT", "DTSTART;VALUE=DATE:20261209", "END:VEVENT",
                   "BEGIN:VEVENT", "DTSTART:onzin", "SUMMARY:Kapot", "END:VEVENT", "END:VCALENDAR"])


def test_ics_gives_dates_with_titles():
    got = parse_ics(ICS, ZoneInfo("Europe/Brussels"))
    assert got == [{"start": "2026-12-01", "summary": "PMD, plastic"}, {"start": "2026-12-04", "summary": "Restafval"}, {"start": "2026-12-05", "summary": "GFT"}], got
    assert parse_ics(ICS, None)[1]["start"] == "2026-12-03", "zonder tijdzone blijft de datum van de UTC-tijd"
    assert parse_ics("", None) == [] and parse_ics("geen kalender", None) == []


# ------------------------------------------------------------ configuratie


@pytest.mark.parametrize("bad,word", [({"weather_json": {"url": "ftp://x"}}, "http"), ({"weather_json": {"url": "https://x/a b"}}, "http"), ({"weather_json": {"temp": "a b"}}, "veld"),
                                      ({"weather_json": {"max_age": 1}}, "tussen"), ({"waste": {"ics": "webcal://x/y.ics"}}, "http")])
def test_bad_external_sources_are_refused(tmp_path, bad, word):
    _, hub = make(tmp_path)
    with pytest.raises(ModelError, match=word):
        hub.update(bad)


def test_defaults_match_the_station_fields_and_the_urls_are_kept(tmp_path):
    _, hub = make(tmp_path)
    assert hub.cfg["weather_json"] == {"url": "", "temp": "outTemp_C", "pressure": "barometer_mbar", "humidity": "outHumidity", "rain_total": "rainTotal", "max_age": 60}
    hub.update({"weather_json": {"url": "https://weer.example/wgl/home/current/all"}, "waste": {"ics": "https://afval.example/k.ics"}})
    again = TvHub(tmp_path, Rig(tmp_path).engine, port=0)
    assert again.cfg["weather_json"]["url"].endswith("/current/all") and again.cfg["waste"]["ics"].endswith("k.ics")


# ------------------------------------------------------------ de regenteller


def midnight(r):
    return r.engine.local_now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def test_rain_today_is_the_difference_with_midnight(tmp_path):
    r, hub = make(tmp_path)
    r.wall = time.time()
    m = midnight(r)
    hub.wx["rain_total"] = 2540.0
    assert hub._rain_today() is None, "nog geen enkel punt: onbekend"
    hub.rain.extend([(m - 7200, 2530.0), (m - 60, 2536.0), (m + 3600, 2537.5)])
    assert hub._rain_today() == 4.0, "het laatste punt van voor middernacht is het begin"
    hub.rain.clear()
    hub.rain.extend([(m + 3600, 2537.5), (m + 7200, 2538.0)])
    assert hub._rain_today() == 2.5, "zonder punt van voor middernacht: sinds het eerste punt dat we hebben"
    hub.wx["rain_total"] = 2000.0
    assert hub._rain_today() == 0.0, "een teller die terugviel is geen negatieve regen"
    hub.wx["rain_total"] = None
    assert hub._rain_today() is None


def test_rain_points_are_kept_every_ten_minutes_survive_a_restart_and_a_reset_starts_over(tmp_path):
    r, hub = make(tmp_path)
    t = time.time()
    hub.wx["rain_total"] = 100.0
    hub._rain_sample(t)
    hub.wx["rain_total"] = 100.4
    hub._rain_sample(t + 60)
    assert len(hub.rain) == 1, "niet vaker dan eens per 10 minuten"
    hub._rain_sample(t + 601)
    assert [round(v, 1) for _t, v in hub.rain] == [100.0, 100.4]
    again = TvHub(tmp_path, Rig(tmp_path).engine, port=0)
    assert len(again.rain) == 2 and again.rain[-1][1] == 100.4, "een herstart raakt de neerslag van vandaag niet kwijt"
    hub.wx["rain_total"] = 3.0  # de teller viel terug
    hub._rain_sample(t + 1300)
    assert [v for _t, v in hub.rain] == [3.0]
    hub._rain_sample(t + 1300 + tvhub.RAIN_KEEP + 700)
    assert len(hub.rain) == 1, "oude punten verdwijnen"


# ------------------------------------------------------------ het weerstation ophalen


def test_the_station_feeds_the_weather_tile_and_wins_for_the_temperature(tmp_path):
    async def go():
        now = time.time()

        async def current(_r):
            return web.json_response(station_json(time.time()))
        srv = await serve({"/wgl/home/current/all": current})
        r, hub = make(tmp_path, weather="weather.thuis", weather_json={"url": f"http://127.0.0.1:{srv.port}/wgl/home/current/all"})
        r.wall = now
        hub.ha.put("weather.thuis", "partlycloudy", temperature=9.9, apparent_temperature=6.2, humidity=50)
        await hub._refresh_web()
        w = hub.data()["weather"]
        assert w["temp"] == 1.6 and w["pressure"] == 1022.4 and w["humidity"] == 83.0, "het station gaat voor HA"
        assert w["state"] == "partlycloudy" and w["feels"] == 6.2, "het beeld en het voelt-aan blijven uit HA"
        assert w["rain"] == 0.0, "de eerste meting van de teller: nog geen regen te zien"
        assert hub.view()["station"]["temp"] == 1.6 and "weerstation" not in hub.view()["errors"]
        hub.wx["rain_total"] = 2536.1  # later meet het station meer
        hub.rain.append((time.time() - 3000, 2535.0))
        assert hub.data()["weather"]["rain"] == 1.1
        await srv.close()
    asyncio.run(go())


def test_the_weather_tile_works_with_the_station_alone(tmp_path):
    async def go():
        async def current(_r):
            return web.json_response(station_json(time.time()))
        srv = await serve({"/c": current})
        r, hub = make(tmp_path, weather_json={"url": f"http://127.0.0.1:{srv.port}/c"})
        assert hub.data()["weather"] is None, "nog niets opgehaald"
        await hub._refresh_web()
        w = hub.data()["weather"]
        assert w["state"] == "" and w["temp"] == 1.6 and w["hours"] == [] and w["max"] is None
        await srv.close()
    asyncio.run(go())


def test_a_stale_or_failing_station_is_unknown_not_the_last_value(tmp_path):
    async def go():
        calls = {"n": 0}

        async def current(_r):
            calls["n"] += 1
            if calls["n"] == 1:
                return web.json_response(station_json(time.time()))
            if calls["n"] == 2:
                return web.json_response(station_json(time.time(), outTemp_C={"time": "2024-01-01T00:00:00+00:00", "value": 4.0}))
            if calls["n"] == 3:
                return web.Response(text="kapot", status=200)
            return web.Response(status=503)
        srv = await serve({"/c": current})
        r, hub = make(tmp_path, weather="weather.thuis", weather_json={"url": f"http://127.0.0.1:{srv.port}/c"})
        hub.ha.put("weather.thuis", "cloudy", temperature=9.9)
        await hub._refresh_web()
        assert hub.data()["weather"]["temp"] == 1.6
        hub.wx["at"] = 0
        await hub._refresh_web()
        assert hub.data()["weather"]["temp"] == 9.9, "een oude meting telt niet: de temperatuur valt terug op HA"
        assert hub.data()["weather"]["pressure"] == 1022.4
        hub.wx["at"] = 0
        await hub._refresh_web()
        assert hub.errors["weerstation"] == "geen geldige JSON" and hub.data()["weather"]["pressure"] is None
        hub.wx["at"] = 0
        await hub._refresh_web()
        assert hub.errors["weerstation"] == "HTTP 503" and hub.view()["errors"]["weerstation"] == "HTTP 503" and hub.view()["station"]["ok"] is False
        await srv.close()
    asyncio.run(go())


def test_a_station_that_is_removed_is_forgotten(tmp_path):
    async def go():
        async def current(_r):
            return web.json_response(station_json(time.time()))
        srv = await serve({"/c": current})
        r, hub = make(tmp_path, weather_json={"url": f"http://127.0.0.1:{srv.port}/c"})
        await hub._refresh_web()
        assert hub.data()["weather"] is not None
        hub.update({**hub.cfg, "weather_json": {**hub.cfg["weather_json"], "url": ""}})
        await hub._refresh_web()
        assert hub.data()["weather"] is None and hub.errors == {}
        await srv.close()
    asyncio.run(go())


# ------------------------------------------------------------ de kalenderlink


def test_the_ics_link_fills_the_waste_calendar_next_to_ha(tmp_path):
    async def go():
        async def cal(_r):
            return web.Response(text=ICS, content_type="text/calendar")
        srv = await serve({"/k.ics": cal})
        r, hub = make(tmp_path, waste={"on": True, "calendar": "", "ics": f"http://127.0.0.1:{srv.port}/k.ics", "days": 21})
        today = r.engine.local_now().date()
        ics = ICS.replace("20261201", (today + timedelta(days=1)).strftime("%Y%m%d")).replace("20261205", (today + timedelta(days=6)).strftime("%Y%m%d"))
        hub.update({**hub.cfg, "waste": {**hub.cfg["waste"], "ics": hub.cfg["waste"]["ics"]}})

        async def fresh(_r):
            return web.Response(text=ics)
        await srv.close()
        srv = await serve({"/k.ics": fresh})
        hub.cfg["waste"]["ics"] = f"http://127.0.0.1:{srv.port}/k.ics"
        await hub._refresh_web()
        w = hub.data()["waste"]
        assert w["configured"] is True and w["items"][0] == {"date": (today + timedelta(days=1)).isoformat(), "label": "PMD, plastic", "days": 1}
        assert hub.view()["ics_count"] >= 2 and hub.view()["ics_ok"] is True and "ophaalkalender" not in hub.view()["errors"]
        hub.events["waste"] = [{"start": (today + timedelta(days=3)).isoformat(), "summary": "Papier"}]
        assert [i["label"] for i in hub.data()["waste"]["items"]][:2] == ["PMD, plastic", "Papier"], "HA en ICS samen"
        await srv.close()
    asyncio.run(go())


def test_a_calendar_link_that_stops_answering_keeps_the_old_pickups(tmp_path):
    async def go():
        up = {"ok": True}

        async def cal(_r):
            if up["ok"]:
                return web.Response(text=ICS.replace("20261201", (datetime.now() + timedelta(days=2)).strftime("%Y%m%d")))
            return web.Response(status=500)
        srv = await serve({"/k.ics": cal})
        r, hub = make(tmp_path, waste={"on": True, "calendar": "", "ics": f"http://127.0.0.1:{srv.port}/k.ics", "days": 21})
        r.wall = time.time()
        await hub._refresh_web()
        n = len(hub.ics["events"])
        assert n >= 1
        up["ok"] = False
        hub.ics["at"] = -10 ** 9
        await hub._refresh_web()
        assert len(hub.ics["events"]) == n and hub.ics["ok"] is False and hub.errors["ophaalkalender"] == "HTTP 500", "een kalender die even niet antwoordt, is niet leeg"
        await srv.close()
    asyncio.run(go())


def test_too_large_answers_are_refused(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(tvhub, "MAX_BODY", 1000)

        async def big(_r):
            return web.Response(text="x" * 5000)
        srv = await serve({"/k.ics": big})
        _, hub = make(tmp_path, waste={"on": True, "calendar": "", "ics": f"http://127.0.0.1:{srv.port}/k.ics", "days": 21})
        assert await hub._get(hub.cfg["waste"]["ics"], "ophaalkalender") is None and hub.errors["ophaalkalender"] == "antwoord te groot"
        await srv.close()
    asyncio.run(go())


def test_unreachable_sources_are_reported_not_fatal(tmp_path):
    async def go():
        _, hub = make(tmp_path, weather_json={"url": "http://127.0.0.1:9/nee"}, waste={"on": True, "calendar": "", "ics": "http://127.0.0.1:9/nee.ics", "days": 21})
        await hub._refresh_web()
        assert hub.errors["weerstation"] and hub.errors["ophaalkalender"] and hub.data()["weather"] is None
    asyncio.run(go())


def test_the_loop_fetches_the_web_sources_without_ha(tmp_path):
    async def go():
        async def current(_r):
            return web.json_response(station_json(time.time()))
        srv = await serve({"/c": current})
        r = Rig(tmp_path)
        hub = TvHub(tmp_path, r.engine, port=0)  # geen HA
        hub.update({**hub.cfg, "weather_json": {**hub.cfg["weather_json"], "url": f"http://127.0.0.1:{srv.port}/c"}})
        hub.start()
        for _ in range(100):
            if hub.wx["ok"]:
                break
            await asyncio.sleep(0.05)
        assert hub.wx["temp"] == 1.6
        hub.close()
        await srv.close()
    asyncio.run(go())


def test_the_pressure_icon_exists_in_the_page_and_the_list():
    assert "gauge" in tvhub.ICON_NAMES
