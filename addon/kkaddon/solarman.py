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
import re
import struct
from typing import Callable

log = logging.getLogger("kk.solarman")

PORT = 8899
POLL_EVERY = 15.0  # s: de stick meldt niets uit zichzelf
TIMEOUT = 5.0  # s: zo lang wachten op een antwoord
MISSES = 3  # zoveel ronden zonder antwoord: niet meer bereikbaar
MAX_FRAME = 1024
DISCOVERY_PORT = 48899  # de stick antwoordt hier op de vraag van de Solarman-app (en van HA's Solarman-integratie)
DISCOVER = b"WIFIKIT-214028-READ"
MAC_RE = re.compile(r"^[0-9A-Fa-f]{12}$")


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
# Eén tabel voor alles: wat we lezen, wat op het schema een uitgang is en wat in HA een sensor wordt.
# (sleutel, groep, naam, eenheid, registers, soort, schaal, HA-klasse, HA-staat)
#   soort: u16, s16 (met teken), s16n (met teken, omgedraaid), u32 (twee registers, het eerste is het lage), sum (som van de registers),
#          temp (u16, min 1000), ...
# Tekens: `batt` en `batt_a` zijn positief bij laden (de omvormer meldt laden negatief); `grid` is positief bij afname.
M, TI = "measurement", "total_increasing"
FIELDS = (
    ("pv", "Zon", "zon totaal", "W", (0x02A0, 0x02A1), "sum", 1, "power", M),
    ("pv1", "Zon", "zon PV1", "W", (0x02A0,), "u16", 1, "power", M),
    ("pv2", "Zon", "zon PV2", "W", (0x02A1,), "u16", 1, "power", M),
    ("pv1_v", "Zon", "spanning PV1", "V", (0x02A4,), "u16", 0.1, "voltage", M),
    ("pv2_v", "Zon", "spanning PV2", "V", (0x02A6,), "u16", 0.1, "voltage", M),
    ("pv1_a", "Zon", "stroom PV1", "A", (0x02A5,), "u16", 0.1, "current", M),
    ("pv2_a", "Zon", "stroom PV2", "A", (0x02A7,), "u16", 0.1, "current", M),
    ("today", "Zon", "opbrengst vandaag", "kWh", (0x0211,), "u16", 0.1, "energy", TI),
    ("total", "Zon", "opbrengst totaal", "kWh", (0x0216, 0x0217), "u32", 0.1, "energy", TI),
    ("batt", "Accu", "accu (+ laden)", "W", (0x024E,), "s16n", 1, "power", M),
    ("batt_a", "Accu", "accustroom (+ laden)", "A", (0x024F,), "s16n", 0.01, "current", M),
    ("batt_v", "Accu", "accuspanning", "V", (0x024B,), "u16", 0.01, "voltage", M),
    ("soc", "Accu", "accu %", "%", (0x024C,), "u16", 1, "battery", M),
    ("batt_t", "Accu", "accutemperatuur", "°C", (0x024A,), "temp", 0.1, "temperature", M),
    ("batt_in", "Accu", "accu geladen vandaag", "kWh", (0x0202,), "u16", 0.1, "energy", TI),
    ("batt_out", "Accu", "accu ontladen vandaag", "kWh", (0x0203,), "u16", 0.1, "energy", TI),
    ("batt_in_tot", "Accu", "accu geladen totaal", "kWh", (0x0204, 0x0205), "u32", 0.1, "energy", TI),
    ("batt_out_tot", "Accu", "accu ontladen totaal", "kWh", (0x0206, 0x0207), "u32", 0.1, "energy", TI),
    ("grid", "Net", "net (+ afname)", "W", (0x0271,), "s16", 1, "power", M),
    ("grid_l1", "Net", "net L1 (externe CT)", "W", (0x0268,), "s16", 1, "power", M),
    ("grid_l2", "Net", "net L2 (externe CT)", "W", (0x0269,), "s16", 1, "power", M),
    ("grid_l3", "Net", "net L3 (externe CT)", "W", (0x026A,), "s16", 1, "power", M),
    ("ict_l1", "Net", "interne CT L1", "W", (0x025C,), "s16", 1, "power", M),
    ("ict_l2", "Net", "interne CT L2", "W", (0x025D,), "s16", 1, "power", M),
    ("ict_l3", "Net", "interne CT L3", "W", (0x025E,), "s16", 1, "power", M),
    ("vgrid_l1", "Net", "netspanning L1", "V", (0x0256,), "u16", 0.1, "voltage", M),
    ("vgrid_l2", "Net", "netspanning L2", "V", (0x0257,), "u16", 0.1, "voltage", M),
    ("vgrid_l3", "Net", "netspanning L3", "V", (0x0258,), "u16", 0.1, "voltage", M),
    ("freq", "Net", "netfrequentie", "Hz", (0x027E,), "u16", 0.01, "frequency", M),
    ("bought", "Net", "afgenomen vandaag", "kWh", (0x0208,), "u16", 0.1, "energy", TI),
    ("sold", "Net", "geleverd vandaag", "kWh", (0x0209,), "u16", 0.1, "energy", TI),
    ("bought_tot", "Net", "afgenomen totaal", "kWh", (0x020A, 0x020B), "u32", 0.1, "energy", TI),
    ("sold_tot", "Net", "geleverd totaal", "kWh", (0x020C, 0x020D), "u32", 0.1, "energy", TI),
    ("load", "Verbruik", "verbruik", "W", (0x028D,), "u16", 1, "power", M),
    ("load_l1", "Verbruik", "verbruik L1", "W", (0x028A,), "u16", 1, "power", M),
    ("load_l2", "Verbruik", "verbruik L2", "W", (0x028B,), "u16", 1, "power", M),
    ("load_l3", "Verbruik", "verbruik L3", "W", (0x028C,), "u16", 1, "power", M),
    ("vload_l1", "Verbruik", "spanning belasting L1", "V", (0x0284,), "u16", 0.1, "voltage", M),
    ("vload_l2", "Verbruik", "spanning belasting L2", "V", (0x0285,), "u16", 0.1, "voltage", M),
    ("vload_l3", "Verbruik", "spanning belasting L3", "V", (0x0286,), "u16", 0.1, "voltage", M),
    ("load_today", "Verbruik", "verbruik vandaag", "kWh", (0x020E,), "u16", 0.1, "energy", TI),
    ("load_tot", "Verbruik", "verbruik totaal", "kWh", (0x020F, 0x0210), "u32", 0.1, "energy", TI),
    ("inv", "Omvormer", "omvormer uitgang", "W", (0x027C,), "s16", 1, "power", M),
    ("inv_l1", "Omvormer", "omvormer L1", "W", (0x0279,), "s16", 1, "power", M),
    ("inv_l2", "Omvormer", "omvormer L2", "W", (0x027A,), "s16", 1, "power", M),
    ("inv_l3", "Omvormer", "omvormer L3", "W", (0x027B,), "s16", 1, "power", M),
    ("cur_l1", "Omvormer", "stroom L1", "A", (0x0276,), "s16", 0.01, "current", M),
    ("cur_l2", "Omvormer", "stroom L2", "A", (0x0277,), "s16", 0.01, "current", M),
    ("cur_l3", "Omvormer", "stroom L3", "A", (0x0278,), "s16", 0.01, "current", M),
    ("dc_t", "Omvormer", "temperatuur DC", "°C", (0x021C,), "temp", 0.1, "temperature", M),
    ("ac_t", "Omvormer", "temperatuur AC", "°C", (0x021D,), "temp", 0.1, "temperature", M),
    ("gen", "Generator", "generatorpoort", "W", (0x029B, 0x029F), "u32", 1, "power", M),
    ("gen_today", "Generator", "generator vandaag", "kWh", (0x0218,), "u16", 0.1, "energy", TI),
    ("gen_tot", "Generator", "generator totaal", "kWh", (0x0219, 0x021A), "u32", 0.1, "energy", TI),
)
GROUPS = ("Zon", "Accu", "Net", "Verbruik", "Omvormer", "Generator")
# wat we elke ronde lezen: (eerste register, aantal); alles tussen de uitersten staat er gewoon in
BLOCKS = ((0x0202, 28), (0x024A, 6), (0x0256, 41), (0x0284, 36))


def s16(v: int) -> int:
    return v - 65536 if v >= 32768 else v


def _value(regs: dict[int, int], addrs: tuple, kind: str, scale: float):
    raw = [regs.get(a) for a in addrs]
    if kind == "sum":
        got = [x for x in raw if x is not None]
        v = sum(got) if got else None
    elif any(x is None for x in raw):
        return None
    elif kind == "u32":
        v = raw[0] + (raw[1] << 16)
    elif kind in ("s16", "s16n"):
        v = s16(raw[0])
        v = -v if kind == "s16n" else v
    elif kind == "temp":
        v = raw[0] - 1000
    else:
        v = raw[0]
    if v is None:
        return None
    return v if scale == 1 else round(v * scale, 2)


def decode(regs: dict[int, int]) -> dict[str, float | int | None]:
    """Gelezen registers -> de waarden. Wat ontbreekt, is None (onbekend), nooit 0."""
    return {key: _value(regs, addrs, kind, scale) for key, _g, _n, _u, addrs, kind, scale, _c, _s in FIELDS}


def parse_discovery(data: bytes, addr=None) -> dict | None:
    """Het antwoord van een stick: "ip,mac,serienummer". Ruis, een ander formaat of een ongeldig serienummer geeft None."""
    try:
        ip, mac, serial = data.decode("ascii").strip().split(",")
    except (UnicodeDecodeError, ValueError):
        return None
    if not MAC_RE.match(mac) or not re.fullmatch(r"\d{6,10}", serial) or not 0 < int(serial) < 2 ** 32:
        return None
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip) or any(int(x) > 255 for x in ip.split(".")):
        return None
    if addr and addr[0] != ip:  # een stick meldt zijn eigen adres; iets anders is een vervalst of verdwaald antwoord
        return None
    return {"ip": ip, "mac": mac.lower(), "serial": int(serial)}


class _Finder(asyncio.DatagramProtocol):
    def __init__(self, on_found: Callable[[dict], None]) -> None:
        self.on_found = on_found

    def datagram_received(self, data: bytes, addr) -> None:
        f = parse_discovery(data, addr)
        if f:
            self.on_found(f)


async def finder(on_found: Callable[[dict], None]) -> asyncio.DatagramTransport:
    """Een eigen poort voor de zoekvraag: de antwoorden komen terug naar de poort die vroeg."""
    t, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: _Finder(on_found), local_addr=("0.0.0.0", 0), allow_broadcast=True)
    return t


def discover(transport: asyncio.DatagramTransport, target: str = "255.255.255.255", port: int = DISCOVERY_PORT) -> None:
    try:
        transport.sendto(DISCOVER, (target, port))
    except OSError as e:
        log.debug("zoekvraag niet verstuurd: %s", e)


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
