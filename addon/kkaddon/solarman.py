"""Het lokale Solarman V5-protocol van de wifi-stick (LSW-3) op een omvormer, zonder cloud en zonder de Solarman-integratie.

De stick luistert op TCP 8899. Elk bericht is een V5-frame met daarin een gewoon Modbus-RTU-bericht (adres 1, CRC):
  A5 | lengte(2) | 10 45 (vraag) of 10 15 (antwoord) | volgnummer(2) | serienummer van de stick(4, little endian) | inhoud | som | 15
De inhoud van een vraag is: 02, sensortype(2), drie keer 4 nullen, dan Modbus. Een antwoord heeft 14 bytes ervoor (type, status, 3 x 4).
De stick stuurt ook uit zichzelf frames (hartslag, 0x4710 e.d.): we letten alleen op het antwoord op onze vraag.
De referentie is de Solarman-integratie voor Home Assistant (de registerlijst van de Deye SUN-xK-SG04LP3); die registers zijn hier
op een echte omvormer nagelopen.

Elke ronde verbinden we opnieuw: de stick heeft maar plaats voor een paar verbindingen en Solarman zelf (de app) gebruikt er ook een.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Callable

log = logging.getLogger("kk.solarman")

PORT = 8899
POLL_EVERY = 15.0  # s: de stick meldt niets uit zichzelf
TIMEOUT = 5.0  # s: zo lang wachten op een antwoord
MISSES = 3  # zoveel ronden zonder antwoord: niet meer bereikbaar
# (eerste register, aantal): wat we elke ronde lezen (functiecode 3). Deye SG04LP3: zie `decode`.
BLOCKS = ((0x0202, 16), (0x024A, 6), (0x0271, 1), (0x028D, 1), (0x02A0, 8))
MAX_FRAME = 1024


class SolarmanError(Exception):
    pass


def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


def modbus_read(start: int, count: int, unit: int = 1, function: int = 3) -> bytes:
    msg = bytes([unit, function]) + struct.pack(">HH", start, count)
    return msg + crc16(msg)


def frame(serial: int, seq: int, modbus: bytes) -> bytes:
    """Een V5-vraag met een Modbus-bericht erin."""
    body = b"\x02" + struct.pack("<H", 0) + bytes(12) + modbus
    head = struct.pack("<BHHHI", 0xA5, len(body), 0x4510, seq & 0xFFFF, serial)
    raw = head + body
    return raw + bytes([sum(raw[1:]) & 0xFF, 0x15])


def split(buf: bytearray) -> list[tuple[int, int, int, bytes]]:
    """Haalt de volledige frames uit `buf` (en laat een half frame staan): (controlecode, volgnummer, serienummer, inhoud).
    Een frame met een foute som of zonder eindbyte wordt weggegooid."""
    out = []
    while True:
        i = buf.find(b"\xa5")
        if i < 0:
            buf.clear()
            return out
        del buf[:i]
        if len(buf) < 13:
            return out
        n = struct.unpack_from("<H", buf, 1)[0]
        total = 13 + n
        if n > MAX_FRAME:
            del buf[:1]
            continue
        if len(buf) < total:
            return out
        raw = bytes(buf[:total])
        if raw[-1] != 0x15 or sum(raw[1:-2]) & 0xFF != raw[-2]:
            del buf[:1]  # geen frame: misschien begon het echte iets verder
            continue
        del buf[:total]
        code, seq, serial = struct.unpack_from("<HHI", raw, 3)
        out.append((code, seq, serial, raw[11:-2]))


def modbus_data(payload: bytes, function: int = 3, unit: int = 1) -> list[int]:
    """De registers uit het antwoord (de inhoud van een V5-antwoord). Een foute CRC of een uitzondering is een fout."""
    mb = payload[14:]
    if len(mb) < 5:
        raise SolarmanError("antwoord te kort")
    if mb[-2:] != crc16(mb[:-2]):
        raise SolarmanError("CRC klopt niet")
    if mb[0] != unit:
        raise SolarmanError(f"antwoord van adres {mb[0]}")
    if mb[1] == function | 0x80:
        raise SolarmanError(f"de omvormer weigert (Modbus-fout {mb[2] if len(mb) > 2 else '?'})")
    if mb[1] != function or len(mb) < 3 + mb[2] + 2 or mb[2] % 2:
        raise SolarmanError("onverwacht antwoord")
    return list(struct.unpack(f">{mb[2] // 2}H", mb[3:3 + mb[2]]))


# ------------------------------------------------------------ de registers (Deye SG04LP3 en verwanten)


def s16(v: int) -> int:
    return v - 65536 if v >= 32768 else v


def decode(regs: dict[int, int]) -> dict[str, float | int | None]:
    """Gelezen registers -> de waarden. Wat ontbreekt, is None. Tekens: `batt` is positief bij laden, `grid` positief bij afname."""
    def one(a, scale=1.0, signed=False):
        v = regs.get(a)
        if v is None:
            return None
        v = s16(v) if signed else v
        return round(v * scale, 2) if scale != 1.0 else v

    def both(*xs):
        got = [x for x in xs if x is not None]
        return sum(got) if got else None

    batt = one(0x024E, 1, True)
    return {
        "pv1": one(0x02A0), "pv2": one(0x02A1), "pv": both(one(0x02A0), one(0x02A1)),
        "pv1_v": one(0x02A4, 0.1), "pv2_v": one(0x02A6, 0.1), "pv1_a": one(0x02A5, 0.1), "pv2_a": one(0x02A7, 0.1),
        "load": one(0x028D), "grid": one(0x0271, 1, True),
        "batt": -batt if batt is not None else None,  # de omvormer telt ontladen positief; wij laden
        "batt_v": one(0x024B, 0.01), "soc": one(0x024C),
        "batt_t": (regs[0x024A] - 1000) / 10 if 0x024A in regs else None,
        "today": one(0x0211, 0.1), "bought": one(0x0208, 0.1), "sold": one(0x0209, 0.1),
        "load_today": one(0x020E, 0.1), "batt_in": one(0x0202, 0.1), "batt_out": one(0x0203, 0.1),
    }


class SolarmanClient:
    """Eén stick. `run()` leest elke ronde de registers en meldt de waarden; `connected` is false na MISSES mislukte ronden."""

    def __init__(self, host: str, serial: int, port: int = PORT, on_values: Callable[[dict], None] | None = None,
                 on_link: Callable[[bool], None] | None = None, poll: float | None = None) -> None:
        self.host, self.serial, self.port = host, int(serial), port
        self.on_values, self.on_link = on_values, on_link
        self.poll = poll or POLL_EVERY
        self.connected = False
        self.last_error = ""
        self._seq = 0
        self._misses = 0

    async def read_all(self) -> dict[int, int]:
        regs: dict[int, int] = {}
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), TIMEOUT)
        except (OSError, asyncio.TimeoutError) as e:
            raise SolarmanError(f"geen verbinding met {self.host}:{self.port} ({e or 'time-out'})") from None
        try:
            buf = bytearray()
            for start, count in BLOCKS:
                self._seq = (self._seq + 1) & 0xFFFF
                w.write(frame(self.serial, self._seq, modbus_read(start, count)))
                await w.drain()
                payload = await self._answer(r, buf, self._seq)
                for i, v in enumerate(modbus_data(payload)[:count]):
                    regs[start + i] = v
        finally:
            w.close()
            try:
                await w.wait_closed()
            except OSError:
                pass
        return regs

    async def _answer(self, r: asyncio.StreamReader, buf: bytearray, seq: int) -> bytes:
        loop = asyncio.get_running_loop()
        end = loop.time() + TIMEOUT
        while True:
            for code, s, serial, body in split(buf):
                if code == 0x1510 and s & 0xFF == seq & 0xFF:  # de stick telt zelf in de hoge byte, de lage herhaalt onze vraag
                    if serial != self.serial:
                        raise SolarmanError(f"dit is stick {serial}, niet {self.serial}")
                    return body
            left = end - loop.time()
            if left <= 0:
                raise SolarmanError("geen antwoord van de omvormer (slaapt hij? 's nachts zwijgt hij vaak)")
            try:
                data = await asyncio.wait_for(r.read(2048), left)
            except asyncio.TimeoutError:
                continue
            if not data:
                raise SolarmanError("de stick sloot de verbinding")
            buf.extend(data)

    async def poll_once(self) -> bool:
        try:
            regs = await self.read_all()
        except SolarmanError as e:
            self._misses += 1
            self.last_error = str(e)
            if self._misses >= MISSES and self.connected:
                self.connected = False
                if self.on_link:
                    self.on_link(False)
            return False
        except Exception as e:  # noqa: BLE001 - een rare fout mag de lus niet stoppen
            log.exception("solarman %s", self.host)
            self._misses += 1
            self.last_error = str(e)
            return False
        self._misses = 0
        self.last_error = ""
        was = self.connected
        self.connected = True
        if self.on_values:
            self.on_values(decode(regs))
        if not was and self.on_link:
            self.on_link(True)
        return True

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.poll)
