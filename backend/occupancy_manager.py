"""
WaveSense Occupancy Manager — State Machine + Ghost Booking + Janitorial Tracking

Tracks occupancy state transitions, implements a 15-second ghost booking
auto-cancel timer (condensed for demo), and accumulates usage time for
the janitorial heatmap feature.
"""

import time
import threading


class OccupancyManager:
    # Ghost booking timeout: 15 seconds for live demo (production would be 5-15 min)
    GHOST_TIMEOUT = 15.0

    # Janitorial status thresholds (seconds occupied)
    CLEAN_THRESHOLD = 30
    MODERATE_THRESHOLD = 60

    def __init__(self):
        self._lock = threading.Lock()

        # State tracking
        self._state = "empty"           # "empty" or "occupied"
        self._session_start = time.time()
        self._last_update = time.time()

        # Ghost booking
        self._became_empty_at = None
        self._ghost_booking_triggered = False
        self._cancelled_count = 0

        # Janitorial tracking
        self._occupied_seconds = 0.0

    def update(self, is_occupied: bool):
        """
        Call once per CSI sample processing cycle.
        Manages state transitions, ghost booking timer, and usage accumulation.
        """
        with self._lock:
            now = time.time()
            dt = now - self._last_update
            self._last_update = now

            # Accumulate occupied time
            if self._state == "occupied":
                self._occupied_seconds += dt

            # State transitions
            if is_occupied:
                # Room is occupied
                if self._state == "empty":
                    self._state = "occupied"
                # Reset ghost booking state
                self._became_empty_at = None
                self._ghost_booking_triggered = False
            else:
                # Room appears empty
                if self._state == "occupied":
                    # Just transitioned to empty — start ghost booking timer
                    self._state = "empty"
                    self._became_empty_at = now
                    self._ghost_booking_triggered = False
                elif self._state == "empty" and self._became_empty_at is not None:
                    # Already empty — check if ghost booking should trigger
                    elapsed = now - self._became_empty_at
                    if elapsed >= self.GHOST_TIMEOUT and not self._ghost_booking_triggered:
                        self._trigger_ghost_booking()

    def _trigger_ghost_booking(self):
        """Fire the mock Google Calendar API call."""
        self._ghost_booking_triggered = True
        self._cancelled_count += 1
        print("\n" + "=" * 60)
        print("  API CALL: Google Calendar meeting cancelled due to no-show")
        print(f"  Cancellation #{self._cancelled_count}")
        print("=" * 60 + "\n")

    def get_ghost_booking_status(self) -> dict:
        """Return ghost booking state for the frontend."""
        with self._lock:
            if self._state == "occupied" or self._became_empty_at is None:
                return {
                    "active": False,
                    "countdown": 0.0,
                    "triggered": False,
                    "cancelled_count": self._cancelled_count
                }

            elapsed = time.time() - self._became_empty_at
            remaining = max(0.0, self.GHOST_TIMEOUT - elapsed)

            return {
                "active": not self._ghost_booking_triggered,
                "countdown": round(remaining, 1),
                "triggered": self._ghost_booking_triggered,
                "cancelled_count": self._cancelled_count
            }

    def get_heatmap_data(self) -> dict:
        """Return janitorial heatmap data for the frontend."""
        with self._lock:
            total = time.time() - self._session_start
            occupied = self._occupied_seconds

            if occupied < self.CLEAN_THRESHOLD:
                status = "Clean"
            elif occupied < self.MODERATE_THRESHOLD:
                status = "Moderate"
            else:
                status = "Dirty"

            return {
                "room": "WaveSense-Room-1",
                "total_session_seconds": round(total),
                "occupied_seconds": round(occupied),
                "status": status
            }
