"""录制链路自检：真机字节 -> 切帧 -> 界面 -> CSV，检查列结构与光强是否真实。

用法：.venv/Scripts/python.exe tools/diag_record.py
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ev1"))
import os  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import socket  # noqa: E402

import device_frame as df  # noqa: E402
from PyQt5 import QtWidgets  # noqa: E402

N = 50


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)

    import contract
    from monitor_14ch import Monitor14
    from source import RealSource

    port = 5200
    src = RealSource(port=port, proto="udp", raw_dump=str(ROOT / "capture" / "rec_test.bin"))
    src.start()
    time.sleep(0.2)

    # 发 N 帧：光强码值 2000+i，用来验证 CSV 里是不是真值
    blob = bytearray()
    for i in range(N):
        blob += df.pack_frame(7, i, i * 10,
                              [1000 + i] * 4,
                              [[2000 + i + c * 7] * 2 for c in range(10)], 0, 0)
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for i in range(0, len(blob), 900):      # 每包 10 帧，模拟模组攒包
        sk.sendto(bytes(blob[i:i + 900]), ("127.0.0.1", port))
        time.sleep(0.01)
    sk.close()

    mon = Monitor14()
    mon.attach_receiver(src)
    mon._toggle_record(True)
    got = 0
    t0 = time.time()
    while got < N and time.time() - t0 < 3:
        for fr in src.poll_frames():
            mon.push_frame(**fr)
            got += 1
        time.sleep(0.01)
    mon._toggle_record(False)
    src.stop()

    path = mon._csv_path
    print(f"[录制] 收到并推送 {got} 帧 -> {path}")

    with path.open(encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    hdr, body = rows[0], rows[1:]
    print(f"[录制] 表头 {len(hdr)} 列 / 数据 {len(body)} 行 / 每行 {len(body[0]) if body else 0} 列")
    ok_len = bool(body) and len(body[0]) == len(hdr)
    print(f"[录制] 列数一致: {ok_len}")

    # 检查必需列是否齐全
    missing = [c for c in contract.REQUIRED_COLUMNS if c not in hdr]
    print(f"[录制] 必需列缺失: {missing if missing else '无'}")

    if body:
        idx = hdr.index("fnirs1_760_intensity")
        idx_raw = hdr.index("fnirs1_760_raw")
        inten = [float(r[idx]) for r in body[:5]]
        raws = [float(r[idx_raw]) for r in body[:5]]
        print(f"[录制] 前5行 fnirs1_760_raw  = {raws}")
        print(f"[录制] 前5行 对应强度         = {inten}")
        nonzero = all(v > 0 for v in inten)
        print(f"[录制] 强度列是真实值(非0): {nonzero}")
        eeg = [float(r[hdr.index('eeg1_uv')]) for r in body[:3]]
        print(f"[录制] 前3行 eeg1_uv = {eeg}")

    good = ok_len and not missing and bool(body)
    print("=" * 56)
    print("录制链路:", "PASS" if good else "FAIL")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
