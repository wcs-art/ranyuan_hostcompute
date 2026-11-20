# EV1 睡眠监测上位机

燃元科技 EV1 睡眠监测设备的上位机软件。采集 EEG 4 通道 + fNIRS 10 通道（760/850 nm 双波长）数据，
实时显示、记录，并导出为 CSV / EDF / BDF / SNIRF / XDF。

## 运行环境
- Python 3.9.13

### 源码运行
```
.venv\Scripts\python.exe main.py
```

### 打包
```
.venv\Scripts\python.exe -m PyInstaller packaging/HostComputer6.spec --noconfirm
```
### ev1/ 分层模块（25 个）

| 子目录 | 类别 | 模块 | 职责 |
|---|---|---|---|
| `ev1/protocol/` | 协议/帧/契约 | contract、device_frame、frame_slicer、core_events、commands | 数据契约、CRC 与帧解析、切帧、事件总线、命令编码 |
| `ev1/algorithm/` | 算法（纯函数） | derive、fnirs、quality、band_power、imu | 光强→OD→ΔHbO/ΔHbR（MBLL）、暗噪声与质量、频带功率、姿态解算 |
| `ev1/source/` | 数据源适配 | source、text_source | 三态数据源（sim/real/dry）、文本源解析 |
| `ev1/transport/` | 通信收发 | receivers、tx | 串口/UDP/TCP 接收、下行命令发送 |
| `ev1/sync/` | 时钟/多设备 | lsl_outlet、lsl_sync、device_manager | LSL 输出与时钟同步、多设备状态管理 |
| `ev1/storage/` | 存储导出 | exporters、xdf_writer | CSV/EDF/BDF/SNIRF/XDF 导出、XDF 真实时钟校正 |
| `ev1/ui/` | 界面 | main_window、monitor_14ch、panels、replay_panel、test_panel、verify_center | PyQt5 主窗口、14 通道波形显示与放大、回放/测试/验证面板 |

模块之间保持平级 import（如 `from contract import Frame`），不写包前缀；这是靠 `ev1_paths.py` 在启动时把各子目录加入 `sys.path` 实现的，新增子目录只需在 `ev1_paths.py` 和打包配置的路径列表里各加一处。

