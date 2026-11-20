"""最小XDF v1写出器(纯python, 无第三方依赖), 写出文件可被 pyxdf.load_xdf / MoBI 读取.
对照要求§三S2-XDF(含时钟校正信息)+§八S7. 格式依据pyxdf reader实现:
magic XDF: + chunk[不定长长度 tag-u16 (stream_id-u32) payload], tag:1文件头 2流头 3采样 4时钟偏移 6流尾."""
from __future__ import annotations
import struct
from pathlib import Path

def _varlen(n: int) -> bytes:
    if n < 256: return b"\x01" + struct.pack("B", n)
    if n < 2**32: return b"\x04" + struct.pack("<I", n)
    return b"\x08" + struct.pack("<Q", n)

def _chunk(tag: int, stream_id: int | None, payload: bytes) -> bytes:
    body = struct.pack("<H", tag)
    if stream_id is not None: body += struct.pack("<I", stream_id)
    body += payload
    return _varlen(len(body)) + body

_HDR = '<?xml version="1.0"?><info><version>1.0</version></info>'

def _stream_xml(name: str, stype: str, nch: int, fs: float, fmt: str, labels: list[str]) -> bytes:
    chs = "".join(f"<channel><label>{l}</label></channel>" for l in labels)
    return (f'<?xml version="1.0"?><info><name>{name}</name><type>{stype}</type>'
            f"<channel_count>{nch}</channel_count><nominal_srate>{fs}</nominal_srate>"
            f"<channel_format>{fmt}</channel_format><desc><channels>{chs}</channels></desc></info>").encode()

def write_xdf(path: str | Path, streams: dict, clock_offsets: dict | None = None) -> Path:
    """streams={name:(data[ch,time] float32, fs, type)}; clock_offsets={name:[(t,off)..]}."""
    import numpy as np
    out = Path(path); out.parent.mkdir(parents=True, exist_ok=True)
    buf = bytearray(b"XDF:")
    buf += _chunk(1, None, _HDR.encode())
    for sid, (name, val) in enumerate(streams.items(), start=1):
        data, fs, stype = val[:3]
        # 第 4 位是调用方给的真实起始时间戳(s)。没有就明确用合成时间戳，
        # 不能再把 1000+sid 当成"设备时间"——下游拿它对齐会整体错开。
        ts0 = float(val[3]) if len(val) > 3 else 1000.0 + sid
        data = np.asarray(data, dtype=np.float32)
        nch, n = data.shape
        labels = [f"ch{i+1}" for i in range(nch)]
        buf += _chunk(2, sid, _stream_xml(name, stype, nch, fs, "float32", labels))
        step = 1.0/fs if fs > 0 else 0.02
        samp = bytearray(_varlen(n))
        for k in range(n):
            samp += b"\x01" + struct.pack("<d", ts0 + k*step)
            samp += data[:, k].astype("<f4").tobytes()
        buf += _chunk(3, sid, bytes(samp))
        # 只有调用方真的给了校正信息才写 tag=4；静默写 0 等于谎报"已对时"。
        for t, off in (clock_offsets or {}).get(name, []):
            buf += _chunk(4, sid, struct.pack("<dd", t, off))
        foot = (f'<?xml version="1.0"?><info><first_timestamp>{ts0}</first_timestamp>'
                f"<last_timestamp>{ts0+(n-1)*step}</last_timestamp><sample_count>{n}</sample_count></info>").encode()
        buf += _chunk(6, sid, foot)
    out.write_bytes(bytes(buf))
    return out
