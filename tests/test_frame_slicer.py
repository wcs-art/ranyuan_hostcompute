"""FrameSlicer 切帧门禁: 只验证"字节流 -> 完整帧字节块", 不涉及字段解析。

期望值来自协议约定(定长帧 / 固定帧头), 不用被测实现反推。
"""
from __future__ import annotations

import random

from frame_slicer import FrameSlicer
from device_frame import FRAME_HEAD, pack_frame

DEV = 1
EEG = [1000, -2000, 3000, -4000]
FNIRS = [[100 + c * 10, 200 + c * 10] for c in range(10)]

# 帧长(合同 F2 的 v1 布局): 帧头2 + 头部7 + seq4 + ts_mono4 + ts_abs8
#                          + 4*3 + 10*2*3 + (ev_t1+ev_ts4) + CRC2 = 104
FRAME_LEN = 2 + 7 + 4 + 4 + 8 + 4 * 3 + 10 * 2 * 3 + (1 + 4) + 2
N_FRAMES = 600


def _stream(n: int) -> bytes:
    return b"".join(pack_frame(DEV, seq, seq * 10, EEG, FNIRS) for seq in range(n))


def _slicer() -> FrameSlicer:
    return FrameSlicer(FRAME_LEN, FRAME_HEAD)


def _seqs(frames: list[bytes]) -> list[int]:
    # seq 在帧内绝对偏移 9..13 (小端); v1 帧头前面多了 ver/通道数/ratio/flags
    return [int.from_bytes(f[9:13], "little") for f in frames]


def test_frame_len_is_104():
    assert FRAME_LEN == 104
    assert len(pack_frame(DEV, 0, 0, EEG, FNIRS)) == FRAME_LEN


def test_aligned_blocks_600_frames():
    data = _stream(N_FRAMES)
    assert len(data) == N_FRAMES * FRAME_LEN
    s = _slicer()
    out: list[bytes] = []
    for i in range(0, len(data), 9000):
        out += s.feed(data[i:i + 9000])
    assert len(out) == N_FRAMES
    assert _seqs(out) == list(range(N_FRAMES))
    assert s.pending == b""
    assert s.resync_count == 0


def test_single_byte_feed_600_frames():
    data = _stream(N_FRAMES)
    s = _slicer()
    out: list[bytes] = []
    for i in range(len(data)):
        out += s.feed(data[i:i + 1])
    assert len(out) == N_FRAMES
    assert _seqs(out) == list(range(N_FRAMES))
    assert s.pending == b""


def test_random_chunks_600_frames():
    data = _stream(N_FRAMES)
    rng = random.Random(20261112)
    s = _slicer()
    out: list[bytes] = []
    i = 0
    while i < len(data):
        n = rng.randint(1, 2048)
        out += s.feed(data[i:i + n])
        i += n
    assert len(out) == N_FRAMES
    assert _seqs(out) == list(range(N_FRAMES))
    assert s.pending == b""


def test_garbage_before_head_resync_observable():
    garbage = b"\x00\x11\x22"
    data = garbage + _stream(3) + garbage + _stream(2)
    s = _slicer()
    out = s.feed(data)
    assert len(out) == 5
    assert _seqs(out) == [0, 1, 2, 0, 1]
    assert s.resync_count > 0
    assert s.resync_bytes >= len(garbage)


def test_half_frame_tail_then_completion():
    full = pack_frame(DEV, 42, 999, EEG, FNIRS)
    s = _slicer()
    assert s.feed(full[:40]) == []
    assert len(s.pending) == 40, "半个帧必须留在内部缓冲"
    assert s.resync_count == 0, "半个帧不算垃圾"
    out = s.feed(full[40:])
    assert len(out) == 1
    assert out[0] == full
    assert s.pending == b""


def test_split_head_bytes_retained():
    full = pack_frame(DEV, 7, 70, EEG, FNIRS)
    s = _slicer()
    assert s.feed(full[:1]) == []       # 只到 0xAA
    assert s.pending == b"\xAA"
    out = s.feed(full[1:])
    assert len(out) == 1
    assert out[0] == full
