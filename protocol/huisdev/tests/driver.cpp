// Testdriver: draait de echte huisdev.h op een host met een realistisch (poolcontroller-achtig) device
// en laat een Python-test het aansturen via een regelprotocol op stdin/stdout.
//
// Regel = velden gescheiden door TAB. Escapes in een veld: \\ \t \n \r. Antwoord: "OK<TAB>veld..." of "ERR<TAB>tekst".
#include <functional>
#include <memory>
#include <iostream>
#include <sstream>
#include <vector>
#include "Arduino.h"
#include "WiFi.h"
#include "WiFiUdp.h"
#include "Preferences.h"
#include "WebServer.h"
#include "huisdev.h"

static std::unique_ptr<HuisDev> dev;
static WebServer ws;
static std::vector<std::string> g_calls;

static std::string num(double v) {
  char b[64];
  snprintf(b, sizeof b, "%.10g", v);
  return b;
}

static void register_all() {
  auto logn = [](const char* name) {
    return [name](double n, const String& t) {
      g_calls.push_back(std::string(name) + "|" + num(n) + "|" + t.c_str());
    };
  };
  dev->point("ph", "pH", HD_FLOAT).decimals(2).devClass("ph").stateClass("measurement").stale(120000);
  dev->point("orp", "Redox", HD_INT).unit("mV").devClass("voltage").stateClass("measurement");
  dev->point("temp", "Watertemperatuur", HD_FLOAT).unit("°C").decimals(1).devClass("temperature").stateClass("measurement");
  dev->point("salt", "Zout", HD_FLOAT).unit("g/L").decimals(1);
  dev->point("cell_voltage", "Celspanning", HD_FLOAT).unit("V").decimals(1);
  dev->point("mode_electro", "Elektrolyse", HD_ENUM).option(0, "Uit").option(1, "Handmatig").option(2, "Auto").writable(
      [logn](double n, const String& t) {
        logn("mode_electro")(n, t);
        dev->setNum("mode_electro", n);
        return HdResult::applied();
      });
  dev->point("ph_set", "pH instelling", HD_FLOAT).decimals(1).range(6.8, 7.6, 0.1).group("instellingen").writable(
      [logn](double n, const String& t) {
        logn("ph_set")(n, t);
        return HdResult::queued();
      });
  dev->point("prod_set", "Productie instelling", HD_INT).range(10, 990, 10).group("instellingen").writable(
      [logn](double n, const String& t) {
        logn("prod_set")(n, t);
        dev->setNum("prod_set", n);
        return HdResult::applied();
      });
  dev->point("relay", "Relais", HD_BOOL).writable([logn](double n, const String& t) {
    logn("relay")(n, t);
    dev->setBool("relay", n != 0);
    return HdResult::applied();
  });
  dev->point("pump", "Pomp", HD_BOOL).writable([logn](double n, const String& t) {
    logn("pump")(n, t);
    return HdResult::rejected("pomp draait niet");
  });
  // id "value": botst met de sleutel "value" in het verzoek
  dev->point("value", "Waarde", HD_INT).range(0, 100).writable([logn](double n, const String& t) {
    logn("value")(n, t);
    dev->setNum("value", n);
    return HdResult::applied();
  });
  dev->point("pool_name", "Naam", HD_TEXT).writable([logn](double n, const String& t) {
    logn("pool_name")(n, t);
    dev->setText("pool_name", t);
    return HdResult::applied();
  });
  dev->point("alarm_no_flow", "Alarm: geen doorstroming", HD_BOOL).group("alarmen");
  dev->point("calibrate", "Kalibratie", HD_BOOL).danger("Kalibratie \"overschrijft\" instellingen. Doorgaan?").writable(
      [logn](double n, const String& t) {
        logn("calibrate")(n, t);
        return HdResult::applied();
      });
  dev->point("rssi", "WiFi-signaal", HD_INT).unit("dBm").diag();
  dev->point("esc_test", "Zeg \"hi\" \\ terug\nregel\ttab\x01 \xC2\xB0""C \xF0\x9F\x98\x80", HD_TEXT).unit("°C");

  HdAction& boost = dev->action("boost", "Boost starten", [](const String& body) {
    g_calls.push_back("boost|" + std::string(body.c_str()));
    return HdResult::queued();
  });
  boost.arg("minutes", "Duur", HD_INT).unit("min").range(5, 240, 5);
  dev->action("reset", "Fabrieksinstellingen", [](const String& body) {
    g_calls.push_back("reset|" + std::string(body.c_str()));
    return HdResult::applied();
  }).danger("Alle instellingen gaan verloren. Doorgaan?");
  HdAction& cal = dev->action("calibrate_ph", "Kalibreer pH", [](const String& body) {
    g_calls.push_back("calibrate_ph|" + std::string(body.c_str()));
    return HdResult::rejected("sonde niet ondergedompeld");
  });
  cal.arg("buffer", "Buffer", HD_ENUM).option(7, "pH 7").option(10, "pH 10");
  cal.arg("note", "Notitie", HD_TEXT);
  dev->action("flush", "Spoelen", [](const String&) { return HdResult::applied(); });
  dev->actionState("flush", true, "pomp draait niet");
}

static void boot(bool full) {
  dev.reset(new HuisDev());
  ws = WebServer();
  if (full) register_all();
  dev->begin("poolcontroller", "Zwembad", "0.3.1", &ws);
  if (full) {
    dev->setNum("ph", 7.2);
    dev->setNum("orp", 740);
    dev->setBool("alarm_no_flow", false);
    dev->setNum("mode_electro", 2);
    dev->setText("pool_name", "Zwembad");
  }
}

static void factory() {
  g_prefs.clear();
  g_millis = 100000;
  WiFi = WiFiClass();
  g_udp_in.clear();
  g_udp_out.clear();
  g_udp_on = false;
  g_udp_begins = g_udp_stops = 0;
  g_calls.clear();
  boot(true);
}

static std::string unesc(const std::string& s) {
  std::string o;
  for (size_t i = 0; i < s.size(); i++) {
    if (s[i] == '\\' && i + 1 < s.size()) {
      char c = s[++i];
      o += c == 't' ? '\t' : c == 'n' ? '\n' : c == 'r' ? '\r' : c;
    } else {
      o += s[i];
    }
  }
  return o;
}
static std::string esc(const std::string& s) {
  std::string o;
  for (char c : s) {
    if (c == '\\') o += "\\\\";
    else if (c == '\t') o += "\\t";
    else if (c == '\n') o += "\\n";
    else if (c == '\r') o += "\\r";
    else o += c;
  }
  return o;
}
static HdKind kindOf(const std::string& k) {
  if (k == "bool") return HD_BOOL;
  if (k == "int") return HD_INT;
  if (k == "enum") return HD_ENUM;
  if (k == "text") return HD_TEXT;
  return HD_FLOAT;
}
static HdPoint& P(const std::string& id) { return dev->point(id.c_str(), "", HD_FLOAT); }  // alleen voor bestaande punten
static double D(const std::string& s) { return strtod(s.c_str(), nullptr); }

static std::string ok(const std::vector<std::string>& f = {}) {
  std::string o = "OK";
  for (auto& x : f) o += "\t" + esc(x);
  return o;
}

static std::string run(const std::vector<std::string>& a) {
  const std::string& c = a[0];
  auto arg = [&](size_t i) { return i < a.size() ? a[i] : std::string(); };
  if (c == "factory") { factory(); return ok(); }
  if (c == "reboot") { boot(true); return ok(); }
  if (c == "reboot_empty") { boot(false); return ok(); }
  if (c == "rereg") { register_all(); return ok(); }
  if (c == "handle") {
    String out;
    int st = dev->handle(arg(1).c_str(), arg(2).c_str(), arg(4).c_str(), arg(3).c_str(), out);
    return ok({std::to_string(st), out.c_str()});
  }
  if (c == "ws_call") {
    static const char* names[] = {"ANY", "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"};
    HTTPMethod m = HTTP_GET;
    for (int i = 0; i < 8; i++)
      if (arg(1) == names[i]) m = (HTTPMethod)i;
    ws.plain = arg(4).c_str();
    ws.tok = arg(3).c_str();
    ws.code = 0;
    ws.cache = "";
    bool routed = ws.dispatch(m, arg(2));  // false = onNotFound van de sketch
    return ok({std::to_string(ws.code), ws.ctype.c_str(), ws.cache.c_str(), ws.content.c_str(), routed ? "routed" : "notfound"});
  }
  if (c == "ws_info") {
    std::string r, h;
    for (auto& rt : ws.routes) r += std::string(rt.glob ? "glob " : "exact ") + std::to_string((int)rt.method) + " " + rt.pattern + ",";
    for (auto& x : ws.collected) h += x + ",";
    return ok({r, h});
  }
  if (c == "setnum") { dev->setNum(arg(1).c_str(), D(arg(2))); return ok(); }
  if (c == "setbool") { dev->setBool(arg(1).c_str(), arg(2) == "1"); return ok(); }
  if (c == "settext") { dev->setText(arg(1).c_str(), arg(2).c_str()); return ok(); }
  if (c == "setunknown") { dev->setUnknown(arg(1).c_str()); return ok(); }
  if (c == "actionstate") { dev->actionState(arg(1).c_str(), arg(2) == "1", arg(3).c_str()); return ok(); }
  if (c == "addpoint") { dev->point(arg(1).c_str(), arg(2).c_str(), kindOf(arg(3))); return ok(); }
  if (c == "unit") { P(arg(1)).unit(arg(2).c_str()); return ok(); }
  if (c == "decimals") { P(arg(1)).decimals((uint8_t)atoi(arg(2).c_str())); return ok(); }
  if (c == "option") { P(arg(1)).option(atoi(arg(2).c_str()), arg(3).c_str()); return ok(); }
  if (c == "range") { P(arg(1)).range(D(arg(2)), D(arg(3)), arg(4).empty() ? NAN : D(arg(4))); return ok(); }
  if (c == "remove") { return ok({dev->remove(arg(1).c_str()) ? "1" : "0"}); }
  if (c == "addaction") { dev->action(arg(1).c_str(), arg(2).c_str(), [](const String&) { return HdResult::applied(); }); return ok(); }
  if (c == "addaction_nofn") { dev->action(arg(1).c_str(), arg(2).c_str(), nullptr); return ok(); }
  if (c == "addarg") {
    HdAction& ac = dev->action(arg(1).c_str(), "", [](const String&) { return HdResult::applied(); });
    ac.arg(arg(2).c_str(), arg(3).c_str(), kindOf(arg(4)));
    return ok();
  }
  if (c == "removeaction") { return ok({dev->removeAction(arg(1).c_str()) ? "1" : "0"}); }
  if (c == "rebegin") { dev->begin(arg(1).c_str(), arg(2).c_str(), arg(3).c_str(), (uint16_t)atoi(arg(4).c_str())); return ok(); }
  if (c == "loop") { dev->loop(); return ok(); }
  if (c == "millis") { g_millis = (uint32_t)strtoul(arg(1).c_str(), nullptr, 10); return ok(); }
  if (c == "advance") { g_millis += (uint32_t)strtoul(arg(1).c_str(), nullptr, 10); return ok(); }
  if (c == "wifi") { WiFi.connected = arg(1) == "1"; return ok(); }
  if (c == "mac") { WiFi.mac = arg(1).c_str(); return ok(); }
  if (c == "bcast") { WiFi.bcast = IPAddress::parse(arg(1).c_str()); return ok(); }
  if (c == "udp_in") {
    if (g_udp_on) g_udp_in.push_back({IPAddress::parse(arg(1).c_str()), (uint16_t)atoi(arg(2).c_str()), arg(3)});  // socket dicht: verloren
    return ok();
  }
  if (c == "udp_out") {
    std::vector<std::string> f;
    for (auto& p : g_udp_out) {
      f.push_back(std::string(p.ip.toString().c_str()) + ":" + std::to_string(p.port));
      f.push_back(p.data);
    }
    g_udp_out.clear();
    return ok(f);
  }
  if (c == "udp_state") {
    return ok({g_udp_on ? "1" : "0", std::to_string(g_udp_begins), std::to_string(g_udp_stops), std::to_string(g_udp_port)});
  }
  if (c == "calls") {
    std::vector<std::string> f = g_calls;
    g_calls.clear();
    return ok(f);
  }
  if (c == "prefs_token") {
    auto it = g_prefs.find("huisdev");
    if (it == g_prefs.end() || !it->second.count("token")) return ok({"-"});
    return ok({"+" + it->second["token"]});
  }
  if (c == "resetclaim") { dev->resetClaim(); return ok(); }
  if (c == "claimed") { return ok({dev->claimed() ? "1" : "0"}); }
  if (c == "id") { return ok({dev->id().c_str()}); }
  return "ERR\tonbekend commando " + c;
}

int main() {
  std::ios::sync_with_stdio(false);
  factory();
  std::string line;
  while (std::getline(std::cin, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    std::vector<std::string> f;
    std::stringstream ss(line);
    std::string x;
    while (std::getline(ss, x, '\t')) f.push_back(unesc(x));
    if (!line.empty() && line.back() == '\t') f.push_back("");  // lege laatste veld
    if (f.empty()) continue;
    if (f[0] == "quit") break;
    std::cout << run(f) << "\n" << std::flush;
  }
  dev.reset();
  return 0;
}
