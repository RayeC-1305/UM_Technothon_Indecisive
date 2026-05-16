"""
WaveSense CSI Data Simulator — Hardware-Free Testing

Generates realistic fake CSI data and publishes to MQTT, allowing the full
pipeline (MQTT → backend → dashboard) to be tested without ESP32 hardware.

Usage:
    python simulator.py

Controls:
    Press Enter to toggle between occupied/empty states.
    Type 'q' + Enter to quit.

The simulator publishes to the same MQTT topics the real ESP32-RX uses.
Run app.py in another terminal to see the dashboard respond.
"""

import json
import time
import random
import math
import threading
import paho.mqtt.client as mqtt


class CSISimulator:
    """Generates realistic CSI amplitude data with distinct occupied/empty signatures."""

    def __init__(self, broker_host="localhost", broker_port=1883):
        self._broker_host = broker_host
        self._broker_port = broker_port
        self._occupied = False
        self._phase = 0.0
        self._running = False

        # MQTT client
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="wavesense-simulator"
        )

    def _generate_sample(self) -> dict:
        """Generate one CSI sample matching the ESP32-RX JSON format."""
        if self._occupied:
            # Occupied: high variance with sinusoidal movement pattern
            base = 15.0
            movement = 5.0 * math.sin(self._phase * 0.1)
            noise = random.gauss(0, 3.0)
            mean_amp = base + movement + noise
        else:
            # Empty: low variance, just RF noise
            mean_amp = 15.0 + random.gauss(0, 0.8)

        self._phase += 1.0

        # Generate per-subcarrier amplitudes (52 subcarriers for HT20)
        nsub = 52
        amps = [max(0.0, mean_amp + random.gauss(0, 1.5)) for _ in range(nsub)]

        return {
            "ts": int(time.time() * 1000),
            "mac": "1a:00:00:00:00:00",
            "rssi": random.randint(-50, -30),
            "nsub": nsub,
            "mean": round(mean_amp, 2),
            "amps": [round(a, 1) for a in amps]
        }

    def _input_thread(self):
        """Listen for keyboard input to toggle state."""
        while self._running:
            try:
                cmd = input()
                if cmd.strip().lower() == 'q':
                    self._running = False
                    break
                else:
                    self._occupied = not self._occupied
                    state = "OCCUPIED" if self._occupied else "EMPTY"
                    print(f"\n[Simulator] State → {state}")
            except EOFError:
                break

    def run(self, rate_hz=50):
        """
        Start the simulator. Publishes CSI data at the given rate.

        Args:
            rate_hz: Samples per second (default 50, gentler than real 100 Hz).
        """
        try:
            self._client.connect(self._broker_host, self._broker_port, keepalive=60)
            self._client.loop_start()
            print(f"[Simulator] Connected to MQTT broker at {self._broker_host}:{self._broker_port}")
        except Exception as e:
            print(f"[Simulator] ERROR: Cannot connect to broker: {e}")
            print("[Simulator] Make sure Mosquitto is running on port 1883")
            return

        self._running = True

        # Start input listener in background
        input_t = threading.Thread(target=self._input_thread, daemon=True)
        input_t.start()

        print("[Simulator] Generating CSI data at {} Hz".format(rate_hz))
        print("[Simulator] Press Enter to toggle occupied/empty, 'q' to quit")
        print(f"[Simulator] Initial state: {'OCCUPIED' if self._occupied else 'EMPTY'}")
        print("-" * 50)

        sample_count = 0
        interval = 1.0 / rate_hz

        while self._running:
            sample = self._generate_sample()
            payload = json.dumps(sample)
            self._client.publish("wavesense/csi/raw", payload, qos=0)

            sample_count += 1
            if sample_count % rate_hz == 0:
                state = "OCCUPIED" if self._occupied else "EMPTY"
                print(f"[Simulator] {sample_count} samples sent | State: {state} | Mean: {sample['mean']:.1f}")

            time.sleep(interval)

        self._client.loop_stop()
        self._client.disconnect()
        print("[Simulator] Stopped.")


if __name__ == "__main__":
    sim = CSISimulator()
    sim.run(rate_hz=50)
