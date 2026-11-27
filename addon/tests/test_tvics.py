"""Een geüpload ICS-bestand als ophaalkalender, en de herhalingsregels in een ICS-bestand."""
import asyncio
import time
from datetime import date, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from kkaddon import tvhub
from kkaddon.model import ModelError
from kkaddon.tvhub import TvHub, expand, parse_ics
from tests.rig import KEY, Rig
from tests.test_tv import make

TODAY = date(2026, 11, 27)


def cal(*events: str) -> str:
    return "BEGIN:VCALENDAR\r\nPRODID:-//eluceo/ical//2.0/EN\r\nVERSION:2.0\r\n" + "".join(events) + "END:VCALENDAR\r\n"


def ev(day: str, title: str, *extra: str) -> str:
    """Een afspraak zoals een afvalophaler hem exporteert: een hele dag, een titel, geen herhaling."""
    return "BEGIN:VEVENT\r\nUID:" + (day + title).encode().hex() + "\r\nDTSTAMP:20261127T145042Z\r\nSUMMARY:" + title + "\r\nDTSTART;VALUE=DATE:" + day + "\r\n" + "".join(x + "\r\n" for x in extra) + "END:VEVENT\r\n"


# ------------------------------------------------------------ herhalingsregels


def days(start, rule, skip=(), lo=date(2026, 11, 1), hi=date(2027, 3, 1)):
    return [d.isoformat() for d in expand(start, rule, set(skip), lo, hi)]


def test_weekly_every_two_weeks_with_count():
    assert days(date(2026, 12, 1), "FREQ=WEEKLY;INTERVAL=2;COUNT=4") == ["2026-12-01", "2026-12-15", "2026-12-29", "2027-01-12"]


def test_weekly_on_several_days_and_until():
    got = days(date(2026, 12, 1), "FREQ=WEEKLY;BYDAY=TU,FR;UNTIL=20261212T000000Z")  # 1 december 2026 is een dinsdag
    assert got == ["2026-12-01", "2026-12-04", "2026-12-08", "2026-12-11"]


def test_daily_monthly_and_yearly():
    assert days(date(2026, 12, 30), "FREQ=DAILY;INTERVAL=2;COUNT=3") == ["2026-12-30", "2027-01-01", "2027-01-03"]
    assert days(date(2026, 11, 30), "FREQ=MONTHLY", hi=date(2027, 4, 30)) == ["2026-11-30", "2026-12-30", "2027-01-30", "2027-03-30", "2027-04-30"], "een 30e bestaat niet in februari: die keer vervalt"
    assert days(date(2025, 12, 5), "FREQ=YEARLY", lo=date(2026, 1, 1), hi=date(2028, 1, 1)) == ["2026-12-05", "2027-12-05"]


def test_exdate_skips_a_day_and_count_still_counts_it():
    assert days(date(2026, 12, 1), "FREQ=WEEKLY;COUNT=3", skip=[date(2026, 12, 8)]) == ["2026-12-01", "2026-12-15"]


def test_the_window_cuts_the_result_but_count_counts_from_the_start():
    assert days(date(2026, 1, 6), "FREQ=WEEKLY;COUNT=60", lo=date(2026, 11, 20), hi=date(2026, 12, 5)) == ["2026-11-24", "2026-12-01"]
    assert days(date(2026, 1, 6), "FREQ=WEEKLY;COUNT=3", lo=date(2026, 11, 1), hi=date(2027, 1, 1)) == [], "na drie keer is het voorbij"


@pytest.mark.parametrize("rule", ["FREQ=MONTHLY;BYDAY=1MO", "FREQ=MONTHLY;BYSETPOS=1;BYDAY=MO", "FREQ=SECONDLY", "FREQ=WEEKLY;BYDAY=XX", "FREQ=WEEKLY;INTERVAL=abc", "FREQ=MONTHLY;BYMONTHDAY=-1"])
def test_a_rule_we_do_not_know_counts_only_the_first_day(rule):
    assert days(date(2026, 12, 7), rule) == ["2026-12-07"], "liever te weinig dan een verkeerde dag"


def test_endless_rules_are_capped():
    assert len(expand(date(2000, 1, 1), "FREQ=DAILY", set(), date(2000, 1, 1), date(2100, 1, 1))) <= tvhub.MAX_OCCURRENCES


def test_ics_with_recurrence_and_exdate():
    text = cal("BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20261201\r\nRRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=3\r\nEXDATE;VALUE=DATE:20261215\r\nSUMMARY:PMD\r\nEND:VEVENT\r\n")
    assert parse_ics(text, None, TODAY) == [{"start": "2026-12-01", "summary": "PMD"}, {"start": "2026-12-29", "summary": "PMD"}]


def test_ics_window_and_order_and_same_day_events():
    text = cal(ev("20261110", "OUD"), ev("20261208", "RESTAFVAL"), ev("20261208", "PMD"), ev("20261201", "GFT"), ev("20290101", "VER"))
    got = parse_ics(text, None, TODAY)
    assert [(e["start"], e["summary"]) for e in got] == [("2026-12-01", "GFT"), ("2026-12-08", "PMD"), ("2026-12-08", "RESTAFVAL")], "alleen vanaf gisteren tot ruim een jaar, op datum"
    assert [e["start"] for e in parse_ics(text, None, TODAY, back=30)][:1] == ["2026-11-10"]


# ------------------------------------------------------------ upload


def hub_at(tmp_path):
    r, hub = make(tmp_path)
    r.wall = time.time()
    return r, hub


def real_shape(r):
    t = r.engine.local_now().date()
    d = lambda n: (t + timedelta(days=n)).strftime("%Y%m%d")  # noqa: E731
    return cal(ev(d(-9), "GFT"), ev(d(1), "PMD"), ev(d(1), "RESTAFVAL"), ev(d(3), "GROFVUIL"), ev(d(8), "PAPIER"), ev(d(120), "KERSTBOMEN"))


def test_upload_is_read_kept_and_comes_back_after_a_restart(tmp_path):
    r, hub = hub_at(tmp_path)
    info = hub.upload_ics(real_shape(r), "kalender.ics")
    assert info["count"] == 5 and info["name"] == "kalender.ics" and info["loaded"] is True, "de afspraak van 9 dagen geleden telt niet meer mee"
    w = hub.data()["waste"]
    assert w["configured"] is True and [(i["label"], i["days"]) for i in w["items"]] == [("PMD", 1), ("Restafval", 1), ("Grofvuil", 3), ("Papier", 8), ("Kerstbomen", 120)][:6]
    r2 = Rig(tmp_path)
    r2.wall = r.wall
    r2.engine.set_tz("UTC")
    again = TvHub(tmp_path, r2.engine, port=0)
    assert again.ics_view()["count"] == 5 and again.ics_name == "kalender.ics" and again.data()["waste"]["items"][0]["label"] == "PMD"


def test_all_caps_titles_read_well_but_abbreviations_stay(tmp_path):
    r, hub = hub_at(tmp_path)
    t = r.engine.local_now().date()
    hub.upload_ics(cal(ev((t + timedelta(days=1)).strftime("%Y%m%d"), "GFT"), ev((t + timedelta(days=2)).strftime("%Y%m%d"), "RESTAFVAL"), ev((t + timedelta(days=3)).strftime("%Y%m%d"), "Papier")))
    assert [i["label"] for i in hub.data()["waste"]["items"]] == ["GFT", "Restafval", "Papier"]


@pytest.mark.parametrize("text,word", [("hallo", "ICS-bestand"), ("", "ICS-bestand"), ("BEGIN:VCALENDAR\r\nEND:VCALENDAR", "geen ophalingen"),
                                       (None, "ICS-bestand"), ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART;VALUE=DATE:19990101\r\nSUMMARY:Oud\r\nEND:VEVENT\r\nEND:VCALENDAR", "geen ophalingen")])
def test_a_file_that_is_not_a_calendar_is_refused_and_nothing_changes(tmp_path, text, word):
    r, hub = hub_at(tmp_path)
    hub.upload_ics(real_shape(r), "goed.ics")
    with pytest.raises(ModelError, match=word):
        hub.upload_ics(text)
    assert hub.ics_name == "goed.ics" and hub.ics_view()["count"] == 5, "een mislukte upload laat het vorige bestand staan"


def test_a_file_that_is_too_large_is_refused(tmp_path):
    _, hub = hub_at(tmp_path)
    with pytest.raises(ModelError, match="te groot"):
        hub.upload_ics("BEGIN:VCALENDAR\r\n" + "X" * (tvhub.MAX_BODY + 10))


def test_clearing_removes_the_file_and_the_calendar_is_a_placeholder_again(tmp_path):
    r, hub = hub_at(tmp_path)
    hub.upload_ics(real_shape(r), "k.ics")
    assert (tmp_path / "tv_ics.ics").exists()
    hub.clear_ics()
    assert not (tmp_path / "tv_ics.ics").exists() and not (tmp_path / "tv_ics.json").exists()
    assert hub.data()["waste"] == {"configured": False, "today": r.engine.local_now().date().isoformat(), "items": []}
    hub.clear_ics()  # nogmaals: geen fout


def test_file_ha_calendar_and_link_add_up(tmp_path):
    r, hub = hub_at(tmp_path)
    hub.update({**hub.cfg, "waste": {"on": True, "calendar": "calendar.afval", "ics": "", "days": 21}})
    t = r.engine.local_now().date()
    hub.upload_ics(cal(ev((t + timedelta(days=2)).strftime("%Y%m%d"), "PMD")))
    hub.events["waste"] = [{"start": (t + timedelta(days=1)).isoformat(), "summary": "Papier"}]
    hub.ics["events"] = [{"start": (t + timedelta(days=3)).isoformat(), "summary": "GFT"}]
    assert [i["label"] for i in hub.data()["waste"]["items"]] == ["Papier", "PMD", "GFT"]


def test_the_file_is_read_again_each_day(tmp_path):
    r, hub = hub_at(tmp_path)
    t = r.engine.local_now().date()
    hub.upload_ics(cal("BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:" + t.strftime("%Y%m%d") + "\r\nRRULE:FREQ=WEEKLY\r\nSUMMARY:PMD\r\nEND:VEVENT\r\n"))
    first = hub.ics_view()
    r.wall += 3 * 86400
    assert hub.ics_view()["first"] != first["first"], "de eerste ophaling schuift mee met de dag"


# ------------------------------------------------------------ de web-API


def test_web_api_uploads_shows_and_removes_the_file(tmp_path):
    from kkaddon import web as kweb
    from kkaddon.settings import Settings

    async def go():
        r, hub = hub_at(tmp_path)
        ctx = kweb.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), r.engine, tv=hub)
        c = TestClient(TestServer(kweb.make_app(ctx)))
        await c.start_server()
        try:
            ok = await c.put("/api/tv/ics?name=pick-up.ics", data=real_shape(r).encode("utf-8"), headers={"Content-Type": "text/calendar"})
            body = await ok.json()
            assert ok.status == 200 and body["file"]["count"] == 5 and body["file"]["name"] == "pick-up.ics"
            view = await (await c.get("/api/tv")).json()
            assert view["ics_file"]["loaded"] is True and view["ics_file"]["first"]
            data = await (await c.get("/tv/view/data")).json()
            assert data["waste"]["items"][0]["label"] == "PMD"
            bad = await c.put("/api/tv/ics", data=b"dit is geen kalender")
            assert bad.status == 400 and "ICS-bestand" in (await bad.json())["error"]
            big = await c.put("/api/tv/ics", data=b"x" * (1024 * 1024 + 10))
            assert big.status == 400 and "te groot" in (await big.json())["error"]
            assert (await c.delete("/api/tv/ics")).status == 200 and hub.ics_text == ""
            assert (await (await c.get("/tv/view/data")).json())["waste"]["configured"] is False
        finally:
            await c.close()
    asyncio.run(go())
