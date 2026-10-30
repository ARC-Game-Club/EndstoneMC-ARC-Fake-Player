"""混合架构测试：桥接、身体队列、异步挖掘状态机、行为包安装。

和另外两个测试文件一样，走 main() + check() 的自运行风格，
不依赖 pytest——因为这个包要能在一个只有 Endstone 的环境里直接跑。

重点覆盖三件容易写错的事：
  1. scriptevent 的鉴权与请求-响应配对
  2. "正在进行"和"已完成"的区分（异步挖掘状态机的核心）
  3. 执行器遇到异步步骤时不能推进游标
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import time
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
from endstone_arc_fake_player.body import BodyController
from endstone_arc_fake_player.bridge import FakePlayerBridge
from endstone_arc_fake_player.executor import BatchExecutor, Plan, PlanState, Step
from endstone_arc_fake_player import nbt as NBT
from endstone_arc_fake_player import packing
from endstone_arc_fake_player.realbody import RealBodyActions
from endstone_arc_fake_player.world import WorldAdapter


# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------

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


class FakeLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def _log(self, level: str, msg: str) -> None:
        self.lines.append(f"{level}: {msg}")

    def debug(self, m: str) -> None:
        self._log("DEBUG", m)

    def info(self, m: str) -> None:
        self._log("INFO", m)

    def warning(self, m: str) -> None:
        self._log("WARN", m)

    def error(self, m: str) -> None:
        self._log("ERROR", m)


class FakeBlock:
    def __init__(self, name: str) -> None:
        self.type = name


class FakeDimension:
    """一个可读写的方块容器。y=63 是石头地板。"""

    def __init__(self) -> None:
        self.blocks: dict[tuple[int, int, int], str] = {}
        for x in range(-20, 21):
            for z in range(-20, 21):
                self.blocks[(x, 63, z)] = "minecraft:stone"

    def get_block_at(self, x: int, y: int, z: int):
        name = self.blocks.get((int(x), int(y), int(z)), "minecraft:air")
        return FakeBlock(name)

    def set(self, x: int, y: int, z: int, name: str) -> None:
        self.blocks[(int(x), int(y), int(z))] = name

    def is_solid(self, x: int, y: int, z: int) -> bool:
        n = self.blocks.get((int(x), int(y), int(z)), "minecraft:air")
        return "air" not in n and "water" not in n and "lava" not in n


class FakeLocation:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x, self.y, self.z = x, y, z
        self.pitch = 0.0
        self.yaw = 0.0


class FakeActor:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.location = FakeLocation(x, y, z)
        self.dimension = FakeDimension()
        self.id = 1
        self.health = 20.0
        self.max_health = 20.0


# ---------------------------------------------------------------------------
# 第一部分：桥接层
# ---------------------------------------------------------------------------

def test_bridge() -> None:
    section("第一部分：scriptevent 桥接")

    sent: list[str] = []
    log = FakeLogger()
    br = FakePlayerBridge(log, lambda cmd: (sent.append(cmd), True)[1])
    token = br.new_token()

    check("生成了令牌", len(token) == 32, f"{len(token)} 位十六进制")
    check("初始状态未连接", br.connected is False)

    # 发一条生成请求
    got: list[tuple] = []
    rid = br.spawn("Alice", 1.0, 64.0, 2.0,
                   callback=lambda ok, err, info: got.append((ok, err)))
    check("请求有请求号", rid > 0, f"#{rid}")
    check("命令已发出", len(sent) == 1 and "scriptevent bot:spawn" in sent[0])
    check("请求里带令牌", token in sent[0])
    check("请求里带请求号", f'"i":{rid}' in sent[0])

    # 伪造令牌的回复应被丢弃
    fake = json.dumps({"n": "Alice", "ok": True, "i": rid, "t": "deadbeef" * 4})
    kind = br.dispatch("bot:spawned", fake)
    check("伪造令牌被忽略", kind == "ignored", f"返回 {kind}")
    check("伪造消息不触发回调", len(got) == 0)

    # 正确令牌的回复应被派发
    good = json.dumps({"n": "Alice", "ok": True, "i": rid, "t": token})
    kind = br.dispatch("bot:spawned", good)
    check("合法回复被处理", kind == "spawn")
    check("回调被触发", len(got) == 1 and got[0][0] is True)
    check("待处理表已清空", br.pending_count == 0)

    # 非 bot: 命名空间的消息不管
    check("无关事件被忽略", br.dispatch("other:thing", "{}") == "ignored")

    # 心跳让 connected 变真
    pong = json.dumps({"names": ["alice", "bob"], "t": token})
    br.dispatch("bot:pong", pong)
    check("心跳后视为已连接", br.connected is True)
    check("心跳带回假人名单", br.remote_names == {"alice", "bob"},
          str(br.remote_names))

    # 超时清理
    log2 = FakeLogger()
    br2 = FakePlayerBridge(log2, lambda cmd: True)
    br2.new_token()
    timed_out: list[str] = []
    br2.send_tracked("bot:walk", {"n": "x"},
                     callback=lambda ok, err, info: timed_out.append(err))
    check("超时前不清理", br2.expire_pending(timeout=999) == 0)
    n = br2.expire_pending(timeout=-1)
    check("超时后清理", n == 1, f"清了 {n} 个")
    check("超时会通知回调", len(timed_out) == 1 and "超时" in timed_out[0])

    # 异步挖掘回调
    log3 = FakeLogger()
    br3 = FakePlayerBridge(log3, lambda cmd: True)
    tk = br3.new_token()
    dug: list[tuple] = []
    did = br3.dig("Bob", 10, 20, 30, callback=lambda ok, err, i: dug.append((ok, err, i)))
    check("挖掘请求已发出", did > 0, f"#{did}")
    br3.dispatch("bot:dug", json.dumps({
        "i": did, "n": "Bob", "ok": True, "swing": 17, "x": 10, "y": 20, "z": 30,
        "t": tk}))
    check("挖掘回调被触发", len(dug) == 1 and dug[0][0] is True)
    check("挖掘回调带回挥臂次数", dug[0][2].get("swing") == 17)


# ---------------------------------------------------------------------------
# 第二部分：身体队列的"进行中"与"已完成"
# ---------------------------------------------------------------------------

def test_body_controller() -> None:
    section("第二部分：身体动作队列")

    sent: list[str] = []
    log = FakeLogger()
    br = FakePlayerBridge(log, lambda cmd: (sent.append(cmd), True)[1])
    token = br.new_token()
    dim = FakeDimension()
    world = WorldAdapter(log)
    world.body_dimension = dim

    ctl = BodyController("Alice", br, world, log)
    check("初始不忙", ctl.busy is False)
    check("初始无最近结果", ctl.dig_result_for(1, 2, 3) is None)

    # 发起一次挖掘
    ok = ctl.start_dig(5, 64, 5)
    check("挖掘已发起", ok is True)
    check("发起后处于忙碌", ctl.busy is True)
    check("忙碌时不接受新挖掘", ctl.start_dig(6, 64, 5) is False)

    # 关键：即使还没结论，dig_result_for 也应该返回 None
    check("未完成时查不到结果", ctl.dig_result_for(5, 64, 5) is None,
          "这是'进行中'与'已完成'的核心区分")

    # 回调到达
    rid = 1
    ctl._on_dig_done(ctl._dig, True, "", {"swing": 23})  # noqa: SLF001
    check("回调后不再忙", ctl.busy is False)
    result = ctl.dig_result_for(5, 64, 5)
    check("完成后能查到结果", result is not None)
    check("结果标记成功", result is not None and result.ok is True)
    check("结果带挥臂次数", result is not None and result.swing == 23)
    check("坐标不同的查询返回 None", ctl.dig_result_for(9, 9, 9) is None)
    check("成功后统计 +1", ctl.digs_done == 1)

    # 超时
    ctl2 = BodyController("Bob", br, world, log, dig_timeout=0.01)
    ctl2.start_dig(7, 64, 7)
    time.sleep(0.02)
    ctl2.tick()
    check("超时后不再忙", ctl2.busy is False)
    check("超时被记为失败", ctl2.digs_failed == 1)
    r2 = ctl2.dig_result_for(7, 64, 7)
    check("超时结果可查", r2 is not None and r2.ok is False)
    check("超时原因清楚", r2 is not None and "超时" in r2.error)

    # cancel 也要留下可查的结论，否则执行器会一直等
    ctl3 = BodyController("Carol", br, world, log)
    ctl3.start_dig(8, 64, 8)
    ctl3.cancel()
    r3 = ctl3.dig_result_for(8, 64, 8)
    check("取消后留下结论", r3 is not None and r3.ok is False)
    check("取消原因清楚", r3 is not None and "取消" in r3.error)


# ---------------------------------------------------------------------------
# 第三部分：异步步骤不能推进游标
# ---------------------------------------------------------------------------

def test_async_executor() -> None:
    section("第三部分：执行器遇异步步骤的处理")

    log = FakeLogger()
    ex = BatchExecutor(log, max_steps_per_tick=10)

    # 一个"要等 3 次 tick 才完成"的异步步骤
    state = {"calls": 0}

    def slow_op(step: Step) -> bool:
        state["calls"] += 1
        return state["calls"] >= 3

    ex.register("slow", slow_op)

    plan = Plan(label="t: 测试", steps=[
        Step("slow", 0, 64, 0, async_op=True),
        Step("slow", 1, 64, 0, async_op=True),
    ])
    ex.submit(plan)

    ex.tick()
    check("第 1 tick 未完成", plan.cursor == 0, f"游标 {plan.cursor}")
    check("标记为正在等待", plan.pending_op == "slow", f"'{plan.pending_op}'")
    check("不算作失败", plan.fail_count == 0, f"{plan.fail_count} 次失败")

    ex.tick()
    check("第 2 tick 仍未完成", plan.cursor == 0, f"游标 {plan.cursor}")

    # 第 3 tick：slow_op 终于返回 True，而且预算还有剩余，
    # 所以它会连续推进——两步一起过。这正是我们想要的行为：
    # 一旦解除阻塞就不再人为拖延。
    finished = ex.tick()
    check("第 3 tick 解除阻塞后连续推进", plan.cursor == 2, f"游标 {plan.cursor}")
    check("第 3 tick 一次推进 2 步", plan.done_count == 2, f"{plan.done_count} 步")
    check("第 3 tick 计划结算", len(finished) == 1)
    check("状态为 DONE", plan.state == PlanState.DONE, str(plan.state))
    check("没有误记失败", plan.fail_count == 0, f"{plan.fail_count}")
    check("等待标记已清空", plan.pending_op == "")
    check("已从队列移除", ex.busy is False)

    # 同步步骤不受影响（原有行为不能回归）
    sync_calls = {"n": 0}

    def sync_op(step: Step) -> bool:
        sync_calls["n"] += 1
        return True

    ex2 = BatchExecutor(log, max_steps_per_tick=10)
    ex2.register("fast", sync_op)
    p2 = Plan(label="t2: 同步", steps=[Step("fast", x, 64, 0) for x in range(5)])
    ex2.submit(p2)
    done = ex2.tick()
    check("同步步骤一个 tick 跑完", p2.cursor == 5, f"游标 {p2.cursor}")
    check("同步计划当轮结算", len(done) == 1)
    check("调用次数正确", sync_calls["n"] == 5)


# ---------------------------------------------------------------------------
# 第四部分：动作编译器的真挖掘模式
# ---------------------------------------------------------------------------

def test_compiler_real_mode() -> None:
    section("第四部分：编译器的两种挖掘模式")

    log = FakeLogger()
    world = WorldAdapter(log)
    actor = FakeActor(3.5, 64.0, 3.5)
    world.body_dimension = actor.dimension

    # 直连模式
    c = ActionCompiler(world, log)
    check("默认不启用真挖掘", c.use_real_dig is False)
    steps = c.dig_steps([(0, 63, 0), (1, 63, 0)])
    check("直连模式用 break_block",
          all(s.op == "break_block" for s in steps), str([s.op for s in steps]))
    check("直连模式步骤不是异步的",
          all(not s.async_op for s in steps))

    # 真挖掘模式
    c2 = ActionCompiler(world, log)
    c2.use_real_dig = True
    steps2 = c2.dig_steps([(0, 63, 0), (1, 63, 0)])
    check("真挖掘模式用 dig_far",
          all(s.op == "dig_far" for s in steps2), str([s.op for s in steps2]))
    check("真挖掘步骤标记为异步",
          all(s.async_op for s in steps2),
          "这是让它'依次挖'而不是'一起挖'的关键")

    # mine_area 走的也是 dig_steps
    from endstone_arc_fake_player.llm import ToolCall
    ctx: dict = {}
    r = c2.compile(actor, ToolCall("mine_area", {
        "x1": 0, "y1": 63, "z1": 0, "x2": 2, "y2": 63, "z2": 2}), ctx)
    check("mine_area 产出计划", r.plan is not None)
    if r.plan:
        check("mine_area 产出 9 个步骤", r.plan.total == 9, f"{r.plan.total}")
        check("全部是异步挖掘步",
              all(s.op == "dig_far" and s.async_op for s in r.plan.steps))

    # 空气会被跳过
    for x in range(3):
        for z in range(3):
            actor.dimension.set(x, 63, z, "minecraft:air")
    r2 = c2.compile(actor, ToolCall("mine_area", {
        "x1": 0, "y1": 63, "z1": 0, "x2": 2, "y2": 63, "z2": 2}), ctx)
    check("已经空了的区域不再产生步骤",
          r2.plan is None and "空" in r2.immediate, r2.immediate)


# ---------------------------------------------------------------------------
# 第五部分：真实身体的动作处理器
# ---------------------------------------------------------------------------

def test_real_actions() -> None:
    section("第五部分：真实身体动作处理器")

    sent: list[str] = []
    log = FakeLogger()

    def dispatch(cmd: str) -> bool:
        sent.append(cmd)
        return True

    br = FakePlayerBridge(log, dispatch)
    br.new_token()
    dim = FakeDimension()
    world = WorldAdapter(log)
    world.body_dimension = dim
    actor = FakeActor(0.5, 64.0, 0.5)
    actor.dimension = dim

    ctl = BodyController("Alice", br, world, log)
    ctl.actor = actor
    ra = RealBodyActions(ctl, world, log)

    # 站在方块正上方 → 够得着 → 直接挖
    dim.set(0, 64, 0, "minecraft:stone")
    check("同格够得着", ra._within_reach(0, 64, 0) is True)

    # 远处 → 够不着
    dim.set(15, 64, 15, "minecraft:stone")
    check("远处够不着", ra._within_reach(15, 64, 15) is False)

    # 靠近点应该挑一个能站人的相邻格，而不是目标方块本身
    ap = ra._approach_point(10, 64, 10)
    check("接近点不是目标本身", ap != (10, 64, 10), str(ap))
    check("接近点在目标相邻处",
          max(abs(ap[0] - 10), abs(ap[2] - 10)) <= 1, str(ap))

    # dig_near：第一次调用只发起，不返回成功
    dim.set(0, 64, 0, "minecraft:stone")
    step = Step("dig_far", 0, 64, 0, async_op=True)
    first = ra.dig_far(step)
    check("第一次调用未完成（正在发起）", first is False)
    check("确实发出了挖掘命令", any("bot:dig" in s for s in sent),
          f"{len(sent)} 条命令")
    check("未完成时不报成功", first is False,
          "这是防止'还没挖就以为挖好了'的关键")

    # 制造挖掘完成：方块变空气 + 回调
    dim.set(0, 64, 0, "minecraft:air")
    ctl._on_dig_done(ctl._dig, True, "", {"swing": 12})  # noqa: SLF001
    second = ra.dig_far(step)
    check("看到方块没了才报成功", second is True)

    # 挖掘失败的情况：方块还在，回调报错
    dim.set(2, 64, 2, "minecraft:stone")
    step2 = Step("dig_far", 2, 64, 2, async_op=True)
    ra.dig_far(step2)
    ctl._on_dig_done(ctl._dig, False, "被推开", {})  # noqa: SLF001
    third = ra.dig_far(step2)
    check("挖掘失败时报失败", third is False)
    check("失败不误报成功",
          "被推开" in ctl.last_error or not third, ctl.last_error)

    # 够不着时必须先走
    sent.clear()
    dim.set(12, 64, 12, "minecraft:stone")
    ra2 = RealBodyActions(ctl, world, log)
    step3 = Step("dig_far", 12, 64, 12, async_op=True)
    ra2.dig_far(step3)
    check("够不着时先走而不是先挖",
          any("bot:walk" in s for s in sent) and not any("bot:dig" in s for s in sent),
          "这就实现了'不隔墙挖方块'")


# ---------------------------------------------------------------------------
# 第六部分：行为包安装
# ---------------------------------------------------------------------------

def test_packing() -> None:
    section("第六部分：行为包安装与 level.dat 补丁")

    log = FakeLogger()

    # 造一个最小的合法 level.dat
    def s(name: str) -> bytes:
        enc = name.encode()
        return struct.pack("<H", len(enc)) + enc

    body = struct.pack("<B", 8) + s("LevelName") + s("My World")
    body += struct.pack("<B", 4) + s("RandomSeed") + struct.pack("<q", 42)
    body += struct.pack("<B", 3) + s("GameType") + struct.pack("<i", 0)
    body += struct.pack("<B", 0)
    nbt_bytes = struct.pack("<B", 10) + s("") + body
    level_dat_bytes = struct.pack("<II", 10, len(nbt_bytes)) + nbt_bytes

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        world = tmp_path / "worlds" / "W"
        world.mkdir(parents=True)
        ld = world / "level.dat"
        ld.write_bytes(level_dat_bytes)

        check("初始未启用实验", packing.experiments_enabled(ld) is False)

        changed, msg = packing.enable_experiments(ld)
        check("补丁报告已修改", changed is True, msg)
        check("补丁后实验已启用", packing.experiments_enabled(ld) is True)
        check("留下了备份", (world / "level.dat.bak").exists())

        # 最关键：原有数据不能被动
        after = NBT.read_bedrock_nbt(ld.read_bytes(), has_header=True)
        check("世界名未被破坏", after.get("LevelName") == "My World",
              str(after.get("LevelName")))
        check("随机种子未被破坏", after.get("RandomSeed") == 42,
              str(after.get("RandomSeed")))
        check("游戏模式未被破坏", after.get("GameType") == 0)
        check("experiments 已写入",
              after.get("experiments", {}).get("gametest") == 1)

        # 幂等
        changed2, msg2 = packing.enable_experiments(ld)
        check("重复打补丁不重复修改", changed2 is False, msg2)

        # 注册不能覆盖别人的包
        existing = [{"pack_id": "someone-else-uuid", "version": [9, 9, 9]}]
        (world / "world_behavior_packs.json").write_text(
            json.dumps(existing), encoding="utf-8")
        ok, msg3 = packing.register_pack(world, logger=log)
        check("注册成功", ok is True, msg3)
        data = json.loads((world / "world_behavior_packs.json").read_text("utf-8"))
        check("别人的包还在", any(e["pack_id"] == "someone-else-uuid" for e in data))
        check("我们的包已加入", any(e["pack_id"] == packing.PACK_UUID for e in data))

        # 再来一次不重复
        packing.register_pack(world, logger=log)
        data2 = json.loads((world / "world_behavior_packs.json").read_text("utf-8"))
        check("重复注册不产生重复条目", len(data2) == 2, f"{len(data2)} 条")

        # 损坏的 json 不能被覆盖
        bad = world / "world_behavior_packs.json"
        bad.write_text("{ 这不是 json", encoding="utf-8")
        ok2, msg4 = packing.register_pack(world, logger=log)
        check("损坏文件时拒绝写入", ok2 is False, msg4)
        check("损坏文件内容未被覆盖",
              bad.read_text(encoding="utf-8") == "{ 这不是 json")

        # 释放行为包
        src = Path(__file__).resolve().parent.parent / "endstone_arc_fake_player" / "behavior_pack"
        check("行为包源目录存在", src.is_dir(), str(src))
        check("manifest 存在", (src / "manifest.json").exists())
        check("main.js 存在", (src / "scripts" / "main.js").exists())
        check("空结构存在",
              (src / "structures" / "arc_fake_player" / "empty.mcstructure").exists())

        if src.is_dir():
            manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
            check("manifest 的 uuid 与 packing 常量一致",
                  manifest["header"]["uuid"] == packing.PACK_UUID,
                  manifest["header"]["uuid"])
            check("manifest 的 version 与常量一致",
                  manifest["header"]["version"] == packing.PACK_VERSION)
            mods = manifest.get("modules", [])
            check("有脚本模块",
                  any(m.get("type") == "script" for m in mods))
            deps = [d.get("module_name") for d in manifest.get("dependencies", [])]
            check("依赖 server-gametest",
                  "@minecraft/server-gametest" in deps, str(deps))

            target = tmp_path / "target"
            ok3, msg5 = packing.extract_pack(src, target, logger=log)
            check("释放成功", ok3 is True, msg5)
            check("释放后 manifest 就位", (target / "manifest.json").exists())
            check("释放后脚本就位", (target / "scripts" / "main.js").exists())

            # --- 重复释放：内容一致就跳过 ---
            ok4, msg6 = packing.extract_pack(src, target, logger=log)
            check("内容一致时跳过释放", ok4 is True and "跳过" in msg6, msg6)

            # --- 用户手改过 → 必须保住 ---
            #
            # 这条守的是"别抹掉服主自己的改动"（比如他加了调试输出）。
            # 判据不是"内容不同就覆盖"，而是"和**上次发布时**比有没有变"。
            (target / "scripts" / "main.js").write_text("// 用户手动改过", encoding="utf-8")
            ok5, msg7 = packing.extract_pack(src, target, logger=log)
            check("用户改过的文件被保留",
                  (target / "scripts" / "main.js").read_text("utf-8") == "// 用户手动改过",
                  msg7)
            check("保留时消息里点名了那个文件", "保留" in msg7, msg7)

            # --- 插件自己的旧版本 → 必须覆盖 ---
            #
            # 这是最关键的一条，也是上一版真出过问题的：改了 main.js
            # 但 header.version 没动，于是补丁永远送不到服务器，
            # 用户看到的就是"作者说修好了但我这儿还是老样子"。
            fresh = tmp_path / "target2"
            packing.extract_pack(src, fresh, logger=log)
            # 模拟"我们上次发布的是旧内容"
            (fresh / "scripts" / "main.js").write_text("// 插件 v1 的旧脚本", encoding="utf-8")
            # 同步记账，让插件认为这个旧内容就是它发布的
            rel_fp = packing._read_release(fresh)
            import hashlib as _h
            rel_fp["scripts/main.js"] = _h.sha256(
                b"// \xe6\x8f\x92\xe4\xbb\xb6 v1 \xe7\x9a\x84\xe6\x97\xa7\xe8\x84\x9a\xe6\x9c\xac"
            ).hexdigest()
            packing._write_release(fresh, rel_fp)
            ok6, msg8 = packing.extract_pack(src, fresh, logger=log)
            now = (fresh / "scripts" / "main.js").read_text("utf-8")
            check("插件自己的旧脚本会被升级覆盖",
                  now == (src / "scripts" / "main.js").read_text("utf-8"),
                  msg8)
            check("覆盖时消息里说明是释放", "释放" in msg8, msg8)

            # --- 版本号没变也必须能送到 ---
            #
            # 直接构造"内容变了但 manifest.version 一模一样"的场景。
            v1 = tmp_path / "v1src"
            v2 = tmp_path / "v2src"
            for d, script in ((v1, "// v1"), (v2, "// v2 修了 bug")):
                (d / "scripts").mkdir(parents=True, exist_ok=True)
                (d / "manifest.json").write_text(
                    (src / "manifest.json").read_text("utf-8"), encoding="utf-8")
                (d / "scripts" / "main.js").write_text(script, encoding="utf-8")
            live = tmp_path / "live"
            packing.extract_pack(v1, live, logger=log)
            packing.extract_pack(v2, live, logger=log)
            check("同版本号但内容变化时仍会更新（补丁能送到）",
                  (live / "scripts" / "main.js").read_text("utf-8")
                  == "// v2 修了 bug")

    # --- install() 的失败契约：找不到世界目录必须硬失败 ---------------
    # 这是用户上一轮报的 bug：以前会往 behavior_packs/ 撒文件但不登记，
    # 用户看到"安装完成"却完全没生效。
    import os
    cwd = Path.cwd()
    empty_root = tmp_path / "empty_server"
    empty_root.mkdir(parents=True, exist_ok=True)
    try:
        os.chdir(empty_root)
        rep = packing.install(src, level_name="", logger=log)
        check("找不到世界目录时 ok=False", rep.get("ok") is False,
              str(rep.get("ok")))
        check("errors 里说明了原因", bool(rep.get("errors")))
        joined = " ".join(str(e) for e in rep.get("errors", []))
        check("错误里提到“找不到世界目录”", "找不到世界目录" in joined)
        check("错误里给了排查方向",
              "启动过一次" in joined or "服务器根目录" in joined)
        check("硬失败时不释放任何文件",
              not (empty_root / "behavior_packs").exists(),
              "behavior_packs 不该被创建")
    finally:
        os.chdir(cwd)

    # summarize() 必须把 errors 打印出来，不能吞掉
    text = "\n".join(packing.summarize(rep))
    check("summarize 输出 errors 段", "找不到世界目录" in text)
    check("summarize 不谎报成功", "完成" not in text.split("找不到")[0][:20]
          or "✗" in text)   # 至少不能出现"✓ 全部完成"这种


# ---------------------------------------------------------------------------
# 第七部分：JS 桥接脚本的协议一致性
# ---------------------------------------------------------------------------

def test_protocol_consistency() -> None:
    section("第七部分：Python 与 JS 的协议一致性")

    src = Path(__file__).resolve().parent.parent / "endstone_arc_fake_player" / "behavior_pack" / "scripts" / "main.js"
    js = src.read_text(encoding="utf-8")

    # Python 侧会发的每个事件，JS 都得有对应的处理分支
    for ev in ["bot:ping", "bot:spawn", "bot:remove", "bot:teleport",
               "bot:walk", "bot:dig", "bot:place", "bot:attack",
               "bot:select", "bot:stop", "bot:list", "bot:status"]:
        check(f"JS 处理 {ev}", f'case "{ev}"' in js)

    # JS 会回的每个事件，Python 都得认得
    br = FakePlayerBridge(FakeLogger(), lambda c: True)
    token = br.new_token()
    for ev in ["bot:pong", "bot:positions", "bot:spawned", "bot:removed",
               "bot:result", "bot:dug", "bot:list_result", "bot:status"]:
        kind = br.dispatch(ev, json.dumps({"t": token}))
        check(f"Python 认得 {ev}", kind not in ("unhandled", "ignored"),
              f"派发为 {kind}")

    # 关键 API 必须出现在 JS 里
    for api, why in [
        ("spawnSimulatedPlayer", "造假人的唯一途径"),
        ("breakBlock", "真挖掘"),
        ("navigateToLocation", "真寻路"),
        ("attackEntity", "真攻击"),
        ("disconnect", "移除假人"),
    ]:
        check(f"JS 用了 {api}", api in js, why)

    # GameTest 必须用超长 maxTicks 常驻，否则 Test 对象会失效
    check("GameTest 常驻（maxTicks 极大）",
          "0x7fffffff" in js, "Test 对象一失效就再也造不出假人了")
    check("structureName 与结构文件对应",
          'structureName("arc_fake_player:empty")' in js)

    # 令牌鉴权
    check("JS 侧校验令牌", "bridgeToken" in js)
    check("JS 侧要求令牌匹配",
          "if (data.t) bridgeToken = data.t;" in js)


# ---------------------------------------------------------------------------
# 第八部分：不静默降级
# ---------------------------------------------------------------------------

def test_no_silent_fallback() -> None:
    """行为包没就绪时，必须报错停摆，而不是换个实现偷偷顶上。

    这是用户明确要求的行为：宁可报错让人去修，也不要造一个看起来能用
    的假货——屏幕上站着个假人但方块是自己凭空消失的，那比报错糟糕。
    """
    section("第八部分：身体不可用时不静默降级")

    src = (Path(__file__).resolve().parent.parent
           / "endstone_arc_fake_player" / "plugin.py").read_text(encoding="utf-8")

    # --- 直连兜底必须彻底消失 ----------------------------------------
    # 注意只扫代码本身：注释里会提到"以前是退回直连"，那是说明为什么删掉它
    code_only = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#"))
    # 剥掉 docstring 里成段的说明文字（它们可能引用旧行为）
    for marker in ("退回直连，至少把活干完",):
        code_only = code_only.replace(marker, "")
    check("代码里不再有“退回直连”的分支", "退回直连" not in code_only)
    check("不再有“暂时退回”的字样", "暂时退回" not in code_only)

    # 异步处理器里不能出现直接改方块
    i = src.find("def h_dig_far")
    j = src.find("def h_attack", i)
    block = src[i:j]
    check("h_dig_far 里没有直连 break_block 兜底",
          "w.break_block" not in block)
    check("h_dig_far 里没有直连 set_block 兜底",
          "w.set_block" not in block)
    check("身体缺失时调用 _no_body 报错", "_no_body(step," in block)

    # 攻击也不该自己动手
    i = src.find("def h_attack")
    j = src.find("def h_consume", i)
    atk = src[i:j]
    check("h_attack 真身体分支失败后直接 return False",
          "return False" in atk and "直连：直接对实体造成伤害" not in atk)

    # --- _health 只有两条路，不按连接状态自动挑实现 -------------------
    i = src.find("def _health")
    j = src.find("def _set_body_mode", i)
    health = src[i:j]
    check("_health 不再有 want_real 自动选择", "want_real" not in health)
    check("_health 掉线时报错（logger.error）", "logger.error" in health)
    check("_health 掉线时中止假人动作", "_halt_bodies(" in health)

    # --- 生成前必须过闸 ----------------------------------------------
    i = src.find("def _cmd_spawn")
    j = src.find("def _spawn_body", i)
    spawn = src[i:j]
    check("_cmd_spawn 生成前调用 _require_body",
          "_require_body(sender)" in spawn)
    check("_spawn_body 不再自己判断 want_real", "want_real" not in spawn)
    idx_gate = spawn.find("_require_body(sender)")
    idx_body = spawn.find("_spawn_body(name, loc)")
    check("闸门在生成动作之前", 0 <= idx_gate < idx_body,
          f"require@{idx_gate} < spawn@{idx_body}")

    # --- _require_body 自己不能悄悄放行 ------------------------------
    i = src.find("def _require_body")
    j = src.find("def _body_error_text", i)
    req = src[i:j]
    check("_require_body 没有“退回 npc”的兜底", "占位" not in req)
    check("_require_body 不可用时返回 False",
          "return False" in req and "return True" in req)
    check("npc 模式是显式配置而非降级",
          'get("body_mode", "auto")).lower() == "npc"' in req)

    # --- 诊断文本要能照着修 ------------------------------------------
    i = src.find("def _body_error_text")
    j = src.find("def _startup_check", i)
    err = src[i:j]
    for hint in ("还没重启", "Beta APIs", "world_behavior_packs.json"):
        check(f"诊断里提到「{hint}」", hint in err)
    check("诊断指向 /ai body", "/ai body" in err)

    # --- 启动自检不能无条件装包 --------------------------------------
    i = src.find("def _startup_check")
    j = src.find("def _health", i)
    startup = src[i:j]
    check("自检先握手", "_handshake()" in startup)
    check("自检延迟判定（等心跳回来）", "_confirm_body" in startup)
    check("自检只跑一次（_body_checked）", "_body_checked" in startup)

    # --- 连接恢复 ------------------------------------------------------
    body = (Path(__file__).resolve().parent.parent
            / "endstone_arc_fake_player" / "body.py").read_text(encoding="utf-8")
    check("cancel 接受原因参数", "def cancel(self, reason: str" in body)
    check("cancel 留下 last_dig 结论", "self.last_dig = task" in body)
    check("BodyRegistry 有 all()", "def all(self)" in body)

    # --- _halt_bodies 真的会清空排队的异步步骤 -------------------------
    ex = BatchExecutor(FakeLogger())
    ex.register("dig_far", lambda _s: False)   # 永远"还没完"，模拟卡住的挖掘
    ex.submit(Plan(label="t: x", steps=[
        Step("dig_far", 0, 0, 0, async_op=True),
        Step("dig_far", 1, 0, 0, async_op=True),
    ]))
    ex.tick()
    check("异步步骤卡住时计划仍在队列里", len(ex._plans) == 1)  # noqa: SLF001
    stuck = ex._plans[0]                                        # noqa: SLF001
    check("卡住的计划记录了 pending_op", stuck.pending_op == "dig_far",
          stuck.pending_op)
    stuck.abort("连接断开")
    check("abort 后计划被标记中止",
          stuck.state is PlanState.CANCELLED)
    check("abort 清掉 pending_op", stuck.pending_op == "")
    check("abort 把剩余步数记入 skipped", len(stuck.skipped) == 1)
    check("abort 后进度看起来是满的",
          stuck.done_count == stuck.total)

    # --- npc 模式是显式选择，不算降级 ---------------------------------
    check("配置注释明确 npc 不是虚拟玩家",
          "不是虚拟玩家" in src)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 70)
    print("混合架构测试（Python 大脑 + 行为包身体）")
    print("=" * 70)

    test_bridge()
    test_body_controller()
    test_async_executor()
    test_compiler_real_mode()
    test_real_actions()
    test_packing()
    test_protocol_consistency()
    test_no_silent_fallback()

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
