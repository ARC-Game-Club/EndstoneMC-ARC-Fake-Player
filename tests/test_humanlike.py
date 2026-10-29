"""行为真人化大版本（3.9.17）的回归：熔断 / 狩猎 / 睡觉 / 交易 / 门控 / 作息。

全部纯逻辑、不碰服务器：世界用 FakeDimension，服务器端用 Mock。
对应 5 天活动日志里统计出的穿帮点，一个特征一个用例。
"""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.endstone_stub import install as _install_stub  # noqa: E402

_install_stub()

from endstone_arc_fake_player.actions import ActionCompiler  # noqa: E402
from endstone_arc_fake_player.agent import Agent, AgentConfig  # noqa: E402
from endstone_arc_fake_player.llm import LLMReply, ToolCall  # noqa: E402
from endstone_arc_fake_player.plugin import ArcFakePlayerPlugin  # noqa: E402
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


def time_monotonic():
    import time
    return time.monotonic()


def main():
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(HumanlikeTests))
    if not result.wasSuccessful():
        return 1
    print(f"全部 {result.testsRun} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
