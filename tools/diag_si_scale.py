"""SI 缩放诊断：大量程下重设轴标签，刻度是否从 -161437 缩成 -161.5。

用法: .venv/Scripts/python.exe tools/diag_si_scale.py
期望: 两次 tick 都是六位原值，autoSIPrefixScale 恒为 1.0。
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "ev1")]
from PyQt5 import QtWidgets, QtGui
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import monitor_14ch

m = monitor_14ch.Monitor14()
m.fs_eeg = m.fs_fnirs = 500.0
for i in range(1000):
    m.push_frame(seq=i+1, eeg_uv=[-161437.0 + (i % 7) * 5, 0, 0, 0],
                 hbo=[0.0]*10, hbr=[0.0]*10, raw=None, eeg_n=1)
m.show()
for _ in range(4): app.processEvents()
m.paused = True
m._solo_plot(0)
for _ in range(4): app.processEvents()                  # 此时 Y 还是默认，scale 应为 1
ax = m._big.getAxis("left")
print("首次放大 SI=", ax.autoSIPrefix, "scale=", ax.autoSIPrefixScale,
      "label=", repr(ax.labelText), "prefix=", repr(ax.labelUnitPrefix))

def ticks():
    img = QtGui.QImage(10, 10, QtGui.QImage.Format_ARGB32)
    pa = QtGui.QPainter(img)
    if ax.style["tickFont"] is not None:
        pa.setFont(ax.style["tickFont"])
    ax.picture = None
    try:
        _, _, texts = ax.generateDrawSpecs(pa)
    finally:
        pa.end()
    return [t[2].strip() for t in texts][:6]

print("刻度(首次):", ticks())
# 现场等价动作：切链路/再放大时 _apply_axis_labels -> _set_left_label，
# 此刻 Y 量程已经是 -161437 量级
m._set_left_label(m._big, 0)
for _ in range(4): app.processEvents()
print("再设标签 SI=", ax.autoSIPrefix, "scale=", ax.autoSIPrefixScale,
      "prefix=", repr(ax.labelUnitPrefix), "标签文字=", ax.labelString())
print("刻度(再设标签):", ticks())
