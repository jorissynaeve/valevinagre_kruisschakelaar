"""Een Smappee-laadpaal in het laadblok (chargehub.py): de sessies komen uit de cloud met de badge erbij en worden per huis geboekt, pauzeren en de stroom (nooit start of stop), wie dat
mag, de kaart op het paneel, en het geheel met de echte EspDevHub tegen een nagebootste Smappee-cloud.

De sessies uit Smappee volgen de aanname in smappee.py (zie tests/fakesmappee.py): of de echte lijst `rfidToken` en `energy` precies zo geeft, is door ons niet gezien."""
import asyncio
import json
import time

import pytest

from kkaddon import cards, smappee
from kkaddon import espdevhub as eh
from kkaddon.chargehub import ChargeHub
from kkaddon.engine import Engine
from kkaddon.espdevhub import EspDevHub
from kkaddon.model import Model, ModelError
from tests.fakesmappee import DEVICE, PASSWORD, USERNAME, FakeSmappee
from tests.rig import KEY, Rig

T0 = 1_700_000_000.0
JORIS, PIET = "04AAAA01", "04BBBB02"
TAGS = {JORIS: {"name": "Joris", "by": ""}, PIET: {"name": "Piet", "by": "buren"}}


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class StubEsp:
    """Wat de laadhub van de EspDevHub gebruikt, zonder cloud."""

    def __init__(self):
        self.devices = {"smappee_x": {"driver": "smappee", "points": [{"id": "current", "min": 6, "max": 32}]}}
        self.up = True
        self.values = {"state": smappee.S_CONNECTED, "current": 19}
        self.sessions: list[dict] = []
        self.sent: list[tuple] = []
        self.refuse = ""

    def is_online(self, did):
        return self.up and did in self.devices

    def point_value(self, did, pid):
        return self.values.get(pid) if self.up else None

    def smappee_sessions(self, did):
        return list(self.sessions) if self.up else None

    async def set(self, did, pid, value, confirm=False):
        if self.refuse:
            raise ModelError(self.refuse)
        self.sent.append((pid, value))
        return {"ok": True}


def sess(start, end, kwh, tag):
    return {"key": str(int(start)), "start": float(start), "end": None if end is None else float(end), "kwh": kwh, "tag": tag}


def make(tmp_path, tags=None, price=0.25, **extra):
    clock, esp = Clock(), StubEsp()
    hub = ChargeHub(tmp_path, Engine(KEY, Model(), lambda raw: None), None, clock=clock)
    hub.home, hub.peer_name, hub.espdev = lambda: "Atelier", lambda pid: pid.capitalize(), esp
    cid = hub.add({"name": "Oprit", "espdev": "smappee_x", "price": price, "tags": TAGS if tags is None else tags, **extra})
    return hub, esp, clock, cid


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ instellen


def test_a_smappee_charger_needs_a_smappee_block_and_has_no_tuya_fields(tmp_path):
    hub, esp, clock, cid = make(tmp_path)
    c = hub.chargers[cid]
    assert cid == "smappee_x" and c["espdev"] == "smappee_x" and c["device"] == "" and c["since"] == T0, "een sessie telt vanaf het koppelen"
    esp.devices["tuya_ding"] = {"driver": "huisdev", "points": []}
    with pytest.raises(ModelError, match="Smappee-laadpunt"):
        hub.add({"name": "Fout", "espdev": "tuya_ding"})
    with pytest.raises(ModelError, match="geen Tuya-toestel"):
        hub.add({"name": "Fout", "espdev": "smappee_x", "inputs": {"status": "101"}})
    with pytest.raises(ModelError, match="houdt zijn laadpunt"):
        esp.devices["smappee_y"] = {"driver": "smappee", "points": []}
        hub.update(cid, {"espdev": "smappee_y"})
    hub.update(cid, {"since": T0 - 86400})
    assert hub.chargers[cid]["since"] == T0 - 86400
    for bad in (-1, "gisteren", True):
        with pytest.raises(ModelError, match="begin"):
            hub.update(cid, {"since": bad})


def test_badges_are_validated_and_a_name_alone_gives_a_house_id(tmp_path):
    hub, esp, clock, cid = make(tmp_path, tags={})
    hub.update(cid, {"tags": {"AB12": {"name": "Els de Buur"}}})
    assert hub.chargers[cid]["tags"] == {"AB12": {"by": "els_de_buur", "name": "Els de Buur"}}
    hub.update(cid, {"tags": {"AB12": {"name": "Thuis", "by": ""}, "CD34": {"name": "Thuis", "by": ""}}})
    assert {t["by"] for t in hub.chargers[cid]["tags"].values()} == {""}, "twee badges van een huis"
    for bad in ({"met spatie": {"name": "x"}}, {"AB": {"name": ""}}, {"AB": {"name": "x", "by": "Fout Id"}}, {"AB": "x"}, "geen dict", {"": {"name": "x"}}):
        with pytest.raises(ModelError):
            hub.update(cid, {"tags": bad})
    with pytest.raises(ModelError, match="maximaal"):
        hub.update(cid, {"tags": {f"T{i}": {"name": "x", "by": ""} for i in range(smappee_max() + 1)}})


def smappee_max():
    from kkaddon.chargehub import MAX_TAGS
    return MAX_TAGS


# ------------------------------------------------------------ sessies uit de cloud


def test_a_finished_session_is_booked_once_for_the_house_of_the_badge(tmp_path):
    hub, esp, clock, cid = make(tmp_path, price=0.25)
    esp.sessions = [sess(T0 + 3600, T0 + 7200, 8.0, PIET), sess(T0 + 100, T0 + 1900, 4.0, JORIS)]
    hub.tick()
    assert [(e["name"], e["by"], e["kwh"], e["cost"], e["seconds"]) for e in hub.log] == [("Joris", "", 4.0, 1.0, 1800), ("Piet", "buren", 8.0, 2.0, 3600)], "de oudste eerst, elk op zijn eigen huis"
    assert hub.log[0]["reason"] == "sessie uit Smappee" and hub.log[0]["tag"] == JORIS and hub.log[0]["tariff"] == 0.25
    for _ in range(3):
        hub.tick()
    esp.sessions = list(esp.sessions)
    hub._cloud_sig.clear()
    hub.tick()
    assert len(hub.log) == 2, "dezelfde sessie wordt niet nog eens geboekt, ook niet als de lijst opnieuw wordt gelezen"


def test_the_invoice_data_splits_per_house(tmp_path):
    hub, esp, clock, cid = make(tmp_path)
    esp.sessions = [sess(T0 + 5000, T0 + 6000, 3.0, PIET), sess(T0 + 3000, T0 + 4000, 5.0, JORIS), sess(T0 + 100, T0 + 900, 2.0, PIET)]
    hub.tick()
    per: dict[str, float] = {}
    for e in hub.sessions()["sessions"]:
        per[e["name"]] = per.get(e["name"], 0) + e["kwh"]
    assert per == {"Joris": 5.0, "Piet": 5.0} and "Joris" in hub.csv() and "Piet" in hub.csv()


def test_sessions_from_before_the_charger_was_added_are_not_booked(tmp_path):
    hub, esp, clock, cid = make(tmp_path)
    esp.sessions = [sess(T0 - 7200, T0 - 3600, 9.0, JORIS), sess(T0 + 60, T0 + 600, 1.0, JORIS)]
    hub.tick()
    assert [e["kwh"] for e in hub.log] == [1.0]
    hub.update(cid, {"since": T0 - 86400})  # wie het wil, boekt ook de week ervoor
    hub.tick()
    assert sorted(e["kwh"] for e in hub.log) == [1.0, 9.0]


def test_a_badge_without_a_house_and_a_session_without_a_badge_are_told_apart(tmp_path):
    hub, esp, clock, cid = make(tmp_path)
    esp.sessions = [sess(T0 + 4000, T0 + 5000, 2.0, ""), sess(T0 + 2000, T0 + 3000, 3.0, "04CCCC03"), sess(T0 + 100, T0 + 900, 1.0, "04DDDD04")]
    hub.tick()
    names = [e["name"] for e in hub.log]
    assert names == ["Onbekende badge …DD04", "Onbekende badge …CC03", "Zonder badge"] and hub.log[2]["by"] == "-"
    assert hub.log[0]["by"] != hub.log[1]["by"] and hub.log[0]["by"].startswith("?"), "elke onbekende badge is een eigen groep op de factuur"
    assert [(u["tag"], u["sessions"], u["kwh"]) for u in hub.status()[0]["unknown_tags"]] == [("04CCCC03", 1, 3.0), ("04DDDD04", 1, 1.0)], "de meest recente eerst"


def test_naming_a_badge_assigns_its_earlier_sessions_to_the_house(tmp_path):
    hub, esp, clock, cid = make(tmp_path, tags={})
    esp.sessions = [sess(T0 + 4000, T0 + 4500, 2.0, ""), sess(T0 + 2000, T0 + 3000, 3.0, JORIS), sess(T0 + 100, T0 + 900, 1.0, PIET)]  # nieuwste eerst, zoals smappee.sessions ze geeft
    hub.tick()
    assert [e["tag"] for e in hub.log] == [PIET, JORIS, ""] and all(e["name"].startswith("Onbekende") for e in hub.log[:2])
    hub.update(cid, {"tags": {JORIS: {"name": "Joris", "by": ""}}})
    assert [(e["name"], e["by"]) for e in hub.log if e["tag"] == JORIS] == [("Joris", "")]
    assert [e["name"] for e in hub.log if e["tag"] == PIET][0].startswith("Onbekende") and [e["name"] for e in hub.log if not e["tag"]] == ["Zonder badge"]
    hub.tick()
    assert len(hub.log) == 3, "opnieuw toewijzen boekt niets dubbel"


def test_a_running_session_is_the_active_session_and_becomes_a_log_line_when_it_ends(tmp_path):
    hub, esp, clock, cid = make(tmp_path)
    esp.values["state"] = smappee.S_CHARGING
    esp.sessions = [sess(T0 + 60, None, 2.5, PIET)]
    clock.t = T0 + 660
    hub.tick()
    s = hub.active[cid]
    assert (s["by"], s["name"], s["last_e"]) == ("buren", "Piet", 2.5) and hub.log == []
    v = hub.view(cid, None)
    assert (v["phase"], v["by"], v["energy"], v["time"], v["cost"]) == ("other", "Piet", 2.5, "0:10", 0.62)
    esp.sessions = [sess(T0 + 60, None, 4.0, PIET)]  # meer kWh: de actieve sessie volgt
    hub.tick()
    assert hub.active[cid]["last_e"] == 4.0
    esp.sessions = [sess(T0 + 60, T0 + 1800, 6.0, PIET)]
    hub.tick()
    assert cid not in hub.active and [(e["name"], e["kwh"]) for e in hub.log] == [("Piet", 6.0)]
    assert hub.view(cid, None)["phase"] == "free"


def test_nothing_happens_without_a_connection_to_smappee(tmp_path):
    hub, esp, clock, cid = make(tmp_path)
    esp.sessions = [sess(T0 + 60, T0 + 600, 1.0, JORIS)]
    esp.up = False
    hub.tick()
    assert hub.log == [] and not hub.active
    esp.up = True
    hub.tick()
    assert len(hub.log) == 1


def test_a_session_that_vanishes_from_the_list_ends_the_active_session_without_a_log_line(tmp_path):
    hub, esp, clock, cid = make(tmp_path)
    esp.sessions = [sess(T0 + 60, None, 2.5, JORIS)]
    hub.tick()
    assert cid in hub.active
    esp.sessions = []
    hub.tick()
    assert cid not in hub.active and hub.log == []


def test_bookings_and_what_was_imported_survive_a_restart_without_doubling(tmp_path):
    hub, esp, clock, cid = make(tmp_path)
    esp.sessions = [sess(T0 + 60, T0 + 600, 1.0, JORIS), sess(T0 + 900, None, 0.5, PIET)]
    hub.tick()
    saved = json.loads((tmp_path / "charge.json").read_text(encoding="utf-8"))
    assert saved["imported"][cid] == [str(int(T0 + 60))] and saved["chargers"][cid]["tags"][JORIS]["name"] == "Joris" and saved["active"][cid]["cloud"] == str(int(T0 + 900))
    hub2 = ChargeHub(tmp_path, Engine(KEY, Model(), lambda raw: None), None, clock=clock)
    hub2.espdev = esp
    assert len(hub2.log) == 1 and hub2.chargers[cid]["espdev"] == "smappee_x" and cid in hub2.active
    hub2.tick()
    assert len(hub2.log) == 1, "dezelfde sessie na een herstart niet nog eens"


# ------------------------------------------------------------ pauzeren en de stroom


def test_the_view_offers_pause_or_resume_and_never_start_or_stop(tmp_path):
    hub, esp, clock, cid = make(tmp_path)
    v = hub.view(cid, None)
    assert (v["act"], v["status"], v["charging"], v["amps"], v["amp_min"], v["amp_max"], v["ctl"]) == (None, "connect", False, 19, 6, 32, True), "niet aan het laden: niets te pauzeren"
    esp.values["state"] = smappee.S_CHARGING
    assert hub.view(cid, None)["act"] == "pause" and hub.view(cid, None)["status"] == "charging"
    esp.values["state"] = smappee.S_PAUSED
    v = hub.view(cid, None)
    assert (v["act"], v["status"]) == ("resume", "paused")
    esp.values["state"] = None
    assert hub.view(cid, None)["act"] is None and hub.view(cid, None)["status"] is None
    esp.up = False
    assert hub.view(cid, None)["online"] is False and hub.view(cid, None)["act"] is None
    assert not hub.can_start(hub.chargers[cid])


def test_start_and_stop_are_refused_with_the_reason(tmp_path):
    hub, esp, clock, cid = make(tmp_path)
    with pytest.raises(ModelError, match="badge aan de laadpaal"):
        hub.start(cid, hub.who_self())
    esp.sessions = [sess(T0 + 60, None, 1.0, JORIS)]
    hub.tick()
    with pytest.raises(ModelError, match="badge aan de laadpaal"):
        hub.stop(cid, hub.who_self())
    assert esp.sent == [] and cid in hub.active, "een weigering verandert niets aan de sessie"


def test_the_owner_pauses_resumes_and_sets_the_current(tmp_path):
    hub, esp, clock, cid = make(tmp_path)
    run(hub.control(cid, hub.who_self(), "pause"))
    run(hub.control(cid, hub.who_self(), "resume"))
    run(hub.control(cid, hub.who_self(), "current", "16"))
    run(hub.control(cid, hub.who_self(), "current", 12.0))
    assert esp.sent == [("paused", True), ("paused", False), ("current", 16.0), ("current", 12.0)]
    for op, val in (("start", None), ("stop", None), ("current", "veel"), ("current", None), ("nergens", 1)):
        with pytest.raises(ModelError):
            run(hub.control(cid, hub.who_self(), op, val))
    esp.refuse = "te snel na de vorige opdracht"
    with pytest.raises(ModelError, match="te snel"):
        run(hub.control(cid, hub.who_self(), "pause"))
    esp.up = False
    with pytest.raises(ModelError, match="niet bereikbaar"):
        run(hub.control(cid, hub.who_self(), "pause"))
    with pytest.raises(ModelError, match="onbekende laadpaal"):
        run(hub.control("nergens", hub.who_self(), "pause"))


def test_another_house_may_only_control_its_own_session(tmp_path):
    hub, esp, clock, cid = make(tmp_path)
    esp.values["state"] = smappee.S_CHARGING
    peer = hub.who_peer("buren")
    with pytest.raises(ModelError, match="alleen het huis dat laadt"):
        run(hub.control(cid, peer, "pause"))  # geen sessie
    esp.sessions = [sess(T0 + 60, None, 1.0, JORIS)]  # de eigenaar laadt
    hub.tick()
    for op in ("pause", "resume", "current"):
        with pytest.raises(ModelError, match="alleen het huis dat laadt"):
            run(hub.control(cid, peer, op, 10))
    assert hub.view(cid, "buren") == {"kind": "charger", "name": "Oprit", "online": True, "ctl": True, "phase": "busy", "act": None}, "tijdens de sessie van een ander niets dan bezet"
    assert esp.sent == []
    esp.sessions = [sess(T0 + 60, None, 1.0, PIET)]  # nu laadt het huis van Piet (by=buren)
    hub.tick()
    v = hub.view(cid, "buren")
    assert (v["phase"], v["act"], v["amps"]) == ("mine", "pause", 19) and v.get("by") is None
    run(hub.control(cid, peer, "pause"))
    run(hub.control(cid, peer, "current", "10"))
    assert esp.sent == [("paused", True), ("current", 10.0)]
    assert hub.view(cid, "garage")["phase"] == "busy" and hub.view(cid, "garage")["act"] is None
    owner = hub.view(cid, None)
    assert (owner["phase"], owner["by"], owner["act"]) == ("other", "Piet", "pause"), "de eigenaar kan altijd ingrijpen"


def test_the_nowait_variant_checks_at_once_and_sends_afterwards(tmp_path):
    async def go():
        hub, esp, clock, cid = make(tmp_path)
        hub.control_nowait(cid, hub.who_self(), "current", "14")
        with pytest.raises(ModelError, match="alleen het huis dat laadt"):
            hub.control_nowait(cid, hub.who_peer("buren"), "pause")
        assert esp.sent == [], "het versturen volgt pas na deze regel"
        await asyncio.sleep(0.05)
        assert esp.sent == [("current", 14.0)]
        esp.refuse = "Smappee weigert dit"
        hub.control_nowait(cid, hub.who_self(), "pause")  # de weigering komt niet terug bij de aanroeper maar in het log
        await asyncio.sleep(0.05)
        assert esp.sent == [("current", 14.0)]
    asyncio.run(go())


# ------------------------------------------------------------ de kaart op het paneel


def card(tmp_path, amps=True, hub=None):
    hub = hub or make(tmp_path)[0]
    cid = next(iter(hub.chargers))
    m = Model()
    m.add_card("charger")
    name = next(iter(m.cards))
    m.set_card(name, charger=f"k:{cid}", **({"amps": True} if amps else {}))
    return m, hub, name, cid


def test_the_card_has_a_current_row_only_when_asked_for_and_other_cards_stay_as_they_were(tmp_path):
    m, hub, name, cid = card(tmp_path, amps=True)
    rows = [r["entity"].split("__")[1] for r in cards._card(m, name, "nl")["entities"]]
    assert rows == ["run", "amps", "time", "energy", "price"]
    m2, _, name2, _ = card(tmp_path, amps=False, hub=hub)
    assert [r["entity"].split("__")[1] for r in cards._card(m2, name2, "nl")["entities"]] == ["run", "time", "energy", "price"] and "amps" not in m2.cards[name2]
    with pytest.raises(ModelError, match="onbekend veld"):
        m2.set_card(name2, bogus=1)
    m2.set_card(name2, amps=True)
    assert m2.cards[name2]["amps"] is True
    m2.set_card(name2, amps=False)
    assert "amps" not in m2.cards[name2]


def test_the_rows_show_the_state_a_pause_button_and_a_slider_for_the_current(tmp_path):
    m, hub, name, cid = card(tmp_path)
    esp = hub.espdev
    live = cards.Live(m, charge=hub)
    run_row = lambda: live.value(f"kk.{name}__run", "nl")  # noqa: E731
    assert (run_row()["kind"], run_row()["name"]) == ("text", "Aangesloten"), "niet aan het laden: alleen de status, geen knop"
    esp.values["state"] = smappee.S_CHARGING
    r = run_row()
    assert (r["kind"], r["name"], r["value"]) == ("button", "Aan het laden", "Pauze")
    esp.values["state"] = smappee.S_PAUSED
    r = run_row()
    assert (r["kind"], r["name"], r["value"]) == ("button", "Gepauzeerd", "Hervat")
    assert live.value(f"kk.{name}__run", "en")["value"] == "Resume"
    a = live.value(f"kk.{name}__amps", "nl")
    assert a == {"kind": "number", "value": "19|6|32", "text": "19 A"}
    esp.values["current"] = None  # Slim of Zon: geen regelaar
    assert live.value(f"kk.{name}__amps", "nl") == {"kind": "text", "value": "—"}
    esp.up = False
    assert live.value(f"kk.{name}__run", "nl")["name"] == "Laadpaal offline" and live.value(f"kk.{name}__amps", "nl")["kind"] == "text"


def test_pressing_the_card_pauses_and_the_slider_sets_the_current(tmp_path):
    async def go():
        m, hub, name, cid = card(tmp_path)
        esp = hub.espdev
        live = cards.Live(m, charge=hub)
        esp.values["state"] = smappee.S_CHARGING
        assert live.press(f"kk.{name}__run", "button", None) is True
        assert live.press(f"kk.{name}__amps", "number-set", "12") is True
        await asyncio.sleep(0.05)
        assert esp.sent == [("paused", True), ("current", 12.0)]
        esp.values["state"] = smappee.S_PAUSED
        assert live.press(f"kk.{name}__run", "button", None) is True
        await asyncio.sleep(0.05)
        assert esp.sent[-1] == ("paused", False)
        esp.values["state"] = smappee.S_CONNECTED  # niet aan het laden: niets te pauzeren
        assert live.press(f"kk.{name}__run", "button", None) is False
        esp.values["current"] = None
        assert live.press(f"kk.{name}__amps", "number-set", "12") is False, "bij Slim of Zon is er geen stroom in te stellen"
        assert live.press(f"kk.{name}__amps", "button", None) is False and len(esp.sent) == 3
    asyncio.run(go())


def test_a_tuya_charger_card_is_unchanged(tmp_path):
    from tests.test_charge import CFG, Tuya, Clock as TClock
    t, clock = Tuya(), TClock()
    hub = ChargeHub(tmp_path, Engine(KEY, Model(), lambda raw: None), t, clock=clock)
    cid = hub.add(CFG)
    m = Model()
    m.add_card("charger")
    name = next(iter(m.cards))
    m.set_card(name, charger=f"k:{cid}")
    live = cards.Live(m, tuya=t, charge=hub)
    r = live.value(f"kk.{name}__run", "nl")
    assert (r["kind"], r["value"]) == ("button", "Start") and live.value(f"kk.{name}__amps", "nl") == {"kind": "text", "value": "—"}
    assert hub.view(cid, None).get("ctl") is None and "amps" not in hub.view(cid, None)


# ------------------------------------------------------------ het geheel: de echte EspDevHub en een nagebootste Smappee


def test_a_badge_session_from_the_cloud_ends_up_in_the_log_and_a_pause_reaches_the_cloud(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(eh, "SMAPPEE_POLL", 0.1)
        monkeypatch.setattr(eh, "SMAPPEE_BURST", (0.05, 0.1))
        monkeypatch.setattr(eh, "SMAPPEE_SESS_ACTIVE", 0.1)
        monkeypatch.setattr(eh, "SMAPPEE_SESS_IDLE", 0.1)
        monkeypatch.setattr(eh, "SMAPPEE_WRITE_GAP", 0.0)
        monkeypatch.setattr(eh, "TICK", 3600)
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = await FakeSmappee().start()
        monkeypatch.setattr(smappee, "BASE", f"http://127.0.0.1:{fake.port}")
        aid = await hub.add_smappee(USERNAME, PASSWORD)
        (did,) = await hub.claim_smappee(aid)
        ch = ChargeHub(tmp_path, r.engine, None)
        ch.home, ch.peer_name, ch.espdev = lambda: "Atelier", lambda pid: pid, hub
        cid = ch.add({"name": "Oprit", "espdev": did, "price": 0.30, "tags": TAGS, "since": time.time() - 86400})
        end = time.time()
        fake.add_session(end - 7200, end - 3600, 10.0, tag=JORIS)  # gisteren badgede Joris
        fake.charging()
        fake.add_session(end - 600, None, 2.0, tag=PIET)  # nu laadt het huis van Piet
        end_t = time.monotonic() + 6
        while time.monotonic() < end_t and not (ch.log and cid in ch.active):
            ch.tick()
            await asyncio.sleep(0.05)
        assert [(e["name"], e["kwh"], e["cost"]) for e in ch.log] == [("Joris", 10.0, 3.0)] and ch.active[cid]["name"] == "Piet"
        v = ch.view(cid, None)
        assert (v["phase"], v["status"], v["charging"], v["act"], v["amps"]) == ("other", "charging", True, "pause", 19)
        await ch.control(cid, ch.who_self(), "pause")
        assert fake.actions[-1][0] == "pauseCharging"
        end_t = time.monotonic() + 6
        while time.monotonic() < end_t and ch.view(cid, None)["act"] != "resume":
            await asyncio.sleep(0.05)
        assert ch.view(cid, None)["act"] == "resume" and ch.view(cid, None)["status"] == "paused"
        with pytest.raises(ModelError, match="Slim of Zon|niet instelbaar|tussen"):
            await ch.control(cid, ch.who_self(), "current", 99)
        fake.sessions[1]["to"] = int((end - 100) * 1000)  # Piet is klaar
        end_t = time.monotonic() + 6
        while time.monotonic() < end_t and cid in ch.active:
            ch.tick()
            await asyncio.sleep(0.05)
        assert cid not in ch.active and [e["name"] for e in ch.log] == ["Joris", "Piet"] and ch.log[1]["by"] == "buren"
        hub.close()
        await fake.stop()
        await asyncio.sleep(0.05)
    asyncio.run(go())


def test_web_api_pauses_and_sets_the_current_on_the_owners_page(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from kkaddon import web
    from kkaddon.settings import Settings

    async def go():
        r = Rig(tmp_path)
        hub, esp, clock, cid = make(tmp_path)
        hub.engine = r.engine
        ctx = web.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), r.engine, charge=hub)
        c = TestClient(TestServer(web.make_app(ctx)))
        await c.start_server()
        try:
            assert (await c.post(f"/api/charge/{cid}/pause", json={})).status == 200
            assert (await c.post(f"/api/charge/{cid}/resume", json={})).status == 200
            assert (await c.post(f"/api/charge/{cid}/current", json={"amps": 14})).status == 200
            assert esp.sent == [("paused", True), ("paused", False), ("current", 14.0)]
            bad = await c.post(f"/api/charge/{cid}/current", json={"amps": "veel"})
            assert bad.status == 400 and "getal" in (await bad.json())["error"]
            assert (await c.post(f"/api/charge/{cid}/start", json={})).status == 400
            assert (await c.post(f"/api/charge/{cid}/stop", json={})).status == 400
            put = await c.put(f"/api/charge/{cid}", json={"tags": {"AB12": {"name": "Els", "by": ""}}})
            assert put.status == 200 and hub.chargers[cid]["tags"] == {"AB12": {"by": "", "name": "Els"}}
        finally:
            await c.close()
    asyncio.run(go())
