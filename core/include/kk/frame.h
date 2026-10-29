// Frameformaat en AEAD (XChaCha20-Poly1305 via Monocypher).
//
// Draad: ver:1 type:1 ttl:1 src:2 boot:2 ctr:2 | ciphertext | tag:16
// ttl zit niet in de geauthenticeerde data, zodat doorsturen het frame niet opnieuw hoeft te seal'en.
#pragma once
#include "kk/config.h"

namespace kk {

constexpr uint8_t PROTO_VER = 1;
constexpr size_t HDR = 9;
constexpr size_t TAG = 16;
constexpr size_t MAX_FRAME = 250;  // ESP-NOW v1
constexpr size_t MAX_PAYLOAD = MAX_FRAME - HDR - TAG;
constexpr size_t TTL_OFFSET = 2;

enum class Type : uint8_t {
  BtnEvent = 1,
  LightState = 2,
  Heartbeat = 3,
  Probe = 4,
  ProbeResp = 5,
  Beacon = 6,
  Hello = 7,
  CfgSet = 8,
  CfgAck = 9,
  Learn = 10,
  Identify = 11,
  CmdLight = 12,
  OtaCmd = 13,
  OtaStatus = 14,
  Announce = 15,  // 'ik verhuis naar kanaal X' (een hop, op het oude kanaal)
};

// Frames die enkel binnen een eiland (een radiokanaal) betekenis hebben: nooit over de LAN-link.
inline bool isLinkLocal(Type t) {
  return t == Type::Probe || t == Type::ProbeResp || t == Type::Beacon || t == Type::Announce;
}
// Frames die ook binnen het eiland niet worden doorgestuurd. (Beacon wel: die laat elke node
// weten dat de bridge nog op dit kanaal zit.)
inline bool isOneHop(Type t) { return t == Type::Probe || t == Type::ProbeResp || t == Type::Announce; }

struct FrameId {
  NodeId src;
  uint16_t boot;
  uint16_t ctr;
  uint32_t key() const { return (uint32_t(boot) << 16) | ctr; }
};

struct Frame {
  Type type;
  uint8_t ttl;
  FrameId id;
  uint8_t len;
  uint8_t payload[MAX_PAYLOAD];
};

// Geeft het aantal bytes terug (0 bij fout).
size_t seal(const Key& k, const Frame& f, uint8_t* out);
bool open(const Key& k, const uint8_t* in, size_t n, Frame& f);

// Kleine helpers voor payloads (little endian).
struct Writer {
  uint8_t* p;
  size_t n = 0;
  explicit Writer(uint8_t* buf) : p(buf) {}
  void u8(uint8_t v) { p[n++] = v; }
  void u16(uint16_t v) {
    p[n++] = uint8_t(v);
    p[n++] = uint8_t(v >> 8);
  }
};
struct Reader {
  const uint8_t* p;
  size_t len;
  size_t n = 0;
  bool bad = false;
  Reader(const uint8_t* b, size_t l) : p(b), len(l) {}
  uint8_t u8() {
    if (n + 1 > len) {
      bad = true;
      return 0;
    }
    return p[n++];
  }
  uint16_t u16() {
    uint16_t lo = u8();
    uint16_t hi = u8();
    return uint16_t(lo | (hi << 8));
  }
};

}  // namespace kk
