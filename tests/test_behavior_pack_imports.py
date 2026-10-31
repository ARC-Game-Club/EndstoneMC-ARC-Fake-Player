"""行为包不许静态 import "可选导出"——缺一个导出，整个包就在链接期死了。

事故背景
--------
2026-10-31 18:18 两台服同时报"行为包未连接"，根因是：

    [Scripting] [arc_fake_player 假人桥接] SyntaxError: Could not find export
    'Vector3' in module '@minecraft/server'

本机 BDS 1.26.52.3 的 @minecraft/server **没有导出 Vector3 这个类**（原生类型
还在，只是没导出到 JS 侧）。而把 Vector3 写进 `import { ... } from` 的花括号里
是**静态**导入：链接期就失败 → 整个行为包加载失败 → 心跳 `bot:ping` 永远收不到
`bot:pong` → Python 侧判定"行为包未连接" → 假人一个都上不来。

同一个错误让三个包一起阵亡：假人桥接包、枪械包（`Lost Files Arsenal`）、
`arc_gun_diag`。

修法
----
**可选**的导出一律走命名空间导入 + 运行时判断：

    import * as server from "@minecraft/server";
    const Vec3 = server.Vector3;      // 没有就是 undefined，不炸

命名空间导入不会因为缺导出而失败（缺的只是 undefined），所以行为包照常加载。

这个文件就是防止有人再把 Vector3 之类的写回 import 列表。
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import dukpy

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "endstone_arc_fake_player" / "behavior_pack" / "scripts"

#: 这些导出**不是每个 @minecraft/server 版本都有**（Vector3 在 MC 26.52 的模块里
#: 就没有导出），静态 import 会让整个行为包加载失败。
#: 类型/工具类最容易在模块大版本之间搬家，所以重点盯它们。
OPTIONAL_EXPORTS = ("Vector3", "Vector2", "Location", "Rotation")

#: 从 @minecraft/server 静态 import 一整串名字
RE_STATIC_IMPORT = re.compile(
    r"""import\s*\{([^}]*)\}\s*from\s*["']@minecraft/server["']""", re.S
)

#: 命名空间导入（正确写法）
RE_NAMESPACE_IMPORT = re.compile(
    r"""import\s+\*\s+as\s+(\w+)\s+from\s*["']@minecraft/server["']"""
)

#: 任何 import（命名空间/默认/裸模块）
RE_ANY_IMPORT = re.compile(r"""^[ \t]*import\s+[^;]*?;[ \t]*$""", re.M | re.S)

RE_EXPORT_PREFIX = re.compile(r"^\s*export\s+", re.M)


def strip_comments(src: str) -> str:
    """去掉 JS 注释（分块 + 行），但**不动字符串里的内容**。

    为什么需要：注释里也会出现 "import" 这个词（比如"跨行为包没法 import 对方的
    模块"），不先剥掉就会被当成 import 语句，把后面真正的代码一起吃掉。
    """
    out: list[str] = []
    i = 0
    n = len(src)
    quote: str | None = None
    while i < n:
        ch = src[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _read_scripts() -> list[Path]:
    return sorted(SCRIPTS_DIR.glob("*.js"))


def _strip_for_dukpy(src: str, mock_source: str) -> str:
    """把 ES 模块语法换成能在 dukpy 里跑的样子。

    - `@minecraft/server` 的命名空间导入换成假的模块对象（由调用方给）；
    - 其它 import 语句整条删掉（dukpy 不认识 ES 模块）；
    - `export ` 前缀去掉。
    """
    src = strip_comments(src)
    src = RE_NAMESPACE_IMPORT.sub(mock_source, src, count=1)
    src = RE_ANY_IMPORT.sub("", src)
    src = RE_EXPORT_PREFIX.sub("", src)
    return src


#: 一个"这台服真的长什么样"的假模块：没有 Vector3。
MOCK_WITHOUT_VECTOR3 = """
const server = {
    world: {},
    system: { afterEvents: { scriptEventReceive: { subscribe() {} } } },
    EntityDamageCause: {},
};
"""

#: 同一个假模块，但**有** Vector3（模拟旧一点的模块版本）。
MOCK_WITH_VECTOR3 = """
const server = {
    world: {},
    system: { afterEvents: { scriptEventReceive: { subscribe() {} } } },
    EntityDamageCause: {},
    Vector3: class Vector3 {
        constructor(x, y, z) { this.x = x; this.y = y; this.z = z; }
    },
};
"""

#: 挂在模块末尾的探针：把内部状态读出来给 Python 断言。
PROBE = """
;JSON.stringify({
    vec3Type: typeof Vec3,
    nativeIsNull: nativeVec(1, 2, 3) === null,
    toVecPlain: JSON.stringify(toVec(1, 2, 3)),
    toVecIsInstance: typeof Vec3 === "undefined"
        ? null
        : (toVec(1, 2, 3) instanceof Vec3),
})
"""


def _run_probe(mock_source: str) -> dict:
    """在 dukpy 里加载 arena_combat.js，并跑探针。"""
    src = (SCRIPTS_DIR / "arena_combat.js").read_text(encoding="utf-8")
    src = _strip_for_dukpy(src, mock_source)
    src = src + PROBE
    return json.loads(dukpy.evaljs(src))


class BehaviorPackImportTests(unittest.TestCase):
    def test_no_static_import_of_optional_exports(self):
        """行为包脚本里不能有 Vector3 之类的静态导入。"""
        offenders = []
        for path in _read_scripts():
            text = strip_comments(path.read_text(encoding="utf-8"))
            for names in RE_STATIC_IMPORT.findall(text):
                wanted = {
                    n.strip().split(" as ")[0].strip()
                    for n in names.split(",")
                    if n.strip()
                }
                hit = wanted & set(OPTIONAL_EXPORTS)
                if hit:
                    offenders.append(f"{path.name}: {sorted(hit)}")
        self.assertEqual(
            offenders, [],
            "行为包静态导入了可选导出（缺导出会让整个包加载失败）："
            + "; ".join(offenders),
        )

    def test_namespace_import_is_used(self):
        """必须用命名空间导入 @minecraft/server（缺导出时才不会炸）。"""
        text = strip_comments(
            (SCRIPTS_DIR / "arena_combat.js").read_text(encoding="utf-8"))
        self.assertRegex(text, RE_NAMESPACE_IMPORT)
        self.assertIn("server.Vector3", text)

    def test_module_loads_without_vector3_export(self):
        """这台服没有 Vector3 导出时，行为包照样能加载（事故的直接回归测试）。"""
        probe = _run_probe(MOCK_WITHOUT_VECTOR3)
        self.assertEqual(probe["vec3Type"], "undefined")
        # 造不出原生实例 → 视线判定老实跳过，而不是拿普通对象去撞原生 API
        self.assertTrue(probe["nativeIsNull"])
        # 退化坐标仍是可用的 {x,y,z}
        self.assertEqual(probe["toVecPlain"], '{"x":1,"y":2,"z":3}')
        self.assertIsNone(probe["toVecIsInstance"])

    def test_native_vector3_is_used_when_available(self):
        """有 Vector3 类的服务器上，坐标要造出真正的原生实例。"""
        probe = _run_probe(MOCK_WITH_VECTOR3)
        self.assertEqual(probe["vec3Type"], "function")
        self.assertFalse(probe["nativeIsNull"])
        self.assertTrue(probe["toVecIsInstance"])


def main():
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(BehaviorPackImportTests))
    if not result.wasSuccessful():
        return 1
    print(f"全部 {result.testsRun} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
