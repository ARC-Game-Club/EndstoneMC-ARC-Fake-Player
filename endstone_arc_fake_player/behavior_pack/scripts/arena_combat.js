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

// ⚠️ 这里**必须**用命名空间导入，不能把 Vector3 写进下面那行 import 的花括号里。
//
// 这台服务器（MC 26.52）的 @minecraft/server **没有导出 Vector3 这个类**：
//   [Scripting] [arc_fake_player 假人桥接] SyntaxError: Could not find export
//   'Vector3' in module '@minecraft/server'
// 静态 import 在**链接期**就失败 → 整个行为包加载失败 → 心跳断 → Python 判
// "行为包未连接" → 假人一个都上不来（枪械包 arc_gun_diag 也是这么集体阵亡的）。
//
// 命名空间导入不会因为缺导出而炸（缺的只是 undefined），所以：**可选**的导出
// 一律走命名空间 + 运行时判断，别写进 import 列表。
import * as server from "@minecraft/server";

const world = server.world;
const system = server.system;
const EntityDamageCause = server.EntityDamageCause;

/**
 * Vector3 这个类在**有些**服务器上有、有些没有（原生类型一直都在，只是
 * 不一定导出到 JS 侧）。这里是 undefined 就说明这台服没有。
 */
const Vec3 = server.Vector3;

/**
 * 造一个能喂给原生 API 的坐标。
 *
 * 有 Vector3 类就造真的（原生 API 只认原生实例，传普通对象会抛
 * "Native type conversion failed ... expected type: Vector3"）；
 * 没有就退回普通对象——能用就用，不能用会抛，调用点都有 try/catch 兜着。
 */
function toVec(x, y, z) {
    if (Vec3) return new Vec3(x, y, z);
    return { x, y, z };
}

/**
 * 只在**能造出原生实例**时返回坐标，否则返回 null。
 *
 * 用途：像 getBlockFromRay 这种只认真生实例的 API，在没有 Vector3 类的
 * 服务器上根本没法调，就该老实跳过，而不是拿普通对象去撞。
 */
function nativeVec(x, y, z) {
    if (!Vec3) return null;
    return new Vec3(x, y, z);
}

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

/** 名字 → 它应该在哪儿重生（地面上）。引擎的 setSpawnPoint 对
 *  SimulatedPlayer 两种写法都不认，所以这个点由我们自己用，
 *  重生之后把假人传送过来（见 main.js 的 teleportAfterRespawn）。 */
const respawnPoints = new Map();

/** 名字 → 上一拍的位置，用来发现"死后重生"造成的位置突变。 */
const lastPositions = new Map();

/** 名字 → 上一拍的血量，用来区分"死过然后重生"和"只是从高处掉下去"。 */
const lastHealth = new Map();

/** 名字 → 落地判定状态：连续两次采样 Y 不变就算站稳了。 */
const landingState = new Map();

/** 名字集合：这些假人的重生点是**服务器配置**的，落地位置不许覆盖。 */
const anchoredBots = new Set();

/** 上一次广播 simhit 的时间，避免刷屏。 */
let lastSimHitLog = 0;

/** 最近结算过的射击：key = "射手@tick"，用来挡掉同一枪的重复广播。 */
const recentShots = new Set();

/**
 * 假人 → 它正在打的敌人名单。
 *
 * 谁在跟谁对枪，是 Python 侧下发的（`bot:arena` 带 enemies 名单）。
 * 这里留一份副本，是为了在这台服**取不到玩家视线方向**时还能判定命中：
 * 名单里有这个玩家，说明那个假人正举枪对着他。
 */
const simEnemies = new Map();

/** 记录某个假人当前的敌人名单。main.js 在 doArena 里调用。 */
export function setSimEnemies(simName, enemies) {
    const list = Array.isArray(enemies) ? enemies.map(String) : [];
    simEnemies.set(simName, new Set(list));
}

/** 正在跟这个玩家对枪的假人名字集合。 */
export function getSimsFighting(playerName) {
    const out = [];
    for (const [simName, enemies] of simEnemies) {
        if (enemies && enemies.has(playerName)) out.push(simName);
    }
    return out;
}

/** 射击日志限流：key = "射手|目标"，同一对每 N 毫秒最多记一条。 */
const shotLogAt = new Map();
const SHOT_LOG_INTERVAL_MS = 2000;

/** 枪口火焰失败只警告一次，别把日志刷爆。 */
let muzzleFlashWarned = false;

/** 是不是一把枪。枪械包的枪全在 `blazik:` 命名空间下（护甲/食物/方块是其它命名空间）。 */
export function isGunId(itemId) {
    return typeof itemId === "string" && itemId.startsWith("blazik:");
}

/**
 * 开火被拒的原因日志。
 *
 * 为什么要专门开一个：main.js 调 fireSimShot 时把返回值丢掉了，
 * 于是"视线被挡 / 手里不是枪 / 空仓"从来没进过日志——2026-11-01
 * 排查"隔墙穿人"时连"假人到底开没开火"都看不到，全靠猜。
 * 这里按 key 限流（每类一分钟内最多一条）。
 */
function logShotReject(key, message) {
    logShotThrottled(`reject|${key}`, message);
}

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
 * ⚠️ 这里**不能**用 `safe()` 包 getBlock——它把"抛异常"和"返回 undefined"
 * 混成同一个值，于是分不清两种完全不同的情况：
 *   · 抛异常 = 这一列的区块没加载 → **扫不了**，直接放弃；
 *   · 返回 undefined = 这一格是空气 → **继续往下找**。
 * 混起来的后果：假人明明站在地面上（脚下一格就是地板），扫描却在第一格
 * 空气上就放弃了 → 地面点找不到 → 重生点没地方设。
 */
function findGroundY(x, z, fromY) {
    const dim = safe(() => world.getDimension("overworld"));
    if (!dim) return null;
    const start = Math.min(fromY, 318);
    for (let y = start; y >= -64; y--) {
        let block;
        try {
            block = dim.getBlock({ x: x, y: y, z: z });
        } catch (e) {
            return null;   // 区块没加载 → 扫不了
        }
        if (!block) continue;            // 空气（或引擎没给）→ 继续往下
        if (block.isAir || block.isLiquid) continue;
        const id = String(block.typeId || "").toLowerCase();
        if (NOT_STANDABLE.some((bad) => id.endsWith(bad))) continue;
        return y + 1;
    }
    return null;
}

/**
 * 设引擎重生点（**尽力而为**，真正的保障在 respawn 后的那次传送）。
 *
 * ⚠️ **本机 BDS 上这条路是走不通的**，两种写法都失败：
 *
 *   · `setSpawnPoint(loc)`（新引擎签名，dimension 参数已移除）
 *     → 引擎内部要取实体的 dimension，而 SimulatedPlayer **不暴露** dimension
 *       → 抛 "Interface property ['dimension'] expected type: Dimension"
 *   · `setSpawnPoint(loc, dim)`（老引擎签名）
 *     → 抛 "Incorrect number of arguments to function. Expected 0-1, received 2"
 *
 * 所以重生点这条路只能当"锦上添花"。**真正管用的是 `respawnPoints` + 重生后传送**
 * （见 main.js 的 autoRespawn / teleportAfterRespawn）。
 *
 * 这里把两种错误都打出来——否则只看得到最后那个"参数个数"错误，
 * 会误以为是写法不对，而实际上单参数写法也过不去。
 */
function applySpawnPoint(sim, point) {
    if (!sim || typeof sim.setSpawnPoint !== "function") return false;
    const loc = { x: point.x, y: point.y, z: point.z };
    try {
        sim.setSpawnPoint(loc);
        return true;
    } catch (e1) {
        console.warn(`${TAG} setSpawnPoint 单参数写法失败: ${e1}`);
        try {
            const dim = safe(() => world.getDimension("overworld"));
            sim.setSpawnPoint(loc, dim);
            return true;
        } catch (e2) {
            console.warn(`${TAG} setSpawnPoint 双参数写法也失败: ${e2}`);
            return false;
        }
    }
}

/**
 * 记录假人的出生点，并算出它该在哪儿重生。
 *
 * ⚠️ 重生点必须落在**地面上**，不能直接用"我们把它生成在哪"。
 * 生成点经常在半空（Python 侧扫不到地表 → 退回 y=200），
 * 拿半空当重生点 = 死后原地再摔一次。
 *
 * `anchor` 是 Python 侧带来的**服务器配置出生点**（弧光核心 /spawn 那个
 * 实坐标，Y 配死）。有它就用它——那是服主定的地方，假人死了就回那儿，
 * 和真人按 /spawn 回出生点是一个道理。没有它才退回"脚下扫地面 +
 * 落地后再钉一次"那套兜底。
 */
export function rememberSpawnPoint(name, sim, loc, anchor) {
    const point = { x: Math.floor(loc.x), y: Math.floor(loc.y), z: Math.floor(loc.z) };
    spawnPoints.set(name, point);
    lastPositions.set(name, null);
    landingState.delete(name);
    if (anchor) {
        const fixed = {
            x: Math.floor(anchor.x), y: Math.floor(anchor.y), z: Math.floor(anchor.z),
        };
        respawnPoints.set(name, fixed);
        anchoredBots.add(name);
        applySpawnPoint(sim, fixed);
        console.log(
            `${TAG} 假人 ${name} 出生于 (${point.x}, ${point.y}, ${point.z})；`
            + `重生点用服务器出生点 (${fixed.x}, ${fixed.y}, ${fixed.z})`,
        );
        return;
    }
    // 假人已经站在这一格上了，所以这一列的区块**一定是加载的**——
    // 这正是 Python 侧那次扫描失败（区块没加载）时缺的那一环。
    const groundY = findGroundY(point.x, point.z, point.y);
    const respawn = groundY === null
        ? point
        : { x: point.x, y: groundY, z: point.z };
    respawnPoints.set(name, respawn);
    applySpawnPoint(sim, respawn);
    console.log(
        `${TAG} 假人 ${name} 出生于 (${point.x}, ${point.y}, ${point.z})；`
        + `地面重生点 (${respawn.x}, ${respawn.y}, ${respawn.z})`
        + (groundY === null ? "（脚下没扫到地面，先按生成点算）" : ""),
    );
}

/**
 * 这个假人应该在哪儿重生。
 *
 * main.js 的 autoRespawn 用它：引擎把假人放回**它自己的**重生点，
 * 而那个点我们设不上（见 applySpawnPoint），于是引擎退回世界出生点——
 * 高度是 level.dat 里的自动值（无规则服 32767），可能落在半空，
 * 落地即摔死，接着再重生，死循环。所以我们自己把它挪到这里。
 */
export function getRespawnPoint(name) {
    return respawnPoints.get(name) || null;
}

/** 外部修正某假人的重生点（teleportAfterRespawn 验证后写回用）。 */
export function setRespawnPoint(name, point) {
    respawnPoints.set(name, point);
}

/**
 * 把"记录在案的重生点"验一遍：脚下必须是实的、可站的方块。
 *
 * 为什么要验：记录点有三个来源——服务器配置（弧光核心）、生成时的地面
 * 扫描、落地后钉的点。任何一个都可能"记下的时候是对的，用的时候不对了"：
 * 配置点的 Y 本身配在半空、扫描那一列后来被挖空。直接传送过去 = 传进
 * 半空 = 死后摔一次——正是"复活了就摔死"死循环的来源。
 *
 * 返回验证过的落点（脚下不实时沿这一列向下修正到真地面）；返回 null
 * 表示这一列验证不了（区块没加载 / 整列没有可站方块），调用方自己兜底。
 */
export function verifyGroundPoint(point) {
    const dim = safe(() => world.getDimension("overworld"));
    if (!dim) return null;
    let below;
    try {
        below = dim.getBlock({ x: point.x, y: point.y - 1, z: point.z });
    } catch (e) {
        return null;   // 区块没加载 → 验证不了
    }
    if (below && !below.isAir && !below.isLiquid) {
        const id = String(below.typeId || "").toLowerCase();
        if (!NOT_STANDABLE.some((bad) => id.endsWith(bad))) {
            return point;   // 脚下就是实地 → 原样可用
        }
    }
    // 脚下不是实地 → 沿这一列向下找真地面（从 point.y+8 起扫，容忍记录偏差）
    const groundY = findGroundY(point.x, point.z, point.y + 8);
    if (groundY === null) return null;
    return { x: point.x, y: groundY, z: point.z };
}

/**
 * 假人被移除时清掉它的记录。
 *
 * 不清的话，每次角色轮换下线都会在四张表里留下一条——
 * 假人每 20~180 分钟换一轮，几天下来表就撑大了。
 */
export function forgetBot(name) {
    spawnPoints.delete(name);
    respawnPoints.delete(name);
    lastPositions.delete(name);
    lastHealth.delete(name);
    landingState.delete(name);
    anchoredBots.delete(name);
}

/**
 * 落地后把重生点钉在脚下。
 *
 * 为什么还要钉第二遍：生成那一刻的地面扫描也可能失败（区块还没加载完）。
 * 等它**真的站住了**（连续两次采样 Y 不变），脚下那块地一定是实的——
 * 这时候再更新一次，之后它每次重生都会回到这块地上。
 *
 * ⚠️ 但如果重生点是**服务器配置的那个**（服主用 /spawn 定的地方），
 * 就不许被"落地位置"覆盖——那是服主定的规矩，假人死了就回那儿，
 * 和真人按 /spawn 回出生点是一个道理。
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
    if (anchoredBots.has(name)) return;   // 配置的点说了算，不覆盖
    const x = Math.floor(loc.x);
    const z = Math.floor(loc.z);
    const groundY = findGroundY(x, z, Math.floor(loc.y));
    const respawn = groundY === null
        ? { x: x, y: Math.floor(loc.y), z: z }
        : { x: x, y: groundY, z: z };
    respawnPoints.set(name, respawn);
    applySpawnPoint(sim, respawn);
    console.log(`${TAG} 假人 ${name} 已落地，重生点钉在 (${respawn.x}, ${respawn.y}, ${respawn.z})`);
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

export function gunKeyOf(gunId) {
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
/**
 * 只读弹药（**计分板**），不建计分板。
 *
 * 为什么读计分板而不是动态属性
 * ----------------------------
 * 枪械包把权威弹药写在两处：动态属性 `{gunKey}Ammo`（它自己读自己写）
 * 和计分板 `{gunKey}Ammo`（跨组件共享）。
 * 而**动态属性在这台服上是按行为包隔离的**：诊断包读真人玩家的
 * `firing` / `reloadTimer` / `lastSlotUsed` 全是 undefined，
 * 只有它自己写的 `arc_diag_raw` 读得到。
 * 桥接包在别的包里，也读不到枪械包写的动态属性。
 * 计分板是世界级共享数据（EndStone 插件就用它给真人发弹），只能盯它。
 *
 * readAmmo → ammoObjective 会在计分板不存在时 addObjective，
 * 那会凭空造出一堆空计分板，所以检测"枪械包有没有在管这颗枪的弹药"
 * 只能用这个只读版本。
 *
 * @param {object} player 真人实体
 * @param {string} gunKey  枪短名（qbz95）
 * @returns {number|undefined} undefined = 没人写过（不是 0）
 */
export function peekAmmo(player, gunKey) {
    let obj = null;
    try {
        obj = world.scoreboard.getObjective(`${gunKey}Ammo`);
    } catch (_) {
        return undefined;
    }
    if (!obj) return undefined;
    // 参与者候选：名字 → 身份。名字最通用（EndStone 插件就是按名字写的）。
    const candidates = [];
    try { if (player?.name) candidates.push(player.name); } catch (_) { }
    try { if (player?.scoreboardIdentity) candidates.push(player.scoreboardIdentity); } catch (_) { }
    for (const c of candidates) {
        try {
            const v = obj.getScore(c);
            if (v === undefined || v === null) continue;
            return Number(v);
        } catch (_) { /* 试下一个 */ }
    }
    return undefined;
}

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
    // ⚠ 这里必须按**身高比例**分档，不能用绝对高度。
    //
    // 原来的写法：头 = 身高 - 0.45。而实测身高（getHeadLocation）是 1.62
    // → 爆头线 1.17；可瞄点固定在胸口 +1.2 → 1.2 > 1.17
    // → **每一枪都判爆头**，伤害 ×1.5。
    // 2026-10-31 实测：m4 每枪都是 "head 伤害 4.5"（基础 3.0 × 1.5），
    // 真人 20 血挨 5 枪就倒，而假人 0.2 秒一枪 → "复活即死"。
    //
    // 改成比例：头 = 顶部 18%，腿 = 底部 32%，中间是躯干。
    // 瞄胸口（1.2/1.62 = 0.74）→ body ✓；瞄头（≥0.82）→ head ✓。
    const rel = (hitY - feet.y) / entityHeight;
    if (rel >= 0.82) return "head";
    if (rel <= 0.32) return "legs";
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

// ---------------------------------------------------------------------------
// 交战判定：射程 / 视线 / 无敌
// ---------------------------------------------------------------------------

/** 假人开火的最大交战距离（格）。超出这个距离**只追，不打**。
 *
 *  为什么必须有：之前完全没有距离上限，假人隔着半个图都能打到真人——
 *  2026-10-31 实测真人"人都没看到就死了"。
 */
const SIM_MAX_ENGAGE = 26;

/** 真人开枪打假人的最大距离（格）。 */
const HUMAN_MAX_ENGAGE = 48;

/** 枪战插件的无敌 nameTag 前缀。
 *
 *  语言文件 NAME_TAG_INVINCIBLE = "§6[无敌状态]"，无敌时名字牌会带上它
 *  （见枪战插件 _team_name_tag）。
 *
 *  为什么桥接包要自己判一次：桥接包的 applyDamage 走的是 **JS API**，
 *  **绕过**了枪战插件的伤害闸门（Python 侧 ActorDamageEvent）——
 *  所以出生保护/准备阶段的无敌对它无效，真人会在"无敌状态"下被打死。
 */
const INVINCIBLE_NAME_TAG = "[无敌状态]";

/** 这个目标现在是不是无敌（出生保护 / 开局准备）。 */
function isInvincible(entity) {
    try {
        const tag = entity?.nameTag;
        if (typeof tag !== "string" || tag.length === 0) return false;
        return tag.indexOf(INVINCIBLE_NAME_TAG) >= 0;
    } catch (_) {
        return false;
    }
}

/**
 * **不**挡视线的方块（其余一律默认挡）。
 *
 * 为什么从"白名单"翻成"黑名单"
 * ----------------------------
 * 老写法把"会挡视线的方块"列成 70 多项的白名单，不在这张表里的 = 透明。
 * 结果枪战地图的墙只要不是那几十种之一（自定义 RP 方块 aplok:*、各种
 * 带颜色前缀的 concrete、抛光石、深板岩……），整面墙就透明 —— 假人
 * "隔着墙打人"。而且白名单里 `minecraft:concrete` 本身就不是合法 id
 * （真实 id 是 `minecraft:black_concrete` 这种），那一条从没匹配上。
 *
 * 反过来列"放行"的才可靠：**没见过的方块默认挡**，自定义方块自然也算墙。
 */
const NON_BLOCKING = new Set([
    "minecraft:air", "minecraft:cave_air", "minecraft:void_air",
    "minecraft:water", "minecraft:flowing_water",
    "minecraft:lava", "minecraft:flowing_lava",
    // 花草植被、作物、树苗
    "minecraft:short_grass", "minecraft:tall_grass", "minecraft:fern",
    "minecraft:large_fern", "minecraft:deadbush", "minecraft:seagrass",
    "minecraft:kelp", "minecraft:sugar_cane", "minecraft:wheat",
    "minecraft:carrots", "minecraft:potatoes", "minecraft:beetroots",
    "minecraft:torchflower", "minecraft:pitcher_plant",
    // 花
    "minecraft:dandelion", "minecraft:poppy", "minecraft:blue_orchid",
    "minecraft:allium", "minecraft:azure_bluet", "minecraft:red_tulip",
    "minecraft:orange_tulip", "minecraft:white_tulip", "minecraft:pink_tulip",
    "minecraft:oxeye_daisy", "minecraft:cornflower", "minecraft:lily_of_the_valley",
    // 火把/火/灯笼/线绳类小物件
    "minecraft:torch", "minecraft:soul_torch", "minecraft:wall_torch",
    "minecraft:fire", "minecraft:soul_fire", "minecraft:lantern",
    "minecraft:tripwire", "minecraft:tripwire_hook", "minecraft:cobweb",
    "minecraft:string",
    // 按钮/拉杆/压力板/告示牌/旗帜
    "minecraft:lever", "minecraft:stone_button", "minecraft:oak_button",
    "minecraft:stone_pressure_plate", "minecraft:oak_pressure_plate",
    "minecraft:light_weighted_pressure_plate",
    "minecraft:heavy_weighted_pressure_plate",
    "minecraft:oak_sign", "minecraft:oak_wall_sign", "minecraft:oak_hanging_sign",
    // 地毯/雪片/铁轨/脚手架/藤蔓
    "minecraft:oak_carpet", "minecraft:white_carpet", "minecraft:black_carpet",
    "minecraft:snow_layer", "minecraft:rail", "minecraft:golden_rail",
    "minecraft:detector_rail", "minecraft:activator_rail", "minecraft:scaffolding",
    "minecraft:vine", "minecraft:glow_lichen",
    // 一片草/花/水的杂项
    "minecraft:short_dry_grass", "minecraft:tall_dry_grass", "minecraft:bush",
]);

/** 上一次判定的阻挡方块 id（日志用）。 */
let losLastBlocker = "";
/** getBlock 不可用的告警只打一次。 */
let losWarned = false;

/**
 * 射线到 maxDist 之间有没有方块挡着。
 *
 * 返回 true = 视线通畅。注意**不再有"永久关闭"**：getBlock 抛异常只影响
 * 当次（放行 + 告警一次），不会让整局退化成"不挡枪"。
 */
/**
 * 让假人手里握着枪：选中槽不是枪就在快捷栏里找一把切过去。
 *
 * 返回手里的枪 id（找不到返回 null）。
 * 为什么要这么做：假人入场后背包被清、只发护甲，选中槽常常停在护甲上，
 * 于是"手里拿的不是枪"→ 不开火，玩家看到的是假人站着不还手。
 * 切选中槽失败（引擎不认）也不报错，退回"不开火"。
 */
function ensureHoldingGun(sim) {
    const held = heldGunId(sim);
    if (isGunId(held)) return held;
    let container = null;
    try {
        container = sim.getComponent("minecraft:inventory")?.container;
    } catch (_) {
        container = null;
    }
    if (!container) return null;
    const size = Math.min(9, Number(container.size) || 9);
    for (let slot = 0; slot < size; slot++) {
        let item = null;
        try {
            item = container.getItem(slot);
        } catch (_) {
            item = null;
        }
        if (!item || !isGunId(item.typeId)) continue;
        try {
            sim.selectedSlotIndex = slot;
        } catch (_) {
            return null;      // 切不过去就别打了（总比拿头盔当枪强）
        }
        logShotThrottled(`${sim.name}|takegun`,
            `${TAG} 假人 ${sim.name} 切到第 ${slot} 格的 ${item.typeId}`);
        return item.typeId;
    }
    return null;
}

function hasLineOfSight(dim, origin, dir, maxDist) {
    losLastBlocker = "";
    if (!dim) return true;
    // 有原生 Vector3 时优先用引擎射线（最准，直接给出命中的方块）
    const rayOrigin = nativeVec(origin.x, origin.y, origin.z);
    if (rayOrigin) {
        try {
            const hit = dim.getBlockFromRay(
                rayOrigin, new Vec3(dir.x, dir.y, dir.z),
                { maxDistance: Math.min(maxDist, MAX_RANGE) });
            if (hit) {
                losLastBlocker = (hit.block && hit.block.typeId) || "未知方块";
                return false;
            }
            return true;
        } catch (_) { /* 落回步进判定 */ }
    }
    // 步进判定：跳过射手身边和目标身边这两个"贴脸"区间
    const startAt = 1.0;
    const stopAt = Math.max(startAt, maxDist - 0.7);
    try {
        const step = 0.5;
        for (let d = startAt; d < stopAt; d += step) {
            const block = dim.getBlock({
                x: origin.x + dir.x * d,
                y: origin.y + dir.y * d,
                z: origin.z + dir.z * d,
            });
            if (!block) continue;
            const typeId = block.typeId;
            if (!typeId) continue;
            if (NON_BLOCKING.has(typeId)) continue;
            losLastBlocker = typeId;
            return false;
        }
        return true;
    } catch (e) {
        // 只影响当次：放行并告警一次，绝不永久关闭这个判定
        if (!losWarned) {
            losWarned = true;
            console.warn(`${TAG} 视线判定出错（getBlock），本次放行: ${e}`);
        }
        return true;
    }
}

/**
 * 假人开一枪。
 *
 * 流程：查枪 → 查弹药 → 射程 → 视线 → 算部位 → 扣血 → 表现 → 扣弹药。
 * 返回 {ok, dmg, dealt, zone, err}。
 */
export function fireSimShot(sim, target, targetName) {
    if (!sim || !target) return { ok: false, err: "没有目标" };

    // 射手自己已经倒了就别再扣扳机（死了还在开火会刷一屏"实际扣血 0.0"）
    if (!isAlive(sim)) return { ok: false, err: "射手已阵亡" };
    // 目标已经倒了同样不用打
    if (!isAlive(target)) return { ok: false, err: "目标已阵亡" };
    // 目标无敌中（出生保护 / 开局准备）→ 不打
    if (isInvincible(target)) return { ok: false, err: "目标无敌中" };

    // 手里必须是**枪**。
    //
    // 2026-10-31 实踩：入场时背包被清、只发护甲，模拟玩家的选中槽停在护甲上，
    // 于是日志里出现"假人 ak47king 用 minecraft:diamond_helmet 射击 DEVILENMO"
    // ——拿头盔当枪打人，还照 DEFAULT_DAMAGE 扣血。
    // 现在：不是枪就自动在快捷栏里找一把切过去，实在没有才不开火。
    const gunId = ensureHoldingGun(sim);
    if (!gunId) {
        logShotReject(`${sim.name}|nogun`,
            `${TAG} 假人 ${sim.name} 快捷栏里没有枪，不开火`);
        return { ok: false, err: "手里没枪" };
    }

    const gunKey = gunKeyOf(gunId);
    const ammo = readAmmo(sim.name, gunKey);
    if (ammo !== undefined && ammo <= 0) {
        return { ok: false, err: "空仓" };
    }

    const eye = { x: sim.location.x, y: sim.location.y + EYE_HEIGHT, z: sim.location.z };
    const aim = { x: target.location.x, y: target.location.y + 1.2, z: target.location.z };
    const dir = normalized({ x: aim.x - eye.x, y: aim.y - eye.y, z: aim.z - eye.z });
    const range = Math.hypot(aim.x - eye.x, aim.y - eye.y, aim.z - eye.z);

    // 射程上限：超出就不开火（只追）。没有这条，假人隔着半个图都能打到
    // 真人——2026-10-31 实测真人"人都没看到就死了"。
    if (range > SIM_MAX_ENGAGE) {
        return { ok: false, err: `超出射程 ${range.toFixed(0)}` };
    }

    // 视线被方块挡住就不开火（不然就是隔墙打人）
    if (!hasLineOfSight(sim.dimension, eye, dir, range)) {
        logShotReject(`${sim.name}|los`,
            `${TAG} 假人 ${sim.name} 看不见 ${targetName}：被 ${losLastBlocker || "方块"}`
            + ` 挡住（距离 ${range.toFixed(1)}），没开火`);
        return { ok: false, err: "视线被挡" };
    }

    const zone = hitZone(target, eye, dir);
    const base = SIM_GUN_DAMAGE[gunId] !== undefined ? SIM_GUN_DAMAGE[gunId] : DEFAULT_DAMAGE;
    const mult = zone === "head" ? HEADSHOT_MULTIPLIER : zone === "legs" ? LEGSHOT_MULTIPLIER : 1;
    const dmg = base * mult;

    const dealt = applyDamage(target, dmg, sim);

    // 表现：后坐力动画 + 枪声 + 枪口火焰
    try {
        sim.playAnimation(`animation.${gunKey}.shoot`);
    } catch (e) {
        console.warn(`${TAG} 假人 ${sim.name} 的射击动画播不了: ${e}`);
    }
    try {
        sim.playSound(`${gunKey}.shoot`, { volume: 1, pitch: 1 });
    } catch (e) {
        console.warn(`${TAG} 假人 ${sim.name} 的枪声播不了: ${e}`);
    }
    spawnMuzzleFlash(sim, eye, dir);

    if (ammo !== undefined) writeAmmo(sim.name, gunKey, ammo - 1);

    logShotThrottled(
        `${sim.name}|${targetName}`,
        `${TAG} 假人 ${sim.name} 用 ${gunId} 射击 ${targetName}：${zone} 伤害 ${dmg.toFixed(1)}（实际扣血 ${dealt.toFixed(1)}）距离 ${range.toFixed(1)}`,
    );

    return { ok: true, dmg, dealt, zone, gunId };
}

/**
 * 枪口火焰。
 *
 * 为什么自己生成粒子，不靠 RP 动画里那份：
 * RP（失落枪械档案馆弧光重制版 RP）把枪口火焰写在 `animation.<枪>.shoot` 的
 * `particle_effects` 里，要靠 `playAnimation` 才播得出来；但**模拟玩家**
 * （SimulatedPlayer）身上 playAnimation 经常不生效——用户反馈"假人开枪没有特效"。
 * 所以这里用引擎的 spawnParticle 直接补一份。
 *
 * 粒子 ID 取自本 RP 里真实存在的定义
 * （resource_packs/失落枪械档案馆弧光重制版 RP/particles/guns/*.particle.json）：
 *   nova:muzzleflash  枪口火焰
 *   nova:flash        亮光
 *   nova:sparkles     火星
 *   nova:bullet_smoke 枪口烟
 */
function spawnMuzzleFlash(sim, eye, dir) {
    try {
        const dim = sim.dimension;
        const muzzle = toVec(
            eye.x + dir.x * 0.6,
            eye.y + dir.y * 0.6,
            eye.z + dir.z * 0.6,
        );
        const smoke = toVec(
            eye.x + dir.x * 1.0,
            eye.y + dir.y * 1.0,
            eye.z + dir.z * 1.0,
        );
        dim.spawnParticle("nova:muzzleflash", muzzle);
        dim.spawnParticle("nova:flash", muzzle);
        dim.spawnParticle("nova:sparkles", muzzle);
        dim.spawnParticle("nova:bullet_smoke", smoke);
    } catch (e) {
        // 这台服没有 Vector3 类时坐标只能是普通对象，spawnParticle 可能不收。
        // 每枪都报一遍会把日志刷爆，所以只报第一次。
        if (!muzzleFlashWarned) {
            muzzleFlashWarned = true;
            console.warn(`${TAG} 枪口火焰生成失败（只报一次）: ${e}`);
        }
    }
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
 * 没有视线方向时的兜底：找出正在跟这个玩家对枪、且离得最近的假人。
 *
 * 判据来自 Python 下发的敌人名单（见 setSimEnemies）：
 * 名单里有这个玩家 ⇒ 这个假人正举枪对着他。
 * 距离上限 40 格（比射线判定的 48 略小）。
 */
function findEngagedSim(shooterName, origin) {
    let best = null;
    let bestD = Infinity;
    for (const [simName, enemies] of simEnemies) {
        if (!enemies || !enemies.has(shooterName)) continue;
        const sim = simsRef.get(simName);
        if (!sim || sim.isValid === false) continue;
        if (!isAlive(sim)) continue;
        let d;
        try {
            const loc = sim.location;
            d = Math.hypot(loc.x - origin.x, loc.z - origin.z);
        } catch (_) {
            continue;
        }
        if (d > 40) continue;
        if (d < bestD) {
            bestD = d;
            best = simName;
        }
    }
    return best;
}

/**
 * 真人扣扳机 → 直接把命中的假人打掉血。
 *
 * 为什么在这里直接结算，而不是等枪械包广播事件
 * --------------------------------------------
 * 这台服（MC 26.52）的 @minecraft/server **没有 world.scriptEvent 这个 API**
 * （实测 `typeof world.scriptEvent === "undefined"`，诊断包 arc_gun_diag 的
 * apiProbe 有日志）→ **JS 行为包之间发不了脚本事件**。
 * 桥接包平时收到的 bot:* 事件是 **Python 插件用 Endstone 的 broadcast_event** 发的，
 * 不是 JS 发的。所以"枪械包广播 arc_gun:shot → 桥接包收"这条链在这台服上
 * 从一开始就不成立（广播调用每次都抛异常被吞，日志里 arc_gun:shot 广播数恒为 0）。
 *
 * 而 **itemStartUse 每个行为包都收得到**（诊断包一直在收），并且
 * **getViewDirection() 在这台服是好的**（apiProbe 拿到了完整方向向量）。
 * 所以：真人扣扳机这件事，桥接包自己就能看到，直接结算，不用绕事件。
 *
 * 命中判定用的是 rayHitSim（竖直圆柱求交，纯数学，不依赖引擎的射线 API）。
 */
export function handleHumanShot(player, gunId, shotCount = 1) {
    if (!player || !gunId) return null;
    // 一次检测到多枪（桥接包漏了 tick）时按枪数放大伤害，
    // 而不是重复调用（同一 tick 会被 recentShots 去重挡掉）。
    const shots = Math.max(1, Math.floor(Number(shotCount) || 1));

    let origin;
    try {
        const loc = player.location;
        origin = { x: loc.x, y: loc.y + EYE_HEIGHT, z: loc.z };
    } catch (_) {
        return null;
    }

    let dir = null;
    try {
        const d = player.getViewDirection();
        if (d && Number.isFinite(d.x) && Number.isFinite(d.y) && Number.isFinite(d.z)) {
            dir = normalized({ x: d.x, y: d.y, z: d.z });
        }
    } catch (_) { /* 取不到方向就不结算 */ }
    if (!dir) return null;

    const shooterName = player.name ?? "";
    const base = SIM_GUN_DAMAGE[gunId] !== undefined ? SIM_GUN_DAMAGE[gunId] : DEFAULT_DAMAGE;

    // 同一 tick、同一射手只结算一次（防重复广播 + 防连点算多枪）
    const shotKey = `${shooterName}@${system.currentTick}`;
    if (recentShots.has(shotKey)) return null;
    if (recentShots.size > 256) recentShots.clear();
    recentShots.add(shotKey);

    // 只打**正在跟这个真人对枪**的假人——别打友军。
    //
    // simEnemies 是"假人 → 它的敌人名单"这张表，反查（getSimsFighting）
    // 就是"哪些假人正把真人当敌人"。等待阶段 Python 侧根本不下发名单
    // （队伍在进房那刻就分好了，但那时没在打），所以这里天然不会
    // 误伤等待室里的人。
    const fighting = new Set(getSimsFighting(shooterName));
    if (fighting.size === 0) {
        // 这一枪不会结算。最常见的原因：Python 侧的 bot:arena 没把
        // 这个真人下发成"敌人"（等待阶段不下发，或对局状态不是 playing）。
        // 不记日志的话，表现就是"我打他完全没反应"，而且查不出来。
        logShotThrottled(`nofight|${shooterName}`,
            `${TAG} ${shooterName} 开了一枪（${gunId}），但没有假人把他当敌人，这一枪不结算`);
        return null;
    }

    let hitName = null;
    let hitSim = null;
    let hitRatio = -1;
    for (const [name, sim] of simsRef) {
        if (!sim || sim.isValid === false) continue;
        if (name === shooterName) continue;    // 别打到自己
        if (!fighting.has(name)) continue;      // 只打敌人，不打友军
        if (!isAlive(sim)) continue;
        const ratio = rayHitSim(origin, dir, sim);
        if (ratio < 0) continue;
        if (hitRatio < 0 || ratio > 0) {      // 取更靠前/更高的那个
            hitRatio = ratio;
            hitName = name;
            hitSim = sim;
        }
    }
    if (!hitSim) {
        logShotThrottled(
            `miss|${shooterName}`,
            `${TAG} ${shooterName} 开了一枪（${gunId}）但没打中假人`,
        );
        return null;
    }

    // 目标无敌中（刚复活/准备阶段）→ 不扣血
    if (isInvincible(hitSim)) {
        logShotThrottled(
            `inv|${shooterName}`,
            `${TAG} ${shooterName} 开枪命中假人 ${hitName}，但它无敌中（没扣血）`,
        );
        return null;
    }

    // 射程上限 + 视线遮挡（和假人开枪同一套规矩）
    const hitDist = Math.hypot(
        hitSim.location.x - origin.x,
        hitSim.location.y + 0.9 - origin.y,
        hitSim.location.z - origin.z,
    );
    if (hitDist > HUMAN_MAX_ENGAGE) {
        logShotThrottled(`far|${shooterName}`,
            `${TAG} ${shooterName} 命中 ${hitName} 但超出真人射程`
            + `（${hitDist.toFixed(1)} > ${HUMAN_MAX_ENGAGE}），没扣血`);
        return null;
    }
    if (!hasLineOfSight(hitSim.dimension, origin, dir, hitDist)) {
        logShotThrottled(`los|${shooterName}`,
            `${TAG} ${shooterName} 打 ${hitName} 的射线被 ${losLastBlocker || "方块"}`
            + ` 挡住（距离 ${hitDist.toFixed(1)}），没扣血`);
        return null;
    }

    const zone = hitRatio >= 0.8 ? "head" : hitRatio <= 0.35 ? "legs" : "body";
    const mult = zone === "head" ? HEADSHOT_MULTIPLIER : zone === "legs" ? LEGSHOT_MULTIPLIER : 1;
    const dmg = base * mult * shots;
    const dealt = applyDamage(hitSim, dmg, player);

    logShotThrottled(
        `${shooterName}|${hitName}`,
        `${TAG} ${shooterName} 开枪命中假人 ${hitName}：${zone} 伤害 ${dmg.toFixed(1)}（实际扣血 ${dealt.toFixed(1)}）${shots > 1 ? " ×" + shots + "发" : ""}`,
    );
    return hitName;
}

/**
 * 处理枪械包广播来的"开了一枪"：把命中的假人打掉血。
 * 返回被命中的假人名字（没命中返回 null）。
 *
 * 注：这条路在这台服上**收不到东西**（world.scriptEvent 不存在，JS 发不出事件），
 * 留着是为了别的服务器（API 完整时）也能用。
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

    // 同一个 tick、同一个射手，只结算一次（和 handleHumanShot 共用这张表）。
    //
    // 为什么需要：真人开枪的广播可能有两个来源——
    //   ① 枪械包自己主循环里的开火分支；
    //   ② 我们在枪械包 `system/gunPerformance.js` 里按 itemStartUse 补的广播
    //      （主循环目前对真人是死的，事件一次都没发过）。
    // 两边都发的话，同一枪会被算成两枪，假人血量掉得飞快。
    const shotKey = `${shooterName}@${system.currentTick}`;
    if (recentShots.has(shotKey)) return null;
    if (recentShots.size > 256) recentShots.clear();
    recentShots.add(shotKey);

    let hitName = null;
    let hitSim = null;
    let hitRatio = -1;
    let hitByEngagement = false;
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
    if (!hitSim && Number(data.nodir || 0) === 1) {
        // 这台服取不到玩家的视线方向（@minecraft/server 不导出 Vector3，
        // getViewDirection 也没值）→ 射线判定用不了。
        // 兜底：**看"谁在跟这个玩家对枪"**——对局里的假人被下发了敌人名单，
        // 名单里有他，说明它正举枪对着他；玩家这时开枪，打中的就是它。
        const engaged = findEngagedSim(shooterName, origin);
        if (engaged) {
            hitSim = simsRef.get(engaged) || null;
            hitName = engaged;
            hitRatio = 0.5;                 // 算躯干伤害
            hitByEngagement = true;
        }
    }

    if (!hitSim) {
        // 打偏了也要留痕：这样能区分"广播根本没来"和"广播来了但没打中"。
        logShotThrottled(
            `miss|${shooterName}`,
            `${TAG} ${shooterName} 开了一枪（${gunId}）但没打中假人`,
        );
        return null;
    }

    const zone = hitRatio >= 0.8 ? "head" : hitRatio <= 0.35 ? "legs" : "body";
    const mult = zone === "head" ? HEADSHOT_MULTIPLIER : zone === "legs" ? LEGSHOT_MULTIPLIER : 1;
    const dmg = base * mult;
    const dealt = applyDamage(hitSim, dmg, shooter);

    logShotThrottled(
        `${shooterName}|${hitName}`,
        `${TAG} ${shooterName} 开枪命中假人 ${hitName}：${zone} 伤害 ${dmg.toFixed(1)}（实际扣血 ${dealt.toFixed(1)}）`
        + (hitByEngagement ? "（按对枪关系判定：这台服取不到视线方向）" : ""),
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
// ---------------------------------------------------------------------------
// 真人开火检测：**跟着枪械包的真实开火走**
// ---------------------------------------------------------------------------
//
// 为什么不再用 itemStartUse 当触发
// --------------------------------
// 枪械包自己的开火模型：
//     itemStartUse → setDynamicProperty("firing", 1)
//     主循环每 tick：冷却到了 && 弹药>0 && !换弹 →
//         setPlayerGunAmmo(ammo-1)   ← 弹药减 1（计分板 + 动态属性都写）
//         播动画/枪声 → fireBullet × 弹丸数
//     itemStopUse → 清 firing
//
// 也就是说 **itemStartUse 只是"按下扳机"**，按住右键只来一次；
// 真正的连发节奏是枪械包主循环按射速跑的。
// 桥接包以前拿 itemStartUse 当开火触发 → 按住扫射时**只结算一枪**
// （用户原话："我按住右键扫射但是只有第一下可以打到假人"）。
//
// 现在改成盯**枪械包自己的弹药计分板**：它每真开一枪就减 1，
// 掉了就是一枪。于是射速、换弹、空仓、半自动锁、RPM 倍率
// 全部自动跟着枪械包走——桥接包不再自己造一套开火节奏，
// 只负责枪械包做不到的那一步：沿射线找到假人并扣血
// （这台服上枪械包看不见模拟玩家：getEntitiesFromRay 里假人是 undefined）。
//
// 兜底：枪械包完全没写这颗枪的弹药时，退回"按住扳机期间按固定节奏打"。

/** 真人 → 上次看到的弹药（名字 → {key, value}）。 */
const humanAmmoWatch = new Map();
/** 真人 → 按住扳机的状态（名字 → {gunId, nextTick}）。 */
const humanHold = new Map();
/** 兜底节奏：5 次/秒（只有枪械包完全没管弹药时才用）。 */
const HUMAN_HOLD_INTERVAL = 4;
/** 一次最多结算几枪：再多就是插件批量重置弹药，不是连发。 */
const HUMAN_MAX_SHOTS_PER_TICK = 5;

try {
    world.afterEvents.itemStartUse.subscribe((event) => {
        try {
            const player = event?.source;
            const gunId = event?.itemStack?.typeId;
            if (!player || !gunId) return;
            if (!gunId.startsWith("blazik:")) return;    // 只管枪械包的枪
            if (simsRef.has(player.name ?? "")) return;   // 假人自己开枪不算
            humanHold.set(player.name, { gunId, nextTick: system.currentTick });
        } catch (_) { /* 绝不能把整个行为包带走 */ }
    });
} catch (_) {
    // 订阅失败最多是"真人打假人没伤害"，不能连累假人开火/复活监控
}

try {
    world.afterEvents.itemStopUse.subscribe((event) => {
        try {
            const player = event?.source;
            if (player) humanHold.delete(player.name);
        } catch (_) { }
    });
} catch (_) { }

/**
 * 每 tick 调一次：看真人的枪械包有没有开火，有就把命中的假人扣血。
 *
 * @returns {number} 本轮结算了几枪
 */
export function detectHumanShots() {
    let players = [];
    try {
        players = world.getAllPlayers();
    } catch (_) {
        return 0;
    }
    if (!Array.isArray(players)) return 0;
    let shots = 0;
    for (const p of players) {
        if (!p) continue;                 // 假人在这个数组里是 undefined
        let name = "";
        try { name = p.name ?? ""; } catch (_) { continue; }
        if (!name) continue;
        if (simsRef.has(name)) continue;   // 假人自己开枪不算
        let gunId = null;
        try { gunId = heldGunId(p); } catch (_) { gunId = null; }
        if (!gunId || !gunId.startsWith("blazik:")) {
            humanAmmoWatch.delete(name);
            humanHold.delete(name);
            continue;
        }
        const key = gunKeyOf(gunId);
        const ammo = peekAmmo(p, key);
        if (ammo !== undefined) {
            // 主路径：弹药掉了 = 枪械包真开了一枪
            humanHold.delete(name);
            const prev = humanAmmoWatch.get(name);
            humanAmmoWatch.set(name, { key, value: ammo });
            if (!prev || prev.key !== key) continue;   // 刚拿到基线 / 换了枪
            const drop = prev.value - ammo;
            if (drop <= 0) continue;                  // 没开火，或刚补弹/换枪
            if (drop > HUMAN_MAX_SHOTS_PER_TICK) {
                // 一次掉太多 = 插件重置弹药（EndStone 写 default_ammo），不是连发
                continue;
            }
            handleHumanShot(p, gunId, drop);
            shots += drop;
            continue;
        }
        // 兜底：枪械包没管这颗枪的弹药 → 按住扳机期间按固定节奏打
        const hold = humanHold.get(name);
        if (!hold) continue;
        if (hold.gunId !== gunId) {
            humanHold.set(name, { gunId, nextTick: system.currentTick });
            continue;
        }
        const now = system.currentTick;
        if (now < hold.nextTick) continue;
        hold.nextTick = now + HUMAN_HOLD_INTERVAL;
        handleHumanShot(p, gunId, 1);
        shots += 1;
    }
    return shots;
}

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
