#!/usr/bin/env python3
"""
================================================================================
  CYYZ Wind Direction → Antenna Rotator Controller
  Author  : Michael Walker  VA3MW
  Date    : 2026-03-16
  Version : 1.4
================================================================================

PURPOSE
-------
  Keeps a CSN SAT rotator controller pointed into the prevailing wind
  so that a directional antenna is always aligned with the wind.

  The script is "polite" — it will NOT move the antenna if another application
  (e.g. a satellite tracker) has sent positioning commands recently.  It
  monitors UDP port 12001 for those commands and backs off for 5 minutes after
  the last one is seen.

  Wind-speed threshold: the antenna is only repositioned when wind speed is
  above 13 knots.  Below that threshold the script logs the reason and skips.

HOW IT WORKS
------------
  1. A background listener thread watches UDP port 12001 for any incoming
     <AZIMUTH> or <ELEVATION> commands that originate from other programs.
     When one is detected the timestamp is recorded.

  2. A keyboard thread watches for P and R keypresses:
       P  →  Pause  — suspends all automatic antenna updates
       R  →  Resume — re-enables automatic antenna updates

  3. The main loop wakes up every INTERVAL_SEC seconds (default 5 minutes).
     Before sending anything it checks three conditions:
       a) Is the script paused by the user (P key)?
          If so → skip until R is pressed.
       b) Has it been at least IDLE_TIMEOUT seconds since the last external
          positioning command?  If not → skip (antenna in use).
       c) Is the current CYYZ wind speed above MIN_WIND_KT?
          If not → skip (wind too light to matter).
     If all conditions pass it fetches the METAR, extracts wind direction,
     and sends ELEVATION=0 + AZIMUTH=<wind direction> to the SAT controller.


HARDWARE / NETWORK SETUP
------------------------
  - A CSN SAT rotator connected to a SAT (Satellite Antenna Tracker) or any
    PSTRotator-compatible controller on your LAN.
  - The SAT must be reachable at SAT_HOST on your local network.
  - The computer running this script must be able to reach the internet to
    pull METAR data.

THINGS YOU MAY NEED TO EDIT
----------------------------
  SAT_HOST     IP address of your SAT / PSTRotator controller
  SAT_PORT     UDP port the SAT listens on for commands   (default 12000)
  LISTEN_PORT  UDP port this script listens on for commands from other apps
               (default 12001 — the port the SAT sends responses back on)
  INTERVAL_SEC How often (seconds) the main loop runs    (default 300 = 5 min)
  IDLE_TIMEOUT Seconds of silence before antenna is considered free
               (default 300 = 5 min)
  MIN_WIND_KT  Minimum wind speed in knots to trigger a move (default 13)
  ICAO         Airport ICAO code for weather source       (default CYYZ)

KEYBOARD COMMANDS
-----------------
  P  —  Pause  : suspend automatic antenna updates (antenna stays put)
  R  —  Resume : re-enable automatic antenna updates

DEPENDENCIES
------------
  Python 3.8+
  requests  →  pip install requests

PSTROTATOR UDP PROTOCOL
-----------------------
  All commands must be wrapped in <PST>...</PST> tags.
  Commands are sent TO the SAT on UDP port 12000.
  Responses come back FROM the SAT to the originating IP on UDP port 12001.

  Supported commands used here:
    <PST><AZIMUTH>xxx.x</AZIMUTH></PST>    — rotate to xxx.x degrees
    <PST><ELEVATION>xxx.x</ELEVATION></PST> — tilt to xxx.x degrees

METAR SOURCES (tried in order, first success wins)
---------------------------------------------------
  1. https://tgftp.nws.noaa.gov  — NOAA plain-text METAR server (preferred)
  2. https://metar.vatsim.net    — VATSIM public METAR feed (fallback)
================================================================================
"""

import logging
import msvcrt   # Windows-only: non-blocking keyboard input
import re
import socket
import sys
import threading
import time
from datetime import datetime

import requests

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION  ←  Edit these values to match your setup
# ══════════════════════════════════════════════════════════════════════════════

SAT_HOST     = "xxx.xxx.xxx.xxx"  # IP address of your SAT Controller box
SAT_PORT     = 12000              # UDP port the SAT listens on for commands
LISTEN_PORT  = 12001              # UDP port to monitor for other apps' commands

INTERVAL_SEC = 300   # Main loop interval in seconds (300 = 5 minutes)
IDLE_TIMEOUT = 300   # Seconds of quiet before we consider the antenna free
MIN_WIND_KT  = 13    # Minimum wind speed (knots) required to reposition

ICAO = "CYYZ"        # ICAO airport code for weather — change for other sites

# METAR data sources — tried in order; first successful response is used
METAR_SOURCES = [
    "https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT",
    "https://metar.vatsim.net/{icao}",
]

# ══════════════════════════════════════════════════════════════════════════════
#  END OF CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════


# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── Shared state (all threads read/write these) ───────────────────────────────

# Timestamp of the last external positioning command seen on LISTEN_PORT.
# Value 0.0 means no command has been seen yet this session.
_last_cmd_time: float = 0.0
_last_cmd_lock = threading.Lock()

# Pause flag — set True by P keypress, False by R keypress.
# When True the main loop skips all antenna updates.
_paused: bool = False
_pause_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
#  KEYBOARD THREAD  —  P to pause, R to resume
# ══════════════════════════════════════════════════════════════════════════════

def keyboard_thread() -> None:
    """
    Background daemon thread — polls for keypresses using msvcrt (Windows).
    P  →  sets _paused = True   (suspends antenna updates)
    R  →  sets _paused = False  (resumes antenna updates)
    Any other key is silently ignored.
    Polls every 100 ms so it stays responsive without burning CPU.
    """
    global _paused

    while True:
        if msvcrt.kbhit():
            key = msvcrt.getch().decode("utf-8", errors="ignore").upper()
            with _pause_lock:
                if key == "P":
                    _paused = True
                    log.info("[keyboard] PAUSED — automatic updates suspended. Press R to resume.")
                elif key == "R":
                    _paused = False
                    log.info("[keyboard] RESUMED — automatic updates re-enabled.")
        time.sleep(0.1)


# ══════════════════════════════════════════════════════════════════════════════
#  LISTENER THREAD  —  watches for external antenna commands on UDP 12001
# ══════════════════════════════════════════════════════════════════════════════

def listener_thread() -> None:
    """
    Background daemon thread — monitors UDP LISTEN_PORT for positioning
    commands from other applications (e.g. satellite trackers, DX clusters).

    When an <AZIMUTH> or <ELEVATION> command is received the shared
    _last_cmd_time is updated so the main loop knows to back off.

    The 1-second socket timeout lets the thread respond to program exit
    without hanging.
    """
    global _last_cmd_time

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", LISTEN_PORT))
        sock.settimeout(1.0)
        log.info(f"[listener] Monitoring :{LISTEN_PORT} for external positioning commands.")

        while True:
            try:
                data, addr = sock.recvfrom(4096)
                msg = data.decode("ascii", errors="replace").strip()

                # Only care about commands that actually move the antenna
                if "<AZIMUTH>" in msg or "<ELEVATION>" in msg:
                    with _last_cmd_lock:
                        _last_cmd_time = time.time()
                    log.info(
                        f"[listener] External command from {addr[0]}:{addr[1]} → {msg}"
                    )
                    log.info("[listener] Antenna marked IN USE — auto-updates paused.")

            except socket.timeout:
                continue   # expected — just loop back and listen again
            except Exception as exc:
                log.error(f"[listener] Unexpected error: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
#  WEATHER  —  METAR fetch and wind parsing
# ══════════════════════════════════════════════════════════════════════════════

def fetch_raw_metar(icao: str) -> str:
    """
    Try each URL in METAR_SOURCES and return the raw METAR observation string
    for *icao*.  Raises RuntimeError if all sources fail.
    """
    for url_template in METAR_SOURCES:
        url = url_template.format(icao=icao)
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            # The response may contain a date/header line before the METAR —
            # find the line that starts with the ICAO identifier.
            for line in resp.text.splitlines():
                if line.strip().startswith(icao):
                    return line.strip()
        except Exception as exc:
            log.warning(f"[weather] Source {url} failed: {exc}")

    raise RuntimeError(f"All METAR sources failed for {icao}")


def parse_wind(raw: str) -> tuple[int, int]:
    """
    Extract wind direction and speed from a raw METAR string.

    METAR wind group formats:
      DDDSSKt       e.g. 27015KT  → 270° at 15 kt
      DDDSSGSSkt    e.g. 23024G34KT → 230° at 24 kt, gusting 34
      VRBSSkt       variable direction
      00000KT       calm

    Returns (direction_degrees, speed_knots).
    Raises ValueError for calm or variable winds.
    """
    # Match the wind group — direction (3 digits or VRB), speed, optional gust
    match = re.search(r'\b(\d{3}|VRB)(\d{2,3})(?:G\d{2,3})?KT\b', raw)
    if not match:
        raise ValueError(f"No wind group found in METAR: {raw}")

    direction_str = match.group(1)
    speed         = int(match.group(2))

    if direction_str == "VRB":
        raise ValueError("Wind is variable (VRB) — no fixed direction to point to.")
    if direction_str == "000" and speed == 0:
        raise ValueError("Wind is calm (00000KT) — no direction to point to.")

    return int(direction_str), speed


def get_wind(icao: str) -> tuple[int, int]:
    """
    Fetch METAR for *icao*, log the raw observation, and return
    (wind_direction_degrees, wind_speed_knots).
    """
    raw = fetch_raw_metar(icao)
    log.info(f"[weather] METAR : {raw}")

    wdir, wspd = parse_wind(raw)
    log.info(f"[weather] Wind  : {wdir}° at {wspd} kt")
    return wdir, wspd


# ══════════════════════════════════════════════════════════════════════════════
#  ROTATOR COMMANDS  —  PSTRotator UDP protocol
# ══════════════════════════════════════════════════════════════════════════════

def send_command(sock: socket.socket, inner: str) -> None:
    """
    Wrap *inner* in <PST>...</PST> tags and transmit as a UDP datagram
    to SAT_HOST:SAT_PORT.  No response is expected.
    """
    payload = f"<PST>{inner}</PST>"
    sock.sendto(payload.encode("ascii"), (SAT_HOST, SAT_PORT))
    log.info(f"[rotator] TX → {payload}")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    log.info("=" * 70)
    log.info("  CYYZ Wind Tracker  —  Michael Walker VA3MW")
    log.info(f"  SAT target : {SAT_HOST}:{SAT_PORT}")
    log.info(f"  Listening  : :{LISTEN_PORT}  (external command monitor)")
    log.info(f"  Interval   : every {INTERVAL_SEC // 60} min")
    log.info(f"  Idle guard : {IDLE_TIMEOUT // 60} min after last external command")
    log.info(f"  Wind min   : {MIN_WIND_KT} kt  (below this, antenna is not moved)")
    log.info("  Keys       : P = Pause,  R = Resume,  Ctrl+C = Quit")
    log.info("=" * 70)

    # Start background daemon threads (all die automatically when main exits)
    threading.Thread(target=listener_thread, daemon=True, name="listener").start()
    threading.Thread(target=keyboard_thread, daemon=True, name="keyboard").start()

    # UDP socket used exclusively for sending commands to the SAT
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx_sock:

        while True:
            # Wait for the next check interval before doing anything
            time.sleep(INTERVAL_SEC)

            log.info("-" * 70)
            log.info("[main] Running scheduled check...")

            # ── Guard 1: has the user manually paused the script? ─────────────
            with _pause_lock:
                paused = _paused

            if paused:
                log.info("[main] SKIPPED — script is paused. Press R to resume.")
                continue

            # ── Guard 2: is another application using the antenna? ────────────
            with _last_cmd_lock:
                idle_secs = time.time() - _last_cmd_time

            if _last_cmd_time > 0 and idle_secs < IDLE_TIMEOUT:
                remaining = int(IDLE_TIMEOUT - idle_secs)
                log.info(
                    f"[main] SKIPPED — antenna in use. "
                    f"Last external command {int(idle_secs)}s ago "
                    f"({remaining}s until idle threshold)."
                )
                continue

            # ── Fetch current METAR ───────────────────────────────────────────
            try:
                wdir, wspd = get_wind(ICAO)
            except Exception as exc:
                log.error(f"[main] SKIPPED — could not retrieve weather: {exc}")
                continue

            # ── Guard 3: is the wind strong enough to bother moving? ──────────
            if wspd <= MIN_WIND_KT:
                log.info(
                    f"[main] SKIPPED — wind speed {wspd} kt is at or below "
                    f"threshold of {MIN_WIND_KT} kt. Antenna not moved."
                )
                continue

            # ── All conditions met — send rotator commands ────────────────────
            log.info(
                f"[main] MOVING antenna → Azimuth={wdir}°  Elevation=0°  "
                f"(wind {wspd} kt from {wdir}°)"
            )
            try:
                send_command(tx_sock, "<ELEVATION>0.0</ELEVATION>")
                send_command(tx_sock, f"<AZIMUTH>{wdir}.0</AZIMUTH>")
                log.info("[main] Commands sent successfully.")
            except Exception as exc:
                log.error(f"[main] Failed to send commands: {exc}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped by user.")
        sys.exit(0)
