# HUISDEV: protocol voor zelfbeschrijvende ESP-apparaten

```yaml
huisdev-protocol: "1.0"      # MAJOR.MINOR, zie "Versiebeheer"
huisdev-header:   "1.0.1"    # versie van huisdev.h (referentie-implementatie)
status: voorstel             # nog niet geimplementeerd in de add-on
```

Een ESP met dit protocol meldt zich op het LAN, beschrijft zichzelf (welke gegevens en acties hij heeft) en laat de
Huisconfig-add-on daar automatisch een blok van maken. Verandert de set gegevens of acties tijdens het draaien, dan past
het blok zich aan.

Dit document is geschreven om aan Claude te geven. Het bevat alles wat nodig is om een nieuw device te maken of een
bestaand device naar een nieuwere protocolversie te brengen.

---

## 0. Zo gebruikt u dit document

### Nieuw device

Geef Claude deze map (`HUISDEV.md` + `huisdev.h`) en de bron van het device, plus deze opdracht:

> Maak (of pas aan) de sketch `<pad>` zodat hij HUISDEV volgt, protocolversie uit `HUISDEV.md`.
> Kopieer `huisdev.h` ongewijzigd naast de sketch. Bepaal de punten en acties volgens "Een device modelleren".
> Zet bovenaan de sketch de regel `// huisdev-protocol: <versie>`. Doorloop de conformiteitschecklist en toon het resultaat.

### Bestaand device upgraden

> Zoek in `<map>` alle sketches met `huisdev-protocol:` en vergelijk met de versie in `HUISDEV.md`.
> Voer per afwijkend device de stappen uit "Upgrade-gids" uit, vervang `huisdev.h` en werk de marker bij.

Zoeken zonder Claude: `grep -r "huisdev-protocol:" C:\Users\joris\Documents\Arduino --include=*.ino`.

### Het protocol binnenhalen (elke client, voor elke build)

De bron is deze map in de repo `https://github.com/jorissynaeve/valevinagre_kruisschakelaar` (tak `main`):

| Bestand | Rol |
|---|---|
| `HUISDEV.md` | deze spec |
| `huisdev.h` | referentiecode, ongewijzigd te gebruiken |
| `tools/huisdev_sync.py` | het script dat een client in zijn eigen map zet |
| `manifest.json` | actuele protocol- en headerversie, plus sha256 van bovenstaande |

Een client (sketchmap) bevat een kopie van `huisdev_sync.py` en draait die **voor elke build**:

```
python huisdev_sync.py            haalt huisdev.h en HUISDEV.md op, controleert de sha256, schrijft huisdev.lock
python huisdev_sync.py --check    alleen vergelijken (niets schrijven)
python huisdev_sync.py --source <https-url of lokale map>   andere bron, bv. een lokale kopie van de repo
```

De sync meldt of de marker `// huisdev-protocol: X.Y` in de sketch achterloopt (afsluitcode 2: volg dan de Upgrade-gids). Kan de bron niet
bereikt worden, dan blijft de lokale kopie in gebruik. `huisdev.h`, `HUISDEV.md` en `huisdev.lock` worden meegecommit zodat een build ook
offline reproduceerbaar is. Wie het protocol wijzigt, draait daarna `python tools/make_manifest.py` en commit `manifest.json` mee (anders
weigert de sync de bestanden). Een sketch die een vaste protocolversie wil, gebruikt `--ref <tag>` (bijvoorbeeld `huisdev-1.0`).

### Regels voor wie dit leest

- De spec hieronder is leidend. `huisdev.h` is een referentie. Wijkt de header af, dan is de header fout.
- Pas `huisdev.h` nooit per device aan. Ontbreekt iets, pas dan de header hier aan en verhoog zijn versie.
- Sleutels, ids en codes zijn Engels. Labels en eenheden die een gebruiker ziet zijn Nederlands.

---

## 1. Overzicht

```
add-on --UDP-broadcast "HUISDEV?1.0" (poort 47500)-->  ESP        ontdekken
add-on <--{id,type,proto,rev,port,claimed}----------   ESP
add-on --GET  /huisdev/descriptor-------------------->  ESP        wat heb je? (bij een nieuwe rev)
add-on --POST /huisdev/claim {token}----------------->  ESP        koppelen (eenmalig)
add-on --GET  /huisdev/state  (X-Huisdev-Token)------>  ESP        waarden, elke 2-5 s
add-on --POST /huisdev/set | /huisdev/do ------------>  ESP        schrijven en acties
ESP    --UDP-broadcast announce (poort 47500)------->  add-on     bij start en bij een nieuwe rev
```

Drie begrippen:

| Begrip | Betekenis |
|---|---|
| **Punt** | een gegeven, en eventueel instelbaar: temperatuur, relais, modus, alarm |
| **Actie** | een opdracht die geen waarde is: boost starten, reset, kalibreren. Met eventuele argumenten |
| **Descriptor** | de beschrijving van alle punten en acties, met een `rev` die verandert als de beschrijving verandert |

De **state** (waarden) staat los van de descriptor. Waarden veranderen continu, de descriptor zelden.

---

## 2. Identiteit

- `id`: `<type-slug>-<laatste 3 bytes MAC in hex>`, bv. `poolcontroller-a1b2c3`. Tekens `[a-z0-9-]`, maximaal 32. Stabiel,
  verandert nooit, ook niet bij een firmware-update of een naamswijziging.
- `type`: korte soortnaam van het apparaat (`poolcontroller`, `peilmeter`). Vrij, maar vast per soort.
- `name`: standaardnaam. De gebruiker hernoemt in de Huisconfig, het apparaat hoeft dat niet te weten.
- `fw`: firmwareversie van de sketch (vrije string).
- `lib`: versie van `huisdev.h` (alleen informatief, voor de upgrade-controle).

---

## 3. Ontdekking (UDP, poort 47500)

- **Vraag:** de add-on stuurt een broadcast met de tekst `HUISDEV?<proto>` (bv. `HUISDEV?1.0`) naar poort 47500.
  Een apparaat antwoordt op elke vraag die met `HUISDEV?` begint, ongeacht de versie die erachter staat.
- **Antwoord** (unicast naar het bronadres en de bronpoort van de vraag, een regel JSON):

```json
{"huisdev":"1.0","id":"poolcontroller-a1b2c3","type":"poolcontroller","name":"Zwembad","fw":"0.3.1","rev":"1f3a9c02","port":80,"claimed":false}
```

- **Aankondiging:** hetzelfde bericht als broadcast naar poort 47500, bij het verbinden met wifi, bij elke nieuwe `rev` en
  elke 5 minuten. De add-on luistert op 47500, dus een nieuw apparaat verschijnt meteen zonder dat er gevraagd is.
- Berichten die niet met `HUISDEV?` beginnen, of die geen JSON-object zijn, worden genegeerd. Er is geen authenticatie op
  ontdekking: dat is bewust, het bevat niets gevoeligs.
- `huisdev` is de protocolversie van het apparaat. De add-on beslist op basis daarvan of hij het apparaat ondersteunt.

---

## 4. HTTP-endpoints

Basispoort: `port` uit de ontdekking (standaard 80). Alle antwoorden zijn `application/json; charset=utf-8` met
`Cache-Control: no-store`. Alle verzoeken met een body zijn `application/json`. Het token gaat in de header
`X-Huisdev-Token`.

| Methode en pad | Auth | Doel |
|---|---|---|
| `GET /huisdev/descriptor` | geen | beschrijving van het apparaat |
| `GET /huisdev/state` | token als geclaimd | huidige waarden |
| `POST /huisdev/claim` | geen, alleen als niet geclaimd | token vastleggen |
| `POST /huisdev/unclaim` | token | claim intrekken |
| `POST /huisdev/set` | token | een instelbaar punt wijzigen |
| `POST /huisdev/do` | token | een actie uitvoeren |

**Foutcodes.** Protocolfouten krijgen een HTTP-status en `{"ok":false,"error":"<code>","reason":"<tekst>"}`:

| Status | `error` | Betekenis |
|---|---|---|
| 401 | `token` | token ontbreekt of klopt niet |
| 403 | `not_claimed` | schrijven vereist eerst een claim |
| 403 | `not_writable` | punt is niet instelbaar |
| 404 | `unknown_point`, `unknown_action`, `unknown_path` | |
| 405 | `method` | |
| 409 | `already_claimed`, `not_claimed` (bij unclaim), `disabled`, `confirm_required` | |
| 422 | `missing_value`, `missing_arg`, `bad_value`, `bad_option`, `range`, `step`, `bad_token` | ongeldige invoer, `reason` of het argument-id zegt welke |

Een verzoek dat het apparaat wel begreep maar inhoudelijk weigert is **geen** protocolfout: dat is HTTP 200 met
`"result":"rejected"` (zie §7).

---

## 5. Descriptor

`GET /huisdev/descriptor` (zonder token):

```json
{
  "proto": "1.0",
  "rev": "1f3a9c02",
  "device": {"id":"poolcontroller-a1b2c3","type":"poolcontroller","name":"Zwembad","fw":"0.3.1","mac":"AA:BB:CC:A1:B2:C3","lib":"1.0.0"},
  "points": [
    {"id":"ph","label":"pH","kind":"float","decimals":2,"class":"ph","state":"measurement","stale":120000},
    {"id":"mode_electro","label":"Elektrolyse","kind":"enum","writable":true,
     "options":[{"v":0,"l":"Uit"},{"v":1,"l":"Handmatig"},{"v":2,"l":"Auto"}]},
    {"id":"alarm_no_flow","label":"Alarm: geen doorstroming","kind":"bool","group":"alarmen"}
  ],
  "actions": [
    {"id":"boost","label":"Boost starten","args":[{"id":"minutes","label":"Duur","kind":"int","unit":"min","min":5,"max":240,"step":5}]},
    {"id":"reset","label":"Fabrieksinstellingen","danger":true,"confirm":"Alle instellingen gaan verloren. Doorgaan?"}
  ]
}
```

### Punt

| Veld | Type | Verplicht | Betekenis |
|---|---|---|---|
| `id` | string | ja | `[a-z][a-z0-9_]{0,23}`. **Stabiel en nooit hergebruikt voor iets anders** |
| `label` | string | ja | leesbare naam, Nederlands, max 40 tekens |
| `kind` | `bool` `int` `float` `enum` `text` | ja | waardetype |
| `unit` | string | nee | eenheid (`°C`, `mV`, `g/L`); ontbreekt = geen |
| `decimals` | int | nee | alleen `float`, aantal decimalen (standaard 1) |
| `min` `max` `step` | getal | nee | bereik en stap; bij schrijven gecontroleerd. `step` telt vanaf `min` (of 0) |
| `options` | `[{"v":int,"l":string}]` | bij `enum` | toegestane waarden met label |
| `writable` | bool | nee | instelbaar via `/huisdev/set` (standaard nee) |
| `danger` | bool | nee | gevaarlijk: niet bedraadbaar in de logica, alleen handmatig met bevestiging |
| `confirm` | string | bij `danger` | bevestigingstekst |
| `diag` | bool | nee | secundair (RSSI, uptime): standaard verborgen in de add-on |
| `group` | string | nee | groepering in het zijpaneel (`alarmen`, `instellingen`, ...) |
| `class` | string | nee | hint voor Home Assistant (`temperature`, `power`, `energy`, `voltage`, `current`, `ph`, `battery`, ...) |
| `state` | `measurement` `total_increasing` | nee | hint voor HA-statistieken (kWh-tellers: `total_increasing`) |
| `stale` | int (ms) | nee | na zoveel ms zonder update geldt de waarde als verouderd. Zet dit op ~2x het verwachte interval |

Er is **geen** bitveld-type. Een statusbyte of alarmmasker publiceert u als losse `bool`-punten met een `group`.

### Actie

| Veld | Type | Verplicht | Betekenis |
|---|---|---|---|
| `id` | string | ja | zelfde regels als een punt |
| `label` | string | ja | knoptekst |
| `args` | lijst | nee | argumenten, elk met `id`, `label`, `kind`, `unit`, `min`, `max`, `step`, `options` zoals een punt. Alle argumenten zijn verplicht. Argument-id's mogen niet `id`, `args`, `confirm` of `value` heten |
| `danger`, `confirm` | | nee | zoals bij een punt |

Of een actie nu uitvoerbaar is staat **niet** in de descriptor maar in de state (`disabled`), omdat dat vaak wisselt.

### `rev`

`rev` is een korte string (hex, max 16 tekens) die **dan en alleen dan** verandert als `points` of `actions` verandert.
`huisdev.h` berekent hem als FNV-1a-32 over de JSON-tekst van `"points":[...],"actions":[...]`. `fw`, `name` en `lib`
horen er niet bij. Een andere berekening mag, als het maar deterministisch is en hetzelfde apparaat dezelfde `rev` houdt na
een herstart zolang de punten niet wijzigen.

### Limieten

Maximaal 64 punten, 16 acties, 8 argumenten per actie. Descriptor maximaal 8 KB, state maximaal 4 KB.

---

## 6. State

`GET /huisdev/state` (token als geclaimd):

```json
{
  "proto": "1.0",
  "rev": "1f3a9c02",
  "up": 86400,
  "points": {
    "ph":   {"v": 7.2, "age": 4200},
    "temp": {"v": null},
    "alarm_no_flow": {"v": false, "age": 4200}
  },
  "actions": {"boost": {"disabled": true, "reason": "al actief"}}
}
```

- `rev`: de huidige `rev`. Verschilt hij van wat de add-on kent, dan haalt die de descriptor opnieuw op.
- `v`: de waarde, of `null` als er (nog) geen waarde is. **Een onbekende waarde is `null`, nooit `0`.** `bool` is `true`/`false`,
  `int` en `enum` zijn gehele getallen, `float` een getal met `decimals` decimalen, `text` een string.
- `age`: milliseconden sinds de waarde voor het laatst vers was. Alleen bij een bekende waarde.
- `up`: uptime in seconden.
- `actions`: alleen de **uitgeschakelde** acties. Wat er niet staat is uitvoerbaar. `reason` is een korte Nederlandse uitleg.
- Een punt dat niet in de descriptor staat, staat ook niet in de state. Een punt dat wel in de descriptor staat maar nog geen
  waarde heeft, staat er met `"v":null`.
- Een `enum`-waarde die niet in `options` staat mag niet gemeld worden. Komt de hardware met een onbekende waarde, voeg dan eerst
  een optie toe (bv. `{"v":7,"l":"Onbekend (7)"}`, de `rev` verandert dan) en meld hem daarna. De add-on toont anders het kale getal.
- Het antwoord moet binnen 1 s komen. Blokkeer de webserver niet met trage hardware (BLE, Modbus): antwoord met de laatst bekende
  waarden en laat een andere taak ze verversen.

---

## 7. Schrijven en acties

### Een punt instellen

```
POST /huisdev/set
{"id":"ph_set","value":7.4}
{"id":"relay","value":true}
{"id":"mode_electro","value":2}
{"id":"pool_name","value":"Zwembad"}
```

Het apparaat controleert type, `options`, `min`, `max` en `step`, en geeft bij een fout een 422 (zie §4). Gevaarlijke punten
vereisen daarnaast `"confirm":true` in het verzoek, anders 409 `confirm_required`.

### Een actie uitvoeren

```
POST /huisdev/do
{"id":"boost","args":{"minutes":30}}
{"id":"reset","confirm":true}
```

Een uitgeschakelde actie geeft 409 `disabled` met de `reason`.

### Antwoord

```json
{"ok": true,  "result": "applied",  "reason": ""}
{"ok": true,  "result": "queued",   "reason": ""}
{"ok": false, "result": "rejected", "reason": "pomp draait niet"}
```

| `result` | Betekenis |
|---|---|
| `applied` | gedaan. De nieuwe waarde staat al in de state |
| `queued` | het apparaat gaat het doen en meldt het later in de state (BLE-schrijven, trage hardware) |
| `rejected` | het apparaat weigert, `reason` zegt waarom |

**De waarheid is de state, niet het antwoord.** De add-on toont een nieuwe waarde pas als de state hem meldt.

### Regels voor veilig schrijven

- Opdrachten zijn **absoluut** (`true`/`false`, een waarde), nooit toggle. Een herhaald verzoek moet hetzelfde resultaat geven.
- Zet `danger` op alles wat niet terug te draaien is of echte schade kan doen: reset, fabrieksinstellingen, kalibratie,
  firmware, wissen. De add-on laat zo'n punt of actie nooit aan de logica koppelen.
- Schrijven zonder geldige claim en token is altijd verboden. Een apparaat zonder claim aanvaardt geen enkel `set` of `do`.
- Maak een punt pas `writable` als het schrijven op de hardware bewezen werkt. Een punt dat u nog niet getest hebt, blijft alleen lezen.

---

## 8. Claimen en beveiliging

- Een nieuw apparaat is **niet geclaimd**: descriptor en state zijn open (zodat u kunt zien wat het is), schrijven kan niet.
- De add-on claimt het apparaat op aanvraag van de gebruiker: `POST /huisdev/claim` met `{"token":"<16-128 tekens>"}`.
  Het apparaat bewaart het token in niet-vluchtig geheugen (`Preferences`, namespace `huisdev`, sleutel `token`).
- Vanaf dan vereisen `state`, `set` en `do` de header `X-Huisdev-Token`. De descriptor blijft open.
- Het token vergelijken in constante tijd.
- `POST /huisdev/unclaim` met het token trekt de claim in. **Elk apparaat moet daarnaast een lokale manier hebben om de claim
  te wissen zonder token** (knop lang indrukken, seriele opdracht, of de eigen webpagina). Zonder dat is een apparaat na
  een verloren add-on onbruikbaar. In `huisdev.h`: `dev.resetClaim()`.
- Het verkeer is gewoon HTTP op het LAN (geen TLS). Het token beschermt tegen vergissingen en nieuwsgierige apparaten, niet
  tegen iemand die het LAN afluistert. Gebruik het token dus niet voor iets anders.
- Wifi-wachtwoorden, API-sleutels en dergelijke staan **nooit** in een punt, de descriptor of een voorbeeld.

---

## 9. Dynamische punten

Een apparaat mag tijdens het draaien punten en acties toevoegen, verwijderen of van eigenschappen veranderen (eenheid, bereik,
opties, label). Bijvoorbeeld omdat een sensor wordt aangesloten, een model andere velden heeft, of het apparaat van modus wisselt.

Regels voor het apparaat:

1. Na een wijziging verandert de `rev`. `huisdev.h` doet dat vanzelf en kondigt de nieuwe `rev` aan.
2. **Het `kind` van een id verandert nooit.** Heeft u een ander type nodig, gebruik dan een nieuw id en laat het oude weg.
3. **Een id wordt niet hergebruikt voor iets anders.** Het id is wat de logica, het schema en Home Assistant onthouden.
4. Een punt dat tijdelijk geen waarde heeft blijft in de descriptor en meldt `null`. Haal een punt alleen uit de descriptor
   als het apparaat het echt niet meer kan leveren (sensor losgekoppeld, ander model).
5. Een wijziging van eenheid (bv. °C naar °F) is een descriptorwijziging, geen state-wijziging. Het id blijft gelijk.
6. Wissel niet snel heen en weer. Elke wijziging is een nieuwe `rev` en een nieuwe aankondiging op het LAN.
7. Heeft een grootheid twee mogelijke bronnen (bv. pH standaard of premium), publiceer dan **een** punt en kies de beste bron
   in de firmware. Publiceer niet beide.

Wat de add-on ermee doet staat in §13.

---

## 10. Een device modelleren

Vaste volgorde voor Claude:

1. **Inventariseer** alles wat het apparaat weet en kan: metingen, standen, instellingen, alarmen, opdrachten.
2. **Kies per ding** punt of actie. Een waarde die blijft staan is een punt. Een opdracht zonder blijvende waarde is een actie.
3. **Kies het kind.** Aan/uit is `bool`. Een keuze uit een lijst is `enum` met `options`. Een vrij getal is `int` of `float`.
   Bitvelden worden losse `bool`-punten. Een vrije tekst is `text`; gebruik dat spaarzaam.
4. **Zet eenheden en bereiken** zoals ze op dat moment gelden. Een eenheid die kan wisselen hoort in een dynamisch punt (§9).
5. **Bepaal wat `danger` is** (§7) en wat voorlopig alleen lezen blijft.
6. **Kies ids** in snake_case, kort en stabiel: `ph`, `cell_voltage`, `alarm_no_flow`. Niet het label of een volgnummer.
7. **Zet `stale`** op de waarden die niet continu binnenkomen (de poolcontroller krijgt zijn volledige set ongeveer 1x per minuut).
8. **Zet `diag`** op RSSI, uptime, firmwareversie en andere diagnose.

Wat dit protocol in 1.0 **niet** dekt (zie ook §14): lijsten of tijdreeksen (historie), korte gebeurtenissen die tussen twee
polls kunnen verdwijnen, en push vanuit het apparaat. Los zulke dingen in 1.0 op met een gewoon punt (bv. `last_event` als `text`,
`event_count` als `int`) en meld in de code dat het een omweg is.

### Voorbeeld: poolcontroller

Uit `C:\Users\joris\Documents\Arduino\poolcontroller` (gegevens uit zijn `PROTOCOL.md`; schrijven is daar nog niet op hardware bevestigd):

| id | label | kind | eenheid | schrijven | opmerking |
|---|---|---|---|---|---|
| `ph` | pH | float, 2 dec | | nee | premium (tag 2), anders standaard (tag 1) |
| `orp` | Redox | int | mV | nee | premium (tag 6), tag 5 niet bruikbaar op dit model |
| `temp` | Watertemperatuur | float, 1 dec | °C of °F | nee | eenheid hangt van de controllerconfiguratie af: dynamisch |
| `salt` | Zout | float, 1 dec | g/L of ppm | nee | idem |
| `cell_voltage`, `cell_current` | Celspanning, celstroom | float | V, A | nee | |
| `production` | Productie | int | | nee | eenheid onbekend, laat leeg tot bevestigd |
| `ph_set`, `orp_set`, `prod_set` | Setpoints | float / int | | **nee** tot bevestigd | daarna `writable` met `min`, `max`, `step` uit de app-UI |
| `mode_electro`, `mode_ph` | Regelmodus | enum | | nee tot bevestigd | opties uit zijn enumeraties |
| `boost_remaining` | Boost resterend | int | min | nee | |
| `alarm_*`, `status_*` | alarmen en statusbits | bool | | nee | `group: alarmen` of `status`; bitvolgorde is nog onbevestigd |
| `ble_connected` | Verbonden met controller | bool | | nee | zonder BLE zijn alle metingen `null` |
| `rssi` | WiFi-signaal | int | dBm | nee | `diag` |
| actie `boost` | Boost starten | | | | pas toevoegen als het schrijven bevestigd is |
| actie `reset_device`, `restore_params` | | | | | altijd `danger` |

---

## 11. Conformiteitschecklist

Een device is conform als alles hieronder klopt. Claude toont dit lijstje ingevuld na elke generatie of upgrade.

- [ ] Bovenaan de sketch staat `// huisdev-protocol: <versie>` en die versie staat in `HUISDEV.md`
- [ ] `huisdev.h` is ongewijzigd gekopieerd (zelfde `HUISDEV_HEADER_VERSION` als in deze map)
- [ ] `dev.begin()` wordt na het opzetten van wifi/mac aangeroepen en `dev.loop()` draait in `loop()`
- [ ] Elk punt heeft een stabiel snake_case-id, een Nederlands label en het juiste `kind`
- [ ] Onbekend is `null` (`setUnknown`), nergens 0 als plaatsvervanger
- [ ] Enum-punten hebben `options`. Getallen met een bekend bereik hebben `min`/`max`/`step`
- [ ] Alleen punten die op hardware bewezen werken zijn `writable`; onomkeerbare dingen zijn `danger`
- [ ] Er is een lokale manier om de claim te wissen (`resetClaim`)
- [ ] Geen wachtwoord, sleutel of token in een punt, label of voorbeeld
- [ ] `/huisdev/state` antwoordt binnen 1 s, ook als de hardware traag is
- [ ] Een punt dat tijdens het draaien verschijnt of verdwijnt wijzigt de `rev` (getest: voeg een punt toe, `rev` verandert)

Controle met `curl` (vervang het adres):

```
curl http://<ip>/huisdev/descriptor
curl http://<ip>/huisdev/state
curl -X POST http://<ip>/huisdev/claim -H "Content-Type: application/json" -d "{\"token\":\"0123456789abcdef0123456789abcdef\"}"
curl http://<ip>/huisdev/state -H "X-Huisdev-Token: 0123456789abcdef0123456789abcdef"
curl -X POST http://<ip>/huisdev/set -H "X-Huisdev-Token: 0123456789abcdef0123456789abcdef" -H "Content-Type: application/json" -d "{\"id\":\"relay\",\"value\":true}"
curl -X POST http://<ip>/huisdev/unclaim -H "X-Huisdev-Token: 0123456789abcdef0123456789abcdef"
```

Ontdekking testen (Python, op dezelfde laptop als het apparaat):

```python
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
s.settimeout(3)
s.sendto(b"HUISDEV?1.0", ("255.255.255.255", 47500))
print(s.recvfrom(1024))
```

---

## 12. Async server

`huisdev.h` bindt standaard aan de blokkerende `WebServer` (`dev.begin(type, name, fw, &server)`). Gebruikt een sketch
`ESPAsyncWebServer` (dat botst met `WebServer.h`), zet dan voor de include:

```cpp
#define HUISDEV_NO_WEBSERVER
#include "huisdev.h"
...
dev.begin("type", "naam", "1.0.0");   // zonder server
```

en roep `dev.handle(method, path, body, token, out)` aan vanuit de eigen routes. Dat geeft de HTTP-status (0 = niet
van HUISDEV) en de JSON in `out`. De body van een POST moet u in de async-handler zelf verzamelen. **Deze route is niet getest
op hardware**, alleen dat de header compileert met `HUISDEV_NO_WEBSERVER`.

---

## 13. Wat de add-on ermee doet (informatief, nog te bouwen)

Dit is de bedoeling voor de add-on. Het hoort bij de implementatie, niet bij het protocol dat een device moet volgen.

- **Ontdekken:** luistert op UDP 47500 en stuurt elke 60 s `HUISDEV?1.0` zolang er een apparaat zonder IP, offline of niet geclaimd is.
  Gevonden apparaten staan als "niet geclaimd" in een lijst, net als Solarman-sticks.
- **Claimen:** op een klik genereert de add-on een token (32 bytes hex), stuurt het naar het apparaat en bewaart het in een
  eigen bestand in `/data` (modus 0600, buiten de modelexport).
- **Blok:** een blok `e:<id>` met een **uitgang** per punt en een **ingang** per `writable` punt dat niet `danger` is. Ingangen
  volgen het Gree-patroon: alleen bij een wijziging van de ingang, de eerste waarde onthouden, wachttijd na een mislukking, altijd absoluut.
  `diag`-punten staan standaard niet op het blok.
- **Home Assistant:** per punt een entiteit, gekozen op `kind` en `writable` zoals bij Tuya (`switch`, `binary_sensor`, `select`,
  `number`, `sensor`), met `unit`, `class` en `state` als hints. Het `unique_id` komt van `<id>` en het punt-id, nooit van een volgnummer.
- **Ids:** het apparaat-id gaat over de draad met streepjes (`pool-a1b2c3`). In de add-on staat het met een laagstreepje
  (`pool_a1b2c3`), omdat verwijzingen in de logica alleen `[a-z0-9_]` toestaan. Punt-id's veranderen niet.
- **Poll:** `GET /huisdev/state` elke 2-5 s. Drie mislukkingen op rij is offline en alle waarden zijn dan onbekend. Een afwijkende
  `rev` haalt de descriptor opnieuw op.
- **Wijziging van de descriptor:** nieuw punt wordt een nieuwe poort. Een verdwenen punt wordt "niet beschikbaar": de verwijzing in
  de logica blijft staan en geeft onbekend, de HA-entiteit wordt `unavailable` en pas na 30 dagen verwijderd (de add-on bewaart
  daarvoor de lijst van gepubliceerde punten, want opruimen in het geheugen overleeft een herstart niet). Een gewijzigde eenheid
  wordt overgenomen. Een gewijzigd `kind` van een bestaand id wordt als fout gelogd en als verwijderd plus nieuw behandeld.
- **Versie:** zelfde MAJOR wordt ondersteund, een nieuwere MINOR van het apparaat wordt gewoon gelezen (onbekende velden en onbekende
  `kind`-waarden worden overgeslagen). Een apparaat met een lagere MINOR dan de add-on kent, krijgt in de Nodes-tab "upgrade beschikbaar".
  Een onbekende MAJOR wordt als "niet ondersteund" getoond.

---

## 14. Bewust niet in 1.0

Mogelijke vervolgen, elk als aparte minor- of major-stap (zie "Versiebeheer"):

| Idee | Waarom niet nu |
|---|---|
| Lijsten en tijdreeksen (peilmeter-historie, relais-historie) | past niet in puntwaarden. Aparte `list`-kind met pagina's |
| Gebeurtenissen (knopdruk, alarm tussen twee polls) | polling mist korte pulsen. Aparte `events`-endpoint met teller |
| Push (SSE of WebSocket) | de blokkerende `WebServer` kan het niet goedkoop |
| MQTT als alternatief transport | geen enkele sketch heeft een MQTT-bibliotheek |
| TLS en echte authenticatie | geheugen en certificaatbeheer; het token volstaat voor het LAN |
| Bitvelden als een eigen type | losse `bool`-punten zijn voor de add-on en de logica eenvoudiger |
| Argumenten die optioneel zijn | in 1.0 zijn alle argumenten verplicht |

---

## 15. Versiebeheer

### Versienummers

- **Protocol:** `MAJOR.MINOR` (bovenaan dit document, in `HUISDEV_PROTO`, in de ontdekking en in descriptor en state).
- **Header:** `MAJOR.MINOR.PATCH` (`HUISDEV_HEADER_VERSION`). Een bugfix in de header is een patch zonder protocolwijziging.
- **Marker in elke sketch:** `// huisdev-protocol: X.Y` bovenaan. Daarmee vindt `grep` direct welke devices achterlopen.

### Compatibiliteitsregels

| Wijziging | Versie | Eis |
|---|---|---|
| nieuw optioneel veld, nieuw `kind`, nieuw endpoint, nieuw optioneel state-veld | **MINOR** | oude apparaten blijven werken, oude add-ons negeren wat ze niet kennen |
| verplicht veld erbij, veld verwijderd of andere betekenis, ander pad, andere foutcode | **MAJOR** | de add-on ondersteunt de huidige en de vorige MAJOR; apparaten moeten upgraden |

Beide kanten **negeren onbekende velden** en een punt met een onbekend `kind` wordt overgeslagen, niet als fout behandeld. Een MINOR mag dus nooit
een bestaand veld anders interpreteren.

### Wijzigingslogboek

| Versie | Datum | Wijziging |
|---|---|---|
| 1.0 | 2026-11-12 | eerste versie |

### Wijzigingslogboek van de header (`huisdev.h`)

De header heeft eigen versienummers. Een headerversie zonder protocolwijziging hoort hier, niet hierboven.
Een device hoeft pas te upgraden als een header-bugfix het device raakt; vervang dan alleen `huisdev.h`.

| Header | Protocol | Wijziging |
|---|---|---|
| 1.0.0 | 1.0 | eerste versie |
| 1.0.1 | 1.0 | Bugfixes, gevonden door de host-tests in `tests/` (zie `tests/README.md`). Zie de lijst direct hieronder. |

Header 1.0.1 repareert:

1. `GET` op een onbekend pad gaf 405 in plaats van 404 `unknown_path`.
2. `PUT`, `DELETE` en `HEAD` op `/huisdev/set`, `do`, `claim` en `unclaim` werden uitgevoerd alsof het `POST` was. Nu 405 `method`.
3. De JSON-lezer zocht sleutels als losse tekst. Een waarde `"id"` of `"value"`, of een punt met id `value`, misleidde hem. Hij zoekt nu echte sleutels.
4. `\uXXXX`, `\r`, `\b`, `\f` en surrogaatparen in tekstwaarden en tokens werden niet gedecodeerd. `json.dumps` stuurt standaard `°` voor `°`, dus een token of tekst met niet-ASCII-tekens werd verminkt.
5. `true` en `false` werden als getal 1 en 0 aanvaard voor `int`, `float` en `enum`. `1e400`, `0x10`, `nan` en `inf` kwamen door als getal. Nu `422 bad_value`.
6. `setNum` met oneindig meldde `inf` (ongeldige JSON). Nu onbekend (`null`).
7. Een tekst-argument van een actie werd niet op type gecontroleerd (`{"note":5}` gaf `""`).
8. `option()` met een bestaande waarde voegde een dubbele optie toe, waardoor dezelfde registratie opnieuw uitvoeren de `rev` veranderde.
9. Meer dan 8 argumenten per actie werden niet geweigerd.
10. Een `id` kon langer dan 32 tekens worden.
11. `min`, `max` en `step` werden op 3 decimalen afgerond (0,0005 werd 0,001). Nu 6.
12. Een actie zonder functie crashte bij het uitvoeren. Nu `rejected`.
13. Bij meer dan 64 punten of 16 acties groeide het overloop-object bij elke aanroep. Het wordt nu bij elke aanroep leeggemaakt (preventief, niet door een test gevonden).

### Upgrade-gids

Per versie staat hier wat een bestaand device moet doen. Elke stap is zo klein dat Claude hem mechanisch kan uitvoeren.
Een sprong over meerdere versies voert alle tussenliggende stappen in volgorde uit.

**Algemeen, elke upgrade:**
1. Vervang `huisdev.h` door de versie uit deze map (niet bewerken, hele bestand).
2. Zet de marker bovenaan de sketch op de nieuwe protocolversie.
3. Compileer. Compileerfouten komen meestal door een gewijzigde API in `huisdev.h`; de stappen per versie benoemen die.
4. Doorloop de conformiteitschecklist (§11).

**1.0:** eerste versie, geen upgrade nodig.

**Sjabloon voor een volgende versie** (kopieer, vul in):

```
**X.Y** (van X.(Y-1)):
- Wat is nieuw: ...
- Moet de sketch iets aanpassen? ja/nee. Zo ja: <oude aanroep> wordt <nieuwe aanroep>.
- Nieuwe punten of velden die een bestaand device kan gebruiken: ...
- Breekt dit iets bij een oudere add-on? ...
```
