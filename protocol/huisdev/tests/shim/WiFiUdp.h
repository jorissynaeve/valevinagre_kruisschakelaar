// UDP-stub: inkomende pakketten worden door de test geinjecteerd, uitgaande pakketten worden vastgelegd.
#pragma once
#include <deque>
#include <string>
#include <vector>
#include "WiFi.h"

struct ShimPacket {
  IPAddress ip;
  uint16_t port;
  std::string data;
};
inline std::deque<ShimPacket> g_udp_in;
inline std::vector<ShimPacket> g_udp_out;
inline bool g_udp_on = false;
inline uint16_t g_udp_port = 0;
inline int g_udp_begins = 0, g_udp_stops = 0;

class WiFiUDP {
  ShimPacket cur;
  size_t pos = 0;
  ShimPacket tx;

 public:
  uint8_t begin(uint16_t port) {
    g_udp_on = true;
    g_udp_port = port;
    g_udp_begins++;
    return 1;
  }
  void stop() {
    g_udp_on = false;
    g_udp_stops++;
    g_udp_in.clear();
  }
  int parsePacket() {
    if (!g_udp_on || g_udp_in.empty()) return 0;
    cur = g_udp_in.front();
    g_udp_in.pop_front();
    pos = 0;
    return (int)cur.data.size();
  }
  int read(uint8_t* buf, size_t len) {
    size_t n = cur.data.size() - pos;
    if (n > len) n = len;
    memcpy(buf, cur.data.data() + pos, n);
    pos += n;
    return (int)n;
  }
  IPAddress remoteIP() { return cur.ip; }
  uint16_t remotePort() { return cur.port; }
  int beginPacket(IPAddress ip, uint16_t port) {
    tx.ip = ip;
    tx.port = port;
    tx.data.clear();
    return 1;
  }
  size_t write(const uint8_t* b, size_t n) {
    tx.data.append((const char*)b, n);
    return n;
  }
  int endPacket() {
    g_udp_out.push_back(tx);
    return 1;
  }
};
