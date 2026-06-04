#!/usr/bin/env python3
"""
WaveSense CSI Processor – Enterprise‑Grade Production Release v4.7.14
===================================================================
All fixes from v4.7.13 plus:
  - FIX: Still‑while‑occupied flicker (0.5s empty → back to occupied).
    Root cause: fast_vacancy and quick_off fired when body sat still
    because motion/still scores dropped near baseline.
    Fix 1 (correlation guard): fast_vacancy and quick_off now require
    last_corr > 0.93.  A person sitting still still distorts the
    channel; a truly empty room scores higher correlation.
    Fix 2 (output debounce): reported occupancy only flips to empty
    after `report_vacant_delay_sec` of sustained internal vacancy.
    Sub‑second flickers are absorbed entirely before callers see them.
"""

import numpy as np
import collections
import threading
import time
import logging
from typing import Optional, Tuple

try:
    from numpy.lib.stride_tricks import sliding_window_view
except ImportError:
    sliding_window_view = None

try:
    from scipy.signal import butter, sosfiltfilt, welch as scipy_welch
    from scipy.signal import find_peaks, decimate as scipy_decimate
    _SCIPY = True
except ImportError:
    _SCIPY = False
    logging.warning("scipy not found – vital signs disabled. pip install scipy")

_log = logging.getLogger("WaveSense")


# ═══════════════════════════════════════════════════════════
#  Utility helpers
# ═══════════════════════════════════════════════════════════

def _hampel_vec(arr: np.ndarray, half_k: int = 5, n_sigma: float = 3.0) -> np.ndarray:
    if sliding_window_view is None:
        out = arr.copy()
        for i in range(len(arr)):
            lo = max(0, i - half_k)
            hi = min(len(arr), i + half_k + 1)
            win = arr[lo:hi]
            med = np.median(win)
            mad = np.median(np.abs(win - med))
            thr = n_sigma * 1.4826 * max(mad, 1e-30)
            if abs(arr[i] - med) > thr:
                out[i] = med
        return out
    n = len(arr)
    if n < 2 * half_k + 1:
        return arr.copy()
    padded  = np.pad(arr, half_k, mode='edge')
    windows = sliding_window_view(padded, 2 * half_k + 1)
    medians = np.median(windows, axis=1)
    mads    = np.median(np.abs(windows - medians[:, None]), axis=1)
    thresh  = n_sigma * 1.4826 * np.maximum(mads, 1e-30)
    out     = arr.copy()
    mask    = np.abs(arr - medians) > thresh
    out[mask] = medians[mask]
    return out

def _parabolic_interp(freqs: np.ndarray, psd: np.ndarray, peak_idx: int) -> float:
    if peak_idx <= 0 or peak_idx >= len(psd) - 1:
        return float(freqs[peak_idx])
    y0, y1, y2 = psd[peak_idx - 1], psd[peak_idx], psd[peak_idx + 1]
    denom = y0 - 2 * y1 + y2
    if abs(denom) > 1e-9:
        delta = 0.5 * (y0 - y2) / denom
        delta = float(np.clip(delta, -0.5, 0.5))
    else:
        delta = 0.0
    df = freqs[1] - freqs[0]
    return float(freqs[peak_idx] + delta * df)

def _welch_psd(signal: np.ndarray, fs: float, nperseg: int = 256):
    if _SCIPY:
        nperseg = max(4, min(nperseg, len(signal) // 2))
        f, p = scipy_welch(signal, fs=fs, nperseg=nperseg,
                           window='hann', average='median')
        return f, p
    n     = len(signal)
    win   = np.hanning(n)
    win_power = np.sum(win ** 2)
    spec  = np.abs(np.fft.rfft(signal * win)) ** 2 / (fs * win_power)
    spec[1:-1] *= 2.0
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    return freqs, spec


# ═══════════════════════════════════════════════════════════
#  VitalSignDetector – downsampled, amplitude‑first
# ═══════════════════════════════════════════════════════════

class VitalSignDetector:
    BREATH_BAND = (0.08, 0.60)
    HEART_BAND  = (0.80, 2.00)
    HARMONIC_TOL = 0.12
    VITAL_FS = 20.0

    def __init__(self, sample_rate: float = 100.0, window_sec: float = 30.0,
                 min_occupied_sec: float = 10.0, n_subcarriers: int = 64,
                 update_every_sec: float = 1.0):
        self.fs               = sample_rate
        self.window           = int(window_sec * sample_rate)
        self.min_occ_sec      = min_occupied_sec
        self.n_sc             = n_subcarriers
        self._update_frames   = max(1, int(update_every_sec * sample_rate))
        self._frame_cnt       = 0
        self._occupied_since  = None
        self._phase_buf = collections.deque(maxlen=self.window)
        self._amp_buf   = collections.deque(maxlen=self.window)
        self._lock = threading.RLock()
        self._computing = threading.Lock()
        self.breathing_bpm   = -1.0
        self.heart_bpm       = -1.0
        self.breathing_conf  = 0.0
        self.heart_conf      = 0.0
        self._breath_signal  = np.zeros(200).tolist()
        self._heart_signal   = np.zeros(200).tolist()
        self._last_valid_breath_bpm = None
        self._smoothed_breath_bpm = None

    def notify_occupancy(self, occupied: bool):
        with self._lock:
            if occupied and self._occupied_since is None:
                self._occupied_since = time.time()
            elif not occupied:
                self._occupied_since = None
                self.breathing_bpm  = -1.0
                self.heart_bpm      = -1.0
                self.breathing_conf = 0.0
                self.heart_conf     = 0.0
                self._last_valid_breath_bpm = None
                self._smoothed_breath_bpm   = None
                self._amp_buf.clear()
                self._phase_buf.clear()

    def add_sample(self, amps: list, phase: list):
        should_compute = False
        with self._lock:
            if amps and len(amps) >= self.n_sc:
                self._amp_buf.append(np.array(amps[:self.n_sc], dtype=np.float32))
            if phase and len(phase) >= self.n_sc:
                ph = self._sanitize_phase(phase)
                self._phase_buf.append(ph)
            self._frame_cnt += 1
            if self._frame_cnt % self._update_frames == 0:
                should_compute = True
        if should_compute:
            self._compute()

    def _sanitize_phase(self, raw_phase: list) -> np.ndarray:
        ph = np.unwrap(np.array(raw_phase[:self.n_sc], dtype=np.float32))
        ref_idx = self.n_sc // 2
        ph -= ph[ref_idx]
        x = np.linspace(-1.0, 1.0, self.n_sc, dtype=np.float32)
        coeffs = np.polyfit(x, ph, 1)
        ph -= np.polyval(coeffs, x).astype(np.float32)
        return ph

    def _decimate(self, signal: np.ndarray, from_fs: float, to_fs: float):
        if not _SCIPY:
            return signal, from_fs
        factor = int(round(from_fs / to_fs))
        if factor <= 1:
            return signal, from_fs
        try:
            out = scipy_decimate(signal, factor, ftype='fir', zero_phase=True)
            return out, from_fs / factor
        except Exception:
            return signal, from_fs

    def _bandpass(self, signal, low, high, fs=None):
        _fs = fs if fs is not None else self.fs
        if not _SCIPY or len(signal) < 30:
            return signal
        nyq = _fs / 2.0
        if low >= nyq or high >= nyq:
            return signal
        sos = butter(4, [low / nyq, high / nyq], btype='band', output='sos')
        return sosfiltfilt(sos, signal)

    def _band_peak(self, signal, low, high, *, fs=None, check_harmonic=False):
        _fs = fs if fs is not None else self.fs
        freqs, psd = _welch_psd(signal, _fs)
        mask = (freqs >= low) & (freqs <= high)
        if not mask.any():
            return 0.0, 0.0
        band_f, band_psd = freqs[mask], psd[mask]
        if len(band_psd) <= 1:
            return 0.0, 0.0
        if _SCIPY:
            peaks, _ = find_peaks(band_psd, height=np.mean(band_psd),
                                  distance=max(1, len(band_psd) // 10))
            peak_local = peaks[np.argmax(band_psd[peaks])] if len(peaks) else int(np.argmax(band_psd))
        else:
            peak_local = int(np.argmax(band_psd))
        peak_freq = _parabolic_interp(band_f, band_psd, peak_local)
        peak_pow  = float(band_psd[peak_local])
        noise_pow = float(np.mean(np.delete(band_psd, peak_local)))
        snr  = peak_pow / max(noise_pow, 1e-30)
        conf = float(np.clip(np.log10(max(snr, 1.0)) / 2.5, 0.0, 1.0))
        if check_harmonic and conf > 0.3:
            h2_lo = peak_freq * (1.0 - self.HARMONIC_TOL)
            h2_hi = peak_freq * (1.0 + self.HARMONIC_TOL)
            h2_mask = (freqs >= h2_lo) & (freqs <= h2_hi)
            if h2_mask.any():
                h2_power = float(np.max(psd[h2_mask]))
                if h2_power < noise_pow * 1.5:
                    conf *= 0.8
            else:
                conf *= 0.9
        return float(peak_freq), conf

    def _autocorr_peak_freq(self, signal: np.ndarray, fs: float, f_low: float, f_high: float):
        n = len(signal)
        if n < int(fs * 10):
            return 0.0, 0.0
        sig   = signal - np.mean(signal)
        acorr = np.correlate(sig, sig, mode='full')[n-1:]
        acorr /= acorr[0] + 1e-30
        min_lag = max(1, int(fs / f_high))
        max_lag = min(n - 1, int(fs / f_low))
        if min_lag >= max_lag:
            return 0.0, 0.0
        window     = acorr[min_lag:max_lag]
        peak_local = int(np.argmax(window))
        peak_lag   = min_lag + peak_local
        peak_val   = float(acorr[peak_lag])
        if 0 < peak_local < len(window) - 1:
            y0, y1, y2 = window[peak_local-1], window[peak_local], window[peak_local+1]
            denom = y0 - 2 * y1 + y2
            if abs(denom) > 1e-9:
                delta = 0.5 * (y0 - y2) / denom
                delta = float(np.clip(delta, -0.5, 0.5))
            else:
                delta = 0.0
            peak_lag = peak_lag + delta
        freq = fs / peak_lag if peak_lag > 0 else 0.0
        conf = float(np.clip(peak_val, 0.0, 1.0))
        return freq, conf

    @staticmethod
    def _cross_validate(fft_f, fft_c, ac_f, ac_c, tol=0.15):
        if fft_f == 0 and ac_f == 0:
            return 0.0, 0.0
        if fft_f == 0:
            return ac_f, min(ac_c, 0.5)
        if ac_f == 0:
            return fft_f, min(fft_c, 0.5)
        agree = abs(fft_f - ac_f) / max(max(fft_f, ac_f), 1e-6) < tol
        if agree:
            return (fft_f + ac_f) / 2.0, min(1.0, (fft_c + ac_c) / 2.0 * 1.3)
        else:
            if fft_c >= ac_c:
                return fft_f, fft_c * 0.5
            return ac_f, ac_c * 0.5

    def _compute(self):
        MIN_SAMPLES = int(self.fs * 20)
        if not self._computing.acquire(blocking=False):
            return
        try:
            with self._lock:
                if self._occupied_since is None:
                    return
                amp_snap = list(self._amp_buf)
                ph_snap  = list(self._phase_buf)
                smooth_bpm_snap = self._smoothed_breath_bpm

            if len(amp_snap) >= MIN_SAMPLES:
                snap = amp_snap
            elif len(ph_snap) >= MIN_SAMPLES:
                snap = ph_snap
            else:
                return
            mat = np.stack(snap, axis=0).astype(np.float64)

            t = np.arange(mat.shape[0], dtype=np.float64)
            for j in range(mat.shape[1]):
                coeffs = np.polyfit(t, mat[:, j], 1)
                mat[:, j] -= np.polyval(coeffs, t)
            col_std = np.std(mat, axis=0)
            col_std[col_std < 1e-9] = 1.0
            mat = (mat - np.mean(mat, axis=0)) / col_std
            try:
                U, S, Vt = np.linalg.svd(mat, full_matrices=False)
                pc1 = U[:, 0] * S[0]
                trimmed = pc1[int(len(pc1)*0.1):int(len(pc1)*0.9)]
                sign = np.sign(np.mean(trimmed))
                if sign == 0:
                    sign = 1.0
                pc1 *= sign
            except np.linalg.LinAlgError:
                pc1 = mat[:, 0]
            pc1 -= np.polyval(np.polyfit(np.arange(len(pc1)), pc1, 1), np.arange(len(pc1)))
            pc1 = _hampel_vec(pc1, half_k=int(self.fs * 0.5), n_sigma=3.5)

            pc1_ds, fs_ds = self._decimate(pc1, self.fs, self.VITAL_FS)

            b_sig = self._bandpass(pc1_ds, *self.BREATH_BAND, fs=fs_ds)
            b_freq, b_conf = self._band_peak(b_sig, *self.BREATH_BAND, fs=fs_ds)
            b_acorr_freq, b_acorr_conf = self._autocorr_peak_freq(b_sig, fs_ds, *self.BREATH_BAND)
            b_freq, b_conf = self._cross_validate(b_freq, b_conf, b_acorr_freq, b_acorr_conf)
            b_bpm = b_freq * 60.0

            if smooth_bpm_snap is not None and b_bpm > 0:
                if abs(b_bpm - smooth_bpm_snap) / smooth_bpm_snap > 0.4:
                    b_conf *= 0.6

            h_sig = self._bandpass(pc1_ds, *self.HEART_BAND, fs=fs_ds)
            if b_freq > 0 and b_conf >= 0.25:
                for k in range(2, 5):
                    notch_freq = b_freq * k
                    if self.HEART_BAND[0] <= notch_freq <= self.HEART_BAND[1]:
                        bw = max(0.04, notch_freq * 0.06)
                        sos_notch = butter(2, [(notch_freq - bw) / (fs_ds/2),
                                               (notch_freq + bw) / (fs_ds/2)],
                                           btype='bandstop', output='sos')
                        h_sig = sosfiltfilt(sos_notch, h_sig)
            h_freq, h_conf = self._band_peak(h_sig, *self.HEART_BAND, fs=fs_ds, check_harmonic=True)
            h_acorr_freq, h_acorr_conf = self._autocorr_peak_freq(h_sig, fs_ds, *self.HEART_BAND)
            h_freq, h_conf = self._cross_validate(h_freq, h_conf, h_acorr_freq, h_acorr_conf)
            h_bpm = h_freq * 60.0

            with self._lock:
                if self._occupied_since is None:
                    return
                alpha = 0.3
                if b_conf >= 0.15 and b_bpm > 0:
                    if self.breathing_bpm < 0:
                        self.breathing_bpm = round(b_bpm, 1)
                    else:
                        self.breathing_bpm = round((1 - alpha) * self.breathing_bpm + alpha * b_bpm, 1)
                    self.breathing_conf = round(b_conf, 3)
                if h_conf >= 0.15 and h_bpm > 0:
                    if self.heart_bpm < 0:
                        self.heart_bpm = round(h_bpm, 1)
                    else:
                        self.heart_bpm = round((1 - alpha) * self.heart_bpm + alpha * h_bpm, 1)
                    self.heart_conf = round(h_conf, 3)
                if b_bpm > 0:
                    if self._smoothed_breath_bpm is None:
                        self._smoothed_breath_bpm = b_bpm
                    else:
                        self._smoothed_breath_bpm = 0.9 * self._smoothed_breath_bpm + 0.1 * b_bpm
                self._breath_signal = b_sig[-200:].tolist() if len(b_sig) >= 200 else b_sig.tolist()
                self._heart_signal  = h_sig[-200:].tolist() if len(h_sig) >= 200 else h_sig.tolist()
                if b_conf >= 0.40 and 8 <= b_bpm <= 30:
                    self._last_valid_breath_bpm = b_bpm
        finally:
            self._computing.release()

    def get_vitals(self):
        with self._lock:
            breath_signal = self._breath_signal
            if isinstance(breath_signal, np.ndarray):
                breath_signal = breath_signal.tolist()
            heart_signal = self._heart_signal
            if isinstance(heart_signal, np.ndarray):
                heart_signal = heart_signal.tolist()
            valid_breath = self.breathing_conf >= 0.40 and 8 <= self.breathing_bpm <= 30
            valid_heart  = self.heart_conf     >= 0.35 and 45 <= self.heart_bpm    <= 120
            return {
                "breathing_bpm":     self.breathing_bpm if valid_breath else None,
                "heart_bpm":         self.heart_bpm if valid_heart else None,
                "breathing_conf":    self.breathing_conf,
                "heart_conf":        self.heart_conf,
                "breath_signal":     breath_signal,
                "heart_signal":      heart_signal,
                "valid_breathing":   valid_breath,
                "valid_heart":       valid_heart,
                "last_valid_breath_bpm": self._last_valid_breath_bpm,
            }


# ═══════════════════════════════════════════════════════════
#  CSIProcessor – Enterprise‑Grade Production Release v4.7.14
# ═══════════════════════════════════════════════════════════

class CSIProcessor:
    def __init__(
        self,
        calibration_sec:        float = 30.0,
        sample_rate_hz:         float = 100.0,
        motion_sensitivity:     float = 2.0,
        ema_alpha:              float = 0.08,
        hysteresis_sec:         float = 6.0,
        min_trigger_frames:     int   = 30,
        amp_outlier_sigma:      float = 4.0,
        sustain_frames:         int   = 10,
        use_phase_veto:         bool  = True,
        phase_coherence_min:    float = 0.25,
        cal_trim_pct:           float = 0.10,
        short_win_frames:       int   = 100,
        mid_win_frames:         int   = 300,
        long_win_frames:        int   = 600,
        win_weights:            tuple = (0.5, 0.3, 0.2),
        still_sensitivity:      float = 0.5,
        motion_alpha:           float = 0.05,
        still_alpha:            float = 0.05,
        correlation_threshold:  float = 0.85,
        mean_shift_threshold:   float = 0.15,
        motion_still_threshold: float = 0.3,
        num_subcarriers:        int   = 64,
        bg_recal_interval_sec:    float = 300.0,
        bg_recal_window_sec:      float = 30.0,
        bg_recal_max_motion:      float = 0.08,
        bg_recal_max_still:       float = 0.05,
        bg_recal_blend_alpha:     float = 0.30,
        still_sustain_frames:     int   = 8,
        still_max_rise_per_frame: float = 0.02,
        still_fast_fall_sec:      float = 0.1,
        use_phase_still:          bool  = True,
        phase_still_weight:       float = 0.4,
        vital_enabled:            bool  = True,
        vital_window_sec:         float = 30.0,
        vital_min_occupied_sec:   float = 10.0,
        vital_update_every_sec:   float = 1.0,
        strong_reset_thr:         float = 0.7,
        weak_hold_thr:            float = 0.4,
        quick_off_threshold_frames: int = 600,
        shadow_boost_thr:           float = 0.5,
        shadow_max_hold_sec:        float = 12.0,
        shadow_decay_per_sec:       float = 0.12,
        body_shadow_floor_initial:  float = 0.50,
        body_shadow_floor_final:    float = 0.80,
        body_shadow_floor_time:     float = 180.0,
        stuck_timeout_sec:        float = 300.0,
        stuck_low_motion_sec:     float = 60.0,
        bg_recal_timeout_sec:     float = 3600.0,
        recal_lockout_after_occupied_sec: float = 300.0,
        vitals_empty_debounce_frames: int = 300,
        cal_max_acceptable_std:   float = 0.5,
        var_std_ref:              float = 0.25,
        phase_veto_motion_threshold: float = 0.1,
        bg_recal_during_occupancy:      bool = True,
        bg_recal_occupancy_min_corr:    float = 0.95,
        bg_recal_occupancy_blend_alpha: float = 0.01,
        motion_sensitivity_alias: float = None,
        still_sens:               float = None,
        sensitivity:              float = None,
        stuck_safety_timeout_sec: float = 7200.0,
        stuck_safety_var_ratio:   float = 0.5,
        fast_vacancy_enabled:      bool  = True,
        fast_vacancy_var_mult:     float = 1.5,
        fast_vacancy_hold_frames:  int   = 300,
        bac_enabled:               bool  = True,
        bac_interval_sec:          float = 600.0,
        bac_blend_alpha:           float = 0.15,
        motion_absence_timeout_sec: float = 120.0,
        # ── v4.7.14: output debounce ────────────────────────────────────────
        # Reported occupancy only flips to empty after this many seconds of
        # sustained internal vacancy, absorbing sub-second flickers entirely.
        report_vacant_delay_sec:   float = 5.0,
        # ── v4.7.14: correlation guard threshold for vacancy checks ─────────
        # fast_vacancy and quick_off require last_corr > this value.
        # A seated person distorts the channel; an empty room scores higher.
        vacancy_corr_guard:        float = 0.93,
    ):
        if motion_sensitivity_alias is not None:
            motion_sensitivity = motion_sensitivity_alias
        if still_sens is not None:
            still_sensitivity = still_sens
        if sensitivity is not None:
            motion_sensitivity = sensitivity

        if not (0 < ema_alpha <= 1.0):
            raise ValueError(f"ema_alpha must be in (0, 1], got {ema_alpha}")
        if not (0 < still_alpha <= 1.0):
            raise ValueError(f"still_alpha must be in (0, 1], got {still_alpha}")
        if num_subcarriers < 4:
            raise ValueError(f"num_subcarriers must be >= 4, got {num_subcarriers}")
        if calibration_sec <= 0:
            raise ValueError(f"calibration_sec must be > 0, got {calibration_sec}")
        if sample_rate_hz <= 0:
            raise ValueError(f"sample_rate_hz must be > 0, got {sample_rate_hz}")
        if abs(sum(win_weights) - 1.0) > 1e-6:
            raise ValueError(f"win_weights must sum to 1.0, got {win_weights} sum={sum(win_weights)}")

        self._lock       = threading.RLock()
        self._cal_needed = int(calibration_sec * sample_rate_hz)
        self._hyst_max   = int(hysteresis_sec * sample_rate_hz)
        self._hysteresis_sec = hysteresis_sec
        self._min_trig   = min_trigger_frames
        self._n_sc       = num_subcarriers
        self._sr         = sample_rate_hz

        self._m_alpha          = motion_alpha
        self._m_sens           = motion_sensitivity
        self._motion_score     = 0.0
        self._var_baseline_mean = 0.0
        self._var_baseline_std  = 1.0
        self._var_baseline_p95  = 1.0
        self._sw   = short_win_frames
        self._mw   = mid_win_frames
        self._lw   = long_win_frames
        self._ww   = win_weights
        self._short_win = collections.deque(maxlen=short_win_frames)
        self._mid_win   = collections.deque(maxlen=mid_win_frames)
        self._long_win  = collections.deque(maxlen=long_win_frames)

        bg_window_frames = int(bg_recal_window_sec * sample_rate_hz)
        self._var_hist   = collections.deque(maxlen=max(600, bg_window_frames))
        self._motion_hist = collections.deque(maxlen=bg_window_frames)
        self._still_hist  = collections.deque(maxlen=bg_window_frames)

        self._var_above_cnt  = 0
        self._sustain_frames = sustain_frames

        self._corr_thr           = correlation_threshold
        self._shift_thr          = mean_shift_threshold
        self._still_score        = 0.0
        self._mean_shift         = 0.0
        self._still_sens         = still_sensitivity
        self._motion_still_thr   = motion_still_threshold * still_sensitivity
        self._s_alpha            = still_alpha

        self._strong_reset_thr = strong_reset_thr * still_sensitivity
        self._weak_hold_thr    = weak_hold_thr * still_sensitivity

        self._calibrating    = True
        self._cal_failed     = False
        self._cal_frame_count = 0
        self._cal_retry_count = 0
        self._cal_max_retries = 3
        self._cal_amps_buf   = []
        self._cal_phase_buf  = []
        self._cal_var_buf    = []
        self._baseline_amps  = None
        self._baseline_phase = None
        self._baseline_mean  = 0.0
        self._cal_trim_pct   = cal_trim_pct
        self._cal_max_std    = cal_max_acceptable_std
        self._var_std_ref    = var_std_ref

        self._occupied      = False
        self._hyst_ctr      = 0
        self._above_thr_cnt = 0

        self._amp_outlier_sigma   = amp_outlier_sigma
        self._phase_coherence_min = phase_coherence_min
        self._use_phase_veto      = use_phase_veto
        self._phase_veto_motion_thr = phase_veto_motion_threshold
        self._frames_rejected     = 0
        self._frames_rejected_phase = 0
        self._last_phase_coh      = 1.0
        self._last_corr           = 1.0
        self._last_phase_corr     = 1.0

        self._bg_window_frames = bg_window_frames
        self._recent_amps_buf  = collections.deque(maxlen=self._bg_window_frames)
        self._recent_phase_buf = collections.deque(maxlen=self._bg_window_frames)
        self._bg_interval      = bg_recal_interval_sec
        self._bg_max_motion    = bg_recal_max_motion
        self._bg_max_still     = bg_recal_max_still
        self._bg_blend_alpha   = bg_recal_blend_alpha
        self._bg_recal_count   = 0
        self._bg_recal_skipped = 0
        self._recal_lockout_sec = recal_lockout_after_occupied_sec
        self._last_vacated_time = None

        self._still_sustain_frames = still_sustain_frames
        self._still_max_rise       = still_max_rise_per_frame
        self._still_raw_above_cnt  = 0

        self._use_phase_still    = use_phase_still
        self._phase_still_weight = phase_still_weight

        self._still_raw_low_cnt      = 0
        self._still_fast_fall_frames = max(1, int(still_fast_fall_sec * sample_rate_hz))
        self._quick_off_counter      = 0
        self._quick_off_threshold_frames = quick_off_threshold_frames

        self._shadow_score         = 0.0
        self._shadow_boost_thr     = shadow_boost_thr
        self._shadow_decay_per_sec = shadow_decay_per_sec
        self._shadow_max_hold_sec  = shadow_max_hold_sec
        self._last_shadow_boost    = None

        self._bs_floor_initial = body_shadow_floor_initial
        self._bs_floor_final   = body_shadow_floor_final
        self._bs_floor_time    = body_shadow_floor_time

        self._despike_window = collections.deque(maxlen=50)
        self._despike_threshold = 3.0

        self._lp_alpha = 0.1
        self._lp_filtered = None

        self._var_scale = 1.0

        self._bg_recal_timeout_sec = bg_recal_timeout_sec
        self._last_occupied_time   = None

        self._stuck_timeout_sec    = stuck_timeout_sec
        self._stuck_low_motion_sec = stuck_low_motion_sec
        self._last_motion_above_02 = time.time()

        self._last_frame_time = 0.0

        self._vitals_empty_debounce = vitals_empty_debounce_frames
        self._vitals_empty_streak   = 0
        self._vitals_stable_occ     = False

        self._presence_hist = collections.deque(maxlen=int(sample_rate_hz * 30))
        self._feature_update_counter = 0
        self._feature_update_every   = 10
        self._cached_ent_presence    = 0.0
        self._cached_ac_contrib      = 0.0

        self._vital_enabled = vital_enabled and _SCIPY
        if vital_enabled and not _SCIPY:
            _log.error("Vital signs DISABLED – scipy is required. pip install scipy")

        self._vitals = VitalSignDetector(
            sample_rate=sample_rate_hz,
            window_sec=vital_window_sec,
            min_occupied_sec=vital_min_occupied_sec,
            n_subcarriers=num_subcarriers,
            update_every_sec=vital_update_every_sec,
        ) if self._vital_enabled else None

        self._bg_recal_during_occupancy = bg_recal_during_occupancy
        self._bg_recal_occ_min_corr = bg_recal_occupancy_min_corr
        self._bg_recal_occ_blend = bg_recal_occupancy_blend_alpha
        self._last_occ_adapt_time = 0.0
        self._occ_adapt_interval = 600.0

        self._stuck_safety_timeout_sec = stuck_safety_timeout_sec
        self._stuck_safety_var_ratio   = stuck_safety_var_ratio

        self._fast_vacancy_enabled     = fast_vacancy_enabled
        self._fast_vacancy_var_mult    = fast_vacancy_var_mult
        self._fast_vacancy_hold_frames = fast_vacancy_hold_frames
        self._fast_vacancy_cnt         = 0

        self._bac_enabled          = bac_enabled
        self._bac_interval         = bac_interval_sec
        self._bac_blend_alpha      = bac_blend_alpha
        self._last_bac_time        = 0.0

        # ── Still‑score floor tracking ──────────────────────────────────────
        self._still_floor_ema       = 0.0
        self._still_floor_init      = False
        self._still_floor_alpha     = 0.0005
        self._still_floor_cap       = 0.60
        self._still_norm_scale      = 0.15
        self._still_normalized_last = 0.0

        # ── Motion‑absence vacancy ──────────────────────────────────────────
        self._motion_absence_timeout_sec = motion_absence_timeout_sec
        self._last_motion_above_005 = time.time()

        # ── Baseline clamping ───────────────────────────────────────────────
        self._initial_var_mean = None
        self._initial_var_p95  = None

        # ── Re‑entry sensitivity boost ─────────────────────────────────────
        self._reentry_boost_until = 0.0

        # ── v4.7.14: Output debounce ────────────────────────────────────────
        # _reported_occupied is the debounced value returned to callers.
        # _first_vacant_time tracks when the current vacant streak started.
        # Only after report_vacant_delay_sec of continuous internal vacancy
        # does _reported_occupied flip to False.
        self._report_vacant_delay_sec = report_vacant_delay_sec
        self._first_vacant_time       = None   # wall-clock of vacant streak start
        self._reported_occupied       = False  # debounced output value

        # ── v4.7.14: Correlation guard for vacancy checks ───────────────────
        # fast_vacancy and quick_off require last_corr > this threshold.
        self._vacancy_corr_guard = vacancy_corr_guard

        self._stop_event = threading.Event()
        self._bg_thread = threading.Thread(target=self._bg_recal_loop, daemon=True)
        self._bg_thread.start()

    def close(self, timeout: float = 5.0):
        self._stop_event.set()
        self._bg_thread.join(timeout=timeout)
        if self._vitals is not None:
            self._vitals.notify_occupancy(False)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def calibration_failed(self) -> bool:
        with self._lock:
            return self._cal_failed

    def _sanitize_phase(self, raw_phase: list) -> np.ndarray:
        ph = np.unwrap(np.array(raw_phase[:self._n_sc], dtype=np.float32))
        ref_idx = self._n_sc // 2
        ph -= ph[ref_idx]
        x = np.linspace(-1.0, 1.0, self._n_sc, dtype=np.float32)
        coeffs = np.polyfit(x, ph, 1)
        ph -= np.polyval(coeffs, x).astype(np.float32)
        return ph

    def _validate_and_clean(self, mean_amp: float,
                            amps: Optional[list], phase: Optional[list]) -> Tuple[float, Optional[list], Optional[list]]:
        if not np.isfinite(mean_amp):
            mean_amp = self._lp_filtered if self._lp_filtered is not None else 0.0
        if amps is not None:
            a = np.asarray(amps, dtype=np.float32)
            if not np.all(np.isfinite(a)):
                a = np.where(np.isfinite(a), a, np.nanmedian(a) if np.any(np.isfinite(a)) else 0.0)
                amps = a.tolist()
        if phase is not None:
            p = np.asarray(phase, dtype=np.float32)
            if not np.all(np.isfinite(p)):
                p = np.where(np.isfinite(p), p, 0.0)
                phase = p.tolist()
        return mean_amp, amps, phase

    def _despike_mean_amp(self, raw: float) -> float:
        self._despike_window.append(raw)
        if len(self._despike_window) < 20:
            return raw
        arr = np.array(self._despike_window)
        med = float(np.median(arr))
        mad = float(np.median(np.abs(arr - med)))
        if mad < 1e-9:
            return raw
        thr = self._despike_threshold * 1.4826 * mad
        if abs(raw - med) > thr:
            return med
        return raw

    def _low_pass(self, value: float) -> float:
        if self._lp_filtered is None:
            self._lp_filtered = value
        else:
            self._lp_filtered = (self._lp_alpha * value +
                                 (1.0 - self._lp_alpha) * self._lp_filtered)
        return self._lp_filtered

    def _is_amplitude_spike(self, mean_amp: float) -> bool:
        if self._occupied:
            return False
        if len(self._short_win) < 20:
            return False
        arr = np.array(self._short_win)
        med = float(np.median(arr))
        mad = float(np.median(np.abs(arr - med)))
        thr = self._amp_outlier_sigma * 1.4826 * max(mad, 1e-9)
        return abs(mean_amp - med) > thr

    @staticmethod
    def _phase_coherence(phases: list) -> float:
        if phases is None or len(phases) < 4:
            return 1.0
        ph = np.unwrap(np.array(phases, dtype=np.float32))
        x = np.linspace(-1.0, 1.0, len(ph), dtype=np.float32)
        ph -= np.polyval(np.polyfit(x, ph, 1), x).astype(np.float32)
        std_diff = float(np.std(np.diff(ph)))
        return max(0.0, 1.0 - std_diff / (np.pi / np.sqrt(3)))

    def _phase_corr(self, phases: list) -> float:
        if self._baseline_phase is None or phases is None or len(phases) < self._n_sc:
            return 1.0
        cur_ph = self._sanitize_phase(phases)
        base_ph = self._baseline_phase
        if np.std(cur_ph) < 1e-6 or np.std(base_ph) < 1e-6:
            return 1.0
        corr = np.corrcoef(base_ph, cur_ph)[0, 1]
        return float(corr) if not np.isnan(corr) else 1.0

    @staticmethod
    def _phase_diff_var(phases: list) -> float:
        if phases is None or len(phases) < 4:
            return 0.0
        ph = np.unwrap(np.array(phases, dtype=np.float32))
        diff = np.diff(ph)
        return float(np.var(diff))

    def _fused_variance(self) -> float:
        wins = [self._short_win, self._mid_win, self._long_win]
        vars_ = []
        ws = []
        for win, w in zip(wins, self._ww):
            if len(win) >= 20:
                vars_.append(float(np.var(np.array(win))))
                ws.append(w)
        if not vars_:
            return 0.0
        total_w = sum(ws)
        return sum(v * w for v, w in zip(vars_, ws)) / total_w

    def _spectral_entropy(self, signal: np.ndarray, fs: float,
                          f_low: float = 0.05, f_high: float = 5.0) -> float:
        if len(signal) < 64:
            return 0.0
        freqs, psd = _welch_psd(signal, fs, nperseg=min(256, len(signal)//2))
        mask = (freqs >= f_low) & (freqs <= f_high)
        if not mask.any():
            return 0.0
        band = psd[mask]
        total = band.sum()
        if total < 1e-30:
            return 0.0
        p = band / total
        ent = -np.sum(p * np.log2(p + 1e-30))
        return float(ent / np.log2(len(p)))

    def _autocorr_presence(self, signal: np.ndarray, fs: float,
                           min_period_sec: float = 2.0, max_period_sec: float = 8.0) -> float:
        n = len(signal)
        if n < int(fs * max_period_sec * 2):
            return 0.0
        sig = signal - np.mean(signal)
        if np.std(sig) < 1e-9:
            return 0.0
        acorr = np.correlate(sig, sig, mode='full')[n-1:]
        acorr /= acorr[0] + 1e-30
        min_lag = max(1, int(fs * min_period_sec))
        max_lag = min(n - 1, int(fs * max_period_sec))
        if min_lag >= max_lag:
            return 0.0
        window = acorr[min_lag:max_lag]
        peak = float(np.max(window))
        return float(np.clip(peak, 0.0, 1.0))

    def _bg_recal_loop(self):
        while not self._stop_event.wait(self._bg_interval):
            try:
                with self._lock:
                    now = time.time()
                    force_timeout = (
                        self._occupied
                        and self._last_occupied_time is not None
                        and (now - self._last_occupied_time) > self._bg_recal_timeout_sec
                        and (now - self._last_motion_above_02) > self._stuck_low_motion_sec
                        and self._still_score < 0.15
                        and self._shadow_score < 0.1
                    )
                    do_occ_adapt = False
                    if (self._bg_recal_during_occupancy and self._occupied and
                        (now - self._last_occ_adapt_time) > self._occ_adapt_interval and
                        self._motion_score < 0.05 and self._shadow_score < 0.1 and
                        self._last_corr > self._bg_recal_occ_min_corr):
                        do_occ_adapt = True

                    # ── BAC LOGIC (v4.7.13) ─────────────────────────────────
                    # Triggers only when room is unambiguously empty.
                    do_bac = False
                    if self._bac_enabled and not self._occupied:
                        if ((now - self._last_bac_time) > self._bac_interval
                            and self._motion_score < 0.02
                            and self._still_score < 0.10
                            and self._last_corr > 0.97
                            and self._mean_shift < 0.03):
                            if len(self._var_hist) >= 100:
                                recent_var = list(self._var_hist)[-100:]
                                cv = np.std(recent_var) / max(np.mean(recent_var), 1e-6)
                                if cv < 0.3:
                                    do_bac = True
                            else:
                                do_bac = True

                    if not force_timeout and not do_occ_adapt and not do_bac:
                        if self._occupied or self._shadow_score > self._weak_hold_thr:
                            self._bg_recal_skipped += 1
                            continue
                    elif force_timeout:
                        _log.info("Forced recal: occupied > 1 h with no motion, still=%.3f shadow=%.3f",
                                  self._still_score, self._shadow_score)
                    elif do_bac:
                        _log.info("BAC: restoring empty baseline (still=%.3f motion=%.3f corr=%.3f)",
                                  self._still_score, self._motion_score, self._last_corr)

                    if self._calibrating:
                        continue

                    if self._last_vacated_time is not None and not force_timeout and not do_bac:
                        if (now - self._last_vacated_time) < self._recal_lockout_sec:
                            self._bg_recal_skipped += 1
                            continue

                    n_motion = len(self._motion_hist)
                    n_still  = len(self._still_hist)
                    n_var    = len(self._var_hist)
                    if n_motion < 50 or n_still < 50 or n_var < 50:
                        self._bg_recal_skipped += 1
                        continue

                    window = min(self._bg_window_frames, n_motion, n_still, n_var)
                    recent_motion = list(self._motion_hist)[-window:]
                    recent_still  = list(self._still_hist)[-window:]
                    recent_var    = list(self._var_hist)[-window:]
                    recent_amps   = list(self._recent_amps_buf) if len(self._recent_amps_buf) >= window else None
                    recent_phase  = list(self._recent_phase_buf) if len(self._recent_phase_buf) >= window else None
                    baseline_amps  = self._baseline_amps.copy() if self._baseline_amps is not None else None
                    baseline_phase = self._baseline_phase.copy() if self._baseline_phase is not None else None
                    var_baseline_mean_snap = self._var_baseline_mean
                    var_baseline_std_snap  = self._var_baseline_std
                    var_baseline_p95_snap  = self._var_baseline_p95

                if not recent_motion or not recent_still or not recent_var:
                    with self._lock:
                        self._bg_recal_skipped += 1
                    continue

                p95_motion = float(np.percentile(recent_motion, 95))
                p95_still  = float(np.percentile(recent_still, 95))
                mean_var   = float(np.median(recent_var))
                var_std    = float(np.std(recent_var))
                stability  = var_std / max(mean_var, 1e-9)

                if not force_timeout and not do_occ_adapt and not do_bac:
                    if p95_motion > self._bg_max_motion or p95_still > self._bg_max_still or stability > 0.5:
                        with self._lock:
                            self._bg_recal_skipped += 1
                        continue
                elif force_timeout or do_occ_adapt or do_bac:
                    if p95_motion > self._bg_max_motion or p95_still > self._bg_max_still:
                        with self._lock:
                            self._bg_recal_skipped += 1
                        continue

                new_amp_mean = None
                updated_amps = baseline_amps
                if baseline_amps is not None and recent_amps and len(recent_amps) >= window:
                    new_bl = np.median(np.stack(recent_amps, axis=0), axis=0)
                    if do_bac:
                        a = self._bac_blend_alpha
                    else:
                        a = self._bg_blend_alpha if (force_timeout or not do_occ_adapt) else self._bg_recal_occ_blend
                    updated_amps = (1 - a) * baseline_amps + a * new_bl
                    new_amp_mean = float(np.mean(updated_amps))

                updated_phase = baseline_phase
                if (self._use_phase_still and baseline_phase is not None
                        and recent_phase and len(recent_phase) >= window):
                    new_ph = np.median(np.stack(recent_phase, axis=0), axis=0)
                    if do_bac:
                        a = self._bac_blend_alpha
                    else:
                        a = self._bg_blend_alpha if (force_timeout or not do_occ_adapt) else self._bg_recal_occ_blend
                    updated_phase = ((1 - a) * baseline_phase + a * new_ph).astype(np.float32)

                new_var_mean = float(np.median(recent_var))
                new_var_std  = float(np.std(recent_var)) + 1e-4
                new_var_p95  = float(np.percentile(recent_var, 95))
                if do_bac:
                    a_var = self._bac_blend_alpha
                else:
                    a_var = self._bg_blend_alpha if (force_timeout or not do_occ_adapt) else self._bg_recal_occ_blend
                updated_var_mean = (1 - a_var) * var_baseline_mean_snap + a_var * new_var_mean
                updated_var_std  = (1 - a_var) * var_baseline_std_snap  + a_var * new_var_std
                updated_var_p95  = (1 - a_var) * var_baseline_p95_snap  + a_var * new_var_p95

                # ── Clamp baseline to prevent drift (v4.7.13) ─────────────
                if self._initial_var_mean is not None:
                    updated_var_mean = min(updated_var_mean, 2.0 * self._initial_var_mean)
                    updated_var_p95  = min(updated_var_p95,  2.0 * self._initial_var_p95)
                updated_var_scale = max(1.0, updated_var_std / self._var_std_ref) if updated_var_std > self._var_std_ref else 1.0

                with self._lock:
                    if self._calibrating:
                        self._bg_recal_skipped += 1
                        continue

                    now = time.time()
                    force_timeout_now = (
                        self._occupied
                        and self._last_occupied_time is not None
                        and (now - self._last_occupied_time) > self._bg_recal_timeout_sec
                        and (now - self._last_motion_above_02) > self._stuck_low_motion_sec
                        and self._still_score < 0.15
                        and self._shadow_score < 0.1
                    )
                    if not force_timeout_now and not do_occ_adapt and not do_bac and (self._occupied or self._shadow_score > self._weak_hold_thr):
                        self._bg_recal_skipped += 1
                        continue

                    if updated_amps is not None:
                        self._baseline_amps = updated_amps
                        self._baseline_mean = new_amp_mean if new_amp_mean is not None else self._baseline_mean
                    if updated_phase is not None:
                        self._baseline_phase = updated_phase
                    self._var_baseline_mean = updated_var_mean
                    self._var_baseline_std  = updated_var_std
                    self._var_baseline_p95  = updated_var_p95
                    self._var_scale         = updated_var_scale
                    self._bg_recal_count += 1

                    if do_bac:
                        self._last_bac_time = now
                        _log.info("BAC #%d — new amp mean %.4f, still=%.3f, corr=%.3f",
                                  self._bg_recal_count, self._baseline_mean, self._still_score, self._last_corr)
                    elif do_occ_adapt:
                        self._last_occ_adapt_time = now
                        _log.info("OccAdapt #%d — corr=%.3f, amp_mean=%.4f var_mean=%.5f",
                                  self._bg_recal_count, self._last_corr, self._baseline_mean, updated_var_mean)
                    else:
                        _log.info("BG Recal #%d — amp_mean=%.4f var_mean=%.5f",
                                  self._bg_recal_count, self._baseline_mean, updated_var_mean)

            except Exception as e:
                _log.error("BG recal thread unexpected error: %s", e, exc_info=True)

    def add_sample(self, mean_amp: float, amps: list = None, phase: list = None) -> bool:
        with self._lock:
            mean_amp, amps, phase = self._validate_and_clean(mean_amp, amps, phase)
            now = time.time()
            self._last_frame_time = now

            mean_amp = self._despike_mean_amp(mean_amp)
            mean_amp = self._low_pass(mean_amp)

            if self._is_amplitude_spike(mean_amp):
                self._frames_rejected += 1
                return self._reported_occupied

            if (self._use_phase_veto and not self._occupied and
                self._motion_score < self._phase_veto_motion_thr and
                phase and len(phase) >= 4):
                coh = self._phase_coherence(phase)
                self._last_phase_coh = coh
                if coh < self._phase_coherence_min:
                    self._frames_rejected_phase += 1
                    self._frames_rejected += 1
                    fill_val = self._lp_filtered if self._lp_filtered is not None else mean_amp
                    self._short_win.append(fill_val)
                    self._mid_win.append(fill_val)
                    self._long_win.append(fill_val)
                    self._presence_hist.append(fill_val)
                    return self._reported_occupied
            else:
                self._last_phase_coh = 1.0

            self._short_win.append(mean_amp)
            self._mid_win.append(mean_amp)
            self._long_win.append(mean_amp)
            self._presence_hist.append(mean_amp)

            if amps and len(amps) >= self._n_sc:
                self._recent_amps_buf.append(np.array(amps[:self._n_sc], dtype=np.float32))
            if phase and len(phase) >= self._n_sc:
                ph = self._sanitize_phase(phase)
                self._recent_phase_buf.append(ph)

            var = self._fused_variance()
            if len(self._short_win) >= 20:
                self._var_hist.append(var)

            if self._calibrating:
                self._cal_frame_count += 1
                if amps and len(amps) >= self._n_sc:
                    self._cal_amps_buf.append(np.array(amps[:self._n_sc], dtype=np.float32))
                if phase and len(phase) >= self._n_sc:
                    self._cal_phase_buf.append(self._sanitize_phase(phase))
                if len(self._short_win) >= 20:
                    self._cal_var_buf.append(var)
                amps_ready = len(self._cal_amps_buf) >= self._cal_needed
                var_fallback = (
                    self._cal_frame_count >= self._cal_needed * 2
                    and len(self._cal_var_buf) >= 50
                    and not self._cal_amps_buf
                )
                if amps_ready or var_fallback:
                    self._finish_calibration(var_fallback)
                return False

            # ── Re‑entry sensitivity boost (v4.7.13) ────────────────────────
            boost_factor = 1.0
            if now < self._reentry_boost_until:
                boost_factor = 0.5
            motion_thr = self._var_baseline_p95 * self._m_sens * self._var_scale * boost_factor
            above = var > motion_thr
            self._var_above_cnt = (
                min(self._var_above_cnt + 1, self._sustain_frames + 10)
                if above else max(0, self._var_above_cnt - 1))
            z = (max(0.0, (var - self._var_baseline_mean) / max(self._var_baseline_std, 1e-6))
                 if self._var_above_cnt >= self._sustain_frames else 0.0)
            self._motion_score = (1 - self._m_alpha) * self._motion_score + self._m_alpha * z
            self._motion_hist.append(self._motion_score)

            if self._motion_score >= 0.2:
                self._last_motion_above_02 = now
            if self._motion_score >= 0.05:
                self._last_motion_above_005 = now

            corr = 1.0
            mean_shift_ratio = 0.0
            if self._baseline_amps is not None and amps and len(amps) >= self._n_sc:
                cur_amps = np.array(amps[:self._n_sc], dtype=np.float32)
                s_cur, s_base = np.std(cur_amps), np.std(self._baseline_amps)
                if s_cur > 1e-6 and s_base > 1e-6:
                    corr = np.corrcoef(self._baseline_amps, cur_amps)[0, 1]
                    if np.isnan(corr):
                        corr = 1.0 if np.mean(np.abs(cur_amps - self._baseline_amps)) < 1e-3 else 0.0
                self._last_corr = corr
                cur_mean = float(np.mean(cur_amps))
                if self._baseline_mean > 1e-6:
                    mean_shift_ratio = abs(cur_mean - self._baseline_mean) / self._baseline_mean
                self._mean_shift = mean_shift_ratio

                if (not self._occupied and self._motion_score < 0.03 and
                        self._shadow_score < 0.02 and self._baseline_mean > 1e-6 and
                        corr > 0.98):
                    _cur_amp = float(np.mean(np.array(amps[:self._n_sc], dtype=np.float32)))
                    if np.isfinite(_cur_amp) and _cur_amp > 0:
                        self._baseline_mean = (0.9999 * self._baseline_mean +
                                               0.0001 * _cur_amp)

            effective_corr = corr
            phase_diff_var = 0.0
            if self._use_phase_still and phase and len(phase) >= self._n_sc:
                if corr > 0.7:
                    ph_corr = self._phase_corr(phase)
                    self._last_phase_corr = ph_corr
                    effective_corr = ((1 - self._phase_still_weight) * corr
                                      + self._phase_still_weight * ph_corr)
                else:
                    self._last_phase_corr = 1.0
                phase_diff_var = self._phase_diff_var(phase)

            base_presence_corr = max(0.0, (1.0 - effective_corr) / 0.3)
            if self._last_shadow_boost is None:
                body_shadow_floor = self._bs_floor_final
            else:
                sec_since_boost = now - self._last_shadow_boost
                floor_progress  = min(1.0, sec_since_boost / self._bs_floor_time)
                body_shadow_floor = (self._bs_floor_initial * (1.0 - floor_progress) +
                                     self._bs_floor_final * floor_progress)

            if (self._shadow_score < 0.01 and self._motion_score < 0.05
                    and self._last_shadow_boost is not None):
                post_clear_sec = max(0.0, (now - self._last_shadow_boost)
                                     - self._shadow_max_hold_sec)
                if post_clear_sec > 5.0:
                    corr_quality = float(np.clip((corr - 0.88) / 0.07, 0.0, 1.0))
                    pcp = min(1.0, (post_clear_sec - 5.0) / 25.0)
                    body_shadow_floor = max(0.20,
                                           body_shadow_floor * (1.0 - pcp * corr_quality * 0.75))

            motion_factor = max(body_shadow_floor, min(1.0, self._motion_score * 2.0))
            presence_corr = base_presence_corr * motion_factor

            if corr < 0.97:
                presence_mean = min(1.0, mean_shift_ratio / self._shift_thr)
            else:
                presence_mean = min(0.05, mean_shift_ratio / (self._shift_thr * 8.0))

            presence_phase_diff = min(1.0, phase_diff_var / 0.5)
            raw_presence = max(presence_corr, presence_mean, presence_phase_diff)

            needs_feature_update = False
            if self._feature_update_counter + 1 >= self._feature_update_every:
                needs_feature_update = True
                self._feature_update_counter = 0
                feat_var  = list(self._var_hist) if len(self._var_hist) >= 128 else None
                feat_pres = list(self._presence_hist) if len(self._presence_hist) >= int(self._sr * 16) else None
            else:
                self._feature_update_counter += 1
                feat_var  = None
                feat_pres = None

            vitals_stable = self._vitals_stable_occ
            occupied_out  = self._occupied

        new_ent = new_ac = None
        if needs_feature_update and feat_var is not None:
            vh = np.array(feat_var, dtype=np.float64)
            min_ds_len = 64
            stride = max(1, len(vh) // min_ds_len)
            vh_ds = vh[::stride]
            new_ent = np.clip(self._spectral_entropy(vh_ds, self._sr / stride,
                                                      f_low=0.05, f_high=2.0) * 2.0 - 0.4, 0.0, 1.0)
            if feat_pres is not None:
                sig_arr = np.array(feat_pres, dtype=np.float64)
                new_ac = np.clip(self._autocorr_presence(sig_arr, self._sr) - 0.3, 0.0, 0.7)
            else:
                new_ac = 0.0
        elif needs_feature_update:
            new_ent = 0.0
            new_ac  = 0.0

        with self._lock:
            if new_ent is not None:
                self._cached_ent_presence = new_ent
            if new_ac is not None:
                self._cached_ac_contrib   = new_ac

            ent_presence = max(0.0, (1.0 - self._cached_ent_presence) - 0.65)
            raw_presence = max(raw_presence,
                               ent_presence,
                               self._cached_ac_contrib)

            if raw_presence > 0.2:
                self._still_raw_above_cnt = min(self._still_raw_above_cnt + 1,
                                                self._still_sustain_frames + 5)
                self._still_raw_low_cnt = max(0, self._still_raw_low_cnt - 2)
            else:
                self._still_raw_above_cnt = max(0, self._still_raw_above_cnt - 3)
                if raw_presence < 0.2:
                    self._still_raw_low_cnt += 1
                else:
                    self._still_raw_low_cnt = max(0, self._still_raw_low_cnt - 1)

            gated_presence = raw_presence if self._still_raw_above_cnt >= self._still_sustain_frames else 0.0

            new_still = (1 - self._s_alpha) * self._still_score + self._s_alpha * gated_presence
            delta = new_still - self._still_score
            if delta > self._still_max_rise:
                new_still = self._still_score + self._still_max_rise

            if self._still_raw_low_cnt >= self._still_fast_fall_frames:
                new_still = min(new_still, self._still_score * 0.80)

            self._still_score = new_still
            self._still_hist.append(self._still_score)

            # ── Stiffened still floor update (v4.7.13) ──────────────────────
            empty_floor_cond = (not self._occupied and
                                self._motion_score < 0.02 and
                                self._last_corr > 0.98 and
                                self._mean_shift < 0.02)
            if self._initial_var_mean is not None and len(self._var_hist) > 0:
                empty_floor_cond = empty_floor_cond and (self._var_hist[-1] < 1.2 * self._initial_var_mean)
            if empty_floor_cond:
                if not self._still_floor_init:
                    self._still_floor_ema  = self._still_score
                    self._still_floor_init = True
                else:
                    if self._still_score <= self._still_floor_ema + 0.10:
                        self._still_floor_ema = (
                            (1.0 - self._still_floor_alpha) * self._still_floor_ema
                            + self._still_floor_alpha * self._still_score)
                        self._still_floor_ema = min(self._still_floor_ema, self._still_floor_cap)

            _still_normalized = (
                min(1.0, max(0.0, self._still_score - self._still_floor_ema)
                    / self._still_norm_scale)
                if self._still_floor_init else self._still_score)
            self._still_normalized_last = _still_normalized

            if self._motion_score > self._shadow_boost_thr:
                self._shadow_score = 1.0
                self._last_shadow_boost = now
            else:
                if self._last_shadow_boost is None:
                    self._shadow_score = 0.0
                elif (now - self._last_shadow_boost) > self._shadow_max_hold_sec:
                    self._shadow_score = 0.0
                else:
                    dt = now - self._last_shadow_boost
                    self._shadow_score = max(0.0, 1.0 - self._shadow_decay_per_sec * dt)

            effective_occupancy = max(_still_normalized, self._shadow_score)

            old_occupied = self._occupied
            if self._var_above_cnt >= self._sustain_frames:
                self._above_thr_cnt = self._min_trig
                self._hyst_ctr = self._hyst_max
                self._occupied = True
            else:
                self._above_thr_cnt = max(0, self._above_thr_cnt - 2)

            if self._occupied:
                if effective_occupancy > self._strong_reset_thr:
                    self._hyst_ctr = self._hyst_max
                elif effective_occupancy > self._weak_hold_thr or self._motion_score > self._motion_still_thr:
                    pass
                else:
                    if self._hyst_ctr > 0:
                        self._hyst_ctr -= 1
                    else:
                        self._occupied = False
                        self._shadow_score = 0.0

            # ── FAST VACANCY (v4.7.14: correlation guard added) ─────────────
            # Require last_corr > vacancy_corr_guard so a person sitting
            # perfectly still does not trigger a false vacancy — their body
            # still distorts the channel measurably. A truly empty room
            # returns higher correlation to the calibration baseline.
            if self._fast_vacancy_enabled and self._occupied:
                var_empty = self._var_hist and self._var_hist[-1] < self._fast_vacancy_var_mult * self._var_baseline_mean
                if (self._motion_score < 0.1 and
                    _still_normalized < 0.2 and
                    var_empty and
                    self._last_corr > self._vacancy_corr_guard):   # ← v4.7.14 guard
                    self._fast_vacancy_cnt += 1
                else:
                    self._fast_vacancy_cnt = max(0, self._fast_vacancy_cnt - 2)
                if self._fast_vacancy_cnt >= self._fast_vacancy_hold_frames:
                    _log.info("Fast vacancy: room quiet and looks empty "
                              "(motion %.3f, still %.3f, var %.5f, corr %.3f)",
                              self._motion_score, self._still_score,
                              self._var_hist[-1], self._last_corr)
                    self._occupied = False
                    self._hyst_ctr = 0
                    self._above_thr_cnt = 0
                    self._quick_off_counter = 0
                    self._fast_vacancy_cnt = 0

            # ── QUICK OFF (v4.7.14: correlation guard added) ─────────────────
            if self._occupied:
                if (self._shadow_score < 0.05 and
                        self._motion_score < 0.1 and
                        _still_normalized < 0.2 and
                        self._last_corr > self._vacancy_corr_guard):   # ← v4.7.14 guard
                    self._quick_off_counter += 1
                    if self._quick_off_counter >= self._quick_off_threshold_frames:
                        self._occupied = False
                        self._hyst_ctr = 0
                        self._above_thr_cnt = 0
                        self._quick_off_counter = 0
                else:
                    self._quick_off_counter = 0

            # ── MOTION-ABSENCE VACANCY ───────────────────────────────────────
            if self._occupied and self._shadow_score < 0.02:
                _no_motion_sec = now - self._last_motion_above_005
                if (_no_motion_sec >= self._motion_absence_timeout_sec
                        and self._still_score < 0.20
                        and corr > 0.92):
                    _log.info("Motion-absence vacancy: %.0fs no motion "
                              "(shadow=%.3f still=%.3f corr=%.3f)",
                              _no_motion_sec, self._shadow_score,
                              self._still_score, corr)
                    self._occupied = False
                    self._hyst_ctr = 0
                    self._above_thr_cnt = 0
                    self._quick_off_counter = 0
                    self._fast_vacancy_cnt = 0
                    self._shadow_score = 0.0

            if self._hyst_ctr > self._hyst_max:
                self._hyst_ctr = self._hyst_max

            if self._occupied:
                if self._last_occupied_time is None:
                    self._last_occupied_time = now
                self._last_vacated_time = None
            else:
                if self._last_occupied_time is not None:
                    self._last_vacated_time = now
                self._last_occupied_time = None
                # Set re‑entry boost on vacancy (v4.7.13)
                if old_occupied and not self._occupied:
                    self._reentry_boost_until = now + 5.0

            unstuck_triggered = False
            if self._occupied:
                if (now - self._last_occupied_time) > self._stuck_safety_timeout_sec:
                    if (now - self._last_motion_above_02) > self._stuck_safety_timeout_sec:
                        if self._var_hist and self._var_hist[-1] < self._stuck_safety_var_ratio * self._var_baseline_mean:
                            _log.warning("Safety reset: occupied > %ds with no motion and low variance (%.5f < %.5f)",
                                         self._stuck_safety_timeout_sec,
                                         self._var_hist[-1],
                                         self._stuck_safety_var_ratio * self._var_baseline_mean)
                            self._force_recalibration_now()
                            unstuck_triggered = True
                            occupied_out = False

            if not unstuck_triggered and self._occupied:
                empty_looking = (effective_occupancy < 0.15 and
                                 self._motion_score < 0.05)
                if empty_looking and (now - self._last_motion_above_02) > self._stuck_low_motion_sec:
                    if (now - self._last_occupied_time) > self._stuck_timeout_sec:
                        _log.warning("Unstuck: forcing full recalibration – room appears empty "
                                     "(still=%.3f, motion=%.3f)",
                                     effective_occupancy, self._motion_score)
                        self._force_recalibration_now()
                        unstuck_triggered = True
                        occupied_out = False

            if not unstuck_triggered:
                if self._occupied:
                    self._vitals_empty_streak = 0
                    if not self._vitals_stable_occ:
                        self._vitals_stable_occ = True
                else:
                    self._vitals_empty_streak += 1
                    if self._vitals_empty_streak >= self._vitals_empty_debounce:
                        self._vitals_stable_occ = False
                vitals_stable = self._vitals_stable_occ

            if not unstuck_triggered:
                occupied_out = self._occupied

        # ── Handle unstuck path ──────────────────────────────────────────────
        if unstuck_triggered:
            if self._vitals is not None:
                self._vitals.notify_occupancy(False)
            with self._lock:
                self._reported_occupied = False
                self._first_vacant_time = None
            return False

        if self._vitals is not None:
            self._vitals.notify_occupancy(vitals_stable)
            self._vitals.add_sample(amps, phase)

        # ── v4.7.14: Output debounce ─────────────────────────────────────────
        # The raw occupied_out from the state machine can flicker briefly when
        # someone sits perfectly still (scores drop toward empty thresholds).
        # We only report a transition to empty after the room has been
        # internally vacant for report_vacant_delay_sec continuously.
        # Transitions to occupied are reported immediately.
        with self._lock:
            _now = time.time()
            if occupied_out:
                # Occupied: clear vacant streak, report immediately
                self._first_vacant_time = None
                self._reported_occupied = True
            else:
                # Vacant internally: start or continue timing the streak
                if self._first_vacant_time is None:
                    self._first_vacant_time = _now
                elapsed = _now - self._first_vacant_time
                if elapsed >= self._report_vacant_delay_sec:
                    # Sustained vacancy confirmed – report empty
                    self._reported_occupied = False
                # else: streak too short, keep reporting occupied to callers
            return self._reported_occupied

    def _force_recalibration_now(self):
        self.force_recalibrate()

    def _finish_calibration(self, var_fallback: bool = False):
        trimmed_pct = 0.0
        if self._cal_var_buf:
            var_arr = np.array(self._cal_var_buf)
            lo  = np.percentile(var_arr, self._cal_trim_pct * 100)
            hi  = np.percentile(var_arr, (1 - self._cal_trim_pct) * 100)
            clean = var_arr[(var_arr >= lo) & (var_arr <= hi)]
            if len(clean) < 10:
                clean = var_arr
            self._var_baseline_mean = float(np.mean(clean))
            self._var_baseline_std  = max(float(np.std(clean)), 1e-4)
            self._var_baseline_p95  = float(np.percentile(clean, 95))
            trimmed_pct = 100.0 * (1 - len(clean) / len(var_arr))

            self._var_scale = max(1.0, self._var_baseline_std / self._var_std_ref) if self._var_baseline_std > self._var_std_ref else 1.0

        mask = None
        if self._cal_amps_buf:
            all_amps = np.stack(self._cal_amps_buf, axis=0)
            frame_means = np.mean(all_amps, axis=1)
            lo_a = np.percentile(frame_means, self._cal_trim_pct * 100)
            hi_a = np.percentile(frame_means, (1 - self._cal_trim_pct) * 100)
            mask = (frame_means >= lo_a) & (frame_means <= hi_a)
            clean_amps = all_amps[mask] if mask.sum() >= 10 else all_amps
            self._baseline_amps = np.mean(clean_amps, axis=0)
            self._baseline_mean = float(np.mean(self._baseline_amps))

        if self._use_phase_still and self._cal_phase_buf:
            all_ph = np.stack(self._cal_phase_buf, axis=0)
            if mask is not None and len(self._cal_phase_buf) == len(self._cal_amps_buf):
                clean_ph = all_ph[mask] if mask.sum() >= 10 else all_ph
            else:
                clean_ph = all_ph
            self._baseline_phase = np.mean(clean_ph, axis=0).astype(np.float32)

        if self._var_baseline_std > self._cal_max_std:
            self._cal_retry_count += 1
            if self._cal_retry_count >= self._cal_max_retries:
                _log.warning("Calibration quality POOR after %d retries – accepting noisy baseline", self._cal_retry_count)
            else:
                _log.warning("Calibration quality POOR: var_std=%.4f > %.4f. Retrying (%d/%d).",
                             self._var_baseline_std, self._cal_max_std,
                             self._cal_retry_count, self._cal_max_retries)
                self._calibrating = True
                self._cal_amps_buf = []
                self._cal_phase_buf = []
                self._cal_var_buf = []
                self._cal_frame_count = 0
                return

        if self._baseline_amps is None and not var_fallback:
            _log.error("Calibration FAILED: no signal received. Check hardware.")
            self._cal_failed = True
            self._cal_amps_buf = []
            self._cal_phase_buf = []
            self._cal_var_buf = []
            self._cal_frame_count = 0
            return

        # Store initial variance for clamping (v4.7.13)
        self._initial_var_mean = self._var_baseline_mean
        self._initial_var_p95  = self._var_baseline_p95

        self._cal_retry_count = 0
        self._cal_amps_buf = []
        self._cal_phase_buf = []
        self._cal_var_buf = []
        self._calibrating = False
        self._cal_failed   = False

        _log.info("\n" + "=" * 70)
        _log.info("  WaveSense Ultimate – Production Release v4.7.14 (Still-Flicker Fix)")
        _log.info(f"  Frames used        : {self._cal_frame_count}")
        _log.info(f"  Subcarriers        : {self._n_sc}")
        _log.info(f"  Var trimmed        : {trimmed_pct:.1f}% removed as spikes")
        _log.info(f"  Var baseline       : mean={self._var_baseline_mean:.5f}  std={self._var_baseline_std:.5f}  p95={self._var_baseline_p95:.5f}  scale={self._var_scale:.2f}")
        _log.info(f"  Amp baseline mean  : {self._baseline_mean:.5f}")
        _log.info(f"  Phase baseline     : {'stored' if self._baseline_phase is not None else 'N/A'}")
        _log.info(f"  Body‑shadow floor  : {self._bs_floor_initial}→{self._bs_floor_final} over {self._bs_floor_time}s")
        _log.info(f"  Shadow score       : hold {self._shadow_max_hold_sec}s, decay {self._shadow_decay_per_sec}/s")
        _log.info(f"  Hysteresis / Qoff  : {self._hysteresis_sec}s hold / {self._quick_off_threshold_frames}fr ({self._quick_off_threshold_frames/self._sr:.1f}s) quick‑off")
        _log.info(f"  BG recal           : every {self._bg_interval:.0f}s, lockout {self._recal_lockout_sec:.0f}s after occ")
        _log.info(f"  OccAdapt           : {'ON' if self._bg_recal_during_occupancy else 'OFF'} (min_corr={self._bg_recal_occ_min_corr}, blend={self._bg_recal_occ_blend})")
        _log.info(f"  Vital signs        : {'ON (downsampled to 20 Hz, cross‑validated)' if self._vital_enabled else 'OFF'}")
        _log.info(f"  Safety reset       : {self._stuck_safety_timeout_sec}s after stuck occ, var < {self._stuck_safety_var_ratio} × baseline")
        _log.info(f"  Fast vacancy       : {'ON' if self._fast_vacancy_enabled else 'OFF'} (hold {self._fast_vacancy_hold_frames} fr, var < {self._fast_vacancy_var_mult} × baseline, corr > {self._vacancy_corr_guard})")
        _log.info(f"  Quick-off          : corr guard > {self._vacancy_corr_guard}")
        _log.info(f"  Baseline auto‑corr : {'ON' if self._bac_enabled else 'OFF'} every {self._bac_interval}s, blend {self._bac_blend_alpha}")
        _log.info(f"  Still floor norm   : scale={self._still_norm_scale}")
        _log.info(f"  Motion‑absence vac : {self._motion_absence_timeout_sec}s after motion < 0.05")
        _log.info(f"  Re‑entry boost     : 5.0s after vacancy (threshold halved)")
        _log.info(f"  Baseline clamp     : max 2× initial variance")
        _log.info(f"  Output debounce    : {self._report_vacant_delay_sec}s sustained vacancy before reporting empty")
        _log.info("=" * 70 + "\n")

    # ── Public API ──────────────────────────────────────────────────────────

    def is_occupied(self) -> bool:
        """Returns the debounced occupancy state (flicker-free)."""
        with self._lock:
            return self._reported_occupied

    def is_occupied_raw(self) -> bool:
        """Returns the raw internal occupancy state (may flicker briefly)."""
        with self._lock:
            return self._occupied

    def is_calibrating(self) -> bool:
        with self._lock:
            return self._calibrating

    def get_score(self) -> float:
        with self._lock:
            return self._motion_score

    def get_still_score(self) -> float:
        with self._lock:
            return self._still_score

    def get_score_threshold(self) -> float:
        with self._lock:
            return float(self._m_sens)

    def get_still_threshold(self) -> float:
        with self._lock:
            return self._strong_reset_thr

    def get_variance(self) -> float:
        with self._lock:
            return self._var_hist[-1] if self._var_hist else 0.0

    def get_threshold(self) -> float:
        with self._lock:
            return self._var_baseline_p95 * self._m_sens * self._var_scale

    def calibration_progress(self) -> float:
        with self._lock:
            if not self._calibrating:
                return 1.0
            return min(1.0, self._cal_frame_count / max(1, self._cal_needed))

    def get_history(self, n=200) -> list:
        with self._lock:
            return list(self._var_hist)[-n:]

    def get_score_history(self, n=200) -> list:
        with self._lock:
            return list(self._motion_hist)[-n:]

    def get_still_history(self, n=200) -> list:
        with self._lock:
            return list(self._still_hist)[-n:]

    def get_vitals(self) -> dict:
        return self._vitals.get_vitals() if self._vitals else {}

    def get_debug(self) -> dict:
        with self._lock:
            _now = time.time()
            return {
                "motion_score":            round(self._motion_score, 3),
                "still_score":             round(self._still_score, 3),
                "still_normalized":        round(self._still_normalized_last, 3),
                "still_floor_ema":         round(self._still_floor_ema, 3),
                "shadow_score":            round(self._shadow_score, 3),
                "motion_thr":              round(self._m_sens, 2),
                "still_thr":               round(self._strong_reset_thr, 2),
                "weak_hold_thr":           round(self._weak_hold_thr, 2),
                "variance":                round(self._var_hist[-1] if self._var_hist else 0, 5),
                "baseline_mean":           round(self._var_baseline_mean, 5),
                "baseline_std":            round(self._var_baseline_std, 5),
                "var_p95":                 round(self._var_baseline_p95, 5),
                "var_scale":               round(self._var_scale, 2),
                "var_std_ref":             round(self._var_std_ref, 3),
                "var_thr":                 round(self.get_threshold(), 5),
                "last_corr":               round(self._last_corr, 4),
                "last_phase_corr":         round(self._last_phase_corr, 4),
                "mean_shift":              round(self._mean_shift, 4),
                "hyst_pct":                round(self._hyst_ctr / max(1, self._hyst_max), 2),
                "above_thr":               self._above_thr_cnt,
                "var_above_cnt":           self._var_above_cnt,
                "still_raw_above":         self._still_raw_above_cnt,
                "still_raw_low_cnt":       self._still_raw_low_cnt,
                "quick_off_counter":       self._quick_off_counter,
                "frames_rejected":         self._frames_rejected,
                "phase_reject_count":      self._frames_rejected_phase,
                "phase_coherence":         round(self._last_phase_coh, 3),
                "bg_recal_count":          self._bg_recal_count,
                "bg_recal_skipped":        self._bg_recal_skipped,
                "cal_samples":             len(self._cal_amps_buf) if self._cal_amps_buf else 0,
                "cal_needed":              self._cal_needed,
                "strong_reset_thr":        self._strong_reset_thr,
                "weak_hold_thr":           self._weak_hold_thr,
                "vitals_stable_occ":       self._vitals_stable_occ,
                "seconds_since_last_frame": round(_now - self._last_frame_time, 1),
                "motion_absence_sec":       round(_now - self._last_motion_above_005, 1),
                "motion_absence_timeout":   self._motion_absence_timeout_sec,
                "reentry_boost_active":     _now < self._reentry_boost_until,
                # ── v4.7.14 additions ───────────────────────────────────────
                "occupied_raw":             self._occupied,
                "reported_occupied":        self._reported_occupied,
                "vacancy_corr_guard":       self._vacancy_corr_guard,
                "vacant_debounce_sec":      round(_now - self._first_vacant_time, 2)
                                            if self._first_vacant_time is not None else 0.0,
                "report_vacant_delay_sec":  self._report_vacant_delay_sec,
            }

    def force_recalibrate(self):
        with self._lock:
            self._calibrating          = True
            self._cal_failed           = False
            self._cal_amps_buf         = []
            self._cal_phase_buf        = []
            self._cal_var_buf          = []
            self._baseline_amps        = None
            self._baseline_phase       = None
            self._baseline_mean        = 0.0
            self._var_baseline_mean    = 0.0
            self._var_baseline_std     = 1.0
            self._var_baseline_p95     = 1.0
            self._var_scale            = 1.0
            self._motion_score         = 0.0
            self._still_score          = 0.0
            self._shadow_score         = 0.0
            self._occupied             = False
            self._hyst_ctr             = 0
            self._above_thr_cnt        = 0
            self._var_above_cnt        = 0
            self._still_raw_above_cnt  = 0
            self._still_raw_low_cnt    = 0
            self._quick_off_counter    = 0
            self._fast_vacancy_cnt     = 0
            self._frames_rejected      = 0
            self._frames_rejected_phase = 0
            self._bg_recal_count       = 0
            self._bg_recal_skipped     = 0
            self._last_occupied_time   = None
            self._last_vacated_time    = None
            self._vitals_stable_occ    = False
            self._vitals_empty_streak  = 0
            self._cal_frame_count      = 0
            self._cal_retry_count      = 0
            self._lp_filtered          = None
            self._despike_window.clear()
            self._last_frame_time      = 0.0
            self._last_motion_above_02  = time.time()
            self._last_motion_above_005 = time.time()
            self._short_win.clear()
            self._mid_win.clear()
            self._long_win.clear()
            self._motion_hist.clear()
            self._var_hist.clear()
            self._still_hist.clear()
            self._presence_hist.clear()
            self._recent_amps_buf.clear()
            self._recent_phase_buf.clear()
            self._last_shadow_boost    = None
            self._feature_update_counter = 0
            self._cached_ent_presence    = 0.0
            self._cached_ac_contrib      = 0.0
            self._last_bac_time          = 0.0
            self._still_floor_ema       = 0.0
            self._still_floor_init      = False
            self._still_normalized_last = 0.0
            self._initial_var_mean      = None
            self._initial_var_p95       = None
            self._reentry_boost_until   = 0.0
            # ── v4.7.14 resets ──────────────────────────────────────────────
            self._reported_occupied    = False
            self._first_vacant_time    = None
        if self._vitals is not None:
            self._vitals.notify_occupancy(False)
        _log.info("CSIProcessor Recalibrating – keep room EMPTY for 30 s")

    def check_watchdog(self, max_silence_sec: float = 15.0) -> bool:
        with self._lock:
            if self._last_frame_time == 0.0:
                return False
            return (time.time() - self._last_frame_time) > max_silence_sec
