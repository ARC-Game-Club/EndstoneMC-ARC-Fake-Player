"""Plugin integration and rename migration regressions (no server required)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tests.endstone_stub import install

install()

from endstone_arc_fake_player.plugin import ArcFakePlayerPlugin
from endstone_arc_fake_player import confmig
from endstone_arc_fake_player.executor import BatchExecutor, Plan, PlanState, Step


class PluginFixTests(unittest.TestCase):
    def config_plugin(self, root):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._config_path = root / "arc_fake_player" / "config.json"
        return plugin

    def test_legacy_config_copy_keeps_source_and_new_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "pureagent" / "config.json"
            legacy.parent.mkdir()
            legacy.write_text('{"model": "old", "api_key": "example"}', encoding="utf-8")
            plugin = self.config_plugin(root)
            plugin._migrate_legacy_config()
            self.assertEqual(legacy.read_bytes(), plugin._config_path.read_bytes())
            plugin._config_path.write_text('{"model": "new"}', encoding="utf-8")
            plugin._migrate_legacy_config()
            self.assertEqual(json.loads(plugin._config_path.read_text("utf-8"))["model"], "new")
            self.assertEqual(json.loads(legacy.read_text("utf-8"))["model"], "old")

    def test_config_migration_uses_pristine_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self.config_plugin(Path(tmp))
            plugin._config_path.parent.mkdir()
            old = {"config_version": 1, "prompt_persona": "old default", "model": "custom"}
            plugin._config_path.write_text(json.dumps(old), encoding="utf-8")
            from unittest.mock import patch
            with patch.dict(confmig.LEGACY_DEFAULTS, {"prompt_persona": {1: "old default"}}):
                cfg = plugin._load_config()
            self.assertNotEqual(cfg["prompt_persona"], "old default")
            self.assertEqual(cfg["model"], "custom")
            self.assertEqual(json.loads(plugin._config_path.read_text(encoding="utf-8"))["config_version"], confmig.CONFIG_VERSION)

    def test_schema_upgrade_saved_when_no_values_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self.config_plugin(Path(tmp))
            cfg = plugin._load_config()
            cfg["config_version"] = 1
            plugin._config_path.write_text(json.dumps(cfg), encoding="utf-8")
            plugin._load_config()
            self.assertEqual(json.loads(plugin._config_path.read_text("utf-8"))["config_version"], confmig.CONFIG_VERSION)

    def test_chat_requires_name_boundary(self):
        plugin = ArcFakePlayerPlugin()
        # 搭话概率钉死为 0：这个用例只测名字边界，别让闲聊随机监听
        # 把 hear 意外触发（否则它是 15% 概率的隐性炸弹）。
        plugin.chat_reply_chance = lambda name: 0.0
        agent = SimpleNamespace(hear=Mock(), wake=Mock(), state=SimpleNamespace(last_decision=5))
        plugin._agents = {"Al": agent}
        plugin._stealth = set()
        plugin.server = SimpleNamespace(broadcast_message=Mock())
        event = SimpleNamespace(message="Alice hello", player=SimpleNamespace(name="Steve"), cancel=Mock())
        plugin.on_player_chat(event)
        agent.hear.assert_not_called()
        # 普通玩家（非 OP）：对话而非控制——不取消事件（话留在公屏），
        # 大脑以"朋友聊天"的定位听到。
        event.message = "@Al 挖石头"
        plugin.on_player_chat(event)
        agent.hear.assert_called_once_with("Steve(玩家)", "@Al 挖石头")
        event.cancel.assert_not_called()
        # 管理员：指令式控制——取消事件并立即唤醒决策。
        op_agent = SimpleNamespace(hear=Mock(), wake=Mock(), state=SimpleNamespace(last_decision=5))
        plugin._agents = {"Al": op_agent}
        event2 = SimpleNamespace(message="@Al 挖石头",
                                 player=SimpleNamespace(name="Steve", is_op=True),
                                 cancel=Mock())
        plugin.on_player_chat(event2)
        op_agent.hear.assert_called_once_with("Steve(管理员)", "@Al 挖石头")
        event2.cancel.assert_called_once()

    def test_terminal_interaction_failure_reaches_executor(self):
        for op, method in (("attack", "attack_real"), ("consume", "consume_real")):
            with self.subTest(op=op):
                plugin = ArcFakePlayerPlugin()
                plugin.logger = Mock()
                plugin._executor = BatchExecutor(plugin.logger)
                plugin._world = SimpleNamespace()
                plugin._use_real_body = True
                plugin._exec_owner = "bot"
                plugin._current_actor = lambda: object()
                plugin._note_step_owner = lambda plan, step: None
                step = Step(op, 0, 0, 0, async_op=True)
                plan = Plan(label="bot: test", steps=[step])
                plugin._exec_plan = plan
                actions = SimpleNamespace(key_of=lambda s: (op,), pop_gave_up=lambda key: "rejected")
                setattr(actions, method, lambda s: True)
                plugin._real_actions = {"bot": actions}
                plugin._register_handlers()
                plugin._executor.submit(plan)
                plugin._executor.tick()
                self.assertEqual(plan.state, PlanState.FAILED)
                self.assertEqual(plan.fail_count, 1)
                self.assertEqual(plan.done_count, 0)


def main():
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(PluginFixTests))
    if not result.wasSuccessful():
        return 1
    print(f"全部 {result.testsRun} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
