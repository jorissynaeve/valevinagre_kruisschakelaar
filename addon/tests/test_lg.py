"""LG-tv's met webOS: de client tegen een nagebootste tv over een echte websocket."""
import asyncio
import time

import pytest

from kkaddon import lg
from tests.faketv import FOREGROUND, KEY, FakeTv


async def eventually(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("duurde te lang")


def client(tv, **kw):
    seen: dict = {"state": {}, "keys": [], "links": []}

    def on_state(v):
        seen["state"].update(v)
    c = lg.LgClient("127.0.0.1", port=tv.port, scheme="ws", on_state=on_state, on_key=seen["keys"].append, on_link=seen["links"].append,
                    answer=1.0, retry=(0.05, 0.1), **kw)
    return c, seen


# ------------------------------------------------------------ puur


def test_magic_packet_is_six_ff_and_sixteen_macs():
    p = lg.magic_packet("C8:F7:42:A1:B2:C3")
    assert p[:6] == b"\xff" * 6 and p[6:] == bytes.fromhex("c8f742a1b2c3") * 16 and len(p) == 102


@pytest.mark.parametrize("mac", ["", "c8f742a1b2", "zz:zz:zz:zz:zz:zz"])
def test_magic_packet_refuses_a_bad_mac(mac):
    with pytest.raises(lg.LgError):
        lg.magic_packet(mac)


def test_manifest_asks_for_the_screen_permission_and_is_not_signed():
    # zonder CONTROL_TV_SCREEN antwoordt de tv op turnOffScreen "401 insufficient permissions"; het ondertekende voorbeeldmanifest van LG
    # (com.lge.test) weigert een tv met webOS 25 ("blacklisted certificate"). Beide zijn op de echte tv vastgesteld.
    assert "CONTROL_TV_SCREEN" in lg.MANIFEST["permissions"] and "CONTROL_POWER" in lg.MANIFEST["permissions"]
    assert "signed" not in lg.MANIFEST and "signatures" not in lg.MANIFEST


def test_volume_is_read_from_both_shapes():
    assert lg.volume_of({"volumeStatus": {"volume": 7, "muteStatus": True}}) == {"volume": 7, "muted": True}
    assert lg.volume_of({"volume": 9, "mute": False}) == {"volume": 9, "muted": False}
    assert lg.volume_of({"volume": True}) == {}


# ------------------------------------------------------------ verbinden


def test_connects_with_a_known_key_and_follows_the_state():
    async def go():
        tv = await FakeTv().start()
        c, seen = client(tv, key=KEY)
        t = asyncio.get_running_loop().create_task(c.run())
        await eventually(lambda: c.connected)
        assert tv.registers[0]["client-key"] == KEY and tv.registers[0]["pairingType"] == "PROMPT"
        await eventually(lambda: seen["state"].get("volume") == 12)
        assert seen["state"] == {"app": "com.webos.app.home", "power": "Active", "volume": 12, "muted": False}
        await tv.change(app="netflix", power="Screen Off")
        await eventually(lambda: seen["state"]["app"] == "netflix")
        assert seen["state"]["power"] == "Screen Off"
        assert seen["keys"] == [] and seen["links"] == [True], "een bekende sleutel wordt niet opnieuw bewaard"
        t.cancel()
        await tv.stop()
    asyncio.run(go())


def test_pairing_waits_for_the_prompt_and_keeps_the_key():
    async def go():
        tv = await FakeTv(pairing=True).start()
        c, seen = client(tv)
        t = asyncio.get_running_loop().create_task(c.run())
        await eventually(lambda: c.pairing)
        assert not c.connected and "client-key" not in tv.registers[0]
        tv.accepted.set()
        await eventually(lambda: c.connected)
        assert not c.pairing and c.key == KEY and seen["keys"] == [KEY]
        t.cancel()
        await tv.stop()
    asyncio.run(go())


def test_a_rejected_pairing_is_reported_and_not_repeated():
    async def go():
        tv = await FakeTv(pairing=True).start()
        tv.reject = True
        c, _ = client(tv)
        t = asyncio.get_running_loop().create_task(c.run())
        await eventually(lambda: c.pairing)
        tv.accepted.set()
        await t  # de run stopt: de tv krijgt niet telkens opnieuw de melding
        assert c.rejected and "rejected" in c.last_error and not c.connected
        assert len(tv.registers) == 1
        await tv.stop()
    asyncio.run(go())


def test_a_wrong_key_makes_the_tv_ask_again():
    async def go():
        tv = await FakeTv(pairing=True).start()
        c, _ = client(tv, key="stale")
        t = asyncio.get_running_loop().create_task(c.run())
        await eventually(lambda: c.pairing)
        assert tv.registers[0]["client-key"] == "stale"
        tv.accepted.set()
        await eventually(lambda: c.connected)
        assert c.key == KEY
        t.cancel()
        await tv.stop()
    asyncio.run(go())


def test_a_tv_that_is_off_is_not_connected_and_comes_back():
    async def go():
        tv = await FakeTv().start()
        port = tv.port
        c, seen = client(tv, key=KEY)
        t = asyncio.get_running_loop().create_task(c.run())
        await eventually(lambda: c.connected)
        await tv.stop()
        await eventually(lambda: not c.connected)
        assert seen["links"][-1] is False
        tv2 = await FakeTv().start(port)  # de tv gaat weer aan
        await eventually(lambda: c.connected, 5)
        t.cancel()
        await tv2.stop()
    asyncio.run(go())


def test_nothing_listening_gives_an_error_not_a_crash():
    async def go():
        c = lg.LgClient("127.0.0.1", port=9, scheme="ws", retry=(0.05, 0.1))
        t = asyncio.get_running_loop().create_task(c.run())
        await eventually(lambda: bool(c.last_error))
        assert not c.connected
        t.cancel()
    asyncio.run(go())


# ------------------------------------------------------------ bevelen


def test_commands_reach_the_tv_with_their_payload():
    async def go():
        tv = await FakeTv().start()
        c, _ = client(tv, key=KEY)
        t = asyncio.get_running_loop().create_task(c.run())
        await eventually(lambda: c.connected)
        assert c.open_url("http://ha.local:8123/dashboard") and c.screen(False) and c.screen(True) and c.power_off() and c.launch("netflix")
        await eventually(lambda: len(tv.requests) == 5)
        assert tv.requests == [("ssap://system.launcher/open", {"target": "http://ha.local:8123/dashboard"}),
                               ("ssap://com.webos.service.tvpower/power/turnOffScreen", None),
                               ("ssap://com.webos.service.tvpower/power/turnOnScreen", None),
                               ("ssap://system/turnOff", None), ("ssap://system.launcher/launch", {"id": "netflix"})]
        t.cancel()
        await tv.stop()
    asyncio.run(go())


def test_a_command_the_tv_refuses_is_noted_and_the_link_stays():
    async def go():
        tv = await FakeTv().start()
        tv.errors[lg.SCREEN_OFF] = "401 insufficient permissions"
        c, _ = client(tv, key=KEY)
        t = asyncio.get_running_loop().create_task(c.run())
        await eventually(lambda: c.connected)
        assert c.screen(False)
        await eventually(lambda: "insufficient" in c.last_error)
        assert c.connected
        t.cancel()
        await tv.stop()
    asyncio.run(go())


def test_commands_are_refused_without_a_link():
    c = lg.LgClient("127.0.0.1")
    assert not c.open_url("http://x") and not c.screen(False) and not c.power_off()


def test_foreground_app_changes_are_followed():
    async def go():
        tv = await FakeTv().start()
        c, seen = client(tv, key=KEY)
        t = asyncio.get_running_loop().create_task(c.run())
        await eventually(lambda: c.connected)
        await eventually(lambda: "app" in seen["state"])
        await tv.change(app="com.webos.app.browser")
        await eventually(lambda: seen["state"]["app"] == "com.webos.app.browser")
        assert FOREGROUND in [u for _, _, u in tv.subs]
        t.cancel()
        await tv.stop()
    asyncio.run(go())
