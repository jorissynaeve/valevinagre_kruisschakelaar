#!/usr/bin/env python3
"""Schrijft protocol/huisdev/manifest.json: protocolversie, headerversie en sha256 van de bestanden die clients ophalen.

Draai dit na elke wijziging aan HUISDEV.md, huisdev.h of tools/huisdev_sync.py, en commit het resultaat mee.
`--check` faalt als manifest.json niet meer klopt (voor CI of een pre-commit).
"""
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = {"huisdev.h": "huisdev.h", "HUISDEV.md": "HUISDEV.md", "huisdev_sync.py": os.path.join("tools", "huisdev_sync.py")}


def read(rel):
    with open(os.path.join(ROOT, rel), "rb") as f:
        return f.read()


def main():
    md = read("HUISDEV.md").decode("utf-8")
    hdr = read("huisdev.h").decode("utf-8")
    sync = read(os.path.join("tools", "huisdev_sync.py")).decode("utf-8")
    proto = re.search(r'huisdev-protocol:\s*"([0-9]+\.[0-9]+)"', md).group(1)
    header = re.search(r'#define HUISDEV_HEADER_VERSION "([^"]+)"', hdr).group(1)
    hproto = re.search(r'#define HUISDEV_PROTO "([^"]+)"', hdr).group(1)
    mhead = re.search(r'huisdev-header:\s*"([^"]+)"', md).group(1)
    if hproto != proto:
        sys.exit("HUISDEV_PROTO (%s) in huisdev.h wijkt af van het protocol in HUISDEV.md (%s)" % (hproto, proto))
    if mhead != header:
        sys.exit("huisdev-header in HUISDEV.md (%s) wijkt af van HUISDEV_HEADER_VERSION (%s)" % (mhead, header))
    sync_version = int(re.search(r"^SYNC_VERSION = (\d+)", sync, re.M).group(1))
    manifest = {
        "proto": proto,
        "header": header,
        "sync_version": sync_version,
        "files": {name: hashlib.sha256(read(rel).replace(bytes((13, 10)), bytes((10,)))).hexdigest() for name, rel in sorted(FILES.items())},
    }
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    path = os.path.join(ROOT, "manifest.json")
    if "--check" in sys.argv:
        cur = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        if cur != text:
            sys.exit("manifest.json is verouderd: draai tools/make_manifest.py")
        print("manifest.json klopt (protocol %s, header %s)" % (proto, header))
        return
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("manifest.json geschreven: protocol %s, header %s" % (proto, header))


if __name__ == "__main__":
    main()
