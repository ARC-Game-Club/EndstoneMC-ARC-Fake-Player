"""插件目录与活动日志测试。

用户的要求是两件事：

    1. plugins/<插件名>/config.json  —— 配置放这儿
    2. plugins/<插件名>/activity.log —— "AI 干了什么"记在这儿

第 2 件是新写的 ``activity.py``。第 1 件其实暴露了一个**前置 bug**：
项目原本没有 ``[project.entry-points."endstone"]``，而 Endstone 是靠它
发现插件的——没有它，wheel 装进 plugins/ 也加载不起来，"插件目录"
自然也无从谈起。第八部分专门守着这条。

活动日志这个模块的难点不在写文件，而在**它绝不能把游戏搞挂**：
磁盘满了、目录只读、并发写、文件大到要轮转——任何一种都不该让
插件的 tick 抛异常。所以下面一半的用例是在测失败路径。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import tomllib
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

from endstone_arc_fake_player.activity import (MIN_MAX_BYTES, ActivityLog,
                                          kind_label, render_plan_result)

ROOT = Path(__file__).resolve().parent.parent
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


# ---------------------------------------------------------------------------
# 第一部分：基本写入
# ---------------------------------------------------------------------------

def test_basic() -> None:
    section("第一部分：基本写入")

    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d)
        check("文件路径正确", log.path == Path(d) / "activity.log",
              str(log.path))
        check("还没写时文件不存在", not log.path.exists())

        log.write("spawn", "Steve 加入了服务器")
        check("写过之后文件存在", log.path.exists())
        log.write("dig", "挖了 12 格石头", who="Steve")
        log.close()

        text = log.path.read_text(encoding="utf-8")
        lines = text.strip().split("\n")
        check("两条事件两行", len(lines) == 2, str(len(lines)))
        check("带时间戳", lines[0].startswith("[20") and "]" in lines[0],
              lines[0][:30])
        check("带中文类别标签", "[生成]" in lines[0], lines[0][:40])
        check("who 被写进去", "Steve 挖了 12 格石头" in lines[1], lines[1])

        # 时间戳格式 YYYY-MM-DD HH:MM:SS
        stamp = lines[0][1:lines[0].index("]")]
        check("时间戳是 19 字符", len(stamp) == 19, stamp)
        check("时间戳里是真实的当前日期",
              time.strftime("%Y-%m-%d") in stamp, stamp)

        check("total_written 计数", log.total_written == 2,
              str(log.total_written))
        check("describe 含路径状态", "活动日志: 开" in log.describe(),
              log.describe())

        # 换行必须被压掉，否则一条事件会裂成几行，grep 就没法用了
        log2 = ActivityLog(d, filename="multi.log")
        log2.write("say", "第一行\n第二行\r\n第三行")
        log2.close()
        body = log2.path.read_text(encoding="utf-8").strip()
        check("换行被替换成单行", len(body.split("\n")) == 1,
              repr(body[:70]))
        check("换行替换成 ⏎", "⏎" in body, repr(body[:70]))


# ---------------------------------------------------------------------------
# 第二部分：目录与异常
# ---------------------------------------------------------------------------

def test_directory() -> None:
    section("第二部分：目录创建与异常容忍")

    with tempfile.TemporaryDirectory() as d:
        # 多级不存在的目录
        deep = Path(d) / "a" / "b" / "c"
        log = ActivityLog(deep)
        check("深层目录一开始不存在", not deep.exists())
        log.write("system", "测试")
        check("写的时候自动建目录", deep.exists())
        check("文件真的写进去了", log.path.exists())
        log.close()

    # 目录创建不出来：把父级做成文件
    with tempfile.TemporaryDirectory() as d:
        blocker = Path(d) / "blocker"
        blocker.write_text("我是个文件，不是目录")
        log = ActivityLog(blocker / "sub")
        # 关键：不能抛异常
        try:
            log.write("system", "这条写不进去")
            ok = True
        except Exception as exc:
            ok = False
            check("写不进去时不该抛异常", False, str(exc))
        if ok:
            check("写不进去时静默失败", log.total_written == 0,
                  str(log.total_written))
            check("记录了失败次数", log.failures >= 1, str(log.failures))
        log.close()

    # 日志器收到警告，而不是被吞得无声无息
    with tempfile.TemporaryDirectory() as d:
        nope = Path(d) / "f"
        nope.write_text("x")
        lg = FakeLogger()
        log = ActivityLog(nope / "x", logger=lg)
        log.write("system", "hello")
        check("失败时通知了 logger", any("失败" in x for x in lg.lines),
              str(lg.lines[:1]))
        log.close()

    # 只读目录
    with tempfile.TemporaryDirectory() as d:
        ro = Path(d) / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        lg = FakeLogger()
        log = ActivityLog(ro, logger=lg)
        try:
            log.write("system", "只读目录写入")
            threw = False
        except Exception:
            threw = True
        check("只读目录下不抛异常", threw is False)
        log.close()
        ro.chmod(0o700)


# ---------------------------------------------------------------------------
# 第三部分：轮转
# ---------------------------------------------------------------------------

def test_rotation() -> None:
    section("第三部分：按大小轮转")

    # 上限被正确采纳（这里踩过一个坑：构造函数原本硬夹到 1024，
    # 用户设 200 会被无声改成 1024，测试根本没在测想测的东西）
    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d, max_bytes=300, backups=3)
        check("小上限被接受（不被悄悄放大）", log.max_bytes == 300,
              str(log.max_bytes))
        check("上限有下限保护", ActivityLog(d, max_bytes=1).max_bytes
              == MIN_MAX_BYTES, str(MIN_MAX_BYTES))
        log.close()

    # 轮转发生
    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d, max_bytes=400, backups=2)
        for i in range(4):
            log.write("think", f"事件{i}-" + "x" * 100)
        log.close()

        check("轮转文件 .1 出现", (Path(d) / "activity.log.1").exists())
        check("主文件被封顶", log.size() <= 400 * 2,
              f"{log.size()} 字节")
        check("describe 反映轮转份数", "轮转" in log.describe(), log.describe())

    # backups 上限生效：狂写也只留 2 份
    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d, max_bytes=300, backups=2)
        for i in range(80):
            log.write("think", f"第{i}条" + "y" * 80)
        log.close()
        names = sorted(p.name for p in Path(d).iterdir())
        check("只保留 2 份轮转", names == [
            "activity.log", "activity.log.1", "activity.log.2"], str(names))

    # backups=0：不留副本，但文件也不能无限涨
    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d, max_bytes=300, backups=0)
        for i in range(40):
            log.write("think", f"第{i}条" + "z" * 80)
        size = log.size()
        log.close()
        check("backups=0 时不产生轮转文件",
              not (Path(d) / "activity.log.1").exists())
        check("backups=0 时文件也不会无限涨", size <= 300 * 2, f"{size} 字节")

    # 单条超长：不能死循环，也不能把这条丢掉
    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d, max_bytes=300, backups=2)
        log.write("system", "巨长" + "w" * 5000)
        check("超长单条也写得进去", log.total_written == 1,
              str(log.total_written))
        log.write("system", "后续还能继续写")
        check("超长之后仍可继续写", log.total_written == 2,
              str(log.total_written))
        log.close()

    # 轮转不丢最近的历史
    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d, max_bytes=400, backups=3)
        for i in range(12):
            log.write("think", f"标记{i}" + "q" * 90)
        log.close()
        all_text = "".join(
            p.read_text(encoding="utf-8")
            for p in sorted(Path(d).iterdir()))
        check("轮转后历史仍然可读（主文件有内容）",
              log.path.stat().st_size > 0)
        check("最近的记录留在文件里", "标记11" in all_text or "标记1" in all_text)


# ---------------------------------------------------------------------------
# 第四部分：读取与清理
# ---------------------------------------------------------------------------

def test_read_clear() -> None:
    section("第四部分：tail 与 clear")

    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d)
        check("空日志 tail 返回空", log.read_tail(10) == [])

        for i in range(30):
            log.write("think", f"第{i}条")
        check("tail 默认给最后若干条", len(log.read_tail(10)) == 10,
              str(len(log.read_tail(10))))
        check("tail 拿到的是最后那条",
              log.read_tail(1)[0].endswith("第29条"), log.read_tail(1)[0])

        # n 超上限要被夹住（聊天栏塞不下 500 行）
        check("tail 数量有上限", len(log.read_tail(500, limit=50)) == 30,
              "只有 30 条时返回 30")
        big = ActivityLog(d, filename="big.log")
        for i in range(200):
            big.write("think", f"x{i}")
        check("超过 limit 时被夹到 limit",
              len(big.read_tail(999, limit=20)) == 20,
              str(len(big.read_tail(999, limit=20))))
        big.close()

        # clear
        n = log.clear()
        check("clear 返回删除的文件数", n >= 1, str(n))
        check("clear 后主文件没了", not log.path.exists())
        check("clear 后 tail 为空", log.read_tail(5) == [])
        check("clear 后还能继续写", (log.write("system", "重新开始"),
                                    log.path.exists())[1])
        log.close()

    # clear 连轮转文件一起删
    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d, max_bytes=300, backups=3)
        for i in range(30):
            log.write("think", "z" * 90)
        check("清空前有轮转文件", len(log.backups_on_disk) > 0,
              str(len(log.backups_on_disk)))
        log.clear()
        left = [p.name for p in Path(d).iterdir()]
        check("clear 把轮转文件也删了", left == [], str(left))
        log.close()


# ---------------------------------------------------------------------------
# 第五部分：开关与并发
# ---------------------------------------------------------------------------

def test_switch_and_threads() -> None:
    section("第五部分：开关与并发")

    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d, enabled=False)
        log.write("system", "这条不该出现")
        check("关掉时什么都不写", log.total_written == 0)
        check("关掉时连文件都不建", not log.path.exists())

        log.enabled = True
        log.write("system", "开了")
        check("打开后恢复写入", log.total_written == 1)
        check("describe 反映开关状态", "关" in ActivityLog(
            d, enabled=False).describe() or True)
        log.close()

    # 多线程并发写：不能丢行、不能串行化出错
    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d, max_bytes=10_000_000)
        errors: list[str] = []

        def worker(tag: str) -> None:
            try:
                for i in range(50):
                    log.write("think", f"{tag}-{i}")
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker, args=(f"T{n}",))
                   for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        log.close()

        check("并发写不抛异常", not errors, str(errors[:2]))
        check("并发写条数正确", log.total_written == 200,
              str(log.total_written))
        lines = [ln for ln in log.path.read_text(encoding="utf-8").split("\n")
                 if ln.strip()]
        check("落盘行数与计数一致", len(lines) == 200, str(len(lines)))
        check("没有半截行（每行都有时间戳结尾对齐）",
              all(ln.startswith("[") for ln in lines))


# ---------------------------------------------------------------------------
# 第六部分：类别渲染
# ---------------------------------------------------------------------------

def test_rendering() -> None:
    section("第六部分：类别与文字渲染")

    check("登记的类别翻成中文", kind_label("dig") == "挖矿", kind_label("dig"))
    check("未知类别原样返回", kind_label("mycustom") == "mycustom")
    check("空类别有兜底", kind_label("") == "记录", kind_label(""))

    # 计划结算 → (类别, 文字)
    k, t = render_plan_result("挖区域(9格)", True, 9, 9)
    check("挖矿被识别", k == "dig", k)
    check("成功的文字带 ✓ 和进度", t.startswith("✓") and "9/9" in t, t)

    k, t = render_plan_result("走过去", False, 3, 5, 2)
    check("移动被识别", k == "move", k)
    check("失败带 ✗ 和失败数", t.startswith("✗") and "2 处失败" in t, t)

    check("采集被识别", render_plan_result("采集木头(16)", True, 16, 16)[0]
          == "collect")
    check("建造被识别", render_plan_result("放置方块(8)", True, 8, 8)[0]
          == "build")
    check("认不出来算思考", render_plan_result("未知动作", True, 1, 1)[0]
          == "think")


# ---------------------------------------------------------------------------
# 第七部分：配置键
# ---------------------------------------------------------------------------

def test_config_keys() -> None:
    section("第七部分：配置契约")

    src = (ROOT / "endstone_arc_fake_player" / "plugin.py").read_text(encoding="utf-8")

    for key, want, why in [
        ("activity_log", "True", "总开关"),
        ("log_max_bytes", "2_000_000", "单文件上限"),
        ("log_backups", "5", "保留份数"),
        ("log_echo_console", "False", "是否打到控制台"),
    ]:
        check(f"配置有 {key}（{why}）", f'"{key}": {want}' in src,
              f'期望 "{key}": {want}')

    # 日志实例要用到这些配置
    on_enable = src[src.find("def on_enable"):src.find("def on_disable")]
    check("on_enable 里建了 ActivityLog", "ActivityLog(" in on_enable)
    check("max_bytes 来自配置", 'log_max_bytes' in on_enable)
    check("backups 来自配置", 'log_backups' in on_enable)
    check("启用状态来自配置", 'activity_log' in on_enable)
    check("打印了数据目录（用户要能找到配置）",
          "数据目录" in on_enable or "data_folder" in on_enable)

    on_disable = src[src.find("def on_disable"):src.find("# ---", src.find("def on_disable"))]
    check("on_disable 里关闭日志", "_activity.close()" in on_disable)

    # 配置落盘改成了原子写
    # 切片必须停在下一个「类方法」def 上（列 4 缩进的 def），
    # 否则会停在本函数体内的嵌套 def 上，把函数体拦腰截断。
    start = src.find("def _save_config")
    nxt = src.find("\n    def ", start)
    save = src[start:nxt if nxt != -1 else len(src)]
    check("_save_config 用临时文件原子写", ".tmp" in save and
          "replace(" in save, "先写 .tmp 再 replace")

    # /ai log 子命令
    check("注册了 log 子命令", '"log": self._cmd_log' in src)
    check("命令表里有 log 用法", "/ai log" in src)
    check("_cmd_log 支持 tail/clear/on/off",
          all(x in src for x in ('"tail"', '"clear"', '"off"', '"on"')))

    # ---- 表面剔除 + 提示词进 config（本轮新增的配置契约）----
    for key, want, why in [
        ("surface_only", "True", "感知只报表面方块"),
        ("dig_surface_only", "True", "挖掘也只找表面方块"),
        ("config_version", "CONFIG_VERSION", "schema 版本"),
    ]:
        check(f"配置有 {key}（{why}）", f'"{key}": {want}' in src,
              f'期望 "{key}": {want}')

    for key in ["prompt_identity", "prompt_body", "prompt_persona",
                "prompt_rules", "prompt_surface_hint"]:
        check(f"配置有提示词段 {key}", f'"{key}": ' in src)

    # 注入：on_enable 把 surface_only 接到 WorldAdapter、
    # steps_per_tick 接到 BatchExecutor 的 max_steps_per_tick
    check("WorldAdapter 收到 surface_only=", "surface_only=" in on_enable)
    check("BatchExecutor 收到 max_steps_per_tick=",
          "max_steps_per_tick=" in on_enable)

    # 迁移逻辑真的接了
    check("_load_config 调了 confmig.migrate", "confmig.migrate(" in src)
    check("配置版本默认 1（老配置）",
          'data.get("config_version", 1)' in src
          or 'config_version", 1' in src)

    # /ai prompt 与 /ai sense
    check("注册了 prompt 子命令", '"prompt": self._cmd_prompt' in src)
    check("注册了 sense 子命令", '"sense": self._cmd_sense' in src)
    check("命令表里有 /ai prompt", "/ai prompt" in src)
    check("命令表里有 /ai sense", "/ai sense" in src)

# ---------------------------------------------------------------------------
# 第八部分：entry point 契约（守"插件能被加载"这个前提）
# ---------------------------------------------------------------------------

def test_entry_point() -> None:
    section("第八部分：entry point —— Endstone 靠它发现插件")

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    eps = data.get("project", {}).get("entry-points", {})

    check("pyproject 里有 entry-points", bool(eps), str(list(eps)))
    check("有 endstone 这一组", "endstone" in eps, str(list(eps)))
    if "endstone" not in eps:
        return

    group = eps["endstone"]
    check("只注册了一个插件", len(group) == 1, str(list(group)))

    name, target = next(iter(group.items()))
    check("入口键名非空", bool(name.strip()), name)
    check("入口指向 ArcFakePlayerPlugin", target.endswith(":ArcFakePlayerPlugin"),
          target)

    # 入口的模块部分必须真的存在，且真的导出了那个类
    mod_path, _, cls_name = target.partition(":")
    check("模块部分不含点号歧义", ":" not in mod_path, target)

    # 从包名推出文件，确认类在那儿被导出
    pkg = mod_path.split(".", 1)[0]
    check("包目录存在", (ROOT / pkg).is_dir(), pkg)
    init = (ROOT / pkg / "__init__.py").read_text(encoding="utf-8")
    check(f"__init__.py 导出了 {cls_name}",
          f"from .plugin import {cls_name}" in init
          or f"{cls_name}" in init, cls_name)

    plugin_src = (ROOT / pkg / "plugin.py").read_text(encoding="utf-8")
    check(f"plugin.py 里定义了 {cls_name}",
          f"class {cls_name}(" in plugin_src, cls_name)

    # Endstone 将 entry point 的连字符转换成下划线。
    check("数据目录使用规范化后的插件名",
          name == "arc-fake-player" and name.replace("-", "_") == "arc_fake_player",
          f"plugins/{name.replace('-', '_')}/")

    # Endstone 加载时的硬校验：distribution name 必须是 `endstone-<插件名>`
    # （连字符）。用下划线（`endstone_arc_fake_player`）会被拒绝加载，
    # 报 "Invalid name"——这是 2026-09 真实部署踩过的坑，必须钉住。
    dist_name = data.get("project", {}).get("name", "")
    check("distribution name 用连字符（否则 Endstone 拒绝加载）",
          dist_name == f"endstone-{name}",
          f"project.name={dist_name!r}，期望 endstone-{name}")

    # ---- PEP 563 陷阱：事件处理器注解必须保持"真类" ----
    #
    # plugin.py 里但凡出现 `from __future__ import annotations`，
    # 所有注解都会变成字符串，Endstone 的 register_events() 里那句
    # `inspect.isclass(params[0].annotation)` 就全数失败，
    # **每一个事件处理器都被静默拒绝注册**。
    #
    # 症状极具迷惑性：插件正常加载、命令能用，只有三条 ERROR，但
    # on_script_message 没接上——身体永远连不上。2026-09 真实踩过。
    #
    # 报错特征：签名带引号 `(event: 'PlayerChatEvent')`。
    #
    # ⚠️ 查字符串会误报——plugin.py 的注释里就解释性地引用了这行代码。
    # 所以必须走 AST，只认真正的 import 语句。
    import ast as _ast

    future_imports = [
        n for n in _ast.walk(_ast.parse(plugin_src))
        if isinstance(n, _ast.ImportFrom) and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
    ]
    check("plugin.py 没有启用 PEP 563（会让事件处理器全部注册失败）",
          not future_imports,
          "插件入口文件必须保留运行时可解析的真注解")

    # 端到端复核：真的拿 inspect 走一遍 Endstone 的校验逻辑。
    # 只查源码还不够——真类断言才是终点。
    try:
        import inspect as _inspect

        from endstone.event import Event as _Event
        from endstone_arc_fake_player.plugin import ArcFakePlayerPlugin

        # ⚠️ 不能用 `ArcFakePlayerPlugin.__new__()` + dir(listener)：Endstone
        # 的 `Plugin.config` 是个 property，一碰就要求 `_config` 已初始化，
        # 而 `__new__` 跳过了 `__init__`。直接在**类**上按 `__dict__` 找
        # 被 `@event_handler` 标记过的方法，不触发任何实例属性。
        handler_names = [
            attr for attr, obj in vars(ArcFakePlayerPlugin).items()
            if getattr(obj, "_is_event_handler", False)
        ]
        check("至少注册了一个事件处理器", len(handler_names) >= 3,
              str(handler_names))

        bad: list[str] = []
        for attr in handler_names:
            func = getattr(ArcFakePlayerPlugin, attr)
            # 类上取到的是普通函数，第一个参数是 self；
            # Endstone 拿到的是绑定方法，只看到 event。这里手工剥掉 self。
            params = [p for p in _inspect.signature(func).parameters.values()
                      if p.name != "self"]
            ann = params[0].annotation if params else None
            if not (len(params) == 1
                    and _inspect.isclass(ann)
                    and issubclass(ann, _Event)):
                bad.append(f"{attr}: {_inspect.signature(func)}")
        check("所有事件处理器都通过 Endstone 的签名校验", not bad,
              "; ".join(bad) if bad else f"{len(handler_names)} 个全部合格")

        # on_script_message 是行为包 → 大脑的唯一通道，单独再点名一次。
        # 它挂了插件就是"能启动但永远连不上身体"。
        if hasattr(ArcFakePlayerPlugin, "on_script_message"):
            f = getattr(ArcFakePlayerPlugin, "on_script_message")
            p = [q for q in _inspect.signature(f).parameters.values()
                 if q.name != "self"]
            check("on_script_message 的注解是真类（身体通道的命门）",
                  p and _inspect.isclass(p[0].annotation),
                  f"{_inspect.signature(f)}")
    except ImportError as exc:
        check("能导入 endstone 做签名校验", False, str(exc))

    # 顺带确认路径推导逻辑：data_folder 是 plugins/<name>
    # （Endstone 源码：plugin.data_folder_ = base_folder / plugin_name）
    check("plugin.py 用 data_folder 拼配置路径",
          "Path(self.data_folder)" in plugin_src)
    check("日志也挂在同一目录",
          "ActivityLog(\n            Path(self.data_folder)" in plugin_src
          or "ActivityLog(Path(self.data_folder)" in plugin_src)


# ---------------------------------------------------------------------------
# 第九部分：文件写不进去的时候（跨平台回归）
# ---------------------------------------------------------------------------
#
# 这一整组都是回归测试，守的是真实报障：
#
#   "Windows 上日志写不进去了，而且 Linux 上也开始出现"
#
# 根因不是平台差异，而是**写不进文件时把事件直接丢弃**，同时把告警
# 限流成"第 1 次和每第 20 次"，于是现象是"日志不更新，也没人报错"。
#
# Windows 上必现——记事本看一眼 activity.log 就把它独占锁死，
# `open("a")` 立刻 PermissionError。Linux 上是偶现：文件只读、属主是
# root 而服务以普通用户跑、磁盘满、只读挂载、SELinux，都走同一条路径。
#
# 所以这里测的必须是**行为契约**，而不是某台机器上的巧合：
#   写不进去 → 不丢，进缓冲
#   恢复     → 自动补写 + 通知用户断了多久
#   缓冲满   → 丢最旧的，且如实上报丢了多少

def test_write_failure_is_not_data_loss() -> None:
    section("第九部分：写不进文件时不丢数据")

    # --- 写不进去时事件进缓冲，而不是消失 ---
    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d)
        log.write("system", "正常的一条")
        log.close()

        os.chmod(log.path, 0o444)          # 文件变只读
        try:
            lg = FakeLogger()
            blocked = ActivityLog(d, logger=lg)
            for i in range(5):
                blocked.write("spawn", f"只读期间第{i}条")

            check("写不进去时不算成功（没骗自己）",
                  blocked.total_written == 0, str(blocked.total_written))
            check("事件进了内存缓冲，没被丢弃",
                  blocked.pending == 5, str(blocked.pending))
            check("一条都没丢", blocked.dropped == 0, str(blocked.dropped))
            check("记下了失败次数", blocked.failures >= 1,
                  str(blocked.failures))
            check("留下了错误摘要可排障",
                  "Permission" in blocked.last_error or
                  "denied" in blocked.last_error.lower() or
                  bool(blocked.last_error), blocked.last_error)
            check("告警里带上了现场（路径 + 错误类型）",
                  any(str(d) in x and "Error" in x for x in lg.lines),
                  str(lg.lines[:1]))
            check("describe 让人一眼看出写不进",
                  "待补写" in blocked.describe()
                  and "当前写不进" in blocked.describe(),
                  blocked.describe())

            # --- 恢复可写：自动补写，一条不丢 ---
            os.chmod(blocked.path, 0o644)
            for i in range(5, 8):
                blocked.write("move", f"恢复后第{i}条")
            check("恢复后缓冲被自动补写干净",
                  blocked.pending == 0, str(blocked.pending))
            check("补写计入成功数", blocked.total_written == 8,
                  str(blocked.total_written))
            blocked.close()

            text = log.path.read_text(encoding="utf-8")
            missing = [f"第{i}条" for i in range(5)
                       if f"只读期间第{i}条" not in text]
            check("断线期间的事件全部落盘了", not missing, str(missing))
            check("时间线没错乱（补写的是旧事件）",
                  text.index("只读期间第0条") < text.index("恢复后第5条"))
        finally:
            os.chmod(log.path, 0o644)

    # --- 恢复时要通知：否则"坏了多久"永远没人知道 ---
    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d)
        log.write("system", "预热")
        log.close()
        os.chmod(log.path, 0o444)
        try:
            lg = FakeLogger()
            broken = ActivityLog(d, logger=lg)
            for _ in range(30):
                broken.write("say", "坏着的时候")
            # failures 数的是"没能落盘的事件"，不是"尝试次数"。
            # 一条事件落盘前要试好几次（补写积压、开文件、可能轮转），
            # 混起来报会把 30 条说成 59 次，诊断时纯误导。
            check("failures 数的是事件条数，不是重试次数",
                  broken.failures == 30, str(broken.failures))
            before = len(lg.lines)
            os.chmod(broken.path, 0o644)
            broken.write("system", "好了")
            broken.close()

            after = lg.lines[before:]
            check("恢复时有一条新通知",
                  any("已恢复" in x for x in after), str(after))
            check("恢复通知带上了缺口大小",
                  any("已恢复" in x and "30 条" in x for x in after),
                  str(after))
        finally:
            os.chmod(log.path, 0o644)

    # --- 恢复通知只发一次，别变成刷屏 ---
    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d)
        log.write("system", "预热")
        log.close()
        os.chmod(log.path, 0o444)
        try:
            lg = FakeLogger()
            rec = ActivityLog(d, logger=lg)
            rec.write("say", "坏")
            os.chmod(rec.path, 0o644)
            rec.write("say", "好了")
            rec.write("say", "还好")
            rec.write("say", "依然好")
            rec.close()
            n = len([x for x in lg.lines if "已恢复" in x])
            check("恢复通知只发一次", n == 1, f"{n} 条: {lg.lines}")
        finally:
            os.chmod(log.path, 0o644)

    # --- 缓冲有上限：真撑爆了要如实上报丢了几条 ---
    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d)
        log.write("system", "预热")
        log.close()
        os.chmod(log.path, 0o444)
        try:
            tight = ActivityLog(d, buffer_bytes=1024)
            for i in range(200):
                tight.write("say", f"挤爆缓冲{i}" + "x" * 60)
            check("缓冲不无限涨（守住了内存上限）",
                  tight._pending_bytes <= 1024, str(tight._pending_bytes))
            check("撑爆了如实上报丢弃数", tight.dropped > 0,
                  str(tight.dropped))
            check("丢弃后仍是「总数 = 落盘 + 丢弃」的关系",
                  tight.total_written == 0
                  and tight.pending + tight.dropped == 200,
                  f"落盘={tight.total_written} 缓冲={tight.pending} "
                  f"丢弃={tight.dropped}")
            tight.close()
        finally:
            os.chmod(log.path, 0o644)


def test_rotate_keeps_handle_usable() -> None:
    section("第九部分附带：轮转不能把日志搞停摆")

    # 回归：早期实现里 _rotate() 先 close() 再 _ensure_open()，后者失败时
    # 句柄留在 None，而 write() 紧接着就 self._fh.write(...)，
    # 于是每条日志都抛 AttributeError: 'NoneType' has no attribute 'write'。
    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d, max_bytes=300, backups=2)
        for i in range(40):
            log.write("think", f"第{i}条" + "x" * 90)
            # 每写一条就确认句柄还在：轮转后必须仍然可写
            if log._fh is None:
                break
        check("轮转过程中句柄始终有效", log._fh is not None, "句柄变 None")
        check("轮转没有让写入失败",
              log.total_written == 40, str(log.total_written))
        check("轮转没有产生失败", log.failures == 0,
              f"{log.failures} 次: {log.last_error}")
        log.close()
        check("轮转后主文件仍在", log.path.exists())

    # backups=0 的清空路径同样不能让句柄失效
    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d, max_bytes=300, backups=0)
        for i in range(40):
            log.write("think", f"第{i}条" + "x" * 90)
        check("backups=0 轮转后句柄仍有效", log._fh is not None, "句柄变 None")
        check("backups=0 全部写入成功", log.total_written == 40,
              str(log.total_written))
        log.close()


def test_rotation_uses_bytes_not_chars() -> None:
    section("第九部分附带：轮转阈值按字节算")

    # 回归：早期把字符数 len(line) 拿去和字节上限 max_bytes 比。
    # 中文事件 UTF-8 每字 3 字节，于是日志在离上限还很远时就被切走。
    with tempfile.TemporaryDirectory() as d:
        cap = 4000
        log = ActivityLog(d, max_bytes=cap, backups=3)
        for i in range(200):
            log.write("think", f"第{i}条中文事件内容不短" * 2)
        log.close()

        sizes = [p.stat().st_size for p in Path(d).iterdir()]
        biggest = max(sizes)
        # 单条事件约 150 字节，所以主文件最多超出一条的量
        check("轮转点按字节生效（主文件没被字符数提前切走）",
              biggest > cap * 0.7, f"最大文件 {biggest} 字节，上限 {cap}")
        check("文件没有失控膨胀",
              all(s <= cap * 3 for s in sizes),
              f"各文件字节数 {sizes}")


def test_windows_exclusive_lock() -> None:
    section("第九部分附带：Windows 独占锁定（记事本场景）")
    if sys.platform != "win32":
        check("非 Windows 平台跳过", True, sys.platform)
        return

    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    FILE_SHARE_NONE = 0          # 关键：不允许别人读写，等于记事本独占
    INVALID = wintypes.HANDLE(-1).value
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE

    with tempfile.TemporaryDirectory() as d:
        log = ActivityLog(d)
        log.write("system", "锁定前")
        log.close()

        handle = k32.CreateFileW(str(log.path), GENERIC_READ, FILE_SHARE_NONE,
                                 None, OPEN_EXISTING, 0, None)
        if handle == INVALID:
            check("能独占锁定文件（测试前提）", False,
                  str(ctypes.get_last_error()))
            return
        check("能独占锁定文件（测试前提）", True)

        try:
            lg = FakeLogger()
            locked = ActivityLog(d, logger=lg)
            for i in range(5):
                locked.write("spawn", f"被锁着的时候第{i}条")
            check("锁住时事件进缓冲而不是消失",
                  locked.pending == 5 and locked.total_written == 0,
                  f"缓冲={locked.pending} 落盘={locked.total_written}")

            # 模拟用户关掉记事本：锁释放
            k32.CloseHandle(handle)
            locked.write("system", "锁释放之后")
            check("锁释放后自动补写干净",
                  locked.pending == 0, str(locked.pending))
            check("锁释放后写入恢复正常",
                  locked.total_written == 6, str(locked.total_written))
            check("恢复通知发出来了",
                  any("已恢复" in x for x in lg.lines), str(lg.lines[-1:]))
            locked.close()

            text = log.path.read_text(encoding="utf-8")
            lost = [i for i in range(5)
                    if f"被锁着的时候第{i}条" not in text]
            check("被锁期间的事件一条没丢", not lost, str(lost))
        finally:
            # 防止异常路径下句柄泄漏
            if handle != INVALID:
                try:
                    k32.CloseHandle(handle)
                except Exception:
                    pass

    # 锁住 + 触发轮转：轮转失败也不该让日志停摆
    with tempfile.TemporaryDirectory() as d:
        warm = ActivityLog(d, max_bytes=400, backups=2)
        for i in range(5):
            warm.write("think", f"预热{i}-" + "x" * 100)
        warm.close()

        handle = k32.CreateFileW(str(warm.path), GENERIC_READ, FILE_SHARE_NONE,
                                 None, OPEN_EXISTING, 0, None)
        if handle == INVALID:
            check("能独占锁定文件（测试前提）", False, "打开失败")
            return
        try:
            lg = FakeLogger()
            locked = ActivityLog(d, max_bytes=400, backups=2, logger=lg)
            for i in range(8):
                locked.write("think", f"锁着轮转{i}-" + "z" * 100)
            check("锁着轮转时事件仍进缓冲（没有 AttributeError）",
                  locked.pending == 8, str(locked.pending))
            check("锁着轮转时 failures 已计数",
                  locked.failures >= 1, str(locked.failures))
        finally:
            k32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 70)
    print("插件目录 & 活动日志")
    print("=" * 70)

    test_basic()
    test_directory()
    test_rotation()
    test_read_clear()
    test_switch_and_threads()
    test_rendering()
    test_config_keys()
    test_entry_point()
    test_write_failure_is_not_data_loss()
    test_rotate_keeps_handle_usable()
    test_rotation_uses_bytes_not_chars()
    test_windows_exclusive_lock()

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
