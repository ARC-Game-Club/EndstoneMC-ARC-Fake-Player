/**
 * arena_combat.js —— 假人在「枪战对局」里的开火，以及死亡后的复活落点。
 *
 * 为什么要有这个文件
 * ------------------
 * 枪械包（失落枪械档案馆弧光重制版 BP）的主循环遍历的是
 * `world.getAllPlayers()`。而在本机 BDS 1.26.52.3 上，SimulatedPlayer
 * 在这个数组里是 **undefined**（实测快照）：
 *
 *     all=len=6 undef=5 [UNDEF,DEVILENMO,UNDEF,UNDEF,UNDEF,UNDEF]
 *
 * 也就是说枪械包**看不见假人**——它既不会给假人发枪、也不会替假人结算子弹。
 * 之前桥接包里的做法是"对着敌人调 `sim.useItemInHand()`，射击由枪械包结算"，
 * 在这个引擎上等于**扣了扳机没人管**。
 *
 * 所以：假人的射击必须由**握着 SimulatedPlayer 真身的这一侧**（本文件）
 * 亲手结算。枪械包负责真人，假人这边自己算。
 *
 * 第二个用途：真人开枪打假人
 * --------------------------
 * 枪械包的射线用的是 `dimension.getEntitiesFromRay()`，命中的是"无名实体"
 * （`entity.name` 为 null）。这种实体拿不到可用的 Player 包装，
 * `applyDamage()` 要么抛异常、要么静默不生效——表现就是"打假玩家没伤害"。
 * 枪械包在这种情况下会广播 `arc_gun:simhit` 事件，由本文件把伤害补到
 * 对应的假人身上（我们手上有真身，血量、血条、死亡全都正常）。
 *
 * 伤害表
 * ------
 * 与枪械包 `system/pvpGun.js` 的 `PVP_BODY_DAMAGE` 保持一致（单发身体伤害）。
 * 两边各存一份是有意的：跨行为包没法 import 对方的模块，而枪械包是
 * 混淆过的第三方包，改它风险大。改数值时**两边都要改**。
 */

import { world, system, EntityDamageCause } from "@minecraft/server";

const TAG = "[ArcFakePlayer]";

/** 单发身体伤害。与枪械包 PVP_BODY_DAMAGE 一致。 */
const SIM_GUN_DAMAGE = {
    "blazik:ak47": 4.5,
    "blazik:scarh": 4.5,
    "blazik:ak12": 3.25,
    "blazik:qbz191": 3.25,
    "blazik:m4": 3.0,
    "blazik:scarl": 3.0,
    "blazik:qbz95": 3.0,
    "blazik:sar80": 3.0,
    "blazik:rpk74": 3.5,
    "blazik:qbb95": 3.5,
    "blazik:m249": 3.0,
    "blazik:minimi": 3.0,
    "blazik:ump45": 3.25,
    "blazik:pmx": 3.0,
    "blazik:mp5": 2.75,
    "blazik:pp2000": 2.75,
    "blazik:k7": 2.5,
    "blazik:p90": 2.5,
    "blazik:fp6": 2.0,
    "blazik:spas12": 2.222,
    "blazik:ultraleggero": 3.333,
    "blazik:aa12": 1.25,
    "blazik:svch": 9.0,
    "blazik:hcar": 8.5,
    "blazik:saiga308": 8.0,
    "blazik:gm6_lynx": 22,
    "blazik:mrad": 14,
    "blazik:vshk": 8,
    "blazik:m1911": 7.5,
    "blazik:mpl1": 4.0,
    "blazik:glock17": 3.5,
    "blazik:pdp": 3.5,
};

const DEFAULT_DAMAGE = 3.0;
const HEADSHOT_MULTIPLIER = 1.5;
const LEGSHOT_MULTIPLIER = 0.7;
const MAX_RANGE = 48;
const EYE_HEIGHT = 1.62;

/** 名字 → SimulatedPlayer。由 main.js 在启动时交过来。 */
let simsRef = new Map();

/** 名字 → 出生点（我们生成它的地方）。 */
const spawnPoints = new Map();

/** 名字 → 上一拍的位置，用来发现"死后重生"造成的位置突变。 */
const lastPositions = new Map();

/** 名字 → 上一拍的血量，用来区分"死过然后重生"和"只是从高处掉下去"。 */
const lastHealth = new Map();

/** 名字 → 落地判定状态：连续两次采样 Y 不变就算站稳了。 */
const landingState = new Map();

/** 上一次广播 simhit 的时间，避免刷屏。 */
let lastSimHitLog = 0;

/** 射击日志限流：key = "射手|目标"，同一对每 N 毫秒最多记一条。 */
const shotLogAt = new Map();
const SHOT_LOG_INTERVAL_MS = 2000;

function logShotThrottled(key, message) {
    const now = Date.now();
    const last = shotLogAt.get(key) || 0;
    if (now - last < SHOT_LOG_INTERVAL_MS) return;
    shotLogAt.set(key, now);
    console.log(message);
}

export function initArenaCombat(sims) {
    if (sims) simsRef = sims;
}

/**
 * 这些方块不能当地面：踩上去会掉下去（树叶）或根本站不住（花草）。
 *
 * 判定用**后缀**匹配，因为方块 id 带树种：`minecraft:oak_leaves`、
 * `minecraft:birch_leaves`……只有按后缀才拦得住。注意 `grass_block` 的后缀
 * 是 `grass_block` 不是 `grass`，所以正常地面不会被误伤。
 *
 * 树干/树皮也算进去：站在树干上是站得住，但那是**树顶上**，离真正的
 * 地面还有几十格——重生点设在树冠 = 死后又摔一次。
 *
 * 为什么单列一份：Python 侧的地表扫描是"从上往下第一个非空气方块"，
 * 如果那一列正好长着树，它会返回**树冠的高度**（无规则服的世界出生点就在
 * 山上，Y 200 上下全是树冠），假人就出生在树冠上——然后掉下去摔死。
 */
const NOT_STANDABLE = [
    "leaves", "leaf", "azalea", "sapling", "tallgrass", "short_grass",
    "grass", "fern", "large_fern", "flower", "poppy", "dandelion",
    "red_flower", "tall_flower", "vine", "kelp", "seagrass", "sugar_cane",
    "deadbush", "torch", "carrots", "potatoes", "beetroot",
    "brown_mushroom", "red_mushroom", "lily_pad", "nether_sprout",
    "log", "wood", "stem", "hyphae",
];

/**
 * 从 fromY 往下找这一列第一块**能站人**的方块，返回可站立的 y（方块 y + 1）。
 *
 * ⚠️ `getBlock()` 在**区块没加载**时返回 undefined——那不是"这里是空气"，
 * 必须当成"扫不了"直接放弃，不能当成空气一路扫到底（那会一路扫到 y=-64
 * 然后返回 null，白白丢掉一个本来能扫出来的地面）。
 */
function findGroundY(x, z, fromY) {
    const dim = safe(() => world.getDimension("overworld"));
    if (!dim) return null;
    const start = Math.min(fromY, 318);
    for (let y = start; y >= -64; y--) {
        const block = safe(() => dim.getBlock({ x: x, y: y, z: z }));
        if (block === undefined) return null;   // 区块没加载 → 扫不了
        if (block.isAir || block.isLiquid) continue;
        const id = String(block.typeId || "").toLowerCase();
        if (NOT_STANDABLE.some((bad) => id.endsWith(bad))) continue;
        return y + 1;
    }
    return null;
}

/**
 * 设引擎重生点。
 *
 * ⚠️ **参数个数是这套 bug 的核心**。
 * 本机 BDS 1.26.52.3 的脚本 API 里 `setSpawnPoint` 只收 **0~1** 个参数
 * （dimension 参数被去掉了）。老写法 `setSpawnPoint(loc, dim)` 会抛：
 *
 *     TypeError: Incorrect number of arguments to function.
 *                Expected 0-1, received 2
 *
 * 于是重生点**从来没设成功过**——假人死后引擎把它放回"创建时的位置"。
 * 而创建时的位置常常在半空（Python 侧扫不到地表时退回 y=200），
 * 于是：出生在半空 → 摔死 → 自动重生 → 又是半空 → 再摔死，死循环。
 *
 * 所以先试 1 个参数（新引擎），失败再退回 2 个参数的写法（老引擎）。
 */
function applySpawnPoint(sim, point) {
    if (!sim || typeof sim.setSpawnPoint !== "function") return false;
    const loc = { x: point.x, y: point.y, z: point.z };
    try {
        sim.setSpawnPoint(loc);
        return true;
    } catch (e1) {
        try {
            const dim = safe(() => world.getDimension("overworld"));
            sim.setSpawnPoint(loc, dim);
            return true;
        } catch (e2) {
            console.warn(`${TAG} 设置重生点失败: ${e2}`);
            return false;
        }
    }
}

/**
 * 记录假人的出生点，并把它设成引擎的重生点。
 *
 * ⚠️ 重生点必须落在**地面上**，不能直接用"我们把它生成在哪"。
 * 生成点经常在半空（Python 侧扫不到地表 → 退回 y=200），
 * 拿半空当重生点 = 死后原地再摔一次。
 */
export function rememberSpawnPoint(name, sim, loc) {
    const point = { x: Math.floor(loc.x), y: Math.floor(loc.y), z: Math.floor(loc.z) };
    spawnPoints.set(name, point);
    lastPositions.set(name, null);
    landingState.delete(name);
    // 假人已经站在这一格上了，所以这一列的区块**一定是加载的**——
    // 这正是 Python 侧那次扫描失败（区块没加载）时缺的那一环。
    const groundY = findGroundY(point.x, point.z, point.y);
    const respawn = groundY === null
        ? point
        : { x: point.x, y: groundY, z: point.z };
    const ok = applySpawnPoint(sim, respawn);
    console.log(
        `${TAG} 假人 ${name} 出生于 (${point.x}, ${point.y}, ${point.z})；`
        + `重生点${ok ? "已设为" : "设置失败"} (${respawn.x}, ${respawn.y}, ${respawn.z})`
        + (groundY === null ? "（脚下没扫到地面，先按生成点设）" : ""),
    );
}

/**
 * 落地后把重生点钉在脚下。
 *
 * 为什么还要钉第二遍：生成那一刻的地面扫描也可能失败（区块还没加载完）。
 * 等它**真的站住了**（连续两次采样 Y 不变），脚下那块地一定是实的——
 * 这时候再设一次重生点，就再也不会"死后回到半空"。
 */
function pinSpawnPointOnLanding(name, sim, loc) {
    const st = landingState.get(name) || { y: null, still: 0, done: false };
    if (st.done) return;
    if (st.y !== null && Math.abs(loc.y - st.y) < 0.05) st.still += 1;
    else st.still = 0;
    st.y = loc.y;
    landingState.set(name, st);
    if (st.still < 1) return;      // 需要连续两次几乎不动
    st.done = true;
    const x = Math.floor(loc.x);
    const z = Math.floor(loc.z);
    const groundY = findGroundY(x, z, Math.floor(loc.y));
    const respawn = groundY === null
        ? { x: x, y: Math.floor(loc.y), z: z }
        : { x: x, y: groundY, z: z };
    if (applySpawnPoint(sim, respawn)) {
        console.log(`${TAG} 假人 ${name} 已落地，重生点钉在 (${respawn.x}, ${respawn.y}, ${respawn.z})`);
    }
}

export function getSpawnPoint(name) {
    return spawnPoints.get(name) || null;
}

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

function safe(fn) {
    try {
        return fn();
    } catch (_) {
        return undefined;
    }
}

function dist(a, b) {
    return Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);
}

function normalized(direction) {
    const length = Math.sqrt(direction.x ** 2 + direction.y ** 2 + direction.z ** 2) || 1;
    return {
        x: direction.x / length,
        y: direction.y / length,
        z: direction.z / length,
    };
}

function gunKeyOf(gunId) {
    return String(gunId).includes(":") ? String(gunId).split(":")[1] : String(gunId);
}

/** 假人手里拿着的枪（只看主手槽）。 */
export function heldGunId(sim) {
    try {
        const inv = sim.getComponent("minecraft:inventory");
        const item = inv?.container?.getItem(sim.selectedSlotIndex);
        if (item?.typeId) return item.typeId;
    } catch (_) { /* ignore */ }
    try {
        const eq = sim.getComponent("minecraft:equippable");
        const item = eq?.getEquipment("Mainhand");
        if (item?.typeId) return item.typeId;
    } catch (_) { /* ignore */ }
    return undefined;
}

// ---------------------------------------------------------------------------
// 弹药：走计分板（跨组件共享），和枪械包、EndStone 插件用同一套约定
// ---------------------------------------------------------------------------

function ammoObjective(gunKey) {
    const id = `${gunKey}Ammo`;
    try {
        let obj = world.scoreboard.getObjective(id);
        if (!obj) obj = world.scoreboard.addObjective(id, id);
        return obj;
    } catch (_) {
        return undefined;
    }
}

/** 读弹药。undefined = 没人写过（当成满弹匣，不去覆盖插件的值）。 */
export function readAmmo(name, gunKey) {
    const obj = ammoObjective(gunKey);
    if (!obj) return undefined;
    try {
        const v = obj.getScore(name);
        if (v === undefined || v === null) return undefined;
        return Number(v);
    } catch (_) {
        return undefined;
    }
}

export function writeAmmo(name, gunKey, value) {
    const obj = ammoObjective(gunKey);
    if (!obj) return false;
    try {
        obj.setScore(name, Math.max(0, Math.floor(Number(value) || 0)));
        return true;
    } catch (_) {
        return false;
    }
}

// ---------------------------------------------------------------------------
// 伤害
// ---------------------------------------------------------------------------

/** 命中部位：头 ×1.5 / 腿 ×0.7。逻辑与枪械包 pvpGun.js 的 hitZone 一致。 */
function hitZone(target, origin, direction) {
    const feet = target.location;
    const dir = normalized(direction);
    let entityHeight = 1.8;
    try {
        const head = typeof target.getHeadLocation === "function"
            ? target.getHeadLocation()
            : undefined;
        if (head && Number.isFinite(head.y - feet.y)) {
            entityHeight = Math.max(0.75, head.y - feet.y);
        }
    } catch (_) { /* ignore */ }

    const horizontalLength = dir.x * dir.x + dir.z * dir.z;
    let t;
    if (horizontalLength >= 0.0001) {
        t = Math.max(0, ((feet.x - origin.x) * dir.x + (feet.z - origin.z) * dir.z) / horizontalLength);
    } else {
        const centerY = feet.y + entityHeight * 0.5;
        t = Math.max(0, (centerY - origin.y) * dir.y);
    }
    const hitY = origin.y + dir.y * t;
    const relativeY = hitY - feet.y;
    const headStart = Math.max(0.35, entityHeight - 0.45);
    const legEnd = Math.max(0.25, entityHeight * 0.38);
    if (relativeY >= headStart) return "head";
    if (relativeY <= legEnd) return "legs";
    return "body";
}

/**
 * 结算伤害。
 *
 * 先走 applyDamage（这样引擎会派发受伤事件，EndStone 插件能收到"被谁打了"）。
 * 万一引擎对 SimulatedPlayer 不结算（实测有这可能），再用血量组件直接扣，
 * 保证"打中了就是掉了血"，不至于打了没反应。
 */
export function applyDamage(target, amount, shooter) {
    if (!target || amount <= 0) return 0;
    const hp = safe(() => target.getComponent("minecraft:health"));
    if (!hp) return 0;
    const before = Number(hp.currentValue);
    if (!Number.isFinite(before) || before <= 0) return 0;

    try {
        target.applyDamage(amount, {
            cause: EntityDamageCause.entityAttack,
            damagingEntity: shooter,
        });
    } catch (e) {
        console.warn(`${TAG} applyDamage 抛异常: ${e}`);
    }

    let after = Number(safe(() => target.getComponent("minecraft:health")?.currentValue));
    if (!Number.isFinite(after)) after = before;

    if (after >= before - 0.001) {
        // 引擎没结算（假人常见）——直接改血量兜底。
        try {
            hp.setCurrentValue(Math.max(0, before - amount));
            after = Number(hp.currentValue);
        } catch (e) {
            console.warn(`${TAG} 兜底扣血失败: ${e}`);
        }
    }
    return Math.max(0, before - after);
}

// ---------------------------------------------------------------------------
// 找敌人
// ---------------------------------------------------------------------------

/**
 * 找最近的敌人。真人从 world.getAllPlayers() 拿，假人从 sims 表拿
 * （脚本 API 里它们是 undefined，只能靠自己这张表）。
 */
export function findNearestEnemy(sim, enemies, sims) {
    const want = enemies instanceof Set ? enemies : new Set(enemies || []);
    if (want.size === 0) return null;
    const table = sims || simsRef;
    let best = null;
    let bestD = Infinity;

    try {
        for (const p of world.getAllPlayers()) {
            if (!p) continue;
            if (!want.has(p.name)) continue;
            if (!isAlive(p)) continue;          // 已经倒下的不用再打
            const d = dist(p.location, sim.location);
            if (d < bestD) {
                bestD = d;
                best = { entity: p, name: p.name, dist: d, isSim: false };
            }
        }
    } catch (_) { /* 列举失败就只比假人 */ }

    try {
        for (const [name, other] of table) {
            if (!other || name === sim.name) continue;
            if (other.isValid === false) continue;
            if (!want.has(name)) continue;
            if (!isAlive(other)) continue;      // 同上
            const d = dist(other.location, sim.location);
            if (d < bestD) {
                bestD = d;
                best = { entity: other, name, dist: d, isSim: true };
            }
        }
    } catch (_) { /* ignore */ }

    return best;
}

// ---------------------------------------------------------------------------
// 开火
// ---------------------------------------------------------------------------

/** 实体是不是还活着（血量 > 0）。 */
export function isAlive(entity) {
    if (!entity) return false;
    const hp = safe(() => entity.getComponent("minecraft:health")?.currentValue);
    if (hp === undefined) return true;      // 拿不到血量就不拦
    return Number(hp) > 0;
}

/**
 * 假人开一枪。
 *
 * 流程：查枪 → 查弹药 → 算射线 → 视线是否被挡 → 扣血 → 表现 → 扣弹药。
 * 返回 {ok, dmg, dealt, zone, err}。
 */
export function fireSimShot(sim, target, targetName) {
    if (!sim || !target) return { ok: false, err: "没有目标" };

    // 射手自己已经倒了就别再扣扳机（死了还在开火会刷一屏"实际扣血 0.0"）
    if (!isAlive(sim)) return { ok: false, err: "射手已阵亡" };
    // 目标已经倒了同样不用打
    if (!isAlive(target)) return { ok: false, err: "目标已阵亡" };

    const gunId = heldGunId(sim);
    if (!gunId) return { ok: false, err: "手里没枪" };

    const gunKey = gunKeyOf(gunId);
    const ammo = readAmmo(sim.name, gunKey);
    if (ammo !== undefined && ammo <= 0) {
        return { ok: false, err: "空仓" };
    }

    const eye = { x: sim.location.x, y: sim.location.y + EYE_HEIGHT, z: sim.location.z };
    const aim = { x: target.location.x, y: target.location.y + 1.2, z: target.location.z };
    const dir = normalized({ x: aim.x - eye.x, y: aim.y - eye.y, z: aim.z - eye.z });
    const range = Math.hypot(aim.x - eye.x, aim.y - eye.y, aim.z - eye.z);

    // 视线被方块挡住就不开火（不然就是隔墙打人）
    try {
        const block = sim.dimension.getBlockFromRay(eye, dir, {
            maxDistance: Math.min(range, MAX_RANGE),
        });
        if (block) return { ok: false, err: "视线被挡" };
    } catch (_) { /* 射线查询失败就当没挡 */ }

    const zone = hitZone(target, eye, dir);
    const base = SIM_GUN_DAMAGE[gunId] !== undefined ? SIM_GUN_DAMAGE[gunId] : DEFAULT_DAMAGE;
    const mult = zone === "head" ? HEADSHOT_MULTIPLIER : zone === "legs" ? LEGSHOT_MULTIPLIER : 1;
    const dmg = base * mult;

    const dealt = applyDamage(target, dmg, sim);

    // 表现：动画 + 枪声
    try {
        sim.playAnimation(`animation.${gunKey}.shoot`);
    } catch (_) { /* ignore */ }
    try {
        sim.playSound(`${gunKey}.shoot`, { volume: 1, pitch: 1 });
    } catch (_) { /* ignore */ }

    if (ammo !== undefined) writeAmmo(sim.name, gunKey, ammo - 1);

    logShotThrottled(
        `${sim.name}|${targetName}`,
        `${TAG} 假人 ${sim.name} 用 ${gunId} 射击 ${targetName}：${zone} 伤害 ${dmg.toFixed(1)}（实际扣血 ${dealt.toFixed(1)}）`,
    );

    return { ok: true, dmg, dealt, zone, gunId };
}

// ---------------------------------------------------------------------------
// 真人开枪 → 命中假人（几何判定）
// ---------------------------------------------------------------------------
//
// 为什么必须自己算
// --------------
// 枪械包的射线用的是 `dimension.getEntitiesFromRay()`，而在本机 BDS 1.26.52.3 上，
// 假人在实体列表里是 **null**（`getEntities({types:["minecraft:player"]})` 返回
// 全是 UNDEF 的数组）。也就是说**射线永远打不到假人**——枪械包那边再怎么改
// 都不会有伤害。
//
// 但"开了几枪、枪口朝哪"只有枪械包知道（射速、散布都在它那边），所以它每开一枪
// 广播一次 `arc_gun:shot`，这边用几何判定（射线到假人身体的距离）补上伤害。
// 只补**假人**目标，真人目标由枪械包自己结算，不会双倍伤害。

/**
 * 射线是否穿过假人的身体。
 *
 * 用「竖直圆柱」求交，不是「到身体中心的点距」——后者在真人平视射击时
 * 会因为眼睛比身体中心高 0.7 格而判定为没打中。
 *
 * 返回命中点的垂直比例（0=脚 1=头），没中返回 -1。
 */
function rayHitSim(origin, dir, sim) {
    let feet;
    try {
        feet = sim.location;
    } catch (_) {
        return -1;
    }
    const ox = origin.x, oz = origin.z;
    const dx = dir.x, dz = dir.z;
    const px = feet.x - ox, pz = feet.z - oz;
    const horizLen2 = dx * dx + dz * dz;

    let t;
    if (horizLen2 < 1e-6) {
        t = 0;                       // 几乎垂直射击：取正下方
    } else {
        t = (px * dx + pz * dz) / horizLen2;
    }
    if (t < 0.2 || t > MAX_RANGE) return -1;   // 0.2 格内算枪口自己，不算命中

    const hx = ox + dx * t;
    const hz = oz + dz * t;
    const dH = Math.hypot(feet.x - hx, feet.z - hz);
    if (dH > 0.45) return -1;        // 没擦到身体（玩家碰撞半径 ~0.3，留点余量）

    const hitY = origin.y + dir.y * t;
    if (hitY < feet.y - 0.2 || hitY > feet.y + 1.9) return -1;
    return Math.max(0, Math.min(1, (hitY - feet.y) / 1.8));
}

/**
 * 处理枪械包广播来的"开了一枪"：把命中的假人打掉血。
 * 返回被命中的假人名字（没命中返回 null）。
 */
export function handleShotEvent(data) {
    if (!data) return null;
    const dir = normalized({ x: Number(data.dx) || 0, y: Number(data.dy) || 0, z: Number(data.dz) || 0 });
    const origin = { x: Number(data.x) || 0, y: Number(data.y) || 0, z: Number(data.z) || 0 };
    const gunId = String(data.gun || "");
    const shooterName = String(data.by || "");
    if (!gunId) return null;

    // 射手：真人从 getAllPlayers 找，假人从 sims 表找
    let shooter;
    try {
        for (const p of world.getAllPlayers()) {
            if (p && p.name === shooterName) {
                shooter = p;
                break;
            }
        }
    } catch (_) { /* ignore */ }
    if (!shooter && shooterName) shooter = simsRef.get(shooterName) || undefined;

    const base = SIM_GUN_DAMAGE[gunId] !== undefined ? SIM_GUN_DAMAGE[gunId] : DEFAULT_DAMAGE;

    let hitName = null;
    let hitSim = null;
    let hitRatio = -1;
    for (const [name, sim] of simsRef) {
        if (!sim || sim.isValid === false) continue;
        if (name === shooterName) continue;    // 别打到自己
        if (!isAlive(sim)) continue;
        const ratio = rayHitSim(origin, dir, sim);
        if (ratio < 0) continue;
        if (hitRatio < 0 || ratio > 0) {      // 取更靠前/更高的那个
            hitRatio = ratio;
            hitName = name;
            hitSim = sim;
        }
    }
    if (!hitSim) return null;

    const zone = hitRatio >= 0.8 ? "head" : hitRatio <= 0.35 ? "legs" : "body";
    const mult = zone === "head" ? HEADSHOT_MULTIPLIER : zone === "legs" ? LEGSHOT_MULTIPLIER : 1;
    const dmg = base * mult;
    const dealt = applyDamage(hitSim, dmg, shooter);

    logShotThrottled(
        `${shooterName}|${hitName}`,
        `${TAG} ${shooterName} 开枪命中假人 ${hitName}：${zone} 伤害 ${dmg.toFixed(1)}（实际扣血 ${dealt.toFixed(1)}）`,
    );
    return hitName;
}

// ---------------------------------------------------------------------------
// 复活落点
// ---------------------------------------------------------------------------

/**
 * 盯位置突变。假人死一次会被引擎放回重生点——如果重生点不是我们设定的
 * 那个，它就会"莫名其妙"出现在地图另一头。这里把这种事抓出来并说清楚。
 *
 * ⚠️ 判据是"**上一拍血量是 0**"，不是"这一拍移动超过 8 格"。
 * 老写法用位移当判据，结果**从高处往下掉的每一秒都算一次"重生"**——
 * 无规则服假人生在 y=200 掉下去，一路上刷出十几条"阵亡后重生在 (…)"，
 * 真正的重生反而被淹没在这堆假货里。
 * 血量从 0 回到满，才是"死过一次又活了"的唯一特征。
 *
 * 返回 {loc, spawn} 表示"刚重生过"，否则 null。
 */
export function watchRespawn(name, sim) {
    let loc;
    try {
        loc = sim.location;
    } catch (_) {
        return null;
    }
    const hp = safe(() => sim.getComponent("minecraft:health")?.currentValue);
    const prev = lastPositions.get(name);
    const prevHp = lastHealth.get(name);
    lastPositions.set(name, { x: loc.x, y: loc.y, z: loc.z });
    lastHealth.set(name, hp);

    // 落地判定（与"是否重生"无关）：站稳之后把重生点钉在脚下。
    pinSpawnPointOnLanding(name, sim, loc);

    if (prev === null || prev === undefined) return null;
    const wasDead = prevHp !== undefined && prevHp !== null && prevHp <= 0;
    if (!wasDead) return null;   // 没死过 → 只是移动/坠落，不算重生

    const spawn = spawnPoints.get(name);
    console.log(
        `${TAG} 假人 ${name} 阵亡后重生在 (${loc.x.toFixed(1)}, ${loc.y.toFixed(1)}, ${loc.z.toFixed(1)})`
        + (spawn ? `；设定出生点是 (${spawn.x}, ${spawn.y}, ${spawn.z})` : "；没有设定出生点"),
    );
    return { loc, spawn };
}

// ---------------------------------------------------------------------------
// 接枪械包的"这一枪打在假人身上"事件
// ---------------------------------------------------------------------------

/** 在坐标附近找假人（枪械包只能给出命中点，给不出名字）。 */
function findSimNear(point, radius = 3.0) {
    let best = null;
    let bestD = radius;
    for (const [name, sim] of simsRef) {
        if (!sim) continue;
        if (sim.isValid === false) continue;
        let loc;
        try {
            loc = sim.location;
        } catch (_) {
            continue;
        }
        const d = Math.hypot(loc.x - point.x, loc.y - point.y, loc.z - point.z);
        if (d <= bestD) {
            bestD = d;
            best = { name, sim };
        }
    }
    return best;
}

// ⚠️ scriptEventReceive 是 **system 级**事件，不是 world 级。
// 写成 `world.afterEvents.scriptEventReceive` 会拿到 undefined，
// 模块顶层一抛错 → 整个 main.js 加载失败 → 行为包没有心跳 →
// 插件判定"行为包未连接" → 所有假人都上不了线（2026-10-30 实踩）。
//
// 所以这里整段包 try/catch：**这个订阅挂了最多是"真人打假人没补刀"，
// 绝不能再把整个行为包带走**（假人开火、复活监控都在这个文件里）。
try {
    system.afterEvents.scriptEventReceive.subscribe((event) => {
    if (!event) return;

    // 枪械包每开一枪广播一次 → 这里给命中的假人补伤害
    // （枪械包的射线看不见假人，只能靠几何判定）。
    if (event.id === "arc_gun:shot") {
        try {
            handleShotEvent(JSON.parse(event.message || "{}"));
        } catch (_) { /* ignore */ }
        return;
    }

    if (event.id !== "arc_gun:simhit") return;
    let data;
    try {
        data = JSON.parse(event.message || "{}");
    } catch (_) {
        return;
    }
    const now = Date.now();
    const hit = findSimNear({ x: Number(data.x) || 0, y: Number(data.y) || 0, z: Number(data.z) || 0 });
    if (!hit) {
        if (now - lastSimHitLog > 3000) {
            lastSimHitLog = now;
            console.warn(`${TAG} 收到 arc_gun:simhit，但附近没有假人（${data.x}, ${data.y}, ${data.z}）`);
        }
        return;
    }
    const dmg = Number(data.dmg) || 0;
    if (dmg <= 0) return;

    // 射手：真人从 getAllPlayers 找，假人从 sims 表找
    let shooter;
    try {
        for (const p of world.getAllPlayers()) {
            if (p && p.name === data.by) {
                shooter = p;
                break;
            }
        }
    } catch (_) { /* ignore */ }
    if (!shooter && data.by) shooter = simsRef.get(data.by) || undefined;

    const dealt = applyDamage(hit.sim, dmg, shooter);
    if (now - lastSimHitLog > 1000) {
        lastSimHitLog = now;
        console.log(`${TAG} 真人 ${data.by} 命中假人 ${hit.name}：伤害 ${dmg.toFixed(1)}（实际扣血 ${dealt.toFixed(1)}）`);
    }
    });
} catch (e) {
    console.warn(`${TAG} 枪械包事件订阅失败（真人打假人的补刀将不可用）：${e}`);
}
