"""Ontdekking (UDP, §3), identiteit (§2), registratie en dynamische punten (§9)."""
import json
import re
import unittest

from huisdevtest import Base, PROTO

BCAST = "192.168.1.255:47500"
KEYS = {"huisdev", "id", "type", "name", "fw", "rev", "port", "claimed"}


class TestDiscovery(Base):
    def test_nothing_before_first_loop_then_one_announce(self):
        self.assertEqual(self.d.udp_out(), [])
        self.assertEqual(self.d.cmd("udp_state"), ["0", "0", "0", "0"])
        self.d.loop()
        self.assertEqual(self.d.cmd("udp_state"), ["1", "1", "0", "47500"])
        out = self.d.udp_out()
        self.assertEqual(len(out), 1)
        addr, data = out[0]
        self.assertEqual(addr, BCAST)
        j = json.loads(data)
        self.assertEqual(set(j), KEYS)
        self.assertEqual(j["huisdev"], PROTO)
        self.assertEqual(j["id"], "poolcontroller-a1b2c3")
        self.assertEqual((j["type"], j["name"], j["fw"], j["port"], j["claimed"]), ("poolcontroller", "Zwembad", "0.3.1", 80, False))
        self.assertEqual(j["rev"], self.d.rev())
        self.d.loop()
        self.assertEqual(self.d.udp_out(), [], "geen tweede aankondiging zonder reden")

    def test_no_announce_without_wifi(self):
        self.d.cmd("wifi", "0")
        self.d.loop()
        self.assertEqual(self.d.udp_out(), [])
        self.assertEqual(self.d.cmd("udp_state"), ["0", "0", "0", "0"])

    def test_query_gets_unicast_reply_to_source(self):
        self.d.loop()
        self.d.udp_out()
        self.d.cmd("udp_in", "192.168.1.20", "51234", "HUISDEV?1.0")
        self.d.loop()
        out = self.d.udp_out()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], "192.168.1.20:51234")
        j = json.loads(out[0][1])
        self.assertEqual(set(j), KEYS)
        self.assertEqual(j["rev"], self.d.rev())
        self.assertIs(j["claimed"], False)
        self.d.claim()
        self.d.cmd("udp_in", "10.0.0.7", "40000", "HUISDEV?1.0")
        self.d.loop()
        (addr, data), = self.d.udp_out()
        self.assertEqual(addr, "10.0.0.7:40000")
        self.assertIs(json.loads(data)["claimed"], True)

    def test_any_version_after_prefix_is_answered(self):
        self.d.loop()
        self.d.udp_out()
        for payload in ("HUISDEV?", "HUISDEV?1.0", "HUISDEV?9.9", "HUISDEV?" + "x" * 300):
            self.d.cmd("udp_in", "192.168.1.20", "5000", payload)
            self.d.loop()
            self.assertEqual(len(self.d.udp_out()), 1, payload[:20])

    def test_other_packets_are_ignored(self):
        self.d.loop()
        announce = self.d.udp_out()[0][1]
        for payload in ("huisdev?1.0", " HUISDEV?1.0", "xHUISDEV?", "HUISDEV", "HUISDEV1.0", "{}", announce,
                        "GET / HTTP/1.1", "\x00\x01\x02", "HUISDEV!"):
            self.d.cmd("udp_in", "192.168.1.20", "5000", payload)
            self.d.loop()
            self.assertEqual(self.d.udp_out(), [], repr(payload))

    def test_several_queries_in_one_loop(self):
        self.d.loop()
        self.d.udp_out()
        for port in (6001, 6002, 6003):
            self.d.cmd("udp_in", "192.168.1.30", str(port), "HUISDEV?1.0")
        self.d.loop()
        self.assertEqual([a for a, _ in self.d.udp_out()], ["192.168.1.30:6001", "192.168.1.30:6002", "192.168.1.30:6003"])

    def test_wifi_drop_and_return(self):
        self.d.loop()
        self.d.udp_out()
        self.d.cmd("wifi", "0")
        self.d.loop()
        self.assertEqual(self.d.cmd("udp_state"), ["0", "1", "1", "47500"])
        self.d.cmd("udp_in", "192.168.1.20", "5000", "HUISDEV?1.0")  # socket is dicht: pakket gaat verloren
        self.d.loop()
        self.assertEqual(self.d.udp_out(), [])
        self.d.cmd("wifi", "1")
        self.d.loop()
        self.assertEqual(self.d.cmd("udp_state"), ["1", "2", "1", "47500"])
        out = self.d.udp_out()
        self.assertEqual([a for a, _ in out], [BCAST], "aankondiging na het terugkomen van wifi")

    def test_announce_on_rev_change_only(self):
        self.d.loop()
        self.d.udp_out()
        self.d.cmd("setnum", "ph", "7.0")
        self.d.cmd("rebegin", "poolcontroller", "Nieuwe naam", "2.0", "80")  # naam/fw: geen rev-wijziging
        self.d.loop()
        self.assertEqual(self.d.udp_out(), [])
        old = self.d.rev()
        self.d.cmd("addpoint", "nieuw", "Nieuw", "int")
        self.d.loop()
        (addr, data), = self.d.udp_out()
        self.assertEqual(addr, BCAST)
        j = json.loads(data)
        self.assertEqual(j["rev"], self.d.rev())
        self.assertNotEqual(j["rev"], old)
        self.assertEqual(j["name"], "Nieuwe naam")
        self.d.cmd("unit", "salt", "ppm")
        self.d.loop()
        self.assertEqual(len(self.d.udp_out()), 1)

    def test_announce_when_state_poll_notices_new_rev(self):
        # de add-on pollt vaak sneller dan loop() draait: ook dan moet er een aankondiging volgen, maar niet twee
        self.d.loop()
        self.d.udp_out()
        self.d.cmd("addpoint", "nieuw", "Nieuw", "int")
        self.d.state()
        self.d.loop()
        self.assertEqual(len(self.d.udp_out()), 1)

    def test_periodic_announce_every_5_minutes(self):
        self.d.loop()
        self.d.udp_out()
        self.d.cmd("advance", "299999")
        self.d.loop()
        self.assertEqual(self.d.udp_out(), [])
        self.d.cmd("advance", "2")
        self.d.loop()
        self.assertEqual(len(self.d.udp_out()), 1)
        self.d.cmd("advance", "100000")
        self.d.loop()
        self.assertEqual(self.d.udp_out(), [])

    def test_port_and_broadcast_address(self):
        self.d.cmd("rebegin", "poolcontroller", "Zwembad", "0.3.1", "8080")
        self.d.cmd("bcast", "10.1.2.255")
        self.d.loop()
        (addr, data), = self.d.udp_out()
        self.assertEqual(addr, "10.1.2.255:47500")
        self.assertEqual(json.loads(data)["port"], 8080)

    def test_reply_is_one_valid_utf8_json_line(self):
        self.d.cmd("rebegin", "poolcontroller", 'Zwem"bad\\ °', 'fw"', "80")
        self.d.loop()
        self.d.udp_out()
        self.d.cmd("udp_in", "192.168.1.20", "5000", "HUISDEV?1.0")
        self.d.loop()
        (_, data), = self.d.udp_out()
        self.assertNotIn("\n", data)
        self.assertEqual(json.loads(data)["name"], 'Zwem"bad\\ °')


class TestIdentity(Base):
    def test_id_from_mac(self):
        self.assertEqual(self.d.cmd("id"), ["poolcontroller-a1b2c3"])
        self.d.cmd("mac", "00:11:22:33:44:55")
        self.d.cmd("reboot")
        self.assertEqual(self.d.cmd("id"), ["poolcontroller-334455"])
        _, j = self.d.descriptor()
        self.assertEqual(j["device"]["id"], "poolcontroller-334455")
        self.assertEqual(j["device"]["mac"], "00:11:22:33:44:55")

    def test_slug_from_type(self):
        self.d.cmd("rebegin", "My Dev_1", "N", "1", "80")
        self.assertEqual(self.d.cmd("id"), ["my-dev-1-a1b2c3"])

    def test_id_never_longer_than_32(self):
        for t in ("x" * 40, "poolcontroller-extra-lange-naam-met-veel-tekens"):
            self.d.cmd("rebegin", t, "N", "1", "80")
            (i,) = self.d.cmd("id")
            self.assertRegex(i, r"^[a-z0-9-]{1,32}$")
            self.assertTrue(i.endswith("-a1b2c3"), i)

    def test_id_stable_across_boot_and_rename(self):
        i = self.d.cmd("id")
        self.d.cmd("rebegin", "poolcontroller", "Andere naam", "2.0", "80")
        self.assertEqual(self.d.cmd("id"), i)
        self.d.cmd("reboot")
        self.assertEqual(self.d.cmd("id"), i)


class TestRegistration(Base):
    def test_idempotent_reregistration(self):
        txt, _ = self.d.descriptor()
        for _ in range(3):
            self.d.cmd("rereg")
        txt2, _ = self.d.descriptor()
        self.assertEqual(txt2, txt, "dezelfde registratie opnieuw uitvoeren mag niets veranderen (ook geen dubbele opties)")

    def test_kind_change_keeps_original(self):
        self.d.cmd("addpoint", "ph", "pH", "int")
        self.d.cmd("addpoint", "mode_electro", "Elektrolyse", "text")
        _, j = self.d.descriptor()
        P = {p["id"]: p for p in j["points"]}
        self.assertEqual(P["ph"]["kind"], "float")
        self.assertEqual(P["mode_electro"]["kind"], "enum")

    def test_point_limit_64(self):
        self.d.cmd("reboot_empty")
        for i in range(64):
            self.d.cmd("addpoint", "p%d" % i, "P%d" % i, "int")
        r = self.d.rev()
        for i in range(64, 70):
            self.d.cmd("addpoint", "p%d" % i, "P%d" % i, "int")
        _, j = self.d.descriptor()
        self.assertEqual(len(j["points"]), 64)
        self.assertEqual(j["rev"], r)
        self.d.cmd("setnum", "p65", "5")  # niet geregistreerd: geen effect
        _, s = self.d.state()
        self.assertEqual(len(s["points"]), 64)
        self.assertNotIn("p65", s["points"])
        self.d.cmd("remove", "p3")
        self.d.cmd("addpoint", "p70", "P70", "int")  # plek vrij na verwijderen
        _, j = self.d.descriptor()
        self.assertEqual(len(j["points"]), 64)
        self.assertIn("p70", [p["id"] for p in j["points"]])

    def test_overflowing_builder_calls_do_not_touch_points(self):
        self.d.cmd("reboot_empty")
        for i in range(64):
            self.d.cmd("addpoint", "p%d" % i, "P%d" % i, "int")
        before, _ = self.d.descriptor()
        self.d.cmd("addpoint", "extra", "Extra", "int")
        self.d.cmd("unit", "extra", "kWh")  # zou op het overloop-punt landen
        after, _ = self.d.descriptor()
        self.assertEqual(after, before)

    def test_action_limit_16(self):
        self.d.cmd("reboot_empty")
        for i in range(20):
            self.d.cmd("addaction", "a%d" % i, "A%d" % i)
        _, j = self.d.descriptor()
        self.assertEqual(len(j["actions"]), 16)

    def test_arg_limit_8(self):
        self.d.cmd("reboot_empty")
        self.d.cmd("addaction", "multi", "Multi")
        for i in range(12):
            self.d.cmd("addarg", "multi", "arg%d" % i, "Arg %d" % i, "int")
        _, j = self.d.descriptor()
        self.assertEqual(len(j["actions"][0]["args"]), 8)

    def test_remove_then_state_and_descriptor_agree(self):
        self.d.cmd("remove", "salt")
        _, j = self.d.descriptor()
        _, s = self.d.state()
        self.assertEqual([p["id"] for p in j["points"]], list(s["points"]))
        self.assertNotIn("salt", s["points"])

    def test_dynamic_add_remove_cycle_stays_valid(self):
        for n in range(30):
            self.d.cmd("addpoint", "dyn", "Dyn", "float")
            self.d.cmd("setnum", "dyn", str(n))
            self.d.descriptor()
            self.d.state()
            self.d.cmd("remove", "dyn")
        _, j = self.d.descriptor()
        self.assertEqual(len(j["points"]), 16)

    def test_enum_option_relabel_does_not_duplicate(self):
        self.d.cmd("option", "mode_electro", "1", "Handmatig!")
        _, j = self.d.descriptor()
        P = {p["id"]: p for p in j["points"]}
        self.assertEqual(P["mode_electro"]["options"], [{"v": 0, "l": "Uit"}, {"v": 1, "l": "Handmatig!"}, {"v": 2, "l": "Auto"}])

    def test_float_precision_of_min_max_step(self):
        self.d.cmd("range", "ph_set", "0.0005", "7.6", "0.0005")
        _, j = self.d.descriptor()
        P = {p["id"]: p for p in j["points"]}
        self.assertEqual((P["ph_set"]["min"], P["ph_set"]["step"]), (0.0005, 0.0005))


if __name__ == "__main__":
    unittest.main()
