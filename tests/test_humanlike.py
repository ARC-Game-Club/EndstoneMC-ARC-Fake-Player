"""行为真人化大版本（3.9.17）的回归：熔断 / 狩猎 / 睡觉 / 交易 / 门控 / 作息。

全部纯逻辑、不碰服务器：世界用 FakeDimension，服务器端用 Mock。
对应 5 天活动日志里统计出的穿帮点，一个特征一个用例。
"""
from __future__ import annotations

import random
import sys
import time as _time_mod
import unittest


def time_now() -> float:
    return _time_mod.monotonic()

from pathlib import Path
import types
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.endstone_stub import install as _install_stub  # noqa: E402

_install_stub()

from endstone_arc_fake_player.actions import ActionCompiler  # noqa: E402
from endstone_arc_fake_player.agent import Agent, AgentConfig  # noqa: E402
from endstone_arc_fake_player.llm import LLMReply, ToolCall  # noqa: E402
from endstone_arc_fake_player.plugin import (  # noqa: E402
    ArcFakePlayerPlugin, _sanitize_speech, _strip_punct,
)
from endstone_arc_fake_player.roam import RoamDriver  # noqa: E402
from endstone_arc_fake_player.world import WorldAdapter  # noqa: E402

# ---- Fake 世界（同 test_pure_agent 的形状，砍到最小） --------------------


class FakeDimension:
    def __init__(self) -> None:
        self.blocks = {}
        self.name = "overworld"
        self.actors: list = []
        for x in range(-32, 32):
            for z in range(-32, 32):
                self.blocks[(x, 63, z)] = "minecraft:stone"

    def get_block_at(self, x, y, z):
        key = (int(x), int(y), int(z))
        name = self.blocks.get(key, "minecraft:air")
        # world.get_block_name 直接 short_id(block.type)——type 就是名字串
        return SimpleNamespace(type=name)


class FakeLocation:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class FakeActorType:
    def __init__(self, name):
        self.id = f"minecraft:{name}"


class FakeActor:
    def __init__(self, dim, x, y, z, kind="npc"):
        self.dimension = dim
        self.location = FakeLocation(x, y, z)
        self.name = "Bot"
        self.health = 20.0
        self.max_health = 20.0
        self.is_on_ground = True
        self.is_in_water = False
        self.is_in_lava = False
        self.is_valid = True
        self.type = FakeActorType(kind)
        self.id = id(self)
        self.inventory = None


class FakeLogger:
    def debug(self, *a):
        pass

    def info(self, *a):
        pass

    def warning(self, *a):
        pass

    def exception(self, *a):
        pass


def make_compiler(dim):
    world = WorldAdapter(FakeLogger(), surface_only=False)
    c = ActionCompiler(world, FakeLogger())
    return c


class HumanlikeTests(unittest.TestCase):
    # ---- 失败熔断 -----------------------------------------------------

    def _agent(self, dim=None):
        from endstone_arc_fake_player.executor import BatchExecutor
        dim = dim or FakeDimension()
        executor = BatchExecutor(FakeLogger())
        agent = Agent(
            config=AgentConfig(name="Bot"), llm=Mock(),
            world=WorldAdapter(FakeLogger(), surface_only=False),
            compiler=ActionCompiler(
                WorldAdapter(FakeLogger(), surface_only=False), FakeLogger()),
            executor=executor, logger=FakeLogger(),
            pool=Mock(), on_main=lambda fn: None)
        return agent

    def test_failure_streak_trips_after_three(self):
        agent = self._agent()
        kicked = []
        agent._on_stuck = lambda sig: kicked.append(sig)
        msg = "20 格内没找到 oak_log（背包现有 0/16）"
        agent._note_failure(msg)
        agent._note_failure("20 格内没找到 oak_log（背包现有 0/8）")  # 数字不同，签名相同
        self.assertEqual(len(agent.state.history), 0)
        agent._note_failure(msg)
        # 第 3 次：历史里出现纠正 + 插件钩子被调用 + 计数清零
        self.assertTrue(any("连续失败" in h["content"]
                            for h in agent.state.history))
        self.assertEqual(len(kicked), 1)
        self.assertNotIn(agent._failure_sig(msg), agent.state.fail_streaks)

    def test_failure_sig_ignores_numbers(self):
        self.assertEqual(Agent._failure_sig("goto: 找不到通往 (12, 64, -8) 的路径"),
                         Agent._failure_sig("goto: 找不到通往 (-100, 70, 2000) 的路径"))

    def test_failure_streak_window_expires(self):
        agent = self._agent()
        msg = "x"
        sig = agent._failure_sig(msg)
        import time as _time
        # 10 分钟前 failed 三次——早已滑出窗口
        agent.state.fail_streaks[sig] = [
            _time.monotonic() - 700.0 for _ in range(3)]
        agent._note_failure(msg)
        self.assertEqual(len(agent.state.fail_streaks.get(sig, [])), 1)
        self.assertEqual(len(agent.state.history), 0)

    # ---- 空转治理 -------------------------------------------------------

    def test_empty_reply_stretches_gap(self):
        agent = self._agent()
        agent.config.decision_interval_max = 300.0
        actor = FakeActor(FakeDimension(), 0, 64, 0)
        agent._get_actor = lambda: actor
        reply = LLMReply(ok=True, text="嗯", calls=[])
        agent._apply(reply, "p")
        self.assertEqual(agent.state.empty_turns, 1)
        agent.state.decision_gap = 100.0
        agent._apply(reply, "p")
        self.assertEqual(agent.state.empty_turns, 2)
        self.assertGreaterEqual(agent.state.decision_gap, 200.0)
        # 有动作就复位
        agent.state.empty_turns = 5
        agent._apply(LLMReply(ok=True, text="", calls=[
            ToolCall("say", {"message": "hi"})]), "p")
        self.assertEqual(agent.state.empty_turns, 0)

    # ---- 挖矿改造 -------------------------------------------------------

    def test_mine_area_capped_at_48(self):
        dim = FakeDimension()
        compiler = make_compiler(dim)
        actor = FakeActor(dim, 0, 64, 0)
        r = compiler.compile(actor, ToolCall("mine_area", {
            "x1": 0, "y1": 55, "z1": 0, "x2": 9, "y2": 64, "z2": 9}), {})
        # 10x10x10 全是石头实心（地面上下都有）→ 远超 48
        self.assertIsNotNone(r.plan)
        self.assertEqual(r.plan.total, 48)
        self.assertIn("先挖最近的", r.immediate)

    # ---- 狩猎 -----------------------------------------------------------

    def test_hunt_walks_to_nearest_animal(self):
        dim = FakeDimension()
        cow = FakeActor(dim, 8, 64, 8, kind="cow")
        dim.actors.append(cow)
        compiler = make_compiler(dim)
        actor = FakeActor(dim, 0, 64, 0)
        r = compiler.compile(actor, ToolCall("hunt", {}), {})
        self.assertIsNotNone(r.plan, r.immediate)
        first = r.plan.steps[0]
        self.assertEqual(first.op, "walk_to")
        self.assertEqual((first.x, first.y, first.z), (8, 64, 8))
        self.assertEqual(r.plan.steps[-1].note, "cow")

    def test_hunt_without_animals_gives_direction(self):
        compiler = make_compiler(FakeDimension())
        actor = FakeActor(FakeDimension(), 0, 64, 0)
        r = compiler.compile(actor, ToolCall("hunt", {"target": "cow"}), {})
        self.assertIsNone(r.plan)
        self.assertIn("没有可猎取", r.immediate)

    # ---- 睡觉 -----------------------------------------------------------

    def test_sleep_without_bed_tells_how_to_make_one(self):
        compiler = make_compiler(FakeDimension())
        actor = FakeActor(FakeDimension(), 0, 64, 0)
        r = compiler.compile(actor, ToolCall("sleep", {}), {})
        self.assertIsNone(r.plan)
        self.assertIn("羊毛", r.immediate)

    def test_sleep_walks_to_bed(self):
        dim = FakeDimension()
        dim.blocks[(3, 64, 3)] = "minecraft:bed"
        compiler = make_compiler(dim)
        actor = FakeActor(dim, 0, 64, 0)
        r = compiler.compile(actor, ToolCall("sleep", {}), {})
        self.assertIsNotNone(r.plan, r.immediate)
        self.assertEqual([s.op for s in r.plan.steps], ["walk_to", "sleep"])

    # ---- 交易 -----------------------------------------------------------

    def test_offer_trade_compile_validation(self):
        dim = FakeDimension()
        compiler = make_compiler(dim)
        actor = FakeActor(dim, 0, 64, 0)
        compiler.bind_player_lookup(lambda n: object())
        base = {"player": "Steve", "mode": "sell", "item": "iron_ingot",
                "count": 4, "price": 100}
        ok = compiler.compile(actor, ToolCall("offer_trade", dict(base)), {})
        self.assertIsNotNone(ok.plan)
        self.assertEqual(ok.plan.steps[0].op, "offer_trade")
        for bad, kwargs in {
            "mode 非法": {"mode": "swap"},
            "价格 0": {"price": 0},
            "缺物品": {"item": ""},
        }.items():
            args = dict(base)
            args.update(kwargs)
            r = compiler.compile(actor, ToolCall("offer_trade", args), {})
            self.assertIsNone(r.plan, bad)

    def test_offer_trade_offline_player_rejected_at_compile(self):
        compiler = make_compiler(FakeDimension())
        actor = FakeActor(FakeDimension(), 0, 64, 0)
        compiler.bind_player_lookup(lambda n: None)
        r = compiler.compile(actor, ToolCall("offer_trade", {
            "player": "Ghost", "mode": "sell", "item": "log",
            "count": 1, "price": 5}), {})
        self.assertIsNone(r.plan)
        self.assertIn("不在线", r.immediate)

    # ---- gs 门控 ---------------------------------------------------------

    def test_gs_absent_from_tool_description_without_shooter(self):
        """没装枪战：run_command 的说明里**一个 gs 都不许出现**——
        负向提示（"gs 不可用别尝试"）等于把命令名教给模型。"""
        from endstone_arc_fake_player.llm import LLMClient, set_shooter_available
        set_shooter_available(False)
        try:
            desc = [i for i in LLMClient.tool_spec()
                    if i["function"]["name"] == "run_command"][0]["function"]["description"]
            self.assertNotIn("gs", desc.lower())
            manual = LLMClient.tool_manual()
            # 手册同样不该在无枪战时出现 gs 命令示例
            self.assertNotIn("gs quick", manual)
        finally:
            set_shooter_available(True)

    def test_gs_blocked_when_no_shooter(self):
        dim = FakeDimension()
        compiler = make_compiler(dim)
        compiler.shooter_available = False
        actor = FakeActor(dim, 0, 64, 0)
        r = compiler.compile(actor, ToolCall("run_command", {"command": "gs quick"}), {})
        self.assertIsNone(r.plan)
        self.assertIn("没有装枪战插件", r.immediate)
        # 别的命令不受影响
        r2 = compiler.compile(actor, ToolCall("run_command", {"command": "list"}), {})
        self.assertIsNotNone(r2.plan)

    # ---- 模糊玩家名 -------------------------------------------------------

    def _plugin_with_players(self, names):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        roster = {n: SimpleNamespace(name=n, xuid="1") for n in names}
        plugin.server = SimpleNamespace(
            get_player=lambda n: roster.get(n),
            online_players=list(roster.values()))
        return plugin

    def test_fuzzy_player_match(self):
        p = self._plugin_with_players(["Steve", "Alex_ZH", "qianduoduo66"])
        self.assertEqual(p._resolve_online_player("Steve").name, "Steve")
        self.assertEqual(p._resolve_online_player("steve").name, "Steve")  # 大小写
        self.assertEqual(p._resolve_online_player("qianduoduo666").name, "qianduoduo66")  # 打多一个字
        self.assertEqual(p._resolve_online_player("AlexZH").name, "Alex_ZH")  # 漏个符号
        self.assertIsNone(p._resolve_online_player("player"))  # 字面占位名不猜
        self.assertIsNone(p._resolve_online_player("完全不相关"))

    # ---- 昼夜标签 ---------------------------------------------------------

    def test_daynight_label(self):
        f = __import__("endstone_arc_fake_player.world", fromlist=["x"]).WorldAdapter.daynight_label
        self.assertEqual(f(0), "白天")
        self.assertEqual(f(12541), "白天")
        self.assertEqual(f(12542), "夜晚")
        self.assertEqual(f(18000), "夜晚")
        self.assertEqual(f(23459), "夜晚")
        self.assertEqual(f(23460), "白天")
        self.assertEqual(f(12542 + 24000), "夜晚")  # 跨天取模
        self.assertIsNone(f(None))

    # ---- 闲逛带离 ---------------------------------------------------------

    def test_roam_kick_far_walks_away(self):
        walks = []

        def walk(name, x, y, z):
            walks.append((x, z))
            return True

        drv = RoamDriver(
            names=lambda: ["Bot"], busy=lambda n: False,
            anchor=lambda n: (0.0, 64.0, 0.0, "家"),
            bot_pos=lambda n: (0.0, 64.0, 0.0),
            surface=lambda x, z, near_y: 64.0,
            walk=walk, dig=lambda n, x, y, z: True,
            log=lambda m: None, rng=random.Random(7))
        drv.kick_far("Bot")
        self.assertTrue(drv.state_of("Bot").forced_far)
        drv.tick()
        self.assertEqual(len(walks), 1)
        dist = (walks[0][0] ** 2 + walks[0][1] ** 2) ** 0.5
        self.assertGreaterEqual(dist, 40.0)
        self.assertLessEqual(dist, 120.0)
        self.assertFalse(drv.state_of("Bot").forced_far)  # 消费即清

    # ---- 作息曲线 ---------------------------------------------------------

    def test_night_target_downscaled(self):
        import datetime
        from endstone_arc_fake_player.crowd import CrowdScheduler
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        cfg = {"target_count": 5, "night_target_count": 1}
        plugin.crowd_config = lambda: cfg
        sched = CrowdScheduler(plugin, rng=random.Random(1))
        self.assertEqual(sched.target_count, 5)
        real_now = datetime.datetime
        real_hour = real_now.now().hour

        class _FakeDT:
            @staticmethod
            def now():
                fake = real_now(2026, 1, 1, 3, 0, 0)  # 凌晨 3 点
                return fake

        datetime.datetime = _FakeDT
        try:
            self.assertEqual(sched.effective_target, 1)
        finally:
            datetime.datetime = real_now
        self.assertEqual(sched.effective_target, 5 if real_hour not in CrowdScheduler.NIGHT_HOURS else 1)

    # ---- 说话清洗（JSON 兜底残渣） ----------------------------------

    def test_sanitize_strips_structural_leftovers(self):
        cases = [
            # 整个 JSON 动作塞进了 message
            ('{"reply":"你好","actions":[]}', "你好"),
            ('{"name":"say","args":{"message":"挖到钻石了"}}', "挖到钻石了"),
            ('[{"reply":"排队"}]', "排队"),
            # XML 风格标签
            ("<message>你好</message>", "你好"),
            ("你好<message/>", "你好"),
            ("挖完了<message/>好累", "挖完了 好累"),
            # Python 字典字符串（clean_args 把 dict str() 化的产物）
            ("{'message': '来了'}", "来了"),
            # 两端悬空的残渣
            ('你好"}', "你好"),
            ('{"草', "草"),
            ("来了\",", "来了"),
            ('"reply": "今晚下矿"', "今晚下矿"),
            # 正常话不受影响
            ("这波我的！", "这波我的！"),
            ("风水轮流转{哈哈}继续", "风水轮流转{哈哈}继续"),  # 中间括号保留
            ("x<y不大于", "x<y不大于"),  # 非标签的尖括号保留
        ]
        for raw, want in cases:
            got = _sanitize_speech(raw)
            self.assertEqual(got, want, f"输入 {raw!r} 期望 {want!r} 实得 {got!r}")

    def test_sanitize_drops_pure_leftovers(self):
        for raw in ("<message/>", "{}", '""', "", "   "):
            self.assertEqual(_sanitize_speech(raw), "", raw)

    # ---- 说话去标点（v3.9.20） ---------------------------------------

    def test_strip_punct(self):
        cases = [
            ("挖到钻石了！", "挖到钻石了"),
            ("好耶~", "好耶"),
            ("go go go!", "go go go"),
            ("233。", "233"),
            ("（小声）你说啥？", "小声你说啥"),
            ("来了来了~哈", "来了来了哈"),
            ("", ""),
            ("！！！", ""),
            ("x<y", "xy"),
        ]
        for raw, want in cases:
            self.assertEqual(_strip_punct(raw), want, raw)

    # ---- 核心聊天格式（v3.9.20） --------------------------------------

    def _chat_plugin(self):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._identities = SimpleNamespace(xuid_of=lambda n: "9372854858298499")
        plugin._qq_sync = Mock()
        plugin._activity = Mock()
        sent: list = []
        core = Mock()
        core.format_player_display_label_with_guild = Mock(
            return_value="§9[金算盘]§r§2[大金商行]§rqianduoduo66")
        ts = Mock()
        ts.get_equipped_title_by_xuid = Mock(return_value="金算盘")
        core.title_system = ts
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=core)),
            broadcast_message=sent.append)
        return plugin, sent

    def test_core_chat_format_two_lines(self):
        plugin, sent = self._chat_plugin()
        plugin._send_chat("qianduoduo66", "收木头一个价")
        self.assertEqual(len(sent), 1)
        msg = sent[0]
        self.assertTrue(msg.startswith("§9[金算盘]§r§2[大金商行]§rqianduoduo66("), msg)
        self.assertTrue(msg.endswith(")：\n收木头一个价"), msg)

    def test_broadcast_strips_punctuation_end_to_end(self):
        plugin, sent = self._chat_plugin()
        plugin._broadcast("qianduoduo66", "挖到钻石了！大家快来看！")
        self.assertTrue(sent)
        for m in sent:
            self.assertNotIn("！", m)
        self.assertTrue(any("挖到钻石了" in m for m in sent))

    # ---- 公会身份（v3.9.20） ------------------------------------------

    def test_bot_creates_guild_when_absent(self):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._activity = Mock()
        calls: list = []
        gs = Mock()
        gs.get_membership = Mock(return_value=None)
        gs.get_guild_by_name = Mock(return_value=None)
        gs.create_guild = Mock(
            side_effect=lambda n, x, motto="": (calls.append((n, x)), (True, None))[1])
        gp = Mock()
        gp.guild_system = gs
        gp._refresh_player_name_tag_by_xuid = Mock()
        # 本用例专测旧路径回退：抹掉 Mock 自动生成的免费 API 属性
        del gp.api_bot_create_guild
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=gp)))
        plugin._bot_join_guild("BossZhong", "9257971652503711", "铜剑帮", owner=True)
        self.assertEqual(calls, [("铜剑帮", "9257971652503711")])

    def test_bot_joins_existing_guild_via_owner(self):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._activity = Mock()
        gs = Mock()
        gs.get_membership = Mock(return_value=None)
        gs.get_guild_by_name = Mock(
            return_value={"id": 7, "owner_xuid": "9257971652503711"})
        gs.join_via_live_invite = Mock(return_value=(True, None))
        gp = Mock()
        gp.guild_system = gs
        gp._refresh_player_name_tag_by_xuid = Mock()
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=gp)))
        plugin._bot_join_guild("SuperBoyYiChen", "9604489171753731", "铜剑帮")
        gs.join_via_live_invite.assert_called_once_with(
            "9604489171753731", 7, "9257971652503711")

    def test_bot_already_in_guild_is_noop(self):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._activity = Mock()
        gs = Mock()
        gs.get_membership = Mock(return_value={"guild_id": 3})
        gp = Mock()
        gp.guild_system = gs
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=gp)))
        plugin._bot_join_guild("BossZhong", "9257971652503711", "铜剑帮")
        gs.create_guild.assert_not_called()
        gs.join_via_live_invite.assert_not_called()

    def test_member_bot_waits_for_owner_guild(self):
        """成员卡遇到公会不存在：不抢建，等会长（防'张少校的卫队被治安队长开走'）。"""
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._activity = Mock()
        gs = Mock()
        gs.get_membership = Mock(return_value=None)
        gs.get_guild_by_name = Mock(return_value=None)
        gs.create_guild = Mock()
        gp = Mock()
        gp.guild_system = gs
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=gp)))
        plugin._bot_join_guild("SuperBoyYiChen", "9604489171753731",
                               "弧光卫队", owner=False)
        gs.create_guild.assert_not_called()

    # ---- 头衔只认系统已有定义（v3.9.22） ------------------------------

    def _title_plugin(self, has_def: bool):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._activity = Mock()
        plugin._identities = SimpleNamespace(xuid_of=lambda n: "9257971652503711")
        ts = Mock()
        ts.has_title_definition = Mock(return_value=has_def)
        ts.get_equipped_title_by_xuid = Mock(return_value=None)
        core = Mock()
        core.title_system = ts
        core.api_unlock_title_by_xuid = Mock()
        core._update_player_name_tag = Mock()
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=core)),
            online_players=[])
        return plugin, core, ts

    def test_existing_title_unlocked_and_equipped(self):
        plugin, core, ts = self._title_plugin(has_def=True)
        plugin.crowd_cards = lambda: [{"name": "BossZhong", "goal": "x", "title": "十人斩"}]
        plugin._apply_card_identity("BossZhong", Mock())
        core.api_unlock_title_by_xuid.assert_called_once()
        ts.set_equipped_title_by_xuid.assert_called_once()

    # ---- 出生点对齐世界（v3.9.36） -------------------------------------

    def _spawn_pos_plugin(self, *, home=None, world_spawn=(5, 64, 7)):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._activity = Mock()
        plugin._spawn_xz_cache = (world_spawn[0], world_spawn[2])
        plugin._world = WorldAdapter(FakeLogger(), surface_only=False)
        plugin.crowd_config = lambda: ({"home": home} if home else {})
        dim = FakeDimension()
        plugin.server = SimpleNamespace(
            online_players=[SimpleNamespace(
                xuid="1", location=FakeLocation(-500, 70, -900))],
            level=SimpleNamespace(get_dimension=lambda n: dim))
        return plugin

    def test_spawn_prefers_world_spawn_over_players(self):
        """世界出生点优先于真人身边——出生/复活对齐（用户规矩）。"""
        plugin = self._spawn_pos_plugin(world_spawn=(5, 64, 7))
        pos = plugin._crowd_spawn_pos()
        self.assertIsNotNone(pos)
        x, y, z, grounded = pos
        self.assertEqual((x, z), (5, 7))            # 世界出生点，不是真人旁
        self.assertTrue(grounded)

    def test_spawn_config_home_wins(self):
        plugin = self._spawn_pos_plugin(home=[7, 65, 9])
        pos = plugin._crowd_spawn_pos()
        self.assertEqual(pos[:3], (7, 65, 9))

    def test_spawn_falls_back_to_player_when_no_world_spawn(self):
        plugin = self._spawn_pos_plugin()
        plugin._spawn_xz_cache = None
        # _world_spawn_xz 会尝试读 level.dat——stub 下走缓存 None 后返回 None
        plugin._world_spawn_xz = lambda: None
        pos = plugin._crowd_spawn_pos()
        self.assertIsNotNone(pos)
        x, y, z, grounded = pos
        self.assertTrue(abs(x - (-500)) <= 4)      # 真人身边兜底

    # ---- 种地 farm（v3.9.35） ------------------------------------------

    def _farm_compiler(self, inv, ground="grass_block"):
        dim = FakeDimension()
        for x in range(-3, 4):
            for z in range(-3, 4):
                dim.blocks[(x, 63, z)] = f"minecraft:{ground}"
        compiler = make_compiler(dim)
        actor = FakeActor(dim, 0, 64, 0)      # 脚下 y63 是地面
        compiler.world.scan_inventory = Mock(return_value=list(inv))
        return compiler, actor

    def test_farm_till_then_plant(self):
        compiler, actor = self._farm_compiler([("wooden_hoe", 1),
                                               ("wheat_seeds", 30)])
        r = compiler.compile(actor, ToolCall("farm", {"size": 2}), {})
        self.assertIsNotNone(r.plan, r.immediate)
        ops = [s.op for s in r.plan.steps]
        self.assertEqual(ops.count("till"), 25)       # 5x5=25 格
        self.assertEqual(ops.count("plant"), 25)
        self.assertEqual(ops.index("plant"), ops.index("till") + 25)  # 先耕后种

    def test_farm_rejected_without_hoe(self):
        compiler, actor = self._farm_compiler([("wheat_seeds", 30)])
        r = compiler.compile(actor, ToolCall("farm", {}), {})
        self.assertIsNone(r.plan)
        self.assertIn("锄头", r.immediate)

    def test_farm_rejected_without_seeds(self):
        compiler, actor = self._farm_compiler([("stone_hoe", 1)])
        r = compiler.compile(actor, ToolCall("farm", {}), {})
        self.assertIsNone(r.plan)
        self.assertIn("short_grass", r.immediate)      # 教打草拿种子

    def test_farm_seeds_cap_plot_size(self):
        compiler, actor = self._farm_compiler([("iron_hoe", 1),
                                               ("wheat_seeds", 4)])
        r = compiler.compile(actor, ToolCall("farm", {"size": 3}), {})
        self.assertIsNotNone(r.plan)
        self.assertEqual(r.plan.total, 8)              # 4 格 ×（耕+种）

    # ---- 挖矿要持镐（v3.9.34） -----------------------------------------

    def test_collect_stone_without_pickaxe_rejected(self):
        dim = FakeDimension()
        compiler = make_compiler(dim)
        compiler._find_blocks = Mock(return_value=[(1, 63, 1)])
        actor = FakeActor(dim, 0, 64, 0)
        compiler.world.scan_inventory = Mock(return_value=[("stick", 2)])
        r = compiler.compile(actor, ToolCall("collect", {"block": "cobblestone", "count": 8}), {})
        self.assertIsNone(r.plan)
        self.assertIn("镐子", r.immediate)
        self.assertIn("chop_tree", r.immediate)

    def test_collect_stone_with_pickaxe_passes(self):
        dim = FakeDimension()
        compiler = make_compiler(dim)
        compiler._find_blocks = Mock(return_value=[(1, 63, 1)])
        actor = FakeActor(dim, 0, 64, 0)
        compiler.world.scan_inventory = Mock(return_value=[("stone_pickaxe", 1)])
        r = compiler.compile(actor, ToolCall("collect", {"block": "cobblestone", "count": 8}), {})
        self.assertIsNotNone(r.plan, r.immediate)

    def test_collect_dirt_bare_hands_allowed(self):
        dim = FakeDimension()
        compiler = make_compiler(dim)
        compiler._find_blocks = Mock(return_value=[(1, 63, 1)])
        actor = FakeActor(dim, 0, 64, 0)
        compiler.world.scan_inventory = Mock(return_value=[])
        r = compiler.compile(actor, ToolCall("collect", {"block": "dirt", "count": 4}), {})
        self.assertIsNotNone(r.plan, r.immediate)      # 泥土空手能挖

    def test_mine_vein_stone_needs_pickaxe(self):
        dim = FakeDimension()
        compiler = make_compiler(dim)
        compiler._find_blocks = Mock(return_value=[(2, 63, 2)])
        actor = FakeActor(dim, 0, 64, 0)
        compiler.world.scan_inventory = Mock(return_value=[])
        r = compiler.compile(actor, ToolCall("mine_vein", {"block": "iron_ore"}), {})
        self.assertIsNone(r.plan)
        self.assertIn("木镐", r.immediate)

    # ---- 角色卡字段兼容（v3.9.39：修身份全失效的根因） ------------------

    def test_card_field_dict_and_object(self):
        from endstone_arc_fake_player.plugin import ArcFakePlayerPlugin as P
        self.assertEqual(P._card_field({"name": "A"}, "name"), "A")
        self.assertEqual(P._card_field({"name": "A"}, "guild", None), None)
        obj = types.SimpleNamespace(name="B", guild="钟氏财团", guild_owner=True)
        self.assertEqual(P._card_field(obj, "name"), "B")
        self.assertEqual(P._card_field(obj, "guild"), "钟氏财团")
        self.assertTrue(P._card_field(obj, "guild_owner"))
        self.assertIsNone(P._card_field(obj, "gear"))

    def test_identity_applies_with_object_card(self):
        """crowd_cards 返回 CharacterCard 对象——身份应用必须能读（3.9.38
        及以前把它当 dict，AttributeError 被静默吞，公会/头衔全没生效）。"""
        plugin, core, ts = self._title_plugin(has_def=True)
        card = types.SimpleNamespace(name="BossZhong", goal="x",
                                     title="十人斩", guild="", guild_owner=False)
        plugin.crowd_cards = lambda: [card]
        plugin._apply_card_identity("BossZhong", Mock())
        core.api_unlock_title_by_xuid.assert_called_once()

    def test_guild_create_prefers_free_bot_api(self):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._activity = Mock()
        gs = Mock()
        gs.get_membership = Mock(return_value=None)
        gs.get_guild_by_name = Mock(return_value=None)
        gs.create_guild = Mock(return_value=(True, None))
        gp = Mock()
        gp.guild_system = gs
        gp.api_bot_create_guild = Mock(return_value=(True, None))
        gp._refresh_player_name_tag_by_xuid = Mock()
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=gp)))
        plugin._bot_join_guild("BossZhong", "9257971652503711",
                               "钟氏财团", owner=True)
        gp.api_bot_create_guild.assert_called_once()      # 走免费 API
        gs.create_guild.assert_not_called()               # 不走扣费路径

    def test_guild_create_falls_back_without_bot_api(self):
        """旧版公会插件没有免费 API → 退回普通建会（可能扣费）。"""
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._activity = Mock()
        gs = Mock()
        gs.get_membership = Mock(return_value=None)
        gs.get_guild_by_name = Mock(return_value=None)
        gs.create_guild = Mock(return_value=(True, None))
        gp = Mock()
        gp.guild_system = gs
        gp._refresh_player_name_tag_by_xuid = Mock()
        del gp.api_bot_create_guild
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=gp)))
        plugin._bot_join_guild("BossZhong", "9257971652503711",
                               "钟氏财团", owner=True)
        gs.create_guild.assert_called_once()

    # ---- 战力装备档位（v3.9.33） ---------------------------------------

    def test_gear_tier_mapping(self):
        from endstone_arc_fake_player.plugin import gear_items_for, GEAR_TIERS
        self.assertEqual(gear_items_for("无"), [])
        self.assertEqual(gear_items_for("萌新"), [])
        self.assertEqual(gear_items_for("leather"), GEAR_TIERS["leather"])
        copper = gear_items_for("铜")
        self.assertTrue(any("copper_helmet" in i for i in copper))
        iron = gear_items_for("铁")
        self.assertTrue(any("shield" in i for i in iron))
        diamond = gear_items_for("钻石")
        self.assertTrue(any("diamond_sword" in i for i in diamond))
        nx = gear_items_for("下界合金")
        self.assertTrue(any("netherite_helmet" in i for i in nx))
        self.assertIsNone(gear_items_for("圣剑"))          # 未知档位

    def _gear_plugin(self, inv_names, card_gear="钻石"):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._activity = Mock()
        plugin.crowd_cards = lambda: [{"name": "Bot", "goal": "x",
                                       "gear": card_gear}]
        actor = FakeActor(FakeDimension(), 0, 64, 0)
        plugin._live_actor = Mock(return_value=actor)
        plugin._world = Mock()
        plugin._world.scan_inventory = Mock(
            return_value=[(n, 1) for n in inv_names])
        given: list = []
        plugin._dispatch = Mock(side_effect=lambda cmd:
                                given.append(cmd) or True)
        plugin._record = Mock()
        return plugin, given

    def test_apply_gear_gives_only_missing(self):
        plugin, given = self._gear_plugin(
            ["diamond_helmet", "diamond_chestplate"])   # 已有两件
        plugin._apply_gear("Bot")
        joined = " | ".join(given)
        self.assertNotIn("diamond_helmet", joined)       # 有的不发
        self.assertNotIn("diamond_chestplate", joined)
        self.assertIn("diamond_leggings", joined)        # 缺的补上
        self.assertIn("diamond_sword", joined)
        self.assertEqual(len(given), 3)                  # 5 件装备补 3 件

    def test_apply_gear_none_tier_gives_nothing(self):
        plugin, given = self._gear_plugin([], card_gear="无")
        plugin._apply_gear("Bot")
        self.assertEqual(given, [])

    def test_apply_gear_no_card_no_give(self):
        plugin, given = self._gear_plugin([])
        plugin.crowd_cards = lambda: []
        plugin._apply_gear("Bot")
        self.assertEqual(given, [])

    # ---- 主动追杀（v3.9.31） -------------------------------------------

    def _hunt_plugin(self, dist_to_target=500.0):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._activity = Mock()
        plugin._cfg = {"social": {"enabled": True, "grudge_threshold": -30.0,
                                  "vengeance_radius": 40.0, "vengeance_hunt": True,
                                  "reflex_duration_seconds": 90.0}}
        plugin._sleeping = {}
        plugin._hunt_next = {}
        plugin._hunt_until = {}
        plugin._hunt_told = set()
        plugin._vengeance_cd = {}
        plugin._favor_next = {}
        walks: list = []
        plugin._bridge = Mock()
        plugin._bridge.walk = Mock(side_effect=lambda n, x, y, z, timeout=0:
                                   walks.append((n, x, z)) or 1)
        plugin._reflex = Mock()
        plugin._reflex.is_engaged = Mock(return_value=False)
        plugin._favor = Mock()
        plugin._favor.grudges = Mock(return_value=["Killer"])
        plugin._favor.describe = Mock(return_value="")
        plugin._human_pos = Mock(return_value=(dist_to_target, 64, 0))
        plugin._roam_surface = Mock(return_value=64.0)
        plugin._roam_busy = Mock(return_value=False)
        agent = Mock()
        agent.note_events = Mock()
        plugin._agents = {"Bot": agent}
        plugin._executor = Mock()
        actor = FakeActor(FakeDimension(), 0, 64, 0)
        return plugin, walks, actor, agent

    def test_hunt_walks_toward_online_grudge(self):
        import time as _t
        plugin, walks, actor, agent = self._hunt_plugin()
        plugin._social_tick("Bot", agent, actor, _t.monotonic())
        self.assertEqual(len(walks), 1)          # 朝仇人方向发了一腿
        self.assertEqual(walks[0][0], "Bot")
        # 腿的落点在假人与仇人连线方向上（x 远大于 0）
        self.assertGreater(walks[0][1], 40)
        # 宣言只发一次
        plugin._social_tick("Bot", agent, actor, _t.monotonic() + 100)
        told = [c.args for c in agent.note_events.call_args_list
                if "算账" in str(c)]
        self.assertEqual(len(told), 1)

    def test_hunt_yields_when_busy_or_engaged(self):
        import time as _t
        plugin, walks, actor, agent = self._hunt_plugin()
        plugin._roam_busy = Mock(return_value=True)
        plugin._social_tick("Bot", agent, actor, _t.monotonic())
        self.assertEqual(walks, [])              # 忙（有计划/闲逛占身）不追
        plugin._roam_busy = Mock(return_value=False)
        plugin._reflex.is_engaged = Mock(return_value=True)
        plugin._social_tick("Bot", agent, actor, _t.monotonic())
        self.assertEqual(walks, [])              # 已在打（就近开战接管）

    def test_hunt_disabled_by_config(self):
        import time as _t
        plugin, walks, actor, agent = self._hunt_plugin()
        plugin._cfg["social"]["vengeance_hunt"] = False
        plugin._social_tick("Bot", agent, actor, _t.monotonic())
        self.assertEqual(walks, [])

    # ---- 反击追击（v3.9.41） -------------------------------------------

    def test_reflex_chases_when_out_of_reach(self):
        """够不着 → 向怪的位置走（远程怪不然永远站 4.5 格外白嫖）。"""
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        walks: list = []
        plugin._bridge = Mock()
        plugin._bridge.attack = Mock(
            side_effect=lambda n, t, callback=None: (
                callback and callback(False, "zombie 距离 8.0 格，够不着", {}),
                1)[-1])
        plugin._bridge.walk = Mock(
            side_effect=lambda n, x, y, z, timeout=0: walks.append((n, x, y, z)) or 1)
        plugin._attack_target = Mock(return_value="zombie")
        plugin._mob_pos = Mock(return_value=(10, 64, -20))
        ok = plugin._reflex_attack("Bot", "123")
        self.assertTrue(ok)
        import time as _t
        _t.sleep(0.01)          # 回调同步执行（Mock 直接调）
        self.assertEqual(walks, [("Bot", 10, 64, -20)])

    def test_reflex_no_chase_on_other_errors(self):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        walks: list = []
        plugin._bridge = Mock()
        plugin._bridge.attack = Mock(
            side_effect=lambda n, t, callback=None: (
                callback and callback(False, "附近没有 zombie", {}),
                1)[-1])
        plugin._bridge.walk = Mock(
            side_effect=lambda n, x, y, z, timeout=0: walks.append(1) or 1)
        plugin._attack_target = Mock(return_value="zombie")
        plugin._reflex_attack("Bot", "123")
        import time as _t
        _t.sleep(0.01)
        self.assertEqual(walks, [])            # 目标没了不追

    # ---- 泡水/死亡循环检测（v3.9.40） -----------------------------------

    def _water_plugin(self, in_water=True, engaged=False, sleeping=False):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._activity = Mock()
        plugin._water_since = {}
        plugin._rescue_at = {}
        plugin._agents = {"Bot": Mock()}
        actor = FakeActor(FakeDimension(), 0, 64, 0)
        actor.is_in_water = in_water
        plugin._live_actor = Mock(return_value=actor)
        plugin._reflex = Mock()
        plugin._reflex.is_engaged = Mock(return_value=engaged)
        plugin._sleeping = {"Bot": True} if sleeping else {}
        rescued = []
        plugin._rescue_bot = Mock(side_effect=lambda n: rescued.append(n))
        return plugin, rescued

    def test_water_stuck_triggers_rescue(self):
        import time as _t
        plugin, rescued = self._water_plugin()
        plugin._water_stuck_check()                       # 记首时刻
        plugin._water_since["Bot"] = _t.monotonic() - 31.0  # 直接泡了 31 秒
        plugin._water_stuck_check()
        self.assertEqual(rescued, ["Bot"])

    def test_water_stuck_requires_sustained(self):
        plugin, rescued = self._water_plugin()
        plugin._water_stuck_check()                       # 刚入水，不救
        self.assertEqual(rescued, [])

    def test_water_stuck_ignored_when_fighting_or_asleep(self):
        import time as _t
        plugin, rescued = self._water_plugin(engaged=True)
        plugin._water_stuck_check()
        plugin._water_since["Bot"] = _t.monotonic() - 60.0
        plugin._water_stuck_check()
        self.assertEqual(rescued, [])                     # 打架中不救
        plugin2, rescued2 = self._water_plugin(sleeping=True)
        plugin2._water_stuck_check()
        plugin2._water_since["Bot"] = _t.monotonic() - 60.0
        plugin2._water_stuck_check()
        self.assertEqual(rescued, [])                     # 睡觉不救

    def test_water_stuck_fires_even_when_roam_busy(self):
        """关键：闲逛走位中的 busy 不能挡泡水检测——绕圈走正是被困形态。"""
        import time as _t
        plugin, rescued = self._water_plugin()
        plugin._roam_busy = Mock(return_value=True)       # 模拟"正在走"
        plugin._water_stuck_check()
        plugin._water_since["Bot"] = _t.monotonic() - 31.0
        plugin._water_stuck_check()
        self.assertEqual(rescued, ["Bot"])

    def test_death_loop_schedules_rescue(self):
        import time as _t
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._activity = Mock()
        plugin._death_times = {}
        plugin._rescue_at = {}
        plugin._agents = {}
        scheduled = []
        plugin.server = SimpleNamespace(
            scheduler=SimpleNamespace(
                run_task=Mock(side_effect=lambda p, fn, delay=0:
                              scheduled.append(delay))))
        for _ in range(3):
            plugin._note_bot_death("Bot")
        self.assertEqual(scheduled, [240])                # 12 秒后排脱离传送
        self.assertEqual(plugin._death_times, {})          # 计数已清零
        # 两次不触发
        scheduled.clear()
        for _ in range(2):
            plugin._note_bot_death("Bot")
        self.assertEqual(scheduled, [])

    # ---- 受困救援（v3.9.29） -------------------------------------------

    def _rescue_plugin(self, warps, ok=True):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._activity = Mock()
        plugin._roam = Mock()
        plugin._walk_fails = {}
        plugin._rescue_at = {}
        plugin._agents = {}
        plugin._live_actor = Mock(return_value=FakeActor(FakeDimension(), 0, 64, 0))
        core = Mock()
        core.api_list_public_warps = Mock(return_value=warps)
        core.api_teleport_player_to = Mock(return_value={"ok": ok})
        teleports = []
        core.api_teleport_player_to = Mock(
            side_effect=lambda d, x, y, z, player_name="":
            teleports.append((player_name, x, z)) or {"ok": ok})
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=core)))
        return plugin, teleports

    def test_rescue_after_repeated_walk_fails(self):
        plugin, teleports = self._rescue_plugin(
            [{"warp_name": "主城", "x": 500, "y": 70, "z": 500},
             {"warp_name": "渔村", "x": 800, "y": 65, "z": -300}])
        for _ in range(5):
            plugin._on_walk_fail("Bot", "no path")
        self.assertEqual(len(teleports), 1)
        self.assertEqual(teleports[0][0], "Bot")

    def test_rescue_cooldown_blocks_spam(self):
        plugin, teleports = self._rescue_plugin(
            [{"warp_name": "主城", "x": 500, "y": 70, "z": 500}])
        for _ in range(5):
            plugin._on_walk_fail("Bot", "no path")
        for _ in range(5):                      # 冷却期内再来一轮失败
            plugin._on_walk_fail("Bot", "no path")
        self.assertEqual(len(teleports), 1)     # 不重复传送

    def test_no_rescue_below_threshold(self):
        plugin, teleports = self._rescue_plugin(
            [{"warp_name": "主城", "x": 500, "y": 70, "z": 500}])
        for _ in range(4):
            plugin._on_walk_fail("Bot", "no path")
        self.assertEqual(teleports, [])

    def test_walk_fail_window_expires(self):
        import time as _t
        plugin, teleports = self._rescue_plugin(
            [{"warp_name": "主城", "x": 500, "y": 70, "z": 500}])
        plugin._walk_fails["Bot"] = [_t.monotonic() - 400.0] * 4   # 全过期
        plugin._on_walk_fail("Bot", "no path")                       # 只 1 次新的
        self.assertEqual(teleports, [])

    # ---- 夜间免打扰（v3.9.25） -----------------------------------------

    def _quiet_plugin(self):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._activity = Mock()
        plugin._identities = SimpleNamespace(xuid_of=lambda n: "1")
        plugin._qq_sync = Mock()
        plugin._cfg = {"night_quiet": {"enabled": True, "start_hour": 23, "end_hour": 8}}
        sent: list = []
        plugin.server = SimpleNamespace(broadcast_message=sent.append)
        return plugin, sent

    def test_night_quiet_hours(self):
        plugin, _ = self._quiet_plugin()
        for h, want in [(22, False), (23, True), (0, True), (7, True), (8, False)]:
            self.assertEqual(plugin._night_quiet_on(hour=h), want, h)

    def test_night_quiet_mutes_spontaneous_but_not_answers(self):
        plugin, sent = self._quiet_plugin()
        plugin._night_quiet_on = lambda: True
        plugin._agents = {"Bot": SimpleNamespace(_turn_woken=False)}
        plugin._broadcast("Bot", "半夜闲聊")
        self.assertEqual(sent, [])          # 主动发言被静音
        plugin._agents = {"Bot": SimpleNamespace(_turn_woken=True)}
        plugin._broadcast("Bot", "有人找我")
        self.assertEqual(len(sent), 1)      # 应答照常
        plugin._night_quiet_on = lambda: False
        plugin._agents = {"Bot": SimpleNamespace(_turn_woken=False)}
        plugin._broadcast("Bot", "白天随便聊")
        self.assertEqual(len(sent), 2)

    def test_join_qq_sync_muted_at_night(self):
        plugin, _ = self._quiet_plugin()
        plugin._night_quiet_on = lambda: True
        plugin._cfg["join_message"] = "§e{name} 加入了游戏"
        plugin._announce("join_message", "Bot")
        plugin._qq_sync.assert_not_called()  # QQ 静音
        plugin._night_quiet_on = lambda: False
        plugin._announce("join_message", "Bot")
        plugin._qq_sync.assert_called_once_with("join", "Bot")

    # ---- 安家三件套（v3.9.24） ----------------------------------------

    def test_build_structure_compiles_blueprint(self):
        dim = FakeDimension()
        compiler = make_compiler(dim)
        actor = FakeActor(dim, 0, 64, 0)   # 站在 y64，脚下 y63 是地面
        # 给点材料
        compiler._count_material = lambda a, b: 999
        bp = '[{"off":[0,0,0],"block":"oak_planks"},{"off":[1,0,0],"block":"cobblestone"},{"off":[0,1,0],"block":"oak_planks"}]'
        r = compiler.compile(actor, ToolCall("build_structure", {"blocks": bp}), {})
        self.assertIsNotNone(r.plan, r.immediate)
        self.assertEqual(r.plan.total, 3)
        ops = {s.op for s in r.plan.steps}
        self.assertTrue(ops <= {"place_real", "set_block"})

    def test_build_structure_rejects_bad_block(self):
        dim = FakeDimension()
        compiler = make_compiler(dim)
        actor = FakeActor(dim, 0, 64, 0)
        r = compiler.compile(actor, ToolCall("build_structure",
            {"blocks": '[{"off":[0,0,0],"block":"bedrock"}]'}), {})
        self.assertIsNone(r.plan)
        self.assertIn("格式不对", r.immediate)

    def test_build_structure_rejects_oversize_offset(self):
        dim = FakeDimension()
        compiler = make_compiler(dim)
        actor = FakeActor(dim, 0, 64, 0)
        r = compiler.compile(actor, ToolCall("build_structure",
            {"blocks": '[{"off":[99,0,0],"block":"oak_planks"}]'}), {})
        self.assertIsNone(r.plan)

    def test_claim_land_blocked_near_land(self):
        dim = FakeDimension()
        compiler = make_compiler(dim)
        compiler.bind_land_nearby(lambda cx, cz, d: True)
        actor = FakeActor(dim, 0, 64, 0)
        r = compiler.compile(actor, ToolCall("claim_land", {"radius": 8}), {})
        self.assertIsNone(r.plan)
        self.assertIn("别人的领地", r.immediate)

    def test_claim_land_emits_step_when_clear(self):
        dim = FakeDimension()
        compiler = make_compiler(dim)
        compiler.bind_land_nearby(lambda cx, cz, d: False)
        actor = FakeActor(dim, 0, 64, 0)
        r = compiler.compile(actor, ToolCall("claim_land", {"radius": 6, "name": "小窝"}), {})
        self.assertIsNotNone(r.plan, r.immediate)
        step = r.plan.steps[0]
        self.assertEqual(step.op, "claim_land")
        import json as _j
        spec = _j.loads(step.note)
        self.assertEqual(spec["radius"], 6)
        self.assertEqual(spec["name"], "小窝")

    def test_set_home_calls_callback(self):
        dim = FakeDimension()
        compiler = make_compiler(dim)
        got = []
        compiler.bind_set_home(lambda n, x, y, z: got.append((n, x, y, z)))
        actor = FakeActor(dim, 10, 64, -5)
        actor.name = "BossZhong"
        r = compiler.compile(actor, ToolCall("set_home", {}), {})
        self.assertEqual(got, [("BossZhong", 10, 64, -5)])
        self.assertIn("家已设为", r.immediate)

    # ---- 领地附近不动土（v3.9.23） -------------------------------------

    def test_dig_plan_blocked_near_land(self):
        dim = FakeDimension()
        compiler = make_compiler(dim)
        hits = []
        compiler.bind_land_nearby(lambda cx, cz, d: hits.append((cx, cz, d)) or True)
        actor = FakeActor(dim, 0, 64, 0)
        r = compiler.compile(actor, ToolCall("mine_area", {
            "x1": 0, "y1": 63, "z1": 0, "x2": 4, "y2": 63, "z2": 4}), {})
        self.assertIsNone(r.plan)
        self.assertIn("领地", r.immediate)
        self.assertTrue(hits)          # 真的查过区块

    def test_dig_plan_passes_when_no_land(self):
        dim = FakeDimension()
        compiler = make_compiler(dim)
        compiler.bind_land_nearby(lambda cx, cz, d: False)
        actor = FakeActor(dim, 0, 64, 0)
        r = compiler.compile(actor, ToolCall("mine_area", {
            "x1": 0, "y1": 63, "z1": 0, "x2": 4, "y2": 63, "z2": 4}), {})
        self.assertIsNotNone(r.plan)

    def test_dig_plan_passes_without_core_api(self):
        """核心没有 API（bind 为 None）→ 粗闸放行（执行期硬闸兜底）。"""
        dim = FakeDimension()
        compiler = make_compiler(dim)
        actor = FakeActor(dim, 0, 64, 0)
        r = compiler.compile(actor, ToolCall("mine_area", {
            "x1": 0, "y1": 63, "z1": 0, "x2": 4, "y2": 63, "z2": 4}), {})
        self.assertIsNotNone(r.plan)

    def test_non_dig_actions_not_blocked_by_land(self):
        dim = FakeDimension()
        compiler = make_compiler(dim)
        compiler.bind_land_nearby(lambda cx, cz, d: True)   # 到处都是领地
        actor = FakeActor(dim, 0, 64, 0)
        r = compiler.compile(actor, ToolCall("jump", {}), {})
        self.assertIsNotNone(r.plan)

    def test_roam_dig_blocked_near_land(self):
        plugin = ArcFakePlayerPlugin()
        plugin.logger = Mock()
        plugin._activity = Mock()
        plugin._roam = Mock()
        plugin._live_actor = Mock(return_value=FakeActor(FakeDimension(), 0, 64, 0))
        plugin._bridge = Mock()
        core = Mock()
        core.api_has_land_nearby_chunk = Mock(return_value=True)
        plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=Mock(return_value=core)))
        ok = plugin._roam_dig("Bot", 10, 63, 10)
        self.assertFalse(ok)
        plugin._bridge.dig.assert_not_called()
        plugin._roam.note_failure.assert_called_once()

    def test_unknown_title_skipped_without_creating(self):
        """配置了系统不存在的头衔：跳过，不解锁不佩戴更不注册定义。"""
        plugin, core, ts = self._title_plugin(has_def=False)
        plugin.crowd_cards = lambda: [{"name": "BossZhong", "goal": "x", "title": "十人斩"}]
        plugin._apply_card_identity("BossZhong", Mock())
        core.api_unlock_title_by_xuid.assert_not_called()
        ts.set_equipped_title_by_xuid.assert_not_called()
        # 确保没有走"注册定义"的老路
        core.api_ensure_title_definition.assert_not_called()

    # ---- 计划完成即再规划（v3.9.20） -----------------------------------

    def test_request_rethink_shortens_wait(self):
        agent = self._agent()
        agent.state.last_snapshot = object()
        agent.state.thinking = False
        agent.state.decision_gap = 300.0
        agent.state.last_decision = time_now()
        agent.request_rethink(3.0)
        now = time_now()
        self.assertFalse(agent.should_think(now + 0.5))
        self.assertTrue(agent.should_think(now + 3.5))


def main():
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(HumanlikeTests))
    if not result.wasSuccessful():
        return 1
    print(f"全部 {result.testsRun} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
