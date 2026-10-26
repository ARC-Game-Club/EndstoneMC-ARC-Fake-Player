"""怪物反击反射的回归测试（零游戏依赖）。

为什么单独一个文件
------------------
这一段代码曾经把**整个服**连带崩掉，而且崩法很隐蔽：

    2026-10-26 无规则服连崩 5 次，栈都是
        Exception code: 0xc0000005 (EXCEPTION_ACCESS_VIOLATION)
        #9  endstone::Actor::`vcall'{296}'      ← 读 is_valid 的虚函数
        #27 endstone::core::EndstoneScheduler::mainThreadHeartbeat

原因链条：

1. 伤害事件回调拿到的 attacker 是引擎的**借用指针**；
2. 旧版把它**缓存 120 秒**在 ``_mob_targets`` 里；
3. 怪物一死（或者箭落地、区块卸载），引擎就释放那块 C++ 内存；
4. 反射层下一个 tick 去读缓存句柄的 ``is_valid``——那在 Endstone 里
   是个**虚函数**，等于在已释放内存上取虚表 → 访问违规；
5. Python 的 try/except 对访问违规完全无效 → 整个 BDS 进程死。

所以这里锁死三条规矩：

* 注册表里**不许出现 Actor**（只有标量）；
* 坐标靠 runtime id **重新解析**，解析出来的 Actor 当场用完就丢；
* 反击要锁定**射手**，不是那支箭（追箭是追不上的）。

另外顺手锁住"反击打不中要有日志"——旧版攻击不挂回调，失败是静默的，
所以"命中 0 次"这件事在日志里一点痕迹都没有。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Endstone 的桩必须在任何 endstone_arc_fake_player 导入之前装好。
from tests.endstone_stub import install as _install_stub  # noqa: E402

_install_stub()

import endstone_arc_fake_player.plugin as pm  # noqa: E402


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class QuietLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.infos: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def _rec(self, msg: str) -> None:
        self.lines.append(str(msg))

    def debug(self, msg: str) -> None:
        self._rec(msg)

    def info(self, msg: str) -> None:
        self._rec(msg)
        self.infos.append(str(msg))

    def warning(self, msg: str) -> None:
        self._rec(msg)
        self.warnings.append(str(msg))

    def error(self, msg: str) -> None:
        self._rec(msg)
        self.errors.append(str(msg))


class FakeLoc:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x, self.y, self.z = x, y, z


class FakeMob:
    """怪物替身。

    ⚠ **故意没有 is_valid 属性**：旧实现会去读它，而那正是崩溃点。
    新的实现根本不碰怪物的任何属性（除了当场取一次坐标）。
    """

    def __init__(self, runtime_id: int, name: str,
                 pos: tuple[float, float, float] = (0.0, 64.0, 0.0)) -> None:
        self.runtime_id = runtime_id
        self.name = name
        self.location = FakeLoc(*pos)

    def move(self, pos: tuple[float, float, float]) -> None:
        self.location = FakeLoc(*pos)


class FakeBot:
    """假人（受害者）替身。xuid 为空 = 不是真人。"""

    def __init__(self, name: str = "Bot") -> None:
        self.name = name
        self.xuid = ""
        self.runtime_id = 1


class FakeLevel:
    def __init__(self) -> None:
        self.actors: list[object] = []

    def add(self, actor: object) -> None:
        self.actors.append(actor)

    def drop(self, actor: object) -> None:
        if actor in self.actors:
            self.actors.remove(actor)


class FakeServer:
    def __init__(self) -> None:
        self.level = FakeLevel()


class FakeExecutor:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def cancel_all(self, bot: str) -> int:
        self.cancelled.append(bot)
        return 1


class FakeReflex:
    def __init__(self) -> None:
        self.engaged: list[tuple] = []

    def engage(self, bot: str, target: str, **kw) -> None:
        self.engaged.append((bot, target, kw))


class FakeBridge:
    """桥接替身：记录攻击，并可以把回调立刻打回去。"""

    def __init__(self, *, ok: bool = True, err: str = "") -> None:
        self.attacks: list[tuple[str, str]] = []
        self.ok = ok
        self.err = err

    def attack(self, name: str, target: str, callback=None) -> int:
        self.attacks.append((name, target))
        if callback is not None:
            callback(self.ok, self.err, {})
        return 1


class FakeDamageSource:
    def __init__(self, actor=None, damaging_actor=None) -> None:
        self.actor = actor
        self.damaging_actor = damaging_actor


class FakeDamageEvent:
    def __init__(self, actor, damage_source, damage: float = 4.0) -> None:
        self.actor = actor
        self.damage_source = damage_source
        self.damage = damage


class Thin:
    """只挂被测方法的薄壳。

    真插件是个巨大的类，直接构造不现实（要 endstone 运行时）。
    这里把要测的那几个方法**搬**过来，依赖用替身接上——
    和 test_v2_fixes.py 里的 Thin 同一个路子。
    """

    def __init__(self) -> None:
        self.logger = QuietLogger()
        self.server = FakeServer()
        self._agents = {"Bot": FakeBot()}
        self._mob_targets: dict = {}
        self._executor = FakeExecutor()
        self._reflex = FakeReflex()
        self._bridge = FakeBridge()
        self.events: list[tuple[str, str]] = []
        self.social_cfg = {"enabled": True, "mob_reflex_enabled": True,
                           "reflex_duration_seconds": 90.0}

    # -- 依赖 --
    def _social_cfg(self) -> dict:
        return self.social_cfg

    def _reflex_event(self, bot: str, text: str) -> None:
        self.events.append((bot, text))

    def _social_on_attacked(self, bot, name, attacker, damage) -> None:
        self.player_attacks.append((bot, name, attacker, damage))

    # -- 被测方法（真实现的搬运） --
    def _mob_on_attacked(self, bot, mob_name, attacker, damage):
        return pm.ArcFakePlayerPlugin._mob_on_attacked(
            self, bot, mob_name, attacker, damage)

    def _mob_pos(self, key):
        return pm.ArcFakePlayerPlugin._mob_pos(self, key)

    def _resolve_mob_pos(self, runtime_id):
        return pm.ArcFakePlayerPlugin._resolve_mob_pos(self, runtime_id)

    def _attack_target(self, target):
        return pm.ArcFakePlayerPlugin._attack_target(self, target)

    def _reflex_attack(self, bot, target):
        return pm.ArcFakePlayerPlugin._reflex_attack(self, bot, target)

    def on_actor_damage(self, event):
        return pm.ArcFakePlayerPlugin.on_actor_damage(self, event)

    # 静态方法要用 staticmethod 包一层，否则通过实例访问会凭空多一个 self
    _actor_pos = staticmethod(pm.ArcFakePlayerPlugin._actor_pos)


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

class MobTargetRegistryTests(unittest.TestCase):
    """注册表里不许出现 Actor 句柄（崩服根因）。"""

    def setUp(self) -> None:
        self.thin = Thin()
        self.mob = FakeMob(682, "Zombie", (10.0, 64.0, -3.0))
        self.thin.server.level.add(self.mob)

    def test_registry_holds_only_scalars(self):
        self.thin._mob_on_attacked("Bot", "Zombie", self.mob, 4.0)
        self.assertEqual(len(self.thin._mob_targets), 1)
        st = next(iter(self.thin._mob_targets.values()))
        # 注册表里的每一个值都不能是那个 Actor 本体
        for value in vars(st).values():
            self.assertIsNot(value, self.mob)
        # 而且字段集合是封闭的（多一个字段就多一个可能藏句柄的地方）
        self.assertEqual(
            set(vars(st)),
            {"runtime_id", "name", "pos", "expire_at", "resolved_at"})

    def test_mob_pos_returns_last_known_position(self):
        self.thin._mob_on_attacked("Bot", "Zombie", self.mob, 4.0)
        pos = self.thin._mob_pos("682")
        self.assertEqual(pos, (10.0, 64.0, -3.0))

    def test_mob_pos_refreshes_by_runtime_id(self):
        self.thin._mob_on_attacked("Bot", "Zombie", self.mob, 4.0)
        # 怪物动了，且世界里的实体也换了位置
        self.mob.move((20.0, 64.0, -3.0))
        # 还没到刷新间隔 → 用的还是旧坐标
        self.assertEqual(self.thin._mob_pos("682"), (10.0, 64.0, -3.0))
        # 强制到期 → 重新按 runtime id 解析
        st = next(iter(self.thin._mob_targets.values()))
        st.resolved_at = time.monotonic() - 10.0
        self.assertEqual(self.thin._mob_pos("682"), (20.0, 64.0, -3.0))

    def test_mob_pos_drops_target_when_mob_is_gone(self):
        """怪物死了（引擎回收了它）→ 目标消失，绝不碰野指针。"""
        self.thin._mob_on_attacked("Bot", "Zombie", self.mob, 4.0)
        self.thin.server.level.drop(self.mob)       # 引擎把它回收了
        st = next(iter(self.thin._mob_targets.values()))
        st.resolved_at = time.monotonic() - 10.0    # 强制重新解析
        self.assertIsNone(self.thin._mob_pos("682"))
        self.assertEqual(self.thin._mob_targets, {})   # 顺手清掉

    def test_mob_pos_expires_after_ttl(self):
        self.thin._mob_on_attacked("Bot", "Zombie", self.mob, 4.0)
        st = next(iter(self.thin._mob_targets.values()))
        st.expire_at = time.monotonic() - 1.0
        self.assertIsNone(self.thin._mob_pos("682"))
        self.assertEqual(self.thin._mob_targets, {})

    def test_attack_target_uses_mob_display_name(self):
        """JS 侧按名字认目标：id 认不出来，名字认得出来。"""
        self.thin._mob_on_attacked("Bot", "Skeleton", self.mob, 4.0)
        self.assertEqual(self.thin._attack_target("682"), "Skeleton")
        # 真人目标原样传名字
        self.assertEqual(self.thin._attack_target("Steve"), "Steve")


class DamageSourcePickingTests(unittest.TestCase):
    """反击要锁定射手，不是那支箭。"""

    def setUp(self) -> None:
        self.thin = Thin()

    def test_projectile_damage_targets_the_shooter(self):
        arrow = FakeMob(900, "Arrow", (1.0, 64.0, 1.0))
        skeleton = FakeMob(682, "Skeleton", (12.0, 64.0, -5.0))
        self.thin.server.level.add(skeleton)
        ev = FakeDamageEvent(
            self.thin._agents["Bot"],
            FakeDamageSource(actor=skeleton, damaging_actor=arrow),
            4.0)
        self.thin.on_actor_damage(ev)
        # 锁定的是骷髅（682），不是箭（900）
        self.assertIn("682", self.thin._mob_targets)
        self.assertNotIn("900", self.thin._mob_targets)
        self.assertEqual(self.thin._reflex.engaged[0][1], "682")
        self.assertTrue(any("Skeleton" in line for line in self.thin.logger.infos))

    def test_direct_damage_targets_the_mob(self):
        zombie = FakeMob(555, "Zombie", (3.0, 64.0, 3.0))
        self.thin.server.level.add(zombie)
        ev = FakeDamageEvent(
            self.thin._agents["Bot"],
            FakeDamageSource(actor=zombie, damaging_actor=zombie),
            6.0)
        self.thin.on_actor_damage(ev)
        self.assertIn("555", self.thin._mob_targets)
        self.assertEqual(self.thin._reflex.engaged[0][1], "555")


class ReflexAttackLoggingTests(unittest.TestCase):
    """反击失败要留下日志（旧版是静默的）。"""

    def test_failed_attack_is_logged(self):
        thin = Thin()
        thin._bridge = FakeBridge(ok=False, err="距离 8.1 格，够不着")
        thin._mob_targets["682"] = pm._MobTarget(
            runtime_id=682, name="Skeleton", pos=(1.0, 2.0, 3.0))
        self.assertTrue(thin._reflex_attack("Bot", "682"))
        self.assertEqual(thin._bridge.attacks, [("Bot", "Skeleton")])
        self.assertTrue(any("反击 Skeleton 失败" in line
                            for line in thin.logger.infos))

    def test_successful_attack_is_not_logged_as_failure(self):
        thin = Thin()
        thin._bridge = FakeBridge(ok=True, err="")
        thin._mob_targets["682"] = pm._MobTarget(
            runtime_id=682, name="Skeleton", pos=(1.0, 2.0, 3.0))
        thin._reflex_attack("Bot", "682")
        self.assertFalse(any("失败" in line for line in thin.logger.infos))


def main() -> int:
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for cls in (MobTargetRegistryTests, DamageSourcePickingTests,
                ReflexAttackLoggingTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total = result.testsRun
    if result.failures or result.errors:
        print(f"\n❌ {len(result.failures) + len(result.errors)} 项失败（共 {total} 项）")
        return 1
    print(f"\n✅ 全部 {total} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
