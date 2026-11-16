"""启动闪退诊断脚本（临时工具，不属于产品代码）。

用法：.venv/Scripts/python.exe tools/diag_start.py
输出：tools/diag_start.log
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ev1"))

LOG = Path(__file__).with_suffix(".log")
_fh = open(LOG, "w", encoding="utf-8", buffering=1)


def log(*a):
    print(*a, file=_fh)


from PyQt5 import QtCore, QtWidgets  # noqa: E402


class Spy(QtCore.QObject):
    """全局事件过滤器：记录 Close / Hide 事件，定位谁关闭了主窗口。"""

    def __init__(self):
        super().__init__()
        self.n = 0

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QtCore.QEvent.Close, QtCore.QEvent.Hide):
            kind = "CLOSE" if t == QtCore.QEvent.Close else "HIDE"
            try:
                name = obj.objectName()
                cls = type(obj).__name__
            except Exception:
                name, cls = "?", "?"
            self.n += 1
            if self.n <= 40:
                log(f"[{kind}] {cls} name={name!r}")
                log("".join(traceback.format_stack(limit=8)))
        return False


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    spy = Spy()
    app.installEventFilter(spy)

    from source import make_source
    from main_window import ClassicMainWindow

    log("[1] QApplication 就绪 platform=", app.platformName())
    src = make_source("sim", port=5000, proto="udp", path=None)
    w = ClassicMainWindow()
    log("[2] 主窗口构造完成")
    src.start()
    log("[3] source.start() 完成")
    w.mon.attach_receiver(src)
    w.showMaximized()
    log("[4] showMaximized 完成 visible=", w.isVisible())

    app.lastWindowClosed.connect(lambda: log("[!!] lastWindowClosed 触发"))
    w.destroyed.connect(lambda: log("[!!] 主窗口 destroyed"))

    t0 = time.time()
    state = {"n": 0}

    def tick():
        state["n"] += 1
        if state["n"] % 2 == 0:
            log(f"t={time.time()-t0:4.1f}s visible={w.isVisible()} top={len(QtWidgets.QApplication.topLevelWidgets())}")
        if state["n"] >= 20:
            log("[监控] 10s 仍在运行（正常）")
            app.quit()

    tm = QtCore.QTimer()
    tm.timeout.connect(tick)
    tm.start(500)

    code = app.exec_()
    log(f"[5] exec_ 返回 {code}，历时 {time.time()-t0:.2f}s")
    _fh.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
