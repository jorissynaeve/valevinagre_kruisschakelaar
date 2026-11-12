"""Nuki-sloten via de Nuki Web API als HUISDEV-apparaat (nuki.py en de Nuki-kant van espdevhub.py): de lijst lezen, punten en waarden, een token koppelen, een blok per slot,
pollen, de opdrachten (en dat wat een deur opent alleen met de hand en met bevestiging kan), de foutmeldingen van Nuki, de entiteiten in HA, opslag en het schema.
Tegen een nagebootste API over echte HTTP (tests/fakenuki.py), gebouwd naar het antwoord van een echt slot."""
import asyncio
import json
import time

import pytest

from kkaddon import espdevhub as eh
from kkaddon import inventory, nuki, schema
from kkaddon.espdevhub import EspDevHub
from kkaddon.model import ModelError
from tests.fakenuki import TOKEN, FakeNuki, house, lock
from tests.rig import KEY, Rig

A, B = "nuki_100000001", "nuki_100000002"  # Voordeur, Garage


async def eventually(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("duurde te lang")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(eh, "NUKI_POLL", 0.1)
    monkeypatch.setattr(eh, "NUKI_BURST", (0.05, 0.1))
    monkeypatch.setattr(eh, "NUKI_BACKOFF", 0.3)
    monkeypatch.setattr(eh, "ACTION_GAP", 0.1)
    monkeypatch.setattr(eh, "TICK", 3600)


async def rig(tmp_path, monkeypatch, fake=None, claim=True):
    r = Rig(tmp_path)
    hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
    fake = fake or house()
    await fake.start()
    monkeypatch.setattr(nuki, "BASE", f"http://127.0.0.1:{fake.port}")
    aid = await hub.add_nuki(fake.token)
    await eventually(lambda: hub.nk[aid]["online"])
    dids = await hub.claim_nuki(aid) if claim else []
    return r, hub, fake, aid, dids


async def shutdown(hub, *fakes):
    hub.close()
    for f in fakes:
        await f.stop()
    await asyncio.sleep(0.05)


def status(hub, did):
    return next(x for x in hub.status() if x["id"] == did)


# ------------------------------------------------------------ de lijst lezen


def test_the_points_follow_what_the_lock_reports():
    d = nuki.describe(lock(lid=1, door=0, battery=80))
    assert [p["id"] for p in d["points"]] == ["state", "locked", "battery", "battery_low"], "zonder deursensor geen punt voor de deur"
    d2 = nuki.describe(lock(lid=1, door=2))
    assert [p["id"] for p in d2["points"]] == ["state", "locked", "door_open", "battery", "battery_low"] and d2["rev"] != d["rev"], "een deursensor die erbij komt is een andere beschrijving"
    old = lock(lid=1)
    del old["state"]["batteryCharge"]  # een ouder slot meldt alleen of de batterij kritiek is
    assert [p["id"] for p in nuki.describe(old)["points"]] == ["state", "locked", "battery_low"]
    by = {p["id"]: p for p in d2["points"]}
    assert not any(p["writable"] for p in d2["points"]), "een slot stel je niet in via een punt: een timer mag een deur niet openen"
    assert (by["battery"]["cls"], by["battery"]["unit"]) == ("battery", "%") and by["state"]["options"][1] == {"v": 1, "l": "Vergrendeld"}
    assert d["type"] == "Nuki-slot" and d["id"] == "nuki-1" and d["name"] == "Voordeur", "het id komt van het smartlockId"


def test_every_point_is_a_valid_huisdev_point_and_action():
    d = nuki.describe(lock(lid=1, door=2))
    c = eh.clean_descriptor({"proto": "1.0", "rev": d["rev"], "device": {"id": d["id"]}, "points": [{**p, "class": p["cls"]} for p in d["points"]], "actions": d["actions"]})
    assert len(c["points"]) == len(d["points"]) == 5 and [a["id"] for a in c["actions"]] == ["lock", "unlock", "unlatch", "lockngo", "unlatch_lockngo"]


def test_what_opens_the_door_is_dangerous_and_locking_is_not():
    acts = {a["id"]: a for a in nuki.describe(lock(lid=1))["actions"]}
    assert acts["lock"]["danger"] is False and acts["lock"]["confirm"] == ""
    for a in ("unlock", "unlatch", "lockngo", "unlatch_lockngo"):
        assert acts[a]["danger"] is True and "Doorgaan" in acts[a]["confirm"], a


def test_values_are_unknown_while_the_lock_moves_and_for_states_we_do_not_know():
    v = lambda **st: nuki.values(lock(lid=1, **st))  # noqa: E731
    assert (v(state=1)["locked"], v(state=1)["state"]) == (True, 1)
    assert [v(state=s)["locked"] for s in (3, 5, 6)] == [False, False, False]
    assert [v(state=s)["locked"] for s in (0, 2, 4, 7, 253, 254, 255)] == [None] * 7, "ontgrendelen, vergrendelen en fouten zijn geen 'vergrendeld' en geen 'open'"
    assert v(state=2)["state"] == 2 and v(state=99)["state"] is None and v(state=99)["locked"] is None, "een stand die we niet kennen is onbekend, nooit een gok"
    assert (v(door=2)["door_open"], v(door=3)["door_open"], v(door=0)["door_open"], v(door=4)["door_open"], v(door=5)["door_open"]) == (False, True, None, None, None)
    assert (v(battery=14, critical=True)["battery"], v(battery=14, critical=True)["battery_low"]) == (14, True)
    for bad in ({"state": None}, {"state": "1"}, {"state": True}):
        assert nuki.values({"smartlockId": 1, "state": bad})["state"] is None, bad
    assert nuki.values({"smartlockId": 1})["locked"] is None


def test_the_lock_list_ignores_what_is_not_a_lock():
    assert list(nuki.locks([lock(lid=5), {"name": "geen id"}, {"smartlockId": "x"}, 7, None, {"smartlockId": True}])) == ["5"]
    assert nuki.locks({"fout": 1}) == {} and nuki.locks(None) == {}
    assert nuki.account_id("abc") == nuki.account_id("abc") != nuki.account_id("abd") and "abc" not in nuki.account_id("abc")


def test_actions_use_nukis_codes():
    assert [(a, nuki.action_request("77", a)) for a in ("unlock", "lock", "unlatch", "lockngo", "unlatch_lockngo")] == [
        ("unlock", ("/smartlock/77/action", {"action": 1})), ("lock", ("/smartlock/77/action", {"action": 2})), ("unlatch", ("/smartlock/77/action", {"action": 3})),
        ("lockngo", ("/smartlock/77/action", {"action": 4})), ("unlatch_lockngo", ("/smartlock/77/action", {"action": 5}))]
    with pytest.raises(ValueError):
        nuki.action_request("77", "reboot")


# ------------------------------------------------------------ koppelen en volgen


def test_a_token_is_checked_before_it_is_kept_and_never_shown(tmp_path, monkeypatch):
    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = await house().start()
        monkeypatch.setattr(nuki, "BASE", f"http://127.0.0.1:{fake.port}")
        for bad in ("", "kort", "met spatie erin 12345"):
            with pytest.raises(ModelError, match="API-token"):
                await hub.add_nuki(bad)
        with pytest.raises(ModelError, match="token"):
            await hub.add_nuki("x" * 40)  # Nuki weigert hem (401)
        assert hub.accounts == {} and not (tmp_path / "espdev.json").exists(), "een token die niet werkt wordt niet bewaard"
        aid = await hub.add_nuki(TOKEN, name="Thuis")
        assert aid == nuki.account_id(TOKEN) and hub.accounts[aid] == {"name": "Thuis", "token": TOKEN}
        assert json.loads((tmp_path / "espdev.json").read_text(encoding="utf-8"))["nuki"][aid]["token"] == TOKEN
        await eventually(lambda: hub.nk[aid]["online"])
        shown = json.dumps([hub.nuki_view(), hub.status(), r.engine.snapshot()["nuki"]])
        assert TOKEN not in shown and [x["name"] for x in hub.nuki_view()[0]["locks"]] == ["Garage", "Voordeur"]
        with pytest.raises(ModelError, match="staat er al"):
            await hub.add_nuki(TOKEN)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_an_account_without_locks_is_refused(tmp_path, monkeypatch):
    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = await FakeNuki().start()
        monkeypatch.setattr(nuki, "BASE", f"http://127.0.0.1:{fake.port}")
        with pytest.raises(ModelError, match="geen enkel slot"):
            await hub.add_nuki(TOKEN)
        fake.list_status = 500
        with pytest.raises(ModelError, match="HTTP 500"):
            await hub.add_nuki(TOKEN)
        monkeypatch.setattr(nuki, "BASE", "http://127.0.0.1:9")  # er luistert niets
        with pytest.raises(ModelError, match="geen verbinding"):
            await hub.add_nuki(TOKEN)
        assert hub.accounts == {}
        hub.close()
        await fake.stop()
    asyncio.run(go())


def test_claiming_makes_a_block_per_lock(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, claim=False)
        assert not any(x["claimed"] for x in hub.nuki_view()[0]["locks"])
        new = await hub.claim_nuki(aid, ["100000001"])
        assert new == [A] and hub.devices[A]["driver"] == "nuki" and hub.devices[A]["lid"] == "100000001" and hub.devices[A]["ha"] is False
        with pytest.raises(ModelError, match="kent dit slot niet"):
            await hub.claim_nuki(aid, ["5"])
        rest = await hub.claim_nuki(aid)
        assert rest == [B] and set(hub.devices) == {A, B}
        with pytest.raises(ModelError, match="staat er al"):
            await hub.claim_nuki(aid)
        with pytest.raises(ModelError, match="niet bereikbaar"):
            await hub.claim_nuki("n_nergens")
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_blocks_show_the_state_of_the_lock(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: A in hub.online and B in hub.online)
        assert (r.engine.ref_value(f"e:{A}:locked"), r.engine.ref_value(f"e:{A}:state"), r.engine.ref_value(f"e:{A}:battery"), r.engine.ref_value(f"e:{A}:battery_low")) == (True, 1, 80, False)
        assert (r.engine.ref_value(f"e:{B}:locked"), r.engine.ref_value(f"e:{B}:door_open"), r.engine.ref_value(f"e:{B}:battery_low"), r.engine.ref_value(f"e:{B}:battery")) == (False, False, True, 14)
        assert r.engine.ref_value(f"e:{A}:door_open") is None, "geen deursensor: geen bron"
        s = status(hub, A)
        assert (s["driver"], s["online"], s["type"], s["host"], s["ha"], s["account"]) == ("nuki", True, "Nuki-slot", "api.nuki.io", False, aid)
        assert [a["id"] for a in s["actions"]] == ["lock", "unlock", "unlatch", "lockngo", "unlatch_lockngo"]
        assert fake.lists >= 1
        await shutdown(hub, fake)
    asyncio.run(go())


def test_one_request_serves_all_locks_of_an_account(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(eh, "NUKI_POLL", 0.2)
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        n, t0 = fake.lists, time.monotonic()
        await asyncio.sleep(1.0)
        per_second = (fake.lists - n) / (time.monotonic() - t0)
        assert per_second < 8, "een lijst voor alle sloten, geen verzoek per slot: Nuki laat 4 per seconde toe"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_change_by_hand_arrives_with_the_next_poll(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: A in hub.online)
        fake.set_state(100000001, state=3)  # iemand draait aan de knop
        await eventually(lambda: r.engine.ref_value(f"e:{A}:locked") is False)
        fake.set_state(100000001, state=4)  # het slot is aan het vergrendelen
        await eventually(lambda: r.engine.ref_value(f"e:{A}:locked") is None and r.engine.ref_value(f"e:{A}:state") == 4)
        fake.set_state(100000002, doorState=3)
        await eventually(lambda: r.engine.ref_value(f"e:{B}:door_open") is True)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_door_sensor_that_comes_later_adds_a_point(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: A in hub.online)
        rev = hub.devices[A]["rev"]
        fake.set_state(100000001, doorState=2)
        await eventually(lambda: hub.devices[A]["rev"] != rev)
        assert "door_open" in {p["id"] for p in hub.devices[A]["points"]} and r.engine.ref_value(f"e:{A}:door_open") is False
        await shutdown(hub, fake)
    asyncio.run(go())


def test_offline_when_nuki_cannot_be_reached_and_back(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: A in hub.online)
        port = fake.port
        await fake.stop()
        await eventually(lambda: A not in hub.online and not hub.nk[aid]["online"])
        assert r.engine.ref_value(f"e:{A}:locked") is None, "offline: elke waarde is onbekend, nooit de laatste gok (een deur!)"
        assert "geen verbinding" in status(hub, A)["error"] and hub.nuki_view()[0]["error"]
        await fake.start(port)
        await eventually(lambda: A in hub.online, 8)
        assert r.engine.ref_value(f"e:{A}:locked") is True and status(hub, A)["error"] == ""
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_rejected_token_puts_everything_offline_with_the_reason(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: A in hub.online)
        fake.token = "ingetrokken"
        await eventually(lambda: A not in hub.online and B not in hub.online)
        v = hub.nuki_view()[0]
        assert v["key"] is True and "token" in v["error"] and "web.nuki.io" in status(hub, A)["error"]
        fake.token = TOKEN
        await eventually(lambda: A in hub.online, 8)
        assert hub.nuki_view()[0]["key"] is False
        await shutdown(hub, fake)
    asyncio.run(go())


def test_too_many_requests_makes_the_poll_wait(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: A in hub.online)
        fake.list_status = 429
        await eventually(lambda: A not in hub.online)
        assert "te veel verzoeken" in status(hub, A)["error"]
        n = fake.lists
        await asyncio.sleep(0.2)
        assert fake.lists - n <= 1, "na een 429 wachten we (NUKI_BACKOFF), we blijven niet hameren"
        fake.list_status = 0
        await eventually(lambda: A in hub.online, 5)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_lock_that_leaves_the_account_goes_offline(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: B in hub.online)
        del fake.locks[100000002]
        await eventually(lambda: B not in hub.online)
        assert "niet meer in je Nuki-account" in status(hub, B)["error"] and A in hub.online
        await shutdown(hub, fake)
    asyncio.run(go())


# ------------------------------------------------------------ bedienen


def test_locking_is_sent_and_the_state_follows_from_a_quick_poll(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(eh, "NUKI_POLL", 3600)  # alleen de snelle polls na de opdracht kunnen de stand brengen
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: B in hub.online and r.engine.ref_value(f"e:{B}:locked") is False)
        fake.delay = 0.03
        res = await hub.do(B, "lock")
        assert res == {"ok": True, "result": "queued"} and fake.calls == [("POST", "/smartlock/100000002/action", {"action": 2})]
        assert r.engine.ref_value(f"e:{B}:locked") is False, "de stand komt uit wat het slot meldt, niet uit de opdracht"
        await eventually(lambda: r.engine.ref_value(f"e:{B}:locked") is True, 3)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_what_opens_the_door_needs_a_confirmation_and_nothing_leaves_without_it(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: A in hub.online)
        for a in ("unlock", "unlatch", "lockngo", "unlatch_lockngo"):
            with pytest.raises(ModelError, match="Doorgaan"):
                await hub.do(A, a)
        with pytest.raises(ModelError, match="onbekende actie"):
            await hub.do(A, "reboot")
        assert fake.calls == []
        await hub.do(A, "unlock", confirm=True)
        await hub.do(A, "unlatch", confirm=True)
        await hub.do(A, "lockngo", confirm=True)
        await hub.do(A, "unlatch_lockngo", confirm=True)
        assert [c[2]["action"] for c in fake.calls] == [1, 3, 4, 5]
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_command_nuki_refuses_or_cannot_get_is_reported(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: A in hub.online)
        fake.status_next, fake.status_code = 1, 400
        with pytest.raises(ModelError, match="unlatch niet.*gebruik ontgrendelen"):
            await hub.do(A, "unlatch", confirm=True)  # een slot in een regio zonder unlatch
        fake.status_next, fake.status_code = 1, 400
        with pytest.raises(ModelError, match="nope"):
            await hub.do(A, "lock")
        fake.status_next, fake.status_code = 1, 429
        with pytest.raises(ModelError, match="te veel verzoeken"):
            await hub.do(A, "lock")
        fake.status_next, fake.status_code = 1, 403
        with pytest.raises(ModelError, match="smartlock.action"):
            await hub.do(A, "lock")
        fake.status_next, fake.status_code = 1, 500
        with pytest.raises(ModelError, match="HTTP 500"):
            await hub.do(A, "lock")
        await fake.stop()
        await eventually(lambda: A not in hub.online)
        with pytest.raises(ModelError, match="geen verbinding"):
            await hub.do(A, "lock")
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_lock_that_does_not_obey_keeps_its_state(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: B in hub.online)
        fake.unreachable = True  # Nuki neemt de opdracht aan (204), het slot voert hem nooit uit
        await hub.do(B, "lock")
        await asyncio.sleep(0.6)
        assert r.engine.ref_value(f"e:{B}:locked") is False and r.engine.ref_value(f"e:{B}:state") == 3, "een aangenomen opdracht is geen vergrendelde deur"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_slot_has_no_point_to_set_but_its_actions_are_inputs(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: A in hub.online)
        for pid in ("locked", "state", "battery"):
            with pytest.raises(ModelError, match="niet instelbaar"):
                await hub.set(A, pid, 1)
            with pytest.raises(ModelError, match="niet te koppelen"):
                hub.update(A, {"inputs": {pid: "t:1"}})
        assert fake.calls == []
        b = next(x for x in schema.view(r.model, r.engine.snapshot())["blocks"] if x["key"] == f"e:{A}")
        assert [p["name"] for p in b["ports"]] == ["do_lock", "do_unlock", "do_unlatch", "do_lockngo", "do_unlatch_lockngo"], "elke actie is een ingang, alleen die"
        assert [(p["row"], p["action"], p["danger"], p["type"]) for p in b["ports"]] == [(4, True, False, "bool"), (5, True, True, "bool"), (6, True, True, "bool"), (7, True, True, "bool"), (8, True, True, "bool")]
        assert b["rows"] == 9 and len(b["outs"]) == 4, "vier punten en daaronder een rij per actie"
        await shutdown(hub, fake)
    asyncio.run(go())


async def wired(tmp_path, monkeypatch, action="unlock", **kw):
    """Een slot (A) met een actie aangesloten op een module die we zelf sturen: `flag["v"]` is wat de bron nu zegt (True, False of None)."""
    r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, **kw)
    await eventually(lambda: A in hub.online)
    mod = r.model.add_module("above", "Bron", 20)
    flag = {"v": False}
    real = r.engine.ref_value
    monkeypatch.setattr(r.engine, "ref_value", lambda ref: flag["v"] if ref == f"m:{mod}" else real(ref))
    hub.update(A, {"inputs": {f"do_{action}": f"m:{mod}"}})
    return r, hub, fake, flag, mod


def posts(fake):
    return [c[2]["action"] for c in fake.calls if c[0] == "POST"]


def test_an_action_input_starts_only_on_a_rise_from_0_to_1(tmp_path, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="kk.espdev")

    async def go():
        r, hub, fake, flag, mod = await wired(tmp_path, monkeypatch)
        hub.tick()
        await asyncio.sleep(0.1)
        assert posts(fake) == [], "de eerste stand wordt alleen onthouden"
        flag["v"] = True
        hub.tick()
        await eventually(lambda: posts(fake) == [1])
        for _ in range(3):
            hub.tick()  # 1 blijft 1: niets opnieuw
        flag["v"] = False
        hub.tick()  # 1 naar 0: niets
        await asyncio.sleep(0.15)
        assert posts(fake) == [1]
        assert "verstuurd" in status(hub, A)["last"]["do_unlock"] and "gestart door" in caplog.text, "wat een deur opende staat in het logboek van de add-on"
        await asyncio.sleep(0.15)  # voorbij ACTION_GAP: een nieuwe stijging doet het opnieuw
        flag["v"] = True
        hub.tick()
        await eventually(lambda: posts(fake) == [1, 1])
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_source_that_is_already_1_when_wired_or_after_a_restart_opens_nothing(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, flag, mod = await wired(tmp_path, monkeypatch)
        flag["v"] = True
        hub.update(A, {"inputs": {"do_unlock": f"m:{mod}", "do_lock": f"m:{mod}"}})  # opnieuw aansluiten: de eerste stand is weer alleen een stand
        hub.tick()
        await asyncio.sleep(0.15)
        assert posts(fake) == [], "een bron die al 1 is bij het aansluiten, opent niets"
        flag["v"] = None  # onbekend, en daarna weer 1: dat is geen stijging maar een eerste stand
        hub.tick()
        flag["v"] = True
        hub.tick()
        await asyncio.sleep(0.15)
        assert posts(fake) == []
        hub.close()
        hub2 = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)  # een herstart met de bron op 1
        hub2.start()
        await eventually(lambda: A in hub2.online)
        hub2.tick()
        await asyncio.sleep(0.15)
        assert posts(fake) == [] and hub2.devices[A]["inputs"] == {"do_unlock": f"m:{mod}", "do_lock": f"m:{mod}"}, "de aansluiting blijft, maar een herstart opent geen deur"
        await shutdown(hub2, fake)
    asyncio.run(go())


def test_an_action_never_waits_for_a_connection_and_never_retries(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, flag, mod = await wired(tmp_path, monkeypatch, action="unlatch")
        hub.tick()
        port = fake.port
        await fake.stop()
        await eventually(lambda: A not in hub.online)
        flag["v"] = True
        hub.tick()
        assert "vervalt" in status(hub, A)["last"]["do_unlatch"] and posts(fake) == []
        await fake.start(port)
        await eventually(lambda: A in hub.online, 8)
        for _ in range(5):
            hub.tick()
            await asyncio.sleep(0.05)
        assert posts(fake) == [], "een deur die tien minuten later opengaat is erger dan een deur die dicht blijft: de opdracht is vervallen"
        # een mislukking wordt niet opnieuw geprobeerd
        flag["v"] = False
        hub.tick()
        await asyncio.sleep(0.15)
        fake.status_next, fake.status_code = 1, 400
        flag["v"] = True
        hub.tick()
        await eventually(lambda: "niet opnieuw geprobeerd" in status(hub, A)["last"].get("do_unlatch", ""))
        for _ in range(5):
            hub.tick()
            await asyncio.sleep(0.05)
        assert posts(fake) == [3], "precies een poging (de mislukte): geen herhaling"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_source_that_flaps_does_not_open_the_door_three_times(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(eh, "ACTION_GAP", 5.0)
        r, hub, fake, flag, mod = await wired(tmp_path, monkeypatch, action="unlock")
        hub.tick()
        for v in (True, False, True, False, True):
            flag["v"] = v
            hub.tick()
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        assert posts(fake) == [1] and "minder dan 5 s" in status(hub, A)["last"]["do_unlock"]
        await shutdown(hub, fake)
    asyncio.run(go())


def test_an_action_input_checks_what_is_connected(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: A in hub.online and B in hub.online)
        number = r.model.add_module("above", "Getal", 20)
        r.model.set_module(number, inputs={"a": f"e:{B}:battery"})
        with pytest.raises(ModelError, match="0 of 1"):
            hub.update(A, {"inputs": {"do_unlock": f"e:{B}:battery"}})  # een getal start geen actie
        with pytest.raises(ModelError, match="niet te koppelen"):
            hub.update(A, {"inputs": {"do_reboot": f"m:{number}"}})  # een actie die er niet is
        with pytest.raises(ModelError, match="niet bestaat"):
            hub.update(A, {"inputs": {"do_unlock": "m:nergens"}})
        with pytest.raises(ModelError, match="zichzelf"):
            hub.update(A, {"inputs": {"do_unlock": f"e:{A}:locked"}})
        hub.update(A, {"inputs": {"do_unlock": f"e:{B}:door_open", "do_lock": f"m:{number}"}})
        assert set(hub.devices[A]["inputs"]) == {"do_unlock", "do_lock"}
        hub.update(A, {"inputs": {"do_unlock": None}})
        assert set(hub.devices[A]["inputs"]) == {"do_lock"}, "weghalen kan"
        b = next(x for x in schema.view(r.model, r.engine.snapshot())["blocks"] if x["key"] == f"e:{A}")
        assert {p["name"]: p["ref"] for p in b["ports"]}["do_lock"] == f"m:{number}" and fake.calls == []
        await shutdown(hub, fake)
    asyncio.run(go())


# ------------------------------------------------------------ Home Assistant


def test_ha_gets_only_the_harmless_button_and_refuses_the_rest(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: A in hub.online)
        assert hub.discovery() == {} and hub.states() == {}, "standaard staat er niets in HA via deze add-on"
        hub.update(A, {"ha": True})
        disc = hub.discovery()
        base = f"kk_esp_{A}"
        buttons = [t for t in disc if "/button/" in t]
        assert len(buttons) == 1 and buttons[0].endswith(f"/button/{base}_do_lock/config"), buttons
        assert not any(w in t for t in disc for w in ("unlock", "unlatch", "lockngo")), "ontgrendelen kan niet vanuit HA via deze add-on"
        assert any(f"/binary_sensor/{base}_locked/config" in t for t in disc) and any(f"/sensor/{base}_state/config" in t for t in disc)
        bat = next(v for t, v in disc.items() if t.endswith(f"/sensor/{base}_battery/config"))
        assert (bat["device_class"], bat["unit_of_measurement"]) == ("battery", "%") and next(iter(disc.values()))["device"]["manufacturer"] == "Nuki"
        assert hub.states()[hub.t(A, "locked")][0] == "ON" and hub.states()[hub.t(A, "state")][0] == "Vergrendeld"
        assert hub.command(f"{eh.d.PREFIX}/esp/{A}/do/unlock/press", "PRESS") and hub.command(f"{eh.d.PREFIX}/esp/{A}/do/lock/press", "PRESS")
        await eventually(lambda: len(fake.calls) >= 1)
        await asyncio.sleep(0.2)
        assert [c[2]["action"] for c in fake.calls] == [2], "alleen vergrendelen komt door; ontgrendelen via MQTT wordt geweigerd"
        await shutdown(hub, fake)
    asyncio.run(go())


# ------------------------------------------------------------ opslag, schema en opruimen


def test_it_survives_a_restart(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        hub.update(A, {"name": "Voordeur (hal)", "ha": True})
        rev = hub.devices[A]["rev"]
        hub.close()
        hub2 = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        assert hub2.accounts[aid]["token"] == TOKEN and set(hub2.devices) == {A, B}
        d = hub2.devices[A]
        assert (d["name"], d["driver"], d["account"], d["lid"], d["rev"], d["ha"]) == ("Voordeur (hal)", "nuki", aid, "100000001", rev, True)
        hub2.start()
        await eventually(lambda: A in hub2.online)
        assert hub2.output(f"e:{A}:locked") is True and hub2.devices[A]["rev"] == rev
        await shutdown(hub2, fake)
    asyncio.run(go())


def test_a_damaged_file_does_not_stop_the_hub(tmp_path):
    (tmp_path / "espdev.json").write_text(json.dumps({"nuki": {"n_ok": {"token": "abcdefgh12", "name": "Thuis"}, "n_leeg": {"token": ""}, "Fout Id": {"token": "x"}, "n_nul": 5},
                                                      "devices": {"nuki_1": {"hid": "nuki-1", "driver": "nuki", "account": "n_ok", "lid": "geen nummer"}}}), encoding="utf-8")
    r = Rig(tmp_path)
    hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
    assert list(hub.accounts) == ["n_ok"] and hub.devices["nuki_1"]["lid"] == "" and hub.devices["nuki_1"]["driver"] == "nuki"


def test_the_block_the_inventory_and_the_account_removal(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: A in hub.online and B in hub.online)
        blocks = {b["key"]: b for b in schema.view(r.model, r.engine.snapshot())["blocks"]}
        b = blocks[f"e:{A}"]
        assert (b["kind"], b["tech"], b["driver"], b["online"], b["account"]) == ("espdev", "Nuki-slot · Nuki", "nuki", True, aid)
        assert [o["name"] for o in b["outs"]] == ["state", "locked", "battery", "battery_low"] and [o["name"] for o in blocks[f"e:{B}"]["outs"]] == ["state", "locked", "door_open", "battery", "battery_low"]
        row = next(x for x in inventory.build(r.engine, None) if x["key"] == f"e{A}")
        assert (row["name"], row["kind"], row["protocol"], row["ip"], row["online"]) == ("Voordeur", "Nuki-slot", "Nuki Web API", "api.nuki.io", True)
        n = len(fake.calls)
        assert await hub.remove(A) and A not in hub.devices and A not in hub.nuki_desc and fake.calls[n:] == []
        assert [x["claimed"] for x in hub.nuki_view()[0]["locks"] if x["name"] == "Voordeur"] == [False], "hij staat weer in de lijst"
        assert await hub.remove_nuki(aid) and hub.accounts == {} and hub.devices == {} and hub.nuki_view() == [] and f"nuki:{aid}" not in hub.tasks
        assert json.loads((tmp_path / "espdev.json").read_text(encoding="utf-8"))["nuki"] == {}
        assert not await hub.remove_nuki(aid)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_web_api_adds_claims_confirms_and_removes(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from kkaddon import web
    from kkaddon.settings import Settings

    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = await house().start()
        monkeypatch.setattr(nuki, "BASE", f"http://127.0.0.1:{fake.port}")
        ctx = web.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), r.engine, espdev=hub)
        r.model.set_schema({**r.model.schema, "pages": [{"id": "hal", "name": "Hal"}]})
        c = TestClient(TestServer(web.make_app(ctx)))
        await c.start_server()
        try:
            bad = await c.post("/api/espdev/nuki/account", json={"token": "x" * 40})
            assert bad.status == 400 and "token" in (await bad.json())["error"]
            aid = (await (await c.post("/api/espdev/nuki/account", json={"token": TOKEN})).json())["id"]
            await eventually(lambda: hub.nk[aid]["online"])
            st = await (await c.get("/api/state")).json()
            assert [a["id"] for a in st["nuki"]] == [aid] and len(st["nuki"][0]["locks"]) == 2 and TOKEN not in json.dumps(st)
            assert (await c.post("/api/espdev/nuki/claim", json={"account": aid, "lids": "ja"})).status == 400
            res = await (await c.post("/api/espdev/nuki/claim", json={"account": aid, "page": "hal", "x": 40, "y": 30})).json()
            assert res["keys"] == [f"e:{A}", f"e:{B}"] and all(r.model.schema["blocks"][k]["page"] == "hal" for k in res["keys"])
            await eventually(lambda: A in hub.online)
            bad = await c.post(f"/api/espdev/{A}/do", json={"action": "unlock"})
            assert bad.status == 400 and "Doorgaan" in (await bad.json())["error"] and fake.calls == [], "zonder bevestiging gaat er niets naar het slot"
            assert (await c.post(f"/api/espdev/{A}/do", json={"action": "unlock", "confirm": True})).status == 200
            assert [c_[2]["action"] for c_ in fake.calls] == [1]
            assert (await c.put(f"/api/espdev/{A}", json={"host": "1.2.3.4"})).status == 400
            assert (await c.put(f"/api/espdev/{A}", json={"inputs": {"do_unlock": f"e:{B}:door_open"}})).status == 200 and hub.devices[A]["inputs"] == {"do_unlock": f"e:{B}:door_open"}
            assert (await c.put(f"/api/espdev/{A}", json={"inputs": {"do_unlock": f"e:{B}:battery"}})).status == 400
            assert (await c.delete(f"/api/espdev/nuki/account/{aid}")).status == 200 and hub.accounts == {} and hub.devices == {}
            assert all(k not in r.model.schema["blocks"] for k in res["keys"]) and (await c.delete(f"/api/espdev/nuki/account/{aid}")).status == 404
        finally:
            await c.close()
            await shutdown(hub, fake)
    asyncio.run(go())
