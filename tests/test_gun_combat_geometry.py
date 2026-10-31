"""假人开火的交火判定：命中部位 / 射程 / 视线遮挡 / 无敌状态。

2026-10-31 21:09 枪战服实踩（用户原话："我人都没看到就一直死呢，甚至是无敌时间"）：

    21:09:31 [ArcFakePlayer] 假人 zhaohuazi 用 blazik:m4 射击 DEVILENMO：head 伤害 4.5（实际扣血 4.5）
    21:09:32 DEVILENMO died

四个原因都在这里测，而且是在 **dukpy 里真跑 arena_combat.js**（不是静态检查）：

1. **命中部位算错**——原来按**绝对高度**分档（头 = 身高 - 0.45 = 1.17），
   而瞄点固定在胸口 +1.2 → 1.2 > 1.17 → **每一枪都判爆头**（伤害 ×1.5）。
   m4 基础 3.0 × 1.5 = 4.5，和日志里的 4.5 完全对上 → 20 血挨 5 枪就倒，
   假人 0.2 秒一枪 → "复活即死"。
2. **没有射程上限** → 隔着半个图也能打中（"人都看不到"）。
3. **视线遮挡判定被跳过**——这台服不导出 Vector3 → getBlockFromRay 用不了。
4. **无敌状态对桥接包无效**——applyDamage 走 JS API，绕过了枪战插件的
   ActorDamageEvent 闸门，所以出生保护/准备阶段拦不住。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import dukpy

from tests.endstone_stub import install

install()

from tests.test_behavior_pack_imports import (
    MOCK_WITHOUT_VECTOR3,
    _strip_for_dukpy,
)

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "endstone_arc_fake_player" / "behavior_pack" / "scripts"

#: 一整段在 dukpy 里跑的探针：造出假人/真人模型，把各种开火场景跑一遍。
PROBE = r"""
;(() => {
    function mkHealth(v) {
        const h = { currentValue: v };
        h.setCurrentValue = (x) => { h.currentValue = x; };
        return h;
    }
    function mkDim(blockType) {
        return {
            getBlock: (p) => {
                if (blockType === "throw") throw new Error("no plain object");
                return { typeId: blockType || "minecraft:air" };
            },
            spawnParticle: () => {},
        };
    }
    function mkEntity(pos, opts) {
        opts = opts || {};
        const health = mkHealth(opts.hp === undefined ? 20 : opts.hp);
        return {
            location: { x: pos.x, y: pos.y, z: pos.z },
            nameTag: opts.nameTag || "",
            dimension: opts.dimension || mkDim(),
            selectedSlotIndex: 0,
            getHeadLocation: () => ({ x: pos.x, y: pos.y + (opts.height || 1.62), z: pos.z }),
            getComponent: (id) => {
                if (id === "minecraft:health") return health;
                if (id === "minecraft:inventory") {
                    return { container: { getItem: () => (opts.gun ? { typeId: opts.gun } : undefined) } };
                }
                return undefined;
            },
            applyDamage: (amount) => { health.currentValue -= amount; },
            playAnimation: () => {},
            playSound: () => {},
            navigateToLocation: () => {},
            stopMoving: () => {},
            lookAtLocation: () => {},
        };
    }
    function aimDir(eye, target, dy) {
        const aim = { x: target.x, y: target.y + dy, z: target.z };
        const d = { x: aim.x - eye.x, y: aim.y - eye.y, z: aim.z - eye.z };
        const L = Math.hypot(d.x, d.y, d.z) || 1;
        return { x: d.x / L, y: d.y / L, z: d.z / L };
    }

    // ---------- 1. 命中部位 ----------
    const feet = { x: 10, y: 64, z: 10 };
    const tgt = mkEntity(feet, {});
    const eye = { x: feet.x, y: feet.y + 1.62, z: feet.z - 10 };
    const zones = {
        chest: hitZone(tgt, eye, aimDir(eye, feet, 1.2)),
        head: hitZone(tgt, eye, aimDir(eye, feet, 1.55)),
        legs: hitZone(tgt, eye, aimDir(eye, feet, 0.3)),
    };

    // ---------- 2~4. 开火 ----------
    const sim = mkEntity({ x: 0, y: 64, z: 0 }, { gun: "blazik:m4" });
    const near = mkEntity({ x: 0, y: 64, z: 20 }, {});
    const far = mkEntity({ x: 0, y: 64, z: 40 }, {});
    const invincible = mkEntity({ x: 0, y: 64, z: 20 },
        { nameTag: "§6[无敌状态]\n§fTarget" });
    const nearAfter = fireSimShot(sim, near, "near");
    const farShot = fireSimShot(sim, far, "far");
    const invShot = fireSimShot(sim, invincible, "inv");

    // ---------- 3. 视线遮挡 ----------
    //
    // 顺序是刻意的：**先用会抛异常的维度打一枪**，再测石墙。
    // 老实现里 getBlock 一抛异常就把视线判定**永久**关掉（退回"不挡枪"），
    // 于是石墙也挡不住了 —— 这里正好把它当回归测试。
    const brokenSim = mkEntity({ x: 0, y: 64, z: 0 },
        { gun: "blazik:m4", dimension: mkDim("throw") });
    const brokenTarget = mkEntity({ x: 0, y: 64, z: 20 }, {});
    const brokenShot = fireSimShot(brokenSim, brokenTarget, "broken");

    const wallSim = mkEntity({ x: 0, y: 64, z: 0 },
        { gun: "blazik:m4", dimension: mkDim("minecraft:stone") });
    const wallTarget = mkEntity({ x: 0, y: 64, z: 20 }, {});
    const wallShot = fireSimShot(wallSim, wallTarget, "wall");

    // 自定义 RP 方块（枪战地图的墙很可能就是这类）——
    // 白名单时代它不在表里 = 透明 = 隔墙打人。
    const customSim = mkEntity({ x: 0, y: 64, z: 0 },
        { gun: "blazik:m4", dimension: mkDim("aplok:arena_wall") });
    const customTarget = mkEntity({ x: 0, y: 64, z: 20 }, {});
    const customShot = fireSimShot(customSim, customTarget, "custom");

    // 花草不挡枪（否则在草地上永远开不了火）
    const grassSim = mkEntity({ x: 0, y: 64, z: 0 },
        { gun: "blazik:m4", dimension: mkDim("minecraft:short_grass") });
    const grassTarget = mkEntity({ x: 0, y: 64, z: 20 }, {});
    const grassShot = fireSimShot(grassSim, grassTarget, "grass");

    // ---------- 4. 手里不是枪 ----------
    // 日志实证：假人曾"用 minecraft:diamond_helmet 射击 DEVILENMO"。
    const helmetSim = mkEntity({ x: 0, y: 64, z: 0 },
        { gun: "minecraft:diamond_helmet" });
    const helmetTarget = mkEntity({ x: 0, y: 64, z: 20 }, {});
    const helmetShot = fireSimShot(helmetSim, helmetTarget, "helmet");

    return JSON.stringify({
        zones: zones,
        near: { ok: nearAfter.ok, zone: nearAfter.zone, dmg: nearAfter.dmg, err: nearAfter.err || "" },
        far: { ok: farShot.ok, err: farShot.err || "" },
        inv: { ok: invShot.ok, err: invShot.err || "" },
        wall: { ok: wallShot.ok, err: wallShot.err || "" },
        broken: { ok: brokenShot.ok, err: brokenShot.err || "" },
        custom: { ok: customShot.ok, err: customShot.err || "" },
        grass: { ok: grassShot.ok, err: grassShot.err || "" },
        helmet: { ok: helmetShot.ok, err: helmetShot.err || "" },
    });
})()
"""


def _run_probe() -> dict:
    src = (SCRIPTS_DIR / "arena_combat.js").read_text(encoding="utf-8")
    src = _strip_for_dukpy(src, MOCK_WITHOUT_VECTOR3)
    src = src + PROBE
    return json.loads(dukpy.evaljs(src))


class GunCombatGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = _run_probe()

    # ---------- 命中部位 ----------

    def test_chest_aim_is_not_headshot(self):
        """瞄胸口必须是 body —— 原来永远判 head，伤害 ×1.5。"""
        self.assertEqual(
            self.probe["zones"]["chest"], "body",
            "瞄胸口被判成爆头：爆头线用绝对高度(身高-0.45=1.17)，"
            "而瞄点是胸口+1.2 → 每枪都 ×1.5 伤害，真人 5 枪就倒",
        )

    def test_head_aim_is_headshot(self):
        self.assertEqual(self.probe["zones"]["head"], "head")

    def test_leg_aim_is_legs(self):
        self.assertEqual(self.probe["zones"]["legs"], "legs")

    def test_body_damage_is_not_multiplied(self):
        """m4 基础伤害 3.0；判成爆头会变成 4.5。"""
        self.assertTrue(self.probe["near"]["ok"])
        self.assertEqual(self.probe["near"]["zone"], "body")
        self.assertAlmostEqual(
            self.probe["near"]["dmg"], 3.0, places=3,
            msg="躯干伤害不该被爆头倍率放大",
        )

    # ---------- 射程 ----------

    def test_shot_beyond_range_is_refused(self):
        self.assertFalse(self.probe["far"]["ok"], "超出射程也开火 = 隔着半个图杀人")
        self.assertIn("射程", self.probe["far"]["err"])

    def test_shot_within_range_is_allowed(self):
        self.assertTrue(self.probe["near"]["ok"])

    # ---------- 无敌 ----------

    def test_invincible_target_is_not_damaged(self):
        self.assertFalse(self.probe["inv"]["ok"], "无敌状态还能被打死")
        self.assertIn("无敌", self.probe["inv"]["err"])

    # ---------- 视线遮挡 ----------

    def test_wall_blocks_the_shot(self):
        self.assertFalse(self.probe["wall"]["ok"], "隔墙也能打中")
        self.assertIn("视线", self.probe["wall"]["err"])

    def test_broken_getblock_does_not_break_the_shot(self):
        """getBlock 不认普通对象时，这一次要放行，而不是不开枪。"""
        self.assertTrue(
            self.probe["broken"]["ok"],
            "视线判定用不了时应该退回'不挡枪'，不能连累假人不开枪",
        )

    def test_los_is_not_permanently_disabled_after_a_throw(self):
        """抛过一次异常之后，石墙**仍然要挡**。

        老实现里 `losCheckUsable = false` 是永久的 —— 一次异常就让整局
        退化成"任何墙都不挡"，这就是"还是隔着墙穿我"的直接来源。
        （探针里故意先跑抛异常的那一枪，再跑石墙。）
        """
        self.assertFalse(
            self.probe["wall"]["ok"],
            "getBlock 抛过一次异常之后石墙就挡不住了 —— 视线判定被永久关闭",
        )

    def test_custom_pack_block_blocks_the_shot(self):
        """自定义 RP 方块（aplok:*）也要挡。

        白名单时代它不在 70 多项的表里 = 透明 = 隔墙打人。
        枪战地图的墙完全可能就是这类方块。
        """
        self.assertFalse(
            self.probe["custom"]["ok"],
            "自定义方块没挡住视线 —— 白名单漏掉的方块全都是透明的，"
            "枪战地图只要不用原版石块就等于整面墙透明",
        )
        self.assertIn("视线", self.probe["custom"]["err"])

    def test_grass_does_not_block_the_shot(self):
        """花草不能挡枪，否则草地上永远开不了火（黑名单的边界）。"""
        self.assertTrue(self.probe["grass"]["ok"], "被草挡住不开枪 = 假人永远不还手")

    # ---------- 手里不是枪 ----------

    def test_non_gun_item_does_not_fire(self):
        """拿头盔不能当枪打。

        日志实证（2026-10-31 23:17）：`假人 ak47king 用 minecraft:diamond_helmet
        射击 DEVILENMO`。入场背包被清、只发护甲，模拟玩家选中槽停在护甲上，
        于是拿头盔照 DEFAULT_DAMAGE 扣血。
        """
        self.assertFalse(
            self.probe["helmet"]["ok"],
            "手里拿的是护甲，却当枪打人了",
        )
        self.assertIn("枪", self.probe["helmet"]["err"])


class ArenaLiveStateTests(unittest.TestCase):
    def test_only_playing_state_allows_firing(self):
        """准备阶段(buying)是"无敌且无法移动"，那段时间不许开火。"""
        from endstone_arc_fake_player.plugin import ArcFakePlayerPlugin
        plugin = ArcFakePlayerPlugin()
        self.assertEqual(
            tuple(plugin._arena_live_states), ("playing",),
            "开火状态必须只认 playing——buying 阶段全员无敌",
        )


def main():
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(GunCombatGeometryTests))
    result2 = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ArenaLiveStateTests))
    if not (result.wasSuccessful() and result2.wasSuccessful()):
        return 1
    print(f"全部 {result.testsRun + result2.testsRun} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
