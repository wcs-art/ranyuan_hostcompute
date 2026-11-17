"""文本行数据源：有线固件以 ASCII 十进制行输出时使用（FireWater 风格）。

实测格式（2026-11-17 现场抓取，COM3 @921600，VOFA+ FireWater 引擎）：

    原始字节 2d 31 32 34 34 31 2e 38 30 0d 0a
    即文本   "-12441.80\\r\\n"

每行一个或多个十进制浮点值，CRLF 结尾，实测约 500 行/秒。

与 AA55 二进制帧的能力差异（不是适配缺陷，是格式本身不带这些字段）：

    帧头 / 包序号 / CRC16 / 绝对时间戳 / 事件标记 / 设备编号 —— 全部没有。
    因此本数据源：seq 由上位机按行自增（非固件序号，丢包统计对此无意义）、
    不产生 CRC 校验、ts_abs_ms 恒为 0（界面按本机收包时刻打戳）、事件恒为 0。

    结论：合同 F2 的验收判据无法由此格式判定，该结论须写入结论台账。
"""
from __future__ import annotations

import time
from pathlib import Path

# 通道口径与界面/协议保持同一组常量（不在本文件另写一份数字）
N_EEG = 4
N_FNIRS = 10
N_WL = 2
N_FNIRS_RAW = N_FNIRS * N_WL          # 20 路光强码值

#: 单行列数上限。超过即判坏行，防止垃圾字节把日志和内存打满。
MAX_COLS = 256
#: 未成行缓冲上限（字节）。超了丢最旧，并计入坏行，不允许无限增长。
MAX_PENDING = 1 << 20


def parse_line(line: bytes) -> list[float] | None:
    """一行原始字节 -> 数值列表；解析不出返回 None。

    支持的写法：
        -12441.80               纯数值（实测格式，单列）
        1.0,2.0,3.0             逗号分隔多列
        ch0:-1.0,ch1:2.0        FireWater 命名格式（取冒号后的数值）
    """
    if not line:
        return None
    try:
        text = line.decode("ascii")
    except UnicodeDecodeError:
        return None
    text = text.replace("\x00", "").strip()
    if not text:
        return None
    parts = text.split(",")
    if len(parts) > MAX_COLS:
        return None
    out: list[float] = []
    for p in parts:
        p = p.strip()
        if not p:
            return None
        if ":" in p:                        # FireWater: name:value
            p = p.split(":", 1)[1].strip()
        try:
            v = float(p)
        except ValueError:
            return None
        # nan/inf 不是有效采样：放进去会把整条波形和后续统计毁掉
        if v != v or v in (float("inf"), float("-inf")):
            return None
        out.append(v)
    return out or None


class TextDecoder:
    """字节流 -> 帧 dict。与链路无关，可脱离串口直接单测。"""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.seq = 0
        self.n_lines = 0          # 成功解析成帧的行数
        self.n_bad = 0            # 解析失败的行数（非空）
        self.n_dropped = 0        # 缓冲超限被丢掉的字节
        self.n_short = 0          # 列数少于 EEG 通道数的行
        self.n_extra = 0          # 多出映射之外的列（已丢弃）
        self.col_hist: dict[int, int] = {}
        self._t_first: float | None = None
        self._t_last: float | None = None

    # ---- 统计 ----
    def dominant_cols(self) -> int:
        """出现次数最多的列数；没有数据时为 0。"""
        if not self.col_hist:
            return 0
        return max(self.col_hist.items(), key=lambda kv: kv[1])[0]

    def measured_fps(self) -> float:
        """实测行率（帧/秒）。帧格式未冻结前以它为准，不用代码里的假设值。"""
        if self._t_first is None or self._t_last is None or self.n_lines < 2:
            return 0.0
        span = self._t_last - self._t_first
        return (self.n_lines - 1) / span if span > 0 else 0.0

    def stats(self) -> dict:
        return {
            "frames": self.n_lines,
            "bad_lines": self.n_bad,
            "dropped_bytes": self.n_dropped,
            "short_lines": self.n_short,
            "extra_cols_dropped": self.n_extra,
            "cols": self.dominant_cols(),
            "col_hist": dict(self.col_hist),
            "fps": round(self.measured_fps(), 1),
            "buffered_bytes": len(self._buf),
        }

    # ---- 主流程 ----
    def feed(self, chunk: bytes) -> list[dict]:
        """喂入任意长度字节，返回本次解析出的帧（按到达顺序）。"""
        frames: list[dict] = []
        if not chunk:
            return frames
        self._buf += chunk
        if len(self._buf) > MAX_PENDING:
            drop = len(self._buf) - MAX_PENDING
            del self._buf[:drop]
            self.n_dropped += drop
        while True:
            i = self._buf.find(b"\n")
            if i < 0:
                break
            line = bytes(self._buf[:i])
            del self._buf[:i + 1]
            vals = parse_line(line)
            if vals is None:
                if line.strip():
                    self.n_bad += 1
                continue
            frames.append(self._to_frame(vals))
        return frames

    def _to_frame(self, vals: list[float]) -> dict:
        n = len(vals)
        self.col_hist[n] = self.col_hist.get(n, 0) + 1

        eeg = [0.0] * N_EEG
        raw = None
        if n <= N_EEG:
            eeg[:n] = vals
            if n < N_EEG:
                self.n_short += 1
        else:
            eeg = list(vals[:N_EEG])
            # 够 4 + 20 才当完整的 fNIRS 光强用；不够就不猜，多出的列计数丢弃
            if n >= N_EEG + N_FNIRS_RAW:
                raw = list(vals[N_EEG:N_EEG + N_FNIRS_RAW])
                self.n_extra += n - (N_EEG + N_FNIRS_RAW)
            else:
                self.n_extra += n - N_EEG

        self.seq += 1
        self.n_lines += 1
        now = time.time()
        if self._t_first is None:
            self._t_first = now
        self._t_last = now
        return {
            "device_id": 1,
            "seq": self.seq,
            "ts_abs_ms": 0,
            "eeg_uv": eeg,
            "hbo": [0.0] * N_FNIRS,
            "hbr": [0.0] * N_FNIRS,
            "raw": raw,
            "battery": -1.0,
            "acc_xyz": None,
            "gyro_xyz": None,
            "event_type": 0,
            "event_ts": 0,
        }


class TextSource:
    """串口文本行链路。kind 与 Source 基类一致，界面按 kind 做提示。"""

    kind = "text"

    def __init__(self, com: str = "COM3", baud: int = 921600,
                 raw_dump: str | Path | None = None, retry_s: float = 2.0):
        from receivers import SerialReceiver   # 延迟导入: 无 pyserial 时不影响其它链路
        from source import RawDumper

        self.com = com
        self.baud = int(baud)
        self._rx = SerialReceiver(com=com, baud=self.baud, retry_s=retry_s)
        self._dec = TextDecoder()
        self._dump = RawDumper(raw_dump) if raw_dump else None
        #: 存证路径单独留下: stop() 会关掉 dump 对象, 但"证据写在哪"必须一直能报出来
        self._dump_path = str(raw_dump) if raw_dump else None

    # ---- Source 接口 ----
    def start(self) -> None:
        self._rx.start()

    def stop(self) -> None:
        self._rx.stop()
        if self._dump:
            self._dump.close()
            self._dump = None

    def poll_frames(self) -> list[dict]:
        frames: list[dict] = []
        while True:
            chunk = self._rx.pop()
            if chunk is None:
                break
            if self._dump:
                self._dump.write(chunk)
            frames.extend(self._dec.feed(chunk))
        return frames

    def measured_fps(self) -> float:
        return self._dec.measured_fps()

    def stats(self) -> dict:
        d = self._rx.stats()
        d.update(self._dec.stats())
        d["text"] = True
        d["dump"] = self._dump_path
        return d


class TextFileSource:
    """从文本文件回放（离线回归用，不碰串口）。

    把整个文件一次性喂进解码器，用来验证解析层对真实抓包的处理结果，
    也让回归测试不依赖硬件。kind 标成 text-file，界面同样加"原始值"提示。
    """

    kind = "text-file"

    def __init__(self, path: str | Path, chunk_sizes: tuple[int, ...] = (1471, 37, 2048, 5, 900)):
        self.path = Path(path)
        if not self.path.exists():
            raise SystemExit(f"文本回放文件不存在: {self.path}")
        self._dec = TextDecoder()
        self._chunk_sizes = list(chunk_sizes) or [1024]
        self._data = self.path.read_bytes()
        self._pos = 0
        self._k = 0

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def exhausted(self) -> bool:
        return self._pos >= len(self._data)

    def poll_frames(self) -> list[dict]:
        frames: list[dict] = []
        # 每次调用只喂一块，保证界面能按节拍逐段消费（与真实链路同节奏）
        while self._pos < len(self._data):
            n = self._chunk_sizes[self._k % len(self._chunk_sizes)]
            self._k += 1
            chunk = self._data[self._pos:self._pos + n]
            self._pos += len(chunk)
            frames.extend(self._dec.feed(chunk))
            if frames:
                break
        return frames

    def measured_fps(self) -> float:
        return self._dec.measured_fps()

    def stats(self) -> dict:
        d = self._dec.stats()
        d.update({"link": "text-file", "file": str(self.path),
                  "bytes": len(self._data), "read": self._pos,
                  "done": self.exhausted(), "text": True})
        return d
