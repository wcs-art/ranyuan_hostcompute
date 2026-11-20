"""fNIRS 原始码值 → 光强/OD → ΔHbO/ΔHbR 解算。"""
from __future__ import annotations
from typing import Optional, Union

import numpy as np

from derive import FnirsOpticalParams
from derive import HbMetrics
from derive import hb_metrics as _hb_metrics
from derive import mbll as _mbll
from derive import od_delta as _od_delta
from derive import raw_to_lux as _raw_to_lux


def raw_to_lux(raw: np.ndarray, params: Optional[FnirsOpticalParams] = None) -> np.ndarray:
    return _raw_to_lux(raw, params)


def od_delta(
    lux: np.ndarray,
    baseline: np.ndarray,
    params: Optional[FnirsOpticalParams] = None,
) -> np.ndarray:
    return _od_delta(lux, baseline, params)


def mbll(
    od760: float, od850: float, params: Optional[FnirsOpticalParams] = None
) -> tuple[float, float]:
    """单通道双波长 -> (dHbO, dHbR) mM。矩阵奇异时 (nan, nan), 不抛异常。"""
    return _mbll(od760, od850, params)


def hb_metrics(hbo_mM, hbr_mM,
               params: Optional[FnirsOpticalParams] = None) -> HbMetrics:
    """dHbT 与变化量氧合占比, 转发到 derive 的唯一实现。"""
    return _hb_metrics(hbo_mM, hbr_mM, params)
