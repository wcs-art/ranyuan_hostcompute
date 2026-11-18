"""缩放诊断：CH1(ECG1) 在不同缩放档下 纵轴量程 / 点数 是否稳定。

现场两个现象，都跑一遍看数值：
  A. 横向放大后纵轴读数变了（旧：5s 量程 3459 -> 0.2s 量程 1884）
  B. 放大后整屏空白（旧：视图落到 fNIRS 缓冲覆盖不到的时段，连 CH1 一起清掉）

用法: .venv/Scripts/python.exe tools/diag_zoom.py
数据源: capture/text_field.bin（现场抓的文本流），平移到 -12 万量级。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "ev1")]

import numpy as np
from PyQt5 import QtWidgets

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import monitor_14ch

raw = open(os.path.join(ROOT, "capture", "text_field.bin"), "rb").read()
base = np.array([float(x) for x in raw.decode("ascii", "ignore").split()], dtype=float)
base = base - base.mean()
FS = 500.0
N = int(40 * FS)
t = np.arange(N) / FS
vals = -120362.5 + base[np.arange(N) % base.size] - 30.0 * t   # 40 s 内漂 -1200

mon = monitor_14ch.Monitor14()
mon.fs_eeg = mon.fs_fnirs = FS
for i, v in enumerate(vals):
    mon.push_frame(seq=i + 1, eeg_uv=[float(v), 0, 0, 0], hbo=[0.0] * 10,
                   hbr=[0.0] * 10, raw=None, eeg_n=1)
mon._tick(force=True)
print(f"数据 {N} 点 / {N/FS:.0f}s, 值域 {vals.min():.0f}~{vals.max():.0f}")
print(f"缓冲 EEG {mon.buf_eeg.shape[1]} 点({mon.buf_eeg.shape[1]/FS:.0f}s) "
      f"fNIRS {mon.buf_fn.shape[1]} 点({mon.buf_fn.shape[1]/mon.fs_fnirs:.0f}s)\n")


def snap(tag):
    p = mon.plots[0]
    x0, x1 = p.getViewBox().viewRange()[0]
    y0, y1 = p.getViewBox().viewRange()[1]
    d = mon.curves[0].getData()
    n = 0 if d[0] is None else len(d[0])
    print(f"{tag:>12} | x[{x0:7.2f},{x1:7.2f}] w={x1-x0:6.2f} | 点数={n:5d} | "
          f"Y[{y0:9.0f},{y1:9.0f}] 量程={y1-y0:7.0f} | 读数 "
          f"{mon._ylbl_lo[0].text()}~{mon._ylbl_hi[0].text()}")
    return y1 - y0


print("=== A. 横向放大 50 倍（右端固定在最新），量程不该变 ===")
spans = []
for w in (10.0, 5.0, 2.0, 1.0, 0.5, 0.2):
    mon.btn_follow.setChecked(True)
    mon.plots[0].setXRange(40.0 - w, 40.0, padding=0)
    mon._on_manual_zoom(0)
    mon._tick(force=True)
    spans.append(snap(f"{w:g}s 窗口"))
print(f"-> 量程极差 {max(spans)-min(spans):.0f}（旧实现 3459 -> 1884，差 1575）\n")

print("=== B. 视图落在 15~25s（fNIRS 缓冲覆盖不到），CH1 必须还在 ===")
mon.btn_follow.setChecked(True)
mon._tick(force=True)
mon.plots[0].setXRange(15.0, 25.0, padding=0)
mon._on_manual_zoom(0)
mon._tick(force=True)
snap("15~25s")
print(f"-> fNIRS 提示: {mon._tips[mon.n_eeg].toPlainText()!r}")
