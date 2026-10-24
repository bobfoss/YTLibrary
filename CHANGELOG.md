# Changelog

## 1.1.0 — 2026-10-23

### Playlist membership and dates

- Reconcile playlist memberships in place, preserving dates through scans and
  reorders; protect ambiguous duplicate and incomplete observations.
- Add membership First seen, added-date provenance, and stable YouTube occurrence
  IDs. First seen is recorded only for new members after a complete baseline,
  without backfilling observation times onto existing undated members.
- Enrich missing added dates through optional API access, Music web evidence,
  and Takeout. The Advanced API option defaults off; broad account collection
  remains an explicit CLI action.
- Display Date added or First seen on playlist member cards and use that same
  date for chronological member sorting. Preserve date-only precision and keep
  membership dates out of unrelated video views and compact cards.
- Retain occurrence IDs from verified API playlist-item identifiers, with guarded
  matching that avoids transferring dates between ambiguous duplicate entries.
- Track independent 24-hour API and Music web date-collection cooldowns. Manual
  playlist-member scans bypass them; skips log the source and next eligible time.
  Cancellation clears only the interrupted source's timer. Header-only scans
  remain excluded from member-date collection.

### Search and plugin reliability

- Isolate optional plugin search failures so ordinary searches can return the
  available results. Clearly mark partial results and unknown facet counts,
  preserve error explanations and recovery controls, and avoid caching partial
  responses or showing stale cards after a blocked search.
- Keep explicit include/exclude filters strict when a required source is
  unavailable rather than silently broadening results.
- Support persisted plugin search options across matching, result hydration,
  URLs, pagination, and caches.
- Add a shared authenticated YouTube account-identity lookup for plugins. Cache
  invalidation follows cookie content/path and proxy changes, including changes
  during verification; failed lookups do not reuse stale identity.

### Interface and maintenance

- Link channel detail titles directly to YouTube while preserving internal
  navigation from channel list cards.
- Show OAuth readiness beside the cookie statuses when the API checkbox is
  selected, with access-token expiry in the configured display timezone.
  Cookie timestamps are labeled `checked:` and OAuth expiry is labeled
  `expires:`. Status polling is a local credential check, not live Google
  verification, and does not refresh tokens or make API requests. API setting
  changes still require Save.
- Update repository links and catalog refresh for the renamed GitHub repositories
  while retaining immutable plugin release URLs, artifact hashes, and package IDs.
- Document maintainer GitHub/OAuth access and scoped comment-discovery refreshes.

### Compatibility and upgrading

- Application version is **1.1.0**; Python and browser plugin APIs remain **2**.
- Database schema advances from **36 to 38**. Migrations add membership date,
  provenance, occurrence-identity, scan-completeness, and per-source cooldown
  bookkeeping while preserving existing memberships and dates. No rebuild or
  re-import is required. The version bump itself adds no further migration.
- Back up the configured database before upgrading and restart the service after
  updating. On Windows, use `scripts/service.ps1 restart` to preserve queue intent.
  Existing settings and plugin activation are retained; API collection remains
  opt-in and disabled by default for other installations.
- Maintainer-only date backfills, OAuth credentials, and runtime data are not
  distributed with this release. Plugin repositories are versioned separately.

## 1.0.0 — 2026-10-03

First versioned YT Library release, establishing the current application as the
1.0 baseline. Future browser URL changes preserve existing links or provide
redirects; retired pre-1.0 routes are not restored retroactively.

### Included in the baseline

- Local-first, paginated browsing and search across videos, clips, playlists,
  channels, and watch-history occurrences, with notes, tags, and history heatmaps.
- Persistent metadata/recovery queues, scheduled updates, adaptive live-stream
  refreshes, and source-faithful metadata decorators and playlist change dates.
- Optional, independently maintained plugins with versioned host APIs and
  Windows Advanced Admin installation, updates, activation, and code-only
  removal. Editable installations are protected and plugin data is retained.
- Serialized Windows service control, optional persistent service supervision,
  restart diagnostics, and preservation of queue intent.
- GPL-3.0-or-later licensing for YTL and the five published plugins.

### Version identification and compatibility

- `yt_library.__version__` is the canonical application version: `1.0.0`.
  `yt_library_manager.py --version`, browser/Admin headers, and the status
  API's `service.version` report it without deriving it from the database.
- Database schema remains **36**; Python and browser plugin APIs remain **2**.
  This version-label change requires no data migration or re-import.
- Restart the service after updating so the running UI/API reflects the new
  application version. Existing configuration and plugin activation are retained.
