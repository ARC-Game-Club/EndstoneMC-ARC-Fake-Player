"""LLM 驱动的 Endstone 虚拟玩家。

架构：Python 大脑 + 行为包身体
------------------------------
Endstone 的 Python API 能读写方块、能传送实体，但**造不出 SimulatedPlayer**——
`spawnSimulatedPlayer` 只存在于 ``@minecraft/server-gametest``，跑在 BDS 的
独立 JS 运行时里。所以真正"像玩家"的身体必须由行为包提供。

于是分工是这样的：

    大脑（Python，本包主体）                身体（behavior_pack/ + JS）
    ──────────────────────                 ─────────────────────────
    world.py       感知压缩，读方块/实体      SimulatedPlayer
    actions.py     批量意图 → 执行计划        · 会被怪物仇恨、会掉血
    pathfinder.py  A* 寻路                    · 手会挥，方块真的被敲碎
    executor.py    逐 tick 分片执行            · 其他玩家看得见、打得中
    agent.py       感知-决策-执行循环
    llm.py         OpenAI 兼容客户端
    bridge.py      scriptevent 双向通道  ◄──►  scripts/main.js
    body.py        身体动作队列（真挖掘/真移动）
    realbody.py    执行器侧的真实动作处理器
    packing.py     行为包自动安装（含 level.dat 实验开关）
    activity.py    活动日志：把"AI 干了什么"写成人话流水账
    plugin.py      插件入口，把上面全部串起来

数据目录
--------
配置和日志都落在 Endstone 给的 ``data_folder`` 里，也就是::

    plugins/<插件名>/config.json     配置
    plugins/<插件名>/activity.log    AI 活动流水，按大小轮转

插件名由 ``pyproject.toml`` 里 ``[project.entry-points."endstone"]``
的键名经连字符转下划线后决定（入口为 ``arc-fake-player``，插件名为
``arc_fake_player``）。Endstone 正是靠这个 entry point
发现插件的——少了它，wheel 装进 ``plugins/`` 也加载不起来。

身体不可用时
------------
插件**不会**偷偷降级。行为包没装、握手超时、假人掉线——一律明确报错，
并把真实原因写进 activity.log。理由很简单：一个"看起来在跑、其实什么都没干"
的 AI 比一个直接报错的 AI 难查得多。

可独立测试
----------
``world`` / ``pathfinder`` / ``executor`` / ``actions`` / ``bridge`` /
``activity`` 都不 import Endstone，可以用假对象跑单元测试。这是刻意的
设计——游戏 API 只在 ``plugin.py`` 和 ``world.py`` 的薄适配层里出现。
"""

from .activity import ActivityLog
from .plugin import ArcFakePlayerPlugin

__all__ = ["ActivityLog", "ArcFakePlayerPlugin"]
