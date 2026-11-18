"""S1/S3 界面回归: 记录真的写出 CSV、通道质量真的算在 10 个通道上.

为什么需要这一层: 之前 monitor_14ch 的 _rec_row 里用了 contract 却没 import,
每帧抛 NameError, 被 _tick 的 except 吞掉 —— 单测全绿, 界面上却"点开始记录,
一行都写不出来"。这类"链路断了但没人报错"的问题只有跑一遍真实路径才能暴露。
"""
from __future__ import annotations

import csv
import math
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("pyqtgraph")

import contract  # noqa: E402
import monitor_14ch  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _monitor(app):
    import monitor_14ch
    return monitor_14ch.Monitor14()


# 1 -----------------------------------------------------------------------
def test_recording_writes_rows_with_full_columns(app, tmp_path, monkeypatch):
    """[规格, S2] 点开始记录后必须真的落盘, 且行宽等于表头(列数从契约推导)。"""
    w = _monitor(app)
    monkeypatch.chdir(tmp_path)
    w.btn_record.setChecked(True)
    for i in range(30):
        w.push_frame(i, [10.0] * 4, [0.0] * 10, [0.0] * 10,
                     raw=[1000.0 + c for c in range(20)])
    w.btn_record.setChecked(False)

    path = w._csv_path
    assert path is not None and path.exists(), "记录没有产生文件"
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    assert len(rows) == 30
    header = contract.csv_header(optional=True)
    assert len(rows[0]) == len(header)
    # 文档 §1.3 的输出指标: 10 路 ΔHbT + 10 路变化量占比
    assert len([c for c in header if c.startswith("hbt_")]) == contract.N_FNIRS
    assert len([c for c in header if c.startswith("sto2_delta_")]) == contract.N_FNIRS


# 2 -----------------------------------------------------------------------
def test_quality_is_per_fnirs_channel_not_per_wavelength(app):
    """[规格, S3] 质量判定按 10 个 fNIRS 通道, 不是 20 个波长列。

    buf_raw 有 20 行(10 通道 × 2 波长)。直接喂进 fnirs_flags 会得到 20 个状态,
    界面上的通道号就对不上实物, 甲方按 CH7 去找会找到别的地方。
    """
    w = _monitor(app)
    for i in range(100):                    # 25 帧一次判定, 100 帧保证刷到
        raw = [1000.0] * 20
        if i >= 50:
            raw[4] = raw[5] = 90000.0        # CH3 两波长饱和
            raw[12] = raw[13] = 0.0          # CH7 两波长无光
        w.push_frame(i, [10.0] * 4, [0.0] * 10, [0.0] * 10, raw=raw)

    assert len(w.fnirs_quality) == contract.N_FNIRS == 10
    assert w.fnirs_quality[2] == "饱和"
    assert w.fnirs_quality[6] == "暗噪声"


# 3 -----------------------------------------------------------------------
def test_display_has_14_curves(app):
    """[规格, S1] 同一界面 4 路 EEG + 10 通道 fNIRS = 14 条曲线。"""
    w = _monitor(app)
    assert len(w.curves) == 14
    assert len(w.chk) == 14


# 4 -----------------------------------------------------------------------
def test_od_columns_filled_after_baseline(app, tmp_path, monkeypatch):
    """[规格, S2 可选列] 基线建好后 OD 列写真实值, 不再留空。

    OD = -ln(I/I0) 是无量纲比值, 与 lux 标定系数无关, 因此不需要等光学标定就能算。
    """
    w = _monitor(app)
    monkeypatch.chdir(tmp_path)
    w.btn_record.setChecked(True)
    for i in range(w.n_baseline + 20):
        raw = [1000.0 + 50 * math.sin(i / 20.0 + c) for c in range(20)]
        w.push_frame(i, [10.0] * 4, [0.0] * 10, [0.0] * 10, raw=raw)
    w.btn_record.setChecked(False)

    rows = list(csv.DictReader(open(w._csv_path, encoding="utf-8-sig")))
    od_cols = [k for k in rows[0] if k.endswith("_od")]
    assert len(od_cols) == 20
    last = rows[-1]
    assert any(last[c] not in ("", None) for c in od_cols), "基线已建好, OD 却是空的"


# 5 -----------------------------------------------------------------------
def test_seq_wrap_counts_no_loss(app):
    """[规格, 协议 §1.3] seq 是 uint32, 回绕后从 0 继续; 按差值判丢包, 不按大小.

    旧实现用 `seq <= 上一条` 判复位, 0xFFFFFFFF -> 0 这一下会被当成复位静默放过,
    回绕前后的丢包就永远统计不出来。
    """
    w = _monitor(app)
    w._seq_last = -1
    for s in (0xFFFFFFFD, 0xFFFFFFFE, 0xFFFFFFFF, 0, 1, 2):
        w.push_frame(s, [0.0] * 4, [0.0] * 10, [0.0] * 10, raw=[1000.0] * 20)
    assert w._lost == 0, f"回绕被误判成丢包 {w._lost}"


def test_seq_jump_forward_counts_loss(app):
    """[规格] 中间缺 3 帧就是丢 3 帧, 不含端点。"""
    w = _monitor(app)
    w._seq_last = -1
    for s in (10, 14):
        w.push_frame(s, [0.0] * 4, [0.0] * 10, [0.0] * 10, raw=[1000.0] * 20)
    assert w._lost == 3


def test_seq_backward_is_reset_not_mass_loss(app):
    """[规格] 设备复位(seq 回落)不能算成"丢了几百万帧"。

    按 32 位差值算, 1000 -> 3 的差值是 0xFFFFF003, 大于 2^31, 判为复位。
    """
    w = _monitor(app)
    w._seq_last = -1
    for s in (1000, 3):
        w.push_frame(s, [0.0] * 4, [0.0] * 10, [0.0] * 10, raw=[1000.0] * 20)
    assert w._lost == 0, f"复位被算成丢包 {w._lost}"
    assert w._seq_resets == 1


# 6 -----------------------------------------------------------------------
def test_csv_timestamp_is_sampling_time_even_when_pushed_in_a_burst(app, tmp_path, monkeypatch):
    """[规格, 台账 M1/P13] 批量取帧时 timestamp_s 必须等间隔，不能挤在同一毫秒。

    旧写法 t = time.time()-_t0：300 帧在 tight loop 里 60 ms 写完，
    导出 _fs_from_time 算出 3642 Hz，EDF/SNIRF 时间轴全错。
    """
    w = _monitor(app)
    monkeypatch.chdir(tmp_path)
    w.btn_record.setChecked(True)
    for i in range(300):
        w.push_frame(i + 1, [10.0] * 4, [0.0] * 10, [0.0] * 10,
                     raw=[1000.0] * 20)
    w.btn_record.setChecked(False)
    rows = list(csv.DictReader(open(w._csv_path, encoding="utf-8-sig")))
    t = [float(r["timestamp_s"]) for r in rows]
    dt = [b - a for a, b in zip(t, t[1:])]
    assert len(set(round(d, 6) for d in dt)) == 1, f"时间戳不是等间隔: {dt[:6]}"
    assert dt[0] == pytest.approx(1.0 / w.fs_eeg, rel=1e-3)

    # 设备给了单调毫秒时优先用它，不受界面 fs 估计影响
    w2 = _monitor(app)
    monkeypatch.chdir(tmp_path)
    w2.btn_record.setChecked(True)
    for i in range(100):
        w2.push_frame(i + 1, [10.0] * 4, [0.0] * 10, [0.0] * 10,
                      raw=[1000.0] * 20, t_dev_ms=i * 4)
    w2.btn_record.setChecked(False)
    t2 = [float(r["timestamp_s"]) for r in
          csv.DictReader(open(w2._csv_path, encoding="utf-8-sig"))]
    assert t2[1] - t2[0] == pytest.approx(0.004, abs=1e-6)


def test_quality_panel_shows_dark_noise_baseline(app):
    """[规格, S3] fnirs_levels 之前是死代码；暗噪声基线必须真的显示在界面上。"""
    w = _monitor(app)
    for i in range(100):
        w.push_frame(i + 1, [10.0] * 4, [0.0] * 10, [0.0] * 10,
                     raw=[1000.0 + i % 5 for _ in range(20)])
    assert len(w._fnirs_levels) == 10
    text = w.lbl_quality.text()
    assert "暗噪声基线" in text, f"界面上没有基线读数: {text[:200]}"
    assert "系数=1待标定" in text, "定标系数是占位值，必须显式提示"


def test_lux_coefficient_has_single_source(app):
    """[工程约束] 光学系数只在 derive.FnirsOpticalParams 定义一次。"""
    from pathlib import Path

    from derive import default_params

    w = _monitor(app)
    assert w.lux_per_lsb == default_params().lux_per_lsb
    src = Path(monitor_14ch.__file__).read_text(encoding="utf-8")
    assert "lux_per_lsb = 1.0" not in src, "monitor_14ch 里又写了一份系数"
