// Stub van de blokkerende ESP32 WebServer. De test roept `dispatch()` aan zoals de echte server een verzoek afhandelt:
// de eerste route die op methode en pad past wint, en past er geen, dan komt de onNotFound van de sketch (hier: 404 "niet gevonden").
#pragma once
#ifdef HUISDEV_NO_WEBSERVER
#error "WebServer.h mag niet worden binnengehaald met HUISDEV_NO_WEBSERVER"
#endif
#include <functional>
#include <string>
#include <vector>
#include "Arduino.h"

// Dezelfde waarden als de echte core (WebServer/src/HTTP_Method.h)
enum HTTPMethod { HTTP_ANY = 0, HTTP_GET = 1, HTTP_HEAD = 2, HTTP_POST = 3, HTTP_PUT = 4, HTTP_PATCH = 5, HTTP_DELETE = 6, HTTP_OPTIONS = 7 };

class Uri {
 public:
  explicit Uri(const char* u) : pattern(u) {}
  virtual ~Uri() {}
  virtual bool canHandle(const std::string& requestUri) const { return requestUri == pattern; }
  std::string pattern;
};

class WebServer {
 public:
  struct Route {
    std::string pattern;
    bool glob;
    HTTPMethod method;
    std::function<void()> fn;
  };
  std::vector<Route> routes;
  std::vector<std::string> collected;
  String plain, tok, ctype, content, cache;
  int code = 0;
  HTTPMethod curMethod = HTTP_GET;
  std::string curUri;

  void on(const char* uri, HTTPMethod m, std::function<void()> f) { routes.push_back({uri, false, m, f}); }
  void on(const Uri& uri, HTTPMethod m, std::function<void()> f);  // UriGlob.h
  // zoals de echte: collectHeaders vervangt de lijst
  void collectHeaders(const char* keys[], size_t n) {
    collected.clear();
    for (size_t i = 0; i < n; i++) collected.push_back(keys[i]);
  }
  HTTPMethod method() { return curMethod; }
  String uri() { return String(curUri.c_str()); }
  String arg(const String& name) { return name == "plain" ? plain : String(); }
  String header(const char* name) {
    for (auto& c : collected)
      if (c == name) return tok;
    return String();  // niet verzamelde headers zijn leeg
  }
  void sendHeader(const String& n, const String& v) {
    if (n == "Cache-Control") cache = v;
  }
  void send(int c, const char* type, const String& body) {
    code = c;
    ctype = type;
    content = body;
  }
  // Een verzoek afhandelen. true = een route nam het, false = onNotFound van de sketch.
  bool dispatch(HTTPMethod m, const std::string& u) {
    curMethod = m;
    curUri = u;
    for (auto& r : routes) {
      if (r.method != HTTP_ANY && r.method != m) continue;
      bool match = r.glob ? (r.pattern.size() >= 2 && r.pattern.compare(r.pattern.size() - 2, 2, "/*") == 0 &&
                             u.compare(0, r.pattern.size() - 1, r.pattern, 0, r.pattern.size() - 1) == 0)
                          : r.pattern == u;
      if (match) {
        r.fn();
        return true;
      }
    }
    send(404, "text/plain", "niet gevonden");  // de onNotFound van de sketch
    return false;
  }
};
