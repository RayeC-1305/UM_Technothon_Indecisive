"""
Original CSIProcessor configuration for app.py
===============================================
Save this file to restore the original settings after testing.
"""

CSI_CONFIG = {
    "calibration_sec":           30.0,
    "sample_rate_hz":            100.0,
    "sensitivity":               2.0,
    "ema_alpha":                 0.08,
    "hysteresis_sec":            8.0,
    "min_trigger_frames":        30,
    "amp_outlier_sigma":         4.0,
    "sustain_frames":            10,
    "use_phase_veto":            True,
    "phase_coherence_min":       0.25,
    "cal_trim_pct":              0.10,
    "short_win_frames":          100,
    "mid_win_frames":            300,
    "long_win_frames":           600,
    "win_weights":               (0.50, 0.30, 0.20),
    "bg_recal_interval_sec":     120.0,
    "bg_recal_window_sec":       30.0,
    "bg_recal_max_motion":       0.5,
    "bg_recal_max_still":        0.25,
    "bg_recal_blend_alpha":      0.30,
    "still_sustain_frames":      15,
    "still_max_rise_per_frame":  0.02,
    "use_phase_still":           True,
    "phase_still_weight":        0.4,
    "vital_enabled":             True,
    "vital_window_sec":          30.0,
    "vital_min_occupied_sec":    10.0,
    "vital_update_every_sec":    1.0,
    "strong_reset_thr":          0.7,
    "weak_hold_thr":             0.5,
    "quick_off_threshold_frames": 30,
    "bg_recal_timeout_sec":      3600.0,
}
