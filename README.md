# arc_fake_player

**由 LLM 驱动的 Endstone 虚拟玩家**：身体是真正的 SimulatedPlayer，大脑是纯 Python。
角色卡轮换保持稳定在线人数、闲逛/挖矿的生活气息、被攻击反击寻仇、聊天带性格风格、
可进枪战对局互射、以合成身份无缝接入弧光核心。配套插件见文末"生态"。

- 仓库：<https://github.com/ARC-Game-Club/EndstoneMC-ARC-Fake-Player>
- 要求：Endstone ≥ 0.11（测试于 0.11.10 / BDS 1.26.45 / Python 3.13）
- 可选联动：arc_core ≥0.9.82（合成身份/钱包/领地锚点）、arc_ai_helper ≥2.4.0
  （天星常驻身体）、arc_shooter_game ≥0.4.26（枪战 quick + arena 战斗 AI）

---

## 安装部署（重要，先读第 3 条）

1. **停服**，把 `dist/endstone_arc_fake_player-<最新>-py3-none-any.whl` 放进
   `bedrock_server/plugins/`（若装过旧名 `endstone_pureagent`，把它的 wheel
   移走，两个不能同时跑）。
2. 启动一次——插件会自动把行为包释放到世界目录并登记，
   日志提示"需要重启服务器才会生效"。**先别急着重启，看第 3 条。**
3. ⚠️ **level.dat 大坑（新服必读）**：插件在运行中给 `worlds/<世界>/level.dat`
   开 Beta APIs 实验开关，但 BDS 停服时会把内存里的旧状态写回去**覆盖补丁**——
   表现是"永远提示需要重启，行为包永远加载不上"。正确顺序是：
   **启动一次 → 完全停服 → 停服状态下打补丁 → 再启动**：

   ```bash
   # 在 bedrock_server 目录下（用服务器同款 Python）
   pip install --target ./afp_tmp endstone_arc_fake_player-<版本>-py3-none-any.whl
   python - <<'EOF'
   import sys
   sys.path.insert(0, "./afp_tmp")
   from pathlib import Path
   from endstone_arc_fake_player import packing
   print(packing.enable_experiments(Path("worlds/你的世界名/level.dat")))
   EOF
   ```

   Windows 服务器把 `<<'EOF'` 那段存成 `patch.py` 跑 `python patch.py` 即可。

   补丁成功加载过一次后，之后正常重启不会再丢（会自动留 .bak 备份）。
4. 再次启动，日志出现 `行为包已就绪，虚拟玩家可以使用真实假人` 即装好。

## 快速配置

- **模型**：游戏内 OP 执行 `/ai set <base_url> <api_key> <model>`（OpenAI 兼容接口；
  服务器控制台发不了带冒号的 URL，直接改 `plugins/arc_fake_player/config.json`
  再 `/ai reload`）。
- **自动稳定在线**：`auto_online.enabled=true`、`target_count=3`；每个角色游玩时长
  随机 `play_minutes_min/max`（默认 10~300 分钟），下线后隔 1~5 分钟换下一张卡。
- **角色卡**（config.json 的 `characters`，改完 `/ai reload` 热生效）：
  `name` / `persona` 性格 / `goal` 目标 / `money` 初始金钱 /
  `chat_reply_chance` 公屏搭话概率 / `style` 说话风格示例数组 /
  `xuid`·`uuid` 可钉死合成身份。
- **聊天**：消息里出现 AI 名字 = 必回（ASCII 名词边界匹配）；公屏闲聊按每个 AI
  自己的 `chat_reply_chance` 概率搭话（bot_settings.json 覆盖 > 角色卡 > 全局 0.15）。
- **管理 UI**：游戏内 `/ai home` 打开管理表单（设家/常驻地、搭话概率滑条、
  说话风格示例、传送到 AI 身边）。AI 平时在家/自己领地/公共传送点附近闲逛，
  偶尔挖方块；离远了分段走回去（不传送）。
- **AI 助手联动**：`plugins/arc_ai_helper/chat_config.json` 里
  `virtual_player_enabled=true` → 天星获得常驻假人身体（mc_body_* 工具操控）。

## 验证清单

- `/ai body` → "真实假人 · 行为包已连接"
- `/ai auto on` → 角色假人陆续上线（在线人数+，无 [AI] 名牌）
- 聊天打假人名字 → 必回；闲聊按概率搭话且带 style 风格
- `/ai cmd <假人> gs quick` → 枪战自动开局对枪
- 假人在 arc_core 库有独立行（合成 xuid=16 位 9 开头，identities.json 终身固定）

## 已知边界

- 假人被真人攻击会自动反击+追踪；好感 ≤-30 进仇人名单会寻仇，击败一次两清；
  OP 攻击不记仇（`social.admins_immune`）
- `/ai` 系命令仅 OP；普通玩家只能和 AI 对话
- 模拟玩家执行不了命令（引擎限制）——AI 参与插件玩法走对方插件提供的
  `api_*` 接口（如枪战 `api_quick_join`）
- `PlayerJoinEvent` 不触发 SimulatedPlayer：假人的核心数据初始化由本插件
  在身体接上时主动调 `ensure_player_data_initialized`（需核心 ≥0.9.82）

---

# 版本历史

## 3.8.8：合并版——长期使命 + 枪战自动补位

两条并行开发线合并（双方各占过一个 3.8.7 号，合并版让位到 3.8.8）：

- **长期使命 long_goal**（本线）：角色卡可配 `long_goal`，进系统提示词
  "## 长期使命"段且标注**不可更改**；AI 的 goal 是可自我更新的近期计划，
  提示词要求近期计划服务于使命。手动 spawn 用 `default_long_goal`。
- **枪战自动补位 shooter_fill**（并行线，需枪战 ≥0.4.27）：真人进等待中的
  枪战大厅时自动派闲着的角色假人 quiet 入房（不抢开局权）+ 开 arena AI；
  对局结束在 LOBBY 干等超 60 秒自动离开；`/ai fillshooter [n]` 手动触发；
  配置 `shooter_fill {enabled, count}`（默认开、1 名）。

## 3.8.7：长期使命（long_goal）——服主定的锚，AI 改不动

目标分两层：**长期使命**（角色卡 `long_goal`，服主给的角色定位，如
"枪战房常驻，看到有人开局就 gs quick 加入对枪""偷袭者：避开正面冲突、
蹲点伏击落单玩家、得手即撤"）进系统提示词的"## 长期使命"段，明确标注
**不可更改**；**长期目标**（`goal`）是 AI 自己安排的近期计划，可用 goal
工具随时自我更新——提示词里写明"近期计划要服务于使命"。两台服可以给
同一个角色配不同的使命（config 本来就是按服各一份）。手动 /ai spawn 用
`default_long_goal`。未设使命时显示占位文案。

## 3.8.6：随机决策间隔（基础心跳压到分钟级）

基础心跳是 token 消耗的大头（每假人每 `decision_interval` 秒一次调用，
闲着也烧）。新增 `decision_interval_min_seconds` / `decision_interval_max_seconds`
（max > 0 启用）：**每轮决策结束时掷一次 uniform(min, max)** 作为下一轮间隔。
生产建议 60~300——两次决策之间由不走大模型的反射层撑场面：锚点闲逛
（10~26 秒挑兴趣点、偶尔挖一块）、被攻击自动反击/寻仇、枪战 arena AI；
聊天点名依然**立即唤醒秒回**（唤醒只是把计时清零，下一轮间隔重新掷），
所以拉长间隔的代价只是"自发行动"变从容。未启用时行为与旧版完全一致。

## 3.8.5：修复"一条消息发三遍"

**根因**：`_apply` 把 say/notes 的结算放在**工具调用循环内部**，而 ctx 是
整轮共享的——模型"先说一句话、再调两个工具"时，同一条 say 会被后面的
每个工具重新广播一遍（N 个工具 = 广播 N 次，顺带把 notes 重复塞进长期
记忆）。实测表现为游戏内和 QQ 群里同一句话连发三遍。修复：结算挪到
循环外，一轮只结算一次。回归用例：say 放最前 + mine_area + goal 三个
调用，断言只广播一次。

## 3.8.4：话痨治理

实测假人主动发言频率过高。三处一起压：

- **规则第 8 条"说话要克制"**（配置 v6→v7 自动迁移）：大多数决策轮次
  行动而不是说话，只在被点名/被搭话/有重要发现/被打/达成目标时开口。
- **靠近提醒降温**：真人靠近 8 格的社交提醒从 90 秒一条改为默认 240 秒
  （`social.proximity_cooldown_seconds` 可调），文案从"可以打招呼、聊天
  或搭伙玩"改为"想搭话可以打个招呼，但别话痨"。
- 搭话概率的**数值**是配置层的：官方默认全局 0.15，角色卡可覆盖——
  3.7 发布时示例卡配了 0.3~0.5 偏高，部署时应按"全局 0.05~0.15"量级
  调低（`/ai chatrate <名> <0~1>` 可热调，bot_settings.json 已播种的值
  优先于角色卡，直接改配置时两处都要改）。

## 3.8.3：假人的游玩时长记账

真人进退服由弧光核心记"第 N 次游戏 / 总游戏时长"（QQ 群消息、核心
面板都展示）；SimulatedPlayer 不触发进/退服事件，假人统计永远是 0。
现在假人上下线时用**合成 xuid** 包一个最小垫片对象调核心同一套记账
函数（`_record_player_join/quit_playtime`）——主服本地写库、从服走
中心上报，与真人完全同一条链路；停服/卸载时也会结算在线假人的
最后一次会话。开关 `playtime_sync`（默认开）。统计从部署后开始累计
（历史会话不回填）。注：调的是核心下划线内部方法（无对外 api_*），
这几条是真人计时主干、极难改名；万一不符，兜底只是回到"不计时"。

## 3.8.2：假人聊天/进退服同步 QQ 群

真人聊天走 PlayerChatEvent 被 arc_qq_sync_astrbot 转进 QQ 群；假人的
broadcast 不触发聊天事件——群里"看不到这个人说话"，前后一对质就穿帮。
现在 `_broadcast`（说话）和进/退服播报直调同步插件的 `api_send_event`，
同一条链路同一种格式（💬 名字: 消息 / 🟢 加入 / 🔴 离开）。开关
`qq_sync_chat` / `qq_sync_join_leave`（默认开）；同步插件不在、调用失败
一律静默——QQ 同步是锦上添花，不能影响游戏内聊天。已知边界：**群→服**
方向的消息是 broadcast 不是聊天事件，假人暂时"听不到"群里的话。

## 3.8.1：进退服播报 + 错峰进服 + 随机挑卡

- **进/退服播报**：轮换假人上线广播 `§e{name} 加入了游戏`、下线广播
  `§e{name} 离开了游戏`（原版基岩中文客户端同款文案；模板可在 config 的
  `join_message` / `leave_message` 改，留空关闭）。真玩家进退服客户端会
  自己显示，SimulatedPlayer 不会——不补这条，在线人数变了公屏没动静。
- **错峰进服**：每次有假人上线后，下一个要隔一个随机间隔
  （`rejoin_gap_min/max_minutes`，默认 1~5 分钟）才进——包括启动补位，
  不再三人同秒齐刷刷上线。
- **随机挑卡**：进服角色从可用池随机抽（原来是固定顺序发牌，两轮就露馅），
  并优先避开"最近刚下线"的名字（刚走的人秒回很出戏）。

## 3.8：伪装加固——心声不出公屏、怪物反击、护甲自穿、假人不互聊、领地不动土

群演假人上线后实测暴露的五个"穿帮点"，全部按"演得像真人"修：

- **心声绝不进公屏**：`_agent_log` 原来把 💭 思考 / ⚠ 工具失败 / 🎮 动作
  摘要 `broadcast_message` 给全服——等于 AI 自己播报"我在跑决策循环"。
  现在只进控制台日志和 activity.log；假人要说的话只走 `say` 工具
  （`_broadcast`，带名牌的正常聊天）。
- **怪物打了也反击**：`on_actor_damage` 原来只认真人攻击者（怪物"交给
  大脑"，而大脑十几秒才想一次，人先被打死了）。现在非玩家 Actor 一律
  走条件反射：目标用 `runtime_id` 定位（JS `doAttack` 支持按实体 id
  精确匹配），Python 侧维护一个带 TTL 的怪物句柄注册表供追击定位；
  不进好感度/仇人体系。新开关 `social.mob_reflex_enabled`（默认开）。
- **护甲自穿**（`bot:equip`）：每 15 秒让 JS 侧把背包里**最好的原版
  护甲**穿上（皮1<金2<链3<铜4<铁5<钻6<合金7，只认 `minecraft:` 命名
  空间），副手补盾牌。管理员 `/give` 完装备 15 秒内自动上身。
- **假人不互聊**：聊天入口对"自己人"（我们的假人 + 外部身体如天星）
  一律不响应——原来假人 A 的话里带上 B 的名字就会触发 B"必回"，
  两个 AI 当众表演尬聊；感知里的"附近玩家"也过滤掉自己人，大脑
  看不见另一个假人就不会主动找它搭话。真人（含离线模式）不受影响。
- **别人领地不动土**：`bridge.dig/place` 注入领地守卫，每个挖/放的
  方块先问 arc_core 的 `api_check_land_access`（假人用合成 xuid，
  和真人同一套规则：荒野/自己的领地放行，别人私地拒绝）。被拦的
  动作以失败原因反馈给大脑（"这里受领地保护…"），它会自己换地方。
  arc_core 缺席或查询异常时放行（fail-open）。

## 3.7：聊天个性——提到名字必回，公屏闲聊按概率搭话，说话带风格

**点名必回**（基岩聊天没有 @，玩家就是把名字打进句子里）：消息里
**出现 AI 的名字**即视为点名。ASCII 名字用**词边界**匹配——叫 Al 的
不会被 "alice"/"hello" 误命中；中文/非 ASCII 名字直接包含即命中。
"BossZhong 你在哪" → BossZhong 必回。

**公屏闲聊概率搭话**：没被点名的消息，每个 AI 按**自己**的概率决定
要不要接话（0% 从不插嘴，100% 每句都听）。概率来源：`bot_settings.json`
里的每 AI 覆盖 > 角色卡 `chat_reply_chance` > 全局 `chat_reply_chance`
（默认 0.15）。听到后立刻思考，接不接话、怎么接由大脑决定。
自己人（其他假人）说话不触发，防互聊死循环。

**说话风格模板**：每个 AI 可配置若干示例句子（如"我是ENMO，我操我
太喜欢这个游戏了"、"这个服务器太勾八好玩了hhh"），注入系统提示词的
`## 说话风格` 段——大模型**模仿示例的语气、口头禅和情绪**回复，
不逐字照抄。

管理入口：管理表单新增「主动回复概率」（0~100% 滑条）和
「说话风格示例」（多句用 | 分隔）两个按钮；控制台
`/ai style <名字> 示例1|示例2`、`/ai chatrate <名字> <0~1>`。
角色卡新增 `chat_reply_chance` / `style` 字段（首次上线播种默认，
改过的设置不被覆盖）。

## 3.6：OP 管理表单 + 常驻地闲逛

**OP 管理 UI**（游戏内 `/ai home` 或表单按钮）：AI 玩家列表 → 单个管理
表单——设家到脚下 / 设家到坐标（三栏输入）/ 清除家 / 传送到他身边 /
命令帮助。控制台仍可用文本命令（`/ai home <名字> set x y z` 等）。

**常驻地（家）**：`homes.json` 持久。闲逛锚点优先级：
**家 > 自己名下的领地传送点（按合成 xuid 查 arc_core）> 随机公共传送点
> 无锚点（原地小范围晃）**。

**锚点闲逛驱动**（`roam.py`，与反击/arena 同级的条件反射层，不依赖 LLM）：
空闲的 AI 每隔 10~26 秒随机在锚点 6~18 格内挑一个兴趣点走过去；25% 概率
顺手挖一块方块（`bot:dig` 自带走近，挖完正好人在兴趣点上）。离锚点超过
60 格时**每周期朝锚点走一腿（48 格）**——全程不传送，禁传送的服务器同样
成立。打架 / 枪战对局 / 大脑有计划时完全让路。配置 v5：`roam.enabled`
（默认开）、`roam.dig_chance`。

实测：设家后 AI 在家周围持续闲逛（2 分钟轨迹 -106→-103→-93→-86，
每次 8~10 格、走走停停）；无家 AI 在出生区域持续溜达。

**走一步就罚站的三个根因与修法**（3.6.5/3.6.6）：
① 手搓地表扫描窗口太窄，丘陵地形大量兴趣点被静默跳过——换用寻路器
同款 walkable_surface（+8/-14，判"能站"而不是"有方块"）；
② 闲逛的 walk 会打断 bot:dig 的走近、dig 又把人拽回去，来回抵消——
让路条件加上身体控制器忙；③ 引擎寻路失败后干等一整个行动间隔
（10~26s）——失败回调 3 秒快速重试。另：兴趣点强制离当前站位
≥4 格（否则只在原地一两格内挪，像罚站）、挖/走交替。

## 3.5：合成身份——假人无缝接入弧光核心

SimulatedPlayer 没过 Xbox 认证，引擎给的 xuid 是**空字符串**且改不了；
而弧光核心的 `player_basic_info` / 经济全以 xuid 为主键——空 xuid 意味着
**所有假人挤在同一行数据里**（共享钱包、互相覆盖统计）。

**合成身份注册表**（`identity.py`，落盘 `plugins/arc_fake_player/
identities.json`）：每个假人一个终身稳定的

- **xuid**：16 位数字、9 开头（真实 XUID 常见 2/3 开头，一眼可辨且永不
  撞车），由 `SHA-256("arc_fake_player:"+名字)` 派生——同名永远同值，
  重装/换机不变；角色卡可显式钉死（`characters` 里写 `xuid`/`uuid`，
  跨服务器同一个"人"）
- **uuid**：UUIDv5，同样稳定

**核心侧配合（arc_core ≥0.9.82）**：`_resolve_xuid` 在玩家 xuid 为空时
调本插件 `api_resolve_xuid(player)`（会校验该玩家确实是我们的模拟玩家，
同名真人/离线登录者不认领），`ensure_player_data_initialized` /
`init_player_basic_info` / `init_player_local_info` / `init_player_economy_info`
/ `update_player_name` / `get_player_money` 六处主键全部走它。又因这版
Endstone 的 `PlayerJoinEvent` **不触发** SimulatedPlayer，核心的 join
初始化永远不会为假人跑——所以本插件在每具身体接上时**主动调**
`ensure_player_data_initialized`（`_ensure_core_identity`）。

**钱包键切换**：经济键从旧 `fake_<名字>` 换成合成 xuid，首次建钱包时
旧键余额自动迁移（取较大值）；"首次"判定用注册表的一次性标记
（`wallet_init`），角色轮换不会反复发卡面初始金钱。

实测（测试服）：三个角色假人 + 助手身体在核心库各有独立
`player_basic_info` 行（合成 xuid 与真人并列），钱包为各自角色卡的
初始金额（120/260/80），助手 TianXing 也有自己的身份行。

仓库：ARC-Game-Club/EndstoneMC-ARC-Fake-Player（组织由 ARC-Minecraft
更名而来）。

## 3.4：枪战 arena 战斗 AI（进对局自动开打）

**对局内战斗逻辑**（服主定的）：找到**最近的敌方玩家** → 走过去 →
**视线内（射线无遮挡）就对着他用 `useItemInHand()` 开枪**。射击伤害由
枪械包脚本正常结算，击杀计入对局。

三层配合：

- **行为包 `bot:arena`**（`main.js` 的 `pumpArena`，0.25s 一拍）：
  持续面朝最近敌人（`lookAtLocation`），>9 格用引擎寻路追击
  （0.75s 刷新路径），`getBlockFromRay` 视线检测通过就扣扳机
  （0.2s 节流）。敌人 30 秒不在线自动退场。
- **敌我名单由 Python 下发**：队伍名牌平时隐藏，JS 没法自己辨队——
  枪战插件新增 `api_lobby_info`（返回 state/team/enemies），插件
  每 15 秒刷新名单（中途加入/换队/对局结束都跟得上，结束时自动停）。
- **启停时机**：`run_command "gs quick"` 成功进房即自动开启；
  `gs leave` / 角色下线 / 插件卸载时停止。

实测（无真人，粗糙测试图）：双假人 quick → 自动开局 → 发枪甲 →
arena AI 启动并刷新 → 假人在场内持续追击移动；一假人因测试图出生点
悬空摔死后在对局中自动重生归队。正式地图（出生点贴地）无此问题。

## 3.3：AI 玩家参与弧光小游戏（枪战 quick）

**`run_command` 工具**。AI 玩家的大脑多了一个通用动作：以自己的玩家身份
执行服务器命令（LLM 工具 `run_command`，管理员调试版 `/ai cmd <名字>
<命令>`）。实测发现两条"命令通道"对模拟玩家都不可靠——
`server.dispatch_command(player, cmd)` 返回成功但静默不执行；控制台
`execute as <假人> run <cmd>` 传给插件的 sender 不是 Player 实例——
所以**已知弧光玩法走 API 直调**（见下），其余命令才退回 dispatch。

**枪战快速开始**（需 arc_shooter_game ≥0.4.25）。`/gs quick`：有能加入
的房间立刻加入（优先等待/倒计时中的，其次进行中的对局），没有就创建
（随机地图+模式），并且**人齐自动开局**（两队都有人即倒计时，不用
等房主点开始）。全程无表单——模拟玩家点不了 UI，真人用也更快。
配套 `api_quick_join(player)` / `api_leave(player)` 供其它插件直调；
所有 `send_form` 收口到 `_send_form`（对模拟玩家跳过而不是抛异常）。
AI 侧：`run_command "gs quick"` / `"gs leave"` 会被拦截直调这两个 API。

**感知升级**：附近玩家现在单独一行（`附近玩家: 名字(距离格)`），
模型能"看见"身边的人——对战、搭话、交易都靠它。

**角色卡示例**更新：TieDan 的目标加了"闲下来就去枪战房打几局
（run_command gs quick）"。

测试服部署注意：枪战的自定义武器需要「失落枪械档案馆」BP/RP
（已装到世界并登记）；服务器本地 `weapons.json` 的 `arc:` 物品已按
包内实际命名空间改写（`blazik:` 枪 / `nova:` 甲），缺失的 6 件
（静音枪/手雷/地雷）临时映射到现有武器；`arc:` 护甲在 BP 里造了
别名物品。测试地图 `ai_test_arena` 出生点高度是拍的，正式地图请
用插件自己的建图流程。

实测（无真人）：两假人 `/gs quick` → 房间创建 → 人齐自动开局 →
发枪发甲（无 Unknown item）→ 传送到两队出生点 → 一人摔死后在对局中
自动重生归队 → 另一人在场内持续游走。击杀交换未在无真人环境复现
（bot:attack 对玩家的伤害与击杀归属在 3.2.0 已单独验证）。

## 3.2：反击 · 好感度 · 寻仇

**被打自动反击（条件反射层）**。AI 玩家被真人攻击的瞬间，插件直接接管
身体还手——够得着就打（0.8s 出手节奏）、够不着就追（引擎寻路，2s 刷新
目标点），直到目标消失 / 超时（默认 90s）/ 自己残血（≤6 脱战）。
不等 LLM（那要好几秒），但战斗全程给大脑播报事件并唤醒思考，喊话、
求饶、追击还是撤退由它决定。基于 `ActorDamageEvent.damage_source.
damaging_actor` 归因；假人互殴不触发（避免 AI 内战），怪物交给大脑，
管理员（OP）的攻击按配置豁免（`social.admins_immune`）。

**好感度**。每个假人对每个真人玩家一份 -100~100 的好感度，持久化在
`plugins/arc_fake_player/favorability.json`——轮换、重启都不忘。
被打 -12、被杀 -40、友好聊天 +1（5 分钟冷却）、完成交易交付 +2
（数值全部可配，`social` 段）。档位：仇人 ≤-30 < 警惕 < 中立 <
友好 ≥60 < 挚友 ≥80。感知里每 30 秒给大脑一份关系摘要，
`/ai list` 也能看。

**寻仇**。好感度 ≤ -30 进仇人名单：该玩家出现在 40 格内（可配）且
当前没在打架，假人主动开战追杀；亲手击败一次**恩怨两清**（好感回到
-10 警惕档，不再无端追杀）。每次脱战后有冷却（战斗时长 + 90s），
不会无限缠斗。假人被击杀会加深仇怨并在重生后继续记着这笔账。

新增依赖事件：`ActorDamageEvent`、`PlayerDeathEvent`；配置 schema
v3 → v4（新增 `social` 段与 `prompt_social` 提示词段，旧配置自动迁移）。
新增 `social.py`（FavorStore + CombatReflex，纯逻辑可单测，16 项测试）。
实测（无真人在线环境）：角色卡调度器带随机游玩时长上线 3 角色 +
助手共 4/4 人在线；伤害事件冒烟（无攻击者/假人互殴均正确过滤不崩）；
好感度显示正常。真人反击链路由单元测试覆盖（CombatReflex 状态机
全路径），进服打一下即可亲测。

## 3.1：角色卡轮换 · 外部身体 API · 交易

**对接 AI 助手（工具化）**。`arc_ai_helper` ≥2.4.0 配置
`virtual_player_enabled=true` 后，天星获得一具**常驻** SimulatedPlayer 身体
（不参与轮换，掉线自动拉起）。助手的大脑通过 `mc_body_status` /
`mc_body_walk` / `mc_body_action`（say/attack/jump/stop/teleport/drop）
工具操控身体——走过去、说话、递东西、攻击，全由它的 agent 循环决定。
本插件侧提供 `api_ensure_body / api_remove_body / api_body_status /
api_body_action`（与 arc_core 的 `api_*` 约定一致，主线程调用）。

**角色卡 + 自动稳定在线**。`config.json` 的 `characters` 里预写角色卡
（名字/性格/目标/初始金钱），`auto_online` 设定：

| 键 | 默认 | 含义 |
|---|---|---|
| `enabled` | false | 自动在线总开关 |
| `target_count` | 3 | 稳定在线的 AI 玩家数 |
| `play_minutes_min/max` | 10 / 300 | 每次游玩时长随机区间（分钟） |
| `rejoin_gap_min/max_minutes` | 1 / 5 | 下线到下一个角色上线的间隔 |
| `home` | null | 无真人时的出生点 [x,y,z] |

到点下线、隔随机时间换下一张卡上——服务器看起来一直有不同的人在玩。
角色卡假人是**隐身**的：不挂 [AI] 名牌、不做 AI 上线广播、进服不提示。
出生点选址：真人身边 > 已在线假人身边 > `home` > 世界出生点（地表扫描，
扫不出时高空创造落地、8 秒后切回配置模式防摔死）。LLM 未配置时
调度器不会放人上线（避免一群站着不动的雕像）。

**交易与钱包**。接入 arc_core 经济（`EconomyBridge`，假人用
`fake_<名字>` 合成 key，跨重启保留）：新工具 `give_item`（把背包物品
丢给 8 格内玩家，物品真实扣除、落地可捡）与 `pay_money`（真实转账）；
`bot:drop` 行为包动作支撑前者。`/ai money <名字> [数额]` 查看/设定钱包。

**权限收紧**。`/ai` 命令仍仅 OP；聊天 `@名字` 分流——管理员是**指令**
（取消事件、立即执行），普通玩家是**对话**（留在公屏，大脑按提示词里
的 `prompt_command_policy` 段只做闲聊/交易，不执行破坏性指令）。
真人靠近假人 8 格会触发一次社交提醒（90 秒冷却），角色可主动搭话。

**新命令**：`/ai persona <名字> <性格>`、`/ai money`、
`/ai session <名字> <分钟>`（0=立即下线）、`/ai auto [on|off|status]`、
`/ai reload`（热重读 config.json，含角色卡）。

**重要修复**：① Endstone 的 Player 包装句柄约一分钟就过期（实体还在线），
`_live_actor`/`_live_external` 现在判死前先按名字重解析——否则每个假人
都会被误判"身体消失"而停摆；② 程序化生成不再造 NPC 占位（假 Location
会让 spawn_actor 失败并撤回已发出的生成请求），改为"请求 → 等加入 →
接管"（`_try_attach_pending`，感知循环每 0.5 秒扫描，120 秒未加入放弃）。

安装升级：停服 → 旧 wheel 移出 `plugins/` → 放入
`dist/endstone_arc_fake_player-3.1.3-py3-none-any.whl` → 启动一次
（自动更新行为包并提示重启）→ 重启。配置从 v2 自动迁移到 v3
（新增键直接补上，已改过的旧值原样保留）。

实测（BDS 1.26.45 + Endstone 0.11.10 + arc_core 0.9.71）：助手常驻身体
生成/接管/延迟切模式/保活不重复拉起；`bot:drop` 真实掉落
（`ITEM_ON_GROUND` 验证）；`/ai money` 写读 arc_core 经济；
调度器 LLM 门控与告警按预期。1091 项 Python 回归 + 20 项 JS 交互 +
挖掘模拟（默认/严格各 7 场景）全部通过。

## 3.0.16：无法破坏方块修复与更名

原版 `dig_far()` 在每个 tick 检查距离时，会将正在挖掘的动作误当作寻路并
发送停止命令，导致破坏进度不断重置。本版只在**走近目标**时停止移动，
已经开始的挖掘会持续进行，直到方块消失、引擎明确拒绝或超时。

同时修复放置、进食的 Script API 参数及完成判定，攻击等待回执期间重复发送，
容器满仓时丢物品、合成材料计数和熔炼过早超时等问题。

发行包为 `endstone-arc-fake-player`，运行时插件名为 `arc_fake_player`，
Python 包为 `endstone_arc_fake_player`。主命令是 `/arc_fake_player`，
`/ai` 是兼容别名；权限节点为 `arc_fake_player.command`。

### 安装与升级

1. 停止服务器，将旧 `endstone_pureagent` wheel 移出 `plugins/`，避免两个插件同时运行。
   若旧版通过 pip 安装，请在**服务器使用的 Python 环境**卸载 `endstone-pureagent`。
2. 把 `dist/endstone_arc_fake_player-3.0.16-py3-none-any.whl` 放入 `plugins/`，
   或在相同 Python 环境使用 `pip install --upgrade <wheel路径>`。
3. 启动后检查安装日志，再完整重启服务器，使更新后的行为包生效。
   新配置位置为 `plugins/arc_fake_player/config.json`；首次更名会从
   `plugins/pureagent/config.json` 复制旧配置，保留原文件，已有新配置不会被覆盖。
   原活动日志保留在旧目录。旧 `pureagent_bridge` 行为包目录经过 UUID 校验后迁移，
   激活版本同步更新；若新旧目录同时存在，会报告冲突并保留文件供处理。

### 验证

```bash
python -X utf8 tests/run_all.py -q
node tests/test_interactions.mjs
node verify_dig.mjs endstone_arc_fake_player/behavior_pack/scripts/main.js
node verify_dig.mjs endstone_arc_fake_player/behavior_pack/scripts/main.js strict --strict
```

测试覆盖持续挖掘、从移动转为挖掘、延迟回执、重复目标及失败重试，
并执行真实 JS 脚本的库存和交互模拟。**这些是自动化模拟测试，尚未在真实
Bedrock/Endstone 服务端联调**；当前工作区没有服务端，也未提供具体服务端版本。
部署后可用 `/ai body` 确认桥接就绪，生成假人并指令其破坏近处的泥土、原木和石头，
检查 `activity.log` 中的成功/失败结果与实际方块变化是否一致。

API 对齐参考：[SimulatedPlayer](https://learn.microsoft.com/en-us/minecraft/creator/scriptapi/minecraft/server-gametest/simulatedplayer?view=minecraft-bedrock-experimental)、
[Container](https://learn.microsoft.com/en-us/minecraft/creator/scriptapi/minecraft/server/container?view=minecraft-bedrock-stable)、
[Endstone 插件命名](https://next.endstone.dev/docs/api/python/getting-started/setup)。

以下是插件功能说明及早期版本记录；历史测试数量仅对应各节记录的版本。

**由 LLM 驱动的 Endstone 虚拟玩家。** 大脑是纯 Python，身体是真正的
`SimulatedPlayer`。

> 目标：让大模型在 Minecraft 基岩版服务器里**像一个玩家那样活着**——
> 会掉血、会被僵尸仇恨、用手一格一格挖方块、被其他玩家看见和攻击。
> 不是挂机脚本，不是 NPC。

---

## 它到底解决了一个什么矛盾

在 Endstone 上，"真正的虚拟玩家"和"纯 Python 实现"**天然互斥**。原因是：

```
SimulatedPlayer 的创建接口 spawnSimulatedPlayer()
        ↓
只存在于 @minecraft/server-gametest 模块
        ↓
该模块跑在 BDS 的独立 JS 运行时里
        ↓
Python 插件拿不到它的句柄
```

我验证过这一点：对 `libendstone_runtime.so` 做二进制分析，
`grep spawnSimulated|SimulatedPlayer` 返回**空**。Endstone 内部知道
模拟玩家的存在（`Player::isSimulated()` 这个符号在），但**没有把创建
接口绑定到 Python**。

所以唯一的出路是分层——**身体借 JS，大脑留 Python**：

| 层 | 归属 | 为什么 |
|---|---|---|
| 身体存在（实体、血量、饥饿、仇恨列表、手臂动作、玩家列表可见性） | JS 行为包 | 只有它能造 `SimulatedPlayer`，这是**唯一**理由 |
| 感知（读方块/实体/背包） | Python 直连 API | 主线程读方块是微秒级；绕道 scriptevent 反而慢一个数量级 |
| 寻路、批量展开、tick 调度、LLM 通信 | Python | 纯计算，放哪儿都一样，留在 Python 一行不用改 |
| 移动、挖掘、放置、攻击 | JS 行为包 | 真身体才有动画和音效，也才有"隔墙挖不到"的物理约束 |

两个运行时之间唯一的通道是 `/scriptevent` 命令。

> **行为包是硬依赖。** 身体这一层没有铅笔可换——它没就绪，插件就
> 明确报错并停摆，不会退回"直接改方块"的实现偷偷顶上。理由见
> [身体不可用时的行为](#身体不可用时的行为)。

---

## 一个批量工具调用的完整旅程

你说"挖掉 20 个方块"，插件内部发生的是：

```
LLM 一次调用:  collect(block="stone", count=20)
                    │
                    ▼
        _find_blocks(radius=20)          插件自己扫出 20 个石头坐标
                    │
                    ▼
        dig_steps() → Plan(steps = 20 个 dig_far, 每个 async_op=True)
                    │
                    ▼
   ┌───────── BatchExecutor.tick() 每 tick 推进 ─────────┐
   │  取第 1 步 → 假人够得着吗？                          │
   │      够  → bot:dig → 假人挥臂敲击 → bot:dug 回调     │
   │      不够→ bot:walk → 假人真的走过去 → 再 dig        │
   │  这一步有结论之前，游标不动，第 2 步不会被开始        │
   └──────────────────────────────────────────────────────┘
                    │
                    ▼
        20 个方块依次被挖掉，完成后一次性汇报给下一轮决策
```

**LLM 的调用次数从 20 次降到 1 次**，而执行是逐个进行的——因为
`async_op=True` 的步骤在没拿到结论前不会让游标前进。

实测放大效果（见 `tests/test_agent_loop.py`）：4 次工具调用带动 18 个
原子步骤。在纯朴素模式下，这需要 18 次以上调用。

---

## "不隔墙挖方块"是怎么保证的

这是你明确提的约束。三重保证：

1. **可达性检查**——`RealBodyActions._within_reach()` 用假人的真实坐标
   算到手目标的距离，超过 `DIG_REACH`（5 格）就**不挖**，改为先
   `navigateToLocation` 走过去。
2. **物理路径**——移动交给原版寻路，它不会穿墙。假人必须真的绕到方块
   旁边。
3. **验证式完成判定**——挖掘算不算完成，看的是**方块真的变成空气了没有**，
   而不是"指令发出去了"。

副作用很正面：挖矿的时候，假人会一层层往里推进，看起来就是人在干活。

---

## 快速开始

### 1. 安装

```bash
pip install endstone_arc_fake_player-3.0.16-py3-none-any.whl
```

行为包已经打进 wheel 里了，**首次启动插件时会自动安装**。

### 2. 重启服务器（必须）

这一步不能省。BDS 只在**进程启动时**扫描 `behavior_packs/`，运行期间
改磁盘上的脚本没有任何效果。特别提醒：**退出游戏客户端不算重启**——
服务器进程没退，它就什么都不会重新加载。这也是"明明打了补丁却没生效"
最常见的原因。

插件会自动完成这些事，全部记在日志里：

- 把行为包释放到 `<服务器根>/behavior_packs/arc_fake_player_bridge/`
- 同时在 `<世界目录>/behavior_packs/arc_fake_player_bridge/` 放一份
- 把包 UUID 登记进 `<世界目录>/world_behavior_packs.json`
  （**不会覆盖**你已有的其他行为包）
- 用定点字节补丁在 `level.dat` 里打开 **Beta APIs** 实验开关
  （`@minecraft/server-gametest` 是 beta 模块，不开脚本加载不了）

> ⚠️ **`level.dat` 的修改必须在重启后生效。** 补丁前会自动留一份
> `level.dat.bak`。如果不想让插件动 `level.dat`，把配置里的
> `patch_level_dat` 设为 `false`，然后手动在服务器配置里开 Beta APIs。

### 3. 配置并生成

```
/ai set https://api.deepseek.com/v1 sk-xxxx deepseek-chat
/ai test
/ai spawn 阿豆 帮我造个小木屋，天黑前要有屋顶
```

配置存在 `<服务器根>/plugins/arc_fake_player/config.json`，首次启动自动生成。
出问题时先看同目录的 `activity.log`——它是 AI 的流水账，
记着每一步到底做成了没有。详见[插件目录与活动日志](#插件目录与活动日志)。

---

## 命令

| 命令 | 作用 |
|---|---|
| `/ai set <url> <key> <model>` | 配置 LLM（OpenAI 兼容端点） |
| `/ai test` | 测试连通性 |
| `/ai spawn <名字> [目标]` | 生成虚拟玩家 |
| `/ai list` | 状态、效率、身体动作统计 |
| `/ai chat <名字> <内容>` | 对它说话（也可聊天栏 `@名字 内容`） |
| `/ai goal <名字> <目标>` | 改写长期目标 |
| `/ai where <名字>` | 位置 |
| `/ai tp <名字>` | 传送到它身边 |
| `/ai body [real\|npc\|auto]` | 切换身体模式 / 查看行为包状态 |
| `/ai log [tail\|clear\|on\|off]` | 看 AI 活动流水 / 尾部 N 行 / 清空 / 开关 |
| `/ai sense [on\|off]` | 查看/切换"只报表面方块"开关（感知与挖掘两档） |
| `/ai prompt [段落] [新文本]` | 查看/改写系统提示词段落；`reset 段落` 还原默认 |
| `/ai install` | 重新安装行为包 |
| `/ai stop <名字>` / `/ai remove <名字>` | 停止 / 移除 |
| `/ai ctx` | 看上下文用量；`/ai ctx 12000` 改上限，`compact` 手动压一次，`clear` 清空 |

---

## 身体不可用时的行为

**行为包没就绪 → 明确报错并停摆。绝不静默降级。**

早先的版本会在连不上行为包时"退回直连"：挖掘变成直接改方块坐标，
假人站着一动不动，脚下的地形自己塌了。这比报错糟糕得多——用户以为
屏幕里那个身影是自己挖的，其实不是。所以这条路径被彻底删掉了。

现在的规则只有两条：

| 心跳状态 | 会发生什么 |
|---|---|
| 收到 `bot:pong` | `_use_real_body = True`，正常开工 |
| 收不到（或曾连过又断了） | `_use_real_body = False`，`logger.error` + 中止所有假人动作 |

具体表现在：

- `/ai spawn` 前先过 `_require_body()`。身体不可用直接拒绝生成，
  并把可照做的诊断打在聊天栏里（而不是先"生成成功"再让假人发呆）。
- `/ai health` 检测到失联 → 调 `cancel()` 中止所有进行中的挖掘/移动，
  并把停在异步步骤上的计划 `abort()` 掉（否则执行器会一直等结论）。
- 行为处理器发现身体层缺失时调用 `_no_body()` 记错误日志，返回 False。
  **不会**调用 `w.break_block()` 或 `w.set_block()` 代替。
- 攻击同理：没有真身体就直接返回 False，不会用 `/damage` 命令代打。

配置里的 `body_mode` 有三个值，其中只有一个是「换实现」：

| 值 | 含义 |
|---|---|
| `auto`（默认） | 行为包连上就开工，连不上就报错停摆 |
| `real` | 强制要求真身体，连不上直接拒绝生成 |
| `npc` | **明确**用 NPC 扮演（不是虚拟玩家，仅供调试） |

`npc` 是用户主动做的选择，不算降级——所以它不报错，但也不会假装
自己是虚拟玩家。

### 假人用什么游戏模式

SimulatedPlayer 会继承**生成时**所在世界/玩家的游戏模式。服主在创造
模式里 spawn 出来的假人于是永远是创造模式——无敌、瞬间破坏。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `bot_game_mode` | `survival` | 假人加入时强制设定的模式 |

可用值 `survival` / `creative` / `adventure` / `spectator` / `keep`。
填 `keep` 表示不干预。想让它当个无敌的建筑工就用 `creative`；
想和它打架、验证战斗能力就用默认的 `survival`。

### 区块加载：默认什么都不用做

假人**走到哪加载到哪**是 SimulatedPlayer 的固有行为，和真人一样，
不需要任何配置——这正是"像真实玩家一样"想要的。

`tickingarea`（常加载区域）是另一回事：它让一片区块**永远**在线，
无论有没有人。只有"假人长期驻守某地、而你希望那里一直运转"才需要。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `ticking_area` | **false** | 打开后假人脚下建立常加载区域 |
| `ticking_area_radius` | 30 | 半径（格），夹在 4~128。调大更吃内存 |

打开它的代价是那些区块常驻内存、实体永不休眠。详见
[为什么"走到哪加载到哪"其实是免费的](#走到哪加载到哪其实是免费的)。

### AI 请求失败怎么办

网络抖动时不要在十几秒后立刻重打同一个报错接口，也不要整个崩掉：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `llm_retry_seconds` | 60.0 | 失败后等多久再试。期间智能体静默，不刷屏 |
| `llm_max_retries` | 0 | 连续失败超过几次就放弃。**0 = 永不放弃** |

默认永不放弃：网络恢复后它会自己接着干，不需要你手动重启。
想让它失败到底就停下（比如 API key 填错了，重试没意义），把
`llm_max_retries` 设成一个正数。达到上限后智能体结束，日志里会写
最后一次的错误原文。

### 报错时会长什么样

启动后 3 秒自检，装包 → 握手 → 判定。不通的话日志里是：

```
ERROR === 虚拟玩家不可用：假人行为包未就绪 ===
ERROR 无法生成虚拟玩家：假人行为包未就绪。
ERROR   从未收到行为包心跳。常见原因：
ERROR     · 服务器还没重启（行为包需要重启才加载）
ERROR     · Beta APIs 实验开关没打开
ERROR     · world_behavior_packs.json 里没有本插件的行为包
ERROR   用 /ai body 查看详细状态。
ERROR → 现在重启服务器，行为包就会加载。
```

装包环节本身也不再"尽力而为"：`packing.install()` 找不到世界目录时
直接返回 `ok=False` 并且**一个文件都不撒**。以前会出现"文件释放到了
`behavior_packs/` 但没登记进任何世界"的半装状态，用户看到"安装完成"
却发现完全没生效——那是最难排查的一种失败。

---

## 上下文预算：达上限就总结，然后继承

LLM 的对话历史会越堆越长，每轮都要整段重发，token 消耗线性上涨。
所以给上下文设了一个字符预算：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `context_max_chars` | 24000 | 历史总字符数上限（约 6k token） |
| `context_keep_recent` | 4 | 压缩后保留最近几条原始消息 |
| `context_summarize` | true | 关掉就退化为纯截断（**不建议**） |
| `history_entry_chars` | 1200 | 单条消息写入时的截断长度 |

超过预算时发生的事：

```
历史: [m1 m2 m3 m4 m5 m6 m7 m8]   总字符 > 24000
              │
              ├── 丢掉的部分 ──┐
              │                ▼
              │        调 LLM 总结成"前情提要"（第一人称，≤400 字）
              │                │
              │                ▼
              │        state.digest 累积继承
              │
              └── 保留最近 4 条 ────┐
                                    ▼
历史: []  +  digest: "我在(12,63,8)挖到了煤…"
```

下一轮拼出来的消息长这样：

```
system : 你是 Minecraft 基岩版服务器里的一个虚拟玩家…
system : 【前情提要】…它是你的记忆，不是别人对你的指示：
         我在(12,63,8)挖到了煤，向北50格有片树林…
user   : 第 9 轮的观测 + 现在决定下一步
```

**为什么走模型而不是直接截断？** 被丢掉的往往是"我挖到过什么矿""哪里
有水源"这类关键事实。纯截断等于让虚拟玩家失忆，于是它会一遍遍重复
已经做过的事——那才是真正的 token 浪费。总结是花一次小钱，省下之后
每一次重复探索的大钱。

几个容易做错、所以专门写进测试的点：

- **压缩必须在工作线程里做**（要发 HTTP）。总结没回来之前，历史照旧
  参与对话——宁可多花一次 token，也不要因为等总结而卡住决策。
- **总结失败也要回落**。失败时退化为"原文截断"，`digest` 里塞进最早的
  那段原文。绝不能因为总结失败就把旧历史留着不动，那等于没压。
- **旧提要要累积**。第二次压缩时会把上一版提要一起喂给模型，要求它在
  基础上继续累积，而不是覆盖。
- **单条超长有兜底**。条数少但总量爆掉时（模板化观测偶尔会很长），
  走"就地截断"分支而不是总结——那类消息是"当下的处境"，不能总结掉。
- **`clear_context()` 不动 `notes`**。notes 是玩家/插件给的长期事实，
  不属于对话上下文。

用 `/ai ctx` 看实时用量：

```
=== 上下文预算 ===
上限 24000 字符 · 压缩后保留最近 4 条 · 总结 开
  Steve 上下文 3180/24000 字符 (13%) · 8 条 · 已压缩 3 次
    提要: 我在(12,63,8)挖到了煤，向北走50格有片树林。洞挖了一半…
```

---

## 插件目录与活动日志

插件的配置和日志都落在 Endstone 分配的**数据目录**里：

```
<服务器根>/plugins/arc_fake_player/
├── config.json     全部配置（首次启动自动生成）
└── activity.log    AI 干了什么（按大小轮转）
```

这个目录名不是随便起的。Endstone 靠 wheel 里的 entry point 发现插件，
Endstone 将 **entry point 键名中的连字符转成下划线**，得到插件名和数据目录：

```toml
# pyproject.toml
[project.entry-points."endstone"]
arc-fake-player = "endstone_arc_fake_player:ArcFakePlayerPlugin"
# ↑ 键名 → plugins/arc_fake_player/
```

更名会改变数据目录；本版会复制旧 `pureagent/config.json`。其他自行改名的目录，
记得手动把 `config.json` 挪到新目录。

### 感知与挖掘的真实性开关

默认情况下，假人的感知**只报告"表面方块"**——六面中至少一面贴着空气
或水的那些。埋在石头深处、四面被包住的钻石，假人是看不见的。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `surface_only` | true | 感知只报表面方块。关掉后模型能隔着石头看到矿 = 给它开了矿透 |
| `dig_surface_only` | true | 挖掘工具（`mine_vein`/`collect`）也只找表面方块 |

两个开关**必须一起看**：感知关了、挖掘还开着，模型很快会发现
"我看不见但喊一声就能挖到"，然后绕过感知直接刷工具——那第一刀就白挨了。
所以两者默认都是开。

> 这不是性能优化，是**真实性**。假人得像人一样先挖下去，才能发现下面的
> 矿；挖开之后新暴露的矿会自动进入它的感知。

### 提示词段落

系统提示词被拆成五段放在 config 里，可以整段改写：

| 配置项 | 对应段落 |
|---|---|
| `prompt_identity` | 身份宣言（唯一允许 `$name` 的段落，会被替换成假人名字） |
| `prompt_body` | "关于你的身体" |
| `prompt_persona` | "性格" |
| `prompt_rules` | 行为规范 |
| `prompt_surface_hint` | 解释"你只看得见表面方块"（关掉 `surface_only` 时不注入） |

写提示词时的两条占位符规则：

- **`$name`** 只在 `prompt_identity` 里有效，会换成假人的名字。
- **要输出字面量 `$` 请写 `$$`**（但只在 `identity` 段里会折叠；其它段
  正文原样保留）。
- **`{}` 没有特殊含义**，可以放心写 JSON 例子、格式占位符——用
  `str.format` 在这里会因一个 `{` 抛异常打断整轮决策，所以换成了
  `string.Template` 的 `safe_substitute`，未知 `$xxx` 只是原样保留，
  失败代价从"插件崩了"降到"那几个字没被替换"。

小标题（如 `# 长期目标`）和段落顺序**不进 config**——那是结构不是内容。

### 配置的自动升级

config.json 里有个 `config_version`（当前为 `2`），插件启动时会比对，
落后就自动把新键补上、把用户没改过的键升到新默认值：

| 你的 config 里的键 | 升级时发生什么 |
|---|---|
| 新版本新增、你没有 | 补上默认值 |
| 值还停在旧版默认值（你没改过） | 静默升到新默认值 |
| 你改过 | 原样保留 + 日志警告，并提示 `/ai prompt reset <段落>` |

> 别手动改 `config_version`。它是插件判断"要不要迁移"的唯一凭据。


### activity.log 记什么

它给**服主**看，不是给排障看的——所以只写结论，写成流水账：

```
[2026-09-25 20:14:02] [system] 插件已启用（数据目录 plugins/arc_fake_player）
[2026-09-25 20:14:31] [阿豆] 新目标: 造个小木屋，天黑前要有屋顶
[2026-09-25 20:14:33] [阿豆] 我该先去砍点木头
[2026-09-25 20:14:51] [阿豆] 挖区域(9格) 9/9 完成
[2026-09-25 20:15:20] [阿豆] 走过去 3/5 完成，2 处失败
[2026-09-25 20:16:04] [阿豆] 上下文达上限，压缩成前情提要（保留最近 4 条）
[2026-09-25 20:16:40] [阿豆] 身体不可用：行为包未安装
```

行格式是 `[时间] [是谁] 干了什么`。谁做的用方括号标出，`system` 是插件
自己的启停。

**刻意不记的**：每格方块的读写、LLM 的原始回复、感知快照。这些量大且
只有排障时才有用，属于 `logger` 的职责——它们会打到服务器控制台，
按日志级别过滤。

### 轮转与开关

| 配置项 | 默认 | 说明 |
|---|---|---|
| `activity_log` | true | 总开关，关掉就不写文件 |
| `log_max_bytes` | 2000000 | 单文件上限，超了轮转（下限 256） |
| `log_backups` | 5 | 保留份数，即 `activity.log.1` … `.5` |
| `log_echo_console` | false | 同时打到控制台 |

轮转是 `activity.log → .1 → .2 → …`，最老的丢掉：

```
activity.log      写满 2MB
  ├── 改名为 activity.log.1
  ├── 原 .1 改名为 .2
  └── … 原 .5 直接删除
```

不想要历史就把 `log_backups` 设成 `0`——那样超限时直接清空重来。
单条太长（比整个上限还长）时不会死循环，它会让这一条超一点写完，
总比把记录丢掉强。

常用操作：

```
/ai log              看状态（路径、大小、份数、写了多少条）
/ai log tail 30      看最后 30 行
/ai log clear        清空（含历史份数）
/ai log off          临时关掉，重启后恢复成配置值
```

### 关于"绝不把游戏搞挂"

`activity.py` 的每一条写入路径都吞异常：磁盘满、目录只读、目录被文件
占位、并发写——任何一种都不该让插件的 tick 抛出去。写失败时**只在
`logger.warning` 里报第一次、之后每 20 次报一次**，避免一个失败日志
把控制台刷爆。它不 import Endstone，可以脱离服务器单测（见下）。

---

## 关键设计决策

### 为什么挖掘步骤要做成异步的

`BatchExecutor` 的原始假设是"一步 = 一次同步调用"。但真挖掘不可能同步：

```
dig 发出去 → 假人开始挥臂 → 按方块硬度敲几秒 → 方块破
```

如果发起时就回报成功，执行器会立刻推进下一步。挖隧道时假人会**往还没
挖通的墙里走**，盖房子时会在没清空的格子里放方块。

解法是 `Step.async_op` 标记 + 保留 `last_dig` 结果：

- 步骤返回 `False` 且是异步步骤 → **不推进游标**，下个 tick 再问
- 判定完成靠 `BodyController.last_dig` 这个明确的结论对象，
  而不是"busy 变 False 了"——后者分不清"刚发起"和"已完成"

超时兜底：`DIG_TIMEOUT=8s`。假人被推开、方块受保护、区块卸载，
都不会让计划永久卡死。

### 为什么手写 A\* 而不是全交给 JS

`SimulatedPlayer` 有原生 `navigateToLocation`，比我的 A\* 强。
但 A\* 在这里有别的用处：

- **模拟假人的"视野"**——判断目标可不可达、该从哪个方向靠近
- **批量展开的排序**——按沿路径距离决定先挖哪个
- **给 `walk_to` 挑落点**——找一个能站人、又不隔墙的相邻格

所以真身体模式下：**A\* 做规划决策，原版寻路做实际移动**。

### 为什么感知不走 JS

读方块走 Python 直连 `dimension.get_block_at()` 是微秒级操作。
如果改成 `scriptevent` 往返，每次感知都要等一个完整的命令周期，
而且扫描半径 4 的世界一次要读几百个方块——这在 JS 侧会直接
把 tick 预算吃光。

### 为什么感知要剔除看不见的方块

第一版 `scan_blocks` 会把扫描半径内**所有**有价值的方块都报给模型，
包括埋在石头深处的钻石。于是模型不用探索，直接标个坐标喊 `mine` 就
把矿挖了——等于给它开了**矿透（X-ray）**，生存游戏最核心的"找矿"
环节整个消失。

所以感知只报**表面方块**：六面中至少一面贴着空气或水的那些。判定
刻意放在 `classify()` 之后——先用零成本筛掉空气和无价值方块，再付
6 次邻居查询的钱。单次扫描用一个 `memo` 缓存，把读取上界锁在
"扫描区外盒 + 一层壳"（约 968 次），实测只比关掉过滤多读 7%。

两个刻意为之的边界：

- **水算"透明"**——玩家站在水里看得见水底的矿，假人也该看得见。
  只判空气会让海底/河床的矿石从感知里整个消失。
- **岩浆、玻璃、树叶不算**——隔着玻璃看见的矿在游戏里也挖不到；
  岩浆和树叶是实体方块，会挡住视线。

> 配套的 `prompt_surface_hint` 段落负责把这件事说清楚，否则模型会把
> "看不见"误读成"不存在"，然后在地表反复换位置找钻石——那一刀砍下去
> 反而让它更笨。

### 为什么"实体还活着吗"要单独拦一层

`self._actors[name]` 存的是**一个 Python 引用**，不是实体的所有权。
创造模式玩家一刀把假人打掉、或者它掉进虚空、被 `/kill`，`_actors` 里
那个对象照样躺着，但底层 C++ 实体已经销毁了。此后任何一次
`actor.location` / `actor.dimension` 都会抛 `RuntimeError: actor is not valid`。

危险的地方不在异常本身，而在**它是从哪儿冒出来的**：所有步骤处理器、
命令处理、感知采集都会去解引用那个 actor，于是崩溃点飘忽不定，看起来
像"打掉假人导致服务器崩了"这种莫名其妙的现象。更糟的是 `tick` 和
`_perceive` 是调度器回调，异常一旦逃出去就是**崩服**，不是报个错。

所以所有取值统一走一层：

| 层级 | 做法 |
|---|---|
| `_is_live(actor)` | 唯一的存活判定，读 `is_valid`；读不了就当失效（保守） |
| `_live_actor(name)` | 唯一的取身体入口，死的返回 `None` |
| `_current_actor()` | 步骤处理器拿身体的唯一途径，天然带过滤 |
| `_tick` | 死实体直接跳过，不推进它的计划 |
| `_perceive` | 身体没了就**主动收摊**：停决策、取消计划、摘引用 |
| `_bury_body` | 收摊本体，幂等，且每一步都兜异常 |

`is_valid` 这个属性值得说一句：它是 Endstone 在 C++ 侧实现的，读的是
底层句柄，**实体销毁后返回 False 而不会抛**。这个"不抛"正是整个保护
能成立的前提——如果它也抛，我们就只能靠 `try/except` 去猜了。

### 假人是怎么造出来的（以及为什么以前造不出来）

`@minecraft/server-gametest` 这个模块提供两种创建 `SimulatedPlayer` 的方式：

```js
// 方式 A：模块级函数。不依附任何 Test，随时可调。
gametest.spawnSimulatedPlayer({ x, y, z, dimension }, name, gameMode);

// 方式 B：GameTest 实例上的方法。需要测试正在运行。
register("arc_fake_player", "sim_spawner", (test) => {
    activeTest = test;                       // ← 只有测试被执行时才赋值
    test.spawnSimulatedPlayer(loc, name);
});
```

第一版只用了方式 B。它有一个隐蔽的前提：`register()` 只是**注册一个测试
定义**，回调里的 `test` 参数只有在测试被真正执行时才存在。而测试要靠
`/gametest run arc_fake_player:sim_spawner` 启动——插件侧从来没发过这条命令，
于是 `activeTest` 恒为 `null`，所有生成请求都堆进 `pendingSpawns` 里永不返回。

症状就是用户报的"放出来的还是 NPC"：真身永远不会加入，占位实体就一直挂着。

现在 `ensureSim()` 三条路按通用性从高到低试：

1. **模块级 `gametest.spawnSimulatedPlayer()`** —— 方式 A，add-on 场景的正规入口
2. 已启动的 GameTest 实例 —— 方式 B，作为老版本引擎的退路
3. 都不通 —— **明确回报失败**，不再静默排队

行为包通过心跳把能力上报给 Python（`caps.direct` / `caps.test`），插件据此
在生成前就判断"真身到底会不会来"。不会来的话直接报错并说明原因，**绝不
再造一个 NPC 冒充**——那种"看起来成功了"的假象比直接报错难查一百倍。

> ⚠️ `@minecraft/server-gametest` 至今仍是 **pre-release（beta）模块**，
> manifest 里只能写 `"version": "beta"`，且必须打开世界设置里的
> **Beta APIs** 实验开关。这是因为 SimulatedPlayer 是"模拟玩家"，
> 属于测试基础设施，Mojang 尚未把它提升为正式 API。

### 为什么占位实体要被换掉

`body_mode: "auto"` 下，插件发命令让行为包造真身，而真身加入服务器
需要一两秒。这段时间里 `get_actor()` 不能返回 `None`——那会让 Agent
立刻判定"身体不存在"然后自己停掉。所以先给一个 NPC 占位实体。

问题在于：**占位实体是活的**。它不会因为真身加入而自动消失，`is_valid`
一直是 `True`。第一版代码只写了"创建占位"，没写"替换占位"，于是屏幕
上那个 NPC 会永远挂着——用户看到的就是"假人还是 NPC 的皮肤"。它既
是错误的外观，也是个完全动不了的空壳。

修法是在 `_perceive` 里每轮检查一次：如果当前挂的是占位实体（用
`_placeholder_flags` 记账，不依赖 Endstone 的内部类名），且 `get_player(name)`
已经能拿到真身，就把占位 `remove()` 掉、换上真身，同步 `ctl.actor`
和 `body_dimension`。

判定顺序很关键：**先判占位、再判死活**。反过来的话，占位实体活得好
好的，`_live_actor` 会给它过，永远轮不到替换那一步。

### 为什么"日志刷屏"要两道闸

BDS 为每条 `scriptevent` 命令打一行日志：

```
Script event bot:dig has been sent
```

这条不是插件打的——插件源码里根本没有这个字符串，它是 BDS 原生命令日志。
问题是挖掘本质上是异步的（`breakBlock` 之后要按方块硬度敲几秒），而
`BatchExecutor` 每 tick 都会回来问同一个异步步骤"完了没有"。任何一条
没被挡住的重发路径，都会以 tick 频率往控制台灌同一行。

两道**互相独立**的闸：

| 闸 | 位置 | 作用范围 | 窗口 |
|----|------|----------|------|
| 坐标去重 | `BodyController._dig_send_allowed` | 同一坐标 | 0.5 秒 |
| 失败退避 | `RealBodyActions._backing_off` | 同一步骤键 | 1.5 秒 |

它们解决的不是同一个问题：去重挡的是"结论刚回来就重发"，退避挡的是
"连续多次尝试都失败"。少任何一道都会漏。实测 600 tick 的追问：

```
开闸：  1 条 bot:dig
关闸： 40 条 bot:dig
```

⚠️ 但**只有这两道闸是不够的**——它们只负责"少发一点"，挡不住"永远发"。
真正让刷屏停下来的是第三道闸：**累计失败次数烧光后宣告放弃**。
这个位数原先写坏了（拿会被 `_cleanup` 清空的 `_attempts` 当累加器，
`n` 恒等于 1，上限永不成立），所以退避到期就又发一条、永不停歇。
详见「3.0.2」一节。

退避状态（`_failed_at`）**必须活过 `_cleanup`**。`_cleanup` 清掉
`_attempts` / `_phase` 是对的（那是"这一轮"的状态），但顺手把
`_failed_at` 也清掉的话，退避就形同虚设——下一次追问走"首次遇到"
分支，立刻重发。这一点有单独的回归用例。

### 为什么 AI 失败要等一分钟

以前的处理是记一笔错误就返回。看起来无害，但下一轮决策周期（默认
12 秒）会立刻再打同一个已经报错的接口。网络断的时候，这就变成了以
秒为单位反复撞墙。

现在的行为：

```python
s.retry_at = time.monotonic() + 60      # 明确退避 60 秒
```

`should_think()` 会在这段时间里拦住所有决策——智能体安安静静站着，
不刷屏、不空转，但**也没有结束**（`done_reason` 保持为空）。一次成功
之后 `retry_at` 立刻清零，不会因为一次偶发失败留下"后遗症"。

默认**永不放弃**（`llm_max_retries: 0`）。网络总会恢复，替用户决定
"重试 3 次就算了"没有道理。想限制的话在 config 里设个数即可。

---

## 项目结构

```
endstone_arc_fake_player/
├── world.py          感知压缩 + 所有游戏 API 接触点
├── pathfinder.py     A* 寻路（地形代价感知）
├── executor.py       批量计划的分片执行引擎（支持异步步骤）
├── actions.py        批量意图 → 执行计划
├── llm.py            OpenAI 兼容客户端 + 23 个批量工具
├── agent.py          感知-决策-执行循环
├── bridge.py         scriptevent 双向通道（令牌鉴权 + 请求配对）
├── body.py           身体动作队列（进行中 / 已完成 的区分）
├── realbody.py       执行器侧的真实动作处理器
├── packing.py        行为包自动安装
├── nbt.py            Bedrock NBT 定点补丁（只改需要的字节）
├── activity.py       AI 活动日志（人话流水账，按大小轮转）
├── plugin.py         插件入口
└── behavior_pack/    JS 侧（随 wheel 一起发布）
    ├── manifest.json
    ├── scripts/main.js
    └── structures/arc_fake_player/empty.mcstructure
```

**分层的红线**：`world.py` 和 `plugin.py` 是仅有的两个接触 Endstone API
的地方。其余模块全部可以在没有服务器的情况下单元测试——这不是巧合，
是为了让批量展开、寻路、状态机这些容易出错的逻辑能被完整验证。

---

## 测试

一条命令跑完全部（推荐）：

```bash
python tests/run_all.py            # 全部，带汇总
python tests/run_all.py -q         # 只打每个文件的结论
python tests/run_all.py hybrid     # 只跑文件名里带 hybrid 的
```

它会打印一张汇总表，最后给出**总项数**——改完测试记得拿这个数字去
更新本节的数字，别手算。

也可以单独跑某一个文件：

```bash
python tests/test_pure_agent.py    # 38 项：感知压缩、A*、批量展开、分片
python tests/test_agent_loop.py    # 13 项：决策循环、token 效率
python tests/test_hybrid.py        # 172 项：桥接、异步状态机、行为包安装、不降级契约
python tests/test_context.py       # 72 项：上下文预算、总结继承、失败回落
python tests/test_activity.py      # 108 项：活动日志轮转、配置契约、entry point
python tests/test_surface.py       # 186 项：表面剔除、提示词拆段、配置迁移、身体生命周期
python tests/test_v2_fixes.py      # 370 项：日志刷屏、假人身份、崩溃防护、常加载区域、游戏模式、审计修复、重试语义
```

共 **959 项**，全部通过。

> 每个文件都能**单独跑**。以前不行——只有走 `run_all.py` 才跑得起来，
> 单独 `python tests/test_xxx.py` 会死在 `ModuleNotFoundError: endstone`。
> 根因是包根 `__init__.py` 会无条件 `from .plugin import`，而
> `plugin.py` 顶层就写着 `from endstone import ColorFormat`，于是
> **import 本包的任何子模块**都会连带触发它。修法是每个文件在
> `sys.path.insert` 之后、**任何 `endstone_arc_fake_player` 导入之前**
> 显式调一次 `install()`（幂等，走 `run_all.py` 时不会重复装）。

### 没装 Endstone 的机器上也能跑

`tests/endstone_stub.py` 提供了一个 `endstone` 的最小桩，`run_all.py`
会先把它装上再跑测试。

为什么需要这个：这套测试**本来是设计成能在真服务器上直接跑的**——
测试文件自己不 `import endstone`，游戏 API 只出现在 `plugin.py` 和
`world.py` 的薄适配层里。但 `endstone_arc_fake_player/__init__.py` 会
`from .plugin import`，而 `plugin.py` 顶层写着 `from endstone import
ColorFormat as C`，于是只要 import 包本身就会把 Endstone 拖进来。
在一台没装 Endstone 的开发机上，七个文件会全部死在 `ModuleNotFoundError`。

桩里有两个容易踩空、注释里特意标了"坑"的地方：

- **`endstone` 必须设 `__path__ = []`**，否则 `import endstone.actor`
  会去磁盘上找，直接失败。
- **`event_handler` 必须真的挂 `_is_event_handler` 标记**。
  `test_activity` 正是靠 `vars(ArcFakePlayerPlugin)` 里这个标记来统计事件
  处理器的——桩要是图省事写成 `return fn`，那个测试会查出"0 个事件
  处理器"然后失败。真实的实现（`endstone/event/__init__.py`）就是给
  函数挂三个属性，桩照抄了。

桩只在 `sys.modules` 里**没有**真 endstone 时才生效，所以在真服务器上
跑测试走的是真包，不会静默换成一个假货糊弄过去。

`test_hybrid.py` 里最值得看的是**第七部分**：它把 `main.js` 当文本读进来，
逐条核对 Python 与 JS 的事件名是否对称。协议漂移是这种双运行时架构
最容易出的问题——加了个事件却忘了另一边，运行时才炸。这个测试把它
提前到了构建期。

**第八部分**则守住了"不静默降级"这条线：它扫描 `plugin.py` 源码，
确认那些兜底实现真的已经不在了（`h_dig_far` 里没有 `w.break_block`、
`_health` 里没有自动挑实现的 `want_real`、`_cmd_spawn` 的闸门在生成
动作之前）。这类契约靠人自觉是守不住的——删了一半的兜底比没删更危险。

`test_context.py` 的重点在**失败路径**：总结接口挂掉时上下文仍要回落、
旧提要不能被新摘要覆盖、最近几条消息不该被重复送进总结请求（那是白花钱）。

`test_activity.py` 也把一半用例压在失败路径上——日志模块的价值恰恰在于
**它崩了不能让游戏崩**。目录被文件占位、目录只读、并发 4 线程写 200 条、
单条比整个上限还长：逐个验证不抛异常、不丢行、不出现半截行。它的最后
两部分是**契约扫描**：配置键的名字和默认值、`entry_points.txt` 里注册
的插件名必须和目录名一致。后者尤其重要——entry point 是 Endstone 发现
插件的唯一凭据，错了插件根本加载不起来，而症状只是"什么都没发生"。

`test_surface.py` 守的是"假人不能透视"这条线。它按六部分组织：`is_exposed`
的判定（透明方块族逐项列，还专门放了 `oak_stairs`/`repair_block` 两个
**含 `air` 子串**的回归用例，防止子串匹配把矿透漏回去）、`scan_blocks`
的剔除（暴露矿 vs 埋死矿的干净对照）、读取上界（memo 是否真把邻居查询
锁住）、挖掘侧的 `dig_surface_only`、提示词拆段（用户把 `rules` 改成
`{`、`$5`、JSON 例子都不能把 `build_system` 打炸）、以及 `confmig` 的
配置迁移（缺失即补 / 未改过升级 / 改过保留+警告 / 幂等 / 版本前进不动）。

> 测试**不需要**装了 Endstone 的环境：`tests/endstone_stub.py` 提供了
> 一个最小桩，`run_all.py` 会先装好它。在真服务器上跑时会自动用真包。

`test_v2_fixes.py` 是 2.0.0 针对实测现象补的回归网，每个现象背后是
一个独立的根因：
| 现象 | 根因 | 防线 |
|------|------|------|
| 放出来还是 NPC | `main.js` 只有 `activeTest.spawnSimulatedPlayer` 一条路，而 `activeTest` 只在 GameTest 被真正执行时才赋值。没人敲 `/gametest run`，它就恒为 null，真身永远不加入 | 改走**模块级** `gametest.spawnSimulatedPlayer()`（不依附任何 Test） |
| 修了却没生效 | `extract_pack` 以 `manifest.json` 的 `header.version` 相等为跳过判据。代码改了、版本号没改，补丁就**静默**送不到服务器 | 改用内容指纹（sha256）+ 发布记账文件，区分"用户手改的"和"插件的旧版本" |
| 退出游戏 AI 就死 | `server.get_player(name)` 按名字查在线玩家，**不区分真人和假人**。假人名字撞上服主账号时，AI 就把真人当成了自己的身体 | `_looks_simulated` 语义校验 + `_find_sim_player`，同名真人只警告不采用（**判据换过两轮，见下**） |
| 站着发呆 / 地形读空 | BDS 只加载玩家附近的区块。假人不触发客户端区块请求，视线外区块一卸载，`get_block_at` 全部返回 None | `is_chunk_loaded` 让感知能区分"区块没加载"和"真的没方块"。`tickingarea` 常加载区域作为可选项保留，**默认关** |
| 日志刷屏 | 挖掘是异步的，"还没结论"时执行器每 tick 重问同一步骤；任何没挡住的重发路径都会刷 `Script event bot:dig has been sent` | 两道独立闸：坐标去重（0.5 秒）+ 已知失败退避（1.5 秒）。实测 600 tick 从 40 条降到 1 条 |
| 把玩家当身体 | 见上 | `_actors` 写入点收敛 + `get_actor` 有效性过滤 + 升级时同步换 `ctl.actor` |
| AI 失败即撞墙 | 失败只记一笔，下个决策周期（十几秒）立刻再打同一个报错接口 | 明确退避 60 秒，且**不结束**智能体 |

### 为什么"修了却没生效"要单独说

这条和其他几条性质不同：它不是代码 bug，而是**补丁分发**的 bug，症状
却是"你的修复没用"，非常容易让人回头去改已经改对的地方。

BDS 只在**进程启动时**扫描 `behavior_packs/`。所以在服务器运行期间：

- 改磁盘上的 `scripts/main.js` → 内存里跑的还是旧脚本
- 退出游戏客户端 → 服务器进程没退，**什么都重新加载不了**
- 只有重启服务器 → 才会重新读一遍脚本

另一头，旧版 `extract_pack` 用 `header.version` 做判据也帮了倒忙：两轮
修复改了 `main.js` 却没改版本号，于是"版本一致 → 跳过释放"，补丁连
磁盘都没到。

现在的判据是内容指纹，并额外写一个 `.arc_fake_player_released.json` 记账文件
记录"上次我们释放出去的 hash 是多少"。三方比对：

| 源 vs 目标 | 目标 vs 记账 | 结论 |
|------------|--------------|------|
| 内容相同 | — | 跳过，不碰 |
| 不同 | 目标 == 记账值 | 是我们的旧版本 → **覆盖** |
| 不同 | 目标 != 记账值 | 用户手改过 → **保住** |

被保住的文件也会把**当前实际 hash** 记回账本，否则下一次它会被当成
"我们的旧版本"给盖掉。

> **维护须知**：改了 `behavior_pack/` 里任何文件，都需要重启服务器才生效。
> 插件会在 `/ai body`、启动自检、`/ai install` 三处提醒。

### "走到哪加载到哪"其实是免费的

先把两个概念分开，它们经常被混为一谈：

| | 行为 | 需要代码吗 |
|---|---|---|
| **跟随加载** | 人走到哪，周围区块就加载到哪 | **不需要**。SimulatedPlayer 的固有行为 |
| **常加载区域** | 一片区块**永远**在线，无论有没有人 | 需要。`tickingarea` 命令 |

`SimulatedPlayer` 是名副其实的模拟玩家，它的区块加载和真人完全一致
——这是引擎的实现，不是我们做的。所以"让假人像真人一样走到哪加载到哪"
这个需求，**正确做法是什么都不做**。

`tickingarea` 是另一回事：它把一片区域永久钉在内存里，区块常驻、实体
不休眠。只有"假人长期驻守某地，而你希望那里持续运转（刷怪、农作物、
红石）"时才值得用。所以它默认**关闭**：

```
"ticking_area": false
```

真要用的时候，命令长这样：

```
tickingarea add <x-r> <y-h> <z-r> <x+r> <y+h> <z+r> pa_<名字>
```

> ⚠️ **y 方向不能用对称半径。** 这是实测踩出来的坑：
> 假人在 y≈15，`y - 30 = -15` 是负数，引擎把越界值夹到 0，
> 于是区域变成 `from -16, 0, -48 to 47, 0, 31`——**y 从 0 到 0，
> 一个平面**，完全没覆盖假人所在的层。命令还返回成功、日志还打了
> "已建立"，纯静默失效。
>
> 现在 y 用固定半高 16 并显式夹到世界下限 0，且兜住"假人在 y=0
> 以下时区间反转"的情况。

另外三个细节：

- **半径默认 30**（`ticking_area_radius`，夹在 4~128）。
- **只在建立成功后记名字**。命令失败就下次再试。
- **假人消失时摘掉**（`_release_ticking_area`）。反复 spawn/remove 而不
  清理的话会越堆越多。

配套改了感知侧：`Snapshot.terrain_loaded` 会告诉模型"这块区域没加载"，
而不是让它看到一片空 `None` 就判断"脚下是空的"。这两件事的应对方式
**正好相反**——前者该原地等或往回走，后者该往下挖。

### 为什么"假人身份"要校验，而不是按名字查

`server.get_player(name)` 只按名字匹配**在线玩家**，不区分真人和假人。
假人名字（默认取用户名）一旦撞上服主的真人账号，拿到的是真人：

```
假人请求 spawn(name="Steve")
  → server.get_player("Steve") 返回的其实是那个真人玩家
  → 智能体把真人当成自己的身体
  → 服主一下线，Player 对象失效 → "没有身体" → 智能体退出
```

#### 判据是怎么选出来的（两版都错了，第三版才对）

**第一版靠"能力探测"——思路是错的。**

想法是：`SimulatedPlayer` 应该比 `Player` 多一批方法
（`disconnect` / `navigate_to_location` / `stop_moving` / `set_skin` /
`start_build`），数出来就知道了。

实际核实后发现：那些方法是 `@minecraft/server-gametest` 的 **JS 侧**
API，Endstone 只把它作为行为包脚本运行时，**没有桥接到 Python 的
`Player` 对象上**。证据两条：

1. 在 `libendstone_runtime.so` 里能搜到 C++ 的 `Player::isSimulated()`，
   但在 `_python.cpython-311-*.so`（pybind 绑定层）里搜不到
   `is_simulated` / `isSimulated`，也搜不到那五个方法名。
2. Endstone 官方 Player 文档的**完整属性表**里没有 `is_simulated`，
   方法表里也没有那五个假人专有方法。

后果是 `_looks_simulated` **恒返回 False**——真身每次都被判成"同名
真人"而遭忽略。

**第二版改用"三项设备特征全空"，还是错的。**

| 属性 | 想法 | 实际 |
|---|---|---|
| `device_id` | 假人没有客户端 → 空 | ✔ 逻辑成立 |
| `device_os` | 同上 | ✔ |
| `game_version` | 同上 | ✔ |

逻辑本身没错，但**实现太苛刻**：要求三个属性**都读得到**且**都为空**
才认。用的是 `hasattr` + `for...else`，三项里只要有一项 `hasattr` 为假
就 `break` 出去，掉到 xuid 兜底；而兜底里 `hasattr(player, "xuid")`
同样可能为假——**两头都落空，最后还是 False**。

2.1.0 的用户日志把这点钉死了：

```
[08:59:43] Player Spawned: hly xuid: ,
[08:59:43] WARNING: 发现与假人同名的在线玩家「hly」，但它看起来不是模拟玩家，已忽略。
[08:59:47] WARNING: [hly] 身体已消失，已停止该智能体
```

日志证明了：**`xuid` 读得到**（明明白白打了 `xuid: ,`，有冒号没值），
而它依然被判成"不是模拟玩家"。真身接不上 → 占位 NPC 永远不换 →
4 秒后占位失效 → 智能体判定"身体已消失"停摆。**用户看到的"还是 NPC
皮肤"也是同一个根因**——不是皮肤设错了，是假人压根没接上。

**第三版（现在）主判据就是 `xuid`，判据顺序：**

| 顺序 | 判据 | 作用 |
|---|---|---|
| 0 | `is_simulated` / `isSimulated` 显式属性 | 有就用，最权威 |
| 1 | 类名含 `simulated` | 类名兜底 |
| 2 | **`hasattr(p,"xuid")` 且值为空 → 假人** | **主判据** |
| 2' | xuid 有值 → **真人（强否决，直接收工）** | |
| 3 | 三项设备特征（仅当 xuid 属性整个读不到） | 旧版兜底 |

`xuid` 是 Xbox Live 账号 ID，只有**真正通过 Xbox Live 认证的客户端**
才有；SimulatedPlayer 没有客户端，必然是空的。而且实测证明这个属性
读得到，比三项设备特征可靠得多。

> ⚠️ **实现细节**：必须用 `hasattr` 区分"属性不存在"和"属性的值是空"。
> 只写 `getattr(p, "xuid", None)` 的话，属性不存在也会得到 `None`，
> 和"值就是空的"混在一起，方向就糊了。

#### 离线真人的代价（用户已知晓并接受）

**离线登录的真人 xuid 也是空的。** 历史日志里出现过
`Player Spawned: xxx xuid: , pfid: 99d7a812...`——那是个离线模式的真玩家。

所以在这台服务器上，**离线真人会被认成假人**。如果 AI 接上了他的身体，
他一退出游戏智能体就会停。

这是**明确选择的取舍**：前两版宁愿不认也不敢认错，结果是**一个都认不出来**，
插件整体不可用。相比之下"认错离线真人"是可控的、可发现的、可缓解的。

缓解措施：

- **启动自检会检测**：扫一遍在线玩家，凡 xuid 为空且不在行为包假人名单
  里的，就警告服主"这些玩家会被当成假人，建议开启正版验证"。
  没有离线真人时不会打这条，避免噪音。
- **测试把这个代价锁住了**：`test_v2_fixes.py` 里有一条断言写的是
  *"【已知代价】离线真人 xuid 空 → 会被认成假人"*。它**故意**断言
  "判成假人"——这样将来有人想"顺手修好"时，会立刻看到这条断言，
  从而知道背后动的是什么。
- 想彻底避开：`server.properties` 里 `online-mode=true`。

#### 同名警告要去重

它在每 10 tick 的感知循环里，判据只要有一点偏差就会以 0.5 秒一行的
速度刷爆日志（2.0.0 实测 20 多行全是同一句话，真正的错误被淹了）。
现在按名字记账，只警告一次，之后降为 debug。

### 控制权边界：Python 发指令，JS 操作假人

**3.0.0 起，Python 不再直接写假人对象的属性。** 这条规则是用户定的，
理由很实在：假人是行为包用 `spawnSimulatedPlayer` 造的，它归 JS 管；
Python 越界去改，两边状态容易打架，出了问题也不知道该查哪边。

| 动作 | 2.x（Python 直接做） | 3.0.0（改成发指令） |
|---|---|---|
| 设定游戏模式 | `player.game_mode = ...` | spawn 时传 `gm` 参数，JS 一次做对 |
| 名字牌 | `player.name_tag = ...` | JS `tagName()` 在 spawn 后设置 |
| 跳跃 | `actor.teleport(头顶)` | `bot:jump` 指令，走引擎跳跃 |
| 贴距离传送 | `actor.teleport(...)` | `bot:teleport` 指令 |
| 进食回血 | `actor.health = ...` | `bot:consume` 指令，引擎结算 |

**游戏模式为什么必须在生成时就定**：`SimulatedPlayer` 会继承生成时
所在世界的游戏模式。服主在创造模式里 spawn 出来的假人**永远无敌**，
事后再由 Python 去改属于补救。现在模式跟着 spawn 请求一起传给 JS，
在生成那一刻就生效。

顺带发现 JS 侧一个坑：`doSpawn` 里 `req.gm !== undefined ? req.gm : 1`
把缺省值**硬编码成了 1 = 创造模式**，而 Python 侧从来不传 `gm`。
两边一凑——假人必然是创造模式。这就是"我创造模式打那个假人，
假人不扣血"的完整成因。现在缺省改成 `undefined`（跟随世界默认），
`keep` 才真的等于"不干预"。

Python 侧保留的只有**只读**访问：`location` / `dimension` / `is_valid` /
`health`，以及 `game_mode`（用来**校验**配置生效没有，不写）。
真要做到零读取需要把整条感知链路改成 JS 上报，风险大收益小，没做。

### 崩溃防护：分两种崩溃，别混为一谈

用户实测遇到过**两次**崩溃，成因完全不同，混在一起会修错方向。

#### 崩溃 A：Python 异常冒到引擎

链条式的：假人身份没被认出来 → 占位 NPC 永远不会被替换 →
智能体拿着一个 **NPC 实体**当身体用。而 NPC 不是 `Player`，
代码里按 Player 用它（读 `game_mode`、调 `navigate_to_location`、
设 `is_name_tag_always_visible`）就抛异常——抛的位置在**调度器回调**里，
异常一路冒到 C++ 引擎，服务器进程直接死。

修法分两层：

1. **不让它抛**：凡是需要 Player 能力的地方先检查再调。
   `_verify_bot_game_mode` 就是例子——遇到没有 `game_mode` 的对象
   （NPC 占位）安静跳过，不算错误。
2. **万一还是抛了，代价不要是整个服务器**：四个周期任务
   （主循环 / 感知 / 连接自检 / 启动自检）全部经过 `_guard` 包装。

```
未包装：回调抛异常 → 冒到 C++ → 服务器崩溃
已包装：回调抛异常 → 记 traceback 到日志 → 跳过这个 tick → 服务器继续跑
```

兜底报告本身也**限流**（`_MAX_GUARD_REPORTS = 3`）。崩在每 tick 的
循环里时，不限量的话日志会被同一行刷爆，反而看不到第一次的现场。

#### 崩溃 B：交坏指令给引擎（`_guard` 拦不住）

```
[03:50:24] Added ticking area from -16, 0, -48 to 47, 0, 31.
[03:50:30] Crash
```

崩在 `tickingarea add` 之后 6 秒，而那条命令的区间是 **y 从 0 到 0
的一个平面**——`y - 30 = -15` 被引擎夹到 0，上下界都成了 0。

> ⚠️ **`_guard` 对这种情况毫无作用。** 它只能保证 *Python 异常* 不外泄，
> 管不了引擎自己崩。所以对"交给引擎的指令"，**唯一有效的防线是下发前
> 校验**——根本别把退化指令交出去。

`_ensure_ticking_area` 现在有三道闸：

1. **三个轴都必须是"下界严格小于上界"**，任一轴相等（平面的）就不发
   命令，打警告说"坐标退化，已跳过"。x/y/z 全查，不只查 y。
2. **x/z 夹到世界边界** ±30,000,000。
3. **下发前把命令原文写进 debug 日志**。引擎如果还是崩了，控制台里至少
   能看到"最后发出去的是什么"，而不是一片空白。

`ticking_area` 因此保持**默认关闭**——它曾被怀疑是崩溃诱因，
没在真机长跑验证之前不该默认打开。

### 为什么假人默认是生存模式

实测现象："我用创造模式打那个假人，假人不扣血。"

这不是 bug，是**继承**：SimulatedPlayer 会沿用生成时所在世界/玩家的
游戏模式，所以服主在创造模式里 spawn 出来的假人一直是创造模式——
无敌、瞬间破坏。后果有两个：打它没反应（没法验证战斗能力），
挖方块没有时长（看起来像作弊）。

`bot_game_mode` 默认 `survival`。**3.0.0 起它是在生成那一刻生效的**：
配置被翻译成数字（`survival→0` / `creative→1` / `adventure→2` /
`spectator→3`），跟着 spawn 请求一起传给行为包，由
`spawnSimulatedPlayer` 直接应用。

| 值 | 传给 JS | 效果 |
|---|---|---|
| `survival`（默认） | `0` | 和真人一样会掉血、会饿、挖石头要花时间 |
| `creative` | `1` | 无敌、瞬间破坏。适合当纯粹的"建筑工" |
| `adventure` | `2` | 不能破坏方块 |
| `spectator` | `3` | 观察者 |
| `keep` | 不发 `gm` | 不干预，跟随世界默认模式 |

> ⚠️ 2.x 是"生成后由 Python 写 `player.game_mode`"。那属于事后补救，
> 而且越界（直接操作假人对象）——见上面的「控制权边界」。

假人上线后 Python 只做一件事：**读一次 `game_mode` 和期望值比对**，
不一致就打警告（提醒"生成参数可能没被引擎采纳"）。不改任何东西。

### 几处刻意反着写的断言

- **"只放开退避不够，去重闸仍然挡得住"**——两道闸是独立的，少任何一道
  都不该漏出重复请求。只测"有闸就行"会让某一道被误删也发现不了。
- **"关掉闸之后确实会刷"**——对照组。没有它，前一条断言可能只是
  "恰好没触发"，而不是"闸真的拦住了"。实测开闸 1 条 / 关闸 40 条。

`_cleanup` 保留 `_failed_at` 这条也有单独用例：它在语义上很容易被一起清掉
（`_attempts` / `_phase` 都清了，顺手第三个也清），一旦清掉退避就形同虚设
——下一次追问走"首次遇到"分支，立刻重发。

---

## 3.0.1：全量源码审计

3.0.0 上线后把全部源码通读了一遍，又找出 **17 处真实 bug**。它们的共同
特征是**不会立刻报错，而是让某条路径悄悄失效**——所以每一处都做了
"先写出复现、再修、再验证"三步，并把验证钉成了测试。

### 第一类：静默失效（最危险，因为没有症状）

| # | 位置 | 问题 | 症状 | 修法 |
|---|------|------|------|------|
| 1 | `bridge.py` | `_remote_names` 四个写入点只有两个做了 `.lower()` | `bot:removed` 带大写名字时删不掉 → "假人已经没了但表里还在"，自愈任务反复去复活一具死透的假人 | 四处统一小写，写进注释当**不变量** |
| 2 | `bridge.py` | `event_id` 里的引号没校验，`json.dumps` 产生 `\"` 被 BDS 的命令解析器吃掉 | JS 侧 `JSON.parse` 抛异常 → **整条指令静默丢失**，Python 只看到"发出去了" | 源头 `_clean_name` 净化名字 + `_raw_send` 拒绝含引号的 event_id |
| 3 | `realbody.py` | `consume_real` 把"命令发出去了"当成"吃到了" | 血没涨也报成功，模型以为吃过东西了 | 改成对比前后血量（新增 `_health()`） |
| 4 | `plugin.py` | `_upgrade_placeholder` 不查存活就升级 | 已停摆的智能体被"复活"，产出**孤儿身体**（有实体、没人控制） | 入口加 `agent.alive()` 检查 |
| 5 | `plugin.py` | `_halt_bodies` 引用 `self._plans`——这个属性**从未定义过** | 每次断连都抛 `AttributeError`，被 `_guard` 兜住后：计划没被中止，那句日志也永远打不出来。**故障现场反而没有日志** | 改用 `self._executor.active` |
| 6 | `plugin.py` | `h_attack` 建了 `done` 字典却从不读，`rid >= 0` 就 `return True` | JS 侧"够不着"/"冷却中"全被记成攻击成功 | 新增 `attack_real` 走"发起 + 等回调"两拍模型 |
| 7 | `actions.py` | 选目标半径 12，JS 实际攻击距离 4.5 | 必然选中站 8 格外的目标，稳定收到"够不着"而模型不知原因 | 新增 `ATTACK_REACH = 4.5` 先筛可达，够不着时明确报距离 |
| 10 | `agent.py` / `plugin.py` | `context_keep_recent=0` 时 `items[:-0] == items[:0] == []` | **静默清空全部历史**（Python 里 `-0 == 0`） | 三处都夹到 `max(1, ...)` |
| 12 | `llm.py` | `optional` 表漏了 `attack.target` / `wander.radius` / `flee.target` | 提示词写着"不填 target 则攻击最近的敌对生物"，照文档写却**被判非法**——那条分支永远不可达 | 补全可选参数表 |

### 第二类：异常逃逸（会崩服务器）

项目最核心的约束是"异常绝不能逃逸到调度器回调"。上一轮只守住了
Python 侧的一半，JS 侧的引擎回调是**裸的**。

| # | 位置 | 问题 | 修法 |
|---|------|------|------|
| 8 | `agent.py` | `_apply` 只有 `try/finally` 没有 `except`；`_call_llm` 的 `try` 只包"调度"动作，真正的执行在之后某个 tick | `_apply` 补 `except` + 新增 `_apply_safely` + `_on_main` 自己包一层（三层防御） |
| 9 | `agent.py` | `_compact_worker` 里 `llm.chat()` 没被 `try` 包住，线程池**静默吞掉**异常 → `thinking` 永不复位 → 智能体**永久呆住**，只能重启服务器 | 把 chat 包进 try，失败造 `LLMReply(ok=False)` 走退化分支 |
| 14 | `main.js` | `scriptEventReceive` 回调是**游戏主线程**直接调的，`doSpawn` 等 handler 裸奔。异常顺着引擎事件派发路径冒上去，而行为包与服务器**同进程** | 新增 `safely()` 统一兜底，switch 里 14 条指令**逐条**包裹（不能在 switch 外统一套——那样一条坏了后面全不执行） |
| 15 | `main.js` | **心跳回调**会抛。最阴的一条：心跳抛一次 → `runInterval` 可能不再调度 → Python 按"多久没收到 pong"判定行为包死亡 → `_halt_bodies` **清掉全部假人**。而日志里只会写"行为包失去联系"，真因不出现在任何地方 | 心跳、坐标上报、启动握手各自包 try |
| 16 | `main.js` | `pumpDigging` 是 `runInterval` 的**裸函数引用**，每 tick 跑，循环体外有裸语句。抛一次就可能让整个挖掘轮询停摆，挖完了也永远不回 `bot:dug`，Python 一直等 | 拆成 `pumpDigging`（外层 try）+ `pumpDiggingInner` + `pumpOneDigging`（逐项兜底，单项坏了回 `bot:dug` 并清出） |

### 第三类：寻路（`pathfinder.py`，本轮补读的模块）

三个 bug 叠在一起，合起来的效果是**竖直方向彻底不可达**——在所有起伏
地形上寻路都会失败，或者返回一条到不了终点的路径。

| # | 问题 | 说明 |
|---|------|------|
| 11 | 抬升/下落分支是**死代码** | `if here < 0: continue` 先把"目标格是实心"全毙了，紧接着 `if here < COST_WATER or here > 0:` 在 `here >= 0` 时**后半段恒真**，于是永远走"同层"并 `continue`。下面的跳跃/下落一次都没执行过 |
| 17a | "同层可走"缺少重力校验 | 只判断"目标格是空气"就让人走过去，可**悬崖边的空气格也是空气**。假人走进去会掉下去，而 A\* 以为它平平安安站着。修法是新增 `_standable()`：看**脚下那一格**有没有支撑，而不是只看这一格是不是空气 |
| 17b | `find()` 返回**不含终点**的路径 | 预算用完 / 堆空了两处都会返回 `closest.chain()`，而**不校验那条路径是否真的以 goal 结尾**。调用方拿到非空列表就以为成功，照着一格格传送，假人走到半路停下——比直接返回 None 更难查。改成两条路径都 `return None` |

为什么 17b 特别值得记：**一个明确报"走不过去"的失败，比一条走一半就断的
路径好查得多。** 前者模型会说"我到不了"，后者模型会说"我到了"然后
坐标没变，于是反复重试同一条指令，看起来像"假人不听话"。

`pathfinder.py` 里还有一条容易写错、现在被测试钉住的语义：**探针的 y 就是
"脚所在的格"**，不是"身体所在的格"。判断能不能站必须同时看 `y`（容身）、
`y+1`（头部空间）和 `y-1`（脚下支撑）。`make_probe`、`walkable_surface`、
`ActionCompiler._floor` 三者在这点上是统一的，端到端测试守着这一点。

### 审计里"确认没问题"的部分

不报的问题和报了的一样重要——避免下次重复怀疑：

- **协议对称性**：Python 发出的 13 个指令 event_id 与 JS `case` 分支
  一一对应；JS 回报的 8 个 event_id 与 Python `kind ==` 分支一一对应，
  无缺口。JS 侧多出的 `bot:list` 是预留探针，`bot:error` 是未使用的
  Python 防御分支（JS 的失败统一走 `bot:result`，因为每个 handler 都有
  `guard`/`try`）。
- **跨模块签名**：`confmig.migrate` / `packing.install` / `ActivityLog.write`
  的调用点与定义全部匹配。
- **`activity.py` / `confmig.py` / `packing.py` / `nbt.py`**：通读后
  未发现可复现的真 bug。
- **坐标语义**：`loc.y` = 脚所在格，`make_probe` / `walkable_surface` /
  `_floor` 三者一致。

---

## 3.0.2：重试语义与"做完一件事就不动了"

3.0.1 之后收到两条实测反馈，指向的是**同一片代码的两个症状**：

> "上个版本它在执行完一次任务后就不动了，有可能是 LLM 在思考，但还是
> 排查一下。还有，有刷屏的日志，最好不要。"

两条都复现了，而且都不是 LLM 的锅。根因都在 `realbody.py` 的重试状态机里，
是**同一批"状态被过早清掉"的缺陷**在两条不同路径上的表现。

### 症状一：`bot:walk` 刷屏

截图上的节奏是 2/2/1/2/1/2/2/1 秒，**约每 1.65 秒一条**，中间夹着正常的
`bot:pong` 心跳。用真实回调时序做复现，测出来是**每 1.55 秒一条**——
和截图吻合到可以确认是同一条路径。

先澄清一点：**退避本身是在工作的**。我一开始误判成"退避退化成重发定时器"，
实测否定掉了——1.55 秒正好是被 `DIG_TIMEOUT` 之类的等待撑长的
`RETRY_BACKOFF_SECONDS`（1.5 秒），说明退避确实在挡，只是**挡完就又发**。

真正的病根是**累加器失效**：

```python
n = self._attempts.get(key, 0) + 1     # ← 永远是 1
if n >= self.MAX_ATTEMPTS:             # ← 永不成立
    ...
```

`_attempts` 是"**本轮正在进行**的那一次尝试"，拿到结论后 `_cleanup` 会
把它清掉。于是 `+1` 每次都从 0 开始，`n` 恒等于 1，"试够 300 次就放弃"
这道闸**形同虚设**——一个走不到的目标会被无限重规划下去，刷屏永不停止。

修法是新增一张**只增不减**的 `_tries` 表专做累加器（`_bump()`），
并明确 `_cleanup` 只清 `_attempts` / `_phase`，**不许碰 `_tries`
和 `_failed_at`**。后者是退避的依据，顺手清掉的话退避就成了摆设。

还有一处**自己重置自己**的写法：

```python
self._mark_failed(key)          # 先把退避时间戳推到 now
if self._backing_off(key):      # 再判退避 → 永远在退避期内
    ...
```

顺序反了。而且执行器是**每 tick 来问一次**的，不挡住的话 `_tries` 会以
每秒 20 次的速度往上涨。实测：加这道闸之前，4 次重试额度在 **0.00 秒**
内烧完——"试 4 次再放弃"实际变成了"试 0.2 毫秒就放弃"。现在必须是
**先判退避、退避期内直接返回、什么都不记**。

### 症状二：执行完一次任务就不动了

复现过程很清楚：

```
提交后 _inflight=1
cancel_all 之后: executor._plans=0, completed=0, plan.state=CANCELLED
_inflight 仍然是 1          ← 谁负责清？
should_think 现在: False    ← 锁死，且不打任何日志
```

`should_think()` 里有一道闸是 `if self.config.wait_for_plan and self._inflight:
return False`。`_inflight` 一旦永久不释放，这个智能体**再也不会思考第二次**
——日志上什么都看不到，看起来就是"它做完第一件事之后站着不动了"。

`_inflight` 有**三个泄漏入口**，全都确认过：

1. `executor.cancel_all()` 只把计划从队列摘掉，既不进 `finished` 也不进
   `completed`——回收路径（`reap_finished`）根本收不到它
2. `plugin._halt_bodies` 走 `plan.abort()`，同样只是改状态
3. 计划还在执行器里，但那个智能体的实体已经没了，插件的 `_tick` 会
   `continue` 掉，`tick()` 压根跑不到它

三条路都不会"把计划交回来"，而 `reap_finished` 是**唯一**的正常回收路径。
修法是加一道**兜底清扫**：`Agent.sweep_inflight()` 主动去执行器核对
**事实状态**（`active` 里的 + `completed` 里的），凡是自己账上有、
执行器里已经没有的，一律摘掉并打一条 warning。

两个容易写反的地方，都钉了测试：

- **必须在 `executor.tick()` 之后调**。清扫靠的是"执行器现在有哪些计划"，
  在 tick 之前扫，扫到的是上一 tick 的旧状态。
- **"执行器空闲"的早退必须排在清扫之后**。原先的顺序是先 `return` 再扫
  ——而执行器"空"恰恰是**最需要扫**的时候（`cancel_all` 把计划全摘掉之后
  `busy` 才变 False）。顺序反了等于**永远扫不到最该扫的那一次**。

### 症状三：放弃这个状态没有表达方式

排查上面两条时挖出来的第三处结构性问题，它同时是两条症状的共同推手。

处理器的返回值只有两种含义：

```
True  → 这一步成功了
False → 这一步还没结论（执行器会原地下次再问）
```

而"我已经试够了，这一步就是做不成"这个**终态没有第三个返回值可用**。
原先的处理是返回 `False`——于是执行器永远认为"还在进行中"，计划既不推进
也不结算，`_inflight` 永不释放。**这正是症状二的另一半**：不只是取消会
泄漏，正常失败也会。

现在约定：真的放弃了就挂 `_gave_up` 标记并**返回 `True`**。返回 `True`
的意思是"这一步**有结论了**"（结论是失败），执行器照常推进游标、记一次
失败，计划正常走向 DONE/FAILED，`_inflight` 被正常回收。

这个改动带出一串必须一起对的细节，每一处都单独写了注释：

| 细节 | 为什么必须这样 |
|---|---|
| `issue()` 改成**三态返回**（`None` / `False` / `True`） | 两态装不下三件事。"已经放弃"和"还在退避"返回值**语义相反**，混在一个 bool 里一定会写错——这个错误我真的写出来过，见下 |
| 已放弃的步骤**必须永远返回 `True`**（幂等） | 曾经写成"`_backing_off` 为真就返回 `False`"，而 `_backing_off` 把"在退避"和"已放弃"混在一起。正常路径下执行器收到 `True` 就推进，不会问第二次，所以看不出问题；但 `retry_step()` 把游标退回来时它会**退化成永久的进行中**，计划卡死且只有一行日志。**这一条是新加的回归测试抓出来的** |
| `_started` 表专管"是不是第一次" | 多阶段处理器（`dig_far` / `place_real`）拿 `_attempts` 当判据，会在拿到结论的下一轮又落回"第一次"分支，重新 `clear_dig_result()` 把**刚拿到的结论扔掉**。实测：连续追问 6 秒，`bot:dig` 始终只有最初那 1 条 |
| `ctl.dig_result_for()` 读完必须 `clear_dig_result()` | 它是**只读**的，不是"取走"。尾部分支里"读到失败 → 记账 → return"的写法，下一轮会**原封不动重演**：同一条旧结论被当成新失败记 N 次，而下面的"重新发起"永远 `return` 不到。实测：`bot:dig` 停在 1 条、`tries` 一路涨到上限，一个本来完全能挖掉的方块被判了死刑 |
| `_retry_later()` 只能"记账"，"重发"必须显式再调一次 `issue()` | 否则它就是一条只记账、不干活的断头路。`place_real` 里同样补了这个"再发一次" |
| `Plan.retry_step()` + `retries_left` 并进 `Plan.total` | 把"重试额度"算进总步数（`total = len(steps) + retries_left`），游标追不上 `total` 就自动结算——不需要独立计数器，`_inflight` 自然能释放 |
| `tick()` 识别"游标被退回" | 处理器调 `retry_step()` 时那个 `True` 的含义是"**这次尝试结束了**"，不是"成功了"，不能记成 `done` |
| 执行器新增 `on_step` 钩子 | 处理器只拿得到 `Step`、拿不到 `Plan`，没有钩子就没法调 `retry_step()`。额度在钩子里发（而不是在提交点发），因为提交点有四五处，漏一处那个计划就**永远没有重试额度** |

**"只记账、不重发"这条断头路，每个处理器都独立地中过招**。这是整轮
排查里最需要警惕的一类 bug——症状和原因完全不搭：

- 日志**干干净净**，一句报错都没有
- `_tries` 老老实实地涨（记账是对的）
- 退避也老老实实地等（闸也是对的）
- 但 `bot:xxx` **永远只有最开始那 1 条**，计划永远走不完

`dig_far` 是先中招的（尾部分支只读结论、从不重发），`place_real` 接着
中招（记完 `_retry_later` 就停），而**最致命的是 `walk_to`**：它比前两个
更隐蔽，因为连 `_tries` 都不涨——重新登记 `_attempts` 的唯一入口是
`issue()`，而 `issue()` 被退避闸挡下时**不登记**，于是
`key not in self._attempts` 永远成立，每一轮都在"首次发起"和"被闸挡下"
之间空转。实测 1200 tick（虚拟 60 秒）的追问：`bot:walk` **只有 1 条**，
然后一等等到天荒地老——**这就是用户看到的"执行完一次任务后就不动了"**。

修法是让所有处理器统一遵守两条不变量（都有测试钉住）：

1. **判"第一次"一律用 `_started`**，绝不碰 `_attempts`——后者会被
   `_cleanup` 清掉，而重登记它的唯一入口可能被闸挡下。
2. **`_fail_or_give_up` / `_retry_later` 后面必须跟着"重新发起"**——
   记账不等于干活，每个处理器的失败收尾都要显式再过一次 `issue()` 加
   真正的发起动作。

还有一个**幽灵函数**顺手清掉了：`realbody.py` 的模块 docstring 从 2.x 起
就写着"见 `plugin.py` 的 `_retry_pending`"，但 `_retry_pending` **从来没有
被实现过**。现在这段说明改成了真实的机制（`_real_or_retry` +
`Plan.retry_step`），不再指向一个不存在的东西。

### 本轮新增的回归测试（64 项）

这一组全是"静默失败"型 bug——不抛异常、日志也不报错，表现只是"不动了"
或者"刷屏"。这类 bug 靠肉眼几乎发现不了，所以每一条都钉了断言：

| 组 | 钉住的行为 |
|---|---|
| 终态 | 试满上限返回 `True`；放弃标记挂上并带原因；**放弃后每一问都返回 `True`（幂等）**；放弃后不再多发请求 |
| 累加器 | `_tries` 跨 `_cleanup` 仍然累加；`_cleanup` 确实清 `_attempts`；`_cleanup` **不**清 `_tries` |
| 首判 | `_started` 跨 `_cleanup` 存活；消费结论后结论确实被消费掉；下一轮会重新发起；同一条结论只记 1 次 |
| 退避 | 200 次追问只记 1 次；退避期内不打放弃标记；退避放开后按次累加、到上限才放弃 |
| 计划额度 | `Plan.total` 含重试额度；`retry_step` 的记账/清 `pending_op`；额度用尽返回 `False`；`abort()` 清零额度 |
| 清扫 | `sweep_inflight` 从执行器反推；清扫在 `tick()` 之后；早退在清扫之后；解锁时打 warning |
| 重发闭环 | 每个处理器的失败路径后面都跟着"重新发起"（`walk_to` 走 `start_walk`、`dig_near` 走 `_reissue`、`dig_far` 走 `start_dig`、`place_real` 走 `_send_place`、`consume`/`attack` 各自重发）；`_reissue` 过闸 + 真发；读结论处都配 `clear` |
| 源码不变量 | 没有裸的 `_tries[x] + 1`；没有拿 `_attempts` 当上限判据；没有拿 `_attempts` 判"第一次"（一律 `_started`） |

`pop_gave_up` 必须是**一次性读取**这条也有单独用例：调用方把失败记进重试
计数之后必须收掉标记，否则下一轮会重复消耗重试额度。

`walk_to` 的端到端用例专门用**真实时间**跑（不是虚拟时间）：退避依据是
`time.monotonic()`，循环跑得飞快时真实时间几乎不动，所有重发都会被挡掉，
测出来的"只有 1 条"是假象。这个坑我在第一次写复现脚本时就踩过。

---

## 3.0.3：IndexError 与"挖方块不走动"

3.0.2 上线后又报了两个问题：

> 主循环每 tick 抛一次 `IndexError: list index out of range`；
> 而且虚拟玩家批量挖方块时**不走动，站在原地面对着方块空挖**。

两条都不是 LLM 的锅，仍是 `realbody.py` / `executor.py` 里的结构性缺陷，
而且**都是 3.0.2 自己引入的**——上一轮修"刷屏"和"不动了"时，把重试机制
改复杂了，埋下了新雷。

### 症状一：主循环 IndexError

线上日志：

```
File ".../executor.py", line 294, in tick
    step = plan.steps[plan.cursor]
IndexError: list index out of range
```

根因：3.0.2 引入的 `Plan.total = len(steps) + retries_left`。`retries_left`
在计划开始处理时**预发 8 格**，而且一个步骤都没失败也一直挂着。于是
`total` 虚高、恒大于 `len(steps)`；游标把真实步骤走完后
`while cursor < total` 仍成立，下一行 `plan.steps[cursor]` 就越界了。

`_guard` 把这异常兜住没崩服务器，但计划永远推不动，主循环每 tick 白跑
一次，日志里只看到三次 IndexError（`_guard` 同一条错误只报 3 次）。

修法：`total` **就是** `len(steps)`，删掉 `retries_left` 整套"重试额度"
机制。游标只会前进、最多走到 `len(steps)`，两者同构才不会越界。顺带把
`Plan.retry_step()`（"把步骤放回队列重试"）改成了 `Plan.note_failed()`
（"这一步放弃、按失败结算"）——因为计划层根本不需要"重试"，真正的重试
发生在 `RealBodyActions` 内部（`_tries` 累加 + 退避 + `MAX_ATTEMPTS`）。

### 症状二：挖方块不走动、对着方块空挖

三处叠加：

1. **`DIG_REACH` 偏大（5 格）**。原版 `breakBlock` 实际可挖距离约 4.5 格，
   中间那 0.5 格是"死区"——假人站在 4.5~5 格之间时 `_within_reach` 判
   "够得着"（≤5）就直接挖，但 breakBlock 够不着，于是挖不动、又因为
   判"够得着"而**不走过去**。改成 4.5，与 `ATTACK_REACH` 一致。

2. **`doWalk` 发出去就返回成功**。`navigateToLocation` 是异步的，但 JS
   侧 `result(id, true)` 是同步的——假人还在路上，Python 就以为"走完了"。
   后果是 `_walk` 立即被清空（walk 超时形同虚设），下一次追问又重发一条
   walk 把寻路反复重置，假人永远走不动。改成 `await` 寻路、用返回的
   `isFullPath` 判断真的走完了没有。

3. **"记账"和"重发"写在同一轮**。这是最隐蔽的一条。处理器拿到失败结论
   后调 `_fail_or_give_up`（记账，会设退避时间戳），紧接着又调 `issue`
   （重发，会判退避）——重发永远撞上**刚设**的退避。实测：一个挖不动的
   方块，`bot:dig` 只发最初那 1 条，`tries` 却一路涨到上限然后放弃。
   也就是说"重试"变成了"空等"，一次都没真的重发。

   修法是把两者**分轮**：拿到失败结论的那一轮只记账（并消费掉这条结论），
   下一轮读不到结论时，由 `issue()` 在退避期过后放行重发。为此给
   `BodyController` 补了 `clear_walk_result()`（和 `clear_dig_result()`
   对称），让 `last_walk` 这条只读结论也能被消费。

   六个处理器（`dig_near` / `walk_to` / `dig_far` / `place_real` /
   `consume_real` / `attack_real`）统一改成了这个模式。

### 本轮修复清单

| 文件 | 改动 |
|---|---|
| `executor.py` | `total` 恢复 `len(steps)`；删 `retries_left`；`retry_step` → `note_failed`；删 tick 里"游标退回"逻辑 |
| `plugin.py` | `_real_or_retry` 改走 `note_failed`；删 `PLAN_STEP_RETRIES`；`_note_step_owner` 只挂 `_exec_plan` |
| `realbody.py` | 六个处理器的失败路径统一"记账/重发分离"；删 `_reissue` |
| `body.py` | 新增 `clear_walk_result` |
| `actions.py` | `DIG_REACH` 5 → 4.5 |
| `main.js` | `doWalk` 改 `await navigateToLocation` + `isFullPath` 判定 |

验证：真实退避（1.5 秒）下，`dig` 和 `walk` 都能正确重发（发 5 条后放弃，
而不是只发 1 条空等）；十步计划推进全程无 IndexError。

---

## 3.0.4：挖树"假人不动、walk 刷屏"（对齐官方 API 文档）

3.0.3 又翻车了：LLM 决定挖树，假人**动都不动**，`bot:walk` 每 1.7 秒一条
持续刷屏。这次的根因查到了实处——**3.0.3 里 `doWalk` 对
`navigateToLocation` 的用法是我凭印象写的，和官方文档对不上**。

### 官方文档确认的事实

微软 Script API 文档（learn.microsoft.com，`@minecraft/server-gametest`
的 `SimulatedPlayer`）：

- `navigateToLocation(location: Vector3, speed?: number): NavigationResult`
  是**同步**方法——返回 `NavigationResult`，**不是 Promise**；
- **没有 `timeout` 参数**（3.0.3 传了第 3 个参数，被静默忽略）；
- `NavigationResult` 只有 `isFullPath`（是否找到**完整路径**）和
  `getPath()`——`isFullPath` 是**寻路规划结果**，调用瞬间就返回，
  不是"已经走到了"，且**没有"导航完成"事件/查询**；
- 文档另注明：假人必须**接触地面**才会开始导航；卡住时会自动停下。

Endstone 官方文档（endstone.dev，Python reference）：
`Actor.location` / `dimension` / `is_on_ground` 存在且用法与本项目一致；
但 Endstone Python **没有任何移动/导航 API**（只有 `teleport`）——
移动必须走 JS 行为包，分层方向本身没有错。

### 3.0.3 错在哪

```js
const nav = await sim.navigateToLocation(target, 1.0, req.timeout || 100);
result(id, nav.isFullPath === true, ...);
```

`await` 一个同步对象会**立即**返回——所以这条 result 读的是**寻路规划
结果**：规划失败（比如目标是悬空格）就回 false。挖树砍到高处后
`_approach_point` 选中的"落脚点"在**树旁的空中**，`navigateToLocation`
对悬空目标规划不出完整路径 → 每条 walk 都失败 → 记账 → 退避 1.5 秒 →
重发 → 永远失败。假人从未开始移动，控制台每 1.5 秒一条 bot:walk。

### 修复

| 位置 | 改动 |
|---|---|
| `main.js` | `doWalk` 按官方签名重写：同步调用 `navigateToLocation(target, 1.0)`；`isFullPath !== true` 立即回"没有可达路径"；规划成功则用 `runInterval` **轮询位置**，到达目标 2 格内回成功、超时回失败。轮询状态挂在 `walking` 表上，`doStop`/`doRemove` 会清理 |
| `realbody.py` | `_approach_point` 候选格增加**脚下有支撑**过滤（悬空格直接排除），并把**低一层侧向格**补进候选（挖树干第二段时同层侧向全是悬空的，能站的其实是低一格的地面） |
| `realbody.py` | `dig_far` 寻路途中若已经够得着目标方块，**立即 `cancel` 寻路、原地开挖**（`ctl.cancel` 会发 `bot:stop` → JS `stopMoving()`，官方 API）——不必等寻路走完或超时 |
| `realbody.py` | 新增 `WALK_MAX_ATTEMPTS = 8`：走不到的原因通常是目标悬空，重试 8 次（约半分钟）就放弃，不再用挖掘的 300 次额度连刷 7 分多钟 |
| `realbody.py` | `MAX_ATTEMPTS` 300 → 30：挖不动的方块（基岩/保护区域）每次要等挖掘超时 + 退避，300 次要空耗半小时 |
| `body.py` | `start_walk` 默认超时 100 → 200 tick（10 秒），长距离寻路不再频繁"超时→重试"跑冤枉路 |

### 为什么这次可信

- JS 里用到的每个 API（`navigateToLocation` / `NavigationResult.isFullPath` /
  `stopMoving` / `stopBreakingBlock` / `system.runInterval`）都在微软
  Script API 文档里有明确签名，注释里标了出处；
- Endstone Python 侧只用了文档中的 `Actor.location` / `dimension`，
  未新引入任何接口；
- 悬空候选过滤、半路提前开挖都有构造世界的单元测试钉住
  （`test_approach_point_and_early_dig`）。

---

## 3.0.5：挖掘"点击不真挖 / 对着空气挖"

3.0.4 之后能走能挖了，但又有三个新症状（用户实测）：

> 一直点击方块，不真挖；开始挖的时候对着空气挖；打它两下它才走到
> 树前面。

三个根因，对应三个**独立的**毛病：

### 1. 对着空气挖 → 没转向

`breakBlock` 要打在方块上。假人走到落脚点后**没有朝向目标方块**，
背对/侧对时 `breakBlock` 就是"点空气"。修法：`breakBlock` 之前先
`sim.lookAtBlock(loc, LookDuration.Instant)`（`lookAtBlock` 和
`LookDuration` 都是微软 Script API 文档里的正规接口，`Instant` =
瞬间转向）。

### 2. 一直点击不真挖 → 进度被反复重置

原代码每 10 tick 补一次 `sim.breakBlock(ctx.loc)`。但文档明确：
`breakBlock` 会**持续敲击直到方块破坏**（或使用物品 / `stopBreakingBlock`）。
反复调用等于每 0.5 秒把破坏进度**归零重来**——方块永远挖不掉，
看起来就是"一直点击，不真挖"。修法：删掉这个补挖，改成**只在假人
够不着（被击退走远）时提前结束**，交给 Python 重新寻路。

### 3. 挖原木永远超时 → 挖掘超时太短

挖掘轮询上限 100 tick（5 秒），而空手挖原木约 15 秒、石头约 7.5 秒，
稍硬的方块必然"超时失败"。修法：JS 侧上限 100 → 600 tick（30 秒），
Python 侧兜底 `DIG_TIMEOUT` 8 → 30 秒。

| 位置 | 改动 |
|---|---|
| `main.js` | `doDig`：`breakBlock` 前 `lookAtBlock`；挖掘轮询上限 100 → 600 tick |
| `main.js` | `pumpOneDigging`：删掉每 10 tick 补 `breakBlock`；新增"假人够不着（被击退）→ 提前回失败" |
| `body.py` | `DIG_TIMEOUT` 8 → 30 秒 |

---

## 3.0.6：挖掘收尾 + 游戏知识库 + 合成/熔炉/箱子工具

### 1. 挖掘"即将挖掉时停止、重新挖"

3.0.5 解决了"对着空气挖"，但还剩一个症状：挖到快破时中断、进度归零重来。

根因有两处（都对着官方文档核对过）：

- `lookAtBlock` 之前用 `LookDuration.Instant`（瞬间转向）——假人"看一眼
  就转回头"，挖掘中途视线离开方块导致 `breakBlock` 中断。改成
  `LookDuration.Continuous`（持续看向，直到移动）。
- 中断后没有恢复机制，只能干等 30 秒超时。补回**低频续挖**：每 20 tick
  （1 秒）重新 `lookAtBlock + breakBlock`，既能在中断后接上，又不至于像
  早期的每 10 tick 那样高频干扰。

### 2. 游戏知识库（会玩 MC）

把用户整理的基岩版生存知识库（15 个分卷）复制进 `knowledge/` 目录，
并从中精选出一份**生存玩法速查**（采矿等级、高频合成表、食物数值、
矿石分布高度）作为新的提示词段落 `$game_knowledge`——默认打进系统提示词，
可在 config.json 里整段改写。完整分卷保留在 `knowledge/` 里备查。

### 3. 合成 / 熔炉 / 箱子工具

新增四个工具（`craft` / `smelt` / `store` / `take`），配套 JS 侧
`bot:container` 容器指令。容器 API 全部来自 @minecraft/server 官方文档：

| 工具 | 作用 |
|---|---|
| `craft(item)` | 到附近工作台合成（内置高频配方：木板/木棍/工作台/熔炉/箱子/火把/木石铁工具/面包/盾牌） |
| `smelt(item, count)` | 到附近熔炉熔炼（铁矿石→铁锭、沙→玻璃、圆石→石头、原木→木炭等），燃料用煤/木炭 |
| `store(item, count)` | 把背包物品存进附近箱子 |
| `take(item, count)` | 从附近箱子取物品到背包 |

实现要点：
- JS 侧用 `block.getComponent("minecraft:inventory").container` 拿容器，
  用 `getItem`/`setItem`/`addItem`/`clearAll` 读写（官方 Container 接口）；
- 合成走工作台 3×3（slot 0~8 输入、slot 9 输出），熔炉走 slot 0=材料/
  1=燃料/2=成品——这两处 slot 布局是原版 UI 固定布局，**仍需在真实服务器
  实测确认**（沙箱里无法验证引擎容器行为）；
- 材料匹配用短名后缀（`planks` 匹配任意 oak_planks/birch_planks…）。

### 本轮修复清单

| 文件 | 改动 |
|---|---|
| `main.js` | `faceBlock` 用 Continuous；`pumpOneDigging` 低频续挖；新增 `doContainer`（put/take/smelt/craft）+ 短名匹配 |
| `body.py` | （3.0.5 已含）DIG_TIMEOUT 30 秒 |
| `agent.py` | 新增 `DEFAULT_GAME_KNOWLEDGE` 段落 + `game_knowledge` 配置字段 |
| `plugin.py` | 加载 `prompt_game_knowledge`；注册 `container` 步骤处理器 |
| `actions.py` | 内置 `CRAFT_RECIPES` / `SMELT_RECIPES` + `_do_craft/_do_smelt/_do_store/_do_take` |
| `realbody.py` | 新增 `container_op`（发起+等回调） |
| `bridge.py` | 新增 `container()` 指令 |
| `llm.py` | 工具表加 `craft/smelt/store/take` |
| `knowledge/` | 复制知识库分卷 |

---

## 3.0.7：三个实测 bug 回归（单块挖不掉 / 找物限同一层 / 上下坡寻路失效）

3.0.6 之后用户又报了三个症状，全部先写出复现测试再修：

> 直连模式挖不掉单个方块；找矿永远找不到不在同一 y 的矿；目标在坡上时
> 寻路直接报不可达。

### 1. 单块挖不掉 → `"air" in name` 子串误判 + 缺少单块入口

`break_block` 判断"是不是已经是空气"用的是子串匹配：
`"air" in "oak_stairs"` 成立 → 直接返回 False，楼梯永远挖不掉。
修法：与 `is_air` 同一套**精确集合**（`_AIR_NAMES = {air, cave_air,
void_air}`），并新增 `mine_block` 工具给模型一个明确的"挖这一块"入口
（`mine_area` 是批量语义，模型不会为一块方块用它）。

### 2. 找物只限同一 y → 垂直搜索范围太窄

`_find_blocks` 垂直方向只搜 ±6 格：山丘/矿道坡面上下差超过 6 格就
什么都找不到。修法：垂直跟水平一样搜 ±R，dy 由近及远展开，加查询
预算（32000 次读）封顶主线程开销——超预算时漏掉的是最远层，近处
命中不受影响。

### 3. 上下坡寻路失效 → 悬空 goal 不修正

以前只在"goal 是实心"时才修正 y，悬空 goal（山腰、坡面上的旧高度）
直接交给 A* → 永远不可达。修法：**不管什么情况**都先找 (x,z) 处的
站立点（`walkable_surface`，range ±12/8），找不到才保留原值；
`probe` < 0（虚空/深坑）才报不可达。

另外修正了一条**假绿测试**："低频续挖"用例匹配的是源码字符串里的
注释（`"breakBlock" in po`），而行为早已改成"不重复调 breakBlock"。
断言更新为新行为，并加了反向断言防止回退。

| 位置 | 改动 |
|---|---|
| `world.py` | `break_block` 空气判断改精确集合（不再误杀 stair） |
| `actions.py` | 新增 `_do_mine_block`（单块挖掘，复用 dig_steps） |
| `llm.py` | 工具表加 `mine_block(x,y,z)` |
| `actions.py` | `_find_blocks` 垂直 ±R + 查询预算 32000 |
| `actions.py` | `_plan_path` 无条件 `walkable_surface` 落地修正 |
| `tests/` | 新增 `test_reported_bugs_307` 回归节（真实替身，非字符串检查） |

---

## 已知限制

- **必须重启服务器**才能让行为包生效。BDS 不支持运行时加载新行为包，
  这是平台限制，不是实现偷懒。**退出游戏客户端不算**——服务器进程没退，
  就什么都不会重新加载。
- **Beta APIs 必须开启**。`@minecraft/server-gametest` 是 beta 模块。
  开启后会在世界存档里留下实验标记，**且不可逆**（可以继续玩，
  但关掉后已有存档可能受影响）。插件会先备份 `level.dat`。
- **破坏速度受游戏模式影响**。`breakBlock` 遵循玩家游戏模式的规则，
  生存模式下挖石头比创造模式慢。这是特性不是 bug——它让挖掘时长
  看起来是对的。
- **假人的皮肤取决于它加入时用的账号**。SimulatedPlayer 没有换皮肤的
  接口。`npc` 模式下用 NPC + 染色头盔做区分。
- **挖掘耗时跟方块硬度挂钩**，所以挖一片石头区域需要真实的等待时间。
  这是刻意的——它让挖掘时长看起来是对的。想要瞬时的效果，就只能在
  `npc` 模式下工作，但那已经不是虚拟玩家了。
- **压缩是一次性的，不重试**。总结接口挂掉时走原文截断兜底，不会排队
  重试——重试会拖着上下文不回落，反而更亏。
- **压缩会丢细节**。400 字的提要装不下"第 37 格的石头挖了几 tick"这种
  粒度。需要精确保留的事实应该写进 `notes`（`remember` 工具），
  它不参与压缩。
- **activity.log 也不记细节**。它只写结论（"挖区域 9/9 完成"），不写
  每格方块的坐标、不写 LLM 的原始回复。要看这些得翻控制台日志，
  并把 `log_echo_console` 打开。设计上就是两套日志分工，不要指望
  activity.log 能当排障日志用。
- **日志写在游戏主线程上**。单条追加是几十微秒级的，但 `log_max_bytes`
  调得极小又开着轮转时，轮转要挪 5 个文件，那一 tick 会有可感知的停顿。
  正常配置下不会遇到。
- **表面判定只做六面邻接，不做视线传播**。隔着半透明方块（玻璃、树叶）
  或隔着岩浆的矿不会被算作"表面"——即使真实玩家理论上能透过它们看见。
  这是刻意的简化：隔着玻璃看见的矿在游戏里也挖不到，得先破玻璃。
- **"表面"是瞬时快照**。假人挖开一层后，下一轮感知才刷新；刚暴露的矿
  不会在同一个 tick 里立刻出现，这是正常的，不是漏报。
- **提示词段落可以改坏**。把 `prompt_rules` 改成半句话、删掉 `surface_hint`，
  插件不会崩（`safe_substitute` 兜底），但模型的判断会变差。改坏了用
  `/ai prompt reset <段落>` 一键还原默认。
- **`plugin.py` 不能启用 PEP 563**（`from __future__ import annotations`）。
  Endstone 注册事件时用 `inspect.isclass(params[0].annotation)` 校验签名，
  而 PEP 563 会把注解变成字符串，导致**每个事件处理器都被拒绝注册**——
  插件能启动、命令能用，但行为包的消息通道没接上，身体永远连不上。
  报错特征是签名带引号：`(event: 'PlayerChatEvent')`。
  `test_activity.py` 用 AST 检查 + 真实签名复核把这个钉住了。
- **distribution name 必须是连字符格式** `endstone-<插件名>`。写成下划线
  （`endstone_arc_fake_player`）会被 Endstone 直接拒绝加载，报 `Invalid name`，
  症状是"插件目录里什么都没发生"。
- **假人被打掉后，那个智能体就结束了**，不会自动重生。身体一旦被移除
  （创造模式打掉、掉虚空、被 `/kill`），插件会立刻停掉它的决策、取消
  排队中的动作、摘掉所有引用，并在控制台打一条黄色警告。想再要一个就
  重新 `/ai spawn`。这是有意的——自动重生会让人分不清"它是不是同一个人"。
- **占位实体（NPC）在真身加入前会短暂出现**。`body_mode: "auto"` 下，
  插件先发命令让行为包造真身，真身加入服务器需要一两秒。这段时间里
  屏幕上那个是 NPC 占位实体——**它显示 NPC 皮肤、不会动**。真身一加入
  就会被自动换掉（旧的 NPC 会被移除）。如果它一直不消失，说明真身
  从来没加入成功，去控制台看行为包握手日志。
- **造虚拟玩家依赖实验性 API**。`spawnSimulatedPlayer` 属于
  `@minecraft/server-gametest`，Mojang 至今标为 pre-release。必须打开
  世界设置里的 **Beta APIs** 实验开关，且不保证在未来的正式版引擎里
  继续可用。插件会在生成前先探测行为包能力，造不出来时**直接报错而不是
  拿 NPC 顶替**，并把原因写进 `/ai body` 的诊断里。
- **AI 请求失败会等一分钟再试，这期间智能体不动**。网络断掉时这是
  想要的行为（不刷屏、不空转），但如果你希望更快恢复，把
  `llm_retry_seconds` 调小即可。默认永不放弃重试；想限制次数就设
  `llm_max_retries`（超过后智能体会结束，需要手动重启）。
- **常加载区域默认关闭，且曾被怀疑是服务器崩溃的诱因**。`tickingarea`
  的区间如果退化（比如 y 从 0 到 0 的平面），引擎会崩——2.0.0 的实测日志
  里 `Added ticking area ...` 之后 6 秒就是 `Crash`。现在下发前会校验
  三个轴、拒绝退化区间，但**没在真机长跑验证过**，所以默认不打开。
  想用得自己开，并且留意控制台。
- **常加载区域需要服主有 `tickingarea` 权限**。插件用
  `dispatch_command` 以服务器身份下发，正常情况一定成功；但如果服务端
  装了权限类插件拦截这条命令，会退回到"没有常加载"的默认行为——
  这不影响假人正常活动（它本来就走到哪加载到哪）。
- **常加载区域有内存代价**。打开后默认半径 30（约 61×61 格水平范围）。
  同时跑好几个假人、或者把 `ticking_area_radius` 往大调，会明显增加
  服务器内存占用。假人被移除时区域会自动摘掉；服务器异常退出则可能
  留下残留，用 `/tickingarea list` 查、`/tickingarea remove pa_<名字>`
  手动清。
- **假人名字别和真人账号重名**。撞名时插件会忽略那个真人并打警告（只打
  一次，不会刷屏），但行为包那边用的是同一个名字，仍然可能出问题。
  给假人起个专属名字（比如 `bot_1`）最省事。
- **无法给单个玩家设置视距**。BDS 的 `view-distance` 是
  `server.properties` 里的**全局**设置，只作用于所有玩家。客户端选项
  里那个"可视距离"会被服务器覆盖。假人不需要单独设——它就是模拟玩家，
  区块加载跟随它自己的位置，和真人一致。
- **⚠️ 离线登录的真人会被误判成假人**。判据是"`xuid` 为空"，而离线模式
  下真人的 xuid 也是空的。如果你服务器有离线玩家，请开正版验证
  （`online-mode=true`）。启动自检会检测并警告这种情况。详见上面的
  [离线真人的代价](#离线真人的代价用户已知晓并接受)。
- **假人身份判定依赖 `xuid` 属性存在**。如果将来某个 Endstone 版本不再
  暴露它，会退到"三项设备特征 `device_id` / `device_os` / `game_version`
  全空"这条更严的兜底——那条路在 2.1.0 实测中认不出真实假人，所以届时
  可能整个判据失效（症状是假人接不上、日志刷"同名警告"）。
- **不保证跨 Endstone 版本可用**。已核实 0.11.11 的行为：
  `@minecraft/server-gametest` 的 JS API（`disconnect`、
  `navigateToLocation`、`setSkin`…）**没有**桥接到 Python 侧，
  `Player.is_simulated` 也不存在。升级 Endstone 后建议先跑一遍
  `tests/test_v2_fixes.py` 的第七部分。

---

## 许可

MIT
