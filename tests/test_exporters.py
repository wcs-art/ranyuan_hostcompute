"""S2 导出链回归: CSV -> EDF / SNIRF, 以及 CSV 行形状.

这里的期望值来自**合同 2.5.1 S2 与 SNIRF 1.0 规范**, 不是从实现反推:
  - S2 要求三种格式"均须用第三方工具验证可读", 故 EDF 用 pyedflib 独立回读,
    SNIRF 按规范检查根组名与 measurementList 数量;
  - EDF 采样率必须等于 recording 的真实采样率(否则第三方工具里时间轴整体缩放);
  - 越过历史量程的大幅值不得被裁剪, 更不能让导出直接失败。
"""
from __future__ import annotations

import csv
import math

import pytest

import contract
from exporters import CsvWriter, export_edf, export_snirf


def _write_csv(path, n=200, fs=100.0, amp=20.0, fnirs=True):
    w = CsvWriter(path, header=contract.csv_header(optional=True))
    for i in range(n):
        t = i / fs
        fr = contract.Frame(
            device_id=1, seq=i, t_device_ms=int(t * 1000), t_host_s=t,
            t_abs_ms=1_763_000_000_000 + int(t * 1000),
            eeg_uv=[amp * math.sin(2 * math.pi * 10.0 * t + k) for k in range(4)],
            fnirs_raw=[[1000 + 10 * c + wl for wl in range(2)] for c in range(10)]
            if fnirs else [],
            fnirs_intensity=[1000.0 + c for c in range(20)] if fnirs else None,
            hbo=[0.1] * 10, hbr=[-0.05] * 10,
        )
        w.write_row(contract.frame_to_row(fr, optional=True))
    w.close()
    return path


# 1 -----------------------------------------------------------------------
def test_edf_sample_rate_follows_timestamps_not_caller(tmp_path):
    """[规格, S2] EDF 采样率必须等于真实采样率。

    调用方传 500 而数据是 100 Hz 时, 旧实现会把 5 s 的记成 1 s,
    EDFbrowser 里时间轴整体压缩 5 倍 —— 甲方一比对就发现对不上。
    """
    from pyedflib import highlevel

    csv_path = _write_csv(tmp_path / "a.csv", n=500, fs=100.0)
    edf = tmp_path / "a.edf"
    export_edf(csv_path, edf, fs_eeg=500.0)          # 故意传错

    sig, hdr, _ = highlevel.read_edf(str(edf))
    assert hdr[0]["sample_frequency"] == pytest.approx(100.0, abs=0.5)
    assert len(sig[0]) == 500                        # 5 s @100Hz, 没被压缩成 100 点


# 2 -----------------------------------------------------------------------
def test_edf_keeps_large_amplitudes(tmp_path):
    """[规格, S2] ±500 µV 以外的真实信号不得丢失。

    睡眠记录里翻身/咬牙轻松过千微伏。旧实现把 physical_min/max 写死 ±500,
    pyedflib 直接 assert 失败, 界面上只显示"EDF跳过"——真实数据一条都导不出来。
    """
    from pyedflib import highlevel

    csv_path = _write_csv(tmp_path / "b.csv", n=100, amp=2000.0)
    edf = tmp_path / "b.edf"
    export_edf(csv_path, edf)

    sig, hdr, _ = highlevel.read_edf(str(edf))
    peak = max(float(abs(v)) for v in sig[0])
    # 100 Hz 采 10 Hz 正弦, 采样点落在 36° 步长上, 峰值本身只有 0.951×2000 ≈ 1902
    assert peak > 1800.0                             # 关键: 没被削到 500
    assert hdr[0]["physical_max"] >= peak            # 量程随数据自适应


# 3 -----------------------------------------------------------------------
def test_snirf_root_group_is_nirs(tmp_path):
    """[规格, SNIRF 1.0] 根组必须叫 nirs。

    规范原文 "a single root-level group named /nirs"。写成 nirs1 时
    Homer3 与 MNE 都找不到, MNE 报 object 'nirs' doesn't exist。
    """
    import h5py

    csv_path = _write_csv(tmp_path / "c.csv", n=300)
    snirf = tmp_path / "c.snirf"
    export_snirf(csv_path, snirf)

    with h5py.File(snirf, "r") as f:
        assert "nirs" in f, f"根组必须是 nirs, 实际 {list(f.keys())}"
        d = f["nirs/data1"]
        ts = d["dataTimeSeries"]
        assert ts.shape == (300, 20)                 # 10 通道 × 2 波长
        assert float(d["time"][0]) == 0.0            # 规范: time 从 0 起算
        ml = [k for k in d.keys() if k.startswith("measurementList")]
        assert len(ml) == 20
        assert f["nirs/probe/wavelengths"][:].tolist() == [760.0, 850.0]


# 4 -----------------------------------------------------------------------
def test_csv_row_accepts_nested_intensity(tmp_path):
    """[现状] 光强按 [10][2] 嵌套传入时, 行长度仍须等于表头。

    少 10 列的话 CsvWriter 会抛 ValueError, 表现为"点开始记录后一行都写不进去"。
    """
    w = CsvWriter(tmp_path / "d.csv", header=contract.csv_header(optional=True))
    fr = contract.Frame(
        device_id=1, seq=0, t_device_ms=0, t_host_s=0.0,
        eeg_uv=[0.0] * 4,
        fnirs_raw=[[1, 2]] * 10,
        fnirs_intensity=[[float(v)] * 2 for v in range(20)],   # 嵌套 20 个
    )
    row = contract.frame_to_row(fr, optional=True)
    assert len(row) == len(w.header)
    w.close()


# 5 -----------------------------------------------------------------------
def test_fs_from_time_ignores_batch_write_timestamps():
    """[规格, 台账 M1/P13] 批量写盘的台阶时间戳不能把 500 Hz 算成几千 Hz。"""
    from exporters import _fs_from_time

    # 40 ms 定时器一拍取 8 帧：批内时间戳相同，批间才差 40 ms
    t = [round(k * 0.04, 6) for k in range(20) for _ in range(8)]
    cols = {"timestamp_s": t}
    got = _fs_from_time(cols, 