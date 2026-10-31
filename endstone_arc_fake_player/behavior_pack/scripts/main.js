/**
 * ArcFakePlayer 假人桥接行为包。
 *
 * 这个脚本只有一个职责：给 Endstone 那边的 Python "大脑" 提供一具真实的
 * SimulatedPlayer 身体。大脑负责思考、寻路、批量展开；这里负责让身体
 * 真的动起来、真的挖方块。
 *
 * 为什么需要它
 * ------------
 * Endstone 的 Python API 能读写方块、能传送实体，但创建不出 SimulatedPlayer。
 * 真正提供 spawnSimulatedPlayer 的是 @minecraft/server-gametest 模块，跑在
 * 独立的 JS 运行时里。两个运行时之间唯一的通道是 scriptevent 命令。
 *
 * 通信协议
 * --------
 * Python → 行为包（每条都带 t 令牌和 i 请求号）:
 *   bot:ping      {}                                  心跳
 *   bot:spawn     {n,x,y,z,d}                         生成假人
 *   bot:remove    {n}                                 移除假人
 *   bot:teleport  {n,x,y,z,d}                         瞬移（仅调试用）
 *   bot:walk      {n,x,y,z,timeout}                   真寻路走过去
 *   bot:dig       {n,x,y,z,tool}                      真挖掘（异步，会挖一会儿）
 *   bot:place     {n,x,y,z,block,face}                放置方块
 *   bot:attack    {n,target,times}                    攻击实体
 *   bot:select    {n,item}                            切换手持物品
 *   bot:equip     {n}                                 穿上背包里最好的护甲
 *   bot:stop      {n}                                 停止一切动作
 *   bot:list      {}                                  列出假人
 *
 * 行为包 → Python:
 *   bot:pong      {names}                             心跳响应 + 当前假人列表
 *   bot:spawned   {n,ok,existed}                      生成完成
 *   bot:removed   {n}
 *   bot:result    {i,ok,e}                            同步请求完成（walk/place/attack）
 *   bot:dug       {i,n,x,y,z,ok,swing}                挖掘完成（异步）
 *   bot:busy      {i,n,x,y,z,e}                       挖掘时被打断
 *   bot:positions {p}                                 定期上报坐标
 *
 * 关于"真挖掘"
 * -----------
 * SimulatedPlayer.breakBlock(loc, direction) 的官方说明是：
 *
 *     "Destroys the block at blockLocation, respecting the rules of the
 *      server player's game mode. The block will be hit until broken, an
 *      item is used or stopBreakingBlock is called.
 *      Returns true if the block at blockLocation is solid."
 *
 * 有两件事必须看清楚，它们直接决定了挖掘该怎么写：
 *
 *  1. **它只认坐标**。文档对"视线、距离、中间有没有挡着"只字未提，实测
 *     也是这样：把任意一个坐标丢进去，它会照挖，哪怕假人背对着、哪怕隔着
 *     三堵墙、哪怕方块在二十格外。所以**绝不能**把 Python 传来的坐标直接
 *     交给它 —— 那就是"穿墙挖"。唯一的约束来自 `getBlockFromViewDirection`
 *     射线：只有射线打到的正是这一格，才允许动手（见 pumpOneDigging）。
 *
 *  2. **它是一次"持续敲击"**，不是瞬间变空气：有挥臂、有按硬度算的耗时、
 *     有掉落物。但它停下来的条件里**没有**"假人转头"。所以挖掘期间正确的
 *     做法是**每 tick 补发一次 breakBlock**（Understudy、FlashFakePlayerPack
 *     两个成熟假人包都是这么写的，`system.runInterval(breaks, 0)`），
 *     而不是"只发起一次然后干等"——后者一旦中途被打断就只能靠超时重启，
 *     表现出来就是"一顿一顿地挖"。
 *
 * 因此 bot:dug 仍是异步的：每 tick 敲一次，直到方块真的变成空气（或判定
 * 视线丢失/够不着）才回结论。
 */

import * as gametest from "@minecraft/server-gametest";
import { system, world, ItemStack, EquipmentSlot } from "@minecraft/server";
import {
    initArenaCombat,
    rememberSpawnPoint,
    findNearestEnemy,
    fireSimShot,
    watchRespawn,
    getRespawnPoint,
    setRespawnPoint,
    verifyGroundPoint,
    forgetBot,
    isAlive,
    setSimEnemies,
} from "./arena_combat.js";

// ---------------------------------------------------------------------------
// 状态
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// 假人创建：两条路，谁通用用谁
// ---------------------------------------------------------------------------
//
// 这里踩过一个很深的坑，值得写下来。
//
// 最初的写法是只留 GameTest 那条路：
//
//     register("arc_fake_player", "sim_spawner", (test) => { activeTest = test; ... })
//     ...
//     const sim = activeTest.spawnSimulatedPlayer(loc, name);
//
// 问题是 `register()` 只是**注册一个测试定义**，它回调里的 `test` 参数
// 只有在测试被真正执行时才存在。而测试要靠 `/gametest run arc_fake_player:sim_spawner`
// 启动 —— 没人敲这条命令，`activeTest` 就恒为 null，所有生成请求都堆进
// pendingSpawns 里永远出不来。屏幕上留下的就是那个占位 NPC。
//
// 正确做法：`@minecraft/server-gametest` 模块本身也导出了
// `spawnSimulatedPlayer(location, name, gameMode)`，它**不依附于任何 Test**，
// 可以随时调用。这才是给 add-on 用的正规入口。
//
// 所以下面的 `ensureSim` 优先走模块级函数；只有它不存在（老版本引擎）
// 时才退回 GameTest，并回报 `gm` 字段让 Python 侧知道该不该去跑测试。

/** GameTest 实例。模块级 spawnSimulatedPlayer 不可用时才用得上。 */
let activeTest = null;

/** 记录这条退路有没有真的被用上，供 Python 侧判断 */
let usedTestFallback = false;

/** 名字 → SimulatedPlayer */
const sims = new Map();

/** GameTest 未就绪时排队的生成请求 */
const pendingSpawns = [];

/** 鉴权令牌，从 Python 的第一条消息里学到 */
let bridgeToken = "";

/** 正在进行的挖掘: `${name}|${x},${y},${z}` → 挖矿上下文 */
const digging = new Map();

/** 正在进行的寻路轮询: name → { id, timer, settled } */
const walking = new Map();

/** 需要等待引擎完成的使用物品/容器动作。停止、移除时一并结算。 */
const interactions = new Map();

function finishInteraction(name, state, ok, err, extra = {}) {
    if (state.settled) return;
    state.settled = true;
    system.clearRun(state.timer);
    if (interactions.get(name) === state) interactions.delete(name);
    reply("bot:result", { i: state.id, ok, e: err || "", ...extra });
}

function cancelInteraction(name, reason) {
    const state = interactions.get(name);
    if (state) finishInteraction(name, state, false, reason);
}

/** 每个假人的忙碌状态，防止一条指令没做完又来一条 */
const busyUntil = new Map();

/**
 * 挖掘允许的最大**射线**距离（格）。和 Understudy / FlashFakePlayerPack
 * 两个成熟假人包一致，取 6。
 *
 * ⚠️ 这是"不许隔墙挖"的核心闸门，不是装饰。见文件头"关于真挖掘"：
 * `breakBlock` 只认坐标、不校验视线与遮挡，所以约束只能由**射线**提供——
 * `getBlockFromViewDirection` 从假人头部发一条射线，返回第一个被挡到的
 * 东西。先看它打到哪一格，只有打到的**正是目标方块**才允许 breakBlock。
 */
const DIG_RAY_DISTANCE = 6;

/**
 * 挖掘允许的最大实际距离（格），从假人脚下量到目标方块中心。
 *
 * 原版生存交互距离约 4.5，这里给到 5.0 以容忍"站在格子边缘"的情况。
 * 射线闸门管"看得见"，这一条管"够得着"，两道合起来才是完整约束——
 * 少了任何一道，`breakBlock` 就会变成隔墙取物的作弊器。
 */
const DIG_MAX_DISTANCE = 5.0;

/**
 * 连续多少 tick 瞄不上目标，就再转一次头（重发 lookAtBlock）。
 *
 * 为什么要连着数而不是一次就算：假人挥臂时头部有轻微摆动，单帧 miss 很
 * 常见；而且 `lookAtBlock` 是异步生效的，刚发出的转向要几拍才落地。
 *
 * ⚠️ 这个值直接决定"到底挖不挖得动"：它太小 → 头还没转正就判"瞄不上"、
 *    反复重瞄永远不收敛 → 表现出"它明明看得见却挖不动"。所以给足余量
 *    （8 拍 ≈ 0.4 秒），远大于引擎实际转向耗时（1~3 拍）。
 *    真正的"被遮挡"由 `MAX_AIM_RETRIES` 轮之后仍然 miss 才能判定，
 *    不会因为拍数大就漏判。
 */
/**
 * 要不要"射线必须打到目标那一格"才允许挖（严格视线闸门）。
 *
 * 默认 **false**。这段是被反复改错过的地方，原因必须留档：
 *
 *   3.0.12 把它做成**硬闸门**（`return` 掉、不调 breakBlock）。道理没错——
 *   `breakBlock` 确实只认坐标、不判视线；但真机上射线**几乎永远匹配不到
 *   目标那一格**：`lookAtBlock` 是异步的，而 `getBlockFromViewDirection`
 *   返回的是"视线扫到的第一格"，和"我要挖的那一格"常常不是同一个。于是
 *   `breakBlock` **一次都没被调到**，用户看到的就是"假人盯着方块不动手"
 *   —— 连着报了三次"不挖方块"，都是这一个原因。
 *
 *   所以默认关闭：挖照挖，防作弊改由 `DIG_MAX_DISTANCE` 负责（身体本来
 *   就会先走到方块旁边，见 Python 侧 `DIG_REACH = 4.5`，"隔空/超距"先被
 *   那一道拦住）。
 *
 * 如果你确实想"看不见就绝不许挖"，把它改成 **true**。
 */
const DIG_STRICT_SIGHT = false;

const AIM_LOST_TICKS = 8;

/**
 * 重瞄几次仍瞄不上，就认定"这个方块根本不在视野里"（被遮挡 / 已经走开），
 * 如实回报失败。
 *
 * 总容忍额度 = (MAX_AIM_RETRIES + 1) × AIM_LOST_TICKS = 40 拍 ≈ 2 秒，
 * 足够覆盖异步转向；超过 2 秒还瞄不上，才是有东西真挡着（或假人已经
 * 走开）。宁可等这 2 秒也不误判，因为误判的代价是"能挖却挖不动"。
 *
 * 失败时会把**挡住视线的那一格**连同类型一起报上去，而不是只丢一句
 * "挖掘失败"——AI 看到 `minecraft:stone @ 12,64,7` 才知道下一步该先挖掉
 * 它，或者换个位置，而不是在原地重试。
 */
const MAX_AIM_RETRIES = 4;

/**
 * 单次挖掘的硬上限（tick）。空手挖原木约 300 tick、石头约 150 tick，
 * 900 tick（45 秒）足够任何正常方块挖完；再往上就是真挖不动了
 * （基岩、被保护、或者方块在射线边缘反复闪烁）。
 *
 * 这**不是**"每一轮的耐心"——现在每 tick 都在敲，不存在"发起一轮"的
 * 概念了，它纯粹是防死循环的兜底。45 秒也远小于 Python 侧 150 秒的
 * DIG_TIMEOUT，不会出现"假人还在挖、Python 已判超时"的误杀。
 */
const DIG_LIMIT_TICKS = 900;

/**
 * 结束某个假人的寻路轮询并回报结果。
 * 供 doWalk 的轮询和 doStop/doRemove 的清理共用，保证一条 walk
 * 最终**必然**回一次 bot:result（Python 侧的 walk 超时依赖它）。
 */
function settleWalk(name, state, ok, err) {
    if (state.settled) return;
    state.settled = true;
    system.clearRun(state.timer);
    if (walking.get(name) === state) walking.delete(name);
    result(state.id, ok, err);
}

// ---------------------------------------------------------------------------
// 通信原语
// ---------------------------------------------------------------------------

function reply(eventId, data) {
    try {
        const msg = JSON.stringify({ ...data, t: bridgeToken });
        world.getDimension("overworld").runCommand(`scriptevent ${eventId} ${msg}`);
    } catch (e) {
        console.warn(`[ArcFakePlayer] 回复失败: ${e}`);
    }
}

/**
 * 生成一个假人。返回 { sim } 或 { err, needTest }。
 *
 * 三条路，按"通用性"从高到低试：
 *   1. 模块级 spawnSimulatedPlayer —— 不依赖任何 Test，最通用
 *   2. 已经启动过的 GameTest 实例 —— 需要 Python 侧先跑 /gametest run
 *   3. 都没有 —— 明确告诉 Python 侧"我需要你把测试跑起来"
 */
function ensureSim(loc, name, gameMode) {
    let lastErr = "";

    // 路 1：模块级函数。这就是 add-on 想要的那条路。
    try {
        if (typeof gametest.spawnSimulatedPlayer === "function") {
            const sim = gametest.spawnSimulatedPlayer(
                { x: loc.x, y: loc.y, z: loc.z, dimension: world.getDimension("overworld") },
                name,
                gameMode,
            );
            if (sim) return { sim: sim };
            lastErr = "模块级 spawnSimulatedPlayer 返回 undefined";
        } else {
            lastErr = "模块级 spawnSimulatedPlayer 不存在（引擎版本过旧）";
        }
    } catch (e) {
        lastErr = `模块级生成失败: ${e}`;
    }

    // 路 2：GameTest 实例。需要 /gametest run 已经跑过。
    if (activeTest) {
        usedTestFallback = true;
        try {
            const sim = activeTest.spawnSimulatedPlayer(loc, name, gameMode);
            if (sim) return { sim: sim };
            lastErr = "Test.spawnSimulatedPlayer 返回 null";
        } catch (e) {
            lastErr = `GameTest 生成失败: ${e}`;
        }
    }

    // 路 3：都没有。
    return { err: lastErr, needTest: true };
}

/** 回一个带请求号的同步结果。 */
function result(id, ok, err) {
    reply("bot:result", { i: id, ok: ok, e: err || "" });
}

/** 把一个可能抛异常的动作包起来，统一成 result 语义。 */
function guard(id, fn) {
    try {
        fn();
    } catch (e) {
        result(id, false, String(e));
    }
}

/**
 * 指令处理器外面那层"绝不往外抛"的壳。
 *
 * 为什么必须有
 * ------------
 * `scriptEventReceive.subscribe(cb)` 里的 cb 是**游戏主线程**直接调的。
 * cb 里任何一个没被 catch 的异常，都会顺着引擎的事件派发路径冒上去。
 * 行为包的 JS 运行时和服务器是同一个进程，这不是"某段脚本挂了"，
 * 是**可能把整个服务器带下去**——和 Python 侧 `_guard` 存在的理由
 * 是同一件事，只是被忽略的是另一半。
 *
 * 为什么不能只靠 `guard(id, ...)`
 * ------------------------------
 * `guard` 只包住"拿到 sim 之后、真正干活的那一段"。它**盖不住**：
 *   · `sims.get()` 之前/之后的取字段（`req.n` 本身就不会抛，但
 *     `resolveGameMode(req.gm)` 这类会）
 *   · 处理器里"守卫判断"和"guard 调用"之间的那几行
 *   · `reply()` 自己抛异常（`runCommand` 在引擎忙时会抛）
 *   · 最后那个没写 `guard` 的 `doSpawn`
 * 也就是说，一个 handler 里调了 `guard` 只说明"主干被盖住了"，
 * 不说明"这条指令绝不会炸"。在这里统一再兜一次，才是真的盖住。
 */
function safely(fn) {
    try {
        fn();
    } catch (e) {
        console.warn(`[ArcFakePlayer] 指令处理异常（已兜住）: ${e}`);
    }
}

function vec(x, y, z) {
    return { x: Math.floor(x), y: Math.floor(y), z: Math.floor(z) };
}

/**
 * 这个行为包到底能不能造假人，以及靠哪条路。
 *
 * Python 侧靠它决定要不要去促成 GameTest —— 不知道能力就瞎跑命令，
 * 只会往控制台里灌一堆没用的 /gametest 报错。
 */
function caps() {
    const moduleLevel = typeof gametest.spawnSimulatedPlayer === "function";
    return {
        direct: moduleLevel,          // 模块级函数可用 = 随时能造，无需测试
        test: activeTest !== null,    // GameTest 实例已就位
        fallback: usedTestFallback,   // 是否已经退化到走 GameTest
    };
}

// ---------------------------------------------------------------------------
// GameTest 注册：只在模块级函数不可用时才需要它的实例
// ---------------------------------------------------------------------------

gametest.register("arc_fake_player", "sim_spawner", (test) => {
    activeTest = test;
    usedTestFallback = true;
    while (pendingSpawns.length > 0) {
        doSpawn(pendingSpawns.shift());
    }
})
    .structureName("arc_fake_player:empty")
    .maxTicks(0x7fffffff)
    .tag(gametest.Tags.suiteDefault);

// ---------------------------------------------------------------------------
// 生成 / 移除
// ---------------------------------------------------------------------------

/**
 * 解析生成时该用哪个游戏模式。
 *
 * 历史坑：这里原先写的是 `req.gm !== undefined ? req.gm : 1`，
 * 也就是**缺省硬编码 1 = 创造模式**。后果是 Python 侧不传 gm 时，
 * 假人永远是创造模式（无敌、瞬间破坏）——用户实测的
 * "我创造模式打那个假人，假人不扣血" 就是这么来的。
 *
 * 现在缺省改成"跟随世界默认模式"，这样 Python 传 `keep`
 * （不发 gm 字段）时真的就是"不干预"。
 *
 * 数字约定（Bedrock 原生）：
 *   0=survival  1=creative  2=adventure  3=spectator
 */
function resolveGameMode(gm) {
    // Script API 的 GameMode 是字符串枚举，不能把命令使用的整数直接传入。
    const modes = ["Survival", "Creative", "Adventure", "Spectator"];
    if (gm !== undefined && gm !== null && gm !== "") {
        const named = modes.find((mode) => mode.toLowerCase() === String(gm).toLowerCase());
        if (named) return named;
        const n = Number(gm);
        if (Number.isInteger(n) && n >= 0 && n < modes.length) return modes[n];
    }
    return typeof world.getDefaultGameMode === "function" ? world.getDefaultGameMode() : modes[0];
}

/**
 * 给假人挂上名字牌，方便服主在地图上认出来。
 *
 * 放在 JS 侧做，而不是 Python 侧写 `player.name_tag = ...`：
 * 假人是这个行为包造的，它的外观就该由造它的这一侧负责。
 * Python 那边只管"要什么"，不管"怎么呈现"。
 */
function tagName(sim, name, stealth) {
    // stealth（自动在线的"群演"）不挂 [AI] 标识——它们的工作就是
    // 看起来像普通玩家。名字牌本身就是玩家名，不需要额外装饰。
    if (stealth) return;
    try {
        sim.nameTag = `§b${name}§r §7[AI]`;
    } catch (e) {
        // 名字牌失败纯属外观问题，绝不该影响假人能不能用
        console.warn(`[ArcFakePlayer] 设置名字牌失败: ${e}`);
    }
}

function doSpawn(req) {
    const name = req.n;

    if (sims.has(name)) {
        // 幂等：Endstone 侧的自愈任务会重复下发，这里当成成功，不要报错
        reply("bot:spawned", { n: name, ok: true, existed: true, i: req.i });
        return;
    }

    const loc = { x: Math.floor(req.x), y: Math.floor(req.y), z: Math.floor(req.z) };
    const out = ensureSim(loc, name, resolveGameMode(req.gm));
    if (!out.sim) {
        // 三条路都不通。不要再默默塞进 pendingSpawns 里等一个永远不来的
        // GameTest —— 那样 Python 侧只会看到"请求发出去了却没人回"。
        // 直接把失败和原因回上去，让它决定是报错还是去跑 /gametest。
        reply("bot:spawned", {
            n: name, ok: false, e: out.err || "无法创建假人",
            need_test: !!out.needTest, i: req.i,
        });
        return;
    }

    tagName(out.sim, name, !!req.st);
    sims.set(name, out.sim);
    initArenaCombat(sims);
    // 记下出生点，并算出它该在哪儿重生。
    //
    // Python 侧会把它知道的**服务器配置出生点**一起带过来（rx/ry/rz）——
    // 那是弧光核心 /spawn 用的那个实坐标，Y 是配死的，站在上面就是地面。
    // 引擎的 setSpawnPoint 对 SimulatedPlayer 两种写法都不认，所以这个点
    // 由我们自己用：重生之后把假人传送回去（见 teleportAfterRespawn）。
    const anchor = (req.rx !== undefined && req.ry !== undefined && req.rz !== undefined)
        ? { x: req.rx, y: req.ry, z: req.rz }
        : null;
    rememberSpawnPoint(name, out.sim, loc, anchor);
    reply("bot:spawned", { n: name, ok: true, existed: false, i: req.i });
}

function doRemove(name, id) {
    cancelInteraction(name, "假人已移除");
    const sim = sims.get(name);
    if (!sim) {
        result(id, true, "");   // 本来就没有 = 移除成功
        return;
    }
    try {
        sim.disconnect();
    } catch (e) {
        // disconnect 会抛异常是常态（玩家已经不在），忽略
    }
    sims.delete(name);
    forgetBot(name);
    const w = walking.get(name);
    if (w) {
        walking.delete(name);
        w.settled = true;
        system.clearRun(w.timer);
        result(w.id, false, "假人已移除");
    }
    reply("bot:removed", { n: name, i: id });

    // 把该假人身上挂着的所有挖掘任务都标记为失败，
    // 否则 Python 那边会一直等一个永远不会来的回调。
    // ⚠️ 注意 key 是 `${name}|x,y,z`：旧版这里写过 `digging.delete(name)`，
    //    那个 key 永远匹配不上，是条死代码，已删除。
    for (const [key, ctx] of Array.from(digging.entries())) {
        if (key.startsWith(name + "|")) {
            digging.delete(key);
            digReplyAll(ctx.ids, name, ctx.loc, false, "假人已移除");
        }
    }
    busyUntil.delete(name);
    result(id, true, "");
}

// ---------------------------------------------------------------------------
// 移动：优先交给原版寻路，那比我们自己的 A* 更懂爬楼梯和绕树
// ---------------------------------------------------------------------------

function doWalk(req, id) {
    const name = req.n;
    const sim = sims.get(name);
    if (!sim) {
        result(id, false, "假人不存在");
        return;
    }
    try {
        if (!sim.isValid) {
            result(id, false, "假人已失效");
            return;
        }
        const target = vec(req.x, req.y, req.z);

        // 旧轮询还挂着就直接结掉（Python 侧保证串行，这里只做防御）。
        const prev = walking.get(name);
        if (prev) settleWalk(name, prev, false, "被新的寻路取代");

        // ⚠️ 官方签名（@minecraft/server-gametest 的 SimulatedPlayer，见
        // learn.microsoft.com 的 scriptapi 文档）：
        //     navigateToLocation(location: Vector3, speed?: number): NavigationResult
        //   · **同步**方法，立即返回 NavigationResult —— 不是 Promise，
        //     也没有 timeout 参数（曾经画蛇添足传了第 3 个参数）。
        //   · speed 取值 [0, 1]，默认 1。
        //   · 文档另注明：假人必须接触地面才会开始导航；卡住时会自动停。
        // NavigationResult 只有 isFullPath（是否找到**完整路径**，是寻路
        // 规划结果，不是"已经走到了"）和 getPath()。没有"导航完成"事件，
        // 所以到达与否只能轮询位置来判断。
        let nav;
        try {
            nav = sim.navigateToLocation(target, 1.0);
        } catch (e) {
            result(id, false, `navigateToLocation: ${e}`);
            return;
        }
        if (!nav || nav.isFullPath !== true) {
            // 寻路规划阶段就找不到完整路径（典型：目标是悬空格——
            // 站在空气里的格子假人到不了）。明确失败，让上层记账。
            result(id, false, "没有可达路径");
            return;
        }

        // 找到了路，假人开始走。轮询位置：到达目标附近 → 成功；
        // 超时（req.timeout，单位 tick）→ 失败。必须最终回一次 result，
        // 否则 Python 侧的 `_walk` 永远挂住。
        const timeoutTicks = Math.max(20, Number(req.timeout) || 200);
        const state = { id: id, timer: 0, settled: false, ticks: 0 };
        state.timer = system.runInterval(() => {
            let finish = null;
            try {
                if (!sim.isValid) {
                    finish = [false, "假人已失效"];
                } else {
                    const loc = sim.location;
                    // 判定用格中心（target 是整数格角，假人站在格中时
                    // location ≈ 格中心）。阈值 2.0 比给 Python 侧的
                    // 2.5 略严 —— 万一停在临界距离，由 Python 判成功。
                    const d = Math.hypot(
                        loc.x - (target.x + 0.5),
                        loc.y - target.y,
                        loc.z - (target.z + 0.5));
                    if (d <= 2.0) finish = [true, ""];
                }
            } catch (e) {
                finish = [false, String(e)];
            }
            if (finish === null && ++state.ticks >= timeoutTicks) {
                finish = [false, "寻路超时"];
            }
            if (finish !== null) settleWalk(name, state, finish[0], finish[1]);
        }, 1);
        walking.set(name, state);
    } catch (e) {
        result(id, false, String(e));
    }
}

function doTeleport(req, id) {
    const sim = sims.get(req.n);
    if (!sim) {
        result(id, false, "假人不存在");
        return;
    }
    guard(id, () => {
        const dim = world.getDimension(req.d || "overworld");
        sim.teleport({ x: req.x, y: req.y, z: req.z }, { dimension: dim });
        result(id, true, "");
    });
}

/**
 * 让假人跳一下。
 *
 * 为什么要走这条指令而不是从 Python 侧"把假人传送到它头上"：
 * 传送会绕开碰撞检测，假人能直接穿天花板。引擎自己的跳跃
 * （`jump()`）会正常计算头顶有没有方块。
 */
/**
 * 让假人吃/喝一样东西。
 *
 * 为什么要走这条指令，而不是从 Python 侧直接 `actor.health += 4`：
 * 那样客户端看不到任何动作，而且绕过了饥饿度——假人可以永远不吃饭
 * 却一直满血。真吃一口，血量由引擎自己算。
 */
function doConsume(req, id) {
    const sim = sims.get(req.n);
    if (!sim) {
        result(id, false, "假人不存在");
        return;
    }
    guard(id, () => {
        if (interactions.has(req.n)) { result(id, false, "假人正在交互"); return; }
        const cont = playerInventory(sim);
        const slot = req.item ? findSlot(cont, normId(req.item)) : sim.selectedSlotIndex;
        const item = slot >= 0 ? cont.getItem(slot) : undefined;
        if (!item) { result(id, false, `背包里没有 ${req.item || "手持物品"}`); return; }
        const before = countInContainer(cont, item.typeId, false);
        // useItem(ItemStack) 不消耗背包；进食必须使用真实背包槽位。
        const ok = sim.useItemInSlot(slot);
        if (ok === false) { result(id, false, "无法使用物品（可能处于冷却）"); return; }
        const state = { id, timer: 0, settled: false, ticks: 0 };
        interactions.set(req.n, state);
        state.timer = system.runInterval(() => {
            try {
                if (!sim.isValid) throw new Error("假人已失效");
                if (countInContainer(cont, item.typeId, false) < before) {
                    try { sim.stopUsingItem(); } catch (e) { /* 已结束 */ }
                    finishInteraction(req.n, state, true, "");
                } else if (++state.ticks >= 100) {
                    try { sim.stopUsingItem(); } catch (e) { /* 已结束 */ }
                    finishInteraction(req.n, state, false, "物品未被消耗（可能已吃饱或物品不能食用）");
                }
            } catch (e) { finishInteraction(req.n, state, false, String(e)); }
        }, 1);
    });
}

function doJump(req, id) {
    const sim = sims.get(req.n);
    if (!sim) {
        result(id, false, "假人不存在");
        return;
    }
    guard(id, () => {
        const ok = sim.jump();
        result(id, ok !== false, ok === false ? "当前不能跳跃" : "");
    });
}

/**
 * 丢物品：把背包里的物品真的丢到地上（交易的基础动作）。
 *
 *   bot:drop {n, item, count?, to?}
 *     item  物品代码名（别名规则与 select/container 一致）
 *     count 数量，默认 1
 *     to    目标玩家名。给了且在 8 格内 → 丢在两者中点（看起来像
 *           递过去）；太远或没这人 → 丢在自己脚下并说明。
 *
 * 物品从假人背包**真实扣除**（跨槽位凑数，保留附魔/耐久），用
 * `dimension.spawnItem` 落地。玩家走上前捡起来就是他的了——和真人
 * 交易完全同一条物理路径，没有"凭空给"。
 */
function doDrop(req, id) {
    const sim = sims.get(req.n);
    if (!sim) {
        result(id, false, "假人不存在");
        return;
    }
    guard(id, () => {
        const want = String(req.item || "");
        if (!want) {
            result(id, false, "缺少 item 参数");
            return;
        }
        const count = Math.max(1, Math.min(2304, Number(req.count) || 1));
        const cont = playerInventory(sim);
        const loc = sim.location;
        const dim = sim.dimension;

        let dropLoc = { x: loc.x, y: loc.y + 0.8, z: loc.z };
        let note = "";
        const toName = String(req.to || "");
        if (toName) {
            const target = world.getAllPlayers().find((p) => p && p.name === toName);
            if (!target) {
                note = `（没找到玩家 ${toName}，丢在自己脚下）`;
            } else {
                const t = target.location;
                const dist = Math.hypot(t.x - loc.x, t.y - loc.y, t.z - loc.z);
                if (dist > 8) {
                    note = `（${toName} 距离 ${dist.toFixed(1)} 格太远，丢在自己脚下）`;
                } else {
                    // 中点落地：既不出现在假人口袋里，也不凭空飞到玩家跟前
                    dropLoc = {
                        x: (loc.x + t.x) / 2,
                        y: Math.max(loc.y, t.y) + 0.5,
                        z: (loc.z + t.z) / 2,
                    };
                }
            }
        }

        let remaining = count;
        let dropped = 0;
        for (let slot = 0; slot < cont.size && remaining > 0; slot++) {
            const item = cont.getItem(slot);
            if (!item || !itemMatches(item.typeId, want)) continue;
            const take = Math.min(item.amount, remaining);
            const give = item.clone();
            give.amount = take;
            const spawned = dim.spawnItem(give, dropLoc);
            if (!spawned) continue;
            if (item.amount > take) {
                item.amount -= take;
                cont.setItem(slot, item);
            } else {
                cont.setItem(slot, undefined);
            }
            dropped += take;
            remaining -= take;
        }
        if (dropped === 0) {
            result(id, false, `背包里没有 ${want}`);
            return;
        }
        result(id, true, dropped < count
            ? `只丢出 ${dropped}/${count} 个${note}`
            : (note || ""));
    });
}


function doStop(req, id) {
    const sim = sims.get(req.n);
    if (!sim) {
        result(id, false, "假人不存在");
        return;
    }
    guard(id, () => {
        try { sim.stopMoving(); } catch (e) { /* 没在动就算了 */ }
        try { sim.stopBreakingBlock(); } catch (e) { /* 没在挖就算了 */ }
        try { sim.stopUsingItem(); } catch (e) { /* 没在使用 */ }
        try { sim.stopInteracting(); } catch (e) { /* 没在交互 */ }
        cancelInteraction(req.n, "被 stop 打断；熔炉内物品保留在熔炉中");
        // 停掉挂在它身上的寻路轮询（迟到的 result 会被 Python 忽略：
        // cancel 已经把 `_walk` 清了，回调里的 `task is not self._walk`
        // 判定会挡住它）
        const w = walking.get(req.n);
        if (w) settleWalk(req.n, w, false, "被 stop 打断");
        // 停掉所有挂在它身上的挖掘轮询
        for (const [key, ctx] of Array.from(digging.entries())) {
            if (key.startsWith(req.n + "|")) {
                endDigging(key, ctx, false, "被 stop 打断");
            }
        }
        result(id, true, "");
    });
}

// ---------------------------------------------------------------------------
// 挖掘：只挖「准星真正打到的那一格」
// ---------------------------------------------------------------------------
//
// 整段只有一条铁律：**先让射线打到目标，才允许 breakBlock**。
//
// 因为 `breakBlock(loc)` 不校验视线、不校验距离、不校验遮挡（见文件头
// "关于真挖掘"）。谁把这个坐标递到它手上，谁就得自己保证那个坐标是
// "看得见、够得着、没被挡"的。Python 递过来的坐标只是"**想**挖哪儿"，
// 不是"**能**挖哪儿"——两者之差正好就是"隔墙挖方块"这个 bug。
// 射线检测是补上这个差的唯一手段，没有别的办法。
//
// 参考实现（两个都是跑得通的成熟假人包，做法完全一致）：
//   • Understudy  scripts/classes/RepeatableAction.js#break()
//       const lookingAtLocation =
//           sim.getBlockFromViewDirection({ maxDistance: 6 })?.block?.location;
//       if (lookingAtLocation === void 0) return;
//       sim.breakBlock(lookingAtLocation);
//   • FlashFakePlayerPack  tscripts/xTerrain/plugins/breakBlock.ts
//       system.runInterval(() => {
//           const block = man.getBlockFromViewDirection({ maxDistance: 6 })?.block;
//           if (!block) return;
//           if (block.isValid && !block.isLiquid && !block.isAir)
//               man.breakBlock(block_location);
//       }, 0);                        // ← 每 tick 一次
// 两者都是"射线取格 + 每 tick 敲"，没有例外。

/**
 * 让假人面朝某个方块。`lookAtBlock` 是**异步生效**的：这一 tick 发出的
 * 转向要下一 tick 才真正落地，所以调用方必须留出缓冲再去看射线结果。
 *
 * ⚠️ 瞄的是方块**中心**（+0.5），不是它的整数角点。
 *    `lookAtBlock(loc)` 是"把视线转向这个点"。若传方块的整数坐标
 *    （如 `5,64,0`），视线正好指向 8 个格子共享的那个**角**，射线贴着
 *    格子边界走，`getBlockFromViewDirection` 返回的可能是相邻那一格 ——
 *    "视线闸门"就会判成"没瞄上目标"，反复重瞄也收敛不了，表现出来就是
 *    **"它明明盯着方块却挖不动"**。给中心点，射线从近面射进目标格，
 *    返回的必然是目标本身。
 *
 * 用默认时长（等价 Instant）而**不是** `LookDuration.Continuous`。
 * Continuous 是"持续看向，直到你让它看别处"，是一条**常驻转向行为**，
 * 会和挖掘抢行为槽、反复打断挖掘——旧版"一直点击方块、进度反复归零"
 * 就是这么来的，别再改回去。
 *
 * 那"中途真被推开/转走了"怎么办？交给 `pumpOneDigging` 的射线检测：连着
 * 几拍打不到才重瞄（`AIM_LOST_TICKS`），不丢就绝不去碰它。
 */
function faceBlock(sim, loc) {
    // 方块中心，而不是整数角点——理由见上面的 ⚠️。
    const center = { x: loc.x + 0.5, y: loc.y + 0.5, z: loc.z + 0.5 };
    try {
        sim.lookAtBlock(center);
    } catch (e) {
        try {
            sim.lookAtBlock(center, undefined);
        } catch (e2) {
            // lookAtBlock 不存在/不可用时忽略，交给调用方兜
        }
    }
}

/**
 * 从假人头部发一条射线，看第一个被打到的是哪一格（**只读**）。
 *
 * 为什么用射线，而不是"自己读朝向角 + 算包围盒 + 算遮挡"：射线一次把这三
 * 件事全办了，而且它用的就是引擎判定"这一锤子会砸在谁身上"的同一套逻辑，
 * 不会出现"我以为瞄着、引擎认为没瞄着"的偏差。
 *
 * ⚠️ 异常一律归为"打不到"（fail-closed）。整个"不许穿墙挖"的约束就建立
 *    在这条射线上；射线不可用时若放行，闸门就等于不存在。
 */
function raycastBlock(sim) {
    try {
        const hit = sim.getBlockFromViewDirection({ maxDistance: DIG_RAY_DISTANCE });
        const block = hit ? hit.block : undefined;
        const loc = block ? block.location : undefined;
        if (!loc) return { block: undefined, loc: undefined };
        return { block: block, loc: { x: loc.x, y: loc.y, z: loc.z } };
    } catch (e) {
        return { block: undefined, loc: undefined };
    }
}

/** 两个方块坐标是不是同一格。 */
function sameBlock(a, b) {
    return !!a && !!b && a.x === b.x && a.y === b.y && a.z === b.z;
}

/** 这个方块现在还是不是实体。用来判断挖完没有。 */
function isSolidAt(dim, p) {
    try {
        const block = dim.getBlock(p);
        if (!block) return true; // 未加载不等于空气，不能回报已经挖完。
        const id = block.typeId || "";
        return !(id.endsWith(":air") || id.endsWith(":cave_air") || id.endsWith(":void_air"));
    } catch (e) {
        // 区块没加载会抛异常。当成"还不是空气"，让轮询继续等
        return true;
    }
}

/**
 * 给一次挖掘的**所有**请求号回 `bot:dug`。
 *
 * 为什么是"所有"：同一个方块可能被重复要求挖（Python 侧丢包重发、
 * 超时重试、旧版本每 tick 重发都会触发）。这些重复请求不再被静默丢弃，
 * 而是把请求号挂进 `ctx.ids`，最后一起回复——**任何一条发出的请求都
 * 必须有回复**，否则 Python 侧会一直挂到 150 秒超时。
 */
function digReplyAll(ids, name, loc, ok, err, extra) {
    for (const rid of ids) {
        reply("bot:dug", {
            i: rid, n: name, ok: ok, e: err || "", ...loc, ...(extra || {}),
        });
    }
}

function doDig(req, id) {
    const name = req.n;
    const sim = sims.get(name);
    if (!sim) {
        reply("bot:dug", { i: id, n: name, ok: false, e: "假人不存在", x: req.x, y: req.y, z: req.z });
        return;
    }

    const loc = vec(req.x, req.y, req.z);
    const dim = sim.dimension;
    const key = `${name}|${loc.x},${loc.y},${loc.z}`;

    if (!isSolidAt(dim, loc)) {
        // 已经是空气了，不算失败——可能别的动作刚好把它挖了
        reply("bot:dug", { i: id, n: name, ok: true, already: true, ...loc });
        return;
    }

    // 开挖前把最合适的工具换到手上（镐对石头/矿、斧对木头、锹对土）
    try { autoToolFor(sim, dim, loc); } catch (_) { /* 换不了就照旧 */ }

    // 同一方块已经在挖 → 把请求号挂到这次挖掘上，等它结算时一起回复。
    // **绝不静默丢弃**：旧版在这里裸 `return`，那条 `bot:dig` 一条回复都
    // 收不到，Python 侧一直挂到 150 秒超时（"挖着挖着突然不动了"）。
    // 也**绝不重复登记**：两块流程同时收尾同一个方块只会互相踩。
    const running = digging.get(key);
    if (running) {
        if (!running.ids.includes(id)) running.ids.push(id);
        return;
    }

    // 每个身体只能持续破坏一个方块。新目标必须明确结束旧目标。
    for (const [otherKey, ctx] of Array.from(digging.entries())) {
        if (ctx.n === name) endDigging(otherKey, ctx, false, "被新的挖掘目标取代");
    }

    // 先选工具。没有指定就用当前手持，让原版自己判断能不能挖
    if (req.tool) {
        try {
            const inv = sim.getComponent("minecraft:inventory");
            const container = inv ? inv.container : null;
            if (container) {
                for (let slot = 0; slot < container.size; slot++) {
                    const item = container.getItem(slot);
                    if (item && item.typeId.endsWith(":" + req.tool.replace(/^minecraft:/, ""))) {
                        try { selectInventorySlot(sim, slot); } catch (e) { /* 选中失败不影响挖掘 */ }
                        break;
                    }
                }
            }
        } catch (e) {
            // 背包不可访问：用空手挖，慢一点但照样能挖
        }
    }

    // 登记任务，并立刻把头转过去。真正动手在 `pumpOneDigging` 里——每 tick
    // 一次，而且**必须先让射线打到目标**才会敲。
    // 注意第一两拍通常还在转头的路上（lookAtBlock 异步生效），miss 属于
    // 正常现象，由 `AIM_LOST_TICKS` 的连续计数吸收掉，不会误判成遮挡。
    digging.set(key, {
        ids: [id], n: name, loc: loc, dim: dim,
        ticks: 0, miss: 0, aimRetries: 0,
    });
    busyUntil.set(name, Date.now() + 30000);
    faceBlock(sim, loc);
}

/**
 * 轮询所有进行中的挖掘。每 tick 跑。
 *
 * 为什么用轮询而不是监听 blockBreak 事件：事件要按维度+坐标过滤，
 * 而这里已经知道确切坐标了，直接查方块状态更简单也更准。
 *
 * ⚠️ 这个函数是 `runInterval` 的**裸函数引用**（不是箭头包一层），
 *    所以函数体里的 `return` / 异常都会直接作用在周期任务本身。
 *    它每 tick 都跑，是全局最热的回调——一旦有一次抛出去，
 *    整个挖掘轮询可能就此停摆（挖完了也永远不回 `bot:dug`），
 *    Python 侧只能一直等那个永远不来的回调。
 *
 *    为此这里做了三层：
 *      1. 最外层 try —— 保证本函数**绝不往外抛**
 *      2. 每项一个 try —— 单个 ctx 坏了只跳过它，后面照跑（见下）
 *      3. `isSolidAt` / `raycastBlock` / `breakBlock` 内部各自的 try
 */
function pumpDigging() {
    try {
        pumpDiggingInner();
    } catch (e) {
        console.warn(`[ArcFakePlayer] 挖掘轮询异常（已兜住，下 tick 继续）: ${e}`);
    }
}

function pumpDiggingInner() {
    if (digging.size === 0) return;

    for (const [key, ctx] of Array.from(digging.entries())) {
        // 逐项兜底：一个坏掉的记录不该拖垮同一 tick 里其他挖掘。
        // 抛了就把它当"这次挖掘失败"清出去，免得每 tick 都再抛一次
        // （那种情况会变成"日志刷屏 + 轮询卡在这一项上"）。
        try {
            pumpOneDigging(key, ctx);
        } catch (e) {
            // 抛了就把它当"这次挖掘失败"清出去，免得每 tick 都再抛一次
            // （那种情况会变成"日志刷屏 + 轮询卡在这一项上"）。
            endDigging(key, ctx, false, `挖掘轮询异常: ${e}`);
        }
    }
}

/**
 * 结束一次挖掘：把结论回给**所有**挂在这上面的请求号，并让假人收手。
 */
function endDigging(key, ctx, ok, err, extra) {
    digging.delete(key);
    // 只有"这个假人身上已经没有别的挖掘了"才 stopBreakingBlock，
    // 否则会把同一假人另一块的敲击一起停掉。
    if (!Array.from(digging.keys()).some((k) => k.startsWith(ctx.n + "|"))) {
        busyUntil.delete(ctx.n);
        const sim = sims.get(ctx.n);
        if (sim && sim.isValid) {
            try { sim.stopBreakingBlock(); } catch (e) { /* 没在挖就算了 */ }
        }
    }
    digReplyAll(ctx.ids, ctx.n, ctx.loc, ok, err, extra);
}

/**
 * 瞄不上目标时，给一句**能指导下一步**的失败原因。
 *
 * 把挡住视线的那一格连同类型一起报上去：AI 看到
 * `目标被遮挡：准星打在 minecraft:stone @ 12,64,7` 才知道下一步要么先挖掉
 * 挡路的、要么换个位置站。只说一句"挖掘失败"，它只能原地重试。
 */
function describeOcclusion(hit) {
    if (!hit || !hit.block || !hit.loc) {
        return `目标不在视线内（${DIG_RAY_DISTANCE} 格内没打到方块，可能已走开或没瞄准）`;
    }
    let id = "未知方块";
    try { id = hit.block.typeId || id; } catch (e) { /* 读不到就算了 */ }
    return `目标被遮挡：准星打在 ${id} @ ${hit.loc.x},${hit.loc.y},${hit.loc.z}`;
}

/**
 * 推进单项挖掘。每 tick 一次。
 *
 * 顺序不能变：先判"挖完没有"，再判"够不够得着"，最后才判"看不看得见"。
 * 视线闸门放最后，是为了让"刚好在最后一拍挖掉"的情况直接以成功结束，
 * 而不是先被射线判成遮挡。
 */
function pumpOneDigging(key, ctx) {
    ctx.ticks++;

    const sim = sims.get(ctx.n);
    if (!sim || !sim.isValid) {
        endDigging(key, ctx, false, "假人已失效");
        return;
    }
    if (sim.dimension.id !== ctx.dim.id) {
        endDigging(key, ctx, false, "假人已切换维度");
        return;
    }

    // ① 方块没了 → 成功。swing = 真花了多少 tick，让 Python 知道这是真挖了
    //    一会儿，不是瞬移消除。
    let stillSolid;
    try {
        stillSolid = isSolidAt(ctx.dim, ctx.loc);
    } catch (e) {
        stillSolid = true;
    }

    if (!stillSolid) {
        endDigging(key, ctx, true, "", { swing: ctx.ticks });
        return;
    }

    // ② 够得着吗（脚下量到方块中心）。
    //    射线闸门管"看得见"，这一条管"够得着"。少了它，站在二十格外只要
    //    中间没阻挡，一样能把方块隔空敲掉。
    let dist = null;
    try {
        const p = sim.location;
        dist = Math.hypot(
            p.x - (ctx.loc.x + 0.5),
            p.y - (ctx.loc.y + 0.5),
            p.z - (ctx.loc.z + 0.5));
    } catch (e) {
        // 读不到位置就跳过这轮检查
    }
    if (dist !== null && dist > DIG_MAX_DISTANCE) {
        endDigging(key, ctx, false, `假人够不着（距目标 ${dist.toFixed(1)} 格）`);
        return;
    }

    // ③ 视线闸门（默认关闭，见 `DIG_STRICT_SIGHT` 的注释）。
    //    开着的时候：射线打不到目标那一格就**绝不** breakBlock，宁可如实
    //    失败也不穿墙；头几拍还在转头的路上（lookAtBlock 异步生效）属于
    //    正常 miss，靠"连着几拍"的计数吸收掉。
    //
    //    默认关着：这一步整段跳过，直接进 ④ 开敲。
    if (DIG_STRICT_SIGHT) {
        const hit = raycastBlock(sim);
        if (!sameBlock(hit.loc, ctx.loc)) {
            ctx.miss++;
            if (ctx.miss >= AIM_LOST_TICKS) {
                ctx.miss = 0;
                if (ctx.aimRetries < MAX_AIM_RETRIES) {
                    // 再转一次头，多等几拍。转头异步生效，接下来那几拍的
                    // miss 仍然算正常，不用急着下结论。
                    ctx.aimRetries++;
                    faceBlock(sim, ctx.loc);
                    return;
                }
                endDigging(key, ctx, false, describeOcclusion(hit));
                return;
            }
            return;
        }
        ctx.miss = 0;
        ctx.aimRetries = 0;
    }

    // ④ 瞄到了 → 敲。**每 tick 都敲**，这是"持续挖掘"的关键。
    //
    //    旧版只在发起时敲一次、之后干等轮询。`breakBlock` 的"敲到碎为止"
    //    一旦中途被引擎静默打断（被推挤、区块边界、状态抖动），旧版就只能
    //    干等到判超时再整个重启一轮——表现出来就是"一顿一顿地挖"。
    //    每 tick 补一次，等于把中断随时接上，挖掘是连续推进的。
    //
    //    重复调用是**安全**的：语义是"继续敲这一格"，不是"重新开始"，不会
    //    把破坏进度清零。Understudy（continuous 模式）和 FlashFakePlayerPack
    //    （runInterval 0）都是每 tick 调一次，是跑得通的成熟做法。
    ctx.miss = 0;
    ctx.aimRetries = 0;
    try {
        if (sim.breakBlock(ctx.loc) === false) {
            // 方块可能刚被其它玩家破坏，下一拍按实际世界状态结算。
            if (!isSolidAt(ctx.dim, ctx.loc)) return;
            endDigging(key, ctx, false, "无法破坏目标方块（液体、游戏模式或保护限制）");
            return;
        }
    } catch (e) {
        endDigging(key, ctx, false, `breakBlock 失败: ${e}`);
        return;
    }

    // ⑤ 兜底：真挖不动（基岩 / 被保护 / 方块在射线边缘反复闪烁）时，
    //    别无限敲下去，如实回报。
    if (ctx.ticks >= DIG_LIMIT_TICKS) {
        endDigging(key, ctx, false,
            `挖掘超时（${Math.round(DIG_LIMIT_TICKS / 20)} 秒仍未破坏，方块可能被保护）`);
    }
}

// ---------------------------------------------------------------------------
// 放置方块
// ---------------------------------------------------------------------------

function doPlace(req, id) {
    const sim = sims.get(req.n);
    if (!sim) { result(id, false, "假人不存在"); return; }
    guard(id, () => {
        if (interactions.has(req.n)) throw new Error("假人正在交互");
        const loc = vec(req.x, req.y, req.z);
        checkReach(sim, loc);
        const dim = sim.dimension;
        const target = dim.getBlock(loc);
        if (!target) throw new Error("目标区块未加载");
        if (!target.isAir && !target.isLiquid && !String(target.typeId).endsWith(":air")) {
            throw new Error("目标位置已被占用");
        }
        const cont = playerInventory(sim);
        const slot = findSlot(cont, normId(req.block));
        if (slot < 0) throw new Error("背包里没有 " + req.block);
        const item = cont.getItem(slot);
        // 请求坐标是新方块的位置；引擎参数是被点击的已有方块及其表面。
        const faces = { Up: [0, 1, 0], Down: [0, -1, 0], North: [0, 0, -1],
            South: [0, 0, 1], East: [1, 0, 0], West: [-1, 0, 0] };
        const preferred = Object.keys(faces).find((face) => face.toLowerCase() === String(req.face || "Up").toLowerCase());
        if (!preferred) throw new Error("无效的放置面");
        let support = null;
        let direction = null;
        for (const face of [preferred, ...Object.keys(faces).filter((face) => face !== preferred)]) {
            const offset = faces[face];
            const point = { x: loc.x - offset[0], y: loc.y - offset[1], z: loc.z - offset[2] };
            const block = dim.getBlock(point);
            if (block && !block.isAir && !block.isLiquid && !String(block.typeId).endsWith(":air")) {
                support = point;
                direction = face;
                break;
            }
        }
        if (!support) throw new Error("目标周围没有可点击的支撑方块");
        const selected = selectInventorySlot(sim, slot);
        faceBlock(sim, support);
        if (sim.useItemInSlotOnBlock(selected, support, direction) === false) throw new Error("放置失败（目标可能被占用或受到保护）");
        // 点击成功也可能只是打开箱子，必须确认实际产生目标方块。
        const state = { id, timer: 0, settled: false, ticks: 0 };
        interactions.set(req.n, state);
        state.timer = system.runInterval(() => {
            try {
                if (!sim.isValid || sim.dimension.id !== dim.id) throw new Error("假人失效或已切换维度");
                const placed = dim.getBlock(loc);
                if (placed && blockMatchesItem(placed.typeId, item.typeId)) finishInteraction(req.n, state, true, "");
                else if (++state.ticks >= 5) finishInteraction(req.n, state, false, "目标位置没有放出指定方块");
            } catch (e) { finishInteraction(req.n, state, false, String(e)); }
        }, 1);
    });
}

// 耕地与播种：种地的基本功。锄头对着泥土/草地右键 → 变耕地（farmland），
// 种子对着耕地右键 → 长出麦苗（wheat 方块在耕地上一格）。两者都是
// "选物品 → 面向方块 → useItemInSlotOnBlock → 验证结果方块"，与
// doPlace 同构，只是验证目标不同（farmland / wheat）。
const HOE_RE = /^(wooden|stone|copper|iron|golden|diamond|netherite)_hoe$/;

function bestHoeSlot(cont) {
    let best = -1, bestRank = -1;
    for (let slot = 0; slot < cont.size; slot++) {
        const item = cont.getItem(slot);
        if (!item) continue;
        const m = item.typeId.replace("minecraft:", "").match(HOE_RE);
        if (!m) continue;
        const rank = TOOL_RANK[m[1]] ?? -1;
        if (rank > bestRank) { bestRank = rank; best = slot; }
    }
    return best;
}

function doTill(req, id) {
    const sim = sims.get(req.n);
    if (!sim) { result(id, false, "假人不存在"); return; }
    guard(id, () => {
        if (interactions.has(req.n)) throw new Error("假人正在交互");
        const loc = vec(req.x, req.y, req.z);
        checkReach(sim, loc);
        const dim = sim.dimension;
        const block = dim.getBlock(loc);
        if (!block) throw new Error("目标区块未加载");
        if (!["minecraft:dirt", "minecraft:grass_block", "minecraft:dirt_path"].includes(block.typeId))
            throw new Error("目标不是泥土/草地，锄不了");
        const above = dim.getBlock({ x: loc.x, y: loc.y + 1, z: loc.z });
        if (above && above.typeId !== "minecraft:air") throw new Error("方块上面有东西，清掉再耕");
        const cont = playerInventory(sim);
        const slot = bestHoeSlot(cont);
        if (slot < 0) throw new Error("背包里没有锄头（木板+木棍×2 合成）");
        const selected = selectInventorySlot(sim, slot);
        faceBlock(sim, loc);
        if (sim.useItemInSlotOnBlock(selected, loc, "Up") === false)
            throw new Error("耕地失败（可能被保护）");
        const state = { id, timer: 0, settled: false, ticks: 0 };
        interactions.set(req.n, state);
        state.timer = system.runInterval(() => {
            try {
                if (!sim.isValid || sim.dimension.id !== dim.id) throw new Error("假人失效或已切换维度");
                const now = dim.getBlock(loc);
                if (now && String(now.typeId).includes("farmland")) finishInteraction(req.n, state, true, "");
                else if (++state.ticks >= 5) finishInteraction(req.n, state, false, "这格没耕成田");
            } catch (e) { finishInteraction(req.n, state, false, String(e)); }
        }, 1);
    });
}

function doPlant(req, id) {
    const sim = sims.get(req.n);
    if (!sim) { result(id, false, "假人不存在"); return; }
    guard(id, () => {
        if (interactions.has(req.n)) throw new Error("假人正在交互");
        const loc = vec(req.x, req.y, req.z);
        checkReach(sim, loc);
        const dim = sim.dimension;
        const block = dim.getBlock(loc);
        if (!block) throw new Error("目标区块未加载");
        if (String(block.typeId) !== "minecraft:farmland") throw new Error("目标不是耕地，先用锄头耕");
        const above = dim.getBlock({ x: loc.x, y: loc.y + 1, z: loc.z });
        if (!above || above.typeId !== "minecraft:air") throw new Error("耕地上已经有作物了");
        const cont = playerInventory(sim);
        const slot = findSlot(cont, req.item || "wheat_seeds");
        if (slot < 0) throw new Error("背包里没有种子 " + (req.item || "wheat_seeds"));
        const selected = selectInventorySlot(sim, slot);
        faceBlock(sim, loc);
        if (sim.useItemInSlotOnBlock(selected, loc, "Up") === false)
            throw new Error("播种失败");
        const state = { id, timer: 0, settled: false, ticks: 0 };
        interactions.set(req.n, state);
        state.timer = system.runInterval(() => {
            try {
                if (!sim.isValid || sim.dimension.id !== dim.id) throw new Error("假人失效或已切换维度");
                const crop = dim.getBlock({ x: loc.x, y: loc.y + 1, z: loc.z });
                if (crop && String(crop.typeId).includes("wheat")) finishInteraction(req.n, state, true, "");
                else if (++state.ticks >= 5) finishInteraction(req.n, state, false, "种子没种上");
            } catch (e) { finishInteraction(req.n, state, false, String(e)); }
        }, 1);
    });
}

// 库存操作先在 ItemStack 克隆上规划，保留物品附魔、耐久和名称。
function normId(id) {
    const value = String(id || "");
    return value && !value.includes(":") ? "minecraft:" + value : value;
}

function shortMatches(short, want) {
    return short === want || short.endsWith("_" + want) ||
        (want === "coal" && short === "charcoal") ||
        (want === "log" && (short === "wood" || /_(wood|stem|hyphae)$/.test(short)));
}

function itemMatches(actual, wanted) {
    const want = normId(wanted);
    return actual === want || (actual.startsWith("minecraft:") && want.startsWith("minecraft:") &&
        shortMatches(actual.slice(10), want.slice(10)));
}

function blockMatchesItem(actual, wanted) {
    if (actual === wanted) return true;
    const aliases = { "minecraft:reeds": "minecraft:sugar_cane", "minecraft:lit_pumpkin": "minecraft:jack_o_lantern" };
    return aliases[actual] === wanted || aliases[wanted] === actual;
}

function playerInventory(sim) {
    const inv = sim.getComponent("minecraft:inventory");
    if (!inv || !inv.container) throw new Error("背包不可访问");
    return inv.container;
}

// 自动持镐/斧/锹：breakBlock 的挖掘速度和掉落都看手持——没镐挖石头
// 不掉东西、拿着剑半天挖不动。真人都会换工具，这里在开挖前替它把
// 背包里**该类型最好的工具**换到手上（选不中就照旧，不挡挖掘）。
const TOOL_RANK = { wooden: 0, stone: 1, copper: 2, iron: 3, golden: 4, diamond: 5, netherite: 6 };
const TOOL_KIND_RE = /^(wooden|stone|copper|iron|golden|diamond|netherite)_(pickaxe|axe|shovel)$/;

function bestToolSlot(cont, kind) {
    let best = -1, bestRank = -1;
    for (let slot = 0; slot < cont.size; slot++) {
        const item = cont.getItem(slot);
        if (!item) continue;
        const m = item.typeId.replace("minecraft:", "").match(TOOL_KIND_RE);
        if (!m || m[2] !== kind) continue;
        const rank = TOOL_RANK[m[1]] ?? -1;
        if (rank > bestRank) { bestRank = rank; best = slot; }
    }
    return best;
}

function blockToolKind(blockShort) {
    if (/(_ore|stone|cobble|deepslate|granite|diorite|andesite|quartz|basalt|netherrack|blackstone|obsidian|brick|concrete|tuff|calcite|marble|ice$|amethyst|dripstone)/.test(blockShort)) return "pickaxe";
    if (/(_log$|_wood$|planks|bamboo_block|pumpkin|melon|hay_block|crafting_table|chest$|fence|door$)/.test(blockShort)) return "axe";
    if (/(dirt|grass_block|sand$|gravel|clay|snow|mud|farmland|podzol)/.test(blockShort)) return "shovel";
    return null;
}

function autoToolFor(sim, dim, loc) {
    const block = dim.getBlock(loc);
    if (!block) return;
    const kind = blockToolKind(block.typeId.replace("minecraft:", ""));
    if (!kind) return;
    const cont = playerInventory(sim);
    const slot = bestToolSlot(cont, kind);
    if (slot >= 0) selectInventorySlot(sim, slot);
}

function findSlot(cont, typeId) {
    for (let slot = 0; slot < cont.size; slot++) {
        const item = cont.getItem(slot);
        if (item && itemMatches(item.typeId, typeId)) return slot;
    }
    return -1;
}

function selectInventorySlot(sim, slot) {
    const cont = playerInventory(sim);
    if (slot < 0 || slot >= cont.size || !cont.getItem(slot)) throw new Error("背包槽位为空");
    if (slot >= 9) {
        const hotbar = Number.isInteger(sim.selectedSlotIndex) ? sim.selectedSlotIndex : 0;
        cont.swapItems(slot, hotbar, cont);
        slot = hotbar;
    }
    sim.selectedSlotIndex = slot;
    return slot;
}

function countInContainer(cont, typeId, aliases = true) {
    let total = 0;
    for (let slot = 0; slot < cont.size; slot++) {
        const item = cont.getItem(slot);
        if (item && (aliases ? itemMatches(item.typeId, typeId) : item.typeId === typeId)) total += item.amount;
    }
    return total;
}

function countInPlayer(sim, typeId) { return countInContainer(playerInventory(sim), typeId); }

function snapshot(cont) {
    return Array.from({ length: cont.size }, (_, slot) => cont.getItem(slot)?.clone());
}

function commitInventories(changes) {
    const originals = changes.map(([cont]) => snapshot(cont));
    try {
        changes.forEach(([cont, items]) => items.forEach((item, slot) => cont.setItem(slot, item)));
    } catch (error) {
        // 同一个脚本 tick 内没有异步交错，写入失败时恢复原来的槽位。
        changes.forEach(([cont], index) => originals[index].forEach((item, slot) => cont.setItem(slot, item)));
        throw error;
    }
}

function requestedCount(value, fallback = 1) {
    const count = value === undefined ? fallback : Number(value);
    if (!Number.isInteger(count) || count < 1 || count > 2304) throw new Error("数量必须是 1~2304 的整数");
    return count;
}

function reserveItems(items, typeId, count, exact = false) {
    const removed = [];
    for (let slot = 0; slot < items.length && count > 0; slot++) {
        const item = items[slot];
        if (!item || !(exact ? item.typeId === typeId : itemMatches(item.typeId, typeId))) continue;
        const take = Math.min(item.amount, count);
        const piece = item.clone();
        piece.amount = take;
        removed.push(piece);
        if (take === item.amount) items[slot] = undefined;
        else item.amount -= take;
        count -= take;
    }
    if (count > 0) throw new Error("材料数量不足：" + typeId + "（还缺 " + count + "）");
    return removed;
}

function insertItems(items, stacks) {
    for (const stack of stacks) {
        let amount = stack.amount;
        for (const current of items) {
            if (amount <= 0) break;
            if (!current || !current.isStackableWith(stack)) continue;
            const put = Math.min(amount, current.maxAmount - current.amount);
            current.amount += put;
            amount -= put;
        }
        for (let slot = 0; slot < items.length && amount > 0; slot++) {
            if (items[slot]) continue;
            const put = Math.min(amount, stack.maxAmount);
            const piece = stack.clone();
            piece.amount = put;
            items[slot] = piece;
            amount -= put;
        }
        if (amount > 0) throw new Error("目标背包或容器空间不足");
    }
}

function checkReach(sim, loc) {
    const origin = sim.location;
    if (Math.hypot(origin.x - loc.x - 0.5, origin.y - loc.y - 0.5, origin.z - loc.z - 0.5) > 5) {
        throw new Error("距离目标超过 5 格，够不着");
    }
}

function doCraft(sim, req, id) {
    const grid = Number(req.grid) === 2 ? 2 : 3;
    if (grid === 3) {
        const loc = vec(req.x, req.y, req.z);
        checkReach(sim, loc);
        const table = sim.dimension.getBlock(loc);
        if (!table || table.typeId !== "minecraft:crafting_table") throw new Error("目标不是工作台");
    }
    const slots = Array.isArray(req.slots) ? req.slots : [];
    if (slots.length === 0 || !req.result) throw new Error("配方不完整（需要 slots、result、count）");
    const seen = new Set();
    const cont = playerInventory(sim);
    const items = snapshot(cont);
    const materials = [];
    for (const one of slots) {
        const slot = Number(one.slot);
        if (!Number.isInteger(slot) || slot < 0 || slot >= grid * grid || seen.has(slot)) throw new Error("配方槽位无效或重复");
        seen.add(slot);
        materials.push(...reserveItems(items, normId(one.id), 1));
    }
    let output = normId(req.result);
    if (output === "minecraft:planks") {
        const match = materials[0].typeId.match(/^minecraft:(?:stripped_)?(.+)_(?:log|wood|stem|hyphae)$/);
        if (!match) throw new Error("无法确定原木对应的木板种类");
        output = "minecraft:" + match[1] + "_planks";
    }
    const count = requestedCount(req.count);
    const produced = new ItemStack(output, count);
    if (produced.amount !== count) throw new Error("单次合成产出超过物品堆叠上限");
    insertItems(items, [produced]);
    // 工作台与背包合成格都不提供 Script API 容器；按 Python 配方扣料兑换。
    // 提交前验证全部材料和成品空间，任何失败都不会消耗物品。
    commitInventories([[cont, items]]);
    reply("bot:result", { i: id, ok: true, e: "", made: { id: output, count } });
}

function doSmelt(sim, req, id, block, cont) {
    if (!["minecraft:furnace", "minecraft:lit_furnace", "minecraft:blast_furnace", "minecraft:lit_blast_furnace",
        "minecraft:smoker", "minecraft:lit_smoker"].includes(block.typeId)) throw new Error("目标不是熔炉、烟熏炉或高炉");
    if (cont.getItem(0) || cont.getItem(1) || cont.getItem(2)) throw new Error("熔炉非空，请先取出已有物品");
    const count = requestedCount(req.count);
    if (count > 64) throw new Error("单次熔炼最多 64 个物品");
    const inventory = playerInventory(sim);
    const items = snapshot(inventory);
    const first = items.find((item) => item && itemMatches(item.typeId, req.input));
    if (!first) throw new Error("背包里没有 " + req.input);
    const inputId = first.typeId;
    const input = first.clone();
    if (count > input.maxAmount) throw new Error("熔炼材料超过单槽堆叠上限");
    input.amount = count;
    reserveItems(items, inputId, count, true);
    const fuelId = normId(req.fuel || "coal");
    if (!["minecraft:coal", "minecraft:charcoal"].includes(fuelId)) throw new Error("当前熔炼支持煤或木炭燃料");
    const fuelStack = items.find((item) => item && itemMatches(item.typeId, fuelId));
    if (!fuelStack) throw new Error("背包里没有燃料 " + fuelId);
    const fuel = fuelStack.clone();
    const fuelCount = Math.ceil(count / 8);
    reserveItems(items, fuel.typeId, fuelCount, true);
    fuel.amount = fuelCount;
    const furnace = snapshot(cont);
    furnace[0] = input;
    furnace[1] = fuel;
    commitInventories([[inventory, items], [cont, furnace]]);
    const dim = sim.dimension;
    const state = { id, timer: 0, settled: false, ticks: 0, made: 0, outputId: "" };
    interactions.set(req.n, state);
    state.timer = system.runInterval(() => {
        try {
            if (!sim.isValid || sim.dimension.id !== dim.id) throw new Error("假人失效或已切换维度；物品保留在熔炉内");
            checkReach(sim, block.location);
            const out = cont.getItem(2);
            if (out) {
                const amount = Math.min(out.amount, count - state.made);
                const destination = snapshot(inventory);
                const source = snapshot(cont);
                const piece = out.clone();
                piece.amount = amount;
                insertItems(destination, [piece]);
                if (amount === out.amount) source[2] = undefined;
                else source[2].amount -= amount;
                commitInventories([[inventory, destination], [cont, source]]);
                state.made += amount;
                state.outputId = out.typeId;
                if (state.made >= count) {
                    finishInteraction(req.n, state, true, "", { made: { id: state.outputId, count: state.made } });
                    return;
                }
            }
            // 普通熔炉每件约 200 tick；旧值 120 tick 连一件都等不到。
            if (++state.ticks >= count * 240 + 200) throw new Error("熔炼超时；未完成的材料和产物保留在熔炉内");
        } catch (e) {
            finishInteraction(req.n, state, false, String(e), { made: { id: state.outputId, count: state.made } });
        }
    }, 1);
}

function doContainer(req, id) {
    const sim = sims.get(req.n);
    if (!sim) { result(id, false, "假人不存在"); return; }
    guard(id, () => {
        if (!sim.isValid) throw new Error("假人已失效");
        if (interactions.has(req.n)) throw new Error("假人正在交互");
        const op = req.op || "put";
        if (op === "craft") { doCraft(sim, req, id); return; }
        const loc = vec(req.x, req.y, req.z);
        checkReach(sim, loc);
        const block = sim.dimension.getBlock(loc);
        if (!block) throw new Error("方块不存在或未加载");
        const component = block.getComponent("minecraft:inventory");
        if (!component || !component.container) throw new Error("这个方块没有容器");
        const cont = component.container;
        if (op === "smelt") { doSmelt(sim, req, id, block, cont); return; }
        if (op !== "put" && op !== "take") throw new Error("未知容器操作 " + op);
        const inventory = playerInventory(sim);
        const source = op === "put" ? inventory : cont;
        const destination = op === "put" ? cont : inventory;
        const sourceItems = snapshot(source);
        const destinationItems = snapshot(destination);
        const count = requestedCount(req.count);
        const moved = reserveItems(sourceItems, normId(req.item), count);
        insertItems(destinationItems, moved);
        commitInventories([[source, sourceItems], [destination, destinationItems]]);
        reply("bot:result", { i: id, ok: true, e: "", moved: count });
    });
}

// ---------------------------------------------------------------------------
// 攻击
// ---------------------------------------------------------------------------

function doAttack(req, id) {
    const sim = sims.get(req.n);
    if (!sim) {
        result(id, false, "假人不存在");
        return;
    }
    guard(id, () => {
        const targetName = String(req.target || "").replace(/^minecraft:/, "");
        const origin = sim.location;
        const dim = sim.dimension;
        let best = null;
        let bestDist = Infinity;
        for (const ent of dim.getEntities()) {
            if (ent.id === sim.id) continue;
            const id2 = (ent.typeId || "").split(":").pop();
            if (id2 !== targetName && ent.name !== targetName && ent.nameTag !== targetName && ent.id !== targetName) continue;
            const d = Math.hypot(
                ent.location.x - origin.x,
                ent.location.y - origin.y,
                ent.location.z - origin.z
            );
            if (d < bestDist) {
                bestDist = d;
                best = ent;
            }
        }
        if (!best) {
            result(id, false, `附近没有 ${targetName}`);
            return;
        }
        if (bestDist > 4.5) {
            result(id, false, `${targetName} 距离 ${bestDist.toFixed(1)} 格，够不着`);
            return;
        }
        // 先转向目标再出手：attackEntity 的命中看朝向，不转脸就是
        // 对着空气挥（实测怪物不掉血的一个来源）。yaw/pitch 从向量算。
        try {
            const dx = best.location.x - origin.x;
            const dy = best.location.y - origin.y;
            const dz = best.location.z - origin.z;
            const horizontal = Math.hypot(dx, dz);
            const yaw = Math.atan2(-dx, -dz) * 180 / Math.PI;
            const pitch = Math.atan2(dy, horizontal) * 180 / Math.PI;
            sim.setRotation({ x: Math.max(-90, Math.min(90, pitch)), y: yaw });
        } catch (_) { /* 转向失败不挡出手 */ }
        let ok = false;
        try {
            ok = sim.attackEntity(best);
        } catch (e) {
            result(id, false, String(e));
            return;
        }
        result(id, ok !== false, ok === false ? "攻击冷却中" : "");
    });
}

// ---------------------------------------------------------------------------
// 杂项
// ---------------------------------------------------------------------------

// 护甲材质等级：数值越大越好。只认原版护甲（自定义命名空间的护甲
// 属性没谱，不擅自往身上套）。
const ARMOR_RANK = {
    leather: 1, golden: 2, chainmail: 3, turtle: 4, copper: 4,
    iron: 5, diamond: 6, netherite: 7,
};
const ARMOR_SLOT = {
    helmet: EquipmentSlot.Head,
    chestplate: EquipmentSlot.Chest,
    leggings: EquipmentSlot.Legs,
    boots: EquipmentSlot.Feet,
};

function doEquip(req, id) {
    const sim = sims.get(req.n);
    if (!sim) {
        result(id, false, "假人不存在");
        return;
    }
    guard(id, () => {
        const cont = playerInventory(sim);
        let eq = null;
        try {
            eq = sim.getComponent("minecraft:equippable");
        } catch (e) { /* 组件不可用 */ }
        if (!eq) throw new Error("装备栏不可访问");
        const worn = [];
        for (const kind of ["helmet", "chestplate", "leggings", "boots"]) {
            const slot = ARMOR_SLOT[kind];
            // 已穿戴的等级
            let curRank = -1;
            try {
                const cur = eq.getEquipment(slot);
                if (cur) {
                    const m = String(cur.typeId || "").split(":")[1] || "";
                    curRank = ARMOR_RANK[m.replace("_" + kind, "")] ?? -1;
                }
            } catch (e) { /* 读不到按空装备处理 */ }
            // 背包里最好的同类护甲
            let bestSlot = -1, bestRank = curRank, bestItem = null;
            for (let i = 0; i < cont.size; i++) {
                const it = cont.getItem(i);
                if (!it) continue;
                const typeId = String(it.typeId || "");
                if (!typeId.startsWith("minecraft:")) continue;
                const short = typeId.split(":")[1];
                if (!short.endsWith("_" + kind)) continue;
                const rank = ARMOR_RANK[short.slice(0, -kind.length - 1)] ?? 0;
                if (rank > bestRank) { bestRank = rank; bestSlot = i; bestItem = it; }
            }
            if (bestItem) {
                try {
                    cont.setItem(bestSlot, undefined);
                    eq.setEquipment(slot, bestItem);
                    worn.push(kind);
                } catch (e) {
                    // 穿戴失败要把东西放回去，不能白拿玩家一格
                    try { cont.setItem(bestSlot, bestItem); } catch (_) {}
                }
            }
        }
        // 副手盾牌（没拿东西才补）
        try {
            if (!eq.getEquipment(EquipmentSlot.Offhand)) {
                for (let i = 0; i < cont.size; i++) {
                    const it = cont.getItem(i);
                    if (it && String(it.typeId || "").endsWith("shield")) {
                        cont.setItem(i, undefined);
                        eq.setEquipment(EquipmentSlot.Offhand, it);
                        worn.push("offhand");
                        break;
                    }
                }
            }
        } catch (e) { /* 副手失败不致命 */ }
        result(id, true, worn.length ? ("已穿戴: " + worn.join(",")) : "无需更换");
    });
}

function doSelect(req, id) {
    const sim = sims.get(req.n);
    if (!sim) {
        result(id, false, "假人不存在");
        return;
    }
    guard(id, () => {
        const container = playerInventory(sim);
        const slot = findSlot(container, normId(req.item));
        if (slot < 0) { result(id, false, `背包里没有 ${req.item}`); return; }
        selectInventorySlot(sim, slot);
        result(id, true, "");
    });
}

function doStatus(req) {
    const out = [];
    for (const [name, sim] of sims) {
        try {
            if (!sim.isValid) continue;
            const loc = sim.location;
            const dim = sim.dimension ? sim.dimension.id : "overworld";
            let health = null;
            let food = null;
            try {
                const h = sim.getComponent("minecraft:health");
                if (h) health = h.currentValue;
            } catch (e) { /* 忽略 */ }
            try {
                const f = sim.getComponent("minecraft:player.saturation");
                if (f) food = f.value;
            } catch (e) { /* 忽略 */ }
            out.push({
                n: name,
                x: Math.round(loc.x * 100) / 100,
                y: Math.round(loc.y * 100) / 100,
                z: Math.round(loc.z * 100) / 100,
                d: dim,
                hp: health,
                food: food,
                // ⚠️ 旧版这里查的是"脚下这一格是不是正在挖"，几乎恒为
                // false —— 假人是站在**旁边**挖目标方块的，脚下那格通常
                // 根本不是挖掘对象。`/ai list` 于是永远显示"空闲"。
                // 改成"这个名字名下有没有任何进行中的挖掘"。
                busy: walking.has(name) || interactions.has(name) || Array.from(digging.keys()).some((k) => k.startsWith(name + "|")),
            });
        } catch (e) {
            // 读到一半失效了，跳过
        }
    }
    reply("bot:status", { p: out });
}

// ---------------------------------------------------------------------------
// 枪战 arena 战斗 AI
// ---------------------------------------------------------------------------

/**
 * 对局内的自动战斗循环（Python 侧用 bot:arena 开关并下发敌人名单）。
 *
 * 逻辑（服主定的）：找最近的敌人 → 走过去 → 看得见（射线无遮挡）
 * 就对着他用 `useItemInHand()` 开枪。射击本身由枪械包的脚本结算，
 * 这里只负责"瞄准 + 扣扳机"。
 *
 * 敌我分辨靠 Python 传来的**敌人名单**（队伍名牌平时是隐藏的，
 * JS 侧没法自己辨队）；名单由插件定期刷新（有人中途加入也跟得上）。
 *
 * 自动退场：敌人 30 秒不在线/不在附近，或超过对局时长上限。
 */
const arenaModes = new Map(); // name -> {enemies:Set, until, lastNav, lastShot, seenAt}

function doArena(req, id) {
    const name = req.n;
    if (req.off) {
        arenaModes.delete(name);
        result(id, true, "");
        return;
    }
    const enemies = Array.isArray(req.enemies) ? req.enemies.map(String) : [];
    // 把敌人名单同步给 arena_combat.js：这台服取不到玩家视线方向时，
    // "真人开枪打假人"要靠"谁在跟这个玩家对枪"来判定命中。
    try { setSimEnemies(name, enemies); } catch (_) { /* ignore */ }
    const minutes = Math.max(1, Math.min(30, Number(req.min) || 6));
    const prev = arenaModes.get(name);
    arenaModes.set(name, {
        enemies: new Set(enemies),
        until: system.currentTick + minutes * 1200,
        lastNav: prev ? prev.lastNav : 0,
        lastShot: prev ? prev.lastShot : 0,
        seenAt: system.currentTick,
    });
    result(id, true, "");
}

function pumpArena() {
    if (arenaModes.size === 0) return;
    const now = system.currentTick;
    for (const [name, st] of Array.from(arenaModes)) {
        if (now > st.until) { arenaModes.delete(name); continue; }
        const sim = sims.get(name);
        if (!sim || !sim.isValid) { arenaModes.delete(name); continue; }
        // 自己已经倒了就先停火，等重生回来再继续打。
        // 不检查的话会一直对着尸体扣扳机，日志被"实际扣血 0.0"刷爆。
        if (!isAlive(sim)) continue;

        // 最近的在线敌人。真人从 world.getAllPlayers() 拿，假人从 sims 表拿
        // ——脚本 API 里假人是 undefined，光靠 getAllPlayers() 永远找不到
        // 另一个假人，枪战服里就变成"假人只打真人、假人对枪永远不开火"。
        const found = findNearestEnemy(sim, st.enemies, sims);
        const best = found ? found.entity : null;
        const bestD = found ? found.dist : Infinity;
        const bestName = found ? found.name : "";
        if (!best) {
            // 敌人全都不见了（对局结束/全下线）——30 秒后自动退场
            if (now - st.seenAt > 600) arenaModes.delete(name);
            continue;
        }
        st.seenAt = now;

        // 一直面朝敌人（胸口高度）
        try {
            sim.lookAtLocation({ x: best.location.x, y: best.location.y + 1.2, z: best.location.z });
        } catch (e) { /* 瞄准失败不致命 */ }

        // 太远就追（0.75 秒刷新一次路径，目标在跑）
        if (bestD > 9) {
            if (now - st.lastNav >= 15) {
                try {
                    sim.navigateToLocation(
                        { x: best.location.x, y: best.location.y, z: best.location.z }, 1.0);
                } catch (e) { /* 走不了就算了 */ }
                st.lastNav = now;
            }
        } else if (now - st.lastNav >= 40) {
            try { sim.stopMoving(); } catch (e) { /* 没在动 */ }
            st.lastNav = now;
        }

        // 开火。
        //
        // ⚠️ 这里**不能**再用 `sim.useItemInHand()`：枪械包的主循环遍历的是
        // `world.getAllPlayers()`，而假人在那个数组里是 undefined，枪械包
        // 根本看不见它——扣了扳机也没有人结算子弹（而且枪物品带
        // minecraft:food，useItemInHand 还会把枪当食物吃掉）。
        //
        // 所以射击由桥接包自己结算：射线、扣血、弹药、表现全在这边做。
        if (now - st.lastShot >= 4) {
            st.lastShot = now;
            try {
                fireSimShot(sim, best, bestName);
            } catch (e) { /* 开火失败就算了这一轮 */ }
        }
    }
}

// ---------------------------------------------------------------------------
// 事件入口
// ---------------------------------------------------------------------------

system.afterEvents.scriptEventReceive.subscribe((event) => {
    if (!event.id.startsWith("bot:")) return;

    let data = {};
    try {
        if (event.message && event.message.length > 0) {
            data = JSON.parse(event.message);
        }
    } catch (e) {
        console.warn(`[ArcFakePlayer] JSON 解析失败: ${event.message}`);
        return;
    }

    // 第一条来自 Python 的消息教会我们令牌。之后所有回复都带上它，
    // Python 那边据此忽略掉其他插件/玩家伪造的 scriptevent。
    if (data.t) bridgeToken = data.t;

    const id = data.i !== undefined ? data.i : -1;

    // 每一条都包在 `safely()` 里。
    //
    // 这里**不能**图省事，在 switch 外面套一个 try —— 那样一旦某条指令
    // 抛了异常，后面所有指令都跟着不执行（整个 cb 提前 return）。
    // 逐条包才能做到"这条坏了不影响别的"。
    switch (event.id) {
        case "bot:ping":
            reply("bot:pong", { names: Array.from(sims.keys()), caps: caps() });
            break;
        case "bot:spawn":
            safely(() => doSpawn({ ...data, i: id }));
            break;
        case "bot:remove":
            safely(() => doRemove(data.n, id));
            break;
        case "bot:teleport":
            safely(() => doTeleport(data, id));
            break;
        case "bot:walk":
            safely(() => doWalk(data, id));
            break;
        case "bot:dig":
            safely(() => doDig(data, id));
            break;
        case "bot:till":
            safely(() => doTill(data, id));
            break;
        case "bot:plant":
            safely(() => doPlant(data, id));
            break;
        case "bot:place":
            safely(() => doPlace(data, id));
            break;
        case "bot:attack":
            safely(() => doAttack(data, id));
            break;
        case "bot:select":
            safely(() => doSelect(data, id));
            break;
        case "bot:stop":
            safely(() => doStop(data, id));
            break;
        case "bot:container":
            safely(() => doContainer(data, id));
            break;
        case "bot:jump":
            safely(() => doJump(data, id));
            break;
        case "bot:equip":
            safely(() => doEquip(data, id));
            break;
        case "bot:drop":
            safely(() => doDrop(data, id));
            break;
        case "bot:arena":
            safely(() => doArena(data, id));
            break;
        case "bot:consume":
            safely(() => doConsume(data, id));
            break;
        case "bot:list":
            reply("bot:list_result", { names: Array.from(sims.keys()), caps: caps() });
            break;
        case "bot:status":
            safely(() => doStatus(data));
            break;
        default:
            break;
    }
});

// 每 tick 推进挖掘轮询。放这里而不是 runInterval，
// 因为挖掘的等待精度直接决定"看起来像不像在挖"。
system.runInterval(pumpDigging, 1);
// arena 战斗 AI：0.25 秒一拍（追击刷新 0.75s、开火节流 0.2s 都由内部计时）
system.runInterval(pumpArena, 5);

// 复活落点监控：每秒看一眼假人的位置。
//
// 假人死后引擎会把它放回**重生点**。以前我们没设过重生点，它就落回
// 世界出生点（ARCShooterGame 的 level.dat 里是 0,53,0），于是"死了以后
// 不知道复活到哪里去了"。现在生成时就 setSpawnPoint 记住了，这里再盯
// 一次位置突变，把"谁在哪儿重生了"打进日志。
system.runInterval(() => {
    if (sims.size === 0) return;
    try {
        for (const [name, sim] of Array.from(sims.entries())) {
            try {
                if (!sim || sim.isValid === false) continue;
                watchRespawn(name, sim);
                autoRespawn(name, sim);
            } catch (e) { /* 单个失败不影响别的 */ }
        }
    } catch (e) { /* ignore */ }
}, 20);

// 自动复活：假人没有客户端去点死亡界面的"重生"按钮——没人替它点，
// 它就永远躺在死亡位置（对其他玩家不可见，干活/战斗全停）。这里每秒
// 查一次血量，归零就替它点：延迟 2~5 秒随机，像真人看到"你死了"
// 界面反应一下再点。respawn() 在旧引擎上可能不存在，先探再调。
const respawnDue = new Map();
function autoRespawn(name, sim) {
    let hp;
    try {
        hp = sim.getComponent("minecraft:health")?.currentValue;
    } catch (_) {
        return;
    }
    if (hp === undefined) return;
    if (hp > 0) {
        respawnDue.delete(name);
        return;
    }
    if (typeof sim.respawn !== "function") return;
    const now = system.currentTick;
    const due = respawnDue.get(name);
    if (due === undefined) {
        respawnDue.set(name, now + 40 + Math.floor(Math.random() * 60));
        return;
    }
    if (now < due) return;
    respawnDue.delete(name);
    try {
        sim.respawn();
        console.log(`${TAG} 假人 ${name} 阵亡，已自动重生（替它点了重生按钮）`);
        teleportAfterRespawn(name, sim);
    } catch (e) {
        console.warn(`${TAG} 假人 ${name} 自动重生失败: ${e}`);
    }
}

/**
 * 重生之后把假人挪到**验证过的地面落点**。
 *
 * 为什么要多这一步
 * ----------------
 * `sim.respawn()` 把假人放回**引擎认定的重生点**。而这个重生点我们设不上：
 * 对 SimulatedPlayer，`setSpawnPoint` 两种写法都不认（单参数写法因为实体
 * 不暴露 dimension 而失败，双参数写法因为本机引擎只收 0~1 个参数而失败），
 * 于是引擎退回**世界出生点**。
 *
 * 世界出生点的高度是 level.dat 里的"自动"值（无规则服 ARCAnarchy 是
 * `SpawnY=32767`，由引擎运行时解析，实测 200 / 312 都出现过），而那一列
 * 在山上——所以假人每次重生都掉回半空，落地摔死，接着再重生，死循环。
 *
 * 2026-10-31 加固（"复活了就摔死"仍在发生）：
 *   1. 记录的重生点**用之前先验**（verifyGroundPoint）：脚下不是实地就
 *      沿这一列向下修正——记录点可能本身就在半空（配置坐标偏差、生成时
 *      扫描失败拿生成点当了重生点），传过去等于主动送它摔一次。
 *   2. 验不了/没有记录点 → 扫**假人自己脚下这一列**：它人就在那，区块
 *      必然是加载的，扫到的地面就是能站的地方。绝不留它在世界出生点半空。
 *   3. 传送从"一锤子买卖"改成**传送 + 校验 + 重试**：老写法第 3 拍传一次
 *      就收工，但引擎的重生摆位可能比我们的传送**晚到**（新引擎上实测过
 *      被覆盖），传送等于白发。现在传完盯着位置，没到点就再传，最多 3 次。
 */
function teleportAfterRespawn(name, sim) {
    let ticks = 0;
    let attempts = 0;
    let dest = null;
    const timer = system.runInterval(() => {
        ticks += 1;
        try {
            if (!sim || sim.isValid === false) {
                system.clearRun(timer);
                return;
            }
            // 等引擎把这次重生做完（位置还没落地时传送会被覆盖）。
            if (ticks < 3) return;

            if (dest === null) {
                dest = pickRespawnDest(name, sim);
                if (dest === null) {
                    system.clearRun(timer);
                    console.warn(
                        `${TAG} 假人 ${name} 重生后找不到可站的落点，先留在原地等下一轮`,
                    );
                    return;
                }
            }

            const loc = sim.location;
            const arrived = Math.hypot(
                loc.x - dest.x, loc.y - dest.y, loc.z - dest.z,
            ) <= 4;
            if (arrived) {
                system.clearRun(timer);
                console.log(`${TAG} 假人 ${name} 重生后已落到 (${dest.x}, ${dest.y}, ${dest.z})`);
                return;
            }
            // 途中又死了 → 这轮作废，autoRespawn 会带着新的重生再走一遍。
            const hp = safely(() => sim.getComponent("minecraft:health")?.currentValue);
            if (hp !== undefined && hp !== null && hp <= 0) {
                system.clearRun(timer);
                return;
            }
            if (attempts >= 3) {
                system.clearRun(timer);
                console.warn(
                    `${TAG} 假人 ${name} 重生后传送 3 次仍没站住，现在在`
                    + ` (${loc.x.toFixed(1)}, ${loc.y.toFixed(1)}, ${loc.z.toFixed(1)})`,
                );
                return;
            }
            attempts += 1;
            const dim = world.getDimension("overworld");
            sim.teleport({ x: dest.x, y: dest.y, z: dest.z }, { dimension: dim });
        } catch (e) {
            if (attempts >= 3) {
                system.clearRun(timer);
                console.warn(`${TAG} 假人 ${name} 重生后挪回地面失败: ${e}`);
            } else {
                attempts += 1;   // 单次异常（比如区块没加载好）→ 还有机会
            }
        }
    }, 1);
}

/**
 * 选这次重生要传送去的落点（只选一次，之后 teleportAfterRespawn 盯着它）。
 *
 * 优先级：
 *   1. 记录的重生点 → verifyGroundPoint 验证/修正（脚下不是实地就沿列
 *      下探到真地面）。修过的点**写回去**，不能每次重生都摔一遍再修一遍。
 *   2. 记录点验不了（那一列区块没加载）或压根没有记录点 → 扫假人**自己
 *      脚下这一列**。它人就在那，区块必然加载，扫到的就是能站的地方。
 *   3. 都不行（整列虚空/异常）→ null，调用方放弃本次传送。
 */
function pickRespawnDest(name, sim) {
    const recorded = getRespawnPoint(name);
    if (recorded) {
        const verified = verifyGroundPoint(recorded);
        if (verified) {
            if (verified.y !== recorded.y) {
                console.warn(
                    `${TAG} 假人 ${name} 的记录重生点 (${recorded.x}, ${recorded.y}, ${recorded.z})`
                    + ` 脚下不是实地，修正为 (${verified.x}, ${verified.y}, ${verified.z})`,
                );
                setRespawnPoint(name, verified);
            }
            return verified;
        }
        console.warn(
            `${TAG} 假人 ${name} 的记录重生点 (${recorded.x}, ${recorded.y}, ${recorded.z})`
            + " 验证不了（区块没加载？），改扫它脚下这一列",
        );
    }
    let loc;
    try {
        loc = sim.location;
    } catch (_) {
        return null;
    }
    return verifyGroundPoint({
        x: Math.floor(loc.x), y: Math.floor(loc.y), z: Math.floor(loc.z),
    });
}

// 心跳：每 5 秒无条件 pong。Python 用它判断行为包是否真的活着。
//
// ⚠️ 这个回调**必须**自己不抛。理由不是"怕崩溃"，是更阴的：
//    一旦它抛了一次，`runInterval` 注册的周期任务在某些引擎版本里会
//    连带被判定为出错而**不再调度**——心跳就永久停了。而 Python 侧的
//    判断恰好是"多久没收到 pong 就认为行为包死了"，于是它会开始
//    `_halt_bodies`、清掉全部假人。也就是说：**心跳自己抛一次，
//    整套假人就全没了**，而日志里只会看到一句"行为包失去联系"。
//    真正的原因（这个回调抛了什么）反而不会出现在任何地方。
system.runInterval(() => {
    try {
        reply("bot:pong", { names: Array.from(sims.keys()) });
    } catch (e) {
        console.warn(`[ArcFakePlayer] 心跳上报失败（下个周期会重试）: ${e}`);
    }
}, 100);

// 每 5 秒清理失效假人 + 上报坐标，供 Python 侧落库/自愈
//
// 和心跳同理：这个回调也不能抛。它负责的是"假人没了要让 Python 知道"，
// 一旦它停摆，Python 侧的名单就会永远停在旧状态——自愈任务会去
// 反复复活一个其实已经死透的假人。
system.runInterval(() => {
    if (sims.size === 0) return;
    const report = [];
    // 整个循环包一层。循环体里逐项已有 try，这里兜的是
    // `Array.from(sims.entries())` 和循环之外的收尾。
    try {
        for (const [name, sim] of Array.from(sims.entries())) {
            try {
                if (!sim.isValid) {
                    sims.delete(name);
                    continue;
                }
                const loc = sim.location;
                report.push({
                    n: name,
                    x: Math.round(loc.x * 100) / 100,
                    y: Math.round(loc.y * 100) / 100,
                    z: Math.round(loc.z * 100) / 100,
                    d: sim.dimension ? sim.dimension.id : "overworld",
                });
            } catch (e) {
                sims.delete(name);
            }
        }
    } catch (e) {
        console.warn(`[ArcFakePlayer] 坐标上报异常（已兜住）: ${e}`);
    }
    if (report.length > 0 && bridgeToken) {
        reply("bot:positions", { p: report });
    }
}, 100);

// 引擎加载时踢一下 Python，让它知道行为包起来了，不用等下一个心跳周期
system.run(() => {
    try {
        reply("bot:pong", { names: [], caps: caps() });
    } catch (e) {
        console.warn(`[ArcFakePlayer] 启动握手失败: ${e}`);
    }
});

console.log(
    `[ArcFakePlayer] 假人桥接行为包已加载` +
    `（模块级 spawnSimulatedPlayer: ${typeof gametest.spawnSimulatedPlayer === "function" ? "可用" : "不可用"}）`
);
