"""
WaveSense DemoRoomManager
──────────────────────────
Multi-room demo infrastructure for Janitorial Heatmap and Ghost Booking tabs.

- Room 0 ("Meeting Room A") receives REAL data from the ESP32/simulator
- Rooms 1-3 are auto-simulated with different occupancy profiles
- Each room has its own OccupancyManager for heatmap + ghost booking
- Simulated rooms are seeded with pre-computed history so the dashboard
  shows interesting data immediately (no cold-start empty grids)
"""

import time
import math
import random
import threading
from occupancy_manager import OccupancyManager


# ─── Room profiles ────────────────────────────────────────────────
ROOM_PROFILES = [
    {"name": "Meeting Room A", "real": True},
    {"name": "Office B",       "real": False, "period": 180, "duty": 0.60},
    {"name": "Conference C",   "real": False, "period": 600, "duty": 0.35},
    {"name": "Lounge D",       "real": False, "period": 300, "duty": 0.20},
]


class DemoRoomManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._rooms = []
        for profile in ROOM_PROFILES:
            om = OccupancyManager(
                ghost_free_sec=5.0,
                over_time_sec=7200.0,
            )
            self._rooms.append({
                "name": profile["name"],
                "real": profile["real"],
                "profile": profile,
                "om": om,
                "sim_occupied": False,
                "sim_phase": random.uniform(0, 6.28),
            })

        # Seed simulated rooms with fake history so heatmaps look interesting
        self._seed_history()

        # Start simulation thread for fake rooms
        self._running = True
        self._sim_thread = threading.Thread(target=self._simulation_loop, daemon=True)
        self._sim_thread.start()

    def _seed_history(self):
        """Pre-fill simulated rooms' heatmaps with synthetic occupancy data.

        Directly manipulates the heatmap matrices so data is distributed
        across multiple hours/days for a realistic visual demo.
        """
        for i, room in enumerate(self._rooms):
            if room["real"]:
                continue
            om = room["om"]
            profile = room["profile"]
            duty = profile["duty"]

            # Seed the past 3 weekdays x 8 business hours (9am-5pm)
            # Each cell gets 3600s (1 hour) of tracked time
            for day_offset in range(3):  # today, yesterday, day before
                wd = (time.localtime().tm_wday - day_offset) % 7
                for hr in range(9, 17):  # 9am to 4pm
                    # Add some variation per hour
                    hour_duty = duty + random.uniform(-0.15, 0.15)
                    hour_duty = max(0.05, min(0.95, hour_duty))
                    total_sec = 3600.0
                    occ_sec = total_sec * hour_duty
                    om._heatmap_total[wd][hr] += total_sec
                    om._heatmap_occ[wd][hr] += occ_sec
                    om._total_tracked_sec += total_sec
                    om._total_occupied_sec += occ_sec

            # Seed a few ghost booking sessions
            om._session_count = random.randint(2, 8)
            om._sessions = [
                (time.time() - random.randint(300, 7200),
                 time.time() - random.randint(100, 299))
                for _ in range(min(5, om._session_count))
            ]
            om._last_tick = time.time()

    def _simulation_loop(self):
        """Background thread that simulates occupancy for fake rooms."""
        tick_interval = 2.0  # update every 2 seconds
        while self._running:
            time.sleep(tick_interval)
            with self._lock:
                for room in self._rooms:
                    if room["real"]:
                        continue
                    profile = room["profile"]
                    # Simple sine-wave state machine
                    t = time.time()
                    phase = room["sim_phase"] + t / profile["period"] * 6.28
                    duty = profile["duty"]
                    # Occupied when sine wave is above threshold
                    threshold = 1.0 - 2.0 * duty
                    occupied = math.sin(phase) > threshold
                    room["sim_occupied"] = occupied
                    room["om"].update(occupied)

    def update_primary(self, is_occupied: bool):
        """Update the primary room (Room 0) with real sensor data."""
        with self._lock:
            self._rooms[0]["om"].update(is_occupied)
            self._rooms[0]["sim_occupied"] = is_occupied

    def get_all_room_data(self) -> list:
        """Return per-room data for the SSE payload."""
        with self._lock:
            result = []
            for i, room in enumerate(self._rooms):
                om = room["om"]
                result.append({
                    "name": room["name"],
                    "is_primary": room["real"],
                    "occupied": room["sim_occupied"],
                    "ghost": om.get_ghost_booking_status(),
                    "heatmap": om.get_heatmap_data(),
                })
            return result

    def get_primary_ghost(self) -> dict:
        """Get ghost booking status for the primary room only."""
        with self._lock:
            return self._rooms[0]["om"].get_ghost_booking_status()

    def get_primary_heatmap(self) -> dict:
        """Get heatmap data for the primary room only."""
        with self._lock:
            return self._rooms[0]["om"].get_heatmap_data()

    def reset(self):
        """Reset all rooms to clean state."""
        with self._lock:
            for room in self._rooms:
                room["om"] = OccupancyManager(
                    ghost_free_sec=5.0,
                    over_time_sec=7200.0,
                )
                room["sim_occupied"] = False
            self._seed_history()

    def stop(self):
        """Stop the simulation thread."""
        self._running = False
