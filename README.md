# Huisconfig: kruisschakelaars over ESP-NOW en meer

Lokale verlichtingsbesturing met ESP-NOW tussen ESP8266/ESP32-nodes, Home Assistant als viewer en configurator.
Ontwerp: [DESIGN.md](DESIGN.md).

```
core/include/kk/   protocol, flood, lichtlogica, kanaalvolging (portable C++17, geen heap)
core/src/
sim/               discrete-event simulator en tests
third_party/       Monocypher 4.0.2 (publiek domein): XChaCha20-Poly1305
tools/test.sh      draait de tests in containers (kern, add-on, end-to-end)
addon/             Home Assistant add-on (Python): UDP-peer, MQTT-discovery, web-UI, leer-modus
e2e/               docker compose: simulator + add-on + MQTT-broker
```

## Tests

```sh
tools/test.sh          # alles (unit + scenario's, ~10 s)
tools/test.sh unit
tools/test.sh quick    # kortere scenario's
tools/test.sh sweep    # parameter-sweep van de flood
tools/test.sh probe    # slaagkans per enkele poging, slechtste paden
```

Op Linux/macOS met g++: `make test`.
Vereist op Windows: Docker Desktop (de scripts gebruiken `gcc:14`).

## Add-on

```sh
tools/test.sh addon    # Python-tests + interop met de C++-kern (golden vectors)
tools/test.sh e2e      # simulator (echte UDP) + add-on + Mosquitto, ~1 min
tools/demo.sh          # gesimuleerd huis met de web-UI op http://localhost:8099
python tools/simctl.py "press 1 0"   # een knop indrukken in het demo-huis
tools/demo.sh down
```

In het demo-huis staat ook een nagebootste Home Assistant voor externe apparaten (Tuya): een fysieke druk op een gang
simuleer je met `curl -X POST localhost:8123/ctl/press -d '{"entity":"switch.t_0"}'`.

Gebruik en opties: [addon/DOCS.md](addon/DOCS.md). Ontwerp en resultaten: DESIGN.md §9 en §16.

## Installeren op Home Assistant

1. Instellingen → Apps → App store → ⋮ → *Repositories* → voeg `https://github.com/jorissynaeve/valevinagre_kruisschakelaar` toe.
2. Zoek *Huisconfig* (eventueel eerst ⋮ → *Op updates controleren*), installeer en start de app.
3. Na een nieuwe versie op GitHub: ⋮ → *Op updates controleren* (soms twee keer) en dan *Bijwerken*. HA bouwt alleen opnieuw als `version` in `addon/config.yaml` hoger is.

Gebouwd en gestart op een Raspberry Pi 3 (aarch64). De app vindt Mosquitto en de HA-API zelf via de Supervisor.
