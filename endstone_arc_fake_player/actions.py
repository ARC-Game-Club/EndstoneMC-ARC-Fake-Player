"""动作编译器：把 LLM 的一次"批量意图"翻译成插件的"多步执行计划"。

这是整个省 token 设计的落点。举几个实际的换算：

    LLM 说: mine_area(x1=10,y1=64,z1=10, x2=14,y2=64,z2=14)
    插件做: 25 次 break_block，按最近的顺序排好，逐格执行

    LLM 说: build_box(..., block="cobblestone")
    插件做: 先按层展开成 N 个 set_block，再用 A* 算出移动路线，
            边走边放，最后汇报"用了 128 个石头，还剩 12 个"

    LLM 说: dig_tunnel(direction="north", length=32)
    插件做: 32×2 = 64 个 break_block

所有几何展开、顺序规划、资源校验都在这里完成，模型不参与。
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from typing import Any, Callable

from .executor import Plan, Step, iter_box, iter_line, nearest_first
from .llm import ToolCall, _norm_block
from .pathfinder import PathFinder, make_probe, walkable_surface
from .world import HOSTILE, PASSIVE, WorldAdapter, classify, short_id


@dataclass
class CompileResult:
    """编译结果：要么产出一个计划，要么产出一条即时反馈。"""

    plan: Plan | None = None
    immediate: str = ""          # 直接回给模型看的结果文本
    ok: bool = True


class ActionCompiler:
    """把工具调用编译成执行计划。"""

    MAX_AREA = 4096       # 单个区域最多多少格，防止模型给一个大得离谱的范围
    REACH = 8             # 机器人的"手长"：超过这个距离要先走过去
    #: 真挖掘的够得着距离。
    #:
    #: ⚠️ 这个值必须**不大于**原版 `breakBlock` 的实际可挖距离。原版玩家
    #: 交互距离约 4.5 格；曾经写成 5，中间那 0.5 格是个"死区"——假人
    #: 站在 4.5~5 格之间时 `_within_reach` 判"够得着"（≤5）就直接挖，
    #: 但 breakBlock 其实够不着，于是挖不动、又因为判"够得着"而**不走过去**，
    #: 变成"面对着方块空挖"（用户报的"挖方块不走动"）。
    #: 和 `ATTACK_REACH = 4.5` 保持一致，两个"够得着"语义统一。
    DIG_REACH = 4.5
    #: 真攻击的够得着距离。
    #:
    #: ⚠️ 这个值必须和 `main.js` 里 `doAttack` 的判定**保持一致**
    #: （那边是 `bestDist > 4.5` 就返回"够不着"）。写大了的后果不是
    #: "偶尔打空"，而是**必然失败**：编译器会选中一个站在 8 格外的
    #: 目标，下发攻击指令，然后稳定收到"距离 8.0 格，够不着"。
    #: 模型看到的是"攻击失败"却查不到原因。
    ATTACK_REACH = 4.5

    def __init__(self, world: WorldAdapter, logger: Any) -> None:
        self.world = world
        self.logger = logger
        self._notes: dict[str, tuple[int, int, int, str]] = {}
        self._goto_player: Callable[[str], Any] | None = None
        self._land_nearby: Any = None
        self._set_home_cb: Any = None
        self._hostile_tracker: Any = None
        #: 是否让挖掘走真实假人。插件启用行为包后会打开。
        self.use_real_dig = False
        #: 找矿时是否只找"表面方块"（六面中至少一面朝空气/水）。
        #:
        #: 与 `WorldAdapter.surface_only` 是两个独立开关，必须分开：
        #:   · world.surface_only 管**感知**——模型能看见什么
        #:   · 这个管**挖掘**——插件替模型找什么
        #: 只关其中一个都会留下"看不见但能挖到"的矿透后门，模型很快
        #: 就会发现并绕过去。所以两者默认都是开的。
        self.surface_only = True

    def bind_player_lookup(self, fn: Callable[[str], Any]) -> None:
        self._goto_player = fn

    def bind_set_home(self, fn: Any) -> None:
        """注入"把当前位置设为家常驻地"的回调：(name, x, y, z)。"""
        self._set_home_cb = fn

    def bind_land_nearby(self, fn: Any) -> None:
        """注入"这附近（3x3 区块）有没有领地"的查询：(cx, cz, dim) -> bool。

        由插件绑到弧光核心的 api_has_land_nearby_chunk；核心不在时为
        None，粗闸直接放行——硬闸仍是执行期的逐格 modify_guard。"""
        self._land_nearby = fn

    # ------------------------------------------------------------------
    # 位置工具
    # ------------------------------------------------------------------

    @staticmethod
    def _floor(loc: Any) -> tuple[int, int, int]:
        return (int(math.floor(loc.x)), int(math.floor(loc.y)), int(math.floor(loc.z)))

    def _order_by_distance(self, actor: Any, cells: list[tuple[int, int, int]]
                           ) -> list[tuple[int, int, int]]:
        """按离机器人由近到远排序，减少来回跑动。"""
        return nearest_first(self._floor(actor.location), cells)

    # ------------------------------------------------------------------
    # 挖掘步骤生成
    # ------------------------------------------------------------------

    def dig_steps(self, cells: list[tuple[int, int, int]]) -> list[Step]:
        """把一批坐标编译成挖掘步骤。

        两种模式：

        **直连模式**（没有行为包）——直接对世界写空气，一个 ``break_block``
        步骤就够了。方块会瞬间消失，没有手臂动画，但任务效果完全一致。

        **真挖掘模式**（有行为包）——每个方块生成 ``dig_far`` 步骤，
        标记为异步：假人先真的走到方块旁边（够不着的话），再用
        ``breakBlock`` 一下下敲。执行器会等每个方块真的被挖掉才动下一个，
        所以看起来就是"一个人在一格一格有序地挖"。

        为什么统一用 ``dig_far`` 而不是分开 near/far：可达性是**变化的**——
        假人一开始可能离得很远，但挖着挖着就走到跟前了。让处理器每步
        自己判断当前距离，比在编译期就钉死要准。
        """
        if not self.use_real_dig:
            return [Step("break_block", x, y, z) for (x, y, z) in cells]
        return [Step("dig_far", x, y, z, async_op=True) for (x, y, z) in cells]

    def _reachable_first(self, actor: Any, cells: list[tuple[int, int, int]]
                         ) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
        """把手长够得着的和够不着的分开。够不着的要先寻路。"""
        ax, ay, az = self._floor(actor.location)
        near, far = [], []
        for c in cells:
            if abs(c[0] - ax) <= self.REACH and abs(c[1] - ay) <= self.REACH and abs(c[2] - az) <= self.REACH:
                near.append(c)
            else:
                far.append(c)
        return near, far

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    #: 会改变世界方块的动作——动土前先过"附近有没有领地"的粗闸
    #: （别人领地周边 3x3 区块内不挖不建，避免挖墙脚/贴边破坏）。
    DIG_PLACE_ACTIONS = {
        "mine_block", "mine_area", "dig_tunnel", "mine_vein", "collect",
        "chop_tree", "clear_area", "descend",
        "build_box", "build_wall", "build_floor", "place_line",
        "build_structure",
    }
    #: 步骤里真正动方块的 op（walk_to/attack 等坐标不算）
    DIG_PLACE_STEP_OPS = {"break_block", "dig_far", "place_real", "set_block"}
    #: 一次计划最多查多少个区块（超长隧道取前 16 个，足够代表性）
    MAX_LAND_CHUNK_CHECKS = 16

    def compile(self, actor: Any, call: ToolCall, ctx: dict[str, Any]) -> CompileResult:
        handler = getattr(self, f"_do_{call.name}", None)
        if handler is None:
            return CompileResult(ok=False, immediate=f"未知动作 {call.name}")
        try:
            result = handler(actor, call.args, ctx)
        except ValueError as exc:
            return CompileResult(ok=False, immediate=f"{call.name} 参数问题: {exc}")
        except Exception as exc:
            self.logger.debug(f"编译 {call.name} 失败: {exc}")
            return CompileResult(ok=False, immediate=f"{call.name} 执行失败: {exc}")
        # 动土粗闸：挖/建类计划的目标区块附近（3x3）有领地就整计划拒绝。
        # 放在统一的出口而不是每个 handler 里——新加的动土动作天然被覆盖。
        if result.plan is not None and call.name in self.DIG_PLACE_ACTIONS:
            near = self._land_nearby_chunk_of(actor, result.plan)
            if near is not None:
                cx, cz = near
                return CompileResult(
                    ok=False,
                    immediate=f"区块 ({cx},{cz}) 附近 3x3 范围内有玩家的领地，"
                              f"这里不动土（挖和建都不行，别贴着别人家挖墙脚）。"
                              f"走到离领地远一点的地方（50 格开外）再继续。")
        return result

    def _land_nearby_chunk_of(self, actor: Any, plan: Plan) -> tuple[int, int] | None:
        """计划步骤坐标 → 涉及区块 → 逐个查附近领地。命中返回该区块。"""
        if self._land_nearby is None:
            return None
        try:
            dim = str(getattr(actor.dimension, "name", "overworld"))
        except Exception:
            dim = "overworld"
        seen: set[tuple[int, int]] = set()
        for step in plan.steps:
            if getattr(step, "op", "") not in self.DIG_PLACE_STEP_OPS:
                continue
            seen.add((int(step.x) >> 4, int(step.z) >> 4))
            if len(seen) >= self.MAX_LAND_CHUNK_CHECKS:
                break
        for cx, cz in sorted(seen):
            try:
                if self._land_nearby(cx, cz, dim):
                    return (cx, cz)
            except Exception:
                return None      # 查询异常按无领地放行（硬闸在执行期）
        return None

    # ------------------------------------------------------------------
    # 采集类
    # ------------------------------------------------------------------

    def _do_mine_block(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        """挖掉指定坐标的单个方块。

        `mine_area` 被描述成批量工具后，模型不会为一块方块用它。
        要挖"眼前这一块"（一块矿石、一截原木）时是这个入口。
        实现上也只是 1 格区域，走同一套 dig_steps。
        """
        x, y, z = int(a["x"]), int(a["y"]), int(a["z"])
        if self.world.is_air(actor.dimension, x, y, z):
            return CompileResult(
                immediate=f"({x},{y},{z}) 已经是空气，没有可挖的方块")
        steps = self.dig_steps([(x, y, z)])
        return CompileResult(
            plan=Plan(label=f"挖掉方块({x},{y},{z})", steps=steps))

    #: 单次挖掘计划的最大格数。一次排 296 格的"工程"本身就是机器行为
    #: （真人挖一段看一段），而且 5 天日志里出现过 296/316 格的计划
    #: 全军覆没（0/296）。限到 48 格：挖完计划结束，模型看到结果后
    #: 自然会决定"接着挖"还是"换个地方"——更像人，也控得住失败半径。
    MAX_DIG_PLAN = 48

    def _do_mine_area(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        cells = list(iter_box(a["x1"], a["y1"], a["z1"], a["x2"], a["y2"], a["z2"],
                              order="bottom", max_steps=self.MAX_AREA))
        # 挖矿就是要全清，但跳过空气以省步骤
        solid = [
            (x, y, z) for (x, y, z) in self._order_by_distance(actor, cells)
            if not self.world.is_air(actor.dimension, x, y, z)
        ]
        if not solid:
            return CompileResult(immediate="该区域已经是空的，没有可挖的方块")
        note = ""
        if len(solid) > self.MAX_DIG_PLAN:
            note = (f"区域共有 {len(solid)} 格，这次先挖最近的 {self.MAX_DIG_PLAN} 格，"
                    f"挖完再看要不要继续。")
            solid = solid[: self.MAX_DIG_PLAN]
        steps = self.dig_steps(solid)
        return CompileResult(
            plan=Plan(label=f"挖区域({len(steps)}格)", steps=steps),
            immediate=note)

    def _do_clear_area(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        cells = list(iter_box(a["x1"], a["y1"], a["z1"], a["x2"], a["y2"], a["z2"],
                              order="bottom", max_steps=self.MAX_AREA))
        solid = [
            (x, y, z) for (x, y, z) in self._order_by_distance(actor, cells)
            if not self.world.is_air(actor.dimension, x, y, z)
        ]
        if not solid:
            return CompileResult(immediate="该区域已经是空的")
        note = ""
        if len(solid) > self.MAX_DIG_PLAN:
            note = (f"区域共有 {len(solid)} 格，这次先清最近的 {self.MAX_DIG_PLAN} 格，"
                    f"挖完再看要不要继续。")
            solid = solid[: self.MAX_DIG_PLAN]
        steps = self.dig_steps(solid)
        return CompileResult(
            plan=Plan(label=f"清空区域({len(steps)}格)", steps=steps),
            immediate=note)

    def _do_dig_tunnel(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        """挖隧道。

        语义上这是"开凿"而不是"清扫"——即使前面是空气，也要往下/往侧面
        扩展，否则挖出来的是个悬空的洞。所以这里对空气格不会跳过，
        但会先做一个"如果整条路径都是空气就报错"的检查，避免无意义执行。

        ⚠️ 垂直方向（up/down）被**刻意禁掉**。直上直下挖是最经典的
        机器行为（真人知道会摔死/被岩浆浇），5 天日志里 down 隧道挖了
        62 次。下矿用 `descend`（阶梯式下探）替代——模型会从失败反馈
        里得到指引。
        """
        direction = a.get("direction", "north")
        if direction in ("up", "down"):
            return CompileResult(
                ok=False,
                immediate="不要垂直挖（会摔死/被岩浆浇）。往下走用 descend（阶梯式下探），"
                          "水平隧道用 north/south/east/west。")
        length = int(a.get("length", 8))
        height = int(a.get("height", 2))
        ox, oy, oz = self._floor(actor.location)

        dx, dy, dz = {"north": (0, 0, -1), "south": (0, 0, 1),
                      "east": (1, 0, 0), "west": (-1, 0, 0)}.get(direction, (0, 0, -1))

        cells: list[tuple[int, int, int]] = []
        for i in range(1, length + 1):
            bx, by, bz = ox + dx * i, oy + dy * i, oz + dz * i
            for h in range(height):
                cells.append((bx, by + h, bz))
                # 水平隧道：把脚下那层也带上，隧道才有地板
                if h == 0:
                    cells.append((bx, by - 1, bz))
        cells = list(dict.fromkeys(cells))

        dim = actor.dimension
        solid = [c for c in cells if not self.world.is_air(dim, *c)]
        if not solid:
            return CompileResult(
                immediate=f"朝 {direction} 方向 {length} 格内没有任何方块，无需挖掘")

        # 按沿路径的距离排序，保证是"一段段推进"而不是到处乱挖
        solid = [(x, y, z) for (x, y, z) in cells
                 if not self.world.is_air(dim, x, y, z)]
        solid.sort(key=lambda c: (abs(c[0] - ox) + abs(c[2] - oz), abs(c[1] - oy)))
        steps = self.dig_steps(solid)
        return CompileResult(plan=Plan(label=f"挖隧道 {direction} {length}格", steps=steps))

    def _do_descend(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        """阶梯式下探：像真人挖楼梯一样，"前进一格、下一层"地往下走。

        每层展开成三步挖掘（脚下 + 前方列的高低两格），最后走到底部。
        全程都是水平邻格与脚下格，不会挖出垂直深井——不会摔伤，
        碰到岩浆也有反应空间。模型要下矿时用它替代被禁掉的 down 隧道。
        """
        depth = max(1, min(12, int(a.get("depth", 8) or 8)))
        ox, oy, oz = self._floor(actor.location)
        # 方向随机取水平一支：拿不到"面朝方向"，随机还能避免每只假人
        # 都朝同一个方向挖出一条一模一样的走廊。
        dx, dz = random.choice(((0, -1), (0, 1), (1, 0), (-1, 0)))

        cells: list[tuple[int, int, int]] = []
        cx, cy, cz = ox, oy, oz
        for _ in range(depth):
            # 本层脚下 + 前方一列（新层的高低两格）
            cells += [(cx, cy - 1, cz),
                      (cx + dx, cy - 1, cz + dz),
                      (cx + dx, cy - 2, cz + dz)]
            cx, cy, cz = cx + dx, cy - 1, cz + dz
        cells = list(dict.fromkeys(cells))
        steps = self.dig_steps(cells)
        steps.append(Step("walk_to", cx, cy, cz, async_op=self.use_real_dig))
        return CompileResult(plan=Plan(
            label=f"阶梯下探{depth}层", steps=steps, steps_per_tick=2),
            immediate=f"开始阶梯式下探 {depth} 层（每层下一格，安全下矿）")

    def _do_mine_vein(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        """找附近的矿石并挖掉。插件负责搜索，模型只说要什么。

        如果附近没有，不说"找不到"就完了——而是根据常见矿物的分布高度，
        给出一个可执行的建议（往下挖）。这样模型不用自己记住矿业知识。
        """
        target = short_id(a.get("block", ""))
        cap = int(a.get("max", 16))
        if self._needs_pickaxe(target) and not self._has_tool(actor, "pickaxe"):
            return CompileResult(
                ok=False,
                immediate=f"挖 {target} 必须有镐子（空手挖不掉）。"
                          f"先砍树做木镐：chop_tree → craft planks → craft stick → "
                          f"craft wooden_pickaxe，然后再来挖矿")
        found = self._find_blocks(actor, target, cap, radius=20)
        if found:
            steps = self.dig_steps(found)
            return CompileResult(plan=Plan(label=f"采集{target}x{len(steps)}", steps=steps))

        # 给模型一条可行的下一步，而不是一句"没找到"
        hints = {
            "diamond_ore": "钻石通常在 y=-59 到 y=12 之间，建议先 descend 阶梯式下探",
            "iron_ore": "铁矿石常见于 y=0 到 y=64，建议往地下走（descend 阶梯下探）",
            "gold_ore": "金矿石常见于 y=-48 以下，建议深度挖掘",
            "coal_ore": "煤炭从地表到深处都有，可以换个方向探索",
            "copper_ore": "铜矿常见于 y=0 到 y=96",
            "redstone_ore": "红石常见于 y=-63 到 y=15",
            "lapis_ore": "青金石常见于 y=-64 到 y=30",
            "emerald_ore": "绿宝石只在山地生物群系生成",
        }
        hint = hints.get(target, "可以换个方向探索，或先用 descend 阶梯式下探")
        return CompileResult(
            ok=False,
            immediate=f"20 格内没发现 {target}。{hint}",
        )

    #: 石头系方块（没镐子挖了不掉落）。判据用子串，宁多勿漏——
    #: 泥土/木头不在内（空手能挖能掉）。
    _STONE_FAMILY_RE = None  # 惰性编译，见 _needs_pickaxe

    @staticmethod
    def _needs_pickaxe(block: str) -> bool:
        import re as _re
        if ActionCompiler._STONE_FAMILY_RE is None:
            ActionCompiler._STONE_FAMILY_RE = _re.compile(
                r"(_ore|stone|cobble|deepslate|granite|diorite|andesite|"
                r"quartz|basalt|netherrack|blackstone|obsidian|brick|"
                r"concrete|tuff|calcite|marble|amethyst|dripstone)")
        return bool(ActionCompiler._STONE_FAMILY_RE.search(str(block or "")))

    def _has_tool(self, actor: Any, kind: str) -> bool:
        """背包里有没有指定类别的工具（镐/斧/锹，任意材质）。"""
        for name, _cnt in self.world.scan_inventory(actor):
            if name.endswith(f"_{kind}"):
                return True
        return False

    def _do_collect(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        """收集若干块指定方块——和 mine_vein 类似但语义是"攒够数量"。"""
        target = short_id(a.get("block", ""))
        need = int(a.get("count", 8))

        # 先看背包里已有多少
        have = 0
        for name, amount in self.world.scan_inventory(actor):
            if target in name:
                have += amount
        if have >= need:
            return CompileResult(immediate=f"背包里已有 {have} 个 {target}，够了")

        if self._needs_pickaxe(target) and not self._has_tool(actor, "pickaxe"):
            return CompileResult(
                ok=False,
                immediate=f"挖 {target} 必须有镐子（空手挖石头不掉东西）。"
                          f"先 chop_tree 砍树拿原木 → craft 木板和木棍 → "
                          f"craft wooden_pickaxe（木镐），再回来挖。"
                          f"有条件就升级石镐/铁镐")
        found = self._find_blocks(actor, target, max(need - have, 4), radius=20)
        if not found:
            return CompileResult(
                ok=False,
                immediate=f"20 格内没找到 {target}（背包现有 {have}/{need}）。换个地方或用 goto 过去",
            )
        steps = self.dig_steps(found)
        return CompileResult(
            plan=Plan(label=f"收集{target}({len(steps)}格)", steps=steps),
            immediate=f"开始收集 {target}，当前背包 {have}/{need}",
        )

    def _do_chop_tree(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        """砍整棵树：从根部 BFS 把相连的原木全找出来。"""
        x, y, z = int(a["x"]), int(a["y"]), int(a["z"])
        dim = actor.dimension
        root = self.world.get_block_name(dim, x, y, z)
        if not root or "log" not in root and "stem" not in root and "wood" not in root:
            return CompileResult(ok=False, immediate=f"({x},{y},{z}) 不是木头")

        seen: set[tuple[int, int, int]] = set()
        queue = [(x, y, z)]
        logs: list[tuple[int, int, int]] = []
        while queue and len(logs) < 128:
            cx, cy, cz = queue.pop(0)
            if (cx, cy, cz) in seen:
                continue
            seen.add((cx, cy, cz))
            name = self.world.get_block_name(dim, cx, cy, cz)
            if not name or not any(k in name for k in ("log", "stem", "wood")):
                continue
            logs.append((cx, cy, cz))
            for ddx, ddy, ddz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                queue.append((cx + ddx, cy + ddy, cz + ddz))

        if not logs:
            return CompileResult(ok=False, immediate="没能定位到树干")
        # 从下往上砍：先断根，上面的会自然掉（物理上我们用直接清除模拟）
        logs.sort(key=lambda c: c[1])
        steps = self.dig_steps(logs)
        return CompileResult(plan=Plan(label=f"砍树({len(steps)}格木)", steps=steps))

    # ------------------------------------------------------------------
    # 建造类
    # ------------------------------------------------------------------

    def _count_material(self, actor: Any, block: str) -> int:
        target = short_id(block)
        total = 0
        for name, amount in self.world.scan_inventory(actor):
            if target in name or name in target:
                total += amount
        return total

    def _do_build_box(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        block = a["block"]
        cells = list(iter_box(a["x1"], a["y1"], a["z1"], a["x2"], a["y2"], a["z2"],
                              order="layer", max_steps=self.MAX_AREA))
        return self._make_build_plan(actor, cells, block, "填充区域")

    def _do_build_wall(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        block = a["block"]
        x1, y1, z1 = a["x1"], a["y1"], a["z1"]
        x2, y2, z2 = a["x2"], a["y2"], a["z2"]
        # 把三个轴里最薄的那个压成 1，形成"面"
        spans = [(abs(x2 - x1), 0), (abs(y2 - y1), 1), (abs(z2 - z1), 2)]
        thin_axis = min(spans)[1]
        cell_list = list(iter_box(x1, y1, z1, x2, y2, z2, order="layer", max_steps=self.MAX_AREA))
        cells = [c for c in cell_list if c[thin_axis] == (x1, y1, z1)[thin_axis]]
        cells = list(dict.fromkeys(cells))
        return self._make_build_plan(actor, cells, block, "砌墙")

    def _do_build_floor(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        block = a["block"]
        y = a["y"]
        cells = [(x, y, z)
                 for (x, _y, z) in iter_box(a["x1"], y, a["z1"], a["x2"], y, a["z2"],
                                            order="snake", max_steps=self.MAX_AREA)]
        return self._make_build_plan(actor, cells, block, "铺地板")

    def _do_build_structure(self, actor: Any, a: dict[str, Any],
                            ctx: dict[str, Any]) -> CompileResult:
        """按蓝图一次建造：LLM 给一份方块清单（JSON），插件逐块放。

        格式（off 相对当前位置）：
            [{"off": [dx, dy, dz], "block": "oak_planks"}, ...]
        也接受绝对坐标 {"x","y","z","block"}。校验：方块白名单、
        偏移范围、数量上限；按类型做材料预算（不够先放够数的部分）。
        盖房子这类"成型建筑"用它，一次调用就是一整面墙/一栋小屋。
        """
        from .llm import ALLOWED_BLOCKS

        raw = a.get("blocks")
        items = self._parse_blueprint(raw, actor)
        if items is None:
            return CompileResult(
                ok=False,
                immediate='蓝图格式不对：要 JSON 数组，如 '
                          '[{"off":[0,0,0],"block":"oak_planks"},'
                          '{"off":[1,0,0],"block":"oak_planks"}]'
                          '（off 相对你，±20 格内，最多 160 块）')
        if not items:
            return CompileResult(ok=False, immediate="蓝图是空的")

        ox, oy, oz = self._floor(actor.location)
        dim = actor.dimension
        cells: list[tuple[int, int, int, str]] = []
        for off, block in items:
            bx, by, bz = ox + off[0], oy + off[1], oz + off[2]
            if not self.world.is_air(dim, bx, by, bz):
                continue          # 已有方块的位置跳过（地基不平很正常）
            cells.append((bx, by, bz, block))
        if not cells:
            return CompileResult(immediate="蓝图范围内的位置都已被占用，无需建造")

        # 材料预算（按类型）：够多少放多少，缺口写进反馈
        need_by: dict[str, int] = {}
        for _, _, _, b in cells:
            need_by[b] = need_by.get(b, 0) + 1
        notes: list[str] = []
        allowed_cells = []
        for b, need in sorted(need_by.items()):
            have = self._count_material(actor, b)
            take = min(need, have)
            if have <= 0:
                notes.append(f"没有 {b}（缺 {need}）")
            elif take < need:
                notes.append(f"{b} 只够 {take}/{need}")
            if take > 0:
                picked = [c for c in cells if c[3] == b][:take]
                allowed_cells.extend(picked)
        if not allowed_cells:
            return CompileResult(
                ok=False,
                immediate="背包里没有任何蓝图材料：" + "；".join(notes)
                          + "。先去采集（collect/ chops），凑够再来 build_structure")

        # 底层先放、近的先放——像真人砌墙的顺序
        allowed_cells.sort(key=lambda c: (c[1], (c[0]-ox)**2 + (c[2]-oz)**2))
        if self.use_real_dig:
            steps = [Step("place_real", x, y, z, block=b, async_op=True)
                     for (x, y, z, b) in allowed_cells]
        else:
            steps = [Step("set_block", x, y, z, block=b)
                     for (x, y, z, b) in allowed_cells]
            steps.sort(key=lambda s: (s.y, s.x, s.z))
        label = f"建造蓝图({len(steps)}块)"
        imm = f"开始按蓝图建造 {len(steps)} 块"
        if notes:
            imm += "（" + "；".join(notes) + "，缺口先跳过）"
        return CompileResult(plan=Plan(label=label, steps=steps), immediate=imm)

    #: 蓝图的硬边界
    BLUEPRINT_MAX_BLOCKS = 160
    BLUEPRINT_MAX_OFFSET = 20

    def _parse_blueprint(self, raw: Any, actor: Any = None) -> list[tuple[tuple[int, int, int], str]] | None:
        """把模型的蓝图参数解析成 [(off, block)]。格式不对返回 None。"""
        from .llm import ALLOWED_BLOCKS, _norm_block

        items = None
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return None
            import json as _json
            for attempt in (text, text.replace("'", '"')):
                try:
                    items = _json.loads(attempt)
                    break
                except Exception:
                    continue
            if items is None:
                import ast as _ast
                try:
                    items = _ast.literal_eval(text)
                except Exception:
                    return None
        elif isinstance(raw, list):
            items = raw
        if not isinstance(items, list) or not items:
            return None
        out: list[tuple[tuple[int, int, int], str]] = []
        for it in items[: self.BLUEPRINT_MAX_BLOCKS * 2]:
            if not isinstance(it, dict):
                return None
            nb = _norm_block(str(it.get("block") or ""))
            if not nb:
                return None
            block = nb.split(":", 1)[-1]     # 白名单存短名，剥掉 minecraft: 前缀
            if block not in ALLOWED_BLOCKS:
                return None
            if "off" in it:
                off = it.get("off")
                if (not isinstance(off, (list, tuple)) or len(off) != 3):
                    return None
                try:
                    dx, dy, dz = int(off[0]), int(off[1]), int(off[2])
                except (TypeError, ValueError):
                    return None
                if (abs(dx) > self.BLUEPRINT_MAX_OFFSET
                        or abs(dz) > self.BLUEPRINT_MAX_OFFSET
                        or not -8 <= dy <= 20):
                    return None
            elif all(k in it for k in ("x", "y", "z")):
                if actor is None:
                    return None
                try:
                    ax, ay, az = int(it["x"]), int(it["y"]), int(it["z"])
                except (TypeError, ValueError):
                    return None
                act = self._floor(actor.location)
                dx, dy, dz = ax - act[0], ay - act[1], az - act[2]
                if (abs(dx) > self.BLUEPRINT_MAX_OFFSET
                        or abs(dz) > self.BLUEPRINT_MAX_OFFSET
                        or not -8 <= dy <= 20):
                    return None
            else:
                return None
            out.append(((dx, dy, dz), block))
            if len(out) >= self.BLUEPRINT_MAX_BLOCKS:
                break
        return out

    def _do_farm(self, actor: Any, a: dict[str, Any],
                 ctx: dict[str, Any]) -> CompileResult:
        """开垦农田：身边找泥土/草地 → 锄头耕地 → 播种小麦。

        种地的完整链路（教给大脑的）：打草拿种子（collect short_grass）
        → craft 锄头 → farm 耕地播种 → 等麦子成熟 → collect wheat 收割
        （掉麦子+种子，种子循环用）→ craft bread（3 麦子横排）。
        编译期两道闸：没锄头、种子不够 → 拒绝并教补课路径。
        """
        size = max(1, min(5, int(a.get("size", 3) or 3)))   # size×size 格
        seeds = short_id(str(a.get("seed") or "wheat_seeds"))
        ox, oy, oz = self._floor(actor.location)
        dim = actor.dimension

        # 找可耕格：泥土/草地 且上方是空气（苗要长）
        cells: list[tuple[int, int, int]] = []
        for dx in range(-size, size + 1):
            for dz in range(-size, size + 1):
                x, z = ox + dx, oz + dz
                y = oy - 1               # 脚下方块
                if y < -60:
                    continue
                name = self.world.get_block_name(dim, x, y, z)
                if name not in ("dirt", "grass_block", "dirt_path"):
                    continue
                if not self.world.is_air(dim, x, y + 1, z):
                    continue
                cells.append((x, y, z))
        if not cells:
            return CompileResult(
                ok=False,
                immediate="脚下和周围没有能耕的泥土/草地（要么上面有东西，"
                          "要么不是泥地）。换块开阔的泥地再 farm")

        # 闸 1：锄头
        if not self._has_tool(actor, "hoe"):
            return CompileResult(
                ok=False,
                immediate=f"种地要有锄头（耕 {len(cells)} 格地）。"
                          f"先 craft 木锄/石锄（锄头 = 2 木板 + 2 木棍），"
                          f"拿好再来 farm")
        # 闸 2：种子
        have_seeds = 0
        for name, cnt in self.world.scan_inventory(actor):
            if "seed" in name:
                have_seeds += cnt
        if have_seeds < len(cells):
            if have_seeds <= 0:
                return CompileResult(
                    ok=False,
                    immediate="背包里没有种子。先 collect short_grass 打草——"
                              "草有概率掉小麦种子（打一片总能凑够）；"
                              "或者收割别人田里的成熟麦子也会掉种子")
            cells = cells[:have_seeds]
        steps = []
        for (x, y, z) in cells:
            steps.append(Step("till", x, y, z, async_op=True))
        for (x, y, z) in cells:
            steps.append(Step("plant", x, y, z, async_op=True, note=seeds))
        note = ""
        if len(cells) < (2 * size + 1) ** 2:
            note = f"（这片只找到 {len(cells)} 格可耕的地）"
        return CompileResult(
            plan=Plan(label=f"开垦农田({len(cells)}格)", steps=steps),
            immediate=f"开始种地：先耕地再播种 {len(cells)} 格{note}。"
                      f"麦子成熟后用 collect wheat 收割（掉麦子和种子），"
                      f"3 个麦子 craft 成面包")

    def _do_claim_land(self, actor: Any, a: dict[str, Any],
                       ctx: dict[str, Any]) -> CompileResult:
        """圈地安家：以当前位置为中心圈一块自己的领地。

        编译期只做一道便宜的闸（附近 3x3 区块有领地就拒绝并让大脑
        走远点）；真正的重叠校验/扣费/建地由插件的主线程处理器调
        弧光核心 api_claim_land 完成（同玩家圈地规则：面积×单价，
        从假人钱包扣）。
        """
        radius = max(2, min(24, int(a.get("radius", 8) or 8)))
        name = str(a.get("name") or "").strip()[:24]
        x, y, z = self._floor(actor.location)
        if self._land_nearby is not None:
            try:
                dim = str(getattr(actor.dimension, "name", "overworld"))
                if self._land_nearby(x >> 4, z >> 4, dim):
                    return CompileResult(
                        ok=False,
                        immediate="这附近 3x3 区块内有别人的领地，不能在这圈地。"
                                  "朝一个方向走 60 格以上再试。")
            except Exception:
                pass
        spec = {"radius": radius, "name": name, "x": x, "y": y, "z": z}
        return CompileResult(
            plan=Plan(label=f"圈地安家(半径{radius})",
                      steps=[Step("claim_land", x, y, z, async_op=False,
                                  note=json.dumps(spec, ensure_ascii=False))]),
            immediate=f"开始圈地：以 ({x},{z}) 为中心、半径 {radius}"
                      f"（费用从钱包扣，成功后自动把家设在这）")

    def _do_set_home(self, actor: Any, a: dict[str, Any],
                     ctx: dict[str, Any]) -> CompileResult:
        """把当前位置设为家常驻地（闲逛锚点、睡觉回这）。"""
        if self._set_home_cb is None:
            return CompileResult(ok=False, immediate="本服不支持设家")
        x, y, z = self._floor(actor.location)
        name = str(getattr(actor, "name", "") or "")
        try:
            self._set_home_cb(name, x, y, z)
        except Exception:
            return CompileResult(ok=False, immediate="设家失败")
        return CompileResult(immediate=f"家已设为 ({x},{y},{z})，"
                                        f"闲逛会以这里为中心，天黑记得回这睡")

    def _do_place_line(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        block = a["block"]
        cells = list(iter_line(a["x1"], a["y1"], a["z1"], a["x2"], a["y2"], a["z2"],
                               max_steps=self.MAX_AREA))
        return self._make_build_plan(actor, cells, block, "摆放", keep_order=True)

    def _make_build_plan(self, actor: Any, cells: list[tuple[int, int, int]],
                         block: str, label: str, keep_order: bool = False) -> CompileResult:
        """建造计划：只放空气格，按层推进，并做材料预算。"""
        dim = actor.dimension
        todo = [c for c in cells if self.world.is_air(dim, *c)]
        if not todo:
            return CompileResult(immediate="目标位置已经被占满了，无需建造")

        have = self._count_material(actor, block)
        short = len(todo) - have
        if have <= 0:
            return CompileResult(
                ok=False,
                immediate=f"背包里没有 {short_id(block)}，需要先采集 {len(todo)} 个",
            )

        note = ""
        if short > 0:
            todo = todo[:have]
            note = f"材料只够 {have} 块，将先放这部分"

        if self.use_real_dig:
            # 有真身体：让假人自己走过去、用手放。会有放置动作和音效，
            # 而且是逐个推进的——看起来就是人在砌墙。
            steps = [Step("place_real", x, y, z, block=block, async_op=True)
                     for (x, y, z) in todo]
        else:
            steps = [Step("set_block", x, y, z, block=block) for (x, y, z) in todo]
            if not keep_order:
                steps.sort(key=lambda s: (s.y, s.x, s.z))

        result = CompileResult(plan=Plan(label=f"{label}({len(steps)}格{short_id(block)})", steps=steps))
        result.immediate = note or f"开始{label}，使用 {len(steps)} 个 {short_id(block)}"
        return result

    # ------------------------------------------------------------------
    # 移动类
    # ------------------------------------------------------------------

    def _do_goto(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        return self._plan_path(actor, (a["x"], a["y"], a["z"]))

    def _do_come_to(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        player = a.get("player", "")
        if self._goto_player is None:
            return CompileResult(ok=False, immediate="无法查询玩家")
        target = self._goto_player(player)
        if target is None:
            return CompileResult(ok=False, immediate=f"玩家 {player} 不在线")
        loc = target.location
        return self._plan_path(actor, (int(loc.x), int(loc.y), int(loc.z)))

    def _do_wander(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        import random
        radius = int(a.get("radius", 8))
        ox, oy, oz = self._floor(actor.location)
        candidates = []
        for _ in range(12):
            dx = random.randint(-radius, radius)
            dz = random.randint(-radius, radius)
            y = walkable_surface(actor.dimension, self.world, ox + dx, oz + dz, oy)
            if y is not None:
                candidates.append((ox + dx, y, oz + dz))
        if not candidates:
            return CompileResult(immediate="附近找不到可走的落脚点")
        dest = min(candidates, key=lambda c: math.dist((ox, oy, oz), c))
        return self._plan_path(actor, dest)

    def _plan_path(self, actor: Any, goal: tuple[int, int, int]) -> CompileResult:
        """纯 Python A* 算出路径，逐格推进。"""
        start = self._floor(actor.location)
        probe = make_probe(actor.dimension, self.world)
        finder = PathFinder(probe, max_nodes=4000)

        # 目标点总是先修正成可站立的 y（地表高度兜底）。
        #
        # LLM 给的 y 经常不是能站的坐标：给的是方块坐标（埋地里）、
        # 给的是悬空位置（山腰 / 悬崖边）、上下坡时给的是坡面上的旧
        # 高度。以前只有在"goal 是实心"时才修正，悬空的 goal 会让
        # A* 永远找不到路——"一旦有上下坡，寻路功能就不能用了"就是
        # 这个：目标在坡上/坡下，y 差几格到十几格，寻路直接报不可达。
        # 所以不管什么情况都先找 (x,z) 处的站立点；找不到（深坑/虚空）
        # 才保留原值交给 A* 自己判断。range 给到 ±12/8，盖住常见陡坡。
        landing = walkable_surface(
            actor.dimension, self.world, goal[0], goal[2], goal[1],
            range_down=12, range_up=8)
        if landing is not None:
            goal = (goal[0], landing, goal[2])
        elif probe(*goal) < 0:
            return CompileResult(ok=False, immediate=f"({goal[0]},{goal[1]},{goal[2]}) 不可到达")

        path = finder.find(start, goal)
        if not path:
            return CompileResult(
                ok=False,
                immediate=f"找不到通往 {goal} 的路径"
                          f"（地形复杂或太远走不过去）。可以试试 tp_warp "
                          f"直接传送到公共传送点，或换个近点的目的地")

        if self.use_real_dig:
            # 有真身体：让原版寻路一次走到终点。假人会真的爬坡、绕树、
            # 跳台阶——比我们逐格传送自然得多，而且中途不会被地形卡住。
            dest = path[-1]
            steps = [Step("walk_to", dest[0], dest[1], dest[2], async_op=True)]
            return CompileResult(
                plan=Plan(label=f"移动({len(path)-1}格)", steps=steps,
                          steps_per_tick=2),
                immediate=f"规划了 {len(path)-1} 步路径",
            )

        steps = [Step("walk_to", x, y, z) for (x, y, z) in path[1:]]
        if not steps:
            return CompileResult(immediate="已经在目标位置了")
        return CompileResult(
            plan=Plan(label=f"移动({len(steps)}格)", steps=steps, steps_per_tick=2),
            immediate=f"规划了 {len(steps)} 步路径",
        )

    # ------------------------------------------------------------------
    # 战斗与生存
    # ------------------------------------------------------------------

    def _do_attack(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        target_name = a.get("target", "")
        count = int(a.get("count", 5))
        # 扫描半径取"能看见"的范围，但**是否够得着**要单独判。
        #
        # 这里踩过一个坑：原先只有 `radius=12` 这一个条件，选出来的
        # 是"最近的敌对生物"，而 `main.js` 的 `doAttack` 只允许 4.5 格内
        # 出手。于是目标站在 8 格外时，插件会信心十足地下发攻击、
        # 然后稳定收到"距离 8.0 格，够不着"——必然失败，而且模型
        # 完全不知道原因（它看到的是"攻击失败"，没有任何线索指向距离）。
        #
        # 现在把距离分档说清楚，让模型能自己决定下一步：
        #   · ≤ ATTACK_REACH   → 直接打
        #   · 够不着           → **不生成计划**，把距离告诉模型
        # 不做"自动走过去"是因为 `EntityHit` 只带了距离、没带坐标，
        # 要寻路得先把坐标补上；在没补上之前，"诚实地报告够不着"
        # 远好过"假装打了"或者"让模型自己猜"。
        hits = [e for e in self.world.scan_entities(actor, radius=12)
                if (e.name == target_name if target_name else e.hostile)]
        if not hits:
            return CompileResult(ok=False, immediate=f"附近没有{'目标' if target_name else '敌对生物'}")

        in_reach = [e for e in hits if e.dist <= self.ATTACK_REACH]
        if not in_reach:
            nearest = min(hits, key=lambda e: e.dist)
            return CompileResult(
                ok=False,
                immediate=(f"{nearest.name} 距离 {nearest.dist:.1f} 格，够不着"
                           f"（攻击范围 {self.ATTACK_REACH:g} 格）。"
                           f"先用 goto 走到它附近再攻击。"),
            )

        nearest = min(in_reach, key=lambda e: e.dist)
        # ⚠️ 必须标 `async_op=True`。
        #
        # 真实身体的攻击是"发请求 → 等回调"两拍的（见
        # `RealBodyActions.attack_real`）：第一拍返回 False 表示
        # "还没结论"，不是失败。不标的话执行器会把它当硬失败，
        # 游标直接推进——于是 never retry，第一拍那必然的 False
        # 会让每次攻击都记成失败。
        #
        # npc 模式下这个标记无害：那条路径是同步的，返回 True 就推进。
        steps = [Step("attack", 0, 0, 0, note=nearest.name, async_op=True)
                 for _ in range(max(1, min(count, 20)))]
        return CompileResult(
            plan=Plan(label=f"攻击{nearest.name}", steps=steps, steps_per_tick=1),
            immediate=f"开始攻击 {nearest.name}（距离 {nearest.dist:.1f}）",
        )

    def _do_flee(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        """朝远离威胁的方向跑。插件自己算方向，模型不用管。"""
        name = a.get("target", "")
        threats = [e for e in self.world.scan_entities(actor, radius=12)
                   if (e.name == name if name else e.hostile)]
        if not threats:
            return CompileResult(immediate="附近没有威胁")
        ax, ay, az = self._floor(actor.location)
        threat = min(threats, key=lambda e: e.dist)

        # 找出所有威胁的质心，然后往反方向走
        positions = []
        for e in threats[:4]:
            for other in actor.dimension.actors:
                if short_id(getattr(other.type, "id", "")) == e.name:
                    positions.append(other.location)
                    break
        if positions:
            cx = sum(p.x for p in positions) / len(positions)
            cz = sum(p.z for p in positions) / len(positions)
        else:
            cx, cz = ax, az

        vx, vz = ax - cx, az - cz
        norm = math.hypot(vx, vz) or 1.0
        fx, fz = int(ax + vx / norm * 12), int(az + vz / norm * 12)
        fy = walkable_surface(actor.dimension, self.world, fx, fz, ay)
        if fy is None:
            return CompileResult(ok=False, immediate="无路可逃")
        return self._plan_path(actor, (fx, fy, fz))

    def _do_eat(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        foods = ("bread", "cooked_beef", "cooked_porkchop", "cooked_chicken",
                 "apple", "carrot", "potato", "beef", "porkchop", "chicken",
                 "mutton", "cooked_mutton", "melon_slice", "sweet_berries")
        inv = self.world.scan_inventory(actor)
        for name, amount in inv:
            if any(f in name for f in foods) and amount > 0:
                return CompileResult(plan=Plan(
                    label=f"进食 {name}",
                    steps=[Step("consume", 0, 0, 0, note=name, async_op=True)],
                    steps_per_tick=1,
                ))
        return CompileResult(ok=False, immediate="背包里没有食物")

    def _do_jump(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        return CompileResult(plan=Plan(label="跳跃", steps=[Step("hop", 0, 0, 0)], steps_per_tick=1))

    def _do_hunt(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        """猎杀动物：找最近的被动生物 → 走过去 → 连续攻击。

        5 天日志里假人从没主动打过动物（attack 只在被打/点名僵尸时出现），
        食物全靠"捡到什么吃什么"——不像真人。 hunt 让"饿了→打猎→生肉
        →熔炉烤熟"成为一条完整的生存链。掉落物是掉在地上的 item 实体，
        假人走过去会自动拾取（和真人一样）。
        """
        target_name = short_id(str(a.get("target", "") or "")).lower()
        count = max(1, min(20, int(a.get("count", 6) or 6)))
        hits = [e for e in self.world.scan_entities(actor, radius=24)
                if e.passive and (e.name == target_name if target_name else True)]
        if not hits:
            want = target_name or "动物"
            return CompileResult(
                ok=False,
                immediate=f"24 格内没有可猎取的{want}。动物多在草地/树林开阔处，"
                          f"可以先 wander 换片区域再找。")
        nearest = min(hits, key=lambda e: e.dist)
        steps: list[Step] = []
        if nearest.dist > self.ATTACK_REACH and nearest.pos is not None:
            # 动物会动，走到它**扫描时**的位置附近即可；攻击步骤执行时
            # 会现场再找同名目标。
            steps.append(Step("walk_to", nearest.pos[0], nearest.pos[1], nearest.pos[2],
                              async_op=self.use_real_dig))
        steps += [Step("attack", 0, 0, 0, note=nearest.name, async_op=True)
                  for _ in range(count)]
        return CompileResult(
            plan=Plan(label=f"猎杀{nearest.name}", steps=steps, steps_per_tick=1),
            immediate=f"开始猎杀 {nearest.name}（距离 {nearest.dist:.0f} 格）")

    def _do_sleep(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        """睡觉：走到附近的床边，挂机到天亮（由插件的 sleep 处理器看守）。

        假人睡不了真觉（没有客户端、引擎的"睡觉"交互不可用），但行为上
        可以一模一样：天黑→回床边→不动→天亮起床。5 天日志里 186 次幻翼
        骚扰的根因就是"从不睡觉"——夜里窝在床边至少让"夜生活"消失。
        """
        beds = self._find_blocks(actor, "bed", 1, radius=16)
        if not beds:
            return CompileResult(
                ok=False,
                immediate="附近 16 格内没有床。先合成一张：需要 3 个羊毛（猎杀 sheep 掉落）"
                          "+ 3 个木板，在**工作台**上合成；然后用 build_floor 放下，再 sleep。"
                          "没时间准备的话，至少躲进有屋顶的地方过夜。")
        bx, by, bz = beds[0]
        steps = [Step("walk_to", bx, by, bz, async_op=self.use_real_dig),
                 Step("sleep", 0, 0, 0, async_op=True, note=f"{bx},{by},{bz}")]
        return CompileResult(
            plan=Plan(label="睡觉到天亮", steps=steps, steps_per_tick=1),
            immediate="走到床边过夜，天亮自动起床")


    def _do_stop(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        ctx["cancel_all"] = True
        return CompileResult(immediate="已停止所有动作")

    # ------------------------------------------------------------------
    # 认知类（不需要执行，直接返回文本）
    # ------------------------------------------------------------------

    def _do_say(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        ctx.setdefault("say", []).append(a.get("message", ""))
        return CompileResult(ok=True)

    def _do_goal(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        ctx["goal"] = a.get("text", "")
        return CompileResult(ok=True)

    def _do_remember(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        ctx.setdefault("notes", []).append(a.get("text", ""))
        return CompileResult(ok=True)

    def _do_note_here(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        label = a.get("label", "")
        x, y, z = self._floor(actor.location)
        self._notes[label] = (x, y, z, label)
        ctx.setdefault("notes", []).append(f"{label} 在 ({x},{y},{z})")
        return CompileResult(immediate=f"已记录 {label} 于 ({x},{y},{z})")

    def _do_task_done(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        ctx["done"] = a.get("summary", "") or "任务完成"
        return CompileResult(ok=True)

    # ------------------------------------------------------------------
    # 合成 / 熔炼 / 容器
    # ------------------------------------------------------------------

    #: 高频合成配方。`shape` 用字符串列表画 3×3 合成格（行优先，' ' 是空），
    #: `mats` 把字符映射成材料（短名，JS 侧按后缀匹配背包，如 "planks"
    #: 匹配任意 oak_planks/birch_planks…）。`count` 是一次合成的产出数。
    #: 内容来自 `knowledge/11_常用生存合成表`。
    CRAFT_RECIPES: dict[str, dict[str, Any]] = {
        "planks": {"result": "planks", "count": 4, "shape": ["L"], "mats": {"L": "log"}},
        "stick": {"result": "stick", "count": 4, "shape": ["P", "P"], "mats": {"P": "planks"}},
        "crafting_table": {"result": "crafting_table", "count": 1, "shape": ["PP", "PP"], "mats": {"P": "planks"}},
        "furnace": {"result": "furnace", "count": 1, "shape": ["SSS", "S S", "SSS"], "mats": {"S": "cobblestone"}},
        "chest": {"result": "chest", "count": 1, "shape": ["PPP", "P P", "PPP"], "mats": {"P": "planks"}},
        "torch": {"result": "torch", "count": 4, "shape": ["C", "S"], "mats": {"C": "coal", "S": "stick"}},
        "wooden_pickaxe": {"result": "wooden_pickaxe", "count": 1, "shape": ["PPP", " S ", " S "], "mats": {"P": "planks", "S": "stick"}},
        "wooden_axe": {"result": "wooden_axe", "count": 1, "shape": ["PP ", "PS ", " S "], "mats": {"P": "planks", "S": "stick"}},
        "wooden_sword": {"result": "wooden_sword", "count": 1, "shape": ["P", "P", "S"], "mats": {"P": "planks", "S": "stick"}},
        "wooden_shovel": {"result": "wooden_shovel", "count": 1, "shape": ["P", "S", "S"], "mats": {"P": "planks", "S": "stick"}},
        "wooden_hoe": {"result": "wooden_hoe", "count": 1, "shape": ["PP", " S", " S"], "mats": {"P": "planks", "S": "stick"}},
        "stone_pickaxe": {"result": "stone_pickaxe", "count": 1, "shape": ["CCC", " S ", " S "], "mats": {"C": "cobblestone", "S": "stick"}},
        "stone_axe": {"result": "stone_axe", "count": 1, "shape": ["CC ", "CS ", " S "], "mats": {"C": "cobblestone", "S": "stick"}},
        "stone_sword": {"result": "stone_sword", "count": 1, "shape": ["C", "C", "S"], "mats": {"C": "cobblestone", "S": "stick"}},
        "iron_pickaxe": {"result": "iron_pickaxe", "count": 1, "shape": ["III", " S ", " S "], "mats": {"I": "iron_ingot", "S": "stick"}},
        "bread": {"result": "bread", "count": 1, "shape": ["WWW"], "mats": {"W": "wheat"}},
        "shield": {"result": "shield", "count": 1, "shape": ["PIP", "PPP", " P "], "mats": {"P": "planks", "I": "iron_ingot"}},
        # ---- 补全工具族（石/铁/铜/钻；木系见上） ----
        "stone_shovel": {"result": "stone_shovel", "count": 1, "shape": ["C", "S", "S"], "mats": {"C": "cobblestone", "S": "stick"}},
        "stone_hoe": {"result": "stone_hoe", "count": 1, "shape": ["CC", " S", " S"], "mats": {"C": "cobblestone", "S": "stick"}},
        "iron_axe": {"result": "iron_axe", "count": 1, "shape": ["II ", "IS ", " S "], "mats": {"I": "iron_ingot", "S": "stick"}},
        "iron_sword": {"result": "iron_sword", "count": 1, "shape": ["I", "I", "S"], "mats": {"I": "iron_ingot", "S": "stick"}},
        "iron_shovel": {"result": "iron_shovel", "count": 1, "shape": ["I", "S", "S"], "mats": {"I": "iron_ingot", "S": "stick"}},
        "iron_hoe": {"result": "iron_hoe", "count": 1, "shape": ["II", " S", " S"], "mats": {"I": "iron_ingot", "S": "stick"}},
        "diamond_pickaxe": {"result": "diamond_pickaxe", "count": 1, "shape": ["DDD", " S ", " S "], "mats": {"D": "diamond", "S": "stick"}},
        "diamond_axe": {"result": "diamond_axe", "count": 1, "shape": ["DD ", "DS ", " S "], "mats": {"D": "diamond", "S": "stick"}},
        "diamond_sword": {"result": "diamond_sword", "count": 1, "shape": ["D", "D", "S"], "mats": {"D": "diamond", "S": "stick"}},
        "diamond_shovel": {"result": "diamond_shovel", "count": 1, "shape": ["D", "S", "S"], "mats": {"D": "diamond", "S": "stick"}},
        "diamond_hoe": {"result": "diamond_hoe", "count": 1, "shape": ["DD", " S", " S"], "mats": {"D": "diamond", "S": "stick"}},
        "copper_pickaxe": {"result": "copper_pickaxe", "count": 1, "shape": ["UUU", " S ", " S "], "mats": {"U": "copper_ingot", "S": "stick"}},
        "copper_axe": {"result": "copper_axe", "count": 1, "shape": ["UU ", "US ", " S "], "mats": {"U": "copper_ingot", "S": "stick"}},
        "copper_sword": {"result": "copper_sword", "count": 1, "shape": ["U", "U", "S"], "mats": {"U": "copper_ingot", "S": "stick"}},
        "copper_shovel": {"result": "copper_shovel", "count": 1, "shape": ["U", "S", "S"], "mats": {"U": "copper_ingot", "S": "stick"}},
        "copper_hoe": {"result": "copper_hoe", "count": 1, "shape": ["UU", " S", " S"], "mats": {"U": "copper_ingot", "S": "stick"}},
        # ---- 护甲四件套（皮/铁/铜/钻） ----
        "leather_helmet": {"result": "leather_helmet", "count": 1, "shape": ["XXX", "X X"], "mats": {"X": "leather"}},
        "leather_chestplate": {"result": "leather_chestplate", "count": 1, "shape": ["X X", "XXX", "XXX"], "mats": {"X": "leather"}},
        "leather_leggings": {"result": "leather_leggings", "count": 1, "shape": ["XXX", "X X", "X X"], "mats": {"X": "leather"}},
        "leather_boots": {"result": "leather_boots", "count": 1, "shape": ["X X", "X X"], "mats": {"X": "leather"}},
        "iron_helmet": {"result": "iron_helmet", "count": 1, "shape": ["XXX", "X X"], "mats": {"X": "iron_ingot"}},
        "iron_chestplate": {"result": "iron_chestplate", "count": 1, "shape": ["X X", "XXX", "XXX"], "mats": {"X": "iron_ingot"}},
        "iron_leggings": {"result": "iron_leggings", "count": 1, "shape": ["XXX", "X X", "X X"], "mats": {"X": "iron_ingot"}},
        "iron_boots": {"result": "iron_boots", "count": 1, "shape": ["X X", "X X"], "mats": {"X": "iron_ingot"}},
        "copper_helmet": {"result": "copper_helmet", "count": 1, "shape": ["XXX", "X X"], "mats": {"X": "copper_ingot"}},
        "copper_chestplate": {"result": "copper_chestplate", "count": 1, "shape": ["X X", "XXX", "XXX"], "mats": {"X": "copper_ingot"}},
        "copper_leggings": {"result": "copper_leggings", "count": 1, "shape": ["XXX", "X X", "X X"], "mats": {"X": "copper_ingot"}},
        "copper_boots": {"result": "copper_boots", "count": 1, "shape": ["X X", "X X"], "mats": {"X": "copper_ingot"}},
        "diamond_helmet": {"result": "diamond_helmet", "count": 1, "shape": ["XXX", "X X"], "mats": {"X": "diamond"}},
        "diamond_chestplate": {"result": "diamond_chestplate", "count": 1, "shape": ["X X", "XXX", "XXX"], "mats": {"X": "diamond"}},
        "diamond_leggings": {"result": "diamond_leggings", "count": 1, "shape": ["XXX", "X X", "X X"], "mats": {"X": "diamond"}},
        "diamond_boots": {"result": "diamond_boots", "count": 1, "shape": ["X X", "X X"], "mats": {"X": "diamond"}},
        # ---- 常用杂项 ----
        "bucket": {"result": "bucket", "count": 1, "shape": ["I I", " I "], "mats": {"I": "iron_ingot"}},
        "flint_and_steel": {"result": "flint_and_steel", "count": 1, "shape": ["I ", " F"], "mats": {"I": "iron_ingot", "F": "flint"}},
        "shears": {"result": "shears", "count": 1, "shape": [" I", "I "], "mats": {"I": "iron_ingot"}},
        "bow": {"result": "bow", "count": 1, "shape": [" TS", "T S", " TS"], "mats": {"T": "stick", "S": "string"}},
        "arrow": {"result": "arrow", "count": 4, "shape": ["F", "S", "R"], "mats": {"F": "flint", "S": "stick", "R": "feather"}},
        "fishing_rod": {"result": "fishing_rod", "count": 1, "shape": ["  S", " TS", "T S"], "mats": {"T": "stick", "S": "string"}},
        "ladder": {"result": "ladder", "count": 3, "shape": ["S S", "SSS", "S S"], "mats": {"S": "stick"}},
        "oak_boat": {"result": "oak_boat", "count": 1, "shape": ["P P", "PPP"], "mats": {"P": "planks"}},
        "bed": {"result": "bed", "count": 1, "shape": ["WWW", "PPP"], "mats": {"W": "wool", "P": "planks"}},
    }

    #: 熔炼配方：输入 → 产出。燃料统一用煤/木炭（JS 侧按后缀匹配）。
    SMELT_RECIPES: dict[str, str] = {
        "iron_ore": "iron_ingot", "raw_iron": "iron_ingot",
        "gold_ore": "gold_ingot", "raw_gold": "gold_ingot",
        "copper_ore": "copper_ingot", "raw_copper": "copper_ingot",
        "sand": "glass", "cobblestone": "stone", "log": "charcoal",
        "oak_log": "charcoal", "chicken": "cooked_chicken",
        "beef": "cooked_beef", "porkchop": "cooked_porkchop",
    }

    def _recipe_slots(self, recipe: dict[str, Any]) -> list[dict[str, Any]]:
        """把 shape 画成 3×3 网格转成 slot 布局（slot 0~8，行优先）。"""
        slots: list[dict[str, Any]] = []
        mats = recipe["mats"]
        for r, row in enumerate(recipe["shape"]):
            for c, ch in enumerate(row):
                if ch == " ":
                    continue
                mat = mats.get(ch)
                if mat:
                    slots.append({"slot": r * 3 + c, "id": mat})
        return slots

    def _recipe_slots_2x2(self, recipe: dict[str, Any]) -> list[dict[str, Any]]:
        """把 ≤2×2 的 shape 画进 2×2 背包合成格（slot 0~3，行优先）。

        JS 侧对 grid=2 是"按材料等价兑换"，slot 位置只是留档；真正起作用
        的是"每个非空格消耗 1 个对应材料"。所以这里保证格子数正确即可。
        """
        slots: list[dict[str, Any]] = []
        mats = recipe["mats"]
        for r, row in enumerate(recipe["shape"]):
            for c, ch in enumerate(row):
                if ch == " ":
                    continue
                mat = mats.get(ch)
                if mat:
                    slots.append({"slot": r * 2 + c, "id": mat})
        return slots

    def _do_craft(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        item = short_id(str(a.get("item", "")))
        recipe = self.CRAFT_RECIPES.get(item)
        if recipe is None:
            return CompileResult(
                ok=False,
                immediate=f"不知道 {item} 的合成配方。支持：{', '.join(self.CRAFT_RECIPES)}")

        # 配方塞得进 2×2 吗？塞得进就在**背包合成格**里做，不需要工作台。
        #
        # 这是整条合成链的启动条件：工作台（2×2）、木板（1×1）、木棍（2×1）、
        # 火把（2×1）本来就能在背包里合出来。旧版一律要求附近有工作台，于是
        # 死循环——"要先有工作台才能合成工作台"，用户实测卡在这里。
        rows = len(recipe["shape"])
        cols = max((len(r) for r in recipe["shape"]), default=0)
        if rows <= 2 and cols <= 2:
            x, y, z = self._floor(actor.location)
            spec = {
                "op": "craft", "grid": 2,
                "slots": self._recipe_slots_2x2(recipe),
                "result": recipe["result"], "count": recipe["count"],
            }
            return CompileResult(plan=Plan(
                label=f"背包合成 {recipe['count']} 个 {recipe['result']}",
                steps=[Step("container", x, y, z, async_op=True,
                            note=json.dumps(spec))]))

        tables = self._find_blocks(actor, "crafting_table", 1)
        if not tables:
            return CompileResult(
                ok=False,
                immediate="这个配方要 3×3 工作台，附近没有。先 craft 一个工作台"
                          "（只要 4 个木板，能在背包里做），或用 build 放一个。")
        tx, ty, tz = tables[0]
        slots = self._recipe_slots(recipe)
        steps = [
            Step("walk_to", tx, ty, tz, async_op=True),
            Step("container", tx, ty, tz, async_op=True,
                 note=json.dumps({"op": "craft", "grid": 3, "slots": slots,
                                  "result": recipe["result"], "count": recipe["count"]})),
        ]
        return CompileResult(plan=Plan(
            label=f"合成 {recipe['count']} 个 {recipe['result']}", steps=steps))

    def _do_smelt(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        item = short_id(str(a.get("item", "")))
        count = max(1, min(64, int(a.get("count", 1))))
        output = self.SMELT_RECIPES.get(item)
        if output is None:
            return CompileResult(
                ok=False,
                immediate=f"不知道 {item} 怎么熔炼。支持：{', '.join(self.SMELT_RECIPES)}")
        furnaces = self._find_blocks(actor, "furnace", 1)
        if not furnaces:
            return CompileResult(
                ok=False,
                immediate="附近没有熔炉。先用 build 放一个熔炉方块，或先合成熔炉（8 圆石）。")
        fx, fy, fz = furnaces[0]
        spec = {"op": "smelt", "input": item, "fuel": "coal", "count": count}
        steps = [
            Step("walk_to", fx, fy, fz, async_op=True),
            Step("container", fx, fy, fz, async_op=True, note=json.dumps(spec)),
        ]
        return CompileResult(plan=Plan(label=f"熔炼 {count} 个 {item} → {output}", steps=steps))

    def _do_store(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        item = short_id(str(a.get("item", "")))
        count = max(1, int(a.get("count", 1)))
        chests = self._find_blocks(actor, "chest", 1)
        if not chests:
            return CompileResult(ok=False, immediate="附近没有箱子。先用 build 放一个箱子方块。")
        cx, cy, cz = chests[0]
        spec = {"op": "put", "item": item, "count": count}
        steps = [
            Step("walk_to", cx, cy, cz, async_op=True),
            Step("container", cx, cy, cz, async_op=True, note=json.dumps(spec)),
        ]
        return CompileResult(plan=Plan(label=f"把 {count} 个 {item} 存进箱子", steps=steps))

    def _do_take(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        item = short_id(str(a.get("item", "")))
        count = max(1, int(a.get("count", 1)))
        chests = self._find_blocks(actor, "chest", 1)
        if not chests:
            return CompileResult(ok=False, immediate="附近没有箱子。")
        cx, cy, cz = chests[0]
        spec = {"op": "take", "item": item, "count": count}
        steps = [
            Step("walk_to", cx, cy, cz, async_op=True),
            Step("container", cx, cy, cz, async_op=True, note=json.dumps(spec)),
        ]
        return CompileResult(plan=Plan(label=f"从箱子取 {count} 个 {item}", steps=steps))

    # ------------------------------------------------------------------
    # 社交与交易
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 传送系统（tpa 申请 / 地标 / 出生点）
    # ------------------------------------------------------------------

    def _do_tp_player(self, actor: Any, a: dict[str, Any],
                      ctx: dict[str, Any]) -> CompileResult:
        """向玩家申请传送（tpa 同意流）：编译成主线程步骤，由插件调
        arc_core 的 send_tpa_request——对方接受后才真的传送并扣费。"""
        player = str(a.get("player", "")).strip()
        if not player:
            return CompileResult(ok=False, immediate="tp_player 需要 player")
        return CompileResult(plan=Plan(
            label=f"向 {player} 申请传送",
            steps=[Step("tp_player", 0, 0, 0, note=player[:32])]))

    def _do_tp_warp(self, actor: Any, a: dict[str, Any],
                    ctx: dict[str, Any]) -> CompileResult:
        """传送到公共传送点（地标）。名字不匹配时执行器会把可用列表
        作为失败原因喂回大脑，模型下一轮自己改对。"""
        name = str(a.get("name", "")).strip()
        if not name:
            return CompileResult(ok=False, immediate="tp_warp 需要 name")
        return CompileResult(plan=Plan(
            label=f"传送到地标 {name}",
            steps=[Step("tp_warp", 0, 0, 0, note=name[:64])]))

    def _do_tp_spawn(self, actor: Any, a: dict[str, Any],
                     ctx: dict[str, Any]) -> CompileResult:
        return CompileResult(plan=Plan(
            label="传送到出生点",
            steps=[Step("tp_spawn", 0, 0, 0)]))

    def _do_give_item(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        """把背包里的物品丢给玩家（交易交付）。

        编译期只做两道便宜的闸：玩家在线、距离 ≤ 8 格。
        **背包里有没有这个货**不在这一层查——那要看行为包回执，
        放到执行阶段让 `drop_item` 步骤等结果，失败原因（"背包里
        没有 xxx"）原样回给模型，它自然会想到先去凑货。
        """
        player = str(a.get("player", "")).strip()
        item = short_id(str(a.get("item", "")))
        count = max(1, int(a.get("count", 1) or 1))
        if not player or not item:
            return CompileResult(ok=False, immediate="give_item 需要 player 和 item")
        if self._goto_player is None:
            return CompileResult(ok=False, immediate="无法查询玩家")
        target = self._goto_player(player)
        if target is None:
            return CompileResult(ok=False, immediate=f"玩家 {player} 不在线")
        dist = math.dist(
            (target.location.x, target.location.y, target.location.z),
            (actor.location.x, actor.location.y, actor.location.z))
        if dist > 8:
            return CompileResult(
                ok=False,
                immediate=f"{player} 在 {dist:.1f} 格外，走近一点再交付")
        spec = {"item": item, "count": count, "to": player}
        step = Step("drop_item", 0, 0, 0, async_op=True, note=json.dumps(spec))
        return CompileResult(
            plan=Plan(label=f"把 {count} 个 {item} 交给 {player}", steps=[step]))

    def _do_pay_money(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        """用金币付款。真正的转账在插件侧的步骤处理器里做（那才有经济桥）。"""
        player = str(a.get("player", "")).strip()
        try:
            amount = round(float(a.get("amount", 0) or 0), 2)
        except (TypeError, ValueError):
            amount = 0.0
        if not player:
            return CompileResult(ok=False, immediate="pay_money 需要 player")
        if amount <= 0:
            return CompileResult(ok=False, immediate="金额必须大于 0")
        step = Step("pay_money", 0, 0, 0, note=f"{player}|{amount}")
        return CompileResult(
            plan=Plan(label=f"付给 {player} {amount:g} 金币", steps=[step]))

    def _do_offer_trade(self, actor: Any, a: dict[str, Any],
                        ctx: dict[str, Any]) -> CompileResult:
        """向玩家发起交易申请（弹窗接受/拒绝）。

        编译期只做参数闸；在线/距离/背包/钱包检查在插件的步骤处理器里
        （那里才有 actor 和经济桥）。申请发出后计划即结束——成败由对方
        玩家的决定决定，和 tpa 一个模式；结果会作为事件喂回大脑。
        """
        player = str(a.get("player", "")).strip()
        mode = str(a.get("mode", "")).strip().lower()
        item = short_id(str(a.get("item", "")).strip())
        try:
            count = int(a.get("count", 1) or 1)
        except (TypeError, ValueError):
            count = 0
        try:
            price = round(float(a.get("price", 0) or 0), 2)
        except (TypeError, ValueError):
            price = 0.0
        if not player:
            return CompileResult(ok=False, immediate="offer_trade 需要 player")
        if mode not in ("sell", "buy"):
            return CompileResult(
                ok=False, immediate="mode 必须是 sell（你出货收钱）或 buy（你出钱收货）")
        if not item:
            return CompileResult(ok=False, immediate="offer_trade 需要 item（英文代码名）")
        count = max(1, min(64, count))
        if price <= 0:
            return CompileResult(ok=False, immediate="price 必须大于 0")
        if self._goto_player is not None and self._goto_player(player) is None:
            return CompileResult(ok=False, immediate=f"玩家 {player} 不在线")
        spec = {"player": player, "mode": mode, "item": item,
                "count": count, "price": price}
        verb = "出售" if mode == "sell" else "收购"
        step = Step("offer_trade", 0, 0, 0, note=json.dumps(spec, ensure_ascii=False))
        return CompileResult(
            plan=Plan(label=f"向 {player} 发起交易（{verb} {count}×{item}）",
                      steps=[step]),
            immediate=f"已向 {player} 发出交易申请，等对方决定")

    def _do_run_command(self, actor: Any, a: dict[str, Any], ctx: dict[str, Any]) -> CompileResult:
        """以假人自己的玩家身份执行一条服务器命令。

        执行在插件侧的步骤处理器里做（那才有 server 和身体引用）。
        这里只做最便宜的闸：非空、去掉可能的斜杠前缀、长度限制。
        权限由服务器自己裁——假人不是 OP，能跑的命令和普通玩家一样。

        gs 命令（枪战小游戏）在**编译期**就按服务器能力拦截：没装枪战
        插件的服上，模型无论重试多少次都必然失败（实测一只假人 51 连败）。
        在闸这里拒绝并告诉它"这服没有"，循环根本不会开始。
        """
        command = str(a.get("command", "")).strip().lstrip("/").strip()
        if not command:
            return CompileResult(ok=False, immediate="run_command 需要 command")
        if len(command) > 120:
            return CompileResult(ok=False, immediate="命令太长")
        first = command.split()[0].lower() if command.split() else ""
        if first == "gs" and not getattr(self, "shooter_available", True):
            return CompileResult(
                ok=False,
                immediate="本服没有装枪战插件，gs 命令不可用。别再尝试了，"
                          "去干点别的（挖矿/打猎/交易都行）。")
        step = Step("bot_command", 0, 0, 0, note=command[:120])
        return CompileResult(
            plan=Plan(label=f"执行命令 {command[:40]}", steps=[step]))

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _find_blocks(self, actor: Any, target: str, cap: int,
                     radius: int = 16,
                     surface_only: bool | None = None) -> list[tuple[int, int, int]]:
        """在半径内找出所有匹配的方块，按距离排序取前 cap 个。

        半径默认给到 16——因为模型喊一次"挖钻石"就应该真的把附近
        能找到的都挖了，而不是因为它站在 10 格外就报告"没找到"。
        搜索本身在服务器主线程上，所以层数做了限制（±6）控制开销。

        `surface_only` 默认取实例属性：为真时只找**表面方块**。
        不加这一刀的话，模型喊一声"挖钻石"就能拿到 20 格外石头里的
        钻石坐标——那是插件在替它开透视。真实玩家得先挖下去。
        找不到时 `_do_mine_vein` 会给"往下挖"的建议，所以关掉全量搜索
        不会让假人卡住，只是把玩法扳回"先挖再找"。

        判定刻意放在 `target in name` **之后**：先花零成本确认"这是我
        要找的方块"，再付 6 次邻居读的钱。反过来的话无效读取会翻 6 倍。
        """
        want_surface = self.surface_only if surface_only is None else surface_only
        dim = actor.dimension
        ox, oy, oz = self._floor(actor.location)
        # 与 scan_blocks 同一个套路：本次搜索内共享的读缓存。
        memo: dict[tuple[int, int, int], str] = {}
        hits: list[tuple[float, tuple[int, int, int]]] = []
        R = radius
        # 旧逻辑垂直只搜 ±6：上下坡（山丘/矿道坡面）差超过 6 格就
        # 什么都找不到——"寻物功能只限同一个 y 坐标上"就是这么来的。
        # 现在垂直跟水平一样搜 ±R，dy 由近及远展开；层数涨上来了，
        # 加个查询预算（按 server 主线程一次搜索几十 ms 的量级封顶），
        # 超预算时漏掉的是最远层，近处的命中不受影响。
        BUDGET = 32000
        looked = 0

        def _candidates():
            for dx in range(-R, R + 1):
                for dz in range(-R, R + 1):
                    if dx * dx + dz * dz > R * R:
                        continue
                    for dist in range(0, R + 1):
                        for dy in ((dist,) if dist == 0 else (dist, -dist)):
                            yield ox + dx, oy + dy, oz + dz

        for x, y, z in _candidates():
            if looked >= BUDGET:
                break
            looked += 1
            key = (x, y, z)
            name = memo.get(key)
            if name is None:
                name = self.world.get_block_name(dim, x, y, z)
                memo[key] = name
            if not name or target not in name:
                continue
            if want_surface and not self.world.is_exposed(
                    dim, x, y, z, memo):
                continue
            hits.append((math.dist((ox, oy, oz), (x, y, z)), (x, y, z)))
        hits.sort(key=lambda h: h[0])
        return [c for _, c in hits[:cap]]

    @property
    def notes(self) -> dict[str, tuple[int, int, int, str]]:
        return dict(self._notes)
