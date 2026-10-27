"""自动在线调度器（crowd.py）与经济桥（economy.py）的单元测试。

两者都是零游戏依赖的纯逻辑：CrowdScheduler 通过 plugin 钩子回调，
EconomyBridge 通过 plugin_manager 查 core.economy。这里全部用假对象，
时钟与随机源注入，可以确定性地快进几小时。
"""

from __future__ import annotations

import random
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from endstone_arc_fake_player.crowd import (  # noqa: E402
    CharacterCard, CrowdScheduler, load_cards,
)
from endstone_arc_fake_player.economy import EconomyBridge, wallet_key  # noqa: E402
from endstone_arc_fake_player.identity import (  # noqa: E402
    IdentityRegistry, looks_synthetic, synthetic_uuid, synthetic_xuid,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakePlugin:
    """CrowdScheduler 钩子的假实现：记录调用，行为可控。"""

    def __init__(self, *, cards=None, llm_ready=True, target=3,
                 play=(10, 300), gap=(1, 5)) -> None:
        self.config = {
            "enabled": True,
            "target_count": target,
            "play_minutes_min": play[0],
            "play_minutes_max": play[1],
            "rejoin_gap_min_minutes": gap[0],
            "rejoin_gap_max_minutes": gap[1],
        }
        self.cards = cards if cards is not None else []
        self.llm_ready = llm_ready
        self.online = {}          # name -> alive
        self.reserved: set[str] = set()
        self.real_players: set[str] = set()
        self.retired: list[tuple[str, str]] = []
        self.spawned: list[str] = []
        self.spawn_fail: set[str] = set()
        self.logs: list[tuple[str, str]] = []

    # ---- 钩子（与 plugin.py 上的同名方法一一对应） ----
    def crowd_config(self):
        return self.config

    def crowd_cards(self):
        return self.cards

    def crowd_llm_ready(self):
        return self.llm_ready

    def crowd_reserved_names(self):
        return set(self.reserved)

    def crowd_real_player_names(self):
        return set(self.real_players)

    def crowd_log(self, level, msg):
        self.logs.append((level, msg))

    def crowd_bot_alive(self, name):
        return self.online.get(name, False)

    def crowd_spawn(self, card):
        if card.name in self.spawn_fail:
            return False, "行为包未连接"
        self.online[card.name] = True
        self.spawned.append(card.name)
        return True, "已上线"

    def crowd_retire(self, name, why):
        self.online.pop(name, None)
        self.retired.append((name, why))


def make_card(name):
    return CharacterCard(name=name, persona="p", goal="g", money=100)


class CardLoadingTests(unittest.TestCase):
    def test_load_cards_valid_and_invalid(self):
        cards = load_cards([
            {"name": "A", "persona": "x", "goal": "y", "money": 5},
            {"name": ""},               # 没名字 → 丢弃
            "not-a-dict",               # 不是对象 → 丢弃
            {"name": "B", "money": "12.5"},
        ])
        self.assertEqual([c.name for c in cards], ["A", "B"])
        self.assertEqual(cards[0].money, 5.0)
        self.assertEqual(cards[1].money, 12.5)

    def test_long_goal_parsed(self):
        cards = load_cards([
            {"name": "A", "long_goal": "枪战房常驻，有人开局就加入"},
            {"name": "B"},
            "junk",
        ])
        self.assertEqual(cards[0].long_goal, "枪战房常驻，有人开局就加入")
        self.assertEqual(cards[1].long_goal, "")

    def test_bad_money_becomes_none(self):
        card = CharacterCard.from_dict({"name": "A", "money": "abc"})
        self.assertIsNotNone(card)
        self.assertIsNone(card.money)

    def test_non_list_returns_empty(self):
        self.assertEqual(load_cards(None), [])
        self.assertEqual(load_cards("x"), [])


class CrowdSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.plugin = FakePlugin(cards=[make_card(n) for n in "ABCD"])
        self.sched = CrowdScheduler(self.plugin, clock=self.clock,
                                    rng=random.Random(42))

    def tick(self, n=1, seconds: float = 30.0):
        for _ in range(n):
            self.clock.advance(seconds)
            self.sched.tick()

    def run_until(self, predicate, *, max_ticks: int = 400,
                  seconds: float = 30.0):
        """反复 tick 直到条件成立（进服有随机间隔，不能一次到位）。"""
        for _ in range(max_ticks):
            if predicate():
                return
            self.tick(1, seconds)
        self.assertTrue(predicate(), "run_until 超时")

    def test_disabled_drains_pending(self):
        self.plugin.config["enabled"] = False
        self.sched.tick()
        self.assertEqual(self.plugin.spawned, [])

    def test_llm_not_ready_blocks_spawning(self):
        self.plugin.llm_ready = False
        self.sched.tick()
        self.assertEqual(self.plugin.spawned, [])
        # 只告警一次
        self.sched.tick()
        warns = [m for lvl, m in self.plugin.logs if lvl == "warning"]
        self.assertEqual(len(warns), 1)

    def test_startup_staggers_joins(self):
        """启动补位不再齐刷刷上线：第一拍只进一个人，之后每个新进服
        都要隔一个随机间隔（rejoin_gap）。"""
        self.plugin.config.update({
            "rejoin_gap_min_minutes": 1, "rejoin_gap_max_minutes": 1,
        })
        self.sched.tick()
        # 只有 1 个人进服，另外 2 个空位在等进服冷却
        self.assertEqual(len(self.plugin.spawned), 1)
        self.assertEqual(len(self.sched.pending), 2)
        self.assertIsNotNone(self.sched._next_join_due)
        # 30 秒后仍在冷却（gap=1 分钟）
        self.tick(1)
        self.assertEqual(len(self.plugin.spawned), 1)

    def test_startup_fills_to_target(self):
        self.run_until(lambda: len(self.sched.sessions) == 3)
        self.assertEqual(len(self.plugin.spawned), 3)

    def test_expires_at_deadline_and_rejoins_after_gap(self):
        self.plugin.config.update({
            "target_count": 1,
            "play_minutes_min": 10, "play_minutes_max": 10,   # 固定 10 分钟
            "rejoin_gap_min_minutes": 2, "rejoin_gap_max_minutes": 2,
        })
        self.sched.tick()
        first = self.plugin.spawned[0]
        # 9 分钟：还在线
        self.tick(18, 30.0)
        self.assertIn(first, self.sched.sessions)
        # 到点：下线，空位进入 gap 等待
        self.sched.sessions[first].deadline = self.clock.now
        self.tick(1)
        self.assertEqual(len(self.sched.sessions), 0)
        # gap 2 分钟未到：不补
        self.tick(3, 30.0)
        self.assertEqual(len(self.sched.sessions), 0)
        # gap 过了：补人（不是刚走的那个）
        self.run_until(lambda: len(self.sched.sessions) == 1)
        self.assertNotEqual(self.plugin.spawned[-1], first)

    def test_dead_body_triggers_retire_and_refill(self):
        self.sched.tick()
        victim = self.plugin.spawned[0]
        self.plugin.online[victim] = False     # 身体消失（被打掉没回来）
        self.tick(1)
        self.assertIn((victim, "身体消失"), self.plugin.retired)

    def test_fill_uses_all_cards_when_target_matches_pool(self):
        self.plugin.config["target_count"] = 4
        self.run_until(lambda: len(self.sched.sessions) == 4)
        self.assertEqual(sorted(self.plugin.spawned), ["A", "B", "C", "D"])

    def test_pick_is_random_not_round_robin(self):
        """挑卡是随机的：不同种子下首个上线角色应有多个取值
        （按序发牌永远只出同一张）。"""
        firsts = set()
        for seed in range(30):
            plugin = FakePlugin(cards=[make_card(n) for n in "ABCD"])
            clock = FakeClock()
            sched = CrowdScheduler(plugin, clock=clock,
                                   rng=random.Random(seed))
            sched.tick()
            firsts.add(plugin.spawned[0])
        self.assertGreater(len(firsts), 1)

    def test_recently_retired_not_re_picked(self):
        """刚下线的名字短时间内不该立刻回来（出戏）。"""
        self.plugin.config.update({
            "target_count": 1,
            "rejoin_gap_min_minutes": 1, "rejoin_gap_max_minutes": 1,
        })
        self.sched.tick()
        first = self.plugin.spawned[0]
        # 到点下线 → 空位补人：新进来的不该是刚走的那个
        self.sched.sessions[first].deadline = self.clock.now
        self.run_until(lambda: len(self.plugin.spawned) >= 2)
        self.assertNotEqual(self.plugin.spawned[1], first)

    def test_reserved_and_real_player_names_skipped(self):
        self.plugin.reserved = {"A"}
        self.plugin.real_players = {"B"}
        self.run_until(lambda: len(self.sched.sessions) >= 1)
        self.assertNotIn("A", self.plugin.spawned)
        self.assertNotIn("B", self.plugin.spawned)

    def test_spawn_failure_backs_off(self):
        self.plugin.spawn_fail = {"A"}
        self.plugin.config["target_count"] = 1
        self.sched.tick()
        self.assertEqual(self.plugin.spawned, [])
        # 60 秒退避后重试；随机挑卡可能又撞 A，但总会轮到能上线的卡
        self.tick(2)
        self.run_until(lambda: len(self.plugin.spawned) >= 1)
        self.assertNotIn("A", self.plugin.spawned)

    def test_forget_then_refill_keeps_count(self):
        self.sched.tick()
        gone = self.plugin.spawned[0]
        self.plugin.online.pop(gone)
        self.sched.forget(gone)                # 管理员手动移除
        self.tick(1)
        self.assertNotIn(gone, self.sched.sessions)

    def test_session_minutes_extend_and_zero_retires(self):
        self.sched.tick()
        name = self.plugin.spawned[0]
        ok, msg = self.sched.set_session_minutes(name, 999)
        self.assertTrue(ok)
        self.tick(60)                          # 半小时过去：不该下线
        self.assertIn(name, self.sched.sessions)
        ok, _ = self.sched.set_session_minutes(name, 0)
        self.assertTrue(ok)
        self.tick(1)
        self.assertNotIn(name, self.sched.sessions)
        ok, _ = self.sched.set_session_minutes("nobody", 5)
        self.assertFalse(ok)

    def test_shortage_refill_uses_gap_after_bootstrap(self):
        self.plugin.config.update({
            "target_count": 2,
            "rejoin_gap_min_minutes": 1, "rejoin_gap_max_minutes": 1,
        })
        self.run_until(lambda: len(self.sched.sessions) == 2)
        # 目标提到 3：新空位要等 gap（1 分钟），不是立刻冒出来
        self.plugin.config["target_count"] = 3
        self.tick(1)
        self.assertEqual(len(self.sched.sessions), 2)
        self.run_until(lambda: len(self.sched.sessions) == 3)

    def test_describe_mentions_counts(self):
        self.run_until(lambda: len(self.sched.sessions) == 3)
        text = "\n".join(self.sched.describe())
        self.assertIn("目标 3 人", text)
        self.assertIn("在线 3", text)


class JoinLeaveAnnounceTests(unittest.TestCase):
    """进/退服播报（plugin.py 上的 _announce_*，用假 self 直调）。"""

    CFG = {
        "join_message": "§e{name} 加入了游戏",
        "leave_message": "§e{name} 离开了游戏",
    }

    def _make_fake(self, cfg=None):
        import endstone_arc_fake_player.plugin as plugin_mod
        from types import MethodType

        class Server:
            def __init__(self):
                self.sent = []

            def broadcast_message(self, text):
                self.sent.append(text)

        fake = SimpleNamespace(
            _stealth={"BotA"}, _cfg=dict(cfg if cfg is not None else self.CFG),
            server=Server(), _record=lambda *a, **k: None)
        # _announce_* 内部调 self._announce / self._qq_sync——把真方法绑到
        # 假 self 上（假 server 没有 plugin_manager，_qq_sync 会安静返回）
        fake._announce = MethodType(plugin_mod.ArcFakePlayerPlugin._announce,
                                    fake)
        fake._qq_sync = MethodType(plugin_mod.ArcFakePlayerPlugin._qq_sync, fake)
        fake._schedule_checkin = MethodType(
            plugin_mod.ArcFakePlayerPlugin._schedule_checkin, fake)
        return fake, plugin_mod.ArcFakePlayerPlugin

    def test_join_announced_for_stealth_bot(self):
        fake, cls = self._make_fake()
        cls._announce_join(fake, "BotA")
        self.assertEqual(fake.server.sent, ["§eBotA 加入了游戏"])

    def test_leave_announced_in_game_only(self):
        """退服只播游戏内：QQ 由 qq_sync 的 PlayerQuitEvent 处理
        （统计准确），我们再推会变成"离开游戏×2"。"""
        fake, cls = self._make_fake()
        cls._announce_leave(fake, "BotA")
        self.assertEqual(fake.server.sent, ["§eBotA 离开了游戏"])

    def test_join_still_pushes_qq(self):
        fake, cls = self._make_fake()
        pushed = []
        fake._qq_sync = lambda et, n, m="": pushed.append(et)
        cls._announce_join(fake, "BotA")
        self.assertEqual(pushed, ["join"])

    def test_leave_does_not_push_qq(self):
        fake, cls = self._make_fake()
        pushed = []
        fake._qq_sync = lambda et, n, m="": pushed.append(et)
        cls._announce_leave(fake, "BotA")
        self.assertEqual(pushed, [])

    def test_manual_spawn_not_announced(self):
        """不在 _stealth 里的手动假人不播报（它有自己的回执）。"""
        fake, cls = self._make_fake()
        cls._announce_join(fake, "ManualBot")
        self.assertEqual(fake.server.sent, [])

    def test_empty_template_disables(self):
        fake, cls = self._make_fake({"join_message": "", "leave_message": ""})
        cls._announce_join(fake, "BotA")
        cls._announce_leave(fake, "BotA")
        self.assertEqual(fake.server.sent, [])


class QQSyncTests(unittest.TestCase):
    """假人聊天/进退服 → QQ 群同步（_qq_sync 直调）。"""

    def _make_fake(self, cfg=None, qq_plugin=None, raise_on_get=False):
        import endstone_arc_fake_player.plugin as plugin_mod
        from types import MethodType

        class Manager:
            def get_plugin(self, name):
                if raise_on_get:
                    raise RuntimeError("no such plugin")
                return qq_plugin

        class Server:
            def __init__(self):
                self.plugin_manager = Manager()

        fake = SimpleNamespace(
            _cfg=dict(cfg if cfg is not None else {
                "qq_sync_chat": True, "qq_sync_join_leave": True}),
            server=Server(),
            logger=SimpleNamespace(debug=lambda *_: None),
            _identities=SimpleNamespace(
                xuid_of=lambda n: "9123456789012345"))
        fake._qq_sync = MethodType(plugin_mod.ArcFakePlayerPlugin._qq_sync,
                                   fake)
        return fake, plugin_mod.ArcFakePlayerPlugin

    @staticmethod
    def _qq_plugin():
        calls = []

        class QQ:
            def api_send_event(self, *args, **kwargs):
                calls.append((args, kwargs))
                return True

        return QQ(), calls

    @staticmethod
    def _qq_plugin_legacy():
        calls = []

        class QQ:
            def api_send_event(self, *args):
                calls.append(args)
                return True

        return QQ(), calls

    def test_chat_pushed_to_qq_with_xuid(self):
        """事件带合成 xuid：绕开'在线匹配拿到空 xuid'的解析坑。"""
        qq, calls = self._qq_plugin()
        fake, cls = self._make_fake(qq_plugin=qq)
        cls._qq_sync(fake, "chat", "BotA", "大家好")
        self.assertEqual(
            calls,
            [(("chat", "BotA", "BotA", "大家好"),
              {"xuid": "9123456789012345"})])

    def test_legacy_qq_plugin_without_xuid_kwarg(self):
        """老版同步插件没有 xuid 参数：TypeError 回退为不带参调用。"""
        qq, calls = self._qq_plugin_legacy()
        fake, cls = self._make_fake(qq_plugin=qq)
        cls._qq_sync(fake, "chat", "BotA", "大家好")
        self.assertEqual(calls, [("chat", "BotA", "BotA", "大家好")])

    def test_chat_disabled(self):
        qq, calls = self._qq_plugin()
        fake, cls = self._make_fake({"qq_sync_chat": False}, qq_plugin=qq)
        cls._qq_sync(fake, "chat", "BotA", "大家好")
        self.assertEqual(calls, [])

    def test_join_leave_gated_separately(self):
        qq, calls = self._qq_plugin()
        fake, cls = self._make_fake({"qq_sync_join_leave": False},
                                    qq_plugin=qq)
        cls._qq_sync(fake, "join", "BotA")
        cls._qq_sync(fake, "quit", "BotA")
        self.assertEqual(calls, [])
        cls._qq_sync(fake, "chat", "BotA", "hi")   # 聊天不受影响
        self.assertEqual(len(calls), 1)

    def test_plugin_missing_is_silent(self):
        fake, cls = self._make_fake(raise_on_get=True)
        cls._qq_sync(fake, "chat", "BotA", "x")   # 不抛即为通过

    def test_plugin_without_api_is_silent(self):
        fake, cls = self._make_fake(qq_plugin=object())
        cls._qq_sync(fake, "chat", "BotA", "x")

    def test_api_exception_swallowed(self):
        class BadQQ:
            def api_send_event(self, *a, **k):
                raise RuntimeError("hub down")

        fake, cls = self._make_fake(qq_plugin=BadQQ())
        cls._qq_sync(fake, "chat", "BotA", "x")   # 不抛即为通过


class PlaytimeSyncTests(unittest.TestCase):
    """游玩时长记账（_core_playtime：合成 xuid 垫片直调核心）。"""

    XUID = "9123456789012345"

    def _make_fake(self, cfg=None, core=None, xuid=XUID):
        import endstone_arc_fake_player.plugin as plugin_mod
        from types import MethodType

        class Manager:
            def get_plugin(self, name):
                if core is None:
                    raise RuntimeError("no core")
                return core

        class Server:
            def __init__(self):
                self.plugin_manager = Manager()

        fake = SimpleNamespace(
            _cfg=dict(cfg if cfg is not None else {"playtime_sync": True}),
            server=Server(),
            logger=SimpleNamespace(debug=lambda *_: None),
            _identities=SimpleNamespace(xuid_of=lambda n: xuid))
        fake._core_playtime = MethodType(
            plugin_mod.ArcFakePlayerPlugin._core_playtime, fake)
        return fake, plugin_mod.ArcFakePlayerPlugin

    @staticmethod
    def _core():
        calls = []

        class Core:
            def _record_player_join_playtime(self, player):
                calls.append(("join", player.name, player.xuid))

            def _record_player_quit_playtime(self, player=None, *,
                                             xuid="", name=""):
                calls.append(("quit", name, xuid, player))

        return Core(), calls

    def test_join_passes_synthetic_xuid_shim(self):
        core, calls = self._core()
        fake, cls = self._make_fake(core=core)
        cls._core_playtime(fake, "BotA", joining=True)
        self.assertEqual(calls, [("join", "BotA", self.XUID)])

    def test_quit_passes_keywords(self):
        core, calls = self._core()
        fake, cls = self._make_fake(core=core)
        cls._core_playtime(fake, "BotA", joining=False)
        self.assertEqual(
            calls, [("quit", "BotA", self.XUID, None)])

    def test_disabled_by_config(self):
        core, calls = self._core()
        fake, cls = self._make_fake({"playtime_sync": False}, core=core)
        cls._core_playtime(fake, "BotA", joining=True)
        cls._core_playtime(fake, "BotA", joining=False)
        self.assertEqual(calls, [])

    def test_core_missing_is_silent(self):
        fake, cls = self._make_fake(core=None)
        cls._core_playtime(fake, "BotA", joining=True)   # 不抛即为通过

    def test_core_without_methods_is_silent(self):
        fake, cls = self._make_fake(core=object())
        cls._core_playtime(fake, "BotA", joining=True)

    def test_no_synthetic_xuid_skips(self):
        core, calls = self._core()
        fake, cls = self._make_fake(core=core, xuid="")
        cls._core_playtime(fake, "BotA", joining=True)
        self.assertEqual(calls, [])


class EconomyBridgeTests(unittest.TestCase):
    def make_bridge(self, *, fail=None):
        """core.economy 的最小假实现。fail 是要让它失败的方法名集合。

        inc 用"只失败一次"的语义：模拟"给玩家加钱那一下失败、
        回滚的第二次调用成功"——回滚要验证的正是这个顺序。
        """
        fail = fail or set()
        balances = {}
        inc_calls = {"n": 0}

        def get(x):
            return balances.get(x)

        def set_(x, v):
            if "set" in fail:
                return False
            balances[x] = v
            return True

        def dec(x, v):
            if "dec" in fail:
                return False
            if balances.get(x, 0) < v:
                return False
            balances[x] -= v
            return True

        def inc(x, v):
            inc_calls["n"] += 1
            if "inc" in fail and inc_calls["n"] == 1:
                return False
            balances[x] = balances.get(x, 0) + v
            return True

        def init(x):
            balances.setdefault(x, 100.0)
            return True

        core = SimpleNamespace(economy=SimpleNamespace(
            get_player_money_by_xuid=get,
            set_player_money_by_xuid=set_,
            decrease_player_money_by_xuid=dec,
            increase_player_money_by_xuid=inc,
            init_player_economy_by_xuid=init,
        ))
        server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=lambda n: core))
        bridge = EconomyBridge(server, _SilentLogger())
        bridge._cached = core.economy
        return bridge, balances

    def test_wallet_key_is_stable_and_namespaced(self):
        self.assertEqual(wallet_key("Aqing"), "fake_aqing")
        self.assertEqual(wallet_key("Aqing"), wallet_key("AQING"))

    def test_no_core_means_unavailable(self):
        server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=lambda n: None))
        bridge = EconomyBridge(server, _SilentLogger())
        self.assertFalse(bridge.available())
        self.assertIsNone(bridge.get_balance("X"))
        self.assertFalse(bridge.set_balance("X", 5))

    def test_ensure_wallet_sets_initial_and_ensure_only_keeps(self):
        bridge, balances = self.make_bridge()
        # 首次：设初始额（get-or-create 建行 + 写值）
        self.assertTrue(bridge.ensure_wallet("XiaoHe", 500))
        self.assertEqual(balances["fake_xiaohe"], 500.0)
        # 之后：ensure-only（initial=None）不改余额
        balances["fake_xiaohe"] = 999
        bridge.ensure_wallet("XiaoHe", None)
        self.assertEqual(balances["fake_xiaohe"], 999.0)

    def test_ensure_wallet_migrates_legacy_balance(self):
        # 换键（合成 xuid）后，旧 fake_<名字> 键的余额要带过来
        bridge, balances = self.make_bridge()
        balances["fake_seller"] = 700.0
        bridge.bind_key_fn(lambda n: "9504154966749693")
        # 首次设值 100，但旧键有 700 → 取较大
        self.assertTrue(bridge.ensure_wallet("seller", 100))
        self.assertEqual(balances["9504154966749693"], 700.0)
        self.assertEqual(balances["fake_seller"], 700.0)   # 旧键不动

    def test_pay_player_moves_money(self):
        bridge, balances = self.make_bridge()
        balances["fake_seller"] = 50.0
        player = SimpleNamespace(xuid="12345")
        ok, why = bridge.pay_player("seller", player, 30.0)
        self.assertTrue(ok, why)
        self.assertEqual(balances["fake_seller"], 20.0)
        self.assertEqual(balances["12345"], 30.0)

    def test_pay_player_rejects_insufficient(self):
        bridge, balances = self.make_bridge()
        balances["fake_seller"] = 5.0
        ok, _ = bridge.pay_player("seller", SimpleNamespace(xuid="1"), 30.0)
        self.assertFalse(ok)

    def test_pay_player_rollback_when_credit_fails(self):
        bridge, balances = self.make_bridge(fail={"inc"})
        balances["fake_seller"] = 50.0
        ok, _ = bridge.pay_player("seller", SimpleNamespace(xuid="1"), 30.0)
        self.assertFalse(ok)
        # 加钱失败 → 退回，假人不吃亏
        self.assertEqual(balances["fake_seller"], 50.0)

    def test_receive_from_player(self):
        bridge, balances = self.make_bridge()
        balances["42"] = 10.0
        ok, _ = bridge.receive_from_player("bot", SimpleNamespace(xuid="42"), 7.5)
        self.assertTrue(ok)
        self.assertEqual(balances["fake_bot"], 7.5)
        self.assertEqual(balances["42"], 2.5)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "identities.json"

    def registry(self):
        return IdentityRegistry(self.path, logger=None)

    def test_synthetic_xuid_shape(self):
        for name in ("Aqing", "TieDan", "测试假人"):
            x = synthetic_xuid(name)
            self.assertEqual(len(x), 16, x)
            self.assertTrue(x.isdigit(), x)
            self.assertTrue(x.startswith("9"), x)
        # 同名永远同 xuid，不同名不同 xuid
        self.assertEqual(synthetic_xuid("Aqing"), synthetic_xuid("Aqing"))
        self.assertNotEqual(synthetic_xuid("Aqing"), synthetic_xuid("aqing"))

    def test_synthetic_uuid_stable(self):
        self.assertEqual(synthetic_uuid("Aqing"), synthetic_uuid("Aqing"))
        self.assertNotEqual(synthetic_uuid("Aqing"), synthetic_uuid("TieDan"))

    def test_looks_synthetic(self):
        self.assertTrue(looks_synthetic(synthetic_xuid("X")))
        self.assertFalse(looks_synthetic("2535441234567890"))   # 真人形状
        self.assertFalse(looks_synthetic(""))
        self.assertFalse(looks_synthetic("fake_aqing"))

    def test_registry_generates_once_and_persists(self):
        reg = self.registry()
        first = reg.of("Aqing")
        self.assertTrue(looks_synthetic(first["xuid"]))
        reg.save()
        # 重开（模拟重启）：同一个身份
        reborn = self.registry()
        self.assertEqual(reborn.of("Aqing")["xuid"], first["xuid"])
        self.assertEqual(reborn.of("Aqing")["uuid"], first["uuid"])

    def test_pin_respects_first_registration(self):
        reg = self.registry()
        pinned = reg.pin("Bot7", xuid="9111111111111111", uuid="uuid-7")
        self.assertEqual(pinned["xuid"], "9111111111111111")
        # 第二次钉不同值：以先到为准，不改
        again = reg.pin("Bot7", xuid="9222222222222222")
        self.assertEqual(again["xuid"], "9111111111111111")

    def test_pin_none_generates_synthetic_not_string_none(self):
        """pin(xuid=None) 必须生成合成身份，而不是字符串 "None"。

        回归：str(None).strip() == "None" 是真值，曾让所有假人的
        身份都变成 xuid="None"（又挤回同一行数据）。
        """
        reg = self.registry()
        ident = reg.pin("Bot8")                     # 两个参数都不传
        self.assertTrue(looks_synthetic(ident["xuid"]), ident["xuid"])
        self.assertTrue(ident["uuid"] and ident["uuid"] != "None")
        ident2 = reg.pin("Bot9", xuid=None, uuid=None)
        self.assertTrue(looks_synthetic(ident2["xuid"]), ident2["xuid"])

    def test_name_of_xuid_roundtrip(self):
        reg = self.registry()
        ident = reg.of("MuTou")
        self.assertEqual(reg.name_of_xuid(ident["xuid"]), "MuTou")
        self.assertIsNone(reg.name_of_xuid("2535441234567890"))

    def test_card_parses_identity_fields(self):
        card = CharacterCard.from_dict(
            {"name": "A", "xuid": "9123456789012345", "uuid": "u-1", "money": 3})
        self.assertEqual(card.xuid, "9123456789012345")
        self.assertEqual(card.uuid, "u-1")
        card2 = CharacterCard.from_dict({"name": "B"})
        self.assertIsNone(card2.xuid)
        self.assertIsNone(card2.uuid)


class _SilentLogger:
    def debug(self, *_a):
        pass

    info = warning = error = debug


def main():
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for cls in (CardLoadingTests, CrowdSchedulerTests, EconomyBridgeTests, IdentityTests):
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


class SpeechSplitTests(unittest.TestCase):
    """聊天按标点分句发送（"这波我的！跟上" → 两条）。"""

    def test_examples_from_user(self):
        from endstone_arc_fake_player.plugin import _split_speech
        self.assertEqual(_split_speech("这波我的！跟上"),
                         ["这波我的！", "跟上"])
        self.assertEqual(_split_speech("刚过来，谁在边上"),
                         ["刚过来", "谁在边上"])

    def test_no_punct_single_fragment(self):
        from endstone_arc_fake_player.plugin import _split_speech
        self.assertEqual(_split_speech("没标号的一条"), ["没标号的一条"])

    def test_trailing_punct_no_empty_tail(self):
        from endstone_arc_fake_player.plugin import _split_speech
        self.assertEqual(_split_speech("尾标点。"), ["尾标点。"])

    def test_fragment_cap(self):
        from endstone_arc_fake_player.plugin import _split_speech
        frags = _split_speech("嗯。好。行。走。冲。")
        self.assertEqual(len(frags), 4)
        self.assertEqual(frags[-1], "走。冲。")

    def test_broadcast_splits_and_staggers(self):
        from types import MethodType
        import endstone_arc_fake_player.plugin as plugin_mod

        sent = []
        scheduled = []

        class Sched:
            def run_task(self, plugin, fn, delay=0, period=0):
                scheduled.append((delay, fn))

        class Server:
            scheduler = Sched()

            def broadcast_message(self, text):
                sent.append(text)

        fake = SimpleNamespace(
            server=Server(), _cfg={"qq_sync_chat": True},
            logger=SimpleNamespace(debug=lambda *_: None))
        fake._qq_sync = lambda *a, **k: None
        fake._send_chat = MethodType(plugin_mod.ArcFakePlayerPlugin._send_chat, fake)
        fake._schedule_chat = MethodType(
            plugin_mod.ArcFakePlayerPlugin._schedule_chat, fake)
        plugin_mod.ArcFakePlayerPlugin._broadcast(fake, "Bot", "这波我的！跟上")
        # 第一句立刻发，第二句进了延迟队列（ticks ≥ 1）
        self.assertEqual(len(sent), 1)
        self.assertIn("这波我的！", sent[0])
        self.assertEqual(len(scheduled), 1)
        delay, fn = scheduled[0]
        self.assertGreaterEqual(delay, 1)
        fn()
        self.assertEqual(len(sent), 2)
        self.assertIn("跟上", sent[1])

    def test_broadcast_single_fragment_no_schedule(self):
        from types import MethodType
        import endstone_arc_fake_player.plugin as plugin_mod

        sent = []

        class Server:
            def broadcast_message(self, text):
                sent.append(text)

        fake = SimpleNamespace(server=Server(), _cfg={},
                               logger=SimpleNamespace(debug=lambda *_: None))
        fake._qq_sync = lambda *a, **k: None
        fake._send_chat = MethodType(plugin_mod.ArcFakePlayerPlugin._send_chat, fake)
        plugin_mod.ArcFakePlayerPlugin._broadcast(fake, "Bot", "没标号的一条")
        self.assertEqual(len(sent), 1)


class QQChatInboundTests(unittest.TestCase):
    """QQ 群消息入口 api_hear_chat：群里 @假人 = 点名必回。"""

    def _plugin_with_agents(self, chance=0.0):
        import endstone_arc_fake_player.plugin as plugin_mod
        from types import MethodType
        from unittest.mock import Mock

        plugin = plugin_mod.ArcFakePlayerPlugin()
        agent = SimpleNamespace(hear=Mock(), wake=Mock(), alive=lambda: True,
                                state=SimpleNamespace(last_decision=5.0))
        plugin._agents = {"Bot": agent}
        plugin._external = {}
        plugin._stealth = set()
        plugin._favor = SimpleNamespace(
            chat_bonus=Mock(return_value=None))
        plugin._social_cfg = lambda: {"chat_bonus": 1.0}
        plugin.chat_reply_chance = lambda name: chance
        plugin.server = SimpleNamespace(broadcast_message=Mock())
        plugin._record = lambda *a, **k: None
        plugin._cfg = {}
        return plugin, agent

    def test_group_mention_reaches_bot(self):
        plugin, agent = self._plugin_with_agents()
        plugin.api_hear_chat("群友老哥", "Bot 在吗，跟一把")
        agent.hear.assert_called_once_with("群友老哥(群友)", "Bot 在吗，跟一把")
        agent.wake.assert_called_once()

    def test_group_ambient_never_triggers(self):
        """外部消息（QQ 群/跨服）只认点名——概率拉满也不搭闲话。"""
        plugin, agent = self._plugin_with_agents(chance=1.0)
        plugin.api_hear_chat("群友老哥", "今天天气不错")
        agent.hear.assert_not_called()
        # 游戏内闲聊概率 1 照样听（对比：门控只对外部源收紧）
        import endstone_arc_fake_player.plugin as plugin_mod
        ev = SimpleNamespace(
            message="今天天气不错",
            player=SimpleNamespace(name="Steve", is_op=False),
            cancel=lambda: None)
        plugin.on_player_chat(ev)
        agent.hear.assert_called_once()

    def test_own_bot_name_ignored(self):
        plugin, agent = self._plugin_with_agents(chance=1.0)
        plugin.api_hear_chat("Bot", "我自言自语")
        agent.hear.assert_not_called()


class CraftCoverageTests(unittest.TestCase):
    """配方覆盖：护甲/铜具/杂项不再是"不知道配方"。"""

    def test_gear_recipes_known(self):
        import endstone_arc_fake_player.actions as actions_mod
        recipes = actions_mod.ActionCompiler.CRAFT_RECIPES
        for want in ("iron_chestplate", "copper_sword", "diamond_helmet",
                     "leather_boots", "stone_shovel", "diamond_hoe",
                     "bucket", "bow", "arrow", "bed", "ladder"):
            self.assertIn(want, recipes, want)
        # 每个配方的 shape/mats 都能转成槽位
        comp = actions_mod.ActionCompiler.__new__(actions_mod.ActionCompiler)
        for name, r in recipes.items():
            slots = (comp._recipe_slots_2x2(r) if max(
                len(row) for row in r["shape"]) <= 2
                and len(r["shape"]) <= 2 else comp._recipe_slots(r))
            self.assertTrue(slots, name)

    def test_walk_timeout_scales_with_distance(self):
        from endstone_arc_fake_player.body import BodyController as BC
        self.assertEqual(BC.auto_walk_timeout(0, 0, 0, dist=5), 200)   # 下限
        self.assertEqual(BC.auto_walk_timeout(0, 0, 0, dist=80), 960)  # 12/格
        self.assertEqual(BC.auto_walk_timeout(0, 0, 0, dist=500), 1200)  # 上限


class CoreCheckinTests(unittest.TestCase):
    """假人走弧光核心签到体系（合成 xuid 直调）。"""

    def _core(self, already=False, pre_count=2):
        calls = {"sql": [], "money": [], "broadcast": [], "update": []}

        class DBM:
            def query_one(self, sql, params=()):
                calls["sql"].append((sql, params))
                if "COUNT" in sql:
                    class R(dict):
                        pass
                    r = R()
                    r["cnt"] = pre_count if "COUNT" in sql and sql.count(
                        "last_checkin_date") else 0
                    # 第二次 COUNT（签到后）+1
                    if calls["sql"].count((sql, params)) > 1:
                        r["cnt"] = pre_count + 1
                    return r
                if "last_checkin_date" in sql and "continuous" in sql:
                    return ({"last_checkin_date": "2026-10-26",
                             "continuous_checkin_days": 3}
                            if already else
                            {"last_checkin_date": "2026-10-25",
                             "continuous_checkin_days": 3})
                return None

            def execute(self, sql, params=()):
                calls["update"].append((sql, params))

        class Core:
            database_manager = DBM()

            def _today_checkin_date_str(self):
                return "2026-10-26"

            def get_checkin_config(self):
                return {"daily_money": 100.0, "pick_min": 0, "pick_max": 0,
                        "top_rank_limit": 0, "top_rank_bonus_item_count": 0,
                        "top_rank_bonus_money_step": 0.0,
                        "continuous_checkin_money_increment": 10.0,
                        "reward_list": []}

            def _compute_next_continuous_checkin_days(self, last, days, today):
                return 4

            def _calculate_checkin_top_rank_bonus_money(self, *a):
                return 0.0

            def increase_player_money_by_xuid(self, xuid, amount, notify=True):
                calls["money"].append((xuid, amount, notify))
                return True

            def _weighted_sample_checkin_rewards(self, lst, n):
                return []

            def get_player_name_by_xuid(self, xuid, return_with_title=False):
                return "§6[大佬]Bot"

            def _broadcast_checkin_rankings(self, display, today):
                calls["broadcast"].append((display, today))

        return Core(), calls

    def _fake(self, core):
        import endstone_arc_fake_player.plugin as plugin_mod
        from types import MethodType

        class Manager:
            def get_plugin(self, name):
                return core

        class Server:
            plugin_manager = Manager()

            def dispatch_command(self, *a):
                pass

            command_sender = object()

        fake = SimpleNamespace(
            server=Server(), _agents={"Bot": object()},
            _identities=SimpleNamespace(xuid_of=lambda n: "9123456789012345"),
            logger=SimpleNamespace(debug=lambda *_: None),
            _record=lambda *a, **k: None)
        fake._core_checkin = MethodType(
            plugin_mod.ArcFakePlayerPlugin._core_checkin, fake)
        return fake, plugin_mod.ArcFakePlayerPlugin

    def test_checkin_success_with_rank(self):
        core, calls = self._core(pre_count=2)
        fake, cls = self._fake(core)
        info = cls._core_checkin(fake, "Bot")
        self.assertEqual(info, {"already": False, "rank": 3, "total": 3})
        self.assertTrue(calls["update"])              # 记账 UPDATE
        self.assertTrue(calls["broadcast"])           # 全服排行广播
        self.assertEqual(calls["money"][0][0], "9123456789012345")  # 合成 xuid 入账
        self.assertFalse(calls["money"][0][2])        # notify=False 不打扰

    def test_checkin_already_today(self):
        core, calls = self._core(already=True)
        fake, cls = self._fake(core)
        info = cls._core_checkin(fake, "Bot")
        self.assertEqual(info, {"already": True})
        self.assertFalse(calls["update"])
        self.assertFalse(calls["broadcast"])

    def test_checkin_core_missing(self):
        fake, cls = self._fake(None)
        self.assertIsNone(cls._core_checkin(fake, "Bot"))


class TeleportToolTests(unittest.TestCase):
    """传送工具的编译（tp_player/tp_warp/tp_spawn → 主线程步骤）。"""

    def _compiler(self):
        import endstone_arc_fake_player.actions as actions_mod
        comp = actions_mod.ActionCompiler.__new__(actions_mod.ActionCompiler)
        return comp

    def test_spec_contains_tp_tools(self):
        from endstone_arc_fake_player.llm import LLMClient, SPEC
        for name in ("tp_player", "tp_warp", "tp_spawn"):
            self.assertIn(name, SPEC)
        names = [it["function"]["name"] for it in LLMClient.tool_spec()]
        for name in ("tp_player", "tp_warp", "tp_spawn"):
            self.assertIn(name, names)

    def test_compile_tp_tools_to_steps(self):
        comp = self._compiler()

        class Actor:
            location = type("L", (), {"x": 0, "y": 64, "z": 0})()
            dimension = "overworld"

        r1 = comp.compile(Actor(), type("C", (), {"name": "tp_player",
                                                  "args": {"player": "Steve"}})(), {})
        self.assertIsNotNone(r1.plan)
        self.assertEqual(r1.plan.steps[0].op, "tp_player")
        self.assertEqual(r1.plan.steps[0].note, "Steve")

        r2 = comp.compile(Actor(), type("C", (), {"name": "tp_warp",
                                                  "args": {"name": "主城"}})(), {})
        self.assertEqual(r2.plan.steps[0].op, "tp_warp")
        self.assertEqual(r2.plan.steps[0].note, "主城")

        r3 = comp.compile(Actor(), type("C", (), {"name": "tp_spawn",
                                                  "args": {}})(), {})
        self.assertEqual(r3.plan.steps[0].op, "tp_spawn")

        r4 = comp.compile(Actor(), type("C", (), {"name": "tp_warp",
                                                  "args": {}})(), {})
        self.assertFalse(r4.ok)
