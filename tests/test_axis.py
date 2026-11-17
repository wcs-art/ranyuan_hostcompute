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


# ---- 自动量程：抗毛刺 / 空通道提示 -------------------------------------------
def _push(mon, vals, eeg_n=1):
    """不接串口，直接按帧喂数据（与 push_frame 的真实入参一致）。"""
    for i, v in enumerate(vals):
        mon.push_frame(seq=i + 1, eeg_uv=[float(v), 0.0, 0.0, 0.0],
                       hbo=[0.0] * 10, hbr=[0.0] * 10, raw=None, eeg_n=eeg_n)
    mon._tick()


def _yspan(mon, i=0):
    lo, hi = mon.plots[i].getViewBox().viewRange()[1]
    return float(hi - lo)


def test_single_glitch_does_not_flatten_the_waveform(mon):
    """一个毛刺不能把量程撑到上万。

    现场实测：数据在 -112666 附近波动 ±25，窗口里混进 1 个 -127968 的毛刺，
    按 max-min 定量程得 21933，真实起伏只占 0.8% 屏高 —— 界面上看起来
    "一条曲线都没有"。VOFA 同一段数据也被撑大到 283 倍。
    """
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(0)
    vals = (-112666.0 + rng.normal(0.0, 25.0, 5000)).tolist()
    vals[2500] = -127968.1
    _push(mon, vals)
    assert _yspan(mon) < 2000.0, f"量程被毛刺撑开: {_yspan(mon)}"
    assert mon.eeg_scale <= 300.0, f"量程档位还停在 {mon.eeg_scale}"


def test_wide_real_signal_still_uses_the_height(mon):
    """反过来也不能压太死：真有大摆幅（眨眼/体动）时必须撑开量程。"""
    np = pytest.importorskip("numpy")
    t = np.arange(5000) / 500.0
    vals = (-12000.0 + 3000.0 * np.sin(2 * np.pi * 2.0 * t)).tolist()   # 峰峰 6000
    _push(mon, vals)
    span = _yspan(mon)
    assert span > 6000.0, f"6000 的峰峰被压到 {span}"
    assert 6000.0 / span > 0.3, f"只占 {6000.0 / span:.0%} 屏高"


def test_idle_channel_shows_a_hint(mon):
    """没有数据的子图不能只是一片黑：要写明"本链路未发送该通道"。"""
    _push(mon, [-8234.32] * 100)
    assert mon._tips[0].isVisible() is False, "CH1 有数据，不该挂提示"
    assert mon._tips[1].isVisible() is True, "空通道没有任何提示"
    assert "无数据" in mon._tips[1].toPlainText()


def test_waiting_hint_before_any_frame(mon):
    """一帧都没收到时，14 张图都该写"等待数据"，而不是留白。"""
    for _ in range(3):
        mon._tick()
    assert all("等待数据" in t.toPlainText() for t in mon._tips)


# ---- 单通道放大 / 纵轴贴合 ---------------------------------------------------
def _vb_height(mon, i=0):
    return mon.plots[i].getViewBox().sceneBoundingRect().height()


def test_solo_plot_fills_the_grid(mon, app):
    """14 图并排时每张只有几像素高，双击放大后必须真正占满整块。"""
    mon.resize(1200, 760); mon.show()
    _push(mon, [-112666.0 + (i % 7) for i in range(600)])
    for _ in range(2):
        mon._tick(); app.processEvents()
    h14 = _vb_height(mon)
    mon._solo_plot(0)
    for _ in range(2):
        mon._tick(); app.processEvents()
    assert sum(p.isVisible() for p in mon.plots) == 1, "放大后其它通道还在抢版面"
    assert "还原" in mon.btn_solo.text()
    solo_h = _vb_height(mon)
    assert solo_h > h14 * 3, f"放大没生效: {h14:.1f}px -> {solo_h:.1f}px"


def test_restore_brings_back_all_14(mon, app):
    mon.resize(1200, 760); mon.show()
    _push(mon, [-112666.0 + (i % 7) for i in range(600)])
    mon._solo_plot(2)
    mon._restore_grid()
    app.processEvents()
    assert all(p.isVisible() for p in mon.plots)
    assert mon._solo is None
    assert "单通道放大" in mon.btn_solo.text()


def test_solo_button_targets_last_clicked_plot(mon):
    """工具栏按钮放大"最近点中的那一张"，不然现场不知道该点哪。"""
    mon._focus_idx = 3
    mon._toggle_solo()
    assert mon._solo == 3
    # 窗口没 show 时 isVisible() 恒为 False，这里看"有没有被显式隐藏"
    assert not mon.plots[3].isHidden() and mon.plots[0].isHidden()
    mon._toggle_solo()
    assert mon._solo is None and not mon.plots[0].isHidden()


def test_auto_scale_fills_the_height(mon):
    """量程要贴着峰值：±45 的正弦必须占满 85% 以上，否则"看不出浮动"。"""
    np = pytest.importorskip("numpy")
    t = np.arange(5000) / 500.0
    _push(mon, (-112666.0 + 45.0 * np.sin(2 * np.pi * 8.0 * t)).tolist())
    lo, hi = mon.plots[0].getViewBox().viewRange()[1]
    assert hi > lo
    assert 90.0 / (hi - lo) > 0.85, f"峰峰 90 只占 {90.0 / (hi - lo):.0%} 屏高"
