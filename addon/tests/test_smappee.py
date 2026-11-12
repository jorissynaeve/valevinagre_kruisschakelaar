"""Smappee-laadpalen via de cloud van Smappee als HUISDEV-apparaat (smappee.py en de Smappee-kant van espdevhub.py): de lijsten lezen, punten en waarden, een blok per laadpunt, pollen,
pauzeren en de stroom instellen, een sessie die verloopt, een onjuist wachtwoord (nooit steeds opnieuw proberen), te veel verzoeken, de entiteiten in HA, opslag en het schema.

Tegen een nagebootste Smappee-cloud over echte HTTP (tests/fakesmappee.py). **Dat is een nabootsing van de aanname in smappee.py**, gebouwd naar wat de Home Assistant-integratie
`smappee_ev` van de echte API leest en schrijft; de echte antwoorden zijn door ons niet gezien. Starten en stoppen bestaan hier niet: dat doe je met de badge aan de laadpaal."""
import asyncio
import json
import time

import pytest

from kkaddon import inventory, schema, smappee
from kkaddon import espdevhub as eh
from kkaddon.espdevhub import EspDevHub
from kkaddon.model import ModelError
from tests.fakesmappee import DEVICE, DEVICE2, LED, LOC, PASSWORD, SERIAL, SITE, USERNAME, FakeSmappee
from tests.rig import KEY, Rig

KEYS = f"{SERIAL}:{DEVICE}"
DID = f"smappee_{SERIAL}_{DEVICE}"


async def eventually(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("duurde te lang")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(eh, "SMAPPEE_POLL", 0.1)
    monkeypatch.setattr(eh, "SMAPPEE_BURST", (0.05, 0.1))
    monkeypatch.setattr(eh, "SMAPPEE_BACKOFF", 0.3)
    monkeypatch.setattr(eh, "SMAPPEE_AUTH_WAIT", 3600)
    monkeypatch.setattr(eh, "SMAPPEE_WRITE_GAP", 0.0)
    monkeypatch.setattr(eh, "SMAPPEE_SESS_ACTIVE", 0.1)
    monkeypatch.setattr(eh, "SMAPPEE_SESS_IDLE", 0.1)
    monkeypatch.setattr(eh, "TICK", 3600)


async def rig(tmp_path, monkeypatch, fake=None, claim=True, keys=False, budget=None):
    r = Rig(tmp_path)
    hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
    fake = fake or FakeSmappee()
    await fake.start()
    monkeypatch.setattr(smappee, "BASE", f"http://127.0.0.1:{fake.port}")
    monkeypatch.setattr(smappee, "PUB", f"http://127.0.0.1:{fake.port}")
    aid = await hub.add_smappee(USERNAME, PASSWORD, client_id=fake.client_id if keys else "", client_secret=fake.client_secret if keys else "", budget=budget)
    await eventually(lambda: hub.sm[aid]["online"])
    dids = await hub.claim_smappee(aid) if claim else []
    return r, hub, fake, aid, dids


async def shutdown(hub, *fakes):
    hub.close()
    for f in fakes:
        await f.stop()
    await asyncio.sleep(0.05)


def status(hub, did=DID):
    return next(x for x in hub.status() if x["id"] == did)


def unit_of(fake=None, did=DEVICE, **kw):
    fake = fake or FakeSmappee()
    if kw:
        fake.set_state(did, **kw)
    c = fake.conn[did]
    return {"id": str(did), "uuid": c["uuid"], "name": c["name"], "pos": c["pos"], "serial": SERIAL, "loc": str(LOC), "dev": fake.device(c), "station_available": True, "only": True}


# ------------------------------------------------------------ de lijsten lezen


def test_the_station_comes_from_the_location_with_a_charging_station_and_the_led_is_not_a_connector():
    f = FakeSmappee(connectors=2)
    locs = [{"id": SITE, "name": "Thuis"}, {"id": LOC, "parentId": SITE, "name": " Laadpaal ", "chargingStation": {"serialNumber": SERIAL}}, {"id": 5}, "x", None, {"id": True, "chargingStation": {"serialNumber": "A"}},
            {"id": 9, "chargingStation": {"serialNumber": "bad serial!"}}]
    assert smappee.stations(locs) == {SERIAL: {"name": "Laadpaal", "loc": str(LOC)}}
    assert smappee.stations([{"id": 9, "chargingStations": [{"serial": "AB12"}]}]) == {"AB12": {"name": "Smappee AB12", "loc": "9"}}
    assert smappee.stations(None) == {} and smappee.stations({}) == {}
    det = {"modules": [{"position": c["pos"], "smartDevice": {"id": c["id"], "uuid": c["uuid"], "name": c["name"], "type": {"category": "CARCHARGER"}}} for c in f.conn.values()]
           + [{"position": 0, "smartDevice": {"id": LED, "type": {"category": "LED"}}}, {"smartDevice": "x"}, "y", {"position": 3, "smartDevice": {"id": "met spatie/en!", "type": {"category": "CARCHARGER"}}}, {"position": 4, "smartDevice": {"id": 0, "type": {"category": "CARCHARGER"}}}]}
    assert [(c["id"], c["pos"], c["name"]) for c in smappee.connectors(det)] == [(str(DEVICE), 1, "Laadpunt 1"), (str(DEVICE2), 2, "Laadpunt 2")]
    assert smappee.connectors(None) == [] and smappee.connectors({"modules": "x"}) == []
    m = smappee.device_map([f.device(c) for c in f.conn.values()] + [{"id": LED, "type": {"category": "LED"}}, 5, None])
    assert {str(DEVICE), str(DEVICE2), str(LED), f.conn[DEVICE]["uuid"]} <= set(m) and m[str(DEVICE)]["uuid"] == f.conn[DEVICE]["uuid"] == m[f.conn[DEVICE]["uuid"]]["uuid"], "elk apparaat onder zijn id en zijn uuid; de lijst filtert niet op categorie"
    assert smappee.categories([{"type": {"category": "LED"}}, {"type": "CARCHARGER"}, {"id": 1}, 5]) == ["CARCHARGER", "LED", "zonder categorie"]


def test_a_typed_property_is_unwrapped_like_the_dashboard_sends_it():
    assert smappee.unwrap({"Quantity": {"value": 6, "unit": "A"}}) == 6 and smappee.unwrap({"Integer": 5}) == 5 and smappee.unwrap({"String": "x"}) == "x" and smappee.unwrap("ruw") == "ruw"
    assert smappee.unwrap({"iets": 1}) is None
    assert smappee.prop_value({"value": {"Integer": 7}}) == 7 and smappee.prop_value({"values": [{"Integer": 8}]}) == 8 and smappee.prop_value({"values": []}) is None and smappee.prop_value(5) is None


# ------------------------------------------------------------ de status van een laadpunt


@pytest.mark.parametrize("kw,state", [
    (dict(state="CHARGING", connection="CONNECTED", iec="C1"), smappee.S_CHARGING),
    (dict(state="STARTED"), smappee.S_CHARGING),
    (dict(state="PAUSED", mode="PAUSED"), smappee.S_PAUSED),
    (dict(state="SUSPENDED_EVSE"), smappee.S_PAUSED),
    (dict(state="IDLE", connection="CONNECTED"), smappee.S_CONNECTED),
    (dict(state="CHARGING_FINISHED", connection="CONNECTED"), smappee.S_CONNECTED),
    (dict(state="STOPPED", connection="DISCONNECTED", iec="A1"), smappee.S_FREE),
    (dict(state="IDLE", connection="", iec="B1"), smappee.S_CONNECTED),
    (dict(state="IDLE", connection="", iec="A1"), smappee.S_FREE),
])
def test_the_state_follows_the_chargers_own_words(kw, state):
    assert smappee.state_of(unit_of(**kw)["dev"]) == state


def test_an_unknown_state_is_unknown_not_a_guess():
    assert smappee.state_of(unit_of(state="IETS NIEUWS", connection="", iec="")["dev"]) is None
    assert smappee.state_of(unit_of(state="", connection="", iec="", mode="PAUSED")["dev"]) == smappee.S_PAUSED, "zonder laadstand beslist de modus"
    assert smappee.state_of({}) is None
    v = smappee.values(unit_of(state="IETS NIEUWS", connection="", iec=""))
    assert v["state"] is None and v["charging"] is None and v["paused"] is None


def test_the_current_is_a_percentage_of_the_range_and_reads_back_as_the_same_whole_amp():
    for lo, hi in ((6, 32), (6, 16), (10, 10), (0, 63)):
        for a in range(lo, hi + 1):
            pct = smappee.percentage_for(a, lo, hi)
            assert 0 <= pct <= 100
            u = unit_of(min=lo, max=hi, pct=pct)
            assert smappee.current_of(u["dev"]) == a, (lo, hi, a, pct)
    assert smappee.percentage_for(6, 6, 32) == 0 and smappee.percentage_for(32, 6, 32) == 100 and smappee.percentage_for(16, 6, 32) == 38
    assert smappee.current_range(unit_of(min=20, max=10)["dev"]) == (6, 32), "een onbruikbaar bereik geeft de standaard"
    assert smappee.current_range({}) == (6, 32)


def test_the_current_is_unknown_when_the_charger_runs_smart_or_solar():
    assert smappee.current_of(unit_of(pct=50)["dev"]) == 19
    for strategy in ("EXCESS_ONLY", "SCHEDULES_FIRST_THEN_EXCESS"):
        assert smappee.current_of(unit_of(pct=50, strategy=strategy)["dev"]) is None, "bij Slim of Zon beslist de lader zelf: dan zeggen we niets over een limiet"
    d = unit_of(pct=50)["dev"]
    d["carCharger"].pop("optimizationStrategy")
    assert smappee.current_of(d) == 19, "geeft de lijst geen strategie, dan nemen we aan dat de limiet geldt"
    assert smappee.current_of(unit_of(pct=250)["dev"]) is None and smappee.current_of({"properties": []}) is None


def test_the_points_are_valid_huisdev_points_and_only_two_are_writable():
    d = smappee.describe(unit_of())
    p = {x["id"]: x for x in d["points"]}
    assert list(p) == ["state", "charging", "paused", "current", "session_kwh", "power", "available"]
    assert [i for i, x in p.items() if x["writable"]] == ["paused", "current"] and not d["actions"], "alleen pauzeren en de stroom; geen start en stop"
    assert (p["current"]["min"], p["current"]["max"], p["current"]["step"], p["current"]["unit"], p["state"]["kind"], p["available"]["diag"]) == (6, 32, 1, "A", "enum", True)
    c = eh.clean_descriptor({"proto": "1.0", "rev": d["rev"], "device": {"id": "smappee-5001000042-8101"}, "points": [{**x, "class": x["cls"]} for x in d["points"]], "actions": []})
    assert len(c["points"]) == 7
    assert smappee.describe(unit_of(min=6, max=16))["rev"] != d["rev"], "een ander bereik is een andere descriptor"
    assert smappee.describe(unit_of())["rev"] == d["rev"] and d["name"] == "Laadpunt 1" and d["type"] == "Laadpaal"


# ------------------------------------------------------------ opdrachten


def test_pausing_a_charging_connector_sends_pause_and_resuming_sends_standard_mode():
    u = unit_of(state="CHARGING", iec="C1")
    m, path, body = smappee.write_request(u, "paused", True)
    assert (m, path, body) == ("POST", f"/api/v10/servicelocation/{LOC}/homecontrol/smart/devices/{DEVICE}/actions/pauseCharging", [])
    u = unit_of(state="PAUSED", mode="PAUSED")
    m, path, body = smappee.write_request(u, "paused", False)
    assert m == "POST" and path.endswith(f"/devices/{DEVICE}/actions/setChargingMode") and body[0]["values"] == [{"String": "STANDARD"}] and body[0]["spec"]["name"] == "mode"


def test_nothing_is_sent_when_it_is_already_so_because_resuming_would_reset_smart_mode():
    assert smappee.write_request(unit_of(state="PAUSED", mode="PAUSED"), "paused", True) is None
    assert smappee.write_request(unit_of(state="CHARGING", strategy="EXCESS_ONLY"), "paused", False) is None, "niet gepauzeerd: hervatten zou Zon op Standaard zetten"
    with pytest.raises(ValueError, match="onbekend"):
        smappee.write_request(unit_of(state="IETS", connection="", iec=""), "paused", False)
    assert smappee.write_request(unit_of(state="IETS", connection="", iec=""), "paused", True)[1].endswith("/pauseCharging"), "pauzeren mag altijd, ook als we de status niet kennen"


def test_the_current_becomes_a_percentage_and_is_checked_against_the_range():
    u = unit_of()
    m, path, body = smappee.write_request(u, "current", 16)
    assert path.endswith(f"/devices/{DEVICE}/actions/setPercentageLimit") and body == [{"spec": {"name": "percentageLimit", "species": "Integer", "unit": "%", "required": True}, "values": [{"Integer": 38}]}]
    assert smappee.write_request(u, "current", 6)[2][0]["values"] == [{"Integer": 0}] and smappee.write_request(u, "current", 32)[2][0]["values"] == [{"Integer": 100}]
    for bad in (5, 33, "veel", None, True, float("nan")):
        with pytest.raises(ValueError, match="tussen 6 en 32"):
            smappee.write_request(u, "current", bad)
    with pytest.raises(ValueError, match="Slim of Zon"):
        smappee.write_request(unit_of(strategy="EXCESS_ONLY"), "current", 16)
    for pid in ("state", "charging", "session_kwh", "nergens"):
        with pytest.raises(ValueError, match="niet instelbaar"):
            smappee.write_request(u, pid, 1)


def test_there_is_no_way_to_start_or_stop_from_here():
    names = {p["id"] for p in smappee.describe(unit_of())["points"]}
    assert not {"start", "stop", "charge"} & names and smappee.describe(unit_of())["actions"] == []
    for pid in ("start", "stop", "charging", "state"):
        with pytest.raises(ValueError):
            smappee.write_request(unit_of(), pid, True)


# ------------------------------------------------------------ sessies


def test_sessions_are_read_with_seconds_or_milliseconds_and_a_running_one_has_no_end():
    f = FakeSmappee()
    now = time.time()
    f.add_session(now - 7200, now - 3600, 11.5, tag="04AABBCC")
    f.add_session(now - 600, None, 2.25, tag="04DDEEFF", ms=False)  # in seconden en nog bezig
    f.add_session(now - 90000, now - 86000, 5, tag=None)
    s = smappee.sessions(f.sessions, unit_of(f), True)
    assert [x["tag"] for x in s] == ["04DDEEFF", "04AABBCC", ""] and [x["kwh"] for x in s] == [2.25, 11.5, 5]
    assert s[0]["end"] is None and s[1]["end"] - s[1]["start"] == pytest.approx(3600, abs=1) and s[0]["key"] == str(int(now - 600))
    assert smappee.sessions([{"energy": 3}, {"from": 0}, {"from": "x"}, 5, None, {"from": now * 1000, "to": (now - 5) * 1000, "energy": -1}], unit_of(f), True)[0]["end"] is None, "een einde voor het begin telt niet"
    assert smappee.sessions(None, unit_of(f), True) == [] and smappee.sessions({}, unit_of(f), True) == []


def test_with_two_connectors_a_session_is_only_placed_when_we_can_tell_where_it_belongs():
    f = FakeSmappee(connectors=2)
    now = time.time()
    f.add_session(now - 3000, now - 2000, 4, tag="A", did=DEVICE)
    f.add_session(now - 2000, now - 1000, 6, tag="B", did=DEVICE2)
    f.add_session(now - 1000, now - 500, 7, tag="C")  # niet te plaatsen
    u1, u2 = unit_of(f, DEVICE), unit_of(f, DEVICE2)
    u1["only"] = u2["only"] = False
    assert [x["tag"] for x in smappee.sessions(f.sessions, u1, False)] == ["A"] and [x["tag"] for x in smappee.sessions(f.sessions, u2, False)] == ["B"], "wat we niet kunnen plaatsen, blijft weg: nooit een gok"
    assert [x["tag"] for x in smappee.sessions(f.sessions, u1, True)] == ["C", "B", "A"], "bij een laadstation met een laadpunt hoort alles daarbij"
    by_number = [{"from": now * 1000 - 5000, "to": now * 1000, "energy": 1, "connectorNumber": 2}]
    assert smappee.sessions(by_number, u2, False) and not smappee.sessions(by_number, u1, False)


# ------------------------------------------------------------ inloggen


def test_login_gives_a_working_token_and_a_wrong_password_is_one_attempt(tmp_path, monkeypatch):
    async def go():
        import aiohttp
        f = await FakeSmappee().start()
        monkeypatch.setattr(smappee, "BASE", f"http://127.0.0.1:{f.port}")
        async with aiohttp.ClientSession() as s:
            token, ttl = await smappee.login(s, USERNAME, PASSWORD)
            assert 3000 < ttl <= 3600 and f.logins == 1
            st, data = await smappee.request(s, token, "GET", f"{smappee.API}/v11/user/servicelocations", params={"fullDetails": "true"})
            assert st == 200 and smappee.stations(data) == {SERIAL: {"name": "Laadpaal", "loc": str(LOC)}}
            with pytest.raises(smappee.SmappeeError) as e:
                await smappee.request(s, "nep-token", "GET", f"{smappee.API}/v11/user/servicelocations")
            assert e.value.code == "token"
            with pytest.raises(smappee.SmappeeError, match="gebruikersnaam of het wachtwoord") as e:
                await smappee.login(s, USERNAME, "fout")
            assert e.value.code == "auth" and f.logins == 2 and f.bad_logins == 1
            f.maintenance = True
            with pytest.raises(smappee.SmappeeError, match="onderhoud") as e:
                await smappee.login(s, USERNAME, PASSWORD)
            assert e.value.code == "maintenance"
            f.maintenance = False
            f.retry_after = "77"
            with pytest.raises(smappee.SmappeeError) as e:
                await smappee.request(s, token, "GET", f"{smappee.API}/v11/user/servicelocations")
            assert e.value.code == "rate" and e.value.wait == 77, "Retry-After van Smappee wordt gevolgd"
        await f.stop()
    asyncio.run(go())


# ------------------------------------------------------------ koppelen en volgen


def test_an_account_is_checked_by_logging_in_and_the_password_is_never_shown(tmp_path, monkeypatch):
    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = await FakeSmappee().start()
        monkeypatch.setattr(smappee, "BASE", f"http://127.0.0.1:{fake.port}")
        for bad in ("", "  "):
            with pytest.raises(ModelError, match="gebruikersnaam"):
                await hub.add_smappee(bad, "x")
        with pytest.raises(ModelError, match="gebruikersnaam"):
            await hub.add_smappee(USERNAME, "")
        with pytest.raises(ModelError, match="gebruikersnaam of het wachtwoord"):
            await hub.add_smappee(USERNAME, "fout")
        assert hub.smappee_accounts == {} and not (tmp_path / "espdev.json").exists() and fake.bad_logins == 1, "een wachtwoord dat niet werkt wordt niet bewaard, en maar een keer geprobeerd"
        aid = await hub.add_smappee(USERNAME, PASSWORD, name="Thuis")
        assert aid == smappee.account_id(USERNAME) and hub.smappee_accounts[aid] == {"name": "Thuis", "username": USERNAME, "password": PASSWORD, "client_id": "", "client_secret": "", "budget": eh.PUB_BUDGET}
        assert json.loads((tmp_path / "espdev.json").read_text(encoding="utf-8"))["smappee"][aid]["password"] == PASSWORD
        await eventually(lambda: hub.sm[aid]["online"])
        shown = json.dumps([hub.smappee_view(), hub.status(), r.engine.snapshot()["smappee"]])
        assert PASSWORD not in shown and USERNAME not in shown and [u["name"] for u in hub.smappee_view()[0]["units"]] == ["Laadpunt 1"]
        with pytest.raises(ModelError, match="staat er al"):
            await hub.add_smappee(USERNAME, PASSWORD)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_an_account_without_charging_stations_or_with_a_broken_site_is_refused(tmp_path, monkeypatch):
    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = await FakeSmappee().start()
        monkeypatch.setattr(smappee, "BASE", f"http://127.0.0.1:{fake.port}")
        fake.no_stations = True
        with pytest.raises(ModelError, match="geen laadpunten"):
            await hub.add_smappee(USERNAME, PASSWORD)
        fake.no_stations = False
        fake.api_status = 500
        with pytest.raises(ModelError, match="HTTP 500"):
            await hub.add_smappee(USERNAME, PASSWORD)
        fake.api_status = 0
        fake.maintenance = True
        with pytest.raises(ModelError, match="onderhoud"):
            await hub.add_smappee(USERNAME, PASSWORD)
        monkeypatch.setattr(smappee, "BASE", "http://127.0.0.1:9")
        with pytest.raises(ModelError, match="geen verbinding"):
            await hub.add_smappee(USERNAME, PASSWORD)
        assert hub.smappee_accounts == {}
        hub.close()
        await fake.stop()
    asyncio.run(go())


def test_claiming_makes_a_block_per_connector_and_not_for_the_led(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, FakeSmappee(connectors=2), claim=False)
        assert [u["key"] for u in hub.smappee_view()[0]["units"]] == [KEYS, f"{SERIAL}:{DEVICE2}"], "de led is geen blok"
        with pytest.raises(ModelError, match="kent dit laadpunt niet"):
            await hub.claim_smappee(aid, ["1:2"])
        new = await hub.claim_smappee(aid, [KEYS])
        assert new == [DID] and hub.devices[DID]["driver"] == "smappee" and (hub.devices[DID]["sid"], hub.devices[DID]["cid"]) == (SERIAL, str(DEVICE)) and hub.devices[DID]["ha"] is False
        assert len(await hub.claim_smappee(aid)) == 1, "de rest die er nog niet stond"
        with pytest.raises(ModelError, match="staat er al"):
            await hub.claim_smappee(aid)
        with pytest.raises(ModelError, match="niet bereikbaar"):
            await hub.claim_smappee("s_nergens")
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_block_shows_the_connector_and_follows_changes(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        ref = lambda pid: r.engine.ref_value(f"e:{DID}:{pid}")  # noqa: E731
        assert (ref("state"), ref("charging"), ref("paused"), ref("current"), ref("available")) == (smappee.S_CONNECTED, False, False, 19, True)
        s = status(hub)
        assert (s["driver"], s["online"], s["type"], s["host"], s["ha"], s["account"]) == ("smappee", True, "Laadpaal", "dashboard.smappee.net", False, aid)
        rev = hub.devices[DID]["rev"]
        fake.charging()
        await eventually(lambda: ref("state") == smappee.S_CHARGING and ref("charging") is True)
        fake.set_state(strategy="EXCESS_ONLY")  # iemand zette hem in de app op Zon
        await eventually(lambda: ref("current") is None)
        assert hub.devices[DID]["rev"] == rev and ref("charging") is True
        fake.set_state(state="IETS NIEUWS", connection="", iec="")
        await eventually(lambda: ref("state") is None and ref("charging") is None)
        assert fake.lists >= 3
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_last_session_shows_as_a_point_and_sessions_are_asked_more_often_while_charging(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(eh, "SMAPPEE_SESS_IDLE", 3600)
        monkeypatch.setattr(eh, "SMAPPEE_SESS_ACTIVE", 0.1)
        f = FakeSmappee()
        now = time.time()
        f.add_session(now - 7200, now - 3600, 11.5)
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, f)
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:session_kwh") == 11.5)
        calls = fake.session_calls
        await asyncio.sleep(0.4)
        assert fake.session_calls == calls, "niet aan het laden: de sessies worden niet steeds opnieuw gevraagd"
        fake.charging()
        await eventually(lambda: fake.session_calls > calls + 2)
        assert [x["key"] for x in hub.smappee_sessions(DID)] == [str(int(now - 7200))]
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_sessions_list_that_fails_does_not_take_the_connector_offline(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        fake.sessions_status = 500
        await asyncio.sleep(0.4)
        assert DID in hub.online and r.engine.ref_value(f"e:{DID}:state") == smappee.S_CONNECTED
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_connector_that_leaves_the_account_goes_offline(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, FakeSmappee(connectors=2))
        await eventually(lambda: DID in hub.online)
        del fake.conn[DEVICE]
        await eventually(lambda: DID not in hub.online)
        assert "niet meer in je Smappee-account" in status(hub)["error"] and r.engine.ref_value(f"e:{DID}:state") is None
        await shutdown(hub, fake)
    asyncio.run(go())


def test_offline_when_smappee_cannot_be_reached_and_back(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        port = fake.port
        await fake.stop()
        await eventually(lambda: DID not in hub.online and not hub.sm[aid]["online"])
        assert "geen verbinding" in status(hub)["error"] and r.engine.ref_value(f"e:{DID}:state") is None
        assert hub.smappee_sessions(DID) is None, "zonder verbinding geen sessies: het laadblok doet dan niets"
        await fake.start(port)
        await eventually(lambda: DID in hub.online, 8)
        assert r.engine.ref_value(f"e:{DID}:state") == smappee.S_CONNECTED and status(hub)["error"] == ""
        await shutdown(hub, fake)
    asyncio.run(go())


def test_too_many_requests_makes_the_account_wait_for_what_smappee_asks(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(eh, "SMAPPEE_BACKOFF", 0.05)
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        fake.retry_after = "1"
        await eventually(lambda: DID not in hub.online)
        assert "te veel verzoeken" in status(hub)["error"]
        n = fake.lists
        await asyncio.sleep(0.5)
        assert fake.lists == n, "Smappee vraagt een seconde wachten: dan niet doorvragen"
        fake.retry_after = ""
        await eventually(lambda: DID in hub.online, 4)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_token_that_expires_or_is_revoked_is_replaced_by_logging_in_again(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(eh, "SMAPPEE_REAUTH", 0)
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        n = fake.logins
        fake.revoke_tokens()  # de sessie is ingetrokken: de volgende ronde krijgt een 401 en logt een keer opnieuw in
        await eventually(lambda: fake.logins > n)
        fake.charging()
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:state") == smappee.S_CHARGING, 6)
        assert DID in hub.online and fake.bad_logins == 0
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_wrong_password_later_is_tried_once_then_left_alone_until_the_password_changes(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        fake.password = "nieuw-wachtwoord"  # het wachtwoord is bij Smappee gewijzigd
        fake.revoke_tokens()
        await eventually(lambda: DID not in hub.online and hub.smappee_view()[0]["auth"])
        assert "gebruikersnaam of het wachtwoord" in status(hub)["error"] and fake.bad_logins == 1
        await asyncio.sleep(0.6)
        assert fake.bad_logins == 1, "een onjuist wachtwoord probeert de add-on niet steeds opnieuw: een account kan daarop worden geblokkeerd"
        with pytest.raises(ModelError, match="onbekend account"):
            hub.update_smappee("s_nergens", "x")
        with pytest.raises(ModelError, match="nieuw wachtwoord"):
            hub.update_smappee(aid, "")
        hub.update_smappee(aid, "nieuw-wachtwoord")
        await eventually(lambda: DID in hub.online, 6)
        assert fake.bad_logins == 1 and not hub.smappee_view()[0]["auth"] and hub.smappee_accounts[aid]["password"] == "nieuw-wachtwoord"
        await shutdown(hub, fake)
    asyncio.run(go())


# ------------------------------------------------------------ bedienen


def test_pausing_sends_the_action_and_the_state_follows_from_a_quick_poll(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(eh, "SMAPPEE_POLL", 3600)  # alleen de snelle polls na de opdracht kunnen de stand brengen
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        fake.charging()
        hub._kick(f"smappee:{aid}")
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:state") == smappee.S_CHARGING)
        fake.apply_delay = 0.03
        res = await hub.set(DID, "paused", True)
        assert res == {"ok": True, "result": "queued"} and fake.actions == [("pauseCharging", DEVICE, [])]
        assert r.engine.ref_value(f"e:{DID}:paused") is False, "de stand komt uit wat de lader meldt, niet uit de opdracht"
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:paused") is True, 3)
        assert r.engine.ref_value(f"e:{DID}:charging") is False and r.engine.ref_value(f"e:{DID}:state") == smappee.S_PAUSED
        await hub.set(DID, "paused", False)
        assert fake.actions[-1][0] == "setChargingMode" and fake.actions[-1][2][0]["values"] == [{"String": "STANDARD"}]
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:paused") is False and r.engine.ref_value(f"e:{DID}:charging") is True, 3)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_second_pause_or_resume_that_changes_nothing_sends_nothing(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        res = await hub.set(DID, "paused", False)  # staat niet gepauzeerd
        assert res == {"ok": True, "result": "unchanged"} and fake.actions == []
        fake.set_state(state="PAUSED", mode="PAUSED")
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:paused") is True)
        assert (await hub.set(DID, "paused", True))["result"] == "unchanged" and fake.actions == []
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_current_is_sent_as_a_percentage_and_read_back_as_the_same_amp(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(eh, "SMAPPEE_POLL", 3600)
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:current") == 19)
        await hub.set(DID, "current", 10)
        assert fake.actions[-1][0] == "setPercentageLimit" and fake.actions[-1][2][0]["values"] == [{"Integer": 15}]
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:current") == 10, 3)
        assert fake.conn[DEVICE]["pct"] == 15
        await shutdown(hub, fake)
    asyncio.run(go())


def test_what_must_not_be_sent_is_refused_before_it_leaves(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        for pid, v, word in (("state", 1, "niet instelbaar"), ("charging", 1, "niet instelbaar"), ("session_kwh", 1, "niet instelbaar"), ("nergens", 1, "onbekend"),
                             ("current", 5, "tussen 6 en 32"), ("current", 33, "tussen 6 en 32"), ("current", "veel", "getal"), ("current", 10.5, "geheel getal"), ("paused", "misschien", "aan of uit")):
            with pytest.raises(ModelError, match=word):
                await hub.set(DID, pid, v)
        fake.set_state(strategy="EXCESS_ONLY")
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:current") is None)
        with pytest.raises(ModelError, match="Slim of Zon"):
            await hub.set(DID, "current", 16)
        assert fake.actions == []
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_command_smappee_refuses_or_cannot_get_is_reported(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        fake.charging()
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:charging") is True)
        fake.action_status = 403
        with pytest.raises(ModelError, match="weigert dit voor dit account"):
            await hub.set(DID, "paused", True)
        fake.action_status = 500
        with pytest.raises(ModelError, match="HTTP 500"):
            await hub.set(DID, "paused", True)
        assert r.engine.ref_value(f"e:{DID}:paused") is False, "de stand blijft wat de lader meldt"
        fake.action_status = 200
        fake.retry_after = "1"
        with pytest.raises(ModelError, match="te veel verzoeken"):
            await hub.set(DID, "paused", True)
        fake.retry_after = ""
        await eventually(lambda: DID in hub.online, 6)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_command_right_after_another_is_refused_to_spare_the_cloud(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(eh, "SMAPPEE_WRITE_GAP", 3600.0)
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        await hub.set(DID, "current", 10)
        with pytest.raises(ModelError, match="te snel"):
            await hub.set(DID, "current", 12)
        assert len(fake.actions) == 1
        fake.charging()
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:charging") is True)
        await hub.set(DID, "paused", True)  # een ander punt: dat mag meteen
        assert [a[0] for a in fake.actions] == ["setPercentageLimit", "pauseCharging"]
        await shutdown(hub, fake)
    asyncio.run(go())


def test_an_input_sets_the_current_only_on_a_change(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online and r.engine.ref_value(f"e:{DID}:current") == 19)
        amps = r.model.add_module("number", "Ampere", 1)
        with pytest.raises(ModelError, match="niet te koppelen"):
            hub.update(DID, {"inputs": {"state": f"m:{amps}"}})  # alleen-lezen
        hub.update(DID, {"inputs": {"current": f"m:{amps}"}})
        hub.tick()
        await asyncio.sleep(0.15)
        assert fake.actions == [], "de eerste stand wordt alleen onthouden: een herstart verandert de lader niet"
        r.model.set_module(amps, value=12)
        hub.tick()
        await eventually(lambda: len(fake.actions) == 1)
        assert fake.actions[0][0] == "setPercentageLimit" and fake.actions[0][2][0]["values"] == [{"Integer": 23}]
        for _ in range(3):
            hub.tick()
        await asyncio.sleep(0.15)
        assert len(fake.actions) == 1, "een ongewijzigde ingang stuurt niets opnieuw"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_block_on_the_schema_the_inventory_and_home_assistant(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        b = next(x for x in schema.view(r.model, r.engine.snapshot())["blocks"] if x["key"] == f"e:{DID}")
        assert (b["kind"], b["tech"], b["driver"], b["online"], b["account"]) == ("espdev", "Laadpaal · Smappee", "smappee", True, aid)
        assert [p["name"] for p in b["ports"]] == ["paused", "current"] and not any(p.get("action") for p in b["ports"]), "twee ingangen: de punten die je instelt, en geen actie"
        row = next(x for x in inventory.build(r.engine, None) if x["key"] == f"e{DID}")
        assert (row["name"], row["kind"], row["protocol"], row["ip"], row["online"]) == ("Laadpunt 1", "Smappee-laadpaal", "Smappee (cloud)", "dashboard.smappee.net", True)
        assert hub.discovery() == {} and hub.states() == {}, "standaard niets in HA"
        hub.update(DID, {"ha": True})
        disc = hub.discovery()
        base = f"kk_esp_{DID}"
        assert any(t.endswith(f"/switch/{base}_paused/config") for t in disc) and any(t.endswith(f"/number/{base}_current/config") for t in disc)
        assert next(iter(disc.values()))["device"]["manufacturer"] == "Smappee" and not any("start" in t or "stop" in t for t in disc)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_it_survives_a_restart_and_removal_takes_everything(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        hub.update(DID, {"name": "Laadpaal oprit", "ha": True})
        rev = hub.devices[DID]["rev"]
        hub.close()
        hub2 = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        assert hub2.smappee_accounts[aid]["password"] == PASSWORD and hub2.devices[DID]["driver"] == "smappee" and hub2.devices[DID]["name"] == "Laadpaal oprit"
        assert (hub2.devices[DID]["account"], hub2.devices[DID]["sid"], hub2.devices[DID]["cid"], hub2.devices[DID]["rev"]) == (aid, SERIAL, str(DEVICE), rev)
        hub2.start()
        await eventually(lambda: DID in hub2.online, 8)
        assert hub2.output(f"e:{DID}:current") == 19
        n = len(fake.actions)
        assert await hub2.remove(DID) and fake.actions[n:] == [] and DID not in hub2.devices
        assert [x["claimed"] for x in hub2.smappee_view()[0]["units"]] == [False]
        assert await hub2.remove_smappee(aid) and hub2.smappee_accounts == {} and hub2.devices == {} and hub2.smappee_view() == [] and f"smappee:{aid}" not in hub2.tasks
        assert json.loads((tmp_path / "espdev.json").read_text(encoding="utf-8"))["smappee"] == {} and not await hub2.remove_smappee(aid)
        await shutdown(hub2, fake)
    asyncio.run(go())


def test_a_damaged_file_does_not_stop_the_hub(tmp_path):
    (tmp_path / "espdev.json").write_text(json.dumps({"smappee": {"s_ok": {"username": "a@b.c", "password": "x", "name": "Thuis"}, "s_leeg": {"username": "", "password": "x"}, "Fout Id": {"username": "a@b.c", "password": "x"},
                                                       "s_nul": 5, "s_geen": {"username": "a@b.c"}},
                                                "devices": {"smappee_1_2": {"hid": "smappee-1-2", "driver": "smappee", "account": "s_ok", "sid": "geen serienummer!", "cid": "2"}}}), encoding="utf-8")
    r = Rig(tmp_path)
    hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
    assert list(hub.smappee_accounts) == ["s_ok"] and hub.devices["smappee_1_2"]["sid"] == "" and hub.devices["smappee_1_2"]["cid"] == "2"


def test_web_api_adds_claims_sets_and_removes(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from kkaddon import web
    from kkaddon.settings import Settings

    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = await FakeSmappee().start()
        monkeypatch.setattr(smappee, "BASE", f"http://127.0.0.1:{fake.port}")
        ctx = web.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), r.engine, espdev=hub)
        r.model.set_schema({**r.model.schema, "pages": [{"id": "hal", "name": "Hal"}]})
        c = TestClient(TestServer(web.make_app(ctx)))
        await c.start_server()
        try:
            bad = await c.post("/api/espdev/smappee/account", json={"username": USERNAME, "password": "fout"})
            assert bad.status == 400 and "wachtwoord" in (await bad.json())["error"]
            aid = (await (await c.post("/api/espdev/smappee/account", json={"username": USERNAME, "password": PASSWORD})).json())["id"]
            await eventually(lambda: hub.sm[aid]["online"])
            st = await (await c.get("/api/state")).json()
            assert [a["id"] for a in st["smappee"]] == [aid] and PASSWORD not in json.dumps(st) and USERNAME not in json.dumps(st)
            assert (await c.post("/api/espdev/smappee/claim", json={"account": aid, "keys": "ja"})).status == 400
            res = await (await c.post("/api/espdev/smappee/claim", json={"account": aid, "page": "hal", "x": 40, "y": 30})).json()
            assert res["keys"] == [f"e:{DID}"] and r.model.schema["blocks"][f"e:{DID}"]["page"] == "hal"
            await eventually(lambda: DID in hub.online)
            assert (await c.post(f"/api/espdev/{DID}/set", json={"point": "current", "value": 10})).status == 200 and fake.actions[-1][0] == "setPercentageLimit"
            assert (await c.post(f"/api/espdev/{DID}/set", json={"point": "current", "value": 99})).status == 400
            assert (await c.put(f"/api/espdev/{DID}", json={"host": "1.2.3.4"})).status == 400
            assert (await c.put(f"/api/espdev/smappee/account/{aid}", json={"name": "Thuis"})).status == 200 and hub.smappee_accounts[aid]["name"] == "Thuis"
            assert (await c.put("/api/espdev/smappee/account/s_nergens", json={"password": "x"})).status == 400
            assert (await c.delete(f"/api/espdev/smappee/account/{aid}")).status == 200 and hub.smappee_accounts == {} and hub.devices == {}
            assert f"e:{DID}" not in r.model.schema["blocks"] and (await c.delete(f"/api/espdev/smappee/account/{aid}")).status == 404
        finally:
            await c.close()
            await shutdown(hub, fake)
    asyncio.run(go())


def test_a_device_list_without_categories_still_gives_the_connectors(tmp_path, monkeypatch):
    """De lijst met smart devices hoeft zijn categorie niet te geven: welke apparaten laadpunten zijn, zegt het laadstation."""
    async def go():
        fake = FakeSmappee()
        orig = fake.device
        fake.device = lambda c: {k: v for k, v in orig(c).items() if k != "type"}
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, fake)
        await eventually(lambda: DID in hub.online)
        assert r.engine.ref_value(f"e:{DID}:current") == 19
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_device_can_be_found_by_its_uuid_when_the_id_does_not_match(tmp_path, monkeypatch):
    async def go():
        fake = FakeSmappee()
        orig = fake.device
        fake.device = lambda c: {**orig(c), "id": 99999}  # de lijst geeft een ander id, maar wel de uuid van het laadpunt
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, fake)
        await eventually(lambda: DID in hub.online)
        assert r.engine.ref_value(f"e:{DID}:state") == smappee.S_CONNECTED
        await shutdown(hub, fake)
    asyncio.run(go())


def test_when_nothing_is_found_the_message_says_where_the_search_stopped(tmp_path, monkeypatch):
    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = await FakeSmappee().start()
        monkeypatch.setattr(smappee, "BASE", f"http://127.0.0.1:{fake.port}")
        fake.no_stations = True  # de locaties hebben geen laadstation
        with pytest.raises(ModelError) as e:
            await hub.add_smappee(USERNAME, PASSWORD)
        msg = str(e.value)
        assert "Wat Smappee gaf" in msg and "1 locaties" in msg and "0 met een laadstation" in msg and "velden: " in msg and "id" in msg
        assert USERNAME not in msg and PASSWORD not in msg and SERIAL not in msg, "geen gegevens van het account in de melding"
        fake.no_stations = False
        fake.conn.clear()  # het station meldt geen laadpunten
        with pytest.raises(ModelError) as e:
            await hub.add_smappee(USERNAME, PASSWORD)
        assert "1 met een laadstation" in str(e.value) and f"station …{SERIAL[-4:]}: 1 modules" in str(e.value) and "0 laadpunten" in str(e.value) and SERIAL not in str(e.value)
        hub.close()
        await fake.stop()
    asyncio.run(go())


def test_the_connector_comes_from_the_device_list_when_the_station_modules_name_none(tmp_path, monkeypatch):
    """Bij een echt account meldde het laadstation 6 modules waar geen laadpunt uit kwam, terwijl de lijst met smart devices een CARCHARGER had."""
    async def go():
        fake = FakeSmappee()
        fake.bare_modules = True
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, fake)
        assert [u["key"] for u in hub.smappee_view()[0]["units"]] == [KEYS], "alleen de CARCHARGER, niet het laadstation (CHARGINGSTATION) of de led"
        await eventually(lambda: DID in hub.online)
        assert r.engine.ref_value(f"e:{DID}:current") == 19 and hub.devices[DID]["sid"] == SERIAL
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_fallback_picks_chargers_only_and_the_shape_message_has_no_values():
    f = FakeSmappee(connectors=2)
    devs = [{"id": 1, "type": {"category": "CHARGINGSTATION"}}] + [f.device(c) for c in f.conn.values()] + [{"id": LED, "type": {"category": "LED"}}, {"id": 0, "type": {"category": "CARCHARGER"}}, 5]
    assert [(c["id"], c["pos"], c["name"]) for c in smappee.connectors_from_devices(devs)] == [(str(DEVICE), 1, "Laadpunt 1"), (str(DEVICE2), 2, "Laadpunt 2")]
    assert smappee.connectors_from_devices(None) == []
    shape = smappee.module_shape({"modules": [{"position": 0, "label": "geheim"}, {"smartDevice": {"id": 5, "name": "geheim", "type": {"category": "LED"}}}]})
    assert shape == "2 modules (velden: label, position, smartDevice; 1 met smartDevice, categorieën: LED)" and "geheim" not in shape
    assert smappee.module_shape(None) == "geen lijst modules"


def test_a_device_id_as_text_works_too_and_an_unsafe_one_does_not():
    assert smappee.ident(8101) == "8101" and smappee.ident("8101") == "8101" and smappee.ident(" 3f2a-8101-b7 ") == "3f2a-8101-b7" and smappee.ident("ab_cd-1") == "ab_cd-1"
    for bad in (0, -5, None, True, "", "met spatie", "../x", "a/b", "a?b", 1.5, "x" * 65, {}):
        assert smappee.ident(bad) is None, bad
    det = {"modules": [{"position": 1, "smartDevice": {"id": "3f2a-8101-b7", "uuid": "u-1", "name": "Laadpunt", "type": {"category": "CARCHARGER"}}}]}
    assert smappee.connectors(det) == [{"id": "3f2a-8101-b7", "uuid": "u-1", "name": "Laadpunt", "pos": 1}]
    assert "id van een laadpunt: str(12 tekens)" in smappee.module_shape(det), "de melding zegt wat voor id het is, zonder het id zelf"
    assert "3f2a" not in smappee.module_shape(det)
    u = {**unit_of(), "id": "3f2a-8101-b7"}
    assert smappee.write_request({**u, "dev": unit_of(state="CHARGING")["dev"]}, "paused", True)[1].endswith("/devices/3f2a-8101-b7/actions/pauseCharging")
    assert smappee.hid_of(SERIAL, "3f2a-8101-b7") == "smappee-5001000042-3f2a-8101-b7" and eh.ID_RE.match(smappee.hid_of(SERIAL, "3f2a-8101-b7"))
    assert eh.ID_RE.match(smappee.hid_of(SERIAL, "x" * 60)), "een lang id wordt een hash"


def test_with_text_ids_the_whole_chain_works(tmp_path, monkeypatch):
    async def go():
        fake = FakeSmappee()
        fake.use_string_ids()
        fake.bare_modules = False
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, fake)
        (did,) = dids
        assert hub.devices[did]["cid"] == f"3f2a-{DEVICE}-b7" and hub.devices[did]["sid"] == SERIAL
        await eventually(lambda: did in hub.online)
        assert r.engine.ref_value(f"e:{did}:current") == 19
        fake.charging()
        await eventually(lambda: r.engine.ref_value(f"e:{did}:charging") is True)
        await hub.set(did, "paused", True)
        assert fake.actions == [("pauseCharging", DEVICE, [])]
        hub.close()
        hub2 = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        assert hub2.devices[did]["cid"] == f"3f2a-{DEVICE}-b7", "het id als tekst blijft bewaard na een herstart"
        await fake.stop()
    asyncio.run(go())


# ------------------------------------------------------------ verbruik en teller: de officiele API met een budget van 20 verzoeken per uur


def bucket(ts, active):
    return {"timestamp": ts, "active": active}


def test_the_meter_is_the_sum_of_all_phases_and_buckets_in_kwh_and_unknown_when_the_answer_is_odd():
    assert smappee.meter_kwh({"consumptions": [{"active": [1000, 2000, 500]}, {"active": [4000, 3000, 1500]}]}) == 12.0
    assert smappee.meter_kwh({"consumptions": [{"active": [1500.5]}]}) == 1.5 and smappee.meter_kwh({"consumptions": [{"active": [0, 0, 0]}]}) == 0.0
    for odd in (None, {}, {"consumptions": "x"}, {"consumptions": []}, {"consumptions": [{}]}, {"consumptions": [{"active": ["a", None, True]}]}, [], 5):
        assert smappee.meter_kwh(odd) is None, odd


def test_the_rate_is_the_newest_finished_five_minute_block_times_twelve_and_sleep_use_is_zero():
    now = 1_700_000_000_000
    ts = now - now % 300000
    cons = [bucket(ts - 900000, [10, 10, 10]), bucket(ts - 600000, [100, 50, 50]), bucket(ts - 300000, [200, 100, 100]), bucket(ts, [1, 0, 0])]
    assert smappee.rate_w({"consumptions": cons}, now) == 4800.0, "het nieuwste afgeronde blok (400 Wh x 12), niet het blok dat nog loopt"
    assert smappee.rate_w({"consumptions": [bucket(ts - 300000, [0.1, 0.1, 0.0])]}, now) == 0.0, "onder 0,3 Wh is het slaapverbruik"
    assert smappee.rate_w({"consumptions": [bucket(ts - 300000, [0.1, 0.1, 0.09])]}, now) == 0.0 and smappee.rate_w({"consumptions": [bucket(ts - 300000, [0.2, 0.1, 0.1])]}, now) == 4.8, "0,3 Wh telt zelf al mee (zoals `d < 0.3` bij JavaLox)"
    assert smappee.rate_w({"consumptions": [{"active": [10, 20, 30]}, {"active": [99, 99, 99]}]}, now) == 720.0, "zonder tijdstippen het eerste blok, zoals JavaLox"
    assert smappee.rate_w({"consumptions": [bucket(ts, [10, 10, 10])]}, now) is None, "alleen een blok dat nog loopt: onbekend"
    for odd in (None, {}, {"consumptions": []}, {"consumptions": [5]}, {"consumptions": [{"active": []}]}, {"consumptions": [{"active": ["x"]}]}, {"consumptions": [{"timestamp": 1}]}):
        assert smappee.rate_w(odd, now) is None, odd


def test_the_budget_counts_requests_in_the_last_hour_keeps_them_over_a_restart_and_never_exceeds_twenty(tmp_path):
    path = tmp_path / "smappee_budget.json"
    t = [1_700_000_000.0]
    b = smappee.Budget(path, "mijn-client-id", 14, clock=lambda: t[0])
    assert (b.limit, b.used(), b.free(), b.can(14), b.can(15)) == (14, 0, 14, True, False)
    for _ in range(14):
        b.spend()
    assert (b.used(), b.free(), b.can()) == (14, 0, False)
    assert "mijn-client-id" not in path.read_text(encoding="utf-8"), "het client-id staat als hash in het bestand"
    again = smappee.Budget(path, "mijn-client-id", 14, clock=lambda: t[0])
    assert again.used() == 14 and not again.can(), "een herstart vergeet het budget niet"
    other = smappee.Budget(path, "een-andere-client", 14, clock=lambda: t[0])
    assert other.used() == 0, "een andere client heeft een eigen telling"
    t[0] += 1800
    assert again.used() == 14, "na een half uur tellen ze nog mee"
    t[0] += 1801
    assert again.used() == 0 and again.can(14), "na een uur niet meer"
    assert smappee.Budget(path, "x", 99).limit == 20 and smappee.Budget(path, "x", 0).limit == 1, "nooit meer dan wat Smappee toelaat"
    path.write_text("kapot", encoding="utf-8")
    assert smappee.Budget(path, "x", 5).used() == 0, "een kapot bestand houdt niemand tegen"


def test_the_default_schedule_stays_well_under_twenty_requests_per_hour():
    worst = 3600 / eh.PUB_RATE_ACTIVE + 3600 / eh.PUB_METER_EVERY + 2  # laden de hele uur, plus een token of twee
    assert worst <= eh.PUB_BUDGET <= smappee.PUB_LIMIT == 20, worst
    assert eh.PUB_RATE_ACTIVE < eh.PUB_RATE_IDLE and eh.PUB_RETRY >= 300 and eh.PUB_HOLD_AUTH >= 3600


@pytest.fixture
def pubfast(monkeypatch):
    monkeypatch.setattr(eh, "PUB_RATE_ACTIVE", 0.15)
    monkeypatch.setattr(eh, "PUB_RATE_IDLE", 0.15)
    monkeypatch.setattr(eh, "PUB_METER_EVERY", 0.3)
    monkeypatch.setattr(eh, "PUB_RETRY", 0.2)
    monkeypatch.setattr(eh, "PUB_MIN_GAP", 0.0)
    monkeypatch.setattr(eh, "PUB_HOLD_RATE", 0.6)
    monkeypatch.setattr(eh, "PUB_HOLD_MAINT", 0.6)


def test_without_the_keys_there_are_no_points_and_not_one_request_to_the_official_api(tmp_path, monkeypatch, pubfast):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: dids[0] in hub.online)
        ids = {p["id"] for p in hub.devices[DID]["points"]}
        assert "power" in ids and "meter" not in ids, "het verbruik is geschat uit de sessie; de teller heeft de API nodig voor zijn beginstand"
        await asyncio.sleep(0.5)
        assert fake.pub_calls == [] and hub.smappee_view()[0]["pub"] == {"on": False, "error": "", "auth": False, "used": 0, "limit": 0, "detail": {}}
        await shutdown(hub, fake)
    asyncio.run(go())


def test_with_the_keys_the_block_gets_a_rate_in_watts_and_a_meter_in_kwh(tmp_path, monkeypatch, pubfast):
    async def go():
        fake = FakeSmappee()
        fake.charging()  # aan het laden en nog geen sessie: dan geldt het blok van de API
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, fake, keys=True)
        ref = lambda pid: r.engine.ref_value(f"e:{DID}:{pid}")  # noqa: E731
        await eventually(lambda: ref("power") == 2400.0 and ref("meter") == 12.0), "200 Wh in een afgerond blok = 2400 W; de teller is 12 kWh"
        p = {x["id"]: x for x in hub.devices[DID]["points"]}
        assert (p["power"]["unit"], p["power"]["cls"], p["power"]["state"], p["meter"]["unit"], p["meter"]["cls"], p["meter"]["state"]) == ("W", "power", "measurement", "kWh", "energy", "total_increasing")
        assert not p["power"]["writable"] and not p["meter"]["writable"]
        assert fake.pub_calls.count("token") == 1, "een token wordt hergebruikt zolang hij geldig is"
        fake.bucket_wh = [0.0, 0.0, 0.0]  # de laadpaal doet niets meer
        fake.year_buckets = [[1000.0, 2000.0, 500.0], [4000.0, 3000.0, 2500.0]]
        await eventually(lambda: ref("power") == 0.0 and ref("meter") == 13.0, 6)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_budget_is_never_exceeded_and_a_restart_does_not_forget_it(tmp_path, monkeypatch, pubfast):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, keys=True, budget=5)
        await asyncio.sleep(1.5)  # bij deze tijden zou er meer dan 5 keer gevraagd worden
        assert len(fake.pub_calls) == 5, fake.pub_calls
        assert hub.smappee_view()[0]["pub"]["used"] == 5 and hub.smappee_view()[0]["pub"]["limit"] == 5
        assert r.engine.ref_value(f"e:{DID}:meter") == 12.0, "wat al gelezen is blijft bruikbaar"
        hub.close()
        hub2 = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        hub2.start()
        await asyncio.sleep(1.0)
        assert len(fake.pub_calls) == 5, "na een herstart telt het verbruik van het afgelopen uur nog mee"
        await shutdown(hub2, fake)
    asyncio.run(go())


def test_too_many_requests_makes_the_add_on_wait_and_say_so(tmp_path, monkeypatch, pubfast):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, keys=True)
        await eventually(lambda: "rate" in fake.pub_calls and "meter" in fake.pub_calls)
        fake.pub_retry_after = "1"
        await eventually(lambda: "te veel verzoeken" in hub.smappee_view()[0]["pub"]["error"], 6)
        n = len(fake.pub_calls)
        await asyncio.sleep(0.4)
        assert len(fake.pub_calls) <= n + 1, "niet doorvragen na een 429"
        fake.pub_retry_after = ""
        await eventually(lambda: hub.smappee_view()[0]["pub"]["error"] == "", 8)
        assert DID in hub.online, "het dashboard ging gewoon door"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_wrong_keys_are_refused_at_once_and_later_ones_are_not_retried(tmp_path, monkeypatch, pubfast):
    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = await FakeSmappee().start()
        monkeypatch.setattr(smappee, "BASE", f"http://127.0.0.1:{fake.port}")
        monkeypatch.setattr(smappee, "PUB", f"http://127.0.0.1:{fake.port}")
        with pytest.raises(ModelError, match="zowel de client-id als het secret"):
            await hub.add_smappee(USERNAME, PASSWORD, client_id="cid-123")
        with pytest.raises(ModelError, match="budget"):
            await hub.add_smappee(USERNAME, PASSWORD, client_id="cid-123", client_secret="sec-456", budget=21)
        with pytest.raises(ModelError, match="verbruik en teller: .*client-id"):
            await hub.add_smappee(USERNAME, PASSWORD, client_id="cid-123", client_secret="fout")
        assert hub.smappee_accounts == {} and fake.pub_calls == ["token"], "een keer geprobeerd en niet bewaard"
        aid = await hub.add_smappee(USERNAME, PASSWORD, client_id="cid-123", client_secret="sec-456")
        await hub.claim_smappee(aid)
        await eventually(lambda: "rate" in fake.pub_calls and "meter" in fake.pub_calls)
        fake.client_secret = "veranderd"
        fake.revoke_pub_tokens()
        await eventually(lambda: hub.smappee_view()[0]["pub"]["auth"], 6)
        n = fake.pub_calls.count("token")
        await asyncio.sleep(0.8)
        assert fake.pub_calls.count("token") == n, "onjuiste sleutels worden niet steeds opnieuw geprobeerd"
        assert "weigert" in hub.smappee_view()[0]["pub"]["error"] and DID in hub.online, "het dashboard blijft werken"
        hub.close()
        await fake.stop()
    asyncio.run(go())


def test_a_token_that_expires_is_replaced_once(tmp_path, monkeypatch, pubfast):
    async def go():
        fake = FakeSmappee()
        fake.charging()
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, fake, keys=True)
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:power") == 2400.0)
        n = fake.pub_calls.count("token")
        fake.revoke_pub_tokens()
        await eventually(lambda: fake.pub_calls.count("token") > n, 6)
        fake.bucket_wh = [10.0, 10.0, 10.0]
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:power") == 360.0, 6)
        assert fake.pub_calls.count("token") == n + 1 and not hub.smappee_view()[0]["pub"]["error"]
        await shutdown(hub, fake)
    asyncio.run(go())


def test_with_two_connectors_the_rate_and_meter_stay_unknown(tmp_path, monkeypatch, pubfast):
    async def go():
        fake = FakeSmappee(connectors=2)
        fake.charging(DEVICE)
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, fake, keys=True)
        await eventually(lambda: len(fake.pub_calls) >= 3)
        await asyncio.sleep(0.3)
        by = {hub.devices[d]["cid"]: d for d in dids}
        assert all(r.engine.ref_value(f"e:{d}:meter") is None for d in dids) and r.engine.ref_value(f"e:{by[str(DEVICE)]}:power") is None, "het verbruik van de API geldt voor het hele laadstation: niet aan een laadpunt toe te wijzen"
        assert r.engine.ref_value(f"e:{by[str(DEVICE2)]}:power") == 0.0, "een laadpunt dat niet laadt gebruikt niets, ook naast een dat wel laadt"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_rate_that_is_too_old_is_unknown(tmp_path, monkeypatch, pubfast):
    async def go():
        monkeypatch.setattr(eh, "PUB_STALE_RATE", 0.5)
        fake = FakeSmappee()
        fake.charging()
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, fake, keys=True, budget=3)  # token, tarief en teller; daarna geen budget meer
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:power") == 2400.0)
        assert r.engine.ref_value(f"e:{DID}:meter") == 12.0
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:power") is None, 4)
        assert r.engine.ref_value(f"e:{DID}:meter") == 12.0, "de teller mag ouder zijn"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_meter_is_read_again_when_a_session_has_just_ended(tmp_path, monkeypatch, pubfast):
    async def go():
        monkeypatch.setattr(eh, "PUB_METER_EVERY", 3600.0)
        monkeypatch.setattr(eh, "PUB_RATE_IDLE", 3600.0)
        monkeypatch.setattr(eh, "PUB_RATE_ACTIVE", 3600.0)
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, keys=True)
        fake.charging()
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:charging") is True)
        await eventually(lambda: fake.pub_calls.count("meter") >= 1)
        n = fake.pub_calls.count("meter")
        fake.year_buckets = [[1000.0, 2000.0, 500.0], [4000.0, 3000.0, 3500.0]]
        fake.set_state(state="IDLE", iec="B1")  # klaar met laden
        await eventually(lambda: fake.pub_calls.count("meter") == n + 1, 6)
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:meter") == 14.0, 4)
        await asyncio.sleep(0.4)
        assert fake.pub_calls.count("meter") == n + 1, "een keer, niet steeds"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_keys_never_leave_the_add_on_and_can_be_changed_or_removed(tmp_path, monkeypatch, pubfast):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, keys=True)
        saved = json.loads((tmp_path / "espdev.json").read_text(encoding="utf-8"))["smappee"][aid]
        assert (saved["client_id"], saved["client_secret"], saved["budget"]) == ("cid-123", "sec-456", eh.PUB_BUDGET)
        shown = json.dumps([hub.smappee_view(), hub.status(), r.engine.snapshot()["smappee"]])
        assert "sec-456" not in shown and "cid-123" not in shown and PASSWORD not in shown
        with pytest.raises(ModelError, match="zowel de client-id"):
            hub.update_smappee(aid, "", client_id="x", client_secret="")
        with pytest.raises(ModelError, match="budget"):
            hub.update_smappee(aid, "", budget=0)
        with pytest.raises(ModelError, match="geef een nieuw"):
            hub.update_smappee(aid, "")
        hub.update_smappee(aid, "", budget=7)
        assert hub.smappee_accounts[aid]["budget"] == 7 and hub.smappee_view()[0]["pub"]["limit"] == 7
        hub.update_smappee(aid, "", client_id="", client_secret="")  # verbruik en teller weg
        assert hub.smappee_accounts[aid]["client_id"] == "" and hub.smappee_view()[0]["pub"]["on"] is False
        await eventually(lambda: "meter" not in {p["id"] for p in hub.devices[DID]["points"] if not p.get("gone")}, 4)
        n = len(fake.pub_calls)
        await asyncio.sleep(0.5)
        assert len(fake.pub_calls) == n, "zonder sleutels geen verzoeken meer"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_keys_and_the_budget_can_be_set_over_the_web_api(tmp_path, monkeypatch, pubfast):
    from aiohttp.test_utils import TestClient, TestServer
    from kkaddon import web
    from kkaddon.settings import Settings

    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = await FakeSmappee().start()
        monkeypatch.setattr(smappee, "BASE", f"http://127.0.0.1:{fake.port}")
        monkeypatch.setattr(smappee, "PUB", f"http://127.0.0.1:{fake.port}")
        ctx = web.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), r.engine, espdev=hub)
        c = TestClient(TestServer(web.make_app(ctx)))
        await c.start_server()
        try:
            bad = await c.post("/api/espdev/smappee/account", json={"username": USERNAME, "password": PASSWORD, "client_id": "cid-123", "client_secret": "fout"})
            assert bad.status == 400 and "client-id" in (await bad.json())["error"]
            aid = (await (await c.post("/api/espdev/smappee/account", json={"username": USERNAME, "password": PASSWORD, "client_id": "cid-123", "client_secret": "sec-456", "budget": 10})).json())["id"]
            assert hub.smappee_accounts[aid]["budget"] == 10 and hub.smappee_accounts[aid]["client_id"] == "cid-123"
            st = await (await c.get("/api/state")).json()
            assert st["smappee"][0]["pub"]["on"] is True and "sec-456" not in json.dumps(st)
            assert (await c.put(f"/api/espdev/smappee/account/{aid}", json={"budget": 12})).status == 200 and hub.smappee_accounts[aid]["budget"] == 12
            assert (await c.put(f"/api/espdev/smappee/account/{aid}", json={"budget": 25})).status == 400
            assert (await c.put(f"/api/espdev/smappee/account/{aid}", json={"client_id": "nieuw", "client_secret": "geheim"})).status == 200 and hub.smappee_accounts[aid]["client_id"] == "nieuw"
        finally:
            await c.close()
            await shutdown(hub, fake)
    asyncio.run(go())


# ------------------------------------------------------------ verbruik en teller geschat uit de sessie, zonder verzoeken


def test_the_power_is_estimated_from_how_the_session_energy_grows():
    ev = []
    for i in range(0, 600, 60):  # elke minuut een waarde, 11 kW: 0,1833 kWh per minuut
        ev = smappee.note_energy(ev, 1000.0 + i, round(i / 3600 * 11, 4))
    assert len(ev) == 8, "de laatste acht veranderingen"
    p = smappee.virtual_power(ev, 1000.0 + 540)
    assert p is not None and abs(p - 11000) < 50
    assert smappee.virtual_power(ev, 1000.0 + 540 + 300) is None, "de laatste verandering is te oud: de energie groeit niet meer, dan weten we het vermogen niet"
    assert smappee.virtual_power(ev[:1], 1000.0) is None and smappee.virtual_power([], 5.0) is None
    assert smappee.virtual_power([(1000.0, 1.0), (1060.0, 1.2)], 1060.0) is None, "minder dan twee minuten tussen de veranderingen: te grof"
    assert smappee.virtual_power([(1000.0, 1.0), (1200.0, 1.0)], 1200.0) is None and smappee.virtual_power([(1000.0, 1.2), (1200.0, 1.0)], 1200.0) is None, "geen groei: onbekend, niet 0"


def test_a_value_that_does_not_change_adds_nothing_and_lumpy_updates_still_give_the_right_power():
    ev = smappee.note_energy([], 0.0, 1.0)
    assert smappee.note_energy(ev, 60.0, 1.0) == ev
    ev = []
    for i in range(0, 1200, 60):  # Smappee werkt de energie maar elke 5 minuten bij; wij zien het bij de eerstvolgende poll
        k = round((i // 300) * 300 / 3600 * 7.4, 4)
        ev = smappee.note_energy(ev, float(i), k)
    p = smappee.virtual_power(ev, 1140.0)
    assert p is not None and abs(p - 7400) < 200, "de tijdstippen waarop een waarde voor het eerst gezien is, vallen even ver uiteen als de waarden zelf"


def test_the_meter_is_the_api_value_plus_what_the_sessions_added_since():
    base = {"wall": 1000.0, "key": "k1", "kwh": 2.0}
    s = lambda key, start, kwh: {"key": key, "start": start, "end": None, "kwh": kwh, "tag": ""}  # noqa: E731
    assert smappee.virtual_meter(100.0, base, [s("k1", 500.0, 5.5)]) == 103.5, "de sessie die liep: alleen wat erbij kwam"
    assert smappee.virtual_meter(100.0, base, [s("k2", 1500.0, 4.0), s("k1", 500.0, 2.0)]) == 104.0, "een sessie die na de lezing begon telt helemaal mee"
    assert smappee.virtual_meter(100.0, base, [s("k0", 100.0, 9.0)]) == 100.0, "een sessie van voor de lezing zit al in de waarde"
    assert smappee.virtual_meter(100.0, {"wall": 1000.0, "key": None, "kwh": 0.0}, [s("k2", 1500.0, 3.0)]) == 103.0
    assert smappee.virtual_meter(100.0, base, [s("k1", 500.0, None)]) is None and smappee.virtual_meter(100.0, base, [s("k2", 1500.0, None)]) is None, "een sessie zonder kWh die meetelt: niet te weten"
    assert smappee.virtual_meter(100.0, base, [s("k1", 500.0, 1.0)]) == 100.0, "daalt de energie, dan telt het niet negatief"
    assert smappee.virtual_meter(None, base, []) is None and smappee.virtual_meter(100.0, None, []) is None


def test_without_a_charge_the_power_is_zero_without_any_request(tmp_path, monkeypatch, pubfast):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)  # geen sleutels
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:power") == 0.0)
        for kw in (dict(state="PAUSED", mode="PAUSED"), dict(state="IDLE", mode="NORMAL"), dict(state="STOPPED", connection="DISCONNECTED", iec="A1")):
            fake.set_state(**kw)
            await eventually(lambda: r.engine.ref_value(f"e:{DID}:state") is not None)
            await asyncio.sleep(0.25)
            assert r.engine.ref_value(f"e:{DID}:power") == 0.0, kw
        fake.set_state(state="IETS NIEUWS", connection="", iec="")
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:state") is None)
        assert r.engine.ref_value(f"e:{DID}:power") is None, "een onbekende status en geen API: onbekend, geen 0"
        assert fake.pub_calls == []
        await shutdown(hub, fake)
    asyncio.run(go())


def test_while_charging_the_power_follows_the_session_energy_without_the_api(tmp_path, monkeypatch, pubfast):
    async def go():
        monkeypatch.setattr(smappee, "VP_MIN_SPAN", 0.3)
        monkeypatch.setattr(smappee, "VP_MAX_AGE", 2.0)
        fake = FakeSmappee()
        fake.charging()
        fake.add_session(time.time() - 2, None, 0.0, tag="04AABBCC")
        fake.live_kw = 11.0  # de energie van de lopende sessie groeit met 11 kW
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, fake)  # geen sleutels
        await eventually(lambda: (r.engine.ref_value(f"e:{DID}:power") or 0) > 0, 8)
        await asyncio.sleep(0.6)
        p = r.engine.ref_value(f"e:{DID}:power")
        assert p is not None and 8000 < p < 14000, p
        assert fake.pub_calls == [], "geen verzoeken aan de officiele API nodig"
        fake.live_kw = 0.0  # de energie groeit niet meer, maar de status zegt nog laden: dan weten we het niet
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:power") is None, 8)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_while_charging_the_meter_grows_between_two_reads_of_the_api(tmp_path, monkeypatch, pubfast):
    async def go():
        monkeypatch.setattr(eh, "PUB_METER_EVERY", 3600.0)
        monkeypatch.setattr(eh, "PUB_RATE_ACTIVE", 3600.0)
        monkeypatch.setattr(eh, "PUB_RATE_IDLE", 3600.0)
        fake = FakeSmappee()
        fake.charging()
        fake.add_session(time.time() - 2, None, 0.0, tag="04AABBCC")
        fake.live_kw = 36.0  # 0,01 kWh per seconde
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, fake, keys=True)
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:meter") is not None)
        first = r.engine.ref_value(f"e:{DID}:meter")
        await asyncio.sleep(1.0)
        later = r.engine.ref_value(f"e:{DID}:meter")
        assert fake.pub_calls.count("meter") == 1, "de API is niet opnieuw gelezen"
        assert later > first + 0.005, (first, later)  # ongeveer 0,01 kWh per seconde erbij
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_meter_counts_only_the_first_three_values_of_active_like_javalox():
    """JavaLox telt per blok `active.get(0..2)`; wat daarna in de lijst staat telt niet mee."""
    data = {"consumptions": [{"active": [1000, 2000, 500, 9_000_000, 9_000_000]}, {"active": [4000, 3000, 1500, 7_000_000]}]}
    assert smappee.meter_kwh(data) == 12.0
    sh = smappee.shape_of(data, 1_700_000_000_000)
    assert sh["buckets"] == 2 and sh["active_len"] == 5 and sh["kwh_first3"] == 12.0 and sh["kwh_all"] == 25012.0, "de diagnose laat het verschil zien"
    assert sh["newest_age_s"] is None
    assert smappee.shape_of({"consumptions": [{"timestamp": 1_699_999_940_000, "active": [1, 2, 3]}]}, 1_700_000_000_000)["newest_age_s"] == 60
    assert smappee.shape_of(None, 0) == {"buckets": None} and smappee.shape_of({"consumptions": [5, {}]}, 0)["active_len"] == 0


def test_the_diagnosis_gives_raw_words_and_numbers_but_no_ids_names_or_badges():
    u = unit_of(state="CHARGING", iec="C1", pct=40)
    d = smappee.diagnose(u, [{"key": "1", "start": 1000.0, "end": None, "kwh": 2.5, "tag": "04AABBCC"}, {"key": "0", "start": 100.0, "end": 900.0, "kwh": None, "tag": ""}], [(10.0, 1.0), (70.0, 1.2)], 100.0, 1600.0)
    assert d == {"chargingState": "CHARGING", "connection": "CONNECTED", "iec": "C1", "mode": "NORMAL", "strategy": "NONE", "pct": 40, "state": smappee.S_CHARGING,
                 "sessions": [{"ago_min": 10, "running": True, "kwh": 2.5, "badge": True}, {"ago_min": 25, "running": False, "kwh": None, "badge": False}], "events": [{"ago_s": 90, "kwh": 1.0}, {"ago_s": 30, "kwh": 1.2}]}
    assert "04AABBCC" not in json.dumps(d) and str(DEVICE) not in json.dumps(d) and "Laadpunt" not in json.dumps(d)
    assert smappee.diagnose({"dev": {}}, [], [], 0.0, 0.0)["state"] is None


def test_the_view_shows_what_smappee_said_and_where_the_meter_comes_from(tmp_path, monkeypatch, pubfast):
    async def go():
        fake = FakeSmappee()
        fake.charging()
        fake.add_session(time.time() - 120, None, 1.5, tag="04AABBCC")
        fake.year_buckets = [[1000.0, 2000.0, 500.0, 99999.0], [4000.0, 3000.0, 1500.0, 99999.0]]
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, fake, keys=True)
        await eventually(lambda: hub.smappee_view()[0]["pub"]["detail"].get("meter_api") == 12.0 and hub.smappee_view()[0]["pub"]["detail"].get("rate_api") is not None)
        v = hub.smappee_view()[0]
        d = v["pub"]["detail"]
        assert d["meter"]["active_len"] == 4 and d["meter"]["kwh_first3"] == 12.0 and d["meter"]["kwh_all"] == 211.998 and d["rate"]["buckets"] == 4
        assert d["meter_shown"] >= 12.0 and d["rate_api"] == 2400.0
        u = v["units"][0]["diag"]
        assert u["chargingState"] == "CHARGING" and u["sessions"][0]["running"] is True and u["sessions"][0]["badge"] is True
        shown = json.dumps([v, r.engine.snapshot()["smappee"]])
        assert "04AABBCC" not in shown and "sec-456" not in shown and "cid-123" not in shown and PASSWORD not in shown
        await shutdown(hub, fake)
    asyncio.run(go())
