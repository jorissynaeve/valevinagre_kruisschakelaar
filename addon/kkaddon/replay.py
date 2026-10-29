"""Dedup en replay-bescherming: per bron een schuivend venster van 64 frame-sleutels (boot<<16 | ctr).

Dezelfde logica als core/include/kk/replay.h, zonder de grens van 48 bronnen.
"""
from __future__ import annotations


class ReplayTable:
    WINDOW = 64

    def __init__(self) -> None:
        self._e: dict[int, list[int]] = {}  # src -> [hoogste sleutel, bitvenster]

    def accept(self, src: int, key: int) -> bool:
        """True als het frame nieuw is (en wordt onthouden), False bij een duplicaat of een te oud frame."""
        e = self._e.get(src)
        if e is None:
            self._e[src] = [key, 1]
            return True
        d = ((key - e[0] + 0x80000000) & 0xFFFFFFFF) - 0x80000000  # int32 verschil
        if d > 0:
            e[1] = 0 if d >= self.WINDOW else ((e[1] << d) & 0xFFFFFFFFFFFFFFFF)
            e[1] |= 1
            e[0] = key
            return True
        age = -d
        if age >= self.WINDOW:
            return False
        bit = 1 << age
        if e[1] & bit:
            return False
        e[1] |= bit
        return True
