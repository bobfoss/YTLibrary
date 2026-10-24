"""Read timestamped personal-library facts from the YouTube Data API."""

from __future__ import annotations

import base64
import binascii
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any, Callable, Iterator


YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"


class YouTubeDataApiError(RuntimeError):
    """Base error for optional YouTube Data API collection."""


class YouTubeDataApiNotConfigured(YouTubeDataApiError):
    """Raised when the local OAuth files have not been configured yet."""


class YouTubeDataApiAuthenticationError(YouTubeDataApiError):
    """Raised when the saved OAuth grant cannot be refreshed or used."""


@dataclass(frozen=True)
class YouTubeSubscription:
    channel_id: str
    title: str
    published_at: str


@dataclass(frozen=True)
class YouTubePlaylistItem:
    playlist_id: str
    video_id: str
    position: int
    title: str
    published_at: str
    set_video_id: str = ""


@dataclass(frozen=True)
class YouTubePlaylist:
    playlist_id: str
    title: str
    description: str
    owner_channel_id: str
    visibility: str
    published_at: str
    video_count: int
    items: tuple[YouTubePlaylistItem, ...]


@dataclass(frozen=True)
class YouTubeAccountSnapshot:
    subscriptions: tuple[YouTubeSubscription, ...]
    playlists: tuple[YouTubePlaylist, ...]


def playlist_item_set_video_id(playlist_id: str, item_id: Any) -> str:
    """Decode the verified API occurrence format, failing closed if it changes."""
    if not playlist_id or not isinstance(item_id, str) or len(item_id) > 256:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", item_id):
        return ""
    try:
        decoded = base64.b64decode(item_id + "=" * (-len(item_id) % 4), altchars=b"-_", validate=True)
        canonical = base64.urlsafe_b64encode(decoded).decode("ascii")
        if item_id not in (canonical, canonical.rstrip("=")):
            return ""
        prefix, separator, set_id = decoded.decode("ascii").partition(".")
    except (ValueError, UnicodeError, binascii.Error):
        return ""
    # Undocumented encoding: never adopt another playlist's ID or guess a new shape.
    if separator and prefix == playlist_id and re.fullmatch(r"[0-9A-F]{16}", set_id):
        return set_id
    return ""


def _google_dependencies() -> tuple[Any, Any, Any, Any]:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise YouTubeDataApiNotConfigured(
            "YouTube Data API support needs the Google API dependencies from requirements.txt"
        ) from exc
    return Request, Credentials, InstalledAppFlow, build


def _write_private_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(value)
            if not value.endswith("\n"):
                handle.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def authorize_youtube_data_api(client_secrets_path: Path, token_path: Path) -> Path:
    """Run Google's installed-app OAuth flow and save the resulting local token."""

    if not client_secrets_path.is_file():
        raise YouTubeDataApiNotConfigured(
            f"YouTube OAuth client-secret file not found: {client_secrets_path}"
        )
    _Request, _Credentials, InstalledAppFlow, _build = _google_dependencies()
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secrets_path),
            [YOUTUBE_READONLY_SCOPE],
        )
        credentials = flow.run_local_server(port=0, open_browser=True)
    except Exception as exc:
        raise YouTubeDataApiAuthenticationError(
            f"YouTube OAuth authorization failed: {exc}"
        ) from exc
    _write_private_text_atomic(token_path, credentials.to_json())
    return token_path


def _load_credentials(client_secrets_path: Path, token_path: Path, proxy_url: str = "") -> Any:
    Request, Credentials, _InstalledAppFlow, _build = _google_dependencies()
    if not token_path.is_file():
        raise YouTubeDataApiNotConfigured(
            "YouTube Data API OAuth token is missing; run authorize-youtube-data-api"
        )
    try:
        credentials = Credentials.from_authorized_user_file(
            str(token_path),
            [YOUTUBE_READONLY_SCOPE],
        )
    except Exception as exc:
        raise YouTubeDataApiAuthenticationError(
            f"Could not load the YouTube OAuth token: {exc}"
        ) from exc
    if credentials.expired and credentials.refresh_token:
        try:
            if proxy_url:
                import requests
                with requests.Session() as session:
                    session.trust_env = False
                    session.proxies.update({"http": proxy_url, "https": proxy_url})
                    credentials.refresh(Request(session=session))
            else:
                credentials.refresh(Request())
        except Exception as exc:
            raise YouTubeDataApiAuthenticationError(
                f"Could not refresh the YouTube OAuth token: {exc}"
            ) from exc
        _write_private_text_atomic(token_path, credentials.to_json())
    if not credentials.valid:
        suffix = (
            f" Client secrets are expected at {client_secrets_path}."
            if client_secrets_path
            else ""
        )
        raise YouTubeDataApiAuthenticationError(
            "The YouTube OAuth token is no longer valid; authorize it again." + suffix
        )
    return credentials


def youtube_oauth_status(token_path: Path, *, enabled: bool) -> dict[str, Any]:
    """Inspect saved credential readiness without refreshing or making requests."""
    result = {"enabled": enabled, "status": "disabled", "label": "disabled",
              "tone": "", "message": "YouTube Data API collection is disabled.", "expiresAt": ""}
    if not enabled:
        return result

    def state(status: str, label: str, message: str, tone: str = "warn") -> dict[str, Any]:
        return {**result, "status": status, "label": label, "tone": tone,
                "message": message + " Local credential check only; not live verification with Google."}

    try:
        if not token_path.is_file():
            return state("missing", "not configured", "The configured OAuth token file is missing. Run authorize-youtube-data-api to sign in.")
        _Request, Credentials, _InstalledAppFlow, _build = _google_dependencies()
        credentials = Credentials.from_authorized_user_file(str(token_path))
        if not credentials.client_id or not credentials.client_secret:
            return state("invalid", "sign-in required", "The saved OAuth credentials are incomplete. Run authorize-youtube-data-api again.")
        if not credentials.has_scopes([YOUTUBE_READONLY_SCOPE]):
            return state("scope_missing", "sign-in required", "The saved grant does not record the required YouTube read-only scope. Run authorize-youtube-data-api again.")
        if credentials.expiry:
            result["expiresAt"] = credentials.expiry.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        if credentials.expired and credentials.refresh_token:
            return state("refresh_pending", "refresh on next use", "The access token needs refreshing. A saved refresh token is available; collection will try to refresh it on the next API use.", "")
        if credentials.valid:
            return state("ready", "ready", "The saved access token is not expired. Revocation and server-side restrictions are detected only when it is used.", "valid")
        return state("expired", "sign-in required", "The access token is unusable and cannot be refreshed. Run authorize-youtube-data-api again.")
    except YouTubeDataApiNotConfigured:
        return state("unavailable", "dependencies missing", "Install the Google API dependencies from requirements.txt.")
    except OSError:
        return state("unreadable", "file unavailable", "The configured OAuth token file cannot be read.")
    except Exception:
        # Credential parser errors can contain token contents; never expose them.
        return state("invalid", "invalid credentials", "The configured OAuth token file could not be parsed. Run authorize-youtube-data-api again.")


def build_youtube_data_service(
    client_secrets_path: Path,
    token_path: Path,
    proxy_url: str = "",
) -> Any:
    """Build an authenticated YouTube v3 service from local OAuth material."""

    _Request, _Credentials, _InstalledAppFlow, build = _google_dependencies()
    credentials = _load_credentials(client_secrets_path, token_path, proxy_url)
    try:
        if proxy_url:
            import httplib2
            import socks
            from google_auth_httplib2 import AuthorizedHttp

            from .network import parse_socks5_proxy_url

            proxy = parse_socks5_proxy_url(proxy_url)
            if proxy is None:
                raise ValueError("Proxy configuration was empty")
            proxy_info = httplib2.ProxyInfo(
                socks.PROXY_TYPE_SOCKS5,
                proxy.host,
                proxy.port,
                proxy_rdns=proxy.remote_dns,
                proxy_user=proxy.username or None,
                proxy_pass=proxy.password or None,
            )
            authorized_http = AuthorizedHttp(
                credentials,
                http=httplib2.Http(proxy_info=proxy_info, timeout=30),
            )
            return build(
                "youtube",
                "v3",
                http=authorized_http,
                cache_discovery=False,
            )
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
        return build("youtube", "v3", http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30)), cache_discovery=False)
    except Exception as exc:
        raise YouTubeDataApiError(f"Could not initialize the YouTube Data API: {exc}") from exc


def _response_pages(
    request_factory: Any,
    before_request: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    page_token = ""
    seen_tokens: set[str] = set()
    while True:
        if before_request:
            before_request()
        try:
            response = request_factory(page_token).execute()
        except Exception as exc:
            raise YouTubeDataApiError(f"YouTube Data API request failed: {exc}") from exc
        if not isinstance(response, dict):
            raise YouTubeDataApiError("YouTube Data API returned an unexpected response")
        yield response
        next_token = str(response.get("nextPageToken") or "")
        if not next_token:
            return
        if next_token in seen_tokens:
            raise YouTubeDataApiError("YouTube Data API returned a repeated page token")
        seen_tokens.add(next_token)
        page_token = next_token


def fetch_youtube_account_snapshot(
    service: Any,
    *,
    before_request: Callable[[], None] | None = None,
) -> YouTubeAccountSnapshot:
    """Fetch a complete current snapshot of subscriptions and owned playlists."""

    subscriptions: list[YouTubeSubscription] = []
    for response in _response_pages(
        lambda token: service.subscriptions().list(
            part="snippet",
            mine=True,
            maxResults=50,
            pageToken=token or None,
        ),
        before_request,
    ):
        for item in response.get("items") or []:
            snippet = item.get("snippet") if isinstance(item, dict) else {}
            resource = snippet.get("resourceId") if isinstance(snippet, dict) else {}
            channel_id = str(resource.get("channelId") or "") if isinstance(resource, dict) else ""
            if not channel_id:
                continue
            subscriptions.append(
                YouTubeSubscription(
                    channel_id=channel_id,
                    title=str(snippet.get("title") or ""),
                    published_at=str(snippet.get("publishedAt") or ""),
                )
            )

    playlist_rows: list[dict[str, Any]] = []
    for response in _response_pages(
        lambda token: service.playlists().list(
            part="snippet,status,contentDetails",
            mine=True,
            maxResults=50,
            pageToken=token or None,
        ),
        before_request,
    ):
        playlist_rows.extend(
            item for item in (response.get("items") or []) if isinstance(item, dict)
        )

    playlists: list[YouTubePlaylist] = []
    for playlist_row in playlist_rows:
        playlist_id = str(playlist_row.get("id") or "")
        if not playlist_id:
            continue
        item_rows: list[YouTubePlaylistItem] = []
        for response in _response_pages(
            lambda token, current_id=playlist_id: service.playlistItems().list(
                part="snippet",
                playlistId=current_id,
                maxResults=50,
                pageToken=token or None,
            ),
            before_request,
        ):
            for item in response.get("items") or []:
                snippet = item.get("snippet") if isinstance(item, dict) else {}
                resource = snippet.get("resourceId") if isinstance(snippet, dict) else {}
                video_id = str(resource.get("videoId") or "") if isinstance(resource, dict) else ""
                if not video_id:
                    continue
                item_rows.append(
                    YouTubePlaylistItem(
                        playlist_id=playlist_id,
                        video_id=video_id,
                        position=max(0, int(snippet.get("position") or 0)) + 1,
                        title=str(snippet.get("title") or ""),
                        published_at=str(snippet.get("publishedAt") or ""),
                        set_video_id=playlist_item_set_video_id(playlist_id, item.get("id")),
                    )
                )
        snippet = playlist_row.get("snippet") or {}
        status = playlist_row.get("status") or {}
        content_details = playlist_row.get("contentDetails") or {}
        playlists.append(
            YouTubePlaylist(
                playlist_id=playlist_id,
                title=str(snippet.get("title") or ""),
                description=str(snippet.get("description") or ""),
                owner_channel_id=str(snippet.get("channelId") or ""),
                visibility=str(status.get("privacyStatus") or ""),
                published_at=str(snippet.get("publishedAt") or ""),
                video_count=max(0, int(content_details.get("itemCount") or 0)),
                items=tuple(item_rows),
            )
        )
    return YouTubeAccountSnapshot(
        subscriptions=tuple(subscriptions),
        playlists=tuple(playlists),
    )
