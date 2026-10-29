"""Testhulp: een Engine met bestuurbare klok, en frames zoals nodes ze zouden sturen."""
import random
import struct

from kkaddon import proto
from kkaddon.engine import Engine, Tuning
from kkaddon.model import Model
from kkaddon.proto import T, Frame

KEY = bytes((i * 7 + 3) & 0xFF for i in range(32))
PEER = 0xF000


class Rig:
    def __init__(self, tmp_path=None, model=None, **tuning):
        self.mono = 1000.0
        self.wall = 1_700_000_000.0
        self.sent: list[Frame] = []  # alles wat de engine uitzendt (incl. kopieën)
        self.model = model or Model(tmp_path / "network.json" if tmp_path else None)
        self.tmp = tmp_path
        self.tuning = Tuning(**tuning)
        self.ha_calls: list[tuple[str, bool]] = []
        self.engine = self._engine()
        self.events: list[dict] = []
        self.engine.subscribe(self.events.append)
        self._ctr: dict[int, int] = {}
        self._boot: dict[int, int] = {}

    def _ha_call(self, entity, on):
        self.ha_calls.append((entity, on))
        return True

    def _engine(self):
        e = self._make()
        e.ha_call = self._ha_call
        e.virt.set_link(True)
        return e

    def _make(self):
        return Engine(
            KEY,
            self.model,
            self._tx,
            peer_id=PEER,
            clock=lambda: self.mono,
            wall=lambda: self.wall,
            rng=random.Random(7),
            tuning=self.tuning,
            state_path=self.tmp / "state.json" if self.tmp else None,
        )

    def restart(self):
        """Nieuwe Engine op dezelfde opslag (add-on herstart)."""
        self.engine = self._engine()
        self.events = []
        self.engine.subscribe(self.events.append)
        return self.engine

    def _tx(self, raw: bytes):
        f = proto.open_frame(KEY, raw)
        assert f is not None, "engine zond een frame dat niet opent"
        self.sent.append(f)

    # ------------------------------------------------------------ tijd

    def advance(self, seconds: float, step: float = 0.005):
        end = self.mono + seconds
        while self.mono < end - 1e-9:
            self.mono += step
            self.wall += step
            self.engine.tick()

    # ------------------------------------------------------------ frames van nodes

    def boot(self, src: int, boot: int):
        """Laat een node 'herstarten': nieuwe boot, teller opnieuw."""
        self._boot[src] = boot
        self._ctr[src] = 0

    def inject(self, src: int, ftype: int, payload: bytes, ttl: int = 4) -> Frame:
        ctr = self._ctr.get(src, 0)
        self._ctr[src] = ctr + 1
        f = Frame(ftype, ttl, src, self._boot.get(src, 1), ctr, payload)
        self.engine.on_datagram(proto.seal(KEY, f))
        return f

    def heartbeat(self, src, relays=((False, 0),), nbtn=3, nind=0, cfg_hash=None, fw=0x0102, flags=0):
        p = struct.pack("<HBB", fw, flags, len(relays))
        for on, ver in relays:
            p += struct.pack("<BH", int(on), ver)
        if cfg_hash is not None:
            p += struct.pack("<BBH", nbtn, nind, cfg_hash)
        return self.inject(src, T.HEARTBEAT, p)

    def light_state(self, src, relay, on, ver, cause=(0, 0, 0)):
        return self.inject(src, T.LIGHT_STATE, struct.pack("<BBHHBB", relay, int(on), ver, *cause))

    def button(self, src, btn, evt):
        return self.inject(src, T.BTN_EVENT, bytes([btn, 0, evt]))

    def cfg_ack(self, src, kind, idx, n=0):
        return self.inject(src, T.CFG_ACK, struct.pack("<HBBB", PEER, kind, idx, n))

    # ------------------------------------------------------------ wat de engine zond

    def sent_of(self, ftype) -> list[Frame]:
        return [f for f in self.sent if f.type == ftype]

    def clear(self):
        self.sent.clear()

    def events_of(self, kind, **match):
        return [e for e in self.events if e["t"] == kind and all(e.get(k) == v for k, v in match.items())]
