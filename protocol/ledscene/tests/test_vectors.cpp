// Host-test van ledscene.h: de gedeelde vectoren (vectors/vectors.txt, geschreven door tools/gen_vectors.py uit scenes.py), afwijzen van ongeldige
// payloads en een fuzz-lus. Bouwen en draaien: tests/run.sh (gcc-container met ASAN en UBSAN).
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "../ledscene.h"

using namespace ledscene;

static int g_fail = 0, g_checks = 0;
#define CHECK(cond, ...)                          \
  do {                                            \
    g_checks++;                                   \
    if (!(cond)) {                                \
      g_fail++;                                   \
      if (g_fail <= 30) {                         \
        printf("FOUT %s:%d: ", __FILE__, __LINE__); \
        printf(__VA_ARGS__);                      \
        printf("\n");                             \
      }                                           \
    }                                             \
  } while (0)

static int hexv(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

static bool fromHex(const std::string& h, std::vector<uint8_t>* out) {
  out->clear();
  if (h == "-") return true;
  if (h.size() % 2) return false;
  for (size_t i = 0; i < h.size(); i += 2) {
    int a = hexv(h[i]), b = hexv(h[i + 1]);
    if (a < 0 || b < 0) return false;
    out->push_back((uint8_t)(a * 16 + b));
  }
  return true;
}

// ---- vectoren

struct Cur {
  std::string name;
  int n = 0;
  std::vector<uint8_t> bytes;
  Scene sc;
  bool ok = false;
};

static int g_scenes = 0, g_times = 0, g_bad = 0;

static void runVectors(const char* path) {
  std::ifstream f(path);
  if (!f) {
    printf("FOUT: kan %s niet lezen\n", path);
    g_fail++;
    return;
  }
  std::string line;
  Cur cur;
  int lineNo = 0;
  while (std::getline(f, line)) {
    lineNo++;
    if (!line.empty() && line.back() == '\r') line.pop_back();
    if (line.empty() || line[0] == '#') continue;
    std::istringstream is(line);
    std::string kw;
    is >> kw;
    if (kw == "scene") {
      std::string hex;
      is >> cur.name >> cur.n >> hex;
      CHECK(fromHex(hex, &cur.bytes), "regel %d: hex", lineNo);
      const char* why = validate(cur.bytes.data(), cur.bytes.size(), 0, &cur.sc);
      cur.ok = why == nullptr;
      CHECK(cur.ok, "scene %s wordt afgewezen: %s", cur.name.c_str(), why ? why : "");
      g_scenes++;
    } else if (kw == "h") {
      std::string h;
      is >> h;
      char buf[16];
      snprintf(buf, sizeof buf, "%08x", (unsigned)fnv1a32(cur.bytes.data(), cur.bytes.size()));
      CHECK(h == buf, "%s: hash %s != %s", cur.name.c_str(), h.c_str(), buf);
    } else if (kw == "total") {
      long long want;
      is >> want;
      if (!cur.ok) continue;
      uint64_t got = totalMs(cur.sc);
      bool isBounded = bounded(cur.sc);
      CHECK(want == -1 ? (!isBounded && got == UINT64_MAX) : (isBounded && got == (uint64_t)want), "%s: totalMs %llu, verwacht %lld", cur.name.c_str(),
            (unsigned long long)got, want);
    } else if (kw == "t") {
      unsigned long long t;
      std::string hex;
      is >> t >> hex;
      if (!cur.ok) continue;
      std::vector<uint8_t> want;
      CHECK(fromHex(hex, &want) && want.size() == (size_t)cur.n * 6, "regel %d: lengte van de verwachte uitvoer", lineNo);
      if (want.size() != (size_t)cur.n * 6) continue;
      std::vector<uint16_t> lin((size_t)cur.n * 3, 0xABCD);  // exacte grootte: ASAN ziet een schrijffout buiten het bereik
      render(cur.sc, (uint32_t)t, cur.n, lin.data());
      int diffs = 0;
      for (size_t i = 0; i < lin.size(); i++) {
        uint16_t w = (uint16_t)(want[2 * i] | (want[2 * i + 1] << 8));
        if (lin[i] != w && diffs++ < 3) CHECK(false, "%s t=%llu: u16 %zu (led %zu kanaal %zu) = %u, verwacht %u", cur.name.c_str(), t, i, i / 3, i % 3, lin[i], w);
      }
      if (!diffs) g_checks++;
      g_times++;
    } else if (kw == "bad") {
      std::string name, hex, reason;
      size_t mx;
      is >> name >> mx >> hex;
      std::getline(is, reason);
      if (!reason.empty() && reason[0] == ' ') reason.erase(0, 1);
      std::vector<uint8_t> b;
      CHECK(fromHex(hex, &b), "regel %d: hex", lineNo);
      const char* why = validate(b.data(), b.size(), mx, nullptr);
      CHECK(why != nullptr, "bad %s wordt geaccepteerd", name.c_str());
      if (why) CHECK(reason == why, "bad %s: reden '%s', verwacht '%s'", name.c_str(), why, reason.c_str());
      g_bad++;
    } else {
      CHECK(false, "regel %d: onbekend sleutelwoord %s", lineNo, kw.c_str());
    }
  }
}

// ---- elke geldige scène met één byte minder of meer wordt afgewezen

static std::vector<std::vector<uint8_t>> g_valid;  // geldige scènes uit de vectoren (voor mutaties)

static void collectValid(const char* path) {
  std::ifstream f(path);
  std::string line;
  while (std::getline(f, line)) {
    if (line.rfind("scene ", 0) != 0) continue;
    std::istringstream is(line);
    std::string kw, name, hex;
    int n;
    is >> kw >> name >> n >> hex;
    std::vector<uint8_t> b;
    fromHex(hex, &b);
    bool dup = false;
    for (auto& v : g_valid) dup |= v == b;
    if (!dup) g_valid.push_back(b);
  }
}

static void runTruncations() {
  for (auto& b : g_valid) {
    for (size_t k = 0; k < b.size(); k++) CHECK(validate(b.data(), k, 0, nullptr) != nullptr, "afgekapt tot %zu van %zu bytes wordt geaccepteerd", k, b.size());
    std::vector<uint8_t> x = b;
    x.push_back(0);
    CHECK(validate(x.data(), x.size(), 0, nullptr) != nullptr, "extra byte wordt geaccepteerd");
    CHECK(validate(b.data(), b.size(), b.size() - 1, nullptr) != nullptr, "max_bytes wordt niet afgedwongen");
    CHECK(validate(b.data(), b.size(), b.size(), nullptr) == nullptr, "max_bytes == lengte moet mogen");
  }
}

// ---- handmatige ongeldige payloads uit de opdracht (naast de bad-regels uit de vectoren)

static void runInvalid() {
  // nachtlamp: 1 frame, 1 span
  std::vector<uint8_t> base = {'L', 'S', 1, 1, 1, 1, 100, 0, 0, 0, 20, 0, 0xFF, 0xFF, 0, 0,  // kop
                               0, 0, 0, 0xFF, 0, 0, 1,                                        // frame: dt ease lum scroll n
                               0, 0, 0, 65, 38, 10, 65, 38, 10};                              // span
  Scene s;
  CHECK(validate(base.data(), base.size(), 0, &s) == nullptr, "basis moet geldig zijn");
  auto bad = [&](const char* what, size_t off, uint8_t v) {
    std::vector<uint8_t> x = base;
    x[off] = v;
    CHECK(validate(x.data(), x.size(), 0, nullptr) != nullptr, "%s wordt geaccepteerd", what);
  };
  bad("nkf=0", 3, 0);
  bad("dt != 0 bij loops=1", 16, 1);
  bad("ease=3", 18, 3);
  bad("n=17", 22, 17);
  bad("flags=2", 25, 2);
  bad("ver=2", 2, 2);
  bad("end=2", 5, 2);
  bad("tick=0", 6, 0);
  CHECK(validate(nullptr, 0, 0, nullptr) != nullptr, "nullptr/0 wordt geaccepteerd");
  // dt=0 in het eerste van twee frames
  std::vector<uint8_t> two;
  // frame0: dt=5 ease=1 lum=255 scroll=0 n=0 | frame1: dt=0 ease=0 lum=255 scroll=0 n=0
  two = {'L', 'S', 1, 2, 1, 1, 100, 0, 0, 0, 20, 0, 0xFF, 0xFF, 0, 0, 5, 0, 1, 255, 0, 0, 0, 0, 0, 0, 255, 0, 0, 0};
  CHECK(validate(two.data(), two.size(), 0, &s) == nullptr, "twee lege frames moeten geldig zijn");
  two[16] = 0;
  CHECK(validate(two.data(), two.size(), 0, nullptr) != nullptr, "dt=0 in een niet-laatste frame wordt geaccepteerd");
}

// ---- fuzz

static uint64_t g_rng = 0x9E3779B97F4A7C15ull;
static uint32_t rnd() {  // xorshift64*, vaste seed
  g_rng ^= g_rng >> 12;
  g_rng ^= g_rng << 25;
  g_rng ^= g_rng >> 27;
  return (uint32_t)((g_rng * 0x2545F4914F6CDD1Dull) >> 32);
}

static void runFuzz(int iters) {
  int valid = 0, rendered = 0;
  static const int kNs[] = {1, 2, 7, 20, 64, 300};
  for (int it = 0; it < iters; it++) {
    std::vector<uint8_t> b;
    int mode = rnd() % 4;
    if (mode == 0 || g_valid.empty()) {  // willekeurige bytes, soms met een geldige kop
      size_t len = rnd() % 220;
      b.resize(len);
      for (auto& x : b) x = (uint8_t)rnd();
      if (len >= 16 && (rnd() & 1)) {
        b[0] = 'L';
        b[1] = 'S';
        b[2] = 1;
        b[3] = (uint8_t)(1 + rnd() % 3);
        b[5] &= 1;
        b[6] = (uint8_t)(1 + rnd() % 200);
        b[7] = 0;
        b[11] = 0;
        b[14] = b[15] = 0;
        b[8] = b[9] = 0;
      }
    } else {  // een geldige scène met een paar gemuteerde bytes (en soms afgekapt of verlengd)
      b = g_valid[rnd() % g_valid.size()];
      int muts = 1 + rnd() % 3;
      for (int m = 0; m < muts; m++) b[rnd() % b.size()] = (uint8_t)rnd();
      if (mode == 3 && rnd() % 3 == 0) b.resize(rnd() % (b.size() + 1));
    }
    std::vector<uint8_t> exact = b;  // exact gealloceerd: ASAN ziet een lezing voorbij het einde
    Scene s;
    const char* why = validate(exact.data(), exact.size(), 1024, &s);
    if (why) continue;
    valid++;
    int n = kNs[rnd() % 6];
    for (int k = 0; k < 3; k++) {
      std::vector<uint16_t> lin((size_t)n * 3);
      uint32_t t = rnd() % 8 == 0 ? rnd() : (s.spanMs ? rnd() % (s.spanMs * 3 + 1000) : rnd() % 5000);
      render(s, t, n, lin.data());
      rendered++;
    }
  }
  printf("fuzz: %d iteraties, %d geldig bevonden, %d renders\n", iters, valid, rendered);
  CHECK(valid > 200, "te weinig geldige scenes in de fuzz (%d): de test beproeft de renderer nauwelijks", valid);
}

int main(int argc, char** argv) {
  const char* path = argc > 1 ? argv[1] : "vectors/vectors.txt";
  runVectors(path);
  collectValid(path);
  runTruncations();
  runInvalid();
  runFuzz(20000);
  printf("vectoren: %d scenes, %d tijdstippen, %d ongeldige; %d controles, %d fouten\n", g_scenes, g_times, g_bad, g_checks, g_fail);
  if (g_scenes < 10 || g_times < 100) {
    printf("FOUT: te weinig vectoren gelezen\n");
    return 1;
  }
  return g_fail ? 1 : 0;
}
