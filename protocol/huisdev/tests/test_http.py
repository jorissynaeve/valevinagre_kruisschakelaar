"""Claimen, schrijven, acties, foutcodes en de WebServer-koppeling tegen HUISDEV.md §4, §7 en §8."""
import json
import unittest

from huisdevtest import Base, TOKEN

CONFIRM_CAL = 'Kalibratie "overschrijft" instellingen. Doorgaan?'


class TestPathsAndMethods(Base):
    def test_not_ours_returns_zero(self):
        for path in ("/", "/api/state", "/huisdevx/state", "/huisdev", "/Huisdev/state"):
            st, out = self.d.handle("GET", path)
            self.assertEqual((st, out), (0, ""), path)

    def test_unknown_path_is_404(self):
        for method in ("GET", "POST"):
            for path in ("/huisdev/nope", "/huisdev/", "/huisdev/state/", "/huisdev/Descriptor"):
                self.assertErr(self.d.jh(method, path, TOKEN, "{}"), 404, "unknown_path")
        self.d.claim()
        self.assertErr(self.d.jh("GET", "/huisdev/nope", TOKEN), 404, "unknown_path")

    def test_wrong_method_is_405(self):
        self.d.claim()
        cases = [("POST", "/huisdev/descriptor"), ("POST", "/huisdev/state"), ("GET", "/huisdev/set"),
                 ("GET", "/huisdev/do"), ("GET", "/huisdev/claim"), ("GET", "/huisdev/unclaim"),
                 ("PUT", "/huisdev/state"), ("DELETE", "/huisdev/descriptor"), ("PUT", "/huisdev/set"),
                 ("PUT", "/huisdev/do"), ("PUT", "/huisdev/claim"), ("DELETE", "/huisdev/unclaim"),
                 ("HEAD", "/huisdev/state"), ("OPTIONS", "/huisdev/set")]
        for method, path in cases:
            self.assertErr(self.d.jh(method, path, TOKEN, '{"id":"relay","value":true}'), 405, "method")
        # PUT mag niets uitvoeren
        self.assertEqual(self.d.calls(), [])
        _, s = self.d.state(TOKEN)
        self.assertEqual(s["points"]["relay"], {"v": None})

    def test_every_response_is_json_with_error_shape(self):
        for res in (self.d.jh("GET", "/huisdev/nope"), self.d.jh("POST", "/huisdev/set", "", "{}"),
                    self.d.jh("POST", "/huisdev/claim", "", "{}")):
            st, j = res
            self.assertGreaterEqual(st, 400)
            self.assertEqual(set(j), {"ok", "error", "reason"})


class TestClaim(Base):
    def test_unclaimed_state_open_writes_forbidden(self):
        st, _ = self.d.get("/huisdev/state")
        self.assertEqual(st, 200)
        self.assertErr(self.d.post("/huisdev/set", {"id": "relay", "value": True}), 403, "not_claimed")
        self.assertErr(self.d.post("/huisdev/do", {"id": "boost", "args": {"minutes": 30}}), 403, "not_claimed")
        self.assertErr(self.d.post("/huisdev/set", {"id": "relay", "value": True}, token=""), 403, "not_claimed")
        self.assertEqual(self.d.calls(), [])

    def test_claim_token_length_rules(self):
        for bad in ("", "x" * 15, "x" * 129):
            self.assertErr(self.d.post("/huisdev/claim", {"token": bad}, token=""), 422, "bad_token")
        self.assertErr(self.d.post("/huisdev/claim", None, token="", raw=""), 422, "bad_token")
        self.assertErr(self.d.post("/huisdev/claim", None, token="", raw="{}"), 422, "bad_token")
        self.assertErr(self.d.post("/huisdev/claim", {"token": 1234567890123456789}, token=""), 422, "bad_token")
        self.assertEqual(self.d.cmd("claimed"), ["0"])
        self.assertEqual(self.d.cmd("prefs_token"), ["-"])
        for good in ("x" * 16, "y" * 128):
            self.d.cmd("factory")
            self.d.claim(good)
            self.assertEqual(self.d.cmd("claimed"), ["1"])
            self.assertEqual(self.d.cmd("prefs_token"), ["+" + good])

    def test_claim_with_whitespace_and_key_order(self):
        st, j = self.d.post("/huisdev/claim", None, token="", raw='{ "token" :  "%s" }\n' % TOKEN)
        self.assertEqual((st, j), (200, {"ok": True}))

    def test_state_needs_token_after_claim(self):
        self.d.claim()
        for tok in ("", "x", TOKEN[:-1], TOKEN + "0", "X" + TOKEN[1:], TOKEN[:-1] + "X", TOKEN.upper()):
            self.assertErr(self.d.jh("GET", "/huisdev/state", tok), 401, "token")
        st, _ = self.d.get("/huisdev/state", TOKEN)
        self.assertEqual(st, 200)

    def test_write_with_wrong_token_is_401(self):
        self.d.claim()
        self.assertErr(self.d.post("/huisdev/set", {"id": "relay", "value": True}, token="fout"), 401, "token")
        self.assertErr(self.d.post("/huisdev/do", {"id": "boost", "args": {"minutes": 30}}, token=""), 401, "token")
        self.assertEqual(self.d.calls(), [])

    def test_second_claim_is_409_and_keeps_token(self):
        self.d.claim()
        self.assertErr(self.d.post("/huisdev/claim", {"token": "a" * 32}, token=""), 409, "already_claimed")
        self.assertEqual(self.d.cmd("prefs_token"), ["+" + TOKEN])

    def test_unclaim_rules(self):
        self.assertErr(self.d.post("/huisdev/unclaim", {}, token=TOKEN), 409, "not_claimed")
        self.d.claim()
        self.assertErr(self.d.post("/huisdev/unclaim", {}, token="fout"), 401, "token")
        self.assertEqual(self.d.cmd("claimed"), ["1"])
        self.assertEqual(self.d.post("/huisdev/unclaim", {}), (200, {"ok": True}))
        self.assertEqual(self.d.cmd("claimed"), ["0"])
        self.assertEqual(self.d.cmd("prefs_token"), ["-"])
        st, _ = self.d.get("/huisdev/state")
        self.assertEqual(st, 200)
        new = "n" * 20
        self.d.claim(new)
        self.assertErr(self.d.jh("GET", "/huisdev/state", TOKEN), 401, "token")
        st, _ = self.d.get("/huisdev/state", new)
        self.assertEqual(st, 200)

    def test_token_with_special_characters(self):
        for tok in ('ab"cd\\ef-0123456789', "tab\there-0123456789", "tökén-ünïcode-€-0123456789", "emoji😀emoji😀emoji😀"):
            for ascii_json in (True, False):
                self.d.cmd("factory")
                st, j = self.d.post("/huisdev/claim", {"token": tok}, token="", ascii=ascii_json)
                self.assertEqual(st, 200, (tok, ascii_json, j))
                stt, _ = self.d.get("/huisdev/state", tok)
                self.assertEqual(stt, 200, (tok, ascii_json))

    def test_claim_survives_reboot_and_local_reset(self):
        self.d.claim()
        self.d.cmd("reboot")
        self.assertEqual(self.d.cmd("claimed"), ["1"])
        self.assertErr(self.d.jh("GET", "/huisdev/state", ""), 401, "token")
        st, _ = self.d.get("/huisdev/state", TOKEN)
        self.assertEqual(st, 200)
        self.d.cmd("resetclaim")
        self.assertEqual(self.d.cmd("claimed"), ["0"])
        self.assertEqual(self.d.cmd("prefs_token"), ["-"])
        self.d.cmd("reboot")
        self.assertEqual(self.d.cmd("claimed"), ["0"])
        st, _ = self.d.get("/huisdev/state")
        self.assertEqual(st, 200)
        self.d.claim("z" * 16)

    def test_first_boot_without_namespace(self):
        # factory wist de prefs: begin(readOnly) op een ontbrekende namespace faalt en dat is geen fout
        self.assertEqual(self.d.cmd("claimed"), ["0"])


class TestSet(Base):
    def setUp(self):
        super().setUp()
        self.d.claim()

    def set(self, pid, value, **kw):
        return self.d.post("/huisdev/set", {"id": pid, "value": value}, **kw)

    def test_results_applied_queued_rejected(self):
        self.assertResult(self.set("relay", True), "applied", "")
        self.assertEqual(self.d.calls(), ["relay|1|"])
        _, s = self.d.state(TOKEN)
        self.assertIs(s["points"]["relay"]["v"], True)
        self.assertResult(self.set("ph_set", 7.2), "queued", "")
        self.assertEqual(self.d.calls(), ["ph_set|7.2|"])
        self.assertResult(self.set("pump", True), "rejected", "pomp draait niet")

    def test_unknown_and_not_writable(self):
        self.assertErr(self.set("bestaat_niet", 1), 404, "unknown_point")
        self.assertErr(self.d.post("/huisdev/set", {"value": 1}), 404, "unknown_point")
        self.assertErr(self.d.post("/huisdev/set", None, raw=""), 404, "unknown_point")
        self.assertErr(self.set("ph", 7.0), 403, "not_writable")
        self.assertErr(self.set("alarm_no_flow", True), 403, "not_writable")
        self.assertEqual(self.d.calls(), [])

    def test_missing_value(self):
        self.assertErr(self.d.post("/huisdev/set", {"id": "relay"}), 422, "missing_value")
        self.assertErr(self.d.post("/huisdev/set", {"id": "pool_name"}), 422, "missing_value")

    def test_bool(self):
        self.assertResult(self.set("relay", True), "applied")
        self.assertResult(self.set("relay", False), "applied")
        self.assertEqual(self.d.calls(), ["relay|1|", "relay|0|"])
        for bad in (1, 0, "true", "false", None, 1.5, [], {}):
            self.assertErr(self.set("relay", bad), 422, "bad_value")
        self.assertEqual(self.d.calls(), [])

    def test_float_range_and_step(self):
        for k in range(9):
            v = round(6.8 + 0.1 * k, 1)
            self.assertResult(self.set("ph_set", v), "queued", None)
        self.assertEqual(len(self.d.calls()), 9)
        for v in (6.7, 7.7, 0, 100, -7.2):
            self.assertErr(self.set("ph_set", v), 422, "range")
        for v in (7.25, 6.85, 7.05, 7.55):
            self.assertErr(self.set("ph_set", v), 422, "step")
        for bad in ("7.2", True, None, []):
            self.assertErr(self.set("ph_set", bad), 422, "bad_value")
        self.assertEqual(self.d.calls(), [])

    def test_int_range_and_step(self):
        for v in (10, 500, 990, 500.0):
            self.assertResult(self.set("prod_set", v), "applied")
        for v in (5, 1000, 0, -10):
            self.assertErr(self.set("prod_set", v), 422, "range")
        for v in (15, 505, 11):
            self.assertErr(self.set("prod_set", v), 422, "step")
        for v in (500.5, "500", None, False):
            self.assertErr(self.set("prod_set", v), 422, "bad_value")

    def test_enum(self):
        for v in (0, 1, 2):
            self.assertResult(self.set("mode_electro", v), "applied")
            _, s = self.d.state(TOKEN)
            self.assertEqual(s["points"]["mode_electro"]["v"], v)
        for v in (3, -1, 99):
            self.assertErr(self.set("mode_electro", v), 422, "bad_option")
        for v in (1.5, "1", None, True):
            self.assertErr(self.set("mode_electro", v), 422, "bad_value")

    def test_non_json_numbers_rejected(self):
        for raw in ("1e400", "-1e400", "0x10", "-", "+", ".", "1e", "nan", "inf", "Infinity", "NaN", "7..2"):
            body = '{"id":"ph_set","value":%s}' % raw
            st, j = self.d.post("/huisdev/set", None, raw=body)
            self.assertEqual((st, j.get("error")), (422, "bad_value"), raw)
        self.assertEqual(self.d.calls(), [])

    def test_whitespace_and_key_order(self):
        st, j = self.d.post("/huisdev/set", None, raw=' {\n "value" : 7.2 ,\r\n "id" : "ph_set" } ')
        self.assertEqual((st, j["result"]), (200, "queued"))

    def test_text(self):
        for t in ("Zwembad", "", 'aa"bb', "back\\slash", "regel\nnieuw\ttab\r", "\x01", "°C 😀 é", '{"id":"x","value":1}', "id", "value"):
            for ascii_json in (True, False):
                self.d.calls()
                self.assertResult(self.set("pool_name", t, ascii=ascii_json), "applied")
                self.assertEqual(self.d.calls(), ["pool_name|0|" + t], (t, ascii_json))
                _, s = self.d.state(TOKEN)
                self.assertEqual(s["points"]["pool_name"]["v"], t)
        for bad in (5, True, None, [], 1.5):
            self.assertErr(self.set("pool_name", bad), 422, "bad_value")

    def test_keys_in_any_order_and_values_that_look_like_keys(self):
        # de waarde "id" staat voor de sleutel "id": mag de sleutelzoeker niet misleiden
        st, j = self.d.post("/huisdev/set", None, raw='{"value":"id","id":"pool_name"}')
        self.assertEqual((st, j.get("result")), (200, "applied"), j)
        self.assertEqual(self.d.calls(), ["pool_name|0|id"])
        st, j = self.d.post("/huisdev/set", None, raw='{"value":"value","id":"pool_name"}')
        self.assertEqual((st, j.get("result")), (200, "applied"), j)

    def test_point_called_value(self):
        # een punt met id "value": {"id":"value","value":42}
        self.assertResult(self.set("value", 42), "applied")
        self.assertEqual(self.d.calls(), ["value|42|"])
        self.assertErr(self.set("value", 101), 422, "range")

    def test_danger_needs_confirm(self):
        for body in ({"id": "calibrate", "value": True}, {"id": "calibrate", "value": True, "confirm": False},
                     {"id": "calibrate", "value": True, "confirm": "true"}, {"id": "calibrate", "value": True, "confirm": 1}):
            st, j = self.d.post("/huisdev/set", body)
            self.assertEqual((st, j["error"], j["reason"]), (409, "confirm_required", CONFIRM_CAL), body)
        self.assertEqual(self.d.calls(), [])
        self.assertResult(self.d.post("/huisdev/set", {"id": "calibrate", "value": True, "confirm": True}), "applied")
        self.assertEqual(self.d.calls(), ["calibrate|1|"])
        self.assertErr(self.d.post("/huisdev/set", {"id": "calibrate", "value": 5, "confirm": True}), 422, "bad_value")

    def test_set_on_removed_point_is_404(self):
        self.d.cmd("remove", "relay")
        self.assertErr(self.set("relay", True), 404, "unknown_point")


class TestActions(Base):
    def setUp(self):
        super().setUp()
        self.d.claim()

    def do(self, aid, args=None, **extra):
        body = {"id": aid}
        if args is not None:
            body["args"] = args
        body.update(extra)
        return self.d.post("/huisdev/do", body)

    def test_boost_ok_and_body_reaches_callback(self):
        self.assertResult(self.do("boost", {"minutes": 30}), "queued", "")
        (call,) = self.d.calls()
        self.assertTrue(call.startswith("boost|"))
        self.assertEqual(json.loads(call[len("boost|"):]), {"id": "boost", "args": {"minutes": 30}})
        for v in (5, 240, 100):
            self.assertResult(self.do("boost", {"minutes": v}), "queued")

    def test_boost_arg_validation(self):
        self.assertEqual(self.d.post("/huisdev/do", {"id": "boost"})[1]["error"], "missing_arg")
        st, j = self.d.post("/huisdev/do", {"id": "boost"})
        self.assertEqual((st, j["reason"]), (422, "minutes"))
        self.assertErr(self.do("boost", {}), 422, "missing_arg")
        for v in (4, 245, 0, -5):
            st, j = self.do("boost", {"minutes": v})
            self.assertEqual((st, j["error"], j["reason"]), (422, "range", "minutes"), v)
        for v in (32, 7):
            self.assertErr(self.do("boost", {"minutes": v}), 422, "step")
        for v in ("30", 30.5, None, True, []):
            self.assertErr(self.do("boost", {"minutes": v}), 422, "bad_value")
        self.assertEqual(self.d.calls(), [])

    def test_danger_action(self):
        st, j = self.do("reset")
        self.assertEqual((st, j["error"], j["reason"]), (409, "confirm_required", "Alle instellingen gaan verloren. Doorgaan?"))
        self.assertErr(self.do("reset", confirm=False), 409, "confirm_required")
        self.assertErr(self.do("reset", confirm="true"), 409, "confirm_required")
        self.assertEqual(self.d.calls(), [])
        self.assertResult(self.do("reset", confirm=True), "applied")
        self.assertEqual(len(self.d.calls()), 1)

    def test_enum_and_text_args(self):
        ok = self.do("calibrate_ph", {"buffer": 7, "note": "ok"})
        self.assertResult(ok, "rejected", "sonde niet ondergedompeld")
        self.assertEqual(ok[0], 200)
        self.assertResult(self.do("calibrate_ph", {"buffer": 10, "note": 'a"b\\c\n°'}), "rejected")
        self.assertEqual(len(self.d.calls()), 2)
        st, j = self.do("calibrate_ph", {"buffer": 8, "note": "x"})
        self.assertEqual((st, j["error"], j["reason"]), (422, "bad_option", "buffer"))
        st, j = self.do("calibrate_ph", {"buffer": 7})
        self.assertEqual((st, j["error"], j["reason"]), (422, "missing_arg", "note"))
        st, j = self.do("calibrate_ph", {"buffer": 7, "note": 5})
        self.assertEqual((st, j["error"], j["reason"]), (422, "bad_value", "note"))
        self.assertEqual(self.d.calls(), [])

    def test_disabled_action(self):
        st, j = self.do("flush")
        self.assertEqual((st, j["error"], j["reason"]), (409, "disabled", "pomp draait niet"))
        self.d.cmd("actionstate", "flush", "0", "")
        self.assertResult(self.do("flush"), "applied")

    def test_disabled_wins_over_confirm(self):
        self.d.cmd("actionstate", "reset", "1", "bezig")
        self.assertErr(self.do("reset"), 409, "disabled")

    def test_unknown_action(self):
        self.assertErr(self.do("bestaat_niet"), 404, "unknown_action")
        self.assertErr(self.d.post("/huisdev/do", {}), 404, "unknown_action")
        self.d.cmd("removeaction", "boost")
        self.assertErr(self.do("boost", {"minutes": 30}), 404, "unknown_action")

    def test_action_without_function_is_rejected_not_a_crash(self):
        self.d.cmd("addaction_nofn", "leeg", "Leeg")
        self.assertResult(self.do("leeg"), "rejected")

    def test_auth(self):
        self.assertErr(self.d.post("/huisdev/do", {"id": "boost", "args": {"minutes": 30}}, token="fout"), 401, "token")


class TestWebServerAdapter(Base):
    def test_routes_and_headers(self):
        routes, headers = self.d.cmd("ws_info")
        self.assertEqual(set(r for r in routes.split(",") if r), {
            "GET /huisdev/descriptor", "GET /huisdev/state", "POST /huisdev/set", "POST /huisdev/do",
            "POST /huisdev/claim", "POST /huisdev/unclaim"})
        self.assertEqual(headers, "X-Huisdev-Token,")

    def call(self, method, path, token="", body=""):
        r = self.d.cmd("ws_call", method, path, token, body)
        self.assertNotEqual(r[0], "NOROUTE", path)
        return int(r[0]), r[1], r[2], r[3]

    def test_responses_pass_through(self):
        code, ctype, cache, body = self.call("GET", "/huisdev/descriptor")
        self.assertEqual((code, ctype, cache), (200, "application/json", "no-store"))
        self.assertEqual(json.loads(body), self.d.descriptor()[1])
        code, _, cache, body = self.call("GET", "/huisdev/state")
        self.assertEqual((code, cache), (200, "no-store"))
        json.loads(body)

    def test_token_header_and_plain_body(self):
        code, _, _, _ = self.call("POST", "/huisdev/claim", "", json.dumps({"token": TOKEN}))
        self.assertEqual(code, 200)
        self.assertEqual(self.call("GET", "/huisdev/state", "")[0], 401)
        self.assertEqual(self.call("GET", "/huisdev/state", TOKEN)[0], 200)
        code, _, _, body = self.call("POST", "/huisdev/set", TOKEN, json.dumps({"id": "relay", "value": True}))
        self.assertEqual((code, json.loads(body)["result"]), (200, "applied"))
        code, _, _, body = self.call("POST", "/huisdev/set", "", json.dumps({"id": "relay", "value": True}))
        self.assertEqual(code, 401)
        code, _, _, body = self.call("POST", "/huisdev/do", TOKEN, json.dumps({"id": "boost", "args": {"minutes": 30}}))
        self.assertEqual((code, json.loads(body)["result"]), (200, "queued"))
        self.assertEqual(self.call("POST", "/huisdev/unclaim", TOKEN)[0], 200)


if __name__ == "__main__":
    unittest.main()
