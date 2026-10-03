# Changelog

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
