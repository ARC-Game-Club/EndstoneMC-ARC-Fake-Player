"""批量指令执行器 —— LLM 只描述"要什么"，插件负责"怎么做"。

设计动机
--------
朴素做法是让模型逐个方块地动作：

    goto(10,64,10) → mine(10,64,10) → goto(11,64,10) → mine(11,64,10) → ...

每个方块至少 2 次模型往返。挖一面 5x5 的墙 = 50 次调用，token 直接爆炸。

本模块把"重复"全部下沉到插件里。模型只要说一次：

    {"op": "mine_area", "x1":10,"y1":64,"z1":10, "x2":14,"y2":64,"z2":14}

插件就会自己规划顺序、自己逐格执行、自己处理失败跳过、最后汇报结果。
同样的活，模型往返次数从 50 次降到 1 次。

核心抽象
--------
ExecutionPlan  一串待执行的原子步骤，可被 tick 驱动逐格推进
BatchExecutor  挂在服务器主线程上的执行引擎，每 tick 推进若干步
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator


# ---------------------------------------------------------------------------
# 计划状态
# ---------------------------------------------------------------------------

class PlanState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Step:
    """一个原子步骤。"""

    op: str                      # set_block / break_block / dig_near / walk_to ...
    x: int
    y: int
    z: int
    block: str | None = None     # set_block / place_real 用
    note: str = ""
    #: 这个步骤是"异步的"——发起之后要等物理世界给结论。
    #: 异步步骤返回 False 时不是失败，而是"还没完"，执行器应当
    #: 把它放回队列头部稍后重试，而不是推进到下一步。
    async_op: bool = False


@dataclass
class Plan:
    """一个可被逐 tick 推进的执行计划。"""

    label: str
    steps: list[Step]
    state: PlanState = PlanState.PENDING
    cursor: int = 0
    done_count: int = 0
    fail_count: int = 0
    skipped: list[str] = field(default_factory=list)
    created_at: float = 0.0
    steps_per_tick: int = 8      # 每 tick 推进几步——控制对服务器的影响
    until: Callable[[], bool] | None = None   # 可选的提前终止条件
    #: 当前正在等待完成的异步步骤名（空串表示没在等）
    pending_op: str = ""

    @property
    def total(self) -> int:
        # ⚠️ 这里**必须**是 `len(steps)`，绝不能把什么"重试额度"加进来。
        #
        # 踩过一次大坑（3.0.2）：曾把 `retries_left` 并进 `total`，本意是
        # "重试额度也算总尝试次数，游标追不上 total 就自动结算"。但
        # `retries_left` 是在计划开始处理时**预发**的，即使一个步骤都没
        # 失败也一直挂着，于是 `total` 虚高、`len(steps) < total`。
        # 游标把真实步骤走完后 `while cursor < total` 仍成立，下一行
        # `plan.steps[plan.cursor]` 直接 `IndexError: list index out of
        # range` —— 主循环每 tick 崩一次，用户看到的报错就是这个。
        #
        # 结论：游标只会**前进**，最多走到 `len(steps)`，所以 `total`
        # 必须就是 `len(steps)`，两者同构才不会越界。
        return len(self.steps)

    @property
    def progress(self) -> float:
        return self.done_count / self.total if self.total else 1.0

    def summary(self) -> str:
        parts = [f"{self.label}: {self.done_count}/{self.total} 完成"]
        if self.fail_count:
            parts.append(f"{self.fail_count} 失败")
        if self.skipped:
            parts.append(f"跳过 {len(self.skipped)} 个")
        if self.pending_op:
            parts.append(f"正在{self.pending_op}")
        return "，".join(parts)

    def abort(self, reason: str = "") -> None:
        """外部原因中止计划（身体消失、连接断开等）。

        和"某一步失败"不是一回事：失败是结果未知，这里是我们主动不干了。
        余下的步骤全部记入 skipped，这样统计里的步数仍然对得上。
        """
        self.skipped.append(reason or "已中止")
        self.done_count = self.total
        self.pending_op = ""
        self.state = PlanState.CANCELLED

    def note_failed(self, reason: str = "") -> bool:
        """标记当前这一步"已经放弃、按失败结算"。**返回 True**。

        为什么是 True
        --------------
        执行器只认 True/False，而这一步**确实有结论了**（结论是失败）。
        返回 False 会被解释成"还没结论"——对异步步骤（`async_op`）来说
        执行器会挂住 `pending_op` 并 `break`，整个计划停在这一步，永远
        结算不了，`_inflight` 也不释放，智能体从此发呆。

        调用方（`plugin._real_or_retry`）在 `RealBodyActions` 宣告放弃
        时调它：把失败记进计划，然后自己返回 True 交回执行器推进游标。

        ⚠️ 与"重试"无关。真正的重试发生在 `RealBodyActions` 内部
        （`_tries` 累加 + 退避 + `MAX_ATTEMPTS`），那里已经试够了才
        走到放弃。到了计划这一层，放弃就是放弃，只记账、不再回头。
        """
        self.pending_op = ""
        self.fail_count += 1
        if reason:
            self.skipped.append(reason)
        return True


# ---------------------------------------------------------------------------
# 几何工具：把区域展开成有序的步骤序列
# ---------------------------------------------------------------------------

def iter_box(
    x1: int, y1: int, z1: int, x2: int, y2: int, z2: int,
    *, order: str = "layer", max_steps: int = 4096,
) -> Iterator[tuple[int, int, int]]:
    """把一个长方体区域展开成逐格坐标。

    order 决定遍历顺序，这直接影响"看起来是否自然"：
      layer  从下往上逐层（盖房子最自然）
      bottom 从上往下逐层（拆房子，避免悬空掉方块）
      snake  逐层蛇形（减少来回跑动）
      nearest 由近及远（从原点扩散）
    """
    xa, xb = sorted((int(x1), int(x2)))
    ya, yb = sorted((int(y1), int(y2)))
    za, zb = sorted((int(z1), int(z2)))

    total = (xb - xa + 1) * (yb - ya + 1) * (zb - za + 1)
    if total > max_steps:
        raise ValueError(f"区域过大：{total} 格，上限 {max_steps} 格")

    if order == "bottom":
        ys = range(yb, ya - 1, -1)
    else:
        ys = range(ya, yb + 1)

    for y in ys:
        # snake：每一层交替方向，避免每次都从同一边折返
        flip = order == "snake" and (y - ya) % 2 == 1
        zs = range(zb, za - 1, -1) if flip else range(za, zb + 1)
        for z in zs:
            xs = range(xb, xa - 1, -1) if flip else range(xa, xb + 1)
            for x in xs:
                yield (x, y, z)


def iter_line(
    x1: int, y1: int, z1: int, x2: int, y2: int, z2: int, max_steps: int = 4096,
) -> Iterator[tuple[int, int, int]]:
    """沿直线逐格返回坐标（用于铺路、挖隧道）。"""
    dist = max(abs(x2 - x1), abs(y2 - y1), abs(z2 - z1)) + 1
    if dist > max_steps:
        raise ValueError(f"线路过长：{dist} 格，上限 {max_steps} 格")
    for i in range(dist):
        t = i / (dist - 1) if dist > 1 else 0.0
        yield (
            round(x1 + (x2 - x1) * t),
            round(y1 + (y2 - y1) * t),
            round(z1 + (z2 - z1) * t),
        )


def nearest_first(
    origin: tuple[int, int, int], cells: list[tuple[int, int, int]],
) -> list[tuple[int, int, int]]:
    """按到 origin 的直线距离排序——让机器人少跑路。"""
    ox, oy, oz = origin
    return sorted(cells, key=lambda c: math.dist((ox, oy, oz), c))


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------

class BatchExecutor:
    """在服务器主线程上逐 tick 推进计划队列。

    为什么是逐 tick 而不是一次性做完：一次性 set 几万个方块会造成
    单 tick 严重超时（服务器 TPS 崩掉）。分片执行让每次 tick 的工作量
    可控，同时用户看到的是"方块在一个个出现"，比瞬间成型更自然。
    """

    def __init__(self, logger: Any, max_steps_per_tick: int = 8) -> None:
        self._logger = logger
        self._plans: list[Plan] = []
        self._max_steps_per_tick = max_steps_per_tick
        self._handlers: dict[str, Callable[[Step], bool]] = {}
        self.completed: list[Plan] = []
        #: 每次调用步骤处理器**之前**会被调用，参数是"这个步骤属于哪个计划"。
        #:
        #: 为什么需要这个钩子：处理器只拿得到 `Step`，而"这一步放弃、按
        #: 失败结算"这个决定要落在 `Plan` 上（`Plan.note_failed`）。
        #: 与其给每一处 `register` 都加一个 plan 参数（改动面太大、还容易
        #: 漏），不如让执行器在调用点告诉外部"现在轮到谁了"。
        self.on_step: Callable[[Plan, Step], None] | None = None

    # -- 注册 -------------------------------------------------------------

    def register(self, op: str, handler: Callable[[Step], bool]) -> None:
        """注册一个步骤类型的执行函数。返回 True 表示成功。"""
        self._handlers[op] = handler

    # -- 提交 -------------------------------------------------------------

    def submit(self, plan: Plan) -> Plan:
        plan.state = PlanState.PENDING
        self._plans.append(plan)
        return plan

    def cancel_all(self, label_contains: str | None = None) -> int:
        n = 0
        for plan in self._plans:
            if label_contains and label_contains not in plan.label:
                continue
            plan.state = PlanState.CANCELLED
            n += 1
        self._plans = [p for p in self._plans if p.state != PlanState.CANCELLED]
        return n

    @property
    def active(self) -> list[Plan]:
        return [p for p in self._plans if p.state in (PlanState.PENDING, PlanState.RUNNING)]

    @property
    def busy(self) -> bool:
        return bool(self._plans)

    # -- 推进（每 tick 调用） ---------------------------------------------

    def tick(self) -> list[Plan]:
        """推进所有计划。返回本 tick 内完成的计划列表。"""
        finished: list[Plan] = []
        if not self._plans:
            return finished

        budget = self._max_steps_per_tick
        still_running: list[Plan] = []

        for plan in self._plans:
            if plan.state == PlanState.CANCELLED:
                continue
            plan.state = PlanState.RUNNING

            # 提前终止条件（比如机器人死了、被打断）
            if plan.until is not None:
                try:
                    if plan.until():
                        plan.state = PlanState.CANCELLED
                        plan.skipped.append("触发终止条件")
                        finished.append(plan)
                        continue
                except Exception:
                    pass

            spent = 0
            while plan.cursor < plan.total and spent < budget:
                step = plan.steps[plan.cursor]
                # 先不推进游标——假如这是个"还没完成"的异步步骤，
                # 我们要把它原地留下，游标不能动。
                spent += 1
                try:
                    handler = self._handlers.get(step.op)
                    if handler is None:
                        plan.cursor += 1
                        plan.fail_count += 1
                        plan.skipped.append(f"{step.op} 无处理器")
                        continue
                    if self.on_step is not None:
                        # 告诉外部"现在轮到哪个计划的哪个步骤了"。
                        # 钩子自己抛异常不能影响推进，所以单独兜住。
                        try:
                            self.on_step(plan, step)
                        except Exception:
                            pass
                    failures_before = plan.fail_count
                    ok = bool(handler(step))
                    if ok:
                        plan.cursor += 1
                        # note_failed() returns True to finish an asynchronous
                        # step; that conclusion must not also count as success.
                        if plan.fail_count == failures_before:
                            plan.done_count += 1
                        plan.pending_op = ""
                    elif step.async_op:
                        # 异步步骤返回 False = 还没结论。
                        # 不推进游标，下个 tick 继续问同一个步骤。
                        # 但整个计划的推进要暂停——因为"依次挖掘"的语义
                        # 要求前一个方块挖完了才动下一个。
                        plan.pending_op = step.op
                        break
                    else:
                        plan.cursor += 1
                        plan.fail_count += 1
                        plan.pending_op = ""
                        plan.skipped.append(f"({step.x},{step.y},{step.z}) {step.op} 失败")
                except Exception as exc:
                    plan.cursor += 1
                    plan.fail_count += 1
                    plan.pending_op = ""
                    plan.skipped.append(f"({step.x},{step.y},{step.z}) 异常 {exc}")

            if plan.cursor < plan.total:
                still_running.append(plan)
            else:
                plan.state = PlanState.DONE if plan.fail_count == 0 else PlanState.FAILED
                plan.pending_op = ""
                finished.append(plan)

            budget -= spent
            if budget <= 0:
                # 本 tick 配额用完，剩下的下个 tick 继续
                rest = [p for p in self._plans
                        if p not in finished and p not in still_running]
                self._plans = still_running + rest
                self.completed.extend(finished)
                return finished

        self._plans = still_running
        self.completed.extend(finished)
        return finished
