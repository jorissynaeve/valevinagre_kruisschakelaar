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


def test_a_remote_charger_on_a_panel_card(tmp_path):
    async def go():
        a, b = House(tmp_path, "hoofdhuis"), House(tmp_path, "atelier")
        b.hub.tuya = FakeTuya()
        await a.start()
        await b.start()
        try:
            a.hub.add_peer("127.0.0.1", b.hub.port)
            await until(lambda: "y:lader" in a.hub.status()["remote"].get("atelier", {}).get("catalog", {}))
            a.hub.set_use("atelier", "y:lader", True)
            await until(lambda: a.hub.object("atelier", "y:lader") is not None and a.hub.object("atelier", "y:lader")["online"])
            m = a.engine.model
            m.add_card("charger")
            m.set_card("laadpaal", tuya="r:atelier:y:lader")
            live = cards.Live(m, link=a.hub)
            assert live.value("kk.laadpaal__run", "nl") == {"kind": "button", "name": "Aangesloten", "value": "Start", "color": cards.DEFAULT_COLOR}
            assert live.value("kk.laadpaal__energy", "nl")["value"] == "2.00 kWh"
            assert live.press("kk.laadpaal__run", "button", None) is True
            await until(lambda: b.hub.tuya.sent == [("124", "OpenCharging")], what="starten via de andere installatie")
            await until(lambda: live.value("kk.laadpaal__run", "nl")["value"] == "Stop", what="knop wordt Stop")
            await b.stop()
            await until(lambda: not a.hub.is_up("atelier"))
            assert live.value("kk.laadpaal__run", "nl")["name"] == "Lader offline"
            assert live.press("kk.laadpaal__run", "button", None) is False
        finally:
            await a.stop()

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
                                                    "known": {"atelier": {"name": "Atelier", "objects": {"y:lader": {"kind": "tuya", "name": "Feyree"},
                                                                                                        "g:hal": {"kind": "lamp", "name": "Hal"},
                                                                                                        "g:tuin": {"kind": "lamp", "name": "Tuin"}}}}}), encoding="utf-8")
    m = Model()
    m.add_card("charger")
    m.set_card("laadpaal", tuya="r:atelier:y:lader")
    m.set_schema({**m.schema, "pages": [{"id": "hal", "name": "Hal"}], "blocks": {"r:atelier:g:hal": {"page": "hal", "x": 0, "y": 0}}})
    hub = LinkHub(tmp_path, Engine(KEY, m, lambda raw: None))
    assert sorted(hub.use["atelier"]) == ["g:hal", "y:lader"], "de tuin stond nergens: die wordt niet vanzelf gekozen"
    assert json.loads((tmp_path / "link.json").read_text())["use"] == hub.use, "en dat is bewaard: de volgende start kiest niets meer vanzelf"
    hub.set_use("atelier", "g:hal", False)
    again = LinkHub(tmp_path, Engine(KEY, m, lambda raw: None))
    assert again.use["atelier"] == ["y:lader"], "een keuze die je daarna maakt, blijft"
