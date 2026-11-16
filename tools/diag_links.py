"""链路层四模式自检（临时工具）。用法：.venv/Scripts/python.exe tools/diag_links.py"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ev1"))

import device_frame as df  # noqa: E402

#: 帧长不再写死: 协议改成 v1(104B) 后, 用 90 做字节数断言会全部误判为失败
FRAME = df.get_layout().frame_len
from receivers import LinkError, make_receiver  # noqa: E402


def frames(n: int, dev: int = 7) -> bytes:
    out = bytearray()
    for i in range(n):
        out += df.pack_frame(dev, i, i * 10, [1000 + i] * 4, [[100 + i] * 2] * 10, 0, 0)
    return bytes(out)


def test_udp(port: int) -> bool:
    rx = make_receiver("udp", port=port)
    rx.start()
    time.sleep(0.2)
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk.sendto(frames(50), ("127.0.0.1", port))
    sk.close()
    time.sleep(0.3)
    got = 0
    while rx.pop() is not None:
        got += 1
    st = rx.stats()
    rx.stop()
    print(f"  UDP        : 包 {st['packets']} 字节 {st['bytes']} 错误={st['error']}")
    return st["packets"] == 1 and st["bytes"] == 50 * FRAME


def test_tcp_server(port: int) -> bool:
    rx = make_receiver("tcp", port=port)
    rx.start()
    time.sleep(0.3)
    c = socket.create_connection(("127.0.0.1", port), timeout=2)
    c.sendall(frames(100))
    time.sleep(0.3)
    c.close()
    st = rx.stats()
    rx.stop()
    print(f"  TCP服务端  : 包 {st['packets']} 字节 {st['bytes']} 错误={st['error']}")
    return st["bytes"] >= 100 * FRAME


def test_tcp_client(port: int) -> bool:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    srv.settimeout(3)
    rx = make_receiver("tcp-client", host="127.0.0.1", port=port)
    rx.start()
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        srv.close()
        rx.stop()
        print("  TCP客户端  : 服务端未收到连接 -> FAIL")
        return False
    conn.sendall(frames(100))
    time.sleep(0.4)
    st = rx.stats()
    conn.close()
    srv.close()
    rx.stop()
    print(f"  TCP客户端  : 连接={st.get('connected')} 字节 {st['bytes']} 错误={st['error']}")
    return st["bytes"] >= 100 * FRAME


def test_serial() -> bool:
    rx = make_receiver("serial", com="COM99", baud=921600)
    try:
        rx.start()
        print("  串口       : 意外打开成功（本机真的有 COM99？）")
        rx.stop()
        return False
    except LinkError as e:
        print(f"  串口       : 正确报错 -> {e}")
        return True


def test_port_conflict(port: int) -> bool:
    """端口被占用时必须给明确原因，而不是静默失败。"""
    a = make_receiver("udp", port=port)
    a.start()
    b = make_receiver("udp", port=port)
    try:
        b.start()
        print("  端口冲突   : 竟然绑成功 -> FAIL")
        ok = False
    except LinkError as e:
        print(f"  端口冲突   : 正确报错 -> {str(e)[:60]}…")
        ok = True
    a.stop()
    return ok


def test_raw_dump(port: int) -> bool:
    from source import RealSource

    out = ROOT / "capture" / "selftest.bin"
    for p in (out, Path(str(out) + ".chunks.txt")):
        if p.exists():
            p.unlink()
    src = RealSource(port=port, proto="udp", raw_dump=str(out))
    src.start()
    time.sleep(0.2)
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk.sendto(frames(30), ("127.0.0.1", port))
    sk.close()
    time.sleep(0.4)
    n = 0
    t0 = time.time()
    while n < 30 and time.time() - t0 < 2:
        n += len(src.poll_frames())
        time.sleep(0.01)
    st = src.stats()
    src.stop()
    size = out.stat().st_size if out.exists() else 0
    print(f"  抓包落盘   : 解出 {n}/30 帧, crc坏 {st['crc_bad']}, 实测fps {st['fps']}, 落盘 {size} 字节")
    return n == 30 and size == 30 * FRAME


if __name__ == "__main__":
    base = 5100
    print("=" * 60)
    print("链路层四模式自检")
    print("=" * 60)
    r = {
        "UDP": test_udp(base),
        "TCP服务端": test_tcp_server(base + 1),
        "TCP客户端": test_tcp_client(base + 2),
        "串口报错": test_serial(),
        "端口冲突": test_port_conflict(base + 3),
        "抓包落盘": test_raw_dump(base + 4),
    }
    print("-" * 60)
    for k, v in r.items():
        print(f"  {k:<10} {'PASS' if v else 'FAIL'}")
    print("汇总:", "全部通过" if all(r.values()) else "有失败项")
