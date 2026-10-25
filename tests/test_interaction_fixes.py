"""Regression tests for delayed simulated-player interactions (no BDS needed)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.endstone_stub import install

install()

from endstone_arc_fake_player.actions import ActionCompiler
from endstone_arc_fake_player.body import BodyController
from endstone_arc_fake_player.bridge import FakePlayerBridge
from endstone_arc_fake_player.executor import BatchExecutor, Plan, PlanState, Step
from endstone_arc_fake_player.realbody import RealBodyActions


class Logger:
    def debug(self, *_): pass
    def info(self, *_): pass
    def warning(self, *_): pass


class Interactions(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.log = Logger()
        self.bridge = FakePlayerBridge(self.log, lambda cmd: self.sent.append(cmd) or True)
        self.bridge.new_token()
        self.world = SimpleNamespace(is_solid_id=lambda x, y, z: y <= 63 or (x, y, z) == (2, 64, 0))
        self.body = BodyController("Alice", self.bridge, self.world, self.log)
        self.body.actor = SimpleNamespace(
            location=SimpleNamespace(x=0.5, y=64.0, z=0.5), health=20.0)
        self.actions = RealBodyActions(self.body, self.world, self.log)
        self.actions.RETRY_BACKOFF_SECONDS = 0

    def requests(self, event):
        return [json.loads(cmd.split(" ", 2)[2]) for cmd in self.sent
                if cmd.startswith(f"scriptevent {event} ")]

    def reply(self, event, request, ok=True, **extra):
        self.bridge.dispatch(event, json.dumps(
            {"t": self.bridge._token, "i": request["i"], "ok": ok, **extra}))

    def test_dig_survives_many_ticks_until_completion(self):
        step = Step("dig_far", 2, 64, 0, async_op=True)
        executor = BatchExecutor(self.log)
        executor.register("dig_far", self.actions.dig_far)
        plan = executor.submit(Plan("mine stone", [step]))
        for _ in range(100):
            executor.tick()
        self.assertEqual(plan.cursor, 0)
        self.assertEqual(len(self.requests("bot:dig")), 1)
        self.assertEqual(self.requests("bot:stop"), [])
        self.reply("bot:dug", self.requests("bot:dig")[0], swing=80)
        executor.tick()
        self.assertEqual(plan.state, PlanState.DONE)
        self.assertEqual(plan.done_count, 1)
        self.assertEqual(self.body.digs_done, 1)

    def test_walk_switches_to_dig_only_once(self):
        step = Step("dig_far", 15, 64, 0, async_op=True)
        self.assertFalse(self.actions.dig_far(step))
        walk = self.requests("bot:walk")[0]
        self.body.actor.location.x = 13.5
        self.assertFalse(self.actions.dig_far(step))
        for _ in range(50):
            self.assertFalse(self.actions.dig_far(step))
        self.assertEqual(len(self.requests("bot:stop")), 1)
        self.assertEqual(len(self.requests("bot:dig")), 1)
        # A late walk reply must not terminate the dig that replaced it.
        self.reply("bot:result", walk)
        self.assertTrue(self.body.busy)
        self.reply("bot:dug", self.requests("bot:dig")[0])
        self.assertTrue(self.actions.dig_far(step))

    def test_body_serializes_walk_and_dig(self):
        self.assertTrue(self.body.start_dig(2, 64, 0))
        self.assertFalse(self.body.start_walk(8, 64, 0))
        self.reply("bot:dug", self.requests("bot:dig")[0])
        self.assertTrue(self.body.start_walk(8, 64, 0))
        self.assertFalse(self.body.start_dig(3, 64, 0))

    def test_mining_same_coordinate_gets_new_retry_budget(self):
        self.body.DIG_DEDUP_SECONDS = 0
        step = Step("dig_far", 2, 64, 0, async_op=True)
        key = self.actions.key_of(step)
        self.actions.dig_far(step)
        self.reply("bot:dug", self.requests("bot:dig")[-1], False, e="blocked")
        self.assertFalse(self.actions.dig_far(step))
        self.assertEqual(self.actions._tries[key], 1)
        self.actions.dig_far(step)
        self.reply("bot:dug", self.requests("bot:dig")[-1])
        self.assertTrue(self.actions.dig_far(step))
        self.assertNotIn(key, self.actions._started)
        self.assertNotIn(key, self.actions._tries)
        self.assertFalse(self.actions.dig_far(step))
        self.assertEqual(len(self.requests("bot:dig")), 3)

    def test_attack_waits_and_same_target_can_be_attacked_again(self):
        step = Step("attack", 0, 0, 0, note="zombie", async_op=True)
        for _ in range(100):
            self.assertFalse(self.actions.attack_real(step))
        self.assertEqual(len(self.requests("bot:attack")), 1)
        self.reply("bot:result", self.requests("bot:attack")[0])
        self.assertTrue(self.actions.attack_real(step))
        self.assertFalse(self.actions.attack_real(step))
        self.assertEqual(len(self.requests("bot:attack")), 2)

    def test_attack_retries_only_completed_failures_and_stops(self):
        step = Step("attack", 0, 0, 0, note="zombie", async_op=True)
        for attempt in range(self.actions.ATTACK_MAX_ATTEMPTS):
            self.assertFalse(self.actions.attack_real(step))
            self.reply("bot:result", self.requests("bot:attack")[-1], False, e="blocked")
            terminal = self.actions.attack_real(step)
            self.assertEqual(terminal, attempt == self.actions.ATTACK_MAX_ATTEMPTS - 1)
        self.assertIn("blocked", self.actions.pop_gave_up(("attack",)))
        self.assertEqual(len(self.requests("bot:attack")), self.actions.ATTACK_MAX_ATTEMPTS)

    def test_consume_full_health_waits_for_consumption_receipt(self):
        step = Step("consume", 0, 0, 0, note="bread", async_op=True)
        for _ in range(100):
            self.assertFalse(self.actions.consume_real(step))
        self.assertEqual(len(self.requests("bot:consume")), 1)
        self.reply("bot:result", self.requests("bot:consume")[0])
        self.assertTrue(self.actions.consume_real(step))
        self.assertEqual(self.body.actor.health, 20)

    def test_place_does_not_treat_existing_wrong_block_as_success(self):
        step = Step("place_real", 2, 64, 0, block="minecraft:glass", async_op=True)
        for _ in range(30):
            self.assertFalse(self.actions.place_real(step))
        self.assertEqual(len(self.requests("bot:place")), 1)
        self.reply("bot:result", self.requests("bot:place")[0], False, e="occupied")
        self.assertFalse(self.actions.place_real(step))
        self.assertFalse(self.actions.place_real(step))
        self.assertEqual(len(self.requests("bot:place")), 2)

    def test_container_retries_after_failure_and_returns_output(self):
        step = Step("container", 0, 64, 0, note='{"op":"craft","grid":2}', async_op=True)
        self.assertFalse(self.actions.container_op(step))
        self.reply("bot:result", self.requests("bot:container")[0], False, e="busy")
        self.assertFalse(self.actions.container_op(step))
        self.assertFalse(self.actions.container_op(step))
        self.assertEqual(len(self.requests("bot:container")), 2)
        self.reply("bot:result", self.requests("bot:container")[1], made={"id": "stick", "count": 4})
        self.assertTrue(self.actions.container_op(step))
        self.assertEqual(self.actions.take_container_made(), {"id": "stick", "count": 4})
        self.assertEqual(self.actions.take_container_made(), {})

    def test_reset_ignores_old_callback(self):
        step = Step("attack", 0, 0, 0, note="zombie", async_op=True)
        self.actions.attack_real(step)
        old = self.requests("bot:attack")[0]
        self.actions.reset()
        self.actions.attack_real(step)
        current = self.requests("bot:attack")[1]
        self.reply("bot:result", old)
        self.assertFalse(self.actions.attack_real(step))
        self.reply("bot:result", current)
        self.assertTrue(self.actions.attack_real(step))

    def test_smelt_timeout_covers_entire_batch(self):
        got = []
        rid = self.bridge.container("Alice", 0, 64, 0, {"op": "smelt", "count": 8},
                                    callback=lambda *args: got.append(args))
        started = self.bridge._pending[rid].created_at
        with patch("endstone_arc_fake_player.bridge.time.monotonic", return_value=started + 31):
            self.assertEqual(self.bridge.expire_pending(), 0)
        with patch("endstone_arc_fake_player.bridge.time.monotonic", return_value=started + 107):
            self.assertEqual(self.bridge.expire_pending(), 1)
        self.assertEqual(got[0][0], False)

    def test_async_failure_is_not_counted_as_success(self):
        executor = BatchExecutor(self.log)
        plan = Plan("blocked action", [Step("attack", 0, 0, 0, async_op=True)])
        executor.register("attack", lambda _: plan.note_failed("blocked"))
        executor.submit(plan)
        executor.tick()
        self.assertEqual(plan.state, PlanState.FAILED)
        self.assertEqual((plan.done_count, plan.fail_count, plan.cursor), (0, 1, 1))

    def test_near_dig_failure_key_matches_handler(self):
        self.assertEqual(self.actions.key_of(Step("dig_near", 2, 64, 0)), ("dig", 2, 64, 0))

    def test_workbench_craft_includes_output(self):
        compiler = ActionCompiler(self.world, self.log)
        compiler._find_blocks = lambda *_: [(1, 64, 0)]
        result = compiler._do_craft(self.body.actor, {"item": "wooden_pickaxe"}, {})
        self.assertIsNotNone(result.plan)
        spec = json.loads(result.plan.steps[-1].note)
        self.assertEqual((spec["grid"], spec["result"], spec["count"]), (3, "wooden_pickaxe", 1))

    def test_eat_plan_waits_for_async_consumption(self):
        self.world.scan_inventory = lambda _: [("bread", 2)]
        compiler = ActionCompiler(self.world, self.log)
        result = compiler._do_eat(self.body.actor, {}, {})
        self.assertTrue(result.plan.steps[0].async_op)
        executor = BatchExecutor(self.log)
        executor.register("consume", self.actions.consume_real)
        executor.submit(result.plan)
        executor.tick()
        self.assertEqual(result.plan.cursor, 0)
        self.reply("bot:result", self.requests("bot:consume")[0])
        executor.tick()
        self.assertEqual(result.plan.state, PlanState.DONE)


def main():
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(Interactions))
    if not result.wasSuccessful():
        return 1
    print(f"全部 {result.testsRun} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
