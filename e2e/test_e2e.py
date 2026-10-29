"""End-to-end: de simulator (echte kern) praat via echte UDP met de echte add-on en een echte MQTT-broker.

Waarheid over de hardware komt uit de simulator (relaisstand, indicatoren, wat een node opgeslagen heeft),
niet uit wat de add-on denkt. De tests lopen in volgorde: elke test bouwt voort op de vorige.
"""
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

import paho.mqtt.client as mqtt
import pytest

ADDON = os.environ.get("ADDON_URL", "http://localhost:8099")
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
SIM = os.environ.get("SIM_CTL", "localhost:47475")
FAKEHA = os.environ.get("FAKEHA_URL", "http://localhost:8123")
P = "kruisschakelaars"


# ---------------------------------------------------------------- hulpjes


def api(method, path, body=None):
    req = urllib.request.Request(f"{ADDON}/api/{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raise AssertionError(f"{method} {path} -> {e.code} {e.read().decode()}") from None


def state():
    return api("GET", "state")


def ha(method, path, body=None):
    """De nagebootste Home Assistant (zie addon/tests/fakeha.py)."""
    req = urllib.request.Request(f"{FAKEHA}/{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or b"null")


def light(vid, relay):
    return next(l for l in state()["lights"] if l["node"] == vid and l["relay"] == relay)


def node(st, nid):
    return next(n for n in st["nodes"] if n["id"] == nid)


def sim(line, timeout=2.0):
    host, port = SIM.split(":")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    s.sendto(line.encode(), (host, int(port)))
    try:
        return s.recvfrom(2048)[0].decode()
    finally:
        s.close()


def sim_state(nid):
    return dict(kv.split("=", 1) for kv in sim(f"state {nid}").split())


def press(nid, btn):
    assert sim(f"press {nid} {btn}") == "ok"


def wait(pred, timeout=20.0, what="voorwaarde", every=0.1):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = pred()
        except (AssertionError, KeyError, StopIteration, urllib.error.URLError, socket.timeout) as e:
            last = e
        else:
            if last:
                return last
        time.sleep(every)
    raise AssertionError(f"time-out na {timeout:.0f} s: {what} (laatste: {last!r})")


class Tap:
    """MQTT-luisteraar die alles bewaart wat langskomt (ook retained berichten bij het abonneren)."""

    def __init__(self):
        self.msgs: dict[str, list[str]] = {}
        self.lock = threading.Lock()
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.c.on_message = self._on
        self.c.connect(MQTT_HOST, 1883)
        self.c.subscribe("#")
        self.c.loop_start()

    def _on(self, c, u, m):
        with self.lock:
            self.msgs.setdefault(m.topic, []).append(m.payload.decode())

    def last(self, topic):
        with self.lock:
            v = self.msgs.get(topic)
            return v[-1] if v else None

    def all(self, topic):
        with self.lock:
            return list(self.msgs.get(topic, []))

    def publish(self, topic, payload, retain=False):
        self.c.publish(topic, payload, retain=retain).wait_for_publish(5)


@pytest.fixture(scope="module")
def tap():
    t = Tap()
    yield t
    t.c.loop_stop()


@pytest.fixture(scope="module", autouse=True)
def house():
    """Wacht tot de add-on draait en alle zes nodes van de simulator heeft gehoord."""
    wait(lambda: len(state()["nodes"]) == 6 and all(n["online"] for n in state()["nodes"]), 60, "alle 6 nodes zichtbaar in de add-on")


# ---------------------------------------------------------------- de tests


def test_nodes_are_discovered_with_their_hardware():
    st = state()
    n1, n2, n4, n6 = (node(st, i) for i in (1, 2, 4, 6))
    assert (len(n1["relays"]), n1["nbtn"], n1["nind"]) == (3, 3, 3)
    assert len(n2["relays"]) == 4 and n2["nbtn"] == 0
    assert len(n6["relays"]) == 16  # grootste heartbeat
    assert n4["bridge"] and not n1["bridge"]
    assert n1["fw"] == "1.1"
    assert all(n["cfg_status"] == "ok" for n in st["nodes"])  # lege node, leeg model: de hashes komen overeen
    assert st["peers"], "de add-on heeft de bridge gehoord op het LAN"
    assert st["stats"]["rx"] >= 6 and st["stats"]["bad_auth"] == 0


def test_mqtt_exposes_buttons_and_diagnostics(tap):
    wait(lambda: tap.last("homeassistant/event/kk_1_b0/config"), 15, "event-entiteit voor knop 1 van node 1")
    cfg = json.loads(tap.last("homeassistant/event/kk_1_b2/config"))
    assert cfg["event_types"] == ["press"] and cfg["name"] == "Knop 3"
    assert tap.last(f"{P}/status") == "online"
    assert tap.last(f"{P}/node/2/status") == "online"
    assert tap.last("homeassistant/binary_sensor/kk_2_cfg/config")
    assert tap.last(f"{P}/node/2/cfg_problem") == "OFF"
    assert tap.last("homeassistant/event/kk_2_b0/config") is None  # relaisbox heeft geen knoppen


def test_learn_a_light_and_configure_the_nodes():
    api("PUT", "light/2/0", {"name": "Plafond living"})
    api("POST", "learn/start", {"node": 2, "relay": 0})
    # nodes zetten leer-modus aan (via flood: bridge, dan radio)
    wait(lambda: sim_state(2)["learning"] == "1" and sim_state(1)["learning"] == "1" and sim_state(3)["learning"] == "1", 10, "nodes in leer-modus")
    press(1, 0)
    time.sleep(0.4)
    press(3, 1)
    seen = wait(lambda: {(d["node"], d["btn"]) for d in state()["learn"]["detected"]} >= {(1, 0), (3, 1)}, 10, "beide knoppen gedetecteerd")
    assert seen
    assert sim_state(2)["relays"] == "0000", "tijdens het leren schakelt niets"
    # opslaan zoals de UI het doet: licht opslaan met triggers, dan leren stoppen
    api("PUT", "light/2/0", {"name": "Plafond living", "triggers": [{"node": 1, "btn": 0, "mode": "toggle"}, {"node": 3, "btn": 1, "mode": "toggle"}]})
    api("POST", "learn/stop", {})
    wait(lambda: sim_state(2)["trig"] == "2,0,0,0", 15, "node 2 heeft de twee triggers opgeslagen")
    wait(lambda: sim_state(1)["follow"].split(",")[0] == "2.0", 15, "touch 1: indicator 0 volgt het licht")
    wait(lambda: sim_state(3)["follow"].split(",")[1] == "2.0", 15, "touch 3: indicator 1 volgt het licht")
    wait(lambda: sim_state(2)["learning"] == "0" and sim_state(1)["learning"] == "0", 10, "leer-modus uit")
    wait(lambda: node(state(), 2)["cfg_status"] == "ok" and node(state(), 1)["cfg_status"] == "ok" and node(state(), 3)["cfg_status"] == "ok", 30, "heartbeat-hash bevestigt de configuratie")
    assert state()["learn"] is None


def test_button_press_switches_light_and_everything_reports(tap):
    n_before = len(tap.all(f"{P}/node/1/button/0"))
    press(1, 0)
    wait(lambda: sim_state(2)["relays"] == "1000", 3, "relais van de eigenaar staat aan (hardware)")
    wait(lambda: sim_state(1)["ind"] == "100" and sim_state(3)["ind"] == "010", 3, "indicatoren op beide touches volgen het licht")
    wait(lambda: state()["lights"][0]["on"] is True, 3, "add-on kent de nieuwe staat")
    wait(lambda: tap.last(f"{P}/light/2/0/state") == "ON", 3, "MQTT-toestand ON")
    wait(lambda: len(tap.all(f"{P}/node/1/button/0")) == n_before + 1, 3, "precies een MQTT-knopevent")
    assert json.loads(tap.last(f"{P}/node/1/button/0")) == {"event_type": "press"}
    ev = [e for e in state()["log"] if e["t"] == "light" and e["node"] == 2 and e["on"]][-1]
    assert ev["cause"]["src"] == 1 and ev["cause"]["btn"] == 0  # de add-on weet welke touch schakelde
    time.sleep(0.3)
    press(3, 1)  # andere schakelaar in dezelfde kring
    wait(lambda: sim_state(2)["relays"] == "0000" and sim_state(1)["ind"] == "000", 3, "tweede schakelaar zet het licht uit")
    wait(lambda: tap.last(f"{P}/light/2/0/state") == "OFF", 3, "MQTT-toestand OFF")
    ev = [e for e in state()["log"] if e["t"] == "light" and e["node"] == 2 and not e["on"]][-1]
    assert ev["cause"]["src"] == 3 and ev["cause"]["btn"] == 1


def test_mqtt_command_is_the_virtual_button(tap):
    tap.publish(f"{P}/light/2/0/set", "ON")
    wait(lambda: sim_state(2)["relays"] == "1000", 3, "MQTT ON schakelt de hardware")
    wait(lambda: sim_state(1)["ind"] == "100", 3, "indicator volgt")
    wait(lambda: tap.last(f"{P}/light/2/0/state") == "ON", 3, "toestand teruggemeld")
    ev = [e for e in state()["log"] if e["t"] == "light" and e["node"] == 2 and e["on"]][-1]
    assert ev["cause"]["btn"] == 255  # oorzaak: commando (virtuele knop)
    wait(lambda: any(e["t"] == "cmd" and e["result"] == "ok" for e in state()["log"]), 3, "opdracht bevestigd")
    tap.publish(f"{P}/light/2/0/set", "OFF")
    wait(lambda: sim_state(2)["relays"] == "0000", 3, "MQTT OFF")
    api("POST", "light/2/0/cmd", {"op": "toggle"})  # vanuit de web-UI
    wait(lambda: sim_state(2)["relays"] == "1000", 3, "toggle vanuit de UI")
    api("POST", "light/2/0/cmd", {"op": "off"})
    wait(lambda: sim_state(2)["relays"] == "0000", 3, "uit vanuit de UI")


def test_discovery_follows_renames(tap):
    cfg = json.loads(wait(lambda: tap.last("homeassistant/light/kk_2_0/config"), 5, "discovery van het licht"))
    assert cfg["name"] == "Plafond living" and cfg["command_topic"] == f"{P}/light/2/0/set"
    api("PUT", "light/2/0", {"name": "Plafond"})
    wait(lambda: json.loads(tap.last("homeassistant/light/kk_2_0/config"))["name"] == "Plafond", 5, "nieuwe naam in discovery")
    api("PUT", "node/2", {"name": "Relaisbox living", "room": "Living"})
    wait(lambda: json.loads(tap.last("homeassistant/light/kk_2_0/config"))["device"].get("suggested_area") == "Living", 5, "kamer in discovery")
    api("PUT", "light/2/1", {"name": "Tijdelijk"})
    wait(lambda: tap.last("homeassistant/light/kk_2_1/config"), 5, "tweede licht verschijnt")
    api("DELETE", "light/2/1")
    wait(lambda: tap.last("homeassistant/light/kk_2_1/config") == "", 5, "verwijderd licht verdwijnt uit HA (lege retained payload)")


def test_power_cycle_restores_light_and_network_keeps_working():
    api("POST", "light/2/0/cmd", {"op": "on"})
    wait(lambda: sim_state(2)["relays"] == "1000", 3, "licht aan")
    sim("power 2 off")
    assert sim_state(2)["relays"] == "0000"  # zonder stroom valt het relais weg
    sim("power 2 on")
    assert sim_state(2)["relays"] == "1000", "laatste stand direct terug uit flash"
    time.sleep(1.5)
    press(3, 1)  # herstarte node (nieuwe boot) moet gewoon weer meedoen
    wait(lambda: sim_state(2)["relays"] == "0000", 3, "schakelaar werkt na herstart van de eigenaar")
    wait(lambda: state()["lights"][0]["on"] is False, 5, "add-on volgt de herstarte node")


def test_lost_configuration_is_repaired_from_the_heartbeat_hash():
    sim("wipe 2")
    assert sim_state(2)["trig"] == "0,0,0,0"
    wait(lambda: sim_state(2)["trig"] == "2,0,0,0", 40, "add-on ziet de afwijkende hash en stuurt de configuratie opnieuw")
    wait(lambda: node(state(), 2)["cfg_status"] == "ok", 30, "status weer in orde")
    assert any(e["t"] == "cfg" and e["node"] == 2 and e["status"] == "drift" for e in state()["log"])
    press(1, 0)
    wait(lambda: sim_state(2)["relays"] == "1000", 3, "hersteld licht werkt weer")
    time.sleep(0.4)  # dezelfde knop binnen 150 ms telt niet (debounce)
    press(1, 0)
    wait(lambda: sim_state(2)["relays"] == "0000", 3, "en gaat weer uit")


def test_every_press_counts_exactly_once_on_a_lossy_radio():
    try:
        time.sleep(0.5)  # de vorige test drukte net op dezelfde knop (debounce 150 ms)
        sim("loss 20")
        start = sim_state(2)["relays"]
        for _ in range(8):  # even aantal: het licht moet eindigen zoals het begon
            press(1, 0)
            time.sleep(0.6)
        time.sleep(1.0)
        assert sim_state(2)["relays"] == start, "een druk telde dubbel of ging verloren"
        wait(lambda: (state()["lights"][0]["on"] is True) == (start == "1000"), 10, "add-on eindigt in dezelfde stand als de hardware")
    finally:
        sim("loss 0")


# ---------------------------------------------------------------- extern apparaat: Tuya via local-tuya in HA


def test_external_tuya_device_takes_part_in_the_cross_switch_network(tap):
    wait(lambda: ha("GET", "ctl/state")["auth_ok"] >= 1 and state()["ha"]["connected"], 20, "de add-on is verbonden met HA")
    vid = api("POST", "virtual", {"name": "Tuya gang", "room": "Gang", "gangs": [
        {"entity": "switch.t_0", "name": "Gang plafond", "lamp": True},
        {"entity": "switch.t_1", "name": "Gang spots", "lamp": True},
        {"entity": "switch.t_2", "name": "Knop zonder lamp", "lamp": False}]})["id"]
    assert 0xE000 <= vid <= 0xEFFF
    wait(lambda: node(state(), vid)["online"] and node(state(), vid)["relays"][0]["on"] is False, 10, "extern apparaat online met zijn stand uit HA")
    assert not [e for e in state()["log"] if e["t"] == "btn" and e["node"] == vid], "de beginstand is geen druk"

    # touch 5 (slaapkamer): knop 1 bedient het Tuya-plafond, knop 2 de Tuya-spots
    api("PUT", f"light/{vid}/0", {"name": "Gang plafond", "triggers": [{"node": 5, "btn": 0, "mode": "toggle"}]})
    api("PUT", f"light/{vid}/1", {"name": "Gang spots", "triggers": [{"node": 5, "btn": 1, "mode": "toggle"}]})
    wait(lambda: sim_state(5)["follow"].split(",")[:2] == [f"{vid}.0", f"{vid}.1"], 15, "touch 5: indicatoren volgen de externe lichten")
    wait(lambda: node(state(), 5)["cfg_status"] == "ok", 30, "hash van touch 5 klopt")
    assert not [e for e in state()["log"] if e["t"] == "cfg" and e["node"] == vid], "een extern apparaat krijgt nooit configuratie"

    # 1. een schakelaar op de mesh bedient het Tuya-licht (absoluut bevel via HA) en de LED volgt
    press(5, 0)
    wait(lambda: ha("GET", "ctl/state")["states"]["switch.t_0"] == "on", 5, "Tuya-relais aan via HA")
    assert ("switch.t_0", "turn_on") in [tuple(c) for c in ha("GET", "ctl/state")["calls"]]
    wait(lambda: sim_state(5)["ind"][0] == "1", 5, "LED van touch 5 volgt het externe licht")
    wait(lambda: light(vid, 0)["on"] is True, 5, "add-on kent de stand")
    ev = [e for e in state()["log"] if e["t"] == "light" and e["node"] == vid and e["relay"] == 0 and e["on"]][-1]
    assert ev["cause"]["src"] == 5 and ev["cause"]["btn"] == 0, "oorzaak: touch 5, knop 1"
    time.sleep(0.5)
    press(5, 0)
    wait(lambda: ha("GET", "ctl/state")["states"]["switch.t_0"] == "off" and sim_state(5)["ind"][0] == "0", 5, "tweede druk zet het uit, LED uit")
    calls = [tuple(c) for c in ha("GET", "ctl/state")["calls"] if c[0] == "switch.t_0"]
    assert calls[:2] == [("switch.t_0", "turn_on"), ("switch.t_0", "turn_off")], f"precies een bevel per druk, geen herhalingen: {calls}"

    # 2. iemand drukt op de touch van de Tuya zelf: de LED van touch 5 en het logboek weten het
    ha("POST", "ctl/press", {"entity": "switch.t_1"})
    wait(lambda: sim_state(5)["ind"][1] == "1", 5, "fysieke druk op de Tuya: LED van touch 5 volgt")
    wait(lambda: [e for e in state()["log"] if e["t"] == "btn" and e["node"] == vid and e["btn"] == 1], 5, "de druk staat in het logboek")
    ev = [e for e in state()["log"] if e["t"] == "light" and e["node"] == vid and e["relay"] == 1][-1]
    assert ev["cause"] == {"src": vid, "btn": 1, "evt": 1}
    ha("POST", "ctl/press", {"entity": "switch.t_1"})
    wait(lambda: sim_state(5)["ind"][1] == "0", 5, "uit gedrukt: LED uit")

    # 3. een gang zonder lamp is een knop: zijn druk bedient een echt licht op een ESP-node
    api("PUT", "light/2/2", {"name": "Spots living", "triggers": [{"node": vid, "btn": 2, "mode": "toggle"}]})
    wait(lambda: sim_state(2)["trig"].split(",")[2] == "1", 15, "node 2 luistert naar de Tuya-knop")
    before = sim_state(2)["relays"][2]
    ha("POST", "ctl/press", {"entity": "switch.t_2"})
    wait(lambda: sim_state(2)["relays"][2] != before, 5, "druk op de Tuya schakelt het relais van een ESP-node")
    time.sleep(0.4)
    ha("POST", "ctl/press", {"entity": "switch.t_2"})
    wait(lambda: sim_state(2)["relays"][2] == before, 5, "tweede druk schakelt terug (precies een keer per druk)")

    # 4. bedienen vanuit HA/UI is geen druk: er gaat geen knopevent de mesh op
    n_btn = len([e for e in state()["log"] if e["t"] == "btn" and e["node"] == vid])
    relays2 = sim_state(2)["relays"]
    api("POST", f"light/{vid}/0/cmd", {"op": "on"})
    wait(lambda: ha("GET", "ctl/state")["states"]["switch.t_0"] == "on" and sim_state(5)["ind"][0] == "1", 5, "UI-bevel werkt")
    time.sleep(1.0)
    assert len([e for e in state()["log"] if e["t"] == "btn" and e["node"] == vid]) == n_btn and sim_state(2)["relays"] == relays2
    api("POST", f"light/{vid}/0/cmd", {"op": "off"})
    wait(lambda: ha("GET", "ctl/state")["states"]["switch.t_0"] == "off", 5, "UI-bevel uit")

    # 5. het apparaat staat in HA al (local-tuya): wij maken er geen dubbele entiteiten van
    assert tap.last(f"homeassistant/event/kk_{vid}_b0/config") is None
    assert tap.last(f"homeassistant/light/kk_{vid}_0/config") in (None, "")  # standaard verborgen
    api("PUT", f"light/{vid}/0", {"enabled": True})
    wait(lambda: tap.last(f"homeassistant/light/kk_{vid}_0/config"), 5, "zichtbaar gemaakt: nu wel een light in HA")


def test_import_of_ha_devices_gives_a_working_external_device():
    wait(lambda: state()["ha"]["connected"], 20, "de add-on is verbonden met HA")
    r = api("GET", "ha/devices")
    assert r["registry"] and any(d["platform"] == "esphome" and d["name"] == "Relaisbox" for d in r["devices"])
    box = next(d for d in r["devices"] if d["name"] == "Relaisbox")
    assert not box["imported"]
    vid = api("POST", "virtual", {"name": box["name"], "room": box["area"], "gangs": [{"entity": e["entity_id"], "name": e["name"], "lamp": True} for e in box["entities"]]})["id"]
    wait(lambda: node(state(), vid)["online"], 10, "geimporteerd apparaat is online")
    assert next(d for d in api("GET", "ha/devices")["devices"] if d["name"] == "Relaisbox")["imported"]
    api("DELETE", f"virtual/{vid}")


def test_external_device_failure_and_recovery():
    vid = next(n["id"] for n in state()["nodes"] if n.get("virtual"))
    ha("POST", "ctl/ignore", {"on": True})
    try:
        api("POST", f"light/{vid}/0/cmd", {"op": "on"})
        wait(lambda: [e for e in state()["log"] if e["t"] == "cmd" and e.get("result") == "failed" and e["node"] == vid], 15, "geen reactie van het apparaat: opgegeven met een melding")
        assert light(vid, 0)["on"] is False, "geen stand verzinnen zonder bevestiging"
    finally:
        ha("POST", "ctl/ignore", {"on": False})
    n = ha("GET", "ctl/state")["auth_ok"]
    ha("POST", "ctl/kick", {})
    wait(lambda: ha("GET", "ctl/state")["auth_ok"] > n and node(state(), vid)["online"], 20, "de add-on verbindt opnieuw met HA")
    ha("POST", "ctl/set", {"entity": "switch.t_0", "state": "on"})
    wait(lambda: light(vid, 0)["on"] is True, 5, "stand volgt weer")
    ha("POST", "ctl/set", {"entity": "switch.t_0", "state": "off"})
