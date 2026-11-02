"""Tuya instellen: een 0/1 van een timer of module zet een datapunt van een Tuya-toestel (hier of bij een andere installatie).

Bv. de laadpaal starten om 23:00 (`124` op `OpenCharging` bij 1, `CloseCharging` bij 0), of de laadstroom op 8 A zetten als het
verbruik hoog is. Omdat zo'n routine een lader kan starten, is ze voorzichtig:

  * Een nieuwe routine staat **uit**. Uit volgt ze de ingang stil: aanzetten doet dus ook niets tot de ingang daarna verandert.
  * De **eerste stand** die ze ziet (na een herstart, of als de ingang terug bekend is) onthoudt ze alleen. Er gebeurt pas iets
    als de ingang daarna verandert: een herstart of een weggevallen verbinding start nooit een lader.
  * Een **onbekende** ingang doet niets.
  * Ze stuurt **hoogstens om de `interval` seconden** (standaard 60): een waarde die rond een grens schommelt laat het toestel
    niet klapperen. Is de ingang na die tijd nog altijd anders, dan gaat het bevel alsnog.
  * Een mislukt bevel (toestel offline, geweigerd) wordt na `interval` opnieuw geprobeerd, zolang de ingang anders blijft.
  * Bij 0 een lege waarde: dan gebeurt er bij 0 niets (alleen bij 1 iets zetten).

Alles in het geheugen: wat al gezet is, weet de routine na een herstart niet meer, en dat is de bedoeling (zie het tweede punt).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine


class Actions:
    def __init__(self, eng: "Engine") -> None:
        self.e = eng
        self.applied: dict[str, bool] = {}  # routine -> de ingang waarvoor het laatst iets gezet is (of die stil gevolgd is)
        self.last_at: dict[str, float] = {}
        self.state: dict[str, dict] = {}  # routine -> {input, text, ok} voor de pagina

    def tick(self) -> None:
        model, now = self.e.model, self.e.now()
        for aid in [x for x in self.applied if x not in model.actions]:
            self.applied.pop(aid, None)
            self.last_at.pop(aid, None)
            self.state.pop(aid, None)
        for aid, a in model.actions.items():
            ref = a["inputs"].get("trigger")
            v = self.e.ref_value(ref) if ref else None
            v = v if isinstance(v, bool) else None
            if v is None:
                self.applied.pop(aid, None)  # na een onbekende ingang telt de volgende stand weer als eerste stand
                self.state[aid] = {"input": None, "text": "ingang onbekend of niet aangesloten: doet niets", "ok": True}
                continue
            if aid not in self.applied or not a["enabled"]:
                self.applied[aid] = v
                if aid not in self.state or self.state[aid].get("input") != v:
                    self.state[aid] = {"input": v, "text": "uit: volgt de ingang stil" if not a["enabled"] else "eerste stand onthouden, wacht op een wijziging", "ok": True}
                continue
            if v == self.applied[aid]:
                continue
            value = a["on_value"] if v else a["off_value"]
            if value == "" or not a["device"] or not a["dp"]:
                self.applied[aid] = v
                self.state[aid] = {"input": v, "text": "niets in te stellen bij deze stand", "ok": True}
                continue
            last = self.last_at.get(aid)
            if last is not None and now - last < a["interval"]:
                self.state[aid] = {"input": v, "text": f"wacht: hoogstens elke {a['interval']} s", "ok": True}
                continue
            self.last_at[aid] = now
            why = ""
            try:
                ok = bool(self.e.points and self.e.points.set(a["device"], a["dp"], value))
                if not ok:
                    why = "toestel niet bereikbaar"
            except ValueError as err:  # ModelError: geweigerd door het datamodel
                ok, why = False, str(err)
            if ok:
                self.applied[aid] = v
                self.state[aid] = {"input": v, "text": f"{a['dp']} gezet op {value}", "ok": True}
                self.e._emit("action", what="set", action=aid, name=a["name"], device=a["device"], dp=a["dp"], value=value)
            else:
                self.state[aid] = {"input": v, "text": f"niet gezet: {why}; opnieuw na {a['interval']} s", "ok": False}
                self.e._emit("action", what="failed", action=aid, name=a["name"], device=a["device"], dp=a["dp"], value=value, why=why)

    def view(self, aid: str) -> dict:
        return self.state.get(aid) or {"input": None, "text": "nog niet bekeken", "ok": True}
