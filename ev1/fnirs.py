"""fNIRS: 原始码值->光强/OD->dHbO/dHbR (Beer-Lambert). 对照要求§二§三.

B2 起实现迁至 processing/derive.py, 本模块只保留旧签名的转发(既有 import 不断)。
系数表唯一出处是 derive.FnirsOpticalParams —— 本模块不定义任何常量。
DPF/消光系数须写入协议文档经甲方确认。
"""
from __future__ import annotations

import numpy as np

from derive import FnirsOpticalParams
from derive import HbMetrics
from derive import hb_metrics as _hb_metrics
from derive import mbll as _mbll
from derive import od_delta as _od_delta
from derive import raw_to_lux as _raw_to_lux


def raw_to_lux(raw: np.ndarray, params: FnirsOpticalParams | None = None) -> np.ndarray:
    return _raw_to_lux(raw, params)


def od_delta(
    lux: np.ndarray,
    baseline: np.ndarray,
    params: FnirsOpticalParams | None = None,
) -> np.ndarray:
    return _od_delta(lux, baseline, params)


def mbll(
    od760: float, od850: float, params: FnirsOpticalParams | None = None
) -> tuple[float, float]:
    """单通道双波长 -> (dHbO, dHbR) mM。矩阵奇异时 (nan, nan), 不抛异常。"""
    return _mbll(od760, od850, params)


def hb_metrics(hbo_mM, hbr_mM,
               params: FnirsOpticalParams | None = None) -> HbMetrics:
    """dHbT 与变化量氧合占比, 转发到 derive 的唯一实现。"""
    return _hb_metrics(hbo_mM, hbr_mM, params)
