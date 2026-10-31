"""行为包桥接层：Python ↔ JS 运行时的 scriptevent 通道。

背景
----
Endstone 的插件跑在 Python 运行时里，而 SimulatedPlayer 只存在于 BDS 的
脚本（JS）运行时里。两者不能直接调用，唯一的双向通道是 ``scriptevent``
命令——Python 用 ``dispatch_command`` 发一条，JS 侧订阅
``system.afterEvents.scriptEventReceive`` 收，JS 用 ``runCommand`` 发回来，
Python 用 ``@event_handler`` 的 ``ScriptMessageEvent`` 收。

这就带来三个必须处理的问题：

1. **鉴权** —— ``scriptevent`` 是公开命令，任何玩家都能发 `/scriptevent bot:spawn {...}`。
   所以握手时生成一个随机令牌，之后所有消息都必须带上它，否则丢弃。

2. **请求/响应配对** —— 通道是无序的、异步的。每个请求带一个自增 ``i``，
   回复原样带回，这样才分得清哪个 ``bot:dug`` 是回应哪次挖掘。

3. **异步动作** —— 真挖掘要按方块硬度花时间，不可能同步返回。所以
   ``dig`` 走的是"发起 + 回调"模型：``send_dig`` 立刻返回，真正的结果
   在 ``bot:dug`` 到达时通过回调派发。

活动检测
--------
行为包每 5 秒发一次 ``bot:pong``。超过 ``STALE_SECONDS`` 没收到就认为
身体层不可用——这时候上层会自动退回纯 Python 操作，而不是傻等。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


#: 超过这么久没有 pong 就认为行为包失联（秒）。行为包 5 秒一次心跳，
#: 给 4 倍的余量，避免服务器卡顿时误判。
STALE_SECONDS = 20.0


@dataclass
class PendingRequest:
    """一个已发出、等待回复的同步请求。"""

    kind: str
    created_at: float
    callback: Callable[[bool, str, dict], None] | None = None
    meta: dict = field(default_factory=dict)
    timeout: float | None = None


class FakePlayerBridge:
    """管理插件与假人行为包之间的通信。

    这个类刻意不碰任何游戏 API——它只调用传进来的 ``dispatch`` 函数发命令，
    以及被动接收 ``handle_message``。这样它可以脱离服务器单独测试。
    """

    def __init__(self, logger: Any, dispatch: Callable[[str], bool]) -> None:
        self._logger = logger
        self._dispatch = dispatch

        #: 领地/保护守卫：``(名字, x, y, z) -> 拒绝原因 | None``。
        #: 由插件层注入（它才知道 arc_core 和假人的合成 xuid）；
        #: None 表示放行，返回字符串表示"这个方块动不得"并被写进
        #: 动作失败原因反馈给大脑。保持 None 时一切照旧（可单测）。
        self.modify_guard: Callable[[str, int, int, int],
                                    str | None] | None = None

        self._token = ""
        self._seq = 0
        self._lock = threading.Lock()

        #: 请求号 → 待处理请求
        self._pending: dict[int, PendingRequest] = {}
        #: 挖掘请求号 → 完成回调（挖掘是异步的，单独一张表，不设超时清理，
        #: 由行为包的 100 tick 轮询上限保证一定会回来）
        self._dig_callbacks: dict[int, Callable[[bool, str, dict], None]] = {}

        self._last_pong = -999.0
        self._ever_connected = False
        #: 行为包当前管理的假人名字，用于对照自愈。
        #:
        #: ⚠️ **不变量：一律小写。** 这张表有四个写入点（`_on_pong`、
        #: `_finish` 的 spawn 分支、`bot:removed`、`bot:list_result`），
        #: 任何一个漏了 `.lower()`，跨分支的比较就会失灵——表现是
        #: "假人已经没了但表里还在"（removed 删不掉）或者
        #: "假人还在但表里没有"（pong 大小写不同）。所以读取方
        #: （`_warn_offline_risk` 等）也统一按小写查。
        self._remote_names: set[str] = set()
        #: 行为包自报的能力：能不能直接造假人、要不要靠 GameTest。
        #: 初始值刻意保守——还不知道对方什么底细，不该假设它能造。
        self._caps: dict[str, bool] = {
            "direct": False, "test": False, "fallback": False,
        }

    # ------------------------------------------------------------------
    # 基础属性
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """行为包是否还活着。这是上层决定"用真假人还是退回纯 Python"的依据。"""
        if self._last_pong < 0:
            return False
        return (time.monotonic() - self._last_pong) < STALE_SECONDS

    @property
    def ever_connected(self) -> bool:
        """是否曾经连上过。用于区分"没装行为包"和"装了一直没起来"。"""
        return self._ever_connected

    @property
    def last_pong_age(self) -> float:
        if self._last_pong < 0:
            return -1.0
        return time.monotonic() - self._last_pong

    @property
    def remote_names(self) -> set[str]:
        return set(self._remote_names)

    @property
    def caps(self) -> dict[str, bool]:
        """行为包自报的能力表。没连上时是一份保守的空表。"""
        return dict(self._caps)

    @property
    def can_spawn_directly(self) -> bool:
        """行为包能不能不用 GameTest 就造出假人。

        这是"屏幕上是不是 NPC"的关键分叉：不能直接造、GameTest 又没启动，
        那真身永远不会加入，占位 NPC 就会一直挂着。
        """
        return bool(self._caps.get("direct")) or bool(self._caps.get("test"))

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # ------------------------------------------------------------------
    # 令牌
    # ------------------------------------------------------------------

    def new_token(self) -> str:
        """生成新令牌。每次握手（插件启用、行为包重连）都换一次。"""
        with self._lock:
            self._token = secrets.token_hex(16)
            return self._token

    def _next_id(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    def _raw_send(self, event_id: str, data: dict) -> bool:
        payload = dict(data)
        payload["t"] = self._token
        msg = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        # scriptevent 的消息体是单行命令参数。这里有两类字符会毁掉它：
        #
        #   · 换行 —— 命令会被截断成两条。json.dumps 默认把 \n 写成
        #     两个字符 `\` `n`（合法 JSON 转义），本身是安全的；但
        #     万一将来有谁绕过 json 直接拼字符串，这里再兜一层。
        #   · **双引号** —— 这是真正踩过的坑。`json.dumps` 会把名字里的
        #     `"` 转义成 `\"`，可是 BDS 的命令参数解析器**会把这个反斜杠
        #     吃掉**，于是 `{"n":"hly\"x"}` 到了 JS 侧变成
        #     `{"n":"hly"x"}` —— `JSON.parse` 直接抛异常，整条指令静默
        #     丢失（JS 只打一条 console.warn，Python 侧完全不知情）。
        #
        # 修法：**限制喂进来的字段值**。名字本身就不该带引号（它会成为
        # 玩家名，Bedrock 也不允许），所以在源头把它净化掉；同时仍然
        # 把转义做全，作为第二道防线。
        msg = msg.replace("\n", "\\n").replace("\r", "\\r")
        if '"' in event_id:
            # event_id 是我们自己写死的常量，带引号只可能是代码被改坏了
            self._logger.error(f"内部错误：事件名 {event_id!r} 含引号，已拒绝发送")
            return False
        command = f"scriptevent {event_id} {msg}"
        try:
            return bool(self._dispatch(command))
        except Exception as exc:
            self._logger.debug(f"发送 {event_id} 失败: {exc}")
            return False

    def send_tracked(
        self,
        event_id: str,
        data: dict,
        *,
        kind: str = "",
        callback: Callable[[bool, str, dict], None] | None = None,
        timeout: float | None = None,
    ) -> int:
        """发一条需要回复的请求，返回请求号（失败返回 -1）。"""
        req_id = self._next_id()
        payload = dict(data)
        payload["i"] = req_id
        if callback is not None:
            self._pending[req_id] = PendingRequest(
                kind=kind or event_id, created_at=time.monotonic(), callback=callback,
                timeout=timeout,
            )
        if not self._raw_send(event_id, payload):
            self._pending.pop(req_id, None)
            return -1
        return req_id

    def send_fire_and_forget(self, event_id: str, data: dict) -> bool:
        """发一条不在乎回复的请求。"""
        return self._raw_send(event_id, data)

    # ------------------------------------------------------------------
    # 具体动作
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        return self._raw_send("bot:ping", {})

    def spawn(self, name: str, x: float, y: float, z: float,
              dimension: str = "overworld",
              game_mode: int | None = None,
              stealth: bool = False,
              respawn: tuple[float, float, float] | None = None,
              callback: Callable[[bool, str, dict], None] | None = None) -> int:
        """请求行为包造一个假人。

        `game_mode` 是**生成时**的游戏模式，数字，遵循 Bedrock 约定：
            0=survival  1=creative  2=adventure  3=spectator

        为什么要在这里传、而不是生成后由 Python 去改：
        SimulatedPlayer 会继承生成时所在世界的模式，生成后 Python 再写
        `player.game_mode` 属于"事后补救"，而且那是**直接操作假人对象**
        ——控制权应该在 JS 侧。传参给引擎一次做对，干净得多。

        传 `None` 表示"不干预"，由 JS 侧按世界当前默认模式走。

        `respawn` 是**重生点**（服务器配置好的出生点，x/y/z）。
        引擎的 setSpawnPoint 对 SimulatedPlayer 两种写法都不认，所以
        行为包只能在自己点完重生之后把假人传送回这个点——这个坐标得
        由 Python 侧告诉它。
        """
        payload: dict[str, Any] = {
            "n": name, "x": float(x), "y": float(y), "z": float(z),
            "d": dimension,
        }
        if game_mode is not None:
            payload["gm"] = int(game_mode)
        if stealth:
            # 隐身模式（自动在线的"路人群演"）：不挂 [AI] 名牌。
            # 一切"它是 AI"的公开标识都不给，让它看起来就是个玩家。
            payload["st"] = 1
        if respawn is not None:
            try:
                payload["rx"] = float(respawn[0])
                payload["ry"] = float(respawn[1])
                payload["rz"] = float(respawn[2])
            except (TypeError, ValueError, IndexError):
                pass
        return self.send_tracked(
            "bot:spawn", payload, kind="spawn", callback=callback,
        )

    def remove(self, name: str,
               callback: Callable[[bool, str, dict], None] | None = None) -> int:
        return self.send_tracked("bot:remove", {"n": name}, kind="remove", callback=callback)

    def walk(self, name: str, x: int, y: int, z: int, *,
             timeout: int = 100,
             callback: Callable[[bool, str, dict], None] | None = None) -> int:
        """让假人走到某处。用的是原版寻路，不是瞬移。"""
        return self.send_tracked(
            "bot:walk",
            {"n": name, "x": int(x), "y": int(y), "z": int(z), "timeout": int(timeout)},
            kind="walk", callback=callback,
        )

    def teleport(self, name: str, x: float, y: float, z: float,
                 dimension: str = "overworld",
                 callback: Callable[[bool, str, dict], None] | None = None) -> int:
        return self.send_tracked(
            "bot:teleport",
            {"n": name, "x": float(x), "y": float(y), "z": float(z), "d": dimension},
            kind="teleport", callback=callback,
        )

    def dig(self, name: str, x: int, y: int, z: int, *, tool: str = "",
            callback: Callable[[bool, str, dict], None] | None = None) -> int:
        """发起一次真挖掘。

        这是异步的：返回的请求号只表示"指令已发出"。真正的成败会在
        ``bot:dug`` 到达时通过 callback 告知，参数是
        ``(ok, error_text, info)``，info 里带 swing（挥臂次数）。
        """
        # 领地守卫：被拦下的挖掘**不发指令**，直接以一次失败回调收场
        # ——语义与 bot:dug 失败完全一致，调用方（执行器/闲逛驱动）
        # 的失败路径原样复用，原因文本会进大脑的反馈。
        reason = self._modify_blocked(name, x, y, z)
        if reason is not None:
            req_id = self._next_id()
            if callback is not None:
                callback(False, reason, {})
            return req_id
        req_id = self._next_id()
        if callback is not None:
            self._dig_callbacks[req_id] = callback
        payload = {"n": name, "x": int(x), "y": int(y), "z": int(z), "i": req_id}
        if tool:
            payload["tool"] = str(tool)
        if not self._raw_send("bot:dig", payload):
            self._dig_callbacks.pop(req_id, None)
            return -1
        return req_id

    def _modify_blocked(self, name: str, x: int, y: int, z: int) -> str | None:
        """守卫未注入或放行时返回 None；守卫抛异常按放行（fail-open）。"""
        if self.modify_guard is None:
            return None
        try:
            return self.modify_guard(name, int(x), int(y), int(z))
        except Exception as exc:
            if self._logger:
                try:
                    self._logger.debug(f"领地守卫异常（按放行）: {exc}")
                except Exception:
                    pass
            return None

    def place(self, name: str, x: int, y: int, z: int, block: str, *,
              face: str = "Up",
              callback: Callable[[bool, str, dict], None] | None = None) -> int:
        reason = self._modify_blocked(name, x, y, z)
        if reason is not None:
            req_id = self._next_id()
            if callback is not None:
                callback(False, reason, {})
            return req_id
        return self.send_tracked(
            "bot:place",
            {"n": name, "x": int(x), "y": int(y), "z": int(z),
             "block": str(block), "face": face},
            kind="place", callback=callback,
        )

    def till(self, name: str, x: int, y: int, z: int, *,
             callback=None) -> int:
        """锄头耕地：泥土/草地 → farmland。"""
        return self.send_tracked(
            "bot:till", {"n": name, "x": int(x), "y": int(y), "z": int(z)},
            kind="till", callback=callback,
        )

    def plant(self, name: str, x: int, y: int, z: int,
              item: str = "wheat_seeds", *, callback=None) -> int:
        """播种：种子对耕地右键（麦苗长在耕地上一格）。"""
        return self.send_tracked(
            "bot:plant", {"n": name, "x": int(x), "y": int(y), "z": int(z),
                          "item": str(item)},
            kind="plant", callback=callback,
        )

    def attack(self, name: str, target: str, *,
               callback: Callable[[bool, str, dict], None] | None = None) -> int:
        return self.send_tracked(
            "bot:attack", {"n": name, "target": str(target)},
            kind="attack", callback=callback,
        )

    def select(self, name: str, item: str,
               callback: Callable[[bool, str, dict], None] | None = None) -> int:
        return self.send_tracked(
            "bot:select", {"n": name, "item": str(item)},
            kind="select", callback=callback,
        )

    def consume(self, name: str, item: str = "", *,
                callback: Callable[[bool, str, dict], None] | None = None) -> int | bool:
        """进食结果由行为包确认。未传回调时保留旧版的 bool 返回值。"""
        data = {"n": name, "item": str(item)}
        if callback is None:
            return self._raw_send("bot:consume", data)
        return self.send_tracked("bot:consume", data, kind="consume", callback=callback)

    def jump(self, name: str) -> bool:
        """让假人跳一下。

        用引擎自己的跳跃，不要从 Python 侧"传送到它头上"——后者会
        绕过碰撞检测，假人能穿天花板。
        """
        return self._raw_send("bot:jump", {"n": name})

    def equip(self, name: str) -> bool:
        """让假人把背包里最好的护甲穿上（含副手盾牌）。

        具体挑哪件、怎么穿由 JS 侧 doEquip 决定（背包和装备栏都归
        行为包管）。已穿最好的时候 JS 侧是空操作，所以周期性调用
        也只是浪费一条 scriptevent 日志，无副作用。
        """
        return self._raw_send("bot:equip", {"n": name})

    def drop(self, name: str, item: str, count: int = 1, *, to: str = "",
             callback: Callable[[bool, str, dict], None] | None = None) -> int:
        """让假人把背包里的物品丢到地上（交易的基础动作）。

        `to` 给了目标玩家名时，JS 侧会丢在两者中点（8 格内），
        看起来像"递过去"。物品从假人背包真实扣除，玩家捡起即得。
        失败（背包没有该物品）通过 callback 的 err 带回。
        """
        payload: dict[str, Any] = {"n": name, "item": str(item),
                                   "count": max(1, int(count))}
        if to:
            payload["to"] = str(to)
        return self.send_tracked(
            "bot:drop", payload, kind="drop", callback=callback)

    def stop(self, name: str) -> bool:
        """让假人停下所有动作（移动 + 挖掘）。"""
        return self._raw_send("bot:stop", {"n": name})

    def arena(self, name: str, enemies: list[str], minutes: float = 6.0) -> bool:
        """开启枪战 arena 战斗 AI（找最近敌人→追→视线内开枪）。

        敌人名单由 Python 侧问枪战插件要（队伍名牌平时隐藏，JS 没法
        自己辨队），对局中可反复调用来刷新名单。
        """
        return self._raw_send("bot:arena", {
            "n": name, "enemies": [str(e) for e in enemies],
            "min": max(1.0, float(minutes)),
        })

    def arena_off(self, name: str) -> bool:
        """关闭 arena 战斗 AI。"""
        return self._raw_send("bot:arena", {"n": name, "off": 1})

    def container(self, name: str, x: int, y: int, z: int, spec: dict,
                  callback: Callable[[bool, str, dict], None] | None = None) -> int:
        """对某个方块容器做操作（箱子存取 / 熔炉熔炼 / 工作台合成）。

        `spec` 直接透传给 JS 侧 `doContainer`，字段随 op 不同：
          · {"op":"put","item":...,"count":...}
          · {"op":"take","item":...,"count":...}
          · {"op":"smelt","input":...,"fuel":...,"count":...}
          · {"op":"craft","slots":[{"slot":0,"id":...},...]}

        这是异步操作（合成/熔炼要等引擎产出），成功时回调的 info 里
        会带 `made`（{"id":..., "count":...}）。
        """
        payload = {"n": name, "x": int(x), "y": int(y), "z": int(z)}
        payload.update(spec)
        timeout = None
        if spec.get("op") == "smelt":
            # 单件熔炼约 10 秒，不能在通用 30 秒超时中终止整炉任务。
            count = max(1, min(64, int(spec.get("count", 1))))
            payload["count"] = count
            timeout = 12.0 * count + 10.0
        return self.send_tracked(
            "bot:container", payload, kind="container", callback=callback, timeout=timeout,
        )

    def status(self) -> bool:
        """请求一次完整状态上报。回复走 bot:status。"""
        return self._raw_send("bot:status", {})

    # ------------------------------------------------------------------
    # 接收
    # ------------------------------------------------------------------

    def handle_message(self, message_id: str, message: str) -> dict | None:
        """解析一条来自行为包的消息。

        返回 ``{"id": ..., "data": {...}}``，或 None（不是我们的消息 /
        没通过鉴权 / JSON 坏了）。调用方负责按 id 分派。
        """
        if not message_id.startswith("bot:"):
            return None
        try:
            data = json.loads(message) if message else {}
        except Exception:
            self._logger.debug(f"行为包消息 JSON 解析失败: {message[:120]}")
            return None
        if not isinstance(data, dict):
            return None

        token = data.get("t", "")
        if not self._token or token != self._token:
            # 可能是别的插件或玩家伪造的。不回应，也不记错误——
            # 安静丢弃是这里唯一正确的处理。
            self._logger.debug(f"忽略未通过鉴权的 scriptevent: {message_id}")
            return None

        return {"id": message_id, "data": data}

    def dispatch(self, message_id: str, message: str) -> str:
        """处理一条消息并派发到对应逻辑。返回处理结果标签，便于测试与日志。"""
        parsed = self.handle_message(message_id, message)
        if parsed is None:
            return "ignored"

        kind = parsed["id"]
        data = parsed["data"]

        if kind == "bot:pong":
            self._on_pong(data)
            return "pong"
        if kind == "bot:positions":
            return "positions"
        if kind == "bot:status":
            return "status"
        if kind == "bot:spawned":
            return self._finish(data, "spawn", bool(data.get("ok", False)),
                                "" if data.get("ok") else str(data.get("e", "生成失败")))
        if kind == "bot:removed":
            # ⚠️ 必须 `.lower()`。这张表在别处（`_on_pong`、`_finish`）
            # 一律以小写存入，这里要是存原样，遇到 `HLY` 这种大写名字
            # 就删不掉，表里会永远留一个已经消失的假人。
            self._remote_names.discard(str(data.get("n", "")).lower())
            return self._finish(data, "remove", True, "")
        if kind == "bot:result":
            return self._finish_result(data)
        if kind == "bot:dug":
            return self._finish_dig(data)
        if kind == "bot:list_result":
            # 同样一律小写——这张表的**唯一**表示是"小写"，
            # 任何一个分支忘了 lower，跨分支的比较就会失灵。
            self._remote_names = {str(n).lower() for n in data.get("names", [])}
            caps = data.get("caps")
            if isinstance(caps, dict):
                for key in ("direct", "test", "fallback"):
                    if key in caps:
                        self._caps[key] = bool(caps[key])
            return "list"
        if kind == "bot:error":
            self._logger.warning(f"行为包报错: {data}")
            return "error"
        return "unhandled"

    # -- 内部派发 ------------------------------------------------------

    def _on_pong(self, data: dict) -> None:
        first = not self._ever_connected
        self._last_pong = time.monotonic()
        self._ever_connected = True
        names = data.get("names")
        if isinstance(names, list):
            self._remote_names = {str(n).lower() for n in names}
        caps = data.get("caps")
        if isinstance(caps, dict):
            # 只在字段存在时覆盖。行为包是升级上来的旧版本时可能不发 caps，
            # 那时候保留上一次的判断比清空成"不会造"要安全——
            # 清空会让上层把一具正常的真身误判成不可用。
            for key in ("direct", "test", "fallback"):
                if key in caps:
                    self._caps[key] = bool(caps[key])
        if first:
            self._logger.info("假人行为包已连接，SimulatedPlayer 可用。")
            if not self.can_spawn_directly:
                # 说清楚为什么可能只是个 NPC —— 不然用户只能看到现象。
                self._logger.warning(
                    "行为包自报无法直接创建假人（模块级 API 不可用且 "
                    "GameTest 未启动）。真身不会加入，只会留下占位实体。")


    def _finish(self, data: dict, kind: str, ok: bool, err: str) -> str:
        req_id = data.get("i")
        pend = self._pending.pop(req_id, None) if isinstance(req_id, int) else None
        if pend is not None and pend.callback is not None:
            try:
                pend.callback(ok, err, data)
            except Exception as exc:
                self._logger.warning(f"请求 {req_id} 回调异常: {exc}")
        if kind == "spawn" and ok:
            name = str(data.get("n", ""))
            if name:
                self._remote_names.add(name.lower())
        return kind

    def _finish_result(self, data: dict) -> str:
        req_id = data.get("i")
        ok = bool(data.get("ok", False))
        err = str(data.get("e", ""))
        pend = self._pending.pop(req_id, None) if isinstance(req_id, int) else None
        if pend is not None and pend.callback is not None:
            try:
                pend.callback(ok, err, data)
            except Exception as exc:
                self._logger.warning(f"请求 {req_id} 回调异常: {exc}")
        return "result"

    def _finish_dig(self, data: dict) -> str:
        req_id = data.get("i")
        ok = bool(data.get("ok", False))
        err = str(data.get("e", ""))
        cb = self._dig_callbacks.pop(req_id, None) if isinstance(req_id, int) else None
        if cb is not None:
            try:
                cb(ok, err, data)
            except Exception as exc:
                self._logger.warning(f"挖掘回调 {req_id} 异常: {exc}")
        return "dug"

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------

    def expire_pending(self, timeout: float = 30.0) -> int:
        """清理超时的同步请求。返回清理数量。

        没有这一步的话，一次丢包就会让某个回调永远挂着，
        上层可能因此一直等下去。
        """
        if not self._pending:
            return 0
        now = time.monotonic()
        dead = [rid for rid, p in self._pending.items()
                if now - p.created_at > (p.timeout if p.timeout is not None else timeout)]
        for rid in dead:
            pend = self._pending.pop(rid, None)
            if pend is not None and pend.callback is not None:
                try:
                    pend.callback(False, "行为包未响应（超时）", {})
                except Exception:
                    pass
        if dead:
            self._logger.debug(f"清理了 {len(dead)} 个超时请求")
        return len(dead)

    def reset(self) -> None:
        """断开时清空所有状态。"""
        self._pending.clear()
        self._dig_callbacks.clear()
        self._remote_names.clear()
        # 能力表也一起清掉：重连后对方可能是另一个版本的行为包，
        # 沿用旧判断会导致"以为能造其实不能"。
        self._caps = {"direct": False, "test": False, "fallback": False}
        self._last_pong = -999.0
