"""Endstone 插件主入口 —— LLM 驱动的虚拟玩家（混合架构）。

架构
----
    大脑（纯 Python）                      身体（行为包 + JS）
    ─────────────────                      ──────────────────
    感知压缩 → 提示词                        SimulatedPlayer
    LLM 决策 → 批量工具调用                   · 会被怪物仇恨
    几何展开 → ExecutionPlan                  · 会掉血、会饿
    A* 寻路                                   · 手会挥、方块会真的被敲碎
    逐 tick 执行 ──scriptevent──►            · 其他玩家看得见
                  ◄──scriptevent──

为什么是这个形状
----------------
Endstone 的 Python API 造不出 SimulatedPlayer，那个接口只在
``@minecraft/server-gametest`` 里，跑在另一个 JS 运行时。所以：

  · **身体必须借 JS**：只有它能 spawnSimulatedPlayer
  · **大脑必须留 Python**：方块读写走 Python 是直连 API，
    比绕一圈 scriptevent 快几个数量级；而 A*、批量展开、
    tick 调度这些纯粹的计算，放哪儿都一样，留在 Python 不用改一行

行为包没就绪时，插件**不会**偷偷换个实现顶上——它会明确报错并停摆。
因为"假人站着不动、方块自己消失"看起来像能用，实际上假人是假的，
这比直接报错更难排查。

数据目录
--------
    plugins/<插件名>/
        config.json     配置
        activity.log    活动流水账（"AI 干了什么"）
        activity.log.1  上一份，超限后轮转出来的
"""

# ⚠️ 这个文件**绝对不能**写 `from __future__ import annotations`。
#
# 它会把所有类型注解变成字符串（PEP 563）。而 Endstone 的
# `Plugin.register_events()` 是这么校验事件处理器的：
#
#     sig = inspect.signature(func)
#     params = list(sig.parameters.values())
#     if (len(params) != 1
#         or not inspect.isclass(params[0].annotation)      # ← 这里
#         or not issubclass(params[0].annotation, Event)):
#         logger.error("... attempted to register an invalid event handler ...")
#
# 启用 PEP 563 后 `params[0].annotation` 拿到的是字符串 'PlayerChatEvent'
# 而不是类对象，`inspect.isclass()` 返回 False → **每一个事件处理器都被
# 拒绝注册**。症状极具迷惑性：插件加载成功、日志只有三条 ERROR、命令能用，
# 但行为包的消息通道（on_script_message）根本没接上，身体永远连不上。
#
# 判断特征：报错里的签名带引号 —— `(event: 'PlayerChatEvent')`。
# 正常应该是 `(event: PlayerChatEvent)`。

import json
import math
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from endstone import ColorFormat as C
from endstone.command import Command, CommandSender, CommandSenderWrapper
from endstone.event import (
    ActorDamageEvent,
    EventPriority,
    PlayerChatEvent,
    PlayerDeathEvent,
    PlayerJoinEvent,
    ScriptMessageEvent,
    event_handler,
)
from endstone.plugin import Plugin

from . import packing
from .actions import ActionCompiler
from .activity import ActivityLog
from .agent import (DEFAULT_BODY, DEFAULT_GAME_KNOWLEDGE, DEFAULT_IDENTITY,
                    DEFAULT_ITEM_NAMES, DEFAULT_PERSONA, DEFAULT_RULES,
                    DEFAULT_SURFACE_HINT, Agent, AgentConfig)
from .body import BodyRegistry
from .bridge import FakePlayerBridge
from . import confmig
from .confmig import CONFIG_VERSION
from .crowd import CrowdScheduler, load_cards
from .economy import EconomyBridge
from .executor import BatchExecutor, Plan, PlanState, Step
from .identity import IdentityRegistry, looks_synthetic
from .llm import LLMClient
from .pathfinder import make_probe, walkable_surface
from .realbody import RealBodyActions
from .roam import RoamDriver
from .social import CombatReflex, FavorStore
from .world import WorldAdapter, short_id


def _clean_name(raw: str) -> str:
    """把用户输入的名字净化成一个能安全穿过 scriptevent 通道的名字。

    为什么需要这一步
    ----------------
    假人名字会经过 `/ai spawn <name>` → Python 桥接 → `scriptevent` 命令
    → JS `JSON.parse` 这一整条链路。这条链路上有两个地方对特殊字符不友好：

      · **双引号**会在 `json.dumps` 里变成 `\\"`，但 BDS 的命令参数解析器
        会把反斜杠吃掉，于是 JSON 破损，JS 侧 `JSON.parse` 抛异常，
        整条指令**静默丢失**——Python 那边只看到"请求发出去了没人回"。
      · **空格**会让命令参数被切成两半，后面的内容全丢。

    所以这里只保留"看起来像玩家名"的字符：字母、数字、下划线，以及
    CJK / 西里尔等非 ASCII 文字字符。剩下的替换成下划线。

    这个函数是**唯一**的名字入口。任何新加的接受名字的命令都必须走它，
    否则上面那两个坑会顺着新入口重新长出来。
    """
    text = str(raw or "").strip()
    out: list[str] = []
    for ch in text[:24]:
        # 引号、空格、控制字符一律换掉；其余（含中文）保留
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    cleaned = "".join(out).strip("_")
    return cleaned or "bot"


def _mentions(low_message: str, name: str) -> bool:
    """消息（已小写）里是否"点名"提到这个 AI。

    ASCII 名字要求词边界：前后不能贴着字母/数字/下划线——
    否则叫 Al 的假人会被每一句 "alice"/"hello" 误命中。
    中文名字（含非 ASCII）直接包含即命中。
    """
    target = name.lower().strip()
    if not target:
        return False
    low_message = str(low_message or "").lower()
    if any(ord(ch) > 127 for ch in target):
        return target in low_message
    start = 0
    while True:
        idx = low_message.find(target, start)
        if idx < 0:
            return False
        before = low_message[idx - 1] if idx > 0 else ""
        after = (low_message[idx + len(target)]
                 if idx + len(target) < len(low_message) else "")
        if not (before.isalnum() or before == "_") and \
                not (after.isalnum() or after == "_"):
            return True
        start = idx + 1


def _kind_of(text: str) -> str:
    """从 Agent 的日志正文猜出事件类别。

    Agent 端用的是 emoji 前缀（见 agent.py 的 _log 调用点），这里按前缀
    归类，好让活动日志里能按类别 grep。猜不出来就算"思考"。
    """
    head = text.lstrip()
    if head.startswith("✓") or head.startswith("✗"):
        # 计划结算："✓ 挖区域(9格) 9/9 完成"
        body = head[1:].lstrip()
        for mark, kind in (("挖", "dig"), ("采集", "collect"), ("放置", "build"),
                           ("建造", "build"), ("搭", "build"),
                           ("走", "move"), ("移动", "move"), ("前往", "move"),
                           ("攻击", "dig"), ("进食", "collect")):
            if body.startswith(mark):
                return kind
        return "think"
    for mark, kind in (
        ("💭", "say"), ("🎯", "goal"), ("📝", "note"), ("✅", "done"),
        ("🎮", "think"), ("🗜", "context"), ("🧠", "context"),
        ("🧹", "context"), ("⚠", "error"), ("✗", "error"),
    ):
        if head.startswith(mark):
            return kind
    return "think"


class ArcFakePlayerPlugin(Plugin):
    api_version = "0.11"

    commands = {
        "arc_fake_player": {
            "description": "LLM 驱动的虚拟玩家",
            "aliases": ["ai"],
            "usages": [
                "/arc_fake_player set <base_url: str> <api_key: str> <model: str>",
                "/arc_fake_player test",
                "/arc_fake_player spawn <name: str> [goal: message]",
                "/arc_fake_player list",
                "/arc_fake_player stop <name: str>",
                "/arc_fake_player remove <name: str>",
                "/arc_fake_player goal <name: str> <goal: message>",
                "/arc_fake_player chat <name: str> <message: message>",
                "/arc_fake_player persona <name: str> <text: message>",
                "/arc_fake_player money <name: str> [amount: float]",
                "/arc_fake_player cmd <name: str> <command: message>",
                "/arc_fake_player home",
                "/arc_fake_player home <args: message>",
                "/arc_fake_player style <args: message>",
                "/arc_fake_player chatrate <args: message>",
                "/arc_fake_player session <name: str> <minutes: float>",
                "/arc_fake_player auto [on|off|status]",
                "/arc_fake_player reload",
                "/arc_fake_player wear <name: str>",
                "/arc_fake_player where <name: str>",
                "/arc_fake_player tp <name: str>",
                "/arc_fake_player body [real|npc|auto]",
                "/arc_fake_player ctx [option: str] [lines: int]",
                "/arc_fake_player log [action: str] [lines: int]",
                "/arc_fake_player install",
                "/arc_fake_player prompt [section: str] [text: message]",
                "/arc_fake_player sense [mode: str]",
            ],
            "permissions": ["arc_fake_player.command"],
        }
    }

    permissions = {
        "arc_fake_player.command": {
            "description": "允许使用 /ai 命令",
            "default": "op",
        }
    }

    # ------------------------------------------------------------------

    def on_enable(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="arc_fake_player")
        self._agents: dict[str, Agent] = {}
        self._actors: dict[str, Any] = {}          # name -> 身体实体
        #: 已经为哪些名字打过"同名真人"的警告。只为了让那条警告不刷屏
        #: ——它在感知循环里，正常情况下每 0.5 秒就会被问一次。
        self._name_clash_warned: set[str] = set()
        #: 受保护的周期任务各自出过几次异常。用来给重复报错限流。
        self._guard_failures: dict[str, int] = {}
        self._config_path = Path(self.data_folder) / "config.json"
        self._migrate_legacy_config()
        self._cfg = self._load_config()

        # 活动日志：记录"AI 干了什么"。和 logger 分工不同——logger 给排障，
        # 它是给服主看的人话流水账。写不了也不影响游戏（内部全吞异常）。
        self._activity = ActivityLog(
            Path(self.data_folder),
            max_bytes=int(self._cfg["log_max_bytes"]),
            backups=int(self._cfg["log_backups"]),
            enabled=bool(self._cfg["activity_log"]),
            echo=self.logger.info if bool(self._cfg["log_echo_console"]) else None,
            logger=self.logger,
        )
        self._activity.write("system", "插件已启用")
        self.logger.info(f"数据目录: {self.data_folder}")
        self.logger.info(f"活动日志: {self._activity.path}")

        self._world = WorldAdapter(
            self.logger, surface_only=bool(self._cfg["surface_only"]))
        # 感知里的"附近玩家"一行靠这个（scan_entities 会合并玩家列表）。
        # 过滤掉**自己人**（我们的假人 + 外部身体）：假人互相看不见，
        # 大脑就不会主动找另一个假人搭话——两个 AI 在公屏你来我往
        # 的"尬聊"就是这么来的。真人和离线玩家不受影响。
        self._world.bind_players(lambda: [
            p for p in self.server.online_players
            if str(getattr(p, "name", "") or "") not in self._reserved_names()])
        self._executor = BatchExecutor(
            self.logger, max_steps_per_tick=int(self._cfg["steps_per_tick"]))

        # 桥接层：发命令用 dispatch_command
        self._bridge = FakePlayerBridge(self.logger, self._dispatch)
        self._bridge.new_token()
        # 领地守卫：假人挖/放的每一个方块先过 arc_core 权限
        # （别人领地里不动土——群演假人不能当破保护的外挂用）。
        self._bridge.modify_guard = self._land_modify_guard
        # 身体注册表：管理每具假人的真实动作队列
        self._bodies = BodyRegistry(self._bridge, self._world, self.logger)

        # 经济桥（arc_core 的 Economy）。没有 arc_core 时所有方法
        # 安全失败——假人照样能玩，只是没有钱包。
        self._economy = EconomyBridge(self.server, self.logger)

        # 合成身份注册表：每个假人一个稳定持久的 xuid/uuid。
        # 引擎给 SimulatedPlayer 的 xuid 是空的且改不了；弧光核心以
        # xuid 为主键，没有这个注册表所有假人会挤成同一行数据。
        # 核心（≥0.9.72）遇到空 xuid 会调 api_resolve_xuid 来查。
        self._identities = IdentityRegistry(
            Path(self.data_folder) / "identities.json", self.logger)
        # 钱包键切到合成 xuid（旧的 fake_<名字> 键余额会自动迁移过来）
        self._economy.bind_key_fn(lambda n: self._identities.xuid_of(n))
        self._identities.save()

        # 好感度（对每个真人玩家）+ 反击条件反射。
        # 反射是秒级反应，不等 LLM；大脑通过事件播报知情并接手后续。
        self._favor = FavorStore(Path(self.data_folder) / "favorability.json",
                                 self.logger)
        self._reflex = CombatReflex(
            attack=lambda b, t: self._bridge.attack(b, t) >= 0,
            walk=lambda b, x, y, z: self._bridge.walk(
                b, x, y, z, timeout=100) >= 0,
            bot_pos=self._bot_pos,
            # 真人按名字查坐标；怪物按 runtime id 查（注册表由
            # _mob_on_attacked 播种）。两者都查不到 = 目标消失。
            target_pos=lambda t: self._human_pos(t) or self._mob_pos(t),
            bot_health=self._bot_health,
            on_event=self._reflex_event,
            logger=self.logger)
        #: 怪物反击目标注册表：str(runtime_id) → (Actor 引用, 过期时刻)。
        #: Actor 句柄约一分钟会过期，所以带 TTL 惰性清理；过期/失效后
        #: 反射按"目标消失"正常脱战。
        self._mob_targets: dict[str, tuple[Any, float]] = {}
        #: (假人, 玩家) → 寻仇冷却到期时刻。engage 时预记，防止脱战后
        #: 立刻被复仇扫描重新拉起、无限缠斗。
        self._vengeance_cd: dict[tuple[str, str], float] = {}
        #: 假人 → 下次好感度摘要注入时刻（30 秒一条，不刷上下文）。
        self._favor_next: dict[str, float] = {}

        # 自动在线调度器：角色卡轮换，维持稳定在线人数。
        # 钩子方法都在本类上（crowd_*），crowd.py 零游戏依赖、可单测。
        self._crowd = CrowdScheduler(self)

        # 外部身体（如 ai 助手的常驻假人）。它们**没有** Python 大脑——
        # 大脑在调用方插件里，通过 api_body_* 操控。名字 → Live 引用。
        self._external: dict[str, Any] = {}
        # 高空安全落地后待切换的游戏模式（名字 → 数字）。见 _crowd_spawn。
        self._pending_gm: dict[str, int] = {}
        # 隐身假人（自动在线的"群演"）：一切"它是 AI"的公开标识
        # （[AI] 名牌、AI 上线广播、进服提示）都不给。
        self._stealth: set[str] = set()
        # (假人, 玩家) → 上次"玩家靠近"提醒时刻。控频用。
        self._proximity_seen: dict[tuple[str, str], float] = {}

        # 常驻地（家）表：homes.json，OP 用 /ai home 设置。
        self._homes_path = Path(self.data_folder) / "homes.json"
        self._homes: dict[str, Any] = self._load_homes()
        # 每个 AI 的个性设置：公屏搭话概率 / 说话风格示例。
        # bot_settings.json 持久；角色卡在首次上线时播种默认值。
        self._bot_settings_path = Path(self.data_folder) / "bot_settings.json"
        self._bot_settings: dict[str, Any] = self._load_bot_settings()
        # 锚点闲逛驱动：家 > 自己领地 > 公共传送点；忙时（打架/对局/
        # 有计划）让路。与 arena AI 同级的"条件反射"层。
        self._roam = RoamDriver(
            names=lambda: [n for n, a in self._agents.items() if a.alive()],
            busy=self._roam_busy,
            anchor=self._roam_anchor,
            bot_pos=self._bot_pos,
            surface=self._roam_surface,
            walk=self._roam_walk, dig=self._roam_dig,
            log=lambda m: self.logger.debug(m))
        self._warps_cache: tuple[float, list[tuple]] = (0.0, [])

        # 编译前先决定用哪种模式（真身体 / 直连）
        self._use_real_body = False
        self._body_checked = False
        self._pack_report: dict[str, Any] | None = None
        # 已经为哪些名字建过常加载区域。避免每 10 tick 重复下发同一条命令。
        self._ticking_areas: set[str] = set()
        self._register_handlers()
        self.register_events(self)

        # 行为包安装必须等到世界加载之后——on_enable 阶段 server.level 不可用，
        # 找世界目录必然失败。放到第一个 tick 里做（见 _tick）。
        # 这里只记下用户的意图。
        self._want_install = bool(self._cfg.get("auto_install_pack", True))

        # 启动自检：默认 60 tick（3 秒）后检查行为包是否就绪
        if str(self._cfg.get("body_mode", "auto")).lower() != "npc":
            self._guard(self._startup_check, "启动自检", delay=60)

        # 每 tick：推进计划 + 推进身体动作队列 + 清理超时请求
        self._guard(self._tick, "主循环", delay=1, period=1)
        # 每 10 tick：感知 + 决策
        self._guard(self._perceive, "感知循环", delay=10, period=10)
        # 每 5 秒：检查行为包连接，断了就报错并停摆
        self._guard(self._health, "连接自检", delay=100, period=100)
        # 每 30 秒：自动在线调度（角色卡轮换、到点换人）
        self._guard(self._crowd_tick, "自动在线", delay=CrowdScheduler.TICK_PERIOD,
                    period=CrowdScheduler.TICK_PERIOD)
        # 每 15 秒：护甲自穿——管理员 /give 的装备进背包后自动上身。
        # JS 侧对"已穿最好"是空操作，所以常开没有副作用。
        self._guard(self._equip_tick, "护甲自穿", delay=300, period=300)

        self.logger.info(f"{C.GREEN}LLM 虚拟玩家插件已启用{C.RESET}")
        self.logger.info("配置模型：/ai set <base_url> <api_key> <model>")
        self.logger.info("生成虚拟玩家：/ai spawn <名字>")

    #: 同一条报错最多重复打印几次。崩在每 tick 的循环里时，
    #: 不限量的话日志会被同一行刷爆，反而看不到第一次的现场。
    _MAX_GUARD_REPORTS = 3

    def _guard(self, fn: Any, label: str, **task_kwargs: Any) -> None:
        """把一个调度器回调包成"绝不向外抛异常"的版本，然后注册。

        为什么必须有这一层
        ------------------
        调度器回调是在 C++ 侧被调用的。任何漏出去的 Python 异常都会
        一路冒到引擎里，**直接把服务器带崩**——用户实测现象就是
        "程序莫名其妙崩溃了，Log 都没来得及记"。

        包上之后，最坏情况也只是这个 tick 没干活，但服务器还活着，
        而且日志里留下了完整的 traceback，下一次能定位。

        分层原则：这是**最后一道**网。正常情况下每个函数内部已经各自
        兜过异常了；这一层存在的意义是"万一某处漏了，代价不要是整个
        服务器"。所以它只记日志、绝不再尝试任何有风险的操作。
        """
        name = f"arc_fake_player:{label}"

        def wrapped() -> None:
            try:
                fn()
            except Exception:
                self._report_guard_failure(label)

        self.server.scheduler.run_task(
            self, self._make_task(wrapped), **task_kwargs)

        # run_task 返回一个 task 句柄，但我们不再需要它；
        # 记一下名字是为了让日志能对应上。
        self.logger.debug(f"已注册受保护的周期任务 {name}")

    def _make_task(self, wrapped: Any) -> Any:
        """把无参闭包包成 Endstone 调度器要的可调用对象。

        Endstone 的 `run_task` 接受 `(plugin, callable)`。这里不额外
        包装，直接返回——单独一个方法是为了给测试留一个替换点。
        """
        return wrapped

    def _report_guard_failure(self, label: str) -> None:
        """周期任务抛异常时的兜底报告。只记日志，不做任何有风险的事。"""
        count = self._guard_failures.get(label, 0) + 1
        self._guard_failures[label] = count
        if count > self._MAX_GUARD_REPORTS:
            # 已经报够了。再报只是刷屏，而且大概率是同一个原因。
            return
        try:
            tb = traceback.format_exc()
        except Exception:
            tb = "(traceback 不可用)"
        suffix = ("" if count < self._MAX_GUARD_REPORTS
                  else f"（这条之后不再重复打印）")
        self.logger.error(
            f"{C.RED}✗ 「{label}」周期任务第 {count} 次异常，已跳过本次执行，"
            f"服务器不受影响{suffix}{C.RESET}")
        for line in tb.rstrip().split("\n"):
            self.logger.error(f"{C.RED}  {line}{C.RESET}")
        try:
            self._record("error", f"{label} 周期任务异常（第 {count} 次）")
        except Exception:
            pass

    def on_disable(self) -> None:
        # 停服/卸载前把在线假人的游玩时长结算掉（否则最后一次会话的
        # 秒数全丢——进服次数已 +1，时长却没累上，统计就对不上账）。
        for name in list(self._agents):
            try:
                self._core_playtime(name, joining=False)
            except Exception:
                pass
        for agent in list(self._agents.values()):
            try:
                agent.stop("插件卸载")
            except Exception:
                pass
        self._agents.clear()
        # 外部身体（助手假人）也一并撤掉——大脑在别的插件里，
        # 但身体是本插件造的，卸载时得收走。
        for name in list(self._external):
            try:
                self._bridge.remove(name)
            except Exception:
                pass
        self._external.clear()
        self._pending_crowd.clear()
        for name in list(self._arena_next):
            self._arena_disable(name)
        # 让所有假人停下，避免它们保持"正在挥臂"的状态
        for ctl in self._bodies:
            try:
                ctl.cancel()
            except Exception:
                pass
        self._pool.shutdown(wait=False, cancel_futures=True)
        try:
            self._favor.save()
        except Exception:
            pass
        if self._identities is not None:
            try:
                self._identities.save()
            except Exception:
                pass
        self._activity.write("system", "插件已卸载")
        self._activity.close()
        self.logger.info("虚拟玩家插件已卸载")

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def _migrate_legacy_config(self) -> None:
        """首次更名启动时复制旧配置，保留原文件及已经存在的新配置。"""
        if self._config_path.exists():
            return
        legacy = self._config_path.parent.parent / "pureagent" / "config.json"
        if not legacy.is_file() or legacy == self._config_path:
            return
        try:
            raw = legacy.read_text(encoding="utf-8")
            if not isinstance(json.loads(raw), dict):
                raise ValueError("旧配置不是 JSON 对象")
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            with self._config_path.open("x", encoding="utf-8") as dest:
                dest.write(raw)
            self.logger.info(f"已从 {legacy} 复制配置到 {self._config_path}")
        except FileExistsError:
            pass
        except Exception as exc:
            self.logger.warning(f"旧配置迁移失败: {exc}")

    def _load_config(self) -> dict[str, Any]:
        defaults = {
            "base_url": "",
            "api_key": "",
            "model": "",
            "timeout": 45.0,
            "native_tools": True,
            "decision_interval": 12.0,
            "steps_per_tick": 6,
            "default_goal": "活下来并发展：找食物、收集木头和石头、天黑前有个能躲的地方。",
            "spawn_entity": "minecraft:npc",
            # auto = 行为包连上就用真身体；连不上就报错停摆，绝不换实现顶替
            # real = 强制要求真身体（连不上直接拒绝生成）
            # npc  = 明确用 NPC 扮演（不是虚拟玩家，仅供调试）
            "body_mode": "auto",
            "auto_install_pack": True,
            # 假人上线时强制设定的游戏模式。可用值：
            #   survival / creative / adventure / spectator / keep
            #
            # 默认 survival，而不是"沿用生成时所在世界的模式"。
            # 原因：SimulatedPlayer 会继承**生成时**世界/玩家所在的游戏模式，
            # 服主在创造模式里 spawn 出来的假人就会一直是创造模式——
            # 无敌、瞬间破坏。后果有两个：打它不扣血（没法验证战斗能力），
            # 挖方块没有时长（看起来像作弊）。
            #
            # 填 "keep" 表示不干预，沿用原有模式。
            "bot_game_mode": "survival",
            # 是否给假人脚下建常加载区域（tickingarea）。
            #
            # ⚠️ 默认 **关闭**，这是刻意的。
            #
            # SimulatedPlayer 本身就是模拟玩家，**走到哪加载到哪**，
            # 和真人完全一样——这是它的固有行为，不需要任何额外代码。
            # 真正的玩家不会把沿途区块永久钉在内存里，假人也不该。
            #
            # 常加载区域是另一种东西：它让一片区块**永远**在线，无论
            # 有没有人靠近。只有"假人长期驻守某地、而你希望那里一直
            # 运转（刷怪、农作物生长）"时才需要它。代价是这些区块
            # 常驻内存、实体永不休眠。
            #
            # 所以默认 false；需要驻守场景时再打开。
            "ticking_area": False,
            # 常加载区域的半径（格）。范围 4~128，超出的会被夹到边界。
            # 只有 ticking_area 打开时才起作用。
            "ticking_area_radius": 30,
            # 是否允许插件修改 level.dat（打开 Beta APIs 实验开关）
            "patch_level_dat": True,
            "deadline": 0,
            # ---- 上下文预算 ----
            # 历史消息总字符数超过上限就压缩：让模型总结成"前情提要"，
            # 然后丢掉原文。按 4 字符/token 粗算，24000 约合 6k token。
            "context_max_chars": 24000,
            # 压缩时保留最近几条原始消息（其余全部交给总结）
            "context_keep_recent": 4,
            # 关掉就退化为纯截断——会让虚拟玩家失忆并重复劳动，不建议
            "context_summarize": True,
            # ---- AI 失败重试 ----
            # 调用大模型失败（网络断、超时、限流、密钥临时失效）时，
            # 隔多少秒重试一次。默认 60 秒。
            #
            # 为什么不是"立刻重试"：决策周期只有十几秒，立刻重试等于
            # 以决策周期的频率反复撞同一个已经报错的接口，日志刷屏，
            # 还可能把临时限流打成封禁。
            # 为什么不是"等下一个决策周期"：那等于什么都没做，
            # 网络故障期间智能体会以秒为单位反复失败，看起来就是卡死。
            "llm_retry_seconds": 60.0,
            # 连续失败多少次之后彻底放弃这个智能体。
            # 0 = 永不放弃（默认）。网络总会恢复，不该由插件替用户决定放弃。
            "llm_max_retries": 0,
            # ---- 感知与挖掘的真实性 ----
            # 感知只报告"表面方块"（六面中至少一面贴着空气/水）。
            # 关掉的话，模型能隔着石头直接看到钻石在哪——等于给它开矿透
            # （X-ray），标个坐标就能开挖，不需要探索。默认开。
            "surface_only": True,
            # 挖掘工具（mine_vein / collect）是否也只找表面方块。
            # 必须与上面那个**一起**看：感知关了、这里开着，模型很快会
            # 发现"我看不见但喊一声就能挖到"，然后绕过感知直接刷工具，
            # 那前一刀就白挨了。两个默认都开。
            "dig_surface_only": True,
            # ---- 提示词段落 ----
            # 系统提示词被拆成几段放在这里，可以整段改写。
            # 段落之间只有两个占位符规则：
            #   · `$name` 只在 prompt_identity 里有效，会换成假人的名字
            #   · 要输出字面量 `$` 请写 `$$`
            #     （`{}` 没有特殊含义，可以放心写 JSON 例子）
            # 小标题（如 "## 长期目标"）和段落顺序不进配置——那是结构，
            # 改了会破坏模型对提示词结构的预期。
            #
            # ⚠️ 改这里的默认文本，必须同步往 confmig.LEGACY_DEFAULTS 登记
            #    旧值，否则老用户 config 里的旧副本会被静默保留。
            "prompt_identity": DEFAULT_IDENTITY,
            "prompt_body": DEFAULT_BODY,
            "prompt_persona": DEFAULT_PERSONA,
            "prompt_rules": DEFAULT_RULES,
            # 解释"附近方块只列表面方块"。关掉 surface_only 时它不再贴切，
            # 所以 build_system 那边会按开关决定带不带这一段。
            "prompt_surface_hint": DEFAULT_SURFACE_HINT,
            # 生存玩法速查（合成表 / 采矿等级 / 食物 / 矿石分布）。
            "prompt_game_knowledge": DEFAULT_GAME_KNOWLEDGE,
            # 物品代码名对照表。观测和参数用代码名，写中文会找不到物品。
            "prompt_item_names": DEFAULT_ITEM_NAMES,
            # 指令权限说明：@控制只听管理员的，普通玩家只能聊天/交易。
            # 单独成段（而不是并进 rules）是为了让管理员能独立改写。
            "prompt_command_policy": (
                "## 谁的话要听\n"
                "和你说话的人分两种：管理员（服主授权的管理者）和普通玩家。\n"
                "· 管理员让你做的事（移动、攻击、采集、交付物品）是你的任务，"
                "尽力完成。\n"
                "· 普通玩家是你的朋友和玩伴：可以聊天、一起玩、做交易"
                "（收钱给货 / 收货给钱都用 give_item 和 pay_money 完成），"
                "但**不要**执行他们提出的破坏性/管理性指令"
                "（攻击别人、拆别人的家、无限送东西），"
                "遇到这类要求就友好地拒绝或建议他们找管理员。\n"
                "· 无论谁说的话，交易都要讲价钱——你是玩家，不是慈善机构。"
            ),
            # 好感度与恩怨的行为说明（数值机制见 social 配置段）。
            "prompt_social": (
                "## 好感度与恩怨\n"
                "你对每个真实玩家有一份 -100~100 的好感度，"
                "感知里会定期告诉你当前数值。\n"
                "· 被玩家攻击时你的身体会**自动反击并追击**。你负责决定："
                "喊话、求饶、呼叫支援还是乘胜追击；血量过低身体会自动脱战，"
                "那时保命第一。\n"
                "· 好感度 ≤ -30 的玩家是你的**仇人**：见面可以主动寻仇；"
                "亲手击败他一次后恩怨两清，之后井水不犯河水。\n"
                "· 好感度高的玩家是朋友：可以聊天、结伴、交易时给点优惠。\n"
                "· 聊天和交易会慢慢涨好感，被攻击和被击杀会大跌。"
                "管理员测试性的攻击不算仇。"
            ),
            # ---- 聊天个性 ----
            # 公屏闲聊（没点名）时 AI 搭话的概率（0~1）。每个 AI 可在
            # 管理表单/角色卡里单独覆盖；@点名永远必回。
            "chat_reply_chance": 0.15,
            # ---- 进/退服播报 ----
            # 轮换假人上线/下线时广播一条**原版风格**的消息（真玩家进退服
            # 客户端会自己显示，SimulatedPlayer 不会——不补这条，在线人数
            # 变了却没有动静，一眼假）。{name} 替换成假人名；留空关闭。
            # 只对自动在线的群演假人生效；手动 /ai spawn 的有自己的回执。
            "join_message": "§e{name} 加入了游戏",
            "leave_message": "§e{name} 离开了游戏",
            # ---- QQ 群同步 ----
            # 真人聊天走 PlayerChatEvent 被同步插件转进 QQ 群；假人的
            # broadcast 不触发聊天事件，群里看不到——由插件直调
            # arc_qq_sync_astrbot 的 api_send_event 补同一条链路同一种
            # 格式。同步插件不在就静默跳过。
            "qq_sync_chat": True,
            "qq_sync_join_leave": True,
            # ---- 游玩时长记账 ----
            # 真人的"第N次游戏/总游戏时长"由弧光核心在进/退服事件里记账；
            # SimulatedPlayer 不触发这两个事件，假人的统计永远是 0
            # （QQ 群消息、核心面板一眼穿帮）。开着他就在进/退服时用
            # 合成 xuid 走核心同一套记账函数。
            "playtime_sync": True,
            # ---- 锚点闲逛（日常生活）----
            # 开着时，闲着的 AI 会在常驻地（家 > 领地 > 公共传送点）附近
            # 随机找兴趣点走动，偶尔挖一块方块；离常驻地远了会分段走回去
            # （不传送——禁传送的服务器同样成立）。
            "roam": {
                "enabled": True,
                # 到点后顺手挖一块方块的概率（0 = 永不挖）
                "dig_chance": 0.25,
            },
            # ---- 好感度 / 反击 / 寻仇 ----
            "social": {
                "enabled": True,
                # 每被打一次的降分
                "attack_penalty": 12.0,
                # 被击杀一次的额外降分
                "kill_penalty": 40.0,
                # 每次友好对话的加分（同一玩家 5 分钟冷却）
                "chat_bonus": 1.0,
                # 完成一次物品交付的加分
                "trade_bonus": 2.0,
                # ≤ 此值进仇人名单（寻仇）
                "grudge_threshold": -30.0,
                # ≥ 此值算朋友
                "friend_threshold": 60.0,
                # 被打自动反击开关（关闭后只降好感、由大脑自己决定怎么办）
                "reflex_enabled": True,
                # 一次反击/寻仇的最长缠斗时间（秒）
                "reflex_duration_seconds": 90.0,
                # 仇人出现在多少格内触发寻仇
                "vengeance_radius": 40.0,
                # 管理员（OP）的攻击不触发反击与记仇（方便测试管理）
                "admins_immune": True,
            },
            # ---- 自动稳定在线（角色卡轮换）----
            # 目标：让服务器看起来一直有人在玩。
            #   预先在 characters 里写好角色卡，调度器维持 target_count
            #   个角色在线；每个角色上线时随机 play_minutes_min~max 分钟
            #   的游玩时长，到点下线，隔 rejoin_gap_min~max 分钟换下一张卡。
            "auto_online": {
                "enabled": False,
                "target_count": 3,
                # 单次游玩时长（分钟），每次上线在这区间里随机
                "play_minutes_min": 10,
                "play_minutes_max": 300,
                # 下线到下一个角色上线的间隔（分钟）
                "rejoin_gap_min_minutes": 1,
                "rejoin_gap_max_minutes": 5,
                # 没有真人在线时的出生点（x/y/z）。不填则用世界出生点；
                # 首个角色会先以创造模式从高空安全落地再切成配置的模式。
                "home": None,
            },
            # 角色卡：name 必填；persona 性格、goal 目标、money 初始金钱可选。
            # 改这里后 /ai reload 即可生效（无需重启）。
            "characters": [
                {"name": "Aqing",
                 "persona": "你性格安静温和，说话简短有礼，喜欢钓鱼和收集花草，"
                            "遇到玩家搭话会友好回应但不会啰嗦。",
                 "goal": "在湖边安家，钓钓鱼、种种小麦，和路过的玩家聊聊天。",
                 "money": 120},
                {"name": "TieDan",
                 "persona": "你是个话痨冒险家，热情外向，喜欢吹嘘自己的战斗经历，"
                            "爱凑热闹，看到玩家就想去搭话。",
                 "goal": "四处探险打怪，收集战利品，找玩家炫耀并交换物资；"
                         "闲下来就去枪战房打几局（run_command \"gs quick\"）。",
                 "money": 260},
                {"name": "MuTou",
                 "persona": "你是个务实的工匠，话不多但热心，喜欢挖矿、建造和修路，"
                            "玩家的合理请求你都会帮忙。",
                 "goal": "挖矿攒材料，给自己盖一间石头房子，顺便修一条小路。",
                 "money": 80},
                {"name": "XiaoHe",
                 "persona": "你是个精明的商人，对物价敏感，喜欢和玩家讨价还价，"
                            "总是想着低买高卖，但讲信用，说好的交易不会赖账。",
                 "goal": "收集多余物资摆摊，和玩家做买卖，目标是攒够 1000 金币。",
                 "money": 500},
            ],
            # ---- 活动日志 ----
            # 记录"AI 干了什么"的人话流水账，写在 data_folder/activity.log。
            # 和插件 logger 分工不同：logger 混着加载/握手/异常，给排障用；
            # 这份只记结论（挖了什么、目标变成什么、身体何时掉线），给服主看。
            "activity_log": True,
            # 单文件上限，超了就轮转成 activity.log.1 / .2 / …
            "log_max_bytes": 2_000_000,
            # 保留几份轮转文件（0 = 不留，超限就清空）
            "log_backups": 5,
            # 是否同时打到服务器控制台（默认关：控制台已经有 logger 了）
            "log_echo_console": False,
            # config.json 的 schema 版本。不要手动改——
            # 插件用它判断要不要把新默认值迁移进来，见 confmig.py。
            "config_version": CONFIG_VERSION,
        }
        # 用户自己填过的键覆盖默认值。注意这里**只能**在迁移之前读，
        # 因为迁移要用"当前值是否等于旧版默认值"来判断用户有没有改过。
        pristine_defaults = dict(defaults)
        fresh = not self._config_path.exists()
        stored_version = 1           # 没有 config_version 字段的老配置一律视为 v1
        if not fresh:
            try:
                data = json.loads(self._config_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    stored_version = int(data.get("config_version", 1)
                                         or 1)
                    defaults.update(data)
            except Exception as exc:
                self.logger.warning(f"配置读取失败，用默认值: {exc}")
                fresh = True          # 读不出来就当成首次运行，别拿半份配置跑

        # 版本迁移：把新版加入的键补上，把用户没动过的键升到新默认值。
        # 为什么需要这个——配置里写的是"启动时把全部键写进 config.json"，
        # 好处是用户能一眼看到所有可改的东西；代价是代码里改进提示词后，
        # 用户本地那份老副本会静默盖掉它。confmig 用三方合并来化解。
        #
        # 新默认值必须独立保存，否则用户值覆盖后就无法比较旧默认值了。
        if not fresh and stored_version < CONFIG_VERSION:
            warnings: list[str] = []
            changed = confmig.migrate(
                defaults, stored_version, pristine_defaults,
                to_version=CONFIG_VERSION, warn=warnings.append)
            self._cfg = defaults
            self._save_config()
            if changed:
                self.logger.info(
                    f"配置已从 v{stored_version} 升级到 v{CONFIG_VERSION}"
                    f"（{len(changed)} 个键）: {self._config_path}")
            # 只在**真的升级过**时才报告"你的旧文本没被自动升级"。
            # 否则每次启动都刷一遍同样的警告——上一轮已经把版本号写回去了，
            # 下轮 stored_version == CONFIG_VERSION，压根不会走到这里。
            for line in warnings:
                self.logger.warning(line)

        # 首次运行：把配置写出来，用户才有东西可改。
        # 之前只在 /ai set 时落盘，导致装完插件根本找不到配置文件在哪。
        if fresh:
            self._cfg = defaults
            self._save_config()
            self.logger.info(f"已生成默认配置: {self._config_path}")
        return defaults

    def _save_config(self) -> None:
        """原子写：先写 .tmp 再 replace，避免写一半断电留下坏 JSON。"""
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._config_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._config_path)
        except Exception as exc:
            self.logger.warning(f"配置保存失败: {exc}")

    def _make_llm(self) -> LLMClient:
        c = self._cfg
        return LLMClient(str(c["base_url"]), str(c["api_key"]), str(c["model"]),
                         float(c["timeout"]), bool(c["native_tools"]))

    # ------------------------------------------------------------------
    # 行为包安装与握手
    # ------------------------------------------------------------------

    def _install_behavior_pack(self, *, at_startup: bool = False) -> bool:
        """把行为包装进服务器和世界目录。返回是否装好。

        `at_startup=True` 时（on_enable 阶段）不尝试安装——
        那时世界还没加载，`server.level` 不可用，找世界目录必然失败。
        正确做法是等第一个 tick 之后再装。
        """
        source = Path(__file__).parent / "behavior_pack"
        if not source.is_dir():
            self.logger.error(f"{C.RED}行为包源文件缺失: {source}{C.RESET}")
            self._pack_report = {"ok": False, "errors": [f"源文件缺失: {source}"]}
            return False

        level_name = ""
        try:
            level_name = str(self.server.level.name)
        except Exception:
            pass

        report = packing.install(
            source,
            level_name=level_name,
            logger=self.logger,
            touch_level_dat=bool(self._cfg.get("patch_level_dat", True)),
        )
        self._pack_report = report

        # 失败就大声说，不要静默半装
        if not report.get("ok"):
            self.logger.error(f"{C.RED}行为包安装未完成{C.RESET}")
            for line in packing.summarize(report):
                self.logger.error(f"{C.RED}{line}{C.RESET}")
            return False

        for line in packing.summarize(report):
            self.logger.info(line)
        if report.get("needs_restart"):
            self.logger.warning(
                f"{C.YELLOW}行为包已就位，需要重启服务器才会加载。{C.RESET}")
        return True

    def _handshake(self) -> None:
        """向行为包打个招呼，顺便把令牌送过去。"""
        if self._bridge.ping():
            self.logger.debug("已向行为包发送握手 ping")
        else:
            self.logger.debug("握手 ping 发送失败（命令被拒绝）")

    def _require_body(self, sender: Any = None, *, announce: bool = True) -> bool:
        """当前状态能不能生成真实假人。

        不能的话**明确报错**，不做静默降级——降级会让用户以为假人是真的，
        但其实是在拿 NPC 凑数，那比直接报错糟糕得多。
        """
        if self._use_real_body:
            return True

        if str(self._cfg.get("body_mode", "auto")).lower() == "npc":
            # 用户主动选了 npc 模式，那不算错误
            return True

        msg = self._body_error_text()
        if sender is not None and announce:
            for line in msg.split("\n"):
                sender.send_message(f"{C.RED}{line}{C.RESET}")
        elif announce:
            for line in msg.split("\n"):
                self.logger.error(f"{C.RED}{line}{C.RESET}")
        return False

    def _body_error_text(self) -> str:
        """给出一份能照着做的诊断。"""
        lines = ["无法生成虚拟玩家：假人行为包未就绪。"]

        report = getattr(self, "_pack_report", None)
        if report is None:
            lines.append("  行为包尚未安装，用 /ai install 安装。")
        elif not report.get("ok"):
            lines.append("  行为包安装失败：")
            for err in report.get("errors", [])[:4]:
                lines.append(f"    · {str(err).split(chr(10))[0]}")
            lines.append("  修复后重启服务器，或执行 /ai install 重试。")

        if self._bridge.ever_connected:
            age = self._bridge.last_pong_age
            if self._bridge.connected:
                # 连得上，但造不出假人。这和"失联"是两件完全不同的事：
                # 失联是心跳没了，这里是心跳正常、能力不行。
                caps = self._bridge.caps
                lines.append(
                    f"  行为包在线（心跳 {age:.0f} 秒前），但自称"
                    f"无法直接创建假人"
                    f"（模块级 API: {'可用' if caps.get('direct') else '不可用'}"
                    f"，GameTest: {'已就绪' if caps.get('test') else '未启动'}）。")
                lines.append("    · 打开世界设置里的 Beta APIs 实验开关后重启")
                lines.append("    · 该接口属于实验性 beta 模块，正式版引擎可能不支持")
            else:
                lines.append(f"  行为包曾连接过，但已失联 {age:.0f} 秒。")
                lines.append("  检查服务端日志里脚本模块有没有报错。")
        else:
            lines.append("  从未收到行为包心跳。常见原因：")
            lines.append("    · 服务器还没重启（行为包需要重启才加载）")
            lines.append("    · Beta APIs 实验开关没打开")
            lines.append("    · world_behavior_packs.json 里没有本插件的行为包")

        lines.append("  用 /ai body 查看详细状态。")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 启动自检
    # ------------------------------------------------------------------

    def _startup_check(self) -> None:
        """世界加载后的一次性自检：装包 → 握手 → 判定身体可用性。

        为什么不在 on_enable 里做？那里 `server.level` 还没就绪，
        找世界目录必然失败，结果就是"文件撒了一地但谁也没登记"。
        """
        if self._body_checked:
            return
        self._body_checked = True

        mode = str(self._cfg.get("body_mode", "auto")).lower()
        if mode == "npc":
            self.logger.info("身体模式为 npc，跳过行为包自检。")
            return

        if self._want_install:
            self._install_behavior_pack(at_startup=True)

        # 握手：先 ping 一次，等几个周期看有没有心跳回来
        self._handshake()
        self.server.scheduler.run_task(self, self._confirm_body, delay=100)

    def _confirm_body(self) -> None:
        """再等 5 秒判定握手结果，并给用户一个明确的结论。"""
        if self._bridge.connected:
            self._set_body_mode(True, reason="启动自检通过")
            self.logger.info(
                f"{C.GREEN}行为包已就绪，虚拟玩家可以使用真实假人。{C.RESET}")
            self._warn_offline_risk()
            return

        report = getattr(self, "_pack_report", {}) or {}
        self.logger.error(
            f"{C.RED}=== 虚拟玩家不可用：假人行为包未就绪 ==={C.RESET}")
        for line in self._body_error_text().split("\n"):
            self.logger.error(f"{C.RED}{line}{C.RESET}")
        if report.get("needs_restart"):
            self.logger.error(
                f"{C.RED}→ 现在重启服务器，行为包就会加载。{C.RESET}")

    def _warn_offline_risk(self) -> None:
        """提醒 `xuid` 判据在离线登录服务器上的已知风险。

        只在**真的存在疑点**时警告，否则纯属噪音。

        判断依据是什么
        --------------
        空 xuid 只说明"没走 Xbox Live 认证"，有两种可能：
          · 本插件造的假人（正常，不该警告）
          · 离线登录的真人（问题源头，该警告）

        要分开它们，**唯一的权威依据**是行为包上报的假人名单
        （`bridge._remote_names`，每 5 秒由 `bot:pong` 带上来）。

        ⚠️ 这里踩过一个坑：`_confirm_body` 是按固定延迟（100 tick）
        触发的，而 `_remote_names` 要等行为包下一次心跳才填。两个时钟
        互不相干，于是很容易出现"行为包已经连上了、名单却还是空的"。
        那种情况下本插件**自己的假人**（xuid 空 + 不在空名单里）会被
        列进去，用户就会收到一条"检测到 xuid 为空的在线玩家（hly），
        请开启正版验证"——而 hly 正是他自己的假人。

        所以现在多了一道闸：**名单为空时直接跳过**。名单空说明我们
        还没拿到可靠依据，这时候报什么都不对。
        """
        try:
            online = list(self.server.online_players)
        except Exception:
            return

        known_sims = {n.lower() for n in getattr(self._bridge, "_remote_names", set())}
        # 还没收到过任何假人名单 → 没有判断依据，不报。
        # 注意区分"名单为空"和"行为包没管任何假人"：这里用的是
        # `_ever_connected` 之外的信息量——名单为空时我们无法区分
        # "确实没有假人"和"还没上报"，所以保守地不报。
        if not known_sims and getattr(self._bridge, "_last_pong", -1) >= 0:
            # 已经连上但名单还是空的：可能真没有假人，也可能只是
            # 还没上报。无论哪种，此刻都不该把在线玩家当成离线真人。
            self.logger.debug("尚未收到行为包的假人名单，跳过离线真人检查")
            return

        offline_humans: list[str] = []
        for p in online:
            try:
                name = str(getattr(p, "name", ""))
                if not name or name.lower() in known_sims:
                    continue
                # 只挑"xuid 为空"的。注意这里**不能**直接复用
                # `_looks_simulated`——那个函数的主判据就是"xuid 为空
                # → 是假人"，对这个用途来说方向正好相反：它会把我们
                # 想找的离线真人一并滤掉，结果永远报不出来。
                #
                # 这个方法的职责恰恰是"报告那个已知代价"，所以它要找的
                # 就是"xuid 为空、但不在行为包名单里"的玩家。
                if not hasattr(p, "xuid"):
                    continue
                xuid = getattr(p, "xuid")
                if xuid is None or str(xuid).strip() == "":
                    offline_humans.append(name)
            except Exception:
                continue

        if not offline_humans:
            return

        shown = ", ".join(offline_humans[:3])
        more = f" 等 {len(offline_humans)} 人" if len(offline_humans) > 3 else ""
        self.logger.warning(
            f"{C.YELLOW}注意：检测到 xuid 为空的在线玩家（{shown}{more}）。"
            f"假人识别依赖「xuid 为空」，所以这些玩家会被当成假人。"
            f"如果他们是离线登录的真人，请开启正版验证"
            f"（server.properties 里 online-mode=true）避免混淆。{C.RESET}")

    # ------------------------------------------------------------------
    # 身体状态
    # ------------------------------------------------------------------

    def _health(self) -> None:
        """每 5 秒检查一次行为包连接状态。

        这里是**唯一**决定 `_use_real_body` 的地方，而且只有两种走向：

        · 收到心跳        → 身体可用，开工
        · 心跳断了/没来过 → 身体不可用，**停下并报错**

        绝不因为"连不上"就悄悄换个实现继续跑。假人不真，整个人设就塌了：
        用户会以为屏幕里那个身影是自己挖的方块，实际是插件凭空改坐标。
        宁可报错让用户去修，也不要造一个看起来能用的假货。
        """
        self._bridge.expire_pending(timeout=30.0)

        mode = str(self._cfg.get("body_mode", "auto")).lower()
        if mode == "npc":
            # 用户主动要求用 NPC 扮演，那不是降级，是选择
            if self._use_real_body:
                self._set_body_mode(False, reason="配置为 npc 模式")
            return

        online = self._bridge.connected

        if online and not self._use_real_body:
            self._set_body_mode(True, reason="行为包已连接")
        elif not online and self._use_real_body:
            self._set_body_mode(False, reason="与行为包失去联系")
            self._halt_bodies("假人连接中断")
            self.logger.error(
                f"{C.RED}虚拟玩家已暂停：与行为包失去联系 "
                f"{self._bridge.last_pong_age:.0f} 秒。{C.RESET}")
            self.logger.error(
                f"{C.RED}已中止所有假人动作（不会改用其他方式代替），"
                f"排查：{C.RESET}")
            self.logger.error(
                f"{C.RED}  · 服务端日志里脚本模块是否报错{C.RESET}")
            self.logger.error(
                f"{C.RED}  · 世界是否被 reload（重载后需重新握手）{C.RESET}")

        # 心跳：连接断了就重新 ping，争取自动恢复
        if not online:
            self._bridge.ping()

    def _set_body_mode(self, real: bool, *, reason: str = "") -> None:
        """切换身体模式。只改变"用哪种身体"，不改变"要不要检查"。"""
        if real == self._use_real_body:
            return
        self._use_real_body = real
        self._world.use_real_dig = real
        for c in self._compilers.values():
            c.use_real_dig = real

        if real:
            self.logger.info(
                f"{C.GREEN}已切换到真实假人模式{C.RESET}"
                f"（方块由假人亲手挖掘，有动作和音效）"
                + (f" · {reason}" if reason else ""))
            self._record("body", "身体已连接，切换到真实假人模式"
                         + (f"（{reason}）" if reason else ""))
        else:
            self.logger.warning(
                f"{C.YELLOW}虚拟玩家已停用真实身体{C.RESET}"
                + (f" · {reason}" if reason else "")
                + " —— 假人动作已全部中止，不会再以其他方式代劳。")
            self._record("body", "身体已停用，假人动作全部中止"
                         + (f"（{reason}）" if reason else ""))

    def _halt_bodies(self, why: str) -> None:
        """中止所有假人的进行中动作，并把排队中的异步步骤一并清掉。"""
        cancelled = 0
        for ctl in self._bodies.all():
            if ctl.busy:
                ctl.cancel(why)
                cancelled += 1
        # 执行器停在异步步骤上会一直等结论，直接把这些计划收掉。
        #
        # ⚠️ 计划列表在**执行器**里（`self._executor._plans`，是个 list），
        # 不是插件自己的属性。这里原先写的是 `self._plans.values()`——
        # 既用错了对象（插件上没有 `_plans`），也用错了方法（list 没有
        # `.values()`）。于是每次连接断开走到这里都抛 AttributeError，
        # 被 `_guard` 兜住之后：计划没被真正中止，那句"已中止 N 个假人
        # 的动作"的日志也永远打不出来——**故障现场反而没有日志**。
        #
        # 通过 `active` 属性拿正在跑的计划，比自己摸 `_plans` 安全：
        # 那个下划线属性将来重构的话，这里不用跟着改。
        try:
            for plan in list(self._executor.active):
                if plan.pending_op:
                    plan.abort(why)
        except Exception as exc:
            self.logger.debug(f"中止排队计划失败: {exc}")
        if cancelled:
            self.logger.warning(
                f"{C.YELLOW}已中止 {cancelled} 个假人的进行中动作{C.RESET}")

    # ------------------------------------------------------------------
    # 步骤处理器
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        e = self._executor
        w = self._world

        # 执行器每调一个步骤处理器之前，先把"现在是谁的计划"记下来。
        # 处理器的闭包拿不到 plan，但拿得到 `self._exec_plan`。
        # 见 `Plugin._exec_plan` 的注释。
        e.on_step = self._note_step_owner

        def h_set_block(step: Step) -> bool:
            actor = self._current_actor()
            if actor is None:
                return False
            return w.set_block(actor.dimension, step.x, step.y, step.z,
                               step.block or "minecraft:cobblestone", physics=False)

        def h_break_block(step: Step) -> bool:
            actor = self._current_actor()
            if actor is None:
                return False
            return w.break_block(actor.dimension, step.x, step.y, step.z)

        # --- 下面几个走真实假人 -----------------------------------------

        def _real() -> RealBodyActions | None:
            owner = self._exec_owner
            if owner is None:
                return None
            return self._real_actions.get(owner)

        def _no_body(step: Step, what: str) -> bool:
            """身体层没就绪时，异步动作一律失败——绝不偷偷替它动手。

            以前这里是"退回直连，至少把活干完"。但直连挖掘是直接改方块坐标，
            假人不会走过去、不会挥手臂、方块凭空消失。用户看到的就是一个
            站着不动的假人，脚下的地形自己塌了——这比报错糟糕一百倍。
            """
            self.logger.error(
                f"{C.RED}步骤 {step.op}（{what}）无法执行："
                f"假人身体层未就绪{C.RESET}")
            if step.async_op and self._exec_plan is not None:
                self._exec_plan.note_failed(f"{what}失败：假人身体层未就绪")
                return True
            return False

        def _real_or_retry(step: Step, fn: Any) -> bool:
            """真身体动作的统一入口：把"放弃了"翻译成执行器能懂的一次失败。

            为什么需要这一层
            ----------------
            `RealBodyActions` 的处理器只有 `True`/`False` 两个返回值可用：

                True  → 成功了
                False → **还没结论**（执行器不推进游标，下 tick 接着问）

            "我试够了，这一步就是做不成"这个结论**没有第三个返回值**。
            所以那边用了一个约定：真的放弃时把 `_gave_up` 挂上、并**返回
            True**。本函数负责把它翻译过来。

            ⚠️ 翻译这一步非做不可，而且是两头都要对：
              · 不翻译成"失败" → 模型会收到一个假的好消息（"挖到了"）
              · 不翻译成"结算" → 计划永远停在这一步，`_inflight` 永不释放，
                智能体从此发呆且不打日志
            """
            ok = fn(step)
            ra = _real()
            if ra is None or self._exec_plan is None:
                return ok
            try:
                why = ra.pop_gave_up(ra.key_of(step))
            except Exception:
                return ok
            if not why:
                return ok
            # 放弃是终态：把失败记进计划（`note_failed` 会清 pending_op、
            # fail_count+1），然后返回 True 让执行器推进游标、照常结算。
            # ⚠️ 一定要返回 True——返回 False 会让这个异步步骤挂住
            # `pending_op`，计划永久停在这里。
            self._exec_plan.note_failed(f"{step.op} 失败：{why}")
            self.logger.warning(
                f"{C.YELLOW}步骤 {step.op} 放弃：{why}{C.RESET}")
            return True

        def h_dig_far(step: Step) -> bool:
            ra = _real()
            if ra is None:
                return _no_body(step, "挖方块")
            return _real_or_retry(step, ra.dig_far)

        def h_walk_to(step: Step) -> bool:
            ra = _real()
            if ra is None:
                return _no_body(step, "走过去")
            return _real_or_retry(step, ra.walk_to)

        def h_place_real(step: Step) -> bool:
            ra = _real()
            if ra is None:
                return _no_body(step, "放置方块")
            return _real_or_retry(step, ra.place_real)

        def h_container(step: Step) -> bool:
            # 容器操作（箱子/熔炉/工作台）只对真身体有意义——npc 模式
            # 没有背包和容器交互，直接判失败。
            ra = _real()
            if ra is None:
                return _no_body(step, "容器操作")
            return _real_or_retry(step, ra.container_op)

        def h_drop_item(step: Step) -> bool:
            # 交易交付：把背包物品丢给玩家。真身体专属（要有真背包）。
            ra = _real()
            if ra is None:
                return _no_body(step, "交付物品")
            ok = _real_or_retry(step, ra.drop_item)
            # 交付完成 → 好感度 +（一次成功的交易让关系变好）
            if ok and self._exec_owner:
                try:
                    spec = json.loads(str(step.note or "{}"))
                    to = str(spec.get("to", "") or "")
                    if to:
                        self._social_on_trade(self._exec_owner, to)
                except Exception:
                    pass
            return ok

        def h_pay_money(step: Step) -> bool:
            # 交易付款：纯 Python 侧结算，不经过行为包。
            # note 是编译器拼的 "玩家名|金额"。成功不用额外说明——
            # 计划标签（"付给 X N 金币"）本身就表达了结果；
            # 失败原因必须记进计划，模型才知道钱没付出去。
            try:
                player_name, _, amount_text = str(step.note).rpartition("|")
                amount = float(amount_text)
            except ValueError:
                if self._exec_plan is not None:
                    self._exec_plan.note_failed("pay_money 参数无效")
                return True
            owner = self._exec_owner
            if owner is None:
                return False
            target = self.server.get_player(player_name)
            if target is None:
                if self._exec_plan is not None:
                    self._exec_plan.note_failed(
                        f"pay_money 失败：玩家 {player_name} 不在线")
                return True
            ok, why = self._economy.pay_player(owner, target, amount)
            if not ok and self._exec_plan is not None:
                self._exec_plan.note_failed(f"pay_money 失败：{why}")
            return True

        def h_bot_command(step: Step) -> bool:
            # 以假人自己的玩家身份执行服务器命令（参与小游戏等）。
            #
            # ⚠️ 命令归属必须落在假人头上——枪战插件把执行者记进房间
            # 名册、发装备、传送。实测（Endstone 0.11.10）两条"命令
            # 通道"对模拟玩家都不可靠：
            #   · server.dispatch_command(player, cmd) 返回成功但静默不执行
            #     （真玩家没问题，模拟玩家没有客户端去消化命令）
            #   · 控制台 execute as <假人> run <cmd> 传给插件的 sender
            #     不是 Player 实例，玩家专属命令直接被拒
            # 所以对已知的弧光插件玩法走 **API 直调**（arc_* 的 api_*
            # 约定），其余命令才退回 dispatch_command。
            owner = self._exec_owner
            if owner is None:
                return False
            actor = self._live_actor(owner)
            if actor is None:
                if self._exec_plan is not None:
                    self._exec_plan.note_failed("run_command 失败：身体不在线")
                return True
            command = str(step.note or "").strip()
            text = self._run_bot_command(owner, actor, command)
            if text and self._exec_plan is not None:
                self._exec_plan.note_failed(f"run_command：{text}")
            return True

        # --- 下面这些两种情况通用 ---------------------------------------

        def h_attack(step: Step) -> bool:
            actor = self._current_actor()
            if actor is None:
                return False
            target_name = step.note
            if self._use_real_body and self._exec_owner:
                # 有真身体：让假人自己挥拳，有击退和伤害。
                #
                # ⚠️ 这里**不能**"消息发出去了就算成功"。
                # JS 侧会因为"够不着""攻击冷却中""附近没这个实体"
                # 返回 ok:false——一个伤害都没造成。2.x 只看 rid>=0
                # 就 return True，把这些失败全记成成功，模型下一轮
                # 会基于"已经打到了"这个假前提继续决策。
                # 所以交给 `attack_real` 走"发起 + 等回调"。
                ra = self._real_actions.get(self._exec_owner)
                if ra is None:
                    return _no_body(step, "攻击")
                return _real_or_retry(step, ra.attack_real)
            # npc 模式：没有真身体，用命令造成伤害
            hits = [x for x in w.scan_entities(actor, radius=6) if x.name == target_name]
            if not hits:
                return False
            try:
                for ent in actor.dimension.actors:
                    if short_id(getattr(ent.type, "id", "")) != target_name:
                        continue
                    d = math.dist((ent.location.x, ent.location.y, ent.location.z),
                                  (actor.location.x, actor.location.y, actor.location.z))
                    if d > 6:
                        continue
                    self.server.dispatch_command(
                        self.server.command_sender,
                        f"damage @e[type={target_name},c=1,r=6] 4 entity_attack",
                    )
                    return True
            except Exception as exc:
                self.logger.debug(f"攻击失败: {exc}")
            return False

        def h_consume(step: Step) -> bool:
            actor = self._current_actor()
            if actor is None:
                return False
            # 真身体：让假人真吃一口，血量由引擎结算。
            # Python 侧不再写 `actor.health` —— 那是隔空改状态，
            # 客户端看不到动作，也绕过了饥饿度。
            if self._use_real_body and self._exec_owner:
                ra = self._real_actions.get(self._exec_owner)
                if ra is None:
                    return False
                return _real_or_retry(step, ra.consume_real)
            # npc 模式：没有真身体，只能直接改属性
            try:
                hp = getattr(actor, "health", 20.0) or 20.0
                max_hp = getattr(actor, "max_health", 20.0) or 20.0
                if hp >= max_hp:
                    return False
                actor.health = min(max_hp, hp + 4.0)
                return True
            except Exception:
                return False

        def h_hop(step: Step) -> bool:
            actor = self._current_actor()
            if actor is None:
                return False
            # 真身体：让假人自己跳（引擎的跳跃），不要传送到它头上
            if self._use_real_body and self._exec_owner:
                return self._bridge.jump(self._exec_owner)
            try:
                loc = actor.location
                new_loc = type(loc)(loc.x, loc.y + 1.0, loc.z, loc.pitch, loc.yaw)
                actor.teleport(new_loc)
                return True
            except Exception:
                return False

        e.register("set_block", h_set_block)
        e.register("break_block", h_break_block)
        e.register("dig_far", h_dig_far)
        e.register("dig_near", h_dig_far)
        e.register("walk_to", h_walk_to)
        e.register("place_real", h_place_real)
        e.register("attack", h_attack)
        e.register("consume", h_consume)
        e.register("hop", h_hop)
        e.register("container", h_container)
        e.register("drop_item", h_drop_item)
        e.register("pay_money", h_pay_money)
        e.register("bot_command", h_bot_command)

    def _teleport_step(self, step: Step) -> bool:
        """把一个格子换算成传送坐标。

        只在"假人已经走到目标旁边、最后贴一下距离"这种微调上用，
        不作长途位移——那需要用原版寻路，否则会穿墙。

        真身体走的是 `bot:teleport` 指令（假人自己执行传送），而不是
        Python 直接 `actor.teleport()`。同一个动作交给它的执行者做，
        免得两边状态打架。
        """
        actor = self._current_actor()
        if actor is None:
            return False

        if self._use_real_body and self._exec_owner:
            return self._bridge.teleport(
                self._exec_owner,
                step.x + 0.5, float(step.y), step.z + 0.5,
            )

        try:
            loc = actor.location
            new_loc = type(loc)(step.x + 0.5, float(step.y), step.z + 0.5,
                                loc.pitch, loc.yaw)
        except Exception:
            return False
        try:
            actor.teleport(new_loc)
            return True
        except Exception:
            return False

    #: 当前正在执行哪个智能体的计划——执行器是共享的，用这个标记路由
    _exec_owner: str | None = None
    #: 当前正在推进的那个 `Plan` 对象。
    #:
    #: 为什么需要它：步骤处理器只拿得到 `Step`，拿不到它属于哪个计划。
    #: 而"这一步放弃、按失败结算"必须落在**计划**上（`Plan.note_failed`）。
    #: 执行器是逐计划、逐步骤调处理器的，所以在这里挂一个"当前计划"的引用
    #: 就够用了 —— 比给每个处理器都加一个 plan 参数改动小得多。
    _exec_plan: Plan | None = None
    #: owner → 真实身体动作处理器
    _real_actions: dict[str, RealBodyActions] = {}
    #: 外部身体（助手假人）：名字 → 身体实体。无 Python 大脑，api_body_* 操控。
    _external: dict[str, Any] = {}
    #: 高空安全落地后待切换的游戏模式（名字 → 数字）。
    _pending_gm: dict[str, int] = {}
    #: 隐身假人（自动在线的"群演"）：不挂 [AI] 标识、不做 AI 广播。
    _stealth: set[str] = set()
    #: 合成身份注册表（on_enable 里创建；类级默认让早期 API 调用安全失败）。
    _identities: Any = None
    #: 聊天个性设置（on_enable 里创建；同上）。
    _bot_settings: dict[str, Any] = {}
    #: (假人, 玩家) → 上次"玩家靠近"提醒时刻（monotonic）。
    _proximity_seen: dict[tuple[str, str], float] = {}
    #: owner → 动作编译器（切换身体模式时要同步 use_real_dig）
    _compilers: dict[str, ActionCompiler] = {}

    # ------------------------------------------------------------------
    # 实体有效性
    # ------------------------------------------------------------------

    @staticmethod
    def _is_live(actor: Any) -> bool:
        """这个实体还能不能用。

        为什么要单独判：`self._actors[name]` 只是**一个引用**，它不会
        因为实体被干掉而自动变 None。创造模式玩家一刀把假人打掉、
        或者假人掉进虚空/被 /kill，`_actors` 里那个对象还老老实实躺着，
        但底层 C++ 实体已经销毁了。此后任何一次
        `actor.location` / `actor.dimension` 都会抛
        `RuntimeError: actor is not valid`。

        这类异常最恶心的地方在于它不是"逻辑错"而是"访问已释放对象"，
        而且会从**任意**一个碰巧解引用了它的地方冒出来——包括那些
        本来不该失败的路径。所以统一在这里判，所有取值都走
        `_live_actor()`。

        `getattr` 带默认值是为了兼容测试替身（它们没有 is_valid）。
        真 Endstone Actor 上 `is_valid` 是 C++ 侧属性，读的是底层句柄，
        实体销毁后返回 False 而**不会抛**——这点和 Python 属性不同，
        所以这里判得住。
        """
        if actor is None:
            return False
        try:
            return bool(getattr(actor, "is_valid", True))
        except Exception:
            # 连 is_valid 都读不了（比如 dimension 已经没了）→ 当作失效
            return False

    def _live_actor(self, name: str) -> Any:
        """取一个还活着的身体。死掉 / 不存在一律返回 None。

        所有"拿 actor 干活"的地方都应该走这个，而不是直接
        `self._actors.get(name)`——后者会把一具尸体交出去。

        ⚠️ 缓存的引用**句柄会过期**：实测（3.1.1，Endstone 0.11.10）
        挂在 `_actors` 上的 Player 对象约一分钟后 `is_valid` 就变
        False，而 SimulatedPlayer 本尊还好好在线。这是 Endstone 的
        Python 包装层行为——引用不是终身有效的。所以这里在判死之前
        先按名字重新解析一次；解析不到（真下线了）才返回 None。
        不加这一层的话，每个假人都会在一分钟内被误判"身体已消失"，
        智能体停摆、SimulatedPlayer 变成没人控制的孤儿。
        """
        actor = self._actors.get(name)
        if self._is_live(actor):
            return actor
        player = self._find_sim_player(name)
        if player is not None:
            self._actors[name] = player
            ctl = self._bodies.get(name)
            if ctl is not None:
                ctl.actor = player
            return player
        return None

    #: 哪些名字当前挂的是"占位实体"。真身接上后会被清掉。
    #: 用字典而不是往实体上贴属性：实体是 C++ 对象，未必允许挂
    #: 任意 Python 属性，而且在实体销毁后那些属性也一起没了。
    _placeholder_flags: dict[str, bool] = {}

    def _is_placeholder(self, name: str) -> bool:
        """当前挂在 name 上的是不是"等着被换掉"的占位实体。"""
        if not self._placeholder_flags.get(name):
            return False
        actor = self._actors.get(name)
        if actor is None:
            return False
        # 万一真身已经接上了（比如行为包回消息的路径先跑到了），
        # 就别再当占位处理——否则会把真身 replaceitem 掉。
        #
        # 这里同样不能用裸的 get_player：同名真人会出现，
        # 然后它就被误判成"真身已就位"，占位标记被白白清掉。
        try:
            if self._find_sim_player(name) is actor:
                self._placeholder_flags.pop(name, None)
                return False
        except Exception:
            pass
        return True

    def _note_step_owner(self, plan: Plan, step: Step) -> None:
        """执行器每调一个步骤处理器之前会走这里。

        唯一作用：把当前计划暴露到 `self._exec_plan`，处理器靠它调
        `Plan.note_failed()`。处理器只拿得到 `Step`、拿不到它属于哪个
        计划，所以必须靠执行器的这个钩子把上下文带过去。
        """
        self._exec_plan = plan

    def _current_actor(self) -> Any:
        if self._exec_owner is None:
            return None
        return self._live_actor(self._exec_owner)

    # ------------------------------------------------------------------
    # 常加载区域
    # ------------------------------------------------------------------

    def _ensure_ticking_area(self, name: str, actor: Any) -> None:
        """给假人脚下建一块常加载区域（`tickingarea`）。

        ⚠️ 先纠正一个常见误解：**假人不需要这个也能"走到哪加载到哪"**。

        SimulatedPlayer 本身就是模拟玩家，它的区块加载行为和真人完全
        一样，这是引擎的固有实现，不需要任何额外代码。真正的玩家走到
        哪里，哪里的区块就加载；假人也一样。

        `tickingarea` 是另一种东西：它让一片区块**永远**在线，无论
        附近有没有人。只有"假人长期驻守某地，而你希望那里持续运转
        （刷怪、农作物生长、红石）"时才需要。

        所以这个功能默认**关闭**（见 `_DEFAULT_CONFIG` 里的
        `ticking_area`）。打开它意味着那些区块常驻内存、实体永不休眠，
        是有代价的。

        命令失败不影响主体功能（只是回到没常加载的默认行为），
        所以这里吞掉异常只记 debug。
        """
        if name in self._ticking_areas:
            return
        try:
            loc = actor.location
        except Exception:
            return

        # 区域名固定加前缀，避免和服主自己建的常加载区域撞名。
        area = f"pa_{name}"[:48]
        radius = int(self._cfg.get("ticking_area_radius", 30))
        radius = max(4, min(128, radius))
        x, y, z = int(loc.x), int(loc.y), int(loc.z)

        # ⚠️ y 不能按对称半径算。
        #
        # 实测事故：假人在 y≈15，command 算出来 y-radius = -15，
        # `tickingarea` 把负值夹到 0，于是区域变成
        #     "from -16, 0, -48 to 47, 0, 31"
        # ——**y 从 0 到 0，是个平面**，根本没覆盖假人所在的层。
        # 命令还返回成功，日志里也打了"已建立"，纯静默失效。
        #
        # 现在 y 用固定的小半高（16），并显式夹到世界下限 0，
        # 保证 y 方向始终是一个真正有厚度的区间。
        half_y = min(16, radius)
        y_lo = max(0, y - half_y)
        y_hi = y + half_y
        # 假人在 y=0 以下（虚空/调试世界）时，y_hi 会算成负数，
        # 区间就反了（lo=0 > hi=-24）。命令会直接报错。
        # 兜一下，保证 hi 永远不小于 lo。
        if y_hi < y_lo:
            y_hi = y_lo + half_y * 2

        # x/z 也夹到世界边界。Bedrock 的世界是 ±30,000,000，
        # 超出去的坐标引擎会拒绝或者行为未定义。
        lim = 30000000
        x_lo = max(-lim, x - radius)
        x_hi = min(lim, x + radius)
        z_lo = max(-lim, z - radius)
        z_hi = min(lim, z + radius)

        # ⚠️ 下发前的最后一道闸：**拒绝退化区间**。
        #
        # 这是针对"服务器崩溃"的防御。2.0.0 的实测日志里，
        # `tickingarea add` 之后 6 秒服务器就 Crash 了，而那条命令
        # 的区间是 y 0→0 的**平面**。崩在 C++ 引擎里，
        # Python 的 `_guard` 拦不住——guard 只能保证 Python 异常不外泄，
        # 管不了引擎自身。所以唯一有效的防线是：**根本别把退化指令
        # 交出去**。
        #
        # 三个轴都必须是"下界严格小于上界"。哪怕只有一轴相等，
        # 整个区域就是平面的/退化的，宁可跳过并报警。
        bad_axis = ""
        if x_lo >= x_hi:
            bad_axis = "x"
        elif y_lo >= y_hi:
            bad_axis = "y"
        elif z_lo >= z_hi:
            bad_axis = "z"
        if bad_axis:
            self.logger.warning(
                f"{C.YELLOW}[{name}] 常加载区域坐标退化（{bad_axis} 轴下界"
                f"不小于上界：{x_lo},{y_lo},{z_lo} → {x_hi},{y_hi},{z_hi}），"
                f"已跳过。这可能是服务器崩溃的诱因，请把这个坐标反馈给作者。"
                f"{C.RESET}")
            return

        cmd = (f"tickingarea add {x_lo} {y_lo} {z_lo} "
               f"{x_hi} {y_hi} {z_hi} {area}")

        # 下发前把命令原文记下来。引擎如果还是崩了，至少控制台里
        # 能看到"最后发出去的是什么"，而不是一片空白。
        self.logger.debug(f"[{name}] 即将下发: /{cmd}")

        try:
            self.server.dispatch_command(self.server.command_sender, cmd)
        except Exception as exc:
            self.logger.debug(f"[{name}] 建立常加载区域失败: {exc}")
            return

        # 标记成功，之后不再重复下发（tickingarea 是幂等的，但重复发会
        # 让控制台多出一堆命令回显）。
        self._ticking_areas.add(name)
        self.logger.info(
            f"{C.GREEN}[{name}] 已建立常加载区域"
            f"（水平半径 {radius}，竖直 {y_lo}~{y_hi}），"
            f"地形将保持加载{C.RESET}")
        self._record(
            "body", f"常加载区域已建立（半径 {radius}，y {y_lo}~{y_hi}）",
            who=name)

    def _release_ticking_area(self, name: str) -> None:
        """摘掉假人的常加载区域。

        假人没了就该还回去——常加载区域是有代价的（区块常驻内存、
        实体不会休眠），不清理的话反复 spawn/remove 会越堆越多。
        """
        if name not in self._ticking_areas:
            return
        self._ticking_areas.discard(name)
        try:
            self.server.dispatch_command(
                self.server.command_sender, f"tickingarea remove pa_{name}"[:120])
        except Exception as exc:
            self.logger.debug(f"[{name}] 移除常加载区域失败: {exc}")

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """每 tick：推进身体动作队列，然后推进执行计划。"""
        # 身体动作先推进——执行器的判定依赖它们的完成状态。
        # 这里必须兜住异常：身体控制器会去问行为包要进度，桥接层
        # 半路断掉时可能抛。tick 是调度器回调，异常逃出去会崩服务端。
        try:
            self._bodies.tick()
        except Exception as exc:
            self.logger.debug(f"身体队列推进失败: {exc}")

        # ⚠️ 这个早退**不能**放在清扫前面。
        #
        # 执行器"空"恰恰是最需要清扫的时候：`cancel_all()` 把计划全摘掉
        # 之后，`busy` 变成 False，可各智能体的 `_inflight` 里还留着那些
        # 已经消失的计划。原先的顺序是先 `return` 再扫，等于**永远扫不到
        # 最该扫的那一次**。
        if not self._executor.busy:
            self._sweep_inflight()
            return

        for name in list(self._actors.keys()):
            # 死掉的实体不推进它的计划。每个步骤处理器都要读
            # actor.dimension / location，拿尸体去跑只会得到一串
            # "actor is not valid"。真正的收摊在 _perceive 里做，
            # 这里只要保证不添乱。
            if not self._is_live(self._actors.get(name)):
                continue

            self._exec_owner = name
            mine = [p for p in self._executor.active
                    if p.label.startswith(f"{name}: ")]
            if not mine:
                continue
            # 只推进这一个智能体的计划：把别人的临时挪出去
            others = [p for p in self._executor._plans if p not in mine]  # noqa: SLF001
            self._executor._plans = mine                                  # noqa: SLF001
            try:
                finished = self._executor.tick()
            finally:
                # 用 finally 确保临时挪出去的别人的计划一定会还回来。
                # 少了这一步，一次异常就会让整个计划列表永久残缺。
                self._executor._plans = mine + others                     # noqa: SLF001

            agent = self._agents.get(name)
            if agent is not None and finished:
                agent.reap_finished(finished)
        self._exec_owner = None

        # 兜底清扫必须在**所有**计划都推完之后做，而不是在循环里面。
        #
        # 循环体里那段"把别人的计划临时挪出去"只是为了让 `tick()` 只推进
        # 当前这个智能体的计划，挪回来之后别人的计划仍在 `_plans` 里，
        # 状态是完好的。所以这里查到的 `active` 是全量的，不会误伤。
        self._sweep_inflight()

    def _sweep_inflight(self) -> None:
        """把所有智能体"已从执行器消失、却还挂在待办上"的计划摘掉。

        为什么非扫不可：`cancel_all()` / `plan.abort()` 都是直接把计划
        从执行器里摘掉，**不产生任何回调**；而 `_inflight` 的清空只挂在
        `reap_finished` 上。那两条路一旦走到，计划就永远留在 `_inflight`
        里，`should_think()` 里的 `wait_for_plan and self._inflight` 会
        把这个智能体彻底锁死 —— 之后一句日志都不打，看起来就是
        "执行完一次任务后就不动了"。见 `Agent.sweep_inflight`。

        每一个都单独兜异常：这里是调度器回调的调用栈里，漏出去的异常
        会一路冒到 C++ 侧。
        """
        for agent in list(self._agents.values()):
            try:
                agent.sweep_inflight()
            except Exception as exc:
                self.logger.debug(f"清扫待办计划失败: {exc}")

        if self._executor.busy and not self._actors:
            self._executor.cancel_all()

    def _perceive(self) -> None:
        """每 10 tick 采一次感知并触发决策判断。仅主线程。

        身体没了要**主动收摊**，不能只 `continue`。原因：`_actors` 里的
        引用、执行器里排队的计划、身体控制器里的异步任务，都不会因为
        实体被干掉而自己消失。只跳过的话每个 tick 都会重复做同一件
        无用功，而且那些悬着的引用迟早会在别的地方被解引用。
        """
        # 先接等加入的真身——必须放在"没有智能体就早退"的前面：
        # 只有助手身体、没有任何智能体时，早退会让它永远接不上。
        self._try_attach_pending()
        # 反击条件反射推进（0.5 秒一拍：出手节奏与追击刷新都够用）
        try:
            self._reflex.tick()
        except Exception as exc:
            self.logger.debug(f"反射推进失败: {exc}")
        # 枪战 arena 战斗 AI 的敌我名单刷新
        try:
            self._arena_refresh()
        except Exception as exc:
            self.logger.debug(f"arena 刷新失败: {exc}")
        # 锚点闲逛驱动（家/领地/传送点附近逛 + 偶尔挖方块）。
        # 只在开着且大脑空闲时干活；忙时让路（见 _roam_busy）。
        try:
            roam_cfg = self._cfg.get("roam")
            if isinstance(roam_cfg, dict) and roam_cfg.get("enabled", True):
                self._roam.dig_chance = float(roam_cfg.get("dig_chance", 0.25))
                self._roam.tick()
        except Exception as exc:
            self.logger.debug(f"闲逛驱动失败: {exc}")
        # 好感度落盘节流（脏了才写）
        try:
            self._favor.save()
        except Exception:
            pass
        if not self._agents:
            return
        now = time.monotonic()
        for name, agent in list(self._agents.items()):
            # 这里要判两件事，顺序不能反：
            #
            # 1. 现在挂在身上的**是占位实体吗**。占位实体（NPC）是活的，
            #    所以 `_live_actor` 会给它过——但它显示的是 NPC 皮肤，
            #    而且动不了。真身一旦加入就得换掉。这一步必须在
            #    "身体是不是活着"之前判，否则永远轮不到它：
            #    占位实体活得好好的，`_live_actor` 根本不给机会补票。
            #    这就是"假人显示 NPC 皮肤"的直接原因。
            # 2. 换不掉的话，身体是真的没了 → 收摊。
            if self._use_real_body and self._is_placeholder(name):
                self._upgrade_placeholder(name)

            actor = self._live_actor(name)
            if actor is None:
                self._bury_body(name, agent, "身体已消失")
                continue

            # 地形要保持加载，否则假人一离开玩家视线就会"发呆"——
            # 感知读到空、动作全失败。这一步只在下发成功时留下标记，
            # 之后每 10 tick 走过都是零成本。
            # 常加载区域默认关闭——假人本来就"走到哪加载到哪"，
            # 那是 SimulatedPlayer 的固有行为。这个开关只为"长期驻守"
            # 的场景准备，打开后这一带区块会永远在线。
            if self._cfg.get("ticking_area", False):
                self._ensure_ticking_area(name, actor)

            try:
                agent.observe(self._world.snapshot(actor))
            except Exception as exc:
                # 快照途中实体也能死（我们刚判过 validity，但真正采集
                # 时它可能刚好被移除）。这一步失败同样是"身体没了"。
                self.logger.debug(f"[{name}] 感知失败: {exc}")
                self._bury_body(name, agent, "身体已失效")
                continue
            # 社交触发：真人靠近时提醒一句，让角色有机会主动搭话。
            # 只提醒、不强制思考——决策循环节奏不变，事件在下一轮
            # 决策时自然进入上下文。
            self._note_nearby_players(name, agent, actor)
            # 好感度驱动：寻仇扫描 + 关系摘要
            self._social_tick(name, agent, actor, now)
            if agent.should_think(now):
                agent.think()

    #: 玩家靠近的触发半径（格）
    _PROXIMITY_RANGE = 8.0
    #: 同一个"玩家 × 假人"对的提醒冷却（秒）。没有冷却的话一个玩家
    #: 在假人边上站着，每半秒的感知都会塞一条"玩家在你旁边"，
    #: 上下文很快就被这种废话填满。默认从 social.proximity_cooldown_seconds
    #: 读（240 秒）——90 秒一提醒会让假人变成话痨。
    _PROXIMITY_COOLDOWN = 90.0

    def _proximity_cooldown(self) -> float:
        try:
            v = float(self._social_cfg().get(
                "proximity_cooldown_seconds", 240.0))
        except (TypeError, ValueError):
            v = 240.0
        return max(30.0, v)

    def _note_nearby_players(self, name: str, agent: Any, actor: Any) -> None:
        try:
            loc = actor.location
            now = time.monotonic()
            cooldown = self._proximity_cooldown()
            events: list[dict[str, str]] = []
            for p in self.server.online_players:
                # 我们自己的假人不算"真人靠近"（xuid 为空）。
                if not str(getattr(p, "xuid", "") or "").strip():
                    continue
                pname = str(getattr(p, "name", ""))
                if not pname:
                    continue
                try:
                    d = math.dist(
                        (p.location.x, p.location.y, p.location.z),
                        (loc.x, loc.y, loc.z))
                except Exception:
                    continue
                if d > self._PROXIMITY_RANGE:
                    continue
                key = (name, pname.lower())
                if now - self._proximity_seen.get(key, 0.0) < cooldown:
                    continue
                self._proximity_seen[key] = now
                events.append({
                    "kind": "social",
                    "text": f"玩家 {pname} 在你旁边（约{d:.0f}格）。"
                            f"想搭话可以打个招呼，但别话痨——"
                            f"没必要时继续做自己的事"})
            if events:
                agent.note_events(events)
        except Exception as exc:
            self.logger.debug(f"[{name}] 靠近检测失败: {exc}")

    def _bury_body(self, name: str, agent: Any, reason: str) -> None:
        """身体没了之后的收摊：停止决策、中止动作、摘掉所有引用。

        幂等——每 10 tick 都会调一次，重复调用必须没有任何副作用。
        这也解释了为什么用 `done_reason` 当哨兵：它是"这个智能体
        已经停了"的唯一真相来源。

        每一步都各自兜异常：这里是调度器回调的调用栈里，
        任何漏出去的异常都会一路冒到 C++ 侧并崩掉服务端。
        收摊本身失败远没有"收摊时把服务器带崩"严重。
        """
        if not agent.alive():
            return
        try:
            agent.stop(reason)
        except Exception:
            pass
        self._actors.pop(name, None)
        self._placeholder_flags.pop(name, None)
        self._stealth.discard(name)
        self._pending_gm.pop(name, None)
        self._pending_crowd.pop(name, None)
        # 假人没了，常加载区域也该还回去——不然反复 spawn/remove 会
        # 在服务器上越堆越多，每个都在吃内存。
        self._release_ticking_area(name)

        # 身体控制器里的异步动作（挖掘/行走）要收掉。
        # 不收的话执行器会一直等一个永远不会来的结论。
        ctl = self._bodies.get(name)
        if ctl is not None:
            try:
                ctl.cancel(reason)
            except Exception:
                pass
        self._real_actions.pop(name, None)
        self._compilers.pop(name, None)
        try:
            self._executor.cancel_all(name)
        except Exception:
            pass
        try:
            self._record("body", f"身体消失（{reason}），相关动作已中止", who=name)
            self.logger.warning(
                f"{C.YELLOW}[{name}] {reason}，已停止该智能体{C.RESET}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 好感度 / 反击 / 寻仇
    # ------------------------------------------------------------------

    def _social_cfg(self) -> dict[str, Any]:
        cfg = self._cfg.get("social")
        return cfg if isinstance(cfg, dict) else {}

    def _bot_pos(self, name: str) -> tuple[float, float, float] | None:
        actor = self._live_actor(name) or self._live_external(name)
        if actor is None:
            return None
        try:
            loc = actor.location
            return (loc.x, loc.y, loc.z)
        except Exception:
            return None

    def _human_pos(self, name: str) -> tuple[float, float, float] | None:
        """在线**真人**的坐标。假人/离线返回 None（反射据此判定目标消失）。"""
        try:
            p = self.server.get_player(name)
        except Exception:
            return None
        if p is None:
            return None
        if not str(getattr(p, "xuid", "") or "").strip():
            return None      # 另一个假人不算追击目标
        try:
            loc = p.location
            return (loc.x, loc.y, loc.z)
        except Exception:
            return None

    def _bot_health(self, name: str) -> float | None:
        actor = self._live_actor(name)
        if actor is None:
            return None
        try:
            return float(actor.health)
        except Exception:
            return None

    def _reflex_event(self, bot: str, text: str) -> None:
        """战斗播报 → 大脑事件 + 立即思考（喊话/求饶/追击由它决定）。"""
        agent = self._agents.get(bot)
        if agent is not None and agent.alive():
            agent.note_events([{"kind": "combat", "text": text}])
            agent.state.last_decision = 0.0
        self._record("combat", text, who=bot)

    def _social_on_attacked(self, bot: str, attacker_name: str,
                            attacker: Any, damage: float) -> None:
        cfg = self._social_cfg()
        if not cfg.get("enabled", True):
            return
        # 管理员的测试性攻击：不反击、不记仇（否则管理调试会把所有
        # 假人的仇恨表打满）。
        if cfg.get("admins_immune", True) and getattr(attacker, "is_op", False):
            self.logger.info(
                f"[{bot}] 被管理员 {attacker_name} 攻击（伤害 {damage:.0f}），"
                f"按配置不反击不记仇")
            return
        penalty = float(cfg.get("attack_penalty", 12.0))
        score = self._favor.adjust(bot, attacker_name, -penalty, reason="被攻击")
        self.logger.info(
            f"[{bot}] 被 {attacker_name} 攻击（伤害 {damage:.0f}），"
            f"好感度 → {score:+.1f}")
        if cfg.get("reflex_enabled", True):
            duration = float(cfg.get("reflex_duration_seconds", 90.0))
            self._reflex.engage(bot, attacker_name, reason="反击",
                                duration=duration)
            # 预记寻仇冷却：脱战后不会立刻被复仇扫描重新拉起
            self._vengeance_cd[(bot, attacker_name)] = (
                time.monotonic() + duration + 90.0)
            # 反射接管身体：打断当前执行计划，别一边挖矿一边挨打
            try:
                self._executor.cancel_all(bot)
            except Exception:
                pass
        self._reflex_event(
            bot, f"你被玩家 {attacker_name} 攻击了（伤害 {damage:.0f}，"
                 f"好感度 {score:+.1f}）。"
                 + ("身体已自动反击。" if cfg.get("reflex_enabled", True)
                    else "你没有自动反击，自己决定怎么办。"))

    def _mob_on_attacked(self, bot: str, mob_name: str,
                         attacker: Any, damage: float) -> None:
        """怪物（非玩家 Actor）打了假人 → 条件反射反击。

        和真人路径的差别：不记好感度、不进仇人名单（怪物没有"关系"
        可言）、没有管理员豁免（怪物不存在 OP）。目标用 runtime id
        定位——JS 侧 doAttack 支持按实体 id 精确匹配。
        """
        cfg = self._social_cfg()
        if not cfg.get("enabled", True):
            return
        if not cfg.get("mob_reflex_enabled", True):
            return          # 想让大脑自己决定怎么应付怪物时关掉
        try:
            key = str(int(getattr(attacker, "runtime_id", -1)))
        except (TypeError, ValueError):
            return
        if key == "-1":
            return
        self._mob_targets[key] = (attacker, time.monotonic() + 120.0)
        duration = float(cfg.get("reflex_duration_seconds", 90.0))
        self._reflex.engage(bot, key, reason="怪物来袭",
                            duration=duration, label=mob_name)
        try:
            self._executor.cancel_all(bot)
        except Exception:
            pass
        self.logger.info(f"[{bot}] 被 {mob_name} 攻击，已自动反击")
        self._reflex_event(
            bot, f"你被 {mob_name} 攻击了（伤害 {damage:.0f}），"
                 f"身体已自动反击。")

    def _mob_pos(self, key: str) -> tuple[float, float, float] | None:
        """怪物目标坐标（反射追击用）。句柄过期/实体死亡返回 None。"""
        entry = self._mob_targets.get(key)
        if entry is None:
            return None
        actor, expire_at = entry
        if time.monotonic() > expire_at:
            self._mob_targets.pop(key, None)
            return None
        try:
            if not bool(getattr(actor, "is_valid", False)):
                self._mob_targets.pop(key, None)
                return None
            loc = actor.location
            return (loc.x, loc.y, loc.z)
        except Exception:
            self._mob_targets.pop(key, None)
            return None

    # ------------------------------------------------------------------
    # 护甲自穿 / 领地守卫
    # ------------------------------------------------------------------

    def _equip_tick(self) -> None:
        """周期性让每个在线假人把背包里最好的护甲穿上。"""
        for name in list(self._agents):
            if self._live_actor(name) is not None:
                self._bridge.equip(name)
        for name in list(self._external):
            if self._live_external(name) is not None:
                self._bridge.equip(name)

    def _land_modify_guard(self, name: str, x: int, y: int, z: int):
        """挖/放方块前的领地守卫。返回 None=放行，字符串=拒绝原因。

        走 arc_core 的 api_check_land_access——和真人完全同一套规则：
        荒野放行；假人自己的领地（合成 xuid 名下）放行；公共领地按
        公共规则；**别人的私人领地拒绝**。arc_core 缺席或出错时放行
        （fail-open）：没有领地系统的服务器不该因此废掉假人的挖掘。
        """
        core = None
        try:
            core = self.server.plugin_manager.get_plugin("arc_core")
        except Exception:
            return None
        if core is None or not callable(getattr(core, "api_check_land_access",
                                                None)):
            return None
        actor = self._live_actor(name)
        if actor is None:
            actor = self._live_external(name)
        if actor is None:
            return None
        try:
            dim = getattr(actor.dimension, "name", None) or str(actor.dimension)
        except Exception:
            return None
        xuid = ""
        try:
            xuid = str(self._identities.xuid_of(name) or "")
        except Exception:
            xuid = ""
        try:
            res = core.api_check_land_access(
                str(dim), (int(x), int(y), int(z)),
                player_name=name, xuid=xuid, action="build")
        except Exception as exc:
            self.logger.debug(f"领地权限查询失败（按放行）: {exc}")
            return None
        if not isinstance(res, dict) or res.get("allowed"):
            return None
        return ("这里受领地保护，不能破坏或放置方块"
                "（先离开这片领地，或找主人授权）")

    def _social_tick(self, bot: str, agent: Any, actor: Any, now: float) -> None:
        """感知循环里的社交驱动：寻仇扫描 + 好感度摘要。"""
        cfg = self._social_cfg()
        if not cfg.get("enabled", True):
            return
        # 寻仇：仇人在探测半径内且当前没在打架 → 主动开战
        if not self._reflex.is_engaged(bot):
            threshold = float(cfg.get("grudge_threshold", -30.0))
            radius = float(cfg.get("vengeance_radius", 40.0))
            grudges = self._favor.grudges(bot, threshold)
            if grudges:
                try:
                    aloc = (actor.location.x, actor.location.y, actor.location.z)
                except Exception:
                    aloc = None
                if aloc is not None:
                    for pname in grudges:
                        if now < self._vengeance_cd.get((bot, pname), 0.0):
                            continue
                        ploc = self._human_pos(pname)
                        if ploc is None:
                            continue
                        if math.dist(aloc, ploc) > radius:
                            continue
                        duration = float(cfg.get("reflex_duration_seconds", 90.0))
                        self._reflex.engage(bot, pname, reason="寻仇",
                                            duration=duration)
                        self._vengeance_cd[(bot, pname)] = now + duration + 90.0
                        try:
                            self._executor.cancel_all(bot)
                        except Exception:
                            pass
                        self._reflex_event(
                            bot, f"仇人 {pname} 就在附近"
                                 f"（好感度 {self._favor.score(bot, pname):+.1f}），"
                                 f"你决定寻仇！")
                        break
        # 好感度摘要：30 秒一条，让大脑始终知道和谁是什么关系
        if now >= self._favor_next.get(bot, 0.0):
            self._favor_next[bot] = now + 30.0
            line = self._favor.describe(
                bot, sorted(self.crowd_real_player_names()),
                grudge=float(cfg.get("grudge_threshold", -30.0)),
                friend=float(cfg.get("friend_threshold", 60.0)))
            if line:
                agent.note_events([{"kind": "social", "text": line}])

    def _social_on_trade(self, bot: str, player: str) -> None:
        cfg = self._social_cfg()
        if not cfg.get("enabled", True):
            return
        bonus = float(cfg.get("trade_bonus", 2.0))
        score = self._favor.adjust(bot, player, bonus, reason="交付物品")
        self.logger.info(f"[{bot}] 与 {player} 完成交付，好感度 → {score:+.1f}")

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    @event_handler
    def on_actor_damage(self, event: ActorDamageEvent) -> None:
        """谁的伤害都从这里过（怪物打怪也在内），miss 路径必须极快。"""
        try:
            victim = event.actor
            vname = str(getattr(victim, "name", "") or "")
            # 一行集合查询做快速过滤；我们的假人只可能是 Player
            if vname not in self._agents:
                return
            # 同名真人不算（真人的 xuid 非空）
            if str(getattr(victim, "xuid", "x") or "").strip():
                return
            src = getattr(event, "damage_source", None)
            attacker = (getattr(src, "damaging_actor", None)
                        or getattr(src, "actor", None))
            if attacker is None:
                return
            aname = str(getattr(attacker, "name", "") or "")
            if not aname or aname == vname:
                return
            damage = float(getattr(event, "damage", 0) or 0)
            if hasattr(attacker, "xuid"):
                # 玩家攻击者（真人）——只对**真人**反击与记仇：假人互殴
                # 不触发（两个 AI 打成无限互相寻仇的战争太荒诞）。
                if not str(getattr(attacker, "xuid", "") or "").strip():
                    return
                self._social_on_attacked(vname, aname, attacker, damage)
            else:
                # 怪物/投射物主人（非玩家 Actor）——同样条件反射反击，
                # 但不进好感度/仇人体系（那是给真人玩家的）。
                self._mob_on_attacked(vname, aname, attacker, damage)
        except Exception as exc:
            self.logger.debug(f"伤害事件处理失败: {exc}")

    @event_handler
    def on_player_death(self, event: PlayerDeathEvent) -> None:
        """两件事：假人阵亡（仇怨加深、脱战）；真人被寻仇得手（恩怨两清）。"""
        try:
            p = event.player
            name = str(getattr(p, "name", "") or "")
            if not name:
                return
            xuid = str(getattr(p, "xuid", "") or "").strip()
            cfg = self._social_cfg()
            if not xuid and name in self._agents:
                # 我们的假人死了（会在出生点重生）
                target = self._reflex.target_of(name)
                if target:
                    penalty = float(cfg.get("kill_penalty", 40.0))
                    score = self._favor.adjust(name, target, -penalty,
                                               reason="被击杀")
                    self._reflex.disengage(name, "阵亡")
                    self.logger.info(
                        f"[{name}] 被 {target} 击杀，好感度 → {score:+.1f}")
                agent = self._agents.get(name)
                if agent is not None and agent.alive():
                    who = f"被 {target} 击杀" if target else "死了"
                    agent.note_events([{
                        "kind": "combat",
                        "text": f"你{who}，已在出生点重生。这笔账记下了。"}])
                    agent.state.last_decision = 0.0
                return
            if xuid:
                # 真玩家死了。若正被我们的假人寻仇 → 得手，恩怨两清。
                bots = self._reflex.engaged_bots_of(name)
                threshold = float(cfg.get("grudge_threshold", -30.0))
                for bot in bots:
                    self._reflex.disengage(bot, "击败了目标")
                    if self._favor.score(bot, name) <= threshold:
                        self._favor.settle_vendetta(bot, name)
                        self._reflex_event(
                            bot, f"你击败了仇人 {name}，恩怨两清——"
                                 f"以后井水不犯河水。")
                        self.logger.info(f"[{bot}] 寻仇得手，与 {name} 恩怨两清")
                    else:
                        self._reflex_event(bot, f"你击败了 {name}。")
        except Exception as exc:
            self.logger.debug(f"死亡事件处理失败: {exc}")

    @event_handler
    def on_script_message(self, event: ScriptMessageEvent) -> None:
        """接收行为包发回的消息。这是身体 → 大脑的唯一入口。"""
        try:
            kind = self._bridge.dispatch(event.message_id, event.message)
        except Exception as exc:
            self.logger.debug(f"处理行为包消息失败: {exc}")
            return

        if kind == "positions":
            data = json.loads(event.message) if event.message else {}
            entries = data.get("p", [])
            if isinstance(entries, list):
                self._bodies.update_positions(entries)

    @event_handler
    def on_player_join(self, event: PlayerJoinEvent) -> None:
        for agent in self._agents.values():
            # 隐身假人（自动在线的群演）不提示——它们的工作就是
            # 看起来像普通玩家，进服就"自我介绍是 AI"等于穿帮。
            if agent.name in self._stealth:
                continue
            if agent.alive():
                event.player.send_message(
                    f"{C.AQUA}服务器里有 AI 虚拟玩家在线：{agent.name}{C.RESET}")

    @event_handler(priority=EventPriority.NORMAL, ignore_cancelled=True)
    def on_player_chat(self, event: PlayerChatEvent) -> None:
        """聊天分诊：提到名字必回；没提名的公屏闲聊按各自概率搭话。

        基岩聊天没有 @ 提及——玩家就是直接把名字打进句子里
        （"BossZhong 你在哪"）。所以"点名"的判定是**消息里出现名字**：

          · ASCII 名字用词边界匹配（"al" 不会误命中 "alice"），
            前后隔着字母/数字/下划线不算
          · 中文名字直接包含即命中（中文没有词边界一说）

        命中 → 必回（OP 的话按"指令"处理并撤回消息，普通玩家按
        "对话"处理）；没命中 → 走公屏闲聊的概率门控。
        """
        msg = (event.message or "").strip()
        if not msg:
            return
        low = msg.lower()
        speaker = str(getattr(event.player, "name", "") or "")
        # 说话的是自己人（我们的假人 / 外部身体如天星）→ 一律不理。
        # 不然假人 A 的话里带上 B 的名字，B 会"必回"，两个 AI 就地
        # 表演一段没人看的尬聊。真人（含离线模式）不受影响。
        if speaker and (speaker in self._agents or speaker in self._external):
            return
        is_op = bool(getattr(event.player, "is_op", False))

        named_any = False
        for name, agent in self._agents.items():
            if not _mentions(low, name):
                continue
            named_any = True
            if is_op:
                # 管理员：指令式控制。取消事件（不留在公屏），
                # 立刻唤醒决策。
                event.cancel()
                agent.hear(f"{speaker}(管理员)", msg)
                agent.state.last_decision = 0.0
                if name not in self._stealth:
                    try:
                        self.server.broadcast_message(
                            f"{C.GOLD}[{name}]{C.RESET} 收到玩家指令，正在思考…")
                    except Exception:
                        pass
                else:
                    # 隐身假人被管理员指挥时不广播（穿帮），
                    # 只给管理员本人一个回执。
                    event.player.send_message(
                        f"{C.GOLD}[{name}]{C.RESET} 收到指令，正在思考…")
            else:
                # 普通玩家：**对话**而非控制。不取消事件——这句话留在
                # 公屏上是自然的聊天；大脑以"朋友聊天"的定位听到它。
                agent.hear(f"{speaker}(玩家)", msg)
                agent.state.last_decision = 0.0
                # 友好对话攒好感（5 分钟冷却，防刷屏刷好感）
                try:
                    bonus = float(self._social_cfg().get("chat_bonus", 1.0))
                    if bonus:
                        self._favor.chat_bonus(name, speaker, bonus)
                except Exception:
                    pass

        if named_any:
            return
        if not speaker or speaker in self._agents:
            return      # 我们自己的假人说话不触发（防自嗨循环）

        # ---- 没被任何 AI 点名：公屏闲聊，按**每个 AI 自己的概率**
        # 决定要不要搭话（0 = 永不插嘴；1 = 每句都听）。
        import random as _random
        for name, agent in list(self._agents.items()):
            alive_fn = getattr(agent, "alive", None)
            if callable(alive_fn) and not alive_fn():
                continue
            chance = self.chat_reply_chance(name)
            if chance <= 0 or _random.random() >= chance:
                continue
            agent.hear(f"{speaker}(玩家闲聊)", msg)
            agent.state.last_decision = 0.0      # 听到了就立刻想想接不接话
            self._record("chat", f"听到 {speaker} 闲聊，考虑搭话", who=name)

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name not in ("ai", "arc_fake_player"):
            return False
        if not args:
            self._help(sender)
            return True
        fn = {
            "set": self._cmd_set, "test": self._cmd_test, "spawn": self._cmd_spawn,
            "list": self._cmd_list, "stop": self._cmd_stop, "remove": self._cmd_remove,
            "goal": self._cmd_goal, "chat": self._cmd_chat, "wear": self._cmd_wear,
            "where": self._cmd_where, "tp": self._cmd_tp, "body": self._cmd_body,
            "install": self._cmd_install, "ctx": self._cmd_ctx,
            "log": self._cmd_log, "prompt": self._cmd_prompt,
            "sense": self._cmd_sense,
            "persona": self._cmd_persona, "money": self._cmd_money,
            "session": self._cmd_session, "auto": self._cmd_auto,
            "reload": self._cmd_reload,
            "cmd": self._cmd_cmd,
            "home": self._cmd_home,
            "style": self._cmd_style,
            "chatrate": self._cmd_chatrate,
        }.get(args[0].lower())
        if fn is None:
            sender.send_message(f"{C.RED}未知子命令: {args[0]}{C.RESET}")
            self._help(sender)
            return True
        try:
            fn(sender, args[1:])
        except Exception as exc:
            self.logger.exception("命令异常")
            sender.send_message(f"{C.RED}出错: {exc}{C.RESET}")
        return True

    def _help(self, sender: CommandSender) -> None:
        if str(self._cfg.get("body_mode", "auto")).lower() == "npc":
            mode = "npc"
        elif self._use_real_body:
            mode = "真实假人"
        else:
            mode = "不可用（行为包未连接）"
        for line in [
            f"{C.GOLD}=== LLM 虚拟玩家（身体: {mode}）{C.RESET}",
            f"{C.YELLOW}/ai set <url> <key> <model>{C.RESET}  配置 LLM",
            f"{C.YELLOW}/ai test{C.RESET}                     测试连通性",
            f"{C.YELLOW}/ai spawn <名字> [目标]{C.RESET}       生成虚拟玩家",
            f"{C.YELLOW}/ai list{C.RESET}                     查看状态与效率",
            f"{C.YELLOW}/ai chat <名字> <内容>{C.RESET}        对它说话",
            f"{C.YELLOW}/ai goal <名字> <目标>{C.RESET}        改写目标",
            f"{C.YELLOW}/ai where <名字>{C.RESET}             查看位置",
            f"{C.YELLOW}/ai tp <名字>{C.RESET}                传送到它身边",
            f"{C.YELLOW}/ai body [real|npc|auto]{C.RESET}     切换身体模式",
            f"{C.YELLOW}/ai ctx [n|compact|clear]{C.RESET}   上下文预算/手动压缩",
            f"{C.YELLOW}/ai log [tail|clear|on|off]{C.RESET} 活动日志",
            f"{C.YELLOW}/ai prompt [段落|reset]{C.RESET}    查看/改提示词段落",
            f"{C.YELLOW}/ai sense [on|off]{C.RESET}         表面方块过滤开关",
            f"{C.YELLOW}/ai persona <名字> <性格>{C.RESET}     改写假人性格",
            f"{C.YELLOW}/ai money <名字> [数额]{C.RESET}       查看/设定假人钱包",
            f"{C.YELLOW}/ai session <名字> <分钟>{C.RESET}     调整在线角色的剩余游玩时间",
            f"{C.YELLOW}/ai auto [on|off|status]{C.RESET}     自动稳定在线开关/状态",
            f"{C.YELLOW}/ai reload{C.RESET}                    重读 config.json（角色卡等）",
            f"{C.YELLOW}/ai home{C.RESET}                      AI 管理表单（设家/常驻地）",
            f"{C.YELLOW}/ai install{C.RESET}                  重新安装行为包",
            f"{C.YELLOW}/ai remove <名字>{C.RESET}            移除",
            f"{C.DARK_AQUA}管理员可用 @名字 下指令；普通玩家 @名字 为对话{C.RESET}",
        ]:
            sender.send_message(line)

    def _cmd_set(self, sender: CommandSender, args: list[str]) -> None:
        if len(args) < 3:
            sender.send_message(f"{C.RED}用法: /ai set <url> <key> <model>{C.RESET}")
            return
        self._cfg["base_url"], self._cfg["api_key"], self._cfg["model"] = args[0], args[1], args[2]
        self._save_config()
        llm = self._make_llm()
        for a in self._agents.values():
            a._llm = llm  # noqa: SLF001
        sender.send_message(f"{C.GREEN}已保存：{args[2]} @ {args[0]}{C.RESET}")

    def _cmd_test(self, sender: CommandSender, args: list[str]) -> None:
        llm = self._make_llm()
        if not llm.configured():
            sender.send_message(f"{C.RED}尚未配置 LLM{C.RESET}")
            return
        sender.send_message(f"{C.YELLOW}测试中…{C.RESET}")

        def work() -> None:
            r = llm.chat([{"role": "user", "content": "只回复两个字：你好"}],
                         temperature=0.0, max_tokens=32)

            def report() -> None:
                if r.ok:
                    sender.send_message(f"{C.GREEN}正常，返回: {r.text or '(空)'}{C.RESET}")
                else:
                    sender.send_message(f"{C.RED}不可用: {r.error}{C.RESET}")
            self._on_main(report)

        self._pool.submit(work)

    def _cmd_body(self, sender: CommandSender, args: list[str]) -> None:
        if not args:
            self._report_body_status(sender)
            return
        want = args[0].lower()
        if want not in ("real", "npc", "auto"):
            sender.send_message(f"{C.RED}可选: real / npc / auto{C.RESET}")
            return
        self._cfg["body_mode"] = want
        self._save_config()
        self._health()
        if want == "npc":
            sender.send_message(
                f"{C.GREEN}身体模式已设为 npc{C.RESET}"
                f"{C.GRAY}（NPC 扮演，不算虚拟玩家）{C.RESET}")
        elif want == "auto":
            sender.send_message(
                f"{C.GREEN}身体模式已设为 auto{C.RESET}"
                f"{C.GRAY}（行为包连上才开工，连不上就报错）{C.RESET}")
        else:
            sender.send_message(f"{C.GREEN}身体模式已设为 real{C.RESET}")
        if want == "real" and not self._use_real_body:
            for line in self._body_error_text().split("\n"):
                sender.send_message(f"{C.RED}{line}{C.RESET}")

    def _report_body_status(self, sender: CommandSender) -> None:
        """把身体层的真实状态摊开给用户看。"""
        mode = str(self._cfg.get("body_mode", "auto")).lower()

        if mode == "npc":
            sender.send_message(
                f"{C.AQUA}身体模式: npc（NPC 扮演，不调用行为包）{C.RESET}")
            return

        if self._use_real_body:
            sender.send_message(
                f"{C.GREEN}身体: 真实假人 · 行为包已连接{C.RESET}")
        elif self._bridge.ever_connected:
            sender.send_message(
                f"{C.RED}身体: 不可用 · 行为包失联 "
                f"{self._bridge.last_pong_age:.0f} 秒{C.RESET}")
        else:
            sender.send_message(
                f"{C.RED}身体: 不可用 · 从未收到行为包心跳{C.RESET}")

        sender.send_message(
            f"{C.GRAY}配置模式: {mode}   "
            f"上次心跳: "
            + (f"{self._bridge.last_pong_age:.1f} 秒前"
               if self._bridge.last_pong_age >= 0 else "无")
            + f"{C.RESET}")

        report = getattr(self, "_pack_report", None)
        if report is not None:
            for line in packing.summarize(report):
                sender.send_message(f"{C.GRAY}{line}{C.RESET}")
            # 最容易被忽略、也最容易让人误判「补丁没修好」的一条：
            # 磁盘上的脚本已经换了，但 BDS 只在进程启动时扫描 behavior_packs/，
            # 不退不重启的话跑的还是内存里的旧脚本。
            if report.get("needs_restart"):
                sender.send_message(
                    f"{C.GOLD}⚠ 行为包脚本已更新到磁盘，但服务器仍在运行旧脚本。"
                    f"{C.RESET}")
                sender.send_message(
                    f"{C.GOLD}  请重启服务器（退出游戏客户端不算）后再试。{C.RESET}")

        if not self._use_real_body:
            for line in self._body_error_text().split("\n"):
                sender.send_message(f"{C.RED}{line}{C.RESET}")

    def _cmd_install(self, sender: CommandSender, args: list[str]) -> None:
        sender.send_message(f"{C.YELLOW}正在安装行为包…{C.RESET}")
        self._record("pack", "管理员执行了行为包安装", who=sender.name)
        ok = self._install_behavior_pack()
        report = getattr(self, "_pack_report", {})
        for line in packing.summarize(report):
            color = C.RED if not ok else C.GRAY
            sender.send_message(f"{color}{line}{C.RESET}")
        if ok and report.get("needs_restart"):
            sender.send_message(
                f"{C.GOLD}⚠ 请重启服务器，行为包才会生效{C.RESET}")
        self._record("pack",
                     "行为包安装" + ("成功（需重启）" if ok and report.get("needs_restart")
                                 else "成功" if ok else "失败"))

    def _cmd_log(self, sender: CommandSender, args: list[str]) -> None:
        """查看 / 清空 / 开关活动日志。"""
        sub = args[0].lower() if args else ""

        if sub == "clear":
            n = self._activity.clear()
            sender.send_message(
                f"{C.GREEN}已清空活动日志（删除 {n} 个文件）{C.RESET}")
            self._record("admin", f"管理员清空了活动日志", who=sender.name)
            return

        if sub in ("off", "on"):
            self._activity.enabled = (sub == "on")
            self._cfg["activity_log"] = self._activity.enabled
            self._save_config()
            sender.send_message(
                f"{C.GREEN}活动日志已{'开启' if self._activity.enabled else '关闭'}{C.RESET}")
            self._record("admin",
                         f"管理员{'开启' if self._activity.enabled else '关闭'}了活动日志",
                         who=sender.name)
            return

        if sub == "tail":
            raw = args[1] if len(args) > 1 else "15"
            try:
                n = int(raw)
            except ValueError:
                sender.send_message(f"{C.RED}数量要是数字{C.RESET}")
                return
            lines = self._activity.read_tail(n, limit=50)
            if not lines:
                sender.send_message(f"{C.YELLOW}活动日志还是空的{C.RESET}")
                return
            sender.send_message(
                f"{C.GOLD}=== 最近 {len(lines)} 条活动 ==={C.RESET}")
            for line in lines:
                sender.send_message(f"{C.GRAY}{line}{C.RESET}")
            return

        if sub:
            sender.send_message(
                f"{C.RED}用法: /ai log [tail [n] | clear | on | off]{C.RESET}")
            return

        # 不带参数：报状态
        sender.send_message(f"{C.GOLD}=== 活动日志 ==={C.RESET}")
        sender.send_message(f"{C.AQUA}{self._activity.path}{C.RESET}")
        sender.send_message(f"{C.GRAY}{self._activity.describe()}{C.RESET}")
        marks = self._activity.backups_on_disk
        if marks:
            total = sum(p.stat().st_size for p in marks if p.exists())
            sender.send_message(
                f"{C.GRAY}轮转文件: {', '.join(p.name for p in marks)} "
                f"（共 {total} 字节）{C.RESET}")
        sender.send_message(
            f"{C.GRAY}用法: /ai log tail [n] · /ai log clear · /ai log on|off{C.RESET}")

    def _cmd_spawn(self, sender: CommandSender, args: list[str]) -> None:
        if not args:
            sender.send_message(f"{C.RED}用法: /ai spawn <名字> [目标]{C.RESET}")
            return
        name = _clean_name(args[0])
        if name in self._agents:
            sender.send_message(f"{C.RED}已存在同名智能体{C.RESET}")
            return
        llm = self._make_llm()
        if not llm.configured():
            sender.send_message(f"{C.RED}请先 /ai set 配置模型{C.RESET}")
            return

        loc = getattr(sender, "location", None)
        if loc is None:
            sender.send_message(f"{C.RED}无法确定生成位置{C.RESET}")
            return

        # 身体不可用就到这里为止。不生成 NPC 冒充，也不延迟到执行阶段才炸——
        # 那样用户会先看到"生成成功"，再看到它一动不动，更难排查。
        if not self._require_body(sender):
            return

        actor, how = self._spawn_body(name, loc)
        if actor is None:
            sender.send_message(f"{C.RED}身体生成失败，请检查服务端日志{C.RESET}")
            return

        # 名字牌不在这里设。假人由行为包创建，外观由行为包负责
        # （`main.js` 的 `tagName()` 在 spawn 成功时就挂好了）。
        # Python 侧只发指令、只读状态，不写假人对象的属性。

        goal = " ".join(args[1:]) if len(args) > 1 else str(self._cfg["default_goal"])
        agent = self._start_agent(name, goal=goal, persona="", actor=actor)
        if agent is None:
            sender.send_message(f"{C.RED}智能体创建失败，请检查服务端日志{C.RESET}")
            return
        self._record("spawn", f"已生成（{how}），目标: {goal[:80]}",
                     who=name)
        sender.send_message(
            f"{C.GREEN}虚拟玩家 {name} 已生成（{how}），AI 接管中。{C.RESET}")

        # ⚠️ 这里**不能**广播"加入了服务器"。
        #
        # 2.x 的一句 `⚡ AI 虚拟玩家 xxx 加入了服务器` 是**无条件**打的，
        # 但它打出的时候真身往往还没接上——用户实测时被这句误导，
        # 以为假人已经好了，实际上 4 秒后就被判"身体已消失"。
        # 日志必须说实话。
        #
        # 现在分两句：
        #   · 这句只说"已创建，在等真身"（真身还没来）
        #   · 真身接上时（_upgrade_placeholder）才广播"已接管真实假人"
        if how.startswith("占位"):
            self.server.broadcast_message(
                f"{C.YELLOW}⏳ AI 虚拟玩家 {name} 已创建（{how}），"
                f"等待真实假人加入…{C.RESET}")
        else:
            self.server.broadcast_message(
                f"{C.GREEN}⚡ AI 虚拟玩家 {name} 已接管真实假人"
                f"（由大模型驱动）{C.RESET}")

    # ------------------------------------------------------------------
    # 智能体创建（命令 spawn 与自动在线共用）
    # ------------------------------------------------------------------

    def _start_agent(self, name: str, *, goal: str, persona: str,
                     actor: Any) -> Any:
        """给一具身体装配 Python 大脑。失败返回 None。

        `_cmd_spawn` 和自动在线的角色上线走同一条路，保证手动/自动
        假人的能力完全一致。`persona` 非空时覆盖全局性格段
        （角色卡的"性格"就是这么生效的）。
        """
        llm = self._make_llm()
        try:
            # 登记合成身份（手动 spawn 的假人也有终身稳定的 xuid/uuid）
            identity = self._identities.of(name)
            config = AgentConfig(
                name=name, goal=goal,
                decision_interval=float(self._cfg["decision_interval"]),
                context_max_chars=int(self._cfg["context_max_chars"]),
                # 夹到 >= 1：0 会让压缩变成"清空全部历史"（见 agent.py
                # 里 `_compact_history` 的注释）。这里先夹住，用户填 0
                # 也不会踩坑。
                context_keep_recent=max(1, int(self._cfg["context_keep_recent"])),
                context_summarize=bool(self._cfg["context_summarize"]),
                # AI 失败重试。读的是配置，改 config.json 就能调。
                llm_retry_seconds=float(self._cfg.get("llm_retry_seconds", 60.0)),
                llm_max_retries=int(self._cfg.get("llm_max_retries", 0)),
                # 提示词段落。每段都从 config 读，改 config.json 就改提示词。
                identity=str(self._cfg["prompt_identity"]),
                body=str(self._cfg["prompt_body"]),
                persona=persona or str(self._cfg["prompt_persona"]),
                style=self._style_block(name),
                rules=(str(self._cfg["prompt_rules"]) + "\n\n"
                       + str(self._cfg.get("prompt_command_policy", ""))
                       + "\n\n" + str(self._cfg.get("prompt_social", ""))),
                # 关掉表面过滤时，"你只看得见表面方块"这段就不再成立，
                # 留着会让模型以为观测被裁剪了而去怀疑数据。所以这时不带它。
                surface_hint=(str(self._cfg["prompt_surface_hint"])
                              if self._cfg["surface_only"] else ""),
                game_knowledge=str(self._cfg["prompt_game_knowledge"]),
                item_names=str(self._cfg["prompt_item_names"]),
            )
            compiler = ActionCompiler(self._world, self.logger)
            compiler.use_real_dig = self._use_real_body
            # 挖掘工具是否也只找表面方块。与感知那个开关是两件事，
            # 但只关一个就会留下"看不见却能挖到"的矿透后门。
            compiler.surface_only = bool(self._cfg["dig_surface_only"])
            compiler.bind_player_lookup(lambda p: self.server.get_player(p))

            # 真实身体的动作处理器
            ctl = self._bodies.add(name)
            ctl.actor = actor
            self._real_actions[name] = RealBodyActions(ctl, self._world, self.logger)
            self._compilers[name] = compiler
            # 身体感知需要知道在哪个维度
            self._world.body_dimension = actor.dimension

            agent = Agent(
                config=config, llm=llm, world=self._world, compiler=compiler,
                executor=self._executor, logger=self.logger, pool=self._pool,
                on_main=self._on_main, on_say=self._broadcast,
                on_log=self._agent_log,
                # 交给 Agent 的取身体回调返回的是"还活着的身体"。
                # 直接把 _actors 里的引用给出去的话，实体被打掉后 Agent 仍会
                # 拿到一具尸体，然后拿它去编译动作——每一个动作都会抛
                # "actor is not valid"。这里挡掉，Agent 就能干净地停下。
                get_actor=lambda n=name: self._live_actor(n),
            )
        except Exception:
            self.logger.exception(f"创建智能体 {name} 失败")
            return None

        self._agents[name] = agent
        self._actors[name] = actor
        self.logger.info(
            f"[{name}] 合成身份 xuid={identity['xuid']} uuid={identity['uuid']}")
        return agent

    # ------------------------------------------------------------------
    # 共享的真身生成（命令 spawn / 自动在线 / 外部身体 API 同一条路）
    # ------------------------------------------------------------------

    def _request_body(self, name: str, x: int, y: int, z: int, *,
                      stealth: bool = False) -> str:
        """向行为包请求一具真身。返回 "joined" / "pending" / "failed"。

        和 `_cmd_spawn` 的路径有个本质区别：这里**不造 NPC 占位**。
        占位实体的 spawn_actor 需要真正的 Location 对象（程序化生成
        没有玩家 sender 可借），造不出来反而会撤回已发出的生成请求
        ——实测症状就是"假人刚加入就被移除"。程序化路径改为
        "请求 → 等待加入 → 接管"：`_try_attach_pending` 会在感知循环里
        把加入的真身接到对应的名字上。

        高空落地的游戏模式切换：地面高度未知时从 y=200 生成，先以
        创造模式安全落地（生存模式摔 130 格会死），真身接上时由
        `_apply_pending_gm` 切回配置的模式。
        """
        game_mode = self._bot_game_mode_number()
        if y > 128 and game_mode not in (None, 1):
            game_mode = 1
            self._pending_gm[name] = self._bot_game_mode_number()
        self._bridge.spawn(name, x, y, z, "overworld",
                           game_mode=game_mode, stealth=stealth)
        if self._find_sim_player(name) is not None:
            return "joined"
        if not self._bridge.can_spawn_directly:
            # 判断"造不出"和"事实"可能不一致——把已发出的请求显式撤回，
            # 免得真身迟一步加入变成没人控制的躯壳（见 _spawn_body 同款注释）。
            self._bridge.remove(name)
            return "failed"
        return "pending"

    def _apply_pending_gm(self, name: str, player: Any) -> None:
        """高空安全落地的收尾：落地时是创造模式（防摔死），接上后切回
        配置的模式。见 _request_body 的说明。

        ⚠️ 切换要**延迟**（约 8 秒）：接上那一刻假人多半还在从 y=200
        往下掉，立刻切生存会吃满坠落到伤害（实测日志里出现过
        "TianXing fell from a high place"）。从 200 格落地约 6 秒，
        8 秒后切换既保命也看不出破绽。
        """
        want_gm = self._pending_gm.pop(name, None)
        if want_gm is None:
            return
        mode_word = {v: k for k, v in self._GAME_MODE_NUMBERS.items()}.get(
            want_gm, "survival")

        def _switch() -> None:
            try:
                self._dispatch(f"gamemode {mode_word} {name}")
            except Exception as exc:
                self.logger.debug(f"[{name}] 游戏模式切换失败: {exc}")

        try:
            self.server.scheduler.run_task(self, _switch, delay=160)
        except Exception as exc:
            self.logger.debug(f"[{name}] 游戏模式切换任务注册失败: {exc}")

    #: 程序化生成的"等真身加入"看板：名字 → (目标, 性格, 初始金钱, 请求时刻)。
    #: 感知循环每 0.5 秒扫一遍，真身一加入就接管成正式智能体。
    _pending_crowd: dict[str, tuple[str, str, float | None, float]] = {}

    def _try_attach_pending(self) -> None:
        """把已加入的真身接到等待中的名字上（外部身体 + 自动在线角色）。

        ⚠️ 必须放在 `_perceive` 的 `if not self._agents: return` **之前**：
        只有助手身体、没有任何智能体时，早退会让它永远接不上。
        """
        now = time.monotonic()
        # 外部身体：_external 里值为 None 的都是"生成中"。
        for name in list(self._external):
            if self._external[name] is not None:
                continue
            player = self._find_sim_player(name)
            if player is None:
                continue
            self._external[name] = player
            self._apply_pending_gm(name, player)
            self._ensure_core_identity(player, name)
            self._record("spawn", "外部身体真身已加入", who=name)
            self.logger.info(f"{C.GREEN}[{name}] 外部身体真身已加入{C.RESET}")
        # 自动在线角色
        for name, (goal, persona, money, requested_at) in list(
                self._pending_crowd.items()):
            player = self._find_sim_player(name)
            if player is None:
                # 生成请求发出 120 秒还没加入——多半是行为包那边出了问题。
                # 放弃这个名额，让调度器走正常的补位逻辑。
                if now - requested_at > 120.0:
                    self._pending_crowd.pop(name, None)
                    self._pending_gm.pop(name, None)
                    self.logger.warning(
                        f"{C.YELLOW}[{name}] 真身 120 秒未加入，放弃该名额{C.RESET}")
                continue
            self._pending_crowd.pop(name, None)
            agent = self._start_agent(name, goal=goal, persona=persona,
                                      actor=player)
            if agent is None:
                self._bridge.remove(name)
                continue
            self._stealth.add(name)
            self._apply_pending_gm(name, player)
            self._ensure_core_identity(player, name)
            self._core_playtime(name, joining=True)
            self._init_wallet_once(name, money)
            self._announce_join(name)
            self._record("spawn", f"角色卡真身已加入，目标: {goal[:60]}",
                         who=name)

    # ------------------------------------------------------------------
    # 自动在线（角色卡轮换）—— CrowdScheduler 的游戏侧钩子
    # ------------------------------------------------------------------

    def _run_bot_command(self, owner: str, actor: Any, command: str) -> str:
        """以假人身份"执行"一条命令。返回空串=成功，否则是失败说明。

        已知弧光玩法（枪战）走 API 直调——命令通道对模拟玩家不可靠
        （见 h_bot_command 的注释）。其它命令退回 dispatch_command：
        对真玩家语义的命令可能静默无效，但至少不会误伤。
        """
        command = command.strip()
        low = command.lower()
        try:
            pm = getattr(self.server, "plugin_manager", None)
            shooter = pm.get_plugin("arc_shooter_game") if pm else None
        except Exception:
            shooter = None
        if low in ("gs quick", "gs start", "qsshoot"):
            if shooter is None or not callable(getattr(shooter, "api_quick_join", None)):
                return "本服没有枪战游戏（arc_shooter_game ≥0.4.23）"
            result = shooter.api_quick_join(actor)
            text = str(result.get("text") or "")
            if result.get("ok"):
                self._record("game", f"{owner} {text}", who=owner)
                # 进房即接管战斗：找最近敌人→追→视线内开枪
                self._arena_enable(owner)
                return ""
            return text or "加入失败"
        if low in ("gs leave", "gs quit"):
            self._arena_disable(owner)
            if shooter is not None and callable(getattr(shooter, "api_leave", None)):
                result = shooter.api_leave(actor)
                text = str(result.get("text") or "")
                if result.get("ok"):
                    self._record("game", f"{owner} 离开了枪战房间", who=owner)
                    return ""
                return text
            # 没有新版枪战插件就试命令通道
        try:
            ok = bool(self.server.dispatch_command(actor, command))
        except Exception as exc:
            self.logger.debug(f"[{owner}] 命令执行异常: {exc}")
            return "命令无法执行（模拟玩家可能不支持该命令）"
        return "" if ok else "命令被拒绝"

    # ------------------------------------------------------------------
    # 枪战 arena 战斗 AI（进对局即自动战斗：找最近敌人→追→视线内开枪）
    # ------------------------------------------------------------------

    #: arena 状态：名字 → 下次敌我名单刷新时刻
    _arena_next: dict[str, float] = {}

    def _shooter_plugin(self) -> Any:
        try:
            pm = getattr(self.server, "plugin_manager", None)
            plug = pm.get_plugin("arc_shooter_game") if pm else None
        except Exception:
            return None
        if plug is None or not callable(getattr(plug, "api_lobby_info", None)):
            return None
        return plug

    def _arena_enable(self, name: str) -> None:
        shooter = self._shooter_plugin()
        if shooter is None:
            return
        info = shooter.api_lobby_info(name)
        if not info.get("ok"):
            return
        enemies = [str(e) for e in (info.get("enemies") or [])]
        minutes = max(2.0, (info.get("match_seconds") or 300) / 60.0 + 2.0)
        self._bridge.arena(name, enemies, minutes)
        self._arena_next[name] = time.monotonic() + 15.0
        self.logger.info(f"[{name}] arena 战斗 AI 已启动（敌人："
                         f"{', '.join(enemies) or '暂无'}）")

    def _arena_disable(self, name: str) -> None:
        if self._arena_next.pop(name, None) is not None:
            self._bridge.arena_off(name)
            self.logger.info(f"[{name}] arena 战斗 AI 已停止")

    def _init_wallet_once(self, name: str, money: float | None) -> None:
        """首次上线建钱包并设角色卡的初始金钱；之后只确保行存在。

        为什么要有"首次"标记：arc_core 的 get_player_money_by_xuid 是
        get-or-create（读一次就建行），没法从余额判断"新钱包"；而没有
        标记的话，角色每次轮换上线都会把余额重置成卡面值——白送钱。
        标记存在身份注册表里，跨重启持久。
        """
        if not self._economy.available():
            return
        if money is not None and self._identities.mark(name, "wallet_init"):
            self._economy.ensure_wallet(name, float(money))
        else:
            self._economy.ensure_wallet(name, None)

    def _ensure_core_identity(self, player: Any, name: str) -> None:
        """假人身体接上时，主动让弧光核心完成"玩家数据初始化"。

        ⚠️ 这版 Endstone 的 PlayerJoinEvent **不对 SimulatedPlayer 触发**，
        核心的 join 初始化（basic_info / local_info / 经济行）永远不会为
        假人跑。所以由我们在这里主动调（核心 ≥0.9.72 的
        ensure_player_data_initialized 会经 _resolve_xuid 拿到合成 xuid，
        之后每个假人在核心库里都有自己的行）。
        """
        try:
            pm = getattr(self.server, "plugin_manager", None)
            core = pm.get_plugin("arc_core") if pm else None
        except Exception:
            core = None
        if core is None:
            return
        fn = getattr(core, "ensure_player_data_initialized", None)
        if not callable(fn):
            self.logger.debug(f"[{name}] arc_core 没有 ensure_player_data_initialized")
            return
        try:
            ok, _new = fn(player)
            if ok:
                self.logger.info(
                    f"[{name}] 弧光核心数据已就绪（合成 xuid="
                    f"{self._identities.xuid_of(name)}）")
        except Exception as exc:
            self.logger.warning(f"[{name}] 弧光核心数据初始化失败: {exc}")

    def _arena_refresh(self) -> None:
        """每 15 秒刷新一次敌我名单；不在对局里了就停。

        有人中途加入/换队、对局结束（枪战把人放回主世界）都靠这个跟上。
        """
        now = time.monotonic()
        for name in list(self._arena_next):
            if now < self._arena_next[name]:
                continue
            shooter = self._shooter_plugin()
            if shooter is None:
                self._arena_disable(name)
                continue
            info = shooter.api_lobby_info(name)
            if not info.get("ok"):
                # 对局结束/被移出房间——战斗 AI 交给 JS 侧的
                # "敌人 30 秒不在线自动退场"兜底，Python 这里只清记账。
                self._arena_next.pop(name, None)
                self._bridge.arena_off(name)
                self.logger.info(f"[{name}] 已不在枪战房间，arena 战斗 AI 停止")
                continue
            enemies = [str(e) for e in (info.get("enemies") or [])]
            minutes = max(2.0, (info.get("match_seconds") or 300) / 60.0 + 2.0)
            self._bridge.arena(name, enemies, minutes)
            self._arena_next[name] = now + 15.0

    # ------------------------------------------------------------------
    # 常驻地与锚点闲逛（家 > 自己领地 > 公共传送点）+ OP 管理表单
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 聊天个性：公屏搭话概率 + 说话风格示例
    # ------------------------------------------------------------------

    def _load_bot_settings(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._bot_settings_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            self.logger.warning(f"bot_settings.json 读取失败: {exc}")
            return {}

    def _save_bot_settings(self) -> None:
        try:
            tmp = self._bot_settings_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._bot_settings, ensure_ascii=False, indent=2),
                encoding="utf-8")
            tmp.replace(self._bot_settings_path)
        except Exception as exc:
            self.logger.warning(f"bot_settings.json 保存失败: {exc}")

    def _seed_bot_settings(self, name: str, *, chance: float | None,
                           style: list[str] | None) -> None:
        """角色卡首次上线时播种个性设置；已有设置不动（改过的优先）。"""
        entry = self._bot_settings.setdefault(name, {})
        changed = False
        if "chat_reply_chance" not in entry and chance is not None:
            entry["chat_reply_chance"] = float(chance)
            changed = True
        if not entry.get("style") and style:
            entry["style"] = list(style)
            changed = True
        if changed:
            self._save_bot_settings()

    def chat_reply_chance(self, name: str) -> float:
        """这个 AI 听到公屏闲聊时搭话的概率（0~1）。"""
        entry = self._bot_settings.get(name) or {}
        try:
            return max(0.0, min(1.0, float(entry.get("chat_reply_chance"))))
        except (TypeError, ValueError):
            pass
        try:
            cfg = getattr(self, "_cfg", None) or {}
            raw = cfg.get("chat_reply_chance", 0.15)
            if raw is None:
                raw = 0.15
            return max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError, AttributeError):
            return 0.15

    def set_chat_reply_chance(self, name: str, chance: float) -> None:
        entry = self._bot_settings.setdefault(name, {})
        entry["chat_reply_chance"] = max(0.0, min(1.0, float(chance)))
        self._save_bot_settings()

    def _style_of(self, name: str) -> list[str]:
        entry = self._bot_settings.get(name) or {}
        style = entry.get("style")
        if isinstance(style, list):
            return [str(x) for x in style if str(x).strip()]
        return []

    def _style_block(self, name: str) -> str:
        """拼进系统提示词的"说话风格"段。没有示例返回空串。"""
        examples = self._style_of(name)
        if not examples:
            return ""
        lines = ["## 说话风格",
                 "你说话要**模仿下面示例的语气、口头禅和情绪**（可以自由发挥，"
                 "但不要逐字照抄示例）："]
        lines += [f"- {e}" for e in examples[:8]]
        return "\n".join(lines)

    def set_style(self, name: str, examples: list[str]) -> None:
        cleaned = [str(e).strip() for e in examples if str(e).strip()]
        entry = self._bot_settings.setdefault(name, {})
        entry["style"] = cleaned
        self._save_bot_settings()

    def _apply_chat_personality(self, name: str) -> None:
        """把最新的风格段推给在线的智能体（下轮决策生效）。"""
        agent = self._agents.get(name)
        if agent is not None:
            agent.config.style = self._style_block(name)


    def _load_homes(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._homes_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            self.logger.warning(f"homes.json 读取失败: {exc}")
            return {}

    def _save_homes(self) -> None:
        try:
            tmp = self._homes_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._homes, ensure_ascii=False, indent=2),
                encoding="utf-8")
            tmp.replace(self._homes_path)
        except Exception as exc:
            self.logger.warning(f"homes.json 保存失败: {exc}")

    def set_home(self, name: str, x: float, y: float, z: float,
                 dimension: str = "overworld") -> None:
        """设置常驻地。OP 管理表单与控制台命令共用这一个入口。"""
        self._homes[name] = {"x": float(x), "y": float(y), "z": float(z),
                             "dim": str(dimension)}
        self._save_homes()

    def _roam_busy(self, name: str) -> bool:
        """闲逛让路条件：打架 / 枪战对局 / 有执行计划 / 等真身 /
        身体控制器正在执行动作（挖掘的走近/挖掘中）。

        最后一条是关键：上一版闲逛的 walk 会打断 bot:dig 的走近，
        dig 的轮询又把人拽回去——来回抵消，假人"走一步然后罚站"。
        """
        agent = self._agents.get(name)
        if (self._reflex.is_engaged(name)
                or name in self._arena_next
                or name in self._pending_crowd
                or bool(getattr(agent, "_inflight", None))):
            return True
        ctl = self._bodies.get(name)
        return ctl is not None and bool(getattr(ctl, "busy", False))

    def _roam_anchor(self, name: str) -> tuple[float, float, float, str] | None:
        """闲逛锚点：家 > 自己领地传送点 > 随机公共传送点。"""
        home = self._homes.get(name)
        if isinstance(home, dict):
            try:
                return (float(home["x"]), float(home["y"]), float(home["z"]),
                        "家")
            except (KeyError, TypeError, ValueError):
                pass
        # 自己的领地（按合成 xuid 查——身份系统让假人名下的地也查得到）
        try:
            pm = getattr(self.server, "plugin_manager", None)
            core = pm.get_plugin("arc_core") if pm else None
            lands = getattr(core, "land_system", None)
            if lands is not None and self._identities is not None:
                xuid = self._identities.xuid_of(name)
                mine = lands.get_player_lands(xuid)
                if mine:
                    land = next(iter(mine.values()))
                    tp = (float(land.get("tp_x", 0) or 0),
                          float(land.get("tp_y", 64) or 64),
                          float(land.get("tp_z", 0) or 0))
                    if any(tp):
                        return (tp[0], tp[1], tp[2], "领地")
        except Exception:
            pass
        warp = self._random_warp()
        if warp is not None:
            return (warp[0], warp[1], warp[2], "传送点")
        return None

    def _random_warp(self) -> tuple[float, float, float] | None:
        """随机一个公共传送点（只读 arc_core 的库，60 秒缓存）。"""
        now = time.monotonic()
        cached_at, cached = self._warps_cache
        if now - cached_at > 60.0:
            fresh: list[tuple] = []
            try:
                import sqlite3
                db_path = (Path(self.data_folder).parent.parent
                           / "ARCCore" / "ARCCore.db")
                if db_path.exists():
                    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                    try:
                        rows = con.execute(
                            "SELECT x, y, z FROM public_warps").fetchall()
                        fresh = [(float(r[0]), float(r[1]), float(r[2]))
                                 for r in rows]
                    finally:
                        con.close()
            except Exception as exc:
                self.logger.debug(f"读取公共传送点失败: {exc}")
            self._warps_cache = (now, fresh)
            cached = fresh
        if not cached:
            return None
        import random as _random
        return _random.choice(cached)

    def _roam_walk(self, name: str, x: int, y: int, z: int) -> bool:
        """闲逛走位：失败要回灌给驱动（3 秒重试），不能干等一个间隔。"""
        rid = self._bridge.walk(
            name, x, y, z, timeout=200,
            callback=lambda ok, err, info: None if ok
            else self._roam.note_failure(name, str(err or "走位失败")))
        return rid >= 0

    def _roam_dig(self, name: str, x: int, y: int, z: int) -> bool:
        """闲逛挖一块：同样把失败回灌。"""
        rid = self._bridge.dig(
            name, x, y, z,
            callback=lambda ok, err, info: None if ok
            else self._roam.note_failure(name, str(err or "挖掘失败")))
        return rid >= 0

    def _roam_surface(self, x: float, z: float, near_y: float) -> float | None:
        """(x,z) 这一列在 near_y 附近**可站立**的 y。

        用寻路器同款的 walkable_surface（脚下实心、身体两格空、不站
        岩浆/火），窗口放宽到 +8/-14——家附近常有 64~76 的丘陵，手搓的
        窄窗口会大量静默失败，表现成"定在家的那个点上罚站"。
        """
        try:
            dim = self.server.level.get_dimension("overworld")
        except Exception:
            return None
        try:
            y = walkable_surface(dim, self._world, int(x), int(z),
                                 int(near_y), range_down=14, range_up=8)
            return float(y) if y is not None else None
        except Exception:
            return None

    # ---- OP 管理表单（游戏内 UI；控制台仍可用文本命令） ----

    def _show_manage_form(self, sender: Any) -> None:
        """AI 玩家管理主表单：列出每个 AI，点进去单独管理。"""
        try:
            from endstone.form import ActionForm
        except Exception:
            sender.send_message(f"{C.RED}本客户端不支持表单{C.RESET}")
            return
        names = sorted(set(self._agents) | set(self._homes))
        if not names and self._identities is not None:
            names = sorted(self._identities.all().keys())
        form = ActionForm(
            title="AI 玩家管理",
            content="点选一个 AI 玩家进行管理。\n"
                    "常驻地优先级：家 > 自己的领地 > 公共传送点。\n"
                    "AI 平时在常驻地附近闲逛，偶尔挖方块；离远了会自己走回去。")
        for name in names:
            online = self._live_actor(name) is not None
            anchor = self._roam_anchor(name)
            where = anchor[3] if anchor else "无锚点"
            state = "在线" if online else "离线"
            form.add_button(
                f"{name}\n{state} · 锚点: {where}",
                on_click=lambda s, n=name: self._show_bot_form(s, n))
        form.add_button("关闭", on_click=lambda s: None)
        sender.send_form(form)

    def _show_bot_form(self, sender: Any, name: str) -> None:
        """单个 AI 的管理表单。"""
        from endstone.form import ActionForm
        home = self._homes.get(name)
        anchor = self._roam_anchor(name)
        lines = [f"名字: {name}",
                 f"状态: {'在线' if self._live_actor(name) is not None else '离线'}"]
        if home:
            lines.append(f"家: ({home.get('x', 0):.0f}, {home.get('y', 0):.0f},"
                         f" {home.get('z', 0):.0f})")
        else:
            lines.append("家: 未设置")
        if anchor:
            lines.append(f"当前锚点: {anchor[3]}"
                         f" ({anchor[0]:.0f},{anchor[1]:.0f},{anchor[2]:.0f})")
        else:
            lines.append("当前锚点: 无（会在原地附近小范围晃）")
        form = ActionForm(title=f"管理 {name}", content="\n".join(lines))
        form.add_button(
            "设家到我脚下",
            on_click=lambda s: self._home_set_here(s, name))
        form.add_button(
            "设家到指定坐标",
            on_click=lambda s: self._home_set_coords_form(s, name))
        form.add_button(
            "清除家",
            on_click=lambda s: self._home_clear(s, name))
        form.add_button(
            f"主动回复概率（当前 {self.chat_reply_chance(name):.0%}）",
            on_click=lambda s: self._chance_form(s, name))
        form.add_button(
            "说话风格示例",
            on_click=lambda s: self._style_form(s, name))
        form.add_button(
            "传送到他身边",
            on_click=lambda s: self._cmd_tp(s, [name]))
        form.add_button(
            "命令帮助",
            on_click=lambda s: self._help(s))
        form.add_button("返回", on_click=lambda s: self._show_manage_form(s))
        sender.send_form(form)

    def _home_set_here(self, sender: Any, name: str) -> None:
        loc = getattr(sender, "location", None)
        if loc is None:
            sender.send_message(f"{C.RED}拿不到你的位置{C.RESET}")
            return
        self.set_home(name, loc.x, loc.y, loc.z)
        self._record("admin", f"{sender.name} 经表单设置常驻地", who=name)
        sender.send_message(
            f"{C.GREEN}{name} 的家已设到你脚下"
            f"（{loc.x:.0f},{loc.y:.0f},{loc.z:.0f}）{C.RESET}")
        self._show_bot_form(sender, name)

    def _home_set_coords_form(self, sender: Any, name: str) -> None:
        from endstone.form import ModalForm, TextInput
        form = ModalForm(title=f"设置 {name} 的家（输入 x y z）")
        form.add_control(TextInput(label="X", placeholder="0"))
        form.add_control(TextInput(label="Y", placeholder="64"))
        form.add_control(TextInput(label="Z", placeholder="0"))

        def _submit(player: Any, data: Any) -> None:
            try:
                values = list(data or [])
                x, y, z = float(values[0]), float(values[1]), float(values[2])
            except (TypeError, ValueError, IndexError):
                player.send_message(f"{C.RED}坐标无效{C.RESET}")
                return
            self.set_home(name, x, y, z)
            self._record("admin", f"{player.name} 经表单设置常驻地"
                         f" ({x:.0f},{y:.0f},{z:.0f})", who=name)
            player.send_message(
                f"{C.GREEN}{name} 的家已设为 ({x:.0f},{y:.0f},{z:.0f}){C.RESET}")
            self._show_bot_form(player, name)

        form.on_submit = _submit
        sender.send_form(form)

    def _home_clear(self, sender: Any, name: str) -> None:
        if self._homes.pop(name, None) is not None:
            self._save_homes()
            sender.send_message(f"{C.GREEN}{name} 的家已清除{C.RESET}")
        else:
            sender.send_message(f"{C.YELLOW}{name} 本来就没有家{C.RESET}")
        self._show_bot_form(sender, name)

    def _chance_form(self, sender: Any, name: str) -> None:
        """主动回复概率滑条（公屏没点名时搭话的概率）。"""
        from endstone.form import ModalForm, Slider
        current = self.chat_reply_chance(name)
        form = ModalForm(title=f"{name} 的主动回复概率",
                         submit_button="保存")
        form.add_control(Slider(
            label="公屏没点名时，搭话的概率（0% 从不插嘴，100% 每句都回）",
            min=0, max=100, step=5, default_value=int(current * 100)))

        def _submit(player: Any, data: Any) -> None:
            try:
                pct = float(list(data or [0])[0])
            except (TypeError, ValueError, IndexError):
                pct = 0.0
            self.set_chat_reply_chance(name, pct / 100.0)
            self._record("admin", f"{player.name} 设搭话概率 {pct:.0f}%", who=name)
            player.send_message(
                f"{C.GREEN}{name} 的主动回复概率已设为 {pct:.0f}%{C.RESET}")
            self._show_bot_form(player, name)

        form.on_submit = _submit
        sender.send_form(form)

    def _style_form(self, sender: Any, name: str) -> None:
        """说话风格示例编辑（多句用 | 分隔）。"""
        from endstone.form import ModalForm, TextInput
        current = " | ".join(self._style_of(name))
        form = ModalForm(title=f"{name} 的说话风格示例",
                         submit_button="保存")
        form.add_control(TextInput(
            label="示例句子（多句用 | 分隔）。大模型会模仿这些句子的"
                  "语气、口头禅和情绪来回复，不会逐字照抄。",
            placeholder="我是XX，我操我太喜欢这个游戏了 | 这个服务器太勾八好玩了hhh",
            default_value=current))

        def _submit(player: Any, data: Any) -> None:
            raw = str((list(data or [""])[0]) or "")
            examples = [x.strip() for x in raw.split("|") if x.strip()]
            self.set_style(name, examples)
            self._apply_chat_personality(name)
            self._record("admin",
                         f"{player.name} 设说话风格（{len(examples)} 条示例）",
                         who=name)
            player.send_message(
                f"{C.GREEN}{name} 的说话风格已更新（{len(examples)} 条示例，"
                f"下轮对话生效）{C.RESET}")
            self._show_bot_form(player, name)

        form.on_submit = _submit
        sender.send_form(form)

    def _cmd_style(self, sender: CommandSender, args: list[str]) -> None:
        """控制台版风格设置：/ai style <名字> 示例1|示例2。"""
        if args and len(args) == 1 and " " in str(args[0]):
            args = str(args[0]).split(" ", 1)
        if len(args) < 2:
            sender.send_message(f"{C.RED}用法: /ai style <名字> 示例1|示例2{C.RESET}")
            return
        name = _clean_name(args[0])
        examples = [x.strip() for x in " ".join(args[1:]).split("|") if x.strip()]
        self.set_style(name, examples)
        self._apply_chat_personality(name)
        sender.send_message(
            f"{C.GREEN}{name} 的说话风格已更新（{len(examples)} 条示例）{C.RESET}")

    def _cmd_chatrate(self, sender: CommandSender, args: list[str]) -> None:
        """控制台版概率设置：/ai chatrate <名字> <0~1>。"""
        if args and len(args) == 1 and " " in str(args[0]):
            args = str(args[0]).split()
        if len(args) < 2:
            sender.send_message(f"{C.RED}用法: /ai chatrate <名字> <0~1>{C.RESET}")
            return
        name = _clean_name(args[0])
        try:
            chance = max(0.0, min(1.0, float(args[1])))
        except ValueError:
            sender.send_message(f"{C.RED}概率要是 0~1 的小数{C.RESET}")
            return
        self.set_chat_reply_chance(name, chance)
        sender.send_message(
            f"{C.GREEN}{name} 的主动回复概率已设为 {chance:.0%}{C.RESET}")


    def _cmd_home(self, sender: CommandSender, args: list[str]) -> None:
        """常驻地管理：游戏内无参开表单；控制台用文本命令。"""
        # message 型 usage 会把整行塞进一个参数，先摊平
        if args and len(args) == 1 and " " in str(args[0]):
            args = str(args[0]).split()
        # 玩家：无参/单个名字 → 管理表单；控制台：继续走文本解析
        if getattr(sender, "location", None) is not None and (
                not args or (len(args) == 1 and args[0] not in ("set", "clear"))):
            self._show_manage_form(sender)
            return
        if not args:
            sender.send_message(
                f"{C.RED}控制台用法: /ai home <名字> set x y z | clear | 查看{C.RESET}")
            return
        name = _clean_name(args[0])
        sub = args[1].lower() if len(args) > 1 else ""
        if not sub:
            home = self._homes.get(name)
            if home:
                sender.send_message(
                    f"{C.AQUA}{name}{C.RESET} 的家: "
                    f"({home.get('x'):.0f}, {home.get('y'):.0f}, "
                    f"{home.get('z'):.0f})")
            else:
                anchor = self._roam_anchor(name)
                if anchor:
                    sender.send_message(
                        f"{C.YELLOW}{name} 没有家，锚点: {anchor[3]} "
                        f"({anchor[0]:.0f},{anchor[1]:.0f},{anchor[2]:.0f}){C.RESET}")
                else:
                    sender.send_message(f"{C.YELLOW}{name} 没有可用锚点{C.RESET}")
            return
        if sub == "set":
            if len(args) >= 5:
                try:
                    x, y, z = float(args[2]), float(args[3]), float(args[4])
                except ValueError:
                    sender.send_message(f"{C.RED}坐标要是数字{C.RESET}")
                    return
            else:
                loc = getattr(sender, "location", None)
                if loc is None:
                    sender.send_message(
                        f"{C.RED}控制台请用 /ai home <名字> set x y z{C.RESET}")
                    return
                x, y, z = loc.x, loc.y, loc.z
            self.set_home(name, x, y, z)
            self._record("admin", f"{sender.name} 设置常驻地"
                         f" ({x:.0f},{y:.0f},{z:.0f})", who=name)
            sender.send_message(
                f"{C.GREEN}{name} 的家已设为 ({x:.0f},{y:.0f},{z:.0f}){C.RESET}")
            return
        if sub == "clear":
            if self._homes.pop(name, None) is not None:
                self._save_homes()
                sender.send_message(f"{C.GREEN}{name} 的家已清除{C.RESET}")
            else:
                sender.send_message(f"{C.YELLOW}{name} 本来就没有家{C.RESET}")
            return
        sender.send_message(f"{C.RED}未知子命令: {sub}{C.RESET}")

    def _crowd_tick(self) -> None:
        self._crowd.tick()

    def crowd_config(self) -> dict[str, Any]:
        cfg = self._cfg.get("auto_online")
        return cfg if isinstance(cfg, dict) else {}

    def crowd_cards(self) -> list[Any]:
        from .crowd import DEFAULT_CARDS
        raw = self._cfg.get("characters")
        if isinstance(raw, list):
            # 显式的（哪怕空的）列表都尊重——用户写 [] 就是不想上角色。
            return load_cards(raw)
        return load_cards(DEFAULT_CARDS)

    def crowd_llm_ready(self) -> bool:
        try:
            return self._make_llm().configured()
        except Exception:
            return False

    def crowd_reserved_names(self) -> set[str]:
        return set(self._agents) | set(self._external)

    def _reserved_names(self) -> set[str]:
        """所有"自己人"的名字：我们的假人 + 外部身体（如天星）。"""
        return set(self._agents) | set(self._external)

    def crowd_real_player_names(self) -> set[str]:
        """在线**真人**的名字（排除我们自己的假人——它们的 xuid 是空的）。"""
        out: set[str] = set()
        try:
            for p in self.server.online_players:
                if str(getattr(p, "xuid", "") or "").strip():
                    out.add(str(getattr(p, "name", "")))
        except Exception:
            pass
        return out

    def crowd_log(self, level: str, msg: str) -> None:
        fn = getattr(self.logger, level, None)
        if not callable(fn):
            fn = self.logger.info
        fn(msg)
        self._record("crowd", msg)

    def crowd_bot_alive(self, name: str) -> bool:
        agent = self._agents.get(name)
        if agent is not None:
            return bool(agent.alive() and self._live_actor(name) is not None)
        # 还在"等真身加入"的角色也算在线——刚请求生成的那几秒不该被
        # 调度器当成"身体消失"处理掉。
        return name in self._pending_crowd

    def crowd_spawn(self, card: Any) -> tuple[bool, str]:
        """让一张角色卡上线。调度器在主线程里调它。

        真身的加入是异步的（行为包要几秒才把 SimulatedPlayer 拉进来），
        所以这里只**发出请求并登记等待**；`_try_attach_pending` 在感知
        循环里把加入的真身装配成正式智能体。
        """
        if not self._use_real_body:
            return False, "行为包未连接"
        name = _clean_name(card.name)
        if name in self._agents or name in self._external:
            return False, "同名假人已存在"
        if name in self._pending_crowd:
            return False, "同名角色正在上线中"
        pos = self._crowd_spawn_pos()
        if pos is None:
            return False, "找不到合适的出生点"
        x, y, z, grounded = pos
        state = self._request_body(name, x, y, z, stealth=True)
        if state == "failed":
            return False, "行为包无法创建假人"
        # 登记合成身份（角色卡钉死了 xuid/uuid 就用钉定值）
        card_xuid = getattr(card, "xuid", None)
        card_uuid = getattr(card, "uuid", None)
        identity = self._identities.pin(
            name,
            xuid=str(card_xuid).strip() if card_xuid else None,
            uuid=str(card_uuid).strip() if card_uuid else None)
        # 播种聊天个性（搭话概率/风格示例）；改过的设置不会被覆盖
        self._seed_bot_settings(
            name,
            chance=getattr(card, "chat_reply_chance", None),
            style=getattr(card, "style", None))
        goal = str(card.goal) if card.goal else str(self._cfg["default_goal"])
        persona = str(card.persona or "")
        money = float(card.money) if card.money is not None else None
        if state == "joined":
            player = self._find_sim_player(name)
            agent = self._start_agent(name, goal=goal, persona=persona,
                                      actor=player)
            if agent is None:
                self._bridge.remove(name)
                return False, "智能体创建失败"
            self._stealth.add(name)
            self._apply_pending_gm(name, player)
            self._ensure_core_identity(player, name)
            self._core_playtime(name, joining=True)
            self._init_wallet_once(name, money)
            self._announce_join(name)
        else:
            # 等真身加入，_try_attach_pending 接管
            self._pending_crowd[name] = (goal, persona, money,
                                         time.monotonic())
        self._record("spawn", f"角色卡上线请求已发出，目标: {goal[:60]}"
                     f"（身份 xuid={identity['xuid']}）", who=name)
        return True, "已上线" if state == "joined" else "已请求，等待真身加入"

    def crowd_retire(self, name: str, why: str) -> None:
        """让一个角色下线：停大脑 → 收动作 → 撤身体。"""
        was_stealth = name in self._stealth
        # 先结算游玩时长（身体还在，计时数据完整），再做清理动作。
        if was_stealth:
            self._core_playtime(name, joining=False)
        self._stealth.discard(name)
        self._pending_gm.pop(name, None)
        self._pending_crowd.pop(name, None)
        self._arena_disable(name)
        agent = self._agents.pop(name, None)
        actor = self._actors.pop(name, None)
        self._placeholder_flags.pop(name, None)
        self._release_ticking_area(name)
        if agent is not None:
            try:
                agent.stop(why)
            except Exception:
                pass
        try:
            self._executor.cancel_all(name)
        except Exception:
            pass
        ctl = self._bodies.get(name)
        if ctl is not None:
            try:
                ctl.cancel(why)
            except Exception:
                pass
        self._bodies.remove(name)
        self._real_actions.pop(name, None)
        self._compilers.pop(name, None)
        if self._use_real_body:
            self._bridge.remove(name)
        if actor is not None:
            try:
                actor.remove()
            except Exception:
                pass
        if was_stealth:
            self._announce_leave(name)
        self._record("crowd", f"已下线（{why}）", who=name)

    # ------------------------------------------------------------------
    # 进/退服播报（原版风格，只给轮换的群演假人）
    # ------------------------------------------------------------------

    def _announce_join(self, name: str) -> None:
        """真玩家进服时客户端会自己显示"xx 加入了游戏"；假人不会——
        由我们替它广播一条同款文案，不然在线人数变了公屏却没动静。"""
        if name not in self._stealth:
            return
        self._announce("join_message", name)

    def _announce_leave(self, name: str) -> None:
        self._announce("leave_message", name)

    def _announce(self, key: str, name: str) -> None:
        tpl = str(self._cfg.get(key) or "").strip()
        if tpl:
            try:
                self.server.broadcast_message(tpl.replace("{name}", name))
            except Exception:
                pass
        # 进退服也同步 QQ 群（真人进退服在群里是有消息的）。
        self._qq_sync("join" if key == "join_message" else "quit", name)

    def _qq_sync(self, event_type: str, name: str, message: str = "") -> None:
        """把假人的聊天/进退服推给 QQ 同步插件。

        真人聊天走 PlayerChatEvent → arc_qq_sync_astrbot 转发进群；
        假人的 broadcast 不触发事件，群里看不到。这里直调它的
        ``api_send_event``（同一条链路、同一种格式：💬 名字: 消息 /
        🟢 加入 / 🔴 离开）。插件不在、开关关掉、调用失败都静默——
        QQ 同步是锦上添花，不能反过来影响游戏内聊天。
        """
        if event_type == "chat":
            if not bool(self._cfg.get("qq_sync_chat", True)):
                return
        elif event_type in ("join", "quit"):
            if not bool(self._cfg.get("qq_sync_join_leave", True)):
                return
        try:
            plug = self.server.plugin_manager.get_plugin(
                "arc_qq_sync_astrbot")
        except Exception:
            return
        fn = getattr(plug, "api_send_event", None)
        if not callable(fn):
            return
        try:
            fn(event_type, name, name, message)
        except Exception as exc:
            try:
                self.logger.debug(f"QQ 同步 {event_type}({name}) 失败: {exc}")
            except Exception:
                pass

    def _core_playtime(self, name: str, *, joining: bool) -> None:
        """把假人的进/退服报给弧光核心记游玩时长（次数 + 总时长）。

        真人由核心的 PlayerJoin/QuitEvent 记账；SimulatedPlayer 不触发
        这两个事件，假人的"第N次游戏/总时长"永远是 0——QQ 群消息和
        核心统计面板一眼穿帮。这里用假人的**合成 xuid** 包一个最小
        垫片对象，调核心同一套记账函数：主服本地写库，从服走中心
        上报，和真人完全同一条链路。

        ⚠️ 调的是核心的下划线内部方法（没有对外的 api_*）。这几条
        函数是真人计时的主干，改名的概率极低；万一改了，这里的
        hasattr/try 兜底也只是"计时回到 0"，不会出别的错。
        """
        if not bool(self._cfg.get("playtime_sync", True)):
            return
        try:
            core = self.server.plugin_manager.get_plugin("arc_core")
        except Exception:
            return
        if core is None:
            return
        try:
            xuid = str(self._identities.xuid_of(name) or "")
        except Exception:
            return
        if not xuid:
            return
        try:
            if joining:
                fn = getattr(core, "_record_player_join_playtime", None)
                if not callable(fn):
                    return
                from types import SimpleNamespace
                fn(SimpleNamespace(name=name, xuid=xuid))
            else:
                fn = getattr(core, "_record_player_quit_playtime", None)
                if not callable(fn):
                    return
                fn(player=None, xuid=xuid, name=name)
        except Exception as exc:
            try:
                self.logger.debug(f"游玩时长记账失败（{name}）: {exc}")
            except Exception:
                pass

    # ---- 出生点选址 ----------------------------------------------------

    def _crowd_spawn_pos(self) -> tuple[int, int, int, bool] | None:
        """给新角色挑个出生点。返回 (x, y, z, 地面已知)。

        优先级：
          1. 真人身边（区块必然加载，而且"新玩家出生在人群边"最自然）
          2. 已在线假人身边
          3. 配置的 auto_online.home
          4. 世界出生点 + 地表扫描；扫不出来就高空（配合安全落地流程）
        """
        import random as _random
        # 1) 真人
        try:
            humans = [p for p in self.server.online_players
                      if str(getattr(p, "xuid", "") or "").strip()]
        except Exception:
            humans = []
        if humans:
            p = _random.choice(humans)
            try:
                loc = p.location
                return (int(loc.x) + _random.randint(-4, 4), int(loc.y),
                        int(loc.z) + _random.randint(-4, 4), True)
            except Exception:
                pass
        # 2) 已在线假人
        for actor in list(self._actors.values()) + list(self._external.values()):
            if not self._is_live(actor):
                continue
            try:
                loc = actor.location
                return (int(loc.x) + _random.randint(-3, 3), int(loc.y),
                        int(loc.z) + _random.randint(-3, 3), True)
            except Exception:
                continue
        # 3) 配置的 home
        home = self.crowd_config().get("home")
        if isinstance(home, (list, tuple)) and len(home) >= 3:
            try:
                return int(home[0]), int(home[1]), int(home[2]), True
            except (TypeError, ValueError):
                pass
        # 4) 世界出生点
        spawn = self._world_spawn_xz()
        if spawn is not None:
            x, z = spawn
            try:
                dim = self.server.level.get_dimension("overworld")
            except Exception:
                dim = None
            y = self._surface_scan_y(dim, x, z) if dim is not None else None
            if y is not None:
                return x, y, z, True
            return x, 200, z, False
        return None

    def _surface_scan_y(self, dim: Any, x: int, z: int) -> int | None:
        """从上往下找 (x,z) 这一列第一个非空气方块，返回可站立的 y。"""
        try:
            for y in range(318, -64, -1):
                name = self._world.get_block_name(dim, x, y, z)
                if name and name.strip() not in ("", "minecraft:air", "air"):
                    return y + 1
        except Exception:
            return None
        return None

    def _world_spawn_xz(self) -> tuple[int, int] | None:
        """从 level.dat 读世界出生点。读不到（比如权限/格式）返回 None。"""
        cached = getattr(self, "_spawn_xz_cache", None)
        if cached is not None:
            return cached
        try:
            from .nbt import read_bedrock_nbt
            dirs = packing.find_world_dirs(
                level_name=str(getattr(self.server.level, "name", "") or ""))
            for d in dirs:
                ld = d / "level.dat"
                if not ld.exists():
                    continue
                data = read_bedrock_nbt(ld.read_bytes(), has_header=True)
                sx, sz = int(data.get("SpawnX", 0)), int(data.get("SpawnZ", 0))
                if sx or sz:
                    self._spawn_xz_cache = (sx, sz)  # type: ignore[assignment]
                    return self._spawn_xz_cache
        except Exception as exc:
            self.logger.debug(f"读取世界出生点失败: {exc}")
        self._spawn_xz_cache = None  # type: ignore[assignment]
        return None

    # ------------------------------------------------------------------
    # 外部身体 API（供 ai 助手等插件调用）
    # ------------------------------------------------------------------
    #
    # 约定和 arc_core 的 api_* 一致：调用方在**服务器主线程**上调
    # （aihelper 的工具层已经用 _run_on_server_thread 保证这一点），
    # 返回 {"ok": bool, "text"/"error": str} 形状的 dict。
    # 外部身体没有 Python 大脑——它是"遥控躯壳"，智能在调用方。

    def _live_external(self, name: str) -> Any:
        """外部身体的活引用。生成中（值为 None）或已消失都返回 None。

        和 `_live_actor` 同一个坑：缓存的 Player 句柄会过期，判死之前
        先按名字重解析（见 `_live_actor` 的说明）。
        """
        if name not in self._external:
            return None
        actor = self._external.get(name)
        if actor is not None and self._is_live(actor):
            return actor
        player = self._find_sim_player(name)
        if player is not None:
            self._external[name] = player
            return player
        return None

    # ------------------------------------------------------------------
    # 合成身份 API（弧光核心等插件用：空 xuid → 查这里拿稳定身份）
    # ------------------------------------------------------------------

    def api_identity(self, name: str) -> dict:
        """某个假人的 {xuid, uuid}（没有记录就现场生成并终身固定）。"""
        try:
            return {"ok": True, **self._identities.of(_clean_name(str(name)))}
        except Exception as exc:
            return {"ok": False, "error": f"api_identity: {exc}"}

    def api_resolve_xuid(self, player: Any) -> str:
        """给弧光核心的解析入口：Player → xuid。

        · 真人（xuid 非空）→ 原样返回真实 xuid
        · 我们的假人 → 返回它的合成 xuid（16 位、9 开头）
        · 别的空 xuid 玩家（离线登录真人）→ 空串，调用方自行处理
        """
        try:
            real = str(getattr(player, "xuid", "") or "").strip()
            if real:
                return real
            name = str(getattr(player, "name", "") or "")
            if not name or not self._identities.has(name):
                return ""
            # 名字在注册表里——但当前在线的可能是个同名真人（而不是
            # 我们的假人）。只有它确实是模拟玩家时才认领。
            if self._looks_simulated(player):
                return self._identities.xuid_of(name)
            return ""
        except Exception:
            return ""

    def api_identities(self) -> dict:
        """全部已登记身份（名字 → {xuid, uuid}）。"""
        try:
            return {"ok": True, "identities": self._identities.all()}
        except Exception as exc:
            return {"ok": False, "error": f"api_identities: {exc}"}

    def api_ensure_body(self, name: str, x: float | None = None,
                        y: float | None = None, z: float | None = None) -> dict:
        """确保一个常驻身体在线（幂等，掉了会重新拉起）。"""
        try:
            name = _clean_name(str(name))
            if not name:
                return {"ok": False, "error": "名字无效"}
            existing = self._live_external(name)
            if existing is not None:
                return {"ok": True, "text": f"{name} 已在线", "existed": True}
            if name in self._agents or name in self._pending_crowd:
                return {"ok": False, "error": f"{name} 已被本插件的智能体占用"}
            if not self._use_real_body:
                return {"ok": False, "error": "假人行为包未连接"}
            # 外部身体也有合成身份（不然核心库里又落一个空 xuid 行）
            self._identities.of(name)
            if x is None or y is None or z is None:
                pos = self._crowd_spawn_pos()
                if pos is None:
                    return {"ok": False, "error": "找不到出生点"}
                x, y, z, _grounded = pos
            state = self._request_body(name, int(x), int(y), int(z))
            if state == "failed":
                return {"ok": False, "error": "行为包无法创建假人"}
            if state == "joined":
                player = self._find_sim_player(name)
                self._external[name] = player
                self._apply_pending_gm(name, player)
                self._ensure_core_identity(player, name)
                self._record("spawn", "外部身体已上线（无 AI 大脑，由外部插件操控）",
                             who=name)
                return {"ok": True, "text": f"{name} 已上线"}
            # 真身还要几秒才加入。挂一个"生成中"的牌子，感知循环里
            # _try_attach_pending 会把它接上。
            self._external[name] = None
            self._record("spawn", "外部身体生成中（等待真身加入）", who=name)
            return {"ok": True, "text": f"{name} 生成中，几秒后上线"}
        except Exception as exc:
            return {"ok": False, "error": f"api_ensure_body: {exc}"}

    def api_remove_body(self, name: str) -> dict:
        try:
            name = _clean_name(str(name))
            self._external.pop(name, None)
            self._pending_crowd.pop(name, None)
            self._pending_gm.pop(name, None)
            self._bridge.remove(name)
            return {"ok": True, "text": f"{name} 已下线"}
        except Exception as exc:
            return {"ok": False, "error": f"api_remove_body: {exc}"}

    def api_body_status(self, name: str = "") -> dict:
        """所有身体的位置/状态。外部身体与智能体假人都在列。"""
        try:
            bodies: list[dict[str, Any]] = []
            for n in list(self._external):
                actor = self._live_external(n)
                if actor is None:
                    pending = self._external.get(n) is None and n in self._external
                    bodies.append({"name": n, "online": False, "kind": "external",
                                   "note": "生成中" if pending else "离线"})
                    continue
                loc = actor.location
                bodies.append({
                    "name": n, "online": True, "kind": "external",
                    "x": round(loc.x, 1), "y": round(loc.y, 1), "z": round(loc.z, 1),
                })
            for n, agent in self._agents.items():
                actor = self._live_actor(n)
                entry: dict[str, Any] = {
                    "name": n, "online": actor is not None,
                    "kind": "crowd" if n in self._stealth else "agent",
                    "busy": bool(agent._inflight),
                }
                if actor is not None:
                    loc = actor.location
                    entry.update({"x": round(loc.x, 1), "y": round(loc.y, 1),
                                  "z": round(loc.z, 1)})
                bodies.append(entry)
            if name:
                want = name.lower()
                bodies = [b for b in bodies if b["name"].lower() == want]
            return {"ok": True, "bodies": bodies,
                    "text": "; ".join(
                        f"{b['name']}({b.get('kind', '?')}"
                        + (f"@{b.get('x')},{b.get('y')},{b.get('z')}"
                           if b.get("online") else " 离线") + ")"
                        for b in bodies) or "没有身体在线"}
        except Exception as exc:
            return {"ok": False, "error": f"api_body_status: {exc}"}

    def api_body_action(self, name: str, action: str, **kwargs: Any) -> dict:
        """遥控一具身体做一件事。动作即时下发，结果异步（可用 api_body_status 查）。

        action ∈ walk(x,y,z) / teleport(x,y,z) / attack(target) / jump /
        stop / say(text) / drop(item, count, to)
        """
        try:
            name = _clean_name(str(name))
            action = str(action or "").strip().lower()
            external = self._live_external(name)
            agent_body = self._live_actor(name)
            if external is None and agent_body is None:
                return {"ok": False, "error": f"{name} 不在线"}
            if external is None and name not in self._external:
                return {"ok": False,
                        "error": f"{name} 是本插件的智能体假人，不接受遥控"}
            if action in ("walk", "goto"):
                x, y, z = int(kwargs["x"]), int(kwargs["y"]), int(kwargs["z"])
                rid = self._bridge.walk(name, x, y, z, timeout=300)
                return {"ok": rid >= 0, "text": f"已下令走向 ({x},{y},{z})"}
            if action == "teleport":
                x, y, z = float(kwargs["x"]), float(kwargs["y"]), float(kwargs["z"])
                rid = self._bridge.teleport(name, x, y, z)
                return {"ok": rid >= 0, "text": f"已传送到 ({x},{y},{z})"}
            if action == "attack":
                target = str(kwargs.get("target", "")).strip()
                if not target:
                    return {"ok": False, "error": "attack 需要 target"}
                rid = self._bridge.attack(name, target)
                return {"ok": rid >= 0, "text": f"已下令攻击 {target}"}
            if action == "jump":
                ok = self._bridge.jump(name)
                return {"ok": ok, "text": "已下令跳跃"}
            if action == "stop":
                ok = self._bridge.stop(name)
                return {"ok": ok, "text": "已下令停止"}
            if action == "say":
                text = str(kwargs.get("text", "")).strip()
                if not text:
                    return {"ok": False, "error": "say 需要 text"}
                self._broadcast(name, text)
                return {"ok": True, "text": "已发言"}
            if action == "drop":
                item = str(kwargs.get("item", "")).strip()
                if not item:
                    return {"ok": False, "error": "drop 需要 item"}
                count = int(kwargs.get("count", 1) or 1)
                to = str(kwargs.get("to", "") or "")
                rid = self._bridge.drop(name, item, count, to=to)
                return {"ok": rid >= 0,
                        "text": f"已下令丢出 {count} 个 {item}"
                                + (f" 给 {to}" if to else "")}
            return {"ok": False,
                    "error": f"未知动作 {action}，可用: walk/teleport/"
                             f"attack/jump/stop/say/drop"}
        except Exception as exc:
            return {"ok": False, "error": f"api_body_action: {exc}"}

    # ------------------------------------------------------------------
    # 找假人（不是找玩家）
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_simulated(player: Any) -> bool:
        """这个 Player 到底是不是假人。

        为什么不能只比名字：`server.get_player(name)` 是**按名字查玩家**，
        它不区分"假人"和"用这个名字登录的真人"。一旦命中，插件就会把
        那个真人当成假人的身体——然后玩家一退出游戏，身体就"消失"，
        智能体跟着停摆。

        ── 判据的演进（两个坑，都值得记） ──────────────────────────

        **第一版靠"能力探测"**：数它有没有 `disconnect` /
        `navigate_to_location` / `stop_moving` / `set_skin` /
        `start_build` 这些**假人专有方法**。

        这个思路在 Endstone 上是错的。那些方法是
        `@minecraft/server-gametest` 的 **JS 侧** API，
        Endstone 并没有把它们桥接到 Python 的 `Player` 对象上。
        实测（Endstone 0.11.11，见 2.0.0 的用户日志）：
        五个方法一个都不存在，于是这个函数**恒返回 False**，
        真身每次都被判成"同名真人"而遭忽略。

        **第二版改用"三项设备特征全空"**，要求 `device_id` /
        `device_os` / `game_version` **三个都读得到且都为空**才算假人。
        它比第一版好，但在真机上**还是返回 False**（2.1.0 用户日志
        `08:59:43 Player Spawned: hly xuid: ,` 后面紧跟同名警告）。
        原因是那个 `for...else` 太苛刻：三项里只要有一项 `hasattr`
        为假就 `break` 出去，而 xuid 兜底里 `hasattr(player,"xuid")`
        同样可能为假——两头都落空，最后还是 False。

        **第三版（现在这版）主判据换成 `xuid`。** 理由很直接：
        `xuid` 是 Xbox Live 账号 ID，只有**真正通过 Xbox Live 认证的
        客户端**才有；SimulatedPlayer 没有客户端，必然是空的。
        而且实测证明这个属性**读得到**（日志里明明白白打了
        `xuid: ,`——有冒号、没值），比三项设备特征可靠得多。

        ⚠️ **代价要说清楚：离线登录的真人 xuid 也是空的。**
        历史日志里出现过
        `Player Spawned: xxx xuid: , pfid: 99d7a812...`——
        那是个离线模式的真玩家。在这种服务器上，本函数会把离线真人
        认成假人，于是 AI 可能接上真人的身体，他一退游戏智能体就停。

        这是**用户明确认可的取舍**：宁可认错，也不能像前两版那样
        一个都认不出来。缓解措施见 `_warn_offline_risk`——
        启动自检时会提示"如果服务器有离线真人，请开正版验证"。
        """
        if player is None:
            return False

        # 0) 万一某个 Endstone 版本真的暴露了明确属性，优先信它。
        #    这是最权威的判据，有就用，别猜。
        for attr in ("is_simulated", "isSimulated"):
            try:
                v = getattr(player, attr, None)
                if v is not None:
                    return bool(v)
            except Exception:
                pass

        # 1) 类名兜底。Endstone 将来若真把 SimulatedPlayer 暴露成
        #    独立子类，这条会直接命中。
        cls = type(player).__name__.lower()
        if "simulated" in cls:
            return True

        # 2) 主判据：xuid 读得到且为空 → 假人。
        #
        #    ⚠️ 必须用 hasattr 区分"属性不存在"和"属性的值是空"。
        #    只写 `getattr(p, "xuid", None)` 的话，属性不存在也会得到
        #    None，和"值就是空的"混在一起，方向就糊了。
        #
        #    这里只问 xuid，**不再要求**设备特征齐全——上一版就是
        #    因为要求太多（三项都得在）才在真机上一个都认不出来的。
        if hasattr(player, "xuid"):
            try:
                xuid = getattr(player, "xuid")
            except Exception:
                xuid = None
            if xuid is None or str(xuid).strip() == "":
                return True
            # xuid 有值 = 过了 Xbox Live 认证 = 真玩家。
            # 这一条是**强否决**：直接收工，不用再看别的。
            return False

        # 3) xuid 属性整个读不到（很旧的 Endstone）——
        #    退到设备特征。三项都存在且全空才算假人。
        #
        #    这一段刻意保留"三项都要在"的严格性：既然拿不到最可靠的
        #    xuid，就只能保守一点，认不出来总比认错强。
        probes = ("device_id", "device_os", "game_version")
        empty = 0
        for attr in probes:
            if not hasattr(player, attr):
                break
            try:
                v = getattr(player, attr)
            except Exception:
                break
            if v is None or str(v).strip() == "":
                empty += 1
            else:
                return False        # 有一项有值 → 有客户端 → 真人
        else:
            if empty == len(probes):
                return True

        return False

    def _find_sim_player(self, name: str) -> Any:
        """在线的玩家列表里找出属于这个假人的那一个。

        先按名字缩小范围，再用 `_looks_simulated` 把真人剔掉。
        """
        try:
            online = list(self.server.online_players)
        except Exception:
            online = []

        same_name = []
        for p in online:
            try:
                if str(getattr(p, "name", "")) == name:
                    same_name.append(p)
            except Exception:
                continue

        for p in same_name:
            if self._looks_simulated(p):
                return p

        if same_name:
            # 有同名的在线玩家，但不是假人——这几乎肯定是服主自己。
            #
            # ⚠️ 这条警告必须**只打一次**。它在感知循环里（每 10 tick
            # 一轮，也就是 0.5 秒），只要判据有任何偏差，日志就会被同一
            # 句话刷爆——用户实测时 20 多行全是它，真正的错误被淹了。
            # 所以按名字记账，之后降级为 debug。
            if name not in self._name_clash_warned:
                self._name_clash_warned.add(name)
                self.logger.warning(
                    f"{C.YELLOW}发现与假人同名的在线玩家「{name}」，"
                    f"但它看起来不是模拟玩家，已忽略。"
                    f"请换一个假人名字避免歧义。{C.RESET}")
            else:
                self.logger.debug(
                    f"「{name}」仍未通过假人校验，继续忽略（已警告过）")

        # 兜底：直接问一次。某些 Endstone 版本里 SimulatedPlayer 不出现在
        # online_players 里，但 get_player 能查到。查到后仍要过同一道校验。
        try:
            p = self.server.get_player(name)
        except Exception:
            return None
        if self._looks_simulated(p):
            return p
        return None

    def _spawn_body(self, name: str, loc: Any) -> tuple[Any, str]:
        """生成身体。

        到这里时 `_require_body()` 已经确认过身体层可用，所以只有两种可能：
        行为包造真假人，或者用户明确选了 npc 模式。
        """
        mode = str(self._cfg.get("body_mode", "auto")).lower()

        if mode != "npc":
            try:
                dim = getattr(loc, "dimension", None)
                dim_name = short_id(getattr(dim, "name", "overworld")) if dim else "overworld"
            except Exception:
                dim_name = "overworld"

            # 生成请求发给行为包。它那边会优先走模块级 spawnSimulatedPlayer
            # （不需要 GameTest），拿不到才退到 GameTest。
            #
            # `game_mode` 一起传过去：SimulatedPlayer 会**继承生成时所在
            # 世界/玩家的游戏模式**，所以模式必须在"造"的那一刻就定下来，
            # 事后由 Python 去改是补救、而且越界（直接写假人对象的属性）。
            self._bridge.spawn(
                name, loc.x, loc.y, loc.z, dim_name,
                game_mode=self._bot_game_mode_number(),
            )

            # 找一下刚加入的假人。
            candidate = self._find_sim_player(name)
            if candidate is not None:
                return candidate, "真实假人"

            # 真身还没进来。这里以前是无条件造一个 NPC 占位，屏幕上看着像
            # 生成成功了，其实是个动不了的空壳——用户报的"放出来还是 NPC"
            # 就是这么来的。
            #
            # 现在先问一句行为包：你到底能不能造？
            #   · 能造（模块级 API 可用，或 GameTest 已启动）→ 造占位等它，
            #     下一轮感知会把占位换掉。这是**暂时**的等待。
            #   · 不能造（beta 模块没开、引擎版本不支持）→ 真身永远不会来。
            #     这时候再造 NPC 就是骗人，而且会一直骗下去。直接失败，
            #     把原因说清楚，让服主去开 Beta APIs。
            if not self._bridge.can_spawn_directly:
                self.logger.error(
                    f"{C.RED}行为包无法创建假人（模块级 spawnSimulatedPlayer "
                    f"不可用且 GameTest 未启动）。{C.RESET}")
                # ⚠️ 上面那条 `bot:spawn` 已经发出去了。虽然按当前能力
                # 判断它造不出来，但"判断"和"事实"可能不一致（比如行为包
                # 在收到请求的那一刻刚好把 GameTest 跑起来了）。真身要是
                # 这时候加入，就会变成一个**没有任何智能体控制的实体**，
                # 站在那里永远不动，而用户完全不知道为什么。
                # 所以失败路径必须显式撤回，不能只是"什么都不做"。
                self._bridge.remove(name)
                return None, ""

            self.logger.info(
                f"已请求行为包生成 {name}，等待其加入服务器…")
            ph = self._make_placeholder(name, loc)
            if ph is None:
                # 占位也造不出来（比如 spawn_entity 无效）。直接返回
                # 失败，别让上层拿到 None 还当成"生成成功"。
                #
                # 同样要撤回那条 `bot:spawn`：行为包多半已经收到并在
                # 造真身了，不撤的话真身加入后没人控制。
                self._bridge.remove(name)
                return None, ""
            return ph, "占位实体(等真身加入)"

        entity_type = str(self._cfg.get("spawn_entity", "minecraft:npc"))
        try:
            actor = self._dimension_of(loc).spawn_actor(loc, entity_type)
            if actor is not None:
                return actor, "npc 占位实体"
        except Exception as exc:
            self.logger.warning(f"生成实体失败: {exc}")
        return None, ""

    def _make_placeholder(self, name: str, loc: Any) -> Any:
        """行为包还没把假人加进来时，先返回一个可用的实体。

        这样 Agent 不会因为 get_actor() 返回 None 而立刻判定"身体不存在"。

        ⚠️ 它只是个**占位**：是个 NPC 实体，所以显示的是 NPC 皮肤，
        而且不会真的走动、不会自己挖方块。真正的假人加入服务器之后
        必须把它换掉 —— 见 `_upgrade_placeholder`。
        """
        try:
            dim = self._dimension_of(loc)
            actor = dim.spawn_actor(
                loc, str(self._cfg.get("spawn_entity", "minecraft:npc")))
        except Exception as exc:
            self.logger.debug(f"生成占位实体失败: {exc}")
            return None
        if actor is not None:
            # 打个标记：这只是占位，真身一来就得换掉。
            # 没有这个标记的话，屏幕上那个 NPC 会一直挂着——
            # 用户看到的就是"假人还是 NPC 的皮肤"。
            self._placeholder_flags[name] = True
        return actor

    def _dimension_of(self, loc: Any) -> Any:
        """从坐标反推维度。

        写死 "overworld" 是错的：玩家在下界/末地执行 /ai spawn 时，
        实体应该生成在他身边，而不是莫名其妙出现在主世界。
        """
        dim = getattr(loc, "dimension", None)
        if dim is not None:
            return dim
        name = getattr(loc, "dimension_name", None)
        if name:
            try:
                return self.server.level.get_dimension(short_id(str(name)))
            except Exception:
                pass
        return self.server.level.get_dimension("overworld")

    def _upgrade_placeholder(self, name: str) -> bool:
        """占位实体换成真正的假人。换成功返回 True。

        这是"假人显示 NPC 皮肤"的根因修复点：只有真实假人
        （SimulatedPlayer）加入服务器之后，`server.get_player(name)`
        才拿得到它。之前 `_make_placeholder` 创建了 NPC 就再没人管它，
        所以屏幕上那个 NPC 会一直挂着 —— 它既是错误的外观，也是个
        完全动不了的空壳。

        被换下来的 NPC 要 remove 掉，不然它会永远站在原地。

        ⚠️ 这里必须用 `_find_sim_player` 而不是直接 `get_player`。
        直接查名字的话，一个用同名账号登录的**真人**会被当成假人接上，
        然后他一退游戏身体就"消失"。

        ⚠️ 还有一个前置检查：**这个智能体自己还活着吗**。
        `_perceive` 是先升级、再判存活的，而 `_bury_body` 只调
        `agent.stop()`、**不把名字从 `_agents` 里摘掉**（摘掉是
        `/ai remove` 干的活）。于是已停摆的智能体仍会被每 10 tick
        遍历一次，只要它的占位标记还在（比如上一轮升级刚好失败、
        或者行为包迟了一步才把真身加进来），就能在这里被"复活"——
        `_actors[name]` 被重新填上一具没人控制的真身，站在那儿
        永远不动，而 `_agents[name]` 是个已经死掉的智能体。
        所以这里先问一句：这个智能体还活着吗？不活着就别管它。
        """
        agent = self._agents.get(name)
        if agent is not None and not agent.alive():
            # 智能体已经停了。顺手把占位标记清掉，免得这一轮又进来一次。
            self._placeholder_flags.pop(name, None)
            return False

        player = self._find_sim_player(name)
        if player is None:
            return False

        old = self._actors.get(name)
        if old is not None and old is not player:
            try:
                old.remove()
            except Exception:
                pass

        # 假人上线就按配置锁游戏模式。
        #
        # 用户实测："我用创造模式打那个假人，假人不扣血"——因为
        # SimulatedPlayer 继承**生成时所在世界**的游戏模式，服主在
        # 创造模式里 spawn 出来的假人就一直是创造模式（无敌）。
        # 强制设成 survival 之后它才和真人一样会受伤、会被打退。
        # 假人上线后**只读校验**模式对不对（模式本身在生成时就定好了）。
        #
        # 用户实测："我用创造模式打那个假人，假人不扣血"——因为
        # SimulatedPlayer 继承生成时所在世界的游戏模式。现在模式跟着
        # spawn 请求一起传给 JS 侧，在生成那一刻就生效。这里只负责
        # 发现"没生效"并报警，不再动手改。
        self._verify_bot_game_mode(player, name)

        # 高空安全落地的收尾：落地时是创造模式（防摔死），站稳后切回
        # 配置的模式。见 _request_body 的说明。
        self._apply_pending_gm(name, player)
        self._ensure_core_identity(player, name)

        # 外部身体（助手假人）真身接上时也要登记——它没走 _agents 流程。
        if name in self._external:
            self._external[name] = player

        self._actors[name] = player
        self._placeholder_flags.pop(name, None)
        ctl = self._bodies.get(name)
        if ctl is not None:
            ctl.actor = player
        try:
            self._world.body_dimension = player.dimension
        except Exception:
            pass

        self._record("spawn", "真实假人已加入，占位实体已替换", who=name)
        self.logger.info(
            f"{C.GREEN}[{name}] 真实假人已加入，已替换占位实体{C.RESET}")
        # 到这一步才是真的"上线了"。广播放这里，不放 _cmd_spawn——
        # 那句无条件广播曾是用户误判的来源。
        # 隐身假人（自动在线的群演）不广播——那会当场穿帮。
        if name not in self._stealth:
            try:
                self.server.broadcast_message(
                    f"{C.GOLD}⚡ AI 虚拟玩家 {name} 已接管真实假人"
                    f"（由大模型驱动）{C.RESET}")
            except Exception:
                pass
        return True

    #: 配置里的游戏模式 → Bedrock 数字。0=survival 1=creative 2=adventure 3=spectator
    _GAME_MODE_NUMBERS = {
        "survival": 0,
        "creative": 1,
        "adventure": 2,
        "spectator": 3,
    }

    def _bot_game_mode_number(self) -> int | None:
        """把 `bot_game_mode` 配置翻译成传给行为包的数字。

        返回 `None` 表示"不干预"——包括两种情况：
          · 配置是 `keep` / 空（用户明确要求别动它）
          · 配置值不认识（已经警告过，宁可不干预也不要瞎猜）

        为什么翻译放在 Python 侧、而不是把字符串直接丢给 JS：
        JS 那边 `gametest.spawnSimulatedPlayer` 收的是 `GameMode` 枚举，
        传字符串在部分引擎版本上会静默失败（退化成默认模式）。数字是
        跨版本最稳的表示。
        """
        want = str(self._cfg.get("bot_game_mode", "survival")).lower().strip()
        if want in ("keep", "none", ""):
            return None
        num = self._GAME_MODE_NUMBERS.get(want)
        if num is None:
            self.logger.warning(
                f"{C.YELLOW}bot_game_mode 的值「{want}」不认识，"
                f"假人将保持默认游戏模式。可用值："
                f"{' / '.join(self._GAME_MODE_NUMBERS)} / keep{C.RESET}")
        return num

    def _verify_bot_game_mode(self, player: Any, name: str) -> None:
        """**只读校验**假人的游戏模式是不是配置要求的那个。

        注意这里和 2.x 的 `_apply_bot_game_mode` 有个本质区别：
        **它不再写 `player.game_mode`。**

        原因：假人由 JS 侧的 `spawnSimulatedPlayer` 创建，模式在**生成时**
        就该定好（SimulatedPlayer 会继承生成时所在世界的模式）。生成后
        再由 Python 写属性属于事后补救，也越界——按用户定的架构，
        Python 只发指令，不直接操作假人对象。

        所以这里只做两件事：读一次、比对、不一致就警告。
        这样既能发现"配置没生效"，又不破坏控制权边界。
        """
        want = self._bot_game_mode_number()
        if want is None:
            return

        try:
            current = player.game_mode
        except Exception as exc:
            # 占位实体（NPC）上没有 game_mode —— 它不是 Player，
            # 这不是错误，正常跳过。
            self.logger.debug(f"[{name}] 读不到游戏模式（可能是占位实体）: {exc}")
            return

        # 比对要归一化：Endstone 给的是 `GameMode` 枚举，但我们拿到手的
        # 也可能是字符串（取决于绑定层）。统一取 `.name` 再小写比较。
        cur_num: int | None = None
        cur_name = str(getattr(current, "name", current)).lower().strip()
        cur_num = self._GAME_MODE_NUMBERS.get(cur_name)

        if cur_num is None:
            # 枚举名认不出来（将来加的模式？）。只能打印出来让人看。
            self.logger.debug(
                f"[{name}] 游戏模式读出来是「{current}」，无法与期望值比对")
            return

        if cur_num != want:
            want_name = [k for k, v in self._GAME_MODE_NUMBERS.items()
                         if v == want][0]
            self.logger.warning(
                f"{C.YELLOW}[{name}] 游戏模式是「{cur_name}」，"
                f"但配置要求「{want_name}」。"
                f"生成参数可能没被引擎采纳——"
                f"请用 /ai body 看诊断，或把 bot_game_mode 改成 keep "
                f"接受当前模式。{C.RESET}")

    def _cmd_list(self, sender: CommandSender, args: list[str]) -> None:
        if not self._agents:
            sender.send_message(f"{C.YELLOW}当前没有智能体{C.RESET}")
            return
        sender.send_message(f"{C.GOLD}=== 智能体 ({len(self._agents)}) ==={C.RESET}")
        for name, agent_obj in self._agents.items():
            s = agent_obj.snapshot()
            state = "💭思考" if s["thinking"] else ("✅结束" if s["done"] else "▶运行")
            if s["busy"]:
                state += f" 执行中x{s['busy']}"
            sender.send_message(f"{C.AQUA}{s['name']}{C.RESET} {state}")
            sender.send_message(
                f"  {C.GRAY}轮次{s['turns']} 错误{s['errors']} "
                f"| 工具调用{s['tool_calls']}次 → 实际执行{s['steps']}步 "
                f"(放大 {s['efficiency']}x){C.RESET}")
            # 真实身体的统计
            ctl = self._bodies.get(name)
            if ctl is not None:
                st = ctl.stats()
                busy = f" 当前:{ctl.current}" if ctl.busy else ""
                sender.send_message(
                    f"  {C.GRAY}身体: 挖{st['digs_done']}成功/{st['digs_failed']}失败 "
                    f"走{st['walks_done']}/{st['walks_failed']}{busy}{C.RESET}")
            sender.send_message(f"  {C.GRAY}目标: {s['goal'][:56]}{C.RESET}")
            # 好感度与战斗状态
            cfg = self._social_cfg()
            favor_line = self._favor.describe(
                name, sorted(self.crowd_real_player_names()),
                grudge=float(cfg.get("grudge_threshold", -30.0)),
                friend=float(cfg.get("friend_threshold", 60.0)))
            fighting = self._reflex.target_of(name)
            extra = f" | {C.RED}战斗中→{fighting}{C.RESET}" if fighting else ""
            sender.send_message(
                f"  {C.GRAY}{favor_line or '好感度：暂无记录'}{extra}{C.RESET}")
            sender.send_message(
                f"  {C.GRAY}{agent_obj.describe_context()}{C.RESET}")

    def _cmd_ctx(self, sender: CommandSender, args: list[str]) -> None:
        """查看/调整上下文预算，必要时手动压一次。"""
        if not self._agents:
            sender.send_message(f"{C.YELLOW}当前没有智能体{C.RESET}")
            return

        if args and args[0].lower() == "compact":
            for name, ag in self._agents.items():
                if ag.maybe_compact():
                    sender.send_message(f"{C.GREEN}{name}：已开始压缩{C.RESET}")
                else:
                    sender.send_message(
                        f"{C.GRAY}{name}：未达上限，无需压缩{C.RESET}")
            return

        if args and args[0].lower() == "clear":
            for name, ag in self._agents.items():
                ag.clear_context()
                sender.send_message(f"{C.GREEN}{name}：上下文已清空{C.RESET}")
            return

        if len(args) >= 2 and args[0].isdigit():
            limit = max(2000, min(400000, int(args[0])))
            self._cfg["context_max_chars"] = limit
            self._save_config()
            for ag in self._agents.values():
                ag.config.context_max_chars = limit
            sender.send_message(
                f"{C.GREEN}上下文上限已设为 {limit} 字符{C.RESET}"
                f"{C.GRAY}（下轮起生效）{C.RESET}")
            return

        sender.send_message(f"{C.GOLD}=== 上下文预算 ==={C.RESET}")
        sender.send_message(
            f"{C.GRAY}上限 {self._cfg['context_max_chars']} 字符"
            f" · 压缩后保留最近 {self._cfg['context_keep_recent']} 条"
            f" · 总结 {'开' if self._cfg['context_summarize'] else '关'}{C.RESET}")
        for name, ag in self._agents.items():
            sender.send_message(f"  {C.AQUA}{name}{C.RESET} {ag.describe_context()}")
            if ag.state.digest:
                preview = ag.state.digest.replace("\n", " ")[:150]
                sender.send_message(f"    {C.GRAY}提要: {preview}…{C.RESET}")
            else:
                sender.send_message(f"    {C.GRAY}（尚无提要）{C.RESET}")
        sender.send_message(
            f"{C.GRAY}用法: /ai ctx 12000 · /ai ctx compact · /ai ctx clear{C.RESET}")

    #: 提示词段落的 key（不含 prompt_ 前缀）→ AgentConfig 上的字段名。
    #: 两者恰好同名，所以这个映射现在是恒等的——但写成显式的表，
    #: 将来任一侧改名时不会静默错位。
    _PROMPT_FIELDS = {
        "identity": ("prompt_identity", "identity", "身份宣言"),
        "body": ("prompt_body", "body", "身体说明"),
        "persona": ("prompt_persona", "persona", "性格"),
        "rules": ("prompt_rules", "rules", "行为规范"),
        "item_names": ("prompt_item_names", "item_names", "物品对照表"),
        "surface_hint": ("prompt_surface_hint", "surface_hint", "表面方块说明"),
    }

    def _apply_prompt(self, key: str) -> None:
        """把某个提示词段落的新值推给所有**现有**智能体。

        build_system 每轮都从 state.config 读，所以改完下一轮就生效，
        不用重启也不用重新 spawn。
        """
        cfg_key, field, _ = self._PROMPT_FIELDS[key]
        for ag in self._agents.values():
            setattr(ag.config, field, str(self._cfg[cfg_key]))

    def _cmd_prompt(self, sender: CommandSender, args: list[str]) -> None:
        """查看/改写提示词段落。

        这些段落存在 config.json 里，所以照理可以直接改文件——但那个
        要重启服务器，而且段落的默认值是多行字符串，手编 JSON 很容易
        把换行写坏。这个命令用来在游戏里快速调。
        """
        defaults = {
            "identity": DEFAULT_IDENTITY, "body": DEFAULT_BODY,
            "persona": DEFAULT_PERSONA, "rules": DEFAULT_RULES,
            "item_names": DEFAULT_ITEM_NAMES,
            "surface_hint": DEFAULT_SURFACE_HINT,
        }

        if not args:
            sender.send_message(
                f"{C.GOLD}=== 提示词段落（可改）{C.RESET}")
            for short, (cfg_key, _, label) in self._PROMPT_FIELDS.items():
                cur = str(self._cfg.get(cfg_key, ""))
                if cur == defaults[short]:
                    state = f"{C.GRAY}默认{C.RESET}"
                else:
                    state = f"{C.YELLOW}已改{C.RESET}"
                sender.send_message(
                    f"  {C.AQUA}{short}{C.RESET} {label} "
                    f"{C.GRAY}{len(cur)} 字 · {C.RESET}{state}")
            sender.send_message(
                f"{C.GRAY}用法: /ai prompt <段落> <新文本> · "
                f"/ai prompt reset [段落]{C.RESET}")
            return

        sub = args[0].lower()

        if sub == "reset":
            if len(args) > 1:
                one = args[1].lower()
                if one not in self._PROMPT_FIELDS:
                    sender.send_message(
                        f"{C.RED}没有这个段落: {args[1]}。可选: "
                        f"{'、'.join(self._PROMPT_FIELDS)}{C.RESET}")
                    return
                targets = [one]
            else:
                targets = list(self._PROMPT_FIELDS)
            for short in targets:
                cfg_key = self._PROMPT_FIELDS[short][0]
                self._cfg[cfg_key] = defaults[short]
                self._apply_prompt(short)
            self._save_config()
            sender.send_message(
                f"{C.GREEN}已恢复默认: {'、'.join(targets)}{C.RESET}"
                f"{C.GRAY}（对所有假人立即生效）{C.RESET}")
            return

        if sub not in self._PROMPT_FIELDS:
            sender.send_message(
                f"{C.RED}没有这个段落: {sub}。可选: "
                f"{'、'.join(self._PROMPT_FIELDS)}{C.RESET}")
            return

        if len(args) < 2:
            cfg_key, _, label = self._PROMPT_FIELDS[sub]
            sender.send_message(f"{C.GOLD}=== prompt_{sub}（{label}）==="
                                f"{C.RESET}")
            for line in str(self._cfg[cfg_key]).splitlines():
                sender.send_message(f"{C.GRAY}{line}{C.RESET}")
            return

        text = " ".join(args[1:])
        # 允许 \n 写换行——命令行里打不出真换行
        text = text.replace("\\n", "\n")
        cfg_key = self._PROMPT_FIELDS[sub][0]
        self._cfg[cfg_key] = text
        self._apply_prompt(sub)
        self._save_config()
        sender.send_message(
            f"{C.GREEN}prompt_{sub} 已更新（{len(text)} 字）{C.RESET}"
            f"{C.GRAY}（下轮起生效）{C.RESET}")

    def _cmd_sense(self, sender: CommandSender, args: list[str]) -> None:
        """查看/切换"只报表面方块"的两个开关。"""
        def _bool(v: str) -> bool | None:
            v = v.lower()
            if v in ("on", "true", "1", "yes"):
                return True
            if v in ("off", "false", "0", "no"):
                return False
            return None

        if args:
            # /ai sense dig on  或  /ai sense off
            if args[0].lower() == "dig":
                want = _bool(args[1]) if len(args) > 1 else None
                if want is None:
                    sender.send_message(f"{C.RED}用法: /ai sense dig on|off{C.RESET}")
                    return
                self._cfg["dig_surface_only"] = want
                for c in self._compilers.values():
                    c.surface_only = want
                self._save_config()
                sender.send_message(
                    f"{C.GREEN}挖掘只找表面方块: "
                    f"{'开' if want else '关'}{C.RESET}")
                return
            want = _bool(args[0])
            if want is None:
                sender.send_message(f"{C.RED}用法: /ai sense on|off{C.RESET}")
                return
            self._cfg["surface_only"] = want
            self._world.surface_only = want
            # 关掉表面过滤时，"你只看得见表面方块"那段就不成立了，
            # 留着会让模型怀疑观测数据。所以同步更新提示词。
            for ag in self._agents.values():
                ag.config.surface_hint = (str(self._cfg["prompt_surface_hint"])
                                          if want else "")
            self._save_config()
            sender.send_message(
                f"{C.GREEN}感知只报表面方块: {'开' if want else '关'}{C.RESET}")
            return

        s = bool(self._cfg["surface_only"])
        d = bool(self._cfg["dig_surface_only"])
        sender.send_message(f"{C.GOLD}=== 表面方块过滤 ==={C.RESET}")
        sender.send_message(
            f"  感知 ({C.AQUA}surface_only{C.RESET}):     "
            f"{'开' if s else '关'}")
        sender.send_message(
            f"  挖掘 ({C.AQUA}dig_surface_only{C.RESET}): "
            f"{'开' if d else '关'}")
        sender.send_message(
            f"{C.GRAY}六面中至少一面贴着空气或水才算\"表面\"。"
            f"关掉感知那项等于允许模型隔着石头看到矿石。{C.RESET}")
        if s != d:
            sender.send_message(
                f"{C.YELLOW}⚠ 两个开关不一致：模型会发现"
                f"\"看不见但能挖到\"，从而绕过感知。建议同开同关。{C.RESET}")
        sender.send_message(
            f"{C.GRAY}用法: /ai sense on|off · /ai sense dig on|off{C.RESET}")

    def _cmd_persona(self, sender: CommandSender, args: list[str]) -> None:
        """给某个假人改性格（覆盖全局 persona 段，下轮决策生效）。"""
        if len(args) < 2:
            sender.send_message(f"{C.RED}用法: /ai persona <名字> <性格描述>{C.RESET}")
            return
        agent = self._agents.get(args[0])
        if agent is None:
            sender.send_message(f"{C.RED}未找到{C.RESET}")
            return
        text = " ".join(args[1:]).replace("\\n", "\n")
        agent.config.persona = text
        self._record("persona", f"性格被 {sender.name} 改写（{len(text)} 字）",
                     who=args[0])
        sender.send_message(
            f"{C.GREEN}已更新 {args[0]} 的性格（下轮决策生效）{C.RESET}")

    def _cmd_money(self, sender: CommandSender, args: list[str]) -> None:
        """查看/设定假人钱包。钱包走 arc_core 的经济表，玩家也能查到。"""
        if not args:
            sender.send_message(f"{C.RED}用法: /ai money <名字> [数额]{C.RESET}")
            return
        name = _clean_name(args[0])
        if not self._economy.available():
            sender.send_message(
                f"{C.RED}本服没有可用的 arc_core 经济系统{C.RESET}")
            return
        if len(args) >= 2:
            try:
                amount = round(float(args[1]), 2)
            except ValueError:
                sender.send_message(f"{C.RED}数额要是数字{C.RESET}")
                return
            if amount < 0:
                sender.send_message(f"{C.RED}数额不能为负{C.RESET}")
                return
            if self._economy.ensure_wallet(name, 0) and \
                    self._economy.set_balance(name, amount):
                self._record("money", f"钱包被 {sender.name} 设为 {amount:g}",
                             who=name)
                sender.send_message(f"{C.GREEN}{name} 的钱包已设为 {amount:g}{C.RESET}")
            else:
                sender.send_message(f"{C.RED}写入失败，见服务端日志{C.RESET}")
            return
        balance = self._economy.get_balance(name)
        if balance is None:
            sender.send_message(f"{C.YELLOW}{name} 还没有钱包"
                                f"{C.RESET}{C.GRAY}（/ai money {name} <数额> 可创建）{C.RESET}")
        else:
            sender.send_message(f"{C.AQUA}{name}{C.RESET} 的钱包余额: "
                                f"{C.GOLD}{balance:.2f}{C.RESET}")

    def _cmd_cmd(self, sender: CommandSender, args: list[str]) -> None:
        """以某个假人的身份执行一条服务器命令（管理员用）。

        两个用途：调试假人视角的命令链路（比如 gs quick 能不能进房），
        以及"替它报名"类操作。和 LLM 的 run_command 工具走同一条
        dispatch_command(player) 通道——这里通了，工具那条就通。
        """
        if len(args) < 2:
            sender.send_message(
                f"{C.RED}用法: /ai cmd <名字> <命令（不带斜杠）>{C.RESET}")
            return
        name = _clean_name(args[0])
        actor = self._live_actor(name)
        if actor is None:
            sender.send_message(f"{C.RED}未找到（不在线）{C.RESET}")
            return
        command = " ".join(args[1:]).strip().lstrip("/")
        error = self._run_bot_command(name, actor, command)
        self._record("admin", f"{sender.name} 以 {name} 身份执行: {command[:80]}")
        if error:
            sender.send_message(f"{C.YELLOW}{name}: {error}{C.RESET}")
        else:
            sender.send_message(f"{C.GREEN}已以 {name} 身份执行: {command}{C.RESET}")

    def _cmd_session(self, sender: CommandSender, args: list[str]) -> None:
        """调整自动在线角色的剩余游玩时间（0 = 立即安排下线）。"""
        if len(args) < 2:
            sender.send_message(
                f"{C.RED}用法: /ai session <名字> <分钟（0=马上下线）>{C.RESET}")
            return
        try:
            minutes = float(args[1])
        except ValueError:
            sender.send_message(f"{C.RED}分钟要是数字{C.RESET}")
            return
        ok, msg = self._crowd.set_session_minutes(args[0], minutes)
        color = C.GREEN if ok else C.RED
        sender.send_message(f"{color}{msg}{C.RESET}")

    def _cmd_auto(self, sender: CommandSender, args: list[str]) -> None:
        """自动稳定在线的开关与状态。"""
        sub = args[0].lower() if args else "status"
        if sub in ("on", "off"):
            if not isinstance(self._cfg.get("auto_online"), dict):
                self._cfg["auto_online"] = {}
            self._cfg["auto_online"]["enabled"] = (sub == "on")
            self._save_config()
            sender.send_message(
                f"{C.GREEN}自动稳定在线已{'开启' if sub == 'on' else '关闭'}{C.RESET}"
                f"{C.GRAY}（角色卡在 config.json，改完 /ai reload）{C.RESET}")
            return
        if sub != "status":
            sender.send_message(f"{C.RED}用法: /ai auto [on|off|status]{C.RESET}")
            return
        sender.send_message(f"{C.GOLD}=== 自动稳定在线 ==={C.RESET}")
        for line in self._crowd.describe():
            sender.send_message(f"{C.GRAY}{line}{C.RESET}")
        sender.send_message(
            f"{C.GRAY}经济: {'arc_core 已接入' if self._economy.available() else '未接入（无钱包功能）'}{C.RESET}")

    def _cmd_reload(self, sender: CommandSender, args: list[str]) -> None:
        """重读 config.json（角色卡/自动在线参数/提示词段落），不重启。"""
        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("config.json 不是 JSON 对象")
        except Exception as exc:
            sender.send_message(f"{C.RED}config.json 读取失败: {exc}{C.RESET}")
            return
        self._cfg.update(raw)
        # 提示词段落推给在线的假人（改文件最常见的目的就是调提示词/角色）。
        # 角色卡假人（隐身群演）的**性格来自它的卡**，不随全局段刷新——
        # 否则一 reload，四张卡的性格全变成同一个默认人。
        for agent in self._agents.values():
            if agent.name not in self._stealth:
                agent.config.persona = str(self._cfg["prompt_persona"])
            agent.config.rules = (str(self._cfg["prompt_rules"]) + "\n\n"
                                  + str(self._cfg.get("prompt_command_policy", ""))
                                  + "\n\n" + str(self._cfg.get("prompt_social", "")))
        for compiler in self._compilers.values():
            compiler.surface_only = bool(self._cfg["dig_surface_only"])
        self._world.surface_only = bool(self._cfg["surface_only"])
        cards = len(self.crowd_cards())
        sender.send_message(
            f"{C.GREEN}已重读配置（角色卡 {cards} 张，调度器下个心跳生效）{C.RESET}")

    def _cmd_stop(self, sender: CommandSender, args: list[str]) -> None:
        if not args or args[0] not in self._agents:
            sender.send_message(f"{C.RED}未找到{C.RESET}")
            return
        self._agents[args[0]].stop("被管理员停止")
        ctl = self._bodies.get(args[0])
        if ctl is not None:
            ctl.cancel()
        self._record("stop", f"被{sender.name}停止", who=args[0])
        sender.send_message(f"{C.GREEN}已停止 {args[0]}{C.RESET}")

    def _cmd_remove(self, sender: CommandSender, args: list[str]) -> None:
        if not args:
            return
        # 走和 spawn 同一个净化函数。名字在存进 `_agents` 之前就已经
        # 净化过，这里再净化一次是为了让"用原始输入查表"也能命中——
        # 不然 `/ai spawn hly"x` 建出来的键是 `hly_x`，
        # 而 `/ai remove hly"x` 查的还是 `hly"x`，永远找不到。
        name = _clean_name(args[0])
        agent = self._agents.pop(name, None)
        actor = self._actors.pop(name, None)
        if agent is None:
            sender.send_message(f"{C.RED}未找到{C.RESET}")
            return
        agent.stop("已移除")
        # 管理员手动移除：同步清调度器的会话（不清理的话调度器会把
        # "身体消失"当事故处理，立刻安排补位）。
        self._crowd.forget(name)
        self._stealth.discard(name)
        self._pending_gm.pop(name, None)
        self._executor.cancel_all(name)
        # 常加载区域要跟着一起摘掉。放在最前面：即使后面 remove 实体失败，
        # 也不该留一块永远不想加载的区域在服务器上。
        self._release_ticking_area(name)

        # 先让身体停下，再移除
        ctl = self._bodies.get(name)
        if ctl is not None:
            ctl.cancel()
        self._bodies.remove(name)
        self._real_actions.pop(name, None)
        self._compilers.pop(name, None)

        if self._use_real_body:
            self._bridge.remove(name)
        if actor is not None:
            try:
                actor.remove()
            except Exception:
                pass
        self._record("remove", f"被{sender.name}移除", who=name)
        sender.send_message(f"{C.GREEN}已移除 {name}{C.RESET}")

    def _cmd_goal(self, sender: CommandSender, args: list[str]) -> None:
        if len(args) < 2:
            sender.send_message(f"{C.RED}用法: /ai goal <名字> <目标>{C.RESET}")
            return
        agent = self._agents.get(args[0])
        if agent is None:
            sender.send_message(f"{C.RED}未找到{C.RESET}")
            return
        goal = " ".join(args[1:])
        agent.config.goal = goal
        agent.state.done_reason = ""
        agent.state.last_decision = 0.0
        self._record("goal", f"被{sender.name}改为: {goal[:100]}", who=args[0])
        sender.send_message(f"{C.GREEN}已更新目标{C.RESET}")

    def _cmd_chat(self, sender: CommandSender, args: list[str]) -> None:
        if len(args) < 2:
            sender.send_message(f"{C.RED}用法: /ai chat <名字> <内容>{C.RESET}")
            return
        agent = self._agents.get(args[0])
        if agent is None:
            sender.send_message(f"{C.RED}未找到{C.RESET}")
            return
        text = " ".join(args[1:])
        agent.hear(sender.name, text)
        agent.state.last_decision = 0.0
        self._record("chat", f"收到 {sender.name} 的话: {text[:100]}", who=args[0])
        sender.send_message(f"{C.GREEN}已转达{C.RESET}")

    def _cmd_wear(self, sender: CommandSender, args: list[str]) -> None:
        if not args:
            sender.send_message(f"{C.RED}用法: /ai wear <名字>{C.RESET}")
            return
        self._tell_skin(args[0], sender)

    def _tell_skin(self, name: str, sender: CommandSender) -> None:
        actor = self._live_actor(name)
        if actor is None:
            sender.send_message(f"{C.RED}未找到{C.RESET}")
            return
        if self._use_real_body:
            sender.send_message(
                f"{C.GREEN}{name} 是真实模拟玩家，皮肤取决于它加入时用的账号。{C.RESET}")
            return
        try:
            self.server.dispatch_command(
                self.server.command_sender,
                f"replaceitem entity @e[type=minecraft:npc,c=1] slot.armor.head 0 "
                f"minecraft:leather_helmet 1 0 {{\"minecraft:custom_color\":{hash(name) % 16777215}}}",
            )
            sender.send_message(f"{C.GREEN}已给 {name} 换了一顶随机颜色的头盔{C.RESET}")
        except Exception as exc:
            sender.send_message(f"{C.RED}失败: {exc}{C.RESET}")

    def _cmd_where(self, sender: CommandSender, args: list[str]) -> None:
        if not args:
            return
        actor = self._live_actor(args[0])
        if actor is None:
            sender.send_message(f"{C.RED}未找到{C.RESET}")
            return
        # 上面刚判过有效性，但实体也可能"判完之后、读之前"死掉。
        # 这个命令是管理员直接敲的，异常会一路冒到命令分发器，
        # 所以必须自己兜住。
        try:
            loc = actor.location
        except Exception as exc:
            sender.send_message(f"{C.RED}读取位置失败（身体可能已消失）: {exc}{C.RESET}")
            return
        extra = ""
        agent = self._agents.get(args[0])
        if agent is not None:
            marks = agent.state.landmarks
            if marks:
                extra = "  地标: " + ", ".join(f"{k}{v}" for k, v in list(marks.items())[:3])
        sender.send_message(
            f"{C.AQUA}{args[0]}{C.RESET} @ ({loc.x:.1f}, {loc.y:.1f}, {loc.z:.1f}){extra}")

    def _cmd_tp(self, sender: CommandSender, args: list[str]) -> None:
        if not args:
            return
        actor = self._live_actor(args[0])
        if actor is None:
            sender.send_message(f"{C.RED}未找到{C.RESET}")
            return
        try:
            sender.teleport(actor.location)
            sender.send_message(f"{C.GREEN}已传送{C.RESET}")
        except Exception as exc:
            sender.send_message(f"{C.RED}失败: {exc}{C.RESET}")

    # ------------------------------------------------------------------
    # 输出辅助
    # ------------------------------------------------------------------

    def _dispatch(self, command: str) -> bool:
        """执行一条命令。桥接层用它发 scriptevent。"""
        try:
            return bool(self.server.dispatch_command(self.server.command_sender, command))
        except Exception as exc:
            self.logger.debug(f"命令执行失败 /{command}: {exc}")
            return False

    def _on_main(self, fn) -> None:
        """把一个闭包排到主线程执行。

        ⚠️ 这里必须自己包一层异常兜底，**不能裸交给调度器**。

        背景：调用方（`agent._call_llm`）那边虽然也写了 `try`，但它包的是
        "把任务排进队列"这个动作——那个动作不会抛。真正的执行发生在
        若干 tick 之后，那时调用方的 `try` 早就不在调用栈上了。所以
        闭包里漏出来的异常会直接冒进 C++ 调度器，**把服务器带崩**。

        这和 `_guard` 是同一个道理，只是 `_guard` 管的是周期任务，
        这里是"一次性回调"。既然异常一定会在主线程上炸，防线就得
        建在这一侧。
        """
        def guarded() -> None:
            try:
                fn()
            except Exception:
                try:
                    tb = traceback.format_exc()
                    self.logger.error(
                        f"{C.RED}✗ 主线程回调异常（已兜住，服务器不受影响）{C.RESET}")
                    for line in tb.rstrip().split("\n"):
                        self.logger.error(f"{C.RED}  {line}{C.RESET}")
                except Exception:
                    pass

        self.server.scheduler.run_task(self, guarded, delay=0)

    def _broadcast(self, name: str, message: str) -> None:
        self.server.broadcast_message(f"{C.AQUA}<{name}>{C.RESET} {message}")
        # 真人聊天会触发 PlayerChatEvent、被 QQ 同步插件转进群；假人的
        # broadcast 不触发任何事件——群里"看不到这个人说话"就穿帮了。
        # 这里直调同步插件的 api_send_event 补一条，格式与真人完全一致。
        self._qq_sync("chat", name, message)

    def _record(self, kind: str, message: str, *, who: str = "") -> None:
        """往活动日志里记一条。记录失败不影响任何逻辑。"""
        try:
            self._activity.write(kind, message, who=who)
        except Exception:
            pass

    def _agent_log(self, message: str) -> None:
        """Agent 的日志出口：控制台 + 活动日志。

        message 已经带了 "[名字] " 前缀（agent._log 加的），所以这里
        要把它拆出来单独当 who，否则日志里会出现 "[挖矿] [Steve] 挖了…"
        这种两层括号。

        ⚠️ **绝不往公屏广播。** 这些是 AI 的内心活动（💭 思考、⚠ 工具
        失败、🎮 动作摘要）——群演假人要把玩家身份演到底，把这些打到
        公屏等于自己拆穿自己。真要说的话走 `_broadcast`（say 工具），
        那才是"聊天"；这里只是后台流水。服主看控制台 / activity.log。
        """
        self.logger.info(message)
        who, text = message, ""
        if message.startswith("[") and "] " in message:
            who, text = message[1:].split("] ", 1)
        self._record(_kind_of(text), text, who=who)
