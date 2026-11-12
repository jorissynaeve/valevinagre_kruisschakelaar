// Stub van de blokkerende ESP32 WebServer. De test roept de geregistreerde handlers zelf aan.
#pragma once
#ifdef HUISDEV_NO_WEBSERVER
#error "WebServer.h mag niet worden binnengehaald met HUISDEV_NO_WEBSERVER"
#endif
#include <functional>
#include <map>
#include <string>
#include <vector>
#include "Arduino.h"

enum HTTPMethod { HTTP_ANY = 0, HTTP_GET = 1, HTTP_POST = 3 };

class WebServer {
 public:
  std::map<std::string, std::function<void()>> routes;
  std::vector<std::string> collected;
  String plain, tok, ctype, content, cache;
  int code = 0;

  void on(const char* uri, HTTPMethod m, std::function<void()> f) { routes[std::string(m == HTTP_GET ? "GET " : "POST ") + uri] = f; }
  // zoals de echte: collectHeaders vervangt de lijst
  void collectHeaders(const char* keys[], size_t n) {
    collected.clear();
    for (size_t i = 0; i < n; i++) collected.push_back(keys[i]);
  }
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
};
