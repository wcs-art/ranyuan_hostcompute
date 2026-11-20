"""多设备管理：发现/接入/断开、跨设备时钟偏移、统一时间轴。"""
from __future__ import annotations
from typing import Optional, Union
import time
from dataclasses import dataclass, field

MAX_DEVICES = 8
@dataclass
class DeviceState:
    device_id: int
    connected: bool = False
    last_seq: int = -1
    lost: int = 0
    recv: int = 0
    offsets: list = field(default_factory=list)  # 最近N次时钟偏移(s)
    last_seen: float = 0.0
    reboots: int = 0
    def feed_seq(self, seq: int):
        if self.last_seq >= 0:
            if seq <= self.last_seq:
                self.reboots += 1
                self.last_seq = seq; self.recv += 1
                self.last_seen = time.time()
                return
            if seq != self.last_seq + 1:
                self.lost += seq - self.last_seq - 1
        self.last_seq = seq; self.recv += 1
        self.last_seen = time.time()

    def feed_clock(self, dev_ts_ms: int, pc_ts_ms: Optional[int] = None):
        pc = time.time()*1000 if pc_ts_ms is None else pc_ts_ms
        self.offsets.append((pc - dev_ts_ms)/1000.0)
        self.offsets = self.offsets[-120:]

    @property
    def offset_ms(self) -> float:
        if not self.offsets: return 0.0
        s = sorted(self.offsets)
        return s[len(s)//2]*1000.0

    @property
    def loss_rate(self) -> float:
        d = self.lost + self.recv
        return self.lost/d if d else 0.0

class DeviceManager:
    def __init__(self):
        self.devs: dict[int, DeviceState] = {}
    def discover(self, device_id: int) -> DeviceState:
        return self.devs.setdefault(device_id, DeviceState(device_id))
    def connect(self, device_id: int): self.discover(device_id).connected = True
    def disconnect(self, device_id: int):
        if device_id in self.devs: self.devs[device_id].connected = False
    @property
    def online(self) -> list[DeviceState]:
        return [d for d in self.devs.values() if d.connected]
    def table(self) -> str:
        lines = [f"{'头带':<4}{'在线':<5}{'整夜丢包率':<8}{'睡眠时钟差ms':<10}{'包序号':<8}"]
        for d in sorted(self.devs.values(), key=lambda x: x.device_id):
            lines.append(f"{d.device_id:<4}{('是' if d.connected else '否'):<5}"
                         f"{d.loss_rate:<8.2%}{d.offset_ms:<10.2f}{d.last_seq:<8}")
        return "\n".join(lines)
