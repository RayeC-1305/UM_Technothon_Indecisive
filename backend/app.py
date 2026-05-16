"""
WaveSense Backend — Flask Server + MQTT Bridge

The central hub of the WaveSense system:
- Subscribes to raw CSI data from ESP32-RX via MQTT
- Processes CSI through variance algorithm to detect occupancy
- Manages occupancy state, ghost booking timer, and janitorial tracking
- Publishes relay commands back to ESP32-RX
- Serves the web dashboard with real-time SSE updates

Usage:
    python app.py

Make sure Mosquitto broker is running on localhost:1883.
"""

import json
import time
import threading
from flask import Flask, render_template, Response, jsonify, stream_with_context
import paho.mqtt.client as mqtt
from csi_processor import CSIProcessor
from occupancy_manager import OccupancyManager

# ──────────────────────────────────────────────
#  Flask App
# ──────────────────────────────────────────────

app = Flask(__name__)

# ──────────────────────────────────────────────
#  Shared State (thread-safe via locks)
# ──────────────────────────────────────────────

lock = threading.Lock()
csi = CSIProcessor(window_size=100, threshold=5.0, hysteresis_samples=20)
occupancy = OccupancyManager()
relay_state = False
last_csi_timestamp = 0.0

# ──────────────────────────────────────────────
#  MQTT Client Setup
# ──────────────────────────────────────────────

def on_connect(client, userdata, flags, reason_code, properties):
    """Called when MQTT connection to broker is established."""
    print(f"[MQTT] Connected to broker: {reason_code}")
    client.subscribe("wavesense/csi/raw", qos=0)
    print("[MQTT] Subscribed to wavesense/csi/raw")


def on_message(client, userdata, msg):
    """Called for every incoming MQTT message."""
    global relay_state, last_csi_timestamp

    if msg.topic != "wavesense/csi/raw":
        return

    try:
        data = json.loads(msg.payload.decode())
        mean_amp = data.get("mean", 0.0)

        with lock:
            last_csi_timestamp = time.time()

            # Process through variance algorithm
            is_occupied = csi.add_sample(mean_amp)

            # Update occupancy state machine
            occupancy.update(is_occupied)

            # Auto-control relay based on occupancy changes
            if is_occupied != relay_state:
                relay_state = is_occupied
                payload = json.dumps({"relay": relay_state})
                client.publish("wavesense/relay/control", payload, qos=1)
                state_str = "ON" if relay_state else "OFF"
                print(f"[Relay] HVAC/Lights → {state_str}")

    except (json.JSONDecodeError, KeyError) as e:
        print(f"[MQTT] Parse error: {e}")


# Create and configure MQTT client (paho-mqtt 2.x API)
mqtt_client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id="wavesense-backend"
)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

# Connect to local Mosquitto broker
try:
    mqtt_client.connect("localhost", 1883, keepalive=60)
    mqtt_client.loop_start()
    print("[MQTT] Connecting to broker at localhost:1883...")
except Exception as e:
    print(f"[MQTT] WARNING: Could not connect to broker: {e}")
    print("[MQTT] Make sure Mosquitto is running on port 1883")

# ──────────────────────────────────────────────
#  Flask Routes
# ──────────────────────────────────────────────

@app.route('/')
def index():
    """Serve the main dashboard."""
    return render_template('index.html')


@app.route('/api/status')
def status():
    """Return a single JSON snapshot of the current state (for debugging)."""
    with lock:
        return jsonify({
            "occupied": csi.is_occupied(),
            "variance": round(csi.get_variance(), 2),
            "relay_state": relay_state,
            "ghost_booking": occupancy.get_ghost_booking_status(),
            "heatmap": occupancy.get_heatmap_data(),
            "connected": (time.time() - last_csi_timestamp) < 5.0
        })


@app.route('/api/stream')
def stream():
    """
    Server-Sent Events endpoint for real-time dashboard updates.
    Pushes complete state every 500ms.
    """
    def generate():
        while True:
            with lock:
                data = {
                    "occupied": csi.is_occupied(),
                    "variance": round(csi.get_variance(), 2),
                    "variance_history": [round(v, 2) for v in csi.get_history(200)],
                    "relay_state": relay_state,
                    "ghost_booking": occupancy.get_ghost_booking_status(),
                    "heatmap": occupancy.get_heatmap_data(),
                    "connected": (time.time() - last_csi_timestamp) < 5.0
                }
            yield f"data: {json.dumps(data)}\n\n"
            time.sleep(0.5)  # 2 updates per second

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )

# ──────────────────────────────────────────────
#  Entry Point
# ──────────────────────────────────────────────

if __name__ == '__main__':
    print("=" * 50)
    print("  WaveSense Occupancy Detection Backend")
    print("  Dashboard: http://localhost:5000")
    print("  MQTT Broker: localhost:1883")
    print("=" * 50)
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
