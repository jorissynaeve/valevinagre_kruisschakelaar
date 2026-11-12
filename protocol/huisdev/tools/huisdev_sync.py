#!/usr/bin/env python3
"""Haalt het actuele HUISDEV-protocol binnen in een sketchmap en controleert of de sketch erbij loopt.

Plaats een kopie van dit bestand in elke map met een HUISDEV-device en draai het voor elke build:

    python huisdev_sync.py                 ophalen + controleren (schrijft huisdev.h, HUISDEV.md, huisdev.lock)
    python huisdev_sync.py --check         alleen vergelijken, niets schrijven (voor een start-hook)
    python huisdev_sync.py --source PAD    een lokale kopie van protocol/huisdev als bron (offline, of voor het push)

Bron: standaard de hoofdtak van de repo op GitHub. Overschrijven met --source of de omgevingsvariabele HUISDEV_SOURCE
(een https-url of een mappad), en met --ref voor een andere tak of tag.

Alleen standaardbibliotheek, Python 3.8+. Elk bestand wordt gecontroleerd tegen de sha256 uit manifest.json.

Afsluitcodes: 0 in orde (of niet kunnen ophalen maar lokale kopie aanwezig), 1 fout,
              2 de sketch-marker loopt achter op het protocol (upgrade nodig, zie "Upgrade-gids" in HUISDEV.md),
              3 de sketch-marker is nieuwer dan het protocol of onleesbaar.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request

SYNC_VERSION = 1
DEFAULT_URL = "https://raw.githubusercontent.com/jorissynaeve/valevinagre_kruisschakelaar/{ref}/protocol/huisdev"
MARKER_RE = re.compile(r"huisdev-protocol:\s*([0-9]+)\.([0-9]+)")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def fetch(source, name):
    if re.match(r"^https?://", source):
        if not source.startswith("https://"):
            raise SystemExit("alleen https is toegestaan als bron")
        req = urllib.request.Request(source.rstrip("/") + "/" + name, headers={"User-Agent": "huisdev-sync"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()
    with open(os.path.join(source, name), "rb") as f:
        return f.read()


def sha(b):
    # regeleindes genormaliseerd: git en Windows mogen CRLF/LF door elkaar halen zonder dat de controle breekt
    return hashlib.sha256(b.replace(bytes((13, 10)), bytes((10,)))).hexdigest()


def sketch_markers(folder):
    out = []
    for fn in sorted(os.listdir(folder)):
        if fn.endswith(".ino"):
            with open(os.path.join(folder, fn), encoding="utf-8", errors="replace") as f:
                m = MARKER_RE.search(f.read(4000))
            out.append((fn, (int(m.group(1)), int(m.group(2))) if m else None))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)), help="sketchmap (standaard: map van dit script)")
    ap.add_argument("--source", default=os.environ.get("HUISDEV_SOURCE"), help="https-url of lokale map met protocol/huisdev")
    ap.add_argument("--ref", default="main", help="tak of tag op GitHub (standaard main)")
    ap.add_argument("--check", action="store_true", help="niets schrijven, alleen vergelijken; sluit altijd af met 0")
    args = ap.parse_args()

    folder = args.dir
    source = args.source or DEFAULT_URL.format(ref=args.ref)
    lock_path = os.path.join(folder, "huisdev.lock")
    local = {}
    if os.path.exists(lock_path):
        try:
            with open(lock_path, encoding="utf-8") as f:
                local = json.load(f)
        except (OSError, ValueError):
            local = {}

    try:
        manifest = json.loads(fetch(source, "manifest.json"))
        proto = manifest["proto"]
        files = manifest["files"]
    except Exception as e:  # netwerk, ontbrekend of kapot manifest
        have = os.path.exists(os.path.join(folder, "huisdev.h"))
        print("HUISDEV: kon het protocol niet ophalen (%s)." % e, file=sys.stderr)
        if have:
            print("HUISDEV: gebruik de lokale kopie (protocol %s, header %s)." % (local.get("proto", "?"), local.get("header", "?")))
        return 0 if (have or args.check) else 1

    changed = []
    for name, want in files.items():
        if not NAME_RE.match(name):
            print("HUISDEV: ongeldige bestandsnaam in manifest: %r" % name, file=sys.stderr)
            return 1
        path = os.path.join(folder, name)
        cur = None
        if os.path.exists(path):
            with open(path, "rb") as f:
                cur = sha(f.read())
        if cur == want:
            continue
        if args.check:
            changed.append(name)
            continue
        data = fetch(source, name)
        if sha(data) != want:
            print("HUISDEV: %s komt niet overeen met manifest.json (sha256), niet geschreven." % name, file=sys.stderr)
            return 1
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        changed.append(name)

    if not args.check:
        lock = {"proto": proto, "header": manifest.get("header"), "files": files}
        with open(lock_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(lock, f, indent=2, sort_keys=True)
            f.write("\n")

    verb = "te vernieuwen" if args.check else "vernieuwd"
    print("HUISDEV: protocol %s, header %s%s" % (proto, manifest.get("header"), ("; %s: %s" % (verb, ", ".join(changed))) if changed else " (actueel)"))
    if manifest.get("sync_version", SYNC_VERSION) > SYNC_VERSION:
        print("HUISDEV: dit script (v%d) is ouder dan de bron (v%d): kopieer protocol/huisdev/tools/huisdev_sync.py opnieuw."
              % (SYNC_VERSION, manifest["sync_version"]))

    want_proto = tuple(int(x) for x in proto.split("."))
    status = 0
    for fn, marker in sketch_markers(folder):
        if marker is None:
            print("HUISDEV: %s heeft geen marker '// huisdev-protocol: X.Y'." % fn)
            status = max(status, 3)
        elif marker < want_proto:
            print("HUISDEV: %s volgt protocol %d.%d, actueel is %s: volg de Upgrade-gids in HUISDEV.md en zet de marker bij."
                  % (fn, marker[0], marker[1], proto))
            status = 2
        elif marker > want_proto:
            print("HUISDEV: %s claimt protocol %d.%d, nieuwer dan de bron (%s)." % (fn, marker[0], marker[1], proto))
            status = max(status, 3)
        else:
            print("HUISDEV: %s loopt gelijk met het protocol." % fn)
    return 0 if args.check else status


if __name__ == "__main__":
    sys.exit(main())
