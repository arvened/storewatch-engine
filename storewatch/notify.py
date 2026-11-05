"""Telegram delivery via the Bot API (stdlib only)."""

from __future__ import annotations

import json
import urllib.request


def send_telegram(token: str, chat_id: str, text: str, timeout: float = 15.0,
                  opener=urllib.request.urlopen) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with opener(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()).get("ok", False)
    except Exception:
        return False
