import pytest

from kkaddon import proto
from kkaddon.engine import PEER_ID_MIN
from kkaddon.model import Model, ModelError
from kkaddon.proto import T
from rig import PEER, Rig


# ---------------------------------------------------------------- staat bijhouden


def test_heartbeat_creates_node_and_light_state(tmp_path):
    r = Rig(tmp_path)
    r.heartbeat(2, relays=[(False, 0), (True, 3)], nbtn=0, nind=0, cfg_hash=0)
    n = r.engine.nodes[2]
    assert n.online and n.fw == 0x0102 and [x.on for x in n.relays] == [False, True]
    assert r.events_of("node", what="new", node=2) and r.events_of("node", what="online", node=2)
    assert r.events_of("light", node=2, relay=1, on=True)  # eerste kennis is een wijziging


def test_light_state_versions_and_reboot():
    r = Rig()
    r.heartbeat(2, relays=[(False, 5)])
    r.light_state(2, 0, True, 6, cause=(1, 1, 9))
    assert r.engine.light_state(2, 0) is True
    ev = r.events_of("light", node=2, relay=0, on=True)[-1]
    assert ev["cause"] == {"src": 1, "btn": 1, "evt": 9}
    r.light_state(2, 0, False, 5)  # ouder bericht (uit volgorde): negeren
    assert r.engine.light_state(2, 0) is True
    r.light_state(2, 0, False, 7)
    assert r.engine.light_state(2, 0) is False
    # de node is geflasht: boot verandert en de versieteller begint opnieuw. Zijn staat is dan de waarheid.
    r.boot(2, 9)
    r.heartbeat(2, relays=[(True, 1)])
    assert r.engine.light_state(2, 0) is True


def test_heartbeat_catches_up_missed_light_state():
    r = Rig()
    r.heartbeat(2, relays=[(False, 1)])
    r.heartbeat(2, relays=[(True, 2)])  # LIGHT_STATE gemist, heartbeat herstelt het
    assert r.engine.light_state(2, 0) is True
    assert r.events_of("light", node=2, relay=0, on=True, via="heartbeat")


def test_button_events_are_deduplicated_by_evt():
    r = Rig()
    r.heartbeat(1, relays=[], nbtn=3, nind=0, cfg_hash=0)
    r.button(1, 0, 1)
    r.button(1, 0, 1)  # herhaling door de afzender: nieuw frame-id, zelfde evt
    r.button(1, 0, 2)
    r.button(1, 1, 2)
    assert [(e["btn"], e["evt"]) for e in r.events_of("btn")] == [(0, 1), (0, 2), (1, 2)]
    r.boot(1, 7)  # herstart: evt begint opnieuw en moet tellen
    r.button(1, 0, 1)
    assert len(r.events_of("btn")) == 4


def test_foreign_and_own_and_replayed_frames_are_ignored():
    r = Rig()
    f = r.heartbeat(2, relays=[(False, 0)])
    raw = proto.seal(__import__("rig").KEY, f)
    r.engine.on_datagram(raw)  # replay
    assert r.engine.stats["dups"] == 1
    wrong = bytearray(__import__("rig").KEY)
    wrong[0] ^= 1
    r.engine.on_datagram(proto.seal(bytes(wrong), f))
    assert r.engine.stats["bad_auth"] == 1
    r.engine.on_datagram(b"\x01\x02")
    assert r.engine.stats["bad_auth"] == 2
    # link-lokale types horen niet over de LAN te komen
    r.inject(2, T.BEACON, bytes([6, 1, 0, 1]))
    assert 2 in r.engine.nodes and len(r.engine.nodes) == 1
    # frames van infrastructuur-ids maken geen node aan
    r.inject(PEER_ID_MIN + 5, T.HEARTBEAT, b"\x00\x00\x00\x00")
    assert len(r.engine.nodes) == 1 and r.engine.stats["ignored"] == 1


def test_offline_detection_and_restart_keeps_state(tmp_path):
    r = Rig(tmp_path)
    r.heartbeat(2, relays=[(True, 4)], nbtn=1, nind=0, cfg_hash=0)
    boot1 = r.engine.boot
    r.advance(30)
    assert r.engine.nodes[2].online
    r.advance(260)
    assert not r.engine.nodes[2].online and r.events_of("node", what="offline", node=2)
    r.heartbeat(2, relays=[(True, 4)], nbtn=1, nind=0, cfg_hash=0)
    assert r.engine.nodes[2].online
    r.engine.close()
    e = r.restart()
    assert e.boot == (boot1 + 1) & 0xFFFF  # elke start een nieuwe boot: onze frame-id's blijven uniek
    assert e.nodes[2].relays[0].on is True and e.nodes[2].nbtn == 1 and e.nodes[2].online


def test_boot_without_saved_state_is_time_based_and_rolls_over_ctr(tmp_path):
    r = Rig(tmp_path)
    assert r.engine.boot == (int(1_700_000_000.0 // 60) + 1) & 0xFFFF
    r.heartbeat(2, relays=[(False, 0)])
    b0 = r.engine.boot
    r.engine._ctr = 0xFFFE
    r.engine.send(T.IDENTIFY, proto.identify(2), copies=1)
    r.engine.send(T.IDENTIFY, proto.identify(2), copies=1)
    assert r.sent[-1].boot == (b0 + 1) & 0xFFFF and r.sent[-1].ctr == 0
    assert r.sent[-2].boot == b0 and r.sent[-2].ctr == 0xFFFE


def test_peer_id_range():
    with pytest.raises(ValueError):
        Rig().engine.__class__(b"\0" * 32, Model(), lambda b: None, peer_id=5)
    with pytest.raises(ValueError):
        Rig().engine.__class__(b"\0" * 5, Model(), lambda b: None)


# ---------------------------------------------------------------- lichten schakelen


def test_command_light_sends_two_copies_and_confirms():
    r = Rig()
    r.heartbeat(2, relays=[(False, 0), (False, 0)])
    assert r.engine.command_light(2, 1, True)
    r.advance(0.05)
    cmds = r.sent_of(T.CMD_LIGHT)
    assert len(cmds) == 2 and cmds[0].payload == cmds[1].payload == proto.cmd_light(2, 1, proto.Mode.ON)
    assert cmds[0].ctr == cmds[1].ctr  # blinde kopie: zelfde frame
    r.light_state(2, 1, True, 1, cause=(PEER, 0xFF, 0))
    r.advance(5)
    assert len(r.sent_of(T.CMD_LIGHT)) == 2  # bevestigd: geen herhaling
    assert r.events_of("cmd", node=2, relay=1, result="ok")
    assert (2, 1) not in r.engine.pending_cmds


def test_command_light_retries_as_new_frames_then_gives_up():
    r = Rig()
    r.heartbeat(2, relays=[(False, 0)])
    r.engine.command_light(2, 0, True)
    r.advance(6)
    cmds = r.sent_of(T.CMD_LIGHT)
    assert len(cmds) == 8  # 4 pogingen x 2 kopieën
    assert len({c.ctr for c in cmds}) == 4  # elke herhaling is een nieuw frame (forwarders slaan het anders over)
    assert r.events_of("cmd", result="failed")
    assert not r.engine.pending_cmds


def test_command_replaced_by_newer_and_unknown_targets():
    r = Rig()
    r.heartbeat(2, relays=[(False, 0)])
    r.engine.command_light(2, 0, True)
    r.advance(0.1)
    r.engine.command_light(2, 0, False)  # de gebruiker bedenkt zich
    r.advance(0.1)
    r.light_state(2, 0, False, 1)
    r.advance(6)
    assert [c.payload[-1] for c in r.sent_of(T.CMD_LIGHT)] == [1, 1, 2, 2]  # geen herhaling van de vervangen opdracht
    assert not r.engine.command_light(99, 0, True)
    assert not r.engine.command_light(2, 5, True)
    assert not r.engine.toggle_light(99, 0)


def test_toggle_uses_known_state_as_absolute_command():
    r = Rig()
    r.heartbeat(2, relays=[(True, 3)])
    r.engine.toggle_light(2, 0)
    assert r.sent_of(T.CMD_LIGHT)[0].payload == proto.cmd_light(2, 0, proto.Mode.OFF)


# ---------------------------------------------------------------- leren en configureren


def known_house(r: Rig):
    """Touch 1 (3 knoppen, 3 indicatoren, 1 relais) en relaisbox 2 (4 relais), hashes van een lege config."""
    e = proto.config_hash([[]] * 1, [None] * 3)
    r.heartbeat(1, relays=[(False, 0)], nbtn=3, nind=3, cfg_hash=e)
    r.heartbeat(2, relays=[(False, 0)] * 4, nbtn=0, nind=0, cfg_hash=proto.config_hash([[]] * 4, []))


def ack_all(r: Rig, node: int, frames):
    for f in frames:
        kind, idx = f.payload[2], f.payload[3]
        r.cfg_ack(node, kind, idx)


def test_learn_session_sends_learn_refreshes_and_stops():
    r = Rig()
    known_house(r)
    r.engine.learn_start((2, 1))
    r.advance(0.05)
    assert [bool(f.payload[0]) for f in r.sent_of(T.LEARN)] == [True, True]
    r.clear()
    r.advance(26)
    assert len(r.sent_of(T.LEARN)) == 2 and r.sent_of(T.LEARN)[0].payload == b"\x01"  # vernieuwd
    r.button(1, 2, 1)
    r.button(1, 2, 2)
    r.button(1, 0, 3)
    snap = r.engine.snapshot()["learn"]
    assert snap["target"] == [2, 1]
    assert {(d["node"], d["btn"]): d["count"] for d in snap["detected"]} == {(1, 2): 2, (1, 0): 1}
    r.clear()
    r.engine.learn_stop()
    r.advance(2)
    offs = r.sent_of(T.LEARN)
    assert len(offs) == 6 and all(f.payload == b"\x00" for f in offs)  # 3x (elk met kopie)
    assert r.engine.learn is None and r.events_of("learn", what="stopped")


def test_learn_times_out_without_activity():
    r = Rig()
    known_house(r)
    r.engine.learn_start((2, 0))
    r.advance(310)
    assert r.engine.learn is None and r.events_of("learn", what="stopped", why="timeout")


def test_learn_save_pushes_triggers_and_follow_and_reaches_ok(tmp_path):
    r = Rig(tmp_path)
    known_house(r)
    r.engine.learn_start((2, 1))
    r.button(1, 2, 1)
    r.button(1, 0, 1)
    r.clear()
    r.engine.learn_save([{"node": 1, "btn": 2, "mode": "toggle"}, {"node": 1, "btn": 0, "mode": "on"}])
    r.advance(0.5)
    assert r.model.lights[(2, 1)].triggers[0].btn == 0 and r.model.adopted(1) and r.model.adopted(2)
    cfg = r.sent_of(T.CFG_SET)
    dst = lambda f: int.from_bytes(f.payload[:2], "little")
    to2 = [f for f in cfg if dst(f) == 2]
    to1 = [f for f in cfg if dst(f) == 1]
    # box 2: alleen relais 1 is veranderd (de hash bevestigde de rest), met de nieuwe triggers
    assert {(f.payload[2], f.payload[3]) for f in to2} == {(0, 1)}
    assert to2[0].payload == proto.cfg_triggers(2, 1, [(1, 0, 1), (1, 2, 0)])
    # touch 1: indicator 0 en 2 volgen automatisch het licht (2,1); indicator 1 blijft zoals het was
    fol = {f.payload[3]: f.payload for f in to1 if f.payload[2] == 1}
    assert set(fol) == {0, 2} and not [f for f in to1 if f.payload[2] == 0]
    assert fol[0] == proto.cfg_follow(1, 0, 2, 1) and fol[2] == proto.cfg_follow(1, 2, 2, 1)
    assert r.engine.nodes[2].cfg_status == "pushing"
    # acks komen binnen
    ack_all(r, 2, [f for f in to2])
    ack_all(r, 1, [f for f in to1])
    assert r.engine.nodes[1].cfg_status == "applied" and r.engine.nodes[2].cfg_status == "applied"
    # de volgende heartbeat bevestigt via de hash
    for nid, nrel, nind in ((1, 1, 3), (2, 4, 0)):
        want = r.engine.expected_hash(r.engine.nodes[nid])
        r.heartbeat(nid, relays=[(False, 0)] * nrel, nbtn=3 if nid == 1 else 0, nind=nind, cfg_hash=want)
    assert r.engine.nodes[1].cfg_status == "ok" and r.engine.nodes[2].cfg_status == "ok"
    assert r.events_of("learn", what="saved")


def test_push_retries_unacked_items_then_fails():
    r = Rig()
    known_house(r)
    r.model.adopt(2)
    assert r.engine.sync_node(2, full=True)  # alle vier de relais
    r.advance(0.3)
    first = [f for f in r.sent_of(T.CFG_SET) if int.from_bytes(f.payload[:2], "little") == 2]
    assert len(first) == 2 * 4  # 4 relais x 2 kopieën
    r.cfg_ack(2, 0, 0)
    r.cfg_ack(2, 0, 1)
    r.clear()
    r.advance(1.8)  # eerste wachttijd (1,5 s) is om: alleen de onbevestigde herhalen
    again = r.sent_of(T.CFG_SET)
    assert sorted({f.payload[3] for f in again if int.from_bytes(f.payload[:2], "little") == 2}) == [2, 3]
    r.advance(30)
    assert r.engine.nodes[2].cfg_status == "failed" and r.events_of("cfg", node=2, status="failed")
    assert r.engine.nodes[2].push is None


def test_edit_during_push_is_not_lost():
    r = Rig()
    known_house(r)
    r.engine.set_triggers(2, 0, [{"node": 1, "btn": 1}])
    r.advance(0.3)
    r.engine.set_triggers(2, 0, [{"node": 1, "btn": 1}, {"node": 1, "btn": 2}])  # tijdens de lopende push
    r.clear()
    for i in range(4):
        r.cfg_ack(2, 0, i)
    ack_all(r, 1, [])
    r.advance(0.5)
    again = [f for f in r.sent_of(T.CFG_SET) if int.from_bytes(f.payload[:2], "little") == 2]
    assert again and again[0].payload == proto.cfg_triggers(2, 0, [(1, 1, 0), (1, 2, 0)])


def test_hash_drift_repairs_adopted_nodes_with_backoff_and_leaves_others():
    r = Rig()
    known_house(r)
    # niet geadopteerd: alleen melden
    r.heartbeat(2, relays=[(False, 0)] * 4, nbtn=0, nind=0, cfg_hash=0x1234)
    assert r.engine.nodes[2].cfg_status == "unmanaged" and not r.sent_of(T.CFG_SET)
    # geadopteerd met een lichtconfig: de node heeft hem niet (vers geflasht) -> volledig opnieuw sturen
    r.model.set_light(2, 3, name="Spots", triggers=[{"node": 1, "btn": 1}])
    r.engine.nodes[2].push = None
    r.clear()
    r.heartbeat(2, relays=[(False, 0)] * 4, nbtn=0, nind=0, cfg_hash=0x1234)
    r.advance(0.5)
    assert r.engine.nodes[2].cfg_status == "pushing" and r.events_of("cfg", node=2, status="drift")
    sent = [f for f in r.sent_of(T.CFG_SET) if int.from_bytes(f.payload[:2], "little") == 2]
    assert sorted({f.payload[3] for f in sent}) == [0, 1, 2, 3]  # alle relais, niet alleen het gewijzigde
    # acks uitblijven -> mislukt; een volgende heartbeat binnen de wachttijd start niet meteen opnieuw
    r.advance(40)
    assert r.engine.nodes[2].cfg_status == "failed"
    r.clear()
    r.heartbeat(2, relays=[(False, 0)] * 4, nbtn=0, nind=0, cfg_hash=0x1234)
    r.advance(0.5)
    assert not r.sent_of(T.CFG_SET)
    r.advance(120)  # de wachttijd is verstreken: nieuwe poging
    r.heartbeat(2, relays=[(False, 0)] * 4, nbtn=0, nind=0, cfg_hash=0x1234)
    r.advance(0.5)
    assert r.sent_of(T.CFG_SET)
    # de node heeft het uiteindelijk wel: ok en de teller begint opnieuw
    want = r.engine.expected_hash(r.engine.nodes[2])
    r.engine.nodes[2].push = None
    r.heartbeat(2, relays=[(False, 0)] * 4, nbtn=0, nind=0, cfg_hash=want)
    assert r.engine.nodes[2].cfg_status == "ok" and r.engine.nodes[2].backoff == 0


def test_old_heartbeat_right_after_push_does_not_retrigger():
    r = Rig()
    known_house(r)
    r.engine.set_triggers(2, 0, [{"node": 1, "btn": 1}])
    r.advance(0.3)
    for i in range(4):
        r.cfg_ack(2, 0, i)
    assert r.engine.nodes[2].cfg_status == "applied"
    r.clear()
    r.heartbeat(2, relays=[(False, 0)] * 4, nbtn=0, nind=0, cfg_hash=proto.config_hash([[]] * 4, []))  # nog de oude hash
    r.advance(1)
    assert not r.sent_of(T.CFG_SET) and r.engine.nodes[2].cfg_status == "applied"


def test_drift_after_a_verified_push_is_repaired_without_waiting():
    r = Rig()
    known_house(r)
    r.engine.set_triggers(2, 0, [{"node": 1, "btn": 1}])
    r.advance(0.3)
    for i in range(4):
        r.cfg_ack(2, 0, i)
    want = r.engine.expected_hash(r.engine.nodes[2])
    r.heartbeat(2, relays=[(False, 0)] * 4, nbtn=0, nind=0, cfg_hash=want)
    assert r.engine.nodes[2].cfg_status == "ok"
    r.advance(5)  # ver binnen de wachttijd van 90 s die na een push geldt
    r.clear()
    r.heartbeat(2, relays=[(False, 0)] * 4, nbtn=0, nind=0, cfg_hash=proto.config_hash([[]] * 4, []))  # node is gewist
    r.advance(0.5)
    assert r.engine.nodes[2].cfg_status == "pushing" and r.sent_of(T.CFG_SET)


def test_old_firmware_without_hash_is_left_alone():
    r = Rig()
    r.model.adopt(2)
    r.heartbeat(2, relays=[(False, 0)])  # geen hash/nbtn/nind in de payload
    assert r.engine.nodes[2].cfg_status == "unknown" and not r.sent_of(T.CFG_SET)
    r.engine.set_triggers(2, 0, [{"node": 1, "btn": 0}])  # kan niets sturen: nind onbekend
    r.advance(1)
    assert not r.sent_of(T.CFG_SET)


def test_learn_save_without_session_is_an_error():
    r = Rig()
    with pytest.raises(ModelError):
        r.engine.learn_save([])


def test_snapshot_lists_free_relays_and_lights():
    r = Rig()
    known_house(r)
    r.model.set_light(2, 0, name="Plafond")
    s = r.engine.snapshot()
    assert [(l["node"], l["relay"], l["on"]) for l in s["lights"]] == [(2, 0, False)]
    assert {(f["node"], f["relay"]) for f in s["free_relays"]} == {(1, 0), (2, 1), (2, 2), (2, 3)}
    assert [n["id"] for n in s["nodes"]] == [1, 2]
    r.engine.command_light(2, 0, True)
    assert r.engine.snapshot()["lights"][0]["pending"] is True
