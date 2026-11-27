"""Volledig scherm op de tv: de browser van de tv toont zijn adresbalk tot de pagina volledig scherm krijgt, en dat mag alleen na een klik met de
aanwijzer. De client stuurt die klik via de aanwijzer-socket van webOS; de hub doet dat 5 seconden na het openen van het dashboard, en alleen als
de browser dan nog vooraan staat."""
import asyncio
import re
import time
from pathlib import Path

import pytest

from kkaddon import lg, lghub
from kkaddon.model import ModelError
from tests.faketv import KEY, POINTER, FakeTv
from tests.rig import Rig
from tests.test_lghub import MAC, URL, StubClient, hub_with  # noqa: F401  (woke is een fixture)
from tests.test_lghub import woke  # noqa: F401


async def eventually(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("duurde te lang")


def with_clicks(tmp_path, **b):
    r, hub, did, c = hub_with(tmp_path, **b)
    return r, hub, did, c


# ------------------------------------------------------------ de client


def test_click_goes_to_the_pointer_socket_as_a_click():
    async def go():
        tv = await FakeTv().start()
        c = lg.LgClient("127.0.0.1", KEY, port=tv.port, scheme="ws", answer=1.0, retry=(0.05, 0.1))
        t = asyncio.get_running_loop().create_task(c.run())
        await eventually(lambda: c.connected)
        await c.click()
        await eventually(lambda: tv.pointer)
        assert tv.pointer == ["type:click\n\n"], "zoals de LG-app op een telefoon"
        t.cancel()
        await tv.stop()
    asyncio.run(go())


def test_click_soon_clicks_after_the_delay_only_if_it_still_applies():
    async def go():
        tv = await FakeTv().start()
        c = lg.LgClient("127.0.0.1", KEY, port=tv.port, scheme="ws", answer=1.0, retry=(0.05, 0.1))
        t = asyncio.get_running_loop().create_task(c.run())
        await eventually(lambda: c.connected)
        assert c.click_soon(0.05, lambda: False)
        await asyncio.sleep(0.4)
        assert tv.pointer == [], "de browser staat niet meer vooraan: niet klikken, want dat kan een film zijn"
        assert c.click_soon(0.05, lambda: True) and c.click_soon(0.05)
        await eventually(lambda: len(tv.pointer) == 2)
        assert c.click_error == ""
        t.cancel()
        await tv.stop()
    asyncio.run(go())


def test_a_missing_permission_is_remembered_so_the_page_can_say_how_to_fix_it():
    async def go():
        tv = await FakeTv().start()
        tv.errors[POINTER] = "401 insufficient permissions"  # een koppeling van voor het recht CONTROL_MOUSE_AND_KEYBOARD
        c = lg.LgClient("127.0.0.1", KEY, port=tv.port, scheme="ws", answer=1.0, retry=(0.05, 0.1))
        t = asyncio.get_running_loop().create_task(c.run())
        await eventually(lambda: c.connected)
        assert c.click_soon(0.01)
        await eventually(lambda: "401" in c.click_error)
        assert tv.pointer == [] and c.connected, "de verbinding blijft, alleen de klik mislukt"
        del tv.errors[POINTER]
        assert c.click_soon(0.01)
        await eventually(lambda: c.click_error == "" and tv.pointer)
        t.cancel()
        await tv.stop()
    asyncio.run(go())


def test_click_soon_is_refused_without_a_link():
    assert lg.LgClient("127.0.0.1").click_soon(0.1) is False


def test_manifest_asks_for_the_permission_to_click():
    assert "CONTROL_MOUSE_AND_KEYBOARD" in lg.MANIFEST["permissions"]


# ------------------------------------------------------------ de hub


def test_dashboard_clicks_for_fullscreen_once_it_opened(tmp_path, woke):
    r, hub, did, c = with_clicks(tmp_path)
    val = {"m:toon": False}
    r.engine.ref_value = lambda ref: val.get(ref)
    hub.devices[did]["inputs"] = {"show": "m:toon"}
    hub.tick()
    assert c.clicks == []
    val["m:toon"] = True
    hub.tick()
    assert c.sent == [("open", URL)] and len(c.clicks) == 1
    delay, guard = c.clicks[0]
    assert delay == lghub.FS_DELAY
    assert guard() is False, "zolang de tv nog op het beginscherm staat, niet klikken"
    hub._on_state(did, {"app": "com.webos.app.browser"})
    assert guard() is True
    hub._on_state(did, {"app": "netflix"})
    assert guard() is False, "iemand begon te kijken: de klik gaat niet naar een film"


def test_no_click_when_fullscreen_is_switched_off_or_the_dashboard_did_not_open(tmp_path, woke):
    r, hub, did, c = with_clicks(tmp_path, fullscreen=False)
    assert hub.status()[0]["fullscreen"] is False
    hub.show(did)
    assert c.sent == [("open", URL)] and c.clicks == []
    (tmp_path / "b").mkdir()
    r2, hub2, did2, c2 = with_clicks(tmp_path / "b")
    c2.ok = False  # de tv nam het bevel niet aan
    hub2.show(did2)
    assert c2.sent == [] and c2.clicks == [], "geen klik zonder dat het dashboard geopend werd"


def test_fullscreen_is_on_by_default_can_be_changed_and_exposes_the_click_error(tmp_path):
    _, hub, did, c = with_clicks(tmp_path)
    assert hub.status()[0]["fullscreen"] is True and hub.status()[0]["click_error"] == ""
    hub.update(did, {"fullscreen": False})
    assert hub.devices[did]["fullscreen"] is False and hub.status()[0]["fullscreen"] is False
    hub.update(did, {"fullscreen": True})
    assert hub.devices[did]["fullscreen"] is True
    c.click_error = "401 insufficient permissions"
    assert hub.status()[0]["click_error"] == "401 insufficient permissions"


def test_a_saved_tv_from_before_fullscreen_clicks_too(tmp_path):
    from kkaddon.lghub import LgHub
    assert LgHub._defaults({"name": "Oud", "host": "1.2.3.4"})["fullscreen"] is True
    assert LgHub._defaults({"name": "Oud", "fullscreen": False})["fullscreen"] is False


def test_end_to_end_the_fake_tv_receives_the_click_after_the_page_opened(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(lghub, "FS_DELAY", 0.1)
        tv = await FakeTv().start()
        r = Rig(tmp_path)
        hub = lghub.LgHub(tmp_path, r.engine, port=tv.port, scheme="ws")
        did = hub.add({"name": "TV", "host": "127.0.0.1", "url": URL})
        await eventually(lambda: hub.is_online(did) and hub.state(did)["app"] == "com.webos.app.home")
        hub.show(did)
        await eventually(lambda: hub.state(did)["app"] == "com.webos.app.browser")
        await eventually(lambda: tv.pointer)
        assert tv.pointer == ["type:click\n\n"]
        hub.close()
        await tv.stop()
    asyncio.run(go())


# ------------------------------------------------------------ de pagina


def test_the_page_asks_for_fullscreen_on_a_click_and_not_inside_the_preview():
    js = (Path(__file__).parent.parent / "kkaddon" / "static" / "tv" / "display.js").read_text(encoding="utf-8")
    assert re.search(r"for \(const ev of \['pointerdown', 'click', 'keydown'\]\) addEventListener\(ev, goFull", js)
    assert "requestFullscreen || el.webkitRequestFullscreen" in js, "de tv-browser kent beide namen"
    assert "const isFrame = window.self !== window.top" in js and "if (isFrame || fsElement()) return;" in js, "geen volledig scherm in de voorvertoning"
