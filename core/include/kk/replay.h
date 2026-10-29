// Per bron een schuivend venster van 64 frame-sleutels (boot<<16|ctr): dedup en replay-bescherming in één.
#pragma once
#include "kk/config.h"

namespace kk {

constexpr uint8_t REPLAY_SOURCES = 48;

class ReplayTable {
 public:
  // true als het frame nieuw is (en wordt onthouden), false bij duplicaat of te oud.
  bool accept(NodeId src, uint32_t key, uint32_t now) {
    Entry* e = find(src);
    if (!e) {
      e = alloc(now);
      e->used = true;
      e->src = src;
      e->hi = key;
      e->win = 1;
      e->seen = now;
      return true;
    }
    e->seen = now;
    int32_t d = int32_t(key - e->hi);
    if (d > 0) {
      e->win = d >= 64 ? 0 : (e->win << d);
      e->win |= 1;
      e->hi = key;
      return true;
    }
    uint32_t age = uint32_t(0) - uint32_t(d);
    if (age >= 64) return false;
    uint64_t bit = uint64_t(1) << age;
    if (e->win & bit) return false;
    e->win |= bit;
    return true;
  }

 private:
  struct Entry {
    bool used = false;
    NodeId src = 0;
    uint32_t hi = 0;
    uint64_t win = 0;
    uint32_t seen = 0;
  };
  Entry e_[REPLAY_SOURCES];

  Entry* find(NodeId s) {
    for (auto& e : e_)
      if (e.used && e.src == s) return &e;
    return nullptr;
  }
  Entry* alloc(uint32_t now) {
    Entry* oldest = &e_[0];
    for (auto& e : e_) {
      if (!e.used) return &e;
      if (uint32_t(now - e.seen) > uint32_t(now - oldest->seen)) oldest = &e;
    }
    return oldest;
  }
};

}  // namespace kk
