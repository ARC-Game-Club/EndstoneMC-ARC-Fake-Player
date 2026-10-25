"""给测试用的 `endstone` 最小桩。

为什么需要它
------------
这套测试**本来是设计成能在真服务器上直接跑的**——测试文件自己不 import
endstone，游戏 API 只出现在 ``plugin.py`` 和 ``world.py`` 的薄适配层里。

但 ``endstone_arc_fake_player/__init__.py`` 会 ``from .plugin import``，
而 ``plugin.py`` 顶层又写着::

    from endstone import ColorFormat as C
    from endstone.command import Command, CommandSender, CommandSenderWrapper
    from endstone.event import (...)
    from endstone.plugin import Plugin

于是只要 import 包本身，就会连带把 endstone 拖进来。在一台**没有装
Endstone 的开发机**上（比如 CI、或者就想改改 Python 逻辑的时候），
整套测试会死在 `ModuleNotFoundError` 上，一个都跑不了。

所以这里补一个"空壳版" endstone：只提供 import 时**被求值的名字**，
行为一律留空。测试真正验证的是 Python 侧的逻辑，那些地方本来就用
自己写的 Fake* 替身，不走这个桩。

三个 `sys.modules` 上的坑
------------------------
1. **必须给 `endstone` 设 `__path__ = []`**。不设的话它是个普通
   module，`import endstone.actor` 会去磁盘上找，直接失败。
2. **子模块要显式挂到父模块属性上**（`es.actor = actor`）。只塞
   `sys.modules` 的话，已经 `import endstone` 的代码再访问
   `endstone.actor` 会拿不到。
3. **`endstone.plugin.Plugin` 得是真类**。`ArcFakePlayerPlugin` 要继承它，
   给个函数或 None 都会在类定义那一行炸掉。

用法
----
在**任何** `endstone_arc_fake_player` 的 import 之前调用::

    from tests.endstone_stub import install
    install()

或者直接跑 ``python tests/run_all.py``，它已经装好了。
"""

from __future__ import annotations

import sys
import types


def _make_color_format() -> type:
    """`ColorFormat` 的常量映射成**空串**。

    为什么是空串而不是 `"§a"` 这种像样的假值：空串在格式化输出里
    等于"什么都没加"，测试断言不会去比颜色码，比了反而脆。
    （真实值形如 `ColorFormat.RESET == '§r'`，`§` 是 Bedrock 的
    格式化转义符。）

    这里把 Endstone 实际存在的成员都列上，避免哪天有人写
    `C.MATERIAL_NETHERITE` 时报 AttributeError。
    """
    names = [
        "RESET", "BLACK", "DARK_BLUE", "DARK_GREEN", "DARK_AQUA",
        "DARK_RED", "DARK_PURPLE", "GOLD", "GRAY", "DARK_GRAY",
        "BLUE", "GREEN", "AQUA", "RED", "LIGHT_PURPLE", "YELLOW",
        "WHITE", "MINECOIN_GOLD", "MATERIAL_QUARTZ", "MATERIAL_IRON",
        "MATERIAL_NETHERITE", "MATERIAL_REDSTONE", "MATERIAL_COPPER",
        "MATERIAL_GOLD", "MATERIAL_EMERALD", "MATERIAL_DIAMOND",
        "MATERIAL_LAPIS", "MATERIAL_AMETHYST", "MATERIAL_RESIN",
    ]
    return type("ColorFormat", (), {n: "" for n in names})


def _make_game_mode() -> type:
    """值用字符串，和 Endstone 枚举的 `str()` 表现接近。

    `plugin._apply_bot_game_mode` 靠 `getattr(GameMode, want.upper())`
    取成员，所以这里的大小写必须按 Endstone 的写法给全。
    """
    return type("GameMode", (), {
        "SURVIVAL": "survival",
        "CREATIVE": "creative",
        "ADVENTURE": "adventure",
        "SPECTATOR": "spectator",
    })


def install() -> None:
    """把桩装进 `sys.modules`。重复调用是安全的（幂等）。"""
    if "endstone" in sys.modules:
        existing = sys.modules["endstone"]
        # 真 endstone 已经在了（比如在服务器上跑测试）——别去覆盖它。
        #
        # ⚠️ 判据得用 `sys.modules` 里**已经躺着的对象**，不能自己去
        #    `import endstone`：那等于替调用方做了导入决策，万一它想
        #    用桩、而桩又恰好让导入成功，就绕成死循环了。
        if hasattr(existing, "ColorFormat"):
            return

    es = types.ModuleType("endstone")
    # 坑 1：没有 __path__，`import endstone.xxx` 会去磁盘上找并失败。
    es.__path__ = []  # type: ignore[attr-defined]

    es.ColorFormat = _make_color_format()
    es.GameMode = _make_game_mode()
    es.Player = type("Player", (), {})
    es.Server = type("Server", (), {})
    sys.modules["endstone"] = es

    # --- endstone.actor ---
    actor = types.ModuleType("endstone.actor")
    actor.Actor = type("Actor", (), {})
    es.actor = actor                                    # 坑 2
    sys.modules["endstone.actor"] = actor

    # --- endstone.command ---
    command = types.ModuleType("endstone.command")
    command.Command = type("Command", (), {})
    command.CommandSender = type("CommandSender", (), {})
    command.CommandSenderWrapper = type("CommandSenderWrapper", (), {})
    es.command = command
    sys.modules["endstone.command"] = command

    # --- endstone.event ---
    event = types.ModuleType("endstone.event")
    # `Event` 是所有事件的基类。test_activity 会用它做
    # `issubclass(ann, Event)` 的签名校验，所以这里必须是真类，
    # 而且各事件类得**真的继承**它——否则 issubclass 一律为假。
    event.Event = type("Event", (), {})
    event.EventPriority = type("EventPriority", (), {
        "LOWEST": 0, "LOW": 1, "NORMAL": 2, "HIGH": 3,
        "HIGHEST": 4, "MONITOR": 5,
    })
    for ev in ("PlayerChatEvent", "PlayerJoinEvent", "PlayerQuitEvent",
               "PlayerDeathEvent", "ScriptMessageEvent", "PlayerCommandEvent",
               "ActorDamageEvent", "ActorDeathEvent"):
        setattr(event, ev, type(ev, (event.Event,), {}))

    def event_handler(func=None, *, priority=None, ignore_cancelled=False):
        """忠实复刻 Endstone 0.11 的实现。

        真实版本（`endstone/event/__init__.py`）就是给函数挂三个属性::

            setattr(f, "_is_event_handler", True)
            setattr(f, "_priority", priority)
            setattr(f, "_ignore_cancelled", ignore_cancelled)

        这不是"差不多就行"的地方——`test_activity` 正是靠
        `_is_event_handler` 标记来找出所有处理器的。桩要是只 `return fn`
        不挂标记，那个测试就会查出"0 个事件处理器"然后失败。

        真实版本同时支持 `@event_handler` 和 `@event_handler(...)` 两种写法，
        靠的是"第一参数是不是可调用"来判断，这里照抄。
        """
        def decorator(f):
            f._is_event_handler = True
            f._priority = priority if priority is not None else \
                event.EventPriority.NORMAL
            f._ignore_cancelled = ignore_cancelled
            return f

        if func is not None and callable(func):
            return decorator(func)
        return decorator

    event.event_handler = event_handler
    es.event = event
    sys.modules["endstone.event"] = event

    # --- endstone.scheduler ---
    scheduler = types.ModuleType("endstone.scheduler")
    scheduler.Scheduler = type("Scheduler", (), {})
    es.scheduler = scheduler
    sys.modules["endstone.scheduler"] = scheduler

    # --- endstone.plugin ---
    plugin = types.ModuleType("endstone.plugin")
    # 坑 3：ArcFakePlayerPlugin 要继承它，必须是个类。
    plugin.Plugin = type("Plugin", (), {})
    es.plugin = plugin
    sys.modules["endstone.plugin"] = plugin


def is_real_endstone() -> bool:
    """`install()` 之后判断跑的是真 Endstone 还是桩。"""
    mod = sys.modules.get("endstone")
    if mod is None:
        return False
    # 桩的 ColorFormat 里每个成员都是空串；真的至少 RESET 有内容。
    return getattr(getattr(mod, "ColorFormat", None), "RESET", "") != ""
