"""Een Reolink-camera op de tv-pagina: een opname via de add-on (de inloggegevens blijven daar), gedeeld tussen kijkers, met het geheim in het adres."""
import asyncio
import re
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kkaddon import espdevhub as eh
from kkaddon import tvhub
from kkaddon.espdevhub import EspDevHub
from kkaddon.model import ModelError
from kkaddon.reolink import ReoError
from kkaddon.tvhub import TvHub
from tests.fakereolink import FakeReolink
from tests.rig import KEY, Rig


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    for k in ("REO_FAST", "REO_SLOW", "REO_HOOKED"):
        monkeypatch.setattr(eh, k, 30.0)  # het polleren van standen hoort niet bij deze tests
    monkeypatch.setattr(eh, "TICK", 3600)


async def setup(tmp_path, cam=None, name="Voortuin cam"):
    r = Rig(tmp_path)
    r.engine.set_tz("UTC")
    hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05, webhook_port=0)
    cam = cam or FakeReolink()
    await cam.start()
    did = await hub.add_reolink("127.0.0.1", cam.port, cam.user, cam.password, name=name)
    end = time.monotonic() + 5
    while did not in hub.online and time.monotonic() < end:  # de eerste ronde van de hub zelf (die logt ook in) is dan voorbij
        await asyncio.sleep(0.02)
    return r, hub, cam, did


async def done(hub, cam):
    hub.close()
    await cam.stop()
    await asyncio.sleep(0.05)


# ------------------------------------------------------------ de opname


def test_a_snapshot_is_a_jpeg_and_the_camera_is_not_asked_more_often_than_needed():
    async def go(tmp_path):
        r, hub, cam, did = await setup(tmp_path)
        a = await hub.reolink_snapshot(did)
        assert a[:2] == b"\xff\xd8" and a == cam.snap_body and cam.snaps == 1
        assert await hub.reolink_snapshot(did) == a and cam.snaps == 1, "binnen anderhalve seconde: dezelfde opname"
        await asyncio.sleep(0.05)
        await hub.reolink_snapshot(did, max_age=0.01)
        assert cam.snaps == 2
        await done(hub, cam)
    asyncio.run(go(Path(__import__("tempfile").mkdtemp())))


def test_several_viewers_share_one_request_to_the_camera():
    async def go(tmp_path):
        r, hub, cam, did = await setup(tmp_path)
        got = await asyncio.gather(*[hub.reolink_snapshot(did, max_age=0.0) for _ in range(6)])
        assert len(set(got)) == 1 and cam.snaps == 1, "zes gelijktijdige vragen: een opname die al onderweg is, wordt afgewacht"
        await done(hub, cam)
    asyncio.run(go(Path(__import__("tempfile").mkdtemp())))


def test_the_snapshot_request_has_a_token_and_never_a_password():
    async def go(tmp_path):
        r, hub, cam, did = await setup(tmp_path)
        await hub.reolink_snapshot(did)
        p = cam.snap_params[-1]
        assert p["cmd"] == "Snap" and p["channel"] == "0" and p["token"] in cam.tokens
        assert "geheim" not in str(p) and cam.user not in str(p) and "password" not in str(p).lower(), "de inloggegevens gaan alleen naar de aanmelding"
        await done(hub, cam)
    asyncio.run(go(Path(__import__("tempfile").mkdtemp())))


def test_an_expired_token_logs_in_again_once_and_carries_on():
    async def go(tmp_path):
        r, hub, cam, did = await setup(tmp_path)
        await hub.reolink_snapshot(did, max_age=0.0)
        logins = cam.logins
        cam.expire()
        assert (await hub.reolink_snapshot(did, max_age=0.0)) == cam.snap_body
        assert cam.logins == logins + 1, "de camera antwoordde met een JSON-fout: opnieuw inloggen en nogmaals vragen"
        await done(hub, cam)
    asyncio.run(go(Path(__import__("tempfile").mkdtemp())))


def test_a_camera_that_is_down_or_gives_no_picture_is_an_error_not_a_hang(monkeypatch):
    async def go(tmp_path):
        r, hub, cam, did = await setup(tmp_path)
        cam.down = True
        with pytest.raises(ReoError):
            await hub.reolink_snapshot(did, max_age=0.0)
        cam.down = False
        cam.snap_body = b"<html>geen beeld</html>"
        with pytest.raises(ReoError, match="geen afbeelding"):
            await hub.reolink_snapshot(did, max_age=0.0)
        cam.snap_body = b"\xff\xd8" + b"x" * 5000
        monkeypatch.setattr(eh, "SNAP_MAX", 1000)
        with pytest.raises(ReoError, match="te groot"):
            await hub.reolink_snapshot(did, max_age=0.0)
        with pytest.raises(ReoError, match="geen Reolink"):
            await hub.reolink_snapshot("bestaatniet")
        assert did not in hub._snap_wait, "een mislukte opname laat niets hangen"
        await done(hub, cam)
    asyncio.run(go(Path(__import__("tempfile").mkdtemp())))


# ------------------------------------------------------------ de configuratie


def test_camera_config_is_checked(tmp_path):
    r = Rig(tmp_path)
    hub = TvHub(tmp_path, r.engine, port=0)
    hub.update({"cameras": [{"device": "voortuin_cam", "label": "Voortuin"}, {"device": "", "label": "leeg"}]})
    assert hub.cfg["cameras"] == [{"label": "Voortuin", "device": "voortuin_cam"}], "een rij zonder camera telt niet"
    for bad in ({"cameras": [{"device": "Niet Geldig!"}]}, {"cameras": [{"device": "a"}] * 5}):
        with pytest.raises(ModelError):
            hub.update(bad)
    assert TvHub(tmp_path, Rig(tmp_path).engine, port=0).cfg["cameras"][0]["device"] == "voortuin_cam"


def test_the_data_lists_the_cameras_with_their_names_and_no_address(tmp_path):
    async def go():
        r, hub, cam, did = await setup(tmp_path)
        tv = TvHub(tmp_path, r.engine, port=0)
        tv.espdev = hub
        tv.update({"cameras": [{"device": did, "label": ""}, {"device": "weg", "label": "Terras"}]})
        d = tv.data()
        assert d["cameras"] == [{"n": 0, "label": "Voortuin cam"}, {"n": 1, "label": "Terras"}], "zonder eigen naam de naam van het apparaat"
        assert "127.0.0.1" not in str(d) and cam.password not in str(d) and str(cam.port) not in str(d)
        assert tv.sources([])["cameras"] == [[did, "Voortuin cam"]], "alleen Reolink-camera's staan in de keuze"
        await done(hub, cam)
    asyncio.run(go())


# ------------------------------------------------------------ het adres met het geheim


def test_the_picture_is_served_only_with_the_secret_and_errors_become_a_message(tmp_path):
    async def go():
        r, hub, cam, did = await setup(tmp_path)
        tv = TvHub(tmp_path, r.engine, port=0)
        tv.espdev = hub
        tv.update({"cameras": [{"device": did, "label": "Voortuin"}]})
        app = web.Application()
        tvhub.routes(app, tv, "/tv/{token}", check=True)
        c = TestClient(TestServer(app))
        await c.start_server()
        try:
            tok = tv.cfg["token"]
            img = await c.get(f"/tv/{tok}/cam/0.jpg")
            body = await img.read()
            assert img.status == 200 and img.headers["Content-Type"] == "image/jpeg" and body == cam.snap_body and img.headers["Cache-Control"] == "no-store"
            assert (await c.get(f"/tv/{tok[:-1]}x/cam/0.jpg")).status == 404, "zonder het geheim geen beeld"
            assert (await c.get(f"/tv/{tok}/cam/3.jpg")).status == 404, "een camera die niet bestaat"
            assert (await c.get(f"/tv/{tok}/cam/abc.jpg")).status == 404
            cam.down = True
            hub._snaps.clear()
            bad = await c.get(f"/tv/{tok}/cam/0.jpg")
            assert bad.status == 503 and tv.errors["camera 1"] and tv.view()["errors"]["camera 1"], "de fout komt in het tabblad"
            assert "127.0.0.1" not in await bad.text(), "de tv krijgt het adres van de camera niet te zien"
            cam.down = False
            hub._snaps.clear()
            assert (await c.get(f"/tv/{tok}/cam/0.jpg")).status == 200 and "camera 1" not in tv.errors, "weer bereikbaar: de fout verdwijnt"
        finally:
            await c.close()
            await done(hub, cam)
    asyncio.run(go())


def test_without_espdev_the_picture_is_a_message_not_a_crash(tmp_path):
    async def go():
        r = Rig(tmp_path)
        tv = TvHub(tmp_path, r.engine, port=0)
        tv.update({"cameras": [{"device": "x", "label": "A"}]})
        with pytest.raises(ReoError):
            await tv.camera_snapshot(0)
        with pytest.raises(IndexError):
            await tv.camera_snapshot(1)
    asyncio.run(go())


def test_the_preview_behind_ingress_serves_the_picture_too(tmp_path):
    from kkaddon import web as kweb
    from kkaddon.settings import Settings

    async def go():
        r, hub, cam, did = await setup(tmp_path)
        tv = TvHub(tmp_path, r.engine, port=0)
        tv.espdev = hub
        tv.update({"cameras": [{"device": did, "label": "V"}]})
        ctx = kweb.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), r.engine, tv=tv)
        c = TestClient(TestServer(kweb.make_app(ctx)))
        await c.start_server()
        try:
            assert (await c.get("/tv/view/cam/0.jpg")).status == 200
            assert (await (await c.get("/api/tv/sources")).json())["cameras"] == [[did, "Voortuin cam"]]
        finally:
            await c.close()
            await done(hub, cam)
    asyncio.run(go())


# ------------------------------------------------------------ de pagina


def test_the_page_refreshes_the_picture_and_keeps_the_last_frame():
    js = (Path(__file__).parent.parent / "kkaddon" / "static" / "tv" / "display.js").read_text(encoding="utf-8")
    assert "cam/${n}.jpg" in js and "setInterval(camTick, 2000)" in js, "elke 2 seconden"
    assert "camFrames[c.n]" in js and "URL.revokeObjectURL(old)" in js, "het laatste beeld blijft staan en het geheugen loopt niet vol"
    assert re.search(r"document\.hidden", js), "een tv die niet kijkt, vraagt de camera niet"
