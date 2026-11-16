"""设备接入体检（临时工具，不属于产品代码）。

板子到手第一天，先跑这个，把"线到底通没通"查清楚，再去开界面。

做四件事：
  1. 枚举本机串口，识别是 CP2102N 还是 CH340（Type-C 那条线）
  2. 列出本机 IPv4 与默认网关，判断是否连在板子热点上（WiFi 那条线）
  3. 扫目标 IP 的 TCP/UDP 端口，找模组的透传端口
  4. 用 4 种波特率各试读 2 秒，看有没有字节、有没有 AA 55 帧头

用法：
    .venv/Scripts/python.exe tools/preflight.py                # 只查串口 + 网卡
    .venv/Scripts/python.exe tools/preflight.py --port COM3     # 查串口 + 试读数据
    .venv/Scripts/python.exe tools/preflight.py --ip 192.168.4.1  # 查端口
    .venv/Scripts/python.exe tools/preflight.py --all            # 全都查
    .venv/Scripts/python.exe tools/preflight.py --sniff --port COM3   # 试读后自动嗅探帧格式

说明：
  UDP 端口在局域网内"无回包"通常是端口开着（模组收下不回复），
  "连接被重置"才是端口关闭（对端回了 ICMP 不可达）。所以 UDP 只能给"疑似"。
"""
from __future__ import annotations

import argparse
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _known_layouts() -> dict[int, str]:
    """{帧长: 布局名}，直接从 device_frame 现取。

    工具里不写死任何帧长数字 —— 写死过一次 90 字节，协议改成 104/152 之后
    体检脚本会把真机判成"帧格式不符"，把排查方向带偏。
    """
    try:
        ev1 = str(ROOT / "ev1")
        if ev1 not in sys.path:
            sys.path.insert(0, ev1)
        import device_frame  # noqa: PLC0415 - 只在嗅探到帧头时才需要

        return {ly.frame_len: name for name, ly in device_frame.layouts().items()}
    except Exception:  # noqa: BLE001 - 独立运行时不强依赖
        return {}

# USB 转串口芯片的 VID:PID，用来判断插的到底是哪个口
USB_CHIPS = {
    (0x10C4, 0xEA60): "Silicon Labs CP210x (CP2102N/CP2104)",
    (0x10C4, 0xEA70): "Silicon Labs CP2105 (双串口)",
    (0x1A86, 0x7523): "WCH CH340",
    (0x1A86, 0x5523): "WCH CH341",
    (0x1A86, 0x55D4): "WCH CH9102",
    (0x0403, 0x6001): "FTDI FT232",
    (0x0483, 0x5740): "STM32 虚拟串口 (USB CDC)",
}

# 透传模组常见端口，按可能性排序
TCP_PORTS = [5000, 8080, 5005, 5006, 3333, 8888, 9000, 23, 502, 8000]
UDP_PORTS = [5000, 5005, 8080, 3333, 8888, 9000, 8000]

BAUDS = [921600, 460800, 115200, 57600]


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def title(text: str) -> None:
    print()
    print("=" * 66)
    print(text)
    print("=" * 66)


def ok(text: str) -> None:
    print(f"  [OK]   {text}")


def none(text: str) -> None:
    print(f"  [--]   {text}")


def warn(text: str) -> None:
    print(f"  [!!]   {text}")


def hint(text: str) -> None:
    print(f"         {text}")


# --------------------------------------------------------------------------
# 1. 串口
# --------------------------------------------------------------------------
def list_serial_ports() -> list[dict]:
    try:
        from serial.tools import list_ports
    except ImportError:
        warn("没装 pyserial，跳过串口检查（.venv/Scripts/python.exe -m pip install pyserial）")
        return []

    ports = []
    for p in sorted(list_ports.comports(), key=lambda x: x.device):
        vid, pid = p.vid, p.pid
        chip = USB_CHIPS.get((vid, pid)) if vid and pid else None
        if chip is None and vid:
            chip = f"未知芯片 {vid:04X}:{pid:04X}"
        ports.append({
            "device": p.device,
            "desc": p.description or "",
            "chip": chip or "无 USB 信息（板载或虚拟口）",
            "hwid": p.hwid or "",
            "vid": vid,
            "pid": pid,
        })
    return ports


def check_serial(ports: list[dict]) -> None:
    title("1. 串口（Type-C / USB 转串口）")
    if not ports:
        none("一个串口都没有")
        hint("插上 Type-C 后重跑；没反应就去设备管理器看有没有未知设备（缺驱动）")
        hint("CP2102N 驱动：Silicon Labs CP210x VCP；CH340 驱动：WCH CH341SER")
        return

    ok(f"共 {len(ports)} 个串口")
    for p in ports:
        print(f"         {p['device']:<8} {p['chip']}")
        if p["desc"] and p["desc"] != "n/a":
            print(f"                  {p['desc']}")
        if p["hwid"]:
            print(f"                  {p['hwid']}")

    cp = [p for p in ports if p["vid"] == 0x10C4]
    ch = [p for p in ports if p["vid"] == 0x1A86]
    if len(cp) >= 1 and len(ch) >= 1:
        warn("CP2102N 和 CH340 都在，很可能一个是数据口、一个是日志/下载口")
        hint(f"建议先试 {cp[0]['device']}（CP2102N），再试 {ch[0]['device']}（CH340）")
    elif len(ports) == 1:
        hint("只有一个口，数据大概率走这里；用 --port 指定它试读")


# --------------------------------------------------------------------------
# 2. 网卡
# --------------------------------------------------------------------------
def _run_ipconfig(retries: int = 3) -> str:
    """读 ipconfig 输出。偶发会返回空（系统瞬时繁忙），所以带重试。

    只有解出来的内容里确实有网络关键字才算成功，避免把空串当结果。
    """
    for attempt in range(retries):
        raw = b""
        try:
            p = subprocess.run(
                ["ipconfig"], capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            raw = p.stdout or b""
        except Exception:
            raw = b""
        for enc in ("gbk", "utf-8", "latin-1"):
            try:
                text = raw.decode(enc)
            except UnicodeDecodeError:
                continue
            if "IPv4" in text or "适配器" in text or "adapter" in text.lower():
                return text
        if attempt < retries - 1:
            time.sleep(0.2)
    return ""


def parse_ipconfig(text: str) -> list[dict]:
    """按适配器分段，抽 IPv4 和默认网关。

    中英文 Windows 都认。注意网关行可能是 IPv6 在前、IPv4 在下一行续写，
    所以不能只匹配同一行，要往下看几行续行。
    """
    blocks = []
    cur = None
    cur_lines: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if line[:1] not in (" ", "\t") and s.endswith(":") and ("适配器" in s or "dapter" in s):
            if cur is not None:
                blocks.append((cur, cur_lines))
            cur = s.rstrip(":")
            cur_lines = []
            continue
        if cur is not None and s:
            cur_lines.append(s)
    if cur is not None:
        blocks.append((cur, cur_lines))

    out = []
    for name, lines in blocks:
        ipv4 = None
        for s in lines:
            m = re.search(r"IPv4[^:]*:\s*(\d+\.\d+\.\d+\.\d+)", s)
            if m:
                ipv4 = m.group(1)
                break
        if not ipv4:
            continue

        gw = None
        for i, s in enumerate(lines):
            if "默认网关" not in s and "Default Gateway" not in s:
                continue
            # 网关值可能在本行，也可能续到下面 1~2 行
            joined = " ".join(lines[i:i + 3])
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", joined)
            if m:
                gw = m.group(1)
            break

        out.append({"name": name, "ipv4": ipv4, "gw": gw})
    return out


VIRTUAL_HINTS = ("vethernet", "wsl", "loopback", "虚拟机", "vmware", "virtualbox", "bluetooth", "蓝牙")


def _is_virtual(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in VIRTUAL_HINTS)


def check_network(blocks: list[dict]) -> str | None:
    title("2. 网卡（WiFi 那条线）")
    if not blocks:
        none("没读到任何 IPv4 地址")
        hint("① 确认板子上电、WiFi 已连上")
        hint("② 若确认已联网，可能是读取失败，手动跑一次 ipconfig 对照")
        return None

    real = [b for b in blocks if not _is_virtual(b["name"])]
    shown = real or blocks
    if real and len(real) < len(blocks):
        print(f"         （已隐藏 {len(blocks) - len(real)} 块虚拟网卡）")

    for b in shown:
        ok(f"{b['name']}")
        print(f"         本机 IP   {b['ipv4']}")
        print(f"         默认网关  {b['gw'] or '（无）'}")

    # 判断是否连在板子热点上：网关落在 ESP 模组常见网段
    for b in shown:
        gw = b["gw"]
        if gw and (gw.startswith("192.168.4.") or gw.startswith("192.168.0.")):
            warn(f"疑似连在板子热点上，板子 IP 大概率是 {gw}")
            hint(f"下一步：--ip {gw} 扫端口")
            return gw
        if gw and gw.startswith("192.168.1."):
            # 普通家用路由，板子可能也在这一网段（STA 模式）
            hint(f"网关 {gw} 像普通路由。若板子是 STA 模式，板子也在这个网段")

    hint("没看到 192.168.4.x 这种热点网段")
    hint("① AP 模式：先去系统 WiFi 列表连板子的热点（名字类似 ESP-xxxx）")
    hint("② STA 模式：进路由后台看设备列表，找 MAC 前缀 Espressif 的设备")
    return None


# --------------------------------------------------------------------------
# 3. 端口
# --------------------------------------------------------------------------
def scan_tcp(ip: str, ports: list[int], timeout: float = 0.3) -> list[int]:
    opened = []
    for port in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            if s.connect_ex((ip, port)) == 0:
                opened.append(port)
        finally:
            s.close()
    return opened


def scan_udp(ip: str, ports: list[int], timeout: float = 0.35) -> list[tuple[int, str]]:
    """返回 [(port, 判定)]，判定为 'open' / 'closed' / 'unknown'。"""
    out = []
    for port in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        verdict = "unknown"
        try:
            s.sendto(b"\x00\x00\x00\x00", (ip, port))
            s.recvfrom(2048)
            verdict = "open"          # 有回包，肯定开着
        except socket.timeout:
            verdict = "open"          # 无回包：模组收下不回复，最常见
        except ConnectionResetError:
            verdict = "closed"        # 收到 ICMP 端口不可达
        except OSError as e:
            verdict = f"err:{e.winerror if hasattr(e, 'winerror') else e}"
        finally:
            s.close()
        out.append((port, verdict))
    return out


def check_ports(ip: str) -> None:
    title(f"3. 端口扫描  {ip}")
    t0 = time.time()
    tcp_open = scan_tcp(ip, TCP_PORTS)
    if tcp_open:
        ok(f"TCP 开着：{', '.join(str(p) for p in tcp_open)}")
        hint(f"先用：--port 命令行里 proto=tcp-client host={ip} port={tcp_open[0]}")
    else:
        none(f"TCP 全关（试了 {len(TCP_PORTS)} 个）")

    udp_res = scan_udp(ip, UDP_PORTS)
    udp_suspect = [p for p, v in udp_res if v == "open"]
    udp_closed = [p for p, v in udp_res if v == "closed"]
    if udp_suspect:
        ok(f"UDP 疑似开着：{', '.join(str(p) for p in udp_suspect)}（无回包，可能是模组）")
    if udp_closed:
        none(f"UDP 明确关闭：{', '.join(str(p) for p in udp_closed)}")
    if not udp_suspect and not udp_closed:
        warn("UDP 全是 unknown，可能被本机防火墙拦了 ICMP")

    print(f"         （耗时 {time.time() - t0:.1f}s）")
    if not tcp_open and not udp_suspect:
        hint("一个端口都没通。确认 IP 对不对（先 ping 一下）、板子是否真的在发包")


# --------------------------------------------------------------------------
# 4. 串口试读
# --------------------------------------------------------------------------
def try_read(com: str, baud: int, seconds: float = 2.0) -> dict:
    import serial

    r = {"baud": baud, "bytes": 0, "aa55": 0, "head": b"", "text": False, "error": None}
    try:
        ser = serial.Serial(com, baud, timeout=0.05)
    except Exception as e:
        r["error"] = str(e)
        return r

    t0 = time.time()
    buf = bytearray()
    try:
        while time.time() - t0 < seconds:
            chunk = ser.read(8192)
            if chunk:
                buf += chunk
    except Exception as e:
        r["error"] = str(e)
    finally:
        try:
            ser.close()
        except Exception:
            pass

    r["bytes"] = len(buf)
    r["head"] = bytes(buf[:48])
    if buf:
        r["aa55"] = bytes(buf).count(b"\xaa\x55")
        # 看是不是纯文本日志：可打印字符占比高
        printable = sum(1 for b in buf if 9 <= b <= 13 or 32 <= b <= 126)
        r["text"] = printable / len(buf) > 0.85
    return r


def check_stream(com: str, bauds: list[int], known: list[str] | None = None) -> dict | None:
    title(f"4. 串口试读  {com}")

    # 先确认这个口真的存在，否则 Windows 报的是"拒绝访问"，容易误判成被占用
    if known is not None and com.upper() not in {k.upper() for k in known}:
        warn(f"系统里没有 {com}")
        hint(f"现有串口：{', '.join(known) if known else '一个都没有'}")
        hint("插好 Type-C 后重跑，或用 --port 指定列表里真实存在的名字")
        return None

    best = None
    for baud in bauds:
        r = try_read(com, baud)
        if r["error"]:
            warn(f"{baud:>7}  打不开：{r['error']}")
            hint("口被别的程序占着（界面/串口助手），先关掉再试")
            return None
        if r["bytes"] == 0:
            none(f"{baud:>7}  0 字节")
            continue

        ok(f"{baud:>7}  收到 {r['bytes']:,} 字节（{r['bytes'] / 2 / 1024:.1f} KB/s）"
           f"  AA55 命中 {r['aa55']}")
        print(f"         头部  {r['head'][:32].hex(' ')}")
        if r["text"]:
            warn("看起来是纯文本，这个口可能是调试日志而不是数据")
            try:
                print(f"         文本  {r['head'][:48].decode('ascii', 'replace')!r}")
            except Exception:
                pass
        score = r["aa55"] * 1000 + r["bytes"]
        if best is None or score > best[1]["aa55"] * 1000 + best[1]["bytes"]:
            best = (baud, r)

    if best is None:
        warn("四个波特率都是 0 字节")
        hint("三种可能：① 这个口只供电不传数据 ② 波特率不在列表里 ③ 板子没在发")
        hint("换个 COM 口再试；两根线（CP2102N / CH340）都试一遍")
        return None

    baud, r = best
    print()
    ok(f"最可能：{com} @ {baud}")
    if r["aa55"] > 1:
        ok(f"找到 AA 55 帧头 {r['aa55']} 次，和我预想的帧格式一致")
        hint("可以直接开界面：--source=real --proto=serial --com=%s --baud=%d" % (com, baud))
    elif r["aa55"] == 1:
        warn("AA55 只出现 1 次，可能是巧合（比如数据里恰好有这个值）")
        hint("跑 --sniff 看统计，或者抓一段存下来再看")
    else:
        warn("没找到 AA 55，帧格式可能和预想的不同")
        hint("跑 --sniff 自动嗅探，或者用 hexdump_frames.py 看原始字节")
    return {"com": com, "baud": baud, "bytes": r["bytes"], "aa55": r["aa55"], "text": r["text"]}


# --------------------------------------------------------------------------
# 5. 帧格式嗅探
# --------------------------------------------------------------------------
def read_serial_burst(com: str, baud: int, seconds: float) -> bytes | None:
    import serial

    try:
        ser = serial.Serial(com, baud, timeout=0.05)
    except Exception as e:
        warn(f"打不开串口：{e}")
        hint("口可能被界面或串口助手占着，先关掉")
        return None

    buf = bytearray()
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            chunk = ser.read(8192)
            if chunk:
                buf += chunk
    finally:
        try:
            ser.close()
        except Exception:
            pass
    return bytes(buf)


def analyze_stream(data: bytes, seconds: float | None = None) -> dict:
    """不知道固件格式时，靠统计找候选帧头。

    原理：固定帧头的 2 字节组合出现得极有规律——相邻两次出现的间隔恒等于帧长，
    而数据区里的字节组合间隔杂乱。按"间隔一致率"排序就能把帧头筛出来。
    这个方法不需要知道 CRC、不需要知道字段布局，是先验假设最少的做法。
    """
    from collections import defaultdict

    n = len(data)
    res = {"n": n, "rate": (n / seconds) if seconds else None, "candidates": [], "top": None}
    if n < 64:
        return res

    cand = defaultdict(list)
    for i in range(n - 1):
        lst = cand[data[i:i + 2]]
        lst.append(i)
        if len(lst) > 400:          # 样本够了就滚动，控制内存
            lst.pop(0)

    scored = []
    for key, pos in cand.items():
        if len(pos) < 12:
            continue
        gaps = [b - a for a, b in zip(pos, pos[1:])]
        common, cnt = _mode(gaps)
        if cnt < 5 or common < 8:
            continue
        ratio = cnt / len(gaps)
        if ratio < 0.55:
            continue
        # 真帧头每帧只出现一次；帧内的 00 00 长串也会间隔规律，但每帧出现多次。
        # 用"每帧出现次数"把这两类分开，这是分辨帧头和数据区重复值的关键。
        frames_est = n / common
        per_frame = len(pos) / frames_est if frames_est > 0 else 99
        scored.append({
            "key": key, "count": len(pos), "period": common,
            "ratio": ratio, "first": pos[0], "per_frame": per_frame,
        })

    # 先按"每帧一次"筛，再按间隔一致率排
    scored.sort(key=lambda x: (abs(x["per_frame"] - 1) > 0.15, -x["ratio"], -x["count"]))

    # 帧头会互相重叠：AA 55 和紧邻的 55 07 间隔同样是帧长。相邻 4 字节内只留一个。
    uniq = []
    for s in scored:
        if any(abs(s["first"] - u["first"]) < 4 and s["period"] == u["period"] for u in uniq):
            continue
        uniq.append(s)

    res["candidates"] = uniq
    res["top"] = uniq[0] if uniq else None
    return res


def find_seq_offset(data: bytes, start: int, period: int, max_frames: int = 80) -> list[dict]:
    """在帧内找"每帧固定步长递增"的整数字段。

    seq 每帧 +1，时间戳每帧 +10（10ms 一帧），这两个特征最难伪造，
    找到了基本就能确定字段偏移，直接拿去改解析代码。
    """
    n = len(data)
    frames = min(max_frames, max(0, (n - start) // period))
    if frames < 20:
        return []

    hits = []
    for width in (4, 2, 1):
        for off in range(0, period - width):
            vals = []
            for k in range(frames):
                p0 = start + k * period + off
                if p0 + width > n:
                    break
                vals.append(int.from_bytes(data[p0:p0 + width], "little"))
            if len(vals) < 20:
                continue
            diffs = [b - a for a, b in zip(vals, vals[1:])]
            if not diffs:
                continue
            step, cnt = _mode(diffs)
            ratio = cnt / len(diffs)
            # 步长必须是小的正数；-1 之类是回绕或反向，不算
            if ratio < 0.9 or not (0 < step < 100000):
                continue
            # 去掉"错位读"产生的假象：把 4 字节字段从中间开始读，步长会是 256/65536 的倍数
            if step >= 256 and step % 256 == 0:
                continue
            if step > 5000:
                continue
            hits.append({"offset": off, "width": width, "step": step, "ratio": ratio})
    return hits


def report_seq(hits: list[dict]) -> None:
    if not hits:
        warn("没找到每帧固定递增的字段")
        hint("可能样本太短、或 seq/时间戳不是整数字段")
        return

    # 同一偏移只留最宽的那个（4 字节比 1 字节更可能是真字段）
    seen = {}
    for h in hits:
        key = h["offset"]
        if key not in seen or h["width"] > seen[key]["width"]:
            seen[key] = h
    # seq / 时间戳基本都是 4 字节，先看宽的，再看步长
    uniq = sorted(seen.values(), key=lambda x: (-x["width"], x["step"] != 1, x["offset"]))

    ok(f"找到 {len(uniq)} 个每帧递增的字段，按可信度列出：")
    for h in uniq[:6]:
        kind = ""
        if h["step"] == 1:
            kind = "  ← 像是 seq"
        elif 5 <= h["step"] <= 20:
            kind = f"  ← 像是时间戳（{h['step']}ms/帧 ≈ {1000 / h['step']:.0f} 帧/秒）"
        print(f"         偏移 {h['offset']:>3}  {h['width']} 字节  步长 {h['step']:<6} "
              f"连续率 {h['ratio'] * 100:.0f}%{kind}")
    if len(uniq) > 6:
        hint(f"另有 {len(uniq) - 6} 个，多为 1~2 字节的巧合（数据本身在变）")


def report_stream(res: dict, data: bytes, source: str) -> dict | None:
    n = res["n"]
    title(f"5. 帧格式嗅探  {source}")
    if n < 200:
        warn(f"只收到 {n} 字节，样本太少，多采一会儿")
        return None

    rate = res["rate"]
    if rate:
        ok(f"样本 {n:,} 字节，{rate / 1024:.1f} KB/s")
    else:
        ok(f"样本 {n:,} 字节（文件回放，无时间信息）")

    scored = res["candidates"]
    if not scored:
        warn("没找到稳定的周期性字节对")
        hint("可能没有固定帧头（裸数据流），也可能这段里全是同一个值（板子没在发）")
        uniq = len(set(data))
        print(f"         不同字节值 {uniq} 个，最多的是 0x{_mode(list(data))[0]:02X}（占 "
              f"{_mode(list(data))[1] * 100 / n:.0f}%）")
        if uniq <= 4:
            warn("数据几乎不变，基本可以判定这段没在传有效数据")
        return None

    ok(f"找到 {len(scored)} 个候选帧头，按间隔一致性排序：")
    print()
    print(f"         {'帧头':<10} {'出现':>6} {'帧长(字节)':>11} {'一致率':>7} {'每帧次数':>8}")
    print("         " + "-" * 52)
    for s in scored[:8]:
        print(f"         {s['key'].hex(' ').upper():<10} {s['count']:>6} "
              f"{s['period']:>11} {s['ratio'] * 100:>6.0f}% {s['per_frame']:>8.1f}")
    print("         （每帧次数≈1 才是真帧头；明显大于 1 是数据区里的重复值）")

    top = scored[0]
    p = top["period"]
    known = _known_layouts()
    print()
    if top["key"] == b"\xaa\x55":
        ok(f"帧头就是 AA 55，帧长 {p} 字节")
        if p in known:
            ok(f"和代码里的帧布局对得上：{known[p]}")
        else:
            warn(f"代码里没有 {p} 字节的布局。已知：" +
                 "、".join(f"{v}={k}B" for k, v in sorted(known.items())))
            hint("若确认是这个帧长，由我方补一套 FrameLayout，再把 protocol 指过去")
    else:
        warn(f"帧头疑似 {top['key'].hex(' ').upper():s}，帧长 {p} 字节")
        warn("和代码里的 AA 55 不一致，需要确认固件用的是哪个同步字")

    if rate:
        print(f"         按此帧长推算帧率：{rate / p:.1f} 帧/秒")
        print(f"         若目标是 100 帧/秒，需要 {p * 100 / 1024:.1f} KB/s 带宽")

    print()
    report_seq(find_seq_offset(data, top["first"], p))

    start = top["first"]
    print()
    ok("第 1~3 帧原始字节（帧头处对齐）：")
    for k in range(3):
        seg = data[start + k * p: start + k * p + min(p, 96)]
        if not seg:
            break
        tag = "  ← 只有部分（帧太长）" if len(seg) < p else ""
        print(f"         {seg.hex(' ')}{tag}")

    print()
    hint("核对要点：帧头后第 3 字节通常是设备号/类型，seq 应从 0 或 1 连续递增")
    hint("若 seq 位置固定且每次 +1，把偏移记下来，改 device_frame.py 的解析")
    return top


def _mode(gaps: list[int]) -> tuple[int, int]:
    from collections import Counter
    if not gaps:
        return 0, 0
    c = Counter(gaps)
    v, n = c.most_common(1)[0]
    return v, n


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="设备接入体检：串口 / 网卡 / 端口 / 串口试读 / 帧格式嗅探",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--port", help="串口名，如 COM3")
    ap.add_argument("--ip", help="板子 IP，如 192.168.4.1")
    ap.add_argument("--all", action="store_true", help="串口 + 网卡 + 端口 全查")
    ap.add_argument("--sniff", action="store_true", help="试读后自动嗅探帧格式（需 --port）")
    ap.add_argument("--file", help="对已录制的 bin 文件嗅探帧格式（不碰串口，可反复跑）")
    ap.add_argument("--baud", type=int, help="指定波特率（配合 --sniff，跳过四档试读）")
    ap.add_argument("--seconds", type=float, default=5.0, help="嗅探采样秒数，默认 5")
    args = ap.parse_args(argv)

    print("EV1 上位机 · 设备接入体检")
    print(f"时间 {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # 只分析文件时跳过在线检查，直接干活
    if args.file:
        path = Path(args.file)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            warn(f"文件不存在：{path}")
            return 2
        data = path.read_bytes()
        print(f"回放文件 {path}（{len(data):,} 字节）")
        report_stream(analyze_stream(data), data, path.name)
        return 0

    ports = list_serial_ports()
    check_serial(ports)

    ip_hint = None
    if args.all or args.ip or not args.port:
        blocks = parse_ipconfig(_run_ipconfig())
        ip_hint = check_network(blocks)

    target_ip = args.ip or ip_hint
    if args.all and target_ip:
        check_ports(target_ip)
    elif args.ip:
        check_ports(args.ip)

    com = args.port
    if com is None and len(ports) == 1:
        com = ports[0]["device"]
        hint(f"只检测到一个串口，自动选 {com}")

    picked = None
    if com:
        bauds = [args.baud] if args.baud else BAUDS
        picked = check_stream(com, bauds, [p["device"] for p in ports])

    if args.sniff:
        baud = args.baud or (picked["baud"] if picked else BAUDS[0])
        target = com or (picked["com"] if picked else None)
        if not target:
            warn("--sniff 需要 --port 指定串口")
        else:
            data = read_serial_burst(target, baud, args.seconds)
            if data:
                report_stream(analyze_stream(data, args.seconds), data,
                              f"{target} @ {baud}  {args.seconds:.0f}s")
                out = ROOT / "capture" / f"preflight_{time.strftime('%H%M%S')}.bin"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
                print()
                ok(f"原始字节已存 {out}")
                hint(f"之后可离线反复分析：--file {out.relative_to(ROOT)}")

    # 收尾：给出下一步
    title("下一步")
    lines = []
    if ports:
        lines.append(f"串口：{', '.join(p['device'] for p in ports)}")
    if target_ip:
        lines.append(f"板子 IP：{target_ip}")
    if picked:
        lines.append(f"数据口：{picked['com']} @ {picked['baud']}")
        if picked["aa55"] > 1:
            lines.append(f"开界面：run.bat --source=real --proto=serial --com={picked['com']} "
                         f"--baud={picked['baud']}")
    if not lines:
        warn("还没定位到可用链路，见上面每一节的 [--] 和 [!!]")
    for ln in lines:
        print(f"  {ln}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
