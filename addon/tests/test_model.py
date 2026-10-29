import json

import pytest

from kkaddon import proto
from kkaddon.model import Model, ModelError


def test_light_and_triggers_are_validated_and_sorted(tmp_path):
    m = Model(tmp_path / "n.json")
    l = m.set_light(2, 1, name="Plafond", triggers=[
        {"node": 5, "btn": 2, "mode": "on"},
        {"node": 1, "btn": 0},
        {"node": 1, "btn": 0, "mode": "off"},  # dezelfde knop nog eens: de laatste telt
    ])
    assert [(t.node, t.btn, t.mode) for t in l.triggers] == [(1, 0, "off"), (5, 2, "on")]
    assert m.adopted(2)
    for bad in (
        [{"node": 0, "btn": 0}],
        [{"node": 1, "btn": 8}],
        [{"node": 1, "btn": 0, "mode": "dim"}],
        [{"node": 0xF000, "btn": 0}],
        [{"node": n, "btn": 0} for n in range(1, 14)],  # meer dan 12
        "nee",
    ):
        with pytest.raises(ModelError):
            m.set_light(2, 1, triggers=bad)
    with pytest.raises(ModelError):
        m.set_light(2, 16)
    with pytest.raises(ModelError):
        m.set_light(2, 0, name="x" * 61)


def test_save_load_export_import_roundtrip(tmp_path):
    p = tmp_path / "n.json"
    m = Model(p)
    m.set_node(2, name="Relaisbox", room="Living")
    m.set_light(2, 0, name="Plafond", triggers=[{"node": 1, "btn": 0, "mode": "toggle"}])
    m.set_follow(1, 2, (2, 0))
    m.set_follow(1, 3, None)
    exported = m.to_dict()
    m2 = Model(p)  # herlaadt van schijf
    assert m2.to_dict() == exported
    m3 = Model()
    m3.load_dict(json.loads(json.dumps(exported)))
    assert m3.to_dict() == exported
    # mislukte import laat het model ongemoeid
    before = m3.to_dict()
    with pytest.raises(ModelError):
        m3.load_dict({"version": 1, "lights": [{"node": 0, "relay": 0}]})
    with pytest.raises(ModelError):
        m3.load_dict({"version": 99})
    assert m3.to_dict() == before
    assert not list(tmp_path.glob(".network-*"))  # geen tijdelijke bestanden achtergelaten


def test_desired_config_and_auto_follow():
    m = Model()
    m.set_light(2, 0, name="A", triggers=[{"node": 1, "btn": 0}, {"node": 3, "btn": 1}])
    m.set_light(2, 1, name="B", triggers=[{"node": 1, "btn": 0}, {"node": 1, "btn": 1}])
    assert m.desired_relays(2, 3) == [[(1, 0, 0), (3, 1, 0)], [(1, 0, 0), (1, 1, 0)], []]
    # indicator i volgt het eerste licht dat knop i bedient
    assert m.desired_follows(1, 3) == [(2, 0), (2, 1), None]
    assert m.desired_follows(3, 2) == [None, (2, 0)]
    # handmatig wint, ook "geen"
    m.set_follow(1, 0, (2, 1))
    m.set_follow(1, 1, None)
    assert m.desired_follows(1, 3) == [(2, 1), None, None]
    m.set_follow(1, 1, "auto")
    assert m.desired_follows(1, 3)[1] == (2, 1)
    # hash is stabiel voor dezelfde inhoud
    h = proto.config_hash(m.desired_relays(2, 3), m.desired_follows(2, 0))
    assert h == proto.config_hash(m.desired_relays(2, 3), m.desired_follows(2, 0))


def test_listeners_and_rev():
    m = Model()
    seen = []
    m.on_change(lambda: seen.append(m.rev))
    m.set_node(1, name="x")
    m.set_light(1, 0, name="y")
    assert seen == [1, 2]
    assert m.remove_light(1, 0) and not m.remove_light(1, 0)


PLAN = {"floors": [
    {"id": "g", "name": "Begane grond", "rooms": [{"x": 20, "y": 20, "w": 300, "h": 200, "name": "Living"}], "items": {"n5": [120, 80], "l2.1": [200, 90]}},
    {"id": "v1", "name": "Verdieping", "rooms": [], "items": {"n6": [50, 50]}},
]}


def test_plan_is_saved_exported_and_never_touches_the_circuits(tmp_path):
    p = tmp_path / "n.json"
    m = Model(p)
    m.set_light(2, 0, name="Plafond", triggers=[{"node": 1, "btn": 0, "mode": "toggle"}])
    rev, listened = m.rev, []
    m.on_change(lambda: listened.append(1))
    assert "plan" not in m.to_dict(), "zonder plattegrond blijft het bestandsformaat zoals het was"
    m.set_plan(PLAN)
    assert m.rev == rev and not listened, "een plattegrond verandert niets aan de schakelingen: geen herconfiguratie"
    assert m.plan["floors"][0]["w"] == 1000 and m.plan["floors"][0]["h"] == 640, "standaardmaten"
    assert Model(p).plan == m.plan, "overleeft een herstart"
    m2 = Model()
    m2.load_dict(json.loads(json.dumps(m.to_dict())))
    assert m2.plan == m.plan and m2.to_dict() == m.to_dict()
    m.set_plan({"floors": []})
    assert "plan" not in m.to_dict()


@pytest.mark.parametrize("bad", [
    {"floors": "x"},
    {"floors": [{"id": "A b"}]},  # ongeldig id
    {"floors": [{"id": "a"}, {"id": "a"}]},  # dubbel id
    {"floors": [{"id": f"f{i}"} for i in range(9)]},  # te veel
    {"floors": [{"id": "a", "items": {"n5": [1, 1]}}, {"id": "b", "items": {"n5": [2, 2]}}]},  # een item op twee verdiepingen
    {"floors": [{"id": "a", "items": {"x9": [1, 1]}}]},  # onbekend soort item
    {"floors": [{"id": "a", "items": {"n5": [1]}}]},
    {"floors": [{"id": "a", "items": {"n5": [-1, 1]}}]},
    {"floors": [{"id": "a", "rooms": [{"x": 0, "y": 0, "w": 5, "h": 100}]}]},  # te klein
    {"floors": [{"id": "a", "rooms": [{"x": 0, "y": 0, "w": 100, "h": 100, "name": "x" * 99}]}]},
    {"floors": [{"id": "a", "rooms": [{"x": 0, "y": 0, "w": 100, "h": 100}] * 81}]},
])
def test_plan_rejects_nonsense_and_keeps_the_old_plan(bad):
    m = Model()
    m.set_plan(PLAN)
    before = m.plan
    with pytest.raises(ModelError):
        m.set_plan(bad)
    assert m.plan == before


def test_groups_are_saved_validated_and_new_ones_start_disabled(tmp_path):
    p = tmp_path / "n.json"
    m = Model(p)
    assert "groups" not in m.to_dict()
    m.set_groups([{"id": "keuken", "name": "Keuken", "entities": ["switch.a", "light.b"]}])
    assert m.groups["keuken"] == {"name": "Keuken", "enabled": False, "entities": ["switch.a", "light.b"], "lamp": []}, "een nieuwe kring staat uit"
    m.set_group("keuken", enabled=True)
    m.set_groups([{"id": "keuken", "name": "Keuken 2", "entities": ["switch.a", "light.b", "switch.c"]}])
    assert m.groups["keuken"]["enabled"] is True and m.groups["keuken"]["name"] == "Keuken 2", "opnieuw importeren houdt aan/uit"
    assert Model(p).groups == m.groups
    assert m.disable_all_groups() == 1 and not m.groups["keuken"]["enabled"] and m.disable_all_groups() == 0
    assert m.remove_group("keuken") and not m.remove_group("keuken")
    for bad in ([{"id": "A b", "entities": ["switch.a", "switch.b"]}], [{"id": "g", "entities": ["switch.a"]}],
                [{"id": "g", "entities": ["switch.a", "switch.a"]}], [{"id": "g", "entities": ["switch.a", "Nope"]}], []):
        with pytest.raises(ModelError):
            m.set_groups(bad)
    with pytest.raises(ModelError):
        m.set_group("bestaat-niet", enabled=True)


def test_import_of_a_file_with_conflicting_groups_is_refused_but_startup_is_not():
    d = {"version": 1, "groups": {"a": {"name": "A", "enabled": True, "entities": ["switch.x", "switch.y"]},
                                  "b": {"name": "B", "enabled": True, "entities": ["switch.y", "switch.z"]}}}
    m = Model()
    with pytest.raises(ModelError):
        m.load_dict(d)
    m.load_dict(d, save=False)  # zo laadt het model bij het opstarten: nooit weigeren
    assert set(m.groups) == {"a", "b"}
