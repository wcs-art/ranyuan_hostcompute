"""S3终版质量框架: EEG接触状态(联动阻抗)+fNIRS暗噪声基线/饱和标记+坏道自动标记.
对照原文§2.3 S3. 阻抗值由固件经事件/扩展帧下发, 未到先用阈值法占位(阈值见config)."""
from __future__ import annotations
import numpy as np

IMPEDANCE_BAD_KOHM = 50.0   # 接触阻抗>50kΩ判坏道(待2.1.3确认)
SAT_LUX = 60000.0           # fNIRS光强饱和上限(待甲方确认)
DARK_LUX = 50.0             # 暗噪声基线下限(待甲方确认)

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
