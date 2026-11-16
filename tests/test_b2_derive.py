"""B2 判据: 光强 / OD / ΔHbO 数值链.

期望值来源标注(见 conftest 纪律):
  [规格] = 需求文档 / 公开文献 / 数学恒等式, 人填, 不由被测实现反推
  [现状] = 记录当前实现事实, 仅作回归, 不代表正确
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

import derive
import fnirs as legacy

# [规格] Prahl / Oregon Medical Laser Center 摩尔消光表, cm^-1·M^-1 ÷ 1000 -> 1/(mM·cm)
#        760nm: HbO2 1486, HbR 3843; 850nm: HbO2 2526, HbR 1798
E_HBO_760, E_HBR_760 = 1.4866, 3.8437
E_HBO_850, E_HBR_850 = 2.5264, 1.798
# [规格] 成对正弦激励参数: 生理量级 ΔHbO 幅值 2 µM, ΔHbR -0.5 µM
HB_SINE_UA = (2.0, -0.5)
FS_HB = 10.0
FREQ_HZ = 0.1
SECONDS = 60.0


def _sine_time(seconds: float = SECONDS, fs: float = FS_HB) -> np.ndarray:
    n = int(round(seconds * fs))
    return np.arange(n) / fs


def _delta_od(t: np.ndarray, dpf_760: float, dpf_850: float, l_cm: float,
              hb_ua: tuple[float, float]) -> tuple[np.ndarray, np.ndarray]:
    """正向模型(测试自建, 与被测实现无关): ΔHbO/ΔHbR [µM] -> ΔOD(760), ΔOD(850)。"""
    hbo_mM = hb_ua[0] / 1000.0 * np.sin(2 * np.pi * FREQ_HZ * t)
    hbr_mM = hb_ua[1] / 1000.0 * np.sin(2 * np.pi * FREQ_HZ * t)
    od760 = (E_HBO_760 * hbo_mM + E_HBR_760 * hbr_mM) * dpf_760 * l_cm
    od850 = (E_HBO_850 * hbo_mM + E_HBR_850 * hbr_mM) * dpf_850 * l_cm
    return od760, od850


# ---------------------------------------------------------------- 判据② 20 列非全零


def test_all_20_intensity_columns_have_nonzero_std():
    """20 列(10 通道 x 2 波长)光强: 0.1 Hz 正弦 + 小噪声, 每列 std > 0。"""
    t = _sine_time()
    rng = np.random.default_rng(7)
    codes = 2000.0 + 50.0 * np.sin(2 * np.pi * FREQ_HZ * t) + rng.standard_normal((20, t.size)) * 3.0

    lux = derive.raw_to_lux(codes)
    std = lux.std(axis=1)

    assert lux.shape == (20, t.size)
    assert np.all(np.isfinite(std))
    assert np.all(std > 0.0), f"平线通道: {np.flatnonzero(std == 0)}"
    assert np.all(lux.mean(axis=1) > 0.0)
    # [规格] 正弦幅值 50 LSB(after lux_per_lsb=1.0) -> 理论 std≈35.4; 噪声 3 -> 下限 1.0
    assert std.min() > 1.0
    print(f"\n20 列 std: min={std.min():.3f} max={std.max():.3f} lux")


def test_od_follows_constructed_extinction_change():
    """OD 数学恒等: ΔOD = -ln(I/I0), 光强减半 -> +ln2(吸收增强为正)。[规格] 行=通道, 列=时间"""
    lux = np.array([[1000.0, 500.0], [1000.0, 500.0]])
    base = derive.leading_mean_baseline(lux, 1)
    od = derive.od_delta(lux, base)

    assert np.isclose(od[0, 0], 0.0, atol=1e-12)
    assert np.isclose(od[0, 1], np.log(2.0), atol=1e-12)


def test_zero_lux_gives_finite_od():
    """0 光强(断纤)不得产生 -inf, 只能被 od_eps 夹住。[规格] 数值下限"""
    od = derive.od_delta(np.zeros((10, 4)), np.full((10, 1), 1000.0))
    assert np.all(np.isfinite(od))


def test_baseline_window_is_parameterised():
    """基线来源=前 N 点均值, 窗长由参数给。"""
    lux = np.array([100.0, 200.0, 400.0, 800.0])
    assert np.isclose(derive.leading_mean_baseline(lux, 2)[0], 150.0)
    assert np.isclose(derive.leading_mean_baseline(lux, 4)[0], 375.0)
    assert np.isclose(derive.leading_mean_baseline(lux, 99)[0], 375.0)  # 超长取全长
    with pytest.raises(ValueError):
        derive.leading_mean_baseline(lux, 0)


# ---------------------------------------------------------------- 判据③ DPF 真起作用


def test_two_dpf_sets_give_different_output():
    """注入两组 DPF(2x), ΔHbO 必须差 2x —— 证明系数没被写死。"""
    p1 = derive.default_params()
    p2 = replace(p1, dpf_760=2 * p1.dpf_760, dpf_850=2 * p1.dpf_850)
    od = np.array([0.01, 0.03])

    r1 = derive.mbll_batch(od[0], od[1], p1)
    r2 = derive.mbll_batch(od[0], od[1], p2)

    assert not np.allclose(r1.hbo_mM, r2.hbo_mM)
    assert not np.allclose(r1.hbr_mM, r2.hbr_mM)
    # [规格] OD 固定时浓度与有效光程成反比: DPF 翻倍 -> 浓度减半
    assert np.allclose(r2.hbo_mM, r1.hbo_mM / 2.0, rtol=1e-12)
    assert np.allclose(r2.hbr_mM, r1.hbr_mM / 2.0, rtol=1e-12)
    print(f"\nDPF x1 -> ΔHbO={float(r1.hbo_mM):.6f} mM; DPF x2 -> {float(r2.hbo_mM):.6f} mM")


# ---------------------------------------------------------------- 判据④ ΔHbO 非平线


def test_hbo_peak_to_peak_under_0p1hz_sine():
    """0.1 Hz 正弦: 正向模型造光强 -> 全链反解, ΔHbO 峰峰值须恢复 2*2µM。"""
    p = derive.default_params()
    t = _sine_time()
    od760, od850 = _delta_od(t, p.dpf_760, p.dpf_850, p.src_det_cm, HB_SINE_UA)

    i0 = 2000.0
    lux760 = i0 * np.exp(-od760)
    lux850 = i0 * np.exp(-od850)
    lux = np.stack([np.stack([lux760] * 10), np.stack([lux850] * 10)], axis=-1)  # [10][2][n]

    raw = lux / p.lux_per_lsb
    assert np.all(derive.raw_to_lux(raw).std(axis=(1, 2)) > 0.0)

    od = derive.od_delta(lux, np.full_like(lux, i0))
    res = derive.mbll_batch(od[..., 0], od[..., 1], p)

    assert res.n_singular == 0
    hbo_ua = res.hbo_mM[0] * 1000.0
    p2p = float(hbo_ua.max() - hbo_ua.min())

    # [规格] 幅值 2 µM 的正弦 -> 峰峰值 4 µM, 容差 1%
    assert p2p == pytest.approx(2 * HB_SINE_UA[0], rel=1e-2)
    # [老四定档] 0.1 Hz 生理量级激励下 ΔHbO 峰峰值下限 1 µM, 低于此视为平线
    assert p2p > 1.0
    assert float(np.ptp(res.hbr_mM[0]) * 1000.0) == pytest.approx(2 * abs(HB_SINE_UA[1]), rel=1e-2)
    print(f"\nΔHbO 峰峰值 = {p2p:.6f} µM ({float(res.hbo_mM[0].max()-res.hbo_mM[0].min()):.9f} mM)")


# ---------------------------------------------------------------- 判据① 奇异不抛异常


def _singular_params() -> derive.FnirsOpticalParams:
    p = derive.default_params()
    return replace(p, e_hbo_850=p.e_hbo_760, e_hbr_850=p.e_hbr_760)


def test_singular_matrix_returns_nan_and_counts():
    """两波长消光行相同 -> 2x2 矩阵奇异: 返回 NaN, 计数增加, 不抛异常。"""
    good = derive.default_params()
    od760 = np.array([0.01, 0.02, 0.03])
    od850 = np.array([0.02, 0.04, 0.06])

    ok = derive.mbll_batch(od760, od850, good)
    assert ok.n_singular == 0 and ok.n_total == 3

    bad_p = _singular_params()
    assert abs(np.linalg.det(derive.extinction_matrix(bad_p))) == 0.0

    first = derive.mbll_batch(od760, od850, bad_p)
    assert first.n_singular == 3 and first.n_total == 3
    assert np.all(np.isnan(first.hbo_mM)) and np.all(np.isnan(first.hbr_mM))

    second = derive.mbll_batch(od760, od850, bad_p)
    assert second.n_singular == first.n_singular  # 每次调用独立计数, 不依赖全局状态
    assert first.n_singular > ok.n_singular

    hbo, hbr = derive.mbll(od760[0], od850[0], bad_p)
    assert np.isnan(hbo) and np.isnan(hbr)
    print(f"\n奇异: n_singular {ok.n_singular} -> {first.n_singular}, hbo={hbo}")


def test_single_bad_channel_is_counted_not_fatal():
    """一路 NaN 输入只污染该通道, 其余通道仍出数。"""
    od760 = np.array([0.01, np.nan, 0.03])
    od850 = np.array([0.02, 0.04, 0.06])
    res = derive.mbll_batch(od760, od850)

    assert res.n_singular == 1
    assert np.isnan(res.hbo_mM[1])
    assert np.all(np.isfinite(res.hbo_mM[[0, 2]]))


# ---------------------------------------------------------------- 兼容与纯度


def test_params_default_is_immutable():
    p = derive.default_params()
    with pytest.raises(Exception):
        p.dpf_760 = 99.0
    assert derive.default_params() is p  # 同一份, 但不可改


def test_legacy_fnirs_forwards_to_derive():
    """[现状] 旧 import 路径可用, 且与 derive 结果逐元素相同。"""
    rng = np.random.default_rng(3)
    raw = 1000.0 + rng.standard_normal((10, 20))

    assert np.array_equal(legacy.raw_to_lux(raw), derive.raw_to_lux(raw))
    assert np.array_equal(legacy.raw_to_lux(raw), derive.raw_to_lux(raw, None))
    base = derive.leading_mean_baseline(raw, 5)
    assert np.array_equal(legacy.od_delta(raw, base), derive.od_delta(raw, base))

    a = legacy.mbll(0.01, 0.02)
    b = derive.mbll(0.01, 0.02)
    assert a == b and len(a) == 2


def test_no_scalar_literals_in_derive_source():
    """系数不得写在函数体内: derive.py 顶层只允许多行字符串/导入/dataclass 字段默认值。"""
    import inspect

    src = inspect.getsource(derive)
    for fn in (derive.raw_to_lux, derive.od_delta, derive.mbll_batch, derive.mbll,
               derive.leading_mean_baseline):
        body = inspect.getsource(fn)
        for bad in ("1.4866", "3.8437", "2.5264", "1.798", "6.0", "5.5", "3.0"):
            assert bad not in body, f"函数体内出现字面量系数 {bad} in {fn.__name__}"
    assert "CAL = dict" not in src
