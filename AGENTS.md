# Repository Guidelines

Read this file, `TODO.md`, and `DEVELOPMENT_STATE.md` at the start of every
development chat. `TODO.md` is the source for unfinished or deferred work;
`DEVELOPMENT_STATE.md` is the concise implementation-and-decision handoff.

## Working Agreement

- Start from evidence: read the owning code and correlate UI reports with the
  API and database before diagnosing or changing behavior.
- Unless the user is explicitly exploring or asking only for an explanation,
  carry work through implementation, verification, live QA, and commit.
- Inspect `git status` first. Preserve unrelated tracked and untracked work,
  isolate intended hunks when staging, and never commit personal runtime data.
- Commit every coherent change after it passes verification unless asked not to.
  Use a substantive commit body. Push only when explicitly requested.
- Use the configured host and port. Runtime work is complete only after a live
  status or smoke check succeeds; include the PID when reporting a restart.
- Use `scripts\service.ps1` for Windows service status, start, restart, and stop
  operations. Its comment-based help is the source of truth for process and
  queue handling; do not assemble an ad hoc replacement, call SCM directly, or
  use the Admin restart endpoint. The script serializes mutating calls from
  concurrent chats and across Windows sessions. If the opt-in SCM service is
  installed, it supplies auto-start and process supervision while this script
  still owns queue intent, contention reporting, readiness, and verification;
  the script asks the persistent host to replace its child for a restart.
- Restart after server, worker, served HTML/JS, schema/bootstrap, or source
  config changes. Database-only updates normally do not require a restart.
- Run the full local checks below for code changes. UI and settings changes also
  require browser QA, including reload persistence and rapid-input behavior.
- Keep runtime settings and user preferences in `yt_library.config.json`, apply
  them live where designed, and provide migrations for schema changes.
- Store exact timestamps in UTC and present them in the configured display
  timezone. When discussing the user's data, convert times to Pacific unless
  the configured timezone says otherwise.
- Search is the default landing view; History remains a separate occurrence
  view with repeated watches and the daily heatmap. Keep detail and admin views.
  Use the order Videos, Playlists, Channels where applicable.
- Prefer shared UI and query helpers. Facet counts must remain stable when a
  filter is toggled, and loading must preserve existing headers, controls,
  pagination, heatmaps, and results instead of briefly blanking them.
- Unless explicitly directed otherwise, metadata decorators must reuse the
  established shared typography, sizing, and spacing. Vary glyphs and colors
  only where they communicate a distinct meaning.
- Watch completion belongs to history occurrences; manual metadata scans do not
  assign it. Leave missing titles blank, and derive channel `first_seen` from
  the earliest library evidence rather than the metadata-fetch date.
- Fetch YouTube metadata by direct ID or URL, not search fallback. Continuously
  validate configured authentication during long runs. A configured proxy is
  general and applies to all outbound services.
- Preserve useful playlist and video identity on failures. Keep unavailable,
  members-only, unlisted, private, removed, and unknown states distinct when
  evidence supports the distinction; do not invent missing rows or metadata.

## Project Structure & Module Organization

This repository is a Python web app for browsing, enriching, and reconciling a personal YouTube library.

- `yt_library_manager.py` is the compatibility CLI shim; keep existing commands routed through it.
- `yt_library/core.py` contains importers, parsers, metadata fetchers, and reconciliation logic.
- `yt_library/database.py` contains SQLite connection, schema bootstrap, and migrations.
- `yt_library/server.py` contains HTTP routing and local API endpoints.
- `yt_library/workers.py` contains background worker orchestration.
- `yt_library/worker_runs.py` contains typed persistence transitions shared by
  all worker run families.
- `yt_library/plugins.py` contains the versioned optional plugin host. Plugins
  depend on this API; the core application must not depend on plugin packages.
- `yt_library/queries.py` contains paginated browser list/detail read models, unified omni-search, and history search.
- `yt_library/schema.sql` is the SQLite schema, loaded by `yt_library/schema.py`.
- `yt_library/templates/` contains the browser and admin HTML plus their served
  JavaScript assets.
- `tests/` contains the standard-library `unittest` suite for helpers, schema bootstrap, and read models.
- `TODO.md` is the canonical project TODO and deferred-decisions list. Treat references to the TODO, TODO list, cleanup findings, or future work as references to this file.
- `DEVELOPMENT_STATE.md` summarizes the current implementation, durable
  decisions, and new-chat startup checklist. Keep it aligned when a change
  materially alters the development handoff, but do not duplicate the full
  architecture or TODO there.
- `requirements.txt` lists Python dependencies, including `yt-dlp`.
- `yt_library.sqlite3`, cookie files, thumbnail folders, and Takeout zip exports are local runtime data and should not be committed.
- Put rollback copies, pre-repair SQLite snapshots, retired databases, and
  backup cookie files in `backups/`; do not leave backups in the repository
  root or commit them. Keep only active configured databases and cookies at
  their configured paths.
- Common generated asset folders include `thumbs/`, `video_thumbs/`, and `archivarix_thumbs/`.

## Build, Test, and Development Commands

Use the repository root as the working directory.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$files = @("yt_library_manager.py") + (Get-ChildItem yt_library -Filter *.py | ForEach-Object { $_.FullName }) + (Get-ChildItem tests -Filter *.py | ForEach-Object { $_.FullName })
.\.venv\Scripts\python.exe -m py_compile @files
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe yt_library_manager.py
.\.venv\Scripts\python.exe yt_library_manager.py migrate
.\.venv\Scripts\python.exe yt_library_manager.py import-history
```

- Run the service from `.venv`; a standalone `yt-dlp.exe` on `PATH` does not
  provide the `yt_dlp` Python module imported by the application.
- `py_compile` catches syntax errors without running workers.
- The Python virtual environment does not provide Node.js, and bare `node` is
  not on this machine's PowerShell `PATH`. Run browser JavaScript checks with
  `C:\Users\michael.keenan\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe`.
- With no command, `yt_library_manager.py` creates `yt_library.config.json` if needed, initializes or migrates the configured database, and serves the local browser/admin UI.
- `migrate` initializes or upgrades the configured schema from the migration path in `yt_library/database.py`.
- `serve` starts the local browser/admin UI and initializes or migrates the configured database before listening.
- `import-history` imports Takeout watch history zips from the selected path and rebuilds reconciliation.

This repo is normally operated from PowerShell. Avoid Bash-only syntax such as `python - <<'PY'` here-docs. Prefer PowerShell-safe forms like `python -c "..."`, checked-in or temporary helper scripts when warranted, or explicit PowerShell here-strings piped intentionally. This project does not use `ENVIRONMENT.md`.

## Operational Notes

Treat video IDs, titles, and URLs as shell-hostile strings. YouTube IDs can start with `-`, and titles or copied values can contain leading spaces. When passing a dash-leading value to argparse, use the equals form so it cannot be parsed as an option:

```powershell
.\.venv\Scripts\python.exe yt_library_manager.py recover-missing-thumbnails --video-id=-R3PbSzyD9I
```

For ad hoc Python probes in PowerShell, avoid Bash here-doc syntax and prefer piping a PowerShell here-string into the bundled Python. When printing web/API payloads, force UTF-8 output to avoid Windows console encoding failures:

```powershell
$code = @'
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
'@
$code | & "C:\Users\michael.keenan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -B -
```

For service process troubleshooting, use `scripts\service.ps1 status` and read
`.codex\service-logs\service-control.log` plus the current run's
`service.stdout.log` and `service.stderr.log`. These are stable paths: the
controller log is bounded and rolling, while completed stream logs and their
run manifests move to the bounded `.codex\service-logs\archive` directory. The
SCM host also writes `service-host.log`. Keep service-operation fixes in that
utility so future chats share one verified procedure. Install, credential
refresh, and removal are handled by elevated `scripts\windows-service.ps1`;
ordinary lifecycle operations still go through `scripts\service.ps1`.

SQLite can be held open by long-running ad hoc probes. If schema initialization or imports fail with `database is locked`, inspect local `python.exe`/`pwsh.exe` processes for stale diagnostic scripts before changing application code. Stop only the stale probe, not the active server, unless a server restart is needed.

The unified dispatcher reads the next task from the persistent `worker_queue` before each launch. Priority changes and newly queued tasks can affect later dispatches during the same run, while already running tasks keep their original inputs. A server restart interrupts active in-process workers.

YouTube creator avatars may appear in newer watch-page data under `avatarViewModel.image.sources`, not only older `videoOwnerRenderer` or `channelThumbnailWithLinkRenderer` thumbnail shapes. Keep the channel avatar extractor broad enough to handle both.

### YouTube Data API Access

The maintainer confirmed YouTube Data API v3 access is available for this
project on 2026-10-03. Reuse the existing Python integration in
`yt_library/youtube_data_api.py`, which requests the `youtube.readonly` OAuth
scope. It currently collects the account's subscriptions and their timestamps,
owned playlists and creation dates, and playlist items and added dates.

OAuth file locations come from `youtube_oauth_client_secrets` and
`youtube_oauth_token` in `yt_library.config.json`; their defaults are
`youtube_oauth_client_secret.json` and `youtube_oauth_token.json`. API access is
separate from the authenticated YouTube/My Activity cookie sessions. Verify
the configured local credential files and current grant before relying on API
access in a task; availability of project access does not establish that this
checkout is authorized. Keep client secrets and tokens local and out of logs,
chat output, and commits. Use the configured proxy for API requests.

The existing local reference grant is stored separately from those runtime
defaults:

- OAuth desktop client: `.codex/reference/youtube_oauth_client_secret.json`
- Saved access/refresh token: `.codex/reference/youtube_oauth_token.json`

Both files are Git-ignored. Their presence, the saved `youtube.readonly` scope,
and refresh-token presence were verified on 2026-10-03 without exposing secret
values; this was not a live validation of the grant. The configured default
OAuth files are absent, so normal Update does not currently use this reference
grant. Keep that separation: the user retained API access for reference work,
not as a required runtime dependency or a replacement for adaptive direct-page
live-stream checks. Do not move these files into the runtime defaults or enable
API collection merely because credentials exist.

For explicitly requested read-only API probes, pass the reference paths to
`build_youtube_data_service(client_secrets_path, token_path, proxy_url)` in
`yt_library/youtube_data_api.py`, using `configured_proxy(config)`. The existing
loader refreshes an expired access token when a refresh token is available and
saves the refreshed credentials back to the supplied token file. Reuse this
grant instead of opening a new consent flow routinely; if refresh is rejected,
report that reauthorization is needed. Keep checks bounded and do not print
credentials or raw authentication failures that might contain sensitive data.

The existing CLI commands authorize the local read-only grant and collect an
account snapshot into the library, respectively. Both accept `--client-secrets`
and `--token` overrides. The collection command writes library data and is not
an authentication-only check; do not run it just to test the reference token:

```powershell
.\.venv\Scripts\python.exe yt_library_manager.py authorize-youtube-data-api
.\.venv\Scripts\python.exe yt_library_manager.py collect-youtube-data-api
```

### GitHub CLI And API Access

This development machine has authenticated GitHub CLI/API access as `bobfoss`
(verified 2026-10-03), including administrative access to YTL and the five plugin
repositories. Use `C:\Program Files\GitHub CLI\gh.exe` for repository inspection,
GitHub API requests, and explicitly authorized publishing/administration. This
is maintainer tooling, not an application dependency or end-user setup step.
Recheck the active account and target repository permissions before relying on
this access; do not print, copy, or commit authentication tokens.

```powershell
& "C:\Program Files\GitHub CLI\gh.exe" api user --jq .login
& "C:\Program Files\GitHub CLI\gh.exe" api repos/bobfoss/YTLibrary --jq '{full_name, permissions}'
```

The canonical repositories are `bobfoss/YTLibrary`, `bobfoss/YT-comments`,
`bobfoss/YT-live-chat`, `bobfoss/YT-pockettube`, `bobfoss/YT-subtitles`, and
`bobfoss/YT-llm`. Local origins use these names. Python distribution names and
plugin IDs have not changed. Existing immutable plugin catalog release URLs
retain their working lowercase spelling for cache compatibility; do not rewrite
published release records or artifact bytes just to change repository casing.
Authentication supplies capability, not authorization: push only when requested,
and keep releases and repository-setting changes within the user's request.

### Current YouTube Playlist UI

YouTube's current playlist page uses `pageHeaderRenderer.content.pageHeaderViewModel.metadata.contentMetadataViewModel.metadataRows` for header facts. The row can contain `Playlist`, the visibility label, the authoritative displayed count (for example `150 videos`), and the view count. Do not rely only on older `playlistHeaderRenderer` fields; parse this newer shape when refreshing playlist metadata or validating scan completeness.

Playlist pages may contain login-related strings such as `ServiceLogin` even when the provided cookie is accepted and the private playlist header is visible. Treat parsed header metadata as authoritative: if title, visibility, and video count are present, do not classify the page as signed out just because login markers are also present. Use login markers to explain failures only when the header count is missing.

The initial playlist page commonly exposes only the first 100 `playlistVideoRenderer` entries. Its continuation surface is currently fragile: the generic JSON continuation request can be treated as logged out, while the authenticated `youtubei` request may return only a logged-in response shell with no playlist entries. Adding click-tracking context alone did not resolve that behavior. Treat the displayed header count as the completeness guard and reject an extractor result that is short of it rather than replacing a fuller existing scan.

`yt-dlp` is useful but can return a different or transient playlist membership/count from the web page. It has returned a short partial list in one scan and a fuller list later for the same playlist. Compare its row count to the live page header before saving; preserve the prior raw scan on a short result. Keep the source/count evidence in worker logs so discrepancies are diagnosable.

For playlists owned by others, YouTube can report a larger displayed header count than the rows exposed to the current account. Do not synthesize unavailable rows from that count gap alone. Save the best nonzero visible row set, keep the displayed header count as reported playlist metadata, and log the exposed/reported mismatch. Only create no-ID unavailable placeholder rows when YouTube explicitly exposes an unavailable row or hidden-video notice.

## Coding Style & Naming Conventions

Prefer Python implementations and put changes in the module that owns the behavior. Use 4-space indentation, type hints for new helper functions, and descriptive snake_case names. Keep comments rare and useful. Follow existing patterns for SQLite helpers, worker classes, API route handling, and template edits.

## Testing Guidelines

The formal test suite uses Python's standard `unittest` runner. For each change, at minimum run:

```powershell
$files = @("yt_library_manager.py") + (Get-ChildItem yt_library -Filter *.py | ForEach-Object { $_.FullName }) + (Get-ChildItem tests -Filter *.py | ForEach-Object { $_.FullName })
.\.venv\Scripts\python.exe -m py_compile @files
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m ruff check .
git diff --check
```

Current tests cover pure helpers, Takeout watch-history parsing, fresh temporary SQLite schema bootstrap, server and worker behavior, static template DOM contracts, browser JavaScript syntax and behavior, browser bootstrap/list/detail read models, and omni/history search read models. Keep tests local-only: do not require real cookies, network access, personal Takeout zips, or the live `yt_library.sqlite3`.

For schema, import, or worker changes, also verify a fresh temporary database initializes from `schema.sql` and every supported prior schema upgrades in place without data loss. Treat failures against supported older schemas as migration bugs; do not require a rebuild or re-import as a workaround. Smoke test `/api/admin/status` and `/api/history/search?limit=1`.

Restart the local service when necessary, not automatically after every action. Restart after server code, served HTML/JS, config, schema/bootstrap, or worker behavior changes so the running app picks them up. A database-only update usually does not need a restart because API requests read SQLite fresh; verify with an endpoint instead.

## Data Modeling Notes

The database is a current-state model, not a metadata archive. `videos` owns canonical metadata and current playability. `playlist_items` and `history_events` link to videos and store only membership or event facts. New scans replace superseded metadata; failed or unavailable responses must not erase the last useful identity.

For hidden or memory-holed playlist videos, keep uncertainty visible. Preserve badges that distinguish `Unavailable`, `restored from Takeout`, `Takeout candidate`, and Archivarix statuses such as `DELETED_FULL_META` or `NOT_FOUND`. Avoid forcing ambiguous hidden-slot matches; show candidates when counts or positions do not support a confident mapping.

Video reaction state is stored on `videos.reaction` using YouTube's raw
`LIKE`, `DISLIKE`, or `INDIFFERENT` status. Empty means the response did not
contain a reliable reaction entity, so metadata upserts preserve the prior
value. The `Liked videos` browser view is derived from canonical videos instead
of being stored as a normal playlist.

Store exact timestamps as ISO 8601 UTC with `Z`. Date-only live-history observations keep `watched_at` null and use `watch_date` plus ordinal order. Generate stable YouTube and Archivarix URLs from IDs and capture timestamps instead of storing them.

## Commit & Pull Request Guidelines

Git history uses concise, imperative commit subjects such as `Clean up history storage schema` and `Import Takeout history from zip archives`. Every commit should also include a substantive body covering behavior changes, relevant schema or operational impact, and verification performed. Keep commits focused and avoid staging personal data artifacts. Pull requests should summarize behavior changes, schema changes, verification commands, and UI impact. Include screenshots for visible UI changes.

## Security & Configuration Tips

Cookie files, Takeout zips, SQLite databases, cached thumbnails, and logs can contain personal data. Treat them as local-only runtime state. Prefer passing cookies via explicit file paths, and never paste cookie values into commits, logs, or PR descriptions.
