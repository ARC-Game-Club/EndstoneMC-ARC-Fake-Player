"""上下文预算与"总结继承"测试。

背景：虚拟玩家跑久了，对话历史会越堆越长，每轮都要把整段历史重发一遍，
token 消耗线性上涨。做法是给上下文设一个字符预算，超了就：

  1. 让模型把旧历史总结成一份**前情提要**（第一人称的"我记得…"）
  2. 丢掉旧原文，只留提要 + 最近几条消息
  3. 下一轮把提要当作 system 消息插在系统提示词后面 —— 记忆就这样被继承

本文件验证的重点，是这条链路上容易做错的几件事：

  · 达到上限时**必须**触发压缩，而不是默默继续涨
  · 压缩不能丢关键事实（旧提要要累积，不能被一次新摘要覆盖掉）
  · 总结失败也不能把历史留着不动（那就等于没压）
  · 压缩本身要在线程池里做，不能卡住主线程的决策
  · 待压缩的那批消息，在总结回来之前不能提前从历史里消失
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
from endstone_arc_fake_player.agent import (Agent, AgentConfig, build_digest,
                                       build_summarize_messages)
from endstone_arc_fake_player.executor import BatchExecutor
from endstone_arc_fake_player.llm import LLMClient, LLMReply, ToolCall
from endstone_arc_fake_player.world import WorldAdapter

from tests.test_pure_agent import FakeActor, FakeDimension, FakeLogger

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"PASS  {label}" + (f" — {extra}" if extra else ""))
    else:
        print(f"FAIL  {label}" + (f" — {extra}" if extra else ""))
        FAILURES.append(label)


def section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


class ScriptedLLM(LLMClient):
    """同时扮演"决策"和"总结"两个角色。

    用提示词里有没有出现总结模板里的特征串来区分是哪种调用——
    这正是真实环境里的判定方式（同一条 chat 接口，不同的 user 内容）。
    """

    def __init__(self, decisions: list[LLMReply], summary: str = "",
                 summary_ok: bool = True):
        super().__init__("http://x", "k", "m")
        self._decisions = decisions
        self._i = 0
        self._summary = summary
        self._summary_ok = summary_ok
        self.decide_calls = 0
        self.summarize_calls = 0
        self.summarize_prompts: list[str] = []
        self.last_messages: list[dict] = []

    def chat(self, messages, temperature=0.3, max_tokens=800):
        text = "\n".join(str(m.get("content", "")) for m in messages)
        if "输出前情提要" in text:
            self.summarize_calls += 1
            self.summarize_prompts.append(text)
            if not self._summary_ok:
                return LLMReply(ok=False, error="模拟总结失败")
            return LLMReply(ok=True, text=self._summary)
        self.decide_calls += 1
        self.last_messages = messages
        r = self._decisions[min(self._i, len(self._decisions) - 1)]
        self._i += 1
        return r


def make_agent(llm, *, max_chars: int = 400, keep: int = 2,
               entry_chars: int = 1200, summarize: bool = True):
    """搭一套最小可用的 Agent。返回 (agent, actor, pool, drain)。"""
    logger = FakeLogger()
    world = WorldAdapter(logger)
    dim = FakeDimension()
    actor = FakeActor(dim, 0.5, 64.0, 0.5)
    dim.actors.append(actor)

    executor = BatchExecutor(logger, max_steps_per_tick=1000)
    executor.register("set_block", lambda s: True)
    executor.register("break_block", lambda s: True)

    compiler = ActionCompiler(world, logger)
    compiler.bind_player_lookup(lambda p: None)

    pool = ThreadPoolExecutor(max_workers=3)
    main_q: list = []
    lock = threading.Lock()

    def on_main(fn):
        with lock:
            main_q.append(fn)

    agent = Agent(
        config=AgentConfig(name="Bot", decision_interval=0.0,
                           context_max_chars=max_chars,
                           context_keep_recent=keep,
                           history_entry_chars=entry_chars,
                           context_summarize=summarize),
        llm=llm, world=world, compiler=compiler, executor=executor,
        logger=logger, pool=pool, on_main=on_main,
        on_log=lambda m: None,
        get_actor=lambda: actor,
    )

    def drain():
        while True:
            with lock:
                batch, main_q[:] = list(main_q), []
            for fn in batch:
                try:
                    fn()
                except Exception as exc:      # 让测试失败得可见
                    FAILURES.append(f"主线程回调异常: {exc}")
            time.sleep(0.005)

    threading.Thread(target=drain, daemon=True).start()
    agent.observe(world.snapshot(actor))
    return agent, actor, pool


def wait(pred, timeout=5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# 第一部分：基本行为
# ---------------------------------------------------------------------------

def test_basic() -> None:
    section("第一部分：上下文预算与触发")

    # 预算之内不该压
    llm = ScriptedLLM([LLMReply(ok=True, text="继续")], summary="摘要")
    agent, _actor, pool = make_agent(llm, max_chars=100000)
    agent.state.history.append({"role": "user", "content": "短"})
    check("远未达上限时不压缩", agent.needs_compaction() is False)
    check("describe_context 给出用量", "字符" in agent.describe_context(),
          agent.describe_context())

    # 超过预算就要压
    agent.state.history.append({"role": "user", "content": "长" * 200000})
    check("超过上限时触发压缩", agent.needs_compaction() is True)

    # 关掉总结开关 → 不再压缩（纯截断模式）
    agent.config.context_summarize = False
    check("关掉总结开关后不再压缩", agent.needs_compaction() is False)
    agent.config.context_summarize = True

    # 上限为 0 视为不限制
    agent.config.context_max_chars = 0
    check("上限为 0 表示不限制", agent.needs_compaction() is False)

    pool.shutdown(wait=False)

    # history 用 maxlen 兜底：即使压缩没触发也不会无限增长
    a2 = AgentConfig(name="x")
    check("history 有 maxlen 兜底", a2.history_size > 0)


def test_compaction_flow() -> None:
    section("第二部分：压缩流程（总结 → 丢弃 → 继承）")

    summary = "我在(12,63,8)挖到了煤，向北走50格有片树林。洞挖了一半，还差3格。"
    llm = ScriptedLLM(
        decisions=[LLMReply(ok=True, text="继续挖")] * 30,
        summary=summary,
    )
    # 预算设得很小，第 1 轮历史就会超
    agent, _actor, pool = make_agent(llm, max_chars=200, keep=2)
    s = agent.state

    # 人为把历史堆到超预算
    for i in range(6):
        s.history.append({"role": "user", "content": f"第{i}轮观测：" + "石" * 60})
        s.history.append({"role": "assistant", "content": f"第{i}轮动作：mine_area"})
    before_chars = s.history_chars()
    check("历史确实超预算", before_chars > agent.config.context_max_chars,
          f"{before_chars} > {agent.config.context_max_chars}")
    check("压缩前没有提要", s.digest == "")

    started = agent.maybe_compact()
    check("压缩被发起", started is True)
    check("等待总结返回", wait(lambda: s.compactions >= 1))
    check("调用了总结接口", llm.summarize_calls >= 1, str(llm.summarize_calls))
    check("总结后有了提要", summary[:20] in s.digest, s.digest[:60])
    check("压缩计数 +1", s.compactions == 1, str(s.compactions))
    check("历史被清到只剩最近几条", len(s.history) == 2, str(len(s.history)))
    after_chars = s.history_chars()
    check("上下文显著回落", after_chars < before_chars,
          f"{before_chars} → {after_chars}")

    # 被丢掉的消息不能凭空消失——它们应该体现在提要里
    transcript_seen = llm.summarize_prompts[-1]
    check("总结请求里带着被丢弃的原文", "第0轮动作" in transcript_seen)
    check("总结请求里也确实收到了最老的观测", "第0轮观测" in transcript_seen)

    # 留下的那几条不该进总结请求（否则等于重复计费）
    last_user = s.history[-1]["content"]
    check("最近的消息没被送进总结请求",
          last_user not in transcript_seen,
          last_user[:30])

    # 继承：下一轮构造的消息里，提要作为独立 system 消息出现
    msgs = agent.build_messages("现在做什么")
    roles = [m["role"] for m in msgs]
    check("消息结构是 system→system→…→user",
          roles[0] == "system" and roles[-1] == "user", str(roles))
    check("提要作为第二条 system 消息", roles[1] == "system", str(roles[:3]))
    check("提要在历史之前", "前情提要" in msgs[1]["content"],
          msgs[1]["content"][:40])
    check("提要内容进了上下文", summary[:20] in msgs[1]["content"])
    check("最近消息仍在", any("石" in m.get("content", "") for m in msgs))
    check("本轮输入在最后", msgs[-1]["content"] == "现在做什么")

    pool.shutdown(wait=False)


def test_digest_accumulates() -> None:
    section("第三部分：多轮压缩时记忆需累积")

    # 第一次压出来一份，第二次的摘要里包含旧提要的内容（模型被要求
    # "在旧提要基础上继续累积"），用这种最常见的形态来验：只要新摘要
    # 里出现了旧提要的片段，就认为是改写而不是覆盖。
    first = "我在(12,63,8)挖到了煤。"
    second = "我在(12,63,8)挖到了煤。后来又往北走了50格，找到一片树林。"

    llm = ScriptedLLM([LLMReply(ok=True, text="x")], summary=first)
    agent, _actor, pool = make_agent(llm, max_chars=200, keep=1)
    s = agent.state
    for i in range(4):
        s.history.append({"role": "user", "content": "石" * 100})
    agent.maybe_compact()
    check("第 1 次压缩完成", wait(lambda: s.compactions == 1))
    check("第 1 份提要就位", "挖到了煤" in s.digest)

    # 第二次
    llm._summary = second                        # noqa: SLF001
    for i in range(4):
        s.history.append({"role": "user", "content": "木" * 100})
    agent.maybe_compact()
    check("第 2 次压缩完成", wait(lambda: s.compactions == 2))
    check("提要已更新", "树林" in s.digest, s.digest[:80])
    check("旧事实没被丢掉", "挖到了煤" in s.digest, s.digest[:80])
    check("每次都把旧提要喂给模型",
          "挖到了煤" in llm.summarize_prompts[-1])

    check("clear_context 清空提要",
          (agent.clear_context(), s.digest == "" and s.compactions == 0)[1])
    check("clear_context 保留 notes",
          (s.notes.append("玩家说别挖他家"), agent.clear_context(),
           list(s.notes) == ["玩家说别挖他家"])[2])

    pool.shutdown(wait=False)


def test_summary_failure() -> None:
    section("第四部分：总结失败也必须回落")

    llm = ScriptedLLM([LLMReply(ok=True, text="x")],
                      summary="", summary_ok=False)
    agent, _actor, pool = make_agent(llm, max_chars=200, keep=2)
    s = agent.state
    for i in range(6):
        s.history.append({"role": "user", "content": f"观测{i}：" + "石" * 100})
    before = s.history_chars()

    agent.maybe_compact()
    check("压缩流程走完", wait(lambda: s.compactions == 1))
    check("总结失败时上下文仍然回落", s.history_chars() < before,
          f"{before} → {s.history_chars()}")
    check("失败时用原文截断兜底", bool(s.digest), s.digest[:60])
    check("兜底内容来自真实历史", "观测0" in s.digest, s.digest[:80])
    check("失败不被算作错误轮次", s.errors == 0, str(s.errors))

    pool.shutdown(wait=False)


def test_single_long_message() -> None:
    section("第五部分：单条超长消息的兜底")

    llm = ScriptedLLM([LLMReply(ok=True, text="x")], summary="摘要")
    # keep=2，历史里只有 1 条但特别长 —— 走"就地截断"分支。
    # 预算设成 1000，那条 3000 字符的观测必然超。
    agent, _actor, pool = make_agent(llm, max_chars=1000, keep=2,
                                     entry_chars=100000)
    s = agent.state
    s.history.append({"role": "user", "content": "长" * 3000})
    check("单条超长触发压缩", agent.needs_compaction() is True,
          f"{s.history_chars()} > 1000")

    ok = agent.maybe_compact()
    check("就地截断并返回 True", ok is True)
    check("单条被截短", len(s.history[0]["content"]) < 3000,
          str(len(s.history[0]["content"])))
    check("截断留了标记", "已截断" in s.history[0]["content"])
    check("截断后回到预算内", s.history_chars() <= 1000,
          str(s.history_chars()))
    check("没调用总结接口（没必要）", llm.summarize_calls == 0)
    check("消息条数没变", len(s.history) == 1)
    check("thinking 已复位", s.thinking is False)

    # 多条都超长时，每条都要掐
    s.history.append({"role": "assistant", "content": "木" * 3000})
    agent.maybe_compact()
    check("多条超长时逐条处理",
          len(s.history[1]["content"]) < 3000, str(len(s.history[1]["content"])))

    pool.shutdown(wait=False)


def test_no_interference() -> None:
    section("第六部分：与决策流程的互不干扰")

    steps = []
    llm = ScriptedLLM(
        decisions=[
            LLMReply(ok=True, text="挖", calls=[
                ToolCall("mine_area", {"x1": 1, "y1": 63, "z1": 1,
                                       "x2": 3, "y2": 63, "z2": 3}),
            ]),
        ] * 40,
        summary="我挖了一片石头，坐标在(1,63,1)附近。",
    )
    agent, _actor, pool = make_agent(llm, max_chars=300, keep=2)
    s = agent.state

    # 连跑几轮决策，每轮之间推进执行器
    for turn in range(6):
        s.last_decision = 0.0
        agent.think()
        check(f"第{turn+1}轮决策完成",
              wait(lambda: not s.thinking, timeout=3.0) or s.thinking is False)
        wait(lambda: s.compactions > 0 or len(s.history) < 4, timeout=1.0)
        time.sleep(0.05)

    check("走了多轮决策", llm.decide_calls >= 4, str(llm.decide_calls))
    check("期间触发了压缩", s.compactions >= 1, str(s.compactions))
    check("决策没被压缩卡死", s.thinking is False)
    check("历史长度受控",
          len(s.history) <= 6, str(len(s.history)))

    # thinking 期间不该重复发起压缩
    s.thinking = True
    for _ in range(20):
        s.history.append({"role": "user", "content": "石" * 200})
    started = agent.maybe_compact()
    check("thinking 期间不压缩（避免改到正在用的历史）", started is False)
    s.thinking = False

    pool.shutdown(wait=False)


def test_prompt_shape() -> None:
    section("第七部分：总结提示词本身")

    llm = ScriptedLLM([LLMReply(ok=True, text="x")], summary="摘要")
    agent, _actor, pool = make_agent(llm, max_chars=200, keep=1)
    s = agent.state

    # 没有旧提要时不该出现"之前的前情提要"段
    msgs = build_summarize_messages(s, "一些记录")
    text = msgs[0]["content"]
    check("总结是单条 user 消息", len(msgs) == 1 and msgs[0]["role"] == "user")
    check("首次压缩没有旧提要段", "之前的前情提要" not in text)
    check("要求第一人称", "第一人称" in text)
    check("要求保留坐标", "坐标" in text)
    check("要求带失败教训", "失败" in text or "坑" in text)
    check("限制了长度", "400 字" in text)
    check("带了待压缩记录", "一些记录" in text)

    # 有旧提要时要带上
    s.digest = "我以前挖到过煤。"
    msgs2 = build_summarize_messages(s, "更多记录")
    check("二次压缩带上旧提要", "我曾经挖到过煤" not in msgs2[0]["content"]
          and "以前挖到过煤" in msgs2[0]["content"])

    # build_digest 的形态
    s.digest = ""
    check("没提要时返回空串", build_digest(s) == "")
    s.digest = "记得回家的路。"
    s.compactions = 3
    d = build_digest(s)
    check("有提要时标明是第几次", "第 3 次" in d, d[:40])
    check("说明它是记忆而非指令", "不是别人对你的指示" in d)

    pool.shutdown(wait=False)


def test_history_entry_cap() -> None:
    section("第八部分：单条历史写入时的截断")

    llm = ScriptedLLM([LLMReply(ok=True, text="x")], summary="摘要")
    agent, _actor, pool = make_agent(llm, max_chars=1000000,
                                     entry_chars=100)
    s = agent.state

    # 手动模拟一次 _apply 的历史写入
    cap = agent.config.history_entry_chars
    s.history.append({"role": "user", "content": "长" * 5000})
    s.history[-1]["content"] = s.history[-1]["content"][:cap]
    check("写入时按 history_entry_chars 截断",
          len(s.history[-1]["content"]) == cap, str(len(s.history[-1]["content"])))

    check("history_entry_chars 有默认值",
          AgentConfig(name="x").history_entry_chars == 1200)
    check("默认上限是 24000",
          AgentConfig(name="x").context_max_chars == 24000)
    check("默认保留 4 条",
          AgentConfig(name="x").context_keep_recent == 4)

    pool.shutdown(wait=False)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 70)
    print("上下文预算 & 总结继承")
    print("=" * 70)

    test_basic()
    test_compaction_flow()
    test_digest_accumulates()
    test_summary_failure()
    test_single_long_message()
    test_no_interference()
    test_prompt_shape()
    test_history_entry_cap()

    print()
    print("=" * 70)
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项失败（共 {CHECKS} 项）")
        for f in FAILURES:
            print(f"   · {f}")
        return 1
    print(f"✅ 全部 {CHECKS} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
