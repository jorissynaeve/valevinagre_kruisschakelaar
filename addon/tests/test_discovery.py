import json

from kkaddon import discovery as d
from kkaddon import proto
from rig import Rig


def house():
    r = Rig()
    r.model.set_node(2, name="Relaisbox", room="Living")
    r.model.set_light(2, 0, name="Plafond", triggers=[{"node": 1, "btn": 0}])
    r.model.set_light(2, 2, name="Verborgen", enabled=False)
    r.heartbeat(1, relays=[(False, 0)], nbtn=3, nind=3, cfg_hash=0, flags=0)
    r.heartbeat(2, relays=[(True, 2), (False, 0), (False, 0)], nbtn=0, nind=0, cfg_hash=0, flags=1)
    return r


def test_lights_events_and_diagnostics():
    r = house()
    cfg = d.configs(r.engine)
    light = cfg["homeassistant/light/kk_2_0/config"]
    assert light["name"] == "Plafond" and light["unique_id"] == "kk_2_0"
    assert light["command_topic"] == "kruisschakelaars/light/2/0/set"
    assert light["state_topic"] == "kruisschakelaars/light/2/0/state"
    assert light["device"]["name"] == "Relaisbox" and light["device"]["suggested_area"] == "Living"
    assert light["device"]["model"] == "ESP-NOW bridge" and light["device"]["sw_version"] == "1.2"
    assert light["availability_mode"] == "all"
    assert {a["topic"] for a in light["availability"]} == {"kruisschakelaars/status", "kruisschakelaars/node/2/status"}
    assert "homeassistant/light/kk_2_2/config" not in cfg  # verborgen licht
    assert "homeassistant/light/kk_2_1/config" not in cfg  # geen licht in het model
    # knoppen van node 1 (3 stuks) zijn event-entiteiten, node 2 heeft er geen
    ev = [t for t in cfg if "/event/" in t]
    assert sorted(ev) == [f"homeassistant/event/kk_1_b{b}/config" for b in range(3)]
    e = cfg[ev[0]]
    assert e["event_types"] == ["press"] and e["state_topic"] == "kruisschakelaars/node/1/button/0"
    assert cfg["homeassistant/sensor/kk_1_fw/config"]["enabled_by_default"] is False
    assert cfg["homeassistant/binary_sensor/kk_2_cfg/config"]["device_class"] == "problem"
    assert cfg["homeassistant/light/kk_2_0/config"]["device"]["name"] == "Relaisbox"
    assert cfg["homeassistant/event/kk_1_b0/config"]["device"]["name"] == "Node 1"  # zonder naam in het model
    json.dumps(cfg)  # alles serialiseerbaar


def test_states():
    r = house()
    s = d.states(r.engine)
    assert s["kruisschakelaars/light/2/0/state"] == ("ON", True)
    assert s["kruisschakelaars/light/2/1/state"] == ("OFF", True)
    assert s["kruisschakelaars/node/2/status"] == ("online", True)
    assert s["kruisschakelaars/node/2/cfg_problem"] == ("OFF", True)
    assert s["kruisschakelaars/node/2/fw"] == ("1.2", True)
    r.engine.nodes[2].cfg_status = "drift"
    assert d.states(r.engine)["kruisschakelaars/node/2/cfg_problem"] == ("ON", True)
    r.engine.nodes[2].online = False
    assert d.states(r.engine)["kruisschakelaars/node/2/status"] == ("offline", True)


def test_model_light_for_unseen_node_is_offline_and_has_no_state():
    r = Rig()
    r.model.set_light(9, 1, name="Poolhuis")
    cfg = d.configs(r.engine)
    assert "homeassistant/light/kk_9_1/config" in cfg
    s = d.states(r.engine)
    assert s["kruisschakelaars/node/9/status"] == ("offline", True)
    assert "kruisschakelaars/light/9/1/state" not in s  # onbekend, dus niet verzinnen


def test_command_topic_regex_and_filter():
    from kkaddon.mqttbridge import SET_RE

    assert SET_RE.match("kruisschakelaars/light/12/3/set").groups() == ("12", "3")
    assert not SET_RE.match("kruisschakelaars/light/12/3/state")
    assert not SET_RE.match("kruisschakelaars/light/x/3/set")
    assert d.LIGHT_SET_FILTER == "kruisschakelaars/light/+/+/set"


def test_external_device_exposes_only_enabled_lights():
    r = Rig()
    vid = r.model.set_virtual(None, "Tuya", "Gang", [{"entity": f"switch.t_{i}", "name": f"Gang {i + 1}", "lamp": i < 2} for i in range(4)])
    r.model.lights[(vid, 0)].enabled = True
    r.engine.virt.ha_sync({f"switch.t_{i}": "on" if i == 0 else "off" for i in range(4)})
    cfg = d.configs(r.engine)
    mine = {t: c for t, c in cfg.items() if f"kk_{vid}" in t}
    assert list(mine) == [f"homeassistant/light/kk_{vid}_0/config"]  # geen knoppen, firmware of diagnostiek: dat staat al in HA
    assert mine[f"homeassistant/light/kk_{vid}_0/config"]["device"]["model"] == "Extern apparaat (via HA)"
    s = d.states(r.engine)
    assert s[f"kruisschakelaars/light/{vid}/0/state"] == ("ON", True) and s[f"kruisschakelaars/node/{vid}/status"] == ("online", True)
    assert f"kruisschakelaars/node/{vid}/fw" not in s and f"kruisschakelaars/node/{vid}/cfg_problem" not in s
