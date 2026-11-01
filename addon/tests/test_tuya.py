"""Tuya lokaal: frames tegen vectoren uit tinytuya, de client tegen een nagebootst toestel, de hub en zijn entiteiten."""
import asyncio
import hashlib
import hmac
import json
import os
import stat

import pytest

pytest.importorskip("cryptography")

from kkaddon import schema, tuya  # noqa: E402
from kkaddon.model import ModelError  # noqa: E402
from kkaddon.tuyahub import TuyaHub, parse_model  # noqa: E402
from tests.rig import Rig  # noqa: E402

KEY = b"0123456789abcdef"
DEV = "bfABCDEFGHIJKLMNOPQRST"
JSONB = b'{"dps":{"1":true}}'

# gemaakt met tinytuya 1.20.0 (pack_message), seq 7, key KEY
V35 = "000066990000000000070000000d0000003d6162636465666768696a6b6cd6fc49b553a559a55930b43af63edf7228719c941ac14870b5def70c20222dcc61f68ce4e900c07f260fffc95f204b82b700009966"
V34 = "000055aa000000070000000d000000544490b05d74be9368c24a038cbaeded8e41fb8ef70a70a27b93a7e74ffe75426303000aeb4ff51d29188f82e4a162a1f6cde0c2e23f8f723b979e45c34961429c47307d016ae5a74a9dc2aacbb1896b550000aa55"
V33 = "000055aa000000070000000700000037332e33000000000000000000000000c27ee03f8be481e63320de7dc6eb429664215f365b0dad6b2ac8fab15b1d40bdea51d3550000aa55"


def test_frames_match_tinytuya():
    assert tuya.pack("3.5", KEY, 7, 13, JSONB, iv=b"abcdefghijkl").hex() == V35
    assert tuya.pack("3.4", KEY, 7, 13, JSONB).hex() == V34
    assert tuya.pack("3.3", KEY, 7, 7, JSONB).hex() == V33


@pytest.mark.parametrize("version,frame,cmd", [("3.5", V35, 13), ("3.4", V34, 13), ("3.3", V33, 7)])
def test_frames_are_read_back(version, frame, cmd):
    raw = bytes.fromhex(frame)
    assert tuya.frame_length(raw) == len(raw)
    assert tuya.unpack(version, KEY, raw) == (cmd, JSONB)


def test_session_key_matches_tinytuya():
    local, remote = b"abcdefghijklmnop", b"abcdefghijklmnop"
    assert tuya.session_key("3.5", KEY, local, remote).hex() == "e5d27cb553a559a55930b43af63edf09"
    assert tuya.session_key("3.4", KEY, local, remote).hex() == "0b9b15da4b44a0f5151dcfc4c01f35d5"


@pytest.mark.parametrize("version,frame", [("3.5", V35), ("3.4", V34)])
def test_a_wrong_key_is_noticed(version, frame):
    with pytest.raises(tuya.KeyError_):
        tuya.unpack(version, b"x" * 16, bytes.fromhex(frame))


def test_garbage_on_the_stream_stops_the_session():
    with pytest.raises(tuya.TuyaError):
        tuya.frame_length(b"\xde\xad\xbe\xef" + bytes(20))


@pytest.mark.parametrize("body,want", [
    (b'{"dps":{"1":true}}', {"1": True}),
    (b'{"protocol":4,"t":1,"data":{"dps":{"2":5}}}', {"2": 5}),
    (b"{}", None), (b"", None), (b"nonsense", None), (b"[1]", None),
])
def test_dps_of(body, want):
    assert tuya.dps_of(body) == want


class FakeDevice:
    """Een toestel aan de andere kant van de lijn, voor elke versie: sessie, statusvraag, opdrachten, meldingen."""

    def __init__(self, version, key=KEY, dps=None, silent_query=False):
        self.version, self.key, self.dps = version, key, dict(dps or {"1": False, "2": 160})
        self.silent_query = silent_query
        self.commands, self.heartbeats, self.writers = [], 0, []
        self.connections = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *a):
        for w in self.writers:
            w.close()
        self.server.close()

    def _report(self, w, skey, seq, cmd, dps):
        if self.version == "3.3":
            body = {"dps": dps}
        else:
            body = {"protocol": 4, "t": 1, "data": {"dps": dps}} if cmd == tuya.STATUS else {"dps": dps}
        w.write(tuya.pack(self.version, skey, seq, cmd, json.dumps(body).encode()))

    async def _serve(self, r, w):
        self.connections += 1
        self.writers.append(w)
        skey = self.key
        local = None
        buf = bytearray()
        seq = 100
        try:
            while True:
                chunk = await r.read(4096)
                if not chunk:
                    return
                buf += chunk
                while True:
                    n = tuya.frame_length(bytes(buf[:18]))
                    if n is None or len(buf) < n:
                        break
                    frame = bytes(buf[:n])
                    del buf[:n]
                    if (frame[:4] == tuya.P66) != (self.version == "3.5"):
                        return  # een ander frameformaat dan dit toestel kent: ophangen
                    cmd, body = tuya.unpack(self.version, skey, frame)
                    seq += 1
                    if cmd == tuya.SESS_START:
                        local, remote = body, os.urandom(16)
                        self.remote = remote
                        reply = remote + hmac.new(self.key, local, hashlib.sha256).digest()
                        w.write(tuya.pack(self.version, self.key, seq, tuya.SESS_RESP, reply))
                    elif cmd == tuya.SESS_FINISH:
                        assert body == hmac.new(self.key, self.remote, hashlib.sha256).digest()
                        skey = tuya.session_key(self.version, self.key, local, self.remote)
                    elif cmd in (tuya.DP_QUERY, tuya.DP_QUERY_NEW):
                        if not self.silent_query:
                            self._report(w, skey, seq, cmd, self.dps)
                    elif cmd == tuya.HEARTBEAT:
                        self.heartbeats += 1
                    elif cmd in (tuya.CONTROL, tuya.CONTROL_NEW):
                        j = json.loads(body)
                        dps = j.get("dps") or j["data"]["dps"]
                        self.commands.append(dps)
                        self.dps.update(dps)
                        self._report(w, skey, seq, tuya.STATUS, dps)
                await w.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        except tuya.TuyaError:  # zoals een echt toestel: niet te lezen, dan ophangen
            pass
        finally:
            w.close()


async def _until(cond, what, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(what)


def _client(dev, version="auto", key=KEY, **kw):
    return tuya.TuyaClient(DEV, key.decode(), "127.0.0.1", version, port=dev.port, retry=(0.01, 0.02), **kw)


@pytest.mark.parametrize("version", ["3.5", "3.4", "3.3"])
def test_client_reads_and_commands_every_version(version):
    async def go():
        async with FakeDevice(version) as dev:
            seen, links, found = [], [], []
            c = _client(dev, on_dps=seen.append, on_link=links.append, on_version=found.append)
            t = asyncio.create_task(c.run())
            await _until(lambda: seen, "stand ontvangen")
            assert seen[0] == {"1": False, "2": 160} and c.connected and links == [True] and found == [version] and c.version == version
            assert c.set_dps({"1": True})
            await _until(lambda: dev.commands == [{"1": True}], "opdracht aangekomen")
            await _until(lambda: {"1": True} in seen, "bevestiging ontvangen")
            t.cancel()
    asyncio.run(go())


def test_wrong_key_gives_a_clear_error_and_no_connection():
    async def go():
        async with FakeDevice("3.5") as dev:
            c = _client(dev, "3.5", key=b"wrongwrongwrong!")
            t = asyncio.create_task(c.run())
            await _until(lambda: c.last_error, "fout gemeld")
            assert "sleutel" in c.last_error and not c.connected and not c.set_dps({"1": True})
            t.cancel()
    asyncio.run(go())


def test_client_reconnects_after_the_device_drops_it():
    async def go():
        async with FakeDevice("3.5") as dev:
            seen = []
            c = _client(dev, "3.5", on_dps=seen.append)
            t = asyncio.create_task(c.run())
            await _until(lambda: seen, "eerste stand")
            dev.writers[0].close()
            await _until(lambda: dev.connections >= 2 and c.connected, "opnieuw verbonden")
            t.cancel()
    asyncio.run(go())


def test_no_answer_to_the_query_is_not_a_connection():
    async def go():
        async with FakeDevice("3.5", silent_query=True) as dev:
            old = tuya.ATTEMPT
            tuya.ATTEMPT = 0.05
            try:
                c = _client(dev, "3.5")
                t = asyncio.create_task(c.run())
                await _until(lambda: c.last_error, "fout gemeld")
                assert not c.connected
                t.cancel()
            finally:
                tuya.ATTEMPT = old
    asyncio.run(go())


# ------------------------------------------------------------ de hub

CHARGER = {"name": "Lader", "device_id": DEV, "host": "192.0.2.1", "key": KEY.decode(), "profile": "feyree", "version": "3.5"}
REPORT = {"101": "connect", "102": 2363, "25": 2084, "115": 16, "114": 16, "123": False, "124": "WaitOperation", "125": "FELDWV02", "132": False, "999": 5}


def hub_with(tmp_path, dev=CHARGER, dps=REPORT):
    async def make():
        r = Rig(tmp_path)
        hub = TuyaHub(tmp_path, r.engine)
        did = hub.add(dict(dev))
        hub.close()
        if dps:
            hub._on_dps(did, dps)
        return hub, did
    return asyncio.run(make())


def by_name(hub):
    return {c["name"]: (t.split("/")[1], c) for t, c in hub.discovery().items()}


def test_entities_follow_the_data_model(tmp_path):
    hub, did = hub_with(tmp_path)
    e = by_name(hub)
    assert e["Spanning L1"][0] == "sensor" and e["Spanning L1"][1]["device_class"] == "voltage" and e["Spanning L1"][1]["unit_of_measurement"] == "V"
    assert e["Energie laatste beurt"][1]["state_class"] == "total_increasing"
    assert e["Laadstroom"][0] == "number" and (e["Laadstroom"][1]["min"], e["Laadstroom"][1]["max"]) == (8, 32)
    assert e["Laden"][0] == "select" and "OpenCharging" in e["Laden"][1]["options"]
    assert e["RFID verplicht"][0] == "switch"
    assert e["Softwareversie" if "Softwareversie" in e else "Hardwareversie"][1]["entity_category"] == "diagnostic"
    assert "command_topic" not in e["Status"][1] and "command_topic" not in e["Spanning L1"][1]


def test_only_reported_and_not_hidden_points_become_entities(tmp_path):
    hub, did = hub_with(tmp_path)
    names = by_name(hub)
    assert "Aan" not in names  # dp 18 staat in het datamodel, maar het toestel meldt hem niet
    assert "Set16A" not in names and "log_content6" not in names  # verborgen
    assert "Datapunt 999" in names and "command_topic" not in names["Datapunt 999"][1]  # onbekend: alleen lezen


def test_values_are_scaled_for_ha(tmp_path):
    hub, did = hub_with(tmp_path)
    s = hub.states()
    assert s[hub.t_state(did, "102")][0] == "236.3" and s[hub.t_state(did, "25")][0] == "20.84"
    assert s[hub.t_state(did, "115")][0] == "16" and s[hub.t_state(did, "123")][0] == "OFF" and s[hub.t_state(did, "101")][0] == "connect"
    assert s[hub.t_online(did)][0] == "offline"


@pytest.mark.parametrize("dp,payload,want", [
    ("115", "16", 16), ("115", "16.0", 16), ("115", "7", None), ("115", "33", None), ("115", "abc", None),
    ("124", "OpenCharging", "OpenCharging"), ("124", "Nope", None), ("123", "ON", True), ("123", "off", False), ("123", "x", None),
    ("102", "230", None),  # alleen-lezen
    ("114", "10", None),  # beschrijfbaar maar verborgen
    ("11", "x", None), ("999", "1", None),
])
def test_commands_are_checked_against_the_model(tmp_path, dp, payload, want):
    hub, did = hub_with(tmp_path)
    assert hub.convert(hub._defs(did).get(dp), payload) == want


def test_command_goes_to_the_device_and_a_refused_one_does_not(tmp_path):
    hub, did = hub_with(tmp_path)

    class C:
        sent = []
        def set_dps(self, dps):
            self.sent.append(dps)
            return True

    hub.clients[did] = C()
    assert hub.command(f"kruisschakelaars/tuya/{did}/115/set", "20") and C.sent == [{"115": 20}]
    assert hub.command(f"kruisschakelaars/tuya/{did}/115/set", "99") and C.sent == [{"115": 20}]
    assert not hub.command("hasp/x/command", "1")


def test_scaled_number_is_converted_back():
    assert TuyaHub.convert({"access": "rw", "type": "value", "scale": 1, "min": 0, "max": 1000}, "23.5") == 235


def test_key_never_leaves_the_add_on(tmp_path):
    hub, did = hub_with(tmp_path)
    out = json.dumps([hub.status(), hub.discovery(), hub.states()])
    assert KEY.decode() not in out
    assert hub.status()[0]["device_id"] == DEV and hub.status()[0]["has_key"]
    assert "tuya" not in (tmp_path / "network.json").read_text(encoding="utf-8") if (tmp_path / "network.json").exists() else True


def test_devices_survive_a_restart_and_the_file_is_private(tmp_path):
    hub, did = hub_with(tmp_path)
    r = Rig(tmp_path)
    assert TuyaHub(tmp_path, r.engine).devices[did]["key"] == KEY.decode()
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "tuya.json").stat().st_mode) == 0o600


def test_found_version_is_remembered(tmp_path):
    hub, did = hub_with(tmp_path, {**CHARGER, "version": "auto"})
    hub._on_version(did, "3.5")
    assert TuyaHub(tmp_path, Rig(tmp_path).engine).devices[did]["version"] == "3.5"


@pytest.mark.parametrize("bad,word", [
    ({"key": "short"}, "16"), ({"host": "https://192.168.8.26/"}, "adres"), ({"device_id": "x"}, "device"), ({"name": ""}, "naam"),
    ({"version": "9"}, "versie"), ({"profile": "x"}, "profiel"),
])
def test_bad_input_is_refused(tmp_path, bad, word):
    async def go():
        hub = TuyaHub(tmp_path, Rig(tmp_path).engine)
        with pytest.raises(ModelError, match=word):
            hub.add({**CHARGER, **bad})
        assert not hub.devices
    asyncio.run(go())


def test_edit_without_key_keeps_it_and_duplicates_are_refused(tmp_path):
    async def go():
        hub = TuyaHub(tmp_path, Rig(tmp_path).engine)
        did = hub.add(dict(CHARGER))
        hub.update(did, {"name": "Lader 2", "key": ""})
        assert hub.devices[did]["key"] == KEY.decode() and hub.devices[did]["name"] == "Lader 2"
        with pytest.raises(ModelError):
            hub.add(dict(CHARGER))
        assert hub.remove(did) and not hub.remove(did) and did not in hub.values
        hub.close()
    asyncio.run(go())


def test_a_new_block_starts_empty_and_connects_when_complete(tmp_path):
    async def go():
        r = Rig(tmp_path)
        hub = TuyaHub(tmp_path, r.engine)
        did = hub.add({})
        assert hub.devices[did]["name"] == "Tuya" and did not in hub.clients and not hub.status()[0]["configured"]
        hub.update(did, {"device_id": DEV, "host": "192.0.2.1"})
        assert did not in hub.clients
        hub.update(did, {"key": KEY.decode()})
        assert did in hub.clients and hub.status()[0]["configured"]
        hub.close()
    asyncio.run(go())


def test_block_on_the_schema_shows_the_main_values_and_no_key(tmp_path):
    async def go():
        r = Rig(tmp_path)
        hub = TuyaHub(tmp_path, r.engine)
        did = hub.add(dict(CHARGER))
        hub.close()
        hub._on_dps(did, REPORT)
        page = schema.view(r.model, r.engine.snapshot())
        r.model.set_schema({**r.model.schema, "pages": [{"id": "p1", "name": "Garage"}]})
        schema.place(r.model, r.engine.snapshot(), f"y:{did}", "p1", 40, 40)
        view = schema.view(r.model, r.engine.snapshot())
        b = next(x for x in view["blocks"] if x["key"] == f"y:{did}")
        assert b["kind"] == "tuya" and b["page"] == "p1"
        assert [g["label"] for g in b["gangs"]] == ["Status: connect", "Laden: WaitOperation", "Laadstroom: 16 A"]
        assert not any(g["touch"] or g["relay"] for g in b["gangs"])
        assert KEY.decode() not in json.dumps(view) and KEY.decode() not in json.dumps(r.engine.snapshot())
        assert page["blocks"]
        assert hub.remove(did)
        assert f"y:{did}" not in r.model.schema["blocks"]
        assert not any(x["key"] == f"y:{did}" for x in schema.view(r.model, r.engine.snapshot())["blocks"])
    asyncio.run(go())


MODEL = {"services": [{"properties": [
    {"abilityId": 1, "code": "switch_1", "accessMode": "rw", "typeSpec": {"type": "bool"}},
    {"abilityId": 9, "code": "power", "accessMode": "ro", "typeSpec": {"type": "value", "unit": "kW·h", "scale": 1, "min": 0, "max": 100}},
    {"abilityId": 5, "code": "blob", "accessMode": "rw", "typeSpec": {"type": "raw"}},
]}]}


@pytest.mark.parametrize("text", [json.dumps(MODEL), json.dumps({"result": {"model": json.dumps(MODEL)}, "success": True}), json.dumps({"model": json.dumps(MODEL)})])
def test_tuya_data_model_is_understood(text):
    m = parse_model(text)
    assert m["1"]["access"] == "rw" and m["9"]["unit"] == "kWh" and m["9"]["scale"] == 1 and m["5"]["hide"]


@pytest.mark.parametrize("text", ["", "{}", "[1]", '{"services":[]}'])
def test_nonsense_is_not_a_data_model(text):
    with pytest.raises(ModelError):
        parse_model(text)


def test_a_pasted_model_replaces_the_profile(tmp_path):
    hub, did = hub_with(tmp_path, dps={"1": True})

    async def go():
        hub.update(did, {"model": json.dumps(MODEL)})
        hub.close()
    asyncio.run(go())
    assert hub.devices[did]["profile"] == "" and "switch_1" in by_name(hub)


def test_device_with_a_pasted_model_can_switch(tmp_path):
    hub, did = hub_with(tmp_path, {**CHARGER, "device_id": DEV[:-1] + "k", "profile": "", "model": json.dumps(MODEL)}, {"1": True, "9": 123})
    names = by_name(hub)
    assert names["switch_1"][0] == "switch" and "command_topic" in names["switch_1"][1] and names["power"][0] == "sensor"
    assert hub.states()[hub.t_state(did, "9")][0] == "12.3"
