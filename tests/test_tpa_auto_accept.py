"""玩家 → 假人 tpa 自动同意（3.9.16）的状态机回归。

不需要服务器：核心与玩家全部用 Mock 顶替，只验证调度/过滤/复核
这些纯逻辑——真正的传送与扣费在弧光核心那边，不在本插件职责内。
"""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tests.endstone_stub import install

install()

from endstone_arc_fake_player.plugin import ArcFakePlayerPlugin


def make_plugin(tpa_cfg=None):
    plugin = ArcFakePlayerPlugin()
    plugin.logger = Mock()
    plugin._activity = Mock()
    # 默认延迟给大（60s）：保证"记录"与"接受"不会落在同一 tick——
    # 到点一律靠测试自己把时间戳拨到过去，行为可复现。
    plugin._cfg = {
        "tpa_auto_accept": {"enabled": True,
                            "accept_delay_min_seconds": 60.0,
                            "accept_delay_max_seconds": 60.0},
    }
    if tpa_cfg is not None:
        plugin._cfg["tpa_auto_accept"] = tpa_cfg
    plugin._agents = {"Aqing": Mock()}
    plugin._tpa_pending = {}
    return plugin


def make_core(requests):
    core = Mock()
    core.get_pending_requests_for_player = Mock(return_value=requests)
    core.accept_teleport_request = Mock()
    return core


class TpaAutoAcceptTests(unittest.TestCase):
    def test_real_player_request_gets_accepted(self):
        plugin = make_plugin()
        bot = object()
        plugin._live_actor = Mock(return_value=bot)
        sender = SimpleNamespace(xuid="1099612345678901")
        core = make_core([{"type": "tpa", "sender": "Steve"}])
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=core)),
            get_player=Mock(return_value=sender))

        plugin._tpa_tick()
        core.accept_teleport_request.assert_not_called()  # 只是记下，还没到点
        plugin._tpa_pending[("Aqing", "Steve")] = time.monotonic() - 1.0
        plugin._tpa_tick()
        core.accept_teleport_request.assert_called_once_with(bot)

    def test_accept_waits_out_the_random_delay(self):
        plugin = make_plugin({"enabled": True,
                              "accept_delay_min_seconds": 30.0,
                              "accept_delay_max_seconds": 60.0})
        plugin._live_actor = Mock(return_value=object())
        core = make_core([{"type": "tpa", "sender": "Steve"}])
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=core)),
            get_player=Mock(return_value=SimpleNamespace(xuid="x")))

        plugin._tpa_tick()
        self.assertEqual(len(plugin._tpa_pending), 1)
        accept_at = next(iter(plugin._tpa_pending.values()))
        self.assertGreaterEqual(accept_at - time.monotonic(), 29.0)
        plugin._tpa_tick()
        core.accept_teleport_request.assert_not_called()
        # 手动把到点时刻拨到过去 → 接受
        plugin._tpa_pending[("Aqing", "Steve")] = time.monotonic() - 1.0
        plugin._tpa_tick()
        core.accept_teleport_request.assert_called_once()

    def test_tphere_never_accepted(self):
        plugin = make_plugin()
        plugin._live_actor = Mock(return_value=object())
        core = make_core([{"type": "tphere", "sender": "Steve"}])
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=core)),
            get_player=Mock(return_value=SimpleNamespace(xuid="x")))

        plugin._tpa_tick()
        plugin._tpa_tick()
        core.accept_teleport_request.assert_not_called()
        self.assertEqual(plugin._tpa_pending, {})

    def test_synthetic_sender_skipped(self):
        """没有 xuid 的发起者（假人/合成对象）不代同意。"""
        plugin = make_plugin()
        plugin._live_actor = Mock(return_value=object())
        core = make_core([{"type": "tpa", "sender": "TieDan"}])
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=core)),
            get_player=Mock(return_value=SimpleNamespace(xuid="")))

        plugin._tpa_tick()
        self.assertEqual(plugin._tpa_pending, {})
        core.accept_teleport_request.assert_not_called()

    def test_expired_request_not_accepted(self):
        """等待期间申请被拒/过期：到点也不点同意。"""
        plugin = make_plugin()
        plugin._live_actor = Mock(return_value=object())
        core = make_core([{"type": "tpa", "sender": "Steve"}])
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=core)),
            get_player=Mock(return_value=SimpleNamespace(xuid="x")))

        plugin._tpa_tick()
        # 申请消失了
        core.get_pending_requests_for_player = Mock(return_value=[])
        plugin._tpa_pending[("Aqing", "Steve")] = time.monotonic() - 1.0
        plugin._tpa_tick()
        core.accept_teleport_request.assert_not_called()

    def test_disabled_switch_clears_pending(self):
        plugin = make_plugin()
        plugin._live_actor = Mock(return_value=object())
        core = make_core([{"type": "tpa", "sender": "Steve"}])
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=core)),
            get_player=Mock(return_value=SimpleNamespace(xuid="x")))
        plugin._tpa_tick()

        plugin._cfg["tpa_auto_accept"] = {"enabled": False}
        plugin._tpa_tick()
        self.assertEqual(plugin._tpa_pending, {})
        core.accept_teleport_request.assert_not_called()

    def test_no_core_is_silent_noop(self):
        plugin = make_plugin()
        plugin._live_actor = Mock(return_value=object())
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=None)),
            get_player=Mock())

        plugin._tpa_tick()  # 不应抛异常
        self.assertEqual(plugin._tpa_pending, {})


def main():
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(TpaAutoAcceptTests))
    if not result.wasSuccessful():
        return 1
    print(f"全部 {result.testsRun} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
