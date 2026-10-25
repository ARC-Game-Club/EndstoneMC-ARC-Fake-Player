"""验证 Agent 主循环：接收快照 → 调 LLM → 编译计划 → 执行 → 复盘。

用假 LLM 驱动，检查"一次工具调用带动多个步骤"在完整链路里成立。
"""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Endstone 的桩必须在**任何** `endstone_arc_fake_player` 导入之前装好。
#
# 包根 `__init__.py` 会 `from .plugin import ArcFakePlayerPlugin`，而
# `plugin.py` 顶层就写着 `from endstone import ColorFormat as C`，
# 所以只要 import 本包的任何子模块，父包的 __init__ 都会先跑一遍，
# 没装桩就直接 ModuleNotFoundError。
#
# 走 run_all.py 时它已经装过了（`install()` 幂等）；单独跑本文件时
# 靠的就是这两行。**别把导入挪到这几行上面去。**
from tests.endstone_stub import install as _install_stub  # noqa: E402

_install_stub()

from endstone_arc_fake_player.actions import ActionCompiler
from endstone_arc_fake_player.agent import Agent, AgentConfig
from endstone_arc_fake_player.executor import BatchExecutor
from endstone_arc_fake_player.llm import LLMClient, LLMReply, ToolCall
from endstone_arc_fake_player.world import WorldAdapter

from tests.test_pure_agent import FakeActor, FakeDimension, FakeLogger

PASS, FAIL = [], []


def check(label: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(label)
    print(f"{'PASS' if cond else 'FAIL'}  {label}{(' — ' + extra) if extra else ''}")


class ScriptedLLM(LLMClient):
    """按脚本返回 tool_calls。"""

    def __init__(self, script):
        super().__init__("http://x", "k", "m")
        self._script = script
        self._i = 0
        self.calls = 0
        self.last_user = ""

    def chat(self, messages, temperature=0.3, max_tokens=800):
        self.calls += 1
        self.last_user = messages[-1]["content"]
        r = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return r


def wait(pred, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    logger = FakeLogger()
    world = WorldAdapter(logger)
    dim = FakeDimension()
    actor = FakeActor(dim, 0.5, 64.0, 0.5)
    dim.actors.append(actor)

    executor = BatchExecutor(logger, max_steps_per_tick=1000)  # 测试里一次性跑完
    applied = {"set": 0, "break": 0, "walk": 0}
    executor.register("set_block", lambda s: (applied.__setitem__("set", applied["set"] + 1), True)[1])
    executor.register("break_block", lambda s: (applied.__setitem__("break", applied["break"] + 1), True)[1])
    executor.register("walk_to", lambda s: (applied.__setitem__("walk", applied["walk"] + 1),
                                            actor.teleport(type(actor.location)(s.x, s.y, s.z)), True)[1])

    compiler = ActionCompiler(world, logger)
    compiler.bind_player_lookup(lambda p: None)

    script = [
        # 第 1 轮：挖一片区域（1 次调用 → N 步）。y=63 是地面层。
        LLMReply(ok=True, text="先把这片石头挖掉。", calls=[
            ToolCall("mine_area", {"x1": 1, "y1": 63, "z1": 1, "x2": 3, "y2": 63, "z2": 3}),
            ToolCall("goal", {"text": "收集 20 个石头"}),
        ]),
        # 第 2 轮：继续挖 + 说话（1 次调用 → N 步）
        LLMReply(ok=True, text="继续。", calls=[
            ToolCall("mine_area", {"x1": 4, "y1": 63, "z1": 4, "x2": 6, "y2": 63, "z2": 6}),
            ToolCall("say", {"message": "我在挖石头"}),
        ]),
        # 第 3 轮：say 放最前 + 两个后续工具——旧版在循环里重复结算 ctx，
        # 会把这一句广播 3 遍（"一条消息发三遍"的回归用例）
        LLMReply(ok=True, text="先说一句再干活。", calls=[
            ToolCall("say", {"message": "大家注意"}),
            ToolCall("mine_area", {"x1": 7, "y1": 63, "z1": 7, "x2": 9, "y2": 63, "z2": 9}),
            ToolCall("goal", {"text": "挖完这片就收工"}),
        ]),
    ]
    llm = ScriptedLLM(script)
    pool = ThreadPoolExecutor(max_workers=2)

    main_q = []
    lock = threading.Lock()

    def on_main(fn):
        with lock:
            main_q.append(fn)

    said = []
    logs = []
    agent = Agent(
        config=AgentConfig(name="Bot", decision_interval=0.0),
        llm=llm, world=world, compiler=compiler, executor=executor,
        logger=logger, pool=pool, on_main=on_main,
        on_say=lambda n, m: said.append((n, m)),
        on_log=logs.append,
        get_actor=lambda: actor,
    )

    def drain():
        while True:
            with lock:
                batch, main_q[:] = list(main_q), []
            for fn in batch:
                fn()
            time.sleep(0.01)

    threading.Thread(target=drain, daemon=True).start()

    # 给一个快照并触发决策
    agent.observe(world.snapshot(actor))
    agent.think()
    check("第 1 轮提交", wait(lambda: not agent.state.thinking and agent._inflight))

    # 推进执行器直到空
    for _ in range(50):
        if not executor.busy:
            break
        done = executor.tick()
        agent.reap_finished(done)
        time.sleep(0.01)

    check("一次 mine_area 调用执行了 9 个方块", applied["break"] == 9, f"实际 {applied['break']}")
    check("goal 被记录", agent.config.goal == "收集 20 个石头", agent.config.goal)
    check("goal 不产生游戏步骤", applied["set"] == 0)
    check("计划已结算（inflight 清空）", not agent._inflight, str(len(agent._inflight)))

    # 第 2 轮
    agent.state.last_decision = 0.0
    agent.think()
    check("第 2 轮提交", wait(lambda: not agent.state.thinking))
    for _ in range(50):
        if not executor.busy:
            break
        done = executor.tick()
        agent.reap_finished(done)
        time.sleep(0.01)

    check("第 2 轮又执行 9 个方块", applied["break"] == 18, f"实际 {applied['break']}")
    check("say 触发广播", said == [("Bot", "我在挖石头")], str(said))
    check("say 不产生游戏步骤", applied["set"] == 0)

    # 第 3 轮：say 在最前，后面还有两个工具
    agent.state.last_decision = 0.0
    agent.think()
    check("第 3 轮提交", wait(lambda: not agent.state.thinking and len(said) >= 2))
    for _ in range(50):
        if not executor.busy:
            break
        done = executor.tick()
        agent.reap_finished(done)
        time.sleep(0.01)
    check("say 只广播一次（旧版会三连发）",
          said == [("Bot", "我在挖石头"), ("Bot", "大家注意")], str(said))
    check("第 3 轮照常执行动作", applied["break"] == 27, f"实际 {applied['break']}")

    # 统计
    s = agent.snapshot()
    print()
    print(f"统计: 工具调用 {s['tool_calls']} 次 → 实际执行 {s['steps']} 步 "
          f"(效率 {s['efficiency']}x)")
    check("效率倍数 > 3（每次调用带动多步）", s["efficiency"] > 3, f"{s['efficiency']}x")
    check("记忆在增长", len(agent.state.history) >= 4, str(len(agent.state.history)))

    # 事件回灌
    check("动作结果进入事件流（供下轮决策）",
          len(agent.state.recent_events) > 0,
          str(list(agent.state.recent_events)[:3]))

    # ---- 随机决策间隔 ----
    # 固定 min=max=50：think 后应掷出 50 秒间隔，30 秒时不再思考、51 秒时到点
    agent.config.decision_interval_min = 50.0
    agent.config.decision_interval_max = 50.0
    agent.state.last_decision = 0.0
    agent.state.decision_gap = 0.0
    agent.think()
    check("随机间隔已掷出", wait(lambda: abs(agent.state.decision_gap - 50.0) < 1e-6),
          str(agent.state.decision_gap))
    # 探测用的 think 会提交计划，先排干执行器，别让 wait_for_plan 闸门干扰
    wait(lambda: not agent.state.thinking and not executor.busy)
    for _ in range(50):
        if not executor.busy:
            break
        done = executor.tick()
        agent.reap_finished(done)
        time.sleep(0.01)
    now = time.monotonic()
    agent.state.last_decision = now - 30.0
    check("间隔内不再思考", not agent.should_think(time.monotonic()))
    agent.state.last_decision = now - 51.0
    check("到点恢复思考", agent.should_think(time.monotonic()))
    # 未启用随机（max=0）时回退固定 interval
    agent.config.decision_interval_max = 0.0
    agent.state.decision_gap = 0.0
    agent.config.decision_interval = 12.0
    agent.state.last_decision = now - 13.0
    check("固定间隔模式下到点思考", agent.should_think(time.monotonic()))
    agent.state.last_decision = now - 5.0
    check("固定间隔模式下间隔内不思考", not agent.should_think(time.monotonic()))

    # ---- 自发发言冷却 ----
    # 开启 30~30 分钟冷却：自发轮的 say 被吞；唤醒轮（wake）不受限
    agent.config.spontaneous_say_min = 30.0
    agent.config.spontaneous_say_max = 30.0
    said_before = len(said)
    agent.state.woken = False
    agent.state.next_spontaneous_say = time.monotonic() + 1000.0
    agent.state.last_decision = 0.0
    agent.think()
    wait(lambda: not agent.state.thinking and not executor.busy)
    for _ in range(50):
        if not executor.busy:
            break
        done = executor.tick()
        agent.reap_finished(done)
        time.sleep(0.01)
    check("冷却内的自发发言被吞", len(said) == said_before, str(said[said_before:]))
    check("冷却时刻已重掷", agent.state.next_spontaneous_say > time.monotonic())
    # 唤醒轮：应答式说话放行
    agent.state.last_decision = 0.0
    agent.wake()
    agent.think()
    wait(lambda: not agent.state.thinking and not executor.busy)
    for _ in range(50):
        if not executor.busy:
            break
        done = executor.tick()
        agent.reap_finished(done)
        time.sleep(0.01)
    check("唤醒轮应答式说话放行", len(said) > said_before, str(said[said_before:]))
    agent.config.spontaneous_say_min = 0.0
    agent.config.spontaneous_say_max = 0.0

    # ---- 说话方式段注入 ----
    from endstone_arc_fake_player.agent import build_system as _bs
    agent.config.speech = "短、口语、有态度，禁'值得注意'"
    sys_text3 = _bs(agent.state)
    check("说话守则进系统提示词", "短、口语、有态度" in sys_text3)
    agent.config.speech = ""

    # ---- 长期使命注入 ----
    from endstone_arc_fake_player.agent import build_system
    probe = agent.state
    old_long = probe.config.long_goal
    probe.config.long_goal = "枪战房常驻，有人开局就加入对枪"
    sys_text = build_system(probe)
    check("长期使命进系统提示词", "枪战房常驻" in sys_text and "长期使命" in sys_text)
    check("使命段在目标段之前", sys_text.index("长期使命") < sys_text.index("长期目标"))
    probe.config.long_goal = old_long
    sys_text2 = build_system(probe)
    check("未设使命时显示占位文案", "没有指定" in sys_text2)

    # 检查最终提示词里包含上批动作
    agent.state.last_decision = 0.0
    agent.observe(world.snapshot(actor))
    agent.think()
    wait(lambda: llm.calls >= 3)
    check("提示词里带上了上批动作结果",
          "mine_area" in llm.last_user or "上批动作" in llm.last_user,
          "")

    pool.shutdown(wait=False)
    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败: {FAIL}")
        return 1
    print(f"✅ 全部 {len(PASS)} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
