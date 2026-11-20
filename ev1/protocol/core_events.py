"""同步事件：SYNC IN 事件标记、SYNC OUT 脉冲、软标记。"""
from __future__ import annotations
import time
EV_NONE=0; EV_SYNC_IN=1; EV_EXT_TRIG=2; EV_SOFT=3; EV_SYNC_OUT=4
_T0=time.monotonic()
def dev_ms()->int: return int((time.monotonic()-_T0)*1000)
class EventBus:
    def __init__(self): self.evs:list[tuple[int,int,int]]=[]
    def push(self, typ:int)->tuple[int,int]:
        ts=dev_ms(); self.evs.append((typ,ts,len(self.evs))); return typ,ts
    def soft_mark(self)->tuple[int,int]: return self.push(EV_SOFT)
    def sync_in(self)->tuple[int,int]: return self.push(EV_SYNC_IN)
class SyncOutGen:
    """SYNC OUT可配置周期脉冲0.5/1/2Hz."""
    def __init__(self, hz=1.0): self.hz=hz; self._last=0.0
    def set_hz(self,hz):
        assert hz in (0.5,1.0,2.0); self.hz=hz
    def poll(self, now_s:float)->bool:
        if now_s-self._last>=1.0/self.hz: self._last=now_s; return True
        return False