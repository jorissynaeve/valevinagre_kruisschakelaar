#!/usr/bin/env python3
"""Schrijft protocol/ledscene/vectors/vectors.txt: scènes met de verwachte uitvoer van `scenes.render` (de referentie), voor tests/test_vectors.cpp.

Regelformaat (tekst, geen JSON; regels die met # beginnen of leeg zijn worden overgeslagen):

  scene <naam> <n_leds> <hex van de scènebytes>      begint een scène (daaropvolgende h/total/t-regels horen erbij)
  h <8 hexcijfers>                                   FNV-1a-32 van de bytes
  total <ms>                                         totalMs van de scène, of -1 als hij onbegrensd is
  t <ms> <hex>                                       render op t ms: n_leds*3 u16 little-endian (r,g,b per led, lineair Q16)
  bad <naam> <max_bytes> <hex of -> <reden>          ongeldige bytes (max_bytes 0 = geen grens): validate moet afwijzen met exact deze reden (rest van de regel)

Gebruik: gen_vectors.py           schrijft het bestand
         gen_vectors.py --check   exit 1 als het bestand niet gelijk is aan wat nu geschreven zou worden"""
import random
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "vectors" / "vectors.txt"

try:
    from kkaddon import scenes as S
except ImportError:
    sys.path.insert(0, str(HERE.parents[2] / "addon"))
    from kkaddon import scenes as S


def sp(a, ln, c1, c2=None, led=False):
    return S.Span(a, ln, c1, c2, 1 if led else 0)


def fr(dt, ease, lum, scroll, *spans):
    return S.Frame(dt, ease, lum, scroll, list(spans))


def sc(frames, loops=1, end=1, tick=100, hold=0, fade=20, resume=65535):
    return S.build(S.Scene(frames, loops=loops, end=end, tick=tick, hold=hold, fade=fade, resume=resume))


RED, YEL, GRN, CYA, BLU, MAG, WHT = (255, 0, 0), (255, 255, 0), (0, 255, 0), (0, 255, 255), (0, 0, 255), (255, 0, 255), (255, 255, 255)


def hand_made():
    """(naam, bytes, [aantallen leds])"""
    out = []
    out.append(("nachtlamp", S.gen_static(), [20, 1, 300]))
    out.append(("nachtlamp_dim", S.gen_static((200, 100, 50), lum=40, hold_s=2, fade=5), [20]))
    out.append(("wekker", S.gen_wake(20, 20), [20, 1, 300]))
    out.append(("wekker_kort", S.gen_wake(1, 1, (0, 100, 0), 30, fade=10), [20, 7]))
    out.append(("zonsopkomst", S.gen_sunrise(2, 25, hold_s=3, fade=4), [20, 300]))
    out.append(("fade_rood_blauw", S.gen_fade("#ff0000", (0, 0, 255), 1, hold_s=2, fade=3), [20]))
    out.append(("regenboog", S.gen_rainbow(5), [20, 1, 300, 37]))
    out.append(("regenboog_snel", S.gen_rainbow(0.5, fade=0), [20]))
    # losse leds met ledeenheden; a >= N wikkelt om; len=0 is alle leds
    out.append(("per_led", sc([
        fr(50, 1, 255, 0, sp(0, 1, RED, led=True), sp(5, 3, GRN, BLU, led=True), sp(19, 1, WHT, led=True), sp(30, 2, YEL, led=True)),
        fr(0, 0, 200, 0, sp(1, 1, RED, led=True), sp(5, 3, BLU, GRN, led=True), sp(18, 2, WHT, MAG, led=True), sp(3, 0, (10, 20, 30), led=True))],
        tick=100, hold=1, fade=2), [20, 1, 300]))
    # een span over de rand van de strip (a=200, len=100: 200..300 van 256, dus wikkelt)
    out.append(("wrap_span", sc([
        fr(30, 1, 255, 0, sp(200, 100, RED, BLU)),
        fr(30, 2, 255, 100, sp(200, 100, GRN, YEL), sp(10, 20, WHT)),
        fr(0, 0, 255, 0, sp(200, 100, RED, BLU))], loops=1, end=1, hold=2, fade=2), [20, 1, 300, 13]))
    out.append(("neg_scroll", sc([
        fr(40, 1, 255, -300, sp(0, 86, RED, YEL), sp(86, 85, GRN, CYA), sp(171, 85, BLU, MAG)),
        fr(40, 1, 255, -256, sp(0, 86, RED, YEL), sp(86, 85, GRN, CYA), sp(171, 85, BLU, MAG)),
        fr(0, 0, 255, 0, sp(0, 86, RED, YEL), sp(86, 85, GRN, CYA), sp(171, 85, BLU, MAG))], loops=1, end=0, fade=5), [20, 1, 300, 11]))
    out.append(("ease2", sc([
        fr(25, 2, 255, 40, sp(0, 0, RED, BLU)), fr(25, 2, 60, -40, sp(0, 128, GRN, MAG), sp(128, 128, YEL, CYA)),
        fr(0, 0, 255, 0, sp(0, 0, WHT))], loops=1, end=1, hold=1, fade=1), [20, 1, 300]))
    out.append(("loops2", sc([
        fr(10, 1, 255, 128, sp(0, 128, RED, GRN)), fr(7, 2, 100, 0, sp(64, 128, BLU, YEL)), fr(5, 0, 255, -64, sp(0, 0, MAG, CYA))],
        loops=2, end=1, tick=50, hold=3, fade=2), [20, 3, 300]))
    out.append(("loops3_end0", sc([
        fr(12, 1, 255, 256, sp(0, 128, RED, GRN)), fr(8, 1, 90, 0, sp(128, 128, BLU, YEL))], loops=3, end=0, tick=100, fade=10), [20, 5]))
    out.append(("end0_fade", sc([
        fr(30, 1, 255, 0, sp(0, 0, RED, BLU)), fr(0, 0, 255, 0, sp(0, 0, BLU, RED))], loops=1, end=0, tick=100, fade=10), [20, 1, 300]))
    out.append(("fade0", sc([
        fr(10, 1, 255, 0, sp(0, 0, RED)), fr(0, 0, 255, 0, sp(0, 0, BLU))], loops=1, end=0, fade=0), [20]))
    out.append(("onbegrensd_end0", sc([
        fr(10, 2, 255, 90, sp(0, 100, RED, BLU)), fr(10, 1, 120, 0, sp(100, 156, GRN, YEL)), fr(15, 0, 255, -30, sp(0, 0, MAG))],
        loops=0, end=0, tick=40, fade=6), [20, 1, 300]))
    out.append(("onbegrensd_hold0", sc([
        fr(10, 1, 255, 256, sp(0, 128, RED, GRN)), fr(1, 0, 255, 0, sp(128, 128, BLU, YEL))], loops=0, end=1, tick=20, hold=0, fade=3), [20]))
    # lengte 0 = 256; ledeenheden met len=0 (256 leds, begrensd op de strip); 16 spans in een frame; een zwart frame (n=0)
    out.append(("len0", sc([fr(5, 1, 255, 0, sp(0, 0, RED, BLU), sp(3, 0, GRN, GRN, led=True)), fr(0, 0, 255, 0, sp(0, 0, WHT))], loops=1, end=1, hold=1, fade=1), [20, 1, 300]))
    many = [sp(i * 16, 16, (i * 16, 255 - i * 16, i * 8), ((i * 31) & 255, i * 5, 255)) for i in range(16)]
    out.append(("zestien_spans", sc([fr(8, 1, 255, 0, *many), fr(0, 0, 255, 0)], loops=1, end=1, hold=1, fade=1), [20, 7, 300]))
    out.append(("zwart_frame", sc([fr(8, 1, 255, 0), fr(8, 2, 255, 0, sp(0, 0, WHT)), fr(0, 0, 255, 0)], loops=1, end=0, fade=4), [20]))
    out.append(("grote_tick", sc([fr(2, 1, 255, 77, sp(0, 0, RED, GRN)), fr(0, 0, 255, 0, sp(0, 0, BLU, MAG))], loops=1, end=1, tick=60000, hold=100, fade=255), [20]))
    out.append(("lum_nul", sc([fr(10, 1, 0, 0, sp(0, 0, WHT)), fr(0, 0, 255, 0, sp(0, 0, WHT))], loops=1, end=1, fade=0, hold=1), [20]))
    return out


def random_scene(rng):
    nkf = rng.randint(1, 6)
    loops = rng.choice([0, 1, 1, 2, 3])
    end = rng.randint(0, 1)
    frames = []
    for k in range(nkf):
        last = k == nkf - 1
        dt = 0 if (last and loops == 1) else rng.randint(1, 40)
        spans = []
        for _ in range(rng.choice([0, 1, 2, 3, 3, 5])):
            led = rng.random() < 0.3
            c1 = tuple(rng.randint(0, 255) for _ in range(3))
            c2 = c1 if rng.random() < 0.4 else tuple(rng.randint(0, 255) for _ in range(3))
            spans.append(sp(rng.randint(0, 25 if led else 255), rng.choice([0, rng.randint(1, 256), rng.randint(1, 10)]), c1, c2, led))
        frames.append(fr(dt, rng.randint(0, 2), rng.choice([255, 255, rng.randint(0, 255)]), rng.choice([0, 0, 256, -256, rng.randint(-700, 700)]), *spans))
    return sc(frames, loops=loops, end=end, tick=rng.choice([20, 50, 100, 250]), hold=rng.choice([0, 1, 5]) if end else 0, fade=rng.choice([0, 1, 5, 30]))


def times_for(data, limit):
    s = S.parse(data)
    span, play = S.span_ms(s), S.play_ms(s)
    core, seg = {0, 1}, set()
    acc = 0
    for f in s.frames:
        d = f.dt * s.tick
        for x in (acc - 1, acc, acc + 1, acc + d // 3, acc + d // 2, acc + d - 1):
            seg.add(x)
        acc += d
    for x in (span - 1, span, span + 1):
        core.add(x)
    if s.loops == 0:
        for m in (2, 3, 7):
            for x in (m * span - 1, m * span, m * span + 777):
                core.add(x)
        core.add(86_400_000)
    else:
        for x in (play - 1, play, play + 1):
            core.add(x)
        t0 = play
        if s.end == 1 and s.hold:
            t0 = play + s.hold * 1000
            for x in (t0 - 1, t0, t0 + 1, play + 1000 * s.hold // 2):
                core.add(x)
        if s.end == 0 or s.hold:
            fd = s.fade * 100
            for x in (t0 + fd // 3, t0 + fd // 2, t0 + fd - 1, t0 + fd, t0 + fd + 1, t0 + 1, t0 + 50):
                core.add(x)
        core.add(play + s.hold * 1000 + s.fade * 100 + 5000)
    core = sorted(x for x in core if 0 <= x < 2 ** 32)
    seg = sorted(x for x in seg - set(core) if 0 <= x < 2 ** 32)
    if len(core) >= limit:
        return _thin(core, limit)
    room = limit - len(core)
    if len(seg) > room:
        seg = [seg[i * (len(seg) - 1) // max(1, room - 1)] for i in range(room)] if room > 1 else seg[:room]
    return sorted(set(core) | set(seg))


def _thin(xs, limit):
    return sorted({xs[i * (len(xs) - 1) // (limit - 1)] for i in range(limit)})


def hexle(vals):
    return struct.pack("<%dH" % len(vals), *vals).hex()


def bad_cases():
    base = S.gen_static()  # 32 bytes
    two = S.gen_fade(RED, BLU, 1)

    def mut(b, **patch):
        b = bytearray(b)
        for off, v in patch.items():
            b[int(off[1:])] = v
        return bytes(b)

    c = []
    c.append(("leeg", 0, b""))
    for n in range(1, len(base)):
        c.append((f"afgekapt_{n}", 0, base[:n]))
    c.append(("extra_byte", 0, base + b"\0"))
    c.append(("te_groot", 31, base))
    c.append(("magic", 0, mut(base, o0=0x4D)))
    c.append(("versie2", 0, mut(base, o2=2)))
    c.append(("nkf0", 0, mut(base, o3=0)))
    c.append(("nkf33", 0, mut(base, o3=33)))
    c.append(("end2", 0, mut(base, o5=2)))
    c.append(("tick0", 0, mut(base, o6=0, o7=0)))
    c.append(("tick60001", 0, mut(base, o6=0x61, o7=0xEA)))
    c.append(("gereserveerd11", 0, mut(base, o11=1)))
    c.append(("gereserveerd14", 0, mut(base, o14=1)))
    c.append(("hold_bij_end0", 0, mut(base, o5=0, o8=1)))
    c.append(("hold_43201", 0, mut(base, o8=0xC1, o9=0xA8)))
    c.append(("dt_nul_midden", 0, mut(two, o16=0, o17=0)))
    c.append(("laatste_dt_bij_loops1", 0, mut(two, o32=1)))
    c.append(("laatste_dt_nul_bij_loops0", 0, mut(two, o4=0)))
    c.append(("ease3", 0, mut(base, o18=3)))
    c.append(("n17", 0, mut(base, o22=17)))
    c.append(("n17_met_genoeg_bytes", 0, mut(base, o22=17) + bytes(9 * 17)))
    c.append(("flags2", 0, mut(base, o25=2)))
    c.append(("flags_hoog", 0, mut(base, o25=0x81)))
    c.append(("lengte_n2_maar_1_span", 0, mut(base, o22=2)))
    # te veel spans in totaal: 9 frames met 15 spans = 135
    h = bytearray(b"LS" + bytes([1, 9, 1, 1]) + struct.pack("<HHBBHH", 100, 0, 20, 0, 65535, 0))
    for k in range(9):
        h += struct.pack("<HBBhB", 0 if k == 8 else 1, 0, 255, 0, 15) + bytes(9 * 15)
    c.append(("te_veel_spans_totaal", 0, bytes(h)))
    c.append(("span_ms_te_groot", 0, mut(two, o6=0x60, o7=0xEA, o16=0xA1, o17=0x05)))  # tick 60000 * dt 1441 = 86 460 000 ms
    c.append(("totaal_te_groot", 0, _total_too_big()))
    return c


def _total_too_big():
    """loops=2 met span 43 200 000 ms (play 86 400 000) en fade 2 s: samen boven 24 uur, terwijl span_ms zelf mag."""
    raw = bytearray(sc([fr(1, 1, 255, 0, sp(0, 0, RED)), fr(1, 0, 255, 0, sp(0, 0, BLU))], loops=2, end=0, tick=100, fade=20))
    raw[6:8] = struct.pack("<H", 60000)
    raw[16:18] = struct.pack("<H", 360)
    raw[32:34] = struct.pack("<H", 360)
    return bytes(raw)


def generate():
    lines = ["# ledscene/1 testvectoren, gegenereerd door protocol/ledscene/tools/gen_vectors.py uit addon/kkaddon/scenes.py; niet met de hand aanpassen.",
             "# formaat: zie de kop van gen_vectors.py. Alle uitvoer is lineair Q16 (u16 little-endian, r,g,b per led)."]
    entries = hand_made()
    rng = random.Random(20260101)
    for i in range(40):
        entries.append((f"toeval{i:02d}", random_scene(rng), [rng.choice([1, 2, 3, 20, 37])]))
    for name, data, ns in entries:
        sc_ = S.parse(data)
        tot = S.total_ms(sc_)
        for n in ns:
            lines.append(f"scene {name if n == 20 else f'{name}_n{n}'} {n} {data.hex()}")
            lines.append(f"h {S.hash_hex(data)}")
            lines.append(f"total {-1 if tot is None else tot}")
            for t in times_for(data, 14 if n >= 100 else 45):
                lines.append(f"t {t} {hexle([v for px in S.render(sc_, t, n) for v in px])}")
    for name, mx, data in bad_cases():
        try:
            S.parse(data, mx or None)
        except S.SceneError as e:
            lines.append(f"bad {name} {mx} {data.hex() or '-'} {e}")
        else:
            raise SystemExit(f"bad case {name} is toch geldig")
    return "\n".join(lines) + "\n"


def main(argv):
    text = generate()
    if "--check" in argv:
        ok = OUT.exists() and OUT.read_text(encoding="utf-8") == text
        print("vectors.txt is actueel" if ok else "vectors.txt wijkt af: draai tools/gen_vectors.py")
        return 0 if ok else 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"geschreven: {OUT} ({len(text.splitlines())} regels, {len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
