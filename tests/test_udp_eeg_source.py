"""按当前 ESP8285 报文格式模拟握手、拼包和界面数据入口。"""
import os
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ev1_paths import ev1_dirs

for directory in ev1_dirs():
    sys.path.insert(0, str(directory))

import device_frame
from source import make_source


class FakeSocket:
    def __init__(self):
        self.sent = []

    def sendto(self, data, target):
        self.sent.append((data, target))
        return len(data)

    def close(self):
        pass


def feed_firmware_datagrams(src):
    """测试包 48B，然后 3 个 UDP 包，每包 10 个 100B 帧，其中 1 帧 CRC 错。"""
    rx = src._rx
    rx._sk = FakeSocket()
    rx.running = True
    rx.local_port = 57078
    rx._hello()
    assert rx._sk.sent == [(b"HELLO", rx.target)]
    origin = (rx.target_ip, rx.port + 1)  # 模组回包源端口不一定等于监听端口。
    for seq in range(10):
        packet = struct.pack("<4sIII", b"RY\x01\x01", seq,
                             1000 + seq, 0xA55A3CC3)
        packet += struct.pack("<IIII", 2, 4, 1, 3) + bytes(16)
        rx._handle_datagram(packet, origin)
    assert (b"START", rx.target) in rx._sk.sent
    for batch in range(3):
        frames = []
        for i in range(10):
            seq = batch * 10 + i
            raw = device_frame.pack_frame(
                dev=1, seq=seq, ts=2000 + seq * 2,
                eeg=[10000 + seq, -20000, 30000, -40000],
                fnirs=[[0, 0] for _ in range(10)],
                layout=device_frame.LAYOUT_FW100)
            if seq == 10:
                bad = bytearray(raw)
                bad[21] ^= 1
                raw = bytes(bad)
            frames.append(raw)
        rx._handle_datagram(b"".join(frames), origin)


class UdpEegSourceTest(unittest.TestCase):
    def setUp(self):
        device_frame.configure(layout="fw100", gain=24, vref=4.5, preamp_gain=1000)

    def tearDown(self):
        device_frame.configure(layout="v1")

    def test_handshake_batch_decode_and_crc(self):
        src = make_source("udp", target_ip="127.0.0.1", port=5000,
                          protocol="fw100")
        try:
            feed_firmware_datagrams(src)
            frames = src.poll_frames()
            self.assertEqual(len(frames), 29)
            self.assertEqual([f["seq"] for f in frames],
                             list(range(10)) + list(range(11, 30)))
            self.assertEqual(len(frames[0]["eeg_uv"]), 4)
            self.assertIsNone(frames[0]["raw"])  # bit0=0：fNIRS 填零区不是新采样
            self.assertEqual(frames[0]["fnirs_version"], 1)
            self.assertEqual(src.n_crc_bad, 1)
            self.assertAlmostEqual(src.measured_fps(), 500.0, delta=1.0)
            stats = src.stats()
            self.assertEqual(stats["tests"], 10)
            self.assertEqual(stats["packets"], 3)
            self.assertEqual(stats["mcu_send_fail"], 2)
            self.assertEqual(stats["phase"], "接收 EEG")
            self.assertEqual(stats["local_port"], 57078)
        finally:
            sk = src._rx._sk
            src.stop()
            self.assertIn((b"STOP", ("127.0.0.1", 5000)), sk.sent)

    def test_gui_udp_controls_and_live_labels(self):
        from PyQt5 import QtWidgets
        from main_window import ClassicMainWindow

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = ClassicMainWindow()
        try:
            window._t.stop()
            window._t_link.stop()
            window.mon.timer.stop()
            window.show()
            app.processEvents()
            window.cmb_src.setCurrentIndex(window.cmb_src.findData("udp"))
            app.processEvents()
            self.assertTrue(window.ed_udp_ip.isVisible())
            self.assertTrue(window.cmb_com.isVisible())
            window.cmb_com.setCurrentText("COM17")
            window.ed_udp_ip.setText("127.0.0.1")
            window.ed_udp_port.setText("5000")
            src = make_source("udp", target_ip="127.0.0.1", port=5000,
                              protocol="fw100")
            src.start = lambda: None  # 只替换 socket I/O，解析与界面走真实代码。
            with mock.patch("main_window._ConnectionWorker.start",
                            lambda worker: worker.run()), \
                    mock.patch("receivers.request_eeg_udp_mode") as mode, \
                    mock.patch("source.make_source", return_value=src) as factory:
                window.btn_conn.click()
                window._connection_finished()
                mode.assert_called_once_with("COM17", 921600)
                self.assertEqual(factory.call_args.kwargs["target_ip"], "127.0.0.1")
                self.assertEqual(factory.call_args.kwargs["port"], 5000)
                self.assertEqual(factory.call_args.kwargs["protocol"], "fw100")
            feed_firmware_datagrams(src)
            window.mon._tick()
            window.mon._refresh_status()
            window._refresh_link()
            self.assertEqual(window.mon._recv, 29)
            self.assertGreater(len(window.mon.curves[0].getData()[0]), 0)
            self.assertIn("收包 29", window.mon.lbl.text())
            self.assertIn("测试包 10/10", window.lbl_link.text())
            self.assertIn("解出 29 帧", window.lbl_link.text())
            self.assertIn("CRC错 1", window.lbl_link.text())
            window.mon.view_tabs.setCurrentIndex(1)
            window.mon._tick(force=True)
            self.assertGreater(len(window.mon._overlay_curves[0].getData()[0]), 0)
        finally:
            window._disconnect(silent=True)
            window.close()


if __name__ == "__main__":
    unittest.main()
