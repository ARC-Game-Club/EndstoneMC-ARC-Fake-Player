"""活动日志——记录"AI 干了什么"。

为什么单独一个文件
------------------
插件本来就有 ``logger``，为什么还要再写一份？

因为两者的**读者不同**。``logger`` 的读者是排障的人：它按级别分类，
混着加载顺序、握手细节、API 异常，一屏几百行。而这份日志的读者是
**想知道这个虚拟玩家今天干了什么**的服主——他要的是流水账，
不是诊断报告。

所以这里刻意只记"结论"：

    记：挖了什么、走到哪、目标变成什么、说了什么、记忆压缩了多少、
        身体什么时候掉线、管理员动过什么
    不记：每格方块、每次寻路重试、每轮提示词、感知快照

一句话能说清的事，就不要写十行。

文件布局
--------
    plugins/<插件名>/config.json     配置
    plugins/<插件名>/activity.log    本模块写的东西
                     activity.log.1  上一份（轮转出来的）
                     activity.log.2  ...

设计约束
--------
1. **绝不因为日志问题让插件挂掉**。目录建不出来、磁盘满了、权限不对——
   全部吞掉记 warning，游戏该跑还是跑。
2. **写不进去不等于写不成了**。文件被占用/权限不足时先把事件攒在内存里，
   等文件恢复可用自动补写，而不是直接丢弃。
3. **线程安全**。Agent 的决策回调在主线程，但上下文压缩的完成回调
   可能从别处进来。
4. **不 import endstone**。这样它能脱离服务器单独测。

跨平台踩过的坑
--------------
**Windows 上文件被独占打开就再也写不进去了。** 用记事本看一眼
activity.log，Windows 会以独占方式锁住它，此时 ``open("a")`` 立刻
``PermissionError``。而 Linux 的 ``rename`` 忽略文件占用，轮转照跑
不误——同一个 bug，两边表现完全不同。

更糟的是原实现把这条事件**直接丢掉**，还把告警限流成"第 1 次和每
第 20 次"，于是现象是"日志莫名其妙不更新了，也没人报错"。现在改成
内存缓冲 + 自动补写 + 恢复通知：

    打不开  → 事件进内存缓冲（上限 ``buffer_bytes``）
    缓冲满  → 丢最旧的，并记下丢了多少
    恢复    → 自动补写全部缓冲，并打一条"已恢复，此前失败 N 次"

Linux 上同样的路径由"文件只读/属主不符/磁盘满/只读挂载/SELinux"
触发——bug 是同一个，只是条件更容易凑齐。
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, TextIO

#: 一条事件里的换行会被换成这个，保证"一条事件一行"
NEWLINE_MARK = " ⏎ "

#: 事件类别 → 显示用的方括号标签。给固定的一组，避免日志里出现
#: 五花八门的标签导致 grep 不出来。
KINDS: dict[str, str] = {
    "spawn": "生成",
    "remove": "移除",
    "stop": "停止",
    "goal": "目标",
    "note": "笔记",
    "say": "发言",
    "dig": "挖矿",
    "build": "建造",
    "move": "移动",
    "collect": "采集",
    "chat": "对话",
    "think": "思考",
    "done": "完成",
    "error": "出错",
    "body": "身体",
    "pack": "行为包",
    "admin": "管理",
    "context": "上下文",
    "system": "系统",
}

#: 单文件字节数的下限。再小就没有轮转的意义了——一条事件本身就上百字节，
#: 设成几十字节只会每条都触发轮转，把日志搅成一团。
MIN_MAX_BYTES = 256

#: 写不进文件时，内存里最多攒多少**字节**的事件等着补写。
#:
#: 默认 256 KB ≈ 数百条事件，足够扛过"用户开了一下记事本"这种短窗口，
#: 又不会让插件吃掉可观内存。超出后丢最旧的——新事件比旧事件更该留。
DEFAULT_BUFFER_BYTES = 256_000


def kind_label(kind: str) -> str:
    """把事件类别翻成中文标签。没登记的类别原样返回。"""
    return KINDS.get(kind, kind or "记录")


class ActivityLog:
    """带大小轮转的追加式日志。所有方法都不抛异常。"""

    #: 每失败多少次打一条告警。
    #:
    #: 首次失败必须立刻报（那是最有信息量的一次：它带完整路径和
    #: errno）。之后限流，否则磁盘满的时候会一秒刷几百行，把真正的
    #: 现场冲掉。恢复时另有一次通知，见 ``_note_recovered``。
    _WARN_EVERY = 20

    def __init__(
        self,
        directory: str | Path,
        *,
        filename: str = "activity.log",
        max_bytes: int = 2_000_000,
        backups: int = 5,
        enabled: bool = True,
        echo: Callable[[str], None] | None = None,
        logger: Any = None,
        buffer_bytes: int = DEFAULT_BUFFER_BYTES,
    ) -> None:
        self.directory = Path(directory)
        self.filename = filename
        self.max_bytes = max(MIN_MAX_BYTES, int(max_bytes))
        self.backups = max(0, int(backups))
        self.enabled = bool(enabled)
        #: 可选的额外输出通道（一般用来同时打到控制台）
        self.echo = echo
        self._logger = logger
        #: 打不开文件时内存里最多攒多少字节等着补写
        self.buffer_bytes = max(0, int(buffer_bytes))

        self._lock = threading.RLock()
        self._fh: TextIO | None = None
        self._writes = 0
        self._bytes = 0
        #: 有多少条事件**没能落盘**。这是用户真正该看的数字——它就是
        #: 日志的缺口。还在缓冲里等补写的也算进去。
        self._failures = 0

        #: 打不开文件/写失败的**尝试**次数。只用来给告警限流。
        #:
        #: 必须和 ``_failures`` 分开：一条事件落盘前会试好几次（先补写
        #: 积压、再开文件、可能还要轮转）。混在一起的话 30 条日志会报
        #: "失败 59 次"，数字虚高且限流节奏失真。
        self._attempts = 0

        #: 写不进去、排队等着补写的事件。``(整行文本, 字节数)``
        self._pending: Deque[tuple[str, int]] = deque()
        self._pending_bytes = 0
        #: 因为缓冲满了而**真的丢了**多少条。非零就该让用户知道。
        self._dropped = 0
        #: 上一次失败的错误摘要（类型 + 消息），告警和 /ai log 都用得上
        self._last_error = ""
        #: 当前是否处在"写不进去"的状态。用于恢复时只通知一次。
        self._broken = False

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self.directory / self.filename

    @property
    def total_written(self) -> int:
        """本次运行**成功落盘**了多少条。

        还在缓冲里等补写的**不算**——它们随时可能丢，混进来会让
        ``/ai log`` 报出一个比磁盘上实际更多的数字。
        """
        return self._writes

    @property
    def failures(self) -> int:
        """有多少条事件没能落盘（还在缓冲里等补写的也算）。

        这就是日志的缺口大小。它跟 ``dropped`` 是包含关系：
        ``failures`` 里有一部分还在缓冲（有机会补回来），``dropped``
        是其中**永久丢掉了**的那部分。
        """
        return self._failures

    @property
    def pending(self) -> int:
        """正躺在内存里等补写的事件数。持续非零就是写不进文件。"""
        return len(self._pending)

    @property
    def dropped(self) -> int:
        """缓冲满而被丢弃的事件数。**这个数一旦非零就是真丢数据了。**"""
        return self._dropped

    @property
    def last_error(self) -> str:
        """最近一次写入失败的错误摘要。"""
        return self._last_error

    @property
    def backups_on_disk(self) -> list[Path]:
        """当前实际存在的轮转文件，从新到旧。"""
        out: list[Path] = []
        for i in range(1, self.backups + 1):
            p = self.path.with_name(f"{self.filename}.{i}")
            if p.exists():
                out.append(p)
        return out

    def size(self) -> int:
        """当前主日志文件的字节数（拿不到就返回 0）。"""
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def write(self, kind: str, message: str, *, who: str = "") -> None:
        """记一条。**永远不会抛异常**——日志不该拖垮游戏。

        写不进文件时**不丢弃**：事件进内存缓冲，等文件恢复可用自动补写。
        """
        if not self.enabled:
            return
        line, nbytes = self._render(kind, message, who)

        with self._lock:
            # 先补写积压的，再写这条——顺序才是事件真实的先后。
            # 反过来的话，新事件会插到旧事件前面，读起来时间线是乱的。
            self._drain_pending()
            self._emit(line, nbytes)

        # echo 放在锁外面：它可能又去写控制台，不该占着日志锁
        if self.echo is not None:
            try:
                self.echo(line.rstrip("\n"))
            except Exception:
                pass

    def _render(self, kind: str, message: str, who: str) -> tuple[str, int]:
        """算出整行文本**和它的字节长度**。

        字节长度必须一起算出来：轮转上限是按字节定的（``max_bytes``
        对的是 ``stat().st_size``）。早期版本拿字符数去比字节上限，
        中文事件每字 3 字节，于是日志在离上限还很远的时候就被切走。
        """
        try:
            line = self._format(kind, message, who)
        except Exception:
            # 连格式化都失败（消息里放了奇怪的对象），退到最朴素的形式
            line = f"[?] {kind}: {message!r}\n"
        return line, self._byte_len(line)

    @staticmethod
    def _byte_len(text: str) -> int:
        try:
            return len(text.encode("utf-8", "replace"))
        except Exception:
            # 理论上到不了这儿（replace 兜底），但长度算不出来时
            # 宁可估大一点触发轮转，也不能估小了让文件无限涨。
            return len(text) * 3 + 1

    def _format(self, kind: str, message: str, who: str) -> str:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = str(message).replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\n", NEWLINE_MARK)
        prefix = f"[{stamp}] [{kind_label(kind)}]"
        if who:
            return f"{prefix} {who} {text}\n"
        return f"{prefix} {text}\n"

    def _emit(self, line: str, nbytes: int) -> bool:
        """把一行写进文件。成功返回 True。

        失败不抛异常、不丢数据——进缓冲等下次补写。
        """
        if not self._make_ready(nbytes):
            self._stash(line, nbytes)
            return False
        assert self._fh is not None
        try:
            self._fh.write(line)
            self._fh.flush()          # 崩溃时也要能读到出事前发生了什么
        except Exception as exc:
            # 句柄可能已经坏了（比如磁盘满后 ext4 报 EIO）。丢掉它，
            # 下次重新打开——死抱着一个坏句柄只会每条都失败。
            self._discard_handle()
            self._note_trouble("活动日志写入失败", exc)
            self._stash(line, nbytes)
            return False
        self._writes += 1
        self._bytes += nbytes
        return True

    def _stash(self, line: str, nbytes: int) -> None:
        """写不进去时排队等着补写。缓冲满了丢最旧的。"""
        self._pending.append((line, nbytes))
        self._pending_bytes += nbytes
        # 这一条没落盘，日志缺口 +1
        self._failures += 1
        while self._pending_bytes > self.buffer_bytes and len(self._pending) > 1:
            _, old = self._pending.popleft()
            self._pending_bytes -= old
            self._dropped += 1

    def _drain_pending(self) -> int:
        """文件可用了就把积压的补写出去。返回补写了几条。"""
        if not self._pending:
            return 0
        # 先探一次：文件根本打不开就别白跑一趟循环。
        if not self._ensure_open():
            return 0
        n = 0
        while self._pending:
            line, nbytes = self._pending[0]
            if not self._make_ready(nbytes):
                break
            assert self._fh is not None
            try:
                self._fh.write(line)
                self._fh.flush()
            except Exception as exc:
                self._discard_handle()
                self._note_trouble("活动日志补写失败", exc)
                break
            self._pending.popleft()
            self._pending_bytes -= nbytes
            self._writes += 1
            self._bytes += nbytes
            n += 1
        if n:
            self._note_recovered()
        return n

    def _note_trouble(self, what: str, exc: BaseException | None = None) -> None:
        """记一次"出问题了"的**尝试**，并按限流规则告警。

        注意它**不**动 ``_failures``——那是"多少条事件没落盘"，由
        ``_stash`` 在事件确实没能写出去时计数。两者混在一起的话，
        一条事件内部的三四次重试会被算成四次失败，诊断时完全是误导。
        """
        self._attempts += 1
        self._broken = True
        if exc is not None:
            self._last_error = f"{type(exc).__name__}: {exc}"
        # 首次立刻报（带完整现场），之后每 N 次一次。
        if self._attempts == 1 or self._attempts % self._WARN_EVERY == 0:
            self._warn(f"{what}：{self._detail()}")

    def _note_recovered(self) -> None:
        """从"写不进去"恢复到"写得进去"时通知一次。

        为什么必须有这条
        --------------
        限流之后，失败告警在用户眼里就是"偶尔蹦一行 Permission denied"，
        很容易被当噪音忽略。真正让人后怕的是**它已经坏了很久而没人
        知道**。所以恢复时必须回头报一句：断了多久、期间丢了几条。
        没有这条，Linux 上那种"磁盘满了两小时"的故障就永远隐形。
        """
        if not self._broken:
            return
        self._broken = False
        msg = f"活动日志已恢复（期间有 {self._failures} 条没能落盘"
        if self._dropped:
            msg += f"，其中 {self._dropped} 条因缓冲满被丢弃"
        if self._pending:
            msg += f"，仍有 {len(self._pending)} 条待补写"
        if self._last_error:
            msg += f"；最后错误 {self._last_error}"
        self._warn(msg + "）")

    def _detail(self) -> str:
        """失败现场：最后错误 + 缺口多大 + 缓冲积压。"""
        parts: list[str] = []
        if self._last_error:
            parts.append(self._last_error)
        parts.append(f"尝试 {self._attempts} 次未成功")
        if self._failures:
            parts.append(f"{self._failures} 条未落盘")
        if self._pending:
            parts.append(f"{len(self._pending)} 条待补写")
        if self._dropped:
            parts.append(f"已丢弃 {self._dropped} 条")
        return "，".join(parts)

    def _warn(self, msg: str) -> None:
        if self._logger is None:
            return
        try:
            self._logger.warning(msg)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 文件管理
    # ------------------------------------------------------------------

    def _ensure_open(self) -> bool:
        """确保主日志文件可写。成功返回 True。

        ``open("a")`` 失败在 Windows 上极常见：任何程序独占打开过这个
        文件（记事本、编辑器、日志查看器、杀毒软件扫描）都会锁住它。
        所以失败**不是**"这个日志完了"，只是"这次打不开"——句柄留空，
        下次 write 会再试一次，缓冲里的内容等着补写。
        """
        if self._fh is not None:
            return True
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._note_trouble(
                f"活动日志目录创建失败（{self.directory}）", exc)
            return False
        try:
            self._fh = self.path.open("a", encoding="utf-8")
        except Exception as exc:
            self._fh = None
            self._note_trouble(f"活动日志打不开（{self.path}）", exc)
            return False
        return True

    def _make_ready(self, incoming: int) -> bool:
        """准备好一个能接收 ``incoming`` 字节的句柄。

        返回 False 表示**现在不能写**，调用方据此把事件送去缓冲。
        这里绝不留下"句柄是 None 但代码以为可用"的状态——早期版本
        正是在 ``_rotate`` 之后没复查句柄，直接
        ``AttributeError: 'NoneType' object has no attribute 'write'``。
        """
        if not self._ensure_open():
            return False
        if not self._rotate_if_needed(incoming):
            return False
        # 双重保险：轮转内部可能换过句柄，这里以实际状态为准。
        return self._fh is not None

    def _rotate_if_needed(self, incoming: int) -> bool:
        """加上这 ``incoming`` **字节**会超限就轮转。

        返回 False 表示"现在不能写"（句柄不可用）。
        """
        if self._fh is None:
            return False
        try:
            current = self.path.stat().st_size
        except OSError:
            current = self._bytes
        if current + incoming <= self.max_bytes:
            return True
        if current == 0:
            # 空文件也塞不下这一条：没法再轮转了，否则会死循环。
            # 让它超一点，总比把这条丢掉强。
            return True
        return self._rotate()

    def _rotate(self) -> bool:
        """activity.log → activity.log.1 → .2 → …，最老的丢掉。

        两条铁律
        --------
        1. **不管轮转成功还是失败，主文件最后必须回到可写状态。**
           轮转失败不该让日志彻底停摆——那正是本模块最该避免的事。
        2. **单个文件挪不动不许中断整个轮转。** Windows 上一个被
           打开的 ``activity.log.5`` 删不掉是常事，为此放弃轮转意味着
           主文件无限增长。早期的 ``unlink`` 裸奔会让整个流程抛出去，
           留下一堆半挪动的文件就收场了。
        """
        # 先断开句柄：下面要重命名/截断这个文件。
        self._discard_handle()
        renamed = True
        try:
            if self.backups <= 0:
                # 不留备份：直接清空主文件
                with self.path.open("w", encoding="utf-8"):
                    pass
            else:
                oldest = self.path.with_name(f"{self.filename}.{self.backups}")
                # 从后往前挪，避免覆盖
                try:
                    if oldest.exists():
                        oldest.unlink()
                except OSError as exc:
                    # 删不掉就留着当第 N+1 份，不影响继续轮转
                    self._note_soft_error(exc)
                for i in range(self.backups - 1, 0, -1):
                    src = self.path.with_name(f"{self.filename}.{i}")
                    dst = self.path.with_name(f"{self.filename}.{i + 1}")
                    try:
                        if src.exists():
                            src.replace(dst)
                    except OSError as exc:
                        self._note_soft_error(exc)
                try:
                    self.path.replace(
                        self.path.with_name(f"{self.filename}.1"))
                except OSError as exc:
                    # 主文件挪不动：最常见是它正被别的程序锁着。
                    # 主文件还在、还能写，就让它暂时超限——比停摆强。
                    renamed = False
                    self._note_soft_error(exc)
        except Exception as exc:
            renamed = False
            self._note_trouble("活动日志轮转失败", exc)

        # 铁律 1：不管上面发生了什么，主文件必须回到可写状态。
        if renamed and not self.path.exists():
            # 主文件被挪走了却没留下新的（比如 replace 部分成功）。
            # 补一个空文件，否则后面每条日志都无处可去。
            try:
                self.path.touch()
            except OSError as exc:
                self._note_soft_error(exc)
        self._ensure_open()
        return self._fh is not None

    def _note_soft_error(self, exc: BaseException) -> None:
        """轮转过程中的可容忍错误：记下来，不告警刷屏。"""
        self._last_error = f"{type(exc).__name__}: {exc}"

    def clear(self) -> int:
        """删掉主文件和所有轮转份。返回删了几个文件。

        同时清掉内存缓冲——用户说"清空"，就是真的全清，包括还没
        落盘的那些。
        """
        with self._lock:
            self._discard_handle()
            n = 0
            for p in [self.path, *self.backups_on_disk]:
                try:
                    if p.exists():
                        p.unlink()
                        n += 1
                except Exception as exc:
                    self._note_trouble(f"删除 {p} 失败", exc)
            self._bytes = 0
            self._pending.clear()
            self._pending_bytes = 0
            return n

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def read_tail(self, count: int = 15, *, limit: int = 50) -> list[str]:
        """读最后 count 条（去掉了行尾换行）。

        从文件尾部倒着读，不把整个文件load进内存——日志可能很大。
        """
        count = max(1, min(int(limit), int(count)))
        try:
            with self.path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                block = 4096
                buf = b""
                pos = size
                while pos > 0 and buf.count(b"\n") <= count:
                    step = min(block, pos)
                    pos -= step
                    fh.seek(pos)
                    buf = fh.read(step) + buf
            text = buf.decode("utf-8", "replace")
        except Exception as exc:
            self._note_trouble("读取活动日志失败", exc)
            return []

        lines = [ln for ln in text.splitlines() if ln.strip()]
        return lines[-count:]

    def describe(self) -> str:
        """给 /ai log 用的一行状态。"""
        state = "开" if self.enabled else "关"
        parts = [
            f"活动日志: {state}",
            f"{self.size()} 字节",
        ]
        if self.backups:
            parts.append(f"轮转 {len(self.backups_on_disk)}/{self.backups} 份")
        else:
            parts.append("不轮转")
        parts.append(f"本次已写 {self._writes} 条")
        # 写不进去的时候必须一眼看出来，不然 /ai log 就白问了
        if self._broken:
            parts.append(f"当前写不进（{self._attempts} 次尝试）")
        if self._pending:
            parts.append(f"待补写 {len(self._pending)} 条")
        if self._failures:
            parts.append(f"未落盘 {self._failures} 条")
        if self._dropped:
            parts.append(f"已丢弃 {self._dropped} 条")
        if self._broken and self._last_error:
            parts.append(self._last_error)
        return " · ".join(parts)

    # ------------------------------------------------------------------
    # 句柄与收尾
    # ------------------------------------------------------------------

    def _discard_handle(self) -> None:
        """关掉句柄并置空。不抛异常。"""
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass

    def flush(self) -> None:
        """把缓冲里的事件尽量落盘，再 flush 句柄。"""
        with self._lock:
            self._drain_pending()
            if self._fh is not None:
                try:
                    self._fh.flush()
                except Exception:
                    pass

    def close(self) -> None:
        """关掉句柄。**内存缓冲不丢**——下次 open 同一路径会补写。

        这也是为什么 ``__exit__`` 不该清缓冲：正常关闭插件时缓冲
        通常是空的，但如果是"日志打不开"状态下关的，留着才能在下次
        启动补回来。
        """
        with self._lock:
            self._discard_handle()

    def __enter__(self) -> ActivityLog:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 事件渲染：把内部数据结构变成一句人话
# ---------------------------------------------------------------------------
#
# 单独抽出来是为了能单测。渲染逻辑很琐碎（这些字段有时有、有时没有），
# 但它是"日志好不好读"的全部所在。

def render_plan_result(label: str, ok: bool, done: int, total: int,
                       failed: int = 0) -> tuple[str, str]:
    """一个执行计划结算了。返回 (类别, 文字)。

    标签形如 "挖区域(9格)"、"走过去"、"放置方块"。中文标签直接当类别用，
    英文的走 KINDS 映射——这样 /ai log 里一眼能看出在干什么。
    """
    mark = "✓" if ok else "✗"
    text = f"{mark} {label} {done}/{total} 完成"
    if failed:
        text += f"，{failed} 处失败"

    # 尽量归类，认不出来就算"思考"
    kind = "think"
    head = label.split("(")[0]
    for key, cn in (("挖", "dig"), ("掘", "dig"), ("采集", "collect"),
                    ("放置", "build"), ("建造", "build"), ("搭", "build"),
                    ("走", "move"), ("移动", "move"), ("前往", "move")):
        if key in head:
            kind = cn
            break
    return kind, text


def render_action(op: str, detail: str = "") -> tuple[str, str]:
    """一次工具调用的即时结果（没有产生计划的那种）。"""
    text = f"{op}" + (f"：{detail}" if detail else "")
    return "think", text