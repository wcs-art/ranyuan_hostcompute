# -*- mode: python ; coding: utf-8 -*-
"""EV1 上位机打包配置（单目录结构 ev1/）。

结构变更说明（2026-11-12）：
  原 View/ Algorithm/ Controller/ Protocol/ acquisition/ processing/ Storage/
  transport/ ui/ 九个目录已合并为 ev1/ 单目录，模块名全部平级。
  旧的 --old 入口与遗留单通道工具已删除，不再需要排除项。

打包：pyinstaller packaging/HostComputer6.spec --noconfirm
"""

from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).parent

APP_NAME = "HostComputer6"
ENTRY_SCRIPT = PROJECT_ROOT / "main.py"
EV1_DIR = PROJECT_ROOT / "ev1"

SOURCE_DIRS = [PROJECT_ROOT, EV1_DIR]

datas = []
# 运行参数（数据源 / 协议 / 端口 / 增益），main.py 从 config/run.json 读
for _rel in (("config", "config"),):
    _src = PROJECT_ROOT / _rel[0]
    if _src.exists():
        datas.append((str(_src), _rel[1]))

# 平级模块由 main.py 在运行时拼 sys.path，静态分析看不到，必须显式声明
hiddenimports = [
    # 本项目（ev1/ 全部模块）
    "band_power",
    "contract",
    "core_events",
    "derive",
    "device_frame",
    "device_manager",
    "exporters",
    "fnirs",
    "frame_slicer",
    "imu",
    "lsl_outlet",
    "lsl_sync",
    "main_window",
    "monitor_14ch",
    "panels",
    "quality",
    "receivers",
    "replay_panel",
    "source",
    "test_panel",
    "tx",
    "verify_center",
    "xdf_writer",
    # Qt
    "PyQt5.QtCore",
    "PyQt5.QtGui",
    "PyQt5.QtWidgets",
    "PyQt5.sip",
    # 链路：ESP8285 WiFi 走 socket（标准库），Type-C 走 pyserial
    "serial",
    "serial.tools",
    "serial.tools.list_ports",
    # 其它运行依赖
    "sounddevice",
    "scipy.fft",
    "scipy.integrate",
    "scipy.signal",
    "pandas",
    "openpyxl",
    "yaml",
    "h5py",
    "pyedflib",
    "pyxdf",
    "pylsl",
    "matplotlib",
    "matplotlib.backends.backend_qt5agg",
    "pyqtgraph",
]

a = Analysis(
    [str(ENTRY_SCRIPT)],
    pathex=[str(p) for p in SOURCE_DIRS],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PyQt6",
        "PySide2",
        "PySide6",
        "tkinter",
        "pytest",
        "tests",
        "design",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=APP_NAME,
)
