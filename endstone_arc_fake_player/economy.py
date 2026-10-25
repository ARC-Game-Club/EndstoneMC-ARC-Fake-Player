"""ARCCore 经济桥：给假人一个真实的钱包。

为什么需要这一层
----------------
"AI 玩家和真人交易"要动**真钱**——玩家收到金币后能拿去商城消费，
这才有意义。本服的经济在 arc_core 的 Economy（SQLite，按 xuid 记账），
所以交易结算必须写进同一张表，而不是插件自己另记一本假账。

SimulatedPlayer 的 xuid 是**空字符串**，直接拿去查表永远查不到。
这里的做法是给每个假人合成一个稳定 key（``fake_<小写名字>``）：
同一个名字每次上线都是同一个钱包，金额跨重启保留。

arc_core 不在时的行为
---------------------
**不崩、不抛**，所有方法返回失败/None，调用方（LLM 工具、命令）
把它翻译成"本服没有经济系统"。假人照样能玩，只是没有钱的概念。
"""

from __future__ import annotations

from typing import Any, Callable

#: 钱包 key 前缀。**旧版**键方案：arc_core 的表按 xuid 记账，这个前缀
#: 保证假人钱包和真人钱包在同一个命名空间里不会撞车（真人 xuid 全是数字）。
#: 3.5 起默认键换成合成 xuid（见 identity.py）；这个前缀只用于
#: 识别并迁移旧钱包。
FAKE_KEY_PREFIX = "fake_"


def wallet_key(name: str) -> str:
    """假人名字 → 旧版稳定经济 key。"""
    return FAKE_KEY_PREFIX + str(name or "").strip().lower()


class EconomyBridge:
    """对 arc_core Economy 的薄封装。所有方法都不会抛异常。"""

    def __init__(self, server: Any, logger: Any) -> None:
        self._server = server
        self._logger = logger
        #: arc_core 偶尔会晚于本插件加载（插件启用顺序），所以每次
        #: 用的时候现查，不做启动期一次性绑定。查到后缓存实例，
        #: 但插件重载时引用会失效——用 try/except 兜住再重查。
        self._cached: Any = None
        #: 钱包键函数（默认旧版 fake_<名字>；插件会换成合成 xuid）。
        self._key_fn: Any = None

    def bind_key_fn(self, fn: Any) -> None:
        """注入"假人名 → 钱包键"的函数（3.5 起默认是合成 xuid）。"""
        self._key_fn = fn

    def _key(self, name: str) -> str:
        if self._key_fn is not None:
            try:
                return str(self._key_fn(name))
            except Exception:
                pass
        return wallet_key(name)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _core(self) -> Any | None:
        if self._cached is not None:
            return self._cached
        try:
            pm = getattr(self._server, "plugin_manager", None)
            core = pm.get_plugin("arc_core") if pm is not None else None
        except Exception:
            core = None
        if core is None:
            return None
        eco = getattr(core, "economy", None)
        if eco is None:
            return None
        self._cached = eco
        return eco

    def _call(self, fn_name: str, *args: Any, default: Any = None) -> Any:
        """安全调用 Economy 的一个方法。失败返回 default，绝不抛。"""
        eco = self._core()
        if eco is None:
            return default
        fn: Callable | None = getattr(eco, fn_name, None)
        if not callable(fn):
            return default
        try:
            return fn(*args)
        except Exception as exc:
            self._logger.debug(f"economy.{fn_name} 失败: {exc}")
            return default

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """本服现在能不能用经济（arc_core 已加载且带 Economy）。"""
        return self._core() is not None

    def get_balance(self, name: str) -> float | None:
        """假人钱包余额。查不到（无经济/没建过钱包）返回 None。"""
        value = self._call("get_player_money_by_xuid", self._key(name))
        if isinstance(value, (int, float)):
            return float(value)
        return None

    def ensure_wallet(self, name: str, initial: float | None = None) -> bool:
        """确保假人有钱包行；`initial` 非空时设为该值（首次初始化用）。

        arc_core 的 get_player_money_by_xuid 是 **get-or-create**：读一次
        就会按服务器默认初始额建行。所以"行存在"和"余额是新是旧"没法
        从这里区分——**首次判断由调用方负责**（身份注册表的一次性标记，
        见 plugin._init_wallet_once），这里只管两件事：
          1. 行存在（get_balance 读一次即建）
          2. initial 给了就写入；写入时若旧键（fake_<名字>）还有余额
             且新行还是默认值，取两者较大——老玩家的钱不能凭空消失。
        """
        eco = self._core()
        if eco is None:
            return False
        current = self.get_balance(name) or 0.0
        if initial is None:
            return True
        amount = float(initial)
        legacy = 0.0
        legacy_key = wallet_key(name)
        if legacy_key != self._key(name):
            old = self._call("get_player_money_by_xuid", legacy_key)
            if isinstance(old, (int, float)) and old > 0:
                legacy = float(old)
        if legacy > current:
            amount = max(amount, legacy)
        return bool(self._call("set_player_money_by_xuid", self._key(name),
                               amount))

    def set_balance(self, name: str, amount: float) -> bool:
        return bool(self._call(
            "set_player_money_by_xuid", wallet_key(name), float(amount)))

    def pay_player(self, bot_name: str, player: Any, amount: float) -> tuple[bool, str]:
        """假人 → 真人转账（交易里假人付钱）。

        玩家侧用**真实 xuid** 收款，这样钱真真切切进了玩家的账户。
        先扣假人（余额不足则拒），再加玩家；两步之间没有事务，
        极端情况（加钱失败）会把差额退回假人钱包，宁可对假人宽容。
        """
        amount = round(float(amount), 2)
        if amount <= 0:
            return False, "金额必须大于 0"
        eco = self._core()
        if eco is None:
            return False, "本服没有经济系统"
        player_xuid = str(getattr(player, "xuid", "") or "").strip()
        if not player_xuid:
            return False, "对方 xuid 为空，无法收款"
        bot_key = self._key(bot_name)
        balance = self.get_balance(bot_name)
        if balance is None or balance < amount:
            return False, f"钱包余额不足（{balance if balance is not None else 0:.2f}）"
        if not self._call("decrease_player_money_by_xuid", bot_key, amount):
            return False, "扣款失败"
        if not self._call("increase_player_money_by_xuid", player_xuid, amount):
            # 回滚。回滚要是也失败（同库两连败，理论上几乎不可能），
            # 钱就少了——这时必须大声喊出来，让服主去数据库里对账，
            # 而不是无声吞掉。
            if not self._call("increase_player_money_by_xuid", bot_key, amount):
                self._logger.warning(
                    f"交易回滚失败！{bot_key} 的 {amount:.2f} 金币可能丢失，"
                    f"请核对 arc_core 经济数据")
            return False, "给玩家加钱失败，已退回"
        return True, f"已支付 {amount:.2f}"

    def receive_from_player(self, bot_name: str, player: Any, amount: float) -> tuple[bool, str]:
        """真人 → 假人转账（玩家付钱买假人的东西）。"""
        amount = round(float(amount), 2)
        if amount <= 0:
            return False, "金额必须大于 0"
        eco = self._core()
        if eco is None:
            return False, "本服没有经济系统"
        player_xuid = str(getattr(player, "xuid", "") or "").strip()
        if not player_xuid:
            return False, "对方 xuid 为空，无法扣款"
        bot_key = self._key(bot_name)
        if not self._call("decrease_player_money_by_xuid", player_xuid, amount):
            return False, "玩家余额不足或扣款失败"
        if not self._call("increase_player_money_by_xuid", bot_key, amount):
            if not self._call("increase_player_money_by_xuid", player_xuid, amount):
                self._logger.warning(
                    f"交易回滚失败！玩家 {player_xuid} 的 {amount:.2f} 金币"
                    f"可能丢失，请核对 arc_core 经济数据")
            return False, "假人收款失败，已退回玩家"
        return True, f"已收到 {amount:.2f}"
