"""Een waarde van Loxone als bron voor de huisconfig (`x:<signaal>`, DESIGN §40): een 0/1, een getal of een wekkertekst als "Morgen, 6:45".
De tekst lezen (loxone.parse_alarm), het bericht met tekst (parse_values), de hub (onthouden, bewaren, de dag laten tellen) en de bron in de logica."""
import asyncio
from datetime import date

import aiohttp
import pytest

from kkaddon import loxone as lx
from kkaddon import model as m
from kkaddon.logic import BOOL, NUMBER, TIME
from kkaddon.loxonehub import LoxoneHub
from kkaddon.model import ModelError
from tests.rig import Rig

THU = date(2026, 11, 19)  # een donderdag


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ de wekkertekst


@pytest.mark.parametrize("text,minutes,day", [
    ("Morgen, 6:45", 405, date(2026, 11, 20)),
    ("morgen 06:45", 405, date(2026, 11, 20)),
    ("Vandaag, 7:00", 420, THU),
    ("Overmorgen, 5:30", 330, date(2026, 11, 21)),
    ("Vandaag 18:05 uur", 1085, THU),
    ("6:45", 405, None),
    ("06.45", 405, None),
    ("6u45", 405, None),
    ("Wekker: 6:45", 405, None),
    ("Za, 8:15", 495, date(2026, 11, 21)),
    ("Zondag 9:00", 540, date(2026, 11, 22)),
    ("Do, 6:45", 405, THU),  # de weekdag van vandaag is vandaag
    ("Woensdag, 6:45", 405, date(2026, 11, 25)),  # al geweest: de volgende
    ("Tomorrow, 6:45 AM", 405, date(2026, 11, 20)),
    ("Tomorrow 6:45 PM", 1125, date(2026, 11, 20)),
    ("12:10 AM", 10, None),
    ("12:10 PM", 730, None),
    ("27-11 6:45", 405, date(2026, 11, 27)),
    ("05/11, 6:45", 405, date(2027, 11, 5)),  # al geweest dit jaar: volgend jaar
    ("2026-12-24 07:30", 450, date(2026, 12, 24)),
    ("27.11. 6:45", 405, None),  # een datum met punten is niet te onderscheiden van een tijd; de tijd is wat telt
    ("0:00", 0, None),
    ("23:59", 1439, None),
])
def test_the_alarm_text_gives_a_time_and_a_day(text, minutes, day):
    a = lx.parse_alarm(text, THU)
    assert a is not None, text
    assert (a["minutes"], a["date"]) == (minutes, day), text


@pytest.mark.parametrize("text", ["", "Geen wekker", "Uit", "-", "--:--", "Morgen", "off", "25:00", "6:75", "13:00 PM", "nul", None, 645])
def test_text_without_a_time_is_no_alarm(text):
    assert lx.parse_alarm(text, THU) is None


def test_an_impossible_date_is_no_alarm_instead_of_an_error():
    assert lx.parse_alarm("31-02 6:45", THU) is None
    assert lx.parse_alarm("2026-13-40 6:45", THU) is None


# ------------------------------------------------------------ het bericht met tekst


def test_parse_values_takes_numbers_and_text_per_line():
    assert lx.parse_values(b"kk.a=1") == [("a", 1.0)]
    assert lx.parse_values(b"kk.wekker=Morgen, 6:45") == [("wekker", "Morgen, 6:45")]
    assert lx.parse_values("kk.a=1\nkk.wekker=Vandaag, 7:00\r\nkk.b=-2.5") == [("a", 1.0), ("wekker", "Vandaag, 7:00"), ("b", -2.5)]
    assert lx.parse_values(b"kk.wekker=Za, 8:15;kk.a=0") == [("wekker", "Za, 8:15"), ("a", 0.0)]
    assert lx.parse_values(b"kk.wekker=Zondag \xe9") == [("wekker", "Zondag é")], "geen UTF-8: latin-1"
    assert lx.parse_values("kk.wekker=Morgen, 6:45 – aan") == [("wekker", "Morgen, 6:45 – aan")]


def test_parse_values_still_refuses_what_is_not_a_message():
    for bad in (b"", b"a=1", b"kk.A=1", b"kk.a=", b"kk.=1", b"kk.a = 1", b"\xff\xfe", b"kk.a=" + b"x" * 61, b"kk.a=1\n" + b"x" * 600, b"kk." + b"a" * 41 + b"=1"):
        assert lx.parse_values(bad) == [], bad
    assert lx.parse_values(b"kk.a=x\x00y") == [], "geen besturingstekens"


def test_parse_itself_stays_numeric():
    assert lx.parse(b"kk.wekker=Morgen, 6:45") == []


# ------------------------------------------------------------ de hub


def make(tmp_path, today=THU):
    r = Rig(tmp_path)
    hub = LoxoneHub(tmp_path, r.engine, None, port=0, gap=0.001, start_delay=0, tick=0.02)
    hub.miniserver = "127.0.0.1"
    hub._today = lambda: today
    hub._now_min = lambda: 300  # 05:00, voor de wekker van 06:45 (zonder dit hangt een test af van het uur van de dag)
    return r, hub


def alarm(hub, sid="wekker", kind="time", **kw):
    hub.add_signal(sid, {"dir": "in", "ref": f"x:{sid}", "kind": kind, "label": "Wekker", **kw})


def test_a_value_for_the_logic_is_a_signal_that_is_its_own_source(tmp_path):
    _, hub = make(tmp_path)
    alarm(hub)
    s = hub.signals["wekker"]
    assert (s["dir"], s["ref"], s["kind"]) == ("in", "x:wekker", "time")
    for bad in ({"dir": "in", "ref": "x:ander", "kind": "time"}, {"dir": "in", "ref": "x:", "kind": "time"}):
        with pytest.raises(ModelError):
            hub.add_signal("nog", bad)
    hub.add_signal("uit", {"dir": "out", "ref": "t:iets", "kind": "time"})
    assert hub.signals["uit"]["kind"] == "float", "een tijd kan alleen een waarde van Loxone zijn, niet naar Loxone"


def test_an_alarm_text_becomes_minutes_when_it_is_today(tmp_path):
    async def go():
        _, hub = make(tmp_path)
        alarm(hub)
        assert hub.value_of("x:wekker") is None, "nog niets ontvangen"
        assert await hub.apply([("wekker", "Vandaag, 6:45")]) == [("wekker", "ok")]
        assert hub.value_of("x:wekker") == 405
        assert hub.got["wekker"]["result"] == "tijd 06:45 op 2026-11-19"

    run(go())


def test_tomorrows_alarm_is_unknown_today_and_the_time_tomorrow_even_if_loxone_sends_nothing_at_midnight(tmp_path):
    async def go():
        _, hub = make(tmp_path)
        alarm(hub)
        await hub.apply([("wekker", "Morgen, 6:45")])
        assert hub.value_of("x:wekker") is None
        hub._today = lambda: date(2026, 11, 20)
        assert hub.value_of("x:wekker") == 405
        hub._today = lambda: date(2026, 11, 21)
        assert hub.value_of("x:wekker") is None, "voorbij: weer onbekend"

    run(go())


def test_a_time_without_a_day_counts_every_day(tmp_path):
    async def go():
        _, hub = make(tmp_path)
        alarm(hub)
        await hub.apply([("wekker", "6:45")])
        for d in (THU, date(2026, 11, 20), date(2026, 12, 31)):
            hub._today = lambda d=d: d
            assert hub.value_of("x:wekker") == 405

    run(go())


def test_no_alarm_makes_the_source_unknown_and_says_so(tmp_path):
    async def go():
        _, hub = make(tmp_path)
        alarm(hub)
        await hub.apply([("wekker", "Vandaag, 6:45")])
        assert await hub.apply([("wekker", "Geen wekker")]) == [("wekker", "ok")]
        assert hub.value_of("x:wekker") is None
        assert hub.got["wekker"]["result"] == "geen tijd in de tekst: onbekend"
        assert hub.in_view()[0]["text"] == "Geen wekker"

    run(go())


def test_a_number_for_a_time_and_text_for_a_number_are_refused_with_a_reason(tmp_path):
    async def go():
        _, hub = make(tmp_path)
        alarm(hub)
        alarm(hub, "regen", "float", decimals=1)
        assert (await hub.apply([("wekker", 24300.0)]))[0][1].startswith("verwacht een tekst met een tijd")
        assert await hub.apply([("regen", "veel")]) == [("regen", "verwacht een getal")]
        assert hub.stats["refused"] == 2
        assert hub.value_of("x:wekker") is None and hub.value_of("x:regen") is None

    run(go())


def test_text_for_a_point_of_a_device_is_refused(tmp_path):
    async def go():
        _, hub = make(tmp_path)
        hub.signals["bri"] = {"dir": "in", "ref": "e:tuin:bri", "label": "", "kind": "int", "decimals": 0, "min": 0, "max": 100, "deadband": 0.0, "heartbeat": 60,
                              "skip_first": False, "enabled": True, "ack": False}
        assert await hub.apply([("bri", "Morgen, 6:45")]) == [("bri", "verwacht een getal")]

    run(go())


def test_a_digital_variable_is_a_bool_and_a_number_keeps_its_kind(tmp_path):
    async def go():
        _, hub = make(tmp_path)
        alarm(hub, "thuis", "bool")
        alarm(hub, "stand", "int")
        alarm(hub, "temp", "float", decimals=1, min=-30, max=60)
        await hub.apply([("thuis", 1.0), ("stand", 2.6), ("temp", 21.46)])
        assert hub.value_of("x:thuis") is True
        assert hub.value_of("x:stand") == 3
        assert hub.value_of("x:temp") == 21.46
        await hub.apply([("thuis", 0.0)])
        assert hub.value_of("x:thuis") is False
        assert await hub.apply([("temp", 99.0)]) == [("temp", "buiten het bereik (-30 tot 60)")]
        assert hub.value_of("x:temp") == 21.46, "een geweigerde waarde verandert niets"

    run(go())


def test_the_same_value_again_does_nothing_and_a_new_one_is_pushed_to_the_ui(tmp_path):
    async def go():
        _, hub = make(tmp_path)
        alarm(hub)
        n = []
        hub.changed = lambda: n.append(1)
        assert await hub.apply([("wekker", "Morgen, 6:45")]) == [("wekker", "ok")]
        assert await hub.apply([("wekker", "Morgen, 6:45")]) == [("wekker", "zelfde")]
        assert await hub.apply([("wekker", "Morgen, 7:00")]) == [("wekker", "ok")]
        assert len(n) == 2

    run(go())


def test_a_disabled_or_removed_signal_is_unknown(tmp_path):
    async def go():
        _, hub = make(tmp_path)
        alarm(hub, "thuis", "bool")
        await hub.apply([("thuis", 1.0)])
        hub.update_signal("thuis", {"enabled": False})
        assert hub.value_of("x:thuis") is None and hub.in_view() == []
        hub.update_signal("thuis", {"enabled": True})
        assert hub.value_of("x:thuis") is True
        hub.remove_signal("thuis")
        assert hub.value_of("x:thuis") is None and hub.type_of("x:thuis") is None
        assert "thuis" not in hub.vals

    run(go())


def test_the_last_value_survives_a_restart_and_a_broken_file_is_ignored(tmp_path):
    async def go():
        r, hub = make(tmp_path)
        alarm(hub)
        alarm(hub, "thuis", "bool")
        await hub.apply([("wekker", "Morgen, 6:45"), ("thuis", 1.0)])
        hub2 = LoxoneHub(tmp_path, r.engine, None, port=0)
        hub2._today = lambda: date(2026, 11, 20)
        assert hub2.value_of("x:wekker") == 405 and hub2.value_of("x:thuis") is True
        (tmp_path / "loxone_values.json").write_text("{kapot", encoding="utf-8")
        hub3 = LoxoneHub(tmp_path, r.engine, None, port=0)
        assert hub3.value_of("x:thuis") is None and hub3.signals.keys() == {"wekker", "thuis"}

    run(go())


def test_the_view_shows_what_was_received_and_what_it_means(tmp_path):
    async def go():
        _, hub = make(tmp_path)
        alarm(hub)
        await hub.apply([("wekker", "Vandaag, 6:45")])
        row = hub.view()["signals"][0]
        assert (row["logic"], row["text"], row["got"], row["value"], row["pending"]) == (True, "do 19-11 06:45", "Vandaag, 6:45", 405, False)
        assert hub.in_view() == [{"id": "wekker", "label": "Wekker", "kind": "time", "type": TIME, "ref": "x:wekker", "value": 405, "raw": "Vandaag, 6:45", "text": "do 19-11 06:45", "pending": False, "held": None, "wall": hub.vals["wekker"]["wall"]}]

    run(go())


# ------------------------------------------------------------ in de logica


def test_the_source_has_a_type_the_logic_can_check(tmp_path):
    _, hub = make(tmp_path)
    alarm(hub)
    alarm(hub, "thuis", "bool")
    alarm(hub, "temp", "float")
    assert m.SOURCE_RE.match("x:wekker") and not m.SOURCE_RE.match("x:") and not m.SOURCE_RE.match("x:Wekker")
    assert m.source_type("x:wekker", {}, {}) == TIME
    assert m.source_type("x:thuis", {}, {}) == BOOL
    assert m.source_type("x:temp", {}, {}) == NUMBER
    assert m.source_type("x:weg", {}, {}) == m.ANY, "een signaal dat weg is geeft onbekend, nooit een fout"
    assert m.fits(m.source_type("x:wekker", {}, {}), TIME) and not m.fits(m.source_type("x:wekker", {}, {}), BOOL)


def test_the_engine_reads_the_value_for_a_module_a_rule_or_a_card(tmp_path):
    async def go():
        r, hub = make(tmp_path)
        alarm(hub)
        alarm(hub, "thuis", "bool")
        assert r.engine.ref_value("x:wekker") is None and r.engine.ref_value("x:thuis") is None
        await hub.apply([("wekker", "6:45"), ("thuis", 1.0)])
        assert r.engine.ref_value("x:wekker") == 405
        assert r.engine.ref_value("x:thuis") is True
        assert r.engine.snapshot()["loxone_in"][0]["ref"] in ("x:thuis", "x:wekker")

    run(go())


def test_a_module_can_use_the_alarm_time(tmp_path):
    async def go():
        r, hub = make(tmp_path)
        alarm(hub)
        await hub.apply([("wekker", "6:45")])
        from kkaddon.logic import evaluate
        mods = {"s": {"type": "sub", "name": "tien ervoor", "inputs": {"a": "x:wekker", "b": "m:tien"}}, "tien": {"type": "time", "name": "10 min", "value": "00:10", "inputs": {}}}
        assert evaluate(mods, lambda t: None, 0, None, None, r.engine._airco)["s"] == 395

    run(go())


def test_a_text_travels_over_the_real_http_and_udp_sockets(tmp_path):
    async def go():
        _, hub = make(tmp_path)
        alarm(hub)
        hub.transport = "udp"
        await hub.start()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"http://127.0.0.1:{hub.http_port}/loxone", params={"kk.wekker": "Morgen, 6:45"}) as resp:
                    assert resp.status == 200, await resp.text()
            assert hub.vals["wekker"]["raw"] == "Morgen, 6:45"
            import socket
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.bind(("127.0.0.1", 0))
            u.sendto(b"kk.wekker=Overmorgen, 5:30", ("127.0.0.1", hub.udp_port))
            u.close()
            for _ in range(200):
                if hub.vals["wekker"]["raw"].startswith("Overmorgen"):
                    break
                await asyncio.sleep(0.01)
            assert hub.vals["wekker"]["raw"] == "Overmorgen, 5:30"
            assert hub.vals["wekker"]["date"] == "2026-11-21"
        finally:
            await hub.close()

    run(go())


def test_a_module_input_accepts_a_loxone_time_and_refuses_the_wrong_kind(tmp_path):
    async def go():
        r, hub = make(tmp_path)
        alarm(hub)
        alarm(hub, "thuis", "bool")
        mid = r.model.add_module("sub")
        r.model.set_module(mid, inputs={"a": "x:wekker"})  # een tijd op een tijd-ingang
        with pytest.raises(ModelError, match="verwacht"):
            r.model.set_module(mid, inputs={"a": "x:thuis"})  # een 0/1 op een tijd-ingang
        of = r.model.add_module("or")
        r.model.set_module(of, inputs={"a": "x:thuis"})
        with pytest.raises(ModelError, match="verwacht"):
            r.model.set_module(of, inputs={"a": "x:wekker"})
        assert r.engine.module_values()[of] is None, "nog niets ontvangen: onbekend"
        await hub.apply([("wekker", "6:45"), ("thuis", 1.0)])
        assert r.engine.module_values()[of] is True, "Of met een 1 is 1, ook zonder de tweede ingang"
        assert r.engine.ref_value(f"m:{of}") is True and r.engine.ref_value("x:wekker") == 405
        await hub.apply([("thuis", 0.0)])
        assert r.engine.module_values()[of] is None, "0 en een lege ingang: onbekend"

    run(go())


def test_the_schema_has_one_block_with_a_row_per_value_and_none_without(tmp_path):
    async def go():
        from kkaddon import schema
        r, hub = make(tmp_path)
        assert "x:loxone" not in {b["key"] for b in schema.view(r.model, r.engine.snapshot())["blocks"]}
        alarm(hub)
        alarm(hub, "thuis", "bool")
        await hub.apply([("wekker", "Vandaag, 6:45"), ("thuis", 1.0)])
        blocks = {b["key"]: b for b in schema.view(r.model, r.engine.snapshot())["blocks"]}
        b = blocks["x:loxone"]
        assert (b["kind"], b["rows"], b["page"]) == ("loxone", 2, None)
        outs = {o["ref"]: o for o in b["outs"]}
        assert outs["x:wekker"]["type"] == "time" and outs["x:wekker"]["text"] == "do 19-11 06:45" and outs["x:wekker"]["value"] == 405
        assert outs["x:thuis"]["type"] == "bool" and outs["x:thuis"]["value"] is True
        r.model.set_schema({"pages": [{"id": "woonkamer", "name": "Woonkamer"}], "blocks": {}, "switches": {}, "names": {}})
        schema.place(r.model, r.engine.snapshot(), "x:loxone", "woonkamer", 40, 60)  # op een ruimte zetten, zoals de pagina doet
        assert r.model.schema["blocks"]["x:loxone"] == {"page": "woonkamer", "x": 40, "y": 60}
        assert {b["key"]: b for b in schema.view(r.model, r.engine.snapshot())["blocks"]}["x:loxone"]["page"] == "woonkamer"
        schema.place(r.model, r.engine.snapshot(), "x:loxone", None)  # en er weer af halen
        hub.update_signal("wekker", {"enabled": False})
        assert [o["ref"] for o in {b["key"]: b for b in schema.view(r.model, r.engine.snapshot())["blocks"]}["x:loxone"]["outs"]] == ["x:thuis"]

    run(go())


def test_the_view_shows_the_real_day_and_time_even_when_the_logic_does_not_know_it_yet(tmp_path):
    async def go():
        _, hub = make(tmp_path)
        alarm(hub)
        for text, shown, pending in (("Morgen, 06:45", "vr 20-11 06:45", True), ("Zaterdag, 07:00", "za 21-11 07:00", True), ("Vandaag, 6:45", "do 19-11 06:45", False),
                                     ("6:45", "06:45", False), ("Geen wekker", "Geen wekker", False)):
            await hub.apply([("wekker", text)])
            v = hub.in_view()[0]
            assert (v["text"], v["pending"]) == (shown, pending), text
            assert hub.view()["signals"][0]["text"] == shown
            assert (hub.value_of("x:wekker") is None) == (pending or text == "Geen wekker"), "voor de logica blijft het onbekend tot de dag zelf"

    run(go())


def test_todays_alarm_stays_after_it_went_off_and_loxone_announced_the_next_one(tmp_path):
    async def go():
        _, hub = make(tmp_path)
        alarm(hub)
        await hub.apply([("wekker", "Vandaag, 6:45")])
        assert hub.value_of("x:wekker") == 405
        hub._now_min = lambda: 405  # de wekker gaat af en Loxone meldt meteen de volgende
        await hub.apply([("wekker", "Zaterdag, 07:00")])
        assert hub.value_of("x:wekker") == 405, "de tijd van vandaag blijft, voor de regel die nog minuten erna nodig heeft"
        v = hub.in_view()[0]
        assert (v["text"], v["pending"], v["held"]) == ("za 21-11 07:00", True, "06:45"), "het blok toont de volgende, en dat vandaag nog 06:45 geldt"
        await hub.apply([("wekker", "Zaterdag, 07:00")])  # Loxone herhaalt zich: de tijd van vandaag blijft
        assert hub.value_of("x:wekker") == 405
        await hub.apply([("wekker", "Geen wekker")])  # de laatste wekker ging af en er komt geen andere
        assert hub.value_of("x:wekker") == 405
        hub._today = lambda: date(2026, 11, 20)  # middernacht
        assert hub.value_of("x:wekker") is None and hub.in_view()[0]["held"] is None
        await hub.apply([("wekker", "Vandaag, 7:00")])
        assert hub.value_of("x:wekker") == 420

    run(go())


def test_a_snooze_later_today_replaces_the_time_that_just_went_off(tmp_path):
    async def go():
        _, hub = make(tmp_path)
        alarm(hub)
        await hub.apply([("wekker", "Vandaag, 6:45")])
        hub._now_min = lambda: 405
        await hub.apply([("wekker", "Vandaag, 6:54")])
        assert hub.value_of("x:wekker") == 414 and hub.in_view()[0]["held"] is None

    run(go())


def test_changing_or_cancelling_todays_alarm_before_it_went_off_does_not_keep_it(tmp_path):
    async def go():
        _, hub = make(tmp_path)
        alarm(hub)
        await hub.apply([("wekker", "Vandaag, 6:45")])  # het is 05:00: hij is nog niet afgegaan
        await hub.apply([("wekker", "Morgen, 6:45")])
        assert hub.value_of("x:wekker") is None
        await hub.apply([("wekker", "Vandaag, 6:45")])
        await hub.apply([("wekker", "Geen wekker")])
        assert hub.value_of("x:wekker") is None

    run(go())


def test_the_kept_time_survives_a_restart_and_a_fresh_start_keeps_nothing(tmp_path):
    async def go():
        r, hub = make(tmp_path)
        alarm(hub)
        await hub.apply([("wekker", "Vandaag, 6:45")])
        hub._now_min = lambda: 405
        await hub.apply([("wekker", "Morgen, 6:45")])
        hub2 = LoxoneHub(tmp_path, r.engine, None, port=0)
        hub2._today = lambda: THU
        assert hub2.value_of("x:wekker") == 405
        (tmp_path / "leeg").mkdir()
        _, fresh = make(tmp_path / "leeg")
        alarm(fresh)
        fresh._now_min = lambda: 700
        await fresh.apply([("wekker", "Morgen, 6:45")])
        assert fresh.value_of("x:wekker") is None, "na een eerste bericht is er niets om vast te houden"

    run(go())
