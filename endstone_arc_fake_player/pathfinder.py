"""纯 Python 寻路。

为什么需要自己写
----------------
Endstone 的 Python API 只能让实体"瞬移"，没有原生的寻路接口。
但直接把机器人一路传送过去非常假，而且会穿墙。

这里的做法是计算一条**符合地形约束的合法路径**，然后逐格传送。
好处：
  · 沿真实可通行地面走，不会穿墙、不会浮空
  · 会绕开岩浆、悬崖
  · 逐格推进 → 看起来是在走，而不是在闪
  · 完全在插件侧完成，不消耗任何 LLM token

算法
----
A*，3D 网格但优先水平移动（重力感知）。节点状态包含"是否需要跳跃"，
因为跳跃会改变可达性。代价函数里对危险方块加重惩罚。
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


Vec3 = tuple[int, int, int]


@dataclass(order=True)
class _Node:
    priority: float
    pos: Vec3 = field(compare=False)
    cost: float = field(compare=False, default=0.0)
    parent: "_Node | None" = field(compare=False, default=None)

    def chain(self) -> list[Vec3]:
        out: list[Vec3] = []
        node: "_Node | None" = self
        while node is not None:
            out.append(node.pos)
            node = node.parent
        return list(reversed(out))


#: 每个"查询函数"返回该坐标的通行代价；负数表示不可通行
ProbeFn = Callable[[int, int, int], float]

#: 代价常量
COST_BLOCKED = -1.0
COST_LAVA = 500.0
COST_WATER = 6.0
COST_JUMP = 3.0
COST_FALL = 1.2
COST_DIAGONAL = 1.414


class PathFinder:
    """在真实方块数据上做 A*。

    为了不把服务器卡死，每次搜索有严格的节点预算；
    超预算就返回已找到的最好结果（或 None）。
    """

    def __init__(self, probe: ProbeFn, *, max_nodes: int = 6000, max_drop: int = 3) -> None:
        self._probe = probe
        self.max_nodes = max_nodes
        self.max_drop = max_drop   # 最多允许往下掉几格

    # -- 邻域 -------------------------------------------------------------

    def _standable(self, x: int, y: int, z: int) -> float | None:
        """`(x,y,z)` 能不能让假人**站住**？能就返回该格代价，不能返回 None。

        为什么需要这个
        --------------
        探针只回答"这一格是什么方块"，回答不了"脚踩在这里会不会往下掉"。
        空气既可能是"地面之上的那一层"（能站），也可能是"悬崖半空"
        （站上去立刻掉）。两者都返回 1.0，光看探针分不出来。

        区别在于**脚下那一格**：

            y     ← 脚在这里（必须是空气/水，站得进去）
            y-1   ← 脚下必须有实心（石头/泥土…），否则是悬空

        这里也顺带排掉了 y+1（头）——Minecraft 里玩家身高两格，
        头那一格也必须是空的，否则是"卡在方块里"。

        ⚠️ 这个检查是修一个"悬崖永远下不去"的 bug 时加的。原逻辑看到
        `probe(悬崖上方的空气) >= 0` 就当成"同层可走"，于是假人会
        走进悬空的格、然后掉下去；而 A* 以为它好好站着走过去了。
        所以真正的病根不是"下落分支写错"，是"同层可走"缺少重力校验。
        """
        here = self._probe(x, y, z)
        if here < 0 or here >= COST_LAVA:
            return None                       # 实心 / 危险，站不进去
        if self._probe(x, y + 1, z) < 0:
            return None                       # 头上有方块，塞不进去
        below = self._probe(x, y - 1, z)
        if below < 0:
            return here                       # 脚下实心 → 站得稳
        if below >= COST_LAVA:
            return None                       # 脚下是岩浆，别站
        # 脚下也是空的。除非那是水面（可以浮着），否则算悬空 → 不能站。
        if below >= COST_WATER:
            return here
        return None

    def _neighbors(self, pos: Vec3, goal: Vec3) -> Iterable[tuple[Vec3, float]]:
        """枚举从 `pos` 一步能到的地方。

        顺序**不能换**，每一步都依赖前一步没被满足
        ------------------------------------------
        1. 同层：目标格能**站住**（不只是"是空气"），直接走过去。
        2. 抬高一格：目标格被占，但上方两格能站，跳上去。
        3. 下落：目标格站不住，那就往下找第一个能站的格。

        ⚠️ 这里修过两个叠在一起的 bug，都会让寻路"看起来能跑、其实
        哪也去不了"，值得记下来：

        a) **抬升分支是死代码。** 原来先写 `if here < 0: continue`，
           再写 `if here < COST_WATER or here > 0:`。后者在 `here >= 0`
           的前提下**后半段恒真**（负数已经被上一行挡掉了），于是永远
           走"同层"并 `continue`，下面的跳跃/下落一次都没执行过。
           而 `here < 0` 就 `continue` 又恰好把"抬脚上一格"这种
           Minecraft 里最常见的通行方式给毙了——一格台阶都上不去。

        b) **"同层"缺少重力校验。** 只判断"目标格是空气"就让人走过去，
           但悬崖边的空气格也是空气。假人走进去会掉下去，而 A* 以为
           它平平安安站着。要判"站得住"必须看**脚下那一格**，
           这就是 `_standable` 存在的原因。

        这两点合起来的症状是：`find()` 返回一条**不含终点**的路径
        （见 b 的连带影响），或者干脆返回 None 然后被上层当成"绕不过去"。
        """
        x, y, z = pos
        probe = self._probe

        for dx, dz in (
            (1, 0), (-1, 0), (0, 1), (0, -1),
            (1, 1), (1, -1), (-1, 1), (-1, -1),
        ):
            nx, nz = x + dx, z + dz
            base = COST_DIAGONAL if dx and dz else 1.0

            # 斜向不能穿墙角：两个直角邻居都得能通
            if dx and dz:
                if probe(x + dx, y, z) < 0 or probe(x, y, z + dz) < 0:
                    continue

            here = probe(nx, y, nz)

            # 岩浆只有当它**就是目标**时才允许踩——否则绕开
            if here >= COST_LAVA and (nx, y, nz) != goal:
                here = COST_BLOCKED

            # --- 1) 同层：站得住就走过去 ---
            cost = self._standable(nx, y, nz)
            if cost is not None:
                yield (nx, y, nz), base + cost
                continue

            # --- 2) 抬高一格（上台阶 / 跳上一格矮墙）---
            cost_up = self._standable(nx, y + 1, nz)
            if cost_up is not None:
                yield (nx, y + 1, nz), base + COST_JUMP + cost_up
                continue

            # --- 3) 下落：往下找第一个能站住的格 ---
            #
            # `max_drop` 限制掉落高度，避免为了抄近路跳下悬崖。
            for drop in range(1, self.max_drop + 1):
                dy = y - drop
                mid = probe(nx, dy, nz)
                if mid >= COST_LAVA:
                    break                     # 中间是岩浆，不能穿过去
                if mid < 0:
                    break                     # 撞到实心面，下面没空间了
                cost_down = self._standable(nx, dy, nz)
                if cost_down is not None:
                    yield (nx, dy, nz), base + COST_FALL * drop + cost_down
                    break


    # -- 主搜索 -----------------------------------------------------------

    @staticmethod
    def _heuristic(a: Vec3, b: Vec3) -> float:
        # 加权的八方向距离，允许斜走所以用切比雪夫式
        dx, dy, dz = abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2])
        return max(dx, dz) + 0.6 * dy

    def find(self, start: Vec3, goal: Vec3) -> list[Vec3] | None:
        """返回从 start 到 goal 的路径（含起点与终点）。

        ⚠️ **到不了就返回 None。** 以前这里有两条"聊胜于无"的分支，
        在预算用完 / 堆空了之后会把"当前最接近目标的路径"返回出去，
        而那条路径**不校验是否真的以 goal 结尾**。后果比直接失败更糟：

          · 调用方看到一条非空路径，以为成功了，照着一格一格传送过去
          · 假人走到半路停下（因为路径本来就到不了），停在半路
          · LLM 看到"移动成功"的反馈，但坐标没变，于是反复重试同一条
            指令，看起来像"假人不听话"

        一个明确报"走不过去"的失败，比一条走一半就断的路径好查得多。
        所以现在这两处都改成返回 None。
        """
        if start == goal:
            return [start]
        if self._probe(*goal) < 0:
            return None

        open_heap: list[_Node] = []
        best: dict[Vec3, float] = {start: 0.0}
        start_node = _Node(self._heuristic(start, goal), start, 0.0, None)
        heapq.heappush(open_heap, start_node)
        closed: set[Vec3] = set()
        expanded = 0

        while open_heap:
            node = heapq.heappop(open_heap)
            if node.pos in closed:
                continue
            closed.add(node.pos)
            expanded += 1

            if node.pos == goal:
                return node.chain()

            if expanded >= self.max_nodes:
                # 预算用完，还没碰到目标 → 老实说没找到
                return None

            for nxt, step_cost in self._neighbors(node.pos, goal):
                if nxt in closed:
                    continue
                new_cost = node.cost + step_cost
                if new_cost < best.get(nxt, math.inf):
                    best[nxt] = new_cost
                    heapq.heappush(
                        open_heap,
                        _Node(new_cost + self._heuristic(nxt, goal), nxt, new_cost, node),
                    )

        # 堆空了还没到 goal：真的走不到
        return None


# ---------------------------------------------------------------------------
# 地形探针工厂
# ---------------------------------------------------------------------------

#: 天然可通行的方块类型（机器人可以走进去）
PASSABLE_KEYWORDS = ("air", "water", "torch", "grass", "flower", "sapling",
                     "sign", "rail", "tall_grass", "seagrass", "kelp")


def make_probe(dimension: Any, world: Any) -> ProbeFn:
    """构造一个把方块名映射成通行代价的探针。"""

    def probe(x: int, y: int, z: int) -> float:
        name = world.get_block_name(dimension, x, y, z)
        if not name or "air" in name or "void" in name:
            return 1.0
        if name in ("lava", "fire", "magma", "campfire"):
            return COST_LAVA
        if "lava" in name:
            return COST_LAVA
        if "water" in name:
            return COST_WATER
        if name in ("cactus", "sweet_berry_bush", "wither_rose"):
            return 8.0
        if name in ("magma_block",):
            return 12.0
        if any(k in name for k in PASSABLE_KEYWORDS):
            return 1.0
        return COST_BLOCKED

    return probe


def walkable_surface(dimension: Any, world: Any, x: int, z: int, y_start: int,
                     range_down: int = 6, range_up: int = 4) -> int | None:
    """在 (x,z) 找到可以站立的 y（脚下有实体方块、身体两格空）。"""
    for y in range(y_start + range_up, y_start - range_down - 1, -1):
        feet = world.get_block_name(dimension, x, y, z)
        head = world.get_block_name(dimension, x, y + 1, z)
        below = world.get_block_name(dimension, x, y - 1, z)
        if not feet or not head or not below:
            continue
        if ("air" in feet or "water" in feet) and ("air" in head) and "air" not in below:
            if "lava" not in below and "fire" not in below:
                return y
    return None
