"""字节流切帧: 只把字节流切成完整的帧字节块, 不解析字段、不算 CRC。
"""
from __future__ import annotations
class FrameSlicer:

    def __init__(self, frame_len: int, head: bytes = b"\xAA\x55") -> None:
        head = bytes(head)
        if not head:
            raise ValueError("head 不能为空")
        if frame_len <= len(head):
            raise ValueError("frame_len 必须大于帧头长度")
        self._frame_len = frame_len
        self._head = head
        self._buf = bytearray()
        #: 因帧头错位而丢弃的字节数与次数(可观测的重同步代价)
        self.resync_bytes = 0
        self.resync_count = 0

    @property
    def frame_len(self) -> int:
        return self._frame_len

    @property
    def pending(self) -> bytes:
        """内部仍保留的残余字节(不足一帧, 或可能是半个帧头)。"""
        return bytes(self._buf)

    def _drop(self, n: int) -> None:
        if n <= 0:
            return
        del self._buf[:n]
        self.resync_bytes += n
        self.resync_count += 1

    def _partial_head_len(self) -> int:
        """buf 尾部有多长可能是被切断的帧头, 这段不能丢。"""
        limit = min(len(self._buf), len(self._head) - 1)
        for k in range(limit, 0, -1):
            if self._buf[-k:] == self._head[:k]:
                return k
        return 0

    def feed(self, data: bytes) -> list[bytes]:
        """喂入任意长度字节, 返回本次切出的完整帧(按到达顺序)。"""
        if data:
            self._buf += data
        frames: list[bytes] = []
        while True:
            i = self._buf.find(self._head)
            if i < 0:
                # 没有帧头: 只留可能是半个帧头的尾巴, 其余算垃圾
                keep = self._partial_head_len()
                self._drop(len(self._buf) - keep)
                break
            if i > 0:
                self._drop(i)
            if len(self._buf) < self._frame_len:
                break
            frames.append(bytes(self._buf[: self._frame_len]))
            del self._buf[: self._frame_len]
        return frames

    def reset(self) -> None:
        """丢光残留(断链/重连时用), 计数器保留。"""
        self._buf.clear()
