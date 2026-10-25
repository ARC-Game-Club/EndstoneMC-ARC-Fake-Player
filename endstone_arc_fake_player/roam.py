"""锚点闲逛驱动：AI 玩家的"日常生活"。

背景
----
服主的期望：AI 玩家平时就**在家 / 自己的领地 / 公共传送点附近**闲逛——
随机找个兴趣点走过去、到了四处看看、偶尔挖一块方块；离常驻地太远就
自己走回去（服务器禁了传送也照样走）。

这层和 arena 战斗 AI 一样是**条件反射级**的（不等 LLM）：LLM 大脑忙正
事（有计划、在打架、在对局里）时它完全让路；大脑闲着（没配 key、决策
退避中、或单纯没活干）时它接管身体制造"生活气息"。两条路互补：大脑
负责有意义的行为，闲逛负责"看起来活着"。

锚点优先级：**家（OP 设的常驻地）> 自己名下的领地传送点 > 随机一个
公共传送点 > 没有锚点（原地附近小范围晃）**。

远途处理：距离锚点超过 `FAR_FROM_ANCHOR` 时，每个行动周期朝锚点方向
走一"腿"（48 格）——多走几腿自然就回去了，全程不传送，禁传送的服务器
也成立。
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class RoamState:
    """一个假人的闲逛状态。"""

    #: 下次行动时刻（monotonic）。行走由引擎寻路异步完成，这里只管
    #: "什么时候发起下一个动作"。
    next_at: float = 0.0
    #: 当前锚点（x, y, z, 来源标签）。缓存下来用于日志与调试。
    anchor: tuple[float, float, float, str] | None = None


class RoamDriver:
    """每个在线假人一份 `RoamState`，`tick()` 由插件周期调用。

    钩子（注入，测试可替换）：
      names()               当前应闲逛的假人名单
      busy(name)            True=让路（在打架/对局/有计划）
      anchor(name)          (x,y,z,标签) 或 None（没有锚点）
      bot_pos(name)         (x,y,z) 或 None（不在线）
      surface(x, z, near_y) 这一列可站立的 y，或 None
      walk(name, x, y, z)   下发走位（返回是否成功）
      dig(name, x, y, z)    下发走近+挖一块（返回是否成功）
      log(msg)              调试日志
    """

    #: 两次行动的间隔（秒），随机区间——固定间隔看起来像机器
    ACTION_GAP = (10.0, 26.0)
    #: 兴趣点离锚点的距离（格），随机区间
    POI_RADIUS = (6, 18)
    #: 到点后顺手挖一块方块的概率（0 = 永不挖）
    dig_chance = 0.25
    #: 离锚点超过这个距离算"远"：每腿朝锚点走 …
    FAR_FROM_ANCHOR = 60.0
    #: 一"腿"的长度（格）
    LEG_LENGTH = 48.0

    def __init__(self, *, names: Callable[[], list[str]], busy: Callable[[str], bool],
                 anchor: Callable[[str], tuple | None],
                 bot_pos: Callable[[str], tuple | None],
                 surface: Callable[[float, float, float], float | None],
                 walk: Callable[[str, int, int, int], bool],
                 dig: Callable[[str, int, int, int], bool],
                 log: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 rng: random.Random | None = None) -> None:
        self._names = names
        self._busy = busy
        self._anchor = anchor
        self._bot_pos = bot_pos
        self._surface = surface
        self._walk = walk
        self._dig = dig
        self._log = log or (lambda _m: None)
        self._clock = clock
        self._rng = rng or random.Random()
        self._states: dict[str, RoamState] = {}

    def state_of(self, name: str) -> RoamState:
        return self._states.setdefault(name, RoamState())

    # ------------------------------------------------------------------

    def tick(self) -> None:
        now = self._clock()
        for name in self._names():
            try:
                self._tick_one(name, now)
            except Exception as exc:
                self._log(f"闲逛驱动异常 {name}: {exc}")

    def _tick_one(self, name: str, now: float) -> None:
        st = self.state_of(name)
        # 忙就让路：不发起新动作，行动节奏顺延
        if self._busy(name):
            st.next_at = now + 8.0
            return
        if now < st.next_at:
            return
        pos = self._bot_pos(name)
        if pos is None:
            return

        anchor = self._anchor(name)
        st.anchor = anchor
        # 没有锚点：原地附近小范围晃（半径砍半，动效低调）
        if anchor is None:
            ax, ay, az = pos
            kind = "原地"
        else:
            ax, ay, az = anchor[0], anchor[1], anchor[2]
            kind = anchor[3]

        dist = math.dist((pos[0], pos[1], pos[2]), (ax, ay, az))
        if anchor is not None and dist > self.FAR_FROM_ANCHOR:
            # 远途：朝锚点走一腿（不传送——禁传送的服务器也成立，
            # 而且一路走回去本身就是"生活"）
            dx, dz = ax - pos[0], az - pos[2]
            length = math.hypot(dx, dz) or 1.0
            tx = pos[0] + dx / length * min(self.LEG_LENGTH, length)
            tz = pos[2] + dz / length * min(self.LEG_LENGTH, length)
            ty = self._surface(tx, tz, pos[1])
            if ty is not None:
                if self._walk(name, int(tx), int(ty), int(tz)):
                    self._log(f"[{name}] 离{kind} {dist:.0f} 格，走一腿回去")
            st.next_at = now + self._rng.uniform(*self.ACTION_GAP)
            return

        # 正常闲逛：锚点附近随机一个兴趣点
        lo, hi = self.POI_RADIUS if anchor is not None else (
            self.POI_RADIUS[0] // 2, self.POI_RADIUS[1] // 2)
        for _ in range(4):
            ang = self._rng.uniform(0, math.tau)
            r = self._rng.uniform(lo, hi)
            tx, tz = ax + math.cos(ang) * r, az + math.sin(ang) * r
            ty = self._surface(tx, tz, pos[1])
            if ty is not None:
                break
        else:
            st.next_at = now + 6.0
            return

        want_dig = self.dig_chance > 0 and self._rng.random() < self.dig_chance
        if want_dig:
            # 挖"兴趣点脚下的那块"：bot:dig 自带走近，挖完正好人就在
            # 兴趣点上——一步到位地"走过去顺便挖一块"
            ok = self._dig(name, int(tx), int(ty) - 1, int(tz))
            self._log(f"[{name}] 在{kind}附近挖一块 ({int(tx)},{int(ty)-1},{int(tz)})"
                      if ok else f"[{name}] 挖掘指令下发失败")
        else:
            ok = self._walk(name, int(tx), int(ty), int(tz))
            if ok:
                self._log(f"[{name}] 在{kind}附近溜达 ({int(tx)},{int(ty)},{int(tz)})")
        st.next_at = now + self._rng.uniform(*self.ACTION_GAP)
