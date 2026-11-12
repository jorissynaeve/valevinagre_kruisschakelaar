// huisdev.h - referentie-implementatie van het HUISDEV-protocol.
//
// Protocol : 1.0   (zie HUISDEV.md, de spec is leidend)
// Header   : 1.0.1 (wijzigt de header zonder dat het protocol wijzigt: patch/minor ophogen)
//
// Header-only, alleen ESP32 (Arduino-ESP32 3.x). Geen ArduinoJson, geen async server.
// De kern (`HuisDev::handle`) kent geen webserver. `begin(..., &server)` koppelt de blokkerende `WebServer`.
// Gebruikt u een async server (botst met WebServer.h)? Definieer dan vóór de include
//   #define HUISDEV_NO_WEBSERVER
// en roep `handle()` zelf aan (zie HUISDEV.md, "Async server").
//
// Kopieer dit bestand ongewijzigd naar de sketchmap. Pas het nooit per device aan:
// een protocolupdate betekent dit bestand vervangen.
#pragma once

#if !defined(ESP32)
#error "huisdev.h 1.x ondersteunt alleen ESP32"
#endif

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#ifndef HUISDEV_NO_WEBSERVER
#include <WebServer.h>
#include <uri/UriGlob.h>
#endif
#include <Preferences.h>
#include <list>
#include <vector>
#include <functional>
#include <math.h>

#define HUISDEV_PROTO "1.0"
#define HUISDEV_HEADER_VERSION "1.0.2"
#define HUISDEV_UDP_PORT 47500
#define HUISDEV_MAX_POINTS 64
#define HUISDEV_MAX_ACTIONS 16
#define HUISDEV_MAX_ARGS 8
#define HUISDEV_ANNOUNCE_MS 300000UL

enum HdKind : uint8_t { HD_BOOL, HD_INT, HD_FLOAT, HD_ENUM, HD_TEXT };

// Resultaat van een set of actie. `applied`: de waarde geldt al (meld hem dan ook via setX).
// `queued`: het apparaat past hem toe en meldt het later in de state. `rejected`: geweigerd, met reden.
struct HdResult {
  const char* result;
  String reason;
  static HdResult applied() { return HdResult{"applied", ""}; }
  static HdResult queued() { return HdResult{"queued", ""}; }
  static HdResult rejected(const String& why) { return HdResult{"rejected", why}; }
};

typedef std::function<HdResult(double num, const String& text)> HdSetFn;  // num voor bool/int/float/enum, text voor text
typedef std::function<HdResult(const String& body)> HdDoFn;                // body = het JSON-verzoek, lees met hdJson*

struct HdOption {
  int v;
  String l;
};

// ---- minimale JSON-lezer voor de platte verzoeken van het protocol ----
// Zoekt sleutels echt als sleutel (een string gevolgd door ':'), buiten de waarden van strings, op elk niveau,
// de eerste in documentvolgorde. Een waarde die er als sleutel uitziet ("id", "value") misleidt de lezer dus niet.
inline int hdKeyPos(const String& b, const char* key) {
  const int n = (int)b.length();
  const int kl = (int)strlen(key);
  const char* s = b.c_str();
  int i = 0;
  while (i < n) {
    if (s[i] != '"') {
      i++;
      continue;
    }
    int st = ++i;
    while (i < n && s[i] != '"') i += (s[i] == '\\' && i + 1 < n) ? 2 : 1;
    int en = i;
    i++;  // voorbij het sluitende aanhalingsteken
    int j = i;
    while (j < n && (s[j] == ' ' || s[j] == '\t' || s[j] == '\n' || s[j] == '\r')) j++;
    if (j < n && s[j] == ':' && en - st == kl && memcmp(s + st, key, kl) == 0) {
      j++;
      while (j < n && (s[j] == ' ' || s[j] == '\t' || s[j] == '\n' || s[j] == '\r')) j++;
      return j < n ? j : -1;
    }
  }
  return -1;
}
inline bool hdJsonHas(const String& b, const char* key) { return hdKeyPos(b, key) >= 0; }
inline uint32_t hdHex4(const String& b, int i) {  // 4 hexcijfers vanaf i, 0xFFFFFFFF bij een fout
  if (i + 4 > (int)b.length()) return 0xFFFFFFFFu;
  uint32_t v = 0;
  for (int k = 0; k < 4; k++) {
    char c = b[i + k];
    v <<= 4;
    if (c >= '0' && c <= '9') v |= (uint32_t)(c - '0');
    else if (c >= 'a' && c <= 'f') v |= (uint32_t)(c - 'a' + 10);
    else if (c >= 'A' && c <= 'F') v |= (uint32_t)(c - 'A' + 10);
    else return 0xFFFFFFFFu;
  }
  return v;
}
inline void hdAppendUtf8(String& o, uint32_t cp) {
  if (cp < 0x80) {
    o += (char)cp;
  } else if (cp < 0x800) {
    o += (char)(0xC0 | (cp >> 6));
    o += (char)(0x80 | (cp & 0x3F));
  } else if (cp < 0x10000) {
    o += (char)(0xE0 | (cp >> 12));
    o += (char)(0x80 | ((cp >> 6) & 0x3F));
    o += (char)(0x80 | (cp & 0x3F));
  } else {
    o += (char)(0xF0 | (cp >> 18));
    o += (char)(0x80 | ((cp >> 12) & 0x3F));
    o += (char)(0x80 | ((cp >> 6) & 0x3F));
    o += (char)(0x80 | (cp & 0x3F));
  }
}
// Leest een string-waarde en decodeert alle JSON-escapes (\n \t \r \b \f \/ \" \\ en \uXXXX incl. surrogaatparen) naar UTF-8.
inline String hdJsonStr(const String& b, const char* key, const String& def = "") {
  int i = hdKeyPos(b, key);
  const int n = (int)b.length();
  if (i < 0 || b[i] != '"') return def;
  String out;
  for (i++; i < n; i++) {
    char c = b[i];
    if (c == '"') return out;
    if (c == '\\' && i + 1 < n) {
      c = b[++i];
      switch (c) {
        case 'n': out += '\n'; break;
        case 't': out += '\t'; break;
        case 'r': out += '\r'; break;
        case 'b': out += '\b'; break;
        case 'f': out += '\f'; break;
        case 'u': {
          uint32_t cp = hdHex4(b, i + 1);
          if (cp == 0xFFFFFFFFu) return def;
          i += 4;
          if (cp >= 0xD800 && cp < 0xDC00) {  // hoog surrogaat: hoort bij \uDC00..\uDFFF
            uint32_t lo = (i + 2 < n && b[i + 1] == '\\' && b[i + 2] == 'u') ? hdHex4(b, i + 3) : 0xFFFFFFFFu;
            if (lo >= 0xDC00 && lo < 0xE000) {
              cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
              i += 6;
            } else {
              cp = 0xFFFD;
            }
          } else if (cp >= 0xDC00 && cp < 0xE000) {
            cp = 0xFFFD;
          }
          if (cp != 0) hdAppendUtf8(out, cp);  // \u0000 past niet in een C-string: weggelaten
          break;
        }
        default: out += c; break;  // \" \\ \/
      }
      continue;
    }
    out += c;
  }
  return def;  // niet afgesloten
}
inline String hdJsonRaw(const String& b, const char* key) {  // getal, true, false of null; "" bij een string of ontbrekende sleutel
  int i = hdKeyPos(b, key);
  if (i < 0 || b[i] == '"') return "";
  int j = i;
  while (j < (int)b.length() && b[j] != ',' && b[j] != '}' && b[j] != ']' && b[j] != ' ' && b[j] != '\t' && b[j] != '\n' && b[j] != '\r') j++;
  return b.substring(i, j);
}
// De ruwe JSON-tekst van een waarde, strings met hun aanhalingstekens. "" als de sleutel ontbreekt.
inline String hdJsonValue(const String& b, const char* key) {
  int i = hdKeyPos(b, key);
  if (i < 0) return "";
  const int n = (int)b.length();
  if (b[i] == '"') {
    int j = i + 1;
    while (j < n && b[j] != '"') j += (b[j] == '\\' && j + 1 < n) ? 2 : 1;
    return j < n ? b.substring(i, j + 1) : String();
  }
  return hdJsonRaw(b, key);
}
// Een JSON-getal, anders `def`. Geen true/false/null, geen hex, geen NaN of oneindig, geen waarde buiten het bereik van een double.
inline double hdJsonNum(const String& b, const char* key, double def = NAN) {
  String r = hdJsonRaw(b, key);
  if (!r.length()) return def;
  for (size_t i = 0; i < r.length(); i++) {
    char c = r[i];
    if (!((c >= '0' && c <= '9') || c == '-' || c == '+' || c == '.' || c == 'e' || c == 'E')) return def;
  }
  char* end = nullptr;
  double v = strtod(r.c_str(), &end);
  return (end && *end == 0 && isfinite(v)) ? v : def;
}
inline bool hdJsonBool(const String& b, const char* key, bool def = false) {
  String r = hdJsonRaw(b, key);
  return r == "true" ? true : (r == "false" ? false : def);
}

class HuisDev;

class HdPoint {
 public:
  String id, label;
  HdKind kind = HD_FLOAT;
  String _unit, _group, _class, _state, _confirm;
  uint8_t _decimals = 1;
  double _min = NAN, _max = NAN, _step = NAN;
  bool _writable = false, _danger = false, _diag = false;
  uint32_t _stale = 0;
  std::vector<HdOption> options;
  HdSetFn onSet;
  // waarde (alleen bij punten, niet bij actie-argumenten)
  bool known = false;
  double num = 0;
  String text;
  uint32_t stamp = 0;
  bool* dirty = nullptr;

  HdPoint& touch() {
    if (dirty) *dirty = true;
    return *this;
  }
  HdPoint& unit(const String& u) { _unit = u; return touch(); }
  HdPoint& decimals(uint8_t d) { _decimals = d; return touch(); }
  HdPoint& range(double mn, double mx, double step = NAN) { _min = mn; _max = mx; _step = step; return touch(); }
  HdPoint& group(const String& g) { _group = g; return touch(); }
  HdPoint& devClass(const String& c) { _class = c; return touch(); }        // temperature, power, energy, voltage, current, ph, ...
  HdPoint& stateClass(const String& s) { _state = s; return touch(); }       // measurement | total_increasing
  HdPoint& option(int v, const String& l) {  // zelfde waarde nog eens: label vervangen, niet toevoegen
    for (auto& o : options)
      if (o.v == v) {
        o.l = l;
        return touch();
      }
    options.push_back({v, l});
    return touch();
  }
  HdPoint& diag(bool d = true) { _diag = d; return touch(); }                // secundair (RSSI, uptime): standaard verborgen
  HdPoint& stale(uint32_t ms) { _stale = ms; return touch(); }                // na zoveel ms zonder update is de waarde verouderd
  HdPoint& writable(HdSetFn fn) { _writable = true; onSet = fn; return touch(); }
  HdPoint& danger(const String& confirmText = "Weet u het zeker?") { _danger = true; _confirm = confirmText; return touch(); }
};

class HdAction {
 public:
  String id, label, _confirm;
  bool _danger = false;
  HdDoFn fn;
  bool disabled = false;  // wisselt vaak: staat in de state, niet in de descriptor
  String reason;
  std::list<HdPoint> args;
  HdPoint overflowArg;
  bool* dirty = nullptr;

  HdAction& danger(const String& confirmText = "Weet u het zeker?") {
    _danger = true;
    _confirm = confirmText;
    if (dirty) *dirty = true;
    return *this;
  }
  HdPoint& arg(const char* aid, const char* alabel, HdKind k) {
    for (auto& p : args)
      if (p.id == aid) return p;
    if (args.size() >= HUISDEV_MAX_ARGS) {
      overflowArg = HdPoint();  // boven de limiet: de aanroep landt hier en verdwijnt
      return overflowArg;
    }
    args.emplace_back();
    HdPoint& p = args.back();
    p.id = aid;
    p.label = alabel;
    p.kind = k;
    p.dirty = dirty;
    if (dirty) *dirty = true;
    return p;
  }
};

class HuisDev {
 public:
  // ---- registreren (mag ook tijdens het draaien: een wijziging verandert de `rev` en wordt vanzelf aangekondigd) ----
  // Een bestaand id geeft hetzelfde punt terug. Het type van een id wijzigen mag nooit: gebruik dan een nieuw id.
  HdPoint& point(const char* id, const char* label, HdKind kind) {
    for (auto& p : points)
      if (p.id == id) return p;
    if (points.size() >= HUISDEV_MAX_POINTS) {
      overflow = HdPoint();  // boven de limiet: de aanroep landt hier en verdwijnt (geen groeiend geheugen)
      return overflow;
    }
    points.emplace_back();
    HdPoint& p = points.back();
    p.id = id;
    p.label = label;
    p.kind = kind;
    p._decimals = kind == HD_FLOAT ? 1 : 0;
    p.dirty = &dirty;
    dirty = true;
    return p;
  }
  bool remove(const char* id) {
    for (auto it = points.begin(); it != points.end(); ++it)
      if (it->id == id) {
        points.erase(it);
        dirty = true;
        return true;
      }
    return false;
  }
  HdAction& action(const char* id, const char* label, HdDoFn fn) {
    for (auto& a : actions)
      if (a.id == id) return a;
    if (actions.size() >= HUISDEV_MAX_ACTIONS) {
      overflowAction = HdAction();
      return overflowAction;
    }
    actions.emplace_back();
    HdAction& a = actions.back();
    a.id = id;
    a.label = label;
    a.fn = fn;
    a.dirty = &dirty;
    dirty = true;
    return a;
  }
  bool removeAction(const char* id) {
    for (auto it = actions.begin(); it != actions.end(); ++it)
      if (it->id == id) {
        actions.erase(it);
        dirty = true;
        return true;
      }
    return false;
  }
  void touch() { dirty = true; }

  // ---- waarden melden ----
  void setNum(const char* id, double v) {
    HdPoint* p = find(id);
    if (!p) return;
    p->num = v;
    p->known = isfinite(v);  // nan en oneindig zijn onbekend: geen geldige JSON
    p->stamp = millis();
  }
  void setBool(const char* id, bool v) { setNum(id, v ? 1 : 0); }
  void setText(const char* id, const String& v) {
    HdPoint* p = find(id);
    if (!p) return;
    p->text = v;
    p->known = true;
    p->stamp = millis();
  }
  void setUnknown(const char* id) {  // geen waarde (nog niet ontvangen, sensor weg): nooit 0 melden
    HdPoint* p = find(id);
    if (p) p->known = false;
  }
  void actionState(const char* id, bool disabled, const char* reason = "") {
    for (auto& a : actions)
      if (a.id == id) {
        a.disabled = disabled;
        a.reason = disabled ? reason : "";
      }
  }

  // ---- start en lus ----
  // type: korte soortnaam, bv. "poolcontroller". name: standaardnaam (de gebruiker hernoemt in de huisconfig).
  #ifndef HUISDEV_NO_WEBSERVER
  void begin(const char* type, const char* name, const char* fw, WebServer* srv, uint16_t httpPort = 80) {
    begin(type, name, fw, httpPort);
    if (srv) attach(*srv);
  }
#endif
  void begin(const char* type, const char* name, const char* fw, uint16_t httpPort = 80) {
    _type = type;
    _name = name;
    _fw = fw;
    _port = httpPort;
    String mac = WiFi.macAddress();
    mac.replace(":", "");
    mac.toLowerCase();
    String slug;
    for (const char* c = type; *c && slug.length() < 25; c++) slug += (isalnum((unsigned char)*c) ? (char)tolower(*c) : '-');  // id: max 32 tekens
    _id = slug + "-" + mac.substring(6);
    _mac = WiFi.macAddress();
    Preferences pr;
    if (pr.begin("huisdev", true)) {
      _token = pr.getString("token", "");
      pr.end();
    }
    dirty = true;
  }
  void loop() {
    refresh();
    udpLoop();
  }

  // ---- claim ----
  bool claimed() const { return _token.length() > 0; }
  void resetClaim() {  // lokaal: knop, seriële console of eigen webpagina. Het protocol eist dat dit bestaat.
    _token = "";
    Preferences pr;
    if (pr.begin("huisdev", false)) {
      pr.remove("token");
      pr.end();
    }
  }
  const String& id() const { return _id; }

  // ---- de kern: een HTTP-verzoek afhandelen, zonder webserver-afhankelijkheid ----
  // Geeft de HTTP-status, of 0 als het pad niet van HUISDEV is. `out` is het JSON-antwoord.
  int handle(const String& method, const String& path, const String& body, const String& token, String& out) {
    if (!path.startsWith("/huisdev/")) return 0;
    refresh();
    const bool isGet = path == "/huisdev/descriptor" || path == "/huisdev/state";
    const bool isPost = path == "/huisdev/claim" || path == "/huisdev/unclaim" || path == "/huisdev/set" || path == "/huisdev/do";
    if (!isGet && !isPost) return err(out, 404, "unknown_path", "");
    if (method != (isGet ? "GET" : "POST")) return err(out, 405, "method", "");
    if (path == "/huisdev/descriptor") {
      out = desc;
      return 200;
    }
    bool tokenOk = claimed() && ctEq(token, _token);
    if (path == "/huisdev/state") {
      if (claimed() && !tokenOk) return err(out, 401, "token", "");
      buildState(out);
      return 200;
    }
    if (path == "/huisdev/claim") {
      if (claimed()) return err(out, 409, "already_claimed", "");
      String t = hdJsonStr(body, "token");
      if (t.length() < 16 || t.length() > 128) return err(out, 422, "bad_token", "16-128 tekens");
      _token = t;
      Preferences pr;
      if (pr.begin("huisdev", false)) {
        pr.putString("token", _token);
        pr.end();
      }
      out = "{\"ok\":true}";
      return 200;
    }
    if (path == "/huisdev/unclaim") {
      if (!tokenOk) return err(out, claimed() ? 401 : 409, claimed() ? "token" : "not_claimed", "");
      resetClaim();
      out = "{\"ok\":true}";
      return 200;
    }
    if (path == "/huisdev/set" || path == "/huisdev/do") {
      if (!claimed()) return err(out, 403, "not_claimed", "eerst claimen");
      if (!tokenOk) return err(out, 401, "token", "");
      return path == "/huisdev/set" ? doSet(body, out) : doAction(body, out);
    }
    return err(out, 404, "unknown_path", "");
  }

#ifndef HUISDEV_NO_WEBSERVER
  // ---- blokkerende WebServer koppelen. Roep collectHeaders maar een keer aan: voeg anders X-Huisdev-Token toe aan uw eigen lijst. ----
  void attach(WebServer& srv) {
    _srv = &srv;
    static const char* hdrs[] = {"X-Huisdev-Token"};
    srv.collectHeaders(hdrs, 1);
    // Een route voor alle methoden en alle paden onder /huisdev/: handle() beslist. Met aparte routes per methode valt een verkeerde
    // methode of een onbekend pad door naar de onNotFound van de sketch (platte tekst) in plaats van het JSON-antwoord uit de spec.
    srv.on(UriGlob("/huisdev/*"), HTTP_ANY, [this]() { serve(); });
  }
#endif

 private:
  std::list<HdPoint> points;
  std::list<HdAction> actions;
  HdPoint overflow;
  HdAction overflowAction;
  bool dirty = true;
  bool revChanged = false;
  String _type, _name, _fw, _id, _mac, _token, rev, desc;
  uint16_t _port = 80;
#ifndef HUISDEV_NO_WEBSERVER
  WebServer* _srv = nullptr;
#endif
  WiFiUDP udp;
  bool udpOn = false;
  uint32_t lastAnnounce = 0;

  HdPoint* find(const char* id) {
    for (auto& p : points)
      if (p.id == id) return &p;
    return nullptr;
  }
  static bool ctEq(const String& a, const String& b) {  // constante tijd
    if (a.length() != b.length()) return false;
    uint8_t d = 0;
    for (size_t i = 0; i < a.length(); i++) d |= (uint8_t)(a[i] ^ b[i]);
    return d == 0;
  }
  static int err(String& out, int code, const char* e, const char* reason) {
    out = String("{\"ok\":false,\"error\":\"") + e + "\",\"reason\":\"";
    jesc(out, reason);
    out += "\"}";
    return code;
  }
  static void jesc(String& o, const String& s) {
    for (size_t i = 0; i < s.length(); i++) {
      char c = s[i];
      if (c == '"' || c == '\\') {
        o += '\\';
        o += c;
      } else if ((uint8_t)c < 0x20) {
        char b[8];
        snprintf(b, sizeof b, "\\u%04x", c);
        o += b;
      } else {
        o += c;
      }
    }
  }
  static String fmt(double v, uint8_t dec) {
    String s = String(v, (unsigned int)dec);
    return s;
  }
  static String fmtTrim(double v) {  // voor min/max/step: geen overbodige nullen
    String s = String(v, 6u);
    if (s.indexOf('.') >= 0) {
      while (s.endsWith("0")) s.remove(s.length() - 1);
      if (s.endsWith(".")) s.remove(s.length() - 1);
    }
    return s;
  }
  static const char* kindName(HdKind k) {
    switch (k) {
      case HD_BOOL: return "bool";
      case HD_INT: return "int";
      case HD_ENUM: return "enum";
      case HD_TEXT: return "text";
      default: return "float";
    }
  }
  static uint32_t fnv(const String& s) {
    uint32_t h = 2166136261u;
    for (size_t i = 0; i < s.length(); i++) {
      h ^= (uint8_t)s[i];
      h *= 16777619u;
    }
    return h;
  }
  static void pointJson(String& o, const HdPoint& p, bool isArg) {
    o += "{\"id\":\"";
    jesc(o, p.id);
    o += "\",\"label\":\"";
    jesc(o, p.label);
    o += "\",\"kind\":\"";
    o += kindName(p.kind);
    o += "\"";
    if (p._unit.length()) {
      o += ",\"unit\":\"";
      jesc(o, p._unit);
      o += "\"";
    }
    if (p.kind == HD_FLOAT) o += ",\"decimals\":" + String(p._decimals);
    if (!isnan(p._min)) o += ",\"min\":" + fmtTrim(p._min);
    if (!isnan(p._max)) o += ",\"max\":" + fmtTrim(p._max);
    if (!isnan(p._step)) o += ",\"step\":" + fmtTrim(p._step);
    if (!isArg && p._writable) o += ",\"writable\":true";
    if (!isArg && p._danger) {
      o += ",\"danger\":true,\"confirm\":\"";
      jesc(o, p._confirm);
      o += "\"";
    }
    if (!isArg && p._diag) o += ",\"diag\":true";
    if (!isArg && p._group.length()) {
      o += ",\"group\":\"";
      jesc(o, p._group);
      o += "\"";
    }
    if (!isArg && p._class.length()) {
      o += ",\"class\":\"";
      jesc(o, p._class);
      o += "\"";
    }
    if (!isArg && p._state.length()) {
      o += ",\"state\":\"";
      jesc(o, p._state);
      o += "\"";
    }
    if (!isArg && p._stale) o += ",\"stale\":" + String(p._stale);
    if (p.options.size()) {
      o += ",\"options\":[";
      bool first = true;
      for (auto& op : p.options) {
        if (!first) o += ',';
        first = false;
        o += "{\"v\":" + String(op.v) + ",\"l\":\"";
        jesc(o, op.l);
        o += "\"}";
      }
      o += "]";
    }
    o += "}";
  }
  void buildDescriptor() {
    String body;
    body.reserve(1536);
    body += "\"points\":[";
    bool first = true;
    for (auto& p : points) {
      if (!first) body += ',';
      first = false;
      pointJson(body, p, false);
    }
    body += "],\"actions\":[";
    first = true;
    for (auto& a : actions) {
      if (!first) body += ',';
      first = false;
      body += "{\"id\":\"";
      jesc(body, a.id);
      body += "\",\"label\":\"";
      jesc(body, a.label);
      body += "\"";
      if (a._danger) {
        body += ",\"danger\":true,\"confirm\":\"";
        jesc(body, a._confirm);
        body += "\"";
      }
      if (a.args.size()) {
        body += ",\"args\":[";
        bool f2 = true;
        for (auto& ar : a.args) {
          if (!f2) body += ',';
          f2 = false;
          pointJson(body, ar, true);
        }
        body += "]";
      }
      body += "}";
    }
    body += "]";
    char rb[9];
    snprintf(rb, sizeof rb, "%08x", (unsigned)fnv(body));
    if (rev != rb) {
      revChanged = rev.length() > 0;
      rev = rb;
    }
    desc = String("{\"proto\":\"" HUISDEV_PROTO "\",\"rev\":\"") + rev + "\",\"device\":{\"id\":\"" + _id + "\",\"type\":\"";
    jesc(desc, _type);
    desc += "\",\"name\":\"";
    jesc(desc, _name);
    desc += "\",\"fw\":\"";
    jesc(desc, _fw);
    desc += "\",\"mac\":\"" + _mac + "\",\"lib\":\"" HUISDEV_HEADER_VERSION "\"}," + body + "}";
  }
  void refresh() {
    if (dirty) {
      dirty = false;
      buildDescriptor();
      if (revChanged) {
        revChanged = false;
        announce();
      }
    }
  }
  void buildState(String& out) {
    out = "";
    out.reserve(64 + points.size() * 40);
    out += "{\"proto\":\"" HUISDEV_PROTO "\",\"rev\":\"" + rev + "\",\"up\":" + String((uint32_t)(millis() / 1000)) + ",\"points\":{";
    bool first = true;
    for (auto& p : points) {
      if (!first) out += ',';
      first = false;
      out += "\"";
      jesc(out, p.id);
      out += "\":{\"v\":";
      if (!p.known) {
        out += "null";
      } else {
        switch (p.kind) {
          case HD_BOOL: out += p.num != 0 ? "true" : "false"; break;
          case HD_INT:
          case HD_ENUM: out += String((long)lround(p.num)); break;
          case HD_TEXT:
            out += "\"";
            jesc(out, p.text);
            out += "\"";
            break;
          default: out += fmt(p.num, p._decimals);
        }
        out += ",\"age\":" + String((uint32_t)(millis() - p.stamp));
      }
      out += "}";
    }
    out += "},\"actions\":{";
    first = true;
    for (auto& a : actions) {
      if (!a.disabled) continue;
      if (!first) out += ',';
      first = false;
      out += "\"";
      jesc(out, a.id);
      out += "\":{\"disabled\":true,\"reason\":\"";
      jesc(out, a.reason);
      out += "\"}";
    }
    out += "}}";
  }
  // Valideert een waarde tegen type, enum-opties, bereik en stap. Geeft "" bij ok, anders de fout.
  static const char* validate(const HdPoint& p, const String& body, double& num, String& text) {
    if (!hdJsonHas(body, "value")) return "missing_value";
    if (p.kind == HD_TEXT) {
      if (hdJsonRaw(body, "value").length()) return "bad_value";
      text = hdJsonStr(body, "value");
      return "";
    }
    String raw = hdJsonRaw(body, "value");
    if (p.kind == HD_BOOL) {
      if (raw == "true") num = 1;
      else if (raw == "false") num = 0;
      else return "bad_value";
      return "";
    }
    num = hdJsonNum(body, "value", NAN);
    if (isnan(num)) return "bad_value";  // hdJsonNum geeft NaN voor alles wat geen eindig JSON-getal is
    if (p.kind == HD_INT || p.kind == HD_ENUM) {
      if (fabs(num - round(num)) > 1e-9) return "bad_value";
    }
    if (p.kind == HD_ENUM) {
      for (auto& o : p.options)
        if (o.v == (int)lround(num)) return "";
      return "bad_option";
    }
    if (!isnan(p._min) && num < p._min - 1e-9) return "range";
    if (!isnan(p._max) && num > p._max + 1e-9) return "range";
    if (!isnan(p._step) && p._step > 0) {
      double base = isnan(p._min) ? 0 : p._min;
      double k = (num - base) / p._step;
      if (fabs(k - round(k)) > 1e-6) return "step";
    }
    return "";
  }
  int doSet(const String& body, String& out) {
    String id = hdJsonStr(body, "id");
    HdPoint* p = find(id.c_str());
    if (!p) return err(out, 404, "unknown_point", "");
    if (!p->_writable || !p->onSet) return err(out, 403, "not_writable", "");
    if (p->_danger && !hdJsonBool(body, "confirm")) return err(out, 409, "confirm_required", p->_confirm.c_str());
    double num = 0;
    String text;
    const char* e = validate(*p, body, num, text);
    if (*e) return err(out, 422, e, "");
    return result(out, p->onSet(num, text));
  }
  int doAction(const String& body, String& out) {
    String id = hdJsonStr(body, "id");
    HdAction* act = nullptr;
    for (auto& a : actions)
      if (a.id == id) act = &a;
    if (!act) return err(out, 404, "unknown_action", "");
    if (act->disabled) return err(out, 409, "disabled", act->reason.c_str());
    if (act->_danger && !hdJsonBool(body, "confirm")) return err(out, 409, "confirm_required", act->_confirm.c_str());
    for (auto& ar : act->args) {
      // argumenten staan in "args":{...}. Argument-id's mogen niet "id", "args", "confirm" of "value" heten.
      // De waarde gaat als {"value":<waarde>} door dezelfde validatie als bij /huisdev/set.
      String v = hdJsonValue(body, ar.id.c_str());
      if (!v.length()) return err(out, 422, "missing_arg", ar.id.c_str());
      double num = 0;
      String text;
      const char* e = validate(ar, "{\"value\":" + v + "}", num, text);
      if (*e) return err(out, 422, e, ar.id.c_str());
    }
    if (!act->fn) return result(out, HdResult::rejected("niet beschikbaar"));
    return result(out, act->fn(body));
  }
  static int result(String& out, const HdResult& r) {
    bool ok = String(r.result) != "rejected";
    out = String("{\"ok\":") + (ok ? "true" : "false") + ",\"result\":\"" + r.result + "\",\"reason\":\"";
    jesc(out, r.reason);
    out += "\"}";
    return 200;
  }
#ifndef HUISDEV_NO_WEBSERVER
  static const char* methodName(HTTPMethod m) {
    switch (m) {
      case HTTP_GET: return "GET";
      case HTTP_POST: return "POST";
      case HTTP_PUT: return "PUT";
      case HTTP_DELETE: return "DELETE";
      case HTTP_PATCH: return "PATCH";
      case HTTP_HEAD: return "HEAD";
      case HTTP_OPTIONS: return "OPTIONS";
      default: return "OTHER";
    }
  }
  void serve() {
    String out;
    int code = handle(methodName(_srv->method()), _srv->uri(), _srv->arg("plain"), _srv->header("X-Huisdev-Token"), out);
    _srv->sendHeader("Cache-Control", "no-store");
    _srv->send(code ? code : 404, "application/json", out);
  }
#endif
  // ---- ontdekking ----
  String discoveryJson() {
    String j = "{\"huisdev\":\"" HUISDEV_PROTO "\",\"id\":\"" + _id + "\",\"type\":\"";
    jesc(j, _type);
    j += "\",\"name\":\"";
    jesc(j, _name);
    j += "\",\"fw\":\"";
    jesc(j, _fw);
    j += "\",\"rev\":\"" + rev + "\",\"port\":" + String(_port) + ",\"claimed\":" + (claimed() ? "true" : "false") + "}";
    return j;
  }
  void announce() {
    if (!udpOn) return;
    String j = discoveryJson();
    udp.beginPacket(WiFi.broadcastIP(), HUISDEV_UDP_PORT);
    udp.write((const uint8_t*)j.c_str(), j.length());
    udp.endPacket();
    lastAnnounce = millis();
  }
  void udpLoop() {
    bool up = WiFi.status() == WL_CONNECTED;
    if (up && !udpOn) {
      udp.begin(HUISDEV_UDP_PORT);
      udpOn = true;
      refresh();
      announce();
    }
    if (!up && udpOn) {
      udp.stop();
      udpOn = false;
    }
    if (!udpOn) return;
    int n = udp.parsePacket();
    while (n > 0) {
      char buf[24];
      int len = udp.read((uint8_t*)buf, sizeof(buf) - 1);
      buf[len > 0 ? len : 0] = 0;
      if (strncmp(buf, "HUISDEV?", 8) == 0) {
        IPAddress ip = udp.remoteIP();
        uint16_t port = udp.remotePort();
        String j = discoveryJson();
        udp.beginPacket(ip, port);
        udp.write((const uint8_t*)j.c_str(), j.length());
        udp.endPacket();
      }
      n = udp.parsePacket();
    }
    if (millis() - lastAnnounce > HUISDEV_ANNOUNCE_MS) announce();
  }
};
