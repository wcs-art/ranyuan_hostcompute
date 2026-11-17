# EV1 睡眠监测上位机

燃元科技 EV1 睡眠监测设备的上位机软件。采集 EEG 4 通道 + fNIRS 10 通道（760/850 nm 双波长）数据，
实时显示、记录并导出为 EDF / SNIRF / XDF，同时通过 LSL 对外广播。

## 运行环境

- Python 3.10+

```bash
pip install -r requirements.txt
```

## 启动

最简单的方式：双击 `run.bat`。默认按 `config/run.json` 启动（当前为有线文本串口 COM3 @921600），
窗口打开即自动采集。

也可以在界面底部「采集、保存与串口」一栏里现场切换：

1. 选类型（文本串口 / 二进制串口 / UDP / TCP / 模拟数据）
2. 选端口（点「刷新端口」重新扫描 COM 口，也可手输）与波特率
3. 点「▶ 连接并采集」；再点一次即断开

换线、换口不必重启程序，也不必改配置文件。

命令行方式（参数优先于配置文件）：

```bash
python main.py --source=real  --proto=serial --com=COM3 --baud=921600
python main.py --source=text  --com=COM3 --baud=921600
python main.py --source=text  --replay=capture/text_field.bin
python main.py --source=dry   --replay=capture/demo.bin
python main.py --source=sim
```

> 同一个 COM 口不要同时被 VOFA、串口助手等工具打开：驱动允许两个句柄共存，但字节会被分流，
> 结果是一串跳变脏值（实测出现过 -7218 跳到 +6918，而真实信号全为负），排查时极易误判成硬件问题。

## 有线的两种数据格式（不要混用）

| 格式 | 启动参数 | 内容 |
|---|---|---|
| AA55 二进制帧 | `--source=real --proto=serial` | 合同 F2 帧：帧头、包序号、CRC16、双时间戳、事件标记 |
| ASCII 文本行 | `--source=text` | 实测固件输出：每行一个十进制值 + `\r\n`，约 500 行/秒 |

文本格式不带帧头、序号、CRC、时间戳与事件，因此界面会标注「文本原始值 · 未换算 · 无 CRC/丢包校验」：
丢包统计与 CRC 校验不适用，数值为原始值（单位待定），指标面板不参与计算。
该格式**无法满足合同 F2 的验收判据**，两者不是同一条链路的两种写法。

数据源与协议（`v1` 104 字节 / `v1r5` 152 字节 / `compat90` 90 字节）在 `config/run.json` 中切换。

## 目录结构

```
ev1/          主程序（界面、接收、解析、处理、导出、LSL）
config/       运行配置
tools/        现场排查与抓包工具
tests/        单元测试
packaging/    PyInstaller 打包配置
main.py       入口
```

## 测试

```bash
python -m pytest -q
```

## 状态

- 有线串口文本行链路：已在真实设备上跑通 **[E3]**（COM3 @921600，固件实际输出约 500 行/秒，
  界面实测 475 Hz、波形正常刷新）。
- AA55 二进制帧链路（合同 F2 全字段）：未在真实设备上验证 **[E0]**。
- UDP / TCP 网络链路：未在真实模组上验证 **[E0]**。
- 文本格式缺帧头/序号/CRC/双时间戳/事件/设备号，合同 F2 的五项验收判据无法据此判定。
- 其余单元测试均为离线自测 **[E1]**，不构成链路验收证据。

