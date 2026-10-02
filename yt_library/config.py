"""Runtime configuration for YT Library Manager."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .network import validated_socks5_proxy_url


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "yt_library.config.json"
ConfigMutationResult = TypeVar("ConfigMutationResult")

DEFAULT_CONFIG: dict[str, Any] = {
    "database": "yt_library.sqlite3",
    "youtube_cookies": "yt_cookies.txt",
    "my_activity_cookies": "my_activity_cookies.txt",
    "archivarix_cookies": "archivarix_cookies.txt",
    "youtube_oauth_client_secrets": "youtube_oauth_client_secret.json",
    "youtube_oauth_token": "youtube_oauth_token.json",
    "thumbnail_dir": "thumbs",
    "archivarix_thumbnail_dir": "archivarix_thumbs",
    "video_thumbnail_dir": "video_thumbs",
    "takeout_dir": "takeout",
    "host": "127.0.0.1",
    "port": 8765,
    "display_timezone": "",
    "week_start": "sunday",
    "hide_empty_filters": True,
    "search_card_layout": "grid",
    "playlist_card_layout": "grid",
    "history_card_layout": "compact",
    "channel_playlisted_video_card_layout": "grid",
    "channel_playlist_card_layout": "grid",
    "channel_history_card_layout": "detailed",
    "sort_preferences": {},
    "page_size": 100,
    "partial_completion_min_percent": 1,
    "filter_preferences": {},
    "search_filter_tree_expanded": [
        "kind:videos",
        "kind:playlists",
        "kind:channels",
    ],
    "navigation_group_tree_collapsed": [],
    "update_frequency": "off",
    "update_hour_minute": 0,
    "update_time": "03:00",
    "admin_advanced": False,
    "plugins": {},
    "use_proxy": False,
    "proxy": "",
    "dispatch_mode": "delay",
    "job_dispatch_delay_seconds": 5.0,
    "request_delay_min_seconds": 6.0,
    "request_delay_max_seconds": 10.0,
    "youtube_max_in_flight": 10,
    "archivarix_max_in_flight": 1,
    "archivarix_request_timeout_seconds": 15.0,
    "archivarix_stream_timeout_seconds": 30.0,
    "archivarix_retry_attempts": 3,
    "archivarix_retry_backoff_seconds": 2.0,
    "archivarix_auto_retry": True,
    "broadcast_polling": {
        "recent_watch_days": 7,
        "initial_live_hours": 24,
        "established_live_days": 7,
        "upcoming_near_hours": 24,
        "frequent_hours": 1,
        "normal_hours": 6,
        "long_running_hours": 24,
    },
}

_LEGACY_YOUTUBE_REQUEST_INTERVAL_SECONDS = 5.0
_LEGACY_ARCHIVARIX_REQUEST_INTERVAL_SECONDS = 3.0
CARD_LAYOUTS = frozenset({"grid", "detailed", "compact"})
WEEK_STARTS = frozenset({"sunday", "monday"})
PAGE_SIZES = frozenset({50, 100, 250, 500})
SEARCH_SORTS = frozenset(
    {"relevance", "title", "title_desc", "newest", "oldest", "most_watched", "type"}
)
PLAYLIST_VIDEO_SORTS = frozenset(
    {"newest_added", "title", "title_desc", "oldest_added", "most_watched", "playlist_order"}
)
SEARCH_SORT_CONTEXTS = frozenset(
    {
        "search",
        "videos",
        "playlisted",
        "liked",
        "playlists",
        "channels",
        "subscribed",
        "terminated",
        "playlist-group",
        "channel-group",
    }
)
SORT_PREFERENCE_VALUES = {
    **{context: SEARCH_SORTS for context in SEARCH_SORT_CONTEXTS},
    "playlist": PLAYLIST_VIDEO_SORTS,
}
FILTER_PREFERENCE_KEYS = frozenset(
    {
        "videos.unavailable",
        "completion.partial_below_minimum",
        "playlist_videos.unavailable",
        "playlist_videos.removed",
        "playlists.unavailable",
        "channels.terminated",
    }
)
PLUGIN_FILTER_PREFERENCE_PATTERN = re.compile(
    r"^plugins\.[a-z][a-z0-9_-]*\.(?:search|filters\.[a-z][a-z0-9_-]*)$"
)
SEARCH_FILTER_TREE_NODE_PATTERN = re.compile(
    r"^(?:kind|facet):[A-Za-z][A-Za-z0-9_-]{0,79}$"
)
NAVIGATION_GROUP_TREE_NODE_PREFIXES = (
    "playlist-group:",
    "channel-group:",
)
DAILY_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
UPDATE_FREQUENCIES = frozenset({"off", "hourly", "daily"})


def _configured_float(
    config: dict[str, Any],
    key: str,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    default = float(DEFAULT_CONFIG[key])
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    value = max(minimum, value)
    return min(maximum, value) if maximum is not None else value


def _configured_int(
    config: dict[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    default = int(DEFAULT_CONFIG[key])
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def configured_display_timezone(config: dict[str, Any]) -> str:
    value = str(config.get("display_timezone") or "").strip()
    if not value:
        return ""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return value


def effective_display_timezone(config: dict[str, Any]) -> str:
    return configured_display_timezone(config) or "UTC"


def configured_week_start(config: dict[str, Any]) -> str:
    value = str(config.get("week_start") or "").strip().lower()
    return value if value in WEEK_STARTS else "sunday"


def configured_hide_empty_filters(config: dict[str, Any]) -> bool:
    value = config.get("hide_empty_filters", DEFAULT_CONFIG["hide_empty_filters"])
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def configured_card_layout(
    config: dict[str, Any],
    key: str,
    default: str,
) -> str:
    value = str(config.get(key) or "").strip().lower()
    return value if value in CARD_LAYOUTS else default


def configured_search_card_layout(config: dict[str, Any]) -> str:
    return configured_card_layout(config, "search_card_layout", "grid")


def configured_playlist_card_layout(config: dict[str, Any]) -> str:
    return configured_card_layout(config, "playlist_card_layout", "grid")


def configured_history_card_layout(config: dict[str, Any]) -> str:
    return configured_card_layout(config, "history_card_layout", "compact")


def configured_channel_playlist_card_layout(config: dict[str, Any]) -> str:
    return configured_card_layout(config, "channel_playlist_card_layout", "grid")


def configured_channel_playlisted_video_card_layout(config: dict[str, Any]) -> str:
    return configured_card_layout(
        config,
        "channel_playlisted_video_card_layout",
        "grid",
    )


def configured_channel_history_card_layout(config: dict[str, Any]) -> str:
    return configured_card_layout(config, "channel_history_card_layout", "detailed")


def configured_page_size(config: dict[str, Any]) -> int:
    try:
        value = int(config.get("page_size", DEFAULT_CONFIG["page_size"]))
    except (TypeError, ValueError):
        return int(DEFAULT_CONFIG["page_size"])
    return value if value in PAGE_SIZES else int(DEFAULT_CONFIG["page_size"])


def configured_partial_completion_min_percent(config: dict[str, Any]) -> int:
    return _configured_int(
        config,
        "partial_completion_min_percent",
        minimum=1,
        maximum=99,
    )


def configured_filter_preferences(config: dict[str, Any]) -> dict[str, bool]:
    raw_preferences = config.get("filter_preferences")
    if not isinstance(raw_preferences, dict):
        return {}
    return {
        str(raw_key): True
        for raw_key, raw_value in raw_preferences.items()
        if valid_filter_preference_key(str(raw_key)) and raw_value is True
    }


def valid_filter_preference_key(value: str) -> bool:
    return value in FILTER_PREFERENCE_KEYS or bool(
        PLUGIN_FILTER_PREFERENCE_PATTERN.fullmatch(value)
    )


def configured_search_filter_tree_expanded(config: dict[str, Any]) -> list[str]:
    raw_nodes = config.get(
        "search_filter_tree_expanded",
        DEFAULT_CONFIG["search_filter_tree_expanded"],
    )
    if not isinstance(raw_nodes, list):
        raw_nodes = DEFAULT_CONFIG["search_filter_tree_expanded"]
    nodes: list[str] = []
    for raw_node in raw_nodes:
        node = str(raw_node or "").strip()
        if valid_search_filter_tree_node(node) and node not in nodes:
            nodes.append(node)
    return nodes


def valid_search_filter_tree_node(value: str) -> bool:
    return bool(SEARCH_FILTER_TREE_NODE_PATTERN.fullmatch(value or ""))


def configured_navigation_group_tree_collapsed(
    config: dict[str, Any],
) -> list[str]:
    raw_nodes = config.get("navigation_group_tree_collapsed", [])
    if not isinstance(raw_nodes, list):
        return []
    nodes: list[str] = []
    for raw_node in raw_nodes:
        node = str(raw_node or "").strip()
        if valid_navigation_group_tree_node(node) and node not in nodes:
            nodes.append(node)
    return nodes


def valid_navigation_group_tree_node(value: str) -> bool:
    normalized = str(value or "").strip()
    return (
        1 <= len(normalized) <= 1_100
        and not any(ord(char) < 32 for char in normalized)
        and any(
            normalized.startswith(prefix) and len(normalized) > len(prefix)
            for prefix in NAVIGATION_GROUP_TREE_NODE_PREFIXES
        )
    )


def configured_sort_preferences(config: dict[str, Any]) -> dict[str, str]:
    raw_preferences = config.get("sort_preferences")
    if not isinstance(raw_preferences, dict):
        return {}
    preferences: dict[str, str] = {}
    for raw_context, raw_value in raw_preferences.items():
        context = str(raw_context).strip().lower()
        value = str(raw_value or "").strip().lower()
        if value in SORT_PREFERENCE_VALUES.get(context, frozenset()):
            preferences[context] = value
    return preferences


def configured_update_frequency(config: dict[str, Any]) -> str:
    value = str(config.get("update_frequency") or "").strip().lower()
    return (
        value
        if value in UPDATE_FREQUENCIES
        else str(DEFAULT_CONFIG["update_frequency"])
    )


def configured_update_hour_minute(config: dict[str, Any]) -> int:
    try:
        value = int(
            config.get("update_hour_minute", DEFAULT_CONFIG["update_hour_minute"])
        )
    except (TypeError, ValueError):
        return int(DEFAULT_CONFIG["update_hour_minute"])
    return value if 0 <= value <= 59 else int(DEFAULT_CONFIG["update_hour_minute"])


def configured_admin_advanced(config: dict[str, Any]) -> bool:
    value = config.get("admin_advanced", DEFAULT_CONFIG["admin_advanced"])
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def configured_plugins(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_plugins = config.get("plugins")
    if not isinstance(raw_plugins, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for raw_plugin_id, raw_settings in raw_plugins.items():
        plugin_id = str(raw_plugin_id).strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", plugin_id):
            continue
        if isinstance(raw_settings, bool):
            settings: dict[str, Any] = {"enabled": raw_settings}
        elif isinstance(raw_settings, dict):
            settings = dict(raw_settings)
        else:
            continue
        enabled = settings.get("enabled", False)
        if not isinstance(enabled, bool):
            enabled = str(enabled).strip().lower() in {"1", "true", "yes", "on"}
        settings["enabled"] = enabled
        normalized[plugin_id] = settings
    return normalized


def valid_update_time(value: str) -> bool:
    return bool(DAILY_TIME_PATTERN.fullmatch((value or "").strip()))


def valid_update_frequency(value: str) -> bool:
    return (value or "").strip().lower() in UPDATE_FREQUENCIES


def valid_update_hour_minute(value: str) -> bool:
    try:
        minute = int((value or "").strip())
    except (TypeError, ValueError):
        return False
    return 0 <= minute <= 59


def configured_update_time(config: dict[str, Any]) -> str:
    value = str(config.get("update_time") or "").strip()
    return value if valid_update_time(value) else str(DEFAULT_CONFIG["update_time"])


def next_update_at(
    config: dict[str, Any],
    now: datetime | None = None,
) -> datetime:
    current_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if configured_update_frequency(config) == "hourly":
        candidate = current_utc.replace(
            minute=configured_update_hour_minute(config),
            second=0,
            microsecond=0,
        )
        if candidate <= current_utc:
            candidate += timedelta(hours=1)
        return candidate
    zone = ZoneInfo(effective_display_timezone(config))
    local_now = current_utc.astimezone(zone)
    hour, minute = (int(part) for part in configured_update_time(config).split(":"))
    candidate = datetime.combine(
        local_now.date(),
        time(hour=hour, minute=minute),
        tzinfo=zone,
    )
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def configured_dispatch_mode(config: dict[str, Any]) -> str:
    value = str(config.get("dispatch_mode") or "").strip().lower()
    if value in {"delay", "throttle"}:
        return value
    legacy_value = config.get("request_jitter_enabled", False)
    if isinstance(legacy_value, bool):
        return "throttle" if legacy_value else "delay"
    return (
        "throttle"
        if str(legacy_value).strip().lower() in {"1", "true", "yes", "on"}
        else "delay"
    )


def configured_job_dispatch_delay(config: dict[str, Any]) -> float:
    return _configured_float(
        config,
        "job_dispatch_delay_seconds",
        minimum=0.0,
    )


def configured_proxy_address(config: dict[str, Any]) -> str:
    return validated_socks5_proxy_url(
        str(config.get("proxy", DEFAULT_CONFIG["proxy"]) or "")
    )


def configured_use_proxy(config: dict[str, Any]) -> bool:
    value = config.get("use_proxy")
    if value is None:
        return bool(configured_proxy_address(config))
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def configured_proxy(config: dict[str, Any]) -> str:
    proxy_url = configured_proxy_address(config)
    return proxy_url if configured_use_proxy(config) else ""


def configured_request_delay_range(config: dict[str, Any]) -> tuple[float, float]:
    minimum = _configured_float(
        config,
        "request_delay_min_seconds",
        minimum=0.0,
    )
    maximum = _configured_float(
        config,
        "request_delay_max_seconds",
        minimum=minimum,
    )
    return minimum, maximum


def configured_youtube_max_in_flight(config: dict[str, Any]) -> int:
    return _configured_int(
        config,
        "youtube_max_in_flight",
        minimum=1,
        maximum=100,
    )


def configured_archivarix_max_in_flight(config: dict[str, Any]) -> int:
    return _configured_int(
        config,
        "archivarix_max_in_flight",
        minimum=1,
        maximum=20,
    )


def configured_archivarix_request_timeout(config: dict[str, Any]) -> float:
    return _configured_float(
        config,
        "archivarix_request_timeout_seconds",
        minimum=1.0,
        maximum=120.0,
    )


def configured_archivarix_stream_timeout(config: dict[str, Any]) -> float:
    return _configured_float(
        config,
        "archivarix_stream_timeout_seconds",
        minimum=1.0,
        maximum=300.0,
    )


def configured_archivarix_retry_attempts(config: dict[str, Any]) -> int:
    return _configured_int(
        config,
        "archivarix_retry_attempts",
        minimum=1,
        maximum=10,
    )


def configured_archivarix_retry_backoff(config: dict[str, Any]) -> float:
    return _configured_float(
        config,
        "archivarix_retry_backoff_seconds",
        minimum=0.0,
        maximum=60.0,
    )


def configured_archivarix_auto_retry(config: dict[str, Any]) -> bool:
    value = config.get("archivarix_auto_retry", DEFAULT_CONFIG["archivarix_auto_retry"])
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def configured_broadcast_polling(config: dict[str, Any]) -> dict[str, int]:
    incoming = config.get("broadcast_polling")
    incoming = incoming if isinstance(incoming, dict) else {}
    defaults = DEFAULT_CONFIG["broadcast_polling"]
    result = {}
    for key, default in defaults.items():
        try:
            result[key] = max(1, min(8760, int(incoming.get(key, default))))
        except (TypeError, ValueError, OverflowError):
            result[key] = default
    return result


CONFIG_NORMALIZERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "broadcast_polling": configured_broadcast_polling,
    "display_timezone": configured_display_timezone,
    "week_start": configured_week_start,
    "hide_empty_filters": configured_hide_empty_filters,
    "search_card_layout": configured_search_card_layout,
    "playlist_card_layout": configured_playlist_card_layout,
    "history_card_layout": configured_history_card_layout,
    "channel_playlisted_video_card_layout": (
        configured_channel_playlisted_video_card_layout
    ),
    "channel_playlist_card_layout": configured_channel_playlist_card_layout,
    "channel_history_card_layout": configured_channel_history_card_layout,
    "sort_preferences": configured_sort_preferences,
    "page_size": configured_page_size,
    "partial_completion_min_percent": configured_partial_completion_min_percent,
    "filter_preferences": configured_filter_preferences,
    "search_filter_tree_expanded": configured_search_filter_tree_expanded,
    "navigation_group_tree_collapsed": configured_navigation_group_tree_collapsed,
    "update_frequency": configured_update_frequency,
    "update_hour_minute": configured_update_hour_minute,
    "update_time": configured_update_time,
    "admin_advanced": configured_admin_advanced,
    "plugins": configured_plugins,
    "dispatch_mode": configured_dispatch_mode,
    "job_dispatch_delay_seconds": configured_job_dispatch_delay,
    "request_delay_min_seconds": lambda config: configured_request_delay_range(config)[0],
    "request_delay_max_seconds": lambda config: configured_request_delay_range(config)[1],
    "youtube_max_in_flight": configured_youtube_max_in_flight,
    "archivarix_max_in_flight": configured_archivarix_max_in_flight,
    "archivarix_request_timeout_seconds": configured_archivarix_request_timeout,
    "archivarix_stream_timeout_seconds": configured_archivarix_stream_timeout,
    "archivarix_retry_attempts": configured_archivarix_retry_attempts,
    "archivarix_retry_backoff_seconds": configured_archivarix_retry_backoff,
    "archivarix_auto_retry": configured_archivarix_auto_retry,
    "proxy": configured_proxy_address,
    "use_proxy": configured_use_proxy,
}


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize every registered runtime setting in dependency order."""

    for key, normalizer in CONFIG_NORMALIZERS.items():
        config[key] = normalizer(config)
    return config


def load_config(config_path: Path | str | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Config file must contain a JSON object: {path}")
        config.update(
            {
                key: value
                for key, value in loaded.items()
                if key in DEFAULT_CONFIG and value is not None
            }
        )
        if "channel_playlisted_video_card_layout" not in loaded:
            config["channel_playlisted_video_card_layout"] = (
                configured_channel_playlist_card_layout(loaded)
            )
        if "update_frequency" not in loaded:
            legacy_enabled = loaded.get(
                "update_daily",
                loaded.get("history_fetch_daily", False),
            )
            if isinstance(legacy_enabled, bool):
                enabled = legacy_enabled
            else:
                enabled = str(legacy_enabled).strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
            config["update_frequency"] = "daily" if enabled else "off"
        if "update_time" not in loaded and "history_fetch_time" in loaded:
            config["update_time"] = loaded["history_fetch_time"]
        if "dispatch_mode" not in loaded:
            config["dispatch_mode"] = configured_dispatch_mode(loaded)
        if "job_dispatch_delay_seconds" not in loaded:
            legacy_delays: list[float] = []
            for key, fallback in (
                (
                    "youtube_request_interval_seconds",
                    _LEGACY_YOUTUBE_REQUEST_INTERVAL_SECONDS,
                ),
                (
                    "archivarix_request_interval_seconds",
                    _LEGACY_ARCHIVARIX_REQUEST_INTERVAL_SECONDS,
                ),
            ):
                try:
                    value = float(loaded.get(key, fallback))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    legacy_delays.append(max(0.0, value))
            if legacy_delays:
                config["job_dispatch_delay_seconds"] = max(legacy_delays)
        if "request_delay_min_seconds" not in loaded:
            legacy_minimums: list[float] = []
            for key in (
                "youtube_request_delay_min_seconds",
                "archivarix_request_delay_min_seconds",
            ):
                try:
                    value = float(
                        loaded.get(key, DEFAULT_CONFIG["request_delay_min_seconds"])
                    )
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    legacy_minimums.append(max(0.0, value))
            if legacy_minimums:
                config["request_delay_min_seconds"] = max(legacy_minimums)
        if "request_delay_max_seconds" not in loaded:
            legacy_maximums: list[float] = []
            for key in (
                "youtube_request_delay_max_seconds",
                "archivarix_request_delay_max_seconds",
            ):
                try:
                    value = float(
                        loaded.get(key, DEFAULT_CONFIG["request_delay_max_seconds"])
                    )
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    legacy_maximums.append(max(0.0, value))
            if legacy_maximums:
                config["request_delay_max_seconds"] = max(legacy_maximums)
        if "use_proxy" not in loaded and str(loaded.get("proxy") or "").strip():
            config["use_proxy"] = True
    normalize_config(config)
    config["_config_path"] = str(path)
    return config


def ensure_config_file(config: dict[str, Any]) -> Path:
    path = Path(str(config.get("_config_path") or DEFAULT_CONFIG_PATH))
    if path.exists():
        return path
    save_config(config)
    ensure_directory(config_path(config, "takeout_dir"))
    return path


def _config_payload(config: dict[str, Any]) -> dict[str, Any]:
    return {key: config.get(key, DEFAULT_CONFIG[key]) for key in DEFAULT_CONFIG}


def _atomic_write_config(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def save_config(config: dict[str, Any]) -> Path:
    path = Path(str(config.get("_config_path") or DEFAULT_CONFIG_PATH))
    _atomic_write_config(path, _config_payload(config))
    return path


class ConfigStore:
    """Serialize copy-on-write runtime configuration changes and persistence."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._lock = threading.RLock()

    def update(
        self,
        mutation: Callable[[dict[str, Any]], ConfigMutationResult],
    ) -> ConfigMutationResult:
        with self._lock:
            updated = deepcopy(self.config)
            result = mutation(updated)
            save_config(updated)
            for key in tuple(self.config):
                if key not in updated:
                    del self.config[key]
            for key, value in updated.items():
                if key not in self.config or self.config[key] != value:
                    self.config[key] = value
            return deepcopy(result)


def config_path(config: dict[str, Any], key: str) -> Path:
    value = Path(str(config[key]))
    if value.is_absolute():
        return value
    base = Path(str(config.get("_config_path") or DEFAULT_CONFIG_PATH)).resolve().parent
    return base / value


def ensure_directory(path: Path | str) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def config_int(config: dict[str, Any], key: str) -> int:
    return int(config[key])
