"""Optional, bounded date enrichment; never changes playlist membership."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import re
import sqlite3
from typing import Any, Callable

from .config import config_path, configured_proxy
from .request_pacing import pace_outbound_request
from .time_utils import utc_now
from .youtube_data_api import build_youtube_data_service, _response_pages
from .network import ProxyUnavailableError, proxy_unavailable_error


class DateCollectionStopped(Exception):
    pass


def check_stop(should_stop: Callable[[], bool] | None) -> None:
    if should_stop and should_stop():
        raise DateCollectionStopped()


def valid_added_date(value: Any) -> str:
    from .core import normalize_utc_timestamp
    try:
        text = str(value or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            result = text
        else:
            result = normalize_utc_timestamp(text)
            parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
        return result if datetime(2005, 1, 1, tzinfo=timezone.utc) <= parsed <= datetime.now(timezone.utc) else ""
    except (ValueError, TypeError, OverflowError):
        return ""


def apply_added_dates(conn: sqlite3.Connection, playlist_id: str, evidence: list[dict[str, Any]]) -> int:
    rows = [dict(row) for row in conn.execute("SELECT rowid AS membership_rowid, * FROM playlist_items WHERE playlist_id=?", (playlist_id,))]
    counts = Counter(row["video_id"] for row in rows if row["video_id"])
    evidence_counts = Counter(row.get("video_id") for row in evidence if row.get("video_id"))
    set_counts = Counter(row["set_video_id"] for row in rows if row["set_video_id"])
    incoming_sets = Counter(row.get("set_video_id") for row in evidence if row.get("set_video_id"))
    saved = 0
    for item in evidence:
        date = valid_added_date(item.get("added_at"))
        if not date or item.get("added_at_source") not in {"youtube_api", "music_web", "takeout"}:
            continue
        set_id = item.get("set_video_id")
        candidates = []
        if set_id and set_counts[set_id] == incoming_sets[set_id] == 1:
            candidates = [row for row in rows if row["set_video_id"] == set_id and (not item.get("video_id") or row["video_id"] == item["video_id"])]
        elif item.get("video_id") and counts[item["video_id"]] == evidence_counts[item["video_id"]] == 1:
            candidates = [row for row in rows if row["video_id"] == item["video_id"]]
        if len(candidates) != 1:
            continue
        row = candidates[0]
        if set_id and row["set_video_id"] and set_id != row["set_video_id"]:
            continue
        if row["added_at"] and row["added_at"] != date:
            continue
        if row["added_at"] and row["added_at_source"] != "unknown":
            continue
        conn.execute("UPDATE playlist_items SET added_at=?, added_at_source=?, set_video_id=?, updated_at=? WHERE rowid=?",
                     (date, item["added_at_source"], row["set_video_id"] or set_id or "", utc_now(), row["membership_rowid"]))
        row.update(added_at=date, added_at_source=item["added_at_source"], set_video_id=row["set_video_id"] or set_id or "")
        saved += 1
    return saved


def api_playlist_dates(config: dict[str, Any], playlist_id: str, *, should_stop: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    def before_request() -> None:
        check_stop(should_stop)
        pace_outbound_request()
    service = build_youtube_data_service(config_path(config, "youtube_oauth_client_secrets"),
                                         config_path(config, "youtube_oauth_token"), configured_proxy(config))
    result = []
    for page_number, response in enumerate(_response_pages(lambda token: service.playlistItems().list(
        part="snippet", playlistId=playlist_id, maxResults=50, pageToken=token or None,
    ), before_request), 1):
        if page_number > 200:
            raise ValueError("Playlist date API page limit exceeded")
        for item in response.get("items") or []:
            snippet = item.get("snippet") or {}
            result.append({"video_id": (snippet.get("resourceId") or {}).get("videoId", ""),
                           "added_at": snippet.get("publishedAt"), "added_at_source": "youtube_api"})
    return result


def web_playlist_dates(config: dict[str, Any], playlist_id: str, *, should_stop: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    from . import core
    cookie_path = config_path(config, "youtube_cookies")
    opener = core.load_cookie_opener(cookie_path, configured_proxy(config))
    jar = core.load_cookie_jar(cookie_path)

    def fetch(host: str, music: bool) -> list[dict[str, Any]]:
        check_stop(should_stop)
        origin = "https://" + host
        url = origin + "/playlist?list=" + core.urllib.parse.quote(playlist_id)
        page = core.request_text(opener, url)
        if not core.youtube_page_is_authenticated(page):
            raise ValueError("Playlist date web session not authenticated")
        conf: dict[str, Any] = {}
        for match in re.finditer(r"ytcfg.set\(\s*(\{)", page):
            try:
                conf.update(json.JSONDecoder().raw_decode(page[match.start(1):])[0])
            except ValueError:
                pass
        context = conf["INNERTUBE_CONTEXT"]
        headers = {"Content-Type": "application/json", "Origin": origin, "X-Origin": origin,
                   "Referer": url, "User-Agent": context["client"].get("userAgent", "Mozilla/5.0"),
                   "X-Youtube-Client-Name": str(conf["INNERTUBE_CONTEXT_CLIENT_NAME"]),
                   "X-Youtube-Client-Version": context["client"]["clientVersion"],
                   "Authorization": core.sapisid_auth_header(jar, origin)}
        if "SESSION_INDEX" in conf:
            headers["X-Goog-AuthUser"] = str(conf["SESSION_INDEX"])
        if conf.get("DELEGATED_SESSION_ID"):
            headers["X-Goog-PageId"] = conf["DELEGATED_SESSION_ID"]
        extra = {"browseId": "VL" + playlist_id}
        if not music:
            extra["params"] = "wgYCCAA%3D"
        result, tokens = [], set()
        for _ in range(200):
            check_stop(should_stop)
            request = core.urllib.request.Request(origin + "/youtubei/v1/browse?key=" + core.urllib.parse.quote(conf.get("INNERTUBE_API_KEY", "")),
                data=json.dumps({"context": context, **extra}).encode(), headers=headers)
            with core.open_with_request_pacing(opener, request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            nodes = list(core.walk(data))
            if data.get("error") or any(n.get("loggedOut") is True for n in nodes):
                raise ValueError("Playlist date web request failed")
            if music and any(n.get("votingEnabled") is True or n.get("isVotingEnabled") is True for n in nodes):
                raise ValueError("Voting playlist dates are not supported")
            shelf_keys = ("musicPlaylistShelfRenderer", "musicPlaylistShelfContinuation") if music else ("playlistVideoListRenderer",)
            members = [n[key] for n in nodes for key in shelf_keys if isinstance(n.get(key), dict)
                       and (music or n[key].get("playlistId") == playlist_id)]
            members += [n["appendContinuationItemsAction"] for n in data.get("onResponseReceivedActions", [])
                        if "appendContinuationItemsAction" in n and (music or n["appendContinuationItemsAction"].get("targetId") == playlist_id)]
            renderer_key = "musicResponsiveListItemRenderer" if music else "playlistVideoRenderer"
            result.extend(n[renderer_key] for member in members for n in core.walk(member) if isinstance(n.get(renderer_key), dict))
            token = next((core.playlist_continuation_token(m) for m in members if core.playlist_continuation_token(m)), "")
            if not token:
                return result
            if token in tokens:
                raise ValueError("Repeated playlist date continuation")
            tokens.add(token)
            extra = {"continuation": token}
        raise ValueError("Playlist date web page limit exceeded")

    original = fetch("www.youtube.com", False)
    music = fetch("music.youtube.com", True)
    return match_music_dates(original, music)


def match_music_dates(original: list[dict[str, Any]], music: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(row.get("setVideoId") for row in original)
    by_set = {row.get("setVideoId"): row.get("videoId") for row in original if row.get("setVideoId") and counts[row["setVideoId"]] == 1}
    evidence = []
    for row in music:
        item = row.get("playlistItemData") or {}
        set_id = item.get("playlistSetVideoId")
        raw = item.get("voteSortValue")
        if set_id not in by_set or type(raw) not in (int, float):
            continue
        try:
            date = valid_added_date(datetime.fromtimestamp(raw, timezone.utc).isoformat())
        except (ValueError, OverflowError, OSError):
            continue
        if date:
            evidence.append({"set_video_id": set_id, "video_id": by_set[set_id], "added_at": date, "added_at_source": "music_web"})
    return evidence


def collect_playlist_dates(conn: sqlite3.Connection, config: dict[str, Any], playlist_id: str, *, should_stop: Callable[[], bool] | None = None) -> int:
    from .core import log_playlist_scan_event
    from .config import configured_youtube_data_api_enabled
    missing = conn.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id=? AND membership_state='current' AND video_id IS NOT NULL AND COALESCE(added_at,'')=''", (playlist_id,)).fetchone()[0]
    scan = conn.execute("SELECT date_checked_at, scan_status FROM playlist_scans WHERE playlist_id=?", (playlist_id,)).fetchone()
    if not missing or not scan or scan["scan_status"] != "ok":
        return 0
    if scan["date_checked_at"]:
        elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(scan["date_checked_at"].replace("Z", "+00:00"))
        if elapsed.total_seconds() < 86400:
            return 0
    with conn:
        conn.execute("UPDATE playlist_scans SET date_checked_at=? WHERE playlist_id=?", (utc_now(), playlist_id))
    saved = 0
    for source, fetch in (("youtube_api", api_playlist_dates), ("music_web", web_playlist_dates)):
        if source == "youtube_api" and not configured_youtube_data_api_enabled(config):
            continue
        try:
            check_stop(should_stop)
            evidence = fetch(config, playlist_id, should_stop=should_stop)
            with conn:
                count = apply_added_dates(conn, playlist_id, evidence)
                log_playlist_scan_event(conn, "", "info", f"Playlist added dates: {count} saved from {source}; existing dates and membership preserved", playlist_id)
            saved += count
        except DateCollectionStopped:
            with conn:
                conn.execute("UPDATE playlist_scans SET date_checked_at=NULL WHERE playlist_id=?", (playlist_id,))
            break
        except ProxyUnavailableError:
            raise
        except Exception as exc:
            proxy_error = proxy_unavailable_error(exc, configured_proxy(config))
            if proxy_error:
                raise proxy_error
            # Authentication failures can contain secrets; log only the type.
            with conn:
                log_playlist_scan_event(conn, "", "warn", f"Playlist added dates: {source} unavailable ({type(exc).__name__}); existing evidence preserved", playlist_id)
        missing = conn.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id=? AND membership_state='current' AND video_id IS NOT NULL AND COALESCE(added_at,'')=''", (playlist_id,)).fetchone()[0]
        if not missing:
            break
    return saved
