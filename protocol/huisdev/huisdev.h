// huisdev.h - referentie-implementatie van het HUISDEV-protocol.
//
// Protocol : 1.1   (zie HUISDEV.md, de spec is leidend)
// Header   : 1.1.0 (wijzigt de header zonder dat het protocol wijzigt: patch/minor ophogen)
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
#include <time.h>
#include <algorithm>
#include <initializer_list>
#include <list>
#include <vector>
#include <functional>
#include <math.h>

#define HUISDEV_PROTO "1.1"
#define HUISDEV_HEADER_VERSION "1.1.0"
#define HUISDEV_UDP_PORT 47500
#define HUISDEV_MAX_POINTS 64
#define HUISDEV_MAX_ACTIONS 16
#define HUISDEV_MAX_ARGS 8
#define HUISDEV_ANNOUNCE_MS 300000UL
// Scenes (1.1, HUISDEV.md §16). Alleen actief na dev.scenes(...).
#define HUISDEV_SCENE_MAX_SLOTS 8
#define HUISDEV_SCENE_MAX_BYTES 1024
#define HUISDEV_SCENE_MAX_TOTAL 6144            // slots * max_bytes
#define HUISDEV_SCENE_MAX_SCHED 8
#define HUISDEV_SCENE_MIN_WRITE_MS 2000UL       // minimuminterval tussen schrijfacties naar NVS (store/schedule)
#define HUISDEV_SCENE_TIME_MIN 1700000000UL     // een klok is geldig als hij hierboven staat
#define HUISDEV_SCENE_AHEAD_S (14UL * 86400UL)  // `at` mag hoogstens zo ver vooruit
#define HUISDEV_SCENE_LATE_S 86400UL            // een geplande start die langer geleden is wordt genegeerd
// De klok (epoch, seconden). Een sketch met een eigen klokbron mag dit vóór de include herdefiniëren.
#ifndef HUISDEV_NOW
#define HUISDEV_NOW() ((int64_t)time(nullptr))
#endif

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

// Scenes (1.1). De header kent de inhoud van een scene niet, alleen bytes; valideren en afspelen doet het device.
typedef std::function<String(const uint8_t* data, size_t n)> HdSceneCheckFn;  // "" = geldig, anders de reden van weigering
typedef std::function<HdResult(int slot, const uint8_t* data, size_t n, uint32_t elapsedS)> HdScenePlayFn;  // slot 0 = rechtstreeks; elapsedS > 0: start op positie
typedef std::function<HdResult(int slot)> HdSceneStopFn;                      // slot 0 = elke scene

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

// ---- base64 (standaard alfabet, padding verplicht, strikt canoniek), zonder allocaties ----
inline int hdB64Val(char c) {
  if (c >= 'A' && c <= 'Z') return c - 'A';
  if (c >= 'a' && c <= 'z') return c - 'a' + 26;
  if (c >= '0' && c <= '9') return c - '0' + 52;
  if (c == '+') return 62;
  if (c == '/') return 63;
  return -1;
}
// Aantal bytes dat s[0..len) oplevert, of -1 als het geen geldig base64 is. `out` mag nullptr zijn (alleen controleren);
// anders moet er plaats zijn voor het resultaat (zie de eerste aanroep).
inline long hdB64Decode(const char* s, size_t len, uint8_t* out) {
  if (len % 4) return -1;
  size_t o = 0;
  for (size_t i = 0; i < len; i += 4) {
    const bool last = i + 4 == len;
    int v[4] = {0, 0, 0, 0};
    int pad = 0;
    for (int k = 0; k < 4; k++) {
      char c = s[i + k];
      if (c == '=') {
        if (!last || k < 2) return -1;
        pad++;
      } else {
        int x = pad ? -1 : hdB64Val(c);
        if (x < 0) return -1;
        v[k] = x;
      }
    }
    if (pad == 2 && (v[1] & 0x0F)) return -1;  // niet-canonieke restbits
    if (pad == 1 && (v[2] & 0x03)) return -1;
    uint32_t w = ((uint32_t)v[0] << 18) | ((uint32_t)v[1] << 12) | ((uint32_t)v[2] << 6) | (uint32_t)v[3];
    size_t take = 3 - (size_t)pad;
    if (out) {
      out[o] = (uint8_t)(w >> 16);
      if (take > 1) out[o + 1] = (uint8_t)(w >> 8);
      if (take > 2) out[o + 2] = (uint8_t)w;
    }
    o += take;
  }
  return (long)o;
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
    sceneTick();
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
    sceneWipeAll();  // een device zonder dev.scenes() raakt de scene-opslag niet aan
  }
  const String& id() const { return _id; }

  // ---- scenes (1.1, HUISDEV.md §16): optioneel, alleen actief na scenes() ----
  // formats: de talen die het device begrijpt ("ledscene/1"). Grenzen (normatief): slots 1..8, max_bytes 1..1024, slots*max_bytes <= 6144
  // (is dat te veel, dan verlaagt de header max_bytes), sched 0..8. De waarden die u opgeeft zijn dus een verzoek: de descriptor meldt wat geldt.
  // Mag voor of na begin(); een wijziging van de parameters verandert de `rev`.
  void scenes(std::initializer_list<const char*> formats, int slots, int maxBytes, int sched) {
    slots = slots < 1 ? 1 : (slots > HUISDEV_SCENE_MAX_SLOTS ? HUISDEV_SCENE_MAX_SLOTS : slots);
    maxBytes = maxBytes < 1 ? 1 : (maxBytes > HUISDEV_SCENE_MAX_BYTES ? HUISDEV_SCENE_MAX_BYTES : maxBytes);
    if (slots * maxBytes > HUISDEV_SCENE_MAX_TOTAL) maxBytes = HUISDEV_SCENE_MAX_TOTAL / slots;
    sched = sched < 0 ? 0 : (sched > HUISDEV_SCENE_MAX_SCHED ? HUISDEV_SCENE_MAX_SCHED : sched);
    std::vector<String> f;
    for (const char* c : formats) f.push_back(c ? c : "");
    bool same = _scOn && slots == _scSlots && maxBytes == _scMax && sched == _scSched && f.size() == _scFormats.size();
    for (size_t i = 0; same && i < f.size(); i++) same = f[i] == _scFormats[i];
    if (same) return;
    _scOn = true;
    _scSlots = slots;
    _scMax = maxBytes;
    _scSched = sched;
    _scFormats = f;
    dirty = true;
    sceneLoad();
  }
  // Valideert een scene. "" = geldig, anders de reden (HTTP 200 `rejected`). Zonder deze functie is elke scene geldig.
  void onSceneCheck(HdSceneCheckFn fn) { _onCheck = fn; }
  // Start een scene. slot 0 = rechtstreeks gespeeld (`play` met `data`). elapsedS > 0: een geplande start die te laat is; het device beslist
  // of dat nog zinvol is (resume-venster van de scene) en geeft anders `rejected` terug: de header slaat die start dan over.
  // `data` is alleen geldig tijdens de aanroep. Na `applied`/`queued` meldt de header zelf `run` (zonder `until`), tenzij het device
  // tijdens de aanroep sceneRunning() of sceneIdle() aanriep.
  void onScenePlay(HdScenePlayFn fn) { _onPlay = fn; }
  // Stopt de scene van `slot` (0 = elke scene, ook als de header denkt dat er niets speelt). Na een niet-geweigerd antwoord meldt de header `run` als afwezig.
  void onSceneStop(HdSceneStopFn fn) { _onStop = fn; }
  // Meld wat er speelt (`run` in de state). hash = sceneHash() van de bytes, untilEpoch = einde (epoch, 0 = onbegrensd of onbekend).
  // Roep sceneIdle() aan als de scene eindigt. Is `until` bereikt (met een geldige klok), dan meldt de header zelf niets meer.
  void sceneRunning(int slot, uint32_t hash, uint32_t untilEpoch = 0) {
    _run.active = true;
    _run.slot = slot;
    _run.h = hash;
    _run.until = untilEpoch;
    _runGen++;
  }
  void sceneIdle() {
    _run.active = false;
    _runGen++;
  }
  // Leest de bytes van een slot (bv. na een herstart, om de scene weer af te spelen). Geeft het aantal bytes, 0 als het slot leeg is of
  // `cap` te klein is. Met buf == nullptr: alleen de lengte.
  size_t sceneRead(int slot, uint8_t* buf, size_t cap) {
    if (!_scOn || slot < 1 || slot > _scSlots || !_scHave[slot].have) return 0;
    if (!buf) return _scHave[slot].n;
    return scGet(slot, buf, cap);
  }
  // De hash zoals `have` en `run` hem melden: FNV-1a-32 over de ruwe bytes.
  static uint32_t sceneHash(const uint8_t* d, size_t n) {
    uint32_t h = 2166136261u;
    for (size_t i = 0; i < n; i++) {
      h ^= d[i];
      h *= 16777619u;
    }
    return h;
  }

  // ---- de kern: een HTTP-verzoek afhandelen, zonder webserver-afhankelijkheid ----
  // Geeft de HTTP-status, of 0 als het pad niet van HUISDEV is. `out` is het JSON-antwoord.
  int handle(const String& method, const String& path, const String& body, const String& token, String& out) {
    if (!path.startsWith("/huisdev/")) return 0;
    refresh();
    const bool isGet = path == "/huisdev/descriptor" || path == "/huisdev/state";
    const bool isScene = path == "/huisdev/scene/store" || path == "/huisdev/scene/play" || path == "/huisdev/scene/stop" || path == "/huisdev/scene/schedule";
    const bool isPost = isScene || path == "/huisdev/claim" || path == "/huisdev/unclaim" || path == "/huisdev/set" || path == "/huisdev/do";
    if (!isGet && !isPost) return err(out, 404, "unknown_path", "");
    if (isScene && !_scOn) return err(out, 404, "unknown_path", "");  // zonder dev.scenes() bestaan deze paden niet
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
    if (isScene) {
      if (!claimed()) return err(out, 403, "not_claimed", "eerst claimen");
      if (!tokenOk) return err(out, 401, "token", "");
      return sceneRoute(path, body, out);
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
  // scenes
  struct ScSlot {
    bool have = false;
    uint32_t h = 0;
    uint16_t n = 0;
  };
  struct ScSched {
    uint8_t slot;
    uint32_t at;
  };
  struct ScRun {
    bool active = false;
    int slot = 0;
    uint32_t h = 0, until = 0;
  };
  bool _scOn = false;
  int _scSlots = 0, _scMax = 0, _scSched = 0;
  std::vector<String> _scFormats;
  ScSlot _scHave[HUISDEV_SCENE_MAX_SLOTS + 1];
  std::vector<ScSched> _scList;  // wat nog moet komen (RAM)
  std::vector<ScSched> _scNv;    // wat in NVS staat
  ScRun _run;
  uint32_t _runGen = 0;
  uint32_t _scLastWrite = 0;
  bool _scWrote = false;
  HdSceneCheckFn _onCheck;
  HdScenePlayFn _onPlay;
  HdSceneStopFn _onStop;

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
    if (_scOn) {  // direct na actions; hoort bij de rev (HUISDEV.md §5, §16)
      body += ",\"scenes\":{\"formats\":[";
      for (size_t i = 0; i < _scFormats.size(); i++) {
        if (i) body += ',';
        body += '"';
        jesc(body, _scFormats[i]);
        body += '"';
      }
      body += "],\"slots\":" + String(_scSlots) + ",\"max_bytes\":" + String(_scMax) + ",\"sched\":" + String(_scSched) + "}";
    }
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
    out += "}";
    if (_scOn) sceneState(out);
    out += "}";
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
  // ================= scenes (1.1, HUISDEV.md §16) =================
  // Alles draait vanuit loop() of de webserver-taak: geen eigen taken, geen delay.
  struct ScBuf {  // een tijdelijke buffer (malloc, zodat een tekort een `rejected` wordt en geen crash)
    uint8_t* p = nullptr;
    size_t n = 0;
    ScBuf() {}
    ScBuf(const ScBuf&) = delete;
    ScBuf& operator=(const ScBuf&) = delete;
    ~ScBuf() { free(p); }
    bool alloc(size_t len) {
      free(p);
      p = (uint8_t*)malloc(len ? len : 1);
      n = p ? len : 0;
      return p != nullptr;
    }
  };
  static const char* scKey(int slot, char* b, size_t cap) {
    snprintf(b, cap, "s%d", slot);
    return b;
  }
  static void hex8(String& o, uint32_t v) {
    char b[9];
    snprintf(b, sizeof b, "%08x", (unsigned)v);
    o += b;
  }
  static bool schedLess(const ScSched& a, const ScSched& b) { return a.at != b.at ? a.at < b.at : a.slot < b.slot; }
  static bool schedEq(const std::vector<ScSched>& a, const std::vector<ScSched>& b) {
    if (a.size() != b.size()) return false;
    for (size_t i = 0; i < a.size(); i++)
      if (a[i].slot != b[i].slot || a[i].at != b[i].at) return false;
    return true;
  }
  uint32_t sceneNow() {  // de klok (epoch) als hij geldig is, anders 0
    int64_t t = HUISDEV_NOW();
    return (t > (int64_t)HUISDEV_SCENE_TIME_MIN && t < 4294967295LL) ? (uint32_t)t : 0;
  }
  // ---- NVS (namespace huisdev_sc: s1..s8 = bytes, sched = (slot u8, at u32 little-endian)-paren) ----
  size_t scGet(int slot, uint8_t* buf, size_t cap) {
    char k[8];
    Preferences pr;
    if (!pr.begin("huisdev_sc", true)) return 0;
    size_t n = pr.getBytes(scKey(slot, k, sizeof k), buf, cap);
    pr.end();
    return n;
  }
  bool scPut(const char* key, const uint8_t* d, size_t n) {
    Preferences pr;
    if (!pr.begin("huisdev_sc", false)) return false;
    size_t w = pr.putBytes(key, d, n);
    pr.end();
    return w == n;
  }
  bool scDel(const char* key) {
    Preferences pr;
    if (!pr.begin("huisdev_sc", false)) return false;
    bool ok = !pr.isKey(key) || pr.remove(key);
    pr.end();
    return ok;
  }
  bool scSame(int slot, const uint8_t* d, size_t n) {  // staat er al precies dit?
    ScBuf b;
    return b.alloc(n) && scGet(slot, b.p, n) == n && memcmp(b.p, d, n) == 0;
  }
  bool scTooOften() const { return _scWrote && (uint32_t)(millis() - _scLastWrite) < HUISDEV_SCENE_MIN_WRITE_MS; }
  void scStamp() {
    _scWrote = true;
    _scLastWrite = millis();
  }
  void sceneLoad() {  // na scenes() en na een herstart: wat er in NVS staat inlezen
    for (auto& s : _scHave) s = ScSlot();
    _scList.clear();
    _scNv.clear();
    Preferences pr;
    if (!pr.begin("huisdev_sc", true)) return;
    uint8_t* buf = (uint8_t*)malloc((size_t)_scMax);
    for (int s = 1; s <= _scSlots && buf; s++) {
      char k[8];
      scKey(s, k, sizeof k);
      size_t len = pr.getBytesLength(k);
      if (len < 1 || len > (size_t)_scMax || pr.getBytes(k, buf, (size_t)_scMax) != len) continue;
      _scHave[s].have = true;
      _scHave[s].h = sceneHash(buf, len);
      _scHave[s].n = (uint16_t)len;
    }
    free(buf);
    uint8_t raw[5 * HUISDEV_SCENE_MAX_SCHED];
    size_t len = pr.getBytesLength("sched");
    if (len && len % 5 == 0 && len <= sizeof raw && pr.getBytes("sched", raw, sizeof raw) == len) {
      for (size_t i = 0; i < len; i += 5) {
        ScSched e;
        e.slot = raw[i];
        e.at = (uint32_t)raw[i + 1] | ((uint32_t)raw[i + 2] << 8) | ((uint32_t)raw[i + 3] << 16) | ((uint32_t)raw[i + 4] << 24);
        if (e.slot >= 1 && e.slot <= _scSlots && e.at >= HUISDEV_SCENE_TIME_MIN && (int)_scNv.size() < _scSched) _scNv.push_back(e);
      }
      std::sort(_scNv.begin(), _scNv.end(), schedLess);
    }
    pr.end();
    _scList = _scNv;  // verstreken regels (> at+24 u) vallen weg zodra de klok geldig is, in sceneTick()
  }
  void sceneWipeAll() {  // unclaim en resetClaim: scenes en lijst weg
    if (!_scOn) return;
    Preferences pr;
    if (pr.begin("huisdev_sc", false)) {
      for (int s = 1; s <= HUISDEV_SCENE_MAX_SLOTS; s++) {
        char k[8];
        scKey(s, k, sizeof k);
        if (pr.isKey(k)) pr.remove(k);
      }
      if (pr.isKey("sched")) pr.remove("sched");
      pr.end();
    }
    for (auto& s : _scHave) s = ScSlot();
    _scList.clear();
    _scNv.clear();
    if (_run.active) _run.slot = 0;  // de lopende scene speelt zijn eigen kopie door
  }
  // ---- afspelen ----
  HdResult scenePlayCore(int slot, const uint8_t* d, size_t n, uint32_t h, uint32_t elapsedS) {
    if (_run.active && _run.h == h) return HdResult::applied();  // dezelfde scene speelt al: niets doen
    if (!_onPlay) return HdResult::rejected("niet beschikbaar");
    const uint32_t gen = _runGen;
    HdResult r = _onPlay(slot, d, n, elapsedS);
    if (String(r.result) != "rejected" && _runGen == gen) sceneRunning(slot, h, 0);
    return r;
  }
  void sceneTick() {  // geplande starts en het einde van een scene met `until`
    if (!_scOn) return;
    const uint32_t now = sceneNow();
    if (!now) return;
    if (_run.active && _run.until && now >= _run.until) _run.active = false;
    while (!_scList.empty() && _scList.front().at <= now) {
      const ScSched e = _scList.front();
      _scList.erase(_scList.begin());  // uitgevoerd of overgeslagen: telt niet meer mee (NVS blijft zoals het was)
      const uint32_t late = now - e.at;
      if (late > HUISDEV_SCENE_LATE_S) continue;
      if (e.slot < 1 || e.slot > _scSlots || !_scHave[e.slot].have) continue;  // leeg slot: overslaan
      ScBuf b;
      if (!b.alloc(_scHave[e.slot].n) || scGet(e.slot, b.p, b.n) != b.n) continue;
      scenePlayCore(e.slot, b.p, b.n, sceneHash(b.p, b.n), late);  // `rejected` (te laat voor het device): overgeslagen
    }
  }
  // ---- state ----
  void sceneState(String& out) {
    const uint32_t now = sceneNow();
    out += ",\"scenes\":{\"have\":{";
    bool first = true;
    for (int s = 1; s <= _scSlots; s++) {
      if (!_scHave[s].have) continue;
      if (!first) out += ',';
      first = false;
      out += '"';
      out += String(s);
      out += "\":{\"h\":\"";
      hex8(out, _scHave[s].h);
      out += "\",\"n\":" + String((uint32_t)_scHave[s].n) + "}";
    }
    out += "}";
    if (_run.active) {
      out += ",\"run\":{\"slot\":" + String(_run.slot) + ",\"h\":\"";
      hex8(out, _run.h);
      out += "\",\"until\":";
      out += (_run.until && now) ? String(_run.until) : String("null");
      out += "}";
    }
    out += ",\"sched\":[";
    first = true;
    for (auto& e : _scList) {
      if (!first) out += ',';
      first = false;
      out += "{\"slot\":" + String((int)e.slot) + ",\"at\":" + String(e.at) + "}";
    }
    out += "],\"time\":";
    out += now ? String(now) : String("null");
    out += "}";
  }
  // ---- routes ----
  static int sceneReply(String& out, const HdResult& r, bool withHash = false, uint32_t h = 0) {
    const bool ok = String(r.result) != "rejected";
    out = String("{\"ok\":") + (ok ? "true" : "false") + ",\"result\":\"" + r.result + "\",\"reason\":\"";
    jesc(out, r.reason);
    out += '"';
    if (ok && withHash) {
      out += ",\"hash\":\"";
      hex8(out, h);
      out += '"';
    }
    out += '}';
    return 200;
  }
  // 0 = geen `slot` in het verzoek, -1 = ongeldig (geen geheel getal of buiten 1..slots), anders het slot
  int sceneSlotArg(const String& body) {
    if (!hdJsonHas(body, "slot")) return 0;
    double v = hdJsonNum(body, "slot", NAN);
    if (isnan(v) || fabs(v - round(v)) > 1e-9 || v < 1 || v > _scSlots) return -1;
    return (int)lround(v);
  }
  // Leest en decodeert "data". 0 = gelukt (b.n kan 0 zijn: leeg), anders de HTTP-status met het antwoord al in `out`.
  int sceneDecode(const String& body, ScBuf& b, String& out) {
    const int n = (int)body.length();
    const int i = hdKeyPos(body, "data");
    if (i < 0 || body[i] != '"') return err(out, 422, "bad_data", "data ontbreekt of is geen tekst");
    const char* s = body.c_str();
    int j = i + 1;
    bool esc = false;
    while (j < n && s[j] != '"') {
      if (s[j] == '\\') {
        esc = true;
        j += 2;
      } else {
        j++;
      }
    }
    if (j >= n) return err(out, 422, "bad_data", "data niet afgesloten");
    String hold;
    const char* p = s + i + 1;
    size_t len = (size_t)(j - i - 1);
    if (esc) {  // bv. "\/" van een JSON-schrijver die / escaped: eerst de escapes uitpakken
      hold = hdJsonStr(body, "data", "\x01");
      if (hold == "\x01") return err(out, 422, "bad_data", "ongeldige escape in data");
      p = hold.c_str();
      len = hold.length();
    }
    const long dn = hdB64Decode(p, len, nullptr);
    if (dn < 0) return err(out, 422, "bad_data", "geen geldig base64");
    if (dn > _scMax) return err(out, 422, "too_large", "scene te groot");
    if (dn > 0) {
      if (!b.alloc((size_t)dn)) return sceneReply(out, HdResult::rejected("geen geheugen"));
      hdB64Decode(p, len, b.p);
    }
    return 0;
  }
  int sceneRoute(const String& path, const String& body, String& out) {
    const size_t lim = (size_t)((_scMax + 2) / 3) * 4 + 256;  // ceil(max_bytes/3)*4 + 256, vóór het ontleden
    if (body.length() > lim) return err(out, 422, "too_large", "body te groot");
    if (path == "/huisdev/scene/store") return sceneStore(body, out);
    if (path == "/huisdev/scene/play") return scenePlay(body, out);
    if (path == "/huisdev/scene/stop") return sceneStop(body, out);
    return sceneSchedule(body, out);
  }
  int sceneStore(const String& body, String& out) {
    const int slot = sceneSlotArg(body);
    if (slot <= 0) return err(out, 422, "bad_slot", slot == 0 ? "slot ontbreekt" : "slot buiten bereik");
    ScBuf b;
    if (int c = sceneDecode(body, b, out)) return c;
    char k[8];
    scKey(slot, k, sizeof k);
    if (b.n == 0) {  // "data":"" wist het slot
      if (!_scHave[slot].have) return sceneReply(out, HdResult::applied());
      if (scTooOften()) return sceneReply(out, HdResult::rejected("te vaak"));
      if (!scDel(k)) return sceneReply(out, HdResult::rejected("geen geheugen"));
      scStamp();
      _scHave[slot] = ScSlot();
      if (_run.active && _run.slot == slot) _run.slot = 0;
      return sceneReply(out, HdResult::applied());
    }
    const uint32_t h = sceneHash(b.p, b.n);
    if (_scHave[slot].have && _scHave[slot].h == h && _scHave[slot].n == b.n && scSame(slot, b.p, b.n)) return sceneReply(out, HdResult::applied(), true, h);  // niets schrijven
    String why = _onCheck ? _onCheck(b.p, b.n) : String();
    if (why.length()) return sceneReply(out, HdResult::rejected(why));
    if (scTooOften()) return sceneReply(out, HdResult::rejected("te vaak"));
    if (!scPut(k, b.p, b.n)) return sceneReply(out, HdResult::rejected("geen geheugen"));  // `have` blijft zoals het was
    scStamp();
    _scHave[slot].have = true;
    _scHave[slot].h = h;
    _scHave[slot].n = (uint16_t)b.n;
    if (_run.active && _run.slot == slot) _run.slot = 0;  // het device speelt zijn eigen kopie door
    return sceneReply(out, HdResult::applied(), true, h);
  }
  int scenePlay(const String& body, String& out) {
    const bool hasData = hdJsonHas(body, "data"), hasSlot = hdJsonHas(body, "slot");
    if (hasData && hasSlot) return err(out, 422, "bad_value", "slot of data, niet allebei");
    if (!hasData && !hasSlot) return err(out, 422, "bad_slot", "slot of data ontbreekt");
    ScBuf b;
    int slot = 0;
    if (hasSlot) {
      slot = sceneSlotArg(body);
      if (slot < 1) return err(out, 422, "bad_slot", "slot buiten bereik");
      if (!_scHave[slot].have) return sceneReply(out, HdResult::rejected("slot leeg"));
      if (!b.alloc(_scHave[slot].n)) return sceneReply(out, HdResult::rejected("geen geheugen"));
      if (scGet(slot, b.p, b.n) != b.n) return sceneReply(out, HdResult::rejected("slot onleesbaar"));
    } else {
      if (int c = sceneDecode(body, b, out)) return c;
      if (b.n == 0) return err(out, 422, "bad_data", "lege scene");
      if (_run.active && _run.h == sceneHash(b.p, b.n)) return sceneReply(out, HdResult::applied(), true, _run.h);  // speelt al: ook de check overslaan
      String why = _onCheck ? _onCheck(b.p, b.n) : String();
      if (why.length()) return sceneReply(out, HdResult::rejected(why));
    }
    const uint32_t h = sceneHash(b.p, b.n);
    HdResult r = scenePlayCore(slot, b.p, b.n, h, 0);
    return sceneReply(out, r, true, h);
  }
  int sceneStop(const String& body, String& out) {
    int slot = 0;
    if (hdJsonHas(body, "slot")) {
      slot = sceneSlotArg(body);
      if (slot < 1) return err(out, 422, "bad_slot", "slot buiten bereik");
      if (!(_run.active && _run.slot == slot)) return sceneReply(out, HdResult::applied());  // dat slot speelt niet
    }
    if (!_onStop) return sceneReply(out, _run.active ? HdResult::rejected("niet beschikbaar") : HdResult::applied());
    HdResult r = _onStop(slot);  // bij slot 0 ook als de header denkt dat er niets speelt: stoppen is altijd veilig
    if (String(r.result) != "rejected") sceneIdle();
    return sceneReply(out, r);
  }
  int sceneSchedule(const String& body, String& out) {
    const int n = (int)body.length();
    const char* s = body.c_str();
    int i = hdKeyPos(body, "entries");
    if (i < 0 || body[i] != '[') return err(out, 422, "bad_value", "entries ontbreekt");
    std::vector<ScSched> list;
    auto ws = [&]() {
      while (i < n && (s[i] == ' ' || s[i] == '\t' || s[i] == '\n' || s[i] == '\r')) i++;
    };
    i++;
    ws();
    if (i < n && s[i] == ']') {
      i++;
    } else {
      for (;;) {
        ws();
        if (i >= n || s[i] != '{') return err(out, 422, "bad_value", "entries is geen lijst van objecten");
        const int st = i;
        int depth = 0;
        for (; i < n; i++) {
          if (s[i] == '"') {
            for (i++; i < n && s[i] != '"'; i += (s[i] == '\\' && i + 1 < n) ? 2 : 1) {}
          } else if (s[i] == '{') {
            depth++;
          } else if (s[i] == '}' && --depth == 0) {
            break;
          }
        }
        if (i >= n) return err(out, 422, "bad_value", "entries niet afgesloten");
        const String obj = body.substring((unsigned)st, (unsigned)(i + 1));
        i++;
        if ((int)list.size() >= _scSched) return err(out, 422, "bad_value", "meer regels dan sched");
        const double sv = hdJsonNum(obj, "slot", NAN), av = hdJsonNum(obj, "at", NAN);
        if (isnan(sv) || fabs(sv - round(sv)) > 1e-9 || sv < 1 || sv > _scSlots) return err(out, 422, "bad_slot", "slot buiten bereik");
        if (isnan(av) || fabs(av - round(av)) > 1e-9 || av < (double)HUISDEV_SCENE_TIME_MIN || av > 4294967295.0) return err(out, 422, "bad_value", "at buiten bereik");
        ScSched e;
        e.slot = (uint8_t)lround(sv);
        e.at = (uint32_t)llround(av);
        list.push_back(e);
        ws();
        if (i < n && s[i] == ',') {
          i++;
          continue;
        }
        if (i < n && s[i] == ']') break;
        return err(out, 422, "bad_value", "entries niet afgesloten");
      }
    }
    std::sort(list.begin(), list.end(), schedLess);
    if (!list.empty()) {
      const uint32_t now = sceneNow();
      if (!now) return sceneReply(out, HdResult::rejected("geen tijd"));
      for (auto& e : list)
        if ((uint64_t)e.at > (uint64_t)now + HUISDEV_SCENE_AHEAD_S) return err(out, 422, "bad_value", "at verder dan 14 dagen vooruit");
    }
    if (schedEq(list, _scNv)) return sceneReply(out, HdResult::applied());  // zelfde lijst: niets schrijven
    if (scTooOften()) return sceneReply(out, HdResult::rejected("te vaak"));
    bool ok;
    if (list.empty()) {
      ok = scDel("sched");
    } else {
      uint8_t raw[5 * HUISDEV_SCENE_MAX_SCHED];
      size_t o = 0;
      for (auto& e : list) {
        raw[o++] = e.slot;
        raw[o++] = (uint8_t)e.at;
        raw[o++] = (uint8_t)(e.at >> 8);
        raw[o++] = (uint8_t)(e.at >> 16);
        raw[o++] = (uint8_t)(e.at >> 24);
      }
      ok = scPut("sched", raw, o);
    }
    if (!ok) return sceneReply(out, HdResult::rejected("geen geheugen"));
    scStamp();
    _scNv = list;
    _scList = list;
    return sceneReply(out, HdResult::applied());
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
