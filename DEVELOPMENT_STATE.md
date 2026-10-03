# YT Library Development State

Last consolidated: 2026-08-26. This handoff summarizes the current checkout.
Recheck the live service, queue, schema, and test count before relying on
snapshot values.

## How To Use This Document

This is the fast reorientation guide for a new development chat. It preserves
durable engineering knowledge without making an old chat transcript part of
the product source.

- `AGENTS.md` is authoritative for the working agreement and verification
  requirements.
- `TODO.md` is authoritative for unfinished work and deferred decisions.
- `design.md` is authoritative for the current architecture and plugin
  contract.
- `README.md` is authoritative for setup and user-facing operation.
- This file summarizes the decisions and current implementation that connect
  those sources. Update it when a later change materially alters the handoff.

Do not add personal-library query results, cookie contents, local database
facts, transient PIDs, or other runtime-only data here.

## Product And Data Model

- YT Library is a local-first personal-library manager, not primarily a
  downloader or media server. Metadata, account organization, history,
  reconciliation, evidence, and search remain the product center.
- SQLite stores one best-known current state rather than a metadata revision
  archive. Failed or unavailable responses must not erase the last useful
  identity.
- `videos` owns canonical video metadata. `playlist_items` owns membership and
  unavailable-slot evidence. `history_events` owns repeated watch occurrences.
  Equal video IDs do not collapse history occurrences.
- Exact instants are UTC with `Z`. Date-only history observations remain
  date-only and retain ordinal order; the UI must not fabricate a midnight
  timestamp.
- YouTube direct pages and authenticated account surfaces are authoritative for
  current YouTube state. Takeout is strong historical evidence. Archivarix is
  independent recovery evidence and must not redefine canonical YouTube
  availability.
- Metadata fetches use direct IDs or URLs. YouTube search is not a metadata
  fallback.
- Nullable feature fields are intentional three-way observations. Unknown,
  observed absence, and observed presence are distinct states.
- Playlist rows may support an inferred public availability, but playability
  changes require an explicit positive or negative signal. Missing negative
  evidence is not proof of `is_playable = 1`.
- `playlists.last_changed_at` is the latest defensible playlist-change evidence,
  not the latest scan or row write. Exact playlist creation and item-added
  timestamps can advance it; after an authoritative baseline, a detected
  membership, order, collaborator, or meaningful metadata difference advances
  it at observation time. Failed, first-baseline, and no-op scans do not.
- Authenticated playlist discovery never advances `last_changed_at`. Missing
  discovery counts remain unknown and cannot overwrite authoritative scan
  counts; a differing reported count queues a confirming membership scan.
- `playlists.youtube_updated_date` separately stores YouTube's displayed
  date-only playlist update evidence. Exact and relative authenticated web
  labels are normalized to `YYYY-MM-DD` in the configured display timezone;
  the raw label is transient and no timestamp is invented. A relative label may
  correct the stored date backward by one day after its post-midnight wording
  advances from `today` to `yesterday` (or another relative-day increment),
  while larger source disagreements retain the later date. The field does not
  advance `last_changed_at` or drive sorting; it is temporarily displayed as a
  separate **YT Last updated** value on shared playlist cards and detail pages
  while its coverage is surveyed.
- Explicit playlist metadata-only queue jobs stop after authenticated header
  metadata. They do not enumerate or rewrite member videos, update the
  membership scan record, enqueue video metadata or placeholder recovery, or
  advance `last_changed_at`; only `metadata_checked_at` and generic persistence
  bookkeeping record the check.
- Supported databases upgrade through ordered migrations. The fresh schema and
  every supported upgrade path must describe the same current model. The
  schema version is 36, adding nullable `videos.auto_dubbed` observations.

## Browser And Search Model

- Search is the default library surface. `/search` is broad omni-search;
  `/videos`, `/clips`, `/playlists`, and `/channels` provide canonical scoped
  contexts.
- History is a separate occurrence view with repeated watches, its activity
  heatmap, and chronological paging. It is not another canonical-video search
  preset.
- Playlist and channel detail searches reuse the scoped native search model.
  An empty channel query still represents that channel's video universe,
  including its title, filters, and complete scoped counts.
- SQLite owns filtering, stable facet counts, sorting, totals, and pagination.
  The browser hydrates only the requested page and must preserve existing
  controls and results while a replacement response loads.
- Search and detail request generations reject stale work. A filter refresh
  keeps the current tree rendered so parent/child selection does not flicker or
  temporarily disappear.
- Global and channel History share one workflow for cards, activity data,
  paging, date changes, scroll synchronization, and rollback after failed or
  superseded transitions.
- Card DOM is shared. Page-specific adapters and CSS may change layout, but
  should not fork core identity, metadata, annotation, or decorator behavior.
- Playlist cards alone show **Last updated**, sourced from
  `playlists.last_changed_at`; video, Clip, channel, and occurrence cards do not
  reinterpret their persistence `updated_at` values as user-facing changes.
- Playlist **Newest** and **Oldest** search ordering uses that same
  `last_changed_at` value. It does not substitute newest-member upload time,
  scan time, or persistence bookkeeping; unknown dates sort last in **Newest**.
- Native decorators precede the ordered plugin contribution slot. Unless a
  distinct meaning requires otherwise, new decorators reuse the established
  typography, sizing, spacing, and muted metadata color.
- Shared video cards and details show **Auto-dubbed** from explicit YouTube
  watch-page disclosures, primary badges, or player `audioTrack.isAutoDubbed`
  evidence. This is independent of **Made with AI**, and inconclusive scans
  preserve the previous observation. Existing videos remain unknown until
  refreshed; no language-based inference or bulk backfill is applied.

## Notes And Tags

- User-authored notes and tags are core personal-library data. External or
  machine-generated labels remain plugin-owned unless the user explicitly
  converts them into a personal tag.
- Notes and tags attach to canonical videos, clips, playlists, and channels.
  Video annotations attach by video ID, never to individual history
  occurrences.
- Each entity has one note. Tags are normalized reusable rows linked through
  explicit per-entity mapping tables. Notes use a rebuildable FTS5 projection;
  tags remain relational.
- Notes and tags participate in search. Note presence has With notes and
  Without notes filters; tags do not create an unbounded facet tree.
- Cards share annotation hydration. Compact cards retain the concise annotation
  treatment, while grid and detailed cards show tags as well.
- Unsaved note edits survive failed saves and trigger discard protection for
  internal navigation, Back/Forward, reload, tab close, and window close.

## Optional Plugin Boundary

- The five implemented plugins have public repositories under `bobfoss` on
  GitHub, listed in `plugins/catalog.json`; all five and YTL are licensed
  GPL-3.0-or-later. The catalog still has no approved installable releases.
  `scripts/build_plugin_release.py`, `smoke_plugin_wheel.py`, and
  `plugin_packages.py` provide clean-commit wheel/sdist candidates, fresh-config
  wheel startup checks, catalog/artifact validation, protected editable inventory,
  and non-mutating dependency preflight. Wheels carry plugin-owned
  `ytl-plugin.json` API/feature/config-template declarations. Existing packages
  are pinned during resolution; development installs cannot be replaced.
  These tools do not change live installations, activation, or service state. Dependency-wheel
  staging, publication/approval, serialized maintenance, durable operations,
  and Advanced Admin package controls remain planned in `design.md`.
- Plugins are optional, separately packaged repositories with their own data,
  schema, migrations, configuration, caches, source artifacts, and tests.
- Core must not import a plugin package, depend on its database, add its domain
  vocabulary, or preserve plugin behavior when it is disabled or uninstalled.
- Integration uses stable YouTube IDs and bounded, read-only host projections.
  Plugins never open or attach the YT Library database.
- YT Library owns activation, compatibility checks, common queue rows, run and
  log persistence, lifecycle dispatch, generic Admin placement, request
  policy, and failure containment.
- A host extension must be domain-neutral and reusable by an unrelated plugin.
  Plugin-specific policy, labels, facets, matching rules, views, and actions
  remain in the plugin.
- The Python plugin API and browser API are both version 2 at this snapshot.
  Breaking changes bump the owning version; additive host services are
  feature-negotiated.
- Plugins are discovered through `yt_library.plugins` entry points but load
  only when explicitly enabled. Missing, disabled, incompatible, or failing
  plugins must not prevent core startup or rendering.
- YT Comments uses additive host services for authenticated My Activity reads,
  highlighted YouTube watch sessions and `next` continuations, same-plugin
  follow-up planning, and explicit video discovery. Discovery queues metadata
  only for newly added IDs and never creates watch occurrences. The plugin owns
  account participation, thread storage, its schema, search, and presentation.
  Comments is a Videos facet with comments/no comments children and persisted
  choices; the additive `video_facet_result_cards_v1` feature lets it retain
  separate thread result cards when the comments child is enabled.
  The generic `unified_search_cards_v1` contract integrates query-matching plugin
  cards into native sorting and pagination. Providers return lightweight video-bound
  descriptors, then hydrate only the selected page. Video facets apply before
  pagination; dynamic Most liked is offered only when matching plugin cards exist.
- The additive `browser_collections_v1` contract lets a plugin declare
  `browser_collection = {"label": "..."}` and a browser `collection.fetch`
  provider. Its `/<plugin-id>` shell route and Meta sidebar link appear only
  while enabled. Meta sits between Videos and Playlists. Collection pages reuse
  search cards, pagination, query URLs, and the main sort control, with a saved
  `meta` sort context; they bypass native/search-field filters and hide those
  controls. YT Comments uses `/comments` to browse participating threads even
  with a blank query. Blank global search still omits comment cards. All
  collection data access remains in the plugin; no schema migration is needed.
- Plugin search can enrich canonical videos and clips, project bounded virtual
  videos, decorate native entity cards, add scoped channel-video tabs, and
  plan work through the host contract. It must not couple core queries or
  templates to the plugin domain.

## Service, Configuration, And Queue

- Run the service from the project `.venv`; do not infer the active Python from
  `PATH` or a standalone `yt-dlp.exe`.
- Use `scripts\service.ps1` for status, start, restart, and stop. It serializes
  mutations across threads and Windows sessions, waits for listener and API
  readiness, and owns stable control and stream logs. It launches the service
  hidden when the optional SCM service is absent and controls that service when
  installed; callers do not invoke SCM directly.
- `scripts\windows-service.ps1` provides opt-in elevated installation under the
  current user's Windows account, automatic delayed startup, SCM host recovery,
  credential refresh, and reversible removal back to direct mode. The Python
  service host supervises the project venv process; the existing controller
  retains queue semantics and rich contention feedback. In installed mode,
  controller restarts replace the supervised child without stopping the SCM
  host; process ancestry is the non-elevated ownership fallback when Windows
  hides cross-session command lines.
- The current stdout/stderr paths remain stable. Before each child launch, the
  previous run and manifest are moved to `.codex\service-logs\archive`, bounded
  to 20 runs and 250 MiB while retaining at least the newest run.
- A restart preserves the dispatcher state only when the queue was running
  before the restart. Always record the old and new service PID and verify the
  queue state after readiness.
- A verified restart commonly spends most of its wall time waiting for the
  application to become healthy. Do not treat process launch alone as service
  success.
- Runtime settings live in `yt_library.config.json`, not SQLite. The server
  clock shown in Admin is calibrated from server UTC and runs locally with a
  monotonic clock between periodic status synchronizations.
- The persistent dispatcher rereads SQLite before every launch. Priority
  changes and newly queued jobs can affect the remaining run without rebuilding
  an in-memory batch. When account, playlist, or history work reaches the front
  of the eligible queue, active workers finish before it starts; lower-priority
  jobs do not refill slots and indefinitely delay this exclusive work.
- Automatic broadcast rechecks now use configurable age/recent-watch cadence
  and deterministic staggered hour slots: hourly for recent watches/new streams
  and imminent upcoming broadcasts, six-hourly for intermediate streams or
  distant upcoming broadcasts, daily for long-running streams. New history
  occurrences and manual refresh retain full metadata work. Unavailable videos
  cannot remain active in canonical writes or browser/plugin projections; no
  end timestamp is inferred. No Data API status polling was integrated.
- Update, Initialize, and Rebuild share the declarative queue planner. Update
  promotes its complete selected batch ahead of older backlog while retaining
  internal ordering. Rebuild replaces only regenerable core plan rows and
  preserves manual, plugin, recovery, Clip, and future non-plan work.
- Cookie uploads are validated for the selected service and saved atomically.
  Authentication status is durable current state; transition logging records
  entry into a non-valid state without repeating identical warnings. A service
  block caused by timeout, proxy, or quota evidence is separate from cookie
  validity.
- Inconclusive YouTube metadata checks do not automatically requeue videos with
  completed Archivarix recovery evidence; explicit manual recovery remains
  available. A later transient Archivarix failure updates the latest search
  outcome but preserves prior recovered status, channel, capture, and media
  evidence.
- Admin polling, queue/log streams, visibility changes, and wake refreshes are
  designed to tolerate a frozen or sleeping browser page. A wake refresh may
  replace stale work rather than waiting indefinitely for a pre-sleep request.

## Verified Implementation Snapshot

The following areas were implemented and committed before this consolidation:

- Core tags and notes, FTS search, annotations on all card layouts, collapsed
  empty editors, and unsaved-change protection (`9aeba1b`, `7d5bac6`,
  `58dc98f`, `44e7de8`).
- Versioned optional-plugin host services, native-before-plugin ordering,
  scoped plugin search, and manual planning context (`fb378df`, `d2e0c7f`,
  `bfaa264`, `5fef3db`).
- Sleep/wake-safe Admin behavior, serialized restart recovery, server clock,
  uptime presentation, and service-aware cookie validation (`7e6d612`,
  `ae1b3fd`, `8273dba`, `5fe60b7`, `2f76da1`).
- Scoped detail search, channel empty-query facets, stable filter refreshes,
  and restored History heatmap scrolling (`d20f54e`, `af97394`, `9b679e9`,
  `6392d5e`).
- Explicit playlist playability observations plus repair of unsupported
  inferred-positive rows (`6839161`).
- YouTube AI-disclosure extraction with a three-way stored state, native
  decorator, filter, and shared decorator styling (`769f500`, `bfc23b0`). The
  extractor distinguishes YouTube's separate **Auto-dubbed** disclosure from
  **Made with AI** rather than treating the shared `How this was made` container
  as the category signal.
- Advanced Search is planned and documented, not implemented (`16656e7`).

The latest implementation milestone reported 509 Python tests and 109 browser
asset tests passing, plus Ruff, API smoke checks, and live browser QA. Those
counts are evidence for that milestone, not permanent expectations.

## Active And Deferred Work

Use `TODO.md` for the complete and current list. The main outstanding themes at
this snapshot are:

1. Investigate foreign-playlist continuation only with a reproducible fixture;
   preserve the best nonzero scan and never synthesize hidden rows from a count
   discrepancy alone.
2. Design and implement Advanced Search on the existing server-owned search
   model while preserving simple search, shareable URLs, and future saved
   searches.
3. Persist scheduled Update last-run and failure state across restarts.
4. Improve hierarchy and partial-selection clarity in nested filters.
5. Define release/changelog practice and supported Docker packaging.
6. Keep Download and Comments as separately packaged optional plugins; Comments
   now has its own implementation in the sibling YT Comments repository.
7. Keep deleted-view reconciliation review-first and reversible; incomplete or
   suspicious history scans must never create deletion evidence.

## New Development Chat Checklist

1. Read `AGENTS.md`, `TODO.md`, this file, and the relevant sections of
   `design.md` before proposing changes.
2. Verify the exact cwd, branch, `git status`, current schema, and recent log.
   Preserve unrelated tracked and untracked work.
3. Run `scripts\service.ps1 status` and record the listener, PID, queue state,
   queue count, and any block before a runtime change.
4. Read the owning implementation and tests. Treat this handoff as orientation,
   not as evidence that current code still matches an older decision.
5. Keep implementation in the owning module, add focused regressions, and run
   the full repository checks required by `AGENTS.md`.
6. For UI work, wait for rendered cards and counts, verify the installed
   browser behavior, reload persistence, rapid-input behavior, and console
   state.
7. Restart only when the changed surface requires it, then verify API readiness
   and restore the prior queue-running state.
8. Commit each coherent validated milestone with a substantive body. Push only
   when explicitly requested.

## Handoff Provenance

This file was consolidated from the former long-running **Current YTL
development** chat, the current repository documentation and implementation,
Git history, and live service status. The predecessor chat remains historical
evidence, but new work should rely on the repository sources above so task
history size does not become a development dependency.
