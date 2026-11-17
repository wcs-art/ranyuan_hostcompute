"""上位机统一入口(源码运行与PyInstaller打包共用).

数据源与链路(任一组合):
  python main.py --source=real --proto=udp --port=5000     # 默认: 模组 UDP 透传, 往本机端口发
  python main.py --source=real --proto=tcp --port=5000     # 模组 TCP Client, 主动连本机
  python main.py --source=real --proto=tcp-client --host=192.168.4.1 --port=8080
                                                          # 模组 TCP Server, 本机主动连
  python main.py --source=real --proto=serial --com=COM3 --baud=921600
                                                          # Type-C 有线(AA55 二进制帧)
  python main.py --source=text --com=COM3 --baud=921600    # Type-C 有线(ASCII 文本行, 如 FireWater)
  python main.py --source=text --replay=t.txt              # 文本文件回放(不碰串口)
  python main.py --source=real --raw-dump=capture/x.bin    # 同时把原始字节落盘, 供帧格式比对
  python main.py --source=dry --replay=f.bin --headless    # dry: 录制字节回放(B1 脱机门禁)
  python main.py --source=sim                              # 仅自检: 合成数据(界面标题会标"模拟数据")

默认 source=real。合成数据不再作为默认值, 必须显式写 --source=sim 才会出现。

调试要点: 收不到数据时先看界面底部"链路"一行 —— 它会明确写出是端口占用、
模组连不上、串口打不开, 还是"已连上但 0 帧"(后者才是帧格式问题)。
"""
from __future__ import annotations
import datetime
import sys
import traceback
from pathlib import Path
def _app_dir() -> Path:
    """源码运行 = 项目根目录; 打包成 exe 后 = exe 所在目录。

    现场排障时配置和日志必须就在 exe 旁边, 不能藏在 _internal 里。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent
ROOT = _app_dir()
LOG_DIR = ROOT / "logs"


def _load_run_config() -> dict:
    """读 config/run.json 作为默认参数, 让"双击 exe"也能连真机。

    优先级: 命令行参数 > config/run.json > 内置默认值。
    打包后先找 exe 旁边的 config/, 再退回包内自带的那份。
    """
    import json

    candidates = [ROOT / "config" / "run.json"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "config" / "run.json")
    for p in candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {k: v for k, v in data.items() if not str(k).startswith("_")}
            except Exception as e:  # noqa: BLE001
                log(f"[WARN] 配置文件 {p} 解析失败, 已忽略: {e}")
    return {}


def _ensure_on_path(*dirs: Path) -> None:
    for directory in dirs:
        path_str = str(directory)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)
def _log_file() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"run_{datetime.datetime.now():%Y%m%d}.log"
def log(msg: str) -> None:
    """同时写 stderr 与 logs/run_YYYYMMDD.log。打包成 exe(无控制台)后,
    文件日志是唯一能留下证据的地方。"""
    line = f"{datetime.datetime.now():%H:%M:%S} {msg}"
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001 - 无控制台时 print 可能失败
        pass
    try:
        with _log_file().open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def popup(title: str, msg: str) -> None:
    """没有控制台时用系统弹窗告知原因, 避免"双击闪退、什么都不知�