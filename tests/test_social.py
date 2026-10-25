"""好感度仓库与反击条件反射的单元测试（零游戏依赖）。"""

from __future__ import annotations

import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from endstone_arc_fake_player.social import (  # noqa: E402
    CombatReflex, FavorStore, clamp_score, level_name,
)


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now


class FavorStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "favorability.json"
        self.clock = FakeClock(1000.0)

    def store(self):
        return FavorStore(self.path, logger=None, clock=self.clock)

    def test_adjust_clamps_and_persists(self):
        store = self.store()
        store.adjust("Aqing", "Steve", -95)
        self.assertEqual(store.score("Aqing", "Steve"), -95.0)
        store.adjust("Aqing", "Steve", -95)      # 应夹到 -100
        self.assertEqual(store.score("Aqing", "Steve"), -100.0)
        store.adjust("Aqing", "Steve", 500)      # 应夹到 +100
        self.assertEqual(store.score("Aqing", "Steve"), 100.0)
        store.save()
        # 重新加载：分数还在（重启/轮换不失忆）
        reborn = FavorStore(self.path, logger=None, clock=self.clock)
        self.assertEqual(reborn.score("Aqing", "Steve"), 100.0)

    def test_default_score_is_zero_and_grudges(self):
        store = self.store()
        self.assertEqual(store.score("Aqing", "Nobody"), 0.0)
        store.adjust("Aqing", "Griefer", -30)
        store.adjust("Aqing", "Steve", 10)
        self.assertEqual(store.grudges("Aqing", -30.0), ["Griefer"])
        self.assertEqual(store.grudges("Steve", -30.0), [])

    def test_chat_bonus_cooldown(self):
        store = self.store()
        self.assertIsNotNone(store.chat_bonus("Aqing", "Steve"))
        self.assertIsNone(store.chat_bonus("Aqing", "Steve"))   # 冷却中
        self.clock.now += 301.0
        self.assertIsNotNone(store.chat_bonus("Aqing", "Steve"))
        # 冷却期间说话不影响别的玩家
        self.assertIsNotNone(store.chat_bonus("Aqing", "Alex"))

    def test_settle_vendetta_clears_grudge(self):
        store = self.store()
        store.adjust("Aqing", "Griefer", -80)
        self.assertIn("Griefer", store.grudges("Aqing", -30.0))
        store.settle_vendetta("Aqing", "Griefer")
        self.assertEqual(store.score("Aqing", "Griefer"), -10.0)
        self.assertEqual(store.grudges("Aqing", -30.0), [])

    def test_describe_orders_grudges_first(self):
        store = self.store()
        store.adjust("Aqing", "Friend", 70)
        store.adjust("Aqing", "Enemy", -50)
        line = store.describe("Aqing", ["Friend", "Enemy"],
                              grudge=-30.0, friend=60.0)
        self.assertIn("Enemy(仇人-50)", line)
        self.assertIn("Friend(友好+70)", line)
        self.assertLess(line.index("Enemy"), line.index("Friend"))
        self.assertEqual(store.describe("Aqing", [], grudge=-30, friend=60), "")

    def test_level_names(self):
        cases = [(-100, "仇人"), (-30, "仇人"), (-29, "警惕"),
                 (0, "中立"), (60, "友好"), (80, "挚友")]
        for score, want in cases:
            self.assertEqual(
                level_name(score, grudge=-30.0, friend=60.0), want,
                f"score={score}")

    def test_corrupt_file_starts_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        store = FavorStore(self.path, logger=None, clock=self.clock)
        self.assertEqual(store.score("x", "y"), 0.0)


class ReflexHarness:
    """CombatReflex 的假世界：可编程的坐标与回调记录。"""

    def __init__(self, *, bot_pos=(0.0, 64.0, 0.0), target_pos=(3.0, 64.0, 0.0),
                 target_online=True, health=20.0):
        self.bot = bot_pos
        self.target = target_pos if target_online else None
        self.health = health
        self.attacks: list[tuple[str, str]] = []
        self.walks: list[tuple] = []
        self.events: list[tuple[str, str]] = []
        self.clock = FakeClock(0.0)
        self.reflex = CombatReflex(
            attack=self._attack, walk=self._walk, bot_pos=lambda b: self.bot,
            target_pos=lambda t: self.target, bot_health=lambda b: self.health,
            on_event=self._event, logger=None, clock=self.clock)

    def _attack(self, bot, target):
        self.attacks.append((bot, target))
        return True

    def _walk(self, bot, x, y, z):
        self.walks.append((bot, x, y, z))
        return True

    def _event(self, bot, text):
        self.events.append((bot, text))


class CombatReflexTests(unittest.TestCase):
    def test_attack_when_in_reach(self):
        h = ReflexHarness(bot_pos=(0, 64, 0), target_pos=(3, 64, 0))
        h.reflex.engage("Aqing", "Steve", reason="反击", duration=60)
        h.reflex.tick()
        self.assertEqual(h.attacks, [("Aqing", "Steve")])
        self.assertEqual(h.walks, [])           # 够得着就不走

    def test_attack_cadence_respected(self):
        h = ReflexHarness()
        h.reflex.engage("Aqing", "Steve")
        h.reflex.tick()
        h.clock.now += 0.3                      # 冷却没到
        h.reflex.tick()
        self.assertEqual(len(h.attacks), 1)
        h.clock.now += 0.6                      # 0.9s ≥ 0.8s 间隔
        h.reflex.tick()
        self.assertEqual(len(h.attacks), 2)

    def test_chases_when_out_of_reach(self):
        h = ReflexHarness(bot_pos=(0, 64, 0), target_pos=(30, 64, 0))
        h.reflex.engage("Aqing", "Steve")
        h.reflex.tick()
        self.assertEqual(h.attacks, [])
        self.assertEqual(len(h.walks), 1)
        self.assertEqual(h.walks[0][1:], (30, 64, 0))
        h.clock.now += 1.0                      # 走位刷新间隔没到
        h.reflex.tick()
        self.assertEqual(len(h.walks), 1)
        h.clock.now += 1.5
        h.reflex.tick()
        self.assertEqual(len(h.walks), 2)

    def test_gives_up_when_target_vanishes_or_times_out(self):
        h = ReflexHarness(target_online=False)
        h.reflex.engage("Aqing", "Steve")
        h.reflex.tick()
        self.assertFalse(h.reflex.is_engaged("Aqing"))

        h2 = ReflexHarness()
        h2.reflex.engage("Aqing", "Steve", duration=50)
        h2.clock.now += 51
        h2.reflex.tick()
        self.assertFalse(h2.reflex.is_engaged("Aqing"))

    def test_low_health_disengages_with_warning(self):
        h = ReflexHarness(health=4.0)
        h.reflex.engage("Aqing", "Steve")
        h.reflex.tick()
        self.assertFalse(h.reflex.is_engaged("Aqing"))
        self.assertTrue(any("快被打死" in text for _, text in h.events))

    def test_reengage_same_target_extends_deadline(self):
        h = ReflexHarness()
        h.reflex.engage("Aqing", "Steve", duration=10)
        h.clock.now += 9
        h.reflex.engage("Aqing", "Steve", duration=60)     # 又被打了一下
        h.clock.now += 30
        h.reflex.tick()
        self.assertTrue(h.reflex.is_engaged("Aqing"))      # 仍然在打

    def test_engaged_bots_of_and_disengage_return_target(self):
        h = ReflexHarness()
        h.reflex.engage("Aqing", "Steve")
        h.reflex.engage("TieDan", "Steve")
        self.assertEqual(sorted(h.reflex.engaged_bots_of("Steve")),
                         ["Aqing", "TieDan"])
        self.assertEqual(h.reflex.disengage("Aqing", "测试"), "Steve")
        self.assertEqual(h.reflex.engaged_bots_of("Steve"), ["TieDan"])
        self.assertIsNone(h.reflex.disengage("Nobody"))

    def test_bot_offline_disengages(self):
        h = ReflexHarness()
        h.reflex.engage("Aqing", "Steve")
        h.bot = None
        h.reflex.tick()
        self.assertFalse(h.reflex.is_engaged("Aqing"))

    def test_clamp_score(self):
        self.assertEqual(clamp_score(123), 100.0)
        self.assertEqual(clamp_score(-123), -100.0)
        self.assertEqual(clamp_score(12.345), 12.3)


def main():
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for cls in (FavorStoreTests, CombatReflexTests):
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


class MobEngagementTests(unittest.TestCase):
    """怪物反击不计好感（≤3.8.8 把僵尸 runtime id 记成仇人的回归）。"""

    def _reflex(self):
        return CombatReflex(
            attack=lambda b, t: True,
            walk=lambda b, x, y, z: True,
            bot_pos=lambda b: (0.0, 0.0, 0.0),
            target_pos=lambda t: (3.0, 0.0, 0.0),
            bot_health=lambda b: 20.0,
            on_event=lambda b, t: None)

    def test_mob_engagement_not_marked_player(self):
        r = self._reflex()
        r.engage("Bot", "12345", label="Zombie", is_player=False)
        e = r.engagement_of("Bot")
        self.assertIsNotNone(e)
        self.assertFalse(e.is_player)
        self.assertEqual(e.target, "12345")
        self.assertEqual(e.label, "Zombie")

    def test_player_engagement_default(self):
        r = self._reflex()
        r.engage("Bot", "Steve")
        self.assertTrue(r.engagement_of("Bot").is_player)

    def test_prune_mob_entries(self):
        import tempfile, os
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "f.json"
            store = FavorStore(path)
            store.adjust("Bot", "682", -40.0)     # 怪物 runtime id
            store.adjust("Bot", "Steve", -5.0)    # 真人
            store.save()
            removed = store.prune_mob_entries()
            self.assertEqual(removed, 1)
            self.assertNotIn("682", store._data["Bot"])
            self.assertIn("Steve", store._data["Bot"])
