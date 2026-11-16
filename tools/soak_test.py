"""序号计数器自检：喂连号判丢包计数与内存是否随帧数增长。这不是 F6 的 2h 真机 soak。

真机 soak 用主程序连续记录 2 小时，看链路行丢包率与 CSV 行数。
用法: python tools/soak_test.py [minutes, 默认5]"""
from __future__ import annotations
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ev1"))
from device_manager import DeviceManager

_NO_PSUTIL = False

def rss_mb() -> float:
    global _NO_PSUTIL
    try:
        import psutil, os
        return psutil.Process(os.getpid()).memory_info().rss/1e6
    except Exception:
        if not _NO_PSUTIL:
            print("[提示] 没装 psutil，内存采样跳过（pip install psutil）")
            _NO_PSUTIL = True
        return -1.0

def main(minutes: float = 5.0):
    mgr = DeviceManager(); mgr.connect(1); d = mgr.discover(1)
    t0 = time.time(); m0 = rss_mb(); seq = 0
    end = t0 + minutes*60
    while time.time() < end:
        for _ in range(500):  # 模拟500Hz一秒的量
            seq += 1; d.feed_seq(seq)
        time.sleep(1.0)
        el = (time.time()-t0)/60
        print(f"[{el:.1f}min] recv={d.recv} lost={d.lost} loss={d.loss_rate:.3%} rss={rss_mb():.1f}MB")
    m1 = rss_mb()
    leak = (m1-m0)/max((time.time()-t0)/3600, 1e-6) if m0 > 0 else 0.0
    print(f"RESULT loss={d.loss_rate:.4%} leak≈{leak:.1f}MB/h "
          + ("PASS" if d.loss_rate == 0 and (leak < 50 or m0 < 0) else "CHECK"))

if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 5.0)
