"""B3 下行发送通路测试.

期望值来源标记:
  [规格] = 任务书 B3 判据 / 协议约定(人填)
  [现状] = 当前实现事实(回归用, 不代表"应该如此")
"""
from __future__ import annotations

import socket
import struct
import threading

import pytest

from receivers import CMD_SOFT_MARK, build_cmd
import tx


class FakeLeg:
    """内存假传输: 记录字节, 可按需抛异常."""

    def __init__(self, channel, exc=None):
        self.channel = channel
        self.exc = exc
        self.sent = []

    def write(self, data):
        if self.exc is not None:
            raise self.exc
        self.sent.append(bytes(data))
        return len(data)


def _sender(udp=None, tcp=None, serial=None):
    return tx.Sender(udp=udp, tcp=tcp, serial_transport=serial)


# ---------- 帧格式 ----------

def test_frame_bytes_equal_build_cmd():
    """[规格] 下行字节必须与 receivers.build_cmd 逐字节相等."""
    payload = b"\x01\x02\x03"
    leg = FakeLeg(tx.UDP)
    r = _sender(udp=leg).send(CMD_SOFT_MARK, payload)
    expected = build_cmd(CMD_SOFT_MARK, payload)
    assert r.ok and r.channel == tx.UDP and not r.fallback
    assert leg.sent == [expected]
    # 协议 §6.1: AA56(2) + dev(1) + cmd(1) + len(1) + payload(3) + CRC(2)
    assert r.nbytes == len(expected) == 2 + 1 + 1 + 1 + 3 + 2
    assert expected[:2] == b"\xAA\x56"
    assert r.reason == ""


def test_empty_payload_frame_len():
    """[规格] 空载荷帧长 = 帧头2 + dev1 + cmd1 + len1 + CRC2 = 7."""
    leg = FakeLeg(tx.UDP)
    r = _sender(udp=leg).send(CMD_SOFT_MARK)
    assert r.nbytes == 7 and leg.sent[0] == build_cmd(CMD_SOFT_MARK, b"")


def test_ts_payload_is_le32_with_wrap():
    """[规格] 32 位时基小端, 超 32 位按回绕截断."""
    assert tx.ts_payload(0x12345678) == b"\x78\x56\x34\x12"
    assert tx.ts_payload(0x1_0000_0005) == struct.pack("<I", 5)


# ---------- 降级链 ----------

def test_udp_fail_degrades_to_tcp_with_reason():
    """[规格] UDP 抛错 -> TCP 成功, 且 UDP 原因必须记录."""
    udp = FakeLeg(tx.UDP, exc=OSError("udp down"))
    tcp = FakeLeg(tx.TCP)
    r = _sender(udp=udp, tcp=tcp).send(CMD_SOFT_MARK, b"\x07")
    assert r.ok and r.channel == tx.TCP and r.fallback
    assert "udp down" in r.reason and "udp" in r.reason
    assert [a.channel for a in r.attempts] == [tx.UDP, tx.TCP]
    assert [a.ok for a in r.attempts] == [False, True]


def test_udp_tcp_fail_degrades_to_serial():
    """[规格] UDP+TCP 均失败 -> 串口成功, 两条原因都在."""
    udp = FakeLeg(tx.UDP, exc=OSError("udp refused"))
    tcp = FakeLeg(tx.TCP, exc=ConnectionRefusedError("tcp refused"))
    ser = FakeLeg(tx.SERIAL)
    r = _sender(udp=udp, tcp=tcp, serial=ser).send(CMD_SOFT_MARK)
    assert r.ok and r.channel == tx.SERIAL and r.fallback
    assert "udp refused" in r.reason and "tcp refused" in r.reason
    assert ser.sent == [build_cmd(CMD_SOFT_MARK, b"")]


def test_all_legs_fail_returns_none_not_bool():
    """[规格] 三腿全失败: ok=False/通道 none/原因非空, 且返回值不是裸 bool."""
    s = _sender(udp=FakeLeg(tx.UDP, exc=OSError("u1")),
                tcp=FakeLeg(tx.TCP, exc=OSError("u2")),
                serial=FakeLeg(tx.SERIAL, exc=ImportError("no pyserial")))
    r = s.send(CMD_SOFT_MARK)
    assert isinstance(r, tx.SendResult) and not isinstance(r, bool)
    assert r.ok is False and r.channel == tx.NONE and r.nbytes == 0
    for frag in ("u1", "u2", "no pyserial"):
        assert frag in r.reason
    assert len(r.attempts) == 3 and all(not a.ok for a in r.attempts)
    assert r.describe().startswith("下行失败")


def test_no_fallback_when_first_leg_ok():
    """[规格] 首腿成功不算降级."""
    r = _sender(udp=FakeLeg(tx.UDP)).send(CMD_SOFT_MARK)
    assert r.ok and not r.fallback and len(r.attempts) == 1


# ---------- 默认实现为真实 socket ----------

def test_default_legs_are_real_transports():
    """[规格] 未注入时三条腿为真实实现, 且构造不开句柄."""
    legs = _sender()._legs()
    assert [type(x) for x in legs] == [tx.UdpTransport, tx.TcpTransport, tx.SerialTransport]


def test_serial_unavailable_reason_nonempty():
    """[规格] 串口不存在时 reason 必须带端口名与异常类型(不是空串)."""
    s = _sender(udp=FakeLeg(tx.UDP, exc=OSError("udp down")),
                tcp=FakeLeg(tx.TCP, exc=OSError("tcp down")),
                serial=tx.SerialTransport(port="COM_NOT_EXIST_255"))
    r = s.send(CMD_SOFT_MARK)
    assert r.ok is False and r.channel == tx.NONE
    assert r.reason.strip() != ""
    assert "COM_NOT_EXIST_255" in r.reason
    assert "serial" in r.reason


# ---------- 真 socket loopback: 逐字节相等 ----------

def test_udp_loopback_byte_equal():
    """[规格] 真 UDP 环回: 收端收到的字节与 build_cmd 完全一致."""
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2.0)
    port = rx.getsockname()[1]
    try:
        payload = b"\x10\x11\x12"
        frame = build_cmd(CMD_SOFT_MARK, payload)
        r = tx.Sender(host="127.0.0.1", udp_port=port).send(CMD_SOFT_MARK, payload)
        got, _ = rx.recvfrom(4096)
    finally:
        rx.close()
    assert r.ok and r.channel == tx.UDP and not r.fallback
    assert r.nbytes == len(got) == len(frame)
    assert got == frame
    assert got.hex() == frame.hex()


def test_tcp_loopback_byte_equal():
    """[规格] 真 TCP 环回: 收端收到的字节与 build_cmd 完全一致."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(2.0)
    port = srv.getsockname()[1]
    held = {}

    def _acc():
        try:
            c, _ = srv.accept()
            c.settimeout(2.0)
            buf = b""
            while len(buf) < 4096:
                try:
                    d = c.recv(4096)
                except socket.timeout:
                    break
                if not d:
                    break
                buf += d
            c.close()
            held["data"] = buf
        finally:
            srv.close()

    t = threading.Thread(target=_acc, daemon=True)
    t.start()
    frame = build_cmd(CMD_SOFT_MARK, b"\x99")
    # UDP 腿注入失败, 才能把流量逼到真实 TCP 腿上(UDP 无 ACK, 否则首腿即"成功")
    r = tx.Sender(host="127.0.0.1", tcp_port=port,
                  udp=FakeLeg(tx.UDP, exc=OSError("udp 被屏蔽以测 TCP"))).send(
                      CMD_SOFT_MARK, b"\x99")
    t.join(3.0)
    assert r.ok and r.channel == tx.TCP and r.fallback
    assert held.get("data") == frame
    assert r.nbytes == len(frame)
