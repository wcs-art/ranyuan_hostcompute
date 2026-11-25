"""上位机统一入口(源码运行与PyInstaller打包共用).

数据源与链路(任一组合):
  python main.py --source=real --proto=udp --target-ip=192.168.3.140 --port=5000
                                                          # 当前 ESP 固件: HELLO/测试包/START/100B EEG
  python main.py --source=real --proto=tcp --port=5000     # 模组 TCP Client, 主动连本机
  python main.py --source=real --proto=tcp-client --host=192.168.4.1 --port=8080
                                                          # 模组 TCP Server, 本机主动连
  python main.py --source=real --proto=serial --com=COM3 --baud=921600 --protocol=fw100
                                                          # 默认: USART1 有线(100B AA55 二进制帧)
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
    """没有控制台时用系统弹窗告知原因, 避免"双击闪退、什么都不知道"。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)
    except Exception:  # noqa: BLE001 - 非 Windows 或无 GUI 时忽略
        pass


def install_crash_hook() -> None:
    """兜底: 未捕获异常写日志 + 弹窗, 而不是静默退出。"""

    def _hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log("[CRASH] 未捕获异常:\n" + text)
        popup("上位机异常退出", f"{exc_type.__name__}: {exc}\n\n详细日志: {_log_file()}")
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook


def _run_dry_headless(src) -> int:
    """脱机回放: 不开窗口, 只统计帧数/序号连续性。B1 脱机门禁的入口。"""
    import time as _t

    t0 = _t.time()
    n = 0
    first_seq = last_seq = None
    gaps = 0
    while True:
        frames = src.poll_frames()
        if not frames:
            if src.exhausted:
                break
            continue
        for f in frames:
            if first_seq is None:
                first_seq = f["seq"]
            elif last_seq is not None and f["seq"] != last_seq + 1:
                gaps += 1
            last_seq = f["seq"]
            n += 1
    dt = _t.time() - t0
    print(f"[dry] 文件      : {src.path}")
    print(f"[dry] 字节      : {len(src._data)}")
    print(f"[dry] 帧数      : {n}")
    print(f"[dry] seq 范围  : {first_seq} .. {last_seq}")
    print(f"[dry] 序号断点  : {gaps}")
    print(f"[dry] 耗时      : {dt*1000:.1f} ms")
    return 0 if n > 0 else 2


def main() -> None:
    install_crash_hook()
    # ev1/ 已按职责分 7 个子目录；统一用 ev1_paths 把全部子目录加进 sys.path，
    # 这样各模块仍按平级模块名 import，不必改 25 个文件的 import 语句。
    from ev1_paths import ev1_dirs
    _ensure_on_path(*ev1_dirs())
    from source import make_source, parse_argv

    opts = parse_argv(sys.argv[1:], base=_load_run_config())
    log(f"启动: source={opts['source']} proto={opts['proto']} port={opts['port']} "
        f"host={opts['host']} target_ip={opts['target_ip']} "
        f"com={opts['com']} baud={opts['baud']} raw_dump={opts['raw_dump']} "
        f"protocol={opts['protocol']} eeg_gain={opts['eeg_gain']} eeg_vref={opts['eeg_vref']} "
        f"eeg_preamp_gain={opts.get('eeg_preamp_gain')}")

    # 未指定控制串口时交给界面选实际设备，不再尝试旧机器上的固定 COM 编号。
    needs_com = (opts["source"] == "real" and opts["proto"] in ("serial", "uart", "com", "udp")) or \
                opts["source"] in ("serial", "uart", "com", "text", "serial-text", "firewater")
    no_control_port = needs_com and not str(opts.get("com") or "").strip()
    if no_control_port:
        log("未预设控制串口：请在界面刷新端口、选择实际 COM 口后点击连接并采集")

    # 链路打不开不等于程序要退出: 现场最常见的是"线还没插/插错口/被别的工具占着",
    # 此时最需要的是界面照常打开, 让人能在里面改端口重试, 而不是闪退加一个弹窗。
    src = None
    start_err = None
    if not no_control_port:
        try:
            src = make_source(
                opts["source"], port=opts["port"], proto=opts["proto"], path=opts["replay"],
                host=opts["host"], target_ip=opts["target_ip"],
                com=opts["com"], baud=opts["baud"],
                raw_dump=opts["raw_dump"],
                protocol=opts["protocol"], eeg_gain=opts["eeg_gain"], eeg_vref=opts["eeg_vref"],
                eeg_preamp_gain=opts.get("eeg_preamp_gain"),
            )
        except SystemExit:
            raise                      # 参数本身写错(如 --source=dry 缺 --replay), 该报错就报错
        except Exception as e:  # noqa: BLE001 - 链路失败要给现场一句话原因
            start_err = str(e)
            log(f"[WARN] 数据源创建失败: {e} —— 窗口照常打开, 请手动选择链路")

    if src is not None and opts["source"] == "dry" and opts["headless"]:
        raise SystemExit(_run_dry_headless(src))

    # 链路在这里就打开: 端口占用/串口被占/模组连不上, 都在这步暴露
    if src is not None:
        try:
            src.start()
        except Exception as e:  # noqa: BLE001
            start_err = str(e)
            log(f"[WARN] 链路启动失败: {e} —— 窗口照常打开, 可在界面改端口/类型后重新连接")
        else:
            log(f"链路已就绪: {type(src).__name__}")
            if opts["source"] == "sim":
                log("[WARN] 当前为合成数据(source=sim), 波形不是真机数据 —— 仅用于自检")

    from main_window import main as new_main
    new_main(source=src, extra_cols=opts["extra_cols"], start_error=start_err)


if __name__ == "__main__":
    main()
