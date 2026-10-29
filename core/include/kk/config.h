// Gedeelde types en persistente configuratie. Geen heap, geen Arduino-afhankelijkheden.
#pragma once
#include <stddef.h>
#include <stdint.h>

namespace kk {

using NodeId = uint16_t;
constexpr NodeId ID_NONE = 0x0000;
constexpr NodeId ID_BROADCAST = 0xFFFF;

constexpr uint8_t MAX_RELAYS = 16;
constexpr uint8_t MAX_TRIG = 12;
constexpr uint8_t MAX_IND = 4;
constexpr uint8_t DEFAULT_TTL = 4;

enum class Mode : uint8_t { Toggle = 0, On = 1, Off = 2 };

struct Key {
  uint8_t b[32];
};

struct Trigger {
  NodeId node;
  uint8_t btn;
  uint8_t mode;  // Mode
};

struct RelayCfg {
  Trigger trig[MAX_TRIG];
  uint8_t ntrig;
  uint8_t on;
  uint16_t ver;  // verhoogt bij elke staatswijziging
};

// Een indicator (LED, of een relais dat de LED hardwarematig aanstuurt) volgt de staat van een licht.
struct FollowCfg {
  NodeId node;
  uint8_t relay;
  uint8_t valid;
};

enum ConfigFlags : uint8_t {
  CF_BRIDGE = 1,       // zit ook op wifi/LAN: geeft frames door tussen ESP-NOW en uplink
  CF_UPLINK_ONLY = 2,  // geen radio (bv. de HA-add-on als peer op het LAN)
};

struct Config {
  NodeId id;
  uint16_t boot;  // telt bij elke start op, deel van het frame-id
  uint8_t channel;
  uint16_t epoch;  // kanaal-epoch: hoger = nieuwer
  uint8_t flags;
  uint8_t nbuttons;
  uint8_t nrelays;
  uint8_t nind;
  uint16_t fw;
  RelayCfg relay[MAX_RELAYS];
  FollowCfg follow[MAX_IND];
};

// Alle tijdsconstanten op een plek, zodat de simulator ermee kan experimenteren.
struct Tuning {
  uint32_t debounceMs = 150;
  uint8_t eventCopies = 2;  // blinde herhalingen (zelfde frame); ack-herhaling dekt de rest
  uint16_t eventGapMin = 5, eventGapMax = 14;
  uint16_t fwdJitterMin = 2, fwdJitterMax = 15;
  uint8_t fwdSuppress = 3;      // niet doorsturen als dit frame in de wachttijd al zo vaak is gehoord
  uint8_t fwdSuppressBulk = 1;  // idem voor heartbeats/beacons: zuiniger, ze komen toch regelmatig terug
  uint32_t hbMin = 40000, hbMax = 80000;
  uint8_t hbFollowUps = 2;  // na een eigen staatswijziging: nog zoveel snelle heartbeats (herstelt gemiste berichten)
  uint32_t hbFastMin = 6000, hbFastMax = 12000;
  uint32_t beaconMs = 10000;
  uint32_t beaconLostMs = 40000;   // bridge-beacons zijn zo lang weg: kanaalzoektocht
  uint32_t beaconLostMax = 600000;  // backoff als de zoektocht niets nieuws vindt (bridge echt weg)
  uint32_t bootScanMs = 45000;     // net opgestart en nog geen bridge-beacon gehoord: eenmalig zoeken
  uint32_t lostMs = 150000;         // zo lang helemaal niets gehoord (> hbMax): kanaalzoektocht
  uint32_t scanDwellMs = 40;
  uint8_t scanQuickRetries = 5;  // na een zoektocht zonder bridge: zo vaak snel opnieuw, daarna backoff
  uint32_t scanRetryMin = 3000, scanRetryMax = 8000;
  uint32_t announceSwitchDelayMs = 14;
  uint32_t learnMs = 120000;
  uint8_t ackRetries = 5;     // herhaal een knop-event als het gevolgde licht niet reageert (0 = uit)
  uint32_t ackFirstMs = 50;   // eerste wachttijd, verdubbelt bij elke poging
  uint8_t uplinkCopies = 2;  // LAN-link: elk frame 2x (dedup aan de ontvangende kant)
};

}  // namespace kk
