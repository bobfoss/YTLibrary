"""Versioned optional plugin discovery and request dispatch."""

from __future__ import annotations

import copy
from collections.abc import (
    Callable,
    Collection,
    Iterable as IterableCollection,
    Mapping,
)
from contextlib import nullcontext
from functools import partial
import http.cookiejar
import importlib.metadata as importlib_metadata
import json
import re
import sqlite3
import threading
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .database import connect
from .broadcasts import effective_broadcast_status_sql
from .network import socks5_proxy_handlers, ytdlp_proxy_options
from .request_pacing import request_paced_youtube_dl
from .time_utils import utc_now
from .worker_runs import WorkerRunRecorder


PLUGIN_API_VERSION = 2
PLUGIN_HOST_FEATURES = frozenset(
    {
        "library_video_lookup_v1",
        "plugin_json_mutations_v1",
        "youtube_watch_session_v1",
        "youtube_ytdlp_v1",
        "youtube_next_session_v1",
        "my_activity_session_v1",
        "worker_followup_v1",
        "video_discovery_v1",
        "video_facet_result_cards_v1",
        "unified_search_cards_v1",
        "browser_collections_v1",
    }
)
PLUGIN_ENTRY_POINT_GROUP = "yt_library.plugins"
PLUGIN_ID = re.compile(r"^[a-z][a-z0-9_-]*$")
PLUGIN_PROCESS_ID = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")
PLUGIN_BROWSER_ASSET_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
PLUGIN_BROWSER_ASSET_TYPES = {"script", "style"}
PLUGIN_PROCESS_SERVICES = {"local", "youtube", "archivarix"}
PLUGIN_ADMIN_SURFACES = {"none", "basic", "advanced"}
PLUGIN_ADMIN_PLACEMENTS = {"plugin", "videos"}
PLUGIN_ADMIN_METRIC_FORMATS = {"integer", "bytes"}
PLUGIN_ADMIN_METRIC_LIMIT = 12
PLUGIN_TASK_LIMIT = 250_000
PLUGIN_TASK_PAYLOAD_BYTES = 64 * 1024
PLUGIN_JSON_REQUEST_BYTES = 64 * 1024
PLUGIN_YOUTUBE_PAGE_BYTES = 16 * 1024 * 1024
PLUGIN_YOUTUBE_REQUEST_BYTES = 64 * 1024
PLUGIN_YOUTUBE_RESPONSE_BYTES = 16 * 1024 * 1024
PLUGIN_NAVIGATION_GROUP_LIMIT = 10_000
PLUGIN_NAVIGATION_MEMBERSHIP_LIMIT = 250_000
PLUGIN_PLAYLIST_GROUP_KEY_PREFIX = "plugin:"
PLUGIN_CHANNEL_GROUP_KEY_PREFIX = "plugin-channel:"
YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
PLUGIN_YTDLP_HOST_OPTIONS = frozenset(
    {
        "cookiefile",
        "cookiesfrombrowser",
        "extractor_args",
        "extractor_retries",
        "fragment_retries",
        "geo_verification_proxy",
        "http_headers",
        "logger",
        "noprogress",
        "postprocessor_hooks",
        "progress_hooks",
        "proxy",
        "quiet",
        "retries",
        "socket_timeout",
        "source_address",
    }
)


class PluginYoutubeSession:
    """Authenticated, video-bound YouTube transport without exposed cookies."""

    def __init__(
        self,
        *,
        video_id: str,
        initial_data: dict[str, Any],
        opener: urllib.request.OpenerDirector,
        cookie_jar: http.cookiejar.CookieJar,
        api_key: str,
        client_version: str,
        client_context: dict[str, Any],
        referer: str,
    ) -> None:
        self.video_id = video_id
        self._initial_data = copy.deepcopy(initial_data)
        self._opener = opener
        self._cookie_jar = cookie_jar
        self._api_key = api_key
        self._client_version = client_version
        self._client_context = copy.deepcopy(client_context)
        self._referer = referer
        self._request_lock = threading.Lock()

    @property
    def initial_data(self) -> dict[str, Any]:
        return copy.deepcopy(self._initial_data)

    def request_json(
        self,
        api_path: str,
        payload: Mapping[str, Any],
        *,
        click_tracking_params: str = "",
    ) -> dict[str, Any]:
        """Submit one bounded request with host-owned authentication and no retries."""

        if api_path not in {"get_panel", "next"}:
            raise ValueError(f"Unsupported plugin YouTube API path: {api_path}")
        if not isinstance(payload, Mapping):
            raise TypeError("Plugin YouTube request payload must be an object")
        if "context" in payload:
            raise ValueError("Plugin YouTube request context is host-owned")
        tracking = str(click_tracking_params or "")
        if len(tracking.encode("utf-8")) > 16 * 1024:
            raise ValueError("Plugin YouTube click tracking exceeds 16 KiB")
        context = copy.deepcopy(self._client_context)
        if tracking:
            context["clickTracking"] = {"clickTrackingParams": tracking}
        request_payload = {"context": context, **dict(payload)}
        encoded = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > PLUGIN_YOUTUBE_REQUEST_BYTES:
            raise ValueError("Plugin YouTube request exceeds 64 KiB")

        from .core import request_youtubei_json

        with self._request_lock:
            response = request_youtubei_json(
                self._opener,
                self._cookie_jar,
                self._api_key,
                request_payload,
                self._referer,
                self._client_version,
                api_path=api_path,
            )
        response_size = len(
            json.dumps(response, ensure_ascii=False).encode("utf-8")
        )
        if response_size > PLUGIN_YOUTUBE_RESPONSE_BYTES:
            raise RuntimeError("Plugin YouTube response exceeds 16 MiB")
        if response.get("responseContext", {}).get("mainAppWebResponseContext", {}).get("loggedOut") is True:
            raise RuntimeError("YouTube session is signed out; refresh the cookie export")
        return response


def _open_plugin_youtube_session(
    cookie_file: Path,
    proxy_url: str,
    video_id: str,
    comment_id: str = "",
) -> PluginYoutubeSession:
    normalized_video_id = str(video_id or "").strip()
    if not YOUTUBE_VIDEO_ID.fullmatch(normalized_video_id):
        raise ValueError(
            f"Expected an 11-character YouTube video ID: {normalized_video_id}"
        )
    if not cookie_file.exists():
        raise RuntimeError("Configured YouTube cookie file is unavailable")

    from .core import (
        extract_json_assignment,
        extract_ytcfg,
        load_cookie_jar,
        request_bytes,
        youtube_authentication_error,
        youtube_page_is_authenticated,
        youtube_web_context,
    )

    jar = load_cookie_jar(cookie_file)
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        *socks5_proxy_handlers(proxy_url),
    )
    referer = "https://www.youtube.com/watch?v=" + normalized_video_id
    if comment_id:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,250}", comment_id):
            raise ValueError("Invalid highlighted item ID")
        referer += "&lc=" + urllib.parse.quote(comment_id)
    page_body, _content_type = request_bytes(opener, referer, timeout=30)
    if len(page_body) > PLUGIN_YOUTUBE_PAGE_BYTES:
        raise RuntimeError("YouTube watch page exceeds 16 MiB")
    page = page_body.decode("utf-8", "replace")
    if not youtube_page_is_authenticated(page):
        raise youtube_authentication_error(page, "plugin video session")
    config = extract_ytcfg(page)
    api_key = str(config.get("INNERTUBE_API_KEY") or "")
    client_version = str(config.get("INNERTUBE_CLIENT_VERSION") or "")
    initial_data = extract_json_assignment(page, "ytInitialData")
    if not api_key or not client_version or not initial_data:
        raise RuntimeError("YouTube watch page is missing required session data")
    initial_data_size = len(
        json.dumps(initial_data, ensure_ascii=False).encode("utf-8")
    )
    if initial_data_size > PLUGIN_YOUTUBE_RESPONSE_BYTES:
        raise RuntimeError("YouTube initial data exceeds 16 MiB")
    return PluginYoutubeSession(
        video_id=normalized_video_id,
        initial_data=initial_data,
        opener=opener,
        cookie_jar=jar,
        api_key=api_key,
        client_version=client_version,
        client_context=youtube_web_context(config),
        referer=referer,
    )


@dataclass(frozen=True)
class PluginContext:
    """Narrow host services made available to an activated plugin."""

    root: Path
    config_path: Path
    plugin_id: str
    plugin_config: dict[str, Any]
    host_features: frozenset[str]
    _library_video_lookup: (
        Callable[[tuple[str, ...]], Iterable[dict[str, Any]]] | None
    ) = None
    _youtube_session_factory: Callable[[str], PluginYoutubeSession] | None = None
    _my_activity_session_factory: Callable[[], Any] | None = None

    def resolve_path(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.config_path.resolve().parent / path

    def library_videos(
        self,
        video_ids: Iterable[str],
    ) -> tuple[dict[str, Any], ...]:
        """Return bounded canonical video metadata for explicit IDs."""

        if isinstance(video_ids, (str, bytes)):
            raise TypeError("Plugin library video lookup requires an iterable of IDs")
        normalized = tuple(
            dict.fromkeys(str(value or "").strip() for value in video_ids)
        )
        normalized = tuple(value for value in normalized if value)
        if len(normalized) > PLUGIN_TASK_LIMIT:
            raise ValueError(
                f"Plugin library video lookup accepts at most {PLUGIN_TASK_LIMIT} IDs"
            )
        invalid = next(
            (value for value in normalized if not YOUTUBE_VIDEO_ID.fullmatch(value)),
            None,
        )
        if invalid is not None:
            raise ValueError(f"Invalid YouTube video ID: {invalid}")
        if not normalized or not callable(self._library_video_lookup):
            return ()
        return tuple(self._library_video_lookup(normalized))

    def youtube_video_session(self, video_id: str, *, comment_id: str = "") -> PluginYoutubeSession:
        """Open a bounded authenticated session for one YouTube watch page."""

        if not callable(self._youtube_session_factory):
            raise RuntimeError("YouTube video sessions are unavailable")
        if comment_id:
            return self._youtube_session_factory(video_id, comment_id=comment_id)
        return self._youtube_session_factory(video_id)

    def my_activity_session(self) -> Any:
        if not callable(self._my_activity_session_factory):
            raise RuntimeError("My Activity sessions are unavailable")
        return self._my_activity_session_factory()


def _library_videos_by_id(
    db_path: Path,
    video_ids: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA query_only = ON")
        for start in range(0, len(video_ids), 500):
            batch = video_ids[start : start + 500]
            placeholders = ",".join("?" for _value in batch)
            rows.extend(
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT video_id, title, COALESCE(channel_id, '') AS channel_id,
                           upload_date, availability, is_playable, video_type,
                           {effective_broadcast_status_sql('')} AS broadcast_status, broadcast_started_at,
                           broadcast_ended_at, broadcast_status_checked_at
                    FROM videos
                    WHERE video_id IN ({placeholders})
                    ORDER BY video_id
                    """,
                    batch,
                )
            )
    finally:
        conn.close()
    return tuple(sorted(rows, key=lambda row: str(row["video_id"])))


class PluginPlanningContext:
    """Bounded read-only library information available while planning tasks."""

    def __init__(self, conn: sqlite3.Connection, plugin_id: str) -> None:
        self._conn = conn
        self.plugin_id = plugin_id

    def library_videos(self) -> Iterable[dict[str, Any]]:
        rows = self._conn.execute(
            f"""
            SELECT video_id, title, COALESCE(channel_id, '') AS channel_id,
                   upload_date, availability, is_playable,
                   video_type, {effective_broadcast_status_sql('')} AS broadcast_status, broadcast_started_at,
                   broadcast_ended_at, broadcast_status_checked_at
            FROM videos
            WHERE video_id <> ''
            ORDER BY video_id
            """
        )
        for row in rows:
            yield dict(row)

    def library_clips(self) -> Iterable[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT c.clip_id,
                   c.title,
                   c.source_video_id,
                   COALESCE(v.title, '') AS source_title,
                   c.start_ms,
                   c.end_ms,
                   c.availability
            FROM clips c
            LEFT JOIN videos v ON v.video_id = c.source_video_id
            WHERE c.clip_id <> ''
            ORDER BY c.clip_id
            """
        )
        for row in rows:
            yield dict(row)

    def latest_worker_outcomes(self, worker_id: str) -> dict[str, dict[str, Any]]:
        normalized_worker_id = str(worker_id or "").strip()
        if not PLUGIN_PROCESS_ID.fullmatch(normalized_worker_id):
            raise ValueError(f"Invalid plugin worker ID: {worker_id}")
        return {
            str(row["subject_id"]): dict(row)
            for row in self._conn.execute(
                """
                SELECT subject_id, outcome, status, finished_at, message
                FROM (
                  SELECT subject_id, outcome, status, finished_at, message,
                         ROW_NUMBER() OVER (
                           PARTITION BY subject_id
                           ORDER BY started_at DESC, run_id DESC
                         ) AS rank
                  FROM plugin_worker_runs
                  WHERE plugin_id = ? AND worker_id = ? AND subject_id <> ''
                )
                WHERE rank = 1
                """,
                (self.plugin_id, normalized_worker_id),
            )
        }


class PluginWorkerRuntime:
    """Host-owned logging and cancellation surface for one plugin task."""

    def __init__(
        self,
        db_path: Path,
        *,
        run_id: str,
        queue_id: int,
        plugin_id: str,
        worker_id: str,
        subject_id: str,
        stop_event: threading.Event,
        service: str = "local",
        cookie_file: Path | None = None,
        proxy_url: str = "",
    ) -> None:
        self.run_id = run_id
        self.queue_id = queue_id
        self.plugin_id = plugin_id
        self.worker_id = worker_id
        self.subject_id = subject_id
        self._db_path = db_path
        self._stop_event = stop_event
        self._service = service
        self._cookie_file = Path(cookie_file) if cookie_file is not None else None
        self._proxy_url = str(proxy_url or "")
        self._manager: PluginManager | None = None

    def enqueue_process(self, worker_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Plan follow-up work belonging to this same plugin."""
        if self._manager is None:
            raise RuntimeError("Plugin follow-up planning is unavailable")
        conn = connect(self._db_path)
        try:
            with conn:
                return self._manager.enqueue_process(
                    conn, self.plugin_id, worker_id, params, manual=False,
                )
        finally:
            conn.close()

    def discover_videos(self, videos: Iterable[Mapping[str, Any]]) -> int:
        """Add explicit discovered identities and queue metadata only for new videos."""
        from .core import enqueue_metadata_item, upsert_video

        rows = list(videos)
        if len(rows) > 1000:
            raise ValueError("At most 1000 videos may be discovered per call")
        for row in rows:
            if not isinstance(row, Mapping) or not YOUTUBE_VIDEO_ID.fullmatch(str(row.get("video_id") or "")):
                raise ValueError("Discovered videos require valid YouTube IDs")
        conn = connect(self._db_path)
        added = 0
        try:
            with conn:
                for row in rows:
                    video_id = str(row["video_id"])
                    if conn.execute("SELECT 1 FROM videos WHERE video_id=?", (video_id,)).fetchone():
                        continue
                    title = str(row.get("title") or "")[:2000]
                    upsert_video(conn, video_id, title=title, source="provided")
                    enqueue_metadata_item(
                        conn, video_id=video_id, current_title=title,
                        metadata_source="provided", source_key=f"plugin:{self.plugin_id}",
                        manual=False,
                    )
                    added += 1
        finally:
            conn.close()
        return added

    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def log(self, level: str, message: str, *, subject_id: str = "") -> None:
        normalized_level = str(level or "info").strip().lower()[:40] or "info"
        normalized_message = str(message or "").strip()[:10_000]
        normalized_subject = str(subject_id or self.subject_id).strip()[:500]
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO plugin_worker_log(
                      run_id, plugin_id, worker_id, created_at, level,
                      subject_id, message
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.run_id,
                        self.plugin_id,
                        self.worker_id,
                        utc_now(),
                        normalized_level,
                        normalized_subject,
                        normalized_message,
                    ),
                )
        finally:
            conn.close()

    def run_youtube_ytdlp(
        self,
        video_id: str,
        options: Mapping[str, Any],
        *,
        download: bool,
    ) -> dict[str, Any]:
        """Run a bounded yt-dlp request with host-owned YouTube policy."""
        if self._service != "youtube":
            raise RuntimeError(
                "youtube_ytdlp_v1 is available only to YouTube plugin workers"
            )
        normalized_video_id = str(video_id or "").strip()
        if not YOUTUBE_VIDEO_ID.fullmatch(normalized_video_id):
            raise ValueError(
                f"Expected an 11-character YouTube video ID: {normalized_video_id}"
            )
        if not isinstance(options, Mapping):
            raise TypeError("yt-dlp options must be a mapping")
        protected = sorted(PLUGIN_YTDLP_HOST_OPTIONS.intersection(options))
        if protected:
            raise ValueError(
                "Plugin yt-dlp options may not override host policy: "
                + ", ".join(protected)
            )
        if self.stop_requested():
            raise PluginWorkerStopped("Stopped before yt-dlp retrieval started")
        try:
            import yt_dlp  # type: ignore
        except ImportError as exc:
            raise RuntimeError("yt-dlp is not installed") from exc

        from .core import temporary_ytdlp_cookie_file

        runtime = self

        class PluginYtdlpLogger:
            def debug(self, message: str) -> None:
                text = str(message or "")
                if text.startswith("[download]") and "Destination:" in text:
                    runtime.log("debug", text)

            def info(self, message: str) -> None:
                self.debug(message)

            def warning(self, message: str) -> None:
                runtime.log("warn", str(message or ""))

            def error(self, message: str) -> None:
                runtime.log("error", str(message or ""))

        def stop_hook(_status: dict[str, Any]) -> None:
            if runtime.stop_requested():
                raise PluginWorkerStopped("Stopped during yt-dlp retrieval")

        try:
            cookie_context = (
                temporary_ytdlp_cookie_file(self._cookie_file)
                if self._cookie_file is not None
                else nullcontext(None)
            )
            with cookie_context as working_cookie_file:
                host_options = dict(options)
                host_options.update(
                    {
                        "cookiefile": (
                            str(working_cookie_file) if working_cookie_file else None
                        ),
                        "extractor_retries": 2,
                        "fragment_retries": 3,
                        "logger": PluginYtdlpLogger(),
                        "noprogress": True,
                        "progress_hooks": [stop_hook],
                        "quiet": True,
                        "retries": 2,
                        "socket_timeout": 30,
                        **ytdlp_proxy_options(self._proxy_url),
                    }
                )
                url = f"https://www.youtube.com/watch?v={normalized_video_id}"
                with request_paced_youtube_dl(yt_dlp, host_options) as ydl:
                    info = ydl.extract_info(url, download=download)
            if self.stop_requested():
                raise PluginWorkerStopped("Stopped after yt-dlp retrieval")
        except Exception as exc:
            if self.stop_requested() and not isinstance(exc, PluginWorkerStopped):
                raise PluginWorkerStopped("Stopped during yt-dlp retrieval") from exc
            raise
        if not isinstance(info, Mapping):
            raise RuntimeError("yt-dlp returned no video information")
        return dict(info)


class PluginWorkerStopped(RuntimeError):
    """Host-owned cooperative cancellation for a plugin worker."""


@dataclass
class _PluginRecord:
    plugin_id: str
    configured: dict[str, Any]
    instance: Any | None = None
    state: str = "configured"
    message: str = ""


def _installed_entry_points() -> list[Any]:
    discovered = importlib_metadata.entry_points()
    if hasattr(discovered, "select"):
        return list(discovered.select(group=PLUGIN_ENTRY_POINT_GROUP))
    return list(discovered.get(PLUGIN_ENTRY_POINT_GROUP, ()))


def _browser_assets(instance: Any) -> list[dict[str, str]]:
    assets: list[dict[str, str]] = []
    for value in getattr(instance, "browser_assets", ()):
        if not isinstance(value, dict):
            raise TypeError("Plugin browser assets must be objects")
        path = str(value.get("path") or "")
        asset_type = str(value.get("type") or "")
        if (
            not PLUGIN_BROWSER_ASSET_PATH.fullmatch(path)
            or ".." in path.split("/")
            or asset_type not in PLUGIN_BROWSER_ASSET_TYPES
        ):
            raise ValueError(f"Invalid plugin browser asset: {path or '<missing>'}")
        assets.append({"path": path, "type": asset_type})
    if assets and not callable(getattr(instance, "handle_browser_asset", None)):
        raise TypeError("Plugin browser assets require handle_browser_asset")
    return assets


def _browser_collection(plugin_id: str, instance: Any) -> dict[str, str] | None:
    value = getattr(instance, "browser_collection", None)
    if value is None:
        return None
    if not isinstance(value, dict) or not str(value.get("label") or "").strip():
        raise ValueError("Plugin browser collection requires a label")
    if plugin_id in {"search", "videos", "clips", "playlists", "channels", "history", "admin", "api", "plugins"}:
        raise ValueError("Plugin browser collection conflicts with a host route")
    return {"path": f"/{plugin_id}", "label": str(value["label"]).strip()}


def _asset_error(message: str) -> bytes:
    return json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")


def _normalized_video_ids(value: Any, label: str) -> frozenset[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, IterableCollection):
        raise TypeError(f"Plugin {label} must be an iterable of video IDs")
    return frozenset(
        video_id
        for item in value
        if (video_id := str(item).strip())
    )


def _normalized_video_projections(
    value: Any,
    requested_video_ids: Collection[str],
) -> dict[str, dict[str, str]]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(
        value, IterableCollection
    ):
        raise TypeError("Plugin video projections must be an iterable of objects")
    requested = frozenset(requested_video_ids)
    projections: dict[str, dict[str, str]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("Plugin video projections must be objects")
        video_id = str(item.get("video_id") or "").strip()
        if not video_id:
            raise ValueError("Plugin video projections require video_id")
        if video_id not in requested:
            raise ValueError("Plugin returned an unrequested video projection")
        if video_id in projections:
            raise ValueError("Plugin returned duplicate video projections")
        projections[video_id] = {
            "video_id": video_id,
            "title": str(item.get("title") or "").strip(),
        }
    return projections


def _plugin_navigation_group_key(
    plugin_id: str,
    group_key: str,
    prefix: str,
) -> str:
    return f"{prefix}{plugin_id}:{group_key}"


def _normalized_navigation_group_projection(
    plugin_id: str,
    value: Any,
    *,
    domain: str,
    identifier_field: str,
    key_prefix: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"Plugin {domain}-group projection must be an object")
    raw_groups = value.get("groups")
    if isinstance(raw_groups, (str, bytes, Mapping)) or not isinstance(
        raw_groups, IterableCollection
    ):
        raise TypeError(f"Plugin {domain} groups must be an iterable of objects")
    raw_memberships = value.get("memberships")
    if isinstance(raw_memberships, (str, bytes, Mapping)) or not isinstance(
        raw_memberships, IterableCollection
    ):
        raise TypeError(
            f"Plugin {domain} memberships must be an iterable of objects"
        )

    groups: list[dict[str, Any]] = []
    raw_group_keys: set[str] = set()
    raw_parents: dict[str, str | None] = {}
    unmatched_group_key: str | None = None
    for item in raw_groups:
        if len(groups) >= PLUGIN_NAVIGATION_GROUP_LIMIT:
            raise ValueError(
                "Plugin returned more than "
                f"{PLUGIN_NAVIGATION_GROUP_LIMIT} {domain} groups"
            )
        if not isinstance(item, Mapping):
            raise TypeError(f"Plugin {domain} groups must be objects")
        group_key = str(item.get("group_key") or "").strip()
        name = str(item.get("name") or "").strip()
        parent_value = item.get("parent_key")
        parent_key = str(parent_value or "").strip() or None
        if not group_key or len(group_key) > 500 or any(ord(char) < 32 for char in group_key):
            raise ValueError(f"Plugin {domain} groups require a valid group_key")
        if group_key in raw_group_keys:
            raise ValueError(f"Plugin returned duplicate {domain} group keys")
        if not name or len(name) > 2_000:
            raise ValueError(f"Plugin {domain} groups require a valid name")
        try:
            position = int(item.get("position") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Plugin {domain} group positions must be integers"
            ) from exc
        if position < 0:
            raise ValueError(
                f"Plugin {domain} group positions must be nonnegative"
            )
        include_unmatched = item.get("include_unmatched", False)
        if not isinstance(include_unmatched, bool):
            raise TypeError(
                f"Plugin {domain} group include_unmatched must be a boolean"
            )
        if include_unmatched:
            if unmatched_group_key is not None:
                raise ValueError(
                    f"Plugin returned multiple unmatched {domain} groups"
                )
            unmatched_group_key = group_key
        icon = str(item.get("icon") or "")[:10_000]
        raw_group_keys.add(group_key)
        raw_parents[group_key] = parent_key
        group = {
            "group_key": _plugin_navigation_group_key(
                plugin_id, group_key, key_prefix
            ),
            "name": name,
            "parent_key": (
                _plugin_navigation_group_key(
                    plugin_id, parent_key, key_prefix
                )
                if parent_key
                else None
            ),
            "position": position,
            "icon": icon,
            "source_plugin_id": plugin_id,
        }
        if include_unmatched:
            group["include_unmatched"] = True
        groups.append(group)
    for group_key, parent_key in raw_parents.items():
        if parent_key is not None and parent_key not in raw_group_keys:
            raise ValueError(
                f"Plugin {domain} group {group_key!r} references a missing parent"
            )
        seen: set[str] = set()
        current: str | None = group_key
        while current is not None:
            if current in seen:
                raise ValueError(
                    f"Plugin {domain} group hierarchy contains a cycle"
                )
            seen.add(current)
            current = raw_parents.get(current)

    memberships: list[dict[str, Any]] = []
    seen_memberships: set[tuple[str, str]] = set()
    for item in raw_memberships:
        if len(memberships) >= PLUGIN_NAVIGATION_MEMBERSHIP_LIMIT:
            raise ValueError(
                "Plugin returned more than "
                f"{PLUGIN_NAVIGATION_MEMBERSHIP_LIMIT} {domain} memberships"
            )
        if not isinstance(item, Mapping):
            raise TypeError(f"Plugin {domain} memberships must be objects")
        group_key = str(item.get("group_key") or "").strip()
        identifier = str(item.get(identifier_field) or "").strip()
        if group_key not in raw_group_keys:
            raise ValueError(
                f"Plugin {domain} membership references an unknown group"
            )
        if not identifier or len(identifier) > 500:
            raise ValueError(
                f"Plugin {domain} memberships require {identifier_field}"
            )
        membership_key = (group_key, identifier)
        if membership_key in seen_memberships:
            raise ValueError(f"Plugin returned duplicate {domain} memberships")
        try:
            position = int(item.get("position") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Plugin {domain} membership positions must be integers"
            ) from exc
        if position < 0:
            raise ValueError(
                f"Plugin {domain} membership positions must be nonnegative"
            )
        seen_memberships.add(membership_key)
        memberships.append(
            {
                "group_key": _plugin_navigation_group_key(
                    plugin_id, group_key, key_prefix
                ),
                identifier_field: identifier,
                "position": position,
                "source_plugin_id": plugin_id,
            }
        )
    if unmatched_group_key is not None and any(
        membership[0] == unmatched_group_key for membership in seen_memberships
    ):
        raise ValueError(
            f"Plugin unmatched {domain} group cannot declare memberships"
        )
    return {
        "plugin_id": plugin_id,
        "revision": str(value.get("revision") or "")[:500],
        "groups": groups,
        "memberships": memberships,
    }


def _navigation_memberships_for_known_identifiers(
    projection: Mapping[str, Any],
    known: frozenset[str] | None,
    *,
    identifier_field: str,
) -> list[dict[str, Any]]:
    memberships = [
        dict(membership)
        for membership in projection["memberships"]
        if known is None or membership[identifier_field] in known
    ]
    if known is None:
        return memberships
    unmatched_groups = [
        group for group in projection["groups"] if group.get("include_unmatched") is True
    ]
    if not unmatched_groups:
        return memberships
    assigned = {
        str(membership[identifier_field])
        for membership in projection["memberships"]
        if membership[identifier_field] in known
    }
    unmatched_ids = sorted(known - assigned)
    if len(memberships) + len(unmatched_ids) > PLUGIN_NAVIGATION_MEMBERSHIP_LIMIT:
        raise ValueError(
            "Plugin projection expands beyond the navigation membership limit"
        )
    unmatched_group = unmatched_groups[0]
    memberships.extend(
        {
            "group_key": unmatched_group["group_key"],
            identifier_field: identifier,
            "position": position,
            "source_plugin_id": unmatched_group["source_plugin_id"],
        }
        for position, identifier in enumerate(unmatched_ids)
    )
    return memberships


def _normalized_playlist_group_projection(
    plugin_id: str,
    value: Any,
) -> dict[str, Any]:
    return _normalized_navigation_group_projection(
        plugin_id,
        value,
        domain="playlist",
        identifier_field="playlist_id",
        key_prefix=PLUGIN_PLAYLIST_GROUP_KEY_PREFIX,
    )


def _normalized_channel_group_projection(
    plugin_id: str,
    value: Any,
) -> dict[str, Any]:
    return _normalized_navigation_group_projection(
        plugin_id,
        value,
        domain="channel",
        identifier_field="channel_id",
        key_prefix=PLUGIN_CHANNEL_GROUP_KEY_PREFIX,
    )


def _short_text(value: Any, *, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def _plugin_admin_metrics(raw_metrics: Any) -> list[dict[str, Any]]:
    if raw_metrics is None:
        return []
    if isinstance(raw_metrics, (str, bytes, Mapping)) or not isinstance(
        raw_metrics, IterableCollection
    ):
        raise TypeError("Plugin admin metrics must be an iterable of objects")
    raw_metrics = list(raw_metrics)
    if len(raw_metrics) > PLUGIN_ADMIN_METRIC_LIMIT:
        raise ValueError(
            f"Plugins may expose at most {PLUGIN_ADMIN_METRIC_LIMIT} admin metrics"
        )
    metrics: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_metric in raw_metrics:
        if not isinstance(raw_metric, Mapping):
            raise TypeError("Plugin admin metrics must be objects")
        metric_id = _short_text(raw_metric.get("id"), maximum=80)
        if not PLUGIN_PROCESS_ID.fullmatch(metric_id):
            raise ValueError(f"Invalid plugin admin metric ID: {metric_id or '<missing>'}")
        if metric_id in seen:
            raise ValueError(f"Duplicate plugin admin metric ID: {metric_id}")
        seen.add(metric_id)
        label = _short_text(raw_metric.get("label"), maximum=120)
        if not label:
            raise ValueError(f"Plugin admin metric {metric_id} requires a label")
        metric_format = _short_text(
            raw_metric.get("format") or "integer", maximum=20
        ).lower()
        if metric_format not in PLUGIN_ADMIN_METRIC_FORMATS:
            raise ValueError(
                f"Invalid plugin admin metric format for {metric_id}: {metric_format}"
            )
        value = raw_metric.get("value")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"Plugin admin metric {metric_id} value must be a nonnegative integer"
            )
        metrics.append(
            {
                "id": metric_id,
                "label": label,
                "value": value,
                "format": metric_format,
                "description": _short_text(
                    raw_metric.get("description"), maximum=250
                ),
            }
        )
    return metrics


def _plugin_admin_inputs(
    worker_id: str,
    action_id: str,
    raw_inputs: Any,
) -> list[dict[str, Any]]:
    if raw_inputs is None:
        return []
    if isinstance(raw_inputs, (str, bytes, Mapping)) or not isinstance(
        raw_inputs, IterableCollection
    ):
        raise TypeError(
            f"Plugin worker process {worker_id} admin action {action_id} "
            "inputs must be iterable"
        )
    inputs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_input in raw_inputs:
        if not isinstance(raw_input, Mapping):
            raise TypeError("Plugin admin action inputs must be objects")
        name = _short_text(raw_input.get("name"), maximum=80)
        if not PLUGIN_PROCESS_ID.fullmatch(name):
            raise ValueError(f"Invalid plugin admin input name: {name or '<missing>'}")
        if name in seen:
            raise ValueError(f"Duplicate plugin admin input name: {name}")
        seen.add(name)
        label = _short_text(raw_input.get("label") or name, maximum=120)
        try:
            max_length = int(
                raw_input.get("max_length", raw_input.get("maxLength", 500))
            )
        except (TypeError, ValueError):
            max_length = 500
        inputs.append(
            {
                "name": name,
                "label": label,
                "placeholder": _short_text(
                    raw_input.get("placeholder"), maximum=250
                ),
                "required": raw_input.get("required") is True,
                "maxLength": max(1, min(2000, max_length)),
            }
        )
    return inputs


def _plugin_admin_actions(
    worker_id: str,
    raw_process: Mapping[str, Any],
    *,
    process_name: str,
    process_description: str,
    legacy_surface: str,
    legacy_button_label: str,
    legacy_confirm: str,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    if legacy_surface != "none":
        actions.append(
            {
                "id": "default",
                "placement": "plugin",
                "surface": legacy_surface,
                "buttonLabel": legacy_button_label,
                "description": process_description,
                "confirm": legacy_confirm,
                "inputs": [],
            }
        )
        seen.add("default")
    raw_actions = raw_process.get("admin_actions", raw_process.get("adminActions"))
    if raw_actions is None:
        return actions
    if isinstance(raw_actions, (str, bytes, Mapping)) or not isinstance(
        raw_actions, IterableCollection
    ):
        raise TypeError(f"Plugin worker process {worker_id} admin actions must be iterable")
    for raw_action in raw_actions:
        if not isinstance(raw_action, Mapping):
            raise TypeError("Plugin worker process admin actions must be objects")
        action_id = _short_text(raw_action.get("id"), maximum=80)
        if not PLUGIN_PROCESS_ID.fullmatch(action_id):
            raise ValueError(f"Invalid plugin admin action ID: {action_id or '<missing>'}")
        if action_id in seen:
            raise ValueError(f"Duplicate plugin admin action ID: {action_id}")
        seen.add(action_id)
        placement = _short_text(
            raw_action.get("placement") or "plugin", maximum=20
        ).lower()
        if placement not in PLUGIN_ADMIN_PLACEMENTS:
            raise ValueError(f"Invalid plugin admin placement: {placement}")
        surface = _short_text(
            raw_action.get("surface") or "advanced", maximum=20
        ).lower()
        if surface not in PLUGIN_ADMIN_SURFACES - {"none"}:
            raise ValueError(f"Invalid plugin admin action surface: {surface}")
        actions.append(
            {
                "id": action_id,
                "placement": placement,
                "surface": surface,
                "buttonLabel": _short_text(
                    raw_action.get("button_label")
                    or raw_action.get("buttonLabel")
                    or process_name,
                    maximum=120,
                ),
                "description": _short_text(
                    raw_action.get("description"), maximum=500
                ),
                "confirm": _short_text(raw_action.get("confirm"), maximum=500),
                "inputs": _plugin_admin_inputs(
                    worker_id,
                    action_id,
                    raw_action.get("inputs"),
                ),
            }
        )
    return actions


def _worker_processes(instance: Any) -> list[dict[str, Any]]:
    raw_processes = getattr(instance, "worker_processes", ())
    if callable(raw_processes):
        raw_processes = raw_processes()
    if isinstance(raw_processes, (str, bytes, Mapping)) or not isinstance(
        raw_processes, IterableCollection
    ):
        raise TypeError("Plugin worker_processes must be an iterable of objects")
    processes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_processes:
        if not isinstance(raw, Mapping):
            raise TypeError("Plugin worker process definitions must be objects")
        worker_id = _short_text(raw.get("id"), maximum=80)
        if not PLUGIN_PROCESS_ID.fullmatch(worker_id):
            raise ValueError(f"Invalid plugin worker process ID: {worker_id or '<missing>'}")
        if worker_id in seen:
            raise ValueError(f"Duplicate plugin worker process ID: {worker_id}")
        seen.add(worker_id)
        name = _short_text(raw.get("name"), maximum=120)
        if not name:
            raise ValueError(f"Plugin worker process {worker_id} requires a name")
        service = _short_text(raw.get("service") or "local", maximum=40).lower()
        if service not in PLUGIN_PROCESS_SERVICES:
            raise ValueError(f"Invalid service for plugin worker process {worker_id}: {service}")
        surface = _short_text(
            raw.get("admin_surface") or raw.get("adminSurface") or "none",
            maximum=20,
        ).lower()
        if surface not in PLUGIN_ADMIN_SURFACES:
            raise ValueError(
                f"Invalid admin surface for plugin worker process {worker_id}: {surface}"
            )
        try:
            max_in_flight = int(raw.get("max_in_flight", raw.get("maxInFlight", 1)))
        except (TypeError, ValueError):
            max_in_flight = 1
        hooks_value = raw.get("hooks") or ()
        if isinstance(hooks_value, (str, bytes)):
            hooks_value = (hooks_value,)
        if not isinstance(hooks_value, IterableCollection):
            raise TypeError(f"Plugin worker process {worker_id} hooks must be iterable")
        hooks: list[str] = []
        for value in hooks_value:
            hook = _short_text(value, maximum=80)
            if not PLUGIN_PROCESS_ID.fullmatch(hook):
                raise ValueError(f"Invalid plugin lifecycle hook: {hook or '<missing>'}")
            if hook not in hooks:
                hooks.append(hook)
        description = _short_text(raw.get("description"), maximum=500)
        button_label = _short_text(
            raw.get("button_label") or raw.get("buttonLabel") or name,
            maximum=120,
        )
        confirm = _short_text(raw.get("confirm"), maximum=500)
        process = {
            "id": worker_id,
            "name": name,
            "description": description,
            "service": service,
            "maxInFlight": max(1, min(100, max_in_flight)),
            "adminSurface": surface,
            "buttonLabel": button_label,
            "confirm": confirm,
            "hooks": hooks,
        }
        process["adminActions"] = _plugin_admin_actions(
            worker_id,
            raw,
            process_name=name,
            process_description=description,
            legacy_surface=surface,
            legacy_button_label=button_label,
            legacy_confirm=confirm,
        )
        processes.append(process)
    if processes:
        if not callable(getattr(instance, "plan_worker", None)):
            raise TypeError("Plugin worker processes require plan_worker")
        if not callable(getattr(instance, "run_worker", None)):
            raise TypeError("Plugin worker processes require run_worker")
    return processes


def _required_host_features(instance: Any) -> frozenset[str]:
    values = getattr(instance, "required_host_features", ())
    if isinstance(values, (str, bytes)) or not isinstance(
        values, IterableCollection
    ):
        raise TypeError("Plugin required_host_features must be an iterable")
    features: set[str] = set()
    for value in values:
        feature = str(value or "").strip()
        if not PLUGIN_PROCESS_ID.fullmatch(feature):
            raise ValueError(f"Invalid required host feature: {feature or '<missing>'}")
        features.add(feature)
    return frozenset(features)


def _normalize_plugin_task(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TypeError("Plugin worker tasks must be objects")
    task_id = _short_text(raw.get("task_id") or raw.get("taskId"), maximum=500)
    if not task_id:
        raise ValueError("Plugin worker tasks require task_id")
    subject_id = _short_text(raw.get("subject_id") or raw.get("subjectId") or task_id, maximum=500)
    video_id = _short_text(raw.get("video_id") or raw.get("videoId"), maximum=500)
    title = _short_text(raw.get("title"), maximum=2_000)
    payload = raw.get("payload") or {}
    if not isinstance(payload, Mapping):
        raise TypeError("Plugin worker task payload must be an object")
    payload_json = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
    if len(payload_json.encode("utf-8")) > PLUGIN_TASK_PAYLOAD_BYTES:
        raise ValueError("Plugin worker task payload is too large")
    try:
        priority = int(raw.get("priority", 0))
    except (TypeError, ValueError):
        priority = 0
    return {
        "task_id": task_id,
        "subject_id": subject_id,
        "video_id": video_id,
        "title": title,
        "payload_json": payload_json,
        "priority": max(-1_000, min(1_000, priority)),
    }


def _normalize_worker_result(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise TypeError("Plugin worker result must be an object")
    outcome = _short_text(value.get("outcome") or "complete", maximum=80).lower()
    if not PLUGIN_PROCESS_ID.fullmatch(outcome):
        raise ValueError(f"Invalid plugin worker outcome: {outcome}")

    def count(name: str, default: int = 0) -> int:
        try:
            return max(0, int(value.get(name, default)))
        except (TypeError, ValueError):
            return max(0, default)

    return {
        "outcome": outcome,
        "processed": count("processed", 1),
        "found": count("found"),
        "failed": count("failed"),
        "skipped": count("skipped"),
        "message": _short_text(value.get("message"), maximum=10_000),
    }


class PluginManager:
    """Track configured plugins, load enabled ones, and contain failures."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        db_path: Path | None = None,
        entry_points: Iterable[Any] | None = None,
        youtube_cookie_file: Path | None = None,
        proxy_url: str = "",
        youtube_session_factory: Callable[[str], PluginYoutubeSession] | None = None,
        my_activity_cookie_file: Path | None = None,
    ) -> None:
        self._config = config
        self._db_path = Path(db_path) if db_path is not None else None
        from .plugin_transport import PluginMyActivitySession

        self._my_activity_session_factory = (
            partial(PluginMyActivitySession, Path(my_activity_cookie_file), str(proxy_url or ""))
            if my_activity_cookie_file is not None else None
        )
        self._youtube_session_factory = youtube_session_factory
        if self._youtube_session_factory is None and youtube_cookie_file is not None:
            self._youtube_session_factory = partial(
                _open_plugin_youtube_session,
                Path(youtube_cookie_file),
                str(proxy_url or ""),
            )
        self._records: dict[str, _PluginRecord] = {}
        configured = config.get("plugins")
        if not isinstance(configured, dict):
            return
        configured_plugins = {
            plugin_id: dict(plugin_config)
            for plugin_id, plugin_config in configured.items()
            if (
                isinstance(plugin_id, str)
                and PLUGIN_ID.fullmatch(plugin_id)
                and isinstance(plugin_config, dict)
            )
        }
        if not configured_plugins:
            return
        has_enabled_plugin = any(
            plugin_config.get("enabled") is True
            for plugin_config in configured_plugins.values()
        )
        available = (
            list(entry_points) if entry_points is not None else _installed_entry_points()
        ) if has_enabled_plugin else []
        by_name: dict[str, list[Any]] = {}
        for entry_point in available:
            by_name.setdefault(str(entry_point.name), []).append(entry_point)
        for plugin_id, plugin_config in configured_plugins.items():
            record = _PluginRecord(plugin_id=plugin_id, configured=plugin_config)
            self._records[plugin_id] = record
            if plugin_config.get("enabled") is not True:
                record.state = "disabled"
                record.message = "Plugin is disabled"
                continue
            matches = by_name.get(plugin_id, [])
            if not matches:
                record.state = "missing"
                record.message = f"No installed {PLUGIN_ENTRY_POINT_GROUP} entry point named {plugin_id}"
                continue
            if len(matches) > 1:
                record.state = "error"
                record.message = f"Multiple installed entry points are named {plugin_id}"
                continue
            self._load_plugin(record, matches[0])

    def _load_plugin(self, record: _PluginRecord, entry_point: Any) -> None:
        try:
            factory = entry_point.load()
            instance = factory()
            instance_id = str(getattr(instance, "plugin_id", ""))
            api_version = int(getattr(instance, "plugin_api_version", 0))
            if instance_id != record.plugin_id:
                raise ValueError(
                    f"Entry point {record.plugin_id} created plugin {instance_id or '<missing>'}"
                )
            if api_version != PLUGIN_API_VERSION:
                record.state = "incompatible"
                record.message = (
                    f"Plugin API {api_version} is incompatible with host API {PLUGIN_API_VERSION}"
                )
                return
            required_host_features = _required_host_features(instance)
            missing_host_features = sorted(
                required_host_features.difference(PLUGIN_HOST_FEATURES)
            )
            if missing_host_features:
                record.state = "incompatible"
                record.message = (
                    "Plugin requires unavailable host features: "
                    + ", ".join(missing_host_features)
                )
                return
            capabilities = {
                str(value) for value in getattr(instance, "capabilities", ())
            }
            if "playlist_groups" in capabilities and not callable(
                getattr(instance, "project_playlist_groups", None)
            ):
                raise TypeError(
                    "Plugin playlist_groups capability requires "
                    "project_playlist_groups"
                )
            if "channel_groups" in capabilities and not callable(
                getattr(instance, "project_channel_groups", None)
            ):
                raise TypeError(
                    "Plugin channel_groups capability requires "
                    "project_channel_groups"
                )
            _browser_assets(instance)
            _browser_collection(record.plugin_id, instance)
            _worker_processes(instance)
            context = PluginContext(
                root=Path(__file__).resolve().parent.parent,
                config_path=Path(
                    str(self._config.get("_config_path") or "yt_library.config.json")
                ),
                plugin_id=record.plugin_id,
                plugin_config=dict(record.configured),
                host_features=PLUGIN_HOST_FEATURES,
                _library_video_lookup=(
                    partial(_library_videos_by_id, self._db_path)
                    if self._db_path is not None
                    else None
                ),
                _youtube_session_factory=self._youtube_session_factory,
                _my_activity_session_factory=self._my_activity_session_factory,
            )
            instance.start(context)
            record.instance = instance
            record.state = "loaded"
        except Exception as exc:
            record.state = "error"
            record.message = f"{type(exc).__name__}: {exc}"

    def _record_status(self, record: _PluginRecord) -> dict[str, Any]:
        instance = record.instance
        payload: dict[str, Any] = {
            "id": record.plugin_id,
            "name": str(record.configured.get("name") or record.plugin_id),
            "enabled": record.configured.get("enabled") is True,
            "state": record.state,
        }
        if record.message:
            payload["message"] = record.message
        if instance is None:
            return payload
        payload.update(
            {
                "name": str(getattr(instance, "plugin_name", record.plugin_id)),
                "version": str(getattr(instance, "plugin_version", "")),
                "apiVersion": int(getattr(instance, "plugin_api_version", 0)),
                "capabilities": sorted(str(value) for value in getattr(instance, "capabilities", ())),
                "requiredHostFeatures": sorted(_required_host_features(instance)),
            }
        )
        browser_assets = _browser_assets(instance)
        if browser_assets:
            payload["browserAssets"] = browser_assets
        collection = _browser_collection(record.plugin_id, instance)
        if collection:
            payload["browserCollection"] = collection
        worker_processes = _worker_processes(instance)
        if worker_processes:
            payload["workerProcesses"] = self._worker_process_statuses(
                record.plugin_id,
                worker_processes,
            )
        try:
            plugin_status = instance.status()
            if not isinstance(plugin_status, dict):
                raise TypeError("Plugin status must be a JSON object")
            payload["pluginStatus"] = plugin_status
            admin_metrics = _plugin_admin_metrics(plugin_status.get("adminMetrics"))
            if admin_metrics:
                payload["adminMetrics"] = admin_metrics
            payload["state"] = str(plugin_status.get("state") or "ready")
        except Exception as exc:
            payload["state"] = "error"
            payload["message"] = f"Status failed: {type(exc).__name__}: {exc}"
        return payload

    def statuses(self) -> list[dict[str, Any]]:
        return [self._record_status(record) for record in self._records.values()]

    def has_browser_collection(self, path: str) -> bool:
        path = path.rstrip("/")
        record = self._records.get(path.removeprefix("/"))
        return bool(
            record and record.configured.get("enabled") is True
            and record.state == "loaded" and record.instance is not None
            and _browser_collection(record.plugin_id, record.instance)
        )

    def _worker_process_statuses(
        self,
        plugin_id: str,
        processes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if self._db_path is None or not self._db_path.exists():
            return [
                {**process, "queuedCount": 0, "runningCount": 0, "latestRun": None}
                for process in processes
            ]
        conn = connect(self._db_path)
        try:
            queued = {
                str(row["task_type"]): int(row["count"] or 0)
                for row in conn.execute(
                    """
                    SELECT task_type, COUNT(*) AS count
                    FROM worker_queue
                    WHERE worker_type = 'plugin' AND source_key = ?
                    GROUP BY task_type
                    """,
                    (plugin_id,),
                )
            }
            running = {
                str(row["worker_id"]): int(row["count"] or 0)
                for row in conn.execute(
                    """
                    SELECT worker_id, COUNT(*) AS count
                    FROM plugin_worker_runs
                    WHERE plugin_id = ? AND status = 'running'
                    GROUP BY worker_id
                    """,
                    (plugin_id,),
                )
            }
            latest = {
                str(row["worker_id"]): dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM (
                      SELECT runs.*,
                             ROW_NUMBER() OVER (
                               PARTITION BY worker_id
                               ORDER BY started_at DESC, run_id DESC
                             ) AS rank
                      FROM plugin_worker_runs runs
                      WHERE plugin_id = ?
                    )
                    WHERE rank = 1
                    """,
                    (plugin_id,),
                )
            }
        finally:
            conn.close()
        return [
            {
                **process,
                "queuedCount": queued.get(process["id"], 0),
                "runningCount": running.get(process["id"], 0),
                "latestRun": latest.get(process["id"]),
            }
            for process in processes
        ]

    def process_definition(self, plugin_id: str, worker_id: str) -> dict[str, Any] | None:
        record = self._records.get(plugin_id)
        if record is None or record.instance is None:
            return None
        return next(
            (
                process
                for process in _worker_processes(record.instance)
                if process["id"] == worker_id
            ),
            None,
        )

    def process_definitions(self) -> dict[tuple[str, str], dict[str, Any]]:
        definitions: dict[tuple[str, str], dict[str, Any]] = {}
        for plugin_id, record in self._records.items():
            if record.instance is None:
                continue
            for process in _worker_processes(record.instance):
                definitions[(plugin_id, process["id"])] = process
        return definitions

    def enqueue_process(
        self,
        conn: sqlite3.Connection,
        plugin_id: str,
        worker_id: str,
        params: Mapping[str, Any] | None = None,
        *,
        manual: bool,
    ) -> dict[str, Any]:
        record = self._records.get(plugin_id)
        if record is None:
            raise LookupError(f"Plugin is not enabled: {plugin_id}")
        if record.instance is None:
            raise RuntimeError(f"Plugin is unavailable: {plugin_id}")
        process = self.process_definition(plugin_id, worker_id)
        if process is None:
            raise LookupError(f"Unknown plugin worker process: {plugin_id}/{worker_id}")
        try:
            planning_params = dict(params or {})
            planning_params["manual"] = bool(manual)
            planned = record.instance.plan_worker(
                worker_id,
                PluginPlanningContext(conn, plugin_id),
                planning_params,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Plugin worker planning failed: {plugin_id}/{worker_id}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if isinstance(planned, (str, bytes, Mapping)) or not isinstance(
            planned, IterableCollection
        ):
            raise TypeError("Plugin worker plan must be an iterable of task objects")
        now = utc_now()
        inserted = 0
        already_queued = 0
        planned_count = 0
        log_subject_id = ""
        for raw_task in planned:
            planned_count += 1
            if planned_count > PLUGIN_TASK_LIMIT:
                raise ValueError(
                    f"Plugin worker plan exceeds the {PLUGIN_TASK_LIMIT} task limit"
                )
            task = _normalize_plugin_task(raw_task)
            if planned_count == 1:
                log_subject_id = task["subject_id"]
            elif planned_count == 2:
                log_subject_id = ""
            subject_key = (
                f"plugin:{plugin_id}:{worker_id}:{task['task_id']}"
            )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO worker_queue(
                  subject_key, worker_type, task_type, video_id, current_title,
                  source_key, priority, manual, plugin_subject_id, payload_json,
                  created_at, updated_at
                )
                VALUES (?, 'plugin', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subject_key,
                    worker_id,
                    task["video_id"],
                    task["title"],
                    plugin_id,
                    task["priority"],
                    1 if manual else 0,
                    task["subject_id"],
                    task["payload_json"],
                    now,
                    now,
                ),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                already_queued += 1
                conn.execute(
                    """
                    UPDATE worker_queue
                    SET priority = MIN(priority, ?),
                        manual = MAX(manual, ?),
                        current_title = CASE
                          WHEN current_title = '' THEN ? ELSE current_title
                        END,
                        plugin_subject_id = ?,
                        payload_json = ?,
                        updated_at = ?
                    WHERE subject_key = ?
                    """,
                    (
                        task["priority"],
                        1 if manual else 0,
                        task["title"],
                        task["subject_id"],
                        task["payload_json"],
                        now,
                        subject_key,
                    ),
                )
        conn.execute(
            """
            INSERT INTO plugin_worker_log(
              run_id, plugin_id, worker_id, created_at, level, subject_id, message
            )
            VALUES ('', ?, ?, ?, 'queue info', ?, ?)
            """,
            (
                plugin_id,
                worker_id,
                now,
                log_subject_id,
                (
                    f"Queued {inserted} {process['name']} tasks; "
                    f"{already_queued} already queued"
                ),
            ),
        )
        return {
            "pluginId": plugin_id,
            "workerId": worker_id,
            "name": process["name"],
            "planned": planned_count,
            "inserted": inserted,
            "alreadyQueued": already_queued,
        }

    def enqueue_hook(
        self,
        conn: sqlite3.Connection,
        hook: str,
        params: Mapping[str, Any] | None = None,
        *,
        manual: bool = False,
    ) -> list[dict[str, Any]]:
        normalized_hook = str(hook or "").strip()
        hook_params = dict(params or {})
        hook_params["hook"] = normalized_hook
        results: list[dict[str, Any]] = []
        for index, ((plugin_id, worker_id), process) in enumerate(
            self.process_definitions().items()
        ):
            if normalized_hook not in process["hooks"]:
                continue
            savepoint = f"plugin_hook_{index}"
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                result = self.enqueue_process(
                    conn,
                    plugin_id,
                    worker_id,
                    hook_params,
                    manual=manual,
                )
            except Exception as exc:
                conn.execute(f"ROLLBACK TO {savepoint}")
                conn.execute(f"RELEASE {savepoint}")
                message = f"{type(exc).__name__}: {exc}"
                conn.execute(
                    """
                    INSERT INTO plugin_worker_log(
                      run_id, plugin_id, worker_id, created_at, level,
                      subject_id, message
                    )
                    VALUES ('', ?, ?, ?, 'queue error', '', ?)
                    """,
                    (
                        plugin_id,
                        worker_id,
                        utc_now(),
                        f"{normalized_hook} hook planning failed: {message}",
                    ),
                )
                result = {
                    "pluginId": plugin_id,
                    "workerId": worker_id,
                    "name": process["name"],
                    "planned": 0,
                    "inserted": 0,
                    "alreadyQueued": 0,
                    "error": message,
                }
            else:
                conn.execute(f"RELEASE {savepoint}")
            results.append(result)
        return results

    def run_worker(
        self,
        plugin_id: str,
        worker_id: str,
        task: Mapping[str, Any],
        runtime: PluginWorkerRuntime,
    ) -> dict[str, Any]:
        record = self._records.get(plugin_id)
        if record is None or record.instance is None:
            raise RuntimeError(f"Plugin worker is unavailable: {plugin_id}/{worker_id}")
        if self.process_definition(plugin_id, worker_id) is None:
            raise LookupError(f"Unknown plugin worker process: {plugin_id}/{worker_id}")
        try:
            runtime._manager = self
            return _normalize_worker_result(
                record.instance.run_worker(worker_id, dict(task), runtime)
            )
        except PluginWorkerStopped:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Plugin worker failed: {plugin_id}/{worker_id}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def handle_api(
        self,
        plugin_id: str,
        method: str,
        path: str,
        query: dict[str, list[str]],
        body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        record = self._records.get(plugin_id)
        if record is None:
            return 404, {"error": f"Plugin is not enabled: {plugin_id}"}
        if record.instance is None:
            return 503, {
                "error": f"Plugin is unavailable: {plugin_id}",
                "plugin": self._record_status(record),
            }
        try:
            if body is None:
                response = record.instance.handle_api(method, path, query)
            else:
                handler = getattr(record.instance, "handle_api_request", None)
                if not callable(handler):
                    return 405, {
                        "error": f"Plugin method is not supported: {plugin_id}/{path}"
                    }
                response = handler(method, path, query, body)
            if response is None:
                return 404, {"error": f"Unknown plugin route: {plugin_id}/{path}"}
            status, payload = response
            status = int(status)
            if status < 100 or status > 599:
                raise ValueError(f"Plugin returned invalid HTTP status {status}")
            return status, payload
        except Exception as exc:
            return 503, {
                "error": f"Plugin request failed: {plugin_id}",
                "message": f"{type(exc).__name__}: {exc}",
            }

    def handle_browser_asset(self, plugin_id: str, path: str) -> tuple[int, str, bytes]:
        record = self._records.get(plugin_id)
        if record is None:
            return 404, "application/json; charset=utf-8", _asset_error(
                f"Plugin is not enabled: {plugin_id}"
            )
        if record.instance is None:
            return 503, "application/json; charset=utf-8", _asset_error(
                f"Plugin is unavailable: {plugin_id}"
            )
        assets = {asset["path"]: asset for asset in _browser_assets(record.instance)}
        if path not in assets:
            return 404, "application/json; charset=utf-8", _asset_error(
                f"Unknown plugin browser asset: {plugin_id}/{path}"
            )
        try:
            response = record.instance.handle_browser_asset(path)
            if not isinstance(response, tuple) or len(response) != 2:
                raise TypeError("Plugin browser asset response must be (content_type, body)")
            content_type, body = response
            if isinstance(body, str):
                body = body.encode("utf-8")
            if not isinstance(body, bytes):
                raise TypeError("Plugin browser asset body must be bytes or text")
            return 200, str(content_type), body
        except Exception as exc:
            return 503, "application/json; charset=utf-8", _asset_error(
                f"Plugin browser asset failed: {type(exc).__name__}: {exc}"
            )

    def _search_card_handler(self, plugin_id: str, name: str):
        record = self._records.get(plugin_id)
        if record is None or record.instance is None:
            raise LookupError(f"Plugin is unavailable: {plugin_id}")
        handler = getattr(record.instance, name, None)
        if not callable(handler):
            raise TypeError(f"Plugin does not provide {name}: {plugin_id}")
        return handler

    def search_result_descriptors(self, plugin_id: str, query: str) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        payload = self._search_card_handler(plugin_id, "search_result_descriptors")(query)
        results = []
        seen = set()
        for raw in payload:
            if len(results) >= PLUGIN_TASK_LIMIT:
                raise ValueError("Plugin search result limit exceeded")
            if not isinstance(raw, Mapping):
                raise TypeError("Plugin search descriptor must be a mapping")
            identity = str(raw.get("id") or "")
            video_id = str(raw.get("video_id") or "")
            if not identity or len(identity) > 512 or identity in seen:
                raise ValueError("Plugin search result IDs must be nonempty and unique")
            if not YOUTUBE_VIDEO_ID.fullmatch(video_id):
                raise ValueError("Plugin search result must reference a video ID")
            dates = {}
            for key in ("newest_at", "oldest_at"):
                value = raw.get(key)
                if value:
                    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        raise ValueError("Plugin search dates must include a timezone")
                    value = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                dates[key] = value or ""
            likes = raw.get("like_count")
            if likes is not None and (type(likes) is not int or likes < 0):
                raise ValueError("Plugin search like count must be a nonnegative integer or null")
            results.append({"id": identity, "pluginId": plugin_id, "video_id": video_id,
                            "title": str(raw.get("title") or ""), "like_count": likes, **dates})
            seen.add(identity)
        return results

    def hydrate_search_results(self, plugin_id: str, ids: list[str], query: str) -> dict[str, Any]:
        if len(ids) > 5000:
            raise ValueError("Plugin search hydration accepts at most 5000 results")
        payload = self._search_card_handler(plugin_id, "hydrate_search_results")(ids, query)
        if not isinstance(payload, Mapping) or set(payload).difference(ids):
            raise ValueError("Plugin search hydration must map requested IDs to items")
        if any(not isinstance(item, Mapping) for item in payload.values()):
            raise TypeError("Plugin search items must be mappings")
        return {key: dict(value) for key, value in payload.items()}

    def filter_videos(
        self,
        plugin_id: str,
        query: str,
    ) -> tuple[frozenset[str], frozenset[str]]:
        record = self._records.get(plugin_id)
        if record is None:
            raise LookupError(f"Plugin is not enabled: {plugin_id}")
        if record.instance is None:
            raise RuntimeError(f"Plugin is unavailable: {plugin_id}")
        handler = getattr(record.instance, "filter_videos", None)
        if not callable(handler):
            raise TypeError(f"Plugin does not provide a video filter: {plugin_id}")
        try:
            payload = handler(query)
        except Exception as exc:
            raise RuntimeError(
                f"Plugin video filter failed: {plugin_id}: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise TypeError("Plugin video filter response must be a mapping")
        video_ids = _normalized_video_ids(payload.get("video_ids"), "video_ids")
        search_match_ids = _normalized_video_ids(
            payload.get("search_match_ids", ()),
            "search_match_ids",
        )
        if not search_match_ids.issubset(video_ids):
            raise ValueError("Plugin search matches must be included in its video filter")
        return video_ids, search_match_ids

    def filter_clips(
        self,
        plugin_id: str,
        query: str,
        clips: Iterable[Mapping[str, Any]],
    ) -> tuple[frozenset[str], frozenset[str]]:
        record = self._records.get(plugin_id)
        if record is None:
            raise LookupError(f"Plugin is not enabled: {plugin_id}")
        if record.instance is None:
            raise RuntimeError(f"Plugin is unavailable: {plugin_id}")
        handler = getattr(record.instance, "filter_clips", None)
        if not callable(handler):
            raise TypeError(f"Plugin does not provide a clip filter: {plugin_id}")
        normalized_clips: list[dict[str, Any]] = []
        requested_clip_ids: set[str] = set()
        for raw_clip in clips:
            clip_id = str(raw_clip.get("clip_id") or "").strip()
            source_video_id = str(raw_clip.get("source_video_id") or "").strip()
            if not clip_id or clip_id in requested_clip_ids:
                continue
            try:
                start_ms = max(0, int(raw_clip.get("start_ms") or 0))
                end_ms = int(raw_clip.get("end_ms") or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid clip bounds: {clip_id}") from exc
            if not source_video_id or end_ms <= start_ms:
                continue
            requested_clip_ids.add(clip_id)
            normalized_clips.append(
                {
                    "clip_id": clip_id,
                    "source_video_id": source_video_id,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                }
            )
        if len(normalized_clips) > PLUGIN_TASK_LIMIT:
            raise ValueError(f"Plugin clip filter exceeds the {PLUGIN_TASK_LIMIT} clip limit")
        try:
            payload = handler(query, tuple(normalized_clips))
        except Exception as exc:
            raise RuntimeError(
                f"Plugin clip filter failed: {plugin_id}: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise TypeError("Plugin clip filter response must be a mapping")
        clip_ids = _normalized_video_ids(payload.get("clip_ids"), "clip_ids")
        search_match_ids = _normalized_video_ids(
            payload.get("search_match_ids", ()),
            "search_match_ids",
        )
        requested = frozenset(requested_clip_ids)
        if not clip_ids.issubset(requested):
            raise ValueError("Plugin clip filter returned an unrequested clip ID")
        if not search_match_ids.issubset(clip_ids):
            raise ValueError("Plugin search matches must be included in its clip filter")
        return clip_ids, search_match_ids

    def project_videos(
        self,
        plugin_id: str,
        video_ids: Collection[str],
    ) -> dict[str, dict[str, str]]:
        record = self._records.get(plugin_id)
        if record is None:
            raise LookupError(f"Plugin is not enabled: {plugin_id}")
        if record.instance is None:
            raise RuntimeError(f"Plugin is unavailable: {plugin_id}")
        requested_video_ids = frozenset(
            video_id
            for value in video_ids
            if (video_id := str(value).strip())
        )
        if not requested_video_ids:
            return {}
        handler = getattr(record.instance, "project_videos", None)
        if not callable(handler):
            return {}
        try:
            payload = handler(requested_video_ids)
        except Exception as exc:
            raise RuntimeError(
                f"Plugin video projection failed: {plugin_id}: {type(exc).__name__}: {exc}"
            ) from exc
        return _normalized_video_projections(payload, requested_video_ids)

    def projected_video(self, video_id: str) -> dict[str, Any] | None:
        video_id = str(video_id).strip()
        if not video_id:
            return None
        projection: dict[str, Any] | None = None
        plugin_ids: list[str] = []
        for plugin_id in sorted(self._records):
            record = self._records[plugin_id]
            if record.instance is None:
                continue
            try:
                candidate = self.project_videos(plugin_id, {video_id}).get(video_id)
            except (RuntimeError, TypeError, ValueError):
                continue
            if candidate is None:
                continue
            plugin_ids.append(plugin_id)
            if projection is None or (not projection["title"] and candidate["title"]):
                projection = dict(candidate)
        if projection is None:
            return None
        projection["projection_plugin_ids"] = plugin_ids
        return projection

    def _project_navigation_groups(
        self,
        *,
        capability: str,
        method_name: str,
        identifier_field: str,
        normalizer: Any,
        known_identifiers: Collection[str] | None = None,
    ) -> dict[str, Any]:
        known = (
            None
            if known_identifiers is None
            else frozenset(
                identifier
                for value in known_identifiers
                if (identifier := str(value).strip())
            )
        )
        groups: list[dict[str, Any]] = []
        memberships: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for plugin_id in sorted(self._records):
            record = self._records[plugin_id]
            instance = record.instance
            if instance is None or capability not in {
                str(value) for value in getattr(instance, "capabilities", ())
            }:
                continue
            try:
                status = instance.status()
                if not isinstance(status, Mapping):
                    raise TypeError("Plugin status must be a JSON object")
                if str(status.get("state") or "ready") != "ready":
                    continue
                payload = getattr(instance, method_name)()
                projection = normalizer(plugin_id, payload)
                projected_memberships = _navigation_memberships_for_known_identifiers(
                    projection,
                    known,
                    identifier_field=identifier_field,
                )
            except Exception as exc:
                errors.append(
                    {
                        "pluginId": plugin_id,
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            groups.extend(projection["groups"])
            memberships.extend(projected_memberships)
        return {
            "groups": groups,
            "memberships": memberships,
            "errors": errors,
        }

    def project_playlist_groups(
        self,
        known_playlist_ids: Collection[str] | None = None,
    ) -> dict[str, Any]:
        return self._project_navigation_groups(
            capability="playlist_groups",
            method_name="project_playlist_groups",
            identifier_field="playlist_id",
            normalizer=_normalized_playlist_group_projection,
            known_identifiers=known_playlist_ids,
        )

    def project_channel_groups(
        self,
        known_channel_ids: Collection[str] | None = None,
    ) -> dict[str, Any]:
        return self._project_navigation_groups(
            capability="channel_groups",
            method_name="project_channel_groups",
            identifier_field="channel_id",
            normalizer=_normalized_channel_group_projection,
            known_identifiers=known_channel_ids,
        )

    def _identifiers_for_group(
        self,
        group_key: str,
        *,
        key_prefix: str,
        identifier_field: str,
        projection: dict[str, Any],
    ) -> frozenset[str] | None:
        normalized_group_key = str(group_key or "").strip()
        if not normalized_group_key.startswith(key_prefix):
            return None
        parent_by_group = {
            str(group["group_key"]): (
                str(group["parent_key"]) if group.get("parent_key") else None
            )
            for group in projection["groups"]
        }
        if normalized_group_key not in parent_by_group:
            return None

        selected_groups = {normalized_group_key}
        changed = True
        while changed:
            changed = False
            for candidate, parent in parent_by_group.items():
                if candidate not in selected_groups and parent in selected_groups:
                    selected_groups.add(candidate)
                    changed = True
        return frozenset(
            str(membership[identifier_field])
            for membership in projection["memberships"]
            if membership["group_key"] in selected_groups
        )

    def playlist_ids_for_group(
        self,
        group_key: str,
        known_playlist_ids: Collection[str] | None = None,
    ) -> frozenset[str] | None:
        if not str(group_key or "").strip().startswith(
            PLUGIN_PLAYLIST_GROUP_KEY_PREFIX
        ):
            return None
        return self._identifiers_for_group(
            group_key,
            key_prefix=PLUGIN_PLAYLIST_GROUP_KEY_PREFIX,
            identifier_field="playlist_id",
            projection=self.project_playlist_groups(known_playlist_ids),
        )

    def channel_ids_for_group(
        self,
        group_key: str,
        known_channel_ids: Collection[str] | None = None,
    ) -> frozenset[str] | None:
        if not str(group_key or "").strip().startswith(
            PLUGIN_CHANNEL_GROUP_KEY_PREFIX
        ):
            return None
        return self._identifiers_for_group(
            group_key,
            key_prefix=PLUGIN_CHANNEL_GROUP_KEY_PREFIX,
            identifier_field="channel_id",
            projection=self.project_channel_groups(known_channel_ids),
        )

    def shutdown(self) -> None:
        for record in reversed(list(self._records.values())):
            if record.instance is None:
                continue
            try:
                record.instance.shutdown()
            except Exception:
                pass
            record.instance = None


class PluginTaskWorker:
    """Run one queued plugin task with host-owned lifecycle persistence."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._run_id = ""

    def is_alive(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def is_running(self) -> bool:
        return self.is_alive() and not self._stop.is_set()

    def stop(self) -> None:
        self._stop.set()

    def start(
        self,
        db_path: Path,
        manager: PluginManager,
        row: Mapping[str, Any],
        *,
        cookie_file: Path | None = None,
        proxy_url: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {
                    "started": False,
                    "run_id": self._run_id,
                    "message": "Plugin worker task already running",
                }
            self._stop.clear()
            self._run_id = uuid.uuid4().hex
            self._thread = threading.Thread(
                target=self._run,
                args=(
                    Path(db_path),
                    manager,
                    dict(row),
                    Path(cookie_file) if cookie_file is not None else None,
                    str(proxy_url or ""),
                ),
                name=f"plugin-worker-{row.get('source_key', '')}-{row.get('task_type', '')}",
                daemon=True,
            )
            self._thread.start()
            return {
                "started": True,
                "run_id": self._run_id,
                "message": "Plugin worker task started",
            }

    def _run(
        self,
        db_path: Path,
        manager: PluginManager,
        row: dict[str, Any],
        cookie_file: Path | None,
        proxy_url: str,
    ) -> None:
        run_id = self._run_id
        queue_id = int(row.get("queue_id") or 0)
        plugin_id = str(row.get("source_key") or "").strip()
        worker_id = str(row.get("task_type") or "").strip()
        subject_id = str(
            row.get("plugin_subject_id") or row.get("video_id") or ""
        ).strip()
        if not subject_id:
            subject_key = str(row.get("subject_key") or "")
            subject_id = subject_key.rsplit(":", 1)[-1]
        started_at = utc_now()
        conn = connect(db_path)
        try:
            with conn:
                WorkerRunRecorder(conn, "plugin").start(
                    run_id,
                    message="Plugin worker task started",
                    started_at=started_at,
                    plugin_id=plugin_id,
                    worker_id=worker_id,
                    queue_id=queue_id,
                    subject_id=subject_id,
                )
        finally:
            conn.close()
        runtime = PluginWorkerRuntime(
            db_path,
            run_id=run_id,
            queue_id=queue_id,
            plugin_id=plugin_id,
            worker_id=worker_id,
            subject_id=subject_id,
            stop_event=self._stop,
            service=str(
                (manager.process_definition(plugin_id, worker_id) or {}).get(
                    "service", "local"
                )
            ),
            cookie_file=cookie_file,
            proxy_url=proxy_url,
        )
        status = "complete"
        result = {
            "outcome": "complete",
            "processed": 0,
            "found": 0,
            "failed": 0,
            "skipped": 0,
            "message": "",
        }
        try:
            try:
                payload = json.loads(str(row.get("payload_json") or "{}"))
            except json.JSONDecodeError as exc:
                raise ValueError("Queued plugin task payload is invalid JSON") from exc
            task = {
                "queue_id": queue_id,
                "subject_id": subject_id,
                "video_id": str(row.get("video_id") or ""),
                "title": str(row.get("current_title") or ""),
                "payload": payload,
            }
            result = manager.run_worker(plugin_id, worker_id, task, runtime)
            if self._stop.is_set():
                status = "interrupted"
                result["message"] = result["message"] or "Interrupted by stop request"
            else:
                conn = connect(db_path)
                try:
                    with conn:
                        conn.execute("DELETE FROM worker_queue WHERE queue_id = ?", (queue_id,))
                finally:
                    conn.close()
        except PluginWorkerStopped as exc:
            status = "interrupted"
            result = {
                "outcome": "cancelled",
                "processed": 0,
                "found": 0,
                "failed": 0,
                "skipped": 1,
                "message": str(exc) or "Interrupted by stop request",
            }
        except Exception as exc:
            status = "error"
            result = {
                "outcome": "worker_error",
                "processed": 1,
                "found": 0,
                "failed": 1,
                "skipped": 0,
                "message": f"{type(exc).__name__}: {exc}",
            }
            runtime.log("error", result["message"])
            conn = connect(db_path)
            try:
                with conn:
                    conn.execute("DELETE FROM worker_queue WHERE queue_id = ?", (queue_id,))
            finally:
                conn.close()
        finished_at = utc_now()
        conn = connect(db_path)
        try:
            with conn:
                WorkerRunRecorder(conn, "plugin").finish(
                    run_id,
                    status=status,
                    finished_at=finished_at,
                    outcome=result["outcome"],
                    processed=result["processed"],
                    found=result["found"],
                    failed=result["failed"],
                    skipped=result["skipped"],
                    message=result["message"],
                )
        finally:
            conn.close()
