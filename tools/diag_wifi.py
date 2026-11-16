"""WiFi 链路回环自检（临时工具，不属于产品代码）。

注意：本脚本只做本机回环（自己发自己收），不碰板子。
要扫板子的串口/IP/端口、嗅探真实帧格式，用 tools/preflight.py。

不依赖板子：自己组真实帧，从本机 socket 发到接收端，验证
  UDP / TCP 两条链路能否完整收出帧、序号是否连续、CRC 通过率、可持续帧率。

用法：
    .venv/Scripts/python.exe tools/diag_wifi.py          # 默认 UDP:5005 + TCP:5006
    .venv/Scripts/python.exe tools/diag_wifi.py 5005     # 指定 UDP 端口
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ev1"))

import device_frame as df  # noqa: E402

FRAME = df.get_layout().frame_len


def make_frames(n: int, dev: int = 7) -> list[bytes]:
    """时间戳按协议是设备毫秒计数(uint32)，不是 Unix 时间戳 —— 每帧 10 ms。"""
    out = []
    for i in range(n):
        eeg = [1000 + i, -2000 - i, 300000 + i, -400000 - i]
        fnirs = [[100 + i + c + w * 50 for w in range(2)] for c in range(10)]
        out.append(df.pack_frame(dev, i, i * 10, eeg, fnirs, 0, 0))
    return out


def drain(src, expect: int, timeout_s: float = 3.0) -> tuple[list[dict], float]:
    """轮询直到收齐 expect 帧或超时。"""
    t0 = time.time()
    got: list[dict] = []
    while len(got) < expect and time.time() - t0 < timeout_s:
        got.extend(src.poll_frames())
        if len(got) < expect:
            time.sleep(0.005)
    return got, time.time() - t0


def report(tag: str, got: list[dict], sent: int, elapsed: float) -> bool:
    seqs = [f["seq"] for f in got]
    gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if b != a + 1)
    loss = sent - len(got)
    fps = len(got) / elapsed if elapsed > 0 else 0
    ok = len(got) == sent and gaps == 0
    print(f"[{tag}] 发出 {sent} 帧 / 收到 {len(got)} 帧 / 丢 {loss} / 序号断点 {gaps}")
    print(f"[{tag}] 耗时 {elapsed*1000:.0f} ms  ->  {fps:,.0f} 帧/秒  ({fps*FRAME/1024:.0f} KB/s)")
    print(f"[{tag}] 结论: {'通' if ok else '不通或丢帧'}")
    if got:
        f = got[0]
        print(f"[{tag}] 首帧字段: seq={f['seq']} EEG={f['eeg_uv'][:2]} fNIRS强度前4={f['raw'][:4] if f.get('raw') else 'N/A'}")
    return ok


def test_udp(port: int, n: int = 300) -> bool:
    from source import RealSource

    src = RealSource(port=port, proto="udp")
    src.start()
    time.sleep(0.2)  # 等 bind 完成
    frames = make_frames(n)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    t0 = time.time()
    # 故意按不规则块发送，模拟帧被 UDP 报文边界切开
    i, chunks = 0, 0
    blob = b"".join(frames)
    step_seq = (FRAME, 47, FRAME*2, 13, FRAME*4, FRAME)
    while i < len(blob):
        k = step_seq[chunks % len(step_seq)]
        sock.sendto(blob[i:i + k], ("127.0.0.1", port))
        i += k
        chunks += 1
        time.sleep(0.0008)
    sock.close()
    got, dt = drain(src, n)
    src.stop()
    return report("UDP", got, n, dt)


def test_tcp(port: int, n: int = 300) -> bool:
    from source import RealSource

    src = RealSource(port=port, proto="tcp")
    src.start()
    time.sleep(0.3)
    frames = make_frames(n)
    blob = b"".join(frames)
    c = socket.create_connection(("127.0.0.1", port), timeout=2)
    t0 = time.time()
    # 故意按不规则块发送，模拟 TCP 粘包
    i, k = 0, 0
    step = (137, 64, 251, 40, 400)
    while i < len(blob):
        sz = step[k % len(step)]
        c.sendall(blob[i:i + sz])
        i += sz
        k += 1
        time.sleep(0.0008)
    got, dt = drain(src, n)
    c.close()
    src.stop()
    return report("TCP", got, n, dt)


def test_sustained(port: int, fps: int = 100, seconds: float = 3.0) -> None:
    """按真实节拍（100 帧/秒）持续发送，看长时间是否丢包/积压。"""
    from source import RealSource

    src = RealSource(port=port, proto="udp")
    src.start()
    time.sleep(0.2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    total = int(fps * seconds)
    frames = make_frames(total)
    t0 = time.time()
    sent = 0
    for i, f in enumerate(frames):
        target = t0 + i / fps
        now = time.time()
        if target > now:
            time.sleep(target - now)
        sock.sendto(f, ("127.0.0.1", port))
        sent += 1
    sock.close()
    got, dt = drain(src, total, timeout_s=2.0)
    src.stop()
    report(f"UDP持续{fps}fps", got, sent, dt)


if __name__ == "__main__":
    base = int(sys.argv[1]) if len(sys.argv) > 1 else 5005
    print("=" * 62)
    print("WiFi 链路回环自检（本机模拟，不接板子）")
    print("=" * 62)
    r1 = test_udp(base)
    print("-" * 62)
    r2 = test_tcp(base + 1)
    print("-" * 62)
    test_sustained(base + 2)
    print("=" * 62)
    print("汇总: UDP", "PASS" if r1 else "FAIL", "| TCP", "PASS" if r2 else "FAIL")
