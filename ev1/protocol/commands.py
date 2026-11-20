"""上位机 -> 下位机 下行命令编码（合同 F2 对时 / F4 工程测试 / F5 配置 / F8 同步触发）。

为什么先有编码、没有发送
    传输层（receivers.py）的四个链路都只有收，没有发；而固件侧的接收逻辑也还没确认。
    command 帧格式一旦冻结，固件才能写解析、上位机才能挂到链路上。
    所以这里先把**字节格式**用代码钉死（含单测），等固件对上以后再补 send_command()。
    合同 F2 原文："上位机下发的对时/同步命令格式须在协议文档中定义" —— 本文件就是那个定义。

命令帧（上位机 -> 下位机）

    AA 56 | dev(1) | cmd(1) | len(1) | payload(len) | crc16(2, 小端)

    帧头 2 字节是 AA 56（与上行数据的 AA 55 区分开，避免串线时互相误判）。
    CRC16/MODBUS 覆盖 dev..payload（不含帧头、不含 CRC 自身）。

应答帧（下位机 -> 上位机）

    AA 57 | dev(1) | cmd(1) | status(1) | len(1) | payload(len) | crc16(2)

    status: 0=成功, 1=未知命令, 2=参数错, 3=忙(上一命令未完成)
    读寄存器时 payload 返回读到的值。

命令表

    0x01 SET_TIME     u64 Unix 毫秒                对时：设备据此填帧内 ts_abs_ms
    0x02 SYNC_OUT     1B 周期码 0=关 1=0.5Hz 2=1Hz 3=2Hz
    0x03 SOFT_MARKER  1B 事件类型(1..255)          软标记，等价于按下设备事件键
    0x04 REG_WRITE    1B 芯片 1B 寄存器 1B 值      芯片 0=ADS1299, 1..8=AFE4404
    0x05 REG_READ     1B 芯片 1B 寄存器
    0x06 SET_CONFIG   2B EEG 采样率(Hz) 1B LED 电流码 1B 通道使能位图
    0x07 START_STOP   1B 1=开始采集 0=停止
    0x08 LED_FORCE    1B 通道 1B 1=强制点亮 0=恢复   F4 单 LED 强制点亮
    0x09 TEST_SIGNAL  1B 1=打开 ADS1299 内部方波 0=关闭
"""
from __future__ import annotations
from typing import Optional, Union

import struct
from dataclasses import dataclass, field
from enum import IntEnum

CMD_HEAD = b"\xAA\x56"
RSP_HEAD = b"\xAA\x57"

CRC16_CAP = 0xFFFF


class Cmd(IntEnum):
    SET_TIME = 0x01
    SYNC_OUT = 0x02
    SOFT_MARKER = 0x03
    REG_WRITE = 0x04
    REG_READ = 0x05
    SET_CONFIG = 0x06
    START_STOP = 0x07
    LED_FORCE = 0x08
    TEST_SIGNAL = 0x09


class Status(IntEnum):
    OK = 0
    UNKNOWN_CMD = 1
    BAD_ARG = 2
    BUSY = 3


#: SYNC_OUT 周期码（合同 F8 只要求 0.5/1/2 Hz 三档）
SYNC_HZ_CODE = {0.5: 1, 1.0: 2, 2.0: 3}
SYNC_CODE_HZ = {v: k for k, v in SYNC_HZ_CODE.items()}

#: 寄存器读写用的芯片编号
CHIP_ADS1299 = 0
CHIP_AFE_BASE = 1          # 1..8 = AFE4404 #1..#8


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def encode(dev: int, cmd: "Union[Cmd, int]", payload: bytes = b"") -> bytes:
    """组一条下行命令帧。payload 上限 255 字节（len 字段只有 1 字节）。"""
    if not 0 <= int(dev) <= 255:
        raise ValueError(f"device_id 必须在 0..255, 收到 {dev!r}")
    if len(payload) > 255:
        raise ValueError(f"payload 最长 255 字节, 现在 {len(payload)}")
    body = bytes([int(dev), int(cmd) & 0xFF, len(payload)]) + bytes(payload)
    return CMD_HEAD + body + struct.pack("<H", crc16_modbus(body))


# --------------------------------------------------------------------------
# 各命令的便捷构造
# --------------------------------------------------------------------------
def set_time(dev: int, unix_ms: int) -> bytes:
    """对时。设备收到后把 ts_abs_ms 填成 unix_ms + 之后的上电毫秒增量。"""
    if not 0 <= int(unix_ms) < (1 << 64):
        raise ValueError(f"unix_ms 越界: {unix_ms!r}")
    return encode(dev, Cmd.SET_TIME, struct.pack("<Q", int(unix_ms)))


def sync_out(dev: int, hz: float) -> bytes:
    code = SYNC_HZ_CODE.get(float(hz))
    if code is None:
        raise ValueError(f"SYNC OUT 只支持 0.5/1/2 Hz, 收到 {hz!r}")
    return encode(dev, Cmd.SYNC_OUT, bytes([code]))


def sync_off(dev: int) -> bytes:
    return encode(dev, Cmd.SYNC_OUT, bytes([0]))


def soft_marker(dev: int, event_type: int = 3) -> bytes:
    """软标记命令：载荷 1 字节事件类型（协议 §2.2 / §6.3）。

    默认值必须是 **3 = EV_SOFT（上位机软件标记）**。原先写 1，那是
    EV_SYNC_IN（外部同步输入）的编码——调用方只要漏传，固件就会把上位机
    软标记当成"外部同步输入"写进帧内 event_type，事后无法区分事件来源。
    三种事件（SYNC_IN=1 / EXT_TRIG=2 / SOFT=3 / SYNC_OUT=4）必须能分开，
    否则合同 F2「三类事件统一经该字段进入数据流」就只剩"有个事件"。
    """
    if not 1 <= int(event_type) <= 255:
        raise ValueError(f"事件类型必须 1..255（0 表示无事件）, 收到 {event_type!r}")
    return encode(dev, Cmd.SOFT_MARKER, bytes([int(event_type)]))


def reg_write(dev: int, chip: int, reg: int, value: int) -> bytes:
    for name, v in (("chip", chip), ("reg", reg), ("value", value)):
        if not 0 <= int(v) <= 255:
            raise ValueError(f"{name} 必须在 0..255, 收到 {v!r}")
    return encode(dev, Cmd.REG_WRITE, bytes([int(chip), int(reg), int(value)]))


def reg_read(dev: int, chip: int, reg: int) -> bytes:
    return encode(dev, Cmd.REG_READ, bytes([int(chip) & 0xFF, int(reg) & 0xFF]))


def set_config(dev: int, eeg_hz: int, led_code: int = 0, ch_mask: int = 0x0F) -> bytes:
    """采样率 / LED 电流 / 通道使能（合同 F5：可通过上位机或配置文件设置）。"""
    if not 0 < int(eeg_hz) <= 65535:
        raise ValueError(f"EEG 采样率越界: {eeg_hz!r}")
    return encode(dev, Cmd.SET_CONFIG,
                  struct.pack("<H", int(eeg_hz)) + bytes([int(led_code) & 0xFF,
                                                          int(ch_mask) & 0xFF]))


def start_stop(dev: int, on: bool = True) -> bytes:
    return encode(dev, Cmd.START_STOP, bytes([1 if on else 0]))


def led_force(dev: int, channel: int, on: bool = True) -> bytes:
    return encode(dev, Cmd.LED_FORCE, bytes([int(channel) & 0xFF, 1 if on else 0]))


def test_signal(dev: int, on: bool = True) -> bytes:
    return encode(dev, Cmd.TEST_SIGNAL, bytes([1 if on else 0]))


# --------------------------------------------------------------------------
# 应答解析
# --------------------------------------------------------------------------
@dataclass
class Response:
    device_id: int
    cmd: int
    status: int
    payload: bytes = b""
    crc_ok: bool = False
    raw: bytes = field(default=b"", repr=False)

    @property
    def ok(self) -> bool:
        return self.crc_ok and self.status == Status.OK

    def reason(self) -> str:
        if not self.crc_ok:
            return "应答 CRC 错"
        if self.status in Status._value2member_map_:
            return Status(self.status).name
        return f"未知状态码 {self.status}"


def decode_response(raw: bytes) -> "Optional[Response]":
    """解析一帧应答。长度不符或帧头不对返回 None，由调用方决定是重同步还是报错。"""
    raw = bytes(raw)
    if len(raw) < 8 or raw[:2] != RSP_HEAD:
        return None
    n = raw[5]
    if len(raw) != 8 + n:
        return None
    body = raw[2:-2]
    crc_rx = struct.unpack("<H", raw[-2:])[0]
    return Response(
        device_id=raw[2], cmd=raw[3], status=raw[4],
        payload=raw[6:6 + n], crc_ok=(crc_rx == crc16_modbus(body)), raw=raw,
    )


def response_len(raw: bytes) -> "Optional[int]":
    """只读帧头与长度字段，告诉调用方"这帧总共几个字节"，用于半帧等待。"""
    if len(raw) < 6 or raw[:2] != RSP_HEAD:
        return None
    return 8 + raw[5]
