"""运动姿态、状态指标、fNIRS 通道映射三个面板。"""
from __future__ import annotations
import time
import numpy as np
from PyQt5 import QtWidgets, QtCore
import pyqtgraph as pg


class MotionPanel(QtWidgets.QWidget):
    """运动姿态：三轴曲线 + 静止/活动占比。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("运动姿态 (S8)")
        self.fs = 100.0
        self.win_s = 10.0
        self.paused = False
        self.mode = "accel"
        self.buf = np.zeros((6, int(self.fs * 30)))
        self.att = np.zeros((3, int(self.fs * 30)))
        self.n_still = 0
        self.n_move = 0
        lay = QtWidgets.QVBoxLayout(self)
        bar = QtWidgets.QHBoxLayout()
        self.cmb = QtWidgets.QComboBox()
        self.cmb.addItems(["加速度", "角速度", "姿态角(俯仰/横滚/偏航)"])
        self.cmb.currentIndexChanged.connect(self._switch)
        self.btn = QtWidgets.QPushButton("暂停")
        self.btn.setCheckable(True)
        self.btn.toggled.connect(lambda v: setattr(self, "paused", v))
        self.lbl = QtWidgets.QLabel("静止 --% | 活动 --%")
        bar.addWidget(self.cmb)
        bar.addWidget(self.btn)
        bar.addWidget(self.lbl)
        lay.addLayout(bar)
        self.plot = pg.PlotWidget(title="运动姿态 XYZ (X红/Y黄/Z蓝)")
        self.plot.showGrid(x=True, y=True)
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.addLegend()
        self.c = [self.plot.plot(pen=p, name=n) for p, n in
                  [("r", "X"), ("y", "Y"), ("b", "Z")]]
        lay.addWidget(self.plot, stretch=3)
        # 环形占比: 用饼图三片 (静止/活动) 简化实现
        self.pie = pg.PlotWidget(title="静止/活动占比")
        self.pie.setAspectLocked(True)
        self.pie.hideAxis("left")
        self.pie.hideAxis("bottom")
        lay.addWidget(self.pie, stretch=1)
        self.t = QtCore.QTimer(self)
        self.t.timeout.connect(self._tick)
        self.t.start(100)

    def _switch(self, i):
        self.mode = ["accel", "gyro", "att"][i]
        titles = {"accel": "加速度 XYZ", "gyro": "角速度 XYZ",
                  "att": "姿态角 俯仰/横滚/偏航"}
        self.plot.setTitle(f"运动姿态 {titles[self.mode]} (X红/Y黄/Z蓝)")

    def push(self, acc_xyz, gyro_xyz=None, att_rpy=None):
        """与波形同节拍调用，共用统一时间轴。"""
        acc_xyz = np.asarray(acc_xyz, dtype=float).reshape(3)
        gyro_xyz = (np.asarray(gyro_xyz, dtype=float).reshape(3)
                    if gyro_xyz is not None else np.zeros(3))
        self.buf = np.roll(self.buf, -1, axis=1)
        self.buf[:, -1] = np.concatenate([acc_xyz, gyro_xyz])
        if att_rpy is None:
            try:
                from imu import attitude_rpy
                att_rpy = attitude_rpy(acc_xyz, gyro_xyz)
            except Exception:
                att_rpy = np.zeros(3)
        self.att = np.roll(self.att, -1, axis=1)
        self.att[:, -1] = np.asarray(att_rpy, dtype=float).reshape(3)
        try:
            from imu import still_active
            still = bool(still_active(acc_xyz, gyro_xyz))
        except Exception:
            still = float(np.linalg.norm(acc_xyz - np.array([0, 0, 1.0]))) < 0.3
        if still:
            self.n_still += 1
        else:
            self.n_move += 1

    def _tick(self):
        if self.paused:
            return
        n = int(self.fs * self.win_s)
        t = np.arange(n) / self.fs
        src = {"accel": self.buf[:3], "gyro": self.buf[3:],
               "att": self.att}[self.mode]
        for i in range(3):
            self.c[i].setData(t, src[i, -n:])
        tot = max(1, self.n_still + self.n_move)
        self.lbl.setText(f"静止 {self.n_still/tot:.0%} | 活动 {self.n_move/tot:.0%}")
        self.pie.clear()
        try:
            from PyQt5 import QtGui as _QG, QtCore as _QC
            import math
            vals = [self.n_still / tot, self.n_move / tot]
            if self.n_still + self.n_move == 0:
                vals = [1.0, 0.0]
                brushes = ["b", "r"]
                self.lbl.setText("等待姿态数据… (已显示占位环)")
            else:
                brushes = ["g", "r"]
            start = 0.0
            for frac, brush in zip(vals, brushes):
                if frac <= 0:
                    continue
                pts = [(0, 0)]
                for k in range(17):
                    a = start + frac * 2 * math.pi * k / 16
                    pts.append((math.cos(a), math.sin(a)))
                poly = QtWidgets.QGraphicsPolygonItem(
                    _QG.QPolygonF([_QC.QPointF(x, y) for x, y in pts]))
                poly.setBrush(pg.mkBrush(brush))
                self.pie.addItem(poly)
                start += frac * 2 * math.pi
            self.pie.setXRange(-1.2, 1.2)
            self.pie.setYRange(-1.2, 1.2)
        except Exception as e:
            self.lbl.setText(f"占比图绘制失败: {e}")


class MetricsPanel(QtWidgets.QWidget):
    """状态指标：脑激活/放松/专注/心率，数字 + 环形 + 趋势。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("状态指标 (S9)")
        self.labs = {}
        self.dials = {}
        lay = QtWidgets.QVBoxLayout(self)
        grid = QtWidgets.QGridLayout()
        for j, (k, title) in enumerate([("brain_active", "脑激活"), ("brain_relax", "脑放松"),
                                        ("focus", "专注度"), ("hr_bpm", "实时心率bpm")]):
            lb = QtWidgets.QLabel(f"{title}: --")
            dial = QtWidgets.QProgressBar()
            dial.setRange(0, 100)
            dial.setFormat("%v")
            self.labs[k] = lb
            self.dials[k] = dial
            grid.addWidget(QtWidgets.QLabel(title), 0, j)
            grid.addWidget(dial, 1, j)
            grid.addWidget(lb, 2, j)
        lay.addLayout(grid)
        self.plot = pg.PlotWidget(title="指标趋势 (≥1Hz刷新)")
        self.curves = {k: self.plot.plot(pen=p) for k, p in
                       zip(self.labs, "rgby")}
        self.hist = {k: [] for k in self.labs}
        lay.addWidget(self.plot)

    def push(self, active, relax, focus, hr01, bpm):
        vals = {"brain_active": active, "brain_relax": relax,
                "focus": focus, "hr_bpm": bpm}
        for k, v in vals.items():
            v = float(v)
            label = {"brain_active": "脑激活", "brain_relax": "脑放松",
                     "focus": "专注度", "hr_bpm": "实时心率bpm"}[k]
            if k == "hr_bpm":
                self.labs[k].setText(f"{label}: {v:.1f}bpm ({min(v,220)/220*100:.0f}分)")
                self.dials[k].setValue(int(min(max(v / 220 * 100, 0), 100)))
            else:
                self.labs[k].setText(f"{label}: {v:.1f}")
                self.dials[k].setValue(int(min(max(v, 0), 100)))
            self.hist[k].append(float(v))
            self.hist[k] = self.hist[k][-300:]
            self.curves[k].setData(self.hist[k])


class FnirsMapPanel(QtWidgets.QWidget):
    """fNIRS 通道映射，暂时占位，等甲方给布局坐标。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("fNIRS 通道映射 (S4, 待甲方坐标)")
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel("10通道 光源-探测器映射待甲方坐标表"))
        self.plot = pg.PlotWidget(title="S4 占位")
        xs = np.arange(10) % 5
        ys = np.arange(10) // 5
        self.plot.plot(xs, ys, pen=None, symbol="o")
        lay.addWidget(self.plot)
