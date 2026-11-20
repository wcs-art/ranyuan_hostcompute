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


def _fs_from_time(cols: dict, fs_eeg: float | None = None) -> float:
    """从 timestamp_s 反推采样率：用**总跨度**，绝不用相邻间隔中位数。

    台账 M1/P13 的实测：40 ms 定时器每拍批量取 8~20 帧，写盘时刻几乎相同，
    批内相邻间隔 ≈ 单帧处理耗时(1 ms)，批间才有 40 ms；中位数被批内值主导，
    500 fps 的记录算出 996~2237 Hz，EDF 时间轴被压缩 2~4.5 倍。
    跨度法 (n-1)/(t末-t首) 对批内台阶免疫，只认整段记录的平均节奏。

    非单调时间戳（对时跳变/回放乱序）不猜采样率，返回 0 让调用方用显式 fs 兜底。
    """
    t = cols.get("timestamp_s") or []
    if len(t) < 3:
        return 0.0
    if any(b < a for a, b in zip(t, t[1:])):
        return 0.0
    gaps = [b - a for a, b in zip(t, t[1:])]
    span = float(t[-1]) - float(t[0])
    if span <= 0:
        return 0.0
    fs = (len(t) - 1) / span
    # 超过半数相邻时间戳相同 = 批量写盘。跨度法在"均匀分批"时仍可用，
    # 在乱速回放时会算出几千 Hz；用调用方实测帧率做合理性闸门。
    if sum(1 for g in gaps if g == 0) > len(gaps) / 2:
        if fs_eeg and 0.5 <= fs / float(fs_eeg) <= 2.0:
            return round(fs, 3)
        return 0.0
    return round(fs, 3)


def _time_from_cols(cols: dict, fs: float):
    """SNIRF/XDF 的时间向量。正常用记录时间戳；旧记录的"台阶"用等间隔重建。

    台阶判据：相邻间隔最大值 > 4×中位数。批量写盘的记录里批内 1 ms、批间 40 ms，
    比值 40，一眼就能识别；真实丢包只跳过个别间隔，不会整段呈两种节奏。
    """
    import numpy as np

    t = np.asarray(cols.get("timestamp_s") or [], dtype=np.float64)
    if t.size == 0:
        return t
    if fs <= 0 or t.size < 3:
        return t - t[0]
    dt = np.diff(t)
    pos = dt[dt > 0]
    duplicated = float((dt == 0).sum()) / max(1, dt.size) > 0.5
    stepped = bool(pos.size) and float(np.max(pos)) > 4.0 * float(np.median(pos))
    if duplicated or stepped:
        print(f"[时间轴] 时间戳含批量写盘台阶/重复值，SNIRF/XDF 改用等间隔 {fs:g} Hz")
        return np.arange(t.size, dtype=np.float64) / fs
    return t - t[0]


def _eeg_signals(cols: dict, fs_eeg: float | None):
    """CSV -> (4 路信号, 采样率, 信号头)。EDF/BDF 共用，避免两份格式参数走偏。"""
    import math

    import numpy as np
    from pyedflib import highlevel

    eeg_cols = [f"eeg{i+1}_uv" for i in range(4)]
    missing = [c for c in eeg_cols if c not in cols]
    if missing:
        raise KeyError(f"CSV 缺少 EEG 列: {missing}")
    sigs = [np.asarray(cols[c], dtype=float) for c in eeg_cols]
    if not sigs[0].size:
        raise ValueError("CSV 里没有数据行")

    fs = _fs_from_time(cols, fs_eeg)
    if fs <= 0:
        fs = float(fs_eeg or 500.0)
    elif fs_eeg and abs(fs - fs_eeg) / fs > 0.05:
        print(f"[EDF/BDF] 采样率按时间戳跨度取 {fs} Hz, 调用方传的是 {fs_eeg} Hz; 以时间戳为准")

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
    return sigs, fs, chs


def _write_edf_family(csv_path, out_path, fs_eeg, file_type: int):
    """EDF/BDF 共用的低层写出：显式控制 record 长度，避免尾部补点。

    pyedflib 默认 1 s/record，300 点 @200Hz 会被补成 400 点，
    第三方工具里时长多出 0.5 s。按 gcd(n, fs) 定每 record 样本数即可整除。
    """
    import math

    import pyedflib
    from pyedflib import highlevel

    cols = _read_csv_columns(csv_path)
    sigs, fs, chs = _eeg_signals(cols, fs_eeg)
    n = len(sigs[0])
    spr = math.gcd(n, max(1, round(fs)))
    with pyedflib.EdfWriter(str(out_path), n_channels=len(sigs),
                            file_type=file_type) as w:
        w.setDatarecordDuration(float(spr) / fs)
        w.setSignalHeaders(chs)
        w.setHeader(highlevel.make_header(patientname="aidsleep"))
        w.writeSamples(sigs)
    return out_path


def export_edf(csv_path: str | Path, out_path: str | Path, fs_eeg: float | None = None):
    """CSV -> EDF，第三方可用 EDFbrowser / MNE 打开验证。"""
    import pyedflib
    return _write_edf_family(csv_path, out_path, fs_eeg, pyedflib.FILETYPE_EDF)


def export_bdf(csv_path: str | Path, out_path: str | Path, fs_eeg: float | None = None):
    """CSV -> BDF（24-bit EEG 欧洲数据格式），合同 S2 ②要求 EDF/BDF 两种格式。

    必须手写 EdfWriter 而不是 highlevel.write_edf(file_type=BDF)：
    pyedflib 默认 1 s 一个 data record，300 点 @200Hz 会被补成 400 点，
    Homer/EDFbrowser 读到的时长比真实记录多 0.5 s。这里按样本数选 record 长度，
    使 记录数 × 每记录样本数 == 真实样本数，一点不多一点不少。
    """
    import pyedflib

    return _write_edf_family(csv_path, out_path, fs_eeg, pyedflib.FILETYPE_BDF)

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
                 subject_id: str = "aidsleep", probe: dict | None = None,
                 fs_eeg: float | None = None):
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
    # 规范: time 相对原点。旧记录的时间戳是"写盘时刻"，批量写盘时呈台阶，
    # 直接 t-t[0] 会让 Homer3 读到一串几乎重复的采样点；这里统一走台阶检测。
    t = _time_from_cols(cols, _fs_from_time(cols, fs_eeg))

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
            g.create_dataset("dataTypeIndex", data=np.int32(1))      # 规范必