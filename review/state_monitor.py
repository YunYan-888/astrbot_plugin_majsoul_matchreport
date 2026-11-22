"""对局监控：登录 1~3 个雀魂账号 → 轮询观战列表发现对局 → 结束后拉牌谱。

数据来源：
- 账号是否在对局中：`.lq.Lobby.fetchAccountState`（批量查询，不需要是好友）。
- 对局 uuid：状态里的 `playing.game_uuid`，与牌谱 uuid 通用。
- 对局详情/顺位：`.lq.Lobby.fetchGameRecord`（必须带 client_version_string，否则 151）。
- 昵称解析：数字直接当 account_id；否则用牌谱屋 search_player 解析（只解析一次，不做轮询）。

结束判定：好友的局看好友列表里这局的「观战」还能不能点进去（`playing.game_uuid` 还在
不在）；观战入口没了，或者房间列表里发现他已经开了新的一局，就是这局结束，直接查一次牌谱。
非好友的局等它掉出观战列表，再按 T2 轮询牌谱。观战列表每个筛选只有 100 局、对局中途会被
挤出，所以它只是发现对局和兜底用的，不能凭它判断好友的局结束。

多账号：账号池里所有「启用」的账号都会同时登录（不设上限）；查牌谱时优先用
「这局的玩家是它好友」的账号，都不是好友就在有牌谱权限的账号里挑今天消耗次数最少的。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from astrbot.api import logger

from .account_ids import (
    decode_account_id,
    encode_account_id,
    ids_are_majsoul,
    set_ids_majsoul,
    to_account_id,
    to_majsoul,
)
from .constants import URL_BASE
from .majsoul_connection import (
    THREE_PLAYER_MODE_IDS,
    MajsoulConnection,
    create_connection,
)
from .utils import HTTPX_CLIENT

DEFAULT_DAILY_LIMIT = 200
DEFAULT_POLL_INTERVAL = 120
LOGIN_CHECK_INTERVAL = 60
SEEN_RETAIN_SECONDS = 3 * 86400
AMAE_SEARCH_URLS = [
    "https://ak-data-1.sapk.ch/api/v2/{mode}/search_player/{name}",
    "https://1.data.amae-koromo.com/api/v2/{mode}/search_player/{name}",
    "https://4.data.amae-koromo.com/api/v2/{mode}/search_player/{name}",
    "https://5-data.amae-koromo.com/api/v2/{mode}/search_player/{name}",
]
# 牌谱屋只收录金之间以上对局；四麻和三麻分开索引，两个都要查
AMAE_SEARCH_MODES = ("pl4", "pl3")

# 「牌谱权限」按天重置，边界是每天 05:01
PERM_RESET_HOUR = 5
PERM_RESET_MINUTE = 1


def _perm_day_key(ts: float) -> str:
    """返回某个时间点所属的结算日（以 05:01 为界）。"""
    dt = datetime.fromtimestamp(ts) - timedelta(hours=PERM_RESET_HOUR, minutes=PERM_RESET_MINUTE)
    return dt.date().isoformat()

# 观战列表 filter_id = 200 + mode_id。
# LIVE_MODE_IDS 是全部合法段位场（含铜/银），用来判定「是不是段位场」、显示房间名；
# 实际轮询只用 SCAN_MODE_IDS：雀魂观战列表里铜/银房间基本没有可拉取的对局，
# 牌谱屋也只收录金之间以上，扫这两个段位场只会每轮白白多发 8 个请求。
LIVE_MODE_IDS = (2, 3, 5, 6, 8, 9, 11, 12, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26)
# 金/玉/王座 × 东/南 × 四麻/三麻，共 12 个
SCAN_MODE_IDS = (8, 9, 11, 12, 15, 16, 21, 22, 23, 24, 25, 26)
# 东场：四人东 2/5/8/11/15，三人东 17/19/21/23/25
EAST_MODE_IDS = {2, 5, 8, 11, 15, 17, 19, 21, 23, 25}

LIVE_MODE_NAMES = {
    2: "铜之间·四人东", 3: "铜之间·四人南",
    5: "银之间·四人东", 6: "银之间·四人南",
    8: "金之间·四人东", 9: "金之间·四人南",
    11: "玉之间·四人东", 12: "玉之间·四人南",
    15: "王座间·四人东", 16: "王座间·四人南",
    17: "铜之间·三人东", 18: "铜之间·三人南",
    19: "银之间·三人东", 20: "银之间·三人南",
    21: "金之间·三人东", 22: "金之间·三人南",
    23: "玉之间·三人东", 24: "玉之间·三人南",
    25: "王座间·三人东", 26: "王座间·三人南",
}
# 观战接口每个筛选最多返回 100 局；靠轮询缓存积累「较早」的对局，缓解只能看到一部分的问题
LIVE_CACHE_RETAIN = 2 * 3600
LIVE_CACHE_MAX = 8000
LIVE_LIST_MAX = 1500

# ── 轮询节奏与对局结束判定（省查询次数）──────────────────
# 三个时间：
#   SCAN_INTERVAL（固定 30s）：扫描一遍所有房间的当前对局，发现监控玩家开局就记下 uuid；
#   T1 = friend_poll_interval（默认 30s）：好友轮询间隔，刷新好友列表看这局观战还能不能进；
#   T2 = poll_interval（默认 120s）：非好友轮询间隔，非好友的局只能等观战列表里没有它，
#      之后每 T2 查一次牌谱（每次查询计一次数）。
# 观战列表每个筛选最多 100 局，对局打到一半就可能被挤出列表，所以「掉出观战列表」不等于
# 「对局结束」。对好友来说真正的结束信号只有一个：好友列表里这局的观战入口没了
# （playing.game_uuid 变了或为空），也就是「观战进不去了」。房间列表每 30s 扫一遍，
# 好友新开一局也会立刻体现为 uuid 变化，同样算上一局结束。
SCAN_INTERVAL = 30             # 固定 30s 扫一遍观战列表（发现对局用）
FRIEND_POLL_INTERVAL_DEFAULT = 30  # 好友轮询间隔 T1 默认值
APPLY_CLAIM_SECONDS = 600      # 同一份好友申请只让一个账号同意：占位保留多久
POLL_TIMEOUT = 120             # 单轮扫描超时
LOGIN_CONCURRENCY = 5          # 账号池同时登录时的并发上限（每个账号握手约 10s，串行会排很久）
STATE_RPC_TIMEOUT = 8          # 批量查账号状态的超时
END_SETTLE_SECONDS = 4         # 确认结束后等几秒让牌谱落库，保证第一次就查到（只查 1 次）
LIVE_ABSENT_GRACE = 600        # 好友列表查不到（非好友/好友被删）时，观战列表消失多久才算结束
MANUAL_ABSENT_GRACE = 120      # 手动查询对局（用户正在等结果）用更短的兜底等待
PENDING_MAX_PLAYING = 3 * 3600 # 待查对局最多算「还在打」3 小时，防止状态一直不刷新卡死
PENDING_TIMEOUT_SECONDS = 2 * 3600  # 开局算起 2 小时还没拉到牌谱就放弃（不再继续花查询次数）
QUERY_FAST_RETRY_DELAYS = (20, 45, 90, 180)   # 好友分支牌谱没拉到时的重试间隔（逐次拉长）
QUERY_FAST_RETRY_MAX = 5       # 快速重试次数上限，超过后按 QUERY_SLOW_RETRY_DELAY 慢速重试
QUERY_SLOW_RETRY_DELAY = 600   # 慢速重试间隔：10 分钟一次，不再每轮都花查询次数
MANUAL_RESULT_KEEP = 200       # 手动查询结果暂存上限（页面轮询读取，不额外消耗次数）
RESULT_WATCH_RETAIN = 2 * 3600 # 页面等结果的有效期：超过 2 小时不再为它保留牌谱
PAIPU_RETAIN_DAYS = 30         # 本地牌谱缓存（raw json / 役满牌面图）保留天数，超过自动清理

GameCallback = Callable[[Any, Dict[str, Any]], Awaitable[None]]


def _today() -> str:
    """牌谱计数/额度使用的「一天」：以每天 05:01 为界，和牌谱权限重置一致。"""
    return _perm_day_key(time.time())


# 旧版本（好友监控）和本版本创建的监控任务名；热重载后旧任务可能还活着
STALE_TASK_NAMES = {
    "majsoul-account-monitor",
    "majsoul-friend-monitor",
    "majsoul-login-check",
}


def _monitor_registry() -> set:
    """挂在事件循环上的监控实例注册表，模块热重载后仍然存在。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return set()
    reg = getattr(loop, "_majsoul_matchreport_registry", None)
    if reg is None:
        reg = set()
        loop._majsoul_matchreport_registry = reg
    return reg


def _monitor_owner_of_task(task) -> Any:
    """从旧任务的协程栈里找出它所属的 AccountGameMonitor（跨模块重载也能拿回来）。"""
    try:
        coro = task.get_coro()
        frame = getattr(coro, "cr_frame", None) or getattr(coro, "gi_frame", None)
        if frame is None:
            return None
        obj = frame.f_locals.get("self")
        if obj is not None and hasattr(obj, "conn") and hasattr(obj, "_stop"):
            return obj
    except Exception:
        pass
    return None


def _stop_stale_monitor(monitor: Any) -> None:
    try:
        monitor._stop.set()
    except Exception:
        pass
    try:
        hb = getattr(monitor, "_heartbeat_task", None)
        if hb is not None and not hb.done():
            hb.cancel()
        monitor._heartbeat_task = None
    except Exception:
        pass
    try:
        conn = getattr(monitor, "conn", None)
        if conn is not None:
            monitor.conn = None
            asyncio.create_task(_safe_close(conn))
    except Exception:
        pass


def _cancel_stale_monitor_tasks() -> int:
    """取消当前事件循环里残留的旧监控任务（跨模块热重载也能清掉）。"""
    cancelled = 0
    # 先停掉旧实例：它们可能还握着登录连接/心跳任务，会和新的监控互相顶号
    for monitor in list(_monitor_registry()):
        _stop_stale_monitor(monitor)
    _monitor_registry().clear()

    try:
        tasks = asyncio.all_tasks()
    except RuntimeError:
        return 0
    for task in tasks:
        if task.done():
            continue
        name = task.get_name()
        if name in STALE_TASK_NAMES or name.startswith("majsoul-finalize-"):
            # 旧模块创建的实例不在注册表里，从协程栈里把它找出来关掉
            monitor = _monitor_owner_of_task(task)
            if monitor is not None:
                _stop_stale_monitor(monitor)
            task.cancel()
            cancelled += 1
    return cancelled


async def _safe_close(conn: Any) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(conn.close(), timeout=5)


class NoAccountAvailable(RuntimeError):
    """账号池里没有可用的账号。"""

    """账号池里没有可用账号（没有启用账号，或全部账号今天无牌谱权限）。"""


class DailyQuota:
    """按账号记录每天查询过的牌谱条数（默认 200 条/账号/天）。"""

    def __init__(self, path: Path, limit: int = DEFAULT_DAILY_LIMIT):
        self.path = path
        self.limit = max(1, int(limit))
        # 某个账号当天额度刚好用完时回调一次（uid），供监控写日志用
        self.on_exhausted: Optional[Callable[[str], None]] = None

    def _load(self) -> Dict[str, Dict[str, int]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8-sig") or "{}")
        except Exception:
            return {}

    def _save(self, data: Dict[str, Dict[str, int]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 保存牌谱配额失败: {exc}")

    def used(self, uid: str) -> int:
        return int(self._load().get(_today(), {}).get(str(uid), 0))

    def remaining(self, uid: str) -> int:
        return max(0, self.limit - self.used(uid))

    def consume(self, uid: str, count: int = 1) -> None:
        data = self._load()
        day = data.setdefault(_today(), {})
        before = int(day.get(str(uid), 0))
        day[str(uid)] = before + count
        self._prune(data)
        self._save(data)
        # 刚好把当天额度用光时通知一次；之后同一天继续调用不会重复触发
        if self.on_exhausted is not None and before < self.limit <= before + count:
            try:
                self.on_exhausted(str(uid))
            except Exception:
                pass

    def _prune(self, data: Dict[str, Dict[str, int]]) -> None:
        for key in list(data.keys()):
            if key != _today() and key < _today():
                data.pop(key, None)


class AccountSession:
    """一个同时在线账号的连接 + 它的好友列表快照。"""

    __slots__ = (
        "uid",
        "account",
        "nickname",
        "conn",
        "friends",
        "friends_at",
        "friend_error_logged",
        "fail_count",
        "next_try_at",
        "last_error",
    )

    def __init__(self, uid: str, account: Optional[Dict[str, Any]] = None):
        self.uid = str(uid or "")
        self.account: Dict[str, Any] = dict(account or {})
        self.nickname = str(self.account.get("nickname") or "")
        self.conn: Any = None
        # 这个账号的好友列表 {account_id: {nickname, online, game_uuid, mode_id}}
        self.friends: Dict[int, Dict[str, Any]] = {}
        self.friends_at = 0.0
        self.friend_error_logged = False
        self.fail_count = 0
        self.next_try_at = 0.0
        self.last_error = ""

    def is_online(self) -> bool:
        conn = self.conn
        if conn is None:
            return False
        ws = getattr(conn, "_ws", None)
        if ws is None:
            return False
        closed = getattr(ws, "closed", None)
        return True if closed is None else not closed

    @property
    def label(self) -> str:
        # 日志里显示雀魂内看到的数字 ID（和 account_id 不是一个数）
        show = self.uid
        try:
            show = str(encode_account_id(int(self.uid)))
        except (TypeError, ValueError):
            pass
        return f"{self.nickname}({show})" if self.nickname else (show or "未知账号")

    def friend_ids(self) -> set:
        return {int(aid) for aid in self.friends}


class AccountGameMonitor:
    """每个启用的账号各开一条登录连接，轮询监控列表里账号的对局状态，结束后回调。"""

    def __init__(
        self,
        data_dir: Path,
        account_store_path: Path,
        get_targets: Callable[[], List[Dict[str, Any]]],
        on_game_finished: GameCallback,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        friend_poll_interval: int = FRIEND_POLL_INTERVAL_DEFAULT,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        nonfriend_daily_limit: int = 2,
        monitor_three_player: bool = True,
        monitor_east: bool = True,
        auto_approve_friends: bool = True,
        log_event: Optional[Callable[..., None]] = None,
        search_urls: Optional[List[str]] = None,
        server_url: str = "",
    ):
        self.data_dir = Path(data_dir)
        self.account_store_path = Path(account_store_path)
        self.get_targets = get_targets
        self.on_game_finished = on_game_finished
        # T2：非好友轮询间隔（非好友的局只能靠观战列表消失 + 每 T2 查一次牌谱）
        self.poll_interval = max(10, int(poll_interval))
        # T1：好友轮询间隔（好友的局靠好友列表里观战还能不能点进去判断结束）
        self.friend_poll_interval = max(10, int(friend_poll_interval))
        self.scan_interval = SCAN_INTERVAL
        # 非好友每日播报上限：-1 表示无限制
        self.nonfriend_daily_limit = int(nonfriend_daily_limit)
        self.monitor_three_player = bool(monitor_three_player)
        self.monitor_east = bool(monitor_east)
        self.auto_approve_friends = bool(auto_approve_friends)
        # 只轮询金之间以上、当前设置允许的场风；关掉三麻/东场后不再请求对应观战列表
        self._active_live_mode_ids = tuple(
            mode_id
            for mode_id in SCAN_MODE_IDS
            if (self.monitor_three_player or mode_id not in THREE_PLAYER_MODE_IDS)
            and (self.monitor_east or mode_id not in EAST_MODE_IDS)
        )
        self.log_event = log_event
        self.server_url = str(server_url or "").strip() or URL_BASE
        self.search_urls = [u for u in (search_urls or []) if u] or list(AMAE_SEARCH_URLS)
        self.quota = DailyQuota(self.data_dir / "monitor_quota.json", daily_limit)
        self.quota.on_exhausted = self._on_quota_exhausted
        self.seen_path = self.data_dir / "monitor_seen.json"
        self.pending_path = self.data_dir / "monitor_pending.json"
        self.broadcast_path = self.data_dir / "monitor_broadcast.json"
        # 每个玩家今天已经播报了几次（非好友限播用）：{day: {account_id: 次数}}
        self._broadcast: Dict[str, Dict[str, int]] = self._load_broadcast_counts()

        self.conn: Optional[MajsoulConnection] = None
        # 所有同时在线的账号：{uid: AccountSession}（含主账号）
        self._sessions: Dict[str, AccountSession] = {}
        self._task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._conn_lock = asyncio.Lock()
        self._reconnecting = False
        self._last_poll_at = 0.0
        self._no_targets_logged = False
        self._account_changed = asyncio.Event()
        self._poll_wake = asyncio.Event()
        self._waiting_account = False
        self.last_paipu_cache_hit = False
        self.last_paipu_error = ""
        self.last_recent_error = ""
        # 每天第一次扫描时清理一次超过一个月的本地牌谱
        self._paipu_cleanup_day = ""

        raw_pending = self._load_pending()
        self._pending: Dict[str, Dict[str, Any]] = {
            uuid: info
            for uuid, info in raw_pending.items()
            if self._mode_monitored(info.get("mode_id"))
        }
        if len(self._pending) != len(raw_pending):
            # 关掉三麻/东场后，把对应场风的待查对局丢弃，不再补播
            self._save_pending()
        self._live_snapshot: List[Dict[str, Any]] = []
        self._live_uuids: set[str] = set()
        # 好友列表快照 {account_id: {nickname, online, game_uuid}}：
        # game_uuid 就是这局「观战」按钮指向的对局，空了或变了就说明这局结束了
        self._friend_states: Dict[int, Dict[str, Any]] = {}
        self._friend_poll_at = 0.0
        self._friend_error_logged = False
        # 本轮批量查到的账号状态 {account_id: {online, game_uuid, mode_id}}，
        # 只用于非好友分支的「还在打」保护（观战列表 100 条上限会提前挤掉对局）
        self._account_states: Dict[int, Dict[str, Any]] = {}
        self._state_error_logged = False
        # 手动「显示结果」的待查对局拉到牌谱后暂存在这里，供页面轮询读取
        self.manual_results: Dict[str, Any] = {}
        # 页面正在等结果的对局 uuid（只有这些牌谱才需要暂存给页面）
        self._result_watchers: Dict[str, float] = {}
        # 好友的局「观战还能点进去」的检查记录：uuid -> (上次记日志时间, 已检查次数)
        self._watch_log: Dict[str, tuple] = {}
        self._live_caches: Dict[int, Dict[str, Dict[str, Any]]] = self._load_live_caches()
        self._live_cache_dirty: set = set()
        self._live_cache_saved_at: Dict[int, float] = {}
        self._live_cache_dirty = {mode for mode, cache in self._live_caches.items() if cache}
        self._seen: Dict[str, float] = self._load_seen()
        self.current_uid: str = ""
        self.last_error: str = ""
        # 最近一次昵称解析失败的原因（添加监控找不到玩家时显示给用户看）
        self.last_resolve_error: str = ""
        # account_id -> 昵称缓存（好友申请日志里显示名字，不显示数字）
        self._name_cache: Dict[int, str] = {}
        # 同一个人同时给多个账号发好友申请时，只让一个账号同意：
        # account_id -> (同意它的账号 uid, 这个占位的过期时间)
        self._apply_claims: Dict[int, tuple] = {}
        self.last_record_code = 0
        self._reconnect_attempts = 0
        # 牌谱权限：记录当天没有权限的账号，次日 05:01 自动重置
        self.perm_path = self.data_dir / "monitor_paipu_perms.json"
        self._bad_paipu: Dict[str, str] = self._load_perm_marks()
        self._perm_day = _perm_day_key(time.time())

    # ── 今日播报次数（非好友每日限播）────────────────────
    def _load_broadcast_counts(self) -> Dict[str, Dict[str, int]]:
        path = self.data_dir / "monitor_broadcast.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 读取播报计数失败: {exc}")
            return {}
        if not isinstance(data, dict):
            return {}
        day = _today()
        today = data.get(day)
        if not isinstance(today, dict):
            return {}
        counts: Dict[str, int] = {}
        majsoul_ids = self._ids_majsoul()
        for key, value in today.items():
            try:
                account_id = int(key)
                if majsoul_ids:
                    account_id = int(to_account_id(account_id))
                counts[str(account_id)] = int(value or 0)
            except (TypeError, ValueError):
                continue
        return {day: counts}

    def _save_broadcast(self) -> None:
        try:
            payload = self._broadcast
            if self._ids_majsoul():
                payload = {}
                for day, bucket in self._broadcast.items():
                    payload[day] = {
                        str(to_majsoul(int(key))): value for key, value in bucket.items()
                    }
            self.broadcast_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 保存播报计数失败: {exc}")

    def broadcast_count_today(self, account_id: int) -> int:
        day = _today()
        bucket = self._broadcast.get(day) or {}
        return int(bucket.get(str(int(account_id or 0)), 0) or 0)

    def _bump_broadcast(self, account_id: int) -> int:
        day = _today()
        if day not in self._broadcast:
            self._broadcast = {day: {}}
        bucket = self._broadcast.setdefault(day, {})
        key = str(int(account_id or 0))
        bucket[key] = int(bucket.get(key, 0) or 0) + 1
        self._save_broadcast()
        return bucket[key]

    def apply_settings(
        self,
        *,
        poll_interval: Optional[int] = None,
        friend_poll_interval: Optional[int] = None,
        monitor_three_player: Optional[bool] = None,
        monitor_east: Optional[bool] = None,
        auto_approve_friends: Optional[bool] = None,
        nonfriend_daily_limit: Optional[int] = None,
        daily_limit: Optional[int] = None,
        search_urls: Optional[List[str]] = None,
    ) -> List[str]:
        """热更新设置：不重连，正在跑的这一轮扫描结束后就生效。

        返回实际变化的中文说明（用于日志和网页回显），例如「已关闭三麻监控」。
        """
        applied: List[str] = []
        if poll_interval is not None:
            value = max(10, int(poll_interval))
            if value != self.poll_interval:
                self.poll_interval = value
                applied.append(f"非好友轮询间隔改为 {value} 秒")
        if friend_poll_interval is not None:
            value = max(10, int(friend_poll_interval))
            if value != self.friend_poll_interval:
                self.friend_poll_interval = value
                applied.append(f"好友轮询间隔改为 {value} 秒")
        if monitor_three_player is not None or monitor_east is not None:
            three = (
                self.monitor_three_player
                if monitor_three_player is None
                else bool(monitor_three_player)
            )
            east = self.monitor_east if monitor_east is None else bool(monitor_east)
            if three != self.monitor_three_player:
                applied.append("已开启三麻监控" if three else "已关闭三麻监控")
            if east != self.monitor_east:
                applied.append("已开启东场监控" if east else "已关闭东场监控")
            if (three, east) != (self.monitor_three_player, self.monitor_east):
                self.monitor_three_player = three
                self.monitor_east = east
                # 重算要轮询的观战列表（关掉三麻/东场就不再请求对应房间）
                self._active_live_mode_ids = tuple(
                    mode_id
                    for mode_id in SCAN_MODE_IDS
                    if (three or mode_id not in THREE_PLAYER_MODE_IDS)
                    and (east or mode_id not in EAST_MODE_IDS)
                )
        if auto_approve_friends is not None:
            value = bool(auto_approve_friends)
            if value != self.auto_approve_friends:
                self.auto_approve_friends = value
                applied.append("已开启自动同意好友申请" if value else "已关闭自动同意好友申请")
        if nonfriend_daily_limit is not None:
            value = int(nonfriend_daily_limit)
            if value != self.nonfriend_daily_limit:
                self.nonfriend_daily_limit = value
                text = "无限制" if value < 0 else f"每天最多 {value} 局"
                applied.append(f"非好友每日播报上限改为 {text}")
        if daily_limit is not None:
            value = max(1, int(daily_limit))
            if int(getattr(self.quota, "limit", value)) != value:
                self.quota.limit = value
                applied.append(f"每日牌谱查询上限改为 {value} 次")
        if search_urls is not None:
            urls = [u for u in search_urls if u]
            if urls and urls != self.search_urls:
                self.search_urls = urls
                applied.append("牌谱屋查询节点已更新")
        if applied:
            self._event("配置更新", "、".join(applied), "INFO")
        return applied

    def nonfriend_limit(self) -> int:
        """非好友每日播报上限；-1 = 无限制。"""
        return int(self.nonfriend_daily_limit)

    def nonfriend_limit_reached(self, account_id: int) -> bool:
        limit = self.nonfriend_limit()
        if limit < 0:
            return False
        return self.broadcast_count_today(account_id) >= limit

    def _display_id(self, account_id: int) -> str:
        """日志里显示雀魂 ID。"""
        return self.display_uid(account_id)

    def _inflight_count(self, account_id: int) -> int:
        """这个玩家在待查列表里还没播报的局数。"""
        aid = int(account_id or 0)
        if not aid:
            return 0
        count = 0
        for info in self._pending.values():
            for target in self._pending_targets(info):
                if int(target.get("account_id") or 0) == aid:
                    count += 1
                    break
        return count

    def _nonfriend_can_monitor(self, account_id: int) -> bool:
        """今天已播报 + 还在等播报的局数，还没到上限才监控新的一局。"""
        limit = self.nonfriend_limit()
        if limit < 0:
            return True
        return self.broadcast_count_today(account_id) + self._inflight_count(account_id) < limit

    def _enabled_account_ids(self) -> List[str]:
        """账号池里已启用账号的雀魂ID（加好友提示用）。"""
        ids: List[str] = []
        for acc in self._accounts():
            if not bool(acc.get("enabled", True)):
                continue
            uid = str(acc.get("uid") or "").strip()
            if not uid.isdigit():
                continue
            text = self.display_uid(uid)
            if text and text not in ids:
                ids.append(text)
        return ids

    def nonfriend_notice(self) -> str:
        """「这个账号还没被任何在线账号加好友」时的提示语（播报末尾、添加成功回复都用它）。"""
        # ID 全部来自账号池：只列已启用的账号，插件里不存任何写死的 ID。
        # 一个启用账号都没有时不会有播报，这里返回空串（不追加提示）；
        # 需要「目前无法播报，请联系管理员在账号池添加账号。」的场景由 main.py 的守卫负责。
        ids = "、".join(self._enabled_account_ids())
        if not ids:
            return ""
        limit = self.nonfriend_limit()
        if limit < 0:
            return f"该账号尚未添加好友，请添加以下任意一个ID的雀魂账号为好友：{ids}。"
        return (
            f"该账号尚未添加好友，请添加以下任意一个ID的雀魂账号为好友：{ids}，"
            f"否则每日仅限播报{limit}次。"
        )

    def _mode_monitored(self, mode_id: int) -> bool:
        """该场风当前是否要监控；未知场风（mode_id=0）保持兼容，继续处理。

        这里只按「三麻/东场」开关过滤，不按轮询列表过滤：铜/银房间虽然不轮询，
        但好友列表兜底发现的对局仍可能来自这些段位场，照常收下。
        """
        try:
            mode_id = int(mode_id or 0)
        except (TypeError, ValueError):
            return True
        if not mode_id:
            return True
        if mode_id not in LIVE_MODE_IDS:
            return True
        if mode_id in THREE_PLAYER_MODE_IDS and not self.monitor_three_player:
            return False
        if mode_id in EAST_MODE_IDS and not self.monitor_east:
            return False
        return True

    # ── 牌谱权限（每天 05:01 重置）────────────────────────
    def _load_perm_marks(self) -> Dict[str, str]:
        if not self.perm_path.exists():
            return {}
        try:
            data = json.loads(self.perm_path.read_text(encoding="utf-8-sig") or "{}")
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 读取牌谱权限记录失败: {exc}")
            return {}
        if not isinstance(data, dict):
            return {}
        today = _perm_day_key(time.time())
        return {str(uid): str(day) for uid, day in data.items() if str(day) == today}

    def _save_perm_marks(self) -> None:
        try:
            self.perm_path.parent.mkdir(parents=True, exist_ok=True)
            self.perm_path.write_text(json.dumps(self._bad_paipu, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 保存牌谱权限记录失败: {exc}")

    def _perm_is_ok(self, uid: str) -> bool:
        uid = str(uid or "")
        return self._bad_paipu.get(uid) != self._perm_day

    def _refresh_perm_marks(self) -> bool:
        """过了每天 05:01 就清空所有「无牌谱权限」标记，重新从第一个账号开始尝试。"""
        today = _perm_day_key(time.time())
        if today == self._perm_day:
            return False
        self._perm_day = today
        had_bad = bool(self._bad_paipu)
        self._bad_paipu.clear()
        self._save_perm_marks()
        self._event(
            "牌谱权限重置",
            "已到新的结算日（05:01），所有账号重新尝试登录" if had_bad else "已到新的结算日（05:01）",
            "INFO",
        )
        self._account_changed.set()
        return True

    def mark_no_paipu_permission(self, uid: str) -> bool:
        uid = str(uid or "")
        if not uid:
            return False
        self._refresh_perm_marks()
        if self._bad_paipu.get(uid) == self._perm_day:
            return False
        self._bad_paipu[uid] = self._perm_day
        self._save_perm_marks()
        return True

    def clear_paipu_permission(
        self, uid: str, reason: str = "手动恢复", log: bool = True
    ) -> bool:
        """把账号牌谱权限立即恢复为「有」；返回这次是否真的清掉了「无权限」标记。

        只有原本被标记过「无」才记一条「牌谱权限恢复」——新加的账号本来就没有标记，
        不该冒出「重新加入账号池 / 权限恢复」这种日志。
        """
        uid = str(uid or "")
        if not uid:
            return False
        self._refresh_perm_marks()
        cleared = self._bad_paipu.pop(uid, None) is not None
        if cleared:
            self._save_perm_marks()
        if log and cleared:
            self._event(
                "牌谱权限恢复",
                f"账号 {self.display_uid(uid)} {reason}，牌谱权限重置为「有」",
                "INFO",
            )
        return cleared

    def paipu_permission(self, uid: str) -> str:
        """返回账号的牌谱权限状态：有 / 无。"""
        uid = str(uid or "")
        self._refresh_perm_marks()
        return "有" if not uid or self._perm_is_ok(uid) else "无"

    def _event(self, status: str, detail: str = "", level: str = "INFO") -> None:
        """写一条系统事件：既进 AstrBot 日志，也进插件的查询日志。"""
        text = f"{status} | {detail}" if detail else status
        level_up = level.upper()
        if level_up == "SCAN":
            # 每 30s 一条的轮询记录只进插件查询日志，不刷 AstrBot 主日志
            log_fn = logger.debug
        elif level_up == "ERROR":
            log_fn = logger.error
        elif level_up == "WARN":
            log_fn = logger.warning
        else:
            log_fn = logger.info
        try:
            log_fn(f"[MajsoulMonitor] {text}")
        except Exception:
            pass
        if self.log_event is not None:
            try:
                self.log_event(status, detail, level)
            except Exception:
                pass

    def cleanup_old_paipu(self, force: bool = False) -> int:
        """删除超过 PAIPU_RETAIN_DAYS 天的本地牌谱缓存（raw json 和役满牌面图）。

        每天最多清理一次；启动后的第一轮扫描也会触发一次（新实例 _paipu_cleanup_day 为空）。
        """
        today = datetime.now().date().isoformat()
        if not force and self._paipu_cleanup_day == today:
            return 0
        self._paipu_cleanup_day = today
        cutoff = time.time() - PAIPU_RETAIN_DAYS * 86400
        roots = (
            self.data_dir / "review" / "paipu",
            self.data_dir / "review" / "yakuman",
        )
        removed = 0
        for root in roots:
            if not root.is_dir():
                continue
            for f in root.iterdir():
                try:
                    if not f.is_file() or f.stat().st_mtime >= cutoff:
                        continue
                    f.unlink()
                    removed += 1
                except FileNotFoundError:
                    continue
                except Exception as exc:
                    logger.warning(f"[MajsoulMonitor] 清理旧牌谱失败 {f.name}: {exc}")
        if removed:
            self._event("清理牌谱", f"已清理 {removed} 个超过 {PAIPU_RETAIN_DAYS} 天的本地牌谱文件", "INFO")
        return removed

    # ── 生命周期 ──────────────────────────────────────────
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        # 热重载可能留下旧实例的监控任务（旧版好友监控也在内），它们会用同一个账号
        # 反复登录，和本实例互相顶号（1004）。启动前统一清理掉。
        cancelled = _cancel_stale_monitor_tasks()
        if cancelled:
            self._event("清理残留任务", f"取消了 {cancelled} 个旧监控任务", "WARN")
        _monitor_registry().add(self)
        self._reconnect_attempts = 0
        self._stop.clear()
        self._poll_wake.clear()
        self._last_poll_at = time.time()
        self._save_live_cache(force=True)
        self._task = asyncio.create_task(self._run(), name="majsoul-account-monitor")
        self._task.add_done_callback(self._on_run_task_done)
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="majsoul-login-check"
            )
        self._event(
            "监控已启动",
            f"对局扫描每 {self.scan_interval}s，好友轮询间隔 {self.friend_poll_interval}s，"
            f"非好友轮询间隔 {self.poll_interval}s，登录检测每 60s",
            "INFO",
        )
        if self._pending:
            names = "、".join(
                sorted({str(i.get("nickname") or i.get("uuid") or "") for i in self._pending.values()})
            )
            self._event(
                "恢复待查牌谱",
                f"{len(self._pending)} 个（{names}），继续查询直到出牌谱",
                "INFO",
            )

    async def stop(self, reason: str = "插件停止") -> None:
        self._stop.set()
        with contextlib.suppress(Exception):
            _monitor_registry().discard(self)
        for task in (self._task, self._heartbeat_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._task = None
        self._heartbeat_task = None
        await self._close_all_sessions()
        self._save_live_cache(force=True)
        self._event("监控已停止", reason, "INFO")

    def _on_run_task_done(self, task: asyncio.Task) -> None:
        """监控任务退出时写日志，避免“静默死亡”。"""
        if task.cancelled() or self._stop.is_set():
            return
        exc = None
        try:
            exc = task.exception()
        except Exception:
            pass
        if exc is not None:
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            try:
                logger.error(f"[MajsoulMonitor] 监控任务异常退出: {self.last_error}", exc_info=exc)
            except Exception:
                pass
            self._event("监控任务退出", f"{self.last_error}（重载插件可恢复）", "ERROR")
        else:
            logger.warning("[MajsoulMonitor] 监控任务结束（未设置停止标志）")

    @property
    def connected(self) -> bool:
        """只要有一个账号在线就算已连接。"""
        return bool(self.online_sessions())

    @property
    def reconnecting(self) -> bool:
        return bool(self._reconnecting)

    # ── 登录状态检测（每 60s）─────────────────────────────
    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=LOGIN_CHECK_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            try:
                self._watchdog_check()
                if self._refresh_perm_marks():
                    # 新的一天：牌谱权限恢复，让会话重新对齐一次（已在线的不重连）
                    await self.refresh_sessions()
                await self._check_login()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"[MajsoulMonitor] 登录检测异常: {exc}")

    def _watchdog_check(self) -> None:
        """监控循环的看门狗：任务死了就重启，太久没扫描就报警。"""
        if self._stop.is_set():
            return
        task = self._task
        if task is None or task.done():
            self._event("监控重启", "监控任务已退出，重新启动监控循环", "WARN")
            self._last_poll_at = time.time()
            self._task = asyncio.create_task(self._run(), name="majsoul-account-monitor")
            self._task.add_done_callback(self._on_run_task_done)
            return
        gap = time.time() - (self._last_poll_at or time.time())
        if gap > max(300, self.scan_interval * 6):
            self._event(
                "监控疑似卡住",
                f"已经 {int(gap)}s 没有开始新一轮扫描（任务仍在运行）",
                "ERROR",
            )
            self._last_poll_at = time.time()  # 避免每分钟重复刷屏

    async def _check_login(self) -> None:
        if self._stop.is_set():
            return
        for session in list(self._sessions.values()):
            if not session.is_online() or session.conn is None:
                continue
            try:
                await asyncio.wait_for(
                    session.conn.rpc_call(
                        ".lq.Route.heartbeat",
                        {
                            "delay": 0,
                            "no_operation_counter": 0,
                            "platform": 11,
                            "network_quality": 100,
                        },
                    ),
                    timeout=15,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._event(
                    "登录检测",
                    f"{session.label} 心跳失败（{exc.__class__.__name__}: {exc}），判定掉线，开始重连",
                    "WARN",
                )
                self._reconnecting = True
                await self._close_session(session.uid)
        if self.online_sessions():
            self._reconnecting = False
            return
        if self._conn_lock.locked():
            # 首次登录或重连正在另一个任务里进行（例如刚重载插件、握手较慢），
            # 等它完成即可，不要在这里重复报「当前未连接」并再触发一次重连
            return
        if not self._reconnecting:
            self._event("登录检测", "当前未连接，开始重连", "WARN")
        self._reconnecting = True

        try:
            await self._ensure_connection()
            self._reconnecting = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._event("重连失败", f"{exc.__class__.__name__}: {exc}，下轮继续", "ERROR")

    # ── 主循环 ─────────────────────────────────────────────
    async def _run(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            try:
                try:
                    await self._ensure_connection()
                except asyncio.CancelledError:
                    raise
                except NoAccountAvailable as exc:
                    self._reconnecting = True
                    self.last_error = f"NoAccountAvailable: {exc}"
                    if not self._waiting_account:
                        self._waiting_account = True
                        self._event(
                            "等待账号",
                            f"{exc}；账号池新增或启用账号后自动登录，不再定时重连",
                            "WARN",
                        )
                    await self._wait_for_account_change()
                    self._waiting_account = False
                    backoff = 5
                    continue
                except Exception as exc:
                    self.last_error = f"{exc.__class__.__name__}: {exc}"
                    self._reconnect_attempts += 1
                    self._reconnecting = True
                    delay = min(60, backoff)
                    self._event(
                        "登录失败",
                        f"{self.last_error}（{delay}s 后重试，第 {self._reconnect_attempts} 次）",
                        "ERROR",
                    )
                    await self._close_conn()
                    await asyncio.sleep(delay)
                    backoff = min(60, backoff * 2)
                    continue

                scan_started = time.time()
                try:
                    await asyncio.wait_for(self._poll_once(), timeout=POLL_TIMEOUT)
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    self.last_error = f"扫描超时（>{POLL_TIMEOUT}s）"
                    self._event("连接断开", f"uid={self.display_uid(self.current_uid)} {self.last_error}", "WARN")
                    await self._close_conn()
                    self._reconnect_attempts += 1
                    self._reconnecting = True
                    delay = min(60, backoff)
                    self._event(
                        "准备重连",
                        f"uid={self.display_uid(self.current_uid)} {delay}s 后重新登录（第 {self._reconnect_attempts} 次）",
                        "WARN",
                    )
                    await asyncio.sleep(delay)
                    backoff = min(60, backoff * 2)
                    continue
                except Exception as exc:
                    self.last_error = f"{exc.__class__.__name__}: {exc}"
                    self._event("连接断开", f"uid={self.display_uid(self.current_uid)} {self.last_error}", "WARN")
                    await self._close_conn()
                    self._reconnect_attempts += 1
                    self._reconnecting = True
                    delay = min(60, backoff)
                    self._event(
                        "准备重连",
                        f"uid={self.display_uid(self.current_uid)} {delay}s 后重新登录（第 {self._reconnect_attempts} 次）",
                        "WARN",
                    )
                    await asyncio.sleep(delay)
                    backoff = min(60, backoff * 2)
                    continue
                if self._reconnect_attempts:
                    self._event("重连成功", f"已恢复轮询（此前重试 {self._reconnect_attempts} 次）", "INFO")
                self._reconnect_attempts = 0
                backoff = 5
                # 每天第一次成功轮询前，顺手清掉超过一个月的本地牌谱
                self.cleanup_old_paipu()
                # 固定 30s 一轮：按本轮扫描开始时间计算，避免「扫描耗时 + 间隔」导致实际变慢
                delay = self.scan_interval - (time.time() - scan_started)
                if delay < 0:
                    delay = 0
                # 切号/账号池变化要立即唤醒，不能等完整个轮询间隔，
                # 否则心跳会先把旧账号重连一遍，再切一次。
                if delay:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._poll_wake.wait(), timeout=delay)
                self._poll_wake.clear()
            except asyncio.CancelledError:
                # 被取消（含热重载清理）时尽量把 WebSocket 关掉，避免服务端还占着会话
                self._stop.set()
                hb = self._heartbeat_task
                self._heartbeat_task = None
                if hb is not None and not hb.done():
                    with contextlib.suppress(Exception):
                        hb.cancel()
                for session in list(self._sessions.values()):
                    conn, session.conn = session.conn, None
                    if conn is not None:
                        with contextlib.suppress(RuntimeError):
                            asyncio.create_task(_safe_close(conn))
                self._sessions.clear()
                self.conn = None
                raise
            except Exception as exc:
                # 兜底：任何没被内层捕获的异常都不能让监控任务静默死掉
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                self._reconnecting = True
                try:
                    self._event("监控循环异常", f"{self.last_error}，5s 后继续", "ERROR")
                except Exception:
                    logger.exception("[MajsoulMonitor] 记录监控异常失败")
                with contextlib.suppress(Exception):
                    await self._close_conn()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(5)
                backoff = min(60, backoff * 2)

    # ── 多账号会话管理 ────────────────────────────────────
    def online_sessions(self) -> List["AccountSession"]:
        return [s for s in self._sessions.values() if s.is_online()]

    def _primary_session(self) -> Optional["AccountSession"]:
        session = self._sessions.get(self.current_uid or "")
        if session is not None and session.is_online():
            return session
        online = self.online_sessions()
        return online[0] if online else None

    @staticmethod
    def display_uid(uid) -> str:
        """把内部 account_id 换成雀魂里显示的数字 ID（日志/界面用）。"""
        try:
            return str(encode_account_id(int(uid)))
        except (TypeError, ValueError):
            return str(uid)

    def _sync_primary_conn(self) -> None:
        session = self._sessions.get(self.current_uid or "")
        self.conn = session.conn if session is not None else None

    def _desired_accounts(self) -> List[Dict[str, Any]]:
        """账号池里所有启用的账号都要登录（不设数量上限）。"""
        accounts = [a for a in self._accounts() if bool(a.get("enabled", True))]
        if not accounts:
            return []
        self._refresh_perm_marks()
        # 今天没牌谱权限的排后面，但照样登录（观战列表/好友列表都不需要牌谱权限）
        return sorted(
            accounts,
            key=lambda a: 0 if self._perm_is_ok(str(a.get("uid") or "")) else 1,
        )

    async def _ensure_connection(self) -> None:
        async with self._conn_lock:
            await self._ensure_sessions_locked()

    async def _ensure_sessions_locked(self) -> None:
        """把在线会话对齐成「账号池里所有启用的账号」。调用方需持有 _conn_lock。"""
        wanted = self._desired_accounts()
        if not wanted:
            raise NoAccountAvailable(
                "账号池没有启用的账号（请先在账号池添加并启用账号）"
            )
        wanted_uids = {str(a.get("uid") or "") for a in wanted}
        for uid in list(self._sessions):
            if uid not in wanted_uids:
                await self._close_session_locked(uid, "账号池里已禁用或删除")
        now = time.time()
        errors: List[str] = []
        todo: List[Dict[str, Any]] = []
        for account in wanted:
            uid = str(account.get("uid") or "")
            session = self._sessions.get(uid)
            if session is not None and session.is_online():
                session.fail_count = 0
                continue
            if session is not None and now < session.next_try_at:
                errors.append(f"{uid}: {session.last_error or '等待重试'}")
                continue
            todo.append(account)

        if todo:
            # 所有待登录账号一起握手：串行的话每个要 10 多秒，账号越多越慢
            results = await asyncio.gather(
                *(self._open_connection(account) for account in todo),
                return_exceptions=True,
            )
            for account, result in zip(todo, results):
                uid = str(account.get("uid") or "")
                if isinstance(result, BaseException):
                    self._record_connect_failure(account, result, now)
                    session = self._sessions.get(uid)
                    errors.append(f"{uid}: {session.last_error if session else result}")
                    continue
                conn, method = result
                try:
                    session = self._register_session(account, conn, method)
                    session.fail_count = 0
                    session.next_try_at = 0.0
                    session.last_error = ""
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await asyncio.wait_for(conn.close(), timeout=10)
                    self._record_connect_failure(account, exc, now)
                    errors.append(f"{uid}: {exc}")
        online = self.online_sessions()
        if not online:
            raise RuntimeError("；".join(errors) or "没有登录成功的账号")
        online_uids = {s.uid for s in online}
        if not self.current_uid:
            # 首次登录：直接认定第一个在线账号，这不算「切换账号」，不用记日志
            self.current_uid = online[0].uid
        elif self.current_uid not in online_uids:
            previous = self.current_uid
            self.current_uid = online[0].uid
            self._event("切换账号", f"原账号 {previous} 不在线，改用 {online[0].label}", "INFO")
        self._reconnecting = False
        self._sync_primary_conn()

    def _record_connect_failure(
        self, account: Dict[str, Any], exc: BaseException, now: float
    ) -> None:
        """记一次登录失败：累加退避时间，供下一轮重试。"""
        uid = str(account.get("uid") or "")
        session = self._sessions.get(uid)
        if session is None:
            session = AccountSession(uid, account)
            self._sessions[uid] = session
        session.conn = None
        session.fail_count += 1
        session.next_try_at = now + min(60, 5 * (2 ** (session.fail_count - 1)))
        session.last_error = f"{exc.__class__.__name__}: {exc}"

    async def _open_connection(self, account: Dict[str, Any]) -> Tuple[Any, str]:
        """只做登录握手（token 优先，失败回退账号密码），可以在多个账号之间并发调用。

        返回 (连接, 登录方式)；失败直接抛异常，由调用方记退避。
        """
        uid = str(account.get("uid") or "")
        token = str(account.get("token") or "")
        username = str(account.get("username") or "")
        password = str(account.get("password") or "")
        sem = getattr(self, "_login_sem", None)
        if sem is None:
            sem = self._login_sem = asyncio.Semaphore(LOGIN_CONCURRENCY)
        conn = None
        method = "账号密码"
        async with sem:
            if token:
                try:
                    conn = await create_connection(access_token=token, url_base=self.server_url)
                    method = "token"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._event(
                        "token 登录失败",
                        f"{uid} {exc.__class__.__name__}: {exc or '无描述'}，回退账号密码",
                        "WARN",
                    )
                    conn = None
            if conn is None:
                if not username or not password:
                    raise RuntimeError(f"账号 {uid} 缺少用户名或密码")
                method = "账号密码"
                conn = await create_connection(
                    username=username, password=password, url_base=self.server_url
                )
        return conn, method

    async def _connect_session_locked(self, account: Dict[str, Any]) -> "AccountSession":
        """登录单个账号（握手 + 登记）。调用方需持有 _conn_lock。"""
        conn, method = await self._open_connection(account)
        return self._register_session(account, conn, method)

    def _register_session(
        self, account: Dict[str, Any], conn: Any, method: str
    ) -> "AccountSession":
        """把已经握手成功的连接登记进 self._sessions，并记一条「账号已连接」。

        这一步要串行做：里面可能写回账号池（修正 ID），并发写会互相覆盖。
        """
        uid = str(account.get("uid") or "")
        nickname = str(account.get("nickname") or "").strip() or str(
            getattr(conn, "nick_name", "") or ""
        ).strip()
        actual_uid = str(getattr(conn, "account_id", "") or "")
        if actual_uid and actual_uid != uid:
            old_uid = uid
            uid = actual_uid
            self._sync_account_identity(account, actual_uid, nickname)
            self._event(
                "账号ID修正",
                f"{nickname} 账号池 ID {old_uid or '未知'} -> 游戏内 {actual_uid}",
                "INFO",
            )

        session = self._sessions.get(uid)
        if session is None:
            session = AccountSession(uid, account)
            self._sessions[uid] = session
        session.account = dict(account, uid=uid, nickname=nickname)
        session.nickname = nickname
        session.conn = conn
        session.fail_count = 0
        session.next_try_at = 0.0
        session.last_error = ""
        self.last_error = ""
        self._reconnecting = False
        suffix = ""
        if len(self.online_sessions()) > 1:
            suffix = f"，同时在线 {len(self.online_sessions())} 个账号"
        self._event(
            "账号已连接",
            f"{nickname}({self.display_uid(uid)}) 方式={method}{suffix}",
            "INFO",
        )
        return session

    async def _close_session_locked(self, uid: str, reason: str = "") -> None:
        session = self._sessions.pop(str(uid), None)
        if session is None:
            return
        label = session.label
        conn, session.conn = session.conn, None
        session.friends = {}
        if conn is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(conn.close(), timeout=10)
        if self.current_uid == str(uid):
            self.current_uid = ""
            self._sync_primary_conn()
        if reason:
            self._event("账号已断开", f"{label} 已退出登录（{reason}）", "INFO")

    async def _close_session(self, uid: str) -> None:
        async with self._conn_lock:
            await self._close_session_locked(uid)

    async def _close_conn_locked(self) -> None:
        """关闭主账号连接（调用方持有 _conn_lock）。"""
        if self.current_uid:
            await self._close_session_locked(self.current_uid)
        else:
            self.conn = None

    async def _close_conn(self) -> None:
        async with self._conn_lock:
            await self._close_conn_locked()

    async def _close_all_sessions(self) -> None:
        async with self._conn_lock:
            for uid in list(self._sessions):
                await self._close_session_locked(uid)
            self.conn = None

    def _sync_account_identity(
        self, account: Dict[str, Any], actual_uid: str, nickname: str
    ) -> None:
        """登录返回的 account_id 才是游戏内 ID；写回账号池，修正旧牌谱屋 ID。"""
        actual_uid = str(actual_uid or "").strip()
        if not actual_uid or not self.account_store_path.exists():
            return
        try:
            data = json.loads(
                self.account_store_path.read_text(encoding="utf-8-sig") or "[]"
            )
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 读取账号池失败，无法同步 ID: {exc}")
            return
        if not isinstance(data, list):
            return
        old_uid = str(account.get("uid", "") or "")
        username = str(account.get("username", "") or "")
        changed = False
        for item in data:
            if not isinstance(item, dict):
                continue
            matched = (
                bool(username and str(item.get("username", "")) == username)
                or (not username and str(item.get("uid", "")) == old_uid)
            )
            if not matched:
                continue
            if str(item.get("uid", "")) != actual_uid:
                item["uid"] = actual_uid
                changed = True
            if nickname and str(item.get("nickname", "")) != nickname:
                item["nickname"] = nickname
                changed = True
            break
        if changed:
            try:
                self.account_store_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                logger.info(
                    f"[MajsoulMonitor] 账号池 ID 已同步: {old_uid or username} -> {actual_uid}"
                )
            except Exception as exc:
                logger.warning(f"[MajsoulMonitor] 写回账号池 ID 失败: {exc}")

    def _accounts(self) -> List[Dict[str, Any]]:
        if not self.account_store_path.exists():
            return []
        try:
            data = json.loads(self.account_store_path.read_text(encoding="utf-8-sig") or "[]")
            return [a for a in data if isinstance(a, dict)]
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 读取账号池失败: {exc}")
            return []

    def notify_accounts_changed(self) -> None:
        """账号池新增/启用/禁用账号后调用：唤醒等待中的监控循环。"""
        try:
            self._account_changed.set()
        except Exception:
            pass
        try:
            self._poll_wake.set()
        except Exception:
            pass

    async def refresh_sessions(self) -> None:
        """账号池变化后立即对齐在线会话：只补连缺的、断开多的，已在线的不重连。"""
        self.notify_accounts_changed()
        async with self._conn_lock:
            try:
                await self._ensure_sessions_locked()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._event(
                    "账号池更新",
                    f"调整登录账号失败：{exc.__class__.__name__}: {exc}",
                    "WARN",
                )
                return
        self._sync_primary_conn()

    async def _wait_for_account_change(self) -> None:
        """没有可用账号时挂起，直到账号池变化或监控停止。"""
        while not self._stop.is_set():
            if self._account_changed.is_set():
                self._account_changed.clear()
                return
            try:
                await asyncio.wait_for(self._account_changed.wait(), timeout=5)
                self._account_changed.clear()
                return
            except asyncio.TimeoutError:
                continue

    # ── 好友申请自动审批 ─────────────────────────────────
    async def _process_friend_applies(self) -> bool:
        """处理每个在线账号的好友申请。

        规则：这个玩家已经是**别的**在线账号的好友 → 拒绝（避免重复加好友）；
        否则直接通过。返回是否处理过申请（处理过要重新拉一次好友列表）。
        """
        if not self.auto_approve_friends:
            return False
        now = time.time()
        for aid in [k for k, v in self._apply_claims.items() if v[1] <= now]:
            self._apply_claims.pop(aid, None)
        handled_any = False
        for session in self.online_sessions():
            if session.conn is None:
                continue
            if await self._process_session_friend_applies(session):
                handled_any = True
        return handled_any

    async def _process_session_friend_applies(self, session: "AccountSession") -> bool:
        conn = session.conn
        if conn is None:
            return False
        try:
            applies = await asyncio.wait_for(conn.fetch_friend_apply_list(), timeout=10)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._event("好友申请", f"{session.label} 拉取好友申请失败：{exc}", "WARN")
            return False
        if not applies:
            return False
        targets = [
            int(apply.get("account_id") or 0)
            for apply in applies[:20]
            if int(apply.get("account_id") or 0)
        ]
        if not targets:
            return False
        labels: Dict[int, str] = {}

        async def _resolve_label(account_id: int) -> None:
            try:
                labels[account_id] = await self._player_label(account_id)
            except Exception:
                labels[account_id] = str(account_id)

        await asyncio.gather(*[_resolve_label(tid) for tid in targets])

        def _name(account_id: int) -> str:
            return labels.get(int(account_id)) or str(account_id)

        handled: list[int] = []
        for target_id in targets:
            owner = self.friend_owner(target_id)
            if owner is not None and owner.uid != session.uid:
                try:
                    await asyncio.wait_for(conn.handle_friend_apply(target_id, 2), timeout=8)
                    self._event(
                        "好友申请",
                        f"{_name(target_id)} 已经是 {owner.label} 的好友，"
                        f"{session.label} 已拒绝这条重复的好友申请",
                        "INFO",
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._event(
                        "好友申请", f"{session.label} 拒绝 {_name(target_id)} 的申请失败：{exc}", "WARN"
                    )
                continue
            claim = self._apply_claims.get(target_id)
            if claim is not None and claim[0] != session.uid and claim[1] > time.time():
                # 另一个账号刚同意过这个人：本轮先别动，等那边好友列表刷新后
                # 由上面的「已经是 X 的好友」分支正式拒绝，避免两个账号都把人加上
                self._event(
                    "好友申请",
                    f"{_name(target_id)} 的申请已经由 {self.display_uid(claim[0])} 同意，"
                    f"{session.label} 本轮不再重复同意",
                    "SCAN",
                )
                continue
            try:
                await asyncio.wait_for(conn.handle_friend_apply(target_id, 1), timeout=8)
                handled.append(target_id)
                self._apply_claims[target_id] = (
                    session.uid,
                    time.time() + APPLY_CLAIM_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._event(
                    "好友申请", f"{session.label} 同意 {_name(target_id)} 的申请失败：{exc}", "WARN"
                )
        if not handled:
            return bool(targets)

        # 服务端返回成功不代表真正加上：以好友列表和申请列表为准复核。
        await asyncio.sleep(0.5)
        try:
            remaining = await asyncio.wait_for(conn.fetch_friend_apply_list(), timeout=6)
            still_applying = {
                int(a.get("account_id") or 0) for a in (remaining or [])
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            still_applying = set()
            self._event("好友申请", f"复核申请列表失败：{exc}", "WARN")
        try:
            friends = await asyncio.wait_for(conn.fetch_friend_list(), timeout=6)
            friend_ids = {int(f.get("account_id") or 0) for f in (friends or [])}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            friend_ids = set()
            self._event("好友申请", f"复核好友列表失败：{exc}", "WARN")

        for target_id in handled:
            if target_id in friend_ids:
                self._event(
                    "好友申请", f"{session.label} 已同意 {_name(target_id)} 的好友申请（好友列表已确认）", "INFO"
                )
                # 立刻写进快照：别的账号下一轮就能看到「已经是这个账号的好友」并拒绝，
                # 不用等 T1 好友列表刷新
                session.friends.setdefault(
                    target_id,
                    {
                        "account_id": target_id,
                        "nickname": _name(target_id),
                        "online": False,
                        "game_uuid": "",
                        "mode_id": 0,
                        "level_id": 0,
                        "level_score": 0,
                    },
                )
            elif target_id in still_applying:
                self._event(
                    "好友申请",
                    f"{session.label} 同意 {_name(target_id)} 返回成功，但申请仍未消失，下一轮重试",
                    "WARN",
                )
            else:
                self._event(
                    "好友申请",
                    f"{session.label} 同意 {_name(target_id)} 返回成功，申请已消失，但好友列表未出现（对方可能已撤销）",
                    "WARN",
                )
        self._friend_states = self._friend_union()
        return True

    # ── 对局扫描（观战列表发现对局、账号状态判断结束）───────
    def _add_pending(
        self,
        uuid: str,
        mode_id: int,
        start_time: int,
        matched_targets: List[Dict[str, Any]],
        via: str = "观战列表",
        friend: bool = False,
        contest_uid: str = "",
        room_id: int = 0,
        allow_unknown_mode: bool = False,
    ) -> bool:
        """记录一局待查牌谱；已播报或已在待查列表里时返回 False。"""
        uuid = str(uuid or "")
        if not uuid or uuid in self._seen or uuid in self._pending:
            return False
        if not matched_targets:
            return False
        mode_id = int(mode_id or 0)
        room_id = int(room_id or 0)
        names = "、".join(t.get("nickname") or str(t.get("account_id")) for t in matched_targets)
        # 好友列表里的对局有时不带 meta（mode_id 拿不到）。这种情况只在既没有 room_id
        # 也没有 contest_uid 时按段位场收下：友人戦一定带 room_id、比赛场一定带 contest_uid
        unknown_ok = bool(allow_unknown_mode and mode_id == 0 and not room_id and not contest_uid)
        if contest_uid or room_id or (mode_id not in LIVE_MODE_IDS and not unknown_ok):
            # 比赛场/友人戦/自定义房间：都不是段位场，没有公开牌谱，不监控也不播报
            if contest_uid:
                where = f"比赛场（contest={contest_uid}）"
            elif room_id:
                where = f"友人戦房间（{room_id}）"
            else:
                where = self._mode_label(mode_id)
            self._event(
                "非段位场",
                f"{names} 正在 {where}（不是段位场），这局不监控、不播报",
                "SCAN",
            )
            return False
        if not friend:
            # 不是任何一个在线账号的好友：看今天播报 + 还没播报的这局，是不是已经到上限了
            for target in matched_targets:
                aid = int(target.get("account_id") or 0)
                if not aid or self.is_friend_of_any(aid):
                    continue
                if not self._nonfriend_can_monitor(aid):
                    self._event(
                        "非好友限播",
                        f"{target.get('nickname') or aid}（{self._display_id(aid)}）今天已播报 "
                        f"{self.broadcast_count_today(aid)} 次，还有 "
                        f"{self._inflight_count(aid)} 局在等播报，达到上限 "
                        f"{self.nonfriend_limit()} 次，这局不再监控（加好友后可继续播报）",
                        "WARN",
                    )
                    return False
        first = matched_targets[0]
        self._pending[uuid] = {
            "uuid": uuid,
            "account_id": int(first.get("account_id") or 0),
            "nickname": str(first.get("nickname") or ""),
            "mode_id": int(mode_id or 0),
            "start_time": int(start_time or 0),
            "found_at": time.time(),
            "wait_logged": False,
            "source": via,
            "friend": bool(friend),
            "targets": matched_targets,
        }
        names = "、".join(t.get("nickname") or str(t.get("account_id")) for t in matched_targets)
        how = (
            f"每 {self.friend_poll_interval}s 看一次好友列表里的观战入口"
            if friend
            else f"等它离开观战列表后每 {self.poll_interval}s 查一次牌谱"
        )
        self._event(
            "发现对局",
            f"{names} 正在 {self._mode_label(mode_id)} 对局中，"
            f"记录观战 uuid={uuid}（来源：{via}；{how}）",
            "INFO",
        )
        return True

    async def _poll_once(self) -> None:
        self._last_poll_at = time.time()
        targets = [
            t for t in self.get_targets()
            if int(t.get("account_id") or 0) or str(t.get("nickname") or "").strip()
        ]
        if not self.conn:
            return
        # 每 T1 刷新一次好友列表：好友列表里的 playing.game_uuid 就是这局「观战」
        # 按钮指向的对局，空了/变了就说明这局结束（只对好友有效，非好友查不到）。
        await self._refresh_friends_if_due()
        if not targets:
            # 没有监控目标也要把之前记录的待查牌谱继续查完
            self._live_uuids = set()
            if self._pending:
                if self._has_live_branch_pending():
                    await self._refresh_account_states()
                await self._process_pending()
            if not self._no_targets_logged:
                self._no_targets_logged = True
                self._event(
                    "无监控目标",
                    "监控列表里没有可用的 account_id/player_id，本轮扫描跳过（检查监控列表数据）",
                    "WARN",
                )
            self._event("轮询房间", "没有监控目标，本轮不扫房间", "SCAN")
            return
        self._no_targets_logged = False

        by_id = {int(t["account_id"]): t for t in targets if int(t.get("account_id") or 0)}
        # 昵称区分大小写：Hikaru 和 HIkaru 是不同玩家
        by_name = {
            str(t.get("nickname") or "").strip(): t
            for t in targets
            if str(t.get("nickname") or "").strip()
        }

        # 固定每 30s 扫一遍所有房间的当前对局，命中监控玩家就记下 uuid
        games = await self._fetch_all_live_games()
        # 非好友的待查对局才需要账号状态做「还在打」保护，好友走好友列表
        if self._has_live_branch_pending():
            await self._refresh_account_states()

        snapshot = []
        added = False
        touched = False
        friend_added = 0
        for game in games:
            matched = []
            for player in game.get("players", []):
                pid = int(player.get("account_id") or 0)
                pname = str(player.get("nickname") or "").strip()
                target = by_id.get(pid) or by_name.get(pname)
                if target is not None:
                    matched.append((target, player))
            if not matched:
                continue
            uuid = str(game.get("uuid") or "")
            if not uuid:
                continue
            mode_id = int(game.get("mode_id") or 0)
            snapshot.append({"uuid": uuid, "mode_id": mode_id, "players": game.get("players", [])})

            # 好友列表/账号状态发现时没有开局时间，之后在观战列表里再看到就补上
            known = self._pending.get(uuid)
            if known is not None and int(known.get("start_time") or 0) <= 0:
                start = int(game.get("start_time") or 0)
                if start > 0:
                    known["start_time"] = start
                    touched = True

            # 这一局里所有被监控的玩家都记下来，删除某个玩家时其余玩家仍可收到结果
            matched_targets = []
            seen_keys = set()
            for target, player in matched:
                pid = int(player.get("account_id") or 0)
                nickname = str(target.get("nickname") or player.get("nickname") or "")
                key = pid or nickname
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                matched_targets.append({"account_id": pid, "nickname": nickname})
            if self._add_pending(
                uuid,
                mode_id,
                int(game.get("start_time") or 0),
                matched_targets,
                friend=self._any_of_friends(matched_targets),
                contest_uid=str(game.get("contest_uid") or ""),
                room_id=int(game.get("room_id") or 0),
            ):
                added = True

        # 补充发现：好友列表里在打、但这一轮没出现在观战列表里的对局
        # （观战列表每个筛选只有 100 局，对局中途会被挤掉；好友列表里的
        #  playing.game_uuid 就是那一局，只要这个玩家在监控列表里就照常监控）
        target_names = {
            str(t.get("nickname") or "").strip() for t in targets if str(t.get("nickname") or "").strip()
        }
        for aid, state in self._friend_states.items():
            uuid = str(state.get("game_uuid") or "")
            if not uuid or uuid in self._seen or uuid in self._pending:
                continue
            nickname = str(state.get("nickname") or "").strip()
            if not any(int(t.get("account_id") or 0) == aid for t in targets) and (
                not nickname or nickname not in target_names
            ):
                continue
            mode_id = int(state.get("mode_id") or 0)
            if mode_id and not self._mode_monitored(mode_id):
                continue
            if self._add_pending(
                uuid,
                mode_id,
                0,
                [{"account_id": aid, "nickname": nickname}],
                via="好友列表",
                friend=True,
                contest_uid=str(state.get("contest_uid") or ""),
                room_id=int(state.get("room_id") or 0),
                allow_unknown_mode=True,
            ):
                added = True
                friend_added += 1

        if added or touched:
            self._save_pending()

        self._live_snapshot = snapshot
        self._live_uuids = {str(game.get("uuid") or "") for game in games}
        await self._process_pending()
        self._save_live_cache(force=True)
        # 每轮房间扫描都记一条（SCAN 级别，页面「插件日志」可单独筛选），
        # 这样随时能看到监控在按 scan_interval 正常轮询
        self._event(
            "轮询房间",
            f"扫了 {len(self._active_live_mode_ids)} 个房间共 {len(games)} 局进行中，"
            f"监控玩家命中 {len(snapshot)} 场"
            + (f"，好友列表补发现 {friend_added} 场" if friend_added else "")
            + f"，待查牌谱 {len(self._pending)} 个，{self.scan_interval}s 后再次扫描",
            "SCAN",
        )

    async def _fetch_all_live_games(self) -> List[Dict[str, Any]]:
        """按监控范围拉取观战列表（filter_id = 200 + mode_id）。"""
        if self.conn is None:
            return []
        mode_ids = self._active_live_mode_ids
        tasks = [self.conn.fetch_live_games(200 + mode_id) for mode_id in mode_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        games: List[Dict[str, Any]] = []
        errors = []
        for mode_id, result in zip(mode_ids, results):
            if isinstance(result, Exception):
                errors.append(f"{mode_id}: {result}")
                continue
            games.extend(result)
        if len(errors) == len(results):
            raise ConnectionError(f"观战列表请求全部失败: {'; '.join(errors)}")
        if errors:
            logger.debug(f"[MajsoulMonitor] 部分房间观战列表失败: {errors}")
        self._remember_live_games(games)
        return games

    # ── 账号状态：判断对局还在不在（替代「观战列表消失」）──────
    async def _fetch_player_states(self, account_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """批量查这些账号「现在在打哪一局」，返回 {account_id: {online, game_uuid, mode_id}}。

        这里的 playing.game_uuid 就是好友列表里点「观战」时用的那局（观战进得去
        说明 uuid 还在；进不去说明这局已经结束），所以不需要加好友也能判断：
        fetchAccountState 对非好友账号同样有效。
        """
        ids = sorted({int(x) for x in account_ids if int(x or 0)})
        if not ids or self.conn is None:
            return {}
        try:
            states = await asyncio.wait_for(
                self.conn.fetch_account_states(ids), timeout=STATE_RPC_TIMEOUT
            )
            self._state_error_logged = False
            return states or {}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 批量状态查不到时退回好友列表：playing.game_uuid 是同一份数据（只覆盖好友）
            if not self._state_error_logged:
                self._state_error_logged = True
                self._event(
                    "账号状态",
                    f"批量查账号状态失败（尝试用好友列表兜底）：{exc.__class__.__name__}: {exc}",
                    "WARN",
                )
        try:
            friends = await asyncio.wait_for(self.conn.fetch_friend_list(), timeout=STATE_RPC_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[MajsoulMonitor] 好友列表兜底也失败: {exc}")
            return {}
        wanted = set(ids)
        states: Dict[int, Dict[str, Any]] = {}
        for friend in friends or []:
            aid = int(friend.get("account_id") or 0)
            if aid not in wanted:
                continue
            states[aid] = {
                "account_id": aid,
                "online": bool(friend.get("online")),
                "game_uuid": str(friend.get("game_uuid") or ""),
                "mode_id": 0,
            }
        return states

    # ── 好友列表：每 T1 刷新一次，看「观战」还能不能点进去 ──────
    async def _refresh_friends_if_due(self) -> None:
        """按好友轮询间隔 T1 刷新每个在线账号的好友列表（顺带处理好友申请）。"""
        now = time.time()
        if now < self._friend_poll_at:
            return
        self._friend_poll_at = now + self.friend_poll_interval
        for session in self.online_sessions():
            if session.conn is None:
                continue
            await self._refresh_session_friends(session)
        self._friend_states = self._friend_union()
        # 先刷新快照再处理申请：判断「这个玩家是不是已经加在别的账号上」要靠这份快照
        if self.auto_approve_friends and await self._process_friend_applies():
            for session in self.online_sessions():
                if session.conn is None:
                    continue
                await self._refresh_session_friends(session)
            self._friend_states = self._friend_union()

    async def _refresh_session_friends(self, session: "AccountSession") -> None:
        """刷新一个账号的好友列表快照。"""
        if session.conn is None:
            return
        try:
            friends = await asyncio.wait_for(
                session.conn.fetch_friend_list(), timeout=STATE_RPC_TIMEOUT
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not session.friend_error_logged:
                session.friend_error_logged = True
                self._event(
                    "好友列表",
                    f"{session.label} 刷新好友列表失败（这局的结束判断退回观战列表）："
                    f"{exc.__class__.__name__}: {exc}",
                    "WARN",
                )
            return
        session.friend_error_logged = False
        states: Dict[int, Dict[str, Any]] = {}
        for friend in friends or []:
            aid = int(friend.get("account_id") or 0)
            if not aid:
                continue
            states[aid] = {
                "account_id": aid,
                "nickname": str(friend.get("nickname") or ""),
                "online": bool(friend.get("online")),
                "game_uuid": str(friend.get("game_uuid") or ""),
                "mode_id": int(friend.get("mode_id") or 0),
                "room_id": int(friend.get("room_id") or 0),
                "contest_uid": str(friend.get("contest_uid") or ""),
                "level_id": int(friend.get("level_id") or 0),
                "level_score": int(friend.get("level_score") or 0),
                "level3_id": int(friend.get("level3_id") or 0),
                "level3_score": int(friend.get("level3_score") or 0),
                "last_online": int(friend.get("last_online") or 0),
            }
        session.friends = states
        session.friends_at = time.time()

    def _friend_union(self) -> Dict[int, Dict[str, Any]]:
        """所有在线账号的好友并集（同一个玩家被多个账号加过时取第一个）。"""
        states: Dict[int, Dict[str, Any]] = {}
        for session in self._sessions.values():
            for aid, info in session.friends.items():
                states.setdefault(int(aid), info)
        return states

    def friend_owner(self, account_id: int) -> Optional["AccountSession"]:
        """哪个在线账号加了这个玩家为好友。"""
        aid = int(account_id or 0)
        if not aid:
            return None
        for session in self.online_sessions():
            if aid in session.friends:
                return session
        return None

    def _any_of_friends(self, matched_targets: List[Dict[str, Any]]) -> bool:
        for target in matched_targets:
            if self.friend_owner(int(target.get("account_id") or 0)) is not None:
                return True
        return False

    def is_friend_of_any(self, account_id: int) -> bool:
        return self.friend_state(account_id) is not None

    def friend_state(self, account_id: int) -> Optional[Dict[str, Any]]:
        """这个玩家在任意在线账号好友列表里的状态（会话快照优先，其次并集快照）。"""
        aid = int(account_id or 0)
        if not aid:
            return None
        for session in self._sessions.values():
            info = session.friends.get(aid)
            if info is not None:
                return info
        return self._friend_states.get(aid)

    def _friend_sort_key(self, friend: Dict[str, Any]) -> tuple:
        """好友列表排序：在线（含对局中）在前；离线的按最后在线时间倒序，最早的排最后。"""
        online = 1 if (friend.get("online") or friend.get("game_uuid")) else 0
        try:
            last = int(friend.get("last_online") or 0)
        except (TypeError, ValueError):
            last = 0
        return (-online, -last, str(friend.get("nickname") or ""))

    async def find_friend_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """在在线账号的好友列表里按昵称（或雀魂ID）找玩家，解析不到时兜底用。

        快照太旧时先刷新一次，避免用户刚加完好友就添加监控却搜不到。
        """
        key = str(name or "").strip()
        if not key:
            return None
        found = self._find_friend_in_snapshots(key)
        if found is not None:
            return found
        online = self.online_sessions()
        if not online:
            return None
        now = time.time()
        stale = any(
            now - float(session.friends_at or 0) > max(10, self.friend_poll_interval)
            for session in online
        )
        if not stale:
            return None
        for session in online:
            await self._refresh_session_friends(session)
        self._friend_states = self._friend_union()
        return self._find_friend_in_snapshots(key)

    def _find_friend_in_snapshots(self, key: str) -> Optional[Dict[str, Any]]:
        lowered = key.casefold()

        def match(aid: int, info: Dict[str, Any]) -> bool:
            nickname = str(info.get("nickname") or "").strip()
            if nickname and nickname.casefold() == lowered:
                return True
            try:
                return str(encode_account_id(int(aid))) == key
            except Exception:
                return False

        for session in self._sessions.values():
            for aid, info in session.friends.items():
                if match(int(aid), info):
                    merged = dict(info)
                    merged["account_id"] = int(aid)
                    return merged
        for aid, info in self._friend_states.items():
            if match(int(aid), info):
                merged = dict(info)
                merged["account_id"] = int(aid)
                return merged
        return None

    async def is_friend_of_any_fresh(self, account_id: int) -> bool:
        """判断前按需刷新一次好友列表（添加监控玩家时用，避免快照还没拉过）。"""
        aid = int(account_id or 0)
        if not aid:
            return False
        if self.is_friend_of_any(aid):
            return True
        online = self.online_sessions()
        if not online:
            return False
        now = time.time()
        stale = any(
            now - float(session.friends_at or 0) > max(10, self.friend_poll_interval)
            for session in online
        )
        if not stale:
            return False
        for session in online:
            await self._refresh_session_friends(session)
        self._friend_states = self._friend_union()
        return self.is_friend_of_any(aid)

    async def _player_label(self, account_id: int) -> str:
        """把 account_id 换成昵称（查不到就退回数字）。"""
        aid = int(account_id or 0)
        if not aid:
            return str(account_id)
        cached = self._name_cache.get(aid)
        if cached:
            return cached
        try:
            info = await self.lookup_account(aid)
        except Exception:
            info = None
        name = str((info or {}).get("nickname") or "").strip()
        if name:
            if len(self._name_cache) > 500:
                self._name_cache.clear()
            self._name_cache[aid] = name
            return name
        return str(aid)

    def _has_live_branch_pending(self) -> bool:
        for info in self._pending.values():
            if self._pending_branch(info) == "live":
                return True
        return False

    def _log_friend_watch(self, info: Dict[str, Any], source: str, now: float) -> None:
        """对局还在打时，每 T1 记一条「对局仍在进行中」的轮询情况（SCAN 级）。"""
        uuid = str(info.get("uuid") or "")
        if not uuid:
            return
        last, count = self._watch_log.get(uuid, (0.0, 0))
        if now - last < 5:
            # 同一轮扫描里 _process_pending 可能被调用两次（等牌谱落库那一下），别重复记
            return
        count += 1
        self._watch_log[uuid] = (now, count)
        if len(self._watch_log) > 200:
            for key in list(self._watch_log):
                if key not in self._pending:
                    self._watch_log.pop(key, None)
        relation = "好友" if self._pending_branch(info) == "friend" else "非好友"
        detail = f"{info.get('nickname')}（{relation}） uuid={uuid} 对局仍在进行中（第 {count} 次检查"
        started = float(info.get("start_time") or 0)
        if 0 < started <= now:
            detail += f"，已进行 {int((now - started) // 60)} 分钟"
        else:
            # 好友列表/账号状态发现的局没有开局时间，用第一次看到它的时间估算
            found = float(info.get("found_at") or 0)
            if 0 < found <= now:
                detail += f"，已监控 {int((now - found) // 60)} 分钟"
        interval = self.friend_poll_interval if relation == "好友" else self.poll_interval
        detail += f"），{interval}s 后再看"
        self._event("轮询观战", detail, "SCAN")

    async def _refresh_account_states(self, targets: Optional[List[Dict[str, Any]]] = None) -> None:
        """刷新本轮账号状态：监控列表里的玩家（用于发现对局）+ 待查对局里的玩家。"""
        ids = set()
        for target in (self.get_targets() if targets is None else targets):
            aid = int(target.get("account_id") or 0)
            if aid:
                ids.add(aid)
        for info in self._pending.values():
            if self._pending_branch(info) != "live":
                continue
            for target in self._pending_targets(info):
                aid = int(target.get("account_id") or 0)
                if aid:
                    ids.add(aid)
        self._account_states = await self._fetch_player_states(sorted(ids))

    def _any_target_friend(self, info: Dict[str, Any]) -> bool:
        for target in self._pending_targets(info):
            if self.friend_state(int(target.get("account_id") or 0)) is not None:
                return True
        return False

    def _pending_expired(self, info: Dict[str, Any], now: float) -> bool:
        """开局算起超过 PENDING_TIMEOUT_SECONDS 还没拉到牌谱就算超时。

        没有开局时间（好友列表/账号状态补充发现的局）时用「开始跟踪的时间」算；
        手动查询用「点开的时间」，避免老对局一加进来就被判超时。
        """
        found = float(info.get("found_at") or 0.0)
        started = float(info.get("start_time") or 0.0)
        anchor = started if started > 0 else found
        if bool(info.get("manual")) and found > 0:
            anchor = max(anchor, found)
        return anchor > 0 and now - anchor >= PENDING_TIMEOUT_SECONDS

    def _pending_branch(self, info: Dict[str, Any]) -> str:
        """这局走哪条路线判断结束。

        friend：玩家在当前登录账号的好友列表里——每 T1 看一次「观战能不能点进去」，
                点不进去（好友不在对局中）就说明这局结束；
        live：  不是好友——只能等观战列表里没有这局，之后按 T2 轮询牌谱。
        """
        if info.get("friend"):
            return "friend"
        return "friend" if self._any_target_friend(info) else "live"

    def _pending_play_state(self, info: Dict[str, Any]) -> tuple:
        """待查对局现在还在不在打：返回 (state, source)。

        state: playing（还在打）/ ended（已结束）/ unknown（判断不了，继续等）
        source: friend（好友列表观战入口）/ live（观战列表）/ state（账号状态）/
                timeout（长时间没有信号）
        """
        uuid = str(info.get("uuid") or "")
        if not uuid:
            return "ended", "live"
        if uuid in self._live_uuids:
            # 本轮房间列表刚扫过，这局还在打（好友列表/账号状态可能比它旧一轮，不能据此判结束）
            return "playing", "live"
        if self._pending_branch(info) == "friend":
            known = False
            for target in self._pending_targets(info):
                aid = int(target.get("account_id") or 0)
                if not aid:
                    continue
                state = self.friend_state(aid)
                if state is None:
                    continue
                known = True
                if str(state.get("game_uuid") or "") == uuid:
                    # 好友列表里这局还在 → 观战还能点进去 → 还在打
                    return "playing", "friend"
            if known:
                # 观战点不进去了（或者他已经开了新的一局）：这局结束
                return "ended", "friend"
        else:
            known = False
            for target in self._pending_targets(info):
                aid = int(target.get("account_id") or 0)
                state = self._account_states.get(aid) if aid else None
                if state is None:
                    continue
                known = True
                if str(state.get("game_uuid") or "") == uuid:
                    # 账号状态还能看到这局在打（观战列表每筛选只有 100 局，会被提前挤掉），
                    # 这一轮先不花查询次数
                    return "playing", "state"
            if known:
                # 账号状态里这局已经没了
                return "ended", "state"
        return "unknown", "friend" if self._pending_branch(info) == "friend" else "live"

    # ── 观战列表缓存（按房间/人数分成 12 个文件）─────────────
    def _live_cache_path(self, mode_id: int) -> Path:
        return self.data_dir / f"monitor_live_cache_{int(mode_id)}.json"

    def _load_live_caches(self) -> Dict[int, Dict[str, Dict[str, Any]]]:
        caches: Dict[int, Dict[str, Dict[str, Any]]] = {mode: {} for mode in LIVE_MODE_IDS}
        now = time.time()
        for mode_id in LIVE_MODE_IDS:
            path = self._live_cache_path(mode_id)
            if not path.exists():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
            except Exception as exc:
                logger.warning(f"[MajsoulMonitor] 读取观战缓存 {path.name} 失败: {exc}")
                continue
            if not isinstance(raw, dict):
                continue
            for uuid, entry in raw.items():
                if not uuid or not isinstance(entry, dict):
                    continue
                if now - float(entry.get("last_seen") or 0) > LIVE_CACHE_RETAIN:
                    continue
                entry.setdefault("uuid", uuid)
                entry["mode_id"] = int(entry.get("mode_id") or mode_id)
                caches[mode_id][uuid] = entry
        self._migrate_legacy_live_cache(caches)
        for cache in caches.values():
            if len(cache) > LIVE_CACHE_MAX:
                ordered = sorted(cache.items(), key=lambda kv: float(kv[1].get("last_seen") or 0))
                for uuid, _ in ordered[: len(cache) - LIVE_CACHE_MAX]:
                    cache.pop(uuid, None)
        return caches

    def _migrate_legacy_live_cache(self, caches: Dict[int, Dict[str, Dict[str, Any]]]) -> None:
        """旧版单文件 monitor_live_cache.json 一次性拆分成按房间的文件。"""
        legacy = self.data_dir / "monitor_live_cache.json"
        if not legacy.exists():
            return
        try:
            raw = json.loads(legacy.read_text(encoding="utf-8-sig") or "{}")
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 读取旧观战缓存失败: {exc}")
            return
        now = time.time()
        moved = 0
        if isinstance(raw, dict):
            for uuid, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                mode_id = int(entry.get("mode_id") or 0)
                if mode_id not in caches:
                    continue
                if now - float(entry.get("last_seen") or 0) > LIVE_CACHE_RETAIN:
                    continue
                entry.setdefault("uuid", uuid)
                caches[mode_id][uuid] = entry
                moved += 1
        try:
            legacy.rename(legacy.with_suffix(".json.bak"))
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 备份旧观战缓存失败: {exc}")
        logger.info(f"[MajsoulMonitor] 已把旧观战缓存拆分为按房间文件（{moved} 条）")

    def _save_live_cache(self, force: bool = False, mode_ids=None) -> None:
        ids = list(mode_ids) if mode_ids is not None else list(self._live_cache_dirty)
        if not ids:
            return
        now = time.time()
        for mode_id in ids:
            if mode_id not in self._live_cache_dirty:
                continue
            if not force and now - float(self._live_cache_saved_at.get(mode_id) or 0) < 15:
                continue
            try:
                path = self._live_cache_path(mode_id)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(self._live_caches.get(mode_id, {}), ensure_ascii=False),
                    encoding="utf-8",
                )
                self._live_cache_dirty.discard(mode_id)
                self._live_cache_saved_at[mode_id] = now
            except Exception as exc:
                logger.warning(f"[MajsoulMonitor] 保存观战缓存 {mode_id} 失败: {exc}")

    def _remember_live_games(self, games: List[Dict[str, Any]]) -> None:
        now = time.time()
        touched = set()
        for game in games:
            uuid = str(game.get("uuid") or "")
            if not uuid:
                continue
            mode_id = int(game.get("mode_id") or 0)
            if mode_id not in LIVE_MODE_IDS:
                continue
            cache = self._live_caches.setdefault(mode_id, {})
            entry = dict(game)
            entry["last_seen"] = now
            cache[uuid] = entry
            touched.add(mode_id)
        for mode_id in touched:
            self._prune_live_cache(mode_id, now)
            self._live_cache_dirty.add(mode_id)
        self._save_live_cache(mode_ids=touched)

    def _prune_live_cache(self, mode_id: int, now: Optional[float] = None) -> None:
        now = now or time.time()
        cache = self._live_caches.get(mode_id) or {}
        for uuid, entry in list(cache.items()):
            if now - float(entry.get("last_seen") or 0) > LIVE_CACHE_RETAIN:
                cache.pop(uuid, None)
        if len(cache) > LIVE_CACHE_MAX:
            ordered = sorted(cache.items(), key=lambda kv: float(kv[1].get("last_seen") or 0))
            for uuid, _ in ordered[: len(cache) - LIVE_CACHE_MAX]:
                cache.pop(uuid, None)

    def _merge_live_cache(self, mode_id: int, games: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """把该房间缓存里较早的对局并进来，并按开局时间倒序。"""
        live_uuids = {str(g.get("uuid") or "") for g in games}
        merged = []
        for game in games:
            item = dict(game)
            item["live"] = True
            merged.append(item)
        for uuid, entry in (self._live_caches.get(int(mode_id)) or {}).items():
            if uuid in live_uuids:
                continue
            if int(entry.get("mode_id") or 0) != int(mode_id):
                continue
            merged.append({
                "uuid": uuid,
                "start_time": int(entry.get("start_time") or 0),
                "mode_id": int(entry.get("mode_id") or 0),
                "players": entry.get("players", []),
                "live": False,
                "last_seen": entry.get("last_seen", 0),
            })
        merged.sort(key=lambda g: int(g.get("start_time") or 0), reverse=True)
        return merged

    @staticmethod
    def _mode_label(mode_id: int) -> str:
        """房间名（日志用）：认不出来的模式直接显示 mode 号。"""
        if not mode_id:
            return "未知房间"
        return LIVE_MODE_NAMES.get(int(mode_id), f"非段位场（mode={mode_id}）")

    async def _process_pending(self, settle_pass: int = 0) -> None:
        """查已结束对局的牌谱。

        好友的局：看好友列表里这局「观战」还能不能点进去（每 T1 一次），进不去了就是
        结束了，此时按 uuid 查一次牌谱（计 1 次）。
        非好友的局：只能等观战列表里没有这局，之后按 T2 轮询牌谱（每次计 1 次）。
        两条路线都不会「每 30s 硬查一次」，一局最多消耗 1 次（正常情况）。
        """
        monitored_ids, monitored_names = self._monitored_index()
        dirty = False
        wait_settle = 0.0
        for key, info in list(self._pending.items()):
            if key not in self._pending:
                continue
            uuid = str(info.get("uuid") or key)
            manual = bool(info.get("manual"))
            friend = self._pending_branch(info) == "friend"
            if friend and not info.get("friend"):
                # 后来成为好友（或好友列表刚刷新出来）：改走好友路线
                info["friend"] = True
                dirty = True
            if not manual:
                targets = self._pending_targets(info)
                remaining = [t for t in targets if self._target_in_index(t, monitored_ids, monitored_names)]
                if not remaining:
                    self._pending.pop(uuid, None)
                    self._save_pending()
                    self._event(
                        "停止查询牌谱",
                        f"uuid={uuid} 里的监控玩家都已删除，不再查询",
                        "INFO",
                    )
                    continue
                info["targets"] = remaining
                info["account_id"] = int(remaining[0].get("account_id") or 0)
                info["nickname"] = str(remaining[0].get("nickname") or "")

            now = time.time()
            # 开局两小时后还没拉到牌谱：多半这局根本没有公开牌谱（非段位场/异常局），
            # 再查下去只是白烧查询次数，直接放弃这条待查
            if self._pending_expired(info, now):
                self._pending.pop(uuid, None)
                self._mark_seen(uuid)
                dirty = True
                started = float(info.get("start_time") or 0) or float(info.get("found_at") or 0)
                minutes = int(max(0, now - started) // 60) if started else 0
                self._event(
                    "牌谱查询超时",
                    f"{info.get('nickname') or uuid} uuid={uuid} 已经查了 {minutes} 分钟"
                    f"（开局算起超过 {PENDING_TIMEOUT_SECONDS // 3600} 小时）还没拉到牌谱，"
                    f"不再查询，已从待查列表移除",
                    "WARN",
                )
                continue
            state, source = self._pending_play_state(info)
            ended_at = float(info.get("ended_at") or 0)
            if state == "unknown" and ended_at > 0:
                # 已经确认过结束（只是牌谱还没出来）：别退回「判断不了」，按结束后的重试节奏走
                state = "ended"
            if state == "playing" and now - float(info.get("found_at") or now) >= PENDING_MAX_PLAYING:
                # 一局不可能打 3 小时：状态长时间没刷新（查询一直失败）时按结束处理，避免卡死
                source = "timeout"
            elif state == "playing":
                # 还在打：清掉上次残留的结束标记，继续等它打完
                if info.pop("ended_at", None) is not None or info.pop("ended_by", None) is not None:
                    dirty = True
                if info.get("absent_since"):
                    info["absent_since"] = 0
                    dirty = True
                # 还在打：每轮都记一条「对局仍在进行中」，名字后面标好友/非好友
                self._log_friend_watch(info, source, now)
                continue

            if state == "unknown":
                # 账号状态查不到（没有状态记录/隐私设置）时退回观战列表：
                # 消失一段时间才算结束，避免列表被挤掉就误判
                absent_since = float(info.get("absent_since") or 0)
                if absent_since <= 0:
                    info["absent_since"] = now
                    dirty = True
                    continue
                grace = MANUAL_ABSENT_GRACE if manual else LIVE_ABSENT_GRACE
                if now - absent_since < grace:
                    continue

            ended_at = float(info.get("ended_at") or 0)
            if ended_at <= 0:
                ended_at = now
                info["ended_at"] = ended_at
                info["ended_by"] = source or "live"
                info["attempts"] = 0
                info["next_query_at"] = 0
                dirty = True
                via = {
                    "friend": "好友列表里的这局观战已经进不去了",
                    "state": "账号状态里这局已经没了",
                    "timeout": "长时间没有结束信号，按已结束处理",
                }.get(source, f"观战列表里已经没有这局，按非好友轮询间隔 {self.poll_interval}s 查牌谱")
                self._event(
                    "对局结束",
                    f"{info.get('nickname')} uuid={uuid}（{via}），等牌谱落库后查询",
                    "INFO",
                )
            remain = ended_at + END_SETTLE_SECONDS - now
            if remain > 0:
                wait_settle = max(wait_settle, remain)
                continue
            if float(info.get("next_query_at") or 0) > now:
                continue

            mode_id = int(info.get("mode_id") or 0)
            if mode_id and mode_id not in LIVE_MODE_IDS:
                # 友人戦/大会/自定义房间不生成可查的公开牌谱，一直问只会一直烧查询次数
                self._event(
                    "跳过查询",
                    f"{info.get('nickname')} uuid={uuid} 这局在 {self._mode_label(mode_id)}，"
                    f"不是段位场、没有公开牌谱，已从待查列表移除",
                    "WARN",
                )
                self._pending.pop(uuid, None)
                dirty = True
                continue

            # 查询结果由 fetch_record 计数：成功或非 540 失败算一次；
            # 540（无权限）和请求异常不计数。手动加入待查列表的对局也走同一规则。
            self._event(
                "查牌谱",
                f"{info.get('nickname')} uuid={uuid} 第 {int(info.get('attempts') or 0) + 1} 次查询",
                "SCAN",
            )
            head = await self.fetch_record(uuid, quiet=True)
            if head is None:
                code = self.last_record_code
                attempts = int(info.get("attempts") or 0) + 1
                info["attempts"] = attempts
                info["wait_code"] = code
                dirty = True
                if code == 540:
                    # 无牌谱权限：这次不消耗查询次数，换个账号尽快重试
                    info["next_query_at"] = time.time() + 60
                    query_uid = str(self.current_uid or "")
                    marked = self.mark_no_paipu_permission(query_uid) if query_uid else False
                    # 启用的账号本来都在线，下一轮自动挑别的有权限的账号继续查
                    if not info.get("perm_logged"):
                        info["perm_logged"] = True
                        self._event(
                            "牌谱权限不足",
                            f"{info.get('nickname')} uuid={uuid} 返回 540："
                            f"账号 {self.display_uid(query_uid)} 今天没有牌谱查看权限"
                            f"{'（已标记）' if marked else ''}，"
                            f"下一轮改用其它有权限的账号（次日 05:01 恢复）",
                            "WARN",
                        )
                    continue
                # 失败后的重试间隔：好友/手动的局已经确认结束，按快速退避重试就行；
                # 非好友的局按 T2 轮询（用户要求：T2 固定间隔）
                if friend or manual:
                    if attempts <= QUERY_FAST_RETRY_MAX:
                        delay = QUERY_FAST_RETRY_DELAYS[min(attempts - 1, len(QUERY_FAST_RETRY_DELAYS) - 1)]
                    else:
                        delay = QUERY_SLOW_RETRY_DELAY
                else:
                    delay = int(self.poll_interval)
                info["next_query_at"] = time.time() + delay
                self._event(
                    "等待牌谱",
                    f"{info.get('nickname')} uuid={uuid} 第 {attempts} 次没拉到牌谱（code={code}），"
                    f"{delay}s 后再查",
                    "INFO" if attempts <= 2 else "WARN",
                )
                continue

            # 查到牌谱：播报并从待查列表移除
            self._pending.pop(uuid, None)
            dirty = True
            self._mark_seen(uuid)
            if manual or uuid in self._result_watchers:
                # 页面正在等这局的结果：留一份给它，轮询读取时不再消耗查询次数
                self._keep_manual_result(uuid, head)
            if manual:
                # 手动查询的对局只用于页面展示结果，不参与播报
                self._event("牌谱已生成", f"手动查询 uuid={uuid}", "INFO")
                continue

            # 记一次「今日播报」（好友、非好友都计）；非好友设了上限时在播报末尾提示去加好友
            notice = ""
            for target in self._pending_targets(info):
                aid = int(target.get("account_id") or 0)
                if not aid:
                    continue
                self._bump_broadcast(aid)
                if not self.is_friend_of_any(aid) and self.nonfriend_limit() >= 0:
                    notice = self.nonfriend_notice()
            if notice:
                info["broadcast_notice"] = notice
                info["nonfriend_broadcast"] = True

            self._event("牌谱已生成", f"{info.get('nickname')} uuid={uuid}", "INFO")

            account_info = await self.lookup_account(int(info.get("account_id") or 0))
            if account_info:
                # 三麻对局用三麻段位（level3）
                if int(info.get("mode_id") or 0) in THREE_PLAYER_MODE_IDS and account_info.get("level3_id"):
                    account_info["level_id"] = account_info["level3_id"]
                    account_info["level_score"] = account_info.get("level3_score", 0)
                info.update({k: v for k, v in account_info.items() if v})
            try:
                await self.on_game_finished(head, info)
            except Exception as exc:
                logger.error(f"[MajsoulMonitor] 播报回调失败 uuid={uuid}: {exc}", exc_info=True)
                self._event("播报失败", f"uuid={uuid} {exc.__class__.__name__}: {exc}", "ERROR")

        if dirty:
            self._save_pending()
        if wait_settle > 0 and settle_pass < 1:
            # 刚结束的对局：等牌谱落库（通常几秒）再查，保证一次就能查到
            await asyncio.sleep(max(0.5, min(wait_settle, END_SETTLE_SECONDS)))
            await self._process_pending(settle_pass + 1)

    # ── 手动查询结果（页面轮询读取，不额外消耗查询次数）────
    def watch_result(self, uuid: str) -> None:
        """标记「页面正在等这一局的结果」，拉到牌谱后给页面留一份。"""
        uuid = str(uuid or "")
        if not uuid:
            return
        now = time.time()
        self._result_watchers[uuid] = now
        if len(self._result_watchers) > MANUAL_RESULT_KEEP:
            for key, ts in list(self._result_watchers.items()):
                if now - float(ts or 0) > RESULT_WATCH_RETAIN:
                    self._result_watchers.pop(key, None)

    def is_game_live(self, uuid: str) -> bool:
        """这局是否确定还在进行中（进行中的对局不用浪费一次牌谱查询）。"""
        uuid = str(uuid or "")
        if not uuid:
            return False
        if uuid in self._live_uuids:
            return True
        info = self._pending.get(uuid)
        if info is not None:
            state, _ = self._pending_play_state(info)
            return state == "playing"
        return False

    def _keep_manual_result(self, uuid: str, head: Any) -> None:
        self.manual_results[str(uuid)] = head
        if len(self.manual_results) > MANUAL_RESULT_KEEP:
            for key in list(self.manual_results)[: len(self.manual_results) - MANUAL_RESULT_KEEP]:
                self.manual_results.pop(key, None)

    def take_manual_result(self, uuid: str) -> Optional[Any]:
        """取走手动查询已经拉到的牌谱（没有则返回 None）。"""
        uuid = str(uuid or "")
        self._result_watchers.pop(uuid, None)
        return self.manual_results.pop(uuid, None)

    def has_pending(self, uuid: str) -> bool:
        """这局是否还在待查列表里（页面据此决定要不要继续等）。"""
        return str(uuid or "") in self._pending

    # ── 待查牌谱的玩家筛选 ────────────────────────────────
    @staticmethod
    def _pending_targets(info: Dict[str, Any]) -> List[Dict[str, Any]]:
        targets = info.get("targets")
        if isinstance(targets, list) and targets:
            return [t for t in targets if isinstance(t, dict)]
        return [{
            "account_id": int(info.get("account_id") or 0),
            "nickname": str(info.get("nickname") or ""),
        }]

    def _monitored_index(self) -> tuple:
        ids, names = set(), set()
        for target in self.get_targets():
            pid = int(target.get("account_id") or 0)
            if pid:
                ids.add(pid)
            nickname = str(target.get("nickname") or "").strip()
            if nickname:
                names.add(nickname)
        return ids, names

    @staticmethod
    def _target_in_index(target: Dict[str, Any], ids: set, names: set) -> bool:
        pid = int(target.get("account_id") or 0)
        nickname = str(target.get("nickname") or "").strip()
        return bool((pid and pid in ids) or (nickname and nickname in names))

    def add_manual_pending(
        self,
        uuid: str,
        mode_id: int = 0,
        start_time: int = 0,
        account_id: int = 0,
    ) -> bool:
        """手动查询时把还没结束的对局加入待查列表（只记结果，不播报）。

        account_id 是这局里任意一名玩家：是好友就按好友列表判断结束（每 T1 看观战入口），
        不是好友就等观战列表消失后按 T2 轮询牌谱。
        """
        uuid = str(uuid or "").strip()
        if not uuid or uuid in self._pending or uuid in self._seen:
            return False
        account_id = int(account_id or 0)
        targets = [{"account_id": account_id, "nickname": "手动查询"}] if account_id else []
        self._pending[uuid] = {
            "uuid": uuid,
            "account_id": 0,
            "nickname": "手动查询",
            "mode_id": int(mode_id or 0),
            "start_time": int(start_time or 0),
            "found_at": time.time(),
            "targets": targets,
            "manual": True,
            "friend": self._any_of_friends(targets),
        }
        self._save_pending()
        if account_id:
            self._event(
                "加入待查牌谱",
                f"手动查询 uuid={uuid}（玩家 {account_id}），这局结束后自动获取结果",
                "INFO",
            )
        else:
            self._event("加入待查牌谱", f"手动查询 uuid={uuid}，结束后自动获取结果", "INFO")
        return True

    def drop_pending_for_player(self, account_id: int = 0, nickname: str = "") -> int:
        """玩家被删除后清理待查牌谱：该局还有其他监控玩家时保留，否则停止查询。"""
        account_id = int(account_id or 0)
        nick = str(nickname or "").strip()
        if not account_id and not nick:
            return 0
        dropped = 0
        changed = False
        for uuid, info in list(self._pending.items()):
            targets = self._pending_targets(info)

            def is_removed(target):
                pid = int(target.get("account_id") or 0)
                tn = str(target.get("nickname") or "").strip()
                return bool((account_id and pid == account_id) or (nick and tn == nick))

            remaining = [t for t in targets if not is_removed(t)]
            if len(remaining) == len(targets):
                continue
            changed = True
            if not remaining:
                self._pending.pop(uuid, None)
                dropped += 1
                self._event("停止查询牌谱", f"uuid={uuid} 的监控玩家已删除，不再查询", "INFO")
            else:
                info["targets"] = remaining
                info["account_id"] = int(remaining[0].get("account_id") or 0)
                info["nickname"] = str(remaining[0].get("nickname") or "")
        if changed:
            self._save_pending()
        return dropped

    def _pick_query_session(self, uuid: str = "") -> Optional["AccountSession"]:
        """挑一个在线账号去查牌谱：有牌谱权限、今天还有额度的账号里，选今日查询次数最少的。

        好友的局也一样——不再优先用「这局玩家是它好友」的账号，统一按今日消耗最少来分摊。
        """
        online = self.online_sessions()
        if not online:
            return None
        self._refresh_perm_marks()
        healthy = [
            s for s in online
            if self._perm_is_ok(s.uid) and self.quota.remaining(s.uid) > 0
        ]
        if healthy:
            return min(healthy, key=lambda s: self.quota.used(s.uid))
        permitted = [s for s in online if self._perm_is_ok(s.uid)]
        pool = permitted or online
        return min(pool, key=lambda s: self.quota.used(s.uid))

    def _account_label(self, uid: str) -> str:
        """日志里显示账号的雀魂数字 ID；在线的用会话 label，不在线就现算。"""
        session = self._sessions.get(str(uid))
        if session is not None:
            try:
                return session.label
            except Exception:
                pass
        return self.display_uid(uid)

    def _on_quota_exhausted(self, uid: str) -> None:
        """某个账号当天牌谱查询额度用完时写一条日志（同一账号每天只写一次）。"""
        self._event(
            "额度用尽",
            f"{self._account_label(uid)} 今日牌谱查询已用 "
            f"{self.quota.used(uid)}/{self.quota.limit} 条，额度已用完，之后优先用其它账号查询",
            "WARN",
        )

    async def _fetch_record_with(
        self,
        session: "AccountSession",
        uuid: str,
        quiet: bool = False,
    ) -> Optional[Any]:
        """用指定会话拉一次牌谱（调用方持有 _conn_lock）。"""
        if session.conn is None:
            return None
        res = await session.conn.fetch_game_record(uuid)
        code = int(getattr(res.error, "code", 0) or 0)
        self.last_record_code = code
        if code != 0:
            self.last_error = f"fetchGameRecord code={code}"
            if not quiet:
                self._event("拉取牌谱失败", f"{session.label} uuid={uuid} code={code}", "WARN")
            # 540 是无牌谱权限，不算一次查询；其它失败仍算查了一次。
            if code != 540:
                self.quota.consume(session.uid)
            return None
        self.quota.consume(session.uid)
        return res.head

    async def fetch_record(
        self,
        uuid: str,
        quiet: bool = False,
        session: Optional["AccountSession"] = None,
    ) -> Optional[Any]:
        """用合适的账号拉牌谱：有牌谱权限的在线账号里，挑今天查得最少的那个（好友的局也一样）。

        quiet=True 时不记录“牌谱还没生成”这类中间失败（轮询等待时会用到）。
        成功或非 540 失败会消耗一次额度；540（无权限）和请求异常不计数。
        """
        async with self._conn_lock:
            self.last_record_code = 0
            try:
                if not self.online_sessions():
                    await self._ensure_sessions_locked()
                target = session or self._pick_query_session(uuid)
                if target is None or target.conn is None:
                    return None
                return await self._fetch_record_with(target, uuid, quiet=quiet)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # -1 = 请求异常（不计额度），调用方据此不要当成“没权限”
                self.last_record_code = -1
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                if not quiet:
                    self._event("拉取牌谱异常", f"uuid={uuid} {self.last_error}", "WARN")
                uid = (session.uid if session else "") or self.current_uid
                if uid:
                    await self._close_session_locked(uid)
                return None

    async def recent_player_records(self, account_id: int, limit: int = 10):
        """用登录连接查任意玩家的最近对局顺位，返回 (ranks, latest_ts) 或 None。

        优先 fetchAccountInfoExtra（带每局时间），只取四麻（一局 4 个结果），
        失败再用 fetchAccountStatisticInfo 的 recent_10_game_result 兜底；
        只有查询对象正好是当前登录账号时，才回退到自己的牌谱列表。
        """
        self.last_recent_error = ""
        try:
            if not self.connected or not self.conn:
                self.last_recent_error = "监控未连接"
                return None
            account_id = int(account_id or 0)
            if account_id <= 0:
                self.last_recent_error = "账号 ID 无效"
                return None

            def _rank_of(game) -> int:
                rank = int(getattr(game, "rank", 0) or 0)
                if rank > 0:
                    return rank
                for result in list(getattr(game, "results", []) or []):
                    if int(getattr(result, "account_id", 0) or 0) == account_id:
                        return int(getattr(result, "rank", 0) or 0)
                return 0

            # 1) 详细对局：有顺位也有开始时间
            errors: List[str] = []
            for game_type in (0,):
                try:
                    extra = await asyncio.wait_for(
                        self.conn.fetch_account_info_extra(
                            account_id, category=1, game_type=game_type
                        ),
                        timeout=2.5,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    errors.append(f"extra{game_type}:{exc.__class__.__name__}")
                    continue
                code = int(getattr(getattr(extra, "error", None), "code", 0) or 0)
                if code != 0:
                    errors.append(f"extra{game_type}:code={code}")
                    continue
                games = list(getattr(extra, "recent_games", []) or [])
                if not games:
                    errors.append(f"extra{game_type}:empty")
                ranks: List[int] = []
                latest_ts = 0
                for game in games:
                    results = list(getattr(game, "results", []) or [])
                    # 最近对局是四麻/三麻混排的；四麻一局固定 4 个结果，
                    # 三麻只有 3 个，按结果数过滤，避免把三麻顺位混进来。
                    if len(results) != 4:
                        continue
                    rank = _rank_of(game)
                    if rank <= 0:
                        continue
                    ranks.append(rank)
                    try:
                        ts = int(getattr(game, "start_time", 0) or 0)
                    except (TypeError, ValueError):
                        ts = 0
                    if ts > latest_ts:
                        latest_ts = ts
                    if len(ranks) >= limit:
                        break
                if ranks:
                    return ranks, latest_ts

            # 2) 统计接口里的最近 10 局顺位（没有时间）
            try:
                stat = await asyncio.wait_for(
                    self.conn.fetch_account_statistic_info(account_id), timeout=3
                )
                code = int(getattr(getattr(stat, "error", None), "code", 0) or 0)
                if code == 0:
                    for data in list(getattr(stat, "statistic_data", []) or []):
                        if int(getattr(data, "mahjong_category", 0) or 0) != 1:
                            continue
                        rows = list(
                            getattr(getattr(data, "statistic", None), "recent_10_game_result", [])
                            or []
                        )
                        ranks = [
                            int(getattr(row, "rank", 0) or 0)
                            for row in rows
                            if int(getattr(row, "rank", 0) or 0) > 0
                        ]
                        if ranks:
                            return ranks[:limit], 0
                    errors.append("stat:empty")
                else:
                    errors.append(f"stat:code={code}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.append(f"stat:{exc.__class__.__name__}")

            # 3) 查询对象就是当前登录账号时，回退到自己的牌谱列表
            if int(getattr(self.conn, "account_id", 0) or 0) != account_id:
                self.last_recent_error = "; ".join(errors) + "; not_self"
                return None
            entries = await asyncio.wait_for(
                self.conn.fetch_own_recent_entries(max(limit * 2, 20)), timeout=3
            )
            ranks = []
            latest_ts = 0
            for entry in entries:
                players = list(getattr(entry, "players", []) or [])
                if len(players) < 4:
                    continue
                target = None
                for player in players:
                    if int(getattr(player, "account_id", 0) or 0) == account_id:
                        target = player
                        break
                if target is None:
                    continue
                rank = int(getattr(target, "rank", 0) or 0)
                if rank <= 0:
                    continue
                ranks.append(rank)
                try:
                    ts = int(getattr(entry, "start_time", 0) or 0)
                except (TypeError, ValueError):
                    ts = 0
                if ts > latest_ts:
                    latest_ts = ts
                if len(ranks) >= limit:
                    break
            if not ranks:
                errors.append("own:empty")
                self.last_recent_error = "; ".join(errors)
                return None
            self.last_recent_error = ""
            return ranks, latest_ts
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            self.last_recent_error = f"{exc.__class__.__name__}: {exc}"
            return None

    async def recent_own_records(self, account_id: int, limit: int = 10):
        """兼容旧调用：查当前登录账号自己的最近顺位。"""
        return await self.recent_player_records(account_id, limit)

    # ── 玩家解析 ──────────────────────────────────────────
    async def ensure_connected(self) -> bool:
        if not self.connected:
            try:
                await self._ensure_connection()
            except Exception as exc:
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                return False
        return self.connected

    async def list_live_games(self, filter_id: int = 216, include_old: bool = True) -> Dict[str, Any]:
        """进行中的对局列表（观战列表，filter_id = 200 + mode_id）。

        服务端每个筛选最多返回 100 局；勾选「包含较早对局」时把缓存文件里的同房间对局
        一起返回，并标记 live=False。
        """
        if not await self.ensure_connected() or self.conn is None:
            raise RuntimeError("雀魂连接未就绪")
        games = await self.conn.fetch_live_games(filter_id)
        self._remember_live_games(games)
        if include_old:
            merged = self._merge_live_cache(int(filter_id) - 200, games)
        else:
            merged = []
            for game in games:
                item = dict(game)
                item["live"] = True
                merged.append(item)
            merged.sort(key=lambda g: int(g.get("start_time") or 0), reverse=True)
        message = ""
        if len(merged) > LIVE_LIST_MAX:
            merged = merged[:LIVE_LIST_MAX]
            message = f"对局较多，仅显示最近 {LIVE_LIST_MAX} 局"
        return {"games": merged, "source": "live", "message": message}

    async def fetch_paipu_log(self, game_id: str) -> Optional[Dict[str, Any]]:
        """拉取整局牌谱的 action 日志（天凤格式），用于回放/和牌详情。

        复用当前登录连接，不额外登录账号；牌谱会缓存在 data_dir/review/paipu 下。
        传入的 game_id 可以带 `_a<id>` 后缀，这里统一用对局 uuid 做缓存 key。
        """
        game_id = str(game_id or "").strip()
        if not game_id:
            return None
        # 牌谱链接里的 paipu 可能是 <uuid>_a<encoded>，缓存统一按 uuid 存
        game_id = game_id.split("_")[0] or game_id
        self.last_paipu_error = ""
        cache_file = self.data_dir / "review" / "paipu" / f"{game_id} - raw.json"
        self.last_paipu_cache_hit = cache_file.exists()
        async with self._conn_lock:
            session: Optional["AccountSession"] = None
            try:
                if not self.online_sessions():
                    # 已持有 _conn_lock，这里必须调用 _ensure_sessions_locked，不能再走 _ensure_connection
                    await self._ensure_sessions_locked()
                session = self._pick_query_session(game_id)
                conn = session.conn if session is not None else None
                if conn is None:
                    self.last_paipu_error = "雀魂连接未就绪"
                    return None
                raw = await conn.fetch_logs(
                    game_id, self.data_dir / "review" / "paipu"
                )
                if raw and not self.last_paipu_cache_hit:
                    # 只有实际从服务器拉取时，才计入当天的牌谱数量；命中缓存不计数
                    self.quota.consume(session.uid)
                return raw
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = str(exc)
                self.last_paipu_error = message
                self.last_error = f"fetch_logs: {exc.__class__.__name__}: {message}"
                match = re.search(r"code=(\d+)", message)
                code = int(match.group(1)) if match else 0
                if code == 540:
                    uid = str(session.uid if session is not None else "") or str(self.current_uid or "")
                    marked = self.mark_no_paipu_permission(uid) if uid else False
                    if marked:
                        self.last_paipu_error = (
                            f"当前账号 {self.display_uid(uid)} 没有牌谱查看权限（code=540），已标记为「无权限」"
                        )
                    else:
                        self.last_paipu_error = (
                            f"当前账号 {self.display_uid(uid)} 没有牌谱查看权限（code=540），"
                            f"该账号已手动恢复，保持「有」权限"
                        )
                logger.warning(f"[MajsoulMonitor] 拉取牌谱回放失败 {game_id}: {self.last_paipu_error}")
                return None

    async def lookup_account(self, account_id: int) -> Optional[Dict[str, Any]]:
        """查询任意账号的基础资料（昵称/段位），不需要好友关系。"""
        account_id = int(account_id or 0)
        if account_id <= 0:
            return None
        if not self.connected:
            try:
                await self._ensure_connection()
            except Exception as exc:
                logger.debug(f"[MajsoulMonitor] lookup_account 连接失败: {exc}")
                return None
        if not self.conn:
            return None
        try:
            res = await self.conn.rpc_call(".lq.Lobby.fetchAccountInfo", {"account_id": account_id})
        except Exception as exc:
            logger.debug(f"[MajsoulMonitor] fetchAccountInfo 失败 {account_id}: {exc}")
            return None
        if int(getattr(res.error, "code", 0) or 0) != 0:
            return None
        account = getattr(res, "account", None)
        real_id = int(getattr(account, "account_id", 0) or 0)
        # 注意：fetchAccountInfo(account_id=0) 会返回登录账号本身，账号不存在时也可能返回空账号，
        # 这里必须校验返回的 account_id，避免把登录账号或空账号当成目标玩家加进监控列表。
        if real_id != account_id:
            logger.warning(
                f"[MajsoulMonitor] fetchAccountInfo 返回账号不匹配："
                f"请求 {account_id}，返回 {real_id}（{getattr(account, 'nickname', '')!r}），已忽略"
            )
            return None
        level = getattr(account, "level", None)
        level3 = getattr(account, "level3", None)
        return {
            "account_id": real_id,
            "nickname": str(getattr(account, "nickname", "") or ""),
            "level_id": int(getattr(level, "id", 0) or 0),
            "level_score": int(getattr(level, "score", 0) or 0),
            "level3_id": int(getattr(level3, "id", 0) or 0),
            "level3_score": int(getattr(level3, "score", 0) or 0),
        }

    async def _search_account_id_in_game(self, nickname: str) -> Optional[int]:
        """在当前登录区服内按昵称搜索，返回昵称完全一致的 account_id。"""
        if not self.conn:
            self.last_resolve_error = "监控未连接，无法使用游戏内搜索"
            return None
        try:
            ids = await asyncio.wait_for(
                self.conn.search_account_by_pattern(nickname), timeout=8
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_resolve_error = (
                f"游戏内搜索失败（{exc.__class__.__name__}: {exc}）"
            )
            logger.warning(f"[MajsoulMonitor] 游戏内昵称搜索失败 {nickname!r}: {exc}")
            return None
        exact = str(nickname or "").strip()
        for account_id in ids[:20]:
            info = await self.lookup_account(int(account_id))
            if info and str(info.get("nickname") or "").strip() == exact:
                logger.info(
                    f"[MajsoulMonitor] 游戏内搜索 {nickname!r} -> {info['account_id']}"
                )
                return int(info["account_id"])
        self.last_resolve_error = f"游戏内搜索返回 {len(ids)} 个候选，没有一个昵称完全一致"
        logger.warning(
            f"[MajsoulMonitor] 游戏内搜索 {nickname!r} 没匹配到完全一致的昵称"
            f"（候选 {len(ids)} 个）"
        )
        return None

    async def lookup_by_display_id(self, display_id: int) -> Optional[Dict[str, Any]]:
        """按雀魂里显示的数字 ID 查玩家资料（网页/指令添加玩家用）。"""
        self.last_resolve_error = ""
        try:
            number = int(display_id)
        except (TypeError, ValueError):
            self.last_resolve_error = f"雀魂ID {display_id!r} 不是数字"
            return None
        try:
            account_id = int(decode_account_id(number))
        except (TypeError, ValueError):
            account_id = 0
        if account_id <= 0:
            self.last_resolve_error = f"雀魂ID {number} 无法换算成账号"
            return None
        info = await self.lookup_account(account_id)
        if info is None:
            self.last_resolve_error = f"雀魂ID {number} 对应的账号不存在（或不是当前区服）"
            logger.warning(
                f"[MajsoulMonitor] 雀魂ID {number} -> account_id {account_id} 查询失败"
            )
        return info

    async def resolve_player(self, key: str, allow_id: bool = True) -> Optional[Dict[str, Any]]:
        """把昵称（或 account_id）解析成账号资料。

        - allow_id=True 且是纯数字：当作 account_id 查询（网页添加用）
        - 其它情况：先按当前区服的游戏内搜索，搜不到再退回牌谱屋按昵称精确匹配
          （游戏内搜索接口偶尔报错/对方不允许被搜索，退回牌谱屋才不至于直接说「没查到」）
        """
        key = (key or "").strip()
        self.last_resolve_error = ""
        if not key:
            return None
        if allow_id and key.isdigit():
            number = int(key)
            info = await self.lookup_account(number)
            if info is None:
                # 也可能是雀魂里显示的数字 ID：换算回 account_id 再试一次
                try:
                    decoded = int(decode_account_id(number))
                except (TypeError, ValueError):
                    decoded = 0
                if decoded and decoded != number:
                    info = await self.lookup_account(decoded)
            if info is None:
                logger.warning(
                    f"[MajsoulMonitor] 按 ID {key} 解析失败："
                    f"账号不存在，或接口返回的不是这个账号"
                )
                self.last_resolve_error = f"账号 {key} 不存在"
            return info
        account_id = await self._search_account_id_in_game(key)
        from_amae = False
        if not account_id:
            # 游戏内搜索没结果：退回牌谱屋按昵称精确匹配，再让 fetchAccountInfo 复核
            game_error = self.last_resolve_error
            account_id = await self._search_account_id_by_nickname(key)
            from_amae = bool(account_id)
            if not account_id:
                amae_error = self.last_resolve_error
                # 两个原因都留着，命令里一眼能看出是哪一步没查到
                self.last_resolve_error = "；".join(
                    x for x in (game_error, amae_error) if x
                ) or "游戏内搜索和牌谱屋都没查到"
                return None
        info = await self.lookup_account(int(account_id))
        if info is not None:
            if str(info.get("nickname") or "").strip() != key.strip():
                logger.warning(
                    f"[MajsoulMonitor] 昵称 {key!r} 解析到 {account_id}，"
                    f"但当前区服账号昵称是 {info.get('nickname')!r}，已忽略"
                )
                self.last_resolve_error = f"ID {account_id} 在当前区服的昵称与「{key}」不一致"
                return None
            return info
        if from_amae:
            # 牌谱屋是按昵称精确匹配到的，查资料本身走的是牌谱屋接口，
            # 这里不需要游戏内资料也能继续（否则接口一抖就整个查询失败）
            logger.warning(
                f"[MajsoulMonitor] 昵称 {key!r} 由牌谱屋解析为 {account_id}，"
                f"但 fetchAccountInfo 校验失败，按牌谱屋结果继续"
            )
            return {
                "account_id": int(account_id),
                "nickname": key.strip(),
                "level_id": 0,
                "level_score": 0,
            }
        self.last_resolve_error = self.last_resolve_error or "查不到该账号的资料"
        logger.warning(
            f"[MajsoulMonitor] 游戏内搜索 {key!r} 得到 {account_id}，"
            f"但 fetchAccountInfo 校验失败，已忽略"
        )
        return None

    async def _search_account_id_by_nickname(self, nickname: str) -> Optional[int]:
        """用牌谱屋搜索昵称拿到 account_id（四麻 pl4 和三麻 pl3 都查）。

        各节点并发请求：一串节点里总有几个连不上的，顺序等会把昵称解析拖到超时。
        """
        quoted = urllib.parse.quote(nickname, safe="")
        urls = []
        for template in self.search_urls:
            for mode in AMAE_SEARCH_MODES:
                try:
                    url = template.format(mode=mode, name=quoted)
                except KeyError:
                    url = template.format(name=quoted)
                if url not in urls:
                    urls.append(url)
        if not urls:
            self.last_resolve_error = "没有配置牌谱屋节点"
            return None

        async def _fetch(url: str):
            try:
                resp = await asyncio.wait_for(HTTPX_CLIENT.get(url), timeout=6)
                resp.raise_for_status()
                data = resp.json()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return url, None, exc.__class__.__name__
            if not isinstance(data, list):
                return url, None, "返回格式异常"
            return url, data, ""

        results = await asyncio.gather(*[_fetch(u) for u in urls], return_exceptions=True)
        errors = []
        for item in results:
            if isinstance(item, BaseException):
                errors.append(str(item))
                continue
            url, data, err = item
            host = urllib.parse.urlparse(url).netloc or url
            mode = "pl3" if "/pl3/" in url else "pl4"
            if err:
                errors.append(f"{host}/{mode}: {err}")
                logger.debug(f"[MajsoulMonitor] 昵称搜索节点 {host}/{mode} 失败: {err}")
                continue
            for entry in data or []:
                # 昵称区分大小写：Hikaru 和 HIkaru 是两个不同玩家
                if str(entry.get("nickname", "")) == nickname:
                    try:
                        account_id = int(entry.get("id"))
                    except (TypeError, ValueError):
                        continue
                    logger.info(
                        f"[MajsoulMonitor] 昵称 {nickname} 通过 {host}/{mode} 解析为 {account_id}"
                    )
                    return account_id
            errors.append(f"{host}/{mode}: 未找到该昵称")
        self.last_resolve_error = f"牌谱屋 {len(urls)} 个节点都查不到「{nickname}」"
        logger.warning(
            f"[MajsoulMonitor] 昵称解析失败 {nickname}（所有节点/模式）: {'; '.join(errors)}"
        )
        return None

    def status(self) -> Dict[str, Any]:
        targets = [t for t in self.get_targets() if int(t.get("account_id") or 0)]
        return {
            "connected": self.connected,
            "reconnecting": self._reconnecting,
            "uid": self.current_uid,
            "targets": len(targets),
            "playing": [
                {
                    "uuid": game.get("uuid", ""),
                    "mode_id": game.get("mode_id", 0),
                    "players": [p.get("nickname", "") for p in game.get("players", [])],
                }
                for game in self._live_snapshot
            ],
            "pending": [
                {
                    "uuid": uuid,
                    "nickname": info.get("nickname", ""),
                    "mode_id": info.get("mode_id", 0),
                    "ended": bool(info.get("ended_at")),
                    "attempts": int(info.get("attempts") or 0),
                    "manual": bool(info.get("manual")),
                    "friend": self._pending_branch(info) == "friend",
                }
                for uuid, info in self._pending.items()
            ],
            "quota_used": self.quota.used(self.current_uid) if self.current_uid else 0,
            "quota_limit": self.quota.limit,
            "poll_interval": self.poll_interval,
            "friend_poll_interval": self.friend_poll_interval,
            "scan_interval": self.scan_interval,
            "last_error": self.last_error,
            "nonfriend_daily_limit": self.nonfriend_limit(),
            "sessions": [
                {
                    "uid": s.uid,
                    "nickname": s.nickname,
                    "online": s.is_online(),
                    "primary": s.uid == self.current_uid,
                    "friends": len(s.friends),
                    "friends_at": s.friends_at,
                    "quota_used": self.quota.used(s.uid),
                    "paipu_permission": self.paipu_permission(s.uid),
                    "last_error": s.last_error,
                }
                for s in self._sessions.values()
            ],
        }

    def broadcast_snapshot(self) -> Dict[str, Any]:
        """今日播报次数（监控列表的「今日播报」列用）。"""
        day = _today()
        return {
            "day": day,
            "limit": self.nonfriend_limit(),
            "counts": {
                str(k): int(v) for k, v in (self._broadcast.get(day) or {}).items()
            },
        }

    def friends_snapshot(self) -> List[Dict[str, Any]]:
        """「好友列表」页用：每个在线账号及其好友。"""
        out: List[Dict[str, Any]] = []
        for session in self._sessions.values():
            friends = sorted(session.friends.values(), key=self._friend_sort_key)
            out.append(
                {
                    "uid": session.uid,
                    "nickname": session.nickname,
                    "online": session.is_online(),
                    "primary": session.uid == self.current_uid,
                    "friends_at": session.friends_at,
                    "count": len(friends),
                    "friends": [
                        {
                            "account_id": int(f.get("account_id") or 0),
                            "nickname": str(f.get("nickname") or ""),
                            "online": bool(f.get("online")),
                            "game_uuid": str(f.get("game_uuid") or ""),
                            "mode_id": int(f.get("mode_id") or 0),
                            "level_id": int(f.get("level_id") or 0),
                            "level_score": int(f.get("level_score") or 0),
                            "level3_id": int(f.get("level3_id") or 0),
                            "level3_score": int(f.get("level3_score") or 0),
                            "last_online": int(f.get("last_online") or 0),
                        }
                        for f in friends
                    ],
                }
            )
        return out

    async def remove_friend(self, account_id: int) -> Tuple[bool, str]:
        """删除好友（好友列表页的删除按钮）：用「正把他当好友」的那个在线账号去删。"""
        aid = int(account_id or 0)
        if not aid:
            return False, "玩家 ID 无效"
        target = None
        for session in list(self._sessions.values()):
            if session.conn is None or aid not in session.friends:
                continue
            target = session
            if session.is_online():
                break
        if target is None or target.conn is None:
            return False, "没有在线账号把他当好友（可能好友列表还没刷新）"
        try:
            await asyncio.wait_for(target.conn.remove_friend(aid), timeout=STATE_RPC_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._event(
                "删除好友",
                f"{target.label} 删除好友 {self.display_uid(aid)} 失败："
                f"{exc.__class__.__name__}: {exc}",
                "WARN",
            )
            return False, f"删除失败：{exc.__class__.__name__}"
        target.friends.pop(aid, None)
        self._friend_states = self._friend_union()
        self._event("删除好友", f"{target.label} 已删除好友 {self.display_uid(aid)}", "INFO")
        return True, "已删除该好友"

    # ── 已播报记录 ────────────────────────────────────────
    def _load_seen(self) -> Dict[str, float]:
        if not self.seen_path.exists():
            return {}
        try:
            raw = json.loads(self.seen_path.read_text(encoding="utf-8-sig") or "{}")
            now = time.time()
            return {k: float(v) for k, v in raw.items() if now - float(v) < SEEN_RETAIN_SECONDS}
        except Exception:
            return {}

    # ── 待查牌谱（持久化，重载后继续查）─────────────────
    def _load_pending(self) -> Dict[str, Dict[str, Any]]:
        if not self.pending_path.exists():
            return {}
        try:
            raw = json.loads(self.pending_path.read_text(encoding="utf-8-sig") or "{}")
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 读取待查牌谱失败: {exc}")
            return {}
        if not isinstance(raw, dict):
            return {}
        pending = {}
        majsoul_ids = self._ids_majsoul()
        for uuid, info in raw.items():
            if not isinstance(info, dict) or not uuid:
                continue
            item = dict(info)
            item.setdefault("uuid", uuid)
            item.setdefault("nickname", "")
            item.setdefault("account_id", 0)
            item.setdefault("mode_id", 0)
            item.setdefault("start_time", 0)
            item.setdefault("found_at", time.time())
            if not isinstance(item.get("targets"), list) or not item.get("targets"):
                item["targets"] = [{
                    "account_id": int(item.get("account_id") or 0),
                    "nickname": str(item.get("nickname") or ""),
                }]
            if majsoul_ids:
                # 存档里存的是雀魂 ID，内存里统一用 account_id
                try:
                    item["account_id"] = int(to_account_id(int(item.get("account_id") or 0)))
                except (TypeError, ValueError):
                    pass
                for target in item["targets"]:
                    if not isinstance(target, dict):
                        continue
                    try:
                        target["account_id"] = int(to_account_id(int(target.get("account_id") or 0)))
                    except (TypeError, ValueError):
                        continue
            pending[uuid] = item
        return pending

    def _save_pending(self) -> None:
        try:
            self.pending_path.parent.mkdir(parents=True, exist_ok=True)
            payload = self._pending
            if self._ids_majsoul():
                payload = {}
                for uuid, info in self._pending.items():
                    copy = dict(info)
                    try:
                        copy["account_id"] = to_majsoul(int(info.get("account_id") or 0))
                    except (TypeError, ValueError):
                        pass
                    targets = info.get("targets")
                    if isinstance(targets, list):
                        new_targets = []
                        for target in targets:
                            if not isinstance(target, dict):
                                new_targets.append(target)
                                continue
                            target_copy = dict(target)
                            try:
                                target_copy["account_id"] = to_majsoul(
                                    int(target.get("account_id") or 0)
                                )
                            except (TypeError, ValueError):
                                pass
                            new_targets.append(target_copy)
                        copy["targets"] = new_targets
                    payload[uuid] = copy
            self.pending_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 保存待查牌谱失败: {exc}")

    def _ids_majsoul(self) -> bool:
        """存档里的 ID 是不是已经是雀魂 ID（修正过一次之后就都是）。"""
        try:
            return ids_are_majsoul(self.data_dir)
        except Exception:
            return False

    def _mark_seen(self, uuid: str) -> None:
        self._seen[uuid] = time.time()
        try:
            self.seen_path.parent.mkdir(parents=True, exist_ok=True)
            self.seen_path.write_text(json.dumps(self._seen, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.debug(f"[MajsoulMonitor] 保存已播报记录失败: {exc}")
