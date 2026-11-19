"""Toegangslogboek: een bool is een periode, een enum een moment, onbekend geeft niets, en een herstart houdt een lopende periode vast."""
import pytest

from kkaddon.accesshub import AccessHub
from kkaddon.engine import Engine
from kkaddon.model import Model
from rig import KEY

LOCK_OPTS = [{"v": 1, "l": "Vergrendeld"}, {"v": 3, "l": "Ontgrendeld"}]


def _pt(pid, label, kind, **kw):
    return {"id": pid, "label": label, "kind": kind, "diag": False, "options": kw.pop("options", []), **kw}


class Esp:
    """Wat AccessHub van EspDevHub nodig heeft: `devices` en `point_value`."""

    def __init__(self):
        self.devices = {
            "cam": {"name": "Oprit", "driver": "reolink", "points": [_pt("motion", "Beweging", "bool"), _pt("person", "Persoon", "bool"), _pt("led", "Batterij", "int"), _pt("dbg", "Debug", "bool", diag=True)]},
            "slot": {"name": "Voordeur", "driver": "nuki", "points": [_pt("state", "Stand", "enum", options=LOCK_OPTS)]},
        }
        self.values: dict[tuple[str, str], object] = {}

    def point_value(self, did, pid):
        return self.values.get((did, pid))


class Clock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def __call__(self):
        return self.t


def make(tmp_path):
    esp, clock = Esp(), Clock()
    hub = AccessHub(tmp_path, Engine(KEY, Model(), lambda raw: None), clock=clock)
    hub.espdev = esp
    return hub, esp, clock


def test_candidates_are_bool_and_enum_points_without_diagnostics(tmp_path):
    hub, _, _ = make(tmp_path)
    assert [c["key"] for c in hub.candidates()] == ["cam:motion", "cam:person", "slot:state"]
    with pytest.raises(ValueError):
        hub.set_sources("cam:motion")
    hub.set_sources(["cam:motion", "cam:led", "nope:x", "cam:motion"])  # onbekende en niet-bool bronnen vallen weg, dubbele ook
    assert hub.sources == ["cam:motion"]


def test_nothing_is_logged_until_a_source_is_on(tmp_path):
    hub, esp, _ = make(tmp_path)
    esp.values[("cam", "motion")] = True
    hub.tick()
    assert hub.log == []


def test_a_bool_is_a_period_with_start_and_end(tmp_path):
    hub, esp, clock = make(tmp_path)
    hub.set_sources(["cam:motion"])
    esp.values[("cam", "motion")] = False
    hub.tick()
    assert hub.log == []
    clock.t += 5
    esp.values[("cam", "motion")] = True
    hub.tick()
    assert [(e["label"], e["end"]) for e in hub.log] == [("Beweging", None)]
    clock.t += 12
    hub.tick()  # nog steeds aan: geen tweede regel
    assert len(hub.log) == 1
    esp.values[("cam", "motion")] = False
    hub.tick()
    e = hub.log[0]
    assert e["end"] - e["start"] == 12 and e["name"] == "Oprit"


def test_unknown_gives_no_event_and_no_end(tmp_path):
    hub, esp, clock = make(tmp_path)
    hub.set_sources(["cam:motion"])
    esp.values[("cam", "motion")] = True
    hub.tick()
    del esp.values[("cam", "motion")]  # camera offline
    clock.t += 30
    hub.tick()
    assert hub.log[0]["end"] is None


def test_an_enum_is_one_line_per_change_and_the_first_value_is_not_a_change(tmp_path):
    hub, esp, clock = make(tmp_path)
    hub.set_sources(["slot:state"])
    esp.values[("slot", "state")] = 1
    hub.tick()
    assert hub.log == []
    clock.t += 60
    esp.values[("slot", "state")] = 3
    hub.tick()
    e, = hub.log
    assert e["text"] == "Vergrendeld → Ontgrendeld" and e["start"] == e["end"]


def test_a_period_survives_a_restart_and_ends_when_the_point_is_off(tmp_path):
    hub, esp, clock = make(tmp_path)
    hub.set_sources(["cam:motion"])
    esp.values[("cam", "motion")] = True
    hub.tick()
    again, esp2, clock2 = make(tmp_path)
    clock2.t = clock.t + 20
    esp2.values[("cam", "motion")] = True
    again.tick()
    assert len(again.log) == 1 and again.log[0]["end"] is None  # dezelfde periode loopt door
    clock2.t += 7
    esp2.values[("cam", "motion")] = False
    again.tick()
    assert again.log[0]["end"] - again.log[0]["start"] == 27


def test_turning_a_source_off_ends_its_period_and_clear_keeps_running_ones(tmp_path):
    hub, esp, clock = make(tmp_path)
    hub.set_sources(["cam:motion", "cam:person"])
    esp.values[("cam", "motion")] = True
    esp.values[("cam", "person")] = True
    hub.tick()
    clock.t += 3
    hub.set_sources(["cam:person"])
    assert [e["end"] is not None for e in hub.log] == [True, False]
    hub.clear()
    assert [e["label"] for e in hub.log] == ["Persoon"]


def test_csv_has_a_header_and_one_row_per_line(tmp_path):
    hub, esp, clock = make(tmp_path)
    hub.set_sources(["cam:motion"])
    esp.values[("cam", "motion")] = True
    hub.tick()
    clock.t += 4
    esp.values[("cam", "motion")] = False
    hub.tick()
    lines = hub.csv().strip().splitlines()
    assert lines[0].startswith("apparaat;punt;start") and lines[1].split(";")[0] == "Oprit" and lines[1].split(";")[4] == "4"
