"""离线渲染某局牌谱里的役满牌型图（不需要登录雀魂）。

用法（在插件目录下运行，需要 Pillow）：
    python tools/render_yakuman.py "plugin_data/.../review/paipu/261115-xxxx - raw.json"

牌谱 JSON 就是插件从 fetchGameRecord 解析后缓存在
`plugin_data/astrbot_plugin_majsoul_matchreport/review/paipu/<牌谱uuid> - raw.json`
的那个文件（tenhou 格式，带 "log" 和 "head"）。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PLUGIN_DIR = Path(__file__).resolve().parent.parent
MAIN_PY = PLUGIN_DIR / "main.py"

WANT_FUNCS = {
    "_tenhou_tile_key",
    "_tenhou_tile_code",
    "_tenhou_tile_file_name",
    "_tenhou_symbol_tokens",
    "_paipu_agari_items",
    "_paipu_text",
    "_paipu_names",
    "_paipu_dealer_seat",
    "_paipu_int_list",
    "_paipu_round_label",
    "_paipu_meld_hand_tiles",
    "_paipu_meld_tiles",
    "_paipu_parse_meld",
    "_paipu_meld_relative",
    "_paipu_seat_tiles",
    "_paipu_winner_layout",
    "_paipu_winner_hand",
    "_paipu_tile_chars",
    "_paipu_tile_image",
    "_paipu_image_groups",
    "_paipu_yakuman_name",
    "_paipu_yakuman_events",
    "_yakuman_mult_text",
    "_yakuman_message",
    "_PAIPU_TILE_DEV_DUMMY",
}
WANT_PREFIX = ("_PAIPU_TILE", "_TENHOU_", "_PAIPU_HAND_LAST", "_YAKUMAN_VALUES", "_PAIPU_TRAD_TO_SIMP")


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


def _load_main_namespace() -> Dict[str, Any]:
    """从 main.py 里挑出牌谱相关的纯函数，避免 import 整个插件（会拉起 AstrBot）。"""
    ns: Dict[str, Any] = {
        "re": re,
        "Path": Path,
        "Dict": Dict,
        "List": List,
        "Any": Any,
        "Optional": Optional,
        "Tuple": Tuple,
        "logger": _Logger(),
        "__file__": str(MAIN_PY),
    }
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<main>", "exec"), ns)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (
                [t.id for t in node.targets if isinstance(t, ast.Name)]
                if isinstance(node, ast.Assign)
                else [node.target.id]
                if isinstance(node.target, ast.Name)
                else []
            )
            if any(t.startswith(WANT_PREFIX) for t in targets):
                exec(compile(ast.Module(body=[node], type_ignores=[]), "<main>", "exec"), ns)
    return ns


def _nickname(record: Dict[str, Any], seat: int) -> str:
    head = record.get("head") or {}
    accounts = head.get("accounts") or []
    if 0 <= seat < len(accounts):
        return str(accounts[seat].get("nickname") or f"座位{seat}")
    return f"座位{seat}"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="渲染牌谱里的役满牌型图")
    parser.add_argument("record", help="review/paipu 下的 '<uuid> - raw.json'")
    parser.add_argument("--seat", type=int, default=-1, help="只渲染某个座位（默认全部）")
    parser.add_argument("--out", default="", help="输出目录（默认当前目录）")
    args = parser.parse_args(argv)

    record_path = Path(args.record)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if "log" not in record and isinstance(record.get("game"), dict):
        record = record["game"]

    ns = _load_main_namespace()
    out_dir = Path(args.out) if args.out else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)

    seats = [args.seat] if args.seat >= 0 else [0, 1, 2, 3]
    found = 0
    for seat in seats:
        events = ns["_paipu_yakuman_events"](record, seat)
        for index, event in enumerate(events):
            if event.get("kind") not in ("win", "deal"):
                continue
            found += 1
            nickname = _nickname(record, seat)
            message = ns["_yakuman_message"](nickname, event, with_tiles=event.get("layout") is None)
            print("-" * 60)
            print(f"座位 {seat}（{nickname}）第 {index + 1} 个役满：")
            print(message)
            layout = event.get("layout")
            if not layout:
                print("  [牌面还原失败] 只输出文字")
                continue
            path = out_dir / f"yakuman-seat{seat}-{index + 1}.png"
            saved = ns["_paipu_tile_image"](layout, path)
            print(f"  牌型图：{saved}（{layout['total']} 张）")

    if not found:
        print("这局没有检测到役满。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
