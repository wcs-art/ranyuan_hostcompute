# -*- coding: utf-8 -*-
"""文档《fNIRS和血氧计算20261012---VR002》血氧计算方法的落地守卫。

期望值来源:
  [规格] 文档 §1.3/§1.4/§1.5 的公式, 以及数学恒等式; 不由被测实现反推。
  [E1]   本机自测, 输入全部为自造数据; 不是真机/临床验证。
"""
from __future__ import annotations

import math
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import contract  # noqa: E402
import derive  # noqa: E402
import quality  # noqa: E402

QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("pyqtgraph")


# ---------------------------------------------------------------- §1.1 文档示例值


def test_default_params_match_document_example():
    """[规格 §1.1] 文档原文举例 d=30mm、DPF=6, 默认值与之对齐。

    只证明没有偏离文档示例; 文档措辞是"例如", 不构成定标验证 [E1]。
    """
    p = derive.default_params()
    assert p.src_det_cm == pytest.approx(3.0)
    assert p.dpf_760 == pytest.approx(6.0)


# ---------------------------------------------------------------- §1.3 输出指标


def test_hb_metrics_follow_doc_formula():
    """[规格 §1.3] ΔHbT=ΔHbO+ΔHbR, 变化量占比=ΔHbO/ΔHbT。"""
    hbo = np.array([0.002, 0.004])
    hbr = np.array([0.001, -0.001])
    m = derive.hb_metrics(hbo, hbr)

    assert m.hbt_mM == pytest.approx([0.003, 0.003])
    assert m.sto2_delta == pytest.approx([2.0 / 3.0, 4.0 / 3.0])
    # 变化量占比可以 >1 或 <0; 这正是它不能冒充绝对 StO2(0~1) 的原因。


def test_hb_metrics_zero_denominator_is_nan_not_zero():
    """[规格] ΔHbT≈0 时比值无意义 -> NaN, 不写 0 冒充正常值。"""
    m = derive.hb_metrics(np.array([0.0, 1e-13]), np.array([0.0, -1e-13]))
    assert np.all(np.isnan(m.sto2_delta))
    assert np.all(m.hbt_mM == 0.0)


# ---------------------------------------------------------------- §1.5 异常通道门控


def test_mask_excludes_channel_without_polluting_singular_count():
    """[规格 §1.5] 异常通道不进 MBLL; 计数与数值故障分开。"""
    od760 = np.array([0.01, 0.02, 0.03])
    od850 = np.array([0.02, 0.04, 0.06])
    valid = np.array([True, False, True])

    res = derive.mbll_batch(od760, od850, valid=valid)
    assert res.n_masked == 1
    assert res.n_singular == 0
    assert np.isnan(res.hbo_mM[1]) and np.isnan(res.hbr_mM[1])
    assert np.all(np.isfinite(res.hbo_mM[[0, 2]]))

    plain = derive.mbll_batch(od760, od850)
    assert np.allclose(res.hbo_mM[[0, 2]], plain.hbo_mM[[0, 2]])


def test_masked_channel_not_counted_as_singular_when_matrix_bad():
    """被主动排除的通道不能同时算作奇异, 否则故障计数翻倍。"""
    p = derive.default_params()
    bad = derive.FnirsOpticalParams(
        lux_per_lsb=p.lux_per_lsb,
        e_hbo_760=p.e_hbo_760, e_hbr_760=p.e_hbr_760,
        e_hbo_850=p.e_hbo_760, e_hbr_850=p.e_hbr_760,
        dpf_760=p.dpf_760, dpf_850=p.dpf_850,
        src_det_cm=p.src_det_cm,
    )
    res = derive.mbll_batch(np.array([0.01, 0.02, 0.03]),
                            np.array([0.02, 0.04, 0.06]),
                            bad, valid=[True, False, True])
    assert res.n_singular == 2
    assert res.n_masked == 1


def test_valid_shape_mismatch_raises():
    """[工程纪律] 门控位长度不对必须报错, 不许静默按广播猜通道。"""
    with pytest.raises(ValueError):
        derive.mbll_batch(np.zeros(10), np.zeros(10), valid=[True, False])


# ---------------------------------------------------------------- §1.4 暗噪声基线


def test_dark_reference_uses_doc_formula_n_minus_one():
    """[规格 §1.4] μ=(1/N)Σx, σ=sqrt(Σ(x-μ)²/(N-1))。"""
    x = np.array([10.0, 12.0, 11.0, 13.0, 14.0])
    ref = quality.dark_reference(x)

    assert ref.n == 5
    assert ref.mu == pytest.approx(12.0)
    assert ref.sigma == pytest.approx(float(np.sqrt(10.0 / 4.0)))
    assert ref.sigma == pytest.approx(float(x.std(ddof=1)))


def test_dark_reference_drops_nonfinite_and_flags_insufficient():
    """非有限值剔除; 样本不足时 sigma=NaN, 调用方据此停用暗判据。"""
    ref = quality.dark_reference([10.0, np.nan, 12.0, 14.0])
    assert ref.n == 3 and ref.mu == pytest.approx(12.0)

    one = quality.dark_reference([10.0])
    assert one.n == 1 and math.isnan(one.sigma)


def test_dark_flags_and_mask_from_flags():
    """[规格 §1.5] 饱和/暗噪声/异常通道不进 MBLL, 只有正常通道放行。"""
    ref = quality.DarkReference(mu=100.0, sigma=2.0, n=100)
    lux = np.array([
        [120.0, 122.0, 121.0],      # 正常: 中位数 121 > 100+3*2=106
        [50.0, 50.0, 50.0],         # 暗噪声
        [70000.0] * 3,              # 饱和
        [120.0, np.nan, 120.0],     # 异常
    ])
    flags = quality.fnirs_flags_by_dark(lux, ref)
    assert flags == ["正常", "暗噪声", "饱和", "异常"]
    assert quality.mask_from_flags(flags).tolist() == [True, False, False, False]


def test_insufficient_dark_samples_do_not_kill_all_channels():
    """样本不足时不做暗判据, 避免拿无统计意义的值误杀正常通道。"""
    ref = quality.DarkReference(mu=100.0, sigma=float("nan"), n=1)
    flags = quality.fnirs_flags_by_dark([[120.0, 122.0]], ref)
    assert flags == ["正常"]


# ---------------------------------------------------------------- CSV 列与界面链


def test_document_metrics_have_own_optional_columns():
    """[规格 §1.3] 指标逐通道存; 列名带 delta, 防止被当绝对 StO2。"""
    header = contract.csv_header(optional=True)
    assert "hbt_1" in header and "sto2_delta_1" in header
    assert not any(c.startswith("sto2_1") and c != "sto2_delta_1" for c in header)

    fr = contract.Frame(
        device_id=1, seq=1, t_device_ms=1, t_host_s=0.001,
        fnirs_raw=[[1, 2]] * contract.N_FNIRS,
        hbt=[0.3] * contract.N_FNIRS,
        sto2=[0.6] * contract.N_FNIRS,
    )
    row = contract.frame_to_row(fr, optional=True)
    assert row[header.index("hbt_1")] == 0.3
    assert row[header.index("sto2_delta_1")] == 0.6

    fr.sto2 = [float("nan")] * contract.N_FNIRS
    row = contract.frame_to_row(fr, optional=True)
    assert row[header.index("sto2_delta_1")] == ""


def test_monitor_blanks_bad_channel_in_csv(tmp_path, monkeypatch):
    """[E1] 本机自测: 质控判异常的通道在 CSV 写空, 不写 0 冒充结果。"""
    w = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import monitor_14ch

    monkeypatch.chdir(tmp_path)
    m = monitor_14ch.Monitor14()
    m.n_baseline = 3
    m.btn_record.setChecked(True)
    for i in range(10):
        m.push_frame(i + 1, [10.0] * 4, [0.0] * 10, [0.0] * 10,
                     raw=[1000.0 + (i % 5) for _ in range(20)])
    m.fnirs_quality = ["正常"] * contract.N_FNIRS
    m.fnirs_quality[2] = "饱和"
    m.push_frame(20, [10.0] * 4, [0.0] * 10, [0.0] * 10,
                 raw=[1000.0 + (i % 5) for i in range(20)])
    m.btn_record.setChecked(False)

    import csv
    rows = list(csv.DictReader(open(m._csv_path, encoding="utf-8-sig")))
    last = rows[-1]
    assert last["hbo_3"] == "" and last["hbt_3"] == "" and last["sto2_delta_3"] == ""
    assert last["hbo_1"] != "" and last["hbt_1"] != ""
    assert m.n_mbll_masked > 0
