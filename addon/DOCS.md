# Huisconfig

Deze add-on is de viewer en configurator van het ESP-NOW kruisschakelaar-netwerk. De lichten werken zonder
Home Assistant: de nodes beslissen zelf. De add-on laat zien wat er gebeurt, bedient lichten als "virtuele
knop", leert welke schakelaar welk licht bedient en houdt de configuratie van de nodes in orde.

## Installatie

1. Installeer de Mosquitto-broker en de MQTT-integratie van Home Assistant (voor lichten en knoppen in HA).
2. Installeer en start deze add-on. Bij de eerste start maakt hij een **netwerksleutel** en bewaart die.
3. Open **Huisconfig** in de zijbalk, tabblad *Instellingen* → *Toon secrets.h*. Zet die regel in
   `firmware/secrets.h` en compileer de firmware. Alle nodes van dit huis delen die sleutel.
4. De add-on praat via UDP (poort 47474) met de bridge(s). Dat gaat standaard via broadcast. Staat een bridge
   achter een repeater die broadcast filtert (bv. het poolhuis), zet hem dan bij **Vaste bridges** in de opties.

## Schema (zoals Loxone Config)

Tabblad *Schema*: per ruimte een tabblad met blokken en draden. Kies de eerste keer **Huidige configuratie omzetten**. De add-on
maakt dan de ruimtes en zet je kringen, lichten en toestellen op hun plek, met de draden zoals
ze nu lopen. Aan de schakelingen verandert daarbij niets. Later kun je opnieuw *Omzetten*: wat al staat blijft staan.

Een lamp komt in de ruimte uit zijn naam (`Quarto philippe bed links` bij `Quarto philippe`) of zijn HA-gebied. Een schakelaar komt in
zijn eigen HA-gebied of kamer, en anders in **Centraal**. Zet hem via *Ruimte* in het paneel op zijn echte plek: binnen een ruimte
staan ze samen, en de verwijzingen tonen er geen ruimtenaam. *Opnieuw indelen* verdeelt alles opnieuw en wist eigen plekken, nooit draden.

* Een **lamp-blok** heeft links de **ingangen** (de touches die de lamp bedienen) en rechts de **uitgangen** (de relais die de lamp
  echt schakelen). De bol zet de lamp aan of uit. *Actief* of *uit* zet je in de zijbalk.
* **Een lamp uit HA kiezen**: bij *+ Module → Lamp* kies je de lamp of het relais meteen uit een keuzelijst, ook `light.*`. Die lijst staat ook bij de ingangen en uitgangen in de zijbalk van een lamp, bij *Schakelaar* (*Losse entiteit toevoegen*) en bij de zones van een sprinkler. Ze toont alle lampen en schakelaars uit HA, per soort. De lijst *Uit Home Assistant* bij *Schakelaar* toont alleen wat aan een apparaat hangt; een groep, template of helper staat in de andere lijst. Is HA niet bereikbaar, dan typ je de entiteit.
* Een **schakelaar-blok** is een toestel met per gang links een **touch** (ingang) en rechts een **relais** (uitgang).
* Klik op een blok voor de eigenschappen in de zijbalk: een **eigen naam** naast de technische naam uit HA, en de **ruimte**.
* **Draad trekken**: sleep van een poort naar een andere poort, of klik op de ene en daarna op de andere (handig op een tablet).
  **Naar een andere ruimte**: laat de draad los op een lege plek, of klik op de poort. Hij blijft dan wachten: kies het tabblad van de andere ruimte en klik daar op de poort of de lamp. *Stoppen* of Esc breekt af.
  Een verbinding weghalen: klik op de regel in de lamp en druk op Delete, of kies ✕ rechts. Een verbinding in een actieve lamp geldt **meteen**.
* Elke verbinding staat voorlopig als **verwijzing**: `↗` op de lamp en een blauwe tekst op de schakelaar (klik om erheen te gaan). Er worden geen lijnen getekend.
* De kleur toont het type: lamp geel, schakelaar groen, relaisbox oranje, extern apparaat cyaan, node blauw.
  Maak hem in het paneel rechts, op de lamp of op de schakelaar.
* Een relais op een uitgang reageert nog op zijn eigen touch, en een touch-relais volgt de lamp mee (daar hangt zijn LED aan).
  Dat gedrag is hetzelfde als voorheen.

### Timer en disable

Een **timer** (paars, *+ Module* → *Tijd*) heeft per dag tot 3 intervallen waarin zijn uitgang 1 is, en anders 0. De tijd is die van Home
Assistant. Een interval loopt niet over middernacht: maak er twee van, op twee dagen. *Tot 00:00* is tot middernacht.

Sluit de timer aan op de **disable-ingang** van een lamp: sleep naar het paarse bolletje, of kies hem in de zijbalk van de lamp. Staat de
uitgang op 1, dan bedient een touch de lamp niet. Het relais van de touch schakelt zelf en wordt meteen teruggezet op de stand van de lamp;
in het logboek zie je dat de druk genegeerd is. De lamp zelf blijft zoals hij stond, ook als hij aan was. Bedienen in de add-on of via
HA (het lamp-relais) blijft kunnen. Een lamp die niet *Actief* is, doet sowieso niets. Op lichten op het netwerk kan dit nog niet.

Een lamp via Home Assistant werkt alleen met HA-entiteiten, en een licht op het netwerk alleen met nodes en externe apparaten.
Zijn uitgang ligt vast. Mengen kan nog niet.

### Tijd- en logicamodules

Alles wat je op een pagina zet, staat in het menu **+ Module**, rechtsboven in het schema:

| Groep | Module | Uitgang |
|---|---|---|
| Toestellen | Lamp, Schakelaar | |
| Tijd | Timer | 1 binnen de intervallen van de dag |
| | Zonsopgang, Zonsondergang | de tijd van vandaag, op de locatie van Home Assistant |
| | Vaste tijd | een tijd of duur, bv. 02:30 |
| Rekenen | Optellen, Aftrekken | tijd + tijd, tijd − tijd; over middernacht telt het gewoon door |
| Vergelijken | Tussen | 1 als het nu tussen *van* en *tot* is; is *van* later dan *tot*, dan loopt het over middernacht |
| | Na | 1 vanaf die tijd tot middernacht |
| | Voor | 1 van middernacht tot die tijd |
| Logica | En, Of | 1 als beide, of minstens één van de ingangen, 1 is |

Verbind door van een uitgang (rechts) naar een ingang (links) te slepen, of kies de bron in de zijbalk. Cyaan poorten zijn tijden, paarse zijn een 0 of 1;
de verkeerde soort aansluiten of een lus maken wordt geweigerd. Een uitgang die niet te bepalen is (losse ingang, locatie nog niet bekend) toont `?` en blokkeert niets.

Een uitgang met 0 of 1 past op de **disable-ingang** van een lamp. Voorbeeld: touches alleen laten werken tussen zonsondergang en zonsopgang. Zet een *Tussen* met *van* = Zonsopgang
en *tot* = Zonsondergang op de disable: overdag is hij 1 en blokkeert hij de touches. 
### Aan/uit-ingang

Een lamp via Home Assistant heeft ook een **aan/uit-ingang** (boven de disable). Sluit er een uitgang met 0 of 1 op aan, bv. een *Tussen* van Zonsondergang tot Zonsopgang. Op 1 gaat de lamp aan en
blijft hij aan, op 0 gaat hij uit en blijft hij uit. Drukt iemand op een touch of verandert iets anders de stand (ook via HA), dan wordt dat meteen teruggezet; bedienen in de add-on wordt geweigerd.
De lamp reageert binnen 5 seconden op de ingang, en dat werkt alleen als de lamp *Actief* is en HA verbonden is. Een ingang die onbekend geeft (`?`) houdt niets vast: de lamp werkt dan gewoon. De aan/uit-ingang gaat voor op disable.
Op lichten op het netwerk kan dit nog niet.

### Sprinkler

*+ Module* → *Routines* → **Sprinkler**. Elke zone gaat na de vorige aan: zone 1 staat zijn aan-tijd aan en gaat uit, dan volgt de wachttijd, dan zone 2, enzovoort, en dat het aantal cycli lang.

* **Zones**: per zone een naam, een aan-tijd in seconden en een relais. Een relais hoeft geen schakelaar-blok te zijn: kies in de zijbalk **Zones uit relaisbox…** en vink de relais aan die een zone zijn, of kies het relais bij de zone. Staat het relais wel als blok op het schema, dan kun je ook van de zone naar dat **relais** slepen. Een zone zonder relais of met 0 seconden wordt overgeslagen, zonder wachttijd.
* **Instellingen in de zijbalk**: startuur, dagen, aantal cycli en wachttijd. Startuur, cycli en wachttijd kunnen ook van een ingang komen: een tijd (bv. Zonsopgang min 00:30) of een module *Getal*. Een aangesloten ingang gaat voor. Een getal dat onbekend is valt terug op de waarde uit de zijbalk; een onbekend startuur start niets.
* **Blokkeren**: een uitgang met 0 of 1, bv. bij regen. Op 1 start er niets en stopt een lopende beurt.
* **Actief**: een nieuwe sprinkler staat uit en raakt zijn relais niet aan. *Nu starten* werkt alleen als hij actief is. Wat je wijzigt tijdens een beurt, geldt vanaf de volgende beurt; uitzetten stopt meteen.
* **Na een herstart** is een lopende beurt weg: een gemiste beurt wordt niet ingehaald en de relais krijgen "uit".

Een actieve sprinkler stuurt zijn relais elke 30 seconden opnieuw aan (instelbaar), ook als de stand "uit" is. Een gemist bevel herstelt zo vanzelf. **Zet je een relais met de hand aan, dan gaat het dus binnen die tijd weer uit.**

**Bescherming tegen een open klep.** Valt de add-on, HA of de wifi weg terwijl een zone aan staat, dan komt er geen uit-bevel meer. Dat vang je alleen op in het toestel zelf, bv. in ESPHome (de tijd iets langer dan je langste zone):

```yaml
switch:
  - platform: gpio
    id: klep_1
    pin: GPIO5
    on_turn_on:
      - delay: 30min
      - switch.turn_off: klep_1
```

## Scherm (openHASP)

*+ Module → Toestellen → Scherm* zet een aanraakscherm met openHASP-firmware als blok op het schema. De add-on maakt dan de pagina's en bedient de knoppen; pyscript en andere software op Home Assistant zijn er niet voor nodig. De firmware op het scherm blijft, en het scherm praat via de Mosquitto-broker die de add-on ook zelf gebruikt.

* **Eén eigenaar**: haal een scherm eerst uit de pyscript-config (en herstart pyscript) voor je het hier koppelt, anders sturen beide tegelijk.
* **Naam op de firmware**: bv. `scherm01`. Schermen die de broker kent, staan als suggestie in de lijst.
* **Kaarten**: dezelfde kaarten als op een NSPanel (zie hieronder). Sleep de uitgang van een kaart naar **+ kaart** op het scherm; elke kaart is een pagina, in die volgorde. Eén kaart mag op een paneel én een scherm hangen. Knoppen, laadpaal, energie en wifi kunnen; een screensaver niet (de wifi-kaart toont netwerk en wachtwoord als tekst, zonder QR-code).
* **Pagina's**: 4 knoppen per pagina. Een kaart met meer knoppen loopt door op een volgende pagina ("Licht (1/2)", "Licht (2/2)"). Met twee pagina's of meer komen er knoppen onderaan: vorige, eerste, volgende. Een subpagina (een kaart aan een knop van een rasterkaart) heeft onderaan **Terug**, naar de pagina waar je vandaan kwam. Het scherm heeft 12 pagina's; passen de kaarten niet, dan zegt de zijbalk dat en blijft het scherm zoals het was.
* **Lampen uit de add-on**: een lamp of kring van het schema, of een lamp van een andere installatie, leest zijn stand uit de add-on en schakelt rechtstreeks, zoals op een NSPanel.
* **Kleur**: geel is aan, lichtgrijs is uit en donkergrijs is een stand die niet bekend is. Knoppen naar een entiteit die HA niet kent, worden in de zijbalk opgesomd. Een sensor toont zijn waarde.
* **Van 0.15 naar 0.16**: de eigen pagina's van een scherm worden bij de eerste start eenmalig rasterkaarten, naast het scherm op het schema.
* **Donker**: na de ingestelde tijd gaat het scherm uit. De eerste aanraking maakt het alleen wakker en schakelt niets.
* **Wijzigen**: een andere indeling wist het scherm en stuurt het ontwerp opnieuw (ongeveer 5 seconden). Een herstart van de add-on laat het scherm met rust.

Zonder de add-on of HA doen de knoppen niets; de lampen zelf blijven werken. In HA verschijnt per scherm een verbindingssensor.

## NSPanel

*+ Module → Toestellen → NSPanel* zet het scherm met twee knoppen (Sonoff NSPanel) als blok op het schema. De panel praat dan rechtstreeks met deze add-on, zonder MQTT en zonder AppDaemon. De Tasmota-firmware en de TFT van nspanel-lovelace-ui blijven op de panel; alleen het brein verhuist. Alles staat in de zijbalk van het blok en zit in de back-up (*Exporteer configuratie*). Staat er een wifi-wachtwoord op een QR-kaart, dan staat dat dus ook in die export.

### Kaarten

Wat er op het scherm staat, bouw je uit **kaarten**: eigen blokken op het schema (*+ Module → Kaarten*), die je met een draad aan het paneel hangt. Sleep de **uitgang** van een kaart (rechts) naar **+ kaart** op het paneel. De volgorde op het paneel is de volgorde van de pagina's; in de zijbalk van het paneel kun je ze verschuiven (↑ ↓) of weghalen. Eén kaart mag aan meerdere panelen hangen.

| Kaart | Wat ze toont |
|---|---|
| **Knoppen** | Een raster tot 8 knoppen (of een lijst tot 4). Een knop bedient een **lamp** van het schema, een **entiteit** uit HA, of opent een **subpagina**. |
| **Laadpaal** | Start/stop, de status, de laadtijd, de geladen kWh en de prijs van één laadsessie. Kies bij de kaart een **laadpaal** (het blok *Laadpaal*, hier of van een andere installatie, zie *Laadpaal met sessies*). Buiten een sessie toont de kaart alleen de status; tijdens jouw sessie ook tijd, kWh en prijs; tijdens een sessie van een ander huis alleen *Laadpaal bezet*. |
| **Screensaver** | Het startscherm: klok, datum, weer met 4 dagen voorspelling, een temperatuur binnen en tot 2 statuspictogrammen. Eén per paneel; ze is geen pagina. |
| **Energie** | Het energieschema, tot 6 sensoren, met per sensor eventueel twee grenzen voor de kleur (groen, oranje, rood). |
| **Wifi (QR)** | Een QR-code voor het gastnetwerk, met de naam en het wachtwoord ernaast. |
| **Airco** | De thermostaatpagina van het paneel (`cardThermo`) voor een airco van de add-on (zie *Airco*): de temperatuur in de kamer, de ingestelde temperatuur met een schuif om hem te wijzigen, en een knop per stand (uit, auto, koelen, drogen, ventileren, verwarmen). Kies in de zijbalk van de kaart welke airco. De airco kent hele graden (16 tot 30). Is de airco offline, dan staat dat op het scherm en gaat er geen bevel. Alleen op een paneel, niet op een scherm met openHASP. |

* **Een knop aansluiten**: sleep van een knop (links op een rasterkaart) naar een lamp, naar een gang van een schakelaar, of naar de uitgang van een andere kaart. Die laatste wordt een **subpagina**, met een pijl terug. Een entiteit uit HA kies je in de zijbalk.
* **Lampen, de laadpaal en de airco lezen hun stand uit de add-on**, niet via HA: een lamp van het schema (een kring of een licht op het netwerk) en de Tuya-lader werken op het paneel ook als hun naam in HA anders is. De laadpaal start en stopt via datapunt 124. Is de lader offline, dan staat dat op het scherm en gaat er geen bevel.
* **Pictogrammen**: de MDI-naam, bv. `ceiling-light`. Het scherm kent een selectie (ongeveer 100). Een onbekende naam geeft een uitroepteken. Extra namen kun je zetten in `panel_icons.json` in de datamap: `{"naam": "\\ue334"}` (de tekens uit `icon_mapping.py` van nspanel-lovelace-ui).
* **Scherm**: taal (Nederlands of Engels), na hoeveel seconden het terug naar de screensaver gaat, en de helderheid.
* **Screensaver**: de eerste tik maakt de screensaver helder, zodat je het weer en de temperatuur goed ziet. De volgende tik, of meteen een dubbele tik, opent de pagina's. Zonder verdere tik wordt hij weer donker. Rechts van de laatste pagina en links van de eerste staat de screensaver, meteen helder.

* **Waarden van een Tuya-toestel**: een knop van een kaart (best in de lijstweergave) kan een datapunt tonen, bv. het vermogen van een stroommeter, en een rij van de Energie-kaart kan een datapunt zijn in plaats van een sensor uit HA, met dezelfde kleurgrenzen. Kies in de zijbalk *Of een waarde van een Tuya-toestel*: een toestel van deze installatie of een dat je bij een andere installatie gekozen hebt.

### Van YAML naar kaarten

Tot versie 0.10 had elk paneel een YAML van `nspanel-lovelace-ui`. Bij het starten van 0.11 wordt die **eenmalig omgezet naar kaarten**, die naast het paneel op het schema komen. De oude YAML staat daarna in de datamap als `panel-<paneel>.yaml.overgenomen`. Wat niet mee kon (een sjabloon in een naam, een voorwaarde zoals `state:`, een ander kaarttype, opties van de screensaver zoals `tapToWake`) staat in de zijbalk van het paneel onder *Omgezet uit de YAML*, tot je op *Gelezen* klikt.
Een andere YAML (bv. van een tweede panel onder AppDaemon) zet je om via *YAML van nspanel-lovelace-ui omzetten* onderaan de zijbalk van het paneel. De kaarten komen dan achteraan.
* **Touches en relais**: het blok werkt als een schakelaar met twee gangen. Sleep een touch (de knop op de panel) naar de ingang van een lamp, of een relais naar de uitgang van een lamp of een zone van een sprinkler. Dat loopt via hun entiteit in HA (`switch.kk_panel_<paneel>_relay1` en `_relay2`), dus alleen als HA en de add-on draaien. Een knop schakelt altijd zijn eigen relais, ook zonder HA: de lamp ziet die wissel als een druk, en het relais volgt daarna de lamp. Een knop gebruiken zonder dat zijn relais mee schakelt kan nog niet.
* **In Home Assistant** (via MQTT-discovery): *Relais 1*, *Relais 2*, *Temperatuur* (`sensor.kk_panel_<paneel>_temp`) en een Wifi-signaal. Zet die temperatuur op de screensaver-kaart in plaats van `sensor.tasmota_analog_temperature1`.
* **Op de panel zetten**: vul het IP-adres van de panel en dat van Home Assistant in en druk op *Op de panel zetten*. De add-on uploadt `huisconfig.be` (met het adres van deze add-on erin) via de webpagina van de panel, zet `load("huisconfig.be")` achteraan `autoexec.be` als het er nog niet staat en herstart de panel. Heeft de webpagina van de panel een wachtwoord, vul het dan in; het wordt niet bewaard. Het adres van Home Assistant wordt onthouden. Zet de AppDaemon-app eerst uit. Zelf uploaden kan nog: *huisconfig.be downloaden*.
  Terugdraaien kan altijd: de regel weghalen en herstarten.
* **Netwerk**: UDP, poort 47480 op de add-on en 47481 op de panel. Het verkeer is niet versleuteld en de add-on kijkt alleen naar het IP-adres van de panel; houd het op een vertrouwd LAN.
  Een pakket kan verloren gaan: de add-on stuurt de huidige pagina daarom elke twee minuten opnieuw, en een stand die verandert meteen.

## Waarden van Tuya-toestellen in de logica

Een Tuya-blok (van hier, of gekozen bij een andere installatie) heeft poorten zoals de andere blokken: **rechts** wat het toestel meldt, **links** wat je kunt instellen. Naast elke poort staat waar ze aan hangt.

* Sleep een **waarde** (rechts) naar de ingang van *Groter dan*, *Kleiner dan* of een getal-ingang van een sprinkler: de waarde gaat er rechtstreeks in. Of naar een knop van een kaart: die toont de waarde.
* Sleep de uitgang van een **timer of een 0/1-module** naar een **instelling** (links): er komt een routine *Tuya instellen* voor dat datapunt, nog uit. Kies de waarden bij 1 en 0 en zet ze aan.

* **Tuya-waarde** (*+ Module → Waarden*): de waarde van een datapunt als getal, van een toestel hier of bij een andere installatie. Aan/uit is 1/0. Is het toestel offline, dan is de waarde onbekend.
* **Groter dan** en **Kleiner dan** (*+ Module → Vergelijken*): 1 als de waarde boven of onder een grens ligt (met decimalen, bv. 3.5 kW). Die 0/1 sluit je aan op alles wat een 0/1 neemt: de disable- of aan/uit-ingang van een lamp, *En* en *Of*, het blokkeren van een sprinkler, of de routine hieronder.

## Tuya instellen

*+ Module → Routines → Tuya instellen*: een 0/1 van een timer of module zet een datapunt van een Tuya-toestel, hier of bij een andere installatie. Bv. de laadpaal start om 23:00 (*Laden* op `OpenCharging` bij 1, `CloseCharging` bij 0), of de laadstroom gaat naar 8 A als het verbruik boven 3.5 kW komt en terug naar 16 A eronder.

Omdat zo'n routine een lader kan starten, is ze voorzichtig:

* Een nieuwe routine staat **uit**. Aanzetten zet zelf niets: pas als de ingang daarna verandert.
* Ze doet **nooit iets bij het opstarten** of als de ingang weer bekend wordt (na een herstart, of als een toestel of een andere installatie terug is). Die eerste stand onthoudt ze alleen.
* Een **onbekende** ingang doet niets.
* Ze stuurt **hoogstens om de zoveel seconden** (standaard 60): een waarde die rond de grens schommelt laat de lader niet klapperen. Is de ingang na die tijd nog altijd anders, dan gaat het bevel alsnog.
* Laat je de waarde bij 0 leeg, dan gebeurt er bij 0 niets.
* Een mislukt bevel staat in het logboek en op het blok, en wordt na de wachttijd opnieuw geprobeerd. Een toestel van een andere installatie wordt bediend via die installatie, die beslist (alleen wat je daar gekozen hebt).

## Andere installaties (een ander subnet)

Staat een deel van het huis in een ander subnet, met een eigen Home Assistant en een eigen Huisconfig, dan bieden die installaties elkaar hun **lampen** (kringen en lichten op het netwerk), **gangen** (de touch en het relais van een schakelaar), **laadpalen** en **Tuya-toestellen** aan. Je kiest per installatie wat je ervan wilt gebruiken, bv. alleen de laadpaal en de stroommeter. Zo kan een NSPanel in het ene subnet de laadpaal in het andere starten.

1. **Naam**: tabblad *Instellingen* → *Andere installaties* → geef elke installatie een eigen naam (bv. *Hoofdhuis*, *Atelier*). De id die eruit volgt blijft, ook als je de naam later verandert.
2. **Koppelsleutel**: maak op één installatie een sleutel (*Nieuwe sleutel*), **kopieer hem vóór je opslaat**, en plak hem bij elke andere. Alle installaties van het huis gebruiken dezelfde sleutel. De add-on toont hem na het opslaan niet meer.
3. **Adres**: geef bij één van de twee het IP-adres van de andere Home Assistant (en de poort, standaard 47490). Eén kant volstaat; de verbinding werkt in beide richtingen. Met meer installaties geef je elke verbinding die je wilt.
4. **Poort**: de add-on luistert op TCP-poort 47490 (de optie *Poort voor koppelingen*). Zet die open in de router of firewall tussen de subnetten.
5. **Kiezen**: onder *Wat je van andere installaties gebruikt* staat per installatie wat ze aanbiedt. Vink aan wat je hier wilt gebruiken. Niets wordt vanzelf gebruikt: wat je niet aanvinkt, wordt geen blok, de stand ervan komt niet over de lijn, en de andere installatie aanvaardt er geen bevel voor.

Wat je gekozen hebt, staat links in de lijst onder *Andere installaties* en zet je als blok op het schema. Zo'n blok is **alleen-lezen**: je kunt het bedienen (aan/uit, een datapunt van een Tuya-toestel) en aan een kaart hangen, maar instellen, hernoemen en aansluiten op touches doe je bij de installatie waar het toestel hangt. Die beslist ook: een lamp die daar vastgehouden wordt door haar aan/uit-ingang, weigert een bevel.

* **Op een kaart**: sleep een knop van een rasterkaart naar een lamp van een andere installatie, of kies bij een laadpaalkaart een laadpaal onder *Van andere installaties*.
* **In een kring (een touch hier, een lamp daar)**: kies bij de andere installatie een **gang**: een schakelaar uit HA, een extern apparaat, een Tuya-relais of een relais van een NSPanel. Ze verschijnt als blok met een touch en een relais, net als een schakelaar. Sleep de touch naar de ingang van een lamp hier, of het relais naar de uitgang. Een druk op die touch schakelt dan de lamp hier, en de LED van de touch volgt de lamp. Dat werkt alleen op een lamp via Home Assistant (een kring), niet op een licht op het netwerk.
  * **De kring draait waar de lamp gedefinieerd is.** De installatie van de touch voert alleen uit wat de kring vraagt.
  * **Valt de koppeling weg**, dan werkt alles in elk subnet gewoon door, maar de touch daar schakelt de lamp hier niet meer (zijn eigen relais wel). **Komt ze terug**, dan neemt de touch de stand van de lamp over: de lamp heeft gelijk, en er gaat niets vanzelf aan of uit.
  * **Bezet**: zit een gang bij de andere installatie al in een actieve kring, dan staat ze als *bezet* in de lijst. Die kring daar beslist; een bevel van hier wordt geweigerd. Eén ding stuurt een entiteit, ook over installaties heen.
* **Versie**: beide installaties hebben dezelfde versie van de koppeling nodig (0.14 spreekt versie 3). Is één ervan ouder, dan staat bij het adres dat je beide add-ons moet bijwerken.
* **Valt de verbinding weg**, dan blijft het blok staan als offline, met de laatst bekende naam. Een bevel gaat dan niet weg en wordt ook niet bewaard voor later. Bestaat een installatie niet meer, dan kun je haar onder *Bekende installaties* vergeten.
* **Veiligheid**: elk bericht is versleuteld en ondertekend met de koppelsleutel, en een opgenomen bericht nog eens sturen werkt niet. Wie de sleutel niet heeft, krijgt geen antwoord. Een korte zelfbedachte sleutel is wel te raden: gebruik de knop *Nieuwe sleutel*.

## Een licht instellen

1. Tabblad *Schema* → klik op de node en kies bij een gang *Als lamp toevoegen*, met een naam.
2. Klik op de lamp en kies *Schakelaars leren* → druk op elke fysieke schakelaar die dit licht moet bedienen. Ze verschijnen live in
   de lijst. Tijdens het leren schakelt er niets. Kies per schakelaar *wisselen*, *aan* of *uit*.
3. *Opslaan*. De add-on stuurt de configuratie naar de betrokken nodes en toont in het logboek wanneer die
   bevestigd is. De indicator (LED) van een touch volgt automatisch het licht dat die knop bedient.

## Laadpaal met sessies

*+ Module → Toestellen → Laadpaal* maakt een blok op een **Tuya-toestel van deze installatie** (bv. de Feyree-lader). Het blok houdt de laadsessies bij en is wat andere installaties (andere subnets, met een eigen HA) bedienen, in plaats van de ruwe datapunten.

* **Eén sessie tegelijk.** Wie start, is de *starter*. Een tweede start wordt geweigerd (*de laadpaal is bezet*). Alleen de starter kan stoppen; de eigenaar (de installatie waar de laadpaal hangt) altijd, als noodknop. Dat staat dan in de log.
* **Verborgen voor de anderen.** Tijdens een sessie krijgt een andere installatie niets dan *bezet*: geen status, tijd, kWh of naam van de starter. Dat beslist de eigenaar vóór het versturen, het gaat niet over de lijn. Het Tuya-toestel zelf wordt daarom niet meer aan andere installaties aangeboden: ze gaan via het blok. (Gevolg: een kaart of module bij een andere installatie die dat toestel rechtstreeks las, ziet het niet meer; kies daar de laadpaal.)
* **Log.** Elke sessie krijgt een regel: wie (welke installatie), start, einde, duur, kWh, kosten en waarom ze eindigde (gestopt door de starter of de eigenaar, klaar of losgekoppeld, niet gestart). Zie het blok in de zijbalk; *CSV downloaden* geeft alles voor Excel. Laadt de paal zonder dat de add-on startte (kaart of app van de auto), dan staat dat er als *Lokaal (aan de laadpaal)*. In HA verschijnen per laadpaal drie sensoren: *Laadsessie* (vrij of bezet), *Laatste sessie* (kWh) en *Laatste sessie door*.
* **Tabblad Laden (alle sessies en facturen).** Alle sessies van alle laadpalen van deze installatie, met filter op huis, laadpaal en periode (met knoppen *Deze maand* en *Vorige maand*) en de totalen. *CSV van deze selectie* downloadt wat je ziet. Kies een huis en een periode en klik *Factuur maken*: je krijgt een factuur met alle sessies van dat huis in die periode (start, duur, kWh, tarief, bedrag) en het totaal. Vul eventueel een factuurnummer en een opmerking (bv. rekeningnummer) in en kies *Afdrukken of opslaan als PDF*. De sessies volgen op start-tijd; de som is de som van de bedragen zoals ze in de log staan. Een nog lopende sessie staat er pas in als ze eindigt.
* **Tarief.** Op het blok: een vast bedrag per kWh, of een entiteit van HA die wint als ze een getal geeft. De kosten in de log gebruiken het tarief op het moment dat de sessie eindigt.
* **Verbinding weg.** Valt de verbinding met de starter weg, dan loopt de sessie door. Ze eindigt als de laadpaal klaar is, of als de starter (of de eigenaar) stopt. Ook een herstart van de add-on onthoudt wie er laadt. Is de laadpaal zelf offline, dan wordt er niets afgesloten.
* **Instellingen** (op het blok): het toestel, de datapunten voor status, laadtijd en geladen kWh, het datapunt en de waarden om te starten en te stoppen, en per statuswaarde of je mag starten (bv. `connect`, `finish`). De kWh van een sessie is de meterstand aan het eind min die aan het begin; begint de teller opnieuw, dan telt de nieuwe teller.
* **Bij het bijwerken naar 0.28** worden de bestaande laadpaalkaarten eenmalig omgezet: een lader van deze installatie wordt een blok (met de instellingen van de kaart), een lader bij een andere installatie wordt een verwijzing naar het blok dat die installatie op dezelfde manier maakt. Een Feyree-lader zonder kaart krijgt ook een blok. **Werk alle installaties tegelijk bij**: de koppeling weigert een andere versie.

## Tuya rechtstreeks (zonder HACS, LocalTuya of cloud)

*+ Module → Tuya* op het schema zet een Tuya-blok op de pagina. Vul in de zijbalk device-id, IP-adres en local key in. Het protocol (3.3, 3.4 of 3.5) zoekt de add-on zelf. Het blok
toont per toesteltype zijn eigen poorten: links wat je kunt instellen, rechts wat het toestel meldt, met de huidige waarde. Bedienen doe je onder *Bedienen* in de zijbalk; aansluiten met draden kan nog niet. De
entiteiten komen via MQTT in HA, dus de Mosquitto-broker en de MQTT-integratie zijn nodig.

* **Device-id**: SmartLife- of Tuya-app → toestel → ✎ → *Apparaatinformatie* (*Virtuele ID*), of in het Tuya IoT Platform onder
  *Cloud → Devices*.
* **Local key en datamodel, automatisch**: vul onder *Tuya-cloud* eenmalig de *Access ID* en het *Access Secret* van je cloudproject in
  (iot.tuya.com → *Cloud* → je project → *Overview*; de SmartLife-app moet aan het project gekoppeld zijn onder *Devices → Link App Account*),
  en kies het datacenter van je account (Belgisch account: *Centraal-Europa*). Daarna haalt de add-on bij een nieuw device-id meteen de local key en
  het datamodel op; *Ophalen uit de cloud* doet het opnieuw, bv. na opnieuw koppelen (de local key verandert dan). Het toestel zelf werkt
  daarna lokaal, zonder internet. Het IP-adres komt niet uit de cloud (die kent alleen je publieke adres) maar uit het LAN, zie hieronder.
* **Zonder cloud**: de local key uit de API Explorer (*Query Device Details*, `local_key`) zelf invullen en het antwoord van
  *Query Things Data Model* plakken. Zonder datamodel zijn de datapunten alleen te lezen.
* Voor bekende toestellen (nu de *Feyree EV-lader*) heeft de add-on een ingebouwd datamodel met nette namen; dat gebruikt hij vanzelf als
  de cloud dat product meldt.
* **Eén verbinding**: veel toestellen laten maar één lokale verbinding toe. Haal een toestel eerst uit LocalTuya en sluit de SmartLife-app als het niet verbindt.
* **IP-adres, automatisch**: Tuya-toestellen melden zich met een UDP-broadcast (poort 6666 en 6667) en de add-on luistert mee.
  Nieuwere toestellen (3.5) melden zich vaak pas als iemand vraagt wie er is; de add-on doet dat zoals de app, elke minuut op UDP 7000,
  zolang een toestel geen IP heeft of onbereikbaar is. Een toestel dat al verbonden is met LocalTuya meldt zich vaak niet. Toestellen die nog geen blok hebben, staan in de zijbalk onder *Op het netwerk gevonden*: kies er een en device-id en IP staan
  ingevuld. Wat zich ooit meldde, onthoudt de add-on (30 dagen, in `/data/tuya.json`): een nieuw blok krijgt dan meteen het IP,
  zonder op de volgende broadcast te wachten. Een leeg IP vult de add-on zelf in; krijgt een toestel van DHCP een ander adres, dan neemt hij het nieuwe over zolang hij het
  oude niet bereikt. Een vast IP via een DHCP-reservering blijft het betrouwbaarst. Broadcasts komen niet over routers of VLAN's heen.
* De sleutels en het Access Secret staan in `/data/tuya.json`: ze zitten in de HA-back-up, niet in *Exporteer configuratie*. Op armv7 is deze functie niet beschikbaar.

## Airco (Gree, Toyotomi, ...: zonder HACS of cloud)

*+ Module → Airco* zet een airco-blok op de pagina, voor een airco met een Gree-wifimodule: Gree, Toyotomi, Sinclair, Cooper&Hunter en
andere merken die de app *Gree+* of *EWPE Smart* gebruiken. Het is een eigen blok, los van Tuya.

* **Instellen**: kies in de zijbalk de airco onder *Op het netwerk gevonden* (de add-on vraagt elke minuut wie er is, en meteen met
  *Zoeken op het netwerk*), of vul het IP-adres zelf in. Het MAC-adres zoekt de add-on op en de sleutel vraagt hij eenmalig aan de airco
  (binden), met de oudere (v1) of nieuwere versleuteling (v2), wat werkt. Komt er geen antwoord: staat de airco op de wifi, en zit ze in
  hetzelfde subnet? Na een nieuwe wifi-koppeling van de airco: *Opnieuw binden*.
* **In HA**: per airco een `climate.kk_gree_<id>` (aan/uit en stand, temperatuur 16–30 °C, ventilator, lamellen op/neer en links/rechts),
  een sensor voor de kamertemperatuur en schakelaars voor turbo, stil, licht, X-fan, health, energiebesparing, 8 °C-verwarming en slaapstand.
  Via MQTT, dus de Mosquitto-broker en de MQTT-integratie zijn nodig.
* **Overstappen van HACS**: de HACS-integratie mag tijdens de overstap blijven staan (de airco aanvaardt beide). Zet daarna je dashboards en
  automaties om naar `climate.kk_gree_<id>` (of geef die entiteit in HA de naam van de oude) en verwijder de HACS-integratie en haar repository.
* **Ingangen** (links): *aan* (0/1), *temperatuur* (een getal, bv. een module Getal) en *blokkeren* (0/1, bv. een raamcontact via een
  module). Een ingang doet pas iets als ze **verandert**: na een herstart of als de bron weer bekend wordt, onthoudt ze alleen de stand. Wat
  je met de afstandsbediening doet, blijft dus staan tot de ingang weer wisselt. *Blokkeren* op 1 zet de airco uit en houdt *aan* tegen;
  terug op 0 gaat ze weer aan als *aan* dan 1 is. Bij *Ingang aan zet de airco* kies je of ze in haar laatste stand aangaat of in een vaste
  (bv. verwarmen). Lukt een bevel niet (airco onbereikbaar), dan probeert de add-on het na 30 s opnieuw zolang de ingang anders blijft.
* **Uitgangen** (rechts): *aan* (0/1), *kamer* en *ingesteld* (°C). Sleep ze naar Groter dan of Kleiner dan, En, Of, een routine of een
  ingang van een andere airco. Offline is de waarde onbekend.
* De sleutels staan in `/data/gree.json`: in de HA-back-up, niet in *Exporteer configuratie*. Op armv7 is deze functie niet beschikbaar.

## Omvormer (Deye, Sunsynk, ...: zonder Solarman-integratie of cloud)

*+ Module → Omvormer* zet een omvormer-blok op de pagina, voor een omvormer met een Solarman-wifistick (LSW-3): Deye, Sunsynk en andere
merken die de Solarman-app gebruiken. Het is een eigen blok, los van Tuya. De add-on leest de registers van de stick zelf
(TCP 8899, Solarman V5), elke 15 s.

* **Instellen**: de add-on vraagt elke minuut wie er is (en meteen met *Zoeken op het netwerk*) zoals de Solarman-app dat doet. Kies in de
  zijbalk de stick onder *Op het netwerk gevonden*: het IP en het serienummer worden ingevuld. Daarna volgt de add-on de stick zelf als die na
  DHCP een nieuw IP krijgt (alleen als het oude niet meer bereikbaar is). Zelf invullen kan ook: het IP en het **serienummer van de stick**
  (niet dat van de omvormer; het staat op de sticker en op zijn statuspagina). *Serienummer opzoeken* haalt het van die pagina (gebruikersnaam
  en wachtwoord staan standaard op `admin`; ze worden niet bewaard). Zoeken werkt met een broadcast en reikt dus niet over routers of VLAN's
  heen: de add-on moet in het subnet van de sticks draaien.
* **Uitgangen** (rechts): alle 54 waarden die de omvormer meldt, in zes groepen. *Zon*: totaal, per string (PV1, PV2) vermogen, spanning en
  stroom, opbrengst vandaag en totaal. *Accu*: vermogen en stroom (positief = laden), spanning, %, temperatuur, geladen en ontladen
  vandaag en totaal. *Net*: vermogen (positief = afname, negatief = teruglevering), per fase (externe en interne CT), netspanning per fase,
  frequentie, afgenomen en geleverd vandaag en totaal. *Verbruik*: totaal en per fase, spanning, vandaag en totaal. *Omvormer*: uitgang
  totaal en per fase, stroom per fase, temperatuur DC en AC. *Generator*: poort, vandaag en totaal. Op het blok staan standaard zes rijen
  (zon, opbrengst vandaag, accu, accu %, net, verbruik); onder *Uitgangen op het blok* in het zijpaneel vink je aan welke rijen je wilt zien.
  Elke uitgang, ook een die niet op het blok staat, kies je bij een module als bron. Sleep ze naar Groter dan of Kleiner dan, En, Of, een
  routine of een airco-ingang: bv. een boiler of laadpaal starten bij veel zon. Offline is de waarde onbekend, nooit 0.
* **In HA**: per omvormer een apparaat met een sensor voor elk van die 54 waarden, met eenheid en klasse (de energieën als `total_increasing`, dus
  bruikbaar in het energiedashboard), via MQTT. De Mosquitto-broker en de MQTT-integratie zijn dus nodig.
* **Alleen lezen**: er zijn bewust geen ingangen. Een schrijfbevel naar het verkeerde register verandert de instellingen van de omvormer
  en de registers verschillen per model.
* **Getest met een Deye SUN-xK-SG04LP3** (driefasen hybride, 2 MPPT, lagespanningsaccu). Een ander model kan andere registers hebben:
  dan zie je ontbrekende of rare waarden. Meld welk model het is.
* Het serienummer van de stick staat in `/data/solar.json`.

## Zelfbouwapparaten (ESP's met het HUISDEV-protocol)

Een eigen ESP met `huisdev.h` (zie `protocol/huisdev/HUISDEV.md`) meldt zich vanzelf op het netwerk en beschrijft zijn gegevens en acties.
*+ Module → Zelfbouw (HUISDEV)* toont de apparaten die zich meldden; een klik op **Koppelen** zet er een blok van op de pagina. Het blok heeft
een rij per punt (kies in de zijbalk welke), met links een ingang voor wat instelbaar is en rechts een uitgang voor de logica. Komt er op het
apparaat een punt bij of valt er een weg, dan past het blok zich vanzelf aan. In Home Assistant staan de punten als entiteiten
(`kk_esp_<apparaat>_<punt>`). Gevaarlijke acties (reset, kalibratie) bedien je alleen met de hand in de zijbalk, met een bevestiging.
Loskoppelen geeft de claim op het apparaat vrij; lukt dat niet, dan wis je de claim op het apparaat zelf.

### ESPHome-toestellen die geen schakelaar of lamp zijn

Een stopcontact met vermogensmeting, een sensor, een ventilator, een toestel met keuzes of getallen: *+ Module → ESPHome-toestel toevoegen* en
tik het IP-adres in. De YAML van het toestel hoeft niet te veranderen, maar hij moet `web_server:` hebben (bij de meeste staat dat al aan).
Elke entiteit van het toestel wordt een punt of actie op hetzelfde soort blok als hierboven: metingen zijn uitgangen, schakelaars, lichten,
ventilatoren, keuzes, getallen en tekst zijn ook ingangen, een knop is een actie. Een knop als *Restart* of *Reset* is gevaarlijk: alleen met de hand,
met bevestiging. Wat de add-on niet kent (rolluik, klimaat, slot) staat in de zijbalk als "niet ondersteund".

* Geef het toestel een **vast IP** in je router; de add-on zoekt het niet zelf terug.
* Een nieuwe entiteit na een firmware-update verschijnt zodra het toestel opnieuw verbindt (dus na zijn herstart).
* In Home Assistant staan de entiteiten **niet** nog eens via de add-on, want het toestel staat daar meestal al via ESPHome. Wil je ze toch, zet dan in de zijbalk het vinkje *ook als entiteiten in Home Assistant* aan (ze staan dan dubbel).
* Heeft de webserver een wachtwoord (`web_server: auth:`), geef dan gebruiker en wachtwoord bij het toevoegen (API: `POST /api/espdev/esphome` met `user` en `password`).

### Philips Hue: lampen, stopcontacten, bewegingsmelders en kamers

*+ Module → Philips Hue → Hue-bridge koppelen*: tik het IP-adres van de bridge in, druk op de ronde knop bovenop de bridge en klik binnen 30 seconden op OK.
Daarna staan de apparaten van de bridge in hetzelfde menu: één voor één, of *Alles toevoegen* in een raster op de pagina. Elk apparaat is een blok:

| Apparaat | Rijen op het blok |
|---|---|
| Lamp | *Aan* en *Helderheid* (1–100 %): beide zijn uitgang en ingang |
| Stopcontact (een lamp zonder dimmen) | *Aan* |
| Bewegingsmelder | *Beweging*, *Temperatuur*, *Lichtniveau* (lux), *Batterij*: alleen uitgangen |
| Kamer (de groep van de Hue-app) | *Aan*, *Helderheid* (0–100 %, 0 is uit) en *Scène* (een keuze): alle drie uitgang en ingang |

Een **scène** van een kamer heeft een vast nummer (1, 2, …, op volgorde van naam bij het eerste zien; 0 is *geen scène*, schrijven van 0 zet de kamer uit). Een nummer verandert nooit: een scène die je hernoemt houdt zijn nummer, een scène die je weghaalt geeft het niet door aan een nieuwe. Zo kan Loxone een scène als getal onthouden. Een kamer is *bereikbaar* als minstens één van haar lampen dat is.

*Bereikbaar* staat in de zijbalk als diagnose. Knoppen en dimmers (de Hue dimmer switch, Friends of Hue) worden niet aangeboden; ze staan in het menu onder *Niet aangeboden*.

* Een **lamp die de bridge niet bereikt** (zonder netspanning, lege batterij) heeft geen bekende stand: de bridge onthoudt de laatste, maar dat is een gok. Het blok zegt dan *onbereikbaar* en de waarden zijn `?`. Wat de bridge doet met een opdracht aan zo'n lamp is niet geprobeerd; meldt hij een fout, dan staat die bij de ingang in de zijbalk.
* Een **stopcontact herken je niet aan zijn type** (dat kies je zelf in de Hue-app: `plug`, `string_light`, `classic_bulb`) maar aan het ontbreken van dimmen.
* Hue-apparaten staan **niet** in Home Assistant via deze add-on; ze staan daar al via de Hue-integratie. Wil je ze toch, zet dan in de zijbalk het vinkje *ook als entiteiten in Home Assistant* aan.
* De add-on praat alleen met de bridge, met de nieuwe API (CLIP v2) over HTTPS. Het certificaat van de bridge is van Signify en wordt bij het koppelen **vastgezet**. Toont de bridge later een ander certificaat (hij is vervangen of teruggezet), dan staat er *Certificaat opnieuw vertrouwen* in het menu en de zijbalk; dat kan alleen voor dezelfde bridge (zijn bridge-id).
* Geef de bridge een **vast IP** in je router. Het adres of het certificaat wijzigen kan via `PUT /api/espdev/hue/bridge/<id>` met `host` en/of `trust`.
* Een bestaande applicatiesleutel gebruiken in plaats van koppelen: `POST /api/espdev/hue/bridge` met `host` en `key`. Die sleutel trekt de add-on bij *Bridge loskoppelen* **niet** in (hij kan ergens anders in gebruik zijn); een sleutel die de add-on zelf maakte wel.
* Een ingang handelt op een **wijziging** en stuurt absolute opdrachten, zoals bij de overige apparaten. De add-on laat tussen twee opdrachten aan een bridge 0,1 s: Hue verdraagt ongeveer 10 opdrachten per seconde.

### Nuki-sloten

*+ Module → Nuki → Nuki-account koppelen*: plak een **API-token** van Nuki (web.nuki.io → *Integraties* → *API*, met het recht om sloten te lezen en te bedienen,
`smartlock` en `smartlock.action`). Daarna staan je sloten in hetzelfde menu en maak je er een blok van. De add-on gebruikt de Nuki-cloud (`api.nuki.io`): het slot
moet dus via wifi (of een Nuki Bridge) bij Nuki bereikbaar zijn. Een lokale koppeling met een Nuki Bridge (zonder cloud) is er nog niet.

| Punt | Betekenis |
|---|---|
| *Stand* | de stand van Nuki: vergrendeld, ontgrendeld, vrijgegeven, bezig met vergrendelen of ontgrendelen, motor geblokkeerd, niet gekalibreerd |
| *Vergrendeld* | 1 bij vergrendeld, 0 bij ontgrendeld of vrijgegeven; **onbekend** zolang het slot beweegt of een stand heeft die we niet kennen |
| *Deur open* | alleen met een deursensor |
| *Batterij* en *Batterij kritiek* | het percentage (als het slot het meldt) en of Nuki de batterij kritiek vindt |

**Bedienen** kan op twee manieren:

* **Met de hand**, in de zijbalk van het blok. *Vergrendelen* is een gewone knop (en komt als knop in Home Assistant als je het vinkje *ook als entiteiten in Home Assistant* aanzet).
  *Ontgrendelen*, *Deur vrijgeven (unlatch)*, *Lock 'n' go* en *Unlatch met lock 'n' go* openen de deur: die vragen een bevestiging. Ze staan niet in Home Assistant en een MQTT-opdracht ervoor wordt geweigerd.
* **Met een ingang op het blok**: onder de punten staat een rij (▶) per actie, de vier die de deur openen in rood. Sluit er een timer, module of ander signaal op aan
  (in het schema of onder *Ingangen* in de zijbalk) en de actie start als dat signaal van **0 naar 1** gaat. De aansluiting is dan de bevestiging.
  *Unlatch* kent niet elk slot of elke regio (in de VS geeft Nuki daar een fout op): gebruik dan *Ontgrendelen*.

Omdat het een deur is, is een ingang die een actie start **strenger** dan de ingangen van andere apparaten:

* Alleen de overgang **0 naar 1** start. De eerste stand na het aansluiten, na een herstart van de add-on of nadat de bron onbekend was, onthoudt de add-on alleen: een bron die al 1 is, opent niets.
* Een actie **wacht nooit**. Is er op dat moment geen verbinding met Nuki, dan vervalt hij (een deur die tien minuten later opengaat is erger dan een deur die dicht blijft).
* Een mislukte actie wordt **niet opnieuw geprobeerd**; de melding staat bij de ingang in de zijbalk.
* Dezelfde actie start hoogstens eens per **5 seconden**, zodat een bron die flappert de deur niet drie keer opent.
* Elke actie die een ingang start staat in het logboek van de add-on (*… gestart door …*).

Goed om te weten:

* Nuki voert een opdracht **asynchroon** uit. De add-on meldt dat Nuki hem aannam; of het slot hem uitvoerde zie je aan de *Stand*, die na een opdracht na 2, 6 en 15 seconden wordt opgehaald.
  Het slot kan tijdens het uitvoeren even *Ontgrendelen* of *Vergrendelen* melden.
* De stand wordt elke **30 seconden** opgehaald (Nuki laat 100 verzoeken per minuut en 1000 per uur per token toe; één verzoek dient alle sloten van een account). Een wijziging aan het slot zie je dus met enkele tientallen seconden vertraging.
  Nuki geeft de stand door zoals de cloud hem kent: **een slot dat offline is, merkt de add-on niet altijd direct**. Gebruik de stand daarom niet als enige beveiliging.
* Heeft ook iets anders (bijvoorbeeld JavaLox) dezelfde token, dan delen jullie de limiet.
* De token staat alleen in `/data/espdev.json` (0600), nooit in het model, de export of de pagina. *Account loskoppelen* haalt de blokken weg maar trekt de token niet in bij Nuki: doe dat zelf op web.nuki.io.

### Brink-ventilatie (Flair, Ubiflux Vigor) via Brink Home

*+ Module → Brink Home (ventilatie) → Brink Home-account koppelen*: je e-mailadres en wachtwoord van **brink-home.com** (hetzelfde als in de Brink Home-app). Daarna staat je ventilatie-unit in het menu
en maak je er een blok van. De add-on gebruikt de cloud van Brink; er is geen extra hardware voor nodig. (Het oude pad van Brink dat JavaLox gebruikte bestaat niet meer.)

| Punt | Betekenis |
|---|---|
| *Ventilatiestand* | stand 0 tot 3, **instelbaar** |
| *Bedrijfsmodus* | automatisch, handbediening, vakantie, party of nachtventilatie, **instelbaar** |
| *Bypass-modus* | automatisch, gesloten of open, **instelbaar** |
| *Debiet toevoer/afvoer*, *Buiten-* en *Toevoertemperatuur* | metingen |
| *Bypassklep*, *Voorverwarming*, *Filter vuil*, *Dagen sinds filterreset*, *Resterende looptijd* | toestand |
| *Actieve regeling* | wie de unit nu stuurt (portaal, wandschakelaar, ...); diagnose |

* De drie instelbare punten zijn ook **ingangen** op het blok: sluit er een timer, module of getal op aan (bv. een module *Getal* die je 0 tot 3 laat volgen). Een ingang doet pas iets als ze **verandert**.
* De stand die JavaLox zette (0 tot 3) is *Ventilatiestand*. Bij JavaLox stond de unit in *Handbediening*. Of je stand ook blijft gelden in *Automatisch* (waar het klokprogramma van de unit kan sturen), is niet nagegaan: zet de modus dan eerst op *Handbediening*.
* De waarden worden elke **30 seconden** opgehaald en na een opdracht nog drie keer snel. Is de module niet bereikbaar voor Brink, dan staan de waarden op **onbekend** en niet op de laatste stand.
* Het wachtwoord staat alleen in `/data/espdev.json` (0600), nooit in het model, de export of de pagina. Een **onjuist wachtwoord** probeert de add-on niet steeds opnieuw (een account kan geblokkeerd raken); kies dan *Wachtwoord wijzigen* in het menu.
* **Let op:** Brink documenteert deze API niet en wijzigt hem zonder waarschuwing (dat gebeurde al eens). Stopt de koppeling, kijk dan eerst of Brink Home zelf nog werkt. Wil je geen afhankelijkheid van de cloud, dan kan het lokaal via Modbus (zie DESIGN.md §38).

### Smappee-laadpaal (EV Wall) via de cloud van Smappee

*+ Module → Smappee (laadpaal) → Smappee-account koppelen*: de gebruikersnaam en het wachtwoord van **dashboard.smappee.net** (hetzelfde als in de Smappee-app). Daarna staat je laadpunt in het menu en maak je er een blok van.
Smappee heeft geen officiële of lokale API voor de EV Wall: de add-on doet wat het dashboard zelf doet, via de cloud.

**Starten en stoppen doe je met de badge (RFID) aan de laadpaal, niet met de huisconfig.** Hier kun je alleen **pauzeren**, **hervatten** en de **stroom** instellen.

| Punt | Betekenis |
|---|---|
| *Status* | vrij, aangesloten, laadt of gepauzeerd; onbekend als de lader iets meldt wat we niet kennen |
| *Laadt* | waar als hij nu laadt |
| *Gepauzeerd* | **instelbaar**: aan pauzeert, uit hervat |
| *Stroom* | ampère, **instelbaar** binnen het bereik van het laadpunt (bv. 6 tot 32 A) |
| *Laatste sessie* | de kWh van de laatste sessie |
| *Verbruik* | het huidige vermogen in W, **geschat** (zie hieronder) |
| *Teller* | het totaal in kWh sinds 1 januari 2024, met de sleutels van de API van Smappee (zie hieronder) |
| *Beschikbaar* | diagnose |

* *Gepauzeerd* en *Stroom* zijn ook **ingangen** op het blok (bv. een module *Getal* voor de stroom). Een ingang doet pas iets als ze **verandert**.
* **Hervatten zet de lader op Standaard** (de knop Standaard van de Smappee-app). Daarom stuurt de add-on niets als de lader al niet gepauzeerd is, en weigert hij hervatten als de status onbekend is.
* **De stroom werkt alleen op Standaard.** Staat de lader op Slim of Zon, dan beslist hij zelf en is de stroom *onbekend*; instellen wordt dan geweigerd (zet hem in de app op Standaard). Smappee bewaart de stroom als een percentage van het bereik; een heel getal ampère wordt het dichtstbijzijnde percentage en je leest het weer als hetzelfde getal.
* Een opdracht gaat naar Smappee en de stand volgt na enkele seconden (de add-on kijkt daarna nog drie keer snel). Dezelfde opdracht komt hoogstens eens per **5 seconden** door.
* De stand wordt elke **minuut** opgehaald, de sessies elke minuut zolang er geladen of gepauzeerd wordt en anders elke 10 minuten. De stand is die van de cloud: Smappee geeft de live waarden via MQTT, die de add-on niet gebruikt.
* Het wachtwoord staat alleen in `/data/espdev.json` (0600), nooit in het model, de export of de pagina. Een **onjuist wachtwoord** probeert de add-on niet steeds opnieuw; kies dan *Wachtwoord wijzigen* in het menu.
* **Let op:** Smappee documenteert deze API niet en kan hem zonder waarschuwing wijzigen. De add-on is getest tegen een nagebootste cloud, niet tegen een echte EV Wall (zie DESIGN.md §39).

**Verbruik en teller** (zoals `getCurrentRate` en `getCurrentMeter` in JavaLox): *+ Module → Smappee (laadpaal) → Teller en verbruik koppelen*, met de **client-id en het secret van de API van Smappee** (dezelfde als in JavaLox; ze staan alleen in `/data/espdev.json`, nooit op de pagina).
De add-on leest dan `app1pub.smappee.net/dev/v3/servicelocation/<id>/consumption`: de **teller** is de som van alle verbruik sinds 1 januari 2024 en het **verbruik** is het laatste afgeronde blok van 5 minuten × 12.
* **Hoogstens 20 verzoeken per uur** laat Smappee toe. De add-on houdt een eigen telling bij van het afgelopen uur (ook het token telt mee, en de telling blijft bewaard over een herstart) en doet er standaard hoogstens **14**. Het verbruik wordt elke 6 minuten gevraagd tijdens laden en elke 15 minuten daarbuiten, de teller elke 30 minuten en na het einde van een sessie. Is het budget op, dan wacht hij; bij "te veel verzoeken" of onjuiste sleutels vraagt hij niet door (onjuiste sleutels een uur niet).
  **Deelt JavaLox dezelfde client, dan delen jullie die 20**: laat de add-on dan minder doen (een lager budget bij *Sleutels wijzigen*) of zet het ophalen in JavaLox uit.
* **Het verbruik is een schatting** die geen verzoeken kost. Terwijl de laadpaal laadt groeit de energie van de lopende sessie (elke minuut gelezen) en daaruit volgt het vermogen; de teller is de laatste waarde van de API plus wat de sessie sindsdien erbij kreeg. Pas na ongeveer twee minuten laden is er een schatting; zolang die er niet is, of als de energie niet meer verandert terwijl de status nog *Laadt* zegt, geldt het blok van de API of is het verbruik onbekend. Als hij niet laadt is het verbruik 0 (zonder verzoek).
* De API zelf loopt enkele minuten achter. Het verbruik en de teller gelden voor de **hele servicelocatie** van het laadstation: bij een station met twee laadpunten blijven ze (behalve 0 bij niet laden) onbekend.
* *Verbruik* is er ook zonder sleutels (geschat uit de sessie); *Teller* heeft de sleutels nodig voor zijn beginstand.

**Sessies per badge en huis**: kies in de zijbalk van het laadpunt *Laadsessies en badges bijhouden*. Dat maakt een **laadpaal-blok** met een log. Elke badge (de tag van je laadpas) hoort bij een **huis**: dat huis krijgt de kWh en de kosten, en de factuur (tabblad *Laden*) splitst daarop.
Een badge die nog bij geen huis hoort, staat onder *Nog te koppelen*: geef hem een naam en het log van die badge wordt opnieuw toegewezen. Een sessie zonder badge staat in het log als *Zonder badge*. Sessies tellen vanaf het koppelen (of de datum die je bij *Sessies boeken vanaf* zet, tot een week terug) en worden nooit dubbel geboekt.
Een gekoppeld huis (andere installatie) mag **pauzeren, hervatten en de stroom instellen tijdens zijn eigen sessie**; tijdens de sessie van een ander ziet het niets dan *bezet*. Jij (de eigenaar) kunt altijd ingrijpen.
Op de **kaart Laadpaal** zet je *stroomregeling tonen* aan voor een regelaar voor de stroom; de knop op de kaart pauzeert en hervat in plaats van start en stop.

## Energie (alle huisjes op één pagina, met prijzen)

Home Assistant heeft al een energiedashboard (**Energie** in de zijbalk van HA). Het tabblad *Energie* vult het in, zodat je niet in HA
elke meter apart hoeft aan te klikken:

* Elke rij is een meter met een **rol**: *Huis* (een eigen aansluiting met afname en eventueel teruglevering; ze staat apart met haar
  naam in het overzicht, zo zie je alle huisjes naast elkaar), *Net: afname*, *Net: teruglevering*, *Zon*, *Accu* (ontladen en laden zijn twee meters) of *Apparaat* (laadpaal, airco, ...).
* De lijst *Meters die HA kent* toont alle sensoren met een kWh-teller: de stroommeters van de Tuya-toestellen (*Energie totaal*) en de
  totalen van de omvormers. Eén klik maakt er een rij van.
* **Prijs**: een bedrag in € per kWh (`0,31`) of een sensor met de prijs (dynamisch tarief), voor afname en teruglevering, of per rij.
  Zonder prijs toont HA alleen kWh.
* *Opslaan en naar HA schrijven* zet het in het dashboard van HA. De add-on past alleen zijn eigen regels aan: wat je in HA zelf toevoegt blijft
  staan, en een meter die je hier weghaalt verdwijnt daar ook weer. Je kunt zo vaak schrijven als je wilt.
* De meters moeten in **deze** HA staan. Een Tuya-stroommeter die je op deze installatie toevoegt is dat al. Een meter bij een andere
  installatie (*Andere installaties*, bv. het huisje in een ander subnet) komt vanaf 0.27.0 ook als sensor in HA, zodra je dat toestel daar
  gekozen hebt: elk datapunt in kWh wordt `sensor.kk_remote_<installatie>_<toestel>_<dp>`. Valt de verbinding weg, dan blijven de sensoren
  staan (niet beschikbaar) met hun geschiedenis.
* Nieuwere HA-versies hebben per netaansluiting een eigen bron (met naam); oudere hebben één netbron met lijsten. De add-on schrijft het
  formaat dat HA aanvaardt.
* Instellingen staan in `/data/energy.json`.

## Externe apparaten (Tuya via local-tuya)

Schakelaars die niet te flashen zijn, doen toch mee in je kruisschakelingen:

1. Zorg dat het apparaat in Home Assistant staat (local-tuya), met een entiteit per gang.
2. Tabblad *Nodes* → *Extern apparaat toevoegen*: kies per gang de HA-entiteit en vink aan of die gang een lamp aanstuurt.
3. Koppel schakelaars zoals bij elk ander licht (*Schakelaars leren*). De LED van een touch volgt het Tuya-licht, en
   een druk op de touch van de Tuya zelf werkt de andere LED's bij en kan andere lichten schakelen.

Een gang zonder lamp is een gewone knop: zijn druk kan lichten op andere nodes bedienen.
De Tuya-touch schakelt altijd zelf zijn relais. De vertraging voor schakelaars van andere nodes is 100–300 ms
(via Home Assistant). Zonder werkende HA-koppeling staat zo'n apparaat op *offline*; de rest van het netwerk
merkt er niets van. De add-on gebruikt de HA-API via de supervisor (`homeassistant_api` in `config.yaml`).
Buiten Home Assistant (tests) zet je `ha_url` en `ha_token`.

## De oude AppDaemon-kruisschakelaars

Tabblad *Instellingen* → *AppDaemon-YAML importeren*: plak je `kruisschakelaar`-configuratie (een heel `apps.yaml` mag ook),
kies *Controleren* en daarna *OK*. Elke kring wordt een lamp op het schema. **Alle kringen komen uitgeschakeld binnen**: de add-on kijkt
dan alleen mee en laat de stand zien. Zet een lamp *Actief* in de zijbalk en zet dezelfde kring **in AppDaemon uit**, anders schakelen ze
allebei. Zo verhuis je kring per kring. *Alle kringen uitzetten* onder *Instellingen* zet ze hier in één keer uit.

In de zijbalk van een lamp wissel je met ⇄ één relais, en daar staat ook of de relais niet gelijk staan. *Lamp-relais kiezen op naam…*
onder *Instellingen* maakt voor alle kringen tegelijk een stuk naam (bv. `dingtian`) tot uitgang.

## Overzicht van alle apparaten

Bovenaan het tabblad *Nodes* staat *Alle apparaten*: naam, type, protocol, IP-adres en versie van alles wat de add-on
kent (ESP-NOW-nodes, externe apparaten, Tuya lokaal, Gree-airco's, NSPanels, openHASP-schermen) en van alle
ESPHome-apparaten in Home Assistant, ook de nog niet geïmporteerde ("alleen in Home Assistant"). Klik op een kop om
te sorteren. De versie van een ESPHome-apparaat is de ESPHome-versie waarmee het is gebouwd, dus zo zie je welke
toestellen achterlopen. Het adres staat er alleen als het toestel een `web_server` heeft (HA kent het adres anders niet).
ESP-NOW-nodes hebben geen IP-adres.

## Bestaande ESPHome-schakelaars tijdelijk meenemen

Je hoeft de ESPHome-toestellen niet eerst te flashen. Tabblad *Nodes* → *Importeren uit Home Assistant*: vink de
apparaten aan (ESPHome staat bovenaan) en de add-on maakt er externe apparaten van met hun schakelaars als gangen.
Zet daarna een gang uit *stuurt een lamp aan* als die touch alleen een ander licht bedient. Een relaisbox met meer
dan 8 relais wordt in delen gesplitst.

Zet per kring **de oude HA-automation uit** zodra de kring in de add-on staat. Anders schakelen beide en
gebeurt er niets. Zie DESIGN.md §18 voor de stappen en beperkingen.

## Hoe het zichzelf herstelt

Elke node meldt in zijn heartbeat een vingerafdruk van zijn configuratie. Wijkt die af van het model (vers
geflasht, bericht gemist), dan stuurt de add-on de configuratie opnieuw, met oplopende wachttijd. Alleen nodes
die je aan een licht hebt gekoppeld worden zo bijgewerkt; een node die "niet beheerd" is, blijft ongemoeid.
Het model zit in `/data/network.json` (zit in de HA-back-up) en kan geëxporteerd en geïmporteerd worden.

## In Home Assistant

* Elk licht uit het model wordt een `light` (aan/uit). Het staat pas aan als de node de stand heeft bevestigd.
* Elke knop van elke node wordt een `event`-entiteit ("Knop 1"): HA ziet welke schakelaar is ingedrukt, ook als
  er niets mee geschakeld wordt.
* Per node: firmwareversie en "Configuratie wijkt af" (diagnostisch).
* Het logboek in de add-on toont per wijziging welke schakelaar of opdracht de oorzaak was.
* **Tab "Lampen" in de Overview**: de add-on zet zelf een tab *Lampen* naast je andere tabs (Verlichting, Zwembad, ...) in de Overview, met
  alle lampen van het schema per kamer (de pagina waarop de lamp staat, anders het gebied uit HA, anders *Zonder kamer*) en een aan/uit-schakelaar per kamer. Bij een lamp op een extern apparaat (bv. een Dingtian-relais) staat de HA-entiteit van die gang erin.
  Hij volgt vanzelf: een licht toevoegen, verwijderen, verbergen of hernoemen, of een ander gebied in HA, werkt hem binnen enkele seconden bij.
  Alleen die ene tab is van de add-on (handmatige aanpassingen eraan worden overschreven); je andere tabs blijven zoals ze waren.
  Voorwaarde: de Overview is overgenomen (*Overview* → potlood → *Dashboard overnemen*, zodra je zelf tabs hebt is dat al zo). Een automatisch
  gemaakte Overview laat de add-on met rust en meldt dat in het logboek. Het aparte dashboard *Lampen* uit versie 0.18.0 ruimt de add-on zelf op. Heb je airco's (zie *Airco*), dan zet de add-on daar een tweede tab *Airco* naast, met een thermostaat per airco. Ook die volgt
  vanzelf: een airco toevoegen, hernoemen of verwijderen werkt hem bij, en zonder airco's is er geen tab. Zet de optie `ha_dashboard` uit als je dit niet wilt.

## Loxone (in plaats van JavaLox)

Instellingen → **Loxone** koppelt de add-on aan een Loxone Miniserver. Elk signaal is een kort bericht `kk.<naam>=<getal>`; een aan/uit is 1 of 0 en een keuze (scene, ventilatiestand) is het nummer ervan.

1. Vul het IP van de Miniserver in, de poort van zijn virtuele UDP-ingang (je kiest er zelf een, standaard 9100) en eventueel het IP van de add-on.
2. Maak eventueel een **kanaal** (*Kanalen*): één virtuele UDP-ingang plus één virtuele uitgang in Loxone, met een eigen poort. Kies dan bij een apparaat in welk kanaal het zit; zonder keuze zit het in *Standaard*. Zet nieuwe apparaten in een nieuw kanaal, want Config werkt een sjabloon dat al in je project staat niet bij.
   Kies een apparaat (Hue, Smappee, Brink, Nuki, een eigen apparaat) en een voorvoegsel: de punten van zijn blok worden signalen naar Loxone, wat instelbaar is kan Loxone ook zetten (`…_set`), en `…_ok` meldt of het apparaat bereikbaar is.
3. Download per kanaal de **VIU** (wat naar Loxone gaat) en de **VO** (wat van Loxone komt) en importeer ze in Loxone Config als sjabloon. Het signalenoverzicht is per apparaat ingeklapt; het vinkje bij een apparaat bepaalt of het in de bestanden komt (standaard alle apparaten).
   Een sjabloon dat al in je project staat, werkt Config niet bij door opnieuw te importeren, en commando's toevoegen aan een bestaande ingang of uitgang via een bestand lukt niet. Twee virtuele UDP-ingangen op dezelfde poort kunnen ook niet.
   Bij nieuwe signalen naar Loxone typ je de commando's over uit de tabel (kies alleen dat apparaat). Een nieuwe virtuele **uitgang** (van Loxone) mag wel naast de bestaande staan: importeer het bestand van alleen dat apparaat (de titel noemt het apparaat).

**Een Nuki-slot bedienen** (Instellingen → Loxone → *Een slot bedienen vanuit Loxone*): maak een signaal per actie, bijvoorbeeld `voordeur_lock` en `voordeur_unlock`, en importeer de uitgang in Loxone Config. Vergrendelen werkt gewoon. Een actie die de deur **opent** moet je bevestigen met een vinkje en werkt alleen over **HTTP** (kies dat bij *Loxone roept de add-on aan via*); hij start alleen bij een puls van 0 naar 1, hoogstens eens per 5 seconden en alleen als het slot bereikbaar is. Na een herstart van de add-on wordt de eerste 1 genegeerd (je drukt dan nog een keer). De add-on controleert alleen het IP van de Miniserver; wie op het netwerk dat IP kan nabootsen, kan ontgrendelen. Elke start en elke weigering staat in het logboek van de add-on.

De add-on stuurt een waarde bij een verandering en elke minuut opnieuw, en alles 10 s na het starten. Een apparaat dat offline is geeft niets (nooit een verzonnen 0). Van Loxone neemt de add-on alleen berichten aan van het IP van de Miniserver, alleen punten die instelbaar zijn en niet gevaarlijk (nooit een deur), en alleen als de waarde verandert. `kk.sync=1` vanuit Loxone vraagt om alles opnieuw. Gebruik je **HTTP** (aanbevolen: je krijgt een bevestiging), dan roept Loxone `http://<add-on>:<poort>/loxone?kk.<naam>=<waarde>` aan; bij **UDP** stuurt het `kk.<naam>=<waarde>` naar de poort `loxone_port`.

## Opties

| Optie | Betekenis |
|---|---|
| `network_key` | 64 hex-tekens. Leeg: de add-on maakt er een en bewaart hem. |
| `peers` | Vaste bridges (unicast), `host` of `host:poort`. |
| `hub` | Frames van de ene vaste bridge ook naar de andere doorgeven. |
| `broadcast_addr` | Standaard `255.255.255.255`; gebruik het subnet-broadcast als dat niet aankomt. |
| `mqtt_host` e.a. | Leeg: de broker van Home Assistant. |
| `ha_dashboard` | Standaard aan: de add-on houdt de tabs *Lampen* en *Airco* in de Overview van HA bij. |
| `loxone_port` | Standaard 47510 (UDP en TCP): waar de add-on luistert naar de Loxone Miniserver. |

## Beperkingen (versie 0.1)

* Nodes verschijnen pas na hun eerste heartbeat (40–80 s na opstart).
* Externe apparaten zijn nog niet tegen een echte HA geprobeerd (alleen een nagebootste).
* Geen OTA, geen adoptie van nieuwe nodes (de node-id zit nu in de firmware) en geen lange druk of dimmen.
* De web-UI is alleen bereikbaar via de HA-ingress.
