"""Schrijft fixtures die de ECHTE huisdev.h maakte, zodat de add-on-tests ze kunnen hergebruiken.

Alle waarden zijn deterministisch (vaste klok en MAC). Bestanden in tests/fixtures/ zijn de ruwe tekst zoals het apparaat die stuurde.
Het device is het poolcontroller-achtige testdevice uit tests/driver.cpp (token: 0123456789abcdef0123456789abcdef).
"""
import json
import os
import unittest

from huisdevtest import FIXTURES, Base, TOKEN, b64, fnv1a


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


    def test_write_scene_fixtures(self):
        """Hetzelfde device met scenes (HUISDEV.md §16): 8 slots, 768 bytes, 8 geplande starts, klok op een vast tijdstip."""
        d = self.d
        now = 1760000123
        d.scenes_on(8, 768, 8, "ledscene/1")
        d.claim()
        d.settime(now)
        txt, j = d.descriptor()
        self.write("descriptor_scenes.json", txt)
        self.assertEqual(j["scenes"], {"formats": ["ledscene/1"], "slots": 8, "max_bytes": 768, "sched": 8})

        a = bytes((i * 7 + 3) & 255 for i in range(424))
        b = bytes((i * 11 + 5) & 255 for i in range(122))
        d.advance(2500)
        st, txt = d.handle("POST", "/huisdev/scene/store", TOKEN, json.dumps({"slot": 1, "data": b64(a)}))
        self.assertEqual(st, 200)
        self.write("scene_store_applied.json", txt)
        d.advance(2500)
        d.sc("store", {"slot": 3, "data": b64(b)})
        self.assertEqual(d.sc("play", {"slot": 1})[1]["result"], "applied")
        d.scene_running(1, fnv1a(a), now + 1077)  # het device meldt het einde van de scene
        d.advance(2500)
        self.assertEqual(d.sc("schedule", {"entries": [{"slot": 1, "at": 1760000400}, {"slot": 1, "at": 1760086800}]})[1]["result"], "applied")
        st, txt = d.handle("GET", "/huisdev/state", TOKEN)
        self.write("state_scenes.json", txt)
        self.assertEqual(json.loads(txt)["scenes"]["time"], now)

        # een device dat een scene zonder klok niet kan afspelen weigert (hier: een scene die de klok nodig heeft)
        d.scene_idle()
        d.settime(0)
        d.cmd("scene_cfg", "play", "rejected", "geen tijd")
        st, txt = d.handle("POST", "/huisdev/scene/play", TOKEN, json.dumps({"slot": 3}))
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(txt)["result"], "rejected")
        self.write("scene_play_rejected_notime.json", txt)

        def post(name, path, body):
            st, txt = d.handle("POST", path, TOKEN, json.dumps(body))
            self.write(name, txt)
            return st

        self.assertEqual(post("error_422_bad_slot.json", "/huisdev/scene/store", {"slot": 9, "data": b64(a)}), 422)
        self.assertEqual(post("error_422_too_large.json", "/huisdev/scene/store", {"slot": 1, "data": b64(bytes(769))}), 422)
        self.assertEqual(post("error_422_bad_data.json", "/huisdev/scene/store", {"slot": 1, "data": "###"}), 422)

    def test_write_seek_fixtures(self):
        """Het scenes-device van de scene-fixtures, nu met onSceneSeek (1.2): scene/seek en `run.seek`."""
        d = self.d
        now = 1760000123
        d.scenes_on(8, 768, 8, "ledscene/1")
        d.cmd("seek_on")
        d.claim()
        d.settime(now)
        txt, j = d.descriptor()
        self.write("descriptor_seek.json", txt)
        self.assertEqual(j["scenes"], {"formats": ["ledscene/1"], "slots": 8, "max_bytes": 768, "sched": 8, "seek": True})

        # een seek zonder scene om te tonen
        st, txt = d.handle("POST", "/huisdev/scene/seek", TOKEN, json.dumps({"t": 5000}))
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(txt)["result"], "rejected")
        self.write("scene_seek_rejected_noscene.json", txt)

        a = bytes((i * 7 + 3) & 255 for i in range(424))
        b = bytes((i * 11 + 5) & 255 for i in range(122))
        d.advance(2500)
        d.sc("store", {"slot": 1, "data": b64(a)})
        d.advance(2500)
        d.sc("store", {"slot": 3, "data": b64(b)})
        d.advance(2500)
        self.assertEqual(d.sc("schedule", {"entries": [{"slot": 1, "at": 1760000400}, {"slot": 1, "at": 1760086800}]})[1]["result"], "applied")
        st, txt = d.handle("POST", "/huisdev/scene/seek", TOKEN, json.dumps({"slot": 1, "t": 123456}))
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(txt)["hash"], fnv1a(a))
        self.write("scene_seek_applied.json", txt)
        st, txt = d.handle("GET", "/huisdev/state", TOKEN)
        self.write("state_seek.json", txt)
        self.assertEqual(json.loads(txt)["scenes"]["run"], {"slot": 1, "h": fnv1a(a), "until": None, "seek": 123456})


if __name__ == "__main__":
    unittest.main()
