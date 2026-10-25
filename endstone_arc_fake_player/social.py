"""好感度、恩怨与反击条件反射。

为什么反击不走 LLM
------------------
"被打了要还手"是**秒级**反应——等大模型一轮决策（数秒到十几秒），
假人早就被打死了。所以分两层：

  · **条件反射层**（本模块的 CombatReflex）：被打瞬间由插件接管身体，
    够得着就打、够不着就追，直到目标消失/超时/自己残血。
  · **决策层**（Agent/LLM）：反射同时给大脑塞事件、唤醒思考，
    由它决定喊话、求饶、撤退后复仇还是乘胜追击。反射结束时身体
    交还给执行器，大脑下一轮自然接管。

好感度（FavorStore）
--------------------
每个假人对每个**真人玩家**一份 -100~100 的好感度，持久化到
``data_folder/favorability.json``——角色卡轮换、服务器重启都不忘。
仇人（≤ grudge_threshold）触发寻仇：上线相遇主动追杀；
亲手击败一次恩怨两清（好感回到警惕档，不再无端追杀）。

两个模块都是纯逻辑 + 注入回调（时钟/发包/查坐标），可离线单测。
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

#: 好感度硬边界
FAVOR_MIN, FAVOR_MAX = -100.0, 100.0


def clamp_score(value: float) -> float:
    return max(FAVOR_MIN, min(FAVOR_MAX, round(float(value), 1)))


def level_name(score: float, *, grudge: float, friend: float) -> str:
    """数值 → 关系档位名。档位边界由配置传入（默认 -30 / 60）。"""
    if score <= grudge:
        return "仇人"
    if score < -10:
        return "警惕"
    if score < friend:
        return "中立"
    if score < 80:
        return "友好"
    return "挚友"


# ---------------------------------------------------------------------------
# 好感度仓库
# ---------------------------------------------------------------------------

class FavorStore:
    """(假人, 玩家) → 好感度。落地为 JSON，写入做脏标记由调用方节流。

    结构::

        {"<bot>": {"<player>": {"score": -45.0, "updated": 1730000000.0}}}
    """

    #: 同一对（bot,player）两次"聊天加分"的最小间隔（秒）。
    #: 没有冷却的话，一个玩家刷屏 @假人 就能把仇人聊成挚友。
    CHAT_COOLDOWN = 300.0

    def __init__(self, path: Any, logger: Any = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._logger = logger
        self._clock = clock
        self._data: dict[str, dict[str, dict[str, float]]] = {}
        self.dirty = False
        self._last_chat: dict[tuple[str, str], float] = {}
        self.load()

    # -- 持久化 ----------------------------------------------------------

    def load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self._data = raw
        except FileNotFoundError:
            self._data = {}
        except Exception as exc:
            if self._logger:
                try:
                    self._logger.warning(f"好感度文件读取失败，从空开始: {exc}")
                except Exception:
                    pass
            self._data = {}

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self._path)
            self.dirty = False
        except Exception as exc:
            if self._logger:
                try:
                    self._logger.warning(f"好感度文件保存失败: {exc}")
                except Exception:
                    pass

    # -- 读写 ------------------------------------------------------------

    def score(self, bot: str, player: str) -> float:
        return float(self._data.get(bot, {}).get(player, {}).get("score", 0.0))

    def adjust(self, bot: str, player: str, delta: float,
               *, reason: str = "") -> float:
        """改好感度并返回新值。自动夹到 [-100, 100]。"""
        entry = self._data.setdefault(bot, {}).setdefault(
            player, {"score": 0.0, "updated": 0.0})
        entry["score"] = clamp_score(float(entry.get("score", 0.0)) + delta)
        entry["updated"] = self._clock()
        self.dirty = True
        return entry["score"]

    def chat_bonus(self, bot: str, player: str, amount: float = 1.0) -> float | None:
        """聊天加分（带冷却）。冷却中返回 None，不加也不减。"""
        key = (bot, player)
        now = self._clock()
        if now - self._last_chat.get(key, 0.0) < self.CHAT_COOLDOWN:
            return None
        self._last_chat[key] = now
        return self.adjust(bot, player, amount, reason="聊天")

    def grudges(self, bot: str, threshold: float) -> list[str]:
        """好感度 ≤ threshold 的玩家（仇人名单）。"""
        out = []
        for player, entry in self._data.get(bot, {}).items():
            if float(entry.get("score", 0.0)) <= threshold:
                out.append(player)
        return out

    def settle_vendetta(self, bot: str, player: str) -> float:
        """寻仇得手：恩怨两清。好感回到警惕档（-10），不再是仇人。"""
        entry = self._data.setdefault(bot, {}).setdefault(
            player, {"score": 0.0, "updated": 0.0})
        entry["score"] = -10.0
        entry["updated"] = self._clock()
        self.dirty = True
        return -10.0

    def prune_mob_entries(self) -> int:
        """清掉误记到**怪物**头上的好感度条目（键是纯数字的 runtime id）。

        ≤3.8.8 的 bug：假人被怪打死时死亡结算不看反击对象是人还是怪，
        一律 -40——僵尸的 runtime id 就这么成了"仇人"。加载后清一遍，
        返回清掉的条数。
        """
        removed = 0
        for bot in list(self._data):
            tbl = self._data.get(bot) or {}
            for key in [k for k in tbl if str(k).isdigit()]:
                tbl.pop(key, None)
                removed += 1
        if removed:
            self.dirty = True
        return removed

    def forget(self, bot: str, player: str) -> None:
        self._data.get(bot, {}).pop(player, None)
        self.dirty = True

    def describe(self, bot: str, players: list[str], *,
                 grudge: float, friend: float, limit: int = 6) -> str:
        """给提示词用的一行好感度摘要。只列在线的，仇人优先。"""
        if not players:
            return ""
        scored = [(p, self.score(bot, p)) for p in players]
        scored.sort(key=lambda ps: ps[1])          # 仇人（最低分）排前面
        parts = [f"{p}({level_name(s, grudge=grudge, friend=friend)}{s:+.0f})"
                 for p, s in scored[:limit]]
        return "好感度： " + "，".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# 反击条件反射
# ---------------------------------------------------------------------------

@dataclass
class Engagement:
    """一次战斗接管的状态。"""

    target: str
    reason: str = ""
    #: 播报用的人话名字（怪物目标的 target 是 runtime id）。
    label: str = ""
    #: 目标是不是真人玩家。怪物的 runtime id 不能进好感度/仇人表
    #: （≤3.8.8 的 bug：被怪打死也记 -40，一串数字成了"仇人"）。
    is_player: bool = True
    engaged_at: float = 0.0
    give_up_at: float = 0.0
    # 初始为"-无穷"：接管的**第一拍**必须立刻出手/追击，
    # 不能被节流间隔挡住（那 0.8 秒在实战里就是白挨一下打）。
    last_attack_at: float = -1e18
    last_walk_at: float = -1e18


class CombatReflex:
    """被打后的自动反击：够得着就打，够不着就追。

    钩子（全部注入，测试可替换）：
      attack(bot, target) -> bool          出一次手
      walk(bot, x, y, z) -> bool           追击走位
      bot_pos(bot) -> (x,y,z) | None       假人当前坐标
      target_pos(target) -> (x,y,z)|None   目标当前坐标（None=不在线/死了）
      bot_health(bot) -> float | None      假人血量（读不到按满血处理）
      on_event(bot, text)                  给大脑的战斗播报（开打/放弃/撤退）
    """

    #: 出手距离（比 doAttack 的 4.5 略保守，减少"够不着"回执）
    ATTACK_REACH = 4.2
    #: 出手间隔（秒）——Bedrock 攻击冷却约 0.6s，留一点余量
    ATTACK_INTERVAL = 0.8
    #: 追击走位的重发间隔（目标在跑，路径要跟着刷新）
    WALK_INTERVAL = 2.0
    #: 血量低于此值放弃缠斗（交给大脑决定逃命还是求饶）
    LOW_HEALTH = 6.0

    def __init__(self, *, attack: Callable, walk: Callable, bot_pos: Callable,
                 target_pos: Callable, bot_health: Callable | None = None,
                 on_event: Callable | None = None, logger: Any = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._attack = attack
        self._walk = walk
        self._bot_pos = bot_pos
        self._target_pos = target_pos
        self._bot_health = bot_health or (lambda _b: None)
        self._on_event = on_event or (lambda _b, _t: None)
        self._logger = logger
        self._clock = clock
        self._engaged: dict[str, Engagement] = {}

    # -- 状态 ------------------------------------------------------------

    def is_engaged(self, bot: str) -> bool:
        return bot in self._engaged

    def target_of(self, bot: str) -> str | None:
        e = self._engaged.get(bot)
        return e.target if e else None

    def engagement_of(self, bot: str) -> Engagement | None:
        return self._engaged.get(bot)

    def engaged_bots_of(self, target: str) -> list[str]:
        """正在围殴某个目标的所有假人（寻仇得手判定用）。"""
        return [b for b, e in self._engaged.items() if e.target == target]

    # -- 接管 / 放手 ------------------------------------------------------

    def engage(self, bot: str, target: str, *, reason: str = "",
               duration: float = 90.0, label: str = "",
               is_player: bool = True) -> None:
        now = self._clock()
        old = self._engaged.get(bot)
        if old is not None and old.target == target:
            # 已经在打同一个目标：只延长时限（被连续攻击时不断续期）
            old.give_up_at = max(old.give_up_at, now + duration)
            return
        self._engaged[bot] = Engagement(
            target=target, reason=reason, engaged_at=now,
            give_up_at=now + duration, label=label, is_player=is_player)
        # 播报给大脑用 label（怪物目标的 target 是 runtime id，念出来
        # 是一串数字，没法读）；日志和内部状态仍然用精确的 target。
        shown = label or target
        self._on_event(bot, f"你受到攻击，已自动反击{shown}"
                        + (f"（{reason}）" if reason else ""))
        if self._logger:
            try:
                self._logger.info(f"[{bot}] 反击条件反射激活 → {shown}")
            except Exception:
                pass

    def disengage(self, bot: str, why: str = "") -> str | None:
        e = self._engaged.pop(bot, None)
        if e is None:
            return None
        self._on_event(bot, f"与 {e.label or e.target} 的战斗结束（{why or '结束'}）")
        return e.target

    # -- 主循环 ----------------------------------------------------------

    def tick(self) -> None:
        now = self._clock()
        for bot in list(self._engaged):
            e = self._engaged.get(bot)
            if e is None:
                continue
            tpos = self._target_pos(e.target)
            if tpos is None:
                self.disengage(bot, "目标消失")
                continue
            bpos = self._bot_pos(bot)
            if bpos is None:
                self.disengage(bot, "自己不在线")
                continue
            if now >= e.give_up_at:
                self.disengage(bot, "追击超时")
                continue
            hp = self._bot_health(bot)
            if hp is not None and hp <= self.LOW_HEALTH:
                self.disengage(bot, "血量过低")
                self._on_event(bot, f"你快被打死了，已脱离与 {e.target} 的缠斗——快逃命或想办法！")
                continue
            dist = math.dist(bpos, tpos)
            if dist <= self.ATTACK_REACH:
                if now - e.last_attack_at >= self.ATTACK_INTERVAL:
                    if self._attack(bot, e.target):
                        e.last_attack_at = now
            elif now - e.last_walk_at >= self.WALK_INTERVAL:
                # 逐次刷新追击目标点：目标在跑，旧路径很快作废
                if self._walk(bot, int(tpos[0]), int(tpos[1]), int(tpos[2])):
                    e.last_walk_at = now
