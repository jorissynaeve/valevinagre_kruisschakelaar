"""Laadpalen met sessies: één starter tegelijk, andere installaties zien niets tijdens een sessie, en de log bij de eigenaar."""
import json
from types import SimpleNamespace

import pytest

from kkaddon import cards, link
from kkaddon.chargehub import ChargeHub
from kkaddon.engine import Engine
from kkaddon.model import Model, ModelError
from rig import KEY
from test_link import FakeTuya as LinkTuya, House, run, until


class Tuya:
    """Een lader: 101 status, 25 meterstand, 120 laadtijd, 124 start/stop."""

    def __init__(self, **v):
        self.devices = {"lader": {}}
        self.values = {"101": "connect", "25": 10.0, "120": "00:00:00", "124": "CloseCharging", **v}
        self.online, self.sent = True, []

    def status(self):
        return [{"id": "lader", "name": "Lader", "online": self.online, "product": "Feyree EV-lader",
                 "points": [{"dp": dp, "name": f"Punt {dp}", "value": v, "unit": "", "rw": dp == "124"} for dp, v in self.values.items()]}]

    def set(self, did, dp, value):
        self.sent.append((dp, value))
        self.values[dp] = value


class Clock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def __call__(self):
        return self.t


CFG = {"name": "Laadpaal", "device": "lader", "inputs": {"status": "101", "time": "120", "energy": "25"}, "price": 0.25,
       "target": {"dp": "124", "on": "OpenCharging", "off": "CloseCharging"}}


def make(tmp_path, **tv):
    t, clock = Tuya(**tv), Clock()
    hub = ChargeHub(tmp_path, Engine(KEY, Model(), lambda raw: None), t, clock=clock)
    hub.home, hub.peer_name = lambda: "Atelier", lambda pid: pid.capitalize()
    hub.add(CFG)
    return hub, t, clock


def test_one_session_at_a_time_and_only_the_starter_or_the_owner_stops(tmp_path):
    hub, t, _ = make(tmp_path)
    hub.start("lader", hub.who_peer("hoofdhuis"))
    assert t.sent == [("124", "OpenCharging")]
    with pytest.raises(ModelError, match="bezet"):
        hub.start("lader", hub.who_peer("garage"))
    with pytest.raises(ModelError, match="bezet"):
        hub.start("lader", hub.who_self())
    with pytest.raises(ModelError, match="alleen het huis dat gestart heeft"):
        hub.stop("lader", hub.who_peer("garage"))
    assert t.sent == [("124", "OpenCharging")], "een geweigerde stop stuurt niets"
    hub.stop("lader", hub.who_peer("hoofdhuis"))
    assert t.sent[-1] == ("124", "CloseCharging") and not hub.active
    assert hub.log[-1]["name"] == "Hoofdhuis" and hub.log[-1]["reason"] == "gestopt door de starter"
    hub.start("lader", hub.who_peer("garage"))
    hub.stop("lader", hub.who_self())  # de eigenaar als noodknop
    assert hub.log[-1]["name"] == "Garage" and hub.log[-1]["reason"] == "gestopt door de eigenaar" and hub.log[-1]["stopped_by"] == "Atelier"
    with pytest.raises(ModelError, match="geen laadsessie"):
        hub.stop("lader", hub.who_self())


def test_another_house_sees_nothing_but_busy_during_a_session(tmp_path):
    hub, t, _ = make(tmp_path)
    free = hub.view("lader", "garage")
    assert free["phase"] == "free" and free["act"] == "start" and free["status"] == "connect", "zonder sessie is er niets te verbergen"
    assert free["time"] is None and free["energy"] is None, "de waarden van een vorige sessie wel"
    hub.start("lader", hub.who_peer("hoofdhuis"))
    t.values.update({"101": "charing", "25": 13.0, "120": "00:30:00"})
    other = hub.view("lader", "garage")
    assert other == {"kind": "charger", "name": "Laadpaal", "online": True, "phase": "busy", "act": None}, "geen status, kWh, tijd, naam van de starter"
    assert "hoofdhuis" not in json.dumps(other).lower() and "Hoofdhuis" not in json.dumps(other)
    mine = hub.view("lader", "hoofdhuis")
    assert mine["phase"] == "mine" and mine["act"] == "stop" and mine["energy"] == 3.0 and mine["time"] == "00:30:00" and mine["cost"] == 0.75 and mine["status"] == "charing"
    own = hub.view("lader", None)
    assert own["phase"] == "other" and own["by"] == "Hoofdhuis" and own["act"] == "stop", "de eigenaar ziet alles en kan stoppen"
    assert hub.export("garage")["k:lader"]["phase"] == "busy"
    t.online = False
    assert hub.view("lader", "garage")["phase"] == "busy", "ook als hij wegvalt: niets meer dan bezet"
    assert hub.view("lader", "hoofdhuis") == {"kind": "charger", "name": "Laadpaal", "online": False, "phase": "mine", "act": None}


def test_a_session_ends_when_the_charger_is_done_and_counts_only_its_own_kwh(tmp_path):
    hub, t, clock = make(tmp_path)
    hub.start("lader", hub.who_peer("hoofdhuis"))
    t.values["101"] = "charing"
    hub.tick()
    clock.t += 600
    t.values["25"] = 12.0
    hub.tick()
    t.values["101"] = "finish"
    clock.t += 5
    hub.tick()
    assert "lader" in hub.active, "een korte hapering is geen einde"
    clock.t += 30
    hub.tick()
    e = hub.log[-1]
    assert "lader" not in hub.active and e["reason"] == "klaar of losgekoppeld" and e["kwh"] == 2.0 and e["cost"] == 0.5 and e["tariff"] == 0.25
    assert e["name"] == "Hoofdhuis" and e["seconds"] == 635 and e["end"] - e["start"] == 635


def test_a_meter_that_restarts_with_the_session_is_not_subtracted(tmp_path):
    hub, t, clock = make(tmp_path, **{"25": 40.0})
    hub.start("lader", hub.who_peer("hoofdhuis"))
    t.values.update({"101": "charing", "25": 0.0})  # de laadpaal begon met een nieuwe teller
    hub.tick()
    t.values["25"] = 6.5
    hub.tick()
    t.values["101"] = "finish"
    hub.tick()
    clock.t += 30
    hub.tick()
    assert hub.log[-1]["kwh"] == 6.5


def test_a_start_that_never_charges_is_given_up_and_a_gone_charger_does_not_end_a_session(tmp_path):
    hub, t, clock = make(tmp_path)
    hub.start("lader", hub.who_peer("hoofdhuis"))
    clock.t += 100
    hub.tick()
    assert "lader" in hub.active
    t.online = False
    clock.t += 5000
    hub.tick()
    assert "lader" in hub.active, "niets weten is niet hetzelfde als gestopt"
    t.online = True
    hub.tick()
    assert hub.log[-1]["reason"] == "niet gestart" and hub.log[-1]["kwh"] == 0.0


def test_charging_that_the_addon_did_not_start_is_logged_as_local(tmp_path):
    hub, t, clock = make(tmp_path, **{"101": "charing"})
    hub.tick()
    assert hub.active["lader"]["by"] == "-" and hub.view("lader", "garage")["phase"] == "busy"
    with pytest.raises(ModelError, match="alleen het huis dat gestart heeft"):
        hub.stop("lader", hub.who_peer("garage"))
    t.values["25"] = 11.5
    t.values["101"] = "finish"
    hub.tick()
    clock.t += 60
    hub.tick()
    assert hub.log[-1]["name"] == "Lokaal (aan de laadpaal)" and hub.log[-1]["kwh"] == 1.5 and not hub.active


def test_a_session_survives_a_restart_and_the_log_goes_to_csv_and_ha(tmp_path):
    hub, t, clock = make(tmp_path)
    hub.start("lader", hub.who_peer("hoofdhuis"))
    again = ChargeHub(tmp_path, Engine(KEY, Model(), lambda raw: None), t, clock=clock)
    assert again.active["lader"]["by"] == "hoofdhuis", "ook na een herstart weet de eigenaar wie er laadt"
    assert again.view("lader", "garage")["phase"] == "busy"
    again.peer_name = lambda pid: pid.capitalize()
    t.values.update({"101": "charing", "25": 11.5})
    again.tick()
    again.stop("lader", again.who_peer("hoofdhuis"))
    csv_text = again.csv()
    assert csv_text.splitlines()[0].startswith("laadpaal;huis;start;einde") and "Lader" not in csv_text and ";Hoofdhuis;" in csv_text and ";1,500;" in csv_text
    assert again.states()[again.t("lader", "last_kwh")] == ("1.500", True) and again.states()[again.t("lader", "session")] == ("vrij", True)
    assert [k for k in again.discovery() if k.endswith("_last_by/config")]
    assert again.status()[0]["log"][0]["kwh"] == 1.5 and again.status()[0]["session"] is None


def test_a_charger_with_a_session_cannot_be_removed_or_moved(tmp_path):
    hub, _, _ = make(tmp_path)
    hub.start("lader", hub.who_self())
    with pytest.raises(ModelError, match="stop ze eerst"):
        hub.remove("lader")
    assert hub.devices_used() == {"lader"}
    hub.stop("lader", hub.who_self())
    assert hub.remove("lader") is True and hub.devices_used() == set()


# ---------------------------------------------------------------- over de koppeling


def test_three_houses_the_owner_logs_who_charges_and_the_others_see_busy(tmp_path):
    async def go():
        a, b, c = House(tmp_path, "hoofdhuis"), House(tmp_path, "atelier"), House(tmp_path, "garage")
        b.hub.tuya = LinkTuya()
        clk = Clock()
        b.hub.charge = ChargeHub(tmp_path / "atelier", b.engine, b.hub.tuya, clock=clk)
        b.hub.charge.home, b.hub.charge.peer_name = lambda: "Atelier", b.hub.peer_name
        b.hub.charge.add({"name": "Laadpaal", "device": "lader", "inputs": {"status": "101", "energy": "25"}, "target": {"dp": "124", "on": "OpenCharging", "off": "CloseCharging"}, "price": 0.5})
        for h in (a, b, c):
            await h.start()
        try:
            for h in (a, c):
                h.hub.add_peer("127.0.0.1", b.hub.port)
                await until(lambda h=h: "k:lader" in h.hub.status()["remote"].get("atelier", {}).get("catalog", {}), what="catalogus")
                assert "y:lader" not in h.hub.status()["remote"]["atelier"]["catalog"], "het toestel zelf wordt niet meer aangeboden: dan omzeilen ze het blok"
                h.hub.set_use("atelier", "k:lader", True)
                h.engine.model.add_card("charger")
                h.engine.model.set_card("laadpaal", charger="r:atelier:k:lader")
            live_a, live_c = (cards.Live(h.engine.model, link=h.hub) for h in (a, c))
            await until(lambda: live_a.value("kk.laadpaal__run", "nl")["value"] == "Start" and live_c.value("kk.laadpaal__run", "nl")["value"] == "Start", what="beide kunnen starten")

            assert live_a.press("kk.laadpaal__run", "button", None) is True
            await until(lambda: "lader" in b.hub.charge.active, what="sessie bij de eigenaar")
            assert b.hub.charge.active["lader"]["by"] == "hoofdhuis" and b.hub.charge.active["lader"]["name"] == "Hoofdhuis", "de eigenaar noteert wie het was, uit de koppeling"
            assert b.hub.tuya.sent == [("124", "OpenCharging")]
            b.hub.tuya.points.update({"101": "charing", "25": 5.0})
            await until(lambda: live_a.value("kk.laadpaal__run", "nl")["value"] == "Stop", what="de starter kan stoppen")
            await until(lambda: live_c.value("kk.laadpaal__run", "nl")["name"] == "Laadpaal bezet", what="de ander ziet bezet")
            assert live_a.value("kk.laadpaal__energy", "nl")["value"] == "3.00 kWh" and live_a.value("kk.laadpaal__price", "nl")["value"] == "€ 1.50"
            assert live_c.value("kk.laadpaal__energy", "nl")["value"] == "—" and live_c.value("kk.laadpaal__price", "nl")["value"] == "—"
            assert c.hub.object("atelier", "k:lader") == {"kind": "charger", "name": "Laadpaal", "online": True, "phase": "busy", "act": None}, "ook niet over de lijn"
            assert c.hub.conns["atelier"].ws is not None and "Hoofdhuis" not in json.dumps(c.hub.remote), "de naam van de starter bereikt de ander niet"

            assert live_c.press("kk.laadpaal__run", "button", None) is False
            with pytest.raises(link.LinkError, match="bezet"):
                await c.hub.command("atelier", "k:lader", op="start")
            with pytest.raises(link.LinkError, match="alleen het huis dat gestart heeft"):
                await c.hub.command("atelier", "k:lader", op="stop")
            conn = SimpleNamespace(peer="garage", want={"y:lader", "k:lader"})
            assert "hoort bij een laadpaal" in b.hub._execute(conn, {"obj": "y:lader", "dp": "124", "value": "CloseCharging"}), "ook niet langs het datapunt"
            assert b.hub.tuya.sent == [("124", "OpenCharging")], "niets van dit alles bereikte de laadpaal"

            await a.stop()  # de starter valt weg: de sessie loopt door
            await until(lambda: not b.hub.is_up("hoofdhuis"), what="verbinding weg")
            b.hub.charge.tick()
            assert "lader" in b.hub.charge.active
            b.hub.tuya.points["101"] = "finish"
            b.hub.charge.tick()
            clk.t += 60
            b.hub.charge.tick()
            e = b.hub.charge.log[-1]
            assert e["name"] == "Hoofdhuis" and e["kwh"] == 3.0 and e["cost"] == 1.5 and e["reason"] == "klaar of losgekoppeld"
            await until(lambda: live_c.value("kk.laadpaal__run", "nl")["value"] == "Start", what="weer vrij voor de ander")
            assert live_c.value("kk.laadpaal__run", "nl")["name"] == "Aangesloten" or live_c.value("kk.laadpaal__run", "nl")["name"] == "Klaar"
        finally:
            for h in (a, b, c):
                if not h.task.done():
                    await h.stop()

    run(go())


# ---------------------------------------------------------------- schema en web-API


def test_the_charger_is_a_block_and_the_card_points_to_it(tmp_path):
    from kkaddon import schema
    hub, _, _ = make(tmp_path)
    eng = hub.engine
    eng.model.add_card("charger")
    eng.model.set_card("laadpaal", charger="k:lader")
    blocks = {b["key"]: b for b in schema.view(eng.model, eng.snapshot())["blocks"]}
    assert blocks["k:lader"]["kind"] == "charge" and blocks["k:lader"]["log"] == [] and blocks["k:lader"]["view"]["phase"] == "free"
    assert blocks["c:laadpaal"]["device"] == {"name": "Laadpaal", "online": True, "key": "k:lader"}
    eng.model.set_card("laadpaal", charger="k:weg")
    assert next(b for b in schema.view(eng.model, eng.snapshot())["blocks"] if b["key"] == "c:laadpaal")["device"] is None, "een laadpaal die weg is: de kaart zegt het"


def test_the_web_api_adds_runs_and_removes_a_charger_and_serves_the_log(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from kkaddon import web
    from kkaddon.settings import Settings

    async def go():
        t = Tuya()
        eng = Engine(KEY, Model(), lambda raw: None)
        hub = ChargeHub(tmp_path, eng, t)
        ctx = web.Ctx(Settings(data_dir=tmp_path, key=KEY, key_generated=False, dev_mode=True), eng, tuya=None, charge=hub)
        c = TestClient(TestServer(web.make_app(ctx)))
        await c.start_server()
        try:
            eng.model.set_schema({**eng.model.schema, "pages": [{"id": "hal", "name": "Hal"}]})
            r = await c.post("/api/charge", json={**CFG, "page": "hal", "x": 10, "y": 20})
            assert r.status == 200 and (await r.json())["key"] == "k:lader"
            assert (await c.put("/api/charge/lader", json={"price": 0.4, "start_when": "connect"})).status == 200
            assert (await c.put("/api/charge/lader", json={"price": -1})).status == 400
            assert (await c.post("/api/charge/lader/start")).status == 200 and t.sent == [("124", "OpenCharging")]
            assert (await c.post("/api/charge/lader/start")).status == 400, "bezet"
            st = await (await c.get("/api/state")).json()
            assert st["charge"][0]["session"]["name"] == "Dit huis"
            assert (await c.delete("/api/charge/lader")).status == 400, "er loopt een sessie"
            live = await (await c.get("/api/charge/sessions")).json()
            assert live["me"] == "Dit huis" and live["chargers"] == {"lader": "Laadpaal"} and live["sessions"] == [] and live["active"][0]["name"] == "Dit huis"
            assert (await c.post("/api/charge/lader/stop")).status == 200
            done = await (await c.get("/api/charge/sessions")).json()
            assert done["active"] == [] and done["sessions"][0]["by"] == "" and done["sessions"][0]["charger"] == "lader", "alles voor de pagina Laden: filter en factuur gebeuren daar"
            r = await c.get("/api/charge/lader/log.csv")
            text = await r.text()
            assert r.status == 200 and "text/csv" in r.headers["Content-Type"] and "Dit huis" in text and "attachment" in r.headers["Content-Disposition"]
            assert (await c.delete("/api/charge/lader")).status == 200 and "k:lader" not in eng.model.schema["blocks"]
        finally:
            await c.close()

    run(go())
