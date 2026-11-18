// ledscene.h: ledscene/1 (protocol/ledscene/LEDSCENE.md): valideren en renderen van een scène, header-only, puur C++17.
// Geen Arduino-includes en geen heap: compileert op de host (tests) en op de ESP32. Bit-identiek met addon/kkaddon/scenes.py
// (gedeelde vectoren: protocol/ledscene/vectors/vectors.txt).
//
//   ledscene::Scene sc;
//   const char* why = ledscene::validate(buf, len, max_bytes, &sc);   // nullptr = geldig, anders de reden (Nederlands, statische string)
//   if (!why) ledscene::render(sc, t_ms, nLeds, lin);                 // lin: nLeds*3 u16, lineair Q16 (r,g,b per led)
//
// `Scene` is een niet-bezittende view: de bytes moeten blijven bestaan zolang de scène gebruikt wordt.
#pragma once
#include <stddef.h>
#include <stdint.h>

#include "ledscene_lut.h"

namespace ledscene {

static const uint32_t kMaxSpanMs = 86400000UL;  // absolute grenzen (LEDSCENE.md §6)
static const uint16_t kMaxHoldS = 43200;
static const int kMaxFrames = 32;
static const int kMaxSpansPerFrame = 16;
static const int kMaxSpansTotal = 128;

struct Scene {
  const uint8_t* d = nullptr;  // de bytes (niet bezittend)
  size_t len = 0;
  uint8_t nkf = 0, loops = 0, end = 0, fade = 0;
  uint16_t tick = 0, hold = 0, resume = 0;
  uint32_t spanMs = 0;               // tick * som van de dt's (<= 86 400 000)
  uint64_t playMs = 0;               // loops * spanMs; UINT64_MAX bij loops == 0
  uint16_t off[kMaxFrames] = {};     // offset van elk frame in d
  uint32_t start[kMaxFrames] = {};   // starttijd van elk frame (ms)
};

namespace detail {

inline uint16_t rd16(const uint8_t* p) { return (uint16_t)(p[0] | (p[1] << 8)); }

// frame-velden
inline uint16_t fDt(const Scene& s, int k) { return rd16(s.d + s.off[k]); }
inline uint8_t fEase(const Scene& s, int k) { return s.d[s.off[k] + 2]; }
inline uint8_t fLum(const Scene& s, int k) { return s.d[s.off[k] + 3]; }
inline int16_t fScroll(const Scene& s, int k) { return (int16_t)rd16(s.d + s.off[k] + 4); }
inline int fN(const Scene& s, int k) { return s.d[s.off[k] + 6]; }
inline const uint8_t* fSpan(const Scene& s, int k, int j) { return s.d + s.off[k] + 7 + 9 * j; }

inline int64_t fmod64(int64_t x, int64_t m) {  // floor-modulo, resultaat >= 0
  int64_t r = x % m;
  return r < 0 ? r + m : r;
}

inline int64_t lutq(int64_t x) {  // LUT(x) uit LEDSCENE.md §5, x in Q8.8 (0..65280)
  int64_t i = x >> 8, f = x & 255;
  return ((int64_t)kLut[i] * (256 - f) + (int64_t)kLut[i + 1] * f + 128) >> 8;
}

inline int64_t ovl(int64_t s, int64_t e, int64_t lo, int64_t hi) {
  int64_t a = (e < hi ? e : hi) - (s > lo ? s : lo);
  return a > 0 ? a : 0;
}

struct Geo {
  int64_t S, E, len;  // begin (na verschuiving, 0..L-1), einde (S+len) en lengte in positie-eenheden
  uint8_t c1[3], c2[3];
};

inline int loadGeo(const Scene& s, int k, int64_t N, int64_t L, int64_t sh, Geo* g) {
  int ns = fN(s, k);
  for (int j = 0; j < ns; j++) {
    const uint8_t* sp = fSpan(s, k, j);
    int64_t unit = (sp[2] & 1) ? 256 : N;
    int64_t lu = (int64_t)(sp[1] == 0 ? 256 : sp[1]) * unit;
    if (lu > L) lu = L;
    g[j].S = fmod64((int64_t)sp[0] * unit + sh, L);
    g[j].len = lu;
    g[j].E = g[j].S + lu;
    for (int c = 0; c < 3; c++) {
      g[j].c1[c] = sp[3 + c];
      g[j].c2[c] = sp[6 + c];
    }
  }
  return ns;
}

// Q8.8-kleur van led i (LEDSCENE.md §4)
inline void px(const Geo* g, int ns, int64_t i, int64_t L, int64_t P[3]) {
  P[0] = P[1] = P[2] = 0;
  int64_t a = 0;  // tot nu toe gedekt deel van de led (0..256)
  int64_t lo = i * 256, hi = lo + 256;
  for (int j = 0; j < ns; j++) {
    int64_t S = g[j].S, E = g[j].E, lu = g[j].len;
    int64_t cov = ovl(S, E < L ? E : L, lo, hi) + (E > L ? ovl(0, E - L, lo, hi) : 0);
    if (cov > 256) cov = 256;
    if (cov <= 0) continue;
    int64_t d = fmod64(lo + 128 - S, L);
    int64_t u = d < lu ? d * 256 / lu : ((L - d) < (d - lu) ? 0 : 256);
    int64_t o = a + cov - 256;
    if (o < 0) o = 0;
    int64_t keep = a == 0 ? 256 : (a - o) * 256 / a;
    for (int c = 0; c < 3; c++) {
      int64_t C = (int64_t)g[j].c1[c] * (256 - u) + (int64_t)g[j].c2[c] * u;
      P[c] = (P[c] * keep + C * cov + 128) >> 8;
    }
    a = a + cov < 256 ? a + cov : 256;
  }
}

// Q16-uitvoer van n leds voor de overgang van frame ka naar kb met gewicht we en verschuiving sh (LEDSCENE.md §5); g: extra versterking Q16 (65536 = geen)
inline void blend(const Scene& s, int ka, int kb, int64_t we, int64_t sh, int n, int64_t gain, uint16_t* lin) {
  Geo ga[kMaxSpansPerFrame], gb[kMaxSpansPerFrame];
  const int64_t N = n, L = 256 * N;
  int na = loadGeo(s, ka, N, L, sh, ga), nb = loadGeo(s, kb, N, L, sh, gb);
  int64_t G = (((int64_t)fLum(s, ka) << 8) * (65536 - we) + ((int64_t)fLum(s, kb) << 8) * we + 32768) >> 16;
  int64_t lg = lutq(G);
  for (int i = 0; i < n; i++) {
    int64_t A[3], B[3];
    px(ga, na, i, L, A);
    px(gb, nb, i, L, B);
    for (int c = 0; c < 3; c++) {
      int64_t X = (A[c] * (65536 - we) + B[c] * we + 32768) >> 16;
      int64_t v = (lutq(X) * lg + 32768) >> 16;
      if (gain != 65536) v = (v * gain) >> 16;
      lin[3 * i + c] = (uint16_t)v;
    }
  }
}

}  // namespace detail

// Q16-versterking van een fade-out van `fade*100` ms na `elapsedMs` ms: 65536 aan het begin, 0 bij fade == 0 of als hij klaar is.
inline uint32_t fadeGain(uint64_t elapsedMs, int fade) {
  if (fade == 0 || elapsedMs >= (uint64_t)fade * 100) return 0;
  return (uint32_t)(65536 - (int64_t)(elapsedMs * 65536 / ((uint64_t)fade * 100)));
}

// Onbegrensd: loops == 0, of (end == 1 en hold == 0).
inline bool bounded(const Scene& s) { return s.loops != 0 && !(s.end == 1 && s.hold == 0); }

// play + (end == 1 ? hold*1000 : 0) + fade*100 in ms; UINT64_MAX als de scène onbegrensd is.
inline uint64_t totalMs(const Scene& s) {
  if (!bounded(s)) return UINT64_MAX;
  return s.playMs + (s.end == 1 ? (uint64_t)s.hold * 1000 : 0) + (uint64_t)s.fade * 100;
}

// Valideert (LEDSCENE.md §7) en vult `out` (mag nullptr zijn). max_bytes == 0: geen grens. Geeft nullptr bij een geldige scène, anders de reden.
// De redenen zijn dezelfde teksten als in scenes.py.
inline const char* validate(const uint8_t* d, size_t n, size_t max_bytes, Scene* out) {
  using detail::rd16;
  if (max_bytes && n > max_bytes) return "te groot voor dit apparaat";
  if (!d || n < 16) return "lengte klopt niet";
  if (d[0] != 0x4C || d[1] != 0x53) return "magic klopt niet";
  if (d[2] != 1) return "versie wordt niet ondersteund";
  const int nkf = d[3];
  const int loops = d[4], end = d[5];
  const uint16_t tick = rd16(d + 6), hold = rd16(d + 8), resume = rd16(d + 12);
  const int fade = d[10];
  if (nkf < 1 || nkf > kMaxFrames) return "nkf buiten 1..32";
  if (end > 1) return "end moet 0 of 1 zijn";
  if (tick < 1 || tick > 60000) return "tick buiten 1..60000";
  if (d[11] != 0 || rd16(d + 14) != 0) return "gereserveerd veld is niet 0";
  if (end == 0 && hold != 0) return "hold moet 0 zijn bij end=0";
  if (hold > kMaxHoldS) return "hold boven 43200 s";
  Scene s;
  s.d = d;
  s.len = n;
  s.nkf = (uint8_t)nkf;
  s.loops = (uint8_t)loops;
  s.end = (uint8_t)end;
  s.fade = (uint8_t)fade;
  s.tick = tick;
  s.hold = hold;
  s.resume = resume;
  size_t pos = 16;
  int spansTotal = 0;
  uint64_t acc = 0;
  for (int k = 0; k < nkf; k++) {
    if (pos + 7 > n) return "lengte klopt niet";
    const uint16_t dt = rd16(d + pos);
    const int ease = d[pos + 2], ns = d[pos + 6];
    if (k < nkf - 1 && dt == 0) return "dt=0 in een frame dat niet het laatste is";
    if (k == nkf - 1) {
      if (loops == 1 && dt != 0) return "laatste dt moet 0 zijn bij loops=1";
      if (loops != 1 && dt == 0) return "laatste dt moet groter dan 0 zijn bij loops ongelijk 1";
    }
    if (ease > 2) return "ease groter dan 2";
    if (ns > kMaxSpansPerFrame) return "meer dan 16 spans in een frame";
    spansTotal += ns;
    if (spansTotal > kMaxSpansTotal) return "meer dan 128 spans in de scene";
    if (pos + 7 + 9 * (size_t)ns > n) return "lengte klopt niet";
    for (int j = 0; j < ns; j++)
      if (d[pos + 7 + 9 * j + 2] & ~1) return "flags met onbekende bits";
    s.off[k] = (uint16_t)pos;
    s.start[k] = acc > UINT32_MAX ? UINT32_MAX : (uint32_t)acc;  // alleen betekenisvol als de scène geldig is (acc <= 86 400 000)
    acc += (uint64_t)dt * tick;
    pos += 7 + 9 * (size_t)ns;
  }
  if (pos != n) return "lengte klopt niet";
  if (acc > kMaxSpanMs) return "span_ms boven 24 uur";
  s.spanMs = (uint32_t)acc;
  s.playMs = loops == 0 ? UINT64_MAX : (uint64_t)loops * acc;
  if (bounded(s) && totalMs(s) > kMaxSpanMs) return "totale duur boven 24 uur";
  if (out) *out = s;
  return nullptr;
}

// Beeld van n leds t_ms ms na de start: lin[3*i + c], lineair Q16 (LEDSCENE.md §2-§5). Na het einde (en de fade-out) alles 0. Zonder heap; n tot enkele honderden.
// `s` moet door validate() zijn gevuld.
inline void render(const Scene& s, uint32_t t_ms, int n, uint16_t* lin) {
  if (n <= 0 || !lin) return;
  const int64_t t = t_ms;
  if (s.loops == 0 || (uint64_t)t < s.playMs) {
    const int64_t tc = t % (int64_t)s.spanMs;  // spanMs > 0 zodra loops != 1 (validatie) of t < playMs
    int k = 0;
    for (int j = s.nkf - 1; j >= 0; j--)
      if ((int64_t)s.start[j] <= tc) {
        k = j;
        break;
      }
    const int nxt = k + 1 < s.nkf ? k + 1 : 0;
    const int64_t dur = (int64_t)detail::fDt(s, k) * s.tick;
    const int64_t w = (tc - (int64_t)s.start[k]) * 65536 / dur;
    const int e = detail::fEase(s, k);
    const int64_t we = e == 0 ? 0 : e == 1 ? w : (((w * w) >> 16) * (196608 - 2 * w)) >> 16;
    const int64_t sh = ((int64_t)detail::fScroll(s, k) * n * w) >> 16;  // >> op een negatief getal is een floor (rekenkundig), zoals de spec vraagt
    detail::blend(s, k, nxt, we, sh, n, 65536, lin);
    return;
  }
  const int kf = s.loops == 1 ? s.nkf - 1 : 0;
  int64_t t0 = (int64_t)s.playMs;
  if (s.end == 1) {
    if (s.hold == 0 || t < t0 + (int64_t)s.hold * 1000) {
      detail::blend(s, kf, kf, 0, 0, n, 65536, lin);
      return;
    }
    t0 += (int64_t)s.hold * 1000;
  }
  const uint32_t g = fadeGain((uint64_t)(t - t0), s.fade);
  if (g == 0) {
    for (int i = 0; i < 3 * n; i++) lin[i] = 0;
    return;
  }
  detail::blend(s, kf, kf, 0, 0, n, g, lin);
}

inline uint32_t fnv1a32(const uint8_t* p, size_t n) {
  uint32_t h = 0x811C9DC5u;
  for (size_t i = 0; i < n; i++) h = (h ^ p[i]) * 0x01000193u;
  return h;
}

}  // namespace ledscene
