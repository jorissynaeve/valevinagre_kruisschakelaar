"""Hulp voor de tests: start de C++-testdriver (de echte huisdev.h op een host) en praat ermee via een regelprotocol."""
import json
import os
import re
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DRIVER = os.environ.get("HUISDEV_DRIVER") or os.path.join(ROOT, "build", "driver")
FIXTURES = os.path.join(HERE, "fixtures")
TOKEN = "0123456789abcdef0123456789abcdef"

with open(os.path.join(ROOT, "huisdev.h"), encoding="utf-8") as _f:
    _src = _f.read()
HEADER_VERSION = re.search(r'#define HUISDEV_HEADER_VERSION "([^"]+)"', _src).group(1)
PROTO = re.search(r'#define HUISDEV_PROTO "([^"]+)"', _src).group(1)


def esc(v):
    b = v if isinstance(v, bytes) else str(v).encode("utf-8")
    return b.replace(b"\\", b"\\\\").replace(b"\t", b"\\t").replace(b"\n", b"\\n").replace(b"\r", b"\\r")


def unesc(b):
    out = bytearray()
    i = 0
    while i < len(b):
        c = b[i]
        if c == 0x5C and i + 1 < len(b):
            i += 1
            out += {ord("t"): b"\t", ord("n"): b"\n", ord("r"): b"\r"}.get(b[i], bytes([b[i]]))
        else:
            out.append(c)
        i += 1
    return bytes(out)


def fnv1a(data: bytes) -> str:
    h = 2166136261
    for b in data:
        h ^= b
        h = (h * 16777619) & 0xFFFFFFFF
    return "%08x" % h


def rev_body(descriptor_text: str) -> str:
    """De tekst waarover `rev` wordt berekend: "points":[...],"actions":[...] (zie HUISDEV.md §5)."""
    i = descriptor_text.index('"points":[')
    return descriptor_text[i:-1]


class Dev:
    def __init__(self):
        self.err = tempfile.TemporaryFile()
        env = dict(os.environ)
        self.p = subprocess.Popen([DRIVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err, env=env)

    def stderr(self):
        self.err.seek(0)
        return self.err.read().decode("utf-8", "replace")

    def cmd(self, *fields):
        line = b"\t".join(esc(f) for f in fields) + b"\n"
        try:
            self.p.stdin.write(line)
            self.p.stdin.flush()
        except BrokenPipeError:
            raise AssertionError("driver is gestopt: " + self.stderr())
        reply = self.p.stdout.readline()
        if not reply:
            raise AssertionError("driver crashte op %r: %s" % (fields, self.stderr()))
        parts = reply.rstrip(b"\n").split(b"\t")
        if parts[0] != b"OK":
            raise AssertionError("driver: %r" % reply)
        return [unesc(x).decode("utf-8") for x in parts[1:]]

    def close(self):
        try:
            self.p.stdin.write(b"quit\n")
            self.p.stdin.flush()
        except BrokenPipeError:
            pass
        self.p.stdin.close()
        rc = self.p.wait(timeout=20)
        self.p.stdout.close()
        err = self.stderr()
        self.err.close()
        if rc != 0:
            raise AssertionError("driver eindigde met %s (sanitizer?):\n%s" % (rc, err))

    # ---- HTTP via HuisDev::handle ----
    def handle(self, method, path, token="", body=""):
        r = self.cmd("handle", method, path, token, body)
        return int(r[0]), r[1]

    def jh(self, method, path, token="", body=""):
        st, txt = self.handle(method, path, token, body)
        return st, json.loads(txt)

    def get(self, path, token=""):
        return self.jh("GET", path, token)

    def post(self, path, obj, token=TOKEN, ascii=True, raw=None):
        body = raw if raw is not None else json.dumps(obj, ensure_ascii=ascii)
        return self.jh("POST", path, token, body)

    def descriptor(self):
        st, txt = self.handle("GET", "/huisdev/descriptor")
        assert st == 200, (st, txt)
        return txt, json.loads(txt)

    def state(self, token=""):
        st, txt = self.handle("GET", "/huisdev/state", token)
        assert st == 200, (st, txt)
        return txt, json.loads(txt)

    def claim(self, token=TOKEN):
        st, j = self.post("/huisdev/claim", {"token": token}, token="")
        assert st == 200 and j == {"ok": True}, (st, j)

    def loop(self):
        self.cmd("loop")

    def udp_out(self):
        f = self.cmd("udp_out")
        return [(f[i], f[i + 1]) for i in range(0, len(f), 2)]

    def calls(self):
        return self.cmd("calls")

    def rev(self):
        return self.descriptor()[1]["rev"]


class Base(unittest.TestCase):
    def setUp(self):
        self.d = Dev()
        self.d.cmd("factory")
        self.addCleanup(self.d.close)

    def assertErr(self, res, status, error):
        st, j = res
        self.assertEqual(st, status, j)
        self.assertEqual(j.get("ok"), False, j)
        self.assertEqual(j.get("error"), error, j)
        self.assertIn("reason", j)

    def assertResult(self, res, result, reason=None):
        st, j = res
        self.assertEqual(st, 200, j)
        self.assertEqual(j["result"], result, j)
        self.assertEqual(j["ok"], result != "rejected", j)
        if reason is not None:
            self.assertEqual(j["reason"], reason)
