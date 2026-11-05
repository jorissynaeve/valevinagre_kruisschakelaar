"""Waarden van Tuya-toestellen: als ingang van de logica, op een kaart van het NSPanel, en de routine Tuya instellen."""
import pytest

from kkaddon import cards
from kkaddon.engine import Engine
from kkaddon.logic import evaluate
from kkaddon.model import Model, ModelError
from kkaddon.points import Points
from rig import KEY


class FakeTuya:
    """Twee toestellen: een stroommeter (alleen-lezen) en een lader (124 beschrijfbaar)."""

    def __init__(self):
        self.devices = {"meter": {}, "lader": {}}
        self.values = {"meter": {"19": 2.5, "1": True}, "lader": {"124": "CloseCharging", "115": 16}}
        self.online = {"meter": True, "lader": True}
        self.sent, self.refuse = [], None

    def status(self):
        def pt(dp, v, name, unit="", rw=False):
            return {"dp": dp, "name": name, "value": v, "unit": unit, "rw": rw, "type": "", "options": [], "hide": False, "diag": False}
        names = {"19": ("Vermogen", "kW"), "1": ("Aan", ""), "124": ("Laden", ""), "115": ("Laadstroom", "A")}
        return [{"id": d, "name": d.capitalize(), "online": self.online[d],
                 "points": [pt(dp, v, names[dp][0], names[dp][1], d == "lader") for dp, v in self.values[d].items()]} for d in self.devices]

    def set(self, did, dp, value):
        if self.refuse:
            raise ModelError(self.refuse)
        self.sent.append((did, dp, value))
        self.values[did][dp] = value


def test_a_tuya_value_is_a_number_for_the_logic_and_a_comparison_gives_0_or_1():
    points = Points(FakeTuya())
    mods = {"p": {"type": "dp", "device": "meter", "dp": "19", "inputs": {}},
            "hoog": {"type": "above", "value": 3.5, "inputs": {"a": "m:p"}},
            "laag": {"type": "below", "value": 3.5, "inputs": {"a": "m:p"}},
            "aan": {"type": "dp", "device": "meter", "dp": "1", "inputs": {}}}
    v = evaluate(mods, lambda t: None, 600, None, points.number)
    assert (v["p"], v["hoog"], v["laag"], v["aan"]) == (2.5, False, True, 1.0), "aan/uit telt als 1/0"
    points.tuya.values["meter"]["19"] = 4.2
    assert evaluate(mods, lambda t: None, 600, None, points.number)["hoog"] is True
    points.tuya.online["meter"] = False
    v = evaluate(mods, lambda t: None, 600, None, points.number)
    assert v["p"] is None and v["hoog"] is None, "offline is onbekend, en onbekend blokkeert nooit iets"
    assert points.text("meter", "19") == "4.2 kW" and points.text("meter", "1") == "aan"


def test_the_model_checks_the_new_modules_and_routines():
    m = Model()
    mid = m.add_module("dp")
    m.set_module(mid, device="r:atelier:y:meter", dp="19")
    with pytest.raises(ModelError, match="Tuya-toestel"):
        m.set_module(mid, device="r:atelier:g:hal")
    with pytest.raises(ModelError, match="datapunt"):
        m.set_module(mid, dp="vermogen")
    hoog = m.add_module("above", value=3.5)
    m.set_module(hoog, inputs={"a": f"m:{mid}"})
    with pytest.raises(ModelError, match="getal"):
        m.set_module(hoog, inputs={"a": "m:" + m.add_module("time")})  # een tijd is geen getal
    aid = m.add_action()
    assert m.actions[aid]["enabled"] is False, "een nieuwe routine staat uit"
    m.set_action(aid, device="lader", dp="124", on_value="OpenCharging", off_value="CloseCharging", inputs={"trigger": f"m:{hoog}"})
    with pytest.raises(ModelError, match="0 of 1"):
        m.set_action(aid, inputs={"trigger": f"m:{mid}"})
    with pytest.raises(ModelError, match="interval"):
        m.set_action(aid, interval=1)
    m.remove_module(hoog)
    assert m.actions[aid]["inputs"] == {}, "een verdwenen bron is niet meer aangesloten"
    other = Model()
    other.load_dict(m.to_dict())
    assert other.actions == m.actions


class Rig:
    def __init__(self):
        self.t = [1000.0]
        self.m = Model()
        self.e = Engine(KEY, self.m, lambda raw: None, clock=lambda: self.t[0])
        self.tuya = FakeTuya()
        self.e.points = Points(self.tuya)
        p = self.m.add_module("dp")
        self.m.set_module(p, device="meter", dp="19")
        self.hoog = self.m.add_module("above", value=3.5)
        self.m.set_module(self.hoog, inputs={"a": f"m:{p}"})
        self.aid = self.m.add_action("Laadstroom")
        self.m.set_action(self.aid, device="lader", dp="115", on_value="8", off_value="16", interval=60, inputs={"trigger": f"m:{self.hoog}"})

    def power(self, kw, after=0.0):
        self.t[0] += after
        self.tuya.values["meter"]["19"] = kw
        self.e.actions.tick()


def test_the_routine_is_careful_off_first_state_only_changes_and_not_too_often():
    r = Rig()
    r.power(5.0)
    r.power(1.0, 10)
    assert r.tuya.sent == [], "uit: doet niets, ook als de ingang verandert"
    r.m.set_action(r.aid, enabled=True)
    r.power(1.0, 10)
    assert r.tuya.sent == [], "aanzetten zet niets: de stand van de ingang is niet veranderd"
    r.power(4.0, 10)
    assert r.tuya.sent == [("lader", "115", "8")], "boven de grens: de laadstroom omlaag"
    r.power(3.0, 10)
    assert len(r.tuya.sent) == 1 and "wacht" in r.e.actions.view(r.aid)["text"], "rond de grens: niet vaker dan het interval"
    r.power(3.0, 60)
    assert r.tuya.sent[-1] == ("lader", "115", "16"), "na het interval nog altijd onder de grens: nu wel"
    r.tuya.online["meter"] = False
    r.power(4.0, 120)
    assert len(r.tuya.sent) == 2 and "onbekend" in r.e.actions.view(r.aid)["text"], "een onbekende ingang doet niets"
    r.tuya.online["meter"] = True
    r.power(4.5, 10)
    assert len(r.tuya.sent) == 2, "terug bekend: dat is weer een eerste stand, geen wijziging"
    r.tuya.refuse = "waarde buiten bereik"
    r.power(2.0, 120)
    assert len(r.tuya.sent) == 2 and "buiten bereik" in r.e.actions.view(r.aid)["text"] and not r.e.actions.view(r.aid)["ok"]
    assert any(ev["t"] == "action" and ev["what"] == "failed" for ev in r.e.log)
    r.tuya.refuse = None
    r.power(2.0, 30)
    assert len(r.tuya.sent) == 2, "opnieuw proberen pas na het interval"
    r.power(2.0, 31)
    assert r.tuya.sent[-1] == ("lader", "115", "16"), "en dan wel"


def test_a_restart_never_sets_anything_by_itself():
    r = Rig()
    r.m.set_action(r.aid, enabled=True)
    r.power(4.0)
    assert r.tuya.sent == []
    from kkaddon.actions import Actions
    r.e.actions = Actions(r.e)  # herstart: wat al gezet was weet de routine niet meer
    r.power(4.0, 1)
    r.power(4.0, 100)
    assert r.tuya.sent == [], "na een herstart is de stand een eerste stand"
    r.m.set_action(r.aid, off_value="")
    r.power(1.0, 100)
    assert r.tuya.sent == [], "een lege waarde bij 0: dan niets"


class FakeLink:
    def __init__(self, obj):
        self.obj, self.sent = obj, []

    def object(self, peer, obj):
        return self.obj if (peer, obj) == ("atelier", "y:meter") else None

    def is_up(self, peer):
        return True

    def send(self, peer, obj, **cmd):
        self.sent.append((peer, obj, cmd))
        return True


def test_values_on_a_panel_card_here_and_from_another_installation():
    m = Model()
    m.add_card("grid", "Meters")
    m.set_card("meters", layout="list", items=[{"ref": "y:meter", "dp": "19"}, {"ref": "r:atelier:y:meter", "dp": "19", "name": "Atelier"}])
    m.add_card("power", "Stroom")
    m.set_card("stroom", items=[{"tuya": "y:meter", "dp": "19", "levels": [3, 4]}])
    with pytest.raises(ModelError, match="Tuya-toestel"):
        m.set_card("meters", items=[{"ref": "g:hal", "dp": "19"}])
    m.add_panel("P")
    m.set_panel("p", cards=["meters", "stroom"])
    cfg = cards.config(m, "p")
    assert cfg["cards"][0]["entities"] == [{"entity": "kk.meters__0", "name": None, "icon": "mdi:gauge"}, {"entity": "kk.meters__1", "name": "Atelier", "icon": "mdi:gauge"}]
    assert cfg["cards"][1]["entities"][0] == {"entity": "kk.stroom__p0", "name": None, "icon": "mdi:flash", "speed": 0}
    link = FakeLink({"kind": "tuya", "name": "Meter", "online": True, "points": [{"dp": "19", "name": "Vermogen", "value": 1.25, "unit": "kW"}]})
    live = cards.Live(m, tuya=FakeTuya(), link=link)
    assert live.value("kk.meters__0", "nl") == {"kind": "text", "name": "Vermogen", "value": "2.5 kW", "color": cards.DEFAULT_COLOR}
    assert live.value("kk.meters__1", "nl")["value"] == "1.25 kW"
    from kkaddon.panel import rgb565
    assert live.value("kk.stroom__p0", "nl")["color"] == rgb565([0, 255, 0]), "2.5 onder de eerste grens: groen"
    live.tuya.values["meter"]["19"] = 3.5
    assert live.value("kk.stroom__p0", "nl")["color"] == rgb565([255, 165, 0])
    live.tuya.online["meter"] = False
    assert live.value("kk.meters__0", "nl")["value"] == "—"
    assert Points(link=link).set("r:atelier:y:meter", "19", "1") is True and link.sent == [("atelier", "y:meter", {"dp": "19", "value": "1"})]


def test_an_input_can_point_straight_at_a_datapoint():
    points = Points(FakeTuya())
    mods = {"hoog": {"type": "above", "value": 2.0, "inputs": {"a": "v:19@meter"}}, "weg": {"type": "above", "value": 2.0, "inputs": {"a": "v:19@bestaat_niet"}}}
    v = evaluate(mods, lambda t: None, 600, None, points.number)
    assert v["hoog"] is True and v["weg"] is None, "een toestel dat er niet is: onbekend, geen fout"
    m = Model()
    hoog = m.add_module("above", value=3.5)
    m.set_module(hoog, inputs={"a": "v:19@r:atelier:y:meter"})
    with pytest.raises(ModelError, match="0 of 1"):
        m.set_module(m.add_module("and"), inputs={"a": "v:19@meter"})  # een waarde is geen 0/1
    with pytest.raises(ModelError, match="ongeldige bron"):
        m.set_module(hoog, inputs={"a": "v:19@r:atelier:g:hal"})  # alleen een Tuya-toestel
    sid = m.add_sprinkler("Tuin")
    m.set_sprinkler(sid, inputs={"cycles": "v:115@lader"})
    r = Rig()
    r.m.set_module(r.hoog, inputs={"a": "v:19@meter"})
    assert r.e.ref_value("v:19@meter") == 2.5 and r.e.ref_value(f"m:{r.hoog}") is False


def test_the_charger_card_has_inputs_and_an_output_instead_of_a_fixed_charger():
    m = Model()
    m.load_dict({"version": 1, "cards": {"oud": {"type": "charger", "name": "Oud", "tuya": "r:atelier:y:lader", "price": 0.3}}}, save=False)
    assert m.cards["oud"]["inputs"] == {"status": "v:101@r:atelier:y:lader", "time": "v:120@r:atelier:y:lader", "energy": "v:25@r:atelier:y:lader"}
    assert m.cards["oud"]["target"] == {"device": "r:atelier:y:lader", "dp": "124", "on": "OpenCharging", "off": "CloseCharging"}, "een Feyree-kaart van vroeger blijft werken"
    cid = m.add_card("charger", "Stekker")
    m.set_card(cid, inputs={"energy": "v:19@meter"}, target={"device": "meter", "dp": "1", "on": "ON", "off": "OFF"})  # een slimme stekker als lader
    with pytest.raises(ModelError, match="datapunt"):
        m.set_card(cid, inputs={"status": "m:iets"})
    with pytest.raises(ModelError, match="onbekende ingang"):
        m.set_card(cid, inputs={"vermogen": "v:19@meter"})
    t = FakeTuya()
    live = cards.Live(m, tuya=t)
    run = live.value(f"kk.{cid}__run", "nl")
    assert run["kind"] == "button" and run["name"] == "Stekker" and run["value"] == "Stop", "aan staat op de startwaarde: Stop; zonder status de naam van de kaart"
    assert live.value(f"kk.{cid}__energy", "nl")["value"] == "2.50 kW" and live.value(f"kk.{cid}__time", "nl")["value"] == "—"
    assert live.press(f"kk.{cid}__run", "button", None) and t.sent == [("meter", "1", "OFF")]
    m.set_card(cid, target={"device": "meter", "dp": "1", "on": "ON", "off": ""})
    t.values["meter"]["1"] = True
    assert live.press(f"kk.{cid}__run", "button", None) is False and len(t.sent) == 1, "geen stopwaarde gekozen: dan niets"


def test_the_power_card_block_has_an_input_per_row_and_a_free_one():
    from kkaddon import schema
    m = Model()
    cid = m.add_card("power", "Stroom")
    m.set_card(cid, items=[{"tuya": "y:meter", "dp": "19", "name": "Huis"}, {"entity": "sensor.zon"}])
    b = schema._card_blocks(m, {"tuya": []}, set())[0]
    assert b["rows"] == 3, "twee rijen en een vrije ingang voor een nieuwe"
    assert b["labels"][1] == "sensor.zon" and len(b["labels"]) == 2
    m.set_card(cid, items=[{"entity": f"sensor.s{i}"} for i in range(6)])
    assert schema._card_blocks(m, {"tuya": []}, set())[0]["rows"] == 6, "bij het maximum geen vrije ingang meer"
