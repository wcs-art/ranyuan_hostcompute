"""S3终版质量框架: EEG接触状态(联动阻抗)+fNIRS暗噪声基线/饱和标记+坏道自动标记.
对照原文§2.3 S3. 阻抗值由固件经事件/扩展帧下发, 未到先用阈值法占位(阈值见config).

文档《fNIRS和血氧计算20261012---VR002》§1.4/§1.5 的落地:
    μ_dark = (1/N)Σx_i, σ_dark = sqrt(Σ(x_i-μ_dark)²/(N-1))
    异常通道不做处理, 正常通道才进 MBLL。
文档没给 k(σ 倍数)与饱和上限的具体数值, 因此这里只做可注入的判据框架,
k 与 SAT_LUX 仍是待甲方定标值, 不冒充已确认参数。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

IMPEDANCE_BAD_KOHM = 50.0   # 接触阻抗>50kΩ判坏道(待2.1.3确认)
SAT_LUX = 60000.0           # fNIRS光强饱和上限(待甲方确认)
DARK_LUX = 50.0             # 暗噪声基线下限(待甲方确认)
DARK_SIGMA_K = 3.0          # 暗判据 μ_dark + k·σ_dark 的 k(文档未给, 待定标)

def eeg_contact(eeg_uv: np.ndarray, impedance_kohm: np.ndarray | None = None) -> list[str]:
    """返回每导联状态: 好/接触差/坏道."""
    out = []
    for i, v in enumerate(np.atleast_2d(eeg_uv)):
        if impedance_kohm is not None and impedance_kohm[i] > IMPEDANCE_BAD_KOHM:
            out.append("坏道"); continue
        rms = float(np.sqrt(np.mean(v**2)))
        out.append("好" if rms > 1.0 else "接触差")
    return out

def fnirs_flags(lux: np.ndarray) -> list[str]:
    """每 fNIRS 通道状态: 正常 / 饱和 / 暗噪声 / 异常。

    判据为什么用 max 而不是 mean: 饱和是"某个时刻顶到上限", 一旦发生该帧就是废的。
    取窗内均值会把"30 帧饱和 + 20 帧正常"平均回正常区间, 恰好把要找的问题抹掉。
    暗噪声则相反 —— 它是一条持续的低基线, 用中位数比均值稳(不怕个别尖峰)。
    """
    out = []
    for v in np.atleast_2d(lux):
        a = np.asarray(v, dtype=float).reshape(-1)
        if a.size == 0 or not np.all(np.isfinite(a)):
            out.append("异常")
            continue
        if float(np.max(a)) >= SAT_LUX:
            out.append("饱和")
        elif float(np.median(a)) <= DARK_LUX:
            out.append("暗噪声")
        else:
            out.append("正常")
    return out


def fnirs_levels(lux: np.ndarray) -> list[tuple[float, float, float]]:
    """每通道 (最小, 中位, 最大) 光强, 供界面显示暗噪声基线/饱和余量。"""
    out = []
    for v in np.atleast_2d(lux):
        a = np.asarray(v, dtype=float).reshape(-1)
        if a.size == 0:
            out.append((float("nan"),) * 3)
        else:
            out.append((float(np.min(a)), float(np.median(a)), float(np.max(a))))
    return out


# --------------------------------------------------------------------------
# 文档 §1.4 / §1.5: 暗场统计与采集端质控门控
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DarkReference:
    """暗场参考统计值。sigma 用 N-1 分母(样本标准差), 与文档公式一致。"""

    mu: float
    sigma: float
    n: int


def dark_reference(dark_samples, min_samples: int = 2) -> DarkReference:
    """按文档公式从暗场采集样本算 (μ_dark, σ_dark)。

    非有限值剔除后计数; 样本数 < min_samples 时 sigma=NaN, 调用方据此
    停用暗判据而不是拿一个假的小标准差把所有通道判成异常。
    """
    a = np.asarray(dark_samples, dtype=float).reshape(-1)
    a = a[np.isfinite(a)]
    n = int(a.size)
    if n == 0:
        return DarkReference(float("nan"), float("nan"), 0)
    mu = float(a.mean())
    sigma = float(a.std(ddof=1)) if n >= min_samples else float("nan")
    return DarkReference(mu, sigma, n)


def fnirs_flags_by_dark(lux, ref: DarkReference,
                        sat_lux: float = SAT_LUX,
                        k: float = DARK_SIGMA_K) -> list[str]:
    """基于暗场参考的逐通道状态: 正常 / 饱和 / 暗噪声 / 异常。

    饱和仍用 max(任一时刻顶到上限即废帧); 暗噪声用窗内中位数与
    μ_dark + k·σ_dark 比较。sigma 非有限(样本不足)时不做暗判据 ——
    宁可漏报, 也不拿没有统计意义的值误杀正常通道。
    """
    threshold = (ref.mu + k * ref.sigma) if np.isfinite(ref.sigma) else None
    out: list[str] = []
    for v in np.atleast_2d(lux):
        a = np.asarray(v, dtype=float).reshape(-1)
        if a.size == 0 or not np.all(np.isfinite(a)):
            out.append("异常")
        elif float(np.max(a)) >= sat_lux:
            out.append("饱和")
        elif threshold is not None and float(np.median(a)) <= threshold:
            out.append("暗噪声")
        else:
            out.append("正常")
    return out


def mask_from_flags(flags) -> np.ndarray:
    """只让"正常"通道进入 MBLL; 其余按文档 §1.5 不做处理。"""
    return np.array([f == "正常" for f in flags], dtype=bool)
