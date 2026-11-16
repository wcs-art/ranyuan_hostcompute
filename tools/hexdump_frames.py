"""原始字节分析：从抓包 .bin 反推帧长、验证 CRC，用来和固件对帧格式。

为什么需要它：上位机按"代码里的帧布局"切帧（v1=104 B / v1r5=152 B / compat90=90 B）。
板子一到，先抓原始字节，再用本工具看真实帧长是多少、CRC 算法对不对。
这样才能把"链路不通"和"帧格式不符"分开。

用法：
    .venv/Scripts/python.exe tools/hexdump_frames.py capture/x.bin
    .venv/Scripts/python.exe tools/hexdump_frames.py capture/x.bin --dump 3
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ev1"))

from device_frame import crc16_modbus, get_layout  # noqa: E402

FRAME = get_layout().frame_len   # 当前代码假设的帧长(随 protocol 配置变)

HEAD = b"\xAA\x55"


def find_heads(data: bytes) -> list[int]:
    return [i for i in range(len(data) - 1) if data[i:i + 2] == HEAD]


def crc_hits(data: bytes, heads: list[int], frame_len: int, sample: int = 300) -> int:
    """按给定帧长尝试 CRC16-MODBUS(帧头2 + 帧体 + CRC2)。返回命中个数。"""
    body_len = frame_len - 4
    if body_len <= 0:
        return 0
    hits = 0
    for o in heads[:sample]:
        end = o + frame_len
        if end > len(data):
            break
        body = data[o + 2:end - 2]
        rx = int.from_bytes(data[end - 2:end], "little")
        if crc16_modbus(body) == rx:
            hits += 1
    return hits


def guess_frame_len(data: bytes, heads: list[int]) -> tuple[int, int, dict]:
    """在 16..400 字节范围内穷举，取 CRC 命中数最高的帧长。"""
    n = min(len(heads), 300)
    candidates = []
    for L in range(16, 401):
        h = crc_hits(data, heads, L, n)
        if h:
            candidates.append((h, L))
    candidates.sort(reverse=True)
    best = candidates[0] if candidates else (0, 0)
    return best[1], best[0], {"tried": n, "candidates": candidates[:6]}


def dump_hex(data: bytes, offset: int, length: int) -> None:
    chunk = data[offset:offset + length]
    for row in range(0, len(chunk), 16):
        part = chunk[row:row + 16]
        hexs = " ".join(f"{b:02X}" for b in part)
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in part)
        print(f"  {offset+row:08X}  {hexs:<48}  |{ascii_}|")


def load_chunks(path: Path) -> list[tuple[int, int, float]]:
    side = Path(str(path) + ".chunks.txt")
    if not side.exists():
        return []
    out = []
    for line in side.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        p = line.split("\t")
        if len(p) >= 3:
            out.append((int(p[0]), int(p[1]), float(p[2])))
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    path = Path(argv[0])
    if not path.exists():
        print(f"文件不存在: {path}")
        return 2
    dump_n = 3
    if "--dump" in argv:
        dump_n = int(argv[argv.index("--dump") + 1])

    data = path.read_bytes()
    print("=" * 64)
    print(f"文件: {path}   共 {len(data)} 字节")
    print("=" * 64)

    if not data:
        print("空文件：一个字节都没收到，说明链路层没通（不是帧格式问题）。")
        return 1

    heads = find_heads(data)
    print(f"帧头 AA55 出现次数: {len(heads)}")
    if len(heads) >= 2:
        deltas = Counter(b - a for a, b in zip(heads, heads[1:]))
        print("相邻帧头间距 Top5:", deltas.most_common(5))

    if not heads:
        print("\n没有找到 AA55 帧头。可能原因：")
        print("  1) 固件帧头不是 AA55（改成 55AA / 0x5A5A 等）")
        print("  2) 串口/WiFi 传的是文本或经过转义")
        print("\n说明: 这部分字节仍可用于分析，下面给出前 64 字节原始内容。")
        dump_hex(data, 0, min(64, len(data)))
        return 1

    L, hits, info = guess_frame_len(data, heads)
    print(f"\n穷举帧长 16~400 字节，CRC16-MODBUS 命中最多的是: {L} 字节  "
          f"({hits}/{info['tried']} 个帧头)")
    if info["candidates"]:
        print("候选:")
        for h, l in info["candidates"]:
            flag = f"  <== 当前代码用的 {FRAME} 字节" if l == FRAME else ""
            print(f"  帧长 {l:3d} 命中 {h:3d}{flag}")
    else:
        print("  没有任何帧长能通过 CRC16-MODBUS 校验。")

    print("\n判定:")
    if L == FRAME and hits >= max(1, info["tried"] * 0.9):
        print(f"  ✔ 帧格式与上位机解析器一致({FRAME} 字节 + CRC16-MODBUS)。可以直接收数据。")
    elif hits >= max(1, info["tried"] * 0.5):
        print(f"  ✘ 帧格式不一致: 真实帧长约 {L} 字节, 上位机按 {FRAME} 字节解析。")
        print("    -> 先看这个长度对应哪套布局(改 config/run.json 的 protocol 即可, 不用改代码):")
        print("       v1 = 104 B(一帧 1 个 EEG 样点, 帧率=EEG 采样率)")
        print("       v1r5 = 152 B(一帧 5 个 EEG 样点, EEG 500 SPS / fNIRS 100 SPS)")
        print("       compat90 = 90 B(2026-11-12 自拟的旧布局)")
        print("       都不是这个数 -> 由我方按真实长度补一套 FrameLayout, 再对一次 CRC。")
    else:
        print("  ✘ CRC 校验通不过。可能是 CRC 算法不同(CCITT/XMODEM)、")
        print("    校验范围不同(不含帧头/含长度字段)、或字节序不同(big-endian)。")
        print("    -> 先向固件要组帧函数，或把下面这段十六进制发给他们比对。")

    chunks = load_chunks(path)
    if chunks:
        sizes = Counter(c[1] for c in chunks)
        print(f"\n分包情况: {len(chunks)} 个包, 包长 Top5: {sizes.most_common(5)}")
        if L and sizes:
            aligned = sum(1 for c in chunks if c[1] % L == 0)
            print(f"  包长能被 {L} 整除的包: {aligned}/{len(chunks)}"
                  f"  —— 整除说明模组按帧边界分包，否则上层必须靠切帧器兜底")

    print(f"\n前 {dump_n} 个帧头的十六进制内容:")
    for o in heads[:dump_n]:
        print(f"\n[偏移 {o}]")
        dump_hex(data, o, min(L if L else FRAME, 96))

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
