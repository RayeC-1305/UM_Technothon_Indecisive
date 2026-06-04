"""
WaveSense OccupancyManager v2.1
────────────────────────────────
Tracks room occupancy over time and exposes:
  • per-hour / per-weekday heatmap
  • ghost-booking detection  (room empty but calendar slot active)
  • session statistics
"""

import time
import collections
from datetime import datetime


class OccupancyManager:
    def __init__(
        self,
        ghost_empty_sec:       float = 600.0,
        ghost_short_session_sec: float = 120.0,
        over_time_sec:         float = 7200.0,
        heatmap_resolution:    int   = 1,
    ):
        self._ghost_empty_sec     = ghost_empty_sec
        self._ghost_short_session = ghost_short_session_sec
        self._over_time_sec       = over_time_sec
        self._occupied      = False
        self._state_since   = time.time()
        self._sessions: list[tuple[float, float]] = []
        self._session_start: float | None = None
        self._ghost_booking = False
        self._ghost_reason  = ""
        self._overrun       = False
        self._heatmap_occ   = [[0.0] * 24 for _ in range(7)]
        self._heatmap_total  = [[0.0] * 24 for _ in range(7)]
        self._last_tick      = time.time()
        self._total_occupied_sec  = 0.0
        self._total_tracked_sec   = 0.0
        self._session_count       = 0

    def update(self, is_occupied: bool):
        now = time.time()
        dt  = now - self._last_tick
        self._last_tick = now
        self._total_tracked_sec += dt
        dt_now = datetime.now()
        wd, hr = dt_now.weekday(), dt_now.hour
        self._heatmap_total[wd][hr] += dt
        if is_occupied:
            self._heatmap_occ[wd][hr] += dt
            self._total_occupied_sec  += dt
        if is_occupied != self._occupied:
            self._on_transition(is_occupied, now)
        self._occupied = is_occupied
        self._evaluate_ghost(now)

    def _on_transition(self, new_state: bool, now: float):
        if new_state:
            self._session_start = now
            self._ghost_booking = False
            self._ghost_reason  = ""
            self._overrun       = False
        else:
            if self._session_start is not None:
                duration = now - self._session_start
                self._sessions.append((self._session_start, now))
                self._session_count += 1
                if len(self._sessions) > 500:
                    self._sessions.pop(0)
                self._session_start = None
                if duration < self._ghost_short_session:
                    self._ghost_booking = True
                    self._ghost_reason  = (
                        f"Very short session ({duration:.0f}s). "
                        "Possible ghost booking or sensor glitch."
                    )

    def _evaluate_ghost(self, now: float):
        if self._occupied and self._session_start is not None:
            elapsed = now - self._session_start
            if elapsed > self._over_time_sec:
                self._overrun       = True
                self._ghost_booking = True
                self._ghost_reason  = (
                    f"Room occupied for {elapsed/3600:.1f}h — possible meeting overrun "
                    "or ghost booking (sensor stuck)."
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
        return {
            "ghost_booking":      self._ghost_booking,
            "overrun":            self._overrun,
            "reason":             self._ghost_reason,
            "session_duration_s": session_duration,
            "recent_sessions_s":  recent_durations,
            "session_count":      self._session_count,
            "occupancy_pct":      round(
                100.0 * self._total_occupied_sec
                / max(1.0, self._total_tracked_sec), 1
            ),
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
