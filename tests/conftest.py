"""B0 测试骨架 —— 路径与纪律.

纪律(见交接文档第五节铁律):
  AI 可以写测试代码, 但"期望值"那一栏必须由人填, 且来源不能是这份代码。
  本目录的期望值分两类, 注释里标清楚:
    [规格] = 来自需求文档 / 协议约定, 人填的
    [现状] = 记录当前实现的事实, 用于回归, 不代表"这样是对的"
  用实现算期望、再拿实现去比的同义反复测试, 一律删除。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "ev1"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
