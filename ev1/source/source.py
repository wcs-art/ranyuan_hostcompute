"""数据源三态：sim / real / dry，统一 poll_frames() 接口。"""
from __future__ import annotations

import math
import random
import time
from pathlib import Path
from typing import Optional, Union

from device_frame import pack_frame, parse_stream
import device_frame
import contract


# --------------------------------------------------------------------------
# 基类
# --------------------------------------------------------------------------
class Source:
    """三种来源的共同接口。界面只认这三个方法。"""

    kind = "base"

    def start(self) -> None:  # noqa: D401
        pass

    def stop(self) -> None:
        pass

    def poll_frames(self) -> list[dict]:
        raise NotImplementedError


# --------------------------------------------------------------------------
# sim: 合成字节流, 但走真实解析
# --------------------------------------------------------------------------
class SimSource(Source):
    """合成数据。

    速率口径(未冻结, 见未决事项 #2): EEG 500 Hz 逐帧出新值, fNIRS 100 Hz,
    即每 5 帧 fNIRS 才更新一次, 其余帧沿用上一值。
    协议目前每帧固定 4 路 EEG + 10x2 fNIRS, 没有"本帧哪些通道有效"的字段,
    所以双速率在实际协议下无法表达 —— 这是待固件组冻结的项, 不是本文件的实现选择。
    """

    kind = "sim"

    def __init__(self, eeg_fs: float = 500.0, fnirs_fs: float = 100.0, seed: int = 0):
        self.eeg_fs = float(eeg_fs)
        self.fnirs_fs = float(fnirs_fs)
        self._ratio = max(1, int(round(self.eeg_fs / self.fnirs_fs)))
        self._rng = random.Random(seed)
        self._seq = 0
        self._n = 0
        self._raw = bytearray()
        self._t0 = time.monotonic() if hasattr(time, "monotonic") else time.time()
        self._sent = 0

    def _time_now(self) -> float:
        return (time.monotonic() if hasattr(time, "monotonic") else time.time()) - self._t0

    def _make_one(self) -> dict:
        self._n += 1
        ph = self._n / max(1.0, self.eeg_fs)
        # 增益折算系数与真机同源: 改 gain 时仿真值跟着变, 不会"仿真绿真机红"
        gain = device_frame.EEG_GAIN_UV_PER_LSB

        eeg_codes = [
            int((20.0 * math.sin(2 * math.pi * 10.0 * ph) + self._rng.gauss(0, 3)) / gain)
            for _ in range(contract.N_EEG)
        ]
        # fNIRS 每 ratio 帧更新一次
        if self._n % self._ratio == 1 or not hasattr(self, "_fnirs_cache"):
            self._fnirs_cache = [
                [int(1000 + self._rng.gauss(0, 10) + 100 * math.sin(2 * math.pi * 0.1 * ph))
                 for _ in contract.FNIRS_WLS]
                for _ in range(contract.N_FNIRS)
            ]
        body = pack_frame(
            dev=1, seq=self._seq, ts=int(self._n * 1000 / self.eeg_fs),
            eeg=eeg_codes, fnirs=self._fnirs_cache,
            # 仿真也走绝对时间戳字段, 让 CSV 的 time_abs_ms 列在自检时就有值
            ts_abs=int(time.time() * 1000),
        )
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        self._raw += body
        out = parse_stream(self._raw)
        return out[0] if out else None

    def poll_frames(self) -> list[dict]:
        # 按真实时间推进, 每次调用补齐"到现在应该有多少帧"
        target = int(self._time_now() * self.eeg_fs)
        frames: list[dict] = []
        guard = 0
        while self._n < target and guard < int(self.eeg_fs):
            df = self._make_one()
            guard += 1
            if df is None:
                continue
            frames.append(contract.frame_to_ui_dict(contract.from_protocol(df)))
        return frames


# --------------------------------------------------------------------------
# real: 真实接收器 + 累积缓冲 + 切帧
# --------------------------------------------------------------------------
class RawDumper:
    """把收到的原始字节原样落盘, 用于"帧格式还没冻结时"先抓证据。

    产物两个:
      <path>.bin         连续字节流, 直接用 tools/hexdump_frames.py 分析
      <path>.chunks.txt  每次 recv 的 (偏移, 长度, 到达时刻), 用于判断
                         模组是否按帧边界分包、有没有把多帧粘成一个大包
    """

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("wb")
        # 行缓冲: 程序被强杀时记录不至于全丢
        self._idx = self.path.with_suffix(self.path.suffix + ".chunks.txt").open(
            "w", encoding="utf-8", buffering=1)
        self._idx.write("# offset\tlength\tt_arrive_s\n")
        self._off = 0
        self._writes = 0
        self._t0 = time.time()

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._f.write(chunk)
        self._idx.write(f"{self._off}\t{len(chunk)}\t{time.time() - self._t0:.6f}\n")
        self._off += len(chunk)
        self._writes += 1
        if self._writes % 50 == 0:
            self._f.flush()

    def close(self) -> None:
        for f in (self._f, self._idx):
            try:
                f.close()
            except Exception:  # noqa: BLE001
                pass


class RealSource(Source):
    """真机收包 -> 累积 -> 切帧 -> contract.frame_to_ui_dict。

    支持四种链路(见 receivers.make_receiver): udp / tcp / tcp-client / serial。
    缓冲按实例持有, 不跨调用丢弃半帧 —— 这是原 parse_stream(buf.clear()) 丢帧的根因。
    """

    kind = "real"

    def __init__(self, port: int = 5000, proto: str = "udp", host: str = "0.0.0.0",
                 com: str = "COM3", baud: int = 921600, raw_dump: Optional[Union[str, Path]] = None):
        from receivers import make_receiver

        self.proto = proto
        self.com = com
        self.baud = int(baud)
        self._rx = make_receiver(proto, port=port, host=host, com=com, baud=baud)
        self._buf = bytearray()
        self._dump = RawDumper(raw_dump) if raw_dump else None
        self.n_frames = 0
        self.n_crc_bad = 0
        self.n_hdr_bad = 0        # 帧头自报的通道数与配置不符: 说明切帧边界本身就是错的
        self._t_first = None
        self._t_last = None

    def start(self) -> None:
        self._rx.start()

    def stop(self) -> None:
        self._rx.stop()
        if self._dump:
            self._dump.close()
            self._dump = None

    def poll_frames(self) -> list[dict]:
        while True:
            chunk = self._rx.pop()
            if chunk is None:
                break
            if self._dump:
                self._dump.write(chunk)
            self._buf += chunk
        frames = []
        for df in parse_stream(self._buf):
            if not df.crc_ok:
                self.n_crc_bad += 1
                continue
            if not getattr(df, "header_ok", True):
                self.n_hdr_bad += 1
                continue
            fr = contract.from_protocol(df, t_host_s=time.time())
            frames.append(contract.frame_to_ui_dict(fr))
        if frames:
            self.n_frames += len(frames)
            now = time.time()
            if self._t_first is None:
                self._t_first = now
            self._t_last = now
        return frames

    def measured_fps(self) -> float:
        """实测帧率。帧格式/节拍还没冻结时, 以它为准而不是代码里的假设值。"""
        if self._t_first is None or self._t_last is None or self.n_frames < 2:
            return 0.0
        span = self._t_last - self._t_first
        return (self.n_frames - 1) / span if span > 0 else 0.0

    def stats(self) -> dict:
        d = self._rx.stats()
        d.update({
            "frames": self.n_frames,
            "crc_bad": self.n_crc_bad,
            "hdr_bad": self.n_hdr_bad,
            "fps": round(self.measured_fps(), 1),
            "buffered_bytes": len(self._buf),
            "dump": str(self._dump.path) if self._dump else None,
        })
        return d


# --------------------------------------------------------------------------
# dry: 录制字节回放(B1 脱机门禁的输入)
# --------------------------------------------------------------------------
class DrySource(Source):
    """回放一个录制好的原始字节文件, 按可配置的分块大小喂解析器。

    chunk_sizes 默认故意不是帧长的整数倍, 用来复现"帧头被 recv 边界切开"这个真实故障。
    """

    kind = "dry"

    def __init__(self, path: Union[str, Path], chunk_sizes: tuple[int, ...] = (1471, 37, 2048, 5, 900)):
        self.path = Path(path)
        self._data = self.path.read_bytes()
        self._pos = 0
        self._buf = bytearray()
        self._chunk_idx = 0
        self._chunk_sizes = list(chunk_sizes) or [1024]
        self.frames_read = 0

    @property
    def exhausted(self) -> bool:
        return self._pos >= len(self._data)

    def poll_frames(self) -> list[dict]:
        if self.exhausted:
            return []
        n = self._chunk_sizes[self._chunk_idx % len(self._chunk_sizes)]
        self._chunk_idx += 1
        self._buf += self._data[self._pos:self._pos + n]
        self._pos += n
        frames = [contract.frame_to_ui_dict(contract.from_protocol(df))
                  for df in parse_stream(self._buf) if df.crc_ok and df.header_ok]
        self.frames_read += len(frames)
        return frames


# --------------------------------------------------------------------------
# 工厂 + CLI
# --------------------------------------------------------------------------
def make_source(kind: str = "real", **kw) -> Source:
    # 帧布局与增益折算在这里统一设定: 三种来源(sim/real/dry)都必须同源,
    # 否则仿真按 4 通道组帧、真机按 6 通道切帧, 现场会对不上还找不到原因。
    device_frame.configure(
        layout=kw.get("protocol") or None,
        gain=kw.get("eeg_gain") or None,
        vref=kw.get("eeg_vref") or None,
        preamp_gain=kw.get("eeg_preamp_gain") or None,
    )
    kind = (kind or "real").lower()
    if kind == "sim":
        return SimSource(**{k: v for k, v in kw.items() if k in ("eeg_fs", "fnirs_fs", "seed")})
    # real 之外, 也允许直接写链路名: --source=tcp-client / --source=serial
    if kind in ("real", "udp", "tcp", "tcp-client", "serial", "uart", "com"):
        opts = {k: v for k, v in kw.items()
                if k in ("port", "proto", "host", "com", "baud", "raw_dump")}
        if kind != "real":
            opts["proto"] = kind
        return RealSource(**opts)
    if kind == "dry":
        if "path" not in kw or not kw["path"]:
            raise SystemExit("--source=dry 需要 --replay=<录制字节文件路径>")
        return DrySource(kw["path"])
    # 文本行链路(有线固件的 ASCII 输出)。不是 AA55 二进制帧, 解析路径完全不同,
    # 因此不经过 device_frame.configure 的布局/增益折算。
    if kind in ("text", "serial-text", "firewater"):
        # 给了 --replay 就走文件回放, 否则开串口
        if kw.get("path"):
            from text_source import TextFileSource
            return TextFileSource(kw["path"])
        from text_source import TextSource
        return TextSource(com=kw.get("com") or "COM3", baud=kw.get("baud") or 921600,
                          raw_dump=kw.get("raw_dump"))
    if kind in ("text-file", "textfile"):
        from text_source import TextFileSource
        if "path" not in kw or not kw["path"]:
            raise SystemExit("--source=text-file 需要 --replay=<文本文件路径>")
        return TextFileSource(kw["path"])
    raise SystemExit(f"未知 --source={kind}, 可选 sim / real / dry / text / text-file")


def parse_argv(argv: list[str], base: Optional[dict] = None) -> dict:
    """极简参数解析, 不引入 argparse 依赖(3 档 source + 四种链路 + 回放文件)。

    base: 来自 config/run.json 的默认值, 命令行参数覆盖它。这样"双击 exe"
          也能连真机, 不必每次都敲命令行。
    """
    # 默认 real/serial/fw100: 对应当前固件 USART1 的 100 字节二进制帧。
    # 合成数据必须显式 --source=sim,
    # 避免现场"看到波形在动就以为板子通了"。(2026-11-12)
    out = {"source": "real", "port": 5000, "proto": "serial", "replay": None,
           "extra_cols": False, "headless": False,
           "host": "0.0.0.0", "com": "COM3", "baud": 921600, "raw_dump": None,
           # 帧布局与 ADS1299 增益: 合同 F2 要求折算系数随增益联动, 所以增益必须是配置项
           "protocol": "fw100", "eeg_gain": 24.0, "eeg_vref": 4.5,
           # 合同 A3 前置 INA333 G=1000±5%（硬件固定值）。显示值要折算到电极端,
           # 必须除以它; 漏除就是所有 EEG 数值大 1000 倍。
           "eeg_preamp_gain": 1000.0}
    if base:
        for k, v in base.items():
            if k in out and v is not None:
                out[k] = type(out[k])(v) if isinstance(out[k], (int, float)) and not isinstance(out[k], bool) else v
    for a in argv:
        if a.startswith("--source="):
            out["source"] = a.split("=", 1)[1]
        elif a.startswith("--port="):
            out["port"] = int(a.split("=", 1)[1])
        elif a.startswith("--proto="):
            out["proto"] = a.split("=", 1)[1]
        elif a.startswith("--host="):
            out["host"] = a.split("=", 1)[1]
        elif a.startswith("--com="):
            out["com"] = a.split("=", 1)[1]
        elif a.startswith("--baud="):
            out["baud"] = int(a.split("=", 1)[1])
        elif a.startswith("--raw-dump="):
            out["raw_dump"] = a.split("=", 1)[1]
        elif a.startswith("--protocol="):
            out["protocol"] = a.split("=", 1)[1]
        elif a.startswith("--eeg-gain="):
            out["eeg_gain"] = float(a.split("=", 1)[1])
        elif a.startswith("--eeg-vref="):
            out["eeg_vref"] = float(a.split("=", 1)[1])
        elif a.startswith("--preamp-gain="):
            out["eeg_preamp_gain"] = float(a.split("=", 1)[1])
        elif a.startswith("--replay="):
            out["replay"] = a.split("=", 1)[1]
        elif a == "--extra-cols":
            out["extra_cols"] = True
        elif a == "--headless":
            out["headless"] = True
    return out
