"""Descriptor, rev en state tegen HUISDEV.md §5 en §6."""
import json
import re
import unittest

from huisdevtest import Base, HEADER_VERSION, PROTO, fnv1a, rev_body

ESC_LABEL = 'Zeg "hi" \\ terug\nregel\ttab\x01 °C 😀'
POINT_KEYS = {"id", "label", "kind", "unit", "decimals", "min", "max", "step", "writable", "danger", "confirm",
              "diag", "group", "class", "state", "stale", "options"}
ARG_KEYS = {"id", "label", "kind", "unit", "decimals", "min", "max", "step", "options"}


def raw_v(state_text, pid):
    m = re.search(r'"%s":\{"v":([^,}]*)' % re.escape(pid), state_text)
    assert m, pid
    return m.group(1)


class TestDescriptor(Base):
    def test_top_level(self):
        txt, j = self.d.descriptor()
        self.assertEqual(set(j), {"proto", "rev", "device", "points", "actions"})
        self.assertEqual(j["proto"], PROTO)
        self.assertRegex(j["rev"], r"^[0-9a-f]{1,16}$")

    def test_device_object(self):
        _, j = self.d.descriptor()
        self.assertEqual(j["device"], {"id": "poolcontroller-a1b2c3", "type": "poolcontroller", "name": "Zwembad",
                                       "fw": "0.3.1", "mac": "AA:BB:CC:A1:B2:C3", "lib": HEADER_VERSION})

    def test_points_follow_spec(self):
        _, j = self.d.descriptor()
        ids = set()
        for p in j["points"]:
            self.assertRegex(p["id"], r"^[a-z][a-z0-9_]{0,23}$")
            self.assertNotIn(p["id"], ids)
            ids.add(p["id"])
            self.assertIn(p["kind"], {"bool", "int", "float", "enum", "text"})
            self.assertIsInstance(p["label"], str)
            self.assertLessEqual(len(p["label"]), 40)
            self.assertLessEqual(set(p), POINT_KEYS)
            if "decimals" in p:
                self.assertEqual(p["kind"], "float")
            if "options" in p:
                self.assertEqual(p["kind"], "enum")
            if p["kind"] == "enum":
                self.assertTrue(p["options"])
            for k in ("writable", "danger", "diag"):
                if k in p:
                    self.assertIs(p[k], True)  # nooit false: het veld ontbreekt dan
            if "danger" in p:
                self.assertIsInstance(p["confirm"], str)
            if "unit" in p:
                self.assertTrue(p["unit"])
        self.assertEqual(len(j["points"]), 16)

    def test_specific_points(self):
        _, j = self.d.descriptor()
        P = {p["id"]: p for p in j["points"]}
        self.assertEqual(P["ph"], {"id": "ph", "label": "pH", "kind": "float", "decimals": 2, "class": "ph",
                                   "state": "measurement", "stale": 120000})
        self.assertEqual(P["temp"]["unit"], "°C")
        self.assertEqual(P["temp"]["decimals"], 1)
        self.assertEqual(P["orp"]["kind"], "int")
        self.assertNotIn("decimals", P["orp"])
        self.assertEqual(P["mode_electro"]["options"], [{"v": 0, "l": "Uit"}, {"v": 1, "l": "Handmatig"}, {"v": 2, "l": "Auto"}])
        self.assertIs(P["mode_electro"]["writable"], True)
        ps = P["ph_set"]
        self.assertEqual((ps["min"], ps["max"], ps["step"], ps["group"]), (6.8, 7.6, 0.1, "instellingen"))
        pr = P["prod_set"]
        self.assertEqual((pr["min"], pr["max"], pr["step"]), (10, 990, 10))
        self.assertIsInstance(pr["min"], int)  # "10", niet "10.000"
        self.assertEqual(P["alarm_no_flow"]["group"], "alarmen")
        self.assertIs(P["rssi"]["diag"], True)
        self.assertEqual(P["calibrate"]["confirm"], 'Kalibratie "overschrijft" instellingen. Doorgaan?')
        self.assertNotIn("writable", P["ph"])

    def test_actions(self):
        _, j = self.d.descriptor()
        A = {a["id"]: a for a in j["actions"]}
        self.assertEqual(list(A), ["boost", "reset", "calibrate_ph", "flush"])
        self.assertEqual(A["boost"]["args"], [{"id": "minutes", "label": "Duur", "kind": "int", "unit": "min",
                                               "min": 5, "max": 240, "step": 5}])
        self.assertIs(A["reset"]["danger"], True)
        self.assertEqual(A["reset"]["confirm"], "Alle instellingen gaan verloren. Doorgaan?")
        self.assertNotIn("args", A["reset"])
        cal = A["calibrate_ph"]["args"]
        self.assertEqual(cal[0]["options"], [{"v": 7, "l": "pH 7"}, {"v": 10, "l": "pH 10"}])
        self.assertEqual(cal[1]["kind"], "text")
        for a in j["actions"]:
            for arg in a.get("args", []):
                self.assertLessEqual(set(arg), ARG_KEYS)
        # of een actie nu kan staat in de state, nooit in de descriptor
        self.assertNotIn("disabled", A["flush"])

    def test_open_without_token_even_when_claimed(self):
        self.d.claim()
        st, _ = self.d.get("/huisdev/descriptor")
        self.assertEqual(st, 200)

    def test_escaping_roundtrip(self):
        txt, j = self.d.descriptor()
        P = {p["id"]: p for p in j["points"]}
        self.assertEqual(P["esc_test"]["label"], ESC_LABEL)
        self.assertEqual(P["esc_test"]["unit"], "°C")
        self.assertTrue(all(ord(c) >= 0x20 for c in txt), "ongeescapete besturingstekens in JSON")
        txt.encode("utf-8")

    def test_name_and_fw_escaping(self):
        self.d.cmd("rebegin", 'poolcontroller', 'Naam "met" \\ tekens\n°', 'fw"1', "80")
        _, j = self.d.descriptor()
        self.assertEqual(j["device"]["name"], 'Naam "met" \\ tekens\n°')
        self.assertEqual(j["device"]["fw"], 'fw"1')

    def test_empty_device_is_valid(self):
        self.d.cmd("reboot_empty")
        _, j = self.d.descriptor()
        self.assertEqual(j["points"], [])
        self.assertEqual(j["actions"], [])
        _, s = self.d.state()
        self.assertEqual(s["points"], {})
        self.assertEqual(s["actions"], {})

    def test_size_limits_with_64_points(self):
        self.d.cmd("reboot_empty")
        for i in range(64):
            self.d.cmd("addpoint", "p%d" % i, "Punt nummer %d" % i, "float")
            self.d.cmd("unit", "p%d" % i, "kWh")
            self.d.cmd("setnum", "p%d" % i, "1234.5")
        txt, j = self.d.descriptor()
        self.assertEqual(len(j["points"]), 64)
        self.assertLess(len(txt.encode()), 8192)
        stxt, _ = self.d.state()
        self.assertLess(len(stxt.encode()), 4096)


class TestRev(Base):
    def test_matches_fnv_over_points_and_actions(self):
        txt, j = self.d.descriptor()
        self.assertEqual(j["rev"], fnv1a(rev_body(txt).encode("utf-8")))

    def test_stable_across_calls_and_reboot(self):
        r = self.d.rev()
        self.assertEqual(self.d.rev(), r)
        self.d.cmd("setnum", "ph", "6.9")
        self.d.cmd("actionstate", "flush", "0", "")
        self.assertEqual(self.d.rev(), r)
        self.d.cmd("reboot")
        self.assertEqual(self.d.rev(), r)

    def test_name_fw_type_do_not_change_rev(self):
        r = self.d.rev()
        self.d.cmd("rebegin", "poolcontroller", "Ander naam", "9.9.9", "80")
        txt, j = self.d.descriptor()
        self.assertEqual(j["rev"], r)
        self.assertEqual(j["device"]["name"], "Ander naam")
        self.assertEqual(j["device"]["fw"], "9.9.9")

    def test_changes_when_points_change(self):
        r0 = self.d.rev()
        self.d.cmd("addpoint", "nieuw", "Nieuw", "int")
        r1 = self.d.rev()
        self.assertNotEqual(r1, r0)
        self.assertEqual(self.d.cmd("remove", "nieuw"), ["1"])
        self.assertEqual(self.d.rev(), r0, "terugdraaien geeft dezelfde rev")
        self.d.cmd("unit", "salt", "ppm")
        self.assertNotEqual(self.d.rev(), r0)
        self.d.cmd("unit", "salt", "g/L")
        self.assertEqual(self.d.rev(), r0)
        self.d.cmd("range", "ph_set", "6.8", "7.6", "0.05")
        self.assertNotEqual(self.d.rev(), r0)
        self.d.cmd("range", "ph_set", "6.8", "7.6", "0.1")
        self.assertEqual(self.d.rev(), r0)
        self.assertEqual(self.d.cmd("remove", "orp"), ["1"])
        self.assertNotEqual(self.d.rev(), r0)
        self.assertEqual(self.d.cmd("remove", "bestaat_niet"), ["0"])

    def test_changes_when_actions_change(self):
        r0 = self.d.rev()
        self.d.cmd("addaction", "extra", "Extra")
        r1 = self.d.rev()
        self.assertNotEqual(r1, r0)
        self.d.cmd("addarg", "extra", "n", "N", "int")
        r2 = self.d.rev()
        self.assertNotEqual(r2, r1)
        self.assertEqual(self.d.cmd("removeaction", "extra"), ["1"])
        self.assertEqual(self.d.rev(), r0)

    def test_rev_is_in_state_and_matches(self):
        _, s = self.d.state()
        self.assertEqual(s["rev"], self.d.rev())
        self.d.cmd("addpoint", "nieuw", "Nieuw", "int")
        _, s = self.d.state()
        self.assertEqual(s["rev"], self.d.rev())


class TestState(Base):
    def test_shape(self):
        _, s = self.d.state()
        self.assertEqual(set(s), {"proto", "rev", "up", "points", "actions"})
        self.assertEqual(s["proto"], PROTO)
        self.assertEqual(s["up"], 100)  # millis 100000

    def test_points_match_descriptor(self):
        _, j = self.d.descriptor()
        _, s = self.d.state()
        self.assertEqual(list(s["points"]), [p["id"] for p in j["points"]])

    def test_unknown_is_null_never_zero(self):
        txt, s = self.d.state()
        for pid in ("temp", "salt", "relay", "cell_voltage", "rssi", "esc_test", "calibrate"):
            self.assertEqual(s["points"][pid], {"v": None}, pid)
        self.assertEqual(raw_v(txt, "temp"), "null")

    def test_set_unknown_after_value(self):
        self.d.cmd("setnum", "temp", "9.5")
        _, s = self.d.state()
        self.assertEqual(s["points"]["temp"]["v"], 9.5)
        self.d.cmd("setunknown", "temp")
        _, s = self.d.state()
        self.assertEqual(s["points"]["temp"], {"v": None})

    def test_nan_and_inf_are_unknown_and_json_stays_valid(self):
        for bad in ("nan", "inf", "-inf"):
            self.d.cmd("setnum", "temp", bad)
            txt, s = self.d.state()  # json.loads weigert ongeldige JSON zoals inf of nan
            self.assertEqual(s["points"]["temp"], {"v": None}, bad)

    def test_value_formats(self):
        txt, s = self.d.state()
        self.assertEqual(raw_v(txt, "ph"), "7.20")        # decimals 2
        self.assertEqual(raw_v(txt, "orp"), "740")        # int zonder punt
        self.assertEqual(raw_v(txt, "alarm_no_flow"), "false")
        self.assertEqual(raw_v(txt, "mode_electro"), "2")  # enum als geheel getal
        self.d.cmd("setnum", "ph", "7.256")
        self.d.cmd("setnum", "orp", "740.6")
        self.d.cmd("setnum", "salt", "3")
        self.d.cmd("setnum", "temp", "-0.04")
        self.d.cmd("setbool", "alarm_no_flow", "1")
        txt, s = self.d.state()
        self.assertEqual(raw_v(txt, "ph"), "7.26")
        self.assertEqual(raw_v(txt, "orp"), "741")
        self.assertEqual(raw_v(txt, "salt"), "3.0")
        self.assertIn(raw_v(txt, "temp"), ("-0.0", "0.0"))
        self.assertEqual(raw_v(txt, "alarm_no_flow"), "true")
        self.assertIs(s["points"]["alarm_no_flow"]["v"], True)
        self.d.cmd("setnum", "orp", "-12")
        txt, _ = self.d.state()
        self.assertEqual(raw_v(txt, "orp"), "-12")

    def test_age(self):
        self.d.cmd("advance", "4200")
        _, s = self.d.state()
        self.assertEqual(s["points"]["ph"]["age"], 4200)
        self.assertNotIn("age", s["points"]["temp"])
        self.d.cmd("setnum", "ph", "7.1")
        _, s = self.d.state()
        self.assertEqual(s["points"]["ph"]["age"], 0)

    def test_text_value_roundtrip(self):
        for t in ('Zwembad', '', 'aa"bb', 'back\\slash', 'regel\nnieuw\ttab', '\x01\x02', '°C 😀 é', '{"id":"x"}'):
            self.d.cmd("settext", "esc_test", t)
            txt, s = self.d.state()
            self.assertEqual(s["points"]["esc_test"]["v"], t)
            self.assertTrue(all(ord(c) >= 0x20 for c in txt))

    def test_actions_only_disabled(self):
        _, s = self.d.state()
        self.assertEqual(s["actions"], {"flush": {"disabled": True, "reason": "pomp draait niet"}})
        self.d.cmd("actionstate", "flush", "0", "")
        self.d.cmd("actionstate", "boost", "1", 'reden "met" aanhalingstekens')
        _, s = self.d.state()
        self.assertEqual(s["actions"], {"boost": {"disabled": True, "reason": 'reden "met" aanhalingstekens'}})
        self.d.cmd("actionstate", "boost", "0", "oude reden")
        _, s = self.d.state()
        self.assertEqual(s["actions"], {})

    def test_point_not_in_descriptor_not_in_state(self):
        self.d.cmd("remove", "orp")
        _, s = self.d.state()
        self.assertNotIn("orp", s["points"])
        self.d.cmd("setnum", "orp", "5")  # onbekend id: geen effect, geen crash
        _, s = self.d.state()
        self.assertNotIn("orp", s["points"])


if __name__ == "__main__":
    unittest.main()
