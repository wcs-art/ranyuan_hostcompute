"""回放已存的 CSV：按原采样率推送到 14 通道波形和姿态、指标面板。"""
from __future__ import annotations
import sys, csv
import numpy as np
from PyQt5 import QtWidgets, QtCore

class CsvReplayer(QtCore.QObject):
    tick = QtCore.pyqtSignal(dict)
    def __init__(self, path: str, speed: float = 1.0):
        super().__init__()
        self.rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
        self.i = 0; self.speed = speed
        self.timer = QtCore.QTimer(); self.timer.timeout.connect(self._step)
    def start(self, interval_ms: int = 2):
        self.timer.start(max(1, int(interval_ms/self.speed)))
    def _step(self):
        if self.i >= len(self.rows): self.timer.stop(); return
        self.tick.emit(self.rows[self.i]); self.i += 1

def open_replay_windows(path: str, parent=None):
    """给主界面调用: 复用已有QApplication, 返回窗体列表(由调用方持有)."""
    from monitor_14ch import Monitor14
    from panels import MotionPanel, MetricsPanel
    mon = Monitor14(); mot = MotionPanel(); met = MetricsPanel()
    for w in (mon, mot, met): w.show()
    rp = CsvReplayer(path)
    def _on(row: dict):
        try:
            eeg = [float(row[f"eeg{i+1}_uv"]) for i in range(4)]
            hbo = [float(row.get("hbO", 0))]*10; hbr = [float(row.get("hbR", 0))]*10
            mon.push_frame(int(float(row.get("seq", 0))), eeg, hbo, hbr,
                           acc_xyz=[float(row.get(k, 0)) for k in ("accel_x","accel_y","accel_z")],
                           gyro_xyz=[float(row.get(k, 0)) for k in ("gyro_x","gyro_y","gyro_z")])
            mot.push(np.array([float(row.get(k, 0)) for k in ("accel_x","accel_y","accel_z")]),
                     np.array([float(row.get(k, 0)) for k in ("gyro_x","gyro_y","gyro_z")]))
            met.push(float(row.get("brain_active",50)), float(row.get("brain_relax",50)),
                     float(row.get("focus",50)), float(row.get("hr_bpm",70))/100*100,
                     float(row.get("hr_bpm",70)))
        except Exception: pass
    rp.tick.connect(_on); rp.start()
    return [mon, mot, met, rp]

def main(path: str):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    open_replay_windows(path)
    sys.exit(app.exec_())

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "verify_out.csv")
