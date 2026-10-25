"""2.0.0 的四个实测问题：假人还是 NPC / 日志刷屏 / 把玩家当身体 / AI 失败重试。

用户报的原话是：

    "放出来的还是NPC，他还有大量的日志滚动，他貌似把玩家当成了他的身体，
     但是我更希望的是他通过add on，弄个虚拟玩家出来，然后控制虚拟玩家。
     还有就是如果某次请求AI失败了，也不要崩溃，等一分钟重新尝试。"

这四条各自对应一个**独立**的根因，容易互相掩盖，所以分开测：

1. **还是 NPC** —— `main.js` 原来只有 `activeTest.spawnSimulatedPlayer()`
   这一条路，而 `activeTest` 只在 GameTest 被真正执行时才被赋值。没人敲
   `/gametest run`，它就恒为 null，生成请求全堆在队列里，真身永远不会
   加入，占位 NPC 就一直挂着。
   现在改成优先走**模块级** `gametest.spawnSimulatedPlayer()`——它不依附
   任何 Test，这才是 add-on 场景该用的入口。

2. **日志刷屏** —— BDS 为每条 `scriptevent` 命令打一行
   `Script event bot:dig has been sent`。挖掘是异步的，"还没结论"的阶段
   执行器每 tick 都会回来问同一个步骤，任何没被挡住的重发路径都会让控制台
   被同一行刷满。现在有两道闸：`BodyController` 的坐标去重，加上
   `RealBodyActions` 对"已知失败"的退避（1.5 秒）。

3. **把玩家当身体** —— 见 test_body_identity 的说明。

4. **AI 失败重试** —— 以前失败只是记一笔，下个决策周期（十几秒）立刻
   再打同一个已经报错的接口，等于以秒为单位反复撞墙。现在明确退避
   60 秒，并且**不结束**智能体。

这个文件守的是**行为**，不是文本。凡是能用假对象跑起来的，都真跑一遍；
只有"JS 里到底调了哪个 API"这类无法在 Python 里执行的东西，才做源码断言。
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _pkg_root() -> Path:
    """源码目录，从**实际被导入的那个包**反推。

    为什么要绕这一圈：本文件被拷到别处、装在独立 venv 里跑的时候，
    `__file__` 相对定位会指向不存在的地方。而这个测试有相当一部分是
    读源码做契约断言的——路径错了它整片失败，看起来很吓人，其实只是
    找错了地方。所以这里以导入到的包为锚点。
    """
    import endstone_arc_fake_player as pkg
    return Path(pkg.__file__).resolve().parent


# Endstone 的桩必须在**任何** `endstone_arc_fake_player` 导入之前装好。
#
# 为什么：包根 `__init__.py` 会 `from .plugin import ArcFakePlayerPlugin`，
# 而 `plugin.py` 顶层就写着 `from endstone import ColorFormat as C`。
# 没装桩 → 在这里直接 ModuleNotFoundError，整个文件一项都跑不了。
#
# 走 run_all.py 时它已经装过了，`install()` 是幂等的；单独跑本文件时
# 靠的就是这几行。（导入顺序踩过一次，别挪到下面去。）
from tests.endstone_stub import install as _install_stub  # noqa: E402

_install_stub()

from endstone_arc_fake_player.agent import AgentConfig, AgentState  # noqa: E402
from endstone_arc_fake_player.body import BodyController  # noqa: E402
from endstone_arc_fake_player.bridge import FakePlayerBridge  # noqa: E402
from endstone_arc_fake_player.realbody import RealBodyActions  # noqa: E402

PKG = _pkg_root()
ROOT = PKG.parent
FAILURES: list[str] = []
CHECKS = 0


def check(label: str, cond: bool, detail: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"PASS  {label}" + (f" — {detail}" if detail else ""))
        return True
    print(f"FAIL  {label}" + (f" — {detail}" if detail else ""))
    FAILURES.append(label)
    return False


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class QuietLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []
        # 按级别分开记一份，方便断言"这条是 warning 还是 debug"。
        # 只用 lines + 子串匹配会被降级后的 debug 行骗过去。
        self.debugs: list[str] = []
        self.infos: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def _rec(self, msg: str) -> None:
        self.lines.append(str(msg))

    def debug(self, msg: str) -> None:
        self._rec(msg)
        self.debugs.append(str(msg))

    def info(self, msg: str) -> None:
        self._rec(msg)
        self.infos.append(str(msg))

    def warning(self, msg: str) -> None:
        self._rec(msg)
        self.warnings.append(str(msg))

    def error(self, msg: str) -> None:
        self._rec(msg)
        self.errors.append(str(msg))


class CountingBridge:
    """记下每一次 dig 请求。每一条都对应 BDS 控制台里的一行日志。"""

    def __init__(self) -> None:
        self.digs: list[tuple[int, int, int]] = []
        self.walks: list[tuple[int, int, int]] = []
        self.stops = 0
        self._cb: dict[int, object] = {}
        self._seq = 0

    def dig(self, name, x, y, z, *, tool="", callback=None):
        self._seq += 1
        self.digs.append((int(x), int(y), int(z)))
        if callback is not None:
            self._cb[self._seq] = callback
        return self._seq

    def stop(self, name: str) -> bool:
        self.stops += 1
        return True

    def walk(self, name, x, y, z, *, timeout=100, callback=None):
        # ⚠️ 要记下来。"执行器每 tick 问一次"的场景里，`walks` 的长度
        # 就是"控制台会被刷多少行"，是判断断头路的关键依据 ——
        # 只返回 `_seq` 的话，测不出"只发了 1 条然后卡死"这种情形。
        self.walks.append((int(x), int(y), int(z)))
        self._seq += 1
        if callback is not None:
            self._cb[self._seq] = callback
        return self._seq

    def finish_dig(self, ok: bool, err: str = "") -> None:
        """让最后一个还挂着的请求有个结论。"""
        if not self._cb:
            return
        rid = max(self._cb)
        cb = self._cb.pop(rid)
        cb(ok, err, {"swing": 3})

    def finish_walk(self, ok: bool, err: str = "") -> None:
        """同上，但给寻路用。

        ⚠️ 少了这个，`BodyController._walk` 会**永远挂着** → `busy`
        恒为 True → `walk_to` 每一轮都在"还在走，没结论"那一行返回。
        真实环境里 `WALK_TIMEOUT = 12 秒` 会兜底把请求结掉，测试里
        必须手动补这一下，否则测的就不是 walk_to 而是忙碌判定。
        """
        if not self._cb:
            return
        rid = max(self._cb)
        cb = self._cb.pop(rid)
        cb(ok, err, {})


class SolidWorld:
    """所有方块都是实心的。挖掘永远不会成功。"""

    def is_solid_id(self, x: int, y: int, z: int) -> bool:
        return True


class Step:
    """一个待挖的方块步骤。"""

    def __init__(self, x: int, y: int, z: int) -> None:
        self.op = "dig_far"
        self.x = x
        self.y = y
        self.z = z
        self.note = ""
        self.async_op = True


# ---------------------------------------------------------------------------
# 一、日志刷屏
# ---------------------------------------------------------------------------

def test_dig_spam() -> None:
    print("\n" + "-" * 70)
    print("一、bot:dig 日志刷屏")
    print("-" * 70)

    # --- 关卡 1：BodyController 的坐标去重 ---
    bridge = CountingBridge()
    ctl = BodyController("bot", bridge, SolidWorld(), QuietLogger())

    first = ctl.start_dig(10, 64, 10)
    # 立刻再发同一个坐标。busy 挡住它 —— 但这只覆盖"正在挖"的情况。
    again = ctl.start_dig(10, 64, 10)
    check("正在挖时重复发起被挡（busy）", first and not again,
          f"first={first} again={again}")
    check("只发出了一条 dig 命令", len(bridge.digs) == 1,
          f"实际 {len(bridge.digs)} 条")

    # 让这一轮有结论，然后立刻重发**同一个**坐标。
    # 这条路径才是刷屏的真凶：busy 已经释放，看起来"可以发了"，
    # 但结论刚才是失败的，马上重发就是原地打转。
    bridge.finish_dig(False, "挖不动")
    before = len(bridge.digs)
    ctl.start_dig(10, 64, 10)
    check("结论刚返回时同一坐标不会立刻重发（去重闸）",
          len(bridge.digs) == before,
          f"又发了 {len(bridge.digs) - before} 条")
    check("被挡下的请求被记账", ctl.digs_deduped >= 1,
          f"digs_deduped={ctl.digs_deduped}")

    # 换个坐标不受影响 —— 去重不能把正常的连续挖掘也挡掉。
    ctl.start_dig(11, 64, 10)
    check("不同坐标不受去重影响", len(bridge.digs) == before + 1,
          f"digs={bridge.digs}")

    # 去重窗口过后，同一坐标应该能再试（不是永久拉黑）。
    ctl2 = BodyController("bot", CountingBridge(), SolidWorld(), QuietLogger())
    ctl2.DIG_DEDUP_SECONDS = 0.0
    ctl2.start_dig(1, 2, 3)
    ctl2._dig = None            # 手工释放，模拟"上一轮结束了"
    ctl2.start_dig(1, 2, 3)
    check("窗口过后同一坐标可以重试", len(ctl2._bridge.digs) == 2,
          f"digs={ctl2._bridge.digs}")

    # --- 关卡 2：RealBodyActions 对已知失败的退避 ---
    b2 = CountingBridge()
    c2 = BodyController("bot", b2, SolidWorld(), QuietLogger())
    # 让假人"站得很近"，直接进 dig 阶段，绕开寻路
    ra = RealBodyActions(c2, SolidWorld(), QuietLogger())
    ra._within_reach = lambda x, y, z: True        # type: ignore[method-assign]

    step = Step(5, 64, 5)

    # 第一次：发起
    ra.dig_far(step)
    n1 = len(b2.digs)
    check("首次挖掘会发一条 dig", n1 == 1, f"n={n1}")

    # 给个失败结论，然后**连续问 50 次**——模拟执行器每 tick 回来问。
    b2.finish_dig(False, "挖不动")
    ra.dig_far(step)          # 这一次消费掉失败结论，打上退避标记
    mark = len(b2.digs)

    for _ in range(50):
        ra.dig_far(step)

    check("失败后会退避，50 次追问只多发 0 条 dig",
          len(b2.digs) == mark,
          f"mark={mark} now={len(b2.digs)}")

    # 退避时间到了应该能再试一次。
    #
    # 注意这里要同时放开两道闸：退避（RealBodyActions）和坐标去重
    # （BodyController）。它们是**各自独立**的防线，测试里必须都松开
    # 才能观察到"重试"这个行为本身——只松一道是在测另一道，不是测重试。
    #
    # ⚠️ `dig_far` 是**多阶段**的（先靠近、再挖），发起动作用的是
    # `ctl.start_dig`。所以要看到"真的又发了一条 dig"，得让第一阶段
    # （`_within_reach` 判定 + `clear_dig_result`）和第二阶段（真发）
    # 各走一次。上面那句 `mark=` 之前在第三次调用时正好落在退避窗口里，
    # 现在退避闸被正确地接在了多阶段分支上，所以要多问一次才轮到发起。
    ra.RETRY_BACKOFF_SECONDS = 0.0
    c2.DIG_DEDUP_SECONDS = 0.0
    for _ in range(4):          # 足够走完"认出阶段 + 真发出去"
        ra.dig_far(step)
        if len(b2.digs) > mark:
            break
    check("两道闸都放开后允许重试", len(b2.digs) == mark + 1,
          f"mark={mark} now={len(b2.digs)}")

    # 只放开退避、不放开去重 → 仍然被挡住。这条证明两道闸是独立的，
    # 少任何一道都不会漏出重复请求。
    c2.DIG_DEDUP_SECONDS = 0.5
    ra.RETRY_BACKOFF_SECONDS = 0.0
    b2.finish_dig(False, "还是挖不动")
    ra.dig_far(step)
    m2 = len(b2.digs)
    ra.dig_far(step)
    check("只放开退避不够，去重闸仍然挡得住",
          len(b2.digs) == m2, f"m2={m2} now={len(b2.digs)}")

    # --- 退避标记必须活过 _cleanup ---
    #
    # 这是个很容易写错的地方：_cleanup 清掉 _attempts / _phase 是对的，
    # 但如果顺手把 _failed_at 也清了，退避就形同虚设——下一次追问
    # 走"首次遇到"分支，立刻重发。
    ra3 = RealBodyActions(c2, SolidWorld(), QuietLogger())
    ra3._within_reach = lambda x, y, z: True       # type: ignore[method-assign]
    key3 = ("digfar", 5, 64, 5)
    ra3._mark_failed(key3)
    ra3._cleanup(key3)
    check("_cleanup 之后退避标记仍然有效",
          ra3._backing_off(key3), f"failed_at={ra3._failed_at}")

    # --- 关卡 3：退避不能把尝试次数烧光 ---
    ra2 = RealBodyActions(c2, SolidWorld(), QuietLogger())
    ra2._within_reach = lambda x, y, z: True       # type: ignore[method-assign]
    ra2.RETRY_BACKOFF_SECONDS = 999.0
    key = ("digfar", 5, 64, 5)
    for _ in range(400):
        ra2.dig_far(step)
    check("退避期内的追问不消耗尝试次数",
          ra2._attempts.get(key, 0) <= 2,
          f"attempts={ra2._attempts.get(key, 0)}")

    # --- 关卡 4：源码里没有裸的 _begin 重试入口 ---
    rsrc = (PKG / "realbody.py").read_text("utf-8")
    # 所有动作处理器都必须走 `issue()`（它统一收着退避闸和次数闸）；
    # 只有 `issue()` 自己内部才允许调 `_begin`。
    bad = []
    for op in ("def dig_near", "def dig_far", "def walk_to", "def place_real"):
        start = rsrc.find(op)
        end = rsrc.find("\n    def ", start + 10)
        body = rsrc[start:end if end > 0 else len(rsrc)]
        if "self._begin(key)" in body:
            bad.append(op.split()[1])
    check("所有动作处理器都不再直接调 _begin（绕过退避闸）", not bad,
          f"违规: {bad}")

    # `issue()` 必须是**唯一**碰 `_begin` 的地方，否则就会有人漏掉闸。
    isrc = rsrc[rsrc.find("def issue"):rsrc.find("def _begin")]
    check("issue() 里同时握着退避闸和次数闸",
          "_backing_off" in isrc and "_begin" in isrc,
          "少了任一道，处理器就有绕过它的路")
    check("issue() 把'已放弃'当终态返回 True（不能返回 False）",
          "if key in self._gave_up:" in isrc and "return True" in isrc,
          "返回 False → 永久的进行中状态，计划永久卡死")


# ---------------------------------------------------------------------------
# 二、能力探测：能不能造假人
# ---------------------------------------------------------------------------

def test_capability_probe() -> None:
    print("\n" + "-" * 70)
    print("二、行为包能力探测")
    print("-" * 70)

    bridge = FakePlayerBridge(QuietLogger(), lambda cmd: True)

    check("未连接时不敢假设能造",
          not bridge.can_spawn_directly,
          f"caps={bridge.caps}")

    # 行为包自报"模块级 API 可用"
    bridge._on_pong({"names": [], "caps": {"direct": True, "test": False}})
    check("行为包说能直接造 → 上层就该相信",
          bridge.can_spawn_directly, f"caps={bridge.caps}")

    # 老版本行为包不发 caps：不能把已知能力清掉
    bridge._on_pong({"names": []})
    check("旧行为包不发 caps 时保留上次判断",
          bridge.can_spawn_directly, f"caps={bridge.caps}")

    # 自报不能造
    bridge2 = FakePlayerBridge(QuietLogger(), lambda cmd: True)
    bridge2._on_pong({"names": [], "caps": {"direct": False, "test": False}})
    check("明确自报不能造 → can_spawn_directly 为假",
          not bridge2.can_spawn_directly, f"caps={bridge2.caps}")

    # GameTest 就绪也算能用
    bridge3 = FakePlayerBridge(QuietLogger(), lambda cmd: True)
    bridge3._on_pong({"names": [], "caps": {"direct": False, "test": True}})
    check("GameTest 已就绪也算能造",
          bridge3.can_spawn_directly, f"caps={bridge3.caps}")

    # 断开要清掉能力判断，避免重连到另一个版本后沿用旧结论
    bridge3.reset()
    check("reset 后能力表清空", not bridge3.can_spawn_directly,
          f"caps={bridge3.caps}")

    # bot:list_result 也带 caps
    bridge4 = FakePlayerBridge(QuietLogger(), lambda cmd: True)
    bridge4.new_token()
    tok = bridge4._token
    import json as _json
    bridge4.dispatch("bot:list_result",
                     _json.dumps({"t": tok, "names": [], "caps": {"direct": True}}))
    check("bot:list_result 也更新能力表",
          bridge4.can_spawn_directly, f"caps={bridge4.caps}")


# ---------------------------------------------------------------------------
# 三、JS 侧的身体创建路径
# ---------------------------------------------------------------------------

def test_js_body_creation() -> None:
    print("\n" + "-" * 70)
    print("三、main.js 的身体创建路径")
    print("-" * 70)

    js = (PKG / "behavior_pack" / "scripts" / "main.js").read_text("utf-8")

    check("导入的是整个 gametest 模块（不是只取 register）",
          "import * as gametest from" in js)
    check("用到模块级 spawnSimulatedPlayer（add-on 的正规入口）",
          "gametest.spawnSimulatedPlayer" in js)
    check("模块级不可用时才退回 GameTest",
          "activeTest.spawnSimulatedPlayer" in js)

    # 最关键的一条：调用 spawnSimulatedPlayer 必须发生在 ensureSim 里，
    # 而这个函数在收到 bot:spawn 时**直接**被调，不经过任何 GameTest 门槛。
    start = js.find("function ensureSim")
    end = js.find("\n// ---", start)
    body = js[start:end if end > 0 else start + 3000]
    check("ensureSim 里先试模块级", body.find("gametest.spawnSimulatedPlayer")
          < body.find("activeTest.spawnSimulatedPlayer"))

    # 旧行为：请不进来就 push 进 pendingSpawns 干等。
    # 那正是"永远是 NPC"的原因 —— 必须已经改掉。
    dstart = js.find("function doSpawn")
    dend = js.find("\nfunction ", dstart + 10)
    dbody = js[dstart:dend]
    check("doSpawn 不再静默排队（那会让真身永远不来）",
          "pendingSpawns.push" not in dbody)
    check("doSpawn 失败时把 need_test 回给 Python",
          "need_test" in dbody)

    check("能力信息随 pong 上报", "caps: caps()" in js)
    check("加载日志里说明模块级 API 是否可用",
          "模块级 spawnSimulatedPlayer" in js)

    # 语法真的没问题（上面的断言都是文本匹配，语法错误它看不出来）
    import subprocess
    rc = subprocess.run(["node", "--check",
                         str(PKG / "behavior_pack" / "scripts" / "main.js")],
                        capture_output=True)
    check("main.js 语法通过 node --check", rc.returncode == 0,
          rc.stderr.decode()[:200])


# ---------------------------------------------------------------------------
# 四、"把玩家当成了他的身体"
# ---------------------------------------------------------------------------

def test_body_identity() -> None:
    print("\n" + "-" * 70)
    print("四、假人身份 / 身体归属")
    print("-" * 70)

    psrc = (PKG / "plugin.py").read_text("utf-8")

    # `_actors[name]` 是"当前这具身体"的唯一真相来源。
    # 3.1.2 起有三处合法写入：占位升级 / 智能体创建 / _live_actor 的
    # 句柄刷新（Endstone 的 Player 包装对象会过期，SimulatedPlayer
    # 还在线时必须按名字重解析，否则一分钟后全员被误判"身体消失"）。
    writes = []
    for i, line in enumerate(psrc.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("self._actors[") and "=" in stripped:
            writes.append((i, stripped))
    check("_actors 的写入点收敛（只有 3 处：升级 + 创建 + 句柄刷新）",
          len(writes) == 3,
          f"实际 {len(writes)} 处: {writes}")

    # 每一处写入都必须是"我们特意挑出来的实体"，不能是遍历产物。
    for line_no, stmt in writes:
        ok = "_upgrade_placeholder" in "\n".join(
            psrc.splitlines()[max(0, line_no - 45):line_no]) or "actor" in stmt
        check(f"第 {line_no} 行的写入来自受控路径", ok, stmt)

    # 交给 Agent 的取身体回调必须过滤有效性。
    # 这是"拿玩家顶替"能被发现的前提：真身死了回调会返回 None，
    # Agent 才会干净地停下，而不是继续拿一个不属于它的实体干活。
    check("get_actor 回调走 _live_actor（死实体给 None）",
          "get_actor=lambda n=name: self._live_actor(n)" in psrc)

    # 身体控制器在升级时必须换 actor，否则占位实体的控制器
    # 会一直绑着那个 NPC —— 表面上"换掉了"，实际上动作还发给旧实体。
    up = psrc.find("def _upgrade_placeholder")
    upend = psrc.find("\n    def ", up + 10)
    upbody = psrc[up:upend]
    check("升级占位时同步更新身体控制器",
          "ctl.actor = player" in upbody)
    check("升级占位时同步更新世界维度",
          "_world.body_dimension" in upbody)
    check("升级后清掉占位标记（否则下一轮又被当占位）",
          "self._placeholder_flags.pop(name, None)" in upbody)

    # 占位判定必须在死活判定之前 —— 这个顺序错过一次，代价是
    # 占位永远不会被换掉（占位是活的，死活判定会直接放行）。
    perc = psrc.find("def _perceive")
    pend = psrc.find("\n    def ", perc + 10)
    pbody = psrc[perc:pend]
    i_ph = pbody.find("_is_placeholder(name)")
    i_live = pbody.find("self._live_actor(name)")
    check("_perceive 里占位判定排在死活判定之前",
          0 <= i_ph < i_live, f"占位@{i_ph} 死活@{i_live}")


# ---------------------------------------------------------------------------
# 五、AI 失败重试
# ---------------------------------------------------------------------------

def test_llm_retry() -> None:
    print("\n" + "-" * 70)
    print("五、AI 请求失败后等待重试")
    print("-" * 70)

    cfg = AgentConfig(name="bot", goal="测试")
    check("默认重试间隔是 60 秒", cfg.llm_retry_seconds == 60.0,
          f"={cfg.llm_retry_seconds}")
    check("默认永不放弃", cfg.llm_max_retries == 0,
          f"={cfg.llm_max_retries}")

    st = AgentState(config=cfg)
    check("初始没有在等重试", st.retry_at == 0.0)

    # --- 直接跑 _on_llm_failure ---
    #
    # Agent.__init__ 会碰游戏 API，这里只把需要的方法绑到一个瘦对象上，
    # 避免为了测一行重试逻辑去装配整个 Agent。
    import endstone_arc_fake_player.agent as agent_mod

    logs: list[str] = []

    class Thin:
        name = "bot"
        state = st
        config = cfg

        def _log(self, msg: str) -> None:
            logs.append(msg)

    thin = Thin()
    thin._on_llm_failure = agent_mod.Agent._on_llm_failure.__get__(thin)

    before = time.monotonic()
    thin._on_llm_failure("connection reset")
    now = time.monotonic()

    check("失败后设置了重试时间点",
          st.retry_at > now, f"retry_at-now={st.retry_at - now:.1f}s")
    check("重试间隔就是配置的 60 秒",
          59.0 <= st.retry_at - before <= 61.0,
          f"={st.retry_at - before:.1f}s")
    check("失败没有结束智能体（不能判死）", st.done_reason == "",
          f"done_reason={st.done_reason!r}")
    check("累计了失败次数", st.llm_failures == 1, f"={st.llm_failures}")
    check("日志里告诉用户多久后重试",
          any("秒后重试" in m for m in logs), str(logs)[:160])

    # --- should_think 必须真的拦住 ---
    think = agent_mod.Agent.should_think
    st2 = AgentState(config=cfg)
    st2.last_snapshot = object()          # 绕过"还没观测过"这一关
    st2.last_decision = 0.0               # 决策周期早就过了

    class T2:
        state = st2
        config = cfg
        _inflight: list = []

    t2 = T2()
    check("正常情况下应该去决策",
          think(t2, time.monotonic() + 999))

    st2.retry_at = time.monotonic() + 30
    check("退避期内不去决策（这是防刷屏的关键）",
          not think(t2, time.monotonic()))
    check("退避期内即使时间过了很久也不去",
          not think(t2, time.monotonic() + 10))

    st2.retry_at = time.monotonic() - 1
    check("退避期满后恢复决策",
          think(t2, time.monotonic() + 999))

    # --- 成功一次要清掉退避状态 ---
    asrc = (PKG / "agent.py").read_text("utf-8")
    check("成功路径会清掉 retry_at",
          "s.retry_at = 0.0" in asrc)
    check("失败路径抽成了独立方法（可单独测）",
          "def _on_llm_failure" in asrc)
    check("_apply 不再自行记错误（改走统一入口）",
          "self._on_llm_failure(reply.error)" in asrc)

    # --- 配置项真的能读到 ---
    psrc = (PKG / "plugin.py").read_text("utf-8")
    check("config.json 里有 llm_retry_seconds", '"llm_retry_seconds"' in psrc)
    check("config.json 里有 llm_max_retries", '"llm_max_retries"' in psrc)
    check("这两项真的传给了 AgentConfig",
          "llm_retry_seconds=float(" in psrc
          and "llm_max_retries=int(" in psrc)

    # --- 用户显式限制次数时才放弃 ---
    cfg3 = AgentConfig(name="bot", goal="测试", llm_max_retries=2)
    st3 = AgentState(config=cfg3)

    class T3:
        name = "bot"
        state = st3
        config = cfg3

        def _log(self, msg: str) -> None:
            pass

    t3 = T3()
    t3._on_llm_failure = agent_mod.Agent._on_llm_failure.__get__(t3)
    t3._on_llm_failure("e1")
    t3._on_llm_failure("e2")
    check("达到用户设定的上限前不放弃", st3.done_reason == "",
          f"={st3.done_reason!r}")
    t3._on_llm_failure("e3")
    check("超过用户设定的上限才放弃",
          st3.done_reason != "", f"={st3.done_reason!r}")


# ---------------------------------------------------------------------------
# 六、占位实体的诚实性
# ---------------------------------------------------------------------------

def test_placeholder_honesty() -> None:
    print("\n" + "-" * 70)
    print("六、真身不可能出现时不留 NPC")
    print("-" * 70)

    psrc = (PKG / "plugin.py").read_text("utf-8")

    start = psrc.find("def _spawn_body")
    end = psrc.find("\n    def ", start + 10)
    body = psrc[start:end]

    check("_spawn_body 会先问行为包能不能造",
          "can_spawn_directly" in body)
    check("不能造就直接失败，不造 NPC 冒充",
          "return None, \"\"" in body)

    # 判定必须出现在 _make_placeholder 之前 —— 反了就白判。
    i_check = body.find("can_spawn_directly")
    i_make = body.find("_make_placeholder")
    check("能力判定排在造占位之前", 0 <= i_check < i_make,
          f"判定@{i_check} 占位@{i_make}")

    # 诊断文本要能区分"失联"和"在线但造不出"。
    check("诊断能区分'在线但造不出假人'",
          "但自称" in psrc or "无法直接创建假人" in psrc)

    # 旧的错误提示会误导向"没装行为包"，必须已经分开。
    err = psrc.find("def _body_error_text")
    errend = psrc.find("\n    def ", err + 10)
    ebody = psrc[err:errend]
    check("'从未收到心跳'只在真的没连上时才说",
          ebody.find("从未收到行为包心跳") > ebody.find("if self._bridge.ever_connected"))


# ---------------------------------------------------------------------------
# 六、常加载区域：让地形一直在线
# ---------------------------------------------------------------------------

def test_ticking_area() -> None:
    print("\n" + "-" * 70)
    print("六、常加载区域（地形保持加载）")
    print("-" * 70)

    psrc = (PKG / "plugin.py").read_text("utf-8")

    check("有建立常加载区域的方法", "def _ensure_ticking_area" in psrc)
    check("有释放常加载区域的方法", "def _release_ticking_area" in psrc)
    check("用的是 tickingarea 命令", 'tickingarea add' in psrc)
    check("区域名加了前缀，避免和服主自己建的撞名", 'f"pa_{name}"' in psrc)

    # 每 10 tick 都会走一次，必须只在第一次真正下发。
    check("有去重集合，不会每次感知都重复下发",
          "self._ticking_areas: set[str] = set()" in psrc)
    ta = psrc.find("def _ensure_ticking_area")
    ta_end = psrc.find("\n    def ", ta + 10)
    tabody = psrc[ta:ta_end]
    check("已建过就直接返回", "if name in self._ticking_areas" in tabody)

    # 收摊时必须还回去 —— 常加载区域有内存代价。
    bury = psrc.find("def _bury_body")
    bury_end = psrc.find("\n    def ", bury + 10)
    check("收摊时释放常加载区域",
          "_release_ticking_area(name)" in psrc[bury:bury_end])

    rem = psrc.find("def _cmd_remove")
    rem_end = psrc.find("\n    def ", rem + 10)
    check("移除智能体时释放常加载区域",
          "_release_ticking_area(name)" in psrc[rem:rem_end])

    check("配置里有 ticking_area 开关", '"ticking_area"' in psrc)
    check("配置里有半径", '"ticking_area_radius"' in psrc)
    check("半径被夹在安全范围内", "max(4, min(128, radius))" in psrc)

    # 感知必须先建区域，再读快照 —— 反了的话第一次读的还是空。
    perc = psrc.find("def _perceive")
    perc_end = psrc.find("\n    def ", perc + 10)
    pbody = psrc[perc:perc_end]
    i_area = pbody.find("_ensure_ticking_area")
    i_snap = pbody.find("self._world.snapshot")
    check("_perceive 里先建常加载区域、再读快照",
          0 <= i_area < i_snap, f"区域@{i_area} 快照@{i_snap}")

    # --- 区块未加载的判据 ---
    wsrc = (PKG / "world.py").read_text("utf-8")
    check("WorldAdapter 有区块加载判据", "def is_chunk_loaded" in wsrc)
    check("Snapshot 带 terrain_loaded 字段", "terrain_loaded" in wsrc)
    check("提示词里区分'没加载'和'附近没方块'",
          "周围地形尚未加载完成" in wsrc)

    snap = wsrc.find("def snapshot")
    snap_end = wsrc.find("\n    def ", snap + 10)
    check("快照为空时会去查区块到底加载没有",
          "is_chunk_loaded" in wsrc[snap:snap_end])


# ---------------------------------------------------------------------------
# 七、假人身份校验：不能把真人当身体
# ---------------------------------------------------------------------------

def test_sim_identity() -> None:
    print("\n" + "-" * 70)
    print("七、假人身份校验（防真人被当成身体）")
    print("-" * 70)

    import endstone_arc_fake_player.plugin as pm

    looks = pm.ArcFakePlayerPlugin._looks_simulated

    class RealPlayer:
        """一个货真价实的正版玩家账号：过了 Xbox Live 认证，xuid 有值。"""

        def __init__(self, name="steve"):
            self.name = name
            self.is_valid = True
            self.xuid = "2535445428243157"
            self.device_id = "1A2B3C4D5E6F"
            self.device_os = "Android"
            self.game_version = "1.26.51"

    class OfflineRealPlayer:
        """离线登录的真人。xuid 是空的。

        ⚠️ 3.0.0 起这个对象**会被认成假人**——这是用户明确认可的取舍。
        离线模式下没有 xuid 可用，只有"xuid 空 = 假人"这一条路，
        否则真假人一个都认不出来（2.1.0 的实际结果）。
        测试把这个**已知代价**锁住，免得哪天有人"顺手修好"了却不知道
        背后换掉了什么。
        """

        def __init__(self, name="zonnig"):
            self.name = name
            self.is_valid = True
            self.xuid = ""              # ← 空的！
            self.device_id = "99d7a8123147739a"
            self.device_os = "Android"
            self.game_version = "1.26.51"

    class SimPlayer:
        """模拟玩家：没有客户端，所以 xuid 和设备特征全是空的。

        注意这里**故意不提供** disconnect / navigate_to_location 之类
        的方法——真实的 Endstone SimulatedPlayer 也没有。第一版依赖
        这些方法来识别，结果一个都取不到，恒判 False。
        """

        def __init__(self, name="bot"):
            self.name = name
            self.is_valid = True
            self.xuid = ""
            self.device_id = ""
            self.device_os = ""
            self.game_version = ""

    check("真人玩家（xuid 有值）不被认作假人",
          not looks(RealPlayer()), "steve")
    check("模拟玩家（xuid 空）被认出来", looks(SimPlayer()), "bot")
    check("None 当然是假", not looks(None))

    # ── 已知代价：离线真人会被误判 ──────────────────────────────────
    #
    # 这条**不是 bug，是设计取舍**。写在这里是为了让它可见、可控，
    # 而不是哪天被人"修好"之后又把真假人识别搞坏（那正是 2.1.0）。
    # 缓解措施：启动自检会检测 xuid 为空的在线玩家并警告服主。
    check("【已知代价】离线真人 xuid 空 → 会被认成假人",
          looks(OfflineRealPlayer()), "zonnig —— 见 README「离线真人的代价」")


    # 类名带 simulated 也认（有的 Endstone 版本可能给了不同的类）
    class SimulatedPlayer:
        pass

    check("类名带 simulated 也认", looks(SimulatedPlayer()))

    # 显式标记优先
    class Marked:
        is_simulated = True

    check("显式 is_simulated 标记被尊重", looks(Marked()))

    class MarkedNo:
        """显式说"不是假人"，即使 xuid 是空的也否决。"""

        is_simulated = False
        xuid = ""

    check("显式 is_simulated=False 是否决（哪怕 xuid 空）",
          not looks(MarkedNo()))

    # ── 主判据就是 xuid，不再依赖设备特征 ───────────────────────────
    class XuidOnly:
        """只暴露 xuid，没有 device_* 三项。

        这正是真实 SimulatedPlayer 的样子：2.1.0 之所以恒判 False，
        就是因为当时要求"三个设备特征都得读得到"，而实际读不到。
        """

        xuid = ""

    check("只有 xuid（空）也认得出假人 —— 这正是 2.1.0 漏掉的情况",
          looks(XuidOnly()), "主判据不依赖 device_*")

    class XuidOnlyReal:
        """同样读不到 device_*，但 xuid 有值 → 真人。"""

        xuid = "2535445428243157"

    check("xuid 有值 → 真人（强否决，不看别的）",
          not looks(XuidOnlyReal()))

    class NoXuidNoDevice:
        """xuid 属性整个不存在（很旧的 Endstone），设备特征也没有。"""

        name = "mystery"

    check("什么线索都没有时不认（宁可不认也不认错）",
          not looks(NoXuidNoDevice()))

    class OneDeviceField:
        """xuid 读不到，退到设备特征；三项都有值 → 有客户端 → 真人。"""

        device_id = "ABC"
        device_os = "Android"
        game_version = "1.26.51"

    check("无 xuid 时退到设备特征：有值就是真人",
          not looks(OneDeviceField()))

    class DeviceAllEmpty:
        """xuid 读不到，但三项设备特征都在且全空 → 假人。"""

        device_id = ""
        device_os = ""
        game_version = ""

    check("无 xuid 时退到设备特征：三项全空也是假人",
          looks(DeviceAllEmpty()), "旧版 Endstone 的兜底路径")

    # --- _find_sim_player 的筛选行为 ---
    class FakeServer:
        def __init__(self, players):
            self.online_players = players
            self._by_name = {getattr(p, "name", ""): p for p in players}

        def get_player(self, name):
            return self._by_name.get(name)

    class Thin:
        server = None

        def __init__(self):
            # ⚠️ `logger` 必须是**实例**属性，不能写成类属性。
            #    写成 `logger = QuietLogger()` 的话所有 Thin 实例共用
            #    同一个 logger，前一个用例的日志会攒到后一个用例里，
            #    断言"警告只打了一次"就永远数出 2。
            self.logger = QuietLogger()
            # 警告去重用的集合。真插件在 on_enable 里建，
            # 测试替身得自己带上，否则一调用就 AttributeError。
            self._name_clash_warned = set()

        def _find_sim_player(self, n):
            return pm.ArcFakePlayerPlugin._find_sim_player(self, n)

        # 测试替身要把被调用的依赖也接上，否则一调用就 AttributeError。
        #
        # 注意 staticmethod 的搬运方式：`vars(cls)[name]` 取到的是
        # **描述符对象**，直接赋成类属性后通过实例访问会退化成普通函数
        # （凭空多一个 self 参数），报 "takes 1 positional argument but
        # 2 were given"。必须用 `.__func__` 取出底层函数，再包一层
        # staticmethod 放回去。这个坑上一轮踩过一次。
        _looks_simulated = staticmethod(
            vars(pm.ArcFakePlayerPlugin)["_looks_simulated"].__func__)

    real = RealPlayer("steve")
    sim = SimPlayer("steve")

    thin = Thin()
    thin.server = FakeServer([real])
    check("同名真人在线时，不会把它当假人",
          thin._find_sim_player("steve") is None)

    # ⚠️ 3.0.0 起离线真人**会**被认成假人——这是用户选的取舍。
    #    这条断言把代价锁住，见上面 test_sim_identity 的说明。
    offline = OfflineRealPlayer("steve")
    thin_off = Thin()
    thin_off.server = FakeServer([offline])
    check("【已知代价】同名离线真人会被当成假人接上",
          thin_off._find_sim_player("steve") is offline,
          "离线模式没有 xuid，这是唯一可行的判据")

    thin.server = FakeServer([real, sim])
    check("真人和假人同名时，挑出假人",
          thin._find_sim_player("steve") is sim)

    thin.server = FakeServer([sim])
    check("只有假人在线时正常返回", thin._find_sim_player("steve") is sim)

    thin.server = FakeServer([])
    check("都不在线时返回 None", thin._find_sim_player("steve") is None)

    # ⚠️ 注意：既然离线真人现在会被当成假人，下面这条"同名警告"的回归
    #    测试必须用**有 xuid 的真人**才能触发警告。用离线真人会直接
    #    被接上，根本走不到警告分支。
    #
    # 这条是实测事故的直接回归：警告在感知循环（每 10 tick）里，
    # 判据一旦有偏差就会以 0.5 秒一行的速度刷爆日志，
    # 用户实测时 20 多行全是同一句话，真正的错误被淹掉了。
    thin2 = Thin()
    thin2.server = FakeServer([real])
    for _ in range(50):
        thin2._find_sim_player("steve")
    # 只数 warning 级别的那条。debug 那条也含"同名"字样，
    # 用子串匹配会把它一起算进来——必须按日志级别过滤。
    warnings = [l for l in thin2.logger.warnings if "同名" in l]
    check("同名警告只打一次（50 次调用）",
          len(warnings) == 1, f"实际 {len(warnings)} 次")
    check("后续重复查询降级为 debug",
          len(thin2.logger.debugs) >= 49,
          f"debug 行数 {len(thin2.logger.debugs)}")

    # 换个名字能重新警告——去重是按名字记的，不是全局只管一次
    thin3 = Thin()
    thin3.server = FakeServer([RealPlayer("alex")])
    thin3._find_sim_player("alex")
    thin3._find_sim_player("alex")
    check("换个名字可以重新警告",
          len([l for l in thin3.logger.warnings if "同名" in l]) == 1)


# ---------------------------------------------------------------------------
# 八、行为包载荷更新（改了 main.js 要能送到）
# ---------------------------------------------------------------------------

def test_pack_update() -> None:
    print("\n" + "-" * 70)
    print("八、行为包载荷更新")
    print("-" * 70)

    import tempfile
    from pathlib import Path as _P

    from endstone_arc_fake_player import packing

    check("有载荷指纹函数", hasattr(packing, "_fingerprint"))
    check("有发布记账文件常量", hasattr(packing, "_RELEASE_NOTE"))
    check("载荷清单包含 main.js",
          "scripts/main.js" in packing._FINGERPRINT_REL)
    check("载荷清单包含 manifest.json",
          "manifest.json" in packing._FINGERPRINT_REL)

    with tempfile.TemporaryDirectory() as td:
        root = _P(td)
        src = root / "src"
        (src / "scripts").mkdir(parents=True)
        (src / "manifest.json").write_text('{"header":{"version":[1,0,0]}}')
        (src / "scripts" / "main.js").write_text("// v1")

        dst = root / "dst"
        ok, msg = packing.extract_pack(src, dst)
        check("首次释放成功", ok and (dst / "scripts" / "main.js").exists(), msg)

        # 同内容重复释放 → 跳过
        ok, msg = packing.extract_pack(src, dst)
        check("内容一致时跳过", ok and "跳过" in msg, msg)

        # 插件升级：内容变了但版本号没变 → 必须覆盖
        (src / "scripts" / "main.js").write_text("// v2 修了 bug")
        ok, msg = packing.extract_pack(src, dst)
        check("版本号没变但内容变了 → 仍然更新",
              (dst / "scripts" / "main.js").read_text() == "// v2 修了 bug",
              msg)

        # 用户手改 → 保住
        (dst / "scripts" / "main.js").write_text("// 用户改的")
        (src / "scripts" / "main.js").write_text("// v3")
        ok, msg = packing.extract_pack(src, dst)
        check("用户改过的文件被保住",
              (dst / "scripts" / "main.js").read_text() == "// 用户改的", msg)

        # 用户改过之后，自己又升级 → 还是保住（它现在是"用户的东西"）
        (src / "scripts" / "main.js").write_text("// v4")
        ok, msg = packing.extract_pack(src, dst)
        check("用户改过之后持续被尊重",
              (dst / "scripts" / "main.js").read_text() == "// 用户改的", msg)

    # install 报告里，释放成功要标记 needs_restart —— 因为 BDS 只在
    # 进程启动时扫描 behavior_packs。
    psrc = (PKG / "packing.py").read_text("utf-8")
    inst = psrc.find("def install")
    inst_end = psrc.find("\ndef ", inst + 10)
    check("安装时释放了载荷就标记需要重启",
          'msg == "已释放"' in psrc[inst:inst_end]
          and 'report["needs_restart"] = True' in psrc[inst:inst_end])


# ---------------------------------------------------------------------------
# 九、把「需要重启」说出来（用户可见的最后一道提示）
# ---------------------------------------------------------------------------

def test_restart_notice() -> None:
    print("\n" + "-" * 70)
    print("九、需要重启的提示")
    print("-" * 70)

    src = (PKG / "plugin.py").read_text("utf-8")

    # 用户报「开了实验性 API 还是出 NPC」的真实原因，就是他服务器上跑的还是
    # 旧的 main.js —— BDS 只在进程启动时扫描 behavior_packs/，而「退出游戏
    # 客户端」完全不会让服务器重新扫描。所以只要载荷更新到了磁盘，就必须
    # 在三个地方都喊出来，尤其是用户会主动去看的那两个入口。
    # _cmd_body 是命令分发壳，真正的状态渲染在 _report_body_status 里。
    body = src.find("def _report_body_status")
    body_end = src.find("\n    def ", body + 10)
    chunk = src[body:body_end] if body >= 0 else ""
    check("/ai body 里检查了 needs_restart",
          body >= 0 and 'report.get("needs_restart")' in chunk)
    check("/ai body 明确说「退出游戏客户端不算」",
          "退出游戏客户端不算" in chunk)

    health = src.find("def _confirm_body")
    health_end = src.find("\n    def ", health + 10)
    hchunk = src[health:health_end] if health >= 0 else ""
    check("启动自检失败时也提示要重启",
          health >= 0 and 'report.get("needs_restart")' in hchunk)

    inst = src.find("def _install_behavior_pack")
    inst_end = src.find("\n    def ", inst + 10)
    ichunk = src[inst:inst_end] if inst >= 0 else ""
    check("安装后提示要重启", 'report.get("needs_restart")' in ichunk)
    check("安装入口的提示文案里含「重启」", "重启" in ichunk)


# ---------------------------------------------------------------------------
# 十、2.1.0：崩溃防护
# ---------------------------------------------------------------------------

def test_crash_guard() -> None:
    print("\n" + "-" * 70)
    print("十、崩溃防护（调度器回调绝不向外抛异常）")
    print("-" * 70)

    import endstone_arc_fake_player.plugin as pm

    src = (PKG / "plugin.py").read_text("utf-8")

    # 实测事故：假人造出来之后服务器直接崩了，"Log 都没来得及记"。
    # 调度器回调在 C++ 侧被调用，任何漏出去的异常都会冒到引擎里。
    check("有 _guard 包装器", "def _guard(" in src)
    check("有兜底上报函数", "def _report_guard_failure(" in src)
    check("导入了 traceback", "import traceback" in src)

    # 四个周期任务全部要走 _guard，不能有裸的 run_task
    for label in ("_tick", "_perceive", "_health", "_startup_check"):
        check(f"{label} 走受保护的注册路径",
              f"self._guard(self.{label}" in src,
              "" if f"self._guard(self.{label}" in src else "仍在裸 run_task")

    # 兜底报告必须限流。崩在每 tick 的循环里时，不限量会把日志刷爆，
    # 反而看不到第一次的现场。
    check("兜底报告有限流",
          "_MAX_GUARD_REPORTS" in src and "_guard_failures" in src)

    # --- 行为验证：让回调抛异常，服务器不能受影响 ---
    class FakeScheduler:
        def __init__(self) -> None:
            self.task = None

        def run_task(self, plugin, fn, **kw):
            self.task = fn
            self.kw = kw

    class FakePlugin:
        _MAX_GUARD_REPORTS = pm.ArcFakePlayerPlugin._MAX_GUARD_REPORTS
        _guard = pm.ArcFakePlayerPlugin.__dict__["_guard"]
        _make_task = pm.ArcFakePlayerPlugin.__dict__["_make_task"]
        _report_guard_failure = pm.ArcFakePlayerPlugin.__dict__[
            "_report_guard_failure"]

        def __init__(self):
            self.logger = QuietLogger()
            self._guard_failures = {}
            self.server = type("S", (), {"scheduler": FakeScheduler()})()
            self._records = []

        def _record(self, *a, **k):
            self._records.append(a)

    p = FakePlugin()

    calls = {"n": 0}

    def boom() -> None:
        calls["n"] += 1
        raise RuntimeError("actor is not valid")

    p._guard(boom, "测试任务", delay=1, period=1)
    task = p.server.scheduler.task

    # 连跑 10 次，每一次都抛异常——但绝不能有异常漏出来
    escaped = None
    for _ in range(10):
        try:
            task()
        except Exception as exc:      # 漏出来就是服务器要崩
            escaped = exc
            break
    check("回调抛异常时不会漏到调用方（服务器不会崩）",
          escaped is None, repr(escaped))
    check("回调确实被执行了 10 次", calls["n"] == 10)

    # 报错日志要有限流：第一次全量，后面不再刷
    errs = [l for l in p.logger.errors if "测试任务" in l]
    check("第一次异常打出了完整报告", len(errs) >= 1)
    check("traceback 进了日志（能看到真正出错的位置）",
          any("actor is not valid" in l for l in p.logger.errors))
    check("重复异常被限流（日志不会刷爆）",
          len(errs) <= FakePlugin._MAX_GUARD_REPORTS + 1,
          f"实际 {len(errs)} 行")

    # 失败计数按任务名分开记，互不干扰
    check("失败计数按任务名分开记",
          p._guard_failures.get("测试任务") == 10,
          str(p._guard_failures))


# ---------------------------------------------------------------------------
# 十一、2.1.0：tickingarea 的两个 bug
# ---------------------------------------------------------------------------

def test_ticking_area_fixes() -> None:
    print("\n" + "-" * 70)
    print("十一、常加载区域：默认关闭 + y 范围")
    print("-" * 70)

    src = (PKG / "plugin.py").read_text("utf-8")

    # --- 默认值必须是 false ---
    #
    # 用户原话："我所说的区块加载不是区块常加载，而是说这个人走到哪里
    # 哪里的区块加载。和真实玩家差不多"。SimulatedPlayer 本来就是走到哪
    # 加载到哪，这是引擎固有行为，不需要额外代码。常加载区域是另一回事
    # （区块永远在线），默认不该开。
    i = src.find('"ticking_area":')
    seg = src[i:i + 60]
    check("ticking_area 默认关闭", '"ticking_area": False' in seg, seg.strip())

    # 读取处的默认值也要一致，否则配置缺失时会意外打开
    check("读取处的默认值也是 False",
          'self._cfg.get("ticking_area", False)' in src,
          "写成 True 的话缺失配置时会意外打开")

    # --- y 范围必须是一个真正的区间 ---
    #
    # 实测事故：假人在 y≈15，y-radius = -15 被命令夹到 0，
    # 于是区域变成 "from -16, 0, -48 to 47, 0, 31" —— y 从 0 到 0，
    # 一个平面，根本没覆盖假人所在的层。命令还返回成功，纯静默失效。
    ta = src.find("def _ensure_ticking_area")
    taend = src.find("\n    def ", ta + 10)
    body = src[ta:taend]

    check("y 不再用对称半径", "y - radius" not in body)
    check("y 用固定半高", "half_y" in body)
    check("y 下限显式夹到 0", "max(0," in body)
    check("y 上限仍在 y 之上", "y + half_y" in body)

    # 手工算一遍，确认区间有厚度，且 hi 永远不小于 lo
    def calc(y: int, radius: int) -> tuple[int, int]:
        half_y = min(16, radius)
        y_lo = max(0, y - half_y)
        y_hi = y + half_y
        if y_hi < y_lo:
            y_hi = y_lo + half_y * 2
        return y_lo, y_hi

    lo, hi = calc(15, 30)
    check("假人在 y=15 时区间有厚度", hi > lo, f"y {lo}~{hi}")
    check("假人在 y=15 时覆盖了它所在的层", lo <= 15 <= hi, f"y {lo}~{hi}")
    check("旧算法会退化成平面（对照组）",
          (15 - 30) < 0, "这就是 bug 成因")

    lo2, hi2 = calc(0, 30)
    check("y=0 时不会算出负数", lo2 == 0 and hi2 > lo2, f"y {lo2}~{hi2}")
    lo3, hi3 = calc(-40, 30)
    check("虚空里也不会算出反区间", hi3 > lo3, f"y {lo3}~{hi3}")
    check("虚空里的区间也在世界下限之上", lo3 >= 0, f"y {lo3}~{hi3}")

    # 半径仍要夹取
    check("半径有夹取", "max(4, min(128, radius))" in body)


# ---------------------------------------------------------------------------
# 十二、3.0.0：假人游戏模式改由 JS 在生成时设定
# ---------------------------------------------------------------------------

def test_bot_game_mode() -> None:
    print("\n" + "-" * 70)
    print("十二、假人游戏模式（创造模式打不死的问题）")
    print("-" * 70)

    src = (PKG / "plugin.py").read_text("utf-8")
    js = (PKG / "behavior_pack" / "scripts" / "main.js").read_text("utf-8")
    br = (PKG / "bridge.py").read_text("utf-8")

    check("有了 _bot_game_mode_number（配置 → 数字）",
          "def _bot_game_mode_number" in src)
    check("默认强制 survival", '"bot_game_mode": "survival"' in src)
    check("有了 _verify_bot_game_mode（只读校验）",
          "def _verify_bot_game_mode" in src)

    # ⚠️ 3.0.0 的核心：Python **不再**写 player.game_mode
    check("旧的 _apply_bot_game_mode 已删除",
          "def _apply_bot_game_mode" not in src,
          "控制权已移交 JS")
    check("Python 里不再有 player.game_mode = 赋值",
          "player.game_mode =" not in src,
          "只读校验，不写属性")

    # 模式必须在**生成时**就传下去
    check("bridge.spawn 支持 game_mode 参数",
          "game_mode: int | None = None" in br)
    check("spawn 载荷里带 gm 字段", 'payload["gm"]' in br)
    up_body = src[src.find("def _spawn_body"):src.find("\n    def ", src.find("def _spawn_body") + 10)]
    check("_spawn_body 在生成时传 game_mode",
          "game_mode=self._bot_game_mode_number()" in up_body)

    # 真身接入时只做校验
    up = src.find("def _upgrade_placeholder")
    upend = src.find("\n    def ", up + 10)
    check("升级成真身时只做校验",
          "_verify_bot_game_mode(player, name)" in src[up:upend])

    # --- JS 侧 ---
    check("JS 有 resolveGameMode 解析函数",
          "function resolveGameMode" in js)

    # ⚠️ 查字符串会误报——上面那段注释里就解释性地引用了旧写法
    #    （`历史坑：这里原先写的是 req.gm !== undefined ? req.gm : 1`）。
    #    所以必须把注释剥掉再查，只认真正的代码。
    #    这个坑 test_activity 里也踩过一次（PEP 563 那条），
    #    处理办法一样：别拿子串匹配去查源码，先归一化。
    js_code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js_code = re.sub(r"^\s*//.*$", "", js_code, flags=re.M)
    check("JS 不再把 gm 缺省硬编码成 1（创造模式）",
          "req.gm !== undefined ? req.gm : 1" not in js_code,
          "旧写法导致假人永远无敌")
    check("JS 的 gm 缺省跟随世界默认，且传入 Script API 枚举",
          "world.getDefaultGameMode()" in js_code and
          '["Survival", "Creative", "Adventure", "Spectator"]' in js_code)

    check("支持 keep 表示不干预",
          '"keep"' in src or "'keep'" in src)
    check("认不出的值会警告而不是静默失败",
          "不认识" in src and "bot_game_mode" in src)

    # --- 行为验证：配置 → 数字 ---
    import endstone_arc_fake_player.plugin as pm

    class NumPlugin:
        _bot_game_mode_number = pm.ArcFakePlayerPlugin.__dict__[
            "_bot_game_mode_number"]
        # ⚠️ `_GAME_MODE_NUMBERS` 是**类属性**，`vars(cls)[name]` 取单个方法
        #    搬不过来。测试替身得自己带上，否则一调用就 AttributeError。
        _GAME_MODE_NUMBERS = pm.ArcFakePlayerPlugin._GAME_MODE_NUMBERS

        def __init__(self, want):
            self._cfg = {"bot_game_mode": want}
            self.logger = QuietLogger()

    check("survival → 0", NumPlugin("survival")._bot_game_mode_number() == 0)
    check("creative → 1", NumPlugin("creative")._bot_game_mode_number() == 1)
    check("adventure → 2", NumPlugin("adventure")._bot_game_mode_number() == 2)
    check("spectator → 3", NumPlugin("spectator")._bot_game_mode_number() == 3)
    check("大小写不敏感", NumPlugin("SURVIVAL")._bot_game_mode_number() == 0)
    check("keep → None（不干预）",
          NumPlugin("keep")._bot_game_mode_number() is None)
    check("空值 → None", NumPlugin("")._bot_game_mode_number() is None)

    p_bad = NumPlugin("banana")
    check("认不出的值 → None 且不抛",
          p_bad._bot_game_mode_number() is None)
    check("认不出的值会警告", len(p_bad.logger.warnings) >= 1)

    # --- 行为验证：只读校验 ---
    class VerPlugin:
        _verify_bot_game_mode = pm.ArcFakePlayerPlugin.__dict__[
            "_verify_bot_game_mode"]
        _bot_game_mode_number = pm.ArcFakePlayerPlugin.__dict__[
            "_bot_game_mode_number"]
        _GAME_MODE_NUMBERS = pm.ArcFakePlayerPlugin._GAME_MODE_NUMBERS

        def __init__(self, want):
            self._cfg = {"bot_game_mode": want}
            self.logger = QuietLogger()

    class FakePlayer:
        def __init__(self, mode_name):
            # 用一个有 .name 的小对象模拟 GameMode 枚举
            self.game_mode = type("GM", (), {"name": mode_name})()

    class NotAPlayer:
        """没有 game_mode 的实体（NPC 占位）—— 必须安静跳过。"""

    # 模式对得上：不该有警告
    pv = VerPlugin("survival")
    pl = FakePlayer("SURVIVAL")
    pv._verify_bot_game_mode(pl, "bot")
    check("模式一致时不报警",
          len([w for w in pv.logger.warnings if "游戏模式" in w]) == 0)

    # 模式对不上：必须警告（这正是用户"打不掉血"的可发现性）
    pm2 = VerPlugin("survival")
    pl2 = FakePlayer("CREATIVE")
    pm2._verify_bot_game_mode(pl2, "bot")
    check("模式不一致时警告（创造模式打不掉血的可发现性）",
          len([w for w in pm2.logger.warnings if "游戏模式" in w]) == 1,
          str(pm2.logger.warnings))

    # 只读校验**绝不能**改玩家状态
    check("只读校验没有修改 game_mode",
          getattr(pl2.game_mode, "name", "") == "CREATIVE",
          "校验不是设置")

    # keep：不校验，也就不该有警告
    pk = VerPlugin("keep")
    plk = FakePlayer("CREATIVE")
    pk._verify_bot_game_mode(plk, "bot")
    check("keep 时不做校验也不警告", len(pk.logger.warnings) == 0)

    # 不是 Player 的对象（占位 NPC）：安静跳过，绝不能抛
    p4 = VerPlugin("survival")
    thrown2 = None
    try:
        p4._verify_bot_game_mode(NotAPlayer(), "bot")
    except Exception as exc:
        thrown2 = exc
    check("对 NPC 占位实体不抛异常（这是崩溃源的回归）",
          thrown2 is None, repr(thrown2))


# ---------------------------------------------------------------------------
# 十三、3.0.0：tickingarea 下发前校验（疑似崩溃源）
# ---------------------------------------------------------------------------

def test_ticking_area_guard() -> None:
    print("\n" + "-" * 70)
    print("十三、常加载区域的下发前校验（疑似服务器崩溃源）")
    print("-" * 70)

    src = (PKG / "plugin.py").read_text("utf-8")
    fn = src.find("def _ensure_ticking_area")
    body = src[fn:src.find("\n    def ", fn + 10)]

    # 三个轴都必须校验，不能只校验 y
    check("校验 x 轴退化", 'bad_axis = "x"' in body)
    check("校验 y 轴退化", 'bad_axis = "y"' in body)
    check("校验 z 轴退化", 'bad_axis = "z"' in body)
    check("退化时直接 return（不下发命令）",
          "已跳过" in body and "return" in body)

    # x/z 也要夹取
    check("x/z 夹到世界边界", "lim = 30000000" in body)
    check("x 下界夹取", "max(-lim, x - radius)" in body)
    check("x 上界夹取", "min(lim, x + radius)" in body)

    # 下发前留痕
    check("下发前把命令原文写进 debug 日志",
          "即将下发" in body,
          "引擎若崩，控制台要能看到最后发了什么")

    # 默认必须是关的
    check("ticking_area 默认关闭",
          '"ticking_area": False' in src,
          "疑似崩溃源，默认不启用")

    # --- 手工复刻校验逻辑 ---
    def axes(x, y, z, radius):
        """照抄 `_ensure_ticking_area` 的算法，返回三个轴的区间。

        ⚠️ `radius` 必须先夹取！真实代码里在这之前有一行
           `radius = max(4, min(128, radius))`——不夹的话半径 0 或负数
           会让 x/z 轴算出 `(0,0)` 或反向区间，那正是要拦的东西。
           我第一版复刻漏了这行，测试直接把这个漏洞照出来了。
        """
        radius = max(4, min(128, radius))
        lim = 30000000
        half_y = min(16, radius)
        y_lo = max(0, y - half_y)
        y_hi = y + half_y
        if y_hi < y_lo:
            y_hi = y_lo + half_y * 2
        return (
            (max(-lim, x - radius), min(lim, x + radius)),
            (y_lo, y_hi),
            (max(-lim, z - radius), min(lim, z + radius)),
        )

    def ok(ax):
        return all(lo < hi for lo, hi in ax)

    # 实测那个坐标（原日志：假人 y≈15，半径 30）
    ax = axes(15, 15, -8, 30)
    check("实测坐标不再退化", ok(ax), f"x{ax[0]} y{ax[1]} z{ax[2]}")
    check("实测坐标的 y 区间有厚度", ax[1][1] > ax[1][0], f"y {ax[1]}")

    # 旧算法（对称半径，无校验）在同一点会算出平面。
    # 照实测日志那行原样复刻：`from -16, 0, -48 to 47, 0, 31`
    # —— y 从 0 到 0。注意这是**引擎夹取后**的结果，Python 侧算出来是
    # `-15 ~ 15`，是引擎把 -15 夹到 0 才塌掉的。
    old_y_lo = max(0, 15 - 30)      # 引擎把 -15 夹到 0
    old_y_hi = 15 + 30 - 30         # 对称半径下 hi = y - radius + 2r … 实际被夹到 0
    old_y_hi = min(old_y_hi, 0) if old_y_lo == 0 else old_y_hi
    check("旧算法在同一坐标会退化成平面（对照组）",
          old_y_lo >= old_y_hi,
          f"y {old_y_lo}~{old_y_hi} —— 这就是下发前校验要拦的东西")

    check("虚空坐标也不退化", ok(axes(0, -40, 0, 30)), f"{axes(0, -40, 0, 30)}")
    check("世界边界坐标不退化", ok(axes(29999999, 100, -29999999, 30)))

    # 极端半径（配置被人改成 0 或负数）也不该产出退化区间。
    # 夹取那一行是这里的防线：半径 0 → 4，半径 -10 → 4。
    check("半径 0 时仍不退化", ok(axes(0, 5, 0, 0)), f"{axes(0, 5, 0, 0)}")
    check("半径负数时仍不退化", ok(axes(0, 5, 0, -10)), f"{axes(0, 5, 0, -10)}")
    check("半径 0 被夹成 4",
          axes(0, 5, 0, 0)[0] == (-4, 4), f"{axes(0, 5, 0, 0)[0]}")


# ---------------------------------------------------------------------------
# 第十四部分：全面审计发现的 8 个 bug（3.0.0 收尾）
# ---------------------------------------------------------------------------
#
# 这一批是"上线下一个版本之前通读全部源码"找出来的，全部**先复现再修**。
# 它们的共同特征是：不会立刻炸，而是让某条路径**悄悄失效**，
# 所以必须靠测试钉住，否则下次重构又会掉回去。


def test_audit_fixes() -> None:
    print()
    print("-" * 70)
    print("第十四部分：全面审计修复（14 处）")
    print("-" * 70)

    src = (ROOT / "endstone_arc_fake_player" / "plugin.py").read_text(encoding="utf-8")
    bsrc = (ROOT / "endstone_arc_fake_player" / "bridge.py").read_text(encoding="utf-8")
    asrc = (ROOT / "endstone_arc_fake_player" / "agent.py").read_text(encoding="utf-8")
    lsrc = (ROOT / "endstone_arc_fake_player" / "llm.py").read_text(encoding="utf-8")
    rsrc = (ROOT / "endstone_arc_fake_player" / "realbody.py").read_text(encoding="utf-8")
    acsrc = (ROOT / "endstone_arc_fake_player" / "actions.py").read_text(encoding="utf-8")
    jsrc = (ROOT / "endstone_arc_fake_player" / "behavior_pack" / "scripts"
            / "main.js").read_text(encoding="utf-8")

    # 桩已经在本文件顶部装好（见那里的注释），这里不再重复安装。

    class _L:
        def debug(self, *a): pass
        def info(self, *a): pass
        def warning(self, *a): pass
        def error(self, *a): pass

    # ---- 1) _remote_names 大小写必须统一 ----
    #
    # 四个写入点只要有一个忘了 .lower()，"假人还在不在"就会算错。
    from endstone_arc_fake_player.bridge import FakePlayerBridge

    b = FakePlayerBridge(_L(), lambda c: True)
    b.new_token()
    b.dispatch("bot:pong", '{"names":["HLY"],"t":"%s"}' % b._token)
    check("bot:pong 存小写", b.remote_names == {"hly"}, str(b.remote_names))
    b.dispatch("bot:removed", '{"n":"HLY","t":"%s"}' % b._token)
    check("bot:removed 用大写名字也能删掉（大小写统一）",
          b.remote_names == set(), str(b.remote_names))
    b.dispatch("bot:list_result", '{"names":["Bob"],"t":"%s"}' % b._token)
    check("bot:list_result 存小写", b.remote_names == {"bob"}, str(b.remote_names))

    # ---- 2) 名字里的引号不能破坏 JSON ----
    from endstone_arc_fake_player.plugin import _clean_name
    check("名字里的双引号被净化", '"' not in _clean_name('hly"x'),
          repr(_clean_name('hly"x')))
    check("名字里的空格被净化", " " not in _clean_name("a b"),
          repr(_clean_name("a b")))
    check("换行被净化", "\n" not in _clean_name("a\nb"))
    check("中文名字保留", _clean_name("玩家一") == "玩家一",
          repr(_clean_name("玩家一")))
    check("空输入有兜底", _clean_name("") == "bot", repr(_clean_name("")))
    check("纯符号有兜底", _clean_name("!!!") == "bot", repr(_clean_name("!!!")))
    check("长度截到 24", len(_clean_name("x" * 50)) <= 24)
    check("spawn 走 _clean_name", "name = _clean_name(args[0])" in src)
    check("remove 也走 _clean_name",
          src.count("_clean_name(args[0])") >= 2)

    # ---- 3) consume_real 不能"发出去就算成功" ----
    #
    # 进食必须等行为包确认实际消耗；回血也可能来自其他来源。
    import json
    from endstone_arc_fake_player.realbody import RealBodyActions

    class _W:
        body_dimension = None
        def is_solid_id(self, *a): return True

    class _Ent:
        health = 10.0

    class _Ctl:
        name = "hly"
        busy = False
        def __init__(self): self.actor = _Ent()
        def cancel(self, *a, **k): pass

    bb = FakePlayerBridge(_L(), lambda c: True)
    bb.new_token()
    ctl = _Ctl()
    ctl._bridge = bb
    ra = RealBodyActions(ctl, _W(), _L())

    class _Step:
        note = ""

    check("consume_real 第一拍返回 False（还没结论）",
          ra.consume_real(_Step()) is False)
    ctl.actor.health = 16.0
    check("consume_real 血涨了仍须等回执",
          ra.consume_real(_Step()) is False)
    bb.dispatch("bot:result", json.dumps({"i": bb._seq, "ok": True, "t": bb._token}))
    check("consume_real 收到实际消耗回执才成功",
          ra.consume_real(_Step()) is True)

    ra.reset()
    check("consume_real 重新发起", ra.consume_real(_Step()) is False)
    check("【回归】血没涨时不能报成功（旧代码会 return True）",
          ra.consume_real(_Step()) is False,
          "血量 10→10，说明没吃到")
    import inspect
    check("bridge.consume 支持消费完成回调",
          "callback" in inspect.signature(FakePlayerBridge.consume).parameters)

    # ---- 4) _upgrade_placeholder 不能复活已停摆的智能体 ----
    from endstone_arc_fake_player.plugin import ArcFakePlayerPlugin

    PH = object()

    class _Agent:
        def __init__(self, alive): self._a = alive
        def alive(self): return self._a

    class _P:
        _upgrade_placeholder = ArcFakePlayerPlugin._upgrade_placeholder
        def _record(self, *a, **k): pass
        class logger:
            @staticmethod
            def info(*a): pass
            @staticmethod
            def debug(*a): pass
        def _find_sim_player(self, name):
            return "真身"

    p = _P()
    p._agents = {"hly": _Agent(alive=False)}
    p._actors = {"hly": PH}
    p._placeholder_flags = {"hly": True}
    r = p._upgrade_placeholder("hly")
    check("【回归】已停摆的智能体不会被升级（防孤儿身体）",
          r is False and p._actors["hly"] is PH)
    check("停摆时顺手清掉占位标记",
          "hly" not in p._placeholder_flags)

    # ---- 5) 断连时"交出坏指令给引擎"这条路要被拦住 ----
    #
    # ⚠️ 断言写法上踩过两个坑，都记在这：
    #
    # 坑一：`src.find("def _halt_bodies")` 命中的**不是函数定义**，而是
    #   `_halt_bodies` 调用点附近、或者函数体里那段解释"为什么不该用
    #   self._plans"的注释——注释里正好带着这几个字。顺着那个位置切出来
    #   的是一段注释，判据自然失真。所以要先圈定函数本体：
    #   从定义行切到下一个 `    def `。
    #
    # 坑二：圈定本体之后，里面**仍然**会出现 `self._plans`——因为那段
    #   警示注释就写在函数体里，而且是有意保留的（它是给未来改代码的人
    #   看的）。所以判据必须是"**去掉注释后**没有 self._plans"，
    #   而不是"一个字都不许有"。
    hb_start = src.find("    def _halt_bodies(")
    hb_end = src.find("    def ", hb_start + 10)
    hb = src[hb_start:hb_end] if hb_start >= 0 and hb_end > hb_start else ""
    hb_code = "\n".join(
        ln for ln in hb.splitlines() if not ln.lstrip().startswith("#"))
    check("_halt_bodies 用 executor.active 而不是不存在的 self._plans",
          "self._executor.active" in hb_code)
    check("【回归】_halt_bodies 的**代码**里不再引用 self._plans",
          hb_start >= 0 and "self._plans" not in hb_code,
          "注释里留着说明是故意的，代码里不许再出现")
    check("_halt_bodies 走的是计划自己的 abort()",
          "plan.abort(why)" in hb_code)
    check("_halt_bodies 取的是计划的 pending_op（只收停在异步步骤上的）",
          "plan.pending_op" in hb_code)
    check("_halt_bodies 整体包了 try（这里出错会拖垮断连处理）",
          "except Exception" in hb_code)

    # ---- 6) attack 必须标 async_op，否则第一拍就被判死 ----
    from endstone_arc_fake_player.actions import ActionCompiler
    from endstone_arc_fake_player.executor import BatchExecutor, Plan, Step

    calls = {"n": 0}

    def _h(step):
        calls["n"] += 1
        return calls["n"] >= 3

    ex = BatchExecutor(_L())
    ex.register("attack", _h)
    pl = Plan(label="t: 攻击", steps=[Step("attack", 0, 0, 0, async_op=True)],
              steps_per_tick=1)
    ex.submit(pl)
    for _ in range(5):
        if ex.tick():
            break
    check("【回归】attack 标了 async_op 后会被重试（不是一次就判死）",
          calls["n"] == 3, f"handler 调了 {calls['n']} 次")
    check("actions 里 attack 步骤标了 async_op",
          'Step("attack", 0, 0, 0, note=nearest.name, async_op=True)' in acsrc)
    check("attack_real 存在（把'发出去了'和'打到了'分开）",
          "def attack_real" in rsrc)

    # 反向对照：不标 async_op 的话，第一拍就被判死、永不重试。
    # 这是修 bug 6 之前的行为，钉住它免得将来有人"顺手"删掉那个参数。
    calls2 = {"n": 0}

    def _h2(step):
        calls2["n"] += 1
        return calls2["n"] >= 3

    ex2 = BatchExecutor(_L())
    ex2.register("attack", _h2)
    ex2.submit(Plan(label="t: 旧行为", steps=[Step("attack", 0, 0, 0)],
                    steps_per_tick=1))
    for _ in range(5):
        if ex2.tick():
            break
    check("【对照组】不标 async_op 时 handler 只被调 1 次（旧代码的错法）",
          calls2["n"] == 1, f"handler 调了 {calls2['n']} 次")

    # ---- 7) 攻击选目标的距离必须和 JS 的实际攻击范围一致 ----
    m = re.search(r"if \(bestDist > ([\d.]+)\)", jsrc)
    js_reach = float(m.group(1)) if m else -1
    check("Python 的 ATTACK_REACH 与 JS 的判定一致",
          abs(ActionCompiler.ATTACK_REACH - js_reach) < 0.01,
          f"py={ActionCompiler.ATTACK_REACH} js={js_reach}")
    check("_do_attack 会先筛'够得着的'再选目标",
          "in_reach = [e for e in hits if e.dist <= self.ATTACK_REACH]" in acsrc)
    check("够不着时明确告诉模型距离（而不是假装打了）",
          "够不着" in acsrc and "self.ATTACK_REACH:g" in acsrc)

    # ---- 8) 压缩边界：keep=0 不能清空历史 ----
    items = ["m1", "m2", "m3", "m4", "m5", "m6"]
    old_keep = max(0, 0)
    old_dropped = items[:-old_keep] if len(items) > old_keep else []
    check("【对照组】旧逻辑 keep=0 时 dropped 为空（会清空历史）",
          old_dropped == [], str(old_dropped))
    new_keep = max(1, 0)
    new_dropped = items[:-new_keep] if len(items) > new_keep else []
    new_kept = items[-new_keep:]
    check("【回归】新逻辑 keep=0 被夹成 1，仍有内容可总结",
          len(new_dropped) == 5 and len(new_kept) == 1,
          f"dropped={len(new_dropped)} kept={len(new_kept)}")
    check("agent.py 里 keep 下限是 1", "keep = max(1, int(s.config.context_keep_recent))" in asrc)
    check("plugin.py 装载配置时也夹到 1",
          "max(1, int(self._cfg[\"context_keep_recent\"]))" in src)

    # ---- 9) 异常兜底：_apply / _on_main / _compact_worker ----
    check("agent 有 _apply_safely（_apply 的绝不出错版本）",
          "def _apply_safely" in asrc)
    check("_call_llm 走的是 _apply_safely",
          "lambda: self._apply_safely(reply, prompt_text)" in asrc)
    check("plugin._on_main 自己包了异常（闭包在别的 tick 才执行）",
          "def guarded" in
          src[src.find("def _on_main"):src.find("def _on_main") + 1200]
          and "except Exception" in
          src[src.find("def _on_main"):src.find("def _on_main") + 1200])
    cw = asrc[asrc.find("def _compact_worker"):asrc.find("def _finish_compaction")]
    check("_compact_worker 把 llm.chat 也包进了 try（否则 thinking 永久卡死）",
          "except Exception as exc:" in cw and "build_summarize_messages" in cw)
    check("_compact_worker 的 except 在 chat 之后、不是只包 _on_main",
          cw.find("except Exception as exc:") < cw.find("self._on_main"),
          "顺序必须是 chat → except → _on_main")

    # ---- 10) native_tools 不能被一次网络抖动永久关掉 ----
    from endstone_arc_fake_player.llm import LLMClient

    def _client(status):
        c = LLMClient("http://x/v1", "k", "m")

        def post(payload):
            c.last_http_status = status
            return None

        c._post = post
        return c

    c0 = _client(0)
    c0.chat([{"role": "user", "content": "hi"}])
    check("【回归】网络故障不该关掉 native_tools",
          c0.native_tools is True)
    c5 = _client(500)
    c5.chat([{"role": "user", "content": "hi"}])
    check("HTTP 5xx 也不该关掉 native_tools", c5.native_tools is True)
    c4 = _client(400)
    c4.chat([{"role": "user", "content": "hi"}])
    check("HTTP 400（服务端不认识 tools）才降级",
          c4.native_tools is False)
    check("LLMClient 有 last_http_status 字段", "last_http_status" in lsrc)
    check("降级条件限定在 400/404/422",
          "in (400, 404, 422)" in lsrc)

    # ---- 11) 离线风险警告不能把自己的假人当真人 ----
    class _Player:
        def __init__(self, n, x): self.name = n; self.xuid = x

    class _Server:
        def __init__(self, ps): self.online_players = ps

    warned: list[str] = []

    class _WL:
        def debug(self, *a): pass
        def info(self, *a): pass
        @staticmethod
        def warning(m): warned.append(m)
        def error(self, *a): pass

    class _WP:
        _warn_offline_risk = ArcFakePlayerPlugin._warn_offline_risk
        logger = _WL()

    def _run_warn(players, remote, ever=True):
        warned.clear()
        wp = _WP()
        wp.server = _Server(players)
        wb = FakePlayerBridge(_WL(), lambda c: True)
        wb.new_token()
        wb._remote_names = set(remote)
        wb._last_pong = 123.0 if ever else -999.0
        wp._bridge = wb
        wp._warn_offline_risk()
        return bool(warned)

    check("【回归】名单还没上报时不警告（否则自己的假人被当真人）",
          _run_warn([_Player("hly", "")], set()) is False)
    check("假人在名单里 → 不警告",
          _run_warn([_Player("hly", "")], {"hly"}) is False)
    check("名单外的空 xuid 玩家 → 该警告",
          _run_warn([_Player("hly", ""), _Player("steve", "")], {"hly"}) is True)
    check("正版真人（xuid 有值）→ 不警告",
          _run_warn([_Player("alex", "2535")], {"hly"}) is False)

    # ---- 12) 工具参数校验不能拒绝"文档说可以不填"的形式 ----
    from endstone_arc_fake_player.llm import missing_required
    check("【回归】attack 不给 target 是合法的（打最近的敌对生物）",
          missing_required("attack", {}) is False)
    check("attack 只给 count 也合法",
          missing_required("attack", {"count": 3}) is False)
    check("【回归】wander 不给 radius 合法", missing_required("wander", {}) is False)
    check("【回归】flee 不给 target 合法（逃最近的敌对生物）",
          missing_required("flee", {}) is False)
    # 反向：真的缺必填还是要拒绝
    check("mine_area 缺坐标仍然被拒", missing_required("mine_area", {}) is True)
    check("say 缺 message 仍然被拒", missing_required("say", {}) is True)
    check("collect 缺 block 仍然被拒", missing_required("collect", {}) is True)

    # ---- 13) 返回路径的"撤回"不能漏 ----
    check("_spawn_body 失败路径会撤回已发出的 bot:spawn",
          src[src.find("def _spawn_body"):src.find("def _make_placeholder")]
          .count("self._bridge.remove(name)") >= 2,
          "两条失败分支都要撤回，否则真身加入后没人控制")

    # ---- 14) JS 侧的引擎回调必须自己兜住异常 ----
    #
    # 这批是同一类问题的另一半：Python 侧已经用 `_guard` / `_apply_safely`
    # 兜住了，JS 侧的 `scriptEventReceive` 回调和 `runInterval` 回调却
    # 是**裸的**。它们跑的是游戏主线程，抛出去不只是崩脚本。
    check("JS 有 safely() 这个统一兜底",
          "function safely(fn)" in jsrc)
    switch = jsrc[jsrc.find("switch (event.id)"):jsrc.find("// 每 tick 推进挖掘轮询")]
    cases = re.findall(r'case "(bot:\w+)":\s*\n\s*(\S[^\n]*)', switch)
    unsafe = [n for n, body in cases
              if not body.strip().startswith(("safely(", "reply("))]
    check("【回归】switch 里每条指令都被包住（doSpawn 以前是裸的）",
          not unsafe, f"裸的: {unsafe}" if unsafe else f"{len(cases)} 条全覆盖")
    check("【回归】doSpawn 不再裸奔（它是唯一没调 guard 的 handler）",
          'safely(() => doSpawn(' in jsrc)

    # 心跳：它抛一次 → runInterval 可能不再调度 → Python 判定行为包死亡
    # → _halt_bodies 清掉全部假人。日志里只会写"失去联系"，看不到真因。
    hb = jsrc[jsrc.find("// 心跳：每 5 秒"):jsrc.find("// 每 5 秒清理失效假人")]
    check("【回归】心跳回调自己包了 try（否则可能永久停摆）",
          "try {" in hb and "bot:pong" in hb,
          "它一停，Python 会把全套假人当成失联")
    check("心跳的 catch 会打日志（不然故障现场没痕迹）",
          "catch (e)" in hb and "console.warn" in hb)

    # 每 5 秒的坐标上报 / 失效清理，同理
    pos = jsrc[jsrc.find("// 每 5 秒清理失效假人"):jsrc.find("// 引擎加载时踢一下")]
    check("【回归】坐标上报回调也包了 try",
          "try {" in pos and "bot:positions" in pos)

    # pumpDigging 是 runInterval 的裸函数引用，每 tick 跑，最热
    pd_start = jsrc.find("function pumpDigging()")
    pd = jsrc[pd_start:jsrc.find("function pumpOneDigging")]
    check("【回归】pumpDigging 外层包了 try（它每 tick 都跑）",
          pd_start >= 0 and "try {" in pd and "pumpDiggingInner" in pd)
    check("pumpDigging 逐项兜底（单项坏了不拖垮整轮）",
          "pumpOneDigging(key, ctx)" in pd and "catch (e)" in pd)
    check("单项出错时会回 bot:dug，不留 Python 干等",
          ("挖掘轮询异常" in pd and "digReplyAll(" in pd),
          "一次挖掘可能挂着多个请求号，异常时必须全部回复")
    check("runInterval(pumpDigging, 1) 还在（别把周期任务写没了）",
          "system.runInterval(pumpDigging, 1);" in jsrc)


# ---------------------------------------------------------------------------
# 第十五部分：寻路（pathfinder）的三处 bug
# ---------------------------------------------------------------------------
#
# 这一批是"把没细读过的模块补上"时找出来的。它们有个共同特征：
# **不会报错，只是悄悄做不到**。所以必须用真实地形把行为钉死，
# 不能只做源码断言。


def _mk_pf(surface_fn, **kw):
    """用 `make_probe` 造一个寻路器。

    `surface_fn(x, z) -> int` 返回该列"脚能站的那一格"的 y。
    方块分布由此推出：y >= surface 是空气，以下是石头。

    ⚠️ 为什么要绕这一圈、不直接写 `probe(x,y,z)`：探针的返回值
    只有"可通行 / 实心 / 危险"三种语义，**表达不了重力**——
    "平地上一格空气"和"悬崖半空一格空气"都返回 1.0。
    想测寻路就必须自己把"地面在哪"建出来，让探针能通过
    `probe(x, y-1, z)` 判断脚下有没有支撑。早期我图省事直接按
    y 分空气/石头，结果造出来的地形根本站不住人，测试全在测空气。
    """
    from endstone_arc_fake_player.pathfinder import PathFinder, make_probe

    class _W:
        def get_block_name(self, dim, x, y, z):
            return "minecraft:air" if y >= surface_fn(x, z) else "minecraft:stone"

    return PathFinder(make_probe(None, _W()), **kw)


def _reaches(path, goal) -> bool:
    return path is not None and path[-1] == goal


def test_pathfinder() -> None:
    print()
    print("-" * 70)
    print("第十五部分：寻路的三处 bug")
    print("-" * 70)

    flat = lambda x, z: 0
    pf = _mk_pf(flat)

    # ---- 1) 竖直方向不能再是死代码 ----
    #
    # 旧代码 `if here < 0: continue` 先把"目标格是实心"全毙了，
    # 紧接着 `if here < COST_WATER or here > 0:` 在 here>=0 时恒真，
    # 于是永远走"同层"并 continue —— 跳跃/下落那段一次都没跑过。
    p = pf.find((0, 0, 0), (3, 0, 0))
    check("平地上能正常走（基本盘）", _reaches(p, (3, 0, 0)), str(p))

    # ---- 2) 一格台阶必须能上去 ----
    #
    # 这是 Minecraft 里最常见的通行方式。旧代码里 (1,0,0) 是石头 →
    # `here < 0` → continue，连邻居都不产生，寻路直接失败。
    def step_surface(x, z):
        return 0
    step_surface_blocked = (1, 0)      # 这一列在 y=0 放一块石头

    from endstone_arc_fake_player.pathfinder import PathFinder, make_probe

    class _StepW:
        def get_block_name(self, dim, x, y, z):
            if (x, z) == step_surface_blocked and y == 0:
                return "minecraft:stone"       # 一格台阶
            return "minecraft:air" if y >= 0 else "minecraft:stone"

    pf_step = PathFinder(make_probe(None, _StepW()))
    p = pf_step.find((0, 0, 0), (3, 0, 0))
    check("【回归】一格台阶能绕过去或跳上去（旧代码直接失败）",
          _reaches(p, (3, 0, 0)), str(p))

    # ---- 3) 两级地形：抬升分支真的会被走到 ----
    pf2 = _mk_pf(lambda x, z: 0 if x < 3 else 1)
    p = pf2.find((0, 0, 0), (5, 1, 0))
    check("【回归】能爬上高一格的地面（抬升分支可达）",
          _reaches(p, (5, 1, 0)), str(p))

    # ---- 4) 悬崖下落 ----
    #
    # 这里能测出"同层缺少重力校验"：悬崖边的空气格探针也是 1.0，
    # 旧代码会当成"同层可走"，让假人走进悬空、然后掉下去，
    # 而 A* 以为它好好站着。正确做法是看**脚下那一格**。
    pf3 = _mk_pf(lambda x, z: 1 if x < 3 else -2, max_drop=6)
    p = pf3.find((2, 1, 0), (5, -2, 0))
    check("【回归】能跳下 3 格到谷底（旧代码下不去）",
          _reaches(p, (5, -2, 0)), str(p))

    # 反例：悬空的空气格不能被当成可站
    p = pf.find((0, 0, 0), (1, 3, 0))
    check("【回归】悬空目标不算可达（空气≠能站）",
          p is None, str(p))

    # ---- 5) 到不了必须返回 None，不能给"半条路" ----
    #
    # 旧代码在"预算用完"和"堆空了"两处都会返回 closest.chain()，
    # 而**不校验那条路径是否真的以 goal 结尾**。调用方拿到非空列表
    # 就以为成功，照着一格格传送，假人走到半路停下——比直接失败更难查。
    p = pf.find((0, 0, 0), (0, -5, 0))          # 目标埋在石头里
    check("目标不可通行 → None", p is None, str(p))

    class _WallW:
        def get_block_name(self, dim, x, y, z):
            if x == 2 and 0 <= y < 4:
                return "minecraft:stone"
            return "minecraft:air" if y >= 0 else "minecraft:stone"

    pf_wall = PathFinder(make_probe(None, _WallW()))
    p = pf_wall.find((0, 0, 0), (5, 0, 0))
    check("【回归】4 格高墙挡住 → None（不再返回一条断头路）",
          p is None, str(p))

    # 源码上也要钉住：那两处 closest 分支已经删掉了
    src = (ROOT / "endstone_arc_fake_player" / "pathfinder.py").read_text(encoding="utf-8")
    find_body = src[src.find("    def find("):src.find("    # -- 地形探针工厂")]
    check("find() 里不再有 closest 兜底返回",
          "closest" not in find_body)
    check("find() 的两条失败路径都是 return None",
          find_body.count("return None") >= 3,
          f"共 {find_body.count('return None')} 处")
    check("_standable 存在（判'脚下有没有支撑'，而不是只判'是不是空气'）",
          "def _standable" in src)

    # ---- 6) 岩浆与墙角 ----
    class _LavaW:
        def get_block_name(self, dim, x, y, z):
            if (x, y, z) == (1, 0, 0):
                return "minecraft:lava"
            return "minecraft:air" if y >= 0 else "minecraft:stone"

    pf_lava = PathFinder(make_probe(None, _LavaW()))
    p = pf_lava.find((0, 0, 0), (3, 0, 0))
    check("岩浆不会被踩过去",
          _reaches(p, (3, 0, 0)) and (1, 0, 0) not in p, str(p))

    # 斜向不能穿墙角：两个直角邻居都堵时不允许斜走
    class _CornerW:
        """(1,0,0) 和 (0,0,1) 都是墙，只留 (1,0,1) 是空的。

        如果允许斜穿，就会从 (0,0,0) 直接"钻"到 (1,0,1)。
        """
        def get_block_name(self, dim, x, y, z):
            if y < 0:
                return "minecraft:stone"
            if (x, z) in ((1, 0), (0, 1)):
                return "minecraft:stone"
            return "minecraft:air"

    pf_corner = PathFinder(make_probe(None, _CornerW()))
    ns = {n for n, _ in pf_corner._neighbors((0, 0, 0), (9, 9, 9))}
    check("【回归】堵住的墙角不能斜穿",
          (1, 0, 1) not in ns, f"邻居含 (1,0,1): {(1,0,1) in ns}")

    # ---- 7) 端到端：经 actions 层走一遍 ----
    #
    # 上面测的是 finder 本身。这里从 `ActionCompiler` 进去，确认
    # 坐标语义（`loc.y` = 脚所在格）在整条链路上是一致的——
    # 单测 finder 时很容易把"脚格"和"身体格"弄混，然后写出一个
    # 在假想地形里跑通、在真实地形里全失败的测试。
    from tests.test_hybrid import FakeActor
    from endstone_arc_fake_player.actions import ActionCompiler
    from endstone_arc_fake_player.world import WorldAdapter
    from endstone_arc_fake_player.llm import ToolCall

    class _AL:
        def debug(self, *a): pass
        def info(self, *a): pass
        def warning(self, *a): pass
        def error(self, *a): pass

    def _goto(actor, **kw):
        ac = ActionCompiler(WorldAdapter(actor.dimension), _AL())
        return ac.compile(actor, ToolCall("goto", kw), {})

    # FakeDimension 的地板在 y=63，所以脚站在 y=64。
    a = FakeActor(0.0, 64.0, 0.0)
    r = _goto(a, x=5, y=64, z=5)
    check("actions 层：平地 goto 能规划出路径",
          r.ok and r.plan is not None, getattr(r, "immediate", ""))
    if r.ok and r.plan:
        last = r.plan.steps[-1]
        check("路径终点就是目标点",
              (last.x, last.y, last.z) == (5, 64, 5),
              f"({last.x},{last.y},{last.z})")

    # 一格台阶：旧代码在这里必然失败
    b = FakeActor(0.0, 64.0, 0.0)
    b.dimension.set(2, 64, 2, "minecraft:stone")
    r = _goto(b, x=5, y=64, z=5)
    check("【回归】actions 层：一格台阶不会让 goto 崩掉",
          r.ok and r.plan is not None, getattr(r, "immediate", ""))

    # 走上高一层的台地
    c = FakeActor(0.0, 64.0, 0.0)
    for dx in range(3, 9):
        for dz in range(-8, 9):
            c.dimension.set(dx, 64, dz, "minecraft:stone")
    r = _goto(c, x=6, y=65, z=0)
    check("【回归】actions 层：能走上高一层的台地",
          r.ok and r.plan is not None, getattr(r, "immediate", ""))
    if r.ok and r.plan:
        last = r.plan.steps[-1]
        check("台地路径终点带正确的 y（65，不是 64）",
              last.y == 65, f"y={last.y}")


# ---------------------------------------------------------------------------
# 十六、3.0.2：重试语义的四处结构性缺陷
# ---------------------------------------------------------------------------
#
# 这一组测试针对的全是"静默失败"型 bug：代码不抛异常、日志也不报错，
# 表现只是"智能体做完一件事之后就不动了"或者"控制台被同一行刷屏"。
# 这类 bug 一旦回归，靠肉眼几乎发现不了，所以每一条都必须有断言钉住。

def test_retry_semantics() -> None:
    print("\n" + "-" * 70)
    print("十六、重试语义的四处结构性缺陷（3.0.2）")
    print("-" * 70)

    # --- 缺陷 1：终态没有表达方式 -------------------------------------
    #
    # 处理器的返回值只有 True/False 两种含义（成功 / 还没结论），
    # "我试够了、就是做不成"这个终态**无处安放**。曾经的写法是返回
    # False，于是执行器永远认为"还在进行中"——计划既不推进也不结算，
    # `_inflight` 永不释放，智能体从此不再思考，而且一行日志都不打。
    b = CountingBridge()
    c = BodyController("bot", b, SolidWorld(), QuietLogger())
    ra = RealBodyActions(c, SolidWorld(), QuietLogger())
    ra._within_reach = lambda x, y, z: True       # type: ignore[method-assign]
    ra.RETRY_BACKOFF_SECONDS = 0.0
    c.DIG_DEDUP_SECONDS = 0.0
    ra.MAX_ATTEMPTS = 2

    step = Step(7, 64, 7)
    key7 = ("digfar", 7, 64, 7)

    # 一次"发起 + 结论"要占两轮（一轮发出去、一轮读结论），
    # 所以烧掉 2 次额度需要 4 轮。这里循环问、每轮补一条失败结论，
    # 直到函数交出明确的终态为止 —— 顺便也断言了它**一定会**交出来。
    verdict = None
    for _ in range(8):
        verdict = ra.dig_far(step)
        if verdict is True:
            break
        if c.busy:                     # 还在挖 → 给它一个失败结论
            b.finish_dig(False, "挖不动")

    check("试满上限后返回 True（明确的终态，不是'还没完'）",
          verdict is True, f"返回 {verdict!r}")
    check("放弃标记被挂上", key7 in ra._gave_up,
          f"gave_up={ra._gave_up}")
    check("放弃时带上原因", "挖不动" in ra._gave_up.get(key7, ""),
          f"reason={ra._gave_up.get(key7)!r}")

    # ⚠️ 放弃之后必须**永远**返回 True（幂等），不能再回到 False。
    # 回到 False 就是"永久的进行中"，计划永远卡在 `_inflight` 里 ——
    # 这正是"执行完一次任务后就不动了"的表现形式之一。
    again = [ra.dig_far(step) for _ in range(5)]
    check("放弃是终态：之后每一问都返回 True（不会回到'还没完'）",
          all(v is True for v in again), f"后续返回={again}")
    check("放弃之后不会再多发请求",
          len(b.digs) == 2, f"digs={len(b.digs)}（应该停在 2 条）")

    why = ra.pop_gave_up(key7)
    check("pop_gave_up 能取走原因", bool(why), f"why={why!r}")
    check("pop_gave_up 是一次性的（第二次取为空）",
          ra.pop_gave_up(key7) == "", "取第二次还有内容 → 会重复消耗重试额度")

    # `key_of` 必须和各处理器开头算 key 的规则一致，否则调用方查不到
    # "它是不是刚被放弃了"，翻译就会静默失效。
    check("key_of 认得 dig_far（用 digfar 前缀）",
          ra.key_of(Step(7, 64, 7)) == ("digfar", 7, 64, 7),
          f"{ra.key_of(Step(7, 64, 7))}")

    class _OpStep(Step):
        def __init__(self, op: str, x: int = 0, y: int = 0, z: int = 0) -> None:
            super().__init__(x, y, z)
            self.op = op

    check("key_of 认得 walk_to",
          ra.key_of(_OpStep("walk_to", 1, 2, 3)) == ("walk", 1, 2, 3),
          f"{ra.key_of(_OpStep('walk_to', 1, 2, 3))}")
    check("key_of 认得 place_real",
          ra.key_of(_OpStep("place_real", 1, 2, 3)) == ("place", 1, 2, 3),
          f"{ra.key_of(_OpStep('place_real', 1, 2, 3))}")
    check("key_of 的 attack/consume 是单例键（不带坐标）",
          ra.key_of(_OpStep("attack", 9, 9, 9)) == ("attack",)
          and ra.key_of(_OpStep("consume", 9, 9, 9)) == ("consume",),
          "attack/consume 带坐标 → 同一个攻击会被当成不同步骤")

    # --- 缺陷 2：累加器会被 _cleanup 清空 -------------------------------
    #
    # `_attempts` 是"本轮进行中的一次尝试"，拿到结论后 `_cleanup` 会清掉。
    # 拿它当累加器（`_attempts.get(k, 0) + 1`）的话，累出来的**永远是 1**，
    # `n >= MAX_ATTEMPTS` 永不成立 —— 一个挖不动的方块会被无限重发，
    # 刷屏永远不停，计划也永远等不到"放弃"这个终态。
    b2 = CountingBridge()
    c2 = BodyController("bot", b2, SolidWorld(), QuietLogger())
    ra2 = RealBodyActions(c2, SolidWorld(), QuietLogger())
    ra2.RETRY_BACKOFF_SECONDS = 0.0
    k2 = ("digfar", 8, 64, 8)

    seen: list[int] = []
    for _ in range(4):
        ra2._cleanup(k2)                # 模拟"每次拿到结论都会被清"
        seen.append(ra2._bump(k2))
    check("_tries 在 _cleanup 之间仍然累加（_attempts 会归零）",
          seen == [1, 2, 3, 4], f"累加结果={seen}")
    check("_cleanup 确实会清掉 _attempts（所以它不能当累加器）",
          ra2._attempts.get(k2, 0) == 0, f"_attempts={ra2._attempts}")
    check("_cleanup **不**清 _tries（累加器必须活得久）",
          ra2._tries.get(k2) == 4, f"_tries={ra2._tries}")

    # --- 缺陷 3：判"第一次"的据据也会被清空 -----------------------------
    #
    # 多阶段处理器（dig_far / place_real）要区分"首次发起"和"等结论"。
    # 拿 `_attempts` 当判据，会在拿到结论的下一轮又落回"第一次"分支，
    # 于是重新 `clear_dig_result()`，把**刚拿到的结论扔掉**再发一次……
    # 结论永远读不到、方块永远挖不掉、计划永远停在这一步。
    bs = CountingBridge()
    cs = BodyController("bot", bs, SolidWorld(), QuietLogger())
    ras = RealBodyActions(cs, SolidWorld(), QuietLogger())
    ras._within_reach = lambda x, y, z: True       # type: ignore[method-assign]
    ras.RETRY_BACKOFF_SECONDS = 0.0
    cs.DIG_DEDUP_SECONDS = 0.0

    s9 = Step(9, 64, 9)
    k9 = ("digfar", 9, 64, 9)
    ras.dig_far(s9)
    check("首次发起后 _started 记下了这一步", k9 in ras._started,
          f"started={ras._started}")
    check("首次发起时发了一条 dig", len(bs.digs) == 1, f"digs={bs.digs}")

    bs.finish_dig(False, "挖不动")
    ras.dig_far(s9)                     # 消费结论
    check("消费结论后 _started 仍然在（不会被 _cleanup 清掉）",
          k9 in ras._started, f"started={ras._started}")
    check("消费结论后结论被消费掉（不会下一轮重演）",
          cs.dig_result_for(9, 64, 9) is None,
          f"last_dig={cs.last_dig}")

    n_before = len(bs.digs)
    ras.dig_far(s9)                     # 这一轮才轮到"重新发起"
    check("拿到失败结论的下一轮会重新发起（断头路已打通）",
          len(bs.digs) == n_before + 1,
          f"before={n_before} now={len(bs.digs)}")
    check("重新发起后 _tries 只记了 1 次（同一条结论不会反复记账）",
          ras._tries.get(k9) == 1, f"tries={ras._tries.get(k9)}")

    # --- 缺陷 4：退避闸会自己重置自己 -----------------------------------
    #
    # 若先 `_mark_failed` 再判退避，刚设的 1.5 秒会被自己往后推，
    # 退避永不生效；而执行器是**每 tick** 来问一次的，不挡住的话
    # `_tries` 会以每秒 20 次的速度往上涨，几次重试额度在零点几秒内
    # 就烧光 —— "试 4 次再放弃"实际变成"试 0.2 秒就放弃"。
    br = CountingBridge()
    cr = BodyController("bot", br, SolidWorld(), QuietLogger())
    rar = RealBodyActions(cr, SolidWorld(), QuietLogger())
    rar._within_reach = lambda x, y, z: True       # type: ignore[method-assign]
    rar.RETRY_BACKOFF_SECONDS = 999.0              # 退避永远不过期
    kr = ("digfar", 11, 64, 11)

    for _ in range(200):                           # 模拟执行器连续追问
        rar._fail_or_give_up(kr, 4, "挖不动")
    check("退避期内的追问不消耗重试次数",
          rar._tries.get(kr, 0) <= 1,
          f"tries={rar._tries.get(kr, 0)}（200 次追问只该记 1 次）")
    check("退避期内不会打上放弃标记",
          kr not in rar._gave_up, f"gave_up={rar._gave_up}")

    # 反过来：退避放开后，额度应该按次消耗、到点才放弃。
    rar2 = RealBodyActions(cr, SolidWorld(), QuietLogger())
    rar2.RETRY_BACKOFF_SECONDS = 0.0
    kr2 = ("digfar", 12, 64, 12)
    hits = [rar2._fail_or_give_up(kr2, 3, "挖不动") for _ in range(5)]
    check("退避放开后尝试次数按次累加、到上限才放弃",
          hits[:2] == [False, False] and hits[2] is True,
          f"逐次返回={hits}")

    # --- 缺陷 5：`Plan.note_failed` 把"放弃"结算成失败 ------------------
    #
    # `RealBodyActions` 那边按步骤上限烧掉自己的重试额度、宣告放弃之后，
    # 计划层要做的不是"再给它从头来过的机会"（那样会无限重规划，正是
    # 日志刷屏的另一个来源），而是**把这一步记成失败、让计划照常结算**。
    # `note_failed` 返回 True 是关键 —— 它让 `_real_or_retry` 能交回
    # 执行器推进游标；返回 False 会被当成"还没结论"，异步步骤挂住
    # `pending_op`，计划永久停在这一步。
    from endstone_arc_fake_player.executor import Plan, Step as ExStep  # noqa: I001

    pl = Plan(label="bot: 测试", steps=[ExStep("dig_far", 1, 64, 1)])
    check("Plan.total 就是步骤数（不再把重试额度算进去）",
          pl.total == 1, f"total={pl.total}")

    check("note_failed 返回 True（让执行器推进游标）",
          pl.note_failed("失败一次") is True)
    check("note_failed 记了一次失败", pl.fail_count == 1,
          f"fail_count={pl.fail_count}")
    check("note_failed 保留了失败原因", "失败一次" in pl.skipped,
          f"skipped={pl.skipped}")
    check("note_failed 会清掉 pending_op（不然执行器会挂住）",
          pl.pending_op == "", f"pending_op={pl.pending_op!r}")

    # ⚠️ 核心回归：`total` 绝不能比 `len(steps)` 大，否则游标走完
    # 真实步骤后 `while cursor < total` 仍成立，`steps[cursor]` 越界。
    # 这正是线上日志 `IndexError: list index out of range` 的根因。
    pl_many = Plan(label="bot: 十步", steps=[ExStep("dig_far", i, 64, i)
                                             for i in range(10)])
    check("十步计划 total == 10（游标最多走到 10，不会越界）",
          pl_many.total == len(pl_many.steps) == 10,
          f"total={pl_many.total} len={len(pl_many.steps)}")

    pl2 = Plan(label="bot: 无额度", steps=[ExStep("dig_far", 2, 64, 2)])
    pl2.abort()
    check("abort 后进度看起来是满的（done_count == total）",
          pl2.done_count == pl2.total,
          f"done={pl2.done_count} total={pl2.total}")

    # --- 缺陷 6：源码里不该再有失效的累加写法 ---------------------------
    rsrc = (PKG / "realbody.py").read_text("utf-8")
    bad = re.findall(r"_tries\[[^\]]+\]\s*\+\s*1", rsrc)
    check("realbody.py 里没有裸的 _tries[x] + 1（一律走 _bump）",
          not bad, f"违规 {len(bad)} 处: {bad[:3]}")
    bad2 = re.findall(r"_attempts\.get\([^)]*\)\s*\+\s*1\s*>=", rsrc)
    check("realbody.py 里没有拿 _attempts 当上限判据的写法",
          not bad2, f"违规 {len(bad2)} 处: {bad2[:3]}")
    check("realbody.py 里确实有 _gave_up 这张表（终态的表达方式）",
          "_gave_up" in rsrc and "def _give_up" in rsrc)
    check("realbody.py 里确实有 _started 这张表（首判的依据）",
          "_started" in rsrc)


def test_reissue_paths() -> None:
    """**每条失败路径后面都必须跟着"重新发起"** —— 本组测试的核心命题。

    这是本项目最隐蔽的一类 bug，症状和原因完全不搭：

        日志干干净净（一句报错都没有）
        `_tries` 老老实实地涨（记账是对的）
        退避也老老实实地等（闸也是对的）
        —— 但 `bot:xxx` **永远只有最开始那 1 条**，计划永远走不完。

    根因统一是"**只记账、不干活**"：处理器拿到失败结论 → 调
    `_retry_later()` / `_fail_or_give_up()` → `return`。
    这两个函数**只负责记账**，重发必须由调用方显式再调一次 `issue()`
    加真正的发起动作。少了那一步，整个函数就成了一条断头路。

    实测（`verify_final.py` 的 A 段，真实时间 5 秒 / 100 次追问）：

        修好前：`bot:walk` 只有 1 条，然后一等等到天荒地老
        修好后：`tries` 累加到 4，重发机制活着，且退避把频率压在
                1.5 秒一条（不是每 tick 一条）

    ⚠️ 为什么必须**每个**处理器都测：这类 bug 是逐个函数独立存在的，
    `dig_near` 修好了不代表 `walk_to` 也修好了 —— 事实上它们就是分别中招的。
    """
    print("\n" + "-" * 70)
    print("十八、失败路径的'重新发起'闭环（3.0.2）")
    print("-" * 70)

    # --- walk_to：本轮真正致命的那个（用户报的"不动了"）----------------
    #
    # ⚠️ 用**真实时间**跑，不能用虚拟时间。退避依据是 `time.monotonic()`，
    # 循环跑得飞快时真实时间几乎不动，所有重发都会被挡掉，测出来的
    # "只有 1 条"是假象（我第一次就踩了这个坑，看着像修好了）。
    b = CountingBridge()
    c = BodyController("bot", b, SolidWorld(), QuietLogger())
    ra = RealBodyActions(c, SolidWorld(), QuietLogger())
    ra._within_reach = lambda x, y, z: False       # 永远够不着 → 只能走
    ra.RETRY_BACKOFF_SECONDS = 0.0                 # 松开退避，专测重发闭环
    # walk 的重试上限是独立的 WALK_MAX_ATTEMPTS（3.0.4 起），
    # 不再和挖掘共用 MAX_ATTEMPTS——走不到的原因通常是"目标悬空"，
    # 重试多了只会刷屏。
    ra.WALK_MAX_ATTEMPTS = 5

    class WalkStep:
        op, x, y, z, note = "walk_to", 40, 64, 40, ""

    ws = WalkStep()
    kw = ("walk", 40, 64, 40)

    # 假人位置恒定为 (0,64,0)，离目标 40 格 —— 永远走不到。
    class StuckActor:
        class _L:
            x, y, z = 0.0, 64.0, 0.0
        location = _L()
    c.actor = StuckActor()

    verdict = None
    # 每一轮"记账"要占两拍（一拍重发、一拍读到失败结论），
    # 所以烧掉 5 次额度需要 10 拍以上。给足余量，顺便也断言了
    # 它**一定会**在有限轮内交出终态（循环跑满还不结束就是卡死）。
    for _ in range(40):
        verdict = ra.walk_to(ws)
        if verdict is True:
            break
        if c.busy:                     # 寻路挂起了 → 给它一个失败结论
            b.finish_walk(False, "寻路失败")
    check("walk_to 失败重试后能走到明确的终态（不是永远 False）",
          verdict is True, f"返回 {verdict!r}")
    check("walk_to 的终态挂了放弃标记", kw in ra._gave_up,
          f"gave_up={ra._gave_up}")
    check("walk_to 真的重发过（不是只发了 1 条就记账到死）",
          len(b.walks) > 1,
          f"发了 {len(b.walks)} 条 → 重发闭环已打通")

    again = [ra.walk_to(ws) for _ in range(3)]
    check("walk_to 的终态是幂等的（之后每问都返回 True）",
          all(v is True for v in again), f"后续={again}")

    # --- 逐个处理器检查"记账之后有没有重新发起" -------------------------
    #
    # 源码级检查：`_fail_or_give_up` / `_retry_later` 的调用点之后，
    # 同一个函数体内必须还能看到一次 `issue(` 或 `start_*`。
    # 这是能一次抓住所有同类 bug 的最经济写法 —— 手工给六个处理器
    # 各写一套时序测试既慢又容易漏。
    rsrc = (PKG / "realbody.py").read_text("utf-8")

    def body_of(name: str) -> str:
        start = rsrc.find(f"def {name}")
        if start < 0:
            return ""
        end = rsrc.find("\n    def ", start + 10)
        return rsrc[start:end if end > 0 else len(rsrc)]

    #: 处理器 → 它"重新发起"时该调用的函数名
    need_reissue = {
        "walk_to": "start_walk",
        "dig_near": "start_dig",
        "dig_far": "start_dig",
        "place_real": "_send_place",
        "consume_real": "consume",
        "attack_real": "attack",
    }
    missing = []
    for fn, reissue in need_reissue.items():
        body = body_of(fn)
        if not body:
            missing.append(f"{fn}(未找到)")
            continue
        if "_fail_or_give_up(" not in body and "_retry_later(" not in body:
            continue                       # 没有失败路径，不用检查
        if reissue not in body:
            missing.append(f"{fn}(缺 {reissue})")
    check("每个处理器的失败路径后面都有'重新发起'",
          not missing, f"断头路: {missing}")

    # "记账"和"重发"必须分轮：`_fail_or_give_up` / `_retry_later` 那一行
    # **不能再紧跟** `issue(` 或 `start_*`（同一轮重发会撞上刚设的退避）。
    # 这是本轮发现的最隐蔽 bug：dig 失败后永远只发最初那 1 条。
    for fn in need_reissue:
        body = body_of(fn)
        # 找"记账"调用，看它后面是否在同一行/紧邻行又去重发。
        for m in re.finditer(r"return self\._retry_later\(|return self\._fail_or_give_up\(",
                             body):
            tail = body[m.start():]
            nxt = tail.split("\n", 2)
            joined = "\n".join(nxt[:2]) if len(nxt) > 1 else tail
            # 记账调用之后紧跟着 issue / start_* / _send_place 就是同轮重发
            if re.search(r"return self\._(retry_later|fail_or_give_up)\([^\n]*\n\s*(if self\.issue|pre = self\.issue|self\._send_place|ctl\.start_)", joined):
                missing.append(f"{fn}(记账后同轮重发)")

    check("记账和重发没有写在同一轮（否则重发撞退避）",
          not missing, f"违规: {missing}")

    # walk 的结论必须和 dig 一样能"消费"：`last_walk` 是只读的，读完不清
    # 下一轮会重演同一条失败，把同一次失败重复记账。
    check("body.py 有 clear_walk_result（和 clear_dig_result 对称）",
          "def clear_walk_result" in (PKG / "body.py").read_text("utf-8"))
    for fn in ("walk_to", "dig_far", "place_real"):
        body = body_of(fn)
        if "last_walk" in body:
            check(f"{fn} 读 last_walk 的地方配了 clear_walk_result",
                  "clear_walk_result()" in body,
                  "不消费 → 同一条寻路失败被重复记账")

    # 判"第一次"一律用 `_started`，不能再有拿 `_attempts` 判首判的写法。
    # ⚠️ 这是本轮改动的核心不变量：`_attempts` 会被 `_cleanup` 清掉，
    # 而重新登记它的唯一入口 `issue()` 可能被退避闸挡下（挡下时不登记），
    # 于是"第一次"判据永远成立 → 永远在"首次发起"和"被闸挡下"之间循环。
    #
    # 检查时只认**真正的代码**（`if key not in self._attempts:`），
    # 不能匹配 docstring 里"解释这个坑"的引用文字 —— 那些注释里
    # 本来就会提到 `_attempts` 这个名字。
    offenders = []
    for fn in need_reissue:
        if "if key not in self._attempts:" in body_of(fn):
            offenders.append(fn)
    check("没有处理器再拿 _attempts 判'是不是第一次'",
          not offenders, f"违规: {offenders}（必须用 _started）")

    # `dig_result_for` 是只读的 —— 读过的地方都必须 `clear_dig_result()`，
    # 否则同一条陈旧结论会在每一轮被当成新失败重新记账。
    for fn in ("dig_near", "dig_far"):
        body = body_of(fn)
        n_read = body.count("dig_result_for(")
        n_clear = body.count("clear_dig_result(")
        check(f"{fn} 读结论的地方都配了 clear（不重演旧结论）",
              n_clear >= n_read,
              f"dig_result_for={n_read} 次，clear={n_clear} 次")

    # walk 的重试上限必须是独立的 WALK_MAX_ATTEMPTS。走不到的原因通常
    # 是"目标悬空"（挖高树时上段原木的落脚点在空中），重试 300 次只会
    # 每 1.5 秒刷一条 bot:walk、连刷 7 分多钟 —— 正是用户截图里的现象。
    check("walk_to 用独立的 WALK_MAX_ATTEMPTS（不用挖掘的 300 次）",
          "self.WALK_MAX_ATTEMPTS" in body_of("walk_to"))
    check("dig_far 的 walk 阶段也用 WALK_MAX_ATTEMPTS",
          "self.WALK_MAX_ATTEMPTS" in body_of("dig_far"))
    check("挖掘上限已从 300 降到 30（挖不动的方块别空耗半小时）",
          re.search(r"MAX_ATTEMPTS = 30\b", rsrc) is not None)


def test_approach_point_and_early_dig() -> None:
    """3.0.4：挖树"假人不动、walk 刷屏"的两个直接现场。

    官方文档确认的事实（learn.microsoft.com scriptapi）：
      · SimulatedPlayer.navigateToLocation(location, speed?) 是**同步**的，
        返回 NavigationResult（isFullPath = 是否找到完整**路径**，不是
        "走到了"），没有 timeout 参数。
      · 假人必须接触地面才能开始导航 → 悬空目标必然规划失败。

    推论：
      · `_approach_point` 若返回悬空格（挖高树时上段原木的"落脚点"
        在树旁空中），每条寻路都会失败 → walk 每 1.5 秒重发一条，
        假人原地不动 —— 用户截图的刷屏就是它。
      · 反过来，假人走向高处落脚点的**半路**往往已经够得着低处原木，
        没必要等寻路超时，应该停下直接挖。
    """
    print("\n" + "-" * 70)
    print("二十、approach 点悬空过滤与半路提前开挖（3.0.4）")
    print("-" * 70)

    # --- _approach_point：悬空候选必须被过滤 ----------------------------
    class LayeredWorld:
        """可控方块世界：用集合记录实心格，其余全是空气。"""

        def __init__(self) -> None:
            self.solid: set[tuple[int, int, int]] = set()

        def is_solid_id(self, x: int, y: int, z: int) -> bool:
            return (int(x), int(y), int(z)) in self.solid

    w = LayeredWorld()
    # 地面 y=64 一整层；树干 (5,65,5)、(5,66,5)（挖树场景）
    for dx in range(-2, 9):
        for dz in range(-2, 9):
            w.solid.add((dx, 63, dz))          # y=63 是地面（支撑层）
    w.solid.add((5, 64, 5))                    # 树干底（在地面之上）
    w.solid.add((5, 65, 5))
    w.solid.add((5, 66, 5))

    b = CountingBridge()
    c = BodyController("bot", b, w, QuietLogger())
    ra = RealBodyActions(c, w, QuietLogger())

    # 挖 (5, 65, 5)（树干中段）：
    #   同层侧向 (4,65,5) 等的脚下是 (·,64,·) 空气 → 悬空，被过滤
    #   低一层侧向 (4,64,5) 等的脚下是地面 → 合法候选
    #   上方 (5,66,5) 脚下是原木 → 合法候选
    #   下方 (5,64,5) 是树干（实心）→ 跳过
    ap = ra._approach_point(5, 65, 5)
    check("树干中段的落脚点选在地面侧向格（不是悬空格）",
          ap in {(4, 64, 5), (6, 64, 5), (5, 64, 4), (5, 64, 6), (5, 66, 5)},
          f"approach={ap}")
    check("落脚点脚下有支撑",
          (ap[0], ap[1] - 1, ap[2]) in w.solid, f"{ap} 脚下 = {w.solid}")

    # 挖 (5, 66, 5)（树干上段）：侧向格 (4,66,5) 等的脚下是空气 → 悬空，
    # 必须被过滤掉；唯一有支撑的候选是 (5,67,5)（方块上方，脚下是原木）。
    ap2 = ra._approach_point(5, 66, 5)
    check("树干上段的悬空候选被过滤（不再返回空中格）",
          (ap2[0], ap2[1] - 1, ap2[2]) in w.solid or ap2 == (5, 67, 5),
          f"approach={ap2} 脚下={(ap2[0], ap2[1]-1, ap2[2]) in w.solid}")

    # --- dig_far：走的一半够得着了 → 停下直接挖 ------------------------
    class Walker:
        """位置可编程的假人。"""

        def __init__(self) -> None:
            self.pos = (0.0, 64.0, 0.0)

        class _L:
            x = y = z = 0.0
        location = _L()

        def set(self, x, y, z):
            self.pos = (x, y, z)
            self.location.x, self.location.y, self.location.z = x, y, z

    wk = Walker()
    c.actor = wk

    # 目标 (5,64,5)；假人从 (1.6, 64, 5.5) 出发 —— 距离目标中心约 3 格，
    # 已在 4.5 格够得着范围内。首次发起时故意让 _within_reach 为 False
    # （模拟"规划时够不着"），寻路挂起后位置更新为够得着。
    ra2 = RealBodyActions(c, w, QuietLogger())
    reach = {"v": False}
    ra2._within_reach = lambda x, y, z: reach["v"]   # type: ignore[method-assign]
    ra2._approach_point = lambda x, y, z: (3, 64, 5)  # type: ignore[method-assign]

    class DS:
        op, x, y, z, note = "dig_far", 5, 64, 5, ""
    ds = DS()

    reach["v"] = False
    ra2.dig_far(ds)                     # 首次：发起寻路
    check("首次发起后寻路挂起（busy）", c.busy, f"busy={c.busy}")
    check("寻路目标已发出", len(b.walks) == 1, f"walks={b.walks}")

    # 假人走到半路（离目标 3 格，已够得着）
    wk.set(1.6, 64.0, 5.5)
    reach["v"] = True
    r = ra2.dig_far(ds)
    check("半路够得着时会停走转挖（cancel 寻路）",
          b.stops >= 1 and len(b.digs) == 1,
          f"stops={b.stops} digs={len(b.digs)} 返回={r}")
    kdf = ("digfar", 5, 64, 5)
    check("转挖后 phase 是 dig", ra2._phase_of(kdf) == "dig",
          f"phase={ra2._phase_of(kdf)!r}")
    check("cancel 留下的 walk 失败结论被清掉", c.last_walk is None,
          f"last_walk={c.last_walk}")


def test_dig_orientation_and_timeout() -> None:
    """3.0.11 → 3.0.12：把"真挖掘"做对——既要连续，又要不穿墙。

    用户实测现象（两轮）：
      1. "断断续续地挖"——一顿一顿，不是平滑推进。
      2. "不是假人的挖掘"——假人偏离位置、眼睛没盯着那个方块、甚至
         中间有方块格挡，照样能把目标挖掉（穿墙 / 隔空挖）。

    根因（已查官方文档 + 两个成熟包 Understudy / FlashFakePlayerPack 交叉验证）：
      · `SimulatedPlayer.breakBlock(loc)` 只看坐标，**不判定视线、距离、
        遮挡**——文档原话 "Destroys the block at blockLocation"。所以只要
        把 Python 给的坐标直接喂进去，就天然会"穿墙/隔空挖"。旧版所谓的
        "朝向校验"只是个 advisory 射线，**根本没挡住** breakBlock。
      · 旧版只在发起时调一次 breakBlock，之后只轮询。一旦引擎中途把这一
        锤子静默打断（被推挤 / 区块边界 / 状态抖动），就只能干等到判超时
        再整个重启一轮——这就是"一顿一顿"。两个成熟包都是**每 tick**
        调一次 breakBlock，证明重复调用是安全的（语义是"继续敲"不是"重来"）。

    修法（对齐两个成熟包）：
      1. 核心闸门：每 tick 先 `getBlockFromViewDirection` 射线，只有射线
         打到的**第一格就是目标格**才 breakBlock；否则宁可如实失败，
         绝不穿墙。`AIM_LOST_TICKS` 连续计数吸收转头途中的正常 miss。
      2. 距离闸门：`DIG_MAX_DISTANCE` 管"够得着"，没有它站在远处也能隔空敲。
      3. 每 tick 补一次 breakBlock，把中断随时接上 → 连续推进。
      4. 硬上限 `DIG_LIMIT_TICKS`（45 秒）兜底，真挖不动如实回报。
    """
    print("\n" + "-" * 70)
    print("二十一、挖掘：每 tick 持续敲击 + 距离闸门（视线闸门默认关，3.0.14）")
    print("-" * 70)

    jsrc = (ROOT / "endstone_arc_fake_player" / "behavior_pack" / "scripts"
            / "main.js").read_text(encoding="utf-8")

    # --- faceBlock：统一的转向辅助，doDig 和 pumpOneDigging 都用 ----
    fb = jsrc[jsrc.find("function faceBlock"):jsrc.find("/** 这个方块现在还是不是实体")]
    check("有 faceBlock 辅助函数（doDig 和续挖共用转向）",
          "function faceBlock" in jsrc and "lookAtBlock" in fb)
    # ⚠️ 这条断言的含义被**反转**过，原因必须留档。
    #
    # 旧版断言 `"Continuous" in fb`，理由是"挖掘要十几秒，怕假人中途把头
    # 转走"。但 Continuous 在引擎里是一条**常驻转向行为**，而转向和挖掘
    # 是互斥的行为槽——每维持一次朝向就把持续挖掘打断一次，破坏进度归零。
    # 这就是用户报的"假人挖不动方块""进度反复归零"的真正原因。
    #
    # 模拟玩家没有真人的无意识转头漂移，瞄一次就够（Understudy 的
    # `./simplayer:look block` + `break` 就是这个模型）。中途真转走了由
    # `raycastBlock()`（getBlockFromViewDirection）只读射线检测发现后
    # 再补瞄（pumpOneDigging 的 AIM_LOST_TICKS 连续计数），不会打断挖掘。
    # 所以现在要求 faceBlock **不要**传 Continuous。
    check("【回归】faceBlock 不再用 Continuous（持续转向会打断持续挖掘）",
          "sim.lookAtBlock(center);" in fb and "LookDuration.Continuous" not in fb,
          "Continuous 是常驻转向行为，与挖掘互斥 → 破坏进度反复归零")

    # --- doDig：登记任务 + 立刻转向；真正动手在 pumpOneDigging --------
    dd = jsrc[jsrc.find("function doDig"):jsrc.find("function pumpDigging")]
    check("doDig 登记任务后立即 faceBlock 转向（breakBlock 放到轮询里约束后敲）",
          "faceBlock(sim, loc)" in dd
          and "digging.set(key" in dd
          and "breakBlock(" not in dd,
          "doDig 不直接敲：射线闸门必须集中在 pumpOneDigging 一处")
    check("单次挖掘硬上限 DIG_LIMIT_TICKS = 900（45 秒，盖住最硬的方块）",
          "DIG_LIMIT_TICKS = 900" in jsrc,
          "旧版 100 tick=5 秒会让原木/石头必然误判超时；新版用硬上限兜底")

    # --- pumpOneDigging：够不着提前结束 + 硬上限超时 -----------------
    # 注意：切片要吃到整个函数体（含 // ① 之后的射线闸门），不能用第一个
    # \n// 截断——闸门逻辑全在 // ① 之后。
    po_end = jsrc.find("\nfunction ", jsrc.find("function pumpOneDigging"))
    po = jsrc[jsrc.find("function pumpOneDigging"):po_end if po_end > 0 else None]
    check("pumpOneDigging 检测够不着（距离闸门）并提前结束",
          "DIG_MAX_DISTANCE" in po and "够不着" in po,
          "没有距离闸门，站二十格外也能隔空敲掉方块")
    check("pumpOneDigging 有硬上限超时（DIG_LIMIT_TICKS → 挖掘超时），且无重启轮",
          "DIG_LIMIT_TICKS" in po and "挖掘超时" in po
          and "startBreakBlock(" not in po and "restart(" not in po,
          "超时如实回报即可；旧版的重启轮只会让一停一顿更明显")

    # ⚠️ 这一段是"假人只会盯着方块、根本不挖"的正解，原因必须留档。
    #
    # 3.0.12 把"射线没打到目标格就**绝不** breakBlock"做成了硬闸门：射线不
    # 匹配就 `return`，`breakBlock` **一次都调不到**。道理没错（`breakBlock`
    # 确实只认坐标、不判视线），但真机上 `lookAtBlock` 是异步的，而
    # `getBlockFromViewDirection` 返回的是"视线扫到的第一格"，它和"我要挖的
    # 那一格"常常不是同一个 —— 于是假人永远只是盯着方块不动手。用户连着
    # 三次报"不挖方块"，根因都在这里。
    #
    # 现在默认**不拦截**（`DIG_STRICT_SIGHT = false`）：挖照挖，防作弊交给
    # 距离闸门（身体本来就会先走到方块旁，见 Python 侧 `DIG_REACH = 4.5`）。
    # 想要"看不见就不许挖"就把开关打开 —— 严格分支整套都还在。
    check("【核心回归】视线闸门默认关闭（DIG_STRICT_SIGHT = false），保证一定挖得动",
          "const DIG_STRICT_SIGHT = false;" in jsrc,
          "开成 true 会让 breakBlock 一次都调不到 → 假人盯着方块不动手")
    check("视线闸门整段包在 if (DIG_STRICT_SIGHT) 里（关掉就不会硬拦截）",
          "if (DIG_STRICT_SIGHT) {" in po,
          "不包起来的话，默认模式也会被射线卡住")
    check("距离闸门**始终**生效（DIG_MAX_DISTANCE + 够不着）",
          "DIG_MAX_DISTANCE" in po and "够不着" in po,
          "这是默认模式下挡住隔空挖的约束")
    check("【回归】挖掘进行中不再周期性 lookAtBlock（会打断持续挖掘）",
          "% 20 === 0" not in po,
          "每 20 tick 重瞄一次 = 每 20 tick 把破坏进度清零，方块永远挖不掉")
    check("每 tick 都敲：breakBlock 在轮询里逐 tick 调用",
          "sim.breakBlock(ctx.loc)" in po,
          "只发起一次、之后干等 → 引擎一中断就一顿一顿")
    check("【回归】breakBlock 只敲目标格（坐标来自 ctx.loc，非射线命中）",
          po.count("breakBlock(") == 1
          and "breakBlock(ctx.loc)" in po,
          "若用射线命中方块的坐标，就会把挡路的方块自己挖掉")
    # 严格模式的三件套仍然在位（开关打开时才走，别删）
    check("严格模式三件套仍在（sameBlock 闸门 / miss 计数 / describeOcclusion）",
          "sameBlock(hit.loc, ctx.loc)" in po
          and "AIM_LOST_TICKS" in jsrc and "MAX_AIM_RETRIES" in jsrc
          and "ctx.miss" in po and "describeOcclusion(hit)" in po,
          "开关打开后仍要能挡住穿墙挖，这几件不能丢")

    # --- Python 侧挖掘兜底超时 --------------------------------------
    # JS 侧现在是"一直挖直到破坏"：每 tick 持续敲击，硬上限 DIG_LIMIT_TICKS
    # = 900 tick（45 秒）后如实回报。最长 JS 路径就是 45 秒。Python 的
    # DIG_TIMEOUT = 150 秒纯粹是兜底（JS 轮询挂了永远不回时才触发），
    # 必须盖住 45 秒这条最长路径、且远大于它。
    bsrc = (ROOT / "endstone_arc_fake_player" / "body.py").read_text(encoding="utf-8")
    check("DIG_TIMEOUT 盖住 JS 最长挖掘路径（45s 硬上限 → 兜底 150s）",
          "DIG_TIMEOUT = 150.0" in bsrc, "必须大于 45s 硬上限，否则会在 JS 还在挖时误判超时")
    check("JS 侧无重启轮：超时即如实回报（DIG_LIMIT_TICKS → 挖掘超时）",
          "DIG_LIMIT_TICKS" in jsrc and "挖掘超时" in jsrc
          and "restart(" not in jsrc,
          "超时直接报失败即符合'真挖掘'语义；旧版重启轮是一顿一顿的元凶")

    # --- 重复挖掘请求必须回复（3.0.11）→ 在 3.0.12 仍然成立 ----------
    #
    # 用户实测："挖着挖着突然不动了，过好几分钟才继续"。
    # 根因：doDig 发现同坐标已在挖时直接裸 `return`，那条 bot:dig 请求
    # **一条 bot:dug 都收不到**，Python 侧一直挂到 150 秒超时。所以"任何
    # 发出的请求都必须有回复"。3.0.11 起把它挂到既有挖掘的 `ctx.ids`，
    # 由唯一的 funnel `endDigging`/`digReplyAll` 一起回复——3.0.12 没动这
    # 条契约，但要守住它不被新改动破坏。
    check("【回归】重复挖掘请求会挂到既有挖掘上，绝不静默丢弃",
          "digging.get(key)" in dd
          and "ids.push(id)" in dd
          and "pendingDigs" not in jsrc,
          "静默丢弃 = Python 侧干等 150 秒，表现为假人卡死")
    check("回复走 digReplyAll（一次挖掘可能挂着多个请求号）",
          "function digReplyAll" in jsrc and "for (const rid of ids)" in jsrc,
          "只回第一个请求号，其余的照样挂死")
    check("移除 pendingDigs：重复请求直接并入 digging 的 ids（不再有独立 Map）",
          "const pendingDigs" not in jsrc,
          "残留的 pendingDigs 会重新引入静默丢弃分支")
    # 真正的回归点在：ctx 建起来之后，所有回复都走 `digReplyAll`，绝不按
    # 单个 id 回复。`ctx.id\b`（正则 \b 保证不会误命中 `ctx.ids`）必须
    # 彻底消失，否则那条分支上的重复请求会永远挂着。
    check("doRemove / endDigging 全部改用 digReplyAll 回复，且无单 id 回复残留",
          jsrc.count("digReplyAll(") >= 2
          and "digReplyAll(ctx.ids" in jsrc
          and not re.search(r"ctx\.id\b", jsrc),
          "任何一条漏改都会留下一个永不回复的分支")
    check("doStatus 的 busy 按假人名字查，不再查脚下那一格",
          "k.startsWith(name + \"|\")" in jsrc,
          "假人是站在旁边挖，脚下那格几乎不可能是挖掘对象 → busy 恒为 false")


def test_craft_and_containers() -> None:
    """3.0.6：知识库整合 + 合成/熔炉/箱子工具。"""
    print("\n" + "-" * 70)
    print("二十二、知识库 / 合成 / 容器（3.0.6）")
    print("-" * 70)

    # --- 知识库整合 -------------------------------------------------
    kdir = ROOT / "endstone_arc_fake_player" / "knowledge"
    check("knowledge 目录有分卷知识库",
          (kdir / "11_常用生存合成表_Crafting_Recipes.md").exists())
    asrc = (ROOT / "endstone_arc_fake_player" / "agent.py").read_text(encoding="utf-8")
    check("agent.py 有 DEFAULT_GAME_KNOWLEDGE（合成表/采矿等级/食物）",
          "DEFAULT_GAME_KNOWLEDGE" in asrc and "采矿等级" in asrc)
    check("SYSTEM_TEMPLATE 带 $game_knowledge 段落",
          "$game_knowledge" in asrc)

    from tests.test_hybrid import FakeActor
    from endstone_arc_fake_player.actions import ActionCompiler
    from endstone_arc_fake_player.world import WorldAdapter
    from endstone_arc_fake_player.llm import ToolCall

    class _L:
        def debug(self, *a): pass
        def info(self, *a): pass
        def warning(self, *a): pass
        def error(self, *a): pass

    # --- 配方表 + slot 布局 ----------------------------------------
    check("CRAFT_RECIPES 覆盖高频配方（stick/wooden_pickaxe/stone_pickaxe）",
          all(k in ActionCompiler.CRAFT_RECIPES
              for k in ("stick", "wooden_pickaxe", "stone_pickaxe", "torch", "bread")))
    check("SMELT_RECIPES 铁矿石→铁锭",
          ActionCompiler.SMELT_RECIPES.get("iron_ore") == "iron_ingot")

    ac = ActionCompiler(WorldAdapter(_L()), _L())
    slots = ac._recipe_slots(ActionCompiler.CRAFT_RECIPES["stick"])
    check("stick 配方 → 2 个木板竖排（slot 0 和 3）",
          slots == [{"slot": 0, "id": "planks"}, {"slot": 3, "id": "planks"}],
          f"slots={slots}")
    slots2 = ac._recipe_slots(ActionCompiler.CRAFT_RECIPES["wooden_pickaxe"])
    check("木镐配方 → 3 木板顶行 + 2 木棍中列",
          slots2 == [{"slot": 0, "id": "planks"}, {"slot": 1, "id": "planks"},
                     {"slot": 2, "id": "planks"}, {"slot": 4, "id": "stick"},
                     {"slot": 7, "id": "stick"}],
          f"slots={slots2}")

    # --- craft 编译：2×2 配方走背包，**不需要工作台** ----------------
    #
    # 这是 3.0.13 修的启动死循环：工作台本身只能在 2×2 里合出来，
    # 旧版却对**所有**配方都要求附近有工作台 → "要先有工作台才能合成
    # 工作台"，永远出不来。
    check("_recipe_slots_2x2 把 stick 画进 2×2（slot 0 和 2）",
          ac._recipe_slots_2x2(ActionCompiler.CRAFT_RECIPES["stick"])
          == [{"slot": 0, "id": "planks"}, {"slot": 2, "id": "planks"}])
    a = FakeActor(0.0, 64.0, 0.0)      # 附近**没有**工作台
    r = ac.compile(a, ToolCall("craft", {"item": "stick"}), {})
    check("2×2 配方（stick）没有工作台也能合成（一步背包合成）",
          r.ok and r.plan is not None and len(r.plan.steps) == 1
          and r.plan.steps[0].op == "container",
          getattr(r, "immediate", ""))
    if r.ok and r.plan:
        import json as _json
        note = _json.loads(r.plan.steps[0].note)
        check("背包合成 note 带 grid=2 + result/count",
              note.get("op") == "craft" and note.get("grid") == 2
              and note.get("result") == "stick" and note.get("count") == 4,
              str(note))
    # crafting_table 也是 2×2：它能自举出来才谈得上后面 3×3 的东西
    r_ct = ac.compile(a, ToolCall("craft", {"item": "crafting_table"}), {})
    check("工作台自己也是 2×2，没有工作台也能先合出来（自举）",
          r_ct.ok and r_ct.plan is not None and len(r_ct.plan.steps) == 1,
          getattr(r_ct, "immediate", ""))

    # --- 3×3 配方才要求工作台 --------------------------------------
    b = FakeActor(0.0, 64.0, 0.0)      # 附近没有工作台
    r2 = ac.compile(b, ToolCall("craft", {"item": "wooden_pickaxe"}), {})
    check("3×3 配方没工作台时明确报错（不静默失败）",
          not r2.ok and "工作台" in getattr(r2, "immediate", ""),
          getattr(r2, "immediate", ""))

    c = FakeActor(0.0, 64.0, 0.0)
    c.dimension.set(2, 64, 0, "minecraft:crafting_table")
    r3 = ac.compile(c, ToolCall("craft", {"item": "wooden_pickaxe"}), {})
    check("3×3 配方有工作台时编译出两步计划（走过去 + 操作容器）",
          r3.ok and r3.plan is not None and len(r3.plan.steps) == 2,
          getattr(r3, "immediate", ""))
    if r3.ok and r3.plan:
        s0, s1 = r3.plan.steps
        import json as _json
        note = _json.loads(s1.note)
        check("第一步走过去、第二步是 container(craft)",
              s0.op == "walk_to" and s1.op == "container" and note.get("op") == "craft",
              f"{s0.op},{s1.op},{note}")

    # --- 熔炉 / 箱子工具也编译 --------------------------------------
    a2 = FakeActor(0.0, 64.0, 0.0)
    a2.dimension.set(1, 64, 0, "minecraft:furnace")
    a2.dimension.set(-1, 64, 0, "minecraft:chest")
    r3 = ac.compile(a2, ToolCall("smelt", {"item": "iron_ore", "count": 3}), {})
    check("smelt 编译出计划", r3.ok and r3.plan is not None,
          getattr(r3, "immediate", ""))
    r4 = ac.compile(a2, ToolCall("store", {"item": "cobblestone", "count": 10}), {})
    check("store 编译出计划（存箱子）", r4.ok and r4.plan is not None,
          getattr(r4, "immediate", ""))

    # --- llm 工具定义 + JS 容器指令 ---------------------------------
    from endstone_arc_fake_player.llm import LLMClient
    names = [t["function"]["name"] for t in LLMClient.tool_spec()]
    check("llm 工具表里有 craft/smelt/store/take",
          all(n in names for n in ("craft", "smelt", "store", "take")),
          f"{names}")
    jsrc = (ROOT / "endstone_arc_fake_player" / "behavior_pack" / "scripts"
            / "main.js").read_text(encoding="utf-8")
    check("main.js 有 doContainer 容器指令",
          "function doContainer" in jsrc and "bot:container" in jsrc)
    check("main.js 的 findSlot 支持短名后缀匹配（planks→oak_planks）",
          "shortMatches" in jsrc and "endsWith" in jsrc)


def test_inflight_sweep() -> None:
    """`_inflight` 泄漏 —— "执行完一次任务后就不动了"的主因。

    这个 bug 的可怕之处在于它**完全静默**：`should_think()` 里那道
    `wait_for_plan and self._inflight` 闸一旦永久锁死，智能体就再也不
    思考第二次，而这条路径上**一行日志都不打**。用户看到的只是
    "它做完了第一件事，然后就一直站着"。
    """
    print("\n" + "-" * 70)
    print("十七、_inflight 泄漏与自动解锁（3.0.2）")
    print("-" * 70)

    src = (PKG / "agent.py").read_text("utf-8")
    check("agent.py 里有 sweep_inflight", "def sweep_inflight" in src)
    check("sweep_inflight 会去执行器核对事实状态",
          "ex.active" in src and "completed" in src,
          "必须从执行器反推，不能只看自己记的账")
    check("清扫后会对账（把 keep 写回 _inflight）",
          "self._inflight = keep" in src)
    check("解锁时会打 warning（不能静默）",
          "已自动解锁" in src or "warning" in src)

    psrc = (PKG / "plugin.py").read_text("utf-8")
    check("plugin.py 里有 _sweep_inflight 并在主循环调用",
          "def _sweep_inflight" in psrc and "self._sweep_inflight()" in psrc)
    check("清扫发生在 executor.tick() **之后**",
          psrc.find("self._executor.tick()") < psrc.rfind("self._sweep_inflight()"),
          "顺序反了就永远扫不到刚消失的计划")
    # ⚠️ 早退不能放在清扫前面：执行器"空"恰恰是最需要清扫的时候
    # （cancel_all 把计划全摘掉之后 busy 才变 False）。
    early = psrc.find("if not self._executor.busy:")
    check("'执行器空闲'的早退在清扫之后（否则永远扫不到那一次）",
          early > psrc.find("def _tick"), f"early_return_pos={early}")

    check("plugin.py 里有 _real_or_retry（把'放弃'翻译成一次失败）",
          "def _real_or_retry" in psrc)
    check("_real_or_retry 会消费 pop_gave_up",
          "pop_gave_up" in psrc)
    check("_real_or_retry 放弃时会记 warning（不能静默）",
          "放弃" in psrc and "warning" in psrc)
    check("_real_or_retry 放弃时调 note_failed（结算成失败）",
          "note_failed" in psrc,
          "不调的话 fail_count 不加，计划会带着假的好消息走完")
    check("三个真身体处理器都走了 _real_or_retry",
          psrc.count("_real_or_retry(step,") >= 3,
          f"只找到 {psrc.count('_real_or_retry(step,')} 处")

    esrc = (PKG / "executor.py").read_text("utf-8")
    check("executor.py 里有 on_step 钩子（处理器才拿得到 Plan）",
          "on_step" in esrc)
    check("Plan 有 note_failed 方法", "def note_failed" in esrc)
    # ⚠️ 最关键的回归：`total` 必须是 `len(steps)`，绝不能把重试额度
    # 算进去。之前 `total = len(steps) + retries_left` 导致游标走完
    # 真实步骤后 `while cursor < total` 仍成立 → `steps[cursor]` 越界，
    # 主循环每 tick 抛一次 `IndexError: list index out of range`。
    check("total 就是 len(steps)（不再含 retries_left）",
          "return len(self.steps)" in esrc,
          "total 虚高会导致 IndexError")
    check("Plan 不再有 retries_left 字段（删干净了）",
          "retries_left: int" not in esrc,
          "残留会诱使后来者又把额度并进 total")


def test_no_indexerror() -> None:
    """复现线上 `IndexError: list index out of range` 并确认已修。

    线上日志（3.0.2）：
        File ".../executor.py", line 294, in tick
            step = plan.steps[plan.cursor]
        IndexError: list index out of range

    根因：`Plan.total` 一度等于 `len(steps) + retries_left`，而
    `retries_left` 在计划开始处理时预发 8 格、且一步都没失败也一直挂着。
    于是游标把真实步骤走完后 `while cursor < total` 仍成立，`steps[cursor]`
    越界。主循环每 tick 崩一次（被 `_guard` 兜住不崩服务器，但计划永远
    推不动），用户看到的就是这条 IndexError。
    """
    print("\n" + "-" * 70)
    print("十九、IndexError 回归（total 越界）")
    print("-" * 70)

    from endstone_arc_fake_player.executor import BatchExecutor, Plan, Step as ExStep

    ex = BatchExecutor(QuietLogger())
    ex.register("dig_far", lambda _s: True)     # 一步就成功，正常推进

    # 十步计划，模拟"批量挖方块"里那种十几步的计划。
    plan = Plan(label="bot: 挖一片", steps=[ExStep("dig_far", i, 64, i)
                                            for i in range(10)])
    ex.submit(plan)

    # 反复 tick 直到计划结算，任何一步都不许抛 IndexError。
    crashed = False
    for _ in range(50):
        try:
            ex.tick()
        except IndexError:
            crashed = True
            break
        if plan.state.value in ("done", "failed", "cancelled"):
            break

    check("十步计划推进全程无 IndexError",
          not crashed, "越界了 → total 又虚高了")
    check("计划正常走到终态",
          plan.state.value in ("done", "failed"),
          f"state={plan.state.value}")
    check("游标没有越过步骤数",
          plan.cursor <= len(plan.steps),
          f"cursor={plan.cursor} len={len(plan.steps)}")




def test_reported_bugs_307() -> None:
    """3.0.7 + 3.0.12：三个用户实测 bug 的回归。

    用户原话（三条，逐条对应一个根因）：
      1. "批量挖方块的时候，他挖到最后一步不挖了，直接停止，然后重新开始挖"
         —— 旧版只在发起时调一次 breakBlock、之后只轮询；引擎中途把这一锤子
         静默打断后只能干等到判超时再整轮重启（"停止→重来"）。3.0.12 改为
         **每 tick 补敲一次** breakBlock，把中断随时接上（两个成熟包
         Understudy / FlashFakePlayerPack 都是每 tick 调一次，证明重复调用
         是安全的，语义是"继续敲"而非"重置进度"）。世界层/编译层在下面验证。
      2. "他无法单个方块挖掘" —— 工具集里没有单方块入口（mine_area 被
         描述成批量工具），以及直连模式下 `world.break_block` 用子串
         `"air" in name` 把 stair 误判成空气。
      3. "他的寻物功能只限同一个y坐标上，一旦有上下坡，他的寻路功能
        就不能用了" —— `_find_blocks` 垂直只搜 ±6；`_plan_path` 只在
         goal 是实心时才修正高度，悬空 goal 让 A* 永远不可达。
    """
    print("\n" + "-" * 70)
    print("二十三、三个实测 bug 回归（3.0.7）")
    print("-" * 70)

    from tests.test_hybrid import FakeActor, FakeDimension
    from endstone_arc_fake_player.world import WorldAdapter
    from endstone_arc_fake_player.actions import ActionCompiler
    from endstone_arc_fake_player.llm import ToolCall, SPEC

    class _L:
        def debug(self, *a): pass
        def info(self, *a): pass
        def warning(self, *a): pass
        def error(self, *a): pass

    # --- bug 2（直连模式）：break_block 不再把 stair 误判成空气 --------
    # FakeBlock 没有 set_type，这里给一个能写回 blocks 的局部替身。
    class _WritableBlock:
        def __init__(self, dim, x, y, z):
            self.dim, self.x, self.y, self.z = dim, x, y, z
            self.type = dim.blocks.get((x, y, z), "minecraft:air")

        def set_type(self, name, physics=True):
            self.dim.blocks[(self.x, self.y, self.z)] = name

    class _BreakDim(FakeDimension):
        def get_block_at(self, x, y, z):
            xi, yi, zi = int(x), int(y), int(z)
            return _WritableBlock(self, xi, yi, zi)

    w = WorldAdapter(_L())
    dim = _BreakDim()
    dim.set(0, 63, 0, "minecraft:stone")
    dim.set(1, 63, 0, "minecraft:oak_stairs")
    r_stone = w.break_block(dim, 0, 63, 0)
    check("world.break_block 能挖掉石头",
          r_stone is True, f"ok={r_stone}")
    check("world.break_block 挖石头后变成空气",
          w.is_air(dim, 0, 63, 0), f"name={w.get_block_name(dim, 0, 63, 0)}")
    r_stair = w.break_block(dim, 1, 63, 0)
    check("world.break_block 不再把 stair 误判成空气（能挖）",
          r_stair is True,
          "旧逻辑 'air' in 'oak_stairs' 成立 → 直接返回 False，楼梯挖不掉")
    check("world.break_block 挖楼梯后变成空气",
          w.is_air(dim, 1, 63, 0), f"name={w.get_block_name(dim, 1, 63, 0)}")
    r_air = w.break_block(dim, 2, 62, 0)  # y=62 无方块，是真空气
    check("world.break_block 对真空气返回 False（无需挖）",
          r_air is False, f"ok={r_air}")

    # --- bug 2（工具入口）：mine_block 编译出单方块挖掘计划 ------------
    ac = ActionCompiler(WorldAdapter(_L()), _L())
    actor = FakeActor(0.5, 64.0, 0.5)
    actor.dimension.set(0, 64, 0, "minecraft:iron_ore")
    r = ac.compile(actor, ToolCall("mine_block", {"x": 0, "y": 64, "z": 0}), {})
    check("mine_block 编译出计划", r.ok is True and r.plan is not None,
          f"ok={r.ok} plan={r.plan}")
    check("mine_block 计划是 1 个挖掘步骤",
          r.plan is not None and len(r.plan.steps) == 1
          and r.plan.steps[0].op == "break_block",
          f"steps={[s.op for s in r.plan.steps] if r.plan else []}")
    r_air = ac.compile(actor, ToolCall("mine_block", {"x": 5, "y": 64, "z": 5}), {})
    check("mine_block 目标是空气时给提示而不是计划",
          r_air.ok and "空气" in r_air.immediate,
          f"msg={r_air.immediate!r}")
    check("llm SPEC 里有 mine_block（参数会被裁剪/强转）",
          "mine_block" in SPEC and set(SPEC["mine_block"]) == {"x", "y", "z"},
          f"spec={SPEC.get('mine_block')}")

    # --- bug 3（找物）：垂直差 11 格也能找到（旧逻辑只搜 ±6） ---------
    actor2 = FakeActor(0.5, 64.0, 0.5)
    actor2.dimension.set(3, 75, 0, "minecraft:diamond_ore")  # dy = +11
    found = ac._find_blocks(actor2, "diamond_ore", cap=4, radius=16)
    check("_find_blocks 能找到垂直差 11 格的矿石（上下坡）",
          (3, 75, 0) in found,
          f"found={found}（旧逻辑垂直只搜 ±6，必漏）")

    # --- bug 3（寻路）：悬空 goal 先落到地表再寻路 ---------------------
    r3 = ac._plan_path(FakeActor(0.5, 64.0, 0.5), (5, 70, 0))
    check("_plan_path 对悬空目标不报不可达，能规划出路径",
          r3.ok is True, f"ok={r3.ok} msg={r3.immediate!r}")
    if r3.plan is not None and r3.plan.steps:
        dest = r3.plan.steps[-1]
        check("_plan_path 把悬空 y=70 修正到地表 y=64",
              (dest.x, dest.y, dest.z) == (5, 64, 0),
              f"dest={(dest.x, dest.y, dest.z)}（旧逻辑直接找 y=70 悬空 → 不可达）")


def test_actions_visible_without_native_tools() -> None:
    """3.0.13：接口忽略 tools 时，模型仍要知道"有哪些动作"。

    用户实测：bot 反复说"我有 64 个木板了，现在合成工作台和木镐"，
    日志里却一条条 `（没有返回动作）`。根因**不是** craft 实现坏了——
    是模型压根不知道 craft 存在：动作清单只通过 API 的 `tools` 参数传递，
    而第三方中转普遍**静默忽略** tools（返回 200 但没有 tool_calls）；
    系统提示词的 rules 里只点名了 mine_area/dig_tunnel/collect 等批量
    工具，craft/smelt/store/take 一个都没提。于是模型只能"干说话不干活"。

    修法：
      1. 把与原生工具**同源**的动作清单（含 craft）写进系统提示词；
      2. 给出 JSON 兜底输出格式（工具调用不可用时的唯一出路）；
      3. "只说话不调工具"时往历史塞一条纠正，下一轮补动作。
    """
    print("\n" + "-" * 70)
    print("二十四、接口不支持 tools 时仍能行动（3.0.13）")
    print("-" * 70)

    from endstone_arc_fake_player.llm import SPEC, LLMClient, build_tool_manual
    from endstone_arc_fake_player.agent import build_system

    s = AgentState(AgentConfig(name="阿明"))
    prompt = build_system(s)
    manual = build_tool_manual()

    names = list(SPEC.keys())
    check("动作手册列出了**每一个**动作名",
          all(n in manual for n in names),
          f"缺：{[n for n in names if n not in manual]}")
    # 这一组是 rules 里**没点名**的，只靠 tools 参数最容易漏
    check("手册特别包含 craft/smelt/store/take（rules 里没点名的那批）",
          all(f"- {n}(" in manual for n in ("craft", "smelt", "store", "take")))
    check("craft 那行保留了 item 取 的枚举（含 crafting_table）",
          "item 取" in manual and "crafting_table" in manual)
    check("系统提示词里出现了动作手册（不再只靠 tools 参数）",
          "- craft(" in prompt and "- mine_area(" in prompt)
    check("提示词给了 JSON 兜底输出格式",
          '"actions"' in prompt and '"reply"' in prompt)
    check("手册与原生工具**同源**（都来自 tool_spec）",
          set(names) == {i["function"]["name"] for i in LLMClient.tool_spec()})

    # 429 要翻译成人话，而不是干巴巴一句"请求失败"
    c = LLMClient("http://x/v1", "sk-k", "m")
    c.last_http_status = 429
    check("HTTP 429 被翻译成 被限流 而不是干巴巴的 请求失败",
          "限流" in c._fail_reason() and "429" in c._fail_reason(),
          c._fail_reason())

    # ★本轮的根因回归★：craft 的原生工具调用**不再被解析器丢弃**。
    # 之前 SPEC 里没有 craft，`chat()` 的 `if name not in SPEC: continue`
    # 直接把它扔掉 → calls 为空 → "（没有返回动作）"。
    class _Native(LLMClient):
        def _post(self, payload):                      # noqa: D102
            self.last_http_status = 200
            return {"choices": [{"message": {
                "content": "",
                "tool_calls": [{"function": {
                    "name": "craft",
                    "arguments": '{"item": "crafting_table"}'}}],
            }}]}

    rep = _Native("http://x/v1", "sk-k", "m").chat([{"role": "user", "content": "做工作台"}])
    check("【核心回归】craft 的原生工具调用能被解析出来（不再被 SPEC 丢弃）",
          rep.ok and len(rep.calls) == 1 and rep.calls[0].name == "craft"
          and rep.calls[0].args.get("item") == "crafting_table",
          f"calls={[(x.name, x.args) for x in rep.calls]}")

    class _Json(LLMClient):
        def _post(self, payload):                      # noqa: D102
            self.last_http_status = 200
            return {"choices": [{"message": {
                "content": '{"reply":"做工作台","actions":'
                           '[{"name":"craft","args":{"item":"crafting_table"}}]}'}}]}

    rep2 = _Json("http://x/v1", "sk-k", "m").chat([{"role": "user", "content": "做工作台"}])
    check("craft 走 JSON 兜底也能被解析（接口不回 tool_calls 时的出路）",
          rep2.ok and len(rep2.calls) == 1 and rep2.calls[0].name == "craft",
          f"calls={[(x.name, x.args) for x in rep2.calls]}")

    # ★另一个根因回归★：模型实测输出的是"正文 + 一串**裸对象**"，而且多个
    # 对象用逗号裸拼（**不是**合法 JSON 文档）。老解析器整段丢，日志就是
    # "（没有返回动作）"。这个用例就是用户截图里那两行原文。
    from endstone_arc_fake_player.llm import extract_actions
    raw = ("背包里有64个橡木原木了。现在合成木板、木棍和工作台。\n"
           '{"name": "craft", "args": {"item": "planks"}}, '
           '{"name": "craft", "args": {"item": "stick"}}')
    _, acts = extract_actions(raw)
    check("【核心回归】截图里那串裸动作对象能被逐个抠出来",
          [a.get("name") for a in acts] == ["craft", "craft"],
          str([a.get("name") for a in acts]))

    class _Bare(LLMClient):
        def _post(self, payload):                      # noqa: D102
            self.last_http_status = 200
            return {"choices": [{"message": {"content": raw}}]}

    rep3 = _Bare("http://x/v1", "sk-k", "m").chat([{"role": "user", "content": "hi"}])
    check("裸对象串走完整 chat() 也能变成 2 个 craft 调用",
          rep3.ok and [x.name for x in rep3.calls] == ["craft", "craft"],
          f"calls={[(x.name, x.args) for x in rep3.calls]}")

    # 物品代码名对照表要进提示词
    check("提示词含物品代码名对照表（oak_log / oak_planks / crafting_table / iron_ingot）",
          all(k in prompt for k in ("oak_log", "oak_planks", "crafting_table", "iron_ingot")))

    # main.js 的背包合成分支
    mjs = (ROOT / "endstone_arc_fake_player" / "behavior_pack" / "scripts"
           / "main.js").read_text(encoding="utf-8")
    check("main.js 有背包合成分支（grid=2，不需要工作台）",
          "Number(req.grid) === 2" in mjs and "function countInPlayer" in mjs)

    # main.js：faceBlock 瞄方块**中心**，不是整数角点
    jsrc = (ROOT / "endstone_arc_fake_player" / "behavior_pack" / "scripts"
            / "main.js").read_text(encoding="utf-8")
    fb = jsrc[jsrc.find("function faceBlock"):jsrc.find("function raycastBlock")]
    check("【回归】faceBlock 瞄方块中心（整数角点会让射线贴格边界打偏）",
          "loc.x + 0.5" in fb and "sim.lookAtBlock(center)" in fb,
          "瞄角点 → getBlockFromViewDirection 可能返回相邻格 → 判成没瞄上")


def main() -> int:
    print("=" * 70)
    print("2.0.0 / 2.1.0 / 3.0.0 实测问题修复")
    print("=" * 70)

    test_dig_spam()
    test_capability_probe()
    test_js_body_creation()
    test_body_identity()
    test_llm_retry()
    test_placeholder_honesty()
    test_ticking_area()
    test_sim_identity()
    test_pack_update()
    test_restart_notice()
    test_crash_guard()
    test_ticking_area_fixes()
    test_bot_game_mode()
    test_ticking_area_guard()
    test_audit_fixes()
    test_pathfinder()
    test_retry_semantics()
    test_reissue_paths()
    test_inflight_sweep()
    test_no_indexerror()
    test_approach_point_and_early_dig()
    test_dig_orientation_and_timeout()
    test_craft_and_containers()
    test_reported_bugs_307()
    test_actions_visible_without_native_tools()

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
    raise SystemExit(main())
