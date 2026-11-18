"""暂停/继续 + 放大的坐标轴诊断（E1 本机自测）。

用法: .venv/Scripts/python.exe tools/diag_pause.py
A: 手动缩放 -> 暂停 -> 推进数据 -> 继续，视野不该被抢走；
B: 暂停后双击放大，不该取帧、不该跳到最新一段；
C: 30 s 窗口但只采到 10 s 数据时的横轴/曲线覆盖。
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "ev1")]
from PyQt5 import QtWidgets
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import monitor_14ch

FS = 500.0
mon = monitor_14ch.Monitor14()
mon.fs_eeg = mon.fs_fnirs = FS


def push(n0, n1):
    for i in range(n0, n1):
        mon.push_frame(seq=i + 1, eeg_uv=[-166000.0 + (i % 40), 0, 0, 0],
                       hbo=[0.0] * 10, hbr=[0.0] * 10, raw=None, eeg_n=1)


def snap(tag, plot=None):
    p = plot or mon.plots[0]
    x0, x1 = p.getViewBox().viewRange()[0]
    y0, y1 = p.getViewBox().viewRange()[1]
    curve = mon._big_curve if p is mon._big else mon.curves[0]
    d = curve.getData()
    n = 0 if d[0] is None else len(d[0])
    dx = "" if n == 0 else f"{d[0][0]:.2f}~{d[0][-1]:.2f}"
    ax = p.getAxis("left")
    print(f"{tag:>22} | x[{x0:7.2f},{x1:7.2f}] w={x1-x0:5.2f} | 曲线 n={n:5d} x段={dx:>15} | "
          f"Y[{y0:9.0f},{y1:9.0f}] | units={ax.labelUnits!r} SI={ax.autoSIPrefix} "
          f"| follow={mon._follow} paused={mon.paused}")


push(0, int(40 * FS)); mon._tick(force=True)
snap("基线 40s")

print("\n=== A. 手动缩到 15~25s -> 暂停 -> 推进 5s -> 继续 ===")
mon.plots[0].setXRange(15, 25, padding=0); mon._on_manual_zoom(0); mon._tick(force=True)
snap("缩放后")
mon.paused = True
push(int(40 * FS), int(45 * FS))
snap("暂停中(未重绘)")
mon.paused = False
mon._tick(force=True)
snap("继续后")

print("\n=== B. 暂停中双击放大(force 只重绘，不取帧、不跳视野) ===")
mon.paused = True
mon._solo_plot(0)
snap("暂停后放大", mon._big)
push(int(45 * FS), int(50 * FS))
mon.paused = False
mon._tick(force=True)
snap("继续后", mon._big)

print("\n=== C. 30s 窗口，最新数据只有 10s 时 ===")
mon2 = monitor_14ch.Monitor14(); mon2.fs_eeg = mon2.fs_fnirs = FS
mon2.win_s = 30.0
for i in range(int(10 * FS)):
    mon2.push_frame(seq=i+1, eeg_uv=[-166000.0 + (i % 40), 0, 0, 0],
                    hbo=[0.0]*10, hbr=[0.0]*10, raw=None, eeg_n=1)
mon2._tick(force=True)
x0, x1 = mon2.plots[0].getViewBox().viewRange()[0]
d = mon2.curves[0].getData()
print(f"x[{x0:.2f},{x1:.2f}] n={len(d[0]) if d[0] is not None else 0} "
      f"曲线段={d[0][0]:.2f}~{d[0][-1]:.2f}" if d[0] is not None else "无数据")
