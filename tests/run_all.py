"""一次跑完所有测试文件，并汇总总数。

为什么要有这个文件
------------------
七个测试文件各自是 main() + check() 的自运行风格，可以单独跑::

    python3.11 tests/test_hybrid.py

但两个不方便的地方：

1. **没有装 Endstone 的机器上会直接 import 失败**（原因见
   `tests/endstone_stub.py` 的说明）。这个脚本会先装桩再跑。
2. **数总数要靠人肉把七个 "全部 N 项通过" 加起来**，改测试之后特别
   容易忘了同步 README 里的数字。这个脚本直接打印总数。

用法::

    python3.11 tests/run_all.py            # 全部
    python3.11 tests/run_all.py hybrid     # 只跑名字里带 hybrid 的
    python3.11 tests/run_all.py -q         # 只打每个文件的结论

退出码：有任何一项失败就是 1，全绿是 0（方便挂 CI）。
"""

from __future__ import annotations

import io
import re
import sys
import traceback
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(ROOT))

from tests.endstone_stub import install  # noqa: E402

install()

#: 顺序就是打印顺序。把"纯逻辑"放前面，越靠后越贴近真实服务器环境。
ALL_TESTS = [
    "test_pure_agent.py",
    "test_agent_loop.py",
    "test_hybrid.py",
    "test_context.py",
    "test_activity.py",
    "test_surface.py",
    "test_despawn_body.py",
    "test_spawn_point.py",
    "test_v2_fixes.py",
    "test_interaction_fixes.py",
    "test_plugin_fixes.py",
    "test_pack_upgrade.py",
    "test_behavior_pack_imports.py",
    "test_crowd.py",
    "test_social.py",
    "test_mob_reflex.py",
    "test_roam.py",
    "test_chat_persona.py",
    "test_tpa_auto_accept.py",
    "test_humanlike.py",
]

#: 各文件用自己的 main() 打印的结论行，两种写法都在这了。
VERDICT_PATTERNS = [
    re.compile(r"全部\s*(\d+)\s*项通过"),
    re.compile(r"(\d+)\s*项全部通过"),
    re.compile(r"通过\s*(\d+)/(\d+)"),
    re.compile(r"总计\s*(\d+)\s*项"),
]


def parse_count(text: str) -> int | None:
    """从输出里抠出"通过了多少项"。抠不到返回 None。"""
    for pat in VERDICT_PATTERNS:
        m = pat.search(text)
        if m:
            return int(m.group(1))
    return None


def has_failure(text: str) -> bool:
    """看有没有 FAIL / 崩溃的字样。

    ⚠️ 这里**不能**去搜中文的"失败 N 次"——测试的 PASS 文案里就有
    `-- 'AI 连续失败 3 次：e3'`（在断言"重试上限"），一搜就误报。
    每个测试文件的 main() 自己会打 `❌ N 项失败` 或 `✅ 全部 N 项通过`，
    认这两种终结句最稳。
    """
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("FAIL") or s.startswith("!!"):
            return True
        if s.startswith("❌"):
            return True
        if "Traceback (most recent call last)" in s:
            return True
    return False


def main(argv: list[str]) -> int:
    quiet = "-q" in argv or "--quiet" in argv
    filters = [a for a in argv if not a.startswith("-")]

    wanted = []
    for name in ALL_TESTS:
        if not filters:
            wanted.append(name)
            continue
        if any(f in name for f in filters):
            wanted.append(name)

    if not wanted:
        print(f"没有匹配的测试文件。过滤器: {filters}")
        print("可选:", ", ".join(ALL_TESTS))
        return 1

    results: list[tuple[str, int | None, bool, str]] = []

    for name in wanted:
        path = TESTS_DIR / name
        print(f"\n{'#' * 70}\n# {name}\n{'#' * 70}")

        buf = io.StringIO()
        crashed = ""
        try:
            with redirect_stdout(buf), redirect_stderr(buf):
                code = path.read_text(encoding="utf-8")
                # 用 exec 而不是 runpy：runpy 会在同一次进程里
                # 反复设 __name__，某些模块的顶层缓存会互相干扰。
                g = {"__name__": "__main__", "__file__": str(path)}
                exec(compile(code, str(path), "exec"), g)  # noqa: S102
        except SystemExit as exc:
            if exc.code not in (0, None):
                crashed = f"退出码 {exc.code}"
        except BaseException:  # noqa: BLE001
            crashed = traceback.format_exc()

        text = buf.getvalue()
        if quiet:
            # 只留结论行
            keep = [ln for ln in text.splitlines()
                    if "项通过" in ln or ln.strip().startswith("FAIL")
                    or ln.strip().startswith("===")]
            print("\n".join(keep))
        else:
            print(text, end="" if text.endswith("\n") else "\n")
        if crashed:
            print(crashed)

        count = parse_count(text)
        failed = bool(crashed) or has_failure(text)
        results.append((name, count, failed, crashed))

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 70)
    print("汇总")
    print("=" * 70)

    total = 0
    any_fail = False
    for name, count, failed, _ in results:
        mark = "✗" if failed else "✓"
        shown = f"{count} 项" if count is not None else "项数未知"
        print(f"  {mark}  {name:<24} {shown}")
        if count:
            total += count
        if failed:
            any_fail = True

    print("-" * 70)
    print(f"  合计: {total} 项")
    if any_fail:
        print("\n❌ 有失败项，上面标记 ✗ 的文件需要看输出。")
        return 1
    print(f"\n✅ 全部 {total} 项通过（{len(results)} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
