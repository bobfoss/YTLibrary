"""Command-line interface for YT Library Manager."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    config_int,
    config_path,
    configured_proxy,
    ensure_config_file,
    load_config,
)
from .core import (
    connect,
    discover_current_playlists,
    enqueue_placeholder_recovery_item,
    enqueue_playlist_scan_item,
    import_history,
    import_playlists,
    import_takeout_playlists,
    migrate_database,
    placeholder_recovery_candidate_rows,
    recover_archivarix_thumbnails,
    save_youtube_data_api_snapshot,
    worker_queue_rows_by_id,
)
from .my_activity import collect_my_activity
from .request_pacing import configure_request_pacing, pace_outbound_request
from .server import serve
from .workers import WorkerQueueDispatcher
from .youtube_data_api import (
    YouTubeDataApiError,
    authorize_youtube_data_api,
    build_youtube_data_service,
    fetch_youtube_account_snapshot,
)


def _service_base_url(args: argparse.Namespace) -> str:
    config = getattr(args, "config_data", {})
    try:
        requested_db = Path(args.db).resolve()
        configured_db = config_path(config, "database").resolve()
    except (OSError, RuntimeError):
        return ""
    if requested_db != configured_db:
        return ""
    host = str(config.get("host") or "127.0.0.1").strip()
    if host in {"", "0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{config_int(config, 'port')}"


def _request_service_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    timeout: float = 2.0,
) -> dict[str, Any] | None:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=b"" if method == "POST" else None,
        method=method,
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _active_service_base_url(args: argparse.Namespace) -> str:
    base_url = _service_base_url(args)
    if not base_url:
        return ""
    status = _request_service_json(base_url, "/api/admin/service/status")
    service = status.get("service", {}) if status else {}
    return base_url if service.get("status") == "running" else ""


def _manual_queue_priority(conn: Any, count: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MIN(priority), 0) AS priority FROM worker_queue"
    ).fetchone()
    return min(-1, int(row["priority"] or 0) - max(1, count))


def _queued_task_status(
    conn: Any,
    target: dict[str, Any],
) -> tuple[str, str]:
    if target["worker_type"] == "playlist":
        row = conn.execute(
            """
            SELECT scan_status, unavailable_count, video_count, error
            FROM playlist_scans
            WHERE playlist_id = ?
            """,
            (target["subject_id"],),
        ).fetchone()
        if not row:
            return "complete", target["label"]
        status = str(row["scan_status"] or "complete")
        detail = (
            str(row["error"] or status)
            if status not in {"ok", "complete"}
            else f"{int(row['unavailable_count'] or 0)} unavailable / {int(row['video_count'] or 0)} videos"
        )
        return status, f"{detail} - {target['label']}"
    row = conn.execute(
        """
        SELECT status, recovery_status, message
        FROM placeholder_recovery_worker_runs
        WHERE queue_id = ?
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (target["queue_id"],),
    ).fetchone()
    if not row:
        return "complete", target["label"]
    status = str(row["recovery_status"] or row["status"] or "complete")
    return status, f"{row['message'] or status} - {target['label']}"


def _retained_queue_error(conn: Any, queue_ids: set[int]) -> str:
    if not queue_ids:
        return ""
    placeholders = ",".join("?" for _ in queue_ids)
    row = conn.execute(
        f"""
        SELECT message
        FROM placeholder_recovery_worker_runs
        WHERE queue_id IN ({placeholders})
          AND status IN ('blocked', 'error', 'stopped')
        ORDER BY started_at DESC
        LIMIT 1
        """,
        sorted(queue_ids),
    ).fetchone()
    return str(row["message"] or "") if row else ""


def _run_queued_cli_batch(
    args: argparse.Namespace,
    targets: list[dict[str, Any]],
) -> dict[str, int]:
    if not targets:
        return {"completed": 0, "failed": 0}
    db_path = Path(args.db)
    pending = {int(target["queue_id"]): target for target in targets}
    service_url = _active_service_base_url(args)
    dispatcher: WorkerQueueDispatcher | None = None
    if service_url:
        response = _request_service_json(
            service_url,
            "/api/admin/queue/start",
            method="POST",
            timeout=10.0,
        )
        if response is None:
            raise SystemExit(
                "The configured service is running, but its worker queue could not be started. "
                "Queued tasks were retained."
            )
        remote_dispatcher = response.get("dispatcher", {})
        if remote_dispatcher.get("blocked"):
            raise SystemExit(
                f"Worker queue could not start: {remote_dispatcher.get('message') or 'unavailable'}. "
                "Queued tasks were retained."
            )
        print("Using the running service worker queue.")
    else:
        config = getattr(args, "config_data", {})
        dispatcher = WorkerQueueDispatcher()
        result = dispatcher.start(
            db_path,
            config_path(config, "youtube_cookies"),
            config_path(config, "video_thumbnail_dir"),
            config,
            queue_ids=pending,
        )
        if not result.get("started"):
            raise SystemExit(
                f"Worker queue could not start: {result.get('message') or 'unavailable'}. "
                "Queued tasks were retained."
            )
        print("Using a local worker queue for the selected tasks.")

    completed = 0
    failed = 0
    try:
        while pending:
            conn = connect(db_path)
            try:
                remaining = {
                    int(row["queue_id"])
                    for row in worker_queue_rows_by_id(conn, pending)
                }
                finished = [queue_id for queue_id in pending if queue_id not in remaining]
                for queue_id in finished:
                    target = pending.pop(queue_id)
                    completed += 1
                    status, message = _queued_task_status(conn, target)
                    if status in {"error", "blocked", "timeout"}:
                        failed += 1
                    print(f"[{completed:03d}/{len(targets):03d}] {message}")
                if not pending:
                    break
                runner_active = (
                    dispatcher.is_alive()
                    if dispatcher is not None
                    else bool(
                        (
                            _request_service_json(
                                service_url,
                                "/api/admin/status?queue_limit=0&include_logs=0",
                            )
                            or {}
                        ).get("workerQueueRunning")
                    )
                )
                if not runner_active:
                    message = _retained_queue_error(conn, set(pending))
                    raise SystemExit(
                        (message or "Worker queue stopped before the selected tasks completed.")
                        + " Queued tasks were retained."
                    )
            finally:
                conn.close()
            time.sleep(0.25)
    except KeyboardInterrupt as exc:
        if dispatcher is not None:
            dispatcher.stop()
        raise SystemExit("Interrupted; unfinished tasks remain in the worker queue.") from exc
    return {"completed": completed, "failed": failed}


def scan_hidden_queued(args: argparse.Namespace) -> dict[str, int]:
    if args.limit < 0:
        raise SystemExit("--limit must be zero or greater")
    conn = connect(Path(args.db))
    targets: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT playlist_id, title FROM playlists ORDER BY title COLLATE NOCASE"
        ).fetchall()
        if args.limit:
            rows = rows[: args.limit]
        priority = _manual_queue_priority(conn, len(rows))
        with conn:
            for index, row in enumerate(rows):
                subject_key = enqueue_playlist_scan_item(
                    conn,
                    row["playlist_id"],
                    title=row["title"] or row["playlist_id"],
                    source_key="cli:scan-hidden",
                    priority=priority + index,
                    manual=True,
                    payload={"cookie_file": str(Path(args.cookies))},
                )
                queued = conn.execute(
                    "SELECT queue_id FROM worker_queue WHERE subject_key = ?",
                    (subject_key,),
                ).fetchone()
                targets.append(
                    {
                        "queue_id": int(queued["queue_id"]),
                        "worker_type": "playlist",
                        "subject_id": row["playlist_id"],
                        "label": row["title"] or row["playlist_id"],
                    }
                )
    finally:
        conn.close()
    print(f"Queued {len(targets)} playlists for authoritative unavailable-video scans.")
    result = _run_queued_cli_batch(args, targets)
    print(f"Completed {result['completed']} playlist scans ({result['failed']} failed).")
    return result


def recover_unavailable_videos_queued(args: argparse.Namespace) -> dict[str, int]:
    if args.limit < 0:
        raise SystemExit("--limit must be zero or greater")
    if args.delay < 0:
        raise SystemExit("--delay must be zero or greater")
    conn = connect(Path(args.db))
    targets: list[dict[str, Any]] = []
    try:
        rows = placeholder_recovery_candidate_rows(
            conn,
            limit=args.limit,
            include_completed=True,
            video_id=args.video_id,
            only_missing_thumbnails=args.only_missing,
            likely_unavailable_only=args.likely_unavailable_only,
            order_by="video",
        )
        priority = _manual_queue_priority(conn, len(rows))
        with conn:
            for index, row in enumerate(rows):
                video_id = str(row["video_id"] or "")
                subject_key = f"placeholder:{video_id}"
                enqueue_placeholder_recovery_item(
                    conn,
                    video_id=video_id,
                    playlist_id=row["playlist_id"] or "",
                    current_title=row["title"] or video_id,
                    source_key="cli:recover-missing-thumbnails",
                    playlist_count=int(row["playlist_count"] or 0),
                    priority=priority + index,
                    manual=True,
                    task_type="thumbnail" if args.no_api else "recover",
                    payload={
                        "cookie_file": str(Path(args.archivarix_cookies)),
                        "thumbnail_dir": str(Path(args.thumbs)),
                        "refresh_metadata": bool(args.refresh_metadata),
                        "no_api": bool(args.no_api),
                        "delay_seconds": float(args.delay),
                    },
                )
                queued = conn.execute(
                    "SELECT queue_id FROM worker_queue WHERE subject_key = ?",
                    (subject_key,),
                ).fetchone()
                targets.append(
                    {
                        "queue_id": int(queued["queue_id"]),
                        "worker_type": "placeholder",
                        "subject_id": video_id,
                        "label": row["title"] or video_id,
                    }
                )
    finally:
        conn.close()
    scope = "likely unavailable" if args.likely_unavailable_only else "unavailable"
    print(f"Queued {len(targets)} {scope} video IDs for authoritative recovery.")
    result = _run_queued_cli_batch(args, targets)
    print(f"Completed {result['completed']} recovery tasks ({result['failed']} failed).")
    return result


def migrate(args: argparse.Namespace) -> None:
    ensure_config_file(args.config_data)
    migrate_database(Path(args.db))
    print(f"Migrated {args.db}")


def authorize_youtube(args: argparse.Namespace) -> None:
    try:
        token_path = authorize_youtube_data_api(
            Path(args.client_secrets),
            Path(args.token),
        )
    except YouTubeDataApiError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Saved YouTube Data API OAuth token to {token_path}")


def collect_youtube_data_api(args: argparse.Namespace) -> dict[str, int]:
    try:
        service = build_youtube_data_service(
            Path(args.client_secrets),
            Path(args.token),
            configured_proxy(getattr(args, "config_data", {})),
        )
        snapshot = fetch_youtube_account_snapshot(
            service,
            before_request=pace_outbound_request,
        )
    except YouTubeDataApiError as exc:
        raise SystemExit(str(exc)) from exc
    conn = connect(Path(args.db))
    try:
        with conn:
            stats = save_youtube_data_api_snapshot(conn, snapshot)
    finally:
        conn.close()
    print(
        f"Collected {stats['subscriptions']} subscriptions, "
        f"{stats['playlists']} playlists, and {stats['playlist_items']} playlist "
        f"items from the YouTube Data API; updated {stats['playlist_items_updated']} "
        f"and inserted {stats['playlist_items_inserted']} playlist-item dates "
        f"({stats['playlist_items_unmatched']} unmatched)."
    )
    return stats


def _preparse_config(argv: list[str] | None) -> tuple[list[str] | None, dict[str, Any]]:
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--version", action="version", version=f"YT Library {__version__}")
    config_parser.add_argument("--config", default=None)
    known, _ = config_parser.parse_known_args(argv)
    return argv, load_config(known.config)


def _attach_config(args: argparse.Namespace, config: dict[str, Any]) -> argparse.Namespace:
    args.config_data = config
    return args


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv, config = _preparse_config(argv)
    configure_request_pacing(config)
    parser = argparse.ArgumentParser(description="Import YouTube library data and browse it locally.")
    parser.add_argument("--version", action="version", version=f"YT Library {__version__}")
    parser.add_argument("--config", default=str(config["_config_path"]), help="Path to the JSON configuration file")
    subparsers = parser.add_subparsers(dest="command")

    import_parser = subparsers.add_parser("import", help="Import playlists and cache thumbnails")
    import_parser.add_argument("--db", default=str(config_path(config, "database")))
    import_parser.add_argument("--thumbs", default=str(config_path(config, "thumbnail_dir")))
    import_parser.add_argument("--cookies", default=str(config_path(config, "youtube_cookies")))
    import_parser.add_argument("--pockettube", required=True)
    import_parser.set_defaults(func=import_playlists)

    discover_parser = subparsers.add_parser(
        "discover-current",
        help="Discover current signed-in YouTube playlists and update library rows",
    )
    discover_parser.add_argument("--db", default=str(config_path(config, "database")))
    discover_parser.add_argument("--thumbs", default=str(config_path(config, "thumbnail_dir")))
    discover_parser.add_argument("--cookies", default=str(config_path(config, "youtube_cookies")))
    discover_parser.add_argument("--browse-id", default="FEplaylist_aggregation")
    discover_parser.add_argument("--include-system", action="store_true")
    discover_parser.set_defaults(func=discover_current_playlists)

    scan_parser = subparsers.add_parser("scan-hidden", help="Scan playlists for unavailable videos")
    scan_parser.add_argument("--db", default=str(config_path(config, "database")))
    scan_parser.add_argument("--cookies", default=str(config_path(config, "youtube_cookies")))
    scan_parser.add_argument("--limit", type=int, default=0, help="Scan only the first N playlists")
    scan_parser.set_defaults(func=scan_hidden_queued)

    archivarix_parser = subparsers.add_parser(
        "archivarix-thumbnails",
        help="Search Archivarix for deleted video thumbnail candidates",
    )
    archivarix_parser.add_argument("--db", default=str(config_path(config, "database")))
    archivarix_parser.add_argument("--thumbs", default=str(config_path(config, "archivarix_thumbnail_dir")))
    archivarix_parser.add_argument("--limit", type=int, default=0, help="Search only the first N affected playlists")
    archivarix_parser.add_argument("--page-size", type=int, default=50)
    archivarix_parser.set_defaults(func=recover_archivarix_thumbnails)

    takeout_parser = subparsers.add_parser("import-takeout", help="Import current playlists from an extracted Takeout")
    takeout_parser.add_argument("--db", default=str(config_path(config, "database")))
    takeout_parser.add_argument("--takeout", default=str(config_path(config, "takeout_dir")))
    takeout_parser.set_defaults(func=import_takeout_playlists)

    history_parser = subparsers.add_parser("import-history", help="Import YouTube Takeout watch/search history")
    history_parser.add_argument("--db", default=str(config_path(config, "database")))
    history_parser.add_argument("--takeout", default=str(config_path(config, "takeout_dir")))
    history_parser.add_argument("--history-key", default="")
    history_parser.set_defaults(func=import_history)

    my_activity_parser = subparsers.add_parser(
        "collect-my-activity",
        help="Collect exact YouTube watch and subscription events from Google My Activity",
    )
    my_activity_parser.add_argument("--db", default=str(config_path(config, "database")))
    my_activity_parser.add_argument(
        "--cookies",
        default=str(config_path(config, "my_activity_cookies")),
        help="Netscape cookie export containing google.com cookies",
    )
    my_activity_parser.add_argument(
        "--html",
        default="",
        help="Parse a saved My Activity HTML page instead of fetching it",
    )
    my_activity_parser.add_argument(
        "--max-pages",
        type=int,
        default=25,
        help="Fetch at most N activity pages, including the initial page (default: 25)",
    )
    my_activity_parser.set_defaults(func=collect_my_activity)

    youtube_authorize_parser = subparsers.add_parser(
        "authorize-youtube-data-api",
        help="Authorize read-only YouTube Data API access and save a local OAuth token",
    )
    youtube_authorize_parser.add_argument(
        "--client-secrets",
        default=str(config_path(config, "youtube_oauth_client_secrets")),
    )
    youtube_authorize_parser.add_argument(
        "--token",
        default=str(config_path(config, "youtube_oauth_token")),
    )
    youtube_authorize_parser.set_defaults(func=authorize_youtube)

    youtube_collect_parser = subparsers.add_parser(
        "collect-youtube-data-api",
        help="Collect subscription, playlist, and playlist-item dates through YouTube Data API",
    )
    youtube_collect_parser.add_argument("--db", default=str(config_path(config, "database")))
    youtube_collect_parser.add_argument(
        "--client-secrets",
        default=str(config_path(config, "youtube_oauth_client_secrets")),
    )
    youtube_collect_parser.add_argument(
        "--token",
        default=str(config_path(config, "youtube_oauth_token")),
    )
    youtube_collect_parser.set_defaults(func=collect_youtube_data_api)

    recover_missing_parser = subparsers.add_parser(
        "recover-missing-thumbnails",
        help="Recover Archivarix metadata for exact unavailable video IDs",
    )
    recover_missing_parser.add_argument("--db", default=str(config_path(config, "database")))
    recover_missing_parser.add_argument("--thumbs", default=str(config_path(config, "archivarix_thumbnail_dir")))
    recover_missing_parser.add_argument("--archivarix-cookies", default=str(config_path(config, "archivarix_cookies")))
    recover_missing_parser.add_argument("--video-id", default="")
    recover_missing_parser.add_argument("--limit", type=int, default=0)
    recover_missing_parser.add_argument("--only-missing", action="store_true")
    recover_missing_parser.add_argument("--likely-unavailable-only", action="store_true")
    recover_missing_parser.add_argument("--no-api", action="store_true", help="Only try direct Archivarix thumbnail URLs")
    recover_missing_parser.add_argument("--delay", type=float, default=3.0, help="Seconds to wait before each Archivarix API search")
    recover_missing_parser.add_argument("--refresh-metadata", action="store_true", help="Use Archivarix API even when a thumbnail is already cached")
    recover_missing_parser.set_defaults(func=recover_unavailable_videos_queued)

    migrate_parser = subparsers.add_parser(
        "migrate",
        help="Initialize or upgrade the configured database schema",
    )
    migrate_parser.add_argument("--db", default=str(config_path(config, "database")))
    migrate_parser.set_defaults(func=migrate)

    serve_parser = subparsers.add_parser("serve", help="Serve the library manager")
    serve_parser.add_argument("--db", default=str(config_path(config, "database")))
    serve_parser.add_argument("--cookies", default=str(config_path(config, "youtube_cookies")))
    serve_parser.add_argument("--video-thumbs", default=str(config_path(config, "video_thumbnail_dir")))
    serve_parser.add_argument("--takeout", default=str(config_path(config, "takeout_dir")))
    serve_parser.add_argument("--host", default=str(config["host"]))
    serve_parser.add_argument("--port", type=int, default=config_int(config, "port"))
    serve_parser.set_defaults(func=serve)

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args([*(argv or []), "serve"])
    _attach_config(args, config)
    if hasattr(args, "db") and args.command not in {"serve", "migrate"}:
        migrate_database(Path(args.db))
    args.func(args)
    return 0
