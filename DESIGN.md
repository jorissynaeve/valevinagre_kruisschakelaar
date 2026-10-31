# Kruisschakelaars over ESP-NOW — ontwerp (v0.1)

Status: ontwerp ter bespreking. Nog geen code.

## 1. Doel en uitgangspunten

- Alle verlichting wordt **lokaal in de nodes** aangestuurd. Home Assistant (HA) is viewer, configurator en OTA-server, en beslist niets over de verlichting.
- Elke node draait één eigen firmware (geen ESPHome): ESP-NOW-communicatie, OTA, persistente staat.
- Lichten blijven werken als wifi, router, broker of HA uitvallen.
- Een kruisschakelaar-netwerk is een verzameling knoppen (op verschillende nodes) die één licht (relais op één node) bedienen.
- Terugkoppeling: elke event heeft een bron, zodat zichtbaar is welke touch een relais schakelde.
- Na stroomuitval keert elk relais terug naar de laatste staat, meteen bij het opstarten.

Vastgelegde keuzes (uit bespreking):

| Onderwerp | Keuze |
|---|---|
| HA-koppeling | MQTT-discovery + eigen add-on met web-UI |
| HA bedienen | Ja, als *virtuele knop* (event in de mesh), geen staat-commando |
| OTA | ESP-NOW-trigger, download via router-wifi, wifi-gegevens niet bewaard op de node |
| Schaal | 12–30 nodes, ESP8266/ESP8285 (≥1 MB) en ESP32 |
| Kanaal | Mesh volgt het wifi-kanaal van de bridge, per *eiland* |
| Veraf (poolhuis) | Eigen eiland, via repeater op wifi, verbonden met de hoofdmesh via LAN-link |

## 2. Architectuur

```
            ┌──────── HA add-on (UI, config, OTA-server, MQTT-discovery) ────────┐
            │                         UDP (LAN-link)                              │
   ┌────────┴───────┐                                              ┌──────────────┴───┐
   │ Bridge A (ESP) │ ◄───── UDP broadcast / unicast, wifi ──────► │ Bridge B (ESP)   │
   │ eiland "huis"  │                                              │ eiland "poolhuis"│
   │ ESP-NOW ch=r   │                                              │ ESP-NOW ch=r'    │
   └──┬─────────┬───┘                                              └────────┬─────────┘
      │ ESP-NOW (flood)                                                      │ ESP-NOW
   node  node  node ...                                                   node ...
```

- **Link** = een medium waarover frames gaan: ESP-NOW (per eiland, op het kanaal van dat eiland) en UDP op het LAN.
- **Bridge** = een node die op wifi (STA) zit en frames tussen ESP-NOW en UDP doorgeeft. Elke bridge zit vast op het kanaal van zijn AP (router of repeater). Elk eiland heeft dus zijn eigen kanaal. De poolhuis-repeater kan een ander kanaal hebben dan de router en dat is geen probleem.
- Het frameformaat is overal gelijk. Floodregels (dedup, TTL) gelden over alle links, dus de LAN is gewoon "nog een hop".
- **HA add-on** is zelf een UDP-peer in de flood. Een apart USB-dongle is niet nodig.
- Eén bridge per eiland is genoeg. Meer bridges mogen en dedup voorkomt lussen.

## 3. Identiteit en endpoints

- `node_id`: 16-bit kort adres, toegekend door de add-on bij adoptie. Het MAC-adres staat in HELLO-frames.
- `endpoint` = `(node_id, index)`. Elke node heeft knoppen `b0..bn` en relais `r0..rm`.
- Een **licht** is een relais-endpoint. De **eigenaar** is de node waarop dat relais zit (schakelaar of relaisbox).
- Namen, kamers en groepen staan alleen in HA, niet op de nodes.

## 4. Frameformaat

ESP-NOW v1 geeft 250 bytes. Doel: ≤ 200 bytes.

```
Header (niet versleuteld, geauthenticeerd als AD):
  ver:1  type:1  src:2  boot:2  ctr:2        # (src,boot,ctr) = unieke frame-id
Mutabel (niet in MAC): ttl:1                 # wordt bij doorsturen verlaagd
Payload: ChaCha20-Poly1305 (16 B tag), nonce = src|boot|ctr
```

- `boot` is een teller die bij elke start in flash met 1 omhoog gaat (replay-bescherming en uniek frame-id).
- Eén gedeelde netwerksleutel (128/256 bit), **bij compile-time** in de firmware gezet via een gitignored `secrets`-bestand. Alle nodes draaien dezelfde image. Rotatie vereist een OTA-overgang met twee sleutels.
- Alle nodes verifiëren het tag voordat ze doorsturen, zodat vreemde zenders de flood niet kunnen vullen.
- Eén cryptoprimitive (ChaCha20-Poly1305 of vergelijkbaar, bv. Monocypher) voor frames, OTA-wifi-gegevens en firmwarehandtekening (Ed25519). Past op ESP8266 (~10 KB).

### Berichttypen

| Type | Inhoud |
|---|---|
| `BTN_EVENT` | knop, actie (nu alleen press), `evt` (8 bit, per knop, voor herhalingen) |
| `LIGHT_STATE` | relais, aan/uit, `ver`, `cause = (bron, knop, evt)` |
| `HEARTBEAT` | fw-versie, vlaggen, alle relaisstaten met `ver`, daarna (achteraan, optioneel voor lezers) aantal knoppen, aantal indicatoren en de **config-hash** (CRC-16 over triggers en volg-koppelingen, zie `core/include/kk/cfghash.h`) |
| `PROBE` / `PROBE_RESP` | kanaalzoektocht en provisioning, een hop, nooit over de LAN |
| `BEACON` | bridge: "ik zit op kanaal X, epoch E", elke 10 s, floodt binnen het eiland, nooit over de LAN |
| `HELLO` | niet-geadopteerde node meldt zich (MAC, model, fw) |
| `CFG_SET` / `CFG_ACK` | configuratie, gefragmenteerd, geadresseerd aan één node |
| `LEARN` | leer-modus aan/uit (onderdrukt actuatie) |
| `IDENTIFY` | LED laten knipperen |
| `OTA_CMD` / `OTA_STATUS` | zie §8 |
| `CMD_LIGHT` | HA of bridge: relais `r` van node `n` aan, uit of toggle (de "virtuele knop"); wordt bevestigd met `LIGHT_STATE` |
| `ANNOUNCE` | "ik verhuis naar kanaal X, epoch E", een hop, op het oude kanaal |

## 5. Flood-regels (de "mesh")

Waarden hieronder komen uit de simulatie (§15) en staan in `Tuning` in `core/include/kk/config.h`.

- **Dedup en replay**: per bron een schuivend venster van 64 frame-sleutels (`boot<<16 | ctr`), tot 48 bronnen. Eigen frames worden bij het uitzenden meteen geregistreerd.
- **Doorsturen**: TTL 4. Willekeurige vertraging 2–15 ms. Wordt hetzelfde frame in die tijd al **3×** van anderen gehoord, dan stuurt de node het niet door (gossip-suppressie). Voor `HEARTBEAT` en `BEACON` is dat **1×**: zuiniger, want die komen toch regelmatig terug. K=2 gaf in de simulatie 88–95% slagen op de zwakste paden, K=3 98%.
- **Betrouwbaarheid bij een knopdruk** (broadcast heeft geen ACK):
  1. De afzender zendt het `BTN_EVENT` 2× uit (zelfde frame, 5–14 ms tussenruimte).
  2. De touch kent zijn licht al (de indicator die die knop volgt). Wijzigt dat licht niet binnen 50 ms van staat, dan herhaalt de afzender het event als **nieuw frame** met hetzelfde `evt`-nummer. Tot 5 pogingen met verdubbelende wachttijd (50, 100, 200, 400, 800 ms).
  3. De eigenaar dedupliceert op `(bron, boot, knop, evt)`, dus een herhaling schakelt nooit twee keer. Het is een nieuw frame, dus forwarders sturen het gewoon door, ook als ze de eerste poging al hadden onderdrukt.
  4. De bevestiging is een `LIGHT_STATE` met `cause = (bron, knop, evt)`. Een gelijktijdige druk van een andere schakelaar telt dus niet als bevestiging van jouw event. Ontvangt de eigenaar een herhaling die hij al verwerkte, dan zendt hij de bevestiging opnieuw.
- **Zelfherstel**: `HEARTBEAT` elke 40–80 s (met jitter) en na een eigen staatswijziging nog 2 snelle heartbeats (6–12 s). Gemiste `LIGHT_STATE`-frames en LED's worden zo vanzelf rechtgezet zonder het ruststroomverkeer te verhogen.
- **Debounce**: events van dezelfde knop binnen 150 ms worden genegeerd.

## 6. Lichtlogica (eigenaar-gebaseerd)

De eigenaar-node heeft per relais een **triggerlijst**: `[(endpoint, modus)]` met modus `toggle | on | off`.

1. Een `BTN_EVENT` of `CMD_LIGHT` komt binnen. Staat de bron in de triggerlijst en is de event nieuw (dedup)? Dan voert de eigenaar de modus uit.
2. Relais schakelt, `ver++`, nieuwe staat naar flash (wear-levelling, alleen bij wijziging), `LIGHT_STATE` uitgezonden met `cause`.
3. Knoppen op dezelfde node als het relais schakelen **lokaal en direct**, zonder netwerk. De `BTN_EVENT` wordt daarna toch uitgezonden zodat andere relais en HA het weten.
4. Een `LIGHT_STATE` triggert nooit nieuwe acties. Het is alleen informatie voor LED's en HA. Dit voorkomt feedback-lussen en het huidige "flikkeren".
5. Feedback op de touch zelf: de Sonoff T3 heeft geen aparte LED per gang (alleen status-LED op GPIO13). De gang-LED hangt vermoedelijk hardwarematig aan het relais (te verifiëren, zie §14). Een gang zonder aangesloten lamp kan dus als indicator dienen: de node laat dat relais de staat van het gekoppelde licht spiegelen (`mirror` in de config). Kost een relaisklik per wijziging.
6. Onderscheiden events (twee verschillende drukken) tellen beide, net als bij een echte kruisschakelaar.

## 7. Kanalen en eilanden

- Nodes onthouden hun kanaal en epoch in flash.
- De bridge volgt zijn wifi-AP. Wisselt de router van kanaal, dan verhoogt de bridge de **kanaal-epoch** en meldt dat. Zijn eiland is alleen de nodes in ESP-NOW-bereik op hetzelfde kanaal. Het poolhuis is daarom gewoon een eiland met zijn eigen kanaal en epoch.
- **Aankondiging op het oude kanaal** (als de bridge dat nog kan): `ANNOUNCE(chan, epoch)` 3×. Elke node die een hogere epoch op een ander kanaal hoort, kondigt zelf aan en schakelt na 14 ms om. Zo trekt het hele eiland in ~0,1 s mee (simulatie).
- **Zonder aankondiging** (de bridge zit al op het nieuwe kanaal): elke node weet dat er een bridge hoort te zijn omdat hij `BEACON`s hoort. Zijn die > 40 s weg, dan start hij een **actieve scan**: op elk van de 13 kanalen een `PROBE` (2×, 40 ms per kanaal). Het antwoord (`PROBE_RESP`) bevat kanaal, epoch en een bridge-vlag. Hij kiest de hoogste epoch. Vindt hij de bridge niet, dan probeert hij snel opnieuw (3–8 s, 5×) en daarna met steeds langere pauzes (tot 10 min), zodat een echt uitgevallen bridge het eiland niet blijft belasten.
- Een net opgestarte node die binnen 45 s geen beacon hoort, scant één keer. Hoort hij helemaal niets, dan scant hij na 150 s.
- Beacon, probe en announce gaan **nooit over de LAN**: een eiland mag zijn kanaal niet door verkeer van een ander eiland laten veranderen (getest).
- Tijdens een kanaalwissel blijven lichten werken binnen elke partitie. HA ziet het eiland tijdelijk niet.
- Aanbeveling blijft: zet het routerkanaal vast. Het protocol is dan alleen nog herstel.
- Elk eiland heeft ≥ 1 bridge. De bridge zet elk (niet link-lokaal) frame 2× op de LAN-link (UDP), de ontvangende bridge dedupliceert.

## 8. OTA

1. De add-on bewaart de firmware (`.bin`, signed met Ed25519, sha256, versie). Hij serveert die via HTTP op het LAN.
2. HA stuurt `OTA_CMD` (geadresseerd) via de flood: url, grootte, sha256, handtekening. De wifi-SSID/wachtwoord zitten versleuteld in het commando en worden niet bewaard.
3. De node zet zijn relaisstaat vast, verbindt met wifi (scant alle kanalen), downloadt, controleert sha256 en handtekening, flasht en herstart. Bij falen keert hij terug naar het mesh-kanaal en meldt `OTA_STATUS`.
4. Rollout in stappen: 1–3 nodes tegelijk, 's nachts, bridge als laatste. Een node verwerkt tijdens OTA geen events.
5. Veiligheid: de firmware **moet altijd** OTA kunnen. ESP32 gebruikt rollback met `app validate` na mesh-join. ESP8266 heeft geen rollback, dus een CI-controle dat elke build de OTA-code bevat, en een kabelnoodpad.
6. Geheugen: op 1 MB-modules moet de image onder ~480 KB blijven (geen TLS, minimale libs). Build-guardrail in CI.

## 9. HA add-on

Gebouwd, zie §16. Code in `addon/` (Python 3.12, asyncio, aiohttp, PyNaCl, aiomqtt).

- **UDP-peer** (host-network): elk frame is een los datagram naar het broadcast-adres (poort 47474) en naar vaste unicast-peers. Een vaste peer is de fallback voor een bridge achter een repeater die broadcast filtert; de add-on geeft frames tussen vaste peers door (`hub`). De add-on forwardt verder niets: bridges praten onderling via de LAN.
- **MQTT-discovery**: `light` per licht in het model, `event`-entiteit per knop (HA ziet welke schakelaar werd ingedrukt), diagnostiek (fw, "configuratie wijkt af"). Beschikbaarheid per node via een retained status-topic; een lichtstand wordt alleen gepubliceerd als de node hem gemeld heeft.
- **Web-UI (ingress, poort 8099)**: lichten met leer-dialoog, nodes (naam, kamer, configstatus, LED laten knipperen, config opnieuw sturen, indicatoren), live logboek met oorzaak per wijziging, instellingen (bridges, `secrets.h`, export/import). Alleen de ingress-proxy (172.30.32.2) mag erbij.
- **Leer-modus**: de add-on stuurt `LEARN` (vernieuwd elke 25 s, nodes laten na 120 s los), toont gedetecteerde `(node, knop)` live, en slaat op als triggers van het doellicht. De indicator van een touch volgt automatisch het licht dat die knop bedient (indicator i hoort bij knop i), tenzij handmatig anders gekozen.
- **Configuratie naar de nodes**: per relais een `CFG_SET` met de triggerlijst, per indicator een `CFG_SET` met het gevolgde licht. Alleen wat veranderd is wordt gestuurd; retries als nieuw frame op `CFG_ACK`.
- **Zelfherstel**: de heartbeat bevat de config-hash. Wijkt die af van wat het model voorschrijft, dan stuurt de add-on alles opnieuw (wachttijd 30 s, verdubbelend tot 10 min). Alleen bij nodes die aan een licht gekoppeld ("geadopteerd") zijn, zodat een verloren model (lege `/data`) nooit de werkende nodes leegmaakt.
- **Externe apparaten** (schakelaars die niet te flashen zijn, bv. Tuya): zie §17.
- Opdrachten naar lichten zijn altijd **absoluut** (aan of uit), nooit toggle: een herhaling als nieuw frame is dan idempotent.

## 10. Firmware

- PlatformIO, Arduino-framework, één codebasis voor ESP8266/8285 en ESP32.
- Lagen: `core/` (frame, crypto, dedup, flood, kanaal, licht, config, OTA) en `hal/` per bord (pinmap, aantal gangs, LED/relais-polariteit).
- Borden: zie de hardwareprofielen in §14. Pinouts komen uit de bestaande ESPHome-YAML's.
- Opslag: LittleFS voor config en laatste staat. Schrijf alleen bij wijziging.
- Relais bij opstart: eerst de relaisstaat herstellen, daarna pas wifi/mesh.
- Tests: native unit-tests voor protocol en flood, plus een simulator (30 nodes, verlies, vertraging) vóór hardware.

## 11. Fasering

| Fase | Inhoud |
|---|---|
| F0 | Inventaris, `esptool flash_id` per node, pinouts uit de ESPHome-configs |
| F1 ✅ | Kernprotocol, simulator, native tests (zie §15) |
| F2 | Prototype: 1 bridge + 2 nodes: toggle, LED-feedback, persistentie |
| F2b | **Vroege test** van de poolhuis-repeater: UDP broadcast/multicast en ESP-NOW over de repeater |
| F3 ✅ | Add-on: UDP-peer, MQTT-discovery, live events (zie §16) |
| F4 ✅ | Leer-modus en configuratie, inclusief zelfherstel via de config-hash (zie §16) |
| F3b ✅ | Externe apparaten (Tuya via local-tuya in HA) als virtuele node (zie §17) |
| F5 | OTA-hybride en signing |
| F6 | Kanaal-volgen en eilanden |
| F3c ✅ | Plattegrond in de add-on: kamers tekenen, schakelaars en lampen plaatsen, kringen schematisch tonen (zie §19) |
| F7b ✅ | Entiteitskringen: de oude AppDaemon-kruisschakelaars importeren en per kring aan/uit zetten (zie §20) |
| F7a ✅ | Bestaande ESPHome-schakelaars tijdelijk als extern apparaat in de add-on (zie §18) |
| F3d ✅ | Blokkenschema per ruimte, zoals Loxone Config (zie §21) |
| F7 | Migratie per kring: toestellen om te flashen, met ESPHome-YAML als terugvalmogelijkheid |

## 12. Risico's en punten om vroeg te testen

- Repeaters kunnen multicast/broadcast filteren of vertragen. Fallback: unicast naar de bekende bridge-lijst.
- ESP8266: STA + ESP-NOW op hetzelfde kanaal, en modem-sleep uit voor betrouwbare ontvangst.
- Relais-glitch bij opstart: pins moeten vroeg in de goede staat.
- Radiobereik in metalen inbouwdozen.
- Eén bridge per eiland is een single point of failure voor HA, niet voor de lichten.
- Kanaal-overgang kan tijdelijk partities geven. Goed testen in de simulator.
- Sleutelrotatie en een verloren OTA-handtekeningsleutel: back-up van de add-on `/data`.

## 13. Open vragen

- Exacte Sonoff-modellen en flashgroottes (F0).
- Welke nodes zijn in het poolhuis en hoeveel (voor eiland-indeling).
- Gewenst gedrag bij gelijktijdige drukken op dezelfde kring (nu: beide tellen).
- Lange druk of dim-functies nodig, of alleen aan/uit?
- Welke van de ~85 configs zijn effectief geïnstalleerd, per huis? Met welk huis starten we het prototype?
- Hardware-LED per gang op de T3: los aanstuurbaar van het relais, of niet (test op een toestel)?
- Beslist: elk huis eigen HA/mesh (geen koppeling), mini R2 voorlopig genegeerd, t4b in scope.

## 14. Inventaris uit de ESPHome-repo (github.com/jorissynaeve/esphome_projects)

Geanalyseerd op 127 bestanden. Alleen schakelaars en relaisboxen zijn relevant.

### Hardwareprofielen (HAL)

| Profiel | Bestanden | Chip/board in ESPHome | Knoppen | Relais | LED |
|---|---|---|---|---|---|
| `sonoff_t3` (3-gang touch) | touch01–12, 101–112, 201–212, 301–312, t3h01–03, t3k01–03, t3s01–02, t3st01–02, sonoffT3 | ESP8285, `esp01_1m` | GPIO0, 9, 10 | GPIO12, 5, 4 | status GPIO13 |
| `sonoff_4ch` | r4b01–04, R4-SP, R4-ZW, 4ch, 4ch2 | ESP8285, `esp01_1m` | GPIO0, 9, 10, 14 | GPIO12, 5, 4, 15 | blauw GPIO13 |
| `sonoff_basic` / `s55` | sb01, sb101, sb201, sb301, sbvv01, sbvv02, S55-001 | ESP8285 | GPIO0 | GPIO12 | status GPIO13 |
| `sonoff_mini_r2` (voorlopig genegeerd) | minir2Vale01–04 | ESP8285 | GPIO0 (de externe schakelaaringang is op een Mini R2 GPIO4, nu ongebruikt) | GPIO12 | status GPIO13 |
| `tuya_mcu_4ch` | t4b001–012 | ESP8266 `esp01_1m`, UART 9600 GPIO1/3 | geen (zit in de MCU) | via Tuya-datapunten 1–4 | in MCU |
| `r16_595` (relaisbox) | r16b01 | ESP8266 `nodemcuv2`, 2× 74HC595 (data 14, clock 13, latch 12, OE 5) | geen | 16 via shiftregister | geen |

Buiten scope (geen verlichting): RFBridge, temperatuur/DHT/Dallas (th10*), Cresta, WNK80MA, lsc (BK7231N), gardena4ch, isentrol, powerplug* (stekkers), jonasPoortRelais (ESP32-C3, 1 relais voor een poort, optioneel later), tuya1/tuya3stest (Tuya-MCU-touch, mogelijk later).

### Bevindingen

1. **Flash**: alle Sonoffs zijn ESP8285 met 1 MB. De OTA-limiet van ~480 KB is dus een harde eis. R16 is een NodeMCU, waarschijnlijk 4 MB.
2. **Eén pinmap voor alle T3's en alle 4CH's** (de 48 `touchNNN`-bestanden verschillen alleen in naam en SSID). Eén firmware-profiel dekt dus tientallen toestellen.
3. **Meerdere huizen**: beslist dat elk huis een eigen HA en een eigen mesh heeft, zonder communicatie ertussen. Elk huis wordt met een eigen netwerksleutel gebouwd. Frames van een ander huis falen op het AEAD-tag en worden genegeerd, dus een `net_id` is niet nodig. Eén repo, één codebasis, een `secrets`-bestand per huis.
4. **Tuya-MCU-toestellen** (t4b001–012): in scope, ze zitten in kruisschakelnetwerken. De ESP is alleen de radiomodule. Relais en knoppen zitten in de MCU. We zien enkel staatswijzigingen per datapunt. Een niet-opgedragen staatswijziging is een lokale knopdruk en wordt als `BTN_EVENT` uitgezonden. De MCU schakelt zijn eigen relais zelf. Een echo van onze eigen opdracht herkennen we aan de verwachte staat. (Andere Tuya-bestanden, zoals tuya1/tuya3stest, blijven buiten scope.)
5. **Gang-LED op T3**: de LED moet de staat van de bediende lamp tonen, ook als de gang zelf geen lamp schakelt. Er is maar één bedrade LED (GPIO13, status). De gang-LED hangt vermoedelijk hardwarematig aan het relais. Dan is spiegelen via het relais de enige manier (`mirror` in de config, alleen voor gangen zonder lamp en alleen als nodig). Eerst verifiëren op een toestel of de LED los van het relais aanstuurbaar is.
6. **Geheimen in de repo**: de configs bevatten wifi-wachtwoorden in platte tekst en de repo is openbaar. Wachtwoorden roteren en de geschiedenis opschonen. In de nieuwe opzet horen de netwerksleutels in een gitignored `secrets`-bestand, nooit in de repo.
7. Sommige bestanden zijn ouder (`arduino_version: 2.4.2`, `platform: ESP8266`) en `R4-SP`/`R4-ZW` hebben dezelfde pinmap als de r4b's.

## 15. F1: kern en simulator (gebouwd en getest)

Code: `core/` (protocol, flood, lichtlogica, kanaalvolging; geen heap, geen Arduino-afhankelijkheden), `sim/` (simulator en tests), `third_party/monocypher` (crypto, publiek domein).
Draaien: `tools/test.sh [unit|scen|quick|sweep|probe|all]` (docker met gcc, want Windows heeft hier geen C++-compiler). Op Linux/macOS: `make test`.

Simulatiemodel: 1 ms-resolutie, bereik met afnemende slaagkans (14 m volledig, 28 m nul in `World`, in de scenario's 8/22 m), verdiepingen kosten 10 m, CSMA, botsingen incl. verborgen nodes, willekeurig extra verlies, kanalen, LAN-link met latentie en verlies, HA als peer. Huizen van 24–30 nodes over 3 verdiepingen met kringen van 2–4 schakelaars.

| Scenario | Resultaat |
|---|---|
| Betrouwbaarheid, 0 / 10 / 25% verlies | 100% van de drukken schakelt het licht, nooit dubbel. Latentie p50 6–8 ms, p95 22–28 ms, p99 36–69 ms |
| Chaos: 500 drukken, 156 gelijktijdig, 20 herstarts, 15% verlies | alle 24 kringen eindigen in de juiste stand (pariteit), alle LED's kloppen |
| LED/terugkoppeling na drukken | 0 afwijkingen; geen spontane relaisschakelingen in rust |
| Hop-limiet (ketting, 1 pad) | hop 1–4: 20/20, hop 5–6: 0/20 (TTL 4 is een harde grens) |
| Verkeer in rust, 30 nodes | ~5 frames/s over de lucht (~1% luchttijd) |
| Router wisselt kanaal, met aankondiging | alle nodes in ~0,1 s op het nieuwe kanaal, ook bij 15% verlies |
| Router wisselt kanaal, zonder aankondiging | ~40 s (beacon-time-out), ook bij 15% verlies |
| Node viel weg en komt na de wissel terug | ~45 s na het opstarten weer op het goede kanaal |
| Twee eilanden (kanaal 6 en 11) via LAN + HA-peer, 0 en 10% LAN-verlies | licht schakelt over eilanden (20–30 ms), LED klopt, HA-beeld klopt, kanalen blijven onafhankelijk |

### Wat de simulatie veranderd heeft
1. **Suppressie K=2 is te agressief**: een node die twee keer iets hoort, stuurt niet door, ook als zijn buren aan de andere kant het nog niet kregen. K=3 (en 1 voor heartbeats).
2. **Een herhaling moet een nieuw frame zijn.** Hetzelfde frame opnieuw uitzenden helpt niet: forwarders die het al zagen, negeren het. Daarom het `evt`-nummer en dedup op `(bron, boot, knop, evt)`.
3. **Een ack moet aan het eigen event hangen.** Een gelijktijdige druk van een andere schakelaar veranderde het licht ook en wiste de herhaling (verloren drukken). Nu `cause = (bron, knop, evt)`.
4. **Zonder beacon merken nodes een bridge die van kanaal wisselt nooit**, want ze horen elkaar nog. Daarom beacons die floods binnen het eiland.
5. **Beacons mogen niet over de LAN**: een frame van het poolhuis met "kanaal 11, epoch 9" zou het hele huis naar kanaal 11 trekken.

### Compileert op hardware (`firmware/`, PlatformIO)

Alleen de kern met een dummy-HAL, dus nog geen wifi, OTA of echte pinnen. Dit is de basis, niet het eindresultaat.

| Doel | Flash | RAM |
|---|---|---|
| ESP8285 / ESP8266, 1 MB (`esp01_1m`, layout 1M + 64K FS) | 269 KB (incl. Arduino-core en wifi-stack) | 34,5 KB |
| ESP32 (`esp32dev`) | 133 KB | 19,8 KB |

De OTA-grens op 1 MB is ~470 KB. Er blijft dus ~200 KB over voor HAL, wifi-OTA en config. ESP32-Arduino moet met `-std=gnu++17` gebouwd worden (staat in `firmware/platformio.ini`).

### Bekende beperkingen en open punten
- **Boot-teller na gewiste flash**: een node met gewiste flash begint met `boot` opnieuw en kan door anderen als replay worden genegeerd tot zijn teller voorbij de oude waarde is. Oplossing in firmware: bij ontbrekende teller een willekeurige hoge startwaarde, plus `PROBE_RESP` die de laatst geziene waarde teruggeeft. Nog niet gebouwd.
- **Indicator i hoort bij knop i** (de ack-herhaling gebruikt dat). Een touch zonder gevolgd licht stuurt alleen blinde kopieën.
- Een druk vlak voor een stroomonderbreking van eigenaar of afzender (< ~1,5 s) kan verloren gaan. Dat is fysiek onvermijdelijk.
- Het radiomodel is een model, geen meting. De echte ESP-NOW-prestaties (lucht-tijd, botsingen met wifi) moeten op hardware gecontroleerd worden (F2).
- Nog niet gebouwd: gefragmenteerde `CFG_SET` boven 225 bytes, `HELLO`/adoptie, OTA, lange druk, Tuya-MCU-profiel. De `Platform`-interface (`core/include/kk/platform.h`) is het contract met de firmware (F2).

## 16. F3 en F4: de add-on (gebouwd en getest)

Draaien: `tools/test.sh addon` (pytest in een container + interop met de C++-kern), `tools/test.sh e2e` (docker compose), `tools/demo.sh` (een gesimuleerd huis met de web-UI op http://localhost:8099; knoppen indrukken met `python tools/simctl.py "press 1 0"`).

### Testlagen
| Laag | Wat | Aantal |
|---|---|---|
| C++ kern en simulator | protocol, flood, kanaalvolging, heartbeat-info + hash | 150 checks |
| Python-add-on | protocol, model, engine (klok-gedreven), discovery, instellingen, transport, web-API incl. WebSocket en ingress-controle | 60 tests |
| Interop | `kkvec gen`: echte node-frames en hash-gevallen door Python gelezen; Python-frames door C++ geopend (12 frames, 3 hashes) | 43 checks |
| End-to-end | `kkudp` (de C++-simulator in real time, echte UDP-bridge) + echte add-on + echte Mosquitto. De fysieke relaisstand in de simulator is de waarheid | 9 tests, ~21 s |

De e2e-test dekt: nodes ontdekken, MQTT-discovery, leren met live detectie (er schakelt niets tijdens het leren), config opslaan op de nodes, knopdruk schakelt licht en indicatoren op alle touches, oorzaak per wijziging, MQTT-commando als virtuele knop, hernoemen en verwijderen in discovery, stroomuitval (laatste stand terug, netwerk werkt door), verloren configuratie wordt hersteld uit de hash, en 8 drukken bij 20% radioverlies eindigen in de juiste stand.

### Wat F3/F4 veranderd hebben
1. **Heartbeat uitgebreid** met knop-aantal, indicator-aantal en config-hash. Zonder dat kende de add-on de hardware van een node niet en kon hij een kwijtgeraakte configuratie niet zien. Achteraan toegevoegd, oudere lezers negeren het.
2. **Wachttijd na een push moet vervallen zodra de hash klopt.** Anders werd een latere afwijking tot 90 s genegeerd (gevonden door de e2e-test).
3. **Versie-tellers na herstart**: wijzigt het `boot`-nummer van een node, dan geldt zijn volgende toestand als waarheid, ook als zijn `ver` lager is (nieuw geflasht).
4. **Wijziging tijdens een lopende push** gaat niet verloren (`resync`).
5. **Web-UI**: vernieuwingen kunnen elkaar inhalen, dus oudere antwoorden worden genegeerd (zichtbaar in een echte browser).

### Beslissingen
- UDP-poort 47474, broadcast als standaard, vaste peers als fallback. Het formaat van een datagram is het geseald frame, zonder omhulsel.
- Node-id's `1..0xEFFF`, add-on `0xF000` en hoger.
- Netwerksleutel: door de add-on gemaakt op de eerste start (`/data/network.key`) of via de optie; `python -m kkaddon genkey` maakt er een. De firmware leest `KK_NETWORK_KEY` uit `firmware/secrets.h` (voorbeeld: `secrets.example.h`).
- Model in `/data/network.json` (valt onder de HA-back-up), exporteerbaar.

### Bekende beperkingen en open punten
- Nodes verschijnen pas na hun eerste heartbeat; er is nog geen `HELLO`/adoptie, de node-id zit in de firmware.
- Startwaarde van `boot` van de add-on na gewiste `/data` is tijdsafhankelijk (minuten sinds 1970 mod 65536), niet gegarandeerd hoger dan de oude waarde bij lange stilstand. Zelfde open punt als voor nodes in §15.
- Het pakket (`config.yaml`, `build.yaml`, `repository.yaml`) is niet op een echte Home Assistant geïnstalleerd; de basisimage-tags in `build.yaml` en de repo-url moeten daar gecontroleerd worden. De image bouwt en draait wel (e2e gebruikt dezelfde Dockerfile op `python:3.12-alpine`).
- Broadcast is getest in een docker-netwerk, niet over een echte wifi-repeater (F2b). De unicast-peer/hub-route is geïmplementeerd maar alleen op unit-niveau en niet end-to-end getest.
- Geen OTA (F5), geen lange druk of dimmen, geen gefragmenteerde `CFG_SET` (een relais past met 12 triggers in één frame).

## 17. Externe apparaten (Tuya via local-tuya)

Een schakelaar die niet te flashen is (hier: nieuwe 4-gang Tuya-schakelaars) kan geen ESP-NOW spreken. De add-on vertegenwoordigt hem op de mesh als **virtuele node** (id `0xE000..0xEFFF`; echte nodes `1..0xDFFF`, add-on `0xF000+`). De gangen zijn HA-entiteiten (`switch.*`, `light.*`, ...) uit de bestaande local-tuya-koppeling. De add-on bewaart dus geen Tuya-sleutels. Code: `addon/kkaddon/virtual.py` (gedrag), `habridge.py` (websocket naar HA), model in `model.py` (`virtual`).

Per gang `i` (relais i = knop i), met `lamp` = stuurt de gang een lamp aan:

| Gebeurtenis | Gevolg |
|---|---|
| `BTN_EVENT` van een echte node (of andere gang) die een extern licht bedient | absolute `turn_on`/`turn_off` naar HA. Toggle rekent vanuit de laatst **opgedragen** stand, dus twee snelle drukken geven aan, uit. Daarna `LIGHT_STATE` als die node met `cause = (bron, knop, evt)`, zodat de afzender zijn eigen druk bevestigd ziet |
| Dezelfde druk nog eens (de touch hoorde de bevestiging niet) | niet opnieuw uitgevoerd; na afloop opnieuw bevestigd |
| HA meldt een stand-wijziging die wij niet opdroegen | iemand drukte op de touch van het apparaat zelf: `BTN_EVENT` als die node (echte eigenaren schakelen erop, leer-modus ziet hem), bij `lamp` ook `LIGHT_STATE` zodat de LED's van andere touches meebewegen. Herhaald als nieuw frame tot de echte eigenaren bevestigen |
| Stand-wijziging als gevolg van ons eigen bevel | **geen** druk (de oorzaak is bekend) |
| Beginstand, terugkeer uit `unavailable`, opnieuw verbinden | stil bijwerken, nooit een druk |
| Heartbeat als die node, elke 45–75 s en kort na een wijziging | herstelde touches hebben hun LED snel goed. Niet verzonden zolang een stand onbekend is |

Beslissingen en afwegingen:
- Een gang kan zijn **eigen** knop niet als trigger hebben (het relais is al omgezet): het model laat zo'n trigger weg.
- `ver` (de versieteller die touches gebruiken om oudere berichten te negeren) wordt bewaard en na een herstart ruim opgehoogd (+17), anders zouden touches de eerste nieuwe standen negeren.
- Externe lichten krijgen **nooit** `CFG_SET` en tellen niet mee in de config-hash-controle.
- In HA staan ze al (local-tuya): de add-on maakt er standaard **geen** extra `light`-entiteit van (verborgen), en geen knop- of diagnose-entiteiten.
- Ontbreekt de HA-koppeling of valt ze weg, dan staat het apparaat offline en geeft een bevel meteen een melding. De ESP-NOW-lichten merken er niets van.
- In de leer-modus schakelt het Tuya-relais bij een druk op zijn eigen touch toch (de firmware van het apparaat laat zich niet onderdrukken). De druk wordt wel gedetecteerd.
- Verwachte vertraging: 100–300 ms (HA + local-tuya) tegenover ~10 ms voor ESP-NOW. Eigen touch van de Tuya reageert direct, die loopt niet via ons.

### Getest
- 93 Python-tests (waaronder `test_virtual.py` 23 tests, `test_habridge.py` tegen een nagebootste HA).
- 11 end-to-end tests: simulator + add-on + Mosquitto + nagebootste HA (`addon/tests/fakeha.py`, 150 ms bevestigingstijd). Gedekt: mesh-druk bedient Tuya (precies een bevel per druk), LED volgt, fysieke druk op de Tuya komt op de mesh, gang zonder lamp bedient een ESP-node, UI-bevel veroorzaakt geen druk, HA reageert niet (melding, geen verzonnen stand), HA verbreekt de verbinding en de add-on herstelt.

### Niet getest / open
- Tegen een **echte** Home Assistant en echte local-tuya: de websocket-aanroepen volgen de HA-documentatie, maar de nagebootste HA is mijn lezing daarvan. `homeassistant.turn_on/off` is generiek; of local-tuya voor alle gangen netjes bevestigt, moet blijken. Tuya-apparaten met een eigen "relay mode"-instelling (touch ontkoppeld van relais) zijn niet apart behandeld.
- Een druk op een Tuya-gang die tegelijk met een bevel aankomt, kan als eigen bevel gelezen worden (venster 8 s).
- De UI voor externe apparaten is nog niet in een browser bekeken (de browser-extensie was niet verbonden); de JavaScript is syntactisch gecontroleerd en de API is end-to-end getest.

## 18. Migratie in kleine stappen: ESPHome-schakelaars tijdelijk in de add-on

De huidige ESPHome-toestellen (T3, 4CH, Basic, t4b, R16) schakelen hun relais zelf bij een touch (`on_press: switch.toggle`) en staan als `switch.*` in HA. Dat is precies het gedrag van een extern apparaat (§17), dus ze doen **zonder aanpassing aan de YAML** mee: een kring kan al in de add-on staan, terwijl de rest nog via de oude HA-automations loopt. Later flash je de toestellen één voor één om.

- **Importeren**: Nodes → *Importeren uit Home Assistant*. De add-on leest het apparaten-, entiteiten- en gebiedenregister van HA (alleen schakelaars/lichten zonder config- of diagnosecategorie, niet uitgeschakeld of verborgen), toont ESPHome-apparaten bovenaan en maakt per gekozen apparaat een extern apparaat met de kamer uit HA. Zonder registertoegang valt de UI terug op *Extern apparaat toevoegen*. Code: `HaBridge.devices()`, `GET /api/ha/devices`.
- **Gangen**: de volgorde is die van de entiteit-id's; de import zet elke gang op `lamp`. Zet dat uit voor een touch die alleen een ander licht bedient (zijn eigen relais klikt dan nutteloos mee, zoals nu). Een apparaat met meer dan 8 relais (R16) wordt in delen van 8 gesplitst, want een node heeft knopindex 0..7.
- **Per kring omzetten**: (1) importeer de betrokken toestellen, (2) koppel in de add-on de touches aan de lichten (leer-modus), (3) **zet de oude HA-automation van díe kring uit**. Staan beide aan, dan schakelt het licht twee keer en blijft het uit of aan. (4) Test, en zet bij een probleem de oude automation weer aan en verwijder de kring uit de add-on.
- **Beperkingen** (zelfde als Tuya): een druk van een ander toestel naar een ESPHome-relais loopt via HA (verwacht ~50–200 ms); de LED van een touch toont het eigen relais, niet een ander licht (de ESPHome-firmware kent geen "volg"-LED); in de leer-modus schakelt het relais bij een touch toch.
- **Straks flashen**: een toestel met nieuwe firmware wordt een echte node. Het extern apparaat moet dan vervangen worden door de echte node met behoud van lichten en koppelingen. Die "vervang"-functie is nog niet gebouwd (volgt bij F2/adoptie, want de firmware ontbreekt nog).

## 19. Plattegrond (in de add-on)

Tabblad *Plattegrond*: een rudimentaire tekening per verdieping waarop je laat zien waar de schakelaars en lampen zitten en hoe elke kruisschakeling loopt. Het is **alleen weergave**: het raakt de schakelingen, de nodes en de config-hash niet.

- **Tekenen** (knop *Bewerken*): verdiepingen toevoegen/hernoemen/verwijderen; kamers als rechthoeken slepen (naam, verplaatsen, grootte via het blokje rechtsonder); items neerzetten door ze in de lijst *Nog niet geplaatst* te kiezen en op de plattegrond te klikken, of *Alles in een raster zetten* en daarna slepen. Alles vangt aan op een raster van 10; een item blijft binnen de marge zodat zijn naam zichtbaar blijft. Werkt met muis en aanraking.
- **Items**: een *schakelaar* is een node met knoppen (ook een extern apparaat), getekend als een plaatje met één vakje per knop; een *lamp* is een licht (node, relais). De plek van een relais en de plek van de lamp zijn dus los van elkaar: een relaisbox in de technische ruimte kan lampen in elke kamer sturen.
- **Kringen**: elk licht met schakelaars krijgt een kleur (8 kleuren, geen geel want dat is "aan"). Een lijn loopt van elke schakelaar naar de lamp, met `K2` (knop 2) of `K2 ⇒ aan` bij een andere modus. In de schakelaar krijgt het knopvakje de kleur van de kring die het bedient. Klik een **lamp**: alleen zijn kring in beeld, rechts de schakelaars, een aan/uit-knop en *Schakelaars leren*. Klik een **schakelaar**: de kringen van zijn knoppen in beeld, rechts per knop welke lamp hij bedient. Zonder selectie staat rechts een legenda met alle kringen. *Lijnen: alle kringen / alleen selectie* regelt of de rest gedimd blijft of verdwijnt.
- **Live**: een druk laat het vakje van die knop oplichten en de lijn van de kring pulseren; de lamp kleurt geel als hij aan is, stippellijn als de stand onbekend of de node offline is.
- **Opslag**: `plan` in `network.json` (optioneel; zonder plattegrond verandert het bestandsformaat niet), `PUT /api/plan`, ook in export/import. Het bewaren verhoogt `rev` niet en geeft geen herconfiguratie. Beperkingen: 8 verdiepingen, 80 kamers en 400 items per verdieping.
- **Bij elke update**: de pagina verwijst met `?v=<vingerafdruk>` naar scripts en stijl en de server zet `no-cache` op `/static/`, zodat een browser (en HA-ingress) nooit een oud script bij een nieuwe pagina gebruikt.

### Niet getest / open
- Donkere modus en een smal scherm (telefoon) zijn niet visueel bekeken; de kleuren hebben een eigen donkere set en het paneel schuift onder de tekening.
- Lijnen kunnen elkaar kruisen en labels overlappen als veel kringen naar dezelfde plek lopen; verplaatsen van items lost dat op. Geen automatische indeling.
- Geen achtergrondafbeelding of muren, alleen kamers; geen undo.
- De tekenaar heeft geen geautomatiseerde UI-test (de backend wel); hij is met de demo in een browser geprobeerd.

## 20. Entiteitskringen (de AppDaemon-kruisschakelaar overnemen)

De oude opzet had per lamp een AppDaemon-app (`module: kruisschakelaar`) met een lijst entiteiten (`switch.*` en `light.*`) die elkaar volgen. Om kring per kring te kunnen verhuizen, kan de add-on dat gedrag zelf overnemen, naast de eigenlijke schakelaar-en-lamp-structuur. Code: `kkaddon/sync.py` (gedrag), `appdaemon_import.py` (YAML lezen), `model.py` (`groups`), tabblad *HA-kringen* in `static/groups.js`.

- **Gedrag**: verandert een entiteit van een kring doordat iemand op een touch drukt, dan krijgen de andere leden dezelfde stand, absoluut (`turn_on`/`turn_off`). Een lid dat uit de pas liep wordt zo ook rechtgezet zodra er weer iets verandert.
- **Aan/uit per kring**: alleen een kring die *Actief* is doet iets. Een kring die uit staat wordt wel gevolgd, de stand staat in de UI, zodat je kunt vergelijken. **Nieuwe kringen komen altijd uitgeschakeld binnen**; opnieuw importeren werkt entiteiten en naam bij maar laat aan/uit staan. *Alles uit* zet ze in één keer uit.
- **Bedienen**: elke kring staat ook op het tabblad *Lichten* met een aan/uit-knop (alle bereikbare leden naar dezelfde stand; staat er iets aan, dan wordt het uit). Een klik op één entiteit wisselt alleen die. Is de kring actief, dan is dat voor de kring een druk en volgt de rest; staat hij uit, dan verandert alleen dat ene lid. `POST /api/groups/{id}/cmd {op, entity?}`.
- **Lamp-relais**: per kring kun je aangeven welke entiteit(en) echt de lamp schakelen (`lamp`, een deelverzameling van de entiteiten); de rest zijn touch-relais. Het lamp-relais bepaalt de stand die de knop op *Lichten* toont en waar *wisselen* van uitgaat, en krijgt bij bediening het bevel als eerste. Is het onbereikbaar, dan beslissen alle leden. Het volgen zelf blijft voor alle leden gelijk. Instellen per entiteit (tag *lamp?*) of voor alle kringen tegelijk op naam (*Lamp-relais kiezen op naam…*, bv. `dingtian`). Een lamp-relais blijft bewaard bij opnieuw importeren en valt weg als de entiteit uit de kring verdwijnt.
- **Maken en bewerken**: *Nieuwe kring* (naam en minstens 2 entiteiten, met suggesties uit HA) en *Bewerken* per kring: naam wijzigen en entiteiten toevoegen of weghalen (✕). Een nieuwe kring staat uit; een lamp-relais dat uit de kring verdwijnt valt weg. Is de kring actief, dan gelden wijzigingen meteen.
- **Verhuizen**: zet een kring hier *Actief* en dezelfde kring in AppDaemon uit. Staan beide aan, dan geven ze elkaar tegenbevelen. De add-on kan AppDaemon niet uitzetten.
- **Veiligheid**: een entiteit mag maar door één ding worden bestuurd. Een kring aanzetten wordt geweigerd als een entiteit al in een andere actieve kring of in een extern apparaat zit (en omgekeerd).
- **Wanneer reageert hij niet**: de beginstand, de terugkeer uit `unavailable` en het opnieuw verbinden met HA zijn nooit een aanleiding om te schakelen, en aanzetten van een kring herstelt niets. Alleen een stand-wijziging die wij niet zelf opdroegen telt. Een lid dat niet beschikbaar is wordt overgeslagen; een bevel dat na 10 s niet is opgevolgd wordt gemeld in het logboek.
- **Echo's**: eigen bevelen herkennen we aan een rij verwachte standen per entiteit. Twee snelle drukken achter elkaar geven daardoor geen heen-en-weer: er gaat alleen een bevel uit als de laatst opgedragen of bekende stand afwijkt.
- **Import**: *AppDaemon-YAML importeren* leest een kringenlijst of een heel `apps.yaml`. Commentaarregels (`#- light.…`) blijven weg, andere apps en kringen met minder dan 2 entiteiten worden overgeslagen met een reden, en het voorbeeld toont per kring welke entiteiten HA niet kent. Naam en id komen van de sleutel (`kruisschakelaar_lamp_keuken` → *Keuken*).
- **Opslag**: `groups` in `network.json` (optioneel), `POST /api/groups/parse`, `POST /api/groups`, `PUT`/`DELETE /api/groups/{id}`, `POST /api/groups/all-off`.

### Niet getest / open
- Alleen tegen een nagebootste HA en met unit-tests; de oude AppDaemon-app is niet naast de add-on gedraaid. Of AppDaemon bijzonderheden had die hier ontbreken (bv. vertragingen, uitzonderingen per entiteit) is niet bekeken: alleen de entiteitenlijsten komen mee.
- De kringen staan niet op de plattegrond en gebruiken de nodes en het ESP-NOW-netwerk niet. Ze zijn bedoeld als tussenstap.

## 21. Schema (blokken per ruimte, zoals Loxone Config)

Tabblad *Schema*, nu de startpagina: per ruimte een tabblad met blokken en draden. Code: `kkaddon/schema.py` (blokken, draden, omzetten), `static/schema.js` (tekenen en slepen), `schema` in `network.json`.

| Blok | Sleutel | Links (ingang) | Rechts (uitgang) |
|---|---|---|---|
| Lamp via HA (een kring) | `g:<id>` | ingangen: leden die geen lamp-relais zijn | uitgangen: de lamp-relais |
| Lamp op het netwerk (een licht) | `l<node>.<relais>` | ingangen: de triggers | één vaste uitgang: het eigen relais |
| Schakelaar: node of extern apparaat | `n<id>` | touch per gang | relais per gang |
| Schakelaar: HA-apparaat | `d:<id>` | touch per gang | relais per gang |

- **Eén waarheid**: een draad is geen eigen gegeven maar een lid van een kring of een trigger van een licht. `schema.wire` is de enige plek waar het schema de schakelingen wijzigt, via de bestaande `set_group` en `set_triggers`, dus met dezelfde controles (een actieve entiteit heeft één eigenaar, maximaal 12 triggers). In `schema` staan alleen ruimtes, plekken en de `d:`-blokken. Dat bewaren verhoogt `rev` niet en geeft geen herconfiguratie, net als bij de plattegrond.
- **Ingang of uitgang**: bij een HA-toestel zijn touch en relais één entiteit, want we zien een druk alleen als een stand-wijziging. Een lid is daarom ingang óf uitgang. Dat is hetzelfde onderscheid als `lamp` in §20. **Het gedrag is ongewijzigd**: alle leden volgen elkaar nog. Een touch-relais moet de lamp volgen omdat zijn LED eraan hangt, en een lamp-relais blijft op zijn eigen touch reageren.
- **Omzetten** (`POST /api/schema/convert`): maakt `d:`-blokken uit het HA-apparatenregister, of zonder register op de naam van de entiteit (`…_relay_3`). Een schakelaar komt in zijn HA-gebied of de kamer van de node, anders in *Centraal*: waar een toestel hangt valt niet te raden, en op de echte installatie hebben 70 van de 71 apparaten geen gebied. Een lamp hoort bij zijn naamfamilie (`Quarto philippe bed links` bij `Quarto philippe`, `Buiten keuken` bij `Buiten terras`), anders een ruimte in zijn naam, anders waar de meeste touches zitten, anders een eigen ruimte met zijn naam. Een pagina zonder lampen wordt een raster. *Opnieuw indelen* (`{"reset": true}`) wist ruimtes en plekken, niet de draden. Kan herhaald worden: wat staat blijft staan. Een toestel zonder draden en een extern licht zonder bediening blijven in de lijst links.
- **Eigen naam**: `schema.names` geeft een schakelaar-blok een naam naast de technische naam uit HA of het model; die laatste blijft zichtbaar en doorzoekbaar. Alleen weergave. Een lamp heeft geen aparte eigen naam: de naam van de kring of het licht wordt gewijzigd.
- **Verwijzingen in plaats van lijnen**: op de echte installatie waren lijnen niet te volgen, dus `SC_WIRES` in `schema.js` staat uit: elke verbinding, ook binnen een pagina, is een `↗`-regel op de lamp en een klikbare verwijzing op de schakelaar. Een blok staat op één pagina. De kleur toont het type; een HA-toestel dat alleen als uitgang is aangesloten geldt als relaisbox. Zo'n aansluiting maak je via het zijpaneel, op de lamp of op de schakelaar.
- **Verhuizen naar een extern apparaat**: een gang van een extern apparaat gaat voor op een `d:`-blok. Importeer je een toestel, dan verhuizen de draden vanzelf en verdwijnt het lege `d:`-blok.
- **Een lamp mag leeg zijn**: een kring heeft geen minimum van 2 entiteiten meer, want een lamp-blok krijgt zijn relais één voor één. Import en *Nieuwe kring* vragen wel nog 2.

### Niet getest / open
- Getest met unit-tests (`test_schema.py`) en in een echte browser (slepen, klik-klik, Delete, ruimtes, donker, smal), met een nagebootst huis. Nog niet met de echte HA-registers en het echte aantal kringen: hoe druk een pagina met veel draden wordt en of de indeling per ruimte klopt, moet daar blijken.
- Een HA-lamp mengt niet met geflashte nodes en een netwerklicht niet met `d:`-blokken. Dat wordt nodig bij de migratie (F7).
- Een blok kan niet op twee pagina's staan (Loxone kan dat wel); geen logische blokken zoals timers of bewegingsmelders; geen automatische lijnvoering rond blokken; geen undo.
- De kringen staan nog niet op de plattegrond, en de tabbladen *Lichten* en *HA-kringen* bestaan naast het schema.
