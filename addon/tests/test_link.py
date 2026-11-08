"""Koppelingen tussen installaties: de handdruk en de berichten, en twee hubs over echte TCP op localhost."""
import asyncio
import json
import os
import socket

import pytest

from kkaddon import cards, link
from kkaddon.engine import Engine
from kkaddon.linkhub import LinkHub
from kkaddon.model import Model, ModelError
from rig import KEY

SECRET = link.new_key()


def test_a_session_opens_only_fresh_messages_from_the_other_side():
    key = link.derive(SECRET)
    nc, ns = os.urandom(16), os.urandom(16)
    a, b = link.Session(key, nc, ns, initiator=True), link.Session(key, nc, ns, initiator=False)
    m1, m2 = a.seal({"t": "x", "n": 1}), a.seal({"t": "x", "n": 2})
    assert b.open(m1) == {"t": "x", "n": 1} and b.open(m2)["n"] == 2
    with pytest.raises(link.LinkError, match="herhaald"):
        b.open(m1)  # opgenomen en opnieuw gestuurd
    with pytest.raises(link.LinkError, match="herhaald"):
        a.open(a.seal({"t": "x"}))  # een eigen bericht teruggekaatst
    other = link.Session(key, nc, os.urandom(16), initiator=False)
    with pytest.raises(link.LinkError, match="niet van de andere"):
        other.open(a.seal({"t": "x"}))  # een andere sessie
    tampered = bytearray(a.seal({"t": "x"}))
    tampered[-1] ^= 1
    with pytest.raises(link.LinkError):
        b.open(bytes(tampered))


def test_the_handshake_needs_the_same_key():
    key, n = link.derive(SECRET), os.urandom(16)
    hello = link.seal_hello(key, "hello", "boven", "Boven", n)
    assert link.open_hello(key, hello, "hello") == ("boven", "Boven", n)
    with pytest.raises(link.LinkError, match="verkeerde koppelsleutel"):
        link.open_hello(link.derive(link.new_key()), hello, "hello")
    with pytest.raises(link.LinkError, match="onverwacht"):
        link.open_hello(key, hello, "welcome")
    with pytest.raises(link.LinkError, match="minstens"):
        link.derive("kort")
    old = link.VERSION
    try:
        link.VERSION = 1  # een add-on van 0.12.0
        hello_v1 = link.seal_hello(key, "hello", "boven", "Boven", n)
    finally:
        link.VERSION = old
    with pytest.raises(link.LinkError, match="andere versie.*werk beide add-ons bij"):
        link.open_hello(key, hello_v1, "hello")
    assert link.split_ref("r:boven:y:lader") == ("boven", "y:lader") and link.split_ref("r:boven:x:y") is None


class FakeTuya:
    def __init__(self):
        self.devices = {"lader": {}}
        self.points = {"101": "connect", "124": "CloseCharging", "25": 2.0}
        self.online = True
        self.sent = []

    def status(self):
        pts = [{"dp": dp, "code": "", "name": f"Punt {dp}", "value": v, "unit": "", "rw": dp == "124", "type": "enum" if dp == "124" else "", "options": [],
                "min": None, "max": None, "step": 1, "hide": False, "diag": False} for dp, v in self.points.items()]
        return [{"id": "lader", "name": "Feyree", "online": self.online, "product": "Feyree EV-lader", "points": pts}]

    def set(self, did, dp, value):
        if dp != "124":
            raise ModelError("niet beschrijfbaar")
        self.sent.append((dp, value))
        self.points[dp] = value


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class House:
    def __init__(self, tmp, name, key=SECRET):
        self.engine = Engine(KEY, Model(), lambda raw: None)
        self.hub = LinkHub(tmp / name, self.engine, port=free_port(), retry=(0.05, 0.2))
        self.name, self.key = name, key

    async def start(self):
        await self.hub.start()
        self.hub.configure({"name": self.name.capitalize(), "key": self.key})
        self.task = asyncio.create_task(self.hub.run())

    async def stop(self):
        self.task.cancel()
        await self.hub.close()


async def until(pred, timeout=5.0, what="voorwaarde"):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"time-out: {what}")


def run(coro):
    asyncio.run(coro)


def test_two_houses_share_their_devices_and_the_owner_executes_a_command(tmp_path):
    async def go():
        a, b = House(tmp_path, "hoofdhuis"), House(tmp_path, "atelier")
        b.hub.tuya = FakeTuya()
        b.engine.model.set_groups([{"id": "hal", "name": "Hal", "entities": ["switch.a", "switch.b"]}])
        await a.start()
        await b.start()
        try:
            a.hub.add_peer("127.0.0.1", b.hub.port)  # één kant opgeven volstaat
            await until(lambda: a.hub.is_up("atelier") and b.hub.is_up("hoofdhuis"), what="gekoppeld")
            await until(lambda: "y:lader" in a.hub.status()["remote"].get("atelier", {}).get("catalog", {}), what="catalogus van de andere kant")
            st = a.hub.status()
            assert st["peers"][0]["state"] == "verbonden" and st["peers"][0]["peer"] == "atelier"
            assert st["remote"]["atelier"]["catalog"] == {"g:hal": {"kind": "lamp", "name": "Hal", "used": False}, "y:lader": {"kind": "tuya", "name": "Feyree", "used": False}}
            assert st["remote"]["atelier"]["objects"] == {} and a.hub.object("atelier", "y:lader") is None, "niets wordt vanzelf gebruikt"
            assert b.hub.conns["hoofdhuis"].want == set() and not b.hub.conns["hoofdhuis"].sent.strip("{}"), "en van wat niet gekozen is gaat de stand niet over de lijn"
            a.hub.set_use("atelier", "y:lader", True)  # alleen de lader
            await until(lambda: "points" in (a.hub.object("atelier", "y:lader") or {}), what="stand van de gekozen lader")
            assert b.hub.conns["hoofdhuis"].want == {"y:lader"} and set(a.hub.status()["remote"]["atelier"]["objects"]) == {"y:lader"}
            with pytest.raises(link.LinkError, match="niet gekozen"):
                await a.hub.command("atelier", "g:hal", op="on")
            a.hub.use["atelier"].append("g:hal")  # buiten de UI om: de eigenaar weigert toch, want hij kreeg die keuze niet
            with pytest.raises(link.LinkError, match="niet gekozen bij de andere installatie"):
                await a.hub.command("atelier", "g:hal", op="on")
            a.hub.use["atelier"].remove("g:hal")
            with pytest.raises(ModelError, match="biedt die installatie niet aan"):
                a.hub.set_use("atelier", "y:weg", True)
            await a.hub.command("atelier", "y:lader", dp="124", value="OpenCharging")
            assert b.hub.tuya.sent == [("124", "OpenCharging")], "de eigenaar voert uit"
            await until(lambda: any(p["dp"] == "124" and p["value"] == "OpenCharging" for p in a.hub.object("atelier", "y:lader")["points"]), what="nieuwe stand terug")
            with pytest.raises(link.LinkError, match="niet beschrijfbaar"):
                await a.hub.command("atelier", "y:lader", dp="25", value="9")
            b.hub.tuya.online = False
            await until(lambda: a.hub.object("atelier", "y:lader")["online"] is False, what="een wijziging gaat vanzelf mee")
            assert json.loads((tmp_path / "hoofdhuis" / "link.json").read_text())["known"]["atelier"]["objects"]["y:lader"] == {"kind": "tuya", "name": "Feyree"}
            await b.stop()
            await until(lambda: not a.hub.is_up("atelier"), what="verbinding weg")
            o = a.hub.object("atelier", "y:lader")
            assert o == {"kind": "tuya", "name": "Feyree", "online": False}, "offline, met de laatst bekende naam"
            assert a.hub.send("atelier", "y:lader", dp="124", value="CloseCharging") is False, "zonder verbinding gaat er geen bevel"
            a.hub.set_use("atelier", "y:lader", False)
            assert a.hub.object("atelier", "y:lader") is None and a.hub.status()["remote"]["atelier"]["catalog"]["y:lader"]["used"] is False
            assert a.hub.forget("atelier") is True and "atelier" not in a.hub.status()["remote"] and "atelier" not in a.hub.use
        finally:
            await a.stop()
            if not b.task.done():
                await b.stop()

    run(go())


def test_a_wrong_key_is_refused_and_two_listings_leave_one_connection(tmp_path):
    async def go():
        a, b, c = House(tmp_path, "hoofdhuis"), House(tmp_path, "atelier"), House(tmp_path, "garage", key=link.new_key())
        for h in (a, b, c):
            await h.start()
        try:
            a.hub.add_peer("127.0.0.1", c.hub.port)
            await until(lambda: a.hub.status()["peers"][0]["state"] == "fout", what="geweigerd")
            assert "koppelsleutel" in a.hub.status()["peers"][0]["error"] and not c.hub.conns
            a.hub.add_peer("127.0.0.1", b.hub.port)
            b.hub.add_peer("127.0.0.1", a.hub.port)  # beide kanten geven elkaar op
            await until(lambda: a.hub.is_up("atelier") and b.hub.is_up("hoofdhuis"))
            await asyncio.sleep(0.5)
            assert a.hub.conns["atelier"].initiator == b.hub.conns["hoofdhuis"].initiator == "atelier", "de verbinding van de kleinste id blijft, aan beide kanten"
            with pytest.raises(ModelError, match="staat er al"):
                a.hub.add_peer("127.0.0.1", b.hub.port)
            with pytest.raises(ModelError, match="IP-adres"):
                a.hub.add_peer("1.2.3.4; rm", 1)
        finally:
            for h in (a, b, c):
                await h.stop()

    run(go())


def test_the_key_and_name_stay_out_of_the_model_and_the_status(tmp_path):
    h = House(tmp_path, "hoofdhuis")
    h.hub.configure({"name": "Hoofdhuis", "key": SECRET})
    assert SECRET not in json.dumps(h.hub.status()) and h.hub.status()["me"]["has_key"] is True
    assert SECRET not in json.dumps(h.engine.model.to_dict())
    h.hub.configure({"name": "Ander naam"})
    assert h.hub.me == {"id": "hoofdhuis", "name": "Ander naam"}, "de id blijft: anders breken de verwijzingen bij de andere installaties"
    with pytest.raises(ModelError, match="minstens"):
        h.hub.configure({"key": "kort"})
    again = LinkHub(tmp_path / "hoofdhuis", Engine(KEY, Model(), lambda raw: None))
    assert again.ready() and again.me["id"] == "hoofdhuis"


def test_what_0_12_already_used_on_the_schema_or_a_card_stays_chosen(tmp_path):
    (tmp_path / "link.json").write_text(json.dumps({"id": "hoofdhuis", "name": "Hoofdhuis", "key": SECRET, "peers": [],
                                                    "known": {"atelier": {"name": "Atelier", "objects": {"k:lader": {"kind": "charger", "name": "Laadpaal"},
                                                                                                        "g:hal": {"kind": "lamp", "name": "Hal"},
                                                                                                        "g:tuin": {"kind": "lamp", "name": "Tuin"}}}}}), encoding="utf-8")
    m = Model()
    m.add_card("charger")
    m.set_card("laadpaal", charger="r:atelier:k:lader")
    m.set_schema({**m.schema, "pages": [{"id": "hal", "name": "Hal"}], "blocks": {"r:atelier:g:hal": {"page": "hal", "x": 0, "y": 0}}})
    hub = LinkHub(tmp_path, Engine(KEY, m, lambda raw: None))
    assert sorted(hub.use["atelier"]) == ["g:hal", "k:lader"], "de tuin stond nergens: die wordt niet vanzelf gekozen"
    assert json.loads((tmp_path / "link.json").read_text())["use"] == hub.use, "en dat is bewaard: de volgende start kiest niets meer vanzelf"
    hub.set_use("atelier", "g:hal", False)
    again = LinkHub(tmp_path, Engine(KEY, m, lambda raw: None))
    assert again.use["atelier"] == ["k:lader"], "een keuze die je daarna maakt, blijft"


class OwnHa:
    """De HA van een installatie, zoals linkhub en sync hem zien: standen, luisteraars, en een bevel dat als stand terugkomt."""

    def __init__(self, engine, states):
        self.engine, self.states, self.calls, self.state_listeners = engine, {k: (v, {}) for k, v in states.items()}, [], []
        engine.virt.set_link(True)
        engine.ha_call = self.call
        engine.ha_snapshot({k: v for k, v in states.items()})

    def call(self, entity, on):
        self.calls.append((entity, on))
        asyncio.get_running_loop().call_soon(self.press, entity, "on" if on else "off")  # de echo komt even later, zoals uit HA
        return True

    def press(self, entity, st):
        """Een stand die in HA verandert: een druk op de touch, of de echo van een bevel."""
        self.states[entity] = (st, {})
        if entity in self.engine.ha_entities():
            self.engine.ha_state(entity, st)
        for cb in self.state_listeners:
            cb(entity)


def test_a_touch_in_one_house_switches_a_lamp_in_the_other_and_the_lamp_wins_after_an_outage(tmp_path):
    async def go():
        hh, at = House(tmp_path, "hoofdhuis"), House(tmp_path, "atelier")
        TOUCH, LAMP = "switch.touch_poort", "switch.lamp_oprit"
        at.engine.model.set_schema({**at.engine.model.schema, "switches": {"poort": {"name": "Touch poort", "gangs": [{"entity": TOUCH, "name": "links"}]}}})
        ha_at, ha_hh = OwnHa(at.engine, {TOUCH: "off"}), OwnHa(hh.engine, {LAMP: "off"})
        at.hub.attach_ha(ha_at)
        await hh.start()
        await at.start()
        try:
            hh.hub.add_peer("127.0.0.1", at.hub.port)
            await until(lambda: f"e:{TOUCH}" in hh.hub.status()["remote"].get("atelier", {}).get("catalog", {}), what="de gang staat in het aanbod")
            assert hh.hub.status()["remote"]["atelier"]["catalog"][f"e:{TOUCH}"] == {"kind": "gang", "name": "Touch poort · links", "busy": False, "used": False}
            hh.hub.set_use("atelier", f"e:{TOUCH}", True)
            ref = f"r:atelier:e:{TOUCH}"
            m = hh.engine.model
            m.set_groups([{"id": "oprit", "name": "Oprit", "entities": [LAMP, ref]}])
            m.set_group("oprit", enabled=True, lamp=[LAMP])  # de lamp hier, de touch in het atelier
            hh.engine.ha_snapshot({LAMP: ha_hh.states[LAMP][0]})  # zoals habridge na een modelwijziging de standen opnieuw ophaalt
            touch = lambda: next(x for x in hh.engine.groups.members["oprit"] if x.entity == ref)
            await until(lambda: touch().avail and touch().known is False, what="de stand van de touch komt via de koppeling")

            ha_at.press(TOUCH, "on")  # iemand drukt op de touch in het atelier
            await until(lambda: (LAMP, True) in ha_hh.calls, what="de lamp in het hoofdhuis gaat aan")
            await asyncio.sleep(0.3)
            assert ha_at.calls == [], "de touch zelf krijgt geen bevel terug: hij staat al goed"

            ha_hh.press(LAMP, "off")  # de lamp wordt hier uitgezet (in HA of met een andere touch)
            await until(lambda: (TOUCH, False) in ha_at.calls, what="de touch in het atelier volgt de lamp")

            hh.hub.remove_peer("127.0.0.1", at.hub.port)  # de koppeling valt weg
            await hh.hub.conns["atelier"].ws.close()
            await until(lambda: not touch().avail, what="de touch is niet meer beschikbaar")
            ha_at.press(TOUCH, "on")  # tijdens de uitval: de touch schakelt zijn eigen relais, de lamp hier merkt niets
            await asyncio.sleep(0.3)
            assert ha_hh.calls[-1] == (LAMP, True) and ha_hh.states[LAMP][0] == "off", "de lamp blijft zoals ze stond"
            calls_before = len(ha_hh.calls)

            hh.hub.add_peer("127.0.0.1", at.hub.port)  # de koppeling is terug
            await until(lambda: ha_at.calls[-1] == (TOUCH, False), what="de touch neemt de stand van de lamp over")
            await asyncio.sleep(0.3)
            assert len(ha_hh.calls) == calls_before, "de lamp gaat niet vanzelf aan: zij heeft gelijk"
            assert any(e["t"] == "sync" and e.get("what") == "aligned" for e in hh.engine.log)

            at.engine.model.set_groups([{"id": "poort", "name": "Poort", "entities": [TOUCH, "switch.iets"]}])
            at.engine.model.set_group("poort", enabled=True)  # het atelier stuurt de touch nu zelf: bezet
            await until(lambda: hh.hub.status()["remote"]["atelier"]["catalog"][f"e:{TOUCH}"]["busy"], what="bezet in het aanbod")
            with pytest.raises(link.LinkError, match="actieve kring"):
                await hh.hub.command("atelier", f"e:{TOUCH}", op="on")
        finally:
            await hh.stop()
            await at.stop()

    run(go())


def test_a_remote_gang_is_a_block_with_a_touch_and_a_relay_and_can_be_an_input_of_a_lamp(tmp_path):
    from kkaddon import schema

    h = House(tmp_path, "hoofdhuis")
    h.hub.known = {"atelier": {"name": "Atelier", "objects": {"e:switch.touch_poort": {"kind": "gang", "name": "Touch poort · links", "busy": False}}}}
    h.hub.use = {"atelier": ["e:switch.touch_poort"]}
    m, ref = h.engine.model, "r:atelier:e:switch.touch_poort"
    m.set_groups([{"id": "oprit", "name": "Oprit", "entities": ["switch.lamp_oprit", "switch.iets"]}])
    blocks = {b["key"]: b for b in schema.view(m, h.engine.snapshot())["blocks"]}
    b = blocks[ref]
    assert b["kind"] == "remote" and b["sub"] == "gang" and b["online"] is False, "offline tot de koppeling er is, met de bewaarde naam"
    assert b["gangs"] == [{"i": 0, "label": "Touch poort · links", "entity": ref, "touch": True, "relay": True, "on": None}]
    schema.wire(m, h.engine, "g:oprit", "in", {"entity": ref})
    assert ref in m.groups["oprit"]["entities"], "een lid van de kring, zoals een HA-entiteit"
    lamp = {b["key"]: b for b in schema.view(m, h.engine.snapshot())["blocks"]}["g:oprit"]
    c = next(c for c in lamp["inputs"] if c["ref"]["entity"] == ref)
    assert c["sw"] == ref and (c["swName"], c["label"]) == ("Atelier", "Touch poort · links"), "de lamp verwijst naar het blok van de andere installatie"
    m.set_light(5, 1, name="Plafond")
    with pytest.raises(ModelError, match="lamp via Home Assistant"):
        schema.wire(m, h.engine, "l5.1", "in", {"entity": ref})
    with pytest.raises(ModelError, match="ongeldige entiteit"):
        m.set_group("oprit", entities=["r:atelier:y:lader"])  # een Tuya-toestel is geen gang
