"""14 通道实时波形：4 路 EEG + 10 通道 fNIRS，可缩放、暂停、导出。"""
from __future__ import annotations
import sys, time, math, numpy as np
from PyQt5 import QtWidgets, QtCore, QtGui
import pyqtgraph as pg

import contract   # _rec_row 依赖它; 缺失时 NameError 会被 _tick 的 except 吞掉,
                  # 表现为"点开始记录后一行都写不出来, 界面却毫无提示"。
from derive import default_params as _derive_default_params  # 光学系数唯一出处

N_EEG = 4; N_FNIRS = 10
#: 默认一屏 4 路、2×2 铺满；其余通道分页，滚轮/左右滑动/按钮翻页。
PAGE_SIZE = 4
PAGE_COLS = 2

#: 自动量程用的分位点。不能用 max/min 定量程: 现场一个半包毛刺就能把量程撑到
#: 上万倍, 真实信号被压成一条直线。实测例子: 数据在 -112666 附近波动 ±40,
#: 窗口里混进 1 个 -127968 的毛刺, 按 max-min 得量程 21933 -> 起伏只占 0.8%
#: 屏高, 界面上看起来"一条曲线都没有"; VOFA 同一段数据也是被撑成 283 倍。
AUTO_Q_LO = 0.5
AUTO_Q_HI = 99.5
#: 极端值超过鲁棒半幅的这么多倍才判为毛刺, 否则照样算进量程
#: (真实的大幅波动如眨眼/运动不该被裁掉)
AUTO_GLITCH_X = 4.0
#: 自动量程在峰值上再留的余量。1.05 = 曲线几乎贴着上下边框,
#: 这是"看得出浮动"的关键: 留太多就是一条压扁的线(现场原话: 太紧凑)。
AUTO_PAD = 1.05
#: 量程下限, 防止纯噪声被放大成满屏（那不是"浮动", 是没有信号）
AUTO_MIN_SCALE = 10.0
#: 单张图最多画的点数。缓冲能留 150 s（500 Hz 下 7.5 万点），
#: 缩到全程时 14 张图各 7.5 万点会把 40 ms 的重绘节拍拖垮，超了就等间隔抽稀。
MAX_DRAW = 20000
#: 时间轴最小可视宽度（秒）。滚轮/双击"放到最大"时不许再窄下去：
#: 窄到一个采样点以内就只剩 1~2 个点，屏幕上看不出波形（现场当成"数据没了"）。
MIN_SPAN_S = 0.2
#: 量程回差: 变化不到 ±15% 就不改档, 否则每帧跟着噪声跳
AUTO_HYST = 1.15


class _RawValueAxis(pg.AxisItem):
    """纵轴刻度永远写收到的原值，不让 pyqtgraph 乘 SI 系数。

    光靠 `enableAutoSIPrefix(False)` 不够：它自己仍会按当前量程算
    `autoSIPrefixScale`（六位数 -> 0.001），而 `tickStrings` 无条件用这个系数，
    结果 -161437 被画成 -161.4。这里把系数钉成 1，不管内部状态怎么变。
    """

    def tickStrings(self, values, scale, spacing):
        return super().tickStrings(values, 1.0, spacing)


class Monitor14(QtWidgets.QMainWindow):
    def __init__(self, eeg_names=None, sim=False):
        super().__init__()
        # sim 仅为兼容旧调用保留，界面不造数：没有数据就是平线
        self.sim = bool(sim)
        self._receiver = None
        #: 设备编号取自帧头 device_id，不能写死
        self._device_id = 1
        self.eeg_names = eeg_names or [f"ECG{i+1}" for i in range(N_EEG)]
        self.n_eeg = len(self.eeg_names)
        self.n_total = self.n_eeg + N_FNIRS
        self.setWindowTitle(f"{self.n_total}通道同步显示 (统一时间轴)")
        self.paused = False; self.show_hbo = True
        # 当前固件每个 EEG 帧都带一组 fNIRS 占位字段，绘图缓存也每帧写一次。
        # fs_fnirs 在此表示绘图点率，而不是未来光学板的物理采样率。
        self.win_s = 10.0; self.fs_eeg = 500.0; self.fs_fnirs = 500.0
        # 采样率初值，接上设备后由实测帧率覆盖
        self.fs_measured = 0.0
        self._fs_snapped = False
        # 光强换算系数只从 derive.FnirsOpticalParams 取一次；文献/定标值到了只改那一处。
        self.lux_per_lsb = float(_derive_default_params().lux_per_lsb)
        self._last_od = None     # 最近一帧的 20 路 ΔOD, 有则写入 CSV 的 OD 列
        self._last_hbt = None    # 最近一帧的 10 路 ΔHbT (文档 §1.3)
        self._last_sto2 = None   # 最近一帧的 10 路变化量氧合占比(非绝对 StO₂)
        self._last_mask = None   # 最近一帧的 10 路质控有效位, 异常通道 CSV 写空
        # 前 n_baseline 帧建基线 I0，之后逐帧做 MBLL
        self.n_baseline = 200          # 200 帧 @100Hz = 2 s 基线窗
        self._base_buf = []            # 基线期收集的原始行
        self._base_rows = None         # 建好的 I0, 形状 (20,)
        self.n_mbll_singular = 0       # 矩阵奇异被置 NaN 的次数(现场坏道计数)
        self.n_mbll_masked = 0         # 质控判异常、按文档 §1.5 不参与反解的次数
        self.extra_cols = False  # 是否写 S8 姿态/S9 指标扩展列(默认不写)
        self.eeg_scale = 100.0; self.fnirs_scale = 0.5
        # 轴单位: 二进制链路是折算后的 μV / mM; 文本链路的单位还没确认, 只能标"原始值"
        self.eeg_unit = "μV"
        self.fnirs_unit = "mM"
        self.fnirs_version = 0  # fw100: 1=暗场通道，2=光学 S-D 槽
        self._fw_fnirs_latest = np.full((10, 2), np.nan)
        # 自动改范围(时间轴滚动+量程)的开关。用户一旦手动缩放就关掉,
        # 否则每 40ms 被 setXRange/setYRange 抢回去, "支持缩放"等于没有。
        self._follow = True
        # 时间轴两种画法:
        #   累计(默认) —— 横轴从 0 起一路加: 0~10、12~22、…，刻度能读出"采了多久"；
        #   固定窗口  —— 永远是 0~win，只是波形往左滚，刻度恒定不动。
        # 之前只有固定窗口: 缓冲区正好一个窗长，每帧挤掉最早一帧再从 0 重排，
        # 所以现场看到"时间一直是 0 到 10"，怀疑程序没在走。
        self._abs_clock = True
        self._x_left = 0.0            # 当前窗口左边界(秒)；累计模式下随样本数增长
        self._zoom_src = 0            # 用户最近缩放的那张图(0~13，-1=单通道大图)
        self._n_eeg = 0               # 本次数据源累计收到的 EEG 样本数(绝对时间 = 它/fs)
        self._n_fn = 0                # fNIRS 侧计数(解耦后各走各的)
        self.ch_enable = [True]*self.n_total
        # 每个通道是否真的收到过数据。文本链路只有 1 列时, 其余通道是补 0 的,
        # 若照画就会得到一条贴 0 的假平线, 现场会当成"信号一直是 0"。
        self._ch_ok = [False]*self.n_total
        self._t_rx = None            # 最近一次收到帧的时刻, 用于判断"停发"
        # CSV 的 timestamp_s 必须是**采样时刻**，不是写盘挂钟。
        # 40 ms 定时器一拍取多帧时, 写盘时刻会挤在 1 ms 内, 采样率反推错 2~4.5 倍。
        # 优先用设备时间戳；没有设备时间就用"帧序号 / 当前采样率"合成等间隔时间。
        self._t_synth = 0.0
        self._t_sampling = 0.0
        self._t_sampling0 = None     # 本次记录的第 1 帧采样时刻, 写 CSV 时归零
        cw = QtWidgets.QWidget(); self.setCentralWidget(cw); lay = QtWidgets.QVBoxLayout(cw)
        bar = QtWidgets.QHBoxLayout()
        self.btn_pause = QtWidgets.QPushButton("暂停"); self.btn_pause.setCheckable(True)
        self.btn_pause.toggled.connect(lambda v: setattr(self, "paused", v))
        self.cmb_fnirs = QtWidgets.QComboBox(); self.cmb_fnirs.addItems(["ΔHbO2", "ΔHbR"])
        self.cmb_fnirs.setToolTip(
            "ΔHbO2=带氧血红蛋白(脑活动↑)，ΔHbR=脱氧血红蛋白(脑活动↓)。\n"
            "两者是两束光(760/850nm)经 MBLL 解出的浓度变化量，单位 µM，\n"
            "Δ 表示相对基线的变化；切换时整条历史曲线一起换。")
        self.cmb_fnirs.currentIndexChanged.connect(self._set_fnirs_metric)
        # 时间窗口档位：上限 30s 与环形缓冲容量一致（buf_eeg/fs_eeg = 30）。
        # 再大的档横轴会画出来但波形已被覆盖成空白，现场会当成没数据。
        self.WIN_CHOICES = (1.0, 2.0, 3.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0)
        self.cmb_win = QtWidgets.QComboBox()
        self.cmb_win.addItems([f"{v:g}s" for v in self.WIN_CHOICES])
        self.cmb_win.setCurrentIndex(self.WIN_CHOICES.index(10.0))
        self.cmb_win.currentIndexChanged.connect(self._set_win)
        # 默认自动量程：真机增益未知时固定量程会把波形画到框外
        self.eeg_scale_auto = True
        self.cmb_scale = QtWidgets.QComboBox()
        self.cmb_scale.addItems(["幅度: 自动(按实测峰值)", "幅度: EEG±100μV",
                                 "幅度: EEG±200μV", "幅度: EEG±500μV"])
        self.cmb_scale.setCurrentIndex(0)
        self.cmb_scale.currentIndexChanged.connect(self._set_eeg_scale)
        # S1 要求"幅度标尺"覆盖两类曲线: 只给 EEG 调、fNIRS 固定 ±0.5 mM 就不算可调
        self.cmb_fscale = QtWidgets.QComboBox()
        self.cmb_fscale.addItems(["幅度: fNIRS±0.2mM", "幅度: fNIRS±0.5mM",
                                  "幅度: fNIRS±1mM", "幅度: fNIRS±2mM"])
        self.cmb_fscale.setCurrentIndex(1)
        self.cmb_fscale.currentIndexChanged.connect(
            lambda i: setattr(self, "fnirs_scale", (0.2, 0.5, 1.0, 2.0)[i]))
        # 缩放后必须能停在当前视野: 否则 40ms 一次的重绘会把手动缩放抢回去
        self.btn_follow = QtWidgets.QPushButton("跟随最新")
        self.btn_follow.setCheckable(True); self.btn_follow.setChecked(True)
        self.btn_follow.setToolTip("取消勾选后不再自动改时间轴与量程，方便放大看细节")
        self.btn_follow.toggled.connect(self._set_follow)
        # 14 图并排时每张只有几十像素高，波形看起来是一条平线。单通道放大是主路径，
        # 双击子图也能触发；这个按钮是给"不知道能双击"的人留的入口。
        self.btn_solo = QtWidgets.QPushButton("⤢ 单通道放大")
        self.btn_solo.setToolTip("放大当前点中的子图（也可直接双击子图，Esc 还原）")
        self.btn_solo.clicked.connect(self._toggle_solo)
        # 时间轴模式：默认累计（刻度会涨），可切回固定窗口对照合同里的 0~win
        self.btn_clock = QtWidgets.QPushButton("时间轴: 累计")
        self.btn_clock.setCheckable(True); self.btn_clock.setChecked(True)
        self.btn_clock.setToolTip(
            "勾选：横轴累加（0~10、12~22…），能看出已经采了多久；\n"
            "取消：固定 0~时间窗，刻度恒为 0~10，只有波形滚动")
        self.btn_clock.toggled.connect(self._set_clock)
        self.lbl = QtWidgets.QLabel("丢包率 0.0% | 电量 --% | seq连续 OK")
        # S3: 各通道信号质量的实时显示(此前只在内存里算, 从没画到界面上)
        self.lbl_quality = QtWidgets.QLabel("通道质量 --")
        self.lbl_quality.setStyleSheet("color:#888")
        self.btn_verify = QtWidgets.QPushButton("离线自检")
        self.btn_verify.clicked.connect(self._open_verify)
        for w in (self.btn_pause, self.cmb_fnirs, self.cmb_win, self.cmb_scale,
                  self.cmb_fscale, self.btn_follow, self.btn_clock, self.btn_solo,
                  self.btn_verify, self.lbl):
            bar.addWidget(w)
        lay.addLayout(bar)
        # 主界面顶部信息区：第一项沿用 14 宫格，第二项显示同轴叠加波形。
        view_row = QtWidgets.QHBoxLayout()
        self.view_tabs = QtWidgets.QTabBar()
        self.view_tabs.addTab("14 通道分图")
        self.view_tabs.addTab("多通道叠加")
        self.view_tabs.setExpanding(False)
        view_row.addWidget(self.view_tabs)
        self.btn_overlay_zoom = QtWidgets.QPushButton("Zoom · 适配当前波形")
        self.btn_overlay_zoom.setToolTip("按当前时间窗内已勾选的曲线调整左右纵轴；调整后保持固定")
        self.btn_overlay_zoom.clicked.connect(self._zoom_overlay)
        self.btn_overlay_reset = QtWidgets.QPushButton("恢复默认比例")
        self.btn_overlay_reset.clicked.connect(self._reset_overlay_scale)
        view_row.addWidget(self.btn_overlay_zoom)
        view_row.addWidget(self.btn_overlay_reset)
        view_row.addStretch(1)
        lay.addLayout(view_row)
        self.btn_overlay_zoom.hide()
        self.btn_overlay_reset.hide()
        qrow = QtWidgets.QHBoxLayout()
        qrow.addWidget(self.lbl_quality, 1)
        lay.addLayout(qrow)
        # fNIRS 图例常驻界面：现场不用翻文档就能看懂切换的是什么。
        # Δ 是相对基线变化量、单位 µM —— 这两点最容易现场误读成电压/绝对值。
        self.lbl_fnirs_legend = QtWidgets.QLabel(
            "fNIRS: ΔHbO2=带氧血红蛋白(脑活动↑) | ΔHbR=脱氧血红蛋白(脑活动↓) "
            "| 760/850nm 双波长解算 | Δ=相对基线变化量, 单位 µM")
        self.lbl_fnirs_legend.setStyleSheet("color:#666")
        self.lbl_fnirs_legend.setToolTip(
            "ΔHbO2 与 ΔHbR 由两束光(760/850nm)经 MBLL 联立方程解出，\n"
            "两者相加是 ΔHbT(总血红蛋白)。切换下拉框时整条历史曲线一起换。")
        lay.addWidget(self.lbl_fnirs_legend)
        rec = QtWidgets.QHBoxLayout()
        self.btn_record = QtWidgets.QPushButton("● 开始记录"); self.btn_record.setCheckable(True)
        self.btn_record.toggled.connect(self._toggle_record)
        self.btn_export = QtWidgets.QPushButton("导出EDF/SNIRF/XDF"); self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self._export_all)
        self.lbl_store = QtWidgets.QLabel("未记录")
        for w in (self.btn_record, self.btn_export, self.lbl_store): rec.addWidget(w)
        lay.addLayout(rec)
        # S1 通道开关
        self.chk = []
        ch_labels = self.eeg_names + [f"fNIRS{i+1}" for i in range(N_FNIRS)]
        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        sw_inner = QtWidgets.QWidget(); sw_box = QtWidgets.QHBoxLayout(sw_inner)
        sw_box.addWidget(QtWidgets.QLabel("通道开关:"))
        for i, lab in enumerate(ch_labels):
            c = QtWidgets.QCheckBox(lab); c.setChecked(True)
            c.toggled.connect(lambda v, k=i: self._toggle_ch(k, v))
            self.chk.append(c); sw_box.addWidget(c)
        scroll.setWidget(sw_inner); scroll.setMaximumHeight(60)
        lay.addWidget(scroll)
        self.ch_scroll = scroll  # S1: 主界面嵌入时隐藏内嵌通道条, 只留外部通道开关
        self.plots = []; self.curves = []
        self._tips = []          # 空子图上的灰字提示(等待数据/本链路未发送该通道)
        self._ytick_cache = {}   # 网格小图"上下限两个读数"的上次文本，避免每帧重画
        self._ylbl_hi = []       # 网格图里"纵轴上限"的读数
        self._ylbl_lo = []       # 网格图里"纵轴下限"的读数
        names = ch_labels
        self._names = names
        # 兼容旧测试/外部脚本：网格永远 2 列，但每页只有 4 路。
        self._ncols = PAGE_COLS
        self._nrows = 2
        #: 单通道放大看的是哪一路；None = 回到分页网格
        self._solo = None
        #: 最近一次点到的子图，供工具栏按钮"放大当前"用
        self._focus_idx = 0
        # 网格页内部再分 4 张一页：默认 ECG1~ECG4 铺满一屏，剩下 fNIRS 翻页看。
        # 单通道放大仍在最外层 QStackedWidget：放大/缩小只切页，不动任何子图布局。
        self._stack = QtWidgets.QStackedWidget()
        lay.addWidget(self._stack, 1)
        self._grid_page = QtWidgets.QWidget()
        gp = QtWidgets.QVBoxLayout(self._grid_page)
        gp.setContentsMargins(0, 0, 0, 0); gp.setSpacing(2)
        nav = QtWidgets.QHBoxLayout()
        self._btn_prev_page = QtWidgets.QPushButton("◀ 上一页")
        self._lbl_page = QtWidgets.QLabel("第 1/1 页")
        self._lbl_page.setAlignment(QtCore.Qt.AlignHCenter)
        self._btn_next_page = QtWidgets.QPushButton("下一页 ▶")
        tip = "滚轮 / 左右滑 / ←→ 翻页；Ctrl+滚轮缩放时间轴"
        self._btn_prev_page.setToolTip(tip); self._btn_next_page.setToolTip(tip)
        self._btn_prev_page.clicked.connect(lambda: self._turn_page(-1))
        self._btn_next_page.clicked.connect(lambda: self._turn_page(1))
        for w in (self._btn_prev_page, self._lbl_page, self._btn_next_page):
            nav.addWidget(w)
        gp.addLayout(nav)
        self._page_stack = QtWidgets.QStackedWidget()
        gp.addWidget(self._page_stack, 1)
        self._pages = []
        self._page_layouts = []
        n_pages = (len(names) + PAGE_SIZE - 1) // PAGE_SIZE
        for pi in range(n_pages):
            page = QtWidgets.QWidget()
            gl = QtWidgets.QGridLayout(page)
            gl.setContentsMargins(0, 0, 0, 0)
            gl.setHorizontalSpacing(2); gl.setVerticalSpacing(2)
            cnt = min(PAGE_SIZE, len(names) - pi * PAGE_SIZE)
            gl.setRowStretch(0, 1)
            gl.setRowStretch(1, 1 if cnt > PAGE_COLS else 0)
            gl.setColumnStretch(0, 1); gl.setColumnStretch(1, 1)
            self._page_stack.addWidget(page)
            self._pages.append(page)
            self._page_layouts.append(gl)
        self._gl = self._page_layouts[0] if self._page_layouts else None
        self._page = 0
        self._plot_viewboxes = []
        self._drag_start = None
        self._drag_obj = None
        self._stack.addWidget(self._grid_page)
        for r, n in enumerate(names):
            p = pg.PlotWidget(title=n, axisItems={'left': _RawValueAxis('left')})
            p.showGrid(x=True, y=True)
            # 只让鼠标改横轴：纵轴永远由程序按可见数据贴合（自动档）或按标尺固定档。
            # 之前 x/y 都开放，滚轮一下就把 Y 也缩了/拖了，数据跑出窗口后屏幕上什么都没有，
            # 现场以为是"放大以后数据消失了"。横轴缩放才是这里真正需要的操作。
            p.setMouseEnabled(x=True, y=False)
            # 合同 S1 要求「时间轴统一」: 横轴必须是同一个绝对量(秒), 不能各图各的
            p.setLabel('bottom', '时间', units='s')
            self._set_left_label(p, r, short=True)
            # 纵轴要能读到数，但 14 图只有二三十像素高：六位刻度交给坐标轴画，
            # pyqtgraph 会把超宽的文字整段裁掉（实测轴宽 35px、字宽 30px 就一个都看不见）。
            # 所以网格里用两个小标签直接贴在绘图区左上/左下写上下限，见 _mark_grid_ticks。
            ax_l = p.getAxis('left')
            ax_l.setStyle(showValues=False)
            p.setXRange(0, self.win_s, padding=0)
            # 带上序号: 之后要按"用户缩的是哪一张"取数据并统一到 14 张图
            p.getViewBox().sigRangeChangedManually.connect(
                lambda *_, k=r: self._on_manual_zoom(k))
            c = p.plot(pen="y")
            # 空着的子图不能只是一片黑: 现场分不清"这条链路没送这个通道"和"程序没跑"
            tip = pg.TextItem("等待数据…", color="#909090", anchor=(0.5, 0.5))
            tip.setPos(self.win_s / 2.0, 0.0); tip.hide()
            p.addItem(tip); self._tips.append(tip)
            # 纵轴上下限读数：用 QLabel 贴在小图里。
            # 坐标轴画不了（轴宽 35px、六位字要 45px，会被整段裁掉）；TextItem 也放不下
            # （格子只有三十几像素高，文字锚点贴边就被裁）。QLabel 叠在绘图区上方，稳定可见。
            for top in (True, False):
                lb = QtWidgets.QLabel("", p)
                lb.setFixedSize(58, 13)
                lb.setStyleSheet("color:#c8c8c8; font-size:7pt; background:rgba(0,0,0,110);")
                lb.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
                lb.hide(); lb.raise_()
                (self._ylbl_hi if top else self._ylbl_lo).append(lb)
            # 14 图并排时每张只有几十像素高，起伏看不出来。双击任何一张 -> 单独放大
            p.scene().sigMouseClicked.connect(lambda ev, k=r: self._on_plot_click(ev, k))
            pi, k = divmod(r, PAGE_SIZE)
            li, lj = divmod(k, PAGE_COLS)
            self._page_layouts[pi].addWidget(p, li, lj)
            self.plots.append(p); self.curves.append(c)
            self._plot_viewboxes.append(p.getViewBox())
            p.installEventFilter(self)
            try:
                p.getViewBox().installEventFilter(self)
                p.getViewBox().grabGesture(QtCore.Qt.SwipeGesture)
            except Exception:  # noqa: BLE001 - 某些 Qt 构建没有手势支持
                pass

        self._show_page(0)

        # ---- 单通道大图页 ----
        # 独立的一张 PlotWidget, 不搬动网格里的子图: 放大/缩小只是切页, 几何状态零副作用
        self._solo_page = QtWidgets.QWidget()
        sl = QtWidgets.QVBoxLayout(self._solo_page); sl.setContentsMargins(0, 0, 0, 0)
        head = QtWidgets.QHBoxLayout()
        self._lbl_solo = QtWidgets.QLabel("单通道放大")
        self._lbl_solo.setStyleSheet("font-weight:bold")
        self._btn_back = QtWidgets.QPushButton("← 返回 4 图")
        self._btn_back.clicked.connect(self._restore_grid)
        head.addWidget(self._lbl_solo, 1); head.addWidget(self._btn_back)
        sl.addLayout(head)
        self._big = pg.PlotWidget(axisItems={'left': _RawValueAxis('left')})
        self._big.showGrid(x=True, y=True)
        self._big.setMouseEnabled(x=True, y=False)   # 纵轴恒由数据贴合，见网格页注释
        self._big.setLabel('bottom', '时间', units='s')
        self._big.setXRange(0, self.win_s, padding=0)
        self._big.getViewBox().sigRangeChangedManually.connect(
            lambda *_: self._on_manual_zoom(-1))
        big_font = QtGui.QFont(); big_font.setPointSize(10)
        for ax in ('bottom', 'left'):
            self._big.getAxis(ax).setStyle(tickFont=big_font)
        # 六位刻度（-104300）在默认宽度里会被裁掉首尾，放大页专门留够空间、写全原文数字
        self._big.getAxis('left').setStyle(tickTextWidth=86)
        self._big.getAxis('left').setStyle(showValues=True)
        self._style_axes(self._big)
        self._big_curve = self._big.plot(pen="y")
        self._big_tip = pg.TextItem("等待数据…", color="#909090", anchor=(0.5, 0.5))
        self._big_tip.setPos(self.win_s / 2.0, 0.0); self._big_tip.hide()
        self._big.addItem(self._big_tip)
        self._big.scene().sigMouseClicked.connect(
            lambda ev: self._on_plot_click(ev, 0 if self._solo is None else self._solo))
        sl.addWidget(self._big, 1)
        self._stack.addWidget(self._solo_page)

        # 叠加页：左轴只承载 EEG，右轴单独承载 fNIRS，横轴由两个 ViewBox 共享。
        self._overlay_page = QtWidgets.QWidget()
        ol = QtWidgets.QVBoxLayout(self._overlay_page)
        ol.setContentsMargins(0, 0, 0, 0)
        self._overlay = pg.PlotWidget(axisItems={
            'left': _RawValueAxis('left'), 'right': _RawValueAxis('right')})
        self._overlay.showGrid(x=True, y=True)
        self._overlay.setMouseEnabled(x=True, y=False)
        self._overlay.setLabel('bottom', '时间', units='s')
        self._overlay.setLabel('left', 'EEG', units=self.eeg_unit)
        self._overlay.setLabel('right', 'fNIRS ΔHbO2', units=self.fnirs_unit)
        self._style_axes(self._overlay)
        right_axis = self._overlay.getAxis('right')
        right_axis.enableAutoSIPrefix(False)
        right_axis.autoSIPrefixScale = 1.0
        right_axis.setPen(pg.mkPen('#c8c8c8'))
        right_axis.setTextPen(pg.mkPen('#e6e6e6'))
        self._overlay.setXRange(0, self.win_s, padding=0)
        self._overlay.getViewBox().sigRangeChangedManually.connect(
            lambda *_: self._on_manual_zoom(-2))
        self._overlay.showAxis('right')
        self._overlay_fnirs_vb = pg.ViewBox(enableMenu=False)
        self._overlay.scene().addItem(self._overlay_fnirs_vb)
        self._overlay.getAxis('right').linkToView(self._overlay_fnirs_vb)
        self._overlay_fnirs_vb.setXLink(self._overlay.getViewBox())
        self._overlay_fnirs_vb.setMouseEnabled(x=False, y=False)
        self._overlay.getViewBox().sigResized.connect(self._sync_overlay_viewbox)
        self._overlay_eeg_range = (-100.0, 100.0)
        self._overlay_fnirs_range = (-0.5, 0.5)
        self._overlay.getViewBox().enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
        self._overlay_fnirs_vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
        self._apply_overlay_scale()
        self._sync_overlay_viewbox()
        self._overlay_legend = self._overlay.addLegend(
            offset=(12, 12), colCount=7, labelTextSize='8pt',
            labelTextColor='#e6e6e6', brush=pg.mkBrush(0, 0, 0, 160),
            pen=pg.mkPen('#606060'))
        colors = (
            '#f5d442', '#40c9ff', '#ff6b87', '#a4e05e',
            '#c47bff', '#ff9f43', '#66e0c2', '#ff73ca', '#8aa5ff',
            '#e8c76a', '#6ad5ff', '#c8e86b', '#ff7a65', '#a9a0ff',
        )
        self._overlay_curves = []
        for i, name in enumerate(names):
            curve = pg.PlotDataItem(pen=pg.mkPen(colors[i], width=1.5))
            if i < self.n_eeg:
                self._overlay.addItem(curve)
            else:
                self._overlay_fnirs_vb.addItem(curve)
            self._overlay_legend.addItem(curve, name)
            self._overlay_curves.append(curve)
        self._overlay_tip = pg.TextItem("等待数据…", color="#b0b0b0", anchor=(0.5, 0.5))
        self._overlay_tip.setPos(self.win_s / 2.0, 0.0)
        self._overlay.addItem(self._overlay_tip)
        ol.addWidget(self._overlay, 1)
        self._stack.addWidget(self._overlay_page)
        self._stack.setCurrentWidget(self._grid_page)
        self.view_tabs.currentChanged.connect(self._set_view_tab)

        # 键盘 Esc 还原 —— 仅用于「本窗口独立 show」的调试路径。
        # 主界面(ClassicMainWindow)只嵌 centralWidget, 本窗口从不 show,
        # 挂在这里的快捷键不在活动窗口链里; 真正接收按键的那层由主窗口自己装
        # (见 main_window.py 构造末尾), 不要在这里用 WidgetWithChildrenShortcut
        # 假装覆盖嵌入式场景 —— 实测不触发, 界面却写着"按 Esc 返回"。
        # 用 WindowShortcut: 独立 show 时 self 就是顶层窗口, 能触发。
        try:
            sc = QtWidgets.QShortcut(QtGui.QKeySequence("Esc"), self)
            sc.setContext(QtCore.Qt.WindowShortcut)
            sc.activated.connect(self._restore_grid)
            self._esc = sc
        except Exception:  # noqa: BLE001
            self._esc = None
        self.t0 = time.time()
        self.buf_eeg = np.zeros((self.n_eeg, int(self.fs_eeg*30)))
        # ΔHbO2/ΔHbR 各存一份：切换时整条历史曲线跟着换，不用等新帧慢慢覆盖。
        # 旧版只有 buf_fn 一份，写入时选当时指标，切完历史段还是旧的那条。
        # push_frame 每帧都写浓度曲线，容量必须与 EEG 一样覆盖完整 30 秒。
        # 否则 3000 点按 500 帧/秒写入只剩 6 秒，10 秒窗左侧会空白。
        # 两条浓度缓存按样本序号环形写入，避免 500 SPS 时每帧搬移整段历史。
        self.buf_hbo = np.zeros((10, int(self.fs_eeg*30)))
        self.buf_hbr = np.zeros((10, int(self.fs_eeg*30)))
        # 原始光强只用于最近 50 帧的质控，保留较短缓存即可。
        self.buf_raw = np.zeros((20, 3000))
        self.battery_pct = -1.0
        self.eeg_quality = ["--"]*self.n_eeg; self.fnirs_quality = ["--"]*10
        self._fnirs_levels = []          # S3: 每通道(最小,中位,最大)光强，暗噪声基线显示用
        self._quality_error = ""         # 计算失败必须可见，不许静默 pass
        self.timer = QtCore.QTimer(); self.timer.timeout.connect(self._tick); self.timer.start(40)
        self._seq_last = -1; self._lost = 0; self._recv = 0; self._sim_seq = 0
        self._writer = None; self._csv_path = None; self._n_saved = 0
        self._t0 = time.time()
        self._q_tick = 0
        self._st_tick = 0
        # 提前建默认值：push_frame 会读它，建晚了首帧就抛 AttributeError
        self._metrics = {"brain_active": 50.0, "brain_relax": 50.0, "focus": 50.0, "hr_bpm": 0.0}

    @property
    def buf_fn(self):
        """当前显示的 fNIRS 缓冲：随 ΔHbO2/ΔHbR 切换指向对应那份。

        保留这个只读属性是为兼容外部读取（测试、diag_zoom）。写入必须走
        push_frame 的两份 roll，清零走 _clear_buffers，不要对这个属性做
        np.roll 后回写 —— 那样只会动其中一份，另一份不同步。
        """
        return self.buf_hbo if self.show_hbo else self.buf_hbr

    def _set_fnirs_metric(self, i: int):
        """切换 ΔHbO2/ΔHbR：整条历史曲线立即换成另一份缓冲，不等新帧覆盖。"""
        if self.cmb_fnirs.currentIndex() != i:
            self.cmb_fnirs.blockSignals(True)
            self.cmb_fnirs.setCurrentIndex(i)
            self.cmb_fnirs.blockSignals(False)
        show_hbo = i == 0
        if self.show_hbo == show_hbo:
            return
        self.show_hbo = show_hbo
        self._apply_axis_labels()
        self._tick(force=True)   # 暂停时 force 也只重绘、不取帧（沿用既有 frozen 语义）

    def _toggle_ch(self, k: int, v: bool):
        self.ch_enable[k] = v
        self.plots[k].setVisible(v)
        self._overlay_curves[k].setVisible(v)
        if self._stack.currentWidget() is self._overlay_page:
            self._tick(force=True)

    # ---- 4 路一页的分页导航 ----
    def _page_title(self, idx: int) -> str:
        a = idx * PAGE_SIZE
        b = min(a + PAGE_SIZE, self.n_total)
        return f"第 {idx + 1}/{len(self._pages)} 页 · {self._names[a]}~{self._names[b - 1]}"

    def _show_page(self, idx: int):
        """切到指定页。页内 2×2 布局永不重排，放大还原才可逆。"""
        if not self._pages:
            return
        self._page = max(0, min(int(idx), len(self._pages) - 1))
        self._page_stack.setCurrentIndex(self._page)
        self._btn_prev_page.setEnabled(self._page > 0)
        self._btn_next_page.setEnabled(self._page < len(self._pages) - 1)
        self._lbl_page.setText(self._page_title(self._page))
        # 工具栏"放大当前"应对准本页第一路，而不是已翻走那一页的旧焦点。
        self._focus_idx = min(self._page * PAGE_SIZE, len(self.plots) - 1)

    def _turn_page(self, step: int):
        """滚轮 / 左右滑 / 按钮翻页；单通道放大态不改页。"""
        if getattr(self, "_solo", None) is not None:
            return
        self._show_page(self._page + int(step))

    def eventFilter(self, obj, ev):
        """网格页上：普通滚轮/左右滑翻页，Ctrl+滚轮仍交给 pyqtgraph 缩放时间轴。"""
        watch = obj in self.plots or obj in getattr(self, "_plot_viewboxes", [])
        if getattr(self, "_solo", None) is not None or not watch:
            return super().eventFilter(obj, ev)
        t = ev.type()
        if t == QtCore.QEvent.Wheel:
            if ev.modifiers() & QtCore.Qt.ControlModifier:
                return super().eventFilter(obj, ev)
            d = ev.angleDelta()
            if abs(d.x()) > abs(d.y()):
                step = -1 if d.x() > 0 else (1 if d.x() < 0 else 0)
            else:
                step = -1 if d.y() > 0 else (1 if d.y() < 0 else 0)
            if step:
                self._turn_page(step)
                ev.accept()
                return True
        elif t == QtCore.QEvent.MouseButtonPress and ev.button() == QtCore.Qt.LeftButton:
            self._drag_start = ev.pos()
            self._drag_obj = obj
        elif t == QtCore.QEvent.MouseButtonRelease and self._drag_start is not None \
                and obj is self._drag_obj:
            dx = ev.pos().x() - self._drag_start.x()
            dy = ev.pos().y() - self._drag_start.y()
            self._drag_start = None
            self._drag_obj = None
            if abs(dx) > 70 and abs(dx) > abs(dy) * 1.5:
                self._turn_page(1 if dx < 0 else -1)
                ev.accept()
                return True
        elif t == QtCore.QEvent.Gesture:
            try:
                g = ev.gesture(QtCore.Qt.SwipeGesture)
                if g is not None:
                    self._turn_page(1 if g.direction() == QtCore.Qt.SwipeLeft else -1)
                    ev.accept()
                    return True
            except Exception:  # noqa: BLE001 - 无触摸/无手势支持的构建直接忽略
                pass
        return super().eventFilter(obj, ev)

    def _set_view_tab(self, index: int):
        """切换分图/叠加图，保留当前时间范围和采集、暂停状态。"""
        if self._solo is not None:
            self._restore_grid()
        source = (self._overlay if self._stack.currentWidget() is self._overlay_page
                  else self.plots[self._page * PAGE_SIZE])
        lo, hi = self._view_bounds(source)
        overlay = index == 1
        if overlay:
            self._overlay.setXRange(lo, hi, padding=0)
            self._stack.setCurrentWidget(self._overlay_page)
            self._zoom_src = -2
        else:
            for plot in self.plots:
                plot.setXRange(lo, hi, padding=0)
            self._stack.setCurrentWidget(self._grid_page)
            self._zoom_src = 0
        self.btn_solo.setEnabled(not overlay)
        self.btn_overlay_zoom.setVisible(overlay)
        self.btn_overlay_reset.setVisible(overlay)
        if self._follow:
            self._recalc_x_left()
            self._reset_x_range()
        self._tick(force=True)

    def _sync_overlay_viewbox(self):
        left = self._overlay.getViewBox()
        # 两个 ViewBox 的 boundingRect 都额外扩 0.5px，直接用 sceneBoundingRect
        # 会让右侧宽 1px，时间轴末端产生几个毫秒的偏差。
        self._overlay_fnirs_vb.setGeometry(left.geometry())
        self._overlay_fnirs_vb.linkedViewChanged(left, self._overlay_fnirs_vb.XAxis)

    def _apply_overlay_scale(self):
        """仅在初始化或用户点 Zoom/重置时修改双纵轴，实时刷新不自动缩放。"""
        self._overlay.setYRange(*self._overlay_eeg_range, padding=0)
        self._overlay_fnirs_vb.setYRange(*self._overlay_fnirs_range, padding=0)

    def _reset_overlay_scale(self):
        self._overlay_eeg_range = (-100.0, 100.0)
        self._overlay_fnirs_range = (-0.5, 0.5)
        self._apply_overlay_scale()

    @staticmethod
    def _fit_overlay_range(values, minimum_span: float):
        data = np.asarray(values, dtype=float).reshape(-1)
        data = data[np.isfinite(data)]
        if data.size == 0:
            return None
        low, high = np.percentile(data, (1.0, 99.0))
        mid = float((low + high) / 2.0)
        half = max(float((high - low) * 0.55), minimum_span / 2.0)
        return (mid - half, mid + half)

    def _zoom_overlay(self):
        """一次性适配当前可见时间窗内的曲线，之后固定左右 Y 范围。"""
        if self._recv < 2:
            return
        lo, hi = self._view_bounds(self._overlay)
        for buf, fs, start, end, minimum, attr in (
            (self.buf_eeg, self.fs_eeg, 0, self.n_eeg, 20.0, '_overlay_eeg_range'),
            (self.buf_fn, self.fs_fnirs, self.n_eeg, self.n_total, 0.02,
             '_overlay_fnirs_range'),
        ):
            first = max(0, int(math.floor(lo * fs)))
            count = max(2, int(math.ceil((hi - lo) * fs)) + 2)
            _, segment = self._take(buf, first, count, fs, int(self._recv))
            if segment is None:
                continue
            rows = [segment[i - start] for i in range(start, end)
                    if self.ch_enable[i] and self._ch_ok[i]]
            if rows:
                fitted = self._fit_overlay_range(np.concatenate(rows), minimum)
                if fitted is not None:
                    setattr(self, attr, fitted)
        self._apply_overlay_scale()

    def _paint_overlay(self, xe, seg_e, xf, seg_f, why=None):
        visible = 0
        for i, curve in enumerate(self._overlay_curves):
            if not (self.ch_enable[i] and self._ch_ok[i]):
                curve.setData([], [])
            elif i < self.n_eeg and seg_e is not None:
                curve.setData(xe, seg_e[i])
                visible += 1
            elif i >= self.n_eeg and seg_f is not None:
                curve.setData(xf, seg_f[i - self.n_eeg])
                visible += 1
            else:
                curve.setData([], [])
        if visible:
            self._overlay_tip.hide()
        else:
            self._overlay_tip.setText(
                why or ('等待数据…' if self._recv == 0 else '当前时间窗无可见通道'))
            lo, hi = self._view_bounds(self._overlay)
            self._overlay_tip.setPos((lo + hi) / 2.0, 0.0)
            self._overlay_tip.show()
    # ---- 单通道放大 / 还原 ----
    def _title_of(self, i: int) -> str:
        return self._names[i] if i < len(self._names) else f"CH{i+1}"

    def _solo_header(self, i: int) -> str:
        """放大页标题必须写单位：网格只标"EEG"，换页后看不出数值有没有被缩放。"""
        unit = self.eeg_unit if i < self.n_eeg else self.fnirs_unit
        return (f"{self._title_of(i)}　单通道放大 · 纵轴单位：{unit}"
                "（双击波形 / 按 Esc / 点右上角返回）")

    def _set_left_label(self, plot, i: int, short: bool = False):
        """纵轴单位跟着通道与链路走：fNIRS 是 mM，文本链路的 EEG 是"单位待定"。

        short=True 给 14 图网格用：格子只有二三十像素高，长的旋转标题会被裁成
        "单位."这种半截字，刻度数字也没地方放。网格里只写通道类型，完整单位在放大页看。
        """
        if i < self.n_eeg:
            if short:
                plot.setLabel('left', 'EEG')
            else:
                plot.setLabel('left', 'EEG', units=self.eeg_unit)
        else:
            if short:
                plot.setLabel('left', 'fNIRS 原始码' if self.fnirs_version else 'ΔHbO2·ΔHbR')
            else:
                plot.setLabel('left', '差分码 波长1/2' if self.fnirs_version else 'ΔHbO2/ΔHbR',
                              units=self.fnirs_unit)
        self._style_axes(plot)

    @staticmethod
    def _style_axes(plot):
        """轴颜色 + 关掉 pyqtgraph 的 SI 前缀缩写。

        颜色：默认前景色是配置项 'd'（解析成 #969696 深灰），黑底上对比度很低。
        显式给浅灰，不依赖 pyqtgraph 的全局配置。

        注意：离屏渲染（QT_QPA_PLATFORM=offscreen，本机无字体）下 drawText 一个
        像素都不出，连按钮文字都看不见 —— 所以"截图里轴上没有数字"不能当作
        轴没画刻度的证据，验证刻度只能读 tickStrings / viewRange。

        SI 前缀：默认 autoSIPrefix=True 时，纵轴 -120300 会被画成 "-120"（内部乘
        0.001），而"(x1000)"这个乘数只在 labelUnits 为空时才拼进轴标签；我们给轴
        设了"原始值·单位待定"，于是乘数被静默丢掉 —— 现场看到的 -120 与终端里的
        -120362.52 差 1000 倍，直接判成"数据不对"。宁可刻度长一点，也不能改写数值。
        """
        for name in ('left', 'bottom'):
            try:
                ax = plot.getAxis(name)
                ax.enableAutoSIPrefix(False)
                # 关键：pyqtgraph 的 enableAutoSIPrefix(False) 只是把标志置 False，
                # 它自己仍会调 updateAutoSIPrefix()。若此刻 Y 量程已经是 -161437
                # 这种六位数，内部 autoSIPrefixScale 会被写成 0.001；之后
                # generateDrawSpecs 一律按 `值 × scale` 生成刻度文字，
                # 于是一放大就显示 -161.5 —— 正是现场"数字被藏了几位"。
                # 标志关了不等于系数是 1，必须一起按回去。
                ax.autoSIPrefixScale = 1.0
                ax.labelUnitPrefix = ''
                ax._updateLabel()       # 标签里的"(k原始值)"也要一并去掉
                # 轴线和刻度文字分开设：setPen 只管轴线/刻度，不保证管文字
                ax.setPen(pg.mkPen('#c8c8c8'))
                ax.setTextPen(pg.mkPen('#e6e6e6'))
            except Exception:  # noqa: BLE001
                pass

    def _on_plot_click(self, ev, i: int):
        """点一下记住"当前是哪一路"，双击则放大/还原。"""
        self._focus_idx = i
        if not ev.double():
            return
        if self._solo == i:
            self._restore_grid()
        else:
            self._solo_plot(i)

    def _toggle_solo(self):
        if self._solo is None:
            self._solo_plot(self._focus_idx)
        else:
            self._restore_grid()

    def _solo_plot(self, i: int):
        """放大第 i 路：切成大图页，保留原时间视野，只按这一路重新算量程。"""
        if not (0 <= i < len(self.plots)):
            return
        self._solo = i
        self._zoom_src = i if i < self.n_total else -1
        self._lbl_solo.setText(self._solo_header(i))
        self._set_left_label(self._big, i)
        self._big.setTitle("")
        # 放大只换页，不跳时间：网格里缩到哪一段，放大后就看哪一段。
        # 旧写法强制 follow=True + _reset_x_range()，把用户刚缩好的视野丢了，
        # 暂停后再放大就直接跳到最新一段（现场："暂停只显示一段时间"）。
        lo, hi = self._view_bounds(self.plots[i])
        self._big.setXRange(lo, hi, padding=0)
        self._stack.setCurrentWidget(self._solo_page)
        self.btn_solo.setText("⤢ 返回 4 图")
        # _follow / btn_follow 原样保留：手动缩放过就继续停在那一段
        self._tick(force=True)            # 立刻重画，不要等下一个 40ms 节拍

    def _restore_grid(self):
        """回到默认 4 图一页。时间视野与跟随状态都原样带回来。"""
        if self._solo is None:
            return
        lo, hi = self._view_bounds(self._big)
        self._zoom_src = self._solo if 0 <= self._solo < self.n_total else 0
        self._solo = None
        for p in self.plots:
            p.setXRange(lo, hi, padding=0)
        self._stack.setCurrentWidget(self._grid_page)
        self.btn_solo.setText("⤢ 单通道放大")
        self._tick(force=True)
    def _set_eeg_scale(self, i: int):
        """幅度标尺: 0=自动, 1~3=固定 ±100/±200/±500μV。"""
        self.eeg_scale_auto = (i == 0)
        if not self.eeg_scale_auto:
            self.eeg_scale = (100.0, 200.0, 500.0)[max(0, min(i - 1, 2))]
    # ---- 时间轴 / 跟随 ----
    def _set_win(self, i: int):
        """时间窗切换后立刻重设横轴，不然要等下一次重绘才对齐。"""
        if i < 0 or i >= len(self.WIN_CHOICES):
            return
        if self.cmb_win.currentIndex() != i:
            self.cmb_win.blockSignals(True)
            self.cmb_win.setCurrentIndex(i)
            self.cmb_win.blockSignals(False)
        self.win_s = self.WIN_CHOICES[i]
        self._recalc_x_left()
        self._reset_x_range()
        self._tick(force=True)
    def _set_clock(self, v: bool):
        """切换「累计时间 / 固定窗口」。切换后立刻重画，避免要等下一帧才对齐。"""
        self._abs_clock = bool(v)
        self.btn_clock.setText("时间轴: 累计" if self._abs_clock else "时间轴: 固定窗口")
        self._recalc_x_left()
        self._reset_x_range()
        self._tick(force=True)
    def _recalc_x_left(self):
        """算出当前窗口左边界(秒)。累计模式下 = 已收到的时长 − 窗长；固定窗口恒为 0。

        为什么不取 time.time()-t0 而当"样本数 / 采样率"：文件回放会把 10 s 数据
        在 0.1 s 内灌完，用挂钟会把横轴算成 0.1 s，整段波形挤在左边缘。样本序号
        在实机与回放里都等于"第几个采样点"，两种链路都自洽。
        """
        if not self._abs_clock:
            self._x_left = 0.0
            return
        fe = max(self.fs_eeg, 1e-6)
        self._x_left = max(0.0, self._n_eeg / fe - self.win_s)
    def _reset_x_range(self):
        """所有图（含大图）统一横轴：左边界 ~ 左边界+窗长，右端是最新一帧。

        「统一」是合同 S1 的硬要求：14 张图必须是同一把尺，不能各画各的。
        """
        lo = float(self._x_left); hi = lo + self.win_s
        for i, p in enumerate(self.plots):
            p.setXRange(lo, hi, padding=0)
            if i < len(self._tips):
                self._tips[i].setPos((lo + hi) / 2.0, 0.0)
        if hasattr(self, "_big"):
            self._big.setXRange(lo, hi, padding=0)
            self._big_tip.setPos((lo + hi) / 2.0, 0.0)
        if hasattr(self, "_overlay"):
            self._overlay.setXRange(lo, hi, padding=0)
            self._overlay_tip.setPos((lo + hi) / 2.0, 0.0)
    def _set_follow(self, v: bool):
        self._follow = bool(v)
        if self._follow:
            self._reset_x_range()
    def _on_manual_zoom(self, src=None):
        """用户拖过/滚过就停止自动改范围，否则每 40ms 被抢回，缩放等于没有。

        src 是用户缩的那张图（0~13，-1=单通道大图）。记住它，之后按它的范围取数据，
        并把所有图统一到同一段，否则缩 A 图、B 图还在老位置，看着像数据对不上。
        时间轴是累计秒数，左边界不许为负；pyqtgraph 的 ViewBox 自己会把视图拖出 0 以前。
        """
        if isinstance(src, int):
            self._zoom_src = src
        if self._follow:
            self._follow = False
            self.btn_follow.setChecked(False)
            self.lbl.setText("已手动缩放：不再自动跟随，点「跟随最新」恢复")
        self._unify_time_axis(src)

    def _unify_time_axis(self, src=None):
        """把用户刚改的那张图的横轴夹到非负，再同步给全部子图和大图。

        空屏（_recv==0）时 _tick 提前 return，原来只有拿到数据时才做同步，
        于是等待数据状态下拖一张图，四张图横轴各走各的 —— 现场截图里
        -6~17 / -20~70 / 1~9 / 8.8~9.8 就是这条路径漏掉的结果。
        """
        big = getattr(self, "_big", None)
        overlay = getattr(self, "_overlay", None)
        targets = list(self.plots) + ([big] if big is not None else [])
        if overlay is not None:
            targets.append(overlay)
        if not targets:
            return
        if src == -1 and big is not None:
            ref = big
        elif src == -2 and overlay is not None:
            ref = overlay
        elif isinstance(src, int) and 0 <= src < len(self.plots):
            ref = self.plots[src]
        else:
            ref = (big if (self._solo is not None and big is not None) else
                   overlay if (overlay is not None and
                               self._stack.currentWidget() is self._overlay_page) else
                   self.plots[self._page * PAGE_SIZE])
        v0, v1 = self._view_bounds(ref)
        if v0 < 0.0:
            v1 -= v0                       # 整体右移，保持用户选的窗宽
            v0 = 0.0
        if not (v1 > v0):
            v0, v1 = 0.0, float(self.win_s)
        for p in targets:
            a, b = self._view_bounds(p)
            if abs(a - v0) > 1e-6 or abs(b - v1) > 1e-6:
                p.setXRange(v0, v1, padding=0)
    def _apply_axis_labels(self):
        """单位随链路变：二进制是折算后的 μV，文本是未换算的原始值。

        网格用短标题（格子太矮，长标题会被裁成半截），放大页用完整单位。
        """
        for i, p in enumerate(self.plots):
            self._set_left_label(p, i, short=True)
        if self._solo is not None:
            self._set_left_label(self._big, self._solo)
            self._lbl_solo.setText(self._solo_header(self._solo))
        self._overlay.setLabel('left', 'EEG', units=self.eeg_unit)
        self._overlay.setLabel('right',
                               (f'fNIRS 0{self.fnirs_version} 原始码 波长{1 if self.show_hbo else 2}'
                                if self.fnirs_version else
                                ('fNIRS ΔHbO2' if self.show_hbo else 'fNIRS ΔHbR')),
                               units=self.fnirs_unit)
    @staticmethod
    def _robust_span(row):
        """一段样本 -> (中心, 半幅)，用分位数定量程，单个毛刺不参与。

        返回 None 表示这段没有可用起伏（常数行 / 点数太少），调用方自己兜底。
        """
        try:
            a = np.asarray(row, dtype=float).reshape(-1)
            a = a[np.isfinite(a)]
            if a.size < 8:
                return None
            lo, hi = np.percentile(a, [AUTO_Q_LO, AUTO_Q_HI])
            if not (hi > lo):
                return None                 # 常数通道 = 没有数据
            mid = (lo + hi) / 2.0
            half = (hi - lo) / 2.0
            # 极端值离中心不算太远就并进来：真实的大幅波动(眨眼/体动)不能被裁掉
            art = max(abs(float(a.min()) - mid), abs(float(a.max()) - mid))
            if art <= half * AUTO_GLITCH_X:
                half = art
            return mid, half
        except Exception:  # noqa: BLE001
            return None

    def _scale_of(self, seg) -> float:
        """这一段数据需要的半幅量程（贴峰、抗毛刺、有下限）。取不到返回 0。

        三处必须注意：
        1) 用 (max-min)/2 而不是 max(|x|)：真实信号带直流偏置时（如原始码值在
           -8700 附近），按绝对值定量程会得到 20000 却把波形压在框边。
        2) 跳过最大值等于最小值的通道：单列文本模式下只有 CH1 有数据，其余通道
           全是 0 补位，混进来会把峰峰值从 3000 拉到 12000，量程直接锁死在最高档。
        3) 量程要抗毛刺（走 _robust_span 的分位数），否则一个坏帧就把整屏压平。
        """
        try:
            spans = []
            for row in np.atleast_2d(seg):
                rs = self._robust_span(row)
                if rs is not None:
                    spans.append(rs[1] * 2.0)     # 半幅 -> 峰峰
            peak = max(spans) / 2.0 if spans else 0.0
        except Exception:  # noqa: BLE001
            return 0.0
        if not np.isfinite(peak) or peak <= 0:
            return 0.0
        # 量程直接贴合峰值，不再按 1/2/5 取整：
        # 取整会让 126 的峰峰落到 100 档（只占 63%）甚至 200 档（31%），
        # 现场的原话就是"太紧凑，看不出波形浮动"。VOFA 的 Auto 也是贴极值。
        return max(peak * AUTO_PAD, AUTO_MIN_SCALE)

    def _auto_eeg_scale(self, base_seg, view_seg=None):
        """定纵轴半幅量程。base_seg = 基准段（最近一窗），view_seg = 当前视图段。

        为什么要分成两段：纵轴是测量读数，横轴缩放只该改"看哪段时间"。
        旧写法只拿可见段定量程 —— 从 5 s 放大到 0.2 s，可见段的峰峰值小 25 倍，
        纵轴读数就跟着从 -122081~-118623 变成 -121654~-119770，同一条曲线在两种
        缩放下列出两组数，现场会认为测量值本身在变（实测量程 3459 -> 1884）。

        现在基准恒定取"最近一窗"，缩放不影响；只有当视图里的数据确实比基准大一截
        （缩出去看更早的、幅度更大的历史）才抬高量程，避免削顶。只抬不压，
        缩回来时基准接管，读数自动回到原值。
        """
        want = self._scale_of(base_seg)
        if view_seg is not None:
            need = self._scale_of(view_seg)
            if need > want:
                want = need
        if want <= 0:
            return
        # 回差收到 ±15%：贴合得紧，但不会每一帧都跟着噪声跳档
        if want > self.eeg_scale * AUTO_HYST or want < self.eeg_scale / AUTO_HYST:
            self.eeg_scale = want
    def _open_verify(self):
        from verify_center import VerifyCenter
        dlg = VerifyCenter(self); dlg.exec_()
    def set_battery(self, pct: float):
        """H6: 上位机读取电量百分比(固件经扩展字段下发后接入)."""
        self.battery_pct = float(pct)
    def _toggle_record(self, on: bool):
        import datetime
        from pathlib import Path
        if on:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            # 固定落在程序目录的 records/ 下。原先是相对路径, 从开始菜单快捷方式
            # 双击时工作目录可能是 system32, 录完就找不着文件了。
            rec_dir = Path(__file__).resolve().parent.parent / "records"
            rec_dir.mkdir(parents=True, exist_ok=True)
            self._csv_path = rec_dir / f"sleep_record_{ts}.csv"
            from exporters import CsvWriter
            import contract
            # 表头按契约三组列: 必需(含20路原始码值+20路光强) + 可选(OD/HbO/HbR) [+ 扩展]
            self._writer = CsvWriter(
                self._csv_path,
                header=contract.csv_header(optional=True, extra=self.extra_cols),
            )
            self._n_saved = 0; self._t0 = time.time(); self._t_sampling0 = None
            self.btn_record.setText("■ 停止记录")
            self.btn_export.setEnabled(False)
            self.lbl_store.setText(f"● 记录中: records/{self._csv_path.name}")
        else:
            if self._writer: self._writer.close(); self._writer = None
            el = time.time()-self._t0
            self.btn_record.setText("● 开始记录")
            if self._n_saved <= 0 and self._csv_path is not None:
                try: self._csv_path.unlink(missing_ok=True)
                except Exception: pass
                self._csv_path = None
                self.btn_export.setEnabled(False)
                self.lbl_store.setText("空记录已丢弃, 未生成文件")
                return
            self.btn_export.setEnabled(self._n_saved > 0)
            self.lbl_store.setText(f"已保存 {self._n_saved} 行 ({el:.0f}s) → {self._csv_path.name}")
    def _export_all(self, only=None, out_dir=None, _checked=False):
        from pathlib import Path
        from exporters import (export_edf, export_bdf, export_snirf,
                               export_xdf_from_csv)
        if self._csv_path is None:
            self.lbl_store.setText("请先记录CSV再导出")
            return
        # S2: 无预设时弹窗让用户勾选格式+选择保存目录
        if only is None and out_dir is None:
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("导出 (S2): 选择格式+保存位置")
            fl = QtWidgets.QVBoxLayout(dlg)
            fl.addWidget(QtWidgets.QLabel(f"源: {Path(self._csv_path).name}"))
            cks = {}
            for fmt, tip in [("EDF", "EDFbrowser可读"), ("BDF", "24bit欧洲格式"),
                             ("SNIRF", "Homer3可读"), ("XDF", "pyxdf/MoBI可读")]:
                c = QtWidgets.QCheckBox(f"{fmt} ({tip})")
                c.setChecked(True)
                cks[fmt] = c
                fl.addWidget(c)
            row = QtWidgets.QHBoxLayout()
            le_dir = QtWidgets.QLineEdit(str(Path(self._csv_path).resolve().parent))
            btn_dir = QtWidgets.QPushButton("选择保存位置…")
            btn_dir.clicked.connect(lambda: le_dir.setText(
                QtWidgets.QFileDialog.getExistingDirectory(self, "选择保存位置", le_dir.text())
                or le_dir.text()))
            row.addWidget(le_dir)
            row.addWidget(btn_dir)
            fl.addLayout(row)
            bb = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
            bb.accepted.connect(dlg.accept)
            bb.rejected.connect(dlg.reject)
            fl.addWidget(bb)
            if dlg.exec_() != QtWidgets.QDialog.Accepted:
                return
            only = [k for k, c in cks.items() if c.isChecked()]
            if not only:
                self.lbl_store.setText("未勾选任何格式，已取消")
                return
            out_dir = le_dir.text() or "."
        base = str((Path(out_dir) / self._csv_path.stem) if out_dir else self._csv_path.with_suffix(""))
        want = set(only) if only else {"EDF", "SNIRF", "XDF"}
        parts = [f"{self._csv_path.name}(CSV已有)"]
        if "EDF" in want:
            try:
                export_edf(self._csv_path, base + ".edf", fs_eeg=self.fs_eeg)
                parts.append("EDF[OK]")
            except Exception as e: parts.append(f"EDF跳过({e})")
        if "BDF" in want:
            try:
                export_bdf(self._csv_path, base + ".bdf", fs_eeg=self.fs_eeg)
                parts.append("BDF[OK]")
            except Exception as e: parts.append(f"BDF跳过({e})")
        if "SNIRF" in want:
            try:
                export_snirf(self._csv_path, base + ".snirf", fs_eeg=self.fs_eeg)
                parts.append("SNIRF[OK]")
            except Exception as e: parts.append(f"SNIRF跳过({e})")
        if "XDF" in want:
            try:
                # 必须从已记录的 CSV 导出：旧代码拿界面缓冲的最后 2000 点，
                # 导出的 XDF 与这份 CSV 不是同一段数据，甲方对不上。
                export_xdf_from_csv(self._csv_path, base + ".xdf", fs_eeg=self.fs_eeg)
                parts.append("XDF[OK]")
            except Exception as e: parts.append(f"XDF跳过({e})")
        self.lbl_store.setText(" ".join(parts))
    def _rec_row(self, seq, eeg_uv, hbo, hbr, acc_xyz=None, gyro_xyz=None,
                 raw=None, battery=-1.0, ts_abs_ms=0, event_type=0, event_ts=0,
                 device_id=None, t_sample=None):
        """写一行 CSV：先组装成 Frame 再交给契约转行，表头和行同源，不会错位。"""
        if self._writer is None: return
        # timestamp_s = 采样时刻（相对本次记录起点）。绝不用 time.time()：
        # 批量取帧时一批帧的写盘时刻相同，导出 EDF 会把采样率算成几千 Hz。
        if self._t_sampling0 is None:
            self._t_sampling0 = self._t_sampling if t_sample is None else t_sample
        t = (time.time() - self._t0 if t_sample is None
             else max(0.0, t_sample - self._t_sampling0))
        n_inten = contract.N_FNIRS*len(contract.FNIRS_WLS)
        raw20 = [float(v) for v in (raw or [])][:n_inten]
        raw20 += [float("nan") if self.fnirs_version else 0.0] * (n_inten-len(raw20))
        if self.fnirs_version == 1:
            raw20[12:] = [float("nan")] * 8  # 01 的后四槽不是物理采样
            for channel in range(6):
                if not self._ch_ok[self.n_eeg + channel]:
                    raw20[2*channel:2*channel+2] = [float("nan")] * 2

        def _by_ch(vals):
            """定长 10 路; 质控判异常的通道写 None -> CSV 空串(不写 0 冒充结果)。"""
            if vals is None:
                return [None] * contract.N_FNIRS
            v = [float(x) for x in (vals or [])][:contract.N_FNIRS]
            v += [0.0]*(contract.N_FNIRS-len(v))
            m = getattr(self, "_last_mask", None)
            if m is None or len(m) < contract.N_FNIRS:
                return v
            return [x if ok else None for ok, x in zip(m, v)]

        fr = contract.Frame(
            # 合同 F2 要求"支持上位机区分 ≥8 台设备同时在线" —— 必须写帧头里的真实编号,
            # 不能写死 1: 两台设备同时录制会混成一份无法分辨的记录。
            device_id=int(self._device_id if device_id is None else device_id),
            seq=int(seq), t_device_ms=int(round(t*1000)),
            t_host_s=t, t_abs_ms=int(ts_abs_ms or 0),
            eeg_uv=[float(v) for v in (eeg_uv or [])],
            fnirs_raw=contract.unnest_raw(raw20),
            fnirs_intensity=(None if self.fnirs_version else
                             [round(v*self.lux_per_lsb, 4) for v in raw20]),
            od=list(getattr(self, "_last_od", None) or []) or None,   # 已建基线即写真值
            hbo=_by_ch(hbo),
            hbr=_by_ch(hbr),
            hbt=_by_ch(self._last_hbt),
            sto2=_by_ch(self._last_sto2),
            acc_xyz=list(acc_xyz) if acc_xyz is not None else None,
            gyro_xyz=list(gyro_xyz) if gyro_xyz is not None else None,
            battery_pct=float(battery),
            loss_pct=self._lost/max(1, self._lost+self._recv),
            event_type=int(event_type), event_ts=int(event_ts),
        )
        row = contract.frame_to_row(fr, optional=True, extra=self.extra_cols,
                                    metrics=getattr(self, "_metrics", None))
        expect = len(getattr(self._writer, "header", None) or row)
        if len(row) != expect:
            # 宁可不写这一行, 也不写出一份列错位、看起来正常的 CSV
            self._csv_shape_bad = getattr(self, "_csv_shape_bad", 0)+1
            if self._csv_shape_bad == 1:
                try: self.lbl_store.setText(f"● 记录异常: 行{len(row)}列≠表头{expect}列, 已停写")
                except Exception: pass
            return
        self._writer.write_row(row); self._n_saved += 1
        if self._n_saved % 100 == 0:
            self.lbl_store.setText(f"● 记录中: {self._csv_path.name} ({self._n_saved}行)")
    def _derive_fnirs(self, raw):
        """20 路码值 -> (ΔHbO[10], ΔHbR[10], ΔOD[20])；基线未建完时返回 None。

        文档 §1.5: 异常通道不做处理。这里的 valid 取自每 25 帧刷新的
        质控结果, 因此门控最多滞后 25 帧; 被排除的通道在 CSV 里写空。
        """
        self._last_mask = None
        self._last_hbt = None
        self._last_sto2 = None
        if raw is None:
            return None
        try:
            r = np.asarray(raw, dtype=float).reshape(-1)
            if r.size != 20:
                return None
            if self._base_rows is None:
                self._base_buf.append(r)
                if len(self._base_buf) < self.n_baseline:
                    return None
                self._base_rows = np.vstack(self._base_buf).mean(axis=0)
                self._base_buf = []
            from derive import raw_to_lux, od_delta, mbll_batch, hb_metrics
            lux = raw_to_lux(r)                      # 码值 -> 光强
            od = od_delta(lux, self._base_rows)      # ΔOD = -ln(I/I0)
            valid = None
            q = getattr(self, "fnirs_quality", None)
            if q is not None and len(q) == contract.N_FNIRS:
                from quality import mask_from_flags
                valid = mask_from_flags(q)
            res = mbll_batch(od[0::2], od[1::2], valid=valid)  # 760 / 850 两支
            self.n_mbll_singular += int(res.n_singular)
            self.n_mbll_masked += int(res.n_masked)
            if valid is not None:
                self._last_mask = [bool(v) for v in valid]
            hbo = np.nan_to_num(res.hbo_mM, nan=0.0)
            hbr = np.nan_to_num(res.hbr_mM, nan=0.0)
            met = hb_metrics(res.hbo_mM, res.hbr_mM)  # §1.3 ΔHbT / 变化量占比
            self._last_hbt = np.nan_to_num(met.hbt_mM, nan=0.0).tolist()
            # 占比无意义时保留 NaN: 契约层会写成空串, 不能把 0 当成正常读数
            self._last_sto2 = met.sto2_delta.tolist()
            return hbo.tolist(), hbr.tolist(), od.tolist()
        except Exception:
            return None

    def fnirs_state(self) -> str:
        """状态栏用的 fNIRS 计算状态, 用于区分"没数据"和"没算完"。"""
        if self.fnirs_version:
            valid = sum(self._ch_ok[self.n_eeg:])
            kind = "单板暗场" if self.fnirs_version == 1 else "十槽光学"
            return f"0{self.fnirs_version} {kind} 原始码 {valid}/{6 if self.fnirs_version == 1 else 10}（非血红蛋白浓度）"
        if self._base_rows is None:
            return f"基线 {len(self._base_buf)}/{self.n_baseline}"
        bits = []
        if self.n_mbll_singular > 0:
            bits.append(f"奇异{self.n_mbll_singular}")
        if self.n_mbll_masked > 0:
            bits.append(f"质控排除{self.n_mbll_masked}")
        return f"正常({'、'.join(bits)})" if bits else "正常"

    def push_frame(self, seq, eeg_uv, hbo, hbr, raw=None, battery=-1.0, acc_xyz=None, gyro_xyz=None,
                   ts_abs_ms=0, event_type=0, event_ts=0, device_id=1, eeg_n=None, t_dev_ms=None,
                   fnirs_version=0, fnirs_new=False):
        self._t_rx = time.time()
        firmware_fnirs = fnirs_version in (1, 2)
        if firmware_fnirs and self.fnirs_version != fnirs_version:
            self.fnirs_version = fnirs_version
            self.fnirs_unit = "ADC码"
            self._fw_fnirs_latest[:] = np.nan
            self.buf_hbo[:] = np.nan
            self.buf_hbr[:] = np.nan
            self._ch_ok[self.n_eeg:] = [False] * 10
            self._base_buf = []
            self._base_rows = None
            self._last_od = None
            self._last_hbt = None
            self._last_sto2 = None
            self._last_mask = None
            self.cmb_fnirs.setItemText(0, "原始码·波长1")
            self.cmb_fnirs.setItemText(1, "原始码·波长2")
            self._apply_axis_labels()
        if firmware_fnirs:
            if fnirs_new and raw is not None:
                values = np.asarray(raw, dtype=float).reshape(10, 2)
                if fnirs_version == 1:
                    values[6:] = np.nan  # 01 的槽 7～10 是占位，不是采样
                self._fw_fnirs_latest[:] = values
                if fnirs_version == 1:
                    # 01 没有逐板有效位；不显示未启动座子填充的全零平线。
                    self._ch_ok[self.n_eeg:self.n_eeg + 6] = \
                        np.any(values[:6] != 0, axis=1).tolist()
                else:
                    self._ch_ok[self.n_eeg:] = [True] * 10
                active = values[np.asarray(self._ch_ok[self.n_eeg:], dtype=bool)]
                finite = active[np.isfinite(active)]
                fitted = self._fit_overlay_range(finite, 20.0) if finite.size else None
                if fitted is not None:
                    self._overlay_fnirs_range = fitted
                    self._overlay_fnirs_vb.setYRange(*fitted, padding=0)
            hbo = self._fw_fnirs_latest[:, 0].tolist()
            hbr = self._fw_fnirs_latest[:, 1].tolist()
        # 采样时刻：设备单调毫秒 > 设备绝对毫秒 > 本地等间隔合成。
        # 设备复位会让单调毫秒跳变，回退到合成时间，绝不在 CSV 里留下倒退的时间轴。
        self._t_synth += 1.0 / max(1.0, float(self.fs_eeg))
        # t_dev_ms=0 是合法的设备上电第 0 ms，不能用真值判断当作"没提供"
        t_sample = (float(t_dev_ms) / 1000.0 if t_dev_ms is not None else
                    (float(ts_abs_ms) / 1000.0 if ts_abs_ms and ts_abs_ms > 0
                     else self._t_synth))
        if t_sample + 1.0 < getattr(self, "_t_sampling_prev", -1e18):
            t_sample = self._t_synth
        self._t_sampling_prev = t_sample
        self._t_sampling = t_sample
        # eeg_n 是"本帧实际带了几路 EEG"。文本链路每行只有 1 个数时传 1,
        # 其余 3 路是补 0 的占位, 不能当数据画出来。
        n_ok = self.n_eeg if eeg_n is None else max(0, min(int(eeg_n), self.n_eeg))
        for i in range(n_ok):
            self._ch_ok[i] = True
        if raw is not None and not firmware_fnirs:
            for i in range(10):
                self._ch_ok[self.n_eeg + i] = True
        if device_id is not None:
            self._device_id = int(device_id)
        if self._seq_last >= 0:
            # seq 是 uint32，按 32 位回绕差值判丢包，不直接比较大小
            delta = (int(seq) - int(self._seq_last)) & 0xFFFFFFFF
            if delta == 0:
                self._dups = getattr(self, "_dups", 0) + 1        # 重复帧，不计丢包
            elif delta < 0x80000000:
                self._lost += delta - 1                           # 正常前进（含 32 位回绕）
            else:
                self._seq_resets = getattr(self, "_seq_resets", 0) + 1  # 回落=复位/大乱序
        self._seq_last = seq; self._recv += 1
        # 样本计数只增不减(缓冲区是滚动的)，横轴的绝对秒数由它推出
        self._n_eeg += 1
        self._n_fn += 1
        if battery >= 0: self.battery_pct = float(battery)
        self.buf_eeg = np.roll(self.buf_eeg, -1, axis=1); self.buf_eeg[:, -1] = eeg_uv
        # 有真实光强就用真实值算 ΔHbO/ΔHbR; 算不出来(基线未建完/坏道)沿用入参(通常为 0)
        _d = None if firmware_fnirs else self._derive_fnirs(raw)
        if _d is not None:
            hbo, hbr, self._last_od = _d
        # 两份浓度缓存都按绝对样本序号环形写入，切换指标时历史立刻可用。
        fn_col = (self._n_fn - 1) % self.buf_hbo.shape[1]
        self.buf_hbo[:, fn_col] = hbo
        self.buf_hbr[:, fn_col] = hbr
        if raw is not None:
            self.buf_raw = np.roll(self.buf_raw, -1, axis=1); self.buf_raw[:, -1] = raw
        if acc_xyz is not None or gyro_xyz is not None:
            if not hasattr(self, "buf_imu"):
                self.buf_imu = np.zeros((6, int(100.0*30)))
            self.buf_imu = np.roll(self.buf_imu, -1, axis=1)
            ax = list(acc_xyz) if acc_xyz is not None else [0.0, 0.0, 1.0]
            gx = list(gyro_xyz) if gyro_xyz is not None else [0.0, 0.0, 0.0]
            self.buf_imu[:, -1] = ax + gx
            if hasattr(self, "motion_panel"):
                try: self.motion_panel.push(np.array(ax), np.array(gx))
                except Exception: pass
        self._rec_row(seq, eeg_uv, None if firmware_fnirs else hbo,
                      None if firmware_fnirs else hbr, acc_xyz, gyro_xyz,
                      raw=raw, battery=battery,
                      ts_abs_ms=ts_abs_ms, event_type=event_type, event_ts=event_ts,
                      t_sample=t_sample)
        # S9: 全部由现有EEG/fNIRS算出, ≥1Hz刷新 (每25帧≈1Hz), 推面板+存CSV/XDF
        try:
            if not hasattr(self, "_sm"):
                from band_power import StateMetrics
                self._sm = StateMetrics()
                self._metrics = {"brain_active": 50.0, "brain_relax": 50.0,
                                 "focus": 50.0, "hr_bpm": 70.0}
                self.buf_met = np.zeros((4, 600))
            import numpy as _np
            ppg = float(_np.mean(raw)) if raw is not None else 1000.0
            if not getattr(self, "unit_unknown", False):
                self._sm.push(float(list(eeg_uv)[0]), float(_np.mean(hbo)),
                              float(_np.mean(hbr)), ppg)
        except Exception:
            pass
        if self._recv % 25 == 0:
            try:
                self._metrics.update(self._sm.compute())
                self.buf_met = _np.roll(self.buf_met, -1, axis=1)
                self.buf_met[:, -1] = [self._metrics["brain_active"], self._metrics["brain_relax"],
                                        self._metrics["focus"], self._metrics["hr_bpm"]]
                if hasattr(self, "metrics_panel"):
                    try:
                        m = self._metrics
                        self.metrics_panel.push(m["brain_active"], m["brain_relax"],
                                                m["focus"], m["hr_bpm"] / 220 * 100, m["hr_bpm"])
                    except Exception:
                        pass
            except Exception:
                pass
        if self._recv % 25 == 0:
            from quality import eeg_contact, fnirs_flags, fnirs_levels
            try:
                self.eeg_quality = eeg_contact(self.buf_eeg[:, -500:])
                if firmware_fnirs:
                    self.fnirs_quality = ["未标定"] * 10
                    self._fnirs_levels = []
                else:
                    # 质控按十个通道计算，两个波长先取均值。
                    r = self.buf_raw[:, -50:]
                    if r.shape[0] == contract.N_FNIRS * len(contract.FNIRS_WLS):
                        r = r.reshape(contract.N_FNIRS, len(contract.FNIRS_WLS), -1).mean(axis=1)
                    self.fnirs_quality = fnirs_flags(r)
                    self._fnirs_levels = fnirs_levels(r)
                self._quality_error = ""
            except Exception as e:
                self._quality_err_n = getattr(self, "_quality_err_n", 0) + 1
                self._quality_error = (f"质量计算异常 x{self._quality_err_n}: "
                                       f"{type(e).__name__}: {e}")
            self._refresh_status()

    def _refresh_status(self):
        """状态栏：收包数、丢包率、fNIRS 状态、电量；没有数据时显示「等待数据」。"""
        r = self._lost / max(1, self._lost + self._recv)
        bat = f"{self.battery_pct:.0f}%" if self.battery_pct >= 0 else "--%"
        idle = 0.0 if self._t_rx is None else (time.time() - self._t_rx)
        if self._recv == 0:
            head = "等待数据"
        elif idle > 2.0:
            # 停发后不要继续显示旧帧率, 否则看起来像还在采
            head = f"无新数据 {idle:.0f}s | 累计收包 {self._recv} | 板子已停发"
        else:
            fps_s = f"{self.fs_measured:.1f}Hz" if self.fs_measured else "--Hz"
            # 文本链路没做过折算, 标 μV 就是假信息
            unit = "原始值" if getattr(self, "unit_unknown", False) else "μV"
            head = (f"帧率 {fps_s} | 收包 {self._recv} | 丢包率 {r:.1%}"
                    f" | 量程±{self.eeg_scale:.0f}{unit}")
        self.lbl.setText(f"{head} | fNIRS {self.fnirs_state()} | 电量 {bat}")
        self._refresh_quality()

    _Q_COLOR = {"好": "#2e7d32", "接触差": "#e08a00", "坏道": "#c62828",
                "正常": "#2e7d32", "饱和": "#e08a00", "暗噪声": "#1565c0", "异常": "#c62828"}

    def _refresh_quality(self):
        """把各通道信号质量显示出来（EEG 坏道、fNIRS 暗噪声/饱和）。"""
        if not hasattr(self, "lbl_quality"):
            return
        if self._recv == 0:
            self.lbl_quality.setText("通道质量 -- (无数据)")
            return
        c = self._Q_COLOR

        def seg(name, st):
            return f'<span style="color:{c.get(st, "#555")}">{name} {st}</span>'

        eeg = " ".join(seg(self.eeg_names[i] if i < len(self.eeg_names) else f"EEG{i+1}", s)
                       for i, s in enumerate(self.eeg_quality))
        # fNIRS 通道多, 只把非"正常"的点名, 正常的给个计数
        bad = [(i + 1, s) for i, s in enumerate(self.fnirs_quality) if s != "正常"]
        if bad:
            fs = " ".join(seg(f"CH{n}", s) for n, s in bad)
            fs = f"正常 {len(self.fnirs_quality) - len(bad)}/{len(self.fnirs_quality)} | {fs}"
        else:
            fs = f'<span style="color:{c["正常"]}">全部正常 {len(self.fnirs_quality)}/{len(self.fnirs_quality)}</span>'
        line = f"<b>EEG</b> {eeg} &nbsp;|&nbsp; <b>fNIRS</b> {fs}"
        if self._quality_error:
            self.lbl_quality.setText(line + f"<br><span style='color:#c62828'>{self._quality_error}</span>")
            return
        # S3: 暗噪声基线（每通道中位光强）。系数未标定时就是原始码值，
        # 明确写出当前系数，不能让现场把 1.0 的占位值当 lux 读。
        if self._fnirs_levels:
            med = " ".join(f"{i+1}:{v[1]*self.lux_per_lsb:.0f}"
                           for i, v in enumerate(self._fnirs_levels))
            line += (f"<br><span style='color:#8e8e8e'>暗噪声基线(中位, "
                     f"系数={self.lux_per_lsb:g}待标定): {med}</span>")
        self.lbl_quality.setText(line)

    def reset_buffers(self):
        """换数据源前清空波形与分析状态，避免上一条链路的旧数据混进新曲线。"""
        self.buf_eeg[:] = 0.0
        self.buf_hbo[:] = 0.0
        self.buf_hbr[:] = 0.0
        self.buf_raw[:] = 0.0
        self.fnirs_version = 0
        self.fnirs_unit = "mM"
        self._fw_fnirs_latest[:] = np.nan
        self.cmb_fnirs.setItemText(0, "ΔHbO2")
        self.cmb_fnirs.setItemText(1, "ΔHbR")
        for curve in self._overlay_curves:
            curve.setData([], [])
        self._reset_overlay_scale()
        self._seq_last = -1
        self._lost = 0
        self._recv = 0
        self._n_eeg = 0               # 换源后绝对时间从 0 重新算
        self._n_fn = 0
        self._x_left = 0.0
        self._sim_seq = 0
        self.fs_measured = 0.0
        self._fs_snapped = False      # 换源后重新按实测帧率校准
        self.eeg_scale = 100.0
        self._ch_ok = [False]*self.n_total     # 换源后重新判定哪些通道有数据
        self._t_rx = None
        self.eeg_quality = ["--"] * self.n_eeg
        self.fnirs_quality = ["--"] * 10
        self._fnirs_levels = []

    def detach_receiver(self):
        """断开数据源：之后不再产生任何数据（不造数）。"""
        self._receiver = None
        self.sim = False
        self.unit_unknown = False

    def attach_receiver(self, receiver):
        """接实机：receiver 需提供 poll_frames()，调用后界面改为读设备数据。"""
        self.reset_buffers()
        self._receiver = receiver
        self.sim = False
        # 文本链路给的是未换算的原始值（单位未确认）。它的值域和 μV 差上千倍，
        # 直接喂指标算法会把 brain_relax 算成常数并触发 exp 溢出。这里整段不参与，
        # 指标保持默认值，比显示一个错的数好。
        self.unit_unknown = getattr(receiver, "kind", "") in ("text", "text-file")
        # 轴单位跟着链路走: 文本链路没折算过, 标 μV 就是假信息
        self.eeg_unit = "原始值·单位待定" if self.unit_unknown else "μV"
        self._apply_axis_labels()

    def _update_rate(self):
        """用实测帧率校正显示采样率。

        两点必须排除，否则时间轴直接错：
        1) 文件回放是"瞬间读完一个文件"，实测出来的 fps 是回放速度，不是采样率。
           拿它当 fs，500 SPS 的波形会被按 8000 SPS 画，横向压成一小段。
        2) 样本太少时首末时间差太小，fps 抖动极大。攒够帧数再校准。
        """
        kind = getattr(self._receiver, "kind", "")
        if kind in ("text-file", "dry"):
            return
        if self._recv < 50:
            return
        try:
            fs = float(self._receiver.measured_fps())
        except Exception:
            return
        if fs < 1.0:
            return
        self.fs_measured = fs
        if not self._fs_snapped:
            self.fs_eeg = self.fs_fnirs = fs      # 首次直接采用, 不用平滑
            self._fs_snapped = True
        else:
            self.fs_eeg = self.fs_eeg * 0.9 + fs * 0.1
            self.fs_fnirs = self.fs_eeg

    def _tick(self, force: bool = False):
        # force=True 只表示"用户动作要立刻重画"，不表示"暂停期间继续收数据"。
        # 旧写法把两件事绑在一起：暂停后双击放大(force)会偷偷取走串口缓冲里的帧，
        # 时间轴跟着往前跳 —— 现场就是"暂停了还在动 / 只剩最后一段"。
        if self.paused and not force: return
        frozen = self.paused
        # 唯一数据入口: _receiver。没有 receiver 就不产生任何数据(不造数)。
        if not frozen and self._receiver is not None:
            # 实机/录播: 每节拍取帧并push, 解析(帧头/seq/μV折算/事件)在receivers+device_frame里做
            try:
                for fr in self._receiver.poll_frames():
                    self.push_frame(*fr) if isinstance(fr, tuple) else self.push_frame(**fr)
            except Exception as e:
                # 以前这里是 except: pass —— 帧处理一抛异常就整条链静默停摆,
                # 界面上还是"在跑", 实际一帧没进、一行没存。现在至少报出来。
                self._tick_errors = getattr(self, "_tick_errors", 0) + 1
                if self._tick_errors in (1, 100, 1000):
                    self.lbl.setText(f"⚠ 帧处理异常 x{self._tick_errors}: "
                                     f"{type(e).__name__}: {e}")
            self._update_rate()
        # 状态栏独立刷新: 一帧没到也要显示"等待数据", 不能等 push_frame 才更新
        self._st_tick += 1
        if self._st_tick % 5 == 0 and not frozen:
            self._refresh_status()
        # 只画真数据：一帧都没收到，或板子已停发超过 2 s，就把曲线清空。
        # 暂停时不能用挂钟判"停发"：用户停了 10 s 再看，缓冲区里的波形仍是他要分析的，
        # 一按放大就被判 stale 清成"等待数据…"，等于暂停功能失效。
        stale = False if frozen else (
            self._t_rx is None or (time.time() - self._t_rx) > 2.0)
        if self._recv == 0 or stale:
            for c in self.curves:
                c.setData([], [])
            for tip in self._tips:
                tip.setText("等待数据…"); tip.show()
            # 读数跟着一起收起来：曲线清空后视图范围还是上一帧的，
            # 空图上留着一组六位数字，现场会当成"当前值"。
            for i in range(self.n_total):
                self._hide_ylabels(i)
            self._big_curve.setData([], [])
            self._big_tip.setText("等待数据…"); self._big_tip.show()
            # 空屏也要守住"四图同一把尺 + 时间不为负"。上面这段提前 return，
            # 旧代码只有拿到数据才同步，等待连接时拖一下就会出现四种横轴。
            if not self._follow:
                self._unify_time_axis()
            self._paint_overlay(None, None, None, None, '等待数据…')
            return
        # 画哪一段样本：跟随时取最近一窗；手动缩放过就按当前视图范围取。
        # 以前固定只取 win*fs 点，缩出去只能看到"一段 10 秒的短线"，
        # 前面几十秒明明还在缓冲里，却是一片空白（现场当成"前面的数据没了"）。
        fe = max(self.fs_eeg, 1e-6); ff = max(self.fs_fnirs, 1e-6)
        recv = int(self._recv)
        ref = (self._big if self._solo is not None else
               self._overlay if self._stack.currentWidget() is self._overlay_page else
               self.plots[self._zoom_src if 0 <= self._zoom_src < self.n_total else 0])
        if self._follow:
            c_e = min(recv, int(self.win_s*fe)); i0_e = recv - c_e
            c_f = min(recv, int(self.win_s*ff)); i0_f = recv - c_f
        else:
            v0, v1 = self._view_bounds(ref)
            # 视图夹进"还有数据"的区间：越出去就会看到一段空白，看着像数据断了。
            lo_ok = max(0.0, (recv - self.buf_eeg.shape[1]) / fe)
            # min_span: 放最大时至少留 MIN_SPAN_S（0.2 s）。再窄就只剩一两个采样点，
            # _take 取不到东西会清屏，或者画出一个点 —— 现场就是"放到最大数据没了"。
            v0, v1 = self._clamp_to_data(v0, v1, lo_ok, recv / fe,
                                        max(MIN_SPAN_S, 2.0 / fe))
            # 14 张图统一到这一段（合同 S1「时间轴统一」）：缩一张，其余跟着走，
            # 否则别的子图还停在旧窗口，同一时刻看的是不同时间段的数据。
            for p in self.plots + [self._big, self._overlay]:
                a, b = self._view_bounds(p)
                if abs(a - v0) > 1e-6 or abs(b - v1) > 1e-6:
                    p.setXRange(v0, v1, padding=0)
            i0_e = max(0, int(round(v0*fe))); c_e = max(2, int(round((v1-v0)*fe)))
            i0_f = max(0, int(round(v0*ff))); c_f = max(2, int(round((v1-v0)*ff)))
            # 视图可能拉到采集开始之前 / 缓冲已被覆盖，夹到实际还有的范围
            c_e = max(0, min(c_e, recv - i0_e))
            c_f = max(0, min(c_f, recv - i0_f))
            # 窗口贴着最新点又极窄时可能只剩 1 个点，退两个样本出来，保证画得出来
            if c_e < 2 and recv >= 2:
                i0_e, c_e = recv - 2, 2
            if c_f < 2 and recv >= 2:
                i0_f, c_f = recv - 2, 2
        xe, seg_e = self._take(self.buf_eeg, i0_e, c_e, fe, recv)
        xf, seg_f = self._take(self.buf_fn, i0_f, c_f, ff, recv)
        if self.eeg_scale_auto and self._stack.currentWidget() is not self._overlay_page:
            # 量程基准固定取"最近一窗"，不看横轴缩放到哪里（详见 _auto_eeg_scale）。
            # 单通道放大时只按这一路算：放大就是要看它自己的细节，拿 14 路里最大的
            # 那一路定档，小信号会被压成一条线（这正是上一版的问题）。
            cw = min(recv, max(2, int(self.win_s * fe)))
            _, seg_base = self._take(self.buf_eeg, recv - cw, cw, fe, recv)
            if seg_base is None:
                seg_base = seg_e
            if seg_base is not None:
                view = seg_e
                if self._solo is not None and self._solo < self.n_eeg:
                    seg_base = seg_base[self._solo:self._solo + 1]
                    view = None if view is None else view[self._solo:self._solo + 1]
                self._auto_eeg_scale(seg_base, view)
        if seg_e is None and seg_f is None:
            if self._stack.currentWidget() is self._overlay_page:
                self._paint_overlay(None, None, None, None, '超出缓冲范围')
                return
            # 两端都取不到才整屏清空。旧写法是"一边取不到就全清"：fNIRS 缓冲只
            # 3000 点（500 Hz 下 6 s），缩到 6 s 以前那段时 seg_f 为 None，
            # 于是 CH1 明明有 30 s 数据也被一起抹掉 —— 现场就是"放大以后数据没了"。
            why = "超出缓冲（仅存最近 %.0f s）" % (self.buf_eeg.shape[1] / fe)
            for i in range(self.n_total):
                self._paint_missing(self.plots[i], self.curves[i], self._tips[i], i, why)
            if self._solo is not None:
                self._paint_missing(self._big, self._big_curve, self._big_tip,
                                    self._solo, why)
            return
        # 横轴：左边界由样本序号推出（累计模式会一路涨），14 个子图共用一把尺
        # （合同 S1「时间轴统一」）。只有跟随时才改范围，否则会抢掉用户的缩放。
        if self._follow:
            self._recalc_x_left()
            self._reset_x_range()
        if self._stack.currentWidget() is self._overlay_page:
            self._paint_overlay(xe, seg_e, xf, seg_f)
            return
        # 网格页的 14 张图。EEG / fNIRS 各画各的：一边的缓冲覆盖不到，只清这一边，
        # 另一边的曲线照画（这就是上面说的"整屏消失"的修法）。
        why_fn = "超出缓冲（仅存最近 %.0f s）" % (self.buf_fn.shape[1] / ff)
        for i in range(self.n_total):
            if i < self.n_eeg:
                if seg_e is None:
                    self._paint_missing(self.plots[i], self.curves[i], self._tips[i], i,
                                        "超出缓冲（仅存最近 %.0f s）"
                                        % (self.buf_eeg.shape[1] / fe))
                else:
                    self._paint(self.plots[i], self.curves[i], self._tips[i], i, xe, seg_e[i])
            elif seg_f is None:
                self._paint_missing(self.plots[i], self.curves[i], self._tips[i], i, why_fn)
            else:
                self._paint(self.plots[i], self.curves[i], self._tips[i], i, xf,
                            seg_f[i - self.n_eeg])
        # 大图页：同一份数据再画一次，量程只按这一路算
        if self._solo is not None:
            i = self._solo
            if i < self.n_eeg:
                if seg_e is None:
                    self._paint_missing(self._big, self._big_curve, self._big_tip, i,
                                        "超出缓冲（仅存最近 %.0f s）"
                                        % (self.buf_eeg.shape[1] / fe))
                else:
                    self._paint(self._big, self._big_curve, self._big_tip, i, xe, seg_e[i])
            elif seg_f is None:
                self._paint_missing(self._big, self._big_curve, self._big_tip, i, why_fn)
            else:
                self._paint(self._big, self._big_curve, self._big_tip, i, xf,
                            seg_f[i - self.n_eeg])

    def _view_bounds(self, plot):
        """当前视图的横轴范围(秒)，取不到就给窗口默认值。"""
        try:
            lo, hi = plot.getViewBox().viewRange()[0]
            return float(lo), float(hi)
        except Exception:  # noqa: BLE001
            return 0.0, self.win_s

    @staticmethod
    def _clamp_to_data(v0: float, v1: float, lo_ok: float, hi_ok: float,
                       min_span: float = 0.0):
        """把视图夹进 [lo_ok, hi_ok]（还有数据的区间）。

        不夹的话，缩出去/拖出去就会在曲线两端留下空白 —— 现场看到的是
        "时间轴 210~255，曲线只有 240~250 一段"，会以为前面的数据丢了。
        宽度本身比可用区间还大时也压到可用区间，保证整轴都有数据。
        min_span: 最小宽度（放最大时至少留 2 个采样），太窄会连一个点都取不到。
        """
        span = v1 - v0
        if not (span > 0) or not (hi_ok > lo_ok):
            return v0, v1
        span = min(span, hi_ok - lo_ok)
        span = max(span, min(min_span, hi_ok - lo_ok))
        # 宽度定下来之后，用右边界做锚点整体平移进区间。
        # （旧写法只在"越界"分支里改 v0/v1，宽度被 min_span 撑大时改不动，
        #   于是"放到最大"仍然只剩 0.005 s、屏幕上只有两个点。）
        v0 = v1 - span
        if v0 < lo_ok:
            v0 = lo_ok
        v1 = v0 + span
        if v1 > hi_ok:
            v1 = hi_ok
            v0 = v1 - span
        return v0, v1

    def _take(self, buf, i0: int, cnt: int, fs: float, recv: int):
        """从环形缓冲取绝对样本区间 [i0, i0+cnt)，返回 (x 秒, 二维切片)。

        缓冲里只有最近 B 个样本（B/fs 秒），更早的已经被覆盖；要的那段如果
        整段落在外面就返回 (None, None)，让调用方清屏而不是画错位的数据。
        点数超过 MAX_DRAW 就等间隔抽稀：缩到 150 s 时 14 张图各 7.5 万点会明显卡。
        """
        if cnt < 2:
            return None, None
        B = buf.shape[1]
        if buf is self.buf_hbo or buf is self.buf_hbr:
            # 浓度缓冲按绝对样本序号环形存储；显示时按时间顺序取视窗。
            first = max(0, i0, recv - B)
            last = min(recv, i0 + cnt)
            if last - first < 2:
                return None, None
            indexes = np.arange(first, last)
            x = indexes / fs
            seg = buf[:, indexes % B]
            if seg.shape[1] > MAX_DRAW:
                step = int(np.ceil(seg.shape[1] / MAX_DRAW))
                x = x[::step]; seg = seg[:, ::step]
            return x, seg
        off = recv - B                       # 缓冲第 0 列对应的绝对样本序号
        c0 = max(0, i0 - off); c1 = min(B, i0 + cnt - off)
        if c1 - c0 < 2:
            return None, None
        x = (off + np.arange(c0, c1)) / fs
        seg = buf[:, c0:c1]
        if seg.shape[1] > MAX_DRAW:
            step = int(np.ceil(seg.shape[1] / MAX_DRAW))
            x = x[::step]; seg = seg[:, ::step]
        return x, seg

    def _paint(self, plot, curve, tip, i: int, x, y):
        """把第 i 路画到指定的图上；14 图小图与单通道大图共用这一份逻辑。

        两条规则不变：
        1) ch_enable 是用户手动勾选，_ch_ok 是"这条链路确实收到过该通道"，
           两者都成立才画 —— 文本链路每行只发 1 列时，其余 3 路是补 0 占位，
           画出来就是一条贴 0 的假平线，现场会误判成"信号一直是 0"。
        2) 自动量程走分位数，单个毛刺不参与，否则一个坏帧就把整屏压平。
        """
        if i < self.n_eeg:
            if not (self.ch_enable[i] and self._ch_ok[i]):
                curve.setData([], [])
                tip.setText("无数据（本链路未发送该通道）"); tip.show()
                plot.setYRange(-1.0, 1.0, padding=0)
                self._hide_ylabels(i)
                return
            tip.hide()
            curve.setData(x, y)
            # 纵轴一律按"屏幕上这段数据"定：Y 轴已不接受鼠标缩放（setMouseEnabled(y=False)），
            # 所以不存在抢用户视野的问题。旧做法是手动缩放后冻结 Y，结果拖到直流漂移的
            # 另一段时曲线整段画在框外，屏幕上什么都没有 —— 现场当成"数据没了"。
            self._fit_y(plot, i, y)
            return
        if not (self.ch_enable[i] and self._ch_ok[i]):
            curve.setData([], [])
            tip.setText("无数据（本链路未发送该通道）"); tip.show()
            plot.setYRange(-0.01, 0.01, padding=0)
            self._hide_ylabels(i)
            return
        tip.hide()
        curve.setData(x, y)
        self._fit_y(plot, i, y)

    def _paint_missing(self, plot, curve, tip, i: int, why: str):
        """这一路当前取不到数据：清线、写明原因、收起旧读数。

        必须同时收起纵轴读数：曲线清空后视图范围还是上一帧的值，
        图上就有"两个六位数字 + 一片空白"，现场读到的是一组对不上的数
        （放大以后纵轴数值不对，就是这么来的）。
        """
        curve.setData([], [])
        if not (self.ch_enable[i] and self._ch_ok[i]):
            tip.setText("无数据（本链路未发送该通道）")
        else:
            tip.setText(why)
        tip.show()
        plot.setYRange(-1.0, 1.0, padding=0)
        self._hide_ylabels(i)

    def _hide_ylabels(self, i: int):
        """没有数据的通道不显示纵轴读数，否则旧数字会留在空图上。"""
        for arr in (self._ylbl_hi, self._ylbl_lo):
            if 0 <= i < len(arr):
                arr[i].hide()

    def _fit_y(self, plot, i: int, y):
        """按这一路可见数据的鲁棒中心/半幅设 Y 范围（与横轴是否手动缩放无关）。"""
        if i < self.n_eeg:
            if self.eeg_scale_auto and y.size:
                rs = self._robust_span(y)
                mid = rs[0] if rs is not None else float(np.median(y))
                if np.isfinite(mid):
                    # padding=0: pyqtgraph 默认再给 2% 边距，叠上自己的余量就更扁了
                    plot.setYRange(mid - self.eeg_scale, mid + self.eeg_scale, padding=0)
                else:
                    plot.setYRange(-self.eeg_scale, self.eeg_scale, padding=0)
            else:
                # 手动档: 保持 ±量程 的绝对语义, 不跟随信号漂移
                plot.setYRange(-self.eeg_scale, self.eeg_scale, padding=0)
        else:
            if self.fnirs_version:
                rs = self._robust_span(y)
                finite = np.asarray(y)[np.isfinite(y)]
                mid = rs[0] if rs is not None else (float(np.median(finite)) if finite.size else 0.0)
                half = max(rs[1] * 1.1 if rs is not None else 0.0, 10.0)
                plot.setYRange(mid - half, mid + half, padding=0)
            else:
                plot.setYRange(-self.fnirs_scale, self.fnirs_scale, padding=0)
        self._mark_grid_ticks(plot)

    def _mark_grid_ticks(self, plot):
        """网格小图在图内左上/左下写出纵轴上下限（六位原值，7pt）。

        为什么不交给坐标轴：14 图并排时轴只有三十几像素宽，六位数字会被整段裁掉
        —— 实测轴宽 35px、字宽 45px 时一个数字都看不见，现场就说"放大以前纵坐标没有数据"。
        用 QLabel 叠在绘图区上方：不受轴宽限制，也不挤占曲线；放大页仍是完整刻度。
        """
        if plot is getattr(self, "_big", None):
            return
        try:
            i = self.plots.index(plot)
        except ValueError:
            return
        if i >= len(self._ylbl_hi):
            return
        try:
            lo, hi = plot.getViewBox().viewRange()[1]
            lo = float(lo); hi = float(hi)
            if not (hi > lo):
                return
            big = max(abs(lo), abs(hi))
            fmt = "{:.0f}" if big >= 10 else "{:.3f}"
            key = (fmt.format(lo), fmt.format(hi))
            if self._ytick_cache.get(id(plot)) != key:
                self._ytick_cache[id(plot)] = key
                self._ylbl_hi[i].setText(key[1])
                self._ylbl_lo[i].setText(key[0])
            axw = int(plot.getAxis('left').width()) + 3
            # 两行读数都放在左上角（第一行=上限、第二行=下限）：
            # 放到图底会被 x 轴刻度区盖住，放到图内底部又会压住曲线。
            self._ylbl_hi[i].move(axw, 1)
            self._ylbl_lo[i].move(axw, 15)
            self._ylbl_hi[i].show(); self._ylbl_lo[i].show()
            self._ylbl_hi[i].raise_(); self._ylbl_lo[i].raise_()
        except Exception:  # noqa: BLE001
            return


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = Monitor14(); w.resize(1200, 800); w.show()
    sys.exit(app.exec_())

if __name__ == "__main__": main()
