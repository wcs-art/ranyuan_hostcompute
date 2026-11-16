"""B1 脱机门禁的解析单测: 8 条行为用例 + 1 条 CRC 公开向量。

全部只用合成字节, 不碰 socket / 不碰文件 / 不碰窗口 —— 出现这三样即不算单测。
"""
from __future__ import annotations

import pytest

from device_frame import (
    EEG_CH, EEG_GAIN_UV_PER_LSB, FNIRS_CH, FNIRS_WL,
    ads1299_uv_per_lsb, configure, crc16_modbus, electrode_uv_per_lsb,
    get_layout, pack_frame, parse_stream, set_layout, stream_stats,
)

# ---- 固定的合成帧内容。期望值手写在下面, 不调用被测实现来推算。[规格]
DEV = 1
SEED_EEG = [1000, -2000, 3000, -4000]          # 原始码值, 未乘增益
SEED_FNIRS = [[100 + c * 10, 200 + c * 10] for c in range(FNIRS_CH)]   # [10][2]

#: 单帧总长 [规格, 合同 F2] = 帧头2 + (dev1+ver1+n_eeg1+n_fnirs1+n_wl1+ratio1+flags1)
#:                          + seq4 + ts_mono4 + ts_abs8 + 4*3 + 10*2*3
#:                          + (ev_t1+ev_ts4) + CRC2 = 104
FRAME_LEN = 2 + 7 + 4 + 4 + 8 + EEG_CH * 3 + FNIRS_CH * FNIRS_WL * 3 + (1 + 4) + 2


def _one_frame(seq: int = 0, ts: int = 0, ev_t: int = 0, ev_ts: int = 0) -> bytes:
    return pack_frame(DEV, seq, ts, SEED_EEG, SEED_FNIRS, ev_t, ev_ts)


@pytest.fixture(autouse=True)
def _isolate_decoder_registry():
    """每个用例前清空按 id(buf) 缓存的解码器。

    bytearray 不支持 weakref, 解码器只能按 id(buf) 存活; 上一个用例的缓冲被回收后
    id 会被新缓冲复用, 于是旧解码器的 resync/handoff 计数会漏进下一个用例,
    表现为"单独跑绿、全量跑红"的偶发失败。生产里缓冲是长生命周期的, 不受影响,
    但测试必须隔离。
    """
    import device_frame as dfm

    dfm._DECODERS.clear()
    yield
    dfm._DECODERS.clear()


# 1 -----------------------------------------------------------------------
def test_pack_parse_roundtrip():
    """单帧往返: seq / 时间戳 / 事件 原样回来, EEG 按增益折算。[规格]"""
    buf = bytearray(_one_frame(seq=7, ts=1234, ev_t=3, ev_ts=5678))
    frames = parse_stream(buf)

    assert len(frames) == 1
    f = frames[0]
    assert (f.device_id, f.seq, f.timestamp_ms) == (DEV, 7, 1234)
    assert (f.event_type, f.event_ts) == (3, 5678)
    assert f.crc_ok is True
    # 1000 码值 x 0.022 uV/LSB = 22.0 uV, 符号位要正确
    assert f.eeg_uv[0] == pytest.approx(1000 * EEG_GAIN_UV_PER_LSB)
    assert f.eeg_uv[1] == pytest.approx(-2000 * EEG_GAIN_UV_PER_LSB)
    # fNIRS 保留原始码值, 不做任何折算 [规格: 原始码值随 CSV 交付, 甲方自行复算]
    assert f.fnirs_raw[0] == SEED_FNIRS[0]
    assert buf == b"", "整帧消费完后缓冲必须为空"


# 2 -----------------------------------------------------------------------
def test_multiple_frames_in_one_chunk():
    """一个 recv 里到多帧, 必须全部切出来且顺序不乱。[规格]"""
    buf = bytearray(b"".join(_one_frame(seq=i) for i in range(3)))
    frames = parse_stream(buf)
    assert [f.seq for f in frames] == [0, 1, 2]
    assert buf == b""


# 3 -----------------------------------------------------------------------
def test_partial_tail_retained():
    """半帧必须留在缓冲里, 补齐后能解出 —— 这是跨 recv 边界的基本要求。[规格]"""
    full = _one_frame(seq=9)
    buf = bytearray(full[:40])
    assert parse_stream(buf) == []
    assert len(buf) == 40, "半帧不得被丢弃"

    buf += full[40:]
    frames = parse_stream(buf)
    assert [f.seq for f in frames] == [9]


# 4 -----------------------------------------------------------------------
def test_crc_error_flagged():
    """CRC 错时必须把 crc_ok 置 False, 而不是抛异常或静默通过。[规格]"""
    raw = bytearray(_one_frame(seq=5))
    raw[12] ^= 0xFF                      # 改 body 里一个字节
    frames = parse_stream(raw)
    assert len(frames) == 1
    assert frames[0].crc_ok is False


# 5 -----------------------------------------------------------------------
def test_head_split_across_chunks():
    """帧头跨包: 前一包以 0xAA 结尾, 后一包以 0x55 开头, 仍须解出整帧。[规格]"""
    full = _one_frame(seq=11)
    buf = bytearray(full[:1])            # 只有帧头前半
    assert parse_stream(buf) == []
    buf += full[1:]
    frames = parse_stream(buf)
    assert [f.seq for f in frames] == [11]


# 6 -----------------------------------------------------------------------
def test_frame_len_matches_contract_v1():
    """[规格] 帧长由合同 F2 的字段清单推出, 不再有"没有长度字段、没有版本号"的现状。

    v1 = 104 字节: 合同要求"数据项须同时携带绝对时间戳", 旧 90 字节装不下;
    同时帧头加了通道数字段(EV2 6ch 靠它配置)。
    这里断言具体数字, 是为了让"改协议"必须显式改这条测试,
    而不是让固件悄悄加字段、上位机静默解析错位。
    """
    assert len(_one_frame()) == FRAME_LEN == 104
    assert get_layout().frame_len == 104


# 6b ----------------------------------------------------------------------
def test_abs_timestamp_carried_alongside_monotonic():
    """[规格, 合同 F2] 绝对时间戳与上电归零的单调计数必须并行输出。"""
    buf = bytearray(pack_frame(DEV, 1, 840, SEED_EEG, SEED_FNIRS,
                               ts_abs=1_763_000_000_123))
    f = parse_stream(buf)[0]
    assert f.timestamp_ms == 840, "上电单调计数丢了"
    assert f.timestamp_abs_ms == 1_763_000_000_123, "绝对时间戳丢了"
    assert f.header_ok is True


# 6c ----------------------------------------------------------------------
def test_header_declares_channel_counts():
    """[规格, 合同 F2] 帧头必须自报通道数, 与配置不符要显式标记, 不能静默。"""
    raw = bytearray(_one_frame())
    f = parse_stream(raw)[0]
    assert (f.proto_ver, f.ratio) == (1, 1)

    raw2 = bytearray(_one_frame())
    raw2[4] = 6                                   # 帧头谎报 6 通道 EEG
    f2 = parse_stream(raw2)[0]
    assert f2.header_ok is False, "帧头通道数与配置不符时必须被发现"


# 6d ----------------------------------------------------------------------
def test_gain_scales_eeg_uv():
    """[规格, 合同 F2] 折算系数随 ADS1299 增益联动, 且已除前置 1000 折到电极输入端。"""
    try:
        configure(gain=12, vref=4.5)
        buf = bytearray(_one_frame())
        f = parse_stream(buf)[0]
        assert f.eeg_uv[0] == pytest.approx(1000 * electrode_uv_per_lsb(12, 4.5))
        # 24 档的 2 倍 (gain 减半 -> 每 LSB 的电压翻倍)
        assert f.eeg_uv[0] == pytest.approx(2 * 1000 * electrode_uv_per_lsb(24, 4.5))
    finally:
        configure(gain=24, vref=4.5, preamp_gain=1000.0)
    assert ads1299_uv_per_lsb() == pytest.approx(0.0223517, rel=1e-5)


def test_eeg_uv_referred_to_electrode_not_adc_pin():
    """[规格, 合同 F2/A3] 显示值一律折算到**电极输入端**: 必须除以前置 1000。

    信号链是 电极 -> INA333(G=1000) -> ADS1299。若漏除前置增益, 50 µV 的 EEG
    会显示成 50000 µV —— 波形形状还对, 幅度全错, 且不报任何错。
    """
    configure(gain=24, vref=4.5, preamp_gain=1000.0)
    try:
        buf = bytearray(_one_frame())
        f = parse_stream(buf)[0]
        # 电极端 µV/LSB = ADC 端 ÷ 1000
        assert electrode_uv_per_lsb() == pytest.approx(ads1299_uv_per_lsb() / 1000.0)
        assert f.eeg_uv[0] == pytest.approx(SEED_EEG[0] * ads1299_uv_per_lsb() / 1000.0)
        # 满量程核对: ±(VREF/gain)/1000 = ±187.5 µV, 能容纳 EEG 与眨眼伪迹
        fs_uv = (4.5 / 24.0) * 1e6 / 1000.0
        assert 2**23 * electrode_uv_per_lsb() == pytest.approx(fs_uv, rel=1e-6)
    finally:
        configure(gain=24, vref=4.5, preamp_gain=1000.0)


# 6e ----------------------------------------------------------------------
def test_compat90_layout_still_parsable():
    """[兼容] 固件若仍是旧 90 字节布局, 切 profile 即可, 不用改代码。"""
    try:
        set_layout("compat90")
        assert get_layout().frame_len == 90
        buf = bytearray(pack_frame(DEV, 3, 99, SEED_EEG, SEED_FNIRS))
        assert len(buf) == 90
        f = parse_stream(buf)[0]
        assert (f.seq, f.timestamp_ms, f.crc_ok) == (3, 99, True)
        assert f.timestamp_abs_ms == 0, "旧布局没有绝对时间戳, 应为 0 而不是编一个"
    finally:
        set_layout("v1")
    assert get_layout().frame_len == 104


# 7 -----------------------------------------------------------------------
def test_garbage_before_head_skipped():
    """帧头前的垃圾字节应被跳过, 之后的帧照常解析。[规格]"""
    buf = bytearray(b"\x01\x02\x03" + _one_frame(seq=21))
    frames = parse_stream(buf)
    assert [f.seq for f in frames] == [21]


# 8 -----------------------------------------------------------------------
def test_seq_32bit_boundary():
    """seq 是 4 字节无符号, 0xFFFFFFFE -> 0xFFFFFFFF 必须原样保真。[规格: 丢包率靠 seq 连续性]"""
    buf = bytearray(_one_frame(seq=0xFFFFFFFE) + _one_frame(seq=0xFFFFFFFF))
    frames = parse_stream(buf)
    assert [f.seq for f in frames] == [0xFFFFFFFE, 0xFFFFFFFF]
    # 顺带锁定丢包计算的输入口径: 相邻两帧差 1
    assert frames[1].seq - frames[0].seq == 1


# 9 -----------------------------------------------------------------------
def test_crc16_modbus_reference_value():
    """CRC16/MODBUS 用公开的参考向量, 不用实现自身算期望。[规格]"""
    # 公开向量: b"123456789" 的 CRC-16/MODBUS = 0x4B37
    assert crc16_modbus(b"123456789") == 0x4B37


# ---- B1.1 重同步可观测性 --------------------------------------------------
# 10 ----------------------------------------------------------------------
def test_resync_count_accumulates_across_calls():
    """切片器必须跨调用存活: 两批垃圾前缀, 第二次后 resync_count 必须严格递增。

    旧实现每次新建 FrameSlicer, 两个值都会是 1/3 —— 重同步永远是"刚发生一次",
    于是丢包率恒 0, 现场线缆接触不良看不出来。[规格]
    """
    buf = bytearray(b"\x01\x02\x03")
    assert parse_stream(buf) == []
    first = stream_stats(buf)
    assert (first["resync_count"], first["resync_bytes"]) == (1, 3), "首批垃圾应被记为一次重同步"

    buf += b"\x04\x05\x06"
    assert parse_stream(buf) == []
    second = stream_stats(buf)

    assert second["resync_count"] > first["resync_count"], "resync_count 未跨调用累积"
    assert second["resync_bytes"] > first["resync_bytes"], "resync_bytes 未跨调用累积"
    assert (second["resync_count"], second["resync_bytes"]) == (2, 6)


# 11 ----------------------------------------------------------------------
def test_caller_clear_after_half_frame_is_observable():
    """调用方在两次 parse 之间 clear() 半帧镜像: 必须可观测, 不许静默。

    旧实现 buf[:] = slicer.pending 把半帧唯一副本放在调用方缓冲里, clear() 后这
    1~89 字节(≥ 一整帧)无声消失, 计数器也看不出。现在半帧归解码器所有, 调用方丢了
    镜像就作废并计数(handoff_dropped / dropped_half_bytes), 且流不卡死。[规格]
    """
    full = _one_frame(seq=42)
    buf = bytearray(full[:40])
    assert parse_stream(buf) == []
    before = stream_stats(buf)
    assert before["pending"] == 40, "半帧应由解码器持有"

    buf.clear()                                  # 缺陷场景: 半帧镜像被清
    buf += full[40:]
    parse_stream(buf)
    after = stream_stats(buf)

    assert after["handoff_dropped"] == before["handoff_dropped"] + 1, "clear() 丢半帧必须计数"
    assert after["dropped_half_bytes"] == before["dropped_half_bytes"] + 40, "丢掉的字节数必须可算"

    # 流不得被卡死: 紧接一整帧照常量解析
    buf += _one_frame(seq=43)
    assert [f.seq for f in parse_stream(buf)] == [43]


# 12 ----------------------------------------------------------------------
def test_single_byte_feed_600_frames():
    """B1 回归: 逐字节喂 600 帧(54000 次调用)仍须一帧不少, seq 连续。[规格]"""
    data = b"".join(_one_frame(seq=i) for i in range(600))
    buf = bytearray()
    got = []
    for b in data:
        buf.append(b)
        got.extend(parse_stream(buf))

    assert len(got) == 600, f"逐字节喂入丢了帧: 只出 {len(got)} 帧"
    assert [f.seq for f in got] == list(range(600))
    assert all(f.crc_ok for f in got)
    assert stream_stats(buf)["resync_count"] == 0, "纯净字节流不该产生重同步"


# 13 ----------------------------------------------------------------------
def test_decoder_state_isolated_per_buffer():
    """两个缓冲的状态互不串台: 各自的半帧与计数独立。[规格]"""
    a = bytearray(_one_frame(seq=1)[:40])
    b = bytearray(_one_frame(seq=2)[:50])
    assert parse_stream(a) == []
    assert parse_stream(b) == []

    assert stream_stats(a)["pending"] == 40
    assert stream_stats(b)["pending"] == 50
    assert stream_stats(a)["resync_count"] == 0
    assert stream_stats(b)["resync_count"] == 0

    a += _one_frame(seq=1)[40:]
    assert [f.seq for f in parse_stream(a)] == [1]
    assert stream_stats(b)["pending"] == 50, "喂 a 不得动到 b 的半帧"


# 14 ----------------------------------------------------------------------
def test_decoder_registry_is_bounded():
    """状态表不得无限增长: 一次性缓冲反复创建后, 表长仍受容量上限约束。[规格]"""
    import device_frame as dfm

    for _ in range(64):
        parse_stream(bytearray(_one_frame(seq=3)))
    assert len(dfm._DECODERS) <= dfm._DECODER_CAPACITY


# 15 ----------------------------------------------------------------------
def test_v1r5_layout_is_152_bytes_and_keeps_all_samples():
    """[规格, 合同 F2] EEG 500 SPS / fNIRS 100 SPS -> 一帧 5 个 EEG 样点, 帧长 152 B。

    合同 F2 要求"EEG 与 fNIRS 采样率不同, 帧内复用方式须明确"。这条把复用方式
    钉在代码里: ratio=5, 20 个 EEG 样点(5 时点 × 4 通道)必须全部解出来, 不能只留最后一个。
    """
    try:
        set_layout("v1r5")
        ly = get_layout()
        # 25(头) + 5*4*3(EEG) + 10*2*3(fNIRS) + 5(event) + 2(CRC)
        assert ly.frame_len == 152
        assert ly.eeg_bytes == 60 and ly.fnirs_bytes == 60

        # 时点优先: t0c1..t0c4, t1c1..t1c4, ...
        eeg20 = [100 * k for k in range(1, 21)]
        raw = bytearray(pack_frame(DEV, 7, 1234, eeg20, SEED_FNIRS))
        assert len(raw) == 152

        f = parse_stream(raw)[0]
        assert (f.device_id, f.seq, f.ratio, f.crc_ok, f.header_ok) == (DEV, 7, 5, True, True)
        assert len(f.eeg_uv_all) == 20, "一帧 5 个时点 × 4 通道的样点必须全保留"
        # 单点管线(界面/CSV)取本帧最后一个时点
        assert len(f.eeg_uv) == EEG_CH
        assert f.eeg_raw[-EEG_CH:] == eeg20[-EEG_CH:]
    finally:
        set_layout("v1")
    assert get_layout().frame_len == 104
