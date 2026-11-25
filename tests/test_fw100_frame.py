"""与固件 App/tests/test_app_data_frame.c 使用同一组字段和 CRC 样例。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ev1_paths import ev1_dirs

for directory in ev1_dirs():
    sys.path.insert(0, str(directory))

import device_frame
from source import RealSource, parse_argv


class FirmwareFrameTest(unittest.TestCase):
    def setUp(self):
        device_frame.configure(layout="fw100", gain=24, vref=4.5, preamp_gain=1000)

    def tearDown(self):
        device_frame.configure(layout="v1")

    @staticmethod
    def frame():
        return device_frame.pack_frame(
            dev=7, seq=0x01020304, ts=0x11223344,
            ts_abs=0x0102030405060708,
            eeg=[-1, 0, 0x123456, -8388608],
            fnirs=[[0, 0] for _ in range(10)],
            ev_t=3, ev_ts=0xA1B2C3D4, flags=2,
        )

    def test_firmware_vector_and_fragmented_log_stream(self):
        raw = self.frame()
        self.assertEqual(len(raw), 100)
        self.assertEqual(raw[-2:], b"\x66\x9a")
        self.assertEqual(raw[21:33], bytes.fromhex("ffffff000000563412000080"))

        buf = bytearray(b"CMD READY\r\n" + raw[:37])
        self.assertEqual(device_frame.parse_stream(buf), [])
        buf += raw[37:] + b"EEG STATUS\r\n" + raw
        frames = device_frame.parse_stream(buf)
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(f.crc_ok and f.header_ok for f in frames))
        self.assertEqual(frames[0].device_id, 7)
        self.assertEqual(frames[0].seq, 0x01020304)
        self.assertEqual(frames[0].timestamp_ms, 0x11223344)
        self.assertEqual(frames[0].timestamp_abs_ms, 0x0102030405060708)
        self.assertEqual(frames[0].eeg_raw, [-1, 0, 0x123456, -8388608])
        self.assertEqual(frames[0].event_type, 3)
        self.assertEqual(frames[0].event_ts, 0xA1B2C3D4)

    def test_bad_crc_is_counted_but_not_sent_to_ui(self):
        source = RealSource(proto="serial")
        good = self.frame()
        bad = bytearray(good)
        bad[21] ^= 1

        class FakeReceiver:
            chunks = [bytes(bad) + good]

            def pop(self):
                return self.chunks.pop(0) if self.chunks else None

        source._rx = FakeReceiver()
        frames = source.poll_frames()
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["seq"], 0x01020304)
        self.assertEqual(source.n_crc_bad, 1)
        self.assertEqual(source.n_frames, 1)

    def test_default_settings_match_firmware(self):
        options = parse_argv([])
        self.assertEqual((options["source"], options["proto"], options["protocol"],
                          options["baud"]), ("real", "serial", "fw100", 921600))

    def test_dark_and_optical_versions_share_eeg_layout(self):
        dark = device_frame.pack_frame(
            dev=1, seq=1, ts=2, eeg=[11, 22, 33, 44],
            fnirs=[[i, -i] for i in range(10)], flags=1)
        optical = bytearray(dark)
        optical[3] = 2
        optical[98:100] = device_frame.crc16_modbus(optical[2:98]).to_bytes(2, "little")
        unknown = bytearray(optical)
        unknown[3] = 3
        unknown[98:100] = device_frame.crc16_modbus(unknown[2:98]).to_bytes(2, "little")
        parsed = device_frame.parse_stream(bytearray(dark + optical + unknown))
        self.assertEqual([f.proto_ver for f in parsed], [1, 2, 3])
        self.assertEqual([f.header_ok for f in parsed], [True, True, False])
        self.assertTrue(all(f.crc_ok and f.eeg_raw == [11, 22, 33, 44] for f in parsed))

        source = RealSource(proto="serial")
        class FakeReceiver:
            chunks = [dark + optical]
            def pop(self):
                return self.chunks.pop(0) if self.chunks else None
        source._rx = FakeReceiver()
        ui_frames = source.poll_frames()
        self.assertEqual([f["fnirs_version"] for f in ui_frames], [1, 2])
        self.assertEqual([f["eeg_n"] for f in ui_frames], [4, 4])
        self.assertEqual([f["raw"][0] for f in ui_frames], [0.0, 0.0])


if __name__ == "__main__":
    unittest.main()
