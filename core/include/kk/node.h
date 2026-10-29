// De kern van een node: flood, lichtlogica, indicatoren, kanaal-volgen.
// Draait ongewijzigd op ESP8266/ESP32 en in de simulator.
#pragma once
#include "kk/config.h"
#include "kk/frame.h"
#include "kk/platform.h"
#include "kk/replay.h"

namespace kk {

struct Stats {
  uint32_t tx = 0, txFwd = 0, rx = 0, dups = 0, badAuth = 0, suppressed = 0, qDrops = 0;
  uint32_t scans = 0, chSwitches = 0, lightChanges = 0, ackRetries = 0;
};

class Node {
 public:
  Node(Platform& p, const Key& key, Config& cfg, const Tuning& t = Tuning());

  // Herstelt de relais uit de config en start de timers. Vóór wifi/mesh aanroepen.
  void begin();
  void loop();  // zo vaak mogelijk (elke ms of sneller)

  void onRadio(const uint8_t* d, size_t n);
  void onUplink(const uint8_t* d, size_t n);

  // Gedebouncete fysieke knop. Alleen "pressed" doet iets.
  void button(uint8_t btn, bool pressed);

  // Opdrachten (HA-peer of bridge)
  void sendCmdLight(NodeId dst, uint8_t relay, Mode op);
  void sendCfgTriggers(NodeId dst, uint8_t relay, const Trigger* t, uint8_t n);
  void sendCfgFollow(NodeId dst, uint8_t ind, NodeId node, uint8_t relay);
  void sendLearn(bool on);

  // Bridge: de wifi-uplink zit nu op dit kanaal.
  void uplinkChannel(uint8_t ch, bool announceOnOld);

  const Config& config() const { return cfg_; }
  bool learning() const;
  bool scanning() const { return scan_.active; }
  Stats stats;

 private:
  struct Out {
    bool used = false, fwd = false;
    uint8_t len = 0, rep = 0, dups = 0, supp = 0;
    uint16_t gapMin = 0, gapMax = 0;
    uint32_t at = 0;
    uint8_t buf[MAX_FRAME];
  };
  static constexpr int QN = 14;

  Platform& p_;
  Key key_;
  Config& cfg_;
  Tuning t_;
  ReplayTable replay_;
  Out q_[QN];
  uint16_t ctr_ = 0;
  uint32_t lastHeard_ = 0, nextHb_ = 0, nextBeacon_ = 0, learnUntil_ = 0;
  uint32_t lastBeacon_ = 0, beaconLostCur_ = 0;
  uint8_t scanFails_ = 0;
  uint8_t hbFast_ = 0;
  uint32_t bootAt_ = 0;
  bool bootScanDone_ = false;
  bool sawBeacon_ = false;
  bool learnOn_ = false;
  uint32_t lastPressAt_[8] = {};
  bool hasPressed_[8] = {};
  // Een herhaald knop-event is een NIEUW frame (nieuw frame-id, zodat forwarders het weer doorgeven)
  // met hetzelfde evt-nummer; de eigenaar dedupliceert op (bron, knop, evt).
  struct AckWait {
    bool active = false;
    uint8_t tries = 0, evt = 0;
    uint32_t at = 0;
  } ack_[8];
  uint8_t evtCtr_[8] = {};
  struct EvtSeen {
    NodeId src = 0;
    uint16_t boot = 0;
    uint8_t btn = 0, evt = 0;
    bool valid = false;
  } evtSeen_[24];
  uint8_t evtNext_ = 0;
  bool evtDuplicate(NodeId src, uint16_t boot, uint8_t btn, uint8_t evt);
  struct Ind {
    bool known = false, on = false;
    uint16_t ver = 0;
  } ind_[MAX_IND];
  struct Scan {
    bool active = false, have = false, bridgeFound = false;
    uint8_t ch = 0, bestCh = 0;
    uint16_t bestEpoch = 0;
    uint32_t dwellEnd = 0;
  } scan_;
  struct Switch {
    bool active = false;
    uint8_t ch = 0;
    uint16_t epoch = 0;
    uint32_t at = 0;
  } sw_;

  uint32_t rnd(uint32_t lo, uint32_t hi) { return lo + (hi > lo ? p_.random() % (hi - lo + 1) : 0); }
  static bool newer16(uint16_t a, uint16_t b) { return int16_t(uint16_t(a - b)) > 0; }
  bool radioCapable() const { return !(cfg_.flags & CF_UPLINK_ONLY); }
  bool isBridge() const { return cfg_.flags & CF_BRIDGE; }

  Frame mk(Type t);
  void emit(Frame& f, uint8_t copies, uint16_t gapMin, uint16_t gapMax, uint32_t delay);
  void queueForward(const uint8_t* d, size_t n, uint8_t ttl);
  bool dupOfPending(const uint8_t* d, size_t n);
  void sendUplinkCopy(const uint8_t* d, size_t n);
  void dropForwards();
  void flush();
  void serviceAcks();

  void accept(const uint8_t* d, size_t n, bool fromUplink);
  void handle(const Frame& f);
  // Wat een staatswijziging veroorzaakte: knop (src, btn) met event-nummer evt (btn 0xFF = commando).
  // De afzender herkent daaraan de bevestiging van zijn eigen event.
  struct Cause {
    NodeId src;
    uint8_t btn, evt;
  };
  void trigger(NodeId src, uint8_t btn, const Cause& cause);
  bool applyOp(uint8_t relay, Mode m, const Cause& cause, bool forceReport);
  void emitLightState(uint8_t relay, const Cause& cause);
  void confirmDuplicate(NodeId src, uint8_t btn, const Cause& cause);
  void emitHeartbeat();
  void emitChannelInfo(Type t, uint32_t delay);
  void noteState(NodeId node, uint8_t relay, bool on, uint16_t ver);
  void applyCfg(const Frame& f);

  void noteChannel(uint8_t chan, uint16_t epoch, uint8_t flags);
  void followChannel(uint8_t ch, uint16_t epoch);
  void doSwitch(uint8_t ch, uint16_t epoch);
  void startScan();
  void stepScan();
  void finishScan();
};

}  // namespace kk
