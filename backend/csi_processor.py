"""
WaveSense CSI Processor — Variance-Based Occupancy Detection

Maintains a rolling window of CSI mean-amplitude samples, computes variance,
and determines room occupancy using a threshold with hysteresis.

Why variance works:
- Empty room: CSI amplitudes are stable (low variance < 1.0)
- Person moving: multipath reflections cause amplitude fluctuations (variance 10-100+)
- Person standing still: body presence still changes baseline (variance 2-10)

Hysteresis prevents relay chattering when someone pauses momentarily.
"""

import threading
import collections
import numpy as np


class CSIProcessor:
    def __init__(self, window_size=100, threshold=5.0, hysteresis_samples=20):
        """
        Args:
            window_size: Number of samples in the rolling window.
            threshold: Variance above this = occupied.
            hysteresis_samples: Consecutive below-threshold samples before declaring empty.
        """
        self._window = collections.deque(maxlen=window_size)
        self._threshold = threshold
        self._hysteresis_samples = hysteresis_samples

        self._occupied = False
        self._hysteresis_counter = 0

        # Variance history for the frontend chart
        self._variance_history = collections.deque(maxlen=500)

        # Thread safety: MQTT thread writes, Flask thread reads
        self._lock = threading.Lock()

    def add_sample(self, mean_amplitude: float) -> bool:
        """
        Feed a new CSI mean-amplitude sample. Returns current occupancy state.

        Args:
            mean_amplitude: Mean amplitude across all subcarriers from one CSI frame.

        Returns:
            True if room is occupied, False if empty.
        """
        with self._lock:
            self._window.append(mean_amplitude)

            # Need at least 10 samples before we can compute meaningful variance
            if len(self._window) < 10:
                return self._occupied

            # Compute rolling variance
            arr = np.array(self._window)
            variance = float(np.var(arr))
            self._variance_history.append(variance)

            # Occupancy decision with hysteresis
            if variance > self._threshold:
                # High variance → definitely occupied
                self._hysteresis_counter = self._hysteresis_samples
                self._occupied = True
            else:
                # Low variance → might be empty, but wait for hysteresis
                if self._hysteresis_counter > 0:
                    self._hysteresis_counter -= 1
                    # Stay occupied during hysteresis period
                else:
                    self._occupied = False

            return self._occupied

    def get_variance(self) -> float:
        """Return the most recent variance value."""
        with self._lock:
            if self._variance_history:
                return self._variance_history[-1]
            return 0.0

    def get_history(self, n: int = 200) -> list:
        """Return the last N variance values for charting."""
        with self._lock:
            history = list(self._variance_history)
            return history[-n:] if len(history) > n else history

    def is_occupied(self) -> bool:
        """Return current occupancy state."""
        with self._lock:
            return self._occupied

    def get_threshold(self) -> float:
        """Return the current variance threshold."""
        return self._threshold
