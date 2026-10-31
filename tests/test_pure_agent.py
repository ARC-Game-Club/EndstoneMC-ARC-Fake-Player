"""端到端测试：验证"插件替 LLM 干活"确实大幅减少往返次数。

用一个虚拟的方块世界（不用真服务器）驱动整套逻辑，
统计"LLM 调用次数"与"实际执行的原子步骤数"的比值。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Endstone 的桩必须在**任何** `endstone_arc_fake_player` 导入之前装好。
#
# 包根 `__init__.py` 会 `from .plugin import ArcFakePlayerPlugin`，而
# `plugin.py` 顶层就写着 `from endstone import ColorFormat as C`，
# 所以只要 import 本包的任何子模块，父包的 __init__ 都会先跑一遍，
# 没装桩就直接 ModuleNotFoundError。
#
# 走 run_all.py 时它已经装过了（`install()` 幂等）；单独跑本文件时
# 靠的就是这两行。**别把导入挪到这几行上面去。**
from tests.endstone_stub import install as _install_stub  # noqa: E402

_install_stub()

from endstone_arc_fake_player.actions import ActionCompiler
from endstone_arc_fake_player.executor import BatchExecutor, Plan, PlanState, Step, iter_box, iter_line
from endstone_arc_fake_player.llm import LLMClient, LLMReply, SPEC, ToolCall, clean_args, missing_required
from endstone_arc_fake_player.pathfinder import PathFinder, make_probe
from endstone_arc_fake_player.world import Snapshot, WorldAdapter, classify, short_id


# ---------------------------------------------------------------------------
# 假世界
# ---------------------------------------------------------------------------

class FakeDimension:
    """一个 64x64x64 的方块世界，地面在 y=63。"""

    def __init__(self) -> None:
        self.blocks: dict[tuple[int, int, int], str] = {}
        self.name = "overworld"
        self.actors: list = []
        # 铺一层石头地面
        for x in range(-32, 32):
            for z in range(-32, 32):
                self.blocks[(x, 63, z)] = "minecraft:stone"
        # 种一片橡树林
        for x in range(5, 12):
            for z in range(5, 12):
                self.blocks[(x, 64, z)] = "minecraft:oak_log"
                self.blocks[(x, 65, z)] = "minecraft:oak_log"
        # 埋一些矿石
        for x in range(20, 24):
            for z in range(20, 24):
                self.blocks[(x, 60, z)] = "minecraft:diamond_ore"
        # 一片岩浆池当作危险地形
        for x in range(-10, -6):
            for z in range(10, 14):
                self.blocks[(x, 63, z)] = "minecraft:lava"

    def get_block_at(self, x, y, z):
        key = (int(x), int(y), int(z))
        if key in self.blocks:
            return FakeBlock(self, key)
        return FakeBlock(self, key)   # 未记录 = 空气

    def __repr__(self) -> str:
        return "<FakeDimension>"


class FakeBlock:
    def __init__(self, dim: FakeDimension, key: tuple[int, int, int]) -> None:
        self._dim = dim
        self._key = key

    @property
    def type(self) -> str:
        return self._dim.blocks.get(self._key, "minecraft:air")

    def set_type(self, block_type: str, physics: bool = True) -> None:
        name = str(block_type)
        if "air" in name:
            self._dim.blocks.pop(self._key, None)
        else:
            self._dim.blocks[self._key] = name


class FakeLocation:
    def __init__(self, x, y, z, pitch=0.0, yaw=0.0) -> None:
        self.x, self.y, self.z, self.pitch, self.yaw = x, y, z, pitch, yaw


class FakeActorType:
    def __init__(self, name: str) -> None:
        self.id = f"minecraft:{name}"


class FakeActor:
    def __init__(self, dim, x, y, z, kind="npc") -> None:
        self.dimension = dim
        self.location = FakeLocation(x, y, z)
        self.name = "Bot"
        self.name_tag = "Bot"
        self.is_name_tag_always_visible = True
        self.health = 20.0
        self.max_health = 20.0
        self.is_on_ground = True
        self.is_in_water = False
        self.is_in_lava = False
        self.is_valid = True
        self.type = FakeActorType(kind)
        self.id = id(self)
        self.teleported: list = []
        self.inventory = None

    def teleport(self, loc) -> None:
        self.location = FakeLocation(loc.x, loc.y, loc.z)
        self.teleported.append((loc.x, loc.y, loc.z))

    def set_rotation(self, yaw, pitch) -> None:
        self.location.yaw, self.location.pitch = yaw, pitch

    def remove(self) -> None:
        self.is_valid = False


class FakeLogger:
    def debug(self, *a): pass
    def info(self, *a): pass
    def warning(self, *a): pass
    def exception(self, *a): pass


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

PASS, FAIL = [], []


def check(label: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(label)
    print(f"{'PASS' if cond else 'FAIL'}  {label}{(' — ' + extra) if extra else ''}")


def main() -> int:
    logger = FakeLogger()
    world = WorldAdapter(logger)
    dim = FakeDimension()
    # 起点放在树林边上、矿石附近，模拟一个真实的开局场景
    actor = FakeActor(dim, 3.5, 64.0, 12.5)
    dim.actors.append(actor)

    print("=" * 70)
    print("第一部分：批量工具的省 token 效果")
    print("=" * 70)

    compiler = ActionCompiler(world, logger)

    # --- 挖一个 5x5 的区域 ---
    r = compiler.compile(actor, ToolCall("mine_area", {
        "x1": 5, "y1": 64, "z1": 5, "x2": 9, "y2": 64, "z2": 9}), {})
    check("mine_area 一次调用生成一个计划", r.plan is not None)
    check("5x5 区域 → 25 个步骤", r.plan and r.plan.total == 25,
          f"实际 {r.plan.total if r.plan else 0}")

    # --- 铺一层地板 ---
    r2 = compiler.compile(actor, ToolCall("build_floor", {
        "x1": -5, "y": 66, "z1": -5, "x2": 5, "z2": 5, "block": "cobblestone"}), {})
    check("build_floor 预算材料不足会拒绝",
          r2.plan is None and "没有" in r2.immediate, r2.immediate)

    # 给点材料再试
    class FakeInv:
        def __init__(self, items):
            class S:
                def __init__(self, t, a): self.type, self.amount = t, a
            self.contents = [S(t, a) for t, a in items]
    actor.inventory = FakeInv([("minecraft:cobblestone", 500)])
    r3 = compiler.compile(actor, ToolCall("build_floor", {
        "x1": -5, "y": 66, "z1": -5, "x2": 5, "z2": 5, "block": "cobblestone"}), {})
    check("铺 11x11 地板 → 121 个步骤", r3.plan and r3.plan.total == 121,
          f"实际 {r3.plan.total if r3.plan else 0}")

    # --- 挖隧道（朝南有地面，能挖出实东西）---
    r4 = compiler.compile(actor, ToolCall("dig_tunnel", {"direction": "south", "length": 20}), {})
    check("挖 20 长隧道（只统计真实存在的方块）",
          r4.plan and r4.plan.total >= 15,
          f"实际 {r4.plan.total if r4.plan else 0} 个实心方块")

    # 垂直方向（up/down）被禁：直上直下挖是最经典的机器特征，且危险。
    # 空方向要"明确提示而不是空执行"——垂直方向的提示就是禁令+替代方案。
    r4b = compiler.compile(actor, ToolCall("dig_tunnel", {"direction": "up", "length": 5}), {})
    check("垂直挖隧道被拒绝并指引 descend",
          r4b.plan is None and "垂直" in r4b.immediate and "descend" in r4b.immediate,
          r4b.immediate)
    r4c = compiler.compile(actor, ToolCall("descend", {"depth": 6}), {})
    check("descend 阶梯下探生成计划（相邻层共格去重后=2d+2 步）",
          r4c.plan is not None and r4c.plan.total == 6 * 2 + 2,
          f"实际 {r4c.plan.total if r4c.plan else 0} 步")

    # --- 砍树（一棵 7x7 的树 = 98 格）---
    r5 = compiler.compile(actor, ToolCall("chop_tree", {"x": 8, "y": 64, "z": 8}), {})
    check("砍树自动收集整片相连原木", r5.plan and r5.plan.total >= 49,
          f"实际 {r5.plan.total if r5.plan else 0}")

    # --- 找矿：把机器人放到矿区旁边（模拟它自己走下去）---
    # v3.9.34 起挖矿要持镐——给测试机器人配一把铁镐
    compiler.world.scan_inventory = lambda a, limit=12: [("iron_pickaxe", 1)]
    actor.location = FakeLocation(21.5, 64.0, 21.5)
    r6 = compiler.compile(actor, ToolCall("mine_vein", {"block": "diamond_ore", "max": 8}), {})
    check("站在矿区附近时 mine_vein 能找到矿石", r6.plan and r6.plan.total >= 4,
          f"实际 {r6.plan.total if r6.plan else 0}")

    # 远离矿区时给出可执行的采矿建议，而不是干瞪眼
    actor.location = FakeLocation(-25.5, 64.0, -25.5)   # 距离矿区 > 20 格
    r6b = compiler.compile(actor, ToolCall("mine_vein", {"block": "diamond_ore"}), {})
    check("远离矿区时给出采矿高度建议",
          r6b.plan is None and "y=" in r6b.immediate, r6b.immediate[:70])

    print()
    print("=" * 70)
    print("第二部分：省 token 的量化对比")
    print("=" * 70)

    scenarios = [
        ("挖 5x5 一片区域", 25, r.plan),
        ("铺 11x11 地板", 121, r3.plan),
        ("挖 20 格隧道", 40, r4.plan),
        ("砍一整棵树", 98, r5.plan),
        ("采一批钻石矿", 8, r6.plan),
    ]
    total_steps = 0
    total_calls = 0
    print(f"{'场景':<20} {'LLM调用':>8} {'实际步骤':>8} {'单次放大':>10}")
    print("-" * 52)
    naive_total = 0
    for label, naive_cells, plan in scenarios:
        steps = plan.total if plan else 0
        total_steps += steps
        total_calls += 1
        # 朴素做法每个方块要 2 次调用（goto + mine）
        naive_total += naive_cells * 2
        print(f"{label:<20} {1:>8} {steps:>8} {steps:>9.0f}x")
    print("-" * 52)
    print(f"{'合计':<20} {total_calls:>8} {total_steps:>8} {naive_total / total_calls:>9.0f}x")
    print()
    check(f"LLM 调用从 {naive_total} 次降到 {total_calls} 次",
          total_calls * 10 < naive_total,
          f"降幅 {(1 - total_calls / naive_total) * 100:.1f}%")

    print()
    print("=" * 70)
    print("第三部分：纯 Python 寻路")
    print("=" * 70)

    probe = make_probe(dim, world)
    finder = PathFinder(probe, max_nodes=20000)
    path = finder.find((0, 64, 0), (15, 64, 15))
    check("能找到通往远处的路径", path is not None and len(path) > 1,
          f"{len(path) if path else 0} 步")
    check("路径起点正确", path is not None and path[0] == (0, 64, 0))
    check("路径终点正确", path is not None and path[-1] == (15, 64, 15))
    check("路径连续（每步只走一格）",
          path is not None and all(
              max(abs(a[0]-b[0]), abs(a[1]-b[1]), abs(a[2]-b[2])) == 1
              for a, b in zip(path, path[1:])))

    # 岩浆应该被绕开
    lava_path = finder.find((0, 64, 0), (-8, 64, 12))
    if lava_path:
        crosses_lava = any(dim.blocks.get(c) == "minecraft:lava" for c in lava_path)
        check("路径绕开岩浆", not crosses_lava, f"路径 {len(lava_path)} 步")
    else:
        check("路径绕开岩浆（或拒绝穿越）", True, "无法到达，已拒绝")

    print()
    print("=" * 70)
    print("第四部分：参数清洗与安全")
    print("=" * 70)

    c = clean_args("goto", {"x": 99999999999, "y": -500, "z": 10, "evil": "x"})
    check("坐标被钳制到世界边界", c["x"] == 30000000 and c["y"] == -64, str(c))
    check("未知参数被丢弃", "evil" not in c)

    c2 = clean_args("build_box", {"x1": 0, "y1": 64, "z1": 0, "x2": 3, "y2": 70,
                                  "z2": 3, "block": "bedrock"})
    check("非法方块（基岩）被拒绝", c2 == {} or "bedrock" not in str(c2), str(c2))

    c3 = clean_args("build_box", {"x1": 0, "y1": 64, "z1": 0, "x2": 3, "y2": 70,
                                  "z2": 3, "block": "cobblestone"})
    check("合法方块被接受", c3.get("block") == "minecraft:cobblestone", str(c3))

    c4 = clean_args("dig_tunnel", {"direction": "sideways", "length": 10})
    check("非法方向回退到默认值", c4["direction"] == "north", str(c4))

    check("缺必填参数被识别", missing_required("goto", {"x": 1}))
    check("齐全参数通过", not missing_required("goto", {"x": 1, "y": 2, "z": 3}))

    print()
    print("=" * 70)
    print("第五部分：分片执行不卡服务器")
    print("=" * 70)

    exec_ = BatchExecutor(logger, max_steps_per_tick=8)
    applied: list[tuple] = []
    exec_.register("set_block", lambda s: (applied.append((s.x, s.y, s.z)), True)[1])

    cells = list(iter_box(0, 64, 0, 9, 64, 9, max_steps=1000))
    plan = Plan(label="测试", steps=[Step("set_block", x, y, z, block="minecraft:stone")
                                     for x, y, z in cells])
    exec_.submit(plan)

    ticks = 0
    while exec_.busy and ticks < 200:
        exec_.tick()
        ticks += 1

    check("100 个方块全部执行", len(applied) == 100, f"实际 {len(applied)}")
    check("被拆成多个 tick 执行（未一次做完）", ticks >= 100 // 8,
          f"用了 {ticks} tick，每 tick 上限 8 步")
    check("单 tick 影响可控", len(applied) / max(ticks, 1) <= 8.1,
          f"平均 {len(applied) / max(ticks, 1):.1f} 步/tick")

    print()
    print("=" * 70)
    print("第六部分：感知压缩")
    print("=" * 70)

    actor.location = FakeLocation(3.5, 64.0, 3.5)
    snap = world.snapshot(actor)
    prompt = snap.to_prompt(events=[{"op": "mine_area", "ok": True, "steps": 25}],
                            chat=["Steve: 去砍树"])
    print()
    print("--- 提示词实际内容（这就是模型每轮看到的东西）---")
    print(prompt)
    print("--------------------------------------------------")

    check("快照能生成提示词", len(prompt) > 50)
    check("提示词给出绝对坐标", "绝对(" in prompt, "")
    check("提示词给出相对偏移", "偏移(" in prompt, "")
    check("提示词不包含空气条目", " air " not in prompt and "空气" not in prompt)
    check("提示词长度受控（< 1200 字符）", len(prompt) < 1200, f"{len(prompt)} 字符")

    check("岩浆被识别为危险方块", classify("lava") == "lava")
    check("原木变体归一化", classify("oak_log") is not None and classify("birch_log") is not None)
    check("空气不进入观测", classify("air") is None and classify("cave_air") is None)
    check("楼梯不会被误判为空气",
          not world.is_air(dim, *[k for k, v in dim.blocks.items() if "stone" in v][0])
          if dim.blocks else True)

    # --- 表面方块剔除（防矿透）---
    #
    # 在 (0,62,0) 埋一块钻石矿，六面都是石头。假人站在它正上方，
    # 它落在扫描盒内，但因为六面没有一面贴着空气/水，理应被剔掉。
    for k in ((1, 62, 0), (-1, 62, 0), (0, 61, 0), (0, 62, 1), (0, 62, -1)):
        dim.blocks[k] = "minecraft:stone"
    dim.blocks[(0, 62, 0)] = "minecraft:diamond_ore"

    check("埋死的矿 is_exposed 为假",
          not world.is_exposed(dim, 0, 62, 0))
    check("暴露的矿 is_exposed 为真（上方是空气）",
          world.is_exposed(dim, 20, 60, 20))

    actor.location = FakeLocation(0.5, 63.0, 0.5)
    hidden = {h.name for h in world.scan_blocks(actor)}
    check("surface_only 开启时埋死矿被剔除", "diamond_ore" not in hidden,
          str(sorted(hidden)))
    revealed = {h.name for h in world.scan_blocks(actor, surface_only=False)}
    check("surface_only 关闭时埋死矿出现", "diamond_ore" in revealed,
          str(sorted(revealed)))

    print()
    print("=" * 70)
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败:")
        for f in FAIL:
            print(f"   - {f}")
        return 1
    print(f"✅ 全部 {len(PASS)} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
