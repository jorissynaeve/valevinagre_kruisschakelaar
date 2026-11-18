"""scene/seek (HUISDEV.md §16, protocol 1.2): descriptor en rev, de drie bodyvormen, `t`, validatie, `run.seek`, geen flash en geen invloed op `sched`."""
import json
import unittest

from huisdevtest import Base, Dev, TOKEN, b64, fnv1a, rev_body
from test_scenes import A, B, NOW, REV_1_0_2, SCENES_OBJ, SceneBase

# De rev van het testdevice met scenes (8 slots, 768 bytes, 8 sched, ledscene/1) onder header 1.1.0 (vastgelegd in fixtures/descriptor_scenes.json):
# een device zonder onSceneSeek moet die houden.
REV_SCENES_1_1_0 = "2e610df9"
SEEK_OBJ = SCENES_OBJ[:-1] + ',"seek":true}'
MAX_T = 86400000


class SeekBase(SceneBase):
    """Een geclaimd device met scenes (zoals SceneBase) en onSceneSeek."""

    def setUp(self):
        super().setUp()
        self.d.cmd("seek_on")

    def seek(self, obj=None, raw=None, token=TOKEN):
        return self.d.sc("seek", obj, token=token, raw=raw)

    def seek_ok(self, obj):
        st, j = self.seek(obj)
        self.assertEqual((st, j["result"]), (200, "applied"), j)
        return j


class TestDescriptor(Base):
    def test_device_without_callback_is_identical_to_1_1_0(self):
        self.d.scenes_on()
        txt, j = self.d.descriptor()
        self.assertEqual(j["rev"], REV_SCENES_1_1_0)
        self.assertTrue(txt.endswith("]," + SCENES_OBJ + "}"), txt[-120:])
        self.assertNotIn("seek", txt)
        self.assertNotIn("seek", j["scenes"])
        self.assertEqual(rev_body(txt).endswith(SCENES_OBJ), True)
        self.d.claim()
        _, s = self.d.state(TOKEN)
        self.assertEqual(s["rev"], REV_SCENES_1_1_0)
        self.assertEqual(set(s["scenes"]), {"have", "sched", "time"})

    def test_device_without_scenes_and_without_callback_keeps_its_1_0_rev(self):
        txt, j = self.d.descriptor()
        self.assertEqual(j["rev"], REV_1_0_2)
        self.assertNotIn("seek", txt)

    def test_seek_callback_adds_seek_true_to_the_scenes_object(self):
        self.d.scenes_on()
        self.d.cmd("seek_on")
        txt, j = self.d.descriptor()
        self.assertTrue(txt.endswith("]," + SEEK_OBJ + "}"), txt[-120:])
        self.assertEqual(j["scenes"], {"formats": ["ledscene/1"], "slots": 8, "max_bytes": 768, "sched": 8, "seek": True})
        self.assertEqual(list(j["scenes"])[-1], "seek")
        self.assertLessEqual(len(txt.encode()), 8192)

    def test_rev_covers_seek(self):
        self.d.scenes_on()
        self.d.cmd("seek_on")
        txt, j = self.d.descriptor()
        self.assertEqual(j["rev"], fnv1a(rev_body(txt).encode("utf-8")))
        self.assertTrue(rev_body(txt).endswith(SEEK_OBJ))
        self.assertNotEqual(j["rev"], REV_SCENES_1_1_0)

    def test_rev_changes_back_and_forth_and_is_announced(self):
        self.d.scenes_on()
        self.d.loop()
        self.d.udp_out()
        self.d.cmd("seek_on")
        rev_on = self.d.rev()
        self.assertNotEqual(rev_on, REV_SCENES_1_1_0)
        self.d.loop()
        out = self.d.udp_out()
        self.assertEqual([json.loads(x[1])["rev"] for x in out], [rev_on])
        self.d.cmd("seek_on")  # nog eens: geen wijziging, geen aankondiging
        self.assertEqual(self.d.rev(), rev_on)
        self.d.loop()
        self.assertEqual(self.d.udp_out(), [])
        self.d.cmd("seek_off")
        self.assertEqual(self.d.rev(), REV_SCENES_1_1_0)
        self.d.loop()
        self.assertEqual([json.loads(x[1])["rev"] for x in self.d.udp_out()], [REV_SCENES_1_1_0])

    def test_rev_stable_after_reboot(self):
        self.d.scenes_on()
        self.d.cmd("seek_on")
        rev = self.d.rev()
        self.d.cmd("reboot")
        self.assertEqual(self.d.rev(), rev)

    def test_callback_before_scenes_call_gives_the_same_descriptor(self):
        self.d.cmd("seek_on")
        self.assertEqual(self.d.rev(), REV_1_0_2)  # zonder scenes() geen scenes-object, dus ook geen seek
        self.d.scenes_on()
        txt, j = self.d.descriptor()
        self.assertTrue(txt.endswith("]," + SEEK_OBJ + "}"))
        other = Dev()
        try:
            other.cmd("factory")
            other.scenes_on()
            other.cmd("seek_on")
            self.assertEqual(other.descriptor()[0], txt)
        finally:
            other.close()

    def test_without_scenes_the_path_does_not_exist_even_with_a_callback(self):
        self.d.cmd("seek_on")
        self.d.claim()
        self.assertEqual(self.d.rev(), REV_1_0_2)
        for method in ("POST", "GET"):
            self.assertErr(self.d.jh(method, "/huisdev/scene/seek", TOKEN, '{"t":0}'), 404, "unknown_path")
        self.d.cmd("reboot")  # ook na een herstart (de callback wordt dan weer gezet)
        self.assertErr(self.d.jh("POST", "/huisdev/scene/seek", TOKEN, '{"t":0}'), 404, "unknown_path")
        self.assertEqual(self.d.cmd("scene_log"), [])


class TestWithoutCallback(SceneBase):
    """Een device met scenes maar zonder onSceneSeek: het pad bestaat niet."""

    def test_404_for_every_method_and_without_a_claim(self):
        for method in ("POST", "GET", "PUT"):
            for token in (TOKEN, "fout", ""):
                self.assertErr(self.d.jh(method, "/huisdev/scene/seek", token, '{"t":0}'), 404, "unknown_path")
        self.d.cmd("resetclaim")
        self.assertErr(self.d.jh("POST", "/huisdev/scene/seek", "", '{"t":0}'), 404, "unknown_path")

    def test_the_other_scene_paths_still_work(self):
        self.assertResult(self.d.sc("store", {"slot": 1, "data": b64(A)}), "applied")
        self.assertResult(self.d.sc("play", {"slot": 1}), "applied")
        self.assertEqual(self.running(), {"slot": 1, "h": fnv1a(A), "until": None})

    def test_state_never_has_seek(self):
        self.d.sc("play", {"data": b64(A)})
        self.assertEqual(self.running(), {"slot": 0, "h": fnv1a(A), "until": None})


class TestAuthAndRouting(SeekBase):
    def test_wrong_methods_give_405(self):
        for method in ("GET", "PUT", "DELETE", "HEAD"):
            self.assertErr(self.d.jh(method, "/huisdev/scene/seek", TOKEN, '{"t":0}'), 405, "method")

    def test_405_comes_before_the_claim_check(self):
        self.d.cmd("resetclaim")
        self.assertErr(self.d.jh("GET", "/huisdev/scene/seek", "", ""), 405, "method")

    def test_not_claimed_is_403(self):
        self.d.cmd("resetclaim")
        self.assertErr(self.seek({"t": 0}), 403, "not_claimed")

    def test_wrong_or_missing_token_is_401(self):
        self.assertErr(self.seek({"t": 0}, token="fout"), 401, "token")
        self.assertErr(self.seek({"t": 0}, token=""), 401, "token")
        self.assertErr(self.seek({"t": 0}, token=TOKEN + "x"), 401, "token")

    def test_auth_failures_change_nothing(self):
        self.d.cmd("resetclaim")
        self.d.cmd("scene_log")
        self.seek({"t": 0})
        self.assertEqual(self.d.cmd("scene_log"), [])

    def test_through_the_webserver_adapter(self):
        self.assertResult(self.d.sc("store", {"slot": 1, "data": b64(A)}), "applied")
        r = self.d.cmd("ws_call", "POST", "/huisdev/scene/seek", TOKEN, json.dumps({"slot": 1, "t": 500}))
        self.assertEqual((r[0], r[4]), ("200", "routed"))
        self.assertEqual(json.loads(r[3])["result"], "applied")
        r = self.d.cmd("ws_call", "POST", "/huisdev/scene/seek", "", json.dumps({"t": 500}))
        self.assertEqual(r[0], "401")
        r = self.d.cmd("ws_call", "GET", "/huisdev/scene/seek", TOKEN, "")
        self.assertEqual(r[0], "405")


class TestSlotForm(SeekBase):
    def test_seek_slot(self):
        self.store(1, A)
        self.d.scene_log()
        st, j = self.seek({"slot": 1, "t": 123456})
        self.assertEqual((st, j), (200, {"ok": True, "result": "applied", "reason": "", "hash": fnv1a(A)}))
        self.assertEqual(self.d.scene_log(), ["seek|1|3|123456|" + fnv1a(A)])  # slot, bytes-lengte, t; de check is niet nodig (stond al in het slot)
        self.assertEqual(self.running(), {"slot": 1, "h": fnv1a(A), "until": None, "seek": 123456})

    def test_state_text_has_seek_after_until(self):
        self.store(1, A)
        self.seek_ok({"slot": 1, "t": 7})
        txt, _ = self.d.state(TOKEN)
        self.assertIn('"run":{"slot":1,"h":"%s","until":null,"seek":7}' % fnv1a(A), txt)

    def test_the_header_hands_over_the_bytes_of_the_slot(self):
        self.store(2, B)
        self.d.scene_log()
        self.seek_ok({"slot": 2, "t": 1})
        self.assertEqual(self.d.scene_log(), ["seek|2|5|1|" + fnv1a(B)])

    def test_seek_again_moves_only_the_time(self):
        self.store(1, A)
        self.seek_ok({"slot": 1, "t": 1000})
        self.d.scene_log()
        for t in (2000, 0, 86400000, 5):
            self.seek_ok({"slot": 1, "t": t})
            self.assertEqual(self.running(), {"slot": 1, "h": fnv1a(A), "until": None, "seek": t})
        self.assertEqual(self.d.scene_log(), ["seek|1|3|%d|%s" % (t, fnv1a(A)) for t in (2000, 0, 86400000, 5)])  # elke aanroep gaat naar het device

    def test_seek_another_slot_replaces_the_hold(self):
        self.store(1, A)
        self.store(2, B)
        self.seek_ok({"slot": 1, "t": 10})
        self.seek_ok({"slot": 2, "t": 20})
        self.assertEqual(self.running(), {"slot": 2, "h": fnv1a(B), "until": None, "seek": 20})

    def test_slot_edges(self):
        self.store(1, A)
        self.d.scene_log()
        for slot in (0, 9, -1, 100, 1.5, "1", None, True):
            self.assertErr(self.seek({"slot": slot, "t": 0}), 422, "bad_slot")
        self.assertErr(self.seek(raw='{"slot":,"t":0}'), 422, "bad_slot")
        self.assertEqual(self.d.scene_log(), [])
        st, j = self.seek({"slot": 1.0, "t": 0})
        self.assertEqual(j["result"], "applied")  # een geheel getal mag als 1.0 geschreven zijn
        st, j = self.seek(raw='{"slot":1e0,"t":0}')
        self.assertEqual(j["result"], "applied")

    def test_slot_range_follows_the_device(self):
        self.d.scenes_on(2, 100, 2, "ledscene/1")
        self.assertErr(self.seek({"slot": 3, "t": 0}), 422, "bad_slot")
        self.assertResult(self.seek({"slot": 2, "t": 0}), "rejected", "slot leeg")

    def test_empty_slot_is_rejected(self):
        self.d.scene_log()
        st, j = self.seek({"slot": 4, "t": 100})
        self.assertEqual((st, j), (200, {"ok": False, "result": "rejected", "reason": "slot leeg"}))
        self.assertEqual(self.d.scene_log(), [])
        self.assertIsNone(self.running())

    def test_wiped_slot_is_rejected(self):
        self.store(1, A)
        self.assertResult(self.store(1, b""), "applied")
        self.assertResult(self.seek({"slot": 1, "t": 0}), "rejected", "slot leeg")

    def test_syntax_errors_come_before_content(self):
        self.assertErr(self.seek({"slot": 4, "t": -1}), 422, "bad_value")  # leeg slot, maar t is fout: 422 en geen `rejected`
        self.assertErr(self.seek({"slot": 4, "t": "1"}), 422, "bad_value")

    def test_seek_does_not_need_a_clock(self):
        self.store(1, A)
        self.d.settime(0)
        self.assertResult(self.seek({"slot": 1, "t": 5}), "applied")
        self.assertEqual(self.running(), {"slot": 1, "h": fnv1a(A), "until": None, "seek": 5})

    def test_check_is_not_called_for_a_stored_slot(self):
        self.store(1, A)
        self.d.cmd("scene_cfg", "check", "ongeldig")
        self.d.scene_log()
        self.assertResult(self.seek({"slot": 1, "t": 5}), "applied")
        self.assertEqual(self.d.scene_log(), ["seek|1|3|5|" + fnv1a(A)])


class TestDataForm(SeekBase):
    def test_seek_data_has_slot_zero_and_checks_first(self):
        st, j = self.seek({"data": b64(A), "t": 3000})
        self.assertEqual((st, j), (200, {"ok": True, "result": "applied", "reason": "", "hash": fnv1a(A)}))
        self.assertEqual(self.d.scene_log(), ["check|3|" + fnv1a(A), "seek|0|3|3000|" + fnv1a(A)])
        self.assertEqual(self.running(), {"slot": 0, "h": fnv1a(A), "until": None, "seek": 3000})
        self.assertEqual(self.have(), {})  # bewaart niets

    def test_resending_the_same_data_skips_the_check(self):
        self.seek_ok({"data": b64(A), "t": 1})
        self.d.scene_log()
        self.seek_ok({"data": b64(A), "t": 2})
        self.assertEqual(self.d.scene_log(), ["seek|0|3|2|" + fnv1a(A)])
        self.seek_ok({"data": b64(B), "t": 2})  # een andere scene: weer controleren
        self.assertEqual(self.d.scene_log(), ["check|5|" + fnv1a(B), "seek|0|5|2|" + fnv1a(B)])

    def test_check_rejection(self):
        self.d.cmd("scene_cfg", "check", "ongeldig")
        st, j = self.seek({"data": b64(A), "t": 5})
        self.assertEqual((st, j), (200, {"ok": False, "result": "rejected", "reason": "ongeldig"}))
        self.assertEqual(self.d.scene_log(), ["check|3|" + fnv1a(A)])
        self.assertIsNone(self.running())

    def test_data_errors(self):
        for raw in ('{"data":"###","t":0}', '{"data":"QQ","t":0}', '{"data":5,"t":0}', '{"data":null,"t":0}', '{"data":"abc","t":0}', '{"data":"","t":0}', '{"data":"AQI","t":0}', '{"data":"AQID ","t":0}'):
            self.assertErr(self.seek(raw=raw), 422, "bad_data")
        self.assertEqual(self.d.scene_log(), [])
        self.assertIsNone(self.running())

    def test_invalid_base64_variants(self):
        for data in ("AQID\n", "AQ==AQ==", "A-_=", "AQ=D", "AR=="):
            self.assertErr(self.seek({"data": data, "t": 0}), 422, "bad_data")
        self.assertResult(self.seek({"data": "AQ==", "t": 0}), "applied")

    def test_escaped_slash_in_data(self):
        data = bytes([0xFF, 0xFE, 0xFD, 0xFC])  # base64 bevat '/'
        self.assertIn("/", b64(data))
        st, j = self.seek(raw='{"data":"%s","t":9}' % b64(data).replace("/", "\\/"))
        self.assertEqual((st, j["result"], j["hash"]), (200, "applied", fnv1a(data)))

    def test_too_large(self):
        self.assertResult(self.seek({"data": b64(bytes(768)), "t": 0}), "applied")
        self.assertErr(self.seek({"data": b64(bytes(769)), "t": 0}), 422, "too_large")

    def test_slot_and_data_together_is_bad_value(self):
        self.store(1, A)
        self.d.scene_log()
        self.assertErr(self.seek({"slot": 1, "data": b64(A), "t": 0}), 422, "bad_value")
        self.assertErr(self.seek({"slot": 99, "data": "###", "t": 0}), 422, "bad_value")
        self.assertErr(self.seek({"slot": 1, "data": b64(A)}), 422, "bad_value")
        self.assertEqual(self.d.scene_log(), [])

    def test_seek_data_does_not_write(self):
        w = self.d.writes()
        self.seek_ok({"data": b64(A), "t": 1})
        self.seek_ok({"data": b64(B), "t": 2})
        self.assertEqual(self.d.writes(), w)


class TestTimeOnlyForm(SeekBase):
    def test_t_only_without_a_previous_seek_is_rejected(self):
        self.d.scene_log()
        st, j = self.seek({"t": 100})
        self.assertEqual((st, j), (200, {"ok": False, "result": "rejected", "reason": "geen scène om te tonen"}))
        self.assertEqual(self.d.scene_log(), [])
        self.assertIsNone(self.running())

    def test_t_only_calls_the_device_with_minus_one(self):
        self.store(1, A)
        self.seek_ok({"slot": 1, "t": 100})
        self.d.scene_log()
        st, j = self.seek({"t": 250})
        self.assertEqual((st, j), (200, {"ok": True, "result": "applied", "reason": "", "hash": fnv1a(A)}))  # de hash van de vorige scene
        self.assertEqual(self.d.scene_log(), ["seek|-1|0|250|-"])  # slot -1, geen data, n 0
        self.assertEqual(self.running(), {"slot": 1, "h": fnv1a(A), "until": None, "seek": 250})

    def test_t_only_after_a_direct_seek_keeps_slot_zero(self):
        self.seek_ok({"data": b64(B), "t": 100})
        self.d.scene_log()
        self.seek_ok({"t": 7})
        self.assertEqual(self.running(), {"slot": 0, "h": fnv1a(B), "until": None, "seek": 7})
        self.assertEqual(self.d.scene_log(), ["seek|-1|0|7|-"])

    def test_t_only_chain(self):
        self.store(3, B)
        self.seek_ok({"slot": 3, "t": 0})
        for t in range(0, 1000, 100):
            self.seek_ok({"t": t})
        self.assertEqual(self.running(), {"slot": 3, "h": fnv1a(B), "until": None, "seek": 900})

    def test_t_only_after_play_stop_or_idle_is_rejected(self):
        self.store(1, A)
        self.assertResult(self.d.sc("play", {"slot": 1}), "applied")
        self.assertResult(self.seek({"t": 5}), "rejected", "geen scène om te tonen")  # een spelende scene is geen seek-beeld
        self.seek_ok({"slot": 1, "t": 5})
        self.assertResult(self.d.sc("stop", {}), "applied")
        self.assertResult(self.seek({"t": 5}), "rejected", "geen scène om te tonen")
        self.seek_ok({"slot": 1, "t": 5})
        self.d.scene_idle()
        self.assertResult(self.seek({"t": 5}), "rejected", "geen scène om te tonen")

    def test_t_only_after_reboot_is_rejected(self):
        self.store(1, A)
        self.seek_ok({"slot": 1, "t": 5})
        self.d.cmd("reboot")
        self.assertResult(self.seek({"t": 5}), "rejected", "geen scène om te tonen")

    def test_t_only_after_a_rejected_first_seek_is_rejected(self):
        self.store(1, A)
        self.d.cmd("scene_cfg", "seek", "rejected", "nee")
        self.assertResult(self.seek({"slot": 1, "t": 5}), "rejected", "nee")
        self.d.cmd("scene_cfg", "seek", "applied", "")
        self.assertResult(self.seek({"t": 5}), "rejected", "geen scène om te tonen")

    def test_t_only_syntax_still_checked(self):
        self.store(1, A)
        self.seek_ok({"slot": 1, "t": 5})
        self.assertErr(self.seek({}), 422, "bad_value")
        self.assertErr(self.seek({"t": -1}), 422, "bad_value")
        self.assertEqual(self.running()["seek"], 5)  # een fout verplaatst niets

    def test_unknown_keys_and_whitespace(self):
        self.store(1, A)
        self.assertResult(self.seek(raw=' { "x" : 1 , "slot" : 1 , "t" : 42 } '), "applied")
        self.assertEqual(self.running()["seek"], 42)
        self.assertResult(self.seek(raw='{\n"t"\t:\r\n43}'), "applied")
        self.assertEqual(self.running()["seek"], 43)

    def test_t_inside_a_string_value_is_not_the_key(self):
        self.store(1, A)
        self.assertErr(self.seek({"x": '"t":5', "slot": 1}), 422, "bad_value")
        self.assertResult(self.seek({"x": '"t":5', "slot": 1, "t": 6}), "applied")
        self.assertEqual(self.running()["seek"], 6)


class TestT(SeekBase):
    def setUp(self):
        super().setUp()
        self.store(1, A)

    def test_edges(self):
        for t in (0, 1, 86399999, 86400000):
            self.assertResult(self.seek({"slot": 1, "t": t}), "applied")
            self.assertEqual(self.running()["seek"], t)

    def test_out_of_range(self):
        for t in (86400001, -1, 100000000, 4294967296, 4294967297, 1e15, -86400000):
            self.assertErr(self.seek({"slot": 1, "t": t}), 422, "bad_value")

    def test_not_an_integer(self):
        for raw in ('1.5', '0.5', '-0.5', '"100"', '"1.5"', 'null', 'true', 'false', '[1]', '{}', '""', '1e400', 'NaN', 'Infinity', '0x10', '+', '-'):
            st, j = self.seek(raw='{"slot":1,"t":%s}' % raw)
            self.assertEqual((st, j.get("error")), (422, "bad_value"), raw)

    def test_integral_floats_are_fine(self):
        for raw, want in (("100.0", 100), ("1e3", 1000), ("1E3", 1000), ("2.5e3", 2500), ("0.0", 0), ("-0", 0), ("8.64e7", 86400000)):
            self.assertResult(self.seek(raw='{"slot":1,"t":%s}' % raw), "applied")
            self.assertEqual(self.running()["seek"], want, raw)

    def test_missing(self):
        for obj in ({"slot": 1}, {}, {"slot": 1, "T": 5}, {"slot": 1, "tt": 5}, {"slot": 1, "time": 5}):
            self.assertErr(self.seek(obj), 422, "bad_value")
        self.assertErr(self.seek(raw=""), 422, "bad_value")
        self.assertErr(self.seek(raw="niet json"), 422, "bad_value")

    def test_errors_never_reach_the_device(self):
        self.d.scene_log()
        for t in (-1, 86400001, "1", 1.5, None):
            self.seek({"slot": 1, "t": t})
        self.assertEqual(self.d.scene_log(), [])
        self.assertIsNone(self.running())

    def test_t_is_passed_exactly(self):
        for t in (0, 1, 123456, 86400000):
            self.d.scene_log()
            self.seek_ok({"slot": 1, "t": t})
            self.assertEqual(self.d.scene_log(), ["seek|1|3|%d|%s" % (t, fnv1a(A))])


class TestBodyLimit(SeekBase):
    def test_limit_is_the_same_as_the_other_endpoints(self):
        self.store(1, A)
        base = json.dumps({"slot": 1, "t": 5})
        limit = 1280  # ceil(768/3)*4 + 256
        ok_body = base + " " * (limit - len(base))
        self.assertEqual(self.seek(raw=ok_body)[1]["result"], "applied")
        self.assertErr(self.seek(raw=ok_body + " "), 422, "too_large")

    def test_limit_follows_max_bytes(self):
        self.d.scenes_on(8, 10, 8, "ledscene/1")
        self.d.cmd("seek_on")
        base = json.dumps({"data": b64(b"\x01"), "t": 5})
        ok_body = base + " " * (272 - len(base))
        self.assertEqual(self.seek(raw=ok_body)[1]["result"], "applied")
        self.assertErr(self.seek(raw=ok_body + " "), 422, "too_large")

    def test_body_limit_applies_before_parsing(self):
        self.d.scene_log()
        self.assertErr(self.seek(raw="{" * 5000), 422, "too_large")  # zou als syntaxfout anders bad_value geven
        self.assertEqual(self.d.scene_log(), [])

    def test_64kb_body_under_sanitizers(self):
        w = self.d.writes()
        body = json.dumps({"data": "QUJD" * 16384, "t": 1})
        self.assertGreater(len(body), 65536)
        self.assertErr(self.seek(raw=body), 422, "too_large")
        self.assertErr(self.seek(raw=json.dumps({"slot": 1, "data": "QUJD" * 16384, "t": 1})), 422, "too_large")
        self.assertErr(self.seek(raw="A" * 65536), 422, "too_large")
        self.assertErr(self.seek(raw=json.dumps({"t": "9" * 65536})), 422, "too_large")
        self.assertEqual(self.d.writes(), w)
        self.assertEqual(self.d.scene_log(), [])
        self.d.cmd("claimed")  # de driver leeft nog (ASAN)

    def test_64kb_body_without_claim_is_403(self):
        self.d.cmd("resetclaim")
        self.assertErr(self.seek(raw=json.dumps({"data": "QUJD" * 16384, "t": 1})), 403, "not_claimed")


class TestDeviceAnswers(SeekBase):
    def setUp(self):
        super().setUp()
        self.store(1, A)

    def test_rejected_by_the_device(self):
        self.d.cmd("scene_cfg", "seek", "rejected", "strip uit")
        st, j = self.seek({"slot": 1, "t": 5})
        self.assertEqual((st, j), (200, {"ok": False, "result": "rejected", "reason": "strip uit"}))  # geen hash bij rejected
        self.assertIsNone(self.running())

    def test_rejected_move_keeps_the_previous_hold(self):
        self.seek_ok({"slot": 1, "t": 5})
        self.d.cmd("scene_cfg", "seek", "rejected", "nee")
        self.assertResult(self.seek({"slot": 1, "t": 9}), "rejected", "nee")
        self.assertEqual(self.running(), {"slot": 1, "h": fnv1a(A), "until": None, "seek": 5})
        self.assertResult(self.seek({"t": 9}), "rejected", "nee")
        self.assertEqual(self.running()["seek"], 5)

    def test_queued_counts_as_showing(self):
        self.d.cmd("scene_cfg", "seek", "queued", "")
        st, j = self.seek({"slot": 1, "t": 5})
        self.assertEqual((st, j["result"], j["ok"], j["hash"]), (200, "queued", True, fnv1a(A)))
        self.assertEqual(self.running()["seek"], 5)

    def test_device_calling_scene_idle_during_the_call_wins(self):
        self.d.cmd("scene_cfg", "seekhook", "idle")
        self.assertResult(self.seek({"slot": 1, "t": 5}), "applied")
        self.assertIsNone(self.running())
        self.assertResult(self.seek({"t": 5}), "rejected", "geen scène om te tonen")

    def test_device_calling_scene_running_during_the_call_wins(self):
        self.d.cmd("scene_cfg", "seekhook", "running")
        self.assertResult(self.seek({"slot": 1, "t": 5}), "applied")
        self.assertEqual(self.running(), {"slot": 7, "h": "deadbeef", "until": None})  # wat het device meldt, zonder `seek`
        self.assertResult(self.seek({"t": 5}), "rejected", "geen scène om te tonen")

    def test_device_hook_during_a_move_removes_the_hold(self):
        self.seek_ok({"slot": 1, "t": 5})
        self.d.cmd("scene_cfg", "seekhook", "idle")
        self.assertResult(self.seek({"t": 6}), "applied")
        self.assertIsNone(self.running())


class TestRunState(SeekBase):
    def setUp(self):
        super().setUp()
        self.store(1, A)
        self.store(2, B)

    def test_seek_replaces_a_playing_scene(self):
        self.d.sc("play", {"slot": 1})
        self.d.scene_log()
        self.seek_ok({"slot": 1, "t": 40})  # dezelfde scene, maar nu vastgehouden: geen no-op
        self.assertEqual(self.d.scene_log(), ["seek|1|3|40|" + fnv1a(A)])
        self.assertEqual(self.running(), {"slot": 1, "h": fnv1a(A), "until": None, "seek": 40})

    def test_play_of_the_same_scene_ends_the_hold(self):
        self.seek_ok({"slot": 1, "t": 40})
        self.d.scene_log()
        self.assertResult(self.d.sc("play", {"slot": 1}), "applied")
        self.assertEqual(self.d.scene_log(), ["play|1|3|0|" + fnv1a(A)])  # speelt echt: het is geen no-op meer
        self.assertEqual(self.running(), {"slot": 1, "h": fnv1a(A), "until": None})

    def test_play_of_another_scene_ends_the_hold(self):
        self.seek_ok({"slot": 1, "t": 40})
        self.assertResult(self.d.sc("play", {"slot": 2}), "applied")
        self.assertEqual(self.running(), {"slot": 2, "h": fnv1a(B), "until": None})

    def test_play_direct_of_the_same_bytes_ends_the_hold(self):
        self.seek_ok({"data": b64(A), "t": 40})
        self.d.scene_log()
        self.assertResult(self.d.sc("play", {"data": b64(A)}), "applied")
        self.assertEqual(self.d.scene_log(), ["check|3|" + fnv1a(A), "play|0|3|0|" + fnv1a(A)])  # speelt echt, dus ook de check weer
        self.assertEqual(self.running(), {"slot": 0, "h": fnv1a(A), "until": None})

    def test_rejected_play_keeps_the_hold(self):
        self.seek_ok({"slot": 1, "t": 40})
        self.d.cmd("scene_cfg", "play", "rejected", "nee")
        self.assertResult(self.d.sc("play", {"slot": 2}), "rejected", "nee")
        self.assertEqual(self.running()["seek"], 40)

    def test_stop_ends_the_hold(self):
        self.seek_ok({"slot": 1, "t": 40})
        self.d.scene_log()
        self.assertResult(self.d.sc("stop", {}), "applied")
        self.assertEqual(self.d.scene_log(), ["stop|0"])
        self.assertIsNone(self.running())

    def test_stop_with_the_slot_ends_the_hold(self):
        self.seek_ok({"slot": 1, "t": 40})
        self.assertResult(self.d.sc("stop", {"slot": 1}), "applied")
        self.assertIsNone(self.running())

    def test_stop_of_another_slot_leaves_the_hold(self):
        self.seek_ok({"slot": 1, "t": 40})
        self.assertResult(self.d.sc("stop", {"slot": 2}), "applied")
        self.assertEqual(self.running()["seek"], 40)

    def test_refused_stop_keeps_the_hold(self):
        self.seek_ok({"slot": 1, "t": 40})
        self.d.cmd("scene_cfg", "stop", "rejected", "nee")
        self.assertResult(self.d.sc("stop", {}), "rejected", "nee")
        self.assertEqual(self.running()["seek"], 40)

    def test_scene_idle_from_the_device_ends_the_hold(self):
        self.seek_ok({"slot": 1, "t": 40})
        self.d.scene_idle()
        self.assertIsNone(self.running())
        self.assertNotIn("run", self.d.scene_state())

    def test_scene_running_from_the_device_replaces_the_hold(self):
        self.seek_ok({"slot": 1, "t": 40})
        self.d.scene_running(2, fnv1a(B), NOW + 60)
        self.assertEqual(self.running(), {"slot": 2, "h": fnv1a(B), "until": NOW + 60})

    def test_the_header_never_ends_the_hold_by_itself(self):
        self.seek_ok({"slot": 1, "t": 40})
        for step in (1000, 299000, 400000, 3600000):  # ruim voorbij de 300 s van het device
            self.d.advance(step)
            self.d.settime(NOW + step // 1000)
            self.d.loop()
        self.d.settime(NOW + 100000)
        self.d.loop()
        self.assertEqual(self.running(), {"slot": 1, "h": fnv1a(A), "until": None, "seek": 40})

    def test_scheduled_start_replaces_the_hold(self):
        self.assertResult(self.sched([(2, NOW + 100)]), "applied")
        self.seek_ok({"slot": 1, "t": 40})
        self.d.scene_log()
        self.d.settime(NOW + 100)
        self.d.loop()
        self.assertEqual(self.d.scene_log(), ["play|2|5|0|" + fnv1a(B)])
        self.assertEqual(self.running(), {"slot": 2, "h": fnv1a(B), "until": None})

    def test_scheduled_start_of_the_held_scene_plays_for_real(self):
        self.assertResult(self.sched([(1, NOW + 100)]), "applied")
        self.seek_ok({"slot": 1, "t": 40})
        self.d.scene_log()
        self.d.settime(NOW + 100)
        self.d.loop()
        self.assertEqual(self.d.scene_log(), ["play|1|3|0|" + fnv1a(A)])
        self.assertEqual(self.running(), {"slot": 1, "h": fnv1a(A), "until": None})

    def test_a_rejected_scheduled_start_keeps_the_hold(self):
        self.assertResult(self.sched([(2, NOW + 100)]), "applied")
        self.seek_ok({"slot": 1, "t": 40})
        self.d.cmd("scene_cfg", "play", "rejected", "te laat")
        self.d.settime(NOW + 100)
        self.d.loop()
        self.assertEqual(self.running()["seek"], 40)

    def test_store_on_the_held_slot_leaves_the_hold_alone(self):
        self.seek_ok({"slot": 1, "t": 40})
        self.d.scene_log()
        self.assertResult(self.store(1, B), "applied")
        self.assertEqual(self.running(), {"slot": 0, "h": fnv1a(A), "until": None, "seek": 40})  # zoals bij een spelende scene: slot 0
        self.assertEqual(self.d.scene_log(), ["check|5|" + fnv1a(B)])
        self.assertResult(self.seek({"t": 41}), "applied")  # het device houdt zijn eigen kopie vast
        self.assertEqual(self.running()["seek"], 41)

    def test_wiping_the_held_slot_sets_run_slot_to_zero(self):
        self.seek_ok({"slot": 1, "t": 40})
        self.assertResult(self.store(1, b""), "applied")
        self.assertEqual(self.running(), {"slot": 0, "h": fnv1a(A), "until": None, "seek": 40})

    def test_unclaim_keeps_the_hold_with_slot_zero(self):
        self.seek_ok({"slot": 1, "t": 40})
        self.d.cmd("resetclaim")
        self.assertEqual(self.d.state()[1]["scenes"]["run"], {"slot": 0, "h": fnv1a(A), "until": None, "seek": 40})

    def test_reboot_forgets_the_hold(self):
        self.seek_ok({"slot": 1, "t": 40})
        self.d.cmd("reboot")
        self.assertIsNone(self.running())

    def test_state_stays_under_4k_with_everything(self):
        for s in range(1, 9):
            self.assertResult(self.store(s, bytes([s]) * 700), "applied")
        self.assertResult(self.sched([(s, NOW + 100 * s) for s in range(1, 9)]), "applied")
        self.seek_ok({"slot": 8, "t": 86400000})
        txt, s = self.d.state(TOKEN)
        self.assertLessEqual(len(txt.encode()), 4096)
        self.assertEqual(s["scenes"]["run"]["seek"], 86400000)


class TestNoFlashAndNoSched(SeekBase):
    def test_no_nvs_writes_at_all(self):
        self.store(1, A)
        self.sched([(1, NOW + 500), (1, NOW + 900)])
        w = self.d.writes()
        before = self.d.prefs_get("huisdev_sc", "sched")
        self.assertIsNotNone(before)
        for i in range(30):  # een schuif: tot ~10 verzoeken per seconde, hier zonder dat de tijd verstrijkt
            self.seek_ok({"slot": 1, "t": i * 100} if i % 3 == 0 else {"t": i * 100})
        self.seek_ok({"data": b64(B), "t": 5})
        self.seek_ok({"t": 6})
        self.assertEqual(self.d.writes(), w)
        self.assertEqual(self.d.prefs_get("huisdev_sc", "sched"), before)
        self.assertEqual(self.d.prefs_get("huisdev_sc", "s1"), A)

    def test_rejected_seeks_write_nothing_either(self):
        self.store(1, A)
        w = self.d.writes()
        self.seek({"slot": 2, "t": 5})
        self.seek({"slot": 1, "t": -1})
        self.seek({"t": 5})
        self.d.cmd("scene_cfg", "seek", "rejected", "nee")
        self.seek({"slot": 1, "t": 5})
        self.assertEqual(self.d.writes(), w)

    def test_sched_in_the_state_is_unchanged(self):
        self.store(1, A)
        self.store(2, B)
        self.assertResult(self.sched([(2, NOW + 900), (1, NOW + 300)]), "applied")
        want = [(1, NOW + 300), (2, NOW + 900)]
        self.assertEqual(self.pending(), want)
        self.seek_ok({"slot": 1, "t": 10})
        self.seek_ok({"data": b64(B), "t": 10})
        self.seek_ok({"t": 20})
        self.assertEqual(self.pending(), want)
        self.d.sc("stop", {})
        self.assertEqual(self.pending(), want)

    def test_seek_neither_needs_nor_restarts_the_write_interval(self):
        self.store(1, A)  # stempelt het schrijfinterval
        self.seek({"slot": 1, "t": 5})
        st, j = self.d.sc("store", {"slot": 2, "data": b64(B)})
        self.assertEqual(j["reason"], "te vaak")  # nog binnen 2 s van de store
        self.d.advance(1500)
        self.seek_ok({"slot": 1, "t": 6})
        self.d.advance(600)  # in totaal 2100 ms na de store: de seek heeft de teller niet opnieuw gestart
        self.assertResult(self.d.sc("store", {"slot": 2, "data": b64(B)}), "applied")

    def test_seek_right_after_a_store_is_not_too_often(self):
        self.store(1, A)
        self.assertResult(self.seek({"slot": 1, "t": 5}), "applied")  # geen "te vaak"
        self.assertResult(self.seek({"slot": 1, "t": 6}), "applied")

    def test_seek_while_the_clock_is_invalid_and_with_an_empty_schedule(self):
        self.d.settime(0)
        self.assertResult(self.seek({"data": b64(A), "t": 5}), "applied")
        self.assertEqual(self.pending(), [])


if __name__ == "__main__":
    unittest.main()
