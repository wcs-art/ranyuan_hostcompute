"""工程测试面板：脑电自检信号、灯珠强制点亮、读寄存器。"""
from __future__ import annotations

from PyQt5 import QtWidgets

from commands import Cmd, encode


class TestPanel(QtWidgets.QGroupBox):
    def __init__(self, send=None, dev: int = 1):
        super().__init__("头带工程测试")
        self.dev = int(dev)
        self.send = send or (lambda b: None)
        lay = QtWidgets.QHBoxLayout(self)
        self.b1 = QtWidgets.QPushButton("脑电自检信号")
        self.b2 = QtWidgets.QPushButton("头带灯珠点亮")
        self.reg = QtWidgets.QLineEdit("0x00")
        self.reg.setMaximumWidth(60)
        self.b3 = QtWidgets.QPushButton("读取头带参数")
        for w in (self.b1, self.b2, self.reg, self.b3):
            lay.addWidget(w)
        self.b1.clicked.connect(lambda: self.send(encode(self.dev, Cmd.TEST_SIGNAL, b"\x01")))
        # 灯珠恢复用同一条命令的 on=0
        self.b2.clicked.connect(lambda: self.send(encode(self.dev, Cmd.LED_FORCE, bytes([1, 1]))))
        self.b3.clicked.connect(self._read_reg)

    def _read_reg(self):
        """读取一个寄存器，地址填错时给中文提示，不把异常抛到界面。"""
        try:
            reg = int(self.reg.text().strip(), 0)
        except ValueError:
            self.b3.setText("地址无效")
            return
        self.b3.setText("读取头带参数")
        self.send(encode(self.dev, Cmd.REG_READ, bytes([0, reg & 0xFF])))
