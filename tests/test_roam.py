"""锚点闲逛驱动的单元测试（零游戏依赖）。"""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from endstone_arc_fake_player.roam import RoamDriver  # noqa: E402


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now


class Harness:
    def __init__(self, *, anchor=(100.0, 64.0, 100.0, "家"),
                 bot_pos=(0.0, 64.0, 0.0), surface_y=64.0, busy=False):
        self.anchor = anchor
        self.bot = bot_pos
        self.surface_y = surface_y
        self.busy = busy
        self.walks: list[tuple] = []
        self.digs: list[tuple] = []
        self.logs: list[str] = []
        self.clock = FakeClock()
        self.driver = RoamDriver(
            names=lambda: ["Aqing"],
            busy=lambda n: self.busy,
            anchor=lambda n: self.anchor,
            bot_pos=lambda n: self.bot,
            surface=lambda x, z, ny: self.surface_y,
            walk=self._walk, dig=self._dig,
            log=self.logs.append,
            clock=self.clock, rng=random.Random(7))

    def _walk(self, n, x, y, z):
        self.walks.append((n, x, y, z))
        return True

    def _dig(self, n, x, y, z):
        self.digs.append((n, x, y, z))
        return True


class RoamDriverTests(unittest.TestCase):
    def test_busy_yields(self):
        h = Harness(busy=True)
        h.clock.now = 100
        h.driver.tick()
        self.assertEqual(h.walks, [])
        self.assertEqual(h.digs, [])

    def test_wait_respected(self):
        h = Harness()
        h.driver.tick()                       # 第一次行动
        n = len(h.walks) + len(h.digs)
        self.assertEqual(n, 1)
        h.clock.now += 1                      # 间隔没到：不重复行动
        h.driver.tick()
        self.assertEqual(len(h.walks) + len(h.digs), 1)

    def test_far_from_anchor_walks_leg_toward_it(self):
        # 离家 100+ 格（>60）：一腿 48 格朝家走，不挖不闲逛
        h = Harness(bot_pos=(0.0, 64.0, 0.0), anchor=(200.0, 64.0, 0.0, "家"))
        h.driver.tick()
        self.assertEqual(len(h.walks), 1)
        _, x, _, _ = h.walks[0]
        self.assertAlmostEqual(x, 48.0, places=0)
        self.assertEqual(h.digs, [])
        self.assertTrue(any("走一腿" in m for m in h.logs))

    def test_poi_within_radius_of_anchor(self):
        # 在锚点附近：兴趣点应落在锚点周围 POI_RADIUS 内
        h = Harness(bot_pos=(100.0, 64.0, 100.0),
                    anchor=(100.0, 64.0, 100.0, "家"))
        h.driver.dig_chance = 0.0             # 关挖矿，纯走路好断言
        h.driver.tick()
        self.assertEqual(len(h.walks), 1)
        _, x, y, z = h.walks[0]
        dist = ((x - 100.0) ** 2 + (z - 100.0) ** 2) ** 0.5
        self.assertLessEqual(dist, h.driver.POI_RADIUS[1] + 1.5)

    def test_dig_chance_one_always_digs_ground(self):
        h = Harness(bot_pos=(100.0, 64.0, 100.0),
                    anchor=(100.0, 64.0, 100.0, "家"))
        h.driver.dig_chance = 1.0
        h.driver.tick()
        self.assertEqual(len(h.digs), 1)
        _, x, y, z = h.digs[0]
        self.assertEqual(y, 63)               # 挖的是站立面下面那块

    def test_dig_chance_zero_never_digs(self):
        h = Harness(bot_pos=(100.0, 64.0, 100.0),
                    anchor=(100.0, 64.0, 100.0, "家"))
        h.driver.dig_chance = 0.0
        for _ in range(10):
            h.driver.tick()
            h.clock.now += 60.0
        self.assertEqual(h.digs, [])

    def test_no_anchor_shrinks_radius(self):
        # 没有锚点：原地附近小范围晃（半径减半），一样会动
        h = Harness(bot_pos=(50.0, 64.0, 50.0), anchor=None)
        h.driver.dig_chance = 0.0
        h.driver.tick()
        self.assertEqual(len(h.walks), 1)
        _, x, _, z = h.walks[0]
        dist = ((x - 50.0) ** 2 + (z - 50.0) ** 2) ** 0.5
        self.assertLessEqual(dist, h.driver.POI_RADIUS[1] // 2 + 1.5)

    def test_bot_offline_no_action(self):
        h = Harness()
        h.bot = None
        h.driver.tick()
        self.assertEqual(h.walks, [])
        self.assertEqual(h.digs, [])

    def test_no_walkable_surface_backs_off(self):
        h = Harness()
        h.surface_y = None                    # 地表找不到
        h.driver.tick()
        self.assertEqual(h.walks, [])
        self.assertEqual(h.digs, [])


def main():
    loader = unittest.defaultTestLoader
    suite = loader.loadTestsFromTestCase(RoamDriverTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total = result.testsRun
    if result.failures or result.errors:
        print(f"\n❌ {len(result.failures) + len(result.errors)} 项失败（共 {total} 项）")
        return 1
    print(f"\n✅ 全部 {total} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
