"""下位机二进制帧布局定义、CRC 校验、打包与解析。"""
from __future__ import annotations
from typing import Optional, Union

import struct
from dataclasses import dataclass, field

from frame_slicer import FrameSlicer

FRAME_HEAD = b"\xAA\x55"
PROTO_VER = 1

#: flags 位定义
FLAG_FNIRS_NEW = 0x01


# --------------------------------------------------------------------------
# 布局描述
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FrameLayout:
    """一套帧布局。所有偏移都由它推导，不在解析代码里写死数字。"""

    name: str
    n_eeg: int = 4
    n_fnirs: int = 10
    n_wl: int = 2
    ratio: int = 1
    #: 帧头是否含 ver/n_eeg/n_fnirs/n_wl/ratio/flags 这 6 个字节
    has_counts: bool = True
    #: 是否含 8 字节绝对时间戳
    has_abs_ts: bool = True
    #: 首版固件的短帧头：dev/ver/flags，没有通道数字段
    has_short_header: bool = False

    # ---- 偏移推导（帧内绝对偏移） ----
    @property
    def off_seq(self) -> int:
        return 9 if self.has_counts else (5 if self.has_short_header else 3)

    @property
    def off_ts_mono(self) -> int:
        return self.off_seq + 4

    @property
    def off_ts_abs(self) -> int:
        return self.off_ts_mono + 4

    @property
    def off_data(self) -> int:
        return self.off_ts_abs + (8 if self.has_abs_ts else 0)

    @property
    def n_eeg_samples(self) -> int:
        """每帧 EEG 样点数。ratio>1 是 EV2 双速率用的，首版恒为 n_eeg。"""
        return self.n_eeg * max(1, self.ratio)

    @property
    def eeg_bytes(self) -> int:
        return self.n_eeg_samples * 3

    @property
    def fnirs_bytes(self) -> int:
        return self.n_fnirs * self.n_wl * 3

    @property
    def off_event(self) -> int:
        return self.off_data + self.eeg_bytes + self.fnirs_bytes

    @property
    def off_crc(self) -> int:
        return self.off_event + 5

    @property
    def frame_len(self) -> int:
        return self.off_crc + 2

    @property
    def body_len(self) -> int:
        """CRC 覆盖的字节数 = 帧长 - 帧头 - CRC。"""
        return self.frame_len - 4


def _default_layout() -> FrameLayout:
    """默认布局从 contract 的通道常量取，保证"帧里几路"和"CSV 几列"永远同源。"""
    try:
        import contract

        return FrameLayout(
            "v1",
            n_eeg=int(contract.N_EEG),
            n_fnirs=int(contract.N_FNIRS),
            n_wl=len(contract.FNIRS_WLS),
        )
    except Exception:  # noqa: BLE001 - 独立使用本模块时不强依赖 contract
        return FrameLayout("v1")


LAYOUT_V1 = _default_layout()
LAYOUT_FW100 = FrameLayout("fw100", has_counts=False, has_abs_ts=True,
                           has_short_header=True)
LAYOUT_COMPAT90 = FrameLayout("compat90", has_counts=False, has_abs_ts=False)


def _with_ratio(base: FrameLayout, ratio: int, name: str) -> FrameLayout:
    """派生"一帧 ratio 个 EEG 样点"的布局。帧长 = 104 + (ratio-1)×n_eeg×3。"""
    return FrameLayout(name, n_eeg=base.n_eeg, n_fnirs=base.n_fnirs, n_wl=base.n_wl,
                       ratio=ratio, has_counts=base.has_counts, has_abs_ts=base.has_abs_ts)


#: 合同 F2「EEG 与 fNIRS 采样率不同，帧内复用方式须明确」的落地方案：
#: EEG 500 SPS / fNIRS 100 SPS（判据"每通道每波长 ≥100Hz，论文上限 100Hz"）
#: → ratio = 5，一帧 5 个 EEG 样点 + 1 组 fNIRS 样点，帧长 152 B，帧率 100 Hz。
#: 固件真按这个发就配 "protocol": "v1r5"；仍发 104 B 就用默认 "v1"。
LAYOUT_V1R5 = _with_ratio(LAYOUT_V1, 5, "v1r5")

_LAYOUT: FrameLayout = LAYOUT_V1


def get_layout() -> FrameLayout:
    return _LAYOUT


def layouts() -> dict[str, FrameLayout]:
    return {"v1": LAYOUT_V1, "v1r5": LAYOUT_V1R5,
            "fw100": LAYOUT_FW100, "compat90": LAYOUT_COMPAT90}


def set_layout(name_or_layout: "Union[str, FrameLayout]") -> FrameLayout:
    """切换布局。切完清空解码器缓存 —— 帧长变了，旧半帧不能续。"""
    global _LAYOUT
    if isinstance(name_or_layout, FrameLayout):
        ly = name_or_layout
    else:
        key = str(name_or_layout or "").strip().lower()
        table = layouts()
        if key not in table:
            raise SystemExit(f"未知帧布局 {name_or_layout!r}，可选: {', '.join(table)}")
        ly = table[key]
    if ly != _LAYOUT:
        _LAYOUT = ly
        _DECODERS.clear()
    return _LAYOUT


# --------------------------------------------------------------------------
# 增益折算
# --------------------------------------------------------------------------
#: ADS1299 的 VREF 由寄存器决定：内部 4.5V / 外部参考。CONFIG3 的 PD_REFBUF 决定。
DEFAULT_VREF = 4.5
DEFAULT_GAIN = 24.0
#: 前置仪表放大器增益。合同 A3：「INA333 仪表放大（G = 1000 ± 5%，RG = 100 Ω 0.1%）」，
#: 验收判据也写「增益 前置 1000 ± 5%（10 Hz 信号注入法）」—— 这是硬件固定值，不是可配档位。
DEFAULT_PREAMP_GAIN = 1000.0

_GAIN = DEFAULT_GAIN
_VREF = DEFAULT_VREF
_PREAMP = DEFAULT_PREAMP_GAIN


def ads1299_uv_per_lsb(gain: float = DEFAULT_GAIN, vref: float = DEFAULT_VREF) -> float:
    """ADS1299 **输入引脚**上单 LSB 对应的微伏数（未除前置增益）。

    满量程 = ±VREF/gain，24bit 补码 → 1 LSB = 2·VREF/(gain·2^24) 伏。
    gain=24、VREF=4.5V → 0.0223517 µV/LSB。
    这是芯片手册口径，仅供核对；上位机显示/落盘用的是 electrode_uv_per_lsb()。
    """
    if gain <= 0:
        raise ValueError(f"ADS1299 增益必须为正数，收到 {gain!r}")
    if vref <= 0:
        raise ValueError(f"VREF 必须为正数，收到 {vref!r}")
    return 2.0 * vref / (gain * float(1 << 24)) * 1e6


def electrode_uv_per_lsb(gain: float = DEFAULT_GAIN, vref: float = DEFAULT_VREF,
                         preamp: float = DEFAULT_PREAMP_GAIN) -> float:
    """折算到**电极输入端**的单 LSB 微伏数 —— 合同 F2 要求的口径。

    信号链：电极 → INA333(G=1000) → ADS1299。电极端的电压被放大 1000 倍后
    才进入 ADC，所以电极电压 = ADC 电压 / 1000。
    """
    if preamp <= 0:
        raise ValueError(f"前置增益必须为正数，收到 {preamp!r}")
    return ads1299_uv_per_lsb(gain, vref) / float(preamp)


#: 向后兼容的名字：旧代码/测试按这个常量引用，含义已改为**电极端** µV/LSB。
EEG_GAIN_UV_PER_LSB = electrode_uv_per_lsb()


def __getattr__(name: str):
    """旧名 EEG_CH / FNIRS_CH / FNIRS_WL 动态取自当前布局，避免与 layout 脱节。"""
    if name == "EEG_CH":
        return _LAYOUT.n_eeg
    if name == "FNIRS_CH":
        return _LAYOUT.n_fnirs
    if name == "FNIRS_WL":
        return _LAYOUT.n_wl
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def configure(layout=None, gain: Optional[float] = None,
              vref: Optional[float] = None,
              preamp_gain: Optional[float] = None) -> FrameLayout:
    """一次把"帧长"和"增益折算"都设好。main/source 启动时按 config/run.json 调用。"""
    global _GAIN, _VREF, _PREAMP, EEG_GAIN_UV_PER_LSB
    if gain is not None:
        _GAIN = float(gain)
    if vref is not None:
        _VREF = float(vref)
    if preamp_gain is not None:
        _PREAMP = float(preamp_gain)
    EEG_GAIN_UV_PER_LSB = electrode_uv_per_lsb(_GAIN, _VREF, _PREAMP)
    if layout is not None:
        set_layout(layout)
    return _LAYOUT


def current_gain() -> tuple[float, float, float]:
    """(ads1299 增益, VREF, 前置增益)。"""
    return _GAIN, _VREF, _PREAMP


# --------------------------------------------------------------------------
# 帧结构
# --------------------------------------------------------------------------
@dataclass
class DataFrame:
    device_id: int
    seq: int
    timestamp_ms: int
    eeg_uv: list[float] = field(default_factory=list)
    fnirs_raw: list[list[int]] = field(default_factory=list)   # [ch][wl]
    event_type: int = 0
    event_ts: int = 0
    crc_ok: bool = False
    # ---- v1 新增（compat90 下为默认值，调用方无需判断布局）----
    timestamp_abs_ms: int = 0
    proto_ver: int = 0
    ratio: int = 1
    flags: int = 0
    eeg_raw: list[int] = field(default_factory=list)           # 原始码值，现场核对用
    eeg_uv_all: list[float] = field(default_factory=list)      # ratio>1 时本帧全部样点
    header_ok: bool = True                                     # 帧头通道数与配置是否一致

    @property
    def fnirs_is_new(self) -> bool:
        """本帧 fNIRS 是否为新采样（双速率时只有部分帧有效）。"""
        return bool(self.flags & FLAG_FNIRS_NEW) or self.ratio <= 1


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def _s24(b: bytes) -> int:
    """3 字节有符号补码，小端。"""
    v = int.from_bytes(b, "little", signed=False)
    return v - (1 << 24) if v & 0x800000 else v


def _i24(v: int) -> bytes:
    return (int(v) & 0xFFFFFF).to_bytes(3, "little")


def pack_frame(dev: int, seq: int, ts: int, eeg: list[int], fnirs: list[list[int]],
               ev_t: int = 0, ev_ts: int = 0, *, ts_abs: int = 0,
               flags: Optional[int] = None, layout: Optional[FrameLayout] = None) -> bytes:
    """按当前布局组一帧。eeg 长度 = n_eeg*ratio；fnirs 长度 = n_fnirs × n_wl。"""
    ly = layout or _LAYOUT

    if ly.has_counts:
        body = bytes([
            int(dev) & 0xFF, PROTO_VER,
            ly.n_eeg & 0xFF, ly.n_fnirs & 0xFF, ly.n_wl & 0xFF, max(1, ly.ratio) & 0xFF,
            (FLAG_FNIRS_NEW if flags is None else flags) & 0xFF,
        ])
        body += struct.pack("<II", int(seq) & 0xFFFFFFFF, int(ts) & 0xFFFFFFFF)
        if ly.has_abs_ts:
            body += struct.pack("<Q", int(ts_abs) & 0xFFFFFFFFFFFFFFFF)
    elif ly.has_short_header:
        body = bytes([int(dev) & 0xFF, PROTO_VER,
                      (0 if flags is None else flags) & 0xFF])
        body += struct.pack("<IIQ", int(seq) & 0xFFFFFFFF,
                            int(ts) & 0xFFFFFFFF,
                            int(ts_abs) & 0xFFFFFFFFFFFFFFFF)
    else:
        body = struct.pack("<BII", int(dev) & 0xFF, int(seq) & 0xFFFFFFFF, int(ts) & 0xFFFFFFFF)

    for k in range(ly.n_eeg_samples):
        body += _i24(eeg[k] if k < len(eeg) else 0)
    for c in range(ly.n_fnirs):
        row = fnirs[c] if c < len(fnirs) else []
        for w in range(ly.n_wl):
            body += _i24(row[w] if w < len(row) else 0)

    body += struct.pack("<BI", int(ev_t) & 0xFF, int(ev_ts) & 0xFFFFFFFF)
    return FRAME_HEAD + body + struct.pack("<H", crc16_modbus(body))


def _parse_frame(raw: bytes, ly: FrameLayout) -> DataFrame:
    """解析一个完整帧字节块(含帧头与 CRC 尾部)。切帧由 FrameSlicer 负责。"""
    body = raw[2:ly.off_crc]
    crc_rx = struct.unpack("<H", raw[ly.off_crc:ly.off_crc + 2])[0]
    crc_ok = crc_rx == crc16_modbus(body)

    if ly.has_counts:
        dev, ver = body[0], body[1]
        n_eeg, n_fnirs, n_wl, ratio = body[2], body[3], body[4], max(1, body[5])
        flags = body[6]
        seq, ts = struct.unpack("<II", body[7:15])
        p = 15
        if ly.has_abs_ts:
            ts_abs = struct.unpack("<Q", body[p:p + 8])[0]
            p += 8
        else:
            ts_abs = 0
        # 帧头自报的通道数必须与上位机配置一致，否则切帧边界就是错的 —— 不能静默放过
        header_ok = (ver == PROTO_VER and n_eeg == ly.n_eeg and n_fnirs == ly.n_fnirs
                     and n_wl == ly.n_wl and ratio == max(1, ly.ratio))
    elif ly.has_short_header:
        dev, ver, flags = body[:3]
        seq, ts, ts_abs = struct.unpack("<IIQ", body[3:19])
        n_eeg, n_fnirs, n_wl, ratio = ly.n_eeg, ly.n_fnirs, ly.n_wl, 1
        p = 19
        header_ok = ver == PROTO_VER
    else:
        dev, seq, ts = struct.unpack("<BII", body[:9])
        ver, n_eeg, n_fnirs, n_wl, ratio, flags, ts_abs = 0, ly.n_eeg, ly.n_fnirs, ly.n_wl, 1, 0, 0
        p = 9
        header_ok = True

    eeg_raw = [_s24(body[p + k * 3:p + k * 3 + 3]) for k in range(ly.n_eeg_samples)]
    p += ly.n_eeg_samples * 3
    fnirs: list[list[int]] = []
    for _ in range(ly.n_fnirs):
        fnirs.append([_s24(body[p + k * 3:p + k * 3 + 3]) for k in range(ly.n_wl)])
        p += ly.n_wl * 3
    ev_t = body[p]
    ev_ts = struct.unpack("<I", body[p + 1:p + 5])[0]

    k = EEG_GAIN_UV_PER_LSB
    uv_all = [v * k for v in eeg_raw]
    # 单点管线（界面/CSV）一条帧一个 EEG 值：ratio>1 时取本帧最新一个样点
    uv_one = uv_all[-ly.n_eeg:] if len(uv_all) > ly.n_eeg else uv_all
    return DataFrame(
        device_id=dev, seq=seq, timestamp_ms=ts,
        eeg_uv=uv_one, fnirs_raw=fnirs,
        event_type=ev_t, event_ts=ev_ts, crc_ok=crc_ok,
        timestamp_abs_ms=ts_abs, proto_ver=ver, ratio=ratio, flags=flags,
        eeg_raw=eeg_raw, eeg_uv_all=uv_all, header_ok=header_ok,
    )


# --------------------------------------------------------------------------
# 流解码（跨调用存活 + 半帧归属解码器，原实现缺陷见下）
# --------------------------------------------------------------------------
class StreamDecoder:
    """有状态的流解码器: 切片器跨调用存活, 重同步代价(resync)才是累计值。

    为什么半帧归解码器所有, 而不是写回调用方缓冲:
        旧实现每次 parse_stream 都新建 FrameSlicer, 于是 resync_count/resync_bytes
        每次归零 —— 重同步不可观测, 故障表现成"丢包率恒 0"; 且半帧只存在于调用方的
        bytearray 里, 调用方两次调用之间 clear() 一次就静默丢掉 1~89 字节(≥ 一整帧)。
        这里把 pending 存在解码器内部, 调用方缓冲只保留一份**镜像**:
        镜像被 clear/重建时不猜、不续别人的残字节, 而是作废半帧并计数
        (handoff_dropped / dropped_half_bytes) —— 丢帧可见, 不再静默。
    """

    def __init__(self, layout: FrameLayout) -> None:
        self.layout_name = layout.name
        self.frame_len = layout.frame_len
        self._layout = layout
        self._slicer = FrameSlicer(layout.frame_len, FRAME_HEAD)
        #: 上次留给调用方缓冲的半帧镜像; 空表示上一轮没有残余
        self._mirror = b""
        #: 调用方丢弃镜像(clear/重建缓冲)的次数, 以及由此作废的字节数
        self.handoff_dropped = 0
        self.dropped_half_bytes = 0

    @property
    def resync_count(self) -> int:
        return self._slicer.resync_count

    @property
    def resync_bytes(self) -> int:
        return self._slicer.resync_bytes

    @property
    def pending(self) -> bytes:
        return self._slicer.pending

    def feed(self, buf: bytearray) -> list[bytes]:
        data = bytes(buf)
        if self._mirror:
            if len(data) >= len(self._mirror) and data[:len(self._mirror)] == self._mirror:
                # 常规路径: 调用方保留了镜像, 只喂镜像之后的新增字节(不重复喂)
                data = data[len(self._mirror):]
            else:
                # 镜像对不上 = 调用方 clear()/重建了缓冲(或 id 被新缓冲复用)。
                # 半帧作废要计数; 宁可少续一帧, 也不拿别人的残字节续帧错位出一帧垃圾。
                self.handoff_dropped += 1
                self.dropped_half_bytes += len(self._mirror)
                self._slicer.reset()          # 丢残留, 计数器保留: 重同步代价继续累积
        raws = self._slicer.feed(data)
        self._mirror = self._slicer.pending
        buf[:] = self._mirror                 # buf 只是镜像, 半帧的真实所有者在 self._slicer
        return raws


#: id(buf) -> StreamDecoder。bytearray 不支持 weakref, 无法靠弱引用自动回收,
#: 因此用"容量上限 + 淘汰最久未用": 表长恒定 ≤ _DECODER_CAPACITY, 状态不会无限增长。
#: 淘汰是安全的: 调用方缓冲里本来就有残余镜像, 新建解码器把整段重喂一遍即可复原字节流
#: (只丢计数器, 不丢字节)。id 被新 bytearray 复用则由上面的镜像比对兜住。
_DECODERS: dict[int, StreamDecoder] = {}
_DECODER_CAPACITY = 16


def _decoder_for(buf: bytearray) -> StreamDecoder:
    key = id(buf)
    dec = _DECODERS.pop(key, None)
    if dec is not None and dec.frame_len == _LAYOUT.frame_len:
        _DECODERS[key] = dec                  # 命中后挪到末尾 => 淘汰最久未用
        return dec
    if len(_DECODERS) >= _DECODER_CAPACITY:
        _DECODERS.pop(next(iter(_DECODERS)))
    dec = StreamDecoder(_LAYOUT)
    _DECODERS[key] = dec
    return dec


def parse_stream(buf: bytearray) -> list[DataFrame]:
    """从累积缓冲中切帧, 返回本次解析出的帧列表。

    签名与返回类型与旧版一致(acquisition/source.py 三处调用点无需改动),
    但切帧状态不再每次归零: 解码器按 id(buf) 跨调用存活, resync_count/resync_bytes
    是累计值, 因此"重同步"这件事可被观测。半帧由解码器持有, buf 只保留镜像;
    调用方 clear() 时半帧作废并计入 stream_stats(buf) 的 handoff_dropped。
    """
    dec = _decoder_for(buf)
    return [_parse_frame(raw, dec._layout) for raw in dec.feed(buf)]


def stream_stats(buf: bytearray) -> dict:
    """只读诊断快照: 该缓冲对应解码器的重同步/半帧交接计数(无状态时全 0, 不建状态)。"""
    dec = _DECODERS.get(id(buf))
    if dec is None:
        return {"resync_count": 0, "resync_bytes": 0,
                "handoff_dropped": 0, "dropped_half_bytes": 0, "pending": 0}
    return {"resync_count": dec.resync_count, "resync_bytes": dec.resync_bytes,
            "handoff_dropped": dec.handoff_dropped,
            "dropped_half_bytes": dec.dropped_half_bytes, "pending": len(dec.pending)}
