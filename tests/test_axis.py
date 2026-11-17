"""波形坐标轴回归 —— 合同 S1「时间轴统一」与「幅度标尺」。

为什么需要这一层: 原来横轴用 arange(n)/fs 从 0 起算, 而 n 又随实测帧率变,
同一屏上 EEG 显示 0.3 s、fNIRS 显示 10 s, 14 条曲线根本对不齐; 纵轴一个标签
都没有, 看不出是 μV 还是原始值。这类问题单测测不到, 只能查坐标轴对象本身。
"""
from __future__ import annotations

import os
import time

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
    """14 个子图必须是同一个横轴: 0 ~ win_s 秒，最右是当前。"""
    assert len(mon.plots) == 14
    for i, p in enumerate(mon.plots):
        assert _xrange(p) == (0.0, 10.0), f"第 {i} 张图横轴不一致: {_xrange(p)}"


def test_window_switch_moves_axis(mon):
    """切 5s / 30s 时间窗，横轴要立刻跟着变，不能等下次重绘。"""
    mon.cmb_win.setCurrentIndex(0)          # 5s
    for p in mon.plots:
        assert _xrange(p) == (0.0, 5.0)
    mon.cmb_win.setCurrentIndex(2)          # 30s
    for p in mon.plots:
        assert _xrange(p) == (0.0, 30.0)


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

    mon.plots[0].setXRange(3.0, 5.0, padding=0)     # 用户自己放大了一段
    mon._tick()
    assert _xrange(mon.plots[0]) == (3.0, 5.0)

    mon.btn_follow.setChecked(True)                 # 点「跟随最新」恢复
    assert _xrange(mon.plots[0]) == (0.0, 10.0)


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


def _n_points(mon, i):
    data = mon.curves[i].getData()
    return 0 if data[0] is None else len(data[0])


def test_no_source_draws_nothing(mon):
    """没接数据源时一条线都不许有。

    否则缓冲里的补 0 会被画成贴 0 的平线，现场会当成"信号一直是 0"。
    """
    for _ in range(10):
        mon._tick()
    assert sum(_n_points(mon, i) > 0 for i in range(mon.n_total)) == 0


def test_only_channels_with_data_are_drawn(mon, tmp_path):
    """文本链路每行 1 列：只有 CH1 是真数据，其余 13 路是补 0 占位，必须不画。"""
    from text_source import TextFileSource

    p = tmp_path / "one_col.txt"
    p.write_bytes(b"-8234.32\r\n" * 300)
    src = TextFileSource(p)
    mon.attach_receiver(src)
    for _ in range(200):
        mon._tick()
        if src.exhausted():
            break
    assert _n_points(mon, 0) > 0, "CH1 有真数据，应该画"
    assert all(_n_points(mon, i) == 0 for i in range(1, mon.n_total)), "无数据通道不该画线"


def test_stops_drawing_after_board_stops(mon, tmp_path):
    """板子停发后不能继续画旧波形，否则看起来像还在采。"""
    from text_source import TextFileSource

    p = tmp_path / "one_col.txt"
    p.write_bytes(b"-8234.32\r\n" * 100)
    src = TextFileSource(p)
    mon.attach_receiver(src)
    for _ in range(80):
        mon._tick()
        if src.exhausted():
            break
    assert _n_points(mon, 0) > 0

    mon._t_rx = time.time() - 5.0        # 模拟 5 秒没有新帧
    for _ in range(10):
        mon._tick()
    assert _n_points(mon, 0) == 0, "停发后还在画旧波形"
    assert "停发" in mon.lbl.text(), f"状态栏没提示停发: {mon.lbl.text()}"
