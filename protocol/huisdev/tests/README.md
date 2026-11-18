# Host-tests van `huisdev.h`

De echte header draait hier op een PC, met een kleine Arduino-shim, zodat het protocol uit `HUISDEV.md` getest wordt tegen wat de code werkelijk doet.

```
protocol/huisdev/tests/run.sh            # bouwen en testen (docker, image gcc:14 met Python en sanitizers)
protocol/huisdev/tests/run.sh build      # alleen bouwen
protocol/huisdev/tests/run.sh test       # alleen testen
```

Bouwen gebeurt met `-Wall -Wextra -fsanitize=address,undefined`, plus een compilatie met `-Os -Wall -Wextra -Werror=format-truncation` (de waarschuwingen die de sketch-compiler bij optimalisatie ziet). De driver eindigt met een foutcode bij een sanitizer-melding of een lek, en elke test controleert die exitcode.

## Onderdelen

| Bestand | Doel |
|---|---|
| `shim/` | `Arduino.h` (String zoals Arduino-ESP32, `millis` bestuurbaar), `WiFi.h`, `WiFiUdp.h` (pakketten injecteren en vastleggen), `Preferences.h` (geheugen, met het gedrag van NVS: ook `putBytes`, `getBytes`, `getBytesLength`, `isKey`, een quota op het totaal aantal bytes om "NVS vol" te testen en een teller van schrijfacties), `WebServer.h` (stub waarvan de handlers aangeroepen kunnen worden) |
| `driver.cpp` | Een poolcontroller-achtig device (alle kinds, alle schrijfvormen, danger, acties met argumenten) dat via een regelprotocol op stdin/stdout wordt aangestuurd. Scènes zijn standaard uit (`scenes` zet ze aan); de klok is bestuurbaar via `HUISDEV_NOW()` (`settime`) |
| `nows.cpp` | Compileertest voor `HUISDEV_NO_WEBSERVER` (de shim geeft een `#error` als `WebServer.h` toch binnenkomt), ook met `dev.scenes(...)` en de scene-routes. Laat `HUISDEV_NOW` ongedefinieerd, zodat het standaard `time(nullptr)` van de header compileert |
| `huisdevtest.py` | Start de driver en praat ermee (`Dev`), gedeelde `Base`-klasse |
| `test_descriptor_state.py` | descriptor, `rev` (inclusief de FNV-1a-berekening), state |
| `test_http.py` | paden en methoden, claimen, `set`, `do`, foutcodes, de `WebServer`-koppeling |
| `test_scenes.py` | scènes (1.1, §16): een device zonder scènes is ongewijzigd (rev, descriptor, state, paden), descriptor en `rev` met scènes, state (`have`, `run`, `sched`, `time`), `store`, `play`, `stop`, `schedule`, base64-randgevallen, body-limiet (ook 64 KB onder ASAN), NVS vol, schrijfinterval, idempotentie, herstart, geplande starts in `loop()` |
| `test_seek.py` | `scene/seek` (1.2, §16): descriptor met en zonder `seek` (de `rev` van een device zonder callback is gelijk aan die van 1.1.0), de drie bodyvormen (`slot`, `data`, alleen `t`), `t`-randen, foutcodes en volgorde van de controles, `run.seek` in de state en het wissen ervan (`play`, `stop`, `sceneIdle`, `sceneRunning`, geplande start), body-limiet (ook 64 KB onder ASAN), geen NVS-schrijfacties en `sched` ongemoeid |
| `test_discovery_registration.py` | UDP-ontdekking en aankondigingen, identiteit uit het MAC-adres, registratie, limieten, dynamische punten |
| `test_fixtures.py` | schrijft `fixtures/` |

## Fixtures

`fixtures/*.json` is de ruwe tekst zoals de echte header die stuurde (vaste klok, MAC `AA:BB:CC:A1:B2:C3`, token `0123456789abcdef0123456789abcdef`).
De add-on-tests kunnen ze hergebruiken: `descriptor.json`, `state.json`, `state_claimed.json`, `discovery.json`, `announce.json`, `set_*.json`, `do_queued.json` en een `error_<status>_<code>.json` per foutcode.
`descriptor_rev2.json` en `state_rev2.json` zijn dezelfde device nadat er een punt bij kwam, een punt verdween en een eenheid wijzigde (nieuwe `rev`).
Scènes (1.1): `descriptor_scenes.json` en `state_scenes.json` (twee gevulde slots, een lopende scène, twee geplande starts, klok 1760000123), `scene_store_applied.json`, `scene_play_rejected_notime.json` (een device weigert een scène die de klok nodig heeft zolang de klok ongeldig is), `error_422_bad_slot.json`, `error_422_too_large.json` en `error_422_bad_data.json`.
Seek (1.2): `descriptor_seek.json` (het scenes-device met `"seek":true`), `state_seek.json` (met `run.seek`), `scene_seek_applied.json` en `scene_seek_rejected_noscene.json` (`{"t"}` zonder seek-beeld).
De niet-scène-fixtures zijn bij de stap naar 1.1 en naar 1.2 alleen veranderd waar `proto` (`"1.2"` in descriptor, state en ontdekking) en `lib` (`"1.2.0"`) staan; de `rev` (`f64cf2a6`, `211dec90`, en `2e610df9` voor het scenes-device zonder seek) is gelijk gebleven, en `test_scenes.py` en `test_seek.py` bewaken dat.
De bestanden worden bij elke testrun overschreven. Verandert een fixture, dan veranderde het gedrag van de header.

## Regelprotocol van de driver (voor wie de driver uitbreidt)

Een regel is velden gescheiden door TAB, met de escapes `\\`, `\t`, `\n`, `\r`. Antwoord: `OK<TAB>veld...` of `ERR<TAB>tekst`.
Scènes: `scenes SLOTS MAX_BYTES SCHED [FORMAT...]` (zet `dev.scenes` en de drie callbacks aan, ook na `reboot`; formats `-` = geen), `scene_cfg check REDEN | play RESULT REDEN | stop RESULT REDEN | seek RESULT REDEN | seekhook idle|running|-` (wat de callbacks antwoorden; `seekhook`: het device meldt `sceneIdle()` of `sceneRunning()` tijdens de seek-aanroep), `seek_on` en `seek_off` (`dev.onSceneSeek` zetten of leegmaken; standaard uit, ook bij `scenes`, en onafhankelijk van `scenes`), `scene_log` (callbacks sinds de vorige aanroep: `check|n|hash`, `play|slot|n|elapsed|hash`, `stop|slot`, `seek|slot|n|t|hash` met `-` als hash bij slot -1), `scene_running SLOT HASHHEX UNTIL`, `scene_idle`, `scene_read SLOT [CAP]`, `settime EPOCH` (0 = geen geldige klok), `nvs_quota BYTES` (-1 = onbeperkt), `nvs_writes` (teller van schrijfacties, vergelijk twee standen), `prefs_get NS KEY` (ruwe bytes als hex, `-` = ontbreekt).
Opdrachten: `factory`, `reboot`, `reboot_empty`, `rereg`, `handle METHOD PATH TOKEN BODY`, `ws_call` en `ws_info`, `ws_collect KEY...` en `ws_collect_all` (zoals een sketch die zelf `collectHeaders` of `collectAllHeaders` aanroept), `setnum`, `setbool`, `settext`, `setunknown`,
`actionstate`, `addpoint`, `unit`, `decimals`, `option`, `range`, `remove`, `addaction`, `addarg`, `removeaction`, `rebegin`, `loop`, `millis`, `advance`, `wifi`, `mac`, `bcast`,
`udp_in`, `udp_out`, `udp_state`, `calls`, `prefs_token`, `resetclaim`, `claimed`, `id`.

## Wat de shim niet dekt

- Geen echte radio, TCP of HTTP-parser: `handle()` krijgt de body en het token direct. Hoe `WebServer` een body in `arg("plain")` zet, en dat `collectHeaders` de lijst vervangt (elke aanroep begint opnieuw; `collectAllHeaders` verzamelt alles), is in de stub nagebootst volgens de bron van Arduino-ESP32 core 3.3.7, niet gemeten op hardware.
- `String(double, decimals)` is `%.*f`; de echte Arduino-implementatie kan bij extreme waarden (groter dan 1e15) anders afronden.
- Geheugen en timing van een ESP32 (heap-fragmentatie, 1 s-antwoordeis, watchdog) zijn niet gemeten. ASAN vangt alleen programmeerfouten op de host.
- `Preferences` is een geheugenstub met het gedrag van NVS bij een ontbrekende namespace en alleen-lezen openen. "NVS vol" is een quota op het totaal aantal bytes, niet de paginaindeling van echte NVS (de echte kan vol raken bij minder bytes, en een blob mag maximaal ~4000 bytes zijn); flash-slijtage is niet gedekt, alleen het aantal schrijfacties wordt geteld.
- De klok is een getal dat de test zet. Het echte `time(nullptr)` (SNTP, sprong van 1970 naar nu) is alleen op compileren getest (`nows.cpp`).
- De scenes-callbacks zijn testdubbels die loggen. Dat een echt device de bytes valideert en afspeelt, en dat `sceneRunning`/`sceneIdle` bij een natuurlijk einde worden aangeroepen, kan alleen het device zelf testen.
- UDP: geen echte broadcast of verlies; `parsePacket`/`read` volgen de semantiek van `WiFiUDP` (rest van een pakket wordt weggegooid).
- Alleen ESP32-gedrag; de async-serverroute (`HUISDEV_NO_WEBSERVER` plus eigen routes) is alleen op compileren getest.
