"""IMU 处理：零偏校准、互补滤波姿态角、静止-活动占比。"""
from __future__ import annotations
import numpy as np

class ImuProcessor:
    def __init__(self, fs=100.0, alpha=0.98):
        self.fs = fs; self.alpha = alpha
        self.bias_acc = np.zeros(3); self.bias_gyr = np.zeros(3)
        self.pitch = 0.0; self.roll = 0.0; self.yaw = 0.0
        self.win_act: list[float] = []
    def calibrate(self, acc: np.ndarray, gyr: np.ndarray):
        self.bias_acc = acc.mean(0); self.bias_gyr = gyr.mean(0)
    def update(self, acc: np.ndarray, gyr: np.ndarray, dt: float):
        a = acc - self.bias_acc; g = gyr - self.bias_gyr
        p_acc = float(np.degrees(np.arctan2(a[1], a[2])))
        r_acc = float(np.degrees(np.arctan2(-a[0], np.hypot(a[1], a[2]))))
        self.pitch = self.alpha*(self.pitch+g[1]*dt)+(1-self.alpha)*p_acc
        self.roll = self.alpha*(self.roll+g[0]*dt)+(1-self.alpha)*r_acc
        self.yaw += float(g[2]*dt)
        mag = float(np.linalg.norm(g))
        self.win_act.append(1.0 if mag > 15.0 else 0.0)
        self.win_act = self.win_act[-int(self.fs*60):]
        return self.pitch, self.roll, self.yaw
    def still_active_ratio(self):
        if not self.win_act: return 1.0, 0.0
        a = float(np.mean(self.win_act)); return 1-a, a


def attitude_rpy(acc_xyz, gyro_xyz=None):
    """S8/D6占位: 加速度计互补姿态角(度), 真解算等D6冻结."""
    a = np.asarray(acc_xyz, dtype=float).reshape(3)
    pitch = float(np.degrees(np.arctan2(a[1], max(a[2], 1e-6))))
    roll = float(np.degrees(np.arctan2(-a[0], np.hypot(a[1], a[2]) + 1e-9)))
    return np.array([pitch, roll, 0.0])


def still_active(acc_xyz, gyro_xyz=None, thr_g: float = 0.3):
    """静止=True/活动=False, 阈值法占位."""
    a = np.asarray(acc_xyz, dtype=float).reshape(3)
    g = np.asarray(gyro_xyz if gyro_xyz is not None else np.zeros(3), dtype=float)
    return bool(abs(np.linalg.norm(a) - 1.0) < thr_g and np.linalg.norm(g) < 30.0)
