# Project TODO

`DEVELOPMENT_STATE.md` is the current implementation-and-decision handoff.
This file remains authoritative for unfinished work and deferred decisions.

This review uses the current code as truth and ranks remaining cleanup by
duplication risk. The project supports upgrading existing databases through
versioned migrations as well as creating a fresh current schema.

## Completed Cleanup

### Schema Bootstrap And Migrations

- `migrate` initializes fresh databases from `yt_library/schema.sql` and upgrades
  supported older schema versions through `yt_library/database.py`.
- Migration tests preserve user data and verify each supported schema transition;
  future schema changes must add a versioned migration and regression coverage.
- The newest selected Takeout is imported as current input rather than accumulated as metadata history.

### Canonical Current-State Model

- `videos` is the only owner of video metadata, playability, availability, reaction, and current progress.
- `playlist_items` stores playlist membership and unavailable-slot facts without copying video metadata.
- `history_events` stores exact or date-only watch events without copying video metadata or fabricating midnight timestamps.
- `video_recovery` stores only current Archivarix status, capture time, media availability, and errors.
- The former raw/reconciled playlist, snapshot, history, metadata, and candidate tables are removed.
- Metadata and playlist queues now share `worker_queue`; the old persisted metadata queue is removed.
- The superseded `CHANNEL_NORMALIZATION_PLAN.md` is removed; `design.md` now owns the current model.

### Legacy Archivarix Availability Verification

- Archivarix evidence no longer changes canonical YouTube availability as of
  commit `07c3ee7`. The live-data audit confirmed that 194 public/playable
  videos with `DELETED_*` or `NOT_FOUND` recovery evidence are correctly
  modeled and require no repair.
- Six possible legacy-overwrite candidates were rescanned directly on
  2026-08-10, but YouTube returned inconclusive `no_metadata` results. These
  candidates are resolved by product decision: future authoritative scans may
  update their canonical state naturally, but no targeted retry, repair,
  inference, or speculative backfill remains planned. Their independent
  `video_recovery` evidence is preserved.

### Time And URL Normalization

- Exact timestamps are ISO 8601 UTC values ending in `Z`.
- Live-history rows retain date and ordinal when an exact time is unavailable.
- Browser JavaScript detects and saves an IANA timezone only when the setting is missing; Admin can override it.
- Stable YouTube and Archivarix URLs are generated from IDs and archive capture timestamps.
- Schema, API, and template state use `unavailable` rather than the retired `hidden` compatibility names.

### Configuration And Queue Operations

- Runtime defaults live in `yt_library.config.json`; the database no longer contains `app_settings`.
- Browser and Admin settings share a serialized copy-on-write configuration
  store that atomically replaces the JSON file, preventing overlapping threaded
  requests from losing unrelated preference changes.
- Timezone set and reset routes share scheduler notification and history-date
  reconciliation; browser timezone reset persists the detected zone in one
  request.
- New installs bind to `127.0.0.1` by default, while a specific Tailscale address can be configured for remote access.
- YouTube and Archivarix cookie paths, launch intervals, and concurrency limits have explicit config keys.
- The persistent queue dispatches tasks by priority and applies independent YouTube and Archivarix launch cadence and concurrency limits.
- Initialize, Update, and Rebuild share a declarative library queue planner;
  Rebuild regenerates automatic core plan rows while preserving manual requests,
  Clip, Archivarix-recovery, plugin, and future non-plan work.
- Update and Rebuild no longer poll the costly Liked videos system playlist.
  New History occurrences still queue direct metadata for their videos, where
  raw `LIKE`, `DISLIKE`, and `INDIFFERENT` statuses update canonical reaction
  state. Initialize and explicit Scan all retain the full Liked videos scan.
- Ordinary playlists are not rescanned merely because seven days elapsed.
  Automatic planning selects never-scanned, failed, or reported-count-mismatch
  playlists; explicit Scan all remains the force-refresh path.
- The compatibility `scan-hidden` and `recover-missing-thumbnails` commands now
  enqueue and wait on the same playlist-scan and placeholder-recovery workers as
  the Admin UI. Per-command filters, delays, cookie paths, thumbnail paths, and
  direct-thumbnail-only mode travel in task payloads instead of invoking legacy
  orchestration code.
- YouTube authentication is checked throughout metadata work so an expired cookie stops the run instead of silently degrading later tasks.
- Admin queue and log views use incremental polling rather than repeatedly transferring full snapshots.
- Admin parameter POSTs share one JSON/error transport. Action refresh and
  polling, immediate queue-stop controls, and restart waiting remain explicit
  caller policies; raw cookie-file uploads retain their separate text body.

### Browser Workflow

- Liked videos are derived from canonical reaction metadata and have video-count, unavailable, and sort controls.
- History and channel-history views have navigable activity heatmaps that remain stable across pagination and year changes.
- Global and channel History share one generation-safe page/activity/card/pager
  workflow. Heatmap year, Today, and Sync transitions share busy-state,
  stale-work rejection, and complete date/page rollback on failure.
- Internal Takeout/YouTube source and match badges are retained in data where needed but are not rendered to users.
- Video and channel detail pages avoid repeated headings, and exact video timestamps render in the configured display timezone.
- Omni-search is server-owned: SQLite filters source and text fields, deduplicates canonical videos across playlist/history evidence, ranks all entity types, counts the complete result set, and returns the requested page.
- The browser no longer blocks on a whole-library `/api/data` snapshot. A lightweight bootstrap supplies navigation counts, while playlist, video, channel, and detail read models return only the requested page and hydrate only visible cards.

### Shared Video Card Rendering

- `video-card.js` owns video-card DOM construction, thumbnails, progress, creator chips, watch summaries, sparklines, reactions, details, and descriptions.
- The main browser and standalone history page provide thin row adapters to the shared renderer.
- Page-specific CSS remains responsible for layout, so standalone history keeps wide horizontal cards while playlist and search views keep compact grids.

### Shared Worker Lifecycle

- A private lifecycle base owns worker locks, background threads, stop events, running/stopping state, blocked reasons, duplicate-start protection, and standard start/stop responses.
- A typed run recorder owns the shared start, progress, terminal-state, and
  restart-interruption writes for metadata, playlist, history, placeholder, and
  plugin run tables. It validates each run family's fields and statuses without
  taking ownership of caller transaction boundaries.
- Metadata, playlist, history, placeholder recovery, plugin, and queue dispatcher workers retain their task-specific fetching, queue, logging, and completion behavior.
- Placeholder recovery attempts now persist run IDs, lifecycle and recovery outcomes, and dedicated run-linked logs just like the other external workers.
- Dispatcher site cadence, concurrency limits, queue priority, and per-request YouTube authentication checks are unchanged.

### Archivarix Backoff And Retry Controls

- Archivarix authentication and rate-limit failures persist a service block linked to the triggering placeholder run and queue item.
- The dispatcher preserves blocked placeholder tasks and continues eligible YouTube, playlist, and history work.
- Admin shows the block reason and local time and provides a deliberate retry action after credentials or quota state changes.
- Service restarts retain the block. A default-on Advanced setting automatically retries rate-limited Archivarix work after the next UTC-day boundary; authentication, proxy, timeout, and request failures still require deliberate recovery.

### Shared Collection Card Rendering

- `collection-card.js` owns the common playlist/channel card shell, media region, result-kind label, title/action row, and body container.
- Playlist and channel adapters retain their distinct metadata, visibility, status, owner/subscription, description, and source-link content.
- The shared builder preserves linked playlist thumbnails, playlist placeholders, and the existing channel thumbnail behavior.

## Removal Gate

Remove a vestigial candidate only when all are true:

- No active read/write path depends on it.
- No API payload or template path renders it.
- No current-schema behavior test protects it.
- No current local data operation needs it.

## Cleanup Review (2026-08-02)

The current-tree review found no release-blocking behavior regressions. The
verified removal, deduplication, performance, and module-organization work is
complete and live-validated. The remaining items require fixtures or product
decisions rather than another general cleanup pass.

### Completed Removal

- Removed the retired whole-library `fetch_app_data` read model and moved useful
  assertions to the active paginated and detail read models.
- Removed the superseded synchronous `fetch_provided_metadata` path; manual
  targets continue to use the persistent worker queue.
- Removed the unused `/api/admin/live-history/stop` endpoint; the Admin UI uses
  the unified queue stop endpoint.
- Removed unreferenced Python helpers, imports, constants, browser helpers, and
  stale tests that only preserved superseded APIs.
- Removed queue accessor parameters that were no longer read after queue
  persistence was unified.

### Completed Deduplication

- Centralized repeated worker log persistence and interrupted-run handling
  without changing the schema or worker-specific public functions.
- Reused common video playlist-link and identity hydration for History, Search,
  collection, and detail read models.
- Extracted the repeated Admin transaction/enqueue/dispatcher-start sequence
  and moved the affected POST routes into named handlers.

### Verification

- Passed Python compilation, all 249 local unit tests, and `git diff --check`.
- Restarted the configured service to a new healthy PID without clearing the
  persistent queue, then passed `/api/admin/status` and History search smoke
  checks.
- Verified Search, History, and Admin in a browser with no console errors;
  rapid layout changes persisted the final choice across a reload.

### Completed Follow-up Work (2026-08-02)

- Moved playlist, channel, and video collection filtering, facet aggregation,
  sorting, totals, and pagination into SQLite. The read models now hydrate only
  the requested page while preserving stable facet counts and deduplication.
- Split SQLite bootstrap and migrations from `core.py` into `database.py`, with
  shared history identity and UTC helpers in focused modules. Preserved the
  compatibility imports exposed by `core.py`.
- Split the former `test_core.py` monolith into focused core, schema, config,
  server, and worker suites with a shared temporary-database helper.
- Replaced the large template-source assertion block with DOM contract tests
  for controls, navigation, workstreams, advanced tabs, typed inputs, and
  unique IDs. Added development-only Ruff checks for critical Python errors.

### Follow-up Verification

- Passed Python compilation, all 255 local unit tests, Ruff, and
  `git diff --check`.
- Restarted the configured service from PID 4348 to healthy PID 9092 with the
  persistent queue stopped and empty before and after the restart.
- Passed live Admin status, History search, and paginated playlist, video, and
  channel API smoke checks.
- Verified Search, History, and Admin in a browser: Search rendered 100 results,
  History rendered 100 occurrences and 365 activity cells, Admin reported an
  idle empty queue, and the browser console had no errors.

### Completed Maintainability Recommendations (2026-08-02)

- Made versioned migrations the explicit upgrade policy across contributor
  guidance, project documentation, CLI help, and regression tests. Supported
  databases must upgrade in place without requiring a rebuild or re-import.
- Replaced wildcard coupling between `server.py`, `workers.py`, and `core.py`
  with explicit imports, and moved shared request pacing into
  `request_pacing.py`.
- Decomposed the HTTP request dispatcher into page, public API, Admin read,
  settings, cookie, and action handlers. Centralized repeated static-byte
  responses while preserving the compatibility handler surface.
- Extracted the browser and Admin JavaScript from their HTML templates into
  separately served assets. Added local Node syntax checks and behavior tests
  for theme persistence and shared video-card helpers.
- Centralized runtime configuration coercion in an ordered normalizer registry,
  including safe handling of malformed numeric Archivarix values.
- Expanded Ruff from fatal syntax checks to the full Pyflakes family and added
  a query-plan regression test proving playlist-scoped pagination uses the
  playlist item key index.

### Maintainability Verification

- Passed Python compilation, all 273 local tests (including the Node-backed
  browser asset tests), full configured Ruff checks, and `git diff --check`.
- Restarted the configured service from PID 33324 to healthy PID 26504 with the
  persistent queue stopped and empty before and after the restart.
- Passed live Admin status, History search, paginated playlist/video/channel,
  browser HTML, Admin HTML, and JavaScript asset smoke checks.
- Verified Search, History, and Admin in a browser: Search rendered 100 results,
  History rendered 100 occurrences and 371 activity cells, rapid layout changes
  persisted the final grid choice across reload, Admin showed an idle empty
  queue, and the browser console had no errors.

## Ranked Remaining Cleanup

### 1. Foreign Playlist Continuation Extraction

Foreign playlists can expose fewer rows than their reported count. Continue preserving the best nonzero scan and logging reported versus exposed counts. Investigate continuation behavior only with a concrete fixture and never synthesize unavailable rows from a count gap.

### 2. Saved Searches and History Layouts

The left-navigation library lists are named omni-search presets. Search returns one card per distinct video, playlist, or channel, while History remains a dedicated occurrence view with its activity heatmap, year navigation, date jumps, and chronological repeated watches. Do not add the History heatmap to Search unless a distinct-video activity design is defined.

- Extend the Search card-layout selector to History after the grid, detailed-list, and compact-list behaviors have settled. History must continue rendering every watch occurrence rather than canonical video rows.
- Evaluate user-defined saved searches on top of the preset specification. Preserve explicit entity kinds, source constraints, meta filters, default sort, layout, and optional server-side scopes.
- Playlist, video, and channel detail pages remain specialized because they carry entity chrome, tabs, playlist positions, membership state, and scoped actions that are not generic search-result behavior.

## Planned Features

- Add an Advanced Search mode that builds on the existing server-owned search
  model while keeping simple search as the default. Support composable entity,
  field, date-range, exact-phrase, exclusion, and facet criteria through the UI
  without requiring users to edit URL parameters directly; keep the resulting
  state shareable in the URL and compatible with future saved searches.
- **YTLLM:** Add a general natural-language interface for asking questions
  against YT Library data. Keep database access bounded and read-only through
  the plugin host rather than opening or writing the core database directly,
  and make answers traceable to the underlying library records or query
  criteria.
- Persist scheduled Update last-run and failure status across service restarts; the daily schedule and next-run status are available now.
- Make parent-child relationships in hierarchical filters more visually obvious, including enabled, disabled, selected, and partially selected states.
- Begin publishing versioned releases through GitHub with a defined versioning and release process.
- Implement plugin distribution and management using the proposal in `design.md`.
  The five implemented plugin repositories are published and listed in
  `plugins/catalog.json`; the catalog currently has no approved release artifacts.
  Establish wheel/sdist releases, licenses, artifact checksums and compatibility
  validation; resolve fresh-install config and dependency requirements; add a
  serialized maintenance controller and Advanced Admin install/update/remove
  controls. Preserve plugin data and editable development installs.
- Maintain a changelog for each released version that summarizes user-facing changes, fixes, schema impact, and operational notes.
- Package YT Library as a supported Docker image and add a documented Docker
  Compose and CI workflow covering first-run initialization, schema upgrades,
  persistent config/database/cookie/thumbnail mounts, health checks, and
  versioned image publishing alongside GitHub releases.
- Add an optional Download plugin for preserving downloaded videos and clips
  through the versioned plugin host, with independent video and clip tracking
  and no automatic media eviction.
- YT Comments is implemented as a separate optional plugin. Its My Activity
  discovery, participating-thread capture, search cards, and video decorator
  remain plugin-owned; core supplies bounded transport and queue services.
- Beginning with the 1.0 release, preserve or redirect existing browser URLs when route or parameter formatting changes. Pre-1.0 URLs do not require compatibility handling.

## Deferred Decisions

- Detect views that the user removed from YouTube watch history, but defer the
  implementation until a review-first workflow is designed and validated. The
  current full-history reconciliation can already remove missing YouTube-only
  events and detach YouTube ordering from Takeout/My Activity-backed events;
  replace that implicit destructive behavior with an explicit audit trail.
  Begin with non-destructive, video-scoped candidates recording the affected
  occurrence count, first/last observation, confirming run IDs, and current
  video playability. Only create candidates after a successful complete scan or
  a fully validated overlap range; partial, interrupted, unauthenticated, or
  structurally suspicious feeds must never produce deletion evidence.
  Playable videos whose every previously observed occurrence disappears are the
  strongest signal of a user deletion. Unavailable/private/access-gated videos,
  partially missing occurrences, and unusually large disappearance sets remain
  review-only. Require repeated successful confirmation before any automatic
  action. Approved candidates should soft-delete all prior occurrences for that
  video from normal History, counts, and heatmaps while retaining reversible
  evidence and preventing old Takeout/My Activity imports from resurrecting
  them; a genuinely new watch should remain visible. Surface candidate review
  and Keep/Remove actions in Advanced Admin, then consider a default-off
  `Automatically remove confirmed deleted views` option only after real-world
  candidate results establish that the signal is reliable.
- The PocketTube plugin projects only playlist and channel IDs already present in YT Library. Consider a generic discovery/import workflow for unmatched plugin references only if the proven read-only integration leaves a real need; do not fabricate canonical rows from group membership alone.
- Previous-database queue backfill remains a one-off recovery operation. Promote it to a supported command only if the workflow repeats and can define source-version and conflict rules.
- `watch_resume_seconds` remains less trustworthy than the observed progress percentage. Do not expand resume-time behavior until additional examples explain the mismatch.
- Watch-progress history needs an evidence survey before expanding the model. `history_events` already stores progress observed on individual live-history occurrences, while metadata scans maintain the latest canonical percentage on `videos`; there is no durable chronology that associates successive metadata observations with distinct rewatches. Survey progress coverage by watch date and observation date to estimate when YouTube stops exposing completion data. If repeated watches produce lower percentages alongside corresponding new history occurrences, evaluate retaining those observations per occurrence instead of replacing the prior canonical completion.
- Playlist Newest sorting now uses the same observed `playlists.last_changed_at`
  value displayed as **Last updated**, independent of polling cadence. Exact
  per-item chronology remains unfinished: capture a reliable membership-added
  or first-seen timestamp on `playlist_items` if future features need to say
  when an individual video joined a playlist. YouTube's separate displayed
  update date is now captured in `playlists.youtube_updated_date` and is
  temporarily shown as **YT Last updated**. Survey its coverage and conflicts
  across the library before deciding whether it should replace the observed
  value for display and sorting, then remove or promote the temporary label.
- Audit check-versus-change semantics app-wide. A scan, poll, fetch, or row write
  must update its explicit checked/observed bookkeeping without automatically
  becoming a user-visible **updated** event. Inventory generic `updated_at`
  fields and labels across videos, playlists, channels, history, plugins, and
  admin status surfaces; retain distinct change timestamps only where a durable
  before/after comparison or source timestamp supports them.
- Foreign playlist continuation work remains fixture-driven; current best-nonzero preservation is the safe behavior.

## Suggested Order

1. Investigate foreign playlist continuation extraction when a reproducible fixture is available.
2. Define the History layout extension and the saved-search preset specification.
3. Persist scheduled Update last-run and failure status across service restarts.
4. Improve the visual hierarchy and partial-selection states of nested filters.
