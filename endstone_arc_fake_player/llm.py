"""LLM 客户端与"批量工具"定义。

核心设计：工具是**批量**的，不是逐格的。
-------------------------------
朴素 agent 的做法是每个方块一次工具调用，一面 5x5 的墙要 50 次往返。
这里每个工具都能一次接受一个区域/一条路线，插件在本地展开成逐格步骤。

    build_wall(x1,y1,z1, x2,y2,z2, block)   →  1 次调用，插件执行 N 步
    mine_area(x1,y1,z1, x2,y2,z2)           →  1 次调用，插件执行 N 步
    dig_tunnel(x,y,z, length, direction)    →  1 次调用，插件执行 N 步

token 消耗因此下降一到两个数量级，同时模型的决策复杂度也大幅降低
（它不用再操心"先走到哪一格"这种琐事）。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMReply:
    ok: bool
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    error: str = ""
    native: bool = False


# ---------------------------------------------------------------------------
# 工具白名单与参数规格
# ---------------------------------------------------------------------------

#: name -> (允许的参数: 类型, 是否必填)
#: 类型用于强转，避免模型给出 "10" 还是 10 的不确定性
SPEC: dict[str, dict[str, str]] = {
    # ---- 移动（批量：一次给目的地，插件自己寻路） ----
    "goto":        {"x": "int", "y": "int", "z": "int"},
    "come_to":     {"player": "str"},
    "wander":      {"radius": "int"},
    "tp_player":   {"player": "str"},
    "tp_warp":     {"name": "str"},
    "tp_spawn":    {},
    "jump":        {},
    "stop":        {},

    # ---- 采集（批量：一次给区域，插件逐格挖） ----
    "mine_block":  {"x": "int", "y": "int", "z": "int"},
    "mine_area":   {"x1": "int", "y1": "int", "z1": "int",
                    "x2": "int", "y2": "int", "z2": "int"},
    "dig_tunnel":  {"direction": "str", "length": "int", "height": "int"},
    "mine_vein":   {"block": "str", "max": "int"},
    "collect":     {"block": "str", "count": "int"},
    "chop_tree":   {"x": "int", "y": "int", "z": "int"},

    # ---- 建造（批量：一次给区域，插件按层铺） ----
    "build_box":   {"x1": "int", "y1": "int", "z1": "int",
                    "x2": "int", "y2": "int", "z2": "int", "block": "str"},
    "build_wall":  {"x1": "int", "y1": "int", "z1": "int",
                    "x2": "int", "y2": "int", "z2": "int", "block": "str"},
    "build_floor": {"x1": "int", "y": "int", "z1": "int",
                    "x2": "int", "z2": "int", "block": "str"},
    "place_line":  {"x1": "int", "y1": "int", "z1": "int",
                    "x2": "int", "y2": "int", "z2": "int", "block": "str"},
    "clear_area":  {"x1": "int", "y1": "int", "z1": "int",
                    "x2": "int", "y2": "int", "z2": "int"},

    # ---- 战斗与生存 ----
    "attack":      {"target": "str", "count": "int"},
    "flee":        {"target": "str"},
    "eat":         {},

    # ---- 合成与容器 ----
    # ⚠️ 这四个曾经**漏在 SPEC 里**（只在 tool_spec 出现过），后果很严重：
    # `chat()` 解析时有一道 `if name not in SPEC: continue`，于是模型
    # 就算正确返回了 craft 工具调用，也会被**静默丢弃** → calls 为空 →
    # 日志打出"（没有返回动作）"。用户实测"不会用合成工具"就是这么来的。
    # 教训：`tool_spec()`（给模型看的）与 `SPEC`（解析用的白名单）必须
    # 一一对应，test 里有"同源"断言守着。
    "craft":       {"item": "str"},
    "smelt":       {"item": "str", "count": "int"},
    "store":       {"item": "str", "count": "int"},
    "take":        {"item": "str", "count": "int"},

    # ---- 认知 ----
    "say":         {"message": "str"},
    "goal":        {"text": "str"},
    "remember":    {"text": "str"},
    "note_here":   {"label": "str"},
    "task_done":   {"summary": "str"},

            # ---- 社交与交易 ----
            "give_item":   {"player": "str", "item": "str", "count": "int"},
            "pay_money":   {"player": "str", "amount": "float"},
            # ---- 服务器命令（参与小游戏等插件玩法） ----
            "run_command": {"command": "str"},
}

#: 数值参数的硬边界——防止模型给出把机器人甩到世界外的坐标
BOUNDS = {
    "x": (-30_000_000, 30_000_000), "x1": (-30_000_000, 30_000_000),
    "x2": (-30_000_000, 30_000_000), "z": (-30_000_000, 30_000_000),
    "z1": (-30_000_000, 30_000_000), "z2": (-30_000_000, 30_000_000),
    "y": (-64, 320), "y1": (-64, 320), "y2": (-64, 320),
    "radius": (1, 32), "length": (1, 64), "height": (1, 4),
    "count": (1, 64), "max": (1, 128),
}

#: 允许建造的方块（防止模型用基岩/命令方块搞事）
ALLOWED_BLOCKS = {
    "planks", "oak_planks", "spruce_planks", "birch_planks",
    "cobblestone", "stone", "dirt", "grass_block", "sand", "stone_bricks",
    "glass", "torch", "oak_log", "log", "cobbled_deepslate", "deepslate",
    "sandstone", "bricks", "wool", "white_wool", "oak_fence", "oak_door",
    "ladder", "scaffolding", "chest", "crafting_table", "furnace",
}

_DIRECTIONS = {"north", "south", "east", "west", "up", "down"}


def _norm_block(value: str) -> str | None:
    """把模型给的方块名归一化成 minecraft:xxx 形式。"""
    name = str(value or "").strip().lower().replace(" ", "_")
    if not name:
        return None
    name = name.split(":", 1)[-1]
    # 木材类放宽
    if "plank" in name or "log" in name or "wool" in name:
        return f"minecraft:{name}"
    if name not in ALLOWED_BLOCKS:
        return None
    return f"minecraft:{name}"


def clean_args(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    """按规格裁剪与强转参数。未知参数一律丢弃。"""
    spec = SPEC.get(name)
    if spec is None:
        return {}

    out: dict[str, Any] = {}
    for key, kind in spec.items():
        if key not in raw:
            continue
        value = raw[key]
        if kind == "int":
            try:
                num = int(float(value))
            except (TypeError, ValueError):
                continue
            if key in BOUNDS:
                lo, hi = BOUNDS[key]
                num = max(lo, min(hi, num))
            out[key] = num
        elif kind == "float":
            # 交易金额这类小数。负值对"付款"没有意义，夹到 0。
            try:
                num = round(float(value), 2)
            except (TypeError, ValueError):
                continue
            out[key] = max(0.0, num)
        elif kind == "str":
            text = str(value or "").strip()
            if not text:
                continue
            out[key] = text[:200]

    # 特殊语义校验
    if name in ("build_box", "build_wall", "build_floor", "place_line", "clear_area"):
        if "block" in spec:
            block = _norm_block(out.get("block", ""))
            if block is None:
                return {}
            out["block"] = block

    if name == "dig_tunnel":
        direction = str(out.get("direction", "")).lower()
        if direction not in _DIRECTIONS:
            out["direction"] = "north"
        out.setdefault("length", 8)
        out.setdefault("height", 2)

    if name == "attack" and "target" in out:
        out["target"] = out["target"].lower()

    return out


def missing_required(name: str, args: dict[str, Any]) -> bool:
    """检查必填参数是否齐全。返回 True 表示"缺了必填项，该丢掉这次调用"。

    ⚠️ 这张"可选参数"表必须和两处保持一致，否则会出现
    "提示词让模型这么写、校验却不认"的分裂：

      · `actions.py` 的编译器：能不能处理"没给这个参数"的情况
        （比如 `_do_attack` 里 `if target_name else e.hostile`——
        不给 target 就打最近的敌对生物）
      · 提示词里给模型描述的工具用法

    这里踩过的坑：`optional` 表原先只收 `{"count"}`，没收录
    `attack.target` / `wander.radius` / `flee.target`。而这三个恰恰是
    **明确设计成可以不填**的——`_do_attack` 和 `_do_flee` 都有
    "不给 target 就作用于最近的敌对生物"的分支。

    后果很隐蔽：模型照着提示词写 `{"op":"attack"}`（危机关头想打
    最近的僵尸），这里判它 `missing_required=True` 直接丢弃，
    模型只能看到"参数不合法"，它没有任何线索知道问题出在哪——
    因为按提示词文档这么写本来就是对的。那条分支**永远不可达**。
    """
    spec = SPEC.get(name)
    if spec is None:
        return True
    # 有默认值的可不填。
    #
    # 收录标准：**编译器对该参数缺省有明确、合理的兜底行为**。
    #   dig_tunnel  length/height → 默认 8 / 2
    #   mine_vein   max           → 不限
    #   collect     count         → 默认数量
    #   attack      target        → 缺省 = 最近的敌对生物
    #   attack      count         → 默认攻击次数
    #   wander      radius        → 缺省 = 默认漫游半径
    #   flee        target        → 缺省 = 最近的敌对生物
    optional = {
        "dig_tunnel": {"length", "height"},
        "mine_vein": {"max"},
        "collect": {"count"},
        "attack": {"count", "target"},
        "wander": {"radius"},
        "flee": {"target"},
        # 合成/容器：编译器里 count 缺省有兜底（`max(1, int(a.get("count", 1)))`），
        # 所以不填 count 是合法的，别把这种调用误判成"缺必填项"丢掉。
        "smelt": {"count"},
        "store": {"count"},
        "take": {"count"},
    }
    skip = optional.get(name, set())
    for key in spec:
        if key in skip:
            continue
        if key not in args:
            return True
    return False


# ---------------------------------------------------------------------------
# JSON 兜底解析
# ---------------------------------------------------------------------------

def _iter_json_values(text: str) -> list[Any]:
    """按括号配对，从文本里抠出**所有**顶层 JSON 对象/数组。

    为什么不能直接 `json.loads(整段)`：模型在"正文里写 JSON 动作"这条路
    上，输出常常是**不含外壳、还用逗号连起来的一串对象**，例如：

        {"name": "craft", "args": {"item": "planks"}},
        {"name": "craft", "args": {"item": "stick"}}

    这不是一个合法的 JSON 文档（两个对象裸拼），整段解析必然失败，
    `find("{")…rfind("}")` 那种截取也会截出一个非法的中间串。逐对象扫描
    才能把里面每个动作都取出来。字符串里的括号/引号会被正确跳过。
    """
    out: list[Any] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i] not in "{[":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c in "{[":
                    depth += 1
                elif c in "}]":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        if depth != 0:            # 括号没配对（正文里有孤立的 {），跳过
            i += 1
            continue
        try:
            out.append(json.loads(text[i:j + 1]))
        except json.JSONDecodeError:
            pass
        i = j + 1
    return out


def extract_actions(text: str) -> tuple[str, list[dict[str, Any]]]:
    """从一段模型文本里尽力抽出 `(要说的文字, 动作列表)`。

    接受**所有**实测遇到过的形态（越宽松越好，这里是"兜底"不是"校验"）：

      1. `{"reply": "...", "actions": [{"name":…, "args":{…}}, …]}`
      2. `{"actions": [...]}` / `{"calls": [...]}`（没有 reply）
      3. 裸数组 `[{...}, {...}]`
      4. 单个裸对象 `{"name":…, "args":{…}}`
      5. 正文里用逗号连起来的多个裸对象（**不是**合法 JSON 文档）
      6. 带 ```json 围栏的以上任意一种

    名字/参数合不合法由调用方用 `SPEC` 过滤，这里只负责"抠出来"。
    """
    value = str(text or "").strip()
    if not value:
        return "", []
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)

    say = ""
    actions: list[dict[str, Any]] = []

    def _push(items: Any) -> None:
        if not isinstance(items, list):
            return
        for it in items:
            if isinstance(it, dict):
                actions.append(it)

    for obj in _iter_json_values(value):
        if isinstance(obj, list):
            _push(obj)
        elif isinstance(obj, dict):
            if "actions" in obj or "calls" in obj:
                if not say:
                    say = str(obj.get("reply") or obj.get("say") or "").strip()
                _push(obj.get("actions"))
                _push(obj.get("calls"))
            elif "name" in obj:
                actions.append(obj)          # 裸动作对象
            elif not say:
                say = str(obj.get("reply") or obj.get("say") or "").strip()

    # 极简情况（整段就是一个 {"reply": "..."}）没被上面覆盖时再补一次。
    if not actions and not say and value[:1] in "{[":
        try:
            data = json.loads(value)
            if isinstance(data, dict):
                say = str(data.get("reply") or data.get("say") or "").strip()
                _push(data.get("actions") or data.get("calls"))
            elif isinstance(data, list):
                _push(data)
        except json.JSONDecodeError:
            pass

    return say, actions


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class LLMClient:
    """OpenAI 兼容客户端。同步阻塞——调用方必须放进线程池。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: float = 45.0, native_tools: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = max(5.0, min(180.0, float(timeout)))
        self.native_tools = native_tools
        #: 最近一次请求的 HTTP 状态码。0 表示"根本没拿到响应"
        #: （网络层失败）。`_post` 写、`chat` 读，用来区分
        #: "服务端拒绝 tools" 和 "网络不通"——这两者的处理方式
        #: 完全不同，混在一起会导致原生工具调用被误关。
        self.last_http_status = 0

    def configured(self) -> bool:
        key = (self.api_key or "").strip()
        return bool(self.base_url and self.model and key) and key.lower() not in {
            "xxx", "changeme", "sk-xxx", "none", "your-key",
        }

    @staticmethod
    def tool_spec() -> list[dict[str, Any]]:
        """生成 function-calling 规格。

        注意每个描述都刻意强调"这是批量操作"——模型知道能一次干一片，
        就不会退化成一格一格地干活。
        """

        def f(name: str, desc: str, props: dict, required: list[str]) -> dict:
            return {"type": "function", "function": {
                "name": name, "description": desc,
                "parameters": {"type": "object", "properties": props, "required": required},
            }}

        I = {"type": "integer"}
        S = {"type": "string"}

        return [
            # --- 移动 ---
            f("goto", "走到指定世界坐标。内部会自动寻路绕开障碍与岩浆，你只需给出目的地。",
              {"x": I, "y": I, "z": I}, ["x", "y", "z"]),
            f("come_to", "走到某个在线玩家身边。", {"player": S}, ["player"]),
            f("tp_player",
              "向某个在线玩家**申请**传送（tpa）：对方会收到接受/拒绝表单，"
              "接受后你才传送过去并扣互传费用。想立刻去某地标用 tp_warp。",
              {"player": S}, ["player"]),
            f("tp_warp",
              "传送到公共传送点/地标（服务器传送系统里的名字，比如主城、"
              "出生点附近的地标）。名字不对会返回可用列表。",
              {"name": S}, ["name"]),
            f("tp_spawn", "传送到世界出生点。", {}, []),
            f("wander", "在当前区域随机走动探索。", {"radius": I}, []),
            f("jump", "原地跳一下。卡住时可用。", {}, []),
            f("stop", "立刻停止一切正在进行的动作。", {}, []),

            # --- 采集（批量）---
            f("mine_block",
              "挖掉指定坐标的单个方块（比如眼前一块矿石、一截原木）。"
              "只有明确要挖某一块时才用；挖一整片用 mine_area。",
              {"x": I, "y": I, "z": I}, ["x", "y", "z"]),
            f("mine_area",
              "批量挖空一个长方体区域内的所有方块。参数是区域的两个对角坐标。"
              "一次调用就能挖掉一整面墙或一整片矿区，不要逐格调用。",
              {"x1": I, "y1": I, "z1": I, "x2": I, "y2": I, "z2": I},
              ["x1", "y1", "z1", "x2", "y2", "z2"]),
            f("dig_tunnel",
              "朝指定方向挖一条隧道。direction 取 north/south/east/west/up/down，"
              "length 是长度(1-64)，height 是高度(1-4，默认2)。这是挖矿最高效的方式。",
              {"direction": S, "length": I, "height": I}, ["direction"]),
            f("mine_vein",
              "自动寻找并挖掉附近某种方块(如 diamond_ore、iron_ore)，最多挖 max 个(默认16)。"
              "适合找矿：一次调用就能把附近所有矿石收入囊中。",
              {"block": S, "max": I}, ["block"]),
            f("collect",
              "收集指定类型的方块若干块(如 log、cobblestone)，自动找最近的去挖，"
              "挖满数量即停。不要手动一格一格挖。",
              {"block": S, "count": I}, ["block"]),
            f("chop_tree",
              "把指定坐标处的整棵树砍倒（自动把相连的原木和树叶一起处理）。",
              {"x": I, "y": I, "z": I}, ["x", "y", "z"]),

            # --- 建造（批量）---
            f("build_box",
              "用指定方块填充一个长方体区域（实心）。参数是两个对角坐标和方块名，"
              "如 block=\"cobblestone\"。一次调用完成整栋建筑，不要一块一块放。",
              {"x1": I, "y1": I, "z1": I, "x2": I, "y2": I, "z2": I, "block": S},
              ["x1", "y1", "z1", "x2", "y2", "z2", "block"]),
            f("build_wall",
              "砌一面墙（薄板，只在给定区域的一个面上）。适合围围墙、隔房间。",
              {"x1": I, "y1": I, "z1": I, "x2": I, "y2": I, "z2": I, "block": S},
              ["x1", "y1", "z1", "x2", "y2", "z2", "block"]),
            f("build_floor",
              "铺一层地板。y 是高度，x1/z1 到 x2/z2 是水平范围。",
              {"x1": I, "y": I, "z1": I, "x2": I, "z2": I, "block": S},
              ["x1", "y", "z1", "x2", "z2", "block"]),
            f("place_line",
              "沿直线摆放一串方块（造柱子、铺路、架桥）。",
              {"x1": I, "y1": I, "z1": I, "x2": I, "y2": I, "z2": I, "block": S},
              ["x1", "y1", "z1", "x2", "y2", "z2", "block"]),
            f("clear_area",
              "清空一个区域内的所有方块，把它变成空气。适合平整地面、开洞穴。",
              {"x1": I, "y1": I, "z1": I, "x2": I, "y2": I, "z2": I},
              ["x1", "y1", "z1", "x2", "y2", "z2"]),

            # --- 战斗与生存 ---
            f("attack", "攻击附近的生物。target 是生物名如 zombie、cow；count 是攻击次数(默认5)。"
                        "不填 target 则攻击最近的敌对生物。",
              {"target": S, "count": I}, []),
            f("flee", "逃离指定生物或最近的敌对生物，往反方向跑一段。",
              {"target": S}, []),
            f("eat", "吃掉背包里的食物恢复饥饿值。", {}, []),

            # --- 合成与容器 ---
            f("craft", "合成物品。item 取：stick(木棍)、planks(木板)、crafting_table(工作台)、"
                        "furnace(熔炉)、chest(箱子)、torch(火把)、wooden_pickaxe(木镐)、"
                        "wooden_axe(木斧)、wooden_sword(木剑)、stone_pickaxe(石镐)、"
                        "iron_pickaxe(铁镐)、bread(面包)、shield(盾牌)。"
                        "需要附近有工作台；会自己去工作台前把材料摆好取出成品。",
              {"item": S}, ["item"]),
            f("smelt", "熔炼物品。item 取：iron_ore(铁矿石→铁锭)、raw_iron、gold_ore、sand(→玻璃)、"
                        "cobblestone(→石头)、log(→木炭)。count 是要熔炼的数量。"
                        "需要附近有熔炉和燃料（煤/木炭）。",
              {"item": S, "count": I}, ["item"]),
            f("store", "把背包里的物品存进附近箱子。item 是物品名，count 是数量。",
              {"item": S, "count": I}, ["item"]),
            f("take", "从附近箱子取出物品到背包。item 是物品名，count 是数量。",
              {"item": S, "count": I}, ["item"]),

            # --- 认知 ---
            f("say", "在聊天栏说话，可以和玩家交流。", {"message": S}, ["message"]),
            f("goal", "更新你的长期目标。新目标会持续提醒你。", {"text": S}, ["text"]),
            f("remember", "记住一条对生存有用的信息。", {"text": S}, ["text"]),
            f("note_here", "把当前位置记为某个地点，比如\"家\"、\"矿洞入口\"。",
              {"label": S}, ["label"]),
            f("task_done", "当前目标已完成或无法继续时调用，并说明情况。", {"summary": S}, ["summary"]),

            # --- 社交与交易 ---
            f("give_item",
              "把背包里的物品丢给某个玩家（交易时交付货物）。"
              "对方要在 8 格内；物品会真的从你背包扣除并掉在两人中间，"
              "对方捡起即得。item 用代码名如 iron_ingot。",
              {"player": S, "item": S, "count": I}, ["player", "item"]),
            f("pay_money",
              "用金币向玩家付款（交易时付钱）。会从你的钱包真实扣除并"
              "进入对方账户；余额不足会失败。",
              {"player": S, "amount": {"type": "number"}}, ["player", "amount"]),

            # --- 服务器命令（小游戏等） ---
            f("run_command",
              "以你自己的玩家身份执行一条服务器命令（不带斜杠）。"
              "你只有普通玩家权限。常用于参与服务器小游戏："
              "gs quick=快速加入枪战游戏（没房自动建房随机地图模式，"
              "有房立刻加入；进对局后身体会**自动战斗**——找最近敌人、"
              "追击、视线内开枪，你负责走位决策之外的喊话和退出），"
              "gs leave=离开枪战对局。",
              {"command": S}, ["command"]),
        ]

    # -- 调用 -------------------------------------------------------------

    @staticmethod
    def tool_manual() -> str:
        """把 `tool_spec()` 渲染成一段紧凑的文本**动作手册**（写进提示词）。

        为什么非要写进提示词：原生 function-calling 完全依赖服务端/模型
        真的支持 `tools` 参数。可**大量第三方中转、以及不少小模型会静默
        忽略 tools** —— 返回 200，message 里既没有 tool_calls，也从不提
        工具。这时模型手里只剩系统提示词；如果提示词里没有动作清单，它就
        只能"干说话不干活"（日志里一条条"（没有返回动作）"就是这么来的）。

        把清单写进提示词，等于给这条路兜底：即便原生工具调用不生效，模型
        照样知道自己能做什么。尤其是 `craft`/`smelt`/`say` 这类**不在批量
        规则里**的动作——只靠 tools 参数时它们最容易被漏掉（用户实测"不会
        用合成工具"多半就是这个）。

        渲染来源就是 `tool_spec()`，与原生工具**同源**，不会两处漂移。
        """
        lines = [
            "# 你能调用的动作（用它们真正行动，不要只描述）",
            "优先使用原生工具调用（function calling）。",
            "**如果你的接口返回不了工具调用**，就只输出一个 JSON 对象、"
            "不要有任何多余文字：",
            '{"reply": "你要说的话", "actions": '
            '[{"name": "动作名", "args": {"参数": 值}}]}',
            "可用动作（参数带 ? 的表示可选）：",
        ]
        for item in LLMClient.tool_spec():
            fn = item.get("function") or {}
            params = fn.get("parameters") or {}
            props = params.get("properties") or {}
            required = set(params.get("required") or [])
            args = ", ".join(
                (k if k in required else k + "?") for k in props
            )
            desc = " ".join(str(fn.get("description") or "").split())
            # 截到 72 字：既短，又能保住 craft 那种"item 取：…"的枚举开头
            # （配方细节在"生存玩法速查"里另有，不必在这里重复）。
            if len(desc) > 72:
                desc = desc[:72] + "…"
            lines.append(f"- {fn.get('name')}({args})：{desc}")
        return "\n".join(lines)


    def chat(self, messages: list[dict[str, Any]], temperature: float = 0.3,
             max_tokens: int = 800) -> LLMReply:
        if not self.configured():
            return LLMReply(ok=False, error="LLM 未配置")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": max(0.0, min(2.0, float(temperature))),
            "max_tokens": max(64, min(4096, int(max_tokens))),
        }
        if self.native_tools:
            payload["tools"] = self.tool_spec()
            payload["tool_choice"] = "auto"

        data = self._post(payload)
        if data is None:
            # ⚠️ 只有**明确的** "服务端不接受 tools 参数" 才降级。
            #
            # 这里踩过一个很贵的坑：原先是
            #     if data is None and self.native_tools:
            #         self.native_tools = False
            # 而 `_post` 对**所有**异常（超时、连接被拒、DNS 失败、
            # 5xx）都返回 None。于是只要发生一次瞬时网络抖动，这个
            # 客户端就会**永久**退回 JSON 兜底模式——模型从此再也
            # 用不上原生工具调用，而且没有任何提示。
            #
            # 更糟的是 `native_tools` 是个共享字段，多个 agent 的
            # 后台线程会同时读写它，一旦被置 False 就再也回不去。
            #
            # 现在只在 HTTP 400/404/422 这类"请求本身不被接受"的
            # 状态码上降级——那才真的可能是服务端不认识 tools。
            # 网络类故障直接报告失败，交给上层的重试退避去处理
            # （`_on_llm_failure` 会等 llm_retry_seconds 再试）。
            if self.native_tools and self.last_http_status in (400, 404, 422):
                self.native_tools = False
                print(f"[Agent] 服务端不接受 tools（HTTP {self.last_http_status}），"
                      f"改用 JSON 模式")
                return self.chat(messages, temperature, max_tokens)
            return LLMReply(ok=False, error=self._fail_reason())

        choices = data.get("choices") or []
        if not choices:
            return LLMReply(ok=False, error="返回为空")
        message = choices[0].get("message") or {}

        # 原生 tool_calls
        raw_calls = message.get("tool_calls") or []
        if raw_calls:
            calls: list[ToolCall] = []
            for item in raw_calls[:6]:
                fn = item.get("function") or {}
                name = str(fn.get("name", "")).strip()
                if name not in SPEC:
                    continue
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                cleaned = clean_args(name, args)
                if missing_required(name, cleaned):
                    continue
                calls.append(ToolCall(name, cleaned))
            if calls:
                return LLMReply(ok=True, text=str(message.get("content") or "").strip()[:400],
                                calls=calls, native=True)

        # JSON 兜底：模型在正文里写 JSON 动作（原生工具调用不可用时的出路）。
        # 用宽容的 `extract_actions`，别再用"必须有 {reply,actions} 外壳"的
        # 老逻辑——实测模型经常只吐一串裸对象，会被老逻辑整段丢掉。
        content = str(message.get("content") or "")
        say, raw_actions = extract_actions(content)
        calls: list[ToolCall] = []
        for item in raw_actions[:6]:
            name = str(item.get("name", "")).strip()
            if name not in SPEC:
                continue
            args = item.get("args")
            if not isinstance(args, dict):
                args = {k: v for k, v in item.items() if k != "name"}
            cleaned = clean_args(name, args)
            if missing_required(name, cleaned):
                continue
            calls.append(ToolCall(name, cleaned))
        if calls:
            return LLMReply(ok=True, text=say[:400], calls=calls)
        # 一个动作都没解析出来：把原文当"说"的话交回上层（会打"没有返回动作"）
        return LLMReply(ok=True, text=(content.strip() or say)[:400])

    def _fail_reason(self) -> str:
        """把 `last_http_status` 翻译成**人能看懂**的失败原因。

        以前不管什么错都只回一句"请求失败"，用户看到的就是日志里孤零零
        一行 `LLM HTTP 429` —— 既不知道 429 是"被限流、等一会就好"，
        也不知道 401 是"Key 不对"。分门别类之后，用户至少能自己判断
        是改配置还是等一会。
        """
        st = self.last_http_status
        if st == 429:
            return "被接口限流（HTTP 429），等待后自动重试"
        if st in (401, 403):
            return f"鉴权失败（HTTP {st}），请检查 API Key"
        if st == 404:
            return "接口地址不对（HTTP 404），请检查 base_url 是否带 /v1"
        if st >= 500:
            return f"服务端错误（HTTP {st}），稍后重试"
        if st:
            return f"HTTP {st}"
        return "请求失败（网络不通或超时）"

    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """发一次请求。成功返回解析后的 JSON，失败返回 None。

        ⚠️ 同时把失败原因记进 `self.last_http_status`，因为上层需要
        区分两种完全不同的失败：

          · "服务端不认识 tools 参数"（HTTP 400）→ 该降级到 JSON 模式
          · "网络不通/超时/5xx"            → 只是这次没成功，别改配置

        以前这两者都表现为 `return None`，上层只能瞎猜，把瞬时网络
        抖动当成了"服务端不支持 tools"，于是**永久**关掉了原生
        工具调用（见 `chat` 里的注释）。
        """
        self.last_http_status = 0
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self.last_http_status = int(exc.code)
            print(f"[Agent] LLM HTTP {exc.code}")
            return None
        except Exception as exc:
            print(f"[Agent] LLM 异常: {exc}")
            return None


def build_tool_manual() -> str:
    """`LLMClient.tool_manual()` 的模块级入口（提示词拼接用）。

    单独包一层是为了让 `agent.build_system` 不必先构造一个 client——
    它只需要那份**与原生工具同源**的文本清单。
    """
    return LLMClient.tool_manual()
