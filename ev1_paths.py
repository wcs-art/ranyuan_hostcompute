"""ev1 分层目录的路径引导 —— 单一事实源。

ev1/ 下按职责分成 7 个子目录（algorithm/protocol/source/storage/sync/transport/ui），
但所有模块仍以平级模块名互相 import（如 ``from derive import mbll``）。
要保持这种写法，就必须让每个子目录都在 sys.path 上。

所有入口统一调用本文件的 ev1_dirs()：
  - main.py          运行入口
  - tests/conftest.py 测试
  - tools/*.py       诊断脚本
  - packaging/*.spec  打包（spec 里内联同样逻辑，因为 spec 不能 import 项目模块）

改动目录结构时，只需要维护本文件和 spec 里的同一段，不要在每个脚本里硬编码。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
EV1 = ROOT / "ev1"


def ev1_dirs() -> list[Path]:
    """返回所有需要加入 sys.path 的目录：项目根 + ev1 + ev1 各子目录。

    用 glob 自动发现子目录，新增分类目录时无需改这里。
    注意打包场景：PyInstaller 把模块打进 PYZ，不会复制源码目录 ev1/，
    此时 ROOT 指向 _internal，EV1 不存在，绝不能对它 iterdir()，
    否则启动即 FileNotFoundError。打包后只需 ROOT（bootloader 已设好路径）。
    """
    dirs = [ROOT]
    if EV1.is_dir():  # 源码 checkout 才有；打包后为 False
        dirs.append(EV1)
        dirs.extend(sorted(p for p in EV1.iterdir() if p.is_dir() and not p.name.startswith("__")))
    return dirs


def install() -> None:
    """把所有 ev1 目录加到 sys.path 前面（幂等）。"""
    import sys

    for d in reversed(ev1_dirs()):
        s = str(d)
        if s not in sys.path:
            sys.path.insert(0, s)
