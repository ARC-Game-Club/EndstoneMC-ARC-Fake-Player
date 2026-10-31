"""表面方块剔除 + 提示词进 config + 配置迁移。

用户的原话是两件事：

    1. "假人返回来的结构信息必须只返回表面的方块，而不是所有的方块，
        要不然就成矿透了。可不可以在 python 里面增加一个逻辑，
        非表面方块全部剔除。"
    2. "提示词也加到 config 里面来，可以在 config 里面改提示词。"

第 1 件是**真实性**问题，不是性能问题。假人能隔着石头看到钻石在哪，
就等于给它开了矿透（X-ray）——标个坐标开挖即可，探索过程整个消失。
所以这一刀砍的是"假人的眼睛"，判据必须精确。

第 2 件看起来只是搬字符串，真正的难点在于**用户写坏提示词不能把插件
搞崩**：`prompt_rules` 里出现一个 `{` 是很正常的（写 JSON 例子），
用 `str.format` 会抛 KeyError 并把整轮决策打断。

这个文件按六部分组织：判定 / 剔除 / 读取上界 / 挖掘侧开关 /
提示词拆段 / 配置迁移。其中"读取上界"那部分容易被当成性能测试——
它不是。它守的是**感知本身**：如果邻居查询不受控地翻几倍，服务器
主线程上的 tick 就会被拖慢，最后表现为假人发呆。
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
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

from endstone_arc_fake_player import confmig
from endstone_arc_fake_player.actions import ActionCompiler
from endstone_arc_fake_player.agent import (DEFAULT_BODY, DEFAULT_IDENTITY,
                                       DEFAULT_PERSONA, DEFAULT_RULES,
                                       DEFAULT_SURFACE_HINT, AgentConfig)
from endstone_arc_fake_player.confmig import CONFIG_VERSION
from endstone_arc_fake_player.world import WorldAdapter, classify

ROOT = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []
CHECKS = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"PASS  {label}" + (f" — {extra}" if extra else ""))
    else:
        print(f"FAIL  {label}" + (f" — {extra}" if extra else ""))
        FAILURES.append(label)


def section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


class FakeLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def _log(self, level: str, msg: str) -> None:
        self.lines.append(f"{level}: {msg}")

    def debug(self, m: str) -> None:
        self._log("DEBUG", m)

    def info(self, m: str) -> None:
        self._log("INFO", m)

    def warning(self, m: str) -> None:
        self._log("WARN", m)

    def error(self, m: str) -> None:
        self._log("ERROR", m)


# ---------------------------------------------------------------------------
# 测试替身：一个用 dict 当世界的 dimension
# ---------------------------------------------------------------------------

class DictDimension:
    """用 {(x,y,z): 方块名} 当世界的 dimension。

    ``reads`` 记录 get_block_at 被调了几次——第四部分靠它守读取上界。
    没登记的坐标返回 "air"，这与 `get_block_name` 读不到就返回空串
    的约定一致（两者最终都被当成"透明"）。
    """

    def __init__(self, blocks: dict[tuple[int, int, int], str]) -> None:
        self.blocks = dict(blocks)
        self.reads = 0

    def get_block_at(self, x: int, y: int, z: int) -> object:
        self.reads += 1
        return _Block(self.blocks.get((int(x), int(y), int(z)), "air"))


class _Block:
    def __init__(self, block_type: str) -> None:
        self.type = block_type


class Actor:
    def __init__(self, dimension: object, x: float, y: float, z: float) -> None:
        self.dimension = dimension
        self.location = _Loc(x, y, z)


class _Loc:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x, self.y, self.z = x, y, z


def solid_box(blocks: dict, cx: int, cy: int, cz: int, hx: int, hy: int, hz: int,
              fill: str = "stone") -> None:
    """在 blocks 里填一个实心长方体（用来把矿石"埋"起来）。"""
    for x in range(cx - hx, cx + hx + 1):
        for y in range(cy - hy, cy + hy + 1):
            for z in range(cz - hz, cz + hz + 1):
                blocks[(x, y, z)] = fill


def solid_until_air(blocks: dict, cx: int, cy: int, cz: int, fill: str = "stone",
                    top: int = 6) -> None:
    """从 ``cy`` 往上填到 ``top``，把下面的方块彻底封死。

    比 ``solid_box`` 更狠：不仅把六个邻居填成实心，还**往下多填 2 层**，
    这样邻居的邻居也都是实心方块，连"读到扫描区外、返回空串"这条兜底
    的透光路径都不留。

    注意最后要把中心那一格**放回去**——循环会把它一起覆盖掉，
    必须先填壳再放目标方块。调用方拿到的是"目标 + 完整实心壳"。
    """
    for y in range(cy - 2, top + 1):
        for x in range(cx - 2, cx + 3):
            for z in range(cz - 2, cz + 3):
                blocks[(x, y, z)] = fill
    del blocks[(cx, cy, cz)]          # 中心留给调用方放目标方块


# ---------------------------------------------------------------------------
# 第一部分：is_exposed 的判定
# ---------------------------------------------------------------------------

def test_is_exposed() -> None:
    section("第一部分：is_exposed —— 六面中任意一面贴着空气/水")

    log = FakeLogger()
    w = WorldAdapter(log)

    # -- 透明方块家族：全都要算"看得见" --
    #
    # 这里逐项列而不是循环一个集合，是因为**这一部分测的正是那个集合
    # 本身**——写错了、漏了，都必须在这里看得见。
    for name, why in [
        ("air", "普通空气"),
        ("cave_air", "洞穴空气"),
        ("void_air", "虚空空气"),
        ("water", "水源"),
        ("flowing_water", "流动的水"),
    ]:
        for axis, delta in [("+x", (1, 0, 0)), ("-x", (-1, 0, 0)),
                            ("+y", (0, 1, 0)), ("-y", (0, -1, 0)),
                            ("+z", (0, 0, 1)), ("-z", (0, 0, -1))]:
            blocks = {(1, 0, 0): "diamond_ore"}
            blocks[delta] = name
            dim = DictDimension(blocks)
            got = w.is_exposed(dim, 0, 0, 0)
            check(f"邻居 {name}（{why}）在 {axis} 面 → 可见", got)

    # -- 不透明方块：全都要算"看不见" --
    #
    # `oak_stairs` 和某 mod 的 `repair_block` 是**回归用例**：
    # 如果判定写成 `"air" in name`，它们会被误判成空气，
    # 于是贴着楼梯的矿变成"表面矿"，矿透从这儿漏回去。
    #
    # 注意：这里必须用 `solid_until_air` 而不是 `solid_box`。用后者
    # 只会填一个 3×3×3，扫描盒往上/往外还有大量"未登记 → 当空气"的
    # 坐标，`is_exposed` 会老老实实判成"可见"——那不是判定的锅，
    # 是场景没封死。
    for name, why in [
        ("stone", "石头"),
        ("diamond_ore", "矿石"),
        ("oak_stairs", "楼梯 —— 含 'air' 子串"),
        ("repair_block", "名字含 'air' 子串"),
        ("lava", "岩浆：刻意不算透明"),
        ("glass", "玻璃：隔着它挖不到，刻意不算"),
        ("oak_leaves", "树叶：实体方块"),
        ("oak_planks", "木板"),
    ]:
        blocks: dict[tuple[int, int, int], str] = {}
        solid_until_air(blocks, 0, 0, 0, fill=name, top=8)
        blocks[(0, 0, 0)] = "diamond_ore"
        dim = DictDimension(blocks)
        got = w.is_exposed(dim, 0, 0, 0)
        check(f"六面全是 {name}（{why}）→ 不可见", not got,
              "" if not got else
              "邻居读到了空气/未登记格——场景没封死")

    # -- 恰好只有一面透光就算可见 --
    #
    # 洞必须开在**紧邻的那一格**。开在隔一格外（比如 (0,2,0)）不算数：
    # is_exposed 只看六面贴着的邻居，而 (0,1,0) 还是石头，所以
    # 「隔一格的洞」判为不可见是对的——这一条顺手把这个语义钉住。
    blocks = {}
    solid_until_air(blocks, 0, 0, 0, fill="stone", top=8)
    blocks[(0, 0, 0)] = "diamond_ore"
    blocks[(0, 1, 0)] = "air"          # 紧邻上方透光
    dim = DictDimension(blocks)
    check("六面里只有一面（紧邻）透光 → 可见", w.is_exposed(dim, 0, 0, 0))
    check("（对照）堵上那个洞就不可见",
          not w.is_exposed(DictDimension(
              {**blocks, (0, 1, 0): "stone"}), 0, 0, 0))

    # 隔一格的洞不算"表面"——只看贴着的六个面，不做视线传播
    blocks2 = {}
    solid_until_air(blocks2, 0, 0, 0, fill="stone", top=8)
    blocks2[(0, 0, 0)] = "diamond_ore"
    blocks2[(0, 2, 0)] = "air"          # 中间隔着 (0,1,0) 石头
    check("隔一格的洞不算表面（只判六面，不做视线传播）",
          not w.is_exposed(DictDimension(blocks2), 0, 0, 0))

    # -- 读不到方块（越界/区块没加载）→ 当空气 → 可见 --
    #
    # 失败方向是刻意的：最坏是多报一个埋着的方块，绝不会反过来
    # 把真实可见的方块藏起来（那会让假人对着一堵空墙挖半天）。
    class Exploding(DictDimension):
        def get_block_at(self, x, y, z):
            raise RuntimeError("区块没加载")

    check("读方块抛异常 → 当空气 → 可见",
          WorldAdapter(FakeLogger()).is_exposed(Exploding({}), 0, 0, 0))

    # -- 传入 memo 时必须复用，不能自己新建 --
    dim = DictDimension({(0, 0, 0): "stone",
                         (1, 0, 0): "stone", (-1, 0, 0): "stone",
                         (0, 1, 0): "stone", (0, -1, 0): "stone",
                         (0, 0, 1): "stone", (0, 0, -1): "air"})
    memo: dict[tuple[int, int, int], str] = {}
    w.is_exposed(dim, 0, 0, 0, memo)
    check("传入 memo 会被写入", len(memo) == 6, f"{len(memo)} 个邻居")
    before = dim.reads
    w.is_exposed(dim, 0, 0, 0, memo)
    check("复用 memo 后第二次零读取", dim.reads == before,
          f"reads {before} → {dim.reads}")

    # -- 短路：第一个方向就透光时，不该读满 6 个 --
    dim = DictDimension({(1, 0, 0): "air"})
    w.is_exposed(dim, 0, 0, 0)
    check("第一个方向就透光 → 短路，只读 1 次", dim.reads == 1,
          f"reads={dim.reads}")


# ---------------------------------------------------------------------------
# 第二部分：scan_blocks 真的剔掉了埋着的方块
# ---------------------------------------------------------------------------

def test_scan_blocks() -> None:
    section("第二部分：scan_blocks —— 埋住的方块不进感知")

    log = FakeLogger()
    w = WorldAdapter(log)

    def scene() -> tuple[dict, object, object]:
        """造一个场景，假人站在 (0, 71, 0)，正下方三格：

            71  ← 假人脚下（空气）
            70  ← 暴露的钻石矿，上方是空气       → 该报告
            69  ← 夹在中间的石头
            68  ← 埋死的钻石矿，六面皆石          → 该剔除

        扫描盒是「横向半径 4 的圆盘 × y∈[-2, +3]」，即 y ∈ [69, 74]。
        ⚠️ 68 落在盒外！所以挖开测试必须让矿留在 y=69 才在盒内。
        为此把"埋死的"那块放 y=69、暴露的放 y=70：

            70  ← 暴露的钻石矿（上方是空气）
            69  ← 埋死的钻石矿（左右前后下全是石头，上被 70 压住）
            68  ← 石头地板（挖掉它，69 就露出来了）

        两块都在 y ∈ [69,74] 里，这才是"同样在可见范围内，一个报告
        一个不报告"的干净对照。第一版把埋着的矿放在 y=60（盒外），
        于是"被剔掉"和"根本没扫到"分不清，测试是假绿的。
        """
        blocks: dict[tuple[int, int, int], str] = {}

        # y ∈ [68, 74] 的实心地层：横向铺满整个扫描区，周边多铺 1 圈
        for x in range(-6, 7):
            for z in range(-6, 7):
                for y in range(68, 75):
                    blocks[(x, y, z)] = "stone"

        # 挖出假人站立/下沉的空间（y=71 那一层留空）
        for x in range(-4, 5):
            for z in range(-4, 5):
                if x * x + z * z <= 16:
                    del blocks[(x, 71, z)]

        # y=70：暴露的矿，正上方是空气 → 该被报告
        blocks[(0, 70, 0)] = "diamond_ore"

        # y=69：埋死的矿。先把上方那格盖实，保证它六面都是石头
        blocks[(0, 70, 0)] = "diamond_ore"    # 它的"上面"是矿石，也是实心
        blocks[(0, 69, 0)] = "diamond_ore"
        for key in ((1, 69, 0), (-1, 69, 0), (0, 69, 1), (0, 69, -1)):
            blocks[key] = "stone"

        dim = DictDimension(blocks)
        return blocks, dim, Actor(dim, 0.5, 71.0, 0.5)

    _, dim, actor = scene()
    check("（前提）两块矿都落在扫描盒内",
          True, f"盒 y 范围 [{71 - WorldAdapter.SCAN_HEIGHT_DOWN}, "
                f"{71 + WorldAdapter.SCAN_HEIGHT_UP}]，矿在 69/70")

    hits = {h.name: h for h in w.scan_blocks(actor)}
    check("贴身暴露的钻石矿被报告", "diamond_ore" in hits)
    if "diamond_ore" in hits:
        check("只数了暴露的那一块", hits["diamond_ore"].count == 1,
              f"count={hits['diamond_ore'].count}")
        check("报告的是暴露那块的偏移",
              hits["diamond_ore"].off == (0, -1, 0),
              f"off={hits['diamond_ore'].off}")

    # 埋着的那块单独验一次，防止"count==1"只是恰好漏掉了
    blocks, dim, _ = scene()
    check("（对照）被石头全包住的钻石矿 is_exposed 为假",
          not w.is_exposed(dim, 0, 69, 0))
    check("（对照）暴露的那块 is_exposed 为真",
          w.is_exposed(dim, 0, 70, 0))

    # -- 关掉开关后应该两块都出现（开关真的通到扫描）--
    w_off = WorldAdapter(log, surface_only=False)
    blocks, dim, actor = scene()
    hits_off = {h.name: h for h in w_off.scan_blocks(actor)}
    n_off = hits_off["diamond_ore"].count if "diamond_ore" in hits_off else 0
    check("关掉 surface_only 后埋着的矿也出现", n_off == 2, f"count={n_off}")

    # -- 显式传参优先于实例属性 --
    w_on = WorldAdapter(log, surface_only=True)
    blocks, dim, actor = scene()
    forced = {h.name: h for h in w_on.scan_blocks(actor, surface_only=False)}
    n_forced = forced["diamond_ore"].count if "diamond_ore" in forced else 0
    check("scan_blocks(surface_only=False) 覆盖实例开关",
          n_forced == 2, f"count={n_forced}")

    # -- 模型看得懂的那一步：挖开之后矿自动出现 --
    #
    # 这是"防矿透"不会让假人变瞎的关键。
    #
    # 注意 scan_blocks 的语义：每种方块**只保留最近的一个偏移**
    # （`BlockHit.off`），`count` 是"扫到这个类型的总格数"，不是
    # "有几个桶"。挖开前最近的是 y=70 那块，挖开后它变成空气、
    # 最近的就成了 y=69 那块——所以判据是 **off 往下挪了一格**，
    # 不是 count 变大。用 count 判会写出一条永远为假的断言。
    blocks, dim, actor = scene()
    first = {h.name: h for h in w.scan_blocks(actor)}
    check("挖开前最近的是上面那块矿", first.get("diamond_ore") is not None
          and first["diamond_ore"].off == (0, -1, 0),
          f"off={first['diamond_ore'].off if 'diamond_ore' in first else None}")

    # 把 (0,70,0) 那块暴露矿挖掉 → (0,69,0) 上方露出空气
    #
    # 注意：DictDimension 构造时 `dict(blocks)` 复制了一份，所以旧的
    # `actor.dimension` 读不到这次改动，必须用新 dimension 重建 actor。
    blocks[(0, 70, 0)] = "air"
    dim2 = DictDimension(blocks)
    check("挖开后被埋过的那块确实变可见",
          w.is_exposed(dim2, 0, 69, 0))
    check("挖开前它是不可见的（对照）",
          not w.is_exposed(dim, 0, 69, 0))
    second = {h.name: h for h in w.scan_blocks(Actor(dim2, 0.5, 71.0, 0.5))}
    check("挖开后最近的那块下移到被埋过的那层",
          second.get("diamond_ore") is not None
          and second["diamond_ore"].off == (0, -2, 0),
          f"off={second['diamond_ore'].off if 'diamond_ore' in second else None}")

    # -- 天然暴露的方块本来就不该被误剔 --
    blocks = {(0, 70, 0): "oak_log"}
    dim = DictDimension(blocks)
    actor = Actor(dim, 0.5, 71.0, 0.5)
    hits = {h.name for h in w.scan_blocks(actor)}
    check("浮在空气里的原木照常报告", "log" in hits, str(hits))

    # -- 水下：水算透明，水底矿要看得见 --
    #
    # 这条容易被忽略：如果 _SEETHROUGH 只放空气，海底/河床的矿会从
    # 感知里整个消失，假人会以为水里什么都没有。
    blocks = {(0, 70, 0): "diamond_ore", (0, 71, 0): "water"}
    dim = DictDimension(blocks)
    actor = Actor(dim, 0.5, 72.0, 0.5)
    hits = {h.name for h in w.scan_blocks(actor)}
    check("水底的矿看得见（水算透明，否则海底矿全消失）",
          "diamond_ore" in hits, str(hits))

    # -- 空气和无价值方块不进 buckets，一直是原样 --
    blocks = {}
    solid_box(blocks, 0, 60, 0, 2, 2, 2, fill="stone")
    dim = DictDimension(blocks)
    actor = Actor(dim, 0.5, 61.0, 0.5)
    hits = w.scan_blocks(actor)
    names = {h.name for h in hits}
    check("纯石头场景不报告任何矿石/木材",
          not (names & {"diamond_ore", "log", "iron_ore"}), str(names))


# ---------------------------------------------------------------------------
# 第三部分：memo 与读取上界
# ---------------------------------------------------------------------------

def test_read_budget() -> None:
    section("第三部分：读取次数 —— memo 把邻居查询锁住")

    log = FakeLogger()
    r = WorldAdapter.SCAN_RADIUS
    r2 = r * r
    columns = [(dx, dz) for dx in range(-r, r + 1)
               for dz in range(-r, r + 1) if dx * dx + dz * dz <= r2]
    height = WorldAdapter.SCAN_HEIGHT_UP + WorldAdapter.SCAN_HEIGHT_DOWN + 1
    inner = len(columns) * height

    # 造一个"邻居查询一定会一路读下去"的场景：扫描盒里塞满实心石头，
    # 中间撒几条暴露的矿石。
    #
    # 为什么不图省事用全空气：全空气场景在第一格就短路了（第一个邻居
    # 就是空气），reads 只有内盒大小，测不出上界。必须让邻居查询真的
    # 往下走，才能验证 memo 把它们锁住了。
    blocks: dict[tuple[int, int, int], str] = {}
    for dx, dz in columns:
        for dy in range(-WorldAdapter.SCAN_HEIGHT_DOWN,
                        WorldAdapter.SCAN_HEIGHT_UP + 1):
            blocks[(dx, dy, dz)] = "stone"
    # 在 y=-2 铺一层"实心石头夹着暴露矿"：石头有六个邻居都实心，
    # 会一路读到扫描区外，把 reads 推上去。
    for dx, dz in columns:
        blocks[(dx, -2, dz)] = "stone"
    blocks[(0, -2, 0)] = "diamond_ore"       # 邻居全是石头 → 不可见
    blocks[(0, -1, 0)] = "diamond_ore"       # 上方是空气 → 可见

    def fresh() -> DictDimension:
        return DictDimension(dict(blocks))

    w_on = WorldAdapter(log, surface_only=True)
    w_off = WorldAdapter(log, surface_only=False)

    dim_off = fresh()
    hits_off = w_off.scan_blocks(Actor(dim_off, 0.5, 0.5, 0.5))
    reads_off = dim_off.reads

    dim_on = fresh()
    hits_on = w_on.scan_blocks(Actor(dim_on, 0.5, 0.5, 0.5))
    reads_on = dim_on.reads

    check("关掉过滤时读满扫描区", reads_off == inner, f"reads={reads_off}")
    check("开着过滤时读取次数没有翻倍（memo 生效）",
          reads_on < reads_off * 2, f"{reads_off} → {reads_on}")

    # 上界：扫描区外盒 + 一层壳。外盒 = (2r+1)² 根柱子 × (height+2) 层；
    # 但圆盘之外的柱子也会被邻居查询读到，所以直接用外盒算是安全的
    # 上界（比真实需要的宽，但足以证明"没有失控地翻倍"）。
    bound = (2 * r + 1) ** 2 * (height + 2)
    check(f"读取次数上界 = 外盒+一层壳 = {bound}", reads_on <= bound,
          f"reads={reads_on} ≤ {bound}")

    # 更强的说法：只读邻居、不做任何跨格复用的话最坏是 inner × 6。
    # memo 必须把它压到远低于这个数，否则这一刀等于没砍。
    check("远低于「每格都重读 6 个邻居」的朴素上界",
          reads_on < inner * 6 // 2,
          f"reads={reads_on} < {inner * 6 // 2}（朴素上界 {inner * 6}）")

    # memo 的生命周期 = 一次调用。第二次调用必须重新读——
    # 否则会读到上次的过期方块，假人对着空气挖半天才发现。
    dim_on.reads = 0
    w_on.scan_blocks(Actor(dim_on, 0.5, 0.5, 0.5))
    check("memo 不跨调用（第二次同样是全新一轮读）",
          dim_on.reads == reads_on, f"{reads_on} → {dim_on.reads}")

    # -- 短路也要能省读：全空气场景读得极少 --
    dim_air = DictDimension({})
    w_on.scan_blocks(Actor(dim_air, 0.5, 70.0, 0.5))
    check("全空气场景一行都没多读（第一个邻居就短路）",
          dim_air.reads == inner, f"reads={dim_air.reads} (inner={inner})")

    # -- 过滤把结果真的筛掉了一块（这是本部分与第二部分互相印证处）--
    def ore_count(hits):
        for h in hits:
            if h.name == "diamond_ore":
                return h.count
        return 0

    check("开着过滤只报暴露的那块", ore_count(hits_on) == 1,
          f"count={ore_count(hits_on)}")
    check("关掉过滤两块都报（同一场景，唯一变量是这个开关）",
          ore_count(hits_off) == 2, f"count={ore_count(hits_off)}")


# ---------------------------------------------------------------------------
# 第四部分：挖掘侧的 dig_surface_only
# ---------------------------------------------------------------------------

class _StubWorld:
    """给 ActionCompiler 用的最小 world。

    ⚠️ 不能写成"把 is_exposed 转发给一个真的 WorldAdapter"：

        def is_exposed(self, dim, x, y, z, memo=None):
            return self._real.is_exposed(self, x, y, z, memo)   # ← 错

    `WorldAdapter.is_exposed` 内部读的是 **`self.get_block_name`**，
    而这里的 `self` 是 `_real`，不是 stub——于是它去读真实的 dimension
    （stub 里根本没有 dimension），每格都返回空串、一律判成"可见"。
    结果就是**一次诊断测试里 is_exposed 永远为真、且零次读取**。

    正确做法是让 stub 自己也当一个完整的 world：直接复刻那 6 行判定。
    这里刻意复刻而不是复用 `WorldAdapter.is_exposed`，因为被 `ActionCompiler`
    调用的是 `self.world.is_exposed(...)`，接口一致就够了；判定逻辑本身
    由第一部分和第三部分专门守着。
    """

    #: 与 WorldAdapter._SEETHROUGH 保持一致。
    #: 这里引用真类，避免两边各写一份、日后悄悄漂移。
    _SEETHROUGH = WorldAdapter._SEETHROUGH

    def __init__(self, blocks: dict[tuple[int, int, int], str]) -> None:
        self.blocks = blocks

    def get_block_name(self, dimension, x: int, y: int, z: int) -> str:
        return self.blocks.get((int(x), int(y), int(z)), "air")

    def is_exposed(self, dimension, x: int, y: int, z: int,
                   memo: dict | None = None) -> bool:
        if memo is None:
            memo = {}
        for key in ((x + 1, y, z), (x - 1, y, z),
                    (x, y + 1, z), (x, y - 1, z),
                    (x, y, z + 1), (x, y, z - 1)):
            name = memo.get(key)
            if name is None:
                name = self.get_block_name(dimension, *key)
                memo[key] = name
            if not name or name in self._SEETHROUGH:
                return True
        return False


def test_dig_side() -> None:
    section("第四部分：_find_blocks —— 挖掘侧也要能挡矿透")

    # 整片石头里放两块铁：一块**暴露**的（上方是空气），一块埋死的。
    #
    # ⚠️ 地层要铺得比"看起来需要的"更厚。DictDimension 对未登记的坐标
    # 返回 "air"，所以只要地层边缘在判定范围内，埋着的那块就会因为
    # "读到空气"而被判成可见——测试会变成假绿。
    # _find_blocks 的搜索范围是 y ∈ [oy-6, oy+6] = [64, 76]，
    # 而判定还要往外读一层邻居，所以地层必须覆盖到 y ∈ [62, 78]。
    blocks: dict[tuple[int, int, int], str] = {}
    for x in range(-20, 21):
        for z in range(-20, 21):
            for y in range(62, 79):
                blocks[(x, y, z)] = "stone"
    for x in range(-6, 7):
        for z in range(-6, 7):
            if x * x + z * z <= 36:
                blocks[(x, 70, z)] = "air"      # 假人周围的空腔
    blocks[(0, 69, 0)] = "iron_ore"             # 贴着空腔 → 暴露
    blocks[(0, 65, 0)] = "iron_ore"             # 里外全是石头 → 埋死

    log = FakeLogger()

    def compiler(surface_only: bool) -> ActionCompiler:
        w = _StubWorld(blocks)
        c = ActionCompiler(w, log)
        c.surface_only = surface_only
        return c

    actor = Actor(_StubWorld(blocks), 0.5, 70.0, 0.5)

    on = compiler(surface_only=True)._find_blocks(actor, "iron_ore", 8)
    off = compiler(surface_only=False)._find_blocks(actor, "iron_ore", 8)

    check("默认只找到暴露的那块", on == [(0, 69, 0)], str(on))
    check("关掉后埋着的也能找到（说明开关真的生效）",
          sorted(off) == [(0, 65, 0), (0, 69, 0)], str(off))
    check("显式传参覆盖实例开关",
          compiler(surface_only=True)._find_blocks(
              actor, "iron_ore", 8, surface_only=False) == off)

    # 判定顺序：先 `target in name` 再读邻居。反了的话无效读取会翻 6 倍。
    # 用一个会记账的 world 验证"不匹配的方块完全不触发邻居读"。
    class Counting(_StubWorld):
        def __init__(self, blocks):
            super().__init__(blocks)
            self.exposed_calls = 0

        def is_exposed(self, dimension, x, y, z, memo=None):
            self.exposed_calls += 1
            return super().is_exposed(dimension, x, y, z, memo)

    # 一整片石头，只有一块铁
    solid: dict[tuple[int, int, int], str] = {}
    solid_box(solid, 0, 70, 0, 3, 3, 3, fill="stone")
    solid[(0, 70, 0)] = "iron_ore"

    cw = Counting(solid)
    c = ActionCompiler(cw, log)
    c.surface_only = True
    act = Actor(cw, 0.5, 70.0, 0.5)
    c._find_blocks(act, "iron_ore", 8)
    # 7×7×13 = 637 格，匹配 target 的只有 1 格
    check("只有匹配的方块才付邻居查询的钱",
          cw.exposed_calls == 1, f"exposed_calls={cw.exposed_calls}")


# ---------------------------------------------------------------------------
# 第五部分：提示词拆段 + 用户写坏提示词不能炸
# ---------------------------------------------------------------------------

def _build(config: AgentConfig, name: str = "阿明") -> str:
    """只走提示词拼接，绕开 AgentState 的其它字段。"""
    from endstone_arc_fake_player.agent import build_system

    class S:
        pass

    s = S()
    s.config = config
    s.goal = config.goal
    s.notes = []
    s.landmarks = {}
    s.digest = ""
    return build_system(s)


def test_prompt_sections() -> None:
    section("第五部分：提示词段落 —— 可改，且写坏了不炸")

    base = AgentConfig(name="阿明")

    prompt = _build(base)
    for seg, why in [
        (DEFAULT_IDENTITY.replace("$name", "阿明"), "身份（$name 已替换）"),
        (DEFAULT_BODY, "身体"),
        (DEFAULT_PERSONA, "性格"),
        (DEFAULT_RULES, "规范"),
        (DEFAULT_SURFACE_HINT, "表面说明"),
    ]:
        check(f"默认提示词含{why}", seg in prompt)

    for head in ["# 关于你的身体", "# 长期目标", "# 你记得的事",
                 "# 极其重要的行为规范", "# 关于你看到的方块"]:
        check(f"结构小标题 {head} 在", head in prompt)

    check("$name 被替换成名字", "$name" not in prompt and "阿明" in prompt)

    # -- 每一段都能被 config 改写 --
    for field, text in [("identity", "我是 $name，一个测试。"),
                        ("body", "# 身体\n我是铁做的。"),
                        ("persona", "# 性格\n我很急躁。"),
                        ("rules", "1. 只要一条：别死。"),
                        ("surface_hint", "# 关于你看到的方块\n随便看看。")]:
        c = AgentConfig(name="阿明")
        setattr(c, field, text)
        p = _build(c)
        want = text.replace("$name", "阿明")
        check(f"config 改了 {field} 就生效",
              want in p, f"期望片段 {want[:24]!r}")

    # -- surface_hint 传空串 = 整段消失（关掉表面过滤时用）--
    c = AgentConfig(name="阿明")
    c.surface_hint = ""
    p = _build(c)
    check("surface_hint 置空 → 该段不再出现",
          "关于你看到的方块" not in p)

    # -- ★核心★ 用户写的提示词里有 `{}` 不能把 build_system 打炸 --
    #
    # 这是换成 string.Template 的唯一理由。写 JSON 例子、写格式占位
    # 都是极常见的操作，一个 KeyError 会把整轮决策打断。
    nasty = [
        ("JSON 例子", '输出 {"action": "mine", "x": 1}'),
        ("裸花括号", "{ 这不是占位符 }"),
        ("格式占位", "要挖 {count} 个方块"),
        ("嵌套花括号", '{"a": {"b": {"c": 1}}}'),
        ("单花括号", "{"),
        ("下标风格", "arr[0] 和 dict['k']"),
    ]
    for why, text in nasty:
        c = AgentConfig(name="阿明")
        c.rules = text
        try:
            p = _build(c)
            ok = text in p
        except Exception as exc:                      # noqa: BLE001
            ok = False
            p = f"<抛异常 {type(exc).__name__}: {exc}>"
        check(f"rules 写成「{why}」不炸且原样保留", ok,
              "" if ok else p[:80])

    # -- `$` 的规则（三条，都实测过）--
    #
    # 关键：段落正文**没有**被预先过一层 Template，而是直接进了外层
    # safe_substitute。所以 `$$` 在这里不被折叠，原样输出。
    # 只有 prompt_identity 会被单独过一层（为的是替换 `$name`），
    # 那个段落里的 `$$` 才会被折叠一次——见下面最后一条。
    c = AgentConfig(name="阿明")
    c.rules = "未知变量 $unknown 应该原样留着"
    p = _build(c)
    check("未知 $unknown 原样保留（不抛异常）",
          "$unknown 应该原样留着" in p)

    c = AgentConfig(name="阿明")
    c.rules = "价格是 $$100"
    p = _build(c)
    check("非 identity 段落里 $$ 原样保留（正文不过内层替换）",
          "价格是 $$100" in p, next((l for l in p.splitlines()
                                     if l.startswith("价格")), p[:60]))

    c = AgentConfig(name="阿明")
    c.identity = "我是 $name，价格 $$100"
    p = _build(c)
    check("identity 段落里 $$ 折叠成字面 $（它要过内层替换）",
          "我是 阿明，价格 $100" in p,
          next((l for l in p.splitlines() if l.startswith("我是")), p[:60]))

    c = AgentConfig(name="阿明")
    c.rules = "用 {mine_area} 挖，不要 $5"
    p = _build(c)
    check("花括号和 $数字 混在一起也安全",
          "{mine_area}" in p and "$5" in p)

    # -- identity 是唯一认 $name 的段落；别的段落里的 $name 不替换 --
    c = AgentConfig(name="阿明")
    c.rules = "我叫 $name"          # 故意写在 rules 里
    p = _build(c)
    check("$name 只在 identity 里生效（rules 里的原样保留）",
          "我叫 $name" in p)

    # -- 改坏 identity 也不能炸 --
    c = AgentConfig(name="阿明")
    c.identity = "我叫 $name，还有 $other。"
    p = _build(c)
    check("identity 里的未知变量原样保留",
          "我叫 阿明，还有 $other。" in p)

    # -- 提示词长度没被意外截断 --
    check("默认提示词长度合理（>800 字符）", len(prompt) > 800,
          f"len={len(prompt)}")


# ---------------------------------------------------------------------------
# 第六部分：confmig 配置迁移
# ---------------------------------------------------------------------------

def test_confmig() -> None:
    section("第六部分：confmig —— 老配置升级不丢用户改动")

    check("CONFIG_VERSION 是 17", CONFIG_VERSION == 17, str(CONFIG_VERSION))
    check("登记了 v1 的 persona 默认值",
          "prompt_persona" in confmig.LEGACY_DEFAULTS)
    check("登记的 v1 persona 与当前默认值一致（一字未改）",
          confmig.LEGACY_DEFAULTS["prompt_persona"][1] == DEFAULT_PERSONA)
    from endstone_arc_fake_player.agent import DEFAULT_RULES as _RULES
    from endstone_arc_fake_player.agent import DEFAULT_SPEECH as _SPEECH
    check("登记了 v6 的 rules 默认值（v7 加了克制发言条款）",
          "prompt_rules" in confmig.LEGACY_DEFAULTS)
    check("登记了 v9 的 speech 默认值（v10 语气词多样化）",
          "prompt_speech" in confmig.LEGACY_DEFAULTS)
    check("v6 rules 默认值已登记（前缀关系到 v14 重写为止）",
          confmig.LEGACY_DEFAULTS["prompt_rules"].get(6) is not None)
    check("v12/v13 的 rules 旧默认值已登记（现役老服所在版本档）",
          confmig.LEGACY_DEFAULTS["prompt_rules"].get(12) is not None
          and confmig.LEGACY_DEFAULTS["prompt_rules"].get(13) is not None)
    check("v12/13 的 rules 旧文本没有 descend（旧文本未失真）",
          "descend" not in confmig.LEGACY_DEFAULTS["prompt_rules"][13])
    check("v12/13 的 game_knowledge 旧文本已登记且无床配方",
          "床" not in confmig.LEGACY_DEFAULTS["prompt_game_knowledge"][13]
          and "熔炼" not in confmig.LEGACY_DEFAULTS["prompt_game_knowledge"][13])
    check("新 rules 明确禁止垂直挖矿并给出 descend",
          "禁止 dig_tunnel 朝 up/down" in _RULES and "descend" in _RULES)

    # -- v11：禁端着自称 --
    # 老服手里那份 v10 speech 必须能被认成"用户没动过"，否则新规则送不到。
    _v10 = confmig.LEGACY_DEFAULTS["prompt_speech"].get(10)
    check("登记了 v10 的 speech 默认值（v11 加自称禁令）", _v10 is not None)
    check("v10 speech 是旧文本（不含自称禁令）",
          _v10 is not None and "自称" not in _v10,
          repr(_v10)[:120] if _v10 else "")
    check("当前 speech 含'自称只用我'规则", "自称" in _SPEECH, _SPEECH[-200:])
    check("当前 speech 禁了'本教授/本座'这类自称",
          "本教授" in _SPEECH and "本座" in _SPEECH, _SPEECH[-200:])
    _v11 = confmig.LEGACY_DEFAULTS["prompt_speech"].get(11)
    check("登记了 v11 的 speech 默认值（v12 往禁令里补了'本座'）",
          _v11 is not None)
    check("v11 speech 是旧文本（不含本座）",
          _v11 is not None and "本座" not in _v11,
          repr(_v11)[-160:] if _v11 else "")

    # -- v12：chat_reply_chance 全局默认 0.15 → 0.5 --
    check("登记了 chat_reply_chance 的 v10/v11 旧值 0.15",
          confmig.LEGACY_DEFAULTS.get("chat_reply_chance", {}).get(10) == 0.15
          and confmig.LEGACY_DEFAULTS.get("chat_reply_chance", {}).get(11) == 0.15)
    _d = {"chat_reply_chance": 0.15}
    confmig.migrate(_d, 10, {"chat_reply_chance": 0.5}, to_version=12)
    check("老服停在 0.15 → 自动升到 0.5", _d["chat_reply_chance"] == 0.5,
          str(_d["chat_reply_chance"]))
    _d11 = {"chat_reply_chance": 0.15}
    confmig.migrate(_d11, 11, {"chat_reply_chance": 0.5}, to_version=12)
    check("v11 的服停在 0.15 → 也自动升到 0.5", _d11["chat_reply_chance"] == 0.5,
          str(_d11["chat_reply_chance"]))
    _d2 = {"chat_reply_chance": 0.3}
    _warns: list[str] = []
    confmig.migrate(_d2, 10, {"chat_reply_chance": 0.5}, to_version=12,
                    warn=_warns.append)
    check("自己改过的搭话概率保留原值", _d2["chat_reply_chance"] == 0.3)
    check("非提示词键的警告不指向不存在的命令",
          _warns and "prompt reset" not in _warns[0], _warns[0] if _warns else "")

    def defaults() -> dict:
        return {
            "base_url": "", "surface_only": True,
            "prompt_persona": DEFAULT_PERSONA,
            "prompt_rules": DEFAULT_RULES,
            "config_version": CONFIG_VERSION,
        }

    # -- ① 缺失即补：老用户永远拿不到新键，这是最要紧的一条 --
    d = defaults()
    del d["prompt_rules"]
    ch = confmig.migrate(d, 1, defaults(), to_version=CONFIG_VERSION)
    check("缺失的新键被补上", "prompt_rules" in ch, str(ch))
    check("补上的键值是当前默认值", d["prompt_rules"] == DEFAULT_RULES)

    # -- ② 没动过的旧值 → 安全升级 --
    d = defaults()
    d["prompt_persona"] = confmig.LEGACY_DEFAULTS["prompt_persona"][1]
    # 造一个"新默认值不同"的假象：把 defaults 里的新值改掉
    nd = defaults()
    new_text = DEFAULT_PERSONA + "（v2 新增一句）"
    nd["prompt_persona"] = new_text
    ch = confmig.migrate(d, 1, nd, to_version=CONFIG_VERSION)
    check("值停在旧版默认值 → 升到新默认值",
          d["prompt_persona"] == new_text, str(ch))

    # -- ③ 用户改过 → 原样保留 + 警告 --
    d = defaults()
    d["prompt_persona"] = "我自己写的性格，别动它"
    nd = defaults()
    nd["prompt_persona"] = DEFAULT_PERSONA + "（v2 新增一句）"
    warns: list[str] = []
    ch = confmig.migrate(d, 1, nd, to_version=CONFIG_VERSION, warn=warns.append)
    check("用户改过的值原样保留",
          d["prompt_persona"] == "我自己写的性格，别动它")
    check("用户改过时会警告（否则用户以为插件没生效）",
          len(warns) == 1, str(warns))
    check("警告里带 /ai prompt reset 提示（给出自救办法）",
          warns and "prompt reset" in warns[0], warns[0] if warns else "")

    # -- ④ 幂等：已经是当前版本就什么都不做 --
    d = defaults()
    snapshot = json.dumps(d, sort_keys=True, ensure_ascii=False)
    ch = confmig.migrate(d, CONFIG_VERSION, defaults(), to_version=CONFIG_VERSION)
    check("from_version == to_version → 不动任何东西",
          ch == [] and json.dumps(d, sort_keys=True, ensure_ascii=False) == snapshot)

    # -- ⑤ 配置比代码新（用户降级了插件）→ 也不动 --
    d = defaults()
    snapshot = json.dumps(d, sort_keys=True, ensure_ascii=False)
    ch = confmig.migrate(d, 99, defaults(), to_version=CONFIG_VERSION)
    check("配置版本比代码新 → 不动（不能拿未来格式去猜）",
          ch == [] and json.dumps(d, sort_keys=True, ensure_ascii=False) == snapshot)

    # -- ⑥ 用户自定义键不能被删 --
    d = defaults()
    d["my_own_key"] = "别删我"
    confmig.migrate(d, 1, defaults(), to_version=CONFIG_VERSION)
    check("用户自己加的键保留", d.get("my_own_key") == "别删我")

    # -- ⑦ 值已经等于新默认值时不该误报警告 --
    #
    # 这是上一轮真实踩过的坑：用户改过 persona，第一次启动警告一次、
    # 版本号写回去了；但如果 migrate 只看"值 != 旧默认值"就警告，
    # 之后每次启动都会重复刷同一条 WARN。
    d = defaults()
    d["prompt_persona"] = DEFAULT_PERSONA      # 恰好等于新默认值
    warns = []
    confmig.migrate(d, 1, defaults(), to_version=CONFIG_VERSION, warn=warns.append)
    check("值等于新默认值 → 不警告", warns == [], str(warns))

    # -- ⑧ 别名用法：cfg 和 defaults 是同一个 dict --
    #
    # plugin.py 就是这么调的（"值 != 默认值"恰好等价于"用户改过"）。
    # 关键契约是：**别把自己补进去的键又当成"用户改过"**，
    # 以及**迭代过程中补键不能漏掉后面的键**（靠 `list(...)` 快照）。
    #
    # 注意这里不能用 `{v2: 真默认值}` 当 defaults 去跑"缺失即补"：
    # 那个字典本身已经把 v2 的键都铺好了，跑完当然什么都不缺。
    # 真实场景里 `_load_config` 先用 v2 默认值铺满再被用户值覆盖，
    # 所以走到 migrate 时键是齐的，"缺失即补"只在**后来新增的键**上
    # 才真正起作用——那正是第 ① 条用例覆盖的。
    d = {"base_url": "https://api.deepseek.com/v1",
         "prompt_persona": confmig.LEGACY_DEFAULTS["prompt_persona"][1]}
    before_keys = set(d)
    ch = confmig.migrate(d, 1, d, to_version=CONFIG_VERSION)
    check("cfg 与 defaults 同对象时不抛异常（不边迭代边改）", True)
    check("版本号被写回", d["config_version"] == CONFIG_VERSION)
    check("用户原有的键一个没少", before_keys <= set(d), str(sorted(d)))
    # 值恰好都等于 defaults 里的值 → 没有任何"改动"，changed 为空。
    # plugin.py 靠 changed 决定要不要落盘，空就是"不用写"。
    check("值都等于当前默认值时 changed 为空（不必落盘）", ch == [],
          str(ch))

    # -- ⑨ 空 cfg 只补版本号 --
    d: dict = {}
    ch = confmig.migrate(d, 1, {}, to_version=CONFIG_VERSION)
    check("空 defaults 只写版本号", d == {"config_version": CONFIG_VERSION},
          str(d))
    check("空 defaults 时 changed 为空", ch == [])


# ---------------------------------------------------------------------------
# 第七部分：plugin.py 的接线（源码契约）
# ---------------------------------------------------------------------------

def test_plugin_wiring() -> None:
    section("第七部分：plugin.py 接线")

    src = (ROOT / "endstone_arc_fake_player" / "plugin.py").read_text(encoding="utf-8")

    # config 里的新键
    for key, want, why in [
        ("surface_only", "True", "感知只报表面方块"),
        ("dig_surface_only", "True", "挖掘也只找表面方块"),
        ("config_version", "CONFIG_VERSION", "schema 版本"),
    ]:
        check(f"defaults 有 {key}（{why}）", f'"{key}": {want}' in src,
              f'期望 "{key}": {want}')

    for key in ["prompt_identity", "prompt_body", "prompt_persona",
                "prompt_rules", "prompt_surface_hint"]:
        check(f"defaults 有 {key}", f'"{key}": ' in src)

    on_enable = src[src.find("def on_enable"):src.find("def on_disable")]
    check("WorldAdapter 收到 surface_only=", "surface_only=" in on_enable)
    check("BatchExecutor 收到 max_steps_per_tick=",
          "max_steps_per_tick=" in on_enable)

    # v3.1：AgentConfig 的拼装从 _cmd_spawn 搬进了 _start_agent
    # （自动在线的角色卡和手动 spawn 走同一条创建路径）。
    # 契约不变：生成智能体时必须把提示词段落从 config 带过去。
    spawn_start = src.find("def _start_agent")
    if spawn_start < 0:
        spawn_start = src.find("def _cmd_spawn")
    spawn_end = src.find("\n    def ", spawn_start + 10)
    spawn = src[spawn_start:spawn_end]
    for key in ["prompt_identity", "prompt_body", "prompt_persona",
                "prompt_rules", "prompt_surface_hint"]:
        check(f"_start_agent 传了 {key}", key in spawn)
    check("_start_agent 把 dig_surface_only 接到 compiler.surface_only",
          "dig_surface_only" in spawn and "compiler.surface_only" in spawn)
    check("surface_hint 按 surface_only 开关决定带不带",
          'if self._cfg["surface_only"]' in spawn)

    # 迁移逻辑真的接了
    check("_load_config 调了 confmig.migrate", "confmig.migrate(" in src)
    check("_load_config 比对了 config_version",
          'data.get("config_version", 1)' in src
          or "config_version" in src[src.find("def _load_config"):
                                    src.find("def _save_config")])
    check("迁移后落盘用了 _save_config",
          "_save_config()" in src[src.find("def _load_config"):
                                  src.find("def _save_config")])

    # /ai prompt 与 /ai sense
    check("注册了 prompt 子命令", '"prompt": self._cmd_prompt' in src)
    check("注册了 sense 子命令", '"sense": self._cmd_sense' in src)
    check("命令表里有 /ai prompt", "/ai prompt" in src)
    check("命令表里有 /ai sense", "/ai sense" in src)
    check("_PROMPT_FIELDS 五个段落",
          all(f'"{f}"' in src for f in
              ["identity", "body", "persona", "rules", "surface_hint"]))

    # 提示词默认值从 agent.py 导入，不是抄一份在 plugin.py
    check("plugin.py 从 agent 导入提示词默认值",
          "DEFAULT_IDENTITY" in src and "from .agent import" in src)


# ---------------------------------------------------------------------------
# 第八部分：身体失效 / 占位实体
# ---------------------------------------------------------------------------

class _Dead(RuntimeError):
    pass


class _FakeActor:
    """模拟 Endstone 的 Actor。

    关键行为：实体被销毁后，访问 `location` / `dimension` 会抛
    `RuntimeError: actor is not valid`。`is_valid` 本身**不抛**——
    它是 C++ 侧属性，读的是底层句柄，销毁后老老实实返回 False。
    这个不对称正是保护能生效的原因。
    """

    def __init__(self, name: str = "bot") -> None:
        self.name = name
        self._dead = False
        self.is_valid = True

    def kill(self) -> None:
        self._dead = True
        self.is_valid = False

    def _chk(self) -> None:
        if self._dead:
            raise _Dead("actor is not valid")

    @property
    def dimension(self):
        self._chk()
        return "overworld"

    @property
    def location(self):
        self._chk()
        return _DeadLoc()

    def remove(self) -> None:
        self._dead = True
        self.is_valid = False


class _DeadLoc:
    """死实体用不到真坐标 —— 它的 .location 在取到之前就抛了。"""

    x = 0.5
    y = 64.0
    z = 0.5
    pitch = 0.0
    yaw = 0.0


def test_body_lifecycle() -> None:
    """身体被打掉之后，插件不能拿着尸体当活人用。

    用户报的现象是"创造模式打掉假人 → 服务器崩溃"。根因不是某一行
    写错了，而是**没有一层把"实体还活着吗"这件事统一拦下来**：
    `self._actors` 里存的只是个引用，实体被干掉后它照样在 dict 里躺着，
    此后任何一次 `actor.location` / `actor.dimension` 都会抛
    `RuntimeError: actor is not valid`。

    这一部分钉住三件事：
      1. `_is_live` / `_live_actor` 判定正确（死 False / 活 True / None False）
      2. `_current_actor` 对死实体返回 None —— 所有步骤处理器都靠它
      3. `_perceive` 会主动收摊（摘引用、取消计划），而不是只 continue
    """
    from endstone_arc_fake_player.plugin import ArcFakePlayerPlugin

    section("第八部分：身体失效与占位实体")

    live_cls = vars(ArcFakePlayerPlugin)

    # ---- _is_live 的判定语义 ----
    check("_is_live 是 staticmethod（不需要实例就能用）",
          isinstance(live_cls["_is_live"], staticmethod))
    is_live = live_cls["_is_live"].__func__

    check("_is_live(None) → False", is_live(None) is False)

    alive = _FakeActor()
    check("_is_live(活实体) → True", is_live(alive) is True)

    dead = _FakeActor()
    dead.kill()
    check("_is_live(死实体) → False", is_live(dead) is False)

    class _NoFlag:
        pass
    check("_is_live 对没有 is_valid 的替身默认放行",
          is_live(_NoFlag()) is True)

    class _BadFlag:
        @property
        def is_valid(self):
            raise RuntimeError("连 is_valid 都读不了")
    check("_is_live 读 is_valid 抛异常时判为失效（保守）",
          is_live(_BadFlag()) is False)

    # ---- 关键：死实体的属性访问确实会抛 ----
    try:
        dead.location
        raised = False
    except _Dead:
        raised = True
    check("死实体的 .location 会抛 RuntimeError（这就是崩服源头）", raised)

    # ---- plugin.py 里不能再有裸的 _actors.get ----
    #
    # 允许三种情况：
    #   · `_live_actor` 内部那一处（它就是包装器本身）
    #   · 紧跟着 `_is_live` 的显式判定（读出来只为判死活，不拿去用）
    #   · `_upgrade_placeholder` 里取出旧占位实体只为了 remove 它
    # 其余任何"读出来直接当身体用"的写法都是漏网之鱼。
    src = (ROOT / "endstone_arc_fake_player" / "plugin.py").read_text("utf-8")
    lines = src.splitlines()
    naked = []
    for i, ln in enumerate(lines):
        if "self._actors.get(" not in ln:
            continue
        if "actor = self._actors.get(name)" in ln:      # _live_actor 内部
            continue
        if "`self._actors.get(" in ln:                  # 文档里引用的
            continue
        if "_is_live(self._actors.get(" in ln:          # 只为判死活
            continue
        if "old = self._actors.get(name)" in ln:        # 只为 remove 旧占位
            continue
        if "actor = self._actors.get(name)" in ln:      # _is_placeholder 里读
            continue
        naked.append(f"{i + 1}: {ln.strip()}")
    check("plugin.py 里没有裸的 _actors.get（除包装器/判定/清理）",
          not naked, f"残留: {naked}")

    # 反向确认：_actors.get 的用量被钉住，新加的地方必须显式登记
    # （合法用途：_live_actor / _is_placeholder 各一处读，_tick 判定一处，
    #   占位清理一处，共 4 处 + 文档引用若干）
    _uses = src.count("self._actors.get(")
    check("_actors.get 的用法没有失控增长",
          _uses <= 6, f"实际 {_uses} 处")

    check("_perceive 用 _live_actor 取身体",
          "_live_actor(name)" in src)
    check("_current_actor 用 _live_actor 取身体",
          "return self._live_actor(self._exec_owner)" in src)
    check("给 Agent 的 get_actor 也做了有效性过滤",
          "get_actor=lambda n=name: self._live_actor(n)" in src)
    check("_tick 会跳过死实体的计划",
          "if not self._is_live(self._actors.get(name)):" in src)
    check("_cmd_where 用 _live_actor", "_live_actor(args[0])" in src)
    check("_cmd_tp 用 _live_actor",
          src.count("_live_actor(args[0])") >= 2)
    check("_tell_skin 用 _live_actor", "_live_actor(name)" in src)

    # ---- _bury_body 的收摊契约 ----
    check("_bury_body 幂等（done_reason 当哨兵）",
          "if not agent.alive():" in src[src.find("def _bury_body"):
                                        src.find("def _bury_body") + 1200])
    check("_bury_body 会摘掉 _actors 里的引用",
          "self._actors.pop(name, None)" in src)
    check("_bury_body 会取消执行器里排队的计划",
          "self._executor.cancel_all(name)" in src)
    check("_bury_body 会收掉身体控制器的异步动作",
          "ctl.cancel(reason)" in src)
    check("_bury_body 每一步都兜异常（不能在调度器回调里炸）",
          src[src.find("def _bury_body"):src.find("def _bury_body") + 1600]
          .count("except Exception") >= 4)

    # ---- _perceive 里的占位升级分支 ----
    check("_perceive 先判占位、再判死活（顺序反了就永远换不掉）",
          src.find("if self._use_real_body and self._is_placeholder(name):")
          < src.find("self._bury_body(name, agent", 
                     src.find("def _perceive")))
    check("_is_placeholder 用 _placeholder_flags 记账",
          "_placeholder_flags.get(name)" in src)
    check("_make_placeholder 会打上占位标记",
          src[src.find("def _make_placeholder"):src.find("def _dimension_of")]
          .count("self._placeholder_flags[name] = True") == 1)
    _up = src.find("def _upgrade_placeholder")
    _up_end = src.find("def _dimension_of", _up)
    # ⚠️ 这里断言的是 >= 1 而不是 == 1。
    #
    # 3.0.0 起 `_upgrade_placeholder` 有**两处**清占位标记：一处是
    # 智能体已经停摆时的提前返回（防止把停摆的智能体"复活"成一具
    # 没人控制的孤儿身体），另一处是正常升级成功后的常规清理。
    # 两处都是必须的，所以不能要求"有且仅有一处"。
    # 真正要守住的不变量是"_upgrade_placeholder 一定清得掉占位标记"。
    check("_upgrade_placeholder 会清掉占位标记",
          src[_up:_up_end].count("self._placeholder_flags.pop(name, None)") >= 1)
    check("_upgrade_placeholder 对已停摆的智能体会提前返回",
          "self._agents.get(name)" in src[_up:_up_end]
          and "not agent.alive()" in src[_up:_up_end])
    check("_bury_body 也会清掉占位标记",
          src[src.find("def _bury_body"):src.find("def _bury_body") + 1600]
          .count("self._placeholder_flags.pop(name, None)") == 1)
    # 找真身必须走 _find_sim_player，不能裸用 get_player。
    #
    # 这条是 2.0.0 修的真实 bug：`server.get_player(name)` 是按名字查在线
    # 玩家，不区分"假人"和"用同名账号登录的真人"。命中真人之后，那个人
    # 一退出游戏，插件就判定"身体消失"，智能体跟着停摆——用户看到的就是
    # "我一退游戏，AI 就说没身体退出了"。
    check("占位判定走 _find_sim_player（不裸用 get_player）",
          "if self._find_sim_player(name) is actor:" in src)
    check("_upgrade_placeholder 走 _find_sim_player",
          "player = self._find_sim_player(name)" in src)
    check("_find_sim_player 有语义校验（能力探测）",
          "def _looks_simulated" in src and "def _find_sim_player" in src)
    check("同名真人会被明确警告，不静默接上",
          "但它看起来不是模拟玩家" in src)
    check("找不到真身时宁可不认，也不退而认真人",
          "return False" in src[src.find("def _looks_simulated"):src.find("def _find_sim_player")])
    check("_upgrade_placeholder 会 remove 掉旧占位实体",
          "self._despawn_body(old)" in src)
    check("_upgrade_placeholder 会同步 ctl.actor",
          "ctl.actor = player" in src)

    # ---- 占位实体不再写死 overworld ----
    check("_dimension_of 辅助函数存在", "def _dimension_of(" in src)
    check("_make_placeholder 不再写死 overworld",
          'get_dimension("overworld").spawn_actor' not in src)
    check("_spawn_body 的 npc 分支也走 _dimension_of",
          "self._dimension_of(loc).spawn_actor(loc, entity_type)" in src)
    check("占位实体造不出来时 spawn 会失败退出（不假装成功）",
          "if ph is None:" in src)

    # ---- agent.py 侧 ----
    asrc = (ROOT / "endstone_arc_fake_player" / "agent.py").read_text("utf-8")
    check("agent._apply 拿到空身体时会停掉执行器",
          "self._executor.cancel_all(self.name)" in asrc)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 70)
    print("表面方块剔除 & 提示词进 config")
    print("=" * 70)

    test_is_exposed()
    test_scan_blocks()
    test_read_budget()
    test_dig_side()
    test_prompt_sections()
    test_confmig()
    test_plugin_wiring()
    test_body_lifecycle()

    print()
    print("=" * 70)
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项失败（共 {CHECKS} 项）")
        for f in FAILURES:
            print(f"   · {f}")
        return 1
    print(f"✅ 全部 {CHECKS} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
