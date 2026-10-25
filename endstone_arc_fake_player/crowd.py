"""自动在线调度器：角色卡 + 稳定在线人数 + 定时换人。

需求（服主原话拆解）
--------------------
· 预先写几个**角色卡**（名字/性格/目标/初始金钱）。
· 设定**稳定在线 AI 数**（如 3）：调度器永远维持这个数量的 AI 在线。
· 每个 AI 上线时随机一个**游玩时长**（默认 10~300 分钟），到点下线。
· 下线后隔一个随机**再上线间隔**（默认 1~5 分钟），换下一张卡上——
  让服务器看起来一直有不同的人在玩。
· AI 助手的常驻假人**不参与轮换**（由外部插件持有，见 plugin.api_*）。

为什么单独一个模块
------------------
调度逻辑（到点、补位、挑卡、避开名字冲突）是纯状态机，不碰任何
游戏 API——所有游戏侧操作都通过 plugin 上的钩子回调完成。这样
单元测试可以用假 plugin 直接跑时间线，不需要服务器。

时间与随机都可注入（``clock`` / ``rng``），测试里能确定性地快进。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# 角色卡
# ---------------------------------------------------------------------------

@dataclass
class CharacterCard:
    """一张角色卡：一个预设好性格与钱包的假人。"""

    name: str
    persona: str = ""
    goal: str = ""
    #: 初始金钱。None = 不建钱包；数值 = 上线时确保钱包至少有这么多
    #: （已有钱包不动，避免每次轮换都重置成富人）。
    money: float | None = None
    #: 合成身份（可选）。不填则首次上线时自动生成并终身固定；
    #: 填了就钉死——同一张卡跨服务器/重装都是同一个"人"。
    xuid: str | None = None
    uuid: str | None = None
    #: 公屏闲聊（没点名）时的搭话概率 0~1。None = 用全局默认。
    chat_reply_chance: float | None = None
    #: 说话风格示例（大模型模仿这几句的语气/口头禅/情绪）。
    style: list[str] | None = None

    @staticmethod
    def from_dict(raw: Any) -> "CharacterCard | None":
        if not isinstance(raw, dict):
            return None
        name = str(raw.get("name", "")).strip()
        if not name:
            return None
        money = raw.get("money", None)
        if money is not None:
            try:
                money = float(money)
            except (TypeError, ValueError):
                money = None
        chance = raw.get("chat_reply_chance")
        if chance is not None:
            try:
                chance = max(0.0, min(1.0, float(chance)))
            except (TypeError, ValueError):
                chance = None
        style_raw = raw.get("style")
        style = None
        if isinstance(style_raw, list):
            style = [str(x).strip() for x in style_raw if str(x).strip()]
        return CharacterCard(
            name=name,
            persona=str(raw.get("persona", "") or ""),
            goal=str(raw.get("goal", "") or ""),
            money=money,
            xuid=(str(raw["xuid"]).strip() or None) if raw.get("xuid") else None,
            uuid=(str(raw["uuid"]).strip() or None) if raw.get("uuid") else None,
            chat_reply_chance=chance,
            style=style,
        )


def load_cards(raw_list: Any) -> list[CharacterCard]:
    """从 config 的 characters 数组读卡。坏条目跳过（由调用方告警）。"""
    if not isinstance(raw_list, list):
        return []
    cards: list[CharacterCard] = []
    for raw in raw_list:
        card = CharacterCard.from_dict(raw)
        if card is not None:
            cards.append(card)
    return cards


#: 内置示例角色卡。config 里没写 characters 时用这四张，
#: 服主可以照着格式在 config.json 里换成自己的。
DEFAULT_CARDS: list[dict[str, Any]] = [
    {
        "name": "Aqing",
        "persona": "你性格安静温和，说话简短有礼，喜欢钓鱼和收集花草，"
                   "遇到玩家搭话会友好回应但不会啰嗦。",
        "goal": "在湖边安家，钓钓鱼、种种小麦，和路过的玩家聊聊天。",
        "money": 120,
    },
    {
        "name": "TieDan",
        "persona": "你是个话痨冒险家，热情外向，喜欢吹嘘自己的战斗经历，"
                   "爱凑热闹，看到玩家就想去搭话。",
        "goal": "四处探险打怪，收集战利品，找玩家炫耀并交换物资；"
                "闲下来就去枪战房打几局（run_command \"gs quick\"）。",
        "money": 260,
    },
    {
        "name": "MuTou",
        "persona": "你是个务实的工匠，话不多但热心，喜欢挖矿、建造和修路，"
                   "玩家的合理请求你都会帮忙。",
        "goal": "挖矿攒材料，给自己盖一间石头房子，顺便修一条小路。",
        "money": 80,
    },
    {
        "name": "XiaoHe",
        "persona": "你是个精明的商人，对物价敏感，喜欢和玩家讨价还价，"
                   "总是想着低买高卖，但讲信用，说好的交易不会赖账。",
        "goal": "收集多余物资摆摊，和玩家做买卖，目标是攒够 1000 金币。",
        "money": 500,
    },
]


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """一个在线中的角色及其下线倒计时。"""

    card: CharacterCard
    #: monotonic 时间戳：到点就 retire。
    deadline: float
    #: 上线时刻（monotonic），算"已游玩多久"用。
    started_at: float


@dataclass
class Pending:
    """一个等待上线的空位：到 due 时刻挑一张卡补上。"""

    due: float


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------

class CrowdScheduler:
    """维持目标数量的角色假人在线，按计划换人。

    钩子（plugin 上同名方法，测试时用假对象替换）：
      crowd_llm_ready()        LLM 是否已配置（没配置就不放人上线）
      crowd_spawn(card)        让某张卡上线，返回 (ok, 说明)
      crowd_retire(name, why)  让某个在线角色下线
      crowd_bot_alive(name)    这个角色现在还在线吗
      crowd_reserved_names()   非轮换渠道占用的名字（手动 spawn、助手身体等）
      crowd_real_player_names() 在线真人名字（小写）
      crowd_log(level, msg)    日志
    """

    #: 每 30 秒（600 tick）跑一次心跳。换人粒度到分钟级，30 秒绰绰有余。
    TICK_PERIOD = 600

    def __init__(self, plugin: Any,
                 clock: Callable[[], float] = time.monotonic,
                 rng: random.Random | None = None) -> None:
        self._plugin = plugin
        self._now = clock
        self._rng = rng or random.Random()
        #: 在线会话：名字 → Session
        self.sessions: dict[str, Session] = {}
        #: 等待上线的空位
        self.pending: list[Pending] = []
        #: 卡片轮换游标。按顺序发牌、发完一轮再从头，配合"跳过在线的"，
        #: 保证同一张卡不会连续霸场。
        self._cursor = 0
        #: 是否已经完成过启动期补位（启动期补位不等 gap，直接 stagger 上）
        self._bootstrapped = False
        #: LLM 未就绪只告警一次的状态位
        self._llm_wait_warned = False

    # ------------------------------------------------------------------
    # 配置读取（每次现读，config 热更新立即生效）
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        cfg = self._plugin.crowd_config()
        return bool(cfg.get("enabled", False))

    def _cfg_int(self, key: str, default: int) -> int:
        cfg = self._plugin.crowd_config()
        try:
            return int(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    @property
    def target_count(self) -> int:
        return max(0, self._cfg_int("target_count", 3))

    def _random_minutes(self, low_key: str, high_key: str,
                        default_low: int, default_high: int) -> float:
        low = self._cfg_int(low_key, default_low)
        high = self._cfg_int(high_key, default_high)
        low = max(1, low)
        high = max(low, high)
        return self._rng.uniform(low, high)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """跑一步状态机。由插件的周期任务每 30 秒调一次。"""
        if not self.enabled:
            self._drain(why="自动在线已关闭")
            return

        now = self._now()

        # 1) LLM 没配置：不放人上线，也把现有会话保留（已在线的继续玩，
        #    只是决策重试）。只告警一次，避免每 30 秒刷屏。
        if not self._plugin.crowd_llm_ready():
            if not self._llm_wait_warned:
                self._llm_wait_warned = True
                self._plugin.crowd_log(
                    "warning", "自动在线：LLM 未配置（/ai set），暂不上线新角色")
            return
        self._llm_wait_warned = False

        # 2) 到点下线 → 安排一个空位（间隔后再补）
        for name in list(self.sessions):
            session = self.sessions[name]
            if session.deadline <= now:
                self._retire(name, "游玩时间到，下线休息")
                self.pending.append(Pending(due=self._rejoin_due(now)))

        # 3) 身体没了（被打掉没重生/行为包断连被收摊）→ 会话清掉并补位。
        #    这是"看起来在线其实早死了"的兜底。
        for name in list(self.sessions):
            if name not in self.sessions:
                continue
            if not self._plugin.crowd_bot_alive(name):
                self._retire(name, "身体消失")
                self.pending.append(Pending(due=self._rejoin_due(now)))

        # 4) 补位：在线数 + 待上线数 < 目标 → 新增空位。
        #    启动期（从未有会话）直接到点就上，之后的新空位走正常间隔。
        want = self.target_count
        shortage = want - (len(self.sessions) + len(self.pending))
        for _ in range(max(0, shortage)):
            due = now if not self._bootstrapped else self._rejoin_due(now)
            self.pending.append(Pending(due=due))
        if self.sessions or self.pending:
            self._bootstrapped = True

        # 5) 到点的空位：挑卡上线。
        for slot in list(self.pending):
            if slot.due > now:
                continue
            self.pending.remove(slot)
            self._fill_slot(now)

    def _rejoin_due(self, now: float) -> float:
        gap = self._random_minutes(
            "rejoin_gap_min_minutes", "rejoin_gap_max_minutes", 1, 5)
        return now + gap * 60.0

    def _retire(self, name: str, why: str) -> None:
        self.sessions.pop(name, None)
        try:
            self._plugin.crowd_retire(name, why)
        except Exception as exc:
            self._plugin.crowd_log("warning", f"下线 {name} 失败: {exc}")

    def _fill_slot(self, now: float) -> None:
        card = self._pick_card()
        if card is None:
            # 所有卡都被占用（卡数 < 目标数）。退回一个稍后重试的空位，
            # 否则这个空位永远消失，人数再也补不回来。
            self._plugin.crowd_log(
                "warning", "自动在线：没有可用的角色卡（都在线或名字冲突），"
                           "30 秒后重试")
            self.pending.append(Pending(due=now + 30.0))
            return
        play = self._random_minutes(
            "play_minutes_min", "play_minutes_max", 10, 300)
        try:
            ok, why = self._plugin.crowd_spawn(card)
        except Exception as exc:
            ok, why = False, str(exc)
        if not ok:
            self._plugin.crowd_log("warning", f"角色 {card.name} 上线失败: {why}")
            # 失败退避：1 分钟后再试这个空位。不退避的话，行为包断连
            # 期间这里会每 30 秒撞一次墙。
            self.pending.append(Pending(due=now + 60.0))
            return
        self.sessions[card.name] = Session(
            card=card, deadline=now + play * 60.0, started_at=now)
        self._plugin.crowd_log(
            "info", f"角色 {card.name} 已上线，计划游玩 {play:.0f} 分钟")

    def _pick_card(self) -> CharacterCard | None:
        """挑下一张卡：按序发牌，跳过不可用的名字。"""
        cards = self._plugin.crowd_cards()
        if not cards:
            return None
        taken = self._taken_names()
        for _ in range(len(cards)):
            card = cards[self._cursor % len(cards)]
            self._cursor += 1
            if card.name.lower() not in taken:
                return card
        return None

    def _taken_names(self) -> set[str]:
        taken = {n.lower() for n in self.sessions}
        try:
            taken |= {n.lower() for n in self._plugin.crowd_reserved_names()}
        except Exception:
            pass
        try:
            taken |= {n.lower() for n in self._plugin.crowd_real_player_names()}
        except Exception:
            pass
        return taken

    # ------------------------------------------------------------------
    # 外部干预
    # ------------------------------------------------------------------

    def set_session_minutes(self, name: str, minutes: float) -> tuple[bool, str]:
        """手动调整某个在线角色的剩余游玩时间。"""
        session = self.sessions.get(name)
        if session is None:
            return False, f"{name} 不是自动在线的角色"
        if minutes <= 0:
            # 0/负数 = 立刻安排下线
            session.deadline = self._now()
            return True, f"{name} 将在下一个心跳下线"
        session.deadline = self._now() + minutes * 60.0
        return True, f"{name} 的游玩时间已延长 {minutes:.0f} 分钟"

    def forget(self, name: str) -> None:
        """管理员手动移除了这个角色：清会话，不安排立即补位
        （正常的缺口补位逻辑会在下个心跳按 gap 补一张新卡）。"""
        self.sessions.pop(name, None)

    def _drain(self, why: str) -> None:
        """关闭自动在线时清空所有计划（不打扰已在线的，由服主自行处理）。"""
        if self.pending:
            self.pending.clear()
            self._plugin.crowd_log("info", f"自动在线：已取消 {len(self.sessions)} 个"
                                           f"待上线名额（{why}）")

    # ------------------------------------------------------------------
    # 状态报告
    # ------------------------------------------------------------------

    def describe(self) -> list[str]:
        now = self._now()
        lines = [
            f"自动在线: {'开' if self.enabled else '关'} · "
            f"目标 {self.target_count} 人 · "
            f"在线 {len(self.sessions)} · 待上线 {len(self.pending)} · "
            f"角色卡 {len(self._plugin.crowd_cards())} 张"]
        for name, s in self.sessions.items():
            left = max(0.0, (s.deadline - now) / 60.0)
            played = max(0.0, (now - s.started_at) / 60.0)
            lines.append(f"  {name}: 已玩 {played:.0f} 分钟，剩 {left:.0f} 分钟")
        for slot in self.pending:
            wait = max(0.0, (slot.due - now) / 60.0)
            lines.append(f"  (空位: {wait:.0f} 分钟后补人)")
        return lines
