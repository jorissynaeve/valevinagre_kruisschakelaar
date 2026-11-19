"""De regels van een blok Scènes (kkaddon/scenerules.py en het deel van scenehub.py dat ze uitvoert): de berekening (puur), en met een nagebootst apparaat
(tests/fakehuisdev.py) het voorbeeld van de kidswekker: ingang 1 aan en de tijd tussen twee tijden is scène A, tussen de volgende twee scène B, en een wekker die
een scène een paar minuten voor de wekkertijd start, ook zonder verbinding (een geplande start op het apparaat)."""
import asyncio

import pytest

from kkaddon import scenerules as sr
from kkaddon.model import ModelError
from tests.test_scenehub import NIGHT, RAINBOW, WAKE, Clock, calls, cycle, env, fill, local, settle, shutdown, switch  # noqa: F401


def block(inputs, rules):
    return {"name": "T", "device": "", "inext": len(inputs) + 1, "inputs": [{"id": f"i{n}", "name": f"in{n}", "kind": k, "ref": f"x:{n}"} for n, k in enumerate(inputs, 1)],
            "rules": [{"scene": "s1" if i == 0 else f"s{i + 1}", "when": w} for i, w in enumerate(rules)]}


def run(b, vals, now, ahead=None):
    return sr.evaluate(b, lambda ref: vals.get(ref), ahead or (lambda t, m: None), now)


def hm(h, m=0):
    return h * 60 + m


# ------------------------------------------------------------ de berekening (puur)

def test_the_first_rule_that_matches_wins_and_none_means_none():
    b = block(["bool", "time", "time", "time"], [[{"op": "on", "in": "i1"}, {"op": "between", "from": "i2", "to": "i3"}], [{"op": "on", "in": "i1"}, {"op": "between", "from": "i3", "to": "i4"}]])
    v = {"x:1": True, "x:2": hm(19), "x:3": hm(21), "x:4": hm(7)}
    assert run(b, v, hm(20)) == (0, False)
    assert run(b, v, hm(21)) == (1, False), "het einde hoort er niet bij, het begin van de volgende wel"
    assert run(b, v, hm(23, 59)) == (1, False) and run(b, v, hm(6, 59)) == (1, False), "tot 07:00 loopt over middernacht"
    assert run(b, v, hm(7)) == (None, False) and run(b, v, hm(12)) == (None, False) and run(b, v, hm(18, 59)) == (None, False)
    assert run(b, {**v, "x:1": False}, hm(20)) == (None, False), "ingang 1 uit: geen enkele regel"
    both = block(["bool", "time", "time"], [[{"op": "between", "from": "i2", "to": "i3"}], [{"op": "on", "in": "i1"}]])
    assert run(both, {"x:1": True, "x:2": hm(8), "x:3": hm(9)}, hm(8, 30)) == (0, False), "de volgorde bepaalt: de eerste die klopt"
    assert run(both, {"x:1": True, "x:2": hm(8), "x:3": hm(9)}, hm(12)) == (1, False)


def test_on_off_after_before():
    b = block(["bool", "time"], [[{"op": "off", "in": "i1"}, {"op": "after", "in": "i2"}], [{"op": "before", "in": "i2"}]])
    assert run(b, {"x:1": False, "x:2": hm(7)}, hm(8)) == (0, False)
    assert run(b, {"x:1": True, "x:2": hm(7)}, hm(8)) == (None, False)
    assert run(b, {"x:1": True, "x:2": hm(7)}, hm(6)) == (1, False)
    assert run(b, {"x:1": False, "x:2": hm(7)}, hm(7)) == (0, False), "na: vanaf die minuut"


def test_a_wake_time_runs_from_before_to_after_and_a_wake_timer_starts_earlier():
    t = block(["time"], [[{"op": "wake", "in": "i1", "before": 20, "hold": 30}]])
    v = {"x:1": hm(7)}
    assert [run(t, v, m)[0] for m in (hm(6, 39), hm(6, 40), hm(7), hm(7, 29), hm(7, 30))] == [None, 0, 0, 0, None]
    assert run(block(["time"], [[{"op": "wake", "in": "i1", "before": 20, "hold": 30}]]), {"x:1": hm(0, 10)}, hm(23, 55)) == (0, False), "over middernacht"
    # een 0/1 van een timer: de timer telt `before` minuten eerder; het einde bepaalt de timer zelf
    b = block(["bool"], [[{"op": "wake", "in": "i1", "before": 15, "hold": 0}]])
    b["inputs"][0]["ref"] = "t:wekker"
    asked = []

    def ahead(tid, minutes):
        asked.append((tid, minutes))
        return hm(6, 45) <= 6 * 60 + 40 + minutes < hm(8)  # de timer staat van 07:00 tot 08:00 aan; "nu" is 06:40 + de minuten

    assert run(b, {}, hm(6, 40), ahead)[0] == 0 and asked == [("wekker", 15)]
    # zonder timer (een module) en zonder vroeger: gewoon de waarde van de ingang
    b["inputs"][0]["ref"] = "m:x"
    b["rules"][0]["when"][0]["before"] = 0
    assert run(b, {"m:x": True}, hm(12))[0] == 0 and run(b, {"m:x": False}, hm(12))[0] is None


def test_unknown_counts_as_not_true_but_a_missing_match_is_unsure():
    b = block(["bool", "bool"], [[{"op": "on", "in": "i1"}], [{"op": "on", "in": "i2"}]])
    assert run(b, {"x:2": True}, 0) == (1, False), "een onbekende eerste regel: de tweede mag nog kloppen"
    assert run(b, {"x:2": False}, 0) == (None, True), "geen regel klopt en een regel weten we niet: onzeker"
    assert run(b, {"x:1": False, "x:2": False}, 0) == (None, False)
    assert run(b, {}, 0) == (None, True)
    t = block(["time", "time"], [[{"op": "between", "from": "i1", "to": "i2"}]])
    assert run(t, {"x:1": hm(1)}, hm(2)) == (None, True), "een tijd die ontbreekt"
    assert run(block(["bool"], [[{"op": "on", "in": "i1"}]]), {"x:1": 1}, 0) == (None, True), "een getal is geen 0/1"


def test_condition_text_is_dutch_and_readable():
    names = {"i1": "Slaap", "i2": "Van", "i3": "Tot"}
    assert [sr.condition_text(c, names) for c in ({"op": "on", "in": "i1"}, {"op": "off", "in": "i1"}, {"op": "between", "from": "i2", "to": "i3"}, {"op": "after", "in": "i2"}, {"op": "before", "in": "i3"},
                                                   {"op": "wake", "in": "i2", "before": 10, "hold": 0}, {"op": "wake", "in": "i2", "before": 0, "hold": 45})] == [
        "Slaap = 1", "Slaap = 0", "tussen Van en Tot", "na Van", "voor Tot", "wekker Van, 10 min ervoor", "wekker Van, 45 min erna"]


def test_the_store_loads_what_is_usable_and_never_reuses_an_id(tmp_path):
    p = tmp_path / "scenerules.json"
    st = sr.RuleStore(p)
    st.items["q1"] = {"name": "Een", "device": "d", "inext": 3, "inputs": [{"id": "i1", "name": "A", "kind": "bool", "ref": "t:x"}, {"id": "i2", "name": "B", "kind": "time", "ref": None}],
                      "rules": [{"scene": "s1", "when": [{"op": "on", "in": "i1"}]}, {"scene": "s1", "when": [{"op": "on", "in": "i9"}]}, "stuk"]}
    st.next = 2
    st.save()
    st2 = sr.RuleStore(p)
    assert st2.next == 2 and st2.items["q1"]["rules"] == [{"scene": "s1", "when": [{"op": "on", "in": "i1"}]}], "een regel met een ingang die er niet is valt weg, de rest blijft"
    assert st2.items["q1"]["inext"] == 3 and st2.device_owner() == {"d": "q1"}
    p.write_text("{stuk", encoding="utf-8")
    assert sr.RuleStore(p).items == {} and sr.RuleStore(tmp_path / "nergens.json").items == {}
    p.write_text('{"next": 7, "blocks": {"q5": {"inputs": [{"id": "i1", "kind": "x"}]}, "zzz": {}, "q2": 4}}', encoding="utf-8")
    st3 = sr.RuleStore(p)
    assert sorted(st3.items) == ["q5"] and st3.items["q5"]["inputs"] == [] and st3.next == 7


# ------------------------------------------------------------ met een nagebouwd apparaat

def at(r, clock, epoch):
    """De klok van de engine (tijd van de dag voor de regels) en van de sync gelijk zetten; alleen vooruit."""
    clock.advance(epoch - clock.epoch)
    r.wall = float(epoch)


async def kids(tmp_path, monkeypatch):
    """Het voorbeeld van de kidswekker: ingang 1 aan en de tijd tussen 19:00 en 21:00 is scène A (statisch), tussen 21:00 en 07:00 scène B (regenboog)."""
    r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
    a, b = hub.scn.add({"name": "Avond", "def": NIGHT}), hub.scn.add({"name": "Nacht", "def": RAINBOW})
    setter, src = switch(r)
    t = {k: r.model.add_module("time", k, v) for k, v in (("Van", "19:00"), ("Midden", "21:00"), ("Tot", "07:00"))}
    bid = hub.scn.add_block({"name": "Kidswekker", "device": did, "inputs": [{"name": "Slapen", "kind": "bool", "ref": src}, {"name": "Van", "kind": "time", "ref": f"m:{t['Van']}"},
                                                                             {"name": "Midden", "kind": "time", "ref": f"m:{t['Midden']}"}, {"name": "Tot", "kind": "time", "ref": f"m:{t['Tot']}"}]})
    hub.scn.update_block(bid, {"rules": [{"scene": a, "when": [{"op": "on", "in": "i1"}, {"op": "between", "from": "i2", "to": "i3"}]},
                                         {"scene": b, "when": [{"op": "on", "in": "i1"}, {"op": "between", "from": "i3", "to": "i4"}]}]})
    return r, hub, esp, did, clock, bid, a, b, setter


def test_the_kidswekker_example_a_then_b_then_nothing(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, bid, a, b, setter = await kids(tmp_path, monkeypatch)
        try:
            assert hub.devices[did]["scn"]["sel"] == [a, b]
            await fill(hub, did, esp)
            at(r, clock, local(2027, 1, 5, 20, 0))
            setter(True)
            hub.tick()
            await asyncio.sleep(0.05)
            assert calls(esp, "play") == [] and hub.scn.rmem[bid] == {"scene": a}, "de eerste stand wordt alleen onthouden: een herstart start niets"
            assert hub.scn.rules_view()[0]["active"] == a and hub.scn.rules_view()[0]["active_rule"] == 0
            at(r, clock, local(2027, 1, 5, 21, 30))
            hub.tick()
            await settle(hub, did)
            assert calls(esp, "play") == [("play", {"slot": 2})] and calls(esp, "stop") == [], "A naar B: B start, A hoeft niet gestopt (B vervangt hem)"
            assert esp.run["h"] == hub.scn.lib.hashes[b] and hub.last[did][f"scene:{b}"] == "Nacht: gestart"
            for _ in range(3):
                hub.tick()
            await asyncio.sleep(0.05)
            assert len(calls(esp, "play")) == 1, "een ongewijzigde keuze stuurt niets opnieuw"
            at(r, clock, local(2027, 1, 6, 7, 0))
            hub.tick()
            await settle(hub, did)
            assert calls(esp, "stop") == [("stop", {"slot": 2})] and esp.run is None, "geen regel klopt meer: de scène met `stoppen` stopt"
            at(r, clock, local(2027, 1, 6, 20, 0))
            hub.tick()
            await settle(hub, did)
            assert calls(esp, "play")[-1] == ("play", {"slot": 1}) and esp.run["h"] == hub.scn.lib.hashes[a]
            setter(False)  # ingang 1 uit: A stopt
            hub.tick()
            await settle(hub, did)
            assert calls(esp, "stop")[-1] == ("stop", {"slot": 1}) and esp.run is None
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_finish_scenes_run_out_and_unknown_inputs_change_nothing(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, bid, a, b, setter = await kids(tmp_path, monkeypatch)
        try:
            hub.scn.update(b, {"release": "finish"})
            await fill(hub, did, esp)
            at(r, clock, local(2027, 1, 5, 20, 0))
            setter(True)
            hub.tick()
            at(r, clock, local(2027, 1, 5, 22, 0))
            hub.tick()
            await settle(hub, did)
            assert len(calls(esp, "play")) == 1
            at(r, clock, local(2027, 1, 6, 8, 0))
            hub.tick()
            await asyncio.sleep(0.05)
            assert calls(esp, "stop") == [] and esp.run is not None, "laten uitlopen: nooit stoppen"
            # een ingang die niets meer weet en geen regel die klopt: alles blijft zoals het was en de volgende stand telt weer als eerste
            at(r, clock, local(2027, 1, 6, 20, 0))
            hub.scn.update_block(bid, {"refs": {"i1": None}})
            hub.tick()
            assert bid not in hub.scn.rmem and hub.scn.rules_view()[0]["unsure"] is True
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_after_unknown_the_next_choice_is_only_remembered(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, bid, a, b, setter = await kids(tmp_path, monkeypatch)
        try:
            await fill(hub, did, esp)
            at(r, clock, local(2027, 1, 5, 20, 0))
            setter(False)
            hub.tick()
            assert hub.scn.rmem[bid] == {"scene": None}
            src = hub.scn.rules.items[bid]["inputs"][0]["ref"]
            hub.scn.update_block(bid, {"refs": {"i1": None}})
            hub.tick()
            assert bid not in hub.scn.rmem
            hub.scn.update_block(bid, {"refs": {"i1": src}})
            setter(True)
            hub.tick()
            await asyncio.sleep(0.05)
            assert hub.scn.rmem[bid] == {"scene": a} and calls(esp, "play") == [], "alleen onthouden"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_wake_rule_starts_minutes_before_the_wake_time_and_stops_after(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            w = hub.scn.add({"name": "Wekker", "def": RAINBOW})  # een scène die niet vanzelf eindigt
            t7 = r.model.add_module("time", "Wekker", "07:00")
            bid = hub.scn.add_block({"device": did, "inputs": [{"name": "Wekker", "kind": "time", "ref": f"m:{t7}"}]})
            hub.scn.update_block(bid, {"rules": [{"scene": w, "when": [{"op": "wake", "in": "i1", "before": 20, "hold": 30}]}]})
            await fill(hub, did, esp)
            at(r, clock, local(2027, 1, 5, 6, 0))
            hub.tick()
            at(r, clock, local(2027, 1, 5, 6, 39))
            hub.tick()
            await asyncio.sleep(0.05)
            assert calls(esp, "play") == []
            at(r, clock, local(2027, 1, 5, 6, 40))
            hub.tick()
            await settle(hub, did)
            assert calls(esp, "play") == [("play", {"slot": 1})], "20 minuten voor de wekker"
            at(r, clock, local(2027, 1, 5, 7, 30))
            hub.tick()
            await settle(hub, did)
            assert calls(esp, "stop") == [("stop", {"slot": 1})], "30 minuten erna"
            # een wekkertijd die uit een module komt is niet vooruit te berekenen: geen geplande start, wel een uitleg
            await cycle(hub, did, 3)
            assert calls(esp, "schedule") == [] and "geen timer" in hub.status()[0]["scn"]["sel"][0]["sched_note"]
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_wake_timer_starts_x_minutes_earlier_live_and_on_the_device(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            w = hub.scn.add({"name": "Wekker", "def": WAKE, "release": "finish"})
            tm = r.model.save_timer(None, "Schooldag", {"tue": [{"from": "07:00", "to": "08:00"}], "wed": [{"from": "07:00", "to": "08:00"}]})
            bid = hub.scn.add_block({"device": did, "inputs": [{"name": "Wekker", "kind": "bool", "ref": f"t:{tm}"}]})
            hub.scn.update_block(bid, {"rules": [{"scene": w, "when": [{"op": "wake", "in": "i1", "before": 15}]}]})
            await cycle(hub, did, 4)
            at(r, clock, local(2027, 1, 4, 12, 0))
            await cycle(hub, did, 2)
            sent = calls(esp, "schedule")[-1][1]["entries"]
            assert [e["at"] for e in sent[:3]] == [local(2027, 1, 5, 6, 45), local(2027, 1, 6, 6, 45), local(2027, 1, 12, 6, 45)], "de timer begint om 07:00: de geplande start 15 minuten eerder"
            assert hub.status()[0]["scn"]["sel"][0]["sched_note"] == ""
            # live: de wekker start om 06:45, niet om 07:00
            at(r, clock, local(2027, 1, 5, 6, 30))
            hub.tick()
            at(r, clock, local(2027, 1, 5, 6, 44))
            hub.tick()
            await asyncio.sleep(0.05)
            assert calls(esp, "play") == []
            at(r, clock, local(2027, 1, 5, 6, 45))
            hub.tick()
            await settle(hub, did)
            assert calls(esp, "play") == [("play", {"slot": 1})]
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_timer_rule_gets_a_schedule_but_a_rule_with_more_conditions_does_not(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        try:
            w = hub.scn.add({"name": "Wekker", "def": WAKE, "release": "finish"})
            tm = r.model.save_timer(None, "Schooldag", {d: [{"from": "07:00", "to": "08:00"}] for d in ("mon", "tue", "wed", "thu", "fri")})
            setter, src = switch(r)
            bid = hub.scn.add_block({"device": did, "inputs": [{"name": "Wekker", "kind": "bool", "ref": f"t:{tm}"}, {"name": "Aan", "kind": "bool", "ref": src}]})
            hub.scn.update_block(bid, {"rules": [{"scene": w, "when": [{"op": "on", "in": "i1"}]}]})
            await cycle(hub, did, 4)
            assert len(esp.sched) == 8 and esp.sched[0]["at"] == local(2027, 1, 5, 7, 0)
            hub.scn.update_block(bid, {"rules": [{"scene": w, "when": [{"op": "on", "in": "i1"}, {"op": "on", "in": "i2"}]}]})
            await cycle(hub, did, 2)
            assert esp.sched == [] and "meer dan een voorwaarde" in hub.status()[0]["scn"]["sel"][0]["sched_note"], "de gewone start blijft werken, de reserve op het apparaat niet"
            hub.scn.update_block(bid, {"rules": [{"scene": w, "when": [{"op": "on", "in": "i1"}, {"op": "on", "in": "i2"}]}, {"scene": w, "when": [{"op": "on", "in": "i1"}]}]})
            await cycle(hub, did, 2)
            assert len(esp.sched) == 8, "een scène met een enkele timer-regel krijgt wel een schema"
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_editing_the_rules_makes_the_next_choice_a_first_value_again(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, bid, a, b, setter = await kids(tmp_path, monkeypatch)
        try:
            at(r, clock, local(2027, 1, 5, 20, 0))
            setter(True)
            hub.tick()
            assert hub.scn.rmem[bid] == {"scene": a}
            hub.scn.update_block(bid, {"name": "Anders"})
            assert bid not in hub.scn.rmem
            hub.tick()
            await asyncio.sleep(0.05)
            assert calls(esp, "play") == [] and hub.scn.rmem[bid] == {"scene": a}
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_removing_the_device_keeps_the_block_without_a_device(tmp_path, monkeypatch):
    async def go():
        r, hub, esp, did, clock, bid, a, b, setter = await kids(tmp_path, monkeypatch)
        try:
            at(r, clock, local(2027, 1, 5, 20, 0))
            setter(True)
            hub.tick()
            assert bid in hub.scn.rmem
            await hub.remove(did)
            assert hub.scn.rules.items[bid]["device"] == "" and bid not in hub.scn.rmem and len(hub.scn.rules.items[bid]["rules"]) == 2
            hub.scn.tick_rules()
            v = hub.scn.rules_view()[0]
            assert v["device"] == "" and v["ready"] is False and v["active"] is None
            with pytest.raises(ModelError):
                hub.scn.update_block(bid, {"device": did})
        finally:
            await shutdown(hub, esp)
    asyncio.run(go())


def test_a_wake_rule_with_an_alarm_for_another_day_is_false_and_not_unknown():
    ins = {"i1": {"id": "i1", "kind": "time", "ref": "x:w"}}
    gone = lambda ref: sr.ABSENT  # noqa: E731
    for c in ({"op": "wake", "in": "i1", "before": 10, "hold": 60}, {"op": "after", "in": "i1"}, {"op": "before", "in": "i1"}):
        assert sr.eval_cond(c, ins, gone, None, 700) is False, c
        assert sr.eval_cond(c, ins, lambda ref: None, None, 700) is None, "nog niets ontvangen blijft onbekend"
    ins2 = {"i1": {"id": "i1", "kind": "time", "ref": "x:w"}, "i2": {"id": "i2", "kind": "time", "ref": "m:t"}}
    assert sr.eval_cond({"op": "between", "from": "i1", "to": "i2"}, ins2, lambda ref: sr.ABSENT if ref == "x:w" else 600, None, 700) is False
    block = {"inputs": [{"id": "i1", "kind": "time", "ref": "x:w"}, {"id": "i2", "kind": "bool", "ref": "m:b"}],
             "rules": [{"scene": "s1", "when": [{"op": "wake", "in": "i1", "before": 10, "hold": 60}]}, {"scene": "s2", "when": [{"op": "on", "in": "i2"}]}]}
    assert sr.evaluate(block, lambda ref: sr.ABSENT if ref == "x:w" else True, None, 700) == (1, False)
    assert sr.evaluate(block, lambda ref: sr.ABSENT if ref == "x:w" else False, None, 700) == (None, False), "zeker niets: het blok is niet onzeker"
    assert sr.evaluate(block, lambda ref: None if ref == "x:w" else False, None, 700) == (None, True), "onbekend blijft onzeker"


def test_night_still_starts_when_loxone_switches_off_and_on_again_while_the_alarm_is_tomorrow(tmp_path, monkeypatch):
    """De melding van de gebruiker: kinderen_slapen 0 en dan weer 1 startte de scène niet, omdat de wekker (`Morgen, 06:45`) onbekend telde en het blok onzeker werd."""
    async def go():
        from kkaddon.loxonehub import LoxoneHub
        r, hub, esp, did, clock = await env(tmp_path, monkeypatch)
        wake, night = hub.scn.add({"name": "Wekker", "def": NIGHT}), hub.scn.add({"name": "Nacht", "def": RAINBOW})
        lox = LoxoneHub(tmp_path, r.engine, hub, port=0)
        lox.miniserver = "127.0.0.1"
        lox.add_signal("slapen", {"dir": "in", "ref": "x:slapen", "kind": "bool"})
        lox.add_signal("wekker", {"dir": "in", "ref": "x:wekker", "kind": "time"})
        bid = hub.scn.add_block({"name": "Kidswekker", "device": did, "inputs": [{"name": "Slapen", "kind": "bool", "ref": "x:slapen"}, {"name": "Wekker", "kind": "time", "ref": "x:wekker"}]})
        hub.scn.update_block(bid, {"rules": [{"scene": wake, "when": [{"op": "wake", "in": "i2", "before": 10, "hold": 60}]}, {"scene": night, "when": [{"op": "on", "in": "i1"}]}]})
        try:
            await fill(hub, did, esp)
            at(r, clock, local(2027, 1, 5, 20, 0))
            await lox.apply([("wekker", "Morgen, 06:45"), ("slapen", 1.0)])
            hub.tick()
            await asyncio.sleep(0.05)
            assert calls(esp, "play") == [] and hub.scn.rmem[bid] == {"scene": night}, "de eerste stand wordt alleen onthouden"
            await lox.apply([("slapen", 0.0)])
            hub.tick()
            await settle(hub, did)
            assert hub.scn.rmem[bid] == {"scene": None}, "niets klopt, en dat is zeker: de stand wordt niet vergeten"
            assert hub.scn.rules_view()[0]["unsure"] is False
            await lox.apply([("slapen", 1.0)])
            hub.tick()
            await settle(hub, did)
            assert calls(esp, "play") == [("play", {"slot": 2})], "Nacht start"
            import json
            snap = json.loads(json.dumps(r.engine.snapshot()))  # de pagina haalt dit op: ook met een wekker voor een andere dag moet de state JSON zijn
            assert [i["value"] for i in snap["scenerules"][0]["inputs"]] == [True, None]
        finally:
            await lox.close()
            await shutdown(hub, esp)
    asyncio.run(go())

