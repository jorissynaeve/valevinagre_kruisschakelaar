"""remotesensors: kWh-tellers van Tuya-toestellen bij een andere installatie als sensoren in HA (voor het energiedashboard)."""
from kkaddon.remotesensors import RemoteSensors

POINTS = [{"dp": "1", "name": "total_forward_energy", "value": 1234.5, "unit": "kWh", "rw": "ro"},
          {"dp": "19", "name": "power_total", "value": 0.4, "unit": "kW", "rw": "ro"},
          {"dp": "110", "name": "reverse_energy_total", "value": 0.0, "unit": "kWh", "rw": "ro"},
          {"dp": "9", "name": "verborgen", "value": 5, "unit": "kWh", "hide": True}]


class StubLink:
    def __init__(self):
        self.known = {"casa_vale_vinagre": {"name": "Casa Vale Vinagre"}}
        self.use = {"casa_vale_vinagre": {"y:tuya_2", "e:switch.x"}}
        self.up = True
        self.value = 1234.5

    def used(self, peer):
        return self.use.get(peer, set())

    def peer_name(self, peer):
        return self.known[peer]["name"]

    def is_up(self, peer):
        return self.up

    def object(self, peer, obj):
        if obj not in self.used(peer):
            return None
        if self.up:
            return {"kind": "tuya", "name": "Casa Valevinagre", "online": True, "points": [{**p, "value": self.value if p["dp"] == "1" else p["value"]} for p in POINTS]}
        return {"kind": "tuya", "name": "Casa Valevinagre", "online": False}  # alleen de catalogus


def test_only_visible_kwh_points_of_chosen_tuya_devices_become_energy_sensors(tmp_path):
    rs = RemoteSensors(tmp_path, StubLink())
    cfg = rs.discovery()
    assert sorted(cfg) == ["homeassistant/sensor/kk_remote_casa_vale_vinagre_tuya_2_1/config", "homeassistant/sensor/kk_remote_casa_vale_vinagre_tuya_2_110/config"]
    c = cfg["homeassistant/sensor/kk_remote_casa_vale_vinagre_tuya_2_1/config"]
    assert (c["device_class"], c["state_class"], c["unit_of_measurement"], c["name"]) == ("energy", "total_increasing", "kWh", "total_forward_energy")
    assert c["device"]["name"] == "Casa Valevinagre" and c["default_entity_id"] == "sensor.kk_remote_casa_vale_vinagre_tuya_2_1"


def test_states_follow_the_link_and_the_last_value_stays_when_it_drops(tmp_path):
    link = StubLink()
    rs = RemoteSensors(tmp_path, link)
    st = rs.states()
    assert st["kruisschakelaars/remote/casa_vale_vinagre/tuya_2/1/state"] == ("1234.5", True)
    assert st["kruisschakelaars/remote/casa_vale_vinagre/tuya_2/online"] == ("online", True)
    link.up = False
    off = rs.states()
    assert off == {"kruisschakelaars/remote/casa_vale_vinagre/tuya_2/online": ("offline", True)}  # geen nieuwe waarden; de oude blijft retained
    assert len(rs.discovery()) == 2  # de sensoren blijven bestaan


def test_sensors_survive_a_restart_while_the_link_is_down(tmp_path):
    RemoteSensors(tmp_path, StubLink()).discovery()
    link = StubLink()
    link.up = False
    assert len(RemoteSensors(tmp_path, link).discovery()) == 2


def test_unchosen_devices_have_no_sensors(tmp_path):
    link = StubLink()
    link.use = {"casa_vale_vinagre": {"e:switch.x"}}
    assert RemoteSensors(tmp_path, link).discovery() == {}
