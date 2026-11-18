"""ledscene/1 (addon/kkaddon/scenes.py): validatie (elke afwijsregel uit LEDSCENE.md §7), generatoren (grootte, determinisme), de renderer (randen, tijd, einde, fade),
bibliotheekdefinities, het voorbeeld voor de UI, en de gedeelde vectoren met de C++-kant (vectors.txt moet gelijk zijn aan wat gen_vectors.py nu schrijft)."""
import importlib.util
import os
import random
import struct
from pathlib import Path

import pytest

from kkaddon import scenes as S
from kkaddon.scenes import Frame, Scene, SceneError, Span

ROOT = Path(os.environ.get("KK_PROJECT") or Path(__file__).resolve().parents[2])
LEDSCENE = ROOT / "protocol" / "ledscene"
FULL = 65534  # volle schaal: (65535 * 65535 + 32768) >> 16
RED, GRN, BLU, WHT = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)


def load_tool(name):
    spec = importlib.util.spec_from_file_location("ledscene_" + name, LEDSCENE / "tools" / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def one(frames, **kw):
    return S.build(Scene(frames, **kw))


def solid(c=WHT, lum=255):
    return Frame(0, 0, lum, 0, [Span(0, 256, c)])


def reject(data, why, max_bytes=None):
    with pytest.raises(SceneError) as e:
        S.parse(data, max_bytes)
    assert why in str(e.value), str(e.value)


# ------------------------------------------------------------ LUT en hash

def test_lut_is_the_formula():
    assert len(S.LUT) == 257
    assert list(S.LUT[:256]) == [round(65535 * (i / 255) ** 2.2) for i in range(256)]
    assert S.LUT[256] == S.LUT[255] == 65535 and S.LUT[0] == 0
    assert all(a <= b for a, b in zip(S.LUT, S.LUT[1:]))


@pytest.mark.skipif(not (LEDSCENE / "tools" / "gen_lut.py").exists(), reason="projectmap niet beschikbaar")
def test_lut_header_and_python_table_agree_with_gen_lut():
    gl = load_tool("gen_lut")
    assert list(S.LUT) == gl.table()
    assert (LEDSCENE / "ledscene_lut.h").read_text(encoding="utf-8") == gl.header_text(), "draai protocol/ledscene/tools/gen_lut.py"


def test_fnv1a32_known_values():
    assert S.fnv1a32(b"") == 0x811C9DC5
    assert S.fnv1a32(b"a") == 0xE40C292C
    assert S.fnv1a32(b"foobar") == 0xBF9CF968
    assert S.hash_hex(b"a") == "e40c292c" and S.hash_hex(b"") == "811c9dc5"
    assert len(S.hash_hex(S.gen_static())) == 8


# ------------------------------------------------------------ validatie

BASE = S.gen_static()  # 32 bytes: kop, één frame (dt 0 ease 0 lum 255 scroll 0 n 1), één span
TWO = S.gen_fade(RED, BLU, 1)  # twee frames met elk één span; tweede frame begint op 32


def mut(b, **patch):
    b = bytearray(b)
    for k, v in patch.items():
        b[int(k[1:])] = v
    return bytes(b)


def test_base_layout_is_what_the_mutations_assume():
    assert len(BASE) == 32 and BASE[:2] == b"LS" and BASE[2] == 1 and BASE[3] == 1 and BASE[22] == 1 and len(TWO) == 48


REJECTS = [
    ("magic", mut(BASE, o0=0x4D), "magic"),
    ("magic2", mut(BASE, o1=0), "magic"),
    ("ver", mut(BASE, o2=2), "versie"),
    ("ver0", mut(BASE, o2=0), "versie"),
    ("nkf0", mut(BASE, o3=0), "nkf"),
    ("nkf33", mut(BASE, o3=33), "nkf"),
    ("end2", mut(BASE, o5=2), "end"),
    ("tick0", mut(BASE, o6=0, o7=0), "tick"),
    ("tick60001", mut(BASE, o6=0x61, o7=0xEA), "tick"),
    ("res11", mut(BASE, o11=1), "gereserveerd"),
    ("res14", mut(BASE, o14=1), "gereserveerd"),
    ("res15", mut(BASE, o15=1), "gereserveerd"),
    ("end0_hold", mut(BASE, o5=0, o8=1), "hold moet 0"),
    ("hold43201", mut(BASE, o8=0xC1, o9=0xA8), "hold boven"),
    ("dt0_mid", mut(TWO, o16=0, o17=0), "dt=0"),
    ("last_dt_loops1", mut(TWO, o32=1), "laatste dt moet 0"),
    ("last_dt0_loops0", mut(TWO, o4=0), "laatste dt moet groter"),
    ("last_dt0_loops2", mut(TWO, o4=2), "laatste dt moet groter"),
    ("ease3", mut(BASE, o18=3), "ease"),
    ("n17", mut(BASE, o22=17), "16 spans"),
    ("n17_enough_bytes", mut(BASE, o22=17) + bytes(9 * 17), "16 spans"),
    ("flags2", mut(BASE, o25=2), "flags"),
    ("flags_high", mut(BASE, o25=0x81), "flags"),
    ("n2_one_span", mut(BASE, o22=2), "lengte"),
    ("n0_extra_span_bytes", mut(BASE, o22=0), "lengte"),
    ("extra_byte", BASE + b"\0", "lengte"),
    ("empty", b"", "lengte"),
    ("span_ms", mut(TWO, o6=0x60, o7=0xEA, o16=0xA1, o17=0x05), "span_ms"),  # 60000 ms * 1441
]


@pytest.mark.parametrize("name,data,why", REJECTS, ids=[r[0] for r in REJECTS])
def test_every_rejection_rule(name, data, why):
    reject(data, why)


def test_truncation_at_every_length_is_rejected():
    for data in (BASE, TWO, S.gen_rainbow(), S.gen_wake()):
        for k in range(len(data)):
            reject(data[:k], "lengte")
        S.parse(data)


def test_too_many_spans_in_total():
    frames = [Frame(1, 0, 255, 0, [Span(0, 16, WHT)] * 15) for _ in range(8)] + [Frame(0, 0, 255, 0, [Span(0, 16, WHT)] * 8)]
    assert sum(len(f.spans) for f in frames) == 128
    S.build(Scene(frames))
    frames[-1].spans.append(Span(0, 1, WHT))
    with pytest.raises(SceneError, match="128 spans"):
        S.build(Scene(frames))


def test_total_duration_limit_and_max_bytes_and_hold_limit():
    # play = 2 * 43 200 000 ms is precies 24 uur; de fade erbovenop is te veel (bij end=0); span_ms zelf mag
    ok = Scene([Frame(360, 0, 255, 0, [Span(0, 256, RED)]), Frame(360, 0, 255, 0, [Span(0, 256, BLU)])], loops=2, end=0, tick=60000, fade=0)
    S.build(ok)
    ok.fade = 1
    with pytest.raises(SceneError, match="totale duur"):
        S.build(ok)
    # onbegrensd (end=1, hold=0): play_ms wordt niet getoetst, alleen span_ms
    S.build(Scene(ok.frames, loops=2, end=1, tick=60000, hold=0, fade=1))
    S.parse(BASE, 32)
    reject(BASE, "te groot", 31)
    S.parse(BASE, None)
    S.parse(BASE, 0)
    S.build(Scene([solid()], end=1, hold=43200))
    with pytest.raises(SceneError, match="hold"):
        S.build(Scene([solid()], end=1, hold=43201))


def test_build_rejects_out_of_range_fields_with_scene_error():
    for bad in (Scene([]), Scene([solid()] * 33), Scene([solid()], tick=0), Scene([solid()], loops=256), Scene([solid()], fade=-1), Scene([Frame(0, 0, 256, 0, [])]),
                Scene([Frame(0, 0, 255, 40000, [])]), Scene([Frame(0, 0, 255, 0, [Span(256, 1, WHT)])]), Scene([Frame(0, 0, 255, 0, [Span(0, 1, (0, 0, 256))])]),
                Scene([Frame(0, 0, 255, 0, [Span(0, 257, WHT)])]), Scene([Frame(0, 0, 255, 0, [Span(0, 1, WHT, flags=4)])]), Scene([solid()], tick=True)):
        with pytest.raises(SceneError):
            S.build(bad)


def test_parse_never_raises_anything_but_scene_error():
    rng = random.Random(7)
    seeds = [BASE, TWO, S.gen_rainbow(), S.gen_wake(), S.gen_sunrise(3)]
    valid = 0
    for _ in range(3000):
        if rng.random() < 0.3:
            b = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 80)))
        else:
            b = bytearray(rng.choice(seeds))
            for _ in range(rng.randint(1, 3)):
                b[rng.randrange(len(b))] = rng.randrange(256)
            b = bytes(b[:rng.randrange(len(b) + 1)] if rng.random() < 0.2 else b)
        try:
            sc = S.parse(b)
            valid += 1
            S.render(sc, rng.randrange(0, 5000), 3)
        except SceneError:
            pass
    assert valid > 100


# ------------------------------------------------------------ rond en generatoren

def test_round_trip():
    scenes = [S.gen_static(), S.gen_fade(RED, BLU, 3), S.gen_sunrise(10), S.gen_wake(), S.gen_rainbow(), BASE, TWO]
    for b in scenes:
        sc = S.parse(b)
        assert S.build(sc) == b
        assert S.parse(S.build(sc)) == sc
    sc = Scene([Frame(5, 1, 200, -300, [Span(3, 1, RED, BLU, 1), Span(0, 256, GRN)]), Frame(7, 2, 10, 17, []), Frame(0, 0, 255, 0, [Span(9, 9, WHT)])], loops=1, end=0, tick=50,
               fade=7, resume=1234)
    assert S.parse(S.build(sc)) == sc
    assert S.parse(S.build(Scene([Frame(0, 0, 255, 0, [Span(0, 0, WHT)])]))).frames[0].spans[0].len == 256, "len=0 is 256"
    assert S.build(Scene([Frame(0, 0, 255, 0, [Span(0, 0, WHT)])])) == S.build(Scene([Frame(0, 0, 255, 0, [Span(0, 256, WHT)])]))


def test_generator_sizes():
    assert len(S.gen_static()) == 32
    r = S.gen_rainbow()
    assert len(r) == 138 and 130 <= len(r) <= 150
    for minutes in (1, 10, 20, 60, 180):
        assert len(S.gen_sunrise(minutes)) <= 768
    w = S.gen_wake()
    assert len(w) <= 768
    assert len(one([Frame(1, 1, 255, 0, [Span(0, 256, WHT)] * 3)] * 11 + [Frame(0, 0, 255, 0, [Span(0, 256, WHT)] * 3)])) == 424, "12 frames met 3 spans: 424 bytes (§1)"


def test_generators_are_deterministic():
    for f, a in ((S.gen_static, ()), (S.gen_rainbow, ()), (S.gen_wake, ()), (S.gen_sunrise, (20,)), (S.gen_fade, (RED, BLU, 5))):
        assert f(*a) == f(*a)
    assert S.gen_wake() == S.gen_wake(20, 20, (0, 100, 0))


def test_static_and_fade_and_wake_parameters():
    sc = S.parse(S.gen_static((1, 2, 3), lum=77, hold_s=60, fade=5, resume=0))
    assert (sc.hold, sc.fade, sc.resume, sc.end, sc.loops, sc.frames[0].lum) == (60, 5, 0, 1, 1, 77) and sc.frames[0].spans[0].c1 == (1, 2, 3)
    assert S.total_ms(sc) == 60_000 + 500 and S.is_bounded(sc)
    assert not S.is_bounded(S.parse(S.gen_static()))
    f = S.parse(S.gen_fade(RED, BLU, 2))
    assert S.span_ms(f) == 120_000 and len(f.frames) == 2 and f.frames[0].ease == 1 and f.frames[1].dt == 0
    assert S.render(f, 0, 1)[0][0] > 0 and S.render(f, 0, 1)[0][2] == 0
    mid = S.render(f, 60_000, 1)[0]
    assert mid[0] > 0 and mid[2] > 0 and mid[1] == 0
    end = S.render(f, 120_000, 1)[0]
    assert end[0] == 0 and end[2] == FULL
    w = S.parse(S.gen_wake(20, 15, (10, 20, 30), fade=7, resume=300))
    assert (w.end, w.hold, w.fade, w.resume, w.loops, len(w.frames)) == (1, 15 * 60, 7, 300, 1, 12)
    assert S.span_ms(w) == 20 * 60_000 and w.frames[-1].dt == 0
    assert S.total_ms(w) == 20 * 60_000 + 15 * 60_000 + 700
    assert S.render(w, S.span_ms(w), 4)[0] == [S.render(S.parse(S.gen_static((10, 20, 30))), 0, 1)[0][c] for c in range(3)]
    d = S.parse(S.gen_wake())
    assert d.hold == 1200 and d.fade == 50 and d.resume == 600 and d.frames[-1].spans[0].c1 == (0, 100, 0)
    long = S.parse(S.gen_fade(RED, BLU, 600))  # 10 uur: de tick wordt groter zodat dt in 16 bits past
    assert S.span_ms(long) == pytest.approx(600 * 60_000, rel=0.001) and long.tick > 100


def test_rainbow_matches_the_spec_example():
    sc = S.parse(S.gen_rainbow())
    assert (sc.tick, sc.loops, sc.end, sc.hold) == (20, 0, 1, 0) and len(sc.frames) == 2
    assert [(s.a, s.len) for s in sc.frames[0].spans] == [(0, 43), (43, 42), (85, 43), (128, 43), (171, 42), (213, 43)]
    assert [s.c1 for s in sc.frames[0].spans] == [RED, (255, 255, 0), GRN, (0, 255, 255), BLU, (255, 0, 255)]
    assert [s.c2 for s in sc.frames[0].spans][-1] == RED
    f0, f1 = sc.frames
    assert (f0.dt, f0.ease, f0.scroll, f1.dt, f1.ease, f1.scroll) == (250, 1, 256, 1, 0, 0)
    assert S.span_ms(sc) == 5020 and S.total_ms(sc) is None and S.play_ms(sc) is None
    assert S.span_ms(S.parse(S.gen_rainbow(2))) == 2020


def test_rainbow_is_seamless():
    for n in (1, 20, 300):
        sc = S.parse(S.gen_rainbow())
        a, z = S.render(sc, 0, n), S.render(sc, S.span_ms(sc) - 1, n)
        assert a == z, "het tussenframe (scroll 0) is gelijk aan het beginbeeld"
        # 1 ms voor het einde van de scroll (w = 65520 van 65536) is het beeld bijna gelijk aan het beginbeeld: twee posities in 1/256 led verschoven, dus
        # hoogstens zo'n 2 stappen van 8 bit aan de heldere kant (een stap is daar ~565 in Q16)
        before = S.render(sc, 4999, n)
        assert max(abs(x - y) for p, q in zip(a, before) for x, y in zip(p, q)) <= 1200, n
        for t in (0, 1234, 4999, 5000, 5019):
            assert S.render(sc, t, n) == S.render(sc, t + S.span_ms(sc), n)
    # na een kwart ronde is het beeld een kwart verschoven (20 leds: 5 leds)


def test_rainbow_has_no_dark_seams():
    """De naad tussen twee aansluitende spans mag niet dimmen (§4): elke kleur van de regenboog heeft een kanaal op volle sterkte."""
    sc = S.parse(S.gen_rainbow())
    for n in (20, 37, 300):
        worst = min(max(p) for t in range(0, 5020, 13) for p in S.render(sc, t, n))
        assert worst >= FULL - 300, (n, worst)


def test_adjacent_spans_add_up_and_a_background_still_alpha_overs():
    # twee spans naast elkaar die samen een led delen: led 1 van n=2 (256..512 posities) wordt gedekt door a=0..192 (deel) en 192..256; strip van 2 leds
    sc = S.parse(one([Frame(0, 0, 255, 0, [Span(0, 192, WHT), Span(192, 64, WHT)])]))
    assert S.render(sc, 0, 2) == [[FULL] * 3] * 2, "de naad op 3/4 van de strip (led 1, halverwege 1,5) dimt niet"
    # een ondergrond met er deels overheen een span: gewone alpha-over (ondergrond rood, span blauw over de helft van led 0)
    sc = S.parse(one([Frame(0, 0, 255, 0, [Span(0, 0, RED), Span(0, 64, BLU)])]))
    px = S.render(sc, 0, 2)
    assert px[1] == [FULL, 0, 0] and px[0][0] == px[0][2] and 0 < px[0][0] < FULL and px[0][1] == 0, "led 0 is half rood (ondergrond), half blauw"


# ------------------------------------------------------------ de renderer: randen

def lit(px):
    return [i for i, p in enumerate(px) if any(p)]


def test_solid_and_full_scale_output():
    px = S.render(S.parse(S.gen_static((255, 255, 255))), 0, 5)
    assert px == [[FULL] * 3] * 5
    assert S.render(S.parse(S.gen_static((255, 0, 0), lum=0)), 0, 2) == [[0, 0, 0]] * 2
    assert S.render(S.parse(S.gen_static((0, 0, 0))), 0, 2) == [[0, 0, 0]] * 2
    assert S.render(S.parse(S.gen_static((255, 255, 255))), 0, 0) == []


def test_span_wraps_over_the_strip_edge():
    sc = S.parse(one([Frame(0, 0, 255, 0, [Span(200, 100, WHT)])]))
    px = S.render(sc, 0, 20)  # 200/256 .. 300/256 van de strip = leds 15.6 .. 23.4 -> leds 16..19 en 0..2 vol, 15 en 3 deels
    full = [i for i, p in enumerate(px) if p[0] == FULL]
    assert full == [0, 1, 2, 16, 17, 18, 19]
    assert [i for i, p in enumerate(px) if 0 < p[0] < FULL] == [3, 15]
    assert all(px[i] == [0, 0, 0] for i in range(4, 15))


def test_len_zero_is_the_whole_strip_and_units():
    for n in (1, 7, 300):
        assert all(p == [FULL] * 3 for p in S.render(S.parse(one([Frame(0, 0, 255, 0, [Span(0, 0, WHT)])])), 0, n))
    # ledeenheden: exact leds 3 en 4
    px = S.render(S.parse(one([Frame(0, 0, 255, 0, [Span(3, 2, WHT, flags=1)])])), 0, 10)
    assert lit(px) == [3, 4] and all(px[i] == [FULL] * 3 for i in (3, 4))
    # ledeenheden met len=0 = 256 leds, begrensd op de strip; a >= N wikkelt
    assert all(p == [FULL] * 3 for p in S.render(S.parse(one([Frame(0, 0, 255, 0, [Span(2, 0, WHT, flags=1)])])), 0, 10))
    assert lit(S.render(S.parse(one([Frame(0, 0, 255, 0, [Span(12, 1, WHT, flags=1)])])), 0, 10)) == [2]
    # niet-wikkelend: de span (ledeenheden) 8..11 op een strip van 10 wikkelt naar led 0 en 1
    assert lit(S.render(S.parse(one([Frame(0, 0, 255, 0, [Span(8, 4, WHT, flags=1)])])), 0, 10)) == [0, 1, 8, 9]


def test_gradient_and_alpha_over():
    sc = S.parse(one([Frame(0, 0, 255, 0, [Span(0, 0, (0, 0, 0), WHT)])]))
    g = [p[0] for p in S.render(sc, 0, 16)]
    assert g == sorted(g) and g[0] < g[-1] and g[-1] > 0.9 * FULL
    two = S.parse(one([Frame(0, 0, 255, 0, [Span(0, 0, RED), Span(0, 128, BLU)])]))
    px = S.render(two, 0, 4)
    assert px[0] == [0, 0, FULL] and px[1] == [0, 0, FULL] and px[2] == [FULL, 0, 0] and px[3] == [FULL, 0, 0]


def point(scroll, ease=0):
    return S.parse(one([Frame(10, ease, 255, scroll, [Span(5, 1, WHT, flags=1)]), Frame(0, 0, 255, 0, [Span(5, 1, WHT, flags=1)])]))


def test_scroll_positive_and_negative():
    # n=10, dt=10*100 ms; halverwege (w=1/2) is de verschuiving scroll*N/2 posities = +-1,25 led bij scroll +-64
    assert lit(S.render(point(64), 500, 10)) == [6, 7]
    assert lit(S.render(point(-64), 500, 10)) == [3, 4]
    assert lit(S.render(point(0), 500, 10)) == [5]
    # negatief scroll rekent met floor: een kleine negatieve verschuiving gaat altijd een beetje naar links
    px = S.render(point(-1), 999, 10)
    assert lit(px) == [4, 5] and px[4][0] < px[5][0]
    # meer dan een hele strip (scroll 300 = 1,17 strip) wikkelt
    assert lit(S.render(point(256), 0, 10)) == [5] and lit(S.render(point(256), 999, 10)) in ([4, 5], [5])
    full = S.parse(one([Frame(10, 1, 255, -300, [Span(5, 1, WHT, flags=1)]), Frame(0, 0, 255, 0, [Span(5, 1, WHT, flags=1)])]))
    assert lit(S.render(full, 500, 10)) == [0, 9]  # sh = -300*10/2 = -1500 posities: led 5 (1280) -> 1280-1500 = -220 = 2340 (mod 2560): led 9 en 0


def test_ease_modes():
    def two(ease):
        return S.parse(one([Frame(100, ease, 255, 0, [Span(0, 0, (0, 0, 0))]), Frame(0, 0, 255, 0, [Span(0, 0, WHT)])]))

    e0, e1, e2 = two(0), two(1), two(2)
    t_q, t_m, t_e = 2500, 5000, 9999
    assert S.render(e0, t_e, 1)[0] == [0, 0, 0], "ease 0 houdt vast en springt pas op het volgende frame"
    assert S.render(e1, t_q, 1)[0][0] > S.render(e2, t_q, 1)[0][0] > 0, "smoothstep begint trager"
    assert abs(S.render(e1, t_m, 1)[0][0] - S.render(e2, t_m, 1)[0][0]) <= 1, "in het midden zijn ze gelijk"
    assert S.render(e1, t_e, 1)[0][0] <= S.render(e2, t_e, 1)[0][0] <= FULL, "smoothstep eindigt sneller"
    v = [S.render(e2, t, 1)[0][0] for t in range(0, 10000, 250)]
    assert v == sorted(v)
    assert S.render(e0, 10000, 1)[0] == [FULL] * 3, "na dit frame (loops=1) het eindbeeld"


def test_finite_loops_and_end_phase():
    sc = S.parse(one([Frame(10, 1, 255, 0, [Span(0, 0, RED)]), Frame(10, 0, 255, 0, [Span(0, 0, BLU)])], loops=2, end=0, fade=0))
    assert S.span_ms(sc) == 2000 and S.play_ms(sc) == 4000 and S.total_ms(sc) == 4000 and S.is_bounded(sc)
    for t in (0, 1, 999, 1000, 1999):
        assert S.render(sc, t, 3) == S.render(sc, t + 2000, 3)
    assert S.render(sc, 3999, 3) != [[0, 0, 0]] * 3
    assert S.render(sc, 4000, 3) == [[0, 0, 0]] * 3 and S.render(sc, 10**9, 3) == [[0, 0, 0]] * 3
    # loops=2 en end=1: het eindbeeld is frame 0 (niet het laatste)
    sc = S.parse(one([Frame(10, 1, 255, 0, [Span(0, 0, RED)]), Frame(10, 0, 255, 0, [Span(0, 0, BLU)])], loops=2, end=1, hold=2, fade=0))
    assert S.render(sc, 4000, 1)[0] == [FULL, 0, 0] and S.render(sc, 5999, 1)[0] == [FULL, 0, 0] and S.render(sc, 6000, 1)[0] == [0, 0, 0]
    # loops=1: het eindbeeld is het laatste frame
    sc = S.parse(one([Frame(10, 1, 255, 0, [Span(0, 0, RED)]), Frame(0, 0, 255, 0, [Span(0, 0, BLU)])], loops=1, end=1, hold=1, fade=0))
    assert S.render(sc, 1000, 1)[0] == [0, 0, FULL]


def test_end_zero_fades_out():
    sc = S.parse(one([Frame(10, 1, 255, 0, [Span(0, 0, WHT)]), Frame(0, 0, 255, 0, [Span(0, 0, WHT)])], loops=1, end=0, fade=10))
    assert S.total_ms(sc) == 1000 + 1000
    assert S.render(sc, 999, 1)[0] == [FULL] * 3
    assert S.render(sc, 1000, 1)[0] == [FULL] * 3, "de fade begint op volle sterkte"
    half = S.render(sc, 1500, 1)[0]
    assert half == [FULL * 32768 >> 16] * 3
    vals = [S.render(sc, t, 1)[0][0] for t in range(1000, 2000, 50)]
    assert vals == sorted(vals, reverse=True) and vals[-1] > 0
    assert S.render(sc, 2000, 1)[0] == [0, 0, 0] and S.render(sc, 99999, 1)[0] == [0, 0, 0]
    assert S.fade_gain(0, 10) == 65536 and S.fade_gain(500, 10) == 32768 and S.fade_gain(1000, 10) == 0 and S.fade_gain(0, 0) == 0 and S.fade_gain(5, 0) == 0


def test_hold_then_fade_and_unbounded():
    sc = S.parse(S.gen_static((255, 255, 255), hold_s=3, fade=2))
    assert S.total_ms(sc) == 3200
    for t in (0, 1500, 2999, 3000):
        assert S.render(sc, t, 2) == [[FULL] * 3] * 2
    assert 0 < S.render(sc, 3100, 2)[0][0] < FULL
    assert S.render(sc, 3200, 2) == [[0, 0, 0]] * 2
    # onbegrensd: loops=1, end=1, hold=0 blijft staan; loops=0 loopt door
    nl = S.parse(S.gen_static((255, 255, 255)))
    assert S.total_ms(nl) is None and not S.is_bounded(nl)
    assert S.render(nl, 0, 1) == S.render(nl, 86_400_000 * 3, 1) == [[FULL] * 3]
    rb = S.parse(S.gen_rainbow())
    assert S.render(rb, 123_456_789, 20) == S.render(rb, 123_456_789 % 5020, 20)
    ez = S.parse(one([Frame(10, 1, 255, 0, [Span(0, 0, RED)]), Frame(10, 1, 255, 0, [Span(0, 0, BLU)])], loops=0, end=0))
    assert not S.is_bounded(ez) and S.total_ms(ez) is None


def test_render_rejects_negative_time_and_handles_one_and_many_leds():
    sc = S.parse(S.gen_rainbow())
    with pytest.raises(ValueError):
        S.render(sc, -1, 3)
    assert len(S.render(sc, 7, 1)) == 1 and len(S.render(sc, 7, 300)) == 300
    assert all(0 <= v <= 65535 for p in S.render(sc, 7, 300) for v in p)


def test_stop_by_the_device_uses_fade_gain():
    # het device fadet het huidige beeld: beeld * fade_gain >> 16
    sc = S.parse(S.gen_static((255, 255, 255), fade=20))
    img = S.render(sc, 0, 1)[0]
    assert S.fade_gain(1000, 20) == 32768 and [v * S.fade_gain(1000, 20) >> 16 for v in img] == [FULL // 2] * 3


# ------------------------------------------------------------ zonsopkomst

def test_sunrise_is_monotonically_brighter():
    for n in (1, 20, 300):
        sc = S.parse(S.gen_sunrise(20, 20))
        prev = None
        totals = []
        for t in range(0, 20 * 60_000 + 1, 6_000 if n == 300 else 1_500):
            px = S.render(sc, t, n)
            if prev is not None:
                assert all(a >= b for p, q in zip(px, prev) for a, b in zip(p, q)), (n, t)
            prev = px
            totals.append(sum(map(sum, px)))
        assert totals[0] == 0 and totals[-1] > totals[len(totals) // 2] > 0
        assert S.render(sc, 20 * 60_000 + 10**6, n) == prev, "daarna blijft het eindbeeld (hold=0)"


def test_sunrise_grows_from_the_middle_with_a_warm_sun():
    sc = S.parse(S.gen_sunrise(20, 20))
    px = S.render(sc, 20 * 60_000, 20)
    centre, edge = px[10], px[0]
    assert centre[0] > edge[0] and centre[1] > edge[1] and centre[2] > edge[2]
    assert edge[0] > edge[1] > 0 and edge[2] == 0, "aurora/omgeving is rood-oranje zonder blauw"
    half = S.render(sc, 10 * 60_000, 20)
    assert lit(half) and sum(map(sum, half)) < sum(map(sum, px))
    # de zon is zo breed als sun_pct (20 % van 20 leds = 4 leds) en ligt in het midden
    assert [i for i, p in enumerate(px) if p[2] > 30000] == [8, 9, 10, 11]
    assert [len([1 for p in S.render(S.parse(S.gen_sunrise(20, pct)), 20 * 60_000, 20) if p[2] > 0]) for pct in (0, 50, 100)] == [0, 10, 20]


def test_wake_ends_in_wake_colour_then_off():
    sc = S.parse(S.gen_wake(2, 1, (0, 255, 0), fade=10))
    assert S.total_ms(sc) == 120_000 + 60_000 + 1000
    img = S.render(sc, 130_000, 5)
    assert all(p[0] == 0 and p[1] == FULL and p[2] == 0 for p in img)
    assert S.render(sc, 120_000 + 60_000 + 1000, 5) == [[0, 0, 0]] * 5
    assert S.render(sc, 120_000 + 60_000 + 500, 5)[0][1] == FULL * 32768 >> 16


# ------------------------------------------------------------ bibliotheekdefinities

def test_compile_def_kinds_equal_the_generators():
    assert S.compile_def({"kind": "static"}) == S.gen_static()
    assert S.compile_def({"kind": "static", "color": "#412614", "lum": 100, "hold_s": 5}) == S.gen_static((0x41, 0x26, 0x14), lum=100, hold_s=5)
    assert S.compile_def({"kind": "fade", "c1": "#ff0000", "c2": [0, 0, 255], "minutes": 5.5, "hold_s": 10}) == S.gen_fade(RED, BLU, 5.5, hold_s=10)
    assert S.compile_def({"kind": "sunrise", "minutes": 30, "sun_pct": 10}) == S.gen_sunrise(30, 10)
    assert S.compile_def({"kind": "wake", "sun_min": 10, "wake_min": 5, "wake_color": "#00ff00"}) == S.gen_wake(10, 5, GRN)
    assert S.compile_def({"kind": "rainbow", "period_s": 8}) == S.gen_rainbow(8)
    assert S.compile_def({"kind": "rainbow"}) == S.gen_rainbow()


def test_compile_def_raw():
    d = {"kind": "raw", "header": {"loops": 1, "end": 0, "tick": 50, "fade": 4},
         "frames": [{"dt": 10, "ease": 2, "lum": 200, "scroll": -5, "spans": [{"a": 3, "len": 2, "leds": True, "c1": "#ff0000", "c2": [0, 0, 255]}, {"c1": [1, 2, 3]}]},
                    {"dt": 0, "spans": []}]}
    b = S.compile_def(d)
    sc = S.parse(b)
    assert (sc.end, sc.tick, sc.fade, sc.resume, sc.loops) == (0, 50, 4, 65535, 1)
    f = sc.frames[0]
    assert (f.dt, f.ease, f.lum, f.scroll) == (10, 2, 200, -5)
    assert f.spans[0] == Span(3, 2, RED, BLU, 1) and f.spans[1] == Span(0, 256, (1, 2, 3), (1, 2, 3), 0) and sc.frames[1].lum == 255
    d["frames"][0]["spans"][0] = {"a": 3, "len": 2, "flags": 1, "c1": RED, "c2": BLU}
    assert S.compile_def(d) == b


INVALID_DEFS = [
    ("geen dict", [1, 2]),
    ("geen kind", {}),
    ("kind onbekend", {"kind": "disco"}),
    ("param onbekend", {"kind": "static", "colour": "#ffffff"}),
    ("kleur", {"kind": "static", "color": "rood"}),
    ("kleur rgb", {"kind": "static", "color": [1, 2]}),
    ("kleur bereik", {"kind": "static", "color": [1, 2, 256]}),
    ("kleur bool", {"kind": "static", "color": [True, 2, 3]}),
    ("lum", {"kind": "static", "lum": 256}),
    ("lum str", {"kind": "static", "lum": "100"}),
    ("hold", {"kind": "static", "hold_s": 43201}),
    ("fade", {"kind": "static", "fade": 256}),
    ("fade ontbreekt", {"kind": "fade", "c1": "#000000", "c2": "#ffffff"}),
    ("minutes negatief", {"kind": "fade", "c1": "#000000", "c2": "#ffffff", "minutes": -1}),
    ("minutes nan", {"kind": "sunrise", "minutes": float("nan")}),
    ("minutes te lang", {"kind": "sunrise", "minutes": 1441}),
    ("sunrise zonder minutes", {"kind": "sunrise"}),
    ("sun_pct", {"kind": "sunrise", "minutes": 10, "sun_pct": 101}),
    ("wake_min", {"kind": "wake", "wake_min": 0}),
    ("wake_min te lang", {"kind": "wake", "wake_min": 721}),
    ("period", {"kind": "rainbow", "period_s": 0}),
    ("period str", {"kind": "rainbow", "period_s": "5"}),
    ("raw zonder frames", {"kind": "raw"}),
    ("raw lege frames", {"kind": "raw", "frames": []}),
    ("raw header", {"kind": "raw", "header": [], "frames": [{}]}),
    ("raw headerveld", {"kind": "raw", "header": {"looping": 1}, "frames": [{}]}),
    ("raw framveld", {"kind": "raw", "frames": [{"duur": 1}]}),
    ("raw ease 3", {"kind": "raw", "frames": [{"ease": 3}]}),
    ("raw dt voor laatste", {"kind": "raw", "header": {"loops": 1}, "frames": [{"dt": 5}]}),
    ("raw span zonder c1", {"kind": "raw", "frames": [{"spans": [{"a": 0}]}]}),
    ("raw len 257", {"kind": "raw", "frames": [{"spans": [{"len": 257, "c1": "#000000"}]}]}),
    ("raw 17 spans", {"kind": "raw", "frames": [{"spans": [{"c1": "#000000"}] * 17}]}),
    ("raw hold bij end 0", {"kind": "raw", "header": {"end": 0, "hold": 5}, "frames": [{}]}),
]


@pytest.mark.parametrize("name,defn", INVALID_DEFS, ids=[x[0] for x in INVALID_DEFS])
def test_compile_def_rejects_with_scene_error(name, defn):
    with pytest.raises(SceneError) as e:
        S.compile_def(defn)
    assert str(e.value)


# ------------------------------------------------------------ voorbeeld voor de UI

def test_preview():
    p = S.preview({"kind": "wake", "sun_min": 2, "wake_min": 1}, n=12, samples=30)
    assert p["n"] == 12 and p["bounded"] is True and p["total_ms"] == 180_000 + 5000 and p["window_ms"] == p["total_ms"]
    assert len(p["times"]) == len(p["frames"]) == 30 and p["times"][0] == 0 and p["times"][-1] == p["window_ms"]
    assert all(len(f) == 12 and all(len(px) == 3 and all(0 <= v <= 255 and isinstance(v, int) for v in px) for px in f) for f in p["frames"])
    assert p["frames"][0][0] == [0, 0, 0] and p["frames"][-1][0] == [0, 0, 0] and max(max(px) for px in p["frames"][len(p["frames"]) // 2]) > 0
    q = S.preview(S.gen_rainbow(), samples=10)  # bytes; onbegrensd: een ronde
    assert q["bounded"] is False and q["total_ms"] is None and q["window_ms"] == 5020 and len(q["times"]) == 10 and q["n"] == 20
    s = S.preview(S.gen_static((255, 255, 255)))  # geen duur: één beeld
    assert s["times"] == [0] and s["frames"] == [[[255, 255, 255]] * 20]
    assert S.preview(S.gen_static((255, 255, 255), lum=255))["frames"][0][0] == [255, 255, 255]
    with pytest.raises(SceneError):
        S.preview({"kind": "nope"})
    with pytest.raises(SceneError):
        S.preview(b"garbage")


# ------------------------------------------------------------ helderheid per scène (0.44)

# De bytes van vóór de helderheid (0.43), vastgelegd met de toenmalige generatoren: bij brightness 100 (of zonder) moet het identiek blijven, anders krijgt elke bestaande scène een nieuwe hash en wordt alles opnieuw gesynchroniseerd.
BASELINE = [
    ({"kind": "static"},
     "4c5301010101640000001400ffff0000000000ff00000100000041260a41260a"),
    ({"kind": "static", "color": "#412614", "lum": 100, "hold_s": 5, "fade": 3, "resume": 0},
     "4c53010101016400050003000000000000000064000001000000412614412614"),
    ({"kind": "static", "color": [255, 255, 255], "lum": 255, "hold_s": 600},
     "4c5301010101640058021400ffff0000000000ff000001000000ffffffffffff"),
    ({"kind": "fade", "c1": "#ff0000", "c2": [0, 0, 255], "minutes": 5.5, "hold_s": 10},
     "4c530102010164000a001400ffff0000e40c01ff000001000000ff0000ff0000000000ff0000010000000000ff0000ff"),
    ({"kind": "fade", "c1": "#000000", "c2": "#ffa040", "minutes": 10, "lum": 200, "fade": 0},
     "4c5301020101640000000000ffff0000701701c8000001000000000000000000000000c8000001000000ffa040ffa040"),
    ({"kind": "sunrise", "minutes": 20},
     "4c53010c010164000000140058020000420401050000010000000000000000004304011c0000030000000900000900007418007f19007f19007e0400ff5717ff571743040132000003000000120000120000692e007f19007f19007b0a00ff6e2eff6e2e430401490000030000001b00001b00005d46007f19007f1900790e00ff8646ff864643040160000003000000240000240000515e007f19007f1900771200ff9d5dff9d5d430401770000030000002d00002d00004674007f19007f1900741800ffb474ffb4744304018d0000030000003700003700003a8c007f19007f1900721c00ffcb8bffcb8b430401a40000030000004000004000002fa2007f19007f1900702000ffe2a2ffe2a2430401bb00000300000049000049000023ba007f19007f19006d2600fff9b9fff9b9430401d200000300000052000052000017d2007f19007f19006b2a00ffffd1ffffd1430401e80000030000005b00005b00000ce8007f19007f1900692e00ffffe8ffffe8000000ff0000030000006400006400000000007f19007f1900663400ffffffffffff"),
    ({"kind": "sunrise", "minutes": 30, "sun_pct": 10, "hold_s": 60, "fade": 5, "resume": 120},
     "4c53010c010164003c00050078000000640601050000010000000000000000006406011c0000030000000900000900007418007f19007f19007f0200ff5717ff571765060132000003000000120000120000692e007f19007f19007e0400ff6e2eff6e2e640601490000030000001b00001b00005d46007f19007f19007d0600ff8646ff864664060160000003000000240000240000515e007f19007f19007b0a00ff9d5dff9d5d650601770000030000002d00002d00004674007f19007f19007a0c00ffb474ffb4746406018d0000030000003700003700003a8c007f19007f1900790e00ffcb8bffcb8b640601a40000030000004000004000002fa2007f19007f1900781000ffe2a2ffe2a2650601bb00000300000049000049000023ba007f19007f1900771200fff9b9fff9b9640601d200000300000052000052000017d2007f19007f1900761400ffffd1ffffd1650601e80000030000005b00005b00000ce8007f19007f1900741800ffffe8ffffe8000000ff0000030000006400006400000000007f19007f1900731a00ffffffffffff"),
    ({"kind": "sunrise", "minutes": 0.5, "sun_pct": 0},
     "4c53010c0101640000001400580200001b0001050000010000000000000000001b00011c0000020000000900000900007418007f19007f19001b000132000002000000120000120000692e007f19007f19001c0001490000020000001b00001b00005d46007f19007f19001b000160000002000000240000240000515e007f19007f19001b0001770000020000002d00002d00004674007f19007f19001b00018d0000020000003700003700003a8c007f19007f19001c0001a40000020000004000004000002fa2007f19007f19001b0001bb00000200000049000049000023ba007f19007f19001b0001d200000200000052000052000017d2007f19007f19001c0001e80000020000005b00005b00000ce8007f19007f1900000000ff0000020000006400006400000000007f19007f1900"),
    ({"kind": "wake"},
     "4c53010c01016400b004320058020000420401050000010000000000000000004304011c0000030000000900000900007418007f19007f19007e0400ff5717ff571743040132000003000000120000120000692e007f19007f19007b0a00ff6e2eff6e2e430401490000030000001b00001b00005d46007f19007f1900790e00ff8646ff864643040160000003000000240000240000515e007f19007f1900771200ff9d5dff9d5d430401770000030000002d00002d00004674007f19007f1900741800ffb474ffb4744304018d0000030000003700003700003a8c007f19007f1900721c00ffcb8bffcb8b430401a40000030000004000004000002fa2007f19007f1900702000ffe2a2ffe2a2430401bb00000300000049000049000023ba007f19007f19006d2600fff9b9fff9b9430401d200000300000052000052000017d2007f19007f19006b2a00ffffd1ffffd1430401e80000030000005b00005b00000ce8007f19007f1900692e00ffffe8ffffe8000000ff000001000000006400006400"),
    ({"kind": "wake", "sun_min": 10, "wake_min": 5, "wake_color": "#00ff00", "sun_pct": 50, "fade": 7, "resume": 300},
     "4c53010c010164002c0107002c010000210201050000010000000000000000002102011c0000030000000900000900007418007f19007f19007a0c00ff5717ff571722020132000003000000120000120000692e007f19007f1900741800ff6e2eff6e2e210201490000030000001b00001b00005d46007f19007f19006f2200ff8646ff864622020160000003000000240000240000515e007f19007f1900692e00ff9d5dff9d5d210201770000030000002d00002d00004674007f19007f1900633a00ffb474ffb4742202018d0000030000003700003700003a8c007f19007f19005d4600ffcb8bffcb8b210201a40000030000004000004000002fa2007f19007f1900575200ffe2a2ffe2a2220201bb00000300000049000049000023ba007f19007f1900515e00fff9b9fff9b9210201d200000300000052000052000017d2007f19007f19004c6800ffffd1ffffd1220201e80000030000005b00005b00000ce8007f19007f1900467400ffffe8ffffe8000000ff00000100000000ff0000ff00"),
    ({"kind": "rainbow"},
     "4c5301020001140000001400ffff0000fa0001ff000106002b00ff0000ffff002b2a00ffff0000ff00552b0000ff0000ffff802b0000ffff0000ffab2a000000ffff00ffd52b00ff00ffff0000010000ff000006002b00ff0000ffff002b2a00ffff0000ff00552b0000ff0000ffff802b0000ffff0000ffab2a000000ffff00ffd52b00ff00ffff0000"),
    ({"kind": "rainbow", "period_s": 8, "fade": 0, "resume": 5},
     "4c530102000114000000000005000000900101ff000106002b00ff0000ffff002b2a00ffff0000ff00552b0000ff0000ffff802b0000ffff0000ffab2a000000ffff00ffd52b00ff00ffff0000010000ff000006002b00ff0000ffff002b2a00ffff0000ff00552b0000ff0000ffff802b0000ffff0000ffab2a000000ffff00ffd52b00ff00ffff0000"),
]


def test_brightness_100_gives_exactly_the_bytes_of_before():
    for defn, hexed in BASELINE:
        want = bytes.fromhex(hexed)
        assert S.compile_def(defn) == want, defn
        for b in (100, 100.0):
            assert S.compile_def({**defn, "brightness": b}) == want, (defn, b)
    assert S.gen_static(brightness=100) == S.gen_static() and S.gen_rainbow(brightness=100) == S.gen_rainbow()
    assert S.gen_wake(brightness=100) == S.gen_wake() and S.gen_sunrise(20, brightness=100) == S.gen_sunrise(20) and S.gen_fade(RED, BLU, 5, brightness=100) == S.gen_fade(RED, BLU, 5)


def lums(data):
    return [f.lum for f in S.parse(data).frames]


def test_brightness_scales_lum_per_frame_as_light_not_as_code():
    k = 0.5 ** (1 / 2.2)
    assert S.parse(S.gen_static(WHT, brightness=50)).frames[0].lum == round(255 * k) == 186
    assert S.parse(S.gen_static(WHT, lum=100, brightness=50)).frames[0].lum == round(100 * k)
    assert lums(S.gen_fade(RED, BLU, 5, lum=200, brightness=25)) == [round(200 * 0.25 ** (1 / 2.2))] * 2
    for gen, args in ((S.gen_sunrise, (20,)), (S.gen_wake, ()), (S.gen_rainbow, ())):
        full, half = lums(gen(*args)), lums(gen(*args, brightness=50))
        assert half == [int(v * k + 0.5) for v in full], gen.__name__
        assert len(full) == len(half) and half != full
    assert lums(S.gen_wake(brightness=50))[-1] == 186, "ook het laatste frame (de wekkerkleur)"
    assert lums(S.gen_sunrise(20, brightness=50)) == sorted(lums(S.gen_sunrise(20, brightness=50))), "de zonsopkomst blijft monotoon"
    # alleen lum verandert: kleuren, tijden en vorm blijven
    for gen, args in ((S.gen_sunrise, (20,)), (S.gen_wake, ()), (S.gen_rainbow, ()), (S.gen_static, ())):
        a, b = S.parse(gen(*args)), S.parse(gen(*args, brightness=30))
        assert (a.tick, a.loops, a.end, a.hold, a.fade, a.resume) == (b.tick, b.loops, b.end, b.hold, b.fade, b.resume)
        assert [(f.dt, f.ease, f.scroll, f.spans) for f in a.frames] == [(f.dt, f.ease, f.scroll, f.spans) for f in b.frames]
    # echt de helft van het licht (lineair), tot op de afronding van lum
    full = S.render(S.parse(S.gen_static(WHT)), 0, 1)[0][0]
    half = S.render(S.parse(S.gen_static(WHT, brightness=50)), 0, 1)[0][0]
    assert abs(half / full - 0.5) < 0.01
    assert S.frame_at({"kind": "static", "color": "#ffffff", "brightness": 50}, 0, 1)["frame"] == [[186, 186, 186]]
    assert S.compile_def({"kind": "wake", "brightness": 40}) == S.gen_wake(brightness=40)


def test_brightness_changes_the_hash_only_when_it_changes():
    h = lambda d: S.hash_hex(S.compile_def(d))  # noqa: E731
    base = {"kind": "sunrise", "minutes": 20}
    assert h(base) == h({**base, "brightness": 100}) == h({**base, "brightness": 100.0})
    assert h({**base, "brightness": 60}) == h({**base, "brightness": 60}) != h(base)
    assert h({**base, "brightness": 60}) != h({**base, "brightness": 61})
    assert S.hash_hex(S.gen_static()) == S.hash_hex(bytes.fromhex(BASELINE[0][1]))


@pytest.mark.parametrize("value", [0, -1, 100.5, 101, 1000, "50", None, True, [50], float("nan"), float("inf")], ids=repr)
def test_brightness_must_be_1_to_100(value):
    for kind in ({"kind": "static"}, {"kind": "fade", "c1": "#000000", "c2": "#ffffff", "minutes": 1}, {"kind": "sunrise", "minutes": 1}, {"kind": "wake"}, {"kind": "rainbow"}):
        with pytest.raises(SceneError, match="brightness"):
            S.compile_def({**kind, "brightness": value})


def test_brightness_lower_bound_and_raw():
    for kind in ({"kind": "static"}, {"kind": "sunrise", "minutes": 1}, {"kind": "wake"}, {"kind": "rainbow"}):
        assert S.parse(S.compile_def({**kind, "brightness": 1}))  # 1 % mag
    assert S.parse(S.gen_static(WHT, brightness=1)).frames[0].lum == 31
    with pytest.raises(SceneError, match="raw.*brightness|brightness.*raw|eigen definitie"):
        S.compile_def({"kind": "raw", "brightness": 50, "frames": [{}]})


# ------------------------------------------------------------ het beeld op tijdstip t (de tijdschuif)

def test_frame_at_is_the_preview_at_that_time():
    defn = {"kind": "wake", "sun_min": 2, "wake_min": 1}
    p = S.preview(defn, n=12, samples=30)
    for i in (0, 7, 15, 29):
        f = S.frame_at(defn, p["times"][i], 12)
        assert f["frame"] == p["frames"][i] and f["t_ms"] == p["times"][i]
        assert (f["total_ms"], f["window_ms"], f["bounded"]) == (p["total_ms"], p["window_ms"], p["bounded"]) == (185_000, 185_000, True)
    assert S.frame_at(defn, 0)["frame"] and len(S.frame_at(defn, 0)["frame"]) == 24, "n is standaard 24"
    f = S.frame_at(defn, 60_000, 4)
    sc = S.parse(S.compile_def(defn))
    assert f["frame"] == [[int((v / 65535) ** (1 / 2.2) * 255 + 0.5) for v in px] for px in S.render(sc, 60_000, 4)], "dezelfde render als het apparaat"
    assert S.frame_at(S.compile_def(defn), 60_000, 4) == f, "ook voor bytes"
    assert S.frame_at(defn, 185_000, 2)["frame"] == [[0, 0, 0]] * 2 and S.frame_at(defn, 86_400_000, 2)["frame"] == [[0, 0, 0]] * 2, "voorbij het einde: uit"


def test_frame_at_window_of_an_unbounded_scene_is_a_cycle_or_nothing():
    r = S.frame_at({"kind": "rainbow", "period_s": 5}, 1000, 6)
    assert (r["bounded"], r["total_ms"], r["window_ms"]) == (False, None, 5020)
    assert S.frame_at({"kind": "rainbow", "period_s": 5}, 1000 + 5020, 6)["frame"] == r["frame"], "een lus wikkelt"
    s = S.frame_at({"kind": "static", "color": "#00ff00"}, 5_000, 3)
    assert (s["bounded"], s["window_ms"], s["frame"]) == (False, 0, [[0, 255, 0]] * 3)
    assert S.frame_at({"kind": "static", "hold_s": 60}, 0)["window_ms"] == 60_000 + 2_000 == S.frame_at({"kind": "static", "hold_s": 60}, 0)["total_ms"]


def test_frame_at_validates():
    for t in (-1, 86_400_001, 1.5, "0", None, True):
        with pytest.raises(SceneError, match="t_ms"):
            S.frame_at({"kind": "static"}, t)
    for n in (0, 301, "3", None):
        with pytest.raises(SceneError, match="n"):
            S.frame_at({"kind": "static"}, 0, n)
    with pytest.raises(SceneError):
        S.frame_at({"kind": "nope"}, 0)
    with pytest.raises(SceneError):
        S.frame_at(b"garbage", 0)


# ------------------------------------------------------------ gedeelde vectoren met de C++-kant

needs_project = pytest.mark.skipif(not (LEDSCENE / "tools" / "gen_vectors.py").exists(), reason="projectmap niet beschikbaar")


@needs_project
def test_vectors_file_is_what_gen_vectors_writes_now():
    gv = load_tool("gen_vectors")
    have = (LEDSCENE / "vectors" / "vectors.txt").read_text(encoding="utf-8")
    assert have == gv.generate(), "protocol/ledscene/vectors/vectors.txt wijkt af: draai protocol/ledscene/tools/gen_vectors.py (en tests/run.sh)"


@needs_project
def test_vectors_cover_what_the_task_asks():
    txt = (LEDSCENE / "vectors" / "vectors.txt").read_text(encoding="utf-8").splitlines()
    scenes = [ln.split()[1:3] for ln in txt if ln.startswith("scene ")]
    names = {s[0].rsplit("_n", 1)[0] for s in scenes}
    assert len(names) >= 10
    for want in ("nachtlamp", "wekker", "regenboog", "per_led", "wrap_span", "neg_scroll", "ease2", "loops2", "end0_fade", "onbegrensd_end0"):
        assert want in names, want
    assert {"1", "300"} <= {n for _, n in scenes}
    # elke vector komt overeen met de referentie en met parse
    n = None
    cur = None
    checked = 0
    for ln in txt:
        w = ln.split()
        if not w or w[0].startswith("#"):
            continue
        if w[0] == "scene":
            n, cur = int(w[2]), S.parse(bytes.fromhex(w[3]))
        elif w[0] == "t" and checked < 200:
            got = [v for px in S.render(cur, int(w[1]), n) for v in px]
            assert w[2] == struct.pack("<%dH" % len(got), *got).hex()
            checked += 1
        elif w[0] == "bad":
            with pytest.raises(SceneError) as e:
                S.parse(bytes.fromhex(w[3]) if w[3] != "-" else b"", int(w[2]) or None)
            assert str(e.value) == " ".join(w[4:])
    assert checked == 200
