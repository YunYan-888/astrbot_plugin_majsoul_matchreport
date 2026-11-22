"""雀魂两套 ID 的互转。

- account_id：登录/接口里用的账号 ID，例如 23091527；
- 游戏里显示的数字 ID（加好友用的那个），例如 12345678。

换算方式来自雀魂前端（encode/decode_account_id2），是 32 位整数上的两段循环移位
再加/减 0x04000000 与异或 0x5DAD7E（6139246）。这里按 32 位语义原样移植。
"""

from __future__ import annotations

import json
from pathlib import Path

_MASK32 = 0xFFFFFFFF
_XOR_KEY = 6139246          # 0x5DAD7E
_HIGH_MASK = -67108864      # 0xFC000000（按 32 位符号语义取高位）
_BASE = 10_000_000


def _int32(value: int) -> int:
    """截成 32 位有符号整数（和 JS 的位运算语义一致）。"""
    value &= _MASK32
    return value - 0x100000000 if value & 0x80000000 else value


def encode_account_id(account_id: int) -> int:
    """account_id → 游戏里显示的数字 ID。失败时原样返回。"""
    try:
        source = int(account_id)
    except (TypeError, ValueError):
        return 0
    if source <= 0:
        return source
    value = _int32(source ^ _XOR_KEY)
    low = value & 67108863
    for _ in range(5):
        low = _int32(((511 & low) << 17) | (low >> 9))
    return _int32(low + (_HIGH_MASK & value) + _BASE)


def decode_account_id(display_id: int) -> int:
    """游戏里显示的数字 ID → account_id。失败时原样返回。"""
    try:
        source = int(display_id)
    except (TypeError, ValueError):
        return 0
    if source <= 0:
        return source
    value = _int32(source - _BASE)
    low = value & 67108863
    for _ in range(5):
        low = _int32(((131071 & low) << 9) | (low >> 17))
    return _int32(low + (_HIGH_MASK & value)) ^ _XOR_KEY


# ── 存档里到底存的是哪种 ID ─────────────────────────────
# 存档（监控列表/绑定/待查/播报计数）统一存「雀魂里显示的数字 ID」，
# 但这个标记文件不存在时按老格式（account_id）读，读完会在下次保存时换成新格式。
IDS_FORMAT_FILE = "ids_format.json"


def ids_are_majsoul(data_dir) -> bool:
    """存档里存的 ID 是不是已经是雀魂 ID。"""
    path = Path(data_dir) / IDS_FORMAT_FILE
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except Exception:
        return False
    return bool(isinstance(data, dict) and data.get("majsoul"))


def set_ids_majsoul(data_dir) -> None:
    path = Path(data_dir) / IDS_FORMAT_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"majsoul": True}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def to_majsoul(account_id: int) -> int:
    """安全版编码：任何异常都退回原值。"""
    try:
        return encode_account_id(int(account_id))
    except Exception:
        return account_id


def to_account_id(display_id: int) -> int:
    """安全版解码：任何异常都退回原值。"""
    try:
        return decode_account_id(int(display_id))
    except Exception:
        return display_id

