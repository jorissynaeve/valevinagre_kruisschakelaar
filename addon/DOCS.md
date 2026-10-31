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
* Een **schakelaar-blok** is een toestel met per gang links een **touch** (ingang) en rechts een **relais** (uitgang).
* Klik op een blok voor de eigenschappen in de zijbalk: een **eigen naam** naast de technische naam uit HA, en de **ruimte**.
* **Draad trekken**: sleep van een poort naar een andere poort, of klik op de ene en daarna op de andere (handig op een tablet).
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

## Een licht instellen

1. Tabblad *Schema* → klik op de node en kies bij een gang *Als lamp toevoegen*, met een naam.
2. Klik op de lamp en kies *Schakelaars leren* → druk op elke fysieke schakelaar die dit licht moet bedienen. Ze verschijnen live in
   de lijst. Tijdens het leren schakelt er niets. Kies per schakelaar *wisselen*, *aan* of *uit*.
3. *Opslaan*. De add-on stuurt de configuratie naar de betrokken nodes en toont in het logboek wanneer die
   bevestigd is. De indicator (LED) van een touch volgt automatisch het licht dat die knop bedient.

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
