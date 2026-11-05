import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from kkaddon import tasmota
from kkaddon.model import Model, ModelError


class FakePanel:
    def __init__(self, autoexec=None, password=None):
        self.files = {} if autoexec is None else {"/autoexec.be": autoexec}
        self.password, self.restarts = password, 0

    def app(self):
        app = web.Application()
        app.router.add_post("/ufsu", self.up)
        app.router.add_get("/ufsd", self.down)
        app.router.add_get("/cm", self.cm)
        return app

    def _denied(self, r):
        return self.password is not None and r.headers.get("Authorization") is None

    async def up(self, r):
        if self._denied(r):
            return web.Response(status=401)
        f = await (await r.multipart()).next()
        self.files["/" + f.filename] = (await f.read()).decode()
        return web.Response(text="ok")

    async def down(self, r):
        path = r.query["download"]
        return web.Response(text=self.files[path]) if path in self.files else web.Response(status=404)

    async def cm(self, r):
        self.restarts += 1
        return web.json_response({"Restart": "Restarting"})


async def run(panel, **kw):
    async with TestServer(panel.app()) as srv:
        return await tasmota.install(f"{srv.host}:{srv.port}", "SCRIPT", **kw)


def test_the_script_is_uploaded_the_load_line_added_once_and_the_panel_restarted():
    p = FakePanel(autoexec="import x\nload('driver.be')")
    done = asyncio.run(run(p))
    assert p.files["/huisconfig.be"] == "SCRIPT" and p.files["/autoexec.be"] == "import x\nload('driver.be')\nload(\"huisconfig.be\")\n" and p.restarts == 1
    assert "regel toegevoegd aan autoexec.be" in done
    done = asyncio.run(run(p))
    assert p.files["/autoexec.be"].count("huisconfig.be") == 1 and "autoexec.be had de regel al" in done, "een tweede keer voegt niets toe"


def test_a_panel_without_autoexec_gets_one():
    p = FakePanel()
    asyncio.run(run(p))
    assert p.files["/autoexec.be"] == 'load("huisconfig.be")\n'


def test_a_password_protected_panel_says_so_and_works_with_the_password():
    with pytest.raises(tasmota.TasmotaError, match="wachtwoord"):
        asyncio.run(run(FakePanel(password="x")))
    p = FakePanel(password="x")
    asyncio.run(run(p, password="x"))
    assert "/huisconfig.be" in p.files


def test_an_unreachable_panel_gives_a_readable_error():
    with pytest.raises(tasmota.TasmotaError, match="niet bereikbaar"):
        asyncio.run(tasmota.install("127.0.0.1:9", "S", timeout=2))


def test_the_panel_remembers_the_address_of_home_assistant():
    m = Model()
    pid = m.add_panel("Keuken", "192.168.0.5")
    m.set_panel(pid, hub="192.168.0.10")
    assert m.panels[pid]["hub"] == "192.168.0.10"
    with pytest.raises(ModelError, match="Home Assistant"):
        m.set_panel(pid, hub="http://x/")


def test_an_upload_that_is_not_stored_is_reported():
    class Lying(FakePanel):
        async def up(self, r):
            return web.Response(text="ok")  # 200, maar niets bewaard
    with pytest.raises(tasmota.TasmotaError, match="staat niet op de panel"):
        asyncio.run(run(Lying()))
