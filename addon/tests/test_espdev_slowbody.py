"""Een ESP stuurt zijn antwoord in meerdere TCP-segmenten (blokkerende WebServer, zwak wifi). De add-on moet de hele body lezen.

Gevonden bij de eerste echte poolcontroller: `r.content.read(65536)` geeft terug wat er op dat moment binnen is. Op localhost komt een antwoord in een
keer binnen, dus de andere tests zagen dit niet; over wifi kwam er een halve descriptor aan en dat werd "geen geldige beschrijving".
"""
import asyncio
import json
import os
from pathlib import Path

import pytest
from aiohttp import web

from kkaddon.espdevhub import EspDevHub, clean_descriptor
from tests.rig import Rig

ROOT = Path(os.environ.get("KK_PROJECT") or Path(__file__).resolve().parents[2])
FIX = ROOT / "protocol" / "huisdev" / "tests" / "fixtures" / "descriptor.json"


def descriptor() -> bytes:
    if FIX.is_file():  # de echte JSON van huisdev.h, ~3 KB
        return FIX.read_bytes()
    pts = [{"id": f"p{i}", "label": f"Punt {i}", "kind": "float", "unit": "°C", "decimals": 1} for i in range(40)]
    return json.dumps({"proto": "1.0", "rev": "abc", "device": {"id": "x-1", "type": "t", "name": "N", "fw": "1", "mac": "AA", "lib": "1"}, "points": pts, "actions": []}).encode()


async def serve(body: bytes, piece: int, pause: float, content_length: bool = True):
    async def handler(request):
        resp = web.StreamResponse(headers={"Content-Type": "application/json", **({"Content-Length": str(len(body))} if content_length else {})})
        await resp.prepare(request)
        for i in range(0, len(body), piece):
            await resp.write(body[i:i + piece])
            await asyncio.sleep(pause)  # tussen twee segmenten
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/huisdev/descriptor", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


@pytest.mark.parametrize("piece,pause,content_length", [(700, 0.05, True), (200, 0.01, True), (1, 0.0, True), (500, 0.05, False)])
def test_a_body_in_several_segments_is_read_completely(tmp_path, piece, pause, content_length):
    async def go():
        body = descriptor()
        runner, port = await serve(body, piece, pause, content_length)
        hub = EspDevHub(tmp_path, Rig(tmp_path).engine, port=0, poll=0.05)
        try:
            status, data = await hub._call({"host": "127.0.0.1", "port": port, "token": ""}, "GET", "/huisdev/descriptor", token=False)
            assert status == 200
            want = json.loads(body)
            desc = clean_descriptor(data)
            assert desc is not None, "halve body gelezen: %d van %d bytes" % (len(json.dumps(data)), len(body))
            assert len(desc["points"]) == len(want["points"])
        finally:
            hub.close()
            await runner.cleanup()
    asyncio.run(go())


def test_a_huge_answer_is_refused_not_swallowed(tmp_path):
    async def go():
        runner, port = await serve(b"x" * 300_000, 60_000, 0.0)
        hub = EspDevHub(tmp_path, Rig(tmp_path).engine, port=0, poll=0.05)
        try:
            with pytest.raises(Exception) as e:
                await hub._call({"host": "127.0.0.1", "port": port, "token": ""}, "GET", "/huisdev/descriptor", token=False)
            assert "groot" in str(e.value)
        finally:
            hub.close()
            await runner.cleanup()
    asyncio.run(go())


# ------------------------------------------------------------ de melding zegt waarom een beschrijving niet bruikbaar is


async def serve_fixed(status: int, body: bytes):
    async def handler(request):
        return web.Response(status=status, body=body, content_type="application/json")

    app = web.Application()
    app.router.add_get("/huisdev/descriptor", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


def good() -> dict:
    return json.loads(descriptor())


CASES = [
    ("HTTP 500", 500, b"{}", "HTTP 500"),
    ("HTTP 404", 404, b"niet gevonden", "HTTP 404"),
    ("afgekapte JSON", 200, descriptor()[:300], "geen leesbare JSON"),
    ("platte tekst", 200, b"hallo", "geen leesbare JSON"),
    ("geen object", 200, b"[1,2]", "geen JSON-object"),
    ("proto weg", 200, json.dumps({**good(), "proto": None}).encode(), "proto ontbreekt"),
    ("rev te lang", 200, json.dumps({**good(), "rev": "x" * 17}).encode(), "rev ontbreekt of is ongeldig"),
    ("device weg", 200, json.dumps({k: v for k, v in good().items() if k != "device"}).encode(), "device ontbreekt"),
    ("id met hoofdletter", 200, json.dumps({**good(), "device": {**good()["device"], "id": "Pool-1"}}).encode(), "device.id is ongeldig"),
    ("id met laagstreepje", 200, json.dumps({**good(), "device": {**good()["device"], "id": "pool_1"}}).encode(), "device.id is ongeldig"),
]


@pytest.mark.parametrize("name,status,body,expect", CASES, ids=[c[0] for c in CASES])
def test_a_bad_description_says_what_is_wrong(tmp_path, name, status, body, expect):
    async def go():
        runner, port = await serve_fixed(status, body)
        hub = EspDevHub(tmp_path, Rig(tmp_path).engine, port=0, poll=0.05)
        try:
            with pytest.raises(Exception) as e:
                await hub._fetch_descriptor({"host": "127.0.0.1", "port": port, "token": ""})
            assert expect in str(e.value), str(e.value)
        finally:
            hub.close()
            await runner.cleanup()
    asyncio.run(go())


def test_a_good_description_passes(tmp_path):
    async def go():
        runner, port = await serve_fixed(200, descriptor())
        hub = EspDevHub(tmp_path, Rig(tmp_path).engine, port=0, poll=0.05)
        try:
            desc = await hub._fetch_descriptor({"host": "127.0.0.1", "port": port, "token": ""})
            assert len(desc["points"]) == len(good()["points"])
        finally:
            hub.close()
            await runner.cleanup()
    asyncio.run(go())
