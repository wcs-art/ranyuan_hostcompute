# 远程仓库目录结构说明

本文件说明 **codeup 远程仓库 `origin/main`** 的目录结构（当前 31 个文件）。
按"运行最小集"精简：只保留能编译、能启动的代码本体，开发/诊断/内部文档一律留在本地磁盘、不入库。

## 顶层

| 条目 | 作用 |
|---|---|
| `main.py` | 唯一启动入口（源码运行与打包共用）；启动时初始化路径与崩溃兜底 |
| `ev1_paths.py` | 单一事实源：把 `ev1/` 各分层子目录加入 `sys.path`，模块之间保持平级 import 写法 |
| `config/run.json` | 运行参数（数据源、协议、端口、增益），启动时读取 |
| `ev1/` | 核心代码，25 个模块，按职责分入 7 个子目录（见下） |
| `requirements.txt` | 依赖版本声明，线上编译/重建环境用 |
| `README.md` | 项目简介与运行环境 |
| `.gitignore` | 忽略规则：本地产物、内部文档、开发资产不入库的护栏 |

## ev1/ 分层模块（25 个）

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

## 本地保留、远程没有的目录

这些目录靠 `.gitignore` 排除，本地文件一个没删：

| 目录/文件 | 本地内容 | 不入库原因 |
|---|---|---|
| `tests/` | 17 个回归测试 | 回归证据留本地；运行/打包不依赖 |
| `tools/` | 15 个诊断/抓包脚本 | 现场调试用，非运行必需 |
| `packaging/` | PyInstaller spec（2 个文件） | 线上编译用自己的打包流程 |
| `pytest.ini` | 测试配置 | tests 不入库，配置无需入库 |
| `docs/` | 内部台账、协议说明、调试手册、合同提取 | 甲方内部文档，按约定不上传 |
| `design/` | 设计骨架存档 | 同上 |
| `.workbuddy/` | 工作记忆与 AI 协作产物 | 同上 |
| `ARCHITECTURE.md` | 架构说明（含历史修订） | 同上 |
| `.venv/` | Python 3.9.13 虚拟环境 | 环境产物，560M |
| `dist/` `build/` | 打包产物/中间产物 | 产物，289M/42M |
| `capture/` `records/` `logs/` | 现场抓包、录制数据、运行日志 | 现场数据，不作源码交付 |
| `verify_center.edf/.xdf` | 验证导出样本 | 导出产物样本 |

## 运行环境

- Python **3.9.13**（线上编译加密系统同版本）
- 全仓注解已改为 `Optional`/`Union` 等 3.9 原生写法，不依赖 `from __future__ import annotations`
- 源码运行：`.venv\Scripts\python.exe main.py`
