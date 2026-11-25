"""串口模式切换命令的顺序和确认，无需连接真实设备。"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ev1_paths import ev1_dirs

for directory in ev1_dirs():
    sys.path.insert(0, str(directory))

from receivers import LinkError, SerialReceiver, request_eeg_udp_mode


class FakeSerial:
    def __init__(self, replies=None):
        self.writes = []
        self.pending = b""
        self.closed = False
        self.replies = replies or {}

    @property
    def in_waiting(self):
        return len(self.pending)

    def reset_input_buffer(self):
        self.pending = b""

    def write(self, data):
        self.writes.append(data)
        default = {
            b"EEG OUTPUT UART\r\n": b"OK EEG OUTPUT UART result=0\r\n",
            b"EEG UART ON\r\n": b"OK EEG UART ON\r\n",
            b"EEG OUTPUT UDP\r\n": b"OK EEG OUTPUT UDP result=0\r\n",
        }
        response = self.replies.get(data, default.get(data, b""))
        self.pending = response.pop(0) if isinstance(response, list) else response
        return len(data)

    def flush(self):
        pass

    def read(self, size):
        chunk, self.pending = self.pending[:size], self.pending[size:]
        return chunk

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ModeCommandTest(unittest.TestCase):
    def test_uart_mode_uses_receiver_port_before_reading(self):
        port = FakeSerial()
        with mock.patch("serial.Serial", return_value=port) as factory:
            opened = SerialReceiver(com="COM17", baud=921600)._open()
        self.assertIs(opened, port)
        factory.assert_called_once_with("COM17", 921600, timeout=0.2, write_timeout=2)
        self.assertEqual(port.writes,
                         [b"EEG OUTPUT UART\r\n", b"EEG UART ON\r\n"])
        self.assertFalse(port.closed)

    def test_udp_mode_releases_control_port(self):
        port = FakeSerial()
        with mock.patch("serial.Serial", return_value=port):
            request_eeg_udp_mode("COM17", 921600)
        self.assertEqual(port.writes, [b"EEG OUTPUT UDP\r\n"])
        self.assertTrue(port.closed)

    def test_uart_charge_lock_is_cleared_before_retry(self):
        port = FakeSerial({
            b"EEG UART ON\r\n": [
                b"ERR EEG UART blocked by charge lock; send CHARGE OFF\r\n",
                b"OK EEG UART ON\r\n"],
            b"CHARGE OFF\r\n": b"OK CHARGE OFF result=0\r\n",
        })
        with mock.patch("serial.Serial", return_value=port):
            SerialReceiver(com="COM17", baud=921600)._open()
        self.assertEqual(port.writes, [b"EEG OUTPUT UART\r\n", b"EEG UART ON\r\n",
                                       b"CHARGE OFF\r\n", b"EEG UART ON\r\n"])

    def test_udp_charge_lock_is_cleared_before_retry(self):
        port = FakeSerial({
            b"EEG OUTPUT UDP\r\n": [b"ERR EEG OUTPUT UDP result=6\r\n",
                                        b"OK EEG OUTPUT UDP result=0\r\n"],
            b"CHARGE OFF\r\n": b"OK CHARGE OFF result=0\r\n",
        })
        with mock.patch("serial.Serial", return_value=port):
            request_eeg_udp_mode("COM17", 921600)
        self.assertEqual(port.writes, [b"EEG OUTPUT UDP\r\n", b"CHARGE OFF\r\n",
                                       b"EEG OUTPUT UDP\r\n"])
        self.assertTrue(port.closed)

    def test_pending_charge_off_is_reported_as_failure(self):
        port = FakeSerial({
            b"EEG UART ON\r\n": b"ERR EEG UART blocked by charge lock; send CHARGE OFF\r\n",
            b"CHARGE OFF\r\n": b"OK CHARGE OFF result=8\r\n",
        })
        with mock.patch("serial.Serial", return_value=port):
            with self.assertRaisesRegex(LinkError, "未实际生效"):
                SerialReceiver(com="COM17", baud=921600)._open()
        self.assertTrue(port.closed)


if __name__ == "__main__":
    unittest.main()
