# ESPHome-migratie (tijdelijk hulpmiddel)

Zoekt ESPHome-toestellen op het netwerk, toont hun gegevens, zet de oude YAML om naar een recente ESPHome en uploadt die draadloos.
Bedoeld om per huis de oude toestellen bij te werken, tot de eigen firmware ze vervangt (DESIGN.md §14 en F7).

## Gebruik

1. Dubbelklik `run.bat` (Windows, Python 3.11+). De eerste keer installeert het ESPHome in `%LOCALAPPDATA%\kk-esphome\venv`.
2. Kies bovenaan de zip of map met de **oude YAML's**. Standaard `Downloads\esphome_projects-main.zip`.
3. **Scannen** zoekt via mDNS en een poortscan (6053) op het /24-netwerk van deze computer. Een ander netwerk of meerdere typ je kommagescheiden (`192.168.1.0/24, 10.0.0.0/24`).
   Staat een toestel er niet bij, vul dan het **IP handmatig** in en klik *Opvragen*.
4. Selecteer een toestel. De kolommen tonen naam, ESPHome-versie en bouwdatum, board, MAC en welke YAML erbij hoort (op naam).
   Zijn er meerdere kandidaten (bv. `th10b01Dallas` en `th10b01DHT`), kies dan de juiste in de lijst *YAML*.
5. **Genereer** maakt de nieuwe YAML (bewerkbaar in het tekstvak, met de wijzigingen in het tabblad *Log*), **Valideer** draait `esphome config`,
   **Compileer** bouwt de firmware en toont de grootte, **Upload (OTA)** vraagt bevestiging, compileert, uploadt en wacht tot het toestel terug is met de nieuwe versie.

## In bulk

Scan het netwerk, selecteer meerdere toestellen (Ctrl/Shift-klik of *Alles selecteren*) en klik **Bulk…**. Na één bevestiging (met de lijst
toestellen en de wifi-netwerken die ze krijgen) gebeurt per toestel, **één voor één**: genereren → `esphome config` → compileren → uploaden → wachten tot het
toestel terug is met een nieuwere versie. Het logboek eindigt met een samenvatting per toestel.

- *Bulk: stop bij eerste fout* (standaard aan): bij een fout wordt niets meer aangeraakt. Uitzetten gaat gewoon door met het volgende toestel.
- *sla recente over* (standaard aan): toestellen die al de geïnstalleerde ESPHome-versie of nieuwer draaien worden overgeslagen. Je kunt een onderbroken run dus gewoon opnieuw starten.
- Overgeslagen worden ook: versleutelde toestellen, toestellen zonder YAML, met meerdere YAML-kandidaten (kies er dan zelf een en gebruik *Upload*), of waarvan de naam in de YAML afwijkt.
- Compileren duurt de eerste keer enkele minuten (ESPHome haalt de toolchain op), daarna ongeveer een minuut per toestel.
- *Stop* stopt na het toestel dat bezig is. Begin met één toestel per type voordat je alles selecteert.

## Wat het doet met de YAML

- `esphome: platform/board` gaat naar `esp8266:`/`esp32:`; `arduino_version` vervalt; `board_flash_mode: dout` komt erbij als GPIO9/10 gebruikt worden (Sonoff T3/4CH).
- `ota:` wordt `- platform: esphome`; `api: password` vervalt; `dallas:` wordt `one_wire:` + `dallas_temp`.
- Wifi-gegevens uit het oude bestand gaan naar `secrets.yaml` in de werkmap (`%LOCALAPPDATA%\kk-esphome`) en staan in de YAML als `!secret`. Hebben alleen die oude bestanden geen wifi-gegevens (bv. `r16b01`), dan wordt de *standaard-wifi* uit het scherm gebruikt.
- Alles gebeurt zonder API-versleuteling en zonder OTA-wachtwoord, zoals nu. Daardoor blijft het toestel in Home Assistant hetzelfde apparaat en kun je het opnieuw scannen.
- Niet omzetbaar: bestanden met `!include`/`<<:` (powerplug_new*), LibreTiny (lsc), en custom components (Cresta, WNK80MA). Die toont het tekstvak onveranderd met de reden.

## Let op

- **Wifi:** komt het toestel na de upload niet op het netwerk, dan moet je het met een kabel herflashen. Controleer dus de netwerken in de bevestiging. Test eerst **één** toestel per type.
- **Home Assistant:** de naam (`esphome.name`) blijft gelijk, dus HA houdt het apparaat en zijn entiteiten. Recente ESPHome kan entiteitsnamen anders samenstellen: kijk na de eerste upload of de entiteiten dezelfde `entity_id` houden.
- **Flash:** op een toestel met 1 MB flash moet de firmware onder ~480 KB blijven (`touch01` met web_server: 376 KB). De tool waarschuwt bij meer.
- Een toestel met een API-sleutel (Noise) antwoordt niet in klare tekst. Het staat dan als "versleuteld" in de lijst; naam en versie zijn zonder sleutel niet op te vragen.
- Alleen dit apparaat-op-het-netwerk wordt aangeraakt. Er wordt niets geschreven naar Home Assistant of de add-on.
