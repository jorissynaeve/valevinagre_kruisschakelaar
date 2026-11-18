# HUISDEV-scènes in de Huisconfig-add-on (protocol 1.1)

Dit is de add-on-kant van `protocol/huisdev/HUISDEV.md` §16 en `protocol/ledscene/LEDSCENE.md`: de bibliotheek, de slots op een apparaat, de ingangen
die een scène starten en stoppen, en de geplande starts waarmee een wekker ook zonder verbinding afgaat. Dit bestand is een tijdelijke plek: bij het
samenvoegen gaat de tekst naar een hoofdstuk in `DESIGN.md` en `DOCS.md` (en de versie in `config.yaml` en `__init__.py` wordt dan opgehoogd).

## Bestanden

| Bestand | Rol |
|---|---|
| `addon/kkaddon/scenehub.py` | alle logica: bibliotheek, selectie en slots, flanken en verzender, sync, geplande starts, view voor de pagina |
| `addon/kkaddon/scenesweb.py` | de routes (`/api/scenes...`, `/api/espdev/<id>/scenes`, `.../scene/play`, `.../scene/stop`) |
| `addon/kkaddon/static/scenes.js` | de sectie "Scènes" in de zijbalk van een apparaat, het formulier en de voorbeeldstrip (geladen na `schema.js`) |
| `addon/kkaddon/scenes.py` | ledscene/1 (ontleden, bouwen, renderen, `compile_def`, `preview`): bestond al, ongewijzigd |
| `addon/tests/fakehuisdev.py` | het nagebootste apparaat kent nu scènes (`scenes(...)`, `kidswekker()`) |
| `addon/tests/test_scenehub.py` | de tests |

## Gegevens

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

## Protocol 1.1 in `espdevhub.py`

* `CURRENT = (1, 1)`. "Upgrade beschikbaar" (`proto_state`) geldt alleen voor `driver == "huisdev"`: Hue, Nuki, Brink, Smappee, Reolink en ESPHome zeggen vast 1.0.
* `clean_descriptor` bewaart een geldig `scenes`-object (formats = lijst strings, 1 <= slots <= 8, 1 <= max_bytes <= 1024, slots * max_bytes <= 6144,
  0 <= sched <= 8); ongeldig is weglaten, nooit falen. De foutcodes `bad_slot`, `too_large` en `bad_data` staan in `ERRORS`.
* `_on_state` leest `state.scenes`. `time` en `run.until` tellen niet als wijziging voor de pagina (anders ververst de pagina elke 3 s).

## API

| Route | Body en antwoord |
|---|---|
| `GET /api/scenes` | `[{id, name, def, release, hash, bytes, bounded, total_ms}]` |
| `POST /api/scenes` | `{name, def, release?}` geeft `{ok, id}` |
| `PUT /api/scenes/<id>` | `{name?, def?, release?}` |
| `DELETE /api/scenes/<id>` | ruimt overal op (selectie, bedrading, slot, wat nog wacht); 404 als hij niet bestaat |
| `POST /api/scenes/preview` | `{def, n?, samples?}` geeft `scenes.preview` (`n` leds, `frames` per tijdstip, kleuren 0..255, `times`, `bounded`, `total_ms`, `window_ms`) |
| `PUT /api/espdev/<id>/scenes` | `{scenes: [ids]}`: welke scènes op dit apparaat staan (een rij en een slot elk) |
| `POST /api/espdev/<id>/scene/play` | `{id}` (bibliotheekscène) of `{slot}`: nu spelen; geeft `{ok, message}` |
| `POST /api/espdev/<id>/scene/stop` | `{}` (alles), `{slot}` of `{id}` (alleen als die scène nog speelt) |

Fouten zijn `400 {"error": "<Nederlandse tekst>"}` zoals overal. `GET /api/state` bevat per apparaat `scn`: `{cap, known, sel: [{id, name, slot, release, state, src, note,
sched_note}], have, run: {slot, h, until, id, name}, sched: [{slot, at, id, name}], time, note}`; `state` is `ok`, `wait`, `noslot`, `toolarge` of `broken`.

## Slots

Een scène krijgt bij het selecteren het laagste vrije slot (1..`slots`) en houdt het zolang ze geselecteerd is; een slot wordt nooit aan een andere scène
gegeven terwijl de eerste er nog staat. Zijn er te veel scènes, dan blijft de rij staan met de melding "geen vrij slot" in `last["scene:<id>"]` tot er een
slot vrijkomt.

## De ingang van een scène (`tick()`, een eigen tak)

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

## Sync (na elke poll, `EspDevHub._run`)

Vergelijkt `state.scenes.have` met de bibliotheek voor elke geselecteerde scène met een slot. Alleen bij een afwijking, en met bewaking:

* hoogstens **een schrijfactie per ronde** (een `store`, een wissen of een `schedule`);
* dezelfde (slot, hash) hoogstens **eens per 10 minuten**, hoogstens **6 schrijfacties per uur** per apparaat (en 6 `schedule`-acties per uur);
* de teruggegeven `hash` van een `store` wordt gecontroleerd; wijkt hij af, dan een melding en pas na 10 minuten opnieuw;
* een slot dat gevuld is maar aan geen geselecteerde scène hoort, wordt gewist (`data: ""`): ook wat er met de hand of door een oude add-on in kwam;
* een leeg of teruggezet apparaat wordt opnieuw gevuld, een slot per ronde (en dus binnen de bewaking: na een reset kan het een poos duren);
* `ledscene/1` niet in `formats`: geen enkele bewerking, een melding in `last["scenes"]`; een scène groter dan `max_bytes` wordt niet gestuurd (`state: toolarge`).

## Geplande starts voor een wekker zonder verbinding

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

## Op de pagina

Sectie "Scènes" in de zijbalk van een apparaat met `scenes`: de bibliotheek met een vinkje "op dit apparaat", ▶ en ■, een formulier (naam, soort, parameters, stoppen of laten uitlopen),
een voorbeeldstrip (de server rekent, `scenes.js` tekent de kleuren op een `<canvas>` en toont ze als afbeelding; de tijd loopt naar beneden), de status ("speelt: ..., tot HH:MM") en de lijst
"gepland op het apparaat" in lokale tijd. De ingang staat bij "Ingangen" met de andere.

## Hooks in bestaande bestanden (voor het samenvoegen)

| Bestand | Plaats | Wat |
|---|---|---|
| `espdevhub.py` | import, `CURRENT`, `ERRORS` | `from . import scenehub`; `CURRENT = (1, 1)`; drie foutcodes |
| `espdevhub.py` | `proto_state`, `proto_state_of` | `driver`-parameter: upgrade alleen voor `huisdev` |
| `espdevhub.py` | `clean_descriptor` | `**scenehub.cap_field(raw.get("scenes"))` vooraan het resultaat |
| `espdevhub.py` | `_defaults` | een regel `"scn": scenehub.clean_scn(v.get("scn"))` |
| `espdevhub.py` | `__init__` | `self.scn = scenehub.SceneHub(self)` na `self._load()` |
| `espdevhub.py` | `_inputs` | een tak voor sleutels `scene:` (controleert de bron) |
| `espdevhub.py` | `tick` | een tak voor sleutels `scene:` (`self.scn.tick_input`) |
| `espdevhub.py` | `_run` | `await self.scn.sync(did)` na `_on_state` |
| `espdevhub.py` | `_on_state` | `changed = self.scn.on_state(...) or changed` |
| `espdevhub.py` | `_apply_desc` | `self.scn.on_desc(did, desc)` als laatste regel |
| `espdevhub.py` | `remove` | `self.scn.drop_device(did)` |
| `espdevhub.py` | `status` | `out[-1]["scn"] = self.scn.view(did, dev)` |
| `schema.py` | import, `_espdev_blocks` | `scenehub.block_ports(...)` (de rijen) en `res[-1]["rows"] = ...` na de bestaande `res.append` |
| `web.py` | import, `make_app` | `scenesweb.add_routes(r, CTX, _body)` |
| `static/schema.js` | `scEspdevPanel`, `scEspdev` | twee regels (`scnSection`, `scnHint`) en een tooltip voor `p.scene` |
| `static/index.html` | scripts | `<script src="static/scenes.js">` na `schema.js` |
| `tests/test_espdev.py` | `rig()`, `test_versions` | `CURRENT` vastzetten op (1, 0): die tests gaan over een 1.0-apparaat bij een 1.0-add-on |
| `tests/fakehuisdev.py` | | scènes en `kidswekker()`; het lezen van de body is nu `r.read()` + `json.loads` |

## Bewust

* Een apparaat zonder `scenes` verandert niet. Een 1.0-apparaat krijgt "upgrade beschikbaar" (zo staat het in HUISDEV §13); het werkt gewoon door.
* De sync wist slots die aan geen geselecteerde scène horen. Wie op een apparaat zelf scènes bewaart (bijvoorbeeld via zijn eigen pagina), verliest die zodra de add-on dat apparaat beheert.
* Een bron die geen timer is, krijgt geen geplande start. Een module met `Tussen` of `Na` is niet vooruit te berekenen zonder de hele module-boom te simuleren.
