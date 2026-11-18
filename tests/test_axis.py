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
    """横轴标秒；纵轴标单位。没有单位的刻度不叫"幅度标尺"。

    网格图的纵轴标题用短版（EEG / ΔHbO2·ΔHbR）——格子只有二三十像素高，
    长标题会被裁成半截字；完整单位在单通道放大页。
    """
    assert mon.plots[0].getAxis("bottom").labelUnits == "s"
    assert mon.plots[0].getAxis("left").labelText == "EEG"
    assert mon.plots[4].getAxis("left").labelText == "ΔHbO2·ΔHbR"
    mon._solo_plot(0)
    assert mon._big.getAxis("left").labelUnits == "μV"
    mon._solo_plot(4)
    assert mon._big.getAxis("left").labelUnits == "mM"


def test_text_source_marks_unknown_unit(mon, tmp_path):
    """文本链路没折算过，纵轴不能标 μV —— 标了就是假信息。"""
    from text_source import TextFileSource

    p = tmp_path / "c.txt"
    p.write_bytes(b"-8234.32\r\n-7500.10\r\n")
    src = TextFileSource(p)
    mon.attach_receiver(src)
    assert "原始值" in mon.eeg_unit
    # 网格用短标题，完整单位看放大页
    assert mon.plots[0].getAxis("left").labelText == "EEG"
    mon._solo_plot(0)
    assert mon._big.getAxis("left").labelUnits == mon.eeg_unit


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


def test_zoom_out_reveals_history(mon):
    """缩出去要能看到更早的数据，而不是只留中间一段。

    现场现象：横轴 210~255，曲线只占 240~250，前面一片空白 —— 因为绘图点数
    固定取 win*fs，跟视图范围无关，缩放只能看到"当前窗口"这一截。
    """
    mon.fs_eeg = mon.fs_fnirs = 100.0
    _push(mon, [-120300.0 + (i % 5) for i in range(4000)])      # 40 s
    assert len(mon.curves[0].getData()[0]) == 1000              # 跟随时只画 10 s
    mon.plots[0].setXRange(10.0, 40.0, padding=0)               # 用户滚轮缩小
    mon._on_manual_zoom()
    mon._tick(force=True)
    x = mon.curves[0].getData()[0]
    assert len(x) == 3000, f"缩到 30 s 只画了 {len(x)} 点"
    assert round(float(x[0]), 1) == 10.0 and round(float(x[-1]), 1) == 40.0


def test_view_is_pulled_back_into_data(mon):
    """缩/拖到没有数据的区间时，视图要被拉回来——不许留空白段。

    现场现象：横轴 210~255，曲线只在 240~250，两端空白，看着像数据断了。
    """
    mon.fs_eeg = mon.fs_fnirs = 100.0
    _push(mon, [-120300.0] * 2000)                              # 只有 20 s
    mon.plots[0].setXRange(100.0, 160.0, padding=0)             # 整段都在数据之后
    mon._on_manual_zoom()
    mon._tick(force=True)
    lo, hi = _xrange(mon.plots[0])
    assert (lo, hi) == (0.0, 20.0), f"视图没被拉回数据区间: {(lo, hi)}"
    x = mon.curves[0].getData()[0]
    assert len(x) == 2000, f"拉回后应铺满 2000 点，实际 {len(x)}"
    assert round(float(x[0]), 2) == 0.0 and round(float(x[-1]), 2) == 19.99


def test_zoom_out_beyond_buffer_stops_at_oldest_sample(mon):
    """缩到比缓冲还长时停在最老样本，右端对齐最新一帧，全轴都有数据。"""
    mon.fs_eeg = mon.fs_fnirs = 100.0
    _push(mon, [-120300.0] * 4000)                              # 40 s，缓冲 15000 点=150 s
    mon.plots[0].setXRange(0.0, 400.0, padding=0)
    mon._on_manual_zoom()
    mon._tick(force=True)
    lo, hi = _xrange(mon.plots[0])
    assert hi == 40.0 and lo == 0.0, f"缩过头后视图应为 (0,40): {(lo, hi)}"


def test_zoom_is_shared_by_all_plots(mon):
    """缩一张，14 张一起走：否则同一时刻各图显示的是不同时间段的数据。"""
    mon.fs_eeg = mon.fs_fnirs = 100.0
    _push(mon, [-120300.0] * 3000)
    mon.plots[3].setXRange(5.0, 15.0, padding=0)
    mon._on_manual_zoom(3)
    mon._tick(force=True)
    for i in (0, 3, 9, 13):
        assert _xrange(mon.plots[i]) == (5.0, 15.0), f"第 {i} 张图没跟上: {_xrange(mon.plots[i])}"


# ---- 放大缩小不许改数值本身 ---------------------------------------------------
def test_grid_shows_y_range_as_labels_and_big_shows_raw(mon):
    """网格图要能读到纵轴数值（贴在图内左上角两行），放大页写完整刻度。

    坐标轴画不了：14 图并排时轴只有三十几像素宽，六位数字整段被裁掉（实测看不见）。
    所以网格用两个 QLabel 写上下限；放大页仍由轴画，且不得被 SI 前缀缩成 -104.3。
    """
    _push(mon, [-104300.0 + (i % 7) for i in range(600)])
    hi = mon._ylbl_hi[0].text()
    lo = mon._ylbl_lo[0].text()
    assert hi and lo, "网格图纵轴没有读数"
    assert "." not in hi and "." not in lo, f"坐标被缩放了: {hi} / {lo}"
    assert len(hi) >= 5 and int(hi) > int(lo), f"上下限不对: {hi} / {lo}"
    assert mon._ylbl_hi[1].isHidden(), "没有数据的通道不该显示纵轴读数"
    mon._solo_plot(0)
    axb = mon._big.getAxis("left")
    assert axb.style["showValues"] is True
    assert axb.autoSIPrefix is False
    assert axb.tickStrings([-104300.0], 1.0, 1) == ["-104300"]


def test_zoom_does_not_change_the_numbers(mon, app):
    """缩横轴只是换一段数据看，纵轴刻度必须还是原始量级，不能变成 -104.3。"""
    mon.fs_eeg = mon.fs_fnirs = 100.0
    _push(mon, [-104300.0 + (i % 9) for i in range(3000)])
    mon.resize(1200, 700); mon.show()
    mon._tick(force=True); app.processEvents()
    before = mon.plots[0].getAxis('left').tickStrings(
        list(mon.plots[0].getViewBox().viewRange()[1]), 1.0, 1)
    mon._solo_plot(0)
    mon._big.setXRange(5.0, 12.0, padding=0)
    mon._on_manual_zoom(-1)
    mon._tick(force=True); app.processEvents()
    after = mon._big.getAxis('left').tickStrings(
        list(mon._big.getViewBox().viewRange()[1]), 1.0, 1)
    assert all(round(float(t)) <= -104000 for t in after), f"缩放后刻度被改写了: {after}"
    assert all('.' not in t for t in after), f"刻度出现小数，说明值被缩放了: {after}"
    assert len(before) and len(after)


def test_curve_stays_in_view_when_x_zoomed(mon):
    """拖到直流偏置不同的另一段时，曲线不许整段跑出 Y 窗口（否则看着像没数据）。"""
    mon.fs_eeg = mon.fs_fnirs = 100.0
    # 前 10 s 在 -104300 附近，后 10 s 漂到 -101300（差 3000）
    _push(mon, [-104300.0 + (i % 5) for i in range(1000)] +
              [-101300.0 + (i % 5) for i in range(1000)])
    mon.plots[0].setXRange(10.0, 20.0, padding=0)
    mon._on_manual_zoom(0)
    mon._tick(force=True)
    lo, hi = mon.plots[0].getViewBox().viewRange()[1]
    y = mon.curves[0].getData()[1]
    assert float(min(y)) > lo and float(max(y)) < hi, \
        f"曲线跑到窗口外: 数据 {min(y):.0f}~{max(y):.0f}, 视野 {lo:.0f}~{hi:.0f}"


def test_y_axis_cannot_be_mouse_zoomed(mon):
    """纵轴不许被滚轮/拖拽改：那是"放大以后数据消失"的根源。

    x、y 都开时，滚轮一下把 Y 也缩了；数据一旦落在窗口外，屏幕上什么都没有，
    现场当成"数据没了"。这里锁死 Y，只让鼠标改时间轴。
    """
    assert mon.plots[0].getViewBox().state['mouseEnabled'] == [True, False]
    assert mon._big.getViewBox().state['mouseEnabled'] == [True, False]


def test_bottom_time_axis_is_shown_on_every_plot(mon):
    """时间刻度不能只挂在最后一行 —— 现场会看成"时间被单独放进了一个格子"。"""
    for i, p in enumerate(mon.plots):
        assert p.getAxis('bottom').style['showValues'] is True, f"第 {i} 张图没有时间刻度"


def test_zoom_keeps_data_inside_y_window_in_solo(mon, app):
    """放大页里缩到直流漂移的另一段，曲线也必须留在视野内。"""
    mon.fs_eeg = mon.fs_fnirs = 100.0
    _push(mon, [-104300.0 + (i % 7) for i in range(1000)] +
              [-101300.0 + (i % 7) for i in range(1000)])
    mon.resize(1200, 700); mon.show()
    mon._solo_plot(0)
    app.processEvents()
    for lo, hi in ((0.0, 20.0), (10.0, 20.0), (2.0, 4.0), (11.0, 13.0)):
        mon._big.setXRange(lo, hi, padding=0)
        mon._on_manual_zoom(-1)
        mon._tick(force=True)
        app.processEvents()
        y = mon._big_curve.getData()[1]
        yv = mon._big.getViewBox().viewRange()[1]
        assert len(y) > 0, f"缩到 {lo}~{hi}s 后没有数据"
        assert float(min(y)) > yv[0] and float(max(y)) < yv[1], \
            f"缩到 {lo}~{hi}s 后曲线跑出窗口: 数据 {min(y):.0f}~{max(y):.0f}, 视野 {yv[0]:.0f}~{yv[1]:.0f}"


def test_max_zoom_keeps_a_visible_window(mon):
    """"放到最大"不许缩到只剩一两个点：至少留 MIN_SPAN_S（0.2 s）。

    现场症状：滚到最里面，屏幕空白（只剩 1~2 个采样，_take 取不到或画成一个点）。
    """
    import monitor_14ch
    mon.fs_eeg = mon.fs_fnirs = 334.0
    _push(mon, [-104300.0 + (i % 7) for i in range(3000)])
    mon.plots[0].setXRange(8.899, 8.9, padding=0)
    mon._on_manual_zoom(0)
    mon._tick(force=True)
    lo, hi = _xrange(mon.plots[0])
    assert hi - lo >= monitor_14ch.MIN_SPAN_S - 1e-6, f"放到最大只剩 {hi - lo:.4f}s"
    n = len(mon.curves[0].getData()[0])
    assert n >= 50, f"放到最大只剩 {n} 个点，屏幕上等于没有"


# ---- 2026-11-18 现场两点：缩放改读数 / 缩放后整屏消失 -------------------------
def test_zoom_in_does_not_change_y_scale(mon):
    """横向放大 50 倍，纵轴量程（上限-下限）必须一点不变。

    [规格] 纵轴是幅度标尺；横轴缩放只该改"看哪段时间"。旧实现每帧拿可见段重算
    量程，实测 5 s 窗口 ±1729（跨度 3459）、0.2 s 窗口 ±942（跨度 1884）——
    同一条曲线在两种缩放下列出两组数，现场判断成"测量值在变 / 数值不对"。
    """
    np = pytest.importorskip("numpy")
    mon.fs_eeg = mon.fs_fnirs = 500.0
    t = np.arange(20000) / 500.0                      # 40 s
    _push(mon, (-120362.0 + 45.0 * np.sin(2 * np.pi * 8.0 * t)).tolist())

    spans = []
    for w in (10.0, 5.0, 2.0, 1.0, 0.5, 0.2):
        mon.plots[0].setXRange(40.0 - w, 40.0, padding=0)
        mon._on_manual_zoom(0)
        mon._tick(force=True)
        spans.append(_yspan(mon))
    assert max(spans) - min(spans) < 0.5, f"缩放过程中纵轴量程在变: {spans}"
    # 90 的峰峰要占满屏高，不能被压平（这条是原来的行为，不许回退）
    assert 90.0 / spans[-1] > 0.85, f"峰峰 90 只占 {90.0 / spans[-1]:.0%} 屏高"


def test_short_fnirs_buffer_does_not_wipe_eeg(mon):
    """fNIRS 缓冲只存 3000 点（500 Hz 下 6 s），缩到 15~25 s 时 CH1 必须照画。

    [规格] 一条链路取不到数据，不许牵连另一条。旧写法 `if seg_e is None or
    seg_f is None: 全清`：一缩到 6 s 以前那段，CH1 明明还有 30 s 数据，屏幕上
    却一条线都没有 —— 现场就是"放大以后数据不见了"，而且不点「跟随最新」回不来。
    """
    mon.fs_eeg = mon.fs_fnirs = 500.0
    _push(mon, [-120362.0 + (i % 7) for i in range(20000)])       # 40 s
    mon.plots[0].setXRange(15.0, 25.0, padding=0)
    mon._on_manual_zoom(0)
    mon._tick(force=True)

    assert _xrange(mon.plots[0]) == (15.0, 25.0)
    assert _n_points(mon, 0) == 5000, f"缩到 15~25s 后 CH1 只剩 {_n_points(mon, 0)} 点"


def test_y_reading_matches_the_view_after_zoom(mon):
    """缩放后图上那两行读数必须等于当前视野上下限，不能是上一帧的残留。"""
    mon.fs_eeg = mon.fs_fnirs = 500.0
    _push(mon, [-120362.0 + (i % 7) for i in range(20000)])
    mon.plots[0].setXRange(15.0, 25.0, padding=0)
    mon._on_manual_zoom(0)
    mon._tick(force=True)
    lo, hi = mon.plots[0].getViewBox().viewRange()[1]
    assert mon._ylbl_hi[0].text() == f"{hi:.0f}", \
        f"上限读数 {mon._ylbl_hi[0].text()} != 视野 {hi:.0f}"
    assert mon._ylbl_lo[0].text() == f"{lo:.0f}", \
        f"下限读数 {mon._ylbl_lo[0].text()} != 视野 {lo:.0f}"


def test_fnirs_out_of_buffer_says_so(mon):
    """fNIRS 曲线画不出来时，提示要写明"超出缓冲"，而不是假装这一路没数据。"""
    mon.fs_eeg = mon.fs_fnirs = 500.0
    raw = [1000.0 + (i % 5) for i in range(20)]
    for i in range(200):                       # 前 200 帧带真光强 -> fNIRS 通道算"有数据"
        mon.push_frame(seq=i + 1, eeg_uv=[-120362.0, 0, 0, 0], hbo=[0.0] * 10,
                       hbr=[0.0] * 10, raw=raw, eeg_n=1)
    for i in range(200, 20000):
        mon.push_frame(seq=i + 1, eeg_uv=[-120362.0, 0, 0, 0], hbo=[0.0] * 10,
                       hbr=[0.0] * 10, raw=None, eeg_n=1)
    mon._tick(force=True)
    mon.plots[0].setXRange(15.0, 25.0, padding=0)
    mon._on_manual_zoom(0)
    mon._tick(force=True)
    assert _n_points(mon, 0) > 0, "EEG 不该被 fNIRS 的缓冲长度拖死"
    assert _n_points(mon, mon.n_eeg) == 0
    assert "超出缓冲" in mon._tips[mon.n_eeg].toPlainText(), \
        f"fNIRS 提示不对: {mon._tips[mon.n_eeg].toPlainText()}"


def test_stale_labels_are_hidden_when_board_stops(mon):
    """停发后空图上不许留着上一帧的纵轴读数，否则现场把它当当前值读。"""
    _push(mon, [-120362.0 + (i % 7) for i in range(300)])
    assert not mon._ylbl_hi[0].isHidden(), "有数据时读数该显示"
    mon._t_rx = time.time() - 5.0
    mon._tick()
    assert mon._ylbl_hi[0].isHidden() and mon._ylbl_lo[0].isHidden()


class _CountingSource:
    """只数 poll 次数，不造数据：验证"暂停期间 force 重绘不许取帧"。"""

    kind = "text"

    def __init__(self):
        self.polls = 0

    def poll_frames(self):
        self.polls += 1
        return []

    def measured_fps(self):
        return 500.0

    def stats(self):
        return {}


def test_paused_zoom_keeps_view_and_does_not_poll(mon):
    """[规格] 暂停后放大：视野留在原段、不偷偷取帧、跟随状态不变。

    现场操作链：手动缩到一段 -> 点暂停 -> 双击放大。旧实现里 _solo_plot 强制
    follow=True + _reset_x_range()，而且 force=True 连暂停都绕过照样 poll，
    结果一放大就跳到最新一段，看起来"暂停只显示最后一段时间"。
    """
    src = _CountingSource()
    mon.attach_receiver(src)
    mon.fs_eeg = mon.fs_fnirs = 500.0
    _push(mon, [-166000.0 + (i % 40) for i in range(20000)])   # 40 s
    mon.plots[0].setXRange(15.0, 25.0, padding=0)
    mon._on_manual_zoom(0)
    mon._tick(force=True)
    assert _xrange(mon.plots[0]) == (15.0, 25.0)
    polls, recv = src.polls, mon._recv

    mon.paused = True
    mon._solo_plot(0)
    assert src.polls == polls, f"暂停期间放大 poll 了 {src.polls - polls} 次"
    assert mon._recv == recv, "暂停期间取到了新帧"
    assert _xrange(mon._big) == (15.0, 25.0), f"放大后跳到 {_xrange(mon._big)}"
    assert mon._follow is False, "放大把手动缩放状态冲掉了"
    assert _n_points(mon, 0) > 0, "放大后曲线没了"

    mon._restore_grid()
    assert _xrange(mon.plots[0]) == (15.0, 25.0), "还原到网格又跳回最新"
    assert mon._follow is False


def test_long_pause_then_zoom_keeps_waveform(mon):
    """[规格] 暂停超过 2 s 再放大，不许把冻结的波形判成"停发"清掉。"""
    mon.fs_eeg = mon.fs_fnirs = 500.0
    _push(mon, [-166000.0 + (i % 40) for i in range(5000)])
    mon.paused = True
    mon._t_rx = time.time() - 10.0
    mon._solo_plot(0)
    d = mon._big_curve.getData()
    assert d[0] is not None and len(d[0]) > 0, "暂停久了再放大，曲线被清空"
    assert not mon._big_tip.isVisible(), f"不该显示 {mon._big_tip.toPlainText()!r}"


def test_solo_axis_keeps_raw_unit_and_six_digit_ticks(mon, app, tmp_path):
    """[规格] 文本链路放大后数值不能被缩小 1000 倍，单位也不能变成 μV。

    现场对照：网格读数是 -166233，放大页纵轴却是 -165.0 —— pyqtgraph 的
    autoSIPrefix 把六位数写成三位并吞掉乘数。验证的是轴真正生成的文字，
    不是只看 autoSIPrefix 标志（离屏机器画不出字，但 generateDrawSpecs 有文字）。
    """
    from PyQt5 import QtGui
    from text_source import TextFileSource

    p = tmp_path / "raw.txt"
    p.write_bytes(b"-166233.0\r\n" * 600)
    mon.attach_receiver(TextFileSource(p))
    mon.fs_eeg = mon.fs_fnirs = 500.0
    for _ in range(30):
        mon._tick(force=True)
    mon._solo_plot(0)
    mon._tick(force=True)
    mon.show()
    for _ in range(4):
        app.processEvents()

    ax = mon._big.getAxis("left")
    assert ax.autoSIPrefix is False
    assert ax.labelUnits == mon.eeg_unit and "原始值" in ax.labelUnits
    assert "原始值" in mon._lbl_solo.text(), "放大页标题没写单位"
    img = QtGui.QImage(10, 10, QtGui.QImage.Format_ARGB32)
    painter = QtGui.QPainter(img)
    painter.setFont(ax.style["tickFont"])
    ax.picture = None                      # 强制重新生成，避免拿到旧缓存的空规格
    try:
        _, _, texts = ax.generateDrawSpecs(painter)
    finally:
        painter.end()
    labels = [spec[2].strip() for spec in texts]
    assert any(x.startswith("-166") for x in labels), f"放大页刻度不是六位原值: {labels[:6]}"
    assert not any(x in ("-166", "-166.2") for x in labels), f"刻度被缩小 1000 倍: {labels[:6]}"
