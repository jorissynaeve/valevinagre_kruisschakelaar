// Scenario's op het simulatiemodel: betrouwbaarheid, flikkeren, hop-limiet, kanaalwissel, eilanden.
#include <algorithm>
#include <cstdlib>
#include <map>
#include <set>

#include "check.h"
#include "sim.h"

using namespace kk;

namespace {

struct Circuit {
  int owner = 0, relay = 0;
  std::vector<std::pair<int, int>> sw;  // (node, knop)
};

struct House {
  std::vector<Circuit> circuits;
  int bridge = -1, ha = -1;
  int firstNode = 0, lastNode = 0;
};

constexpr sim::Profile TT{3, 3, 3};  // T3 met drie indicatoren

// Radiomodel dat bij een echt huis past: ~10 m betrouwbaar, ~22 m maximaal, verdieping kost 10 m.
void realisticRadio(sim::World& w) {
  w.r0 = 8;
  w.r1 = 22;
  w.floorPenalty = 10;
}

struct Pos {
  double x, y;
  int floor;
};

std::vector<Pos> connectedPositions(int n, uint64_t seed, double W, double H, int floors, const sim::World& probeCfg) {
  sim::Rng rng(seed);
  for (int attempt = 0; attempt < 2000; attempt++) {
    sim::World w(1);
    w.r0 = probeCfg.r0;
    w.r1 = probeCfg.r1;
    w.floorPenalty = probeCfg.floorPenalty;
    std::vector<Pos> pos;
    for (int i = 0; i < n; i++) {
      Pos p{rng.uni() * W, rng.uni() * H, int(rng.next() % floors)};
      pos.push_back(p);
      w.add({0, 0, 0}, p.x, p.y, p.floor);
    }
    if (w.connected(0.5)) return pos;  // elke node heeft een redelijk goede (>=50%) keten naar elke andere
  }
  return {};
}

House buildHouse(sim::World& w, int n, uint64_t seed, bool bridge, double W = 26, double H = 12, int floors = 3) {
  realisticRadio(w);
  House h;
  auto pos = connectedPositions(n, seed, W, H, floors, w);
  sim::Rng rng(seed * 7 + 1);
  h.firstNode = int(w.nodes.size());
  for (int i = 0; i < n; i++) {
    uint8_t flags = (bridge && i == 0) ? CF_BRIDGE : 0;
    w.add(TT, pos[i].x, pos[i].y, pos[i].floor, flags);
  }
  h.lastNode = int(w.nodes.size()) - 1;
  if (bridge) h.bridge = h.firstNode;
  // kringen: elk relais een licht, elke kring 2-4 schakelaars
  std::set<std::pair<int, int>> usedBtn, usedRelay;
  for (int c = 0; c < n; c++) {
    Circuit ci;
    for (int tries = 0; tries < 50; tries++) {
      int o = h.firstNode + int(rng.next() % n), r = int(rng.next() % 3);
      if (usedRelay.count({o, r})) continue;
      usedRelay.insert({o, r});
      ci.owner = o;
      ci.relay = r;
      break;
    }
    int nsw = 2 + int(rng.next() % 3);
    for (int s = 0; s < nsw; s++) {
      for (int tries = 0; tries < 50; tries++) {
        int nd = h.firstNode + int(rng.next() % n), b = int(rng.next() % 3);
        if (usedBtn.count({nd, b})) continue;
        usedBtn.insert({nd, b});
        ci.sw.push_back({nd, b});
        break;
      }
    }
    if (ci.sw.size() < 2) continue;
    for (auto& s : ci.sw) {
      w.link(ci.owner, ci.relay, s.first, s.second);
      // de touch toont de staat van het licht dat die knop bedient
      w.nodes[s.first]->flash.follow[s.second] = {w.nodes[ci.owner]->flash.id, uint8_t(ci.relay), 1};
    }
    h.circuits.push_back(ci);
  }
  return h;
}

double pct(std::vector<double> v, double p) {
  if (v.empty()) return 0;
  std::sort(v.begin(), v.end());
  return v[std::min(v.size() - 1, size_t(p / 100.0 * v.size()))];
}

struct PressStats {
  int presses = 0, ok = 0, missed = 0, doubled = 0;
  std::vector<double> lat;
  double txPerPress = 0;
};

PressStats runPresses(sim::World& w, const House& h, int count, sim::Rng& rng, uint32_t window = 1400, uint32_t gap = 600) {
  PressStats st;
  uint64_t tx0 = w.totalTx();
  for (int i = 0; i < count; i++) {
    const Circuit& c = h.circuits[rng.next() % h.circuits.size()];
    auto s = c.sw[rng.next() % c.sw.size()];
    auto& owner = *w.nodes[c.owner];
    size_t before = owner.relayLog[c.relay].size();
    bool was = owner.relayOut[c.relay];
    uint32_t t0 = w.now;
    w.nodes[s.first]->node->button(uint8_t(s.second), true);
    w.run(window);
    size_t changes = owner.relayLog[c.relay].size() - before;
    st.presses++;
    if (changes == 1 && owner.relayOut[c.relay] != was) {
      st.ok++;
      st.lat.push_back(double(owner.relayLog[c.relay].back().first - t0));
    } else if (changes == 0) {
      st.missed++;
      if (getenv("KK_DEBUG")) {
        auto& sn = *w.nodes[s.first];
        const RelayCfg& rc = owner.cfg.relay[c.relay];
        std::printf("    owner id %u relay %d triggers:", owner.cfg.id, c.relay);
        for (int k = 0; k < rc.ntrig; k++) std::printf(" (%u,%u)", rc.trig[k].node, rc.trig[k].btn);
        std::printf("  | sender id %u btn %d  ackRetries=%u owner.rx=%u owner.dups=%u owner.badAuth=%u\n", sn.cfg.id, s.second, sn.node->stats.ackRetries, owner.node->stats.rx, owner.node->stats.dups, owner.node->stats.badAuth);
        std::printf("    MISS press #%d: switch node %d btn %d -> owner %d relay %d (dist %.1f m, hops %d) t=%u\n", i, s.first, s.second, c.owner, c.relay, std::hypot(sn.x - owner.x, sn.y - owner.y), w.hops(s.first, c.owner), w.now);
      }
    } else {
      st.doubled++;
    }
    w.run(gap);
  }
  st.txPerPress = double(w.totalTx() - tx0) / count;
  return st;
}

// ------------------------------------------------------------------------------------------------

void scenarioReliability(bool quick) {
  std::printf("[scen] betrouwbaarheid, 24 nodes / 3 verdiepingen, kringen met 2-4 schakelaars\n");
  std::printf("  %-6s %-8s %-8s %-8s %-9s %-9s %-9s %-10s %s\n", "loss%", "ok%", "gemist", "dubbel", "lat p50", "lat p95", "lat p99", "tx/press", "botsingen");
  int n = quick ? 80 : 300;
  struct Case {
    double loss, minOk;
  } cases[] = {{0, 99.0}, {10, 98.0}, {25, 93.0}};
  for (auto c : cases) {
    sim::World w(100 + int(c.loss));
    w.tuning = Tuning();
    House h = buildHouse(w, 24, 5, false);
    w.lossPct = c.loss;
    w.startAll();
    w.run(5000);
    sim::Rng rng(77);
    PressStats st = runPresses(w, h, n, rng);
    double okp = 100.0 * st.ok / st.presses;
    std::printf("  %-6.0f %-8.1f %-8d %-8d %-9.0f %-9.0f %-9.0f %-10.1f %llu\n", c.loss, okp, st.missed, st.doubled, pct(st.lat, 50), pct(st.lat, 95), pct(st.lat, 99), st.txPerPress, (unsigned long long)w.collisions);
    CHECKM(okp >= c.minOk, "loss %.0f%%: %.1f%% geslaagd (min %.0f%%)", c.loss, okp, c.minOk);
    CHECKM(st.doubled == 0, "dubbele toggles bij loss %.0f%%", c.loss);
    CHECK(pct(st.lat, 95) < 150);
  }
}

void scenarioIndicators(bool quick) {
  std::printf("[scen] terugkoppeling (LED), geen flikkeren, zelfherstel via heartbeat\n");
  sim::World w(200);
  House h = buildHouse(w, 24, 9, false);
  w.lossPct = 25;
  w.startAll();
  w.run(5000);
  sim::Rng rng(5);
  runPresses(w, h, quick ? 40 : 120, rng);
  auto mismatches = [&]() {
    int bad = 0, total = 0;
    for (auto& c : h.circuits)
      for (auto& s : c.sw) {
        total++;
        if (w.nodes[s.first]->indOut[s.second] != w.nodes[c.owner]->relayOut[c.relay]) bad++;
      }
    return std::pair<int, int>(bad, total);
  };
  auto before = mismatches();
  // rustig wachten: heartbeats herstellen de gemiste LIGHT_STATE-frames. Intussen mogen er geen relais schakelen.
  std::vector<size_t> logs;
  for (auto& c : h.circuits) logs.push_back(w.nodes[c.owner]->relayLog[c.relay].size());
  w.run(120000);
  auto after = mismatches();
  std::printf("  indicator-afwijkingen: direct na drukken %d/%d, na 120 s rust %d/%d\n", before.first, before.second, after.first, after.second);
  CHECKM(after.first == 0, "%d indicatoren blijven fout na heartbeats", after.first);
  int flicker = 0;
  for (size_t i = 0; i < h.circuits.size(); i++)
    if (w.nodes[h.circuits[i].owner]->relayLog[h.circuits[i].relay].size() != logs[i]) flicker++;
  CHECKM(flicker == 0, "%d relais schakelden spontaan tijdens rust", flicker);
}

void scenarioIdleTraffic() {
  std::printf("[scen] verkeer in rust (30 nodes)\n");
  sim::World w(300);
  buildHouse(w, 30, 11, true);
  w.startAll();
  w.run(10000);
  uint64_t f0 = w.framesOnAir;
  w.run(300000);
  double perSec = double(w.framesOnAir - f0) / 300.0;
  std::printf("  %.2f frames/s over de lucht (heartbeats + beacons)\n", perSec);
  CHECK(perSec < 6.0);
}

void scenarioHops() {
  std::printf("[scen] hop-limiet (TTL %d) op een ketting (1 pad, geen alternatieven), 20 drukken per hop\n", DEFAULT_TTL);
  std::printf("  %-6s %-22s %s\n", "hop", "zonder ack (blind)", "met ack-herhaling (touch kent zijn licht)");
  const int N = 20;
  for (int k = 1; k <= 6; k++) {
    int res[2];
    for (int withAck = 0; withAck < 2; withAck++) {
      sim::World w(400 + k);
      w.r0 = 10;
      w.r1 = 12;
      sim::Profile P{1, 1, 1};
      for (int i = 0; i < 8; i++) w.add(P, i * 10.0, 0);
      w.link(k, 0, 0, 0);
      if (withAck) w.nodes[0]->flash.follow[0] = {w.nodes[k]->flash.id, 0, 1};
      w.startAll();
      w.run(2000);
      for (int i = 0; i < N; i++) {
        w.nodes[0]->node->button(0, true);
        w.run(1800);
      }
      res[withAck] = int(w.nodes[k]->relayLog[0].size()) - 1;
    }
    std::printf("  %-6d %2d/%-19d %2d/%d\n", k, res[0], N, res[1], N);
    if (k <= DEFAULT_TTL) {
      CHECKM(res[0] >= N * 0.7, "hop %d blind: %d/%d", k, res[0], N);
      CHECKM(res[1] >= N - 1, "hop %d met ack: %d/%d", k, res[1], N);
    } else {
      CHECKM(res[0] == 0 && res[1] == 0, "hop %d ligt buiten de TTL maar kreeg %d/%d", k, res[0], res[1]);
    }
  }
}

// ---- kanaalwissel

struct ChanResult {
  double convergeMs = -1;
  bool lightsOk = false;
};

ChanResult channelMove(double loss, bool announce, bool lateNode, uint64_t seed, bool quick) {
  (void)quick;
  sim::World w(seed);
  House h = buildHouse(w, 20, seed, true);
  w.lossPct = loss;
  w.startAll();
  w.run(30000);
  int late = h.lastNode;  // valt weg tijdens de wissel
  if (lateNode) w.nodes[late]->powered = false;
  uint32_t t0 = w.now;
  w.nodes[h.bridge]->node->uplinkChannel(11, announce);
  ChanResult r;
  auto all = [&]() {
    for (int i = h.firstNode; i <= h.lastNode; i++) {
      auto& n = *w.nodes[i];
      if (!n.powered) continue;
      if (n.chan != 11 || n.cfg.epoch != 2) return false;
    }
    return true;
  };
  uint32_t lateOn = 120000;
  bool lateDone = !lateNode;
  for (uint32_t t = 0; t < 400000; t += 100) {
    w.run(100);
    if (lateNode && !lateDone && w.now - t0 >= lateOn) {
      w.nodes[late]->powered = true;
      w.reboot(late);  // komt terug op het oude kanaal
      lateDone = true;
    }
    if (lateDone && all()) {
      r.convergeMs = double(w.now - t0);
      break;
    }
  }
  if (r.convergeMs >= 0) {
    sim::Rng rng(3);
    PressStats st = runPresses(w, h, 15, rng);
    r.lightsOk = st.ok >= 14;
  }
  return r;
}

void scenarioChannel(bool quick) {
  std::printf("[scen] kanaalwissel van de router (bridge 6 -> 11), 20 nodes\n");
  std::printf("  %-34s %s\n", "variant", "alle nodes op kanaal 11 na");
  struct V {
    const char* name;
    double loss;
    bool announce, late;
    double maxMs;
  } vs[] = {
      {"aankondiging op oud kanaal, 0% verlies", 0, true, false, 5000},
      {"aankondiging op oud kanaal, 15% verlies", 15, true, false, 15000},
      {"geen aankondiging (alleen scannen), 0%", 0, false, false, 120000},
      {"geen aankondiging (alleen scannen), 15%", 15, false, false, 180000},
      {"1 node uitgevallen, komt later terug", 5, true, true, 300000},
  };
  for (auto& v : vs) {
    if (quick && v.late) continue;
    ChanResult r = channelMove(v.loss, v.announce, v.late, 500, quick);
    if (r.convergeMs < 0)
      std::printf("  %-34s NIET geconvergeerd\n", v.name);
    else
      std::printf("  %-34s %.1f s, lichten daarna %s\n", v.name, r.convergeMs / 1000.0, r.lightsOk ? "ok" : "FOUT");
    CHECKM(r.convergeMs >= 0 && r.convergeMs <= v.maxMs, "%s: %.0f ms (max %.0f)", v.name, r.convergeMs, v.maxMs);
    CHECK(r.lightsOk);
  }
}

// ---- twee eilanden (huis op kanaal 6, poolhuis op kanaal 11) over de LAN-link, met HA-peer

void scenarioIslands(bool quick) {
  std::printf("[scen] twee eilanden via LAN-link + HA-peer\n");
  for (double lanLoss : {0.0, 10.0}) {
    sim::World w(600 + int(lanLoss));
    realisticRadio(w);
    w.lanLoss = lanLoss;
    sim::Profile P{3, 3, 3};
    // eiland A: huis, kanaal 6
    std::vector<int> A;
    for (int i = 0; i < 8; i++) A.push_back(w.add(P, 3.0 * i, (i % 2) * 4.0, 0, i == 0 ? CF_BRIDGE : 0, 6).idx);
    // eiland B: poolhuis, ver weg, kanaal 11
    std::vector<int> B;
    for (int i = 0; i < 3; i++) B.push_back(w.add(P, 400.0 + 3.0 * i, 0, 0, i == 0 ? CF_BRIDGE : 0, 11).idx);
    auto& ha = w.addHa();
    // poolhuislicht (B[1] relais 0) wordt bediend vanuit het huis (A[3] knop 0) en vanuit het poolhuis (B[2] knop 1)
    w.link(B[1], 0, A[3], 0);
    w.link(B[1], 0, B[2], 1);
    // huislicht (A[5] relais 1) wordt ook bediend vanuit het poolhuis
    w.link(A[5], 1, B[2], 2);
    w.nodes[A[3]]->flash.follow[0] = {w.nodes[B[1]]->flash.id, 0, 1};
    w.startAll();
    w.run(20000);

    uint32_t t0 = w.now;
    w.nodes[A[3]]->node->button(0, true);
    w.run(500);
    bool ok1 = w.nodes[B[1]]->relayOut[0];
    uint32_t lat = ok1 ? w.nodes[B[1]]->relayLog[0].back().first - t0 : 0;
    w.run(60000);  // heartbeats herstellen een gemiste terugkoppeling over de LAN
    bool led = w.nodes[A[3]]->indOut[0] == w.nodes[B[1]]->relayOut[0] && w.nodes[B[1]]->relayOut[0];
    w.nodes[B[2]]->node->button(2, true);
    w.run(500);
    bool ok2 = w.nodes[A[5]]->relayOut[1];
    // HA reconstrueert de staat uit LIGHT_STATE- en HEARTBEAT-frames (met versienummers)
    auto haView = [&](int node, int relay) {
      uint16_t bestVer = 0;
      bool have = false, on = false;
      for (auto& sf : ha.seen) {
        const Frame& f = sf.second;
        if (f.id.src != w.nodes[node]->flash.id) continue;
        Reader r(f.payload, f.len);
        uint16_t ver = 0;
        bool v = false;
        if (f.type == Type::LightState) {
          if (r.u8() != relay) continue;
          v = r.u8() != 0;
          ver = r.u16();
        } else if (f.type == Type::Heartbeat) {
          r.u16();
          r.u8();
          uint8_t n = r.u8();
          if (relay >= n) continue;
          for (int k = 0; k < relay; k++) {
            r.u8();
            r.u16();
          }
          v = r.u8() != 0;
          ver = r.u16();
        } else {
          continue;
        }
        if (!have || int16_t(uint16_t(ver - bestVer)) > 0) {
          have = true;
          bestVer = ver;
          on = v;
        }
      }
      return have ? (on ? 1 : 0) : -1;
    };
    w.run(60000);
    int haStates = haView(B[1], 0) == (w.nodes[B[1]]->relayOut[0] ? 1 : 0) && haView(A[5], 1) == (w.nodes[A[5]]->relayOut[1] ? 1 : 0) ? 2 : 0;
    // HA schakelt als virtuele knop
    ha.node->sendCmdLight(w.nodes[B[1]]->flash.id, 0, Mode::Off);
    w.run(500);
    bool ok3 = !w.nodes[B[1]]->relayOut[0];
    std::printf("  LAN-verlies %2.0f%%: huis->poolhuis %s (%u ms), LED %s, poolhuis->huis %s, HA-beeld klopt: %s, HA-commando %s\n", lanLoss, ok1 ? "ok" : "MIS", lat, led ? "ok" : "MIS", ok2 ? "ok" : "MIS", haStates == 2 ? "ja" : "NEE", ok3 ? "ok" : "MIS");
    CHECK(ok1 && led && ok2 && ok3 && haStates == 2);
    // beacons/probes lekken niet over de LAN: elk eiland houdt zijn eigen kanaal en epoch
    w.run(quick ? 60000 : 300000);
    bool stay = true;
    for (int i : A) stay = stay && w.nodes[i]->chan == 6 && w.nodes[i]->cfg.epoch == 1;
    for (int i : B) stay = stay && w.nodes[i]->chan == 11 && w.nodes[i]->cfg.epoch == 1;
    CHECKM(stay, "een eiland is van kanaal veranderd door verkeer van het andere");
  }
}

// ---- chaos: willekeurige drukken, gelijktijdige drukken, herstarts, 15% verlies; per kring moet de pariteit kloppen

void scenarioChaos(bool quick) {
  std::printf("[scen] chaos: gelijktijdige drukken + herstarts + 15%% verlies\n");
  sim::World w(700);
  House h = buildHouse(w, 24, 31, false);
  w.lossPct = 15;
  w.startAll();
  w.run(5000);
  sim::Rng rng(9);
  std::vector<int> expected(h.circuits.size(), 0);
  std::vector<bool> initial;
  std::vector<size_t> logs;
  for (auto& c : h.circuits) {
    initial.push_back(w.nodes[c.owner]->relayOut[c.relay]);
    logs.push_back(w.nodes[c.owner]->relayLog[c.relay].size());
  }
  std::map<std::pair<int, int>, uint32_t> last;
  auto press = [&](size_t ci, size_t si) {
    auto s2 = h.circuits[ci].sw[si];
    auto it = last.find(s2);
    if (it != last.end() && w.now - it->second < 400) return false;  // binnen de debounce
    last[s2] = w.now;
    w.nodes[s2.first]->node->button(uint8_t(s2.second), true);
    expected[ci]++;
    return true;
  };
  int n = quick ? 150 : 500, reboots = 0, concurrent = 0;
  for (int i = 0; i < n; i++) {
    size_t ci = rng.next() % h.circuits.size();
    size_t si = rng.next() % h.circuits[ci].sw.size();
    press(ci, si);
    if (rng.uni() < 0.3 && h.circuits[ci].sw.size() > 1) {  // een tweede schakelaar in dezelfde kring, binnen 8 ms
      w.run(rng.range(0, 8));
      size_t sj = (si + 1 + rng.next() % (h.circuits[ci].sw.size() - 1)) % h.circuits[ci].sw.size();
      if (press(ci, sj)) concurrent++;
    }
    w.run(rng.range(100, 1500));
    if (i % 25 == 24) {  // een willekeurige node valt even weg (eerst laten uitlopen: lopende herhalingen duren ~1 s)
      w.run(2000);
      int victim = h.firstNode + int(rng.next() % 24);
      w.nodes[victim]->powered = false;
      w.run(1500);
      w.nodes[victim]->powered = true;
      w.reboot(victim);
      w.run(500);
      reboots++;
    }
  }
  w.run(120000);
  int wrong = 0;
  for (size_t c = 0; c < h.circuits.size(); c++) {
    auto& o = *w.nodes[h.circuits[c].owner];
    auto& lg = o.relayLog[h.circuits[c].relay];
    int toggles = 0;
    for (size_t i = std::max<size_t>(logs[c], 1); i < lg.size(); i++)
      if (lg[i].second != lg[i - 1].second) toggles++;
    bool state = o.relayOut[h.circuits[c].relay];
    if (state != (initial[c] ^ bool(expected[c] & 1))) {
      wrong++;
      std::printf("    kring %zu (owner %d, schakelaars %zu): verwacht %d toggles, kreeg %d\n", c, h.circuits[c].owner, h.circuits[c].sw.size(), expected[c], toggles);
    }
  }
  // indicatoren moeten na de rust kloppen
  int ledBad = 0, ledTotal = 0;
  for (auto& c : h.circuits)
    for (auto& s2 : c.sw) {
      ledTotal++;
      if (w.nodes[s2.first]->indOut[s2.second] != w.nodes[c.owner]->relayOut[c.relay]) ledBad++;
    }
  std::printf("  %d drukken (%d gelijktijdig), %d herstarts: %d/%zu kringen met verkeerde eindstand, %d/%d LED's fout\n", n, concurrent, reboots, wrong, h.circuits.size(), ledBad, ledTotal);
  CHECKM(wrong == 0, "%d kringen eindigen in de verkeerde stand (gemiste of dubbele toggles)", wrong);
  CHECKM(ledBad == 0, "%d LED's blijven fout", ledBad);
}

}  // namespace

// Parameter-sweep om de flood af te stellen: kzt kktest sweep
void runSweep() {
  struct P {
    const char* name;
    uint8_t copies;
    uint16_t gmin, gmax, jmin, jmax;
    uint8_t sup, ack;
  } ps[] = {
      {"K2, geen ack (oud)", 3, 5, 14, 2, 15, 2, 0},
      {"K2 + ack", 3, 5, 14, 2, 15, 2, 4},
      {"K3 + ack", 3, 5, 14, 2, 15, 3, 4},
      {"K3 + ack, jitter 4-30, copies 10-40", 3, 10, 40, 4, 30, 3, 4},
      {"K3, geen ack, jitter 4-30, copies 10-40", 3, 10, 40, 4, 30, 3, 0},
      {"K2 + ack, 2 copies", 2, 5, 14, 2, 15, 2, 4},
      {"K3 + ack, 2 copies", 2, 5, 14, 2, 15, 3, 4},
      {"K3 + ack, 1 copy", 1, 5, 14, 2, 15, 3, 4},
  };
  std::printf("%-52s %-9s %-9s %-9s %-9s\n", "variant (24 nodes, 3 seeds x 150 drukken)", "ok% L0", "ok% L10", "ok% L25", "tx/press");
  for (auto& p : ps) {
    double ok[3] = {0, 0, 0}, tx = 0;
    int li = 0;
    for (double loss : {0.0, 10.0, 25.0}) {
      int tot = 0, good = 0;
      for (int seed = 1; seed <= 3; seed++) {
        sim::World w(900 + seed);
        w.tuning.eventCopies = p.copies;
        w.tuning.eventGapMin = p.gmin;
        w.tuning.eventGapMax = p.gmax;
        w.tuning.fwdJitterMin = p.jmin;
        w.tuning.fwdJitterMax = p.jmax;
        w.tuning.fwdSuppress = p.sup;
        w.tuning.ackRetries = p.ack;
        House h = buildHouse(w, 24, 20 + seed, false);
        w.lossPct = loss;
        w.startAll();
        w.run(5000);
        sim::Rng rng(seed);
        PressStats st = runPresses(w, h, 150, rng);
        tot += st.presses;
        good += st.ok;
        if (loss == 10) tx += st.txPerPress / 3;
      }
      ok[li++] = 100.0 * good / tot;
    }
    std::printf("%-52s %-9.1f %-9.1f %-9.1f %-9.1f\n", p.name, ok[0], ok[1], ok[2], tx);
  }
}

// Slaagkans per enkele poging (geen ack, 1 kopie) voor de slechtste (switch, eigenaar)-paren: kzt kktest probe
void runProbe() {
  struct V {
    const char* name;
    uint8_t sup;
    uint16_t jmin, jmax;
    uint8_t ttl;
  } vs[] = {{"K2 jit 2-15", 2, 2, 15, 4}, {"K3 jit 2-15", 3, 2, 15, 4}, {"K9 jit 2-15", 9, 2, 15, 4}, {"K2 jit 5-40", 2, 5, 40, 4}, {"K9 jit 5-40", 9, 5, 40, 4}};
  std::printf("%-16s %-34s %s\n", "variant", "slechtste 3 paren: succes/200 (hops)", "gem. over alle paren");
  for (auto& v : vs) {
    sim::World w(100);
    w.tuning.eventCopies = 1;
    w.tuning.ackRetries = 0;
    w.tuning.fwdSuppress = v.sup;
    w.tuning.fwdJitterMin = v.jmin;
    w.tuning.fwdJitterMax = v.jmax;
    House h = buildHouse(w, 24, 5, false);
    w.startAll();
    w.run(5000);
    struct R {
      double rate;
      int hops, sw, owner;
    };
    std::vector<R> rs;
    for (auto& c : h.circuits)
      for (auto& s : c.sw) {
        if (s.first == c.owner) continue;
        auto& owner = *w.nodes[c.owner];
        int ok = 0, N = 40;
        for (int i = 0; i < N; i++) {
          size_t before = owner.relayLog[c.relay].size();
          w.nodes[s.first]->node->button(uint8_t(s.second), true);
          w.run(400);
          if (owner.relayLog[c.relay].size() > before) ok++;
          w.run(200);
        }
        rs.push_back({double(ok) / N, w.hops(s.first, c.owner), s.first, c.owner});
      }
    std::sort(rs.begin(), rs.end(), [](const R& a, const R& b) { return a.rate < b.rate; });
    double avg = 0;
    for (auto& r : rs) avg += r.rate;
    avg /= rs.size();
    std::printf("%-16s %3.0f%%(%dh) %3.0f%%(%dh) %3.0f%%(%dh)                 %.1f%%\n", v.name, rs[0].rate * 100, rs[0].hops, rs[1].rate * 100, rs[1].hops, rs[2].rate * 100, rs[2].hops, avg * 100);
  }
}

void runScenarios(bool quick) {
  scenarioReliability(quick);
  scenarioIndicators(quick);
  scenarioHops();
  scenarioIdleTraffic();
  scenarioChannel(quick);
  scenarioIslands(quick);
  scenarioChaos(quick);
}
