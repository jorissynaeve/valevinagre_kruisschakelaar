"""De koppeling tussen Huisconfig-installaties (elk subnet zijn eigen HA en add-on): sleutel, handdruk en berichten. Puur, zonder I/O.

Alle installaties van een huis delen één koppelsleutel. Het verkeer loopt over een websocket op een eigen TCP-poort, maar elk
bericht is zelf versleuteld en ondertekend (XSalsa20-Poly1305, PyNaCl): de websocket hoeft niet te vertrouwen.

  1. wie verbindt stuurt `hello` {id, name, n}, versleuteld met de koppelsleutel en een willekeurige nonce
  2. de ander antwoordt `welcome` {id, name, n}, ook zo
  3. de sessiesleutel = BLAKE2b(koppelsleutel, n_wie_verbindt, n_ander). Daarna heeft elk bericht een nonce met de richting en een
     teller die alleen stijgt: een opgenomen bericht nog eens sturen werkt niet, ook niet in een andere sessie (andere sleutel).

Wie de sleutel niet heeft, komt niet voorbij stap 1 en krijgt geen antwoord.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Any

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox

VERSION = 4  # 2: catalogus en keuze (want); 3: gangen (e:) voor kringen over installaties; 4: laadpalen met sessies (k:). Een andere versie wordt bij de handdruk geweigerd
MAX_MESSAGE = 4 * 1024 * 1024
ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
MIN_KEY = 16  # tekens; de knop in de UI maakt er 64 (32 bytes, hex)
_OBJ = r"g:[a-z0-9_]{1,80}|l\d{1,5}\.\d{1,2}|y:[a-z0-9_]{1,80}|k:[a-z0-9_]{1,40}|e:[a-z0-9_]{1,60}\.[a-z0-9_]{1,190}"
OBJ_RE = re.compile(rf"^({_OBJ})$")  # wat een installatie aanbiedt: een kring, een licht, een Tuya-toestel, een gang (e:<HA-entiteit>)
REF_RE = re.compile(rf"^r:([a-z0-9_]{{1,40}}):({_OBJ})$")
GANG_REF_RE = re.compile(r"^r:[a-z0-9_]{1,40}:e:[a-z0-9_]{1,60}\.[a-z0-9_]{1,190}$")  # een gang van een andere installatie, als lid van een kring


def split_ref(ref: str) -> tuple[str, str] | None:
    """`r:<installatie>:<object>` (een object van een andere installatie) -> (installatie, object)."""
    m = REF_RE.match(ref or "")
    return (m[1], m[2]) if m else None


class LinkError(Exception):
    pass


def new_key() -> str:
    return secrets.token_hex(32)


def derive(text: str) -> bytes:
    """De koppelsleutel zoals je hem intypt (bv. 64 hex-tekens) -> 32 bytes. Spaties eromheen tellen niet."""
    text = (text or "").strip()
    if len(text) < MIN_KEY:
        raise LinkError(f"de koppelsleutel moet minstens {MIN_KEY} tekens hebben")
    return hashlib.blake2b(text.encode("utf-8"), digest_size=32, person=b"kk-link-v1").digest()


def _dump(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _load(data: bytes) -> dict:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise LinkError("onleesbaar bericht") from e
    if not isinstance(obj, dict):
        raise LinkError("onleesbaar bericht")
    return obj


def seal_hello(key: bytes, kind: str, me: str, name: str, nonce: bytes) -> bytes:
    return bytes(SecretBox(key).encrypt(_dump({"t": kind, "v": VERSION, "id": me, "name": name, "n": nonce.hex()})))


def open_hello(key: bytes, data: bytes, kind: str) -> tuple[str, str, bytes]:
    """(id, naam, nonce) van de andere kant. LinkError bij een verkeerde sleutel of een ongeldig bericht."""
    try:
        m = _load(SecretBox(key).decrypt(data))
    except CryptoError as e:
        raise LinkError("verkeerde koppelsleutel") from e
    if m.get("t") != kind:
        raise LinkError("onverwacht bericht")
    if m.get("v") != VERSION:
        raise LinkError(f"de andere installatie heeft een andere versie van de koppeling ({m.get('v')!r}, hier {VERSION}): werk beide add-ons bij")
    pid, name = m.get("id"), m.get("name")
    if not isinstance(pid, str) or not ID_RE.match(pid) or not isinstance(name, str) or len(name) > 60:
        raise LinkError("ongeldige naam van de andere installatie")
    try:
        n = bytes.fromhex(m.get("n") or "")
    except ValueError as e:
        raise LinkError("ongeldige handdruk") from e
    if len(n) != 16:
        raise LinkError("ongeldige handdruk")
    return pid, name, n


class Session:
    """Na de handdruk: berichten met een eigen sleutel, een richting en een stijgende teller."""

    def __init__(self, key: bytes, n_initiator: bytes, n_other: bytes, initiator: bool) -> None:
        self.box = SecretBox(hashlib.blake2b(key + n_initiator + n_other, digest_size=32, person=b"kk-link-sess").digest())
        self.out_dir, self.tx, self.rx = (0 if initiator else 1), 0, 0

    def seal(self, obj: Any) -> bytes:
        self.tx += 1
        nonce = bytes([self.out_dir]) + self.tx.to_bytes(8, "big") + bytes(15)
        return bytes(self.box.encrypt(_dump(obj), nonce))

    def open(self, data: bytes) -> dict:
        if len(data) < 24 + 16:
            raise LinkError("te kort bericht")
        d, n = data[0], int.from_bytes(data[1:9], "big")
        if d == self.out_dir or d not in (0, 1) or n <= self.rx:
            raise LinkError("herhaald of verkeerd gericht bericht")
        try:
            plain = self.box.decrypt(data)
        except CryptoError as e:
            raise LinkError("bericht is niet van de andere installatie") from e
        self.rx = n
        return _load(plain)
