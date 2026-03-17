#!/usr/bin/env python3
"""
================================================================================
  CSN SAT Wind Tracker
  Author  : Michael Walker  VA3MW
  Date    : 2026-03-17
  Version : 2.0
================================================================================

PURPOSE
-------
  Automatically keeps a CSN SAT antenna controller pointed into the prevailing
  wind at Toronto Pearson (CYYZ).  The antenna is repositioned only when gusts
  exceed the configured threshold, ensuring it always faces into significant
  wind events.

  The script is "polite" — if another application (e.g. a satellite tracker)
  has sent positioning commands recently, this script backs off for
  IDLE_TIMEOUT seconds before resuming automatic control.

AUTO-DISCOVERY
--------------
  On startup the app listens on UDP port 9932 for broadcast packets from
  CSNTracker.  The source IP of the first SAT, packet received becomes the
  CSN SAT host address automatically.  If no broadcast arrives within 30
  seconds the user is prompted to enter the IP manually.

  After discovery the port-9932 listener continues running.  It watches for
  satellite tracking events and uses them to manage antenna control:
    SAT,START TRACK,name,catno  — satellite tracker starting; marks antenna IN USE
    SAT,AOS,az                  — satellite acquired;          marks antenna IN USE
    SAT,LOS,az                  — pass over; clears IN USE so wind tracking resumes

HOW IT WORKS
------------
  1. Discovery: listen on :9932 for up to 30 s to auto-find the CSN SAT IP.
     Fall back to a manual entry dialog if nothing is heard.

  2. A background listener thread watches UDP port 12001 for incoming
     <AZIMUTH> or <ELEVATION> commands from other applications.

  3. A second listener watches :9932 for CSNTracker broadcast events
     (START TRACK / AOS / LOS) to maintain the antenna-in-use flag.

  4. The Pause / Resume buttons (or P / R keys) let the operator manually
     suspend auto-updates without stopping the program.

  5. The worker thread runs immediately on startup then sleeps INTERVAL_SEC
     seconds between cycles.  Each cycle checks three guards in order:
       a) Manually paused?       → skip
       b) Antenna in use?        → skip (tracker active or recent ext. command)
       c) Gusts above threshold? → skip if not; move antenna if yes

THINGS YOU MAY NEED TO EDIT
----------------------------
  SAT_PORT       UDP port the SAT listens on for commands       (default 12000)
  LISTEN_PORT    UDP port monitored for other apps' commands    (default 12001)
  DISCOVERY_PORT UDP port CSNTracker broadcasts on             (default 9932)
  DISCOVERY_SECS Seconds to wait for auto-discovery            (default 30)
  INTERVAL_SEC   How often the worker loop runs, in seconds    (default 300)
  IDLE_TIMEOUT   Seconds of silence before antenna is free     (default 300)
  MIN_GUST_KT    Gust speed threshold in knots                 (default 15)
                 Antenna will NOT move if no gusts are reported
  ICAO_DEFAULT   Default airport code — user is prompted at startup (default CYYZ)

  SAT_HOST_DEFAULT is used only if discovery fails AND the user leaves the
  manual entry dialog blank.

KEYBOARD SHORTCUTS
------------------
  P  —  Pause all automatic antenna updates
  R  —  Resume automatic antenna updates

DEPENDENCIES
------------
  Python 3.8+  (tkinter ships with standard Python on Windows)
  requests  →  pip install requests

PSTROTATOR UDP PROTOCOL
-----------------------
  Commands are sent TO the SAT on UDP port 12000, wrapped in <PST>…</PST>.
  The SAT sends responses back to the originating IP on UDP port 12001.
    <PST><AZIMUTH>xxx.x</AZIMUTH></PST>
    <PST><ELEVATION>xxx.x</ELEVATION></PST>

CSNTRACKER BROADCAST PROTOCOL  (port 9932)
------------------------------------------
  SAT,START TRACK,<name>,<catno>   satellite tracker has started a pass
  SAT,AOS,<az>                     satellite acquired at azimuth <az>
  SAT,LOS,<az>                     satellite lost at azimuth <az> — pass over

METAR SOURCES  (tried in order, first success wins)
----------------------------------------------------
  1. https://tgftp.nws.noaa.gov  — NOAA plain-text server  (preferred)
  2. https://metar.vatsim.net    — VATSIM public feed       (fallback)
================================================================================
"""

import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.messagebox
import webbrowser
from datetime import datetime

import requests


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION  ←  edit these values to match your setup
# ══════════════════════════════════════════════════════════════════════════════

SAT_HOST_DEFAULT = "192.168.113.121"  # fallback if discovery fails and user
                                       # leaves the entry dialog blank
SAT_PORT         = 12000   # SAT listens for PSTRotator commands on this port
LISTEN_PORT      = 12001   # monitor for other apps' PSTRotator commands
DISCOVERY_PORT   = 9932    # CSNTracker broadcasts status on this port
DISCOVERY_SECS   = 30      # seconds to wait for a CSNTracker broadcast

INTERVAL_SEC = 300    # seconds between automatic wind checks  (300 = 5 min)
IDLE_TIMEOUT = 300    # seconds of silence before antenna is considered free
MIN_GUST_KT  = 15     # gusts must exceed this (kt) to trigger a move
                      # if no gust is reported in the METAR the antenna stays put

ICAO_DEFAULT = "CYYZ" # Default ICAO station code — user is prompted at startup

METAR_SOURCES = [
    "https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT",
    "https://metar.vatsim.net/{icao}",
]

# ══════════════════════════════════════════════════════════════════════════════
#  END OF CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════


# ── Colour palette (dark theme) ───────────────────────────────────────────────
C_WIN    = "#1e1e1e"
C_BG     = "#2b2b2b"
C_PANEL  = "#2d2d2d"
C_HDR    = "#0d2137"
C_BORDER = "#3a3a3a"
C_TEXT   = "#d4d4d4"
C_DIM    = "#6a6a6a"
C_GREEN  = "#4ec94e"
C_RED    = "#e05555"
C_ORANGE = "#e0943a"
C_YELLOW = "#d4c94a"
C_CYAN   = "#4ab8d4"
C_LOGBG  = "#141414"


# ══════════════════════════════════════════════════════════════════════════════
#  APPLICATION
# ══════════════════════════════════════════════════════════════════════════════

class App:
    """
    All tkinter widget access runs on the main thread.
    Background threads communicate via:
      self._q               — queue of (ts, tag, msg) log entries
      self.root.after(0, f) — schedule a one-shot GUI update on the main thread
    """

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("CSN SAT Wind Tracker  —  VA3MW")
        self.root.configure(bg=C_WIN)
        self.root.minsize(900, 640)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── Ask for ICAO station code before anything else is built ───────────
        self._icao = self._ask_icao()   # blocks on main thread via wait_window()

        # ── Runtime SAT host (set by discovery or user input) ─────────────────
        self._sat_host = SAT_HOST_DEFAULT

        # ── Last sent azimuth (int or None) — used by keepalive ───────────────
        self._last_az   = None
        self._az_lock   = threading.Lock()

        # ── Thread-safe shared state ──────────────────────────────────────────
        self._q             = queue.Queue()
        self._paused        = False
        self._pause_lock    = threading.Lock()
        self._last_cmd_time = 0.0           # epoch of last antenna-use event
        self._cmd_lock      = threading.Lock()
        self._next_check_at = time.time()
        self._wake          = threading.Event()  # set to interrupt worker sleep

        # Discovery sync — runner_9932 sets _discovery_done when it either finds
        # an IP or gives up.  _discovered_ip holds the result (None = not found).
        self._discovery_done = threading.Event()
        self._discovered_ip  = None             # set by runner_9932 on first packet

        # Manual IP dialog sync — worker posts the dialog to the main thread then
        # blocks on _ip_ready until the user clicks Connect.
        self._ip_ready  = threading.Event()
        self._ip_result = SAT_HOST_DEFAULT

        # ── Build UI ──────────────────────────────────────────────────────────
        self._build_ui()

        # ── Start background threads ──────────────────────────────────────────
        # runner_9932   — ONE socket on port 9932 handles both auto-discovery
        #                 and ongoing CSNTracker event monitoring.  It sets
        #                 _discovery_done when the discovery phase is complete.
        # worker        — waits for _discovery_done, then runs the wind loop.
        # listener_12001— watches for PSTRotator commands from other apps.
        # keepalive     — resends the last azimuth/elevation every 60 s.
        threading.Thread(target=self._runner_9932,     daemon=True, name="9932").start()
        threading.Thread(target=self._worker,          daemon=True, name="worker").start()
        threading.Thread(target=self._listener_12001,  daemon=True, name="lst-12001").start()
        threading.Thread(target=self._keepalive,       daemon=True, name="keepalive").start()

        # ── Recurring GUI callbacks ───────────────────────────────────────────
        self._drain_log()   # 100 ms — flush log queue → Text widget
        self._refresh()     # 1 s   — update mode badge + countdown

    # ══════════════════════════════════════════════════════════════════════════
    #  UI CONSTRUCTION
    # ══════════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        # Title bar
        bar = tk.Frame(self.root, bg=C_HDR, pady=14)
        bar.pack(fill="x")
        tk.Label(bar, text="  CSN SAT Wind Tracker",
                 bg=C_HDR, fg="white",
                 font=("Segoe UI", 15, "bold")).pack(side="left")
        tk.Label(bar, text="Michael Walker  VA3MW  ",
                 bg=C_HDR, fg="#7aaabf",
                 font=("Segoe UI", 10)).pack(side="right")

        # Three info cards
        row = tk.Frame(self.root, bg=C_BG)
        row.pack(fill="x", padx=12, pady=10)
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        row.columnconfigure(2, weight=1)
        self._build_card_status(row, 0)
        self._build_card_weather(row, 1)
        self._build_card_antenna(row, 2)

        # Divider
        tk.Frame(self.root, bg=C_BORDER, height=1).pack(fill="x", padx=12)

        # Control buttons
        btn_row = tk.Frame(self.root, bg=C_BG, pady=8)
        btn_row.pack(fill="x", padx=12)
        self._button(btn_row, "⏸   PAUSE",    "#3d1f00", C_ORANGE,
                     self._do_pause).pack(side="left", padx=(0, 8))
        self._button(btn_row, "▶   RESUME",   "#0f2e0f", C_GREEN,
                     self._do_resume).pack(side="left", padx=(0, 8))
        self._button(btn_row, "🖥  Shortcut",  "#1a1a2e", C_CYAN,
                     self._create_shortcut).pack(side="left")

        for key in ("<p>", "<P>"):
            self.root.bind(key, lambda _: self._do_pause())
        for key in ("<r>", "<R>"):
            self.root.bind(key, lambda _: self._do_resume())

        # Divider
        tk.Frame(self.root, bg=C_BORDER, height=1).pack(fill="x", padx=12)

        # Event log
        log_frame = tk.Frame(self.root, bg=C_BG)
        log_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        tk.Label(log_frame, text="  EVENT LOG",
                 bg=C_HDR, fg=C_DIM,
                 font=("Segoe UI", 8, "bold"),
                 anchor="w", pady=4).pack(fill="x")
        inner = tk.Frame(log_frame, bg=C_LOGBG)
        inner.pack(fill="both", expand=True)
        self._log_box = tk.Text(
            inner, bg=C_LOGBG, fg=C_TEXT,
            font=self._mono_font(9),
            wrap="none", state="disabled",
            relief="flat", padx=8, pady=6)
        self._log_box.pack(side="left", fill="both", expand=True)
        vsb = tk.Scrollbar(inner, orient="vertical", command=self._log_box.yview)
        vsb.pack(side="right", fill="y")
        self._log_box.configure(yscrollcommand=vsb.set)

        self._log_box.tag_config("info",     foreground=C_TEXT)
        self._log_box.tag_config("move",     foreground=C_GREEN)
        self._log_box.tag_config("skip",     foreground=C_DIM)
        self._log_box.tag_config("warn",     foreground=C_ORANGE)
        self._log_box.tag_config("error",    foreground=C_RED)
        self._log_box.tag_config("startup",  foreground=C_CYAN)
        self._log_box.tag_config("listen",   foreground="#6a9fbf")
        self._log_box.tag_config("discover", foreground="#c084fc")
        self._log_box.tag_config("tx",       foreground="#8888cc")
        self._log_box.tag_config("sat",      foreground="#f0a050")  # CSNTracker events

    # ── Card / field helpers ──────────────────────────────────────────────────

    def _mono_font(self, size: int):
        for name in ("Cascadia Code", "Consolas", "Courier New"):
            try:
                if name.lower() in [f.lower() for f in tk.font.families()]:
                    return (name, size)
            except Exception:
                pass
        return ("Courier New", size)

    def _card(self, parent, title: str, col: int) -> tk.Frame:
        outer = tk.Frame(parent, bg=C_PANEL,
                         highlightthickness=1, highlightbackground=C_BORDER)
        outer.grid(row=0, column=col, sticky="nsew", padx=5)
        tk.Label(outer, text=f"  {title}",
                 bg=C_HDR, fg="#aac8df",
                 font=("Segoe UI", 9, "bold"),
                 anchor="w", pady=6).pack(fill="x")
        body = tk.Frame(outer, bg=C_PANEL, padx=14, pady=10)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        return body

    def _field(self, parent, label: str, row: int, init: str = "—"):
        tk.Label(parent, text=label, bg=C_PANEL, fg=C_DIM,
                 font=("Segoe UI", 9), anchor="w"
                 ).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 2))
        var = tk.StringVar(value=init)
        lbl = tk.Label(parent, textvariable=var, bg=C_PANEL, fg=C_TEXT,
                       font=self._mono_font(10), anchor="w")
        lbl.grid(row=row, column=1, sticky="w", padx=(10, 0), pady=4)
        return var, lbl

    def _button(self, parent, text, bg, fg, cmd):
        return tk.Button(parent, text=text, command=cmd,
                         bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
                         font=("Segoe UI", 10, "bold"),
                         relief="flat", width=13, pady=7, cursor="hand2", bd=0)

    def _build_card_status(self, parent, col):
        c = self._card(parent, "STATUS", col)
        self._v_sat,      self._l_sat  = self._field(c, "CSN SAT",    0, "Discovering…")
        self._v_mode,     self._l_mode = self._field(c, "Mode",       1)
        self._v_interval, _            = self._field(c, "Interval",   2, f"{INTERVAL_SEC//60} min")
        self._v_idle,     _            = self._field(c, "Idle guard", 3, f"{IDLE_TIMEOUT//60} min")
        self._v_gust_thr, _            = self._field(c, "Gust min",   4, f"{MIN_GUST_KT} kt")
        self._v_next,     _            = self._field(c, "Next check", 5)
        self._v_sat_event, self._l_sat_event = self._field(c, "SAT event", 6)

    def _build_card_weather(self, parent, col):
        c = self._card(parent, f"WEATHER  ({self._icao})", col)
        self._v_metar, _            = self._field(c, "METAR",     0)
        self._v_wdir,  self._l_wdir = self._field(c, "Direction", 1)
        self._v_wspd,  _            = self._field(c, "Speed",     2)
        self._v_wgst,  self._l_wgst = self._field(c, "Gusts",     3)
        self._v_wtime, _            = self._field(c, "Updated",   4)

    def _build_card_antenna(self, parent, col):
        c = self._card(parent, "ANTENNA", col)
        self._v_az,     self._l_az     = self._field(c, "Azimuth",     0)
        self._v_el,     _              = self._field(c, "Elevation",   1, "0.0°")
        self._v_moved,  _              = self._field(c, "Last moved",  2, "Never")
        self._v_action, self._l_action = self._field(c, "Last action", 3)

    # ══════════════════════════════════════════════════════════════════════════
    #  RECURRING GUI CALLBACKS
    # ══════════════════════════════════════════════════════════════════════════

    def _refresh(self):
        with self._pause_lock:
            paused = self._paused
        with self._cmd_lock:
            idle = time.time() - self._last_cmd_time

        if paused:
            self._v_mode.set("⏸  PAUSED")
            self._l_mode.configure(fg=C_ORANGE)
        elif self._last_cmd_time > 0 and idle < IDLE_TIMEOUT:
            self._v_mode.set("⏳  ANTENNA IN USE")
            self._l_mode.configure(fg=C_CYAN)
        else:
            self._v_mode.set("●  RUNNING")
            self._l_mode.configure(fg=C_GREEN)

        secs_left = max(0, int(self._next_check_at - time.time()))
        m, s = divmod(secs_left, 60)
        self._v_next.set(f"{m:02d}:{s:02d}")
        self.root.after(1000, self._refresh)

    def _drain_log(self):
        try:
            while True:
                ts, tag, msg = self._q.get_nowait()
                self._log_box.configure(state="normal")
                self._log_box.insert("end", f"{ts}  {msg}\n", tag)
                self._log_box.see("end")
                self._log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log)

    # ══════════════════════════════════════════════════════════════════════════
    #  THREAD-SAFE HELPERS
    # ══════════════════════════════════════════════════════════════════════════

    def _log(self, level: str, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        if   "MOVING"      in msg:  tag = "move"
        elif "SKIPPED"    in msg:  tag = "skip"
        elif "TX →"       in msg:  tag = "tx"
        elif "[keepalive]"in msg:  tag = "tx"
        elif "[discover]" in msg:  tag = "discover"
        elif "[startup]"  in msg:  tag = "startup"
        elif "[sat]"      in msg:  tag = "sat"
        elif "[listener]" in msg:  tag = "listen"
        elif level == "WARNING":   tag = "warn"
        elif level == "ERROR":     tag = "error"
        else:                      tag = "info"
        self._q.put((ts, tag, msg))
        print(f"{ts}  {level:<7}  {msg}")

    def _ui_sat(self, online: bool):
        def _f():
            label = f"●  {self._sat_host}  ONLINE" if online \
                else f"●  {self._sat_host}  OFFLINE"
            self._v_sat.set(label)
            self._l_sat.configure(fg=C_GREEN if online else C_RED)
        self.root.after(0, _f)

    def _ui_sat_host_discovered(self, ip: str):
        """Update the STATUS card to show the newly discovered SAT IP."""
        def _f():
            self._v_sat.set(f"●  {ip}  (discovered)")
            self._l_sat.configure(fg=C_CYAN)
        self.root.after(0, _f)

    def _ui_sat_event(self, text: str, colour: str):
        def _f():
            self._v_sat_event.set(text)
            self._l_sat_event.configure(fg=colour)
        self.root.after(0, _f)

    def _ui_weather(self, raw: str, wdir: int, wspd: int, wgst):
        def _f():
            self._v_metar.set(raw if len(raw) <= 46 else raw[:43] + "…")
            self._v_wdir.set(f"{wdir}°")
            self._v_wspd.set(f"{wspd} kt")
            self._v_wtime.set(datetime.now().strftime("%H:%M:%S"))
            if wgst:
                self._v_wgst.set(f"{wgst} kt")
                self._l_wgst.configure(fg=C_RED if wgst > MIN_GUST_KT else C_YELLOW)
            else:
                self._v_wgst.set("none reported")
                self._l_wgst.configure(fg=C_DIM)
        self.root.after(0, _f)

    def _ui_moved(self, wdir: int):
        def _f():
            self._v_az.set(f"{wdir}.0°")
            self._l_az.configure(fg=C_GREEN)
            self._v_moved.set(datetime.now().strftime("%H:%M:%S"))
            self._v_action.set("MOVED")
            self._l_action.configure(fg=C_GREEN)
        self.root.after(0, _f)

    def _ui_skipped(self, reason: str):
        def _f():
            self._v_action.set(f"SKIPPED  ({reason})")
            self._l_action.configure(fg=C_DIM)
        self.root.after(0, _f)

    def _ask_icao(self) -> str:
        """
        Modal startup dialog — asks the user for their ICAO weather station code
        before the main UI is built.  Blocks the main thread via wait_window().
        Returns the validated (uppercased) code, or ICAO_DEFAULT if left blank.
        """
        result = [ICAO_DEFAULT]

        self.root.withdraw()            # hide main window while dialog is open

        dlg = tk.Toplevel(self.root)
        dlg.title("Weather Station Setup")
        dlg.configure(bg=C_BG)
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.grab_set()
        dlg.lift()
        dlg.focus_force()

        tk.Label(dlg,
                 text="Enter the ICAO code for your nearest airport weather station:",
                 bg=C_BG, fg=C_TEXT,
                 font=("Segoe UI", 10),
                 justify="left").pack(padx=28, pady=(22, 6), anchor="w")

        entry = tk.Entry(dlg, bg=C_PANEL, fg=C_TEXT,
                         font=self._mono_font(14),
                         insertbackground=C_TEXT,
                         relief="flat", width=10, justify="center")
        entry.insert(0, ICAO_DEFAULT)
        entry.pack(padx=28, pady=6)
        entry.focus_set()
        entry.select_range(0, "end")

        tk.Label(dlg,
                 text="Not sure? Look up your airport code here:",
                 bg=C_BG, fg=C_DIM,
                 font=("Segoe UI", 9)).pack(padx=28, pady=(12, 2), anchor="w")

        link = tk.Label(dlg,
                        text="  ourairports.com  →  search by city or airport name",
                        bg=C_BG, fg=C_CYAN,
                        font=("Segoe UI", 9, "underline"),
                        cursor="hand2")
        link.pack(padx=28, pady=(0, 18), anchor="w")
        link.bind("<Button-1>",
                  lambda _: webbrowser.open("https://ourairports.com"))

        def _ok():
            val = entry.get().strip().upper()
            result[0] = val if val else ICAO_DEFAULT
            dlg.destroy()

        tk.Button(dlg, text="OK — Start Tracking",
                  bg="#0f2e0f", fg=C_GREEN,
                  font=("Segoe UI", 10, "bold"),
                  relief="flat", padx=20, pady=6,
                  command=_ok).pack(pady=(0, 22))

        entry.bind("<Return>", lambda _: _ok())
        dlg.protocol("WM_DELETE_WINDOW", _ok)   # close = accept current value

        dlg.wait_window()
        self.root.deiconify()           # restore main window after dialog closes
        return result[0]

    def _show_ip_dialog(self):
        """
        Show a modal dark-themed dialog asking the user to enter the CSN SAT IP.
        Called on the main thread via root.after().  Signals _ip_ready when done.
        """
        dlg = tk.Toplevel(self.root)
        dlg.title("CSN SAT Not Found")
        dlg.configure(bg=C_BG)
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.lift()

        tk.Label(dlg,
                 text="No CSN SAT found on the network.\n"
                      "Enter the IP address manually:",
                 bg=C_BG, fg=C_TEXT,
                 font=("Segoe UI", 10),
                 justify="left").pack(padx=24, pady=(20, 6))

        entry = tk.Entry(dlg, bg=C_PANEL, fg=C_TEXT,
                         font=self._mono_font(12),
                         insertbackground=C_TEXT,
                         relief="flat", width=20, justify="center")
        entry.insert(0, SAT_HOST_DEFAULT)
        entry.pack(padx=24, pady=6)
        entry.focus_set()
        entry.select_range(0, "end")

        tk.Label(dlg, text="(Leave blank or press Cancel to use the default)",
                 bg=C_BG, fg=C_DIM,
                 font=("Segoe UI", 8)).pack(padx=24)

        def _ok():
            ip = entry.get().strip()
            self._ip_result = ip if ip else SAT_HOST_DEFAULT
            dlg.destroy()
            self._ip_ready.set()

        def _cancel():
            self._ip_result = SAT_HOST_DEFAULT
            dlg.destroy()
            self._ip_ready.set()

        tk.Button(dlg, text="Connect",
                  bg="#0f2e0f", fg=C_GREEN,
                  font=("Segoe UI", 10, "bold"),
                  relief="flat", padx=20, pady=6,
                  command=_ok).pack(pady=(10, 20))

        entry.bind("<Return>", lambda _: _ok())
        dlg.protocol("WM_DELETE_WINDOW", _cancel)

    # ══════════════════════════════════════════════════════════════════════════
    #  BUTTON / KEY HANDLERS
    # ══════════════════════════════════════════════════════════════════════════

    def _do_pause(self):
        with self._pause_lock:
            self._paused = True
        self._log("INFO", "[control] PAUSED — automatic updates suspended.  Press R to resume.")

    def _do_resume(self):
        with self._pause_lock:
            self._paused = False
        self._log("INFO", "[control] RESUMED — automatic updates re-enabled.")
        self._wake.set()

    def _create_shortcut(self):
        """Create a desktop shortcut using PowerShell — handles OneDrive redirection."""
        import os
        script  = os.path.abspath(__file__)
        workdir = os.path.dirname(script)
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.isfile(pythonw):
            pythonw = "pythonw.exe"

        ps = (
            '$desktop = [Environment]::GetFolderPath("Desktop");'
            '$lnk = Join-Path $desktop "CSN SAT Wind Tracker.lnk";'
            f'$s = (New-Object -COM WScript.Shell).CreateShortcut($lnk);'
            f'$s.TargetPath = "{pythonw}";'
            f'$s.Arguments = \'"{script}"\';'
            f'$s.WorkingDirectory = "{workdir}";'
            f'$s.Description = "CSN SAT Wind Tracker - VA3MW";'
            f'$s.Save();'
            f'Write-Output $lnk'
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                check=True, capture_output=True, text=True)
            lnk_path = result.stdout.strip()
            self._log("INFO", f"[shortcut] Desktop shortcut created → {lnk_path}")
            tk.messagebox.showinfo("Shortcut Created",
                f"Desktop shortcut created successfully.\n\n{lnk_path}")
        except subprocess.CalledProcessError as exc:
            err = exc.stderr.strip()
            self._log("ERROR", f"[shortcut] Failed: {err}")
            tk.messagebox.showerror("Shortcut Failed",
                f"Could not create shortcut:\n\n{err}")

    def _on_close(self):
        self._wake.set()
        self._ip_ready.set()   # unblock worker if waiting on dialog
        self.root.destroy()

    # ══════════════════════════════════════════════════════════════════════════
    #  NETWORK / WEATHER HELPERS
    # ══════════════════════════════════════════════════════════════════════════

    def _ping(self, host: str) -> bool:
        r = subprocess.run(["ping", "-n", "1", "-w", "1000", host],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0

    def _send(self, sock: socket.socket, inner: str):
        payload = f"<PST>{inner}</PST>"
        sock.sendto(payload.encode("ascii"), (self._sat_host, SAT_PORT))
        self._log("INFO", f"[rotator] TX → {payload}")

    def _fetch_metar(self) -> str:
        for tmpl in METAR_SOURCES:
            url = tmpl.format(icao=self._icao)
            try:
                r = requests.get(url, timeout=10)
                r.raise_for_status()
                for line in r.text.splitlines():
                    if line.strip().startswith(self._icao):
                        return line.strip()
            except Exception as exc:
                self._log("WARNING", f"[weather] {url} — {exc}")
        raise RuntimeError("All METAR sources failed.")

    def _parse_wind(self, raw: str):
        m = re.search(r'\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT\b', raw)
        if not m:
            raise ValueError(f"No wind group in: {raw}")
        d, s, g = m.group(1), int(m.group(2)), m.group(3)
        if d == "VRB":
            raise ValueError("Wind variable — no fixed bearing.")
        if d == "000" and s == 0:
            raise ValueError("Wind calm — no bearing to point to.")
        return int(d), s, (int(g) if g else None)

    # ══════════════════════════════════════════════════════════════════════════
    #  BACKGROUND THREADS
    # ══════════════════════════════════════════════════════════════════════════

    def _runner_9932(self):
        """
        Single daemon that owns the ONE socket on DISCOVERY_PORT for the entire
        session.  Using one socket avoids the WinError 10013 / port-conflict
        problem that occurs when separate discovery and listener threads both
        try to bind the same port.

        Phase 1 — Discovery (first DISCOVERY_SECS seconds):
          Waits for any 'SAT,' broadcast.  On the first packet received the
          source IP is stored in self._discovered_ip and _discovery_done is set
          so the worker thread can continue.  If nothing arrives before the
          deadline _discovery_done is set with _discovered_ip still None.

        Phase 2 — Ongoing event monitoring (after discovery):
          Processes CSNTracker broadcast events to manage antenna-in-use state:
            SAT,START TRACK,name,catno  → marks antenna IN USE
            SAT,AOS,az                  → marks antenna IN USE
            SAT,LOS,az                  → clears IN USE immediately

        If the bind fails (e.g. Windows Firewall / port already taken) the
        error is logged, _discovery_done is signalled so the worker falls
        through to the manual-IP dialog, and the thread exits cleanly.
        """
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("", DISCOVERY_PORT))
            s.settimeout(1.0)   # short poll so we can check the deadline
        except OSError as exc:
            self._log("ERROR",
                f"[discover] Cannot bind :{DISCOVERY_PORT}: {exc}  "
                f"— check Windows Firewall or whether another app owns that port.")
            self._discovery_done.set()   # unblock the worker
            return

        self._log("INFO",
            f"[discover] Listening on :{DISCOVERY_PORT} for CSNTracker "
            f"broadcast (timeout {DISCOVERY_SECS}s)…")

        deadline = time.time() + DISCOVERY_SECS
        discovery_complete = False

        with s:
            while True:
                try:
                    data, addr = s.recvfrom(4096)
                    msg = data.decode("ascii", errors="replace").strip()
                    if not msg.startswith("SAT,"):
                        continue

                    # ── Discovery phase: grab the IP from the first packet ────
                    if not discovery_complete:
                        discovery_complete = True
                        self._discovered_ip = addr[0]
                        self._log("INFO",
                            f"[discover] CSNTracker found at {addr[0]}  →  {msg}")
                        self._discovery_done.set()

                    # ── Parse and act on the SAT event ───────────────────────
                    self._handle_sat_event(msg, addr[0])

                except socket.timeout:
                    # Check whether the discovery window has closed
                    if not discovery_complete and time.time() >= deadline:
                        self._log("WARNING",
                            f"[discover] No CSNTracker broadcast in {DISCOVERY_SECS}s "
                            f"— falling back to manual IP entry.")
                        discovery_complete = True
                        self._discovery_done.set()
                    # After discovery is done keep looping for ongoing events

                except Exception as exc:
                    self._log("ERROR", f"[discover] {exc}")

    def _handle_sat_event(self, msg: str, src_ip: str):
        """Parse a CSNTracker SAT, broadcast and update antenna-in-use state."""
        parts = [p.strip() for p in msg.split(",")]
        event = parts[1].upper() if len(parts) > 1 else ""

        if event == "START TRACK":
            name = parts[2] if len(parts) > 2 else "unknown"
            cat  = parts[3] if len(parts) > 3 else ""
            with self._cmd_lock:
                self._last_cmd_time = time.time()
            self._log("INFO", f"[sat] START TRACK  {name} ({cat}) — antenna IN USE.")
            self._ui_sat_event(f"TRACKING  {name}", C_CYAN)

        elif event == "AOS":
            az = parts[2] if len(parts) > 2 else "?"
            with self._cmd_lock:
                self._last_cmd_time = time.time()
            self._log("INFO", f"[sat] AOS at {az}° — antenna IN USE.")
            self._ui_sat_event(f"AOS  az={az}°", C_GREEN)

        elif event == "LOS":
            az = parts[2] if len(parts) > 2 else "?"
            with self._cmd_lock:
                self._last_cmd_time = 0.0   # clear immediately — pass is over
            self._log("INFO",
                f"[sat] LOS at {az}° — pass complete, antenna now FREE.")
            self._ui_sat_event(f"LOS  az={az}°", C_DIM)
            self._wake.set()   # let the worker run a wind check right away

    def _listener_12001(self):
        """
        Daemon — watches LISTEN_PORT for <AZIMUTH>/<ELEVATION> PSTRotator
        commands from other apps and records the time they were last seen.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", LISTEN_PORT))
            s.settimeout(1.0)
            self._log("INFO",
                f"[listener] Monitoring :{LISTEN_PORT} for external positioning commands.")
            while True:
                try:
                    data, addr = s.recvfrom(4096)
                    msg = data.decode("ascii", errors="replace").strip()
                    if "<AZIMUTH>" in msg or "<ELEVATION>" in msg:
                        with self._cmd_lock:
                            self._last_cmd_time = time.time()
                        self._log("INFO",
                            f"[listener] Cmd from {addr[0]}  →  {msg}")
                        self._log("INFO",
                            "[listener] Antenna marked IN USE — auto-control paused.")
                except socket.timeout:
                    continue
                except Exception as exc:
                    self._log("ERROR", f"[listener] {exc}")

    def _keepalive(self):
        """
        Daemon — resends the last azimuth/elevation every 60 s so the antenna
        holds position even if it was bumped or reset between wind checks.
        Respects the same guards as the worker: won't resend while paused or
        while the antenna is marked in use by a satellite tracker / other app.
        Does nothing until the worker has successfully sent at least one command.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
            while True:
                time.sleep(60)

                with self._az_lock:
                    az = self._last_az
                if az is None:
                    continue                     # no position established yet

                with self._pause_lock:
                    if self._paused:
                        continue

                with self._cmd_lock:
                    in_use = (self._last_cmd_time > 0 and
                              time.time() - self._last_cmd_time < IDLE_TIMEOUT)
                if in_use:
                    continue

                try:
                    for inner in ("<ELEVATION>0.0</ELEVATION>",
                                  f"<AZIMUTH>{az}.0</AZIMUTH>"):
                        payload = f"<PST>{inner}</PST>"
                        tx.sendto(payload.encode("ascii"), (self._sat_host, SAT_PORT))
                    self._log("INFO", f"[keepalive] Azimuth {az}° re-sent.")
                except Exception as exc:
                    self._log("ERROR", f"[keepalive] Send failed: {exc}")

    def _worker(self):
        """
        Daemon — handles startup, discovery, then the main wind-tracking loop.
        """
        self._log("INFO", "[startup] CSN SAT Wind Tracker  —  Michael Walker VA3MW")
        self._log("INFO", f"[startup] Interval : {INTERVAL_SEC//60} min  |  "
                          f"Idle guard : {IDLE_TIMEOUT//60} min  |  "
                          f"Gust min : {MIN_GUST_KT} kt")

        # ── Phase 1: wait for runner_9932 to complete the discovery phase ────
        self._discovery_done.wait()          # blocks until found or timed out
        discovered = self._discovered_ip    # None if nothing was heard

        if discovered:
            self._sat_host = discovered
            self._ui_sat_host_discovered(discovered)
        else:
            # Discovery timed out — ask the user on the main thread
            self._log("WARNING",
                "[discover] Showing manual IP entry dialog…")
            self._ip_ready.clear()
            self.root.after(0, self._show_ip_dialog)
            self._ip_ready.wait()          # block until user clicks Connect
            self._sat_host = self._ip_result
            self._log("INFO",
                f"[discover] Using manually entered IP: {self._sat_host}")

        # ── Phase 2: ping the SAT to confirm reachability ─────────────────────
        self._log("INFO", f"[startup] Pinging {self._sat_host}…")
        if self._ping(self._sat_host):
            self._log("INFO",    f"[startup] CSN SAT {self._sat_host} — reachable  ✓")
            self._ui_sat(True)
        else:
            self._log("WARNING", f"[startup] CSN SAT {self._sat_host} — no ping response.")
            self._log("WARNING",  "[startup] Continuing — UDP commands may still work.")
            self._ui_sat(False)

        self._log("INFO", f"[startup] Sending commands to {self._sat_host}:{SAT_PORT}")

        # ── Phase 3: main wind-tracking loop ──────────────────────────────────
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
            while True:
                self._log("INFO", "─" * 56)
                self._log("INFO", "[main] Scheduled check running…")

                with self._pause_lock:
                    paused = self._paused

                # Guard 1: manually paused?
                if paused:
                    self._log("INFO", "[main] SKIPPED — paused by operator.")
                    self._ui_skipped("paused")

                # Guard 2: antenna in use by satellite tracker or other app?
                elif (self._last_cmd_time > 0 and
                      time.time() - self._last_cmd_time < IDLE_TIMEOUT):
                    age = int(time.time() - self._last_cmd_time)
                    rem = IDLE_TIMEOUT - age
                    self._log("INFO",
                        f"[main] SKIPPED — antenna in use  "
                        f"(last event {age}s ago, {rem}s until idle).")
                    self._ui_skipped("antenna in use")

                else:
                    # Fetch METAR
                    wdir = wspd = wgst = None
                    try:
                        raw = self._fetch_metar()
                        self._log("INFO", f"[weather] METAR : {raw}")
                        wdir, wspd, wgst = self._parse_wind(raw)
                        gs = f", gusting {wgst} kt" if wgst else ", no gusts reported"
                        self._log("INFO", f"[weather] Wind  : {wdir}° at {wspd} kt{gs}")
                        self._ui_weather(raw, wdir, wspd, wgst)
                    except Exception as exc:
                        self._log("ERROR", f"[main] SKIPPED — weather error: {exc}")

                    if wdir is not None:
                        # Guard 3: gusts present and above threshold?
                        if wgst is None:
                            self._log("INFO",
                                "[main] SKIPPED — no gusts in METAR.  Antenna not moved.")
                            self._ui_skipped("no gusts")
                        elif wgst <= MIN_GUST_KT:
                            self._log("INFO",
                                f"[main] SKIPPED — gusts {wgst} kt ≤ threshold "
                                f"{MIN_GUST_KT} kt.  Antenna not moved.")
                            self._ui_skipped(f"gusts {wgst} kt")
                        else:
                            # All guards passed — move the antenna
                            self._log("INFO",
                                f"[main] MOVING antenna  →  Azimuth {wdir}°  "
                                f"Elevation 0°  "
                                f"(wind {wspd} kt, gusting {wgst} kt from {wdir}°)")
                            try:
                                self._send(tx, "<ELEVATION>0.0</ELEVATION>")
                                self._send(tx, f"<AZIMUTH>{wdir}.0</AZIMUTH>")
                                self._log("INFO", "[main] Commands sent successfully.")
                                with self._az_lock:
                                    self._last_az = wdir
                                self._ui_moved(wdir)
                            except Exception as exc:
                                self._log("ERROR", f"[main] Send failed: {exc}")

                self._next_check_at = time.time() + INTERVAL_SEC
                self._log("INFO", f"[main] Next check in {INTERVAL_SEC//60} minutes.")
                self._wake.clear()
                self._wake.wait(timeout=INTERVAL_SEC)


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
