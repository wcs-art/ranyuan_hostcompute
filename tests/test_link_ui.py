"""链路打开失败时的界面行为回归。

为什么需要这一层: 原 main() 在 src.start() 抛错后直接 SystemExit(2), 现场表现是
"不插线就弹个框、然后程序没了", 连改端口的机会都没有。修完之后还有第二个坑:
链路行被 500ms 的定时刷新覆盖成"未连接", 报错一闪而过。这两条都只有跑界面才能暴露。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("pyqtgraph")

ERR = ("串口 COM99 打不开: could not open port 'COM99': "
       "PermissionError(13, '拒绝访问。', None, 5)")


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def win(app):
    import main_window
    w = main_window.ClassicMainWindow()
    yield w
    w.close()


def test_error_survives_periodic_refresh(win):
    """打开失败的提示不能被定时刷新冲掉 —— 否则现场只看到一闪而过的红字。"""
    win.show_link_error(ERR)
    assert "打开失败" in win.lbl_link.text()

    for _ in range(5):
        win._refresh_link()          # 模拟 500ms 定时器连跑 5 次

    text = win.lbl_link.text()
    assert "打开失败" in text
    assert "COM99" in text
    assert win._link_error                 # 错误状态还在
    assert win.btn_conn.text().startswith("▶")   # 还能再点一次


def test_error_cleared_after_successful_install(win):
    """连上之后错误状态必须清掉，否则断开又显示上次的旧报错。"""

    class _Fake:
        kind = "sim"

        def start(self):
            pass

        def stop(self):
            pass

        def poll_frames(self):
            return []

        def stats(self):
            return {}

        def measured_fps(self):
            return 0.0

    win.show_link_error(ERR)
    win._install_source(_Fake())
    assert win._link_error is None
    win._disconnect(silent=True)
    assert win._link_error is None
    assert "未连接" in win.lbl_link.text()
    win._refresh_link()
    assert "打开失败" not in win.lbl_link.text()


def test_port_list_has_no_fake_entry(win):
    """没插线时不能凭空塞一个 COM3，否则界面说"有口"、一点就报错。"""
    from serial.tools import list_ports
    real = [p.device for p in list_ports.comports()]
    win._rescan_ports()
    shown = [win.cmb_com.itemText(i) for i in range(win.cmb_com.count())]
    if not real:
        assert shown == []                 # 一个口都没有就老老实实空着
