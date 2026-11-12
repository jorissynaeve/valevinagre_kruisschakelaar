// Minimale host-shim van Arduino.h voor de tests van huisdev.h.
// Doel: de echte header op een PC draaien. String volgt het gedrag van Arduino-ESP32 voor wat huisdev.h gebruikt.
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <math.h>
#include <string>
#include <type_traits>

inline uint32_t g_millis = 0;  // door de tests bestuurd
inline uint32_t millis() { return g_millis; }

class String {
  std::string s;

 public:
  String() {}
  String(const char* c) : s(c ? c : "") {}
  String(const String&) = default;
  String(String&&) = default;
  String& operator=(const String&) = default;
  String& operator=(String&&) = default;
  String& operator=(const char* c) {
    s = c ? c : "";
    return *this;
  }
  explicit String(char c) : s(1, c) {}                          // Arduino: een teken
  explicit String(unsigned char v, unsigned char = 10) { s = std::to_string((unsigned)v); }
  explicit String(int v, unsigned char = 10) { s = std::to_string(v); }
  explicit String(unsigned int v, unsigned char = 10) { s = std::to_string(v); }
  explicit String(long v, unsigned char = 10) { s = std::to_string(v); }
  explicit String(unsigned long v, unsigned char = 10) { s = std::to_string(v); }
  explicit String(long long v, unsigned char = 10) { s = std::to_string(v); }
  explicit String(unsigned long long v, unsigned char = 10) { s = std::to_string(v); }
  explicit String(double v, unsigned int dec = 2) {  // Arduino: dtostrf-achtig; hier %.*f, nan/inf zoals libc
    char b[400];
    snprintf(b, sizeof b, "%.*f", (int)dec, v);
    s = b;
  }
  explicit String(float v, unsigned int dec = 2) : String((double)v, dec) {}

  size_t length() const { return s.size(); }
  const char* c_str() const { return s.c_str(); }
  bool reserve(size_t n) {
    s.reserve(n);
    return true;
  }
  char operator[](size_t i) const { return i < s.size() ? s[i] : 0; }
  char& operator[](size_t i) { return s[i]; }
  String& operator+=(const String& o) {
    s += o.s;
    return *this;
  }
  String& operator+=(const char* o) {
    if (o) s += o;
    return *this;
  }
  String& operator+=(char c) {
    s += c;
    return *this;
  }
  bool concat(const String& o) {
    s += o.s;
    return true;
  }
  bool operator==(const String& o) const { return s == o.s; }
  bool operator==(const char* o) const { return s == (o ? o : ""); }
  bool operator!=(const String& o) const { return s != o.s; }
  bool operator!=(const char* o) const { return s != (o ? o : ""); }
  int indexOf(const String& needle, unsigned int from = 0) const {
    size_t p = s.find(needle.s, from);
    return p == std::string::npos ? -1 : (int)p;
  }
  int indexOf(char c, unsigned int from = 0) const {
    size_t p = s.find(c, from);
    return p == std::string::npos ? -1 : (int)p;
  }
  bool startsWith(const String& p) const { return s.compare(0, p.s.size(), p.s) == 0 && s.size() >= p.s.size(); }
  bool endsWith(const String& p) const { return s.size() >= p.s.size() && s.compare(s.size() - p.s.size(), p.s.size(), p.s) == 0; }
  String substring(unsigned int from) const { return from >= s.size() ? String() : String(s.substr(from).c_str()); }
  String substring(unsigned int from, unsigned int to) const {
    if (to < from) {
      unsigned int t = from;
      from = to;
      to = t;
    }
    if (from >= s.size()) return String();
    return String(s.substr(from, to - from).c_str());
  }
  void remove(unsigned int idx) {
    if (idx < s.size()) s.erase(idx);
  }
  void remove(unsigned int idx, unsigned int count) {
    if (idx < s.size()) s.erase(idx, count);
  }
  void replace(const String& a, const String& b) {
    if (a.s.empty()) return;
    size_t p = 0;
    while ((p = s.find(a.s, p)) != std::string::npos) {
      s.replace(p, a.s.size(), b.s);
      p += b.s.size();
    }
  }
  void toLowerCase() {
    for (auto& c : s) c = (char)tolower((unsigned char)c);
  }
  const std::string& std() const { return s; }
  friend String operator+(const String& a, const String& b) {
    String r(a);
    r += b;
    return r;
  }
  friend String operator+(const String& a, const char* b) {
    String r(a);
    r += b;
    return r;
  }
  friend String operator+(const char* a, const String& b) {
    String r(a);
    r += b;
    return r;
  }
  friend String operator+(const String& a, char b) {
    String r(a);
    r += b;
    return r;
  }
};
