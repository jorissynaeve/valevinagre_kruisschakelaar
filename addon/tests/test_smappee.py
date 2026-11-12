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


async def rig(tmp_path, monkeypatch, fake=None, claim=True):
    r = Rig(tmp_path)
    hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
    fake = fake or FakeSmappee()
    await fake.start()
    monkeypatch.setattr(smappee, "BASE", f"http://127.0.0.1:{fake.port}")
    aid = await hub.add_smappee(USERNAME, PASSWORD)
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
           + [{"position": 0, "smartDevice": {"id": LED, "type": {"category": "LED"}}}, {"smartDevice": "x"}, "y", {"position": 3, "smartDevice": {"id": "nul", "type": {"category": "CARCHARGER"}}}]}
    assert [(c["id"], c["pos"], c["name"]) for c in smappee.connectors(det)] == [(str(DEVICE), 1, "Laadpunt 1"), (str(DEVICE2), 2, "Laadpunt 2")]
    assert smappee.connectors(None) == [] and smappee.connectors({"modules": "x"}) == []
    assert set(smappee.device_map([f.device(c) for c in f.conn.values()] + [{"id": LED, "type": {"category": "LED"}}, 5, {"id": 0, "type": {"category": "CARCHARGER"}}])) == {str(DEVICE), str(DEVICE2)}


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
    assert list(p) == ["state", "charging", "paused", "current", "session_kwh", "available"]
    assert [i for i, x in p.items() if x["writable"]] == ["paused", "current"] and not d["actions"], "alleen pauzeren en de stroom; geen start en stop"
    assert (p["current"]["min"], p["current"]["max"], p["current"]["step"], p["current"]["unit"], p["state"]["kind"], p["available"]["diag"]) == (6, 32, 1, "A", "enum", True)
    c = eh.clean_descriptor({"proto": "1.0", "rev": d["rev"], "device": {"id": "smappee-5001000042-8101"}, "points": [{**x, "class": x["cls"]} for x in d["points"]], "actions": []})
    assert len(c["points"]) == 6
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
        assert aid == smappee.account_id(USERNAME) and hub.smappee_accounts[aid] == {"name": "Thuis", "username": USERNAME, "password": PASSWORD}
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
