// NVS-stub met het gedrag van Arduino-ESP32: een alleen-lezen begin() op een niet bestaande namespace faalt,
// en schrijven op een alleen-lezen handle doet niets (geeft 0).
#pragma once
#include <map>
#include <string>
#include "Arduino.h"

inline std::map<std::string, std::map<std::string, std::string>> g_prefs;

class Preferences {
  std::string ns;
  bool ro = true, open = false;

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
  size_t putString(const char* key, const String& v) {
    if (!open || ro) return 0;
    g_prefs[ns][key] = v.c_str();
    return v.length();
  }
  bool remove(const char* key) {
    if (!open || ro) return false;
    return g_prefs[ns].erase(key) > 0;
  }
};
