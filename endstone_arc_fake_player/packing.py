"""行为包自动安装。

按 BDS 的标准方式装行为包，需要动三个地方：

1. **包本体** → 服务器根的 ``behavior_packs/<包名>/``（服务端扫描的位置）。
   同时也往世界目录下的 ``behavior_packs/`` 放一份，兼容把世界目录
   整体带走（备份、上传存档）的场景。

2. **激活登记** → 世界目录的 ``world_behavior_packs.json``，写进包 UUID 和版本。
   没有这一步包会被扫描到但不会生效。

3. **Beta APIs 实验开关** → ``level.dat`` 里的 ``experiments`` 复合标签。
   ``@minecraft/server-gametest`` 属于 beta 模块，不开这个开关脚本会加载失败。
   这一步用定点字节补丁，并且**先备份**。

改完必须重启服务器才生效——这点在日志里会明确提示，不然用户会以为装失败了。
"""

from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path
from typing import Any

from .nbt import (
    find_byte_value,
    find_compound_body,
    find_compound_end,
    make_byte_tag,
    make_compound_tag,
    read_bedrock_nbt,
    root_body_offset,
)


#: 行为包标识。必须和 behavior_pack/manifest.json 里的 header.uuid 一致。
PACK_UUID = "7d4c1a90-2f63-4b8e-a5d1-9c0e7b3f6a24"
PACK_VERSION = [3, 8, 0]
#: 释放到磁盘上的目录名
PACK_DIR_NAME = "arc_fake_player_bridge"
LEGACY_PACK_DIR_NAME = "pureagent_bridge"

#: 保留用于外部检查的核心文件清单；_fingerprint 还会扫描结构等其它载荷。
_FINGERPRINT_REL = ("manifest.json", "scripts/main.js")

#: level.dat 里 experiments 复合标签需要设置的 BYTE 字段。
#: gametest=1 就是界面上那个 "Beta APIs" 开关。
EXPERIMENT_FIELDS = {
    "gametest": 1,                        # Beta APIs
    "experiments_ever_used": 1,           # 标记曾用过实验功能
    "saved_with_toggled_experiments": 1,  # 标记保存时已开启
}


# ---------------------------------------------------------------------------
# level.dat 实验开关
# ---------------------------------------------------------------------------

def experiments_enabled(level_dat: Path) -> bool:
    """检查 level.dat 里 Beta APIs 是否已经开着。"""
    if not level_dat or not level_dat.exists():
        return False
    try:
        data = read_bedrock_nbt(level_dat.read_bytes(), has_header=True)
        return data.get("experiments", {}).get("gametest", 0) == 1
    except Exception:
        return False


def enable_experiments(level_dat: Path, *, backup: bool = True) -> tuple[bool, str]:
    """在 level.dat 里打开 Beta APIs。

    返回 (是否修改了文件, 说明文本)。

    实现方式是定点补丁而非重新序列化：只改动 experiments 相关的少数字节，
    其余数据（世界种子、出生点、玩家位置、游戏规则）原样不动。
    改之前会留一份 ``level.dat.bak``。
    """
    if not level_dat.exists():
        return False, "level.dat 不存在"
    if experiments_enabled(level_dat):
        return False, "Beta APIs 已启用"

    raw = level_dat.read_bytes()
    if len(raw) < 8:
        return False, "level.dat 太短，格式异常"

    try:
        _version, nbt_len = struct.unpack("<II", raw[:8])
        root = root_body_offset(raw, has_header=True)
        exp_body = find_compound_body(raw, root, "experiments")
    except Exception as exc:
        return False, f"解析 level.dat 失败: {exc}"

    # (偏移, 删除长度, 插入字节)
    patches: list[tuple[int, int, bytes]] = []

    try:
        if exp_body is not None:
            for name, value in EXPERIMENT_FIELDS.items():
                pos = find_byte_value(raw, exp_body, name)
                if pos is not None:
                    current = struct.unpack("<b", raw[pos:pos + 1])[0]
                    if current != value:
                        patches.append((pos, 1, struct.pack("<b", value)))
                else:
                    end = find_compound_end(raw, exp_body)
                    patches.append((end, 0, make_byte_tag(name, value)))
        else:
            # 整个 experiments 复合标签都不存在——在根 compound 的 TAG_END
            # 前插入它。这是从没开过实验功能的世界，很常见。
            end = find_compound_end(raw, root)
            body = b"".join(make_byte_tag(n, v) for n, v in EXPERIMENT_FIELDS.items())
            patches.append((end, 0, make_compound_tag("experiments", body)))
    except Exception as exc:
        return False, f"定位标签失败: {exc}"

    if not patches:
        return False, "无需修改"

    if backup:
        bak = level_dat.with_suffix(".dat.bak")
        if not bak.exists():
            try:
                shutil.copy2(level_dat, bak)
            except Exception as exc:
                return False, f"备份失败，已中止: {exc}"

    # 从后往前应用，避免前面的插入让后面的偏移失效
    patches.sort(key=lambda p: p[0], reverse=True)
    result = bytearray(raw)
    for offset, delete_len, insert in patches:
        result[offset:offset + delete_len] = insert

    new_len = len(result) - 8
    if new_len != nbt_len:
        struct.pack_into("<I", result, 4, new_len)

    try:
        level_dat.write_bytes(bytes(result))
    except Exception as exc:
        return False, f"写入失败: {exc}"
    return True, "已启用 Beta APIs（需要重启服务器生效）"


# ---------------------------------------------------------------------------
# 世界目录定位
# ---------------------------------------------------------------------------

def find_world_dirs(*, level_name: str = "", logger: Any = None) -> list[Path]:
    """找出所有可能的世界目录。

    不同部署方式的目录结构不一样，这里把常见的几种都试一遍：
    BDS 官方的 ``bedrock_server/worlds/<名字>``、直接跑在服务器根目录的
    ``worlds/<名字>``、以及世界就在当前目录的情况。
    """
    def log(msg: str) -> None:
        if logger is not None:
            logger.debug(msg)

    candidates: list[Path] = []
    bases = [
        Path("bedrock_server") / "worlds",
        Path.cwd() / "worlds",
        Path.cwd() / "bedrock_server" / "worlds",
    ]

    if level_name:
        for base in bases:
            d = base / level_name
            if (d / "level.dat").exists():
                resolved = d.resolve()
                if resolved not in candidates:
                    candidates.append(resolved)
        if candidates:
            return candidates

    # 兜底：扫一遍，任何一个带 level.dat 的目录都算
    for base in bases:
        if not base.is_dir():
            continue
        try:
            for child in base.iterdir():
                if child.is_dir() and (child / "level.dat").exists():
                    r = child.resolve()
                    if r not in candidates:
                        candidates.append(r)
        except Exception as exc:
            log(f"扫描 {base} 失败: {exc}")

    # 最兜底：当前目录本身就是世界目录
    if (Path.cwd() / "level.dat").exists():
        r = Path.cwd().resolve()
        if r not in candidates:
            candidates.append(r)

    return candidates


def find_server_root() -> Path | None:
    """找出服务器根目录——也就是 ``behavior_packs/`` 该待的地方。"""
    cwd = Path.cwd()
    # 在 bedrock_server 子目录里跑的情况
    if (cwd / "bedrock_server").is_dir():
        return (cwd / "bedrock_server").resolve()
    # 直接就是服务器根
    if (cwd / "worlds").is_dir() or (cwd / "server.properties").exists():
        return cwd.resolve()
    # 退一步：只要有 behavior_packs 或 worlds 就算
    for probe in (cwd, cwd.parent):
        if (probe / "behavior_packs").is_dir() or (probe / "worlds").is_dir():
            return probe.resolve()
    return cwd.resolve()


# ---------------------------------------------------------------------------
# 释放与注册
# ---------------------------------------------------------------------------

def _fingerprint(root: Path) -> dict[str, str]:
    """载荷目录的内容指纹：路径 → sha256。

    包含脚本、manifest 和结构文件；任何载荷更新都必须触发重新释放。
    路径统一用正斜杠，确保 Windows 的比较与发布记录一致。
    """
    import hashlib

    out: dict[str, str] = {}
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.name in (_RELEASE_NOTE, _LEGACY_RELEASE_NOTE):
            continue
        rel = f.relative_to(root).as_posix()
        out[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


#: 记账文件名。存在目标目录里，记录"我们上次发布出去的内容长什么样"。
_RELEASE_NOTE = ".arc_fake_player_released.json"
_LEGACY_RELEASE_NOTE = ".pureagent_released.json"


def _read_release(target: Path) -> dict[str, str]:
    for note in (_RELEASE_NOTE, _LEGACY_RELEASE_NOTE):
        try:
            raw = (target / note).read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                return {str(k).replace("\\", "/"): str(v)
                        for k, v in data.items() if isinstance(v, str)}
        except Exception:
            pass
    return {}


def _migrate_legacy_pack(target: Path) -> tuple[bool, bool, str]:
    """只迁移同父目录、UUID 已确认的旧包；遇到碰撞保留两边文件。"""
    legacy = target.with_name(LEGACY_PACK_DIR_NAME)
    if not legacy.exists():
        return True, False, "无需迁移"
    try:
        parent = target.parent.resolve()
        if legacy.resolve().parent != parent or target.resolve().parent != parent:
            return False, False, f"行为包目录指向父目录之外，未迁移: {legacy} → {target}"
        if legacy.is_symlink() or target.is_symlink():
            return False, False, f"行为包目录是符号链接，未迁移: {legacy} → {target}"
        manifest = json.loads((legacy / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("header", {}).get("uuid") != PACK_UUID:
            return False, False, f"旧目录的行为包 UUID 不匹配，未迁移: {legacy}"
        if target.exists():
            return False, False, (
                f"新旧行为包目录同时存在，未覆盖或删除任何文件: {legacy} 和 {target}。"
                "请停服备份并合并这两个目录后重试。"
            )
        legacy.rename(target)
    except Exception as exc:
        return False, False, f"旧行为包目录迁移失败（未覆盖目标）: {exc}"
    return True, True, f"已迁移旧行为包目录: {legacy} → {target}"


def _write_release(target: Path, fp: dict[str, str]) -> None:
    try:
        (target / _RELEASE_NOTE).write_text(
            json.dumps(fp, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        pass


def _classify_drift(src_fp: dict[str, str], dst_fp: dict[str, str],
                    released: dict[str, str]) -> tuple[list[str], list[str]]:
    """把目标目录和源之间的差异分成两类。

    返回 `(要覆盖的, 要保住的)`。

    判据是"目标文件**和上次发布时**是否一致"：

      · 一致（或没有发布记录）→ 用户没动过，这是插件自己的旧版本 → **覆盖**
      · 不一致                → 用户手改过 → **保住**，并在日志里说明

    为什么需要这道区分：`header.version` 判据太粗（改了代码但没改版本号，
    补丁就永远送不到）；而"内容不同就覆盖"又太狠（会把用户自己调过的
    参数抹掉）。用发布记账来分，两边都照顾到。
    """
    overwrite: list[str] = []
    keep: list[str] = []
    for rel, want in src_fp.items():
        have = dst_fp.get(rel)
        if have is None:
            overwrite.append(rel)          # 目标缺失 → 必须要
            continue
        if have == want:
            continue                       # 已经一样 → 不用动
        # 内容不同。看用户有没有改过它。
        was = released.get(rel)
        if was is not None and have != was:
            keep.append(rel)               # 用户改过 → 保住
        else:
            overwrite.append(rel)          # 我们的旧版本 → 更新
    return overwrite, keep


def extract_pack(source: Path, target: Path, *, logger: Any = None,
                 force: bool = False) -> tuple[bool, str]:
    """把行为包文件复制到目标目录。

    三种结局：

      · **内容一致** → 跳过，什么都不动
      · **内容是我们的旧版本** → 覆盖（升级补丁要送到）
      · **用户手改过** → 那一个文件保住，其余照常更新，日志里点名

    以前这里的判据是 `header.version` 相等就跳过。那个值只有人手动改
    才会变，于是"升级插件"永远不反映到磁盘上，而且连提示都没有——
    用户看到的是"作者说修好了，但我这里还是老样子"。

    但也不能简单改成"内容不同就覆盖"：那会抹掉用户自己调过的参数
    （比如加了调试输出、改了挖掘超时）。所以引入发布记账文件，
    用来区分"用户改的"和"我们发布的旧版本"。
    """
    def log(msg: str) -> None:
        if logger is not None:
            logger.info(msg)

    if not source.is_dir() or not (source / "manifest.json").exists():
        return False, f"行为包源文件缺失: {source}"

    try:
        json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"读取 manifest 失败: {exc}"

    src_fp = _fingerprint(source)
    overwrite: list[str] = list(src_fp)
    kept: list[str] = []
    released = _read_release(target)

    if not force and (target / "manifest.json").exists():
        dst_fp = _fingerprint(target)
        if src_fp and all(dst_fp.get(rel) == value for rel, value in src_fp.items()):
            return True, "内容一致，跳过"
        overwrite, kept = _classify_drift(src_fp, dst_fp, released)
        if kept:
            log(f"以下文件你手动改过，已保留不覆盖：{kept}")
        if not overwrite:
            return True, f"无需更新（你改过的已保留：{kept}）"

    try:
        target.mkdir(parents=True, exist_ok=True)
        for rel in overwrite:
            item = source / rel
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dst)
    except Exception as exc:
        return False, f"复制失败: {exc}"

    # 记账：把"我们这次发布的内容"写下来，下次才分得清谁的改动。
    # 用户改过的文件保留原发布基线。若记成用户版本，下次升级就会把
    # 它误判为插件发布的原版并覆盖，导致第二轮更新时丢失用户修改。
    shipped = dict(src_fp)
    for rel in kept:
        if rel in released:
            shipped[rel] = released[rel]
        else:
            shipped.pop(rel, None)
    _write_release(target, shipped)

    msg = "已释放" if not kept else f"部分更新（保留 {kept}）"
    log(f"行为包已释放到 {target}（{msg}）")
    return True, msg


def register_pack(world_dir: Path, *, logger: Any = None) -> tuple[bool, str]:
    """把行为包登记进 ``world_behavior_packs.json``。

    文件格式不对时宁可不动——覆盖它会把用户其他行为包的激活状态一起弄丢。
    """
    def log(msg: str) -> None:
        if logger is not None:
            logger.info(msg)

    packs_file = world_dir / "world_behavior_packs.json"

    if packs_file.exists():
        try:
            data = json.loads(packs_file.read_text(encoding="utf-8"))
        except Exception as exc:
            return False, f"解析失败，为避免覆盖其他行为包已跳过: {exc}"
        if not isinstance(data, list):
            return False, "格式异常（不是数组），为避免覆盖已跳过"
    else:
        data = []

    found = False
    changed = False
    updated = []
    for entry in data:
        if isinstance(entry, dict) and entry.get("pack_id") == PACK_UUID:
            if found:
                changed = True
                continue
            found = True
            if entry.get("version") != PACK_VERSION:
                entry = dict(entry, version=list(PACK_VERSION))
                changed = True
        updated.append(entry)
    if found and not changed:
        return True, "已注册过"
    if not found:
        updated.append({"pack_id": PACK_UUID, "version": list(PACK_VERSION)})

    try:
        tmp = packs_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(updated, indent=2), encoding="utf-8")
        tmp.replace(packs_file)
    except Exception as exc:
        return False, f"写入失败: {exc}"

    log(f"行为包已登记到 {packs_file}")
    return True, "已更新登记版本" if found else "已登记"


# ---------------------------------------------------------------------------
# 对外总入口
# ---------------------------------------------------------------------------

def install(source_dir: Path, *, level_name: str = "", logger: Any = None,
            touch_level_dat: bool = True) -> dict[str, Any]:
    """完整安装流程。返回一份可打印的结果报告。

    ``report["ok"]`` 才是权威结论。只要有任何一项必须的动作没做成，
    它就是 False，并且 ``errors`` 里会列出**具体原因和修复办法**。

    刻意不做"尽力而为"：没找到世界目录、level.dat 写不进去、
    world_behavior_packs.json 格式坏了——这些都是**失败**，
    不是可以忽略的警告。静默半装的行为包比没装更糟，
    因为它会让用户以为假人是真的。
    """
    report: dict[str, Any] = {
        "ok": False,
        "server_root": None,
        "pack_dirs": [],
        "world_dirs": [],
        "registered": [],
        "experiments": [],
        "needs_restart": False,
        "errors": [],
        "notes": [],
    }

    def log(msg: str) -> None:
        if logger is not None:
            logger.info(msg)

    if not source_dir.is_dir():
        report["errors"].append(f"行为包源目录不存在: {source_dir}")
        return report

    server_root = find_server_root()
    report["server_root"] = str(server_root)

    # -- 1. 找世界目录。找不到就到此为止，不要往外撒文件 --
    world_dirs = find_world_dirs(level_name=level_name, logger=logger)
    report["world_dirs"] = [str(d) for d in world_dirs]

    if not world_dirs:
        report["errors"].append(
            "找不到世界目录（任何含 level.dat 的目录）。"
            "行为包必须登记到具体世界的 world_behavior_packs.json 才会生效。\n"
            "  请确认：\n"
            "    · 服务器已至少启动过一次（世界才会生成）\n"
            "    · 插件运行目录与服务器根目录一致，或存在 bedrock_server/ 子目录\n"
            "  也可以手动指定世界目录后重试。"
        )
        return report

    # -- 2. 释放到服务器根 + 各世界目录 --
    targets: list[Path] = [server_root / "behavior_packs" / PACK_DIR_NAME]
    for wd in world_dirs:
        targets.append(wd / "behavior_packs" / PACK_DIR_NAME)

    seen: set[str] = set()
    for target in targets:
        key = str(target)
        if key in seen:
            continue
        seen.add(key)
        migrated_ok, migrated, migration_msg = _migrate_legacy_pack(target)
        if not migrated_ok:
            report["pack_dirs"].append({"path": str(target), "ok": False,
                                        "msg": migration_msg})
            report["errors"].append(migration_msg)
            continue
        if migrated:
            report["notes"].append(migration_msg)
            report["needs_restart"] = True
        ok, msg = extract_pack(source_dir, target, logger=logger)
        report["pack_dirs"].append({"path": str(target), "ok": ok, "msg": msg})
        if not ok:
            report["errors"].append(f"释放行为包到 {target} 失败: {msg}")
        elif msg == "已释放" or msg.startswith("部分更新"):
            # 磁盘上的脚本被换掉了。BDS 只在**进程启动时**扫描 behavior_packs，
            # 所以这次覆盖要等下次重启才生效。不标记的话，用户会以为
            # "插件已经修好了"，然后继续看到旧脚本的行为。
            report["needs_restart"] = True

    # 释放失败时不再激活不完整的包，也不修改世界实验开关。
    if report["errors"]:
        return report

    # -- 3. 登记到世界 --
    for wd in world_dirs:
        ok, msg = register_pack(wd, logger=logger)
        report["registered"].append({"world": str(wd), "ok": ok, "msg": msg})
        if not ok:
            report["errors"].append(f"登记行为包到 {wd} 失败: {msg}")
        elif msg != "已注册过":
            report["needs_restart"] = True

    # -- 4. 实验开关 --
    if touch_level_dat:
        for wd in world_dirs:
            level_dat = wd / "level.dat"
            changed, msg = enable_experiments(level_dat, backup=True)
            report["experiments"].append(
                {"world": str(wd), "changed": changed, "msg": msg})
            if changed:
                report["needs_restart"] = True
            elif "已启用" in msg:
                pass
            elif "无需修改" in msg:
                pass
            else:
                # 补丁失败是硬错误——不开 Beta APIs，脚本模块根本加载不了
                report["errors"].append(
                    f"修改 {level_dat} 开启 Beta APIs 失败: {msg}\n"
                    f"  请手动在服务器配置里开启，或检查文件权限。"
                )
    else:
        report["notes"].append(
            "已按配置跳过 level.dat 修改。"
            "如果 Beta APIs 未开启，行为包脚本不会加载。")

    # 至少有一个世界登记成功 + 实验开关到位，才算装好
    reg_ok = any(item["ok"] for item in report["registered"])
    exp_ok = (not touch_level_dat) or all(
        ("已启用" in item["msg"]) or item["changed"]
        for item in report["experiments"]
    )
    release_ok = all(item["ok"] for item in report["pack_dirs"]) and bool(report["pack_dirs"])

    report["ok"] = bool(reg_ok and release_ok and exp_ok and not report["errors"])
    return report


def summarize(report: dict[str, Any]) -> list[str]:
    """把安装报告转成适合逐行打日志的文本。"""
    lines: list[str] = []
    lines.append(f"服务器根目录: {report.get('server_root')}")

    for item in report.get("pack_dirs", []):
        mark = "✓" if item["ok"] else "✗"
        lines.append(f"  {mark} 行为包 → {item['path']}  ({item['msg']})")

    for item in report.get("registered", []):
        mark = "✓" if item["ok"] else "✗"
        lines.append(f"  {mark} 登记 {item['world']}  ({item['msg']})")

    for item in report.get("experiments", []):
        lines.append(f"  · {item['world']} Beta APIs: {item['msg']}")

    for note in report.get("notes", []):
        lines.append(f"  · {note}")

    if report.get("errors"):
        lines.append("安装失败：")
        for err in report["errors"]:
            for i, part in enumerate(str(err).split("\n")):
                lines.append(f"  ✗ {part}" if i == 0 else f"    {part}")

    if report.get("needs_restart"):
        lines.append("⚠ 行为包配置有变更，需要重启服务器才会生效")

    return lines
