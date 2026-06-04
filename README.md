# WaveSense — Wi-Fi CSI Occupancy Detection

> Enterprise occupancy detection using Wi-Fi Channel State Information (CSI). Built by **Indecisive** for UM Technothon.

## Architecture

```
 ESP32-TX               ESP32-RX              Mosquitto           Python Backend         Browser
 +---------+            +----------+           +----------+        +-------------+       +---------+
 | ESP-NOW | --802.11-->| CSI      | --MQTT--> | Broker   | --MQTT>| app.py      | --SSE>| Dashboard|
 | 100Hz   |  frames    | Extract  |  pub      | :1883    |  sub   | Variance    |       | Chart.js |
 | TX      |            | + MQTT   |           +----------+        | + Occupancy |       +---------+
 +---------+            | + Relay  |                               | + Ghost     |
                        +----+-----+                               | + Janitorial|
                             |                                     | + G.Home    |--+  +----------+
                        GPIO 18 (Relay)                            +-------------+  |  | Google   |
                        HVAC / Lights                                               +->| Home App |
                                                                                    +----------+
                                                               Google Home Graph API
```

**Data flow:** ESP32-TX broadcasts 802.11 null frames at 100 Hz → ESP32-RX captures CSI, publishes JSON via MQTT → Python backend computes rolling variance to detect occupancy → publishes relay commands → serves real-time dashboard via SSE.

---

## Features

- **Real-time presence detection** using CSI variance with hysteresis filtering
- **Automatic relay control** (HVAC/Lights ON/OFF based on occupancy)
- **Google Home integration** — virtual switch in Google Home shows ON/OFF based on occupancy, supports "Hey Google, is the room occupied?"
- **Ghost Booking auto-cancel** — detects empty rooms with active calendar bookings and auto-cancels after 15s
- **Janitorial heatmap** — 7x24 weekly occupancy heatmap tracking room usage with Clean/Moderate/Dirty status
- **Vital signs detection** — extracts breathing BPM (0.08–0.60 Hz) and heart BPM (0.80–2.00 Hz) from CSI
- **Live dashboard** — Chart.js graphs, gauges, vital signs, debug panel, recalibrate button
- **Hardware simulator** — test the full pipeline without ESP32 boards

---

## Setup Guide

### Prerequisites

| Requirement | Version | Install |
|---|---|---|
| **Python** | 3.10+ | [python.org](https://www.python.org/downloads/) |
| **Eclipse Mosquitto** | any | `winget install EclipseFoundation.Mosquitto` |
| **ESP-IDF** | v5.1+ (v5.3/5.4 recommended) | [ESP-IDF Install Guide](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/get-started/) |
| **Git** | any | `winget install Git.Git` |
| **ngrok** (optional) | any | [ngrok.com](https://ngrok.com) — for Google Home local testing |

### Hardware (optional — simulator available)

| Component | Qty | Notes |
|---|---|---|
| ESP32-WROOM-32 dev board | 2 | One TX, one RX |
| 5V relay module | 1 | Connect to GPIO 18 on RX |
| USB battery packs | 2 | Powers ESP32s for portable demo |
| Laptop with Wi-Fi | 1 | Runs Mosquitto + Python backend |
| Jumper wires | 3 | For relay connection (VCC, GND, SIG) |

---

### Option A: Run with Simulator (No Hardware)

This is the easiest way to test everything locally.

**Step 1 — Start Mosquitto broker:**
```bash
# If installed as a Windows service, it's already running. Otherwise:
mosquitto
```

**Step 2 — Install Python dependencies:**
```bash
cd backend
python -m venv .venv          # create virtual environment (if not exists)
.venv\Scripts\activate         # activate it (Windows)
pip install -r requirements.txt
```

**Step 3 — Start the backend:**
```bash
python app.py
```

**Step 4 — Start the simulator (in a new terminal):**
```bash
cd backend
.venv\Scripts\activate
python simulator.py
```

**Step 5 — Open the dashboard:**
Navigate to **http://localhost:5000** in your browser.

**Simulator controls:**
- Press **Enter** to toggle between Occupied/Empty states
- Type **q** + Enter to quit

---

### Option B: Run with ESP32 Hardware

**Step 1 — Set up the hotspot:**

On your laptop, enable Mobile Hotspot:
- **SSID:** `WaveSenseAP`
- **Password:** `wavesense123`
- Note the gateway IP: run `ipconfig` → look for "Mobile Hotspot" adapter (usually `192.168.137.1`)
- Note the Wi-Fi channel: run `netsh wlan show interfaces`

**Step 2 — Configure the ESP32 firmware:**

In `esp32-tx/main/main.c`:
```c
#define WIFI_CHANNEL 6  // ← Set to match your hotspot channel
```

In `esp32-rx/main/main.c`:
```c
#define WIFI_SSID       "WaveSenseAP"           // ← Your hotspot SSID
#define WIFI_PASS       "wavesense123"           // ← Your hotspot password
#define MQTT_BROKER_URI "mqtt://192.168.137.1"   // ← Your laptop's hotspot IP
#define WIFI_CHANNEL    6                        // ← Must match TX and hotspot
```

**Step 3 — Flash the ESP32s:**
```bash
# Flash TX (transmitter)
cd esp32-tx
idf.py set-target esp32
idf.py build
idf.py -p COMx flash    # Replace COMx with your TX port

# Flash RX (receiver)
cd ../esp32-rx
idf.py set-target esp32
idf.py build
idf.py -p COMy flash    # Replace COMy with your RX port
```

**Step 4 — Start the backend:**
```bash
mosquitto                # Start broker (if not a service)
cd backend
pip install -r requirements.txt
python app.py
```

**Step 5 — Open the dashboard:**
Navigate to **http://localhost:500** in your browser.

---

### Option C: Google Home Integration

Control your room occupancy from Google Home. A virtual switch shows **ON** when occupied and **OFF** when empty.

#### Prerequisites

| Requirement | Notes |
|---|---|
| Google Cloud project | [Create one here](https://console.cloud.google.com/) |
| Google Home Developer Console | [console.home.google.com](https://console.home.google.com) |
| Service Account JSON key | For Home Graph API authentication |
| ngrok (for local testing) | `winget install ngrok.ngrok` |

#### Step 1 — Google Cloud Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (e.g., `wavesense-smart-home`)
3. Enable these APIs:
   - **HomeGraph API** (`homegraph.googleapis.com`)
   - **Google Assistant API**
4. Create a **Service Account**:
   - IAM & Admin → Service Accounts → Create
   - Role: **Editor** (or Home Graph API Writer if available)
   - Generate a **JSON key** → download it
5. Place the key as `backend/service-account.json`

#### Step 2 — Configure the Backend

Edit `backend/google_home.py` and set your project ID:
```python
PROJECT_ID = "your-google-cloud-project-id"  # ← Your project ID
```

#### Step 3 — Google Home Developer Console Setup

1. Go to [console.home.google.com](https://console.home.google.com)
2. Create a new project → choose **Smart Home** → **Cloud-to-cloud**
3. In **Develop > Actions**:
   - Set fulfillment URL (see Step 4)
4. In **Develop > Account linking**:
   - **Client ID:** `wavesense-client`
   - **Client secret:** `wavesense-secret`
   - **Authorization URL:** `https://YOUR_NGROK_URL/oauth/authorize`
   - **Token URL:** `https://YOUR_NGROK_URL/oauth/token`

#### Step 4 — Expose Backend via ngrok

```bash
# Authenticate ngrok (first time only)
ngrok config add-authtoken YOUR_TOKEN

# Start tunnel
ngrok http 5000
```

Copy the HTTPS URL (e.g., `https://abc123.ngrok-free.dev`) and use it in Step 3.

#### Step 5 — Link in Google Home App

1. Open **Google Home app** on your phone
2. Tap **+** → **Set up device** → **Works with Google**
3. Search for your Action name
4. Click **Link WaveSense Account**
5. The **"Room Occupancy"** switch appears in your home

#### Step 6 — Test It

```bash
# Test Google Home manually (bypasses CSIProcessor)
# Report room as occupied
curl -X POST http://localhost:5000/api/google_home/test \
  -H "Content-Type: application/json" \
  -d '{"occupied": true}'

# Report room as empty
curl -X POST http://localhost:5000/api/google_home/test \
  -H "Content-Type: application/json" \
  -d '{"occupied": false}'

# Check health
curl http://localhost:5000/smarthome/health
```

#### How It Works

```
Occupancy changes → app.py calls report_state() → Google Home Graph API
                                                         ↓
Google Home app ← shows switch ON/OFF ← Google queries /smarthome (QUERY intent)
                                                         ↓
"Hey Google, is the room occupied?" → Google queries /smarthome → returns ON/OFF
```

#### Troubleshooting Google Home

| Issue | Fix |
|---|---|
| `service-account.json` not found | Place the file in `backend/` directory |
| `PROJECT_ID` not set | Edit `backend/google_home.py` and set it |
| Switch not appearing in Google Home | Run `POST /api/google_home/sync` to force a device sync |
| State not updating | Check console for `[GoogleHome]` log messages |
| ngrok URL changed | Update fulfillment URL in Actions Console |

---

## Outputs

The project produces these runtime outputs:

| Output | Where | Description |
|---|---|---|
| **Web Dashboard** | `http://localhost:5000` | Three-tab SPA (Dashboard, Janitorial Heatmap, Ghost Booking Rooms) with real-time SSE updates every 0.5s |
| **SSE Stream** | `/api/stream` | Server-Sent Events stream of all state data |
| **Status API** | `/api/status` | JSON snapshot of current occupancy, variance, scores |
| **Vitals API** | `/api/vitals` | Breathing BPM and heart BPM with confidence scores |
| **Relay Commands** | MQTT `wavesense/relay/control` | ON/OFF commands sent to ESP32-RX relay |
| **GPIO Output** | ESP32-RX GPIO 18 | Active-LOW relay for HVAC/lights control |
| **Google Home** | Google Home App | Virtual switch showing ON (occupied) / OFF (empty), voice query support |
| **Console Logs** | Terminal | Calibration progress, relay state changes, ghost booking alerts, watchdog triggers, Google Home state reports |

All state is held in memory (no database, no log files on disk).

---

## What's New Since Last Session

Your last commit was the **initial framework** on **May 17, 2026** (`3007cbf`). Since then, **helcurt7** added **5,243 lines** across 7 files, and **Google Home integration** was added on June 5:

### Code Changes (May 31 – June 5)

| File | What Changed |
|---|---|
| `backend/csi_processor.py` | Grew from ~50 lines to **1,768 lines**. Added `VitalSignDetector` (breathing/heart rate via FFT), multi-window fused variance, EMA smoothing, hysteresis, phase coherence veto, background recalibration, shadow scoring, fast vacancy detection, output debounce |
| `backend/app.py` | Added watchdog thread (forces relay OFF after 15s no data), vital signs API, SSE streaming, TX power control API, recalibrate endpoint, Google Home integration |
| `backend/google_home.py` | **New file** — Smart Home Action fulfillment (SYNC/QUERY/EXECUTE), OAuth2 server for account linking, Report State API for real-time updates |
| `backend/occupancy_manager.py` | Added 7x24 heatmap data structure, ghost booking detection (short sessions < 120s, extended > 7200s), session history tracking |
| `backend/templates/index.html` | Grew from skeleton to **937 lines**. Three-tab dashboard with Chart.js graphs, gauge visualization, vital signs display, janitorial heatmap grid, ghost booking table, calibration overlay, debug panel |
| `esp32-tx/main/main.c` | Added Wi-Fi connectivity, MQTT for TX power control, 100 Hz null frame transmission |
| `esp32-rx/main/main.c` | Added CSI capture via `esp_wifi_set_csi_rx_cb()`, 30s on-device calibration, per-subcarrier amplitude/phase computation, MQTT publishing, relay GPIO control |
| `csi_processor.md` | **New file** — design doc with v4.7.26 draft (additional features like vitals lock, spectral gate, spatial coherence) |

---

## Feature Check

| Feature | Status | Location |
|---|---|---|
| **Janitorial Heatmap** | ✅ Included | `occupancy_manager.py` (7x24 matrix), `index.html` (Janitorial Heatmap tab), `app.py` (SSE endpoint) |
| **Ghost Booking System** | ✅ Included | `occupancy_manager.py` (detection logic), `index.html` (Ghost Booking Rooms tab), `app.py` (SSE endpoint) |
| **Google Home Integration** | ✅ Included | `google_home.py` (Smart Home fulfillment), `app.py` (report state on change), `/smarthome` endpoint |
| **Vital Signs** | ✅ Included | `csi_processor.py` (`VitalSignDetector` class), `app.py` (`/api/vitals`), `index.html` (vitals display) |
| **Hardware Simulator** | ✅ Included | `backend/simulator.py` |

---

## MQTT Topics

| Topic | Direction | QoS | Payload |
|---|---|---|---|
| `wavesense/csi/raw` | ESP32-RX → Broker | 0 | `{"ts":...,"mac":"...","rssi":-42,"nsub":52,"mean":14.23,"amps":[...],"phase":[...]}` |
| `wavesense/relay/control` | Backend → ESP32-RX | 1 | `{"relay": true}` |
| `wavesense/tx/power` | Backend → ESP32-TX | 0 | `{"dbm": 20}` |

---

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Dashboard UI |
| `/api/stream` | GET | SSE real-time data stream |
| `/api/status` | GET | JSON snapshot of all state |
| `/api/vitals` | GET | Vital signs (breathing/heart BPM) |
| `/api/recalibrate` | POST | Trigger background recalibration |
| `/api/tx_power` | POST | Set ESP32 TX power (2–20 dBm) |
| `/api/google_home/sync` | POST | Manually trigger Google Home device sync |
| `/api/google_home/test` | POST | Test Google Home integration (bypasses CSIProcessor) |
| `/smarthome` | POST | Google Smart Home fulfillment (SYNC/QUERY/EXECUTE) |
| `/smarthome/health` | GET | Google Home integration health check |
| `/oauth/authorize` | GET/POST | OAuth2 authorization endpoint |
| `/oauth/token` | POST | OAuth2 token endpoint |

---

## Troubleshooting

| Issue | Fix |
|---|---|
| ESP32-RX not connecting to Wi-Fi | Verify SSID/password match hotspot. Check hotspot is 2.4 GHz (ESP32 doesn't support 5 GHz). |
| No CSI data in dashboard | Ensure TX and RX are on the same channel. Check Mosquitto is running (`net start mosquitto`). |
| Relay not toggling | Check GPIO 18 wiring. Verify MQTT topic subscription in RX serial monitor. |
| Dashboard shows "No data" | Check `python app.py` is running. Try `http://localhost:5000/api/status` for raw JSON. |
| Firewall blocking MQTT | Allow port 1883 through Windows Firewall for Mosquitto. |
| Simulator not connecting | Make sure Mosquitto is running on `localhost:1883` before starting the simulator. |
| Google Home: `service-account.json` not found | Place the file in `backend/` directory. Download from Google Cloud Console. |
| Google Home: Switch not appearing | Use [console.home.google.com](https://console.home.google.com) (not actions.google.com). Set up Cloud-to-cloud Smart Home action. |
| Google Home: "Something went wrong" | Ensure OAuth URLs point to your ngrok URL. Test with `POST /api/google_home/test`. |
| Google Home: State not updating | Check HomeGraph API is enabled in the same project as your service account. Use `POST /api/google_home/test` to verify. |
| Google Home: OAuth redirect fails | Ensure ngrok is running and URLs in Account linking match `https://YOUR_NGROK_URL/oauth/*`. |
| ngrok: version too old | Run `ngrok update` to get the latest version. |

---

## Team

Built by **Indecisive** for UM Technothon.
