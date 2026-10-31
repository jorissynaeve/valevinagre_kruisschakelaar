"""Entiteitskringen (AppDaemon-stijl): alle leden van een kring volgen elkaar."""
import pytest

from kkaddon.model import Model, ModelError
from rig import Rig

A, B, C = "switch.a", "light.b", "switch.c"


def make(tmp_path, enabled=True, ents=(A, B, C)):
    m = Model()
    m.set_groups([{"id": "keuken", "name": "Keuken", "entities": list(ents)}])
    if enabled:
        m.set_group("keuken", enabled=True)
    rig = Rig(tmp_path, model=m)
    return rig, rig.engine


def start(tmp_path, **kw):
    rig, e = make(tmp_path, **kw)
    e.ha_snapshot({A: "off", B: "off", C: "off"})
    return rig, e


def states(e):
    return {m["entity"]: m["on"] for m in e.snapshot()["groups"][0]["members"]}


def test_a_press_makes_all_others_follow_once(tmp_path):
    rig, e = start(tmp_path)
    e.ha_state(A, "on")
    assert sorted(rig.ha_calls) == [(B, True), (C, True)]
    # de echo's van onze eigen bevelen zetten niets meer in beweging
    e.ha_state(B, "on")
    e.ha_state(C, "on")
    assert len(rig.ha_calls) == 2
    ev = rig.events_of("sync")[-1]
    assert ev["what"] == "follow" and ev["group"] == "keuken" and ev["entity"] == A and ev["on"] is True and sorted(ev["to"]) == [B, C]
    # en nu drukt iemand op een ander lid
    e.ha_state(C, "off")
    assert set(rig.ha_calls[2:]) == {(A, False), (B, False)}
    e.ha_state(A, "off")
    e.ha_state(B, "off")
    assert len(rig.ha_calls) == 4


def test_a_disabled_group_watches_but_never_acts(tmp_path):
    rig, e = start(tmp_path, enabled=False)
    e.ha_state(A, "on")
    assert rig.ha_calls == []
    view = e.snapshot()["groups"][0]
    assert view["enabled"] is False and [(m["entity"], m["on"]) for m in view["members"]] == [(A, True), (B, False), (C, False)]
    assert e.ha_entities() >= {A, B, C}, "ook een kring die uit staat wordt gevolgd, zodat de UI de stand toont"


def test_switching_a_group_on_does_not_switch_anything_by_itself(tmp_path):
    rig, e = start(tmp_path, enabled=False)
    e.ha_state(A, "on")  # A staat aan, B en C uit: een kring die uit de pas loopt
    rig.model.set_group("keuken", enabled=True)
    assert rig.ha_calls == [], "aanzetten herstelt niets: alleen een volgende wijziging telt"
    e.ha_state(A, "off")
    assert rig.ha_calls == [], "B en C stonden al uit"


def test_two_quick_presses_do_not_flip_flop(tmp_path):
    rig, e = start(tmp_path)
    e.ha_state(A, "on")
    e.ha_state(A, "off")  # voor de echo's van B en C binnen zijn
    assert sorted(rig.ha_calls) == [(B, False), (B, True), (C, False), (C, True)]
    for ent, st in ((B, "on"), (C, "on"), (B, "off"), (C, "off")):  # de echo's, in volgorde
        e.ha_state(ent, st)
    assert len(rig.ha_calls) == 4, "geen extra bevelen door een echo die voor een druk werd aangezien"
    assert states(e) == {A: False, B: False, C: False}


def test_unavailable_members_are_skipped_and_those_already_there_get_no_command(tmp_path):
    rig, e = start(tmp_path)
    e.ha_state(B, "on")  # iemand zet B aan: A en C volgen
    assert sorted(rig.ha_calls) == [(A, True), (C, True)]
    e.ha_state(A, "on")
    e.ha_state(C, "on")
    rig.ha_calls.clear()
    e.ha_state(C, "unavailable")
    e.ha_state(A, "off")  # C is weg: alleen B volgt
    assert rig.ha_calls == [(B, False)]
    ev = rig.events_of("sync")[-1]
    assert ev["skipped"] == [C] and ev["to"] == [B]


def test_return_from_unavailable_and_reconnect_are_silent(tmp_path):
    rig, e = start(tmp_path)
    e.ha_state(A, "unavailable")
    e.ha_state(A, "on")  # terug, en aan: geen aanleiding om B en C aan te zetten
    assert rig.ha_calls == []
    e.ha_snapshot({A: "off", B: "on", C: "on"})  # opnieuw verbonden met een andere werkelijkheid
    assert rig.ha_calls == []
    e.ha_state(B, "off")  # nu wel een echte druk
    assert set(rig.ha_calls) == {(C, False)}, "A staat na de momentopname al uit"


def test_a_command_that_is_not_followed_is_reported(tmp_path):
    rig, e = start(tmp_path)
    e.ha_state(A, "on")
    e.ha_state(B, "on")  # B volgt, C reageert niet
    rig.advance(12)
    fails = [x for x in rig.events_of("sync") if x["what"] == "failed"]
    assert [x["entity"] for x in fails] == [C] and "reactie" in fails[0]["why"]


def test_without_a_ha_link_nothing_is_sent_and_it_is_reported(tmp_path):
    rig, e = start(tmp_path)
    e.virt.set_link(False)
    e.ha_state(A, "on")
    assert rig.ha_calls == []
    assert {x["entity"] for x in rig.events_of("sync") if x["what"] == "failed"} == {B, C}


def test_an_entity_cannot_belong_to_two_things_that_act():
    m = Model()
    m.set_groups([{"id": "g1", "name": "G1", "entities": [A, B]}, {"id": "g2", "name": "G2", "entities": [B, C]}])
    m.set_group("g1", enabled=True)
    with pytest.raises(ModelError, match="light.b wordt al bestuurd door kring G1"):
        m.set_group("g2", enabled=True)
    m.set_group("g1", enabled=False)
    m.set_group("g2", enabled=True)  # kan wel zodra de andere uit staat
    with pytest.raises(ModelError, match="switch.c wordt al bestuurd door extern apparaat"):
        m.set_virtual(None, "Tuya", "", [{"entity": C, "name": "x", "lamp": True}])
    m.set_group("g2", enabled=False)
    m.set_virtual(None, "Tuya", "", [{"entity": C, "name": "x", "lamp": True}])
    with pytest.raises(ModelError, match="switch.c wordt al bestuurd door extern apparaat"):
        m.set_group("g2", enabled=True)


def test_model_changes_keep_the_known_states(tmp_path):
    rig, e = start(tmp_path)
    e.ha_state(A, "on")
    rig.model.set_group("keuken", name="Keuken nieuw")
    assert states(e)[A] is True and e.snapshot()["groups"][0]["name"] == "Keuken nieuw"
    rig.model.remove_group("keuken")
    assert e.snapshot()["groups"] == [] and e.ha_entities() == set()


def test_member_changes_are_announced_so_the_page_refreshes(tmp_path):
    rig, e = start(tmp_path)
    rig.events.clear()
    e.ha_state(B, "on")
    assert [x for x in rig.events if x["t"] == "grp"], "een stand-wijziging van een lid wordt gemeld"
    rig.events.clear()
    e.ha_state(B, "on")
    assert not [x for x in rig.events if x["t"] == "grp"], "niets veranderd, niets gemeld"
    e.ha_state(C, "unavailable")
    assert [x for x in rig.events if x["t"] == "grp"]
    assert not [x for x in e.log if x["t"] == "grp"], "geen ruis in het logboek"


def test_controlling_a_single_member_works_with_the_group_off_and_is_a_press_when_it_is_on(tmp_path):
    rig, e = start(tmp_path, enabled=False)
    assert e.groups.command("keuken", entity=B) is True
    assert rig.ha_calls == [(B, True)], "alleen dat ene lid, ook als de kring uit staat"
    e.ha_state(B, "on")
    assert rig.ha_calls == [(B, True)], "de kring staat uit: niets volgt"
    rig.model.set_group("keuken", enabled=True)
    rig.ha_calls.clear()
    assert e.groups.command("keuken", entity=B) is True  # wisselt naar uit
    assert rig.ha_calls == [(B, False)]
    e.ha_state(B, "off")  # voor de kring is dit een druk: de rest volgt (A en C stonden al uit)
    assert rig.ha_calls == [(B, False)]
    rig.ha_calls.clear()
    assert e.groups.command("keuken", entity=C, on=True)
    assert rig.ha_calls == [(C, True)]
    e.ha_state(C, "on")
    assert set(rig.ha_calls) == {(C, True), (A, True), (B, True)}, "een lid bedienen in een actieve kring laat de andere volgen"


def test_controlling_the_whole_group_sets_every_reachable_member_and_those_are_not_presses(tmp_path):
    rig, e = start(tmp_path)
    e.ha_state(C, "unavailable")
    assert e.groups.command("keuken", on=True) is True
    assert set(rig.ha_calls) == {(A, True), (B, True)}, "een niet-beschikbaar lid wordt overgeslagen"
    e.ha_state(A, "on")
    e.ha_state(B, "on")
    assert len(rig.ha_calls) == 2, "de echo's van eigen bevelen zijn geen druk"
    rig.ha_calls.clear()
    assert e.groups.command("keuken") is True  # wisselen: er staat iets aan, dus uit
    assert set(rig.ha_calls) == {(A, False), (B, False)}
    e.ha_state(A, "off")
    e.ha_state(B, "off")
    rig.ha_calls.clear()
    assert e.groups.command("keuken", on=False) is True, "alles staat al zo: geen bevelen, wel gelukt"
    assert rig.ha_calls == []
    assert [x for x in rig.events_of("sync") if x["what"] == "command"]


def test_group_commands_report_failure_for_unknown_or_unreachable(tmp_path):
    rig, e = start(tmp_path)
    assert e.groups.command("bestaat-niet") is False
    assert e.groups.command("keuken", entity="switch.onbekend") is False
    e.ha_state(A, "unavailable")
    assert e.groups.command("keuken", entity=A) is False, "een lid met onbekende stand kun je niet wisselen"
    e.virt.set_link(False)
    assert e.groups.command("keuken", on=True) is False and rig.ha_calls == []


def test_lamp_relay_decides_the_group_state_and_gets_the_command_first(tmp_path):
    rig, e = start(tmp_path, enabled=False)
    rig.model.set_group("keuken", lamp=[B])
    v = e.snapshot()["groups"][0]
    assert v["lamp"] == [B] and [m["lamp"] for m in v["members"]] == [False, True, False]
    # A (een touch-relais) staat aan, maar de lamp (B) staat uit: de kring is "uit", wisselen betekent dus aan
    e.ha_state(A, "on")
    rig.ha_calls.clear()
    assert e.groups.command("keuken") is True
    assert rig.ha_calls[0] == (B, True), "het lamp-relais krijgt het bevel eerst"
    assert set(rig.ha_calls) == {(B, True), (C, True)}, "A staat al aan"
    # zonder lamp-relais telt iedereen: A staat aan, dus wisselen betekent uit
    rig2, e2 = start(tmp_path)
    e2.ha_state(A, "on")
    e2.ha_state(B, "on")
    e2.ha_state(C, "on")
    rig2.ha_calls.clear()
    e2.groups.command("keuken")
    assert {x[1] for x in rig2.ha_calls} == {False}


def test_an_unreachable_lamp_relay_falls_back_to_all_members(tmp_path):
    rig, e = start(tmp_path, enabled=False)
    rig.model.set_group("keuken", lamp=[B])
    e.ha_state(A, "on")
    e.ha_state(B, "unavailable")
    rig.ha_calls.clear()
    assert e.groups.command("keuken") is True
    assert {x[1] for x in rig.ha_calls} == {False}, "de lamp is onbereikbaar: A bepaalt, en staat aan"


def test_lamp_relays_survive_reimport_and_follow_the_entity_list():
    m = Model()
    m.set_groups([{"id": "g", "name": "G", "entities": [A, B, C]}])
    m.set_group("g", lamp=[B, A])
    assert m.groups["g"]["lamp"] == [A, B], "in de volgorde van de kring"
    m.set_groups([{"id": "g", "name": "G2", "entities": [A, C, "switch.d"]}])
    assert m.groups["g"]["lamp"] == [A], "een lamp-relais dat uit de kring verdween valt weg"
    m.set_group("g", entities=[C, "switch.d"])
    assert m.groups["g"]["lamp"] == []
    for bad in ([B], ["Nope"], "x", [5]):
        with pytest.raises(ModelError):
            m.set_group("g", lamp=bad)
    m.set_group("g", lamp=[])


# ---------------------------------------------------------------- disable-ingang met een timer

NIGHT = {d: [{"from": "03:00", "to": "07:00"}] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def at(rig, hour, minute=0):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    rig.wall = datetime(2026, 11, 2, hour, minute, tzinfo=ZoneInfo("Europe/Brussels")).timestamp()  # een maandag


def night_lamp(tmp_path):
    rig, e = start(tmp_path)
    e.set_tz("Europe/Brussels")
    tid = e.model.save_timer(None, "Nacht", NIGHT)
    e.model.set_group("keuken", lamp=[C], disable=tid)
    rig.ha_calls.clear()
    return rig, e, tid


def test_a_touch_does_nothing_to_the_lamp_while_disabled_and_goes_back_itself(tmp_path):
    rig, e, _ = night_lamp(tmp_path)
    at(rig, 4)
    assert e.snapshot()["groups"][0]["blocked"] is True
    e.ha_state(A, "on")  # de touch zette zijn eigen relais om
    assert rig.ha_calls == [(A, False)], "alleen de touch terug, de lamp en de andere touch niet"
    assert rig.events_of("sync", what="blocked")[-1]["entity"] == A
    e.ha_state(A, "off")  # de echo van ons bevel is geen nieuwe druk
    assert len(rig.ha_calls) == 1


def test_a_touch_works_again_outside_the_interval_and_the_edges_are_exact(tmp_path):
    rig, e, _ = night_lamp(tmp_path)
    at(rig, 7)
    assert e.snapshot()["groups"][0]["blocked"] is False, "het einde hoort er niet meer bij"
    e.ha_state(A, "on")
    assert sorted(rig.ha_calls) == [(B, True), (C, True)]
    at(rig, 2, 59)
    assert e.snapshot()["groups"][0]["blocked"] is False
    at(rig, 3)
    assert e.snapshot()["groups"][0]["blocked"] is True, "het begin hoort erbij"
    at(rig, 6, 59)
    assert e.snapshot()["groups"][0]["blocked"] is True


def test_the_lamp_stays_on_when_disabled_and_a_touch_returns_to_its_state(tmp_path):
    rig, e, _ = night_lamp(tmp_path)
    e.ha_state(C, "on")  # bv. via HA: het lamp-relais volgt nog gewoon
    e.ha_state(A, "on"); e.ha_state(B, "on")
    rig.ha_calls.clear()
    at(rig, 4)
    e.ha_state(A, "off")
    assert rig.ha_calls == [(A, True)], "de lamp blijft aan; de touch gaat terug naar aan"


def test_commands_from_the_ui_and_the_lamp_relay_itself_still_work_while_disabled(tmp_path):
    rig, e, _ = night_lamp(tmp_path)
    at(rig, 4)
    assert e.groups.command("keuken", True) is True
    assert (C, True) in rig.ha_calls, "bedienen in de add-on is geen touch"
    for x in (A, B, C):
        e.ha_state(x, "on")
    assert not rig.events_of("sync", what="blocked")
    rig.ha_calls.clear()
    e.ha_state(C, "off")  # iemand schakelt het lamp-relais via HA: de touches volgen, voor hun LED
    assert set(rig.ha_calls) == {(A, False), (B, False)} and len(rig.ha_calls) == 2


def test_a_group_that_is_off_never_acts_even_when_disabled(tmp_path):
    rig, e, _ = night_lamp(tmp_path)
    e.model.set_group("keuken", enabled=False)
    at(rig, 4)
    e.ha_state(A, "on")
    assert rig.ha_calls == []


def test_timezone_decides_the_interval(tmp_path):
    rig, e, _ = night_lamp(tmp_path)
    at(rig, 4)  # 4u in Brussel is 3u in Lissabon, nog binnen; 3u30 in Brussel is 2u30 in Lissabon, erbuiten
    e.set_tz("Europe/Lisbon")
    assert e.snapshot()["groups"][0]["blocked"] is True and e.snapshot()["clock"]["time"] == "03:00"
    at(rig, 3, 30)
    assert e.snapshot()["groups"][0]["blocked"] is False
    e.set_tz("Bestaat/Niet")
    assert e.snapshot()["clock"]["tz"] == "tijd van de add-on"


def test_deleting_a_timer_frees_the_lamp(tmp_path):
    rig, e, tid = night_lamp(tmp_path)
    at(rig, 4)
    assert e.model.remove_timer(tid) is True
    assert "disable" not in e.model.groups["keuken"] and e.snapshot()["groups"][0]["blocked"] is False
