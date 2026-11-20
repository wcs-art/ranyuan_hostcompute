"""LSL 持续时钟校正与统一时间轴。"""
from __future__ import annotations
import time
import numpy as np

ALIGN_MS = 1.0  # 验收门限

def check_alignment(offsets_ms: list[float]) -> tuple[bool, float]:
    """最大两两偏移差≤1ms即通过, 返回(是否通过, 最大差)."""
    if len(offsets_ms) < 2: return True, 0.0
    gap = float(np.max(offsets_ms) - np.min(offsets_ms))
    return gap <= ALIGN_MS, gap

def poll_lsl_corrections(device_ids: list[int]) -> dict[int, float]:
    """真机轮询, 无流时返回空(由调用方跳过判定)."""
    try:
        from pylsl import resolve_streams, StreamInlet
    except Exception: return {}
    out = {}
    for did in device_ids:
        for s in resolve_streams(2.0):
            if f"SleepEEG_{did}" in s.name():
                try: out[did] = StreamInlet(s).time_correction(timeout=2.0)*1000.0
                except Exception: pass
    return out
