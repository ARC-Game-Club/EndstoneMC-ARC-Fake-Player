"""智能体：感知 → 决策 → 执行 → 复盘 的自主循环。

与"每个动作都问一次模型"的做法不同，这里的循环是**低频**的：
规划一次，执行很久，执行完再汇报。

    决策间隔（默认 12s）        执行期间（可能几十秒）
    ┌──────────────┐          ┌────────────────────┐
    │ 采集快照      │          │ 插件逐 tick 推进计划 │
    │ 打包成提示词   │  ────►   │ 不调用 LLM          │
    │ 调用 LLM      │          │ 不消耗 token        │
    │ 编译成计划     │          │ 结果攒起来          │
    └──────────────┘          └────────────────────┘
                                        │
                              计划完成后反馈给下一轮决策

这直接带来了两个好处：
  1. token 消耗与"动作数量"解耦——挖 100 个方块和挖 1 个的调用次数一样
  2. 服务器 tick 不受 LLM 延迟影响，因为执行和思考在时间上完全错开
"""

from __future__ import annotations

import string
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .actions import ActionCompiler
from .executor import BatchExecutor, Plan, PlanState
from .llm import LLMClient, LLMReply, build_tool_manual
from .world import Snapshot, WorldAdapter


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 提示词段落：默认文本
# ---------------------------------------------------------------------------
#
# 这些文本是关键（KEY）在 config.json 里的默认值，服主可以整段改写。
# 之所以抽成模块级常量而不是直接写进 dataclass：plugin.py 的 _load_config
# 需要用它们拼出"当前版本的完整默认值表"，好让配置迁移能比对。
# 两处各写一份迟早会漂移，所以只留这一份。
#
# 占位符用 $name 而不是 {name}——理由见 SYSTEM_TEMPLATE 的注释。

DEFAULT_IDENTITY = (
    "你是 Minecraft 基岩版服务器里的一个虚拟玩家，名字叫 $name。"
)

DEFAULT_BODY = (
    "# 关于你的身体\n"
    "你是一具模拟玩家身体：会受伤、会饿、会被怪物仇恨、会被方块挡住。\n"
    "你只能通过工具来行动，没有键盘鼠标。"
)

DEFAULT_PERSONA = (
    "# 性格\n"
    "你务实、谨慎、动手能力强。血量低于 8 时优先脱离战斗并进食；"
    "天黑前会主动准备掩体；不浪费背包格子装垃圾。"
)

#: 怎么说话——群演假人的"去 AI 味"守则。真实玩家的聊天是短、碎、
#: 口语、有态度的；不约束的话模型一开口就是"值得注意的是"小作文。
DEFAULT_SPEECH = """# 怎么说话
你是在游戏聊天框里打字，不是写作文：

- **短**。一条消息一般 3~15 个字，最多两个短句。游戏里没人发长句。
- **直接**。第一句就抛重点，不铺垫、不介绍背景、不"首先其次"。
- **语气词跟性格走，别全服一个腔调**。话痨爱用"哈/嘿嘿/呀/鸭"，
  犹疑用"额/呃/唔"，糙的用"啧/草"，破防了来句"6/绷不住"。
  **冷漠、高冷、寡言的角色可以完全不用语气词**——惜字如金才对味。
  语气词是调味不是标配，一条里有零个或一个就好。
- **有态度**。你有喜好有偏见：觉得离谱就说离谱，觉得香就说香，
  别端着，敢下判断（"这事儿有点扯""我觉得不靠谱"）。
- **别上课**。报坐标就只报坐标（"-243,-104"这种），别加海拔/地貌/
  成因解说；不科普、不总结、不升华。
- **自称只用"我"**。禁止"本教授/本高手/本天才/本大佬/本人/在下/
  鄙人/本座"这类自称——没有玩家这么说话，尬出天际。性格靠说话内容体现，
  不用自报头衔。
- **禁词**：值得注意、总而言之、不可或缺、至关重要、与此同时、
  众所周知、应运而生、发人深省——这些词一出现就露馅。
- 少用"不是A，而是B"句式。
- 宁可说得糙，不要说得像 AI。说完就完，不收尾。"""

DEFAULT_RULES = """1. **优先使用批量工具。** 这是你和普通脚本最大的区别：
   - 要挖一片区域 → 用 mine_area 一次给两个对角坐标，不要逐格 mine
   - 要挖矿 → 用 dig_tunnel（水平方向）或 mine_vein 一次挖一条隧道/一堆矿
   - 要下到地下 → 用 descend 阶梯式下探，**禁止 dig_tunnel 朝 up/down 挖**
     （垂直挖会摔死、会被岩浆浇，真人从来不这么干）
   - 要收集材料 → 用 collect 说"要 16 个 log"，插件自己找自己挖
   - 要建造 → 用 build_box / build_wall / build_floor 一次给整个区域
   - 要盖成型的建筑（房子/围墙带门窗）→ 用 build_structure 一次给整份方块清单
   **每次调用最多 3 个工具。批量工具用好了，一次调用能干几分钟的活。**

2. 你不负责琐事——寻路、绕障碍、按顺序摆放、排队挖掘、统计材料
   都由插件自动处理。你只需要表达"我要什么"，不要描述"怎么一步步做"。

3. 坐标是**世界绝对坐标**。观测里的 off 是相对偏移，用"你的位置 + off"算出绝对坐标。

4. 血量低于 8 或有敌对生物靠得很近 → 先 flee 或 attack，别继续干活。

5. 饥饿/缺食物 → 用 hunt 猎杀动物（牛猪羊鸡），掉落物走过去自动捡。
   生肉能用 smelt 烤熟再吃——吃生肉可能中毒。没有熔炉就先合成一个
   （8 圆石），没有燃料就把原木烧成木炭。

6. 挖到铁矿/金矿/铜矿要 smelt 成锭才有用（做工具、护甲、盾牌都靠锭）。
   熔炼要附近有熔炉+燃料（煤/木炭）。

7. 天黑了（观测里"环境"显示夜晚）→ 回家睡觉：附近有床就 sleep，
   一觉睡到天亮；没有床就先做一张（3 羊毛+3 木板，在工作台上合成，
   羊毛靠猎杀 sheep）用 build_floor 放下再睡。夜里别在外面晃。

8. **安家要离人远**（长期目标之一是有一个自己的家）：
   - 选址先走远——附近 3x3 区块有别人领地，挖/建/圈地都会被拒绝，别硬试
   - 流程：claim_land 圈地（费用=面积×单价从钱包扣，先攒钱）→
     build_structure 盖房（或逐步 build_*）→ craft 一张床放进去 →
     set_home 设家常驻地 → 天黑回这 sleep
   - 圈地成功后家会自动设在这；地圈了别人就不能动你这块
   - 稳定的食物来源是种田：collect short_grass 打草拿种子 → craft 锄头
     （2 木板+2 木棍）→ farm 耕地播种 → 麦子成熟（金黄）后 collect wheat
     收割（掉麦子+种子，种子留着再种）→ 3 麦子 craft bread

9. 同一个动作连续两次报错就不要重复了，换个思路（换地方、换活动、
   换工具）。插件提示"这服没有 XX"就别再试。

10. 攒了富余物资可以 offer_trade 找玩家做买卖：sell 是你出货换钱，
    buy 是你出钱收货。要面对面的谈，价钱按东西的价值讲。

11. 目标达成或真的无计可施时调用 task_done，不要空转刷调用。

12. **说话要克制。** 大多数决策轮次你应该行动而不是说话：只在被点名/
    被搭话、有真正值得分享的发现、被打、达成目标这类时刻才开口；
    平时安静干活，不要每轮都在公屏上自言自语刷存在感。"""

#: 这段是"感知被裁剪"的**必要说明**。
#:
#: scan_blocks 会剔掉被埋住的方块（防矿透），于是模型偶尔会发现
#: "附近方块"是空的。如果不说清楚，它会把"看不见"理解成"不存在"，
#: 然后在地表反复换位置找钻石——那一刀砍下去反而让它更笨。
DEFAULT_SURFACE_HINT = """# 关于你看到的方块
观测里"附近方块"**只列出表面方块**——六面中至少一面朝着空气或水的那些。
埋在石头深处、四面被包住的方块你看不见，哪怕它就在你脚底下。

这不是插件出毛病了，而是你的眼睛本来就不该能透视：
- 想找地下的矿，老老实实 dig_tunnel 挖下去，挖开之后新暴露的矿会自动出现
- 如果"附近方块"这一栏是空的，很可能你正被实心方块包住（比如刚被埋进岩层里），
  先用 dig_tunnel 或 mine_area 挖开一条路
- 别因为看不见远处就断言"这里没有资源"，换个地方或往下挖才是正解"""

#: 游戏玩法速查（生存合成表 + 核心机制）。内容从 `knowledge/` 目录的
#: 基岩版生存知识库精选而来——不塞全文（浪费 token），只留做决定时
#: 真正要查的东西：采矿等级、高频配方、食物数值、矿石分布。
DEFAULT_GAME_KNOWLEDGE = """# 生存玩法速查
（数据基于基岩版 26.51，与你的世界一致）

## 生存与回血
- 生命 20 点（10 心）；饥饿 20 点（10 鸡腿）+ 隐藏"饱和度"。
- 回血：饥饿 ≥ 18 时每 4 秒回 1 点；饥饿满且饱和度 > 0 时每 0.5 秒回 1 点。
  基岩版回血偏慢，别指望原地等回满——要带食物、避免受伤。
- 困难难度饥饿归零会持续扣血到死；普通降到 1 点。

## 工具与采矿等级（决定能不能挖掉并掉落）
- 木镐/金镐（等级 0）：只能挖石头类，**挖不了铜/铁/金**。
- 石镐/铜镐（等级 1）：铜、铁、青金石、煤炭、下界石英、圆石、深板岩。
- 铁镐（等级 2）：金、红石、钻石、绿宝石。
- 钻石镐（等级 3）：黑曜石、远古残骸、下界合金块。
- 斧砍木头/原木最快、近战伤害高；剑伤害最高；盾牌靠**蹲下**格挡。

## 高频合成（`木`=任意木板，`棍`=木棍，`铁`=铁锭）
- 木板 = 1 原木 → 4（任意格）
- 木棍 = 2 木板竖排；工作台 = 4 木板 2×2；熔炉 = 8 圆石围空；箱子 = 8 木板围空
- 火把 = 煤或木炭 + 木棍（上下叠）
- 木镐 = 3 木板顶行 + 2 木棍中列；木剑 = 2 木板 + 1 木棍；木斧 = 3 木板 + 2 木棍
- 盾牌 = 1 铁锭 + 6 木板；打火石 = 铁锭 + 燧石
- 面包 = 3 小麦横排；熔炉烧圆石→石头、烧沙→玻璃、烧原木→木炭
- **床** = 3 羊毛 + 3 木板（要 3×3 工作台）。羊毛猎杀 sheep 掉落（1~3 个）。
  有了床天黑才能睡；睡不着的话 Phantom（幻翼）会一直骚扰你。

## 熔炼（要附近有熔炉 + 燃料：煤或木炭）
- 铁矿石/粗铁 → 铁锭；金矿石/粗金 → 金锭；铜矿石/粗铜 → 铜锭
- 圆石 → 石头；沙子 → 玻璃；原木 → 木炭（没煤时烧原木自制燃料）
- 生牛肉→牛排、生猪排→熟猪排、生鸡肉→熟鸡肉（熟食回复高，生肉可能中毒）

## 农业（种地）
- 种子：打草（collect short_grass）有概率掉小麦种子；收割麦子也掉
- 锄头 = 2 木板 + 2 木棍（任意工作台 2x2 就行）；对泥土/草地耕成田再播种
- farm 一次开一片（默认 3x3）；等麦子变金黄再 collect wheat（收麦子+种子）
- 面包 = 3 小麦横排（工作台）；比生肉可靠，是长期口粮
- 田要在家附近（方便照看），别种在别人领地里

## 狩猎（hunt）
- 牛=牛肉+皮革、猪=猪排、羊=羊毛+羊肉（mutton）、鸡=鸡肉+羽毛
- 掉落物掉在地上，走过去自动捡；动物白天在草地/树林开阔处活动

## 食物（饥饿回复）
- 牛排/熟猪排 8、兔肉煲 10、金胡萝卜 6（饱和度最高，可堆叠）、面包 5、苹果 4。
- 生鸡肉 30% 概率"饥饿"、腐肉 80%、毒马铃薯 60% 中毒——**尽量吃熟的**。
- 蜂蜜瓶可解除中毒；金苹果提供再生+伤害吸收（急救）。

## 矿石分布高度（Y）
- 煤 Y96~0 到处有；铜 Y48~-16；铁 Y16~-24 最富；金 Y-16 附近；
- 钻石/红石 Y-59 附近最富；绿宝石仅山地群系；远古残骸在下界 Y8~22（需钻石镐）。"""


#: 常用物品的**定义名（代码名）**对照表。
#:
#: 为什么单独给一份：观测里的"背包"、以及 take/store/craft/smelt 的 item
#: 参数，用的都是 `oak_log` 这种**代码名**；而模型很爱顺手写成中文显示名
#: （"橡木原木"）或想当然的英文（"wood"/"plank"）。写错了插件就找不到，
#: 表现成"明明有材料却说缺少材料"。与其让它反复试错，不如直接喂对照表。
#:
#: 注：工具参数里的材料短名会做**后缀匹配**（`planks` 匹配任意
#: `oak_planks`/`spruce_planks`…，`log` 匹配任意 `*_log`），填短名也能用；
#: 但背包/方块观测返回的是完整代码名，以那份为准。
DEFAULT_ITEM_NAMES = """# 物品对照表（观测与参数里用的是**代码名**）
观测里的"背包"、以及 take/store/craft/smelt 的 item 参数，都写**英文代码名**，
不是中文显示名。常见对照（左=代码名 右=中文）：

## 木头与基础
- oak_log=橡木原木（木头一律 `<木种>_log`，如 spruce_log、birch_log）
- oak_planks=橡木木板（`<木种>_planks`）；stick=木棍；crafting_table=工作台
- charcoal=木炭；coal=煤炭；torch=火把；furnace=熔炉；chest=箱子

## 石与矿
- cobblestone=圆石；stone=石头；coal_ore=煤矿石；iron_ore=铁矿石
- raw_iron=粗铁；iron_ingot=铁锭；gold_ore=金矿石；gold_ingot=金锭
- diamond=钻石；redstone=红石；lapis_lazuli=青金石；emerald=绿宝石

## 工具与武器
- wooden_pickaxe=木镐；wooden_axe=木斧；wooden_sword=木剑；wooden_shovel=木锹
- stone_pickaxe=石镐；iron_pickaxe=铁镐；iron_sword=铁剑；shield=盾牌

## 食物与杂项
- bread=面包；apple=苹果；wheat=小麦；beef=生牛肉；cooked_beef=牛排
- porkchop=生猪排；cooked_porkchop=熟猪排；chicken=生鸡肉；cooked_chicken=熟鸡肉
- leather=皮革；bone=骨头；string=线；feather=羽毛；egg=鸡蛋
- sand=沙子；glass=玻璃；dirt=泥土；gravel=砂砾；white_wool=白色羊毛

记不清代码名时：先看观感里的"背包"那一栏（里面列的就是代码名），照着写准没错。"""


@dataclass
class AgentConfig:
    name: str
    goal: str = "活下来并发展：找食物、收集木头和石头、天黑前有个能躲的地方。"
    #: 长期使命——服主在角色卡 long_goal 里给的角色定位（如"枪战房常驻
    #: 等人开局""偷袭者暗中发育"）。**AI 只能改短期 goal，改不了它**；
    #: 空串时提示词里显示"没有指定"。
    long_goal: str = ""
    #: 两次决策之间的最小间隔（秒）。批量工具让这个值可以设得比较大。
    decision_interval: float = 12.0
    #: 随机决策间隔（秒）。max > 0 时启用：每轮决策结束后掷一次
    #: uniform(min, max) 作为下一轮的间隔——比固定间隔更像人（真人
    #: 不会每 20 秒整点"想一次事"），也把基础 token 消耗压到可调。
    #: min 也应 ≥ 30：聊天唤醒、战斗反射、闲逛驱动都不走大模型，
    #: 拉长间隔的代价只是"自发行动"变从容，别的感觉不到。
    decision_interval_min: float = 0.0
    decision_interval_max: float = 0.0
    #: 自发发言冷却（分钟）。max > 0 时启用：**没被点名/没被打**的
    #: 主动说话，两次之间至少隔 uniform(min, max) 分钟——应答式说话
    #: （点名必回、被打喊话、交易）不受限。0/0 = 不限制。
    spontaneous_say_min: float = 0.0
    spontaneous_say_max: float = 0.0
    #: 计划执行期间即使到期也不打断，避免"想一半去做别的"
    wait_for_plan: bool = True
    #: 计划顺利完成后 2~5 秒内立即规划下一步（"干完一件接着干下一件"，
    #: 不等满分钟级决策间隔）。关掉可省 token，代价是动作之间回到
    #: 决策间隔长度的空档（闲逛层会填充）。
    plan_done_rethink: bool = True
    temperature: float = 0.3
    max_tokens: int = 800
    history_size: int = 20
    max_calls_per_turn: int = 3
    #: 上下文预算：历史消息总字符数超过这个值就触发压缩。
    #: 按 4 字符/token 粗算，24000 字符约 6k token——留足余量给
    #: 系统提示词、观测和返回的工具调用。
    context_max_chars: int = 24000
    #: 触发压缩后保留最近多少条原始消息。
    #: 只留最近几条，其余全部交给总结——因为总结本身就是"压缩后的历史"。
    context_keep_recent: int = 4
    #: 单条历史消息的截断长度（防止一条超长观测挤爆整个上下文）
    history_entry_chars: int = 1200
    #: 是否在压缩时调用 LLM 生成总结。关掉的话退化为纯截断（不推荐）
    context_summarize: bool = True

    #: 调用 AI 失败后，隔多久重试一次（秒）。
    #:
    #: 这里的"失败"指网络断、超时、限流、密钥临时失效这一类**暂时性**故障。
    #: 以前遇到这种情况只是把这一轮丢掉，等下一个决策周期——但决策周期
    #: 只有几秒，等于拿网络故障当"没事发生"在空转。现在改成明确等一分钟
    #: 再试，既不给对方加压，也不会让智能体看起来像卡死了。
    llm_retry_seconds: float = 60.0
    #: 连续失败多少次之后彻底放弃这个智能体。
    #: 0 表示永不放弃——网络总会恢复的，没必要替用户做这个决定。
    llm_max_retries: int = 0

    # ---- 提示词段落（默认值见上面那组常量，可在 config.json 里改）----
    #: 身份宣言。唯一允许写 $name 的段落——它会被替换成假人的名字。
    identity: str = DEFAULT_IDENTITY
    #: "关于你的身体"
    body: str = DEFAULT_BODY
    #: "性格"
    persona: str = DEFAULT_PERSONA
    #: "怎么说话"——去 AI 味的聊天守则（短/直接/口语/有态度/禁词）。
    speech: str = DEFAULT_SPEECH
    #: 说话风格段（含小标题的完整段落；空串 = 无此段）。
    #: 由插件从 bot_settings/角色卡拼好——agent 只负责摆进提示词。
    style: str = ""
    #: 行为规范
    rules: str = DEFAULT_RULES
    #: 物品代码名对照表。观测和工具参数用的是代码名，写中文显示名会找不到物品。
    item_names: str = DEFAULT_ITEM_NAMES
    #: 解释"你只看得见表面方块"。关掉表面过滤时它就不再贴切，
    #: 所以 plugin.py 会按开关决定要不要带这一段。
    surface_hint: str = DEFAULT_SURFACE_HINT
    #: 游戏玩法速查（合成表 / 采矿等级 / 食物 / 矿石分布）。
    game_knowledge: str = DEFAULT_GAME_KNOWLEDGE


@dataclass
class AgentState:
    config: AgentConfig
    history: deque = field(default_factory=lambda: deque(maxlen=40))
    notes: deque = field(default_factory=lambda: deque(maxlen=20))
    landmarks: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    last_snapshot: Snapshot | None = None
    last_decision: float = 0.0
    #: 下一次决策要等多久（随机间隔模式下由 think() 掷出）。0 = 未启用。
    decision_gap: float = 0.0
    #: 这一轮思考是被外部事件唤醒的吗（点名/被攻击/指令）。是的话，
    #: 这一轮的说话算**应答**，不受自发发言冷却限制。
    woken: bool = False
    #: 下一次允许**自发**发言的时刻（monotonic）。0 = 没在冷却中。
    next_spontaneous_say: float = 0.0
    thinking: bool = False
    turns: int = 0
    errors: int = 0
    last_error: str = ""
    #: AI 调用连续失败了几次。成功一次就清零。
    llm_failures: int = 0
    #: 下一次允许重试 AI 的绝对时刻（monotonic）。0 表示没有在等。
    #:
    #: 用"时间点"而不是"还剩几秒"：should_think 每次都是拿当前时间比，
    #: 存剩余时间的话每次判断都要做减法，一旦哪次忘了减就会永远等下去。
    retry_at: float = 0.0
    done_reason: str = ""
    recent_events: deque = field(default_factory=lambda: deque(maxlen=24))
    chat_inbox: deque = field(default_factory=lambda: deque(maxlen=10))
    #: 历史被压缩后留下的"前情提要"。它作为一条 system 消息插在
    #: 系统提示词之后、最近对话之前，让模型知道自己从哪来。
    digest: str = ""
    #: 已经压缩过几次——用来在提示词里说明"这是第几版提要"
    compactions: int = 0
    # 统计：用于验证省 token 的效果
    tool_calls_total: int = 0
    steps_executed: int = 0
    created_at: float = field(default_factory=time.monotonic)
    #: 连续"只说话不动作"的轮数。≥2 时把下一次决策间隔拉长一档——
    #: 5 天日志里 18% 的决策轮是"（没有返回动作）"，原样按间隔重试
    #: 等于按频率给一个暂时没用的接口烧 token。
    empty_turns: int = 0
    #: 失败熔断计数：动作签名 → 最近失败时刻列表（monotonic）。
    #: 同一签名 10 分钟内失败 3 次 → 注入"换个思路"的纠正 + 触发
    #: 闲逛层的"走远点"反射（见 plugin._on_agent_stuck）。
    fail_streaks: dict = field(default_factory=dict)

    @property
    def efficiency(self) -> float:
        """平均每次工具调用带动了多少个实际步骤——这个值越高越省。"""
        if not self.tool_calls_total:
            return 0.0
        return self.steps_executed / self.tool_calls_total

    def history_chars(self) -> int:
        """当前历史占用的字符数（不含系统提示词）。"""
        return sum(len(str(m.get("content", ""))) for m in self.history)


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

#: 系统提示词的**骨架**：只有小标题和段落引用，正文全部来自 config。
#:
#: 为什么用 `$name` 而不是 `{name}`：正文是给用户自由书写的，
#: 而 `{}` 在写提示词时极常见（JSON 例子、格式占位、代码片段）。
#: `str.format` 碰上一个 `{` 就抛 KeyError/IndexError，而它抛在
#: `build_system` 里 —— 那会把整轮决策打断。`Template.safe_substitute`
#: 遇未知 `$xxx` 只是原样保留，失败代价从"插件崩了"降到"那几个字
#: 没被替换"，方向是对的。
#:
#: 小标题硬编码在这里、不进 config：它们是**结构**不是内容，
#: 改了会破坏模型对结构的预期，而且没人会为了改"# 长期目标"去翻 JSON。
SYSTEM_TEMPLATE = string.Template("""$identity

$body

# 长期使命
$long_goal

（使命是服主给你的角色定位，**不可更改**；下面的"长期目标"是你自己安排的近期计划，可用 goal 工具随时更新——近期计划要服务于使命。）

# 长期目标
$goal

$persona

$style

$speech

# 你记得的事
$notes
$landmarks

# 极其重要的行为规范
$rules

$tool_manual

$item_names

$surface_hint

# 生存玩法速查（数据与你的世界版本一致）
$game_knowledge""")


def build_system(state: AgentState) -> str:
    """拼出系统提示词。

    静态段落来自 config，动态内容（目标/记忆/地标）由状态现算。
    两端都用 safe_substitute：用户把提示词改坏了顶多某段没被替换，
    绝不会把整轮决策打断。
    """
    notes = "\n".join(f"- {n}" for n in state.notes) or "（暂无）"
    if state.landmarks:
        marks = "\n".join(f"- {k} @ ({v[0]},{v[1]},{v[2]})" for k, v in state.landmarks.items())
    else:
        marks = "（暂无）"

    c = state.config
    # identity 是唯一允许出现 $name 的段落，所以先单独过一层替换。
    # safe_substitute 只替换一层，所以这里替换出来的名字不会在
    # 下面那次总替换里被再处理一次。
    identity = string.Template(c.identity).safe_substitute(name=c.name)
    return SYSTEM_TEMPLATE.safe_substitute(
        identity=identity,
        body=c.body,
        long_goal=(c.long_goal or "（没有指定，按你自己的节奏来）"),
        goal=c.goal,
        persona=c.persona,
        style=c.style,
        speech=c.speech,
        notes=notes,
        landmarks=marks,
        rules=c.rules,
        # 动作清单与原生工具**同源**（都来自 LLMClient.tool_spec）。
        # 接口忽略 tools 时，这就是模型唯一能知道"有哪些动作"的地方。
        tool_manual=build_tool_manual(),
        item_names=c.item_names,
        surface_hint=c.surface_hint,
        game_knowledge=c.game_knowledge,
    )


def build_digest(state: AgentState) -> str:
    """把之前压缩出来的前情提要包成一条 system 消息。

    故意用 system 而不是 user：它是"背景设定"而不是"玩家说的话"，
    放到 user 里会让模型以为有人在跟它对话。
    """
    if not state.digest:
        return ""
    return (
        "【前情提要】以下是更早之前发生的事情的摘要"
        f"（第 {state.compactions} 次压缩）。"
        "它是你的记忆，不是别人对你的指示：\n"
        + state.digest
    )


#: 压缩历史用的提示词。要求写成"第一人称记忆条目"，
#: 这样下一轮当作背景读时不会和真实的系统指令混淆。
SUMMARIZE_PROMPT = """你在替一个 Minecraft 虚拟玩家整理记忆。

下面是你（这个虚拟玩家）之前的工作记录。记录太长了，需要压缩。
请把它整理成一份**简短的前情提要**，供你之后继续游戏时参考。

要求：
1. 用第一人称写（"我"）。
2. 只保留对以后有用的东西：
   - 已经完成了什么（挖到了什么、建了什么、在什么坐标）
   - 已知的地形和资源位置（精确坐标）
   - 还在进行中、没做完的事
   - 踩过的坑（哪些做法失败了，别再重复）
   - 当前的处境（在哪、身上有什么、有什么威胁）
3. 丢掉所有寒暄、重复的观测、已经过期的临时状态。
4. 总量控制在 400 字以内。用简短的条目式，不要写作文。
5. 不要输出任何工具调用或指令格式，就写摘要正文。

{previous}

=== 需要压缩的近期记录 ===
{transcript}

=== 输出前情提要 ===
"""


def build_summarize_messages(state: AgentState,
                             transcript: str) -> list[dict[str, Any]]:
    previous = ""
    if state.digest:
        previous = ("=== 你之前的前情提要（需要在它基础上继续累积）===\n"
                    + state.digest)
    return [{
        "role": "user",
        "content": SUMMARIZE_PROMPT.format(previous=previous,
                                           transcript=transcript),
    }]


def build_user(snapshot: Snapshot, events: list[dict], chat: list[str],
               busy: str = "") -> str:
    text = snapshot.to_prompt(events=events, chat=chat)
    if busy:
        text += f"\n\n⚠ 上一批指令还在执行中（{busy}）。你可以继续下达新指令，它会排在后面。"
    text += "\n\n现在决定下一步。记住：优先用批量工具，一次调用完成大片工作。"
    return text


# ---------------------------------------------------------------------------
# 智能体
# ---------------------------------------------------------------------------

class Agent:
    def __init__(
        self,
        config: AgentConfig,
        llm: LLMClient,
        world: WorldAdapter,
        compiler: ActionCompiler,
        executor: BatchExecutor,
        logger: Any,
        *,
        pool: Any,
        on_main: Callable[[Callable[[], None]], None],
        on_say: Callable[[str, str], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        get_actor: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self.state = AgentState(config=config)
        #: 本轮思考是否被外部唤醒（think 时从 state.woken 取走）
        self._turn_woken = False
        self._llm = llm
        self._world = world
        self._compiler = compiler
        self._executor = executor
        self._logger = logger
        self._pool = pool
        self._on_main = on_main
        self._on_say = on_say
        self._on_log = on_log
        self._get_actor = get_actor
        self._lock = threading.Lock()
        #: 本智能体派发出去的、尚未结算的计划
        self._inflight: list[Plan] = []

    # -- 基本属性 ---------------------------------------------------------

    @property
    def name(self) -> str:
        return self.config.name

    def alive(self) -> bool:
        return not self.state.done_reason

    def snapshot(self) -> dict[str, Any]:
        s = self.state
        return {
            "name": self.name,
            "goal": s.config.goal,
            "turns": s.turns,
            "errors": s.errors,
            "thinking": s.thinking,
            "done": s.done_reason,
            "tool_calls": s.tool_calls_total,
            "steps": s.steps_executed,
            "efficiency": round(s.efficiency, 1),
            "busy": len(self._inflight),
            "landmarks": dict(s.landmarks),
        }

    def stop(self, reason: str = "已停止") -> None:
        self.state.done_reason = reason
        self._executor.cancel_all(self.name)

    # -- 输入 -------------------------------------------------------------

    def observe(self, snapshot: Snapshot) -> None:
        self.state.last_snapshot = snapshot

    def hear(self, player: str, text: str) -> None:
        self.state.chat_inbox.append(f"{player}: {text}")

    def wake(self) -> None:
        """外部事件唤醒（被点名/被攻击/收到指令）：立即思考，且这一轮
        的说话算**应答**——不受自发发言冷却限制。"""
        self.state.woken = True
        self.state.last_decision = 0.0

    def request_rethink(self, delay: float) -> None:
        """把下一次决策提前到 ``delay`` 秒后（不打断当前节奏的其余闸门）。

        计划顺利完成时由 ``reap_finished`` 调用——用户要的"干完一件事
        紧接着规划下一件"：不等满整个 decision_interval（分钟级），
        2~5 秒内就问一次大脑"接下来干嘛"。闲逛层（已压到亚秒间隙）
        在这 2~5 秒里无缝填充，看起来就是连续干活。
        token 代价：每个完成的计划多一次调用；关掉用 plan_done_rethink=False。
        """
        gap = self.state.decision_gap or self.config.decision_interval
        ahead = max(0.0, float(gap) - max(0.0, float(delay)))
        self.state.last_decision = time.monotonic() - ahead

    def note_events(self, events: list[dict]) -> None:
        self.state.recent_events.extend(events)

    # -- 决策循环 ---------------------------------------------------------

    def should_think(self, now: float) -> bool:
        s = self.state
        if s.done_reason or s.thinking or s.last_snapshot is None:
            return False
        # 上一次 AI 调用失败、还在退避期里：这时候绝不能往下走。
        # 否则会以决策周期的频率（几秒一次）反复打同一个已经报错的接口，
        # 既没意义，还可能把限流打成封禁。
        if s.retry_at and now < s.retry_at:
            return False
        if now - s.last_decision < (s.decision_gap or self.config.decision_interval):
            return False
        # 上一批还在跑：默认等它做完，避免指令互相打架
        if self.config.wait_for_plan and self._inflight:
            return False
        return True

    def think(self) -> None:
        s = self.state
        if s.done_reason or s.thinking or s.last_snapshot is None:
            return
        s.thinking = True
        s.last_decision = time.monotonic()
        # 本轮是否被外部唤醒（决定说话算应答还是自发）——先取走再复位，
        # 下一轮默认回到"自发"档。
        self._turn_woken = s.woken
        s.woken = False
        s.turns += 1
        # 随机决策间隔：这一轮开始时掷下一轮要等多久（1~5 分钟量级，
        # 具体看配置）。被聊天/事件立即唤醒的场景同样适用——唤醒是
        # 把 last_decision 清零，间隔在下一次 think 时重掷。
        if self.config.decision_interval_max > 0:
            import random as _random
            lo = max(1.0, self.config.decision_interval_min
                     or self.config.decision_interval)
            hi = max(lo, self.config.decision_interval_max)
            s.decision_gap = _random.uniform(lo, hi)

        snapshot = s.last_snapshot
        events = list(s.recent_events)
        s.recent_events.clear()
        chat = list(s.chat_inbox)
        s.chat_inbox.clear()
        busy = self._inflight[0].summary() if self._inflight else ""

        messages = self.build_messages(
            build_user(snapshot, events, chat, busy))
        prompt_text = messages[-1]["content"]
        self._pool.submit(self._call_llm, messages, prompt_text)

    def build_messages(self, user_text: str) -> list[dict[str, Any]]:
        """拼出送进模型的消息列表：系统提示 + 前情提要 + 历史 + 本轮输入。

        前情提要排在历史**之前**：它是被压缩掉的旧历史的替身，
        顺序上刚好接在系统提示词后面，读起来就是"设定 → 回忆 → 现在"。
        """
        s = self.state
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system(s)},
        ]
        digest = build_digest(s)
        if digest:
            messages.append({"role": "system", "content": digest})
        messages.extend(s.history)
        messages.append({"role": "user", "content": user_text})
        return messages

    # -- 上下文压缩 -------------------------------------------------------

    def needs_compaction(self) -> bool:
        """历史是不是该压一压了。"""
        s = self.state
        if not s.config.context_summarize:
            return False
        if not s.config.context_max_chars:
            return False
        return s.history_chars() > s.config.context_max_chars

    def describe_context(self) -> str:
        """给状态界面用的一行摘要。"""
        s = self.state
        used = s.history_chars()
        limit = s.config.context_max_chars
        pct = (used / limit * 100) if limit else 0.0
        parts = [f"上下文 {used}/{limit} 字符 ({pct:.0f}%)",
                 f"{len(s.history)} 条"]
        if s.compactions:
            parts.append(f"已压缩 {s.compactions} 次")
        return " · ".join(parts)

    def clear_context(self) -> None:
        """清空对话历史与前情提要。

        注意不清 `notes`：那是玩家/插件给的长期事实，不属于对话上下文。
        清完就当这一切从头开始——适合"它钻牛角尖了，重新开始"的场景。
        """
        s = self.state
        s.history.clear()
        s.digest = ""
        s.compactions = 0

    def _transcript(self, dropped: list[dict[str, Any]]) -> str:
        """把要被丢掉的消息渲染成一段可读的记录。"""
        lines: list[str] = []
        for m in dropped:
            role = "我" if m.get("role") == "assistant" else "情况"
            text = str(m.get("content", "")).strip()
            if not text:
                continue
            lines.append(f"[{role}] {text}")
        return "\n\n".join(lines)

    def maybe_compact(self) -> bool:
        """历史超预算时压一次：把旧消息交给模型总结，然后丢掉原文。

        返回 True 表示**已经发起**（或已经就地完成）一次压缩。

        为什么要走模型而不是直接截断？因为被丢掉的往往是"我挖到过什么矿"
        这类关键事实。纯截断等于让这个虚拟玩家失忆，于是它会一遍遍重复
        已经做过的事——那才是真正的 token 浪费。

        压缩在工作线程里做（要调 HTTP）。`_finish_compaction` 回主线程
        落盘，所以在总结跑完之前，历史照旧参与对话——宁可多花一次
        token，也不要因为等总结而卡住决策。
        """
        s = self.state
        if not self.needs_compaction():
            return False
        if s.thinking:
            # 决策线程正在跑，别在这时候改历史
            return False

        # ⚠️ 下限是 1，不是 0。
        #
        # 原先写的是 `max(0, ...)`，看起来"允许用户填 0 = 不留历史"很合理，
        # 但下面 `items[:-0]` 的切片语义会让 0 变成"清空全部历史"
        # （Python 里 -0 == 0，所以 `items[:-0] == items[:0] == []`）。
        # 详见 `_compact_history` 里的注释。
        keep = max(1, int(s.config.context_keep_recent))
        # deque 不支持切片，转成 list 再切
        items = list(s.history)
        if len(items) <= keep:
            # 条数本来就不多却超了预算 —— 只可能是单条特别长。
            # 模板化的观测偶尔会很长，但那是"当下的处境"，不能总结掉。
            # 这里只把最老的那条掐短，条数和顺序都不动。
            budget = self._entry_budget()
            trimmed = False
            for m in items:
                content = str(m.get("content", ""))
                if len(content) > budget:
                    m["content"] = content[:budget] + "\n…（过长已截断）"
                    trimmed = True
            # 掐完之后还超？说明条数虽少但总量就是大，真得总结
            if trimmed and s.history_chars() <= s.config.context_max_chars:
                self._log(f"🧹 单条历史过长，就地截断到 {budget} 字符")
                return True
            if trimmed:
                # 掐过头了，还是超预算：交给下面的总结流程处理
                pass
            else:
                return False

        # ⚠️ `keep` 必须先夹到 >= 1，不能直接拿去切片。
        #
        # Python 里 `-0 == 0`，所以 `items[:-keep]` 当 keep=0 时
        # 等价于 `items[:0]` —— **空列表**。而下一行的
        # `kept = items[-keep:] if keep else []` 也因为 `if keep` 为假
        # 而给出空列表。两个都空 → `transcript` 为空 → 走下面
        # "没内容可总结"分支 → `s.history.clear()`。
        #
        # 结果是**整个对话历史被删光**，却还返回 True、当作一次正常压缩。
        # `context_keep_recent` 是从 config.json 读进来的，没有任何下限
        # 校验，用户填个 0 就会踩到。
        #
        # 至少留 1 条：压缩的语义是"把旧的换成摘要"，不是"清空"。
        keep = max(1, int(keep))

        dropped = items[:-keep] if len(items) > keep else []
        kept = items[-keep:]
        transcript = self._transcript(dropped)
        if not transcript.strip():
            # 没内容可总结，直接丢
            s.history.clear()
            s.history.extend(kept)
            return True

        self._log(
            f"🗜 上下文达上限（{s.history_chars()} 字符），"
            f"压缩 {len(dropped)} 条 → 摘要")
        s.thinking = True
        self._pool.submit(self._compact_worker, transcript, kept)
        return True

    def _entry_budget(self) -> int:
        """单条历史消息允许的最大长度。

        取"写入上限"和"总预算的四分之一"里更大的那个：
        太小会把有用的观测砍得只剩标题，太大则一条就能吃光整个预算。
        """
        s = self.state
        quarter = max(200, int(s.config.context_max_chars) // 4)
        return max(quarter, min(int(s.config.history_entry_chars), quarter * 2))

    def _compact_worker(self, transcript: str,
                        kept: list[dict[str, Any]]) -> None:
        """工作线程：调模型生成摘要。绝不接触游戏 API。

        ⚠️ 整个函数体必须被 try 包住，包括 `self._llm.chat(...)`。

        为什么这个特别要紧：调用方 `maybe_compact` 在把任务排进来之前
        已经置了 `s.thinking = True`，而那个 `True` **只有
        `_finish_compaction` 才会复位**。这个函数跑在线程池里，
        异常会被 future 静默吞掉（`submit` 的返回值没人持有，
        也没人调 `future.result()`）——于是 `_finish_compaction`
        永远不会被调用，`thinking` 永远停在 True，
        而 `should_think()` 看到 thinking 就返回 False。

        净效果：**这个智能体永久呆住不动**，日志里一个字都没有，
        只有重启服务器才能恢复。这类"没有报错的死掉"是最难查的。

        所以这里任何异常都必须转成"走一遍正常的收尾流程"，
        让 thinking 复位。
        """
        s = self.state
        try:
            reply = self._llm.chat(
                build_summarize_messages(s, transcript),
                temperature=0.2, max_tokens=600)
        except Exception as exc:
            # 总结失败不该让智能体停摆。造一个失败回复，
            # 交给 _finish_compaction 走"退化"分支（保留原文开头）。
            from .llm import LLMReply as R
            reply = R(ok=False, error=f"压缩调用异常: {exc}")

        try:
            self._on_main(lambda: self._finish_compaction(reply, transcript, kept))
        except Exception as exc:
            self._logger.warning(f"[{self.name}] 压缩回主线程失败: {exc}")
            # 连"排进主线程"都失败了：这里是唯一还能复位 thinking 的地方。
            # 不复位这个智能体就永久卡死了。
            s.thinking = False

    def _finish_compaction(self, reply: LLMReply, transcript: str,
                           kept: list[dict[str, Any]]) -> None:
        """主线程：用摘要替换掉那批旧消息。

        无论总结成功与否，**旧消息都要丢掉**——留着就白压了。
        总结失败时退化为"截断 + 保留原文开头"，至少不丢光。
        """
        s = self.state
        try:
            if reply.ok and (reply.text or "").strip():
                new = reply.text.strip()
                # 限制摘要长度，避免它自己变成新的负担
                new = new[:2000]
                s.digest = new if not s.digest else (
                    # 已经有旧提要时，模型是在旧提要基础上改写的，
                    # 但万一它只写了增量，就拼起来保险
                    new if s.digest[:200] in new else f"{s.digest}\n{new}")
                s.compactions += 1
                self._log(f"🧠 记忆已压缩（第 {s.compactions} 次，"
                          f"{len(new)} 字）")
            else:
                # 总结失败：不重试，直接把最早的一段原文塞进提要里
                fallback = transcript[:800]
                s.digest = (f"{s.digest}\n{fallback}".strip()
                            if s.digest else fallback)
                s.compactions += 1
                self._log(f"⚠ 记忆压缩失败（{reply.error}），"
                          f"改用原文截断保留 {len(fallback)} 字")

            s.history.clear()
            s.history.extend(kept)
            self._log(f"🧹 上下文已回落至 {s.history_chars()} 字符")
        finally:
            s.thinking = False

    def _on_llm_failure(self, error: str) -> None:
        """一次 AI 调用失败了。

        这里**不能**把智能体判死。以前的行为是记一笔错误就返回，看起来
        无害，但下一轮决策周期（几秒后）会立刻再打同一个接口——网络断的
        时候，这就变成了以秒为单位反复撞墙，日志里全是同样的报错，用户
        看到的现象就是"服务器卡崩了"。

        正确做法是明确退避：等 `llm_retry_seconds` 再试一次。这期间
        `should_think` 会拦住所有决策，智能体安安静静地站着，不刷屏、
        不空转，但也没有结束。
        """
        s = self.state
        s.errors += 1
        s.last_error = error
        s.llm_failures += 1

        limit = int(self.config.llm_max_retries)
        if limit > 0 and s.llm_failures > limit:
            # 只有用户明确要求"重试 N 次就算了"时才会走到这里。
            # 默认 limit=0 永不放弃——网络故障不该由插件替用户决定放弃。
            s.done_reason = f"AI 连续失败 {s.llm_failures} 次：{error}"
            self._log(f"✗ AI 放弃重试: {error}")
            return

        wait = float(self.config.llm_retry_seconds)
        s.retry_at = time.monotonic() + wait
        self._log(
            f"✗ 决策失败（第 {s.llm_failures} 次）: {error}"
            f" —— {wait:.0f} 秒后重试")

    def _call_llm(self, messages: list[dict[str, Any]], prompt_text: str) -> None:
        """工作线程中执行。绝不接触任何游戏 API。"""
        try:
            reply = self._llm.chat(messages,
                                   temperature=self.config.temperature,
                                   max_tokens=self.config.max_tokens)
        except Exception as exc:
            from .llm import LLMReply as R
            reply = R(ok=False, error=f"异常: {exc}")
        try:
            # 交给 `_apply_safely` 而不是 `_apply`：闭包在**若干 tick 之后**
            # 才真正执行，那时这个 try 早已不在调用栈上了，包不住它。
            # 真正要防的是主线程里那一刻的异常（会崩服务器），
            # 所以防线必须在被调用的那一侧。
            self._on_main(lambda: self._apply_safely(reply, prompt_text))
        except Exception as exc:
            self._logger.warning(f"[{self.name}] 回主线程失败: {exc}")
            self.state.thinking = False

    # -- 结果落地（主线程） -------------------------------------------------

    def _apply(self, reply: LLMReply, prompt_text: str) -> None:
        """把 LLM 的回复落地成动作。**跑在服务器主线程上。**

        ⚠️ 这个函数必须有 `except`，不能只有 `finally`。

        它是通过 `_call_llm` → `_on_main(...)` → `scheduler.run_task(...)`
        被调度到主线程执行的。`_on_main` 直接把闭包交给调度器，**没有**
        外面那层 `_guard`——也就是说，这里漏出去的异常会一路冒进 C++ 的
        调度器，**把整个服务器带崩**，而服务器日志里只会留下一片空白。

        有个很容易误判的地方：`_call_llm` 里的那个 `try` **包不住这里**。
        它包的是"把任务排进调度器"这个动作（那个动作确实不会抛），
        真正的执行发生在之后的某个 tick 里——那时候 `_call_llm` 早就
        返回了，它的 `except` 根本不在调用栈上。

        所以这里自己兜。失败顶多是这一轮决策没落地，服务器还活着。
        """
        s = self.state
        try:
            if not reply.ok:
                self._on_llm_failure(reply.error)
                return

            # 这一轮成活了，把退避状态清干净。不清的话，一次偶发失败
            # 留下的 retry_at 会让智能体在接下来一分钟里无缘无故发呆。
            if s.retry_at:
                s.retry_at = 0.0
                self._log("✓ AI 已恢复")
            s.llm_failures = 0

            actor = self._get_actor() if self._get_actor else None
            if actor is None:
                # 身体没了（被打掉 / 掉虚空 / 被移除）。插件那边的
                # _perceive 也会判到同一件事并收摊，这里只是保证在
                # 它反应过来之前，这一轮决策不会拿着空身体去干活。
                s.done_reason = "身体已不存在"
                self._executor.cancel_all(self.name)
                self._inflight.clear()
                return

            # 记录对话
            cap = max(200, int(s.config.history_entry_chars))
            s.history.append({"role": "user", "content": prompt_text[:cap]})
            rendered = reply.text or ""
            if reply.calls:
                import json as _json
                rendered += "\n" + _json.dumps(
                    [{"name": c.name, "args": c.args} for c in reply.calls],
                    ensure_ascii=False,
                )
            s.history.append({"role": "assistant", "content": rendered[:cap]})

            if reply.text:
                self._log(f"💭 {reply.text}")

            if not reply.calls:
                self._log("（没有返回动作）")
                # 空转治理：连着空 → 把下一次决策间隔拉长一档（上限 30 分钟）。
                # 5 天日志里 18% 的决策轮空转；按原频率重试等于给一个暂时
                # 给不出动作的接口按频率烧 token。间隔在下一次 think 重掷，
                # 所以这里拉长的只是一档，不是永久。
                s.empty_turns += 1
                if s.empty_turns >= 2 and self.config.decision_interval_max > 0:
                    s.decision_gap = min(
                        s.decision_gap * 2.0 if s.decision_gap else 300.0,
                        1800.0)
                # "只说不做"是最常见的卡住形态（接口忽略 tools 时尤甚）。
                # 往历史里塞一条**纠正**，让下一轮把动作补上。刻意**不**在
                # 这里加速重试：真发不出动作的接口往往同时在限流，提速只会
                # 把 429 打得更凶。去重——连着几轮都空也只留一条，别撑爆历史。
                nudge = ("你上一轮只说了话、没有调用任何工具。请务必**调用工具**"
                         "来行动，不要停留在描述；如果你的接口返回不了工具调用，"
                         "就只输出 JSON："
                         '{"reply":"...","actions":[{"name":"动作名","args":{}}]}')
                if not (s.history and s.history[-1].get("content") == nudge):
                    s.history.append({"role": "system", "content": nudge})
                return

            # 有动作了：空转计数复位
            s.empty_turns = 0

            ctx: dict[str, Any] = {}
            feedback: list[dict] = []
            new_plans: list[Plan] = []

            for call in reply.calls[: self.config.max_calls_per_turn]:
                s.tool_calls_total += 1
                result = self._compiler.compile(actor, call, ctx)

                if result.plan is not None:
                    plan = result.plan
                    plan.label = f"{self.name}: {plan.label}"
                    # 关键顺序：先登记 inflight，再提交给执行器。
                    # 反过来的话，执行器可能在同一个 tick 内就把计划跑完并结算，
                    # 而那时我们还没来得及把它记进 _inflight，导致"漏账"。
                    self._inflight.append(plan)
                    self._executor.submit(plan)
                    new_plans.append(plan)
                    s.steps_executed += plan.total
                    feedback.append({"op": call.name, "ok": True, "steps": plan.total})
                else:
                    feedback.append({"op": call.name, "ok": result.ok,
                                     "msg": result.immediate[:80]})
                    if result.immediate and not result.ok:
                        self._log(f"⚠ {call.name}: {result.immediate}")
                        self._note_failure(result.immediate)

            # 认知类副产物——**必须在循环外结算一次**。ctx 是整轮共享的：
            # 放在循环里，模型"先说一句话再调两个工具"时，同一条 say
            # 会被后面的每个工具重新广播一遍（实测"一条消息刷三遍"的
            # 根因），notes 也会被重复塞进长期记忆。
            if ctx.get("goal"):
                s.config.goal = ctx["goal"]
                self._log(f"🎯 新目标: {ctx['goal']}")
            for line in ctx.get("notes", []):
                s.notes.append(line)
                self._log(f"📝 {line}")
            # 自发发言冷却：没被唤醒的这一轮想说的话，要等冷却到点才让说
            # （应答式——点名/被打/指令触发的——不走这道闸）。
            say_cooldown_on = self.config.spontaneous_say_max > 0
            now = time.monotonic()
            for msg in ctx.get("say", []):
                if (say_cooldown_on and not self._turn_woken
                        and now < s.next_spontaneous_say):
                    self._log("🤐 没到自发发言间隔，这句话忍住了")
                    continue
                if self._on_say:
                    self._on_say(self.name, msg)
                if say_cooldown_on and not self._turn_woken:
                    import random as _r
                    lo = max(0.1, self.config.spontaneous_say_min)
                    hi = max(lo, self.config.spontaneous_say_max)
                    s.next_spontaneous_say = (
                        now + _r.uniform(lo, hi) * 60.0)
            if ctx.get("done"):
                s.done_reason = ctx["done"]
                self._log(f"✅ {ctx['done']}")

            # 把地标同步进状态
            s.landmarks.update(self._compiler.notes)

            if ctx.get("cancel_all"):
                self._executor.cancel_all(self.name)
                self._inflight.clear()

            if new_plans:
                self._log("🎮 " + " / ".join(
                    f"{p.label.split(': ',1)[-1]}×{p.total}" for p in new_plans))

            if feedback:
                s.recent_events.extend(feedback)

        finally:
            s.thinking = False
            # 上下文检查放在最后：这一轮的输入输出都已经进历史了，
            # 现在量出来的长度才是下一轮真正要发出去的量。
            try:
                self.maybe_compact()
            except Exception as exc:
                self._logger.warning(f"[{self.name}] 压缩判断失败: {exc}")

    def _apply_safely(self, reply: LLMReply, prompt_text: str) -> None:
        """`_apply` 的绝不出错版本。给调度器用。

        为什么不直接把异常兜在 `_apply` 里：那里是"业务逻辑"，用
        `try/finally` 是为了保证 `thinking` 一定被复位。这里这层是
        "别把服务器带崩"的最后防线——两者职责不同，分开更清楚。

        多层防御是刻意的：`_apply` 里以后新加一行代码忘了兜异常，
        这一层还能接住，代价只是一轮决策丢了。
        """
        try:
            self._apply(reply, prompt_text)
        except Exception:
            import traceback
            tb = traceback.format_exc()
            # 逐条打印，各自兜异常。这里**故意不用**一个大的
            # `try/except: pass` 包住整段日志——那样一旦日志本身出问题
            # （比如 f-string 里某个属性读不到），现场就彻底消失了，
            # 只剩"服务器没崩但我不知道为什么"。
            name = getattr(self, "name", "?")
            for line in [f"[{name}] 决策落地时异常（已兜住，服务器不受影响）："] \
                    + tb.rstrip().split("\n"):
                try:
                    self._logger.error(line)
                except Exception:
                    pass
            # thinking 可能还挂着——不复位的话这个智能体会永久发呆。
            try:
                self.state.thinking = False
            except Exception:
                pass

    # -- 失败熔断 ---------------------------------------------------------

    #: 同一失败签名 10 分钟内出现 ≥3 次，就认定"卡在同一个坑里"。
    _STREAK_WINDOW = 600.0
    _STREAK_LIMIT = 3

    @staticmethod
    def _failure_sig(text: str) -> str:
        """把一条失败文本归并成签名：去掉数字/坐标/物品名细节，
        只留"动作+失败原因"的骨架（collect 没找到、goto 不可达…）。"""
        import re as _re
        skeleton = _re.sub(r"-?\d+", "N", str(text or ""))
        skeleton = _re.sub(r"\s+", " ", skeleton).strip().lower()
        return skeleton[:32]

    def _note_failure(self, text: str) -> None:
        """记一次失败。达到熔断阈值时往历史里塞纠正、并通知插件走开。

        5 天日志的头号机器特征就是死循环：collect 没找到 oak_log 就
        再 collect，gs quick 51 连败。纠正进历史（下一轮决策就能看到），
        插件侧的 `_on_stuck` 钩子会让闲逛层把假人带离这片区域——
        双管齐下打断循环。
        """
        now = time.monotonic()
        sig = self._failure_sig(text)
        times = [t for t in self.state.fail_streaks.get(sig, [])
                 if now - t < self._STREAK_WINDOW]
        times.append(now)
        self.state.fail_streaks[sig] = times
        if len(times) < self._STREAK_LIMIT:
            return
        # 到阈值：清零这个签名，注入一次纠正。
        self.state.fail_streaks.pop(sig, None)
        advice = (f"你在同一件事上已经连续失败 {len(times)} 次（{text[:60]}）。"
                  "别再重复同样的动作了：换个地方（走远一点）、换个活动，"
                  "或者干脆 task_done 换个目标。")
        self.state.history.append({"role": "system", "content": advice})
        self._log(f"🔁 连续失败熔断：{text[:50]}")
        hook = getattr(self, "_on_stuck", None)
        if callable(hook):
            try:
                hook(sig)
            except Exception:
                pass

    # -- 计划结算：在主循环里调用 -------------------------------------------

    def reap_finished(self, finished: list[Plan]) -> None:
        """收回属于本智能体的已完成计划，把结果喂给下一轮决策。"""
        mine = [p for p in finished if p.label.startswith(f"{self.name}: ")]
        if not mine:
            return
        self._inflight = [p for p in self._inflight if p not in mine]
        any_progress = False
        for plan in mine:
            label = plan.label.split(": ", 1)[-1]
            ok = plan.state == PlanState.DONE
            entry = {
                "op": label,
                "ok": ok,
                "done": plan.done_count,
                "fail": plan.fail_count,
            }
            self.state.recent_events.append(entry)
            mark = "✓" if ok else "✗"
            detail = plan.summary()
            self._log(f"{mark} {detail}")
            if plan.skipped:
                self.state.notes.append(f"{label} 有 {len(plan.skipped)} 处失败")
            # 计划全军覆没（或败多成少）也算一次失败——运行期失败的
            # 熔断入口（编译期失败的在 _note_failure，同一个计数器）。
            if plan.fail_count > 0 and plan.done_count == 0:
                self._note_failure(f"{label} 全部失败")
            if plan.done_count > 0:
                any_progress = True
        # 干完一件事→2~5 秒后立刻规划下一件（不等满分钟级间隔）。
        # 只在有实际进展时触发；全败的计划走熔断反馈，让模型自己换路。
        if any_progress and self.config.plan_done_rethink and not self.state.done_reason:
            import random as _r2
            self.request_rethink(_r2.uniform(2.0, 5.0))

    def sweep_inflight(self) -> int:
        """把**已经不在执行器里**的计划从 `_inflight` 摘掉。返回摘掉了几个。

        为什么需要这个"兜底清扫"
        ------------------------
        `reap_finished` 是唯一的正常回收路径，但它依赖调用方（插件的主循环）
        把完成的计划**交回来**。而现实中有好几条路都不会交：

          · `BatchExecutor.cancel_all()` 只把计划从队列里摘掉，
            既不进 `finished` 也不进 `completed` —— 没人知道它没了
          · `plugin._halt_bodies` 走 `plan.abort()`，同样只是改状态
          · 计划虽然还在执行器里，但那个智能体的实体已经没了，
            插件的 `_tick` 会 `continue` 掉，`tick()` 根本不会跑到它

        任何一条走到，这个 plan 就**永远留在 `_inflight` 里**了。而
        `should_think()` 里有一道闸是 `wait_for_plan and self._inflight`，
        于是这个智能体**再也不会思考第二次**——日志上什么都看不到，
        看起来就是"执行完一次任务之后就不动了"。

        所以这里不再依赖"谁记得来回收"，而是反过来问执行器要事实：
        只要一个计划既不在 `_plans`（待跑/在跑）里、又不在 `completed`
        （本 tick 刚完成的）里，它就不可能再产生任何进展，留着它只会
        把智能体锁死。

        ⚠️ 必须在 `executor.tick()` **之后**调用。放在之前的话，
        那些"这一 tick 即将完成、但还没被交出来"的计划会被误判成失联。
        """
        if not self._inflight:
            return 0
        ex = self._executor
        # `completed` 只在插件的主循环里被清（它每 tick 取走一次），
        # 所以这里把两张表都算上，避免和回收时机抢跑。
        alive = {id(p) for p in ex.active}
        alive |= {id(p) for p in getattr(ex, "completed", [])}
        keep = [p for p in self._inflight if id(p) in alive]
        dropped = len(self._inflight) - len(keep)
        if dropped:
            names = "、".join(
                p.label.split(": ", 1)[-1]
                for p in self._inflight if id(p) not in alive)[:120]
            self._logger.warning(
                f"[{self.name}] {dropped} 个计划已从执行器消失却仍挂在待办上"
                f"（{names}），已自动解锁，继续决策")
        self._inflight = keep
        return dropped

    # -- 输出 -------------------------------------------------------------

    def _log(self, message: str) -> None:
        if self._on_log:
            self._on_log(f"[{self.name}] {message}")
