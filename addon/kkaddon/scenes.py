"""ledscene/1: binair scenèformaat voor ledstrips (protocol/ledscene/LEDSCENE.md, normatief en bit-exact).

Dit bestand is de referentie-implementatie in Python (alleen standaardbibliotheek): ontleden en valideren (`parse`), bouwen (`build`), renderen naar lineair
Q16 (`render`, wat de C++-kant in `protocol/ledscene/ledscene.h` bit-identiek moet geven), generatoren die naar bytes compileren (`gen_*`, `compile_def`) en een
voorbeeld voor de UI (`preview`). Alles is geheeltallig; `//` en `>>` zijn floor, net als in de spec."""
from __future__ import annotations

import inspect
import math
import re
import struct
from dataclasses import dataclass, field

FORMAT = "ledscene/1"
MAX_SPAN_MS = 86_400_000  # absolute grenzen (§6)
MAX_HOLD_S = 43_200
MAX_SPANS = 128

# T[i] = round(65535 * (i/255)**2.2), i = 0..255, en T[256] = T[255]. Vast (protocol/ledscene/tools/gen_lut.py controleert dit en schrijft ledscene_lut.h).
LUT = (
    0, 0, 2, 4, 7, 11, 17, 24,
    32, 42, 53, 65, 79, 94, 111, 129,
    148, 169, 192, 216, 242, 270, 299, 330,
    362, 396, 432, 469, 508, 549, 591, 635,
    681, 729, 779, 830, 883, 938, 995, 1053,
    1113, 1175, 1239, 1305, 1373, 1443, 1514, 1587,
    1663, 1740, 1819, 1900, 1983, 2068, 2155, 2243,
    2334, 2427, 2521, 2618, 2717, 2817, 2920, 3024,
    3131, 3240, 3350, 3463, 3578, 3694, 3813, 3934,
    4057, 4182, 4309, 4438, 4570, 4703, 4838, 4976,
    5115, 5257, 5401, 5547, 5695, 5845, 5998, 6152,
    6309, 6468, 6629, 6792, 6957, 7124, 7294, 7466,
    7640, 7816, 7994, 8175, 8358, 8543, 8730, 8919,
    9111, 9305, 9501, 9699, 9900, 10102, 10307, 10515,
    10724, 10936, 11150, 11366, 11585, 11806, 12029, 12254,
    12482, 12712, 12944, 13179, 13416, 13655, 13896, 14140,
    14386, 14635, 14885, 15138, 15394, 15652, 15912, 16174,
    16439, 16706, 16975, 17247, 17521, 17798, 18077, 18358,
    18642, 18928, 19216, 19507, 19800, 20095, 20393, 20694,
    20996, 21301, 21609, 21919, 22231, 22546, 22863, 23182,
    23504, 23829, 24156, 24485, 24817, 25151, 25487, 25826,
    26168, 26512, 26858, 27207, 27558, 27912, 28268, 28627,
    28988, 29351, 29717, 30086, 30457, 30830, 31206, 31585,
    31966, 32349, 32735, 33124, 33514, 33908, 34304, 34702,
    35103, 35507, 35913, 36321, 36732, 37146, 37562, 37981,
    38402, 38825, 39252, 39680, 40112, 40546, 40982, 41421,
    41862, 42306, 42753, 43202, 43654, 44108, 44565, 45025,
    45487, 45951, 46418, 46888, 47360, 47835, 48313, 48793,
    49275, 49761, 50249, 50739, 51232, 51728, 52226, 52727,
    53230, 53736, 54245, 54756, 55270, 55787, 56306, 56828,
    57352, 57879, 58409, 58941, 59476, 60014, 60554, 61097,
    61642, 62190, 62741, 63295, 63851, 64410, 64971, 65535,
    65535,
)


class SceneError(ValueError):
    """Een scène die niet geldig is (de tekst is de reden, in het Nederlands)."""


@dataclass
class Span:
    a: int  # begin: 0..255 in 1/256 van de strip, of (flags&1) een ledindex
    len: int  # lengte in dezelfde eenheid, 1..256 (op de draad is 0 = 256)
    c1: tuple = (0, 0, 0)
    c2: tuple | None = None  # None = gelijk aan c1
    flags: int = 0  # bit 0: ledeenheden

    def __post_init__(self):
        if self.len == 0:
            self.len = 256
        self.c1 = tuple(self.c1)
        self.c2 = self.c1 if self.c2 is None else tuple(self.c2)


@dataclass
class Frame:
    dt: int = 0
    ease: int = 0  # 0 vasthouden, 1 lineair, 2 smoothstep
    lum: int = 255
    scroll: int = 0  # 1/256 strip per segment, i16
    spans: list = field(default_factory=list)


@dataclass
class Scene:
    frames: list = field(default_factory=list)
    loops: int = 1
    end: int = 1
    tick: int = 100
    hold: int = 0
    fade: int = 20
    resume: int = 65535


# ------------------------------------------------------------ hash

def fnv1a32(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def hash_hex(data: bytes) -> str:
    return "%08x" % fnv1a32(data)


# ------------------------------------------------------------ ontleden en valideren (§7); de redenen zijn gelijk aan die van ledscene.h

def parse(data: bytes, max_bytes: int | None = None) -> Scene:
    """Ontleedt en valideert. `max_bytes`: het maximum van het apparaat (None of 0 = geen). Gooit SceneError met de reden."""
    data = bytes(data)
    n = len(data)
    if max_bytes and n > max_bytes:
        raise SceneError("te groot voor dit apparaat")
    if n < 16:
        raise SceneError("lengte klopt niet")
    if data[0] != 0x4C or data[1] != 0x53:
        raise SceneError("magic klopt niet")
    if data[2] != 1:
        raise SceneError("versie wordt niet ondersteund")
    nkf, loops, end = data[3], data[4], data[5]
    tick, hold, fade, res1, resume, res2 = struct.unpack_from("<HHBBHH", data, 6)
    if not 1 <= nkf <= 32:
        raise SceneError("nkf buiten 1..32")
    if end > 1:
        raise SceneError("end moet 0 of 1 zijn")
    if not 1 <= tick <= 60000:
        raise SceneError("tick buiten 1..60000")
    if res1 != 0 or res2 != 0:
        raise SceneError("gereserveerd veld is niet 0")
    if end == 0 and hold != 0:
        raise SceneError("hold moet 0 zijn bij end=0")
    if hold > MAX_HOLD_S:
        raise SceneError("hold boven 43200 s")
    sc = Scene(loops=loops, end=end, tick=tick, hold=hold, fade=fade, resume=resume)
    pos, spans_total, acc = 16, 0, 0
    for k in range(nkf):
        if pos + 7 > n:
            raise SceneError("lengte klopt niet")
        dt, ease, lum, scroll, ns = struct.unpack_from("<HBBhB", data, pos)
        if k < nkf - 1 and dt == 0:
            raise SceneError("dt=0 in een frame dat niet het laatste is")
        if k == nkf - 1:
            if loops == 1 and dt != 0:
                raise SceneError("laatste dt moet 0 zijn bij loops=1")
            if loops != 1 and dt == 0:
                raise SceneError("laatste dt moet groter dan 0 zijn bij loops ongelijk 1")
        if ease > 2:
            raise SceneError("ease groter dan 2")
        if ns > 16:
            raise SceneError("meer dan 16 spans in een frame")
        spans_total += ns
        if spans_total > MAX_SPANS:
            raise SceneError("meer dan 128 spans in de scene")
        if pos + 7 + 9 * ns > n:
            raise SceneError("lengte klopt niet")
        fr = Frame(dt=dt, ease=ease, lum=lum, scroll=scroll)
        p = pos + 7
        for _ in range(ns):
            a, ln, flags = data[p], data[p + 1], data[p + 2]
            if flags & ~1:
                raise SceneError("flags met onbekende bits")
            fr.spans.append(Span(a, ln, tuple(data[p + 3:p + 6]), tuple(data[p + 6:p + 9]), flags))
            p += 9
        pos = p
        acc += dt * tick
        sc.frames.append(fr)
    if pos != n:
        raise SceneError("lengte klopt niet")
    if acc > MAX_SPAN_MS:
        raise SceneError("span_ms boven 24 uur")
    if is_bounded(sc) and total_ms(sc) > MAX_SPAN_MS:
        raise SceneError("totale duur boven 24 uur")
    return sc


def _u8(v, name):
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255:
        raise SceneError(f"{name}: buiten 0..255")
    return v


def build(scene: Scene) -> bytes:
    """Scène naar bytes. De uitvoer wordt altijd teruggelezen met `parse`, dus een ongeldige scène geeft SceneError."""
    fr = scene.frames
    for name, v, hi in (("loops", scene.loops, 255), ("end", scene.end, 255), ("tick", scene.tick, 65535), ("hold", scene.hold, 65535), ("fade", scene.fade, 255),
                        ("resume", scene.resume, 65535)):
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= hi:
            raise SceneError(f"{name}: buiten 0..{hi}")
    if not 1 <= len(fr) <= 32:
        raise SceneError("nkf buiten 1..32")
    out = bytearray(b"LS" + bytes([1, len(fr), scene.loops, scene.end]) + struct.pack("<HHBBHH", scene.tick, scene.hold, scene.fade, 0, scene.resume, 0))
    for f in fr:
        if len(f.spans) > 16:
            raise SceneError("meer dan 16 spans in een frame")
        for name, v, lo, hi in (("dt", f.dt, 0, 65535), ("ease", f.ease, 0, 255), ("lum", f.lum, 0, 255), ("scroll", f.scroll, -32768, 32767)):
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise SceneError(f"{name}: buiten {lo}..{hi}")
        out += struct.pack("<HBBhB", f.dt, f.ease, f.lum, f.scroll, len(f.spans))
        for s in f.spans:
            if not 1 <= s.len <= 256:
                raise SceneError("len buiten 1..256")
            if isinstance(s.flags, bool) or not isinstance(s.flags, int) or not 0 <= s.flags <= 255:
                raise SceneError("flags buiten 0..255")
            out += bytes([_u8(s.a, "a"), s.len & 255, s.flags]) + bytes(_u8(x, "kleur") for x in tuple(s.c1) + tuple(s.c2))
    out = bytes(out)
    parse(out)
    return out


# ------------------------------------------------------------ tijd (§2)

def span_ms(scene: Scene) -> int:
    return scene.tick * sum(f.dt for f in scene.frames)


def play_ms(scene: Scene):
    """loops * span_ms, of None bij loops=0 (oneindig)."""
    return None if scene.loops == 0 else scene.loops * span_ms(scene)


def is_bounded(scene: Scene) -> bool:
    return scene.loops != 0 and not (scene.end == 1 and scene.hold == 0)


def total_ms(scene: Scene):
    """play_ms + (end==1 ? hold*1000 : 0) + fade*100; None als de scène onbegrensd is."""
    if not is_bounded(scene):
        return None
    return play_ms(scene) + (scene.hold * 1000 if scene.end == 1 else 0) + scene.fade * 100


def fade_gain(elapsed_ms: int, fade: int) -> int:
    """Q16-versterking van een fade-out van `fade*100` ms na `elapsed_ms` ms (0 bij fade=0 of als hij klaar is)."""
    if fade == 0 or elapsed_ms >= fade * 100:
        return 0
    return 65536 - elapsed_ms * 65536 // (fade * 100)


# ------------------------------------------------------------ renderen (§3-§5)

def _lut(x: int) -> int:
    i, f = x >> 8, x & 255
    return (LUT[i] * (256 - f) + LUT[i + 1] * f + 128) >> 8


def _ovl(s: int, e: int, lo: int, hi: int) -> int:
    return max(0, min(e, hi) - max(s, lo))


def _geo(frame: Frame, N: int, L: int, sh: int):
    g = []
    for sp in frame.spans:
        unit = 256 if sp.flags & 1 else N
        g.append(((sp.a * unit + sh) % L, min(sp.len * unit, L), sp.c1, sp.c2))
    return g


def _px(geo, i: int, L: int):
    """Q8.8-kleur van led i voor een frame (§4)."""
    P = [0, 0, 0]
    a = 0  # tot nu toe gedekt deel van de led (0..256)
    lo = i * 256
    hi = lo + 256
    for S, lu, c1, c2 in geo:
        E = S + lu
        cov = _ovl(S, min(E, L), lo, hi) + (_ovl(0, E - L, lo, hi) if E > L else 0)
        cov = min(256, cov)
        if cov <= 0:
            continue
        d = (lo + 128 - S) % L
        u = d * 256 // lu if d < lu else (0 if (L - d) < (d - lu) else 256)
        o = max(0, a + cov - 256)
        keep = 256 if a == 0 else (a - o) * 256 // a
        for c in range(3):
            C = c1[c] * (256 - u) + c2[c] * u
            P[c] = (P[c] * keep + C * cov + 128) >> 8
        a = min(256, a + cov)
    return P


def _blend(fa: Frame, fb: Frame, we: int, sh: int, n: int):
    L = 256 * n
    ga, gb = _geo(fa, n, L, sh), _geo(fb, n, L, sh)
    G = ((fa.lum << 8) * (65536 - we) + (fb.lum << 8) * we + 32768) >> 16
    lg = _lut(G)
    out = []
    for i in range(n):
        A, B = _px(ga, i, L), _px(gb, i, L)
        out.append([(_lut((A[c] * (65536 - we) + B[c] * we + 32768) >> 16) * lg + 32768) >> 16 for c in range(3)])
    return out


def render(scene: Scene, t_ms: int, n: int):
    """Beeld van `n` leds `t_ms` ms na de start: per led [r,g,b] in lineair Q16 (0..65535). Na het einde (en de fade-out) alles 0."""
    if t_ms < 0:
        raise ValueError("t_ms moet >= 0 zijn")
    if n <= 0:
        return []
    fr, nkf = scene.frames, len(scene.frames)
    span = span_ms(scene)
    if scene.loops == 0 or t_ms < scene.loops * span:
        tc = t_ms % span
        acc, k, start = 0, 0, 0
        for j in range(nkf):
            if acc <= tc:
                k, start = j, acc
            acc += fr[j].dt * scene.tick
        nxt = k + 1 if k + 1 < nkf else 0
        w = (tc - start) * 65536 // (fr[k].dt * scene.tick)
        e = fr[k].ease
        we = 0 if e == 0 else w if e == 1 else (((w * w) >> 16) * (196608 - 2 * w)) >> 16
        sh = (fr[k].scroll * n * w) >> 16
        return _blend(fr[k], fr[nxt], we, sh, n)
    end_frame = fr[nkf - 1] if scene.loops == 1 else fr[0]
    t0 = scene.loops * span
    if scene.end == 1:
        if scene.hold == 0 or t_ms < t0 + scene.hold * 1000:
            return _blend(end_frame, end_frame, 0, 0, n)
        t0 += scene.hold * 1000
    g = fade_gain(t_ms - t0, scene.fade)
    if g == 0:
        return [[0, 0, 0] for _ in range(n)]
    return [[(v * g) >> 16 for v in px] for px in _blend(end_frame, end_frame, 0, 0, n)]


# ------------------------------------------------------------ generatoren

def _color(v, name="kleur"):
    if isinstance(v, str):
        m = re.fullmatch(r"#([0-9a-fA-F]{6})", v.strip())
        if not m:
            raise SceneError(f"{name}: verwacht #rrggbb")
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    if isinstance(v, (list, tuple)) and len(v) == 3 and all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 255 for x in v):
        return tuple(v)
    raise SceneError(f"{name}: verwacht #rrggbb of [r,g,b] met waarden 0..255")


def _int(v, name, lo, hi):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise SceneError(f"{name}: verwacht een geheel getal tussen {lo} en {hi}")
    return v


def _num(v, name, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi):  # NaN valt hier ook buiten
        raise SceneError(f"{name}: verwacht een getal tussen {lo} en {hi}")
    return v


def _common(hold_s, fade, resume):
    return (_int(hold_s, "hold_s", 0, MAX_HOLD_S), _int(fade, "fade", 0, 255), _int(resume, "resume", 0, 65535))


def _tick_for(total_ms_, segments):
    """tick (ms, veelvoud van 100) zodat elke dt in 16 bits past."""
    longest = max(1, math.ceil(total_ms_ / max(1, segments)))
    return max(100, math.ceil(longest / 65535 / 100) * 100)


def gen_static(color=(65, 38, 10), lum=255, hold_s=0, fade=20, resume=65535) -> bytes:
    """Effen kleur. hold_s=0: tot stop (het device begrenst). Standaard het nachtlampje (warm wit, ~5 %)."""
    c = _color(color, "color")
    hold_s, fade, resume = _common(hold_s, fade, resume)
    return build(Scene([Frame(0, 0, _int(lum, "lum", 0, 255), 0, [Span(0, 256, c)])], loops=1, end=1, tick=100, hold=hold_s, fade=fade, resume=resume))


def gen_fade(c1, c2, minutes, hold_s=0, lum=255, fade=20, resume=65535) -> bytes:
    """Lineaire overgang van c1 naar c2 in `minutes` minuten (twee frames), daarna c2 vasthouden."""
    a, b = _color(c1, "c1"), _color(c2, "c2")
    minutes = _num(minutes, "minutes", 1 / 600, 1440)
    hold_s, fade, resume = _common(hold_s, fade, resume)
    lum = _int(lum, "lum", 0, 255)
    ms = round(minutes * 60000)
    tick = _tick_for(ms, 1)
    return build(Scene([Frame(max(1, round(ms / tick)), 1, lum, 0, [Span(0, 256, a)]), Frame(0, 0, lum, 0, [Span(0, 256, b)])],
                       loops=1, end=1, tick=tick, hold=hold_s, fade=fade, resume=resume))


def _sun_frame(num: int, den: int, sun_pct) -> Frame:
    """Eén beeld van de zonsopkomst bij voortgang num/den (0..1): omgevingsrood, aurora (127,25,0) en zon uit het midden; allemaal monotoon in de voortgang."""
    def r(x):
        return int(x * num / den + 0.5)

    spans = [Span(0, 256, (r(100), 0, 0))]
    wa = r(128)
    if wa > 0:
        spans.append(Span(128 - wa, 2 * wa, (127, 25, 0)))
    ws = int(128 * sun_pct / 100 * num / den + 0.5)
    if ws > 0:
        white = r(255)
        spans.append(Span(128 - ws, 2 * ws, (255, min(255, 64 + white), min(255, white))))  # strip is RGB: het witte kanaal van het origineel telt op bij G en B
    return Frame(0, 1, 5 + r(250), 0, spans)


def _sun_frames(count, minutes, sun_pct):
    """`count` beelden met voortgang k/(count-1); de dt's van de `count-1` segmenten zijn samen precies `minutes` minuten, de laatste dt is 0."""
    total = round(minutes * 60000)
    segs = count - 1
    tick = _tick_for(total, segs)
    ticks = round(total / tick)
    frames = [_sun_frame(k, segs, sun_pct) for k in range(count)]
    for k in range(segs):
        frames[k].dt = max(1, ticks * (k + 1) // segs - ticks * k // segs)
    frames[-1].dt, frames[-1].ease = 0, 0
    return frames, tick


def gen_sunrise(minutes, sun_pct=20, hold_s=0, fade=20, resume=600) -> bytes:
    """Zonsopkomst vanuit het midden in `minutes` minuten (12 frames, 3 spans), daarna het eindbeeld vasthouden (hold_s=0: tot stop)."""
    minutes = _num(minutes, "minutes", 1 / 600, 1440)
    sun_pct = _num(sun_pct, "sun_pct", 0, 100)
    hold_s, fade, resume = _common(hold_s, fade, resume)
    frames, tick = _sun_frames(12, minutes, sun_pct)
    return build(Scene(frames, loops=1, end=1, tick=tick, hold=hold_s, fade=fade, resume=resume))


def gen_wake(sun_min=20, wake_min=20, wake_color=(0, 100, 0), sun_pct=20, fade=50, resume=600) -> bytes:
    """Wekker: zonsopkomst van `sun_min` minuten die in het laatste segment overgaat in een effen wekkerkleur, die `wake_min` minuten blijft; dan fade-out en uit."""
    sun_min = _num(sun_min, "sun_min", 1 / 600, 1440)
    wake_min = _num(wake_min, "wake_min", 1 / 60, MAX_HOLD_S / 60)
    sun_pct = _num(sun_pct, "sun_pct", 0, 100)
    c = _color(wake_color, "wake_color")
    _, fade, resume = _common(0, fade, resume)
    frames, tick = _sun_frames(12, sun_min, sun_pct)
    frames[-1] = Frame(0, 0, 255, 0, [Span(0, 256, c)])
    return build(Scene(frames, loops=1, end=1, tick=tick, hold=round(wake_min * 60), fade=fade, resume=resume))


_RAINBOW = ((255, 0, 0), (255, 255, 0), (0, 255, 0), (0, 255, 255), (0, 0, 255), (255, 0, 255))


def gen_rainbow(period_s=5, fade=20, resume=65535) -> bytes:
    """Regenboog die in `period_s` seconden één keer rond de strip loopt (§8: twee frames, scroll 256, loops=0). Naadloos en onafhankelijk van het aantal leds."""
    period_s = _num(period_s, "period_s", 0.02, 1300)
    _, fade, resume = _common(0, fade, resume)
    cuts = (0, 43, 85, 128, 171, 213, 256)
    spans = [Span(cuts[i], cuts[i + 1] - cuts[i], _RAINBOW[i], _RAINBOW[(i + 1) % 6]) for i in range(6)]
    f0 = Frame(max(1, round(period_s * 50)), 1, 255, 256, list(spans))
    f1 = Frame(1, 0, 255, 0, [Span(s.a, s.len, s.c1, s.c2) for s in spans])
    return build(Scene([f0, f1], loops=0, end=1, tick=20, hold=0, fade=fade, resume=resume))


def _only(defn: dict, allowed, kind):
    extra = sorted(set(defn) - set(allowed) - {"kind"})
    if extra:
        raise SceneError(f"{kind}: onbekend veld {', '.join(extra)}")


def _raw(defn: dict) -> bytes:
    _only(defn, ("header", "frames"), "raw")
    h = defn.get("header", {})
    if not isinstance(h, dict):
        raise SceneError("header: verwacht een object")
    _only(h, ("loops", "end", "tick", "hold", "fade", "resume"), "header")
    sc = Scene(loops=h.get("loops", 1), end=h.get("end", 1), tick=h.get("tick", 100), hold=h.get("hold", 0), fade=h.get("fade", 20), resume=h.get("resume", 65535))
    frames = defn.get("frames")
    if not isinstance(frames, list) or not frames:
        raise SceneError("frames: verwacht een lijst met minstens één frame")
    for f in frames:
        if not isinstance(f, dict):
            raise SceneError("frame: verwacht een object")
        _only(f, ("dt", "ease", "lum", "scroll", "spans"), "frame")
        fr = Frame(dt=f.get("dt", 0), ease=f.get("ease", 0), lum=f.get("lum", 255), scroll=f.get("scroll", 0))
        for s in f.get("spans", []):
            if not isinstance(s, dict):
                raise SceneError("span: verwacht een object")
            _only(s, ("a", "len", "flags", "leds", "c1", "c2"), "span")
            if "c1" not in s:
                raise SceneError("span: c1 ontbreekt")
            ln = _int(s.get("len", 256), "len", 0, 256)
            flags = _int(s.get("flags", 0), "flags", 0, 255) | (1 if s.get("leds") else 0)
            c1 = _color(s["c1"], "c1")
            fr.spans.append(Span(_int(s.get("a", 0), "a", 0, 255), ln, c1, _color(s["c2"], "c2") if "c2" in s else c1, flags))
        sc.frames.append(fr)
    return build(sc)


_GENS = {"static": gen_static, "fade": gen_fade, "sunrise": gen_sunrise, "wake": gen_wake, "rainbow": gen_rainbow}


def compile_def(defn: dict) -> bytes:
    """Bibliotheekdefinitie {"kind": "static|fade|sunrise|wake|rainbow|raw", ...parameters} naar scènebytes. SceneError bij ongeldige invoer."""
    if not isinstance(defn, dict):
        raise SceneError("definitie: verwacht een object")
    kind = defn.get("kind")
    if kind == "raw":
        return _raw(defn)
    if kind not in _GENS:
        raise SceneError("kind: verwacht static, fade, sunrise, wake, rainbow of raw")
    fn = _GENS[kind]
    _only(defn, tuple(inspect.signature(fn).parameters), kind)
    try:
        return fn(**{k: v for k, v in defn.items() if k != "kind"})
    except TypeError as e:  # ontbrekend verplicht veld (fade: c1, c2, minutes; sunrise: minutes)
        raise SceneError(f"{kind}: {e}") from None


# ------------------------------------------------------------ voorbeeld voor de UI (niet normatief)

def _window_ms(sc: Scene) -> int:
    if sc.loops == 0:
        return span_ms(sc)
    t = total_ms(sc)
    return t if t is not None else play_ms(sc)


def preview(defn_or_bytes, n: int = 20, samples: int = 60) -> dict:
    """{"n", "bounded", "total_ms", "window_ms", "times": [ms...], "frames": [[[r,g,b] per led] per tijdstip]} met 8-bit sRGB-achtige kleuren ((lin/65535)**(1/2.2)*255)."""
    data = compile_def(defn_or_bytes) if isinstance(defn_or_bytes, dict) else bytes(defn_or_bytes)
    sc = parse(data)
    n, samples = _int(n, "n", 1, 300), _int(samples, "samples", 1, 1000)
    win = _window_ms(sc)
    times = [0] if win == 0 or samples == 1 else sorted({win * i // (samples - 1) for i in range(samples)})
    frames = [[[int((v / 65535) ** (1 / 2.2) * 255 + 0.5) for v in px] for px in render(sc, t, n)] for t in times]
    return {"n": n, "bounded": is_bounded(sc), "total_ms": total_ms(sc), "window_ms": win, "times": times, "frames": frames}
