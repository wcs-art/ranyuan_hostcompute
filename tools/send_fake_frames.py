"""模拟板子发帧：在没有硬件时验证整条链路（收包→切帧→界面→录制）。

板子到之前可以先跑它，确认上位机本身没问题；板子到了之后，
如果它发的能收、板子发的不能收，问题就一定在固件侧或线路上。

用法：
    # 另开一个窗口跑上位机
    python main.py --source=real --proto=udp --port=5000 --raw-dump=capture/bench.bin
    # 再跑本工具，按 100 帧/秒发 2000 帧（20 秒）
    .venv/Scripts/python.exe tools/send_fake_frames.py 127.0.0.1 5000 2000 100
"""
from __future__ import annotations

import math
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ev1"))

import device_frame as df  # noqa: E402


def build(i: int) -> bytes:
    # 折算系数取当前布局/口径的真实值（电极端 µV/LSB）。写死 0.022 的话，
    # 帧布局或折算口径一改，这里发的"20 µV"就不再是 20 µV，界面会是平的，
    # 但看上去像"链路不通"，最容易把排查方向带偏。
    gain = df.EEG_GAIN_UV_PER_LSB
    ph = i / 100.0
    eeg = [int(20 * math.sin(2 * math.pi * 10 * ph) / gain),
           int(10 * math.sin(2 * math.pi * 5 * ph) / gain),
           0, 0]
    fnirs = [[int(2000 + 200 * math.sin(2 * math.pi * 0.1 * ph + c)) for _ in range(2)]
             for c in range(10)]
    return df.pack_frame(dev=7, seq=i & 0xFFFFFFFF, ts=i * 10, eeg=eeg, fnirs=fnirs)


def main(argv: list[str]) -> int:
    host = argv[0] if len(argv) > 0 else "127.0.0.1"
    port = int(argv[1]) if len(argv) > 1 else 5000
    count = int(argv[2]) if len(argv) > 2 else 2000
    fps = float(argv[3]) if len(argv) > 3 else 100.0
    proto = argv[4] if len(argv) > 4 else "udp"

    print(f"目标 {host}:{port}  协议 {proto}  {count} 帧 @ {fps} fps")
    if proto == "udp":
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        t0 = time.time()
        for i in range(count):
            target = t0 + i / fps
            now = time.time()
            if target > now:
                time.sleep(target - now)
            sk.sendto(build(i), (host, port))
        sk.close()
    else:
        c = socket.create_connection((host, port), timeout=3)
        t0 = time.time()
        for i in range(count):
            target = t0 + i / fps
            now = time.time()
            if target > now:
                time.sleep(target - now)
            c.sendall(build(i))
        c.close()
    dt = time.time() - t0
    print(f"完成: {count} 帧 / {dt:.1f}s = {count/max(dt,1e-9):.1f} fps, "
          f"共 {count*df.get_layout().frame_len} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
