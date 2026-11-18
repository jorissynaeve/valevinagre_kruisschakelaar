// NVS-stub met het gedrag van Arduino-ESP32: een alleen-lezen begin() op een niet bestaande namespace faalt,
// en schrijven op een alleen-lezen handle doet niets (geeft 0). Blobs (putBytes/getBytes) volgen de semantiek van de echte Preferences:
// getBytes geeft 0 als de buffer te klein is. Voor de tests: een quota op het totaal aantal opgeslagen bytes (NVS vol: put geeft 0)
// en een teller van schrijfacties (elke put* of remove op een schrijfbare handle, ook als hij mislukt).
#pragma once
#include <map>
#include <string>
#include "Arduino.h"

inline std::map<std::string, std::map<std::string, std::string>> g_prefs;
inline long g_nvs_quota = -1;  // < 0: onbeperkt, anders het maximale totaal van alle opgeslagen waarden in bytes
inline long g_nvs_writes = 0;  // aantal schrijfacties sinds de start van de driver

inline long nvs_total() {
  long t = 0;
  for (auto& ns : g_prefs)
    for (auto& kv : ns.second) t += (long)kv.second.size();
  return t;
}

class Preferences {
  std::string ns;
  bool ro = true, open = false;

  size_t put(const char* key, const void* v, size_t len) {
    if (!open || ro || !key) return 0;
    g_nvs_writes++;
    auto& m = g_prefs[ns];
    auto it = m.find(key);
    long old = it == m.end() ? 0 : (long)it->second.size();
    if (g_nvs_quota >= 0 && nvs_total() - old + (long)len > g_nvs_quota) return 0;  // NVS vol: de oude waarde blijft staan
    m[key] = std::string((const char*)v, len);
    return len;
  }

 public:
  bool begin(const char* name, bool readOnly = false) {
    if (readOnly && !g_prefs.count(name)) return false;
    ns = name;
    ro = readOnly;
    open = true;
    if (!readOnly) g_prefs[ns];
    return true;
  }
  void end() { open = false; }
  String getString(const char* key, const String& def = String()) {
    if (!open) return def;
    auto& m = g_prefs[ns];
    auto it = m.find(key);
    return it == m.end() ? def : String(it->second.c_str());
  }
  size_t putString(const char* key, const String& v) { return put(key, v.c_str(), v.length()); }
  bool isKey(const char* key) {
    if (!open || !key) return false;
    return g_prefs[ns].count(key) > 0;
  }
  size_t getBytesLength(const char* key) {
    if (!open || !key) return 0;
    auto& m = g_prefs[ns];
    auto it = m.find(key);
    return it == m.end() ? 0 : it->second.size();
  }
  size_t getBytes(const char* key, void* buf, size_t maxLen) {
    if (!open || !key || !buf || !maxLen) return 0;
    size_t len = getBytesLength(key);
    if (!len || len > maxLen) return 0;  // zoals de echte: een te kleine buffer geeft 0
    memcpy(buf, g_prefs[ns][key].data(), len);
    return len;
  }
  size_t putBytes(const char* key, const void* value, size_t len) { return put(key, value, len); }
  bool remove(const char* key) {
    if (!open || ro) return false;
    g_nvs_writes++;
    return g_prefs[ns].erase(key) > 0;
  }
};
