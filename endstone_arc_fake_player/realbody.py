"""真实假人动作的步骤处理器。

`BatchExecutor` 原本假设"一步 = 一次同步 API 调用"。接入真假人之后，
有几类步骤变成了"发起之后要等物理世界"：

    walk_to     让假人真的走过去（原版寻路，会爬坡绕树）
    dig_far     走过去再挖（够不着的时候）
    dig_near    直接挖（已经在手够得着的范围内）
    place_real  用假人的手放置方块

这里统一用 **"发起 + 等结果"** 两步式处理。

难点在于 `BatchExecutor` 的处理器返回值**只有两个**：

    True  → 这一步成功了
    False → 还没结论，执行器原地下次再来问

而真实动作需要表达**三件事**："还没完"、"成了"、"**试够了，就是做不成**"。
这个差异是本模块所有坑的源头，处理办法是：

  · "还没完" → `return False`（执行器下一 tick 再来问）
  · "成了"   → `return True`
  · "做不成" → 挂 `_gave_up` 标记并 **`return True`**（见 `_give_up`）

第三条看起来别扭（失败却返回 True），但它是**唯一**能让计划正常结算的
写法：False 会被解释成"还在进行中"，计划既不推进也不结算，
`Agent._inflight` 永不释放，`should_think()` 那道闸就此锁死 ——
智能体从此不再思考第二次，而且**一行日志都不打**。
翻译工作由 `plugin._real_or_retry` 完成：它读到 `_gave_up` 标记，
把这次"有结论的失败"记成计划的一次重试，额度用尽就交给执行器结算。

判定"这一步到底成了没有"的依据是：

  · `BodyController` 保留的 `last_dig` / `last_walk`（**权威结论**，
    比查方块状态准，因为它能区分"被别的动作挖掉了"和"我挖的"）
  · 三张互不替代的记账表：`_attempts`（本轮进行中）、`_tries`（累计
    失败次数）、`_started`（曾经开始过）。**别把它们混用** ——
    前两个会被 `_cleanup` 清掉，拿它们当累加器或首判都会静默失效，
    详见各自的注释。

只有"已发出的请求有了明确结论"才算完成。这样丢包、超时、
假人被推开这些情况都不会导致步骤被误判成成功。
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, Callable

from .body import BodyController


class RealBodyActions:
    """给执行器用的真实假人步骤处理器。每个智能体一个实例。"""

    #: 同一个挖掘步骤最多重试多少次。超过就放弃，避免计划永远做不完。
    #:
    #: ⚠️ 曾经是 300：挖不动的方块（基岩/保护区域）每次要等挖掘超时
    #: （约 6.5 秒）+ 退避 1.5 秒，300 次要耗 30+ 分钟，期间控制台一直
    #: 有 bot:dig。30 次（约 3 分钟）足够区分"暂时被挡"和"真挖不动"。
    MAX_ATTEMPTS = 30

    #: 寻路失败的重试上限。比挖掘的小得多：走不到的原因绝大多数是
    #: "目标悬空/不可达"（比如挖高树时树干上段的落脚点在空中），
    #: 这种重试再多也不会成功；少量次数只是留给"临时被实体挡住"。
    #: 8 次 ×（寻路耗时 + 退避 1.5 秒）≈ 半分钟放弃，刷屏也可控。
    WALK_MAX_ATTEMPTS = 8

    #: 已经**明确失败过**的步骤，多久之后才允许再试（秒）。
    #:
    #: 为什么需要退避：执行器每 tick 都会回来问同一个步骤，而
    #: `DIG_TIMEOUT` 是 8 秒——一次挖不动的方块，会在 8 秒内被反复
    #: 发起 160 次左右，每一次都往控制台喷一条 `Script event bot:dig
    #: has been sent`。用户看到的就是日志刷屏。
    #:
    #: 退避只作用于"上次明确拿到了失败结论"的步骤，不碰"还没结论"的
    #: 情况，所以正常挖掘的节奏完全不变。
    RETRY_BACKOFF_SECONDS = 1.5

    def __init__(self, controller: BodyController, world: Any, logger: Any) -> None:
        self._ctl = controller
        self._world = world
        self._logger = logger
        #: 步骤键 → 已经尝试了几次
        self._attempts: dict[tuple, int] = {}
        #: 步骤键 → 该步骤当前的内部阶段（walk / dig / place）
        self._phase: dict[tuple, str] = {}
        #: 步骤键 → 这个步骤**累计**失败了几次。只增不减。
        #:
        #: ⚠️ 为什么不能用 `_attempts` 当累加器：它在每次拿到结论后都会被
        #: `_cleanup` 清掉，于是 `_attempts.get(key, 0) + 1` **永远是 1**。
        #: 那样 `n >= MAX_ATTEMPTS` 永不成立，"试够次数就放弃"这道闸彻底
        #: 失效 —— 一个挖不动的方块会被无限重发下去（日志刷屏永远不停），
        #: 而计划也永远等不到那个"放弃"的终态（智能体永久发呆）。
        #: 实测：连续追问 12 秒，`bot:dig` 始终只有 1 条，卡死在原地。
        self._tries: dict[tuple, int] = {}
        #: 已经**开始过**的步骤键。只增不减，和 `_attempts` 是两回事。
        #:
        #: `_attempts` 会在每次拿到结论后被 `_cleanup` 清掉，所以它答不了
        #: "这个步骤是不是第一次遇到"。多阶段处理器（`dig_far` / `place_real`）
        #: 必须靠这张表来区分"首次发起"和"等结论"，否则会退化成无限重发。
        self._started: set[tuple] = set()
        #: 步骤键 → 上次拿到失败结论的时间。用于退避。
        self._failed_at: dict[tuple, float] = {}
        #: 步骤键 → 是否已经**彻底放弃**。
        #:
        #: ⚠️ 这个表是修一个"计划永远卡死"的 bug 时加的，它解决的是
        #: **"还没完"和"完了但失败"被混为一谈**的问题。
        #:
        #: 处理器的返回值只有两种含义：
        #:     True  → 这一步成功了
        #:     False → 这一步还没结论（执行器会原地下次再问）
        #: 而"我已经试够了，这一步就是做不成"这个结论**没有第三个返回值
        #: 可用**。原先的处理是返回 False——于是执行器永远认为"还在进行中"，
        #: 计划既不推进也不结算，`_inflight` 永不释放，整个智能体从此发呆，
        #: 而且一行日志都不打（看起来就像"在执行完一次任务后不动了"）。
        #:
        #: 现在的约定：真的放弃了就把 key 记进这张表，并且**返回 True**。
        #: 返回 True 表示"这一步有结论了"，执行器推进游标、记一次失败、
        #: 计划照常走向 DONE/FAILED、`_inflight` 被正常回收。
        #: 调用方（`plugin._real_or_retry`）负责把一次失败记账成一次重试，
        #: 次数烧光后就不再重规划，所以不会变成无限循环。
        self._gave_up: dict[tuple, str] = {}
        #: 合成/熔炼产出（{"id": ..., "count": ...}），供上层反馈给模型。
        self._container_made: dict[str, Any] = {}
        # Only the callback for the current request may complete a step. Keeping
        # the request object also rejects late replies after reset/cancellation.
        self._requests: dict[tuple, dict[str, Any]] = {}

    def reset(self) -> None:
        self._attempts.clear()
        self._phase.clear()
        self._started.clear()
        self._tries.clear()
        self._failed_at.clear()
        self._gave_up.clear()
        self._container_made = {}
        self._requests.clear()

    # ------------------------------------------------------------------
    # "放弃了"的两种表达
    # ------------------------------------------------------------------

    def _give_up(self, key: tuple, reason: str) -> bool:
        """宣告这个步骤彻底做不成了。**返回 True** —— 见 `_gave_up` 的注释。

        为什么返回 True 而不是 False：执行器只认 True/False，而 False 被
        解释成"还没结论"。要让计划能够结算、让上层看到"这个方块就是挖不动"，
        唯一的办法就是回报一个明确的终态。
        """
        self._gave_up[key] = reason
        self._cleanup(key)
        self._logger.debug(f"[{self._ctl.name}] {key} 放弃：{reason}")
        return True

    def pop_gave_up(self, key: tuple) -> str:
        """取走"已放弃"标记（有就返回原因，没有就返回空串）。

        ⚠️ 一次性读取。调用方（插件）在把这次失败记进重试计数之后必须
        调它，否则下一轮会重复消耗重试次数（甚至连环触发）。返回 True
        之后紧接着就要收掉这个标记，这是调用约定的一部分。
        """
        reason = self._gave_up.pop(key, "")
        if reason:
            self._finish_step(key)
        return reason

    def key_of(self, step: Any) -> tuple:
        """算出这个步骤在 `_attempts` / `_gave_up` 里的键。

        和各个处理器开头算 key 的规则**必须一致**。调用方要按步骤查
        "它是不是刚被放弃了"，就得用同一个算法，所以把它抽出来当唯一
        真相来源，别在两处各写一遍。
        """
        op = getattr(step, "op", "")
        if op == "attack":
            return ("attack",)
        if op == "consume":
            return ("consume",)
        if op in ("walk_to",):
            return ("walk", step.x, step.y, step.z)
        if op == "dig_near":
            return ("dig", step.x, step.y, step.z)
        if op == "dig_far":
            return ("digfar", step.x, step.y, step.z)
        if op == "place_real":
            return ("place", step.x, step.y, step.z)
        if op == "container":
            # 容器操作的键带 note（JSON），因为同一个容器方块可能做
            # 多次不同的操作，只按坐标会互相串味。
            return ("container", step.x, step.y, step.z, str(step.note))
        return (op, step.x, step.y, step.z)

    def _backing_off(self, key: tuple) -> bool:
        """这个步骤现在要不要重发。

        ⚠️ 返回 True 的**语义**很关键：它的意思是"**不用发，但这一步仍然
        没结论，执行器请下一 tick 再来问**"。所以返回 True 时调用方必须
        交回 `False`（"还没完"），绝不能交回 True —— 那会被当成成功。

        顺便一提，这个函数**故意不清 `_failed_at`**。原先的写法是"退避
        时间一到就把记录 pop 掉，允许重试"，看着挺合理，实际上是个计数
        黑洞：一旦 pop 掉，`_begin` 就会**重新从 1 开始计**，于是
        `MAX_ATTEMPTS` 永远到不了，"试 300 次就放弃"这道闸形同虚设，
        一个走不到的目标会被无限重规划下去（也就是用户看到的"日志刷屏"
        永远不会停）。现在改成只读取、不删除，次数才会真的累加、
        真的能烧光。

        另外，退避和"烧光次数"是**两道不同的闸**，位置也不同：
        退避只是本次不发，不消耗次数；次数是每拿到一次真实失败才 +1。
        两者都挡在"发起新尝试"这一格上，所以顺序永远是
        `_backing_off` → `_begin` → 真正发起。
        """
        if key in self._gave_up:
            return True                    # 已经判过死刑了，别再试
        failed = self._failed_at.get(key)
        if failed is None:
            return False
        return (time.monotonic() - failed) < self.RETRY_BACKOFF_SECONDS

    def _mark_failed(self, key: tuple) -> None:
        self._failed_at[key] = time.monotonic()

    def _bump(self, key: tuple) -> int:
        """给这个步骤的累计失败次数 +1，并返回新值。

        用 `_tries` 而不是 `_attempts`，因为后者会被 `_cleanup` 清空，
        累加出来的永远是个 1（详见 `_tries` 的注释）。
        """
        n = self._tries.get(key, 0) + 1
        self._tries[key] = n
        return n

    def _fail_or_give_up(self, key: tuple, limit: int, why: str) -> bool:
        """记一次失败：还没到上限就返回 False（下次再试），到了就放弃。

        两个返回值对应处理器的两种语义，调用方直接 `return` 即可。

        ⚠️ 退避期内**直接返回、什么都不记**。这一条是必须的，否则会出
        两个叠加的毛病：

          · `_mark_failed` 会把退避时间戳往后推，等于**自己重置自己的
            退避** —— 上次失败刚设的 1.5 秒，这次进来又推 1.5 秒，
            退避永远不起作用
          · 执行器是每 tick 来问一次的，不挡住的话 `_tries` 会以
            **每秒 20 次**的速度往上涨，几次重试的额度在零点几秒内就
            烧光了 —— "试 4 次再放弃"实际变成了"试 0.2 秒就放弃"

        实测：加这道闸之前，4 次额度在 **0.00 秒**内烧完；加了之后，
        每次重试之间稳定隔 `RETRY_BACKOFF_SECONDS`。

        ⚠️ 计数用的是 `_tries`（只增不减），不是 `_attempts`。用后者的话
        每次 `_cleanup` 一清，累加出来的就永远是 1，`limit` 形同虚设 ——
        一个挖不动的方块会被无限重发，"放弃"这个终态永远到不了，
        计划也就永远卡在 `_inflight` 里。
        """
        if self._backing_off(key):
            return False
        n = self._bump(key)
        self._mark_failed(key)
        if n >= limit:
            return self._give_up(key, f"{why}（第 {n} 次）")
        self._logger.debug(f"[{self._ctl.name}] {why}")
        return False

    def _retry_later(self, key: tuple, limit: int, why: str) -> bool:
        """多阶段处理器（`dig_far` / `place_real`）失败时的统一出口。

        为什么单独起个名字、而不直接叫 `_fail_or_give_up`：多阶段处理器的
        失败分支**不在函数开头**，绕过了 `issue()` 那两道闸，所以这里是
        它们唯一能碰到退避闸的地方。名字不同是在提醒后来者：
        "走到这一行说明已经拿到失败结论了，别再加一层 cond"。

        行为上它和 `_fail_or_give_up` 一致（退避期内不记账、超限就放弃）。
        """
        return self._fail_or_give_up(key, limit, why)

    # ------------------------------------------------------------------
    # 对外：一个步骤到底完成了没有
    # ------------------------------------------------------------------

    def step_done(self, key: tuple) -> bool:
        """这个步骤是否已经到了有结论的状态。

        ⚠️ **目前没有任何调用点**，是留给外部查询的探针。留着它的同时
        把判据写对：不能拿 `_attempts` 判（它只表示"本轮进行中"，
        而且会被 `_cleanup` 清掉），得用只增不减的 `_started`。
        """
        ctl = self._ctl
        if ctl.busy:
            return False
        if key in self._gave_up:
            return True                    # 已放弃也是个"有结论"
        return key in self._started

    # ------------------------------------------------------------------
    # 通用：发起 / 收尾
    # ------------------------------------------------------------------

    def issue(self, key: tuple, *, limit: int | None = None) -> bool | None:
        """发起一次尝试的通用前置。**三态返回**，务必看清：

            None  → 闸都过了，请继续往下发起
            False → 这次不发，但**这一步仍然没结论**，执行器下 tick 再来问
            True  → 这一步已经**彻底放弃**，这是个终态，请交回执行器结算

        把两道闸收在一个地方，是为了让每个处理器开头长得一模一样、
        不会有谁漏掉其中一道。三道判定依序是：
          · 已经判过死刑（`_gave_up`）
          · 还在退避期里
          · 尝试次数烧光了 —— 只有这种情况会**挂着"放弃"标记返回 True**

        ⚠️ 这套三态是重试逻辑里最容易写错的地方。踩过的坑就是最后那条：
        次数烧光之后如果仍然返回 False（"还没结论"），执行器会永远等下去，
        计划既不推进也不结算，`_inflight` 永不释放，整个智能体从此发呆，
        而且一行日志都不打 —— 用户看到的就是"执行完一次任务后就不动了"。
        集中在这里比散在六个处理器里安全得多。

        ⚠️ 另一处同样致命的写法错误：**已经判过死刑的步骤必须返回 True**。
        这一条曾经写漏过 —— 当时的写法是"`_backing_off` 为真就返回 False"，
        而 `_backing_off` 把"还在退避"和"已经放弃"两件事**混在一个
        bool 里**。后果是：第一次宣告放弃时确实返回了 True（计划正常
        结算），但**如果执行器再问一次**，它就会退化成"还没结论" ——
        一个永久的进行中状态。

        为什么这很容易被漏掉：正常路径下执行器收到 True 就推进游标，
        不会再问第二次，所以看上去一切正常。但有两类情况会再问：
        `Plan.note_failed()` 记账后计划层若有别的机制回头，或者
        `_real_or_retry` 没能消耗掉放弃标记。一旦发生，计划就**永久卡死**，
        而日志上只能看到一句"某步骤放弃"，之后什么都没有。

        现在的写法是先把 `True` 这个终态还回去，再去看退避。
        """
        cap = self.MAX_ATTEMPTS if limit is None else limit
        if key in self._gave_up:
            # 终态优先。**必须**排在退避判定前面 —— 这两件事的返回值
            # 语义完全相反（True = 有结论了，False = 还没结论），
            # 合在一起判就一定会写错。
            return True
        if self._backing_off(key):
            return False
        if not self._begin(key, cap):
            n = self._attempts.get(key, 0)
            self._cleanup(key)
            return self._give_up(key, f"重试 {n} 次仍未成功（上限 {cap}）")
        return None

    def _begin(self, key: tuple, limit: int | None = None) -> bool:
        """登记一次尝试。返回 False 表示已经超过上限。

        ⚠️ 注意这里是"登记"，不是"判定"。它**不**负责宣告放弃——
        宣告要走 `_give_up`，因为那需要返回一个 True 给执行器。
        """
        cap = self.MAX_ATTEMPTS if limit is None else limit
        n = self._attempts.get(key, 0) + 1
        self._attempts[key] = n
        return n <= cap

    def _cleanup(self, key: tuple) -> None:
        """清掉这个步骤的**进行中**状态，但保留失败记账。

        `_phase` 和 `_attempts` 都是"这一轮"的东西，结论出来就该清。
        但 `_failed_at` 必须留着：它记录的"这一步刚刚失败过"，正是
        下一步重试时要用的退避依据。一起清掉的话，退避就成了摆设。
        """
        self._attempts.pop(key, None)
        self._phase.pop(key, None)

    def _phase_of(self, key: tuple) -> str:
        return self._phase.get(key, "")

    def _set_phase(self, key: tuple, value: str) -> None:
        self._phase[key] = value

    def _finish_step(self, key: tuple) -> bool:
        """Release per-step history so a later action gets its own retry budget."""
        self._cleanup(key)
        self._started.discard(key)
        self._tries.pop(key, None)
        self._failed_at.pop(key, None)
        return True

    # ------------------------------------------------------------------
    # 挖掘
    # ------------------------------------------------------------------

    def dig_near(self, step: Any) -> bool:
        """假人已经站得够近，直接挖。

        ⚠️ 判"第一次"用 `_started`、不用 `_attempts`。理由和 `walk_to`
        的docstring里那段一样：`_attempts` 会被 `_cleanup` 清掉，而重新
        登记它的唯一入口 `issue()` 可能被退避闸挡下（挡下时不登记），
        于是 `key not in self._attempts` 永远成立 —— 每一轮都在"首次
        发起"和"被闸挡下"之间循环，那条失败重试的路永远走不到。

        ⚠️ "记账"和"重发"必须分开，绝不能在同一轮：
           · 拿到失败结论 → 只记账（`_retry_later`，它会设退避）
           · 没有结论 → 只重发（`issue` 判退避，退避期过了才放行）
        旧版把两件事写在同一轮（记账后立刻 `_reissue`），而 `_reissue`
        的 `issue` 会撞上记账刚设的退避 —— 于是"重发"永远轮不到，
        dig 只发最初那 1 条，`tries` 一路涨到上限后直接放弃。
        """
        key = ("dig", step.x, step.y, step.z)
        ctl = self._ctl

        # 第一次遇到：发起
        if key not in self._started:
            pre = self.issue(key)
            if pre is not None:
                return pre
            self._started.add(key)
            ctl.clear_dig_result()      # 别读到上一步留下的旧结论
            if not ctl.start_dig(step.x, step.y, step.z, tool=step.note):
                self._cleanup(key)
                return False
            return False        # 刚发起，还没结论

        if ctl.busy:
            return False        # 还在挖

        # 读结论
        result = ctl.dig_result_for(step.x, step.y, step.z)
        if result is not None:
            ctl.clear_dig_result()      # 消费掉，否则下一轮重演同一条
            self._cleanup(key)
            if result.ok:
                return self._finish_step(key)
            # 明确失败：只记账（设退避），**不重发**。下一轮"无结论"
            # 时会走到下面的重发分支，由 `issue` 在退避期过后放行。
            return self._retry_later(
                key, self.MAX_ATTEMPTS,
                f"挖掘 ({step.x},{step.y},{step.z}) 失败: {result.error}")

        # 没有对应结论（可能结论还没到 / 被别的动作挖掉了）。
        self._cleanup(key)
        if not self._world.is_solid_id(step.x, step.y, step.z):
            return self._finish_step(key)     # 方块没了，算完成

        # 方块还在、又没有新结论 → 重发。这里**不记账**：没有结论就不算
        # 失败，不能烧重试额度；`issue` 会在退避期内返回 False（等），
        # 退避期过后才真正发出下一次挖掘。
        pre = self.issue(key)
        if pre is not None:
            return pre
        ctl.clear_dig_result()
        if not ctl.start_dig(step.x, step.y, step.z, tool=step.note):
            self._cleanup(key)
            return False
        return False        # 刚发出去，等下一轮读结论

    def dig_far(self, step: Any) -> bool:
        """先走过去，到了再挖。

        这是"不隔墙挖方块"的核心：假人必须真的移动到方块旁边才会动手。
        从计划的角度看这仍是一个原子步骤——执行器不关心里面分了几段。

        ⚠️ 这里用 `_started` 而不是 `_attempts` 来判"是不是第一次"。
        这个区别是整个函数能走完的关键：

            `_attempts` 是"**正在进行**的一次尝试"，结论一出来就被
            `_cleanup` 清掉。拿它当"发起过没有"的判据，会在拿到结论的
            下一轮又变回"第一次"——于是重新 `clear_dig_result()`，
            把**刚刚拿到的结论扔掉**，再发一次请求…… 结论永远读不到，
            方块永远挖不掉，计划永远停在这一步。
            实测：连续追问 6 秒，`bot:dig` 只有最初那 1 条。

            `_started` 是"这个坐标**曾经**开始过"，只增不减，所以
            "第一次"只可能是真的第一次。
        """
        key = ("digfar", step.x, step.y, step.z)
        ctl = self._ctl

        if key not in self._started:
            pre = self.issue(key)
            if pre is not None:
                return pre
            self._started.add(key)
            if self._within_reach(step.x, step.y, step.z):
                self._set_phase(key, "dig")
                ctl.clear_dig_result()
                if not ctl.start_dig(step.x, step.y, step.z, tool=step.note):
                    self._cleanup(key)
                    return False
            else:
                self._set_phase(key, "walk")
                tx, ty, tz = self._approach_point(step.x, step.y, step.z)
                if not ctl.start_walk(tx, ty, tz):
                    self._cleanup(key)
                    return False
            return False

        if ctl.busy:
            # 走的半路上如果已经够得着目标方块了，就不必等寻路走完——
            # 立刻停下转挖。挖高树时尤其明显：approach 点可能算在树干
            # 高处，但假人走到半途就已经够得着低处的原木了。
            # `ctl.cancel` 会发 bot:stop（JS 侧 stopMoving + 清寻路轮询），
            # 留下的 walk 失败结论用 clear_walk_result 清掉。
            if self._phase_of(key) == "walk" and self._within_reach(step.x, step.y, step.z):
                ctl.cancel("半路已经够得着，提前开挖")
                ctl.clear_walk_result()
                self._set_phase(key, "dig")
                ctl.clear_dig_result()
                if not ctl.start_dig(step.x, step.y, step.z, tool=step.note):
                    self._cleanup(key)
                    return False
                return False
            return False

        phase = self._phase_of(key)

        if phase == "walk":
            # 走完了吗？用**真实坐标**判断，不是"指令发出去了就算到"。
            if self._within_reach(step.x, step.y, step.z):
                self._set_phase(key, "dig")
                ctl.clear_dig_result()
                if not ctl.start_dig(step.x, step.y, step.z, tool=step.note):
                    self._cleanup(key)
                    return False
                return False
            # 还没走到。看有没有**新的**寻路结论。
            last = ctl.last_walk
            if last is not None:
                ctl.clear_walk_result()      # 消费掉，否则下一轮重演同一条
                if not last.ok:
                    # 寻路失败：只记账（设退避），**不重发**。下一轮
                    # "无结论"时会走到下面的重发，由 `issue` 放行。
                    # ⚠️ 这里不能 `_cleanup(key)` —— 那会清掉 phase，
                    # 下一轮落到"重新发起挖掘"分支，假人原地对着远处
                    # 方块空挖（正是用户看到的"挖方块不走动"）。
                    return self._retry_later(
                        key, self.WALK_MAX_ATTEMPTS,
                        f"靠近 ({step.x},{step.y},{step.z}) 失败: {last.error or '未知'}")
                # last.ok=true 但没够着 → 走完没够着，落到下面的重发。

            # 没有新结论（已消费），或"走完了却没够着"：重发一次寻路。
            # 保持 phase=walk；`issue` 会在退避期内返回 False（等）。
            pre = self.issue(key)
            if pre is not None:
                return pre
            tx, ty, tz = self._approach_point(step.x, step.y, step.z)
            if not ctl.start_walk(tx, ty, tz):
                self._cleanup(key)
                return False
            return False

        # 走到这里说明既不是第一次，也不是"正要靠近"——那就是"在挖，
        # 或者刚挖完还没读结论"。两件事都得做，而且**顺序不能反**：
        # 先读结论，再决定要不要重新发起。
        #
        # ⚠️⚠️ 这一段是整份重试逻辑里最阴的一个坑，两个毛病叠在一起
        # 才显形，单独看任何一个都不像 bug：
        #
        #   1）`ctl.dig_result_for()` 是**只读**的，不是"取走"。它每次
        #      都返回 `last_dig` 这条记录，`last_dig` 又一直躺在那儿。
        #      所以"读到失败结论 → 记一次失败 → return"这个写法，在
        #      **下一轮会原封不动地重演一遍** —— 同一句 `result.error`
        #      被当成新失败记了 N 次。
        #
        #   2）更要命的是那个 `return`：它让下面的"重新发起"**永远轮
        #      不到**。函数看着是对称的（"上面负责发，下面负责收"），
        #      实际是条断头路。实测：`bot:dig` 永远停在 1 条，`tries`
        #      一路涨到上限，然后宣告放弃 —— 一个本来完全能挖掉的方块
        #      就这么被判了死刑，计划还卡在 `_inflight` 里出不来。
        #
        # 所以这里的写法是：**读完就消费掉**（`clear_dig_result()`），
        # 让"这条结论"和"这次尝试"严格一对一；只有真的拿到结论的那一
        # 轮才记账，其余轮次一律落到下面的"重新发起"。
        result = ctl.dig_result_for(step.x, step.y, step.z)
        if result is not None:
            self._cleanup(key)
            ctl.clear_dig_result()      # ← 消费掉，否则下一轮重演
            if result.ok:
                return self._finish_step(key)
            # 拿到明确失败：记账（含退避）。这一轮到此为止，下一轮
            # 走到下面的"重新发起"去重试。
            return self._retry_later(
                key, self.MAX_ATTEMPTS,
                f"挖掘 ({step.x},{step.y},{step.z}) 失败: {result.error}")

        if not self._world.is_solid_id(step.x, step.y, step.z):
            return self._finish_step(key)

        # 没有结论、方块也还在 → 上一轮发起的尝试已经结算完了（或者
        # 结论早被消费掉），该重新发一次。重新发起要重新过退避闸和
        # 次数闸，所以先 `_cleanup` 把"进行中"清干净。
        #
        # ⚠️ `_cleanup` 不能顺手清掉 `_failed_at` —— 那正是退避闸的
        # 依据，也是"tries 能累加"的前提（见 `_cleanup` 的注释）。
        self._cleanup(key)
        pre = self.issue(key)
        if pre is not None:
            return pre
        if not ctl.start_dig(step.x, step.y, step.z, tool=step.note):
            # 没发出去（正忙 / 去重闸拦下）。这不是结论，清掉这次
            # 尝试下轮再来；**不要**记失败，否则去重闸挡一次就多
            # 烧一次重试额度。
            self._cleanup(key)
            return False
        return False        # 刚发出去，等下一轮读结论

    # ------------------------------------------------------------------
    # 移动
    # ------------------------------------------------------------------

    def walk_to(self, step: Any) -> bool:
        """走到某处。中途失败会重试，重试烧光后**明确交出失败**。

        ⚠️ 两个最容易写错的点，都在本函数的注释里踩过：

        1. **判"第一次"必须用 `_started`，不能用 `_attempts`**——后者会被
           `_cleanup` 清掉，而重登记它的唯一入口 `issue()` 又可能被退避闸
           挡下（挡下时不登记），于是"第一次"判据永远成立，每一轮都在
           "首次发起"和"被闸挡下"之间空转。实测 1200 tick 追问，
           `bot:walk` 只有 1 条，一等等到天荒地老。

        2. **"记账"和"重发"必须分轮**——拿到失败结论的那一轮只记账
           （`_fail_or_give_up` 会设退避），**不重发**；下一轮读不到新结论
           时，由 `issue()` 在退避期过后放行重发。旧版把两件事写在同一轮
           （记账后立刻重发），重发撞上刚设的退避，`bot:walk` 永远只有
           最开始那 1 条、`tries` 却一路涨到上限后放弃。
        """
        key = ("walk", step.x, step.y, step.z)
        ctl = self._ctl

        if key not in self._started:
            pre = self.issue(key, limit=self.WALK_MAX_ATTEMPTS)
            if pre is not None:
                return pre
            self._started.add(key)
            if not ctl.start_walk(step.x, step.y, step.z):
                # 没发出去（正在忙/桥接断了）。这不是"结论"，清掉这次
                # 尝试，下一 tick 再来——但不能记失败，否则会把退避闸
                # 打开，反而拖慢正常的重试。
                self._cleanup(key)
                return False
            return False

        if ctl.busy:
            return False                       # 还在走，没结论

        # 用真实坐标判断到没到，而不是"指令发出去了就算到了"。
        pos = self._actor_position()
        if pos is not None:
            d = math.dist(pos, (step.x + 0.5, step.y, step.z + 0.5))
            if d <= 2.5:
                return self._finish_step(key)

        # 没走到（或读不到坐标）。看有没有**新的**寻路结论。
        last = ctl.last_walk
        if last is not None:
            ctl.clear_walk_result()      # 消费掉，否则下一轮重演同一条
            self._cleanup(key)
            if pos is None and last.ok:
                return self._finish_step(key)  # 读不到坐标，只能信寻路结论
            if not last.ok:
                # 寻路失败：只记账（设退避），下一轮"无结论"时重发。
                return self._fail_or_give_up(
                    key, self.WALK_MAX_ATTEMPTS,
                    f"移动失败: {last.error or '未知'}")
            # last.ok=true 但没走到（坐标有值且还差得远）→ 走完没够着，
            # 落到下面的重发。

        # 没有新结论（已消费），或"走完了却没够着"：重发。这里**不记账**
        # （没有失败结论就不烧额度）；`issue` 会在退避期内返回 False（等），
        # 退避期过了才真正发出下一次寻路。
        pre = self.issue(key, limit=self.WALK_MAX_ATTEMPTS)
        if pre is not None:
            return pre
        if not ctl.start_walk(step.x, step.y, step.z):
            self._cleanup(key)
            return False
        return False

    # ------------------------------------------------------------------
    # 放置
    # ------------------------------------------------------------------

    #: 放置最多试几次。放置是瞬时的，不像挖掘要等好几秒，
    #: 所以不需要 `MAX_ATTEMPTS` 那么多次。
    PLACE_MAX_ATTEMPTS = 3
    #: 缺食物等明确失败的进食请求最多重试两次。
    CONSUME_MAX_ATTEMPTS = 2

    def _request_step(self, key: tuple, limit: int,
                      send: Callable[[Callable], int]) -> bool:
        """Send once, wait for the matching receipt, and retry only failures."""
        if key in self._gave_up:
            return True
        request = self._requests.get(key)
        if request is not None:
            result = request.get("result")
            if result is None:
                return False
            self._requests.pop(key, None)
            self._cleanup(key)
            ok, err, info = result
            if ok:
                if key[0] == "container":
                    made = info.get("made")
                    self._container_made = made if isinstance(made, dict) else {}
                return self._finish_step(key)
            return self._fail_or_give_up(key, limit, err or "动作执行失败")

        if self._ctl.busy:
            return False
        pre = self.issue(key, limit=limit)
        if pre is not None:
            return pre
        self._started.add(key)
        request = {"result": None}
        self._requests[key] = request

        def complete(ok: bool, err: str, info: dict) -> None:
            if self._requests.get(key) is request:
                request["result"] = (bool(ok), str(err or ""), info or {})

        try:
            rid = send(complete)
        except Exception as exc:
            rid = -1
            error = f"指令发送失败: {exc}"
        else:
            error = "指令发送失败"
        if rid < 0:
            self._requests.pop(key, None)
            self._cleanup(key)
            return self._fail_or_give_up(key, limit, error)
        return False

    def place_real(self, step: Any) -> bool:
        """Approach once, then wait for JS to verify the requested block."""
        key = ("place", step.x, step.y, step.z)
        ctl = self._ctl
        if key in self._gave_up:
            return True
        if key in self._requests:
            return self._request_step(
                key, self.PLACE_MAX_ATTEMPTS,
                lambda callback: self._send_place(step, callback))
        if ctl.busy:
            return False
        if not self._within_reach(step.x, step.y, step.z):
            last = ctl.last_walk if self._phase_of(key) == "walk" else None
            if last is not None:
                ctl.clear_walk_result()
                self._cleanup(key)
                if not last.ok:
                    return self._retry_later(
                        key, self.WALK_MAX_ATTEMPTS,
                        f"走近 ({step.x},{step.y},{step.z}) 失败: {last.error}")
            pre = self.issue(key, limit=self.WALK_MAX_ATTEMPTS)
            if pre is not None:
                return pre
            self._started.add(key)
            self._set_phase(key, "walk")
            ctl.clear_walk_result()
            if not ctl.start_walk(*self._approach_point(step.x, step.y, step.z)):
                self._cleanup(key)
            return False
        if self._phase_of(key) == "walk":
            ctl.clear_walk_result()
            self._cleanup(key)
        return self._request_step(
            key, self.PLACE_MAX_ATTEMPTS,
            lambda callback: self._send_place(step, callback))

    def _send_place(self, step: Any, callback: Callable) -> int:
        return self._ctl._bridge.place(
            self._ctl.name, step.x, step.y, step.z,
            step.block or "minecraft:cobblestone", callback=callback)

    TILL_MAX_ATTEMPTS = 3

    def till_real(self, step: Any) -> bool:
        """走近并用锄头耕地（等 JS 验证 farmland 出现）。"""
        key = ("till", step.x, step.y, step.z)
        if key in self._gave_up:
            return True
        ctl = self._ctl
        if key in self._requests:
            return self._request_step(
                key, self.TILL_MAX_ATTEMPTS,
                lambda callback: self._ctl._bridge.till(
                    self._ctl.name, step.x, step.y, step.z, callback=callback))
        if ctl.busy:
            return False
        if not self._within_reach(step.x, step.y, step.z):
            last = ctl.last_walk if self._phase_of(key) == "walk" else None
            if last is not None:
                ctl.clear_walk_result()
                self._cleanup(key)
                if not last.ok:
                    return self._retry_later(
                        key, self.WALK_MAX_ATTEMPTS,
                        f"走近 ({step.x},{step.y},{step.z}) 失败: {last.error}")
            pre = self.issue(key, limit=self.WALK_MAX_ATTEMPTS)
            if pre is not None:
                return pre
            self._started.add(key)
            self._set_phase(key, "walk")
            ctl.clear_walk_result()
            if not ctl.start_walk(*self._approach_point(step.x, step.y, step.z)):
                self._cleanup(key)
            return False
        if self._phase_of(key) == "walk":
            ctl.clear_walk_result()
            self._cleanup(key)
        return self._request_step(
            key, self.TILL_MAX_ATTEMPTS,
            lambda callback: self._ctl._bridge.till(
                self._ctl.name, step.x, step.y, step.z, callback=callback))

    PLANT_MAX_ATTEMPTS = 3

    def plant_real(self, step: Any) -> bool:
        """走近并播种（等 JS 验证麦苗出现）。note 是种子物品名。"""
        item = str(getattr(step, "note", "") or "wheat_seeds")
        key = ("plant", step.x, step.y, step.z, item)
        if key in self._gave_up:
            return True
        ctl = self._ctl
        if key in self._requests:
            return self._request_step(
                key, self.PLANT_MAX_ATTEMPTS,
                lambda callback: self._ctl._bridge.plant(
                    self._ctl.name, step.x, step.y, step.z, item,
                    callback=callback))
        if ctl.busy:
            return False
        if not self._within_reach(step.x, step.y, step.z):
            last = ctl.last_walk if self._phase_of(key) == "walk" else None
            if last is not None:
                ctl.clear_walk_result()
                self._cleanup(key)
                if not last.ok:
                    return self._retry_later(
                        key, self.WALK_MAX_ATTEMPTS,
                        f"走近 ({step.x},{step.y},{step.z}) 失败: {last.error}")
            pre = self.issue(key, limit=self.WALK_MAX_ATTEMPTS)
            if pre is not None:
                return pre
            self._started.add(key)
            self._set_phase(key, "walk")
            ctl.clear_walk_result()
            if not ctl.start_walk(*self._approach_point(step.x, step.y, step.z)):
                self._cleanup(key)
            return False
        if self._phase_of(key) == "walk":
            ctl.clear_walk_result()
            self._cleanup(key)
        return self._request_step(
            key, self.PLANT_MAX_ATTEMPTS,
            lambda callback: self._ctl._bridge.plant(
                self._ctl.name, step.x, step.y, step.z, item,
                callback=callback))

    def consume_real(self, step: Any) -> bool:
        """Wait for actual item consumption, including eating at full health."""
        return self._request_step(
            ("consume",), self.CONSUME_MAX_ATTEMPTS,
            lambda callback: self._ctl._bridge.consume(
                self._ctl.name, getattr(step, "note", "") or "",
                callback=callback))

    ATTACK_MAX_ATTEMPTS = 5

    def attack_real(self, step: Any) -> bool:
        """A queued attack is pending until its own result arrives."""
        return self._request_step(
            ("attack",), self.ATTACK_MAX_ATTEMPTS,
            lambda callback: self._ctl._bridge.attack(
                self._ctl.name, getattr(step, "note", ""), callback=callback))

    CONTAINER_MAX_ATTEMPTS = 2

    def container_op(self, step: Any) -> bool:
        """Run one inventory operation and consume its receipt exactly once."""
        key = ("container", step.x, step.y, step.z, str(step.note))
        try:
            spec = json.loads(str(step.note or "{}"))
            if not isinstance(spec, dict):
                raise ValueError("容器操作必须是对象")
        except (ValueError, TypeError) as exc:
            return self._give_up(key, f"容器参数无效: {exc}")
        return self._request_step(
            key, self.CONTAINER_MAX_ATTEMPTS,
            lambda callback: self._ctl._bridge.container(
                self._ctl.name, step.x, step.y, step.z, spec, callback=callback))

    #: 丢物品最多试几次。和容器不同，"背包里没有"是必然连败的
    #: 明确失败（JS 侧原话带回），两次足够让模型死心去凑货。
    DROP_MAX_ATTEMPTS = 2

    def drop_item(self, step: Any) -> bool:
        """把背包物品丢给玩家（交易交付），等 JS 回执。"""
        key = ("drop", str(step.note))
        if key in self._gave_up:
            return True
        try:
            spec = json.loads(str(step.note or "{}"))
            if not isinstance(spec, dict):
                raise ValueError("交付参数必须是对象")
            item = str(spec.get("item", "")).strip()
            if not item:
                raise ValueError("缺少 item")
        except (ValueError, TypeError) as exc:
            return self._give_up(key, f"交付参数无效: {exc}")
        count = max(1, int(spec.get("count", 1) or 1))
        to = str(spec.get("to", "") or "")
        return self._request_step(
            key, self.DROP_MAX_ATTEMPTS,
            lambda callback: self._ctl._bridge.drop(
                self._ctl.name, item, count, to=to, callback=callback))

    def take_container_made(self) -> dict[str, Any]:
        """取走最近一次合成/熔炼的产出，供上层反馈给模型（一次性）。"""
        made = self._container_made
        self._container_made = {}
        return made

    # ------------------------------------------------------------------
    # 位置辅助
    # ------------------------------------------------------------------

    def _actor_position(self) -> tuple[float, float, float] | None:
        """假人的真实位置。"""
        actor = self._ctl.actor
        if actor is None:
            return None
        try:
            loc = actor.location
            return (loc.x, loc.y, loc.z)
        except Exception:
            return None

    def _within_reach(self, x: int, y: int, z: int) -> bool:
        """现在够不够得着。"""
        pos = self._actor_position()
        if pos is None:
            return False
        from .actions import ActionCompiler
        d = math.dist(pos, (x + 0.5, y + 0.5, z + 0.5))
        return d <= float(ActionCompiler.DIG_REACH)

    def _approach_point(self, x: int, y: int, z: int) -> tuple[int, int, int]:
        """给目标方块找一个能站人的相邻格。

        不能直接往方块本身走——那是要挖的固体。在它周围找一格
        脚下有东西、身体两格空的位置。

        ⚠️ "脚下有东西"这条不能省。曾经只查"这一格和上一格是空气"，
        结果挖高树时选中了**悬空格**（树干旁边的空中）——假人站在
        空气里是到不了那种格子的，`navigateToLocation` 对它规划不出
        完整路径，于是每条寻路都失败，假人原地不动、bot:walk 刷屏。
        """
        cands: list[tuple[int, int, int]] = []
        # 方向集合：同层侧向 + 低一层侧向 + 方块正上方。
        # ⚠️ 低一层侧向不能少：挖树干第二段（y+1）时，同层侧向格的脚下
        # 是空气（假人脚层），只有低一格的侧向格才踩在地面上。
        for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1),
                           (1, -1, 0), (-1, -1, 0), (0, -1, 1), (0, -1, -1),
                           (0, 1, 0)):
            nx, ny, nz = x + dx, y + dy, z + dz
            # 站人的条件是：这一格和上面一格都不是实体
            if self._world.is_solid_id(nx, ny, nz):
                continue
            if self._world.is_solid_id(nx, ny + 1, nz):
                continue
            # 且脚下有支撑（站空气里寻路到不了）。
            if not self._world.is_solid_id(nx, ny - 1, nz):
                continue
            cands.append((nx, ny, nz))

        if not cands:
            # 四面都堵着（比如正在挖实心岩层）——那就挖完之后再站过去，
            # 这里先返回方块上方，让寻路自己判断
            return (x, y + 1, z)

        pos = self._actor_position()
        if pos is None:
            return cands[0]
        return min(cands, key=lambda c: math.dist(pos, (c[0] + 0.5, c[1], c[2] + 0.5)))
