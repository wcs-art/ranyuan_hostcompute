# EV1 睡眠监测上位机

燃元科技 EV1 睡眠监测设备的上位机软件。采集 EEG 4 通道 + fNIRS 10 通道（760/850 nm 双波长）数据，
实时显示、记录并导出为 EDF / SNIRF / XDF，同时通过 LSL 对外广播。

## 运行环境

- Python 3.10+

```bash
pip install -r requirements.txt
```

## 启动

```bash
run.bat
```

或手动指定数据源：

```bash
python main.py --source=real  --proto=serial --com=COM3 --baud=921600
python main.py --source=text  --com=COM3 --baud=921600
python main.py --source=text  --replay=capture/text_field.bin
python main.py --source=dry   --replay=capture/demo.bin
python main.py --source=sim
```

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

数据链路尚未在真实设备上验证，当前测试均为离线自测。
