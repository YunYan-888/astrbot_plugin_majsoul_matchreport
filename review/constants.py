# 雀魂不同区服的入口地址（账号数据互相独立，同一个数字 ID 可能对应不同玩家）
SERVER_URLS = {
    "jp": "https://game.maj-soul.com/",   # 日服
    "cn": "https://game.maj-soul.net/",   # 中文服
}
URL_BASE = SERVER_URLS["jp"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "If-Modified-Since": "0",
    "Referer": URL_BASE,
    "sec-ch-ua": "'Chromium';v='128', 'Google Chrome';v='128'",
    "sec-ch-ua-platform": "Windows",
}
