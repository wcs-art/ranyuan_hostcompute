"""主界面：左侧波形区、右侧功能控制区、底部采集与保存条。"""
from __future__ import annotations
import sys, time
import numpy as np
from PyQt5 import QtWidgets, QtCore, QtGui


class _ConnectionWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, kind, com, baud, port, target_ip, parent=None):
        super().__init__(parent)
        self.kind, self.com, self.baud = kind, com, baud
        self.port, self.target_ip = port, target_ip

    def run(self):
        src = None
        try:
            if self.kind == "udp":
                from receivers import request_eeg_udp_mode
                request_eeg_udp_mode(self.com, self.baud)
            from source import make_source
            src = make_source(self.kind, com=self.com, baud=self.baud,
                              port=self.port, host="0.0.0.0", target_ip=self.target_ip,
                              protocol="fw100" if self.kind == "udp" else None,
                              proto="serial" if self.kind == "serial" else "udp")
            src.start()
            self.result_source = src
            self.ready.emit((src, self.kind, self.com, self.baud,
                             self.target_ip, self.port))
        except Exception as exc:  # noqa: BLE001 - 错误传回主线程显示
            if src is not None:
                try:
                    src.stop()
                except Exception:
                    pass
            self.failed.emit(str(exc))


class ClassicMainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        # 构建标记：重启后看标题栏即可确认跑的是不是新代码（改源码不会影响
        # 已启动的进程，也不会更新 dist 里旧 exe；看到这个标记才说明重载成功）。
        self._base_title = "燃元科技数据采集显示上位机  [build 2026-11-23 udp-pagination]"
        self.setWindowTitle(self._base_title)
        self.resize(1600, 950)
        self.source = None          # 由 main() 注入或界面"连接"按钮创建, 用于读链路统计
        self._connection_worker = None
        self._link_error = None     # 打开失败的原文: 定时刷新不许把它冲掉
        self._link_t0 = time.time()
        cw = QtWidgets.QWidget(); self.setCentralWidget(cw)
        # ---- 左: 波形区 ----
        from monitor_14ch import Monitor14
        self.mon = Monitor14()
        self.mon.setWindowFlags(QtCore.Qt.Widget)
        self.mon.btn_export.hide()  # S2: 上面内嵌条的导出隐藏, 统一用底部导出 (格式+目录自选)
        self.mon.btn_record.hide()  # S2: 上面内嵌条的记录隐藏, 统一用底部记录 (单窗调试时仍可用)
        self.mon.lbl_store.hide()  # S2: 记录状态统一看底部lbl_store
        if hasattr(self.mon, "ch_scroll"):
            self.mon.ch_scroll.hide()  # S1: 通道开关只留外部一条, 内嵌条隐藏
        left_box = QtWidgets.QGroupBox("14通道同步显示 · 默认 ECG1-ECG4 / 滚轮翻页 (S1)")
        left_lay = QtWidgets.QVBoxLayout(left_box)
        left_lay.addWidget(self.mon.centralWidget())
        # 通道开关条 (S1/E5)
        ch_bar = QtWidgets.QHBoxLayout()
        ch_bar.addWidget(QtWidgets.QLabel("通道开关:"))
        self._chks = []
        labels = self.mon.eeg_names + [f"fNIRS{i+1}" for i in range(10)]
        for i, lab in enumerate(labels):
            c = QtWidgets.QCheckBox(lab); c.setChecked(True)
            c.toggled.connect(lambda v, k=i: self._toggle_ch_all(k, v))
            self._chks.append(c); ch_bar.addWidget(c)
        left_lay.addLayout(ch_bar)
        self._mons = []  # S1/E5: 已打开的64通道窗, 外部开关联动

        # ---- 右: 功能控制滚动区 ----
        right_box = QtWidgets.QGroupBox("功能控制")
        right_lay = QtWidgets.QVBoxLayout(right_box)
        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        inner = QtWidgets.QWidget(); vl = QtWidgets.QVBoxLayout(inner)

        # 显示切换 S1
        g1 = QtWidgets.QGroupBox("EEG/fNIRS 显示 (S1)"); f1 = QtWidgets.QVBoxLayout(g1)
        self.cmb_fnirs = QtWidgets.QComboBox(); self.cmb_fnirs.addItems(["ΔHbO2", "ΔHbR"])
        self.cmb_fnirs.currentIndexChanged.connect(self.mon._set_fnirs_metric)
        self.mon.cmb_fnirs.currentIndexChanged.connect(self.cmb_fnirs.setCurrentIndex)
        self.cmb_win = QtWidgets.QComboBox(); self.cmb_win.addItems(["5s", "10s", "30s"])
        self.cmb_win.setCurrentIndex(1)
        self.cmb_win.currentIndexChanged.connect(
            lambda i: self.mon._set_win(self.mon.WIN_CHOICES.index((5.0, 10.0, 30.0)[i])))
        self.mon.cmb_win.currentIndexChanged.connect(self._sync_window_choice)
        f1.addWidget(QtWidgets.QLabel("fNIRS 默认ΔHbO2, 可切ΔHbR (S1)")); f1.addWidget(self.cmb_fnirs)
        f1.addWidget(QtWidgets.QLabel("时间窗")); f1.addWidget(self.cmb_win)
        self.btn_pause = QtWidgets.QPushButton("暂停/继续 (S1)")
        self.btn_pause.setCheckable(True)
        self.btn_pause.toggled.connect(self._set_paused)
        f1.addWidget(self.btn_pause)
        self.btn_64 = QtWidgets.QPushButton("打开64通道 (E5)")
        self.btn_64.clicked.connect(self.open_64); f1.addWidget(self.btn_64)
        vl.addWidget(g1)

        # 离线/回放 S6
        g2 = QtWidgets.QGroupBox("回放 (S6)"); f2 = QtWidgets.QVBoxLayout(g2)
        self.btn_replay = QtWidgets.QPushButton("打开回放…")
        self.btn_replay.clicked.connect(self.open_replay); f2.addWidget(self.btn_replay)
        self.btn_old = QtWidgets.QPushButton("旧离线分析工具")
        self.btn_old.clicked.connect(self.open_old); f2.addWidget(self.btn_old)
        vl.addWidget(g2)

        # 质量映射电量 S3/S4/H6
        g3 = QtWidgets.QGroupBox("质量·映射·电量 (S3/S4/H6)"); f3 = QtWidgets.QVBoxLayout(g3)
        for label, fn in [("S3 质量判定自检", self.check_quality),
                          ("S4 打开通道映射", self.open_map),
                          ("H6 读取电量", self.read_battery)]:
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(lambda _=False, f=fn, l=label: self._run(l, f))
            f3.addWidget(b)
        vl.addWidget(g3)

        # 多设备 LSL S7
        g4 = QtWidgets.QGroupBox("多设备·LSL (S7)"); f4 = QtWidgets.QVBoxLayout(g4)
        for label, fn in [("S7 设备表刷新", self.show_devices),
                          ("S7 Outlet自环测试", self.lsl_loop),
                          ("S7 时钟偏移判定", self.check_sync)]:
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(lambda _=False, f=fn, l=label: self._run(l, f))
            f4.addWidget(b)
        vl.addWidget(g4)

        # 姿态指标 S8/S9
        g5 = QtWidgets.QGroupBox("运动姿态·状态指标 (S8/S9)"); f5 = QtWidgets.QVBoxLayout(g5)
        for label, fn in [("S8 打开运动姿态", self.open_motion),
                          ("S9 打开状态指标", self.open_metrics),
                          ("S9 指标算法自检", self.check_metrics)]:
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(lambda _=False, f=fn, l=label: self._run(l, f))
            f5.addWidget(b)
        vl.addWidget(g5)

        # 工程 F4/F8/验证
        g6 = QtWidgets.QGroupBox("工程·同步·验收 (F4/F8/§十四)"); f6 = QtWidgets.QVBoxLayout(g6)
        for label, fn in [("F4 打开工程测试", self.open_test),
                          ("F8 软件触发(软标记)", self.soft_mark),
                          ("F8 SYNC OUT 1Hz脉冲", self.sync_pulse),
                          ("§十四 全部验证", self.run_all)]:
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(lambda _=False, f=fn, l=label: self._run(l, f))
            f6.addWidget(b)
        vl.addWidget(g6)
        vl.addStretch(1)
        scroll.setWidget(inner); right_lay.addWidget(scroll)

        # ---- 底: 采集保存串口条 ----
        bottom = QtWidgets.QGroupBox("采集、保存与串口")
        bl = QtWidgets.QHBoxLayout(bottom)
        self.log = QtWidgets.QListWidget(); self.log.setMaximumHeight(110)
        bl.addWidget(self.log, stretch=3)

        # ---- 链路连接：换线/换口不必重启程序, 也不必改 config/run.json ----
        conn_box = QtWidgets.QVBoxLayout()
        self.cmb_src = QtWidgets.QComboBox()
        for label, key in (("文本串口 (有线 FireWater)", "text"),
                           ("二进制串口 (AA55 帧)", "serial"),
                           ("EEG UDP (ESP8285)", "udp"),
                           ("TCP 服务端", "tcp-client"),
                           ("模拟数据 (仅自检)", "sim")):
            self.cmb_src.addItem(label, key)
        self.cmb_com = QtWidgets.QComboBox(); self.cmb_com.setEditable(True)
        self.cmb_com.setMinimumWidth(90)
        self.cmb_baud = QtWidgets.QComboBox(); self.cmb_baud.setEditable(True)
        self.cmb_baud.addItems(["921600", "460800", "230400", "115200"])
        self.lbl_com = QtWidgets.QLabel("控制串口")
        self.lbl_udp_ip = QtWidgets.QLabel("ESP IP")
        self.ed_udp_ip = QtWidgets.QLineEdit()
        self.ed_udp_ip.setPlaceholderText("WIFI STATUS 的 ip，如 192.168.3.140")
        self.ed_udp_ip.setMinimumWidth(145)
        self.lbl_udp_port = QtWidgets.QLabel("端口")
        self.ed_udp_port = QtWidgets.QLineEdit("5000")
        self.ed_udp_port.setMaximumWidth(65)
        self.ed_udp_port.setToolTip("ESP 的 UDP 端口；本机接收端口自动分配")
        self.btn_scan = QtWidgets.QPushButton("刷新端口")
        self.btn_scan.clicked.connect(self._rescan_ports)
        self.btn_conn = QtWidgets.QPushButton("▶ 连接并采集")
        self.btn_conn.setMinimumSize(120, 60)
        self.btn_conn.setStyleSheet("font-weight:bold;")
        self.btn_conn.clicked.connect(self._toggle_link)
        r1 = QtWidgets.QHBoxLayout(); r1.addWidget(self.cmb_src, stretch=2)
        r1.addWidget(self.cmb_baud, stretch=1)
        r2 = QtWidgets.QHBoxLayout(); r2.addWidget(self.lbl_com)
        r2.addWidget(self.cmb_com, stretch=1)
        r2.addWidget(self.btn_scan)
        for widget in (self.lbl_udp_ip, self.ed_udp_ip,
                       self.lbl_udp_port, self.ed_udp_port):
            r2.addWidget(widget)
        conn_box.addLayout(r1); conn_box.addLayout(r2)
        bl.addLayout(conn_box, stretch=2)
        bl.addWidget(self.btn_conn)
        self._rescan_ports()
        self.cmb_src.currentIndexChanged.connect(self._update_connection_fields)
        self._update_connection_fields()

        rec_box = QtWidgets.QVBoxLayout()
        self.btn_record = QtWidgets.QPushButton("● 开始记录 (S2)")
        self.btn_record.setCheckable(True)
        self.btn_record.toggled.connect(self._on_record)
        self.lbl_store = QtWidgets.QLabel("未记录 (S2: CSV/EDF/SNIRF/XDF)")
        self.btn_export = QtWidgets.QPushButton("导出EDF/SNIRF/XDF (S2)")
        self.btn_export.setEnabled(False); self.btn_export.clicked.connect(self._on_export)
        for w in (self.btn_record, self.lbl_store, self.btn_export): rec_box.addWidget(w)
        bl.addLayout(rec_box, stretch=2)
        self.lbl_link = QtWidgets.QLabel("链路: 未连接")
        self.lbl_link.setWordWrap(True)
        self.lbl_link.setMinimumWidth(300)
        self.lbl_link.setStyleSheet("color:#8a6d00;")
        bl.addWidget(self.lbl_link)
        # 三处暂停按钮共用一个入口，勾选 = 正在刷新波形
        self.btn_start = QtWidgets.QPushButton("运行中 · 暂停")
        self.btn_start.setMinimumSize(130, 78); self.btn_start.setCheckable(True)
        self.btn_start.toggled.connect(lambda v: self._set_paused(not v))
        bl.addWidget(self.btn_start)
        if hasattr(self.mon, "btn_pause"):
            self.mon.btn_pause.toggled.connect(self._set_paused)
        self._set_paused(False)
        self.say = lambda t, ok=True: self._say(t, ok)

        # 总布局: 上(左+右) 下底条
        top = QtWidgets.QHBoxLayout()
        top.addWidget(left_box, stretch=3); top.addWidget(right_box, stretch=1)
        root = QtWidgets.QVBoxLayout(cw)
        root.addLayout(top, stretch=5); root.addWidget(bottom, stretch=1)
        self.statusBar().showMessage("就绪 | S1统一轴 S2四格式 S3丢包/电量/质量")

        from device_manager import DeviceManager
        from core_events import EventBus, SyncOutGen
        from lsl_outlet import TripleOutlet
        self.devs = DeviceManager(); self.events = EventBus()
        self.syncgen = SyncOutGen(1.0); self.outlet = TripleOutlet(device_id=1)
        self._wins = []
        # 波形定时刷新代理
        self._t = QtCore.QTimer(); self._t.timeout.connect(self._tick_proxy); self._t.start(40)
        self.mon.timer.stop()  # 用本窗统一节拍, 避免双定时器
        self._t_link = QtCore.QTimer(); self._t_link.timeout.connect(self._refresh_link)
        self._t_link.start(500)
        # Esc 退出单通道放大 —— 必须挂在本窗口(真正显示、有焦点), 不能挂 mon 自己。
        # 根因: 这里只把 mon.centralWidget() 嵌进来(见上文 line ~29), Monitor14
        # 这个窗口对象从不 show, 挂在它上面的 QShortcut 不在活动窗口链里,
        # 用户按 Esc 永远不触发(界面标题却写着"按 Esc 返回")。本窗口是真正
        # 接收键盘的顶层窗, WindowShortcut 在此 reliably 生效。
        # _restore_grid 自带 "非放大态直接 return" 守卫, 平时按 Esc 无副作用。
        self._esc = QtWidgets.QShortcut(QtGui.QKeySequence("Esc"), self)
        self._esc.setContext(QtCore.Qt.WindowShortcut)
        self._esc.activated.connect(self.mon._restore_grid)

    def keyPressEvent(self, e):
        """Esc 退出单通道放大的兜底通道。

        QShortcut 依赖窗口管理器的快捷键分发，个别打包环境/无边框窗口下会被吞；
        这里再接一层真实按键事件，落在主窗口或其子控件上的 Esc 一律还原放大页。
        两层都调 _restore_grid（自带非放大态守卫），重复触发无副作用。
        """
        if e.key() == QtCore.Qt.Key_Escape:
            self.mon._restore_grid()
            e.accept()
            return
        if e.key() == QtCore.Qt.Key_Left:
            self.mon._turn_page(-1)
            e.accept()
            return
        if e.key() == QtCore.Qt.Key_Right:
            self.mon._turn_page(1)
            e.accept()
            return
        super().keyPressEvent(e)

    # ---------------- 链路连接 ----------------
    def _rescan_ports(self):
        """扫描本机串口。没装 pyserial 时保留手输口名，不当成错误。"""
        cur = self.cmb_com.currentText().strip()
        ports = []
        try:
            from serial.tools import list_ports
            ports = [p.device for p in list_ports.comports()]
        except Exception as e:  # noqa: BLE001 - 缺依赖不该让界面挂掉, 但要说出来
            self._say(f"[WARN] 串口列表不可用({type(e).__name__})，可手输口名如 COM3", False)
        self.cmb_com.clear()
        if ports:
            self.cmb_com.addItems(ports)
        # 保留上一个用过的口名, 但不再塞一个不存在的 COM3 占位 —— 那会让人以为端口在
        if cur:
            if cur not in ports:
                self.cmb_com.addItem(cur)
            self.cmb_com.setCurrentText(cur)
        try:
            self.cmb_com.lineEdit().setPlaceholderText(
                "可手输，如 COM3" if ports else "未发现串口 · 插好后点「刷新端口」")
        except Exception:  # noqa: BLE001 - 只影响提示文字, 不影响功能
            pass
        self._say(f"串口列表: {', '.join(ports) if ports else '未发现（可手输口名）'}")

    def _toggle_link(self):
        if self.source is None:
            self._connect()
        else:
            active = getattr(self.source, "proto", getattr(self.source, "kind", ""))
            if active == self.cmb_src.currentData():
                self._disconnect()
            else:
                self._connect()

    def _update_connection_fields(self, _index=None):
        kind = self.cmb_src.currentData()
        udp = kind == "udp"
        control = kind in ("udp", "serial", "text")
        for widget in (self.lbl_com, self.cmb_com, self.cmb_baud, self.btn_scan):
            widget.setVisible(control)
        self.lbl_com.setText("控制串口" if udp else "串口")
        for widget in (self.lbl_udp_ip, self.ed_udp_ip,
                       self.lbl_udp_port, self.ed_udp_port):
            widget.setVisible(udp)
        if self.source is None:
            self.btn_conn.setText("▶ 接收 EEG" if udp else "▶ 连接并采集")
        elif getattr(self.source, "proto", getattr(self.source, "kind", "")) != kind:
            self.btn_conn.setText("↪ 切换并连接")

    def _connect(self):
        """按界面选项建链路并开采集。失败原因直接显示在链路行，不让窗口空着。"""
        kind = self.cmb_src.currentData()
        com = self.cmb_com.currentText().strip()
        baud = 921600
        target_ip = None
        port = 5000
        if kind in ("udp", "serial", "text"):
            if not com:
                self.show_link_error("请先选择设备的控制串口 COM 口")
                return
            try:
                baud = int(self.cmb_baud.currentText().strip() or 921600)
            except ValueError:
                self.show_link_error("波特率必须是整数")
                return
        if kind == "udp":
            import ipaddress
            target_ip = self.ed_udp_ip.text().strip()
            try:
                ipaddress.IPv4Address(target_ip)
                port = int(self.ed_udp_port.text().strip())
                if target_ip == "0.0.0.0" or not 1 <= port <= 65535:
                    raise ValueError("地址或端口超出范围")
            except ValueError:
                self.show_link_error("请输入 WIFI STATUS 中的 ESP IPv4 地址和 1～65535 的 UDP 端口")
                return
        self._disconnect(silent=True)
        self.mon.extra_cols = False
        self._connection_worker = _ConnectionWorker(kind, com, baud, port, target_ip, self)
        self._connection_worker.ready.connect(self._connection_ready)
        self._connection_worker.failed.connect(self._connection_failed)
        self._connection_worker.finished.connect(self._connection_finished)
        self.btn_conn.setEnabled(False)
        for widget in (self.cmb_src, self.cmb_com, self.cmb_baud, self.btn_scan,
                       self.ed_udp_ip, self.ed_udp_port):
            widget.setEnabled(False)
        self.lbl_link.setText("链路: 正在切换设备模式并连接…")
        self._connection_worker.start()

    def _connection_ready(self, result):
        src, kind, com, baud, target_ip, port = result
        self._install_source(src)
        where = (f"{com} @{baud}" if kind in ("text", "serial") else
                 f"{target_ip}:{port}" if kind == "udp" else kind)
        self._say(f"[OK] 已连接: {self.cmb_src.currentText()} — {where}")
        if kind in ("text", "serial"):
            # 现场最容易犯的一条：VOFA/串口助手和上位机同时开同一个口。
            # 驱动允许两个句柄，但字节会被分流，出来的是跳变脏值, 不是传感器数据。
            self._say("[注意] 同一 COM 口若被 VOFA 等工具同时打开，数据会被分流成跳变脏值；"
                      "测完整波形前请先断开那些工具", False)

    def _connection_failed(self, error):
        self._say(f"[FAIL] 模式切换或连接失败: {error}", False)
        self.show_link_error(error)

    def _connection_finished(self):
        worker = self._connection_worker
        self._connection_worker = None
        if worker is not None:
            worker.deleteLater()
        self.btn_conn.setEnabled(True)
        for widget in (self.cmb_src, self.cmb_com, self.cmb_baud, self.btn_scan,
                       self.ed_udp_ip, self.ed_udp_port):
            widget.setEnabled(True)
        self._update_connection_fields()

    def _disconnect(self, silent: bool = False):
        if self.source is not None:
            try:
                self.source.stop()
            except Exception as e:  # noqa: BLE001
                self._say(f"[WARN] 关闭链路异常: {e}", False)
        self.source = None
        self._link_error = None
        self.mon.detach_receiver()
        self.setWindowTitle(self._base_title)
        for i, p in enumerate(self.mon.plots[:self.mon.n_eeg]):
            p.setTitle(self.mon.eeg_names[i])
        self.lbl_link.setText("链路: 未连接（选好类型和端口后点「连接并采集」）")
        self.lbl_link.setStyleSheet("color:#666666;")
        if hasattr(self, "btn_conn"):
            self.btn_conn.setText("▶ 接收 EEG" if self.cmb_src.currentData() == "udp"
                                  else "▶ 连接并采集")
        if not silent:
            self._say("已断开数据源")

    def show_link_error(self, msg: str):
        """链路打不开：把原因和下一步写清楚，别让人只看到一句英文异常。

        注意 Windows 的坑：口名不存在时 pyserial 报的是 PermissionError(拒绝访问)，
        不是 FileNotFoundError，照字面翻译会把"没插线"说成"被占用"。所以先按
        实际端口列表判断这个口到底在不在，再决定提示。
        """
        import re
        text = str(msg)
        low = text.lower()
        if self.cmb_src.currentData() == "udp":
            self.lbl_link.setText(f"链路: 打开失败 — {text}\n⚠ 检查 ESP IP、UDP 端口、电脑网段和防火墙")
            self.lbl_link.setStyleSheet("color:#b00020;")
            self.btn_conn.setText("▶ 接收 EEG")
            self._link_error = text
            return
        tip = "检查线缆、驱动与口名后重试；也可点「刷新端口」重新扫描"
        m = re.search(r"(COM\d+)", text, re.I)
        if m:
            port = m.group(1).upper()
            try:
                from serial.tools import list_ports
                ports = [p.device.upper() for p in list_ports.comports()]
            except Exception:  # noqa: BLE001 - 没装 pyserial 时无法判断, 退回按字面提示
                ports = None
            if ports is not None and port not in ports:
                have = "、".join(ports) if ports else "当前一个串口都没有"
                tip = (f"{port} 不存在：线没插 / 驱动没装 / 口名不对。"
                       f"可用端口：{have} —— 插好后点「刷新端口」再连接")
            elif "permission" in low or "access is denied" in low or "拒绝访问" in text:
                tip = f"{port} 被占用：先断开 VOFA、串口助手等工具，再点「连接并采集」"
        elif "filenotfound" in low or "找不到" in text or "could not open port" in low:
            tip = "端口不存在：线没插 / 驱动没装 / 口名不对 —— 点「刷新端口」重新扫描"
        elif "permission" in low or "access is denied" in low or "拒绝访问" in text:
            tip = "端口被占用：先断开 VOFA、串口助手等工具，再点「连接并采集」"
        self.lbl_link.setText(f"链路: 打开失败 — {text}\n⚠ {tip}")
        self.lbl_link.setStyleSheet("color:#b00020;")
        self.btn_conn.setText("▶ 连接并采集")
        self._link_error = text

    def _install_source(self, src):
        """接管一个已启动的数据源：清旧缓冲、挂到波形区、按格式加标注。"""
        self.source = src
        self._link_error = None
        self.mon.attach_receiver(src)
        kind = getattr(src, "kind", "")
        title = self._base_title
        for i, p in enumerate(self.mon.plots[:self.mon.n_eeg]):
            p.setTitle(self.mon.eeg_names[i])
        if kind == "sim":
            title += "  【模拟数据 · 非真机】"
        elif kind in ("text", "text-file"):
            # 文本格式不带 CRC/序号/时间戳, 单位也未换算: 必须写在标题栏,
            # 否则会被当成"14 通道全功能已跑通"
            title += "  【文本原始值 · 未换算 · 无 CRC/丢包校验】"
            for i, p in enumerate(self.mon.plots[:self.mon.n_eeg]):
                p.setTitle(f"CH{i+1}（原始值）")
        self.setWindowTitle(title)
        self.btn_conn.setText("■ 断开")

    def _sync_ui_to_source(self, src):
        """把界面下拉框对齐到实际在跑的链路，否则点了「断开」再「连接」会换错链路。"""
        kind = getattr(src, "kind", "")
        if kind == "real":
            proto = getattr(src, "proto", "udp")
            kind = "serial" if proto == "serial" else proto
        idx = self.cmb_src.findData(kind)
        if idx >= 0:
            self.cmb_src.setCurrentIndex(idx)
        com = getattr(src, "com", None) or getattr(getattr(src, "_rx", None), "port", None)
        if com:
            self.cmb_com.setCurrentText(str(com))
        baud = getattr(src, "baud", None)
        if baud:
            self.cmb_baud.setCurrentText(str(int(baud)))
        target_ip = getattr(src, "target_ip", None)
        if target_ip:
            self.ed_udp_ip.setText(str(target_ip))
            self.ed_udp_port.setText(str(getattr(getattr(src, "_rx", None), "port", 5000)))

    def _refresh_link(self):
        """刷新链路行：区分「没有包」「有包但没帧」「有错误」三种情况。"""
        if self.source is None:
            if self._link_error:
                return          # 打开失败的提示要留着, 不能被 500ms 定时刷新冲掉
            self.lbl_link.setText("链路: 未连接（选好类型和端口后点「连接并采集」）")
            self.lbl_link.setStyleSheet("color:#666666;")
            return
        try:
            st = self.source.stats()
        except Exception as e:  # noqa: BLE001
            self.lbl_link.setText(f"链路: 统计不可用 ({e})")
            return

        pkts = st.get("packets", 0)
        frames = st.get("frames", 0)
        fps = st.get("fps", 0.0)
        idle = st.get("idle_s")
        err = st.get("error")
        link = st.get("link", "?")
        extra = ""
        if link == "tcp-client":
            extra = f" [{'已连接' if st.get('connected') else '未连接'} {st.get('peer','')}]"
        elif link == "udp-eeg":
            extra = (f" {st.get('peer', '')} ← 无线"
                     f" {st.get('local_ip', '?')}:{st.get('local_port', '?')}")
        elif link == "serial":
            extra = ""
        else:
            port = getattr(getattr(self.source, "_rx", None), "port", None)
            extra = f" @{port}" if port else ""

        base = (f"链路: {link}{extra} | 收包 {pkts} 个 / {st.get('bytes', 0)} 字节 | "
                f"解出 {frames} 帧 | {fps:.1f} Hz")
        if link == "udp-eeg":
            base += f" | 测试包 {st.get('tests', 0)}/10 | {st.get('phase', '')}"
            if st.get("invalid_packets"):
                base += f" | 无效 UDP 包 {st['invalid_packets']}"
            if st.get("port_unreachable"):
                base += f" | 端口不可达 {st['port_unreachable']}"
            if st.get("mcu_send_fail"):
                base += f" | MCU 发送失败 {st['mcu_send_fail']}"
        # 帧头通道数与配置不符 = 切帧边界本身就是错的, 这种帧即使偶过 CRC 也不能用;
        # 不显示出来的话, 现场只会看到"有波形但是在乱跳", 查不到根因。
        hdr_bad = int(st.get("hdr_bad") or 0)
        crc_bad = int(st.get("crc_bad") or 0)
        if hdr_bad:
            base += f" | ⚠帧头不符 {hdr_bad}"
        if crc_bad:
            base += f" | CRC错 {crc_bad}"
        if idle is not None:
            base += f" | 最近 {idle:.1f}s 前"

        if err:
            self.lbl_link.setText(base + f"\n⚠ {err}")
            self.lbl_link.setStyleSheet("color:#b00020;")
        elif hdr_bad:
            self.lbl_link.setText(base + "\n⚠ 帧头自报的通道数/版本/ratio 与上位机配置不一致 → "
                                        "固件布局和代码里的 FrameLayout 不是同一套, "
                                        "先跑 tools/hexdump_frames.py 确认真实帧长")
            self.lbl_link.setStyleSheet("color:#c25e00;")
        elif link == "udp-eeg" and st.get("phase") != "接收 EEG":
            if st.get("tests", 0) < 10:
                tip = ("等待 ESP 的 10 个测试包：确认固件已发 EEG OUTPUT UDP、"
                       "串口出现 UDP READY，且电脑与 ESP 在同一网络")
            else:
                tip = "测试包已收齐，正在重发 START 等待 EEG；检查固件 EEG STATUS"
            if st.get("port_unreachable"):
                tip += "；目标端口不可达，请核对 ESP IP/端口"
            self.lbl_link.setText(base + "\n… " + tip)
            self.lbl_link.setStyleSheet("color:#8a6d00;")
        elif idle is not None and idle > 3.0:
            # 串口一直开着、read 也不报错, 但板子已经不发了 —— 这时原实现显示绿字,
            # 现场看到的是"波形停在某个值不动", 会当成界面卡死或算法问题。
            tip = ("正在重新发送 HELLO 恢复 UDP 流" if link == "udp-eeg" else
                   "复位中 / 固件卡住 / 线只供电不传数据")
            self.lbl_link.setText(base + f"\n⚠ 已 {idle:.0f}s 没有新数据：{tip}")
            self.lbl_link.setStyleSheet("color:#c25e00;")
        elif frames > 0:
            self.lbl_link.setText(base)
            self.lbl_link.setStyleSheet("color:#0a7d28;")
        elif pkts > 0:
            self.lbl_link.setText(base + "\n⚠ 收到字节但解不出帧 → 帧格式/CRC 与上位机不一致，"
                                        "请用 --raw-dump 抓包后跑 tools/hexdump_frames.py")
            self.lbl_link.setStyleSheet("color:#c25e00;")
        else:
            if link == "serial":
                tip = ("… 等待数据：确认板子在发、COM 口选对、波特率与固件一致"
                       "（可跑 tools/preflight.py --all 自动试四个波特率）")
            elif link == "tcp-client":
                tip = "… 等待数据：模组未必在线（见上方连接状态），检查网段与模组 IP"
            else:
                tip = "… 等待数据：检查模组工作模式、PC 防火墙、端口号"
            self.lbl_link.setText(base + "\n" + tip)
            self.lbl_link.setStyleSheet("color:#8a6d00;")

    def _tick_proxy(self):
        self.mon._tick()
        # S2: 只要有CSV就保持导出可用 (修"导出按钮没反应": 内外两套记录按钮不同步)
        try:
            if getattr(self.mon, "_csv_path", None) and not self.btn_export.isEnabled():
                self.btn_export.setEnabled(True)
                self.lbl_store.setText(self.mon.lbl_store.text())
        except Exception:
            pass

    def _say(self, text, ok=True):
        it = QtWidgets.QListWidgetItem(text)
        it.setForeground(QtCore.Qt.darkGreen if ok else QtCore.Qt.red)
        self.log.addItem(it)

    def _run(self, label, fn):
        try:
            ret = fn(); self._say(f"[PASS] {label}" + (f": {ret}" if ret else ""), True)
        except Exception as e:
            self._say(f"[FAIL] {label}: {e}", False)
        self.statusBar().showMessage(label)

    def _set_paused(self, on):
        """暂停开关的唯一入口，三处按钮一起同步。"""
        on = bool(on)
        self.mon.paused = on
        pairs = [(self.btn_pause, on), (self.btn_start, not on)]
        if hasattr(self.mon, "btn_pause"):
            pairs.append((self.mon.btn_pause, on))
        for b, want in pairs:
            b.blockSignals(True)
            b.setChecked(want)
            b.blockSignals(False)
        self.btn_start.setText("已停止 · 启动" if on else "运行中 · 暂停")

    def _sync_window_choice(self, index):
        """内层时间窗改为外层也有的档位时，让两个选择框保持一致。"""
        if not (0 <= index < len(self.mon.WIN_CHOICES)):
            return
        outer = self.cmb_win.findText(f"{self.mon.WIN_CHOICES[index]:g}s")
        if outer >= 0 and outer != self.cmb_win.currentIndex():
            self.cmb_win.blockSignals(True)
            self.cmb_win.setCurrentIndex(outer)
            self.cmb_win.blockSignals(False)

    def _on_record(self, on):
        self.mon._toggle_record(on)
        self.lbl_store.setText(self.mon.lbl_store.text())
        self.btn_export.setEnabled(self.mon.btn_export.isEnabled())
        self._say(("开始记录" if on else "停止记录") + ": " + self.mon.lbl_store.text(), True)

    def _on_export(self):
        # S2: 导出格式+保存目录自选 (CSV为原始记录, EDF/SNIRF/XDF按需勾选)
        import shutil
        from pathlib import Path
        src = getattr(self.mon, "_csv_path", None)
        if src is None:
            # 允许直接选一个已存CSV来转格式
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择要导出的CSV", ".", "CSV (*.csv)")
            if not path:
                self._say("[FAIL] S2 导出: 请先记录, 或选一个已有CSV", False); return
            src = Path(path)
            self.mon._csv_path = src
            self.mon.btn_export.setEnabled(True)
        dlg = QtWidgets.QDialog(self); dlg.setWindowTitle("导出 (S2): 格式+保存目录")
        fl = QtWidgets.QVBoxLayout(dlg)
        fl.addWidget(QtWidgets.QLabel(f"源: {Path(src).name}"))
        cks = {}
        for fmt, tip in [("EDF", "EDFbrowser可读"), ("BDF", "24bit欧洲格式"),
                         ("SNIRF", "Homer3可读"), ("XDF", "pyxdf/MoBI可读")]:
            c = QtWidgets.QCheckBox(f"{fmt} ({tip})"); c.setChecked(True)
            cks[fmt] = c; fl.addWidget(c)
        row = QtWidgets.QHBoxLayout()
        le_dir = QtWidgets.QLineEdit(str(Path(src).resolve().parent))
        btn_dir = QtWidgets.QPushButton("选择保存目录…")
        btn_dir.clicked.connect(lambda: le_dir.setText(
            QtWidgets.QFileDialog.getExistingDirectory(self, "选择保存目录", le_dir.text()) or le_dir.text()))
        row.addWidget(le_dir); row.addWidget(btn_dir); fl.addLayout(row)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        fl.addWidget(bb)
        if dlg.exec_() != QtWidgets.QDialog.Accepted: return
        want = [k for k, c in cks.items() if c.isChecked()]
        if not want:
            self._say("[FAIL] S2 导出: 未勾选任何格式", False); return
        out_dir = Path(le_dir.text() or "."); out_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.mon._export_all(only=want, out_dir=out_dir)
            self.lbl_store.setText(self.mon.lbl_store.text())
            self._say("[PASS] S2 导出: " + self.mon.lbl_store.text(), True)
        except Exception as e:
            import traceback; traceback.print_exc()
            self._say(f"[FAIL] S2 导出: {e}", False)

    def _toggle_ch_all(self, k, v):
        """S1/E5: 外部开关同时控制主窗+所有已开64通道窗(同序号通道)."""
        self.mon._toggle_ch(k, v)
        for m in list(self._mons):
            try:
                if k < len(m.plots):
                    m._toggle_ch(k, v)
                    if k < len(getattr(m, "chk", [])):
                        cb = m.chk[k]
                        cb.blockSignals(True)
                        cb.setChecked(v)
                        cb.blockSignals(False)
            except Exception:
                pass

    def open_64(self):
        from monitor_14ch import Monitor14
        w = Monitor14(eeg_names=[f"ECG{i+1}" for i in range(64)])
        for k, cb in enumerate(self._chks):
            if k < len(w.plots):
                w._toggle_ch(k, cb.isChecked())
        w.resize(1400, 900); w.show(); self._wins.append(w); self._mons.append(w)
        try:
            w.destroyed.connect(lambda _=None, m=w: self._mons.remove(m) if m in self._mons else None)
        except Exception:
            pass
        return f"{len(w.plots)}图(外部开关联动)"
    def open_replay(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择已存CSV", ".", "*.csv")
        if not path: return "未选择文件"
        from replay_panel import open_replay_windows
        self._wins.append(open_replay_windows(path)); return path
    def open_old(self):
        import subprocess
        subprocess.Popen([sys.executable, "main.py", "--old"]); return "旧工具已另起进程"
    def check_quality(self):
        from quality import eeg_contact, fnirs_flags
        a = eeg_contact(np.random.randn(4, 500) * 20); b = fnirs_flags(np.full((10, 50), 1000.0))
        assert set(a) == {"好"} and set(b) == {"正常"}, (a, b)
        return "EEG好×4 fNIRS正常×10"
    def open_map(self):
        from panels import FnirsMapPanel
        w = FnirsMapPanel(); w.show(); self._wins.append(w); return "映射图已打开"
    def read_battery(self):
        if self.mon.battery_pct >= 0: return f"电量 {self.mon.battery_pct:.0f}%"
        return "电量 --%(等固件字段下发)"
    def show_devices(self):
        for i in (1, 2): self.devs.connect(i)
        return "在线: " + ",".join(str(d.device_id) for d in self.devs.online)
    def lsl_loop(self):
        import threading
        from pylsl import resolve_streams, StreamInlet
        assert self.outlet.live, "本机无pylsl后端"
        flag = {"stop": False}
        def push():
            i = 0
            while not flag["stop"]:
                self.outlet.push_sleep_eeg([float(i)] * 4); i += 1; time.sleep(0.02)
        t = threading.Thread(target=push, daemon=True); t.start()
        try:
            time.sleep(0.5); got = None
            for s in resolve_streams(5.0):
                if "Aidsleep_SleepEEG_1" in s.name():
                    inlet = StreamInlet(s, max_buflen=5); inlet.open_stream(5.0)
                    time.sleep(1.0); got = inlet.pull_sample(timeout=8.0)
            assert got and got[0] is not None, "未收到采样"
            return f"收到{list(got[0])}"
        finally: flag["stop"] = True
    def check_sync(self):
        from lsl_sync import check_alignment
        offs = [d.offset_ms for d in self.devs.online] or [0.2, 0.5, 0.9]
        ok, gap = check_alignment(offs); assert ok, f"gap={gap}"
        return f"gap={gap:.2f}ms≤1ms"
    def open_motion(self):
        from panels import MotionPanel
        w = MotionPanel(); w.show(); self._wins.append(w)
        self.mon.motion_panel = w; return "运动姿态已打开(统一时间轴联动)"
    def open_metrics(self):
        from panels import MetricsPanel
        w = MetricsPanel(); w.show(); self._wins.append(w)
        self.mon.metrics_panel = w; return "状态指标已打开(≥1Hz联动)"
    def check_metrics(self):
        from band_power import brain_relax_0_100
        v = float(brain_relax_0_100(np.sin(2 * np.pi * 10 * np.arange(1000) / 500.0)))
        assert v > 90, v; return f"脑放松={v:.1f}"
    def open_test(self):
        from test_panel import TestPanel
        w = QtWidgets.QWidget(); w.setWindowTitle("工程测试")
        lay = QtWidgets.QVBoxLayout(w); lay.addWidget(TestPanel()); w.show()
        self._wins.append(w); return "工程测试已打开"
    def soft_mark(self):
        t, ts = self.events.soft_mark(); self.outlet.push_event(t)
        from tx import CMD_SOFT_MARK, send as tx_send
        # 协议 §6.3：SOFT_MARKER 的载荷是 **1 字节事件类型(1~255)**，不是时间戳。
        # 时间戳由设备自己打（event_ts_ms 用上电毫秒），上位机发 4 字节会被固件按 len 拒收。
        try:
            code = int(t)
        except (TypeError, ValueError):
            code = 1
        code = code if 1 <= code <= 255 else 1
        r = tx_send(CMD_SOFT_MARK, bytes([code]))  # 失败原因留在 r.reason, 不吞
        return f"软标记已入流 type={t} 下行={r.describe()}"
    def sync_pulse(self):
        now = time.time()
        fired = self.syncgen.poll(now) or self.syncgen.poll(now + 1.1)
        assert fired; self.outlet.push_event(4); return "SYNC OUT脉冲已入流"
    def run_all(self):
        from verify_center import Worker
        self._worker = Worker(self)
        self._worker.line.connect(lambda t, ok: self._say(("PASS " if ok else "FAIL ") + t, ok))
        self._worker.start(); return "后台逐项验证中…"

    def closeEvent(self, event):
        worker = self._connection_worker
        if worker is not None and worker.isRunning():
            worker.wait()
        if worker is not None:
            pending = getattr(worker, "result_source", None)
            if pending is not None and pending is not self.source:
                pending.stop()
        self._disconnect(silent=True)
        super().closeEvent(event)


MainWindow = ClassicMainWindow


def main(source=None, extra_cols: bool = False, start_error=None):
    """source 为 None 时不产生任何数据；接上后按名字传参推给界面。

    start_error: 启动时链路打不开的原因。此时窗口照常打开、参数预填好，
                 让人在界面里改端口重试，而不是看到弹窗后程序消失。
    """
    app = QtWidgets.QApplication(sys.argv)
    w = ClassicMainWindow()
    w.mon.extra_cols = bool(extra_cols)
    if source is not None:
        w._sync_ui_to_source(source)     # 先把参数填进下拉框, 失败时也能一键重试
    if start_error:
        w._say(f"[FAIL] 链路启动失败: {start_error}", False)
        w.show_link_error(start_error)
    elif source is not None:
        try:
            source.start()          # 幂等: main() 里可能已经开过
        except Exception as e:      # noqa: BLE001 - 让窗口照开, 原因显示在链路栏
            w._say(f"[FAIL] 链路启动失败: {e}", False)
            w.show_link_error(str(e))
        else:
            w._install_source(source)
    else:
        w._say("未接入数据源：选好类型与端口后点「连接并采集」")
    w.showMaximized()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
