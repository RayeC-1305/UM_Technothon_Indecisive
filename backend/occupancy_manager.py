"""
WaveSense OccupancyManager v3.0
────────────────────────────────
Tracks room occupancy over time and exposes:
  • per-hour / per-weekday heatmap
  • ghost-booking detection  (room empty but calendar slot active)
  • session statistics

Ghost Booking Logic (v3.0):
  IDLE → (person enters) → ACTIVE
  ACTIVE → (person leaves) → EMPTY_COUNTDOWN (5s timer)
  EMPTY_COUNTDOWN → (timer expires) → FREED (room available for others)
  EMPTY_COUNTDOWN → (person returns) → ACTIVE (cancel countdown)
  FREED → (person enters) → ACTIVE (new booking)
"""

import time
import collections
from datetime import datetime

# Ghost booking states
GHOST_IDLE     = "idle"       # No booking detected
GHOST_ACTIVE   = "active"     # Room occupied (booked)
GHOST_COUNTDOWN = "countdown" # Room empty, counting down to free
GHOST_FREED    = "freed"      # Room freed for others (ghost booking detected)


class OccupancyManager:
    def __init__(
        self,
        ghost_free_sec:        float = 5.0,     # Seconds empty before freeing room
        over_time_sec:         float = 7200.0,   # Max session duration before overrun
        heatmap_resolution:    int   = 1,
    ):
        self._ghost_free_sec  = ghost_free_sec
        self._over_time_sec   = over_time_sec

        # Occupancy state
        self._occupied        = False
        self._session_start: float | None = None
        self._sessions: list[tuple[float, float]] = []
        self._session_count   = 0

        # Ghost booking state machine
        self._ghost_state     = GHOST_IDLE
        self._ghost_countdown_start: float | None = None
        self._ghost_reason    = ""
        self._overrun         = False

        # Heatmap tracking
        self._heatmap_occ     = [[0.0] * 24 for _ in range(7)]
        self._heatmap_total   = [[0.0] * 24 for _ in range(7)]
        self._last_tick       = time.time()
        self._total_occupied_sec = 0.0
        self._total_tracked_sec  = 0.0

    def update(self, is_occupied: bool):
        now = time.time()
        dt  = now - self._last_tick
        self._last_tick = now
        self._total_tracked_sec += dt

        # Update heatmap
        dt_now = datetime.now()
        wd, hr = dt_now.weekday(), dt_now.hour
        self._heatmap_total[wd][hr] += dt
        if is_occupied:
            self._heatmap_occ[wd][hr] += dt
            self._total_occupied_sec += dt

        # Handle state transitions
        if is_occupied != self._occupied:
            self._on_transition(is_occupied, now)
        self._occupied = is_occupied

        # Evaluate ghost booking state
        self._evaluate_ghost(now)

    def _on_transition(self, new_state: bool, now: float):
        """Handle occupancy state transitions."""
        if new_state:
            # Person entered the room
            self._session_start = now
            self._overrun = False

            if self._ghost_state == GHOST_COUNTDOWN:
                # Person came back during countdown - cancel, room still in use
                self._ghost_state = GHOST_ACTIVE
                self._ghost_countdown_start = None
                self._ghost_reason = ""
            elif self._ghost_state == GHOST_FREED:
                # New booking after room was freed
                self._ghost_state = GHOST_ACTIVE
                self._ghost_reason = ""
            elif self._ghost_state == GHOST_IDLE:
                # First occupancy
                self._ghost_state = GHOST_ACTIVE
                self._ghost_reason = ""
        else:
            # Person left the room
            if self._session_start is not None:
                duration = now - self._session_start
                self._sessions.append((self._session_start, now))
                self._session_count += 1
                if len(self._sessions) > 500:
                    self._sessions.pop(0)
                self._session_start = None

            # Start countdown to free the room
            if self._ghost_state == GHOST_ACTIVE:
                self._ghost_state = GHOST_COUNTDOWN
                self._ghost_countdown_start = now
                self._ghost_reason = "Room empty — freeing in {self._ghost_free_sec:.0f}s"

    def _evaluate_ghost(self, now: float):
        """Evaluate ghost booking state machine."""
        if self._ghost_state == GHOST_ACTIVE:
            # Check for overrun (session too long)
            if self._session_start is not None:
                elapsed = now - self._session_start
                if elapsed > self._over_time_sec:
                    self._overrun = True
                    self._ghost_reason = (
                        f"Session overrun: {elapsed/3600:.1f}h — "
                        "possible meeting running over or sensor stuck."
                    )

        elif self._ghost_state == GHOST_COUNTDOWN:
            # Check if countdown has expired
            if self._ghost_countdown_start is not None:
                if now - self._ghost_countdown_start >= self._ghost_free_sec:
                    # Room is now freed (ghost booking detected)
                    self._ghost_state = GHOST_FREED
                    self._ghost_countdown_start = None
                    self._ghost_reason = (
                        f"Room freed after {self._ghost_free_sec:.0f}s empty — "
                        "ghost booking cancelled. Available for others."
                    )

    def get_ghost_booking_status(self) -> dict:
        now = time.time()
        session_duration = (
            round(now - self._session_start, 1)
            if self._session_start else None
        )
        recent_durations = [
            round(e - s, 1) for s, e in self._sessions[-5:]
        ]

        # Compute countdown (seconds until room is freed)
        countdown = 0
        if self._ghost_state == GHOST_COUNTDOWN and self._ghost_countdown_start is not None:
            elapsed = now - self._ghost_countdown_start
            countdown = max(0, round(self._ghost_free_sec - elapsed, 1))

        # Map ghost state to frontend fields
        is_active    = (self._ghost_state == GHOST_ACTIVE)
        is_triggered = (self._ghost_state == GHOST_FREED)

        return {
            "ghost_state":        self._ghost_state,
            "ghost_booking":      self._ghost_state == GHOST_FREED,
            "overrun":            self._overrun,
            "reason":             self._ghost_reason,
            "session_duration_s": session_duration,
            "recent_sessions_s":  recent_durations,
            "session_count":      self._session_count,
            "occupancy_pct":      round(
                100.0 * self._total_occupied_sec
                / max(1.0, self._total_tracked_sec), 1
            ),
            # Fields the frontend expects:
            "active":             is_active,
            "triggered":          is_triggered,
            "countdown":          countdown,
        }

    def get_heatmap_data(self) -> dict:
        matrix = []
        for wd in range(7):
            row = []
            for hr in range(24):
                total = self._heatmap_total[wd][hr]
                occ   = self._heatmap_occ[wd][hr]
                row.append(round(occ / total, 3) if total > 0 else 0.0)
            matrix.append(row)
        return {
            "matrix":   matrix,
            "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "hours":    list(range(24)),
        }
