"""De sprinkler: zones na elkaar, met een wachttijd ertussen, een aantal cycli lang.

  * Een beurt krijgt bij de start een vast plan; wat je daarna wijzigt geldt pas voor de volgende beurt.
  * De klok is de monotone klok van de engine, dus een wissel van zomer- naar wintertijd rekt of kort geen zone.
  * Een beurt staat alleen in het geheugen. Na een herstart is ze weg: een gemiste beurt wordt niet ingehaald, en
    de eerste aansturing zet een zone die nog aan stond uit.
  * Elk relais van een actieve sprinkler krijgt om de `refresh` seconden opnieuw zijn stand, ook als die "uit" is.
    Zo herstelt een gemist bevel zichzelf. Tegen het wegvallen van de add-on, HA of de wifi helpt dat niet: daarvoor
    moet het toestel zelf een limiet hebben (zie DOCS.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .model import MAX_CYCLES, MAX_WAIT, ModelError

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

TICK = 1.0  # s
RETRY = 5.0  # s: een bevel dat mislukte zo snel opnieuw proberen


@dataclass
class Step:
    zone: int
    cycle: int
    entity: str
    on_at: float
    off_at: float


@dataclass
class Run:
    steps: list[Step]
    cycles: int
    refresh: int

    @property
    def end(self) -> float:
        return self.steps[-1].off_at


class Sprinklers:
    def __init__(self, eng: "Engine") -> None:
        self.e = eng
        self.runs: dict[str, Run] = {}
        self._sent: dict[str, tuple[bool, float]] = {}  # relais -> laatst gezonden stand en wanneer opnieuw
        self._failing: set[str] = set()
        self._last: dict[str, tuple] = {}  # sprinkler -> de minuut van de laatste automatische start

    # ------------------------------------------------------------ plan

    def _number(self, sp: dict, name: str, lo: int, hi: int) -> int:
        """Een aangesloten ingang gaat voor; geeft die onbekend, dan de waarde uit het paneel."""
        ref = sp["inputs"].get(name)
        v = self.e.ref_value(ref) if ref else None
        return max(lo, min(hi, sp[name] if v is None else int(v)))

    def _plan(self, sp: dict, now: float) -> Run | None:
        cycles, wait = self._number(sp, "cycles", 0, MAX_CYCLES), self._number(sp, "wait", 0, MAX_WAIT)
        zones = [(i, z) for i, z in enumerate(sp["zones"]) if z["entity"] and z["seconds"] > 0]  # niets aan te sturen: geen wachttijd
        steps, t = [], now
        for c in range(cycles):
            for i, z in zones:
                steps.append(Step(i, c, z["entity"], t, t + z["seconds"]))
                t += z["seconds"] + wait * 60
        return Run(steps, cycles, sp["refresh"]) if steps else None

    def _blocked(self, sp: dict) -> bool:
        ref = sp["inputs"].get("block")
        return bool(ref) and self.e.ref_value(ref) is True

    def _start_minute(self, sp: dict) -> int | None:
        ref = sp["inputs"].get("start")
        if ref:
            return self.e.ref_value(ref)  # onbekend (bv. geen locatie): dan start er niets
        return int(sp["start"][:2]) * 60 + int(sp["start"][3:])

    def _begin(self, sid: str, sp: dict, how: str) -> bool:
        run = self._plan(sp, self.e._clock())
        if run is None:
            self.e._emit("sprinkler", what="empty", id=sid, name=sp["name"])
            return False
        self.runs[sid] = run
        self.e._emit("sprinkler", what="start", id=sid, name=sp["name"], how=how, cycles=run.cycles)
        return True

    def _stop(self, sid: str, name: str, what: str) -> None:
        del self.runs[sid]
        self.e._emit("sprinkler", what=what, id=sid, name=name)

    # ------------------------------------------------------------ bediening uit de UI

    def start(self, sid: str) -> None:
        sp = self.e.model.sprinklers.get(sid)
        if sp is None:
            raise ModelError("onbekende sprinkler")
        if not sp["enabled"]:
            raise ModelError("Zet de sprinkler eerst actief.")
        if self._blocked(sp):
            raise ModelError("De ingang blokkeren staat op 1.")
        if sid in self.runs:
            raise ModelError("Deze sprinkler loopt al.")
        if not self._begin(sid, sp, "hand"):
            raise ModelError("Er is niets te doen: geef een zone een relais en een tijd, en minstens één cyclus.")
        self.tick()

    def stop(self, sid: str) -> bool:
        sp = self.e.model.sprinklers.get(sid)
        if sid not in self.runs:
            return False
        self._stop(sid, sp["name"] if sp else sid, "stopped")
        self.tick()
        return True

    # ------------------------------------------------------------ elke seconde

    def tick(self) -> None:
        e = self.e
        sprinklers = e.model.sprinklers
        if not sprinklers and not self.runs and not self._sent:
            return
        now = e._clock()
        local = e.local_now()
        minute = local.hour * 60 + local.minute
        for sid in list(self.runs):
            sp = sprinklers.get(sid)
            if sp is None or not sp["enabled"]:
                self._stop(sid, sp["name"] if sp else sid, "stopped")
        desired: dict[str, tuple[bool, int]] = {}
        for sid, sp in sprinklers.items():
            if not sp["enabled"]:
                continue
            for z in sp["zones"]:
                if z["entity"]:
                    desired[z["entity"]] = (False, sp["refresh"])
            if self._blocked(sp):
                if sid in self.runs:
                    self._stop(sid, sp["name"], "blocked")
                continue
            stamp = (local.date(), minute)
            if sid not in self.runs and self._last.get(sid) != stamp and local.weekday() in sp["days"] and self._start_minute(sp) == minute:
                self._last[sid] = stamp
                self._begin(sid, sp, "klok")
        for sid, run in list(self.runs.items()):
            if now >= run.end:
                sp = sprinklers.get(sid)
                self._stop(sid, sp["name"] if sp else sid, "done")
                continue
            for s in run.steps:
                on = s.on_at <= now < s.off_at
                if on or s.entity not in desired:
                    desired[s.entity] = (on or desired.get(s.entity, (False, 0))[0], run.refresh)
        if not (e.virt.link and e.ha_call):
            return  # geen verbinding: de tijd loopt door, een gemiste zone wordt niet ingehaald
        for ent, (on, refresh) in desired.items():
            self._send(ent, on, refresh, now)
        for ent in [x for x in self._sent if x not in desired]:  # niet meer van ons (uitgezet, verwijderd, ander relais): één keer uit
            e.ha_call(ent, False)
            del self._sent[ent]
            self._failing.discard(ent)

    def _send(self, ent: str, on: bool, refresh: int, now: float) -> None:
        last = self._sent.get(ent)
        if last and last[0] == on and now < last[1]:
            return
        e = self.e
        if e.ha_call(ent, on):
            self._sent[ent] = (on, now + refresh)
            self._failing.discard(ent)
            if on and (last is None or not last[0]):
                e._emit("sprinkler", what="zone", entity=ent)
        else:
            self._sent[ent] = (on, now + RETRY)
            if ent not in self._failing:
                self._failing.add(ent)
                e._emit("sprinkler", what="failed", entity=ent, on=on)

    # ------------------------------------------------------------ voor de UI

    def status(self, sid: str) -> dict:
        run = self.runs.get(sid)
        if run is None:
            return {"running": False, "zone": None, "cycle": None, "cycles": None, "left": None}
        now = self.e._clock()
        cur = next((s for s in run.steps if now < s.off_at), run.steps[-1])
        return {"running": True, "zone": cur.zone if cur.on_at <= now else None, "next": cur.zone, "cycle": cur.cycle + 1, "cycles": run.cycles,
                "left": round(run.end - now)}
