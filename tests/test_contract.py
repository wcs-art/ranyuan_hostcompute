"""数据契约的守卫测试。

这些测试保护的不是"算得对不对", 而是"形状对不对" ——
形状错位的代价比数值错更大: 数值错会报错, 形状错会静默写出一份看起来正常的错文件。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import contract

SRC = Path(__file__).resolve().parent.parent / "ev1" / "monitor_14ch.py"


def _push_frame_params() -> list[str]:
    """用 ast 从源码取 push_frame 的形参名, 不 import PyQt(测试保持零依赖、可离线跑)。"""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "push_frame":
            a = node.args
            names = [x.arg for x in (a.posonlyargs + a.args)]
            if names and names[0] == "self":
                names = names[1:]
            names += [x.arg for x in a.kwonlyargs]
            return names
    raise AssertionError(f"在 {SRC} 里没找到 push_frame")


def _sample_frame() -> contract.Frame:
    return contract.Frame(
        device_id=1, seq=42, t_device_ms=840, t_host_s=1.68,
        eeg_uv=[1.1, 2.2, 3.3, 4.4],
        fnirs_raw=[[100 + c, 200 + c] for c in range(contract.N_FNIRS)],
        fnirs_intensity=None, od=None, hbo=None, hbr=None,
        acc_xyz=[0.0, 0.0, 1.0], gyro_xyz=[0.0, 0.0, 0.0],
        battery_pct=88.0, loss_pct=0.0, event_type=0, event_ts=0, crc_ok=True,
    )


# 1 -----------------------------------------------------------------------
def test_ui_dict_keys_match_push_frame_params():
    """frame_to_ui_dict 的键 == push_frame 的形参名。逐一对齐, 不多不少。

    这条测试存在的理由: ui/monitor_14ch.py:260 的 docstring 约定 8 元组第 8 位是 battery,
    而 :199 的形参第 6 位才是 battery, :281 又是按位置 *fr 展开 ——
    acc 会被当成 battery 传进去。改成 dict 按名传参后, 错位在构造上不可能发生;
    本条测试把它钉死: 谁改了形参名或改了键, 立刻红。
    """
    params = _push_frame_params()
    keys = list(contract.frame_to_ui_dict(_sample_frame()).keys())
    assert set(keys) == set(params), (
        f"键与形参不匹配\n  多出来的键: {sorted(set(keys) - set(params))}\n"
        f"  没覆盖的形参: {sorted(set(params) - set(keys))}"
    )


# 2 -----------------------------------------------------------------------
@pytest.mark.parametrize("optional,extra", [(False, False), (True, False), (False, True), (True, True)])
def test_row_length_always_matches_header(optional, extra):
    """四种开关组合下, 行长度恒等于表头长度。少一列会让整份 CSV 静默错位。"""
    header = contract.csv_header(optional=optional, extra=extra)
    row = contract.frame_to_row(_sample_frame(), optional=optional, extra=extra)
    assert len(row) == len(header) == len(set(header)), "行/表头长度不一致, 或表头有重名列"


# 3 -----------------------------------------------------------------------
def test_uncalibrated_written_as_empty_not_zero():
    """未标定的光强必须写空串, 不能写 0.0。

    [规格] 0.0 是一个合法光强值, 写进去下游会算出看起来合理的错结果;
    空串才能表达"这一格没有有效数据"。这是交接文档第七节第 4 条的具体落地。
    """
    fr = _sample_frame()
    fr.fnirs_intensity = None
    row = contract.frame_to_row(fr)
    i = contract.csv_header().index("fnirs1_760_intensity")
    assert row[i] == "", "未标定被写成了数值"


# 4 -----------------------------------------------------------------------
def test_required_columns_have_no_out_of_scope_fields():
    """S1~S5 必需列里不得出现 S8 姿态 / S9 指标。

    交付范围只有 S1~S5。表头一旦出现 focus / accel, 等于向甲方承诺了范围外的交付,
    评审时会被问"这些列怎么没数据"。
    """
    required = set(contract.REQUIRED_COLUMNS)
    leaked = required & set(contract.EXTRA_COLUMNS)
    assert leaked == set(), f"范围外字段泄漏进必需列: {sorted(leaked)}"


# 5 -----------------------------------------------------------------------
def test_required_columns_include_intensity_block():
    """S2 明确要求 CSV 含"换算光强/光功率密度", 共 20 列(10 通道 x 2 波长)。

    原 CSV_HEADER 缺这 20 列, 是 S2 验收的硬缺口, 此处钉死。
    """
    required = contract.REQUIRED_COLUMNS
    inten = [c for c in required if c.endswith("_intensity")]
    assert len(inten) == contract.N_FNIRS * len(contract.FNIRS_WLS) == 20
    raw = [c for c in required if c.endswith("_raw")]
    assert len(raw) == 20


# 6 -----------------------------------------------------------------------
def test_three_column_groups_are_disjoint():
    """必需 / 可选 / 范围外 三组不得重叠, 否则同一列会出现两次。"""
    req = set(contract.REQUIRED_COLUMNS)
    opt = set(contract.OPTIONAL_S2_COLUMNS)
    ext = set(contract.EXTRA_COLUMNS)
    assert req & opt == set()
    assert req & ext == set()
    assert opt & ext == set()


# 7 -----------------------------------------------------------------------
def test_default_header_is_required_only():
    """默认表头 = 只含必需列。范围外的 10 列必须显式 --extra-cols 才出现。"""
    assert contract.csv_header() == contract.REQUIRED_COLUMNS
    assert len(contract.csv_header(extra=True)) == len(contract.REQUIRED_COLUMNS) + 10


# 8 -----------------------------------------------------------------------
def test_from_protocol_preserves_device_clock():
    """from_protocol 必须原样保留设备时间戳, 不得用上位机时间替换。

    core_events.py:7 用本机 monotonic 冒充设备时钟, 与帧内 timestamp_ms 形成双时基。
    契约层不许再掺一次: 两个时间字段分开存, 换算只发生在 clock_mapper(尚未实现)。
    """
    from device_frame import DataFrame

    df = DataFrame(device_id=3, seq=99, timestamp_ms=777,
                   eeg_uv=[1.0, 2.0, 3.0, 4.0], fnirs_raw=[[1, 2]] * 10,
                   event_type=1, event_ts=555, crc_ok=True)
    fr = contract.from_protocol(df)
    assert (fr.device_id, fr.seq, fr.t_device_ms) == (3, 99, 777)
    assert (fr.event_type, fr.event_ts) == (1, 555)
    assert fr.crc_ok is True


# 9 -----------------------------------------------------------------------
def test_abs_timestamp_column_is_empty_when_device_not_synced():
    """[规格, 合同 F2] 未对时时 time_abs_ms 写空串, 不写 0。

    0 是一个合法的时间戳值(1970-01-01), 写进去下游会当成"对过时但时间是 0";
    空串才表达"设备还没对时"。与"未标定光强写空"同一口径。
    """
    i = contract.csv_header().index("time_abs_ms")
    fr = _sample_frame()
    fr.t_abs_ms = 0
    assert contract.frame_to_row(fr)[i] == ""
    fr.t_abs_ms = 1_763_000_000_123
    assert contract.frame_to_row(fr)[i] == 1_763_000_000_123


# 10 ----------------------------------------------------------------------
def test_unnest_raw_roundtrip():
    """平铺 20 列 <-> [10][2] 必须互逆, 否则界面写 CSV 会把通道顺序打乱。"""
    flat = [float(i) for i in range(20)]
    nested = contract.unnest_raw(flat)
    assert len(nested) == contract.N_FNIRS
    assert len(nested[0]) == len(contract.FNIRS_WLS)
    assert contract._flatten_raw(nested) == flat


# 11 ----------------------------------------------------------------------
def test_extra_metrics_passthrough():
    """S9 指标列: 界面给了值就写值, 没给写空 —— 不编占位值。"""
    header = contract.csv_header(extra=True)
    i = header.index("focus")
    fr = _sample_frame()
    row = contract.frame_to_row(fr, extra=True, metrics={"focus": 61.5})
    assert row[i] == 61.5
    row2 = contract.frame_to_row(fr, extra=True)
    assert row2[i] == ""
