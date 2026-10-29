"""世界交互层：把 Endstone 的方块/实体 API 封装成机器人能用的原语。

全部基于纯 Python 的 Endstone API：
  Dimension.get_block_at()  读方块
  Block.set_type()          写方块
  Actor.teleport()          移动
  Actor.set_rotation()      转向
  Dimension.spawn_actor()   生成实体
  Actor.health / max_health 生命值

这里同时也承担"感知压缩"的职责——把三维世界压成一段紧凑文本，
这是控制 token 成本的第一道闸门。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from endstone.actor import Actor


# ---------------------------------------------------------------------------
# 方块分类：决定"什么值得告诉模型"
# ---------------------------------------------------------------------------

#: 对生存真正有价值的方块。不在这里的方块一律不进入观测——
#: 这是压 token 的第一刀，能把观测体积砍掉 90% 以上。
#: 注意：空气不在列表里。报告"附近有空气"对模型毫无信息量。
VALUABLE = {
    # 木材
    "log", "wood", "planks", "leaves", "sapling",
    # 石质
    "stone", "cobblestone", "deepslate", "cobbled_deepslate", "granite",
    "diorite", "andesite", "tuff", "calcite", "obsidian",
    # 矿石
    "coal_ore", "iron_ore", "copper_ore", "gold_ore", "diamond_ore",
    "redstone_ore", "lapis_ore", "emerald_ore", "deepslate_coal_ore",
    "deepslate_iron_ore", "deepslate_gold_ore", "deepslate_diamond_ore",
    "deepslate_redstone_ore", "deepslate_lapis_ore", "deepslate_emerald_ore",
    "raw_iron_block", "iron_block", "gold_block", "diamond_block",
    # 土壤
    "dirt", "grass_block", "sand", "gravel", "clay", "farmland", "podzol",
    # 危险
    "lava", "fire", "magma", "cactus", "sweet_berry_bush",
    # 食物
    "wheat", "carrots", "potatoes", "beetroots", "melon", "pumpkin",
    "sugar_cane", "bamboo", "kelp",
    # 功能方块
    "crafting_table", "furnace", "chest", "torch", "bed", "door",
    "ladder", "scaffolding", "glass", "brick", "sandstone",
    "cobblestone_wall", "stone_bricks", "mossy_cobblestone",
}

#: 危险方块——踩上去或碰一下会掉血
DANGEROUS = {"lava", "fire", "magma", "cactus", "sweet_berry_bush", "campfire"}

#: 常见敌对生物
HOSTILE = {
    "zombie", "skeleton", "creeper", "spider", "cave_spider", "enderman",
    "witch", "slime", "magma_cube", "drowned", "husk", "stray", "phantom",
    "pillager", "vindicator", "ravager", "blaze", "ghast", "piglin",
    "piglin_brute", "hoglin", "zoglin", "wither_skeleton", "silverfish",
    "guardian", "elder_guardian", "shulker", "vex", "evoker", "warden",
}

#: 可获取食物的被动生物
PASSIVE = {
    "cow", "pig", "sheep", "chicken", "rabbit", "horse", "donkey",
    "llama", "goat", "fox", "cod", "salmon", "tropical_fish", "squid",
    "mooshroom", "turtle", "panda", "polar_bear",
}

#: 方块短名（去掉命名空间），同时把常见变体归一化
_NORMALIZE = {
    "oak_log": "log", "birch_log": "log", "spruce_log": "log",
    "jungle_log": "log", "acacia_log": "log", "dark_oak_log": "log",
    "mangrove_log": "log", "cherry_log": "log", "pale_oak_log": "log",
    "crimson_stem": "log", "warped_stem": "log",
}


def short_id(type_id: str) -> str:
    """minecraft:oak_log -> oak_log"""
    return str(type_id or "").split(":", 1)[-1]


def classify(block_id: str) -> str | None:
    """把方块归类。返回 None 表示这个方块对模型没有信息价值。"""
    name = _NORMALIZE.get(block_id, block_id)
    for key in VALUABLE:
        if key in name:
            return name
    return None


# ---------------------------------------------------------------------------
# 观测快照
# ---------------------------------------------------------------------------

@dataclass
class BlockHit:
    """一个被感知到的方块，坐标以"相对机器人的偏移"表示——更省 token。"""

    name: str
    off: tuple[int, int, int]
    count: int = 1
    dist: float = 0.0


@dataclass
class EntityHit:
    name: str
    dist: float
    hostile: bool = False
    passive: bool = False
    #: 这是一个**玩家**（真人或别的假人）——感知里单独成行，
    #: 让模型知道"附近有谁"（聊天/交易/对枪都靠它）。
    player: bool = False
    #: 实体的世界坐标（整数格）。hunt 之类的"走过去再动手"工具需要它；
    #: 感知行不用（只报距离，省 token）。拿不到（实体已消失）为 None。
    pos: tuple[int, int, int] | None = None


@dataclass
class Snapshot:
    """一次完整的感知快照。"""

    pos: tuple[float, float, float]
    dimension: str
    health: float | None = None
    max_health: float | None = None
    on_ground: bool | None = None
    in_water: bool = False
    in_lava: bool = False
    blocks: list[BlockHit] = None          # type: ignore[assignment]
    entities: list[EntityHit] = None       # type: ignore[assignment]
    inventory: list[tuple[str, int]] = None  # type: ignore[assignment]
    #: 地形是否真的在线。False 表示这一带的区块没加载——此时 `blocks`
    #: 为空不是"周围什么都没有"，而是**根本没读到**。
    #:
    #: 必须把这个区分告诉模型：不然它会以为自己站在一片虚空里，
    #: 然后做出"往脚下猛挖"之类完全错误的决定。
    terrain_loaded: bool = True
    #: 环境时间标签："白天"/"夜晚"/None（拿不到世界时间时不显示）。
    #: 模型靠它决定天黑回家睡觉（Rules 第 7 条）。
    daynight: str | None = None

    def __post_init__(self) -> None:
        self.blocks = self.blocks or []
        self.entities = self.entities or []
        self.inventory = self.inventory or []

    # -- 压缩成文本（token 成本的关键） -----------------------------------

    def to_prompt(self, events: list[dict] | None = None, chat: list[str] | None = None) -> str:
        """把快照压成提示词。

        关键约束：模型拿到的是世界绝对坐标，但它对绝对坐标没有直觉。
        所以这里两样都给——绝对坐标供它计算，相对偏移供它判断远近。
        """
        x, y, z = self.pos
        bx, by, bz = int(x), int(y), int(z)
        env = f"  {self.daynight}" if self.daynight else ""
        lines = [f"位置 ({bx},{by},{bz})  维度 {self.dimension}{env}"]

        status = []
        if self.health is not None:
            status.append(f"血量 {self.health:.0f}/{self.max_health:.0f}")
        if self.in_lava:
            status.append("⚠你在岩浆里")
        elif self.in_water:
            status.append("在水中")
        if self.on_ground is False:
            status.append("悬空")
        lines.append("  ".join(status) if status else "状态未知")

        if self.inventory:
            lines.append("背包: " + ",".join(f"{n}x{c}" for n, c in self.inventory[:10]))
        else:
            lines.append("背包: 空")

        if self.blocks:
            lines.append("附近方块 (名称 | 相对你的偏移 | 距离 | 数量):")
            for b in self.blocks[:12]:
                ox, oy, oz = b.off
                ax, ay, az = bx + ox, by + oy, bz + oz
                lines.append(f"  {b.name}  偏移({ox},{oy},{oz}) 绝对({ax},{ay},{az}) "
                             f"距离{b.dist:.0f} 共{b.count}个")
        elif not self.terrain_loaded:
            # 关键区别：这里不是"周围什么都没有"，而是"没读到"。
            # 不说清楚的话，模型会以为自己悬在虚空里，然后往脚下猛挖。
            lines.append(
                "⚠周围地形尚未加载完成（区块不在内存里），"
                "这不是\"附近没有方块\"。先原地等一下，"
                "或者走回你刚才来的方向，不要据此判断脚下是空的。")
        else:
            lines.append("附近没有有价值的方块")

        hostiles = [e for e in self.entities if e.hostile]
        passives = [e for e in self.entities if e.passive]
        players = [e for e in self.entities if e.player]
        others = [e for e in self.entities
                  if not e.hostile and not e.passive and not e.player]
        if hostiles:
            lines.append("⚠敌对生物: " + ",".join(f"{e.name}({e.dist:.0f}格)" for e in hostiles[:6]))
        if passives:
            lines.append("可猎取: " + ",".join(f"{e.name}({e.dist:.0f}格)" for e in passives[:6]))
        if players:
            # 玩家单独一行：模型需要知道"谁在身边"来聊天/交易/对战，
            # 混在"其他实体"里和盔甲架没区别。
            lines.append("附近玩家: " + ",".join(f"{e.name}({e.dist:.0f}格)" for e in players[:6]))
        if others:
            lines.append("其他实体: " + ",".join(f"{e.name}({e.dist:.0f}格)" for e in others[:4]))

        if events:
            lines.append("上批动作: " + "; ".join(
                f"{e.get('op')}{'✓' if e.get('ok') else '✗'}"
                + (f"({e['steps']}步)" if e.get("steps") else "")
                for e in events[-5:]
            ))
        if chat:
            lines.append("玩家说: " + " | ".join(chat[-3:]))

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 世界适配器
# ---------------------------------------------------------------------------

class WorldAdapter:
    """封装一切与 Endstone API 的直接接触。

    刻意做成薄适配层：所有游戏 API 调用集中在这里，便于版本适配，
    也让上层逻辑（计划、决策）可以脱离服务器单独测试。
    """

    #: 视线/扫描半径，决定开销
    SCAN_RADIUS = 4
    SCAN_HEIGHT_UP = 3
    SCAN_HEIGHT_DOWN = 2

    def __init__(self, logger: Any, *, surface_only: bool = True) -> None:
        self._logger = logger
        #: 感知是否只报告"表面方块"（六面中至少一面贴着空气/水）。
        #: 默认开——能报出埋在石头里的钻石等于给模型开矿透，
        #: 观测真实了行为才真实。由插件从 config 注入。
        #: 做成实例属性（像 body_dimension 那样）是为了让 /ai 命令能动态改。
        self.surface_only = surface_only
        #: 在线玩家列表的注入点（server.online_players）。
        #: dimension.actors 拿不拿得到玩家因版本而异，玩家列表以
        #: server 为准；None 时感知里就没有"附近玩家"这一行。
        self._players_fn: Any = None
        #: 世界时间注入点：() -> 当前 tick（0~24000 一天）。插件用
        #: server.level.time 实现；None 时感知里没有昼夜标签。
        self._time_fn: Any = None

    def bind_players(self, fn: Any) -> None:
        """注入"取在线玩家列表"的回调（返回可迭代的 Player）。"""
        self._players_fn = fn

    def bind_time(self, fn: Any) -> None:
        """注入"取世界时间 tick"的回调。"""
        self._time_fn = fn

    @staticmethod
    def daynight_label(tick: int | None) -> str | None:
        """世界 tick → "白天"/"夜晚"。

        基岩版昼夜：0 是清晨，~12000 黄昏，12542 起可以睡觉，
        23459 天亮。粗分成 夜晚 [12542, 23460)，其余白天。
        """
        if tick is None:
            return None
        try:
            tick = int(tick) % 24000
        except (TypeError, ValueError):
            return None
        return "夜晚" if 12542 <= tick < 23460 else "白天"

    # -- 方块读写 ---------------------------------------------------------

    def get_block_name(self, dimension: Any, x: int, y: int, z: int) -> str:
        """读一个方块的短名。失败返回空串。"""
        try:
            block = dimension.get_block_at(int(x), int(y), int(z))
            if block is None:
                return ""
            return short_id(block.type)
        except Exception:
            return ""

    def is_chunk_loaded(self, dimension: Any, x: int, y: int, z: int) -> bool:
        """这一格所在的区块现在在内存里吗。

        为什么需要它：`get_block_at` 在**区块未加载**时返回 None，
        和"这里真的是空气"在返回值上无法区分。而两者的处理方式完全相反：

          · 真的是空气   → 这是有效观测，可以据此决策
          · 区块没加载   → **不是**观测，不能当成"这里什么都没有"

        把后者当空气，假人就会"看见"一片虚空，然后做出各种莫名其妙的
        决定（比如往脚下猛挖）。所以这里给出一个明确的区分手段，
        让上层能把"没加载"和"是空气"分开处理。

        实现上直接查区块对象：能拿到 block 就是加载了；拿不到再看
        区块容器里有没有。两条都不通就算未加载。
        """
        try:
            block = dimension.get_block_at(int(x), int(y), int(z))
            if block is not None:
                return True
        except Exception:
            pass
        # 退一步：查区块本身。有些实现里 get_block_at 对边界方块返回 None，
        # 但区块其实是加载着的。
        try:
            getter = getattr(dimension, "get_block", None)
            if callable(getter):
                return getter(int(x), int(y), int(z)) is not None
        except Exception:
            pass
        return False

    #: 明确的空气类方块
    _AIR_NAMES = {"air", "cave_air", "void_air"}

    def is_air(self, dimension: Any, x: int, y: int, z: int) -> bool:
        """判断是不是空气。

        注意不能简单地用 `"air" in name` —— 那样会把
        `stair`（楼梯）、`repair` 之类误判成空气，进而让挖矿
        跳过真实的方块。这里必须做精确匹配。
        """
        name = self.get_block_name(dimension, x, y, z)
        if not name:
            return True
        return name in self._AIR_NAMES

    #: 能让方块"被看见"的透明类方块——空气族 + 水族。
    #:
    #: 同样用精确集合而不是子串匹配，理由和 `is_air` 一样：
    #: `"air" in name` 会把 `stair` 判成空气，于是贴着楼梯的矿石
    #: 会被算成"表面矿"——矿透就从这儿漏回去了。
    #:
    #: 为什么只含空气和水，不含岩浆/玻璃/树叶：
    #:   · 岩浆：站在岩浆边看不见它**里面**的东西，只报空气和水更合理
    #:   · 玻璃：隔着玻璃看见的矿在游戏里也挖不到，得先破玻璃
    #:   · 树叶：它是实体方块，会挡住视线
    #: 这些都是刻意的简化。真要精确就得做可见性计算，代价远超收益。
    _SEETHROUGH = frozenset({
        "air", "cave_air", "void_air",
        "water", "flowing_water",
    })

    def is_exposed(self, dimension: Any, x: int, y: int, z: int,
                   memo: dict[tuple[int, int, int], str] | None = None) -> bool:
        """这个方块是不是"看得见"——六面中任意一面贴着空气或水。

        朴素做法是把周围 4 格半径内所有方块都报给模型。但那样模型能
        隔着石头"看见"钻石，直接标坐标开挖，等于给它开了矿透（X-ray）。
        这里改成只报表面方块：假人得像人一样先挖下去，才能发现下面的矿。

        `memo` 由**调用方**传入：一次扫描要判几百个方块，而相邻方块会
        反复查同一个邻居（每格 6 个方向，重叠率极高）。传 None 时自建
        临时 dict，语义照样正确，只是没有跨格复用。

        为什么把水也算"透明"：玩家站在水里能看见水底的矿，这个假人也
        该看得见。只判空气的话，所有水下/海底的矿石会从感知里整个消失
        ——那反而比现在更不真实。

        失败方向：读不到方块（越界、区块没加载、API 抛异常）一律当
        "空气"→ 判为可见。这与 `get_block_name` 返回空串、`is_air`
        返回 True 的约定一致。最坏情况是漏剔一个埋着的方块、多报一次；
        绝不会反过来把真实可见的方块藏起来。
        """
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

    def is_solid(self, dimension: Any, x: int, y: int, z: int) -> bool:
        """是不是实体方块——用于判断"这一格有没有东西要挖"。"""
        name = self.get_block_name(dimension, x, y, z)
        if not name or name in self._AIR_NAMES:
            return False
        # 水、岩浆这类流体不算"需要挖的方块"
        return not any(k in name for k in ("water", "lava", "flowing"))

    #: 假人所在的维度。接上真实身体后由插件注入——
    #: 这样 realbody 里那些"只要坐标、不要 dimension"的判断就能复用。
    body_dimension: Any = None

    def is_solid_id(self, x: int, y: int, z: int) -> bool:
        """按假人当前维度判断某格是不是实体方块。

        `realbody.py` 里的判断只关心"这格挖掉了没有"，拿不到 dimension
        对象。这里用一个可注入的维度来兜住，注入前一律返回 True
        （当作"还没挖掉"），这样失败方向是保守的——最坏情况是重试，
        不会把没挖的方块当成挖掉了。
        """
        dim = self.body_dimension
        if dim is None:
            return True
        return self.is_solid(dim, x, y, z)

    def set_block(self, dimension: Any, x: int, y: int, z: int, block_type: str,
                  *, physics: bool = True) -> bool:
        """写一个方块。

        physics=False 用于批量建造：不触发方块更新，速度快很多，
        也不会让沙砾/水四处流动。
        """
        try:
            block = dimension.get_block_at(int(x), int(y), int(z))
            if block is None:
                return False
            block.set_type(str(block_type), physics)
            return True
        except Exception as exc:
            self._logger.debug(f"set_block({x},{y},{z},{block_type}) 失败: {exc}")
            return False

    def break_block(self, dimension: Any, x: int, y: int, z: int) -> bool:
        """破坏方块——对机器人来说是"把它变成空气"，并掉落物品。

        这里刻意不模拟挖掘耗时：那需要真实的玩家行为系统，
        纯插件实现做不到。取而代之，我们把"挖掘"定义为
        "消失 + 进背包"，物理上是瞬时的，但决策语义完整。
        """
        name = self.get_block_name(dimension, x, y, z)
        # 精确判断空气（与 `is_air` 同一套精确集合）：子串匹配 "air" in name
        # 会把 stair（楼梯）等方块误判成"已经是空气"而直接返回 False，
        # 方块原地不动——直连模式下"单个方块挖不掉"就是这个原因。
        if not name or name in self._AIR_NAMES:
            return False
        try:
            block = dimension.get_block_at(int(x), int(y), int(z))
            if block is None:
                return False
            block.set_type("air", True)
            return True
        except Exception as exc:
            self._logger.debug(f"break_block({x},{y},{z}) 失败: {exc}")
            return False

    # -- 扫描 -------------------------------------------------------------

    def scan_blocks(self, actor: Actor, radius: int | None = None,
                    surface_only: bool | None = None) -> list[BlockHit]:
        """扫描周围方块，按类型归并。

        只统计有价值方块，并且只保留每种最近的一个偏移量——
        模型看到"最近的原木在哪"比看到"全部 47 个原木坐标"有用得多，
        而且省掉大量 token。

        `surface_only` 默认取实例属性。为真时**剔除埋在里面的方块**：
        只有六面中至少一面贴着空气/水的才算"看得见"。不加这一刀，
        模型能隔着石头看到钻石，等于给它开了矿透。

        表面判定刻意放在 `classify()` **之后**：`classify` 是纯字符串
        运算（纳秒级），表面判定要读 6 个邻居（微秒级）。先用免费的那刀
        把大部分方块筛掉——空气、无价值方块都在这里就出局了——只有本来
        就是"有价值方块"的才值得付邻居查询的钱。
        """
        r = radius or self.SCAN_RADIUS
        loc = actor.location
        cx, cy, cz = int(math.floor(loc.x)), int(math.floor(loc.y)), int(math.floor(loc.z))
        dimension = actor.dimension
        r2 = r * r
        want_surface = self.surface_only if surface_only is None else surface_only

        # 单次扫描内的方块读缓存。生命周期 = 这一次 scan_blocks，所以
        # 天然不会跨 tick 读到过期方块（用一个长期缓存反而会读到旧方块，
        # 让假人对着空气挖半天才发现）。
        #
        # 把扫描自己读到的 raw 也记进来，邻居查询就能直接命中。这一步
        # 很关键：不加的话最坏要读 294 + 294×6 = 2058 次，加上之后
        # 上界被锁在扫描区外盒 + 一层壳 = 11×11×8 = 968 次。
        memo: dict[tuple[int, int, int], str] = {}

        buckets: dict[str, BlockHit] = {}
        for dx in range(-r, r + 1):
            for dz in range(-r, r + 1):
                if dx * dx + dz * dz > r2:
                    continue
                for dy in range(-self.SCAN_HEIGHT_DOWN, self.SCAN_HEIGHT_UP + 1):
                    x, y, z = cx + dx, cy + dy, cz + dz
                    raw = memo.get((x, y, z))
                    if raw is None:
                        raw = self.get_block_name(dimension, x, y, z)
                        memo[(x, y, z)] = raw
                    if not raw:
                        continue
                    name = classify(raw)
                    if name is None:
                        continue
                    if want_surface and not self.is_exposed(
                            dimension, x, y, z, memo):
                        continue
                    dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                    hit = buckets.get(name)
                    if hit is None:
                        buckets[name] = BlockHit(name=name, off=(dx, dy, dz), dist=dist)
                    else:
                        hit.count += 1
                        if dist < hit.dist:
                            hit.dist = dist
                            hit.off = (dx, dy, dz)

        return sorted(buckets.values(), key=lambda b: b.dist)

    def scan_entities(self, actor: Actor, radius: int = 10) -> list[EntityHit]:
        """扫描附近实体，归类为敌对/被动/其他；玩家单独归类。"""
        out: list[EntityHit] = []
        try:
            for ent in actor.dimension.actors:
                if ent.id == actor.id:
                    continue
                try:
                    d = math.dist(
                        (ent.location.x, ent.location.y, ent.location.z),
                        (actor.location.x, actor.location.y, actor.location.z),
                    )
                except Exception:
                    continue
                if d > radius:
                    continue
                name = short_id(ent.type.id if hasattr(ent.type, "id") else str(ent.type))
                try:
                    epos = (int(math.floor(ent.location.x)),
                            int(math.floor(ent.location.y)),
                            int(math.floor(ent.location.z)))
                except Exception:
                    epos = None
                out.append(EntityHit(name=name, dist=d,
                                     hostile=name in HOSTILE, passive=name in PASSIVE,
                                     pos=epos))
        except Exception as exc:
            self._logger.debug(f"scan_entities 失败: {exc}")
        # 玩家列表走 server（dimension.actors 是否包含玩家随版本漂移，
        # 这里不赌它）。假人自己除外——别让它在感知里看见"自己"。
        if self._players_fn is not None:
            try:
                me = str(getattr(actor, "name", ""))
                origin = (actor.location.x, actor.location.y, actor.location.z)
                for p in self._players_fn():
                    pname = str(getattr(p, "name", ""))
                    if not pname or pname == me:
                        continue
                    try:
                        d = math.dist(
                            (p.location.x, p.location.y, p.location.z), origin)
                    except Exception:
                        continue
                    if d > radius:
                        continue
                    out.append(EntityHit(name=pname, dist=d, player=True))
            except Exception as exc:
                self._logger.debug(f"scan_players 失败: {exc}")
        out.sort(key=lambda e: e.dist)
        return out[:16]

    def scan_inventory(self, actor: Actor, limit: int = 12) -> list[tuple[str, int]]:
        """读取背包摘要。虚拟玩家没有 inventory 属性时返回空。"""
        inv = getattr(actor, "inventory", None)
        if inv is None:
            return []
        out: dict[str, int] = {}
        try:
            for stack in inv.contents:
                if stack is None:
                    continue
                name = short_id(stack.type)
                out[name] = out.get(name, 0) + int(stack.amount)
        except Exception as exc:
            self._logger.debug(f"scan_inventory 失败: {exc}")
            return []
        ranked = sorted(out.items(), key=lambda kv: -kv[1])
        return ranked[:limit]

    # -- 组合 -------------------------------------------------------------

    def snapshot(self, actor: Actor) -> Snapshot:
        """采集一次完整快照。"""
        loc = actor.location
        health = getattr(actor, "health", None)
        max_health = getattr(actor, "max_health", None)
        blocks = self.scan_blocks(actor)
        # 一个方块都没扫到，要分清是"真的在虚空/空中"还是"区块没加载"。
        # 前者是有效观测（假人真悬在空里），后者必须明确告诉模型别乱动。
        loaded = True
        if not blocks:
            loaded = self.is_chunk_loaded(
                actor.dimension, int(loc.x), int(loc.y) - 1, int(loc.z))
        tick = None
        if self._time_fn is not None:
            try:
                tick = self._time_fn()
            except Exception:
                tick = None
        return Snapshot(
            pos=(loc.x, loc.y, loc.z),
            dimension=short_id(getattr(actor.dimension, "name", "overworld")),
            health=health,
            max_health=max_health,
            on_ground=getattr(actor, "is_on_ground", None),
            in_water=bool(getattr(actor, "is_in_water", False)),
            in_lava=bool(getattr(actor, "is_in_lava", False)),
            blocks=blocks,
            entities=self.scan_entities(actor),
            inventory=self.scan_inventory(actor),
            terrain_loaded=loaded,
            daynight=self.daynight_label(tick),
        )

    # -- 移动 -------------------------------------------------------------

    def classify_ground(self, actor: Actor) -> str:
        """给机器人脚下的地形打分，用于纯 Python 寻路。"""
        loc = actor.location
        cx, cy, cz = int(math.floor(loc.x)), int(math.floor(loc.y)), int(math.floor(loc.z))
        dim = actor.dimension
        feet = self.get_block_name(dim, cx, cy, cz)
        below = self.get_block_name(dim, cx, cy - 1, cz)
        if feet in ("lava", "fire") or below in ("lava", "fire"):
            return "deadly"
        if below and "air" not in below:
            return "solid"
        if feet and "air" not in feet and "water" not in feet:
            return "blocked"
        return "open"
