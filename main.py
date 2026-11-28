import asyncio
import json
import re
import time
import urllib.parse
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path

import aiohttp
from quart import jsonify, request

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, StarTools





from .level import MajsoulLevel, level_id_to_display

try:  # 插件内部引用（会被打成包导入时）
    from .review.account_ids import (
        decode_account_id,
        encode_account_id,
        ids_are_majsoul,
        set_ids_majsoul,
        to_account_id,
        to_majsoul,
    )
except ImportError:  # 直接按目录导入时
    from review.account_ids import (
        decode_account_id,
        encode_account_id,
        ids_are_majsoul,
        set_ids_majsoul,
        to_account_id,
        to_majsoul,
    )


# ── 常量 ──────────────────────────────────────────────
# 多数据节点轮换，避免单节点限制
_API_NODES = [
    "https://ak-data-1.sapk.ch/api/v2",
    "https://1.data.amae-koromo.com/api/v2",
    "https://4.data.amae-koromo.com/api/v2",
    "https://5-data.amae-koromo.com/api/v2",
]
_api_node_index = 0

def _get_api_base():
    """轮换获取 API 数据节点"""
    global _api_node_index
    node = _API_NODES[_api_node_index % len(_API_NODES)]
    _api_node_index += 1
    return node
START_TIME = 1262304000000
HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
DATA_FILE = "majsoul_matchreport_players.json"
LOG_DIR_NAME = "query_logs"
LOG_RETAIN_DAYS = 30          # 日志保留天数默认值（插件设置里可以改，见 LOG_RETAIN_KEY）
LOG_RETAIN_KEY = "log_retain_days"
LOG_RETAIN_MAX_DAYS = 3650
VERSION = "1.2.0"
# 群指令默认名（可在「指令设置」页修改）
DEFAULT_CMD_ADD = "雀魂播报"
DEFAULT_CMD_REMOVE = "删除播报"
DEFAULT_CMD_LIST = "雀魂播报列表"
DEFAULT_CMD_HELP = "雀魂播报帮助"
DEFAULT_CMD_PAIPU = "雀魂牌谱"
# 各群独立开关的四类播报指令默认名（可在「指令设置」页修改）
DEFAULT_CMD_THREE = "三麻播报"
DEFAULT_CMD_EAST = "东场播报"
DEFAULT_CMD_FRIEND = "友人播报"
DEFAULT_CMD_CONTEST = "比赛播报"
# 四类播报：kind -> 中文名 / 插件设置里的默认开关 / 指令名配置键
BROADCAST_KINDS = ("three", "east", "friend", "contest")
BROADCAST_LABELS = {
    "three": "三麻播报",
    "east": "东场播报",
    "friend": "友人播报",
    "contest": "比赛播报",
}
BROADCAST_DEFAULT_KEYS = {
    "three": "monitor_three_player",
    "east": "monitor_east",
    "friend": "monitor_friend",
    "contest": "monitor_contest",
}
BROADCAST_DEFAULT_VALUES = {
    "three": True,
    "east": True,
    "friend": False,
    "contest": False,
}
BROADCAST_CMD_KEYS = {
    "three": "cmd_three_name",
    "east": "cmd_east_name",
    "friend": "cmd_friend_name",
    "contest": "cmd_contest_name",
}
BROADCAST_CMD_DEFAULTS = {
    "three": DEFAULT_CMD_THREE,
    "east": DEFAULT_CMD_EAST,
    "friend": DEFAULT_CMD_FRIEND,
    "contest": DEFAULT_CMD_CONTEST,
}
# 各群的播报开关存档：{group_id: {"three": 1/0, "east": 1/0, "friend": 1/0, "contest": 1/0}}
GROUP_SETTINGS_FILE = "majsoul_matchreport_group_settings.json"
# 群名缓存：{group_id: {"name": "群名", "at": 更新时间}}，网页上显示「群名 + 群号」用
GROUP_NAMES_FILE = "majsoul_matchreport_group_names.json"
THEME_FILE = "majsoul_matchreport_theme.json"
# 四人模式ID -> 房间名称映射（仅四麻）
FOUR_PLAYER_MODES = {
    "3":  "铜之间·四人南",
    "6":  "银之间·四人南",
    "9":  "金之间·四人南",
    "12": "玉之间·四人南",
    "16": "王座间·四人南",
    "2":  "铜之间·四人东",
    "5":  "银之间·四人东",
    "8":  "金之间·四人东",
    "11": "玉之间·四人东",
    "15": "王座间·四人东",
}
# 房间 + 人数/场风 -> 雀魂 mode_id（三人南/东为 17~26、18~25 里的偶数/奇数）
ROOM_MODE_IDS = {
    ("铜之间", "四人东"): 2, ("铜之间", "四人南"): 3,
    ("银之间", "四人东"): 5, ("银之间", "四人南"): 6,
    ("金之间", "四人东"): 8, ("金之间", "四人南"): 9,
    ("玉之间", "四人东"): 11, ("玉之间", "四人南"): 12,
    ("王座间", "四人东"): 15, ("王座间", "四人南"): 16,
    ("铜之间", "三人东"): 17, ("铜之间", "三人南"): 18,
    ("银之间", "三人东"): 19, ("银之间", "三人南"): 20,
    ("金之间", "三人东"): 21, ("金之间", "三人南"): 22,
    ("玉之间", "三人东"): 23, ("玉之间", "三人南"): 24,
    ("王座间", "三人东"): 25, ("王座间", "三人南"): 26,
}
MODE_LABELS = {
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
    13: "乱斗之间·四人东", 14: "乱斗之间·四人南",
}
# 段位场（铜/银/金/玉/王座）：乱斗 13/14 不算段位场
RANKED_MODE_IDS = set(MODE_LABELS) - {13, 14}
# 三麻房间（17~26）；其余（含乱斗/友人戦）按四麻处理
THREE_PLAYER_MODE_IDS = {17, 18, 19, 20, 21, 22, 23, 24, 25, 26}
# 东场房间：名字以「东」结尾的（四人东 2/5/8/11/15、三人东 17/19/21/23/25、乱斗东 13）
# 与 review/state_monitor.py 里的 EAST_MODE_IDS 一致，这里从 MODE_LABELS 推导，避免两处写死不同步
EAST_MODE_IDS = {mode for mode, label in MODE_LABELS.items() if str(label).endswith("东")}
# 王座间（只有雀圣和魂天能进）：四人东 15 / 四人南 16、三人东 25 / 三人南 26。
# 这些房间里魂天玩家的段位分叫「魂珠」，播报里用 ❂ 代替 pt
KONTEN_ROOM_MODE_IDS = {15, 16, 25, 26}
# 播报里「近期战绩」只保留同模式最近 10 场
RECENT_RANK_LIMIT = 10


def recent_rank_bucket_key(sanma: bool) -> str:
    """三麻/四麻各存一份近期顺位，互不混。"""
    return "recent_ranks_three" if sanma else "recent_ranks_four"


def recent_ranks_of(entry: Dict, sanma: bool) -> List[str]:
    """取这个玩家同模式的最近十场顺位（按时间升序）。"""
    if not isinstance(entry, dict):
        return []
    values = entry.get(recent_rank_bucket_key(sanma))
    if not isinstance(values, list):
        return []
    return [str(v) for v in values if str(v).isdigit()][-RECENT_RANK_LIMIT:]


def append_recent_rank(entry: Dict, sanma: bool, rank: int) -> None:
    """把这一局的顺位追加到对应模式（三麻/四麻）的最近十场里。"""
    if not isinstance(entry, dict):
        return
    try:
        rank_value = int(rank or 0)
    except (TypeError, ValueError):
        rank_value = 0
    if not rank_value:
        return
    key = recent_rank_bucket_key(sanma)
    values = entry.get(key)
    if not isinstance(values, list):
        values = []
    values = [str(v) for v in values if str(v).isdigit()]
    values.append(str(rank_value))
    entry[key] = values[-RECENT_RANK_LIMIT:]
    entry["ranks_order"] = "asc"


# ── 监控有效期 ─────────────────────────────────────────────
# 从监控时间（added_at）起算，超过有效期就不再轮询、不再播报；
# unit=forever 表示永不过期（老数据监控时间为 - 的也永不过期）。
MONITOR_VALID_UNITS = ("day", "week", "month", "forever")
MONITOR_VALID_DEFAULT_VALUE = 3
MONITOR_VALID_DEFAULT_UNIT = "month"
MONITOR_VALID_MAX_VALUE = 3650


def add_months(ts: float, months: int) -> float:
    """按自然月往后加；目标月没有这一天时夹到月末（1/31 加 1 月 = 2/28）。"""
    import calendar

    dt = datetime.fromtimestamp(ts)
    total = dt.year * 12 + (dt.month - 1) + int(months)
    year, month_index = divmod(total, 12)
    month = month_index + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return datetime(year, month, day, dt.hour, dt.minute, dt.second, dt.microsecond).timestamp()


def monitor_expire_at(added_at: int, value: int, unit: str) -> float:
    """算监控到期时间戳；返回 0 表示不会过期。"""
    if not added_at or unit == "forever":
        return 0
    value = max(1, int(value or MONITOR_VALID_DEFAULT_VALUE))
    if unit == "day":
        return float(added_at) + value * 86400
    if unit == "week":
        return float(added_at) + value * 7 * 86400
    return add_months(added_at, value)


# 段位：level_id 编码为 [模式][主段位][小段位]，主段位 1~6，6 以上为魂天
LEVEL_TIER_NAMES = ["初心", "雀士", "雀杰", "雀豪", "雀圣", "魂天"]


def level_rank(level_id: int) -> tuple[int, str]:
    """把 level_id 转成 (段位序号1~6, 段位显示名)，例如 10502 -> (5, "雀圣2")。

    level_id 编码为 [模式][主段位][小段位]：主段位 1~6（6 以上为魂天），
    小段位对初心~雀圣是 1~3 星，对魂天是星数。
    """
    try:
        level_id = int(level_id or 0)
    except (TypeError, ValueError):
        return 0, ""
    if not level_id:
        return 0, ""
    major = (level_id % 10000) // 100
    minor = level_id % 100
    if major <= 0:
        return 0, ""
    tier = min(major, 6)
    name = LEVEL_TIER_NAMES[tier - 1]
    if 0 < minor <= 20:
        name += str(minor)
    return tier, name


def format_level_score(level_id: int, score: int) -> str:
    """段位 + pt 的显示，例如 10503/3506 -> "雀圣3 3506/9000"（魂天会自动换成 12.3/3000 这种）。"""
    try:
        level_id = int(level_id or 0)
    except (TypeError, ValueError):
        return ""
    if level_id <= 0:
        return ""
    try:
        _tier, name = level_rank(level_id)
    except Exception:
        name = ""
    if not name:
        return ""
    try:
        level = MajsoulLevel(level_id)
        return f"{name} {level.format_score(int(score or 0))}"
    except Exception:
        return name


def format_rank_pairs(level_id: int, level_score: int, level3_id: int, level3_score: int) -> str:
    """四麻/三麻段位并列显示，例如「四麻雀圣3 3506/9000，三麻雀圣2 1200/9000」。"""
    parts = []
    four = format_level_score(level_id, level_score)
    if four:
        parts.append(f"四麻{four}")
    three = format_level_score(level3_id, level3_score)
    if three:
        parts.append(f"三麻{three}")
    return "，".join(parts)




# ── API 客户端 ─────────────────────────────────────────────
class MajsoulMonitorAPI:
    """轻量级雀魂 API 客户端，仅用于监控"""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._session = aiohttp.ClientSession(
                headers=HEADERS, timeout=timeout
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def search_player(self, nickname: str, mode: str = "pl4") -> Optional[Dict]:
        """搜索玩家，返回 {id, nickname, level, latest_timestamp}"""
        session = await self._get_session()
        encoded = urllib.parse.quote(nickname, safe="")
        url = f"{_get_api_base()}/{mode}/search_player/{encoded}"
        last_err = None
        for attempt in range(3):
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json(content_type=None)
                    if isinstance(data, list) and data:
                        for p in data:
                            if p.get("nickname", "").casefold() == nickname.casefold():
                                return p
                        return data[0]
                    return None
            except Exception as e:
                last_err = e
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
        logger.warning(f"search_player failed after 3 retries: {last_err}")
        return None

    async def get_player_stats(
        self, player_id: int, mode: str = "pl4", game_mode: str = "16.12.9.15.11.8"
    ) -> Optional[Dict]:
        """获取玩家统计信息（用于获取 tag/count）"""
        session = await self._get_session()
        end_time = int(time.time() * 1000)
        url = (
            f"{_get_api_base()}/{mode}/player_stats/{player_id}/"
            f"{START_TIME}/{end_time}?mode={game_mode}"
        )
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                text = await resp.text()
                if "cap-token" in text.lower() or "captcha" in text.lower():
                    return None
                data = json.loads(text)
                return data if isinstance(data, dict) else None
        except Exception as e:
            logger.debug(f"get_player_stats failed: {e}")
            return None

    async def get_player_records(
        self, player_id: int, limit: int = 1, mode: str = "pl4", game_mode: str = "16",
        tag: str = ""
    ) -> List[Dict]:
        """获取玩家最近对局记录（可能被 CAPTCHA 拦截）"""
        session = await self._get_session()
        end_time = int(time.time() * 1000)
        tag_param = f"&tag={tag}" if tag else ""
        url = (
            f"{_get_api_base()}/{mode}/player_records/{player_id}/"
            f"{end_time}/{START_TIME}?limit={limit}&mode={game_mode}&descending=true{tag_param}"
        )
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.debug(f"player_records HTTP {resp.status}")
                    return []
                text = await resp.text()
                if "cap-token" in text.lower() or "captcha" in text.lower():
                    logger.debug("player_records blocked by CAPTCHA")
                    return []
                data = json.loads(text)
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug(f"get_player_records failed: {e}")
            return []


# ── 数据持久化 ─────────────────────────────────────────────
def _plugin_data_dir() -> Path:
    return StarTools.get_data_dir("astrbot_plugin_majsoul_matchreport")


def _ids_majsoul() -> bool:
    """存档里的 ID 是不是已经是雀魂里显示的数字 ID。"""
    try:
        return ids_are_majsoul(_plugin_data_dir())
    except Exception:
        return False


def _data_path() -> Path:
    base = StarTools.get_data_dir("astrbot_plugin_majsoul_matchreport")
    base.mkdir(parents=True, exist_ok=True)
    return base / DATA_FILE


def _theme_path() -> Path:
    base = StarTools.get_data_dir("astrbot_plugin_majsoul_matchreport")
    base.mkdir(parents=True, exist_ok=True)
    return base / THEME_FILE


def load_theme() -> Optional[str]:
    """读取页面主题；文件不存在时返回 None，由前端沿用 localStorage。"""
    path = _theme_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except Exception:
        return None
    theme = str(data.get("theme") or "").strip().lower()
    return theme if theme in ("dark", "light") else None


def save_theme(theme: str):
    _theme_path().write_text(
        json.dumps({"theme": "dark" if theme == "dark" else "light"}, ensure_ascii=False),
        encoding="utf-8",
    )


def load_monitored() -> Dict[str, Dict]:
    """加载监控列表 {nickname: {account_id, latest_ts, mode, group_ids}}"""
    path = _data_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return data
    majsoul_ids = _ids_majsoul()
    # 老数据用的是 player_id 字段，统一迁移成 account_id，避免监控目标被过滤成空
    changed = False
    for nickname, info in data.items():
        if not isinstance(info, dict):
            continue
        if not info.get("account_id") and info.get("player_id"):
            try:
                info["account_id"] = int(info["player_id"])
            except (TypeError, ValueError):
                continue
            changed = True
        if majsoul_ids:
            # 存档里是雀魂 ID，内存里统一换成 account_id
            try:
                raw = int(info.get("account_id") or 0)
            except (TypeError, ValueError):
                raw = 0
            if raw:
                info["account_id"] = int(to_account_id(raw))
        # 近期战绩分三麻/四麻两份：老的单一 recent_ranks 按 entry["mode"] 归到对应那一份
        legacy = info.get("recent_ranks")
        if isinstance(legacy, list) and legacy:
            ordered = list(reversed(legacy)) if info.get("ranks_order") != "asc" else list(legacy)
            bucket = recent_rank_bucket_key(str(info.get("mode") or "") == "pl3")
            if not info.get(bucket):
                info[bucket] = [str(r) for r in ordered if str(r).isdigit()][-RECENT_RANK_LIMIT:]
            info.pop("recent_ranks", None)
            info["ranks_order"] = "asc"
            changed = True
    if changed:
        try:
            save_monitored(data)
            logger.info("[MajsoulMonitor] 已把监控列表里的 player_id 迁移为 account_id")
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 迁移监控列表字段失败: {exc}")
    return data


def save_monitored(data: Dict[str, Dict]):
    path = _data_path()
    payload = data
    if _ids_majsoul():
        payload = {}
        for nickname, info in data.items():
            if not isinstance(info, dict):
                payload[nickname] = info
                continue
            copy = dict(info)
            try:
                copy["account_id"] = to_majsoul(int(info.get("account_id") or 0))
            except (TypeError, ValueError):
                pass
            payload[nickname] = copy
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _group_settings_path() -> Path:
    base = StarTools.get_data_dir("astrbot_plugin_majsoul_matchreport")
    base.mkdir(parents=True, exist_ok=True)
    return base / GROUP_SETTINGS_FILE


def load_group_settings() -> Dict[str, Dict[str, int]]:
    """加载各群自己的播报开关 {group_id: {kind: 1/0}}；没设置过的群不在里面。"""
    path = _group_settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except Exception as exc:
        logger.warning(f"[MajsoulMonitor] 读取群播报设置失败: {exc}")
        return {}
    if not isinstance(data, dict):
        return {}
    result: Dict[str, Dict[str, int]] = {}
    for gid, values in data.items():
        gid = str(gid or "").strip()
        if not gid or not isinstance(values, dict):
            continue
        item: Dict[str, int] = {}
        for kind in BROADCAST_KINDS:
            if kind not in values:
                continue
            try:
                item[kind] = 1 if bool(int(values.get(kind))) else 0
            except (TypeError, ValueError):
                item[kind] = 1 if values.get(kind) else 0
        if item:
            result[gid] = item
    return result


def save_group_settings(data: Dict[str, Dict[str, int]]) -> None:
    path = _group_settings_path()
    payload: Dict[str, Dict[str, int]] = {}
    for gid, values in (data or {}).items():
        gid = str(gid or "").strip()
        if not gid or not isinstance(values, dict):
            continue
        item = {}
        for kind in BROADCAST_KINDS:
            if kind in values:
                item[kind] = 1 if values.get(kind) else 0
        if item:
            payload[gid] = item
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _group_names_path() -> Path:
    base = StarTools.get_data_dir("astrbot_plugin_majsoul_matchreport")
    base.mkdir(parents=True, exist_ok=True)
    return base / GROUP_NAMES_FILE


def load_group_names() -> Dict[str, Dict[str, Any]]:
    """加载群号 → {name, at}（群名缓存，用来在网页上显示「群名 + 群号」）。"""
    path = _group_names_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except Exception as exc:
        logger.warning(f"[MajsoulMonitor] 读取群名缓存失败: {exc}")
        return {}
    if not isinstance(data, dict):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for gid, value in data.items():
        gid = str(gid or "").strip()
        if not gid or not isinstance(value, dict):
            continue
        name = str(value.get("name") or "").strip()
        if not name:
            continue
        try:
            at = float(value.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        result[gid] = {"name": name, "at": at}
    return result


def save_group_names(data: Dict[str, Dict[str, Any]]) -> None:
    payload: Dict[str, Dict[str, Any]] = {}
    for gid, value in (data or {}).items():
        gid = str(gid or "").strip()
        name = str((value or {}).get("name") or "").strip() if isinstance(value, dict) else ""
        if not gid or not name:
            continue
        try:
            at = float((value or {}).get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        payload[gid] = {"name": name, "at": at}
    _group_names_path().write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def encode_account_id2(account_id: int) -> int:
    p = 6139246 ^ account_id
    h_mask = 67108863
    s = p & ~h_mask
    z = p & h_mask
    for _ in range(5):
        z = ((511 & z) << 17) | (z >> 9)
    return z + s + 10000000


def show_uid(uid) -> str:
    """把账号管理内部的 uid 换成雀魂里显示的数字 ID（日志/回复统一用它）。"""
    text = str(uid or "").strip()
    if not text.isdigit():
        return text
    value = int(text)
    if value <= 0:
        return text
    return str(encode_account_id(value))


def build_paipu_url(uuid: str, player_id: int, url_base: str = "https://game.maj-soul.com/") -> str:
    encoded = encode_account_id2(player_id)
    base = (url_base or "https://game.maj-soul.com/").rstrip("/")
    return f"{base}/1/?paipu={uuid}_a{encoded}"


def unranked_kind(mode_id: Any, contest_uid: Any = "", room_id: Any = 0) -> str:
    """非段位场的类型：contest=比赛场，friend=友人场，other=其它非段位场，""=段位场。

    和 review/state_monitor.py 的 AccountGameMonitor._unranked_kind 保持同一套判断。
    """
    if str(contest_uid or "").strip():
        return "contest"
    try:
        room = int(room_id or 0)
    except (TypeError, ValueError):
        room = 0
    if room > 0:
        return "friend"
    try:
        mode = int(mode_id or 0)
    except (TypeError, ValueError):
        mode = 0
    try:
        from review.state_monitor import LIVE_MODE_IDS as _live_mode_ids
    except Exception:
        _live_mode_ids = RANKED_MODE_IDS
    if mode and mode not in _live_mode_ids:
        return "other"
    return ""


def unranked_label(kind: str, contest_uid: str = "", room_id: int = 0, mode_name: str = "") -> str:
    """非段位场的房间性质文字：友人场（房间号）/ 友人场·修罗之战（房间号）/ 比赛场（比赛场ID）。"""
    kind = str(kind or "")
    contest = str(contest_uid or "").strip()
    try:
        room = int(room_id or 0)
    except (TypeError, ValueError):
        room = 0
    # 友人场的特殊玩法写在房间性质后面，例如「友人场·修罗之战（76211）」
    friend_prefix = "友人场"
    mode_name = str(mode_name or "").strip()
    if mode_name:
        friend_prefix = f"友人场·{mode_name}"
    if kind == "contest":
        return f"比赛场（{contest}）" if contest else "比赛场"
    if kind == "friend":
        return f"{friend_prefix}（{room}）" if room else friend_prefix
    if contest:
        return f"比赛场（{contest}）"
    if room:
        return f"{friend_prefix}（{room}）"
    return "非段位场"


# 友人场里带特殊规则的模式（按牌谱 mode.detail_rule 的开关认名字）
# 值是一组开关名，全部为 1 才算这个模式；单个字符串就只判这一个开关
_FRIEND_MODE_FLAGS = (
    ("wanxiangxiuluo_mode", "万象修罗"),                 # 有癞子、每局换三张
    (("xuezhandaodi", "huansanzhang"), "修罗之战"),       # 血战到底 + 换三张、无癞子
    (("chuanma", "huansanzhang"), "赤羽之战"),            # 赤羽（穿马）+ 换三张
    ("reveal_discard", "暗夜之战"),                       # 明牌（暗夜）
)

# 娱乐玩法开关 id（detail_rule.amusement_switches）→ 模式名
_FRIEND_MODE_SWITCH_NAMES = {20: "宝牌双刃"}

# 友人场选的局数（牌谱 mode.mode）：四人 1=东风 2=半庄 4=一局，三人同值 +10
_FRIEND_ROUND_NAMES = {
    1: "东",
    2: "南",
    4: "一局",
    11: "东",
    12: "南",
    14: "一局",
}

# 人机对局的 mode.mode（四人 3 / 三人 13；没样本核对，认不出时退回「四人麻将 / 三人麻将」）
_FRIEND_AI_ROUND_MODES = {3: 4, 13: 3}


def _obj_field(obj: Any, name: str, default: Any = None) -> Any:
    """从 dict 或对象里取字段（牌谱头部有时是 proto 对象，有时是 JSON dict）。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _mode_rule_flag(rule: Any, name: str) -> bool:
    try:
        return bool(int(_obj_field(rule, name, 0) or 0))
    except (TypeError, ValueError):
        return False


def friend_mode_label(head: Any, nplayers: int = 0) -> str:
    """友人场房间行里要写的玩法：特殊玩法优先，普通对局按人数 + 局数写（四人东 / 三人南 / 四人一局…）。"""
    name = friend_mode_name(head)
    if name:
        return name
    try:
        count = int(nplayers or 0)
    except (TypeError, ValueError):
        count = 0
    if count >= 4:
        prefix = "四人"
    elif count == 3:
        prefix = "三人"
    else:
        return ""
    config = _obj_field(head, "config", None)
    mode = _obj_field(config, "mode", None)
    try:
        mode_code = int(_obj_field(mode, "mode", 0) or 0)
    except (TypeError, ValueError):
        mode_code = 0
    if mode_code in _FRIEND_AI_ROUND_MODES:
        return f"{prefix}人机"
    round_name = _FRIEND_ROUND_NAMES.get(mode_code)
    if round_name:
        return f"{prefix}{round_name}"
    return f"{prefix}麻将"


def _head_player_count(head: Any) -> int:
    """这局是几个人打的（友人场里用来区分四人麻将 / 三人麻将）。"""
    total = 0
    for key in ("accounts", "robots"):
        try:
            total += len(_obj_field(head, key, None) or [])
        except TypeError:
            continue
    if total >= 3:
        return total
    # 人机对局里 accounts/robots 常常只有 1 条（全是电脑），退回结果里的座位表
    result = _obj_field(head, "result", None)
    players = _obj_field(result, "players", None) or []
    try:
        return max(total, len(players))
    except TypeError:
        return total


def friend_mode_name(head: Any) -> str:
    """这局是友人场的哪种特殊玩法；认不出来返回空串。

    目前能认：修罗之战、暗夜之战、赤羽之战、万象修罗、宝牌双刃。
    """
    config = _obj_field(head, "config", None)
    mode = _obj_field(config, "mode", None)
    rule = _obj_field(mode, "detail_rule", None)
    if rule is None:
        return ""
    for flag, name in _FRIEND_MODE_FLAGS:
        flags = flag if isinstance(flag, tuple) else (flag,)
        if all(_mode_rule_flag(rule, item) for item in flags):
            return name
    switches = _obj_field(rule, "amusement_switches", None) or []
    for sid in switches:
        try:
            name = _FRIEND_MODE_SWITCH_NAMES.get(int(sid))
        except (TypeError, ValueError):
            name = None
        if name:
            return name
    return ""


def notice_field(raw: Any, name: str, default: Any = "") -> str:
    """从平台原始通知事件里取字段（dict 或对象都支持）。"""
    if isinstance(raw, dict):
        value = raw.get(name, default)
    else:
        value = getattr(raw, name, default)
    if value is None:
        return ""
    return str(value).strip()


def group_leave_info(raw: Any) -> Tuple[str, str]:
    """识别「机器人自己被踢出群聊 / 主动退出群聊」的群成员减少事件。

    返回 (群号, 原因文字)；不是这类事件时返回 ("", "")。
    """
    if notice_field(raw, "post_type") != "notice":
        return "", ""
    if notice_field(raw, "notice_type") != "group_decrease":
        return "", ""
    group_id = notice_field(raw, "group_id")
    if not group_id:
        return "", ""
    sub_type = notice_field(raw, "sub_type")
    self_id = notice_field(raw, "self_id")
    user_id = notice_field(raw, "user_id")
    is_self = bool(self_id) and user_id == self_id
    if sub_type == "kick_me" or (sub_type == "kick" and is_self):
        return group_id, "被踢出群聊"
    if sub_type == "leave" and is_self:
        return group_id, "主动退出群聊"
    return "", ""


class GroupLeaveFilter(filter.CustomFilter):
    """只让「机器人自己被踢出 / 主动退出群聊」的事件唤醒处理器。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        group_id, _reason = group_leave_info(raw)
        return bool(group_id)


# ── 牌谱回放 / 和牌详情 ──────────────────────────────
def extract_paipu_id(text: str) -> str:
    """从牌谱链接或牌谱 ID 中提取 paipu 参数。"""
    text = (text or "").strip()
    match = re.search(r"paipu=([^&\s]+)", text)
    if match:
        value = match.group(1)
    else:
        value = text.split()[0] if text else ""
    return value.strip().strip("'\"")


def _paipu_text(text: str) -> str:
    """把天凤/日文记法换成中文习惯写法，并统一转成简体。"""
    mapping = {
        "流し満貫": "流局满贯",
        "数え役満": "累计役满",
        "純正九蓮宝燈": "纯正九莲宝灯",
        "国士無双十三面": "国士无双十三面",
        "国士無双１３面": "国士无双十三面",
        "国士无双１３面": "国士无双十三面",
        "四暗刻単騎": "四暗刻单骑",
        "三倍満": "三倍满",
        "跳満": "跳满",
        "倍満": "倍满",
        "満貫": "满贯",
        "役満": "役满",
        "門前清自摸和": "门清自摸",
        "一盃口": "一杯口",
        "二盃口": "二杯口",
        "一気通貫": "一气通贯",
        "三色同順": "三色同顺",
        "混全帯幺九": "混全带幺九",
        "純全帯幺九": "纯全带幺九",
        "対々和": "对对和",
        "七対子": "七对子",
        "国士無双": "国士无双",
        "清老頭": "清老头",
        "混老頭": "混老头",
        "九蓮宝燈": "九莲宝灯",
        "緑一色": "绿一色",
        "綠一色": "绿一色",
        "四槓子": "四杠子",
        "三槓子": "三杠子",
        "四槓散了": "四杠散了",
        "嶺上開花": "岭上开花",
        "海底撈月": "海底捞月",
        "海底摸月": "海底摸月",
        "河底撈魚": "河底捞鱼",
        "槍槓": "抢杠",
        "九種九牌": "九种九牌",
        "四風連打": "四风连打",
        "八連荘": "八连庄",
        "五門斉": "五门齐",
        "ダブル立直": "双立直",
        "W立直": "双立直",
        "四喜和": "四喜和",
        "一発": "一发",
        "赤ドラ": "赤宝牌",
        "裏ドラ": "里宝牌",
        "ドラ": "宝牌",
        "自風": "自风",
        "場風": "场风",
        # 宝牌双刃：手里有和宝牌指示牌一样的牌叫「影牌」，每张减 1 番（牌谱里记成「役810(n飜)」）
        "役810": "影牌",
        "飜": "番",
        "∀": "all",
    }
    text = str(text)
    for src in sorted(mapping, key=len, reverse=True):
        text = text.replace(src, mapping[src])
    # 兜底：把役种/点数里残留的日文繁体字逐字转简体
    text = text.translate(_PAIPU_TRAD_TO_SIMP).strip()
    # 影牌是扣番的，写成负番数（影牌(2番) → 影牌(-2番)）
    return re.sub(r"影牌\((\d+)番\)", lambda m: f"影牌(-{m.group(1)}番)", text)


# 仅覆盖牌谱文本里会出现的繁体/日文汉字，避免误改玩家昵称
_PAIPU_TRAD_TO_SIMP = str.maketrans({
    "満": "满", "貫": "贯", "対": "对", "門": "门", "風": "风", "場": "场",
    "東": "东", "蓮": "莲", "燈": "灯", "無": "无", "雙": "双", "栄": "荣",
    "榮": "荣", "銃": "铳", "聴": "听", "聽": "听", "発": "发", "發": "发",
    "寶": "宝", "財": "财", "槓": "杠", "槍": "枪", "嶺": "岭", "撈": "捞",
    "魚": "鱼", "帯": "带", "純": "纯", "緑": "绿", "綠": "绿", "龍": "龙",
    "鳳": "凤", "盃": "杯", "連": "连", "荘": "庄", "莊": "庄", "國": "国",
    "點": "点", "與": "与", "為": "为", "這": "这", "說": "说", "時": "时",
    "間": "间", "開": "开", "關": "关",
})


def _paipu_names(raw: Dict) -> List[str]:
    raw_names = list(raw.get("name") or [])
    # 三麻牌谱会被补齐到 4 个名字，末尾的空名字直接去掉，避免出现「座位3」
    while raw_names and not str(raw_names[-1]).strip():
        raw_names.pop()
    names = []
    for idx, item in enumerate(raw_names):
        name = str(item).strip()
        # 好友房里带电脑时牌谱里的名字是「AI」或空的，统一写成「电脑」
        if name in {"", "AI", "CPU", "BOT"}:
            name = "电脑"
        names.append(name)
    return names


def _paipu_scores(names: List[str], scores: List[int]) -> str:
    parts = []
    for idx in range(len(names)):
        score = scores[idx] if idx < len(scores) else 0
        parts.append(f"{names[idx]}:{int(score)}")
    return " / ".join(parts)


def _paipu_int_list(value, size: int = 4) -> List[int]:
    result = [0] * size
    if isinstance(value, list):
        for idx, item in enumerate(value[:size]):
            try:
                result[idx] = int(item)
            except (TypeError, ValueError):
                result[idx] = 0
    return result


def _paipu_round_label(entry: list) -> str:
    # 新拉取的 raw 里 round 是 NamedTuple（tuple），从缓存 JSON 读出来是 list，两种都要支持
    info = entry[0] if entry and isinstance(entry[0], (list, tuple)) else [0, 0]
    kyoku = int(info[0]) if len(info) > 0 else 0
    honba = int(info[1]) if len(info) > 1 else 0
    wind = ["东", "南", "西", "北"][(kyoku // 4) % 4]
    return f"{wind}{kyoku % 4 + 1}局" + (f"{honba}本场" if honba else "")


def _paipu_dealer_seat(entry: list, player_count: int) -> int:
    """天凤格式的局数从 0 开始，庄家按人数轮转。"""
    info = entry[0] if entry and isinstance(entry[0], (list, tuple)) else [0]
    try:
        kyoku = int(info[0]) if info else 0
    except (TypeError, ValueError):
        kyoku = 0
    return kyoku % max(1, int(player_count or 4))


def _paipu_han_fu_order(point: str) -> str:
    """把「30符4番」统一成「4番30符」。"""
    return re.sub(r"(\d+)符(\d+)番", r"\2番\1符", str(point))


_PAIPU_YAKUMAN_SUFFIX = re.compile(r"[（(]\s*役[满満]\s*[)）]\s*$")


def _paipu_drop_duplicate_yakuman(point: str, yaku: List[str]) -> List[str]:
    """点数里已经写了「役满」时，去掉役种后面的「(役满)」，避免一条播报里出现两个役满。

    例：「役满16000点all 四暗刻(役满)」→「役满16000点all 四暗刻」；
    「役满96000点 大四喜(役满)、字一色(役满)」→「役满96000点 大四喜、字一色」。
    点数里不是役满（例如三倍满）时保持原样。
    """
    if "役满" not in str(point or ""):
        return yaku
    cleaned = []
    for item in yaku:
        text = _PAIPU_YAKUMAN_SUFFIX.sub("", str(item)).strip()
        if text:
            cleaned.append(text)
    # 全是「(役满)」后缀、清完没东西了，就保留原样，免得整行役种消失
    return cleaned or yaku


def _paipu_agari_items(result: list):
    """遍历一局里的所有和牌（双响会有多组）。"""
    if not isinstance(result, list) or not result or str(result[0]) != "和了":
        return
    for idx in range(1, len(result), 2):
        delta = _paipu_int_list(result[idx] if idx < len(result) else [])
        agari = result[idx + 1] if idx + 1 < len(result) else None
        if not isinstance(agari, list) or len(agari) < 4:
            continue
        try:
            winner = int(agari[0])
            from_seat = int(agari[1])
        except (TypeError, ValueError):
            continue
        yield delta, winner, from_seat, str(agari[3]), [str(y) for y in agari[4:] if str(y).strip()]


def _paipu_rule_text(raw: Dict) -> str:
    """这局是什么局：段位场 玉之间·四人南 / 友人戦（房间 123456） / 比赛场 / 其他对局。"""
    head = raw.get("head") if isinstance(raw, dict) else None
    if not isinstance(head, dict):
        return ""
    meta = ((head.get("config") or {}).get("meta") or {}) if isinstance(head.get("config"), dict) else {}
    if not isinstance(meta, dict):
        meta = {}
    try:
        mode_id = int(meta.get("mode_id") or 0)
    except (TypeError, ValueError):
        mode_id = 0
    try:
        room_id = int(meta.get("room_id") or 0)
    except (TypeError, ValueError):
        room_id = 0
    contest_uid = str(meta.get("contest_uid") or "").strip()
    if contest_uid and contest_uid != "0":
        return "比赛场"
    if room_id:
        return f"友人戦（房间 {room_id}）"
    label = MODE_LABELS.get(mode_id)
    if label:
        if mode_id in RANKED_MODE_IDS:
            return f"段位场 {label}"
        return label
    return f"其他对局（mode={mode_id}）" if mode_id else "未知对局"


def format_paipu_summary(raw: Dict, game_id: str = "") -> str:
    """把天凤格式牌谱整理成逐局文字总结。"""
    names = _paipu_names(raw)
    logs = raw.get("log") or []
    lines = ["🀄 牌谱回放" + (f"：{game_id}" if game_id else "")]
    rule_text = _paipu_rule_text(raw)
    if rule_text:
        lines.append(f"对局：{rule_text}")
    if names:
        lines.append(f"玩家：{' / '.join(names)}")
    for idx, entry in enumerate(logs, 1):
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        label = _paipu_round_label(entry)
        scores = _paipu_int_list(entry[1], len(names) or 4)
        dealer_seat = _paipu_dealer_seat(entry, len(names) or 4)
        lines.append("")
        lines.append(f"[{idx}] {label}")
        lines.append("  开始点数：" + _paipu_scores(names, scores))
        result = entry[-1] if entry else []
        if not isinstance(result, list) or not result:
            lines.append("  结果：无记录")
            continue
        result_type = str(result[0])
        if result_type == "和了":
            for delta, winner, from_seat, point, yaku in _paipu_agari_items(result):
                who = names[winner] if winner < len(names) else "电脑"
                point = _paipu_text(point)
                yaku = _paipu_sort_yaku([_paipu_text(y) for y in yaku])
                if winner == from_seat:
                    is_dealer = winner == dealer_seat
                    if is_dealer and "all" not in point:
                        point = point.rstrip() + "all"
                    action = "庄家自摸" if is_dealer else "闲家自摸"
                    lines.append(f"  和牌：{who} {action} {_paipu_han_fu_order(point)}")
                else:
                    from_who = names[from_seat] if from_seat < len(names) else "电脑"
                    lines.append(f"  和牌：{who} 荣和 {from_who} {_paipu_han_fu_order(point)}")
                if yaku:
                    lines.append("    役种：" + "、".join(yaku))
                scores = [scores[i] + delta[i] for i in range(len(scores))]
        elif result_type == "流局":
            delta = _paipu_int_list(result[1] if len(result) > 1 else [], len(names) or 4)
            lines.append("  结果：流局")
            scores = [scores[i] + delta[i] for i in range(len(scores))]
        elif result_type == "流し満貫":
            delta = _paipu_int_list(result[1] if len(result) > 1 else [], len(names) or 4)
            winners = [
                f"{names[i]} +{delta[i]}"
                for i in range(min(len(delta), len(names)))
                if delta[i] > 0
            ]
            lines.append(
                "  结果：流局满贯" + ("（" + "、".join(winners) + "）" if winners else "")
            )
            scores = [scores[i] + delta[i] for i in range(len(scores))]
        else:
            lines.append(f"  结果：{result_type}")
        # 下一局的开始点数最准确（含场供/立直棒），有就用它
        if idx < len(logs) and isinstance(logs[idx], list) and len(logs[idx]) > 1:
            scores = _paipu_int_list(logs[idx][1], len(names) or 4)
        lines.append("  结束点数：" + _paipu_scores(names, scores))
    head = raw.get("head") or {}
    accounts = list(head.get("accounts") or []) + list(head.get("robots") or [])
    seat_names = {}
    for a in accounts:
        name = str(a.get("nickname") or "").strip()
        if name in {"", "AI", "CPU", "BOT"}:
            name = "电脑"
        seat_names[int(a.get("seat", 0))] = name
    result_players = ((head.get("result") or {}).get("players") or [])
    if result_players:
        rows = []
        for player in result_players:
            seat = int(player.get("seat", 0))
            point = int(player.get("part_point_1") or 0)
            rows.append((point, seat_names.get(seat) or "电脑"))
        rows.sort(reverse=True)
        lines.append("")
        lines.append("最终点数：" + " / ".join(f"{name}:{point}" for point, name in rows))
    return "\n".join(lines)


def _paipu_han(point: str, yaku: List[str]) -> int:
    """从点数/役种文本里解析总番数（役满按 13 番算）。"""
    match = re.search(r"(\d+)番", point)
    if match:
        return int(match.group(1))
    if "役満" in point or "役满" in point:
        return 13
    # 牌谱里「満貫」这种写法没有番数，按档位换算成番数（三倍满要排在倍满前面判断）
    for key, han in (("三倍满", 11), ("倍满", 8), ("跳满", 6), ("满贯", 5)):
        if key in point:
            return han
    total = _paipu_yaku_han(yaku)
    if total:
        return total
    return 13 if any("役満" in y or "役满" in y for y in yaku) else 0


def _paipu_shadow_han(yaku: List[str]) -> int:
    """宝牌双刃的「影牌」扣了多少番。

    这个模式里宝牌每张算 2 番，手里有和宝牌指示牌一样的牌叫「影牌」，每张减 1 番；
    牌谱把它记成「役810(n飜)」，n 就是扣掉的番数（2 张影牌就是 2）。
    """
    total = 0
    for item in yaku or []:
        text = str(item or "")
        if not text.split("(")[0].strip().startswith("影牌"):
            continue
        found = re.search(r"(\d+)番", text)
        if found:
            total += int(found.group(1))
    return total


def _paipu_yaku_han(yaku: List[str]) -> int:
    """把役种里的番数加起来（影牌是扣番的，要减掉）。

    影牌在牌谱里记成正的 n 番（「役810(2飜)」），实际规则是扣 n 番，
    所以净番数 = 其它役相加 - 影牌（立直1 + 平和1 + 宝牌8 + 里宝牌8 - 影牌2 = 16 番）。
    """
    shadow = _paipu_shadow_han(yaku)
    total = 0
    for item in yaku or []:
        text = str(item or "")
        if text.split("(")[0].strip().startswith("影牌"):
            continue
        found = re.search(r"(\d+)番", text)
        if found:
            total += int(found.group(1))
    total -= shadow
    # 这个模式影牌最多扣到 1 番（牌谱的规则限制，扣不成 0 番或负数）
    if shadow:
        return max(1, total)
    return max(0, total)


def _paipu_kazoe_text(point: str, yaku: List[str]) -> str:
    """累计役满补上番数：「累计役满32000点」→「16番累计役满32000点」。"""
    text = str(point or "")
    if "累计役满" not in text or re.search(r"\d+番累计役满", text):
        return text
    han = _paipu_yaku_han(yaku)
    if han < 13:
        return text
    return text.replace("累计役满", f"{han}番累计役满", 1)


def _paipu_yakuman_mult(yaku: List[str]) -> int:
    """这手役满实际是几倍：大四喜 / 四暗刻单骑 / 纯正九莲宝灯 / 国士十三面各算 2 倍，
    其余役满 1 倍，复合役满直接相加（天和 + 国士无双十三面 = 三倍役满）。"""
    total = 0
    for item in yaku or []:
        name = _paipu_yakuman_name(str(item or "").split("(")[0])
        value = _YAKUMAN_VALUES.get(name)
        if value:
            total += value
    return total


def _paipu_yakuman_text(point: str, yaku: List[str]) -> str:
    """役满点数写成实际倍数：「役满64000点 四暗刻单骑」→「两倍役满64000点」。

    累计役满另有番数写法（见 `_paipu_kazoe_text`），这里不动。
    """
    text = str(point or "")
    if "役满" not in text or "累计役满" in text:
        return text
    prefix = _yakuman_mult_text(_paipu_yakuman_mult(yaku))
    if not prefix:
        return text
    return text.replace("役满", f"{prefix}役满", 1)


# 和牌档位：0=不够满贯 1=满贯 2=跳满 3=倍满 4=三倍满 5=役满（含累计役满）
PAIPU_TIER_NONE = 0
PAIPU_TIER_MANGAN = 1
PAIPU_TIER_HANEMAN = 2
PAIPU_TIER_BAIMAN = 3
PAIPU_TIER_SANBAIMAN = 4
PAIPU_TIER_YAKUMAN = 5


def _paipu_fu(point: str) -> int:
    """从点数文本里取符数（取不到按 30 符，宁可少播也不多播）。"""
    match = re.search(r"(\d+)符", str(point or ""))
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            pass
    return 30


def _paipu_score_tier(point: str, yaku: List[str]) -> int:
    """这手牌到没到满贯 / 跳满 / 倍满 / 三倍满 / 役满。

    满贯及以上不是只看番数：4 番要 40 符以上、3 番要 70 符以上才算满贯，
    4 番 30 符（7700 / 11600）还不够满贯。
    """
    text = str(point or "")
    if "役满" in text or "役満" in text:
        return PAIPU_TIER_YAKUMAN
    if "三倍满" in text:
        return PAIPU_TIER_SANBAIMAN
    if "倍满" in text:
        return PAIPU_TIER_BAIMAN
    if "跳满" in text:
        return PAIPU_TIER_HANEMAN
    if "满贯" in text:
        return PAIPU_TIER_MANGAN
    han = _paipu_han(text, yaku)
    if han >= 13:          # 累计役满
        return PAIPU_TIER_YAKUMAN
    if han >= 11:
        return PAIPU_TIER_SANBAIMAN
    if han >= 8:
        return PAIPU_TIER_BAIMAN
    if han >= 6:
        return PAIPU_TIER_HANEMAN
    if han >= 5:
        return PAIPU_TIER_MANGAN
    fu = _paipu_fu(text)
    if han == 4 and fu >= 40:
        return PAIPU_TIER_MANGAN
    if han == 3 and fu >= 70:
        return PAIPU_TIER_MANGAN
    return PAIPU_TIER_NONE


def _paipu_keep_hand(point: str, yaku: List[str], sanma: bool = False) -> bool:
    """这手牌要不要播：

    - 四麻：满贯及以上（满贯 / 跳满 / 倍满 / 三倍满 / 役满）都播，另外 4 番 30 符以上也播
      （4 番 20 符不算，4 番 30 符的 7700 / 11600 要播）；
    - 三麻：只播跳满及以上。
    """
    tier = _paipu_score_tier(point, yaku)
    if sanma:
        return tier >= PAIPU_TIER_HANEMAN
    if tier >= PAIPU_TIER_MANGAN:
        return True
    han = _paipu_han(point, yaku)
    fu = _paipu_fu(point)
    return han >= 4 and fu >= 30


# 播报里役种的显示顺序：同一格里的写法都算同一类（例如「立直 / 双立直」）
_PAIPU_YAKU_OTHER = 10          # 没列出来的役种排这里（宝牌之前）
_PAIPU_YAKU_ORDER = (
    ("立直", "双立直", "両立直", "ダブル立直", "W立直"),   # 0 立直 / 双立直
    ("一发", "一発", "岭上", "嶺上"),                       # 1 一发 / 岭上开花
    ("海底", "河底"),                                       # 2 海底 / 河底
    ("自摸", "门清自摸", "门前清自摸和", "門前清自摸和"),   # 3 自摸
    ("平和",),                                              # 4 平和
    ("一杯口", "两杯口", "一盃口", "二盃口"),               # 5 一杯口 / 两杯口
    ("断幺",),                                              # 6 断幺
    ("役牌",),                                              # 7 役牌（白/发/中）
    ("自风", "门风", "自風", "門風"),                       # 8 门风（自风）
    ("场风", "場風"),                                       # 9 场风
    ("\x00",),                                              # 10 其他役种（占位，不会匹配）
    ("宝牌", "ドラ"),                                       # 11 宝牌
    ("赤宝牌", "赤ドラ"),                                   # 12 红宝牌
    ("里宝牌", "裏ドラ"),                                   # 13 里宝牌
    ("影牌",),                                              # 14 影牌（宝牌双刃的扣番役）
)


def _paipu_yaku_rank(name: str) -> int:
    """役种排序权重：数字越小越靠前，没列出来的统一排到「其他役种」。"""
    base = str(name or "").split("(")[0].strip()
    for idx, bucket in enumerate(_PAIPU_YAKU_ORDER):
        if any(base.startswith(key) for key in bucket):
            return idx
    return _PAIPU_YAKU_OTHER


def _paipu_sort_yaku(yaku: List[str]) -> List[str]:
    """按固定顺序排列役种（同类内部保持牌谱里的原始顺序）。"""
    return sorted(yaku, key=_paipu_yaku_rank)


def _paipu_player_events(raw: Dict, seat: int, sanma: bool = False):
    """返回 (和牌列表, 放铳列表)。

    门槛见 `_paipu_keep_hand`：四麻 4 番 30 符以上（满贯及以上也都播），三麻跳满以上。
    """
    names = _paipu_names(raw)
    wins: List[str] = []
    deals: List[str] = []
    for entry in raw.get("log") or []:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        label = _paipu_round_label(entry)
        dealer_seat = _paipu_dealer_seat(entry, len(names) or 4)
        result = entry[-1] if entry else []
        if isinstance(result, list) and result and str(result[0]) == "流し満貫":
            # 流局满贯按满贯算，达不到跳满，三麻播报里跳过
            if not sanma:
                delta = _paipu_int_list(result[1] if len(result) > 1 else [])
                if 0 <= seat < len(delta) and delta[seat] > 0:
                    wins.append(f"{label} 流局满贯 +{delta[seat]}")
            continue
        for _delta, winner, from_seat, point, yaku in _paipu_agari_items(result):
            point = _paipu_text(point)
            yaku = _paipu_sort_yaku([_paipu_text(y) for y in yaku])
            if not _paipu_keep_hand(point, yaku, sanma):
                continue
            point = _paipu_han_fu_order(point)
            yaku = _paipu_drop_duplicate_yakuman(point, yaku)
            point = _paipu_kazoe_text(point, yaku)
            point = _paipu_yakuman_text(point, yaku)
            winner_name = names[winner] if winner < len(names) else "电脑"
            yaku_text = (" " + "、".join(yaku)) if yaku else ""
            if winner == seat:
                if winner == from_seat:
                    is_dealer = winner == dealer_seat
                    if is_dealer and "all" not in point:
                        point = point.rstrip() + "all"
                    action = "庄家自摸" if is_dealer else "闲家自摸"
                else:
                    action = "荣和"
                wins.append(f"{label} {action} {point}{yaku_text}")
            elif from_seat == seat and winner != from_seat:
                deals.append(f"{label} 放铳给 {winner_name} {point}{yaku_text}")
    return list(dict.fromkeys(wins)), list(dict.fromkeys(deals))


def _paipu_merged_events(raw: Dict, seats_map: Dict[int, str], sanma: bool = False):
    """一条播报里同桌有多个监控玩家时，合并和牌/放铳。

    - 每行前面写上是哪个玩家的战绩；
    - 荣和时写上被荣和的人是谁（"荣和某某"）；
    - 两个监控玩家互相点炮时，只在和牌那行写「谁荣和的谁」，放铳里不再重复。
    """
    names = _paipu_names(raw)
    nplayers = len(names) or 4
    wins: List[str] = []
    deals: List[str] = []
    for entry in raw.get("log") or []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        label = _paipu_round_label(entry)
        dealer_seat = _paipu_dealer_seat(entry, nplayers)
        result = entry[-1] if entry else []
        if isinstance(result, list) and result and str(result[0]) == "流し満貫":
            # 流局满贯按满贯算，达不到跳满，三麻播报里跳过
            if not sanma:
                delta = _paipu_int_list(result[1] if len(result) > 1 else [])
                for seat_idx in sorted(seats_map):
                    if 0 <= seat_idx < len(delta) and delta[seat_idx] > 0:
                        wins.append(f"{seats_map[seat_idx]} {label} 流局满贯 +{delta[seat_idx]}")
            continue
        for _delta, winner, from_seat, point, yaku in _paipu_agari_items(result):
            point = _paipu_text(point)
            yaku = _paipu_sort_yaku([_paipu_text(y) for y in yaku])
            if not _paipu_keep_hand(point, yaku, sanma):
                continue
            point = _paipu_han_fu_order(point)
            yaku = _paipu_drop_duplicate_yakuman(point, yaku)
            point = _paipu_kazoe_text(point, yaku)
            point = _paipu_yakuman_text(point, yaku)
            winner_name = names[winner] if winner < len(names) else "电脑"
            yaku_text = (" " + "、".join(yaku)) if yaku else ""
            tsumo = winner == from_seat
            winner_owner = seats_map.get(winner)
            dealer_owner = None if tsumo else seats_map.get(from_seat)
            if winner_owner:
                if tsumo:
                    is_dealer = winner == dealer_seat
                    if is_dealer and "all" not in point:
                        point = point.rstrip() + "all"
                    action = "庄家自摸" if is_dealer else "闲家自摸"
                    wins.append(f"{winner_owner} {label} {action} {point}{yaku_text}")
                else:
                    dealt_name = names[from_seat] if from_seat < len(names) else "电脑"
                    wins.append(f"{winner_owner} {label} 荣和{dealt_name} {point}{yaku_text}")
            elif dealer_owner:
                deals.append(f"{dealer_owner} {label} 放铳给 {winner_name} {point}{yaku_text}")
    return list(dict.fromkeys(wins)), list(dict.fromkeys(deals))


def _format_pt_delta(delta: int, level_id: int, unit: str = "pt") -> str:
    """段位分变化：魂天内部按 ×100 存（160 就是 1.6），其它段位就是整数分。

    unit 是单位后缀：普通段位是 pt，王座间的魂天是魂珠（❂）。
    """
    try:
        value = int(delta or 0)
    except (TypeError, ValueError):
        return ""
    if not value:
        return ""
    major = (int(level_id or 0) % 10000) // 100
    if major >= 6:  # 魂天（level.py 里 LEVEL_KONTEN - 1）
        return f"{value / 100:+.1f}{unit}"
    return f"{value:+d}{unit}"


def _level_rank_key(level_id: int) -> tuple:
    """段位高低比较用：(主段位, 小段位)，魂天的小段位也是等级。"""
    real = int(level_id or 0) % 10000
    return real // 100, real % 100


def _rank_direction(before_id: int, after_id: int) -> str:
    """升段 / 掉段：返回 "up"、"down" 或 ""（没变）。"""
    if before_id <= 0 or after_id <= 0 or int(before_id) == int(after_id):
        return ""
    before, after = _level_rank_key(before_id), _level_rank_key(after_id)
    if after > before:
        return "up"
    if after < before:
        return "down"
    return ""


def _yakuman_mult_text(multiplier: int) -> str:
    return {2: "两倍", 3: "三倍", 4: "四倍", 5: "五倍", 6: "六倍"}.get(
        int(multiplier or 1), f"{int(multiplier)}倍" if int(multiplier or 0) > 1 else ""
    )


# 役满倍数按役种算，复合役满直接相加（字一色 + 大四喜 = 三倍役满）
_YAKUMAN_VALUES = {
    "大三元": 1,
    "四暗刻": 1,
    "字一色": 1,
    "小四喜": 1,
    "九莲宝灯": 1,
    "国士无双": 1,
    "天和": 1,
    "地和": 1,
    "绿一色": 1,
    "清老头": 1,
    "四杠子": 1,
    "八连庄": 1,
    "累计役满": 1,
    "大四喜": 2,
    "四暗刻单骑": 2,
    "纯正九莲宝灯": 2,
    "国士无双十三面": 2,
}

# 天凤牌编码 -> 麻将字符：万 1-9 / 筒 1-9 / 索 1-9 / 东南西北白发中
_TENHOU_SUIT_BASE = {1: 0x1F007, 2: 0x1F019, 3: 0x1F010}
_TENHOU_HONOR_CHARS = {
    1: 0x1F000,
    2: 0x1F001,
    3: 0x1F002,
    4: 0x1F003,
    5: 0x1F006,
    6: 0x1F005,
    7: 0x1F004,
}


def _tenhou_tile_key(code) -> tuple:
    """天凤编码 -> (花色 1万/2筒/3索/4字, 点数)，红 5 当普通 5。"""
    try:
        value = int(code)
    except (TypeError, ValueError):
        return 0, 0
    if 51 <= value <= 53:  # 红 5
        return value - 50, 5
    if value >= 50:  # 其它大数不是合法牌码（60 是摸切，另有处理）
        return 0, 0
    if value <= 9:  # 0 = 杠后的占位符
        return 0, 0
    suit = value // 10
    num = value % 10
    if suit not in (1, 2, 3, 4) or num <= 0:
        return 0, 0
    return suit, num


def _tenhou_tile_char(key: tuple) -> str:
    suit, num = key
    if suit == 4:
        return chr(_TENHOU_HONOR_CHARS.get(num, 0))
    base = _TENHOU_SUIT_BASE.get(suit)
    if not base or not 1 <= num <= 9:
        return ""
    return chr(base + num - 1)


def _tenhou_symbol_tokens(text: str) -> List[Any]:
    """把 "c123456" / "1zp2z3z" / "515151a51" 拆成 ['c', 12, 34, 56] 这种记号。"""
    tokens: List[Any] = []
    i = 0
    text = str(text or "")
    while i < len(text):
        ch = text[i]
        if ch.isalpha():
            tokens.append(ch)
            i += 1
            continue
        pair = text[i : i + 2]
        if not pair.isdigit():
            i += 1
            continue
        tokens.append(int(pair))
        i += 2
    return tokens


def _paipu_meld_hand_tiles(text: str) -> List[tuple]:
    """副露符号里「从自己手里拿出去」的牌（被叫的那张不算）。

    暗杠拿走 4 张、加杠 3 张（原来碰的 2 张 + 刚拿到的那张，因为牌谱里加杠会替换掉碰）、
    碰/吃 2 张、大明杠 3 张、北抜き 1 张。
    这些符号在牌谱 dump 里有的在摸牌序列（吃/碰/大明杠），有的在打牌序列（暗杠/加杠/北抜き）。
    """
    text = str(text or "")
    match = re.search(r"a(\d{2})", text)
    if match:  # 暗杠：四张全来自手里
        return [_tenhou_tile_key(match.group(1))] * 4
    match = re.search(r"k(\d{2})", text)
    if match:  # 加杠：两张来自原来的碰 + 手里那张
        return [_tenhou_tile_key(match.group(1))] * 3
    if "f" in text:  # 北抜き（三麻）
        return [(4, 4)]
    tokens = _tenhou_symbol_tokens(text)
    if not tokens:
        return []
    kinds = [t for t in tokens if isinstance(t, str)]
    for kind in ("m", "c", "p"):
        if kind not in kinds:
            continue
        idx = tokens.index(kind)
        called_pos = sum(1 for t in tokens[: idx + 1] if isinstance(t, int))
        nums = [t for t in tokens if isinstance(t, int)]
        if called_pos >= len(nums):
            return []
        others = nums[:called_pos] + nums[called_pos + 1 :]
        return [_tenhou_tile_key(n) for n in others]
    return []


def _paipu_meld_tiles(text: str) -> List[tuple]:
    """副露符号代表的整组牌（含被叫的那张），用于把副露也画进牌型图。"""
    text = str(text or "")
    match = re.search(r"([ak])(\d{2})", text)
    if match:  # 暗杠 / 加杠：四张同牌
        return [_tenhou_tile_key(match.group(2))] * 4
    if "f" in text:  # 北抜き不算副露
        return []
    tokens = _tenhou_symbol_tokens(text)
    if not tokens:
        return []
    nums = [t for t in tokens if isinstance(t, int)]
    if "c" in tokens or "p" in tokens or "m" in tokens:
        return [_tenhou_tile_key(n) for n in nums]
    return []


def _paipu_meld_relative(text: str) -> int:
    """副露里被叫的那张牌的位置 -> 从哪家拿的：0=上家(左)、1=对门、2=下家(右)。

    牌谱里吃固定来自上家；碰/大明杠靠「被叫牌插在第几个位置」表示来源，
    大明杠的插入位置只可能是 0/1/3（跨家被挪到 3）。
    """
    tokens = _tenhou_symbol_tokens(str(text or ""))
    marker = next((t for t in ("m", "p", "k") if t in tokens), "")
    if not marker:
        return 0
    pos = sum(1 for t in tokens[: tokens.index(marker) + 1] if isinstance(t, int))
    if marker == "m":
        return {0: 0, 1: 1, 3: 2}.get(pos, 1)
    return max(0, min(pos, 2))


def _paipu_parse_meld(text: str, seat: int, nplayers: int) -> Optional[Dict[str, Any]]:
    """解析一组副露，返回 {text, kind, tiles, called, called_index, from_seat}。

    tiles 是按点数排好的整组牌；called_index 是被叫那张在 tiles 里的位置（画图时横过来），
    from_seat 是被叫牌来自哪个座位（暗杠/加杠为 -1）。
    """
    text = str(text or "")
    tokens = _tenhou_symbol_tokens(text)
    keys = [_tenhou_tile_key(t) for t in tokens if isinstance(t, int)]
    keys = [k for k in keys if k[0] > 0]
    if not tokens or "f" in tokens:  # 北抜き不算副露
        return None
    if "a" in tokens:  # 暗杠：四张全是自己摸的
        if len(keys) < 4:
            return None
        tiles = sorted(keys[:4], key=lambda k: (k[0], k[1]))
        return {
            "text": text,
            "kind": "ankan",
            "tiles": tiles,
            "called": None,
            "called_index": -1,
            "from_seat": -1,
        }
    if "k" in tokens:  # 加杠：原先碰的三张 + 刚摸到的第四张（牌谱里原先那组碰已被替换掉）
        if not keys:
            return None
        index = tokens.index("k")
        pos = sum(1 for t in tokens[: index + 1] if isinstance(t, int))
        relative = max(0, min(pos, 2))
        called = keys[0]
        return {
            "text": text,
            "kind": "kakan",
            "tiles": [called] * 4,
            "called": called,
            "called_index": relative,
            "from_seat": (int(seat) - {0: 1, 1: 2, 2: 3}[relative]) % int(nplayers),
        }
    marker = next((t for t in ("c", "p", "m") if t in tokens), "")
    if not marker or not keys:
        return None
    index = tokens.index(marker)
    # 被叫牌在整组牌里的位置（前 index+1 个记号里有几个是牌）
    pos = sum(1 for t in tokens[: index + 1] if isinstance(t, int))
    if marker == "c":  # 吃：只可能吃上家，被叫的那张紧跟在 c 后面
        if len(keys) < 3:
            return None
        called = keys[0]
        tiles = sorted(keys[1:3] + [called], key=lambda k: (k[0], k[1]))
        return {
            "text": text,
            "kind": "chi",
            "tiles": tiles,
            "called": called,
            "called_index": tiles.index(called),
            "from_seat": (int(seat) - 1) % int(nplayers),
        }
    if marker == "p":
        if len(keys) < 3:
            return None
        relative = max(0, min(pos, 2))
        called = keys[relative]
        return {
            "text": text,
            "kind": "pon",
            "tiles": [called] * 3,
            "called": called,
            "called_index": relative,
            "from_seat": (int(seat) - {0: 1, 1: 2, 2: 3}[relative]) % int(nplayers),
        }
    if len(keys) < 4:
        return None
    relative = {0: 0, 1: 1, 3: 2}.get(pos, 1)
    called = keys[min(pos, len(keys) - 1)]
    return {
        "text": text,
        "kind": "daiminkan",
        "tiles": [called] * 4,
        "called": called,
        "called_index": min(pos, 3),
        "from_seat": (int(seat) - {0: 1, 1: 2, 2: 3}[relative]) % int(nplayers),
    }


def _paipu_seat_tiles(entry, nplayers: int, seat: int):
    """还原某个座位这一局的最终手牌、打出的牌、副露、摸到的牌。

    牌谱里摸切用 60 表示，只有「摸到的那张」信息是隐式的，
    这里用「最近一次还没被摸切掉的摸牌」还原。
    副露符号可能在摸牌序列（吃/碰/大明杠），也可能在打牌序列（暗杠/加杠/北抜き）。
    """
    base = 4 + int(seat) * 3
    if not isinstance(entry, (list, tuple)) or len(entry) <= base + 2:
        return {}, [], [], []
    hand: Dict[tuple, int] = {}
    melds: List[Dict[str, Any]] = []

    def add(key, count: int = 1) -> None:
        if not key or key[0] <= 0 or key[1] <= 0:
            return
        hand[key] = hand.get(key, 0) + count

    def push_meld(text: str) -> None:
        meld = _paipu_parse_meld(text, seat, nplayers)
        if not meld:
            return
        if meld["kind"] == "kakan":
            # 加杠：原先那组碰升成杠，副露组数不变，被叫牌的位置/来源沿用碰的
            for index, old in enumerate(melds):
                if (
                    old["kind"] == "pon"
                    and old["tiles"]
                    and meld["tiles"]
                    and old["tiles"][0] == meld["tiles"][0]
                ):
                    meld["called"] = old["called"]
                    meld["called_index"] = old["called_index"]
                    meld["from_seat"] = old["from_seat"]
                    melds[index] = meld
                    return
        melds.append(meld)

    for code in entry[base] or []:
        add(_tenhou_tile_key(code))
    # 摸牌：一个摸打回合对应一条记录（牌或副露符号），摸切（60）打的就是这一回合摸到的牌
    draws: List[tuple] = []
    draw_keys: List[Optional[tuple]] = []
    for symbol in entry[base + 1] or []:
        if isinstance(symbol, int):
            key = _tenhou_tile_key(symbol)
            draw_keys.append(key if key[0] > 0 else None)
            if key[0] > 0:
                draws.append(key)
                add(key)
            continue
        for key in _paipu_meld_hand_tiles(str(symbol)):
            add(key, -1)
        push_meld(str(symbol))
        # 碰/吃/大明杠这一回合没有摸牌，但仍然占一个回合位
        draw_keys.append(None)
    # 打牌：第 k 条打牌对应第 k 个回合，摸切直接取这一回合摸到的牌
    discards: List[tuple] = []
    for turn, symbol in enumerate(entry[base + 2] or []):
        text_symbol = str(symbol) if isinstance(symbol, str) else ""
        if text_symbol:
            meld_hand = _paipu_meld_hand_tiles(text_symbol)
            if meld_hand:
                for key in meld_hand:
                    add(key, -1)
                push_meld(text_symbol)
                continue
            text_symbol = text_symbol[1:] if text_symbol.startswith("r") else text_symbol
            if not text_symbol.isdigit():
                continue
            symbol = text_symbol
        try:
            value = int(symbol)
        except (TypeError, ValueError):
            continue
        if value <= 0:  # 杠后的占位
            continue
        if value == 60:  # 摸切：打的就是这次摸到的牌
            key = draw_keys[turn] if turn < len(draw_keys) else None
            if key:
                add(key, -1)
                discards.append(key)
            continue
        key = _tenhou_tile_key(value)
        add(key, -1)
        discards.append(key)
    return {k: v for k, v in hand.items() if v > 0}, discards, melds, draws


# 上一次还原和牌牌面时的张数（还原失败时写进日志，方便定位牌谱里的边界情况）
_PAIPU_HAND_LAST: Dict[str, int] = {"count": 0}


def _paipu_msjl_tile(text) -> tuple:
    """雀魂牌面字符串（"1m"/"0p"）-> (花色, 点数, 是否赤)，认不出返回 (0, 0)。

    "bd" 是万象修罗这类玩法里的癞子（白搭），当成 (9, 0) 的伪牌处理，
    拼图时画成牌背加红框，不会被当成普通牌。
    """
    value = str(text or "").strip().lower()
    if value == "bd":
        return 9, 0
    if len(value) < 2 or not value[0].isdigit():
        return 0, 0
    suit = {"m": 1, "p": 2, "s": 3, "z": 4}.get(value[1], 0)
    if not suit:
        return 0, 0
    num = int(value[0])
    if num == 0 and suit <= 3:  # 0m / 0p / 0s 是赤五
        return suit, 5, True
    if num <= 0:
        return 0, 0
    return suit, num


def _paipu_msjl_tiles(items) -> List[tuple]:
    """牌面字符串列表（含 "1m2m3m" 连写、副露 "kezi(1z,1z,1z)"）-> [(花色, 点数), ...]。"""
    keys: List[tuple] = []
    for item in items or []:
        text = str(item or "").strip().lower()
        if not text:
            continue
        if "(" in text and text.endswith(")"):
            # 副露：kezi(1z,1z,1z) / minggang(4z,4z,4z,4z) / chi(2m,3m,4m)
            for chunk in text[text.index("(") + 1 : -1].split(","):
                key = _paipu_msjl_tile(chunk)
                if key[0] > 0:
                    keys.append(key)
            continue
        chunks = [text[i : i + 2] for i in range(0, len(text) - 1, 2)] if len(text) > 2 else [text]
        for chunk in chunks:
            key = _paipu_msjl_tile(chunk)
            if key[0] > 0:
                keys.append(key)
    return keys


def _paipu_record_hands(result) -> Dict[int, Dict[str, Any]]:
    """结算里附带的真实手牌（解析牌谱时挂在每局结果最后，供拼牌面用）。"""
    if not isinstance(result, list) or not result:
        return {}
    marker = result[-1]
    if not isinstance(marker, dict):
        return {}
    hands = marker.get("hais")
    if not isinstance(hands, dict):
        return {}
    out: Dict[int, Dict[str, Any]] = {}
    for seat, info in hands.items():
        try:
            out[int(seat)] = info if isinstance(info, dict) else {}
        except (TypeError, ValueError):
            continue
    return out


def _paipu_tile_sort_key(key) -> tuple:
    """牌面排序：癞子（白搭）固定排最左边，其余按 万 / 筒 / 索 / 字 升序。"""
    if tuple(key)[:2] == (9, 0):
        return (0, 0, 0)
    return (1, key[0], key[1])


def _paipu_record_melds(items) -> List[Dict[str, Any]]:
    """结算里的副露（"kezi(1z,1z,1z)" / "minggang(4z,4z,4z,4z)"）-> 牌面用的组。

    副露放到手牌右边单独成组，被叫的那张横过来放（跟牌谱回放的样子一致）。
    """
    melds: List[Dict[str, Any]] = []
    kind_map = {
        "kezi": "pon",
        "peng": "pon",
        "pon": "pon",
        "chi": "chi",
        "chi_": "chi",
        "minggang": "daiminkan",
        "daiminkan": "daiminkan",
        "angang": "ankan",
        "ankan": "ankan",
        "kakan": "kakan",
        "jiagang": "kakan",
    }
    for item in items or []:
        text = str(item or "").strip()
        if "(" not in text or not text.endswith(")"):
            continue
        kind = kind_map.get(text[: text.index("(")].strip().lower(), "pon")
        keys = _paipu_msjl_tiles([text])
        if not keys:
            continue
        melds.append(
            {
                "tiles": keys,
                "kind": kind,
                "called_index": 0 if kind in ("pon", "chi", "daiminkan") else -1,
            }
        )
    melds.sort(key=lambda m: (m["tiles"][0][0], m["tiles"][0][1]))
    return melds


def _paipu_record_layout(record) -> Optional[Dict[str, Any]]:
    """用结算里的手牌直接拼牌面（换牌/癞子玩法靠摸打还原是错的，这条路才准）。

    排布跟牌谱回放一致：手牌（癞子最左）｜和了的那张｜副露（杠也在右边成组）。
    """
    if not isinstance(record, dict):
        return None
    hidden = _paipu_msjl_tiles(record.get("hand"))
    melds = _paipu_record_melds(record.get("ming"))
    meld_total = sum(len(meld["tiles"]) for meld in melds)
    hu = _paipu_msjl_tile(record.get("hu_tile"))
    winning = hu if hu[0] > 0 else None
    # 杠比面子多一张，所以「14 张 + 杠数」才是这手牌应有的张数
    kans = sum(1 for meld in melds if len(meld["tiles"]) == 4)
    expected = 14 + kans
    total = len(hidden) + (1 if winning else 0) + meld_total
    if winning and total == expected + 1:
        # 有的记录把手牌写成「含和了牌」，这时别重复算一张（13 张手牌 + 和了牌才是 14）
        for index, key in enumerate(hidden):
            if key[:2] == winning[:2]:
                hidden.pop(index)
                total -= 1
                break
    if not hidden and not winning:
        return None
    hidden.sort(key=_paipu_tile_sort_key)
    if total != expected:
        # 张数对不上时宁可不发图：拼出来的牌面肯定是错的（多见于新玩法的特殊牌没认出来），
        # 详细信息写进查询日志，方便回来补牌面解析。
        logger.warning(
            f"[MajsoulMonitor] 结算手牌张数异常：{total} 张（期望 {expected} 张，副露 "
            f"{len(melds)} 组其中杠 {kans} 组，认出的牌 {hidden}，原始 hand="
            f"{record.get('hand')} ming={record.get('ming')}）"
        )
        return None
    return {
        "hidden": hidden,
        "winning": winning,
        "melds": melds,
        "total": total,
    }


def _paipu_winner_layout(
    entry, nplayers: int, winner: int, from_seat: int, record: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """还原和牌牌面：手牌 / 胡的那张 / 副露（含被叫牌来自哪家）。

    带杠时总张数是 14 + 杠数（每组杠比面子多一张），荣和时手牌少一张、等放铳那张补进来。
    结算里带了真实手牌时优先用它：万象修罗会换牌、还有癞子，靠摸打还原出来的牌面是错的。
    """
    if record:
        from_record = _paipu_record_layout(record)
        if from_record:
            _PAIPU_HAND_LAST["count"] = int(from_record.get("total") or 0)
            return from_record
    try:
        winner = int(winner)
        from_seat = int(from_seat)
    except (TypeError, ValueError):
        return None
    hand, _discards, melds, draws = _paipu_seat_tiles(entry, nplayers, winner)
    hidden: List[tuple] = []
    for key, count in hand.items():
        hidden.extend([key] * max(0, int(count)))
    hidden.sort(key=_paipu_tile_sort_key)
    melds = [m for m in melds if m.get("tiles")]
    melds.sort(key=lambda m: (m["tiles"][0][0], m["tiles"][0][1]))
    kans = sum(1 for meld in melds if len(meld["tiles"]) == 4)
    expected = 14 + kans
    winning = None
    if from_seat == winner:  # 自摸：最后摸到的那张，本来就在手牌里
        winning = draws[-1] if draws else None
        if winning:
            try:
                hidden.remove(winning)
            except ValueError:
                return None
    else:  # 荣和：放铳者最后打出的那张
        _h, dealt, _m, _d = _paipu_seat_tiles(entry, nplayers, from_seat)
        winning = dealt[-1] if dealt else None
    total = len(hidden) + (1 if winning else 0) + sum(len(m["tiles"]) for m in melds)
    _PAIPU_HAND_LAST["count"] = total
    if total != expected:
        logger.debug(
            f"[MajsoulMonitor] 役满牌面还原失败：seat={winner} 还原到 {total} 张"
            f"（期望 {expected} 张，副露 {len(melds)} 组其中杠 {kans} 组）"
        )
        return None
    return {"hidden": hidden, "winning": winning, "melds": melds, "total": total}


def _paipu_winner_hand(
    entry, nplayers: int, winner: int, from_seat: int, record: Optional[Dict[str, Any]] = None
) -> List[tuple]:
    """和牌者的整手牌（手牌 + 胡的那张 + 副露），还原失败返回空。"""
    layout = _paipu_winner_layout(entry, nplayers, winner, from_seat, record=record)
    if not layout:
        return []
    tiles = list(layout["hidden"])
    if layout["winning"]:
        tiles.append(layout["winning"])
    for meld in layout["melds"]:
        tiles.extend(meld["tiles"])
    return tiles


def _paipu_tile_chars(tiles: List[tuple]) -> str:
    return "".join(_tenhou_tile_char(tuple(item)) for item in tiles or [])


# 麻将牌面图片（resources/麻将牌面 下 1m-9m / 1p-9p / 1s-9s / 1z-7z，0m/0p/0s 是赤五）
# 这套图是 68x88、牌面已带白底圆角方框，直接按高度等比缩放拼图即可。
_PAIPU_TILE_DIR = Path(__file__).resolve().parent / "resources" / "麻将牌面"
_PAIPU_TILE_BACK = "back.png"   # 暗杠外侧盖住的牌背
_PAIPU_TILE_JOKER = "bd.png"    # 癞子（白搭）的牌面，缺失时退回牌背加红框
_PAIPU_TILE_HEIGHT = 110
_PAIPU_TILE_GAP = 1
_PAIPU_TILE_MARGIN = 12


def _tenhou_tile_code(key) -> int:
    """(花色, 点数) -> 天凤编码；红 5 用 51/52/53 表示。"""
    if isinstance(key, int):
        return key
    try:
        suit, num = int(key[0]), int(key[1])
    except (TypeError, ValueError, IndexError):
        return 0
    if suit not in (1, 2, 3, 4) or num <= 0:
        return 0
    red = False
    try:
        red = bool(key[2])
    except (TypeError, IndexError):
        red = False
    if red and suit <= 3 and num == 5:
        return 50 + suit
    return suit * 10 + num


def _tenhou_tile_file_name(code) -> str:
    """天凤编码 -> 牌面图片文件名（0m/0p/0s 是赤五）。"""
    try:
        value = int(code)
    except (TypeError, ValueError):
        return ""
    if value in (51, 52, 53):
        return f"0{'mps'[value - 51]}.png"
    suit, num = _tenhou_tile_key(value)
    if suit not in (1, 2, 3, 4) or num <= 0:
        return ""
    letter = "mpsz"[suit - 1]
    return f"{num}{letter}.png"


# 组与组之间（手牌 / 胡的牌 / 每组副露）留更大的一段间隔，一眼能分开
_PAIPU_TILE_GROUP_GAP = 36


def _paipu_image_groups(layout) -> List[List[Dict[str, Any]]]:
    """整理成「手牌 | 胡的那张 | 副露组…」的若干组，并标出哪几张要横过来放。"""
    groups: List[List[Dict[str, Any]]] = []
    if isinstance(layout, dict):
        hidden = [{"key": key, "rotated": False, "joker": key == (9, 0)} for key in layout.get("hidden") or []]
        if hidden:
            groups.append(hidden)
        winning = layout.get("winning")
        if winning:
            groups.append([{"key": winning, "rotated": False, "joker": winning == (9, 0)}])
        for meld in layout.get("melds") or []:
            tiles = list(meld.get("tiles") or [])
            if not tiles:
                continue
            called_index = int(meld.get("called_index", -1))
            kind = meld.get("kind")
            # 暗杠：外侧两张盖着（牌背），中间两张亮出来
            ankan = kind == "ankan"
            if kind == "kakan" and 0 <= called_index < len(tiles) - 1:
                # 加杠：第 4 张压在被叫的那张上面，不额外占位置
                tiles = tiles[:3]
            groups.append(
                [
                    {
                        "key": key,
                        "rotated": index == called_index,
                        "back": ankan and index in (0, len(tiles) - 1),
                        "stack": kind == "kakan" and index == called_index,
                        "joker": key == (9, 0),
                    }
                    for index, key in enumerate(tiles)
                ]
            )
    else:
        groups.append([{"key": key, "rotated": False, "joker": key == (9, 0)} for key in layout or []])
    return [group for group in groups if group]


def _paipu_tile_image(tiles, out_path: Optional[Path] = None) -> Optional[Path]:
    """把一手牌拼成一张白底图片；没装 Pillow 或牌面缺失时返回 None。

    tiles 可以是普通的牌列表，也可以是 _paipu_winner_layout() 的结果：
    带副露时按「手牌 | 胡的牌 | 副露」分组，被叫的那张牌横过来放。
    """
    groups = _paipu_image_groups(tiles)
    if not groups:
        return None
    try:
        from PIL import Image, ImageDraw
    except Exception:
        logger.debug("[MajsoulMonitor] 没有 Pillow，跳过役满牌面图片")
        return None
    height = _PAIPU_TILE_HEIGHT
    try:
        rendered: List[List[Any]] = []
        for group in groups:
            row: List[Any] = []
            for item in group:
                joker = bool(item.get("joker"))
                joker_face = False
                if joker:
                    path = _PAIPU_TILE_DIR / _PAIPU_TILE_JOKER
                    joker_face = path.exists()
                    if not joker_face:
                        path = _PAIPU_TILE_DIR / _PAIPU_TILE_BACK
                elif item.get("back"):
                    path = _PAIPU_TILE_DIR / _PAIPU_TILE_BACK
                else:
                    name = _tenhou_tile_file_name(_tenhou_tile_code(item["key"]))
                    path = (_PAIPU_TILE_DIR / name) if name else None
                if path is None or not path.exists():
                    return None
                image = Image.open(str(path)).convert("RGBA")
                image = image.resize(
                    (max(1, int(round(image.width * height / image.height))), height),
                    Image.LANCZOS,
                )
                if item.get("rotated"):
                    image = image.rotate(-90, expand=True)
                row.append((image, bool(item.get("stack")), joker and not joker_face))
            rendered.append(row)
        width = _PAIPU_TILE_MARGIN * 2
        for index, row in enumerate(rendered):
            if index:
                width += _PAIPU_TILE_GROUP_GAP
            width += sum(image.width for image, _s, _j in row) + _PAIPU_TILE_GAP * (len(row) - 1)
        # 加杠的那张是压在横置牌正上方的，顶上要多留出一个牌位的高度
        extra_top = 0
        for row in rendered:
            for image, stack, _j in row:
                if stack:
                    y = _PAIPU_TILE_MARGIN + height - image.height
                    extra_top = max(extra_top, _PAIPU_TILE_MARGIN - (y - image.height - _PAIPU_TILE_GAP))
        canvas = Image.new(
            "RGB", (width, height + _PAIPU_TILE_MARGIN * 2 + extra_top), (255, 255, 255)
        )
        draw = ImageDraw.Draw(canvas)
        x = _PAIPU_TILE_MARGIN
        for index, row in enumerate(rendered):
            if index:
                x += _PAIPU_TILE_GROUP_GAP
            for image, stack, joker in row:
                # 下沿对齐：横过来的牌比竖牌矮，底边要落在同一条线上
                y = extra_top + _PAIPU_TILE_MARGIN + height - image.height
                canvas.paste(image, (x, y), image)
                if stack:
                    # 加杠：第 4 张放在横置那张正上方，不叠压
                    canvas.paste(image, (x, y - image.height - _PAIPU_TILE_GAP), image)
                if joker:
                    # 癞子（白搭）画牌背 + 红框，跟普通牌区分开
                    draw.rectangle(
                        [x + 1, y + 1, x + image.width - 2, y + image.height - 2],
                        outline=(200, 30, 30),
                        width=3,
                    )
                x += image.width + _PAIPU_TILE_GAP
            x -= _PAIPU_TILE_GAP
        if out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            canvas.save(str(out_path), "PNG")
            return out_path
        return None
    except Exception as exc:  # 图片只是锦上添花，失败不影响播报
        logger.warning(f"[MajsoulMonitor] 拼牌面图片失败: {exc}")
        return None


def _paipu_yakuman_name(name: str) -> str:
    """把役满名字统一成 _YAKUMAN_VALUES 里的写法（去掉「待ち」之类后缀）。"""
    text = str(name or "").strip()
    for suffix in ("待ち", "待"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    text = text.replace("13面", "十三面").replace("１３面", "十三面")
    return text.strip()


def _paipu_yakuman_events(raw: Dict, seat: int) -> List[Dict[str, Any]]:
    """找出牌谱里的役满（含复合役满）。

    返回 [{label, kind, winner_name, multiplier, names, yaku, tiles}]，
    kind: win=自己胡的役满、deal=自己点炮的役满、other=与自己无关。
    倍数按役种算（复合役满相加），认不出的役种才退回按和牌点数估算；
    tiles 是和牌者的 14 张牌（天凤编码），取不到时为空。
    """
    names = _paipu_names(raw)
    nplayers = len(names) or 4
    events: List[Dict[str, Any]] = []
    for entry in raw.get("log") or []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        label = _paipu_round_label(entry)
        dealer_seat = _paipu_dealer_seat(entry, nplayers)
        result = entry[-1] if entry else []
        record_hands = _paipu_record_hands(result)
        for _delta, winner, from_seat, point, yaku in _paipu_agari_items(result):
            point_cn = _paipu_text(point)
            yaku_cn = [_paipu_text(y) for y in yaku]
            if "役满" not in point_cn and not any("役满" in y for y in yaku_cn):
                continue
            tsumo = winner == from_seat
            candidates = [
                _paipu_yakuman_name(item.split("(")[0]) for item in yaku_cn if "役满" in item
            ]
            candidates = [n for n in candidates if n]
            known_names = [n for n in candidates if n in _YAKUMAN_VALUES]
            # 累计役满：番数堆到 13 番以上，本身不是役种，役种列表里是一堆普通役
            is_kazoe = "累计役满" in point_cn or "数え役満" in point_cn
            prefix = re.split(r"\d", point_cn)[0].replace("点", "").strip()
            if known_names:
                # 认得出的役种（含复合役满）优先，倍数按役种相加
                yakuman_names = known_names
                values = [_YAKUMAN_VALUES[n] for n in known_names]
            elif is_kazoe:
                # 累计役满：番数堆到 13 番以上，本身不是役种，役种列表里是一堆普通役
                yakuman_names = ["累计役满"]
                values = None  # 倍数只能看点数额
            elif candidates:
                # 役种写法认不出来，退回点数文本里的前缀
                yakuman_names = [prefix] if prefix else candidates
                values = None
            else:
                yakuman_names = [prefix] if prefix else []
                values = None
            # 倍数按役种算：大四喜/四暗刻单骑/纯正九莲宝灯/国士十三面是两倍，其余一倍，复合相加
            if values and all(v is not None for v in values):
                multiplier = int(sum(values))
            else:
                dealer = winner == dealer_seat
                numbers = [int(x) for x in re.findall(r"\d+", point_cn)]
                # 自摸是「16000-32000点」这种，第一个数是每人付的点数，别拿最大的那个算
                amount = numbers[0] if numbers else 0
                base = 16000 if tsumo else (48000 if dealer else 32000)
                multiplier = 1
                if amount and base:
                    try:
                        multiplier = max(1, int(round(amount / base)))
                    except Exception:
                        multiplier = 1
            if yakuman_names == ["累计役满"]:
                # 累计役满没有多倍说法：哪怕 26 番、点数按两倍役满给，也只算一个累计役满
                multiplier = 1
            kind = "other"
            if winner == seat:
                kind = "win"
            elif from_seat == seat and not tsumo:
                kind = "deal"
            layout = _paipu_winner_layout(entry, nplayers, winner, from_seat, record=record_hands.get(winner))
            events.append(
                {
                    "label": _paipu_round_label(entry),
                    "kind": kind,
                    "winner": winner,
                    "winner_name": names[winner] if winner < len(names) else "电脑",
                    "multiplier": multiplier,
                    "names": yakuman_names,
                    # 一共多少番（累计役满要写「16番累计役满」，影牌在这时已经扣掉）
                    "han": _paipu_yaku_han(yaku_cn),
                    "yaku": [y for y in yaku_cn if str(y).strip()],
                    "layout": layout,
                    "tiles": _paipu_winner_hand(
                        entry, nplayers, winner, from_seat, record=record_hands.get(winner)
                    ),
                }
            )
    return events


def _yakuman_message(nickname: str, event: Dict[str, Any], with_tiles: bool = True) -> str:
    """把役满事件拼成一条播报（喜报=自己胡、悲报=点炮）。"""
    multiplier = int(event.get("multiplier") or 1)
    mult_text = _yakuman_mult_text(multiplier)
    event_names = [str(n) for n in (event.get("names") or [])]
    names = "".join(event_names)
    if event_names == ["累计役满"]:
        # 累计役满永远只有一倍，不写倍数前缀，但要把番数写上（「16番累计役满」）
        kazoe_han = int(event.get("han") or 0)
        body = f"{kazoe_han}番累计役满" if kazoe_han >= 13 else "累计役满"
    elif any("役满" in n for n in event_names):
        body = f"{mult_text}{names}"
    else:
        body = f"{mult_text}役满{names}"
    if event.get("kind") == "win":
        head = f"喜报！恭喜{nickname}在本局中胡到{body}！"
    else:
        head = f"悲报！{nickname}不小心点炮了{event.get('winner_name') or '对手'}的{body}！"
    if not with_tiles:
        return head
    # 兜底：拼不出图片时才退回麻将字符
    chars = _paipu_tile_chars(event.get("tiles") or [])
    if chars:
        return head + "\n牌型：" + chars
    return head


# ── 主插件类 ──────────────────────────────────────────
class MajsoulMonitorPlugin(Star):
    """雀魂对局监控插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.api = MajsoulMonitorAPI()
        self.monitored: Dict[str, Dict] = {}
        # 各群自己的播报开关（三麻/东场/友人/比赛），没设置过的群按插件设置里的默认值
        self.group_settings: Dict[str, Dict[str, int]] = {}
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._player_locks: Dict[str, asyncio.Lock] = {}
        self._monitor = None
        self._log_content_cache: Dict[str, tuple] = {}
        # 群主/管理员身份缓存，避免每条指令都去问一次群成员列表
        self._group_role_cache: Dict[str, tuple] = {}
        # 群名缓存：{group_id: {"name": 群名, "at": 更新时间}}，网页上显示「群名 + 群号」
        self._group_names: Dict[str, Dict[str, Any]] = {}
        # 群名的拉取节流：{group_id: 上次尝试时间}，避免每条消息都问一次群信息
        self._group_name_try: Dict[str, float] = {}
        # 上次从平台拉「群列表」的时间（整批同步群名用）
        self._group_names_synced_at: float = 0.0

    # ── 生命周期 ──────────────────────────────────────
    async def initialize(self):
        try:
            self._migrate_command_names()
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 迁移指令名异常: {exc}")
        self.monitored = load_monitored()
        try:
            self.group_settings = load_group_settings()
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 加载群播报设置失败: {exc}")
            self.group_settings = {}
        try:
            self._group_names = load_group_names()
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 加载群名缓存失败: {exc}")
            self._group_names = {}
        cfg_nodes = self.config.get("api_nodes", [])
        if cfg_nodes and isinstance(cfg_nodes, list):
            _API_NODES.clear()
            _API_NODES.extend([str(n).strip().rstrip("/") for n in cfg_nodes if str(n).strip()])
        # Cancel existing poll task to prevent duplicates on reload
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._monitor is not None:
            await self._monitor.stop("插件重载/重新初始化")
            self._monitor = None

        self._running = bool(self.config.get("enabled", True))
        interval = max(30, int(self.config.get("poll_interval", 120)))
        friend_interval = max(10, int(self.config.get("friend_poll_interval", 30)))
        if self._running:
            # 登录雀魂账号 → 每 30s 扫观战列表发现对局 → 好友看观战入口/非好友等列表消失 → 查牌谱播报
            await self._start_monitor()
            self._log_system(
                "插件已启用",
                f"监控 {len(self.monitored)} 人，好友轮询间隔 "
                f"{getattr(self._monitor, 'friend_poll_interval', friend_interval)} 秒，"
                f"非好友轮询间隔 {getattr(self._monitor, 'poll_interval', interval)} 秒",
                "INFO",
            )
        else:
            self._task = None
            self._log_system("插件已禁用", "enabled=false，未启动监控（所有后台活动停止）", "WARN")
        self._cleanup_old_logs()
        logger.info(
            f"[MajsoulMonitor] 对局监控已启动，监控 {len(self.monitored)} 人，"
            f"好友轮询间隔 {getattr(self._monitor, 'friend_poll_interval', friend_interval)} 秒，"
            f"非好友轮询间隔 {getattr(self._monitor, 'poll_interval', interval)} 秒"
            if self._running
            else "[MajsoulMonitor] 插件已禁用（enabled=false）"
        )
        self._register_web_apis()
        # 后台把「群号 → 群名」整批补一次：这样没在群里发过消息的群也能显示群名
        try:
            asyncio.create_task(self._sync_group_names(force=True))
        except Exception as exc:
            logger.debug(f"[MajsoulMonitor] 启动同步群名失败: {exc}")

    def _migrate_command_names(self) -> None:
        """把旧版默认指令名换成新的：雀魂播报+ → 雀魂播报，雀魂播报- → 删除播报。

        只改「还是旧默认值」的配置；用户自己改过的名字保持不动。
        """
        legacy = {
            "cmd_add_name": ("雀魂播报+", DEFAULT_CMD_ADD),
            "cmd_remove_name": ("雀魂播报-", DEFAULT_CMD_REMOVE),
        }
        changed = []
        for key, (old_name, new_name) in legacy.items():
            if str(self.config.get(key) or "").strip() == old_name:
                self.config[key] = new_name
                changed.append(f"{key}: {old_name} -> {new_name}")
        if not changed:
            return
        try:
            cfg_path = _plugin_config_path()
            existing = {}
            if cfg_path.exists():
                existing = json.loads(cfg_path.read_text(encoding="utf-8-sig") or "{}")
            if not isinstance(existing, dict):
                existing = {}
            existing["cmd_add_name"] = str(self.config.get("cmd_add_name", DEFAULT_CMD_ADD))
            existing["cmd_remove_name"] = str(self.config.get("cmd_remove_name", DEFAULT_CMD_REMOVE))
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 写回指令名失败: {exc}")
        logger.info(f"[MajsoulMonitor] 指令名已更新: {'；'.join(changed)}")

    def _server_url(self) -> str:
        """当前选择的雀魂区服入口（jp=日服，cn=中文服）。"""
        region = str(self.config.get("server_region") or "jp").strip().lower()
        try:
            from review.constants import SERVER_URLS
            return SERVER_URLS.get(region) or SERVER_URLS["jp"]
        except Exception:
            return "https://game.maj-soul.com/"

    def _search_urls(self):
        """昵称解析的多节点列表：优先用配置里的牌谱屋节点，再补默认节点。"""
        from review.state_monitor import AMAE_SEARCH_URLS

        urls = [
            f"{str(base).rstrip('/')}/{{mode}}/search_player/{{name}}"
            for base in list(_API_NODES)
            if str(base).strip()
        ]
        for template in AMAE_SEARCH_URLS:
            if template not in urls:
                urls.append(template)
        return urls

    async def _start_monitor(self):
        from review.state_monitor import AMAE_SEARCH_URLS, AccountGameMonitor

        search_urls = self._search_urls()

        monitor = AccountGameMonitor(
            data_dir=StarTools.get_data_dir("astrbot_plugin_majsoul_matchreport"),
            account_store_path=_accounts_path(),
            get_targets=self._monitor_targets,
            on_game_finished=self._on_game_finished,
            # T2「非好友轮询间隔」：非好友的局掉出观战列表后按这个间隔查牌谱
            poll_interval=max(30, int(self.config.get("poll_interval", 120))),
            # T1「好友轮询间隔」：好友的局每 T1 秒看一次观战入口还在不在
            friend_poll_interval=max(10, int(self.config.get("friend_poll_interval", 30))),
            daily_limit=int(self.config.get("daily_paipu_limit", 200)),
            nonfriend_daily_limit=int(self.config.get("nonfriend_daily_limit", 2)),
            monitor_three_player=bool(self.config.get("monitor_three_player", True)),
            monitor_east=bool(self.config.get("monitor_east", True)),
            auto_approve_friends=bool(self.config.get("auto_approve_friends", True)),
            log_event=self._log_system,
            search_urls=search_urls,
            server_url=self._server_url(),
            # 每个玩家是否要播报这局，交给插件按「监控他的群开了哪些播报」判断
            wants_game=self._wants_game,
        )
        await monitor.start()
        self._monitor = monitor

    def _log_system(self, status: str, detail: str = "", level: str = "INFO") -> None:
        """把插件级事件写进查询日志（SYSTEM 目标），WebUI「查询日志」可见。"""
        try:
            self._write_query_log("SYSTEM", status, detail, level=level)
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 写查询日志失败: {exc}")

    def _monitor_validity(self) -> tuple:
        """当前「监控有效期」设置 -> (数值, 单位)。"""
        unit = str(self.config.get("monitor_valid_unit", MONITOR_VALID_DEFAULT_UNIT) or "").strip().lower()
        if unit not in MONITOR_VALID_UNITS:
            unit = MONITOR_VALID_DEFAULT_UNIT
        try:
            value = int(self.config.get("monitor_valid_value", MONITOR_VALID_DEFAULT_VALUE))
        except (TypeError, ValueError):
            value = MONITOR_VALID_DEFAULT_VALUE
        value = max(1, min(MONITOR_VALID_MAX_VALUE, value))
        return value, unit

    def _group_added_at(self, info: Dict, group_id: Any = "") -> int:
        """某个群的监控时间；这个群没单独记过就退回玩家级的 added_at（老数据）。"""
        gid = str(group_id or "").strip()
        mapping = info.get("group_added_at") or {}
        if gid and isinstance(mapping, dict):
            try:
                ts = int(mapping.get(gid, 0) or 0)
            except (TypeError, ValueError):
                ts = 0
            if ts:
                return ts
        try:
            return int(info.get("added_at", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _group_expire_ts(self, info: Dict, group_id: Any = "") -> float:
        """某个群的到期时间戳；0 表示永不过期（监控时间为 - 的老数据）。"""
        value, unit = self._monitor_validity()
        return monitor_expire_at(self._group_added_at(info, group_id), value, unit)

    def _group_expired(self, info: Dict, group_id: Any = "", now: float = 0) -> bool:
        """这个群的监控是否已过期。"""
        expire_at = self._group_expire_ts(info, group_id)
        if not expire_at:
            return False
        return (now or time.time()) >= expire_at

    def _monitor_expire_ts(self, info: Dict) -> float:
        """这个监控玩家最早的到期时间（兼容旧接口）；没有绑定群时按玩家 added_at 算。"""
        gids = [str(g) for g in (info.get("group_ids") or []) if str(g or "").strip()]
        if not gids:
            value, unit = self._monitor_validity()
            return monitor_expire_at(self._group_added_at(info, ""), value, unit)
        stamps = [ts for ts in (self._group_expire_ts(info, g) for g in gids) if ts]
        return min(stamps) if stamps else 0

    def _monitor_expired(self, info: Dict, now: float = 0) -> bool:
        """所有绑定群都过期了才算这个玩家过期；没有绑定群的按玩家时间判断。"""
        current = now or time.time()
        gids = [str(g) for g in (info.get("group_ids") or []) if str(g or "").strip()]
        if not gids:
            return self._group_expired(info, "", current)
        return all(self._group_expired(info, g, current) for g in gids)

    def _expiry_warning(self, info: Dict, group_ids: Optional[List] = None) -> str:
        """距监控到期不到 3 天时的播报尾巴提示；按本次要发的群里最紧的那个算。"""
        gids = list(group_ids) if group_ids is not None else list(info.get("group_ids") or [])
        if not gids:
            gids = [""]
        now = time.time()
        remaining_min = None
        for gid in gids:
            expire_at = self._group_expire_ts(info, gid)
            if not expire_at:
                continue
            remaining = expire_at - now
            if remaining <= 0:
                continue
            if remaining_min is None or remaining < remaining_min:
                remaining_min = remaining
        if remaining_min is None or remaining_min >= 3 * 86400:
            return ""
        days = max(1, int(remaining_min // 86400))
        add_name = self._command_names()[0]
        return f"该玩家播报时限仅剩{days}天，请重新使用/{add_name}指令重置期限"

    # ── 各群播报开关（三麻 / 东场 / 友人 / 比赛）─────────────
    def _group_display_name(self, group_id: Any) -> str:
        """群名（没记录过就返回空串）。网页上显示「群名 群号」用。"""
        gid = str(group_id or "").strip()
        if not gid:
            return ""
        return str((self._group_names.get(gid) or {}).get("name") or "")

    async def _remember_group_name(self, event: AstrMessageEvent, group_id: Any) -> None:
        """顺手把群名记下来（同群 6 小时内只拉一次；失败 10 分钟内不重试）。"""
        gid = str(group_id or "").strip()
        if not gid:
            return
        now = time.time()
        cached = self._group_names.get(gid) or {}
        if cached.get("name") and now - float(cached.get("at") or 0) < 6 * 3600:
            return
        if now - float(self._group_name_try.get(gid) or 0) < 600:
            return
        self._group_name_try[gid] = now
        getter = getattr(event, "get_group", None)
        if not callable(getter):
            return
        try:
            group = await getter()
        except Exception:
            return
        name = str(getattr(group, "group_name", "") or "").strip() if group is not None else ""
        if not name or name == cached.get("name"):
            return
        self._group_names[gid] = {"name": name, "at": now}
        try:
            save_group_names(self._group_names)
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 保存群名缓存失败: {exc}")

    async def _sync_group_names(self, force: bool = False) -> int:
        """从平台拉一次「群列表」，把群号→群名整批补齐。

        以前只能靠群里发消息时顺手记（没发过消息的群就没有群名），
        这里直接问平台要群列表，没发过消息的群也能拿到群名。
        返回这次新写入/更新的群数量。
        """
        now = time.time()
        # 普通调用 10 分钟最多一次；强制同步（发现有群还没群名）也至少间隔 1 分钟，避免反复请求
        min_gap = 60 if force else 600
        if now - float(self._group_names_synced_at or 0) < min_gap:
            return 0
        self._group_names_synced_at = now

        insts = []
        try:
            manager = getattr(self.context, "platform_manager", None)
            insts = list(getattr(manager, "platform_insts", None) or [])
        except Exception:
            insts = []
        if not insts:
            return 0

        updated = 0
        for platform in insts:
            client = None
            try:
                getter = getattr(platform, "get_client", None)
                if callable(getter):
                    client = getter()
            except Exception:
                client = None
            if client is None or not hasattr(client, "call_action"):
                continue
            try:
                groups = await client.call_action("get_group_list")
            except Exception as exc:
                logger.debug(f"[MajsoulMonitor] 拉取群列表失败，跳过这个平台: {exc}")
                continue
            for item in groups or []:
                gid = str(notice_field(item, "group_id") or "").strip()
                name = str(notice_field(item, "group_name") or "").strip()
                if not gid or not name:
                    continue
                cached = self._group_names.get(gid) or {}
                if cached.get("name") == name:
                    continue
                self._group_names[gid] = {"name": name, "at": now}
                updated += 1
        if updated:
            try:
                save_group_names(self._group_names)
            except Exception as exc:
                logger.warning(f"[MajsoulMonitor] 保存群名缓存失败: {exc}")
            logger.info(f"[MajsoulMonitor] 已同步 {updated} 个群名")
        return updated

    def _default_broadcast(self, kind: str) -> bool:
        """插件设置里的默认开关：所有群的初始默认值。"""
        key = BROADCAST_DEFAULT_KEYS.get(kind, "")
        default = BROADCAST_DEFAULT_VALUES.get(kind, True)
        if not key:
            return default
        return bool(self.config.get(key, default))

    def _group_settings_of(self, group_id: Any) -> Dict[str, bool]:
        """某个群的四个播报开关（没单独设置过的取插件设置里的默认值）。"""
        gid = str(group_id or "").strip()
        stored = self.group_settings.get(gid) if gid else None
        result: Dict[str, bool] = {}
        for kind in BROADCAST_KINDS:
            if isinstance(stored, dict) and kind in stored:
                try:
                    result[kind] = bool(int(stored.get(kind)))
                except (TypeError, ValueError):
                    result[kind] = bool(stored.get(kind))
            else:
                result[kind] = self._default_broadcast(kind)
        return result

    def _group_setting(self, group_id: Any, kind: str) -> bool:
        return bool(self._group_settings_of(group_id).get(kind, False))

    def _set_group_setting(self, group_id: Any, kind: str, enabled: bool) -> bool:
        """写入某个群的开关并落盘；返回是否真的发生了变化。"""
        gid = str(group_id or "").strip()
        if not gid or kind not in BROADCAST_KINDS:
            return False
        current = self._group_settings_of(gid)
        if current.get(kind) == bool(enabled):
            return False
        values = dict(self.group_settings.get(gid) or {})
        values[kind] = 1 if enabled else 0
        self.group_settings[gid] = values
        try:
            save_group_settings(self.group_settings)
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 保存群播报设置失败: {exc}")
        return True

    def _group_wants_ranked(self, group_id: Any, mode_id: Any) -> bool:
        """这个群要不要播报这一场段位场（三麻看三麻开关、东场看东场开关）。"""
        settings = self._group_settings_of(group_id)
        try:
            mode = int(mode_id or 0)
        except (TypeError, ValueError):
            mode = 0
        if mode in THREE_PLAYER_MODE_IDS and not settings.get("three", True):
            return False
        if mode in EAST_MODE_IDS and not settings.get("east", True):
            return False
        return True

    def _group_wants_unranked(self, group_id: Any, kind: str) -> bool:
        """这个群要不要播报友人场 / 比赛场（这两种只对好友播报）。"""
        if kind not in ("friend", "contest"):
            return False
        return self._group_setting(group_id, kind)

    def _monitored_groups(self, entry: Dict, now: float = 0) -> List[str]:
        """这个玩家还在有效期内的群。"""
        current = now or time.time()
        groups: List[str] = []
        for gid in entry.get("group_ids") or []:
            gid_s = str(gid or "").strip()
            if not gid_s or gid_s in groups:
                continue
            if self._group_expired(entry, gid_s, current):
                continue
            groups.append(gid_s)
        return groups

    def _player_entry(self, account_id: int, nickname: str) -> Optional[Dict]:
        """按 account_id / 昵称找到监控列表里的玩家条目。"""
        key = self._match_monitored(int(account_id or 0), str(nickname or ""))
        if not key:
            return None
        entry = self.monitored.get(key)
        return entry if isinstance(entry, dict) else None

    def _wants_game(
        self,
        target: Dict,
        mode_id: int,
        contest_uid: str = "",
        room_id: int = 0,
    ) -> bool:
        """监控端的判断回调：这个玩家这一局有没有群要播报。

        - 段位场：看监控该玩家的群有没有开三麻 / 东场播报；
        - 友人场 / 比赛场：看有没有群开了友人 / 比赛播报
          （这两种对局只能从好友列表里发现，非好友本来就看不到，不用再判断好友关系）。
        """
        if not isinstance(target, dict):
            return False
        account_id = int(target.get("account_id") or 0)
        nickname = str(target.get("nickname") or "")
        entry = self._player_entry(account_id, nickname)
        if not entry:
            return False
        groups = self._monitored_groups(entry)
        if not groups:
            return False
        contest = str(contest_uid or "").strip()
        try:
            room = int(room_id or 0)
        except (TypeError, ValueError):
            room = 0
        if contest or room > 0:
            kind = "contest" if contest else "friend"
            return any(self._group_wants_unranked(gid, kind) for gid in groups)
        return any(self._group_wants_ranked(gid, mode_id) for gid in groups)

    def _monitor_targets(self) -> List[Dict]:
        """当前需要监控的账号列表（跳过已过期；兼容老数据的 player_id 字段）。"""
        targets = []
        now = time.time()
        for nickname, info in list(self.monitored.items()):
            if self._monitor_expired(info, now):
                continue
            try:
                account_id = int(info.get("account_id") or info.get("player_id") or 0)
            except (TypeError, ValueError):
                account_id = 0
            if account_id:
                targets.append({"account_id": account_id, "nickname": nickname})
        return targets

    def _monitor_status(self) -> dict:
        if self._monitor is None:
            return {
                "connected": False,
                "uid": "",
                "targets": 0,
                "playing": [],
                "last_error": "未启动",
            }
        return self._monitor.status()



    def _register_web_apis(self):
        """注册 WebUI API 端点"""
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/list",
            self._api_list_monitored, ["GET"],
            "获取所有监控列表"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/delete",
            self._api_delete_monitored, ["POST"],
            "删除监控玩家"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/groups",
            self._api_update_groups, ["POST"],
            "修改监控玩家绑定群号"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/update",
            self._api_update_monitored, ["POST"],
            "更新监控玩家监控时间"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/add",
            self._api_add_monitored, ["POST"],
            "添加监控玩家"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/config",
            self._api_get_config, ["GET"],
            "获取插件配置"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/config",
            self._api_save_config, ["POST"],
            "保存插件配置"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/group_settings",
            self._api_get_group_settings, ["GET"],
            "获取各群播报开关"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/group_settings",
            self._api_save_group_settings, ["POST"],
            "保存各群播报开关"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/theme",
            self._api_get_theme, ["GET"],
            "获取页面主题"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/theme",
            self._api_save_theme, ["POST"],
            "保存页面主题"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/test_node",
            self._api_test_node, ["POST"],
            "测试牌谱屋API节点可用性"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/logs",
            self._api_list_logs, ["GET"],
            "获取日志文件列表"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/log_content",
            self._api_get_log_content, ["GET"],
            "获取指定日志文件内容"
        )
        # ===== 账号池 API =====
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/accounts",
            self._api_accounts_list, ["GET"],
            "获取账号池列表"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/accounts/login",
            self._api_accounts_login, ["POST"],
            "登录雀魂并添加到账号池"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/accounts/delete",
            self._api_accounts_delete, ["POST"],
            "从账号池删除账号"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/accounts/toggle",
            self._api_accounts_toggle, ["POST"],
            "启用/禁用账号"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/accounts/reorder",
            self._api_accounts_reorder, ["POST"],
            "调整账号使用顺序"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/accounts/restore_perm",
            self._api_accounts_restore_perm, ["POST"],
            "恢复账号牌谱权限"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/friends",
            self._api_friends, ["GET"],
            "各在线账号的好友列表"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/friend_remove",
            self._api_friend_remove, ["POST"],
            "删除好友"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/group_names/sync",
            self._api_sync_group_names, ["POST"],
            "从平台同步全部群名"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/monitor_status",
            self._api_monitor_status, ["GET"],
            "对局监控运行状态"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/live_games",
            self._api_live_games, ["GET"],
            "所有进行中的对局"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/game_result",
            self._api_game_result, ["GET"],
            "查询单局对局结果"
        )
        self.context.register_web_api(
            "/astrbot_plugin_majsoul_matchreport/api/uuid_links",
            self._api_uuid_links, ["GET"],
            "生成观战/牌谱链接"
        )

    async def _api_monitor_status(self):
        """API: 返回对局监控状态（连接、监控人数、正在对局的玩家）"""
        return jsonify({"ok": True, "data": self._monitor_status()})

    async def _api_live_games(self):
        """API: 返回进行中的对局（观战列表，按房间/人数-场风筛选）"""
        room = (request.args.get("room") or "").strip() or "王座间"
        direction = (request.args.get("direction") or "").strip() or "四人南"
        if self._monitor is None:
            return jsonify({"ok": False, "message": "监控未启动，请先登录账号"})

        mode_id = ROOM_MODE_IDS.get((room, direction))
        if mode_id is None:
            return jsonify({"ok": False, "message": f"不支持的筛选: {room} {direction}"})

        # 客户端观战列表的筛选参数 = 200 + mode_id（王座南是 216）
        filter_id = 200 + mode_id
        include_old = (request.args.get("include_old") or "1").strip() != "0"
        logger.info(
            f"[MajsoulMonitor] live_games 请求: room={room} direction={direction} "
            f"filter_id={filter_id} include_old={include_old} monitor={'on' if self._monitor else 'off'}"
        )
        try:
            result = await self._monitor.list_live_games(filter_id, include_old=include_old)
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] live_games 失败: {exc.__class__.__name__}: {exc}", exc_info=True)
            return jsonify({"ok": False, "message": f"{exc.__class__.__name__}: {exc}"})

        games = []
        for game in result.get("games", []):
            game_mode_id = int(game.get("mode_id") or mode_id)
            if game_mode_id != mode_id:
                continue
            players = []
            for player in game.get("players", []):
                level_id = int(player.get("level_id") or 0)
                tier, tier_name = level_rank(level_id)
                players.append({
                    "account_id": player.get("account_id", 0),
                    "nickname": player.get("nickname", ""),
                    "level_id": level_id,
                    "rank_id": tier,
                    "rank": tier_name,
                })
            games.append({
                "uuid": game.get("uuid", ""),
                "start_time": int(game.get("start_time") or 0),
                "mode_id": game_mode_id,
                "mode_label": MODE_LABELS.get(game_mode_id, "其他对局"),
                "live": bool(game.get("live", True)),
                "players": players,
            })
        games.sort(key=lambda item: item["start_time"], reverse=True)
        logger.info(f"[MajsoulMonitor] live_games 返回 {len(games)} 局（filter_id={filter_id}）")
        return jsonify({
            "ok": True,
            "data": games,
            "source": "live",
            "message": result.get("message", ""),
        })

    def _result_payload_from_record(self, uuid: str, head) -> Dict:
        mode_id = int(getattr(getattr(getattr(head, "config", None), "meta", None), "mode_id", 0) or 0)
        accounts = list(getattr(head, "accounts", []) or [])
        name_by_seat = {int(a.seat): str(getattr(a, "nickname", "") or "") for a in accounts}
        id_by_seat = {int(a.seat): int(getattr(a, "account_id", 0) or 0) for a in accounts}
        use_level3 = mode_id in THREE_PLAYER_MODE_IDS
        level_by_seat = {}
        for acc in accounts:
            seat = int(getattr(acc, "seat", -1))
            level = getattr(acc, "level3" if use_level3 else "level", None)
            level_by_seat[seat] = int(getattr(level, "id", 0) or 0)
        result_players = list(getattr(getattr(head, "result", None), "players", []) or [])
        parts = [int(getattr(p, "part_point_1", 0) or 0) for p in result_players]
        use_parts = any(parts)
        points = []
        for player, part in zip(result_players, parts):
            seat = int(getattr(player, "seat", -1))
            if seat < 0:
                continue
            # part_point_1 才是最终点数（四麻总和 100000 / 三麻 105000）；
            # total_point 是精算后的分数增减（总和 0），仅在没有 part_point_1 的老数据里兜底
            point = part if use_parts else int(getattr(player, "total_point", 0) or 0)
            points.append({"seat": seat, "point": point})
        points.sort(key=lambda item: item["point"], reverse=True)
        players = []
        for idx, item in enumerate(points, 1):
            seat = item["seat"]
            players.append({
                "rank": idx,
                "seat": seat,
                "point": item["point"],
                "nickname": name_by_seat.get(seat, ""),
                "account_id": id_by_seat.get(seat, 0),
                "level_id": level_by_seat.get(seat, 0),
            })
        return {
            "uuid": uuid,
            "ended": True,
            "mode_id": mode_id,
            "start_time": int(getattr(head, "start_time", 0) or 0),
            "players": players,
        }

    async def _api_game_result(self):
        """API: 查询单局结果（已结束返回顺位/点数，未结束返回 ended=False）

        cache_only=1 时不发起任何牌谱查询，只读监控侧已经拉到的结果：
        页面在「等待结束」期间轮询这个接口，避免每 10 秒消耗一次查询次数。
        """
        uuid = (request.args.get("uuid") or "").strip()
        if not uuid:
            return jsonify({"ok": False, "message": "缺少对局 uuid"})
        cache_only = (request.args.get("cache_only") or "").strip().lower() not in ("", "0", "false", "no")
        account_id = int(request.args.get("account_id") or 0)
        cache = getattr(self, "_game_result_cache", None)
        if cache is None:
            cache = {}
            self._game_result_cache = cache
        if uuid in cache:
            return jsonify({"ok": True, "data": cache[uuid]})
        if self._monitor is None:
            return jsonify({"ok": False, "message": "监控未启动，请先登录账号"})
        head = None
        # 页面在等这局结果：牌谱拉到后给页面留一份，轮询时直接读
        try:
            self._monitor.watch_result(uuid)
        except Exception:
            pass
        mode_id = int(request.args.get("mode_id") or 0)
        start_time = int(request.args.get("start_time") or 0)
        if cache_only:
            # 只读监控侧结果：待查对局拉到的牌谱会暂存在 monitor.manual_results
            head = self._monitor.take_manual_result(uuid)
            if head is None:
                return jsonify({
                    "ok": True,
                    "data": {
                        "uuid": uuid,
                        "ended": False,
                        "pending": self._monitor.has_pending(uuid),
                    },
                })
        elif self._monitor.is_game_live(uuid):
            # 对局还在进行中：不浪费一次牌谱查询，直接交给待查列表
            added = False
            try:
                added = self._monitor.add_manual_pending(uuid, mode_id, start_time, account_id)
            except Exception as exc:
                logger.debug(f"[MajsoulMonitor] 加入待查牌谱失败: {exc}")
            self._write_query_log(
                "SYSTEM", "手动查询对局",
                f"uuid={uuid} 对局进行中" + ("，已加入待查列表" if added else "，已在待查列表中"),
                "INFO",
            )
            return jsonify({
                "ok": True,
                "data": {
                    "uuid": uuid,
                    "ended": False,
                    "added": added,
                    "pending": self._monitor.has_pending(uuid),
                    "message": "对局还在进行中，结束后自动获取结果",
                },
            })
        else:
            try:
                head = await self._monitor.fetch_record(uuid, quiet=True)
            except Exception as exc:
                return jsonify({"ok": False, "message": f"{exc.__class__.__name__}: {exc}"})
        if head is None:
            code = getattr(self._monitor, "last_record_code", 0)
            added = False
            try:
                added = self._monitor.add_manual_pending(uuid, mode_id, start_time, account_id)
            except Exception as exc:
                logger.debug(f"[MajsoulMonitor] 加入待查牌谱失败: {exc}")
            self._write_query_log(
                "SYSTEM", "手动查询对局",
                f"uuid={uuid} 尚未结束" + ("，已加入待查列表" if added else ""),
                "INFO",
            )
            return jsonify({
                "ok": True,
                "data": {
                    "uuid": uuid,
                    "ended": False,
                    "added": added,
                    "pending": self._monitor.has_pending(uuid),
                    "message": (
                        "牌谱查询请求异常，稍后会自动重试"
                        if code < 0
                        else f"对局尚未结束（code={code}）"
                    ),
                },
            })
        payload = self._result_payload_from_record(uuid, head)
        cache[uuid] = payload
        if len(cache) > 500:
            for key in list(cache)[: len(cache) - 500]:
                cache.pop(key, None)
        return jsonify({"ok": True, "data": payload})

    async def _api_uuid_links(self):
        """API: 生成观战和牌谱的完整链接。"""
        uuid = (request.args.get("uuid") or "").strip()
        account_id = int(request.args.get("account_id") or 0)
        if not uuid:
            return jsonify({"ok": False, "message": "缺少对局 uuid"})
        base = self._server_url().rstrip("/") + "/1/?paipu="
        spectate = base + uuid
        paipu = build_paipu_url(uuid, account_id, self._server_url()) if account_id else base + uuid
        return jsonify({"ok": True, "data": {"spectate": spectate, "paipu": paipu}})

    async def _api_list_monitored(self):
        """API: 返回所有监控玩家及群信息（含今日播报次数、是否已是好友）"""
        # 先把好友快照刷到足够新，网页一打开就能显示「好友 / 非好友」，不用手动点刷新
        if self._monitor is not None:
            try:
                await self._monitor.ensure_friends_fresh()
            except Exception as exc:
                logger.debug(
                    f"[MajsoulMonitor] 刷新好友快照失败（沿用旧快照判定好友）："
                    f"{exc.__class__.__name__}: {exc}"
                )
        result = []
        # 打开监控列表就顺手同步一次群名（有群还没群名时立刻补，否则 10 分钟内最多一次）
        try:
            bound = {str(g) for ent in self.monitored.values() for g in (ent.get("group_ids") or [])}
            missing = any(gid and not self._group_display_name(gid) for gid in bound)
            await self._sync_group_names(force=missing)
        except Exception as exc:
            logger.debug(f"[MajsoulMonitor] 补充群名失败（不影响列表）：{exc}")
        broadcast = self._monitor.broadcast_snapshot() if self._monitor is not None else {}
        counts = broadcast.get("counts") or {}
        for nick, info in self.monitored.items():
            ts = info.get("latest_ts", 0)
            played_at = int(info.get("last_played_at", 0) or 0)
            added_at = int(info.get("added_at", 0) or 0)
            expire_at = self._monitor_expire_ts(info)
            account_id = int(info.get("account_id") or info.get("player_id") or 0)
            is_friend = bool(self._monitor and self._monitor.is_friend_of_any(account_id))
            groups = []
            for gid in info.get("group_ids") or []:
                gid_s = str(gid)
                g_added = self._group_added_at(info, gid_s)
                g_expire = self._group_expire_ts(info, gid_s)
                groups.append({
                    "group_id": gid_s,
                    # 群名（收到过该群消息才会记录，没记录时为空串）
                    "group_name": self._group_display_name(gid_s),
                    "added_at": g_added,
                    "added_time": datetime.fromtimestamp(g_added).strftime("%Y-%m-%d %H:%M:%S") if g_added else "",
                    "expire_time": datetime.fromtimestamp(g_expire).strftime("%Y-%m-%d %H:%M:%S") if g_expire else "",
                    "expired": self._group_expired(info, gid_s),
                })
            result.append({
                "nickname": nick,
                "account_id": account_id,
                # 雀魂里显示的数字 ID（加好友用的那个），跟日志/网页里看到的 account_id 不同
                "display_id": encode_account_id(account_id) if account_id else 0,
                "player_id": info.get("player_id", 0),
                "latest_ts": ts,
                "latest_time": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "",
                "last_game_uuid": info.get("last_game_uuid", ""),
                "last_played_time": datetime.fromtimestamp(played_at).strftime("%Y-%m-%d %H:%M:%S") if played_at else "",
                "added_at": added_at,
                "added_time": datetime.fromtimestamp(added_at).strftime("%Y-%m-%d %H:%M:%S") if added_at else "",
                "expired": self._monitor_expired(info),
                "expire_time": datetime.fromtimestamp(expire_at).strftime("%Y-%m-%d %H:%M:%S") if expire_at else "",
                "groups": groups,
                "recent_ranks": "".join(recent_ranks_of(info, False)),
                "recent_ranks_three": "".join(recent_ranks_of(info, True)),
                "mode": info.get("mode", "pl4"),
                "group_ids": info.get("group_ids", []),
                "today_broadcast": int(counts.get(str(account_id), 0) or 0),
                "is_friend": is_friend,
                "nonfriend_limit": int(broadcast.get("limit", -1)),
            })
        return jsonify({"ok": True, "data": result})

    async def _api_friends(self):
        """API: 每个在线账号的好友列表（「好友列表」标签页用）"""
        if self._monitor is None:
            return jsonify({"ok": False, "message": "监控未启动，请先登录账号"})
        accounts = self._monitor.friends_snapshot()
        # account_id -> 有几个群在播报这个玩家（好友列表的「播报群数」列），
        # 顺便记下具体是哪几个群，右键点数字时列出来
        monitor_groups: Dict[int, int] = {}
        monitor_group_ids: Dict[int, List[str]] = {}
        for entry in self.monitored.values():
            aid_mon = int(entry.get("account_id") or entry.get("player_id") or 0)
            if not aid_mon:
                continue
            try:
                groups = self._monitored_groups(entry)
            except Exception:
                groups = []
            if groups:
                monitor_groups[aid_mon] = len(groups)
                monitor_group_ids[aid_mon] = groups
        for acc in accounts:
            try:
                acc["display_uid"] = encode_account_id(int(acc.get("uid") or 0))
            except Exception:
                acc["display_uid"] = 0
            for friend in acc.get("friends", []):
                aid_friend = int(friend.get("account_id") or 0)
                friend["monitor_groups"] = int(monitor_groups.get(aid_friend, 0))
                gids = list(monitor_group_ids.get(aid_friend, []))
                friend["monitor_group_ids"] = gids
                # 和 group_ids 一一对应：群名（没记录过就是空串，页面只显示群号）
                friend["monitor_group_names"] = [self._group_display_name(g) for g in gids]
                try:
                    _tier, tier_name = level_rank(int(friend.get("level_id") or 0))
                except Exception:
                    tier_name = ""
                friend["rank"] = tier_name
                try:
                    _tier3, tier_name3 = level_rank(int(friend.get("level3_id") or 0))
                except Exception:
                    tier_name3 = ""
                friend["rank3"] = tier_name3
                try:
                    friend["display_id"] = encode_account_id(int(friend.get("account_id") or 0))
                except Exception:
                    friend["display_id"] = 0
        return jsonify({
            "ok": True,
            "accounts": accounts,
        })

    async def _api_friend_remove(self):
        """API: 删除好友（好友列表页每行的删除按钮）"""
        if self._monitor is None:
            return jsonify({"ok": False, "message": "监控未启动，请先登录账号"})
        data = await request.get_json(silent=True) or {}
        try:
            account_id = int(data.get("account_id") or 0)
        except (TypeError, ValueError):
            account_id = 0
        nickname = ""
        try:
            nickname = self._monitor.friend_nickname(account_id) or ""
        except Exception:
            nickname = ""
        ok, message = await self._monitor.remove_friend(account_id)
        if ok:
            who = f"{nickname}({show_uid(account_id)})" if nickname else show_uid(account_id)
            logger.info(f"[MajsoulMonitor] 删除好友: {who}")
            return jsonify({"ok": True, "message": "✅ " + message})
        return jsonify({"ok": False, "message": "❌ " + message})

    async def _api_sync_group_names(self):
        """API: 从平台拉一遍群列表，把所有群的群名补齐（网页「同步群名」按钮）"""
        try:
            updated = await self._sync_group_names(force=True)
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 同步群名失败: {exc}")
            return jsonify({"ok": False, "message": f"同步失败: {exc}"})
        total = len(self._group_names)
        if updated:
            message = f"已更新 {updated} 个群名（共记录 {total} 个群）"
        else:
            message = f"群名没有变化（已记录 {total} 个群）"
        return jsonify({"ok": True, "message": message, "data": {"updated": updated, "total": total}})

    def _remove_group_monitoring(self, group_id: str, reason: str = "") -> Dict[str, int]:
        """机器人离开某个群后，把这个群相关的监控和播报设置全部删掉。

        返回 {"players", "removed", "settings"}：
        players=受影响（原本监控在这个群）的玩家数，removed=因此彻底不再监控的玩家数，
        settings=是否顺带清掉了这个群的播报设置（1/0）。
        """
        gid = str(group_id or "").strip()
        stats = {"players": 0, "removed": 0, "settings": 0}
        if not gid:
            return stats
        changed = False
        drop_keys: List[str] = []
        for key, entry in list(self.monitored.items()):
            if not isinstance(entry, dict):
                continue
            gids = []
            for g in (entry.get("group_ids") or []):
                text = str(g or "").strip()
                if text and text not in gids:
                    gids.append(text)
            if gid not in gids:
                continue
            stats["players"] += 1
            changed = True
            rest = [g for g in gids if g != gid]
            entry["group_ids"] = rest
            mapping = entry.get("group_added_at")
            if isinstance(mapping, dict) and gid in mapping:
                mapping = dict(mapping)
                mapping.pop(gid, None)
                entry["group_added_at"] = mapping
            if not rest:
                drop_keys.append(key)
        for key in drop_keys:
            entry = self.monitored.pop(key, None) or {}
            stats["removed"] += 1
            account_id = int(entry.get("account_id") or entry.get("player_id") or 0)
            if self._monitor is not None:
                try:
                    self._monitor.drop_pending_for_player(account_id, key)
                except Exception as exc:
                    logger.debug(f"[MajsoulMonitor] 清理待查牌谱失败: {exc}")
        if self.group_settings.pop(gid, None) is not None:
            stats["settings"] = 1
            changed = True
        if not changed:
            return stats
        try:
            save_monitored(self.monitored)
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 保存监控列表失败: {exc}")
        try:
            save_group_settings(self.group_settings)
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 保存群播报设置失败: {exc}")
        detail = (
            f"群 {gid}（{reason or '不在群里'}）：清理 {stats['players']} 个玩家的监控，"
            f"其中 {stats['removed']} 个玩家已无其他群、停止监控"
            + ("，并删除该群的播报设置" if stats["settings"] else "")
        )
        self._write_query_log("SYSTEM", "退群清理监控", detail, "INFO")
        logger.info(f"[MajsoulMonitor] {detail}")
        return stats

    async def _api_delete_monitored(self):
        """API: 删除指定监控玩家；带 group_id 时只删这个群的监控（删完没群了才删玩家）。"""
        data = await request.get_json(silent=True) or {}
        nickname = data.get("nickname", "")
        if not nickname or nickname not in self.monitored:
            return jsonify({"ok": False, "message": "玩家不在监控列表中"}), 200
        info = self.monitored[nickname]
        group_id = str(data.get("group_id") or "").strip()
        if group_id:
            gids = [str(g) for g in (info.get("group_ids") or [])]
            if group_id not in gids:
                return jsonify({"ok": False, "message": "这个群不在该玩家的监控列表里"}), 200
            gids.remove(group_id)
            info["group_ids"] = gids
            mapping = info.get("group_added_at")
            if isinstance(mapping, dict):
                mapping = dict(mapping)
                mapping.pop(group_id, None)
                info["group_added_at"] = mapping
            if gids:
                save_monitored(self.monitored)
                self._write_query_log(
                    "SYSTEM", "删除群监控",
                    f"{nickname} 群 {group_id}，剩余 {','.join(gids)}", "INFO",
                )
                logger.info(f"[MajsoulMonitor] 删除群监控: {nickname} 群 {group_id}")
                return jsonify({"ok": True, "message": f"已删除 {nickname} 在群 {group_id} 的监控"})
        account_id = int(info.get("account_id") or info.get("player_id") or 0)
        del self.monitored[nickname]
        save_monitored(self.monitored)
        if self._monitor is not None:
            try:
                self._monitor.drop_pending_for_player(account_id, nickname)
            except Exception as exc:
                logger.debug(f"[MajsoulMonitor] 清理待查牌谱失败: {exc}")
        self._write_query_log(
            "SYSTEM", "删除监控玩家", f"{nickname}({show_uid(account_id)})", "INFO"
        )
        logger.info(f"[MajsoulMonitor] 删除监控玩家: {nickname}({show_uid(account_id)})")
        return jsonify({"ok": True, "message": f"已删除 {nickname}"})

    async def _api_update_monitored(self):
        """API: 把某个玩家的监控时间刷新为当前时间（同时取消过期）"""
        data = await request.get_json(silent=True) or {}
        nickname = str(data.get("nickname") or "").strip()
        if not nickname:
            return jsonify({"ok": False, "message": "缺少玩家昵称"})
        info = self.monitored.get(nickname)
        if info is None:
            # 容忍大小写差异
            for key, value in self.monitored.items():
                if key.strip().lower() == nickname.lower():
                    nickname, info = key, value
                    break
        if info is None and nickname.isdigit():
            # 也接受页面/日志里显示的雀魂数字 ID
            target_id = int(nickname)
            for key, value in self.monitored.items():
                try:
                    aid = int(value.get("account_id") or value.get("player_id") or 0)
                except (TypeError, ValueError):
                    aid = 0
                if aid and (aid == target_id or encode_account_id(aid) == target_id):
                    nickname, info = key, value
                    break
        if info is None:
            return jsonify({"ok": False, "message": "玩家不在监控列表中"})
        now = int(time.time())
        group_id = str(data.get("group_id") or "").strip()
        if group_id:
            gids = [str(g) for g in (info.get("group_ids") or [])]
            if group_id not in gids:
                return jsonify({"ok": False, "message": "这个群不在该玩家的监控列表里"})
            mapping = info.get("group_added_at")
            mapping = dict(mapping) if isinstance(mapping, dict) else {}
            mapping[group_id] = now
            info["group_added_at"] = mapping
            target_text = f"{nickname}（群 {group_id}）"
        else:
            info["added_at"] = now
            target_text = nickname
        save_monitored(self.monitored)
        expire_at = self._group_expire_ts(info, group_id) if group_id else monitor_expire_at(now, *self._monitor_validity())
        self._write_query_log(
            "SYSTEM", "更新监控时间",
            f"{target_text} -> {datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S')}",
            "INFO",
        )
        logger.info(f"[MajsoulMonitor] 更新监控时间: {target_text}")
        return jsonify({
            "ok": True,
            "message": "监控时间已更新为当前时间",
            "data": {
                "nickname": nickname,
                "group_id": group_id,
                "added_at": now,
                "added_time": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
                "expired": False,
                "expire_time": datetime.fromtimestamp(expire_at).strftime("%Y-%m-%d %H:%M:%S") if expire_at else "",
            },
        })

    async def _api_update_groups(self):
        """API: 修改某个监控玩家的绑定群号（中英文逗号/空格分隔）"""
        data = await request.get_json(silent=True) or {}
        nickname = (data.get("nickname") or "").strip()
        raw = data.get("group_ids", data.get("groups", ""))
        if not nickname or nickname not in self.monitored:
            return jsonify({"ok": False, "message": "玩家不在监控列表中"})
        if isinstance(raw, str):
            text = raw.replace("，", ",")
            for sep in ("\r", "\n", "\t", " "):
                text = text.replace(sep, ",")
            parts = text.split(",")
        elif isinstance(raw, list):
            parts = [str(x) for x in raw]
        else:
            parts = []
        gids = []
        for part in parts:
            part = part.strip()
            if part and part not in gids:
                gids.append(part)
        info = self.monitored[nickname]
        info["group_ids"] = gids
        # 新加的群从当前时间开始算监控时长；被删掉的群清掉单独的监控时间
        mapping = info.get("group_added_at")
        mapping = dict(mapping) if isinstance(mapping, dict) else {}
        now = int(time.time())
        for gid in gids:
            if gid not in mapping:
                mapping[gid] = now
        info["group_added_at"] = {gid: ts for gid, ts in mapping.items() if gid in gids}
        save_monitored(self.monitored)
        self._write_query_log(
            "SYSTEM", "修改绑定群号",
            f"{nickname} -> {','.join(gids) if gids else '未绑定'}",
            "INFO",
        )
        logger.info(f"[MajsoulMonitor] {nickname} 绑定群号改为 {gids}")
        return jsonify({"ok": True, "message": "群号已更新", "data": {"group_ids": gids}})

    async def _api_add_monitored(self):
        """API: 按雀魂昵称或雀魂ID 添加监控玩家（不需要是好友）"""
        data = await request.get_json(silent=True) or {}
        nickname = (data.get("nickname") or "").strip()
        group_id = (data.get("group_id") or "").strip()
        if not nickname:
            return jsonify({"ok": False, "message": "请输入雀魂昵称或雀魂ID"})
        if not self._has_online_account():
            return jsonify({"ok": False, "message": "目前无法播报，请联系管理员在账号池添加账号。"})
        player = await self._resolve_player_any(nickname, allow_id=True)
        if not player:
            return jsonify({"ok": False, "message": self._not_found_message(nickname)})
        if nickname.isdigit():
            try:
                expected = int(to_majsoul(int(player.get("account_id") or 0)))
            except (TypeError, ValueError):
                expected = 0
            if expected != int(nickname):
                return jsonify({
                    "ok": False,
                    "message": f"雀魂ID {nickname} 和解析到的账号（雀魂ID {expected}）不一致，已取消添加",
                })
        account_id = int(player.get("account_id") or 0)
        display_name = str(player.get("nickname") or nickname)
        logger.info(f"[MajsoulMonitor] 解析监控玩家 {nickname!r} -> {display_name}({show_uid(account_id)})")
        entry = self.monitored.get(display_name, self.monitored.get(nickname, {}))
        gids = list(entry.get("group_ids", []))
        if group_id and group_id not in gids:
            gids.append(group_id)
        now_ts = int(time.time())
        if group_id:
            mapping = entry.get("group_added_at")
            mapping = dict(mapping) if isinstance(mapping, dict) else {}
            if group_id not in mapping:
                mapping[group_id] = now_ts
            entry["group_added_at"] = mapping
        if not entry.get("added_at"):
            # 第一次加入监控时记下添加时间，之后重复添加/补绑群不再覆盖
            entry["added_at"] = now_ts
        entry.update({
            "account_id": account_id,
            "level_id": int(player.get("level_id") or 0),
            "level_score": int(player.get("level_score") or 0),
            "group_ids": gids,
        })
        level3_id = int(player.get("level3_id") or 0)
        if level3_id:
            entry["level3_id"] = level3_id
            entry["level3_score"] = int(player.get("level3_score") or 0)
        self.monitored[display_name] = entry
        if display_name != nickname:
            self.monitored.pop(nickname, None)
        save_monitored(self.monitored)
        self._write_query_log(
            "SYSTEM", "添加监控玩家",
            f"{display_name}({show_uid(account_id)})"
            + (f" 绑定群 {','.join(gids)}" if gids else " 未绑定群"),
            "INFO",
        )
        logger.info(
            f"[MajsoulMonitor] 添加监控玩家: {display_name}({show_uid(account_id)}) groups={gids}"
        )
        return jsonify({
            "ok": True,
            "message": f"已添加 {display_name}",
            "data": {"nickname": nickname, "account_id": account_id},
        })

    async def _api_get_theme(self):
        """API: 返回已保存的页面主题；没有保存过时 theme 为 null。"""
        theme = load_theme()
        return jsonify({"ok": True, "theme": theme, "data": {"theme": theme}})

    async def _api_save_theme(self):
        """API: 保存页面主题，插件重载后仍沿用。"""
        data = await request.get_json(silent=True) or {}
        theme = "dark" if str(data.get("theme") or "").lower() == "dark" else "light"
        try:
            save_theme(theme)
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 保存主题失败: {exc}")
            return jsonify({"ok": False, "message": f"保存主题失败: {exc}"})
        return jsonify({"ok": True, "theme": theme, "data": {"theme": theme}})

    async def _api_get_group_settings(self):
        """API: 返回各群的播报开关（含插件设置里的默认值）"""
        gids = {str(gid) for gid in self.group_settings.keys() if str(gid or "").strip()}
        for entry in self.monitored.values():
            if not isinstance(entry, dict):
                continue
            for gid in entry.get("group_ids") or []:
                gid_s = str(gid or "").strip()
                if gid_s:
                    gids.add(gid_s)
        groups: Dict[str, Dict[str, bool]] = {
            gid: self._group_settings_of(gid) for gid in sorted(gids)
        }
        return jsonify({"ok": True, "data": {
            "groups": groups,
            "kinds": list(BROADCAST_KINDS),
            "labels": {kind: BROADCAST_LABELS[kind] for kind in BROADCAST_KINDS},
            "defaults": {kind: self._default_broadcast(kind) for kind in BROADCAST_KINDS},
        }})

    async def _api_save_group_settings(self):
        """API: 修改某个群的播报开关（支持一次改一个或多个）"""
        data = await request.get_json(silent=True) or {}
        group_id = str(data.get("group_id") or "").strip()
        if not group_id:
            return jsonify({"ok": False, "message": "缺少群号"})
        changed: List[str] = []
        settings = data.get("settings")
        if isinstance(settings, dict):
            for kind, val in settings.items():
                if kind not in BROADCAST_KINDS:
                    continue
                if self._set_group_setting(group_id, kind, bool(val)):
                    changed.append(BROADCAST_LABELS[kind])
        else:
            kind = str(data.get("kind") or "").strip()
            if kind not in BROADCAST_KINDS:
                return jsonify({"ok": False, "message": "缺少 kind 或 settings"})
            if self._set_group_setting(group_id, kind, bool(data.get("enabled"))):
                changed.append(BROADCAST_LABELS[kind])
        current = self._group_settings_of(group_id)
        if changed:
            self._write_query_log("SYSTEM", "群播报设置", f"群 {group_id}：{'、'.join(changed)}", "INFO")
            return jsonify({
                "ok": True,
                "message": f"群 {group_id} 的{'、'.join(changed)}已更新",
                "data": {"group_id": group_id, "settings": current},
            })
        return jsonify({
            "ok": True, "message": "无变更",
            "data": {"group_id": group_id, "settings": current},
        })

    async def _api_get_config(self):
        """API: 返回当前插件配置"""
        return jsonify({"ok": True, "data": {
            "enabled": bool(self.config.get("enabled", True)),
            "poll_interval": int(self.config.get("poll_interval", 120)),
            "friend_poll_interval": int(self.config.get("friend_poll_interval", 30)),
            "nonfriend_daily_limit": int(self.config.get("nonfriend_daily_limit", 2)),
            "monitor_valid_value": int(self.config.get("monitor_valid_value", MONITOR_VALID_DEFAULT_VALUE)),
            "monitor_valid_unit": str(self.config.get("monitor_valid_unit", MONITOR_VALID_DEFAULT_UNIT)),
            "monitor_three_player": bool(self.config.get("monitor_three_player", True)),
            "monitor_east": bool(self.config.get("monitor_east", True)),
            "monitor_friend": bool(self.config.get("monitor_friend", False)),
            "monitor_contest": bool(self.config.get("monitor_contest", False)),
            "auto_approve_friends": bool(self.config.get("auto_approve_friends", True)),
            "log_retain_days": self._log_retain_days(),
            "admins_id": list(self.config.get("admins_id", [])),
            "api_nodes": list(_API_NODES),
            "cmd_add_name": self.config.get("cmd_add_name", DEFAULT_CMD_ADD),
            "cmd_remove_name": self.config.get("cmd_remove_name", DEFAULT_CMD_REMOVE),
            "cmd_list_name": self.config.get("cmd_list_name", DEFAULT_CMD_LIST),
            "cmd_paipu_name": self.config.get("cmd_paipu_name", DEFAULT_CMD_PAIPU),
            "cmd_help_name": self.config.get("cmd_help_name", DEFAULT_CMD_HELP),
            "cmd_three_name": self.config.get("cmd_three_name", DEFAULT_CMD_THREE),
            "cmd_east_name": self.config.get("cmd_east_name", DEFAULT_CMD_EAST),
            "cmd_friend_name": self.config.get("cmd_friend_name", DEFAULT_CMD_FRIEND),
            "cmd_contest_name": self.config.get("cmd_contest_name", DEFAULT_CMD_CONTEST),
        }})

    @staticmethod
    def _field_labels(fields: List[str]) -> str:
        """把配置字段名翻译成日志里好读的中文名。"""
        labels = {
            "enabled": "启用插件",
            "poll_interval": "非好友轮询间隔",
            "friend_poll_interval": "好友轮询间隔",
            "nonfriend_daily_limit": "非好友每日播报上限",
            "monitor_valid_value": "监控有效期数值",
            "monitor_valid_unit": "监控有效期单位",
            "monitor_three_player": "三麻播报默认开关",
            "monitor_east": "东场播报默认开关",
            "monitor_friend": "友人播报默认开关",
            "monitor_contest": "比赛播报默认开关",
            "auto_approve_friends": "自动同意好友申请",
            "log_retain_days": "日志保存时间",
            "admins_id": "额外授权的管理员",
            "api_nodes": "牌谱屋查询节点",
            "cmd_add_name": "新增指令名",
            "cmd_remove_name": "删除指令名",
            "cmd_list_name": "列表指令名",
            "cmd_paipu_name": "牌谱指令名",
            "cmd_help_name": "帮助指令名",
            "cmd_three_name": "三麻播报指令名",
            "cmd_east_name": "东场播报指令名",
            "cmd_friend_name": "友人播报指令名",
            "cmd_contest_name": "比赛播报指令名",
        }
        return "、".join(labels.get(field, field) for field in fields)

    async def _api_save_config(self):
        """API: 保存插件配置（含牌谱屋 API 节点）"""
        global _API_NODES
        data = await request.get_json(silent=True) or {}
        changed = False
        changed_fields = []
        if "enabled" in data:
            val = bool(data["enabled"])
            if val != bool(self.config.get("enabled", True)):
                self.config["enabled"] = val
                changed = True
                changed_fields.append("enabled")
        if "poll_interval" in data:
            val = max(30, int(data["poll_interval"]))
            if val != int(self.config.get("poll_interval", 120)):
                self.config["poll_interval"] = val
                changed = True
                changed_fields.append("poll_interval")
        if "friend_poll_interval" in data:
            val = max(10, int(data["friend_poll_interval"]))
            if val != int(self.config.get("friend_poll_interval", 30)):
                self.config["friend_poll_interval"] = val
                changed = True
                changed_fields.append("friend_poll_interval")
        if "nonfriend_daily_limit" in data:
            val = int(data["nonfriend_daily_limit"])
            # -1 = 无限制，其余只接受 0~10
            val = -1 if val < 0 else min(10, val)
            if val != int(self.config.get("nonfriend_daily_limit", 2)):
                self.config["nonfriend_daily_limit"] = val
                changed = True
                changed_fields.append("nonfriend_daily_limit")
        if "monitor_valid_unit" in data:
            val = str(data["monitor_valid_unit"] or MONITOR_VALID_DEFAULT_UNIT).strip().lower()
            if val not in MONITOR_VALID_UNITS:
                val = MONITOR_VALID_DEFAULT_UNIT
            if val != str(self.config.get("monitor_valid_unit", MONITOR_VALID_DEFAULT_UNIT)):
                self.config["monitor_valid_unit"] = val
                changed = True
                changed_fields.append("monitor_valid_unit")
        if "monitor_valid_value" in data:
            try:
                val = int(data["monitor_valid_value"])
            except (TypeError, ValueError):
                val = MONITOR_VALID_DEFAULT_VALUE
            val = max(1, min(MONITOR_VALID_MAX_VALUE, val))
            if val != int(self.config.get("monitor_valid_value", MONITOR_VALID_DEFAULT_VALUE)):
                self.config["monitor_valid_value"] = val
                changed = True
                changed_fields.append("monitor_valid_value")
        if "monitor_three_player" in data:
            val = bool(data["monitor_three_player"])
            if val != bool(self.config.get("monitor_three_player", True)):
                self.config["monitor_three_player"] = val
                changed = True
                changed_fields.append("monitor_three_player")
        if "monitor_east" in data:
            val = bool(data["monitor_east"])
            if val != bool(self.config.get("monitor_east", True)):
                self.config["monitor_east"] = val
                changed = True
                changed_fields.append("monitor_east")
        if "monitor_friend" in data:
            val = bool(data["monitor_friend"])
            if val != bool(self.config.get("monitor_friend", False)):
                self.config["monitor_friend"] = val
                changed = True
                changed_fields.append("monitor_friend")
        if "monitor_contest" in data:
            val = bool(data["monitor_contest"])
            if val != bool(self.config.get("monitor_contest", False)):
                self.config["monitor_contest"] = val
                changed = True
                changed_fields.append("monitor_contest")
        if "auto_approve_friends" in data:
            val = bool(data["auto_approve_friends"])
            if val != bool(self.config.get("auto_approve_friends", True)):
                self.config["auto_approve_friends"] = val
                changed = True
                changed_fields.append("auto_approve_friends")
        if LOG_RETAIN_KEY in data:
            try:
                val = int(data[LOG_RETAIN_KEY])
            except (TypeError, ValueError):
                val = LOG_RETAIN_DAYS
            val = max(1, min(LOG_RETAIN_MAX_DAYS, val))
            if val != int(self.config.get(LOG_RETAIN_KEY, LOG_RETAIN_DAYS)):
                self.config[LOG_RETAIN_KEY] = val
                changed = True
                changed_fields.append(LOG_RETAIN_KEY)
        # 页面把日志保存时间改小、用户点了「确定」时会带上这个标记，立刻清一次超期日志
        purged = 0
        if data.get("purge_logs"):
            try:
                purged = self._cleanup_old_logs()
            except Exception as exc:
                logger.warning(f"[MajsoulMonitor] 清理超期日志失败: {exc}")
        if "admins_id" in data:
            val = [str(x).strip() for x in data["admins_id"] if str(x).strip()]
            if val != list(self.config.get("admins_id", [])):
                self.config["admins_id"] = val
                changed = True
                changed_fields.append("admins_id")
        if "api_nodes" in data:
            nodes = [str(n).strip().rstrip("/") for n in data["api_nodes"] if str(n).strip()]
            if nodes and nodes != list(_API_NODES):
                _API_NODES.clear()
                _API_NODES.extend(nodes)
                self.config["api_nodes"] = nodes
                changed = True
                changed_fields.append("api_nodes")
        if "cmd_add_name" in data:
            val = self._clean_cmd_name(data["cmd_add_name"], DEFAULT_CMD_ADD)
            if val != str(self.config.get("cmd_add_name", DEFAULT_CMD_ADD)):
                self.config["cmd_add_name"] = val
                changed = True
                changed_fields.append("cmd_add_name")
        if "cmd_remove_name" in data:
            val = self._clean_cmd_name(data["cmd_remove_name"], DEFAULT_CMD_REMOVE)
            if val != str(self.config.get("cmd_remove_name", DEFAULT_CMD_REMOVE)):
                self.config["cmd_remove_name"] = val
                changed = True
                changed_fields.append("cmd_remove_name")
        if "cmd_list_name" in data:
            val = self._clean_cmd_name(data["cmd_list_name"], DEFAULT_CMD_LIST)
            if val != str(self.config.get("cmd_list_name", DEFAULT_CMD_LIST)):
                self.config["cmd_list_name"] = val
                changed = True
                changed_fields.append("cmd_list_name")
        if "cmd_help_name" in data:
            val = self._clean_cmd_name(data["cmd_help_name"], DEFAULT_CMD_HELP)
            if val != str(self.config.get("cmd_help_name", DEFAULT_CMD_HELP)):
                self.config["cmd_help_name"] = val
                changed = True
                changed_fields.append("cmd_help_name")
        if "cmd_paipu_name" in data:
            val = self._clean_cmd_name(data["cmd_paipu_name"], DEFAULT_CMD_PAIPU)
            if val != str(self.config.get("cmd_paipu_name", DEFAULT_CMD_PAIPU)):
                self.config["cmd_paipu_name"] = val
                changed = True
                changed_fields.append("cmd_paipu_name")
        for kind in BROADCAST_KINDS:
            key = BROADCAST_CMD_KEYS[kind]
            default = BROADCAST_CMD_DEFAULTS[kind]
            if key not in data:
                continue
            val = self._clean_cmd_name(data[key], default)
            if val != str(self.config.get(key, default)):
                self.config[key] = val
                changed = True
                changed_fields.append(key)
        cmd_names = [
            str(self.config.get("cmd_add_name", DEFAULT_CMD_ADD)),
            str(self.config.get("cmd_remove_name", DEFAULT_CMD_REMOVE)),
            str(self.config.get("cmd_list_name", DEFAULT_CMD_LIST)),
            str(self.config.get("cmd_paipu_name", DEFAULT_CMD_PAIPU)),
            str(self.config.get("cmd_help_name", DEFAULT_CMD_HELP)),
        ]
        for kind in BROADCAST_KINDS:
            key = BROADCAST_CMD_KEYS[kind]
            cmd_names.append(str(self.config.get(key, BROADCAST_CMD_DEFAULTS[kind])))
        if len(set(cmd_names)) != len(cmd_names):
            return jsonify({"ok": False, "message": "指令名不能重复"})
        if changed:
            try:
                cfg_path = _plugin_config_path()
                cfg_path.parent.mkdir(parents=True, exist_ok=True)
                existing = {}
                if cfg_path.exists():
                    existing = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
                # 四个播报指令固定只允许群管理员使用，旧的 admin_only 开关已废弃
                existing.pop("admin_only", None)
                existing.update({
                    "enabled": bool(self.config.get("enabled", True)),
                    "poll_interval": int(self.config.get("poll_interval", 120)),
                    "friend_poll_interval": int(self.config.get("friend_poll_interval", 30)),
                    "nonfriend_daily_limit": int(self.config.get("nonfriend_daily_limit", 2)),
                    "monitor_valid_value": int(self.config.get("monitor_valid_value", MONITOR_VALID_DEFAULT_VALUE)),
                    "monitor_valid_unit": str(self.config.get("monitor_valid_unit", MONITOR_VALID_DEFAULT_UNIT)),
                    "monitor_three_player": bool(self.config.get("monitor_three_player", True)),
                    "monitor_east": bool(self.config.get("monitor_east", True)),
                    "monitor_friend": bool(self.config.get("monitor_friend", False)),
                    "monitor_contest": bool(self.config.get("monitor_contest", False)),
                    "auto_approve_friends": bool(self.config.get("auto_approve_friends", True)),
                    "log_retain_days": int(self.config.get(LOG_RETAIN_KEY, LOG_RETAIN_DAYS)),
                    "admins_id": list(self.config.get("admins_id", [])),
                    "api_nodes": list(_API_NODES),
                    "cmd_add_name": str(self.config.get("cmd_add_name", DEFAULT_CMD_ADD)),
                    "cmd_remove_name": str(self.config.get("cmd_remove_name", DEFAULT_CMD_REMOVE)),
                    "cmd_list_name": str(self.config.get("cmd_list_name", DEFAULT_CMD_LIST)),
                    "cmd_paipu_name": str(self.config.get("cmd_paipu_name", DEFAULT_CMD_PAIPU)),
                    "cmd_help_name": str(self.config.get("cmd_help_name", DEFAULT_CMD_HELP)),
                    "cmd_three_name": str(self.config.get("cmd_three_name", DEFAULT_CMD_THREE)),
                    "cmd_east_name": str(self.config.get("cmd_east_name", DEFAULT_CMD_EAST)),
                    "cmd_friend_name": str(self.config.get("cmd_friend_name", DEFAULT_CMD_FRIEND)),
                    "cmd_contest_name": str(self.config.get("cmd_contest_name", DEFAULT_CMD_CONTEST)),
                })
                cfg_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.info(f"[MajsoulMonitor] 配置已保存，变更项: {self._field_labels(changed_fields)}")
                self._write_query_log("SYSTEM", "配置保存", f"变更项: {self._field_labels(changed_fields)}", level="INFO")
            except Exception as e:
                logger.error(f"[MajsoulMonitor] 保存配置失败: {e}", exc_info=True)
                return jsonify({"ok": False, "message": f"保存失败: {e}"})
            # 只有启用/禁用才需要真正起停监控；其余设置一律热更新，不重连、不重新登录
            purge_note = f"，已删除 {purged} 个超期日志" if purged else ""
            if "enabled" in changed_fields:
                if self._monitor is not None:
                    stop_reason = (
                        "配置变更，重启监控"
                        if self.config.get("enabled", True)
                        else "插件被禁用（enabled=false），停止所有后台活动"
                    )
                    await self._monitor.stop(stop_reason)
                    self._monitor = None
                if self.config.get("enabled", True):
                    self._running = True
                    await self._start_monitor()
                    interval = getattr(self._monitor, "poll_interval", 120)
                    friend_interval = getattr(self._monitor, "friend_poll_interval", 30)
                    logger.info(
                        f"[MajsoulMonitor] 对局监控已启动，好友轮询间隔 {friend_interval} 秒，"
                        f"非好友轮询间隔 {interval} 秒"
                    )
                    self._log_system(
                        "插件已启用",
                        f"好友轮询间隔 {friend_interval} 秒，非好友轮询间隔 {interval} 秒",
                        "INFO",
                    )
                    return jsonify({"ok": True, "message": f"配置已保存，对局监控已启动{purge_note}"})
                self._running = False
                logger.info("[MajsoulMonitor] 对局监控已停止（插件已关闭）")
                self._log_system("插件已禁用", "所有后台活动已停止", "WARN")
                return jsonify({"ok": True, "message": f"配置已保存，对局监控已停止{purge_note}"})
            # 非启用状态的设置：直接写进正在运行的监控对象，下一轮扫描就生效
            applied: List[str] = []
            if self._monitor is not None:
                try:
                    applied = self._monitor.apply_settings(
                        poll_interval=int(self.config.get("poll_interval", 120)),
                        friend_poll_interval=int(self.config.get("friend_poll_interval", 30)),
                        monitor_three_player=bool(self.config.get("monitor_three_player", True)),
                        monitor_east=bool(self.config.get("monitor_east", True)),
                        auto_approve_friends=bool(self.config.get("auto_approve_friends", True)),
                        nonfriend_daily_limit=int(self.config.get("nonfriend_daily_limit", 2)),
                        daily_limit=int(self.config.get("daily_paipu_limit", 200)),
                        search_urls=self._search_urls(),
                    )
                except Exception as e:
                    logger.error(f"[MajsoulMonitor] 热更新设置失败: {e}", exc_info=True)
            if applied:
                logger.info(f"[MajsoulMonitor] 设置已即时生效: {'、'.join(applied)}")
                return jsonify({"ok": True, "message": f"配置已保存，已即时生效{purge_note}"})
            return jsonify({"ok": True, "message": f"已保存{purge_note}"})
        return jsonify({"ok": True, "message": f"无变更{purge_note}"})

    async def _api_test_node(self):
        """API: 测试单个牌谱屋 API 节点是否可用"""
        data = await request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip().rstrip("/")
        if not url:
            return jsonify({"ok": False, "message": "缺少节点地址"})
        try:
            session = await self.api._get_session()
            test_url = f"{url}/pl4/search_player/test"
            async with session.get(test_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                # Any non-error response means the node is reachable
                # 200 = found results, 404/empty = node works but no match
                if resp.status < 500:
                    return jsonify({"ok": True, "message": f"节点可用 (HTTP {resp.status})"})
                else:
                    return jsonify({"ok": False, "message": f"服务器错误 (HTTP {resp.status})"})
        except asyncio.TimeoutError:
            return jsonify({"ok": False, "message": "连接超时"})
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)[:100]})

    async def _api_list_logs(self):
        """API: 返回日志文件列表（按日期倒序）"""
        self._cleanup_old_logs()
        log_dir = self._get_log_dir()
        files = sorted(log_dir.glob("*.log"), reverse=True)
        result = []
        for f in files:
            stat = f.stat()
            result.append({
                "filename": f.name,
                "size": stat.st_size,
                "date": f.stem,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
        return jsonify({"ok": True, "data": result})
    async def _api_get_log_content(self):
        """API: 返回指定日志文件内容（结构化JSON数组，支持级别/关键词筛选）"""
        import re as _re
        filename = request.args.get("file", "")
        level_filter = request.args.get("level", "").strip().upper()
        keyword = (request.args.get("q") or "").strip()
        if not filename or ".." in filename or "/" in filename:
            return jsonify({"ok": False, "message": "无效文件名"}), 200
        log_dir = self._get_log_dir()
        log_file = log_dir / filename
        if not log_file.exists():
            return jsonify({"ok": False, "message": "文件不存在"}), 200
        try:
            st = log_file.stat()
            cache_key = str(log_file)
            if not level_filter and not keyword:
                cached = self._log_content_cache.get(cache_key)
                if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
                    return jsonify(cached[2])
            # 默认只展示最近 1000 条（每 30s 一条「轮询房间」，太多会卡）；
            # 带关键词搜索时在当天整个日志文件里过滤，不受 1000 条限制。
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            total = len(lines)
            pattern = _re.compile(r"^\[([^\]]+)\]\s*\[([^\]]+)\]\s*\[([^\]]+)\]\s*(.+)$")

            def _parse(line: str) -> Dict:
                m = pattern.match(line)
                if m:
                    return {"time": m.group(1), "level": m.group(2).upper(), "target": m.group(3), "message": m.group(4)}
                return {"time": "", "level": "INFO", "target": "", "message": line}

            needle = keyword.lower()
            sources = lines if needle else lines[-1000:]
            entries = []
            kw_matches = 0
            for line in sources:
                entry = _parse(line)
                if needle:
                    haystack = " ".join(
                        (entry["time"], entry["level"], entry["target"], entry["message"])
                    ).lower()
                    if needle not in haystack:
                        continue
                    kw_matches += 1
                if level_filter and entry["level"] != level_filter:
                    continue
                entries.append(entry)
            matched = kw_matches if needle else total
            if needle and matched > 2000:
                # 匹配太多时只回传最近 2000 条，避免一次传回几十 MB
                entries = entries[-2000:]
            # 注意：AstrBotPluginPage.apiGet 只把 data 交给页面，顶层 total 会被丢掉，
            # 所以把 entries/total 一起放进 data。
            payload = {
                "ok": True,
                "data": {
                    "entries": entries,
                    "total": total,
                    "matched": matched,
                    "query": keyword,
                },
                "total": total,
            }
            if not level_filter and not keyword:
                if len(self._log_content_cache) > 24:
                    self._log_content_cache.clear()
                self._log_content_cache[cache_key] = (st.st_mtime_ns, st.st_size, payload)
            return jsonify(payload)
        except Exception as e:
            return jsonify({"ok": False, "message": f"读取失败: {e}"}), 200

    def _get_log_dir(self) -> Path:
        base = StarTools.get_data_dir("astrbot_plugin_majsoul_matchreport")
        log_dir = base / LOG_DIR_NAME
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    def _write_query_log(self, nickname: str, status: str, detail: str = "", level: str = "INFO"):
        """写入当日查询日志（结构化格式，含级别标签）"""
        log_dir = self._get_log_dir()
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = log_dir / f"{today}.log"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        level = level.upper() if level else "INFO"
        line = f"[{ts}] [{level}] [{nickname}] {status}"
        if detail:
            line += f" | {detail}"
        line += "\n"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)
        # 每天第一次写日志时顺手清理超过保留期的旧日志（插件连续运行也不会一直堆着）
        if getattr(self, "_log_cleanup_day", "") != today:
            self._log_cleanup_day = today
            try:
                self._cleanup_old_logs()
            except Exception:
                pass

    def _cleanup_old_logs(self, days: Optional[int] = None) -> int:
        """删除超过保留天数的查询日志，返回删掉的文件数。"""
        if days is None:
            days = self._log_retain_days()
        log_dir = self._get_log_dir()
        cutoff = time.time() - int(days) * 86400
        removed = 0
        for f in log_dir.glob("*.log"):
            try:
                if f.stat().st_mtime >= cutoff:
                    continue
                f.unlink()
                removed += 1
                logger.info(f"[MajsoulMonitor] 已清理过期日志: {f.name}")
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning(f"[MajsoulMonitor] 清理日志失败 {f.name}: {e}")
        return removed

    def _log_retain_days(self) -> int:
        """日志保留天数：以插件设置里的数字为准，缺省或非法时退回默认 30 天。"""
        try:
            days = int(self.config.get(LOG_RETAIN_KEY, LOG_RETAIN_DAYS))
        except (TypeError, ValueError):
            days = LOG_RETAIN_DAYS
        return max(1, min(LOG_RETAIN_MAX_DAYS, days))

    async def terminate(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._monitor is not None:
            await self._monitor.stop("插件被停用/重载")
            self._monitor = None
        await self.api.close()
        logger.info("[MajsoulMonitor] 已停止")

    # ── 旧方案：牌谱屋 HTTP 轮询（已停用，保留备查）──────────────
    # 2026-10-23 起改为「登录雀魂账号轮询对局状态」：见 review/state_monitor.py。
    # 下面这组函数不再被调用，仅保留实现方便回滚。
    async def _poll_loop(self, interval: int):
        logger.info(f"[MajsoulMonitor] 轮询循环已启动，间隔 {interval}s")
        while self._running:
            try:
                await self._check_all_players()
            except asyncio.CancelledError:
                logger.info("[MajsoulMonitor] 轮询任务被取消")
                raise
            except Exception as e:
                logger.error(f"[MajsoulMonitor] 轮询异常: {e}", exc_info=True)
                self._write_query_log("SYSTEM", "轮询异常", str(e), level="ERROR")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                logger.info("[MajsoulMonitor] 轮询睡眠被取消")
                raise
        logger.info("[MajsoulMonitor] 轮询循环已退出")

    async def _check_all_players(self):
        # 旧方案入口：已停用（保留函数，避免回滚时找不到代码）
        return
        for nickname, info in list(self.monitored.items()):
            try:
                await self._check_player(nickname, info)
            except Exception as e:
                logger.warning(f"[MajsoulMonitor] 检查 {nickname} 失败: {e}")
                self._write_query_log(nickname, "检查失败", str(e), level="WARN")
            await asyncio.sleep(2)  # 避免请求过快

    async def _check_player(self, nickname: str, info: Dict):
        # Per-player lock to prevent concurrent checks on the same player
        if nickname not in self._player_locks:
            self._player_locks[nickname] = asyncio.Lock()
        async with self._player_locks[nickname]:
            await self._check_player_inner(nickname, info)

    async def _check_player_inner(self, nickname: str, info: Dict):
        mode = info.get("mode", "pl4")
        old_ts = info.get("latest_ts", 0)
        player_id = info.get("player_id", 0)

        # 1. 查询最新 timestamp
        player_data = await self.api.search_player(nickname, mode)
        if not player_data:
            self._write_query_log(nickname, "查询失败", "search_player 返回空", level="ERROR")
            return

        new_ts = player_data.get("latest_timestamp", 0)
        pid = player_data.get("id", player_id)

        if new_ts <= old_ts:
            self._write_query_log(nickname, "无新对局", f"ts={new_ts}", level="INFO")
            return  # 无新对局

        # 2. 有新对局！先记录日志，延迟更新 latest_ts 到获取详情之后
        logger.info(f"[MajsoulMonitor] {nickname} 有新对局 (ts: {old_ts} -> {new_ts})")
        self._write_query_log(nickname, "新对局", f"ts: {old_ts} -> {new_ts}", level="INFO")

        # 3. 获取对局详情（仅四人模式，使用 tag 参数避免 CAPTCHA）
        # 3a. 先查 player_stats 获取总对局数作为 tag
        all_four_modes = "16.12.9.15.11.8"
        stats = await self.api.get_player_stats(pid, mode=mode, game_mode=all_four_modes)
        tag = str(stats.get("count", "")) if stats else ""

        # 3b. 先用多模式合并查询（一次请求），减少 API 调用
        records = []
        matched_mode_id = None
        await asyncio.sleep(1)
        combined = await self.api.get_player_records(pid, limit=10, mode=mode, game_mode=all_four_modes, tag=tag)
        if combined:
            records = combined
            latest_mode = combined[0].get("modeId", "")
            matched_mode_id = str(latest_mode) if latest_mode else None
        else:
            # 3c. 合并查询失败时降级到单模式查询，增大间隔避免 429
            four_modes = ["16", "12", "9", "15", "11", "8"]
            for gm in four_modes:
                await asyncio.sleep(2)
                recs = await self.api.get_player_records(pid, limit=10, mode=mode, game_mode=gm, tag=tag)
                if recs:
                    records = recs
                    matched_mode_id = gm
                    break
        # 4. 构建播报消息
        message = self._build_notify_message(nickname, pid, new_ts, records, mode, matched_mode_id)

        # 5. 更新 latest_ts（在获取详情之后、发送之前）
        self.monitored[nickname]["latest_ts"] = new_ts
        self.monitored[nickname]["player_id"] = pid
        save_monitored(self.monitored)
        # 6. 发送通知
        group_ids = [g for g in info.get("group_ids", []) if not self._group_expired(info, g)]
        try:
            await self._send_notification(message, group_ids)
            log_level = "ERROR" if "对局详情暂不可用" in message else "INFO"
            self._write_query_log(nickname, "播报成功", message.replace("\n", " | "), level=log_level)
        except Exception as e:
            self._write_query_log(nickname, "播报失败", str(e), level="ERROR")
            raise

    def _build_notify_message(
        self, nickname: str, player_id: int, ts: int,
        records: List[Dict], mode: str, matched_mode_id: Optional[str] = None
    ) -> str:
        lines = []
        lines.append("🀄 【雀魂对局播报】")

        if records:
            latest = records[0]
            players = latest.get("players", [])
            sorted_players = sorted(players, key=lambda x: x.get("score", 0), reverse=True)

            # 第二行：玩家名字、段位和pt
            level_info = ""
            for sp in sorted_players:
                if sp.get("nickname", "").casefold() == nickname.casefold():
                    lid = sp.get("level", {}).get("id", 0) if isinstance(sp.get("level"), dict) else 0
                    lscore = sp.get("level", {}).get("score", 0) if isinstance(sp.get("level"), dict) else 0
                    if lid:
                        try:
                            level_info = level_id_to_display(lid, lscore)
                        except Exception:
                            level_info = ""
                    break
            if level_info:
                lines.append(f"玩家：{nickname}（{level_info}）")
            else:
                lines.append(f"玩家：{nickname}")

            # 第三行：近期战绩（最近10局顺位）
            recent_ranks = []
            for rec in records[:10]:
                rec_players = rec.get("players", [])
                rec_sorted = sorted(rec_players, key=lambda x: x.get("score", 0), reverse=True)
                for idx, rp in enumerate(rec_sorted, 1):
                    if rp.get("nickname", "").casefold() == nickname.casefold():
                        recent_ranks.append(str(idx))
                        break
            if recent_ranks:
                lines.append(f"近期战绩：[{''.join(recent_ranks)}]")

            # 第四行：房间类型 + 对局时间
            room_name = FOUR_PLAYER_MODES.get(matched_mode_id, "四人对局") if matched_mode_id else "四人对局"
            game_ts = latest.get("startTime", latest.get("start_time", ts))
            if isinstance(game_ts, (int, float)):
                if game_ts > 1e12:
                    game_ts = game_ts / 1000
                time_str = datetime.fromtimestamp(game_ts).strftime("%Y-%m-%d %H:%M")
            else:
                time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            lines.append(f"{room_name} {time_str}")

            # 第五到八行：各玩家顺位、点数、名次
            rank_labels = {1: "一位", 2: "二位", 3: "三位", 4: "四位"}
            for i, sp in enumerate(sorted_players, 1):
                label = rank_labels.get(i, f"{i}位")
                sc = sp.get("score", 0)
                nm = sp.get("nickname", "?")
                lines.append(f"{label} {sc} {nm}")
        else:
            lines.append(f"玩家：{nickname}")
            lines.append("（对局详情暂不可用）")

        return "\n".join(lines)

    # ── 新方案：对局结束回调 ─────────────────────────
    def _match_monitored(self, account_id: int, nickname: str) -> Optional[str]:
        """把结束对局的账号对到监控列表里的玩家（优先 account_id，其次昵称，大小写敏感）。"""
        if account_id:
            for key, entry in self.monitored.items():
                if str(entry.get("account_id") or entry.get("player_id") or "") == str(account_id):
                    return key
        nick = (nickname or "").strip()
        for key in self.monitored:
            if key.strip() == nick:
                return key
        return None

    @staticmethod
    def _ranked_players(head):
        """按最终点数排序：part_point_1 是最终点数（四麻总和 100000 / 三麻 105000），
        total_point 是精算增减（总和 0），只在没有前者的老数据里兜底。"""
        players = list(getattr(head.result, "players", []) or [])
        parts = [int(getattr(p, "part_point_1", 0) or 0) for p in players]
        scores = parts if any(parts) else [int(getattr(p, "total_point", 0) or 0) for p in players]
        pairs = list(zip(players, scores))
        pairs.sort(key=lambda item: item[1], reverse=True)
        return pairs

    @staticmethod
    def _record_rank(head, seat: int) -> int:
        for idx, (player, _score) in enumerate(MajsoulMonitorPlugin._ranked_players(head), 1):
            if int(getattr(player, "seat", -1)) == seat:
                return idx
        return 0
    async def _pt_and_rank_change(
        self, head, seat: int, account_id: int
    ) -> Tuple[int, str, str, Optional[Dict]]:
        """算出这局的段位分变化和段位升降。

        返回 (pt 变化, "up"/"down"/"", 新段位名, 查询到的当前段位资料)：
        - 牌谱里同一段位时，用「当前分数 - 牌谱分数」最准（魂天本来就是 ×100 的定点数）；
        - 查不到当前状态（账号离线）就退回牌谱里的 grading_score，并按分数阈值推算段位；
        - 第 4 项是 lookup_account 查到的原始资料（查不到就是 None），调用方可以拿它
          刷新监控列表里存的段位，避免播报里一直显示加监控时的旧段位。
        """
        if seat < 0:
            return 0, "", "", None
        mode_id = int(
            getattr(getattr(getattr(head, "config", None), "meta", None), "mode_id", 0) or 0
        )
        sanma = mode_id in THREE_PLAYER_MODE_IDS
        before_level = None
        for acc in list(getattr(head, "accounts", []) or []):
            if int(getattr(acc, "seat", -1)) == seat:
                before_level = getattr(acc, "level3" if sanma else "level", None)
                break
        before_id = int(getattr(before_level, "id", 0) or 0)
        before_score = int(getattr(before_level, "score", 0) or 0)
        delta = 0
        for player in list(getattr(getattr(head, "result", None), "players", []) or []):
            if int(getattr(player, "seat", -1)) == seat:
                delta = int(getattr(player, "grading_score", 0) or 0)
                break
        if before_id <= 0:
            return delta, "", "", None
        fresh: Optional[Dict] = None
        after_id, after_score = 0, 0
        if account_id > 0 and self._monitor is not None:
            try:
                data = await asyncio.wait_for(
                    self._monitor.lookup_account(int(account_id)), timeout=8
                )
            except Exception as exc:  # noqa: BLE001
                # 查不到就退回牌谱里的 grading_score，但不能静默
                logger.debug(
                    f"[MajsoulMonitor] 查询 {show_uid(account_id)} 的当前段位失败（用牌谱值兜底）："
                    f"{exc.__class__.__name__}: {exc}"
                )
                data = None
            if data:
                fresh = data
                if sanma:
                    after_id = int(data.get("level3_id") or 0)
                    after_score = int(data.get("level3_score") or 0)
                else:
                    after_id = int(data.get("level_id") or 0)
                    after_score = int(data.get("level_score") or 0)
        if after_id > 0 and after_id == before_id and after_score:
            diff = after_score - before_score
            if diff:
                delta = diff
        if after_id > 0:
            direction = _rank_direction(before_id, after_id)
            tag = ""
            if direction:
                try:
                    tag = MajsoulLevel(after_id).get_tag()
                except Exception:
                    tag = ""
            return delta, direction, tag, fresh
        # 账号暂时查不到：用「牌谱分数 + 本局 pt」走一遍段位阈值，只用来判断升/掉段
        try:
            level = MajsoulLevel(before_id)
            after_level = level.get_adjusted_level(before_score + delta)
            direction = _rank_direction(before_id, int(after_level.id))
            if direction:
                return delta, direction, after_level.get_tag(), fresh
        except Exception:
            pass
        return delta, "", "", fresh

    def _account_label(self, uid) -> str:
        """日志里显示账号：有在线会话就用「昵称(数字ID)」，否则退回数字 ID。"""
        text = str(uid or "")
        try:
            if self._monitor is not None:
                label = str(self._monitor.account_label(text) or "").strip()
                if label:
                    return label
        except Exception:  # noqa: BLE001
            pass
        return show_uid(text)

    @staticmethod
    def _account_display_name(account, robot: bool = False) -> str:
        """座位上显示的名字：真人用昵称；电脑（好友房里带的 CPU）写成「电脑（角色名）」或「电脑」。

        电脑座位的 account_id 是 1、2 这样的小数字，不能靠 account_id 判断，
        必须看它是不是来自 head.robots（robot=True）。
        """
        try:
            account_id = int(getattr(account, "account_id", 0) or 0)
        except (TypeError, ValueError):
            account_id = 0
        name = str(getattr(account, "nickname", "") or "").strip()
        if name in {"?", "？", "-", "null", "None"}:
            name = ""
        if not name:
            # 牌谱里的角色信息是嵌套的 character 对象，只有 charid，没有名字
            sources = [account, getattr(account, "character", None)]
            for source in sources:
                if source is None:
                    continue
                for attr in ("character_name", "char_name", "name", "nickname"):
                    name = str(getattr(source, attr, "") or "").strip()
                    if name and name not in {"?", "？", "-", "null", "None"}:
                        break
                    name = ""
                if name:
                    break
        if robot or account_id <= 0:
            if name and name.upper() not in {"电脑", "AI", "CPU", "BOT", "机器人", "PLAYER"}:
                return f"电脑（{name}）"
            return "电脑"
        # 真人座位理论上一定有昵称，取不到时别误写成「电脑」
        return name or "玩家"

    def _seat_names(self, head, accounts=None) -> Dict[int, str]:
        """按座位号收集显示名：真人看 accounts，电脑看 head.robots（电脑不会出现在 accounts 里）。"""
        names: Dict[int, str] = {}

        def put(seat: int, account, robot: bool = False) -> None:
            if seat < 0:
                return
            current = names.get(seat)
            candidate = self._account_display_name(account, robot=robot)
            # 已经有名字就不再覆盖；但「电脑」没有角色名时，用后面读到的角色名补上
            if current is None or (current in ("电脑", "玩家") and candidate not in ("电脑", "玩家")):
                names[seat] = candidate

        source = accounts if accounts is not None else list(getattr(head, "accounts", []) or [])
        for account in source:
            put(self._seat_of(account), account)
        for robot in list(getattr(head, "robots", []) or []):
            put(self._seat_of(robot), robot, robot=True)
        for player in list(getattr(getattr(head, "result", None), "players", []) or []):
            put(self._seat_of(player), player)
        return names

    @staticmethod
    def _seat_of(account) -> int:
        try:
            return int(getattr(account, "seat", -1))
        except (TypeError, ValueError):
            return -1

    def _player_level(self, entry: Dict, sanma: bool, info: Optional[Dict] = None) -> Tuple[int, int]:
        """取出这个玩家自己记录的段位/分数（三麻看 level3），没有就退回 info 里的。"""
        info = info or {}
        entry = entry or {}
        if sanma:
            level_id = int(entry.get("level3_id") or 0)
            level_score = int(entry.get("level3_score") or 0)
        else:
            level_id = int(entry.get("level_id") or 0)
            level_score = int(entry.get("level_score") or 0)
        if not level_id:
            level_id = int(info.get("level_id") or 0)
            level_score = int(info.get("level_score") or 0)
        return level_id, level_score

    def _build_game_message(
        self,
        key: str,
        head,
        info: Dict,
        pt_by_seat: Optional[Dict[int, int]] = None,
        room_label: str = "",
        players: Optional[List[Dict]] = None,
    ) -> str:
        """拼一条对局播报。

        players 是本群参与这局的监控玩家列表（同一群里同桌的会被合并成一条播报）：
        [{"key","nickname","account_id","seat","entry"}]；只有一个人时按老格式输出。
        """
        entry = self.monitored.get(key, {})
        nickname = info.get("nickname") or key
        ranked = self._ranked_players(head)
        accounts = list(getattr(head, "accounts", []) or [])
        seat_by_account = {int(a.account_id): int(a.seat) for a in accounts}
        name_by_seat = self._seat_names(head, accounts)
        target_seat = seat_by_account.get(int(info.get("account_id") or 0), -1)
        pt_by_seat = pt_by_seat or {}
        # 友人场/比赛场：段位和近期战绩都只统计段位场，这两种房间不显示
        unranked = bool(room_label)
        # 每个座位这局用的段位（三麻看 level3），用来决定 pt 是整数还是魂天的小数
        sanma = int(
            getattr(getattr(getattr(head, "config", None), "meta", None), "mode_id", 0) or 0
        ) in THREE_PLAYER_MODE_IDS
        level_by_seat: Dict[int, int] = {}
        score_by_seat: Dict[int, int] = {}
        for acc in accounts:
            level = getattr(acc, "level3" if sanma else "level", None)
            seat_no = int(getattr(acc, "seat", -1))
            level_by_seat[seat_no] = int(getattr(level, "id", 0) or 0)
            score_by_seat[seat_no] = int(getattr(level, "score", 0) or 0)

        # 本群的监控玩家（同一个群、同一桌的合并成一条播报）
        seats_info: List[Dict[str, Any]] = []
        for item in players or []:
            p_entry = item.get("entry") or self.monitored.get(item.get("key") or "", {}) or {}
            seat = item.get("seat")
            seat = int(seat) if seat is not None else -1
            level_id, level_score = self._player_level(p_entry, sanma, info)
            # 段位优先用「刚查到的赛后段位」，其次是牌谱头部（这局开始时的段位）；
            # 都没有才退回监控列表里存的（可能是加监控时的旧值）
            given_level = item.get("level")
            if given_level:
                level_id = int(given_level[0] or 0)
                level_score = int(given_level[1] or 0)
            else:
                head_level_id = level_by_seat.get(seat, 0)
                if head_level_id:
                    level_id = head_level_id
                    level_score = score_by_seat.get(seat, 0) or level_score
            level_text = ""
            if level_id:
                try:
                    level_text = level_id_to_display(level_id, level_score)
                except Exception:
                    level_text = ""
            seats_info.append(
                {
                    "nickname": item.get("nickname") or p_entry.get("nickname") or nickname,
                    "level_text": level_text,
                    "ranks": [] if unranked else recent_ranks_of(p_entry, sanma),
                    "account_id": int(item.get("account_id") or p_entry.get("account_id") or 0),
                    "seat": seat,
                }
            )
        if not seats_info:
            level_text = ""
            level_id = int(info.get("level_id") or 0)
            level_score = int(info.get("level_score") or 0)
            if level_id:
                try:
                    level_text = level_id_to_display(level_id, level_score)
                except Exception:
                    level_text = ""
            seats_info.append(
                {
                    "nickname": nickname,
                    "level_text": level_text,
                    "ranks": [] if unranked else recent_ranks_of(entry, sanma),
                    "account_id": int(info.get("account_id") or 0),
                    "seat": target_seat,
                }
            )
        multi = len(seats_info) > 1

        lines = ["🀄 【雀魂对局播报】"]
        if multi:
            # 同桌多个监控玩家：每个玩家一行，近期战绩跟在自己名字后面
            for si in seats_info:
                line = f"玩家：{si['nickname']}"
                if si["level_text"] and not unranked:
                    line += f"（{si['level_text']}）"
                if si["ranks"]:
                    line += f" 近期战绩：[{''.join(si['ranks'][:10])}]"
                lines.append(line)
        else:
            si = seats_info[0]
            if si["level_text"] and not unranked:
                lines.append(f"玩家：{si['nickname']}（{si['level_text']}）")
            else:
                lines.append(f"玩家：{si['nickname']}")
            if si["ranks"]:
                lines.append(f"近期战绩：[{''.join(si['ranks'][:10])}]")

        mode_raw = getattr(getattr(getattr(head, "config", None), "meta", None), "mode_id", 0)
        try:
            mode_int = int(mode_raw or 0)
        except (TypeError, ValueError):
            mode_int = 0
        # MODE_LABELS 同时覆盖四麻/三麻，避免三麻对局被写成「四人对局」；
        # 非段位场（友人场/比赛场）直接用房间性质+房间号/比赛场ID 覆盖这里。
        room_name = room_label or MODE_LABELS.get(mode_int) or FOUR_PLAYER_MODES.get(str(mode_int)) or "对局"
        start_time = int(getattr(head, "start_time", 0) or 0)
        if not start_time:
            start_time = int(time.time())
        time_str = datetime.fromtimestamp(start_time).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{room_name} {time_str}")

        rank_labels = {1: "一位", 2: "二位", 3: "三位", 4: "四位"}
        # 王座间（四人东/南 15/16、三人东/南 25/26）只有雀圣和魂天：
        # 魂天的段位分叫魂珠，用 ❂ 表示（数值也除以 100）
        konten_room = mode_int in KONTEN_ROOM_MODE_IDS
        for idx, (player, score) in enumerate(ranked, 1):
            seat = int(getattr(player, "seat", -1))
            label = rank_labels.get(idx, f"{idx}位")
            line = f"{label} {score} {name_by_seat.get(seat) or '电脑'}"
            seat_level = int(level_by_seat.get(seat, 0) or 0)
            try:
                is_konten = MajsoulLevel(seat_level).is_konten() if seat_level else False
            except Exception:
                is_konten = False
            unit = "❂" if (konten_room and is_konten) else "pt"
            pt_text = _format_pt_delta(pt_by_seat.get(seat, 0), seat_level, unit)
            if pt_text:
                line += " " + pt_text
            lines.append(line)

        uuid = str(getattr(head, "uuid", "") or "")
        if uuid:
            # 牌谱只发一条（同一局，链接里的 _a 参数只是打开时高亮的座位）
            link_account = 0
            for si in seats_info:
                if int(si["account_id"]) > 0:
                    link_account = int(si["account_id"])
                    break
            if not link_account:
                link_account = int(info.get("account_id") or 0)
            if link_account > 0:
                lines.append(f"牌谱：{build_paipu_url(uuid, link_account, self._server_url())}")
        return "\n".join(lines)

    def _append_event_sections(
        self, message: str, raw: Optional[Dict], players: List[Dict], head_sanma: bool
    ) -> str:
        """把和牌/放铳接到播报后面。

        同桌有多个监控玩家时按人写、并写明谁荣和的谁；只有一个人时保持原来的格式。
        """
        if not raw or not players:
            return message
        try:
            if len(players) > 1:
                seats_map: Dict[int, str] = {}
                for p in players:
                    seat_no = p.get("seat")
                    if seat_no is None:
                        continue
                    try:
                        seat_no = int(seat_no)
                    except (TypeError, ValueError):
                        continue
                    if seat_no >= 0:
                        seats_map[seat_no] = str(p.get("nickname") or "")
                wins, deals = _paipu_merged_events(raw, seats_map, head_sanma)
            else:
                seat_no = players[0].get("seat")
                try:
                    seat_no = int(seat_no) if seat_no is not None else -1
                except (TypeError, ValueError):
                    seat_no = -1
                wins, deals = _paipu_player_events(raw, seat_no, head_sanma)
        except Exception as exc:  # noqa: BLE001
            # 新玩法的牌谱结构可能和预期不一样，这里失败只影响和牌/放铳两行，别丢整条播报
            logger.warning(
                f"[MajsoulMonitor] 解析和牌 / 放铳失败，跳过这两行：{exc.__class__.__name__}: {exc}",
                exc_info=True,
            )
            return message
        if wins:
            message += "\n和牌：" + "；".join(wins)
        if deals:
            message += "\n放铳：" + "；".join(deals)
        return message

    def _paipu_problem_note(self, raw: Optional[Dict]) -> str:
        """牌谱没取到 / 解析降级时，往查询日志里补一句原因，方便定位问题。"""
        if raw is None:
            err = str(getattr(self._monitor, "last_paipu_error", "") or "").strip()
            return f" | 牌谱未取到：{err}" if err else " | 牌谱未取到（原因未记录）"
        err = str(raw.get("_parse_error") or "").strip()
        if err:
            return f" | 牌谱解析降级（和牌/放铳/役满缺失）：{err}"
        if not raw.get("log"):
            diag = raw.get("_diag") or {}
            if isinstance(diag, dict) and diag:
                errors = diag.get("parse_errors") or []
                suffix = f", 动作报错={errors[:2]}" if errors else ""
                return (
                    " | 牌谱里没有可解析的动作（和牌/放铳/役满缺失）："
                    f"version={diag.get('version')}, records={diag.get('records')}, "
                    f"actions={diag.get('actions')}, 来源={diag.get('source')}, "
                    f"空动作={diag.get('empty_result')}, 跳过={diag.get('skipped')}{suffix}"
                )
            return " | 牌谱里没有可解析的动作（和牌/放铳/役满缺失）"
        return ""

    async def _on_game_finished(self, head, info: Dict):
        account_id = int(info.get("account_id") or 0)
        nickname = str(info.get("nickname") or "")
        targets = info.get("targets")
        if not isinstance(targets, list) or not targets:
            targets = [{"account_id": account_id, "nickname": nickname}]

        # 只保留仍在监控列表里的玩家（被删除的玩家不再播报）
        keys = []
        for target in targets:
            key = self._match_monitored(
                int(target.get("account_id") or 0), str(target.get("nickname") or "")
            )
            if key and key not in keys:
                keys.append(key)
        if not keys:
            logger.info(
                f"[MajsoulMonitor] {nickname or show_uid(account_id)} 对局结束，但相关玩家都不在监控列表，忽略"
            )
            return

        seats = {int(a.account_id): int(a.seat) for a in list(getattr(head, "accounts", []) or [])}
        uuid = str(getattr(head, "uuid", "") or "")
        # 三麻/四麻的近期战绩分开记，播报时只显示同模式的
        try:
            head_mode = int(getattr(getattr(getattr(head, "config", None), "meta", None), "mode_id", 0) or 0)
        except (TypeError, ValueError):
            head_mode = 0
        head_sanma = head_mode in THREE_PLAYER_MODE_IDS
        if head_mode and head_mode not in MODE_LABELS and head_mode not in FOUR_PLAYER_MODES:
            # 本地还没有这个玩法的房间名（比如新上线的模式），记一条日志方便补表
            self._write_query_log(
                nickname or str(account_id),
                "未知玩法",
                f"mode_id={head_mode}（本地没有房间名，播报里先显示「对局」）",
                level="WARN",
            )
        # 非段位场（友人场/比赛场）：播报里的房间名换成房间性质
        contest_uid = str(info.get("contest_uid") or "")
        room_id = int(info.get("room_id") or 0)
        unranked = str(info.get("unranked_kind") or "") or unranked_kind(
            head_mode, contest_uid, room_id
        )
        # 比赛场要显示玩家看到的比赛场 ID，而不是对局 meta 里的内部 uid
        contest_code = str(info.get("contest_code") or "").strip()
        if not contest_code and contest_uid and self._monitor is not None:
            try:
                contest_code = self._monitor.contest_code(contest_uid)
            except Exception:
                contest_code = contest_uid
        room_label = (
            unranked_label(
                unranked,
                contest_code or contest_uid,
                room_id,
                mode_name=friend_mode_label(head, _head_player_count(head)) if unranked == "friend" else "",
            )
            if unranked
            else ""
        )
        now = int(time.time())

        # 逐个玩家更新记录（同桌的多个监控玩家都会更新）
        players: List[Dict] = []
        for k in keys:
            entry = self.monitored.setdefault(k, {})
            acc = int(entry.get("account_id") or entry.get("player_id") or 0)
            if not acc:
                acc = account_id
            seat = seats.get(acc, -1)
            rank = self._record_rank(head, seat) if seat >= 0 else 0
            entry["account_id"] = acc
            entry["last_game_uuid"] = uuid
            entry["last_played_at"] = now
            # 近期战绩只记段位场（和雀魂页面上显示的口径一致）
            if rank and not unranked:
                append_recent_rank(entry, head_sanma, rank)
            self.monitored[k] = entry
            players.append(
                {
                    "key": k,
                    "entry": entry,
                    "account_id": acc,
                    "seat": seat,
                    "rank": rank,
                    "nickname": str(entry.get("nickname") or k),
                }
            )
        save_monitored(self.monitored)

        # 段位分：先取牌谱里每个座位的 grading_score，再逐个玩家补一次真实变化与升降段
        pt_by_seat: Dict[int, int] = {}
        for player in list(getattr(getattr(head, "result", None), "players", []) or []):
            seat_no = int(getattr(player, "seat", -1))
            value = int(getattr(player, "grading_score", 0) or 0)
            if seat_no >= 0 and value:
                pt_by_seat[seat_no] = value
        for p in players:
            pt_delta, rank_direction, new_rank_tag, fresh = await self._pt_and_rank_change(
                head, int(p["seat"]), int(p["account_id"])
            )
            p["pt_delta"] = pt_delta
            p["rank_direction"] = rank_direction
            p["new_rank_tag"] = new_rank_tag
            if int(p["seat"]) >= 0 and pt_delta:
                pt_by_seat[int(p["seat"])] = pt_delta
            # 段位/段位分以刚查到的为准：否则监控列表和后续播报会一直用加监控时存下的旧值
            level_for_display = None
            if fresh:
                entry = p["entry"]
                new_level_id = int(fresh.get("level_id") or 0)
                new_level3_id = int(fresh.get("level3_id") or 0)
                if new_level_id:
                    entry["level_id"] = new_level_id
                    entry["level_score"] = int(fresh.get("level_score") or 0)
                if new_level3_id:
                    entry["level3_id"] = new_level3_id
                    entry["level3_score"] = int(fresh.get("level3_score") or 0)
                if fresh.get("nickname"):
                    entry["nickname"] = str(fresh.get("nickname"))
                self.monitored[p["key"]] = entry
                fresh_id = new_level3_id if head_sanma else new_level_id
                fresh_score = int(
                    fresh.get("level3_score" if head_sanma else "level_score") or 0
                )
                if fresh_id:
                    level_for_display = (fresh_id, fresh_score)
            p["level"] = level_for_display
        if any(p.get("entry", {}).get("level_id") for p in players):
            save_monitored(self.monitored)

        # 按群分组：同一个群里同桌的玩家合并成一条播报，别的群各发各的
        group_players: Dict[str, List[Dict]] = {}
        for p in players:
            entry = p["entry"]
            for gid in entry.get("group_ids", []):
                gid_s = str(gid or "").strip()
                if not gid_s or p in group_players.get(gid_s, []):
                    continue
                if self._group_expired(entry, gid_s):
                    continue
                # 该群没开这一类的播报就不发到它那里
                if unranked in ("friend", "contest"):
                    if not self._group_wants_unranked(gid_s, unranked):
                        continue
                elif not self._group_wants_ranked(gid_s, head_mode):
                    continue
                group_players.setdefault(gid_s, []).append(p)

        # 牌谱只拉一次，和牌/放铳/役满都用它（失败不影响正常播报）
        raw: Optional[Dict] = None
        if uuid and self._monitor is not None:
            try:
                raw = await asyncio.wait_for(self._monitor.fetch_paipu_log(uuid), timeout=12)
            except Exception:
                raw = None
        # 和牌/放铳的播报门槛：四麻 4 番 30 符以上（满贯及以上也都播），三麻跳满以上
        notice = str(info.get("broadcast_notice") or "")

        if not group_players:
            # 没有群需要播报：只写一条日志（把所有同桌玩家都写上）
            primary = players[0]
            msg_info = dict(info)
            msg_info["account_id"] = int(primary["account_id"]) or account_id
            msg_info["nickname"] = primary["nickname"]
            message = self._build_game_message(
                primary["key"], head, msg_info, pt_by_seat, room_label=room_label, players=players
            )
            message = self._append_event_sections(message, raw, players, head_sanma)
            label = "、".join(str(p["nickname"]) for p in players)
            self._write_query_log(
                label,
                "对局结果（未绑定群，仅记录）",
                message.replace("\n", " | ") + self._paipu_problem_note(raw),
                level="INFO",
            )
            logger.info(
                f"[MajsoulMonitor] {label} 的对局 {uuid}（{primary['rank']}位）未绑定群，结果已写入查询日志"
            )
            return

        for gid_s in sorted(group_players):
            group_list = group_players[gid_s]
            primary = group_list[0]
            msg_info = dict(info)
            msg_info["account_id"] = int(primary["account_id"]) or account_id
            msg_info["nickname"] = primary["nickname"]
            message = self._build_game_message(
                primary["key"], head, msg_info, pt_by_seat, room_label=room_label, players=group_list
            )
            message = self._append_event_sections(message, raw, group_list, head_sanma)

            # 补充消息：(文字, 图片路径或 None)
            extras: List[Tuple[str, Optional[str]]] = []
            # 役满：同一桌里有多个监控玩家时，同一条役满只发一次（优先发荣和者的喜报）
            if raw and uuid:
                seen_yakuman: set = set()
                for p in group_list:
                    try:
                        yakuman_events = _paipu_yakuman_events(raw, int(p["seat"]))
                    except Exception as exc:  # noqa: BLE001
                        # 新玩法的牌谱结构可能让役满识别失败：跳过役满补充消息，主播报照发
                        logger.warning(
                            f"[MajsoulMonitor] 识别役满失败（{uuid}）：{exc.__class__.__name__}: {exc}",
                            exc_info=True,
                        )
                        continue
                    for index, event in enumerate(yakuman_events):
                        if event.get("kind") not in ("win", "deal"):
                            continue
                        if index in seen_yakuman:
                            continue
                        if event.get("kind") == "deal" and any(
                            int(q["seat"]) == int(event.get("winner") or -1) for q in group_list
                        ):
                            # 点炮的对象也是群里监控的人：喜报里已经写过，不再单独发悲报
                            seen_yakuman.add(index)
                            continue
                        seen_yakuman.add(index)
                        image_path = self._yakuman_image(uuid, index, event)
                        if image_path is None:
                            # 图片生成失败时至少留一条日志，方便定位是牌面还原失败还是没装 Pillow
                            self._write_query_log(
                                "、".join(str(q["nickname"]) for q in group_list),
                                "役满牌面",
                                f"uuid={uuid} 第 {index + 1} 个役满没生成图片"
                                f"（还原到 {_PAIPU_HAND_LAST.get('count', 0)} 张牌），这次只发文字",
                                level="WARN",
                            )
                        extras.append(
                            (
                                _yakuman_message(
                                    str(p["nickname"]), event, with_tiles=image_path is None
                                ),
                                image_path,
                            )
                        )
            # 升段 / 掉段：播报之后再补一条（标明是三麻还是四麻段位）
            mode_text = "三麻" if head_sanma else "四麻"
            for p in group_list:
                if not (p.get("rank_direction") and p.get("new_rank_tag")):
                    continue
                if p["rank_direction"] == "up":
                    extras.append(
                        (
                            f"喜报！恭喜{p['nickname']}{mode_text}段位升段到{p['new_rank_tag']}！",
                            None,
                        )
                    )
                else:
                    extras.append(
                        (
                            f"悲报，{p['nickname']}没有守住{mode_text}段位，掉段到{p['new_rank_tag']}了！",
                            None,
                        )
                    )
            # 非好友播报：加好友提示放在最下面（和牌/放铳下面）
            if notice:
                message += "\n" + notice
            # 距到期不到 3 天：再跟在好友提示后面提醒重置期限
            expiry_warning = self._expiry_warning(primary["entry"], [gid_s])
            if expiry_warning:
                message += "\n" + expiry_warning

            label = "、".join(str(p["nickname"]) for p in group_list)
            await self._send_notification(message, [gid_s])
            paipu_note = self._paipu_problem_note(raw)
            self._write_query_log(
                label,
                "播报成功",
                f"群 {gid_s} | " + message.replace("\n", " | ") + paipu_note,
                level="INFO",
            )
            logger.info(
                f"[MajsoulMonitor] 已播报 {label} 的对局 {uuid}（{primary['rank']}位）到群 {gid_s}"
            )
            # 役满 / 段位变动的补充消息
            for extra_text, extra_image in extras:
                try:
                    await self._send_notification(extra_text, [gid_s])
                    self._write_query_log(
                        label, "追加播报", extra_text.replace("\n", " | "), level="INFO"
                    )
                    logger.info(f"[MajsoulMonitor] 已追加播报 {label}：{extra_text.splitlines()[0]}")
                    if extra_image:
                        sent = await self._send_image(extra_image, [gid_s])
                        self._write_query_log(
                            label,
                            "追加播报",
                            f"牌型图片 {extra_image}" if sent else f"牌型图片发送失败 {extra_image}",
                            level="INFO" if sent else "WARN",
                        )
                        await asyncio.sleep(0.3)
                except Exception as exc:
                    logger.warning(f"[MajsoulMonitor] 追加播报失败 {label}: {exc}")
                await asyncio.sleep(0.3)

    async def _resolve_monitored_player(self, nickname: str, allow_id: bool = True) -> Optional[Dict]:
        """把昵称（或雀魂ID）解析成账号资料，不要求是登录账号的好友。

        纯数字一律按「雀魂里显示的数字 ID」处理（网页添加、指令添加都一样），
        不再接受 account_id —— 那个数字在雀魂里根本看不到。
        """
        if self._monitor is None:
            return None
        key = str(nickname or "").strip()
        if not key:
            return None
        try:
            if key.isdigit():
                return await self._monitor.lookup_by_display_id(int(key))
            return await self._monitor.resolve_player(key, allow_id=False)
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 玩家解析失败: {exc}")
            return None

    async def _resolve_player_any(self, key: str, allow_id: bool = False) -> Optional[Dict[str, Any]]:
        """先正常解析；解析不到再到好友列表里按昵称/雀魂ID找（好友资料里就有段位）。"""
        player = await self._resolve_monitored_player(key, allow_id=allow_id)
        if player:
            return player
        if self._monitor is None:
            return None
        try:
            friend = await self._monitor.find_friend_by_name(key)
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 好友列表兜底查找失败: {exc}")
            return None
        if not friend:
            return None
        account_id = int(friend.get("account_id") or 0)
        if not account_id:
            return None
        logger.info(
            f"[MajsoulMonitor] 玩家 {key!r} 在好友列表里找到 -> {friend.get('nickname') or ''}({show_uid(account_id)})"
        )
        # 好友资料里四麻、三麻段位都有，原样带回；没上过雀杰的一边是 0，显示时自动省略
        return {
            "account_id": account_id,
            "nickname": str(friend.get("nickname") or key),
            "level_id": int(friend.get("level_id") or 0),
            "level_score": int(friend.get("level_score") or 0),
            "level3_id": int(friend.get("level3_id") or 0),
            "level3_score": int(friend.get("level3_score") or 0),
        }

    def _image_component(self, path: str):
        """把本地图片包成 AstrBot 的 Image 组件（不同版本接口不一样，挨个试）。"""
        from astrbot.core.message.components import Image

        candidates = [
            ("fromFileSystem", (str(path),), {}),
            ("fromFileSystem", (str(path),), None),
            (None, (), {"file": str(path)}),
            (None, (), {"path": str(path)}),
        ]
        last_error: Optional[Exception] = None
        for name, args, kwargs in candidates:
            try:
                if name and hasattr(Image, name):
                    return getattr(Image, name)(*args)
                if name is None and kwargs:
                    return Image(**kwargs)
            except Exception as exc:  # 换下一种写法
                last_error = exc
                continue
        logger.warning(f"[MajsoulMonitor] 构造图片消息失败: {last_error}")
        return None

    async def _send_chain(self, components: List[Any], group_ids: List[str]):
        """把一组消息组件发到群。"""
        from astrbot.core.message.message_event_result import MessageChain

        if not group_ids:
            logger.info("[MajsoulMonitor] 无群号，跳过发送")
            return
        # Discover actual platform ID
        actual_platform_id = "aiocqhttp"
        try:
            pm = self.context.platform_manager
            for p in pm.platform_insts:
                m = p.meta()
                logger.info(f"[MajsoulMonitor] Found platform: id='{m.id}', name='{m.name}'")
                if m.name == "aiocqhttp":
                    actual_platform_id = m.id
        except Exception as e:
            logger.warning(f"[MajsoulMonitor] Platform discovery failed: {e}", exc_info=True)
        chain = MessageChain(chain=components)
        for gid in group_ids:
            try:
                session = gid
                if gid.isdigit():
                    session = f"{actual_platform_id}:GroupMessage:{gid}"
                logger.info(f"[MajsoulMonitor] Sending to session: {session}")
                result = await self.context.send_message(session, chain)
                logger.info(f"[MajsoulMonitor] send_message result={result} for {session}")
            except Exception as e:
                logger.warning(f"[MajsoulMonitor] 发送到群 {gid} 失败: {e}", exc_info=True)

    async def _send_notification(self, message: str, group_ids: List[str]):
        """通过 AstrBot 消息平台发送通知"""
        from astrbot.core.message.components import Plain
        logger.info(f"[MajsoulMonitor] _send_notification called, group_ids={group_ids}, msg_len={len(message)}")
        await self._send_chain([Plain(message)], group_ids)

    async def _send_image(self, path: str, group_ids: List[str]) -> bool:
        """发送一张本地图片。"""
        component = self._image_component(path)
        if component is None:
            return False
        logger.info(f"[MajsoulMonitor] 发送图片 {path}")
        await self._send_chain([component], group_ids)
        return True

    def _yakuman_image(self, uuid: str, index: int, event: Dict[str, Any]) -> Optional[str]:
        """把役满和的那手牌拼成图片（缓存在 plugin_data/review/yakuman 下）。"""
        try:
            return self._yakuman_image_inner(uuid, index, event)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[MajsoulMonitor] 生成役满牌面失败（{uuid}#{index}）：{exc.__class__.__name__}: {exc}",
                exc_info=True,
            )
            return None

    def _yakuman_image_inner(self, uuid: str, index: int, event: Dict[str, Any]) -> Optional[str]:
        layout = event.get("layout")
        tiles = layout or event.get("tiles") or []
        if not tiles:
            return None
        safe_uuid = re.sub(r"[^0-9A-Za-z_-]", "_", str(uuid or "")) or "game"
        out_path = (
            StarTools.get_data_dir("astrbot_plugin_majsoul_matchreport")
            / "review"
            / "yakuman"
            / f"{safe_uuid}-{int(index)}-v2.png"
        )
        if out_path.exists():
            return str(out_path)
        path = _paipu_tile_image(tiles, out_path)
        return str(path) if path else None

    # ── 指令 ──────────────────────────────────────────

    # 群主/管理员身份缓存：群号 -> (读取时间, 群主ID, 管理员ID集合)
    _GROUP_ROLE_TTL = 300

    @staticmethod
    def _sender_role_from_raw(event) -> str:
        """从平台原始消息里取发送者身份（OneBot 的 sender.role：owner/admin/member）。"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return ""
        sender = raw.get("sender") if isinstance(raw, dict) else getattr(raw, "sender", None)
        if isinstance(sender, dict):
            return str(sender.get("role") or "").strip().lower()
        return str(getattr(sender, "role", "") or "").strip().lower() if sender is not None else ""

    async def _group_owner_admins(self, event, group_id: str):
        """取群主和管理员：返回 (群主ID, 管理员ID集合)，拿不到时返回 ("", set())。"""
        cache = getattr(self, "_group_role_cache", None)
        if cache is None:
            self._group_role_cache = cache = {}
        now = time.time()
        hit = cache.get(group_id)
        if hit and now - hit[0] < self._GROUP_ROLE_TTL:
            return hit[1], hit[2]
        getter = getattr(event, "get_group", None)
        if not callable(getter):
            return "", set()
        try:
            group = await getter()
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 读取群 {group_id} 成员身份失败: {exc}")
            return "", set()
        if group is None:
            return "", set()
        owner = str(getattr(group, "group_owner", "") or "")
        admins = {str(x) for x in (getattr(group, "group_admins", None) or [])}
        cache[group_id] = (now, owner, admins)
        return owner, admins

    async def _is_admin(self, event) -> bool:
        """播报指令权限：只有本群的群主/管理员（以及白名单里的账号）才能改四个播报开关。

        固定规则，不再受配置开关控制；只作用于 /三麻播报 /东场播报 /友人播报 /比赛播报，
        新增、删除、列表、牌谱、帮助不受影响。
        """
        user_id = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
        admins_cfg = [str(a).strip() for a in self.config.get("admins_id", [])]
        if user_id and user_id in admins_cfg:
            return True
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
        if not group_id or not user_id:
            return False
        if self._sender_role_from_raw(event) in ("owner", "admin", "administrator", "creator"):
            return True
        owner, admins = await self._group_owner_admins(event, group_id)
        return bool(user_id and (user_id == owner or user_id in admins))

    @staticmethod
    def _clean_cmd_name(raw, default: str) -> str:
        name = str(raw or "").strip().lstrip("/").strip()
        return name or default

    def _command_names(self) -> tuple:
        add_name = self._clean_cmd_name(self.config.get("cmd_add_name"), DEFAULT_CMD_ADD)
        remove_name = self._clean_cmd_name(self.config.get("cmd_remove_name"), DEFAULT_CMD_REMOVE)
        list_name = self._clean_cmd_name(self.config.get("cmd_list_name"), DEFAULT_CMD_LIST)
        paipu_name = self._clean_cmd_name(self.config.get("cmd_paipu_name"), DEFAULT_CMD_PAIPU)
        help_name = self._clean_cmd_name(self.config.get("cmd_help_name"), DEFAULT_CMD_HELP)
        return add_name, remove_name, list_name, paipu_name, help_name

    def _broadcast_cmd_names(self) -> Dict[str, str]:
        """四个「本群播报开关」指令的实际名字：kind -> 指令名。"""
        names: Dict[str, str] = {}
        for kind in BROADCAST_KINDS:
            key = BROADCAST_CMD_KEYS[kind]
            default = BROADCAST_CMD_DEFAULTS[kind]
            names[kind] = self._clean_cmd_name(self.config.get(key), default)
        return names

    @staticmethod
    def _match_cmd_arg(text: str, name: str):
        """匹配「/指令名 参数」或「/指令名参数」，不匹配返回 None。"""
        text = (text or "").strip()
        for prefix in ("/" + name, name):
            if text.startswith(prefix):
                return text[len(prefix):].strip()
        return None

    @staticmethod
    def _group_matches(stored, group_id: str, event) -> bool:
        stored = str(stored or "")
        if not stored:
            return False
        if stored == str(group_id):
            return True
        try:
            if stored == str(event.unified_msg_origin):
                return True
        except Exception:
            pass
        return stored.endswith(":" + str(group_id))

    def _log_command_usage(self, event: AstrMessageEvent, name: str, arg: str = "") -> None:
        """记录「谁在哪个群用了哪条指令」，方便在插件日志里查看指令使用情况。"""
        try:
            try:
                sender = str(event.get_sender_name() or "").strip()
            except Exception:
                sender = ""
            if not sender:
                _, _, sender_id = self._event_user_key(event)
                sender = sender_id or "未知用户"
            try:
                group_id = str(event.get_group_id() or "").strip()
            except Exception:
                group_id = ""
            parts = []
            if group_id:
                parts.append(f"群 {group_id}")
            else:
                origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
                if origin:
                    parts.append(f"会话 {origin}")
            if arg:
                parts.append(f"参数 {str(arg)[:100]}")
            self._write_query_log(sender, f"使用指令 /{name}", "，".join(parts) or "无参数", "INFO")
        except Exception:
            logger.debug("[MajsoulMonitor] 记录指令使用失败", exc_info=True)

    @filter.custom_filter(GroupLeaveFilter)
    async def on_group_leave(self, event: AstrMessageEvent):
        """机器人被踢出群聊 / 主动退出群聊时，把这个群的监控全部删掉。"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        group_id, reason = group_leave_info(raw)
        if not group_id:
            return
        # 这是通知类事件，不该走聊天/LLM 链路
        try:
            event.should_call_llm(False)
        except Exception:
            pass
        try:
            stats = self._remove_group_monitoring(group_id, reason)
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 退群清理监控失败: {exc}", exc_info=True)
            event.stop_event()
            return
        if not (stats["players"] or stats["removed"] or stats["settings"]):
            logger.info(f"[MajsoulMonitor] 群 {group_id} {reason}，但这个群本来就没有监控数据")
        else:
            logger.info(
                f"[MajsoulMonitor] 群 {group_id} {reason}，已清理 {stats['players']} 个玩家的监控"
                f"（{stats['removed']} 个玩家彻底停止监控）"
            )
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_custom_command(self, event: AstrMessageEvent):
        """可自定义名字的「新增/删除/列表/帮助」指令。"""
        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return
        # 顺手把群名记下来（网页上显示「群名 + 群号」，有缓存节流，不会每条都问）
        try:
            group_id_for_name = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
            if group_id_for_name:
                await self._remember_group_name(event, group_id_for_name)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[MajsoulMonitor] 记录群名失败: {exc}")
        # 默认指令名由 @filter.command 的静态处理器负责，这里跳过避免重复回复
        # 最后两个是改名前的旧默认指令名：直接忽略，避免被当成「雀魂播报 + xxx」的参数
        for default_name in (
            DEFAULT_CMD_ADD, DEFAULT_CMD_REMOVE, DEFAULT_CMD_LIST, DEFAULT_CMD_HELP,
            DEFAULT_CMD_PAIPU, DEFAULT_CMD_THREE, DEFAULT_CMD_EAST, DEFAULT_CMD_FRIEND,
            DEFAULT_CMD_CONTEST, "雀魂播报+", "雀魂播报-",
        ):
            if self._match_cmd_arg(text, default_name) is not None:
                return
        add_name, remove_name, list_name, paipu_name, help_name = self._command_names()
        # 四个播报开关指令（各群独立）
        for kind, cmd_name in self._broadcast_cmd_names().items():
            if not cmd_name:
                continue
            switch_arg = self._match_cmd_arg(text, cmd_name)
            if switch_arg is None:
                continue
            async for result in self._handle_broadcast_switch(event, kind, switch_arg):
                yield result
            return
        if self._match_cmd_arg(text, help_name) is not None:
            async for result in self._handle_custom_help(event):
                yield result
            return
        if self._match_cmd_arg(text, list_name) is not None:
            async for result in self._handle_custom_list(event):
                yield result
            return
        paipu_arg = self._match_cmd_arg(text, paipu_name)
        if paipu_arg is not None:
            async for result in self._handle_paipu_command(event, paipu_arg):
                yield result
            return
        add_arg = self._match_cmd_arg(text, add_name)
        if add_arg is not None:
            async for result in self._handle_custom_add(event, add_arg):
                yield result
            return
        remove_arg = self._match_cmd_arg(text, remove_name)
        if remove_arg is not None:
            async for result in self._handle_custom_remove(event, remove_arg):
                yield result

    async def _handle_custom_help(self, event: AstrMessageEvent):
        """显示本插件的全部指令（/雀魂播报帮助）；播报开关都是按群的，私聊不给用。"""
        add_name, remove_name, list_name, paipu_name, help_name = self._command_names()
        broadcast_names = self._broadcast_cmd_names()
        three_name = broadcast_names.get("three", DEFAULT_CMD_THREE)
        east_name = broadcast_names.get("east", DEFAULT_CMD_EAST)
        friend_name = broadcast_names.get("friend", DEFAULT_CMD_FRIEND)
        contest_name = broadcast_names.get("contest", DEFAULT_CMD_CONTEST)
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
        self._log_command_usage(event, help_name)
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用此指令")
            return
        try:
            nonfriend_limit = int(self.config.get("nonfriend_daily_limit", 2))
        except (TypeError, ValueError):
            nonfriend_limit = -1
        limit_text = "不限制" if nonfriend_limit < 0 else f"{nonfriend_limit}次"
        # 这里显示的是「本群」的设置，不是插件设置里的全局默认值
        settings = self._group_settings_of(group_id)

        valid_value, valid_unit = self._monitor_validity()
        if valid_unit == "forever":
            valid_text = "不限制"
        else:
            unit_text = {"day": "天", "week": "周", "month": "个月"}.get(valid_unit, "个月")
            valid_text = (
                f"{valid_value}{unit_text}"
                f"（添加监控{valid_value}{unit_text}后需重新添加监控）"
            )
        lines = [
            "📖 雀魂播报帮助",
            f"/{add_name} 玩家昵称或雀魂ID：添加监控",
            f"/{remove_name} 玩家昵称或雀魂ID：删除监控",
            f"/{list_name}：查看本群监控列表",
            f"/{paipu_name} 牌谱链接或ID：查看牌谱回放",
            f"/{three_name} 开|关：开启/关闭本群三麻播报",
            f"/{east_name} 开|关：开启/关闭本群东场播报",
            f"/{friend_name} 开|关：开启/关闭本群友人场播报",
            f"/{contest_name} 开|关：开启/关闭本群比赛场播报",
            f"/{help_name}：显示本帮助",
            "",
            "本群播报设置：",
            f"非好友播报上限：{limit_text}",
            f"三麻播报：{'开' if settings.get('three') else '关'}",
            f"东场播报：{'开' if settings.get('east') else '关'}",
            f"友人播报：{'开' if settings.get('friend') else '关'}",
            f"比赛播报：{'开' if settings.get('contest') else '关'}",
            f"监控有效期：{valid_text}",
        ]
        yield event.plain_result("\n".join(lines))

    # 播报开关指令可以接受的参数写法
    _SWITCH_ON_WORDS = {"开", "开启", "打开", "启用", "on", "open", "1", "true", "yes", "是"}
    _SWITCH_OFF_WORDS = {"关", "关闭", "关掉", "禁用", "off", "close", "0", "false", "no", "否"}

    async def _handle_broadcast_switch(self, event: AstrMessageEvent, kind: str, arg: str = ""):
        """处理「/三麻播报 开」这类指令：只改本群的开关。"""
        if kind not in BROADCAST_KINDS:
            return
        label = BROADCAST_LABELS.get(kind, kind)
        cmd_name = self._broadcast_cmd_names().get(kind, BROADCAST_CMD_DEFAULTS.get(kind, label))
        self._log_command_usage(event, cmd_name, arg)
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
        if not group_id:
            yield event.plain_result(f"⚠️ /{cmd_name} 只能在群聊里使用")
            return
        if not await self._is_admin(event):
            yield event.plain_result("⚠️ 你没有权限修改本群的播报设置（仅限本群管理员/群主）")
            return
        text = str(arg or "").strip().lower()
        if not text:
            current = self._group_setting(group_id, kind)
            yield event.plain_result(
                f"本群{label}：{'开启' if current else '关闭'}\n"
                f"用法：/{cmd_name} 开 或 /{cmd_name} 关"
            )
            return
        if text in self._SWITCH_ON_WORDS:
            enabled = True
        elif text in self._SWITCH_OFF_WORDS:
            enabled = False
        else:
            yield event.plain_result(f"⚠️ 参数只能是「开」或「关」，例如：/{cmd_name} 开")
            return
        changed = self._set_group_setting(group_id, kind, enabled)
        state_text = "开启" if enabled else "关闭"
        if changed:
            message = f"✅ 本群{label}已{state_text}"
        else:
            message = f"ℹ️ 本群{label}本来就是{state_text}"
        yield event.plain_result(message)

    @staticmethod
    def _event_user_key(event) -> tuple:
        """返回 (绑定键, 平台, 用户ID)；绑定按平台+用户ID，跨群生效。"""
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
        platform = ""
        for attr in ("get_platform_name", "get_platform_id"):
            fn = getattr(event, attr, None)
            if not callable(fn):
                continue
            try:
                platform = str(fn() or "").strip()
            except Exception:
                platform = ""
            if platform:
                break
        if not platform:
            origin = str(getattr(event, "unified_msg_origin", "") or "")
            if origin:
                platform = origin.split(":", 1)[0]
        key = f"{platform}:{sender_id}" if platform else sender_id
        return key, platform, sender_id


    async def _handle_paipu_command(self, event: AstrMessageEvent, arg: str = ""):
        """查看牌谱回放（/雀魂牌谱 链接或ID）。"""
        arg = (arg or "").strip()
        paipu_name = self._command_names()[3]
        self._log_command_usage(event, paipu_name, arg)
        if not arg:
            yield event.plain_result(f"用法：/{paipu_name} 牌谱链接或牌谱ID")
            return
        game_id = extract_paipu_id(arg)
        if not game_id:
            yield event.plain_result("❌ 无法识别牌谱链接或 ID。")
            return
        group_id = event.get_group_id()
        place = f"{group_id}群" if group_id else "私聊"
        self._write_query_log(
            "SYSTEM", "牌谱回放请求",
            f"在{place}请求回放牌谱 {game_id}",
            "INFO",
        )
        if self._monitor is None:
            self._write_query_log(
                "SYSTEM", "牌谱回放失败",
                f"在{place}请求回放牌谱 {game_id}，原因：监控未启动",
                "ERROR",
            )
            yield event.plain_result("⚠️ 监控未启动，请先在账号池登录账号。")
            return
        current_uid = str(getattr(self._monitor, "current_uid", "") or "")
        if current_uid and self._monitor.paipu_permission(current_uid) == "无":
            self._write_query_log(
                "SYSTEM", "牌谱回放失败",
                f"在{place}请求回放牌谱 {game_id}，原因：当前账号 {self._account_label(current_uid)} 今天没有牌谱权限",
                "ERROR",
            )
            yield event.plain_result("❌ 当前账号今天没有牌谱权限，无法查看牌谱回放。")
            return
        yield event.plain_result("🔍 正在拉取牌谱，请稍候…")
        timed_out = False
        try:
            raw = await asyncio.wait_for(self._monitor.fetch_paipu_log(game_id), timeout=40)
        except asyncio.TimeoutError:
            timed_out = True
            raw = None
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 拉取牌谱异常: {exc}")
            raw = None
        if not raw:
            reason = str(getattr(self._monitor, "last_paipu_error", "") or "")
            if not reason:
                reason = "拉取超时（40s）" if timed_out else "未知错误"
            self._write_query_log(
                "SYSTEM", "牌谱回放失败",
                f"在{place}请求回放牌谱 {game_id}，原因：{reason}",
                "ERROR",
            )
            yield event.plain_result(
                "❌ 牌谱拉取失败。\n请确认链接/ID 正确，且当前登录账号有牌谱查看权限。"
            )
            return
        cached = bool(getattr(self._monitor, "last_paipu_cache_hit", False))
        self._write_query_log(
            "SYSTEM", "牌谱回放",
            f"在{place}回放牌谱 {game_id}，"
            + ("使用本地牌谱缓存（不计数）" if cached else "使用一次调用次数"),
            "INFO",
        )
        summary = format_paipu_summary(raw, game_id)
        if len(summary) > 3500:
            summary = summary[:3500] + "\n…（内容过长，已截断）"
        yield event.plain_result(summary)

    async def _handle_custom_list(self, event: AstrMessageEvent):
        """显示本群监控列表（/雀魂播报列表）。"""
        self._log_command_usage(event, self._command_names()[2])
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用此指令。")
            return
        names = []
        for nick, info in self.monitored.items():
            gids = info.get("group_ids", []) or []
            if any(self._group_matches(g, group_id, event) for g in gids):
                names.append(nick)
        if not names:
            add_name = self._command_names()[0]
            yield event.plain_result(
                f"📋 还没有监控玩家。\n使用 /{add_name} 玩家昵称 添加。"
            )
            return
        lines = [f"📋 监控列表（{len(names)} 人）"]
        for idx, nick in enumerate(sorted(names), 1):
            info = self.monitored.get(nick, {})
            line = f"{idx}. {nick}"
            info_gids = info.get("group_ids", []) or []
            matched_gid = next(
                (g for g in info_gids if self._group_matches(g, group_id, event)), group_id
            )
            if self._group_expired(info, matched_gid):
                line += "（已到期，不再播报）"
            else:
                expire_ts = self._group_expire_ts(info, matched_gid)
                if expire_ts:
                    # 无限制时什么都不写，只留玩家名字
                    line += (
                        "  播报到期："
                        + datetime.fromtimestamp(expire_ts).strftime("%Y-%m-%d %H:%M")
                    )
            lines.append(line)
        yield event.plain_result("\n".join(lines))

    def _has_online_account(self) -> bool:
        """有没有账号正登录着；没有就没法搜玩家、也没法播报。"""
        monitor = self._monitor
        if monitor is None:
            return False
        try:
            return bool(monitor.online_sessions())
        except Exception:
            return False

    def _friend_pick_ids(self) -> str:
        """加好友提示用的账号：按账号池顺序，只挑好友没加满的前两个在线账号。"""
        monitor = self._monitor
        if monitor is not None:
            picker = getattr(monitor, "friend_pick_ids", None)
            if callable(picker):
                try:
                    picked = picker(2)
                    picked_ids = [str(x) for x in (picked or []) if str(x or "").strip()]
                    if picked_ids:
                        return "、".join(picked_ids)
                except Exception as exc:
                    logger.debug(f"[MajsoulMonitor] 挑加好友账号失败: {exc}")
        # 兜底（没有会话快照时）：老逻辑，列在线账号的雀魂ID
        ids: List[str] = []

        def add(account_id: Any) -> None:
            try:
                value = int(account_id or 0)
            except (TypeError, ValueError):
                return
            if value <= 0:
                return
            try:
                text = str(encode_account_id(value))
            except Exception:
                text = str(value)
            if text and text not in ids:
                ids.append(text)

        monitor = self._monitor
        if monitor is not None:
            try:
                for session in monitor.online_sessions():
                    account = getattr(session, "account", None) or {}
                    add(
                        account.get("account_id")
                        or account.get("player_id")
                        or getattr(session, "uid", "")
                    )
            except Exception:
                pass
        if not ids:
            return ""
        return "、".join(ids[:2])

    def _not_found_message(self, arg: str) -> str:
        """查不到玩家时的提示（让他先加一个在线账号当好友，才能搜到）。"""
        ids = self._friend_pick_ids()
        if not ids:
            # 兜底：正常流程在进函数前就确认过有在线账号，走不到这里
            return "目前无法播报，请联系管理员在账号池添加账号。"
        return (
            f"❌ 查询不到玩家「{arg}」。\n"
            f"请确认昵称正确，如该账号未达到雀杰，"
            f"请添加以下任意一个ID的雀魂账号为好友：{ids}，再使用本指令即可进行播报。"
        )

    async def _nonfriend_add_notice(self, account_id: int) -> str:
        """添加监控玩家后：这个号还不是任何在线账号的好友时，返回加好友提示。"""
        monitor = self._monitor
        if monitor is None or not account_id:
            return ""
        try:
            if int(getattr(monitor, "nonfriend_limit", lambda: -1)()) < 0:
                # 上限设成「无限制」时不再提示（没有次数要说明）
                return ""
        except Exception:
            return ""
        try:
            is_friend = await asyncio.wait_for(
                monitor.is_friend_of_any_fresh(int(account_id)), timeout=15
            )
        except Exception as exc:
            logger.debug(f"[MajsoulMonitor] 添加后检查好友关系失败: {exc}")
            try:
                is_friend = monitor.is_friend_of_any(int(account_id))
            except Exception:
                is_friend = False
        if is_friend:
            return ""
        try:
            return monitor.nonfriend_notice()
        except Exception:
            return ""

    def _validity_notice(self) -> str:
        """新增监控成功后的播报时限提示（跟随「监控有效期」设置）。"""
        value, unit = self._monitor_validity()
        if unit == "forever":
            return "播报时限为：不限制。"
        unit_text = {"day": "天", "week": "周", "month": "个月"}.get(unit, "个月")
        return f"播报时限为：{value}{unit_text}，到期需重新添加监控。"

    def _renew_notice(self) -> str:
        """同一个群重复添加时的续期提示（跟随「监控有效期」设置，默认 3 个月）。"""
        value, unit = self._monitor_validity()
        if unit == "forever":
            return "播报时限不限制。"
        unit_text = {"day": "天", "week": "周", "month": "个月"}.get(unit, "个月")
        return f"播报时限延长{value}{unit_text}。"

    async def _handle_custom_add(self, event: AstrMessageEvent, arg: str):
        # 新增监控不受「仅群管理员可用」限制，群成员都能用；那个开关只管四个播报指令
        self._log_command_usage(event, self._command_names()[0], arg)
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用此指令。")
            return
        add_name = self._command_names()[0]
        if not arg:
            yield event.plain_result(f"用法：/{add_name} 玩家昵称或雀魂ID")
            return
        if not self._has_online_account():
            yield event.plain_result("目前无法播报，请联系管理员在账号池添加账号。")
            return
        # 群里只按昵称搜索；纯数字也可能是昵称，同样走昵称搜索
        player = await self._resolve_player_any(arg, allow_id=False)
        if not player:
            yield event.plain_result(self._not_found_message(arg))
            return
        account_id = int(player.get("account_id") or 0)
        display_name = str(player.get("nickname") or arg)
        logger.info(f"[MajsoulMonitor] 解析监控玩家 {arg!r} -> {display_name}({show_uid(account_id)})")
        existed = display_name in self.monitored or arg in self.monitored
        entry = self.monitored.get(display_name, self.monitored.get(arg, {}))
        gids = list(entry.get("group_ids", []))
        group_added = False
        if group_id and not any(self._group_matches(g, group_id, event) for g in gids):
            gids.append(group_id)
            group_added = True
        now_ts = int(time.time())
        if group_id:
            mapping = entry.get("group_added_at")
            mapping = dict(mapping) if isinstance(mapping, dict) else {}
            if group_id not in mapping:
                mapping[group_id] = now_ts
            elif existed and not group_added:
                # 已经在这个群的监控列表里：重复添加视为续期，把这个群的监控时间重置为现在
                mapping[group_id] = now_ts
            entry["group_added_at"] = mapping
        if not entry.get("added_at"):
            # 第一次加入监控时记下添加时间，之后重复添加/补绑群不再覆盖
            entry["added_at"] = now_ts
        entry.update({
            "account_id": account_id,
            "level_id": int(player.get("level_id") or 0),
            "level_score": int(player.get("level_score") or 0),
            "group_ids": gids,
        })
        level3_id = int(player.get("level3_id") or 0)
        if level3_id:
            entry["level3_id"] = level3_id
            entry["level3_score"] = int(player.get("level3_score") or 0)
        self.monitored[display_name] = entry
        if display_name != arg:
            self.monitored.pop(arg, None)
        save_monitored(self.monitored)

        # 只有真正新增玩家或新增群绑定时才记录；同群重复绑定不再刷日志
        if not existed or group_added:
            self._write_query_log(
                "SYSTEM", "添加监控玩家",
                f"{display_name}({show_uid(account_id)}) 由群 {group_id} 添加（指令）",
                "INFO",
            )
            logger.info(
                f"[MajsoulMonitor] 群 {group_id} 通过自定义指令添加监控玩家: "
                f"{display_name}({show_uid(account_id)})"
            )

        extra = await self._nonfriend_add_notice(account_id)
        if existed and not group_added:
            suffix = f"\n{extra}" if extra else ""
            yield event.plain_result(f"ℹ️ {display_name} 已在监控列表中，{self._renew_notice()}{suffix}")
        else:
            # 播报时限放在未加好友提示前面（两行各占一行）
            suffix = f"\n{self._validity_notice()}"
            if extra:
                suffix += f"\n{extra}"
            try:
                display_id = encode_account_id(account_id)
            except Exception:
                display_id = account_id
            # 四麻/三麻段位都写上，例如「四麻雀圣3 3506/9000，三麻雀圣2 1200/9000」
            rank_text = format_rank_pairs(
                int(player.get("level_id") or 0),
                int(player.get("level_score") or 0),
                int(player.get("level3_id") or 0),
                int(player.get("level3_score") or 0),
            )
            head = f"{rank_text}，" if rank_text else ""
            yield event.plain_result(
                f"✅ 已新增监控：{display_name}（{head}雀魂ID {display_id}）{suffix}"
            )

    async def _handle_custom_remove(self, event: AstrMessageEvent, arg: str):
        # 删除监控同样不受「仅群管理员可用」限制
        self._log_command_usage(event, self._command_names()[1], arg)
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 请在群聊中使用此指令。")
            return
        remove_name = self._command_names()[1]
        if not arg:
            yield event.plain_result(f"用法：/{remove_name} 玩家昵称或雀魂ID")
            return

        key = None
        if arg.isdigit():
            key = self._match_monitored(int(arg), "")
        if key is None:
            for nickname in self.monitored:
                if nickname == arg:
                    key = nickname
                    break
        if key is None:
            player = await self._resolve_player_any(arg, allow_id=False)
            if player:
                key = self._match_monitored(
                    int(player.get("account_id") or 0), str(player.get("nickname") or "")
                )
        if key is None:
            yield event.plain_result(f"❌ 监控列表里没有找到「{arg}」。")
            return

        entry = self.monitored[key]
        gids = list(entry.get("group_ids", []))
        new_gids = [g for g in gids if not self._group_matches(g, group_id, event)]
        if len(new_gids) == len(gids):
            yield event.plain_result(f"⚠️ 没有需要删除的监控：{key}。")
            return
        if new_gids:
            entry["group_ids"] = new_gids
            mapping = entry.get("group_added_at")
            if isinstance(mapping, dict):
                removed = [str(g) for g in gids if g not in new_gids]
                entry["group_added_at"] = {
                    k: v for k, v in mapping.items() if k not in removed
                }
            save_monitored(self.monitored)
            self._write_query_log(
                "SYSTEM", "解除群绑定",
                f"{key} 群 {group_id}，剩余 {','.join(new_gids)}",
                "INFO",
            )
            logger.info(f"[MajsoulMonitor] {key} 解除群 {group_id} 绑定（指令），剩余 {new_gids}")
            yield event.plain_result(f"✅ 已删除监控：{key}")
        else:
            acc = int(self.monitored[key].get("account_id") or self.monitored[key].get("player_id") or 0)
            del self.monitored[key]
            save_monitored(self.monitored)
            if self._monitor is not None:
                try:
                    self._monitor.drop_pending_for_player(acc, key)
                except Exception:
                    pass
            shown = f"{key}({show_uid(acc)})" if acc else key
            self._write_query_log("SYSTEM", "删除监控玩家", f"{shown}（所有群，指令）", "INFO")
            logger.info(f"[MajsoulMonitor] 通过自定义指令删除监控玩家: {shown}")
            yield event.plain_result(f"✅ 已删除监控：{key}")

    @filter.command(DEFAULT_CMD_ADD)
    async def cmd_add_monitor(self, event: AstrMessageEvent, nickname: str = ""):
        """添加雀魂玩家到监控列表（默认指令名）"""
        arg = nickname or self._match_cmd_arg(getattr(event, "message_str", ""), DEFAULT_CMD_ADD) or ""
        async for result in self._handle_custom_add(event, arg):
            yield result

    @filter.command(DEFAULT_CMD_REMOVE)
    async def cmd_remove_monitor(self, event: AstrMessageEvent, nickname: str = ""):
        """从监控列表中移除玩家（默认指令名）"""
        arg = nickname or self._match_cmd_arg(getattr(event, "message_str", ""), DEFAULT_CMD_REMOVE) or ""
        async for result in self._handle_custom_remove(event, arg):
            yield result

    @filter.command(DEFAULT_CMD_LIST)
    async def cmd_list_monitor(self, event: AstrMessageEvent):
        """查看本群监控列表（默认指令名）"""
        async for result in self._handle_custom_list(event):
            yield result

    @filter.command(DEFAULT_CMD_HELP)
    async def cmd_help(self, event: AstrMessageEvent):
        """显示插件帮助（默认指令名）"""
        async for result in self._handle_custom_help(event):
            yield result

    @filter.command(DEFAULT_CMD_PAIPU)
    async def cmd_paipu(self, event: AstrMessageEvent, arg: str = ""):
        """查看牌谱回放（/雀魂牌谱 链接或ID）"""
        if not arg:
            arg = self._match_cmd_arg(getattr(event, "message_str", ""), DEFAULT_CMD_PAIPU) or ""
        async for result in self._handle_paipu_command(event, arg):
            yield result

    @filter.command(DEFAULT_CMD_THREE)
    async def cmd_three_player_switch(self, event: AstrMessageEvent, arg: str = ""):
        """开启/关闭本群三麻播报（/三麻播报 开|关）"""
        if not arg:
            arg = self._match_cmd_arg(getattr(event, "message_str", ""), DEFAULT_CMD_THREE) or ""
        async for result in self._handle_broadcast_switch(event, "three", arg):
            yield result

    @filter.command(DEFAULT_CMD_EAST)
    async def cmd_east_switch(self, event: AstrMessageEvent, arg: str = ""):
        """开启/关闭本群东场播报（/东场播报 开|关）"""
        if not arg:
            arg = self._match_cmd_arg(getattr(event, "message_str", ""), DEFAULT_CMD_EAST) or ""
        async for result in self._handle_broadcast_switch(event, "east", arg):
            yield result

    @filter.command(DEFAULT_CMD_FRIEND)
    async def cmd_friend_switch(self, event: AstrMessageEvent, arg: str = ""):
        """开启/关闭本群友人场播报（/友人播报 开|关）"""
        if not arg:
            arg = self._match_cmd_arg(getattr(event, "message_str", ""), DEFAULT_CMD_FRIEND) or ""
        async for result in self._handle_broadcast_switch(event, "friend", arg):
            yield result

    @filter.command(DEFAULT_CMD_CONTEST)
    async def cmd_contest_switch(self, event: AstrMessageEvent, arg: str = ""):
        """开启/关闭本群比赛场播报（/比赛播报 开|关）"""
        if not arg:
            arg = self._match_cmd_arg(getattr(event, "message_str", ""), DEFAULT_CMD_CONTEST) or ""
        async for result in self._handle_broadcast_switch(event, "contest", arg):
            yield result


# ── 账号池功能 ─────────────────────────────────────────────
import hmac
import hashlib


# ── 账号池 WebUI API ───────────────────────────────────────
# 内置 review 连接模块（已从 astrbot_plugin_majsoul 整合）
import sys as _sys2
from pathlib import Path as _Path2

_majsoul_plugin_path = _Path2(__file__).parent
if str(_majsoul_plugin_path) not in _sys2.path:
    _sys2.path.insert(0, str(_majsoul_plugin_path))

# review 是以顶层包名 import 的，AstrBot 热重载只会重新加载插件自身模块，
# 旧的 review.* 会残留在 sys.modules 里继续跑改之前的代码。这里在插件导入时清理掉，
# 保证重载后用的始终是磁盘上的最新代码。
for _stale_module in [m for m in list(_sys2.modules) if m == "review" or m.startswith("review.")]:
    _sys2.modules.pop(_stale_module, None)

ACCOUNTS_FILE = "majsoul_matchreport_accounts.json"

def _plugin_config_path() -> Path:
    """AstrBot 实际读取的插件配置文件：data/config/<插件目录名>_config.json。"""
    base = StarTools.get_data_dir("astrbot_plugin_majsoul_matchreport")
    plugin_dir_name = Path(__file__).parent.name
    return base.parent.parent / "config" / f"{plugin_dir_name}_config.json"

def _accounts_path() -> Path:
    base = StarTools.get_data_dir("astrbot_plugin_majsoul_matchreport")
    base.mkdir(parents=True, exist_ok=True)
    return base / ACCOUNTS_FILE

def _load_accounts() -> list:
    p = _accounts_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8-sig") or "[]")
    except Exception:
        return []

def _save_accounts(accounts: list):
    _accounts_path().write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")

def _mask_username(username: str) -> str:
    if len(username) <= 4:
        return username[0] + "***"
    return username[:2] + "***" + username[-2:]

def _encode_password(password: str) -> str:
    return hmac.new(b"lailai", password.encode(), hashlib.sha256).hexdigest()


# 将账号池 API 方法注入到原插件类中
async def _api_accounts_list(self):
    """API: 返回账号池列表（含实时登录状态）"""
    accounts = _load_accounts()
    monitor = self._monitor
    current_uid = str(getattr(monitor, "current_uid", "") or "") if monitor else ""
    monitor_reconnecting = bool(monitor and getattr(monitor, "reconnecting", False))
    items = []
    sessions = {}
    if monitor is not None:
        for session in getattr(monitor, "_sessions", {}).values():
            sessions[str(session.uid)] = session
    # 好友列表里上次刷新的数量/上限（没刷过就没有数据，页面显示 -）
    friend_stats = {}
    if monitor is not None:
        try:
            for snap in monitor.friends_snapshot():
                snap_uid = str(snap.get("uid") or "")
                if not snap_uid:
                    continue
                # friends_at 为 0 说明这个账号还没成功拉过好友列表，页面显示 -
                if not int(snap.get("friends_at") or 0):
                    continue
                friend_stats[snap_uid] = {
                    "count": int(snap.get("count") or 0),
                    "max": int(snap.get("friend_max") or 0),
                    "at": int(snap.get("friends_at") or 0),
                }
        except Exception as exc:
            logger.debug(f"[MajsoulMonitor] 读取好友快照失败（账号池好友列显示 -）：{exc}")
    for idx, acc in enumerate(accounts, 1):
        uid = str(acc.get("uid", ""))
        fstat = friend_stats.get(uid) or None
        is_current = bool(monitor and current_uid == uid)
        session = sessions.get(uid)
        if session is not None and session.is_online():
            login_status = "已登录"
        elif is_current and monitor_reconnecting:
            login_status = "未登录，重连中"
        else:
            login_status = "未登录"
        items.append({
            "index": idx,
            "uid": uid,
            "display_uid": encode_account_id(int(uid)) if uid.isdigit() else 0,
            "username_masked": _mask_username(acc.get("username", "")),
            "nickname": acc.get("nickname", ""),
            "friend_count": (fstat or {}).get("count"),
            "friend_max": (fstat or {}).get("max"),
            "friends_at": (fstat or {}).get("at", 0),
            "enabled": acc.get("enabled", True),
            "paipu_perm": monitor.paipu_permission(uid) if monitor is not None else "有",
            "paipu_today": monitor.quota.used(uid) if monitor is not None else 0,
            "is_current": is_current,
            "login_status": login_status,
            "last_status": acc.get("last_status", "unknown"),
            "last_error": (acc.get("last_error", "") or "")[:240],
            "updated_at": datetime.fromtimestamp(acc.get("updated_at", 0)).strftime("%Y-%m-%d %H:%M:%S") if acc.get("updated_at") else "",
        })
    return jsonify({"ok": True, "data": {"items": items}})

async def _api_accounts_login(self):
    """API: 登录雀魂并添加到账号池"""
    data = await request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"ok": False, "message": "请输入用户名和密码"})
    try:
        from review.majsoul_connection import create_connection
        conn = await asyncio.wait_for(
            create_connection(username=username, password=password, url_base=self._server_url()),
            timeout=120,
        )
        # 保存账号信息
        accounts = _load_accounts()
        now = int(time.time())
        account_entry = {
            "uid": str(conn.account_id),
            "username": username,
            "password": password,
            "token": conn.access_token,
            "nickname": conn.nick_name,
            "last_status": "ok",
            "last_error": "",
            "updated_at": now,
        }
        # 更新或追加
        found = False
        for i, acc in enumerate(accounts):
            if acc.get("uid") == str(conn.account_id) or acc.get("username") == username:
                # 重新登录不要丢掉原来的启用状态
                account_entry["enabled"] = bool(acc.get("enabled", True))
                accounts[i] = account_entry
                found = True
                break
        if not found:
            account_entry.setdefault("enabled", True)
            accounts.append(account_entry)
        _save_accounts(accounts)
        if self._monitor is not None:
            try:
                # 账号被删除后重新加入：牌谱权限恢复为「有」
                if not found:
                    self._monitor.clear_paipu_permission(
                        str(conn.account_id), "加入账号池"
                    )
                self._monitor.notify_accounts_changed()
            except Exception:
                pass
        # 关闭验证连接
        try:
            await asyncio.wait_for(conn.close(), timeout=5)
        except Exception:
            pass
        new_uid = str(conn.account_id)
        switched = False
        current_uid = str(getattr(self._monitor, "current_uid", "") or "") if self._monitor else ""
        if self._monitor is not None:
            # 账号池里启用的账号本来都会在线，新账号补一条连接即可
            try:
                await self._monitor.refresh_sessions()
                switched = not current_uid
            except Exception as exc:
                logger.warning(f"[MajsoulMonitor] 新账号登录失败: {exc}")
        logger.info(
            f"[MajsoulMonitor] 账号验证成功（已加入池）: username={username}, nickname={conn.nick_name}, uid={show_uid(new_uid)}"
        )
        self._write_query_log(
            username, "账号已加入池",
            f"nickname={conn.nick_name}, uid={show_uid(new_uid)}", level="INFO",
        )
        if switched:
            msg = f"登录成功: {conn.nick_name}({show_uid(new_uid)})，已切换为当前查询账号"
        elif current_uid:
            msg = f"登录成功: {conn.nick_name}({show_uid(new_uid)})，已加入账号池；当前查询仍使用 {show_uid(current_uid)}"
        else:
            msg = f"登录成功: {conn.nick_name}({show_uid(new_uid)})，已加入账号池"
        return jsonify({"ok": True, "message": msg})
    except Exception as e:
        logger.error(f"[MajsoulMonitor] 雀魂登录失败: username={username}, error={e}", exc_info=True)
        self._write_query_log(username, "登录失败", str(e), level="ERROR")
        return jsonify({"ok": False, "message": f"登录失败: {e}"})

async def _api_accounts_delete(self):
    """API: 从账号池删除账号"""
    data = await request.get_json(silent=True) or {}
    identifier = (data.get("identifier") or "").strip()
    if not identifier:
        return jsonify({"ok": False, "message": "请输入要删除的账号标识"})
    accounts = _load_accounts()
    removed_uid = ""
    removed_label = ""
    removed = False
    if identifier.isdigit():
        idx = int(identifier) - 1
        if 0 <= idx < len(accounts):
            removed_uid = str(accounts[idx].get("uid", ""))
            removed_label = str(accounts[idx].get("nickname") or "").strip()
            accounts.pop(idx)
            removed = True
    if not removed:
        kept = []
        for acc in accounts:
            if str(acc.get("uid")) == identifier or str(acc.get("username")) == identifier:
                removed_uid = str(acc.get("uid", ""))
                removed_label = str(acc.get("nickname") or "").strip()
            else:
                kept.append(acc)
        if removed_uid:
            accounts = kept
            removed = True
    if removed:
        _save_accounts(accounts)
        monitor = self._monitor
        if monitor is not None and removed_uid:
            try:
                # 账号池里没存昵称时，用在线会话的 label（昵称 + 数字 ID）
                if not removed_label:
                    removed_label = str(monitor.account_label(removed_uid) or "").strip()
            except Exception:
                pass
        if monitor is not None:
            try:
                # 删除后当天重新加入时，牌谱权限恢复为「有」
                if removed_uid:
                    monitor.clear_paipu_permission(removed_uid, "已从账号池删除")
                monitor.notify_accounts_changed()
                await monitor.refresh_sessions()
            except Exception as exc:
                logger.warning(f"[MajsoulMonitor] 删除账号后处理失败: {exc}")
        shown = show_uid(removed_uid) or identifier
        if removed_label and removed_label != shown:
            shown = f"{removed_label}({shown})" if "(" not in removed_label else removed_label
        self._write_query_log("SYSTEM", "删除账号", shown, "INFO")
        return jsonify({"ok": True, "message": "删除成功"})
    return jsonify({"ok": False, "message": "未找到对应账号"})

async def _sync_login_after_pool_change(monitor, reason: str):
    """账号池启用/排序/删除后，让在线会话与「所有启用的账号」对齐（已在线的不重连）。"""
    if monitor is None:
        return None, False
    try:
        await monitor.refresh_sessions()
    except Exception as exc:
        logger.warning(f"[MajsoulMonitor] 账号池变更后调整登录失败（{reason}）: {exc}")
        return None, False
    return None, True


async def _api_accounts_toggle(self):
    """API: 启用/禁用账号；禁用后不会再用它登录"""
    data = await request.get_json(silent=True) or {}
    uid = str(data.get("uid") or "").strip()
    enabled = bool(data.get("enabled", True))
    if not uid:
        return jsonify({"ok": False, "message": "缺少账号 uid"})
    accounts = _load_accounts()
    target = None
    for acc in accounts:
        if str(acc.get("uid", "")) == uid:
            acc["enabled"] = enabled
            target = acc
            break
    if target is None:
        return jsonify({"ok": False, "message": "未找到对应账号"})
    _save_accounts(accounts)

    nickname = target.get("nickname") or uid
    logger.info(f"[MajsoulMonitor] 账号 {nickname}({show_uid(uid)}) 已{'启用' if enabled else '禁用'}")
    self._write_query_log("SYSTEM", "账号启用" if enabled else "账号禁用",
                          f"{nickname}({show_uid(uid)})", "INFO")

    switched = False
    target = None
    sync_checked = False
    monitor = self._monitor
    perm_hint = ""
    if enabled and monitor is not None and monitor.paipu_permission(uid) == "无":
        perm_hint = "；该账号今天已被标记「无牌谱权限」，如确认有权限请点「恢复权限」"
        self._write_query_log(
            "SYSTEM", "账号启用提示",
            f"{nickname}({show_uid(uid)}) 今天已被标记「无牌谱权限」，如确认有权限请点「恢复权限」",
            "WARN",
        )
    if monitor is not None:
        try:
            monitor.notify_accounts_changed()
            target, switched = await _sync_login_after_pool_change(
                monitor,
                f"账号 {nickname}({show_uid(uid)}) 已{'启用' if enabled else '禁用'}",
            )
            sync_checked = True
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 账号变更后切换账号失败: {exc}")
    if not sync_checked:
        switch_msg = ""
    elif switched:
        switch_msg = "，登录账号已按账号池更新"
    else:
        switch_msg = "，登录状态未变化"
    return jsonify({
        "ok": True,
        "message": f"已{'启用' if enabled else '禁用'} {nickname}" + switch_msg + perm_hint,
    })

async def _api_accounts_reorder(self):
    """API: 调整账号顺序（越靠前越优先被使用）"""
    data = await request.get_json(silent=True) or {}
    uids = data.get("uids")
    if not isinstance(uids, list) or not uids:
        return jsonify({"ok": False, "message": "缺少顺序列表"})
    accounts = _load_accounts()
    by_uid = {str(acc.get("uid", "")): acc for acc in accounts}
    ordered = []
    used = set()
    for uid in uids:
        key = str(uid)
        acc = by_uid.get(key)
        if acc is not None and key not in used:
            ordered.append(acc)
            used.add(key)
    # 未出现在提交列表里的账号按原顺序补到末尾，避免丢账号
    for acc in accounts:
        key = str(acc.get("uid", ""))
        if key not in used:
            ordered.append(acc)
            used.add(key)
    if len(ordered) != len(accounts):
        return jsonify({"ok": False, "message": "账号列表不匹配，请刷新后重试"})
    _save_accounts(ordered)
    names = " > ".join(str(acc.get("nickname") or acc.get("uid")) for acc in ordered)
    logger.info(f"[MajsoulMonitor] 账号顺序已更新: {names}")
    self._write_query_log("SYSTEM", "账号排序", names, "INFO")

    # 顺序变了只切换账号，不重启监控/不无条件切断当前账号
    switched = False
    target = None
    sync_checked = False
    if self._monitor is not None and self.config.get("enabled", True):
        try:
            target, switched = await _sync_login_after_pool_change(
                self._monitor, "账号顺序调整"
            )
            sync_checked = True
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 顺序调整后切换账号失败: {exc}")
    if not sync_checked:
        switch_msg = ""
    elif switched:
        switch_msg = "，登录账号已按账号池更新"
    else:
        switch_msg = "，登录状态未变化"
    return jsonify({
        "ok": True,
        "message": "顺序已保存" + switch_msg,
    })

async def _api_accounts_restore_perm(self):
    """API: 手动恢复某账号的牌谱权限，并从账号池顶部重新选择账号登录"""
    data = await request.get_json(silent=True) or {}
    uid = str(data.get("uid") or "").strip()
    if not uid:
        return jsonify({"ok": False, "message": "缺少账号 uid"})
    monitor = self._monitor
    if monitor is None:
        return jsonify({"ok": False, "message": "监控未启动"})
    monitor.clear_paipu_permission(uid, log=False)
    # 从顶部开始找第一个「已启用且有权限」的账号，作为重新登录的目标
    target_uid = ""
    try:
        for acc in monitor._accounts():
            if not bool(acc.get("enabled", True)):
                continue
            aid = str(acc.get("uid", ""))
            if monitor.paipu_permission(aid) == "有":
                target_uid = aid
                break
    except Exception as exc:
        logger.debug(f"[MajsoulMonitor] 恢复权限后选择账号失败: {exc}")
    current_uid = str(getattr(monitor, "current_uid", "") or "")
    already_top = bool(target_uid) and current_uid == target_uid and bool(getattr(monitor, "connected", False))
    if target_uid and not already_top:
        monitor.current_uid = target_uid
        try:
            await monitor.refresh_sessions()
        except Exception as exc:
            logger.warning(f"[MajsoulMonitor] 恢复权限后调整登录失败: {exc}")
    shown_uid = show_uid(uid)
    shown_target = show_uid(target_uid) if target_uid else ""
    self._write_query_log(
        "SYSTEM", "恢复牌谱权限",
        f"{shown_uid}" + ("" if already_top else (f"，重新选择账号 {shown_target}" if shown_target else "")),
        "INFO",
    )
    if already_top:
        msg = f"已恢复 {shown_uid} 的牌谱权限；当前已是最高优先级的账号，保持登录"
    elif shown_target:
        msg = f"已恢复 {shown_uid} 的牌谱权限，正在重新选择账号"
    else:
        msg = f"已恢复 {shown_uid} 的牌谱权限"
    return jsonify({"ok": True, "message": msg, "paipu_perm": monitor.paipu_permission(uid)})

# 注入方法到原插件类
MajsoulMonitorPlugin._api_accounts_list = _api_accounts_list
MajsoulMonitorPlugin._api_accounts_login = _api_accounts_login
MajsoulMonitorPlugin._api_accounts_delete = _api_accounts_delete
MajsoulMonitorPlugin._api_accounts_toggle = _api_accounts_toggle
MajsoulMonitorPlugin._api_accounts_reorder = _api_accounts_reorder
MajsoulMonitorPlugin._api_accounts_restore_perm = _api_accounts_restore_perm




