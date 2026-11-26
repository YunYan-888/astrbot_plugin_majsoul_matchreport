import asyncio
import contextlib
import hashlib
import hmac
import inspect
import json
import random
import re
import time
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional, cast

import aiofiles
import websockets.client
from astrbot.api import logger
from msgspec import ValidationError, convert

from .codec import MajsoulProtoCodec
from .constants import URL_BASE, USER_AGENT
from .lib import lq as liblq
from .model import (
    MjsLog,
    MjsLogItem,
    MajsoulConfig,
    MajsoulDecodedMessage,
    MajsoulLiqiProto,
    MajsoulResInfo,
    MajsoulVersionInfo,
)
from .remote import decode_account_id2, decode_log_id
from .tenhou.parser import MajsoulPaipuParser
from .utils import HTTPX_CLIENT, get_res

WS_OPEN_TIMEOUT_SEC = 12
RPC_TIMEOUT_SEC = 20
CLIENT_TAG = "cn"
DEFAULT_CURRENCY_PLATFORMS = [2]
# 三麻模式：段位要用 level3（三麻段位），四麻模式用 level
THREE_PLAYER_MODE_IDS = {17, 18, 19, 20, 21, 22, 23, 24, 25, 26}
UNITY_WEB_VERSION_RE = re.compile(r"WebGL-release-([0-9]+(?:\.[0-9]+)+)")
UNITY_PRODUCT_VERSION_RE = re.compile(r"productVersion\s*:\s*[\"']([^\"']+)[\"']")
ProgressCallback = Callable[[str], Optional[Awaitable[None]]]


async def _notify(progress_cb: Optional[ProgressCallback], message: str) -> None:
    logger.info(f"[majsoul-review] {message}")
    if progress_cb is None:
        return
    result = progress_cb(message)
    if inspect.isawaitable(result):
        await result


def _error_summary(error) -> str:
    if error is None:
        return "none"
    return (
        f"code={getattr(error, 'code', 0)} "
        f"u32={list(getattr(error, 'u32_params', []) or [])} "
        f"str={list(getattr(error, 'str_params', []) or [])} "
        f"json={getattr(error, 'json_param', '')!r} "
        f"level={getattr(error, 'level', 0)}"
    )


def _client_device_info() -> dict:
    return {
        "platform": "pc",
        "hardware": "pc",
        "os": "windows",
        "os_version": "win10",
        "is_browser": True,
        "software": "Chrome",
        "sale_platform": "web",
        "screen_width": 1920,
        "screen_height": 1080,
        "user_agent": USER_AGENT,
        "screen_type": 1,
    }


def _legacy_client_version_string(version_info: MajsoulVersionInfo) -> str:
    return "web-" + version_info.version.replace(".w", "")


async def _fetch_login_client_version_string(
    url_base: str,
    version_info: MajsoulVersionInfo,
) -> tuple[str, str]:
    legacy_version = _legacy_client_version_string(version_info)
    index_url = f"{url_base.rstrip('/')}/1/"
    try:
        HTTPX_CLIENT.headers["Referer"] = url_base
        resp = await HTTPX_CLIENT.get(index_url)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        logger.debug(
            "[majsoul-review] web client version fetch failed: "
            f"url={index_url} fallback={legacy_version} error={exc.__class__.__name__}: {exc}"
        )
        return legacy_version, "version_json"

    product_match = UNITY_PRODUCT_VERSION_RE.search(html)
    build_match = UNITY_WEB_VERSION_RE.search(html)
    unity_version = ""
    if product_match and product_match.group(1).strip():
        unity_version = product_match.group(1).strip()
    elif build_match and build_match.group(1).strip():
        unity_version = build_match.group(1).strip()
    if not unity_version:
        logger.debug(
            "[majsoul-review] web client version not found in index: "
            f"url={index_url} fallback={legacy_version}"
        )
        return legacy_version, "version_json"

    return f"web-{unity_version}", "unity_index"


def _seat_index(obj, default: int = -1) -> int:
    """安全取 seat。注意 seat=0 也是合法座位，不能用 `x or default`。"""
    value = getattr(obj, "seat", None)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _degraded_tenhou_log(head, exc: Optional[BaseException] = None) -> Dict:
    """牌谱解析失败时的兜底格式：只有名次/段位等头部信息，没有逐局 action。

    和牌/放铳/役满这些依赖 action 的内容会缺失，但牌谱文件仍会落到 review/paipu 下，
    播报名次、段位、牌谱链接都正常。
    """
    players = list(getattr(getattr(head, "result", None), "players", []) or [])
    nplayers = len(players) or 4
    accounts = list(getattr(head, "accounts", []) or [])
    names = ["AI"] * nplayers
    dan = [""] * nplayers
    rate = [0] * nplayers
    sx = ["C"] * nplayers
    for acc in accounts:
        seat = _seat_index(acc)
        if 0 <= seat < nplayers:
            names[seat] = str(getattr(acc, "nickname", "") or "AI")
            rate[seat] = int(getattr(getattr(acc, "level", None), "score", 0) or 0)
            dan[seat] = _level_full_name_safe(getattr(getattr(acc, "level", None), "id", 0))
    sc = [0.0] * (nplayers * 2)
    for player in players:
        seat = _seat_index(player)
        if 0 <= seat < nplayers:
            sc[2 * seat] = int(getattr(player, "part_point_1", 0) or 0)
            sc[2 * seat + 1] = int(getattr(player, "total_point", 0) or 0) / 1000
    meta = getattr(getattr(head, "config", None), "meta", None)
    mode_id = int(getattr(meta, "mode_id", 0) or 0)
    rule = {"disp": _mode_room_name_safe(mode_id) if mode_id else "", "aka53": 0, "aka52": 0, "aka51": 0}
    return {
        "ref": str(getattr(head, "uuid", "") or ""),
        "ver": "2.3",
        "ratingc": f"PF{nplayers}",
        "rule": rule,
        "lobby": 0,
        "dan": dan,
        "rate": rate,
        "sx": sx,
        "name": names,
        "sc": sc,
        "title": ["", ""],
        "log": [],
        "_parse_error": f"{exc.__class__.__name__}: {exc}" if exc is not None else "",
    }


def _mode_room_name_safe(mode_id) -> str:
    try:
        from .tenhou.parser import _mode_room_name

        return _mode_room_name(mode_id)
    except Exception:  # noqa: BLE001
        return f"mode{int(mode_id or 0)}"


def _level_full_name_safe(level_id) -> str:
    try:
        from .tenhou.parser import _level_full_name

        return _level_full_name(level_id)
    except Exception:  # noqa: BLE001
        return ""


def process_dict(obj):
    if is_dataclass(obj):
        return {k: process_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: process_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [process_dict(i) for i in obj]
    if hasattr(obj, "__dict__"):
        return process_dict(obj.__dict__)
    if isinstance(obj, bytes):
        return str(obj)
    if isinstance(obj, (int, float, str, bool, type(None))):
        return obj
    return str(obj)


class MajsoulConnection:
    def __init__(
        self,
        endpoint: str,
        codec: MajsoulProtoCodec,
        version_info: MajsoulVersionInfo,
        client_version_string: str = "",
        origin: str = URL_BASE,
    ):
        self._endpoint = endpoint
        self._origin = str(origin or URL_BASE).rstrip("/")
        self._codec = codec
        self._ws = None
        self._dispatch_task: asyncio.Task | None = None
        self._dispatch_error: Exception | None = None
        self._req_events: dict[int, asyncio.Event] = {}
        self._res: dict[int, MajsoulDecodedMessage] = {}
        # 0x01 通知（服务端主动推送）处理器：{(完整方法名|短名): [handler]}
        self._notify_handlers: dict[str, list[Callable]] = {}

        self.client_version_string = (
            client_version_string or _legacy_client_version_string(version_info)
        )
        self.client_tag = CLIENT_TAG
        self.currency_platforms = list(DEFAULT_CURRENCY_PLATFORMS)
        self.random_key = str(uuid.uuid4())

        self.account_id = 0
        self.nick_name = ""
        self.access_token = ""
        # 好友上限（服务端 fetchFriendList 返回的 friend_max_count），0 = 还不知道
        self.friend_max_count = 0

    async def connect(self):
        logger.debug(
            "[majsoul-review] websocket connect meta: "
            f"endpoint={self._endpoint} origin={self._origin} "
            f"user_agent_set={bool(USER_AGENT)}"
        )
        self._ws = await websockets.client.connect(
            self._endpoint,
            origin=self._origin,
            user_agent_header=USER_AGENT,
            open_timeout=WS_OPEN_TIMEOUT_SEC,
            close_timeout=5,
        )
        self._dispatch_task = asyncio.create_task(self.dispatch_msg())

    async def close(self):
        if self._dispatch_task:
            self._dispatch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._dispatch_task
        if self._ws:
            await self._ws.close()

    # ── 服务端推送（0x01）────────────────────────────────
    def add_notify_handler(self, name: str, handler: Callable) -> None:
        """注册通知处理器。name 可以是 `.lq.NotifyXxx` 或 `NotifyXxx`。"""
        key = name.rsplit(".", 1)[-1]
        self._notify_handlers.setdefault(key, []).append(handler)

    async def _dispatch_notify(self, data: MajsoulDecodedMessage) -> None:
        key = data.method_name.rsplit(".", 1)[-1]
        for handler in list(self._notify_handlers.get(key, [])):
            try:
                result = handler(data.payload)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.debug(f"[majsoul-review] 通知处理器异常 {key}: {exc}")

    # ── 好友 / 牌谱 ──────────────────────────────────────
    async def fetch_friend_list(self) -> list[dict]:
        """拉好友列表（含是否在对局中、对局 uuid）。"""
        res = await self.rpc_call(".lq.Lobby.fetchFriendList", {})
        if getattr(res.error, "code", 0):
            raise ValueError(f"fetchFriendList failed: {_error_summary(res.error)}")
        # 好友上限跟着列表一起返回，存下来给「好友 22/50」和「加好友提示只列未满的账号」用
        try:
            self.friend_max_count = int(getattr(res, "friend_max_count", 0) or 0)
        except (TypeError, ValueError):
            self.friend_max_count = 0
        friends = []
        for friend in getattr(res, "friends", []) or []:
            base = getattr(friend, "base", None)
            state = getattr(friend, "state", None)
            playing = getattr(state, "playing", None) if state is not None else None
            meta = getattr(playing, "meta", None) if playing is not None else None
            level = getattr(base, "level", None) if base is not None else None
            level3 = getattr(base, "level3", None) if base is not None else None
            last_online = 0
            if state is not None:
                # logout_time 是「这次下线的时间」，login_time 是没有下线记录时（比如从没上过线）的兜底
                last_online = int(getattr(state, "logout_time", 0) or 0) or int(
                    getattr(state, "login_time", 0) or 0
                )
            friends.append(
                {
                    "account_id": int(getattr(base, "account_id", 0) or 0),
                    "nickname": str(getattr(base, "nickname", "") or ""),
                    "level_id": int(getattr(level, "id", 0) or 0),
                    "level_score": int(getattr(level, "score", 0) or 0),
                    "level3_id": int(getattr(level3, "id", 0) or 0),
                    "level3_score": int(getattr(level3, "score", 0) or 0),
                    "online": bool(getattr(state, "is_online", False)) if state is not None else False,
                    "last_online": last_online,
                    "game_uuid": str(getattr(playing, "game_uuid", "") or "") if playing is not None else "",
                    "mode_id": int(getattr(meta, "mode_id", 0) or 0) if meta is not None else 0,
                    # room_id 非空 = 友人戦房间；这种局没有公开牌谱，不能当段位场监控
                    "room_id": int(getattr(meta, "room_id", 0) or 0) if meta is not None else 0,
                    # contest_uid 非空 = 比赛场（大会），这种局没有公开牌谱，不能当段位场监控
                    "contest_uid": str(getattr(meta, "contest_uid", "") or "") if meta is not None else "",
                }
            )
        return friends

    async def remove_friend(self, target_id: int) -> None:
        """删除好友（好友列表里的删除按钮）。"""
        res = await self.rpc_call(".lq.Lobby.removeFriend", {"target_id": int(target_id)})
        if getattr(res.error, "code", 0):
            raise ValueError(f"removeFriend failed: {_error_summary(res.error)}")

    async def search_account_by_pattern(self, pattern: str, max_pages: int = 3) -> list[int]:
        """在当前登录区服内按昵称搜索账号 ID（结果与游戏客户端一致）。"""
        pattern = str(pattern or "").strip()
        if not pattern:
            return []
        found: list[int] = []
        search_next = False
        for _ in range(max(1, max_pages)):
            res = await self.rpc_call(
                ".lq.Lobby.searchAccountByPattern",
                {"search_next": search_next, "pattern": pattern},
            )
            code = int(getattr(res.error, "code", 0) or 0)
            if code:
                raise ValueError(f"searchAccountByPattern failed: {_error_summary(res.error)}")
            page = list(getattr(res, "match_accounts", []) or [])
            for raw in page:
                try:
                    aid = int(raw)
                except (TypeError, ValueError):
                    continue
                if aid > 0 and aid not in found:
                    found.append(aid)
            if getattr(res, "is_finished", True) or not page:
                break
            search_next = True
        return found

    async def fetch_friend_apply_list(self) -> list[dict]:
        """拉取待处理的好友申请。"""
        res = await self.rpc_call(".lq.Lobby.fetchFriendApplyList", {})
        code = int(getattr(res.error, "code", 0) or 0)
        if code:
            raise ValueError(f"fetchFriendApplyList failed: {_error_summary(res.error)}")
        applies = []
        for apply in getattr(res, "applies", []) or []:
            applies.append(
                {
                    "account_id": int(getattr(apply, "account_id", 0) or 0),
                    "apply_time": int(getattr(apply, "apply_time", 0) or 0),
                }
            )
        return applies

    async def handle_friend_apply(self, target_id: int, method: int = 1) -> None:
        """处理一条好友申请；method=1 同意，method=2 拒绝。"""
        res = await self.rpc_call(
            ".lq.Lobby.handleFriendApply",
            {"target_id": int(target_id), "method": int(method)},
        )
        code = int(getattr(res.error, "code", 0) or 0)
        if code:
            raise ValueError(f"handleFriendApply failed: {_error_summary(res.error)}")

    async def fetch_recent_friends(self) -> list[int]:
        """最近一起玩过的人（用来扩大「所有对局」的扫描范围）。"""
        res = await self.rpc_call(".lq.Lobby.fetchRecentFriend", {})
        if int(getattr(res.error, "code", 0) or 0):
            raise ValueError(f"fetchRecentFriend failed: {_error_summary(res.error)}")
        return [int(x) for x in getattr(res, "account_list", []) or []]

    async def fetch_game_record(self, game_uuid: str):
        """按对局 uuid 拉牌谱头（顺位/点数/玩家）。

        注意：必须带 client_version_string，否则服务端返回 code=151。
        """
        return await self.rpc_call(
            ".lq.Lobby.fetchGameRecord",
            {
                "game_uuid": game_uuid,
                "client_version_string": self.client_version_string,
            },
        )

    async def fetch_account_statistic_info(self, account_id: int):
        """查询任意玩家的雀魂统计（含最近 10 局顺位），不需要好友关系。"""
        return await self.rpc_call(
            ".lq.Lobby.fetchAccountStatisticInfo",
            {"account_id": int(account_id)},
        )

    async def fetch_account_info_extra(
        self, account_id: int, category: int = 1, game_type: int = 0
    ):
        """查询任意玩家的最近对局（含每局顺位/时间/点数）。"""
        return await self.rpc_call(
            ".lq.Lobby.fetchAccountInfoExtra",
            {
                "account_id": int(account_id),
                "category": int(category),
                "type": int(game_type),
            },
        )

    async def fetch_own_recent_entries(self, limit: int = 10) -> list:
        """当前登录账号的最近对局列表（RecordListEntry，含每座位顺位）。

        牌谱屋的 player_records 接口有 CAPTCHA，登录账号自己的牌谱列表不受影响。
        """
        now = int(time.time())
        res = await self.rpc_call(
            ".lq.Lobby.fetchGameRecordListV2",
            {
                "tag": 0,
                "begin_time": now - 90 * 24 * 3600,
                "end_time": now,
                "ranks": [],
                "modes": [],
                "max_hu_type": 0,
                "level_mode": [],
            },
        )
        code = int(getattr(res.error, "code", 0) or 0)
        if code:
            raise ValueError(f"fetchGameRecordListV2 failed: {_error_summary(res.error)}")
        iterator = str(getattr(res, "iterator", "") or "")
        entries: list = []
        for _ in range(3):
            if not iterator or len(entries) >= limit:
                break
            nxt = await self.rpc_call(
                ".lq.Lobby.fetchNextGameRecordList",
                {"iterator": iterator, "count": min(30, limit - len(entries))},
            )
            code = int(getattr(nxt.error, "code", 0) or 0)
            if code:
                break
            entries.extend(list(getattr(nxt, "entries", []) or []))
            if not bool(getattr(nxt, "next", False)):
                break
        return entries[:limit]

    async def fetch_account_states(self, account_ids: list[int]) -> dict[int, dict]:
        """批量查询账号在线/对局状态（不需要好友关系）。

        返回 {account_id: {account_id, online, game_uuid, mode_id}}。服务端只返回有状态
        记录的账号，离线很久或设置了隐私的账号可能不在结果里。
        """
        ids = [int(x) for x in account_ids if int(x or 0)]
        result: dict[int, dict] = {}
        for start in range(0, len(ids), 20):
            batch = ids[start:start + 20]
            res = await self.rpc_call(
                ".lq.Lobby.fetchAccountState",
                {"account_id_list": batch},
            )
            if int(getattr(res.error, "code", 0) or 0):
                raise ValueError(f"fetchAccountState failed: {_error_summary(res.error)}")
            for state in getattr(res, "states", []) or []:
                playing = getattr(state, "playing", None)
                meta = getattr(playing, "meta", None) if playing is not None else None
                result[int(state.account_id)] = {
                    "account_id": int(state.account_id),
                    "online": bool(getattr(state, "is_online", False)),
                    "game_uuid": str(getattr(playing, "game_uuid", "") or "") if playing is not None else "",
                    "mode_id": int(getattr(meta, "mode_id", 0) or 0) if meta is not None else 0,
                    # room_id 非空 = 友人戦房间；这种局没有公开牌谱
                    "room_id": int(getattr(meta, "room_id", 0) or 0) if meta is not None else 0,
                    # contest_uid 非空 = 比赛场（大会），这种局没有公开牌谱
                    "contest_uid": str(getattr(meta, "contest_uid", "") or "") if meta is not None else "",
                }
        return result

    async def fetch_live_games(self, filter_id: int = 216) -> list[dict]:
        """拉观战/直播列表（进行中的对局）。

        客户端的筛选参数是 `filter_id = 200 + mode_id`：
        208 金东、209 金南、211 玉东、212 玉南、215 王座东、216 王座南，
        三麻为 221~226。传 0 或其它值服务端会返回 code=8。
        """
        res = await self.rpc_call(".lq.Lobby.fetchGameLiveList", {"filter_id": int(filter_id)})
        code = int(getattr(res.error, "code", 0) or 0)
        if code:
            raise ValueError(f"fetchGameLiveList failed: {_error_summary(res.error)}")
        games = []
        for head in getattr(res, "live_list", []) or []:
            games.append(self._live_head_to_dict(head))
        return games

    async def fetch_live_info(self, game_uuid: str) -> dict:
        """按对局 uuid 拉进行中对局的头部信息（玩家、开局时间、房间）。"""
        res = await self.rpc_call(".lq.Lobby.fetchGameLiveInfo", {"game_uuid": game_uuid})
        code = int(getattr(res.error, "code", 0) or 0)
        if code:
            raise ValueError(f"fetchGameLiveInfo failed: {_error_summary(res.error)}")
        head = getattr(res, "live_head", None)
        if head is None:
            raise ValueError("fetchGameLiveInfo returned empty head")
        return self._live_head_to_dict(head)

    @staticmethod
    def _live_head_to_dict(head) -> dict:
        config = getattr(head, "game_config", None)
        meta = getattr(config, "meta", None) if config is not None else None
        mode_id = int(getattr(meta, "mode_id", 0) or 0) if meta is not None else 0
        # 三麻局取三麻段位（level3），四麻局取四麻段位（level）
        use_level3 = mode_id in THREE_PLAYER_MODE_IDS
        players = []
        for player in getattr(head, "players", []) or []:
            level = getattr(player, "level3" if use_level3 else "level", None)
            players.append(
                {
                    "account_id": int(getattr(player, "account_id", 0) or 0),
                    "nickname": str(getattr(player, "nickname", "") or ""),
                    "level_id": int(getattr(level, "id", 0) or 0),
                }
            )
        return {
            "uuid": str(getattr(head, "uuid", "") or ""),
            "start_time": int(getattr(head, "start_time", 0) or 0),
            "mode_id": mode_id,
            # contest_uid 非空 = 比赛场（大会）；room_id 非空 = 友人戦房间。都不是段位场
            "contest_uid": str(getattr(meta, "contest_uid", "") or "") if meta is not None else "",
            "room_id": int(getattr(meta, "room_id", 0) or 0) if meta is not None else 0,
            "players": players,
        }

    async def dispatch_msg(self):
        if self._ws is None:
            raise ConnectionError("connection is broken")

        try:
            while True:
                msg = await self._ws.recv()
                if not isinstance(msg, bytes):
                    continue
                if msg[0] == self._codec.NOTIFY:
                    # 通知包（含好友状态变动）只做分发，不能影响请求-响应通道
                    try:
                        data = self._codec.decode_message(msg)
                    except Exception as exc:
                        logger.debug(f"[majsoul-review] 忽略无法解析的通知: {exc}")
                        continue
                    await self._dispatch_notify(data)
                    continue
                data = self._codec.decode_message(msg)
                if data.msg_type != self._codec.RESPONSE:
                    continue
                idx = data.req_index
                if idx not in self._req_events:
                    continue
                self._res[idx] = data
                self._req_events[idx].set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._dispatch_error = exc
            logger.error(f"majsoul dispatch task failed: {exc}", exc_info=True)
            # 唤醒所有挂起请求，避免rpc_call无限等待
            for evt in list(self._req_events.values()):
                evt.set()
            raise

    async def rpc_call(self, method_name: str, payload: dict):
        if self._ws is None:
            raise ConnectionError("connection is broken")

        idx = self._codec.index
        req = self._codec.encode_request(method_name, payload)

        evt = asyncio.Event()
        self._req_events[idx] = evt
        try:
            # 发送也要有超时：半开连接时 ws.send 可能一直卡住（尤其云服务器被防火墙静默丢包）
            await asyncio.wait_for(self._ws.send(req), timeout=RPC_TIMEOUT_SEC)
        except asyncio.CancelledError:
            self._req_events.pop(idx, None)
            raise
        except BaseException as exc:
            self._req_events.pop(idx, None)
            if isinstance(exc, asyncio.TimeoutError):
                raise TimeoutError(f"rpc send timeout: {method_name}") from exc
            raise
        try:
            await asyncio.wait_for(evt.wait(), timeout=RPC_TIMEOUT_SEC)
        except asyncio.TimeoutError as exc:
            if self._dispatch_error is not None:
                raise ConnectionError(
                    f"rpc failed before response ({method_name}): {self._dispatch_error}"
                ) from self._dispatch_error
            raise TimeoutError(f"rpc timeout: {method_name}") from exc
        finally:
            if idx in self._req_events:
                del self._req_events[idx]

        if idx not in self._res:
            if self._dispatch_error is not None:
                raise ConnectionError(
                    f"rpc aborted ({method_name}): {self._dispatch_error}"
                ) from self._dispatch_error
            raise ConnectionError(f"rpc response missing: {method_name}")

        res = self._res[idx]
        del self._res[idx]
        return res.payload

    @staticmethod
    def encode_password(password: str) -> str:
        return hmac.new(b"lailai", password.encode(), hashlib.sha256).hexdigest()

    async def manual_login(
        self,
        username: str,
        password: str,
        version_info: MajsoulVersionInfo,
    ):
        password_hash = self.encode_password(password)
        logger.debug(
            "[majsoul-review] manual_login request meta: "
            f"client_version_string={self.client_version_string} "
            f"resource={version_info.version} tag={self.client_tag} "
            f"currency_platforms={self.currency_platforms}"
        )
        resp = cast(
            liblq.ResLogin,
            await self.rpc_call(
                ".lq.Lobby.login",
                {
                    "account": username,
                    "password": password_hash,
                    "reconnect": False,
                    "device": _client_device_info(),
                    "random_key": self.random_key,
                    "client_version": {"resource": version_info.version},
                    "currency_platforms": self.currency_platforms,
                    "client_version_string": self.client_version_string,
                    "gen_access_token": True,
                    "type": 0,
                    "tag": self.client_tag,
                },
            ),
        )
        if not resp.account_id:
            logger.debug(
                "[majsoul-review] manual_login failed response: "
                f"{_error_summary(resp.error)}"
            )
            raise ValueError(f"manual login failed: {_error_summary(resp.error)}")
        self.account_id = resp.account_id
        self.nick_name = resp.account.nickname
        self.access_token = resp.access_token
        logger.debug(
            "[majsoul-review] manual_login success: "
            f"account_id={self.account_id} nickname={self.nick_name} "
            f"access_token_len={len(self.access_token or '')}"
        )
        return self.account_id, self.access_token, self.nick_name

    async def access_token_login(
        self,
        version_info: MajsoulVersionInfo,
        access_token: str,
    ):
        resp = cast(
            liblq.ResOauth2Check,
            await self.rpc_call(
                ".lq.Lobby.oauth2Check",
                {"type": 0, "access_token": access_token},
            ),
        )
        logger.debug(
            "[majsoul-review] oauth2Check response: "
            f"has_account={resp.has_account} {_error_summary(resp.error)}"
        )
        if not resp.has_account:
            await asyncio.sleep(1)
            resp = cast(
                liblq.ResOauth2Check,
                await self.rpc_call(
                    ".lq.Lobby.oauth2Check",
                    {"type": 0, "access_token": access_token},
                ),
            )
            logger.debug(
                "[majsoul-review] oauth2Check retry response: "
                f"has_account={resp.has_account} {_error_summary(resp.error)}"
            )
        if not resp.has_account:
            raise ValueError(f"access token invalid: {_error_summary(resp.error)}")

        logger.debug(
            "[majsoul-review] oauth2Login request meta: "
            f"client_version_string={self.client_version_string} "
            f"resource={version_info.version} tag={self.client_tag} "
            f"currency_platforms={self.currency_platforms}"
        )
        resp = cast(
            liblq.ResLogin,
            await self.rpc_call(
                ".lq.Lobby.oauth2Login",
                {
                    "type": 0,
                    "access_token": access_token,
                    "reconnect": False,
                    "device": _client_device_info(),
                    "random_key": self.random_key,
                    "client_version": {"resource": version_info.version},
                    "currency_platforms": self.currency_platforms,
                    "client_version_string": self.client_version_string,
                    "tag": self.client_tag,
                },
            ),
        )
        if not resp.account_id:
            logger.debug(
                "[majsoul-review] oauth2Login failed response: "
                f"{_error_summary(resp.error)}"
            )
            raise ValueError(f"oauth2 login failed: {_error_summary(resp.error)}")
        self.account_id = resp.account_id
        self.nick_name = resp.account.nickname
        logger.debug(
            "[majsoul-review] oauth2Login success: "
            f"account_id={self.account_id} nickname={self.nick_name}"
        )

        beat = cast(
            liblq.ResCommon,
            await self.rpc_call(
                ".lq.Lobby.loginBeat",
                {"contract": "DF2vkXCnfeXp4WoGSBGNcJBufZiMN3UP"},
            ),
        )
        if beat.error.code:
            raise ValueError(f"loginBeat failed: {_error_summary(beat.error)}")

        self.access_token = access_token
        return self.account_id, self.access_token, self.nick_name

    async def fetch_logs(
        self,
        game_id: str,
        paipu_dir: Path,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> Dict:
        paipu_dir.mkdir(parents=True, exist_ok=True)
        raw_path = paipu_dir / f"{game_id} - raw.json"
        if raw_path.exists():
            await _notify(progress_cb, "命中原始牌谱缓存")
            async with aiofiles.open(raw_path, "r", encoding="utf-8") as f:
                return json.loads(await f.read())

        seps = game_id.split("_")
        log_id = seps[0]

        if len(seps) >= 3 and seps[2] == "2":
            log_id = decode_log_id(log_id)

        target_id = None
        if len(seps) >= 2:
            if seps[1].startswith("a"):
                target_id = decode_account_id2(int(seps[1][1:]))
            else:
                try:
                    target_id = int(seps[1])
                except ValueError:
                    target_id = None

        await _notify(progress_cb, f"调用 fetchGameRecord 拉取牌谱: {log_id}")
        logs = cast(
            liblq.ResGameRecord,
            await self.rpc_call(
                ".lq.Lobby.fetchGameRecord",
                {
                    "game_uuid": log_id,
                    "client_version_string": self.client_version_string,
                },
            ),
        )
        if logs.error.code:
            raise ValueError(f"fetchGameRecord failed: {logs.error}")

        detail_records = liblq.Wrapper().parse(logs.data)
        payload = liblq.GameDetailRecords().parse(detail_records.data)

        action_list = []
        diag = {
            "version": int(payload.version),
            "records": len(payload.records),
            "actions": len(payload.actions),
            "empty_result": 0,
            "skipped": [],
            "data_len": len(getattr(logs, "data", b"") or b""),
            "detail_len": len(getattr(detail_records, "data", b"") or b""),
        }

        def _wrapper_name(raw) -> str:
            parts = [p for p in str(getattr(raw, "name", "")).split(".") if p]
            return parts[-1] if parts else ""

        def _parse_item(name: str, blob: bytes):
            """解析单条 action；兼容 ActionXxx / RecordXxx 两种命名。"""
            if not name:
                diag["skipped"].append("<empty>")
                return None
            stem = name[6:] if name.startswith("Action") else name
            record_name = stem if stem.startswith("Record") else f"Record{stem}"
            parser_cls = getattr(liblq, record_name, None) or getattr(liblq, name, None)
            if parser_cls is None:
                diag["skipped"].append(name)
                logger.warning(f"[majsoul-review] 未知action类型，已跳过: {name}")
                return None
            try:
                msg = parser_cls().parse(blob)
            except Exception as exc:  # noqa: BLE001
                diag["skipped"].append(name)
                logger.warning(
                    f"[majsoul-review] action解析失败，已跳过 {name}: {exc.__class__.__name__}: {exc}"
                )
                return None
            item_name = record_name if hasattr(liblq, record_name) else name
            return MjsLogItem(name=item_name, data=msg)

        # 新记录用 actions（GameAction.result 里再套一层 Wrapper），旧记录用 records；
        # 万象修罗这类新玩法会出现 version 很新、动作却仍放在 records 里的情况，
        # 所以按“哪个来源有数据用哪个”来选，避免整局零动作。
        use_records = len(payload.records) > 0 and (
            payload.version < 210715 or len(payload.actions) == 0
        )
        if use_records:
            diag["source"] = "records"
            for value in payload.records:
                try:
                    raw = liblq.Wrapper().parse(value)
                except Exception as exc:  # noqa: BLE001
                    diag["skipped"].append("<wrapper>")
                    logger.warning(
                        f"[majsoul-review] 旧版action包装解析失败，已跳过: {exc.__class__.__name__}: {exc}"
                    )
                    continue
                item = _parse_item(_wrapper_name(raw), raw.data)
                if item is not None:
                    action_list.append(item)
        else:
            diag["source"] = "actions"
            for action in payload.actions:
                blob = action.result
                if not blob:
                    diag["empty_result"] += 1
                    continue
                try:
                    raw = liblq.Wrapper().parse(blob)
                except Exception as exc:  # noqa: BLE001
                    diag["skipped"].append("<wrapper>")
                    logger.warning(
                        f"[majsoul-review] action包装解析失败，已跳过: {exc.__class__.__name__}: {exc}"
                    )
                    continue
                item = _parse_item(_wrapper_name(raw), raw.data)
                if item is not None:
                    action_list.append(item)

        if not action_list:
            logger.warning(f"[majsoul-review] 牌谱里没有可解析的动作 {game_id}: {diag}")
        else:
            # 记录一下动作类型，方便排查新玩法缺和牌信息时到底来了哪些 action
            counts: Dict[str, int] = {}
            for item in action_list:
                counts[item.name] = counts.get(item.name, 0) + 1
            diag["names"] = counts

        parser = MajsoulPaipuParser()
        try:
            tenhou_log = parser.handle_game_record(record=MjsLog(logs.head, action_list))
        except Exception as exc:  # noqa: BLE001
            # 新玩法（万象修罗等）可能有本地解析器不认识的结构：别让整局牌谱丢掉，
            # 用兜底格式保存，至少保证牌谱文件和名次数据在；和牌详情会缺失。
            logger.warning(
                "[majsoul-review] 牌谱解析失败，改用兜底格式保存 "
                f"{game_id}: {exc.__class__.__name__}: {exc}",
                exc_info=True,
            )
            tenhou_log = _degraded_tenhou_log(logs.head, exc)

        tenhou_log["head"] = process_dict(logs.head)
        tenhou_log["game_id"] = game_id
        tenhou_log["log_id"] = log_id
        tenhou_log["target_id"] = target_id
        # 解析诊断（动作来源/数量/被跳过的类型/被跳过的动作报错），方便排查新玩法的和牌信息缺失
        if parser.parse_errors:
            diag["parse_errors"] = parser.parse_errors[:20]
        tenhou_log["_diag"] = diag

        if target_id is not None:
            for acc in logs.head.accounts:
                if acc.account_id == target_id:
                    tenhou_log["_target_actor"] = acc.seat
                    break
        else:
            if logs.head.accounts:
                tenhou_log["target_id"] = logs.head.accounts[0].account_id
                tenhou_log["_target_actor"] = logs.head.accounts[0].seat
            else:
                tenhou_log["_target_actor"] = 0

        async with aiofiles.open(raw_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(tenhou_log, ensure_ascii=False, indent=2))

        await _notify(progress_cb, "原始牌谱缓存完成")
        return tenhou_log


async def fetch_majsoul_info(
    url_base: str = URL_BASE,
    progress_cb: Optional[ProgressCallback] = None,
):
    await _notify(progress_cb, "正在获取雀魂版本信息")
    version_info = convert(
        await get_res(url_base, "version.json", bust_cache=True),
        MajsoulVersionInfo,
    )
    logger.debug(
        "[majsoul-review] version info: "
        f"version={version_info.version} force_version={version_info.force_version} "
        f"code={version_info.code}"
    )
    client_version_string, client_version_source = (
        await _fetch_login_client_version_string(url_base, version_info)
    )
    logger.debug(
        "[majsoul-review] login client version: "
        f"client_version_string={client_version_string} "
        f"source={client_version_source} "
        f"legacy={_legacy_client_version_string(version_info)}"
    )

    await _notify(progress_cb, "正在获取资源版本信息")
    res_info = convert(
        await get_res(url_base, f"resversion{version_info.version}.json"),
        MajsoulResInfo,
    )

    pb_version = res_info.res["res/proto/liqi.json"].prefix
    config_path = f"{res_info.res['config.json'].prefix}/config.json"
    logger.debug(
        "[majsoul-review] resource info: "
        f"proto_prefix={pb_version} config_path={config_path}"
    )
    await _notify(progress_cb, "正在加载协议定义")
    pb_def = convert(
        await get_res(url_base, f"{pb_version}/res/proto/liqi.json"),
        MajsoulLiqiProto,
    )
    # 雀魂新网关协议在 ReqRequestConnection 中新增了 platform 字段；
    # 线上 liqi.json 可能尚未带该字段，这里补上，保证握手请求能被正确构造。
    if "lq" in pb_def.nested and "ReqRequestConnection" in pb_def.nested["lq"].nested:
        rc_fields = pb_def.nested["lq"].nested["ReqRequestConnection"].fields
        if "platform" not in rc_fields:
            rc_fields["platform"] = {"type": "string", "id": 6}

    await _notify(progress_cb, "正在获取网关配置")
    config_obj = await get_res(url_base, config_path)
    try:
        config = convert(config_obj, MajsoulConfig)
    except ValidationError as exc:
        raise ValueError(f"config parse failed: {exc}")

    ip_defs = list(config.ip or [])
    if not ip_defs:
        raise ValueError("config.ip is empty")

    ip_def = next((x for x in ip_defs if getattr(x, "name", "") == "player"), ip_defs[0])
    gateways = list(getattr(ip_def, "gateways", []) or [])
    if not gateways:
        raise ValueError("gateway list is empty")

    gateway_urls = [g.url for g in gateways if getattr(g, "url", "")]
    if not gateway_urls:
        raise ValueError("gateway list is empty")
    random.shuffle(gateway_urls)

    servers = [f"{url.replace('https://', '')}/gateway" for url in gateway_urls]
    logger.debug(
        "[majsoul-review] gateway config: "
        f"ip_defs={len(ip_defs)} selected_ip={getattr(ip_def, 'name', '')} "
        f"gateway_count={len(servers)} servers={servers}"
    )

    return servers, pb_def, pb_version, version_info, client_version_string


async def create_connection(
    username: str = "",
    password: str = "",
    access_token: str = "",
    progress_cb: Optional[ProgressCallback] = None,
    url_base: str = URL_BASE,
) -> MajsoulConnection:
    (
        servers,
        pb_def,
        pb_version,
        version_info,
        client_version_string,
    ) = await fetch_majsoul_info(
        url_base,
        progress_cb=progress_cb,
    )

    codec = MajsoulProtoCodec(pb_def, pb_version)
    conn: Optional[MajsoulConnection] = None
    connect_errors = []

    for server in servers:
        candidate_conn = MajsoulConnection(
            f"wss://{server}",
            codec,
            version_info,
            client_version_string=client_version_string,
            origin=url_base,
        )
        try:
            await _notify(progress_cb, f"正在建立 WebSocket 连接 ({server})")
            await candidate_conn.connect()
            conn = candidate_conn
            break
        except Exception as exc:
            connect_errors.append(f"{server}: {exc}")
            logger.warning(f"[majsoul-review] 网关 {server} 连接失败: {exc}，尝试备用网关")
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await candidate_conn.close()

    if conn is None:
        raise ConnectionError(f"所有雀魂网关连接失败: {'; '.join(connect_errors)}")

    try:
        await _notify(progress_cb, "连接成功，发送心跳")
        _ = await conn.rpc_call(
            ".lq.Route.heartbeat",
            {
                "delay": random.randint(0, 200),
                "no_operation_counter": 0,
                "platform": 11,
                "network_quality": random.randint(0, 100),
            },
        )

        # 雀魂新网关握手协议：必须带上 platform="Web" 调用 requestConnection，
        # 否则后续 Lobby.login 会被网关拒绝并返回 code=151。
        route_id = "route-2"
        route_match = re.search(r"(route-\d+)", conn._endpoint)
        if route_match:
            route_id = route_match.group(1)

        await _notify(progress_cb, f"正在进行网关握手与平台验证 ({route_id})")
        try:
            await conn.rpc_call(
                ".lq.Route.requestConnection",
                {
                    "type": 1,
                    "route_id": route_id,
                    "timestamp": int(time.time()),
                    "platform": "Web",
                },
            )
        except Exception as route_exc:
            logger.warning(f"[majsoul-review] requestConnection failed or skipped: {route_exc}")

        if access_token:
            await _notify(progress_cb, "正在使用 access_token 登录")
            await conn.access_token_login(version_info, access_token)
        else:
            if not username or not password:
                raise ValueError("username or password is empty")
            await _notify(progress_cb, "正在使用账号密码登录")
            await conn.manual_login(username, password, version_info)

        await _notify(progress_cb, f"登录成功: account_id={conn.account_id}")
        return conn
    except Exception:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await conn.close()
        raise
