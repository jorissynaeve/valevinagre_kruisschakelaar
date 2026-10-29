// Discrete-event simulator (1 ms resolutie) voor de kern: radiobereik, pakketverlies, CSMA,
// botsingen (ook verborgen nodes), kanalen, LAN-link tussen bridges en een HA-peer.
#pragma once
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <functional>
#include <memory>
#include <vector>

#include "kk/node.h"

namespace sim {

struct Rng {
  uint64_t s;
  explicit Rng(uint64_t seed = 1) : s(seed * 0x9E3779B97F4A7C15ull + 1) {}
  uint32_t next() {
    s ^= s >> 12;
    s ^= s << 25;
    s ^= s >> 27;
    return uint32_t((s * 0x2545F4914F6CDD1Dull) >> 32);
  }
  double uni() { return next() / 4294967296.0; }
  uint32_t range(uint32_t lo, uint32_t hi) { return lo + (hi > lo ? next() % (hi - lo + 1) : 0); }
};

class World;

struct Profile {
  uint8_t buttons, relays, inds;
};
constexpr Profile T3{3, 3, 0};      // Sonoff T3: 3 knoppen, 3 relais
constexpr Profile R4{4, 4, 0};      // Sonoff 4CH
constexpr Profile Box16{0, 16, 0};  // relaisbox zonder knoppen
constexpr Profile Sw1{1, 1, 0};     // Basic

struct SimNode : kk::Platform {
  World& w;
  int idx;
  kk::Config cfg{}, flash{};
  std::unique_ptr<kk::Node> node;
  double x = 0, y = 0;
  int floor = 0;
  uint8_t chan = 6;
  bool powered = true;
  bool relayOut[kk::MAX_RELAYS] = {};
  bool indOut[kk::MAX_IND] = {};
  std::vector<std::pair<uint32_t, bool>> relayLog[kk::MAX_RELAYS];
  std::vector<std::pair<uint32_t, bool>> indLog[kk::MAX_IND];
  std::vector<std::pair<uint32_t, kk::Frame>> seen;  // alleen als record=true
  bool record = false;
  uint32_t identifyCount = 0;
  struct Pend {
    std::vector<uint8_t> d;
    uint32_t at;
    int tries;
  };
  std::deque<Pend> txq;
  uint32_t busyUntil = 0;

  SimNode(World& w_, int i) : w(w_), idx(i) {}

  uint32_t nowMs() override;
  uint32_t random() override;
  void radioSend(const uint8_t* d, size_t n) override;
  void setChannel(uint8_t ch) override { chan = ch; }
  void uplinkSend(const uint8_t* d, size_t n) override;
  void setRelay(uint8_t i, bool on) override {
    relayOut[i] = on;
    relayLog[i].push_back({nowMs(), on});
  }
  void setIndicator(uint8_t i, bool on) override {
    indOut[i] = on;
    indLog[i].push_back({nowMs(), on});
  }
  void save(const kk::Config& c) override { flash = c; }
  void onFrame(const kk::Frame& f, bool) override {
    if (record) seen.push_back({nowMs(), f});
  }
  void identify() override { identifyCount++; }
};

struct Tx {
  int from;
  uint8_t ch;
  uint32_t start, end;
  std::vector<uint8_t> d;
  bool done = false;
};

class World {
 public:
  uint32_t now = 0;
  Rng rng;
  kk::Key key{};
  kk::Tuning tuning;
  std::vector<std::unique_ptr<SimNode>> nodes;
  double lossPct = 0;       // extra verlies bovenop afstand
  double r0 = 14, r1 = 28;  // volledig bereik / maximaal bereik (m)
  double floorPenalty = 10;
  uint32_t lanMin = 3, lanMax = 25;
  double lanLoss = 0;
  bool lanUp = true;
  std::vector<int> lan;
  uint64_t framesOnAir = 0, collisions = 0, lossDrops = 0;

  explicit World(uint64_t seed = 1) : rng(seed) {
    for (int i = 0; i < 32; i++) key.b[i] = uint8_t(i * 7 + 3);
  }

  SimNode& add(Profile p, double x, double y, int floor = 0, uint8_t flags = 0, uint8_t chan = 6) {
    int i = int(nodes.size());
    auto n = std::make_unique<SimNode>(*this, i);
    n->cfg.id = kk::NodeId(i + 1);
    n->cfg.nbuttons = p.buttons;
    n->cfg.nrelays = p.relays;
    n->cfg.nind = p.inds;
    n->cfg.channel = chan;
    n->cfg.epoch = 1;
    n->cfg.flags = flags;
    n->cfg.boot = uint16_t(rng.next() % 1000);
    n->x = x;
    n->y = y;
    n->floor = floor;
    n->chan = chan;
    n->flash = n->cfg;
    if (flags & (kk::CF_BRIDGE | kk::CF_UPLINK_ONLY)) lan.push_back(i);
    nodes.push_back(std::move(n));
    return *nodes.back();
  }

  SimNode& addHa() {
    auto& n = add({0, 0, 0}, 0, 0, 0, kk::CF_UPLINK_ONLY);
    n.cfg.id = 0xF000;
    n.flash = n.cfg;
    n.record = true;
    return n;
  }

  void startNode(SimNode& n) {
    n.cfg = n.flash;
    n.node = std::make_unique<kk::Node>(n, key, n.cfg, tuning);
    n.node->begin();
  }
  void startAll() {
    for (auto& n : nodes) startNode(*n);
  }
  void reboot(int i) {
    auto& n = *nodes[i];
    n.txq.clear();
    startNode(n);
  }

  // Een licht bedienen: knop (srcNode,btn) schakelt relais (owner,relay).
  void link(int owner, int relay, int srcNode, int btn, kk::Mode m = kk::Mode::Toggle) {
    auto& rc = nodes[owner]->flash.relay[relay];
    rc.trig[rc.ntrig++] = {nodes[srcNode]->flash.id, uint8_t(btn), uint8_t(m)};
    if (nodes[owner]->node) nodes[owner]->cfg = nodes[owner]->flash;
  }

  double pdr(const SimNode& a, const SimNode& b) const {
    double d = std::hypot(a.x - b.x, a.y - b.y) + floorPenalty * std::abs(a.floor - b.floor);
    if (d <= r0) return 1.0;
    if (d >= r1) return 0.0;
    return 1.0 - (d - r0) / (r1 - r0);
  }

  // Minimaal aantal hops tussen twee nodes over verbindingen met pdr >= minPdr (-1: onbereikbaar).
  int hops(int from, int to, double minPdr = 0.3) const {
    std::vector<int> dist(nodes.size(), -1);
    std::deque<int> q{from};
    dist[from] = 0;
    while (!q.empty()) {
      int u = q.front();
      q.pop_front();
      for (size_t v = 0; v < nodes.size(); v++)
        if (dist[v] < 0 && pdr(*nodes[u], *nodes[v]) >= minPdr) {
          dist[v] = dist[u] + 1;
          q.push_back(int(v));
        }
    }
    return dist[to];
  }

  bool connected(double minPdr = 0.4) const {
    std::vector<int> seen(nodes.size(), 0);
    std::vector<int> st{0};
    seen[0] = 1;
    int cnt = 1;
    while (!st.empty()) {
      int u = st.back();
      st.pop_back();
      for (size_t v = 0; v < nodes.size(); v++)
        if (!seen[v] && pdr(*nodes[u], *nodes[v]) >= minPdr) {
          seen[v] = 1;
          cnt++;
          st.push_back(int(v));
        }
    }
    return cnt == int(nodes.size());
  }

  // ---- tijd
  void run(uint32_t ms) {
    uint32_t end = now + ms;
    while (now < end) step();
  }

  void step() {
    now++;
    // LAN-bezorging
    for (size_t i = 0; i < lanQ.size();) {
      if (lanQ[i].at <= now) {
        auto m = std::move(lanQ[i]);
        lanQ.erase(lanQ.begin() + i);
        auto& n = *nodes[m.to];
        if (n.powered && n.node) n.node->onUplink(m.d.data(), m.d.size());
      } else {
        i++;
      }
    }
    // einde van uitzendingen
    for (auto& t : air)
      if (!t.done && t.end <= now) deliver(t);
    air.erase(std::remove_if(air.begin(), air.end(), [&](const Tx& t) { return t.done && t.end + 6 < now; }), air.end());
    // nodes (start-index roteert voor eerlijkheid)
    size_t N = nodes.size();
    size_t off = rng.next() % (N ? N : 1);
    for (size_t k = 0; k < N; k++) {
      auto& n = *nodes[(k + off) % N];
      if (!n.powered || !n.node) continue;
      n.node->loop();
      startTx(n);
    }
  }

  // ---- radio
  void queueTx(SimNode& n, const uint8_t* d, size_t len) {
    n.txq.push_back({std::vector<uint8_t>(d, d + len), now, 0});
  }

  bool busyFor(const SimNode& n) const {
    for (auto& t : air) {
      if (t.done || t.ch != n.chan || t.end <= now) continue;
      if (pdr(*nodes[t.from], n) > 0.0) return true;
    }
    return false;
  }

  void startTx(SimNode& n) {
    if (n.txq.empty() || n.busyUntil > now) return;
    auto& p = n.txq.front();
    if (p.at > now) return;
    if (busyFor(n) && p.tries < 7) {
      p.tries++;
      p.at = now + 1 + rng.range(0, (1u << std::min(p.tries + 1, 5)) - 1);
      return;
    }
    uint32_t air_ms = 1 + uint32_t(p.d.size()) / 125;
    Tx t{n.idx, n.chan, now, now + air_ms, p.d, false};
    n.busyUntil = t.end;
    air.push_back(std::move(t));
    n.txq.pop_front();
    framesOnAir++;
  }

  void deliver(Tx& t) {
    t.done = true;
    auto& tx = *nodes[t.from];
    for (auto& rp : nodes) {
      SimNode& r = *rp;
      if (r.idx == t.from || !r.powered || !r.node || r.chan != t.ch) continue;
      if (r.cfg.flags & kk::CF_UPLINK_ONLY) continue;
      double p = pdr(tx, r);
      if (p <= 0) continue;
      bool hit = false;
      for (auto& o : air) {
        if (&o == &t || o.ch != t.ch) continue;
        if (!(o.start < t.end && o.end > t.start)) continue;
        if (o.from == r.idx || pdr(*nodes[o.from], r) > 0.05) {
          hit = true;
          break;
        }
      }
      if (hit) {
        collisions++;
        continue;
      }
      if (rng.uni() >= p * (1.0 - lossPct / 100.0)) {
        lossDrops++;
        continue;
      }
      r.node->onRadio(t.d.data(), t.d.size());
    }
  }

  // ---- LAN
  // Koppeling met de buitenwereld (echte UDP, zie udp_sim.cpp): extTx krijgt elk frame dat een bridge op de
  // LAN zet, extRx brengt een frame van buiten bij de bridges.
  std::function<void(const uint8_t*, size_t)> extTx;
  void extRx(const uint8_t* d, size_t n) {
    if (!lanUp) return;
    for (int to : lan)
      if (nodes[to]->cfg.flags & kk::CF_BRIDGE) lanQ.push_back({now + rng.range(lanMin, lanMax), to, std::vector<uint8_t>(d, d + n)});
  }

  void lanSend(int from, const uint8_t* d, size_t n) {
    if (!lanUp) return;
    if (extTx) extTx(d, n);
    for (int to : lan) {
      if (to == from) continue;
      if (lanLoss > 0 && rng.uni() < lanLoss / 100.0) continue;
      lanQ.push_back({now + rng.range(lanMin, lanMax), to, std::vector<uint8_t>(d, d + n)});
    }
  }

  uint32_t totalTx() const {
    uint32_t s = 0;
    for (auto& n : nodes)
      if (n->node) s += n->node->stats.tx;
    return s;
  }

 private:
  struct LanMsg {
    uint32_t at;
    int to;
    std::vector<uint8_t> d;
  };
  std::vector<LanMsg> lanQ;
  std::vector<Tx> air;
};

inline uint32_t SimNode::nowMs() { return w.now; }
inline uint32_t SimNode::random() { return w.rng.next(); }
inline void SimNode::radioSend(const uint8_t* d, size_t n) { w.queueTx(*this, d, n); }
inline void SimNode::uplinkSend(const uint8_t* d, size_t n) { w.lanSend(idx, d, n); }

}  // namespace sim
