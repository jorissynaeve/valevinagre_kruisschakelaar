// Compileertest: huisdev.h met HUISDEV_NO_WEBSERVER mag WebServer.h niet nodig hebben (de shim geeft #error als hij toch binnenkomt).
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
  int st = dev.handle("GET", "/huisdev/state", "", "", out);
  return st == 200 ? 0 : 1;
}
