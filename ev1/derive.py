"""B2: 光强 -> OD -> ΔHbO/ΔHbR 数值链 (modified Beer-Lambert).

约束: 纯函数, 无 I/O, 无 Qt, 无全局可变状态; 系数全部由 FnirsOpticalParams 注入。

单位约定(全模块统一, 除注明处不做隐式换算):
    光强 lux          —— 由 lux_per_lsb 从原始码值折算
    OD   -ln(I/I0)    —— 无量纲
    ε    1/(mM·cm)    —— 消光系数
    L, DPF·L  cm      —— 源检距 / 有效光程
    ΔHbO/ΔHbR  mM     —— MBLL 直接解出 mM; 需 µM 由调用方 ×1000

换算只发生一处: mbll_batch 内 b_λ = ΔOD_λ / (DPF_λ · L)[cm], 得到 mM。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# --------------------------------------------------------------------------
# 系数表: 一个对象装全部可注入参数
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FnirsOpticalParams:
    """fNIRS 光学/定标系数。字段顺序即默认构造顺序, 全部可注入。

    禁止在别处再写这些数字: 本类是全仓唯一的系数出处。
    """

    # ---- 定标: 原始码值 -> 光强 ----
    # 值 1.0 / 单位 lux per LSB / 来源: 临时值, 待光学组确认(积分球定标未做)
    lux_per_lsb: float = 1.0

    # ---- 消光系数 ε / 1/(mM·cm) ----
    # 来源: Prahl, Oregon Medical Laser Center 摩尔消光表 (cm^-1·M^-1 除以 1000),
    #       760nm: HbO2 1486, HbR 3843; 850nm: HbO2 2526, HbR 1798
    e_hbo_760: float = 1.4866
    e_hbr_760: float = 3.8437
    e_hbo_850: float = 2.5264
    e_hbr_850: float = 1.798

    # ---- 微分路径因子 DPF / 无量纲 ----
    # 来源: 成人前额典型值 (Scholkmann & Wolf 2013, NeuroImage DPF 综述);
    #       具体探头波长/几何待光学组确认
    dpf_760: float = 6.0
    dpf_850: float = 5.5

    # ---- 源检距 L / cm ----
    # 来源: 临时值, 待光学组确认(探头几何未到货)
    src_det_cm: float = 3.0

    # ---- 数值参数(非物理系数) ----
    # 值 1e-9 / 单位 lux / 来源: 数值下限, 防 log(0) 得 -inf
    od_eps: float = 1e-9
    # 值 1e-12 / 无量纲 / 来源: 数值判据, |det| <= det_eps·max|ε|^2 判为奇异
    det_eps: float = 1e-12


_DEFAULT_PARAMS = FnirsOpticalParams()


def default_params() -> FnirsOpticalParams:
    """全仓默认系数(不可变)。调用方需要别的取值请用 dataclasses.replace 派生。"""
    return _DEFAULT_PARAMS


def _p(params: FnirsOpticalParams | None) -> FnirsOpticalParams:
    return _DEFAULT_PARAMS if params is None else params


# --------------------------------------------------------------------------
# 光强
# --------------------------------------------------------------------------


def raw_to_lux(raw: np.ndarray, params: FnirsOpticalParams | None = None) -> np.ndarray:
    """原始码值 -> 光强 lux。形状不变, 逐元素乘定标系数(单位 lux/LSB)。"""
    return np.asarray(raw, dtype=float) * _p(params).lux_per_lsb


# --------------------------------------------------------------------------
# OD 与基线(基线来源由参数给, 不写死窗长)
# --------------------------------------------------------------------------


def leading_mean_baseline(lux: np.ndarray, n_samples: int) -> np.ndarray:
    """基线 I0 = 沿最后一维前 n_samples 点均值, 结果保留该维长度 1 以便广播。

    n_samples 由调用方给(例如 10Hz 下 5 s -> 50 点); 超过长度时取全长。
    """
    if n_samples <= 0:
        raise ValueError("baseline window must be > 0 samples")
    a = np.asarray(lux, dtype=float)
    n = min(int(n_samples), a.shape[-1])
    return a[..., :n].mean(axis=-1, keepdims=True)


def od_delta(
    lux: np.ndarray,
    baseline: np.ndarray,
    params: FnirsOpticalParams | None = None,
) -> np.ndarray:
    """ΔOD = -ln(I/I0), 无量纲。baseline 可传外部暗帧/参考帧, 也可用 leading_mean_baseline。

    分母分子都夹到 od_eps 以上, 保证 0 光强时返回有限值而非 -inf。
    """
    e = _p(params).od_eps
    i = np.asarray(lux, dtype=float)
    b = np.asarray(baseline, dtype=float)
    return -np.log(np.maximum(i, e) / np.maximum(b, e))


# --------------------------------------------------------------------------
# MBLL: 解 2x2 线性方程组, 奇异返回 NaN 并计数
# --------------------------------------------------------------------------


def extinction_matrix(params: FnirsOpticalParams | None = None) -> np.ndarray:
    """2x2 消光矩阵, 行=(760, 850), 列=(HbO, HbR), 单位 1/(mM·cm)。"""
    p = _p(params)
    return np.array(
        [[p.e_hbo_760, p.e_hbr_760], [p.e_hbo_850, p.e_hbr_850]], dtype=float
    )


def differential_path_cm(params: FnirsOpticalParams | None = None) -> np.ndarray:
    """[DPF_760·L, DPF_850·L], 单位 cm。"""
    p = _p(params)
    return np.array([p.dpf_760 * p.src_det_cm, p.dpf_850 * p.src_det_cm], dtype=float)


@dataclass(frozen=True)
class MbllResult:
    """MBLL 批量结果。hbo/hbr 单位 mM; n_singular 为本批不可解元素数。"""

    hbo_mM: np.ndarray
    hbr_mM: np.ndarray
    n_singular: int
    n_total: int


def mbll_batch(
    od760: np.ndarray,
    od850: np.ndarray,
    params: FnirsOpticalParams | None = None,
) -> MbllResult:
    """批量 MBLL。od760/od850 同形(任一维之前可广播), 返回同形 ΔHbO/ΔHbR [mM]。

    矩阵奇异(两波长消光行线性相关)或元素不可解时, 该位置为 NaN 并计入
    n_singular, 绝不抛异常 —— 现场一路坏道不能让整条链崩。
    """
    p = _p(params)
    b760, b850 = np.broadcast_arrays(
        np.asarray(od760, dtype=float), np.asarray(od850, dtype=float)
    )
    shape = b760.shape
    n_total = int(b760.size)

    E = extinction_matrix(p)
    dp = differential_path_cm(p)
    det = float(E[0, 0] * E[1, 1] - E[0, 1] * E[1, 0])
    scale = float(np.max(np.abs(E))) ** 2

    if not abs(det) > p.det_eps * scale:
        nan = np.full(shape, np.nan)
        return MbllResult(nan, np.full(shape, np.nan), n_total, n_total)

    with np.errstate(invalid="ignore", divide="ignore"):
        b0 = b760 / dp[0]
        b1 = b850 / dp[1]
        hbo = (E[1, 1] * b0 - E[0, 1] * b1) / det
        hbr = (-E[1, 0] * b0 + E[0, 0] * b1) / det

    bad = ~(np.isfinite(hbo) & np.isfinite(hbr))
    n_singular = int(np.count_nonzero(bad))
    if n_singular:
        hbo = np.where(bad, np.nan, hbo)
        hbr = np.where(bad, np.nan, hbr)
    return MbllResult(hbo, hbr, n_singular, n_total)


def mbll(
    od760: float, od850: float, params: FnirsOpticalParams | None = None
) -> tuple[float, float]:
    """单通道双波长 -> (ΔHbO, ΔHbR) mM。奇异时返回 (nan, nan), 不抛异常。"""
    r = mbll_batch(od760, od850, params)
    return float(r.hbo_mM), float(r.hbr_mM)
