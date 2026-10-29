"""Leest de oude AppDaemon-configuratie (`apps.yaml`) met `module: kruisschakelaar` en maakt er kringen van.

    kruisschakelaar_lamp_keuken:
      module: kruisschakelaar
      class: Kruisschakelaar
      entities:
        - switch.r4b01_sonoff_4ch_r4_01_relay_4
        - light.t4b001_t4b001_relay_1

Regels met `#` zijn commentaar (dus uitgeschakelde entiteiten blijven weg). Alles wat geen kring is wordt overgeslagen
met een reden, zodat je gerust een heel apps.yaml kunt plakken.
"""
from __future__ import annotations

import re

import yaml

from .model import ENTITY_RE, MAX_GROUP_ENTITIES, MAX_NAME, ModelError

MAX_TEXT = 200_000


def _gid(key: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", key.lower()).strip("_")[:80]


def _name(key: str) -> str:
    s = re.sub(r"^kruisschakelaar_", "", key.lower())
    s = re.sub(r"^lamp_", "", s).replace("_", " ").strip()
    return (s[:1].upper() + s[1:])[:MAX_NAME] or key[:MAX_NAME]


def parse(text: str) -> dict:
    """-> {"groups": [{id, name, entities, warnings}], "skipped": [{key, reason}]}. Gooit ModelError bij onleesbare invoer."""
    if len(text) > MAX_TEXT:
        raise ModelError("de tekst is te groot voor een kringenlijst")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ModelError(f"ongeldige YAML: {str(e).splitlines()[0] if str(e) else 'onleesbaar'}")
    if not isinstance(data, dict):
        raise ModelError("verwacht een lijst met kringen (naam: met module, class en entities)")
    groups, skipped, seen = [], [], set()
    for key, val in data.items():
        key = str(key)
        if not isinstance(val, dict) or "entities" not in val:
            skipped.append({"key": key, "reason": "geen kring (geen entities)"})
            continue
        if val.get("class") not in (None, "Kruisschakelaar") or val.get("module") not in (None, "kruisschakelaar"):
            skipped.append({"key": key, "reason": f"andere app ({val.get('module')}.{val.get('class')})"})
            continue
        gid = _gid(key)
        if not gid or gid in seen:
            skipped.append({"key": key, "reason": "naam dubbel of onbruikbaar"})
            continue
        ents, warnings = [], []
        raw = val["entities"] if isinstance(val["entities"], list) else []
        for ent in raw:
            ent = str(ent).strip()
            if not ENTITY_RE.match(ent):
                warnings.append(f"ongeldige entiteit overgeslagen: {ent}")
            elif ent in ents:
                warnings.append(f"dubbele entiteit overgeslagen: {ent}")
            else:
                ents.append(ent)
        if len(ents) > MAX_GROUP_ENTITIES:
            warnings.append(f"alleen de eerste {MAX_GROUP_ENTITIES} entiteiten overgenomen")
            ents = ents[:MAX_GROUP_ENTITIES]
        if len(ents) < 2:
            skipped.append({"key": key, "reason": "minder dan 2 entiteiten"})
            continue
        seen.add(gid)
        groups.append({"id": gid, "name": _name(key), "entities": ents, "warnings": warnings})
    return {"groups": groups, "skipped": skipped}
