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
from .chargehub import ChargeHub
from .engine import Engine
from .greehub import GreeHub
from .habridge import HaBridge
from .hasphub import HaspHub
from .linkhub import LinkHub
from .energyhub import EnergyHub
from .remotesensors import RemoteSensors
from .solarhub import SolarHub
from .espdevhub import EspDevHub
from .model import Model
from .mqttbridge import MqttBridge
from .panelhub import PanelHub
from .points import Points
from .transport import UdpTransport
from .tuyahub import TuyaHub
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
    ha = HaBridge(s.ha_url, s.ha_token, engine, dashboard=s.ha_dashboard)
    panels = PanelHub(s.data_dir, ha, engine, port=s.panel_port, panel_port=s.panel_remote_port)
    mqtt.extras.append(panels)
    panels.changed = mqtt.resync
    await panels.start()
    screens = HaspHub(ha, engine)
    mqtt.extras.append(screens)
    screens.changed = mqtt.resync
    screens.publish = mqtt.publish
    tuya = TuyaHub(s.data_dir, engine)
    panels.tuya = tuya  # de laadpaalkaart leest en stuurt de lader rechtstreeks
    links = LinkHub(s.data_dir, engine, port=s.link_port)
    links.tuya = tuya
    links.attach_ha(ha)  # de gangen van deze installatie: hun stand en ze schakelen voor een andere
    engine.points = Points(tuya, links)  # Tuya-datapunten voor de logica, de routines en de kaarten, hier of bij een andere installatie
    panels.linkhub = links  # kaarten met een lamp of lader van een andere installatie
    screens.tuya, screens.linkhub = tuya, links  # dezelfde kaarten op de schermen met openHASP
    mqtt.extras.append(tuya)
    tuya.changed = mqtt.resync
    tuya.start()
    charge = ChargeHub(s.data_dir, engine, tuya)  # laadpalen met sessies, een eigen blok (k:<id>) bovenop een Tuya-toestel
    charge.state, charge.home, charge.peer_name = ha.states.get, lambda: links.me["name"] or "Dit huis", links.peer_name
    links.charge = charge
    panels.charge, screens.charge = charge, charge
    mqtt.extras.append(charge)
    charge.changed = mqtt.resync
    charge.adopt(links)  # de laadpaalkaarten van voor 0.28 omzetten naar een blok (eenmalig)
    airco = GreeHub(s.data_dir, engine)  # airco's met een Gree-module, een eigen blok (w:<id>) los van Tuya
    mqtt.extras.append(airco)
    panels.airco = airco  # de airco-kaart op het paneel
    ha.airco_names = lambda: {did: dev["name"] for did, dev in airco.devices.items()}

    def airco_changed() -> None:
        mqtt.resync()
        ha.refresh_dashboard()  # de tab Airco volgt toevoegen, verwijderen en hernoemen

    airco.changed = airco_changed
    airco.start()
    solar = SolarHub(s.data_dir, engine)  # omvormers met een Solarman-stick, een eigen blok (o:<id>)
    mqtt.extras.append(solar)
    solar.changed = mqtt.resync
    solar.start()
    espdev = EspDevHub(s.data_dir, engine)  # zelfbouwapparaten met het HUISDEV-protocol, een eigen blok (e:<id>)
    mqtt.extras.append(espdev)
    espdev.changed = mqtt.resync
    espdev.start()
    remote = RemoteSensors(s.data_dir, links)  # de kWh-tellers van gekoppelde Tuya-toestellen als sensoren in HA, voor het energiedashboard
    mqtt.extras.append(remote)
    links.changed = mqtt.resync
    energy = EnergyHub(s.data_dir, ha)  # het energiedashboard van HA invullen vanuit de meters
    await links.start()
    ctx = Ctx(s, engine, transport, mqtt, ha, panels, tuya, links, airco, solar, energy, charge, espdev)
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

    async def charging() -> None:
        while True:
            try:
                charge.tick()
            except Exception:  # noqa: BLE001 - een fout in één ronde mag de sessies niet voorgoed stilleggen
                log.exception("laadsessies")
            await asyncio.sleep(1.0)

    tasks = [asyncio.create_task(ticker()), asyncio.create_task(mqtt.run()), asyncio.create_task(ha.run()), asyncio.create_task(panels.run()), asyncio.create_task(screens.run()), asyncio.create_task(links.run()), asyncio.create_task(charging())]
    await stop.wait()
    log.info("afsluiten")
    for t in tasks:
        t.cancel()
    panels.close()
    screens.close()
    tuya.close()
    airco.close()
    solar.close()
    espdev.close()
    await links.close()
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
