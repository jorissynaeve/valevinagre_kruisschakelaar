"""Datapunten van Tuya-toestellen lezen en zetten, of het toestel nu hier hangt of bij een andere installatie.

Een toestel is `<id>` (een Tuya-blok van deze installatie) of `r:<installatie>:y:<id>` (gekozen bij een andere installatie, zie
linkhub.py). Een punt is wat de zijbalk van het Tuya-blok toont: {dp, name, value, unit, rw, type, options, min, max, ...}, met de
schaal al toegepast. Voor de logica (`number`), de kaarten van het NSPanel en de routine *Tuya instellen*.
"""
from __future__ import annotations

from typing import Any

from .link import split_ref


class Points:
    def __init__(self, tuya=None, link=None) -> None:
        self.tuya, self.link = tuya, link

    def device(self, ref: str) -> dict | None:
        """{name, online, points: {dp: punt}} of None als het toestel er niet is (of niet gekozen is)."""
        ref = ref or ""
        if ref.startswith("r:"):
            hit = split_ref(ref)
            o = self.link.object(*hit) if hit and self.link and hit[1].startswith("y:") else None
            if o is None:
                return None
            return {"name": o.get("name") or ref, "online": bool(o.get("online") and self.link.is_up(hit[0])),
                    "points": {str(p.get("dp")): p for p in o.get("points") or []}}
        if self.tuya is None or ref not in self.tuya.devices:
            return None
        t = next((x for x in self.tuya.status() if x["id"] == ref), None)
        if t is None:
            return None
        return {"name": t["name"], "online": t["online"], "points": {p["dp"]: p for p in t["points"]}}

    def point(self, ref: str, dp: str) -> dict | None:
        d = self.device(ref)
        return d["points"].get(str(dp)) if d else None

    def number(self, ref: str, dp: str) -> float | None:
        """De waarde als getal (aan/uit = 1/0), voor een vergelijking. Onbekend als het toestel offline is of de waarde geen getal."""
        d = self.device(ref)
        if not d or not d["online"]:
            return None
        v = (d["points"].get(str(dp)) or {}).get("value")
        if isinstance(v, bool):
            return 1.0 if v else 0.0
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(v)  # een getal als tekst
        except (TypeError, ValueError):
            return None

    def text(self, ref: str, dp: str) -> str | None:
        """De waarde met eenheid, zoals op een scherm: "3.2 kW", "aan", "charging". None als het punt er niet is."""
        p = self.point(ref, dp)
        if p is None:
            return None
        v: Any = p.get("value")
        if isinstance(v, bool):
            s = "aan" if v else "uit"
        elif isinstance(v, float):
            s = f"{v:.2f}".rstrip("0").rstrip(".")
        else:
            s = "" if v is None else str(v)
        return f"{s} {p['unit']}".strip() if p.get("unit") and s else s

    def set(self, ref: str, dp: str, value: str) -> bool:
        """Een datapunt zetten: zelf, of via de installatie waar het toestel hangt (die beslist). ValueError als het hier geweigerd wordt."""
        if (ref or "").startswith("r:"):
            hit = split_ref(ref)
            return bool(hit and self.link and self.link.send(hit[0], hit[1], dp=str(dp), value=str(value)))
        if self.tuya is None:
            return False
        self.tuya.set(ref, str(dp), str(value))
        return True
