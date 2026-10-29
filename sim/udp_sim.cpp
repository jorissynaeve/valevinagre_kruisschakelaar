// De simulator in real time, met een bridge die echte UDP spreekt (alleen Linux/docker).
// Zo praat de echte add-on met de echte kern: frames, flood, heartbeats, configuratie en lichtlogica.
//
//   KK_BCAST   broadcast-adres voor de LAN-link     (standaard 255.255.255.255)
//   KK_PEERS   komma-gescheiden unicast-bestemmingen (host of host:poort), naast broadcast
//   KK_PORT    UDP-poort van de LAN-link             (standaard 47474)
//   KK_CTL     poort van de besturing                (standaard 47475)
//   KK_LOSS    extra radioverlies in procent         (standaard 0)
//   KK_HB_FAST heartbeat-interval delen door dit getal (standaard 1; de e2e-test gebruikt 10)
//
// Besturing (een tekstregel per datagram, antwoord naar de afzender):
//   press <id> <knop>     fysieke knop indrukken
//   state <id>            relays=0101 ind=010 learning=0 trig=2,0,0 follow=2.0,-,-   (de feitelijke stand van de node)
//   power <id> off|on     stroomuitval en herstel (relaisstaat komt uit flash terug)
//   wipe <id>             configuratie van de node wissen (alsof hij opnieuw geflasht is)
//   loss <procent>        radioverlies aanpassen
//   quit
#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <sys/socket.h>
#include <unistd.h>

#include <chrono>
#include <csignal>
#include <cstdlib>
#include <sstream>
#include <string>
#include <vector>

#include "sim.h"

using namespace kk;

static const char* envs(const char* k, const char* d) {
  const char* v = std::getenv(k);
  return v && *v ? v : d;
}

static int udpSocket(int port, bool broadcast) {
  int s = socket(AF_INET, SOCK_DGRAM, 0);
  int one = 1;
  setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
  if (broadcast) setsockopt(s, SOL_SOCKET, SO_BROADCAST, &one, sizeof one);
  sockaddr_in a{};
  a.sin_family = AF_INET;
  a.sin_addr.s_addr = htonl(INADDR_ANY);
  a.sin_port = htons(uint16_t(port));
  if (bind(s, reinterpret_cast<sockaddr*>(&a), sizeof a) < 0) {
    std::perror("bind");
    std::exit(1);
  }
  fcntl(s, F_SETFL, fcntl(s, F_GETFL) | O_NONBLOCK);
  return s;
}

static bool resolve(const std::string& spec, int defPort, sockaddr_in& out) {
  std::string host = spec;
  int port = defPort;
  auto c = spec.find(':');
  if (c != std::string::npos) {
    host = spec.substr(0, c);
    port = std::atoi(spec.c_str() + c + 1);
  }
  addrinfo hints{}, *res = nullptr;
  hints.ai_family = AF_INET;
  hints.ai_socktype = SOCK_DGRAM;
  if (getaddrinfo(host.c_str(), nullptr, &hints, &res) != 0 || !res) return false;
  out = *reinterpret_cast<sockaddr_in*>(res->ai_addr);
  out.sin_port = htons(uint16_t(port));
  freeaddrinfo(res);
  return true;
}

static volatile std::sig_atomic_t g_quit = 0;

int main() {
  const int port = std::atoi(envs("KK_PORT", "47474"));
  const int ctlPort = std::atoi(envs("KK_CTL", "47475"));
  const int fast = std::max(1, std::atoi(envs("KK_HB_FAST", "1")));
  std::signal(SIGTERM, [](int) { g_quit = 1; });
  std::signal(SIGINT, [](int) { g_quit = 1; });

  sim::World w(std::atoll(envs("KK_SEED", "1")));
  w.lossPct = std::atof(envs("KK_LOSS", "0"));
  w.tuning.hbMin /= fast;
  w.tuning.hbMax /= fast;
  w.tuning.hbFastMin = std::max<uint32_t>(600, w.tuning.hbFastMin / fast);
  w.tuning.hbFastMax = std::max<uint32_t>(1200, w.tuning.hbFastMax / fast);

  // Een huisje: drie touches (3 knoppen, 3 relais, 3 indicatoren), een relaisbox, een grote relaisbox en de bridge.
  // Node-id's zijn index+1. Alles start zonder configuratie: de add-on moet de lichten leren en versturen.
  sim::Profile Touch{3, 3, 3}, Box4{0, 4, 0}, Box16{0, 16, 0}, Bridge{1, 1, 0};
  w.add(Touch, 0, 0);                       // 1 hal
  w.add(Box4, 6, 0);                        // 2 relaisbox living
  w.add(Touch, 12, 6);                      // 3 keuken
  w.add(Bridge, 8, 3, 0, CF_BRIDGE);        // 4 bridge
  w.add(Touch, 14, 0, 1);                   // 5 slaapkamer (verdieping)
  w.add(Box16, 20, 4);                      // 6 grote relaisbox
  for (auto& n : w.nodes) n->cfg.fw = n->flash.fw = 0x0101;
  w.startAll();

  int sock = udpSocket(port, true);
  int ctl = udpSocket(ctlPort, false);
  std::vector<sockaddr_in> dests;
  sockaddr_in b{};
  if (resolve(envs("KK_BCAST", "255.255.255.255"), port, b)) dests.push_back(b);
  if (const char* p = std::getenv("KK_PEERS")) {
    std::stringstream ss(p);
    std::string item;
    while (std::getline(ss, item, ','))
      if (!item.empty()) {
        sockaddr_in a{};
        if (resolve(item, port, a))
          dests.push_back(a);
        else
          std::fprintf(stderr, "peer %s niet gevonden\n", item.c_str());
      }
  }
  w.extTx = [&](const uint8_t* d, size_t n) {
    for (auto& a : dests) sendto(sock, d, n, 0, reinterpret_cast<const sockaddr*>(&a), sizeof a);
  };
  std::printf("kkudp: %zu nodes, UDP :%d (%zu bestemmingen), besturing :%d, heartbeat /%d\n", w.nodes.size(), port, dests.size(), ctlPort, fast);
  std::fflush(stdout);

  std::vector<std::vector<size_t>> seenRelay(w.nodes.size()), seenInd(w.nodes.size());
  for (size_t i = 0; i < w.nodes.size(); i++) {
    seenRelay[i].assign(kk::MAX_RELAYS, 1);
    seenInd[i].assign(kk::MAX_IND, 1);
  }

  auto reply = [&](const sockaddr_in& to, const std::string& s) { sendto(ctl, s.data(), s.size(), 0, reinterpret_cast<const sockaddr*>(&to), sizeof to); };
  auto bits = [](const bool* v, int n) {
    std::string s;
    for (int i = 0; i < n; i++) s += v[i] ? '1' : '0';
    return s;
  };

  auto command = [&](const std::string& line, const sockaddr_in& from) {
    std::istringstream in(line);
    std::string cmd;
    in >> cmd;
    int id = 0, arg = 0;
    std::string word;
    if (cmd == "quit") {
      g_quit = 1;
      return reply(from, "bye");
    }
    if (cmd == "loss") {
      double p = 0;
      in >> p;
      w.lossPct = p;
      return reply(from, "ok");
    }
    in >> id;
    if (id < 1 || id > int(w.nodes.size())) return reply(from, "err: onbekende node");
    auto& n = *w.nodes[id - 1];
    if (cmd == "press") {
      in >> arg;
      if (!n.powered) return reply(from, "err: node heeft geen stroom");
      n.node->button(uint8_t(arg), true);
      return reply(from, "ok");
    }
    if (cmd == "state") {
      std::ostringstream o;
      o << "relays=" << bits(n.relayOut, n.cfg.nrelays) << " ind=" << bits(n.indOut, n.cfg.nind) << " learning=" << (n.node && n.node->learning() ? 1 : 0) << " trig=";
      for (int i = 0; i < n.cfg.nrelays; i++) o << (i ? "," : "") << int(n.cfg.relay[i].ntrig);
      o << " follow=";
      for (int i = 0; i < n.cfg.nind; i++) {
        if (i) o << ",";
        if (n.cfg.follow[i].valid)
          o << n.cfg.follow[i].node << "." << int(n.cfg.follow[i].relay);
        else
          o << "-";
      }
      o << " powered=" << (n.powered ? 1 : 0);
      return reply(from, o.str());
    }
    if (cmd == "power") {
      in >> word;
      if (word == "off") {
        n.powered = false;
        n.txq.clear();
        for (int i = 0; i < kk::MAX_RELAYS; i++) n.relayOut[i] = false;  // zonder stroom valt de hardware weg
        for (int i = 0; i < kk::MAX_IND; i++) n.indOut[i] = false;
      } else {
        n.powered = true;
        w.reboot(id - 1);
      }
      return reply(from, "ok");
    }
    if (cmd == "wipe") {
      for (int i = 0; i < kk::MAX_RELAYS; i++) n.cfg.relay[i].ntrig = 0;
      for (int i = 0; i < kk::MAX_IND; i++) n.cfg.follow[i] = {};
      n.flash = n.cfg;
      return reply(from, "ok");
    }
    reply(from, "err: onbekend commando");
  };

  auto t0 = std::chrono::steady_clock::now();
  uint8_t buf[2048];
  while (!g_quit) {
    uint32_t target = uint32_t(std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - t0).count());
    for (int steps = 0; w.now < target && steps < 100; steps++) w.step();

    for (;;) {
      ssize_t r = recv(sock, buf, sizeof buf, 0);
      if (r <= 0) break;
      w.extRx(buf, size_t(r));
    }
    for (;;) {
      sockaddr_in from{};
      socklen_t fl = sizeof from;
      ssize_t r = recvfrom(ctl, buf, sizeof buf - 1, 0, reinterpret_cast<sockaddr*>(&from), &fl);
      if (r <= 0) break;
      buf[r] = 0;
      std::string line(reinterpret_cast<char*>(buf));
      while (!line.empty() && (line.back() == '\n' || line.back() == '\r')) line.pop_back();
      command(line, from);
    }
    // wijzigingen van de hardware melden (handig bij het lezen van de logs)
    for (size_t i = 0; i < w.nodes.size(); i++) {
      auto& n = *w.nodes[i];
      for (int r = 0; r < n.cfg.nrelays; r++)
        if (n.relayLog[r].size() != seenRelay[i][r]) {
          seenRelay[i][r] = n.relayLog[r].size();
          std::printf("[%7.3f] node %zu relais %d -> %s\n", w.now / 1000.0, i + 1, r, n.relayOut[r] ? "aan" : "uit");
        }
      for (int k = 0; k < n.cfg.nind; k++)
        if (n.indLog[k].size() != seenInd[i][k]) {
          seenInd[i][k] = n.indLog[k].size();
          std::printf("[%7.3f] node %zu indicator %d -> %s\n", w.now / 1000.0, i + 1, k, n.indOut[k] ? "aan" : "uit");
        }
    }
    std::fflush(stdout);
    usleep(300);
  }
  return 0;
}
