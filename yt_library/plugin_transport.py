"""Bounded host-owned authenticated transport for optional plugins."""

from __future__ import annotations

import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Mapping

from .my_activity import _my_activity_opener, _request_headers, _save_refreshed_cookies
from .request_pacing import open_with_request_pacing


class PluginMyActivitySession:
    """Keep cookie ownership, proxy policy and response limits in the host."""

    def __init__(self, cookie_file: Path, proxy_url: str) -> None:
        self._session = _my_activity_opener(cookie_file, proxy_url)
        self._lock = threading.Lock()

    def request_text(self, path: str, fields: Mapping[str, str] | None = None) -> str:
        parsed = urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc or not path.startswith("/") or "\\" in path:
            raise ValueError("My Activity requests must use a relative service path")
        if fields is None:
            allowed = parsed.path == "/page" or parsed.path.startswith("/product/")
        else:
            allowed = parsed.path == "/_/FootprintsMyactivityUi/data/batchexecute"
        if not allowed:
            raise ValueError("Unsupported My Activity request path")
        body = urllib.parse.urlencode(dict(fields)).encode() if fields is not None else None
        if body is not None and len(body) > 65536:
            raise ValueError("My Activity request exceeds 64 KiB")
        request = urllib.request.Request(
            "https://myactivity.google.com" + path,
            data=body,
            headers={
                **_request_headers(),
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Origin": "https://myactivity.google.com",
                "Referer": "https://myactivity.google.com/",
                "X-Same-Domain": "1",
            },
        )
        with self._lock, open_with_request_pacing(self._session.opener, request, timeout=30) as response:
            if urllib.parse.urlsplit(response.geturl()).hostname != "myactivity.google.com":
                raise RuntimeError("My Activity requires a refreshed Google cookie export")
            data = response.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise RuntimeError("My Activity response exceeds 16 MiB")
        return data.decode("utf-8", "replace")

    def __enter__(self) -> PluginMyActivitySession:
        return self

    def __exit__(self, *_args: object) -> None:
        _save_refreshed_cookies(self._session)
