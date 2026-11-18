"""S2 导出验收: 拿一份 CSV, 导四种格式并回读校验, 打印 PASS/FAIL。

用途: 交付/验收时现场跑一遍, 证明 EDF / BDF / SNIRF / XDF 都能被第三方读出来。
不需要装 EDFbrowser 或 Homer3, 用 pyedflib / h5py / pyxdf 做结构性回读;
本机若装了 mne(--mne), 再用 MNE 独立读一遍 EDF 与 SNIRF(更接近甲方验法)。

用法:
    python tools/verify_exports.py sleep_record_20261112_180000.csv
    python tools/verify_exports.py xxx.csv --out build/_accept --mne
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ev1"))
sys.path.insert(0, str(ROOT))

OK = "PASS"
NG = "FAIL"


def _p(tag: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{OK if ok else NG}] {tag}{('  ' + detail) if detail else ''}")
    return ok


def check_edf(path: Path, tag: str = "EDF ") -> bool:
    from pyedflib import highlevel

    sig, hdr, _ = highlevel.read_edf(str(path))
    n = len(sig)
    nsamp = len(sig[0]) if n else 0
    fs = hdr[0]["sample_frequency"] if n else 0
    print(f"{tag} {path.name}  {n} 通道 × {nsamp} 点 @ {fs} Hz")
    good = True
    good &= _p("通道数为 4", n == 4, f"实际 {n}")
    good &= _p("每通道点数一致", all(len(s) == nsamp for s in sig))
    good &= _p("采样率 > 0", fs > 0, f"{fs} Hz")
    # 旧断言写的是 abs(nsamp/fs - nsamp/fs) < 1e-6 —— 恒真，等于没验。
    # pyedflib 的 header 不带 record 数，这里只报可计算时长，不假称已校验。
    good &= _p("时长可计算", fs > 0 and nsamp > 0,
               f"{nsamp / fs:.2f} s" if fs else "无采样率")
    return bool(good)


def check_snirf(path: Path) -> bool:
    import h5py

    with h5py.File(path, "r") as f:
        keys = list(f.keys())
        print(f"SNIRF {path.name}  根组 {keys}")
        good = _p("根组名为 nirs(SNIRF 1.0 规范)", "nirs" in keys, str(keys))
        if not good:
            return False
        d = f["nirs/data1"]
        ts = d["dataTimeSeries"]
        ml = [k for k in d.keys() if k.startswith("measurementList")]
        good &= _p("dataTimeSeries 二维", ts.ndim == 2, str(ts.shape))
        good &= _p("measurementList 数 = 通道数", len(ml) == ts.shape[1],
                   f"{len(ml)} vs {ts.shape[1]}")
        good &= _p("time 从 0 起算", float(d["time"][0]) == 0.0,
                   str(float(d["time"][0])))
        good &= _p("wavelengths 存在", "wavelengths" in f["nirs/probe"])
        good &= _p("metaDataTags 六项齐全",
                   {"SubjectID", "MeasurementDate", "MeasurementTime",
                    "LengthUnit", "TimeUnit", "FrequencyUnit"} <= set(f["nirs/metaDataTags"].keys()))
        return bool(good)


def check_xdf(path: Path) -> bool:
    import pyxdf

    data, _ = pyxdf.load_xdf(str(path))
    names = [s["info"]["name"][0] for s in data]
    print(f"XDF  {path.name}  {len(data)} 流 {names}")
    good = _p("至少 1 个流", len(data) >= 1, str(len(data)))
    good &= _p("每个流有通道数与标称采样率",
               all("channel_count" in s["info"] and "nominal_srate" in s["info"] for s in data))
    return bool(good)


def check_mne(edf: Path, snirf: Path) -> bool:
    try:
        import mne
    except Exception as e:                                    # noqa: BLE001
        print(f"  [跳过] 没装 mne: {e}")
        return True
    good = True
    try:
        raw = mne.io.read_raw_edf(str(edf), preload=True, verbose="ERROR")
        print(f"MNE 读 EDF: {raw.info['sfreq']} Hz, {len(raw.ch_names)} 通道")
        good &= _p("MNE 能读 EDF", len(raw.ch_names) == 4)
    except Exception as e:                                    # noqa: BLE001
        good &= _p("MNE 能读 EDF", False, str(e))
    try:
        raw = mne.io.read_raw_snirf(str(snirf), preload=True, verbose="ERROR")
        print(f"MNE 读 SNIRF: {raw.info['sfreq']} Hz, {len(raw.ch_names)} 通道")
        good &= _p("MNE 能读 SNIRF", len(raw.ch_names) == 20, f"实际 {len(raw.ch_names)}")
    except Exception as e:                                    # noqa: BLE001
        good &= _p("MNE 能读 SNIRF", False, str(e))
    return bool(good)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="S2 导出验收")
    ap.add_argument("csv", help="记录得到的 CSV")
    ap.add_argument("--out", default="build/_accept", help="输出目录")
    ap.add_argument("--mne", action="store_true", help="额外用 MNE 独立回读(需本机有 mne)")
    a = ap.parse_args(argv)

    csv_path = Path(a.csv)
    if not csv_path.exists():
        print(f"找不到 {csv_path}")
        return 2
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    from exporters import export_bdf, export_edf, export_snirf, export_xdf_from_csv

    stem = csv_path.stem
    edf, bdf = out / f"{stem}.edf", out / f"{stem}.bdf"
    snirf, xdf = out / f"{stem}.snirf", out / f"{stem}.xdf"

    print(f"源: {csv_path}")
    print("导出中…")
    export_edf(csv_path, edf)
    export_bdf(csv_path, bdf)
    export_snirf(csv_path, snirf)
    # XDF 直接用这份 CSV 的全部行，不再用 200 点占位数据冒充记录
    export_xdf_from_csv(csv_path, xdf)

    print()
    all_ok = (check_edf(edf) & check_edf(bdf, "BDF") &
              check_snirf(snirf) & check_xdf(xdf))
    if a.mne:
        print()
        all_ok = check_mne(edf, snirf) & all_ok

    print()
    print("结论:", "全部通过" if all_ok else "有项目未通过")
    print("产物:", out.resolve())
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
