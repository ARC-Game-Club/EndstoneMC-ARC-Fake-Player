"""假人出生点 / 重生点：出生即摔死的根因回归。

背景
----
无规则服（ARCAnarchy）的假人"一出生就摔死"，而且是死循环。
根因是三个坑叠在一起（日志 + level.dat + 源码交叉验证）：

1. **世界的出生高度是"自动"的**。``worlds/ARCAnarchy/level.dat`` 里是
   ``SpawnX=-243 SpawnY=32767 SpawnZ=-105``——``32767`` 是哨兵值，意思是
   "引擎运行时自己找"。那一列在山上（地面约 y=151），BDS 每次解析出来
   的高度还不一样：实测 200 和 312 都出现过。
2. **重生点从来没设成功过**。插件本来会给每个假人调 ``setSpawnPoint``
   记住重生点，但这个引擎版本（1.26.52.3）的脚本 API 只收 **0~1** 个
   参数，老写法 ``setSpawnPoint(loc, dim)`` 传两个会抛::

       TypeError: Incorrect number of arguments to function.
                  Expected 0-1, received 2

   于是引擎退回**世界出生点**——就是上面那个高度不确定的 32767 自动值。
   假人从半空掉下来摔死 → 自动重生 → 又是半空 → 再摔。
3. **"高空安全落地"的保护有洞**。y>128 时插件先用创造模式生成、落地后
   再切回生存；老实现是"接上后固定等 8 秒"。但从 200 掉到地面要 10 秒
   以上，8 秒时它**还在半空**——切成生存后落地那一下的坠落伤害直接摔死。

枪战服（ARCShooterGame）为什么没事？它的 level.dat 是
``SpawnX=0 SpawnY=53 SpawnZ=0``：高度是**写死的**，而且生成点就在地面上，
上面三个坑一个都踩不到。

这几条用例把三件事钉住：

  1. 地表扫描要**跳过树叶和花草**——不能把树冠当地面；
  2. 切回生存模式必须**等落地**，不能用固定延迟；
  3. 行为包侧的重生点必须真的设上（1 个参数 + 老引擎兜底）且落在地面上，
     坠落不再被误报成"重生"。
"""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tests.endstone_stub import install

install()

import endstone_arc_fake_player as pkg  # noqa: E402
from endstone_arc_fake_player.plugin import ArcFakePlayerPlugin  # noqa: E402

PKG = Path(pkg.__file__).resolve().parent
JS_SRC = (PKG / "behavior_pack" / "scripts" / "arena_combat.js").read_text("utf-8")


def _js_slice(src: str, marker: str, length: int = 2600) -> str:
    """截出某个 JS 函数附近的源码，用来做契约断言。"""
    i = src.find(marker)
    if i < 0:
        return ""
    return src[i:i + length]


class FakeScheduler:
    """只记录任务、让人手动泵的假调度器。"""

    def __init__(self):
        self.pending: list = []

    def run_task(self, owner, task, delay=0, period=0, **kwargs):
        self.pending.append(task)
        return len(self.pending)

    def pump(self, times: int = 1) -> int:
        done = 0
        for _ in range(times):
            if not self.pending:
                break
            task = self.pending.pop(0)
            task()
            done += 1
        return done


class FakePlayer:
    """高度按剧本递进的假玩家（模拟"从 200 一路掉到地面，然后站住"）。"""

    def __init__(self, ys):
        self.ys = list(ys)
        self.i = 0

    @property
    def location(self):
        y = self.ys[min(self.i, len(self.ys) - 1)]
        self.i += 1
        return SimpleNamespace(x=0.0, y=y, z=0.0)


def _world_with(columns: dict):
    """columns: {(x, z): {y: 短名}} —— 假地表。"""
    def get_block_name(dim, x, y, z):
        return columns.get((x, z), {}).get(y, "")

    return SimpleNamespace(get_block_name=get_block_name)


class SurfaceScanTests(unittest.TestCase):
    """Python 侧的地表扫描。"""

    def setUp(self):
        self.plugin = ArcFakePlayerPlugin()
        self.plugin.logger = SimpleNamespace(
            debug=lambda *a, **k: None, info=lambda *a, **k: None,
            warning=lambda *a, **k: None)

    def _scan(self, columns, x=-243, z=-105):
        self.plugin._world = _world_with(columns)
        return self.plugin._surface_scan_y(object(), x, z)

    def test_returns_ground_plus_one(self):
        """普通地面：扫到 y=153 的泥土 → 可站立的 y=154。"""
        self.assertEqual(self._scan({(-243, -105): {153: "dirt"}}), 154)

    def test_skips_tree_canopy(self):
        """树冠不是地面：树叶要跳过，继续往下找到真正的地面。

        这是无规则服那条链的第一环——世界出生点那一列长着树，
        按"第一个非空气"扫会停在树冠上（y=200），假人就出生在树冠上。
        """
        col = {199: "oak_leaves", 198: "oak_leaves", 197: "oak_log", 153: "dirt"}
        self.assertEqual(self._scan({(-243, -105): col}), 154)

    def test_skips_plants(self):
        """花草站不住，也要跳过。"""
        col = {160: "short_grass", 159: "poppy", 153: "dirt"}
        self.assertEqual(self._scan({(-243, -105): col}), 154)

    def test_grass_block_is_standable(self):
        """`grass_block` 的后缀是 grass_block 不是 grass —— 不能被误伤。"""
        self.assertEqual(self._scan({(-243, -105): {153: "grass_block"}}), 154)

    def test_all_air_returns_none(self):
        """整列都是空气 → 扫不出来（调用方退回 y=200 的高空兜底）。"""
        self.assertIsNone(self._scan({(-243, -105): {}}))

    def test_air_name_is_skipped(self):
        """显式的 minecraft:air 也要跳过。"""
        col = {160: "minecraft:air", 153: "stone"}
        self.assertEqual(self._scan({(-243, -105): col}), 154)


class PendingGameModeTests(unittest.TestCase):
    """切回生存模式：必须等落地。"""

    def setUp(self):
        self.plugin = ArcFakePlayerPlugin()
        self.plugin.logger = SimpleNamespace(
            debug=lambda *a, **k: None, info=lambda *a, **k: None,
            warning=lambda *a, **k: None)
        self.dispatched: list[str] = []
        self.plugin._dispatch = self.dispatched.append
        self.sched = FakeScheduler()
        self.plugin.server = SimpleNamespace(scheduler=self.sched)

    def _prepare(self, ys):
        player = FakePlayer(ys)
        self.plugin._find_sim_player = lambda name: player
        self.plugin._pending_gm["bot"] = 0   # 0 = survival
        self.plugin._apply_pending_gm("bot", player)

    def test_no_switch_while_falling(self):
        """还在半空的时候不许切生存——落地那一下会直接摔死。"""
        self._prepare([200.0, 180.0, 160.0, 154.0])
        self.sched.pump(4)
        self.assertEqual(self.dispatched, [],
                         "半空就切了生存模式，落地必摔死")

    def test_switch_after_landing(self):
        """站住之后才切生存。"""
        self._prepare([200.0, 180.0, 160.0, 154.0, 154.0, 154.0])
        self.sched.pump(8)
        self.assertEqual(self.dispatched, ["gamemode survival bot"])

    def test_switch_happens_only_once(self):
        """切过一次就完了，别反复切。"""
        self._prepare([154.0, 154.0, 154.0, 154.0])
        self.sched.pump(6)
        self.assertEqual(self.dispatched, ["gamemode survival bot"])

    def test_switches_even_if_never_lands(self):
        """一直掉不停（卡住了）也要切，最多等 60 秒。"""
        ys = [200.0 - i for i in range(400)]
        self._prepare(ys)
        self.sched.pump(200)
        self.assertEqual(self.dispatched, ["gamemode survival bot"])

    def test_no_pending_entry_is_noop(self):
        """没有待切换项就什么都不做。"""
        self.plugin._pending_gm.pop("bot", None)
        self.plugin._apply_pending_gm("bot", FakePlayer([154.0]))
        self.sched.pump(3)
        self.assertEqual(self.dispatched, [])


class SpawnAnchorTests(unittest.TestCase):
    """假人的"家"：服务器配置好的出生点（弧光核心 /spawn 那个位置）。

    为什么不用 level.dat 的世界出生点：那里的 `SpawnY=32767` 是"引擎运行时
    自己找"的哨兵值，解析出来的高度每次都可能不同（实测 200 / 312），
    而那一列在山上，假人从那儿掉下来必摔死。弧光核心 `spawn_locations`
    表里存的是配好的实坐标（无规则服 -248.45, 168, -111.57），Y 是确定的。
    """

    def setUp(self):
        self.plugin = ArcFakePlayerPlugin()
        self.plugin.logger = SimpleNamespace(
            debug=lambda *a, **k: None, info=lambda *a, **k: None,
            warning=lambda *a, **k: None)
        self.plugin.crowd_config = lambda: {"home": None}

    def _with_core(self, spawns):
        core = SimpleNamespace(api_list_spawn_locations=lambda: spawns)
        self.plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=lambda name: core))
        return core

    def test_reads_overworld_spawn_from_core(self):
        self._with_core([
            {"dimension": "minecraft:the_end", "x": 1, "y": 2, "z": 3},
            {"dimension": "minecraft:overworld",
             "x": -248.45, "y": 168, "z": -111.57},
        ])
        got = self.plugin._arc_core_spawn()
        self.assertEqual(got, (-248.45, 168.0, -111.57))

    def test_no_core_means_no_anchor(self):
        self.plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=lambda name: None))
        self.assertIsNone(self.plugin._arc_core_spawn())

    def test_broken_core_api_is_not_fatal(self):
        core = SimpleNamespace(api_list_spawn_locations=Mock(side_effect=RuntimeError))
        self.plugin.server = SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugin=lambda name: core))
        self.assertIsNone(self.plugin._arc_core_spawn())

    def test_anchor_prefers_configured_home(self):
        """显式配置的 home 优先于弧光核心的出生点。"""
        self._with_core([{"dimension": "minecraft:overworld",
                          "x": 1, "y": 2, "z": 3}])
        self.plugin.crowd_config = lambda: {"home": [10, 20, 30]}
        self.assertEqual(self.plugin._spawn_anchor(), (10.0, 20.0, 30.0))

    def test_anchor_falls_back_to_core(self):
        self._with_core([{"dimension": "minecraft:overworld",
                          "x": -248.45, "y": 168, "z": -111.57}])
        self.assertEqual(self.plugin._spawn_anchor(), (-248.45, 168.0, -111.57))

    def test_spawn_pos_uses_anchor_when_nobody_around(self):
        """没人在线时，出生点用配置好的出生点（Y 是确定的地面高度）。"""
        self._with_core([{"dimension": "minecraft:overworld",
                          "x": -248.45, "y": 168, "z": -111.57}])
        self.plugin.server.online_players = []
        self.plugin._actors = {}
        self.plugin._external = {}
        self.plugin._world_spawn_xz = lambda: (-243, -105)   # 不该走到这里
        pos = self.plugin._crowd_spawn_pos()
        self.assertEqual(pos[:3], (-248, 168, -111))

    def test_spawn_payload_carries_respawn_anchor(self):
        """重生点要随生成请求一起发给行为包。"""
        from endstone_arc_fake_player.bridge import FakePlayerBridge
        sent = []

        class _B(FakePlayerBridge):
            def __init__(self):
                pass

            def send_tracked(self, event_id, data, kind=None, callback=None):
                sent.append((event_id, data))
                return 1

        b = _B()
        b.spawn("bot", 1, 2, 3, "overworld", respawn=(-248.45, 168.0, -111.57))
        self.assertEqual(sent[0][0], "bot:spawn")
        self.assertEqual(sent[0][1]["rx"], -248.45)
        self.assertEqual(sent[0][1]["ry"], 168.0)
        self.assertEqual(sent[0][1]["rz"], -111.57)


class BehaviorPackContractTests(unittest.TestCase):
    """行为包侧的契约（源码断言——这套引擎上跑不了 JS，只能钉源码）。"""

    def test_set_spawn_point_tries_one_argument_first(self):
        """本机 BDS 的 setSpawnPoint 只收 0~1 个参数，必须先按 1 个参数调。"""
        body = _js_slice(JS_SRC, "function applySpawnPoint")
        self.assertIn("sim.setSpawnPoint(loc);", body)
        self.assertIn("sim.setSpawnPoint(loc, dim);", body,
                      "老引擎的两参数写法要留作兜底")
        self.assertLess(body.index("sim.setSpawnPoint(loc);"),
                        body.index("sim.setSpawnPoint(loc, dim);"),
                        "必须先试 1 个参数，再退回 2 个参数")

    def test_both_spawn_point_errors_are_logged(self):
        """两种写法的错误都要打出来。

        否则只看得到最后那个"参数个数"错误，会误以为只是写法不对——
        实际上单参数写法也过不去（实体不暴露 dimension）。
        """
        body = _js_slice(JS_SRC, "function applySpawnPoint")
        self.assertIn("单参数写法失败", body)
        self.assertIn("双参数写法也失败", body)

    def test_respawn_point_is_ground_not_creation_position(self):
        """重生点要落在地面上，不能直接用生成时的位置（可能在半空）。"""
        body = _js_slice(JS_SRC, "export function rememberSpawnPoint")
        self.assertIn("findGroundY(", body)
        self.assertIn("respawnPoints.set(name, respawn);", body)

    def test_ground_scan_skips_leaves(self):
        """地面扫描要跳过树叶/花草——树冠不是地面。"""
        body = _js_slice(JS_SRC, "function findGroundY")
        self.assertIn("NOT_STANDABLE", body)
        self.assertIn("endsWith(bad)", body,
                      "树叶 id 带树种（oak_leaves），只能按后缀拦")

    def test_ground_scan_does_not_abort_on_air(self):
        """扫描不能在"这一格是空气"上放弃。

        getBlock 抛异常 = 区块没加载（扫不了）；返回 undefined = 空气
        （继续往下找）。两者混在一起的话，假人明明站在地面上、脚下一格
        就是地板，扫描也会在第一格空气上就放弃 → 地面点永远找不到。
        """
        body = _js_slice(JS_SRC, "function findGroundY")
        self.assertIn("catch (e)", body, "抛异常（区块没加载）要单独处理")
        self.assertIn("if (!block) continue;", body,
                      "undefined 是空气，要继续往下找而不是放弃")

    def test_falling_is_not_reported_as_respawn(self):
        """坠落不再被误报成"阵亡后重生"——判据改成"上一拍血量是 0"。"""
        body = _js_slice(JS_SRC, "export function watchRespawn")
        self.assertNotIn("moved < 8", body,
                         "位移判据会把每一秒的坠落都当成重生")
        self.assertIn("wasDead", body)

    def test_landing_pins_spawn_point(self):
        """落地后要把重生点钉在脚下（生成时那一次扫描也可能失败）。"""
        body = _js_slice(JS_SRC, "function pinSpawnPointOnLanding")
        self.assertIn("respawnPoints.set(name, respawn);", body)

    def test_respawn_teleports_to_ground(self):
        """重生之后必须把它挪回地面——这才是真正管用的那一步。

        引擎的 setSpawnPoint 对 SimulatedPlayer 两种写法都不认，
        引擎会退回世界出生点（高度是 level.dat 里的自动值，可能在半空）。
        """
        self.assertIn("export function getRespawnPoint", JS_SRC)
        main = (PKG / "behavior_pack" / "scripts" / "main.js").read_text("utf-8")
        self.assertIn("getRespawnPoint", main, "main.js 要用这个点")
        body = _js_slice(main, "function teleportAfterRespawn")
        self.assertIn("sim.teleport(", body,
                      "重生后要靠传送把假人挪回地面")
        self.assertIn("teleportAfterRespawn(name, sim);", main,
                      "autoRespawn 里要调用它")

    def test_removed_bot_clears_records(self):
        """假人被移除时，五张表都要清——不然每轮换人都留一条。"""
        self.assertIn("export function forgetBot", JS_SRC)
        body = _js_slice(JS_SRC, "export function forgetBot")
        for table in ("spawnPoints", "respawnPoints", "lastPositions",
                      "lastHealth", "landingState", "anchoredBots"):
            self.assertIn(f"{table}.delete(name);", body)
        main = (PKG / "behavior_pack" / "scripts" / "main.js").read_text("utf-8")
        self.assertIn("forgetBot(name);", main)

    def test_configured_anchor_wins_over_landing(self):
        """服务器配置的重生点，不许被"落地位置"覆盖。"""
        body = _js_slice(JS_SRC, "function pinSpawnPointOnLanding")
        self.assertIn("anchoredBots.has(name)", body,
                      "配置的点说了算，落地位置不许覆盖它")
        self.assertIn("anchoredBots.add(name);",
                      _js_slice(JS_SRC, "export function rememberSpawnPoint"))

    def test_spawn_message_carries_anchor(self):
        """Python 侧要把配置好的出生点随生成请求带过来。"""
        main = (PKG / "behavior_pack" / "scripts" / "main.js").read_text("utf-8")
        self.assertIn("req.rx", main)
        self.assertIn("rememberSpawnPoint(name, out.sim, loc, anchor)", main)


def main():
    total = 0
    ok = True
    for cls in (SurfaceScanTests, PendingGameModeTests, SpawnAnchorTests,
                BehaviorPackContractTests):
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(cls))
        total += result.testsRun
        ok = ok and result.wasSuccessful()
    if not ok:
        return 1
    print(f"全部 {total} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
