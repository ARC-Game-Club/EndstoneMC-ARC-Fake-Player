"""聊天个性：公屏搭话概率门控 + 说话风格示例（卡片解析/提示词段）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.endstone_stub import install as install_stub  # noqa: E402

install_stub()

from endstone_arc_fake_player.crowd import CharacterCard, load_cards  # noqa: E402
from endstone_arc_fake_player.plugin import ArcFakePlayerPlugin  # noqa: E402


class CardPersonaTests(unittest.TestCase):
    def test_card_parses_chance_and_style(self):
        cards = load_cards([
            {"name": "TieDan", "chat_reply_chance": 0.3,
             "style": ["我操太好玩了hhh", " | 没用的话别来烦我 "]},
            {"name": "B", "chat_reply_chance": 7},   # 超界夹到 1.0
        ])
        self.assertEqual(cards[0].chat_reply_chance, 0.3)
        self.assertEqual(cards[0].style, ["我操太好玩了hhh", "| 没用的话别来烦我"])
        self.assertEqual(cards[1].chat_reply_chance, 1.0)

    def test_card_chance_invalid_falls_back_none(self):
        card = CharacterCard.from_dict({"name": "A", "chat_reply_chance": "abc"})
        self.assertIsNone(card.chat_reply_chance)
        self.assertIsNone(card.style)


class ChatPersonaPluginTests(unittest.TestCase):
    def plugin_with(self, chance):
        plugin = ArcFakePlayerPlugin()
        plugin._bot_settings = {"Al": {"chat_reply_chance": chance}}
        plugin._stealth = set()
        plugin._cfg = {}
        plugin._favor = SimpleNamespace(chat_bonus=Mock(return_value=None))
        plugin._record = Mock()
        plugin._social_cfg = lambda: {}
        return plugin

    def chat_event(self, msg, name="Steve", is_op=False):
        return SimpleNamespace(
            message=msg,
            player=SimpleNamespace(name=name, is_op=is_op),
            cancel=Mock())

    def test_ambient_chance_zero_never_replies(self):
        plugin = self.plugin_with(chance=0.0)
        agent = SimpleNamespace(
            hear=Mock(), alive=lambda: True,
            state=SimpleNamespace(last_decision=5.0))
        plugin._agents = {"Al": agent}
        event = self.chat_event("这游戏真好玩")       # 没点名
        plugin.on_player_chat(event)
        agent.hear.assert_not_called()

    def test_ambient_chance_one_always_listens(self):
        plugin = self.plugin_with(chance=1.0)
        agent = SimpleNamespace(
            hear=Mock(), alive=lambda: True,
            state=SimpleNamespace(last_decision=5.0))
        plugin._agents = {"Al": agent}
        event = self.chat_event("这游戏真好玩")
        plugin.on_player_chat(event)
        agent.hear.assert_called_once()
        args = agent.hear.call_args[0]
        self.assertIn("Steve(玩家闲聊)", args[0])
        self.assertEqual(args[1], "这游戏真好玩")
        self.assertEqual(agent.state.last_decision, 0.0)   # 立刻想

    def test_mention_still_forced_regardless_of_chance(self):
        # @点名必回：概率 0 也要回
        plugin = self.plugin_with(chance=0.0)
        agent = SimpleNamespace(
            hear=Mock(), alive=lambda: True,
            state=SimpleNamespace(last_decision=5.0))
        plugin._agents = {"Al": agent}
        plugin.server = SimpleNamespace(broadcast_message=Mock())
        event = self.chat_event("@Al 挖石头", is_op=True)
        plugin.on_player_chat(event)
        agent.hear.assert_called_once()

    def test_bot_self_chat_ignored(self):
        # 自己人说话不触发（防互聊死循环）
        plugin = self.plugin_with(chance=1.0)
        agent = SimpleNamespace(
            hear=Mock(), alive=lambda: True,
            state=SimpleNamespace(last_decision=5.0))
        plugin._agents = {"Al": agent, "Bo": agent}
        event = self.chat_event("哈哈", name="Bo")     # Bo 是我们的假人
        plugin.on_player_chat(event)
        agent.hear.assert_not_called()

    def test_style_block_builds_and_empty(self):
        plugin = self.plugin_with(chance=0.5)
        plugin._bot_settings["Al"]["style"] = ["我是ENMO，我操我太喜欢这个游戏了",
                                               "这个服务器太勾八好玩了hhh"]
        block = plugin._style_block("Al")
        self.assertIn("## 说话风格", block)
        self.assertIn("我是ENMO", block)
        self.assertIn("模仿", block)
        # 没有风格 → 空串（提示词里不留空段）
        plugin._bot_settings["Nobody"] = {}
        self.assertEqual(plugin._style_block("Nobody"), "")

    def test_seed_does_not_override_existing(self):
        plugin = self.plugin_with(chance=0.5)
        plugin._save_bot_settings = Mock()
        plugin._seed_bot_settings("Al", chance=0.9, style=["a"])
        self.assertEqual(plugin._bot_settings["Al"]["chat_reply_chance"], 0.5)
        plugin._seed_bot_settings("New", chance=0.4, style=["x"])
        self.assertEqual(plugin._bot_settings["New"]["chat_reply_chance"], 0.4)
        self.assertEqual(plugin._bot_settings["New"]["style"], ["x"])

    def test_mention_word_boundary(self):
        # 词边界：叫 Al 的不会被 "alice"/"hello" 误命中；
        # 句中提到、中文包含都算点名
        from endstone_arc_fake_player.plugin import _mentions
        self.assertFalse(_mentions("alice hello", "Al"))
        self.assertFalse(_mentions("hello world", "Al"))
        self.assertTrue(_mentions("bosszhong 你在哪", "BossZhong"))
        self.assertTrue(_mentions("去问 BossZhong!", "BossZhong"))
        self.assertFalse(_mentions("bosszhongnb 太强", "BossZhong"))  # 贴字不算
        self.assertTrue(_mentions("阿青你在哪", "阿青"))


def main():
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for cls in (CardPersonaTests, ChatPersonaPluginTests):
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
