"""S2/S6/S7 存储: CSV + EDF + SNIRF + XDF, 统一时间轴."""
from __future__ import annotations
from pathlib import Path
import csv, datetime


CSV_HEADER = (["timestamp_s", "device_id", "seq", "battery_pct", "loss_pct"]
    + [f"eeg{i+1}_uv" for i in range(4)]
    + [f"fnirs{c+1}_{w}" for c in range(10) for w in ("760", "850")]
    + ["hbO", "hbR", "accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z",
       "brain_active", "brain_relax", "focus", "hr_bpm", "event"])

class CsvWriter:
    def __init__(self, path: str | Path, header: list[str] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.header = list(header) if header else list(CSV_HEADER)
        self.f = self.path.open("w", newline="", encoding="utf-8-sig")
        self.w = csv.writer(self.f)
        self.w.writerow(self.header)
    def write_row(self, row: list):
        if len(row) != len(self.header):
            raise ValueError(f"CSV 列数不一致: 行 {len(row)} 列, 表头 {len(self.header)} 列")
        self.w.writerow(row)
    def close(self):
        self.f.close()

def _read_csv_columns(csv_path: str | Path) -> dict[str, list[float]]:
    """CSV -> 列字典。空串(未标定/未对时)记为 0.0, 只用于数值导出。"""
    import csv as _csv

    cols: dict[str, list[float]] = {}
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in _csv.DictReader(f):
            for k, v in row.items():
                cols.setdefault(k, []).append(float(v) if v not in ("", None) else 0.0)
    return cols


def _fs_from_time(cols: dict) -> float:
    """从 timestamp_s 反推采样率(取相邻间隔中位数)。

    为什么不用调用方传的值: 界面里的 fs_eeg 是实测帧率的平滑值, 换链路/换固件
    帧率会变; 而 CSV 里的时间戳是"这行到底什么时候录的"。EDF 的采样率一旦写错,
    EDFbrowser 里的时间轴就整体缩放 N 倍, 甲方一验就发现对不上。
    """
    import statistics

    t = cols.get("timestamp_s") or []
    if len(t) < 3:
        return 0.0
    dt = [b - a for a, b in zip(t, t[1:]) if b > a]
    if not dt:
        return 0.0
    med = statistics.median(dt)
    return round(1.0 / med, 3) if med > 0 else 0.0


def export_edf(csv_path: str | Path, out_path: str | Path, fs_eeg: float | None = None):
    """CSV -> EDF, 需 pyedflib; 第三方可用 EDFbrowser / MNE 打开验证。

    两个曾经的坑:
      1. 采样率写死 500 —— 录的是 100 Hz 就整体压缩 5 倍。现在优先用
         timestamp_s 反推, 传进来的 fs_eeg 只在没有时间戳时兜底。
      2. physical_min/max 写死 ±500 µV —— 睡眠记录里翻身/咬牙轻松过千微伏,
         pyedflib 的 write_edf 直接 assert 失败, 界面上表现为"EDF跳过",
         等于真实数据一条都导不出来。现在按各通道实际幅值自适应量程。
    """
    import math

    import numpy as np
    from pyedflib import highlevel

    cols = _read_csv_columns(csv_path)
    eeg_cols = [f"eeg{i+1}_uv" for i in range(4)]
    missing = [c for c in eeg_cols if c not in cols]
    if missing:
        raise KeyError(f"CSV 缺少 EEG 列: {missing}")

    sigs = [np.asarray(cols[c], dtype=float) for c in eeg_cols]
    if not sigs[0].size:
        raise ValueError("CSV 里没有数据行")

    fs = _fs_from_time(cols)
    if fs <= 0:
        fs = float(fs_eeg or 500.0)
    elif fs_eeg and abs(fs - fs_eeg) / fs > 0.05:
        print(f"[EDF] 采样率按时间戳取 {fs} Hz, 调用方传的是 {fs_eeg} Hz; 以时间戳为准")

    hdr = highlevel.make_header(patientname="aidsleep")
    chs = []
    for i, s in enumerate(sigs):
        peak = float(np.max(np.abs(s))) if s.size else 0.0
        # 留 10% 余量, 下限 ±100 µV; 这样 16bit 量化在 ±100µV 时约 0.003 µV/LSB
        lim = max(100.0, math.ceil(peak * 1.1))
        chs.append(highlevel.make_signal_header(
            f"EEG{i+1}", dimension="uV", sample_frequency=fs,
            physical_min=-lim, physical_max=lim,
            digital_min=-32768, digital_max=32767,
        ))
    highlevel.write_edf(str(out_path), sigs, chs, hdr)
    return out_path

def _resolve_fnirs_columns(cols: dict) -> list[str]:
    """定位 20 列 fNIRS 数据。优先"光强"列, 退回"原始码值"列。

    兼容三套命名: 契约命名 fnirs1_760_intensity / fnirs1_760_raw, 以及历史命名 fnirs1_760。
    """
    for suffix in ("_intensity", "_raw", ""):
        cand = [f"fnirs{c}_{w}{suffix}" for c in range(1, 11) for w in (760, 850)]
        if all(k in cols for k in cand):
            return cand
    raise KeyError(
        "CSV 里找不到 20 列 fNIRS 数据; 期望 fnirs1_760_intensity / fnirs1_760_raw / fnirs1_760 之一"
    )


def export_snirf(csv_path: str | Path, out_path: str | Path,
                 subject_id: str = "aidsleep", probe: dict | None = None):
    """CSV -> SNIRF 1.0 (HDF5), 可用 Homer3 打开验证。需 h5py。

    结构对照 SNIRF 1.0 规范(https://fnirs.github.io/snirf/):
      根组必须叫 nirs —— 规范原文 "a single root-level group named /nirs",
        写成 nirs1 时 Homer3 / MNE 都找不到, 直接报 object 'nirs' doesn't exist;
      多组数据用 nirs/data1, nirs/data2 区分, 而不是 nirs1/nirs2。
      nirs/metaDataTags/{SubjectID,MeasurementDate,MeasurementTime,
                          LengthUnit,TimeUnit,FrequencyUnit}   均必填
      nirs/data1/dataTimeSeries   形状 (time, 通道)
      nirs/data1/time             从 0 起算(相对文件原点)
      nirs/data1/measurementList{k}/{sourceIndex,detectorIndex,
                                     wavelengthIndex,dataType,dataTypeIndex}
      nirs/probe/{wavelengths,sourcePos3D,detectorPos3D}

    两处易错点:
      - measurementList{k} 必须直接挂在 data1 下, 不要多套一层容器组;
      - dataType=1 表示 CW 原始幅度; dataTypeLabel 只在 dataType=99999(processed) 时才填,
        写 dataType=1 + dataTypeLabel="dOD" 会被 Homer3 当成光密度解释。
    """
    import datetime

    import h5py
    import numpy as np

    cols = _read_csv_columns(csv_path)

    names = _resolve_fnirs_columns(cols)
    data = np.array([cols[c] for c in names], dtype=np.float64).T      # (time, 20)
    t = np.asarray(cols["timestamp_s"], dtype=np.float64)
    if t.size:
        t = t - t[0]                                                   # 规范: time 相对原点

    wl = np.array([760.0, 850.0])
    now = datetime.datetime.now()

    with h5py.File(str(out_path), "w") as f:
        f.attrs["formatVersion"] = "1.0"
        nirs = f.create_group("nirs")   # 规范: 根组必须是 nirs

        tags = nirs.create_group("metaDataTags")
        tags.create_dataset("SubjectID", data=subject_id)
        tags.create_dataset("MeasurementDate", data=now.strftime("%Y-%m-%d"))
        tags.create_dataset("MeasurementTime", data=now.strftime("%H:%M:%S"))
        tags.create_dataset("LengthUnit", data="mm")
        tags.create_dataset("TimeUnit", data="s")
        tags.create_dataset("FrequencyUnit", data="Hz")

        d = nirs.create_group("data1")
        d.create_dataset("dataTimeSeries", data=data)
        d.create_dataset("time", data=t)
        for k in range(data.shape[1]):
            g = d.create_group(f"measurementList{k + 1}")
            g.create_dataset("sourceIndex", data=np.int32(1))
            g.create_dataset("detectorIndex", data=np.int32((k // 2) + 1))
            g.create_dataset("wavelengthIndex", data=np.int32((k % 2) + 1))
            g.create_dataset("dataType", data=np.int32(1))           # 1 = CW 原始幅度
            g.create_dataset("dataTypeIndex", data=np.int32(1))      # 规范必填

        pr = nirs.create_group("probe")
        pr.create_dataset("wavelengths", data=wl)
        # 占位几何: S4 待甲方布局坐标表冻结后, 由 probe={"source":..., "detector":...} 传入真实坐标
        if probe is None:
            pr.create_dataset("sourcePos3D", data=np.zeros((1, 3)))
            pr.create_dataset("detectorPos3D", data=np.zeros((10, 3)))
        else:
            pr.create_dataset("sourcePos3D", data=np.asarray(probe["source"], dtype=np.float64))
            pr.create_dataset("detectorPos3D", data=np.asarray(probe["detector"], dtype=np.float64))
    return out_path

# ---- 最小纯Python XDF写出器 (XDF 1.x, pyxdf/MoBI可读) ----
import struct as _st

def _chunk(tag: int, content: bytes, has_stream: bool = False) -> bytes:
    n = 2 + (4 if has_stream else 0) + len(content)
    if n <= 0xFF:
        return b"\x01" + bytes([n]) + _st.pack("<H", tag) + content
    return b"\x04" + _st.pack("<I", n) + _st.pack("<H", tag) + content

def _stream_xml(name: str, stype: str, nch: int, fs: float, dtype: str = "float32") -> bytes:
    chs = "".join(f"<channel><label>ch{i+1}</label><unit>uV</unit><type>{stype}</type></channel>" for i in range(nch))
    return (f'<info><name>{name}</name><type>{stype}</type><channel_count>{nch}</channel_count>'
            f'<nominal_srate>{fs:g}</nominal_srate><channel_format>{dtype}</channel_format>'
            f'<desc><channels>{chs}</channels></desc></info>').encode()

def export_xdf(sessions: dict, out_path: str | Path):
    """多设备联合 -> XDF(含时钟校正信息). sessions={name:(data[ch,time], fs, stype?)}.
    经 Storage/xdf_writer 纯python写出, 已用 pyxdf.load_xdf 回读验证, MoBI可读."""
    from xdf_writer import write_xdf
    norm = {}
    for name, val in sessions.items():
        norm[name] = (val[0], float(val[1]), val[2] if len(val) > 2 else "EEG")
    return write_xdf(out_path, norm)
