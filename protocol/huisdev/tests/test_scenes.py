"""Scenes (HUISDEV.md §16, protocol 1.1): descriptor en rev, state, de vier endpoints, opslag en geplande starts."""
import json
import re
import unittest

from huisdevtest import Base, Dev, TOKEN, b64, fnv1a, rev_body

NOW = 1760000000
# De rev van het testdevice zonder scenes onder header 1.0.2 (vastgelegd in fixtures/descriptor.json en descriptor_rev2.json):
# een device zonder dev.scenes(...) moet die houden.
REV_1_0_2 = "f64cf2a6"
REV2_1_0_2 = "211dec90"
SCENES_OBJ = '"scenes":{"formats":["ledscene/1"],"slots":8,"max_bytes":768,"sched":8}'

A = bytes([1, 2, 3])
B = bytes([9, 8, 7, 6, 5])
C = bytes(range(100))


def packed(entries):
    out = b""
    for slot, at in entries:
        out += bytes([slot]) + at.to_bytes(4, "little")
    return out


class SceneBase(Base):
    """Een geclaimd device met scenes (8 slots, 768 bytes, 8 geplande starts) en een geldige klok."""

    def setUp(self):
        super().setUp()
        self.d.scenes_on()
        self.d.claim()
        self.d.settime(NOW)

    def store(self, slot, data, advance=True):
        if advance:
            self.d.advance(2500)  # minimuminterval tussen schrijfacties
        return self.d.sc("store", {"slot": slot, "data": b64(data)})

    def sched(self, entries, advance=True):
        if advance:
            self.d.advance(2500)
        return self.d.sc("schedule", {"entries": [{"slot": s, "at": a} for s, a in entries]})

    def have(self):
        return self.d.scene_state()["have"]

    def running(self):
        return self.d.scene_state().get("run")

    def pending(self):
        return [(e["slot"], e["at"]) for e in self.d.scene_state()["sched"]]


class TestWithoutScenes(Base):
    """Een device zonder dev.scenes(...) verandert niet (HUISDEV.md §16, eerste alinea)."""

    def test_rev_descriptor_and_state_unchanged(self):
        txt, j = self.d.descriptor()
        self.assertEqual(j["rev"], REV_1_0_2)
        self.assertNotIn("scenes", j)
        self.assertEqual(rev_body(txt), txt[txt.index('"points"'):-1])
        self.assertFalse(rev_body(txt).endswith("}}"))  # eindigt op actions, geen scenes
        self.d.claim()
        _, s = self.d.state(TOKEN)
        self.assertEqual(set(s), {"proto", "rev", "up", "points", "actions"})
        self.assertEqual(s["rev"], REV_1_0_2)

    def test_rev_after_dynamic_change_unchanged(self):
        self.d.cmd("addpoint", "chlorine", "Chloor", "float")
        self.d.cmd("unit", "chlorine", "ppm")
        self.d.cmd("setnum", "chlorine", "1.25")
        self.d.cmd("remove", "cell_voltage")
        self.d.cmd("unit", "temp", "°F")
        self.assertEqual(self.d.rev(), REV2_1_0_2)

    def test_scene_paths_do_not_exist(self):
        self.d.claim()
        for name in ("store", "play", "stop", "schedule"):
            for method in ("POST", "GET"):
                self.assertErr(self.d.jh(method, "/huisdev/scene/" + name, TOKEN, "{}"), 404, "unknown_path")
        self.assertErr(self.d.jh("POST", "/huisdev/scene/store", "", "{}"), 404, "unknown_path")  # ook zonder claim: het pad bestaat niet

    def test_resetclaim_does_not_touch_scene_storage(self):
        self.d.claim()
        self.d.cmd("resetclaim")
        self.assertIsNone(self.d.prefs_get("huisdev_sc", "sched"))
        self.assertEqual(self.d.cmd("claimed"), ["0"])

    def test_scenes_object_without_scenes_is_byte_identical(self):
        """Hetzelfde device met scenes geeft, na weglaten van het scenes-object en de rev, exact de tekst van het device zonder."""
        txt_plain, _ = self.d.descriptor()
        other = Dev()
        try:
            other.cmd("factory")
            other.scenes_on()
            txt, j = other.descriptor()
        finally:
            other.close()
        self.assertIn("," + SCENES_OBJ, txt)
        stripped = txt.replace("," + SCENES_OBJ, "").replace('"rev":"%s"' % j["rev"], '"rev":"%s"' % REV_1_0_2)
        self.assertEqual(stripped, txt_plain)


class TestDescriptorAndState(SceneBase):
    def test_scenes_object_follows_actions(self):
        txt, j = self.d.descriptor()
        self.assertEqual(list(j)[-1], "scenes")
        self.assertEqual(list(j)[-2], "actions")
        self.assertTrue(txt.endswith("]," + SCENES_OBJ + "}"), txt[-200:])
        self.assertEqual(j["scenes"], {"formats": ["ledscene/1"], "slots": 8, "max_bytes": 768, "sched": 8})
        self.assertLessEqual(len(txt.encode()), 8192)

    def test_rev_covers_scenes(self):
        txt, j = self.d.descriptor()
        self.assertEqual(j["rev"], fnv1a(rev_body(txt).encode("utf-8")))
        self.assertTrue(rev_body(txt).endswith(SCENES_OBJ))
        self.assertNotEqual(j["rev"], REV_1_0_2)
        _, s = self.d.state(TOKEN)
        self.assertEqual(s["rev"], j["rev"])

    def test_rev_stable_after_reboot_and_changes_with_parameters(self):
        rev = self.d.rev()
        self.d.cmd("reboot")
        self.assertEqual(self.d.rev(), rev)
        self.d.cmd("reboot")
        self.assertEqual(self.d.rev(), rev)
        self.d.scenes_on(8, 768, 8, "ledscene/1")  # dezelfde aanroep nog eens: geen wijziging
        self.assertEqual(self.d.rev(), rev)
        self.d.scenes_on(4, 768, 8, "ledscene/1")
        rev2 = self.d.rev()
        self.assertNotEqual(rev2, rev)
        self.d.scenes_on(4, 768, 8, "ledscene/1", "ledscene/2")
        self.assertNotEqual(self.d.rev(), rev2)

    def test_new_rev_is_announced(self):
        self.d.loop()
        self.d.udp_out()
        self.d.scenes_on(2, 100, 2, "x/1")
        self.d.loop()
        out = self.d.udp_out()
        self.assertEqual(len(out), 1)
        self.assertEqual(json.loads(out[0][1])["rev"], self.d.rev())

    def test_limits_are_clamped(self):
        cases = [
            ((99, 5000, 99), (8, 768, 8)),    # slots*max_bytes <= 6144: max_bytes wordt verlaagd
            ((0, 0, -1), (1, 1, 0)),
            ((6, 2000, 3), (6, 1024, 3)),
            ((8, 1024, 8), (8, 768, 8)),
            ((3, 1024, 0), (3, 1024, 0)),
            ((4, 10, 4), (4, 10, 4)),
        ]
        for given, want in cases:
            self.d.scenes_on(*given)
            sc = self.d.descriptor()[1]["scenes"]
            self.assertEqual((sc["slots"], sc["max_bytes"], sc["sched"]), want, given)
            self.assertLessEqual(sc["slots"] * sc["max_bytes"], 6144)

    def test_formats_are_escaped_and_listed(self):
        self.d.scenes_on(2, 100, 2, 'a"b', "ledscene/1")
        self.assertEqual(self.d.descriptor()[1]["scenes"]["formats"], ['a"b', "ledscene/1"])
        self.d.scenes_on(2, 100, 2, "-")  # "-" = lege lijst (driver)
        self.assertEqual(self.d.descriptor()[1]["scenes"]["formats"], [])

    def test_initial_state(self):
        self.d.settime(0)
        txt, s = self.d.state(TOKEN)
        self.assertEqual(s["scenes"], {"have": {}, "sched": [], "time": None})
        self.assertTrue(txt.endswith('"scenes":{"have":{},"sched":[],"time":null}}'))
        self.assertEqual(list(s)[-1], "scenes")

    def test_time_validity(self):
        for t, want in ((0, None), (1, None), (1700000000, None), (1700000001, 1700000001), (NOW, NOW)):
            self.d.settime(t)
            self.assertEqual(self.d.scene_state()["time"], want, t)
        self.d.settime(NOW)
        r1 = self.d.rev()
        self.d.settime(NOW + 5)
        self.assertEqual(self.d.rev(), r1)  # time verandert elke poll en is geen descriptorwijziging

    def test_have_run_sched_state_shape(self):
        self.assertResult(self.store(1, A), "applied")
        self.assertResult(self.store(3, B), "applied")
        self.d.sc("play", {"slot": 1})
        self.assertResult(self.sched([(3, NOW + 600), (1, NOW + 300)]), "applied")
        sc = self.d.scene_state()
        self.assertEqual(sc["have"], {"1": {"h": fnv1a(A), "n": 3}, "3": {"h": fnv1a(B), "n": 5}})
        self.assertEqual(list(sc["have"]), ["1", "3"])
        self.assertEqual(sc["run"], {"slot": 1, "h": fnv1a(A), "until": None})
        self.assertEqual(sc["sched"], [{"slot": 1, "at": NOW + 300}, {"slot": 3, "at": NOW + 600}])
        self.assertEqual(sc["time"], NOW)
        self.assertRegex(sc["have"]["1"]["h"], r"^[0-9a-f]{8}$")

    def test_state_stays_under_4k_when_full(self):
        for s in range(1, 9):
            self.assertResult(self.store(s, bytes([s]) * 700), "applied")
        self.d.scene_running(8, "ffffffff", NOW + 100)
        self.assertResult(self.sched([(s, NOW + 100 * s) for s in range(1, 9)]), "applied")
        txt, s = self.d.state(TOKEN)
        self.assertEqual(len(s["scenes"]["have"]), 8)
        self.assertEqual(len(s["scenes"]["sched"]), 8)
        self.assertLessEqual(len(txt.encode()), 4096)

    def test_run_until_needs_a_clock(self):
        self.d.scene_running(1, "a1b2c3d4", NOW + 600)
        self.assertEqual(self.running(), {"slot": 1, "h": "a1b2c3d4", "until": NOW + 600})
        self.d.settime(0)
        self.assertEqual(self.running(), {"slot": 1, "h": "a1b2c3d4", "until": None})
        self.d.settime(NOW)
        self.d.scene_running(0, "a1b2c3d4", 0)
        self.assertEqual(self.running(), {"slot": 0, "h": "a1b2c3d4", "until": None})
        self.d.scene_idle()
        self.assertNotIn("run", self.d.scene_state())

    def test_run_ends_by_itself_when_until_is_reached(self):
        self.assertResult(self.store(1, A), "applied")
        self.d.scene_running(1, fnv1a(A), NOW + 60)
        self.d.settime(NOW + 59)
        self.d.loop()
        self.assertIsNotNone(self.running())
        self.d.settime(NOW + 60)
        self.d.loop()
        self.assertIsNone(self.running())
        self.d.scene_log()
        self.assertResult(self.d.sc("play", {"slot": 1}), "applied")  # speelt weer echt (geen no-op van een verlopen run)
        self.assertEqual(self.d.scene_log(), ["play|1|3|0|" + fnv1a(A)])

    def test_scenes_state_is_reported_for_the_unclaimed_device_too(self):
        self.d.cmd("resetclaim")
        _, s = self.d.state()
        self.assertEqual(s["scenes"], {"have": {}, "sched": [], "time": NOW})


class TestAuthAndRouting(SceneBase):
    PATHS = ("store", "play", "stop", "schedule")

    def test_wrong_methods_give_405(self):
        for name in self.PATHS:
            for m in ("GET", "PUT", "DELETE", "HEAD", "PATCH"):
                self.assertErr(self.d.jh(m, "/huisdev/scene/" + name, TOKEN, "{}"), 405, "method")

    def test_405_comes_before_the_claim_check(self):
        self.d.cmd("resetclaim")
        self.assertErr(self.d.jh("GET", "/huisdev/scene/store", "", ""), 405, "method")

    def test_not_claimed_is_403(self):
        self.d.cmd("resetclaim")
        for name in self.PATHS:
            self.assertErr(self.d.sc(name, {}, token=""), 403, "not_claimed")
            self.assertErr(self.d.sc(name, {}, token=TOKEN), 403, "not_claimed")

    def test_wrong_token_is_401(self):
        for name in self.PATHS:
            self.assertErr(self.d.sc(name, {}, token="fout"), 401, "token")
            self.assertErr(self.d.sc(name, {}, token=""), 401, "token")
            self.assertErr(self.d.sc(name, {}, token=TOKEN[:-1]), 401, "token")
            self.assertErr(self.d.sc(name, {}, token=TOKEN + "x"), 401, "token")

    def test_auth_failures_change_nothing(self):
        w = self.d.writes()
        self.assertErr(self.d.sc("store", {"slot": 1, "data": b64(A)}, token="fout"), 401, "token")
        self.assertErr(self.d.sc("schedule", {"entries": []}, token="fout"), 401, "token")
        self.assertEqual(self.d.writes(), w)
        self.assertEqual(self.have(), {})
        self.assertEqual(self.d.scene_log(), [])

    def test_unknown_scene_path(self):
        self.assertErr(self.d.jh("POST", "/huisdev/scene/nope", TOKEN, "{}"), 404, "unknown_path")
        self.assertErr(self.d.jh("POST", "/huisdev/scene/", TOKEN, "{}"), 404, "unknown_path")
        self.assertErr(self.d.jh("POST", "/huisdev/scene", TOKEN, "{}"), 404, "unknown_path")

    def test_through_the_webserver_adapter(self):
        r = self.d.cmd("ws_call", "POST", "/huisdev/scene/store", TOKEN, json.dumps({"slot": 2, "data": b64(A)}))
        self.assertEqual(r[0], "200")
        self.assertEqual(r[2], "no-store")
        self.assertEqual(json.loads(r[3])["hash"], fnv1a(A))
        r = self.d.cmd("ws_call", "GET", "/huisdev/scene/store", TOKEN, "")
        self.assertEqual((r[0], r[4]), ("405", "routed"))
        r = self.d.cmd("ws_call", "POST", "/huisdev/scene/store", "fout", "{}")
        self.assertEqual(r[0], "401")


class TestStore(SceneBase):
    def test_store_applied_with_hash(self):
        st, j = self.store(1, A)
        self.assertEqual((st, j), (200, {"ok": True, "result": "applied", "reason": "", "hash": fnv1a(A)}))
        self.assertEqual(self.have(), {"1": {"h": fnv1a(A), "n": 3}})
        self.assertEqual(self.d.prefs_get("huisdev_sc", "s1"), A)
        self.assertEqual(self.d.scene_log(), ["check|3|" + fnv1a(A)])

    def test_response_text(self):
        st, txt = self.d.handle("POST", "/huisdev/scene/store", TOKEN, json.dumps({"slot": 4, "data": b64(B)}))
        self.assertEqual(txt, '{"ok":true,"result":"applied","reason":"","hash":"%s"}' % fnv1a(B))

    def test_have_survives_reboot_and_sceneread_returns_the_bytes(self):
        self.store(1, A)
        self.store(5, C)
        before = self.have()
        self.d.cmd("reboot")
        self.assertEqual(self.have(), before)
        self.assertEqual(self.d.cmd("scene_read", "1"), ["3", A.hex()])
        self.assertEqual(self.d.cmd("scene_read", "5"), ["100", C.hex()])
        self.assertEqual(self.d.cmd("scene_read", "2"), ["0", ""])      # leeg slot
        self.assertEqual(self.d.cmd("scene_read", "1", "2"), ["0", ""])  # buffer te klein
        self.assertEqual(self.d.cmd("scene_read", "9"), ["0", ""])      # slot buiten bereik
        self.assertEqual(self.d.cmd("scene_read", "0"), ["0", ""])

    def test_reboot_keeps_the_rev(self):
        self.store(1, A)
        rev = self.d.rev()
        self.d.cmd("reboot")
        self.assertEqual(self.d.rev(), rev)

    def test_scene_larger_than_max_bytes_in_nvs_is_ignored_after_a_smaller_limit(self):
        self.store(1, C)
        self.d.scenes_on(8, 50, 8, "ledscene/1")
        self.assertEqual(self.have(), {})
        self.d.scenes_on(8, 768, 8, "ledscene/1")
        self.assertEqual(self.have(), {"1": {"h": fnv1a(C), "n": 100}})

    # ---- base64 ----
    def test_base64_padding_variants(self):
        for n in range(1, 12):
            data = bytes((i * 37 + 11) & 255 for i in range(n))
            self.assertResult(self.store(1, data), "applied")
            self.assertEqual(self.d.prefs_get("huisdev_sc", "s1"), data, n)
            self.assertEqual(self.have()["1"], {"h": fnv1a(data), "n": n})

    def test_base64_all_byte_values_and_the_two_special_characters(self):
        data = bytes(range(256))[:256]
        self.assertTrue("+" in b64(bytes([0xFB, 0xEF, 0xBE])) and "/" in b64(bytes([0xFF, 0xFF, 0xFF])))
        for blob in (bytes([0xFB, 0xEF, 0xBE]), bytes([0xFF, 0xFF, 0xFF]), data):
            self.assertResult(self.store(1, blob), "applied")
            self.assertEqual(self.d.prefs_get("huisdev_sc", "s1"), blob)

    def test_invalid_base64_is_bad_data_and_changes_nothing(self):
        self.store(1, A)
        w = self.d.writes()
        bad = [
            "QQ",              # padding ontbreekt
            "QQ=",             # padding te kort
            "QQ===",           # te lang
            "QUJD=",           # lengte geen viervoud
            "Q=Q=", "=AAA", "AA=A", "A===", "====",   # '=' op een verkeerde plaats
            "QQ==QQ==",        # padding in het midden
            "QR==",            # niet-canonieke restbits
            "QUJ=", "QUK=",    # idem (canoniek is "QUI=")
            "QU JD", "QUJD\n", " QUJD", "QUJD ", "QU\nJD", "Q JD", "Q\tJD",   # witruimte
            "QUJ!", "QU-_", "QU_-",                           # ongeldige tekens, URL-safe alfabet
            "QUJD\u00e9", "\u00e9\u00e9\u00e9\u00e9",          # niet-ASCII
        ]
        for data in bad:
            self.d.advance(2500)
            self.assertErr(self.d.sc("store", {"slot": 2, "data": data}), 422, "bad_data")
        self.assertEqual(self.d.writes(), w)
        self.assertEqual(list(self.have()), ["1"])
        self.assertResult(self.store(2, b"AB"), "applied")  # "QUI=" is wel geldig

    def test_data_missing_or_not_a_string_is_bad_data(self):
        for obj in ({"slot": 1}, {"slot": 1, "data": 5}, {"slot": 1, "data": None}, {"slot": 1, "data": True},
                    {"slot": 1, "data": ["QUJD"]}, {"slot": 1, "data": {"a": "QUJD"}}):
            self.assertErr(self.d.sc("store", obj), 422, "bad_data")
        self.assertErr(self.d.sc("store", raw='{"slot":1,"data":"QUJD'), 422, "bad_data")  # niet afgesloten
        self.assertErr(self.d.sc("store", raw='{"slot":1,"data":"QUJD\\'), 422, "bad_data")
        self.assertEqual(self.have(), {})

    def test_escapes_inside_data(self):
        # sommige JSON-schrijvers escapen '/' (PHP) of schrijven tekens als \uXXXX
        self.assertResult(self.d.sc("store", raw='{"slot":1,"data":"\\/\\/\\/\\/"}'), "applied")
        self.assertEqual(self.d.prefs_get("huisdev_sc", "s1"), bytes([255, 255, 255]))
        self.d.advance(2500)
        self.assertResult(self.d.sc("store", raw='{"slot":2,"data":"\\u0051UJD"}'), "applied")
        self.assertEqual(self.d.prefs_get("huisdev_sc", "s2"), b"ABC")
        # een kapotte escape wist niets en schrijft niets
        w = self.d.writes()
        self.assertErr(self.d.sc("store", raw='{"slot":2,"data":"\\u12"}'), 422, "bad_data")
        self.assertEqual(self.d.writes(), w)
        self.assertEqual(self.d.prefs_get("huisdev_sc", "s2"), b"ABC")

    def test_max_bytes_boundary(self):
        self.assertResult(self.store(1, bytes(768)), "applied")
        self.assertEqual(self.have()["1"]["n"], 768)
        w = self.d.writes()
        self.assertErr(self.store(2, bytes(769)), 422, "too_large")
        self.assertErr(self.store(2, bytes(770)), 422, "too_large")
        self.assertErr(self.store(2, bytes(1000)), 422, "too_large")
        self.assertEqual(self.d.writes(), w)
        self.assertNotIn("2", self.have())
        self.assertEqual(self.d.scene_log(), ["check|768|" + fnv1a(bytes(768))])  # te grote scenes bereiken het device niet

    def test_max_bytes_boundary_with_other_limits(self):
        self.d.scenes_on(2, 10, 2, "ledscene/1")
        self.assertResult(self.store(1, bytes(10)), "applied")
        self.assertErr(self.store(1, bytes(11)), 422, "too_large")
        self.assertEqual(self.have()["1"]["n"], 10)
        self.d.scenes_on(2, 1024, 2, "ledscene/1")
        self.assertResult(self.store(1, bytes(1024)), "applied")
        self.assertErr(self.store(1, bytes(1025)), 422, "too_large")

    def test_body_limit_is_ceil_max_bytes_over_3_times_4_plus_256(self):
        base = json.dumps({"slot": 1, "data": b64(b"\x01")})
        for max_bytes, limit in ((768, 1280), (10, 272), (9, 268), (1, 260), (1024, 1624)):
            self.d.scenes_on(8 if max_bytes <= 768 else 4, max_bytes, 8, "ledscene/1")
            self.d.advance(2500)
            ok_body = base + " " * (limit - len(base))
            self.assertEqual(len(ok_body), limit)
            st, j = self.d.sc("store", raw=ok_body)
            self.assertEqual((st, j["result"]), (200, "applied"), (max_bytes, j))
            self.d.advance(2500)
            self.assertErr(self.d.sc("store", raw=ok_body + " "), 422, "too_large")
            for name in ("play", "stop", "schedule"):  # de grens geldt voor alle vier de endpoints
                self.assertErr(self.d.sc(name, raw=ok_body + " "), 422, "too_large")

    def test_body_limit_applies_before_parsing(self):
        w = self.d.writes()
        self.assertErr(self.d.sc("store", raw="{" * 5000), 422, "too_large")  # zou als syntaxfout anders bad_slot geven
        self.assertErr(self.d.sc("play", raw="x" * 1281), 422, "too_large")
        self.assertEqual(self.d.writes(), w)

    def test_64kb_body_under_sanitizers(self):
        w = self.d.writes()
        body = json.dumps({"slot": 1, "data": "QUJD" * 16384})
        self.assertGreater(len(body), 65536)
        for name in ("store", "play", "schedule", "stop"):
            self.assertErr(self.d.sc(name, raw=body), 422, "too_large")
        self.assertErr(self.d.sc("store", raw="A" * 65536), 422, "too_large")
        self.assertEqual(self.d.writes(), w)
        self.assertEqual(self.have(), {})
        self.d.cmd("claimed")  # de driver leeft nog (ASAN)

    def test_64kb_body_without_claim_is_403(self):
        self.d.cmd("resetclaim")
        self.assertErr(self.d.sc("store", raw=json.dumps({"slot": 1, "data": "QUJD" * 16384})), 403, "not_claimed")

    def test_empty_data_wipes_the_slot(self):
        self.store(1, A)
        self.store(2, B)
        self.d.advance(2500)
        st, j = self.d.sc("store", {"slot": 1, "data": ""})
        self.assertEqual((st, j), (200, {"ok": True, "result": "applied", "reason": ""}))  # geen hash bij wissen
        self.assertEqual(list(self.have()), ["2"])
        self.assertIsNone(self.d.prefs_get("huisdev_sc", "s1"))
        self.assertEqual(self.d.prefs_get("huisdev_sc", "s2"), B)
        self.d.cmd("reboot")
        self.assertEqual(list(self.have()), ["2"])

    def test_wiping_an_empty_slot_writes_nothing(self):
        w = self.d.writes()
        self.assertResult(self.d.sc("store", {"slot": 3, "data": ""}), "applied")
        self.assertEqual(self.d.writes(), w)
        self.assertEqual(self.d.scene_log(), [])

    def test_slot_validation(self):
        w = self.d.writes()
        bad = ['0', '9', '-1', '1.5', '"1"', 'true', 'false', 'null', '[1]', '{"a":1}', '1e400', '4294967297', '4294967296.0',
               '1e10', '-0.5', '0.999', '8.5', '""', '"a"']
        for slot in bad:
            self.assertErr(self.d.sc("store", raw='{"slot":%s,"data":"%s"}' % (slot, b64(A))), 422, "bad_slot")
        self.assertErr(self.d.sc("store", {"data": b64(A)}), 422, "bad_slot")           # slot ontbreekt
        self.assertErr(self.d.sc("store", raw=json.dumps({"slot": "1", "data": b64(A)})), 422, "bad_slot")
        self.assertEqual(self.d.writes(), w)
        self.assertEqual(self.have(), {})

    def test_slot_edges_and_integral_floats(self):
        self.assertResult(self.store(8, A), "applied")
        self.d.advance(2500)
        self.assertResult(self.d.sc("store", raw='{"slot":1.0,"data":"%s"}' % b64(B)), "applied")  # 1.0 is een geheel getal
        self.d.advance(2500)
        self.assertResult(self.d.sc("store", raw='{"slot":2e0,"data":"%s"}' % b64(B)), "applied")
        self.assertEqual(sorted(self.have()), ["1", "2", "8"])

    def test_slot_range_follows_the_device(self):
        self.d.scenes_on(3, 100, 2, "ledscene/1")
        self.assertResult(self.store(3, A), "applied")
        self.assertErr(self.store(4, A), 422, "bad_slot")

    def test_slot_is_read_as_a_key_not_from_a_string_value(self):
        # hdJsonStr-valkuil uit 1.0.1: een waarde die op een sleutel lijkt mag de lezer niet misleiden
        self.assertResult(self.d.sc("store", raw=json.dumps({"x": "\"slot\":5", "data": b64(A), "slot": 2})), "applied")
        self.assertEqual(list(self.have()), ["2"])

    def test_key_order_and_whitespace(self):
        self.assertResult(self.d.sc("store", raw='  {\n "data" : "%s" ,\t"slot"\r\n:\n3 }  ' % b64(A)), "applied")
        self.assertEqual(list(self.have()), ["3"])

    # ---- idempotentie, check, vol, interval ----
    def test_identical_store_writes_nothing_and_skips_the_check(self):
        self.store(1, A)
        self.d.scene_log()
        w = self.d.writes()
        for _ in range(3):
            self.assertEqual(self.store(1, A), (200, {"ok": True, "result": "applied", "reason": "", "hash": fnv1a(A)}))
        self.assertEqual(self.d.writes(), w)
        self.assertEqual(self.d.scene_log(), [])
        self.assertResult(self.store(1, B), "applied")  # afwijkend: wel schrijven
        self.assertEqual(self.d.writes(), w + 1)

    def test_identical_bytes_in_another_slot_are_stored(self):
        self.store(1, A)
        w = self.d.writes()
        self.assertResult(self.store(2, A), "applied")
        self.assertEqual(self.d.writes(), w + 1)
        self.assertEqual(self.have()["1"], self.have()["2"])

    def test_same_length_other_bytes_are_stored(self):
        self.store(1, A)
        self.assertResult(self.store(1, bytes([1, 2, 4])), "applied")
        self.assertEqual(self.d.prefs_get("huisdev_sc", "s1"), bytes([1, 2, 4]))

    def test_check_rejection(self):
        self.d.cmd("scene_cfg", "check", "ongeldig formaat")
        st, j = self.store(1, A)
        self.assertEqual((st, j), (200, {"ok": False, "result": "rejected", "reason": "ongeldig formaat"}))
        self.assertEqual(self.have(), {})
        self.assertIsNone(self.d.prefs_get("huisdev_sc", "s1"))
        self.d.cmd("scene_cfg", "check", "")
        self.assertResult(self.store(1, A), "applied")

    def test_check_rejection_leaves_the_old_content(self):
        self.store(1, A)
        self.d.cmd("scene_cfg", "check", "nee")
        self.assertResult(self.store(1, B), "rejected", "nee")
        self.assertEqual(self.have()["1"], {"h": fnv1a(A), "n": 3})
        self.assertEqual(self.d.prefs_get("huisdev_sc", "s1"), A)

    def test_nvs_full_is_rejected_and_have_unchanged(self):
        self.store(1, A)
        used = len(TOKEN) + len(A)  # token (namespace huisdev) + s1
        self.d.cmd("nvs_quota", used + 5)
        st, j = self.store(2, C)  # 100 bytes passen niet
        self.assertEqual((st, j), (200, {"ok": False, "result": "rejected", "reason": "geen geheugen"}))
        self.assertEqual(list(self.have()), ["1"])
        self.assertIsNone(self.d.prefs_get("huisdev_sc", "s2"))
        # een kleine scene past wel (5 bytes)
        self.assertResult(self.store(2, B), "applied")
        # groter maken van een bestaand slot faalt en laat het oude staan
        self.assertResult(self.store(2, C), "rejected", "geen geheugen")
        self.assertEqual(self.have()["2"], {"h": fnv1a(B), "n": 5})
        self.assertEqual(self.d.prefs_get("huisdev_sc", "s2"), B)
        self.d.cmd("nvs_quota", "-1")
        self.assertResult(self.store(2, C), "applied")

    def test_nvs_full_after_reboot_keeps_what_was_stored(self):
        self.store(1, A)
        self.d.cmd("nvs_quota", "0")
        self.assertResult(self.store(2, B), "rejected", "geen geheugen")
        self.d.cmd("reboot")
        self.assertEqual(list(self.have()), ["1"])

    def test_failed_write_does_not_start_the_interval(self):
        self.store(1, A)
        self.d.advance(2500)
        self.d.cmd("nvs_quota", "0")
        self.assertResult(self.d.sc("store", {"slot": 2, "data": b64(B)}), "rejected", "geen geheugen")
        self.d.cmd("nvs_quota", "-1")
        self.assertResult(self.d.sc("store", {"slot": 2, "data": b64(B)}), "applied")  # geen "te vaak": de mislukte poging telt niet

    def test_minimum_interval_between_writes(self):
        self.assertResult(self.store(1, A, advance=False), "applied")
        w = self.d.writes()
        self.d.advance(1999)
        self.assertResult(self.d.sc("store", {"slot": 2, "data": b64(B)}), "rejected", "te vaak")
        self.assertEqual(self.d.writes(), w)
        self.assertNotIn("2", self.have())
        self.d.advance(1)  # precies 2000 ms na de vorige schrijfactie
        self.assertResult(self.d.sc("store", {"slot": 2, "data": b64(B)}), "applied")
        self.assertEqual(self.d.writes(), w + 1)

    def test_interval_applies_to_wipe_and_schedule_but_not_to_identical_content(self):
        self.assertResult(self.store(1, A, advance=False), "applied")
        self.d.advance(100)
        self.assertEqual(self.d.sc("store", {"slot": 1, "data": b64(A)})[1]["result"], "applied")  # identiek: geen schrijfactie
        self.assertResult(self.d.sc("store", {"slot": 1, "data": ""}), "rejected", "te vaak")
        self.assertResult(self.d.sc("schedule", {"entries": [{"slot": 1, "at": NOW + 100}]}), "rejected", "te vaak")
        self.assertResult(self.d.sc("schedule", {"entries": []}), "applied")  # identiek (leeg): geen schrijfactie
        self.assertResult(self.d.sc("play", {"slot": 1}), "applied")           # play en stop schrijven niet
        self.assertResult(self.d.sc("stop", {}), "applied")
        self.d.advance(2000)
        self.assertResult(self.d.sc("schedule", {"entries": [{"slot": 1, "at": NOW + 100}]}), "applied")
        self.assertResult(self.d.sc("store", {"slot": 1, "data": ""}), "rejected", "te vaak")  # de schedule schreef net

    def test_interval_survives_millis_wraparound(self):
        self.d.cmd("millis", "4294966000")
        self.assertResult(self.store(1, A, advance=False), "applied")
        self.d.cmd("millis", "100")  # 1396 ms later, de teller is rond
        self.assertResult(self.d.sc("store", {"slot": 2, "data": b64(B)}), "rejected", "te vaak")
        self.d.cmd("millis", "800")  # 2096 ms later
        self.assertResult(self.d.sc("store", {"slot": 2, "data": b64(B)}), "applied")

    def test_store_on_the_playing_slot_leaves_the_running_scene_alone(self):
        self.store(1, A)
        self.d.sc("play", {"slot": 1})
        self.d.scene_log()
        self.assertEqual(self.running(), {"slot": 1, "h": fnv1a(A), "until": None})
        self.assertResult(self.store(1, B), "applied")
        self.assertEqual(self.running(), {"slot": 0, "h": fnv1a(A), "until": None})  # de lopende scene is nog A, zonder slot
        self.assertEqual(self.have()["1"]["h"], fnv1a(B))
        self.assertNotIn("stop", "".join(self.d.scene_log()))  # het device kreeg geen stop
        # `stop` met slot 1 raakt A dus niet meer (slot 1 speelt niet), `stop` zonder slot wel
        self.assertResult(self.d.sc("stop", {"slot": 1}), "applied")
        self.assertEqual(self.d.scene_log(), [])
        self.assertIsNotNone(self.running())
        # B uit slot 1 afspelen vervangt A
        self.assertResult(self.d.sc("play", {"slot": 1}), "applied")
        self.assertEqual(self.d.scene_log(), ["play|1|5|0|" + fnv1a(B)])
        self.assertEqual(self.running()["h"], fnv1a(B))

    def test_identical_store_on_the_playing_slot_keeps_run_slot(self):
        self.store(1, A)
        self.d.sc("play", {"slot": 1})
        self.assertResult(self.store(1, A), "applied")
        self.assertEqual(self.running()["slot"], 1)

    def test_wiping_the_playing_slot_sets_run_slot_to_zero(self):
        self.store(1, A)
        self.d.sc("play", {"slot": 1})
        self.assertResult(self.store(1, b""), "applied")
        self.assertEqual(self.running(), {"slot": 0, "h": fnv1a(A), "until": None})

    def test_store_on_another_slot_keeps_run_slot(self):
        self.store(1, A)
        self.d.sc("play", {"slot": 1})
        self.store(2, B)
        self.assertEqual(self.running()["slot"], 1)


class TestPlayStop(SceneBase):
    def test_play_slot(self):
        self.store(1, A)
        self.d.scene_log()
        st, j = self.d.sc("play", {"slot": 1})
        self.assertEqual((st, j), (200, {"ok": True, "result": "applied", "reason": "", "hash": fnv1a(A)}))
        self.assertEqual(self.d.scene_log(), ["play|1|3|0|" + fnv1a(A)])
        self.assertEqual(self.running(), {"slot": 1, "h": fnv1a(A), "until": None})

    def test_play_same_hash_again_is_a_noop(self):
        self.store(1, A)
        self.d.scene_log()
        self.d.sc("play", {"slot": 1})
        for _ in range(3):
            self.assertEqual(self.d.sc("play", {"slot": 1}), (200, {"ok": True, "result": "applied", "reason": "", "hash": fnv1a(A)}))
        self.assertEqual(self.d.scene_log(), ["play|1|3|0|" + fnv1a(A)])  # een callback in totaal

    def test_play_identical_bytes_from_another_slot_or_directly_is_a_noop(self):
        self.store(1, A)
        self.store(2, A)
        self.d.sc("play", {"slot": 1})
        self.d.scene_log()
        self.assertResult(self.d.sc("play", {"slot": 2}), "applied")
        self.assertResult(self.d.sc("play", {"data": b64(A)}), "applied")
        self.assertEqual(self.d.scene_log(), [])
        self.assertEqual(self.running()["slot"], 1)

    def test_another_scene_replaces_the_running_one(self):
        self.store(1, A)
        self.store(2, B)
        self.d.sc("play", {"slot": 1})
        self.d.scene_log()
        self.assertResult(self.d.sc("play", {"slot": 2}), "applied")
        self.assertEqual(self.d.scene_log(), ["play|2|5|0|" + fnv1a(B)])
        self.assertEqual(self.running(), {"slot": 2, "h": fnv1a(B), "until": None})

    def test_play_direct_has_slot_zero(self):
        st, j = self.d.sc("play", {"data": b64(A)})
        self.assertEqual((st, j), (200, {"ok": True, "result": "applied", "reason": "", "hash": fnv1a(A)}))
        self.assertEqual(self.d.scene_log(), ["check|3|" + fnv1a(A), "play|0|3|0|" + fnv1a(A)])
        self.assertEqual(self.running(), {"slot": 0, "h": fnv1a(A), "until": None})
        self.assertEqual(self.have(), {})  # rechtstreeks spelen bewaart niets

    def test_play_direct_writes_nothing(self):
        w = self.d.writes()
        self.d.sc("play", {"data": b64(A)})
        self.d.sc("play", {"data": b64(B)})
        self.assertEqual(self.d.writes(), w)

    def test_play_direct_ignores_the_write_interval(self):
        self.store(1, A, advance=False)
        self.assertResult(self.d.sc("play", {"data": b64(B)}), "applied")

    def test_play_empty_slot_is_rejected(self):
        self.assertResult(self.d.sc("play", {"slot": 1}), "rejected", "slot leeg")
        self.assertEqual(self.d.scene_log(), [])
        self.assertIsNone(self.running())

    def test_play_syntax_errors(self):
        self.store(1, A)
        self.d.scene_log()
        for obj in ({"slot": 0}, {"slot": 9}, {"slot": "1"}, {"slot": 1.5}, {"slot": None}, {}):
            self.assertErr(self.d.sc("play", obj), 422, "bad_slot")
        self.assertErr(self.d.sc("play", {"slot": 1, "data": b64(A)}), 422, "bad_value")  # slot of data, niet allebei
        self.assertErr(self.d.sc("play", {"data": "###"}), 422, "bad_data")
        self.assertErr(self.d.sc("play", {"data": ""}), 422, "bad_data")                    # een lege scene is geen scene
        self.assertErr(self.d.sc("play", {"data": 5}), 422, "bad_data")
        self.assertErr(self.d.sc("play", {"data": b64(bytes(769))}), 422, "too_large")
        self.assertEqual(self.d.scene_log(), [])
        self.assertIsNone(self.running())

    def test_play_direct_max_bytes_boundary(self):
        self.assertResult(self.d.sc("play", {"data": b64(bytes([1]) * 768)}), "applied")
        self.assertErr(self.d.sc("play", {"data": b64(bytes([2]) * 769)}), 422, "too_large")

    def test_play_direct_check_rejection(self):
        self.d.cmd("scene_cfg", "check", "ongeldig")
        self.assertResult(self.d.sc("play", {"data": b64(A)}), "rejected", "ongeldig")
        self.assertEqual(self.d.scene_log(), ["check|3|" + fnv1a(A)])
        self.assertIsNone(self.running())

    def test_device_can_reject_play(self):
        self.store(1, A)
        self.d.cmd("scene_cfg", "play", "rejected", "geen tijd")
        st, j = self.d.sc("play", {"slot": 1})
        self.assertEqual((st, j), (200, {"ok": False, "result": "rejected", "reason": "geen tijd"}))
        self.assertIsNone(self.running())
        self.d.cmd("scene_cfg", "play", "applied", "")
        self.assertResult(self.d.sc("play", {"slot": 1}), "applied")  # na een weigering is er niets gemeld, dus geen no-op

    def test_device_can_answer_queued(self):
        self.store(1, A)
        self.d.cmd("scene_cfg", "play", "queued", "")
        self.assertResult(self.d.sc("play", {"slot": 1}), "queued")
        self.assertIsNotNone(self.running())

    def test_rejected_replacement_keeps_the_running_scene_reported(self):
        self.store(1, A)
        self.store(2, B)
        self.d.sc("play", {"slot": 1})
        self.d.cmd("scene_cfg", "play", "rejected", "nee")
        self.assertResult(self.d.sc("play", {"slot": 2}), "rejected", "nee")
        self.assertEqual(self.running()["h"], fnv1a(A))

    def test_play_does_not_need_a_clock(self):
        self.d.settime(0)
        self.store(1, A)
        self.assertResult(self.d.sc("play", {"slot": 1}), "applied")
        self.assertEqual(self.d.scene_state()["time"], None)

    def test_play_after_reboot_starts_again(self):
        self.store(1, A)
        self.d.sc("play", {"slot": 1})
        self.d.cmd("reboot")
        self.d.scene_log()
        self.assertIsNone(self.running())  # run is RAM: het device meldt hem zelf weer (sceneRead + sceneRunning)
        self.assertResult(self.d.sc("play", {"slot": 1}), "applied")
        self.assertEqual(self.d.scene_log(), ["play|1|3|0|" + fnv1a(A)])

    # ---- stop ----
    def test_stop_without_run_is_applied(self):
        st, j = self.d.sc("stop", {})
        self.assertEqual((st, j), (200, {"ok": True, "result": "applied", "reason": ""}))
        self.assertEqual(self.d.scene_log(), ["stop|0"])  # stoppen is altijd veilig: het device hoort stop(0) zonder scene te verdragen
        self.assertIsNone(self.running())

    def test_stop_with_empty_body_text(self):
        self.assertResult(self.d.sc("stop", raw=""), "applied")
        self.assertResult(self.d.sc("stop", raw="   "), "applied")

    def test_stop_stops(self):
        self.store(1, A)
        self.d.sc("play", {"slot": 1})
        self.d.scene_log()
        self.assertEqual(self.d.sc("stop", {}), (200, {"ok": True, "result": "applied", "reason": ""}))
        self.assertEqual(self.d.scene_log(), ["stop|0"])
        self.assertIsNone(self.running())
        self.assertResult(self.d.sc("play", {"slot": 1}), "applied")  # weer spelen: geen no-op
        self.assertEqual(self.d.scene_log(), ["play|1|3|0|" + fnv1a(A)])

    def test_stop_with_slot_only_when_that_slot_plays(self):
        self.store(1, A)
        self.store(2, B)
        self.d.sc("play", {"slot": 1})
        self.d.scene_log()
        self.assertResult(self.d.sc("stop", {"slot": 2}), "applied")
        self.assertEqual(self.d.scene_log(), [])
        self.assertEqual(self.running()["slot"], 1)
        self.assertResult(self.d.sc("stop", {"slot": 1}), "applied")
        self.assertEqual(self.d.scene_log(), ["stop|1"])
        self.assertIsNone(self.running())

    def test_stop_slot_does_not_stop_a_direct_scene(self):
        self.d.sc("play", {"data": b64(A)})
        self.d.scene_log()
        self.assertResult(self.d.sc("stop", {"slot": 1}), "applied")
        self.assertEqual(self.d.scene_log(), [])
        self.assertEqual(self.running()["slot"], 0)

    def test_stop_syntax(self):
        for obj in ({"slot": 0}, {"slot": 9}, {"slot": "1"}, {"slot": 2.5}, {"slot": None}):
            self.assertErr(self.d.sc("stop", obj), 422, "bad_slot")
        self.assertEqual(self.d.scene_log(), [])

    def test_device_can_refuse_to_stop(self):
        self.store(1, A)
        self.d.sc("play", {"slot": 1})
        self.d.cmd("scene_cfg", "stop", "rejected", "vergrendeld")
        self.assertResult(self.d.sc("stop", {}), "rejected", "vergrendeld")
        self.assertIsNotNone(self.running())


class TestSchedule(SceneBase):
    def test_no_clock_is_rejected(self):
        self.d.settime(0)
        w = self.d.writes()
        st, j = self.d.sc("schedule", {"entries": [{"slot": 1, "at": NOW + 100}]})
        self.assertEqual((st, j), (200, {"ok": False, "result": "rejected", "reason": "geen tijd"}))
        self.assertEqual(self.d.writes(), w)
        self.assertEqual(self.pending(), [])

    def test_empty_list_without_a_clock_is_fine(self):
        self.sched([(1, NOW + 100)])
        self.d.settime(0)
        self.assertResult(self.sched([]), "applied")
        self.assertEqual(self.pending(), [])

    def test_clock_below_the_threshold_counts_as_no_clock(self):
        for t in (1, 1700000000):
            self.d.settime(t)
            self.assertResult(self.d.sc("schedule", {"entries": [{"slot": 1, "at": NOW}]}), "rejected", "geen tijd")

    def test_response_and_state(self):
        st, j = self.sched([(1, NOW + 100)])
        self.assertEqual((st, j), (200, {"ok": True, "result": "applied", "reason": ""}))  # geen hash
        self.assertEqual(self.pending(), [(1, NOW + 100)])

    def test_stored_sorted_ascending(self):
        entries = [(3, NOW + 900), (1, NOW + 100), (2, NOW + 500), (1, NOW + 500), (2, NOW + 100)]
        self.assertResult(self.sched(entries), "applied")
        want = sorted(entries, key=lambda e: (e[1], e[0]))
        self.assertEqual(self.pending(), want)
        self.assertEqual(self.d.prefs_get("huisdev_sc", "sched"), packed(want))

    def test_blob_layout_is_slot_then_little_endian_epoch(self):
        self.sched([(2, 1760000400)])
        blob = self.d.prefs_get("huisdev_sc", "sched")
        self.assertEqual(len(blob), 5)
        self.assertEqual(blob.hex(), "02" + "9079e768")  # 1760000400 = 0x68E77990, little-endian

    def test_survives_reboot(self):
        entries = [(2, NOW + 500), (1, NOW + 100)]
        self.sched(entries)
        self.d.cmd("reboot")
        self.assertEqual(self.pending(), sorted(entries, key=lambda e: e[1]))
        self.d.cmd("reboot")
        self.assertEqual(len(self.pending()), 2)

    def test_at_validation(self):
        w = self.d.writes()
        bad = ["1699999999", "0", "-1", "-1760000400", "1", "1e18", "1e400", "4294967296", "18446744073709551616", "1760000400.5",
               '"1760000400"', "null", "true", "[]", "{}", "1e9", "1.69999e9"]
        for at in bad:
            raw = '{"entries":[{"slot":1,"at":%s}]}' % at
            self.d.advance(2500)
            self.assertErr(self.d.sc("schedule", raw=raw), 422, "bad_value")
        self.assertErr(self.d.sc("schedule", raw='{"entries":[{"slot":1}]}'), 422, "bad_value")  # at ontbreekt
        self.assertEqual(self.d.writes(), w)
        self.assertEqual(self.pending(), [])

    def test_at_validation_is_syntax_so_it_does_not_need_a_clock(self):
        self.d.settime(0)
        for at in ("1e18", "-5", "1699999999"):
            self.assertErr(self.d.sc("schedule", raw='{"entries":[{"slot":1,"at":%s}]}' % at), 422, "bad_value")

    def test_at_window_edges(self):
        self.assertResult(self.sched([(1, 1700000000)]), "applied")             # ondergrens (inclusief; verstreken, dus later overgeslagen)
        self.assertResult(self.sched([(1, NOW + 14 * 86400)]), "applied")        # nu + 14 dagen
        self.assertErr(self.d.sc("schedule", {"entries": [{"slot": 1, "at": NOW + 14 * 86400 + 1}]}), 422, "bad_value")
        self.assertEqual(self.pending(), [(1, NOW + 14 * 86400)])

    def test_past_start_inside_the_window_is_accepted_and_starts_with_elapsed(self):
        self.store(1, A)
        self.d.scene_log()
        self.assertResult(self.sched([(1, NOW - 100)]), "applied")
        self.assertEqual(self.pending(), [(1, NOW - 100)])
        self.d.loop()
        self.assertEqual(self.d.scene_log(), ["play|1|3|100|" + fnv1a(A)])
        self.assertEqual(self.pending(), [])

    def test_entry_count_limit(self):
        entries = [(1, NOW + 100 * i) for i in range(1, 10)]
        self.assertErr(self.d.sc("schedule", {"entries": [{"slot": s, "at": a} for s, a in entries]}), 422, "bad_value")
        self.assertResult(self.sched(entries[:8]), "applied")
        self.assertEqual(len(self.pending()), 8)
        # een apparaat met sched 3
        self.d.scenes_on(8, 768, 3, "ledscene/1")
        self.d.cmd("reboot")
        self.assertEqual(len(self.pending()), 3)  # wat er te veel staat wordt bij het laden afgekapt
        self.assertErr(self.sched(entries[:4]), 422, "bad_value")
        self.assertResult(self.sched(entries[:3]), "applied")

    def test_sched_zero_device_accepts_only_the_empty_list(self):
        self.d.scenes_on(8, 768, 0, "ledscene/1")
        self.assertErr(self.d.sc("schedule", {"entries": [{"slot": 1, "at": NOW + 100}]}), 422, "bad_value")
        self.assertResult(self.d.sc("schedule", {"entries": []}), "applied")

    def test_entry_slot_validation(self):
        for slot in ("0", "9", '"1"', "1.5", "null", "-1"):
            self.assertErr(self.d.sc("schedule", raw='{"entries":[{"slot":%s,"at":%d}]}' % (slot, NOW + 100)), 422, "bad_slot")
        self.assertErr(self.d.sc("schedule", raw='{"entries":[{"at":%d}]}' % (NOW + 100)), 422, "bad_slot")
        self.assertEqual(self.pending(), [])

    def test_structure_errors_are_bad_value(self):
        for raw in ('{}', '{"entries":5}', '{"entries":"x"}', '{"entries":{}}', '{"entries":[5]}', '{"entries":[[]]}',
                    '{"entries":[{"slot":1,"at":1760000400},]}', '{"entries":[{"slot":1,"at":1760000400}', '{"entries":[',
                    '{"entries":[{"slot":1,"at":1760000400} {"slot":1,"at":1760000500}]}', '{"entries":[,]}', '', '[]', 'x',
                    '{"entries":[{"slot":1,"at":1760000400]}'):
            self.assertErr(self.d.sc("schedule", raw=raw), 422, "bad_value")
        self.assertEqual(self.pending(), [])

    def test_all_or_nothing(self):
        self.sched([(1, NOW + 100)])
        w = self.d.writes()
        self.assertErr(self.d.sc("schedule", {"entries": [{"slot": 2, "at": NOW + 50}, {"slot": 99, "at": NOW + 60}]}), 422, "bad_slot")
        self.assertErr(self.d.sc("schedule", {"entries": [{"slot": 2, "at": NOW + 50}, {"slot": 1, "at": 5}]}), 422, "bad_value")
        self.assertEqual(self.pending(), [(1, NOW + 100)])
        self.assertEqual(self.d.writes(), w)

    def test_extra_keys_and_whitespace_in_entries(self):
        raw = '{ "entries" : [ { "at" : %d , "slot" : 2, "label" : "wekker } ]" } , {"slot":1,"at":%d} ] , "x":1 }' % (NOW + 200, NOW + 100)
        self.assertResult(self.d.sc("schedule", raw=raw), "applied")
        self.assertEqual(self.pending(), [(1, NOW + 100), (2, NOW + 200)])

    def test_identical_list_writes_nothing(self):
        entries = [(1, NOW + 100), (2, NOW + 200)]
        self.sched(entries)
        w = self.d.writes()
        self.assertResult(self.sched(entries), "applied")
        self.assertResult(self.sched(list(reversed(entries))), "applied")  # andere volgorde, dezelfde lijst
        self.assertEqual(self.d.writes(), w)
        self.assertResult(self.sched(entries[:1]), "applied")
        self.assertEqual(self.d.writes(), w + 1)
        self.assertResult(self.sched([]), "applied")
        self.assertEqual(self.d.writes(), w + 2)
        self.assertIsNone(self.d.prefs_get("huisdev_sc", "sched"))
        self.assertResult(self.sched([]), "applied")
        self.assertEqual(self.d.writes(), w + 2)

    def test_empty_list_clears(self):
        self.sched([(1, NOW + 100)])
        self.assertResult(self.sched([]), "applied")
        self.assertEqual(self.pending(), [])
        self.d.cmd("reboot")
        self.assertEqual(self.pending(), [])

    def test_nvs_full_keeps_the_old_list(self):
        self.sched([(1, NOW + 100)])
        self.d.advance(2500)
        self.d.cmd("nvs_quota", "0")
        self.assertResult(self.d.sc("schedule", {"entries": [{"slot": 1, "at": NOW + 100}, {"slot": 2, "at": NOW + 200}]}), "rejected", "geen geheugen")
        self.assertEqual(self.pending(), [(1, NOW + 100)])
        self.assertEqual(self.d.prefs_get("huisdev_sc", "sched"), packed([(1, NOW + 100)]))

    def test_play_stop_and_store_do_not_clear_the_list(self):
        entries = [(1, NOW + 1000), (2, NOW + 2000)]
        self.store(1, A)
        self.sched(entries)
        w = self.d.writes()
        self.d.sc("play", {"slot": 1})
        self.d.sc("play", {"data": b64(B)})
        self.d.sc("stop", {})
        self.d.sc("stop", {"slot": 2})
        self.assertEqual(self.pending(), entries)
        self.store(2, B)
        self.assertEqual(self.pending(), entries)
        self.d.advance(2500)
        self.d.sc("store", {"slot": 1, "data": ""})
        self.assertEqual(self.pending(), entries)
        self.assertEqual(self.d.prefs_get("huisdev_sc", "sched"), packed(entries))
        self.assertEqual(self.d.writes(), w + 2)  # alleen de twee store-acties

    def test_unclaim_wipes_scenes_and_list(self):
        self.store(1, A)
        self.store(8, B)
        self.sched([(1, NOW + 100)])
        self.assertResult(self.d.sc("play", {"slot": 1}), "applied")
        st, j = self.d.jh("POST", "/huisdev/unclaim", TOKEN, "{}")
        self.assertEqual((st, j), (200, {"ok": True}))
        for k in ("s1", "s8", "sched"):
            self.assertIsNone(self.d.prefs_get("huisdev_sc", k), k)
        _, s = self.d.state()
        self.assertEqual(s["scenes"]["have"], {})
        self.assertEqual(s["scenes"]["sched"], [])
        self.assertEqual(s["scenes"]["run"]["slot"], 0)  # de lopende scene speelt zijn eigen kopie door, zonder slot
        self.d.cmd("reboot")
        self.assertEqual(self.d.state()[1]["scenes"]["have"], {})
        self.assertEqual(self.d.state()[1]["scenes"]["sched"], [])

    def test_resetclaim_wipes_scenes_and_list(self):
        self.store(3, A)
        self.sched([(3, NOW + 100)])
        self.d.cmd("resetclaim")
        for k in ("s3", "sched"):
            self.assertIsNone(self.d.prefs_get("huisdev_sc", k))
        self.d.cmd("reboot")
        self.assertEqual(self.d.state()[1]["scenes"]["have"], {})
        self.assertEqual(self.d.state()[1]["scenes"]["sched"], [])

    def test_a_new_claim_starts_with_empty_scenes(self):
        self.store(1, A)
        self.d.jh("POST", "/huisdev/unclaim", TOKEN, "{}")
        self.d.claim("f" * 32)
        self.assertEqual(self.d.state("f" * 32)[1]["scenes"]["have"], {})

    # ---- geplande starts in loop() ----
    def test_start_at_the_scheduled_time(self):
        self.store(1, A)
        self.sched([(1, NOW + 100)])
        self.d.scene_log()
        w = self.d.writes()
        self.d.settime(NOW + 99)
        self.d.loop()
        self.assertEqual(self.d.scene_log(), [])
        self.assertEqual(self.pending(), [(1, NOW + 100)])
        self.d.settime(NOW + 100)
        self.d.loop()
        self.assertEqual(self.d.scene_log(), ["play|1|3|0|" + fnv1a(A)])
        self.assertEqual(self.running(), {"slot": 1, "h": fnv1a(A), "until": None})
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.d.writes(), w)  # de NVS-lijst wordt niet herschreven
        self.assertEqual(self.d.prefs_get("huisdev_sc", "sched"), packed([(1, NOW + 100)]))
        self.d.loop()
        self.d.settime(NOW + 200)
        self.d.loop()
        self.assertEqual(self.d.scene_log(), [])  # niet nog eens

    def test_late_start_passes_elapsed_to_the_device(self):
        self.store(2, B)
        self.sched([(2, NOW + 100)])
        self.d.scene_log()
        self.d.settime(NOW + 100 + 4000)
        self.d.loop()
        self.assertEqual(self.d.scene_log(), ["play|2|5|4000|" + fnv1a(B)])

    def test_device_can_refuse_a_late_start_and_the_header_skips_it(self):
        self.store(2, B)
        self.sched([(2, NOW + 100)])
        self.d.scene_log()
        self.d.cmd("scene_cfg", "play", "rejected", "te laat")
        self.d.settime(NOW + 100 + 4000)
        self.d.loop()
        self.assertEqual(len(self.d.scene_log()), 1)
        self.assertEqual(self.pending(), [])
        self.assertIsNone(self.running())
        self.d.cmd("scene_cfg", "play", "applied", "")
        self.d.loop()
        self.assertEqual(self.d.scene_log(), [])  # overgeslagen blijft overgeslagen

    def test_start_older_than_24h_is_skipped(self):
        self.store(1, A)
        self.sched([(1, NOW + 100)])
        self.d.scene_log()
        self.d.settime(NOW + 100 + 86400)  # precies 24 u: nog binnen het venster
        self.d.loop()
        self.assertEqual(self.d.scene_log(), ["play|1|3|86400|" + fnv1a(A)])
        self.d.sc("stop", {})
        self.d.scene_log()
        self.sched([(1, NOW + 100 + 86400 + 500)])
        self.d.settime(NOW + 100 + 86400 + 500 + 86401)
        self.d.loop()
        self.assertEqual(self.d.scene_log(), [])
        self.assertEqual(self.pending(), [])

    def test_empty_slot_start_is_skipped(self):
        self.sched([(4, NOW + 100)])
        self.d.scene_log()
        self.d.settime(NOW + 100)
        self.d.loop()
        self.assertEqual(self.d.scene_log(), [])
        self.assertEqual(self.pending(), [])

    def test_slot_wiped_after_scheduling_is_skipped(self):
        self.store(4, A)
        self.sched([(4, NOW + 100)])
        self.store(4, b"")
        self.d.scene_log()
        self.d.settime(NOW + 100)
        self.d.loop()
        self.assertEqual(self.d.scene_log(), [])

    def test_slot_replaced_after_scheduling_plays_the_new_content(self):
        self.store(4, A)
        self.sched([(4, NOW + 100)])
        self.store(4, B)
        self.d.scene_log()
        self.d.settime(NOW + 100)
        self.d.loop()
        self.assertEqual(self.d.scene_log(), ["play|4|5|0|" + fnv1a(B)])

    def test_several_due_starts_run_in_order_and_the_last_wins(self):
        self.store(1, A)
        self.store(2, B)
        self.sched([(2, NOW + 200), (1, NOW + 100)])
        self.d.scene_log()
        self.d.settime(NOW + 250)
        self.d.loop()
        self.assertEqual(self.d.scene_log(), ["play|1|3|150|" + fnv1a(A), "play|2|5|50|" + fnv1a(B)])
        self.assertEqual(self.running()["slot"], 2)

    def test_scheduled_start_of_the_running_scene_is_a_noop(self):
        self.store(1, A)
        self.sched([(1, NOW + 100)])
        self.d.sc("play", {"slot": 1})
        self.d.scene_log()
        self.d.settime(NOW + 100)
        self.d.loop()
        self.assertEqual(self.d.scene_log(), [])
        self.assertEqual(self.pending(), [])

    def test_no_start_without_a_clock(self):
        self.store(1, A)
        self.sched([(1, NOW + 100)])
        self.d.scene_log()
        self.d.settime(0)
        self.d.loop()
        self.assertEqual(self.d.scene_log(), [])
        self.assertEqual(self.pending(), [(1, NOW + 100)])
        self.d.settime(NOW + 100)  # de klok wordt geldig (NTP) en de start is er
        self.d.loop()
        self.assertEqual(len(self.d.scene_log()), 1)

    def test_reboot_before_the_start_keeps_it(self):
        self.store(1, A)
        self.sched([(1, NOW + 1000)])
        self.d.cmd("reboot")
        self.d.scene_log()
        self.d.settime(NOW + 1000)
        self.d.loop()
        self.assertEqual(self.d.scene_log(), ["play|1|3|0|" + fnv1a(A)])

    def test_reboot_after_the_start_resumes_within_24h(self):
        self.store(1, A)
        self.sched([(1, NOW + 100)])
        self.d.settime(NOW + 100)
        self.d.loop()
        self.d.cmd("reboot")  # de NVS-lijst bevat de start nog: het device mag hem met elapsedS laten hervatten
        self.d.scene_log()
        self.d.settime(NOW + 100 + 600)
        self.d.loop()
        self.assertEqual(self.d.scene_log(), ["play|1|3|600|" + fnv1a(A)])

    def test_after_reboot_expired_lines_are_ignored_but_stay_in_nvs(self):
        self.store(1, A)
        self.sched([(1, NOW + 100), (1, NOW + 100000)])
        self.d.cmd("reboot")
        self.d.scene_log()
        w = self.d.writes()
        self.d.settime(NOW + 100 + 86401)  # de eerste is meer dan 24 u geleden
        self.d.loop()
        self.assertEqual(self.d.scene_log(), [])
        self.assertEqual(self.pending(), [(1, NOW + 100000)])
        self.assertEqual(self.d.writes(), w)
        self.assertEqual(self.d.prefs_get("huisdev_sc", "sched"), packed([(1, NOW + 100), (1, NOW + 100000)]))

    def test_after_reboot_nothing_starts_before_the_clock_is_valid(self):
        self.store(1, A)
        self.sched([(1, NOW + 100)])
        self.d.cmd("reboot")
        self.d.settime(0)
        self.d.scene_log()
        self.d.loop()
        self.assertEqual(self.d.scene_log(), [])
        self.assertEqual(self.pending(), [(1, NOW + 100)])
        self.assertEqual(self.d.scene_state()["time"], None)

    def test_schedule_after_the_start_has_run_replaces_the_list(self):
        self.store(1, A)
        self.sched([(1, NOW + 100), (1, NOW + 5000)])
        self.d.settime(NOW + 100)
        self.d.loop()
        self.assertEqual(self.pending(), [(1, NOW + 5000)])
        # de add-on stuurt de lijst die hij nog wil (alleen de komende start): de NVS-lijst verschilt dus, en wordt vervangen
        w = self.d.writes()
        self.assertResult(self.sched([(1, NOW + 5000)]), "applied")
        self.assertEqual(self.d.writes(), w + 1)
        self.assertEqual(self.d.prefs_get("huisdev_sc", "sched"), packed([(1, NOW + 5000)]))
        self.assertEqual(self.pending(), [(1, NOW + 5000)])

    def test_resending_the_old_full_list_does_not_replay_a_finished_start(self):
        self.store(1, A)
        entries = [(1, NOW + 100), (1, NOW + 5000)]
        self.sched(entries)
        self.d.settime(NOW + 100)
        self.d.loop()
        self.d.sc("stop", {})
        self.d.scene_log()
        self.assertResult(self.sched(entries), "applied")  # gelijk aan NVS: niets schrijven, niets opnieuw plannen
        self.d.loop()
        self.assertEqual(self.d.scene_log(), [])
        self.assertEqual(self.pending(), [(1, NOW + 5000)])


if __name__ == "__main__":
    unittest.main()
