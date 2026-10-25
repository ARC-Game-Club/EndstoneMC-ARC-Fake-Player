"""Bedrock 小端 NBT 的最小实现。

只做三件事，都是为了让插件能安全地往 ``level.dat`` 里塞一个
``experiments`` 复合标签（打开 Beta APIs 开关）：

1. 读出某个复合标签在原始字节里的绝对偏移
2. 找到某个复合标签的 TAG_END 位置（往前面插字节用）
3. 构造 BYTE / COMPOUND 标签的字节

刻意不做"解析成 dict 再整体序列化"——那样任何一个没覆盖到的标签类型
都会让整个 level.dat 损坏（世界种子、出生点、玩家数据全在里面）。
这里只做定点字节补丁，动几个字节就是几个字节。
"""

from __future__ import annotations

import struct
from typing import Any


# NBT 标签类型
TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12

#: 防御性上限：损坏的文件不应该让我们吃到 OOM 或无限递归
MAX_DEPTH = 256
MAX_LEN = 1_000_000


class NBTReader:
    """小端 NBT 游标。支持跳过 payload 以便记录偏移量。"""

    def __init__(self, data: bytes, base_offset: int = 0, depth: int = 0) -> None:
        self._data = data
        self._pos = base_offset
        self._depth = depth

    @property
    def position(self) -> int:
        return self._pos

    def _read(self, n: int) -> bytes:
        chunk = self._data[self._pos:self._pos + n]
        if len(chunk) < n:
            raise EOFError("NBT 数据意外结束")
        self._pos += n
        return chunk

    def _u8(self) -> int:
        return struct.unpack("<B", self._read(1))[0]

    def _u16(self) -> int:
        return struct.unpack("<H", self._read(2))[0]

    def _i32(self) -> int:
        return struct.unpack("<i", self._read(4))[0]

    def _string(self) -> str:
        return self._read(self._u16()).decode("utf-8", errors="replace")

    def skip_payload(self, tag_type: int) -> None:
        """按类型跳过 payload，不解析内容。"""
        if tag_type in (TAG_BYTE,):
            self._pos += 1
        elif tag_type == TAG_SHORT:
            self._pos += 2
        elif tag_type in (TAG_INT, TAG_FLOAT):
            self._pos += 4
        elif tag_type in (TAG_LONG, TAG_DOUBLE):
            self._pos += 8
        elif tag_type == TAG_BYTE_ARRAY:
            n = self._i32()
            if n < 0 or n > MAX_LEN:
                raise ValueError(f"byte array 长度异常: {n}")
            self._pos += n
        elif tag_type == TAG_STRING:
            # 注意不能写成 `self._pos += self._u16()`：Python 会先读左侧
            # `self._pos` 的旧值，再调用 `_u16()`（而它自己也会推进 _pos），
            # 最后把"旧值 + 长度"写回去，等于把 _u16() 的 2 字节推进吞掉。
            length = self._u16()
            self._pos += length
        elif tag_type == TAG_LIST:
            elem = self._u8()
            count = self._i32()
            if count < 0 or count > MAX_LEN:
                raise ValueError(f"list 长度异常: {count}")
            if count == 0:
                return
            if elem == TAG_END:
                raise ValueError("list 元素类型为 END 但长度非 0，数据损坏")
            self._depth += 1
            if self._depth > MAX_DEPTH:
                raise ValueError("NBT 嵌套过深")
            for _ in range(count):
                self.skip_payload(elem)
            self._depth -= 1
        elif tag_type == TAG_COMPOUND:
            self._depth += 1
            if self._depth > MAX_DEPTH:
                raise ValueError("NBT 嵌套过深")
            while True:
                child = self._u8()
                if child == TAG_END:
                    break
                self._string()          # 字段名，不用
                self.skip_payload(child)
            self._depth -= 1
        elif tag_type == TAG_INT_ARRAY:
            n = self._i32()
            if n < 0 or n > MAX_LEN:
                raise ValueError(f"int array 长度异常: {n}")
            self._pos += n * 4
        elif tag_type == TAG_LONG_ARRAY:
            n = self._i32()
            if n < 0 or n > MAX_LEN:
                raise ValueError(f"long array 长度异常: {n}")
            self._pos += n * 8
        else:
            raise ValueError(f"未知 NBT 标签类型: {tag_type}")

    # -- 读取（仅用于检测，不用于写回） ------------------------------------

    def read_root(self) -> dict[str, Any]:
        tag_type = self._u8()
        if tag_type != TAG_COMPOUND:
            raise ValueError(f"根标签不是 COMPOUND (得到 {tag_type})")
        self._string()
        return self._compound_body()

    def _payload(self, tag_type: int) -> Any:
        if tag_type == TAG_BYTE:
            return struct.unpack("<b", self._read(1))[0]
        if tag_type == TAG_SHORT:
            return struct.unpack("<h", self._read(2))[0]
        if tag_type == TAG_INT:
            return struct.unpack("<i", self._read(4))[0]
        if tag_type == TAG_LONG:
            return struct.unpack("<q", self._read(8))[0]
        if tag_type == TAG_FLOAT:
            return struct.unpack("<f", self._read(4))[0]
        if tag_type == TAG_DOUBLE:
            return struct.unpack("<d", self._read(8))[0]
        if tag_type == TAG_BYTE_ARRAY:
            n = self._i32()
            if n < 0 or n > MAX_LEN:
                raise ValueError(f"byte array 长度异常: {n}")
            return list(struct.unpack(f"<{n}b", self._read(n)))
        if tag_type == TAG_STRING:
            return self._string()
        if tag_type == TAG_LIST:
            elem = self._u8()
            count = self._i32()
            if count < 0 or count > MAX_LEN:
                raise ValueError(f"list 长度异常: {count}")
            if count == 0:
                return []
            if elem == TAG_END:
                raise ValueError("list 元素类型为 END 但长度非 0，数据损坏")
            self._depth += 1
            if self._depth > MAX_DEPTH:
                raise ValueError("NBT 嵌套过深")
            out = [self._payload(elem) for _ in range(count)]
            self._depth -= 1
            return out
        if tag_type == TAG_COMPOUND:
            self._depth += 1
            if self._depth > MAX_DEPTH:
                raise ValueError("NBT 嵌套过深")
            out = self._compound_body()
            self._depth -= 1
            return out
        if tag_type == TAG_INT_ARRAY:
            n = self._i32()
            if n < 0 or n > MAX_LEN:
                raise ValueError(f"int array 长度异常: {n}")
            return list(struct.unpack(f"<{n}i", self._read(n * 4)))
        if tag_type == TAG_LONG_ARRAY:
            n = self._i32()
            if n < 0 or n > MAX_LEN:
                raise ValueError(f"long array 长度异常: {n}")
            return list(struct.unpack(f"<{n}q", self._read(n * 8)))
        raise ValueError(f"未知 NBT 标签类型: {tag_type}")

    def _compound_body(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while True:
            tag_type = self._u8()
            if tag_type == TAG_END:
                break
            name = self._string()
            out[name] = self._payload(tag_type)
        return out


# ---------------------------------------------------------------------------
# 定点补丁辅助
# ---------------------------------------------------------------------------

def root_body_offset(data: bytes, has_header: bool = True) -> int:
    """根 COMPOUND 的 body 起始绝对偏移。level.dat 前 8 字节是头。"""
    reader = NBTReader(data, 8 if has_header else 0)
    reader._u8()          # TAG_COMPOUND
    reader._string()      # 根名
    return reader.position


def find_compound_body(data: bytes, body_offset: int, name: str) -> int | None:
    """在复合标签内找名为 name 的子复合标签的 body 偏移。"""
    reader = NBTReader(data, body_offset)
    while True:
        tag_type = reader._u8()
        if tag_type == TAG_END:
            return None
        field = reader._string()
        pos = reader.position
        if field == name and tag_type == TAG_COMPOUND:
            return pos
        reader.skip_payload(tag_type)


def find_byte_value(data: bytes, body_offset: int, name: str) -> int | None:
    """在复合标签内找名为 name 的 BYTE 字段的值偏移。"""
    reader = NBTReader(data, body_offset)
    while True:
        tag_type = reader._u8()
        if tag_type == TAG_END:
            return None
        field = reader._string()
        pos = reader.position
        if field == name:
            return pos if tag_type == TAG_BYTE else None
        reader.skip_payload(tag_type)


def find_compound_end(data: bytes, body_offset: int) -> int:
    """复合标签 body 的 TAG_END 字节偏移——往前插东西就插这儿。"""
    reader = NBTReader(data, body_offset)
    while True:
        tag_type = reader._u8()
        if tag_type == TAG_END:
            return reader.position - 1
        reader._string()
        reader.skip_payload(tag_type)


def make_byte_tag(name: str, value: int) -> bytes:
    """构造一个 BYTE 字段的完整字节。"""
    enc = name.encode("utf-8")
    return struct.pack("<B", TAG_BYTE) + struct.pack("<H", len(enc)) + enc \
        + struct.pack("<b", value)


def make_compound_tag(name: str, body: bytes) -> bytes:
    """构造一个 COMPOUND 标签的完整字节。"""
    enc = name.encode("utf-8")
    return struct.pack("<B", TAG_COMPOUND) + struct.pack("<H", len(enc)) + enc \
        + body + struct.pack("<B", TAG_END)


def read_bedrock_nbt(data: bytes, has_header: bool = True) -> dict[str, Any]:
    """读一份 Bedrock NBT。仅用于检测状态。"""
    if has_header:
        if len(data) < 8:
            raise ValueError("数据太短")
        data = data[8:]
    return NBTReader(data).read_root()
