"""文本行数据源（有线 ASCII 输出）的解析与回放回归。

这些用例锁住三件事：
  1. 真实固件字节（2026-11-17 COM3 @921600 抓取）能被正确解析成采样值；
  2. 串口读边界把一行切成两半时，不能丢帧也不能错位；
  3. 解析失败的行必须被计数，不允许静默吞掉。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ev1"))

# 界面集成用例必须离屏跑，否则 CI 上会因为没有显示设备而失败
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from text_source import TextDecoder, TextFileSource, parse_line  # noqa: E402

#: 现场实测原始字节（十六进制）："-12441.80\r\n-12809.36\r\n-11766.48\r\n"
FIELD_HEX = "2d 31 32 34 34 31 2e 38 30 0d 0a 2d 31 32 38 30 39 2e 33 36 0d 0a 2d 31 31 37 36 36 2e 34 38 0d 0a"
FIELD_BYTES = bytes.fromhex(FIELD_HEX)


def test_real_field_bytes_parse():
    """现场抓到的字节必须解出 3 个采样值。"""
    assert FIELD_BYTES == b"-12441.80\r\n-12809.36\r\n-11766.48\r\n"
    d = TextDecoder()
    frames = d.feed(FIELD_BYTES)
    assert len(frames) == 3
    assert [f["eeg_uv"][0] for f in frames] == [-12441.80, -12809.36, -11766.48]
    # 单列数据只填第一个 EEG 通道，其余补 0（不猜）
    assert frames[0]["eeg_uv"] == [-12441.80, 0.0, 0.0, 0.0]
    assert d.stats()["cols"] == 1


def test_line_split_across_reads():
    """串口读边界可能落在一行中间：逐字节喂也必须是 3 帧、值不变。"""
    d = TextDecoder()
    got = []
    for b in FIELD_BYTES:                  # 最恶劣的切法：一次一个字节
        got += d.feed(bytes([b]))
    assert len(got) == 3
    assert got[2]["eeg_uv"][0] == -11766.48


def test_parse_line_formats():
    assert parse_line(b"-12441.80\r") == [-12441.80]
    assert parse_line(b"1.0,2.0,3.0") == [1.0, 2.0, 3.0]
    # FireWater 命名格式：取冒号后的数值
    assert parse_line(b"ch0:-1.5,ch1:2.5") == [-1.5, 2.5]
    assert parse_line(b"  -3.25  ") == [-3.25]


def test_parse_line_rejects_garbage():
    for bad in (b"", b"   ", b"abc", b"1.0,,2.0", b"nan", b"inf", b"-inf",
                b"\xff\xfe\x00garbage", b"1.0 2.0"):
        assert parse_line(bad) is None, bad


def test_bad_lines_counted_not_swallowed():
    d = TextDecoder()
    # 空行不算坏行（串口常有多余换行），非空坏行必须计数
    d.feed(b"1.0\r\nbad\r\n2.0\r\n\r\n3.0\r\n")
    s = d.stats()
    assert s["frames"] == 3
    assert s["bad_lines"] == 1


def test_24_columns_map_to_eeg_and_fnirs():
    """24 列 = 4 EEG + 20 路光强码值，映射必须对上（将来全通道时走这条）。"""
    d = TextDecoder()
    line = ",".join(str(v) for v in range(1, 25)).encode() + b"\r\n"
    f = d.feed(line)[0]
    assert f["eeg_uv"] == [1.0, 2.0, 3.0, 4.0]
    assert f["raw"] is not None and len(f["raw"]) == 20
    assert f["raw"][:3] == [5.0, 6.0, 7.0]


def test_short_and_extra_columns_visible():
    """列数不足或超出映射范围时，多退少补要在统计里看得见。"""
    d = TextDecoder()
    d.feed(b"1.0\r\n")                     # 1 列 < 4 通道 -> short
    d.feed((",".join(["1"] * 10)).encode() + b"\r\n")   # 10 列：4 EEG + 6 丢弃
    s = d.stats()
    assert s["short_lines"] == 1
    assert s["extra_cols_dropped"] == 6


def test_seq_is_monotonic_local_counter():
    """seq 是上位机按行计数，不来自固件：必须连续、不跳号。"""
    d = TextDecoder()
    d.feed(b"1\r\n2\r\n3\r\n4\r\n")
    d.feed(b"5\r\n")
    assert d.seq == 5


def test_text_file_source_replay(tmp_path):
    p = tmp_path / "capture.txt"
    p.write_bytes(FIELD_BYTES * 3)
    src = TextFileSource(p)
    got = []
    for _ in range(50):
        got += src.poll_frames()
        if src.exhausted():
            break
    assert len(got) == 9                   # 3 行 × 3 份
    assert src.stats()["frames"] == 9
    assert src.stats()["bad_lines"] == 0


# --------------------------------------------------------------------------
# 界面集成：帧的键名/形状必须真的能喂进 Monitor14.push_frame
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def app():
    QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_frames_reach_monitor(app, tmp_path):
    """文本帧落到界面缓冲区：EEG1 有值，fNIRS 不被污染（raw 为 None 时必须跳过 MBLL）。"""
    pytest.importorskip("pyqtgraph")
    import monitor_14ch

    p = tmp_path / "c.txt"
    p.write_bytes(FIELD_BYTES)
    src = TextFileSource(p)
    w = monitor_14ch.Monitor14()
    try:
        for fr in src.poll_frames():
            w.push_frame(**fr)             # 键名不匹配会直接 TypeError
        assert w.buf_eeg[0, -1] == pytest.approx(-11766.48)   # 最后一行
        assert list(w.buf_eeg[1:, -1]) == [0.0, 0.0, 0.0]     # 单列不冒充其它通道
        assert list(w.buf_fn[:, -1]) == [0.0] * 10            # 没光强就不算 ΔHbO
        assert w.fnirs_state().startswith("基线")             # 如实报"没算"
    finally:
        w.close()


def test_tick_consumes_receiver(app, tmp_path):
    """attach_receiver + _tick 是界面唯一数据入口，这条真实路径必须通。"""
    pytest.importorskip("pyqtgraph")
    import monitor_14ch

    p = tmp_path / "c.txt"
    p.write_bytes(FIELD_BYTES)
    src = TextFileSource(p)
    w = monitor_14ch.Monitor14()
    try:
        w.attach_receiver(src)
        w._tick()
        assert w._recv > 0
        assert w.buf_eeg[0, -1] == pytest.approx(-11766.48)
    finally:
        w.close()
