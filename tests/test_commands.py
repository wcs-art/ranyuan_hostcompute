"""下行命令编解码门禁：字节格式一旦冻结，固件与上位机都按它实现。

期望值手写在断言里（不调用被测实现反推），这样"改命令格式"必须显式改测试。
"""
from __future__ import annotations

import struct

import pytest

from commands import (
    CMD_HEAD, RSP_HEAD, Cmd, Status, crc16_modbus, decode_response, encode,
    led_force, reg_read, reg_write, response_len, set_config, set_time,
    soft_marker, start_stop, sync_off, sync_out,
    test_signal as cmd_test_signal,
)

DEV = 1


# 1 -----------------------------------------------------------------------
def test_frame_shape():
    """帧头 AA 56 + dev + cmd + len + payload + crc16(小端)。"""
    raw = set_time(DEV, 1_763_000_000_123)
    assert raw[:2] == CMD_HEAD == b"\xAA\x56"
    assert raw[2] == DEV
    assert raw[3] == Cmd.SET_TIME
    assert raw[4] == 8, "len 字段必须等于 payload 长度"
    assert len(raw) == 3 + 2 + 8 + 2
    assert struct.unpack("<Q", raw[5:13])[0] == 1_763_000_000_123
    assert struct.unpack("<H", raw[-2:])[0] == crc16_modbus(raw[2:-2])


# 2 -----------------------------------------------------------------------
def test_crc_covers_body_not_head():
    """CRC 只覆盖 dev..payload。把帧头算进去会导致固件侧对不上。"""
    raw = bytearray(set_time(DEV, 0))
    raw[2] ^= 0xFF                                  # 改 dev
    body = bytes(raw[2:-2])
    assert struct.unpack("<H", raw[-2:])[0] != crc16_modbus(body)


# 3 -----------------------------------------------------------------------
def test_sync_out_codes():
    """合同 F8 只要求 0.5/1/2 Hz 三档，其余值必须显式拒绝而不是静默取整。"""
    assert sync_out(DEV, 0.5)[5] == 1
    assert sync_out(DEV, 1.0)[5] == 2
    assert sync_out(DEV, 2.0)[5] == 3
    assert sync_off(DEV)[5] == 0
    with pytest.raises(ValueError):
        sync_out(DEV, 3.0)


# 4 -----------------------------------------------------------------------
def test_soft_marker_rejects_zero():
    """事件类型 0 在协议里表示"无事件"，不能拿来做软标记。"""
    assert soft_marker(DEV, 7)[5] == 7
    with pytest.raises(ValueError):
        soft_marker(DEV, 0)


# 5 -----------------------------------------------------------------------
def test_register_commands():
    assert reg_write(DEV, 0, 0x01, 0x96)[5:8] == bytes([0, 0x01, 0x96])
    assert reg_read(DEV, 3, 0x2A)[5:7] == bytes([3, 0x2A])
    with pytest.raises(ValueError):
        reg_write(DEV, 0, 0x01, 256)


# 6 -----------------------------------------------------------------------
def test_config_and_misc():
    raw = set_config(DEV, 500, led_code=3, ch_mask=0x0F)
    assert struct.unpack("<H", raw[5:7])[0] == 500
    assert raw[7:9] == bytes([3, 0x0F])
    assert start_stop(DEV, True)[5] == 1 and start_stop(DEV, False)[5] == 0
    assert led_force(DEV, 2)[5:7] == bytes([2, 1])
    assert cmd_test_signal(DEV, True)[5] == 1


# 7 -----------------------------------------------------------------------
def test_encode_limits():
    assert len(encode(DEV, Cmd.START_STOP, b"")) == 7
    with pytest.raises(ValueError):
        encode(DEV, Cmd.START_STOP, b"\x00" * 256)
    with pytest.raises(ValueError):
        encode(256, Cmd.START_STOP)


# 8 -----------------------------------------------------------------------
def _rsp(dev: int, cmd: int, status: int, payload: bytes = b"") -> bytes:
    body = bytes([dev, cmd, status, len(payload)]) + payload
    return RSP_HEAD + body + struct.pack("<H", crc16_modbus(body))


def test_decode_response_ok():
    r = decode_response(_rsp(DEV, Cmd.REG_READ, Status.OK, b"\x96"))
    assert r is not None and r.ok and r.payload == b"\x96"


def test_decode_response_bad_crc():
    raw = bytearray(_rsp(DEV, Cmd.REG_WRITE, Status.OK))
    raw[3] ^= 0xFF
    r = decode_response(bytes(raw))
    assert r is not None and r.ok is False and "CRC" in r.reason()


def test_decode_response_rejects_bad_frames():
    assert decode_response(b"\xAA\x55\x01\x01\x00\x00\x00") is None, "上行帧头不算应答"
    assert decode_response(_rsp(DEV, 1, 0)[:-1]) is None, "长度不足半帧应返回 None"


def test_response_len_allows_half_frame_wait():
    raw = _rsp(DEV, Cmd.REG_READ, Status.OK, b"\x01\x02\x03")
    assert response_len(raw[:6]) == len(raw)
    assert response_len(b"\x00\x00") is None
