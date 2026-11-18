// Compileertest: huisdev.h met HUISDEV_NO_WEBSERVER mag WebServer.h niet nodig hebben (de shim geeft #error als hij toch binnenkomt).
// Ook de scenes (1.1) horen zo te werken. Hier staat HUISDEV_NOW niet gedefinieerd: dit compileert en draait het standaard time(nullptr).
#ifndef HUISDEV_NO_WEBSERVER
#define HUISDEV_NO_WEBSERVER
#endif
#include "Arduino.h"
#include "WiFi.h"
#include "WiFiUdp.h"
#include "Preferences.h"
#include "huisdev.h"

int main() {
  HuisDev dev;
  dev.point("a", "A", HD_BOOL);
  dev.begin("x", "X", "1");
  String out;
  if (dev.handle("GET", "/huisdev/state", "", "", out) != 200) return 1;
  if (dev.handle("POST", "/huisdev/scene/store", "{}", "tok", out) != 404) return 2;  // zonder dev.scenes bestaat het pad niet

  dev.scenes({"ledscene/1"}, 8, 768, 8);
  dev.onSceneCheck([](const uint8_t*, size_t) -> String { return ""; });
  dev.onScenePlay([](int, const uint8_t*, size_t, uint32_t) -> HdResult { return HdResult::applied(); });
  dev.onSceneStop([](int) -> HdResult { return HdResult::applied(); });
  dev.sceneRunning(1, 0, 0);
  dev.sceneIdle();
  uint8_t buf[8];
  if (dev.sceneRead(1, buf, sizeof buf) != 0) return 3;
  const String tok = "0123456789abcdef0123456789abcdef";
  if (dev.handle("POST", "/huisdev/claim", String("{\"token\":\"") + tok + "\"}", "", out) != 200) return 4;
  if (dev.handle("POST", "/huisdev/scene/store", "{\"slot\":1,\"data\":\"AQID\"}", tok, out) != 200) return 5;
  if (dev.handle("POST", "/huisdev/scene/play", "{\"slot\":1}", tok, out) != 200) return 6;
  if (dev.handle("POST", "/huisdev/scene/stop", "{}", tok, out) != 200) return 7;
  if (dev.handle("GET", "/huisdev/state", "", tok, out) != 200) return 8;
  dev.loop();
  return dev.sceneRead(1, buf, sizeof buf) == 3 ? 0 : 9;
}
