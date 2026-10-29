// Korte vingerafdruk van de lichtconfiguratie (triggers + volg-indicatoren) van een node.
// Gaat mee in de heartbeat: de add-on rekent dezelfde hash uit over wat hij de node heeft opgedragen
// en stuurt de configuratie opnieuw als ze verschillen (verloren CFG_SET, nieuw geflashte node).
//
// CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over, in deze volgorde:
//   per relais 0..nrelays-1:  ntrig, daarna per trigger: node (lo, hi), knop, modus
//   per indicator 0..nind-1:  valid (0/1); als valid: node (lo, hi), relais
// De add-on (addon/kkaddon/proto.py) moet dit byte voor byte nabootsen; tests/vectors controleren dat.
#pragma once
#include "kk/config.h"

namespace kk {

class CfgCrc {
 public:
  void add(uint8_t b) {
    crc_ ^= uint16_t(b) << 8;
    for (int i = 0; i < 8; i++) crc_ = (crc_ & 0x8000) ? uint16_t((crc_ << 1) ^ 0x1021) : uint16_t(crc_ << 1);
  }
  void add16(uint16_t v) {
    add(uint8_t(v));
    add(uint8_t(v >> 8));
  }
  uint16_t value() const { return crc_; }

 private:
  uint16_t crc_ = 0xFFFF;
};

inline uint16_t configHash(const Config& c) {
  CfgCrc h;
  for (uint8_t i = 0; i < c.nrelays; i++) {
    const RelayCfg& r = c.relay[i];
    h.add(r.ntrig);
    for (uint8_t k = 0; k < r.ntrig; k++) {
      h.add16(r.trig[k].node);
      h.add(r.trig[k].btn);
      h.add(r.trig[k].mode);
    }
  }
  for (uint8_t j = 0; j < c.nind; j++) {
    const FollowCfg& f = c.follow[j];
    h.add(f.valid ? 1 : 0);
    if (f.valid) {
      h.add16(f.node);
      h.add(f.relay);
    }
  }
  return h.value();
}

}  // namespace kk
