"""Host-owned, bounded account identity lookup using the configured YouTube cookie."""

from __future__ import annotations

import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from .core import (
    extract_json_assignment, load_cookie_jar, request_bytes, walk,
    youtube_page_is_authenticated,
)
from .network import socks5_proxy_handlers
from .time_utils import utc_now


def account_channel_id(initial_data: dict[str, Any]) -> str:
    """Only the account settings' channel avatar identifies the active channel."""
    identities = set()
    for node in walk(initial_data):
        channel = node.get("channelOptionsRenderer")
        if not isinstance(channel, dict):
            continue
        endpoint = channel.get("avatarEndpoint") or {}
        candidates = [
            endpoint.get("browseEndpoint", {}).get("browseId", ""),
            endpoint.get("urlEndpoint", {}).get("url", ""),
            endpoint.get("commandMetadata", {}).get("webCommandMetadata", {}).get("url", ""),
        ]
        for value in candidates:
            match = re.fullmatch(r"(?:(?:https?://(?:www\.)?youtube\.com)?/channel/)?(UC[A-Za-z0-9_-]{22})/?", str(value))
            if match:
                identities.add(match[1])
    if len(identities) != 1:
        raise RuntimeError("Could not identify the active YouTube channel from the configured cookie")
    return identities.pop()


class YoutubeAccountIdentity:
    """Cache non-secret identity, invalidate on cookie replacement, never guess on failure."""

    def __init__(self, cookie_file: Path, proxy_url: str = "") -> None:
        self.cookie_file = cookie_file
        self.proxy_url = proxy_url
        self._lock = threading.Lock()
        self._key: tuple[int, int, int] | None = None
        self._expires = 0.0
        self._value: dict[str, str] = {}

    def __call__(self) -> dict[str, str]:
        with self._lock:
            try:
                stat = self.cookie_file.stat()
            except OSError:
                raise RuntimeError("Configured YouTube cookie file is unavailable") from None
            key = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
            if key == self._key and time.monotonic() < self._expires:
                if not self._value:
                    raise RuntimeError("YouTube account identity is unavailable; check the configured cookie and retry")
                return dict(self._value)
            self._key = key
            self._value = {}
            self._expires = time.monotonic() + 60
            try:
                jar = load_cookie_jar(self.cookie_file)
                opener = urllib.request.build_opener(
                    urllib.request.HTTPCookieProcessor(jar), *socks5_proxy_handlers(self.proxy_url),
                )
                body, _ = request_bytes(opener, "https://www.youtube.com/account", timeout=30)
                if len(body) > 16 * 1024 * 1024:
                    raise RuntimeError("YouTube account page exceeds the size limit")
                page = body.decode("utf-8", "replace")
                if not youtube_page_is_authenticated(page):
                    raise RuntimeError("YouTube account session is signed out")
                channel_id = account_channel_id(extract_json_assignment(page, "ytInitialData"))
            except Exception:
                raise RuntimeError("YouTube account identity is unavailable; check the configured cookie and retry") from None
            self._value = {"channel_id": channel_id, "checked_at": utc_now()}
            self._expires = time.monotonic() + 15 * 60
            return dict(self._value)
