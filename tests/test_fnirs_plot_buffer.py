"""fNIRS 绘图缓存应覆盖完整时间窗，并在环形回绕后保持顺序。"""
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
from monitor_14ch import Monitor14


class FnirsPlotBufferTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.monitor = Monitor14()
        self.monitor.timer.stop()

    def tearDown(self):
        self.monitor.close()

    def test_full_window_and_wrap_order(self):
        m = self.monitor
        self.assertEqual(m.buf_hbo.shape[1], 15000)
        self.assertEqual(m.buf_hbr.shape[1], 15000)
        self.assertEqual(m.fs_fnirs, 500.0)

        sample_ids = np.arange(5000, 20000)
        m.buf_hbo[0, sample_ids % 15000] = sample_ids
        x, segment = m._take(m.buf_hbo, 14000, 5000, 500.0, 20000)
        self.assertEqual(len(x), 5000)
        np.testing.assert_array_equal(segment[0], np.arange(14000, 19000))
        self.assertAlmostEqual(x[-1] - x[0], 9.998)

    def test_zero_placeholder_spans_ten_second_plot(self):
        m = self.monitor
        m._recv = m._n_eeg = m._n_fn = 45000
        m._ch_ok = [True] * m.n_total
        m._t_rx = time.time()
        m._tick(force=True)
        eeg_x = m.curves[0].getData()[0]
        fnirs_x = m.curves[4].getData()[0]
        self.assertEqual(len(eeg_x), 5000)
        self.assertEqual(len(fnirs_x), 5000)
        np.testing.assert_array_equal(fnirs_x, eeg_x)

    def test_each_frame_writes_next_ring_column(self):
        m = self.monitor
        for i in range(3):
            m.push_frame(seq=i, eeg_uv=[0.0] * 4,
                         hbo=[float(i)] * 10, hbr=[float(-i)] * 10,
                         raw=None, t_dev_ms=i * 2)
        x, hbo = m._take(m.buf_hbo, 0, 3, 500.0, 3)
        _, hbr = m._take(m.buf_hbr, 0, 3, 500.0, 3)
        np.testing.assert_array_equal(hbo[0], [0.0, 1.0, 2.0])
        np.testing.assert_array_equal(hbr[0], [0.0, -1.0, -2.0])
        self.assertEqual(len(x), 3)

    def test_firmware_dark_and_optical_raw_waveforms(self):
        m = self.monitor
        dark = [float(i) for i in range(20)]
        m.push_frame(seq=1, eeg_uv=[1.0] * 4, hbo=[0.0] * 10,
                     hbr=[0.0] * 10, raw=dark, fnirs_version=1, fnirs_new=True)
        self.assertEqual(m.fnirs_version, 1)
        self.assertEqual(m.fnirs_unit, "ADC码")
        self.assertTrue(all(m._ch_ok[4:10]))
        self.assertFalse(any(m._ch_ok[10:]))
        self.assertEqual(m.buf_hbo[0, 0], 0.0)
        self.assertEqual(m.buf_hbr[0, 0], 1.0)
        m.push_frame(seq=2, eeg_uv=[2.0] * 4, hbo=[0.0] * 10,
                     hbr=[0.0] * 10, raw=None, fnirs_version=1, fnirs_new=False)
        self.assertEqual(m.buf_hbr[0, 1], 1.0)  # 无新光学样本时保持最近一次读数
        self.assertEqual(m.buf_eeg[0, -1], 2.0)
        m.push_frame(seq=3, eeg_uv=[3.0] * 4, hbo=[0.0] * 10,
                     hbr=[0.0] * 10, raw=[float(i+100) for i in range(20)],
                     fnirs_version=2, fnirs_new=True)
        self.assertEqual(m.fnirs_version, 2)
        self.assertTrue(all(m._ch_ok[4:]))
        self.assertEqual(m.buf_hbo[9, 2], 118.0)
        self.assertEqual(m.buf_hbr[9, 2], 119.0)
        m._tick(force=True)
        self.assertEqual(m.curves[13].getData()[1][-1], 118.0)
        m._set_fnirs_metric(1)
        self.assertEqual(m.curves[13].getData()[1][-1], 119.0)


if __name__ == "__main__":
    unittest.main()
