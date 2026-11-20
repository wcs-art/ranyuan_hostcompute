"""数据契约：一帧数据的唯一形状与转换。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

# --------------------------------------------------------------------------
# 通道口径 —— 已按合同 F1/F2 冻结(2026-11-12 合同 2.5.2 截图)
#   合同原文: "EEG 4ch × 24bit(首版; EV2 起 6ch, 按帧头通道数字段配置)、
#             fNIRS 10ch × 2 波长 × 24bit"、"LED 双波长时序"
#   因此 4/10/2 波长是**合同值**, 不是临时占位; 改这三个数等于改合同。
#   device_frame 的默认帧布局从这个文件取值, 保证"帧里几路"与"CSV 几列"同源。
# --------------------------------------------------------------------------
N_EEG = 4
N_FNIRS = 10
FNIRS_WLS = (760, 850)


# --------------------------------------------------------------------------
# 唯一的帧形状
# --------------------------------------------------------------------------
@dataclass
class Frame:
    """一帧已校验数据的内部表示。链路层产出它, 其它层只消费它。"""

    device_id: int
    seq: int
    t_device_ms: int                      # 帧内时间戳(设备上电归零, 单调 ms)
    t_host_s: float                       # 上位机接收时刻(统一时间轴上的秒)
    t_abs_ms: int = 0                     # 设备侧绝对时间(Unix ms, 对时后有效; 0=未对时)
    eeg_uv: list[float] = field(default_factory=list)          # 4 路, 已折算 uV
    fnirs_raw: list[list[int]] = field(default_factory=list)   # [10][2], 原始码值
    fnirs_intensity: Optional[list[float]] = None              # 20 路光强, None=未标定
    od: Optional[list[float]] = None                           # 20 路光密度, None=未算
    hbo: Optional[list[float]] = None                          # 10 通道 dHbO
    hbr: Optional[list[float]] = None                          # 10 通道 dHbR
    hbt: Optional[list[float]] = None                          # 10 通道 dHbT = dHbO+dHbR
    sto2: Optional[list[float]] = None                         # 10 通道变化量氧合占比(非绝对 StO2)
    acc_xyz: Optional[list[float]] = None
    gyro_xyz: Optional[list[float]] = None
    battery_pct: float = -1.0                                  # <0 表示设备未上报
    loss_pct: float = 0.0
    event_type: int = 0
    event_ts: int = 0
    crc_ok: bool = False


# --------------------------------------------------------------------------
# CSV 列定义: 三组分离, 默认只出第一组
# --------------------------------------------------------------------------
def _raw_cols() -> list[str]:
    return [f"fnirs{c}_{w}_raw" for c in range(1, N_FNIRS + 1) for w in FNIRS_WLS]


def _intensity_cols() -> list[str]:
    return [f"fnirs{c}_{w}_intensity" for c in range(1, N_FNIRS + 1) for w in FNIRS_WLS]


def _od_cols() -> list[str]:
    return [f"fnirs{c}_{w}_od" for c in range(1, N_FNIRS + 1) for w in FNIRS_WLS]


#: S1~S5 必需列。缺任何一列, S2 验收不成立(光强列是甲方独立复算的依据)。
#: time_abs_ms 对应合同 F2 "数据项须同时携带绝对时间戳…用于多设备数据导出后的人为对比",
#: 未对时时写空串(不是 0), 与"未标定写空"同一口径。
REQUIRED_COLUMNS: list[str] = (
    ["timestamp_s", "time_abs_ms", "device_id", "seq", "battery_pct", "loss_pct"]
    + [f"eeg{i}_uv" for i in range(1, N_EEG + 1)]
    + _raw_cols()
    + _intensity_cols()
    + ["event_type", "event_ts"]
)

#: S2 原文列为"可选存 OD 与 ΔHbO/ΔHbR" —— 逐通道存, 不用均值压缩。
#: 文档《fNIRS和血氧计算20261012---VR002》§1.3 的输出指标追加 ΔHbT 与
#: 变化量氧合占比; 列名带 delta 后缀, 防止下游把它当绝对 StO₂ 用。
OPTIONAL_S2_COLUMNS: list[str] = (
    _od_cols()
    + [f"hbo_{c}" for c in range(1, N_FNIRS + 1)]
    + [f"hbr_{c}" for c in range(1, N_FNIRS + 1)]
    + [f"hbt_{c}" for c in range(1, N_FNIRS + 1)]
    + [f"sto2_delta_{c}" for c in range(1, N_FNIRS + 1)]
)

#: 交付范围外: 6 列 S8 姿态 + 4 列 S9 指标。默认不写, 由 --extra-cols 打开。
EXTRA_COLUMNS: list[str] = [
    "accel_x", "accel_y", "accel_z",
    "gyro_x", "gyro_y", "gyro_z",
    "brain_active", "brain_relax", "focus", "hr_bpm",
]


def csv_header(optional: bool = False, extra: bool = False) -> list[str]:
    """返回表头。默认只含 S1~S5 必需列。"""
    cols = list(REQUIRED_COLUMNS)
    if optional:
        cols += OPTIONAL_S2_COLUMNS
    if extra:
        cols += EXTRA_COLUMNS
    return cols


# --------------------------------------------------------------------------
# 转换 1: 协议解析结果 -> Frame
# --------------------------------------------------------------------------
def from_protocol(df: Any, t_host_s: Optional[float] = None) -> Frame:
    """Protocol.device_frame.DataFrame -> Frame.

    t_host_s 由调用方(接收侧)传入, 因为"这帧什么时候到的"只有那一层知道;
    不传则暂用设备时间戳换算, 并置 0 起点 —— 这属于未完成的时间映射, 见 clock_mapper。
    """
    ts = getattr(df, "timestamp_ms", 0)
    return Frame(
        device_id=getattr(df, "device_id", 0),
        seq=getattr(df, "seq", 0),
        t_device_ms=ts,
        t_host_s=float(t_host_s) if t_host_s is not None else ts / 1000.0,
        t_abs_ms=int(getattr(df, "timestamp_abs_ms", 0) or 0),
        eeg_uv=list(getattr(df, "eeg_uv", []) or []),
        fnirs_raw=[list(ch) for ch in (getattr(df, "fnirs_raw", []) or [])],
        event_type=getattr(df, "event_type", 0),
        event_ts=getattr(df, "event_ts", 0),
        crc_ok=bool(getattr(df, "crc_ok", False)),
    )


# --------------------------------------------------------------------------
# 转换 2: Frame -> 界面 push_frame 的入参(dict, 按名传参, 不按位置)
# --------------------------------------------------------------------------
def frame_to_ui_dict(fr: Frame) -> dict:
    """返回的键必须与 Monitor14.push_frame 的形参名逐一对应。

    tests/test_contract.py 用 ast 解析 push_frame 签名做交叉校验:
    本函数改了键、或 push_frame 改了形参名, 测试立刻红。
    """
    return {
        "device_id": int(fr.device_id),
        "seq": fr.seq,
        "ts_abs_ms": int(fr.t_abs_ms or 0),
        "t_dev_ms": int(fr.t_device_ms or 0),   # 设备单调时钟：CSV 采样时刻的首选来源
        "eeg_uv": [float(v) for v in fr.eeg_uv],
        # 这一帧真实带了几路 EEG。界面只画这几路，其余不画（不留假平线）。
        "eeg_n": len(fr.eeg_uv),
        "hbo": list(fr.hbo) if fr.hbo is not None else [0.0] * N_FNIRS,
        "hbr": list(fr.hbr) if fr.hbr is not None else [0.0] * N_FNIRS,
        "raw": _flatten_raw(fr.fnirs_raw),
        "battery": fr.battery_pct,
        "acc_xyz": list(fr.acc_xyz) if fr.acc_xyz is not None else None,
        "gyro_xyz": list(fr.gyro_xyz) if fr.gyro_xyz is not None else None,
        "event_type": int(fr.event_type),
        "event_ts": int(fr.event_ts),
    }


def _flatten_raw(fnirs_raw: list[list[int]]) -> list[float]:
    """[10][2] -> 20 个, 顺序与列定义一致: c1-760, c1-850, c2-760, ..."""
    out: list[float] = []
    for ch in fnirs_raw:
        for wl in (ch or [])[:len(FNIRS_WLS)]:
            out.append(float(wl))
    if len(out) < N_FNIRS * len(FNIRS_WLS):
        out += [0.0] * (N_FNIRS * len(FNIRS_WLS) - len(out))
    return out[: N_FNIRS * len(FNIRS_WLS)]


def unnest_raw(flat, n_wl: Optional[int] = None) -> list[list[float]]:
    """_flatten_raw 的逆操作: 平铺 20 列 -> [10][2]。

    界面层拿到的是平铺的 raw(便于画图), 写 CSV 要走 frame_to_row, 需要嵌套形状。
    有了它, 界面侧就不必再手写一遍列序 —— 那是列错位的唯一来源。
    """
    n_wl = n_wl or len(FNIRS_WLS)
    vals = [float(v) for v in (flat or [])]
    need = N_FNIRS * n_wl
    vals += [0.0] * (need - len(vals))
    return [vals[c * n_wl:(c + 1) * n_wl] for c in range(N_FNIRS)]


# --------------------------------------------------------------------------
# 转换 3: Frame -> CSV 行(长度恒等于 len(csv_header(...)))
# --------------------------------------------------------------------------
def _flat(vals: Any, n: int) -> list:
    """把 [[a,b],[c,d]] 或 [a,b,c,d] 统一成定长 n 的平铺列表。

    为什么需要这个: frame_to_row 按列数逐列取值, 一旦调用方把 20 路光强按
    [10][2] 传进来, 只会写出 10 列, 整行比表头少 10 列。CsvWriter 会拦下并
    抛 ValueError —— 但在界面里这等于"点开始记录后一行都写不进去"。
    与其让调用方记住形状, 不如在这里两种都接。
    """
    if not vals:
        return [""] * n
    out: list = []
    for v in vals:
        if isinstance(v, (list, tuple)):
            out.extend(v)
        else:
            out.append(v)
    if len(out) < n:
        out += [""] * (n - len(out))
    return out[:n]


def frame_to_row(fr: Frame, optional: bool = False, extra: bool = False,
                 metrics: Optional[dict] = None) -> list:
    """未标定的量写空串, 不写 0.0。

    metrics: S9 指标(范围外列)。界面侧算出真实值时传进来, 不传则写空串 ——
    宁可为空, 也不编一个 50.0 出来。
    """
    row: list = [
        round(fr.t_host_s, 6),
        fr.t_abs_ms if fr.t_abs_ms else "",     # 未对时写空, 不写 0
        fr.device_id, fr.seq,
        _num(fr.battery_pct), _num(fr.loss_pct),
    ]
    row += [_num(v) for v in fr.eeg_uv[:N_EEG]] + [""] * max(0, N_EEG - len(fr.eeg_uv))
    row += [_num(v) for v in _flatten_raw(fr.fnirs_raw)]
    row += [_num(v) if v is not None else ""
            for v in _flat(fr.fnirs_intensity, N_FNIRS * len(FNIRS_WLS))]
    row += [fr.event_type, fr.event_ts]
    if optional:
        row += [_num(v) if v is not None else "" for v in _flat(fr.od, N_FNIRS * len(FNIRS_WLS))]
        row += [_num(v) if v is not None else "" for v in _flat(fr.hbo, N_FNIRS)]
        row += [_num(v) if v is not None else "" for v in _flat(fr.hbr, N_FNIRS)]
        row += [_num(v) if v is not None else "" for v in _flat(fr.hbt, N_FNIRS)]
        row += [_num(v) if v is not None else "" for v in _flat(fr.sto2, N_FNIRS)]
    if extra:
        row += [_num(v) if v is not None else "" for v in (fr.acc_xyz or [None] * 3)]
        row += [_num(v) if v is not None else "" for v in (fr.gyro_xyz or [None] * 3)]
        keys = ("brain_active", "brain_relax", "focus", "hr_bpm")
        if metrics:
            row += [_num(metrics.get(k)) for k in keys]
        else:
            row += [""] * 4  # 界面未算出 S9 指标时写空, 不写占位值
    return row


def _num(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, float):
        # NaN/Inf 是"无有效值", 不是 0。写成 nan 字符串会让下游 pandas 读成
        # 文本列; 写成 0 更糟 —— 看起来像合法读数。
        return round(v, 6) if math.isfinite(v) else ""
    return v
