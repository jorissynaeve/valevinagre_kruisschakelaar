"""LG-tv's als blok op het schema: de stand, de ingangen (aan en dashboard), de uitgangen in de logica, het schema en de web-API."""
import asyncio
import json
import time

import pytest

from kkaddon import lghub, schema
from kkaddon.lghub import LgHub
from kkaddon.model import ModelError
from tests.faketv import KEY, FakeTv
from tests.rig import Rig

URL = "http://homeassistant.local:8123/dashboard-tv/0"
MAC = "c8f742a1b2c3"


class StubClient:
    """Wat de hub van een LgClient gebruikt, zonder netwerk."""

    def __init__(self):
        self.connected, self.pairing, self.rejected, self.last_error = True, False, False, ""
        self.sent: list = []
        self.ok = True
        self.click_error = ""
        self.clicks: list = []  # (wachttijd, bewaking) van elke klik voor volledig scherm

    def _do(self, *what) -> bool:
        if self.ok and self.connected:
            self.sent.append(what)
            return True
        return False

    def click_soon(self, delay, only_if=None):
        if not self.connected:
            return False
        self.clicks.append((delay, only_if))
        return True

    def power_off(self): return self._do("off")
    def screen(self, on): return self._do("screen", on)
    def open_url(self, url): return self._do("open", url)
    def launch(self, app): return self._do("launch", app)


@pytest.fixture
def woke(monkeypatch):
    calls = []
    monkeypatch.setattr(lghub, "wake", lambda mac, *a, **k: calls.append(mac) or True)
    return calls


def hub_with(tmp_path, **b):
    r = Rig(tmp_path)
    hub = LgHub(tmp_path, r.engine)
    did = hub.add({"name": "Woonkamer tv", "mac": MAC, "url": URL, **b})  # zonder adres: er start geen verbinding
    hub.devices[did].update(host="192.0.2.50", key=KEY)
    hub.clients[did] = c = StubClient()
    hub._on_state(did, {"app": "com.webos.app.home", "power": "Active"})
    return r, hub, did, c


# ------------------------------------------------------------ de stand


def test_state_follows_what_the_tv_reports(tmp_path):
    r, hub, did, c = hub_with(tmp_path)
    assert hub.state(did)["on"] is True and hub.state(did)["watching"] is False, "het beginscherm is niet kijken"
    hub._on_state(did, {"app": "netflix"})
    assert hub.state(did)["watching"] is True
    hub._on_state(did, {"app": "com.webos.app.browser"})
    assert hub.state(did)["watching"] is False, "het dashboard zelf is geen kijken"
    hub._on_state(did, {"power": "Screen Off"})
    s = hub.state(did)
    assert s["on"] is False and s["screen_off"] is True and s["watching"] is False
    hub._on_state(did, {"power": "Active Standby"})
    assert hub.state(did)["on"] is False
    assert r.engine.ref_value(f"l:{did}:on") is False and r.engine.ref_value(f"l:{did}:watch") is False


def test_a_tv_that_is_off_is_off_but_one_never_paired_is_unknown(tmp_path):
    r, hub, did, c = hub_with(tmp_path)
    c.connected = False
    assert hub.state(did)["on"] is False and r.engine.ref_value(f"l:{did}:watch") is False, "een tv die uit staat luistert niet meer"
    hub.devices[did]["key"] = ""
    assert hub.state(did)["on"] is None and r.engine.ref_value(f"l:{did}:on") is None, "nog nooit gekoppeld: we weten het niet"
    assert r.engine.ref_value("l:weg:on") is None


def test_custom_own_apps_decide_what_is_watching(tmp_path):
    _, hub, did, _ = hub_with(tmp_path)
    hub.update(did, {"apps": "com.webos.app.hdmi3 com.webos.app.home"})
    hub._on_state(did, {"app": "com.webos.app.hdmi3"})
    assert hub.state(did)["watching"] is False
    hub._on_state(did, {"app": "com.webos.app.browser"})
    assert hub.state(did)["watching"] is True, "de browser stond niet meer in de lijst"
    with pytest.raises(ModelError, match="app-id"):
        hub.update(did, {"apps": "geen geldig id!"})


# ------------------------------------------------------------ de ingangen


def test_on_input_acts_on_a_change_only(tmp_path, woke):
    r, hub, did, c = hub_with(tmp_path)
    val = {"m:aan": False}
    r.engine.ref_value = lambda ref: val.get(ref)
    hub.devices[did]["inputs"] = {"on": "m:aan"}
    hub.tick()
    assert c.sent == [] and woke == [], "de eerste stand wordt alleen onthouden: een herstart zet niets aan of uit"
    val["m:aan"] = True
    hub.tick()
    assert c.sent == [] and woke == [], "hij is al aan: niets te doen"
    hub._on_state(did, {"power": "Screen Off"})
    val["m:aan"] = False
    hub.tick()
    assert c.sent == [("off",)]
    c.connected = False
    val["m:aan"] = True
    hub.tick()
    assert woke == [MAC], "uit en onbereikbaar: Wake-on-LAN"
    val["m:aan"] = None
    hub.tick()
    val["m:aan"] = True
    hub.tick()
    assert woke == [MAC], "na onbekend is de stand weer een eerste stand"


def test_on_wakes_a_screen_that_was_only_off(tmp_path, woke):
    r, hub, did, c = hub_with(tmp_path)
    val = {"m:aan": False}
    r.engine.ref_value = lambda ref: val.get(ref)
    hub.devices[did]["inputs"] = {"on": "m:aan"}
    hub.tick()
    hub._on_state(did, {"power": "Screen Off"})
    val["m:aan"] = True
    hub.tick()
    assert c.sent == [("screen", True)] and woke == []


def test_wake_needs_a_mac_and_a_failed_input_is_retried_later(tmp_path, woke, monkeypatch):
    r, hub, did, c = hub_with(tmp_path, mac="")
    now = [100.0]
    monkeypatch.setattr("kkaddon.lghub.time.monotonic", lambda: now[0])
    val = {"m:aan": False}
    r.engine.ref_value = lambda ref: val.get(ref)
    hub.devices[did]["inputs"] = {"on": "m:aan"}
    hub.tick()
    c.connected = False
    val["m:aan"] = True
    hub.tick()
    now[0] += 1
    hub.tick()
    assert woke == [] and "niet verstuurd" in hub.status()[0]["last"]["on"]
    hub.devices[did]["mac"] = MAC
    now[0] += 30
    hub.tick()
    assert woke == [MAC], "na de wachttijd opnieuw"


def test_dashboard_opens_the_url_when_the_tv_is_on(tmp_path, woke):
    r, hub, did, c = hub_with(tmp_path)
    val = {"m:toon": False}
    r.engine.ref_value = lambda ref: val.get(ref)
    hub.devices[did]["inputs"] = {"show": "m:toon"}
    hub.tick()
    val["m:toon"] = True
    hub.tick()
    assert c.sent == [("open", URL)] and woke == [] and did not in hub.pending
    assert "geopend" in hub.status()[0]["last"]["show"]


def test_dashboard_wakes_the_tv_waits_and_then_opens(tmp_path, woke, monkeypatch):
    r, hub, did, c = hub_with(tmp_path)
    now = [100.0]
    monkeypatch.setattr("kkaddon.lghub.time.monotonic", lambda: now[0])
    val = {"m:toon": False}
    r.engine.ref_value = lambda ref: val.get(ref)
    hub.devices[did]["inputs"] = {"show": "m:toon"}
    hub.tick()
    c.connected = False
    hub.values.pop(did)
    val["m:toon"] = True
    hub.tick()
    assert woke == [MAC] and c.sent == [] and did in hub.pending
    now[0] += 5
    hub.tick()
    assert woke == [MAC], "niet elke seconde een pakket"
    now[0] += 12
    hub.tick()
    assert woke == [MAC, MAC]
    c.connected = True
    hub._on_state(did, {"power": "Active", "app": "com.webos.app.home"})
    now[0] += 1
    hub.tick()
    assert c.sent == [("open", URL)] and did not in hub.pending


def test_dashboard_gives_up_when_the_tv_never_answers(tmp_path, woke, monkeypatch):
    r, hub, did, c = hub_with(tmp_path)
    now = [100.0]
    monkeypatch.setattr("kkaddon.lghub.time.monotonic", lambda: now[0])
    val = {"m:toon": False}
    r.engine.ref_value = lambda ref: val.get(ref)
    hub.devices[did]["inputs"] = {"show": "m:toon"}
    hub.tick()
    c.connected = False
    val["m:toon"] = True
    hub.tick()
    now[0] += lghub.SHOW_FOR + 1
    hub.tick()
    assert did not in hub.pending and "opgegeven" in hub.status()[0]["last"]["show"]


def test_dashboard_lights_a_dark_screen_before_opening(tmp_path, woke):
    r, hub, did, c = hub_with(tmp_path)
    val = {"m:toon": False}
    r.engine.ref_value = lambda ref: val.get(ref)
    hub.devices[did]["inputs"] = {"show": "m:toon"}
    hub.tick()
    hub._on_state(did, {"power": "Screen Off"})
    val["m:toon"] = True
    hub.tick()
    assert c.sent == [("screen", True)] and did in hub.pending
    hub._on_state(did, {"power": "Active"})
    hub.tick()
    assert c.sent[-1] == ("open", URL)


@pytest.mark.parametrize("mode,want", [("screen", ("screen", False)), ("power", ("off",)), ("home", ("launch", "com.webos.app.home"))])
def test_removing_the_dashboard_follows_the_chosen_way(tmp_path, woke, mode, want):
    r, hub, did, c = hub_with(tmp_path, off_mode=mode)
    hub._on_state(did, {"app": "com.webos.app.browser"})
    val = {"m:toon": True}
    r.engine.ref_value = lambda ref: val.get(ref)
    hub.devices[did]["inputs"] = {"show": "m:toon"}
    hub.tick()
    val["m:toon"] = False
    hub.tick()
    assert c.sent == [want]


def test_removing_the_dashboard_never_interrupts_someone_watching(tmp_path, woke):
    r, hub, did, c = hub_with(tmp_path)
    hub._on_state(did, {"app": "netflix"})
    val = {"m:toon": True}
    r.engine.ref_value = lambda ref: val.get(ref)
    hub.devices[did]["inputs"] = {"show": "m:toon"}
    hub.tick()
    val["m:toon"] = False
    hub.tick()
    assert c.sent == [] and "bekeken" in hub.status()[0]["last"]["show"]


def test_showing_without_a_url_says_so_and_does_nothing(tmp_path, woke):
    r, hub, did, c = hub_with(tmp_path, url="")
    val = {"m:toon": False}
    r.engine.ref_value = lambda ref: val.get(ref)
    hub.devices[did]["inputs"] = {"show": "m:toon"}
    hub.tick()
    val["m:toon"] = True
    hub.tick()
    assert c.sent == [] and "adres" in hub.status()[0]["last"]["show"] and did not in hub.pending


# ------------------------------------------------------------ toestellen beheren


@pytest.mark.parametrize("bad,word", [({"host": "http://x"}, "IP"), ({"mac": "12"}, "MAC"), ({"url": "ftp://x"}, "http"), ({"name": " "}, "naam"),
                                      ({"off_mode": "boem"}, "keuze")])
def test_bad_input_is_refused(tmp_path, bad, word):
    _, hub, did, _ = hub_with(tmp_path)
    with pytest.raises(ModelError, match=word):
        hub.update(did, bad)


def test_input_sources_are_checked(tmp_path):
    r, hub, did, _ = hub_with(tmp_path)
    mid = r.model.add_module("number", "Doel", 21)
    with pytest.raises(ModelError, match="bestaat"):
        hub.update(did, {"inputs": {"on": "m:weg"}})
    with pytest.raises(ModelError, match="zichzelf"):
        hub.update(did, {"inputs": {"on": f"l:{did}:on"}})
    with pytest.raises(ModelError, match="0 of 1"):
        hub.update(did, {"inputs": {"show": f"m:{mid}"}})
    hub.update(did, {"inputs": {"show": "l:ander:watch"}})
    assert hub.devices[did]["inputs"] == {"show": "l:ander:watch"}
    hub.update(did, {"inputs": {"show": None}})
    assert hub.devices[did]["inputs"] == {}


def test_key_never_leaves_the_add_on_and_the_file_is_private(tmp_path):
    r, hub, did, _ = hub_with(tmp_path)
    hub._on_key(did, KEY)
    assert KEY not in json.dumps(hub.status()) and hub.status()[0]["has_key"] is True
    b = next(x for x in schema.view(r.model, r.engine.snapshot())["blocks"] if x["key"] == f"l:{did}")
    assert KEY not in json.dumps(b)
    assert json.loads((tmp_path / "lgtv.json").read_text())["devices"][did]["key"] == KEY
    assert LgHub(tmp_path, Rig(tmp_path).engine).devices[did]["key"] == KEY, "na een herstart is hij er nog"
    import os
    import stat
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "lgtv.json").stat().st_mode) == 0o600


def test_another_host_or_rebind_forgets_the_key(tmp_path, monkeypatch):
    _, hub, did, _ = hub_with(tmp_path)
    monkeypatch.setattr(hub, "_start", lambda _did: None)  # een nieuw adres start een verbinding, en dat vraagt een event loop
    hub.update(did, {"name": "Andere naam"})
    assert hub.devices[did]["key"] == KEY
    hub.update(did, {"host": "192.0.2.77"})
    assert hub.devices[did]["key"] == "", "een andere tv kent de sleutel niet"


def test_key_cannot_be_set_through_the_api_body(tmp_path):
    _, hub, did, _ = hub_with(tmp_path)
    hub.update(did, {"key": "evil"})
    assert hub.devices[did]["key"] == KEY


# ------------------------------------------------------------ uitgangen in de logica, het schema en HA


def test_outputs_feed_the_logic(tmp_path):
    r, hub, did, c = hub_with(tmp_path)
    quiet = r.model.add_module("and", "Niet bekeken")
    r.model.set_module(quiet, inputs={"a": f"l:{did}:on", "b": f"l:{did}:watch"})
    assert r.engine.module_values()[quiet] is False
    hub._on_state(did, {"app": "netflix"})
    assert r.engine.module_values()[quiet] is True


def test_block_on_the_schema(tmp_path):
    r, hub, did, _ = hub_with(tmp_path)
    b = next(x for x in schema.view(r.model, r.engine.snapshot())["blocks"] if x["key"] == f"l:{did}")
    assert b["kind"] == "lg" and [p["name"] for p in b["ports"]] == ["on", "show"]
    assert [(o["ref"], o["value"]) for o in b["outs"]] == [(f"l:{did}:on", True), (f"l:{did}:watch", False)]
    hub.remove(did)
    assert f"l:{did}" not in schema.existing_blocks(r.model, r.engine.snapshot())


def test_a_block_can_be_kept_in_the_schema_file(tmp_path):
    r, hub, did, _ = hub_with(tmp_path)
    r.model.set_schema({**r.model.schema, "pages": [{"id": "living", "name": "Living"}], "blocks": {f"l:{did}": {"page": "living", "x": 40, "y": 30}}})
    assert r.model.schema["blocks"][f"l:{did}"]["page"] == "living"


def test_entities_in_ha_and_no_commands(tmp_path):
    _, hub, did, _ = hub_with(tmp_path)
    hub._on_state(did, {"app": "netflix"})
    found = hub.discovery()
    assert {k.split("/")[1] for k in found} == {"binary_sensor"} and len(found) == 2
    states = hub.states()
    assert states[hub.t(did, "on")] == ("ON", True) and states[hub.t(did, "watch")] == ("ON", True)
    assert hub.filters == [] and hub.command("x", "ON") is False


# ------------------------------------------------------------ de web-API


def test_web_api_adds_places_commands_and_removes(tmp_path, woke):
    from aiohttp.test_utils import TestClient, TestServer
    from kkaddon import web
    from kkaddon.settings import Settings

    async def go():
        r = Rig(tmp_path)
        hub = LgHub(tmp_path, r.engine)
        ctx = web.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), r.engine, lg=hub)
        r.model.set_schema({**r.model.schema, "pages": [{"id": "living", "name": "Living"}]})
        c = TestClient(TestServer(web.make_app(ctx)))
        await c.start_server()
        try:
            res = await (await c.post("/api/lg", json={"name": "Woonkamer", "page": "living", "x": 40, "y": 30, "mac": MAC, "url": URL})).json()
            did = res["id"]
            assert res["key"] == f"l:{did}" and r.model.schema["blocks"][f"l:{did}"]["page"] == "living"
            hub.clients[did] = sc = StubClient()
            assert (await c.post(f"/api/lg/{did}/screen_off")).status == 200 and sc.sent == [("screen", False)]
            assert (await c.post(f"/api/lg/{did}/off")).status == 200 and sc.sent[-1] == ("off",)
            sc.connected = False
            assert (await c.post(f"/api/lg/{did}/on")).status == 200 and woke == [MAC]
            bad = await c.post(f"/api/lg/{did}/screen_on")
            assert bad.status == 400 and "verbinding" in (await bad.json())["error"]
            assert (await c.post(f"/api/lg/{did}/show")).status == 200 and did in hub.pending
            assert (await c.post("/api/lg/weg/on")).status == 400
            mid = r.model.add_module("above", "Warm", 23)
            r.model.set_module(mid, inputs={"a": None})
            assert (await c.put(f"/api/lg/{did}", json={"off_mode": "power", "inputs": {"show": f"m:{mid}"}})).status == 200
            assert hub.devices[did]["off_mode"] == "power" and hub.devices[did]["inputs"] == {"show": f"m:{mid}"}
            assert f"l:{did}" in schema.existing_blocks(r.model, r.engine.snapshot())
            assert (await c.delete(f"/api/lg/{did}")).status == 200 and did not in hub.devices
            assert (await c.delete(f"/api/lg/{did}")).status == 404
        finally:
            await c.close()
            hub.close()
    asyncio.run(go())


# ------------------------------------------------------------ van begin tot eind, tegen een nagebootste tv


async def eventually(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("duurde te lang")


def test_pairs_follows_the_tv_and_opens_the_dashboard(tmp_path):
    async def go():
        tv = await FakeTv(pairing=True).start()
        r = Rig(tmp_path)
        hub = LgHub(tmp_path, r.engine, port=tv.port, scheme="ws")
        did = hub.add({"name": "TV", "host": "127.0.0.1", "url": URL})
        await eventually(lambda: hub.status()[0]["pairing"])
        assert r.engine.ref_value(f"l:{did}:on") is None, "nog niet gekoppeld: onbekend"
        tv.accepted.set()
        await eventually(lambda: hub.is_online(did))
        assert hub.devices[did]["key"] == KEY and (tmp_path / "lgtv.json").exists()
        await eventually(lambda: hub.state(did)["app"] == "com.webos.app.home")
        assert r.engine.ref_value(f"l:{did}:on") is True and r.engine.ref_value(f"l:{did}:watch") is False
        await tv.change(app="netflix")
        await eventually(lambda: r.engine.ref_value(f"l:{did}:watch") is True)
        await tv.change(app="com.webos.app.home")
        await eventually(lambda: r.engine.ref_value(f"l:{did}:watch") is False)
        hub.show(did)
        await eventually(lambda: any(u.endswith("system.launcher/open") for u, _ in tv.requests))
        assert tv.requests[-1][1] == {"target": URL}
        await tv.stop()
        await eventually(lambda: not hub.is_online(did))
        assert r.engine.ref_value(f"l:{did}:on") is False, "de tv ging uit"
        hub.close()
    asyncio.run(go())


def test_commands_change_the_state_like_the_real_tv(tmp_path):
    async def go():
        tv = await FakeTv().start()
        r = Rig(tmp_path)
        hub = LgHub(tmp_path, r.engine, port=tv.port, scheme="ws")
        did = hub.add({"name": "TV", "host": "127.0.0.1", "url": URL})
        await eventually(lambda: hub.is_online(did) and hub.state(did)["app"] == "com.webos.app.home")
        hub.screen(did, False)
        await eventually(lambda: hub.state(did)["screen_off"])
        assert r.engine.ref_value(f"l:{did}:on") is False and r.engine.ref_value(f"l:{did}:watch") is False
        hub.power(did, True)  # alleen het scherm was uit: het scherm gaat aan
        await eventually(lambda: hub.state(did)["on"])
        hub.show(did)
        await eventually(lambda: hub.state(did)["app"] == "com.webos.app.browser")
        assert r.engine.ref_value(f"l:{did}:watch") is False, "het dashboard zelf is geen kijken"
        assert hub._hide(did) == "scherm uit"
        await eventually(lambda: hub.state(did)["screen_off"])
        await tv.change(app="netflix", power="Active")
        await eventually(lambda: r.engine.ref_value(f"l:{did}:watch") is True)
        assert "bekeken" in hub._hide(did), "er wordt gekeken: niets weggehaald"
        hub.close()
        await tv.stop()
    asyncio.run(go())
