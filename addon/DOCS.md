# Kruisschakelaars

Deze add-on is de viewer en configurator van het ESP-NOW kruisschakelaar-netwerk. De lichten werken zonder
Home Assistant: de nodes beslissen zelf. De add-on laat zien wat er gebeurt, bedient lichten als "virtuele
knop", leert welke schakelaar welk licht bedient en houdt de configuratie van de nodes in orde.

## Installatie

1. Installeer de Mosquitto-broker en de MQTT-integratie van Home Assistant (voor lichten en knoppen in HA).
2. Installeer en start deze add-on. Bij de eerste start maakt hij een **netwerksleutel** en bewaart die.
3. Open **Kruisschakelaars** in de zijbalk, tabblad *Instellingen* → *Toon secrets.h*. Zet die regel in
   `firmware/secrets.h` en compileer de firmware. Alle nodes van dit huis delen die sleutel.
4. De add-on praat via UDP (poort 47474) met de bridge(s). Dat gaat standaard via broadcast. Staat een bridge
   achter een repeater die broadcast filtert (bv. het poolhuis), zet hem dan bij **Vaste bridges** in de opties.

## Een licht instellen

1. Tabblad *Lichten* → bij een relais zonder licht *Als licht toevoegen* en een naam kiezen.
2. *Schakelaars leren* → druk op elke fysieke schakelaar die dit licht moet bedienen. Ze verschijnen live in
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

## HA-kringen (de oude AppDaemon-kruisschakelaars)

Tabblad *HA-kringen* → *AppDaemon-YAML importeren*: plak je `kruisschakelaar`-configuratie (een heel `apps.yaml` mag ook),
kies *Controleren* en daarna *OK*. Elke kring is een lijst entiteiten die elkaar volgen. **Alle kringen komen uitgeschakeld
binnen**: de add-on kijkt dan alleen mee en laat de stand zien. Zet een kring *Actief* en zet dezelfde kring **in AppDaemon uit**,
anders schakelen ze allebei. Zo verhuis je kring per kring. *Alles uit* zet alle kringen hier uit.

Elke kring staat ook op het tabblad *Lichten* met een aan/uit-knop. Daaronder klik je op één entiteit (bijvoorbeeld het relais dat de lamp aanstuurt) om alleen die te wisselen.

**Welk relais schakelt de lamp?** Klik naast een entiteit op *lamp?* om haar als lamp-relais te markeren, of kies bij *HA-kringen* → *Lamp-relais kiezen op naam…* een stuk naam (bv. `dingtian`) voor alle kringen tegelijk. De knop toont dan de stand van dat relais en geeft het bevel daaraan eerst; de andere entiteiten blijven de kring volgen.

## Plattegrond

Tabblad *Plattegrond* → *Bewerken*: voeg een verdieping toe, sleep kamers op de tekening en zet je schakelaars en
lampen erop (kies ze in de lijst en klik op de plek, of kies *Alles in een raster zetten* en sleep ze naar hun plek).
In de gewone weergave klik je op een lamp of schakelaar: je ziet dan met een kleur welke schakelaars welke lamp
bedienen en met welke knop (`K2` is knop 2). Een druk op een schakelaar licht live op. De plattegrond is alleen een
tekening; hij verandert niets aan de schakelingen.

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
