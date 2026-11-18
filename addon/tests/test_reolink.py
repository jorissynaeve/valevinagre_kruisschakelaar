"""Een Reolink-camera als HUISDEV-apparaat (reolink.py en de Reolink-kant van espdevhub.py): aanmelden en kijken wat de camera kan, de punten (beweging, persoon, voertuig,
schijnwerper), de standen, een verlopen sessie, de schijnwerper bedienen, het webhook als wekker, offline en terug, opslag en de web-API. Tegen een nagebootste camera
(tests/fakereolink.py); er is geen echte camera mee vergeleken."""
import asyncio
import json
import time

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from kkaddon import espdevhub as eh
from kkaddon import reolink, web
from kkaddon.espdevhub import EspDevHub
from kkaddon.model import ModelError
from kkaddon.settings import Settings
from tests.fakereolink import FakeReolink
from tests.rig import KEY, Rig


async def eventually(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("duurde te lang")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(eh, "REO_FAST", 0.05)
    monkeypatch.setattr(eh, "REO_SLOW", 0.05)
    monkeypatch.setattr(eh, "REO_HOOKED", 0.05)
    monkeypatch.setattr(eh, "TICK", 3600)


async def rig(tmp_path, cam=None, **kw):
    r = Rig(tmp_path)
    hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05, webhook_port=0)
    cam = cam or FakeReolink()
    await cam.start()
    did = await hub.add_reolink("127.0.0.1", cam.port, cam.user, cam.password, **kw)
    await eventually(lambda: did in hub.online)
    return r, hub, cam, did


async def shutdown(hub, cam):
    hub.close()
    await cam.stop()
    await asyncio.sleep(0.05)


def pts(hub, did):
    return {p["id"]: p for p in hub.devices[did]["points"]}


# ------------------------------------------------------------ het pure deel


def test_the_points_follow_what_the_camera_reports():
    cam = FakeReolink()
    es = [cam._one(c, True) for c in reolink.probe_commands()]
    caps = reolink.caps_from_probe(es)
    assert caps == {"channel": 0, "model": "Reolink Duo Series P750", "name": "Tuinhuis cam", "serial": "ABC123456", "fw": "v3.1.0.2368", "ai": ["people", "vehicle"], "led": True}
    d = reolink.describe(caps, "reolink-abc123456", "Camera")
    assert [p["id"] for p in d["points"]] == ["motion", "person", "vehicle", "light", "bri"] and all(eh._point(p) for p in d["points"])
    assert {p["id"]: p["writable"] for p in d["points"]} == {"motion": False, "person": False, "vehicle": False, "light": True, "bri": True}
    bare = FakeReolink(led=False, ai=())
    caps2 = reolink.caps_from_probe([bare._one(c, True) for c in reolink.probe_commands()])
    assert caps2["ai"] == [] and caps2["led"] is False, "een camera zonder schijnwerper (fout op GetWhiteLed) of zonder AI krijgt die punten niet"
    assert [p["id"] for p in reolink.describe(caps2, "x", "x")["points"]] == ["motion"]


def test_values_are_unknown_when_a_command_fails_and_the_light_is_zero_when_off():
    cam = FakeReolink()
    caps = reolink.caps_from_probe([cam._one(c, True) for c in reolink.probe_commands()])
    run = lambda: reolink.values([cam._one(c, True) for c in reolink.poll_commands(caps)], caps)  # noqa: E731
    v, led = run()
    assert v == {"motion": False, "person": False, "vehicle": False, "light": False, "bri": 0} and led["bright"] == 100, "uit is helderheid 0, ook als de camera 100 onthoudt"
    cam.md = 1
    cam.alarm["people"] = 1
    cam.led.update(state=1, bright=40)
    v, _ = run()
    assert v["motion"] is True and v["person"] is True and v["vehicle"] is False and v["light"] is True and v["bri"] == 40
    cam.led = None  # de schijnwerper valt weg: alleen die punten zijn onbekend
    v, _ = run()
    assert v["motion"] is True and v["light"] is None and v["bri"] is None


def test_set_requests_are_absolute_and_keep_the_mode():
    caps = {"channel": 0, "model": "", "name": "", "serial": "", "fw": "", "ai": [], "led": True}
    led = {"mode": 3, "bright": 70}
    assert reolink.point_request(caps, "light", True, led) == {"cmd": "SetWhiteLed", "param": {"WhiteLed": {"channel": 0, "state": 1, "mode": 3, "bright": 70}}}
    assert reolink.point_request(caps, "light", False, led)["param"]["WhiteLed"]["state"] == 0
    assert reolink.point_request(caps, "bri", 0, led)["param"]["WhiteLed"]["state"] == 0
    assert reolink.point_request(caps, "bri", 35, led)["param"]["WhiteLed"] == {"channel": 0, "state": 1, "mode": 3, "bright": 35}
    assert reolink.point_request(caps, "light", True, {})["param"]["WhiteLed"] == {"channel": 0, "state": 1, "mode": 1, "bright": 100}, "zonder bekende stand: modus 1, helderheid 100"
    with pytest.raises(ValueError):
        reolink.point_request(caps, "motion", True, led)
    with pytest.raises(ValueError):
        reolink.point_request({**caps, "led": False}, "light", True, led)


def test_login_errors_and_an_expired_session_are_told_apart():
    with pytest.raises(reolink.ReoError) as e:
        reolink.parse_login([{"cmd": "Login", "code": 1, "error": {"detail": "login failed", "rspCode": -27}}])
    assert e.value.kind == "auth" and "login failed" in e.value.reason
    with pytest.raises(reolink.ReoError) as e:
        reolink.parse_login("niets")
    assert e.value.kind == "http"
    assert reolink.parse_login([{"cmd": "Login", "code": 0, "value": {"Token": {"name": "abc", "leaseTime": 3600}}}]) == ("abc", 3600)
    cam = FakeReolink()
    with pytest.raises(reolink.ReoError) as e:
        reolink.values([cam._one({"cmd": "GetMdState", "param": {}}, False)], {"channel": 0, "ai": [], "led": False})
    assert e.value.kind == "token"


# ------------------------------------------------------------ toevoegen


def test_adding_logs_in_looks_at_what_the_camera_can_and_never_shows_the_password(tmp_path):
    async def go():
        r, hub, cam, did = await rig(tmp_path)
        assert did == "reolink_abc123456" and hub.devices[did]["driver"] == "reolink" and hub.devices[did]["name"] == "Tuinhuis cam"
        assert set(pts(hub, did)) == {"motion", "person", "vehicle", "light", "bri"}
        assert hub.devices[did]["reo"]["model"] == "Reolink Duo Series P750" and hub.devices[did]["hook"]
        shown = json.dumps(hub.status())
        assert cam.password not in shown and '"auth"' not in shown, "het wachtwoord komt nergens naar de pagina"
        v = next(x for x in hub.status() if x["id"] == did)["reolink"]
        assert v["hook"].endswith(f"/reolink/{did}/{hub.devices[did]['hook']}") and v["hook_seen"] is False and v["caps"]["led"] is True
        with pytest.raises(ModelError, match="staat er al"):
            await hub.add_reolink("127.0.0.1", cam.port, cam.user, cam.password)
        await shutdown(hub, cam)
    asyncio.run(go())


def test_adding_says_why_it_did_not_work(tmp_path):
    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05, webhook_port=0)
        cam = await FakeReolink().start()
        for args, word in [(("127.0.0.1", cam.port, cam.user, "fout"), "weigert de aanmelding"), (("127.0.0.1", cam.port, "", "x"), "gebruikersnaam"),
                           (("127.0.0.1", cam.port, "a:b", "x"), "gebruikersnaam"), (("127.0.0.1", cam.port, "addon", ""), "wachtwoord"), (("http://x", 80, "a", "b"), "ongeldig adres"),
                           (("127.0.0.1", 0, "a", "b"), "ongeldige poort"), (("127.0.0.1", 1, "a", "b"), "geen verbinding")]:
            with pytest.raises(ModelError, match=word):
                await hub.add_reolink(*args)
        assert not hub.devices
        cam.down = True
        with pytest.raises(ModelError, match="HTTP 503"):
            await hub.add_reolink("127.0.0.1", cam.port, cam.user, cam.password)
        hub.close()
        await cam.stop()
    asyncio.run(go())


def test_a_camera_without_a_spotlight_gets_no_light_points(tmp_path):
    async def go():
        r, hub, cam, did = await rig(tmp_path, FakeReolink(led=False, ai=("people",)))
        assert set(pts(hub, did)) == {"motion", "person"}
        with pytest.raises(ModelError):
            await hub.set(did, "light", True)
        await shutdown(hub, cam)
    asyncio.run(go())


# ------------------------------------------------------------ standen, bedienen, sessie


def test_states_follow_the_camera_and_the_light_can_be_set(tmp_path):
    async def go():
        r, hub, cam, did = await rig(tmp_path)
        out = lambda p: hub.output(f"e:{did}:{p}")  # noqa: E731
        await eventually(lambda: out("motion") is False and out("light") is False and out("bri") == 0)
        cam.md = 1
        cam.alarm["vehicle"] = 1
        await eventually(lambda: out("motion") is True and out("vehicle") is True and out("person") is False)
        assert (await hub.set(did, "light", True))["result"] == "applied"
        await eventually(lambda: out("light") is True and out("bri") == 100)
        await hub.set(did, "bri", 35)
        await eventually(lambda: out("bri") == 35)
        await hub.set(did, "bri", 0)
        await eventually(lambda: out("light") is False and out("bri") == 0)
        sets = [p["WhiteLed"] for c, p in cam.calls if c == "SetWhiteLed"]
        assert [(w["state"], w["bright"]) for w in sets] == [(1, 100), (1, 35), (0, 35)] and all(w["mode"] == 1 and w["channel"] == 0 for w in sets)
        for pid, v, word in [("motion", True, "niet instelbaar"), ("light", 5, "aan of uit"), ("bri", 101, "tussen"), ("nergens", 1, "onbekend")]:
            with pytest.raises(ModelError, match=word):
                await hub.set(did, pid, v)
        await shutdown(hub, cam)
    asyncio.run(go())


def test_an_expired_session_logs_in_again_without_going_offline(tmp_path):
    async def go():
        r, hub, cam, did = await rig(tmp_path)
        n = cam.logins
        cam.expire()
        cam.md = 1
        await eventually(lambda: hub.output(f"e:{did}:motion") is True)
        assert cam.logins == n + 1 and did in hub.online, "opnieuw ingelogd, de camera bleef online"
        await shutdown(hub, cam)
    asyncio.run(go())


def test_a_camera_that_stops_answering_goes_offline_with_a_reason_and_comes_back(tmp_path):
    async def go():
        r, hub, cam, did = await rig(tmp_path)
        cam.down = True
        await eventually(lambda: did not in hub.online, 8)
        assert "503" in hub.errors[did]
        with pytest.raises(ModelError, match="geen verbinding"):
            await hub.set(did, "light", True)
        cam.down = False
        await eventually(lambda: did in hub.online, 8)
        await shutdown(hub, cam)
    asyncio.run(go())


def test_a_wrong_password_later_is_a_hard_error_and_does_not_hammer_the_camera(tmp_path):
    async def go():
        r, hub, cam, did = await rig(tmp_path)
        cam.password = "veranderd"
        cam.expire()
        await eventually(lambda: did not in hub.online, 8)
        assert "weigert de aanmelding" in hub.errors[did]
        await shutdown(hub, cam)
    asyncio.run(go())


# ------------------------------------------------------------ het webhook


def test_a_webhook_wakes_the_camera_task_and_is_checked(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(eh, "REO_FAST", 30.0)
        monkeypatch.setattr(eh, "REO_SLOW", 30.0)
        monkeypatch.setattr(eh, "REO_HOOKED", 30.0)
        monkeypatch.setattr(eh, "REO_HOLD", 0.0)
        r, hub, cam, did = await rig(tmp_path)
        out = lambda p: hub.output(f"e:{did}:{p}")  # noqa: E731
        await eventually(lambda: out("motion") is False)
        await eventually(lambda: hub.hook_port)
        url = f"http://127.0.0.1:{hub.hook_port}/reolink/{did}/{hub.devices[did]['hook']}"
        cam.md = 1
        await asyncio.sleep(0.3)
        assert out("motion") is False, "zonder webhook wacht de camera 30 s: de poll is traag"
        async with aiohttp.ClientSession() as http:
            a = await http.post(url, json={"alarm": {"type": "PEOPLE", "message": "Persoon"}, "type": "alarm", "secret": "x"})
            assert a.status == 200
            await eventually(lambda: out("motion") is True, 3)
            assert (await http.post(f"http://127.0.0.1:{hub.hook_port}/reolink/{did}/fout")).status == 404
            assert (await http.post(f"http://127.0.0.1:{hub.hook_port}/reolink/nergens/{hub.devices[did]['hook']}")).status == 404
            assert (await http.get(f"http://127.0.0.1:{hub.hook_port}/other")).status == 404
            hub.devices[did]["host"] = "10.9.9.9"  # een webhook van een ander adres dan de camera
            assert (await http.post(url)).status == 403
        v = hub.reo[did]
        assert v["hook_seen"] is True and "PEOPLE" in v["hook_last"]["body"]
        await shutdown(hub, cam)
    asyncio.run(go())


# ------------------------------------------------------------ opslag en web-API


def test_a_camera_survives_a_restart_with_its_secret_and_without_asking_again(tmp_path):
    async def go():
        r, hub, cam, did = await rig(tmp_path)
        secret = hub.devices[did]["hook"]
        raw = json.loads((tmp_path / "espdev.json").read_text(encoding="utf-8"))
        assert raw["devices"][did]["auth"] == f"{cam.user}:{cam.password}" and raw["devices"][did]["reo"]["ai"] == ["people", "vehicle"]
        hub.close()
        again = EspDevHub(tmp_path, r.engine, port=0, poll=0.05, webhook_port=0)
        assert again.devices[did]["hook"] == secret and again.devices[did]["driver"] == "reolink" and again.devices[did]["reo"]["led"] is True
        again.start()
        await eventually(lambda: did in again.online)
        assert again.output(f"e:{did}:motion") is False
        again.close()
        await cam.stop()
        await asyncio.sleep(0.05)
    asyncio.run(go())


def test_removing_a_camera_stops_its_task_and_forgets_it(tmp_path):
    async def go():
        r, hub, cam, did = await rig(tmp_path)
        assert await hub.remove(did)
        assert did not in hub.devices and did not in hub.reo and did not in hub.online and did not in hub.tasks
        n = len(cam.calls)
        await asyncio.sleep(0.3)
        assert len(cam.calls) == n, "geen verzoeken meer na het verwijderen"
        await shutdown(hub, cam)
    asyncio.run(go())


def test_the_web_api_adds_a_camera_and_never_returns_the_password(tmp_path):
    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05, webhook_port=0)
        cam = await FakeReolink().start()
        s = Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True)
        c = TestClient(TestServer(web.make_app(web.Ctx(s, r.engine, espdev=hub))))
        await c.start_server()
        try:
            bad = await c.post("/api/espdev/reolink", json={"host": "127.0.0.1", "port": cam.port, "user": cam.user, "password": "fout"})
            assert bad.status == 400 and "fout" not in await bad.text()
            ok = await c.post("/api/espdev/reolink", json={"host": "127.0.0.1", "port": cam.port, "user": cam.user, "password": cam.password, "name": "Tuinhuis"})
            body = await ok.text()
            assert ok.status == 200 and cam.password not in body
            did = json.loads(body)["id"]
            assert hub.devices[did]["name"] == "Tuinhuis"
            listed = await (await c.get("/api/espdev")).text()
            assert cam.password not in listed and '"auth"' not in listed
        finally:
            await c.close()
            hub.close()
            await cam.stop()
    asyncio.run(go())
