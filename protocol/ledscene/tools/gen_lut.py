#!/usr/bin/env python3
"""Schrijft protocol/ledscene/ledscene_lut.h: de vaste tabel T[0..256] van ledscene/1 (T[i] = round(65535 * (i/255)**2.2), T[256] = T[255]).

Gebruik: gen_lut.py            schrijft het headerbestand
         gen_lut.py --check    controleert dat het headerbestand en addon/kkaddon/scenes.py (LUT) precies de formule volgen (exit 1 bij een verschil)"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "ledscene_lut.h"
sys.path.insert(0, str(HERE.parents[2] / "addon"))


def table():
    t = [round(65535 * (i / 255) ** 2.2) for i in range(256)]
    return t + [t[255]]


def header_text():
    t = table()
    rows = "\n".join("    " + ", ".join(str(x) for x in t[r:r + 8]) + "," for r in range(0, 257, 8))
    return ("// Gegenereerd door protocol/ledscene/tools/gen_lut.py; niet met de hand aanpassen.\n"
            "// T[i] = round(65535 * (i/255)^2.2) voor i = 0..255, en T[256] = T[255] (LEDSCENE.md §5).\n"
            "#pragma once\n#include <stdint.h>\n\nnamespace ledscene {\n\n"
            "static const uint16_t kLut[257] = {\n" + rows + "\n};\n\n}  // namespace ledscene\n")


def main(argv):
    if "--check" in argv:
        from kkaddon import scenes
        bad = []
        if list(scenes.LUT) != table():
            bad.append("addon/kkaddon/scenes.py: LUT wijkt af van de formule")
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != header_text():
            bad.append("ledscene_lut.h wijkt af van wat gen_lut.py schrijft")
        for b in bad:
            print(b)
        return 1 if bad else 0
    OUT.write_text(header_text(), encoding="utf-8", newline="\n")
    print("geschreven:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
