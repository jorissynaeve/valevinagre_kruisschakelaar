// Wat de firmware (of de simulator) aan de kern moet leveren.
#pragma once
#include "kk/config.h"
#include "kk/frame.h"

namespace kk {

class Platform {
 public:
  virtual ~Platform() {}
  virtual uint32_t nowMs() = 0;
  virtual uint32_t random() = 0;
  // Broadcast op het huidige radiokanaal.
  virtual void radioSend(const uint8_t* d, size_t n) = 0;
  virtual void setChannel(uint8_t ch) = 0;
  // Naar de LAN-link (alleen bridges en de HA-peer). Standaard: niets.
  virtual void uplinkSend(const uint8_t*, size_t) {}
  virtual void setRelay(uint8_t idx, bool on) = 0;
  virtual void setIndicator(uint8_t idx, bool on) = 0;
  // Persistente config opslaan (moet snel terugkeren; de HAL mag het uitstellen).
  virtual void save(const Config& c) = 0;
  // Elk nieuw geaccepteerd frame, voor bridges/HA/tests.
  virtual void onFrame(const Frame&, bool fromUplink) {
    (void)fromUplink;
  }
  virtual void identify() {}
};

}  // namespace kk
