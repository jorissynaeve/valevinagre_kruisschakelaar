# Host-tests van `huisdev.h`

De echte header draait hier op een PC, met een kleine Arduino-shim, zodat het protocol uit `HUISDEV.md` getest wordt tegen wat de code werkelijk doet.

```
protocol/huisdev/tests/run.sh            # bouwen en testen (docker, image gcc:14 met Python en sanitizers)
protocol/huisdev/tests/run.sh build      # alleen bouwen
protocol/huisdev/tests/run.sh test       # alleen testen
```

Bouwen gebeurt met `-Wall -Wextra -fsanitize=address,undefined`. De driver eindigt met een foutcode bij een sanitizer-melding of een lek, en elke test controleert die exitcode.

## Onderdelen

| Bestand | Doel |
|---|---|
| `shim/` | `Arduino.h` (String zoals Arduino-ESP32, `millis` bestuurbaar), `WiFi.h`, `WiFiUdp.h` (pakketten injecteren en vastleggen), `Preferences.h` (geheugen, met het gedrag van NVS), `WebServer.h` (stub waarvan de handlers aangeroepen kunnen worden) |
| `driver.cpp` | Een poolcontroller-achtig device (alle kinds, alle schrijfvormen, danger, acties met argumenten) dat via een regelprotocol op stdin/stdout wordt aangestuurd |
| `nows.cpp` | Compileertest voor `HUISDEV_NO_WEBSERVER` (de shim geeft een `#error` als `WebServer.h` toch binnenkomt) |
| `huisdevtest.py` | Start de driver en praat ermee (`Dev`), gedeelde `Base`-klasse |
| `test_descriptor_state.py` | descriptor, `rev` (inclusief de FNV-1a-berekening), state |
| `test_http.py` | paden en methoden, claimen, `set`, `do`, foutcodes, de `WebServer`-koppeling |
| `test_discovery_registration.py` | UDP-ontdekking en aankondigingen, identiteit uit het MAC-adres, registratie, limieten, dynamische punten |
| `test_fixtures.py` | schrijft `fixtures/` |

## Fixtures

`fixtures/*.json` is de ruwe tekst zoals de echte header die stuurde (vaste klok, MAC `AA:BB:CC:A1:B2:C3`, token `0123456789abcdef0123456789abcdef`).
De add-on-tests kunnen ze hergebruiken: `descriptor.json`, `state.json`, `state_claimed.json`, `discovery.json`, `announce.json`, `set_*.json`, `do_queued.json` en een `error_<status>_<code>.json` per foutcode.
`descriptor_rev2.json` en `state_rev2.json` zijn dezelfde device nadat er een punt bij kwam, een punt verdween en een eenheid wijzigde (nieuwe `rev`).
De bestanden worden bij elke testrun overschreven. Verandert een fixture, dan veranderde het gedrag van de header.

## Regelprotocol van de driver (voor wie de driver uitbreidt)

Een regel is velden gescheiden door TAB, met de escapes `\\`, `\t`, `\n`, `\r`. Antwoord: `OK<TAB>veld...` of `ERR<TAB>tekst`.
Opdrachten: `factory`, `reboot`, `reboot_empty`, `rereg`, `handle METHOD PATH TOKEN BODY`, `ws_call` en `ws_info`, `setnum`, `setbool`, `settext`, `setunknown`,
`actionstate`, `addpoint`, `unit`, `decimals`, `option`, `range`, `remove`, `addaction`, `addarg`, `removeaction`, `rebegin`, `loop`, `millis`, `advance`, `wifi`, `mac`, `bcast`,
`udp_in`, `udp_out`, `udp_state`, `calls`, `prefs_token`, `resetclaim`, `claimed`, `id`.

## Wat de shim niet dekt

- Geen echte radio, TCP of HTTP-parser: `handle()` krijgt de body en het token direct. Hoe `WebServer` een body in `arg("plain")` zet, en dat `collectHeaders` de lijst vervangt, is in de stub nagebootst volgens de documentatie, niet gemeten op hardware.
- `String(double, decimals)` is `%.*f`; de echte Arduino-implementatie kan bij extreme waarden (groter dan 1e15) anders afronden.
- Geheugen en timing van een ESP32 (heap-fragmentatie, 1 s-antwoordeis, watchdog) zijn niet gemeten. ASAN vangt alleen programmeerfouten op de host.
- `Preferences` is een geheugenstub met het gedrag van NVS bij een ontbrekende namespace en alleen-lezen openen; flash-slijtage en NVS-vol zijn niet gedekt.
- UDP: geen echte broadcast of verlies; `parsePacket`/`read` volgen de semantiek van `WiFiUDP` (rest van een pakket wordt weggegooid).
- Alleen ESP32-gedrag; de async-serverroute (`HUISDEV_NO_WEBSERVER` plus eigen routes) is alleen op compileren getest.
