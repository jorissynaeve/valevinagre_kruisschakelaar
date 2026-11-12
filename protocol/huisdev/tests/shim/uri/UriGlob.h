// Stub van uri/UriGlob.h. De stub-WebServer kent alleen het patroon "/prefix/*".
#pragma once
#include "WebServer.h"

class UriGlob : public Uri {
 public:
  explicit UriGlob(const char* u) : Uri(u) {}
};

inline void WebServer::on(const Uri& uri, HTTPMethod m, std::function<void()> f) {
  routes.push_back({uri.pattern, dynamic_cast<const UriGlob*>(&uri) != nullptr, m, f});
}
