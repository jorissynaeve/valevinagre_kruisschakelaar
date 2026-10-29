// Compileertest: laat de linker de hele kern meenemen (geen echte hardware-aansturing).
#include <Arduino.h>

#include "kk/node.h"

namespace {

class Hal : public kk::Platform {
 public:
  uint32_t nowMs() override { return millis(); }
  uint32_t random() override { return ::random(0x7FFFFFFF); }
  void radioSend(const uint8_t*, size_t) override {}
  void setChannel(uint8_t) override {}
  void setRelay(uint8_t, bool) override {}
  void setIndicator(uint8_t, bool) override {}
  void save(const kk::Config&) override {}
};

Hal hal;
kk::Config cfg{};
kk::Key key{};
kk::Node node(hal, key, cfg);

}  // namespace

void setup() {
  cfg.id = 1;
  cfg.nbuttons = 3;
  cfg.nrelays = 3;
  cfg.nind = 3;
  node.begin();
}

void loop() {
  node.loop();
  node.button(0, digitalRead(0) == LOW);
}
