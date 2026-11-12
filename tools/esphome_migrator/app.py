"""Kleine GUI: ESPHome-toestellen op het netwerk zoeken, hun oude YAML omzetten naar een recente ESPHome en uploaden.

Start met run.bat (maakt de omgeving met ESPHome aan). Zie README.md.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import migrate
import scan

WORK = Path(os.environ.get("KK_ESPHOME_WORK") or (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "kk-esphome"))
SETTINGS = WORK / "settings.json"
DEFAULT_SRC = Path.home() / "Downloads" / "esphome_projects-main.zip"
OTA_LIMIT_1M = 480_000  # ruwweg de ruimte per OTA-slot op een toestel met 1 MB flash


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(d: dict) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(d, indent=1), encoding="utf-8")


def read_sources(path: Path) -> dict[str, str]:
    """Alle .yaml-bestanden uit een zip of map: bestandsnaam -> tekst."""
    out: dict[str, str] = {}
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.lower().endswith(".yaml") and "/" not in n.strip("/").split("/", 1)[-1]:
                    out[Path(n).name] = z.read(n).decode("utf-8-sig", "replace")
    elif path.is_dir():
        for f in path.glob("*.yaml"):
            out[f.name] = f.read_text(encoding="utf-8-sig", errors="replace")
    return out


def esphome_name(text: str) -> str:
    m = re.search(r"^esphome:\s*(?:\n[ \t]+[^\n]*)*?\n[ \t]+name:\s*['\"]?([A-Za-z0-9_-]+)", text, re.M)
    return m.group(1) if m else ""


class App(tk.Tk):
    COLS = [("ip", "IP", 110), ("name", "Naam", 120), ("version", "ESPHome", 80), ("compiled", "Gebouwd", 150), ("model", "Board", 90),
            ("mac", "MAC", 125), ("yaml", "YAML", 150), ("status", "Status", 200)]

    def __init__(self) -> None:
        super().__init__()
        self.title("ESPHome migratie")
        self.geometry("1150x740")
        WORK.mkdir(parents=True, exist_ok=True)
        self.cfg = load_settings()
        self.q: queue.Queue = queue.Queue()
        self.devs: dict[str, scan.Device] = {}
        self.src: dict[str, str] = {}
        self.by_name: dict[str, list[str]] = {}
        self.stop = threading.Event()
        self.busy = False
        self.compiled: dict[str, int] = {}  # ip -> bytes van de laatst gecompileerde firmware
        self._ui()
        self.after(100, self._pump)
        self.load_src(Path(self.cfg.get("src") or DEFAULT_SRC))

    # ------------------------------------------------------------ opbouw

    def _ui(self) -> None:
        pad = {"padx": 4, "pady": 3}
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Netwerken").grid(row=0, column=0, sticky="w")
        self.nets = tk.StringVar(value=self.cfg.get("nets") or ", ".join(scan.local_networks()))
        ttk.Entry(top, textvariable=self.nets, width=44).grid(row=0, column=1, **pad)
        self.b_scan = ttk.Button(top, text="Scannen", command=self.do_scan)
        self.b_scan.grid(row=0, column=2, **pad)
        ttk.Button(top, text="Stop", command=self.stop.set).grid(row=0, column=3, **pad)
        ttk.Label(top, text="IP handmatig").grid(row=0, column=4, padx=(20, 4))
        self.ip = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.ip, width=16)
        e.grid(row=0, column=5, **pad)
        e.bind("<Return>", lambda _: self.do_manual())
        ttk.Button(top, text="Opvragen", command=self.do_manual).grid(row=0, column=6, **pad)

        src = ttk.Frame(self)
        src.pack(fill="x", **pad)
        ttk.Label(src, text="Oude YAML (zip of map)").grid(row=0, column=0, sticky="w")
        self.srcvar = tk.StringVar(value=str(self.cfg.get("src") or DEFAULT_SRC))
        ttk.Entry(src, textvariable=self.srcvar, width=60).grid(row=0, column=1, **pad)
        ttk.Button(src, text="Kies…", command=self.pick_src).grid(row=0, column=2, **pad)
        self.srclbl = ttk.Label(src, text="")
        self.srclbl.grid(row=0, column=3, **pad)
        ttk.Label(src, text="Standaard-wifi (voor YAML's zonder wifi-gegevens)").grid(row=1, column=0, sticky="w")
        self.wssid, self.wpw = tk.StringVar(value=self.cfg.get("wssid", "")), tk.StringVar(value=self.cfg.get("wpw", ""))
        f = ttk.Frame(src)
        f.grid(row=1, column=1, sticky="w")
        ttk.Entry(f, textvariable=self.wssid, width=24).pack(side="left", padx=2)
        ttk.Entry(f, textvariable=self.wpw, width=24, show="•").pack(side="left", padx=2)

        mid = ttk.PanedWindow(self, orient="vertical")
        mid.pack(fill="both", expand=True, **pad)
        tf = ttk.Frame(mid)
        self.tree = ttk.Treeview(tf, columns=[c[0] for c in self.COLS], show="headings", selectmode="extended", height=9)
        for k, t, w in self.COLS:
            self.tree.heading(k, text=t)
            self.tree.column(k, width=w, anchor="w")
        sb = ttk.Scrollbar(tf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.on_select())
        mid.add(tf, weight=1)

        bf = ttk.Frame(mid)
        bar = ttk.Frame(bf)
        bar.pack(fill="x")
        ttk.Label(bar, text="YAML:").pack(side="left")
        self.pick = tk.StringVar()
        self.cb = ttk.Combobox(bar, textvariable=self.pick, width=30, state="readonly")
        self.cb.pack(side="left", padx=4)
        self.cb.bind("<<ComboboxSelected>>", lambda _: self.do_generate())
        self.btns = []
        for text, fn in (("Genereer", self.do_generate), ("Valideer", self.do_validate), ("Compileer", self.do_compile),
                         ("Upload (OTA)", self.do_upload), ("Bulk…", self.do_bulk), ("Open werkmap", lambda: os.startfile(WORK))):
            b = ttk.Button(bar, text=text, command=fn)
            b.pack(side="left", padx=3)
            self.btns.append(b)
        self.stop_on_fail = tk.BooleanVar(value=True)
        self.skip_recent = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Bulk: stop bij eerste fout", variable=self.stop_on_fail).pack(side="left", padx=(12, 2))
        ttk.Checkbutton(bar, text="sla recente over", variable=self.skip_recent).pack(side="left", padx=2)
        ttk.Button(bar, text="Alles selecteren", command=lambda: self.tree.selection_set(self.tree.get_children())).pack(side="right", padx=3)
        nb = ttk.Notebook(bf)
        nb.pack(fill="both", expand=True, pady=3)
        self.txt = tk.Text(nb, height=14, wrap="none", font=("Consolas", 10), undo=True)
        self.log = tk.Text(nb, height=14, wrap="word", font=("Consolas", 9), state="disabled")
        nb.add(self.txt, text="Nieuwe YAML (bewerkbaar)")
        nb.add(self.log, text="Log")
        self.nb = nb
        mid.add(bf, weight=2)
        self.status = ttk.Label(self, text="", anchor="w")
        self.status.pack(fill="x", padx=6, pady=2)

    # ------------------------------------------------------------ hulp

    def say(self, msg: str) -> None:
        self.q.put(("log", msg))

    def _pump(self) -> None:
        try:
            while True:
                kind, *a = self.q.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", a[0].rstrip("\n") + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "status":
                    self.status.configure(text=a[0])
                elif kind == "dev":
                    self.show_dev(a[0])
                elif kind == "row":
                    if self.tree.exists(a[0]):
                        self.tree.set(a[0], "status", a[1])
                elif kind == "busy":
                    self.busy = a[0]
                    st = "disabled" if a[0] else "normal"
                    self.b_scan.configure(state=st)
                    for b in self.btns[:5]:
                        b.configure(state=st)
                elif kind == "text":
                    self.txt.delete("1.0", "end")
                    self.txt.insert("1.0", a[0])
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def save_cfg(self) -> None:
        self.cfg.update(nets=self.nets.get(), src=self.srcvar.get(), wssid=self.wssid.get(), wpw=self.wpw.get())
        save_settings(self.cfg)

    def worker(self, fn, *a) -> None:
        if self.busy:
            return
        self.q.put(("busy", True))

        def run() -> None:
            try:
                fn(*a)
            except Exception as e:  # noqa: BLE001  -- de GUI mag nooit stil blijven staan
                self.say(f"FOUT: {e.__class__.__name__}: {e}")
            finally:
                self.q.put(("busy", False))

        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------ bron-YAML

    def pick_src(self) -> None:
        p = filedialog.askopenfilename(title="Zip met de oude YAML's", filetypes=[("zip", "*.zip")]) or filedialog.askdirectory(title="of een map")
        if p:
            self.srcvar.set(p)
            self.load_src(Path(p))

    def load_src(self, path: Path) -> None:
        try:
            self.src = read_sources(path)
        except (OSError, zipfile.BadZipFile) as e:
            self.src = {}
            self.srclbl.configure(text=f"niet te lezen: {e}")
            return
        self.by_name = {}
        for fn, text in self.src.items():
            n = esphome_name(text)
            if n:
                self.by_name.setdefault(n, []).append(fn)
        self.cb.configure(values=sorted(self.src, key=str.lower))
        self.srclbl.configure(text=f"{len(self.src)} bestanden, {len(self.by_name)} namen")
        self.save_cfg()

    def candidates(self, d: scan.Device) -> list[str]:
        c = list(self.by_name.get(d.name, []))
        stem = f"{d.name}.yaml"
        if stem in self.src and stem not in c:
            c.insert(0, stem)
        return sorted(c, key=lambda f: (f != stem, f.lower()))

    # ------------------------------------------------------------ scannen

    def do_scan(self) -> None:
        nets = [n.strip() for n in self.nets.get().split(",") if n.strip()]
        self.stop.clear()
        self.save_cfg()
        self.say(f"Scannen: {', '.join(nets)}")
        self.worker(lambda: scan.scan(nets, lambda d: self.q.put(("dev", d)), lambda m: self.q.put(("status", m)), self.stop))

    def do_manual(self) -> None:
        ip = self.ip.get().strip()
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
            messagebox.showerror("IP-adres", "Geef een IPv4-adres, bv. 192.168.1.50")
            return
        self.say(f"Opvragen {ip}…")
        self.worker(lambda: self.q.put(("dev", scan.query(ip))))

    def show_dev(self, d: scan.Device) -> None:
        self.devs[d.ip] = d
        cands = self.candidates(d)
        yml = cands[0] if cands else ""
        if len(cands) > 1:
            yml += f" (+{len(cands) - 1})"
        status = d.error or ("ok" + ("" if d.web else ", geen web_server"))
        vals = (d.ip, d.name, d.version, d.compiled, d.model, d.mac, yml or "geen match", status)
        if self.tree.exists(d.ip):
            self.tree.item(d.ip, values=vals)
        else:
            self.tree.insert("", "end", iid=d.ip, values=vals)
        self.say(f"{d.ip}: {d.name or '?'} ESPHome {d.version or '?'} {d.error}")

    def cur(self) -> scan.Device | None:
        s = self.tree.selection()
        return self.devs.get(s[0]) if s else None

    def on_select(self) -> None:
        d = self.cur()
        if not d:
            return
        c = self.candidates(d)
        self.pick.set(c[0] if c else "")
        self.txt.delete("1.0", "end")
        if c:
            self.do_generate()

    # ------------------------------------------------------------ genereren, valideren, compileren, uploaden

    def default_wifi(self) -> list[tuple[str, str]] | None:
        return [(self.wssid.get(), self.wpw.get())] if self.wssid.get() else None

    def do_generate(self) -> None:
        d = self.cur()
        fn = self.pick.get()
        if not d or not fn:
            messagebox.showinfo("YAML", "Kies een toestel en een YAML-bestand.")
            return
        self.save_cfg()
        try:
            r = migrate.migrate(self.src[fn], Path(fn).stem, self.default_wifi())
        except migrate.MigrateError as e:
            self.txt.delete("1.0", "end")
            self.txt.insert("1.0", f"# Niet automatisch om te zetten: {e}\n# Pas het bestand handmatig aan, of kies een ander bestand.\n\n{self.src[fn]}")
            self.say(f"{fn}: {e}")
            return
        migrate.write_secrets(WORK / "secrets.yaml", r.wifi)
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", r.yaml_text)
        self.say(f"{fn} → {r.name}.yaml. Wifi: {', '.join(s for s, _ in r.wifi)}")
        for n in r.notes:
            self.say(f"  - {n}")
        if r.name != d.name and d.name:
            self.say(f"  ! LET OP: de naam in de YAML ({r.name}) verschilt van het toestel ({d.name}).")

    def yaml_path(self) -> Path | None:
        d = self.cur()
        text = self.txt.get("1.0", "end").strip()
        if not d or not text or text.startswith("# Niet automatisch"):
            messagebox.showinfo("YAML", "Er is geen geldige gegenereerde YAML. Klik eerst op Genereer.")
            return None
        m = re.search(r"^\s+name:\s*(\S+)", text, re.M)
        p = WORK / f"{(m.group(1) if m else d.ip.replace('.', '_'))}.yaml"
        p.write_text(text + "\n", encoding="utf-8")
        return p

    def esphome(self, *args: str, dev: scan.Device | None = None) -> int:
        cmd = [sys.executable, "-m", "esphome", *args]
        self.say("$ esphome " + " ".join(args))
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "ESPHOME_DASHBOARD_USE_PING": "0"}
        p = subprocess.Popen(cmd, cwd=WORK, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        d = dev
        for line in p.stdout:  # type: ignore[union-attr]
            line = re.sub(r"\x1b\[[0-9;]*m", "", line)
            self.say(line)
            m = re.search(r"Flash: .*used (\d+) bytes from (\d+) bytes", line)
            if m and d:
                self.compiled[d.ip] = int(m.group(1))
        return p.wait()

    def do_validate(self) -> None:
        p = self.yaml_path()
        if p:
            self.nb.select(self.log)
            self.worker(lambda: self.say("→ YAML is geldig." if self.esphome("config", p.name) == 0 else "→ YAML heeft fouten (zie hierboven)."))

    def do_compile(self) -> None:
        p, d = self.yaml_path(), self.cur()
        if p and d:
            self.nb.select(self.log)
            self.worker(self._compile, p, d)

    def _compile(self, p: Path, d: scan.Device) -> bool:
        rc = self.esphome("compile", p.name, dev=d)
        size = self.compiled.get(d.ip)
        if rc != 0:
            self.say("→ compileren mislukt.")
            return False
        self.say(f"→ gecompileerd{f', {size // 1024} KB' if size else ''}.")
        if size and size > OTA_LIMIT_1M and ("1m" in (d.model or "").lower() or "8285" in (d.model or "")):
            self.say(f"  ! De firmware is groter dan ~{OTA_LIMIT_1M // 1024} KB: op een toestel met 1 MB flash past dat waarschijnlijk niet in een OTA-slot.")
        return True

    def do_upload(self) -> None:
        d, p = self.cur(), self.yaml_path()
        if not d or not p:
            return
        text = p.read_text(encoding="utf-8")
        ssids = sorted(set(re.findall(r"wifi_ssid_(\w+)", text)))
        secrets = (WORK / "secrets.yaml").read_text(encoding="utf-8") if (WORK / "secrets.yaml").exists() else ""
        names = [re.search(rf"^wifi_ssid_{s}:\s*(.+)$", secrets, re.M) for s in ssids]
        nets = ", ".join(m.group(1).strip("'\" ") for m in names if m)
        if not messagebox.askyesno(
            "Upload naar het toestel",
            f"Toestel {d.name} op {d.ip}\nNu: ESPHome {d.version or '?'} (gebouwd {d.compiled or '?'})\n\n"
            f"De nieuwe firmware zet het toestel op de wifi-netwerken:\n  {nets or '(geen!)'}\n"
            f"Is een van die gegevens verouderd, dan komt het toestel niet meer online en moet je het met een kabel herflashen.\n\n"
            f"Het toestel herstart en een lamp aan dit toestel kan even uit gaan. Doorgaan?",
            icon="warning",
        ):
            return
        self.nb.select(self.log)
        self.worker(self._upload, d, p)

    def _upload(self, d: scan.Device, p: Path) -> None:
        if not self._compile(p, d):
            return
        if self.esphome("upload", p.name, "--device", d.ip) != 0:
            self.say("→ upload mislukt. Het toestel draait nog zijn oude firmware.")
            return
        self.say("→ geüpload. Wachten tot het toestel terug is…")
        n = self._wait_back(d)
        if n:
            self.say(f"→ {n.name} is terug met ESPHome {n.version}.")
        else:
            self.say("→ het toestel antwoordt niet binnen 2 minuten. Kijk in je router of het op de wifi staat; zo niet: herflash met een kabel.")

    def _wait_back(self, d: scan.Device) -> scan.Device | None:
        t0 = time.time()
        time.sleep(8)
        while time.time() - t0 < 120:
            n = scan.query(d.ip, timeout=2)
            if n.version and not n.error:
                self.q.put(("dev", n))
                return n
            time.sleep(4)
        return None

    # ------------------------------------------------------------ bulk

    @staticmethod
    def _ver(v: str) -> tuple[int, int]:
        m = re.match(r"(\d+)\.(\d+)", v or "")
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    def do_bulk(self) -> None:
        sel = [self.devs[i] for i in self.tree.selection() if i in self.devs]
        if not sel:
            messagebox.showinfo("Bulk", "Selecteer eerst toestellen (Ctrl/Shift-klik, of Alles selecteren).")
            return
        self.save_cfg()
        try:
            current = self._ver(importlib.metadata.version("esphome"))
        except importlib.metadata.PackageNotFoundError:
            current = (0, 0)
        plan: list[tuple[scan.Device, str, migrate.Result]] = []
        skipped: list[str] = []
        for d in sel:
            c = self.candidates(d)
            if d.encrypted or not d.name:
                skipped.append(f"{d.ip}: versleuteld of naam onbekend")
            elif self.skip_recent.get() and d.version and self._ver(d.version) >= current:
                skipped.append(f"{d.name}: draait al ESPHome {d.version}")
            elif len(c) != 1 and not (c and c[0] == f"{d.name}.yaml"):
                skipped.append(f"{d.name}: {'geen YAML' if not c else 'meerdere YAML-kandidaten, kies er een en gebruik Upload'}")
            else:
                try:
                    r = migrate.migrate(self.src[c[0]], Path(c[0]).stem, self.default_wifi())
                except migrate.MigrateError as e:
                    skipped.append(f"{d.name}: {e}")
                    continue
                if r.name != d.name:
                    skipped.append(f"{d.name}: de naam in {c[0]} is {r.name}")
                    continue
                plan.append((d, c[0], r))
        if not plan:
            messagebox.showinfo("Bulk", "Niets om te doen.\n\n" + "\n".join(skipped[:15]))
            return
        nets = sorted({s for _, _, r in plan for s, _ in r.wifi})
        names = ", ".join(d.name for d, _, _ in plan[:25]) + ("…" if len(plan) > 25 else "")
        skip_txt = (f"Overgeslagen ({len(skipped)}):\n  " + "\n  ".join(skipped[:10]) + "\n\n") if skipped else ""
        if not messagebox.askyesno(
            "Bulk-upload",
            f"{len(plan)} toestellen worden één voor één omgezet en bijgewerkt:\n  {names}\n\n"
            f"Wifi-netwerken die ze krijgen:\n  {', '.join(nets)}\n\n{skip_txt}"
            "Controleer of die netwerken en wachtwoorden nog kloppen: een toestel dat niet op de wifi komt moet met een kabel opnieuw geflasht worden.\n"
            "Elk toestel herstart even. Doorgaan?",
            icon="warning",
        ):
            return
        for line in skipped:
            self.say(f"overgeslagen: {line}")
        self.stop.clear()
        self.nb.select(self.log)
        self.worker(self._bulk, plan, self.stop_on_fail.get())

    def _bulk(self, plan: list, stop_on_fail: bool) -> None:
        results: list[tuple[str, str]] = []
        for i, (d, _fn, r) in enumerate(plan, 1):
            if self.stop.is_set():
                results.append((d.name, "niet gedaan (gestopt)"))
                continue
            self.say(f"\n===== [{i}/{len(plan)}] {d.name} ({d.ip}) =====")
            self.q.put(("status", f"Bulk {i}/{len(plan)}: {d.name}"))
            res = self._one(d, r)
            results.append((d.name, res))
            self.q.put(("row", d.ip, res))
            if res != "bijgewerkt" and stop_on_fail:
                self.say(f"Gestopt na een fout bij {d.name}.")
                results += [(x[0].name, "niet gedaan (gestopt)") for x in plan[i:]]
                break
        self.say("\n===== Samenvatting =====")
        for n, res in results:
            self.say(f"  {n:<16} {res}")
        self.q.put(("status", f"Bulk klaar: {sum(1 for _, x in results if x == 'bijgewerkt')}/{len(plan)} bijgewerkt"))

    def _one(self, d: scan.Device, r: migrate.Result) -> str:
        migrate.write_secrets(WORK / "secrets.yaml", r.wifi)
        p = WORK / f"{r.name}.yaml"
        p.write_text(r.yaml_text + "\n", encoding="utf-8")
        if self.esphome("config", p.name, dev=d) != 0:
            return "YAML ongeldig"
        if not self._compile(p, d):
            return "compileren mislukt"
        size = self.compiled.get(d.ip)
        if size and size > OTA_LIMIT_1M and ("1m" in (d.model or "").lower() or "8285" in (d.model or "")):
            return "firmware te groot voor OTA"
        if self.esphome("upload", p.name, "--device", d.ip, dev=d) != 0:
            return "upload mislukt (toestel ongewijzigd)"
        n = self._wait_back(d)
        if not n:
            return "UPLOAD OK MAAR TOESTEL NIET TERUG: controleer wifi"
        if self._ver(n.version) <= self._ver(d.version):
            return f"upload OK maar versie nog {n.version}"
        return "bijgewerkt"


if __name__ == "__main__":
    App().mainloop()
