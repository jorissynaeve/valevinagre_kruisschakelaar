"""Regels die kiezen welke scène een ledstrip speelt (blok "Scènes" op het schema, DESIGN.md §43).

Een regelblok hoort bij een HUISDEV-apparaat met scènes en heeft:

  ingangen  een lijst met een naam, een soort en een bron: `bool` (een 0/1 van een timer, module of apparaat) of `time` (een tijd in minuten sinds middernacht)
  regels    van boven naar onder; de eerste waarvan alle voorwaarden kloppen bepaalt de scène. Een voorwaarde is een van
              on / off       een 0/1-ingang is 1 (of 0)
              between        de tijd van nu is tussen twee tijd-ingangen (begin hoort erbij, einde niet; "van" later dan "tot" loopt over middernacht)
              after / before de tijd van nu is na (of voor) een tijd-ingang
              wake           een wekker: van `before` minuten voor tot `hold` minuten na de tijd van een tijd-ingang. Is de ingang een 0/1 die een
                             timer geeft, dan telt de timer `before` minuten eerder (en `hold` doet niet mee: de timer bepaalt zelf het einde)

Dit bestand is alleen de gegevens en de berekening (puur); de bediening van het apparaat staat in scenehub.py. Onbekend (een ingang zonder waarde) telt als niet
waar, behalve als er geen enkele regel klopt: dan weten we niet of de scène nog hoort te spelen en blijft alles zoals het was."""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Callable

from .logic import BOOL, DAY, TIME, _between
from .model import ModelError, fits, source_type

log = logging.getLogger("kk.scenes")

BID_RE = re.compile(r"^q[0-9]{1,6}$")
IID_RE = re.compile(r"^i[0-9]{1,6}$")
KINDS = (BOOL, TIME)
OPS = ("on", "off", "between", "after", "before", "wake")
MAX_BLOCKS = 16
MAX_INPUTS = 12
MAX_RULES = 24
MAX_CONDS = 6
MAX_NAME = 60
MAX_WAKE = 720  # minuten voor of na de wekkertijd
DEFAULT_HOLD = 60


def _int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _name(v, dflt: str) -> str:
    return v.strip()[:MAX_NAME] if isinstance(v, str) and v.strip() else dflt


# ------------------------------------------------------------ controleren (puur)

def _clean_inputs(raw, old: dict | None, check_ref: Callable[[str, str], None]) -> tuple[list[dict], int]:
    """De lijst met ingangen: een bestaande heeft zijn `id`, een nieuwe niet (die krijgt de volgende). Geeft (lijst, volgende teller)."""
    if not isinstance(raw, list):
        raise ModelError("ingangen: een lijst verwacht")
    if len(raw) > MAX_INPUTS:
        raise ModelError(f"maximaal {MAX_INPUTS} ingangen")
    prev = {i["id"]: i for i in (old["inputs"] if old else [])}
    nxt = old["inext"] if old else 1
    out, seen = [], set()
    for x in raw:
        if not isinstance(x, dict):
            raise ModelError("ingang: object verwacht")
        iid = x.get("id")
        if iid in (None, ""):
            iid = f"i{nxt}"
            nxt += 1
        elif not isinstance(iid, str) or iid not in prev:
            raise ModelError(f"onbekende ingang {iid!r}")
        if iid in seen:
            raise ModelError("elke ingang hoogstens een keer")
        seen.add(iid)
        kind = x.get("kind", prev[iid]["kind"] if iid in prev else BOOL)
        if kind not in KINDS:
            raise ModelError("ingang: soort is bool of time")
        name = _name(x.get("name"), prev[iid]["name"] if iid in prev else f"Ingang {len(out) + 1}")
        ref = x.get("ref", prev[iid]["ref"] if iid in prev else None)
        if ref in ("", None):
            ref = None
        else:
            if not isinstance(ref, str):
                raise ModelError(f"ingang {name}: ongeldige bron")
            check_ref(ref, kind)
        out.append({"id": iid, "name": name, "kind": kind, "ref": ref})
    return out, nxt


def _minutes(x, label: str, lo: int, hi: int, dflt: int) -> int:
    if x is None:
        return dflt
    if isinstance(x, float) and x.is_integer():
        x = int(x)
    if not _int(x) or not lo <= x <= hi:
        raise ModelError(f"{label}: een geheel aantal minuten van {lo} tot {hi}")
    return x


def _clean_cond(c, ins: dict[str, dict]) -> dict:
    if not isinstance(c, dict) or c.get("op") not in OPS:
        raise ModelError(f"voorwaarde: kies een van {', '.join(OPS)}")
    op = c["op"]

    def pick(field: str, want: str) -> str:
        iid = c.get(field)
        if not isinstance(iid, str) or iid not in ins:
            raise ModelError("voorwaarde: kies een ingang")
        if ins[iid]["kind"] != want:
            raise ModelError(f"ingang {ins[iid]['name']} is geen {'0/1' if want == BOOL else 'tijd'}: dat past hier niet")
        return iid

    if op in ("on", "off"):
        return {"op": op, "in": pick("in", BOOL)}
    if op in ("after", "before"):
        return {"op": op, "in": pick("in", TIME)}
    if op == "between":
        return {"op": op, "from": pick("from", TIME), "to": pick("to", TIME)}
    iid = c.get("in")
    if not isinstance(iid, str) or iid not in ins:
        raise ModelError("voorwaarde: kies een ingang")
    before = _minutes(c.get("before"), "minuten ervoor", 0, MAX_WAKE, 0)
    if ins[iid]["kind"] == TIME:
        return {"op": "wake", "in": iid, "before": before, "hold": _minutes(c.get("hold"), "minuten erna", 1, MAX_WAKE, DEFAULT_HOLD)}
    ref = ins[iid]["ref"]
    if before and ref is not None and not ref.startswith("t:"):
        raise ModelError(f"ingang {ins[iid]['name']}: vroeger starten kan alleen met een timer of een tijd als wekker")
    return {"op": "wake", "in": iid, "before": before, "hold": 0}


def _clean_rules(raw, ins: list[dict], scenes) -> list[dict]:
    if not isinstance(raw, list):
        raise ModelError("regels: een lijst verwacht")
    if len(raw) > MAX_RULES:
        raise ModelError(f"maximaal {MAX_RULES} regels")
    by = {i["id"]: i for i in ins}
    out = []
    for n, r in enumerate(raw, 1):
        if not isinstance(r, dict):
            raise ModelError(f"regel {n}: object verwacht")
        sid = r.get("scene")
        if not isinstance(sid, str) or sid not in scenes:
            raise ModelError(f"regel {n}: kies een scène")
        conds = r.get("when")
        if not isinstance(conds, list) or not conds:
            raise ModelError(f"regel {n}: geef minstens een voorwaarde")
        if len(conds) > MAX_CONDS:
            raise ModelError(f"regel {n}: maximaal {MAX_CONDS} voorwaarden")
        try:
            out.append({"scene": sid, "when": [_clean_cond(c, by) for c in conds]})
        except ModelError as e:
            raise ModelError(f"regel {n}: {e}") from None
    return out


def clean_block(raw, old: dict | None, scenes, check_ref: Callable[[str, str], None], devices, owners: dict[str, str], bid: str | None = None) -> dict:
    """Een regelblok uit de API. `old`: wat er al staat (een gedeeltelijke wijziging laat de rest staan); `scenes`: de ids in de bibliotheek; `check_ref(ref, kind)`
    weigert een bron die niet bestaat of niet past; `devices`: de apparaten die scènes kunnen hebben; `owners`: apparaat -> het blok dat het al heeft (`bid` is dit blok)."""
    if not isinstance(raw, dict):
        raise ModelError("scènes: object verwacht")
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in old.items()} if old else {"name": "", "device": "", "inputs": [], "inext": 1, "rules": []}
    if "name" in raw or not old:
        out["name"] = _name(raw.get("name"), "Scènes")
    if "device" in raw:
        dev = raw["device"] or ""
        if dev and (not isinstance(dev, str) or dev not in devices):
            raise ModelError("onbekend apparaat")
        owner = owners.get(dev) if dev else None
        if owner and owner != bid:
            raise ModelError("dit apparaat heeft al een blok Scènes: een apparaat heeft er hoogstens een")
        out["device"] = dev
    if "inputs" in raw:
        out["inputs"], out["inext"] = _clean_inputs(raw["inputs"], old, check_ref)
    elif "refs" in raw:  # alleen de bronnen wijzigen (een draad op het schema): {ingang-id: bron of null}
        refs = raw["refs"]
        if not isinstance(refs, dict):
            raise ModelError("refs: object verwacht")
        by = {i["id"]: i for i in out["inputs"]}
        if any(k not in by for k in refs):
            raise ModelError("onbekende ingang")
        out["inputs"], out["inext"] = _clean_inputs([{**by[i["id"]], "ref": refs.get(i["id"], i["ref"])} for i in out["inputs"]], out, check_ref)
    if "rules" in raw:
        out["rules"] = _clean_rules(raw["rules"], out["inputs"], scenes)
    elif "inputs" in raw or "refs" in raw:
        out["rules"] = _clean_rules(out["rules"], out["inputs"], scenes)  # een ingang die een regel nog gebruikt, mag niet weg of van soort veranderen
    return out


def used_scenes(block: dict) -> list[str]:
    """De scènes van de regels, in de volgorde van de regels, elk een keer."""
    out: list[str] = []
    for r in block["rules"]:
        if r["scene"] not in out:
            out.append(r["scene"])
    return out


def condition_text(c: dict, names: dict[str, str]) -> str:
    n = lambda k: names.get(c.get(k), "?")  # noqa: E731
    return {"on": lambda: f"{n('in')} = 1", "off": lambda: f"{n('in')} = 0", "after": lambda: f"na {n('in')}", "before": lambda: f"voor {n('in')}",
            "between": lambda: f"tussen {n('from')} en {n('to')}",
            "wake": lambda: f"wekker {n('in')}" + (f", {c['before']} min ervoor" if c.get("before") else "") + (f", {c['hold']} min erna" if c.get("hold") else "")}[c["op"]]()


# ------------------------------------------------------------ rekenen (puur)

def eval_cond(c: dict, ins: dict[str, dict], read: Callable[[str | None], object], ahead: Callable[[str, int], bool | None], now: int) -> bool | None:
    """Een voorwaarde: True, False of None (een ingang zonder waarde). `read(ref)`: de waarde van een bron; `ahead(timer, minuten)`: staat die timer over zoveel
    minuten aan; `now`: minuten sinds middernacht."""
    def val(iid: str):
        v = read(ins[iid]["ref"]) if ins.get(iid) and ins[iid]["ref"] else None
        if ins.get(iid) and ins[iid]["kind"] == BOOL:
            return v if isinstance(v, bool) else None
        return v if _int(v) or isinstance(v, float) and not isinstance(v, bool) else None

    op = c["op"]
    if op in ("on", "off"):
        v = val(c["in"])
        return None if v is None else v is (op == "on")
    if op in ("after", "before"):
        t = val(c["in"])
        return None if t is None else (now >= t if op == "after" else now < t)
    if op == "between":
        a, b = val(c["from"]), val(c["to"])
        return None if a is None or b is None else _between(now, int(a), int(b))
    inp = ins.get(c["in"])
    if inp is None:
        return None
    if inp["kind"] == TIME:
        t = val(c["in"])
        if t is None:
            return None
        return _between(now, int(t - c["before"]) % DAY, int(t + c["hold"]) % DAY) if c["before"] + c["hold"] < DAY else True
    ref = inp["ref"]
    if ref and ref.startswith("t:") and c["before"]:
        return ahead(ref[2:], c["before"])
    return val(c["in"])


def evaluate(block: dict, read, ahead, now: int) -> tuple[int | None, bool]:
    """(index van de eerste regel die klopt of None, `onzeker`): onzeker is True als er een regel was waarvan we het niet weten en er geen regel klopt."""
    ins = {i["id"]: i for i in block["inputs"]}
    unsure = False
    for n, r in enumerate(block["rules"]):
        res = [eval_cond(c, ins, read, ahead, now) for c in r["when"]]
        if all(x is True for x in res):
            return n, False
        if False not in res:
            unsure = True
    return None, unsure


# ------------------------------------------------------------ bewaren

class RuleStore:
    """/data/scenerules.json: `{"next": N, "blocks": {"q1": {name, device, inext, inputs, rules}}}`. Ids worden nooit hergebruikt."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.next = 1
        self.items: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (ValueError, OSError, AttributeError) as e:
            log.error("%s niet leesbaar: %s", self.path, e)
            return
        if not isinstance(raw, dict):
            return
        top = 1
        for bid, v in (raw.get("blocks") if isinstance(raw.get("blocks"), dict) else {}).items():
            if not isinstance(bid, str) or not BID_RE.match(bid) or not isinstance(v, dict):
                continue
            top = max(top, int(bid[1:]) + 1)
            ins, nxt, used = [], 1, set()
            for x in v.get("inputs") if isinstance(v.get("inputs"), list) else []:
                if isinstance(x, dict) and isinstance(x.get("id"), str) and IID_RE.match(x["id"]) and x["id"] not in used and x.get("kind") in KINDS and len(ins) < MAX_INPUTS:
                    used.add(x["id"])
                    nxt = max(nxt, int(x["id"][1:]) + 1)
                    ins.append({"id": x["id"], "name": _name(x.get("name"), x["id"]), "kind": x["kind"], "ref": x["ref"] if isinstance(x.get("ref"), str) and x["ref"] else None})
            rules = []
            for r in v.get("rules") if isinstance(v.get("rules"), list) else []:
                try:  # een regel die niet meer klopt (een ingang of scène die weg is) valt weg; de rest blijft
                    rules += _clean_rules([r], ins, {r.get("scene")} if isinstance(r, dict) else set())
                except ModelError as e:
                    log.warning("scènes %s: een regel valt weg: %s", bid, e)
            n = v.get("inext")
            self.items[bid] = {"name": _name(v.get("name"), "Scènes"), "device": v["device"] if isinstance(v.get("device"), str) else "", "inputs": ins,
                               "inext": max(nxt, n if _int(n) else 1), "rules": rules[:MAX_RULES]}
        n = raw.get("next")
        self.next = max(top, n if _int(n) else 1)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"next": self.next, "blocks": self.items}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def device_owner(self) -> dict[str, str]:
        """Apparaat -> id van het blok dat het heeft."""
        return {b["device"]: bid for bid, b in self.items.items() if b["device"]}
