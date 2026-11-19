"""De taal tussen Huisconfig en een Loxone Miniserver: berichten en de importbestanden voor Loxone Config. Puur, zonder netwerk (loxonehub.py doet het verkeer).

Een bericht is één signaal: `kk.<id>=<getal>`. De voorvoegsel `kk.` en het `=` direct achter het id zorgen dat `kk.tuin=\\v` nooit `kk.tuinhuis=3` herkent.
Alleen getallen: Loxone leest geen tekst of JSON uit een UDP-bericht. Een aan/uit is 1 of 0, een keuze (scene, ventilatiestand) is het nummer van de keuze.

Naar Loxone (een virtuele UDP-ingang met één commando per signaal) en van Loxone (een virtuele uitgang, HTTP of UDP, met één commando per signaal) maakt
`viu_xml` en `vo_xml` het sjabloon dat je in Config importeert, in het formaat van een export uit Loxone Config (templateType 1 en 3). Het formaat is
overgenomen uit echte exports van de gebruiker; wat daar niet in stond (bv. de grenzen van een ingang) is zo gekozen dat het ruim genoeg is.
"""
from __future__ import annotations

import re
from xml.sax.saxutils import quoteattr

PREFIX = "kk."
ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
RESERVED = {"sync"}  # `kk.sync=1` van Loxone: stuur alles opnieuw
LINE_RE = re.compile(r"^kk\.([a-z0-9_]{1,40})=(-?\d{1,12}(?:\.\d{1,6})?)$")
MAX_BYTES = 512  # een bericht is hoogstens zo groot; meer is geen Loxone
MIN_VERSION = "17010727"  # minVersion in de exports van de gebruiker


def key(sid: str) -> str:
    return PREFIX + sid


def fmt(value, decimals: int = 0) -> str:
    """Een waarde als op de lijn: bool als 1/0, een geheel getal zonder komma, een kommagetal met zijn decimalen."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if decimals <= 0:
        return str(int(round(value)))
    return f"{value:.{decimals}f}"


def message(sid: str, value, decimals: int = 0) -> bytes:
    return f"{key(sid)}={fmt(value, decimals)}".encode("ascii")


TEXT_RE = re.compile(r"^kk\.([a-z0-9_]{1,40})=([^\x00-\x1f]{1,60})$")
MAX_TEXT = 60  # een tekst (bv. de wekker) is hoogstens zo lang


def parse_values(data: bytes | str) -> list[tuple[str, float | str]]:
    """Als `parse`, maar wat geen getal is, komt als tekst mee (`kk.wekker=Morgen, 6:45`): voor een signaal dat tekst verwacht, zoals een wekkertijd.
    Wie een getal of een tekst mag krijgen beslist de hub per signaal; hier is elke regel `kk.<naam>=<waarde>` goed."""
    if isinstance(data, bytes):
        if len(data) > MAX_BYTES:
            return []
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError:
            data = data.decode("latin-1")  # een Miniserver stuurt geen UTF-8 met garantie; "Zaterdag" en tijden zijn ASCII, een accent lukt zo ook
    elif len(data) > MAX_BYTES:
        return []
    out: list[tuple[str, float | str]] = []
    for line in re.split(r"[\r\n;]+", data):
        line = line.strip()
        m = LINE_RE.match(line)
        if m:
            out.append((m.group(1), float(m.group(2))))
            continue
        m = TEXT_RE.match(line)
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


# ------------------------------------------------------------ een wekkertekst lezen

_DAYS = {"vandaag": 0, "today": 0, "heute": 0, "morgen": 1, "tomorrow": 1, "overmorgen": 2,
         "ma": 0, "maandag": 0, "mo": 0, "mon": 0, "monday": 0, "montag": 0,
         "di": 1, "dinsdag": 1, "tu": 1, "tue": 1, "tuesday": 1, "dienstag": 1,
         "wo": 2, "woensdag": 2, "we": 2, "wed": 2, "wednesday": 2, "mi": 2, "mittwoch": 2,
         "do": 3, "donderdag": 3, "th": 3, "thu": 3, "thursday": 3, "donnerstag": 3,
         "vr": 4, "vrijdag": 4, "fr": 4, "fri": 4, "friday": 4, "freitag": 4,
         "za": 5, "zaterdag": 5, "sa": 5, "sat": 5, "saturday": 5, "samstag": 5,
         "zo": 6, "zondag": 6, "su": 6, "sun": 6, "sunday": 6, "so": 6, "sonntag": 6}
_RELATIVE = {"vandaag", "today", "heute", "morgen", "tomorrow", "overmorgen"}
_TIME_RE = re.compile(r"(?<![\d:.])(\d{1,2})\s*[:.hHuU]\s*([0-5]\d)(?::[0-5]\d)?(?![\d])(?:\s*([apAP])\.?[mM]\.?)?")
_ISO_RE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_DMY_RE = re.compile(r"(?<![\d:.])(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2}|\d{4}))?(?![\d:])")


def parse_alarm(text: str, today) -> dict | None:
    """De wekkertekst van Loxone: `{"minutes": <minuten sinds middernacht>, "date": <datum of None>}`, of None als er geen tijd in staat ("Geen wekker", "Uit", "-").
    Herkend: een tijd (`6:45`, `06:45`, `6.45`, `6u45`, `6:45 uur`, `6:45 AM`) met erbij een dag (`Vandaag`, `Morgen`, `Overmorgen`, een weekdag in het Nederlands, Engels
    of Duits, of een datum `27-11`, `27/11/2026`, `2026-11-27`) of niets (dan elke dag: `date` is None). Een weekdag of datum is de eerstvolgende op of na `today`."""
    from datetime import date, timedelta
    if not isinstance(text, str):
        return None
    s = text.strip()
    for t in _TIME_RE.finditer(s):  # de eerste echte tijd: "27.11 6:45" heeft een datum voor de tijd
        h, mi, ap = int(t.group(1)), int(t.group(2)), t.group(3)
        if ap and 1 <= h <= 12:
            h = h % 12 + (12 if ap.lower() == "p" else 0)
        elif ap or h > 23:
            continue
        break
    else:
        return None
    rest =(s[:t.start()] + " " + s[t.end():]).strip()
    day = None
    iso = _ISO_RE.search(rest)
    dmy = None if iso else _DMY_RE.search(rest)
    try:
        if iso:
            day = date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        elif dmy:
            d, mo, y = int(dmy.group(1)), int(dmy.group(2)), dmy.group(3)
            if y:
                day = date(int(y) + (2000 if len(y) == 2 else 0), mo, d)
            else:
                day = date(today.year, mo, d)
                if day < today:
                    day = date(today.year + 1, mo, d)
        else:
            for w in re.findall(r"[a-zäöüéë]+", rest.lower()):
                if w in _DAYS:
                    n = _DAYS[w]
                    day = today + timedelta(days=n) if w in _RELATIVE else today + timedelta(days=(n - today.weekday()) % 7)
                    break
    except ValueError:
        return None
    return {"minutes": h * 60 + mi, "date": day}


def parse(data: bytes | str) -> list[tuple[str, float]]:
    """Alle `kk.<id>=<getal>` in een bericht: één per regel (of gescheiden door `;`). Wat niet past, wordt overgeslagen. Een onbruikbaar bericht geeft []."""
    if isinstance(data, bytes):
        if len(data) > MAX_BYTES:
            return []
        try:
            data = data.decode("ascii")
        except UnicodeDecodeError:
            return []
    elif len(data) > MAX_BYTES:
        return []
    out = []
    for line in re.split(r"[\r\n;]+", data):
        m = LINE_RE.match(line.strip())
        if m:
            out.append((m.group(1), float(m.group(2))))
    return out


# ------------------------------------------------------------ importbestanden voor Loxone Config

def _attr(v) -> str:
    return quoteattr(str(v))


def _tag(name: str, attrs: list[tuple[str, object]], close: bool = True) -> str:
    return f"<{name} " + " ".join(f"{k}={_attr(v)}" for k, v in attrs) + ("/>" if close else ">")


def _bounds(sig: dict) -> tuple[float, float]:
    lo, hi = sig.get("min"), sig.get("max")
    return (lo if lo is not None else -10_000_000, hi if hi is not None else 10_000_000)


def viu_xml(title: str, sender: str, port: int, signals: list[dict]) -> str:
    """De virtuele UDP-ingang voor wat Huisconfig naar Loxone stuurt. `sender` is het IP van de add-on (Loxone luistert alleen naar dat adres)."""
    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             _tag("VirtualInUdp", [("HintText", ""), ("Title", title), ("Comment", ""), ("Address", sender), ("Port", port)], close=False),
             "\t" + _tag("Info", [("templateType", 1), ("minVersion", MIN_VERSION)])]
    for s in signals:
        lo, hi = _bounds(s)
        dec = max(0, int(s.get("decimals") or 0))
        lines.append("\t" + _tag("VirtualInUdpCmd", [
            ("Title", key(s["id"])), ("Comment", s.get("label") or ""), ("Address", ""), ("Check", f"{key(s['id'])}=\\v"),
            ("Signed", "true" if lo < 0 else "false"), ("Analog", "true"), ("SourceValLow", 0), ("DestValLow", 0), ("SourceValHigh", 100), ("DestValHigh", 100),
            ("DefVal", 0), ("MinVal", _num(lo)), ("MaxVal", _num(hi)), ("Unit", f"<v.{dec}>" if dec else "<v>"), ("HintText", "")]))
    lines.append("</VirtualInUdp>")
    return "\n".join(lines) + "\n"


def _num(v) -> str:
    return str(int(v)) if float(v) == int(v) else str(v)


def vo_xml(title: str, host: str, port: int, transport: str, signals: list[dict]) -> str:
    """De virtuele uitgang voor wat Loxone naar Huisconfig stuurt, over UDP (`/dev/udp/…`) of HTTP (een GET naar `/loxone?kk.<id>=<waarde>`).
    Een aan/uit krijgt een commando bij AAN en een bij UIT (1 en 0); al het andere is analoog en stuurt de waarde van de uitgang mee."""
    http = transport == "http"
    address = f"http://{host}:{port}" if http else f"/dev/udp/{host}/{port}"
    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             _tag("VirtualOut", [("HintText", ""), ("Title", title), ("Comment", ""), ("Address", address), ("CmdInit", ""), ("CloseAfterSend", "true"), ("CmdSep", ";")], close=False),
             "\t" + _tag("Info", [("templateType", 3), ("minVersion", MIN_VERSION)])]
    for s in signals:
        k = key(s["id"])

        def cmd(v: str) -> str:
            return f"loxone?{k}={v}" if http else f"{k}={v}"

        analog = s.get("kind") != "bool"
        attrs: list[tuple[str, object]] = [("Title", k), ("Comment", s.get("label") or ""), ("CmdOnMethod", "GET"), ("CmdOffMethod", "GET"),
                                           ("CmdOn", cmd("<v>" if analog else "1")), ("CmdOnHTTP", ""), ("CmdOnPost", ""),
                                           ("CmdOff", "" if analog else cmd("0")), ("CmdOffHTTP", ""), ("CmdOffPost", ""), ("CmdAnswer", ""),
                                           ("Analog", "true" if analog else "false"), ("Repeat", 0), ("RepeatRate", 0)]
        if analog:
            attrs += [("SourceValLow", 0), ("DestValLow", 0), ("SourceValHigh", 100), ("DestValHigh", 100)]
        attrs.append(("HintText", ""))
        lines.append("\t" + _tag("VirtualOutCmd", attrs))
    lines.append("</VirtualOut>")
    return "\n".join(lines) + "\n"


def table(sender: str, in_port: int, host: str, out_port: int, transport: str, signals: list[dict]) -> str:
    """Dezelfde commando's als platte tekst, om te controleren of over te typen als een import niet lukt."""
    out = [s for s in signals if s["dir"] == "out"]
    inn = [s for s in signals if s["dir"] == "in"]
    rows = [f"Virtuele UDP-ingang: poort {in_port}, afzender {sender}", "  commandoherkenning                              soort"]
    for s in out:
        check = key(s["id"]) + "=\\v"
        rows.append(f"  {check:<46}  {'aan/uit' if s['kind'] == 'bool' else 'analoog'}   {s.get('label') or ''}")
    rows += ["", f"Virtuele uitgang: {'http://' + host + ':' + str(out_port) if transport == 'http' else '/dev/udp/' + host + '/' + str(out_port)}",
             "  commando bij AAN / analoog                      commando bij UIT"]
    for s in inn:
        k = key(s["id"])
        if transport == "http":
            on, off = (f"loxone?{k}=1", f"loxone?{k}=0") if s["kind"] == "bool" else (f"loxone?{k}=<v>", "")
        else:
            on, off = (f"{k}=1", f"{k}=0") if s["kind"] == "bool" else (f"{k}=<v>", "")
        rows.append(f"  {on:<46}  {off}   {s.get('label') or ''}")
    return "\n".join(rows) + "\n"
