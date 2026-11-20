"""离线自检：用程序内自造数据检查模块自洽性，不构成验收。"""
from __future__ import annotations
import sys, time, traceback
import numpy as np
from PyQt5 import QtWidgets, QtCore

class Worker(QtCore.QThread):
    line = QtCore.pyqtSignal(str, bool)
    def run(self):
        checks = [
            ("协议打包/解析回环（自造帧）", self.c_frame),
            ("指标算法 brain_relax（自造正弦）", self.c_metrics),
            ("质量判定函数（自造噪声/常数）", self.c_quality),
            ("事件与同步输出（纯函数调用）", self.c_events),
            ("丢包/复位计数（手编 seq 序列）", self.c_loss),
            ("对齐判定函数（传入固定数组）", self.c_sync),
            ("连号序号不丢包（逻辑检查，非 soak）", self.c_soak),
            ("EDF 写出/读回（自造正弦）", self.c_edf),
            ("XDF 写出/读回（自造随机数）", self.c_xdf),
            ("LSL 本机自环（不涉及设备）", self.c_lsl),
            ("64 通道布局（自造通道名）", self.c_64),
        ]
        for name, fn in checks:
            if name.startswith("LSL"):
                # 这一项要等本机 LSL 后端（resolve 5 s + pull 8 s），不提示的话
                # 前面的 9 行跑完就静止了，用户会以为"已经全部通过"。
                self.line.emit("… 正在等本机 LSL 后端，最长约 13 秒，请稍候", True)
            try:
                detail = fn()
                self.line.emit(f"[PASS·离线] {name}: {detail}", True)
            except Exception as e:
                self.line.emit(f"[FAIL·离线] {name}: {e}", False)
        self.line.emit("—— 以上均为离线自检，不读取设备数据，不构成 §十四 验收结论 ——", False)

    def c_frame(self):
        from device_frame import pack_frame, parse_stream
        b = pack_frame(3, 42, 12345, [1000, 2000, -3000, 4000], [[100, 200]] * 10, 1, 999)
        f = parse_stream(bytearray(b))[0]
        assert (f.device_id, f.seq, f.crc_ok) == (3, 42, True)
        return "dev3 seq42 crcOK"

    def c_metrics(self):
        from band_power import brain_relax_0_100
        v = float(brain_relax_0_100(np.sin(2 * np.pi * 10 * np.arange(1000) / 500.0)))
        assert v > 90, v
        return f"relax={v:.1f}"

    def c_quality(self):
        from quality import eeg_contact, fnirs_flags
        a = eeg_contact(np.random.randn(4, 500) * 20)
        b = fnirs_flags(np.full((10, 50), 1000.0))
        assert set(a) == {"好"} and set(b) == {"正常"}, (a, b)
        return "EEG好×4 fNIRS正常×10"

    def c_events(self):
        from core_events import EventBus, SyncOutGen
        b = EventBus(); b.soft_mark(); b.sync_in()
        g = SyncOutGen(1.0)
        assert g.poll(0.0) is False and g.poll(1.1) is True
        return "软标记+SYNC_IN+1Hz脉冲"

    def c_loss(self):
        from device_manager import DeviceManager
        m = DeviceManager(); m.connect(1); d = m.discover(1)
        [d.feed_seq(s) for s in [98, 99, 100, 0, 1, 2, 3, 4, 9]]
        assert d.lost == 4 and d.reboots == 1, (d.lost, d.reboots)
        return "lost=4 reboots=1"

    def c_sync(self):
        from lsl_sync import check_alignment
        ok, gap = check_alignment([0.2, 0.5, 0.9])
        assert ok
        return f"判定函数对固定数组返回 ok（间距 {gap:.1f}ms）——未取设备时间戳，不代表实测对齐"

    def c_soak(self):
        from device_manager import DeviceManager
        m = DeviceManager(); m.connect(1); d = m.discover(1)
        for i in range(1, 1501): d.feed_seq(i)
        assert d.lost == 0
        return "连号 1..1500 计数为 0 —— 只验计数逻辑，不代表实测丢包率"

    def c_edf(self):
        from pyedflib import highlevel
        n = 1000; t = np.arange(n) / 500.0
        sigs = [20 * np.sin(2 * np.pi * 10 * t + i) for i in range(4)]
        chs = [highlevel.make_signal_header(f"EEG{i+1}", dimension="uV",
                                            sample_frequency=500,
                                            physical_min=-500, physical_max=500)
               for i in range(4)]
        highlevel.write_edf("verify_center.edf", sigs, chs,
                            highlevel.make_header(patientname="aidsleep"))
        back, _, _ = highlevel.read_edf("verify_center.edf")
        assert len(back) == 4 and len(back[0]) == n
        return "4ch×1000回读一致"

    def c_xdf(self):
        from exporters import export_xdf
        import pyxdf
        export_xdf({"Aidsleep_EEG_1": (np.random.randn(4, 500).astype("float32"), 500.0, "EEG")},
                   "verify_center.xdf")
        s, _ = pyxdf.load_xdf("verify_center.xdf")
        assert len(s) == 1 and s[0]["time_series"].shape == (500, 4)
        return "pyxdf回读(500,4)"

    def c_lsl(self):
        import threading
        from lsl_outlet import TripleOutlet
        from pylsl import resolve_streams, StreamInlet
        o = TripleOutlet(device_id=99)
        assert o.live, "本机无pylsl后端"
        flag = {"stop": False}
        def push():
            i = 0
            while not flag["stop"]:
                o.push_sleep_eeg([float(i)] * 4); i += 1; time.sleep(0.02)
        t = threading.Thread(target=push, daemon=True); t.start()
        try:
            time.sleep(0.5)
            got = None
            for s in resolve_streams(5.0):
                if "Aidsleep_SleepEEG_99" in s.name():
                    inlet = StreamInlet(s, max_buflen=5); inlet.open_stream(5.0)
                    time.sleep(1.0)
                    got = inlet.pull_sample(timeout=8.0)
            assert got and got[0] is not None, "未收到采样"
            return f"收到{list(got[0])}"
        finally:
            flag["stop"] = True

    def c_64(self):
        from monitor_14ch import Monitor14
        w = Monitor14(eeg_names=[f"EEG{i+1}" for i in range(64)])
        assert len(w.plots) == 74, len(w.plots)
        w.close()
        return "74图布局正常"


class VerifyCenter(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("离线自检（非验收 · 不读取设备数据）")
        self.resize(720, 520)
        lay = QtWidgets.QVBoxLayout(self)
        tip = QtWidgets.QLabel(
            "⚠ 本页全部使用程序内自造数据（正弦波 / 随机数 / 自己组的帧 / 手编的序号），"
            "不连接任何设备、不读取任何串口字节。\n"
            "结果只能说明「代码内部模块互相认识、算法不抛异常」，"
            "不代表链路已通，也不构成 §十四 验收结论。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#b00020; background:#fff2f2; padding:8px; border:1px solid #e0b4b4;")
        lay.addWidget(tip)
        self.btn = QtWidgets.QPushButton("开始离线自检")
        self.btn.clicked.connect(self.start)
        self.log = QtWidgets.QListWidget()
        lay.addWidget(self.btn); lay.addWidget(self.log)
        self.worker = None

    def start(self):
        self.log.clear(); self.btn.setEnabled(False)
        self.worker = Worker(self)
        self.worker.line.connect(self.on_line)
        self.worker.finished.connect(lambda: self.btn.setEnabled(True))
        self.worker.start()

    def on_line(self, text: str, ok: bool):
        item = QtWidgets.QListWidgetItem(text)
        item.setForeground(QtCore.Qt.darkGreen if ok else QtCore.Qt.red)
        self.log.addItem(item)


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = VerifyCenter(); w.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
