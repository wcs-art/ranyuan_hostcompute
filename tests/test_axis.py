"""波形坐标轴回归 —— 合同 S1「时间轴统一」与「幅度标尺」。

为什么需要这一层: 原来横轴用 arange(n)/fs 从 0 起算, 而 n 又随实测帧率变,
同一屏上 EEG 显示 0.3 s、fNIRS 显示 10 s, 14 条曲线根本对不齐; 纵轴一个标签
都没有, 看不出是 μV 还是原始值。这类问题单测测不到, 只能查坐标轴对象本身。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("pyqtgraph")


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def mon(app):
    import monitor_14ch
    w = monitor_14ch.Monitor14()
    yield w
    w.close()


def _xrange(plot):
    lo, hi = plot.getViewBox().viewRange()[0]
    return round(lo, 3), round(hi, 3)


def test_all_channels_share_one_time_axis(mon):
    """14 个子图必须是同一个横轴: [-win_s, 0]，0 = 当前时刻。"""
    assert len(mon.plots) == 14
    for i, p in enumerate(mon.plots):
        assert _xrange(p) == (-10.0, 0.0), f"第 {i} 张图横轴不一致: {_xrange(p)}"


def test_window_switch_moves_axis(mon):
    """切 5s / 30s 时间窗，横轴要立刻跟着变，不能等下次重绘。"""
    mon.cmb_win.setCurrentIndex(0)          # 5s
    for p in mon.plots:
        assert _xrange(p) == (-5.0, 0.0)
    mon.cmb_win.setCurrentIndex(2)          # 30s
    for p in mon.plots:
        assert _xrange(p) == (-30.0, 0.0)


def test_axis_labels_carry_units(mon):
    """横轴标秒；纵轴标单位。没有单位的刻度不叫"幅度标尺"。"""
    assert mon.plots[0].getAxis("bottom").labelUnits == "s"
    assert mon.plots[0].getAxis("left").labelUnits == "μV"
    assert mon.plots[4].getAxis("left").labelUnits == "mM"


def test_text_source_marks_unknown_unit(mon, tmp_path):
    """文本链路没折算过，纵轴不能标 μV —— 标了就是假信息。"""
    from text_source import TextFileSource

    p = tmp_path / "c.txt"
    p.write_bytes(b"-8234.32\r\n-7500.10\r\n")
    src = TextFileSource(p)
    mon.attach_receiver(src)
    assert "原始值" in mon.eeg_unit
    assert mon.plots[0].getAxis("left").labelUnits == mon.eeg_unit


def test_manual_zoom_stops_auto_follow(mon):
    """手动缩放后不能被 40ms 的重绘抢回去，否则合同 S1 的"支持缩放"等于没有。"""
    mon._on_manual_zoom()
    assert mon._follow is False
    assert mon.btn_follow.isChecked() is False

    mon.plots[0].setXRange(-3.0, -1.0, padding=0)   # 用户自己放大了一段
    mon._tick()
    assert _xrange(mon.plots[0]) == (-3.0, -1.0)

    mon.btn_follow.setChecked(True)                 # 点「跟随最新」恢复
    assert _xrange(mon.plots[0]) == (-10.0, 0.0)


def test_replay_does_not_corrupt_sample_rate(mon, tmp_path):
    """回放是瞬间读完文件，那个"帧率"是回放速度，不能拿来当采样率。"""
    from text_source import TextFileSource

    p = tmp_path / "c.txt"
    p.write_bytes(b"-100.0\r\n-200.0\r\n-300.0\r\n" * 40)
    src = TextFileSource(p)
    mon.attach_receiver(src)
    for _ in range(5):
        mon._recv += 1                      # 越过 "样本太少不校准" 的门槛
        src.poll_frames()
        mon._update_rate()
    assert mon.fs_eeg == 500.0              # 保持构造时的标称值, 不被回放速度覆盖
