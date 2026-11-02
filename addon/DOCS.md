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
| **Laadpaal** | Start/stop, de status, de laadtijd, de geladen kWh en de prijs (vast tarief per kWh, of een entiteit uit HA die wint als ze een getal geeft). Werkt met elke lader: sleep links een waarde van een Tuya-blok naar *Status*, *Laadtijd* en *Geladen*, en rechts *start/stop* naar een instelling (bv. *Laden*); kies bij de kaart de waarde om te starten en te stoppen. Onder *Start of stop volgens de status* zet je per statuswaarde wat de knop doet: bv. `connect` en `finish` → Start, `charing` → Stop. Bij een andere status toont het paneel alleen de status, zonder knop. Laat je dat leeg, dan toont de knop Stop zolang hij laadt, anders Start. |
| **Screensaver** | Het startscherm: klok, datum, weer met 4 dagen voorspelling, een temperatuur binnen en tot 2 statuspictogrammen. Eén per paneel; ze is geen pagina. |
| **Energie** | Het energieschema, tot 6 sensoren, met per sensor eventueel twee grenzen voor de kleur (groen, oranje, rood). |
| **Wifi (QR)** | Een QR-code voor het gastnetwerk, met de naam en het wachtwoord ernaast. |

* **Een knop aansluiten**: sleep van een knop (links op een rasterkaart) naar een lamp, naar een gang van een schakelaar, of naar de uitgang van een andere kaart. Die laatste wordt een **subpagina**, met een pijl terug. Een entiteit uit HA kies je in de zijbalk.
* **Lampen en de laadpaal lezen hun stand uit de add-on**, niet via HA: een lamp van het schema (een kring of een licht op het netwerk) en de Tuya-lader werken op het paneel ook als hun naam in HA anders is. De laadpaal start en stopt via datapunt 124. Is de lader offline, dan staat dat op het scherm en gaat er geen bevel.
* **Pictogrammen**: de MDI-naam, bv. `ceiling-light`. Het scherm kent een selectie (ongeveer 100). Een onbekende naam geeft een uitroepteken. Extra namen kun je zetten in `panel_icons.json` in de datamap: `{"naam": "\\ue334"}` (de tekens uit `icon_mapping.py` van nspanel-lovelace-ui).
* **Scherm**: taal (Nederlands of Engels), na hoeveel seconden het terug naar de screensaver gaat, en de helderheid.
* **Screensaver**: de eerste tik maakt de screensaver helder, zodat je het weer en de temperatuur goed ziet. De volgende tik, of meteen een dubbele tik, opent de pagina's. Zonder verdere tik wordt hij weer donker. Rechts van de laatste pagina en links van de eerste staat de screensaver, meteen helder.

* **Waarden van een Tuya-toestel**: een knop van een kaart (best in de lijstweergave) kan een datapunt tonen, bv. het vermogen van een stroommeter, en een rij van de Energie-kaart kan een datapunt zijn in plaats van een sensor uit HA, met dezelfde kleurgrenzen. Kies in de zijbalk *Of een waarde van een Tuya-toestel*: een toestel van deze installatie of een dat je bij een andere installatie gekozen hebt.

### Van YAML naar kaarten

Tot versie 0.10 had elk paneel een YAML van `nspanel-lovelace-ui`. Bij het starten van 0.11 wordt die **eenmalig omgezet naar kaarten**, die naast het paneel op het schema komen. De oude YAML staat daarna in de datamap als `panel-<paneel>.yaml.overgenomen`. Wat niet mee kon (een sjabloon in een naam, een voorwaarde zoals `state:`, een ander kaarttype, opties van de screensaver zoals `tapToWake`) staat in de zijbalk van het paneel onder *Omgezet uit de YAML*, tot je op *Gelezen* klikt.
Een andere YAML (bv. van een tweede panel onder AppDaemon) zet je om via *YAML van nspanel-lovelace-ui omzetten* onderaan de zijbalk van het paneel. De kaarten komen dan achteraan.
* **Touches en relais**: het blok werkt als een schakelaar met twee gangen. Sleep een touch (de knop op de panel) naar de ingang van een lamp, of een relais naar de uitgang van een lamp of een zone van een sprinkler. Dat loopt via hun entiteit in HA (`switch.kk_panel_<paneel>_relay1` en `_relay2`), dus alleen als HA en de add-on draaien. Een knop schakelt altijd zijn eigen relais, ook zonder HA: de lamp ziet die wissel als een druk, en het relais volgt daarna de lamp. Een knop gebruiken zonder dat zijn relais mee schakelt kan nog niet.
* **In Home Assistant** (via MQTT-discovery): *Relais 1*, *Relais 2*, *Temperatuur* (`sensor.kk_panel_<paneel>_temp`) en een Wifi-signaal. Zet die temperatuur op de screensaver-kaart in plaats van `sensor.tasmota_analog_temperature1`.
* **Op de panel zetten**: de stappen staan in de zijbalk (script `huisconfig.be` met het adres van deze add-on, uploaden, een regel in `autoexec.be`). Zet de AppDaemon-app eerst uit.
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

Staat een deel van het huis in een ander subnet, met een eigen Home Assistant en een eigen Huisconfig, dan bieden die installaties elkaar hun **lampen** (kringen en lichten op het netwerk), **gangen** (de touch en het relais van een schakelaar) en **Tuya-toestellen** aan. Je kiest per installatie wat je ervan wilt gebruiken, bv. alleen de laadpaal en de stroommeter. Zo kan een NSPanel in het ene subnet de laadpaal in het andere starten.

1. **Naam**: tabblad *Instellingen* → *Andere installaties* → geef elke installatie een eigen naam (bv. *Hoofdhuis*, *Atelier*). De id die eruit volgt blijft, ook als je de naam later verandert.
2. **Koppelsleutel**: maak op één installatie een sleutel (*Nieuwe sleutel*), **kopieer hem vóór je opslaat**, en plak hem bij elke andere. Alle installaties van het huis gebruiken dezelfde sleutel. De add-on toont hem na het opslaan niet meer.
3. **Adres**: geef bij één van de twee het IP-adres van de andere Home Assistant (en de poort, standaard 47490). Eén kant volstaat; de verbinding werkt in beide richtingen. Met meer installaties geef je elke verbinding die je wilt.
4. **Poort**: de add-on luistert op TCP-poort 47490 (de optie *Poort voor koppelingen*). Zet die open in de router of firewall tussen de subnetten.
5. **Kiezen**: onder *Wat je van andere installaties gebruikt* staat per installatie wat ze aanbiedt. Vink aan wat je hier wilt gebruiken. Niets wordt vanzelf gebruikt: wat je niet aanvinkt, wordt geen blok, de stand ervan komt niet over de lijn, en de andere installatie aanvaardt er geen bevel voor.

Wat je gekozen hebt, staat links in de lijst onder *Andere installaties* en zet je als blok op het schema. Zo'n blok is **alleen-lezen**: je kunt het bedienen (aan/uit, een datapunt van een Tuya-toestel) en aan een kaart hangen, maar instellen, hernoemen en aansluiten op touches doe je bij de installatie waar het toestel hangt. Die beslist ook: een lamp die daar vastgehouden wordt door haar aan/uit-ingang, weigert een bevel.

* **Op een kaart**: sleep een knop van een rasterkaart naar een lamp van een andere installatie, of kies bij een laadpaalkaart een lader onder *Van andere installaties*.
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

## Opties

| Optie | Betekenis |
|---|---|
| `network_key` | 64 hex-tekens. Leeg: de add-on maakt er een en bewaart hem. |
| `peers` | Vaste bridges (unicast), `host` of `host:poort`. |
| `hub` | Frames van de ene vaste bridge ook naar de andere doorgeven. |
| `broadcast_addr` | Standaard `255.255.255.255`; gebruik het subnet-broadcast als dat niet aankomt. |
| `mqtt_host` e.a. | Leeg: de broker van Home Assistant. |

## Beperkingen (versie 0.1)

* Nodes verschijnen pas na hun eerste heartbeat (40–80 s na opstart).
* Externe apparaten zijn nog niet tegen een echte HA geprobeerd (alleen een nagebootste).
* Geen OTA, geen adoptie van nieuwe nodes (de node-id zit nu in de firmware) en geen lange druk of dimmen.
* De web-UI is alleen bereikbaar via de HA-ingress.
