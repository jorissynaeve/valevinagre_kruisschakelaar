// Interop-vectoren tussen de C++-kern en de Python-add-on.
//
//   kkvec gen            schrijft frames die echte nodes uitzenden + hash-gevallen naar stdout
//   kkvec check <file>   controleert wat de add-on (Python) heeft gemaakt: frames openen en hashes narekenen
//
// Regelformaten (witruimte-gescheiden):
//   frame <naam> <hex>                                   (gen)
//   hash  <naam> <hex4> <nrelays> <nind> <spec...>       (gen)
//   pyframe <type> <ttl> <src> <boot> <ctr> <payloadhex|-> <framehex>   (check)
//   pyhash <hex4> <nrelays> <nind> <spec...>                            (check)
// spec: per relais "R<n>" gevolgd door n keer "node,knop,modus"; per indicator "I0" of "I1,node,relais".
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "check.h"
#include "kk/cfghash.h"
#include "sim.h"

using namespace kk;

static kk::Key vecKey() {
  kk::Key k{};
  for (int i = 0; i < 32; i++) k.b[i] = uint8_t(i * 7 + 3);
  return k;
}

static std::string hex(const uint8_t* d, size_t n) {
  static const char* H = "0123456789abcdef";
  std::string s;
  for (size_t i = 0; i < n; i++) {
    s += H[d[i] >> 4];
    s += H[d[i] & 15];
  }
  return s;
}

static std::vector<uint8_t> unhex(const std::string& s) {
  std::vector<uint8_t> v;
  if (s == "-") return v;
  for (size_t i = 0; i + 1 < s.size(); i += 2) v.push_back(uint8_t(std::stoi(s.substr(i, 2), nullptr, 16)));
  return v;
}

static std::string specOf(const Config& c) {
  std::ostringstream o;
  o << int(c.nrelays) << ' ' << int(c.nind);
  for (uint8_t i = 0; i < c.nrelays; i++) {
    o << " R" << int(c.relay[i].ntrig);
    for (uint8_t k = 0; k < c.relay[i].ntrig; k++) o << ' ' << c.relay[i].trig[k].node << ',' << int(c.relay[i].trig[k].btn) << ',' << int(c.relay[i].trig[k].mode);
  }
  for (uint8_t j = 0; j < c.nind; j++) {
    if (c.follow[j].valid)
      o << " I1," << c.follow[j].node << ',' << int(c.follow[j].relay);
    else
      o << " I0";
  }
  return o.str();
}

// Leest "<nrelays> <nind> <spec...>" uit een tokenstroom.
static bool configFromSpec(std::istringstream& in, Config& c) {
  c = Config{};
  int nr, ni;
  if (!(in >> nr >> ni)) return false;
  c.nrelays = uint8_t(nr);
  c.nind = uint8_t(ni);
  for (int i = 0; i < nr; i++) {
    std::string t;
    if (!(in >> t) || t[0] != 'R') return false;
    int n = std::atoi(t.c_str() + 1);
    c.relay[i].ntrig = uint8_t(n);
    for (int k = 0; k < n; k++) {
      std::string x;
      in >> x;
      unsigned a, b, m;
      if (std::sscanf(x.c_str(), "%u,%u,%u", &a, &b, &m) != 3) return false;
      c.relay[i].trig[k] = {NodeId(a), uint8_t(b), uint8_t(m)};
    }
  }
  for (int j = 0; j < ni; j++) {
    std::string t;
    if (!(in >> t) || t[0] != 'I') return false;
    if (t[1] == '1') {
      unsigned a, b;
      if (std::sscanf(t.c_str(), "I1,%u,%u", &a, &b) != 2) return false;
      c.follow[j] = {NodeId(a), uint8_t(b), 1};
    }
  }
  return true;
}

static void printFrame(const char* name, const Key& k, const Frame& f) {
  uint8_t buf[MAX_FRAME];
  size_t n = seal(k, f, buf);
  std::printf("frame %s %s\n", name, hex(buf, n).c_str());
}

static void gen() {
  sim::World w(5);
  w.key = vecKey();
  sim::Profile T3p{3, 3, 3}, Boxp{0, 4, 0}, Brp{1, 1, 0};
  auto& A = w.add(T3p, 0, 0);               // id 1: touch met 3 knoppen
  auto& B = w.add(Boxp, 3, 0);              // id 2: relaisbox
  auto& Br = w.add(Brp, 6, 0, 0, CF_BRIDGE);  // id 3: bridge
  auto& H = w.addHa();                      // id 0xF000: peer
  Br.record = true;
  B.record = true;
  A.cfg.fw = 0x0102;
  B.cfg.fw = 0x0102;
  A.flash = A.cfg;
  B.flash = B.cfg;
  w.link(1, 2, 0, 1, Mode::Toggle);  // A.knop1 -> B.relais2
  w.link(1, 2, 0, 0, Mode::On);      // A.knop0 -> B.relais2 (aan)
  w.link(1, 0, 2, 0, Mode::Off);     // Br.knop0 -> B.relais0 (uit)
  A.flash.follow[1] = {B.cfg.id, 2, 1};  // A.indicator1 volgt B.relais2
  w.startAll();
  w.run(7000);  // elke node heeft een heartbeat gestuurd

  A.node->button(1, true);
  w.run(300);
  H.node->sendCfgTriggers(B.cfg.id, 0, std::vector<Trigger>{{A.cfg.id, 2, 0}, {Br.cfg.id, 0, 1}}.data(), 2);
  w.run(300);
  H.node->sendCfgFollow(A.cfg.id, 2, B.cfg.id, 0);
  w.run(300);
  H.node->sendCmdLight(B.cfg.id, 1, Mode::On);
  w.run(300);
  H.node->sendLearn(true);
  w.run(300);

  auto pick = [&](const std::vector<std::pair<uint32_t, Frame>>& v, Type t, NodeId src, uint8_t b0, bool useB0, const char* name) {
    for (auto& p : v)
      if (p.second.type == t && p.second.id.src == src && (!useB0 || (p.second.len > 0 && p.second.payload[0] == b0))) {
        printFrame(name, w.key, p.second);
        return;
      }
    std::fprintf(stderr, "vector %s niet gevonden\n", name);
    std::exit(2);
  };
  // wat H hoorde (via de bridge): heartbeats, knop, lichtstand, ack
  pick(H.seen, Type::Heartbeat, A.cfg.id, 0, false, "hb_A");
  pick(H.seen, Type::Heartbeat, B.cfg.id, 0, false, "hb_B");
  pick(H.seen, Type::BtnEvent, A.cfg.id, 1, true, "btn_A1");
  pick(H.seen, Type::LightState, B.cfg.id, 2, true, "light_B2");
  pick(H.seen, Type::CfgAck, B.cfg.id, 0, false, "cfgack_B");
  pick(H.seen, Type::CfgAck, A.cfg.id, 0, false, "cfgack_A");
  // wat de peer zelf stuurde (gezien door de bridge): opdrachten
  pick(Br.seen, Type::CmdLight, H.cfg.id, 0, false, "cmdlight");
  pick(Br.seen, Type::Learn, H.cfg.id, 0, false, "learn");
  // eerste CfgSet van H = triggers naar B, tweede = follow naar A
  int seenCfg = 0;
  for (auto& p : Br.seen)
    if (p.second.type == Type::CfgSet && p.second.id.src == H.cfg.id) {
      ++seenCfg;
      if (seenCfg == 1) printFrame("cfgset_trig", w.key, p.second);
      if (seenCfg == 2) printFrame("cfgset_follow", w.key, p.second);
    }

  // hash-gevallen: leeg, de begin-configuraties en de configuratie na de CFG_SET's
  Config empty{};
  empty.nrelays = 2;
  empty.nind = 1;
  std::printf("hash empty %04x %s\n", configHash(empty), specOf(empty).c_str());
  std::printf("hash B_final %04x %s\n", configHash(B.cfg), specOf(B.cfg).c_str());
  std::printf("hash A_final %04x %s\n", configHash(A.cfg), specOf(A.cfg).c_str());
  Config big{};
  big.nrelays = 16;
  big.nind = 4;
  for (int i = 0; i < 16; i++) {
    big.relay[i].ntrig = uint8_t(i % (MAX_TRIG + 1));
    for (int k = 0; k < big.relay[i].ntrig; k++) big.relay[i].trig[k] = {NodeId(300 * i + k), uint8_t(k % 8), uint8_t(k % 3)};
  }
  for (int j = 0; j < 4; j++)
    if (j != 2) big.follow[j] = {NodeId(4000 + j), uint8_t(j), 1};
  std::printf("hash big %04x %s\n", configHash(big), specOf(big).c_str());
  // heartbeats van A en B horen bij deze configs op het moment van uitzenden: hash van de begin-config
  // (de test leest die uit de heartbeat zelf en vergelijkt met zijn eigen berekening via de spec hieronder)
  Config bInit{};
  bInit.nrelays = 4;
  bInit.nind = 0;
  bInit.relay[2].ntrig = 2;
  bInit.relay[2].trig[0] = {A.cfg.id, 1, uint8_t(Mode::Toggle)};
  bInit.relay[2].trig[1] = {A.cfg.id, 0, uint8_t(Mode::On)};
  bInit.relay[0].ntrig = 1;
  bInit.relay[0].trig[0] = {Br.cfg.id, 0, uint8_t(Mode::Off)};
  std::printf("hash hb_B %04x %s\n", configHash(bInit), specOf(bInit).c_str());
  Config aInit{};
  aInit.nrelays = 3;
  aInit.nind = 3;
  aInit.follow[1] = {B.cfg.id, 2, 1};
  std::printf("hash hb_A %04x %s\n", configHash(aInit), specOf(aInit).c_str());
}

static int check(const char* path) {
  std::ifstream f(path);
  if (!f) {
    std::printf("kan %s niet openen\n", path);
    return 2;
  }
  Key k = vecKey();
  std::string line;
  int frames = 0, hashes = 0;
  while (std::getline(f, line)) {
    std::istringstream in(line);
    std::string kind;
    in >> kind;
    if (kind == "pyframe") {
      unsigned type, ttl, src, boot, ctr;
      std::string pl, fr;
      in >> type >> ttl >> src >> boot >> ctr >> pl >> fr;
      auto raw = unhex(fr);
      auto payload = unhex(pl);
      Frame g{};
      bool ok = open(k, raw.data(), raw.size(), g);
      CHECKM(ok, "frame %u/%u/%u opent niet", type, src, ctr);
      if (!ok) continue;
      CHECK(unsigned(g.type) == type && g.ttl == ttl && g.id.src == src && g.id.boot == boot && g.id.ctr == ctr);
      CHECK(g.len == payload.size() && std::memcmp(g.payload, payload.data(), payload.size()) == 0);
      frames++;
    } else if (kind == "pyhash") {
      std::string h;
      in >> h;
      Config c;
      bool ok = configFromSpec(in, c);
      CHECKM(ok, "spec onleesbaar: %s", line.c_str());
      if (!ok) continue;
      unsigned want = unsigned(std::stoi(h, nullptr, 16));
      CHECKM(configHash(c) == want, "hash %04x != %04x voor %s", configHash(c), want, line.c_str());
      hashes++;
    }
  }
  std::printf("kkvec check: %d frames, %d hashes gelezen\n", frames, hashes);
  CHECK(frames > 0 && hashes > 0);
  std::printf("%d checks ok, %d failed\n", g_pass, g_fail);
  return g_fail ? 1 : 0;
}

int main(int argc, char** argv) {
  if (argc >= 2 && !std::strcmp(argv[1], "gen")) {
    gen();
    return 0;
  }
  if (argc >= 3 && !std::strcmp(argv[1], "check")) return check(argv[2]);
  std::fprintf(stderr, "gebruik: kkvec gen | kkvec check <bestand>\n");
  return 1;
}
