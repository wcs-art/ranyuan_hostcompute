"""双纵轴叠加页的切换、波形、手动缩放和外部通道开关。"""
import os
import sys
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ev1_paths import ev1_dirs

for directory in ev1_dirs():
    sys.path.insert(0, str(directory))

import numpy as np
from PyQt5 import QtWidgets
from main_window import ClassicMainWindow


class OverlayViewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.window = ClassicMainWindow()
        self.window._t.stop()
        self.window.show()
        self.app.processEvents()
        m = self.window.mon
        m.timer.stop()
        m.paused = True
        m._recv = m._n_eeg = m._n_fn = 5000
        m._ch_ok = [True] * m.n_total
        m._t_rx = time.time()
        phase = np.linspace(0, 20 * np.pi, 5000)
        for i in range(4):
            m.buf_eeg[i, -5000:] = 30.0 + i * 5 + 4.0 * np.sin(phase)
        for i in range(10):
            m.buf_hbo[i, :5000] = i * 0.01 + 0.08 * np.sin(phase)
            m.buf_hbr[i, :5000] = -i * 0.01 - 0.04 * np.sin(phase)

    def tearDown(self):
        self.window.mon.timer.stop()
        self.window.close()

    def test_tabs_curves_and_shared_time_axis(self):
        m = self.window.mon
        self.assertIs(m._stack.currentWidget(), m._grid_page)
        m.view_tabs.setCurrentIndex(1)
        m._tick(force=True)
        self.assertIs(m._stack.currentWidget(), m._overlay_page)
        self.assertEqual(len(m._overlay_curves), 14)
        self.assertEqual(len(m._overlay_curves[0].getData()[0]), 5000)
        self.assertEqual(len(m._overlay_curves[4].getData()[0]), 5000)
        self.assertIs(m._overlay.getAxis('right').linkedView(), m._overlay_fnirs_vb)
        self.assertEqual(m._overlay.getAxis('left').labelUnits, 'μV')
        self.assertEqual(m._overlay.getAxis('right').labelUnits, 'mM')
        self.app.processEvents()
        np.testing.assert_allclose(m._overlay.getViewBox().viewRange()[0],
                                   m._overlay_fnirs_vb.viewRange()[0], atol=1e-6)
        m.view_tabs.setCurrentIndex(0)
        self.assertIs(m._stack.currentWidget(), m._grid_page)
        self.assertEqual(len(m.curves[0].getData()[0]), 5000)

    def test_manual_time_zoom_survives_tab_switch(self):
        m = self.window.mon
        m.view_tabs.setCurrentIndex(1)
        m._overlay.setXRange(2.0, 4.0, padding=0)
        m._on_manual_zoom(-2)
        m._tick(force=True)
        self.assertFalse(m._follow)
        self.app.processEvents()
        np.testing.assert_allclose(m._overlay.getViewBox().viewRange()[0],
                                   m._overlay_fnirs_vb.viewRange()[0], atol=1e-6)
        m.view_tabs.setCurrentIndex(0)
        np.testing.assert_allclose(m.plots[0].getViewBox().viewRange()[0],
                                   [2.0, 4.0], atol=1e-6)

    def test_fixed_vertical_ranges_and_zoom_button(self):
        m = self.window.mon
        m.view_tabs.setCurrentIndex(1)
        initial_left = tuple(m._overlay.getViewBox().viewRange()[1])
        initial_right = tuple(m._overlay_fnirs_vb.viewRange()[1])
        m._tick(force=True)
        self.assertEqual(tuple(m._overlay.getViewBox().viewRange()[1]), initial_left)
        self.assertEqual(tuple(m._overlay_fnirs_vb.viewRange()[1]), initial_right)
        m.btn_overlay_zoom.click()
        fitted_left = tuple(m._overlay.getViewBox().viewRange()[1])
        fitted_right = tuple(m._overlay_fnirs_vb.viewRange()[1])
        self.assertNotEqual(fitted_left, initial_left)
        self.assertNotEqual(fitted_right, initial_right)
        m._tick(force=True)
        self.assertEqual(tuple(m._overlay.getViewBox().viewRange()[1]), fitted_left)
        self.assertEqual(tuple(m._overlay_fnirs_vb.viewRange()[1]), fitted_right)
        m.btn_overlay_reset.click()
        self.assertEqual(tuple(m._overlay.getViewBox().viewRange()[1]), initial_left)
        self.assertEqual(tuple(m._overlay_fnirs_vb.viewRange()[1]), initial_right)

    def test_external_checkbox_and_fnirs_metric(self):
        m = self.window.mon
        m.view_tabs.setCurrentIndex(1)
        self.window._chks[0].setChecked(False)
        self.assertFalse(m._overlay_curves[0].isVisible())
        self.assertFalse(m.plots[0].isVisible())
        self.window._chks[0].setChecked(True)
        self.assertTrue(m._overlay_curves[0].isVisible())
        hbo = m._overlay_curves[4].getData()[1].copy()
        self.window.cmb_fnirs.setCurrentIndex(1)
        hbr = m._overlay_curves[4].getData()[1]
        self.assertFalse(np.array_equal(hbo, hbr))


if __name__ == "__main__":
    unittest.main()
