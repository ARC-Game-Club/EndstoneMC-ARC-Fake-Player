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
    #: 上一个动作是不是挖（挖/走交替，观感更像在"玩"而不是刷任务）
    last_was_dig: bool = False
    #: 上一个兴趣点的方位角（弧度）。选下一个点时避开"原路折返"——
    #: 均匀随机会让假人在两个点之间来回打转，看起来像卡了。
    last_angle: float | None = None
    #: 上一个兴趣点坐标（x, z），短期不重复去同一个地方。
    last_poi: tuple[float, float] | None = None
    #: 熔断带离标志：下一个兴趣点取远处，取完即清。
    forced_far: bool = False


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

    #: 两次行动的间隔（秒），随机区间。
    #:
    #: v3.9.20 起 10~26 秒压到 0.8~2.2 秒：用户反馈"执行完一个动作会休息"，
    #: 真人闲逛是连着走的（到点→几乎立刻下一个兴趣点）。微间隔保留一点点
    #: 随机性（还是不想像机器），但不再是肉眼可见的罚站。
    ACTION_GAP = (0.8, 2.2)
    #: 兴趣点离锚点的距离（格），随机区间
    POI_RADIUS = (6, 18)
    #: 到点后顺手挖一块方块的概率（0 = 永不挖）
    dig_chance = 0.25
    #: 离锚点超过这个距离算"远"：每腿朝锚点走 …
    FAR_FROM_ANCHOR = 60.0
    #: 一"腿"的长度（格）
    LEG_LENGTH = 48.0
    #: 兴趣点必须离**当前站位**至少这么远——不然每个周期都只在原地
    #: 一两格内挪，看起来就是"定在家那个点上罚站"。
    MIN_STEP = 4.0

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

    def note_failure(self, name: str, why: str = "") -> None:
        """动作执行失败（引擎寻路不到/被拒）时由回调告知。

        默认要等满一个行动间隔（10~26s）才会再试；失败时缩短到 3 秒，
        否则一次失败就能让假人在原地罚站半个间隔以上。
        """
        st = self.state_of(name)
        st.next_at = self._clock() + 1.5
        if why:
            self._log(f"[{name}] 闲逛动作失败，1.5 秒后重试: {why}")

    def kick_far(self, name: str) -> None:
        """把一个人"带离现场"：下一个兴趣点取远处（40~120 格）。

        大脑在同一片区域连续失败（collect 找不到、goto 不可达…）时由
        插件的熔断钩子调用——真人遇到"这里没有"的反应就是换个地方，
        而不是原地重试。置标志后由 `_tick_one` 消费，一次有效。
        """
        st = self.state_of(name)
        st.forced_far = True
        st.next_at = 0.0
        self._log(f"[{name}] 熔断带离：下个兴趣点取远处")

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
        # 忙就让路：不发起新动作。1 秒后重查——大脑的计划优先，
        # 但计划一结束闲逛层要能立刻接上，不能空 8 秒。
        if self._busy(name):
            st.next_at = now + 1.0
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

        # 正常闲逛：锚点附近随机一个兴趣点。
        # 候选要满足：在锚点半径内、离**当前站位**够远（MIN_STEP，
        # 保证每步肉眼可见）、那一列真的能站（surface）、**不朝反方向
        # 折返**（last_angle ±90° 内的候选优先跳过——均匀随机会让假人
        # 在两个点之间来回打转）、离上一个兴趣点也别太近。
        # 另有 15% 概率"远足"：半径放大一倍，人有时候就是想走远点看看。
        lo, hi = self.POI_RADIUS if anchor is not None else (
            self.POI_RADIUS[0] // 2, self.POI_RADIUS[1] // 2)
        far_stroll = self._rng.random() < 0.15
        if st.forced_far:
            # 熔断带离：这次必须走得远（40~120 格），且挖的动作先免了——
            # 人是"换地方"状态，不是"到新地方接着挖"。
            st.forced_far = False
            far_stroll = True
            lo, hi = 40, 120
            st.last_was_dig = True
        elif far_stroll:
            hi = hi * 2
        tx = ty = tz = None
        for attempt in range(12):
            ang = self._rng.uniform(0, math.tau)
            if (st.last_angle is not None and attempt < 8
                    and abs((ang - st.last_angle + math.pi) % math.tau
                            - math.pi) < math.pi / 2):
                # 落在"来时的反方向"±90° 里：前 8 次尝试优先跳过，
                # 后 4 次放行（地形受限时总得有得走）。
                continue
            r = self._rng.uniform(lo, hi)
            cx, cz = ax + math.cos(ang) * r, az + math.sin(ang) * r
            if math.dist((cx, cz), (pos[0], pos[2])) < self.MIN_STEP:
                continue
            if (st.last_poi is not None
                    and math.dist((cx, cz), st.last_poi) < 6.0):
                continue
            cy = self._surface(cx, cz, pos[1])
            if cy is not None:
                tx, ty, tz = cx, cy, cz
                st.last_angle = ang
                st.last_poi = (cx, cz)
                break
        if tx is None:
            # 一个能站的点都没挑到（地形太陡/未加载）——小退避后重试
            st.next_at = now + 6.0
            return

        # 挖/走交替：连续两次不重复同一种动作
        want_dig = (self.dig_chance > 0 and not st.last_was_dig
                    and self._rng.random() < self.dig_chance * 1.34)
        st.last_was_dig = want_dig
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
