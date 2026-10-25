"""假人的合成身份：稳定的 xuid / uuid 注册表。

为什么需要这个
--------------
SimulatedPlayer 没有经过 Xbox Live 认证，引擎给的 xuid 是**空字符串**
——而且是只读的，插件层面塞不进去。弧光核心（arc_core）的
``player_basic_info`` / 经济系统都以 xuid 为主键，空 xuid 意味着
**所有假人挤在同一行数据里**（互相覆盖钱包、共享统计）。

解法是"合成身份"：给每个假人生成一个**看起来像真的、且永远不变**的
xuid/uuid，落盘在 ``identities.json``。arc_core 遇到空 xuid 的玩家时
向本插件查询（``api_resolve_xuid``），拿到合成 xuid 后一切按真人流程
走——这就是"无缝接入"。

合成 xuid 的形状
----------------
16 位纯数字、以 9 开头。真实 XUID 是 16 位十进制（通常 2/3 开头），
9 开头既不撞真人，也方便一眼识别"这是我们的人"。由
SHA-256("arc_fake_player:" + 名字) 派生——同一个名字永远同一个 xuid，
重装/换机器都不变；角色卡也可以显式钉死（见 CharacterCard 扩展）。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid as _uuid
from pathlib import Path
from typing import Any

#: 真实 XUID 常见前缀是 2/3；用 9 标记"弧光合成的"，永不与真人撞车。
XUID_PREFIX = "9"
_XUID_MOD = 10 ** 16

#: 派生 uuid 用的固定命名空间（uuid5），保证同名同 uuid。
_NAMESPACE = _uuid.uuid5(_uuid.NAMESPACE_OID, "arc_fake_player")


def synthetic_xuid(name: str) -> str:
    """名字 → 16 位、9 开头的稳定数字串。"""
    digest = hashlib.sha256(
        ("arc_fake_player:" + str(name).strip()).encode("utf-8")).hexdigest()
    number = int(digest[:16], 16) % (_XUID_MOD // 10)  # 留出首位
    return XUID_PREFIX + f"{number:015d}"


def synthetic_uuid(name: str) -> str:
    """名字 → 稳定的 UUIDv5 字符串。"""
    return str(_uuid.uuid5(_NAMESPACE, str(name).strip()))


def looks_synthetic(xuid: str) -> bool:
    """这个 xuid 是不是我们发出去的合成身份。"""
    text = str(xuid or "")
    return len(text) == 16 and text.isdigit() and text.startswith(XUID_PREFIX)


class IdentityRegistry:
    """名字 → {xuid, uuid}。生成一次、终身不变、落盘持久。"""

    def __init__(self, path: Any, logger: Any = None) -> None:
        self._path = Path(str(path))
        self._logger = logger
        self._data: dict[str, dict[str, str]] = {}
        self._load()

    # -- 持久化 ----------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
        except FileNotFoundError:
            self._data = {}
        except Exception as exc:
            if self._logger:
                try:
                    self._logger.warning(f"身份注册表读取失败，从空开始: {exc}")
                except Exception:
                    pass
            self._data = {}

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8")
            os.replace(tmp, self._path)
        except Exception as exc:
            if self._logger:
                try:
                    self._logger.warning(f"身份注册表保存失败: {exc}")
                except Exception:
                    pass

    # -- 查询 / 登记 ------------------------------------------------------

    def of(self, name: str) -> dict[str, str]:
        """拿某个假人的身份（没有就现场登记一个并落盘）。

        角色卡钉死的 xuid/uuid 也走这里：调用方先 ``pin()`` 再 ``of()``，
        或者直接传 prefer 参数。
        """
        key = str(name).strip()
        entry = self._data.get(key)
        if entry is None:
            entry = {"xuid": synthetic_xuid(key), "uuid": synthetic_uuid(key)}
            self._data[key] = entry
            self.save()
        return {"name": key, **entry}

    def pin(self, name: str, *, xuid: str | None = None,
            uuid: str | None = None) -> dict[str, str]:
        """为某个名字钉死指定身份（角色卡/服主显式指定时用）。

        已有记录时不覆盖（避免手滑改掉已有身份导致数据"失联"），
        除非钉的值与现存值不同且现存值是自动生成的——那也以先到为准，
        返回现存值并在日志里说明。
        """
        key = str(name).strip()
        current = self._data.get(key)
        if current is None:
            # ⚠️ None 要先归一成空串再 or——直接 str(None).strip() 会得到
            # 字符串 "None"（真值！），身份就变成 "None" 了。
            xuid_s = str(xuid).strip() if xuid else ""
            uuid_s = str(uuid).strip() if uuid else ""
            current = {
                "xuid": xuid_s or synthetic_xuid(key),
                "uuid": uuid_s or synthetic_uuid(key),
            }
            self._data[key] = current
            self.save()
        elif self._logger and (xuid and str(xuid).strip() != current["xuid"]):
            try:
                self._logger.warning(
                    f"{key} 已有身份 xuid={current['xuid']}，忽略钉定值 {xuid}"
                    f"（保证已有数据可追溯）")
            except Exception:
                pass
        return {"name": key, **current}

    def xuid_of(self, name: str) -> str:
        return self.of(name)["xuid"]

    def name_of_xuid(self, xuid: str) -> str | None:
        """反查：合成 xuid → 假人名。"""
        want = str(xuid or "").strip()
        for name, entry in self._data.items():
            if entry.get("xuid") == want:
                return name
        return None

    def has(self, name: str) -> bool:
        return str(name).strip() in self._data

    def mark(self, name: str, key: str) -> bool:
        """在身份条目上打一次性标记。返回 True 表示**这是第一次**。

        用途：钱包初始化这类"只做一次"的事——注册表跨重启持久，
        天然就是"这个假人从创世以来有没有做过 X"的账本。
        """
        entry = self.of(name)
        if entry.get(key):
            return False
        # of() 返回的是拷贝，标记要写回原条目
        self._data[str(name).strip()][key] = True
        self.save()
        return True

    def all(self) -> dict[str, dict[str, str]]:
        return dict(self._data)
