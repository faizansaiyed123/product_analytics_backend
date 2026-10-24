from __future__ import annotations

import re
from functools import lru_cache

from ua_parser import parse as ua_parse

_TABLET = re.compile(r"iPad|Tablet|PlayBook|Silk|(Android(?!.*Mobile))", re.I)
_MOBILE = re.compile(r"Mobi|iPhone|iPod|Android.*Mobile|Windows Phone", re.I)
_BOT = re.compile(r"bot|crawler|spider|slurp|headless", re.I)


@lru_cache(maxsize=4096)
def parse_user_agent(ua: str) -> dict[str, str | None]:
    if not ua:
        return {"browser": None, "browser_version": None, "os": None, "os_version": None, "device_type": None}
    try:
        res = ua_parse(ua)
        browser = res.user_agent.family if res.user_agent else None
        bver = res.user_agent.major if res.user_agent else None
        os_ = res.os.family if res.os else None
        over = res.os.major if res.os else None
    except Exception:
        browser = bver = os_ = over = None
    if browser == "Other":
        browser = None
    if os_ == "Other":
        os_ = None
    if _BOT.search(ua):
        device = "bot"
    elif _TABLET.search(ua):
        device = "tablet"
    elif _MOBILE.search(ua):
        device = "mobile"
    else:
        device = "desktop"
    return {"browser": browser, "browser_version": bver, "os": os_, "os_version": over, "device_type": device}
