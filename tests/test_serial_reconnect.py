"""串口断线自动重连: 板子复位 / Type-C 松动后必须自己回来。

为什么单独测:
    原实现在 read() 抛异常时直接 break 退出线程, 但 self.running 仍是 True,
    于是 start() 的 `if self.running: return` 永远拦住重开, 而全项目没有任何
    重连入口 —— 现场只能关掉整个程序, 表现成"链路不通"。
"""
from __future__ import annotations

import sys
import time
import types

import pytest

from receivers import LinkError, SerialReceiver


class _FakeSerial:
    """可编程假串口: 读 fail_after 次后抛"设备已移除", 模拟板子复位。"""

    def __init__(self, com: str, baud: int, timeout: float = 0.2, fail_after: int = 3):
        self.com = com
        self.baud = baud
        self.timeout = timeout
        self.fail_after = fail_after
        self.reads = 0
        self.closed = False
        self._payload = b"\xAA\x55" + bytes(10)
        self._left = 10

    @property
    def in_waiting(self) -> int:
        return len(self._payload)

    def read(self, n: int) -> bytes:
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise OSError("ClearCommError failed (device removed)")
        time.sleep(0.002)      # 像真的串口一样持续有数据, 但不空转吃满 CPU
        return self._payload

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def fake_serial(monkeypatch):
    """把 serial.Serial 换成假实现, 返回"造出来的串口句柄"列表。

    第一个句柄读 3 次后抛"设备已移除"(模拟板子复位), 之后重开的句柄都正常 ——
    这才是真实情形: 重连成功后就该恢复收数, 而不是一直故障。
    """
    made: list[_FakeSerial] = []

    def factory(com, baud, timeout=0.2):
        fs = _FakeSerial(com, baud, timeout, fail_after=3 if not made else None)
        made.append(fs)
        return fs

    mod = types.ModuleType("serial")
    mod.Serial = factory
    monkeypatch.setitem(sys.modules, "serial", mod)
    return made


def _wait(pred, timeout: float = 2.0, step: float = 0.02) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def test_serial_reconnects_after_device_removed(fake_serial):
    """读中断后应自动重开串口, 而不是让线程死掉。"""
    rx = SerialReceiver(com="COM_TEST", baud=921600, retry_s=0.1)
    rx.start()
    try:
        assert _wait(lambda: len(fake_serial) >= 2), (
            f"断线后应重新打开串口, 实际只打开过 {len(fake_serial)} 次; error={rx.error!r}"
        )
        # 重连成功后 error 应被清掉, 链路行回到绿色
        assert _wait(lambda: rx.error is None), f"重连成功但 error 未清: {rx.error!r}"
        # 重连后必须继续收字节(不是空转): 字节数要能继续增长
        before = rx.n_bytes
        assert _wait(lambda: rx.n_bytes > before), "重连后没有继续收数据"
        assert rx.running is True
    finally:
        rx.stop()


def test_stop_does_not_reconnect(fake_serial):
    """主动停止后不得再重连(否则关窗口会留下反复开串口的幽灵线程)。"""
    rx = SerialReceiver(com="COM_TEST", retry_s=0.1)
    rx.start()
    rx.stop()
    time.sleep(0.4)
    assert len(fake_serial) == 1, f"stop() 后仍重连了 {len(fake_serial)} 次"
    assert rx.running is False


def test_open_failure_still_raises_link_error(monkeypatch):
    """首次打开失败必须同步抛 LinkError —— 现场靠它弹窗, 不能静默进重连。"""

    def boom(*a, **k):
        raise OSError("拒绝访问, 端口被占用")

    mod = types.ModuleType("serial")
    mod.Serial = boom
    monkeypatch.setitem(sys.modules, "serial", mod)

    rx = SerialReceiver(com="COM9")
    with pytest.raises(LinkError) as ei:
        rx.start()
    assert "COM9" in str(ei.value)
    assert rx.running is False
