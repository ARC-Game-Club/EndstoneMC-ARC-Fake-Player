"""身份系统（头衔/公会/装备）的回归测试。

**背景（真 bug）**：3.9.39 只修了 `_apply_card_identity` 把角色卡当字典读，
但 `CharacterCard` 这个 dataclass **本身根本没有** title / title_rarity /
guild / guild_owner / gear 字段，`from_dict` 也不拷贝它们。
⇒ `crowd_cards()` 返回的对象用 `getattr(card, "title", None)` 读出来全是
None ⇒ 头衔/公会/装备**全部静默失效**，活动日志零记录。

本文件钉死这个行为：角色卡必须真的带上身份字段。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.endstone_stub import install as _install_stub  # noqa: E402

_install_stub()

from endstone_arc_fake_player.crowd import CharacterCard, load_cards  # noqa: E402


class TestCardIdentityFields(unittest.TestCase):
    """角色卡必须承载身份字段（否则插件读出来全是 None）。"""

    def test_from_dict_carries_identity_fields(self):
        card = CharacterCard.from_dict({
            "name": "TestBot",
            "title": "屠夫",
            "title_rarity": "普通",
            "guild": "测试公会",
            "guild_owner": True,
            "gear": "钻石",
        })
        self.assertIsNotNone(card)
        self.assertEqual(card.title, "屠夫")
        self.assertEqual(card.title_rarity, "普通")
        self.assertEqual(card.guild, "测试公会")
        self.assertTrue(card.guild_owner)
        self.assertEqual(card.gear, "钻石")

    def test_identity_defaults_are_empty(self):
        """没配身份的卡不该报错，字段留空。"""
        card = CharacterCard.from_dict({"name": "Plain"})
        self.assertIsNotNone(card)
        self.assertEqual(card.title, "")
        self.assertEqual(card.title_rarity, "")
        self.assertEqual(card.guild, "")
        self.assertFalse(card.guild_owner)
        self.assertEqual(card.gear, "")

    def test_getattr_reads_identity_fields(self):
        """复现插件里 `_card_field` 的读法：getattr 必须能拿到值。"""
        card = CharacterCard.from_dict({
            "name": "TestBot",
            "title": "屠夫",
            "title_rarity": "稀有",
            "guild": "测试公会",
            "guild_owner": True,
            "gear": "下界合金",
        })
        # 插件里 _card_field 对 dataclass 走 getattr 分支
        self.assertEqual(getattr(card, "title", None), "屠夫")
        self.assertEqual(getattr(card, "title_rarity", None), "稀有")
        self.assertEqual(getattr(card, "guild", None), "测试公会")
        self.assertEqual(getattr(card, "guild_owner", None), True)
        self.assertEqual(getattr(card, "gear", None), "下界合金")

    def test_load_cards_preserves_identity(self):
        cards = load_cards([
            {"name": "A", "title": "屠夫", "guild": "甲公会", "guild_owner": True},
            {"name": "B", "title": "水鬼克星", "guild": "甲公会", "gear": "铜"},
        ])
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0].title, "屠夫")
        self.assertTrue(cards[0].guild_owner)
        self.assertEqual(cards[1].title, "水鬼克星")
        self.assertEqual(cards[1].gear, "铜")
        self.assertFalse(cards[1].guild_owner)

    def test_guild_owner_accepts_string_bool(self):
        """配置里写成 "true" / "是" 也要认（JSON 手改常见）。"""
        for raw in ("true", "True", "1", "yes", "是"):
            card = CharacterCard.from_dict(
                {"name": "X", "guild": "G", "guild_owner": raw})
            self.assertTrue(card.guild_owner, "guild_owner=%r 应识别为 True" % raw)
        for raw in ("false", "0", "no", "否", ""):
            card = CharacterCard.from_dict(
                {"name": "X", "guild": "G", "guild_owner": raw})
            self.assertFalse(card.guild_owner, "guild_owner=%r 应识别为 False" % raw)

    def test_identity_fields_are_stripped(self):
        """字段前后空格要剪掉，避免配了却匹配不上头衔定义。"""
        card = CharacterCard.from_dict({
            "name": "X",
            "title": "  屠夫  ",
            "guild": " 测试公会 ",
            "gear": " 钻石 ",
        })
        self.assertEqual(card.title, "屠夫")
        self.assertEqual(card.guild, "测试公会")
        self.assertEqual(card.gear, "钻石")


class TestGearTiers(unittest.TestCase):
    """装备档位映射：中文档位名必须解析到正确的物品清单。"""

    def test_gear_aliases(self):
        from endstone_arc_fake_player.plugin import gear_items_for
        self.assertEqual(gear_items_for("无"), [])
        self.assertEqual(gear_items_for("裸装"), [])
        for tier in ("皮", "铜", "铁", "钻石", "下界合金"):
            items = gear_items_for(tier)
            self.assertTrue(items, "档位 %s 应有装备清单" % tier)
            self.assertIn("%s_sword" % {"皮": "stone", "铜": "copper", "铁": "iron",
                                        "钻石": "diamond",
                                        "下界合金": "netherite"}[tier], items)

    def test_unknown_tier_returns_none(self):
        from endstone_arc_fake_player.plugin import gear_items_for
        self.assertIsNone(gear_items_for("神器"))


if __name__ == "__main__":
    unittest.main()
