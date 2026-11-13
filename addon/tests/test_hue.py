"""Philips Hue-lampen, -stopcontacten en -bewegingsmelders als HUISDEV-apparaat (hue.py en de Hue-kant van espdevhub.py): de resources van de bridge lezen,
apparaten herkennen, punten en waarden, koppelen met een vastgezet certificaat, een blok per apparaat, de eventstroom, bedienen, offline en terug, de entiteiten
in HA (standaard uit), opslag en het blok op het schema. Tegen een nagebootste bridge over echte HTTPS (tests/fakehue.py), gebouwd naar een echte BSB002."""
import asyncio
import json
import time

import pytest

from kkaddon import espdevhub as eh
from kkaddon import hue, schema
from kkaddon.espdevhub import EspDevHub
from kkaddon.model import ModelError
from tests.fakehue import FakeHue, house
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
    monkeypatch.setattr(eh, "HUE_FLUSH", 0.05)
    monkeypatch.setattr(eh, "HUE_GAP", 0.0)
    monkeypatch.setattr(eh, "HUE_PING", 3600)
    monkeypatch.setattr(eh, "TICK", 3600)


def did_of(fake: FakeHue, name: str, n: int = 0) -> str:
    """Het id van het blok van een apparaat: `hue_` en de eerste tien tekens van zijn UUID."""
    return eh._slug(hue.hid_of(fake._id("device", name, n)))


async def rig(tmp_path, fake=None, claim=True):
    r = Rig(tmp_path)
    hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
    fake = fake or house()
    await fake.start(tmp_path)
    fake.link = True
    bid = await hub.add_hue_bridge("127.0.0.1", fake.port)
    await eventually(lambda: hub.hue[bid]["online"])
    dids = await hub.claim_hue(bid) if claim else []
    return r, hub, fake, bid, dids


async def shutdown(hub, *fakes):
    hub.close()
    for f in fakes:
        await f.stop()
    await asyncio.sleep(0.05)


def status(hub, did):
    return next(x for x in hub.status() if x["id"] == did)


def pts(hub, did):
    return {p["id"]: p for p in hub.devices[did]["points"]}


# ------------------------------------------------------------ de resources lezen


def test_a_device_is_a_lamp_a_plug_or_a_sensor_by_its_services():
    f = house()
    store = hue.index(list(f.res.values()))
    kinds = {d["name"]: d["kind"] for d in hue.devices(store)}
    assert kinds == {"Keuken": "lamp", "Gang": "lamp", "Oprit": "lamp", "Stopcontact tuin": "plug", "Melder voordeur": "sensor"}
    assert len(hue.devices(store)) == 6, "twee lampen met dezelfde naam zijn twee apparaten"
    assert hue.skipped(store) == ["Dimmer hal"], "een knop bieden we niet aan, en dat staat erbij: de bridge zelf telt niet"
    plug = next(d for d in hue.devices(store) if d["kind"] == "plug")
    assert store[plug["rid"]]["metadata"]["archetype"] == "classic_bulb", "het archetype kiest de gebruiker, dus het zegt niets over een stopcontact: het ontbreken van dimming wel"


def test_the_points_follow_the_services():
    f = house()
    store = hue.index(list(f.res.values()))
    by = lambda name: {p["id"]: p for p in hue.describe(store[f.dev[name]], store)["points"]}  # noqa: E731
    lamp, plug, sensor = by("Keuken"), by("Stopcontact tuin"), by("Melder voordeur")
    assert list(lamp) == ["on", "bri", "reach"] and list(plug) == ["on", "reach"] and list(sensor) == ["motion", "temp", "lux", "battery", "reach"]
    assert (lamp["on"]["writable"], lamp["bri"]["writable"], lamp["bri"]["min"], lamp["bri"]["max"], lamp["bri"]["unit"]) == (True, True, 1, 100, "%")
    assert (sensor["motion"]["writable"], sensor["temp"]["cls"], sensor["temp"]["unit"], sensor["lux"]["cls"], sensor["battery"]["cls"]) == (False, "temperature", "°C", "illuminance", "battery")
    assert all(p["diag"] for p in (lamp["reach"], plug["reach"], sensor["reach"])) and not any(p["danger"] for p in (*lamp.values(), *sensor.values()))
    d = hue.describe(store[f.dev["Keuken"]], store)
    assert d["type"] == "Hue-lamp" and d["id"] == hue.hid_of(f.dev["Keuken"]) and d["fw"] == "1.122.2" and d["actions"] == []
    # een melder zonder temperatuurdienst krijgt geen temperatuurpunt
    s2 = dict(store)
    sens = dict(store[f.dev["Melder voordeur"]])
    sens["services"] = [x for x in sens["services"] if x["rtype"] != "temperature"]
    assert [p["id"] for p in hue.describe(sens, s2)["points"]] == ["motion", "lux", "battery", "reach"]
    assert hue.describe(store[f.dev["Dimmer hal"]], store) is None and hue.describe(store[f.dev["Hue Bridge"]], store) is None


def test_every_point_is_a_valid_huisdev_point():
    f = house()
    store = hue.index(list(f.res.values()))
    for name in ("Keuken", "Stopcontact tuin", "Melder voordeur"):
        d = hue.describe(store[f.dev[name]], store)
        c = eh.clean_descriptor({"proto": "1.0", "rev": d["rev"], "device": {"id": d["id"]}, "points": [{**p, "class": p["cls"]} for p in d["points"]], "actions": []})
        assert len(c["points"]) == len(d["points"]) and c["points"][0]["id"] == d["points"][0]["id"], name


def test_the_id_comes_from_the_uuid_never_from_the_name():
    f = house()
    a, b = f._id("device", "Oprit", 1), f._id("device", "Oprit", 2)
    assert hue.hid_of(a) != hue.hid_of(b) and eh.ID_RE.match(hue.hid_of(a)) and eh.REF_RE.match(f"e:{eh._slug(hue.hid_of(a))}:on")
    assert hue.hid_of(a) == hue.hid_of(a.upper().lower()), "stabiel"


def test_values_come_from_the_services_and_a_lamp_that_is_out_of_reach_is_unknown():
    f = house()
    store = hue.index(list(f.res.values()))
    v = lambda name: hue.values(store[f.dev[name]], store)  # noqa: E731
    assert v("Keuken") == {"reach": True, "on": False, "bri": 40}
    assert v("Gang") == {"reach": False}, "de bridge onthoudt de stand van een lamp die uit staat, maar dat is een gok"
    assert v("Stopcontact tuin") == {"reach": True, "on": True}, "een stopcontact dimt niet: geen helderheid"
    assert v("Melder voordeur") == {"reach": True, "motion": False, "temp": 9.27, "lux": 0.0, "battery": 100}
    # een melder die de bridge niet meer hoort: alleen `reach`
    f.res[f.svc(f.dev["Melder voordeur"], "zigbee_connectivity")["id"]]["status"] = "connectivity_issue"
    assert hue.values(store[f.dev["Melder voordeur"]], store) == {"reach": False}
    # geldigheid: `motion_valid: false` is onbekend, nooit "geen beweging"
    f2 = house()
    st2 = hue.index(list(f2.res.values()))
    f2.svc(f2.dev["Melder voordeur"], "motion")["motion"]["motion_valid"] = False
    assert hue.values(st2[f2.dev["Melder voordeur"]], st2)["motion"] is None


def test_light_level_is_lux():
    assert hue.lux(0) == 0.0 and hue.lux(-5) == 0.0
    assert hue.lux(10001) == 10.0 and hue.lux(30001) == 1000.0
    assert hue.lux(None) is None and hue.lux(True) is None and hue.lux(float("nan")) is None


def test_events_are_merged_into_the_resources_and_noise_is_ignored():
    f = house()
    store = hue.index(list(f.res.values()))
    light = f.svc(f.dev["Keuken"], "light")["id"]
    raw = json.dumps([{"creationtime": "x", "id": "e1", "type": "update", "data": [{"id": light, "type": "light", "on": {"on": True}}]},
                      {"creationtime": "x", "id": "e2", "type": "update", "data": [{"id": light, "type": "light", "dimming": {"brightness": 75.0}}]}])
    for kind, res in hue.events(raw):
        hue.apply(store, kind, res)
    assert store[light]["on"] == {"on": True} and store[light]["dimming"] == {"brightness": 75.0, "min_dim_level": 5.0}, "een update bevat maar wat veranderde"
    hue.apply(store, "update", {"id": "bestaat-niet", "type": "light", "on": {"on": True}})
    assert "bestaat-niet" not in store, "een halve resource is erger dan geen"
    hue.apply(store, "update", {"id": light, "type": "motion", "on": {"on": False}})
    assert store[light]["on"] == {"on": True}, "een ander type onder hetzelfde id wordt niet samengevoegd"
    hue.apply(store, "delete", {"id": light, "type": "light"})
    assert light not in store
    hue.apply(store, "add", {"id": "nieuw", "type": "light", "on": {"on": False}})
    assert store["nieuw"]["on"] == {"on": False}
    for bad in ("", "niet json", "{}", "[1, 2]", json.dumps([{"type": "error", "data": []}]), json.dumps([{"type": "update", "data": [{"id": 5}]}]), json.dumps([{"type": "update", "data": "x"}])):
        assert hue.events(bad) == [], bad


def test_commands_are_absolute_and_go_to_the_light_service():
    f = house()
    store = hue.index(list(f.res.values()))
    light = f.svc(f.dev["Keuken"], "light")["id"]
    assert hue.point_request(store[f.dev["Keuken"]], store, "on", True) == (f"/clip/v2/resource/light/{light}", {"on": {"on": True}})
    assert hue.point_request(store[f.dev["Keuken"]], store, "on", False)[1] == {"on": {"on": False}}
    assert hue.point_request(store[f.dev["Keuken"]], store, "bri", 30) == (f"/clip/v2/resource/light/{light}", {"dimming": {"brightness": 30.0}})
    for dev, pid in (("Keuken", "reach"), ("Melder voordeur", "on"), ("Stopcontact tuin", "bri")):
        with pytest.raises(ValueError):
            hue.point_request(store[f.dev[dev]], store, pid, 1)


# ------------------------------------------------------------ koppelen


def test_pairing_needs_the_button_and_pins_the_certificate(tmp_path):
    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = await house().start(tmp_path)
        with pytest.raises(ModelError, match="ronde knop"):
            await hub.add_hue_bridge("127.0.0.1", fake.port)
        assert hub.bridges == {} and not (tmp_path / "espdev.json").exists(), "zonder knop wordt er niets bewaard"
        fake.link = True
        bid = await hub.add_hue_bridge("127.0.0.1", fake.port)
        assert bid == "b_123456"
        br = hub.bridges[bid]
        assert (br["key"], br["fp"], br["bridge_id"], br["name"], br["own"]) == (fake.key, fake.fp, "001788FFFE123456", "Hue Bridge", True)
        assert json.loads((tmp_path / "espdev.json").read_text(encoding="utf-8"))["bridges"][bid]["fp"] == fake.fp
        await eventually(lambda: hub.hue[bid]["online"])
        # nooit de sleutel of de vingerafdruk naar de pagina
        shown = json.dumps([hub.hue_view(), hub.status(), r.engine.snapshot()["hue"]])
        assert fake.key not in shown and fake.fp not in shown and "0D4850" not in shown
        with pytest.raises(ModelError, match="staat er al"):
            await hub.add_hue_bridge("127.0.0.1", fake.port)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_an_existing_key_is_checked_and_a_wrong_one_is_refused(tmp_path):
    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = await house().start(tmp_path)
        with pytest.raises(ModelError, match="sleutel"):
            await hub.add_hue_bridge("127.0.0.1", fake.port, key="x" * 40)
        assert hub.bridges == {} and not any(c[1] == "/api" for c in fake.calls), "met een sleutel wordt er niet gekoppeld, en een foute wordt niet bewaard"
        bid = await hub.add_hue_bridge("127.0.0.1", fake.port, key=fake.key, name="Tuin")
        assert hub.bridges[bid]["name"] == "Tuin" and hub.bridges[bid]["key"] == fake.key and hub.bridges[bid]["own"] is False
        await eventually(lambda: hub.hue[bid]["online"])
        assert await hub.remove_hue_bridge(bid)
        assert not any(c[0] == "DELETE" for c in fake.calls), "een sleutel die je zelf meegaf is van iemand anders (een ander programma): die trekken we niet in"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_add_errors_say_what_is_wrong(tmp_path):
    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        for bad in ("", "https://x", "a b", "x/y"):
            with pytest.raises(ModelError, match="ongeldig adres"):
                await hub.add_hue_bridge(bad)
        with pytest.raises(ModelError, match="poort"):
            await hub.add_hue_bridge("127.0.0.1", 0)
        with pytest.raises(ModelError, match="geen verbinding"):
            await hub.add_hue_bridge("127.0.0.1", 9)  # er luistert niets
        hub.close()
    asyncio.run(go())


def test_something_that_is_not_a_hue_bridge_is_refused(tmp_path):
    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = house()
        fake.bridge_id = "geen id"  # antwoordt wel op /api/config, maar niet als een bridge
        await fake.start(tmp_path)
        fake.link = True
        with pytest.raises(ModelError, match="geen Hue-bridge"):
            await hub.add_hue_bridge("127.0.0.1", fake.port)
        assert hub.bridges == {} and fake.calls == [], "aan iets wat zich niet als bridge meldt, vragen we geen sleutel"
        await shutdown(hub, fake)
    asyncio.run(go())


# ------------------------------------------------------------ volgen


def test_claiming_makes_a_block_per_device_and_lists_what_is_left(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path, claim=False)
        view = hub.hue_view()[0]
        assert [d["name"] for d in view["devices"]] == ["Gang", "Keuken", "Melder voordeur", "Oprit", "Oprit", "Stopcontact tuin"] and view["skipped"] == ["Dimmer hal"]
        assert not any(d["claimed"] for d in view["devices"]) and view["online"] is True
        keuken = fake.dev["Keuken"]
        new = await hub.claim_hue(bid, [keuken])
        assert new == [did_of(fake, "Keuken")] and hub.devices[new[0]]["name"] == "Keuken" and hub.devices[new[0]]["driver"] == "hue" and hub.devices[new[0]]["ha"] is False
        with pytest.raises(ModelError, match="staat er al"):
            await hub.claim_hue(bid, [keuken])
        rest = await hub.claim_hue(bid)  # leeg: alles wat er nog niet staat
        assert len(rest) == 5 and len(hub.devices) == 6 and len({*hub.devices}) == 6, "twee lampen met dezelfde naam zijn twee blokken"
        assert all(d["claimed"] for d in hub.hue_view()[0]["devices"])
        with pytest.raises(ModelError, match="kent dit apparaat niet"):
            await hub.claim_hue(bid, ["00000000-0000-4000-8000-000000000000"])
        with pytest.raises(ModelError, match="niet bereikbaar"):
            await hub.claim_hue("b_onbekend")
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_blocks_show_values_and_unreachable_lamps_are_unknown(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path)
        k, g, s, m = (did_of(fake, n) for n in ("Keuken", "Gang", "Stopcontact tuin", "Melder voordeur"))
        await eventually(lambda: all(d in hub.online for d in dids))
        assert r.engine.ref_value(f"e:{k}:on") is False and r.engine.ref_value(f"e:{k}:bri") == 40 and r.engine.ref_value(f"e:{k}:reach") is True
        assert r.engine.ref_value(f"e:{g}:on") is None and r.engine.ref_value(f"e:{g}:bri") is None and r.engine.ref_value(f"e:{g}:reach") is False
        assert r.engine.ref_value(f"e:{s}:on") is True
        assert (r.engine.ref_value(f"e:{m}:motion"), r.engine.ref_value(f"e:{m}:temp"), r.engine.ref_value(f"e:{m}:lux"), r.engine.ref_value(f"e:{m}:battery")) == (False, 9.27, 0.0, 100)
        st = status(hub, k)
        assert (st["driver"], st["online"], st["type"], st["host"], st["fw"], st["ha"], st["bridge"]) == ("hue", True, "Hue-lamp", "127.0.0.1", "1.122.2", False, bid)
        assert {p["id"]: p["value"] for p in st["points"]} == {"on": False, "bri": 40, "reach": True}
        assert fake.streams == 1 and fake.lists == 1, "een eventstroom en een lijst, niet per apparaat"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_changes_arrive_over_the_event_stream(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path)
        k, m, g = did_of(fake, "Keuken"), did_of(fake, "Melder voordeur"), did_of(fake, "Gang")
        rev = hub.devices[k]["rev"]
        fake.update(fake.dev["Keuken"], "light", on={"on": True})
        await eventually(lambda: r.engine.ref_value(f"e:{k}:on") is True)
        fake.update(fake.dev["Keuken"], "light", dimming={"brightness": 72.4})
        await eventually(lambda: r.engine.ref_value(f"e:{k}:bri") == 72)
        fake.update(fake.dev["Melder voordeur"], "motion", motion={"motion": True, "motion_valid": True})
        fake.update(fake.dev["Melder voordeur"], "temperature", temperature={"temperature": 21.5})
        fake.update(fake.dev["Melder voordeur"], "light_level", light={"light_level": 30001})
        fake.update(fake.dev["Melder voordeur"], "device_power", power_state={"battery_level": 54})
        await eventually(lambda: r.engine.ref_value(f"e:{m}:motion") is True and r.engine.ref_value(f"e:{m}:temp") == 21.5 and r.engine.ref_value(f"e:{m}:lux") == 1000.0)
        assert r.engine.ref_value(f"e:{m}:battery") == 54 and hub.devices[k]["rev"] == rev, "een nieuwe waarde is geen nieuwe beschrijving"
        fake.connected(fake.dev["Gang"], True)  # de lamp gaat aan de netspanning: zijn stand is weer bekend
        await eventually(lambda: r.engine.ref_value(f"e:{g}:on") is True and r.engine.ref_value(f"e:{g}:bri") == 100)
        fake.connected(fake.dev["Keuken"], False)
        await eventually(lambda: r.engine.ref_value(f"e:{k}:on") is None and r.engine.ref_value(f"e:{k}:reach") is False)
        assert k in hub.online, "de lamp is weg, de bridge niet: het blok blijft verbonden"
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_device_that_disappears_from_the_bridge_goes_offline_with_a_reason(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path)
        k, s = did_of(fake, "Keuken"), did_of(fake, "Stopcontact tuin")
        fake.remove(fake.dev["Keuken"])
        await eventually(lambda: k not in hub.online)
        assert "bestaat niet meer" in status(hub, k)["error"] and s in hub.online and r.engine.ref_value(f"e:{k}:on") is None
        with pytest.raises(ModelError, match="geen verbinding"):
            await hub.set(k, "on", True)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_long_list_is_read_completely(tmp_path):
    """De regressie van 0.30.1 (`content.read(n)` geeft alleen wat er al binnen is): een lijst van een echte bridge is 100 KB en komt in stukken."""
    async def go():
        fake = house()
        for i in range(300):
            fake.add_lamp(f"Lamp {i}", n=i + 10)
        r, hub, fake, bid, _ = await rig(tmp_path, fake=fake, claim=False)
        assert len(hub.hue_view()[0]["devices"]) == 306, "alles gelezen: 300 lampen en wat er al was"
        assert len(json.dumps(list(fake.res.values()))) > 150_000
        await shutdown(hub, fake)
    asyncio.run(go())


def test_offline_when_the_bridge_goes_away_and_back_when_it_returns(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path)
        k = did_of(fake, "Keuken")
        port = fake.port
        await fake.stop()
        await eventually(lambda: k not in hub.online and not hub.hue[bid]["online"])
        assert hub.output(f"e:{k}:bri") is None, "offline: elke waarde is onbekend, nooit de laatste gok"
        assert status(hub, k)["error"] and hub.hue_view()[0]["error"] and hub.hue_view()[0]["cert"] is False
        fake.update(fake.dev["Keuken"], "light", on={"on": True})  # gebeurt terwijl wij er niet zijn: de lijst bij terugkeer moet het goed hebben
        await fake.start(port=port)
        await eventually(lambda: k in hub.online, 8)
        assert hub.output(f"e:{k}:on") is True and status(hub, k)["error"] == ""
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_dropped_stream_reconnects_and_resyncs(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path)
        k = did_of(fake, "Keuken")
        fake.drop()
        await eventually(lambda: fake.streams == 2 and k in hub.online, 8)
        assert fake.lists == 2, "na een gesloten stroom is een gemiste gebeurtenis mogelijk: de hele lijst opnieuw"
        fake.update(fake.dev["Keuken"], "light", on={"on": True})
        await eventually(lambda: hub.output(f"e:{k}:on") is True)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_bridge_is_asked_if_it_is_still_there_because_the_stream_stays_silent(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(eh, "HUE_PING", 0.2)
        r, hub, fake, bid, dids = await rig(tmp_path)
        k = did_of(fake, "Keuken")
        pings = lambda: sum(1 for c in fake.calls if c[1] == "/clip/v2/resource/bridge")  # noqa: E731
        await eventually(lambda: pings() >= 2, 5)
        assert fake.streams == 1 and fake.lists == 1 and k in hub.online, "een vraag is geen nieuwe lijst en geen nieuwe stroom"
        # de bridge hangt: de eventstroom blijft open maar hij antwoordt niet meer. Zonder de vraag bleef het blok eindeloos "verbonden"
        fake.ping_status = 503
        await eventually(lambda: k not in hub.online, 5)
        assert "503" in status(hub, k)["error"]
        fake.ping_status = 0
        await eventually(lambda: k in hub.online, 8)
        await shutdown(hub, fake)
    asyncio.run(go())


# ------------------------------------------------------------ certificaat


def test_a_changed_certificate_is_refused_until_the_user_trusts_it_again(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path)
        k = did_of(fake, "Keuken")
        port = fake.port
        await fake.stop()
        fake.new_cert()  # een bridge die is teruggezet, of een andere op hetzelfde adres
        await fake.start(port=port)
        await eventually(lambda: hub.hue[bid]["cert"] and k not in hub.online, 8)
        v = hub.hue_view()[0]
        assert "certificaat" in v["error"] and v["cert"] is True and "certificaat" in status(hub, k)["error"]
        with pytest.raises(ModelError, match="geen verbinding"):
            await hub.set(k, "on", True)
        assert not any(c[0] == "PUT" for c in fake.calls), "de sleutel gaat niet naar een certificaat dat we niet vertrouwen"
        await hub.verify_hue_bridge(bid)
        assert hub.bridges[bid]["fp"] == fake.fp
        await eventually(lambda: k in hub.online and not hub.hue_view()[0]["cert"], 8)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_trusting_again_refuses_another_bridge_and_follows_a_new_address(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path)
        old_fp = hub.bridges[bid]["fp"]
        other = FakeHue(bridge_id="001788FFFE999999")
        await other.start(tmp_path)
        hub.bridges[bid]["port"] = other.port  # het adres wijst naar een andere bridge
        with pytest.raises(ModelError, match="andere bridge"):
            await hub.verify_hue_bridge(bid)
        assert hub.bridges[bid]["fp"] == old_fp, "een andere bridge wordt nooit vertrouwd"
        hub.bridges[bid]["port"] = fake.port
        await hub.verify_hue_bridge(bid, host="127.0.0.1", name="Tuin")
        assert hub.bridges[bid]["name"] == "Tuin"
        with pytest.raises(ModelError, match="ongeldig adres"):
            await hub.verify_hue_bridge(bid, host="http://x")
        with pytest.raises(ModelError, match="onbekende bridge"):
            await hub.verify_hue_bridge("b_nergens")
        await shutdown(hub, fake, other)
    asyncio.run(go())


# ------------------------------------------------------------ bedienen


def test_set_reaches_the_bridge_and_the_state_comes_back_over_the_stream(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path)
        k, s = did_of(fake, "Keuken"), did_of(fake, "Stopcontact tuin")
        light, plug = fake.svc(fake.dev["Keuken"], "light")["id"], fake.svc(fake.dev["Stopcontact tuin"], "light")["id"]
        assert (await hub.set(k, "on", True))["result"] == "applied"
        await eventually(lambda: hub.output(f"e:{k}:on") is True)
        await hub.set(k, "bri", 25)
        await eventually(lambda: hub.output(f"e:{k}:bri") == 25)
        await hub.set(s, "on", False)
        await eventually(lambda: hub.output(f"e:{s}:on") is False)
        assert [(c[0], c[1], c[2]) for c in fake.calls if c[0] == "PUT"] == [("PUT", f"/clip/v2/resource/light/{light}", {"on": {"on": True}}),
                                                                           ("PUT", f"/clip/v2/resource/light/{light}", {"dimming": {"brightness": 25.0}}),
                                                                           ("PUT", f"/clip/v2/resource/light/{plug}", {"on": {"on": False}})]
        await shutdown(hub, fake)
    asyncio.run(go())


def test_what_must_not_be_sent_is_refused_before_it_leaves(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path)
        k, s, m = did_of(fake, "Keuken"), did_of(fake, "Stopcontact tuin"), did_of(fake, "Melder voordeur")
        for did, pid, v, word in [(k, "reach", True, "niet instelbaar"), (m, "motion", True, "niet instelbaar"), (m, "temp", 5, "niet instelbaar"), (k, "nergens", 1, "onbekend"),
                                  (k, "on", 5, "aan of uit"), (k, "bri", 0, "tussen"), (k, "bri", 101, "tussen"), (k, "bri", "veel", "getal"), (s, "bri", 5, "onbekend")]:
            with pytest.raises(ModelError, match=word):
                await hub.set(did, pid, v)
        assert not any(c[0] == "PUT" for c in fake.calls)
        await shutdown(hub, fake)
    asyncio.run(go())


def test_a_command_the_bridge_refuses_is_reported_with_its_reason(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path)
        k, g = did_of(fake, "Keuken"), did_of(fake, "Gang")
        fake.reject = 1
        with pytest.raises(ModelError, match="unreachable"):
            await hub.set(k, "on", True)
        assert hub.output(f"e:{k}:on") is False, "de stand blijft wat de bridge meldt"
        fake.status_next, fake.status_code = 1, 429
        with pytest.raises(ModelError, match="te veel opdrachten"):
            await hub.set(k, "on", True)
        fake.status_next, fake.status_code = 1, 500
        with pytest.raises(ModelError, match="HTTP 500"):
            await hub.set(k, "on", True)
        fake.key, fake.status_next = "ander", 0  # de sleutel is op de bridge ingetrokken
        with pytest.raises(ModelError, match="sleutel"):
            await hub.set(k, "on", True)
        await eventually(lambda: k not in hub.online and g not in hub.online, 8)
        with pytest.raises(ModelError, match="geen verbinding"):
            await hub.set(k, "on", True)
        assert "sleutel" in hub.hue_view()[0]["error"]
        await shutdown(hub, fake)
    asyncio.run(go())


def test_commands_to_one_bridge_are_spaced_out(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(eh, "HUE_GAP", 0.08)
        r, hub, fake, bid, dids = await rig(tmp_path)
        k, s = did_of(fake, "Keuken"), did_of(fake, "Stopcontact tuin")
        await asyncio.gather(hub.set(k, "on", True), hub.set(s, "on", False), hub.set(k, "bri", 10), hub.set(k, "bri", 20))
        gaps = [b - a for a, b in zip(fake.times, fake.times[1:])]
        assert len(fake.times) == 4 and min(gaps) > 0.05, gaps
        await shutdown(hub, fake)
    asyncio.run(go())


def test_an_input_drives_a_lamp_only_on_a_change(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path)
        k, m = did_of(fake, "Keuken"), did_of(fake, "Melder voordeur")
        warm = r.model.add_module("above", "Warm", 20)
        r.model.set_module(warm, inputs={"a": f"e:{m}:temp"})
        hub.update(k, {"inputs": {"on": f"m:{warm}", "bri": f"e:{m}:battery"}})
        await eventually(lambda: r.engine.ref_value(f"m:{warm}") is False)  # 9,27 °C is niet warmer dan 20
        hub.tick()
        await asyncio.sleep(0.1)
        assert [c for c in fake.calls if c[0] == "PUT"] == [], "de eerste stand wordt alleen onthouden: een herstart schakelt niets"
        fake.update(fake.dev["Melder voordeur"], "temperature", temperature={"temperature": 25.0})
        fake.update(fake.dev["Melder voordeur"], "device_power", power_state={"battery_level": 55})
        await eventually(lambda: r.engine.ref_value(f"m:{warm}") is True and r.engine.ref_value(f"e:{m}:battery") == 55)
        hub.tick()
        await eventually(lambda: hub.output(f"e:{k}:on") is True and hub.output(f"e:{k}:bri") == 55)
        puts = [c[2] for c in fake.calls if c[0] == "PUT"]
        assert {"on": {"on": True}} in puts and {"dimming": {"brightness": 55.0}} in puts, "absoluut: aan en een helderheid, geen toggle"
        for _ in range(3):
            hub.tick()
        await asyncio.sleep(0.1)
        assert len([c for c in fake.calls if c[0] == "PUT"]) == 2, "een ongewijzigde ingang stuurt niets opnieuw"
        with pytest.raises(ModelError, match="niet te koppelen"):
            hub.update(k, {"inputs": {"reach": f"m:{warm}"}})  # alleen-lezen
        await shutdown(hub, fake)
    asyncio.run(go())


# ------------------------------------------------------------ Home Assistant


def test_ha_entities_are_off_by_default_and_exact_when_wanted(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path)
        k, m = did_of(fake, "Keuken"), did_of(fake, "Melder voordeur")
        assert hub.discovery() == {} and hub.states() == {}, "de lampen staan al in HA via de Hue-integratie: geen dubbele entiteiten tenzij je dat wilt"
        hub.update(k, {"ha": True})
        hub.update(m, {"ha": True})
        disc = hub.discovery()
        assert any(t.endswith(f"/switch/kk_esp_{k}_on/config") for t in disc) and any(t.endswith(f"/number/kk_esp_{k}_bri/config") for t in disc)
        temp = next(v for t, v in disc.items() if t.endswith(f"/sensor/kk_esp_{m}_temp/config"))
        assert (temp["unit_of_measurement"], temp["device_class"], temp["state_class"]) == ("°C", "temperature", "measurement")
        reach = next(v for t, v in disc.items() if t.endswith(f"/binary_sensor/kk_esp_{k}_reach/config"))
        assert reach["entity_category"] == "diagnostic"
        dev = next(iter(disc.values()))["device"]
        assert dev["manufacturer"] == "Signify" and dev["model"] in ("Hue-lamp", "Hue-bewegingsmelder")
        assert hub.states()[hub.t(m, "temp")][0] == "9.3" and hub.states()[hub.t(k, "on")][0] == "OFF"
        hub.update(k, {"ha": False})
        assert not any(f"kk_esp_{k}_" in t for t in hub.discovery()) and any(f"/switch/kk_esp_{k}_on/config" in t for t in hub.retire()), "wat al in HA stond, wordt gewist"
        await shutdown(hub, fake)
    asyncio.run(go())


# ------------------------------------------------------------ opslag, schema en opruimen


def test_it_survives_a_restart(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path)
        k = did_of(fake, "Keuken")
        hub.update(k, {"name": "Keuken plafond", "shown": ["bri"]})
        rev, store_len = hub.devices[k]["rev"], len(hub.hue[bid]["store"])
        hub.close()
        hub2 = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        assert hub2.bridges[bid]["fp"] == fake.fp and hub2.bridges[bid]["key"] == fake.key and hub2.devices[k]["driver"] == "hue"
        d2 = hub2.devices[k]
        assert (d2["name"], d2["rev"], d2["bridge"], d2["rid"], d2["shown"], d2["ha"]) == ("Keuken plafond", rev, bid, fake.svc(fake.dev["Keuken"], "light")["owner"]["rid"], ["bri"], False)
        hub2.start()
        await eventually(lambda: k in hub2.online)
        assert hub2.output(f"e:{k}:bri") == 40 and hub2.devices[k]["rev"] == rev and len(hub2.hue[bid]["store"]) == store_len
        await shutdown(hub2, fake)
    asyncio.run(go())


def test_a_damaged_file_does_not_stop_the_hub(tmp_path):
    (tmp_path / "espdev.json").write_text(json.dumps({"bridges": {"b_ok": {"host": "10.0.0.2", "key": "k", "fp": "00" * 32, "bridge_id": "001788FFFE123456"},
                                                       "b_kapot": {"host": "", "key": "", "fp": "x"}, "Fout Id": {}, "b_nul": 5},
                                                 "devices": {"hue_x": {"hid": "hue-x", "driver": "hue", "bridge": "b_ok", "rid": "geen uuid"}}}), encoding="utf-8")
    r = Rig(tmp_path)
    hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
    assert list(hub.bridges) == ["b_ok"] and hub.devices["hue_x"]["rid"] == "" and hub.devices["hue_x"]["driver"] == "hue"


def test_the_block_on_the_schema_and_the_grid(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path)
        k, s, m = did_of(fake, "Keuken"), did_of(fake, "Stopcontact tuin"), did_of(fake, "Melder voordeur")
        blocks = {b["key"]: b for b in schema.view(r.model, r.engine.snapshot())["blocks"]}
        b = blocks[f"e:{k}"]
        assert (b["kind"], b["tech"], b["driver"], b["online"], b["bridge"]) == ("espdev", "Hue-lamp · Philips Hue", "hue", True, bid)
        assert [o["name"] for o in b["outs"]] == ["on", "bri"] and [p["name"] for p in b["ports"]] == ["on", "bri"], "aan en helderheid zijn ook ingangen; bereikbaar is diagnose en staat niet op het blok"
        assert [o["name"] for o in blocks[f"e:{s}"]["outs"]] == ["on"] and [o["name"] for o in blocks[f"e:{m}"]["outs"]] == ["motion", "temp", "lux", "battery"]
        assert blocks[f"e:{m}"]["ports"] == [], "een melder heeft geen ingangen"
        assert "reach" not in {o["name"] for o in b["all"]}
        # een raster: vier blokken per rij, de volgende rij onder het hoogste blok van de vorige
        r.model.set_schema({**r.model.schema, "pages": [{"id": "tuin", "name": "Tuin"}]})
        keys = [f"e:{d}" for d in dids]
        schema.place_grid(r.model, r.engine.snapshot(), keys, "tuin", 40, 30)
        placed = [r.model.schema["blocks"][x] for x in keys]
        assert len(placed) == 6 and all(p["page"] == "tuin" for p in placed)
        assert [p["x"] for p in placed[:4]] == [40, 420, 800, 1180], "een apparaatblok is 340 breed (SC_AC_W): geen overlap" and placed[4]["x"] == 40 and {p["y"] for p in placed[:4]} == {30}
        tallest = max(schema.HEAD + blocks[x]["rows"] * schema.ROW + schema.FOOT for x in keys[:4])
        assert placed[4]["y"] == 30 + tallest + schema.GAP
        with pytest.raises(ModelError, match="onbekend blok"):
            schema.place_grid(r.model, r.engine.snapshot(), ["e:nergens"], "tuin")
        await shutdown(hub, fake)
    asyncio.run(go())


def test_remove_a_device_leaves_the_bridge_and_remove_the_bridge_takes_everything(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path)
        k, s = did_of(fake, "Keuken"), did_of(fake, "Stopcontact tuin")
        hub.update(k, {"ha": True})
        n = len(fake.calls)
        assert await hub.remove(k) and k not in hub.devices and k not in hub.hue_desc and fake.calls[n:] == [], "er is niets in te trekken bij de bridge"
        assert hub.bridges and hub.retire(), "zijn entiteiten in HA moeten weg"
        assert [d["claimed"] for d in hub.hue_view()[0]["devices"] if d["name"] == "Keuken"] == [False], "hij staat weer in de lijst"
        assert await hub.remove_hue_bridge(bid)
        assert hub.bridges == {} and hub.devices == {} and hub.hue_view() == [] and f"hue:{bid}" not in hub.tasks
        assert any(c[0] == "DELETE" and c[1] == f"/api/{fake.key}/config/whitelist/{fake.key}" for c in fake.calls), "de sleutel wordt ook op de bridge ingetrokken"
        assert json.loads((tmp_path / "espdev.json").read_text(encoding="utf-8"))["bridges"] == {}
        assert not await hub.remove_hue_bridge(bid)
        assert s not in hub.devices
        await shutdown(hub, fake)
    asyncio.run(go())


def test_the_inventory_lists_it(tmp_path):
    async def go():
        from kkaddon import inventory
        r, hub, fake, bid, dids = await rig(tmp_path)
        k = did_of(fake, "Keuken")
        row = next(x for x in inventory.build(r.engine, None) if x["key"] == f"e{k}")
        assert (row["name"], row["kind"], row["protocol"], row["ip"], row["online"], row["note"]) == ("Keuken", "Hue-lamp", "Hue (CLIP v2)", "127.0.0.1", True, "")
        await shutdown(hub, fake)
    asyncio.run(go())


# ------------------------------------------------------------ web


def test_web_api_pairs_claims_places_sets_and_removes(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from kkaddon import web
    from kkaddon.settings import Settings

    async def go():
        r = Rig(tmp_path)
        hub = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        fake = await house().start(tmp_path)
        ctx = web.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), r.engine, espdev=hub)
        r.model.set_schema({**r.model.schema, "pages": [{"id": "tuin", "name": "Tuin"}]})
        c = TestClient(TestServer(web.make_app(ctx)))
        await c.start_server()
        try:
            bad = await c.post("/api/espdev/hue/bridge", json={"host": "127.0.0.1", "port": fake.port})
            assert bad.status == 400 and "ronde knop" in (await bad.json())["error"]
            fake.link = True
            res = await (await c.post("/api/espdev/hue/bridge", json={"host": "127.0.0.1", "port": fake.port})).json()
            bid = res["id"]
            await eventually(lambda: hub.hue[bid]["online"])
            st = await (await c.get("/api/state")).json()
            assert [b["id"] for b in st["hue"]] == [bid] and len(st["hue"][0]["devices"]) == 6 and fake.key not in json.dumps(st) and fake.fp not in json.dumps(st)
            bad = await c.post("/api/espdev/hue/claim", json={"bridge": bid, "rids": "ja"})
            assert bad.status == 400
            res = await (await c.post("/api/espdev/hue/claim", json={"bridge": bid, "page": "tuin", "x": 40, "y": 30})).json()
            assert len(res["ids"]) == 6 and res["keys"] == [f"e:{d}" for d in res["ids"]]
            assert all(r.model.schema["blocks"][k]["page"] == "tuin" for k in res["keys"])
            bad = await c.post("/api/espdev/hue/claim", json={"bridge": bid})
            assert bad.status == 400 and "staat er al" in (await bad.json())["error"]
            k = did_of(fake, "Keuken")
            await eventually(lambda: k in hub.online)
            assert (await c.post(f"/api/espdev/{k}/set", json={"point": "on", "value": True})).status == 200
            await eventually(lambda: hub.output(f"e:{k}:on") is True)
            assert (await c.put(f"/api/espdev/{k}", json={"host": "1.2.3.4"})).status == 400
            assert (await c.put(f"/api/espdev/hue/bridge/{bid}", json={"name": "Tuin"})).status == 200 and hub.bridges[bid]["name"] == "Tuin"
            assert (await c.put(f"/api/espdev/hue/bridge/{bid}", json={"trust": True})).status == 200
            assert (await c.put("/api/espdev/hue/bridge/b_nergens", json={"trust": True})).status == 400
            assert (await c.delete(f"/api/espdev/hue/bridge/{bid}")).status == 200 and hub.bridges == {} and hub.devices == {}
            assert all(k not in r.model.schema["blocks"] for k in res["keys"])
            assert (await c.delete(f"/api/espdev/hue/bridge/{bid}")).status == 404
        finally:
            await c.close()
            await shutdown(hub, fake)
    asyncio.run(go())


# ------------------------------------------------------------ kamers en scènes


def room_house() -> FakeHue:
    h = house()
    h.add_room("Woonkamer", ["Keuken", "Gang"], on=False, bri=60.0)  # Gang is onbereikbaar, Keuken niet
    for n in ("Relax", "Helder", "Avond"):
        h.add_scene("Woonkamer", n)
    return h


def room_id(fake: FakeHue, name: str) -> str:
    return eh._slug(hue.hid_of(fake.dev[name]))


def test_a_room_has_on_brightness_and_a_scene_that_is_a_number():
    h = room_house()
    room = h.res[h.dev["Woonkamer"]]
    assert hue.kind_of(room, h.res) == "room" and any(o["kind"] == "room" for o in hue.devices(h.res))
    nums: dict = {}
    assert hue.assign_scenes(nums, room, h.res) and sorted(nums.values()) == [1, 2, 3]
    assert not hue.assign_scenes(nums, room, h.res), "nog een keer geeft niets nieuws"
    d = hue.describe(room, h.res, numbers=nums)
    assert [p["id"] for p in d["points"]] == ["on", "bri", "scene", "reach"] and all(eh._point(p) for p in d["points"])
    by = {p["id"]: p for p in d["points"]}
    assert by["on"]["writable"] and by["bri"]["writable"] and (by["bri"]["min"], by["bri"]["max"]) == (0, 100) and by["scene"]["writable"] and by["reach"]["diag"]
    assert {o["l"]: o["v"] for o in by["scene"]["options"]} == {"geen / uit": 0, "Avond": 1, "Helder": 2, "Relax": 3}, "nummers op volgorde van naam, 0 is geen scène"


def test_a_room_follows_its_group_and_its_active_scene_and_is_unknown_when_no_lamp_is_reachable():
    h = room_house()
    room = h.res[h.dev["Woonkamer"]]
    nums: dict = {}
    hue.assign_scenes(nums, room, h.res)
    assert hue.values(room, h.res, nums) == {"reach": True, "on": False, "bri": 0, "scene": 0}, "uit is helderheid 0 en geen scène, ook als de bridge nog een oude helderheid kent"
    h.group("Woonkamer")["on"]["on"] = True
    h.res[h._id("scene", "Woonkamer/Relax")]["status"]["active"] = "static"
    assert hue.values(room, h.res, nums) == {"reach": True, "on": True, "bri": 60, "scene": nums[h._id("scene", "Woonkamer/Relax")]}
    h.res[h._id("scene", "Woonkamer/Relax")]["status"]["active"] = "inactive"
    assert hue.values(room, h.res, nums)["scene"] == 0, "aan zonder scène"
    h.svc(h.dev["Keuken"], "zigbee_connectivity")["status"] = "connectivity_issue"
    assert hue.values(room, h.res, nums) == {"reach": False}, "alle lampen weg: de stand van de groep is een gok"


def test_room_commands_go_to_the_group_and_a_scene_to_its_own_resource():
    h = room_house()
    room = h.res[h.dev["Woonkamer"]]
    nums: dict = {}
    hue.assign_scenes(nums, room, h.res)
    gl = h.group("Woonkamer")["id"]
    assert hue.point_request(room, h.res, "on", True) == (f"/clip/v2/resource/grouped_light/{gl}", {"on": {"on": True}})
    assert hue.point_request(room, h.res, "bri", 40)[1] == {"on": {"on": True}, "dimming": {"brightness": 40.0}}
    assert hue.point_request(room, h.res, "bri", 0)[1] == {"on": {"on": False}}
    assert hue.point_request(room, h.res, "scene", 0)[1] == {"on": {"on": False}}
    path, body = hue.point_request(room, h.res, "scene", nums[h._id("scene", "Woonkamer/Avond")], nums)
    assert path == f"/clip/v2/resource/scene/{h._id('scene', 'Woonkamer/Avond')}" and body == {"recall": {"action": "active"}}
    with pytest.raises(ValueError, match="bestaat niet"):
        hue.point_request(room, h.res, "scene", 99, nums)


def test_a_room_is_a_block_and_scenes_and_brightness_reach_the_bridge(tmp_path):
    async def go():
        r, hub, fake, bid, dids = await rig(tmp_path, room_house())
        w = room_id(fake, "Woonkamer")
        assert w in dids and hub.devices[w]["type"] == "Hue-kamer" and hub.devices[w]["name"] == "Woonkamer"
        await eventually(lambda: hub.output(f"e:{w}:scene") == 0)
        sc = {o["l"]: o["v"] for o in pts(hub, w)["scene"]["options"]}
        gl, relax = fake.group("Woonkamer")["id"], fake._id("scene", "Woonkamer/Relax")
        assert (await hub.set(w, "scene", sc["Relax"]))["result"] == "applied"
        await eventually(lambda: hub.output(f"e:{w}:scene") == sc["Relax"] and hub.output(f"e:{w}:on") is True)
        await hub.set(w, "bri", 30)
        await eventually(lambda: hub.output(f"e:{w}:bri") == 30)
        await hub.set(w, "bri", 0)
        await eventually(lambda: hub.output(f"e:{w}:on") is False)
        assert hub.output(f"e:{w}:bri") == 0 and hub.output(f"e:{w}:scene") == 0
        await hub.set(w, "scene", sc["Helder"])
        await eventually(lambda: hub.output(f"e:{w}:scene") == sc["Helder"])
        await hub.set(w, "scene", 0)
        await eventually(lambda: hub.output(f"e:{w}:on") is False)
        with pytest.raises(ModelError, match="geen toegestane keuze"):
            await hub.set(w, "scene", 99)
        puts = [(c[1], c[2]) for c in fake.calls if c[0] == "PUT"]
        grp = f"/clip/v2/resource/grouped_light/{gl}"
        assert puts == [(f"/clip/v2/resource/scene/{relax}", {"recall": {"action": "active"}}), (grp, {"on": {"on": True}, "dimming": {"brightness": 30.0}}), (grp, {"on": {"on": False}}),
                        (f"/clip/v2/resource/scene/{fake._id('scene', 'Woonkamer/Helder')}", {"recall": {"action": "active"}}), (grp, {"on": {"on": False}})]
        await shutdown(hub, fake)
    asyncio.run(go())


def test_scene_numbers_stay_when_scenes_change_and_after_a_restart(tmp_path):
    async def go():
        h = room_house()
        r, hub, fake, bid, dids = await rig(tmp_path, h)
        w = room_id(fake, "Woonkamer")
        opts = lambda: {o["l"]: o["v"] for o in pts(hub, w)["scene"]["options"]}  # noqa: E731
        await eventually(lambda: "Relax" in opts())
        first = opts()
        assert first == {"geen / uit": 0, "Avond": 1, "Helder": 2, "Relax": 3}
        helder = h._id("scene", "Woonkamer/Helder")
        h.res.pop(helder)
        h.push("delete", [{"id": helder, "type": "scene"}])
        nacht = h.add_scene("Woonkamer", "Nacht")
        h.push("add", [h.res[nacht]])
        await eventually(lambda: "Nacht" in opts())
        assert opts() == {"geen / uit": 0, "Avond": 1, "Relax": 3, "Nacht": 4}, "de scène die weg is geeft zijn nummer niet door aan een nieuwe"
        h.res[h._id("scene", "Woonkamer/Relax")]["metadata"]["name"] = "Gezellig"
        h.push("update", [{"id": h._id("scene", "Woonkamer/Relax"), "type": "scene", "metadata": {"name": "Gezellig"}}])
        await eventually(lambda: "Gezellig" in opts())
        assert opts()["Gezellig"] == 3, "een andere naam, hetzelfde nummer"
        nums = dict(hub.devices[w]["scenes"])
        hub.close()
        again = EspDevHub(tmp_path, r.engine, port=0, poll=0.05)
        assert again.devices[w]["scenes"] == nums and len(nums) == 4, "ook de nummers van een scène die weg is blijven, zodat ze niet opnieuw worden uitgegeven"
        again.close()
        await fake.stop()
        await asyncio.sleep(0.05)
    asyncio.run(go())
