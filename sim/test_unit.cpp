// Unit-tests: crypto/frame, replay-venster, lichtlogica op een ideaal radiomedium.
#include "check.h"
#include "kk/cfghash.h"
#include "sim.h"

using namespace kk;

// Het eerste logitem is de herstelling bij begin(): tel enkel echte wijzigingen.
static size_t nchg(const sim::SimNode& n, int relay) { return n.relayLog[relay].size() - 1; }

static Key testKey() {
  Key k{};
  for (int i = 0; i < 32; i++) k.b[i] = uint8_t(i + 1);
  return k;
}

static void testFrame() {
  std::printf("[unit] frame\n");
  Key k = testKey(), other = testKey();
  other.b[5] ^= 1;
  Frame f{};
  f.type = Type::BtnEvent;
  f.ttl = 4;
  f.id = {7, 3, 99};
  f.len = 2;
  f.payload[0] = 1;
  f.payload[1] = 0;
  uint8_t buf[MAX_FRAME];
  size_t n = seal(k, f, buf);
  CHECK(n == HDR + 2 + TAG);
  Frame g{};
  CHECK(open(k, buf, n, g));
  CHECK(g.type == Type::BtnEvent && g.id.src == 7 && g.id.boot == 3 && g.id.ctr == 99 && g.len == 2 && g.payload[0] == 1);
  CHECK(!open(other, buf, n, g));  // verkeerde sleutel
  uint8_t t[MAX_FRAME];
  memcpy(t, buf, n);
  t[TTL_OFFSET] = 1;  // ttl is muteerbaar
  CHECK(open(k, t, n, g) && g.ttl == 1);
  memcpy(t, buf, n);
  t[3] ^= 1;  // src
  CHECK(!open(k, t, n, g));
  memcpy(t, buf, n);
  t[HDR] ^= 1;  // ciphertext
  CHECK(!open(k, t, n, g));
  memcpy(t, buf, n);
  t[n - 1] ^= 1;  // tag
  CHECK(!open(k, t, n, g));
  CHECK(!open(k, buf, HDR + TAG - 1, g));  // te kort
  // maximale payload past
  Frame big{};
  big.type = Type::Heartbeat;
  big.id = {1, 1, 1};
  big.len = uint8_t(MAX_PAYLOAD);
  CHECK(seal(k, big, buf) == MAX_FRAME);
  CHECK(open(k, buf, MAX_FRAME, g) && g.len == MAX_PAYLOAD);
}

static void testReplay() {
  std::printf("[unit] replay\n");
  ReplayTable r;
  CHECK(r.accept(1, 100, 0));
  CHECK(!r.accept(1, 100, 0));  // duplicaat
  CHECK(r.accept(1, 101, 0));
  CHECK(r.accept(1, 99, 0));  // uit volgorde, nog in venster
  CHECK(!r.accept(1, 99, 0));
  CHECK(r.accept(1, 200, 0));
  CHECK(!r.accept(1, 100, 0));  // te oud (>=64 achter)
  CHECK(r.accept(1, 150, 0));   // in venster, nog niet gezien
  CHECK(r.accept(2, 100, 0));   // andere bron
  // boot-overgang: key = boot<<16|ctr
  CHECK(r.accept(3, (5u << 16) | 65000, 0));
  CHECK(r.accept(3, (6u << 16) | 1, 0));
  CHECK(!r.accept(3, (5u << 16) | 65001, 0));
  // wrap rond 2^32
  CHECK(r.accept(4, 0xFFFFFFF0u, 0));
  CHECK(r.accept(4, 0x00000005u, 0));
  CHECK(!r.accept(4, 0xFFFFFFF0u, 0));
  // tabel vol: de oudste wordt verdrongen
  ReplayTable big;
  for (uint32_t s = 1; s <= REPLAY_SOURCES + 5; s++) CHECK(big.accept(NodeId(s), 1, s));
  CHECK(big.accept(NodeId(REPLAY_SOURCES + 5), 2, 1000));
}

// Twee, drie nodes dicht bij elkaar, ideaal medium.
static void testBasic() {
  std::printf("[unit] basic light logic\n");
  sim::World w(11);
  sim::Profile P{3, 3, 1};
  auto& A = w.add(P, 0, 0);
  auto& B = w.add(P, 2, 0);
  auto& C = w.add(P, 4, 0);
  w.link(1, 0, 0, 0);  // knop A0 -> relais B0
  w.link(1, 0, 2, 1);  // knop C1 -> relais B0
  w.link(0, 1, 0, 2);  // lokaal: knop A2 -> relais A1
  A.flash.follow[0] = {B.cfg.id, 0, 1};  // LED van A volgt licht B0
  w.startAll();
  w.run(200);
  CHECK(!B.relayOut[0]);

  A.node->button(0, true);
  w.run(100);
  CHECK(B.relayOut[0]);
  CHECK(A.indOut[0]);  // terugkoppeling op de touch van A
  w.run(300);
  C.node->button(1, true);  // andere schakelaar in dezelfde kring
  w.run(100);
  CHECK(!B.relayOut[0]);
  CHECK(!A.indOut[0]);
  CHECK(nchg(B, 0) == 2);  // precies twee wijzigingen: geen dubbele toggles

  // debounce: twee drukken in 60 ms = één toggle
  w.run(300);
  A.node->button(0, true);
  w.run(60);
  A.node->button(0, true);
  w.run(200);
  CHECK(B.relayOut[0] && nchg(B, 0) == 3);

  // lokaal en direct: zelfde ms, zonder netwerk
  uint32_t t0 = w.now;
  A.node->button(2, true);
  CHECK(A.relayOut[1]);
  CHECK(A.relayLog[1].back().first == t0);

  // persistentie: na herstart staat het relais direct goed (nog vóór er een tick is geweest)
  CHECK(B.relayOut[0]);
  B.relayOut[0] = false;  // hardware valt weg bij stroomuitval
  w.reboot(1);
  CHECK(B.relayOut[0]);
  w.run(200);
  // na herstart zijn eigen frames gedupliceerd? boot is verhoogd: nieuwe frames worden aanvaard
  A.node->button(0, true);
  w.run(100);
  CHECK(!B.relayOut[0]);
  (void)C;
}

static void testLearn() {
  std::printf("[unit] learn mode + commands\n");
  sim::World w(12);
  sim::Profile P{3, 3, 0};
  auto& A = w.add(P, 0, 0);
  auto& B = w.add(P, 2, 0);
  auto& C = w.add(P, 4, 0);
  w.link(1, 0, 0, 0);
  w.startAll();
  w.run(100);
  C.node->sendLearn(true);
  w.run(100);
  CHECK(B.node->learning());
  A.node->button(0, true);
  w.run(100);
  CHECK(!B.relayOut[0]);  // niets geschakeld tijdens het leren
  C.node->sendLearn(false);
  w.run(300);
  A.node->button(0, true);
  w.run(100);
  CHECK(B.relayOut[0]);

  // CmdLight (virtuele knop vanuit HA / bridge)
  C.node->sendCmdLight(B.cfg.id, 0, Mode::Off);
  w.run(100);
  CHECK(!B.relayOut[0]);
  C.node->sendCmdLight(B.cfg.id, 0, Mode::On);
  w.run(100);
  CHECK(B.relayOut[0]);
  size_t n = nchg(B, 0);
  C.node->sendCmdLight(B.cfg.id, 0, Mode::On);  // al aan: geen wijziging
  w.run(100);
  CHECK(nchg(B, 0) == n);

  // configuratie op afstand: knop A1 gaat relais B2 bedienen
  Trigger t{A.cfg.id, 1, uint8_t(Mode::Toggle)};
  C.node->sendCfgTriggers(B.cfg.id, 2, &t, 1);
  w.run(150);
  CHECK(B.flash.relay[2].ntrig == 1);
  A.node->button(1, true);
  w.run(100);
  CHECK(B.relayOut[2]);
}

static void testTamper() {
  std::printf("[unit] vreemde zenders\n");
  sim::World w(13);
  sim::Profile P{3, 3, 0};
  auto& A = w.add(P, 0, 0);
  auto& B = w.add(P, 2, 0);
  w.link(1, 0, 0, 0);
  w.startAll();
  Frame f{};
  f.type = Type::BtnEvent;
  f.ttl = 4;
  f.id = {A.cfg.id, 9999, 1};
  f.len = 3;
  f.payload[0] = 0;  // knop 0, press, evt 1
  f.payload[1] = 0;
  f.payload[2] = 1;
  Key wrong = w.key;
  wrong.b[0] ^= 0xFF;
  uint8_t buf[MAX_FRAME];
  size_t n = seal(wrong, f, buf);
  B.node->onRadio(buf, n);
  w.run(50);
  CHECK(!B.relayOut[0]);
  CHECK(B.node->stats.badAuth == 1);
  n = seal(w.key, f, buf);
  B.node->onRadio(buf, n);  // met de juiste sleutel wel
  B.node->onRadio(buf, n);  // replay
  w.run(50);
  CHECK(B.relayOut[0] && nchg(B, 0) == 1);
}

// De heartbeat meldt wat de node is (knoppen, indicatoren) en een vingerafdruk van zijn configuratie.
static void testHeartbeatInfo() {
  std::printf("[unit] heartbeat info + config-hash\n");
  sim::World w(14);
  sim::Profile P{3, 3, 2};
  auto& A = w.add(P, 0, 0);
  auto& Br = w.add({1, 1, 0}, 3, 0, 0, CF_BRIDGE);
  auto& H = w.addHa();
  w.link(0, 1, 0, 0);
  A.flash.follow[0] = {A.cfg.id, 1, 1};
  w.startAll();
  w.run(7000);
  const Frame* hb = nullptr;
  for (auto& p : H.seen)
    if (p.second.type == Type::Heartbeat && p.second.id.src == A.cfg.id) hb = &p.second;
  CHECK(hb != nullptr);
  if (!hb) return;
  Reader r(hb->payload, hb->len);
  r.u16();
  r.u8();
  uint8_t nrel = r.u8();
  CHECK(nrel == 3);
  for (int i = 0; i < nrel; i++) {
    r.u8();
    r.u16();
  }
  uint8_t nbtn = r.u8(), nind = r.u8();
  uint16_t h = r.u16();
  CHECK(!r.bad && r.n == hb->len);
  CHECK(nbtn == 3 && nind == 2);
  CHECK(h == configHash(A.cfg));
  // elke wijziging verandert de hash
  uint16_t h0 = configHash(A.cfg);
  Trigger t{Br.cfg.id, 0, uint8_t(Mode::Toggle)};
  H.node->sendCfgTriggers(A.cfg.id, 2, &t, 1);
  w.run(300);
  CHECK(A.cfg.relay[2].ntrig == 1);
  uint16_t h1 = configHash(A.cfg);
  CHECK(h1 != h0);
  H.node->sendCfgFollow(A.cfg.id, 1, Br.cfg.id, 0);
  w.run(300);
  CHECK(configHash(A.cfg) != h1);
  // zelfde inhoud op een andere plek geeft een andere hash (volgorde en positie tellen mee)
  Config a{}, b{};
  a.nrelays = b.nrelays = 2;
  a.relay[0].ntrig = 1;
  a.relay[0].trig[0] = {5, 1, 0};
  b.relay[1].ntrig = 1;
  b.relay[1].trig[0] = {5, 1, 0};
  CHECK(configHash(a) != configHash(b));
}

void runUnit() {
  testFrame();
  testReplay();
  testBasic();
  testLearn();
  testTamper();
  testHeartbeatInfo();
}
