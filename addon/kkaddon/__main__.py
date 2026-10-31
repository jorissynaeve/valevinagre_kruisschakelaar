"""Start de add-on:  python -m kkaddon            (draaien)
                    python -m kkaddon genkey     (nieuwe netwerksleutel + inhoud voor secrets.h)
                    python -m kkaddon secrets    (huidige sleutel als secrets.h)
"""
from __future__ import annotations

import asyncio
import logging
import secrets as _secrets
import signal
import sys

from aiohttp import web

from . import __version__, settings as cfg
from .engine import Engine
from .habridge import HaBridge
from .model import Model
from .mqttbridge import MqttBridge
from .transport import UdpTransport
from .web import Ctx, make_app

log = logging.getLogger("kk")


async def run() -> None:
    s = cfg.load()
    logging.basicConfig(level=s.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("Huisconfig add-on %s, peer-id 0x%04x", __version__, s.peer_id)
    if s.key_generated:
        log.warning("Nieuwe netwerksleutel gemaakt in %s/network.key. Zet hem in firmware/secrets.h (zie het tabblad Instellingen).", s.data_dir)

    model = Model(s.data_dir / "network.json")
    transport = UdpTransport(s.udp_port, s.broadcast_addr, s.peers, s.hub)
    engine = Engine(s.key, model, transport.send, peer_id=s.peer_id, state_path=s.data_dir / "state.json")
    transport.on_frame = engine.on_datagram
    engine.relay_cb = transport.relay
    await transport.start()

    mqtt = MqttBridge(s, engine)
    ha = HaBridge(s.ha_url, s.ha_token, engine)
    ctx = Ctx(s, engine, transport, mqtt, ha)
    runner = web.AppRunner(make_app(ctx), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", s.web_port).start()
    log.info("web-UI op :%d%s", s.web_port, " (dev_mode: zonder ingress-controle)" if s.dev_mode else "")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    async def ticker() -> None:
        while True:
            engine.tick()
            await asyncio.sleep(0.01)

    tasks = [asyncio.create_task(ticker()), asyncio.create_task(mqtt.run()), asyncio.create_task(ha.run())]
    await stop.wait()
    log.info("afsluiten")
    for t in tasks:
        t.cancel()
    engine.close()  # zet het leren uit en bewaart de staat
    await asyncio.sleep(0.2)  # laat de Learn(uit)-frames nog weggaan
    transport.close()
    await runner.cleanup()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "genkey":
        print(cfg.key_header(_secrets.token_bytes(cfg.KEY_LEN)), end="")
    elif cmd == "secrets":
        print(cfg.key_header(cfg.load().key), end="")
    elif cmd == "run":
        asyncio.run(run())
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
