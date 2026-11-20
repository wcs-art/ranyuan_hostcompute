"""L5 算法层 · 频带功率与状态指标（自包含，无遗留依赖）。
从 power_cal.py 与 Algorithm/state_metrics.py、metrics_state.py 迁移而来，
目的只有一个：让新界面不再依赖旧目录，从而可以安全删除旧版本。
迁移原则：只搬界面真正在用的部分（Welch 频带功率 + 状态四指标），
不搬 power_cal 里的绘图/Excel/离线批处理（那些属旧版单通道工具）。
依赖：numpy、scipy。不 import Algorithm/、不 import pandas（不需要）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

import numpy as np
from scipy.integrate import trapezoid
from scipy.signal import butter, find_peaks, sosfiltfilt, welch

FILTER_ORDER = 4
DEFAULT_SAMPLE_RATE = 500.0
BANDPASS_LOW_HZ = 0.5
BANDPASS_HIGH_HZ = 40.0

# 0.5–40 Hz 带通范围内的标准节律划分
EEG_BANDS: Dict[str, Tuple[float, float]] = {
    "delta": (0.5, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (8.0, 13.0),
    "beta": (13.0, 30.0),
    "gamma": (30.0, 40.0),
}


@dataclass(frozen=True)
class BandPowerResult:
    absolute: Dict[str, float]
    relative: Dict[str, float]
    total_power: float


@dataclass(frozen=True)
class PowerAnalysis:
    result: BandPowerResult
    freqs: np.ndarray
    psd: np.ndarray


def bandpass_filter(
    signal: np.ndarray,
    sample_rate: float,
    low_hz: float = BANDPASS_LOW_HZ,
    high_hz: float = BANDPASS_HIGH_HZ,
    order: int = FILTER_ORDER,
) -> np.ndarray:
    """零相位 Butterworth 带通。"""
    if low_hz <= 0:
        raise ValueError(f"低截止频率须 > 0，当前为 {low_hz}")
    if sample_rate <= 2 * high_hz:
        raise ValueError(
            f"采样率 {sample_rate} Hz 过低，无法设计 {high_hz} Hz 高通上限"
        )
    nyquist = sample_rate * 0.5
    sos = butter(
        order,
        (low_hz / nyquist, high_hz / nyquist),
        btype="bandpass",
        output="sos",
    )
    return sosfiltfilt(sos, signal, axis=0)


def _band_power_from_psd(
    freqs: np.ndarray, psd: np.ndarray, low_hz: float, high_hz: float
) -> float:
    mask = (freqs >= low_hz) & (freqs <= high_hz)
    if not np.any(mask):
        return 0.0
    return float(trapezoid(psd[mask], freqs[mask]))


def trim_filter_edges(
    filtered: np.ndarray,
    sample_rate: float,
    edge_trim_sec: float,
    *,
    min_keep_sec: float = 1.0,
) -> np.ndarray:
    """丢掉 filtfilt 两端暂态区；若去边后过短则原样返回。"""
    arr = np.asarray(filtered, dtype=np.float64).reshape(-1)
    trim = float(edge_trim_sec)
    fs = float(sample_rate)
    if not np.isfinite(trim) or trim <= 0 or not np.isfinite(fs) or fs <= 0:
        return arr
    n_trim = int(round(trim * fs))
    if n_trim <= 0:
        return arr
    min_keep = max(8, int(round(max(0.0, float(min_keep_sec)) * fs)))
    if arr.size - 2 * n_trim < min_keep:
        return arr
    return arr[n_trim : arr.size - n_trim]


def compute_band_powers_with_metrics(
    signal: np.ndarray,
    sample_rate: float = DEFAULT_SAMPLE_RATE,
    welch_seconds: float = 2.0,
    *,
    edge_trim_sec: float = 0.0,
) -> tuple[PowerAnalysis, float, float]:
    """Welch PSD 积分得各频段功率，附带去边后波形的 RMS 与峰峰值。"""
    filtered = bandpass_filter(signal, sample_rate)
    filtered = trim_filter_edges(
        filtered,
        sample_rate,
        edge_trim_sec,
        min_keep_sec=max(0.5, float(welch_seconds)),
    )
    if filtered.size == 0:
        rms = 0.0
        ptp = 0.0
    else:
        rms = float(np.sqrt(np.mean(np.square(filtered))))
        ptp = float(np.ptp(filtered))
    nperseg = min(len(filtered), max(int(sample_rate * welch_seconds), 256))
    if filtered.size < 8:
        empty_f = np.zeros(0, dtype=np.float64)
        absolute = {name: 0.0 for name in EEG_BANDS}
        relative = {name: 0.0 for name in EEG_BANDS}
        result = BandPowerResult(absolute=absolute, relative=relative, total_power=0.0)
        return PowerAnalysis(result=result, freqs=empty_f, psd=empty_f), rms, ptp

    freqs, psd = welch(filtered, fs=sample_rate, nperseg=nperseg)

    absolute: Dict[str, float] = {}
    for name, (low, high) in EEG_BANDS.items():
        absolute[name] = _band_power_from_psd(freqs, psd, low, high)

    total = sum(absolute.values())
    if total <= 0.0:
        relative = {name: 0.0 for name in EEG_BANDS}
    else:
        relative = {name: absolute[name] / total for name in EEG_BANDS}

    result = BandPowerResult(absolute=absolute, relative=relative, total_power=total)
    return PowerAnalysis(result=result, freqs=freqs, psd=psd), rms, ptp


def compute_band_powers(
    signal: np.ndarray,
    sample_rate: float = DEFAULT_SAMPLE_RATE,
    welch_seconds: float = 2.0,
    *,
    edge_trim_sec: float = 0.0,
) -> PowerAnalysis:
    """对滤波后信号做 Welch PSD，积分得到各频段绝对/相对功率。"""
    analysis, _rms, _ptp = compute_band_powers_with_metrics(
        signal,
        sample_rate,
        welch_seconds,
        edge_trim_sec=edge_trim_sec,
    )
    return analysis


# ---------------------------------------------------------------- 状态指标（S9）


def brain_relax_0_100(eeg_seg: np.ndarray, fs: float = 500.0) -> float:
    """脑放松指数：alpha 相对功率 × 100。"""
    a = compute_band_powers(eeg_seg, fs).result
    return float(a.relative.get("alpha", 0.0) * 100)


def focus_0_100(eeg_seg: np.ndarray, fs: float = 500.0) -> float:
    """专注指数：1 - θ/(θ+β)。"""
    a = compute_band_powers(eeg_seg, fs).result
    t, b = a.absolute.get("theta", 0), a.absolute.get("beta", 1e-9)
    return float(np.clip(100 * (1 - t / (t + b)), 0, 100))


def hr_from_ppg(ppg: np.ndarray, fs: float = 10.0) -> float:
    """从 fNIRS 光强做的粗略心率（0.8–2.5 Hz 带通后数峰）。"""
    sos = butter(4, (0.8 / (fs / 2), 2.5 / (fs / 2)), btype="bandpass", output="sos")
    f = sosfiltfilt(sos, ppg)
    pk, _ = find_peaks(f, distance=int(fs * 0.4))
    if len(pk) < 2:
        return 0.0
    return float(60.0 / (np.diff(pk).mean() / fs))


class StateMetrics:
    """滑动窗计算 4 个状态指标（0–100，≥1 Hz）。

    基线自校准：采满 60 s 后用前段均值/方差做 z-score，再 sigmoid 到 0–100。
    迁移说明：原实现 fs_fnirs 默认 10.0，而合同要求 fNIRS ≥100 Hz，
    这里默认改为 100.0；调用方 ui/monitor_14ch.py 未显式传参，随之生效。
    """

    def __init__(self, fs_eeg: float = 500.0, fs_fnirs: float = 100.0,
                 win_sec: float = 10.0) -> None:
        self.fs_eeg = fs_eeg
        self.fs_fnirs = fs_fnirs
        self.win = win_sec
        self.eeg_buf: list[float] = []
        self.hbo_buf: list[float] = []
        self.ppg_buf: list[float] = []
        self.base: dict[str, list[float]] = {"active": [], "relax": [], "focus": []}
        self.calibrated = False

    def push(self, eeg_ch1: float, hbo: float, hbr: float, ppg: float) -> None:
        self.eeg_buf.append(eeg_ch1)
        self.hbo_buf.append(hbo)
        self.ppg_buf.append(ppg)
        n_e = int(self.fs_eeg * self.win)
        self.eeg_buf = self.eeg_buf[-n_e:]
        n_f = int(self.fs_fnirs * self.win)
        self.hbo_buf = self.hbo_buf[-n_f:]
        self.ppg_buf = self.ppg_buf[-n_f:]

    def _norm(self, key: str, v: float) -> float:
        b = np.array(self.base[key]) if self.base[key] else np.array([v])
        m, s = float(b.mean()), float(b.std() + 1e-9)
        z = (v - m) / (s + 1e-9)
        return float(100 / (1 + np.exp(-z)))

    def compute(self) -> dict:
        out = {"brain_active": 50.0, "brain_relax": 50.0, "focus": 50.0, "hr_bpm": 0.0}
        if len(self.eeg_buf) >= int(self.fs_eeg * 2):
            a = compute_band_powers(np.asarray(self.eeg_buf), self.fs_eeg).result
            relax_raw = a.relative.get("alpha", 0) * 100
            th, be = a.absolute.get("theta", 0), a.absolute.get("beta", 0)
            focus_raw = 100 * th / (be + 1e-9) / 5
            focus_raw = min(100, max(0, focus_raw))
            if not self.calibrated:
                self.base["relax"].append(relax_raw)
                self.base["focus"].append(focus_raw)
            out["brain_relax"] = self._norm("relax", relax_raw) if self.calibrated else relax_raw
            out["focus"] = self._norm("focus", focus_raw) if self.calibrated else focus_raw
        if len(self.hbo_buf) >= 10:
            act_raw = float(np.mean(np.abs(np.diff(self.hbo_buf)))) * 1000
            if not self.calibrated:
                self.base["active"].append(act_raw)
            out["brain_active"] = self._norm("active", act_raw) if self.calibrated else min(100, act_raw)
        if len(self.ppg_buf) >= int(self.fs_fnirs * 5):
            x = np.asarray(self.ppg_buf, float)
            sos = butter(
                4,
                (0.8 / (self.fs_fnirs / 2), 2.5 / (self.fs_fnirs / 2)),
                btype="bandpass",
                output="sos",
            )
            f = sosfiltfilt(sos, x)
            pk, _ = find_peaks(f, distance=int(self.fs_fnirs * 0.4))
            if len(pk) >= 2:
                ibi = np.diff(pk).mean() / self.fs_fnirs
                out["hr_bpm"] = 60.0 / max(ibi, 1e-3)
        if not self.calibrated and len(self.base["relax"]) * self.win > 60:
            self.calibrated = True
        return out
