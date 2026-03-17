# CSN SAT Wind Tracker  v2.1

**Automatic wind-tracking antenna controller for CSN SAT — ICAO METAR · PSTRotator UDP · HTTP status polling · dark-theme GUI**

*Michael Walker — VA3MW*

> ⚠️ **Placeholders — read before running**
> Every IP address and ICAO station code in this document and in the script defaults is a placeholder.
> You **must** replace them with the values correct for your own installation.

---

## What It Does

CSN SAT Wind Tracker keeps a CSN SAT antenna pointed into the prevailing wind during high-gust conditions.
It fetches live METAR reports from your nearest ICAO weather station, evaluates the wind, and sends
PSTRotator UDP positioning commands directly to the CSN SAT controller.

The antenna is held on the last known bearing with a 1-minute keepalive, and the tracker automatically
stands down whenever the SAT is actively tracking a satellite — releasing control the moment the pass ends.

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| Windows PC | Must be on the same LAN as the CSN SAT controller |
| Python 3.8 + | tkinter is included in the standard Windows installer |
| `requests` library | `pip install requests` |

---

## Installation

```
pip install requests
python csnsat-windtrack.py
```

Use the **🖥 Shortcut** button in the app to create a Windows desktop shortcut that launches it with `pythonw.exe` (no console window).

---

## Startup Sequence

### Step 1 — ICAO Weather Station

The first thing shown is a dialog asking for your **ICAO weather station code**.

- Enter the 4-letter ICAO code for the airport nearest your antenna (e.g. `EGLL`, `KJFK`, `YSSY`).
- **This is site-specific — the pre-filled default (`CYYZ`) is a placeholder for Toronto Pearson and will not be correct for your location.**
- Click the **ourairports.com** link in the dialog to search by city or airport name if you are unsure.
- Press **Enter** or click **OK — Start Tracking** to continue.

### Step 2 — CSN SAT Auto-Discovery

The app listens on **UDP port 9932** for a CSNTracker broadcast.

- The source IP of the first `SAT,` broadcast received becomes the CSN SAT host address — no manual entry needed in most cases.
- If nothing is heard within **30 seconds** a dialog appears asking for the IP manually.
- **The pre-filled fallback IP (`192.168.113.121`) is a placeholder — replace it with the actual address of your CSN SAT.**
- You can also set `SAT_HOST_DEFAULT` in the script to avoid the manual dialog.

### Step 3 — Connectivity Check

After the IP is confirmed the app pings the CSN SAT.
A green **ONLINE** indicator means the device is reachable.
If the ping fails a warning is logged but the app continues — UDP commands often work even without a ping response.

---

## Wind Tracking

### 5-Minute Weather Checks

Every **5 minutes** the worker fetches a fresh METAR from your ICAO station:

1. Primary: `tgftp.nws.noaa.gov` — NOAA plain-text feed, no API key required
2. Fallback: `metar.vatsim.net` — VATSIM public feed, no API key required

Three guards are evaluated before any command is sent:

| Guard | Condition | Result |
|-------|-----------|--------|
| 1 | Operator pressed **Pause** | Skip — no command sent |
| 2 | SAT is tracking a satellite | Skip — antenna in use |
| 3 | No gusts, or gusts ≤ threshold | Skip — not significant |

If all three pass, two UDP datagrams are sent to the SAT on port 12000:

```
<PST><ELEVATION>0.0</ELEVATION></PST>
<PST><AZIMUTH>xxx.x</AZIMUTH></PST>
```

### 1-Minute Keepalive

A separate thread resends the last azimuth and elevation **every 60 seconds**.
This ensures the antenna returns to the correct bearing if it was bumped, power-cycled,
or moved by another application between 5-minute checks.
The keepalive honours the same pause and in-use guards as the main worker.

---

## Satellite Pass Awareness

Pass detection uses two complementary mechanisms.  Between them, the antenna is always
released promptly when a pass ends and control returns to wind tracking.

### UDP Events (port 9932 — primary, real-time)

The same socket used for auto-discovery continues listening for CSNTracker broadcasts:

| Message | Meaning | Action |
|---------|---------|--------|
| `SAT,START TRACK,<name>,<catno>` | Tracker has started a pass | Mark antenna **IN USE** |
| `SAT,AOS,<az>` | Satellite acquired | Mark antenna **IN USE** |
| `SAT,LOS,<az>` | Pass complete | Clear IN USE immediately — wind tracking resumes |

### HTTP Polling (GET `/track` — backup)

The app polls `http://<sat-ip>/track` and reads the `mode` field:

| `mode` | Meaning | Poll interval |
|--------|---------|---------------|
| `0` — idle | Antenna is free | Every **30 seconds** |
| `1` — tracking | Antenna in use — suppress all wind commands | Every **5 minutes** (SAT is busy; UDP LOS is the primary notification) |

The live azimuth and elevation from the API are shown on the **ANTENNA** card in real time —
cyan during a pass, normal text when idle.

### Fallback — IDLE_TIMEOUT

If neither a UDP LOS event nor an HTTP mode=0 transition is received within **5 minutes**
of the last tracking event, the keepalive thread logs a timeout, releases the antenna,
and wind tracking resumes automatically.

---

## GUI Overview

```
┌─────────────────────────────────────────────────────────┐
│  CSN SAT Wind Tracker  v2.1              ⊟  VA3MW       │  ← ⊟ = compact toggle
├──────────────────┬──────────────────┬───────────────────┤
│  STATUS          │  WEATHER (CYYZ)  │  ANTENNA          │
│  CSN SAT ●ONLINE │  METAR  …        │  Azimuth  280.0°  │
│  Mode  RUNNING   │  Direction  280° │  Elevation  4.3°  │
│  Interval  5 min │  Speed  24 kt   │  Last moved …     │
│  Idle guard 5min │  Gusts  33 kt   │  Last action …    │
│  Gust min  15 kt │  Updated …      │                   │
│  Next check …    │                 │                   │
│  SAT event …     │                 │                   │
├──────────────────┴──────────────────┴───────────────────┤
│  ⏸ PAUSE    ▶ RESUME    🖥 Shortcut                      │
├─────────────────────────────────────────────────────────┤
│  EVENT LOG                                              │
│  09:38:57  [discover] Listening on :9932 …              │
│  …                                                      │
└─────────────────────────────────────────────────────────┘
```

### Compact Mode

Click **⊟** in the title bar to collapse the window to just the PAUSE / RESUME / Shortcut buttons.
Click **⊞** to restore the full view.  The window remembers its size and position.

---

## Controls

| Control | Keyboard | Action |
|---------|----------|--------|
| **⏸ PAUSE** | `P` | Suspend all automatic antenna updates |
| **▶ RESUME** | `R` | Re-enable automatic updates and trigger an immediate check |
| **🖥 Shortcut** | — | Create a Windows desktop shortcut (`pythonw.exe`, no console) |
| **⊟ / ⊞** | — | Collapse to / restore from compact button-only view |

---

## Configuration

Open `csnsat-windtrack.py` in a text editor and adjust the constants near the top of the file.
**All values below are defaults or placeholders — review every one for your site.**

```python
# ══════════════════════════════════════════════════════════════════════
#  CONFIGURATION  ←  edit these values to match your setup
# ══════════════════════════════════════════════════════════════════════

SAT_HOST_DEFAULT = "192.168.113.121"  # ← PLACEHOLDER — your CSN SAT IP
SAT_PORT         = 12000   # CSN SAT command port (PSTRotator UDP)
DISCOVERY_PORT   = 9932    # CSNTracker broadcast port
DISCOVERY_SECS   = 30      # seconds to wait for auto-discovery before prompting

INTERVAL_SEC = 300         # weather check interval in seconds  (300 = 5 min)
IDLE_TIMEOUT = 300         # seconds after last tracking event before antenna is free
MIN_GUST_KT  = 15          # minimum gust speed (kt) to trigger a move

ICAO_DEFAULT = "CYYZ"      # ← PLACEHOLDER — ICAO code pre-filled in startup dialog
                            #   replace with the code for your nearest airport
```

---

## PSTRotator UDP Protocol

Commands are sent to the CSN SAT on **UDP port 12000**:

```
<PST><ELEVATION>0.0</ELEVATION></PST>
<PST><AZIMUTH>270.0</AZIMUTH></PST>
```

---

## CSN SAT HTTP API

The app reads live status from `http://<sat-ip>/track`.  Fields used:

| Field | Type | Description |
|-------|------|-------------|
| `mode` | int | `1` = tracking a satellite · `0` = idle |
| `az` | float | Current rotator azimuth (degrees) |
| `el` | float | Current rotator elevation (degrees) |

The API is polled every **30 seconds** while idle and every **5 minutes** during a pass.

---

## Background Threads

| Thread | Role |
|--------|------|
| `runner_9932` | Owns the single UDP socket on port 9932 — handles auto-discovery then monitors CSNTracker events for the lifetime of the session |
| `worker` | Waits for discovery, then runs the 5-minute wind-check loop |
| `keepalive` | Resends the last az/el every 60 s; detects IDLE_TIMEOUT expiry |
| `poll-sat` | Polls `/track` every 30 s (idle) or 5 min (tracking) |

---

## Event Log Colour Key

| Colour | Tag | Meaning |
|--------|-----|---------|
| Green | `move` | Antenna repositioned |
| Cyan | `startup` | Startup / connectivity messages |
| Purple | `discover` | Auto-discovery messages |
| Orange | `sat` | CSNTracker satellite events |
| Blue-grey | `tx` | UDP commands sent / keepalive |
| Dim | `skip` | Skipped check (paused / in use / no gusts) |
| Orange | `warn` | Warnings |
| Red | `error` | Errors |

---

## License

MIT — free to use, modify, and distribute.
