"""S7首版: 单设备LSL三路推流(睡眠脑电/前额血氧/睡眠事件标记), pylsl可接收验证. 对照要求§八.
终版多机统一时间轴预留device_id->Outlet映射. 未装pylsl时为占位模式(仅缓存)."""
from __future__ import annotations
try:
    from pylsl import StreamInfo, StreamOutlet
    _HAS=True
except Exception: _HAS=False
class TripleOutlet:
    def __init__(self, device_id=1, sleep_eeg_fs=500.0, sleep_hb_fs=10.0):
        self.device_id=device_id; self.cache=[[],[],[]]
        if _HAS:
            self.sleep_eeg=StreamOutlet(StreamInfo(f"Aidsleep_SleepEEG_{device_id}","SleepEEG",4,sleep_eeg_fs,"float32",f"aid{device_id}"))
            self.sleep_hb=StreamOutlet(StreamInfo(f"Aidsleep_SleepHb_{device_id}","SleepHb",20,sleep_hb_fs,"float32",f"aid{device_id}"))
            self.sleep_evt=StreamOutlet(StreamInfo(f"Aidsleep_SleepEvt_{device_id}","SleepMarkers",1,0,"int32",f"aid{device_id}"))
        else: self.sleep_eeg=self.sleep_hb=self.sleep_evt=None
    def push_sleep_eeg(self,s): (self.sleep_eeg.push_sample(s) if self.sleep_eeg else self.cache[0].append(s))
    def push_sleep_hb(self,s): (self.sleep_hb.push_sample(s) if self.sleep_hb else self.cache[1].append(s))
    def push_event(self,v): (self.sleep_evt.push_sample([v]) if self.sleep_evt else self.cache[2].append(v))
    @property
    def live(self)->bool: return self.sleep_eeg is not None
