"""真人开枪改**跟着枪械包的真实开火**走（v3.9.48）。

2026-10-31 23:13~23:14 枪战服实踩（用户原话："我按住右键扫射但是只有第一下可以打到假人"）：

    [23:13:29] [ArcFakePlayer] DEVILENMO 开了一枪（blazik:qbz95）但没打中假人
    [23:13:29] [GunDiag] itemStartUse by=DEVILENMO item=blazik:qbz95
    [23:13:31] [GunDiag] itemStopUse by=DEVILENMO item=blazik:qbz95
    [23:14:09] [ArcFakePlayer] DEVILENMO 开枪命中假人 ak47king：body 伤害 3.0

每次 itemStartUse 正好一条命中日志 ⇒ 按住右键期间后面那些枪**全没结算**。

根因：桥接包拿 `itemStartUse` 当"开了一枪"的触发，而枪械包那边
`itemStartUse` 只是"按下扳机"（按住只来一次），真正的连发节奏在它自己的
主循环里（每 tick 判断冷却/弹药，然后 `setPlayerGunAmmo(ammo - 1)`）。

改法：盯**枪械包自己的弹药计分板**（`{gunKey}Ammo`）——
它每真开一枪就减 1，掉了就是一枪。射速/换弹/空仓/半自动锁/RPM 倍率
全部自动跟着枪械包走，桥接包不再自己造一套开火节奏。

⚠️ 必须读**计分板**，不能读动态属性：
   动态属性在这台服上按行为包隔离——诊断包读真人玩家的 `firing` /
   `reloadTimer` / `lastSlotUsed` 全是 undefined，只有它自己写的
   `arc_diag_raw` 读得到。计分板才是世界级共享数据
   （EndStone 插件就用它给真人发弹："Set [qbz95Ammo] for DEVILENMO to 30"）。

本文件在 **dukpy 里真跑 arena_combat.js**，用一份可控的计分板 mock 驱动
`detectHumanShots()`，覆盖：
  · 弹药掉 1 → 结算一枪（真人按住扫射时每一枪都算数）
  · 弹药没动 → 不结算
  · 弹药涨了（换弹 / 插件补弹）→ 不结算
  · 一次掉很多（插件批量重置）→ 不结算
  · 一次掉 2（桥接包漏了一个 tick）→ 按两枪算伤害
  · peekAmmo 是只读的，不许凭空造计分板
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import dukpy

from tests.endstone_stub import install

install()

from tests.test_behavior_pack_imports import _strip_for_dukpy

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "endstone_arc_fake_player" / "behavior_pack" / "scripts"

#: 一份"这台服真的长什么样"的假模块：
#: 计分板里只有 qbz95Ammo，读数由 __ammo 控制（模拟枪械包每枪减 1）。
MOCK = r"""
const server = {
    world: {
        scoreboard: {
            getObjective: (id) => {
                if (id !== "qbz95Ammo") return undefined;
                return { getScore: (p) => (p === "Human" ? __ammo : undefined) };
            },
            addObjective: () => { __createdObjectives.push("qbz95Ammo"); return undefined; },
        },
        getAllPlayers: () => [undefined, __human],
        afterEvents: {
            itemStartUse: { subscribe: (fn) => { __onStartUse = fn; } },
            itemStopUse: { subscribe: (fn) => { __onStopUse = fn; } },
        },
    },
    system: {
        currentTick: 100,
        afterEvents: { scriptEventReceive: { subscribe() {} } },
        runInterval() {},
    },
    EntityDamageCause: { entityAttack: "entityAttack" },
};

let __ammo = 30;
let __createdObjectives = [];
let __onStartUse = null;
let __onStopUse = null;

function __mkHealth(v) {
    const h = { currentValue: v };
    h.setCurrentValue = (x) => { h.currentValue = x; };
    return h;
}
function __mkDim() {
    return { getBlock: () => ({ typeId: "minecraft:air" }), spawnParticle: () => {} };
}
function __mkEntity(pos, opts) {
    opts = opts || {};
    const health = __mkHealth(opts.hp === undefined ? 20 : opts.hp);
    return {
        name: opts.name,
        location: { x: pos.x, y: pos.y, z: pos.z },
        nameTag: opts.nameTag || "",
        dimension: opts.dimension || __mkDim(),
        selectedSlotIndex: 0,
        getHeadLocation: () => ({ x: pos.x, y: pos.y + (opts.height || 1.8), z: pos.z }),
        getComponent: (id) => {
            if (id === "minecraft:health") return health;
            if (id === "minecraft:inventory") {
                return { container: { getItem: () => (opts.gun ? { typeId: opts.gun } : undefined) } };
            }
            return undefined;
        },
        getViewDirection: () => opts.dir || { x: 0, y: 0, z: 1 },
        applyDamage: (amount) => { health.currentValue -= amount; },
        playAnimation: () => {},
        playSound: () => {},
        navigateToLocation: () => {},
        stopMoving: () => {},
        lookAtLocation: () => {},
        __health: health,
    };
}

// 真人站在 (0,64,0)，瞄假人胸口（假人在 (0,64,10)）→ 命中部位是 body
const __eye = { x: 0, y: 65.62, z: 0 };
function __aimAtChest() {
    const target = { x: 0, y: 65.2, z: 10 };
    const d = { x: target.x - __eye.x, y: target.y - __eye.y, z: target.z - __eye.z };
    const L = Math.hypot(d.x, d.y, d.z) || 1;
    return { x: d.x / L, y: d.y / L, z: d.z / L };
}
const __human = __mkEntity({ x: 0, y: 64, z: 0 },
    { name: "Human", gun: "blazik:qbz95", dir: __aimAtChest(), height: 1.62 });
const __sim = __mkEntity({ x: 0, y: 64, z: 10 }, { name: "Bot" });
"""

#: 挂在模块末尾的探针：驱动一轮轮"弹药变化"，把假人掉血记下来。
PROBE = r"""
;(() => {
    // simsRef 必须是 Map（main.js 里就是 sims.set(...) 那张表）
    initArenaCombat(new Map([["Bot", __sim]]));
    setSimEnemies("Bot", ["Human"]);        // 这个假人正在跟真人对枪

    const steps = [];
    function tick() { server.system.currentTick += 1; }
    function hp() { return __sim.__health.currentValue; }
    function step(label, ammo) {
        __ammo = ammo;
        tick();
        const before = hp();
        const shots = detectHumanShots();
        steps.push({
            label: label,
            ammo: ammo,
            shots: shots,
            dmg: +(before - hp()).toFixed(3),
            hp: hp(),
        });
    }

    // 第一次看到弹药 → 只建基线，不结算
    step("baseline", 30);
    // 枪械包真开了一枪 → 结算一枪
    step("one-shot", 29);
    step("one-shot-2", 28);
    // 换弹 / 插件补弹 → 弹药涨了，不结算
    step("reload", 30);
    // 插件批量重置弹药（一次掉 25）→ 不是连发，不结算
    step("bulk-reset", 5);
    // 桥接包漏了一个 tick，两枪一起掉 → 按两枪算
    step("two-shots", 3);
    // 换枪 → 基线重建，不结算
    step("switch-gun", 30);

    // peekAmmo 只读：不许凭空造计分板
    const peekUnknown = peekAmmo(__human, "m249");
    const created = __createdObjectives.length;

    return JSON.stringify({
        steps: steps,
        peekUnknown: peekUnknown === undefined ? "undefined" : String(peekUnknown),
        createdObjectives: created,
    });
})()
"""


def _run_probe() -> dict:
    src = (SCRIPTS_DIR / "arena_combat.js").read_text(encoding="utf-8")
    src = _strip_for_dukpy(src, MOCK)
    src = src + PROBE
    return json.loads(dukpy.evaljs(src))


class HumanShotFollowsGunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = _run_probe()
        cls.steps = {s["label"]: s for s in cls.probe["steps"]}

    def test_first_sight_only_establishes_baseline(self):
        """刚拿到读数时不能当成开了一枪（否则白挨一枪）。"""
        s = self.steps["baseline"]
        self.assertEqual(s["shots"], 0, "第一次看到弹药就结算 = 凭空多一枪")
        self.assertEqual(s["dmg"], 0.0)

    def test_one_ammo_drop_is_one_shot(self):
        """弹药掉 1 = 枪械包真开了一枪 = 结算一枪。"""
        s = self.steps["one-shot"]
        self.assertEqual(s["shots"], 1, "弹药掉 1 却没结算 —— 又是只有第一下")
        self.assertAlmostEqual(s["dmg"], 3.0, places=3, msg="qbz95 躯干伤害应为 3.0")

    def test_each_shot_in_a_burst_counts(self):
        """连发每一枪都要算数（这是这次修的核心）。"""
        s = self.steps["one-shot-2"]
        self.assertEqual(s["shots"], 1)
        self.assertAlmostEqual(s["dmg"], 3.0, places=3)

    def test_reload_does_not_count_as_shot(self):
        """换弹/插件补弹 → 弹药涨了，不是开火。"""
        s = self.steps["reload"]
        self.assertEqual(s["shots"], 0, "补弹被当成开火了")
        self.assertEqual(s["dmg"], 0.0)

    def test_bulk_ammo_reset_is_not_a_burst(self):
        """插件批量重置弹药（一次掉 25）不是连发，不能当 25 枪。"""
        s = self.steps["bulk-reset"]
        self.assertEqual(s["shots"], 0, "批量重置被当成了连发")
        self.assertEqual(s["dmg"], 0.0)

    def test_missed_tick_is_compensated(self):
        """桥接包漏了一个 tick（一次掉 2）→ 按两枪算伤害。"""
        s = self.steps["two-shots"]
        self.assertEqual(s["shots"], 2)
        self.assertAlmostEqual(s["dmg"], 6.0, places=3)

    def test_switching_gun_rebuilds_baseline(self):
        """换枪 → 基线重建，不结算。"""
        s = self.steps["switch-gun"]
        self.assertEqual(s["shots"], 0, "换枪时不该凭空结算")
        self.assertEqual(s["dmg"], 0.0)

    def test_peek_ammo_is_read_only(self):
        """peekAmmo 绝不能凭空造计分板（否则检测会污染世界）。"""
        self.assertEqual(self.probe["peekUnknown"], "undefined")
        self.assertEqual(
            self.probe["createdObjectives"], 0,
            "peekAmmo 建了计分板 —— 它必须是只读的",
        )


def main():
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(HumanShotFollowsGunTests))
    if not result.wasSuccessful():
        return 1
    print(f"全部 {result.testsRun} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
