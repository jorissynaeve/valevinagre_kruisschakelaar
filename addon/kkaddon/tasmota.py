"""Het script `huisconfig.be` op een NSPanel zetten via de webserver van Tasmota (het bestandsbeheer onder Consoles), zodat
je het niet met de hand hoeft te uploaden: script uploaden, de regel `load("huisconfig.be")` achteraan `autoexec.be`, herstart."""
from __future__ import annotations

import aiohttp

SCRIPT = "huisconfig.be"
LOAD = f'load("{SCRIPT}")'


class TasmotaError(Exception):
    pass


async def _upload(s: aiohttp.ClientSession, base: str, name: str, data: str) -> None:
    form = aiohttp.FormData()
    form.add_field("ufsu", data.encode("utf-8"), filename=name, content_type="application/octet-stream")
    async with s.post(f"{base}/ufsu", data=form, allow_redirects=False) as r:  # Tasmota antwoordt 303 naar /ufsu
        if r.status == 401:
            raise TasmotaError("de panel vraagt een wachtwoord (Tasmota-webwachtwoord): vul het in")
        if r.status >= 400:
            raise TasmotaError(f"uploaden van {name} mislukt (HTTP {r.status}); staat het bestandsbeheer aan in deze Tasmota?")


async def _read(s: aiohttp.ClientSession, base: str, name: str) -> str | None:
    try:
        async with s.get(f"{base}/ufsd", params={"download": f"/{name}"}) as r:
            if r.status == 401:
                raise TasmotaError("de panel vraagt een wachtwoord (Tasmota-webwachtwoord): vul het in")
            return (await r.text()) if r.status == 200 else None
    except aiohttp.ServerDisconnectedError:
        return None  # een bestand dat er niet is beantwoordt Tasmota 13 met een lege reactie, niet met een 404


async def _put(s: aiohttp.ClientSession, base: str, name: str, data: str) -> None:
    """Uploaden, en teruglezen: Tasmota antwoordt 200 ook als er niets is opgeslagen."""
    await _upload(s, base, name, data)
    back = await _read(s, base, name)
    if back is None or back.strip() != data.strip():
        raise TasmotaError(f"{name} is geüpload, maar staat niet op de panel (teruggelezen: {'niets' if back is None else f'{len(back)} tekens, verwacht {len(data)}'}). "
                           "Staat het bestandsbeheer (Consoles → Manage File system) in deze Tasmota? Upload het bestand dan met de hand.")


async def install(host: str, script: str, password: str = "", restart: bool = True, timeout: float = 15) -> list[str]:
    """Zet `script` als huisconfig.be op de panel. Geeft op wat er gedaan is. TasmotaError met een uitleg als het niet lukt."""
    base = f"http://{host}"
    auth = aiohttp.BasicAuth("admin", password) if password else None
    done: list[str] = []
    try:
        async with aiohttp.ClientSession(auth=auth, timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            await _put(s, base, SCRIPT, script)
            done.append(f"{SCRIPT} geüpload")
            auto = await _read(s, base, "autoexec.be") or ""
            if LOAD not in auto:
                await _put(s, base, "autoexec.be", auto.rstrip("\r\n") + ("\n" if auto.strip() else "") + LOAD + "\n")
                done.append("regel toegevoegd aan autoexec.be")
            else:
                done.append("autoexec.be had de regel al")
            if restart:
                try:
                    async with s.get(f"{base}/cm", params={"cmnd": "Restart 1"}):
                        pass
                except (aiohttp.ClientError, TimeoutError):
                    pass  # de panel start al opnieuw op en verbreekt de verbinding
                done.append("panel herstart")
    except (aiohttp.ClientError, TimeoutError, OSError) as e:
        raise TasmotaError(f"de panel op {host} is niet bereikbaar ({e.__class__.__name__}): klopt het IP-adres?") from e
    return done
