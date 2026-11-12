#pragma once
#include "Arduino.h"

enum wl_status_t { WL_IDLE_STATUS = 0, WL_CONNECTED = 3, WL_DISCONNECTED = 6 };

class IPAddress {
 public:
  uint32_t a = 0;
  IPAddress() {}
  IPAddress(uint8_t o1, uint8_t o2, uint8_t o3, uint8_t o4) : a(((uint32_t)o1 << 24) | ((uint32_t)o2 << 16) | ((uint32_t)o3 << 8) | o4) {}
  String toString() const {
    char b[20];
    snprintf(b, sizeof b, "%u.%u.%u.%u", (a >> 24) & 255, (a >> 16) & 255, (a >> 8) & 255, a & 255);
    return String(b);
  }
  static IPAddress parse(const char* s) {
    unsigned x[4] = {0, 0, 0, 0};
    sscanf(s, "%u.%u.%u.%u", &x[0], &x[1], &x[2], &x[3]);
    return IPAddress(x[0], x[1], x[2], x[3]);
  }
};

struct WiFiClass {
  String mac = "AA:BB:CC:A1:B2:C3";  // zoals Arduino-ESP32: hoofdletters met dubbele punten
  bool connected = true;
  IPAddress bcast = IPAddress(192, 168, 1, 255);
  IPAddress ip = IPAddress(192, 168, 1, 50);
  String macAddress() { return mac; }
  wl_status_t status() { return connected ? WL_CONNECTED : WL_DISCONNECTED; }
  IPAddress broadcastIP() { return bcast; }
  IPAddress localIP() { return ip; }
};
inline WiFiClass WiFi;
