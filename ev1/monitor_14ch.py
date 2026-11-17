"""14 通道实时波形：4 路 EEG + 10 通道 fNIRS，统一时间轴，可缩放、暂停、记录导出。"""
from __future__ import annotations
import sys, time, math, numpy as np
from PyQt5 import QtWidgets, QtCore
import pyqtgraph as pg

import contract   # _rec_row 依赖它; 缺失时 NameError 会被 _tick 的 except 吞掉,
                  # 表现为"点开始记录后一行都写不出来, 界面却毫无提示"。

N_EEG = 4; N_FNIRS = 10

#: 自动量程用的分位点。不能用 max/min 定量程: 现场一个半包毛刺就能把量程撑到
#: 上万倍, 真实信号被压成一条直线。实测例子: 数据在 -112666 附近波动 ±40,
#: 窗口里混进 1 个 -127968 的毛刺, 按 max-min 得量程 21933 -> 起伏只占 0.8%
#: 屏高, 界面上看起来"一条曲线都没有"; VOFA 同一段数据也是被撑成 283 倍。
AUTO_Q_LO = 0.5
AUTO_Q_HI = 99.5
#: 极端值超过鲁棒半幅的这么多倍才判为毛刺, 否则照样算进量程
#: (真实的大幅波动如眨眼/运动不该被裁掉)
AUTO_GLITCH_X = 4.0

class Monitor14(QtWidgets.QMainWindow):
    def __init__(self, eeg_names=None, sim=False):
        super().__init__()
        # sim 仅为兼容旧调用保留，界面不造数：没有数据就是平线
        self.sim = bool(sim)
        self._receiver = None
        #: 设备编号取自帧头 device_id，不能写死
        self._device_id = 1
        self.eeg_names = eeg_names or [f"EEG{i+1}" for i in range(N_EEG)]
        self.n_eeg = len(self.eeg_names)
        self.n_total = self.n_eeg + N_FNIRS
        self.setWindowTitle(f"{self.n_total}通道同步显示 (统一时间轴)")
        self.paused = False; self.show_hbo = True
        self.win_s = 10.0; self.fs_eeg = 500.0; self.fs_fnirs = 100.0
        # 采样率初值，接上设备后由实测帧率覆盖
        self.fs_measured = 0.0
        self._fs_snapped = False
        self.lux_per_lsb = 1.0   # 光强换算系数: 待标定, 改这里即可整体缩放
        # 光强换算系数统一从 derive 取，不在界面另写一份
        try:
            from derive import default_params
            self.lux_per_lsb = float(default_params().lux_per_lsb)
        except Exception:
            pass
        self._last_od = None     # 最近一帧的 20 路 ΔOD, 有则写入 CSV 的 OD 列
        # 前 n_baseline 帧建基线 I0，之后逐帧做 MBLL
        self.n_baseline = 200          # 200 帧 @100Hz = 2 s 基线窗
        self._base_buf = []            # 基线期收集的原始行
        self._base_rows = None         # 建好的 I0, 形状 (20,)
        self.n_mbll_singular = 0       # 矩阵奇异被置 NaN 的次数(现场坏道计数)
        self.extra_cols = False  # 是否写 S8 姿态/S9 指标扩展列(默认不写)
        self.eeg_scale = 100.0; self.fnirs_scale = 0.5
        # 轴单位: 二进制链路是折算后的 μV / mM; 文本链路的单位还没确认, 只能标"原始值"
        self.eeg_unit = "μV"
        self.fnirs_unit = "mM"
        # 自动改范围(时间轴滚动+量程)的开关。用户一旦手动缩放就关掉,
        # 否则每 40ms 被 setXRange/setYRange 抢回去, "支持缩放"等于没有。
        self._follow = True
        self.ch_enable = [True]*self.n_total
        # 每个通道是否真的收到过数据。文本链路只有 1 列时, 其余通道是补 0 的,
        # 若照画就会得到一条贴 0 的假平线, 现场会当成"信号一直是 0"。
        self._ch_ok = [False]*self.n_total
        self._t_rx = None            # 最近一次收到帧的时刻, 用于判断"停发"
        cw = QtWidgets.QWidget(); self.setCentralWidget(cw); lay = QtWidgets.QVBoxLayout(cw)
        bar = QtWidgets.QHBoxLayout()
        self.btn_pause = QtWidgets.QPushButton("暂停"); self.btn_pause.setCheckable(True)
        self.btn_pause.toggled.connect(lambda v: setattr(self, "paused", v))
        self.cmb_fnirs = QtWidgets.QComboBox(); self.cmb_fnirs.addItems(["ΔHbO2", "ΔHbR"])
        self.cmb_fnirs.currentIndexChanged.connect(lambda i: setattr(self, "show_hbo", i == 0))
        self.cmb_win = QtWidgets.QComboBox(); self.cmb_win.addItems(["5s", "10s", "30s"])
        self.cmb_win.setCurrentIndex(1)
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
        self.lbl = QtWidgets.QLabel("丢包率 0.0% | 电量 --% | seq连续 OK")
        # S3: 各通道信号质量的实时显示(此前只在内存里算, 从没画到界面上)
        self.lbl_quality = QtWidgets.QLabel("通道质量 --")
        self.lbl_quality.setStyleSheet("color:#888")
        self.btn_verify = QtWidgets.QPushButton("离线自检")
        self.btn_verify.clicked.connect(self._open_verify)
        for w in (self.btn_pause, self.cmb_fnirs, self.cmb_win, self.cmb_scale,
                  self.cmb_fscale, self.btn_follow, self.btn_verify, self.lbl):
            bar.addWidget(w)
        lay.addLayout(bar)
        qrow = QtWidgets.QHBoxLayout()
        qrow.addWidget(self.lbl_quality, 1)
        lay.addLayout(qrow)
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
        gl = QtWidgets.QGridLayout(); lay.addLayout(gl)
        names = ch_labels
        ncols = 2 if self.n_total <= 16 else 4
        for r, n in enumerate(names):
            p = pg.PlotWidget(title=n); p.showGrid(x=True, y=True)
            p.setMouseEnabled(x=True, y=True)  # 缩放
            # 合同 S1 要求「时间轴统一」: 横轴必须是同一个绝对量(秒), 不能各图各的
            p.setLabel('bottom', '时间', units='s')
            if r < self.n_eeg:
                p.setLabel('left', 'EEG', units=self.eeg_unit)
            else:
                p.setLabel('left', 'ΔHbO2/ΔHbR', units=self.fnirs_unit)
            p.setXRange(0, self.win_s, padding=0)
            p.getViewBox().sigRangeChangedManually.connect(self._on_manual_zoom)
            c = p.plot(pen="y")
            # 空着的子图不能只是一片黑: 现场分不清"这条链路没送这个通道"和"程序没跑"
            tip = pg.TextItem("等待数据…", color="#909090", anchor=(0.5, 0.5))
            tip.setPos(self.win_s / 2.0, 0.0); tip.hide()
            p.addItem(tip); self._tips.append(tip)
            gl.addWidget(p, r // ncols, r % ncols); self.plots.append(p); self.curves.append(c)
        self.t0 = time.time()
        self.buf_eeg = np.zeros((self.n_eeg, int(self.fs_eeg*30)))
        self.buf_fn = np.zeros((10, int(self.fs_fnirs*30)))
        self.buf_raw = np.zeros((20, int(self.fs_fnirs*30)))
        self.battery_pct = -1.0
        self.eeg_quality = ["--"]*self.n_eeg; self.fnirs_quality = ["--"]*10
        self.timer = QtCore.QTimer(); self.timer.timeout.connect(self._tick); self.timer.start(40)
        self._seq_last = -1; self._lost = 0; self._recv = 0; self._sim_seq = 0
        self._writer = None; self._csv_path = None; self._n_saved = 0
        self._t0 = time.time()
        self._q_tick = 0
        self._st_tick = 0
        # 提前建默认值：push_frame 会读它，建晚了首帧就抛 AttributeError
        self._metrics = {"brain_active": 50.0, "brain_relax": 50.0, "focus": 50.0, "hr_bpm": 0.0}
    def _toggle_ch(self, k: int, v: bool):
        self.ch_enable[k] = v
        self.plots[k].setVisible(v)
    def _set_eeg_scale(self, i: int):
        """幅度标尺: 0=自动, 1~3=固定 ±100/±200/±500μV。"""
        self.eeg_scale_auto = (i == 0)
        if not self.eeg_scale_auto:
            self.eeg_scale = (100.0, 200.0, 500.0)[max(0, min(i - 1, 2))]
    # ---- 时间轴 / 跟随 ----
    def _set_win(self, i: int):
        """时间窗切换后立刻重设横轴，不然要等下一次重绘才对齐。"""
        self.win_s = (5.0, 10.0, 30.0)[i]
        self._reset_x_range()
    def _reset_x_range(self):
        """14 个子图统一横轴：0 ~ win_s 秒，0 = 窗口最左（最早），右端 = 最新数据。"""
        for i, p in enumerate(self.plots):
            p.setXRange(0, self.win_s, padding=0)
            if i < len(self._tips):
                self._tips[i].setPos(self.win_s / 2.0, 0.0)
    def _set_follow(self, v: bool):
        self._follow = bool(v)
        if self._follow:
            self._reset_x_range()
    def _on_manual_zoom(self, *_):
        """用户拖过/滚过就停止自动改范围，否则每 40ms 被抢回，缩放等于没有。"""
        if self._follow:
            self._follow = False
            self.btn_follow.setChecked(False)
            self.lbl.setText("已手动缩放：不再自动跟随，点「跟随最新」恢复")
    def _apply_axis_labels(self):
        """单位随链路变：二进制是折算后的 μV，文本是未换算的原始值。"""
        for i, p in enumerate(self.plots):
            if i < self.n_eeg:
                p.setLabel('left', 'EEG', units=self.eeg_unit)
            else:
                p.setLabel('left', 'ΔHbO2/ΔHbR', units=self.fnirs_unit)
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

    def _auto_eeg_scale(self, seg):
        """按实测峰峰值定半幅量程，只改显示范围，不改数据本身。

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
            return
        if not np.isfinite(peak) or peak <= 0:
            return
        target = peak * 1.15                     # 留 15% 余量, 峰值不贴边
        base = 10.0 ** math.floor(math.log10(max(target, 1.0)))
        want = 10.0
        for k in (1.0, 2.0, 5.0, 10.0):          # 取 1/2/5×10^n 这一档, 刻度好读
            if target <= k * base:
                want = k * base
                break
        want = max(want, 10.0)                   # 下限 ±10μV, 避免噪声把量程压到看不见
        # 回差: 只在跨过阈值时才改, 否则量程会随每个心动周期来回跳。
        # 下降放宽到 1/4: 开机冲击(几万)过去后必须能降回来, 否则会被一次性拉高锁死,
        # 之后整段信号都被压成一条平线。
        if want > self.eeg_scale * 1.6 or want < self.eeg_scale / 4.0:
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
            self._n_saved = 0; self._t0 = time.time()
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
        from exporters import export_edf, export_snirf, export_xdf
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
            for fmt, tip in [("EDF", "EDFbrowser可读"), ("SNIRF", "Homer3可读"),
                             ("XDF", "pyxdf/MoBI可读")]:
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
        if "SNIRF" in want:
            try:
                export_snirf(self._csv_path, base + ".snirf"); parts.append("SNIRF[OK]")
            except Exception as e: parts.append(f"SNIRF跳过({e})")
        if "XDF" in want:
            try:
                n = min(2000, self.buf_eeg.shape[1])
                streams = {"Aidsleep_EEG_1": (self.buf_eeg[:, -n:], self.fs_eeg, "EEG"),
                            "Aidsleep_fNIRS_1": (self.buf_fn, self.fs_fnirs, "fNIRS")}
                if hasattr(self, "buf_imu"):
                    m = min(2000, self.buf_imu.shape[1])
                    streams["Aidsleep_IMU_1"] = (self.buf_imu[:, -m:], 100.0, "IMU")
                if hasattr(self, "buf_met"):
                    k = min(600, self.buf_met.shape[1])
                    streams["Aidsleep_Metrics_1"] = (self.buf_met[:, -k:], 1.0, "Metrics")
                export_xdf(streams, base + ".xdf")
                parts.append("XDF[OK]")
            except Exception as e: parts.append(f"XDF跳过({e})")
        self.lbl_store.setText(" ".join(parts))
    def _rec_row(self, seq, eeg_uv, hbo, hbr, acc_xyz=None, gyro_xyz=None,
                 raw=None, battery=-1.0, ts_abs_ms=0, event_type=0, event_ts=0,
                 device_id=None):
        """写一行 CSV：先组装成 Frame 再交给契约转行，表头和行同源，不会错位。"""
        if self._writer is None: return
        t = time.time()-self._t0
        n_inten = contract.N_FNIRS*len(contract.FNIRS_WLS)
        raw20 = [float(v) for v in (raw or [])][:n_inten]
        raw20 += [0.0]*(n_inten-len(raw20))
        fr = contract.Frame(
            # 合同 F2 要求"支持上位机区分 ≥8 台设备同时在线" —— 必须写帧头里的真实编号,
            # 不能写死 1: 两台设备同时录制会混成一份无法分辨的记录。
            device_id=int(self._device_id if device_id is None else device_id),
            seq=int(seq), t_device_ms=int(round(t*1000)),
            t_host_s=t, t_abs_ms=int(ts_abs_ms or 0),
            eeg_uv=[float(v) for v in (eeg_uv or [])],
            fnirs_raw=contract.unnest_raw(raw20),
            fnirs_intensity=[round(v*self.lux_per_lsb, 4) for v in raw20],
            od=list(getattr(self, "_last_od", None) or []) or None,   # 已建基线即写真值
            hbo=[float(v) for v in (hbo or [])],
            hbr=[float(v) for v in (hbr or [])],
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
        """20 路光强码值 -> (ΔHbO[10], ΔHbR[10])；基线未建完时返回 None。"""
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
            from derive import raw_to_lux, od_delta, mbll_batch
            lux = raw_to_lux(r)                      # 码值 -> 光强
            od = od_delta(lux, self._base_rows)      # ΔOD = -ln(I/I0)
            res = mbll_batch(od[0::2], od[1::2])     # 760 / 850 两支
            self.n_mbll_singular += int(res.n_singular)
            hbo = np.nan_to_num(res.hbo_mM, nan=0.0)
            hbr = np.nan_to_num(res.hbr_mM, nan=0.0)
            return hbo.tolist(), hbr.tolist(), od.tolist()
        except Exception:
            return None

    def fnirs_state(self) -> str:
        """状态栏用的 fNIRS 计算状态, 用于区分"没数据"和"没算完"。"""
        if self._base_rows is None:
            return f"基线 {len(self._base_buf)}/{self.n_baseline}"
        if self.n_mbll_singular > 0:
            return f"正常(奇异{self.n_mbll_singular})"
        return "正常"

    def push_frame(self, seq, eeg_uv, hbo, hbr, raw=None, battery=-1.0, acc_xyz=None, gyro_xyz=None,
                   ts_abs_ms=0, event_type=0, event_ts=0, device_id=1, eeg_n=None):
        self._t_rx = time.time()
        # eeg_n 是"本帧实际带了几路 EEG"。文本链路每行只有 1 个数时传 1,
        # 其余 3 路是补 0 的占位, 不能当数据画出来。
        n_ok = self.n_eeg if eeg_n is None else max(0, min(int(eeg_n), self.n_eeg))
        for i in range(n_ok):
            self._ch_ok[i] = True
        if raw is not None:
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
        if battery >= 0: self.battery_pct = float(battery)
        self.buf_eeg = np.roll(self.buf_eeg, -1, axis=1); self.buf_eeg[:, -1] = eeg_uv
        # 有真实光强就用真实值算 ΔHbO/ΔHbR; 算不出来(基线未建完/坏道)沿用入参(通常为 0)
        _d = self._derive_fnirs(raw)
        if _d is not None:
            hbo, hbr, self._last_od = _d
        f = hbo if self.show_hbo else hbr
        self.buf_fn = np.roll(self.buf_fn, -1, axis=1); self.buf_fn[:, -1] = f
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
        self._rec_row(seq, eeg_uv, hbo, hbr, acc_xyz, gyro_xyz, raw=raw, battery=battery,
                      ts_abs_ms=ts_abs_ms, event_type=event_type, event_ts=event_ts)
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
            from quality import eeg_contact, fnirs_flags
            try:
                self.eeg_quality = eeg_contact(self.buf_eeg[:, -500:])
                # buf_raw 是 20 行(10 通道 × 2 波长), 而 S3 判的是 10 个通道 ——
                # 直接把 20 行喂进去会得到 20 个状态, 界面上的通道号就对不上实物。
                r = self.buf_raw[:, -50:]
                if r.shape[0] == contract.N_FNIRS * len(contract.FNIRS_WLS):
                    r = r.reshape(contract.N_FNIRS, len(contract.FNIRS_WLS), -1).mean(axis=1)
                self.fnirs_quality = fnirs_flags(r)
            except Exception: pass
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
        self.lbl_quality.setText(f"<b>EEG</b> {eeg} &nbsp;|&nbsp; <b>fNIRS</b> {fs}")

    def reset_buffers(self):
        """换数据源前清空波形与分析状态，避免上一条链路的旧数据混进新曲线。"""
        self.buf_eeg[:] = 0.0
        self.buf_fn[:] = 0.0
        self.buf_raw[:] = 0.0
        self._seq_last = -1
        self._lost = 0
        self._recv = 0
        self._sim_seq = 0
        self.fs_measured = 0.0
        self._fs_snapped = False      # 换源后重新按实测帧率校准
        self.eeg_scale = 100.0
        self._ch_ok = [False]*self.n_total     # 换源后重新判定哪些通道有数据
        self._t_rx = None
        self.eeg_quality = ["--"] * self.n_eeg
        self.fnirs_quality = ["--"] * 10

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

    def _tick(self):
        if self.paused: return
        # 唯一数据入口: _receiver。没有 receiver 就不产生任何数据(不造数)。
        if self._receiver is not None:
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
        if self._st_tick % 5 == 0:
            self._refresh_status()
        n = int(self.fs_eeg*self.win_s); m = int(self.fs_fnirs*self.win_s)
        # 长度必须夹到缓冲宽度：不相等时绘图库抛异常，PyQt5 会直接 abort 退出
        n = max(32, min(n, self.buf_eeg.shape[1]))
        m = max(16, min(m, self.buf_fn.shape[1]))
        # 只画真数据：一帧都没收到，或板子已停发超过 2 s，就把曲线清空。
        # 否则缓冲区里的历史波形会一直留在屏上，看着像板子还在发。
        stale = self._t_rx is None or (time.time() - self._t_rx) > 2.0
        if self._recv == 0 or stale:
            for c in self.curves:
                c.setData([], [])
            for tip in self._tips:
                tip.setText("等待数据…"); tip.show()
            return
        # 缓冲还没填满时，前面的 0 是补位不是数据。拿它算峰峰值会把量程放大一倍
        # （0 到 -8000 的跳变），之后信号一直被压成平线。只画已收到的样本。
        if self._recv < n:
            n = max(2, int(self._recv))
        if self._recv < m:
            m = max(2, int(self._recv))
        if self.eeg_scale_auto:
            self._auto_eeg_scale(self.buf_eeg[:, -n:])
        # 横轴从 0 向右递增：最左是窗口里最早的一帧，最右是最新一帧，14 个子图共用一把尺
        # （合同 S1「时间轴统一」）。之前用负值表示"距今多少秒"，读数要反着看，现场容易误判。
        fe = max(self.fs_eeg, 1e-6); ff = max(self.fs_fnirs, 1e-6)
        te = np.arange(n) / fe
        tf = np.arange(m) / ff
        if self._follow:
            self._reset_x_range()
        for i in range(self.n_eeg):
            # ch_enable 是用户手动勾选，_ch_ok 是"这条链路该通道确实收到过数据"。
            # 两者都成立才画：文本链路每行只发 1 列时，其余 3 路只是补 0 的占位，
            # 画出来就是一条贴 0 的假平线，现场会误判成"信号一直是 0"。
            if not (self.ch_enable[i] and self._ch_ok[i]):
                self.curves[i].setData([], [])
                self._tips[i].setText("无数据（本链路未发送该通道）")
                self._tips[i].show()
                self.plots[i].setYRange(-1.0, 1.0)
                continue
            self._tips[i].hide()
            seg = self.buf_eeg[i, -n:]
            self.curves[i].setData(te, seg)
            if not self._follow:
                continue          # 手动缩放中: 不再抢用户的视野
            if self.eeg_scale_auto and seg.size:
                # 自动档: 窗口跟着信号中心走, 带直流偏置的原始值也能看清细节。
                # 中心与量程都走分位数, 单个毛刺不会把整屏压平。
                rs = self._robust_span(seg)
                mid = rs[0] if rs is not None else float(np.median(seg))
                if np.isfinite(mid):
                    self.plots[i].setYRange(mid - self.eeg_scale, mid + self.eeg_scale)
                else:
                    self.plots[i].setYRange(-self.eeg_scale, self.eeg_scale)
            else:
                # 手动档: 保持 ±量程 的绝对语义, 不跟随信号漂移
                self.plots[i].setYRange(-self.eeg_scale, self.eeg_scale)
        for i in range(10):
            k = self.n_eeg + i
            if not (self.ch_enable[k] and self._ch_ok[k]):
                self.curves[k].setData([], [])
                self._tips[k].setText("无数据（本链路未发送该通道）")
                self._tips[k].show()
                self.plots[k].setYRange(-0.01, 0.01)
                continue
            self._tips[k].hide()
            self.curves[k].setData(tf, self.buf_fn[i, -m:])
            self.plots[k].setYRange(-self.fnirs_scale, self.fnirs_scale)

def main():
    app = QtWidgets.QApplication(sys.argv)
    w = Monitor14(); w.resize(1200, 800); w.show()
    sys.exit(app.exec_())

if __name__ == "__main__": main()
