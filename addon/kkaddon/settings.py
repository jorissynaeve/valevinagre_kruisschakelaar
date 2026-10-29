"""Instellingen: HA schrijft de add-on-opties naar /data/options.json; omgevingsvariabelen (KK_*) gaan voor,
zodat dezelfde image ook buiten HA draait (tests, docker compose)."""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .engine import PEER_ID_MIN
from .proto import KEY_LEN

DEFAULT_PORT = 47474
WEB_PORT = 8099  # ingress_port in config.yaml


@dataclass
class Settings:
    data_dir: Path
    key: bytes
    key_generated: bool
    peer_id: int = PEER_ID_MIN
    udp_port: int = DEFAULT_PORT
    broadcast_addr: str = "255.255.255.255"
    peers: list[str] = field(default_factory=list)  # extra unicast-bestemmingen: "host" of "host:poort"
    hub: bool = True  # frames van de ene unicast-peer doorgeven aan de andere
    mqtt_host: str = ""  # leeg: vraag HA (supervisor) naar de broker
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    ha_url: str = ""  # websocket van HA; leeg + SUPERVISOR_TOKEN: de supervisor-proxy
    ha_token: str = ""
    web_port: int = WEB_PORT
    dev_mode: bool = False  # web-UI ook buiten HA-ingress toegankelijk
    log_level: str = "info"

    @property
    def key_hex(self) -> str:
        return self.key.hex()


def parse_key(text: str) -> bytes:
    text = text.strip().lower().removeprefix("0x")
    try:
        key = bytes.fromhex(text)
    except ValueError as e:
        raise ValueError("network_key moet hexadecimaal zijn") from e
    if len(key) != KEY_LEN:
        raise ValueError(f"network_key moet {KEY_LEN * 2} hex-tekens zijn (32 bytes), niet {len(text)}")
    return key


def key_header(key: bytes) -> str:
    """Inhoud voor firmware/secrets.h (gitignored)."""
    body = ", ".join(f"0x{b:02x}" for b in key)
    return f"// Netwerksleutel van dit huis. Niet delen, niet committen.\n#define KK_NETWORK_KEY {{ {body} }}\n"


S6_ENV_DIRS = ("/run/s6/container_environment", "/var/run/s6/container_environment")


def supervisor_token(env: dict | None = None) -> str:
    """Het token van de supervisor. In de HA-basisimage start s6 onze dienst zonder de omgeving van de container;
    de waarde staat dan als bestand in de s6-omgevingsmap (daar leest ook bashio `with-contenv` hem)."""
    env = os.environ if env is None else env
    if env.get("SUPERVISOR_TOKEN"):
        return env["SUPERVISOR_TOKEN"]
    for d in ([env["KK_S6_ENV"]] if env.get("KK_S6_ENV") else list(S6_ENV_DIRS)):
        try:
            tok = (Path(d) / "SUPERVISOR_TOKEN").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if tok:
            return tok
    return ""


def _bool(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load(env: dict | None = None) -> Settings:
    env = os.environ if env is None else env
    data_dir = Path(env.get("KK_DATA", "/data"))
    opts: dict = {}
    op = Path(env.get("KK_OPTIONS", data_dir / "options.json"))
    if op.exists():
        opts = json.loads(op.read_text(encoding="utf-8"))

    def get(name, default=""):
        return env.get("KK_" + name.upper(), opts.get(name, default))

    key_text = str(get("network_key", "")).strip()
    generated = False
    keyfile = data_dir / "network.key"
    if key_text:
        key = parse_key(key_text)
    elif keyfile.exists():
        key = parse_key(keyfile.read_text())
    else:
        key = secrets.token_bytes(KEY_LEN)
        data_dir.mkdir(parents=True, exist_ok=True)
        keyfile.write_text(key.hex() + "\n")
        try:
            keyfile.chmod(0o600)
        except OSError:
            pass
        generated = True

    ha_url, ha_token = str(get("ha_url", "")), str(get("ha_token", ""))
    sup = supervisor_token(env)
    if not ha_url and sup:  # we draaien als add-on: de supervisor stuurt door naar HA
        ha_url, ha_token = "ws://supervisor/core/websocket", sup
    peers = get("peers", [])
    if isinstance(peers, str):
        peers = [p.strip() for p in peers.split(",") if p.strip()]
    return Settings(
        data_dir=data_dir,
        key=key,
        key_generated=generated,
        peer_id=int(get("peer_id", PEER_ID_MIN)),
        udp_port=int(get("udp_port", DEFAULT_PORT)),
        broadcast_addr=str(get("broadcast_addr", "255.255.255.255")),
        peers=list(peers),
        hub=_bool(get("hub", True)),
        mqtt_host=str(get("mqtt_host", "")),
        mqtt_port=int(get("mqtt_port", 1883)),
        mqtt_user=str(get("mqtt_user", "")),
        mqtt_password=str(get("mqtt_password", "")),
        ha_url=ha_url,
        ha_token=ha_token,
        web_port=int(get("web_port", WEB_PORT)),
        dev_mode=_bool(get("dev_mode", False)),
        log_level=str(get("log_level", "info")).lower(),
    )
