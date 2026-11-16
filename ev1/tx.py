"""B3 下行发送通路: UDP → TCP → 串口 逐级降级.

设计约束(见任务书 B3):
- 命令帧唯一来源是 ``transport.receivers.build_cmd``, 本文件不重写帧头/CRC/帧长.
- ``send()`` 只返回 ``SendResult``, 不返回裸 bool, 不静默吞异常:
  每条腿的失败都落进 ``attempts`` 与 ``reason``, 全失败时 ``ok=False`` 且 ``reason`` 非空.
- 传输实现可在构造时注入(``udp=`` / ``tcp=`` / ``serial_transport=``), 便于用内存假对象测试;
  默认实现在首次 ``send`` 时才创建, import 本模块不开端口、不碰串口.
- 每条腿超时有界: UDP 0.15s / TCP 0.2s / 串口 0.1s, 最坏 ~0.45s, 端口不通不会挂住.
- **UDP 无 ACK**: 目标端口无人监听时 sendto 仍"成功"(本机 ICMP 不可达不会同步返回),
  只有 socket 层错误(地址解析失败/网络不可达/短写)才判失败并降级.
  需要投递确认请走 TCP 或读回读命令, 不要靠 UDP 返回值判定设备是否收到.
"""
from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field

from receivers import (
    CMD_SOFT_MARK,
    CMD_SYNC_OUT_CFG,
    CMD_SYNC_TIME,
    build_cmd,
    open_uart_fallback,
)

UDP = "udp"
TCP = "tcp"
SERIAL = "serial"
NONE = "none"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_UDP_PORT = 5000
DEFAULT_TCP_PORT = 5000
DEFAULT_SERIAL_PORT = "COM6"
DEFAULT_SERIAL_BAUD = 921600

UDP_TIMEOUT = 0.15
TCP_TIMEOUT = 0.2
SERIAL_TIMEOUT = 0.1

__all__ = [
    "UDP", "TCP", "SERIAL", "NONE",
    "CMD_SOFT_MARK", "CMD_SYNC_TIME", "CMD_SYNC_OUT_CFG",
    "Attempt", "SendResult", "UdpTransport", "TcpTransport", "SerialTransport",
    "Sender", "default_sender", "send", "ts_payload",
]


@dataclass
class Attempt:
    """单条传输腿的尝试记录(成功或失败都必须留痕)."""
    channel: str
    ok: bool
    reason: str = ""
    elapsed_ms: float = 0.0


@dataclass
class SendResult:
    """下行发送统一结果.

    channel: 实际成功的通道 udp/tcp/serial, 全失败为 none
    nbytes : 交给通道的字节数(成功); 全失败为 0
    fallback: 是否发生过降级(前面有腿失败)
    reason : 失败/降级原因; 成功且无降级时为空, 成功但有降级时为被跳过腿的原因
    """
    ok: bool
    channel: str
    nbytes: int
    fallback: bool
    reason: str = ""
    attempts: list = field(default_factory=list)
    elapsed_ms: float = 0.0

    def describe(self) -> str:
        """给 UI 用的一行摘要, 失败原因不会被吞掉."""
        if self.ok:
            head = "%s %dB%s" % (self.channel, self.nbytes, " 降级" if self.fallback else "")
        else:
            head = "下行失败"
        return head + (" (%s)" % self.reason if self.reason else "")


def _join_failures(attempts: list) -> str:
    return "；".join("%s: %s" % (a.channel, a.reason) for a in attempts if not a.ok)


class UdpTransport:
    """UDP 单包发送. 无 ACK, 仅 socket 层错误可判定失败."""

    channel = UDP

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_UDP_PORT,
                 timeout: float = UDP_TIMEOUT):
        self.host = host
        self.port = int(port)
        self.timeout = timeout

    def write(self, data: bytes) -> int:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sk.settimeout(self.timeout)
            sk.connect((self.host, self.port))
            n = sk.send(bytes(data))
        finally:
            sk.close()
        if n != len(data):
            raise OSError("UDP 短写 %d/%dB" % (n, len(data)))
        return n


class TcpTransport:
    """TCP 连接即发即关, 连接被拒/超时都会抛异常(带超时上界)."""

    channel = TCP

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_TCP_PORT,
                 timeout: float = TCP_TIMEOUT):
        self.host = host
        self.port = int(port)
        self.timeout = timeout

    def write(self, data: bytes) -> int:
        sk = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            sk.settimeout(self.timeout)
            sk.sendall(bytes(data))
        finally:
            sk.close()
        return len(data)


class SerialTransport:
    """串口透传, 复用 receivers.open_uart_fallback(不做第二套串口实现).

    pyserial 未装 -> ImportError; 端口不存在/被占用 -> SerialException.
    两种情况都原样抛给 Sender 记账, 由 SendResult.reason 暴露.
    """

    channel = SERIAL

    def __init__(self, port: str = DEFAULT_SERIAL_PORT, baud: int = DEFAULT_SERIAL_BAUD,
                 timeout: float = SERIAL_TIMEOUT):
        self.port = port
        self.baud = int(baud)
        self.timeout = timeout

    def write(self, data: bytes) -> int:
        ser = open_uart_fallback(self.port, self.baud)
        try:
            n = ser.write(bytes(data))
        finally:
            ser.close()
        if n != len(data):
            raise OSError("串口短写 %d/%dB" % (n, len(data)))
        return n


class Sender:
    """下行发送器: UDP → TCP → 串口.

    默认目标 127.0.0.1:5000 与 COM6@921600, 全部可构造期覆盖;
    也可直接注入三个传输对象(测试用内存假对象), 注入的腿不再走真实 socket/串口.
    """

    def __init__(self, host: str = DEFAULT_HOST,
                 udp_port: int = DEFAULT_UDP_PORT,
                 tcp_port: int = DEFAULT_TCP_PORT,
                 serial_port: str = DEFAULT_SERIAL_PORT,
                 serial_baud: int = DEFAULT_SERIAL_BAUD,
                 udp=None, tcp=None, serial_transport=None):
        self.host = host
        self.udp_port = int(udp_port)
        self.tcp_port = int(tcp_port)
        self.serial_port = serial_port
        self.serial_baud = int(serial_baud)
        self._udp = udp
        self._tcp = tcp
        self._serial = serial_transport

    def _legs(self) -> list:
        """惰性建默认腿: import 与构造都不产生 socket/串口句柄."""
        if self._udp is None:
            self._udp = UdpTransport(self.host, self.udp_port)
        if self._tcp is None:
            self._tcp = TcpTransport(self.host, self.tcp_port)
        if self._serial is None:
            self._serial = SerialTransport(self.serial_port, self.serial_baud)
        return [self._udp, self._tcp, self._serial]

    def send(self, command: int, payload: bytes = b"") -> SendResult:
        """按 UDP → TCP → 串口发送一帧命令, 返回 SendResult(不抛传输异常)."""
        frame = build_cmd(command, payload)
        attempts = []
        t_start = time.perf_counter()
        for leg in self._legs():
            name = getattr(leg, "channel", type(leg).__name__)
            t_leg = time.perf_counter()
            try:
                n = leg.write(frame)
            except Exception as e:  # 逐腿兜底: 异常必须落到 attempts/reason, 不得静默
                attempts.append(Attempt(name, False, "%s: %s" % (type(e).__name__, e),
                                        (time.perf_counter() - t_leg) * 1000.0))
                continue
            cost = (time.perf_counter() - t_leg) * 1000.0
            if n != len(frame):
                attempts.append(Attempt(name, False, "短写 %r/%dB" % (n, len(frame)), cost))
                continue
            attempts.append(Attempt(name, True, "", cost))
            return SendResult(ok=True, channel=name, nbytes=int(n),
                              fallback=len(attempts) > 1,
                              reason=_join_failures(attempts), attempts=attempts,
                              elapsed_ms=(time.perf_counter() - t_start) * 1000.0)
        return SendResult(ok=False, channel=NONE, nbytes=0,
                          fallback=len(attempts) > 1,
                          reason=_join_failures(attempts) or "无可用传输",
                          attempts=attempts,
                          elapsed_ms=(time.perf_counter() - t_start) * 1000.0)


_default_sender = None


def default_sender() -> Sender:
    """进程内默认 Sender(单例), 首次调用才构造."""
    global _default_sender
    if _default_sender is None:
        _default_sender = Sender()
    return _default_sender


def send(command: int, payload: bytes = b"") -> SendResult:
    """模块级便捷入口, 走 default_sender()."""
    return default_sender().send(command, payload)


def ts_payload(ts_ms: int) -> bytes:
    """32 位设备时基小端（回绕截断）。

    注意：**这不是下行的软标记载荷**了。协议 §6.3 规定 SOFT_MARKER 载荷是 1 字节
    事件类型、SET_TIME 载荷是 u64 Unix 毫秒；两者都请走 commands.py
    （commands.soft_marker / commands.set_time）。本函数保留仅为兼容旧调用与测试。
    """
    return struct.pack("<I", int(ts_ms) & 0xFFFFFFFF)
