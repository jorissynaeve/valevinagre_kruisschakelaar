"""Externe apparaten (Tuya via HA): model, mesh -> HA, HA -> mesh, bevestigingen en herstel."""
import pytest

from kkaddon import proto
from kkaddon.model import Model, ModelError
from kkaddon.proto import T, VIRTUAL_MIN
from rig import KEY, PEER, Rig

VID = VIRTUAL_MIN


def gangs(lamps=(True, True, False, False)):
    return [{"entity": f"switch.t_{i}", "name": f"Gang {i + 1}", "lamp": l} for i, l in enumerate(lamps)]


def tuya(r: Rig, lamps=(True, True, False, False), states="off"):
    vid = r.model.set_virtual(None, "Tuya", "Gang", gangs(lamps))
    r.engine.virt.ha_sync({f"switch.t_{i}": states for i in range(len(lamps))})
    r.clear()
    r.events.clear()
    r.ha_calls.clear()
    return vid


def frames_from(r, vid, ftype):
    return [f for f in r.sent if f.src == vid and f.type == ftype]


def state_frames(r, vid):
    return [(f, proto.parse(f)) for f in frames_from(r, vid, T.LIGHT_STATE)]


# ---------------------------------------------------------------- model


def test_virtual_device_creates_hidden_lights_for_lamp_gangs_only():
    m = Model()
    vid = m.set_virtual(None, "Tuya", "Gang", gangs())
    assert vid == VIRTUAL_MIN and m.nodes[vid]["name"] == "Tuya" and m.adopted(vid)
    assert sorted(m.lights) == [(vid, 0), (vid, 1)]  # gang 3 en 4 hebben geen lamp
    assert m.lights[(vid, 0)].name == "Gang 1" and m.lights[(vid, 0)].enabled is False
    assert m.set_virtual(None, "Tweede", "", [{"entity": "switch.x", "name": "", "lamp": True}]) == VIRTUAL_MIN + 1


def test_virtual_edit_remove_and_purge():
    m = Model()
    vid = m.set_virtual(None, "Tuya", "", gangs())
    m.set_light(5, 0, name="Plafond", triggers=[{"node": vid, "btn": 3}, {"node": vid, "btn": 1}, {"node": 1, "btn": 0}])
    m.set_light(vid, 0, triggers=[{"node": vid, "btn": 0}, {"node": vid, "btn": 1}, {"node": 2, "btn": 1}])
    assert [(t.node, t.btn) for t in m.lights[(vid, 0)].triggers] == [(2, 1), (vid, 1)]  # niet zichzelf bedienen
    m.lights[(vid, 0)].enabled = True
    m.set_virtual(vid, "Tuya", "", gangs()[:2])  # nog maar twee gangen
    assert [(t.node, t.btn) for t in m.lights[(5, 0)].triggers] == [(1, 0), (vid, 1)]  # knop 4 bestaat niet meer
    assert m.lights[(vid, 0)].enabled is True  # bewerken laat de keuze van de gebruiker staan
    m.set_follow(1, 0, (vid, 0))
    assert m.remove_virtual(vid) and not m.remove_virtual(vid)
    assert vid not in m.nodes and not [k for k in m.lights if k[0] == vid]
    assert [(t.node, t.btn) for t in m.lights[(5, 0)].triggers] == [(1, 0)]
    assert (1, 0) not in m.follow


def test_virtual_validation_and_roundtrip(tmp_path):
    m = Model(tmp_path / "n.json")
    for bad in ([], "x", [{"entity": "geen punt"}], [{"entity": "Switch.X"}], [{"entity": "switch.a"}, {"entity": "switch.a"}], gangs([True] * 9)):
        with pytest.raises(ModelError):
            m.set_virtual(None, "x", "", bad)
    m.set_virtual(None, "Tuya", "Gang", gangs())
    with pytest.raises(ModelError):
        m.set_virtual(None, "Andere", "", [{"entity": "switch.t_0", "name": "", "lamp": True}])  # entiteit al in gebruik
    with pytest.raises(ModelError):
        m.set_virtual(5, "x", "", gangs())  # id buiten het bereik
    assert Model(tmp_path / "n.json").to_dict() == m.to_dict()
    old = Model()
    old.load_dict({"version": 1, "nodes": {}, "lights": [], "follow": []})  # bestand van voor deze functie
    assert old.virtual == {}


# ---------------------------------------------------------------- mesh -> HA


def light_via_touch(r: Rig, vid, btn=2, mode="toggle"):
    r.model.set_light(vid, 0, triggers=[{"node": 1, "btn": btn, "mode": mode}])
    r.heartbeat(1, relays=[(False, 0)], nbtn=3, nind=3, cfg_hash=0)
    r.clear()


def test_press_on_touch_becomes_absolute_ha_call_and_confirms_with_cause():
    r = Rig()
    vid = tuya(r)
    light_via_touch(r, vid)
    r.button(1, 2, 7)
    assert r.ha_calls == [("switch.t_0", True)]
    assert state_frames(r, vid) == []  # eerst wachten tot HA bevestigt
    r.advance(0.15)
    r.engine.virt.ha_event("switch.t_0", "on")
    (f, ls), *_ = state_frames(r, vid)
    assert (ls.relay, ls.on, (ls.cause_src, ls.cause_btn, ls.cause_evt)) == (0, True, (1, 2, 7))
    assert r.engine.light_state(vid, 0) is True
    assert not frames_from(r, vid, T.BTN_EVENT)  # het gevolg van ons eigen bevel is geen druk
    assert r.events_of("cmd", node=vid, relay=0, result="ok")
    assert r.events_of("light", node=vid, relay=0, on=True)[-1]["cause"] == {"src": 1, "btn": 2, "evt": 7}


def test_toggle_counts_from_the_commanded_state_so_fast_presses_alternate():
    r = Rig()
    vid = tuya(r)
    light_via_touch(r, vid)
    r.button(1, 2, 1)
    r.advance(0.2)
    r.button(1, 2, 2)  # HA heeft nog niet geantwoord
    r.advance(0.2)
    r.button(1, 2, 3)
    assert [on for _, on in r.ha_calls] == [True, False, True]


def test_repeat_of_the_same_press_is_not_executed_twice_and_reconfirms_after_the_fact():
    r = Rig()
    vid = tuya(r)
    light_via_touch(r, vid)
    r.button(1, 2, 5)
    r.button(1, 2, 5)  # nieuw frame, zelfde evt (de touch hoorde niets)
    assert len(r.ha_calls) == 1
    assert state_frames(r, vid) == []  # nog onderweg: niets om te bevestigen
    r.engine.virt.ha_event("switch.t_0", "on")
    n = len(state_frames(r, vid))
    r.button(1, 2, 5)  # de touch hoorde de bevestiging niet
    assert len(r.ha_calls) == 1 and len(state_frames(r, vid)) == n + 1
    assert proto.parse(state_frames(r, vid)[-1][0]).cause_evt == 5


def test_on_and_off_modes_and_no_change_is_confirmed_immediately():
    r = Rig()
    vid = tuya(r, states="on")
    light_via_touch(r, vid, mode="on")
    r.button(1, 2, 1)  # al aan: geen wachten, wel meteen bevestigen voor de afzender
    ls = proto.parse(state_frames(r, vid)[-1][0])
    assert ls.on is True and ls.cause_evt == 1
    assert r.engine.virt.devs[vid][0].cmd is None
    light_via_touch(r, vid, mode="off")
    r.button(1, 2, 2)
    assert r.ha_calls[-1] == ("switch.t_0", False)


def test_unknown_state_cannot_be_toggled():
    r = Rig()
    vid = r.model.set_virtual(None, "Tuya", "", gangs())
    r.model.set_light(vid, 0, triggers=[{"node": 1, "btn": 2}])  # HA nog niet gesynchroniseerd
    r.button(1, 2, 1)
    assert r.ha_calls == [] and r.events_of("cmd", result="unknown")


def test_ha_command_failure_retries_then_reports_and_link_down_fails_fast():
    r = Rig()
    vid = tuya(r)
    r.engine.command_light(vid, 0, True)
    assert r.engine.snapshot()["lights"][0]["pending"] is True
    r.advance(3)
    assert r.ha_calls == [("switch.t_0", True)] * 2  # na 2,5 s nog een keer
    assert not r.events_of("cmd", result="failed")
    r.advance(6)
    assert r.events_of("cmd", node=vid, result="failed") and r.engine.snapshot()["lights"][0]["pending"] is False
    r.engine.virt.set_link(False)
    assert r.engine.command_light(vid, 1, True)
    assert len(r.events_of("cmd", node=vid, relay=1, result="failed")) == 1 and not r.engine.nodes[vid].online
    assert not r.engine.command_light(vid, 9, True) and not r.engine.command_light(vid + 5, 0, True)


def test_command_from_ui_uses_ha_cause_and_toggle_needs_known_state():
    r = Rig()
    vid = tuya(r)
    assert r.engine.toggle_light(vid, 0)
    assert r.ha_calls == [("switch.t_0", True)]
    r.engine.virt.ha_event("switch.t_0", "on")
    ls = proto.parse(state_frames(r, vid)[0][0])
    assert (ls.cause_src, ls.cause_btn) == (PEER, 0xFF)
    r.engine.virt.ha_event("switch.t_0", "unavailable")
    assert not r.engine.toggle_light(vid, 0)
    assert r.engine.light_state(vid, 0) is None


# ---------------------------------------------------------------- HA -> mesh


def test_physical_press_on_a_lamp_gang_is_a_button_event_plus_state():
    r = Rig()
    vid = tuya(r)
    r.engine.virt.ha_event("switch.t_1", "on")  # niemand gaf een bevel: iemand drukte op de touch
    (b,) = frames_from(r, vid, T.BTN_EVENT)
    assert proto.parse(b) == proto.BtnEvent(1, 0, 1)
    ls = proto.parse(state_frames(r, vid)[0][0])
    assert (ls.relay, ls.on, ls.cause_src, ls.cause_btn, ls.cause_evt) == (1, True, vid, 1, 1)
    assert r.events_of("btn", node=vid, btn=1)
    r.engine.virt.ha_event("switch.t_1", "off")
    assert proto.parse(frames_from(r, vid, T.BTN_EVENT)[-1]).evt == 2


def test_physical_press_on_a_gang_without_lamp_sends_only_the_button():
    r = Rig()
    vid = tuya(r)
    r.engine.virt.ha_event("switch.t_2", "on")
    assert len(frames_from(r, vid, T.BTN_EVENT)) == 1 and state_frames(r, vid) == []


def test_physical_press_triggers_other_external_lights_but_not_itself():
    r = Rig()
    vid = tuya(r)
    r.model.set_light(vid, 0, triggers=[{"node": vid, "btn": 2}])  # knop 3 (geen lamp) bedient lamp 1
    r.model.set_light(vid, 1, triggers=[{"node": vid, "btn": 1}])  # zichzelf: wordt weggelaten
    r.engine.virt.ha_event("switch.t_2", "on")
    assert r.ha_calls == [("switch.t_0", True)]
    r.engine.virt.ha_event("switch.t_1", "on")
    assert r.ha_calls == [("switch.t_0", True)]  # geen lus


def test_initial_sync_is_silent_and_heartbeats_report_everything():
    r = Rig()
    vid = r.model.set_virtual(None, "Tuya", "", gangs())
    r.engine.virt.ha_sync({"switch.t_0": "on", "switch.t_1": "off", "switch.t_2": "off", "switch.t_3": "off"})
    r.advance(25)
    assert not frames_from(r, vid, T.BTN_EVENT)
    hb = proto.parse(frames_from(r, vid, T.HEARTBEAT)[0])
    assert [on for on, _ in hb.relays] == [True, False, False, False] and hb.nbtn == 4
    assert r.engine.nodes[vid].online and r.engine.nodes[vid].virtual
    # na een wijziging volgen snel een paar heartbeats, en alle versies zijn de versies uit LIGHT_STATE
    n = len(frames_from(r, vid, T.HEARTBEAT))
    r.engine.virt.ha_event("switch.t_0", "off")
    r.advance(25)
    assert len(frames_from(r, vid, T.HEARTBEAT)) >= n + 2
    assert proto.parse(frames_from(r, vid, T.HEARTBEAT)[-1]).relays[0] == (False, r.engine.virt.devs[vid][0].ver)


def test_unavailable_gang_suppresses_heartbeat_and_comes_back_quietly():
    r = Rig()
    vid = tuya(r)
    r.engine.virt.ha_event("switch.t_3", "unavailable")
    r.clear()
    r.advance(130)
    assert not frames_from(r, vid, T.HEARTBEAT)  # een onbekende stand melden we niet als 'uit'
    r.engine.virt.ha_event("switch.t_3", "on")  # terug: dat is geen druk
    assert not frames_from(r, vid, T.BTN_EVENT)


def test_link_loss_marks_offline_and_resync_catches_up_without_presses():
    r = Rig()
    vid = tuya(r)
    r.engine.virt.set_link(False)
    assert not r.engine.nodes[vid].online and r.events_of("node", node=vid, what="offline")
    r.engine.virt.set_link(True)
    r.engine.virt.ha_sync({"switch.t_0": "on", "switch.t_1": "off", "switch.t_2": "off", "switch.t_3": "off"})
    assert r.engine.light_state(vid, 0) is True and not frames_from(r, vid, T.BTN_EVENT)
    assert r.engine.nodes[vid].online


# ---------------------------------------------------------------- leren, bevestigen, herstellen


def test_learn_sees_the_tuya_touch_and_nothing_fires():
    r = Rig()
    vid = tuya(r)
    r.model.set_light(vid, 1, triggers=[{"node": 1, "btn": 0}])
    r.heartbeat(1, relays=[(False, 0)], nbtn=3, nind=0, cfg_hash=0)
    r.engine.learn_start((vid, 0))
    r.button(1, 0, 1)  # een echte schakelaar tijdens het leren: niets schakelt
    r.engine.virt.ha_event("switch.t_2", "on")  # de touch van het apparaat schakelt fysiek altijd
    assert r.ha_calls == []
    seen = {(d["node"], d["btn"]) for d in r.engine.snapshot()["learn"]["detected"]}
    assert seen == {(1, 0), (vid, 2)}


def test_press_repeats_until_the_real_owner_confirms():
    r = Rig()
    vid = tuya(r)
    r.heartbeat(2, relays=[(False, 0)])
    r.model.set_light(2, 0, triggers=[{"node": vid, "btn": 1}])
    r.clear()
    r.engine.virt.ha_event("switch.t_1", "on")
    r.advance(1.0)
    evts = frames_from(r, vid, T.BTN_EVENT)
    assert len(evts) >= 3 and {proto.parse(f).evt for f in evts} == {1}
    assert len({f.ctr for f in evts}) >= 3  # elke herhaling is een nieuw frame (de twee blinde kopieën delen er een)
    # de eigenaar bevestigt: stoppen
    r.light_state(2, 0, True, 1, cause=(vid, 1, 1))
    r.clear()
    r.advance(3)
    assert not frames_from(r, vid, T.BTN_EVENT)


def test_no_repeat_when_nobody_listens_or_owner_is_offline_or_already_in_state():
    r = Rig()
    vid = tuya(r)
    r.engine.virt.ha_event("switch.t_1", "on")
    r.advance(3)
    assert len(frames_from(r, vid, T.BTN_EVENT)) == 2  # alleen de twee blinde kopieën
    r.heartbeat(2, relays=[(True, 1)])
    r.model.set_light(2, 0, triggers=[{"node": vid, "btn": 3, "mode": "on"}])
    r.clear()
    r.engine.virt.ha_event("switch.t_3", "on")
    r.advance(3)
    assert len(frames_from(r, vid, T.BTN_EVENT)) == 2  # eigenaar is al aan: er is niets om op te wachten


def test_frames_of_virtual_nodes_have_own_counters_and_survive_restart_with_higher_ver(tmp_path):
    r = Rig(tmp_path)
    vid = tuya(r)
    r.engine.virt.ha_event("switch.t_0", "on")
    r.engine.virt.ha_event("switch.t_0", "off")
    ctrs = [f.ctr for f in r.sent if f.src == vid]  # knop en lichtstand delen een teller per node
    assert ctrs[0] == 0 and sorted(set(ctrs)) == list(range(max(ctrs) + 1)) and r.engine._ctr == 0
    ver = r.engine.virt.devs[vid][0].ver
    boot = r.engine.boot
    r.engine.close()
    e = r.restart()
    assert e.boot == (boot + 1) & 0xFFFF  # nieuwe boot voor alle bronnen
    assert e.virt.devs[vid][0].ver == (ver + 17) & 0xFFFF  # touches negeren een lagere teller: ruim voorbij de oude
    assert not e.virt.devs[vid][0].avail and e.nodes[vid].online is False  # tot HA er is
    assert vid not in {int(k) for k in __import__("json").loads((tmp_path / "state.json").read_text())["nodes"]}


def test_own_virtual_frames_coming_back_over_another_bridge_are_ignored():
    r = Rig()
    vid = tuya(r)
    r.engine.virt.ha_event("switch.t_1", "on")
    r.events.clear()
    for f in list(r.sent):
        r.engine.on_datagram(proto.seal(KEY, f))
    assert not r.events and len(r.engine.nodes) == 1 and r.engine.stats["ignored"] == 0
    r.inject(vid + 3, T.HEARTBEAT, b"\x00\x00\x00\x00")  # een spook uit een vorig leven
    assert vid + 3 not in r.engine.nodes


def test_snapshot_and_maintenance_for_virtual_nodes():
    r = Rig()
    vid = tuya(r)
    r.advance(1000)  # veel langer dan offline_after: de HA-koppeling bepaalt de status, niet heartbeats
    s = r.engine.snapshot()
    n = next(n for n in s["nodes"] if n["id"] == vid)
    assert n["virtual"] and n["online"] and n["nbtn"] == 4 and n["cfg_status"] == "extern"
    assert [g["lamp"] for g in n["gangs"]] == [True, True, False, False]
    assert not [f for f in s["free_relays"] if f["node"] == vid]  # gangen zijn geen losse relais
    assert not r.sent_of(T.CFG_SET)  # een extern apparaat krijgt nooit configuratie
