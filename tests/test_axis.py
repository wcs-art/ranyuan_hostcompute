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
def _vb_height(plot):
    return plot.getViewBox().sceneBoundingRect().height()


def test_solo_switches_to_the_big_page(mon):
    """放大 = 切到独立大图页，而不是把子图在网格里搬来搬去。"""
    _push(mon, [-112666.0 + (i % 7) for i in range(600)])
    mon._solo_plot(0)
    assert mon._solo == 0
    assert mon._stack.currentWidget() is mon._solo_page
    assert "返回 14 图" in mon.btn_solo.text()
    assert "EEG1" in mon._lbl_solo.text()


def test_big_page_is_much_taller_than_a_grid_cell(mon, app):
    """14 图并排时每张只有几像素高；大图必须高出十几倍，否则"还是看不出浮动"。"""
    mon.resize(1200, 760); mon.show()
    _push(mon, [-112666.0 + (i % 7) for i in range(600)])
    for _ in range(2):
        mon._tick(); app.processEvents()
    h_cell = _vb_height(mon.plots[0])
    mon._solo_plot(0)
    for _ in range(2):
        mon._tick(); app.processEvents()
    h_big = _vb_height(mon._big)
    assert h_cell > 0
    assert h_big > h_cell * 5, f"大图没撑开: {h_cell:.1f}px -> {h_big:.1f}px"


def test_restore_keeps_the_grid_untouched(mon):
    """放大再还原必须完全可逆：14 个格子的行列位置一个都不许变。

    上一版是在同一张 QGridLayout 里 addWidget 跨满格子来放大，还原后格子被重排，
    单张可绘区从 5.1px 变成 64.1px —— 这就是"放大缩小逻辑有问题"的来源。
    """
    before = [mon._gl.getItemPosition(mon._gl.indexOf(p)) for p in mon.plots]
    for i in (0, 5, 13):
        mon._solo_plot(i)
        mon._restore_grid()
    after = [mon._gl.getItemPosition(mon._gl.indexOf(p)) for p in mon.plots]
    assert before == after
    assert mon._stack.currentWidget() is mon._grid_page
    assert mon._solo is None
    for r, pos in enumerate(before):
        assert pos[:2] == (r // mon._ncols, r % mon._ncols), f"第 {r} 张图位置变了: {pos}"


def test_solo_button_targets_last_clicked_plot(mon):
    """工具栏按钮放大"最近点中的那一张"，不然现场不知道该点哪。"""
    mon._focus_idx = 3
    mon._toggle_solo()
    assert mon._solo == 3
    assert "EEG4" in mon._lbl_solo.text()
    mon._toggle_solo()
    assert mon._solo is None
    assert mon._stack.currentWidget() is mon._grid_page


def test_solo_range_uses_only_that_channel(mon):
    """放大某一路时量程只按它自己算：否则被别路的大信号压成一条线。"""
    np = pytest.importorskip("numpy")
    t = np.arange(2000) / 500.0
    big = 4000.0 * np.sin(2 * np.pi * 3.0 * t)          # CH1 大摆幅
    small = 20.0 * np.sin(2 * np.pi * 3.0 * t)          # CH2 小摆幅
    for i in range(len(t)):
        mon.push_frame(seq=i + 1, eeg_uv=[big[i], small[i], 0.0, 0.0],
                       hbo=[0.0] * 10, hbr=[0.0] * 10, raw=None, eeg_n=2)
    mon._tick()
    s_all = mon.eeg_scale
    mon._solo_plot(1)                                    # 放大小信号那一路
    assert mon.eeg_scale < s_all / 20.0, \
        f"放大 CH2 后量程仍是 {mon.eeg_scale}（14 路全局档 {s_all}），小信号会被压平"
    mon._restore_grid()
    assert mon.eeg_scale > s_all / 3.0, "还原后量程要回到按全部通道算"


def test_auto_scale_fills_the_height(mon):
    """量程要贴着峰值：±45 的正弦必须占满 85% 以上，否则"看不出浮动"。"""
    np = pytest.importorskip("numpy")
    t = np.arange(5000) / 500.0
    _push(mon, (-112666.0 + 45.0 * np.sin(2 * np.pi * 8.0 * t)).tolist())
    lo, hi = mon.plots[0].getViewBox().viewRange()[1]
    assert hi > lo
    assert 90.0 / (hi - lo) > 0.85, f"峰峰 90 只占 {90.0 / (hi - lo):.0%} 屏高"


# ---- 刻度必须写真值 / 时间轴要能读出"采了多久" ------------------------------
def test_y_ticks_are_not_si_shrunk(mon):
    """-120300 不许被画成 -120。

    现场终端里是 -120362.52，界面纵轴却是 -120，差 1000 倍。原因是 pyqtgraph 的
    autoSIPrefix 把刻度除以 1000，而"(x1000)"这个乘数只有在轴不带单位文字时才
    拼进标签（AxisItem.labelString）；我们给了"原始值·单位待定"，乘数被静默丢掉。
    """
    for p in mon.plots + [mon._big]:
        ax = p.getAxis('left')
        assert ax.autoSIPrefix is False, "纵轴还开着 SI 前缀缩写"
        assert ax.autoSIPrefixScale == 1.0
    assert mon.plots[0].getAxis('left').tickStrings([-120300.0], 1.0, 1) == ['-120300']


def test_plotted_value_equals_received_value(mon):
    """画到曲线上的数必须就是收到的数，不许做任何缩放。"""
    vals = [-120362.52, -120373.43, -120243.03]
    _push(mon, vals)
    y = [round(float(v), 2) for v in list(mon.curves[0].getData()[1])[-3:]]
    assert y == vals, f"曲线值与接收值不符: {y} != {vals}"


def test_clock_axis_keeps_accumulating(mon):
    """时间轴要能读出"已经采了多久"：30 s 数据时横轴应为 20~30，而不是恒为 0~10。"""
    mon.fs_eeg = mon.fs_fnirs = 100.0        # 降采样，30 s 只要 3000 帧
    _push(mon, [-120300.0] * 1200)           # 12 s -> 窗口 2~12
    assert _xrange(mon.plots[0]) == (2.0, 12.0)
    _push(mon, [-120300.0] * 1800)           # 30 s -> 窗口 20~30
    assert _xrange(mon.plots[0]) == (20.0, 30.0)
    assert all(_xrange(p) == (20.0, 30.0) for p in mon.plots), "14 张图时间轴必须一致"


def test_fixed_window_mode_keeps_zero(mon):
    """切回「固定窗口」时横轴恒为 0~10，方便按合同逐格读数。"""
    mon.fs_eeg = mon.fs_fnirs = 100.0
    _push(mon, [-120300.0] * 2000)
    assert _xrange(mon.plots[0]) == (10.0, 20.0)
    mon.btn_clock.setChecked(False)
    assert _xrange(mon.plots[0]) == (0.0, 10.0)
    assert "固定窗口" in mon.btn_clock.text()
