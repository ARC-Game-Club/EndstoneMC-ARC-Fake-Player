"""假人体操盘：把"真挖掘 + 真移动"接入批量执行器。

这里解决一个核心矛盾
--------------------
``BatchExecutor`` 的设计前提是"步骤是同步的"——每 tick 推进 6 步，
每步立刻返回成功或失败。但真实挖掘不可能同步：

    bridge.dig() 发出去 → 假人开始挥臂 → 按方块硬度敲几秒 → 方块才破

如果在发出挖掘请求时就回报"成功"，执行器会立刻推进下一步。挖隧道的
时候，假人就会往还没挖通的墙里走；盖房子的时候，会在还没清空的格子里放方块。

所以这里用一层 **串行队列** 把异步挖解放回来：

    执行器 tick 一次 → 队列里只取一个方块 → 发 dig → 挂起
    回调到达 → 标记这个方块完成 → 下一个 tick 才能取下一个

效果就是"一次挖一个、挖完再挖下一个"——既保住了真挖掘的物理真实感，
又让批量计划的语义（顺序推进）不变。

移动同理：``walk`` 交给原版寻路后需要等待，期间不能下达新动作。
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable


#: 一次挖掘最多等多久（秒）。超时就跳过这个方块，不让假人卡死在墙前。
#:
#: ⚠️ 空手挖原木约 15 秒、石头约 7.5 秒。之前 8 秒太短，稍硬的方块
#: 会被误判"挖掘超时"反复重试。这里只做**兜底**（JS 侧挖掘轮询挂了、
#: 一直不回结果时）才触发。
#:
#: JS 侧的契约（`behavior_pack/scripts/main.js`）：
#:   • 每 tick 对**准星真正打到的**那一格补发一次 `breakBlock`，连续挖到破坏；
#:   • 打不到目标（被遮挡 / 没瞄准 / 超距）会**很快失败**并回报原因，
#:     通常 20 tick（1 秒）内就有结论 —— 这类失败根本走不到这里的超时；
#:   • 唯一的硬上限是 `DIG_LIMIT_TICKS`=900 tick（45 秒）。
#: 所以正常路径最长 45 秒；150 秒是给"JS 轮询整个挂了"留的余量，
#: 必须**大于** 45 秒。改 JS 那个常量时，这个数要跟着核，
#: 否则会出现"假人还在挖、Python 已经判超时"的误杀。
DIG_TIMEOUT = 150.0
#: 一次寻路最多等多久（秒）
WALK_TIMEOUT = 12.0


@dataclass
class DigTask:
    """一个等待完成的挖掘任务。"""

    x: int
    y: int
    z: int
    tool: str = ""
    started_at: float = 0.0
    done: bool = False
    ok: bool = False
    error: str = ""
    swing: int = 0


@dataclass
class WalkTask:
    """一个等待完成的移动任务。"""

    x: int
    y: int
    z: int
    started_at: float = 0.0
    done: bool = False
    ok: bool = False
    error: str = ""


class BodyController:
    """管理一具假人的真实动作队列。

    每个智能体对应一个实例。所有需要"等待物理世界响应"的动作都经过这里，
    以保证同一时刻假人只做一件事。
    """

    #: 对同一个方块，两次挖掘请求之间至少隔这么多秒。
    #:
    #: 为什么需要这道闸：``bridge.dig()`` 每调一次就发一条
    #: ``scriptevent bot:dig``，而 BDS 会为每条 scriptevent 打一行
    #: ``Script event bot:dig has been sent``。挖掘是异步的，在"发出去了"
    #: 和"有结论了"之间，执行器每 tick 都会重新问一次同一个步骤；
    #: 任何一条重发路径没被挡住，控制台就会被同一行日志刷满。
    #:
    #: 这道闸做的是"同一坐标 0.5 秒内只发一次"，属于兜底性质的去重，
    #: 不改变**语义**（该挖的还是会挖），只是不让重复请求涌出去。
    DIG_DEDUP_SECONDS = 0.5

    def __init__(
        self,
        name: str,
        bridge: Any,
        world: Any,
        logger: Any,
        *,
        dig_timeout: float = DIG_TIMEOUT,
        walk_timeout: float = WALK_TIMEOUT,
    ) -> None:
        self.name = name
        self._bridge = bridge
        self._world = world
        self._logger = logger
        self._dig_timeout = dig_timeout
        self._walk_timeout = walk_timeout
        #: 由插件在生成假人后注入的真实实体
        self._actor: Any = None

        #: 当前正在进行的动作（同一时刻最多一个）
        self._dig: DigTask | None = None
        self._walk: WalkTask | None = None
        #: 最近一次完成的动作。判定"这一步完没完"必须靠它——
        #: 只看 busy 是分不清"刚发起"和"已完成"的。
        self.last_dig: DigTask | None = None
        self.last_walk: WalkTask | None = None

        #: 上一个被发出去的挖掘请求：(坐标, 时间)。用于去重。
        self._last_dig_sent: tuple[tuple[int, int, int], float] | None = None
        #: 被这道闸挡下来的重复请求数。暴露出来是为了能在测试里断言
        #: "确实有重复被挡住"，而不是只能靠肉眼看日志。
        self.digs_deduped = 0
        #: 从桥接过来的 scriptevent 发送次数（每次 dig 一条）。测试用它
        #: 直接数"控制台会被刷多少行"。
        self.dig_commands_sent = 0

        #: 统计：让 /ai list 能看出"身体层"到底跑了多少活
        self.digs_done = 0
        self.digs_failed = 0
        self.walks_done = 0
        self.walks_failed = 0
        self.last_error = ""

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._dig is not None or self._walk is not None

    @property
    def actor(self) -> Any:
        """假人对应的实体对象。插件在生成后注入。"""
        return self._actor

    @actor.setter
    def actor(self, value: Any) -> None:
        self._actor = value

    def dig_result_for(self, x: int, y: int, z: int) -> DigTask | None:
        """如果最近完成的挖掘就是这个坐标，返回它的结果，否则 None。

        执行器用它判断"我发起的那个挖掘到底成了没有"——
        比查方块状态更准，因为它能区分"被别的动作挖掉了"和"我挖的"。
        """
        task = self.last_dig
        if task is None:
            return None
        if (task.x, task.y, task.z) != (int(x), int(y), int(z)):
            return None
        return task

    def clear_dig_result(self) -> None:
        self.last_dig = None

    def clear_walk_result(self) -> None:
        """把"上一次寻路的结论"清掉。

        ⚠️ 和 `clear_dig_result` 是同一件事的另一半：`last_walk` 是**只读**
        的，处理器读完如果不清，下一轮还会读到同一条"寻路失败"，于是把
        同一次失败重复记账，而真正的"重新发起"永远轮不到（见
        `RealBodyActions.walk_to` / `dig_far` 的注释）。
        """
        self.last_walk = None

    @property
    def current(self) -> str:
        if self._dig is not None:
            return f"挖掘({self._dig.x},{self._dig.y},{self._dig.z})"
        if self._walk is not None:
            return f"移动({self._walk.x},{self._walk.y},{self._walk.z})"
        return ""

    def stats(self) -> dict[str, int]:
        return {
            "digs_done": self.digs_done,
            "digs_failed": self.digs_failed,
            "walks_done": self.walks_done,
            "walks_failed": self.walks_failed,
        }

    def cancel(self, reason: str = "已取消") -> None:
        """放弃当前动作（切换计划、假人被移除、连接断开时调用）。

        关键：一定要留下 `last_dig` / `last_walk` 结论。执行器等在异步
        步骤上时，唯一的出路就是读到这个结论——没有它就会一直等下去。
        """
        if self._dig is not None:
            task = self._dig
            self._dig = None
            task.done = True
            task.ok = False
            task.error = reason
            self.last_dig = task
        if self._walk is not None:
            task = self._walk
            self._walk = None
            task.done = True
            task.ok = False
            task.error = reason
            self.last_walk = task
        try:
            self._bridge.stop(self.name)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 挖掘
    # ------------------------------------------------------------------

    def _dig_send_allowed(self, x: int, y: int, z: int) -> bool:
        """这个坐标的挖掘请求现在允许发出去吗。

        允许的条件：上次发的是**别的**坐标，或者同一坐标已经过了
        `DIG_DEDUP_SECONDS`。注意这里只是在"发请求"这一层去重，
        并不假装挖掘成功了——真正的结论仍然等 `bot:dug` 回来。
        """
        key = (int(x), int(y), int(z))
        now = time.monotonic()
        last = self._last_dig_sent
        if last is not None and last[0] == key and (now - last[1]) < self.DIG_DEDUP_SECONDS:
            self.digs_deduped += 1
            return False
        self._last_dig_sent = (key, now)
        return True

    def start_dig(self, x: int, y: int, z: int, tool: str = "") -> bool:
        """发起一次挖掘。返回 False 表示当前正忙或有别的问题。"""
        if self.busy:
            return False

        # 去重闸：同一坐标短时间内重复发起的请求直接吞掉。
        # 返回 False 的语义和"正忙"一致，调用方本来就要处理这种情况，
        # 所以这一步不会引入新的调用约定。
        if not self._dig_send_allowed(x, y, z):
            return False

        task = DigTask(x=int(x), y=int(y), z=int(z), tool=tool,
                       started_at=time.monotonic())
        self._dig = task

        req_id = self._bridge.dig(
            self.name, task.x, task.y, task.z, tool=tool,
            callback=lambda ok, err, info: self._on_dig_done(task, ok, err, info),
        )
        if req_id < 0:
            self._dig = None
            task.done = True
            task.ok = False
            task.error = "指令发送失败"
            self.last_error = task.error
            return False
        # 只有真正发出去的命令才会让 BDS 打日志，所以这里才计数。
        self.dig_commands_sent += 1
        return True

    def _on_dig_done(self, task: DigTask, ok: bool, err: str, info: dict) -> None:
        """挖掘回调。注意：这里跑在服务器主线程（事件处理），不能阻塞。"""
        if task is not self._dig:
            # 已经被 cancel 掉的旧任务，忽略
            return
        task.done = True
        task.ok = ok
        task.error = err
        try:
            task.swing = int(info.get("swing", 0) or 0)
        except Exception:
            task.swing = 0
        if ok:
            self.digs_done += 1
        else:
            self.digs_failed += 1
            self.last_error = f"挖掘({task.x},{task.y},{task.z}) 失败: {err}"
        self.last_dig = task      # 保留结果供执行器判定
        self._dig = None

    # ------------------------------------------------------------------
    # 移动
    # ------------------------------------------------------------------

    def start_walk(self, x: int, y: int, z: int,
                   timeout_ticks: int = 0) -> bool:
        """让假人真的走过去。

        timeout_ticks 是 JS 侧寻路轮询的超时；**0 = 按距离自适应**：
        每格给 12 tick（约 0.6 秒，含绕路余量），夹在 [200, 1200]
        （10~60 秒）。此前一律 200 tick（10 秒 ≈ 40 格），再远的目标
        必然"超时→重试→放弃"——表现就是"走不远"。
        """
        if self.busy:
            return False

        if timeout_ticks <= 0:
            try:
                loc = getattr(self.actor, "location", None)
                dist = ((loc.x - x) ** 2 + (loc.y - y) ** 2
                        + (loc.z - z) ** 2) ** 0.5 if loc else None
            except Exception:
                dist = None
            timeout_ticks = self.auto_walk_timeout(x, y, z, dist=dist)

        task = WalkTask(x=int(x), y=int(y), z=int(z), started_at=time.monotonic())
        self._walk = task

        req_id = self._bridge.walk(
            self.name, task.x, task.y, task.z, timeout=timeout_ticks,
            callback=lambda ok, err, info: self._on_walk_done(task, ok, err),
        )
        if req_id < 0:
            self._walk = None
            task.error = "指令发送失败"
            self.last_error = task.error
            return False
        return True

    @staticmethod
    def auto_walk_timeout(x: float, y: float, z: float, *,
                          dist: float | None = None) -> int:
        """按目标距离估算寻路超时（tick）：每格 12 tick，夹 [200, 1200]。"""
        if dist is None:
            dist = abs(x) + abs(y) + abs(z)   # 兜底：没有站位时按坐标幅度估
        return int(max(200, min(1200, 12 * max(1.0, dist))))

    def _on_walk_done(self, task: WalkTask, ok: bool, err: str) -> None:
        if task is not self._walk:
            return
        task.done = True
        task.ok = ok
        task.error = err
        if ok:
            self.walks_done += 1
        else:
            self.walks_failed += 1
            self.last_error = f"移动失败: {err}"
        self.last_walk = task
        self._walk = None

    # ------------------------------------------------------------------
    # 心跳
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """每 tick 调用。只做超时检查。

        为什么要超时：行为包那边可能因为区块卸载、假人被推开、方块被保护
        而永远挖不掉。没有超时的话，"等挖掘完成"这条状态会永久挂住，
        整个智能体就死住了。
        """
        now = time.monotonic()

        if self._dig is not None and now - self._dig.started_at > self._dig_timeout:
            task = self._dig
            self._dig = None
            task.done = True
            task.ok = False
            task.error = "超时"
            self.digs_failed += 1
            self.last_error = f"挖掘({task.x},{task.y},{task.z}) 超时"
            self.last_dig = task       # 让执行器能拿到"失败了"这个结论

        if self._walk is not None and now - self._walk.started_at > self._walk_timeout:
            task = self._walk
            self._walk = None
            task.done = True
            task.ok = False
            task.error = "超时"
            self.walks_failed += 1
            self.last_error = "移动超时"
            self.last_walk = task


class BodyRegistry:
    """所有假人身体的集合。插件持有一个实例。"""

    def __init__(self, bridge: Any, world: Any, logger: Any) -> None:
        self._bridge = bridge
        self._world = world
        self._logger = logger
        self._bodies: dict[str, BodyController] = {}
        #: 行为包上报的坐标，作为"真实位置"的权威来源
        self.positions: dict[str, tuple[float, float, float]] = {}

    def add(self, name: str) -> BodyController:
        ctl = BodyController(name, self._bridge, self._world, self._logger)
        self._bodies[name] = ctl
        return ctl

    def get(self, name: str) -> BodyController | None:
        return self._bodies.get(name)

    def all(self) -> list[BodyController]:
        """当前所有身体。断开连接时用它把全部动作停掉。"""
        return list(self._bodies.values())

    def __len__(self) -> int:
        return len(self._bodies)

    def remove(self, name: str) -> None:
        ctl = self._bodies.pop(name, None)
        if ctl is not None:
            ctl.cancel()
        self.positions.pop(name, None)

    def tick(self) -> None:
        for ctl in self._bodies.values():
            ctl.tick()

    def update_positions(self, entries: list[dict]) -> None:
        """处理行为包的位置上报。"""
        for item in entries:
            name = str(item.get("n", ""))
            if not name:
                continue
            try:
                self.positions[name] = (
                    float(item.get("x", 0.0)),
                    float(item.get("y", 0.0)),
                    float(item.get("z", 0.0)),
                )
            except (TypeError, ValueError):
                continue

    def __len__(self) -> int:
        return len(self._bodies)

    def __iter__(self):
        return iter(self._bodies.values())
