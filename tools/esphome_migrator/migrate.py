"""Oude ESPHome-YAML (2019-2021) omzetten naar syntaxis voor een recente ESPHome.

Alleen structurele aanpassingen die zeker zijn. Wat daarna nog niet klopt, meldt `esphome config` (zie app.py).
Wifi-wachtwoorden gaan naar secrets.yaml in de werkmap, nooit in de gegenereerde YAML.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class Tagged:
    """Een waarde met een ESPHome-tag (!lambda, !secret, ...), zodat ze onaangeroerd terug in de YAML komt."""

    def __init__(self, tag: str, value) -> None:
        self.tag, self.value = tag, value


class _Loader(yaml.SafeLoader):
    pass


def _tagged(loader: _Loader, suffix: str, node: yaml.Node):
    if isinstance(node, yaml.ScalarNode):
        return Tagged("!" + suffix, loader.construct_scalar(node))
    if isinstance(node, yaml.SequenceNode):
        return Tagged("!" + suffix, loader.construct_sequence(node, deep=True))
    return Tagged("!" + suffix, loader.construct_mapping(node, deep=True))


_Loader.add_multi_constructor("!", _tagged)


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data) -> bool:
        return True


def _rep_tagged(d: _Dumper, t: Tagged):
    if isinstance(t.value, str):
        multi = "\n" in t.value
        return d.represent_scalar(t.tag, t.value.rstrip("\n") + ("\n" if multi else ""), style="|" if multi else None)
    if isinstance(t.value, list):
        return d.represent_sequence(t.tag, t.value)
    return d.represent_mapping(t.tag, t.value)


_Dumper.add_representer(Tagged, _rep_tagged)
_Dumper.add_representer(type(None), lambda d, _: d.represent_scalar("tag:yaml.org,2002:null", ""))


class MigrateError(Exception):
    pass


@dataclass
class Result:
    name: str
    yaml_text: str
    notes: list[str] = field(default_factory=list)
    wifi: list[tuple[str, str]] = field(default_factory=list)  # (ssid, wachtwoord) zoals in het oude bestand
    chip: str = ""
    board: str = ""


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "net"


def secret_keys(ssid: str) -> tuple[str, str]:
    return f"wifi_ssid_{slug(ssid)}", f"wifi_password_{slug(ssid)}"


def load_old(text: str) -> dict:
    if re.search(r"^\s*<<:|!include", text, re.M):
        raise MigrateError("gebruikt !include of <<: (substitutions/pakketten): niet automatisch omzetbaar")
    d = yaml.load(text, Loader=_Loader)
    if not isinstance(d, dict) or not isinstance(d.get("esphome"), dict):
        raise MigrateError("geen esphome:-blok gevonden")
    return d


def _wifi(d: dict, default: list[tuple[str, str]] | None) -> list[tuple[str, str]]:
    w = d.get("wifi")
    if w is None and default:  # `wifi:` zonder inhoud
        d["wifi"] = w = {}
    if not isinstance(w, dict):
        raise MigrateError("geen wifi:-blok, dus geen gegevens om het toestel weer op het netwerk te krijgen")
    nets: list[tuple[str, str]] = []
    if isinstance(w.get("ssid"), str):
        nets.append((w["ssid"], str(w.get("password") or "")))
    for n in w.get("networks") or []:
        if isinstance(n, dict) and isinstance(n.get("ssid"), str):
            nets.append((n["ssid"], str(n.get("password") or "")))
    if not nets and default:
        return list(default)
    if not nets:
        raise MigrateError("wifi: zonder ssid of networks: het toestel zou na de upload niet op het netwerk komen")
    return nets


def _dallas(new: dict, notes: list[str]) -> None:
    """`dallas:` is `one_wire:` geworden (platform gpio) en de sensor `dallas_temp` (ESPHome 2025.x)."""
    hub = new.pop("dallas", None)
    sensors = new.get("sensor")
    used = isinstance(sensors, list) and any(isinstance(x, dict) and x.get("platform") == "dallas" for x in sensors)
    if hub is None and not used:
        return
    hubs = hub if isinstance(hub, list) else ([hub] if hub else [])
    new["one_wire"] = [{"platform": "gpio", **{("id" if k == "id" else k): v for k, v in h.items()}} for h in hubs if isinstance(h, dict)]
    for x in sensors or []:
        if isinstance(x, dict) and x.get("platform") == "dallas":
            x["platform"] = "dallas_temp"
            if "dallas_id" in x:
                x["one_wire_id"] = x.pop("dallas_id")
    notes.append("dallas: omgezet naar one_wire: en dallas_temp")


def migrate(text: str, fallback_name: str = "", default_wifi: list[tuple[str, str]] | None = None) -> Result:
    d = load_old(text)
    notes: list[str] = []
    e = d["esphome"]
    name = str(e.get("name") or fallback_name)
    if not name:
        raise MigrateError("esphome.name ontbreekt")
    nets = _wifi(d, default_wifi)
    if not any(isinstance(d["wifi"].get(k), (str, list)) and d["wifi"].get(k) for k in ("ssid", "networks")):
        notes.append("geen wifi-gegevens in het bestand: standaardnetwerk uit de instellingen gebruikt")
    new: dict = {}

    # esphome: platform/board -> eigen blok per chip (ESPHome 2021.10)
    plat, board = e.get("platform"), e.get("board")
    gone = ("platform", "board", "arduino_version", "board_flash_mode", "esp8266_restore_from_flash", "name")
    new["esphome"] = {"name": name, **{k: v for k, v in e.items() if k not in gone}}
    chip = next((k for k in ("esp8266", "esp32") if k in d), None)
    if plat or board:
        chip = "esp32" if str(plat).upper() == "ESP32" else "esp8266"
        blk: dict = {"board": board}
        if e.get("board_flash_mode"):
            blk["board_flash_mode"] = e["board_flash_mode"]
        if e.get("esp8266_restore_from_flash"):
            blk["restore_from_flash"] = e["esp8266_restore_from_flash"]
        if chip == "esp32":
            blk["framework"] = {"type": "arduino"}
        if e.get("arduino_version"):
            notes.append(f"arduino_version {e['arduino_version']} weggelaten: ESPHome kiest zelf de Arduino-versie")
        if chip == "esp8266" and "board_flash_mode" not in blk and re.search(r"GPIO0?(9|10)\b", text) and str(board) in ("esp8285", "esp01_1m"):
            blk["board_flash_mode"] = "dout"  # GPIO9/10 zijn bij de ESP8285 gewone pinnen, maar alleen in dout-modus
            notes.append("board_flash_mode: dout toegevoegd (de YAML gebruikt GPIO9/10)")
        new[chip] = blk
        notes.append(f"platform/board verplaatst naar een eigen blok {chip}:")
    elif chip:
        new[chip] = d[chip]
    else:
        raise MigrateError("geen platform/board en geen esp8266:/esp32:-blok")

    for k, v in d.items():
        if k not in ("esphome", "esp8266", "esp32", "wifi"):
            new[k] = v

    # wifi met !secret
    nw = {k: v for k, v in d["wifi"].items() if k not in ("ssid", "password", "networks")}
    nw["networks"] = [{"ssid": Tagged("!secret", secret_keys(s)[0]), "password": Tagged("!secret", secret_keys(s)[1])} for s, _ in nets]
    new["wifi"] = nw

    # ota: -> lijst met platform esphome (ESPHome 2024.6)
    o = new.get("ota")
    if "ota" not in new:
        new["ota"] = [{"platform": "esphome"}]
        notes.append("ota: toegevoegd (anders kun je het toestel niet meer draadloos bijwerken)")
    elif o is None:
        new["ota"] = [{"platform": "esphome"}]
    elif isinstance(o, dict):
        new["ota"] = [{"platform": "esphome", **o}]

    if isinstance(new.get("api"), dict) and "password" in new["api"]:
        notes.append("api.password weggelaten (bestaat niet meer in recente ESPHome)")
        del new["api"]["password"]
        if not new["api"]:
            new["api"] = None
    if "api" not in new:
        new["api"] = None
        notes.append("api: toegevoegd (Home Assistant heeft het nodig)")

    _dallas(new, notes)
    order = ["esphome", "esp8266", "esp32", "wifi", "api", "ota", "logger", "web_server"]
    out = {k: new[k] for k in order if k in new}
    out.update({k: v for k, v in new.items() if k not in out})
    text_out = yaml.dump(out, Dumper=_Dumper, sort_keys=False, default_flow_style=False, allow_unicode=True, width=120, indent=2)
    return Result(name, text_out, notes, nets, chip or "", str(board or (new.get(chip) or {}).get("board", "")))


def write_secrets(path: Path, nets: list[tuple[str, str]]) -> None:
    """Voegt de netwerken toe aan secrets.yaml; bestaande sleutels blijven staan."""
    cur: dict = {}
    if path.exists():
        cur = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for s, p in nets:
        a, b = secret_keys(s)
        cur.setdefault(a, s)
        cur.setdefault(b, p)
    path.write_text(yaml.safe_dump(cur, allow_unicode=True, sort_keys=True), encoding="utf-8")
