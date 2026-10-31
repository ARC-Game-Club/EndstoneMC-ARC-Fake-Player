"""枪战补位 / 自动进对局 的回归测试（不需要服务器）。

锁住 2026-10-31 在枪战服实踩的坑：

1. **等待阶段就开火**——队伍在进房那一刻就分好了（枪战插件的
   pick_join_team 平衡两队），所以等待室里已经存在"敌人"。假人的
   arena 战斗 AI 拿到名单就开火，于是"在出生地大厅里杀人"：
   真人复活落地就被打死（2026-10-31 20:46~20:49 枪战服日志）。
   ⇒ 只有对局进行中（playing/buying）才允许下发敌人名单。

2. **补位只派 1 个 / 房间创建不补位 / 假人自己不会进场**
   ⇒ count=0 表示"全部"；建房要补位；假人有自动 gs quick 的主循环。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tests.endstone_stub import install

install()

from endstone_arc_fake_player.plugin import ArcFakePlayerPlugin
from endstone_arc_fake_player import confmig


class ShooterAutoJoinTests(unittest.TestCase):
    def make_plugin(self):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._bridge = Mock()
        plugin._cfg = {}
        plugin._agents = {}
        plugin._arena_next = {}
        plugin._arena_waiting = {}
        plugin._record = Mock()
        return plugin

    def lobby_info(self, state, enemies=("Steve",)):
        return {"ok": True, "state": state, "enemies": list(enemies),
                "match_seconds": 300, "lobby_id": "L1"}

    # ---------- 核心：等待阶段不许开火 ----------

    def test_arena_enable_keeps_enemies_empty_in_waiting_room(self):
        plugin = self.make_plugin()
        shooter = Mock()
        shooter.api_lobby_info.return_value = self.lobby_info("lobby")
        plugin._shooter_plugin = lambda: shooter
        plugin._arena_enable("Al")
        args = plugin._bridge.arena.call_args[0]
        self.assertEqual(args[1], [], "等待阶段下发敌人名单 = 假人立刻开火杀人")
        self.assertIn("Al", plugin._arena_next,
                      "等待阶段也要登记，刷新器才会继续盯着等开局")

    def test_arena_enable_sends_enemies_when_match_live(self):
        plugin = self.make_plugin()
        shooter = Mock()
        shooter.api_lobby_info.return_value = self.lobby_info("playing")
        plugin._shooter_plugin = lambda: shooter
        plugin._arena_enable("Al")
        args = plugin._bridge.arena.call_args[0]
        self.assertEqual(args[1], ["Steve"], "对局进行中必须下发敌人名单")

    def test_arena_refresh_clears_enemies_in_waiting_room(self):
        plugin = self.make_plugin()
        shooter = Mock()
        shooter.api_lobby_info.return_value = self.lobby_info("lobby")
        plugin._shooter_plugin = lambda: shooter
        plugin._arena_next = {"Al": 0.0}
        plugin._arena_refresh()
        args = plugin._bridge.arena.call_args[0]
        self.assertEqual(args[1], [], "等待阶段必须清空名单（否则一直打）")

    def test_arena_refresh_keeps_enemies_when_match_live(self):
        plugin = self.make_plugin()
        shooter = Mock()
        shooter.api_lobby_info.return_value = self.lobby_info("playing")
        plugin._shooter_plugin = lambda: shooter
        plugin._arena_next = {"Al": 0.0}
        plugin._arena_refresh()
        args = plugin._bridge.arena.call_args[0]
        self.assertEqual(args[1], ["Steve"])

    def test_arena_refresh_interval_is_short_enough(self):
        plugin = self.make_plugin()
        # 对局状态切换最多等 5 秒——15 秒的间隔会让假人在真人复活后
        # 还接着打（"复活即死"的窗口）。
        self.assertLessEqual(plugin._arena_refresh_seconds, 5.0)

    # ---------- 补位：默认全部假玩家 ----------

    def test_fill_shooter_sends_all_when_count_zero(self):
        plugin = self.make_plugin()
        plugin._cfg = {"shooter_fill": {"enabled": True, "count": 0}}
        shooter = Mock()
        shooter.api_quick_join.return_value = {"ok": True}
        plugin._shooter_plugin = lambda: shooter
        plugin._arena_enable = Mock()
        plugin._roam_busy = lambda name: False
        plugin._live_actor = lambda name: SimpleNamespace(name=name)
        plugin._agents = {n: SimpleNamespace(alive=lambda: True)
                          for n in ("A", "B", "C")}
        result = plugin.api_fill_shooter(8, lobby_id="L1")
        self.assertEqual(len(result["filled"]), 3,
                         "count=0 = 全部假玩家都补位（服主要求）")
        for call in shooter.api_quick_join.call_args_list:
            self.assertEqual(call.kwargs.get("lobby_id"), "L1",
                             "必须补到指定的那间房，不能散进各个等待室")

    def test_fill_shooter_respects_explicit_count(self):
        plugin = self.make_plugin()
        plugin._cfg = {"shooter_fill": {"enabled": True, "count": 1}}
        shooter = Mock()
        shooter.api_quick_join.return_value = {"ok": True}
        plugin._shooter_plugin = lambda: shooter
        plugin._arena_enable = Mock()
        plugin._roam_busy = lambda name: False
        plugin._live_actor = lambda name: SimpleNamespace(name=name)
        plugin._agents = {n: SimpleNamespace(alive=lambda: True)
                          for n in ("A", "B", "C")}
        result = plugin.api_fill_shooter(8)
        self.assertEqual(len(result["filled"]), 1, "显式指定数量时按数量来")

    def test_fill_shooter_disabled_sends_nobody(self):
        plugin = self.make_plugin()
        plugin._cfg = {"shooter_fill": {"enabled": False, "count": 0}}
        shooter = Mock()
        plugin._shooter_plugin = lambda: shooter
        result = plugin.api_fill_shooter(8)
        self.assertFalse(result.get("ok"))
        shooter.api_quick_join.assert_not_called()

    # ---------- 自动 gs quick 主循环 ----------

    def test_auto_join_prefers_running_match(self):
        plugin = self.make_plugin()
        shooter = Mock()
        shooter.api_lobby_info.return_value = {"ok": False}
        shooter.api_lobby_status.return_value = {
            "ok": True, "total": 2, "open": 1, "running": 1,
            "running_lobby_id": "RUN",
        }
        shooter.api_quick_join.return_value = {"ok": True}
        plugin._shooter_plugin = lambda: shooter
        plugin._arena_enable = Mock()
        plugin._roam_busy = lambda name: False
        plugin._live_actor = lambda name: SimpleNamespace(name=name)
        self.assertTrue(plugin._shooter_auto_join_one("Al", shooter))
        self.assertEqual(shooter.api_quick_join.call_args.kwargs.get("lobby_id"),
                         "RUN", "已经有对局在跑 → 立刻进那一间")

    def test_auto_join_does_not_create_room_when_all_full(self):
        plugin = self.make_plugin()
        shooter = Mock()
        shooter.api_lobby_info.return_value = {"ok": False}
        shooter.api_lobby_status.return_value = {
            "ok": True, "total": 2, "open": 0, "running": 0,
            "running_lobby_id": "",
        }
        plugin._shooter_plugin = lambda: shooter
        plugin._roam_busy = lambda name: False
        plugin._live_actor = lambda name: SimpleNamespace(name=name)
        self.assertFalse(plugin._shooter_auto_join_one("Al", shooter))
        shooter.api_quick_join.assert_not_called()

    def test_auto_join_skips_busy_agents(self):
        plugin = self.make_plugin()
        shooter = Mock()
        shooter.api_lobby_info.return_value = {"ok": False}
        shooter.api_lobby_status.return_value = {
            "ok": True, "total": 0, "open": 0, "running": 0,
            "running_lobby_id": "",
        }
        plugin._shooter_plugin = lambda: shooter
        plugin._roam_busy = lambda name: True      # 在打架/对局/有计划
        plugin._live_actor = lambda name: SimpleNamespace(name=name)
        self.assertFalse(plugin._shooter_auto_join_one("Al", shooter))
        shooter.api_quick_join.assert_not_called()

    def test_auto_join_asks_for_autostart_when_already_in_room(self):
        plugin = self.make_plugin()
        shooter = Mock()
        shooter.api_lobby_info.return_value = self.lobby_info("lobby")
        plugin._shooter_plugin = lambda: shooter
        self.assertFalse(plugin._shooter_auto_join_one("Al", shooter))
        shooter.api_autostart.assert_called_once_with("L1")

    def test_auto_join_tick_is_throttled(self):
        plugin = self.make_plugin()
        plugin._cfg = {"shooter_auto_join": {"enabled": True,
                                            "interval_seconds": 30}}
        shooter = Mock()
        shooter.api_lobby_info.return_value = {"ok": False}
        plugin._shooter_plugin = lambda: shooter
        plugin._agents = {}
        plugin._shooter_auto_next = 0.0
        plugin._shooter_auto_join_tick()
        first = plugin._shooter_auto_next
        self.assertGreater(first, 0.0)
        plugin._shooter_auto_join_tick()
        self.assertEqual(plugin._shooter_auto_next, first,
                         "第二次调用应被节流挡掉")

    def test_auto_join_disabled_does_nothing(self):
        plugin = self.make_plugin()
        plugin._cfg = {"shooter_auto_join": {"enabled": False}}
        shooter = Mock()
        plugin._shooter_plugin = lambda: shooter
        plugin._agents = {"Al": SimpleNamespace(alive=lambda: True)}
        plugin._shooter_auto_next = 0.0
        plugin._shooter_auto_join_tick()
        shooter.api_quick_join.assert_not_called()

    # ---------- 配置迁移 ----------

    def test_config_migration_upgrades_shooter_fill_count(self):
        defaults = {"shooter_fill": {"enabled": True, "count": 0}}
        cfg = {"shooter_fill": {"enabled": True, "count": 1}}
        changed = confmig.migrate(cfg, 18, defaults)
        self.assertIn("shooter_fill", changed)
        self.assertEqual(cfg["shooter_fill"]["count"], 0,
                         "v18 的默认值要自动升到 v19 的全部派")

    def test_config_migration_keeps_user_modified_count(self):
        defaults = {"shooter_fill": {"enabled": True, "count": 0}}
        cfg = {"shooter_fill": {"enabled": True, "count": 5}}
        confmig.migrate(cfg, 18, defaults)
        self.assertEqual(cfg["shooter_fill"]["count"], 5,
                         "服主自己改过的值必须保留")

    def test_new_config_key_is_added(self):
        defaults = {"shooter_auto_join": {"enabled": True,
                                          "interval_seconds": 30}}
        cfg = {}
        changed = confmig.migrate(cfg, 18, defaults)
        self.assertIn("shooter_auto_join", changed)
        self.assertIn("shooter_auto_join", cfg)


def main():
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ShooterAutoJoinTests))
    if not result.wasSuccessful():
        return 1
    print(f"全部 {result.testsRun} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
