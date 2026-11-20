"""L1 传输层: WiFi(UDP / TCP服务端 / TCP客户端) + Type-C串口, 只出字节, 不解析帧.

职责边界: 本层只负责"把字节搬进来", 不做切帧、不做 CRC、不做折算。
切帧在 device_frame.FrameSlicer, 上层统一经 source.RealSource.poll_frames() 取帧。

四种链路的选择取决于 ESP8285/ESP-M1 模组的工作模式(现场用 AT 指令查):
  - 模组配成 UDP 透传, 主动往 PC 的 IP:端口发  -> udp        (本机 bind 监听)
  - 模组配成 TCP Client, 主动连 PC              -> tcp        (本机 bind+listen, 来连就收)
  - 模组配成 TCP Server, 等 PC 来连             -> tcp-client (本机主动 connect 模组IP)
  - Type-C 走 USB 转串口                        -> serial

三种失败都必须"说清楚", 不能静默: 端口被占用、模组连不上、串口打不开,
在界面上显示为明确的中文原因, 而不是"没有数据"。
"""
from __future__ import annotations

import socket
import threading
import time
from collections import deque

#: UDP 单包上限。原值 8192 会在模组攒包时截断(100帧/秒×90B≈9KB), 导致整包 CRC 全错。
MAX_DGRAM = 65535

#: 接收队列上限(包数)。1000 包≈10 秒的数据, 界面卡顿时不至于无限堆积。
QUEUE_MAX = 1000


class LinkError(RuntimeError):
    """链路打开失败。message 是给现场看的中文原因。"""


class _ByteReceiver:
    """所有接收器的公共骨架: 队列 + 统计 + 停止。"""

    kind = "base"

    def __init__(self, bufsize: int = MAX_DGRAM):
        self.bufsize = int(bufsize)
        self.q: deque[bytes] = deque(maxlen=QUEUE_MAX)
        self.running = False
        self.error: str | None = None
        self.n_packets = 0
        self.n_bytes = 0
        self.last_rx_t: float = 0.0
        self._sk = None

    # --- 供子类调用 ---
    def _push(self, data: bytes) -> None:
        if not data:
            return
        self.q.append(data)
        self.n_packets += 1
        self.n_bytes += len(data)
        self.last_rx_t = time.time()

    # --- 对外接口(与旧版兼容) ---
    def pop(self) -> bytes | None:
        return self.q.popleft() if self.q else None

    def stats(self) -> dict:
        idle = None if not self.last_rx_t else round(time.time() - self.last_rx_t, 1)
        return {
            "link": self.kind,
            "running": self.running,
            "packets": self.n_packets,
            "bytes": self.n_bytes,
            "idle_s": idle,
            "queued": len(self.q),
            "error": self.error,
        }

    def stop(self) -> None:
        self.running = False
        sk, self._sk = self._sk, None
        if sk is not None:
            try:
                sk.close()
            except OSError:
                pass


class UdpReceiver(_ByteReceiver):
    """UDP 监听。模组主动往本机端口发字节。"""

    kind = "udp"

    def __init__(self, port: int = 5000, host: str = "0.0.0.0", bufsize: int = MAX_DGRAM):
        super().__init__(bufsize)
        self.port = int(port)
        self.host = host

    def start(self) -> None:
        if self.running:  # 幂等: 入口与界面可能各调一次
            return
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sk.bind((self.host, self.port))
        except OSError as e:
            sk.close()
            raise LinkError(
                f"UDP {self.port} 端口无法监听: {e}。"
                f"常见原因: 端口已被别的程序占用, 或该端口被系统保留。"
                f"换一个端口(--port=5001)再试。"
            ) from e
        sk.settimeout(0.5)
        self._sk = sk
        self.running = True
        self.error = None

        def _loop() -> None:
            while self.running:
                try:
                    data, _ = sk.recvfrom(self.bufsize)
                except socket.timeout:
                    continue
                except OSError as e:
                    if self.running:
                        self.error = f"UDP 接收中断: {e}"
                    break
                self._push(data)

        threading.Thread(target=_loop, daemon=True, name="udp-rx").start()


class TcpReceiver(_ByteReceiver):
    """TCP 服务端。模组配成 TCP Client 主动连过来时用这个。"""

    kind = "tcp"

    def __init__(self, port: int = 5000, host: str = "0.0.0.0", bufsize: int = MAX_DGRAM):
        super().__init__(bufsize)
        self.port = int(port)
        self.host = host
        self.n_clients = 0

    def start(self) -> None:
        if self.running:
            return
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.host, self.port))
            srv.listen(8)
        except OSError as e:
            srv.close()
            raise LinkError(
                f"TCP {self.port} 端口无法监听: {e}。"
                f"常见原因: 端口已被占用。换 --port=5001 再试; "
                f"若模组是 TCP Server 模式, 应改用 --proto=tcp-client。"
            ) from e
        srv.settimeout(0.5)
        self._sk = srv
        self.running = True
        self.error = None

        def _client(c: socket.socket) -> None:
            c.settimeout(0.5)
            self.n_clients += 1
            try:
                while self.running:
                    try:
                        d = c.recv(self.bufsize)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not d:
                        break
                    self._push(d)
            finally:
                self.n_clients -= 1
                try:
                    c.close()
                except OSError:
                    pass

        def _loop() -> None:
            while self.running:
                try:
                    c, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError as e:
                    if self.running:
                        self.error = f"TCP 监听中断: {e}"
                    break
                threading.Thread(target=_client, args=(c,), daemon=True, name="tcp-cli").start()

        threading.Thread(target=_loop, daemon=True, name="tcp-srv").start()


class TcpClientReceiver(_ByteReceiver):
    """TCP 客户端。模组配成 TCP Server 时, 由本机主动连模组 IP。

    断线自动重连(每 1 秒一次), 所以模组重启不需要重启上位机。
    """

    kind = "tcp-client"

    def __init__(self, host: str = "192.168.4.1", port: int = 8080,
                 bufsize: int = MAX_DGRAM, retry_s: float = 1.0):
        super().__init__(bufsize)
        self.host = host or "192.168.4.1"
        self.port = int(port)
        self.retry_s = float(retry_s)
        self.connected = False

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.error = None

        def _loop() -> None:
            while self.running:
                try:
                    c = socket.create_connection((self.host, self.port), timeout=2.0)
                except OSError as e:
                    self.connected = False
                    self.error = (
                        f"连不上模组 {self.host}:{self.port} ({e})。"
                        f"请确认: PC 与模组在同一网段、模组是 TCP Server 模式、端口正确。"
                    )
                    time.sleep(self.retry_s)
                    continue
                c.settimeout(0.5)
                self.connected = True
                self.error = None
                self._sk = c
                try:
                    while self.running:
                        try:
                            d = c.recv(self.bufsize)
                        except socket.timeout:
                            continue
                        except OSError:
                            break
                        if not d:
                            break
                        self._push(d)
                finally:
                    self.connected = False
                    try:
                        c.close()
                    except OSError:
                        pass
                    self._sk = None
                if self.running:
                    self.error = f"与模组 {self.host}:{self.port} 的连接已断开, 正在重连…"
                    time.sleep(self.retry_s)

        threading.Thread(target=_loop, daemon=True, name="tcp-cli-conn").start()

    def stats(self) -> dict:
        d = super().stats()
        d["connected"] = self.connected
        d["peer"] = f"{self.host}:{self.port}"
        return d


class SerialReceiver(_ByteReceiver):
    """Type-C(USB转串口) 有线链路。需 pyserial。"""

    kind = "serial"

    def __init__(self, com: str = "COM3", baud: int = 921600, bufsize: int = MAX_DGRAM,
                 retry_s: float = 2.0):
        super().__init__(bufsize)
        self.com = com
        self.baud = int(baud)
        self._ser = None
        #: 断线后自动重连的间隔(秒)
        self.retry_s = float(retry_s)

    def _open(self):
        """打开串口。失败抛 LinkError(中文原因); 重连路径自行吞掉并重试。"""
        try:
            import serial  # 延迟导入: 没装 pyserial 也不影响 WiFi 链路
        except ImportError as e:
            raise LinkError("缺少 pyserial, 无法使用 Type-C 串口链路: pip install pyserial") from e
        try:
            return serial.Serial(self.com, self.baud, timeout=0.2)
        except Exception as e:  # noqa: BLE001 - pyserial 抛的异常类型很杂
            raw = str(e)
            # Windows 串口是独占的: 别的程序正开着这个口时报的是 PermissionError,
            # 现场最常见的元凶就是 VOFA+ / 串口助手 / 另一个上位机实例。
            # 只说"拒绝访问"没人猜得到要去关 VOFA, 所以按 errno 给下一步。
            if "PermissionError" in raw or "拒绝访问" in raw:
                why = (f"串口 {self.com} 被占用（拒绝访问）。"
                       f"同一个口只能被一个程序打开：请关掉 VOFA+/串口助手/"
                       f"或另一个上位机实例后重试。")
            elif "FileNotFoundError" in raw or "找不到" in raw:
                why = (f"串口 {self.com} 不存在。线没插好，或口名不是这个"
                       f"（在设备管理器 → 端口(COM 和 LPT) 里看实际编号）。")
            else:
                why = f"串口 {self.com} 打不开。"
            raise LinkError(f"{why} 原始错误: {raw}") from e

    def start(self) -> None:
        if self.running:  # 幂等: 入口与界面可能各调一次
            return
        self._ser = self._open()      # 启动即失败要同步抛出, 现场才能弹窗看见原因
        self.running = True
        self.error = None
        threading.Thread(target=self._supervise, args=(self._ser,),
                         daemon=True, name="serial-rx").start()

    def _read_loop(self, ser) -> None:
        while self.running:
            n = ser.in_waiting or 1
            d = ser.read(min(n, self.bufsize))
            if d:
                self._push(d)

    def _supervise(self, ser) -> None:
        """读循环 + 断线自动重连。

        为什么必须有: 板子复位或 Type-C 松动会让 USB 转串口重新枚举, read() 抛
        SerialException。若只是 break 退出线程, self.running 仍是 True, 于是
        start() 的幂等判断永远拦住重开 —— 现场只能关掉整个程序重来, 表现成
        "链路不通"。这里改成按 retry_s 自动重开, 重连期间原因写在 error 里。
        """
        while True:
            try:
                self._read_loop(ser)
            except Exception as e:  # noqa: BLE001
                if self.running:
                    self.error = f"串口读取中断: {e}"
            finally:
                try:
                    ser.close()
                except Exception:  # noqa: BLE001
                    pass
                if self._ser is ser:
                    self._ser = None
            if not self.running:      # 主动 stop(): 不再重连
                return
            # 非主动停止 => 线松了 / 板子复位, 每隔 retry_s 重开一次
            while self.running:
                self.error = f"串口 {self.com} 已断开, 每 {self.retry_s:g}s 自动重连…"
                deadline = time.time() + self.retry_s
                while self.running and time.time() < deadline:   # 分片 sleep, stop() 能立刻响应
                    time.sleep(0.1)
                if not self.running:
                    return
                try:
                    ser = self._open()
                except LinkError:
                    continue          # 具体原因(被占用/找不到口)仍留在 self.error
                self._ser = ser
                self.error = None
                break

    def stop(self) -> None:
        super().stop()
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass


def make_receiver(proto: str = "udp", port: int = 5000, host: str = "0.0.0.0",
                  com: str = "COM3", baud: int = 921600) -> _ByteReceiver:
    """按链路类型造接收器。proto 取值见模块头注释。"""
    p = (proto or "udp").lower()
    if p == "udp":
        return UdpReceiver(port=port, host=host)
    if p == "tcp":
        return TcpReceiver(port=port, host=host)
    if p in ("tcp-client", "tcpclient", "client"):
        return TcpClientReceiver(host=host if host != "0.0.0.0" else "192.168.4.1", port=port)
    if p in ("serial", "uart", "com"):
        return SerialReceiver(com=com, baud=baud)
    raise LinkError(f"未知链路 --proto={proto}, 可选 udp / tcp / tcp-client / serial")


def open_uart_fallback(port: str = "COM6", baud: int = 921600):
    """UART 有线透传降级(单句柄直开, 供 tx.py 下行发送用)。需 pyserial。"""
    import serial
    return serial.Serial(port, baud, timeout=0.1)


# 下行命令帧的字节格式与命令码**只有一处定义**：commands.py（对应 docs/数据协议_v1.md §6）。
# 这里仅做转发，不再自己拼帧。
#
# 为什么去掉旧实现：旧版 build_cmd 用的是上行数据帧头 AA55、没有 dev/len 字段，
# 且命令码与协议文档互换（0x02 当软标记、0x03 当 SYNC OUT）。固件若按文档实现，
# 按"软标记"实际会把 SYNC OUT 配成 0.5 Hz；而 AA55 开头的下行帧还可能被设备
# 当成一帧上行数据。两条都会让"按下按钮没反应"变得无法定位。
from commands import Cmd as _Cmd, encode as _encode_command

CMD_SYNC_TIME = int(_Cmd.SET_TIME)        # 0x01 对时
CMD_SYNC_OUT_CFG = int(_Cmd.SYNC_OUT)     # 0x02 周期同步脉冲
CMD_SOFT_MARK = int(_Cmd.SOFT_MARKER)     # 0x03 软标记（注意不是 0x02）


def build_cmd(cmd: int, payload: bytes = b"", dev: int = 1) -> bytes:
    """组一帧下行命令 = AA56|dev|cmd|len|payload|CRC16（协议文档 §6.1）。

    dev 默认 1，与界面单机场景一致；多机时显式传设备号。
    """
    return _encode_command(int(dev), cmd, payload)
