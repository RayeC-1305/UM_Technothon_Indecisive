# WaveSense — Wi-Fi CSI Occupancy Detection

> Enterprise occupancy detection using Wi-Fi Channel State Information (CSI). Built for UM Technothon.

## Architecture

```
 ESP32-TX               ESP32-RX              Mosquitto           Python Backend         Browser
 +---------+            +----------+           +----------+        +-------------+       +---------+
 | ESP-NOW | --802.11-->| CSI      | --MQTT--> | Broker   | --MQTT>| app.py      | --SSE>| Dashboard|
 | 100Hz   |  frames    | Extract  |  pub      | :1883    |  sub   | Variance    |       | Chart.js |
 | TX      |            | + MQTT   |           +----------+        | + Occupancy |       +---------+
 +---------+            | + Relay  |                               | + Ghost     |
                        +----+-----+                               | + Janitorial|
                             |                                     +-------------+
                        GPIO 4 (Relay)
                        HVAC / Lights
```

**Data flow:** ESP32-TX broadcasts ESP-NOW packets → ESP32-RX captures CSI, publishes JSON via MQTT → Python backend computes rolling variance to detect occupancy → publishes relay commands → serves real-time dashboard via SSE.

## Features

- **Real-time presence detection** using CSI variance with hysteresis filtering
- **Automatic relay control** (HVAC/Lights ON/OFF based on occupancy)
- **Ghost Booking auto-cancel** — mock Google Calendar integration cancels meetings after 15s of emptiness
- **Janitorial heatmap** — tracks room usage time with Clean/Moderate/Dirty status
- **Live dashboard** — Chart.js variance graph, status badges, countdown timer

## Hardware Requirements

| Component | Qty | Notes |
|---|---|---|
| ESP32-WROOM-32 dev board | 2 | One TX, one RX |
| 5V relay module | 1 | Connect to GPIO 4 on RX |
| USB battery packs | 2 | Powers ESP32s for portable demo |
| Laptop with Wi-Fi | 1 | Runs Mosquitto + Python backend |
| Jumper wires | 3 | For relay connection (VCC, GND, SIG) |

## Software Requirements

- **ESP-IDF v5.1+** (v5.3 or v5.4 recommended) — [Install Guide](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/get-started/)
- **Python 3.10+**
- **Eclipse Mosquitto** — `winget install eclipse.mosquitto`

## Quick Start

### 1. Set Up the Hotspot

On your laptop, enable Mobile Hotspot:
- **SSID:** `WaveSenseAP`
- **Password:** `wavesense123`
- Note the gateway IP (check `ipconfig` → look for "Mobile Hotspot" adapter, usually `192.168.137.1`)
- Note the Wi-Fi channel (check with `netsh wlan show interfaces`)

### 2. Configure the ESP32 Firmware

**In `esp32-tx/main/main.c`:**
```c
#define WIFI_CHANNEL 6  // ← Set to match your hotspot channel
```

**In `esp32-rx/main/main.c`:**
```c
#define WIFI_SSID       "WaveSenseAP"           // ← Your hotspot SSID
#define WIFI_PASS       "wavesense123"           // ← Your hotspot password
#define MQTT_BROKER_URI "mqtt://192.168.137.1"   // ← Your laptop's hotspot IP
#define WIFI_CHANNEL    6                        // ← Must match TX and hotspot
```

### 3. Flash the ESP32s

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

### 4. Start the Backend

```bash
# Start Mosquitto broker (if not running as a service)
mosquitto

# In a new terminal, install Python dependencies
cd backend
pip install -r requirements.txt

# Run the backend
python app.py
```

### 5. Open the Dashboard

Navigate to **http://localhost:5000** in your browser.

---

## Testing Without Hardware

Use the simulator to test the full pipeline without ESP32 boards:

```bash
# Terminal 1: Start the backend
cd backend
python app.py

# Terminal 2: Start the simulator
cd backend
python simulator.py
```

**Simulator controls:**
- Press **Enter** to toggle between Occupied/Empty states
- Type **q** + Enter to quit

The simulator generates realistic CSI data at 50 Hz and publishes to the same MQTT topics as the real ESP32-RX.

---

## Demo Script (Technothon)

1. **Start everything:** Hotspot → Mosquitto → `python app.py` → open dashboard
2. **Show live detection:** Wave hand near the ESP32s (or press Enter in simulator) → variance spikes on chart, room shows "Occupied", relay turns ON
3. **Show ghost booking:** Stop moving → after 15 seconds, dashboard shows "Meeting cancelled" and prints the mock API call to console
4. **Show janitorial:** Switch to "Janitorial Heatmap" tab → watch occupied time accumulate → status transitions from Clean → Moderate → Dirty
5. **Reset and repeat:** Walk away to show empty detection, then return for another cycle

---

## MQTT Topics

| Topic | Direction | QoS | Payload |
|---|---|---|---|
| `wavesense/csi/raw` | ESP32-RX → Broker | 0 | `{"ts":...,"mac":"...","rssi":-42,"nsub":52,"mean":14.23,"amps":[...]}` |
| `wavesense/relay/control` | Backend → ESP32-RX | 1 | `{"relay": true}` |

---

## Troubleshooting

| Issue | Fix |
|---|---|
| ESP32-RX not connecting to Wi-Fi | Verify SSID/password match hotspot. Check hotspot is 2.4 GHz (ESP32 doesn't support 5 GHz). |
| No CSI data in dashboard | Ensure TX and RX are on the same channel. Check Mosquitto is running (`net start mosquitto`). |
| Relay not toggling | Check GPIO 4 wiring. Verify MQTT topic subscription in RX serial monitor. |
| Dashboard shows "No data" | Check `python app.py` is running. Try `http://localhost:5000/api/status` for raw JSON. |
| Firewall blocking MQTT | Allow port 1883 through Windows Firewall for Mosquitto. |

## Team

Built by **Indecisive** for UM Technothon.
