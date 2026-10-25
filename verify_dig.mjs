/**
 * 挖掘链路仿真：用假的 @minecraft/server / server-gametest 跑真实的 main.js，
 * 把"假人为什么会隔墙挖、为什么会一顿一顿、以及**还挖不挖得动**"变成数字。
 *
 * ═══ 对引擎的建模 ═══════════════════════════════════════════════════════════
 * 只建模两条**有官方文档依据**的行为，其余都是普通调度/世界模型。
 *
 *  ① breakBlock(loc) 只认坐标
 *     官方原文：
 *       "Destroys the block at blockLocation, respecting the rules of the server
 *        player's game mode. The block will be hit until broken, an item is used
 *        or stopBreakingBlock is called. Returns true if the block at
 *        blockLocation is solid."
 *     通篇没有任何"必须看得见 / 必须够得着 / 不能隔着东西"的措辞。仿真就照
 *     字面来：**不管假人站在哪、朝哪看、中间有没有挡着**，坐标上是固体就照敲。
 *     这正是"穿墙挖、隔空挖"的来源。
 *
 *  ② getBlockFromViewDirection({maxDistance}) 是从头部发出的射线
 *     返回**第一个被挡到的东西**（6 格内）。仿真按真实几何沿视线步进取第一个
 *     固体格——所以中间有墙时，它返回的是墙，不是墙后的目标。
 *
 *  ③（关键）lookAtBlock 是**异步**的
 *     引擎里 `lookAtBlock(loc)` 只是"请求转向"，真正转到位要几拍。仿真用
 *     "目标朝向 + 倒计时"建模：请求后要 AIM_DELAY 拍才转正；重复请求**同一
 *     个点**是幂等的（不重置倒计时），这跟真实引擎一致。
 *     这一条专门用来压测"闸门会不会因为头还没转正就误判成'挖不掉'"。
 *
 *     仿真**忠实**地朝"调用方传进来的那个点"看，不再自己加偏移。
 *     main.js 传的是方块中心（loc+0.5），所以射线射入目标格内部、稳定
 *     返回目标；若改回传整数角点，射线会贴格子边界走，可能返回相邻格。
 *
 * 另有一条**明确标注的扰动**，用于演示"一顿一顿"：
 *
 *  ④ STRESS_AFTER：挖到一半时，引擎静默丢失这次敲击
 *     （实机里对应被推挤、区块边界、状态抖动）。这不是官方语义，而是**实测
 *     会发生、且两个成熟假人包都专门防着**的故障：Understudy 走 continuous、
 *     FlashFakePlayerPack 走 system.runInterval(_, 0)，都是"每 tick 补发一次"
 *     来随时接上中断。仿真用它区分"只发起一次"和"每 tick 补发"两种写法。
 *
 * 用法: node verify_dig.mjs <main.js 路径> [标签]
 */

import fs from "node:fs";

const MAIN = process.argv[2];
const LABEL = process.argv[3] || MAIN;
// --strict：把 main.js 里的视线闸门开关翻成 true，用来验证"严格模式"仍然
// 能挡住穿墙挖。默认是关的（真机上开它会让 breakBlock 一次都调不到）。
const STRICT = process.argv.includes("--strict");
const SRC_RAW = fs.readFileSync(MAIN, "utf8").replace(/^\s*import .*$/gm, "");
const FLAG = "const DIG_STRICT_SIGHT = false;";
let SRC = SRC_RAW;
if (STRICT) {
    if (!SRC_RAW.includes(FLAG)) {
        console.error(`!! 在 ${MAIN} 里找不到 "${FLAG}"，无法切严格模式`);
        process.exit(1);
    }
    SRC = SRC_RAW.replace(FLAG, "const DIG_STRICT_SIGHT = true;");
}

// 与 main.js 里的常量保持一致
const STONE_TICKS = 150;   // 空手挖石头约 150 tick
const REACH = 5.0;         // DIG_MAX_DISTANCE
const RAY = 6;             // DIG_RAY_DISTANCE

const DEFAULT_FEET = { x: 4.5, y: 64.0, z: 0.5 };
const DEFAULT_EYE = { x: 4.5, y: 65.62, z: 0.5 };

const cellOf = (v) => ({ x: Math.floor(v.x), y: Math.floor(v.y), z: Math.floor(v.z) });
const same = (a, b) => !!a && !!b && a.x === b.x && a.y === b.y && a.z === b.z;
const approx = (a, b) => !!a && !!b &&
    Math.abs(a.x - b.x) + Math.abs(a.y - b.y) + Math.abs(a.z - b.z) < 1e-6;

// ---------------------------------------------------------------------------
// 单场景跑一遍
// ---------------------------------------------------------------------------
/**
 * @param {object} opt
 *   target        : 想挖的方块格坐标
 *   wall          : 挡在中间的那一格（可选）
 *   stressAfter   : 开始敲击多少 tick 后，引擎静默丢失这次敲击（0 = 不丢）
 *   feet          : 假人落脚点（可选，默认 DEFAULT_FEET）
 *   aimDelay      : lookAtBlock 转向到位的拍数（0 = 立即，>0 = 异步延迟）
 */
function runScenario(opt) {
    const TARGET = opt.target;
    const WALL = opt.wall || null;
    const FEET = opt.feet || DEFAULT_FEET;
    const EYE = { x: FEET.x, y: FEET.y + 1.62, z: FEET.z };
    const AIM_DELAY = opt.aimDelay || 0;
    const MAX_TICKS = 1600;

    // ── 世界 ──
    let targetAlive = true;
    let wallAlive = !!WALL;

    function solidTypeAt(c) {
        if (targetAlive && same(c, TARGET)) return "minecraft:stone";
        if (wallAlive && WALL && same(c, WALL)) return "minecraft:dirt";
        return null;
    }
    function destroy(c) {
        if (same(c, TARGET)) targetAlive = false;
        if (WALL && same(c, WALL)) wallAlive = false;
    }

    // ── 假人 ──
    let desiredAim = null;        // lookAtBlock 请求的注视点
    let aimPoint = null;          // 当前**实际**注视点（异步转过来才有值）
    let aimCountdown = 0;         // 还要几拍才转到位
    let hit = null;               // 正在被敲的方块 { cell, t }
    let breakOnTarget = 0;        // breakBlock 打在**目标**上的次数
    let breakOnOther = 0;         // breakBlock 打在别处的次数
    let lookCalls = 0;
    let globalTick = 0;

    function raycast() {
        if (!aimPoint) return null;
        const dx = aimPoint.x - EYE.x, dy = aimPoint.y - EYE.y, dz = aimPoint.z - EYE.z;
        const len = Math.hypot(dx, dy, dz);
        if (len < 1e-6) return null;
        const span = Math.min(len, RAY);
        for (let d = 0; d <= span + 1e-9; d += 0.05) {
            const u = d / len;
            const c = cellOf({
                x: EYE.x + dx * u, y: EYE.y + dy * u, z: EYE.z + dz * u,
            });
            const t = solidTypeAt(c);
            if (t) return { block: { location: c, typeId: t }, loc: c };
        }
        return null;
    }

    const sim = {
        get location() { return FEET; },
        get dimension() { return dim; },
        isValid: true,
        nameTag: "",
        lookAtBlock(loc) {
            lookCalls++;
            // 忠实建模：main.js 传哪个点，就朝哪个点看（**不再自己 +0.5**）。
            // main.js 现在传的是方块中心（loc+0.5），射线因此射入目标格
            // 内部；若哪天改回传整数角点，射线就会贴着格子边界走，可能
            // 返回相邻格 —— 这正是"盯着方块却挖不动"的一种成因。
            const p = { x: loc.x, y: loc.y, z: loc.z };
            // 幂等性按"请求点"判断，而不是"当前已转到的点"：再发一次
            // lookAt **同一个方块**不应重新计时（真实引擎里就是同一条转向
            // 指令，不会因为重发而延长）。只有换了新目标才重新计时。
            const changed = !desiredAim || !approx(desiredAim, p);
            desiredAim = p;
            if (AIM_DELAY <= 0) {
                aimPoint = p; aimCountdown = 0;          // 立即生效
            } else if (changed) {
                aimCountdown = AIM_DELAY;                // 转到位要 AIM_DELAY 拍
            }
            // 转向途中 aimPoint 保持旧值 → 射线打空/打偏，模拟"头还没转正"
        },
        breakBlock(loc) {
            const c = cellOf(loc);
            const t = solidTypeAt(c);
            if (!t) return false;
            if (same(c, TARGET)) breakOnTarget++; else breakOnOther++;
            // ① 只认坐标：不看假人在哪、看哪、有没有被挡
            if (!hit || !same(hit.cell, c)) hit = { cell: c, t: 0 };
            return true;
        },
        getBlockFromViewDirection() { return raycast() || undefined; },
        getComponent() { return undefined; },
        disconnect() {}, stopMoving() {}, setItem() {}, jump() {}, useItem() {},
        stopBreakingBlock() { hit = null; },
    };

    const dim = {
        id: "overworld",
        runCommand(cmd) { out.push(cmd); },
        getBlock(p) {
            const t = solidTypeAt(cellOf(p));
            return t ? { typeId: t } : { typeId: "minecraft:air" };
        },
    };

    // ── 调度器 ──
    let now = 0, nextId = 1;
    const jobs = new Map();
    const system = {
        get currentTick() { return now; },
        run(fn) { const id = nextId++; jobs.set(id, { at: now + 1, fn, every: 0 }); return id; },
        runTimeout(fn, d = 1) { const id = nextId++; jobs.set(id, { at: now + Math.max(1, d), fn, every: 0 }); return id; },
        runInterval(fn, d = 1) { const id = nextId++; jobs.set(id, { at: now + Math.max(1, d), fn, every: Math.max(1, d) }); return id; },
        clearRun(id) { jobs.delete(id); },
        afterEvents: { scriptEventReceive: { subscribe: (cb) => receivers.push(cb) } },
    };

    let stressFired = false;      // 扰动只触发一次（模拟"偶发的"丢失，不是持续故障）
    function tick() {
        now++; globalTick = now;
        // ③ 异步转向推进：倒计时归零才真正把朝向转到请求点
        if (AIM_DELAY > 0 && aimCountdown > 0) {
            aimCountdown--;
            if (aimCountdown === 0 && desiredAim) aimPoint = desiredAim;
        }
        for (const [id, j] of [...jobs.entries()]) {
            if (j.at !== now) continue;
            if (j.every > 0) j.at = now + j.every; else jobs.delete(id);
            try { j.fn(); } catch (e) { /* 与真实运行时一致：回调里抛异常不该停摆 */ }
        }
        // ② 推进"敲到碎为止"（① 已开始敲的话）
        if (hit) {
            hit.t++;
            // ④ 扰动：挖到一半引擎静默丢失这次敲击（只丢一次）
            if (opt.stressAfter && !stressFired && hit.t === opt.stressAfter) {
                stressFired = true;
                hit = null;
            } else if (hit.t >= STONE_TICKS) { destroy(hit.cell); hit = null; }
        }
    }

    // ── 载入 main.js ──
    const receivers = [];
    const out = [];
    const world = { getDimension: () => dim };
    const gametest = {
        register: () => ({ structureName: () => ({ maxTicks: () => ({ tag: () => {} }) }) }),
        spawnSimulatedPlayer: () => sim,
        Tags: { suiteDefault: "suiteDefault" },
    };
    const ItemStack = class { constructor(id, n) { this.typeId = id; this.amount = n; } };
    const EquipmentSlot = { Head: "Head", Chest: "Chest", Legs: "Legs",
        Feet: "Feet", Offhand: "Offhand", Mainhand: "Mainhand" };
    new Function("gametest", "system", "world", "ItemStack", "EquipmentSlot", "console", SRC)(
        gametest, system, world, ItemStack, EquipmentSlot, { log() {}, warn() {} });

    const send = (id, payload) =>
        receivers.forEach((cb) => cb({ id, message: JSON.stringify({ ...payload, t: "tok" }), sourceType: "Server" }));

    const duGs = [];
    const origPush = out.push.bind(out);
    out.push = (c) => { origPush(c); if (c.includes("bot:dug")) duGs.push(c); };

    send("bot:spawn", { n: "Steve", x: FEET.x, y: FEET.y, z: FEET.z, i: 1 });
    for (let i = 0; i < 5; i++) tick();

    // ── 下令挖 ──
    let digPending = true;
    let gap = 0, maxGap = 0;
    send("bot:dig", { n: "Steve", x: TARGET.x, y: TARGET.y, z: TARGET.z, i: 100 });

    for (let t = 0; t < MAX_TICKS; t++) {
        const before = breakOnTarget + breakOnOther;
        tick();
        if (digPending) {
            if (breakOnTarget + breakOnOther > before) gap = 0;
            else { gap++; if (gap > maxGap) maxGap = gap; }
        }
        if (duGs.length) { digPending = false; break; }
    }

    const parsed = duGs.map((c) =>
        JSON.parse(c.slice(c.indexOf(" ") + 1).replace(/^bot:dug\s*/, "")));
    const done = parsed.find((p) => p.ok === true && !p.already);

    return {
        label: opt.label,
        broke: !targetAlive,
        wallBroken: !!WALL && !wallAlive,
        ok: !!done,
        err: parsed.length ? (parsed[0].e || "") : "无结论（超时未回）",
        ticks: now,
        breakOnTarget, breakOnOther, lookCalls, maxGap,
        expect: opt.expect || (opt.wall ? "refuse" : "break"),
    };
}

// ---------------------------------------------------------------------------
// 几何准备
// ---------------------------------------------------------------------------
// 场景 B 的墙：取视线在 u=0.6 处穿过的那一格，保证它真的挡在中间
function wallOnSightline(target) {
    const aim = { x: target.x + 0.5, y: target.y + 0.5, z: target.z + 0.5 };
    const u = 0.6;
    return cellOf({
        x: DEFAULT_EYE.x + (aim.x - DEFAULT_EYE.x) * u,
        y: DEFAULT_EYE.y + (aim.y - DEFAULT_EYE.y) * u,
        z: DEFAULT_EYE.z + (aim.z - DEFAULT_EYE.z) * u,
    });
}

const NEAR = { x: 5, y: 64, z: 0 };
const FAR_BEHIND_WALL = { x: 8, y: 64, z: 0 };
const VERY_FAR = { x: 30, y: 64, z: 0 };
const WALL = wallOnSightline(FAR_BEHIND_WALL);

// 距离闸门边界：脚下(4.6,64,0.5) → 目标中心(9.5,64.5,0.5) = 4.925 格（应挖掉）
//                脚下(4.5,64,0.5) → 目标中心(9.5,64.5,0.5) = 5.025 格（应拒绝）
const EDGE_OK_TARGET = { x: 9, y: 64, z: 0 };
const EDGE_OK_FEET = { x: 4.6, y: 64.0, z: 0.5 };
const EDGE_NO_FEET = { x: 4.5, y: 64.0, z: 0.5 };

const scenarios = [
    // 必须挖掉 —— 这是"这次还挖不挖得动"的核心证明
    { label: "A 正常：看得见、够得着", target: NEAR, expect: "break" },
    { label: "E 朝向是异步的：要 6 拍才转正", target: NEAR, aimDelay: 6, expect: "break" },
    { label: "F 距离闸门边缘内（4.93 格）", target: EDGE_OK_TARGET, feet: EDGE_OK_FEET, expect: "break" },
    { label: "D 挖到一半引擎丢失敲击（第 40 tick）", target: NEAR, stressAfter: 40, expect: "break" },
    // 必须如实拒绝 —— 这是"不穿墙/不隔空"的证明
    // 隔墙：严格模式下必须拒绝；默认（非严格）模式**会挖掉**——这是关掉
    // 视觉闸门的代价，防作弊改由距离闸门承担。两种结果都要如实记录。
    { label: `B 隔墙：目标在墙后 ${JSON.stringify(WALL)}`, target: FAR_BEHIND_WALL,
      wall: WALL, expect: "refuse", looseExpect: "break" },
    { label: "C 超距：目标在 25 格外、中间无阻挡", target: VERY_FAR, expect: "refuse" },
    { label: "G 刚超距离（5.03 格）", target: EDGE_OK_TARGET, feet: EDGE_NO_FEET, expect: "refuse" },
];

let pass = 0, fail = 0;
console.log(`\n╔══ ${LABEL}${STRICT ? "  【严格视线闸门 ON】" : "  【默认：只留距离闸门】"} ══`);
for (const sc of scenarios) {
    const r = runScenario(sc);
    const verdict = r.ok
        ? `✔ 挖掉（实际耗时 ${r.ticks} tick）`
        : `✘ 拒绝 / 失败：${r.err}`;
    // 期望校验：期望挖掉 → 必须真挖掉；期望拒绝 → 必须**没**挖掉
    const want = STRICT ? sc.expect : (sc.looseExpect || sc.expect);
    const good = want === "break" ? (r.ok && r.broke) : (!r.broke);
    if (good) pass++; else fail++;

    console.log(`\n  ▸ ${r.label}   [期望：${want === "break" ? "挖掉" : "拒绝"}]  ${good ? "✅" : "❌ 不符"}`);
    console.log(`      结论                : ${verdict}`);
    console.log(`      目标方块真的没了    : ${r.broke ? "是" : "否"}`);
    if (sc.wall) {
        console.log(`      挡路的墙也没了      : ${r.wallBroken ? "**是 —— 穿墙挖！**" : "否"}`);
    }
    console.log(`      breakBlock 打在目标 : ${r.breakOnTarget} 次`);
    console.log(`      breakBlock 打在别处 : ${r.breakOnOther} 次`);
    console.log(`      最长"无接触"时长    : ${r.maxGap} tick`
        + `${r.maxGap >= 40 ? "  ← 这么久一次都没续力，中间引擎一抖就断" : ""}`);
}
console.log(`\n  ── 小计：${pass} 通过 / ${fail} 不符 ──\n`);

// ---------------------------------------------------------------------------
// 转向延迟余量扫描：引擎 lookAtBlock 越慢，闸门越可能"头没转正就误判"。
// 报告"最多能容忍几拍才转正仍然挖得掉"，把余量讲清楚而不是只报一次成功。
// ---------------------------------------------------------------------------
console.log("── 转向延迟余量（AIM_DELAY = 转到位所需拍数）──");
const budget = "(AIM_LOST_TICKS+1)×AIM_MAX_RETRIES 上限，main.js 当前 ≈ 40 拍";
for (const delay of [0, 1, 2, 3, 6, 10, 16, 24, 32, 40, 48, 64]) {
    const r = runScenario({ label: `delay=${delay}`, target: NEAR, aimDelay: delay });
    console.log(`  AIM_DELAY=${String(delay).padStart(2)}  →  ${r.broke ? "✔ 挖掉" : "✘ " + r.err}`
        + `   (breakBlock 成功 ${r.breakOnTarget} 次, 实际 ${r.ticks} tick)`);
}
console.log(`  ${budget}\n`);
