import random
from typing import Dict

from httpx import AsyncClient

from .constants import HEADERS

HTTPX_CLIENT = AsyncClient(headers=HEADERS)


async def get_res(url_base: str, path: str, bust_cache: bool = False) -> Dict:
    HTTPX_CLIENT.headers["Referer"] = url_base

    if url_base == "https://game.maj-soul.com/":
        url = f"{url_base.rstrip('/')}/1/{path}"
    elif url_base.rstrip("/") == "https://game.maj-soul.net":
        # 中文服入口：/1/ 下才是资源，直接拼 /version.json 会被 301 到日服
        url = f"{url_base.rstrip('/')}/1/{path}"
    else:
        url = f"{url_base}{path}"

    if bust_cache:
        # 只影响请求本身（跳过 CDN 缓存），不落任何本地文件
        url += f"?randv={str(random.random())[2:]}"

    resp = await HTTPX_CLIENT.get(url)
    resp.raise_for_status()

    return resp.json()
