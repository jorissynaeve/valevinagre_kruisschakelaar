# TV als huis-screensaver — overdracht naar Huisconfig

> Opgesteld 2026-11-27 uit een bespreking in deze map. Doel: in `valevinagre_kruisschakelaar` (Huisconfig) verder
> bouwen aan het **tv-gedeelte**. Een **interactief scherm** (touch, kalender-eerst) blijft **voor later in deze map (WallMount)**.
> `VOORSTEL.md` hier is deels achterhaald (gaat uit van Loxone en de Teclast T65); dit bestand is de actuele richting.

## 1. Wat we willen

- De tv toont een **alleen-lezen** dashboard als "screensaver": klok, agenda, weer, boodschappen, afval, korte huisstatus.
- **Geen bediening op de tv.** Bediening blijft via HA-app, muurschakelaars en eventueel openHASP/NSPanel.
- De tv is niet permanent aan: **HA bepaalt wanneer** het dashboard verschijnt en verdwijnt.

## 2. Uitgangspunten (de tv)

- **LG 85QNED93A6A (2025)**: QNED evo **Mini LED**, dus een **LCD met backlight** (geen OLED), webOS 25.
- Aantal dimzones niet gevonden; LG zegt dat dit per formaat/regio verschilt. Geen officieel verbruikscijfer voor de 85".
- Ter oriëntatie (ander model): 86" QNED85 = 171 W, 65" = 113 W. **Zelf meten.**

## 3. Stroom en kleur

- Op LCD bepaalt vooral **backlight-niveau en inschakeltijd** het verbruik, niet de kleur.
- Zwarte achtergrond helpt een beetje door lokaal dimmen (mini LED), maar veel minder dan op OLED.
- Kleine felle tekst op zwart kan **blooming** geven: gebruik zacht wit/grijs, geen puur wit. (Algemene kennis, niet getest op dit model.)
- Grootste besparing: de tv **niet de hele dag** laten branden.

## 4. Voorgestelde logica (HA-getriggerd)

**Aan + dashboard tonen** als alle drie waar zijn:
1. tijdvenster of trigger (bv. 06:30–09:00, of thuiskomst, of X min na beweging);
2. iemand is thuis/aanwezig;
3. de tv wordt **niet gebruikt** om te kijken (media_player-stand/bron in HA).

**Uit** (scherm uit of tv uit) zodra een voorwaarde wegvalt. Als niemand kijkt: "ambient" (zwart + klok) of uit.

Aanzet in Huisconfig-termen (idee, nog te toetsen): de bestaande modules **Timer / Tussen / En / Of** leveren een 0/1; een nieuw blok
"LG-tv" neemt die als aan/uit-ingang en stuurt HA aan. Aan de docs te zien lezen blokken al standen uit HA. Of dat ook geldt
voor `media_player` en of acties via `webostv` kunnen, is **niet nagegaan**.

## 5. HA-kant (webOS)

- Integratie **LG webOS TV** (aan/uit via Wake-on-LAN of HDMI-CEC).
- Scherm uit zonder de tv uit te zetten: `screen`-entiteit (standaard uitgeschakeld, niet op alle modellen), of
  `webostv.command` met `com.webos.service.tvpower/power/turnOffScreen` (bij een forumgebruiker werkend op een OLED48A; niet op dit model getest).
- Webpagina openen: `webostv.command` kan de browser met een URL starten; **de payload verschilt per model, testen**.
- De ingebouwde webOS-screensaver (`com.webos.app.screensaver`) kan HA starten, maar toont **niet** je dashboard; een eigen screensaver-app vereist root.
- Betrouwbaarder alternatief indien de ingebouwde browser faalt: **Chromecast/Fire TV Stick of Pi aan HDMI** (niet in deze sessie getest).
- Developer Mode-sessies vervallen; er is een [blog over levend houden via HA](https://elis.nu/blog/2022/07/keeping-webos-developer-mode-alive-with-home-assistant/).

## 6. Layouts (16:9, 4K, leesbaar op 3–4 m, alleen-lezen)

Zwarte achtergrond, zacht grijs/wit, gezinskleuren als dunne streep of stip, weinig lichte vlakken.

**1. Vandaag** (standaard)
```
┌─────────────────────────────────────────────────────────────┐
│ 07:42                          Do 27 nov          5° ☁ 8/2° │
│ VANDAAG                          │ WEER                     │
│   08:30  Tandarts    ● Lotte     │   regen vanaf 10u        │
│   17:00  Voetbal     ● Jan       │   jas: regenjas          │
│ MORGEN                           │ BOODSCHAPPEN (6)         │
│   09:00  Overleg     ● Papa      │   melk · brood · eieren  │
│                                  │ AFVAL: morgen PMD        │
└─────────────────────────────────────────────────────────────┘
```
**2. Vertrek** (ochtend, kort en groot)
```
│ 07:42                                                       │
│  JAS: regenjas          FIETSWEER: regen om 08:15           │
│  AFVAL: PMD buiten      VANDAAG: 08:30 tandarts (Lotte)     │
```
**3. Ambient** (idle/avond)
```
│                        22:42                                │
│                 morgen: 08:30 tandarts                      │
```
Optioneel: **Huisstatus** (lichten, ramen, energie) als vierde layout.
Voorstel dag: ochtend/avond layout 1 of 2, overdag 1, laat 3, 's nachts uit.

## 7. Eerste testplan

1. webOS-integratie koppelen: aan/uit werkt?
2. Dashboard-URL openen via `webostv.command`; wat gebeurt er na 10–30 min (valt hij terug)?
3. Met een slimme stekker (Tuya is al in de add-on) het verbruik meten in drie standen: scherm uit · zwart dashboard · licht dashboard.
4. Beslissen: ingebouwde browser of stick/Pi.

## 8. Open punten

- Waar komt het dashboard vandaan: een extra HA-Lovelace-view (de add-on beheert al tabs *Lampen* en *Airco*), of een eigen eenvoudige pagina?
- Bronnen voor agenda (Google), weer (eigen weerwebsite: heeft die een API/JSON?), afvalkalender, boodschappenlijst (eigen lijst / HA-todo).
- Is **Loxone** nog in beeld naast Huisconfig? (De eerste bespreking ging uit van Loxone; de projectmap is HA + ESP-NOW.)

## 9. Later, in deze map: interactief scherm

Niet nu. Wat al besproken is, voor als het zover is:

- **15,6" Nest-achtig**: Blackview Nest 15 (15,6", Android 13, 250 nit, geen batterij; browser/APK-ondersteuning **onbevestigd**, test met retourrecht).
- **Pi 5 + 15,6" touchmonitor** (bv. Denver PMT-15710; alleen touchmodel, helderheid en montage nog te controleren).
- **Samsung Tab A9+/A11+ of Lenovo Tab M11** met laadlimiet (~11").
- **ESP32-paneel via wifi**: openHASP tot 7" (de add-on ondersteunt dit al), ESP32-P4 10,1" met ESPHome voor lichtbediening.
- Niet aanbevolen: Teclast T65 (geen laadlimiet/updates, freezes), Echo Show 15 (sideloaden geblokkeerd).
- Layoutvoorstel kalender-eerst staat in `VOORSTEL.md` §3.1 en blijft bruikbaar.
