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

Een schakelaar die niet te flashen is (hier: nieuwe 4-gang Tuya-schakelaars) kan geen ESP-NOW spreken. De add-on vertegenwoordigt hem op de mesh als **virtuele node** (id `0xE000..0xEFFF`; echte nodes `1..0xDFFF`, add-on `0xF000+`). De gangen zijn HA-entiteiten (`switch.*`, `light.*`, ...) uit de bestaande local-tuya-koppeling. Deze route bewaart dus geen Tuya-sleutels; §28 is de rechtstreekse route, die dat wel doet. Code: `addon/kkaddon/virtual.py` (gedrag), `habridge.py` (websocket naar HA), model in `model.py` (`virtual`).

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

> Sinds 0.4.1 is het tabblad weg: het schema (§21) vervangt het. `plan` blijft in `network.json`, export en `PUT /api/plan` staan, zodat een getekende plattegrond niet verloren gaat. De code van de tekenaar staat in de git-geschiedenis (`static/plan.js`).

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

> Sinds 0.4.1 zijn de tabbladen *HA-kringen* en *Lichten* weg. Een kring is een lamp-blok op het schema (§21): aan/uit, *Actief*, uitgangen, ⇄ per relais en de naam staan in de zijbalk. Importeren, lamp-relais op naam en alles uitzetten staan onder *Instellingen*. Het gedrag hieronder is ongewijzigd.

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

## 22. Timer en disable-ingang

Eerste logica in het schema, in de geest van de Schakelklok van Loxone. Code: `model.py` (`timers`, `disable` op een kring, `timer_active`), `sync.py` (`blocked`, `_hold`), `engine.py` (tijd), blok `t:<id>` in `schema.py` en `schema.js`.

- **Timer**: per dag tot 3 intervallen met uitgang 1, anders 0. Begin hoort erbij, einde niet. `to` 00:00 is middernacht aan het eind van de dag. Over middernacht lopen kan niet: dat zijn twee intervallen. Dit is veel eenvoudiger dan de Loxone-module (geen feestdagen, geen zon, geen analoge waarde).
- **Disable**: `groups.<id>.disable` wijst naar een timer. Dit is bewust geen schema-gegeven, want het verandert het gedrag.
- **Geen tussenbeurt nodig**: de stand wordt op het moment van een druk berekend, dus er is geen planner die een moment kan missen, ook niet na een herstart. De UI ziet de stand bij de verversing (uiterlijk 20 s).
- **Een druk tijdens disable**: het toestel heeft zijn eigen relais al omgezet, dat is niet tegen te houden. `_hold` zet alleen dat relais terug op de stand van de lamp (de lamp-relais als die bekend zijn, anders de vorige stand). Zonder dat liep de LED van die touch uit de pas. Je ziet het relais dus kort klikken. De echo is een eigen bevel, dus geen nieuwe druk.
- **Niet geblokkeerd**: een wijziging van een lamp-relais (bv. via HA) en bediening in de add-on. De opdracht was dat een touch niets doet. Ook de lamp blijft zoals hij stond: de timer zet nooit iets aan of uit.
- **Tijdzone**: uit HA (`get_config`, `tzdata` in de image), want de container kan op UTC staan. Lukt dat niet, dan de tijd van de container; de zijbalk van een timer toont welke tijd geldt.

### Niet getest / open
- Op de echte installatie is er geen druk gedaan tijdens een interval: het gedrag is getest met unit-tests tegen de engine, niet met een echte touch.
- Een lamp zonder lamp-relais: alle leden gelden als touch.
- Lichten op het netwerk beslissen op de node en hebben geen disable. Een HA-entiteit kan nog niet op de disable (timers en modules wel, zie §23).

## 23. Tijd- en logicamodules, en de naam Huisconfig

De add-on doet meer dan kruisschakelen en groeit naar een algemene configuratie. Daarom heet ze sinds 0.5.0 **Huisconfig**. Alleen de zichtbare naam is veranderd: de `slug`, het MQTT-voorvoegsel `kruisschakelaars`, de repository en de codenaam `kkaddon` blijven. Een andere slug is voor HA een andere add-on, zonder `/data`; een ander voorvoegsel maakt nieuwe entiteiten in HA.

Code: `logic.py` (types, berekening, zon), `model.py` (`modules`, controles), blok `m:<id>` in `schema.py` en `schema.js`.

- **Soorten**: een uitgang is een tijd (minuten sinds middernacht) of een 0/1. `MODULE_TYPES` legt per type de ingangen vast; een nieuw type is daar een regel, plus de berekening en een regel in `SC_MENU`.
- **Geen toestand**: alles wordt op het moment van de vraag berekend uit de klok, net als de timer. Er is geen planner die een moment kan missen.
- **Onbekend**: een derde waarde (losse ingang, geen locatie, poolnacht). `en`/`of` volgen de driewaardige logica (`0 en ?` = 0, `1 of ?` = 1), de rest geeft dan onbekend. Op de disable telt onbekend als 0: een fout in de logica mag een lamp nooit onbedienbaar maken.
- **Over middernacht**: optellen en aftrekken rekenen modulo 24 uur; `tussen` met `van` later dan `tot` loopt over middernacht. `na` en `voor` kijken alleen naar vandaag.
- **Zon**: zelf berekend (de zonsopgangsvergelijking) met breedte en lengte uit `get_config` van HA, in plaats van `sun.sun`: die geeft de *volgende* zonsondergang, dus na zonsondergang die van morgen. Nauwkeurig op een paar minuten, en geen extra afhankelijkheid.
- **Controles**: bij elke wijziging en import bestaan de bronnen, klopt de soort en is er geen lus. Bij het opstarten wordt niets geweigerd; een lus uit een oud bestand geeft onbekend.
- **Disable**: `groups.<id>.disable` is een kaal timer-id (zoals in bestaande bestanden) of `m:<id>`.
- **Keuze** (`select`, 0.51): de Loxone-gedachte van een analoge schakelaar, als kleinste vorm: één 0/1-ingang `s` en twee vaste getallen op het blok (`value` bij 1, `value0` bij 0), een getal als uitgang. De getallen zijn dus geen ingangen: wie ze uit een ander blok wil halen, voegt later twee ingangen toe waarvan een aangesloten bron voorgaat op het vaste getal (zoals de sprinkler bij cycli en wachttijd). Onbekende ingang geeft onbekend, niet 0 of `value0`: een ontbrekende bron mag geen stroom of grens kiezen. Geen hysterese (§23, geen toestand). Naar een Tuya-instelling (0.52): de routine Tuya instellen (§30) neemt ook een getal als ingang en zet dan dat getal, met dezelfde regels (uit, eerste stand alleen onthouden, onbekend niets, interval); het type van de stand (0/1 of getal) telt mee in de vergelijking, want `1 == True`. Het bereik bewaakt het toestel zelf (`convert`).

### Niet getest / open
- Unit-tests tegen de engine en een nagebootste HA, en de pagina in een browser. Niet tegen de echte HA en niet met een echte touch.
- Geen `niet`-module: overdag blokkeren geeft hetzelfde als alleen 's nachts toelaten.

## 24. Aan/uit-ingang van een lamp

`groups.<id>.control` wijst, net als `disable`, naar een timer of `m:<id>`. Code: `sync.py` (`held`, `hold_tick`), `engine.py` (`source_value`, elke 5 s `_hold_tick`).

- **Niveau, geen puls**: de lamp volgt de waarde en wordt erop gehouden. Geen geheugen dus: na een herstart volgt de stand opnieuw uit de klok. Omslaan is er zonder apart moment: wat afwijkt wordt rechtgezet.
- **Afwijkingen**: een stand-wijziging die niet van ons komt wordt meteen teruggezet (`_hold`, ook een lamp-relais, anders dan bij disable). De andere leden corrigeert de volgende tick. Bedienen via de API wordt geweigerd, want de tick zou het toch terugdraaien.
- **Onbekend en uit**: geeft de bron onbekend of staat de kring uit, dan doet de ingang niets en werkt de lamp gewoon.
- **Geen spam**: reageert een toestel niet, dan wordt per kring hooguit elke 60 s opnieuw bevolen. Zonder HA-verbinding gebeurt er niets; bij het terugkomen zet de eerste tick het recht.

### Niet getest / open
- Tegen de nagebootste engine en in de browser, niet tegen de echte HA of een echte touch.
- Alleen lampen via HA. Op een licht op het netwerk beslist de node; daar kan de add-on alleen achteraf bevelen.
- Er is geen handmatige uitzondering: wie 's nachts de lamp uit wil, moet de ingang loskoppelen.

## 25. Sprinkler

Code: `sprinkler.py` (plan en aansturen, elke seconde), `model.py` (`sprinklers`), blok `s:<id>`, module `number` (`Getal`).

- **Plan bij de start**: anders dan de modules heeft een beurt toestand: ze start op een moment en moet doorlopen als het startuur (bv. de zon) ondertussen verschuift. Daarom een vast plan op de monotone klok, alleen in het geheugen. Bewust niet bewaard: na een herstart gaat er liever een beurt verloren dan dat een klep onverwacht opengaat.
- **Herhalen**: elke `refresh` s (standaard 30) krijgt elk relais opnieuw zijn stand. Dat herstelt een gemist bevel, niet een uitval van de add-on, HA of de wifi. Een bewaking in het toestel die op dit herhalen wacht werkt in ESPHome niet zomaar, want `on_turn_on` vuurt alleen bij een verandering: daarom een vaste maximale tijd in het toestel (DOCS.md).
- **Eén eigenaar**: een relais op een actieve sprinkler mag niet ook in een actieve kring of op een andere actieve sprinkler. Een extern apparaat mag wel: dat is alleen de bron van de gang. Heeft die gang een trigger, dan ziet die het schakelen wel als een druk.
- **Geen blok nodig**: een zone bewaart een HA-entiteit, los van het schema. Een relaisbox die alleen kleppen schakelt hoeft dus niet als schakelaar-blok op het schema te staan.
- **Starten**: op de minuut van het startuur, één keer per minuut. Een gemiste minuut wordt niet ingehaald. Loopt er nog een beurt, dan start er geen tweede.

### Niet getest / open
- Tests met een nagebootste klok en een nagebootste HA, en de pagina in een browser. Niet met echte kleppen.
- Alleen relais via Home Assistant; een relais van een geflashte node nog niet.
- De stand van het relais wordt niet teruggelezen: of de klep echt open is, weet de add-on niet.

## 26. NSPanel

Code: `panel.py` (configuratie en schermtaal, puur), `cards.py` (kaarten, zie onder), `panelhub.py` (UDP, HA-standen, relais), `habridge.py` (alle standen, sjablonen, voorspellingen, algemene service-aanroepen), `berry/huisconfig.be` (op de panel), `model.py` (`panels`), blok `p:<id>` (`schema.py`, `static/schema.js`).

- **Wat blijft**: Tasmota op de ESP32 en de Nextion-TFT van nspanel-lovelace-ui. Het scherm is een domme renderer die tekstregels krijgt (`pageType~`, `entityUpd~`, `weatherUpdate~`, ...) en aanrakingen terugmeldt (`event,buttonPress2,...`). We schrijven dus geen schermsoftware en flashen de ESP32 niet (er is geen seriële noodweg).
- **Wat vervangt**: AppDaemon (de pagina's maken) en MQTT (het transport). De driver in `autoexec.be` legt `CustomSend` en `CustomRecv` al bloot; ons Berry-script hangt daar een UDP-brug aan: `D|tekst` naar het scherm, `S|tekst` van het scherm, hartslag `H|{relais, temperatuur, rssi}`.
- **Herkomst en licentie**: het protocol en het gedrag (veldvolgorde, kleuren, navigatie) heb ik uit de broncode van nspanel-lovelace-ui gelezen en daarna zelf uitgeschreven, getest tegen de config van dit huis. De pictogramtabel (`panel_icons.py`) is een selectie uit de lettertypecodes van de TFT. Die repo is GPL-3.0: staat dit project ooit publiek, laat dan nakijken of dat volstaat of dat deze module dezelfde licentie moet krijgen.
- **Eén eigenaar per scherm**: AppDaemon en deze add-on mogen niet tegelijk sturen. Overschakelen = AppDaemon-app uit, `load("huisconfig.be")` in `autoexec.be`.
- **Relais als objecten**: de panel meldt ze in zijn hartslag, de add-on biedt ze aan met MQTT-discovery (`switch`, `sensor`) en zet ze via `P|n|0/1`. De knoppen op de panel blijven lokaal werken (Tasmota).
- **Module op het schema**: de configuratie staat in het model, dus in de back-up; de hub volgt het model. `panels.json` van 0.8.0 wordt eenmalig overgenomen. Elke gang is touch en relais met dezelfde entiteit, zoals een schakelaar uit HA: de knop schakelt lokaal het relais en de lamp ziet de standwissel als een druk. De gangen zijn gewone in- en uitgangen via hun HA-entiteit, die met `default_entity_id` vast ligt, zodat een draad niet afhangt van de naam in HA. De kaarten blijven voorlopig YAML in de zijbalk: eerst moet de keten op de echte panel bewezen zijn, dan pas een visuele editor.
- **Verloren pakketten**: UDP, geen bevestiging. De add-on spaart de ontvangstbuffer van de ESP32 (30 ms tussen pakketten, hartslag-poll als de panel stil is) en stuurt de huidige pagina elke 2 minuten opnieuw.
- **Starten**: de hub wacht tot de panel zich meldt en HA verbonden is, en stuurt dan klok, helderheid en screensaver. Zegt het scherm zelf `event,startup` (herstart), dan begint de hub opnieuw.

### Kaarten in plaats van YAML (0.11)

Code: `cards.py` (puur: configuratie, live standen, omzetten), `model.py` (`cards`, `panels.<id>.cards`), blok `c:<id>` (`schema.py`, `static/schema.js`).

- **Kaarten zijn blokken**: `cards: {id: {type, name, ...}}` met de types `grid` (raster of lijst), `charger`, `saver`, `power`, `qr`. Een paneel heeft een geordende lijst `cards`; een screensaver-kaart daarin is het startscherm (één per paneel, altijd vooraan), de rest zijn de pagina's. Een rasterknop `{"ref": "c:<id>"}` maakt van een kaart een subpagina; `pages_of` zoekt alle subpagina's (ook dieper, een lus hangt niet).
- **Eén renderer**: `cards.config` maakt de configuratie die `panel.Panel` al kende (het formaat van nspanel-lovelace-ui). Het protocol, de navigatie en de screensaver blijven dus de geteste code. `test_cards.py` loopt de config van dit huis door, zowel uit de YAML als uit de omgezette kaarten, en vergelijkt wat het scherm krijgt: alleen de id's van de subpagina's en de labels van de QR-kaart verschillen.
- **Standen uit de add-on**: een rij `kk.<kaart>__<deel>` vraagt haar stand aan `Env.dynamic` en haar druk aan `Env.press` (`cards.Live`). Zo lopen een lamp van het schema (een kring via `groups.command`, een licht op het netwerk via `command_light`) en de laadpaal (TuyaHub `read`/`set`) niet via een HA-entiteit, waarvan de naam niet vaststaat en die bij een toestel van een andere installatie (stap 2) niet bestaat. De hub ververst een paneel met zulke rijen elke tik; `Panel.refresh` stuurt alleen wat veranderd is. Een lamp met een aan/uit-ingang negeert een druk op het paneel, zoals in de UI.
- **Laadpaal** (sinds 0.15.2 met poorten): drie ingangen `inputs` {status, time, energy}, elk een datapunt `v:<dp>@<toestel>`, en een uitgang `target` {device, dp, on, off}. De knop zet `on` of `off`; laden = het datapunt staat op `on` (aan/uit vergeleken als aan/uit: True ↔ ON) of de status is `charing`/`charging`. Prijs = Geladen × tarief (een HA-entiteit wint als ze een getal geeft). Een kaart van vroeger met `tuya` (een Feyree) wordt bij het laden omgezet: 101, 120, 25 en 124 met `OpenCharging`/`CloseCharging`. Een draad van start/stop naar een instelling vult die twee waarden alleen in als het datapunt ze als opties heeft (Feyree) of aan/uit is; anders kiest de gebruiker.
- **Omzetten**: een paneel met `yaml` en zonder `cards` wordt bij het laden kaarten (`from_yaml`), de YAML gaat naar `panel-<id>.yaml.overgenomen`, de kaarten komen links naast het paneel en wat niet mee kon staat in `notes`. Een kleur in drie stappen (`iif(... < a, groen, iif(... < b, oranje, rood))`) wordt twee grenzen; een ander sjabloon gaat niet mee. Zonder `locale` blijft het scherm Engels, zoals het was.
- **Pictogrammen**: `ev-station` en `currency-eur` zijn toegevoegd door het MDI-codepunt om te rekenen (zoals de rest van de tabel klopt), niet op de TFT gezien.

### Niet getest / open
- Getest: de schermtaal tegen de config van dit huis (`tests/data/nspanel1.yaml`), de hub met echte UDP naar een nepl panel en een nagebootste HA, en het Berry-script los op de echte panel (laden, hartslag, `Q|`, een scherm-event; de relais en het schermcommando zijn daarbij niet bediend).
- Kaarten (0.11): getest met unit-tests (`test_cards.py`, `test_panelhub.py`: lamp en laadpaal van druk tot scherm, tegen een nagebootste lader) en de pagina in een browser (omzetten van de YAML van dit huis, kaart toevoegen, draad naar het paneel en naar een lamp, volgorde). Niet op de echte panel en niet tegen de echte lader: welke waarden 101 en 120 echt geven, en of 124 het laden start en stopt.
- `default_entity_id` is niet getest tegen een echte HA: geeft die het relais een andere entiteit, dan werkt een draad naar een lamp niet (de panel en zijn eigen relaisknoppen wel).
- Niet getest: de volledige keten op de echte panel met de echte TFT (of de pagina's er precies zo uitzien als via AppDaemon), `autoexec.be` na een herstart, en een lange periode met verloren pakketten.
- Niet ondersteund: kaarttypes thermostaat/media/alarm/ontgrendelen/grafiek, detailpagina's (lange druk), `cover`-schuiven, dimmen van lichten, `service:`-items, de update van de TFT. Op een kaart niet meer: sjablonen in namen, voorwaarden (`state:`), de opties van de screensaver (`tapToWake`, `inNavigation`, `theme`, `defaultCard`).

## 27. Schermen met openHASP

Code: `hasp.py` (ontwerp en aanrakingen, puur), `hasphub.py` (MQTT, HA-standen, status), `model.py` (`screens`), blok `h:<id>` (`schema.py`, `static/schema.js`).

- **Wat blijft**: de openHASP-firmware op de ESP32-S3-schermen (4848S040) en Mosquitto. Niets opnieuw flashen; de add-on heeft de broker toch al nodig voor zijn entiteiten.
- **Wat vervangt**: de pyscript-app `openhasp_vvscherm` met zijn module `openhasp`. Die zat niet in de officiële openHASP-integratie; `sensor.schermNN` kwam uit `state.set`.
- **Eén eigenaar per scherm**: een scherm zonder blok raakt de hub niet aan, zodat pyscript en de add-on naast elkaar kunnen draaien tot een scherm wordt overgezet. Dezelfde firmwarenaam op twee blokken wordt geweigerd.
- **Merkteken**: de LWT is retained, dus elke herstart van de add-on ziet "online". Wissen en opnieuw sturen zou dan alle schermen laten knipperen. Daarom staat als laatste object een verborgen label (`p0b250`) met de hash van de pagina's en `LAYOUT_VERSION`. Bij "online" vraagt de hub de tekst op: gelijk is klaar, anders (of geen antwoord binnen 3 s) wordt het ontwerp gestuurd. Faalt die vraag, dan knippert het scherm hooguit; het mist nooit zijn ontwerp. Wie de indeling in `hasp.py` wijzigt, hoogt `LAYOUT_VERSION` op.
- **Status uit de LWT**: de broker meldt een weggevallen scherm. De oude koppeling telde op `sensor.time`, die in dit huis niet bestaat, en bleef daardoor altijd `up` zeggen.
- **Navigatie via de hub**, net als de pyscript-app: dat werkt op deze schermen. `action` op het scherm zou de 12 firmwarepagina's rondgaan, ook de lege.
- **Wakker maken**: de eerste aanraking op een donker scherm schakelt niets. De volgorde van `state/backlight` en het `down`-event is niet bekend, daarom ook een marge van 0,7 s na een melding "aan".
- **Kleuren** gaan elke 120 s opnieuw, omdat de brug met QoS 0 publiceert.
- **Kaarten (0.16)**: een scherm hangt aan dezelfde kaartblokken als een NSPanel (`model.screens[sid].cards`, `cards.screen_pages`). `hasp.expand` maakt van de uitvoer van `cards._card` firmwarepagina's: 4 tegels per pagina, een kaart met meer rijen loopt door ("Titel (1/2)"), lege plekken achteraan vallen weg. Rijen `kk.*` lezen en schakelen via `cards.Live` (lampen, kringen, lampen van een andere installatie, Tuya-waarden, laadpaal), net als op het paneel; `iText.*` is alleen tekst, een sensor toont zijn waarde. Subpagina's komen na de hoofdpagina's; hun middelste navigatieknop is **Terug**, naar de pagina waar je vandaan kwam (bijgehouden in `Screen`). Tekst en kleur van een tegel horen niet in de hash (ze gaan als `.text`/`.bg_color`), zodat een naam die van een andere installatie verandert geen nieuw ontwerp vraagt. De hub roept `sync()` elke seconde aan (alleen verschillen gaan weg). Meer dan 12 pagina's: `LayoutError`, in de status als `error`, en het scherm wordt niet aangeraakt. Geen screensaver op een scherm (`cards.SCREEN_TYPES`). De oude eigen pagina's worden eenmalig rasterkaarten (`model._old_screen`); een knop naar een domein dat het oude scherm niet kon bedienen wordt een lege plek.

### Niet getest / open
- Getest: de hub tegen nagebootste schermberichten en een nagebootste HA, en de zijbalk in een browser.
- Gemeten op `scherm03` met de echte broker (0.9.0): het ontwerp komt aan en ziet er goed uit (screenshot), de add-on mag op `hasp/#`, `p0b250.text` en `.hidden` antwoorden, `backlight` zonder inhoud antwoordt, `backlight on` werkt en wordt gemeld, en de time-out stuurt op tijd `backlight off`. `idle off` maakt een scherm na `backlight off` niet wakker, daarom stuurt de hub na een ontwerp `backlight on`.
- Getest in huis op `scherm01` en `scherm03` (0.9.1): aanraken op het echte scherm is in orde bevonden, en bij de herstart van de add-on is geen geknipper gezien. Na een herstart van HA stonden beide schermen online en kwamen de kleuren overeen met HA (29 van de 30 knoppen uitgelezen, alle gecontroleerde lampen uit; "aan" is daarbij dus niet uitgelezen).
- De firmware rondt kleuren af: `#DDE3E8` komt terug als `#dee2ee`. Vergelijk dus niet letterlijk met wat je hebt gestuurd.
- Niet gemeten: de precieze volgorde van `state/backlight` en het `down`-event bij het wakker maken.
- Niet ondersteund: camera's, de Pool-pagina, dimmen, een QR-code (de wifi-kaart is tekst), pictogrammen per knop (alle schakelknoppen krijgen het lamppictogram). Niet getest op een echt scherm (0.16): de tekst met een regeleinde (`\n`) in een knop, en of `.text` met een pictogram goed overkomt.

## 28. Tuya rechtstreeks

Code: `tuya.py` (protocol en verbinding), `tuyahub.py` (toestellen, profielen, entiteiten), blok `y:<id>` (`schema.py`, `static/schema.js`), `tests/test_tuya.py`.

- **Waarom**: de Feyree-lader spreekt protocol 3.5 en LocalTuya 5.2.3 niet ("no datapoints found"). Zelf doen vervangt HACS en LocalTuya en geeft per toestel de vrijheid over de entiteiten.
- **Eigen client in asyncio, geen tinytuya**: tinytuya blokkeert. Het is wel de referentie: de frames en sessiesleutels in de tests zijn vectoren die eruit komen. Afhankelijkheid: `cryptography` (AES-ECB en AES-GCM; PyNaCl kent ze niet). Geen wheel voor musl op armv7: de marker in `requirements.txt` slaat haar daar over en de functie staat dan uit, zodat de add-on op armv7 blijft bouwen.
- **Sleutels apart**: `/data/tuya.json` (0600), niet in `network.json`, want dat bestand gaat mee in de export. `status()` geeft nooit een sleutel.
- **Entiteiten**: een entiteit bestaat pas als het toestel het datapunt meldt. Datamodel uit de Tuya-cloud: eenmalig via een profiel in de code of geplakt. Schrijven kan alleen op een datapunt dat het datamodel beschrijfbaar noemt en binnen het bereik. De stand in HA komt altijd uit een melding van het toestel, nooit uit het bevel.
- **Verbinding**: hartslag elke 10 s, statusvraag elke 30 s, opnieuw verbinden met wachttijd tot 60 s. `auto` probeert 3.5, 3.4, 3.3 en bewaart wat werkt.
- **Op de lader gemeten**: DP's 101 (`connect`), 102–104 (schaal 0,1 V) en 124 komen door. `switch` (18), `work_state` (3) en `work_mode` (14) staan in het datamodel maar meldt deze lader niet: starten en stoppen gaat via 124 (`ChargingOperation`).

### Niet getest / open
- Getest tegen een nagebootst toestel voor 3.3, 3.4 en 3.5, en lezen tegen de echte lader (3.5). De paden voor 3.3 en 3.4, en het `data unvalid`-geval, zijn niet op een echt toestel geprobeerd. 3.1 wordt niet ondersteund.
- Geen UDP-ontdekking en geen massa-import van sleutels: elk toestel wordt met de hand toegevoegd.
- De poorten op het blok tonen alleen wat er is: links instelbaar, rechts meldingen, diagnose alleen in de zijbalk. Draden naar en van andere blokken kunnen nog niet: daarvoor ontbreekt een vergelijker (getal naar 0/1), en het sturen van een toestel vanuit logica heeft een eigen veiligheidsregeling nodig (een lader mag niet zomaar starten). De entiteiten kunnen wel via een extern apparaat of kring meedoen (met de omweg via HA).

## 29. Koppelingen tussen installaties

Code: `link.py` (sleutel, handdruk, berichten; puur), `linkhub.py` (TCP, delen, bevelen), blok `r:<installatie>:<object>` (`schema.py`, `static/schema.js`), `static/links.js` (Instellingen), `tests/test_link.py`.

- **Waarom**: een deel van het huis (bv. de laadpaal) hangt in een ander subnet met een eigen HA en add-on. Alleen de Home Assistants bereiken elkaar over IP, de toestellen erachter niet. De add-ons praten dus rechtstreeks met elkaar, niet via HA (geen tokens, en een toestel van de add-on heeft niet altijd een vaste HA-entiteit).
- **Transport**: een websocket op een eigen TCP-poort (`link_port`, 47490; de add-on draait met `host_network`). De websocket hoeft niet te vertrouwen: elk bericht is zelf versleuteld en ondertekend (XSalsa20-Poly1305 via PyNaCl, dat er ook op armv7 is).
- **Handdruk**: `hello` en `welcome` met de gedeelde koppelsleutel en een willekeurige nonce van 16 bytes per kant; de sessiesleutel is BLAKE2b over de sleutel en beide nonces. Daarna heeft elke nonce een richting en een stijgende teller: een opgenomen bericht nog eens sturen, of een bericht terugkaatsen, wordt geweigerd. Wie de sleutel niet heeft, komt niet voorbij `hello`. De sleutel staat in `/data/link.json` (0600), niet in het model en dus niet in de export.
- **Mesh**: elke installatie verbindt met de adressen die je bij haar opgeeft; één kant volstaat. Ontstaan er twee verbindingen tussen dezelfde twee (beide kanten gaven elkaar op), dan blijft die van de kleinste id; beide kanten passen dezelfde regel toe en melden `dup`. De id volgt eenmalig uit de naam en verandert daarna niet, want andere installaties verwijzen ermee (`r:<id>:...`).
- **Aanbieden en kiezen** (protocol 2, 0.13): de eigenaar stuurt een catalogus (naam en soort van elke lamp en elk Tuya-toestel). De andere kant kiest wat ze gebruikt (`use` in `link.json`) en stuurt die keuze (`want`). Alleen daarvan gaat de stand over de lijn (elke seconde, alleen als er iets veranderd is), en alleen daarvoor aanvaardt de eigenaar een bevel: ook een installatie met de sleutel kan niet bedienen wat ze niet gekozen heeft. Niets wordt vanzelf gekozen; bij de overgang van 0.12.0 blijft gekozen wat al op het schema of op een kaart stond. De catalogus wordt bewaard (`known`), zodat de blokken na een herstart of tijdens een uitval blijven staan, als offline.
- **Versie**: de handdruk draagt de versie van het protocol. Een andere versie wordt geweigerd met de melding dat beide add-ons bijgewerkt moeten worden, in plaats van half te werken.
- **Bevelen**: de eigenaar voert uit en beslist met dezelfde regels als in de eigen UI (een vastgehouden lamp weigert; een Tuya-datapunt alleen binnen zijn datamodel) en antwoordt met `ack`. Zonder verbinding gaat er niets weg en wordt niets bewaard voor later: een bevel van gisteren mag morgen niet alsnog een laadpaal starten.
- **Kringen over installaties** (protocol 3, 0.14): een installatie biedt ook haar **gangen** aan (`e:<HA-entiteit>`: de gangen van haar schakelaars uit HA, externe apparaten, Tuya-relais en NSPanel-relais, met stand en of ze bezet zijn). Een gekozen gang is bij de andere een blok met touch en relais, en lid van een kring als `r:<id>:e:<entiteit>`. De kring draait waar hij gedefinieerd is: `sync.py` behandelt zo'n lid zoals een HA-entiteit, alleen komt de stand via de koppeling (`linkhub._feed` -> `engine.ha_state`) en gaat een bevel naar de eigenaar (`engine.call_entity` -> `remote_call`). De eigenaar duwt een wijziging van een gekozen gang meteen (een HA-luisteraar), niet pas bij de volgende tik: het is een druk. De echo van een bevel wordt herkend zoals bij HA (PENDING_FOR). Een licht op het netwerk kan geen remote gang als trigger hebben: dat beslist de node zelf.
- **Uitval en herstel**: zonder koppeling is het lid niet beschikbaar en wordt het overgeslagen; alles in elk subnet werkt door. Komt een remote lid terug (`prev None`, eerst niet beschikbaar), dan zet `sync._align` elke touch van die kring op de stand van de lamp-relais. De lamp-relais zelf nooit, en alleen als de kring actief is en de stand van de lamp bekend is: de lamp heeft gelijk, er gaat niets vanzelf aan of uit. Dat is de enige plek waar de add-on na een terugkeer iets rechtzet; voor leden in het eigen subnet blijft de regel uit §20. Een remote gang die net aan een kring gehangen wordt, krijgt zo meteen de stand van de lamp (bij elke modelwijziging worden de remote standen opnieuw gegeven).
- **Eén eigenaar per entiteit**: `_conflicts` kan niet over installaties kijken. De eigenaar meldt daarom welke gangen in een eigen actieve kring zitten (bezet) en weigert er bevelen voor.
- **Kaarten**: een rasterknop kan `r:<id>:g:...` of `r:<id>:l..` zijn, een laadpaal `r:<id>:y:...`. `cards.Live` leest ze uit de koppeling en stuurt een druk naar de eigenaar; de naam van een remote lamp komt live mee.

### Niet getest / open
- Getest: de berichten (herhalen, terugkaatsen, knoeien, een andere sessie, een andere versie), twee hubs over echte TCP op localhost (catalogus, kiezen, alleen de stand van wat gekozen is, bevelen, een bevel voor iets niet-gekozens geweigerd door de eigenaar, een wijziging die vanzelf meegaat, uitval, een verkeerde sleutel, twee verbindingen), de overgang van 0.12.0, de laadpaal van een andere installatie op een kaart, en twee add-ons naast elkaar met de pagina in een browser (koppelen via Instellingen, remote blok aan een kaartknop, bevel, uitval).
- Kringen over installaties (0.14): getest met twee hubs over echte TCP en een nagebootste HA aan elke kant: een druk op een touch daar schakelt de lamp hier, de touch volgt de lamp, uitval (de lamp blijft), herstel (de touch neemt de stand van de lamp over, de lamp gaat niet vanzelf), bezet. En in een browser met twee add-ons: een gang kiezen en met een draad op een lamp aansluiten. Niet met echte schakelaars: hoe snel een druk over de koppeling overkomt, en wat een touch doet als hij tijdens een bevel ook ingedrukt wordt.
- Niet getest: twee echte HA's in twee subnetten met een router of firewall ertussen, en een lange uitval.
- Aangeboden worden alle lampen, gangen en Tuya-toestellen; de eigenaar kan (nog) niets uit zijn aanbod weglaten. Niet aangeboden: timers, modules en losse HA-entiteiten. Een licht op het netwerk (ESP-NOW) kan geen gang van een andere installatie als trigger hebben, en een timer of module kan geen lamp van een andere installatie blokkeren.

## 30. Waarden van Tuya-toestellen (0.15)

Code: `points.py` (een datapunt lezen en zetten, hier of bij een andere installatie), `logic.py` (`dp`, `above`, `below`), `actions.py` en `model.actions` (Tuya instellen, blok `a:<id>`), `cards.py` (een waarde op een kaart).

- **Eén plek**: `points.Points` kent een toestel als `<id>` (hier) of `r:<installatie>:y:<id>` (gekozen bij een andere) en geeft de punten zoals de zijbalk ze toont, met de schaal al toegepast. De logica, de routine en de kaarten gebruiken alleen dat; de engine krijgt het als `engine.points`.
- **Logica**: `dp` geeft een getal (aan/uit = 1/0), onbekend als het toestel offline is of de waarde geen getal. `above`/`below` vergelijken met een grens met decimalen (`number` blijft een geheel getal, want de sprinkler rekent ermee). Geen hysterese: de logica heeft geen toestand (§23). Het klapperen rond een grens vangt de routine op met haar interval.
- **Tuya instellen** is een routine en geen module: ze heeft geen uitgang maar doet iets. Ze handelt op een **wijziging** van de ingang ten opzichte van wat ze laatst zette of stil volgde, nooit op een stand: de eerste bekende stand (opstarten, terug bekend, net aangezet) wordt alleen onthouden. Daarbij hoogstens één bevel per `interval`, ook bij een mislukking. Zo kan een herstart, een weggevallen verbinding of een schommelende meter een lader niet starten of laten klapperen. Wat al gezet is, staat alleen in het geheugen: dat is de bedoeling.
- **Poorten op het Tuya-blok** (0.15.1): rechts een waarde, links een instelling, ook op een Tuya-blok van een andere installatie. Een ingang van een module of sprinkler kan rechtstreeks een datapunt zijn (`v:<dp>@<toestel>`, soort getal), zonder module Tuya-waarde ertussen. Een draad van een 0/1 naar een instelling maakt een routine Tuya instellen, die uit staat: de waarden kiest de gebruiker, er wordt niets geraden.
- **Kaarten**: een knop `{"ref": "y:<id>"|"r:..:y:<id>", "dp": "19"}` en een energierij `{"tuya": ..., "dp": ...}` zijn rijen `kk.<kaart>__<n>` / `__p<n>` (zie §26): de waarde, de naam van het datapunt en (energie) de kleur uit de grenzen komen uit `cards.Live`, niet uit een sjabloon van HA.

### Niet getest / open
- Getest met unit-tests (`test_values.py`: logica, model, de routine stap voor stap, kaarten hier en van een andere installatie) en met de add-on en een nagebootste lader en stroommeter in een browser: de keten Tuya-waarde → Groter dan → Tuya instellen zette de laadstroom op 8 en 16 A, niet vaker dan het interval.
- Niet tegen de echte lader en meter. Niet ondersteund: hysterese, een waarde vergelijken met een andere waarde, een enum vergelijken (alleen getallen en aan/uit), een datapunt als tekst op het Energie-schema.

## 31. Airco's met een Gree-module (0.17)

Code: `gree.py` (protocol en verbinding), `greehub.py` (toestellen, ingangen, uitgangen, entiteiten), blok `w:<id>` (`schema.py`, `static/schema.js`), `tests/test_gree.py`.

- **Waarom**: de Toyotomi-airco liep via een HACS-repository (de Gree-integratie). Zelf doen haalt die weg en maakt van de airco een blok met poorten, zoals de rest van het huis.
- **Eigen blok, niet via Tuya**: een airco is geen toestel met datapunten maar heeft een vaste set eigenschappen. Ze staat dus los: eigen opslag (`/data/gree.json`, 0600), eigen blok `w:<id>`, eigen zijbalk, eigen `climate`-entiteit. Wat ze met de rest deelt, gaat via de bestaande bronnen: de uitgangen zijn `w:<id>:on|room|set` (`model.SOURCE_RE`, `logic.evaluate`, `engine.ref_value`), te gebruiken op elke ingang van een module, sprinkler of routine.
- **Protocol**: JSON over UDP 7000, de inhoud AES-128-ECB (v1) of AES-128-GCM met vaste nonce en AAD (v2), zoals greeclimate. Binden met de algemene sleutel geeft de sleutel van het toestel; `auto` probeert v1, dan v2, en bewaart wat werkt. Zonder MAC vraagt de client eerst `scan` aan het IP. Een Gree-module meldt niets uit zichzelf: elke 10 s een statusvraag, drie keer geen antwoord is offline. Zoeken gebeurt van een eigen poort (UDP 7000 is van de Tuya-luisteraar; de antwoorden gaan naar de poort die vroeg).
- **Kamertemperatuur**: de meeste modules tellen 40 bij `TemSen`, nieuwere niet. Binnen is het tussen 0 en 40 °C, dus boven 40 betekent met verschuiving; 0 is geen sensor.
- **Ingangen** handelen op een wijziging, zoals Tuya instellen (§30): de eerste stand wordt onthouden, onbekend wist het geheugen. Zo zet een herstart de airco niet aan of uit en wint de afstandsbediening tot de ingang verandert. Blokkeren wint van aan. Pas na een mislukt bevel wordt 30 s gewacht; een geslaagd bevel houdt de volgende wijziging niet op.

### Niet getest / open
- Getest met unit-tests (versleuteling beide versies, een nagebootste airco over echte UDP: MAC opzoeken, binden v1 en v2, status, bevel, stil toestel, verkeerde sleutel; de hub, MQTT, ingangen stap voor stap, uitgangen in de logica, de web-API) en met de add-on en een nagebootste airco in headless Chrome (blok, zijbalk, bevel, draden, een ingang die verandert).
- Niet tegen de echte Toyotomi: welke versleuteling, of `TemSen` klopt, en welke schakelaars ze echt kent (wat ze niet meldt, staat niet in de zijbalk maar wel als entiteit in HA).
- Niet ondersteund: halve graden (`TemRec`), Fahrenheit, VRF-binnenunits achter één module (`subCnt`), en een airco van een andere installatie (de koppelingen delen nog geen airco's).

## 32. Omvormers met een Solarman-stick (0.22, uitgebreid in 0.23)

Code: `solarman.py` (V5-protocol en client), `solarhub.py` (toestellen, uitgangen, sensoren), blok `o:<id>` (`schema.py`, `static/schema.js`), `tests/test_solar.py`.

- **Waarom**: de omvormers in het subnet van de tweede installatie werden vroeger via de Solarman-integratie gelezen. Zelf doen maakt er een blok met uitgangen van, zoals de airco (§31).
- **Eigen blok, alleen uitgangen**: `o:<id>:<naam>` (`model.SOURCE_RE`, `logic.evaluate`, `engine.ref_value`, allemaal getallen). Eén tabel, `solarman.FIELDS` (54 waarden in 6 groepen), bepaalt wat we lezen, de uitgangen en de HA-sensoren. Het blok toont er standaard zes (`DEFAULT_SHOWN`); `shown` per omvormer kiest de rijen, de blokdata heeft `outs` (getoond) en `all` (voor de bronlijsten van modules). Offline is onbekend. Er zijn **geen ingangen**: een schrijfbevel naar een omvormer is niet terug te draaien als het register niet klopt.
- **Protocol**: Modbus-RTU in een V5-frame over TCP 8899. De stick antwoordt met de lage byte van ons volgnummer (de hoge telt hij zelf) en stuurt ook hartslagen; we letten op controlecode 0x1510 en die byte, en op het serienummer van de stick. Elke ronde een nieuwe verbinding (de stick heeft er maar een paar). Drie ronden zonder antwoord is offline.
- **Registers**: Deye SG04LP3 (lijst van de Solarman-integratie), nagelopen op een echte omvormer: zon 0x02A0/1, accu 0x024B-0x024F, net 0x0271, verbruik 0x028D, energie 0x0202-0x0211. De omvormer meldt accu laden negatief; `batt` draait dat om. Balans gemeten: zon 5137 W = verbruik 1206 + laden 3778 + net 9 + verlies.
- **Ontdekking** (0.23.1): UDP 48899, vraag `WIFIKIT-214028-READ`, antwoord `ip,mac,serienummer` (zonder wachtwoord; de stick meldt zijn eigen adres, een antwoord van een ander adres telt niet). Net als bij de airco: eigen poort, elke 60 s zolang een omvormer geen IP heeft of onbereikbaar is, een keuzelijst met niet-geclaimde sticks, een leeg IP invullen en een nieuw DHCP-adres alleen overnemen als het oude niet bereikbaar is. Een blok mag zonder IP en serienummer bestaan (het menu maakt het zo aan); het start pas als beide er zijn.
- **Serienummer van de stick** staat op `status.html` (`cover_mid`, basic auth, standaard admin/admin); `lookup` haalt het op en bewaart de login niet.

### Niet getest / open
- Alleen een SG04LP3 (met accu). Een stringomvormer (Deye string, Solis, ...) heeft andere registers en zou een eigen tabel nodig hebben.
- Niet in HA zelf gedraaid (MQTT-discovery getest met unit-tests). Niet ondersteund: schrijven, derde MPPT, alarmen. Netfrequentie (0x027E) en generatorpoort (0x029B/0x029F) volgen de Solarman-lijst of een meting; de generator stond op 0 en is dus niet gecontroleerd.
- 32-bits tellers (totaal) zijn laag-register-eerst; gecontroleerd op totaal opbrengst (16976 kWh) en geleverd (6400 kWh) van de echte omvormer.
- **Energiekaart: ingangen** (0.24.0): elke rij van een energiekaart heeft links een ingang (`cpin`) op het blok, plus een vrije om een rij erbij te maken. Sleep een waarde (rechts op een Tuya-blok) erheen, of laat hem los op het blok voor een nieuwe rij; dat zet `tuya` en `dp` van de rij. Op het blok staat per rij `naam ← bron`, in de zijbalk per rij "ingang: Tuya · …", "Home Assistant · …" of "nog niets aangesloten".
- **Op de panel zetten met één knop** (0.25.0): `tasmota.install` doet via de webserver van Tasmota wat je vroeger met de hand deed: `POST /ufsu` (multipart, veld `ufsu`) voor `huisconfig.be`, `GET /ufsd?download=/autoexec.be`, de regel `load("huisconfig.be")` er één keer achter en weer uploaden, dan `/cm?cmnd=Restart 1`. Een webwachtwoord gaat als Basic-auth `admin` mee en wordt niet bewaard. Het paneel onthoudt `hub` (het adres van HA zoals de panel het ziet; leeg = het adres van de browser). Niet getest tegen een echte panel, alleen tegen een nagebootste Tasmota: de paden komen uit xdrv_50_filesystem.

## 33. Laadpalen met sessies (0.28)

Code: `chargehub.py` (sessies, log, wat wie ziet, omzetting), blok `k:<id>` (`schema.py`, `static/schema.js`), `linkhub.py` (aanbieden en bevelen), `cards.py` (de kaart wijst alleen nog), `tests/test_charge.py`.

- **Waarom**: de Feyree-lader hangt in één subnet en wordt vanuit andere huizen gestart. De eigenaar moest weten wie wanneer laadde, en een ander huis mocht een lopende sessie niet zien of stoppen. Met de ruwe datapunten over de koppeling (§29) kon dat niet: het bevel `124=OpenCharging` zegt niet wie het gaf.
- **Eigen blok, eigen opslag**: `k:<id>` met `/data/charge.json` (instellingen, lopende sessies, log van 500, `migrated`). De instellingen die op de kaart stonden (datapunten, start/stop-waarden, statuslijsten, tarief) staan nu op het blok; de kaart (`charger: k:<id>` of `r:<inst>:k:<id>`) toont en bedient alleen. Het id is een functie van het Tuya-toestel (`charger_id`), zodat een kaart bij een andere installatie bij de omzetting zonder overleg naar het juiste blok wijst.
- **Eén eigenaar van de waarheid**: een sessie bestaat alleen bij de eigenaar. De starter is `conn.peer` van het bevel (niet iets wat het bericht zelf beweert), `""` voor de eigenaar zelf, `"-"` voor laden dat niet via de add-on begon (`tick` merkt het op uit de status). Start weigert bij een lopende sessie of een status die geen start toelaat; stop weigert voor iedereen behalve de starter en de eigenaar.
- **Verbergen aan de bron**: `view(cid, viewer)` bouwt per ontvanger wat er over de lijn gaat (`linkhub.export(peer)`, `_push` per verbinding). Tijdens een sessie van een ander is dat `{phase: "busy", act: None}` en niets meer; de naam van de starter staat alleen in de weergave van de eigenaar. Het Tuya-toestel van een laadpaal wordt niet aangeboden (catalogus, stand, gangen) en `_execute` weigert er bevelen voor, anders omzeilt een installatie met de sleutel het blok. Protocol 4: een andere versie wordt geweigerd.
- **Einde van een sessie** (`tick`, elke seconde): de laadpaal heeft geladen en meldt het niet meer gedurende 20 s ("klaar of losgekoppeld"); of ze laadde nooit binnen 180 s na de start ("niet gestart"). Een offline laadpaal beslist niets: niets weten is niet gestopt. Een verbroken koppeling met de starter doet niets: de sessie loopt door. Een herstart houdt haar (`active` staat in het bestand).
- **kWh**: meterstand nu min meterstand bij de start. Daalt de meter tijdens de sessie, dan is de teller opnieuw begonnen en geldt 0 als basis. Kosten = kWh × tarief bij het einde (de HA-entiteit wint, zoals bij de kaart vroeger).
- **kWh bij het totaal van de laatste beurt** (0.45.2): de Feyree meldt op dp 25 (`charge_energy_once`, "Energie laatste beurt") het totaal van de vorige beurt, tijdens de sessie constant, en het nieuwe totaal pas na afloop (soms na de 20 s van het einde). Het profiel herkent hem aan de code van het punt (`_once`). Dan is de kWh de nieuwe waarde zelf (> 0 en ≠ de waarde bij de start), niet min de oude, en onbekend (`None`, geen 0,00) zolang die niet komt. Een afgesloten sessie die geladen heeft en nog geen totaal heeft, krijgt `wait` (einde + 900 s) en `from` (de oude waarde); `_settle` (in `tick`) vult kWh en kosten in zodra het punt verandert, en laat het na `wait` als niet gemeten staan. Een nieuwe start sluit het wachten af (`_drop_wait`), zodat een volgend totaal niet bij een oude sessie belandt. Het log toont zolang "wordt gemeten…". Een lopende teller (andere lader) werkt als hiervoor.
- **De kaart op het paneel** (0.47.1, 0.48): een kaart zonder stroomregelaar is een `cardGrid` van vier tegels (`run`, `time`, `energy`, `price`); een Smappee-laadpaal met `amps` blijft een `cardEntities`-lijst, want een regelaar past niet op een tegel. De tekstgrootte ligt vast in het Nextion-scherm; op een tegel is het "pictogram" het grote element. `Live._charger` geeft daarom per rij een `icon` (de actie: `play`, `pause`, `stop`, of `ev-station`) en voor tijd, kWh en prijs een `glyph` (korte tekst: `H:MM`, `2.50`/`12.5`/`123`, `-` als er niets is), die `panel._dynamic` als pictogram stuurt zolang de rij zelf geen `icon` heeft (de lijst houdt dus haar vaste pictogrammen), zoals `_short` bij een sensor. Kleuren: `CHARGE_COLOR` groen (starten, hervatten, lopende sessie), `PAUSE_COLOR` oranje, `STOP_COLOR` rood. Een scherm met openHASP negeert `icon` en `glyph` en toont naam en waarde. Tijdens een sessie van een andere installatie (`phase: busy`) toont de run-tegel *Sessie bezig* (groen, tekst, geen knop) en de rest een streepje (lijst: *Bezet*); de gegevens blijven verborgen (`view`). De pictogrammen `play`, `pause` en `stop` in `panel_icons.py` zijn afgeleid uit het MDI-codepunt en niet op de TFT gezien; staan ze er niet in, dan zet `panel_icons.json` in de datamap ze goed.
- **Het blok toont het scherm** (0.50.0): het kaartblok van een laadpaalkaart in de huisconfig toont de rijen die het scherm toont (status en START/STOP/PAUZE/HERVAT, laadtijd, geladen kWh, prijs, en de stroom bij `amps`), live en met dezelfde woorden. `cards.charger_part` is de ene bron: `Live._charger` (het scherm) en `cards.charger_preview` (`schema._card_blocks`, veld `screen`) roepen hem allebei aan, dus ze kunnen niet uit elkaar lopen. De preview is Nederlands (`nl`), niet de taal van het paneel of scherm waar de kaart aan hangt. Bij een laadpaal van een andere installatie geldt de lijn: is die weg, dan toont het blok *Laadpaal offline*, zoals het scherm.
- **Feyree: lopende teller op dp 112** (0.50.1): `DeviceKwh` ("Energie totaal") is geen levensduurtotaal maar telt de kWh van de huidige beurt, en `Ctime` (dp 120) de tijd ervan; bij het loskoppelen gaat de teller terug op 0 en komt het totaal in dp 25. `_live` kiest daarom, staat de kWh-ingang op dp 25 (`charge_energy_once`) en heeft het toestel dp 112, die teller (nieuwe laadpalen krijgen 112 als ingang). `_sample` negeert een daling nadat de laadpaal geladen heeft en niet meer laadt (het loskoppelen binnen de 20 s voor het einde), zodat de kWh blijft staan in plaats van op 0,00 te vallen; een daling tijdens het laden of voor het laden begon is een nieuwe teller (base 0). Zegt de teller niets (0), dan wacht het log zoals bij 0.45.2 op dp 25 (`fin`, `_last_turn`, `final_energy`). Meldt de laadpaal een laadtijd die leeg of nul is terwijl ze laadt, dan telt `_time` zelf vanaf `began` (het moment waarop laden voor het eerst gezien werd).
- **Omzetting** (`adopt`, bij het opstarten): een kaart met `legacy` (de oude velden, door `model._card` bewaard tot dan) wordt een blok `k:<toestel>` of een verwijzing `r:<inst>:k:<toestel>` (en die wordt hier gekozen via `link.use_unchecked`, want de catalogus is er nog niet). Een Feyree-lader zonder kaart krijgt eenmalig een blok. Ingangen van een ander toestel dan dat van start/stop worden losgelaten (met een waarschuwing in de log).
- **HA**: per laadpaal drie MQTT-sensoren (`Laadsessie`, `Laatste sessie`, `Laatste sessie door`).

- **Tabblad Laden (0.29)**: `GET /api/charge/sessions` geeft de hele log (max 500), de namen van de laadpalen, de lopende sessies en de naam van deze installatie. Filter, totalen, CSV en factuur zijn client-side (`static/charge.js`): een factuur is de sessies van één huis (`by`) met start in de periode, de som van de bedragen uit de log, afgedrukt via de printfunctie van de browser (CSS `@media print`). Geen factuurnummering of opslag van facturen: de log is de bron.

### Niet getest / open
- Getest met unit-tests (regels voor start en stop, verbergen, einde van een sessie, de meter die opnieuw begint, herstart, omzetting, de web-API en csv) en met drie hubs over echte TCP: een huis start, een ander ziet alleen bezet en kan niet starten, stoppen of langs het datapunt, de starter valt weg en de sessie loopt door tot de laadpaal klaar is.
- Niet tegen de echte Feyree: of `charing` ook echt de status tijdens het laden is, of de meter (25) per sessie opnieuw begint, en of de laadtijd (120) tijdens het laden loopt.
- Niet in een browser bekeken: het blok, de zijbalk en de log in het schema.
- De kaart toont de tijd uit het laadtijd-datapunt van de laadpaal, niet de duur van de sessie.
- Een starter die weg blijft houdt de laadpaal bezet tot de laadpaal klaar is of de eigenaar stopt.


## 34. Zelfbouwapparaten met HUISDEV (0.30)

Code: `espdevhub.py` (ontdekking, claimen, poll, punten, ingangen, MQTT), blok `e:<id>` (`schema.py`, `static/schema.js`), `protocol/huisdev/` (de spec `HUISDEV.md` en de referentie-header `huisdev.h` voor de ESP's), `tests/test_espdev.py` met een nagebootst apparaat naar de spec (`tests/fakehuisdev.py`).

- **Waarom**: eigen ESP-sketches (poolcontroller, peilmeter, tuinbesturing) hadden elk hun eigen webpagina en JSON. Met één protocol meldt een ESP zich, beschrijft zichzelf en krijgt hij een blok, zonder dat er per apparaat code in de add-on komt.
- **Protocol** (zie `protocol/huisdev/HUISDEV.md`, versie 1.0): UDP 47500 voor ontdekking (vraag `HUISDEV?1.0`, antwoord en aankondiging als JSON), daarna HTTP/JSON: `GET /huisdev/descriptor` (punten en acties, met een `rev` die verandert als de beschrijving verandert), `GET /huisdev/state`, `POST /huisdev/set`, `/do`, `/claim`, `/unclaim`. De add-on pollt de state elke 3 s; drie mislukkingen is offline.
- **Claimen, geen automatisch blok**: een gevonden apparaat staat als "niet geclaimd" in het menu *+ Module → Zelfbouw (HUISDEV)* (`S.espdev_found`, ook als er nog geen blok is). Claimen haalt de descriptor, maakt een token (32 bytes hex), laat het apparaat dat bewaren en zet het blok op de pagina. Het token staat in `/data/espdev.json` (0600), nooit in het model, de export of de pagina. Een apparaat dat door iets anders geclaimd is, is zichtbaar maar niet te koppelen; een onbekende major (protocol 2.x) ook niet.
- **Het id**: het apparaat heet `poolcontroller-a1b2c3` (streepjes), de add-on gebruikt `poolcontroller_a1b2c3` want een bron kent alleen `[a-z0-9_]`.
- **Bronnen**: `e:<id>:<punt>` (`model.SOURCE_RE`, `logic.evaluate`, `engine.ref_value`). Het type volgt de descriptor: bool is een 0/1, int, float en enum zijn een getal, tekst is geen bron. Omdat dat type niet in het model staat, hangt de hub `model.esp_type` aan; een verwijzing naar een apparaat of punt dat er niet is, krijgt het type `ANY` (`model.fits`) en geeft onbekend, nooit een fout. Zo blijft het model bewerkbaar en laadbaar als een apparaat weg is. Alleen `espdevhub.py` kent `esp_type`; `close()` haalt hem weer weg.
- **Blok**: een rij per getoond punt (`shown`; standaard de eerste acht, zonder tekst en diagnose, en bevroren zodra er punten komen of gaan zodat het blok niet verspringt). Links een ingang als het punt instelbaar en niet `danger` is, rechts de uitgang. `ports` en `outs` hebben elk een `row`. `all` bevat elk punt dat een bron kan zijn, voor de bronlijsten van modules.
- **Ingangen** volgen de airco (§31): alleen bij een **wijziging**, de eerste stand na een herstart of na onbekend wordt onthouden, altijd absoluut, 30 s wachten na een mislukking. Het versturen is async (HTTP), met een `_inflight` per punt. Is het apparaat offline, dan wacht de wijziging tot het terug is, zonder dat dat als mislukking telt. Een getal van een bron wordt op bereik en stap gezet (`_snap`); een enum-waarde zonder keuze wordt niet verstuurd. Een eigen bron van het apparaat zelf is onbekend zolang het offline is, dus die telt na terugkeer als eerste stand.
- **Gevaarlijke punten en acties** (`danger`): alleen met de hand uit de zijbalk, met `confirm`; de hub weigert ze zonder bevestiging, de logica kan ze niet koppelen (`_inputs`), HA krijgt ze alleen als sensor en MQTT-commando's ervoor worden geweigerd.
- **Dynamische punten**: een andere `rev` in de state haalt de descriptor opnieuw (ook een aankondiging met een nieuwe `rev` stoot de poll aan). Nieuwe punten komen erbij. Een verdwenen punt blijft 30 dagen "niet beschikbaar" (`gone`): de verwijzingen blijven staan en geven onbekend, de HA-entiteit wordt `unavailable` via een eigen beschikbaarheids-topic per punt. Daarna of bij loskoppelen gaan de topics op een **bewaarde wislijst** (`retired` in het bestand); de MQTT-brug (`retire()` in `mqttbridge._sync_discovery`) wist ze met een leeg retained bericht, ook na een herstart, want `_published` overleeft er geen. Een punt dat van type wisselt (verboden door het protocol) telt als nieuw en verliest zijn koppeling.
- **HA**: per punt een entiteit gekozen zoals bij Tuya (bool schrijfbaar: `switch`, anders `binary_sensor`; enum schrijfbaar: `select`; getal met bereik schrijfbaar: `number`; de rest `sensor`, met eenheid en klasse als hint; `ph` zonder eenheid), `unique_id` `kk_esp_<id>_<punt>`. Een knop per actie zonder argumenten die niet gevaarlijk is.
- **Versies**: `CURRENT` is het protocol zoals de add-on het kent. Een lagere minor van het apparaat geeft "upgrade beschikbaar" in de Nodes-tab en de zijbalk (met een verwijzing naar de upgrade-gids), een nieuwere minor wordt gewoon gelezen (onbekende velden en onbekende `kind`-waarden worden overgeslagen), een onbekende major is "niet ondersteund".
- **Web-API**: `POST /api/espdev/claim` (met `page`), `POST /api/espdev/scan`, `PUT /api/espdev/<id>` (naam, `shown`, `inputs`, `host`), `POST /api/espdev/<id>/set` en `/do`, `DELETE /api/espdev/<id>` (geeft ook de claim op het apparaat vrij, lukt dat niet dan wist u hem op het apparaat). Gebeurtenis `espdev` voor de pagina.

### 0.30.1: bevindingen bij de eerste echte poolcontroller
- **De hele body lezen.** `r.content.read(65536)` geeft terug wat er op dat moment binnen is, niet het hele antwoord. Een ESP stuurt zijn descriptor (3 KB) over wifi in meerdere TCP-segmenten, dus een halve beschrijving kon "geen geldige beschrijving" geven. Nu `iter_chunked` met een bovengrens (`MAX_BODY`), en een totale time-out van 5 s. Op localhost viel dit nooit op (test: `test_espdev_slowbody.py`, een server die in stukjes schrijft).
- **De melding zegt waarom.** Voorheen was alles (HTTP-status, geen JSON, afgekapte JSON, ontbrekende velden, ander id) dezelfde tekst. `_fetch_descriptor` noemt nu de oorzaak (`HTTP 500`, `geen leesbare JSON (… bytes, begint met …)`, `proto ontbreekt`, `device.id is ongeldig`, `op <ip> meldt zich nu <id>, niet <id>`) en logt een mislukte koppeling met adres.
- **Waarden die alleen bij wijziging komen.** De poolcontroller stuurt een waarde niet opnieuw zolang hij gelijk blijft: pH was na 14 minuten nog steeds van de eerste dump. Een `stale`-hint op zulke punten maakt ze ten onrechte onbekend. Zie `HUISDEV.md` §5 en §10: `stale` alleen voor periodiek ververste waarden.

### Niet getest / open
- De claim, de poll en de uitgangen zijn **wel** getest tegen een echte poolcontroller (ESP32-C6, via HTTP vanuit de testcontainer). Niet getest: de ontdekking (UDP) vanuit de container (Docker Desktop doet NAT), en het geheel in een echte Home Assistant. De tests zelf gebruiken een nagebootst apparaat dat naar de spec is geschreven, door dezelfde hand als de add-on.
- Niet in een browser bekeken: het blok, de zijbalk en het menu zijn alleen met een Node-script op echte data getekend (geen foutteksten of `undefined`, juiste poorten en acties).
- Niet in HA zelf gedraaid (MQTT-discovery met unit-tests). Of HA `suggested_display_precision`, `device_class: enum` en een `ph`-sensor zonder eenheid in alle versies accepteert, is niet nagelopen.
- Het token gaat in gewone HTTP over het LAN (geen TLS). Het beschermt tegen vergissingen, niet tegen iemand die meeluistert.
- Geen tekstpunten als bron, geen lijsten of tijdreeksen, geen gebeurtenissen en geen push (zie `HUISDEV.md` §14).


## 35. ESPHome-toestellen als HUISDEV-apparaat (0.31)

Code: `esphome.py` (puur: de eventstroom lezen, entiteit naar punt of actie, waarden, opdracht naar REST-verzoek), de ESPHome-kant van `espdevhub.py` (`add_esphome`, `_es_read`, `_run_esphome`, `_es_flush`, `_es_send`), `tests/test_esphome.py` met een nagebootst toestel (`tests/fakeesphome.py`, naar een echte opname van een stopcontact).

- **Waarom**: elk ESPHome-toestel dat geen schakelaar of licht is (stopcontact met vermogensmeting, sensor, ventilator, een toestel met keuzes en getallen) viel buiten de HA-import van §18, die alleen `switch`, `light`, `input_boolean` en `fan` kent. In plaats van een nieuw soort blok is een ESPHome-toestel een **tweede driver** van het HUISDEV-blok (`dev["driver"]`: `huisdev` of `esphome`): blok `e:<id>`, zijbalk, bronnen, ingangen, `shown`, `danger`, het verdwijnen van punten en de HA-entiteiten zijn dezelfde code. Alleen het ophalen (stroom in plaats van poll) en het schrijven (REST in plaats van `/huisdev/set`) zijn anders.
- **Geen aanpassing aan het toestel**, wel `web_server:` in zijn YAML (dat staat bij de meeste toestellen al aan). Toevoegen: *+ Module → ESPHome-toestel toevoegen*, het IP-adres (`POST /api/espdev/esphome`, `host`, optioneel `port`, `user`, `password`, `name`, `page`). Er is niets te claimen en dus niets in te trekken bij verwijderen.
- **Lezen**: `GET /events` (server-sent events) blijft open. Bij het verbinden stuurt ESPHome een `ping` met de titel en per entiteit een **volledige** regel (`name`, `uom`, `option`, `min_value`, ...), daarna alleen deelregels (`id`, `value`, `state`) en logregels. De descriptor komt alleen uit volledige regels, de waarden uit alle. Een poll per entiteit was geen optie: dat zijn tientallen verzoeken per ronde voor een BK7231N. De stroom is de bron van "online": valt hij weg (sluiting of 35 s zonder byte; ESPHome pingt elke ~10 s), dan is het toestel meteen offline en elke waarde onbekend, nooit de laatste gok.
- **Eerst afwachten, dan verwerken**: na het verbinden wacht de hub `ES_SETTLE` (1 s) zodat de eerste stroom volledig is; anders groeit de descriptor in stukjes en bevriest `shown` op een willekeurig deel. Daarna wordt hoogstens elke `ES_FLUSH` (1 s) verwerkt (een vermogensmeting komt twee keer per s en elke wijziging is een `_poke` naar pagina en MQTT).
- **Omzetting** (`esphome.describe`): `sensor` is een `float` (eenheid uit `uom`; de decimalen zijn die van de tekst die ESPHome zelf toont, `0.046 A` is 3), `binary_sensor` een alleen-lezen `bool`, `text_sensor` een `text`, `switch`, `light` en `fan` een schrijfbare `bool` (alleen aan/uit), `select` een `enum` (de waarde is de plaats in de lijst), `number` een `int` of `float` met bereik en stap, `text` een schrijfbare `text`, `button` een actie. `entity_category` 2 is `diag`, 1 komt in de groep *instellingen*. Een knop met `restart`, `reboot`, `reset`, `factory`, `safe mode`, `shutdown` of `deep sleep` in id of naam is `danger` (alleen met de hand, met bevestiging, nooit een draad). Eenheden worden alleen een HA-klasse als ze niets anders kunnen betekenen (`W`, `V`, `A`, `Wh`, `kWh`, `VA`, `°C`, `Hz`, `hPa`, `dBm`, `lx`; een `%` laten we weg). `Wh`/`kWh` krijgen `total_increasing`. **Overgeslagen** (en zichtbaar in de zijbalk als "niet ondersteund"): `cover`, `climate`, `lock`, `valve`, `date`, `time`, `event`, `update`, `media_player`, een `select` zonder keuzes.
- **Ids zijn vast**: een ESPHome-id (`sensor-power`) wordt een punt-id `[a-z][a-z0-9_]{0,23}` (de naam na het eerste streepje, schoongemaakt; bij een botsing met een ander domein `<domein>_<naam>`; anders met een korte hash). De toewijzing staat in `dev["emap"]` (punt-id naar ESPHome-id) en verandert nooit meer: een entiteit erbij of de volgorde die wisselt geeft een ander punt nooit een ander id (HUISDEV §9). Een verdwenen punt houdt zijn toewijzing 30 dagen (`gone`), zodat zijn id niet voor iets anders wordt gebruikt. De volgorde van de punten is die van het toestel, dus zijn belangrijkste entiteiten staan vooraan in het standaardblok.
- **Schrijven** (`esphome.point_request`): altijd absoluut (`turn_on` of `turn_off`, nooit `toggle`), `select/<id>/set?option=<tekst>`, `number/<id>/set?value=`, `text/<id>/set?value=`, `button/<id>/press`. Het antwoord is alleen "gedaan" (HTTP 200); de nieuwe waarde komt over de stroom, zoals bij HUISDEV (de waarheid is de state). Waarden worden vooraf gecontroleerd op type, keuze, bereik en stap (`check_value`).
- **Home Assistant: standaard uit** (`dev["ha"]`). Een ESPHome-toestel staat meestal al in HA via zijn eigen integratie; dezelfde entiteiten nog eens via MQTT zouden dubbel zijn. In de zijbalk staat een vinkje *ook als entiteiten in Home Assistant*; uitzetten wist ze weer (via de bewaarde wislijst, §34). Voor HUISDEV-apparaten is `ha` altijd aan.
- **Wachtwoord**: staat `web_server: auth:` aan, dan geef je gebruiker en wachtwoord bij het toevoegen. Ze staan als `gebruiker:wachtwoord` in `/data/espdev.json` (0600), nooit in het model, de status voor de pagina of de export. Basic-auth gaat in gewone HTTP over het LAN, net als het HUISDEV-token.
- **Bij de bouw gevonden**: `_point()` las de HA-klasse onder `class` maar bewaarde hem onder `cls`, dus na een herstart verloor elk punt zijn klasse (ook bij HUISDEV-apparaten, waar de descriptor niet opnieuw wordt opgehaald zolang de `rev` gelijk is). Een `power`- of `energy`-sensor kwam dan zonder `device_class` in HA en dus niet in het energiedashboard. Nu leest `_point()` beide, met een test.

### Niet getest / open
- Getest tegen een echt toestel (stopcontact `powerplug04tv`, BK7231N via LibreTiny, ESPHome 2026.x): toevoegen, de punten, eenheden, klassen en decimalen, en het schrijven van een `select` naar zijn eigen huidige waarde (de URL en parameter kloppen). **Niet** op echte hardware geprobeerd: schakelen van het relais, de restart-knop, `number`, `text`, `light` en `fan`, en een toestel met `web_server: auth:`. Die zijn alleen met het nagebootste toestel getest, dat naar de opname is gebouwd door dezelfde hand als de code.
- Het toestel moet een **vast IP** hebben (`host` is wat je intikt; er is geen mDNS-opzoeking en geen UDP-ontdekking zoals bij HUISDEV). Verhuist het, dan pas je het adres aan met `PUT /api/espdev/<id>` (`host`).
- Een nieuwe entiteit (nieuwe firmware met extra sensor) verschijnt pas bij het volgende verbinden, want ESPHome stuurt volledige regels alleen bij het verbinden.
- Alleen aan/uit voor `light` en `fan`: helderheid, kleur, snelheid en effecten laat de add-on aan HA.
- Een klein toestel (ESP8266, BK7231) heeft maar ruimte voor een paar gelijktijdige eventverbindingen (het exacte aantal is niet nagemeten). De hub houdt er één open; de eigen webpagina van het toestel en HA delen die ruimte. Gemeten op het echte toestel: een `ping` elke 10 s, dus de leestime-out van 35 s (`ES_READ`) laat ruim drie pings zitten.
- Niet in een browser bekeken: het menu-item, de zijbalktekst en het vinkje zijn alleen op syntaxis (Node) gecontroleerd.
- Bestaande falende tests op deze Windows-machine, ook zonder deze wijziging: `test_offline_after_three_misses_and_back`, `test_input_waits_while_offline_and_retries_after_a_failure`, `test_a_panel_that_reports_in_gets_its_screensaver_and_a_touch_opens_a_page` en `test_an_unreachable_panel_gives_a_readable_error`.

## 36. Philips Hue als HUISDEV-apparaat (0.32)

Code: `hue.py` (puur: resources lezen, apparaten herkennen, punten, waarden, opdracht naar verzoek; plus het netwerk: vingerafdruk, verzoek, koppelen), de Hue-kant van `espdevhub.py` (`add_hue_bridge`, `claim_hue`, `_run_hue`, `_hue_read`, `_hue_sync`, `_hue_flush`, `_hue_send`, `hue_view`), `schema.place_grid`, `tests/test_hue.py` met een nagebootste bridge over echte HTTPS (`tests/fakehue.py`).

- **Waarom rechtstreeks**: de bridge heeft een lokale API (CLIP v2) met een eventstroom; er is geen HA-integratie of HACS voor nodig, en de lampen hoeven niet in HA te staan. Net als bij ESPHome (§35) is Hue een **driver van het HUISDEV-blok** (`dev["driver"] == "hue"`): blok `e:<id>`, zijbalk, bronnen, ingangen, `shown` en de HA-entiteiten zijn dezelfde code.
- **Een blok per apparaat, geen blok per bridge.** Een bridge heeft dertig apparaten; een blok met zestig rijen is niet te gebruiken en past niet bij de ruimtes van het schema. De bridge is daarom een aparte plaats in `espdev.json` (`bridges`: adres, sleutel, vingerafdruk, bridge-id) met **één** taak (`hue:<bridge>`) en **één** eventstroom voor al zijn blokken. Een apparaat heeft `bridge` en `rid` (de UUID van het apparaat bij Hue).
- **Welke apparaten** (`hue.kind_of`): een `light` met `dimming` is een **lamp** (aan, helderheid 1–100), een `light` zonder `dimming` een **stopcontact** (aan), een apparaat met een `motion`-dienst een **melder** (beweging, temperatuur, lichtniveau in lux, batterij). Elk heeft `reach` als diagnose. Knoppen en dimmers worden niet aangeboden en staan als zodanig in `hue_view()["skipped"]`. Gemeten op een echte bridge (BSB002, API 1.78, 26 lampen, 2 melders): de stopcontacten komen binnen als `light` met archetype `classic_bulb` of `plug`, dus het archetype zegt niets (de gebruiker kiest het).
- **Ids liggen vast**: de punt-ids (`on`, `bri`, `motion`, `temp`, `lux`, `battery`, `reach`) staan in de code, het apparaat-id (`hue-3f9a1c2b7d`) komt van de eerste tien tekens van zijn UUID, **nooit** van zijn naam (op de echte bridge heten twee lampen allebei `Oprit11`). Een hernoeming in de Hue-app verandert dus niets aan een blok; de naam van het blok is die van de gebruiker.
- **Onbereikbaar is onbekend.** Een apparaat waarvan `zigbee_connectivity` niet `connected` is, heeft alleen `reach` (onwaar); alle andere punten zijn onbekend (`hue.values`). De bridge onthoudt de laatste stand van een lamp zonder netspanning (`on: false`, `bri: 9.88`), en dat is een gok. Op de echte bridge was dat in november 23 van de 26 lampen. `motion_valid`, `temperature_valid` en `light_level_valid` onwaar zijn ook onbekend. De zijbalk en het blok zeggen dan *onbereikbaar*, niet *verbonden*.
- **Certificaat vastzetten** (`hue.fetch_fingerprint`, `ssl_for`): het certificaat van de bridge is van Signify en door geen publieke autoriteit getekend. Bij het koppelen halen we de SHA-256 van wat de bridge nu toont (zonder iets te verifiëren: dit is het vertrouwensmoment), controleren met `GET /api/config` dat het een bridge is (`bridgeid`, `modelid` `BSB…`) **voor** we een sleutel vragen, en doen daarna elk verzoek met `aiohttp.Fingerprint`. De sleutel gaat dus nooit naar een certificaat dat we niet vastlegden. Een ander certificaat is `HueError("cert")` en de bridge is offline met een melding en een knop *Certificaat opnieuw vertrouwen* (`verify_hue_bridge`: alleen als het `bridgeid` hetzelfde is). `ssl_for` is gecached: een `Fingerprint` is alleen aan zichzelf gelijk, en aiohttp houdt verbindingen open per ssl-object.
- **Koppelen**: `POST /api` met `generateclientkey` na het indrukken van de ronde knop (30 s); fout 101 is *druk op de ronde knop*. Of met een bestaande `key` (gecontroleerd met `GET /clip/v2/resource/bridge`). `own` onthoudt of de add-on de sleutel zelf maakte: alleen dan trekt *Bridge loskoppelen* hem in (v1 `DELETE /api/<sleutel>/config/whitelist/<sleutel>`). Een sleutel die je meegaf (bv. die van een ander programma) blijft.
- **Volgen**: de eventstroom `/eventstream/clip/v2` (`GET`, header `hue-application-key`) geeft bij het verbinden `: hi` en daarna alleen nog gebeurtenissen (`update`, `add`, `delete`). Updates bevatten alleen wat veranderde en worden diep samengevoegd (`hue.merge`); een update van iets onbekends laten we vallen. De hele lijst (`GET /clip/v2/resource`, 108 KB op de echte bridge) halen we op na het verbinden, en daarna elke 10 minuten. Gebeurtenissen die binnenkomen terwijl de lijst wordt opgehaald, gaan in een wachtrij en komen er daarna overheen (`syncing`/`pending`), anders kon een oude lijst een nieuwere gebeurtenis overschrijven.
- **Een stroom die zwijgt**: gemeten 200 s op de echte bridge: geen hartslag, geen gebeurtenis. Een leestime-out zoals bij ESPHome (§35) kan dus niet. Een bridge waarvan de stekker eruit is (zonder dat de TCP-verbinding sluit) zou eindeloos "verbonden" blijven; daarom vraagt de hub elke 30 s `GET /clip/v2/resource/bridge` (`HUE_PING`) en gaat bij een fout offline. Wat de bridge niet meer bereikt maakt alle blokken van die bridge meteen offline met de reden, nooit met de laatste waarden.
- **De hele body lezen** (de fout van 0.30.1): `content.read(n)` geeft alleen wat er al binnen is, en een lijst van 100 KB komt in stukken. `hue.request` leest met `iter_chunked` en een bovengrens. Op de eerste proef tegen de echte bridge liep dit er weer in: een test met een lijst van 300 apparaten (`test_a_long_list_is_read_completely`) bewaakt het nu.
- **Bedienen**: `PUT /clip/v2/resource/light/<id>`, altijd absoluut (`{"on":{"on":true}}`, `{"dimming":{"brightness":55.0}}`), de nieuwe stand komt over de eventstroom (de waarheid is de state). Hoogstens 10 per seconde per bridge (`HUE_GAP`, een lock). Een antwoord met `errors` of een andere status dan 200 is een melding met de tekst van de bridge. Een geweigerde sleutel (401/403) bij een opdracht dwingt een nieuwe lijst af (`need_sync`), die ook weigert en alles offline zet, want de open eventstroom merkt een ingetrokken sleutel zelf niet.
- **Home Assistant: standaard uit** (`dev["ha"]`), zoals bij ESPHome: Hue staat daar al via zijn eigen integratie, en de wens was lampen niet dubbel. Het vinkje in de zijbalk zet ze aan; dan `kk_esp_<id>_<punt>`, fabrikant *Signify*.
- **Het schema**: een apparaatblok is 340 breed (`SC_AC_W`), niet 250. `schema.place_grid` zet een bridge vol in een raster van vier kolommen; de eerste versie rekende met 250 en liet de blokken over elkaar vallen (alleen in een browser te zien).
- **Bij de bouw gevonden**: de keuzelijst van een bool met onbekende waarde (`scEspControl`) toonde **"uit"**: het vraagteken stond erin, maar de optie *uit* kreeg ook `selected` en won. Bij Hue is dat bij de meeste lampen het geval. Nu staat alleen het vraagteken geselecteerd.

### Niet getest / open
- **Niet geprobeerd op echte hardware: elke opdracht.** Geen lamp is geschakeld of gedimd, ook niet door de proeven. Getest zijn alleen de URL en de body tegen een nagebootste bridge, naar de vorm van wat de echte bridge antwoordde. Of `dimming` op een lamp die uit staat hem ook aanzet, is dus niet nagegaan (de ingang `bri` stuurt alleen de helderheid; `on` schakelt).
- **Wel tegen de echte bridge** (BSB002, 28 apparaten): HTTPS met vastgezette vingerafdruk over HTTP/1.1 (de bridge noemt zich `HTTP/2` maar valt terug), de hele lijst, de eventstroom (alleen het verbinden en 200 s stilte), de herkenning (lampen, stopcontacten, melders, 2 overgeslagen schakelaars), de waarden van de melder en de 4 bereikbare lampen, en een foutieve vingerafdruk. Het koppelen met de ronde knop is niet geprobeerd (de bestaande sleutel is gebruikt, alleen lezen).
- **In een echte browser bekeken** (menu, 28 blokken in een raster, de zijbalk van een lamp, onbereikbaar, de keuzelijst met `?`); niet geklikt op *Alles toevoegen*, *Bridge loskoppelen* en *Certificaat opnieuw vertrouwen* (die vragen om een bevestiging en zijn alleen met de API-tests geprobeerd), en niet op een telefoon.
- **Niet in HA zelf gedraaid** (MQTT-discovery met unit-tests), en geen eventstroom met echte gebeurtenissen gezien (er gebeurde niets in die 200 s): de vorm van een `update` komt uit de documentatie van Hue en de nagebootste bridge, niet uit een opname.
- **Kamers en scènes** (0.39): een kamer (`type: room` met een `grouped_light`) is een apparaat van het soort `room`, met de punten `on`, `bri` (0 tot 100, 0 is uit) en `scene` (een keuze, 0 is *geen / uit*). `on` en `bri` gaan naar de `grouped_light` van de kamer (`bri` boven 0 is één verzoek met `on` en `dimming`); een scène wordt geactiveerd met `PUT /resource/scene/<id>` `{"recall":{"action":"active"}}`. Een kamer is bereikbaar als minstens één van haar lampen dat is (`room_reach`); anders alleen `reach`. De stand van de scène is die met `status.active` anders dan `inactive`, en 0 als de kamer uit staat. **Nummers van scènes zijn vast** (`assign_scenes`, `dev["scenes"]` in `espdev.json`, id van de scène -> 1, 2, ...): het eerste keer op naam, daarna nooit hergebruikt, ook niet als de scène weg is (Loxone onthoudt een scène als getal, en een verschoven nummer zou de verkeerde scène zetten). Een hernoemde scène houdt zijn nummer. Dit is wat JavaLox deed (`/hue/device/lights/<kamer>/scene` en `/brightness`), maar zonder de naam of het UUID van de kamer in Loxone. Hoogstens 63 scènes per kamer.
- **Niet ondersteund**: zones,  knoppen en dimmers (gebeurtenissen, geen stand: HUISDEV kent ze nog niet, zie `HUISDEV.md` §14), kleur en kleurtemperatuur, `enabled` en gevoeligheid van een melder, en de ontdekking van een bridge (mDNS): het adres tik je in.
- **Max. 4 bridges, 80 apparaten** per installatie (`MAX_BRIDGES`, `MAX_DEVICES`; eerder 16 voor alle HUISDEV-apparaten).
- Een bridge die zijn adres wijzigt (DHCP) wordt niet teruggevonden: pas het adres aan (`PUT /api/espdev/hue/bridge/<id>` met `host`) of geef hem een vast IP.
- Bestaande falende tests op deze Windows-machine, ook op een schone HEAD: `test_offline_after_three_misses_and_back`, `test_input_waits_while_offline_and_retries_after_a_failure`, `test_a_panel_that_reports_in_gets_its_screensaver_and_a_touch_opens_a_page` en `test_an_unreachable_panel_gives_a_readable_error`.

## 37. Nuki-sloten via de Web API (0.33)

Code: `nuki.py` (puur: de lijst lezen, punten en acties, waarden, opdracht naar verzoek; plus het verzoek), de Nuki-kant van `espdevhub.py` (`add_nuki`, `claim_nuki`, `_run_nuki`, `_nuki_flush`, `_nuki_send`, `nuki_view`), `tests/test_nuki.py` met een nagebootste API (`tests/fakenuki.py`).

- **Waarom de Web API**: die gebruikte je al in JavaLox (token, `GET /smartlock/<id>`), en een Smart Lock 4 Pro (`type` 4, `wifiEnabled`) heeft geen hardware-bridge nodig. Nadeel: de cloud, dus afhankelijk van internet, en de stand zoals Nuki hem kent. Een **lokale** koppeling met de Bridge-API (`lockState`, `lockAction`, een callback) zou dat oplossen; het is niet gebouwd en niet nagegaan of een Pro die kan (de pagina met die API was niet te lezen).
- **Zelfde opzet als Hue (§36)**: een **account** (token) met **één** taak en één verzoek (`GET /smartlock`) voor alle sloten, en een blok per slot (`driver: "nuki"`, `account`, `lid`). Het id is `nuki-<smartlockId>`. De token staat in `espdev.json` onder `nuki` (0600) en gaat nooit naar de pagina; het account-id is een hash van zes tekens (`nuki.account_id`).
- **Pollen, geen push**: Nuki's webhooks vragen een adres dat vanaf het internet bereikbaar is. De limiet is 4 per seconde, 100 per minuut en 1000 per uur per token (docs.nuki.io); 30 s (`NUKI_POLL`) is 120 per uur, en na een opdracht nog drie keer snel (`NUKI_BURST`: 2, 6 en 15 s), want Nuki voert een opdracht **asynchroon** uit (200 of 204 betekent aangenomen). Een 429 wacht 90 s (`NUKI_BACKOFF`).
- **Een slot is een deur**: de punten zijn **alleen lezen** (stand, vergrendeld, deur open, batterij, batterij kritiek). Acties: *vergrendelen* is gewoon; *ontgrendelen*, *unlatch*, *lock 'n' go* en *unlatch met lock 'n' go* zijn `danger` (§34): met de hand uit de zijbalk, met `confirm`, niet als knop in HA, een MQTT-opdracht wordt geweigerd. Drie van die regels zijn in de tests bewust gebroken en teruggezet (`danger` uit voor ontgrendelen, een beweging als "vergrendeld", een punt dat te bedienen is): elke breuk laat tests falen. Ze zijn sinds 0.34 ook via een ingang te starten (hieronder).
- **Vergrendeld is onbekend zolang het slot beweegt** (`nuki.values`): 1 is vergrendeld, 3, 5 en 6 niet; 2, 4, 7, 0, 253, 254 en 255 en alles wat we niet kennen zijn `None`, nooit een gok. De standen komen van docs.nuki.io (de pagina noemt 8 tot 252 niet). Een slot dat de cloud niet meer bereikt maakt zijn blokken offline met de reden.
- **De stand is die van de cloud**: Nuki geeft geen tijdstip van de stand. Een slot dat offline is (batterij leeg, wifi weg) blijft zijn laatste stand melden. Daarom staat in DOCS.md dat de stand niet de enige beveiliging mag zijn. Niet opgelost. **Let op bij een ingang die ontgrendelt:** een bron die op de stand van dit slot leunt (`vergrendeld`) kan achterlopen.
- **Punten die van het slot afhangen**: een deursensor (`doorState` ongelijk aan 0) of een batterijpercentage is alleen een punt als het slot het meldt; komt een deursensor erbij, dan verandert de `rev` en past het blok zich aan (`test_a_door_sensor_that_comes_later_adds_a_point`).
- **HA: standaard uit**, zoals Hue en ESPHome; dan de stand en een knop *Vergrendelen*, fabrikant *Nuki*.

### 0.34: de acties als ingang op het blok
- **Waarom**: de eigenaar probeerde de acties uit en ze werkten; de wens is ze te laten starten door een timer of module (bv. 's avonds vergrendelen, een bel of schakelaar die de deur vrijgeeft). Eerder was dit bewust niet gebouwd (een timer die een deur opent). De aansluiting zelf is nu de bevestiging.
- **Hoe**: elke actie zonder argument is een **rij met een ingang** onder de punten (`schema._espdev_blocks`: poort `do_<actie>`, `action: True`, `danger`), met een naam in het blok (een rij zonder uitgang had anders geen label) en in rood als hij de deur opent. De aansluiting staat in `dev["inputs"]` onder `do_<actie>`, naast de punten (`_wired_action`, alleen bij een Nuki-slot: HUISDEV-acties en ESPHome-knoppen, waaronder een restart, worden hier **niet** aanstuurbaar). Een actie kent alleen een 0/1: een getal als bron wordt geweigerd.
- **Strenger dan de ingang van een punt** (`_tick_action`), want het kan een deur zijn, en elke regel heeft een test die faalt als hij wordt weggehaald:
  1. alleen de overgang **0 naar 1** start; de eerste stand (aansluiten, herstart, na onbekend) en 1 naar 0 doen niets: een bron die al 1 is bij een herstart opent niets;
  2. een actie **wacht nooit**: is het apparaat offline op het moment van de stijging, dan vervalt hij (bij een punt wacht een wijziging wel tot het apparaat terug is);
  3. **nooit opnieuw proberen** na een mislukking (een punt probeert het na 30 s opnieuw);
  4. hoogstens eens per `ACTION_GAP` (5 s) per actie, tegen een bron die flappert;
  5. een spoor: elke gestarte actie staat in het logboek van de add-on (`gestart door <bron>`) en de melding staat bij de ingang in de zijbalk.
- **Wat er niet verandert**: HA krijgt nog steeds alleen *vergrendelen* als knop; een MQTT-opdracht voor een gevaarlijke actie wordt geweigerd. `do()` wordt met `confirm=True` aangeroepen vanuit de ingang, dus dezelfde controles gelden (bereikbaar, actie bekend).
- **Bewust niet gedaan**: een tweede bevestiging bij het aansluiten van een gevaarlijke actie, en een tijdslot ("alleen tussen 8 en 22 u"): wie dat wil sluit er een module `En` met een tijdmodule tussen. Een bron die per ongeluk 0 naar 1 gaat (een verkeerd getekende module) opent de deur: de waarborgen hierboven beperken de schade bij herstarten en flapperen, niet bij een verkeerde logica.

### Niet getest / open
- **Door mij is geen enkele opdracht naar het echte slot gestuurd** (alleen tegen een nagebootste API). De eigenaar meldt dat de acties met de hand in de zijbalk werken (0.33); dat is niet door mij gezien. De aansluiting als **ingang** (0.34) is niet op het echte slot geprobeerd, alleen het blok en de zijbalk zijn in een browser bekeken. Het pad (`POST /smartlock/<id>/action` met `{"action": 1 tot 5}`) en de codes komen uit docs.nuki.io (de concepts-pagina noemt het veld `action` en de codes); de Swagger-specificatie was niet bereikbaar, dus de exacte vorm van het opdrachtpad is niet bevestigd. Een verkeerd pad geeft een melding (400 of 404). De **actiecodes** (1 ontgrendelen, 2 vergrendelen, 3 unlatch, 4 lock 'n' go, 5 unlatch met lock 'n' go) staan op de concepts-pagina en kloppen met wat ik zelf van de API wist, maar zijn door mij niet op het slot gezien.
- **Wel tegen je echte account, alleen lezen**: `GET /smartlock` geeft 1 slot (`type` 4, stand 3 ontgrendeld, batterij 14 % en kritiek, geen deursensor); een foute token geeft 401 met de juiste melding. In een browser bekeken: het blok met de echte stand, de zijbalk met *Vergrendelen* en de drie rode acties, het menu. Niet geklikt op een actie, op *Account koppelen* (prompt) en *Account loskoppelen*.
- **Niet gezien**: wat Nuki antwoordt op een opdracht aan een offline slot, en de foutteksten van Nuki voor `unlatch` in de VS (alleen uit de documentatie).
- **Niet gebouwd**: een lokale Bridge-koppeling, de gebruikers en logboeken van het slot (`/smartlock/<id>/log`), `trigger` en `lastAction` als punt (wie of wat het slot bediende), en de instellingen van het slot (automatisch vergrendelen en dergelijke).
- Bestaande falende tests op deze Windows-machine, ook op een schone HEAD: `test_offline_after_three_misses_and_back`, `test_input_waits_while_offline_and_retries_after_a_failure`, `test_a_panel_that_reports_in_gets_its_screensaver_and_a_touch_opens_a_page` en `test_an_unreachable_panel_gives_a_readable_error`.

## 38. Brink-ventilatie via de cloud van Brink Home (0.35)

Code: `brink.py` (puur: lijst en beschrijving lezen, punten, waarden, opdracht naar verzoek; plus het inloggen en het verzoek), de Brink-kant van `espdevhub.py` (`add_brink`, `claim_brink`, `update_brink`, `_run_brink`, `_brink_call`, `_brink_cycle`, `_brink_flush`, `_brink_send`, `brink_view`), `tests/test_brink.py` met een nagebootst Brink Home (`tests/fakebrink.py`).

- **Waarom de cloud, en waarom nu**: de eigenaar heeft een Flair 400 met een interne wifi-module en wil voorlopig geen extra hardware. De oude API (`UserLogon`, `GetAppGuiDescriptionForGateway`) die JavaLox gebruikte bestaat niet meer (404): Brink Home is verhuisd naar een nieuwe webapp op `/app/`. Een **lokale** weg bestaat wel (Modbus over RS485 op de UWA-2E, zie hieronder), maar vraagt hardware.
- **Hoe dit is uitgezocht**: uit de openbare JavaScript van de nieuwe webapp (`main-*.js`, 4,7 MB, plus `assets/appsettings.json` en `sts.json`), de openbare inlogpagina, het openbare OpenID-document, en **een ingelogde sessie van de eigenaar** (die zelf inlogde in een tabblad; de antwoorden zijn met een hook op `XMLHttpRequest` vastgelegd, zonder tokens). Alles wat hier staat is dus afgeleid van wat de webapp zelf doet, niet uit documentatie van Brink.
- **De API** (`/portal/api/v1.1/`): `GET systems?pageSize=..` (`systemShareId` is het id in alle paden; `gatewayState`: 0 vergrendeld, 1 offline, 2 online, uit de enum van de app), `GET systems/<id>/uidescription` (een boom van componenten, met per parameter `valueId`, `name`, `value` als tekst, `valueState`, `readWrite`, `controlType`, `listItems`), `PUT systems/<id>/parameter-values` met `{"writeValues": [{"valueId", "value" (tekst), "state"}]}` (zoals de app het doet), en een SignalR-hub (`client-app`) voor pushes die we niet gebruiken. `valueState`: 0 goed, 1 wordt gewijzigd, 4 fout, 5 buiten dienst, 6 geen geldige data: alleen 0 en 1 is een waarde, de rest is onbekend.
- **Koppelen op de naam, niet op het id**: `valueId` verschilt per installatie, maar de `name` van een parameter (een Duitse sleutel: `Lüftungsstufe`, `Betriebsart`, `Frischlufttemperatur`, ...) niet. `brink.POINTS` zet die namen om naar punten. Een parameter die de unit niet heeft geeft geen punt. De `Lüftungsstufe` (768902 bij de eigenaar) is hetzelfde `ventilatiemodeid` als in zijn JavaLox-config.
- **Inloggen** (`brink.login`): IdentityServer op `/idsrv`, een openbare client `spa`, code-flow met **PKCE** (S256), scope `openid api role locale`. Geen wachtwoord-grant en geen `offline_access`, dus geen refresh-token: de add-on logt zelf in met e-mail en wachtwoord. De stappen: `GET authorize` -> doorverwijzing naar het formulier -> `POST` van `Input.Username`, `Input.Password` en het anti-forgery token (met zijn cookie) -> doorverwijzing naar `https://www.brink-home.com/app/?code=..` (die volgen we niet maar lezen) -> `POST connect/token`. Het formulier wordt herkend aan `Input.Username` en `Input.Password` (de pagina heeft ook andere formulieren), en een pagina zonder herkenbaar formulier stuurt **geen wachtwoord**. Een captcha of tweede factor is er niet (nagekeken in de openbare pagina).
- **Een onjuist wachtwoord wordt niet herhaald**: dat levert een 200 met de inlogpagina op, geen doorverwijzing (`BrinkError("auth")`). De add-on probeert dat precies een keer, wacht dan `BRINK_AUTH_WAIT` (15 min) en toont het bij het blok; een account kan op herhaalde fouten worden geblokkeerd. *Wachtwoord wijzigen* (`update_brink`) probeert meteen opnieuw. Een verlopen of ingetrokken token (401) is wel een keer opnieuw inloggen (`_brink_call`).
- **Een module die offline is, geeft geen waarden**: Brink toont van een offline module zijn laatste stand. `gatewayState` ongelijk aan 2 maakt de blokken van dat systeem offline met de reden en we vragen de beschrijving dan niet op. Een systeem dat niet meer in het account staat, ook.
- **Schrijven**: `level`, `mode` en `bypass_mode` zijn punten die je instelt (en dus ook ingangen): de waarde als tekst, met de `valueState` van de parameter, alleen als `readWrite` het toelaat en alleen een keuze die in de lijst staat. De opdracht is asynchroon (de stand komt bij de volgende poll; na een opdracht nog drie keer snel: 2, 6 en 15 s). Er zijn **geen acties** en dus niets gevaarlijks: een ventilatiestand is niet iets waarvan je schrik hoeft te hebben.
- **HA: standaard uit**, zoals Hue, Nuki en ESPHome; fabrikant *Brink*.

### Niet getest / open
- **Wel tegen de echte Brink Home, alleen lezen** (de eigenaar koppelde zelf zijn account in de lokale add-on, zodat zijn wachtwoord niet langs mij kwam): de hele login (PKCE, formulier, cookie, token), de lijst, de beschrijving, en alle 13 punten met de juiste waarden (stand 1, handbediening, filter vuil, 181 dagen, software S2.01.21), die ook **vers** zijn: tussen twee uitlezingen veranderden het debiet (100 naar 101) en de buitentemperatuur (8,8 naar 8,3).
- **Niet getest: een opdracht.** Er is geen enkele schrijfopdracht naar de echte Brink Home gestuurd. De vorm (`PUT systems/<id>/parameter-values`, `writeValues` met `valueId`, `value` als tekst en `state`) komt uit de code van de webapp. Wat Brink met de `state` doet en of een opdracht in *Automatisch* wordt teruggedraaid door het klokprogramma van de unit, is niet nagegaan.
- **Niet gezien**: wat Brink antwoordt op een opdracht aan een module die net offline is, en de foutteksten van Brink.
- **Fragiel**: de API is niet gedocumenteerd en is al eens veranderd. Een wijziging in het inlogformulier, de scope of de paden laat de koppeling stoppen (de melding noemt dan "een inlogpagina die ik niet herken" of het HTTP-nummer). De add-on logt niet in zonder het formulier te herkennen.
- **Niet gebouwd**: de vochtigheid en de vier CO₂-sensoren (de unit meldt 0: niet aangesloten), de extra instellingen van de unit, een lokale koppeling, meerdere accounts voor een systeem, en de SignalR-pushes (we pollen).
- **De lokale weg** (niet gebouwd): de Ubiflux Vigor Plus heeft een UWA-2E-regelaar met een Modbus RS485-poort (X06, rood, 3 polen: aarde, A, B) en netwerkpoort X17 voor Brink Home. De registers zijn bekend uit `pyubbink` en de Flair-integratie `ha-brink-modbus` (8000 besturingsmodus, 8001 stand 0 tot 3, 8002 debiet 0 of 50 tot 400, 6100 bypass, 4036/4046 temperaturen, 4031/4032/4041/4042 debieten, 4050 bypassstatus, 4100 filter; slave 20, 19200 baud, parity nog te controleren: N of E). Het kan met een ESP32 met RS485-adapter en ESPHome, dat via de bestaande ESPHome-koppeling (§35) een blok wordt. De Modbus-besturing sluit de wandschakelaar uit.
- **Wat de sniffer op de router liet zien**: de wifi-module (`192.168.1.42`, bevestigd door de module zelf in `uidescription`) praat via een langlevende TCP-verbinding met `brink-home.com` op poort 56155; de server stuurt er ongeveer elke 8 seconden iets, de module antwoordt met berichten van 240 tot 323 bytes. Of dat versleuteld is, is niet vastgesteld (WebFig toont geen frame). Een man-in-the-middle is daarom niet uitgewerkt.
- Bestaande falende tests op deze Windows-machine, ook op een schone HEAD: `test_offline_after_three_misses_and_back`, `test_input_waits_while_offline_and_retries_after_a_failure`, `test_a_panel_that_reports_in_gets_its_screensaver_and_a_touch_opens_a_page` en `test_an_unreachable_panel_gives_a_readable_error`.

## 39. Smappee-laadpaal (EV Wall) via de cloud van Smappee (0.36)

Code: `smappee.py` (puur: lijsten lezen, punten, waarden, opdracht naar verzoek, sessies; plus het inloggen en het verzoek), de Smappee-kant van `espdevhub.py` (`add_smappee`, `claim_smappee`, `update_smappee`, `_run_smappee`, `_smappee_cycle`, `_smappee_send`, `smappee_sessions`, `smappee_view`), de Smappee-kant van `chargehub.py` (hieronder), `tests/test_smappee.py` en `tests/test_smappee_charge.py` met een nagebootste cloud (`tests/fakesmappee.py`).

- **Waarom de cloud**: de EV Wall met een Connect 5130 heeft geen officiële API en volgens meldingen van anderen geen MQTT zonder Genius. Een lokale weg (Modbus TCP op de Connect) is niet nagegaan: geen registerlijst gevonden, en de status zou er niet in zitten. OCPP is niet gebruikt: Smappee moet het adres goedkeuren en een stroom instellen werkte volgens anderen niet.
- **Hoe dit is uitgezocht**: uit de **broncode van de Home Assistant-integratie `myny-git/smappee_ev`** (MIT), die de calls van het dashboard van Smappee volgt. **Niets hiervan is door ons tegen een echte Smappee gezien**: de vorm van de lijsten (`properties`, `configurationProperties`, `carCharger`, `modules`, de sessies met `from`, `to`, `energy`, `rfidToken`) en van de acties is overgenomen van die code. Een echt antwoord dat anders is, geeft geen punt of een onbekende waarde, geen gok.
- **De API**: inloggen `POST /dashapi/login` (`userName`, `password`) met de token in de header `token`; `GET /api/v11/user/servicelocations?fullDetails=true` (de locatie met `chargingStation.serialNumber` is de "control"-locatie), `GET /api/v10/chargingstations/<serial>?includeDetails=true` (de `modules` met `smartDevice` van categorie `CARCHARGER` zijn de laadpunten), `GET /api/v10/servicelocation/<loc>/homecontrol/smart/devices` (de stand), `GET /api/v10/chargingstations/<serial>/sessions` (sessies, 7 dagen); opdrachten `POST .../smart/devices/<id>/actions/<naam>`: `pauseCharging` (`[]`), `setChargingMode` (`STANDARD`, wat de app bij hervatten doet) en `setPercentageLimit`.
- **Een blok per laadpunt** (`driver: "smappee"`, `sid` het serienummer van het station, `cid` het smart device); het account is een hash (`smappee.account_id`), het wachtwoord staat in `espdev.json` (0600). Punten: `state` (vrij, aangesloten, laadt, gepauzeerd), `charging`, `paused` en `current` (instelbaar), `session_kwh`, `available` (diagnose). **Geen acties, geen start en stop**: met een badge start de eigenaar van de tag aan de laadpaal.
- **De stand is onbekend, nooit een gok**: een `chargingState` die we niet kennen (en geen `connectionStatus` of IEC-stand) geeft `None`. *Hervatten* is `setChargingMode STANDARD` en zet dus Slim of Zon op Standaard: daarom wordt er niets gestuurd als de lader niet gepauzeerd is, en hervatten bij een onbekende status geweigerd. Pauzeren mag altijd.
- **De stroom is een percentage van het bereik min..max** van het laadpunt (uit `configurationProperties`, anders 6 tot 32). Een heel getal ampère wordt het dichtstbijzijnde percentage en wordt weer als hetzelfde getal gelezen (getest voor elk getal in vier bereiken). De limiet geldt alleen zonder optimalisatie (`optimizationStrategy` leeg of `NONE`); bij Slim of Zon is de stroom onbekend en schrijven wordt geweigerd, zoals de HA-integratie dat ook behandelt.
- **Pollen**: 60 s voor de stand, sessies elke 60 s tijdens laden of pauze en anders elke 600 s, de lijst van stations elke 30 minuten. Smappee publiceert geen limiet; de HA-integratie vraagt veel zuiniger. Een 429 volgt de `Retry-After`; onderhoud ("under maintenance") is een eigen melding; een onjuist wachtwoord wordt één keer geprobeerd en dan 15 minuten met rust gelaten (zoals Brink). Dezelfde opdracht voor hetzelfde punt hoogstens eens per 5 s.

### Het laadblok (`chargehub.py`) met een Smappee-laadpaal
- **`espdev` in plaats van `device`**: een laadpaal die naar een Smappee-blok wijst heeft geen Tuya-toestel, datapunten of start- en stopregels (`_check`). Het Tuya-pad is niet veranderd (de bestaande tests zijn ongewijzigd groen).
- **Sessies uit de cloud met de badge**: de gesplitste facturatie komt van de **tag**, niet van de add-on, want met een badge begint elke sessie aan de laadpaal. `tags` is `badge -> {by, name}` (`by` leeg is dit huis, het id van een gekoppelde installatie, of een eigen id uit de naam). Afgeronde sessies worden eenmaal geboekt (`imported`, gehouden over een herstart), een lopende sessie is de actieve sessie (`cloud`). Wat Smappee over de tijd voor `since` (standaard het moment van koppelen) meldt, boekt niet vanzelf.
- **Onbekende badge**: een eigen groep (`?<badge>`) met de naam *Onbekende badge …1234*, zodat de factuur ze niet samenvoegt. Krijgt de badge later een huis, dan worden de eerdere sessies van die badge **opnieuw toegewezen** (`_reattribute`). Dat verandert een log van voor een eventuele factuur: bewust, de log is de bron. Een sessie zonder badge is *Zonder badge*.
- **Bedienen**: `view` geeft `act` = `pause` of `resume` (nooit start of stop), `amps`, `amp_min` en `amp_max`; `start` en `stop` weigeren met de reden. De eigenaar mag altijd, een ander huis alleen tijdens zijn eigen sessie (`_sm_check`); tijdens de sessie van een ander ziet het niets dan *bezet*. `control` wacht op Smappee (de pagina), `control_nowait` controleert meteen en verstuurt daarna (paneel en andere huizen): een weigering door Smappee komt dan in het log, niet bij de aanroeper.
- **Kaart**: een charger-kaart heeft `amps` (alleen als het aan staat; zo blijft een bestaande kaart gelijk) en dan een regelaar `kk.<kaart>__amps` (kind `number`, `waarde|min|max`; op een scherm met openHASP alleen de tekst). De knop pauzeert of hervat. `paused` is toegevoegd aan de statuswoorden.
- **Link met andere huizen**: de opdrachten `pause`, `resume` en `current` (met `value`) komen aan bij `ChargeHub.control_nowait` met de regels van de eigenaar.

### 0.36.1: "geen laadpunten" bij het koppelen
- **Wat er gebeurde**: het koppelen met een echt account gaf "dit account heeft geen laadpunten bij Smappee". Eén oorzaak in mijn code was een aanname: `device_map` eiste `type.category == CARCHARGER` van elk apparaat in de lijst met smart devices, terwijl de HA-integratie dat alleen bij de modules van het laadstation controleert. Een lijst zonder die categorie gaf nul laadpunten. Nu staat elk apparaat onder zijn `id`, `uuid`, `smartDeviceId` en `smartDeviceUuid`, zonder filter op categorie; het laadstation bepaalt welke apparaten laadpunten zijn.
- **Of dat de oorzaak was, is niet gezien.** De melding noemt nu waar het zoeken strandde (aantal locaties en hun velden, hoeveel met een laadstation, modules en laadpunten per station, en de categorieën in de lijst met smart devices), zonder namen, ids of tokens. Zo is een volgende afwijking in één melding te lezen.

### 0.36.2: de modules van het laadstation wijzen geen laadpunt aan
- **Wat de melding van 0.36.1 liet zien** (echt account, EV Wall Home): 1 locatie met een laadstation, `modules: 6` maar **0 laadpunten**, en in de lijst met smart devices de categorieën `CARCHARGER`, `CHARGINGSTATION` en `LED`. De aanname uit de HA-integratie (een laadpunt is een module met een `smartDevice` van categorie `CARCHARGER`) klopt dus niet voor dit account: de modules wijzen er geen aan zoals ik ze las.
- **Nu**: wijzen de modules er geen aan, dan zijn de laadpunten de apparaten in de lijst met smart devices met categorie `CARCHARGER` (`connectors_from_devices`; `pos` is de volgorde in de lijst). `CHARGINGSTATION` en `LED` zijn geen laadpunt. De melding beschrijft nu ook de modules (veldnamen en categorieën, geen waarden).
- **Niet gezien**: hoe de modules er wel uitzien (dat zegt de melding als het opnieuw misgaat), en of de sessies, de stand en de opdrachten bij dit account zo werken als aangenomen. Bij een station met twee laadpunten volgt `pos` de volgorde van de lijst, niet de volgorde op de laadpaal.

### 0.36.3: het id van een smart device is geen getal
- **Wat de melding van 0.36.2 liet zien**: de modules hadden velden `active, childModules, firmwareInstallationDate, firmwareVersion, id, label, position, positionLabel, serialNumber, smartDevice, supportsDynamicInstallation, type`, 2 van de 6 met een `smartDevice` (categorieën `CARCHARGER` en `LED`), en toch **0 laadpunten**. De enige drempel die dan overbleef was het id: `smappee.py` nam alleen een positief geheel getal aan. Het id van een smart device is bij dit account dus waarschijnlijk een tekst (bv. een uuid); de HA-integratie zet het gewoon als tekst in de URL. **Dat is een gevolgtrekking, niet gezien**: de melding noemt nu het type van het id van een laadpunt (zonder het id zelf), zodat het bij een volgende fout te lezen is.
- **Nu**: `smappee.ident` laat een getal toe of een tekst met alleen letters, cijfers, `-` en `_` (tot 64 tekens; het gaat in de URL). `sid` en `cid` van een blok volgen dat; `hid_of` maakt van een lang id een hash. Een getest pad van begin tot eind met tekst-ids (koppelen, stand, pauzeren, herstart).

### 0.37.0: verbruik en teller (de officiële API, met een budget) en een schatting uit de sessie
- **Waarom**: de eigenaar wil `getCurrentMeter` en `getCurrentRate` uit JavaLox in het blok. JavaLox gebruikt de **officiële API** (`app1pub.smappee.net/dev/v3`, OAuth met client-id en secret, `grant_type=password`) voor servicelocatie 211532, dezelfde locatie als het laadstation hier (dashboard-locatie …1532). Teller = de som van `active` (drie fasen) over `aggregation=5` vanaf 1 jan 2024 (`1704063600000`), Wh gedeeld door 1000. Verbruik = het blok van `aggregation=1` (5 minuten), de som van de drie fasen maal 12 (Wh per 5 minuten naar W), onder 0,3 Wh is het slaapverbruik (0). Dat de eenheid Wh is, volgt uit die `* 12` en de drempel van JavaLox; niet gezien.
- **Budget: hoogstens 20 verzoeken per uur** (opgave van de eigenaar). `smappee.Budget` telt **elk verzoek aan de officiële API** (ook het token) over het afgelopen uur, bewaart de tijdstippen in `smappee_budget.json` (per client-id, als hash; zo vergeet een herstart ze niet) en laat nooit meer toe dan `limit` (standaard 14, hoogstens 20, per account in te stellen). Een verzoek zonder budget gaat niet weg. Het schema: verbruik elke 360 s tijdens laden en elke 900 s daarbuiten, de teller elke 1800 s en eenmaal na het einde van een sessie (hoogstens eens per 300 s): in het slechtste uur 10 + 2 + token(s) = ongeveer 13. Een test bewaakt dat dit onder het budget blijft. 429 volgt `Retry-After` (minstens 30 min), onderhoud wacht 15 min, onjuiste sleutels (400 of 401 bij het token) een uur, een andere fout probeert na 10 min. Een fout stopt de ronde van het dashboard niet.
- **JavaLox deelt misschien dezelfde client** en dus dezelfde 20 per uur; dat kan de add-on niet zien. Daarom is het budget instelbaar en staat het in DOCS.md.
- **Schatting zonder verzoeken** (`note_energy`, `virtual_power`, `virtual_meter`): terwijl een laadpunt laadt groeit de `energy` van de lopende sessie (elke 60 s gelezen). **Verbruik**: niet aan het laden (status bekend) is 0; aan het laden de groei tussen de eerste en de laatste *verandering* in de laatste 15 minuten, gemeten op de tijdstippen waarop een waarde voor het eerst gezien werd (zo valt de vertraging van het pollen weg, ook als Smappee `energy` maar eens per paar minuten bijwerkt; getest met 5-minutenstappen), mits er minstens 120 s tussen zit en de laatste verandering niet ouder is dan 240 s; anders het blok van de API, anders onbekend. Een energie die niet groeit terwijl de status *Laadt* zegt is onbekend, niet 0. **Teller**: de laatste API-waarde plus wat de sessies sindsdien erbij kregen (de sessie die liep bij die lezing: alleen de groei; een sessie die erna begon: helemaal). Het punt *Verbruik* is er daarom ook zonder sleutels; *Teller* alleen met sleutels.
- **Een station met twee laadpunten**: het verbruik van de API geldt voor de hele servicelocatie en is niet toe te wijzen; dan blijven de schatting uit de sessie (die per laadpunt is, maar alleen bij één laadpunt wordt gebruikt omdat de sessies anders niet zeker te plaatsen zijn), het verbruik en de teller onbekend; niet aan het laden blijft 0.

### 0.37.1: de teller klopte niet met JavaLox, en een diagnose
- **Wat de eigenaar zag**: *Teller* 22293 terwijl JavaLox 4972 geeft, en *Verbruik* dat snel op onbekend ging terwijl hij laadde.
- **Eén fout van mij**: `meter_kwh` telde **alle** waarden van `active`, JavaLox alleen de eerste drie (`active.get(0..2)`); `rate_w` deed het al goed. Is er meer in de lijst (bij een servicelocatie met meer kanalen dan de drie fasen), dan krijg je te veel. Nu de eerste drie. **Of dat de hele verklaring is, is niet gezien**: de verhouding 22293 / 4972 is geen mooi geheel getal, en een tweede mogelijke oorzaak is de teller van nu (de waarde van de API plus wat de sessies sindsdien erbij kregen), als `energy` van een sessie niet in kWh staat (de HA-integratie neemt kWh aan; niet gezien).
- **Waarom het verbruik op onbekend ging** (vermoeden, niet gezien): terwijl hij laadt is er geen schatting uit de sessie (de lopende sessie staat niet in de lijst, of haar `energy` groeit niet) en geen blok van de API (de data loopt achter, of het antwoord is leeg: `rate_w` geeft dan None, JavaLox geeft 0). Een leeg antwoord blijft hier onbekend, niet 0: een laadpaal die laadt gebruikt iets.
- **Diagnose in de zijbalk** van het Smappee-blok (*Diagnose: wat Smappee zegt*): de ruwe statuswoorden (`chargingState`, verbinding, IEC, modus, strategie, limiet), de laatste drie sessies (hoe lang geleden, of ze lopen, kWh, of er een badge was), de veranderingen van de sessie-energie, en van de API het aantal blokken, het aantal waarden per blok, de leeftijd van het nieuwste blok, de som van de eerste drie tegenover alle waarden, en de teller van de API tegenover wat getoond wordt. Zonder id's, namen of badges. Zo is in één blik te zien welke van de vermoedens klopt.

### Niet getest / open
- **Verbruik en teller zijn niet tegen de echte API geprobeerd**: geen token, geen `consumption`. De vorm van het antwoord (`consumptions[].active`, `timestamp`) is van JavaLox en de officiële documentatie zoals ik die ken: JavaLox leest alleen `active`. Of `timestamp` er is en in milliseconden staat, is niet gezien (zonder tijdstippen neemt `rate_w` het eerste blok, zoals JavaLox). Ook niet gezien: hoeveel de API achterloopt en of de eenheid Wh is.
- **De schatting is niet naast de echte meter gelegd**: hoe vaak Smappee `energy` van een lopende sessie bijwerkt, en in welke eenheid, is niet gezien. Een afwijking hier geeft een verkeerd verbruik. Vergelijk de eerste keer *Verbruik* met het blok van de API (of met de app) voor je erop vertrouwt.
- **Niets is tegen een echte Smappee geprobeerd**: geen login, geen lijst, geen opdracht, geen sessie. Alles is tegen een nagebootste cloud getest die de aanname van `smappee.py` herhaalt (`tests/fakesmappee.py`). Wat een echte EV Wall met een Connect 5130 meldt, kan afwijken: de eerste keer *alleen lezen* proberen (koppelen, het blok bekijken) voor je iets instelt, zoals bij Brink.
- **Niet nagegaan**: of pauzeren en de stroom werken met *alleen badge* aan (de aanname is ja: het zijn instellingen op het laadpunt, geen opdracht op de sessie); wat Smappee antwoordt op een opdracht aan een lader die offline is; of `rfidToken` een gewone tekst is (anders heeft een sessie geen badge); of een lopende sessie in de lijst staat zonder `to`; en de eenheid van `energy` (kWh is overgenomen van de HA-integratie).
- **De live stand**: de stand is die van de cloud, op de minuut. De HA-integratie krijgt de live waarden via `mqtt.smappee.net`; dat is niet gebouwd. Een pauze is dus tot een minuut later zichtbaar (na een opdracht binnen enkele seconden).
- **Een laadstation met twee laadpunten**: een sessie wordt aan een laadpunt gekoppeld op id, uuid of nummer; wat niet te plaatsen is, blijft weg. Alleen met een laadpunt is dat altijd goed.
- **Niet in een browser bekeken**: het menu *+ Module → Smappee*, *Account koppelen* (prompt) en *Wachtwoord wijzigen*. Wel gezien tegen de nagebootste cloud: het blok, het laadpaal-paneel met badges, pauzeren en een badge een naam geven. De stroom instellen via het veld werd daarbij door de wachttijd van 10 s geweigerd; die is daarna per punt en 5 s gemaakt, en daarna niet opnieuw in de browser gezien.
- **Geen start of stop**, ook niet via een ingang of een kaart: bewust. Wie dat later wil moet eerst nagaan of dat met badge-modus aan door Smappee wordt toegelaten, en wat het met de facturatie doet.
- Bestaande falende tests op deze Windows-machine, ook op een schone HEAD: `test_offline_after_three_misses_and_back`, `test_input_waits_while_offline_and_retries_after_a_failure`, `test_a_panel_that_reports_in_gets_its_screensaver_and_a_touch_opens_a_page` en `test_an_unreachable_panel_gives_a_readable_error`.

## 40. Loxone Miniserver in plaats van JavaLox (0.38)

Code: `loxone.py` (puur: het bericht en de twee importbestanden voor Loxone Config), `loxonehub.py` (signalen, versturen, ontvangen, opslag), `static/loxone.js` (Instellingen → Loxone), de routes `/api/loxone/*` in `web.py`, de optie `loxone_port`, `tests/test_loxone.py` met de echte exports van de gebruiker in `tests/data/loxone/`.

- **Waarom**: JavaLox (een Spring Boot-app naast Loxone) deed vier dingen voor Loxone: Hue-beweging naar een virtuele ingang (`GET /dev/sps/io/<naam>/on|off`), en een aantal endpoints die Loxone zelf aanroept (Smappee-verbruik, Mikrotik, Nuki, Brink, Hue-scenes en helderheid). De add-on heeft de integraties al als punten (§36 tot §39); er ontbrak alleen de brug naar Loxone.
- **Het idee van de gebruiker, en wat ervan klopt**: weinig virtuele ingangen en uitgangen in Loxone Config, de signalen onderscheiden in de body. Dat is gebouwd, met één kanttekening uit de discussie (drie agents: Loxone-kenner, add-on-architect, pragmaticus): **een commando in Config blijft per signaal een eigen object.** Het aantal ingangen daalt (één virtuele UDP-ingang en één virtuele uitgang), het aantal commando's niet. Wat je wint is dat je ze niet met de hand maakt: de add-on schrijft het sjabloon. Een echte "één ingang voor alles"-oplossing vraagt een Pico-C-parser in de Miniserver; die is bewust niet gekozen (code in de Miniserver, moeilijk te onderhouden).
- **Het bericht**: één signaal per bericht, `kk.<naam>=<getal>` (`loxone.parse`, `loxone.message`). Alleen getallen: Loxone leest geen tekst of JSON uit een UDP-bericht. Aan/uit is 1 of 0; een keuze (Hue-scene, ventilatiestand) is het nummer van de keuze, en de tekst staat in het label van het signaal. De prefix `kk.` en het `=` direct achter de naam zorgen dat de herkenning `kk.tuin=\v` nooit `kk.tuinhuis=3` of `kk.tgr_tuin=3` herkent.
- **Een signaal** (`loxone.json`, 0600): `dir` `out` (naar Loxone) of `in` (van Loxone), een bron (`ref`), en wat Config nodig heeft (soort, decimalen, grenzen), `deadband`, `heartbeat`, `skip_first`, `enabled`. De bron van een `out` is elke uitgang die de engine kent (`e:<apparaat>:<punt>`, `t:` timers, `m:` modules) of `online:<apparaat>` (1 of 0). Van Loxone kan alleen een punt van een HUISDEV-apparaat worden gezet.
- **Een waarde van Loxone als bron in de huisconfig** (0.49, `x:<signaal>`): een ingaand signaal zonder apparaat. `ref` is altijd `x:<id van het signaal>`, `kind` is `bool` (een digitale variabele, 0/1), `int`/`float` (een getal, met `min`/`max`) of `time` (een tekst met een tijd, de wekker). De waarde wordt alleen onthouden (`vals`, `loxone_values.json`, overleeft een herstart), niets wordt bediend, en ze is een bron zoals `t:` of `e:`: `model.SOURCE_RE` kent `x:`, `model._source_type` vraagt het type via `model.lox_type` (BOOL, NUMBER of TIME; een signaal dat weg is geeft `ANY`, dus onbekend en nooit een fout, zoals een HUISDEV-punt), `engine.ref_value` en `logic.evaluate` lezen via `engine.lox_value`. Op het schema is het één blok **Loxone** (`kind: loxone`, `key: x:loxone`, alleen uitgangen, een rij per waarde; er is geen blok zolang er geen waarde is), getekend zoals het blok van een omvormer; de bronkiezers van modules en de regels van een ledstrip (`scAcOuts`) vinden de uitgangen daardoor vanzelf, ook met type `time`. Maken: Instellingen, Loxone, *Een waarde van Loxone gebruiken*.
  - **Het bericht mag nu tekst zijn** (`loxone.parse_values`): `kk.wekker=Morgen, 6:45`. Een getal blijft een getal (`parse` is ongewijzigd); wat geen getal is, komt als tekst (tot 60 tekens, zonder besturingstekens; UTF-8, anders latin-1) en de hub beslist per signaal wat het mag zijn: een tekst voor een getal of een punt van een apparaat wordt geweigerd (*verwacht een getal*), een getal voor een tijd ook (*verwacht een tekst met een tijd*). Geen tijd uit een getal als *seconden sinds middernacht* (zo geeft Loxone zelf tijden door): niet geprobeerd, dus niet aangenomen.
  - **De wekkertekst** (`loxone.parse_alarm`, puur): een tijd (`6:45`, `06:45`, `6.45`, `6u45`, `6:45 uur`, `6:45 AM/PM`) met erbij een dag of niets. Dag: `Vandaag`, `Morgen`, `Overmorgen` (ook Engels en Duits), een weekdag (`Za`, `Zaterdag`, ...: de eerstvolgende op of na vandaag), of een datum (`27-11`, `27/11/2026`, `2026-11-27`; een datum zonder jaar is de eerstvolgende). Zonder dag geldt de tijd elke dag. Geen tijd in de tekst (`Geen wekker`, `Uit`, `-`, `Morgen` alleen) is **onbekend**, geen fout.
  - **De dag telt mee**: de bron geeft de tijd (minuten sinds middernacht, TIME) alleen op de dag waarvoor de wekker staat. `Morgen, 6:45` is vandaag onbekend en morgen 06:45; de hub bewaart de **datum** (niet het woord) en rekent zelf door middernacht, ook als Loxone na middernacht niets meer stuurt. Reden: een regel *wekker* van een ledstrip (`scenerules`) rekent met de tijd van vandaag; zonder de dag zou een wekker van overmorgen vandaag al de zonsopgang starten. Gevolg: een wekker kort na middernacht met `before` minuten ervoor start niet vóór middernacht, want de bron is dan nog onbekend.
  - **De tijd van vandaag blijft na het afgaan** (0.49.2): zodra de wekker afgaat meldt Loxone waarschijnlijk meteen de volgende (`Zaterdag, 07:00`, `Geen wekker`). Zonder tegenmaatregel verdween dan de tijd van vandaag en stopte een regel *wekker* van een ledstrip (`minuten erna`) midden in zijn venster. `loxonehub._kept`: komt er een tekst voor een andere dag (of zonder tijd) terwijl de tijd van vandaag al is aangebroken (`min <= nu + 1 min`, een minuut speling voor het klokverschil met de Miniserver), dan bewaart de hub die als `keep` bij de waarde; `value_of` geeft die tot middernacht. Een tekst die zelf voor vandaag is (ook een snooze, `Vandaag, 6:54`) vervangt hem. Is de tijd van vandaag nog **niet** aangebroken, dan heeft de gebruiker de wekker gewijzigd of uitgezet: niets vasthouden. `keep` wordt bewaard in `loxone_values.json`. In de UI staat de volgende wekker met de echte dag (`za 21-11 07:00`) en de melding *voor de logica vandaag nog 06:45*; staat er geen vasthouden, dan *pas bekend op die dag*. Aanname: Loxone meldt de volgende wekker pas **bij of na** het afgaan; meldt het die eerder, dan is de tijd van vandaag niet aangebroken en wordt niets vastgehouden.
  - **Geen wekker vandaag is zeker, niet onbekend** (0.49.3): een regel *wekker* in een blok Scènes met een wekker die pas morgen is (`Morgen, 06:45`) of ontbreekt (`Geen wekker`) gaf *onbekend*, en onbekend maakt het blok **onzeker**: `tick_rules` vergeet dan zijn vorige keuze (`rmem`), zodat de volgende keuze weer een "eerste stand" is die alleen wordt onthouden. Gevolg (gemeld door de gebruiker): `kinderen_slapen` 0 en dan weer 1 startte de scène Nacht niet. Nu geeft `scenehub._read` voor een `x:`-tijd die bekend niet nu geldt (`LoxoneHub.absent`: er kwam een tekst, maar zonder tijd of voor een andere dag, en er is niets vastgehouden) de waarde `scenerules.ABSENT`: *wekker*, *na*, *voor* en *tussen* zijn dan onwaar. Een tijd die nog niets ontving blijft onbekend (en dus onzeker). Modules en andere gebruikers van `ref_value` merken hier niets van: die krijgen nog steeds None. **`ABSENT` hoort nooit in de state** (0.49.4): 0.49.3 liet hem via `_read` ook in de weergave van de ingangen komen, waarna `GET /api/state` faalde (*Object of type object is not JSON serializable*) en de pagina *geen verbinding met de add-on* meldde; `_read` (pagina) geeft nu None en `_read_rule` (regels) geeft `ABSENT`, en een test zet de hele snapshot om naar JSON.
  - **Niet getest / open**: niet tegen een echte Miniserver. De voorbeelden zijn de teksten die de gebruiker noemde (`Morgen, 6:45`, en "andere formaten" zonder voorbeeld): **de exacte teksten van de wekker van Loxone zijn niet gezien**, de lijst hierboven is een gok die tolerant is. Een tekst die niet als tijd wordt herkend, staat bij Verkeer en bij het signaal (*geen tijd in de tekst: onbekend*); geef die tekst door en `parse_alarm` wordt aangevuld. Of een virtuele uitgang in Loxone Config een tekst (en geen getal) kan sturen is ook niet bevestigd: het bestand maakt de commando's analoog (`<v>`); zo niet, dan moet de tekst via een Pico-C-blok of een tekstvariabele in het commando. Een lamp kan een `x:`-waarde nog niet als *disable* gebruiken (alleen `t:` en `m:`): maak er een module van.
- **Apparaatsjablonen** (`add_device`): kies een apparaat en een voorvoegsel; de punten die op zijn blok staan worden `<voorvoegsel>_<punt>` (naar Loxone), de instelbare ook `<voorvoegsel>_<punt>_set` (van Loxone), en `<voorvoegsel>_ok` meldt of het apparaat bereikbaar is. Dat past voor Hue, Smappee, Brink en Nuki, omdat die al punten zijn. Een enum noemt zijn nummers in het label.
- **Naar Loxone** (UDP, één socket, `loxonehub.tick` elke seconde): bij een verandering (met een dode band en hoogstens één bericht per seconde per signaal), elke 60 s opnieuw (`heartbeat`: een Miniserver verliest zijn ingangen bij een herstart, UDP is niet gegarandeerd), en 10 s na het starten (de volledige stand, uitgespreid). Een onbekende waarde (apparaat offline) wordt **niet** verstuurd; wel meldt `_ok` dat het apparaat weg is, zodat Loxone zelf een time-out kan hanteren. Hoogstens één bericht per 50 ms op de lijn: de Miniserver is waarschijnlijk een eerste generatie (serienummer `504F94A0…`, door de gebruiker vermoed Gen1) en slikt geen vloed.
- **Een slot bedienen vanuit Loxone** (0.40): op verzoek van de eigenaar. Een signaal kan een **actie** van een Nuki-slot zijn (`ref` `a:<slot>:<actie>`, alleen acties zonder argument; `loxonehub._action`). Een punt dat `danger` is, blijft voor Loxone onbereikbaar; een actie is een ander pad, met dezelfde regels als de ingang op het blok (§37, `_tick_action`) en nog twee: (1) een 1 start iets, een 0 niets; (2) een actie die de deur opent (`danger`: ontgrendelen, unlatch, lock 'n' go) start alleen bij de **overgang van 0 naar 1** (`act_mem`; de eerste waarde na een start van de add-on wordt alleen onthouden en geweigerd met *geen overgang*), zodat een herstarte Miniserver die zijn 1 nog eens stuurt niets opent. Dat werkt omdat de uitgang in het bestand bij een digitale uitgang 1 **en** 0 stuurt (`CmdOn` en `CmdOff`). (3) Een gevaarlijke actie komt **alleen over HTTP** (TCP is niet blind te vervalsen, een UDP-pakket wel, en de enige controle is het IP van de afzender), en **alleen met `ack`**: de gebruiker bevestigt bij het maken (vinkje in de UI) en `configure` weigert UDP zolang er zo'n signaal is. (4) Hoogstens eens per `ACTION_GAP` (5 s) per signaal, nooit wachten (een slot dat niet bereikbaar is geeft *niet bereikbaar*, de opdracht vervalt), nooit opnieuw proberen. (5) Elke start staat als waarschuwing in het logboek (`Loxone start … op … (signaal …, via …)`), ook elke weigering. Vergrendelen is gewoon (UDP of HTTP, wel de pauze van 5 s). **Wat dit niet dekt**: een Miniserver of een ander apparaat op het LAN dat vanaf het IP van de Miniserver een geldig HTTP-verzoek stuurt, en een logica in Loxone die 0 en dan 1 geeft. Er is geen geheim in het bericht; de bescherming is het IP en het netwerk. Niet geprobeerd tegen een echt slot of een echte Miniserver.
- **Van Loxone**: over UDP (poort `loxone_port`, standaard 47510) of HTTP (`GET` of `POST /loxone`, de waarde in de query of de body; HTTP geeft een antwoord per signaal, `ok` of de reden). Alleen van het IP van de Miniserver; zolang dat niet is ingesteld wordt alles genegeerd. Alleen absolute waarden. Een waarde gaat door `_snap` (binnen het bereik, op de stap) en `EspDevHub.set` (dezelfde controles als de zijbalk); een punt dat niet instelbaar is of `danger` is, kan niet als `in`-signaal worden gemaakt (nooit een deur). Een waarde die het punt al heeft, doet niets (spaart de grenzen van Brink en Smappee en breekt een lus). `skip_first`: de eerste waarde na een start onthouden, niet uitvoeren (voor een signaal dat een herstarte Miniserver met een oude waarde kan overschrijven). `kk.sync=1` stuurt alles opnieuw.
- **De bestanden** (`/api/loxone/viu.xml`, `vo.xml`, `table.txt`): het formaat is overgenomen uit vier echte exports van de gebruiker (een virtuele UDP-ingang, twee virtuele uitgangen over UDP en HTTP, een met een POST-body): `VirtualInUdp` met per signaal een `VirtualInUdpCmd` (`Check` = `kk.<naam>=\v`, analoog, `Unit` `<v.N>`), `VirtualOut` met per signaal een `VirtualOutCmd` (aan/uit: `CmdOn` en `CmdOff`; analoog: `<v>` in `CmdOn`). De tests vergelijken de attributen met drie van die exports. De ingang filtert op het IP van de add-on (`Address`).
- **Per apparaat** (0.38.1): een signaal hoort bij het apparaat in zijn bron (`e:<apparaat>:<punt>` of `online:<apparaat>`; een timer of module is groep `-`, "Overig"). Het overzicht is per apparaat in te klappen (open/dicht onthouden in de browser) en op apparaatnaam en dan signaalnaam gesorteerd. De downloads nemen `?devices=a,b` (leeg = niets, weggelaten = alles); de titel van het bestand noemt dan de apparaten en de bestandsnaam hun id's, zodat twee imports in Config uit elkaar te houden zijn. **Een sjabloon dat al in het project staat, wordt niet bijgewerkt door opnieuw te importeren**, commando's toevoegen aan een bestaande ingang of uitgang via een bestand lukt niet, en twee virtuele UDP-ingangen op dezelfde poort kunnen niet (door de gebruiker geprobeerd; kopiëren en plakken tussen ingangen werkt ook niet). Dus: nieuwe signalen **naar** Loxone worden overgetypt uit de tabel (`table.txt?devices=…`); een nieuwe virtuele **uitgang** mag wel naast de bestaande staan, dus daar is het bestand per apparaat een echte oplossing. Overwogen en niet gekozen: een poort per apparaat (kan nu wel als kanaal, maar dan kies je dat zelf), reserveplekken `kk.s001…` (onhandig) en een virtuele HTTP-ingang die de add-on pollt (elke import een eigen URL; het formaat is niet bekend; de gebruiker wil UDP behouden).
- **Verkeerslogboek** (0.41.1): `traffic` houdt de laatste 150 berichten bij (tijd, in of uit, bericht, route, uitslag); in Instellingen → Loxone → Verkeer, met een filter. Aanleiding: de gebruiker zag in Loxone op vaste intervallen kortstondig een 1 bij het bewegingssignaal van een Hue-melder. Onderzocht: de bridge zelf (alleen lezen, eventstroom 4 min en de lijst elke 15 s gedurende 7 min) meldde in die tijd geen enkele beweging; een tweede melder was onbereikbaar (`motion_valid` onwaar) en dus onbekend, nooit verstuurd. De add-on stuurt elke 60 s de huidige waarde opnieuw (`heartbeat`), dus ook een 0, en niet uit zichzelf een 1. **Oorzaak** (door de gebruiker gevonden): het commando stond in Loxone Config als **digitaal** gedefinieerd. Een digitaal commando geeft bij elke ontvangst een korte puls (1 en dan weer 0), ook bij `kk.<naam>=0`; dat was de herhaling van 60 s. Ons bestand maakt elk commando **analoog** (`Analog="true"`, de waarde komt uit `\v`); hoe het digitaal werd (met de hand aangepast of een eerder gemaakt commando) is niet nagegaan. In de UDP-monitor van Config is de ontvangst zichtbaar als een regel met het herkende commando achteraan. Een eerste idee (herhaling per signaal uitzetten) is dus niet gebouwd.
- **Kanalen** (0.41, op verzoek van de gebruiker, die eerst een poort per apparaat afwees en daarna zelf "omgevingen" voorstelde; de naam *kanaal* is van mij): een kanaal is **één virtuele UDP-ingang plus één virtuele uitgang** in Loxone, met een eigen poort voor de ingang. Het standaardkanaal (`main`) heeft de poort `out_port`; de andere staan in `channels` (`id -> {name, port}`, hoogstens 20, poorten uniek; een Miniserver kent volgens een forumbericht hoogstens ~50 UDP-poorten). Een **apparaat** (groep) zit in één kanaal (`assign`: groep -> kanaal; wat er niet in staat zit in `main`); al zijn signalen gaan dan naar de poort van dat kanaal (`_pump` kijkt de poort per signaal op) en staan in het bestand van dat kanaal (`viu(devices, channel)`, `vo(...)`, `table(...)`; `?channel=<id>`). De titel van het bestand noemt het kanaal, de bestandsnaam ook. **Waarom**: Config werkt een sjabloon niet bij en twee virtuele UDP-ingangen kunnen niet op dezelfde poort; een nieuw kanaal is wel gewoon een nieuwe import, zonder iets te veranderen aan wat er al staat. De uitgang (van Loxone) heeft dat probleem niet, maar hoort bij hetzelfde kanaal voor het overzicht. Een apparaat verhuizen (`set_channel`) stuurt meteen alles opnieuw, naar de nieuwe poort; de gebruiker haalt zelf de commando's bij het oude kanaal weg en importeert het nieuwe. Een kanaal dat nog apparaten heeft kan niet weg. Een poortwijziging stuurt ook alles opnieuw. Niet getest tegen een echte Miniserver: of meerdere virtuele UDP-ingangen op verschillende poorten naast elkaar werken ligt voor de hand maar is niet gezien.
- **Loxone roept de add-on aan via HTTP of UDP**: HTTP geeft een bevestiging en een foutmelding; UDP is lichter. De gebruiker kiest in Instellingen; het bestand volgt.
- **Migratie van JavaLox**: `kk.<naam>` naast de oude ingangen laten bestaan, de bronnen parallel laten sturen en per koppeling omhangen. Wat JavaLox deed en wat hier staat: Hue-beweging (punt `motion` van een Hue-melder), Smappee-verbruik (§39), Nuki-stand (§37), Brink (§38: de oude API van JavaLox bestaat niet meer, deze koppeling werkt dus waarschijnlijk al niet). Hue-kamers, scenes en helderheid per kamer staan sinds 0.39 in §36 (een kamer is een apparaat, dus `add_device` werkt ook voor haar). **Niet in de add-on, en dat blijft zo**: de Mikrotik-tv-detectie (besloten: die komt er niet); `smartthings` in de configuratie van JavaLox is dode configuratie.
- **Smappee**: JavaLox en de add-on kunnen hetzelfde client-id gebruiken en dus hetzelfde budget van de officiële API (§39). Zet de Smappee-koppeling van JavaLox uit zodra de add-on het overneemt.

### Niet getest / open
- **Niet tegen een echte Miniserver geprobeerd.** De tests gebruiken een UDP-socket als nagebootste Miniserver en een nagebootst HUISDEV-apparaat. Het formaat van de bestanden komt uit echte exports, maar **of Loxone Config een gegenereerd bestand zonder klagen importeert, is niet gezien**.
- **Hoe Loxone een commando herkent** (alleen aan het begin van het bericht of overal; de exports van de gebruiker beginnen met `\i`) is niet bevestigd. De add-on schrijft `kk.<naam>=\v`; werkt dat niet in de UDP-monitor van Config, zet er dan `\i` voor.
- **Gen1 of Gen2** is niet vastgesteld (wel: serienummer `504F94A0…`). De grenzen in `MinVal` en `MaxVal` van een ingang (±10 000 000 als het punt er geen heeft) en `START_DELAY`, `GAP`, `MIN_GAP` zijn aannames; stel ze bij als de Miniserver berichten mist.
- **Niet gebouwd**: (Mikrotik komt er niet), tekst naar Loxone (een virtuele tekstingang via `/dev/sps/io`), een eigen Pico-C-parser, een waarde uit Loxone op een kaart of als disable van een lamp (de logica kan het sinds 0.49: zie hierboven), signalen op het schema als blok per apparaat, en het kiezen van andere bronnen dan een apparaat in de UI (de API kan het al).

## 41. Reolink-camera als HUISDEV-apparaat (0.42)

Code: `reolink.py` (puur: de verzoeken en antwoorden van de camera, de punten, de waarden, de opdracht voor de schijnwerper), de Reolink-kant van `espdevhub.py` (`add_reolink`, `_run_reolink`, `_reo_cycle`, `_reo_send`, `_reo_view`, `_webhook_server`, `_reo_hook`), de knop *Reolink-camera toevoegen* en de zijbalk in `schema.js`, `tests/test_reolink.py` met een nagebootste camera (`tests/fakereolink.py`).

- **Waarom**: de gebruiker heeft een Reolink Duo Series P750 en wil beweging, persoon, voertuig en de schijnwerper als blok, en daarna naar Loxone (§40: het zijn gewone punten, dus `add_device` werkt).
- **Zelfde opzet als ESPHome (§35)**: **een taak per camera** (`driver: "reolink"`, `host`, `port`, `auth` = `gebruiker:wachtwoord`, alleen in `espdev.json` (0600), nooit naar de pagina), blok `e:<id>`, zijbalk, bronnen, ingangen, `shown` en de HA-entiteiten (standaard uit: Reolink staat in HA al via zijn eigen integratie) zijn dezelfde code.
- **Toevoegen** (`add_reolink`): inloggen (`Login`), één verzoek met `GetDevInfo`, `GetMdState`, `GetAiState` en `GetWhiteLed` (`caps_from_probe`), en de punten volgen wat de camera meldt: *Beweging* altijd; *Persoon* en *Voertuig* alleen als `GetAiState` `support: 1` geeft; *Schijnwerper* (aan/uit) en *Helderheid schijnwerper* (0 tot 100, 0 is uit, zoals bij een Hue-kamer §36) alleen als `GetWhiteLed` geen fout geeft (een camera zonder schijnwerper geeft `code` 1, "ability error"). Het id is `reolink-<serienummer>`. De caps staan in `dev["reo"]`.
- **Inloggen en token**: `POST /cgi-bin/api.cgi?cmd=<eerste opdracht>&token=<token>` met een JSON-array; het antwoord is een array in dezelfde volgorde. Het token geldt `leaseTime` seconden (we loggen 120 s eerder opnieuw in). Een verlopen sessie (`rspCode` -6 of "login" in de melding) is geen reden om offline te gaan: meteen opnieuw inloggen (hoogstens twee keer achter elkaar). Een geweigerde aanmelding is een harde fout (offline met de reden); we proberen het daarna op het gewone ritme, niet vaker.
- **Pollen met het webhook als wekker**: zonder webhook vraagt de taak elke 2 s (`REO_SLOW`); zolang er beweging of een detectie is, of net een webhook of opdracht kwam, elke seconde (`REO_FAST`, `REO_HOLD` 20 s); heeft de camera ooit een webhook gestuurd, dan elke 30 s (`REO_HOOKED`) en wekt het webhook de taak (`wake`) bij een gebeurtenis. De camera stuurt het webhook (Instellingen, Surveillance, Push, Webhook, alleen in de webinterface, nieuwere firmware; de Duo 2, Duo 3 en Duo Floodlight staan in de lijst van Reolink) als `POST` met JSON naar `http://<add-on>:<webhook_port>/reolink/<apparaat>/<geheim>` (standaard 47520). **Wat er in het webhook staat gebruiken we niet**: Reolink zegt niet welke waarden `type` of `message` hebben, of het ook een einde van de gebeurtenis meldt, en hoe het `secret` werkt. Het webhook zegt dus alleen "kijk nu"; de stand blijft die van de poll. De laatste inhoud staat in de zijbalk (`hook_last`, 600 tekens), zodat je ziet wat jouw camera stuurt.
- **Het webhook is beveiligd** zoals het kan: een geheim van 24 hexcijfers per camera in het adres (`hmac.compare_digest`; een verkeerd geheim en een onbekende camera geven dezelfde 404), en alleen van het IP van de camera (`request.remote`). Het doet niets behalve een poll wekken.
- **Bedienen**: `SetWhiteLed` met `channel`, `state`, `mode` en `bright`; `mode` en `bright` blijven wat de camera meldde (zonder bekende stand: modus 1, helderheid 100). Absoluut, nooit een toggle. De camera zet de schijnwerper na een tijd zelf weer uit (volgens Loxone's beschrijving na 3 minuten).
- **Niet getest / open**
  - **Niet tegen een echte camera geprobeerd**, ook de eerste aanmelding niet. De vorm van de verzoeken en antwoorden komt uit de bibliotheek `reolink_aio` en de documentatie van Reolink (gelezen, voor een deel niet verbatim); de nagebootste camera is naar die vorm gebouwd.
  - **`SetWhiteLed`**: de betekenis van `mode` (0 uit, 1 automatisch 's nachts, 3 op een tijd) is in de bibliotheek zelf nog "te bevestigen". Of `state: 1` de schijnwerper aanzet in elke modus is niet gezien. Lukt het niet, dan blijft de stand uit en meldt de poll dat.
  - **De Duo Series P750** staat niet letterlijk in de lijst voor het webhook (wel Duo 3 PoE, Duo 2 en Duo Floodlight): of jouw model en firmware het hebben, zie je in de webinterface van de camera.
  - **Een camera achter een NVR** heeft geen webhook en soms een andere API; niet bedoeld.
  - **Alleen kanaal 0**, geen meerdere kanalen (een Duo is één camera).
  - **Geen ONVIF of Baichuan** (de push van Reolink zelf); alleen deze API en het webhook.

## 42. Scènes voor ledstrips via HUISDEV 1.1 (0.43), helderheid en tijdschuif met HUISDEV 1.2 (0.44)

> Sinds 0.45 zitten de scène-ingangen en de selectie in een eigen blok Scènes (§43); wat hieronder over `scene:<id>`-ingangen op het apparaatblok en `PUT /api/espdev/<id>/scenes` staat, geldt alleen nog als voorgeschiedenis. De verzender, slots, sync en geplande starts zijn ongewijzigd.

Code: `scenehub.py` (bibliotheek, selectie en slots, flanken, sync, geplande starts), `scenesweb.py` (routes), `static/scenes.js` (zijbalk), `scenes.py` (het formaat `ledscene/1`), `protocol/huisdev/` (HUISDEV 1.1, `huisdev.h` 1.1.0), `protocol/ledscene/` (`LEDSCENE.md`, `ledscene.h`, testvectoren), `tests/test_scenehub.py` en `tests/test_scenes.py` met een nagebouwd apparaat (`tests/fakehuisdev.py`).

- **Waarom**: een ESP met ledstrip (de kidswekker) moet door de huisconfig een kleur of een overgang in een tempo (zonsopkomst) kunnen krijgen, en die ook afwerken als de verbinding wegvalt. HUISDEV 1.0 kan dat niet: een actie met argumenten is voor de logica niet te bedraden, een punt is een getal en een tekst is geen bron. Een **scène** is daarom een klein binair pakketje (base64 in JSON) dat de add-on bouwt en het apparaat **zelf en volledig afspeelt**; het apparaat bewaart er acht in flash en een lijst geplande starts.
- **Protocol 1.1** (zie `HUISDEV.md` §16): optioneel `scenes` in descriptor en state, endpoints `/huisdev/scene/store`, `play`, `stop` en `schedule`. Een apparaat zonder scènes houdt dezelfde `rev`; een 1.0-add-on negeert de velden.
- **Formaat** (`LEDSCENE.md`): keyframes met spans (positie, verloop van kleur 1 naar 2), `dt`, `ease`, `lum` en `scroll` per frame, geheel getal en bit-exact: de Python- en de C++-implementatie geven voor dezelfde bytes en tijd dezelfde uitvoer (gedeelde vectoren in `protocol/ledscene/vectors/`). De add-on kent generatoren (statisch, kleurverloop, zonsopkomst, wekker, regenboog) die naar die bytes compileren.
- **Reviews vooraf** (vier invalshoeken) vonden onder meer: een eenmalige `at` volstaat niet voor een dagelijkse wekker (daarom `schedule` met een lijst en `sched` in de state), `left` hoort niet in de state (verandert elke poll), `CURRENT = (1, 1)` zou alle niet-HUISDEV-drivers als verouderd tonen (nu alleen `driver == "huisdev"`), en de eerste compositing-regel dimde de naad tussen twee spans (gevonden door de testvectoren met een regenboog op 300 leds; nu een gedekt-deel per led).

### Bestanden

| Bestand | Rol |
|---|---|
| `addon/kkaddon/scenehub.py` | alle logica: bibliotheek, selectie en slots, flanken en verzender, sync, geplande starts, de seeks van de tijdschuif, view voor de pagina |
| `addon/kkaddon/scenesweb.py` | de routes (`/api/scenes...`, `/api/espdev/<id>/scenes`, `.../scene/play`, `.../scene/stop`, `.../scene/seek`, `.../scene/seek/stop`) |
| `addon/kkaddon/static/scenes.js` | de sectie "Scènes" in de zijbalk van een apparaat, het formulier en de voorbeeldstrip (geladen na `schema.js`) |
| `addon/kkaddon/scenes.py` | ledscene/1 (ontleden, bouwen, renderen, `compile_def`, `preview`, `frame_at`) |
| `addon/tests/fakehuisdev.py` | het nagebootste apparaat kent nu scènes (`scenes(...)`, `kidswekker()`) |
| `addon/tests/test_scenehub.py` | de tests |

### Gegevens

* `/data/scenes.json`: `{"next": N, "scenes": {"s1": {"name", "def", "release"}}}`. Ids `s<n>`, **nooit hergebruikt** (`next` telt alleen op, ook na
  verwijderen en herstarten). De naam is vrij te wijzigen. `def` is een `scenes.compile_def`-definitie (`static`, `fade`, `sunrise`, `wake`, `rainbow`,
  `raw`); bewaren compileert en valideert, een `SceneError` wordt een `ModelError` met de reden. `release` is `stop` (1 naar 0 stopt) of `finish` (laten uitlopen).
  Hash en bytes worden uit de definitie berekend en niet bewaard.
* `dev["scn"] = {"sel": [ids], "slots": {id: slot}, "cap": {formats, slots, max_bytes, sched}}` in `espdev.json` (ontbreekt of is stuk: leeg). `sel` is
  welke scènes op dit apparaat staan; `cap` is het `scenes`-object uit zijn descriptor (`None`: dit apparaat heeft geen scènes). De sleutel `dev["scenes"]`
  is van de Hue-driver en wordt niet gebruikt.
* `dev["inputs"]["scene:<id>"]`: de bron (een 0/1) van een scène. Een punt-id mag geen `:` hebben, dus dit botst nooit met een punt. De bedrading van een
  scène die van het apparaat gaat of uit de bibliotheek verdwijnt, wordt opgeruimd. Heeft het apparaat (nog) geen `scenes` in zijn descriptor, dan blijft
  alles bewaard maar slaapt het: geen rij op het blok, geen flanken, geen sync.
* Runtime per apparaat (`SceneHub.rt`, niet bewaard): `have`, `run`, `sched`, `time` van de laatste poll, plus de bewaking (zie sync).

### Protocol 1.1 in `espdevhub.py`

* `CURRENT = (1, 1)` (sinds 0.44 `(1, 2)`, zie hieronder). "Upgrade beschikbaar" (`proto_state`) geldt alleen voor `driver == "huisdev"`: Hue, Nuki, Brink, Smappee, Reolink en ESPHome zeggen vast 1.0.
* `clean_descriptor` bewaart een geldig `scenes`-object (formats = lijst strings, 1 <= slots <= 8, 1 <= max_bytes <= 1024, slots * max_bytes <= 6144,
  0 <= sched <= 8); ongeldig is weglaten, nooit falen. De foutcodes `bad_slot`, `too_large` en `bad_data` staan in `ERRORS`.
* `_on_state` leest `state.scenes`. `time` en `run.until` tellen niet als wijziging voor de pagina (anders ververst de pagina elke 3 s).

### API

| Route | Body en antwoord |
|---|---|
| `GET /api/scenes` | `[{id, name, def, release, hash, bytes, bounded, total_ms, window_ms}]` |
| `POST /api/scenes` | `{name, def, release?}` geeft `{ok, id}` |
| `PUT /api/scenes/<id>` | `{name?, def?, release?}` |
| `DELETE /api/scenes/<id>` | ruimt overal op (selectie, bedrading, slot, wat nog wacht); 404 als hij niet bestaat |
| `POST /api/scenes/preview` | `{def, n?, samples?}` geeft `scenes.preview` (`n` leds, `frames` per tijdstip, kleuren 0..255, `times`, `bounded`, `total_ms`, `window_ms`) |
| `PUT /api/espdev/<id>/scenes` | `{scenes: [ids]}`: welke scènes op dit apparaat staan (een rij en een slot elk) |
| `POST /api/espdev/<id>/scene/play` | `{id}` (bibliotheekscène) of `{slot}`: nu spelen; geeft `{ok, message}` |
| `POST /api/espdev/<id>/scene/stop` | `{}` (alles), `{slot}` of `{id}` (alleen als die scène nog speelt) |
| `POST /api/scenes/frame` | `{def \| id, t_ms, n?}` geeft `{t_ms, total_ms, window_ms, bounded, frame}` (het beeld op dat tijdstip, kleuren 0..255) |
| `POST /api/espdev/<id>/scene/seek` | `{def \| id \| slot, t_ms}`: de strip toont dat beeld en houdt het vast (1.2); `{ok, message, superseded?}` |
| `POST /api/espdev/<id>/scene/seek/stop` | stopt het vasthouden (alleen als het apparaat een seek-beeld toont) |

Fouten zijn `400 {"error": "<Nederlandse tekst>"}` zoals overal. `GET /api/state` bevat per apparaat `scn`: `{cap, known, sel: [{id, name, slot, release, state, src, note,
sched_note}], have, run: {slot, h, until, seek, id, name}, sched: [{slot, at, id, name}], time, note}`; `state` is `ok`, `wait`, `noslot`, `toolarge` of `broken`; `run.seek` is het tijdstip (ms) als het apparaat een beeld uit de tijdschuif toont, anders `null`; `cap.seek` is `true` bij een apparaat dat `scene/seek` kent.

### Slots

Een scène krijgt bij het selecteren het laagste vrije slot (1..`slots`) en houdt het zolang ze geselecteerd is; een slot wordt nooit aan een andere scène
gegeven terwijl de eerste er nog staat. Zijn er te veel scènes, dan blijft de rij staan met de melding "geen vrij slot" in `last["scene:<id>"]` tot er een
slot vrijkomt.

### De ingang van een scène (`tick()`, een eigen tak)

Per geselecteerde scène een rij met een bool-ingang op het blok (zoals een Nuki-actie, `ports[].scene = true`). Het blok toont ▶ en de naam.

* De **eerste stand** (na een herstart of na onbekend) wordt alleen onthouden.
* **0 naar 1** start. Staat de scène in zijn slot met dezelfde hash (`have[n].h`), dan `scene/play {"slot": n}`; anders rechtstreeks `scene/play {"data": <base64>}`. Een
  live trigger gebruikt dus altijd de actuele bibliotheek en wacht nooit op de sync. Faalt `play {slot}` (het slot is intussen anders), dan eenmaal rechtstreeks.
* **1 naar 0** stopt alleen bij `release == "stop"`, en alleen wat deze scène zelf speelt: de add-on vraagt eerst de verse state; speelt `run.h` een hash van deze
  scène (de huidige, die van de start, of die in het slot), dan `scene/stop {"slot": run.slot}`, of `scene/stop {}` bij een rechtstreekse start (`run.slot == 0`). Speelt er iets
  anders (met de hand gestart), dan blijft dat ongemoeid. Bij `finish` gebeurt er niets.
* **Een verzender per apparaat**: een wachtrij met een werker (`hub.tasks["scn:<did>"]`) en een `asyncio.Lock` waar ook de sync en de knoppen op de pagina door moeten.
  De volgorde is die van de flanken en de laatste wint; een 0, 1, 0 binnen 5 s verliest zijn stop niet. Geen `ACTION_GAP`: dat is voor deuren.
* **Offline**: een start vervalt als het apparaat niet binnen 60 s bereikbaar is (de scène heeft een `resume`-venster; een wekker die een uur te laat start is erger dan
  geen), met een melding. Een stop wacht tot het apparaat terug is (hoogstens 6 uur), tenzij er in de tussentijd een nieuwere start van dezelfde scène kwam.
* **Mislukking**: een weigering (`rejected`, 4xx) wordt niet opnieuw geprobeerd; een netwerkfout hoogstens 5 keer met 2 s ertussen (alleen terwijl het apparaat online staat). Alles komt in
  `last["scene:<id>"]` en als `espdev`-event (`what="scene"`).

### Sync (na elke poll, `EspDevHub._run`)

Vergelijkt `state.scenes.have` met de bibliotheek voor elke geselecteerde scène met een slot. Alleen bij een afwijking, en met bewaking:

* hoogstens **een schrijfactie per ronde** (een `store`, een wissen of een `schedule`);
* dezelfde (slot, hash) hoogstens **eens per 10 minuten**, hoogstens **6 schrijfacties per uur** per apparaat (en 6 `schedule`-acties per uur);
* de teruggegeven `hash` van een `store` wordt gecontroleerd; wijkt hij af, dan een melding en pas na 10 minuten opnieuw;
* een slot dat gevuld is maar aan geen geselecteerde scène hoort, wordt gewist (`data: ""`): ook wat er met de hand of door een oude add-on in kwam;
* een leeg of teruggezet apparaat wordt opnieuw gevuld, een slot per ronde (en dus binnen de bewaking: na een reset kan het een poos duren);
* `ledscene/1` niet in `formats`: geen enkele bewerking, een melding in `last["scenes"]`; een scène groter dan `max_bytes` wordt niet gestuurd (`state: toolarge`).

### Geplande starts voor een wekker zonder verbinding

Voor een scène waarvan de bron een **timer** `t:<id>` is (de intervallen per weekdag van `model.timers`), rekent de add-on de eerstvolgende opkomende flanken (0 naar 1) uit,
tot 13 dagen vooruit en hoogstens `sched` per scène, en stuurt de lijst van **alle** geplande scènes van het apparaat samen, gesorteerd en afgekapt op `sched`, als `scene/schedule`.

* **Rekenen** (`scenehub.rising_edges`): per minuut over de echte tijdlijn (epoch), met dezelfde `timer_active` als de engine, in de tijdzone van de add-on. Een interval dat aansluit op het vorige
  (einde 00:00 en begin 00:00, of 07:00 en 07:00) is geen flank. Een zomertijdgrens klopt vanzelf: 7 uur blijft 7 uur, een begintijd die niet bestaat (02:30 op de dag van de
  omschakeling) start bij de eerste minuut die er is, en een tijd die twee keer voorkomt geeft twee flanken, zoals de engine hem live ook twee keer ziet. Het resultaat wordt per minuut onthouden.
* **Sturen** (`needs_write`) met een laagwatermerk: niet bij elke verlopen regel, wel als het apparaat minder dan 3 regels over heeft, als het overlappende deel verschilt (een gewijzigde
  timer), of als de lijst leeg is terwijl ze gevuld hoort te zijn (en omgekeerd). Regels die binnen 90 s afgaan of net voorbij zijn tellen niet mee en blijven bij een herschrijven in de lijst: het
  apparaat loopt misschien een paar seconden achter en haalt zo'n regel nog.
* **Alleen als** het slot gevuld is en klopt (`have`), er geen slot meer wacht, en de klok van het apparaat geldig is (`time`). Een bron die geen timer is (module, `e:`-bron) of een timer die
  niet meer bestaat: geen schema en een melding (`sched_note`), de gewone flank blijft werken.
* Aanvullend op de gewone flank: dubbel starten is onschadelijk (`play` van wat al speelt is een no-op). Een scène of ingang weghalen, of de bron ontkoppelen, wist de regels bij de volgende sync.

### Helderheid per scène (0.44, geen protocolwijziging)

`compile_def` kent voor alle soorten behalve `raw` het veld `brightness` (1..100, standaard 100, een getal; 0 is niet toegestaan: een volledig zwarte scène heeft geen zin). Het is een
percentage **licht**: elk frame krijgt `lum' = round(lum * (brightness/100) ** (1/2.2))` (`lum` is gamma-gecodeerd), zodat 50 echt de helft van het licht is. Het werkt bovenop de
`brightness` van het apparaat. **Bij 100 zijn de bytes identiek aan die van 0.43** (vastgelegd als hex in `test_scenes.py`): een bestaande scène krijgt geen nieuwe hash en niets wordt opnieuw
gesynchroniseerd. Bij `raw` is het veld een fout (de gebruiker zet `lum` per frame). De hash verandert dus alleen als de helderheid verandert.

### Tijdschuif en `scene/seek` (0.44, protocol 1.2)

* `POST /api/scenes/frame {def | id, t_ms, n?}` geeft `{t_ms, total_ms, window_ms, bounded, frame: [[r,g,b]...]}`: het beeld op dat tijdstip, met dezelfde `render` als het apparaat en kleuren 0..255 zoals
  `preview` (`scenes.frame_at`, 0 <= t_ms <= 86 400 000, n standaard 24). `window_ms` is wat de schuif beslaat: de duur (`total_ms`), bij een onbegrensde scène een ronde van de lus of de speelduur
  (0 bij een scène die niet verandert; de schuif staat dan uit). De bibliotheek (`GET /api/scenes`) geeft per scène ook `window_ms`.
* `CURRENT = (1, 2)`; `clean_descriptor` bewaart `scenes.seek` (alleen als het `true` is). Een 1.1-apparaat verandert niet en toont "upgrade beschikbaar".
* `POST /api/espdev/<id>/scene/seek {def | id | slot, t_ms}` laat de strip het beeld op `t_ms` tonen en vasthouden (`scene/seek` van HUISDEV §16); antwoord `{ok, message: "toont m:ss"}`, of `{ok, message, superseded: true}` als er
  intussen een nieuwere seek of een start/stop kwam. `POST .../scene/seek/stop` stopt het vasthouden, maar alleen als de verse state een `run.seek` toont (iets anders dat speelt blijft ongemoeid).
  Zonder `seek` in de descriptor: 400 "dit apparaat kan niet door een scène scrollen (firmware ouder dan 1.2)"; offline: 400 "niet verstuurd: geen verbinding", niets in een wachtrij.
* **Samenvoegen** (`SceneHub.seek`, `_seek_worker`): een verzender per apparaat (`hub.tasks["seek:<did>"]`, onder dezelfde `asyncio.Lock` als de ingangen en de sync, dus nooit twee verzoeken tegelijk). Een nieuwe seek vervangt
  wat nog wacht (de oude aanroeper krijgt `superseded`); de verzender wacht `SEEK_GAP` (0,125 s, dus hoogstens 8 per seconde) na de vorige en stuurt dan het **nieuwste** tijdstip.
* **Bytes of alleen `{"t"}`**: de eerste seek, of een andere scène/definitie/hash, gaat met `data` (base64; `{"slot": n}` bij een slotseek); daarna alleen `{"t": ms}`, zolang de vorige seek dezelfde scène was en
  `run.seek` er nog is. Weigert het apparaat zo'n `{"t"}` ("geen scène om te tonen": het vasthouden verliep na 300 s, een herstart, iets anders nam het over), dan een tweede keer met `data`.
* **Een start of stop wint**: een flank van een scène-ingang (`_enqueue`), `play` en `stop` vanaf de pagina gooien wat nog wacht weg (`_cancel_seek`) en vergeten welke scène vastgehouden werd, zodat de volgende seek weer met de
  bytes gaat. Een geplande start op het apparaat vervangt het vasthouden zelf.
* **Raakt niets anders**: een seek komt niet in `stores`, `tried` of `sched_tries` (de uurplafonds), `sched`, de slots of de sync-bewaking; de sync slaat een ronde over als de verzender juist bezig is.
  `state.scenes.run.seek` komt in `rt["run"]["seek"]` en in `view().run.seek`; alleen het verschijnen of verdwijnen telt als wijziging voor de pagina, het tijdstip niet.
* **Pagina** (`scenes.js`): onder de voorbeeldstrip en bij elke scène een `<input type="range">` met een tijdlabel (mm:ss) en een strip van het beeld; een streepje in de voorbeeldstrip toont de plek. Slepen haalt hoogstens 10 beelden per seconde
  (een tegelijk, het nieuwste wint). Het vinkje "ook op de strip van dit apparaat" (alleen bij `cap.seek`) stuurt elke beweging ook als seek (hoogstens 8 per seconde); wijzig je het formulier terwijl de schuif aanstaat, dan gaat de nieuwe definitie mee.
  Uitzetten, de editor sluiten, een ander blok kiezen of de pagina verlaten (`pagehide`, `fetch` met `keepalive`) stuurt een stop. De status komt uit `scn.run.seek` ("apparaat toont: mm:ss").

### Op de pagina

Sectie "Scènes" in de zijbalk van een apparaat met `scenes`: de bibliotheek met een vinkje "op dit apparaat", ▶ en ■, een formulier (naam, soort, parameters, stoppen of laten uitlopen),
een voorbeeldstrip (de server rekent, `scenes.js` tekent de kleuren op een `<canvas>` en toont ze als afbeelding; de tijd loopt naar beneden), de status ("speelt: ..., tot HH:MM") en de lijst
"gepland op het apparaat" in lokale tijd. De ingang staat bij "Ingangen" met de andere.

### Bewust

* Een apparaat zonder `scenes` verandert niet. Een 1.0- (of 1.1-) apparaat krijgt "upgrade beschikbaar" (zo staat het in HUISDEV §13); het werkt gewoon door.
* Een seek wordt niet in de wachtrij gezet en niet opnieuw geprobeerd: de schuif stuurt zo de volgende. Een netwerkfout is een melding bij de schuif.
* De pagina houdt het vasthouden niet zelf in leven: na 300 s zonder seek eindigt het op het apparaat (een stop bij sluiten is netjes, geen vereiste).
* De sync wist slots die aan geen geselecteerde scène horen. Wie op een apparaat zelf scènes bewaart (bijvoorbeeld via zijn eigen pagina), verliest die zodra de add-on dat apparaat beheert.
* Een bron die geen timer is, krijgt geen geplande start. Een module met `Tussen` of `Na` is niet vooruit te berekenen zonder de hele module-boom te simuleren.

### Niet gedaan

- Geen Home Assistant-entiteiten of MQTT voor scènes (een knop per scène kan later).
- De pagina is niet in een echte browser bekeken; de JS is op syntaxis en met een stub-DOM getest (ook de tijdschuif: tempo, het nieuwste wint, het vinkje, de editor, de stop).
- Het nagebouwde apparaat is gebouwd naar de spec, de echte `huisdev.h` is er niet tegenaan gedraaid (wel de host-tests van de header zelf).

## 43. Het blok Scènes: regels die een scène kiezen (0.45)

Eerst zat elke scène als rij met een 0/1-ingang op het apparaatblok (§42). Bij een kidswekker met veel scènes werd dat blok te vol, en een combinatie ("ingang 1 aan en de tijd tussen A en B") kon alleen met losse modules. Een eigen blok **Scènes** neemt dat over. Code: `scenerules.py` (gegevens, controles en rekenen, puur), `scenehub.py` (uitvoeren, selectie, geplande starts), `scenesweb.py`, `schema.py` (`_scene_blocks`), `static/scenes.js` (zijbalk met ingangen, regels en bibliotheek), `schema.js` (tekenen en slepen), `tests/test_scenerules.py`.

- **Gegevens**: `/data/scenerules.json`, `{"next": N, "blocks": {"q1": {name, device, inext, inputs: [{id, name, kind, ref}], rules: [{scene, when: [..]}]}}}`. Ids van blokken (`q<n>`) en ingangen (`i<n>`, per blok) worden nooit hergebruikt. `kind` is `bool` of `time`; `ref` een bron zoals bij een module (`t:`, `m:`, `e:` ...), gecontroleerd met `source_type`/`fits`. Blokssleutel op het schema: `q:<id>`. Hoogstens 16 blokken, 12 ingangen, 24 regels, 6 voorwaarden per regel en 16 verschillende scènes per blok; een apparaat heeft hoogstens één blok.
- **Voorwaarden**: `on`/`off` (0/1-ingang), `between` (van, tot: `logic._between`, dus over middernacht), `after`, `before`, `wake` (`in`, `before` 0..720, `hold` 1..720). Een wekker op een tijd-ingang is `between(t - before, t + hold)`. Op een 0/1-ingang met een timer (`t:`) en `before` > 0 telt de timer `before` minuten later in de tijd (`timer_active` op `nu + before`): het venster is dan [begin - before, einde - before) en `hold` doet niet mee (0). Vroeger starten op een 0/1 die geen timer is, weigert de server.
- **Kiezen** (`scenerules.evaluate`): per regel alle voorwaarden driewaardig (True/False/None); de **eerste regel met alleen True** wint. Een onbekende voorwaarde telt als niet waar, maar klopt er geen enkele regel terwijl een regel onbekend was, dan is het resultaat *onzeker*: `tick_rules` laat dan alles staan en vergeet de onthouden stand (de volgende bekende stand is weer een eerste stand, zoals bij een ingang in §42).
- **Uitvoeren** (`SceneHub.tick_rules`, elke tik van `EspDevHub.tick`, 3 s): per blok met een apparaat dat `scenes` meldt de gekozen scène t.o.v. de onthouden (`rmem[bid]`). Eerste stand: alleen onthouden. Andere scène: `_enqueue(start)`; geen scène meer: `_enqueue(stop)` voor de vorige, alleen bij `release: stop`. Daarna dezelfde verzender, wachtrij, TTL's, `_do_start`/`_do_stop` en foutafhandeling als in §42; een stop raakt alleen wat deze scène zelf speelt. A naar B start B zonder A te stoppen. Elke wijziging van het blok wist `rmem` (de eerstvolgende stand is weer een eerste stand).
- **Selectie**: `dev["scn"]["sel"]` volgt de scènes in de regels (`_apply_sel`, bij elke wijziging van een blok, bij het verwijderen van een scène en bij het laden). Slots, sync, `have`, `toolarge` enzovoort ongewijzigd. Een apparaat zonder blok houdt zijn `sel` zoals het was (`select()` blijft een interne methode voor tests, geen route meer). Een blok verwijderen leegt de selectie van zijn apparaat; een apparaat verwijderen laat het blok bestaan met `device: ""`; een scène verwijderen haalt haar regels weg.
- **Geplande starts** (`_rule_timer`, `_desired_sched`): alleen een regel met **één** voorwaarde `on` of `wake` op een 0/1-ingang met een `t:`-bron. De komende flanken komen uit `rising_edges(timer, nu + before*60)` min `before` minuten, gesorteerd samen met die van andere scènes. Een regel met meer voorwaarden, een bron die geen timer is of een tijd-ingang geeft `sched_note` ("geen geplande start: ..."); de gewone start blijft werken. Een regel die hoger staat en op dat moment ook klopt, wint live wel, maar niet bij de geplande start: het apparaat kent de regels niet (de reserve is bedoeld voor als het blok niet kan sturen).
- **Overgang** (`_migrate`, eenmalig bij het laden): `scene:<id>`-ingangen op een apparaat worden een blok `Scènes <apparaat>` met per ingang een 0/1-ingang en de regel `ingang is 1 → scène` (in de volgorde van de selectie), geplaatst naast het apparaatblok als dat op een pagina staat; de ingangen verdwijnen uit `dev["inputs"]`. Een scène zonder bron krijgt geen regel (haar slot wordt gewist). Heeft het apparaat al een blok, dan verdwijnen alleen de ingangen.
- **API**: `GET /api/scenerules`; `POST` (`{name?, device?, inputs?, rules?, page?, x?, y?}`, met `page` geplaatst op het schema); `PUT /api/scenerules/<id>` met `name`, `device`, `inputs` (hele lijst; een bestaande ingang met `id`, een nieuwe zonder), `refs` (`{ingang: bron}`, voor een draad op het schema) of `rules` (hele lijst); `DELETE`. Een lijst wordt gecontroleerd als geheel (een ingang die een regel gebruikt kan niet weg of van soort veranderen); een weigering verandert niets. `PUT /api/espdev/<id>/scenes` en de rijen `scene:<id>` op het apparaatblok bestaan niet meer. `GET /api/state` heeft `scenerules` en het schemablok `kind: "scenes"` met `ports`, `defs`, `rules` (met `on`: klopt nu), `active`, `active_rule`, `active_name`, `unsure`.
- **Pagina**: het blok toont links een poort per ingang en onderaan "▶ scène nu"; de zijbalk heeft het apparaat, de ingangen, de regels (↑ ↓ ✕, voorwaarden met keuzelijsten; elke wijziging gaat meteen naar de server) en de bibliotheek met formulier, voorbeeld en tijdschuif uit §42. Het apparaatblok toont alleen een verwijzing.

### Niet gedaan

- Geen vooruitberekening voor regels met meer voorwaarden of voor een wekker op een tijd uit een module (zie §42: de hele module-boom simuleren).
- Geen feestdagen, weekdagen of voorwaarden op getallen in het blok zelf; daarvoor zijn de bestaande timers en modules (Groter dan, Kleiner dan).
- Eén blok per apparaat; één blok bedient geen meerdere strips.
- De pagina is bekeken met een proefserver met het nagebootste apparaat (blok tekenen, regels wijzigen, voorwaarde toevoegen); de sleep-van-uitgang-naar-poort en het maken via + Module zijn alleen op syntaxis gecontroleerd.

## 44. De web-UI opnieuw ingedeeld (0.46)

Alleen de indeling en de stijl; de API, het model en de logica zijn niet aangeraakt.

- **Hoofdmenu**: een smalle zijbalk met pictogrammen, per soort gegroepeerd (Schema · Apparaten · Energie en Laden · Logboek en Instellingen), onder een kop met de statuschips. Op een smal scherm (< 760 px) wordt dat een balk onderaan. De tab *Nodes* heet nu *Apparaten* (`data-tab="nodes"` bleef). Het menu wordt in `app.js` (`NAV`) opgebouwd.
- **Pictogrammen** (`static/icons.js`): eigen inline-SVG's op 24 × 24 met `currentColor`, geen lettertype of CDN (de add-on draait ook zonder internet). `ico(naam, grootte)`; `MENU_ICON` koppelt de sleutels van `SC_MENU` aan een pictogram.
- **+ Module** is een flyout: links de rubrieken (de bestaande groepen uit `SC_MENU`, plus Zelfbouw/ESPHome, Hue, Nuki, Brink en Smappee), rechts wat erin zit; een rubriek opent bij erover te gaan, het zoekveld zoekt over alle rubrieken. De inhoud van de koppelingen (wat Hue of Nuki aanbieden) wordt bij elke verversing opnieuw gemaakt zolang het menu dicht is; eerder bleef die tot je het tabblad wisselde op de stand van het openen staan.
- **Werkbalk van het schema**: zoom als `− 83 % +` (klik op het getal = passend), *Ruimte ▾* met hernoemen, verwijderen, omzetten en opnieuw indelen, en daarnaast *+ Module*. De blokkenlijst links is per soort inklapbaar (onthouden zolang de pagina open is, het aantal nog niet geplaatste blokken staat erbij) en als geheel te verbergen (`localStorage` `kk.tray`).
- **Eigenschappen als venster**: `#scpanel` is een zwevend venster (`position: fixed`) naast het gekozen blok, in plaats van een vaste kolom van 300 px. Dezelfde `scPanel()`-functies vullen het: een kop met pictogram, naam en soort, *breder* (440 → 720 px), *sluiten* (ook Esc), en te verslepen aan de kop. De plek volgt de selectie (`SC.popFor`, `SC.popPos`) en wordt bij een verversing niet verschoven; tijdens het slepen van een blok is het venster vrijwel doorzichtig. Op een telefoon is het een blad onderaan. Wat er in de kolom stond zonder selectie (de uitleg over slepen) staat nu als regel onder het schema.
- **Niet gedaan**: de blokken op het schema hebben nog geen pictogram in hun kop (de tekstposities verschillen per soort), en de rubrieken in Instellingen zijn nog dezelfde vier.
- **Bekeken** in een echte browser met een lokale add-on en 13 blokken van elk soort: menu, zoeken, venster van alle soorten (geen horizontale overloop meer, behalve een lange badge die nu afbreekt), Esc en sluiten, smal scherm. `tests/test_web.py` kent `icons.js` nu ook in de versiestempel.
- **Reolink in een eigen rubriek** (0.46.1): "Reolink-camera toevoegen" stond onder Zelfbouw en ESPHome. Nu heeft het menu + Module een rubriek **Reolink** (`scReolinkMenu`, pictogram `camera`), net als Nuki en Brink. Alleen de knop (`data-sc="reoadd"`) is verplaatst; er is geen account en geen lijst.

## 45. Toegang: een logboek van detecties en deuren (0.47)

Code: `accesshub.py`, `static/access.js`, routes `/api/access` in `web.py`, `tests/test_access.py`. Opslag `/data/access.json`.

- **Bron = een punt van een HUISDEV-blok** (`e:<id>:<punt>`, soort bool of enum, geen diagnose): een Reolink (beweging, persoon, voertuig), een PIR of Hue-melder, een Nuki-stand, een deurcontact van ESPHome. Handmatig: er staat niets in het log tot je een punt aanzet (`set_sources`, het paneel Bronnen).
- **Polling, niet per driver**: `tick()` (elke seconde, taak `accessing` in `__main__`) leest `espdev.point_value`. Zo werkt het voor elk merk zonder een haak in de drivers; een puls korter dan een seconde gaat verloren.
- **Bool = periode** (`start`, `end`; `end` is `None` zolang ze loopt), **enum = moment** (`text` "Vergrendeld → Ontgrendeld", `start == end`). De eerste waarde na het opstarten is een uitgangspunt, geen verandering.
- **Onbekend is geen gebeurtenis**: een offline punt (`None`) geeft geen regel en sluit geen periode. Een lopende periode staat in het bestand en loopt na een herstart door, of sluit als het punt dan uit is. Een bron die uitgaat sluit zijn periode meteen.
- **Pagina**: filter op apparaat en periode, CSV, Log wissen (lopende perioden blijven), push-event `access` herlaadt de lijst. Max 5000 regels.
- **Niet gedaan**: een momentopname van de camera bij een detectie, en de regels als ingang voor scènes of meldingen. Nuki geeft niet door wie het slot bediende. Niet in een browser bekeken.

## 46. LG-tv met webOS als huis-screensaver (0.53)

- **Waarom**: de tv in de woonkamer moet een alleen-lezen dashboard tonen als HA dat wil, en niet als er gekeken wordt (`TV-OVERDRACHT.md`). Zoals bij de airco (§31) is het een eigen blok met poorten, zodat de bestaande modules (Tussen, En, Of) de beslissing nemen.
- **Eigen client in plaats van de HA-integratie**: `lg.py` praat de webOS-websocket (SSAP) zelf, met `wss://:3001` en `ws://:3000` als terugval, en koppelt met `register`. Dat houdt het blok onafhankelijk van HA. De prijs: pairing, sleutelopslag (`/data/lgtv.json`, 0600) en Wake-on-LAN zijn onze zorg.
- **Het manifest**: een ondertekend manifest van LG's voorbeeld-app (`com.lge.test`), zoals de open-source clients gebruiken, wordt door webOS 25 geweigerd (`403 … blacklisted certificate`). Een manifest zonder handtekening werkt, maar `turnOffScreen` en `turnOnScreen` antwoorden dan `401 insufficient permissions` zolang `CONTROL_TV_SCREEN` ontbreekt. Beide zijn op een 85QNED93A6A vastgesteld; `test_lg.py` bewaakt de rechten.
- **Wat de tv meldt**: drie abonnementen (voorgrond-app, `getPowerState`, volume). De stand `Screen Off` is wakker maar donker. `aan` (uitgang) betekent: verbonden en `Active`; een tv die uit staat luistert niet meer, en dat is 0, niet onbekend. Alleen een tv zonder sleutel is onbekend.
- **Wordt bekeken** is geen meting maar een afspraak: aan en een app vooraan die niet in de lijst eigen apps staat. Een dashboard in de browser telt niet als kijken, anders zou het zichzelf wegduwen.
- **Dashboard weghalen** doet niets als er gekeken wordt: een ingang die om 09:00 naar 0 gaat, mag geen film afbreken. Dat is de enige plek waar de hub een bevel bewust niet uitvoert.
- **Dashboard tonen als de tv uit is** is een stap-voor-stap proces in de tick (`pending`): Wake-on-LAN elke 15 s, wachten op een verbinding, het scherm aan als het uit was, dan de URL. Na 3 minuten opgeven met een regel in de zijbalk.
- **Getest**: `test_lg.py` (client tegen een nagebootste tv over een echte websocket: koppelen, weigeren, verbinding herstellen, bevelen), `test_lghub.py` (stand, ingangen, uitgangen in de logica, schema, web-API en een rondgang met `FakeTv`) en op de echte tv: koppelen, standen, toast, scherm uit/aan, een URL openen en een app starten. Niet getest op de echte tv: `system/turnOff` en Wake-on-LAN, en hoe lang de ingebouwde browser een pagina vasthoudt.

## 47. De tv-pagina: een alleen-lezen dashboard met gekoppelde entiteiten (0.54)

- **Waarom**: het tv-blok (§46) kan een pagina openen, maar er was nog niets om te tonen. Het ontwerp volgt een voorbeeld van de gebruiker, zonder het menu (op een tv kun je niet navigeren) en zonder de verlichting. De layout is een vaste 1920x1080-pagina die met het venster meeschaalt; kleuren zijn donker en zacht (geen puur wit), omdat een LCD met backlight niet zwart kan worden zoals een OLED.
- **Eigen poort met een geheim**: HA-ingress is er niet voor een tv. `tvhub.py` luistert op `tv_port` (47530, host-netwerk) met `/tv/<geheim>/` en nog drie routes: de pagina, `display.css` en `display.js`, en `data`. Het geheim is 16 bytes (`secrets.token_urlsafe`), vergeleken met `hmac.compare_digest`, en kan vernieuwd worden. Er is geen API en geen schrijfroute op die poort. Dezelfde routes staan als `/tv/view/` achter ingress voor de voorvertoning, zonder geheim: die zit al achter de toegangscontrole.
- **Eén bronnotatie**: een bron is een HA-entiteit of een verwijzing naar een uitgang van een blok (`o:`, `w:`, `e:`, `x:`, `l:`), dezelfde als de logica gebruikt (`SOURCE_RE`). Een omvormer of airco hoeft dus niet eerst in HA te staan, en de uitgang van het tv-blok (*wordt bekeken*) kan als knop onderaan.
- **Onbekend is geen nul**: een bron die niet bestaat of `unavailable` is, geeft een streepje of "Onbekend", en een grafiek slaat het punt over. Een tegel zonder waarden en een weer-entiteit die HA niet kent, verdwijnen. Alleen een verbroken verbinding met HA zet de kop op "Geen verbinding met Home Assistant".
- **Geschiedenis**: elke minuut een ruw meetpunt in het geheugen (13 uur), bij de start aangevuld met `history/history_during_period` (gemiddeld per 10 minuten). Eenheden worden pas bij het tonen omgerekend (W naar kW), zodat het aanvullen uit HA en het eigen meten door elkaar kunnen. De HA-aanroepen (`history`, `calendar_events`, `forecast`) staan in `habridge.py` en vangen een fout of een time-out op: dan is de lijst leeg.
- **Ophaalkalender**: de plaats is altijd gereserveerd. Een `calendar.*`-entiteit levert de afspraken (`calendar.get_events`, elk half uur); de titel bepaalt de kleur met een trefwoordtabel in de pagina.
- **Het adres**: de add-on kent zijn LAN-adres (`lan_ip`, een UDP-socket die alleen de route zoekt) en gebruikt dat in het adres voor de tv, niet het adres van de browser: via de cloud van HA is dat een ander.
- **Getest**: `test_tv.py` (validatie, bronnen, eenheden, weer met nacht, tegels, knoppen, meldingen, kalender, verversing en aanvullen, de server met geheim, de poort en de web-API, en dat de pictogramnamen in de configuratie bestaan in de pagina) en de pagina en het tabblad in een browser tegen een nagebootste HA. **Niet getest**: een echte HA (de vorm van het antwoord van `history/history_during_period` en `calendar.get_events` komt uit de documentatie, de parser is tolerant), een echte kalender van een afvalophaler, en de pagina in de ingebouwde browser van de tv (oudere webOS-versies kennen niet alles van moderne CSS).

## 48. Bronnen buiten HA voor de tv-pagina, en volledig scherm (0.55)

- **Weerstation**: een JSON-adres dat de gebruiker opgeeft (zijn eigen weerstation). Een meting is `{time, value}` of een getal; een meting boven `max_age` is onbekend (een veld uit 2020 in hetzelfde antwoord mag niet als nu tonen). De temperatuur van het station gaat voor die van HA; beeld, voorspelling en voelt-aan blijven uit HA. De regenteller is een teller sinds de installatie, dus de neerslag van vandaag is een verschil: punten per 10 minuten in `/data/tv_rain.json`, het laatste punt van voor middernacht als begin, een teller die terugvalt als nieuw begin. Het antwoord heeft een time-out en een grens aan de grootte, en een fout staat in het tabblad.
- **Ophaalkalender via een ICS-link**: de website van een afvalophaler is een formulier met een adres en (bij Ivago) reCAPTCHA, en de Recycle!-service van Fost Plus vraagt zich als de eigen app aan te melden; geen van beide is iets om na te bootsen. Een ICS-link die de ophaler zelf aanbiedt, is de eerlijke route: geen adres in de add-on, geen scraping, voor elke aanbieder. De parser doet datum, UTC-tijd naar de lokale datum, regelvouwing en escapes, en slaat een geannuleerde afspraak over; geen RRULE. Antwoordt de link niet, dan blijven de laatste ophalingen staan.
- **Volledig scherm**: gemeten op de tv (Chrome 120, webOS 25): het venster is 1536x856 CSS-pixels bij schaal 1,25 en de adresbalk ligt eroverheen; `requestFullscreen` bestaat maar geeft zonder handeling `Permissions check failed`; een druk op OK op de afstandsbediening bereikt de pagina niet (geen enkele toets- of klikgebeurtenis), een klik met de aanwijzer wel (`pointerdown`, daarna volledig scherm en een venster van 864 hoog, dat minuten bleef). De pagina vraagt volledig scherm bij elke klik of toets (niet in een iframe); de hub stuurt die klik 5 s na het openen via `getPointerInputSocket` (`type:click`), alleen als de browser dan nog vooraan staat.
- **Het recht**: `CONTROL_MOUSE_AND_KEYBOARD` staat nu in het manifest. Een bestaande koppeling heeft het niet (de tv geeft rechten bij het koppelen); de client onthoudt de fout (`click_error`) en de zijbalk zegt hoe het op te lossen is.
- **Getest**: `test_tvext.py` en `test_lgfs.py` (tegen een nagebootste tv en een nagebootst weerstation over echte HTTP) en het weerstation van de gebruiker zelf. **Niet getest**: de klik via de aanwijzer-socket op de echte tv (de koppeling met het nieuwe recht werd op de tv geweigerd of niet bevestigd) en een echte ICS-link van Ivago.

