"""Protocol: replay-venster, frames en payloads, en interop met de C++-kern via golden vectors.

De vectoren komen van `build/kkvec gen` (echte node-frames). Deze test schrijft ook `vectors_py.txt`
met wat Python zelf maakt; `build/kkvec check` leest dat terug (zie tools/test.sh addon).
"""
import os
import struct
from pathlib import Path

import pytest

from kkaddon import proto
from kkaddon.proto import T, Frame, Mode
from kkaddon.replay import ReplayTable

KEY = bytes((i * 7 + 3) & 0xFF for i in range(32))
VEC_DIR = Path(os.environ.get("KK_VECTORS", Path(__file__).resolve().parents[2] / "build"))


def test_replay_window():
    r = ReplayTable()
    assert r.accept(1, 100)
    assert not r.accept(1, 100)  # duplicaat
    assert r.accept(1, 101)
    assert r.accept(1, 99)  # uit volgorde, nog in het venster
    assert not r.accept(1, 99)
    assert r.accept(1, 200)
    assert not r.accept(1, 100)  # te oud
    assert r.accept(1, 150)
    assert r.accept(2, 100)  # andere bron
    assert r.accept(3, (5 << 16) | 65000)  # boot-overgang
    assert r.accept(3, (6 << 16) | 1)
    assert not r.accept(3, (5 << 16) | 65001)
    assert r.accept(4, 0xFFFFFFF0)  # wrap rond 2^32
    assert r.accept(4, 0x00000005)
    assert not r.accept(4, 0xFFFFFFF0)


def test_seal_open_roundtrip_and_tamper():
    f = Frame(T.BTN_EVENT, 4, 7, 3, 99, bytes([1, 0, 5]))
    raw = proto.seal(KEY, f)
    assert len(raw) == proto.HDR + 3 + proto.TAG
    g = proto.open_frame(KEY, raw)
    assert g == f
    other = bytearray(KEY)
    other[5] ^= 1
    assert proto.open_frame(bytes(other), raw) is None
    t = bytearray(raw)
    t[proto.TTL_OFFSET] = 1  # ttl is muteerbaar
    assert proto.open_frame(KEY, bytes(t)).ttl == 1
    for pos in (3, proto.HDR, len(raw) - 1):  # src, ciphertext, tag
        t = bytearray(raw)
        t[pos] ^= 1
        assert proto.open_frame(KEY, bytes(t)) is None
    assert proto.open_frame(KEY, raw[: proto.HDR + proto.TAG - 1]) is None
    assert proto.open_frame(KEY, bytes([2]) + raw[1:]) is None  # onbekende versie
    big = Frame(T.HEARTBEAT, 4, 1, 1, 1, bytes(proto.MAX_PAYLOAD))
    assert len(proto.seal(KEY, big)) == proto.MAX_FRAME
    with pytest.raises(ValueError):
        proto.seal(KEY, Frame(T.HEARTBEAT, 4, 1, 1, 1, bytes(proto.MAX_PAYLOAD + 1)))


def test_newer16():
    assert proto.newer16(2, 1)
    assert not proto.newer16(1, 2)
    assert not proto.newer16(5, 5)
    assert proto.newer16(0, 0xFFFF)  # wrap
    assert not proto.newer16(0xFFFF, 0)


# ---------------------------------------------------------------- C++-vectoren


def parse_spec(tokens):
    """'<nrelays> <nind> R<n> node,knop,modus ... I0 I1,node,relais' -> (relays, follows)."""
    it = iter(tokens)
    nr, ni = int(next(it)), int(next(it))
    relays = []
    for _ in range(nr):
        t = next(it)
        assert t[0] == "R"
        n = int(t[1:])
        relays.append([tuple(int(x) for x in next(it).split(",")) for _ in range(n)])
    follows = []
    for _ in range(ni):
        t = next(it)
        follows.append(tuple(int(x) for x in t[3:].split(",")) if t.startswith("I1") else None)
    return relays, follows


def load_cpp():
    p = VEC_DIR / "vectors_cpp.txt"
    if not p.exists():
        pytest.skip("build/vectors_cpp.txt ontbreekt: draai tools/test.sh addon")
    frames, hashes = {}, {}
    for line in p.read_text().splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "frame":
            frames[parts[1]] = proto.open_frame(KEY, bytes.fromhex(parts[2]))
            assert frames[parts[1]] is not None, f"{parts[1]} opent niet"
        elif parts[0] == "hash":
            hashes[parts[1]] = (int(parts[2], 16), parse_spec(parts[3:]))
    return frames, hashes


def test_hash_matches_cpp():
    _, hashes = load_cpp()
    assert {"empty", "B_final", "A_final", "big", "hb_A", "hb_B"} <= set(hashes)
    for name, (want, (relays, follows)) in hashes.items():
        assert proto.config_hash(relays, follows) == want, name


def test_node_frames_parse():
    frames, hashes = load_cpp()
    hb = proto.parse(frames["hb_A"])
    assert (frames["hb_A"].type, frames["hb_A"].src) == (T.HEARTBEAT, 1)
    assert hb.fw == 0x0102 and len(hb.relays) == 3 and not any(on for on, _ in hb.relays)
    assert (hb.nbtn, hb.nind) == (3, 3)
    assert hb.cfg_hash == hashes["hb_A"][0]
    hb = proto.parse(frames["hb_B"])
    assert frames["hb_B"].src == 2 and len(hb.relays) == 4 and (hb.nbtn, hb.nind) == (0, 0)
    assert hb.cfg_hash == hashes["hb_B"][0]

    assert proto.parse(frames["btn_A1"]) == proto.BtnEvent(1, 0, 1)
    ls = proto.parse(frames["light_B2"])
    assert (frames["light_B2"].src, ls.relay, ls.on, ls.ver) == (2, 2, True, 1)
    assert (ls.cause_src, ls.cause_btn, ls.cause_evt) == (1, 1, 1)
    assert proto.parse(frames["cfgack_B"]) == proto.CfgAck(0xF000, 0, 0, 2)
    assert proto.parse(frames["cfgack_A"]) == proto.CfgAck(0xF000, 1, 2, 1)


def test_our_payloads_equal_cpp():
    frames, _ = load_cpp()
    assert frames["cmdlight"].payload == proto.cmd_light(2, 1, Mode.ON)
    assert frames["learn"].payload == proto.learn(True)
    assert frames["cfgset_trig"].payload == proto.cfg_triggers(2, 0, [(1, 2, 0), (3, 0, 1)])
    assert frames["cfgset_follow"].payload == proto.cfg_follow(1, 2, 2, 0)
    assert frames["cmdlight"].src == frames["learn"].src == 0xF000


def test_heartbeat_without_new_fields_is_accepted():
    """Oudere firmware stuurt geen knop-aantal en hash: de rest moet gewoon werken."""
    p = struct.pack("<HBB", 7, 0, 2) + struct.pack("<BH", 1, 4) + struct.pack("<BH", 0, 9)
    hb = proto.parse(Frame(T.HEARTBEAT, 4, 5, 1, 1, p))
    assert hb.relays == [(True, 4), (False, 9)] and hb.nbtn is None and hb.cfg_hash is None
    assert proto.parse(Frame(T.HEARTBEAT, 4, 5, 1, 2, p[:5])) is None  # afgekapt: geen crash


def test_write_python_vectors():
    """Frames en hashes zoals Python ze maakt; de C++-kant leest ze met `kkvec check`."""
    VEC_DIR.mkdir(exist_ok=True)
    out = []

    def add(ftype, src, boot, ctr, payload, ttl=4):
        f = Frame(ftype, ttl, src, boot, ctr, payload)
        out.append(f"pyframe {int(ftype)} {ttl} {src} {boot} {ctr} {payload.hex() or '-'} {proto.seal(KEY, f).hex()}")

    add(T.CMD_LIGHT, 0xF000, 3, 1, proto.cmd_light(2, 1, Mode.ON))
    add(T.CMD_LIGHT, 0xF000, 3, 2, proto.cmd_light(9, 15, Mode.OFF))
    add(T.CFG_SET, 0xF000, 3, 3, proto.cfg_triggers(2, 0, [(1, 2, 0), (3, 0, 1)]))
    add(T.CFG_SET, 0xF000, 3, 4, proto.cfg_triggers(2, 5, []))
    add(T.CFG_SET, 0xF000, 3, 5, proto.cfg_triggers(4, 1, [(n, n % 8, n % 3) for n in range(12)]))
    add(T.CFG_SET, 0xF000, 3, 6, proto.cfg_follow(1, 2, 2, 0))
    add(T.CFG_SET, 0xF000, 3, 7, proto.cfg_follow(1, 3, None))
    add(T.LEARN, 0xF000, 3, 8, proto.learn(True))
    add(T.LEARN, 0xF000, 3, 9, proto.learn(False))
    add(T.IDENTIFY, 0xF000, 3, 10, proto.identify(2))
    add(T.IDENTIFY, 0xF000, 3, 11, proto.identify(proto.ID_BROADCAST))
    add(T.CMD_LIGHT, 0xF000, 0xFFFF, 0xFFFF, proto.cmd_light(1, 0, Mode.TOGGLE), ttl=1)

    def spec(relays, follows):
        s = f"{len(relays)} {len(follows)}"
        for trigs in relays:
            s += f" R{len(trigs)}" + "".join(f" {n},{b},{m}" for n, b, m in trigs)
        for f in follows:
            s += " I0" if f is None else f" I1,{f[0]},{f[1]}"
        return s

    cases = [
        ([[], []], [None]),
        ([[(1, 0, 0)], [], [(2, 1, 1), (3, 2, 2)]], [None, (5, 1), None]),
        ([[(65535, 7, 2)] * 12] + [[]] * 15, [(65534, 15)] * 4),
    ]
    for relays, follows in cases:
        out.append(f"pyhash {proto.config_hash(relays, follows):04x} {spec(relays, follows)}")
    (VEC_DIR / "vectors_py.txt").write_text("\n".join(out) + "\n")
    assert len(out) > 10
