"""
wraith.launcher — QtScrcpy-style control panel for the integrated client.

Left:  device list (double-click to connect) + a live adb console.
Right: Start Config (bitrate / size / fps / record path / options),
       USB line (server + device tools), and Wireless (adb over TCP).

"Start"/double-click spawns the mirror window (wraith.mirror) as a process;
every adb action streams into the log so it behaves like QtScrcpy's console.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog

from . import __version__
from .mirror import list_devices, KEYMAPS_DIR
from .runtime import adb_path, icon_ico, NO_WINDOW
from . import updater

# dark palette
BG = "#2b2b2b"; PANEL = "#323232"; FG = "#e8eaed"; FIELD = "#3c4043"
ACCENT = "#8ab4f8"; GREEN = "#3a7d3a"; LOG_BG = "#252525"; MUTE = "#9aa0a6"
CREATIONFLAGS = NO_WINDOW   # CREATE_NO_WINDOW — no flashing cmd windows


class Launcher:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Wraith")
        try:
            ico = icon_ico()
            if ico:
                self.root.iconbitmap(default=str(ico))
        except Exception:
            pass
        self.root.configure(bg=BG)
        self.root.geometry("930x600")
        self.root.minsize(900, 560)
        self.procs: list[subprocess.Popen] = []

        # config vars
        self.simple_var   = tk.BooleanVar(value=False)
        self.autoupd_var  = tk.BooleanVar(value=True)
        self.bitrate_var  = tk.StringVar(value="20")
        self.size_var     = tk.StringVar(value="1920")
        self.fps_var      = tk.StringVar(value="60")
        self.recfmt_var   = tk.StringVar(value="mp4")
        self.codec_var    = tk.StringVar(value="H.265 (HEVC)")
        self.preset_var   = tk.StringVar(value="Quality")   # defaults match Quality
        self.lockori_var  = tk.StringVar(value="no lock")
        self.savepath_var = tk.StringVar(value=str(Path.home() / "Videos" / "Wraith"))
        self.keymap_var   = tk.StringVar()
        self.devname_var  = tk.StringVar(value="Phone")
        self.serial_var   = tk.StringVar()
        self.wip_var      = tk.StringVar(value="192.168.1.4")
        self.wport_var    = tk.StringVar(value="5555")
        self.boost_var    = tk.StringVar(value="4")
        self.micboost_var = tk.StringVar(value="1")
        self.cmd_var      = tk.StringVar(value="devices")
        # option checkboxes
        self.opt = {k: tk.BooleanVar(value=v) for k, v in {
            "record": False, "bg_record": False, "reverse": True, "show_fps": True,
            "always_top": False, "screen_off": False, "frameless": False,
            "show_toolbar": True, "stay_awake": True, "mic": True,
        }.items()}

        self._style()
        self._build()
        self.refresh_devices()
        self._refresh_keymaps()
        self.root.after(1500, self._auto_check_updates)   # silent check on startup

    # ------------------------------------------------------------------ style
    def _style(self):
        s = ttk.Style(self.root)
        try: s.theme_use("clam")
        except tk.TclError: pass
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        s.configure(".", background=BG, foreground=FG, fieldbackground=FIELD,
                    font=("Segoe UI", 9))
        s.configure("TFrame", background=BG)
        s.configure("Panel.TLabelframe", background=BG, foreground=ACCENT, bordercolor="#444")
        s.configure("Panel.TLabelframe.Label", background=BG, foreground=ACCENT,
                    font=("Segoe UI", 9, "bold"))
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("TButton", background=FIELD, foreground=FG, padding=(6, 3),
                    borderwidth=0)
        s.map("TButton", background=[("active", "#4a4f57")])
        s.configure("Go.TButton", background=GREEN, foreground="white",
                    font=("Segoe UI", 9, "bold"), padding=(6, 4))
        s.map("Go.TButton", background=[("active", "#48994a")])
        s.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=FG,
                    arrowcolor=FG)
        s.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)])
        s.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG)

    # ------------------------------------------------------------------ build
    def _build(self):
        root = ttk.Frame(self.root, padding=8)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1, uniform="col")
        root.columnconfigure(1, weight=1, uniform="col")
        root.rowconfigure(0, weight=1)
        self._build_left(root)
        self._build_right(root)

    # -- LEFT: devices + adb console -----------------------------------------
    def _build_left(self, parent):
        left = ttk.Frame(parent)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        left.rowconfigure(1, weight=1)
        left.rowconfigure(2, weight=2)
        left.columnconfigure(0, weight=1)

        top = ttk.Frame(left); top.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Checkbutton(top, text="Use Simple Mode", variable=self.simple_var).pack(side="left")
        self.update_btn = ttk.Button(top, text="⟳ Check for Updates",
                                     command=self.check_updates)
        self.update_btn.pack(side="right")
        ttk.Label(top, text=f"v{__version__}", foreground=MUTE).pack(side="right", padx=(0, 8))

        # device group
        g = ttk.LabelFrame(left, text="Devices", style="Panel.TLabelframe", padding=6)
        g.grid(row=1, column=0, sticky="nsew")
        g.columnconfigure(0, weight=1); g.columnconfigure(1, weight=1)
        ttk.Button(g, text="WIFI Connect", command=self.wireless_connect).grid(
            row=0, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(g, text="USB Connect", command=self.connect).grid(
            row=0, column=1, sticky="ew", padx=2, pady=2)
        row = ttk.Frame(g); row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 2))
        ttk.Label(row, text="Double-click a device to connect", foreground=MUTE).pack(side="left")
        ttk.Checkbutton(row, text="auto update", variable=self.autoupd_var).pack(side="right")
        lb = tk.Listbox(g, bg=FIELD, fg=FG, selectbackground=ACCENT, selectforeground="#000",
                        borderwidth=0, highlightthickness=0, height=6, activestyle="none")
        lb.grid(row=2, column=0, columnspan=2, sticky="nsew")
        g.rowconfigure(2, weight=1)
        lb.bind("<Double-Button-1>", lambda e: self.connect())
        lb.bind("<<ListboxSelect>>", self._on_pick)
        self.devlist = lb

        # adb console group
        ag = ttk.LabelFrame(left, text="adb", style="Panel.TLabelframe", padding=6)
        ag.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        ag.columnconfigure(0, weight=1); ag.rowconfigure(1, weight=1)
        cmdrow = ttk.Frame(ag); cmdrow.grid(row=0, column=0, sticky="ew")
        cmdrow.columnconfigure(0, weight=1)
        e = ttk.Entry(cmdrow, textvariable=self.cmd_var)
        e.grid(row=0, column=0, sticky="ew")
        e.bind("<Return>", lambda ev: self.exec_adb())
        ttk.Button(cmdrow, text="execute", command=self.exec_adb).grid(row=0, column=1, padx=(4, 0))
        ttk.Button(cmdrow, text="clear", command=self.clear_log).grid(row=0, column=2, padx=(4, 0))
        logwrap = ttk.Frame(ag); logwrap.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        logwrap.columnconfigure(0, weight=1); logwrap.rowconfigure(0, weight=1)
        self.log = tk.Text(logwrap, bg=LOG_BG, fg="#cdd3da", insertbackground=FG,
                           borderwidth=0, highlightthickness=0, wrap="word",
                           font=("Consolas", 9))
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(logwrap, command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=sb.set, state="disabled")

    # -- RIGHT: config / usb line / wireless ---------------------------------
    def _build_right(self, parent):
        right = ttk.Frame(parent)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)

        # ---- Start Config
        cfg = ttk.LabelFrame(right, text="Start Config", style="Panel.TLabelframe", padding=8)
        cfg.grid(row=0, column=0, sticky="ew")
        for c in (1, 3): cfg.columnconfigure(c, weight=1)

        def field(r, label, c=0):
            ttk.Label(cfg, text=label).grid(row=r, column=c, sticky="w", padx=2, pady=3)

        field(0, "bit rate")
        br = ttk.Frame(cfg); br.grid(row=0, column=1, sticky="ew", padx=2)
        ttk.Entry(br, textvariable=self.bitrate_var, width=8).pack(side="left")
        ttk.Label(br, text="Mbps").pack(side="left", padx=(4, 0))
        field(0, "max size", c=2)
        ttk.Combobox(cfg, textvariable=self.size_var, width=10, state="readonly",
                     values=["1024", "1280", "1600", "1920", "2400", "0 (native)"]).grid(
            row=0, column=3, sticky="ew", padx=2)

        field(1, "fps")
        ttk.Combobox(cfg, textvariable=self.fps_var, width=10, state="readonly",
                     values=["30", "60", "90", "120"]).grid(row=1, column=1, sticky="ew", padx=2)
        field(1, "codec", c=2)
        ttk.Combobox(cfg, textvariable=self.codec_var, width=10, state="readonly",
                     values=["H.265 (HEVC)", "H.264"]).grid(row=1, column=3, sticky="ew", padx=2)

        field(2, "lock orientation")
        ttk.Combobox(cfg, textvariable=self.lockori_var, width=10, state="readonly",
                     values=["no lock", "0", "90", "180", "270"]).grid(
            row=2, column=1, sticky="ew", padx=2)
        field(2, "keymap", c=2)
        kmrow = ttk.Frame(cfg); kmrow.grid(row=2, column=3, sticky="ew", padx=2)
        self.keymap_menu = ttk.Combobox(kmrow, textvariable=self.keymap_var, state="readonly")
        self.keymap_menu.pack(side="left", fill="x", expand=True)
        ttk.Button(kmrow, text="↻", width=2, command=self._refresh_keymaps).pack(side="left", padx=(3, 0))

        field(3, "record save path")
        pr = ttk.Frame(cfg); pr.grid(row=3, column=1, columnspan=3, sticky="ew", padx=2)
        pr.columnconfigure(0, weight=1)
        ttk.Entry(pr, textvariable=self.savepath_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(pr, text="select path", command=self._pick_path).grid(row=0, column=1, padx=(4, 0))

        field(4, "audio boost")
        ttk.Combobox(cfg, textvariable=self.boost_var, width=10, state="readonly",
                     values=["1", "2", "3", "4", "6", "8", "10", "12", "16"]).grid(
            row=4, column=1, sticky="ew", padx=2)
        field(4, "mic level", c=2)
        ttk.Combobox(cfg, textvariable=self.micboost_var, width=10, state="readonly",
                     values=["0.5", "0.75", "1", "1.5", "2"]).grid(row=4, column=3, sticky="ew", padx=2)

        # option checkboxes grid
        opts = ttk.Frame(cfg); opts.grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))
        checks = [
            ("record screen", "record"), ("background record", "bg_record"),
            ("reverse connection", "reverse"), ("show fps", "show_fps"),
            ("always on top", "always_top"), ("screen-off", "screen_off"),
            ("frameless", "frameless"), ("show toolbar", "show_toolbar"),
            ("capture mic", "mic"), ("stay awake", "stay_awake"),
        ]
        for i, (lbl, key) in enumerate(checks):
            ttk.Checkbutton(opts, text=lbl, variable=self.opt[key]).grid(
                row=i // 2, column=i % 2, sticky="w", padx=4, pady=1)

        # performance preset — one-click tuning so weak PCs don't have to guess
        field(6, "performance")
        pre = ttk.Combobox(cfg, textvariable=self.preset_var, width=10, state="readonly",
                           values=["Low-end PC", "Balanced", "Quality", "Custom"])
        pre.grid(row=6, column=1, sticky="ew", padx=2)
        pre.bind("<<ComboboxSelected>>", self._apply_preset)
        ttk.Label(cfg, text="Low-end = 1024p · 30fps · 4Mbps · H.264",
                  foreground=MUTE).grid(row=6, column=2, columnspan=2, sticky="w", padx=2)

        ttk.Button(cfg, text="▶  Start", style="Go.TButton", command=self.connect).grid(
            row=7, column=0, columnspan=4, sticky="ew", pady=(8, 0))

        # ---- USB line
        usb = ttk.LabelFrame(right, text="USB line", style="Panel.TLabelframe", padding=8)
        usb.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        usb.columnconfigure(1, weight=1); usb.columnconfigure(3, weight=1)
        ttk.Label(usb, text="device name").grid(row=0, column=0, sticky="w", padx=2, pady=2)
        ttk.Entry(usb, textvariable=self.devname_var).grid(row=0, column=1, columnspan=2, sticky="ew", padx=2)
        ttk.Button(usb, text="update name", command=self._update_name).grid(row=0, column=3, sticky="ew", padx=2)
        ttk.Label(usb, text="device serial").grid(row=1, column=0, sticky="w", padx=2, pady=2)
        self.serial_menu = ttk.Combobox(usb, textvariable=self.serial_var, state="readonly")
        self.serial_menu.grid(row=1, column=1, sticky="ew", padx=2)
        ttk.Button(usb, text="start server", command=self.connect).grid(row=1, column=2, sticky="ew", padx=2)
        ttk.Button(usb, text="stop server", command=self.stop_server).grid(row=1, column=3, sticky="ew", padx=2)
        for i, (lbl, fn) in enumerate([
            ("stop all server", self.stop_server), ("refresh devices", self.refresh_devices),
            ("get device IP", self.get_device_ip), ("wake / power", lambda: self._key(26)),
        ]):
            ttk.Button(usb, text=lbl, command=fn).grid(row=2, column=i, sticky="ew", padx=2, pady=(4, 0))

        # ---- Wireless
        wl = ttk.LabelFrame(right, text="Wireless", style="Panel.TLabelframe", padding=8)
        wl.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        wl.columnconfigure(0, weight=1)
        ttk.Entry(wl, textvariable=self.wip_var).grid(row=0, column=0, sticky="ew", padx=2)
        ttk.Label(wl, text=":").grid(row=0, column=1)
        ttk.Entry(wl, textvariable=self.wport_var, width=7).grid(row=0, column=2, padx=2)
        ttk.Button(wl, text="wireless connect", command=self.wireless_connect).grid(row=0, column=3, padx=2)
        ttk.Button(wl, text="wireless disconnect", command=self.wireless_disconnect).grid(row=0, column=4, padx=2)

        self.status = ttk.Label(right, text="", foreground=MUTE)
        self.status.grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Label(right, text="In mirror: CTRL=mode · type in MOUSE mode to chat · "
                  "F10=edit keymap · F12=record · F9=quit",
                  foreground=MUTE).grid(row=4, column=0, sticky="w")
        ttk.Label(right, text="Low lag: max size 1920 = sharpest your screen shows; "
                  "'native' adds delay with no visible gain on a 1080p display.",
                  foreground=MUTE, wraplength=440, justify="left").grid(
                      row=5, column=0, sticky="w", pady=(2, 0))

    # --------------------------------------------------------------- logging
    def _log(self, text):
        def append():
            self.log.configure(state="normal")
            self.log.insert("end", text.rstrip() + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(0, append)

    def clear_log(self):
        self.log.configure(state="normal"); self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _set_status(self, t):
        self.root.after(0, lambda: self.status.configure(text=t))

    # ------------------------------------------------------------- adb plumbing
    def _adb_async(self, args, echo=True):
        """Run [adb] + args off the UI thread; stream stdout/stderr to the log."""
        def work():
            cmd = [adb_path()] + args
            if echo:
                self._log("adb " + " ".join(args))
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                   creationflags=CREATIONFLAGS)
                out = (p.stdout or "") + (p.stderr or "")
                for line in out.splitlines():
                    self._log("  " + line)
                if not out.strip():
                    self._log("  (ok)")
            except Exception as exc:
                self._log(f"  ! {exc}")
        threading.Thread(target=work, daemon=True).start()

    def exec_adb(self):
        raw = self.cmd_var.get().strip()
        if not raw:
            return
        self._adb_async(raw.split())

    # ------------------------------------------------------------- device ops
    def refresh_devices(self):
        def work():
            devs = list_devices()
            def fill():
                self.devlist.delete(0, "end")
                for d in devs:
                    self.devlist.insert("end", f"{self.devname_var.get()}-{d}")
                self.serial_menu.configure(values=devs or ["(none)"])
                if devs and not self.serial_var.get():
                    self.serial_var.set(devs[0])
                self.status.configure(text=f"{len(devs)} device(s)" if devs
                                      else "No device — plug in & enable USB debugging")
            self.root.after(0, fill)
            self._log(f"adb devices -> {', '.join(devs) if devs else 'none'}")
        threading.Thread(target=work, daemon=True).start()

    def _on_pick(self, _ev=None):
        sel = self.devlist.curselection()
        if sel:
            txt = self.devlist.get(sel[0])
            self.serial_var.set(txt.split("-", 1)[-1])

    def _refresh_keymaps(self):
        kms = sorted(p.name for p in KEYMAPS_DIR.glob("*.json"))
        self.keymap_menu.configure(values=kms or ["(none)"])
        if kms and self.keymap_var.get() not in kms:
            self.keymap_var.set("df.json" if "df.json" in kms else kms[0])

    def _pick_path(self):
        d = filedialog.askdirectory(initialdir=self.savepath_var.get() or str(Path.home()))
        if d:
            self.savepath_var.set(d)

    def _apply_preset(self, _ev=None):
        """One-click tuning. Low-end favors small frames + H.264 (decodes cheaply
        even without HEVC hardware); Quality matches the historical defaults;
        Custom leaves whatever the user typed alone."""
        presets = {
            "Low-end PC": dict(size="1024", fps="30", bitrate="4",  codec="H.264"),
            "Balanced":   dict(size="1280", fps="60", bitrate="8",  codec="H.265 (HEVC)"),
            "Quality":    dict(size="1920", fps="60", bitrate="20", codec="H.265 (HEVC)"),
        }
        p = presets.get(self.preset_var.get())
        if not p:
            return                                   # Custom — don't touch fields
        self.size_var.set(p["size"]); self.fps_var.set(p["fps"])
        self.bitrate_var.set(p["bitrate"]); self.codec_var.set(p["codec"])
        self._log(f"preset applied: {self.preset_var.get()} "
                  f"({p['size']}p, {p['fps']}fps, {p['bitrate']}Mbps, {p['codec']})")

    def _update_name(self):
        self.refresh_devices()
        self._log(f"device label set to '{self.devname_var.get()}'")

    def get_device_ip(self):
        dev = self.serial_var.get()
        if not dev or dev == "(none)":
            self._log("get device IP: no device"); return
        def work():
            try:
                out = subprocess.run([adb_path(), "-s", dev, "shell", "ip", "-f", "inet",
                                      "addr", "show", "wlan0"], capture_output=True,
                                     text=True, timeout=15, creationflags=CREATIONFLAGS).stdout
                import re
                m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
                if m:
                    self.wip_var.set(m.group(1))
                    self._log(f"device IP: {m.group(1)}")
                else:
                    self._log("device IP not found (Wi-Fi on?)")
            except Exception as exc:
                self._log(f"get device IP failed: {exc}")
        threading.Thread(target=work, daemon=True).start()

    def _key(self, code):
        dev = self.serial_var.get()
        if dev and dev != "(none)":
            self._adb_async(["-s", dev, "shell", "input", "keyevent", str(code)])

    # ------------------------------------------------------------- wireless
    def wireless_connect(self):
        addr = f"{self.wip_var.get()}:{self.wport_var.get()}"
        self._adb_async(["connect", addr])
        self.root.after(1500, self.refresh_devices)

    def wireless_disconnect(self):
        self._adb_async(["disconnect", f"{self.wip_var.get()}:{self.wport_var.get()}"])
        self.root.after(1200, self.refresh_devices)

    # ------------------------------------------------------------- mirror spawn
    def stop_server(self):
        n = 0
        for p in self.procs:
            if p.poll() is None:
                p.terminate(); n += 1
        self.procs = [p for p in self.procs if p.poll() is None]
        self._log(f"stopped {n} mirror session(s)")
        self._set_status(f"stopped {n} session(s)")

    def connect(self):
        dev = self.serial_var.get()
        if not dev or dev == "(none)":
            self._log("Start: no device selected"); return
        km = self.keymap_var.get() or "df.json"
        size = self.size_var.get().split()[0]
        if getattr(sys, "frozen", False):
            launch = [sys.executable, "mirror"]
        else:
            launch = [sys.executable, "-m", "wraith.mirror"]
        codec = "h264" if self.codec_var.get().startswith("H.264") else "h265"
        args = launch + [
            "--serial", dev, "--keymap", km, "--max-size", size,
            "--bitrate", str(int(float(self.bitrate_var.get())) * 1_000_000),
            "--fps", self.fps_var.get(), "--gain", self.boost_var.get(),
            "--mic-gain", self.micboost_var.get(),
            "--save-dir", self.savepath_var.get(), "--codec", codec,
        ]
        if not self.opt["mic"].get():
            args.append("--no-mic")
        if self.opt["screen_off"].get():
            args.append("--screen-off")
        if not self.opt["show_toolbar"].get():
            args.append("--no-toolbar")
        try:
            self.procs.append(subprocess.Popen(args, cwd=str(KEYMAPS_DIR.parent),
                                               creationflags=CREATIONFLAGS))
            self._log(f"▶ launching mirror on {dev} ({km}, {size}p, {self.fps_var.get()}fps)")
            self._set_status(f"Launched on {dev}. CTRL=mode, F10=edit, F9=quit.")
        except Exception as exc:
            self._log(f"launch failed: {exc}")

    # ------------------------------------------------------------- updates
    def _auto_check_updates(self):
        threading.Thread(target=lambda: self._do_check(silent=True), daemon=True).start()

    def check_updates(self):
        self._set_status("checking for updates…")
        threading.Thread(target=lambda: self._do_check(silent=False), daemon=True).start()

    def _do_check(self, silent):
        info = updater.check()
        if info:
            self.root.after(0, lambda: self._offer_update(info))
        elif not silent:
            self.root.after(0, lambda: self._set_status(f"up to date (v{__version__})"))

    def _offer_update(self, info):
        from tkinter import messagebox
        ver, notes = info.get("version"), info.get("notes", "")
        self._log(f"update available: v{ver}  {notes}")
        if not messagebox.askyesno(
                "Wraith update",
                f"Version {ver} is available (you have {__version__}).\n\n{notes}\n\n"
                "Download and install now? Wraith will close to finish."):
            return
        threading.Thread(target=lambda: self._do_update(info), daemon=True).start()

    def _do_update(self, info):
        import tempfile
        self.stop_server()                      # release any locked files
        dest = os.path.join(tempfile.gettempdir(), os.path.basename(info["url"]) or "Wraith-Setup.exe")
        try:
            self._log(f"downloading v{info['version']}…")
            updater.download(info["url"], dest,
                             progress=lambda f: self._set_status(f"downloading update… {int(f*100)}%"))
            self._log("launching installer — Wraith will close.")
            updater.run_installer(dest)
            self.root.after(400, self.root.destroy)
        except Exception as exc:
            self._log(f"update failed: {exc}")
            self._set_status("update failed — see log")

    def run(self):
        self.root.mainloop()


def main():
    Launcher().run()


if __name__ == "__main__":
    main()
