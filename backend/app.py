#!/usr/bin/env python3
"""
WaveSense Backend – Ultimate Edition
=====================================
Modular, enterprise‑ready, with all v7.3 features.
"""
import json
import time
import threading
import numpy as np
from flask import Flask, render_template, Response, jsonify, stream_with_context, request
import paho.mqtt.client as mqtt

from csi_processor import CSIProcessor
from demo_rooms import DemoRoomManager
from google_home import google_home, report_state, request_sync

MQTT_BROKER = "localhost"
MQTT_PORT   = 1883

app  = Flask(__name__)
app.register_blueprint(google_home)
lock = threading.Lock()

csi = CSIProcessor(
    calibration_sec           = 30.0,
    sample_rate_hz            = 100.0,
    sensitivity               = 2.0,
    ema_alpha                 = 0.08,
    hysteresis_sec            = 8.0,
    min_trigger_frames        = 30,
    amp_outlier_sigma         = 4.0,
    sustain_frames            = 10,
    use_phase_veto            = True,
    phase_coherence_min       = 0.25,
    cal_trim_pct              = 0.10,
    short_win_frames          = 100,
    mid_win_frames            = 300,
    long_win_frames           = 600,
    win_weights               = (0.50, 0.30, 0.20),
    bg_recal_interval_sec     = 120.0,
    bg_recal_window_sec       = 30.0,
    bg_recal_max_motion       = 0.5,
    bg_recal_max_still        = 0.25,
    bg_recal_blend_alpha      = 0.30,
    still_sustain_frames      = 15,
    still_max_rise_per_frame  = 0.02,
    use_phase_still           = True,
    phase_still_weight        = 0.4,
    vital_enabled             = True,
    vital_window_sec          = 30.0,
    vital_min_occupied_sec    = 10.0,
    vital_update_every_sec    = 1.0,
    strong_reset_thr          = 0.7,
    weak_hold_thr             = 0.5,
    quick_off_threshold_frames = 30,
    bg_recal_timeout_sec      = 3600.0,
)

demo_rooms      = DemoRoomManager()
relay_state     = False
last_csi_ts     = 0.0
hw_calibrated   = False
hw_cal_progress = 0.0

def publish_relay(state: bool):
    mqtt_client.publish("wavesense/relay/control",
                        "true" if state else "false", qos=1)

def process_frame(data: dict):
    global relay_state, last_csi_ts, hw_calibrated, hw_cal_progress
    with lock:
        last_csi_ts     = time.time()
        hw_calibrated   = data.get("cal", False)
        hw_cal_progress = data.get("cal_pct", 0.0)
        is_occ = csi.add_sample(
            mean_amp = float(data.get("mean", 0.0)),
            amps     = data.get("amps", []),
            phase    = data.get("phase", []),
        )
        if not hw_calibrated or csi.is_calibrating():
            return
        demo_rooms.update_primary(is_occ)
        if is_occ != relay_state:
            relay_state = is_occ
            publish_relay(relay_state)
            report_state(is_occ)
            print(f"[Relay] -> {'ON  (OCCUPIED)' if relay_state else 'OFF (EMPTY)'}")

# ── Watchdog thread (v7.3) ──
def watchdog_thread():
    global relay_state
    while True:
        time.sleep(1)
        if csi.check_watchdog(15.0):
            with lock:
                if relay_state:
                    relay_state = False
                    publish_relay(False)
                    report_state(False)
                    print("[Watchdog] No CSI for 15 s – forcing relay OFF")
threading.Thread(target=watchdog_thread, daemon=True).start()

def on_connect(client, userdata, flags, rc, props):
    print(f"[MQTT] Connected rc={rc}")
    client.subscribe("wavesense/csi/raw", qos=0)

def on_message(client, userdata, msg):
    try:
        process_frame(json.loads(msg.payload.decode()))
    except Exception as e:
        print(f"[MQTT] Error: {e}")

mqtt_client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id="wavesense-backend-ultimate",
)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
try:
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()
    print(f"[MQTT] Connecting to {MQTT_BROKER}:{MQTT_PORT}")
except Exception as e:
    print(f"[MQTT] WARNING: {e}")

def _to_json(obj):
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    elif isinstance(obj, dict):
        return {k: _to_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_to_json(v) for v in obj]
    return obj

def _snapshot() -> dict:
    cal_prog = min(csi.calibration_progress(), hw_cal_progress / 100.0)
    debug    = csi.get_debug()
    vitals   = csi.get_vitals()
    data = {
        "occupied":         csi.is_occupied(),
        "calibrating":      csi.is_calibrating() or not hw_calibrated,
        "cal_progress":     round(cal_prog, 2),
        "motion_score":     round(float(csi.get_score()), 3),
        "still_score":      round(float(csi.get_still_score()), 3),
        "motion_threshold": round(float(csi.get_score_threshold()), 2),
        "still_threshold":  round(float(csi.get_still_threshold()), 2),
        "variance":         round(float(csi.get_variance()), 5),
        "var_threshold":    round(float(csi.get_threshold()), 5),
        "variance_history": [round(float(v), 5) for v in csi.get_history(200)],
        "score_history":    [round(float(s), 3) for s in csi.get_score_history(200)],
        "still_history":    [round(float(s), 3) for s in csi.get_still_history(200)],
        "relay_state":      relay_state,
        "ghost_booking":    demo_rooms.get_primary_ghost(),
        "heatmap":          demo_rooms.get_primary_heatmap(),
        "rooms":            demo_rooms.get_all_room_data(),
        "connected":        (time.time() - last_csi_ts) < 5.0,
        "debug":            debug,
        "bg_recal_count":   debug.get("bg_recal_count", 0),
        "bg_recal_skipped": debug.get("bg_recal_skipped", 0),
        "frames_rejected":  debug.get("frames_rejected", 0),
        "phase_reject_count": debug.get("phase_reject_count", 0),
        "phase_coherence":  debug.get("phase_coherence", 1.0),
        "last_phase_corr":  debug.get("last_phase_corr", 1.0),
        "vitals": {
            "breathing_bpm":   vitals.get("breathing_bpm"),
            "heart_bpm":       vitals.get("heart_bpm"),
            "breathing_conf":  vitals.get("breathing_conf", 0.0),
            "heart_conf":      vitals.get("heart_conf", 0.0),
            "valid_breathing": vitals.get("valid_breathing", False),
            "valid_heart":     vitals.get("valid_heart", False),
            "breath_signal":   vitals.get("breath_signal", []),
            "heart_signal":    vitals.get("heart_signal", []),
        },
    }
    return _to_json(data)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def status():
    with lock: return jsonify(_snapshot())

@app.route('/api/stream')
def stream():
    def generate():
        while True:
            with lock: data = _snapshot()
            yield f"data: {json.dumps(data)}\n\n"
            time.sleep(0.5)
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/api/vitals')
def vitals():
    v = csi.get_vitals()
    return jsonify({
        "breathing": {
            "bpm":        v.get("breathing_bpm"),
            "confidence": v.get("breathing_conf", 0.0),
            "valid":      v.get("valid_breathing", False),
            "signal":     v.get("breath_signal", []),
        },
        "heart": {
            "bpm":        v.get("heart_bpm"),
            "confidence": v.get("heart_conf", 0.0),
            "valid":      v.get("valid_heart", False),
            "signal":     v.get("heart_signal", []),
        },
        "occupied": csi.is_occupied(),
        "ts":       round(time.time(), 3),
    })

@app.route('/api/recalibrate', methods=['POST'])
def recalibrate():
    with lock: csi.force_recalibrate()
    return jsonify({"status": "recalibrating"})

@app.route('/api/tx_power', methods=['POST'])
def set_tx_power():
    power = int(request.json.get('power', 20))
    if not (2 <= power <= 20):
        return jsonify({"status": "error", "reason": "power must be 2–20 dBm"}), 400
    mqtt_client.publish("wavesense/tx/power", str(power), qos=1)
    return jsonify({"status": "ok", "power_dbm": power})

@app.route('/api/google_home/sync', methods=['POST'])
def google_home_sync():
    """Manually trigger Google Home device sync."""
    ok = request_sync()
    return jsonify({"status": "ok" if ok else "error", "synced": ok})

@app.route('/api/google_home/test', methods=['POST'])
def google_home_test():
    """
    Manually test Google Home integration.
    Sends a state report to Google Home without changing actual occupancy.
    Usage: POST /api/google_home/test with JSON body {"occupied": true}
    """
    data = request.get_json(silent=True) or {}
    occupied = data.get("occupied", True)
    result = report_state(occupied)
    return jsonify({
        "status": "ok" if result else "error",
        "reported_state": "ON (occupied)" if occupied else "OFF (empty)",
        "google_home_updated": result
    })

@app.route('/api/demo/reset', methods=['POST'])
def demo_reset():
    """Reset all demo rooms to clean state."""
    demo_rooms.reset()
    return jsonify({"status": "ok", "message": "All rooms reset"})

if __name__ == '__main__':
    print("=" * 60)
    print("  WaveSense Backend Ultimate – v7.3 Hardened & Modular")
    print("  Keep room EMPTY for 30 seconds after start")
    print("=" * 60)
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
