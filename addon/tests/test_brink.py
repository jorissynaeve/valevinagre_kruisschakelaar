"""Brink-ventilatie via de cloud van Brink Home als HUISDEV-apparaat (brink.py en de Brink-kant van espdevhub.py): de login (code-flow met PKCE, een formulier met anti-forgery token),
de lijst van systemen en de beschrijving, punten en waarden, een blok per unit, pollen, schrijven, een sessie die verloopt, een onjuist wachtwoord (nooit steeds opnieuw proberen),
een module die offline is, de entiteiten in HA, opslag en het schema. Tegen een nagebootst Brink Home over echte HTTP (tests/fakebrink.py), gebouwd naar de echte site."""
import asyncio
import json
import time

import pytest

from kkaddon import brink, inventory, schema
from kkaddon import espdevhub as eh
from kkaddon.espdevhub import EspDevHub
from kkaddon.model import ModelError
from tests.fakebrink import BASE_ID, EMAIL, FLAIR, PASSWORD, SYSTEM, WIFI, FakeBrink
from tests.rig import KEY, Rig

DID = f"brink_{SYSTEM}_{FLAIR}"
KEYS = f"{SYSTEM}:{FLAIR}"


async def eventually(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("duurde te lang")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(eh, "BRINK_POLL", 0.1)
    monkeypatch.setattr(eh, "BRINK_BURST", (0.05, 0.1))
    monkeypatch.setattr(eh, "BRINK_BACKOFF", 0.3)
    monkeypatch.setattr(eh, "BRINK_AUTH_WAIT", 3600)
    monkeypatch.setattr(eh, "TICK", 3600)


async def rig(tmp_path, monkeypatch, fake=None, claim=True):
    r = Rig(tmp_path)
    hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
    fake = fake or FakeBrink()
    await fake.start()
    monkeypatch.setattr(brink, "BASE", f"http://127.0.0.1:{fake.port}")
    aid = await hub.add_brink(EMAIL, PASSWORD)
    await eventually(lambda: hub.bk[aid]["online"])
    dids = await hub.claim_brink(aid) if claim else []
    return r, hub, fake, aid, dids


async def shutdown(hub, *fakes):
    hub.close()
    for f in fakes:
        await f.stop()
    await asyncio.sleep(0.05)


def status(hub, did=DID):
    return next(x for x in hub.status() if x["id"] == did)


def comp_of(fake=None):
    fake = fake or FakeBrink()
    return brink.components(fake.tree())[str(FLAIR)]


# ------------------------------------------------------------ de beschrijving lezen


def test_the_system_list_and_the_description_are_read_by_their_ids():
    f = FakeBrink()
    assert brink.systems({"items": [{"systemName": " Thuis ", "systemShareId": SYSTEM, "gatewayState": 2}, {"systemName": "zonder id"}, 5, {"systemShareId": True}]}) == {
        str(SYSTEM): {"name": "Thuis", "state": 2}}
    assert brink.systems({}) == {} and brink.systems(None) == {} and brink.systems({"items": "x"}) == {}
    comps = brink.components(f.tree())
    assert set(comps) == {str(WIFI), str(FLAIR)} and comps[str(FLAIR)]["name"] == "Flair 400" and len(comps[str(FLAIR)]["params"]) == 18, "de parameters van dezelfde unit onder twee plekken in de boom worden samengevoegd"
    assert set(brink.ventilation_units(comps)) == {str(FLAIR)}, "de wifi-module is geen ventilatie-unit"
    assert brink.components(None) == {} and brink.components({"root": {"navigationItems": "x"}}) == {}


def test_the_points_follow_the_parameters_by_name_not_by_id():
    d = brink.describe(comp_of())
    p = {x["id"]: x for x in d["points"]}
    assert list(p) == ["level", "mode", "remaining", "bypass_mode", "supply_flow", "exhaust_flow", "outdoor_temp", "supply_temp", "bypass_state", "preheater", "filter_dirty", "filter_days", "active"]
    assert [i for i, x in p.items() if x["writable"]] == ["level", "mode", "bypass_mode"], "alleen deze drie stel je in"
    assert (p["level"]["kind"], [o["v"] for o in p["level"]["options"]], p["outdoor_temp"]["cls"], p["supply_flow"]["unit"], p["active"]["diag"]) == ("enum", [0, 1, 2, 3], "temperature", "m³/h", True)
    assert d["fw"] == "S2.01.21" and d["name"] == "Flair 400" and d["type"] == "Ventilatie" and not d["actions"]
    # een andere installatie heeft andere ids maar dezelfde namen; een parameter die er niet is geeft geen punt
    f2 = FakeBrink()
    f2.params = {k: {**v, "valueId": v["valueId"] + 777} for k, v in f2.params.items() if k != "Zulufttemperatur"}
    d2 = brink.describe(brink.components(f2.tree())[str(FLAIR)])
    assert "supply_temp" not in {x["id"] for x in d2["points"]} and len(d2["points"]) == 12 and d2["rev"] != d["rev"]


def test_every_point_is_a_valid_huisdev_point():
    d = brink.describe(comp_of())
    c = eh.clean_descriptor({"proto": "1.0", "rev": d["rev"], "device": {"id": "brink-9001-9002"}, "points": [{**x, "class": x["cls"]} for x in d["points"]], "actions": []})
    assert len(c["points"]) == len(d["points"]) == 13


def test_values_come_from_text_and_a_bad_state_is_unknown():
    f = FakeBrink()
    v = brink.values(comp_of(f))
    assert v["level"] == 1 and v["mode"] == 1 and v["supply_flow"] == 100 and v["outdoor_temp"] == 8.8 and v["supply_temp"] == 20.5 and v["filter_dirty"] is False and v["filter_days"] == 181
    assert v["bypass_state"] == 4 and v["active"] == 13 and v["remaining"] == 0
    for state in (4, 5, 6):  # fout, buiten dienst, geen geldige data: onbekend, nooit de laatste gok
        f.set_value("Zulufttemperatur", "20.5", state)
        assert brink.values(comp_of(f))["supply_temp"] is None, state
    f.set_value("Lüftungsstufe", "2", 1)  # 1: wordt gewijzigd, de waarde blijft bruikbaar
    assert brink.values(comp_of(f))["level"] == 2
    f.set_value("Lüftungsstufe", "9")  # een stand die we niet kennen
    f.set_value("Frischlufttemperatur", "NaN")
    f.set_value("Betriebsart", "geen getal")
    w = brink.values(comp_of(f))
    assert (w["level"], w["outdoor_temp"], w["mode"]) == (None, None, None)
    f.params["Status Filtermeldung"]["value"] = None
    assert brink.values(comp_of(f))["filter_dirty"] is None


def test_a_write_names_the_value_id_sends_text_and_keeps_the_state():
    c = comp_of()
    path, body = brink.write_request(str(SYSTEM), c, "level", 2)
    assert path == f"/portal/api/v1.1/systems/{SYSTEM}/parameter-values" and body == {"writeValues": [{"valueId": BASE_ID + 6, "value": "2", "state": 0}]}
    assert brink.write_request(str(SYSTEM), c, "mode", 0)[1]["writeValues"][0]["value"] == "0" and brink.write_request(str(SYSTEM), c, "bypass_mode", 2)[1]["writeValues"][0]["valueId"] == BASE_ID + 9
    for pid, v in (("supply_temp", 1), ("filter_dirty", 1), ("nergens", 1), ("level", 7), ("mode", 9)):
        with pytest.raises(ValueError):
            brink.write_request(str(SYSTEM), c, pid, v)
    c["params"]["Lüftungsstufe"]["readWrite"] = 1  # Brink laat het niet (meer) toe
    with pytest.raises(ValueError, match="laat dit niet"):
        brink.write_request(str(SYSTEM), c, "level", 1)


def test_ids_come_from_the_system_and_the_component_and_the_account_hash_hides_the_email():
    assert brink.hid_of("9001", "9002") == "brink-9001-9002" and eh.ID_RE.match(brink.hid_of("9001", "9002"))
    assert brink.account_id("Jan@Example.org ") == brink.account_id("jan@example.org") != brink.account_id("piet@example.org") and "jan" not in brink.account_id("jan@example.org")


# ------------------------------------------------------------ inloggen


def test_the_login_form_is_found_among_the_other_forms_of_the_page():
    page = FakeBrink._login_page("tok&amp;en", "/idsrv/account/login?ReturnUrl=%2Fx&y=1")
    action, hidden = brink.login_form(page)
    assert action == "/idsrv/account/login?ReturnUrl=%2Fx&y=1" and hidden == {"__RequestVerificationToken": "tok&en"}, "het tweede formulier van de pagina (zonder velden) en RememberMe tellen niet mee"
    assert brink.login_form("<html>onderhoud</html>") is None and brink.login_form('<form><input name="Input.Username"/><input name="Input.Password"/></form>') is None


def test_login_uses_pkce_the_form_and_the_cookie_and_gives_a_working_token(tmp_path, monkeypatch):
    async def go():
        import aiohttp
        f = await FakeBrink().start()
        monkeypatch.setattr(brink, "BASE", f"http://127.0.0.1:{f.port}")
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
            token, ttl = await brink.login(s, EMAIL, PASSWORD)
            assert ttl == 3600 and f.logins == 1 and f.bad_logins == 0, "de code is met de juiste PKCE-verifier ingewisseld (de nagebootste server controleert hem)"
        async with aiohttp.ClientSession() as s:
            st, data = await brink.request(s, token, "GET", f"{brink.API}systems?pageSize=50")
            assert st == 200 and data["items"][0]["systemShareId"] == SYSTEM
            with pytest.raises(brink.BrinkError) as e:
                await brink.request(s, "nep-token", "GET", f"{brink.API}systems")
            assert e.value.code == "token"
        await f.stop()
    asyncio.run(go())


def test_a_wrong_password_is_one_attempt_and_says_so(tmp_path, monkeypatch):
    async def go():
        import aiohttp
        f = await FakeBrink().start()
        monkeypatch.setattr(brink, "BASE", f"http://127.0.0.1:{f.port}")
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
            with pytest.raises(brink.BrinkError, match="e-mail of het wachtwoord") as e:
                await brink.login(s, EMAIL, "fout")
        assert e.value.code == "auth" and f.logins == 1 and f.bad_logins == 1
        f.hide_form = True
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
            with pytest.raises(brink.BrinkError, match="niet herken") as e:
                await brink.login(s, EMAIL, PASSWORD)
        assert e.value.code == "net" and f.logins == 1, "een pagina zonder herkenbaar formulier (Brink veranderde iets): geen wachtwoord gestuurd"
        await f.stop()
    asyncio.run(go())


# ------------------------------------------------------------ koppelen en volgen


def test_an_account_is_checked_by_logging_in_and_the_password_is_never_shown(tmp_path, monkeypatch):
    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = await FakeBrink().start()
        monkeypatch.setattr(brink, "BASE", f"http://127.0.0.1:{fake.port}")
        for bad in ("", "geen-adres", "a@"):
            with pytest.raises(ModelError, match="e-mailadres"):
                await hub.add_brink(bad, "x")
        with pytest.raises(ModelError, match="e-mailadres"):
            await hub.add_brink(EMAIL, "")
        with pytest.raises(ModelError, match="e-mail of het wachtwoord"):
            await hub.add_brink(EMAIL, "fout")
        assert hub.brink_accounts == {} and not (tmp_path / "espdev.json").exists() and fake.bad_logins == 1, "een wachtwoord dat niet werkt wordt niet bewaard, en maar een keer geprobeerd"
        aid = await hub.add_brink(EMAIL, PASSWORD, name="Thuis")
        assert aid == brink.account_id(EMAIL) and hub.brink_accounts[aid] == {"name": "Thuis", "email": EMAIL, "password": PASSWORD}
        assert json.loads((tmp_path / "espdev.json").read_text(encoding="utf-8"))["brink"][aid]["password"] == PASSWORD
        await eventually(lambda: hub.bk[aid]["online"])
        shown = json.dumps([hub.brink_view(), hub.status(), r.engine.snapshot()["brink"]])
        assert PASSWORD not in shown and EMAIL not in shown and [u["name"] for u in hub.brink_view()[0]["units"]] == ["Flair 400"]
        with pytest.raises(ModelError, match="staat er al"):
            await hub.add_brink(EMAIL, PASSWORD)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_an_account_without_systems_or_with_a_broken_site_is_refused(tmp_path, monkeypatch):
    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = await FakeBrink().start()
        monkeypatch.setattr(brink, "BASE", f"http://127.0.0.1:{fake.port}")
        fake.api_status = 500
        with pytest.raises(ModelError, match="HTTP 500"):
            await hub.add_brink(EMAIL, PASSWORD)
        monkeypatch.setattr(brink, "BASE", "http://127.0.0.1:9")
        with pytest.raises(ModelError, match="geen verbinding"):
            await hub.add_brink(EMAIL, PASSWORD)
        assert hub.brink_accounts == {}
        hub.close()
        await fake.stop()
    asyncio.run(go())


def test_claiming_makes_a_block_for_the_ventilation_unit_only(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch, claim=False)
        assert [u["key"] for u in hub.brink_view()[0]["units"]] == [KEYS], "de wifi-module is geen blok"
        with pytest.raises(ModelError, match="kent deze unit niet"):
            await hub.claim_brink(aid, ["1:2"])
        new = await hub.claim_brink(aid, [KEYS])
        assert new == [DID] and hub.devices[DID]["driver"] == "brink" and (hub.devices[DID]["sid"], hub.devices[DID]["cid"]) == (str(SYSTEM), str(FLAIR)) and hub.devices[DID]["ha"] is False
        with pytest.raises(ModelError, match="staat er al"):
            await hub.claim_brink(aid)
        with pytest.raises(ModelError, match="niet bereikbaar"):
            await hub.claim_brink("k_nergens")
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_block_shows_the_unit_and_follows_changes(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        ref = lambda pid: r.engine.ref_value(f"e:{DID}:{pid}")  # noqa: E731
        assert (ref("level"), ref("mode"), ref("supply_flow"), ref("outdoor_temp"), ref("filter_dirty"), ref("bypass_state")) == (1, 1, 100, 8.8, False, 4)
        s = status(hub)
        assert (s["driver"], s["online"], s["type"], s["host"], s["fw"], s["ha"], s["account"]) == ("brink", True, "Ventilatie", "www.brink-home.com", "S2.01.21", False, aid)
        rev = hub.devices[DID]["rev"]
        fake.set_value("Lüftungsstufe", "3")  # iemand draait aan de wandschakelaar
        fake.set_value("Frischlufttemperatur", "10.5")
        fake.set_value("Status Filtermeldung", "1")
        await eventually(lambda: ref("level") == 3 and ref("outdoor_temp") == 10.5 and ref("filter_dirty") is True)
        assert hub.devices[DID]["rev"] == rev
        fake.set_value("Ist-Wert Luftdurchsatz Zuluft", "0", 6)
        await eventually(lambda: ref("supply_flow") is None)
        assert fake.lists >= 3 and fake.uis == fake.lists, "twee verzoeken per ronde voor alle units van het account"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_module_that_is_offline_at_brink_makes_the_block_offline_not_stale(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        fake.gateway_state = 1  # Brink: de module meldt zich niet; de cloud toont zijn laatste stand
        await eventually(lambda: DID not in hub.online)
        assert "offline" in status(hub)["error"] and r.engine.ref_value(f"e:{DID}:level") is None, "een module die weg is geeft geen waarde, nooit de laatste gok"
        assert hub.brink_view()[0]["online"] is True, "Brink zelf is bereikbaar"
        fake.gateway_state = 2
        await eventually(lambda: DID in hub.online)
        assert r.engine.ref_value(f"e:{DID}:level") == 1
        await shutdown(hub, fake)
    asyncio.run(go())


def test_offline_when_brink_cannot_be_reached_and_back(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        port = fake.port
        await fake.stop()
        await eventually(lambda: DID not in hub.online and not hub.bk[aid]["online"])
        assert "geen verbinding" in status(hub)["error"] and r.engine.ref_value(f"e:{DID}:level") is None
        await fake.start(port)
        await eventually(lambda: DID in hub.online, 8)
        assert r.engine.ref_value(f"e:{DID}:level") == 1 and status(hub)["error"] == ""
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_token_that_expires_or_is_revoked_is_replaced_by_logging_in_again(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(eh, "BRINK_REAUTH", 0)
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        n = fake.logins
        fake.revoke_tokens()  # de sessie is ingetrokken: de volgende ronde krijgt een 401 en logt een keer opnieuw in
        await eventually(lambda: fake.logins > n)
        fake.set_value("Lüftungsstufe", "2")
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:level") == 2, 6)
        assert DID in hub.online and fake.bad_logins == 0
        # een token dat bijna verloopt wordt vernieuwd voor het gebruikt wordt
        fake.token_ttl = 1
        hub.bk[aid]["token"] = None
        await eventually(lambda: hub.bk[aid]["exp"] and hub.bk[aid]["exp"] - time.monotonic() < 2, 4)
        n = fake.logins
        await eventually(lambda: fake.logins > n, 6)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_wrong_password_later_is_tried_once_then_left_alone_until_the_password_changes(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        fake.password = "nieuw-wachtwoord"  # het wachtwoord is bij Brink gewijzigd
        fake.revoke_tokens()
        await eventually(lambda: DID not in hub.online and hub.brink_view()[0]["auth"])
        assert "e-mail of het wachtwoord" in status(hub)["error"] and fake.bad_logins == 1
        await asyncio.sleep(0.6)
        assert fake.bad_logins == 1, "een onjuist wachtwoord probeert de add-on niet steeds opnieuw: een account kan daarop worden geblokkeerd"
        with pytest.raises(ModelError, match="onbekend account"):
            hub.update_brink("k_nergens", "x")
        with pytest.raises(ModelError, match="nieuw wachtwoord"):
            hub.update_brink(aid, "")
        hub.update_brink(aid, "nieuw-wachtwoord")
        await eventually(lambda: DID in hub.online, 6)
        assert fake.bad_logins == 1 and not hub.brink_view()[0]["auth"] and hub.brink_accounts[aid]["password"] == "nieuw-wachtwoord"
        await shutdown(hub, fake)
    asyncio.run(go())


# ------------------------------------------------------------ bedienen


def test_a_set_sends_the_value_and_the_state_follows_from_a_quick_poll(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(eh, "BRINK_POLL", 3600)  # alleen de snelle polls na de opdracht kunnen de stand brengen
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online and r.engine.ref_value(f"e:{DID}:level") == 1)
        fake.apply_delay = 0.03
        res = await hub.set(DID, "level", 3)
        assert res == {"ok": True, "result": "queued"} and fake.puts == [{"writeValues": [{"valueId": BASE_ID + 6, "value": "3", "state": 0}]}]
        assert r.engine.ref_value(f"e:{DID}:level") == 1, "de stand komt uit wat de unit meldt, niet uit de opdracht"
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:level") == 3, 3)
        await hub.set(DID, "mode", 0)
        await hub.set(DID, "bypass_mode", 2)
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:mode") == 0 and r.engine.ref_value(f"e:{DID}:bypass_mode") == 2, 3)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_what_must_not_be_sent_is_refused_before_it_leaves(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        for pid, v, word in (("supply_temp", 1, "niet instelbaar"), ("filter_dirty", 0, "niet instelbaar"), ("active", 1, "niet instelbaar"), ("nergens", 1, "onbekend"),
                             ("level", 9, "geen toegestane keuze"), ("level", "veel", "getal"), ("mode", True, "getal"), ("mode", 1.5, "geheel getal")):
            with pytest.raises(ModelError, match=word):
                await hub.set(DID, pid, v)
        assert fake.puts == []
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_command_brink_refuses_or_cannot_get_is_reported(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        fake.put_status = 403
        with pytest.raises(ModelError, match="weigert dit voor dit account"):
            await hub.set(DID, "level", 2)
        fake.put_status = 429
        with pytest.raises(ModelError, match="te veel verzoeken"):
            await hub.set(DID, "level", 2)
        fake.put_status = 500
        with pytest.raises(ModelError, match="HTTP 500"):
            await hub.set(DID, "level", 2)
        assert r.engine.ref_value(f"e:{DID}:level") == 1, "de stand blijft wat de unit meldt"
        fake.put_status = 200
        fake.gateway_state = 1
        await eventually(lambda: DID not in hub.online)
        with pytest.raises(ModelError, match="geen verbinding"):
            await hub.set(DID, "level", 2)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_an_input_sets_the_level_only_on_a_change(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online and r.engine.ref_value(f"e:{DID}:level") == 1)
        stand = r.model.add_module("number", "Stand", 1)
        with pytest.raises(ModelError, match="zichzelf"):
            hub.update(DID, {"inputs": {"level": f"e:{DID}:supply_flow"}})
        with pytest.raises(ModelError, match="niet te koppelen"):
            hub.update(DID, {"inputs": {"supply_temp": f"m:{stand}"}})  # alleen-lezen
        hub.update(DID, {"inputs": {"level": f"m:{stand}"}})
        hub.tick()
        await asyncio.sleep(0.15)
        assert fake.puts == [], "de eerste stand wordt alleen onthouden: een herstart verandert de ventilatie niet"
        r.model.set_module(stand, value=3)
        hub.tick()
        await eventually(lambda: r.engine.ref_value(f"e:{DID}:level") == 3 and len(fake.puts) == 1)
        assert fake.puts[0]["writeValues"][0] == {"valueId": BASE_ID + 6, "value": "3", "state": 0}
        for _ in range(3):
            hub.tick()
        await asyncio.sleep(0.15)
        assert len(fake.puts) == 1, "een ongewijzigde ingang stuurt niets opnieuw"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_block_on_the_schema_the_inventory_and_home_assistant(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        await eventually(lambda: DID in hub.online)
        b = next(x for x in schema.view(r.model, r.engine.snapshot())["blocks"] if x["key"] == f"e:{DID}")
        assert (b["kind"], b["tech"], b["driver"], b["online"], b["account"]) == ("espdev", "Ventilatie · Brink Home", "brink", True, aid)
        assert [p["name"] for p in b["ports"]] == ["level", "mode", "bypass_mode"] and not any(p.get("action") for p in b["ports"]), "drie ingangen: de punten die je instelt"
        assert [o["name"] for o in b["outs"]][:2] == ["level", "mode"] and "active" not in {o["name"] for o in b["all"]}, "de actieve regeling is diagnose"
        row = next(x for x in inventory.build(r.engine, None) if x["key"] == f"e{DID}")
        assert (row["name"], row["kind"], row["protocol"], row["ip"], row["online"]) == ("Flair 400", "Brink-ventilatie", "Brink Home (cloud)", "www.brink-home.com", True)
        assert hub.discovery() == {} and hub.states() == {}, "standaard niets in HA"
        hub.update(DID, {"ha": True})
        disc = hub.discovery()
        base = f"kk_esp_{DID}"
        assert any(t.endswith(f"/select/{base}_level/config") for t in disc) and any(t.endswith(f"/binary_sensor/{base}_filter_dirty/config") for t in disc)
        temp = next(v for t, v in disc.items() if t.endswith(f"/sensor/{base}_outdoor_temp/config"))
        assert (temp["device_class"], temp["unit_of_measurement"]) == ("temperature", "°C") and next(iter(disc.values()))["device"]["manufacturer"] == "Brink"
        assert hub.states()[hub.t(DID, "level")][0] == "Stand 1"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_it_survives_a_restart_and_removal_takes_everything(tmp_path, monkeypatch):
    async def go():
        r, hub, fake, aid, dids = await rig(tmp_path, monkeypatch)
        hub.update(DID, {"name": "Ventilatie", "ha": True})
        rev = hub.devices[DID]["rev"]
        hub.close()
        hub2 = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        assert hub2.brink_accounts[aid]["password"] == PASSWORD and hub2.devices[DID]["driver"] == "brink" and hub2.devices[DID]["name"] == "Ventilatie"
        assert (hub2.devices[DID]["account"], hub2.devices[DID]["sid"], hub2.devices[DID]["cid"], hub2.devices[DID]["rev"]) == (aid, str(SYSTEM), str(FLAIR), rev)
        hub2.start()
        await eventually(lambda: DID in hub2.online, 8)
        assert hub2.output(f"e:{DID}:level") == 1
        n = len(fake.puts)
        assert await hub2.remove(DID) and fake.puts[n:] == [] and DID not in hub2.devices
        assert [x["claimed"] for x in hub2.brink_view()[0]["units"]] == [False]
        assert await hub2.remove_brink(aid) and hub2.brink_accounts == {} and hub2.devices == {} and hub2.brink_view() == [] and f"brink:{aid}" not in hub2.tasks
        assert json.loads((tmp_path / "espdev.json").read_text(encoding="utf-8"))["brink"] == {} and not await hub2.remove_brink(aid)
        await shutdown(hub2, fake)
    asyncio.run(go())


def test_a_damaged_file_does_not_stop_the_hub(tmp_path):
    (tmp_path / "espdev.json").write_text(json.dumps({"brink": {"k_ok": {"email": "a@b.c", "password": "x", "name": "Thuis"}, "k_leeg": {"email": "", "password": "x"}, "Fout Id": {"email": "a@b.c", "password": "x"},
                                                      "k_nul": 5, "k_geen": {"email": "a@b.c"}},
                                                "devices": {"brink_1_2": {"hid": "brink-1-2", "driver": "brink", "account": "k_ok", "sid": "geen", "cid": "2"}}}), encoding="utf-8")
    r = Rig(tmp_path)
    hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
    assert list(hub.brink_accounts) == ["k_ok"] and hub.devices["brink_1_2"]["sid"] == "" and hub.devices["brink_1_2"]["cid"] == "2"


def test_web_api_adds_claims_sets_and_removes(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from kkaddon import web
    from kkaddon.settings import Settings

    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = await FakeBrink().start()
        monkeypatch.setattr(brink, "BASE", f"http://127.0.0.1:{fake.port}")
        ctx = web.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), r.engine, espdev=hub)
        r.model.set_schema({**r.model.schema, "pages": [{"id": "hal", "name": "Hal"}]})
        c = TestClient(TestServer(web.make_app(ctx)))
        await c.start_server()
        try:
            bad = await c.post("/api/espdev/brink/account", json={"email": EMAIL, "password": "fout"})
            assert bad.status == 400 and "wachtwoord" in (await bad.json())["error"]
            aid = (await (await c.post("/api/espdev/brink/account", json={"email": EMAIL, "password": PASSWORD})).json())["id"]
            await eventually(lambda: hub.bk[aid]["online"])
            st = await (await c.get("/api/state")).json()
            assert [a["id"] for a in st["brink"]] == [aid] and PASSWORD not in json.dumps(st) and EMAIL not in json.dumps(st)
            assert (await c.post("/api/espdev/brink/claim", json={"account": aid, "keys": "ja"})).status == 400
            res = await (await c.post("/api/espdev/brink/claim", json={"account": aid, "page": "hal", "x": 40, "y": 30})).json()
            assert res["keys"] == [f"e:{DID}"] and r.model.schema["blocks"][f"e:{DID}"]["page"] == "hal"
            await eventually(lambda: DID in hub.online)
            assert (await c.post(f"/api/espdev/{DID}/set", json={"point": "level", "value": 2})).status == 200 and fake.puts[-1]["writeValues"][0]["value"] == "2"
            assert (await c.post(f"/api/espdev/{DID}/set", json={"point": "level", "value": 9})).status == 400
            assert (await c.put(f"/api/espdev/{DID}", json={"host": "1.2.3.4"})).status == 400
            assert (await c.put(f"/api/espdev/brink/account/{aid}", json={"name": "Thuis"})).status == 200 and hub.brink_accounts[aid]["name"] == "Thuis"
            assert (await c.put("/api/espdev/brink/account/k_nergens", json={"password": "x"})).status == 400
            assert (await c.delete(f"/api/espdev/brink/account/{aid}")).status == 200 and hub.brink_accounts == {} and hub.devices == {}
            assert f"e:{DID}" not in r.model.schema["blocks"] and (await c.delete(f"/api/espdev/brink/account/{aid}")).status == 404
        finally:
            await c.close()
            await shutdown(hub, fake)
    asyncio.run(go())
