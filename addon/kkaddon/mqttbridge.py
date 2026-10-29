"""MQTT-client: publiceert discovery en toestanden en vertaalt HA-commando's naar CMD_LIGHT."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

import aiohttp
import aiomqtt

from . import discovery as d
from .engine import Engine
from .settings import Settings

log = logging.getLogger("kk.mqtt")

SET_RE = re.compile(rf"^{d.PREFIX}/light/(\d+)/(\d+)/set$")


async def supervisor_broker() -> dict | None:
    """Vraag de supervisor naar de MQTT-broker (Mosquitto-add-on of een externe, zoals HA hem kent)."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get("http://supervisor/services/mqtt", headers={"Authorization": f"Bearer {token}"}) as r:
                if r.status != 200:
                    return None
                return (await r.json()).get("data")
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        log.warning("supervisor gaf geen MQTT-gegevens: %s", e)
        return None


class MqttBridge:
    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.s = settings
        self.engine = engine
        self.connected = False
        self.last_error = ""
        self.enabled = True
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._published: dict[str, str] = {}  # discovery-topic -> payload dat nu retained staat
        self._state_sent: dict[str, str] = {}
        self._resync = asyncio.Event()
        engine.subscribe(self._on_event)

    # ---- engine -> MQTT

    def _on_event(self, e: dict) -> None:
        if not self.connected:
            return
        t = e["t"]
        if t == "light":
            self._state(d.t_light_state(e["node"], e["relay"]), "ON" if e["on"] else "OFF")
        elif t == "btn":
            self._put(d.t_button(e["node"], e["btn"]), json.dumps({"event_type": "press"}), False)
        elif t == "node" and e["what"] in ("online", "offline"):
            self._state(d.t_node_status(e["node"]), e["what"])
        elif t in ("node", "model"):
            self._resync.set()
        elif t == "cfg":
            info = self.engine.nodes.get(e["node"])
            if info:
                self._state(d.t_cfg_problem(e["node"]), "ON" if info.cfg_status in d.CFG_PROBLEM else "OFF")

    def _put(self, topic: str, payload: str, retain: bool) -> None:
        try:
            self._queue.put_nowait((topic, payload, retain))
        except asyncio.QueueFull:
            log.warning("MQTT-wachtrij vol, bericht voor %s overgeslagen", topic)

    def _state(self, topic: str, payload: str) -> None:
        if self._state_sent.get(topic) != payload:
            self._state_sent[topic] = payload
            self._put(topic, payload, True)

    def _sync_discovery(self, force: bool = False) -> None:
        """Publiceert wat veranderd is en haalt verdwenen entiteiten weg. Daarna de retained toestanden."""
        if force:
            self._published.clear()
            self._state_sent.clear()
        want = {t: d.encode(c) for t, c in d.configs(self.engine).items()}
        for t, payload in want.items():
            if self._published.get(t) != payload:
                self._put(t, payload, True)
        for t in set(self._published) - set(want):
            self._put(t, "", True)  # lege retained payload verwijdert de entiteit in HA
        self._published = want
        for t, (payload, _) in d.states(self.engine).items():
            self._state(t, payload)

    # ---- verbinding

    async def _broker(self) -> dict | None:
        if self.s.mqtt_host:
            return {"host": self.s.mqtt_host, "port": self.s.mqtt_port, "username": self.s.mqtt_user, "password": self.s.mqtt_password}
        return await supervisor_broker()

    async def run(self) -> None:
        delay = 2.0
        while True:
            broker = await self._broker()
            if not broker:
                if self.enabled:
                    log.warning("geen MQTT-broker gevonden (installeer Mosquitto of vul mqtt_host in); nieuwe poging over 60 s")
                self.enabled = False
                await asyncio.sleep(60)
                continue
            self.enabled = True
            try:
                await self._session(broker)
                delay = 2.0
            except aiomqtt.MqttError as e:
                self.last_error = str(e)
                log.warning("MQTT-verbinding weg: %s", e)
            except asyncio.CancelledError:
                raise
            finally:
                self.connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _session(self, broker: dict) -> None:
        async with aiomqtt.Client(
            hostname=broker["host"],
            port=int(broker.get("port", 1883)),
            username=broker.get("username") or None,
            password=broker.get("password") or None,
            identifier=f"kruisschakelaars-{self.engine.id:x}",
            will=aiomqtt.Will(d.BRIDGE_STATUS, "offline", retain=True),
            keepalive=30,
        ) as client:
            await client.publish(d.BRIDGE_STATUS, "online", retain=True)
            await client.subscribe(d.LIGHT_SET_FILTER)
            await client.subscribe(f"{d.DISCOVERY}/status")
            while not self._queue.empty():
                self._queue.get_nowait()
            self.connected = True
            self.last_error = ""
            self._sync_discovery(force=True)
            log.info("MQTT verbonden met %s", broker["host"])
            tasks = [asyncio.create_task(self._publisher(client)), asyncio.create_task(self._receiver(client))]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    t.result()
            finally:
                for t in tasks:
                    t.cancel()
                self.connected = False

    async def _publisher(self, client: aiomqtt.Client) -> None:
        while True:
            try:
                topic, payload, retain = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                topic = None
            if self._resync.is_set():  # wijzigingen worden zo vanzelf gebundeld
                self._resync.clear()
                self._sync_discovery()
            if topic is not None:
                await client.publish(topic, payload, retain=retain)

    async def _receiver(self, client: aiomqtt.Client) -> None:
        async for msg in client.messages:
            topic = str(msg.topic)
            payload = msg.payload.decode("utf-8", "replace").strip() if isinstance(msg.payload, (bytes, bytearray)) else ""
            if topic == f"{d.DISCOVERY}/status":
                if payload == "online":  # HA is herstart: alles opnieuw aanbieden
                    log.info("HA is online: discovery opnieuw publiceren")
                    self._sync_discovery(force=True)
                continue
            m = SET_RE.match(topic)
            if not m:
                continue
            node, relay = int(m.group(1)), int(m.group(2))
            up = payload.upper()
            if up not in ("ON", "OFF"):
                log.warning("onbekend commando %r voor %s", payload, topic)
                continue
            if not self.engine.command_light(node, relay, up == "ON"):
                log.warning("commando voor onbekend licht %d/%d", node, relay)
