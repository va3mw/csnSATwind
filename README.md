# CSN SAT Wind Tracker

**Automatic wind-tracking antenna controller for CSN SAT — ICAO METAR, PSTRotator UDP, CSNTracker integration, dark-theme GUI.**

*Michael Walker — VA3MW*

---

## What It Does

CSN SAT Wind Tracker keeps a CSN SAT antenna controller pointed into the prevailing wind during high-gust conditions. It fetches live METAR weather data from a configurable ICAO airport station, evaluates the wind, and sends positioning commands in PSTRotator UDP format. The antenna is held on the last known wind bearing with a 1-minute keepalive, and the tracker automatically backs off whenever a satellite pass is in progress.

---

## Requirements

- Windows PC on the same LAN as the CSN SAT controller
- Python 3.8 or later (standard install — tkinter is included)
- The `requests` library:
  ```
  pip install requests
  ```

---

## Startup Sequence

### Step 1 — ICAO Weather Station

The first dialog to appear asks for your **ICAO weather station code**.

![ICAO dialog appears on top at startup]

- Enter the 4-letter ICAO code for the airport nearest to you (e.g. `CYYZ` for Toronto Pearson, `KJFK` for New York Kennedy, `EGLL` for London Heathrow).
- The code must be **exactly 4 characters** — the standard ICAO format used by aviation weather services worldwide.
- If you are not sure of your local code, click the **ourairports.com** link in the dialog — it opens a searchable map where you can find any airport by city or name, then copy the 4-letter code shown.
- Press **Enter** or click **OK — Start Tracking** to continue. Leaving the field unchanged accepts the default (`CYYZ`).

> **Why ICAO?** METAR reports — the authoritative source of actual measured wind speed, direction, and gusts — are filed by ICAO station code. Using the airport closest to your antenna gives you the most representative wind data.

### Step 2 — CSN SAT Auto-Discovery

Once the ICAO code is confirmed the main window opens and the app immediately begins **listening on UDP port 9932** for a broadcast from CSNTracker.

- CSNTracker sends periodic UDP broadcasts on port 9932 as it tracks satellites.
- The app captures the **source IP address** of the first broadcast it receives and uses that automatically as the CSN SAT host — no manual IP entry needed.
- If no broadcast is heard within **30 seconds** a dialog prompts you to enter the CSN SAT IP address manually. The default (`192.168.113.121`) is pre-filled.  This will NOT work until you provide the correct IP address which is on the display of the CSN SAT.

### Step 3 — Ping Check

After the IP is established the app pings the CSN SAT to confirm it is reachable on the network. A green **ONLINE** indicator confirms connectivity. If the ping fails a warning is logged but the app continues — UDP commands may still reach the device even without a ping response.

---

## How Wind Tracking Works

### 5-Minute Weather Checks

Every **5 minutes** the worker thread fetches a fresh METAR from the configured ICAO station:

1. Primary source: `tgftp.nws.noaa.gov` (NOAA plain-text server)
2. Fallback source: `metar.vatsim.net` (VATSIM public feed)

The wind group is parsed from the raw METAR string. Three guards are evaluated before any command is sent:

| Guard | Condition | Result |
|-------|-----------|--------|
| 1 | Operator pressed **Pause** | Skip — no command sent |
| 2 | Antenna in use (satellite pass or external command within 5 min) | Skip — back off |
| 3 | No gusts reported, or gusts ≤ 15 kt | Skip — not significant enough to move |

If all three guards pass the app sends:
```
<PST><ELEVATION>0.0</ELEVATION></PST>
<PST><AZIMUTH>xxx.x</AZIMUTH></PST>
```
to the CSN SAT on UDP port 12000, pointing the antenna into the wind at zero elevation.

### 1-Minute Keepalive

Between weather checks a separate **keepalive thread** resends the last known azimuth and elevation **every 60 seconds**. This ensures the antenna returns to the correct bearing if it was bumped, power-cycled, or reset by another application between the 5-minute cycles. The keepalive respects the same pause and antenna-in-use guards as the main worker — it will not step on a satellite pass.

---

## Satellite Pass Awareness

The app listens continuously on port 9932 for CSNTracker broadcast events:

| Event | Meaning | Action |
|-------|---------|--------|
| `SAT,START TRACK,name,catno` | Tracker beginning a pass | Mark antenna **IN USE** |
| `SAT,AOS,az` | Satellite acquired | Mark antenna **IN USE** |
| `SAT,LOS,az` | Pass complete | Clear IN USE — wind tracking resumes immediately |

While the antenna is IN USE neither the 5-minute check nor the 1-minute keepalive will send any commands. When LOS is received the idle timer is cleared instantly so wind tracking can resume without waiting out the full 5-minute interval.

The app also monitors **UDP port 12001** for PSTRotator commands from any other application. If a positioning command is received from an external source the antenna is marked IN USE for 5 minutes, preventing the wind tracker from overriding it.

---

## Controls

| Control | Action |
|---------|--------|
| **⏸ PAUSE** button (or **P** key) | Suspend all automatic antenna updates |
| **▶ RESUME** button (or **R** key) | Re-enable automatic updates |
| **🖥 Shortcut** button | Create a Windows desktop shortcut to launch the app |

---

## Configuration

Open `csnsat-windtrack.py` in a text editor. The constants near the top of the file are the only values you should need to change:

```python
SAT_HOST_DEFAULT = "192.168.113.121"  # fallback IP if discovery fails
SAT_PORT         = 12000   # CSN SAT command port
LISTEN_PORT      = 12001   # monitor for external PSTRotator commands
DISCOVERY_PORT   = 9932    # CSNTracker broadcast port
DISCOVERY_SECS   = 30      # seconds to wait for auto-discovery

INTERVAL_SEC = 300         # weather check interval (seconds)
IDLE_TIMEOUT = 300         # antenna-in-use guard timeout (seconds)
MIN_GUST_KT  = 15          # minimum gust speed to trigger a move (knots)

ICAO_DEFAULT = "CYYZ"      # pre-filled default in the startup dialog
```

---

## PSTRotator UDP Protocol

Commands are sent to the CSN SAT on **UDP port 12000**:

```
<PST><AZIMUTH>270.0</AZIMUTH></PST>
<PST><ELEVATION>0.0</ELEVATION></PST>
```

Responses from the CSN SAT arrive on **UDP port 12001** (monitored but not required).

---

## METAR Sources

| Priority | URL | Notes |
|----------|-----|-------|
| 1 (primary) | `tgftp.nws.noaa.gov` | NOAA plain-text, reliable, no key needed |
| 2 (fallback) | `metar.vatsim.net` | VATSIM public feed, no key needed |

METARs are typically updated **hourly**, with special SPECI reports issued automatically when conditions change significantly (wind shift ≥ 45°, gusts develop, etc.). Polling every 5 minutes ensures SPECI reports are captured promptly.

---

## License

MIT — free to use, modify, and distribute.
