"""Frameformaat en payloads van het kruisschakelaar-protocol (zie DESIGN.md §4 en core/include/kk/frame.h).

Draad: ver:1 type:1 ttl:1 src:2 boot:2 ctr:2 | ciphertext | tag:16, alles little endian.
AEAD is XChaCha20-Poly1305; ttl zit niet in de geauthenticeerde data zodat forwarders niets opnieuw hoeven te seal'en.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
)
from nacl.exceptions import CryptoError

PROTO_VER = 1
HDR = 9
TAG = 16
MAX_FRAME = 250
MAX_PAYLOAD = MAX_FRAME - HDR - TAG
TTL_OFFSET = 2
DEFAULT_TTL = 4
ID_NONE = 0x0000
ID_BROADCAST = 0xFFFF
KEY_LEN = 32


class T(IntEnum):
    BTN_EVENT = 1
    LIGHT_STATE = 2
    HEARTBEAT = 3
    PROBE = 4
    PROBE_RESP = 5
    BEACON = 6
    HELLO = 7
    CFG_SET = 8
    CFG_ACK = 9
    LEARN = 10
    IDENTIFY = 11
    CMD_LIGHT = 12
    OTA_CMD = 13
    OTA_STATUS = 14
    ANNOUNCE = 15


# Frames die enkel binnen een eiland (een radiokanaal) betekenis hebben: nooit over de LAN-link.
LINK_LOCAL = frozenset({T.PROBE, T.PROBE_RESP, T.BEACON, T.ANNOUNCE})


class Mode(IntEnum):
    TOGGLE = 0
    ON = 1
    OFF = 2


MODE_NAMES = {Mode.TOGGLE: "toggle", Mode.ON: "on", Mode.OFF: "off"}
MODE_BY_NAME = {v: k for k, v in MODE_NAMES.items()}

CF_BRIDGE = 1
CF_UPLINK_ONLY = 2

MAX_RELAYS = 16
MAX_TRIG = 12
MAX_IND = 4

# Echte nodes hebben id 1..0xDFFF. 0xE000..0xEFFF zijn "virtuele nodes": apparaten die geen ESP-NOW spreken
# (bv. een Tuya-schakelaar via HA) en die de add-on op de mesh vertegenwoordigt. 0xF000 en hoger: add-ons.
VIRTUAL_MIN = 0xE000
VIRTUAL_MAX = 0xEFFF
MAX_GANGS = 8  # een knop-index is 0..7


def is_virtual(node: int) -> bool:
    return VIRTUAL_MIN <= node <= VIRTUAL_MAX


@dataclass
class Frame:
    type: int
    ttl: int
    src: int
    boot: int
    ctr: int
    payload: bytes = b""

    @property
    def key(self) -> int:
        """Sleutel voor dedup en replay: boot<<16 | ctr."""
        return (self.boot << 16) | self.ctr


def _nonce(ftype: int, src: int, boot: int, ctr: int) -> bytes:
    return struct.pack("<HHHB", src, boot, ctr, ftype) + bytes(17)


def _ad(ftype: int, src: int, boot: int, ctr: int) -> bytes:
    return struct.pack("<BBHHH", PROTO_VER, ftype, src, boot, ctr)


def seal(key: bytes, f: Frame) -> bytes:
    if len(f.payload) > MAX_PAYLOAD:
        raise ValueError("payload te groot")
    head = struct.pack("<BBBHHH", PROTO_VER, f.type, f.ttl, f.src, f.boot, f.ctr)
    ct = crypto_aead_xchacha20poly1305_ietf_encrypt(
        f.payload, _ad(f.type, f.src, f.boot, f.ctr), _nonce(f.type, f.src, f.boot, f.ctr), key
    )
    return head + ct


def open_frame(key: bytes, data: bytes) -> Frame | None:
    """Geeft None bij een ongeldig of vervalst frame."""
    if len(data) < HDR + TAG or len(data) > MAX_FRAME or data[0] != PROTO_VER:
        return None
    ver, ftype, ttl, src, boot, ctr = struct.unpack_from("<BBBHHH", data)
    try:
        payload = crypto_aead_xchacha20poly1305_ietf_decrypt(
            bytes(data[HDR:]), _ad(ftype, src, boot, ctr), _nonce(ftype, src, boot, ctr), key
        )
    except CryptoError:
        return None
    return Frame(ftype, ttl, src, boot, ctr, payload)


# ---------------------------------------------------------------- payloads lezen


@dataclass
class BtnEvent:
    btn: int
    action: int
    evt: int


@dataclass
class LightState:
    relay: int
    on: bool
    ver: int
    cause_src: int
    cause_btn: int  # 0xFF = commando (CMD_LIGHT)
    cause_evt: int


@dataclass
class Heartbeat:
    fw: int
    flags: int
    relays: list[tuple[bool, int]]  # (aan, ver) per relais
    nbtn: int | None  # ontbreekt bij oudere firmware
    nind: int | None
    cfg_hash: int | None


@dataclass
class CfgAck:
    dst: int
    kind: int
    idx: int
    n: int


def parse(f: Frame):
    """Payload van een frame als dataclass; None als het type niet (of ongeldig) wordt begrepen."""
    p = f.payload
    try:
        if f.type == T.BTN_EVENT:
            b, a, e = struct.unpack_from("<BBB", p)
            return BtnEvent(b, a, e)
        if f.type == T.LIGHT_STATE:
            r, on, ver, cs, cb, ce = struct.unpack_from("<BBHHBB", p)
            return LightState(r, bool(on), ver, cs, cb, ce)
        if f.type == T.HEARTBEAT:
            fw, flags, nrel = struct.unpack_from("<HBB", p)
            off = 4
            relays = []
            for _ in range(nrel):
                on, ver = struct.unpack_from("<BH", p, off)
                relays.append((bool(on), ver))
                off += 3
            nbtn = nind = h = None
            if len(p) >= off + 4:
                nbtn, nind, h = struct.unpack_from("<BBH", p, off)
            return Heartbeat(fw, flags, relays, nbtn, nind, h)
        if f.type == T.CFG_ACK:
            dst, kind, idx, n = struct.unpack_from("<HBBB", p)
            return CfgAck(dst, kind, idx, n)
    except struct.error:
        return None
    return None


# ---------------------------------------------------------------- payloads maken


def cmd_light(dst: int, relay: int, mode: Mode | int) -> bytes:
    return struct.pack("<HBB", dst, relay, int(mode))


def cfg_triggers(dst: int, relay: int, triggers: list[tuple[int, int, int]]) -> bytes:
    """triggers: [(node, knop, modus)]"""
    if len(triggers) > MAX_TRIG:
        raise ValueError("te veel triggers voor een relais")
    out = struct.pack("<HBBB", dst, 0, relay, len(triggers))
    for node, btn, mode in triggers:
        out += struct.pack("<HBB", node, btn, int(mode))
    return out


def cfg_follow(dst: int, ind: int, node: int | None, relay: int = 0) -> bytes:
    """Indicator `ind` van `dst` volgt licht (node, relay); node=None wist de koppeling."""
    n = ID_NONE if node is None else node
    return struct.pack("<HBBBHB", dst, 1, ind, 0 if node is None else 1, n, relay if node is not None else 0)


def btn_event(btn: int, evt: int) -> bytes:
    return struct.pack("<BBB", btn, 0, evt)


def light_state(relay: int, on: bool, ver: int, cause_src: int = 0, cause_btn: int = 0, cause_evt: int = 0) -> bytes:
    return struct.pack("<BBHHBB", relay, 1 if on else 0, ver & 0xFFFF, cause_src, cause_btn, cause_evt)


def heartbeat(fw: int, flags: int, relays: list[tuple[bool, int]], nbtn: int, nind: int, cfg_hash: int) -> bytes:
    out = struct.pack("<HBB", fw, flags, len(relays))
    for on, ver in relays:
        out += struct.pack("<BH", 1 if on else 0, ver & 0xFFFF)
    return out + struct.pack("<BBH", nbtn, nind, cfg_hash)


def learn(on: bool) -> bytes:
    return struct.pack("<B", 1 if on else 0)


def identify(dst: int) -> bytes:
    return struct.pack("<H", dst)


# ---------------------------------------------------------------- config-hash


def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def config_hash(
    relays: list[list[tuple[int, int, int]]],
    follows: list[tuple[int, int] | None],
) -> int:
    """Zelfde vingerafdruk als core/include/kk/cfghash.h: CRC-16/CCITT-FALSE.

    relays: per relais de triggers [(node, knop, modus)] in opgeslagen volgorde.
    follows: per indicator None of (node, relais).
    """
    b = bytearray()
    for trigs in relays:
        b.append(len(trigs))
        for node, btn, mode in trigs:
            b += struct.pack("<HBB", node, btn, int(mode))
    for f in follows:
        if f is None:
            b.append(0)
        else:
            b.append(1)
            b += struct.pack("<HB", f[0], f[1])
    return _crc16(bytes(b))


def newer16(a: int, b: int) -> bool:
    """a is nieuwer dan b in een 16-bit teller met wrap-around."""
    d = (a - b) & 0xFFFF
    return 0 < d < 0x8000
