"""Schrijft fixtures die de ECHTE huisdev.h maakte, zodat de add-on-tests ze kunnen hergebruiken.

Alle waarden zijn deterministisch (vaste klok en MAC). Bestanden in tests/fixtures/ zijn de ruwe tekst zoals het apparaat die stuurde.
Het device is het poolcontroller-achtige testdevice uit tests/driver.cpp (token: 0123456789abcdef0123456789abcdef).
"""
import json
import os
import unittest

from huisdevtest import FIXTURES, Base, TOKEN


class TestFixtures(Base):
    def write(self, name, text):
        os.makedirs(FIXTURES, exist_ok=True)
        json.loads(text)  # alleen geldige JSON als fixture
        with open(os.path.join(FIXTURES, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def test_write_fixtures(self):
        d = self.d
        d.cmd("loop")
        for (addr, data) in d.udp_out():
            self.write("announce.json", data)
        d.cmd("udp_in", "192.168.1.20", "51234", "HUISDEV?1.0")
        d.loop()
        (_, data), = d.udp_out()
        self.write("discovery.json", data)

        txt, _ = d.descriptor()
        self.write("descriptor.json", txt)
        st, txt = d.handle("GET", "/huisdev/state")
        self.write("state.json", txt)

        d.claim()
        st, txt = d.handle("GET", "/huisdev/state", TOKEN)
        self.write("state_claimed.json", txt)

        def post(name, path, body, token=TOKEN):
            st, txt = d.handle("POST", path, token, json.dumps(body))
            self.write(name, txt)
            return st

        self.assertEqual(post("set_applied.json", "/huisdev/set", {"id": "relay", "value": True}), 200)
        self.assertEqual(post("set_queued.json", "/huisdev/set", {"id": "ph_set", "value": 7.2}), 200)
        self.assertEqual(post("set_rejected.json", "/huisdev/set", {"id": "pump", "value": True}), 200)
        self.assertEqual(post("do_queued.json", "/huisdev/do", {"id": "boost", "args": {"minutes": 30}}), 200)
        self.assertEqual(post("error_401_token.json", "/huisdev/set", {"id": "relay", "value": True}, token="fout"), 401)
        self.assertEqual(post("error_403_not_writable.json", "/huisdev/set", {"id": "ph", "value": 7.0}), 403)
        self.assertEqual(post("error_404_unknown_point.json", "/huisdev/set", {"id": "bestaat_niet", "value": 1}), 404)
        self.assertEqual(post("error_404_unknown_action.json", "/huisdev/do", {"id": "bestaat_niet"}), 404)
        self.assertEqual(post("error_409_confirm_required.json", "/huisdev/do", {"id": "reset"}), 409)
        self.assertEqual(post("error_409_disabled.json", "/huisdev/do", {"id": "flush"}), 409)
        self.assertEqual(post("error_409_already_claimed.json", "/huisdev/claim", {"token": "a" * 32}, token=""), 409)
        self.assertEqual(post("error_422_range.json", "/huisdev/set", {"id": "ph_set", "value": 9.0}), 422)
        self.assertEqual(post("error_422_step.json", "/huisdev/set", {"id": "ph_set", "value": 7.25}), 422)
        self.assertEqual(post("error_422_bad_option.json", "/huisdev/set", {"id": "mode_electro", "value": 9}), 422)
        self.assertEqual(post("error_422_bad_value.json", "/huisdev/set", {"id": "relay", "value": "aan"}), 422)
        self.assertEqual(post("error_422_missing_arg.json", "/huisdev/do", {"id": "boost"}), 422)
        st, txt = d.handle("GET", "/huisdev/state", "")
        self.assertEqual(st, 401)
        self.write("error_401_state.json", txt)
        st, txt = d.handle("GET", "/huisdev/nope", TOKEN)
        self.assertEqual(st, 404)
        self.write("error_404_unknown_path.json", txt)
        st, txt = d.handle("POST", "/huisdev/state", TOKEN, "{}")
        self.assertEqual(st, 405)
        self.write("error_405_method.json", txt)

        # dynamisch: er komen punten bij, een punt verdwijnt en de eenheid wisselt (rev 2)
        d.cmd("addpoint", "chlorine", "Chloor", "float")
        d.cmd("unit", "chlorine", "ppm")
        d.cmd("setnum", "chlorine", "1.25")
        d.cmd("remove", "cell_voltage")
        d.cmd("unit", "temp", "°F")
        d.cmd("setnum", "temp", "49")
        txt, _ = d.descriptor()
        self.write("descriptor_rev2.json", txt)
        st, txt = d.handle("GET", "/huisdev/state", TOKEN)
        self.write("state_rev2.json", txt)
        with open(os.path.join(FIXTURES, "descriptor.json"), encoding="utf-8") as f:
            rev1 = json.load(f)["rev"]
        self.assertNotEqual(rev1, json.loads(txt)["rev"])


if __name__ == "__main__":
    unittest.main()
