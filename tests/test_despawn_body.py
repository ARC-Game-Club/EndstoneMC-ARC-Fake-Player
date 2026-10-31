"""撤身体：玩家类实体不再调 remove()。

背景
----
Endstone 把 `Player.remove()` 禁掉了：对玩家（或 SimulatedPlayer）调用，
BDS 会直接拒绝并打出

    [Server/ERROR]: Cannot remove player X, use Player::kick instead.

而且**根本不会真的移除**——身体还在，Python 侧抛出的异常又被
`except Exception: pass` 吞掉。于是这条兜底实际等于"刷一条 ERROR +
什么也没干"，每次假人轮换下线都会在控制台留一条。

真正能撤掉假人身体的是行为包：`bot:remove` → JS 侧 `sim.disconnect()`。

这些用例把三件事钉住：
  1. 玩家类实体走 `_despawn_body` 时**不碰** remove()；
  2. 非玩家实体（占位 NPC）照旧 remove()；
  3. `_same_entity` 按 runtime id 判，而不是 Python 对象身份——
     否则会把正在线上的假人身体误当成"换下来的旧身体"去撤。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tests.endstone_stub import install

install()

from endstone import Player
from endstone_arc_fake_player.plugin import (
    ArcFakePlayerPlugin,
    _is_player_entity,
    _same_entity,
)


class FakeSimPlayer(Player):
    """模拟玩家：有 kick，也有 remove——但 BDS 不允许对玩家用 remove。"""

    def __init__(self, name="Hunter2", ident=42):
        self.name = name
        self.id = ident
        self.removed = 0
        self.kicked = 0

    def remove(self):
        self.removed += 1
        raise RuntimeError("Cannot remove player")

    def kick(self, reason=""):
        self.kicked += 1


class FakeNpc:
    """占位 NPC：只有 remove()，没有 kick()。"""

    def __init__(self, ident=7):
        self.id = ident
        self.removed = 0

    def remove(self):
        self.removed += 1


class DespawnBodyTests(unittest.TestCase):
    def setUp(self):
        self.plugin = ArcFakePlayerPlugin()
        self.plugin.logger = Mock()

    # -- 判据 ---------------------------------------------------------

    def test_player_entity_is_detected(self):
        self.assertTrue(_is_player_entity(FakeSimPlayer()))
        self.assertFalse(_is_player_entity(FakeNpc()))
        self.assertFalse(_is_player_entity(None))

    def test_same_entity_compares_runtime_id_not_python_identity(self):
        a = FakeSimPlayer(ident=99)
        b = FakeSimPlayer(ident=99)
        c = FakeSimPlayer(ident=100)
        self.assertIsNot(a, b)          # 两个不同的 Python 包装对象
        self.assertTrue(_same_entity(a, b))   # 却是同一个底层实体
        self.assertFalse(_same_entity(a, c))
        self.assertFalse(_same_entity(a, None))
        self.assertFalse(_same_entity(None, None))

    def test_same_entity_without_id_is_false(self):
        """取不到 runtime id 时判成"不是同一个"，后续动作对玩家本就跳过。"""
        a = SimpleNamespace(name="x")
        b = SimpleNamespace(name="x")
        self.assertFalse(_same_entity(a, b))

    # -- 撤身体 -------------------------------------------------------

    def test_despawn_skips_player_entities(self):
        """玩家类实体不能走 remove()：BDS 会拒绝并刷一条 ERROR。"""
        p = FakeSimPlayer()
        self.plugin._despawn_body(p)
        self.assertEqual(p.removed, 0)

    def test_despawn_removes_non_player_entities(self):
        npc = FakeNpc()
        self.plugin._despawn_body(npc)
        self.assertEqual(npc.removed, 1)

    def test_despawn_none_is_noop(self):
        self.plugin._despawn_body(None)

    def test_despawn_swallows_errors(self):
        class Boom:
            id = 1

            def remove(self):
                raise RuntimeError("boom")

        self.plugin._despawn_body(Boom())   # 不抛出来就算过

    # -- 占位升级的真实触发路径 ---------------------------------------

    def _upgrade_plugin(self, player):
        plugin = self.plugin
        plugin._agents = {}
        plugin._actors = {}
        plugin._external = {}
        plugin._bodies = {}
        plugin._placeholder_flags = {}
        plugin._world = SimpleNamespace(body_dimension=None)
        plugin._find_sim_player = lambda name: player
        plugin._verify_bot_game_mode = lambda p, n: None
        plugin._apply_pending_gm = lambda n, p: None
        plugin._ensure_core_identity = lambda p, n: None
        plugin._record = lambda *a, **k: None
        return plugin

    def test_upgrade_does_not_remove_live_body(self):
        """同一个实体的两份包装对象 → 不许撤（原来每次都刷 ERROR）。"""
        live = FakeSimPlayer(ident=5)
        wrapper = FakeSimPlayer(ident=5)     # 同一个底层实体，不同包装
        plugin = self._upgrade_plugin(live)
        plugin._actors["Hunter2"] = wrapper

        plugin._upgrade_placeholder("Hunter2")

        self.assertEqual(wrapper.removed, 0)
        self.assertEqual(live.removed, 0)

    def test_upgrade_removes_stale_placeholder(self):
        """真的是占位 NPC（不同实体）→ 照旧撤掉，别让它永远站在原地。"""
        live = FakeSimPlayer(ident=5)
        npc = FakeNpc(ident=6)
        plugin = self._upgrade_plugin(live)
        plugin._actors["Hunter2"] = npc

        plugin._upgrade_placeholder("Hunter2")

        self.assertEqual(npc.removed, 1)
        self.assertEqual(plugin._actors["Hunter2"], live)


def main():
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(DespawnBodyTests))
    if not result.wasSuccessful():
        return 1
    print(f"全部 {result.testsRun} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
