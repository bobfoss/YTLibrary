# YT Library Manager Design Notes

For the maintained implementation snapshot and new-chat handoff, see
`DEVELOPMENT_STATE.md`. This file remains authoritative for architecture and
the optional-plugin contract.

## Product Direction

YT Library Manager is a local-first tool for understanding and managing a personal YouTube library. It mirrors playlist structure, enriches videos with metadata and cached images, reconciles hidden or deleted videos from Takeout and Archivarix evidence, and exposes searchable history and playlist views through a small web UI.

The project intentionally favors YouTube web-interface data where practical, using cookies from the local project directory. The YouTube API or third-party libraries should be fallback tools when the web surface cannot provide the needed data.

The core product goal is not to become a downloader or media server. It is a personal library-management layer that makes YouTube account state easier to inspect, search, preserve, and reconcile. Downloading may be adjacent later, but the current center of gravity is metadata, organization, history, and evidence.

This project is still in an early alpha stage. Prefer the design that clarifies the domain and future maintenance, even when that means a large schema, API, UI, or architecture change. Avoid preserving awkward legacy shapes just because they already exist; source evidence and personal data should be protected, but the application structure is still allowed to move.

## System Shape

The app is intentionally compact but no longer single-file. `yt_library_manager.py` is the compatibility CLI shim, while the application code is split across a small Python package:

- `yt_library/cli.py` defines CLI commands and keeps existing command names stable.
- `yt_library/database.py` owns SQLite connection, schema bootstrap, and
  versioned migrations. `yt_library/core.py` owns importers, parsers, metadata
  fetchers, queue helpers, and reconciliation logic.
- `yt_library/server.py` owns HTTP routing and local API endpoints.
- `yt_library/workers.py` owns in-process worker orchestration.
- `yt_library/queries.py` owns read models for browser and history views.
- `yt_library/schema.sql` is the canonical SQLite schema for fresh local databases.
- `yt_library/templates/` contains the browser and admin HTML plus shared
  browser-side modules for timezone, card rendering, History workflow state,
  and Admin request transport.

On Windows, service lifecycle has two layers. `scripts/service.ps1` is the
authoritative controller for queue intent, serialized start/stop/restart,
process ownership checks, readiness, recovery state, and caller-visible
contention. By default it launches the project venv as a hidden background
process. An opt-in, per-repository SCM registration can instead launch the
standard-library `scripts/windows_service_host.py` supervisor under the user's
Windows account. SCM provides boot startup and session-independent lifetime;
the host captures and archives child streams, retries failed children with
bounded backoff, and converts in-app restart requests into supervised child
replacement. The controller uses that same child-replacement path for installed
mode restarts while retaining its lock, queue intent, recovery record, process
ownership checks, readiness verification, and caller-visible result. This layer
does not replace or bypass the controller's operational contract.

Primary surfaces:

- `/` normalizes to `/search`.
- `/search` is the unscoped omni-search across videos, clips, playlists, and channels.
- `/videos`, `/clips`, `/playlists`, and `/channels` are canonical category-scoped search views. Query parameters refine text, fields, facets, sort, and pagination; they do not redefine the path's entity scope.
- `/history` is the separate occurrence view. It preserves repeated watches, occurrence ordering, pagination, and the daily heatmap instead of collapsing videos into canonical search results.
- `/videos/{id}`, `/clips/{id}`, `/playlists/{id}`, and `/channels/{id}` are detail views. Playlist details reuse the video facet model for their member list, while channel details retain their playlist and History tabs.
- `/admin`: status dashboard and worker control plane for metadata, playlist scans, placeholder recovery, and history.

Before 1.0, replaced hash routes and obsolete named-view URLs have no compatibility aliases. After 1.0, URL changes should preserve or deliberately migrate public links.

Omni-search uses `/api/search` as its single read model. The server applies title/description and source filters, folds playlist and history evidence into one canonical video result, includes unresolved unavailable memberships, globally sorts and counts videos, clips, playlists, and channels, and only then returns the requested page. The browser does not merge a separate history result set.

Clip date sorting uses an exact `clipped_at` value when available. Otherwise it
derives a coarse sortable date from YouTube's relative `clipped_at_text` and the
time that label was first observed. `youtube_feed_ordinal` preserves the Clips
feed's authoritative order within those coarse date buckets; manually scanned
clips that are not present in the account feed have no feed ordinal.

The main browser is also view-driven. `/api/bootstrap` returns only navigation structure and aggregate counts; playlist, video, channel, and detail endpoints fetch the current view's rows from SQLite with server-side filtering, sorting, and pagination. The browser caches completed request keys for navigation within the session and preserves the currently rendered view while a new page is loading. It does not download a whole-library metadata snapshot during startup.

### Browser routes and search context

The browser path is the authoritative search context. `/search` can enable or disable any result category. A scoped path always searches only its category, even if query parameters are copied or edited. Default and path-implied values should be omitted from generated URLs.

The left sidebar separates search controls from navigation:

- **Search in** selects searchable fields such as titles, descriptions, notes,
  tags, and plugin-provided text fields.
- `/search` renders one uninterrupted facet tree above navigation. Videos,
  Clips, Playlists, Channels, and plugin result kinds are parent selectors in
  this tree, not navigation links. Their native child facets are video
  availability, reactions, completion, playlist membership, uploader category,
  note presence, and plugin video facets; Clip ownership and note presence;
  playlist availability, ownership, and note presence; and channel subscription,
  status, and note presence. The tree does not insert navigation
  section headings or dividers between result kinds.
- Navigation follows the complete broad-search facet tree and is grouped under
  Videos, Playlists, and Channels. Category and group names are real links.
  Plain clicks use in-app navigation, while browser affordances such as
  status-bar targets and open-in-new-tab remain available.

On a scoped category page, the path supplies the result kind. The sidebar omits
the redundant kind parent and mounts only that kind's facet groups immediately
below its active category link. The search placeholder names the scope, such as
**Search videos**, **Search clips**, **Search playlists**, or **Search
channels**. Playlist detail is a scoped video search: it reuses the same video
facets beneath Videos, removes inapplicable playlist-membership filtering, and
uses **Search this playlist**. This shared mount keeps broad and scoped filters
on one rendering, event, and query path.

History hides **Search in** and all facets while retaining the search box and
the full category navigation. Entering a query returns to `/search`. Detail
navigation retains the last omni-search URL in session state so returning to
Search restores the prior query, counts, filters, sort, and page.

The old named shortcuts such as Liked, Playlisted, Subscribed, and Terminated
are not separate views or navigation entries. Their behavior is expressed by
the applicable category facets. Playlist and channel group trees remain real
scoped navigation because they carry user-defined membership context.

Facet trees use disclosure chevrons and parent/child checkboxes. Their expanded
state is saved in `yt_library.config.json`. Before server counts arrive, render
only checked kind and facet parents; do not synthesize leaf rows or reserve
empty child space. Populate leaves from the response, and when **Hide empty
filters** is enabled omit zero-count leaves from omni-search and playlist
filters. Facet counts describe the current search universe and remain stable
while a leaf is toggled. Filter changes preserve the current results and
controls while loading, then apply disabled/dimmed parent state only after the
replacement response arrives.

Grid, compact, and detailed card selectors are available on supported list
views, with layout saved separately for Search, playlist detail, History,
channel playlists, and channel History. Defaults are grid for Search, playlist
detail, and channel playlists; compact for History; and detailed for channel
History. Sort choices and page size are also saved as user preferences rather
than encoded as permanent layout state in every URL. Native video, clip,
playlist, and channel IDs are displayed on detailed cards only; grid and
compact cards favor descriptive metadata.

Global History and channel-scoped History use the same browser workflow for
page and activity loading, occurrence-card rendering, pagination, adjacent-page
prefetch, and stale-generation rejection. `history-workflow.js` also owns the
transaction boundary for heatmap year, Today, and Sync changes so a failed or
superseded transition restores the complete prior date, page, and sync state.
While global History is selected, the sidebar search box filters History's
occurrence rows in place and persists the query on `/history`; it does not
activate canonical omni-search. Its Titles and Descriptions field selectors are
also persisted on the History URL. The heatmap applies the same query and field
selection so its daily counts and page offsets remain aligned with the filtered
occurrence list.

Admin actions that send URL-parameter POST requests share one JSON/error
transport. Callers continue to own their distinct user-interface policy:
ordinary actions refresh status and schedule follow-up polling, queue stop
updates its controls before the request, and restart-sensitive settings wait
for a replacement service. Cookie replacement remains a separate raw-text
request because its body and security handling are meaningfully different.

SQLite is the source of truth for local state. Cached thumbnails and avatars are derived local assets. Cookie files, Takeout zips, databases, logs, and thumbnail folders are private runtime data and should remain uncommitted.

## Plugin Architecture And Developer Contract

YT Library supports optional, separately packaged Python plugins. The current
host contract is Python plugin API version 2. Browser extensions use a separate
browser API version 2. Both versions are exact compatibility checks, not version
ranges.

A breaking Python contract change must bump `PLUGIN_API_VERSION`; a breaking
browser registration or host-object change must independently bump
`window.YTLibraryBrowserPlugins.apiVersion`. Update host contract tests and
reference plugins in the same development slice as either change.

Additive Python services are feature-negotiated. A plugin may declare
`required_host_features`; the host refuses to activate it when any named
feature is unavailable. The current feature set includes
`library_video_lookup_v1`, `plugin_json_mutations_v1`,
`youtube_watch_session_v1`, and `youtube_ytdlp_v1`. Activated plugins receive
the immutable set as `context.host_features`.

This section is the handoff for designing and implementing another plugin. The
authoritative implementation is `yt_library/plugins.py`; HTTP integration lives
in `yt_library/server.py`, queue dispatch lives in `yt_library/workers.py`, and
the browser host lives in `yt_library/templates/index.js`. YT Subtitles is the
reference implementation in the sibling `YT Subtitles` repository, especially
`yt_subtitles/plugin.py`, `yt_subtitles/browser.js`, and `pyproject.toml`.

### Architectural boundary

The plugin system must preserve these properties:

- YT Library has no import, package, schema, database, or startup dependency on
  a plugin. Removing the plugin package must leave normal YTL behavior intact.
- A plugin is its own project and owns its domain data, schema, migrations,
  configuration, caches, and source artifacts. It must not add domain tables to
  the YTL database or ask YTL to migrate or write its database.
- YTL owns only host operational state: plugin activation, common queue rows,
  worker runs, worker logs, lifecycle dispatch, and generic UI placement.
- Integration uses stable identifiers and bounded data returned through the
  contract. Video-oriented plugins join by YouTube video ID; playlist-oriented
  plugins join by YouTube playlist ID.
  Do not create cross-database foreign keys or attach a plugin database to YTL.
- YTL core code must remain domain-neutral. Plugin-specific terms, queries,
  markup, and styles belong in the plugin. If a new host hook is required, add
  a generic capability that another plugin could also use.
- Installing and enabling are separate. Installation only makes an entry point
  discoverable; activation requires an explicit YTL configuration entry.
- Plugin discovery, startup, status, request, planning, execution, asset, and
  shutdown failures are contained. A broken optional plugin must not prevent
  YTL from starting or rendering its core views.

### Generic host rule

Every concept that exists only because a plugin is installed is plugin-owned.
This includes domain labels, default or catch-all groups, matching and
classification rules, facets, badges, views, actions, API vocabulary, and
configuration. The plugin declares those concepts through the versioned
contract; YTL may validate, namespace, place, persist generic UI state for, and
resolve them against canonical YTL identifiers.

Core YTL must not create or preserve plugin semantics in native importers,
discovery paths, tables, rows, configuration keys, routes, queries, templates,
or styles. Disabling or uninstalling a plugin must remove its domain behavior
and presentation without a plugin-specific condition in core and without
requiring core data to stand in for the absent plugin.

A host change for a plugin feature is valid only when it is expressed entirely
in domain-neutral terms and is reusable by an unrelated plugin. The contract
defines generic inputs, bounds, validation, containment, and outputs. The
plugin supplies the domain name, labels, policy, and data. If a proposed YTL
change cannot be specified and tested without naming the requesting plugin or
its domain, implement it in the plugin instead.

Host contract tests use generic fake plugins and prove behavior without loading
a real plugin package. Plugin repositories separately test their declarations
against the public contract. YTL must not import a plugin implementation merely
to test or support it.

Removing historical coupling may require a bounded, versioned YTL migration
that deletes obsolete core-owned plugin artifacts while preserving canonical
library data and unrelated user state. Such a migration is compatibility
cleanup, not permission to retain plugin-specific runtime behavior. After the
migration, no active YTL path may recreate or depend on those artifacts.

The plugin object is loaded into the YTL service process and may be called from
HTTP request threads and background worker threads. Its status, definitions,
filters, projections, and caches must therefore be inexpensive, deterministic,
and thread-safe. Expensive scans should be materialized in the plugin's own
store or cached behind a revision marker rather than repeated per request.

### Packaging, discovery, and activation

Plugins are discovered through the `yt_library.plugins` Python entry-point
group. The entry-point name, configured key, and the object returned as
`plugin_id` must match and must satisfy `^[a-z][a-z0-9_-]*$`.

A minimal `pyproject.toml` contains:

```toml
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "yt-example"
version = "0.1.0"
requires-python = ">=3.12"

[project.entry-points."yt_library.plugins"]
example = "yt_example.plugin:create_plugin"

[tool.setuptools]
packages = ["yt_example"]

[tool.setuptools.package-data]
yt_example = ["browser.css", "browser.js"]
```

The plugin must be installed into the same Python environment that runs YTL.
For local development, use the YTL virtual environment rather than creating or
invoking a separate plugin environment:

```powershell
$python = "C:\Users\michael.keenan\personal\YT Library\.venv\Scripts\python.exe"
& $python -m pip install -e "C:\path\to\YT Example"
```

YTL configuration is keyed by plugin ID. The host interprets `enabled`; all
other fields are opaque plugin configuration copied into `PluginContext`:

```json
{
  "plugins": {
    "example": {
      "enabled": true,
      "config": "../YT Example/yt_example.config.json"
    }
  }
}
```

Boolean shorthand is accepted (`"example": true`), but the object form is
preferred because it can carry a pointer to plugin-owned configuration. Paths
should be resolved with `context.resolve_path(...)`; relative values are based
on the directory containing `yt_library.config.json`, not the process working
directory. `context.root` is the YTL repository root, `context.config_path` is
the active YTL config path, `context.plugin_id` is the configured ID, and
`context.plugin_config` is a copy of that plugin's settings.

With the negotiated `library_video_lookup_v1` feature, a plugin may call
`context.library_videos(video_ids)` while activated to retrieve bounded,
read-only canonical metadata for up to 250,000 explicit video IDs. Results
include `video_id`, `title`, `channel_id`, `upload_date`,
availability/playability, video type, and broadcast lifecycle fields. The host
performs the lookup; plugins must not open the YTL database directly. Status
metrics based on this lookup should cache their result and refresh only when
plugin data changes or after a reasonable interval rather than querying the
full identity set on every status poll.

On service startup, YTL follows this lifecycle:

1. Retain a status record for every valid configured plugin.
2. Do not discover or import a disabled plugin. Its state is `disabled` and it
   remains visible in Advanced Admin so it can be re-enabled.
3. For an enabled plugin, require exactly one installed entry point with the
   configured name.
4. Load its zero-argument factory, create the plugin object, and validate its
   ID, Python API version, browser assets, and worker definitions.
5. Call `start(context)` and contain any exception as plugin state `error`.
6. On each status response, call `status()` and expose it as `pluginStatus`.
7. On service shutdown, call `shutdown()` in reverse load order and suppress a
   shutdown failure so core shutdown can finish.

Missing, duplicate, incompatible, or failing entry points are reported as
`missing`, `error`, or `incompatible`; the host continues running. Enabling or
disabling from Advanced Admin writes YTL config and restarts the service. The
host refuses that state change while any worker is active. Pending plugin queue
rows are retained while disabled and become eligible again after re-enabling.

A ready plugin's generic status record contains `id`, `name`, `enabled`,
`state`, `version`, `apiVersion`, sorted `capabilities`, optional
`browserAssets`, optional `workerProcesses`, optional `adminMetrics`, and the
plugin's complete status object under `pluginStatus`. Unavailable records
instead retain `id`, configured `name`, `enabled`, `state`, and `message`.
Browser code should consume this generic shape and keep other domain-specific
details nested under `pluginStatus`.

Plugins may include `adminMetrics` in their `status()` object to expose up to
12 read-only statistics in the Advanced Admin Plugins panel. The host validates
and copies them to the generic status record. Each metric requires a unique
ID-shaped `id`, a nonempty `label`, a nonnegative integer `value`, and a
`format` of `integer` or `bytes`; `description` is optional and becomes hover
text. The host owns number and byte formatting, and metrics never grant the
plugin access to Admin DOM.

### Python plugin object

The entry-point factory takes no arguments and returns one object. A minimal
backend-only implementation is:

```python
from __future__ import annotations

from typing import Any


class ExamplePlugin:
    plugin_id = "example"
    plugin_name = "YT Example"
    plugin_version = "0.1.0"
    plugin_api_version = 2
    capabilities = frozenset({"example_status"})
    browser_assets: tuple[dict[str, str], ...] = ()

    def __init__(self) -> None:
        self._ready = False

    def start(self, context: Any) -> None:
        configured = str(context.plugin_config.get("config") or "").strip()
        self._config_path = context.resolve_path(configured) if configured else None
        self._ready = True

    def status(self) -> dict[str, Any]:
        return {"state": "ready" if self._ready else "stopped"}

    def handle_api(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
    ) -> tuple[int, Any] | None:
        if method == "GET" and path == "status":
            return 200, self.status()
        return None

    def shutdown(self) -> None:
        self._ready = False


def create_plugin() -> ExamplePlugin:
    return ExamplePlugin()
```

A complete plugin should provide the following metadata and methods. The host
strictly validates `plugin_id` and `plugin_api_version`; the explicit name,
version, capabilities, API handler, and shutdown method keep status, browser
assets, and failure behavior predictable rather than relying on host fallbacks:

- `plugin_id`: exact entry-point/config ID.
- `plugin_name`: user-facing name.
- `plugin_version`: plugin release string. It is also used to cache-bust browser
  asset URLs.
- `plugin_api_version`: currently exactly `2`.
- `capabilities`: iterable of strings advertised at the top level of plugin
  status. Browser features should declare and check a capability rather than
  infer support from the plugin name.
- `start(context)`: validate plugin-owned configuration and initialize bounded
  resources. Do not start an untracked background loop here.
- `status()`: return a JSON object. Return `state: "ready"` only when advertised
  features are usable. This method is called by browser bootstrap and frequent
  Admin polling, so it must not scan a large database.
- `handle_api(method, path, query)`: return `(HTTP status, JSON-serializable
  payload)` or `None` for an unknown route.
- `shutdown()`: close plugin-owned resources and make future accidental calls
  harmless.

Optional features add `browser_assets` and `handle_browser_asset`,
`filter_videos`, `project_videos`, `project_playlist_groups`,
`project_channel_groups`, or the worker
methods described below. A plugin can avoid importing YTL at runtime by using
`Any` or plugin-owned typing protocols for host contexts. The dependency
direction remains plugin to host; YTL must never import the plugin package
directly.

### Namespaced backend HTTP API

The current generic HTTP surfaces are:

- `GET /api/plugins` returns all configured plugin status records.
- `GET /api/plugins/{plugin_id}/{path}` delegates to `handle_api`.
- `POST /api/plugins/{plugin_id}/{path}` delegates a bounded JSON object to
  `handle_api_request`.
- `GET /plugins/{plugin_id}/assets/{path}` delegates declared browser assets.
- `/api/bootstrap` and `/api/admin/status` include the same generic plugin
  status records.

`path` has no leading slash. Query parameters come from
`urllib.parse.parse_qs`, so every present parameter maps to a list of strings.
The plugin should parse, validate, and bound every parameter, explicitly cap
pagination and batch IDs, and return 400 for invalid input. Query values are
URL-decoded, but plugins should explicitly use `urllib.parse.unquote` for
dynamic route segments. Returning `None` produces 404. Plugin exceptions are
contained and returned as 503 without taking down the request handler.

JSON mutations require the `plugin_json_mutations_v1` host feature. They are
same-origin `application/json` POST requests with an object body capped at 64
KiB. The browser host exposes them as `postJson(path, body, params)`. Plugins
implement `handle_api_request(method, path, query, body)` without changing the
legacy GET-only `handle_api` signature. Use this synchronous primitive only for
bounded local mutations such as saving preferences or short interactive
messages. Long-running operations remain in the host-owned worker queue.

The `youtube_watch_session_v1` feature adds
`context.youtube_video_session(video_id)`. The returned video-bound session
provides parsed watch-page initial data and an allowlisted `request_json`
transport for `get_panel`. YTL owns cookies, proxy policy, authentication
headers, request pacing, body limits, and serialization. Plugins must keep
server-issued commands in memory, must not log or persist them, and must not
automatically retry a command whose delivery is uncertain.

### Browser assets and browser API version 2

A plugin declares assets as dictionaries containing `path` and `type`:

```python
from pathlib import Path

browser_assets = (
    {"path": "browser.css", "type": "style"},
    {"path": "browser.js", "type": "script"},
)

def handle_browser_asset(self, path: str) -> tuple[str, bytes]:
    content_types = {
        "browser.css": "text/css; charset=utf-8",
        "browser.js": "text/javascript; charset=utf-8",
    }
    return content_types[path], Path(__file__).with_name(path).read_bytes()
```

Asset types are only `style` and `script`. Paths must start with an alphanumeric
character, contain only letters, digits, `.`, `_`, `/`, or `-`, and may not
contain a `..` segment. Only declared exact paths are served. Bodies may be
bytes or text. YTL sends `no-cache`, and appends `plugin_version` to asset URLs
for cache busting.

Assets load only when the plugin is enabled, its effective state is `ready`,
and the asset declaration validates. Plugin scripts run in the YTL page and are
therefore trusted local code. They must namespace their CSS classes, avoid
unnecessary globals, tolerate missing DOM elements, and contain their own
failures. Plugin CSS and JavaScript belong in the plugin package, not YTL's
templates.

The browser script registers synchronously when loaded:

```javascript
(() => {
  'use strict';
  const api = window.YTLibraryBrowserPlugins;
  if (!api || api.apiVersion !== 2) return;

  api.register({
    id: 'example',
    videoDetail: {
      capability: 'example_detail',
      render: async (videoId, host) => {
        if (!host.supports('example_detail')) return null;
        const payload = await host.requestJson(
          `videos/${encodeURIComponent(videoId)}`,
        );
        const panel = document.createElement('article');
        panel.className = 'card example-panel';
        panel.textContent = String(payload.summary || '');
        return panel;
      },
    },
    clipDetail: {
      capability: 'example_detail',
      render: async (clip, host) => {
        if (!host.supports('example_detail')) return null;
        const panel = document.createElement('article');
        panel.className = 'card example-panel';
        panel.textContent = `${clip.source_video_id}: ${clip.start_ms}-${clip.end_ms}`;
        return panel;
      },
    },
  });
})();
```

The registered `id` must equal the backend plugin ID. Duplicate or invalid IDs
are rejected. A browser feature should name a backend capability; the host
enables it only when the plugin is enabled, ready, and advertises that
capability.

The browser `host` object passed to extension functions contains:

- `pluginId`: current plugin ID.
- `status`: current generic status record, including `pluginStatus`.
- `supports(capability)`: readiness and capability check.
- `requestJson(path, params)`: namespaced GET request to the plugin. Scalar or
  array values become query parameters; non-2xx responses throw.
- `postJson(path, body, params)`: feature-gated namespaced JSON POST request.
  The body must be an object; non-2xx responses throw.
- `libraryVideos(videoIds)`: bounded lookup of canonical YTL video summaries;
  the host batches requests in groups of 100 and returns a `Map` keyed by ID.
- `libraryChannels(channelIds)`: bounded lookup of canonical YTL channel rows;
  the host batches requests in groups of 100 and returns a `Map` keyed by ID.
- `ui.createSearchVideoCard(video, options)` and
  `ui.createVideoCard(video, options)`: shared card construction.
- `ui.escapeHtml(value)`, `ui.localVideoHref(videoId)`, and
  `ui.localChannelHref(channelId)`.
- `ui.searchHighlight.textHtml(text, query)`: escaped full text with matches.
- `ui.searchHighlight.excerptHtml(text, query, options)`: escaped excerpt
  centered on the first match; `before` and `after` are optional lengths.
- `ui.searchHighlight.snippetHtml(snippet)`: escapes everything and restores
  only literal `<mark>`/`</mark>` delimiters as YTL search-highlight markup.

Use `textContent` for ordinary plugin data. When markup is required, use host
escaping and highlighting helpers. Never insert an untrusted API string through
raw `innerHTML`.

### Native entity-card extensions

Browser API version 2 includes the `entityCards` feature. Plugins should
feature-detect it with `api.features?.entityCards === 1`.

`entityCards` decorates native cards without requiring the plugin to implement
search. It is capability-gated and supports the canonical native kinds
`video`, `clip`, `playlist`, and `channel`:

```javascript
(() => {
  'use strict';
  const api = window.YTLibraryBrowserPlugins;
  if (!api || api.apiVersion !== 2 || api.features?.entityCards !== 1) return;

  api.register({
    id: 'example',
    entityCards: {
      capability: 'example_cards',
      kinds: ['video', 'clip'],
      prepare: async (entities, host, context) => {
        const ids = entities.map(entity => entity.id);
        return host.requestJson('card-summaries', {
          id: ids,
          view: context.view,
        });
      },
      render: (entity, preparedState, _host, context) => {
        const summary = preparedState?.summaries?.[entity.id];
        if (!summary) return null;
        const action = document.createElement('button');
        action.type = 'button';
        action.textContent = String(summary.actionLabel || 'Open');
        action.dataset.view = context.view;
        const metadata = document.createElement('span');
        metadata.textContent = String(summary.label || '');
        return {
          actions: [action],
          primaryMetadata: [],
          secondaryMetadata: [metadata],
        };
      },
    },
  });
})();
```

Each descriptor is a frozen `{kind, id, item}` object. `id` is the canonical
YT Library identity (`video_id`, `clip_id`, `playlist_id`, or `channel_id`), and
`item` is the bounded read model already used to render that card. Cards without
a canonical ID are not offered to extensions. `context` is a frozen
`{view, layout}` object. Current views include `search`, `playlist`, `history`,
`channel-history`, `channel-playlisted-videos`, `channel-playlists`,
`video-detail`, and `clip-detail`; layout is `grid`, `compact`, or `detailed`
where the view supports it. Channel details separate playlist-member videos
from playlists owned by that channel, with independent tab URLs and layout
preferences for both collections.

The host deduplicates descriptors by kind and ID, then calls `prepare` at most
once per plugin for the rendered batch. A repeated history occurrence is still
rendered separately, but it does not cause another preparation request. Do all
bounded I/O in `prepare`; `render` must return synchronously with `actions`,
`primaryMetadata`, and `secondaryMetadata` arrays containing plugin-owned
`HTMLElement` instances, or return `null`. Actions are placed beside native
title actions. Primary metadata is placed with the card's principal status
facts, including video availability. Secondary metadata follows later native
facts such as uploader category and precedes native descriptions and source
lists. Omitted arrays are treated as empty.

Plugins compose in browser registration order. The host wraps contributions by
plugin ID, replaces a plugin's previous contribution on re-decoration, contains
preparation and per-card rendering failures, and rejects stale asynchronous
work after navigation. Readiness and the declared capability are checked before
preparation, so disabled, unavailable, and capability-missing plugins do not
decorate. One plugin's asset, preparation, or rendering failure does not block
native cards or another plugin.

Search-match presentation is intentionally separate from entity decoration and
uses the structured `search.resultPresentation` contract below. Plugins do not
receive native card elements and must not query or mutate host card DOM.
`videoDetail: {capability, render}` panels also remain independent.

### Search, facets, cards, and virtual videos

There are two browser search patterns. Choose one deliberately.

The preferred pattern for data associated with host entities is an entity
facet. `videoFacet` keeps a result as a canonical YTL video; `clipFacet` does
the same for clips while allowing plugin presence and bounded text matches to
participate in the host query:

```javascript
api.register({
  id: 'example',
  search: {
    capability: 'example_search',
    label: 'Example data',
    searchField: {
      key: 'example',
      label: 'Example data',
      defaultEnabled: true,
      appliesToKinds: ['videos', 'clips'],
    },
    videoFacet: {
      presentLabel: 'has example data',
      absentLabel: 'no example data',
      presentHashParam: 'with-example',
      absentHashParam: 'without-example',
    },
    clipFacet: {
      presentLabel: 'has example data',
      absentLabel: 'no example data',
      presentHashParam: 'clips-with-example',
      absentHashParam: 'clips-without-example',
    },
    catalogCount: status => Number(status?.pluginStatus?.itemCount || 0),
    resultPresentation: {
      kinds: ['video', 'clip'],
      prepare: async (results, host, {query}) => {
        const ids = results
          .filter(result => result.pluginSearchMatches.includes('example'))
          .map(result => result.id);
        return host.requestJson('matches', {q: query, id: ids});
      },
      render: (result, prepared, host) => {
        const match = prepared?.matches?.[result.id];
        if (!match) return null;
        const summary = document.createElement('div');
        summary.className = 'description example-match';
        summary.innerHTML = host.ui.searchHighlight.snippetHtml(match.snippet);
        return {kindLabel: 'Example match', summary};
      },
    },
  },
});
```

`searchField` adds a checkbox under **Search in**. Its key follows the plugin ID
syntax and must be unique. An optional nonempty `appliesToKinds` array limits
the field to matching result-kind identifiers. The host hides and disables the
field when a scoped path selects another kind, or when broad Search has none of
its applicable kinds selected; its checked state is retained so returning to an
applicable kind restores the prior choice. Omit `appliesToKinds` for a field
that applies everywhere. `videoFacet` and `clipFacet` add independent facets
under the corresponding Videos or Clips filter root, with independently
selectable present and absent values. Both
are enabled on a fresh search by default, so simply installing a plugin does
not narrow the core video set. Optional
`presentDisabledPreferenceKey`/`absentDisabledPreferenceKey` values may make
those disabled states persist; omit them when the facet should reset on reload.
Saved keys must follow YTL's plugin preference namespaces:
`plugins.{plugin_id}.search` or
`plugins.{plugin_id}.filters.{lowercase_key}`. A separate, non-facet plugin
search kind starts disabled unless its valid `preferenceKey` is enabled in YTL
filter preferences.

The matching backend method is:

```python
def filter_videos(self, query: str) -> dict[str, object]:
    return {
        "video_ids": all_video_ids_with_plugin_data,
        "search_match_ids": matching_video_ids if query.strip() else (),
    }

def filter_clips(self, query: str, clips: tuple[dict[str, object], ...]) -> dict[str, object]:
    return {
        "clip_ids": clip_ids_with_plugin_data,
        "search_match_ids": matching_clip_ids if query.strip() else (),
    }
```

The host supplies `filter_clips` only valid descriptors containing `clip_id`,
`source_video_id`, `start_ms`, and `end_ms`. Plugins that index source-video
content must apply those bounds before returning a clip search match. A source
match outside the requested interval is not a clip match. The plugin may keep
one source-owned payload and reuse it for every referencing clip; the host does
not require clip-local duplication.

Both values must be iterable collections of nonempty IDs, not strings or
mappings. `search_match_ids` must be a subset of `video_ids`. The host may call
this for facet membership with an empty query, for text matching with the
current query, or both. Cache the complete membership behind a cheap plugin
revision and use an index for query matches. Returning hundreds of thousands of
IDs is supported, but building them by scanning large text or relational tables
on every search is not.

The host applies the returned sets to its normal search model, preserves native
availability/reaction/completion/membership filtering, computes stable present
and absent facet counts, and annotates page results with:

- `result.pluginFacets[plugin_id]`: whether the video or clip has plugin data.
- `result.pluginSearchMatches`: plugin IDs whose text matched the query.

Browser API version 2 advertises
`api.features?.searchResultPresentations === 1`. A
`search.resultPresentation` definition declares a nonempty unique `kinds`
array, an optional asynchronous `prepare(results, host, context)`, and a
required synchronous `render(result, preparedState, host, context)`. Search
result descriptors are frozen
`{kind, id, item, pluginFacets, pluginSearchMatches}` objects. The context
contains the current `query`.

The host calls `prepare` once per plugin and rendered page, then calls `render`
for each applicable native result. `render` returns `null` or an object with an
optional `kindLabel` string and optional plugin-owned `summary` HTMLElement.
The first label contribution wins and summaries compose in browser registration
order. A contributed summary replaces the native description without changing
the canonical entity represented by the card. Preparation and rendering
failures are isolated and surfaced with the other plugin-search warnings.
Plugins perform all I/O in `prepare`, create a distinct summary element per
result, and use the host highlighting helpers for marked snippets. There is no
imperative native-card decoration hook.

The same presentation contract applies to native-video results returned by
scoped playlist and channel searches. Those collection responses preserve
`pluginSearchMatches`, and the browser invokes the generic preparation and
rendering path so a plugin match has the same label, summary, highlighting, and
failure containment as it does in unscoped omni-search. A plugin search field
whose applicable kind is `videos` is visible before typing on channel detail
because that detail search box targets the channel's videos.

The second pattern is a separate result type. Use it only when results are not
best represented as canonical host entities. Omit both `videoFacet` and
`clipFacet`, implement
`search.fetch({query, limit, offset}, host)`, and return:

```javascript
{
  total: 123,
  totalIsExact: true,
  results: [/* bounded plugin-owned result objects */],
}
```

Implement `search.renderResult(item, host)` to return an `HTMLElement`.
`fetchEmptyQuery: true` opts into empty-query fetches. `preferenceKey` can make
the separate search kind an opt-in saved filter. The host composes plugin and
core pagination, surfaces request failures without failing core search, and
uses `catalogCount` for unloaded counts. The plugin owns the ordering within
its separate result page. Host sort selection remains authoritative for native
entity results.

A video-oriented plugin may expose read-only virtual videos for IDs absent from
YTL:

```python
def project_videos(self, requested_video_ids: frozenset[str]):
    return [
        {"video_id": video_id, "title": title_snapshot}
        for video_id in requested_video_ids
        if video_id in self_catalog
    ]
```

The response must be an iterable of unique objects containing a requested,
nonempty `video_id` and optional `title`. Returning an unrequested or duplicate
ID is a contract error. YTL combines projections from ready plugins, renders a
virtual video with an unknown availability and `Not in library` badge, and may
use it for search or `/api/videos/{video_id}` detail fallback. It never inserts
or hydrates that projection into the YTL database.

`videoDetail: {capability, render}` adds a lazy plugin-owned panel to video
detail. `render(videoId, host)` returns an `HTMLElement` or `null`. Failures are
contained so the core detail card remains usable.

`clipDetail: {capability, render}` is the corresponding clip-detail surface.
`render(clip, host)` receives the host clip read model, including `clip_id`,
`source_video_id`, `start_ms`, and `end_ms`, and returns an `HTMLElement` or
`null`. A plugin that stores source-video data should keep the clip identity and
bounds in YTL, then use this descriptor to request only the bounded plugin data;
it should not duplicate or hydrate the source video into the host database.

Browser API version 2 also advertises `features.channelVideoTabs === 1`.
`channelVideoTabs` lets a plugin contribute a paginated native-video tab to
channel detail without giving the plugin access to YTL's database:

```javascript
channelVideoTabs: [{
  id: 'example-videos',
  label: 'Example videos',
  capability: 'example_channel_videos',
  emptyMessage: 'No example videos match this channel.',
  count: async (channel, host) => {
    const payload = await host.requestJson(`channels/${channel.channel_id}`, { limit: 1 });
    return payload.total;
  },
  load: async (channel, host, { limit, offset }) => {
    const payload = await host.requestJson(
      `channels/${channel.channel_id}`,
      { limit, offset },
    );
    return {
      videoIds: payload.videoIds,
      total: payload.total,
      limit: payload.limit,
      offset: payload.offset,
    };
  },
}]
```

Tab IDs are plugin-local lowercase slugs. The host namespaces them in the URL,
isolates count and load failures, hydrates returned IDs through bounded canonical
video lookups, renders normal YTL cards, applies entity-card decorators, and
owns pagination and layout. Missing canonical videos are omitted rather than
being inserted or inferred by the plugin.

For both detail surfaces, return a lightweight panel shell during initial
render. Fetch large data only when the user expands or requests it, paginate
it, and avoid loading full transcripts or other large payloads before that.

### Navigation-group projections

A ready plugin advertising `playlist_groups` must implement
`project_playlist_groups()` and return one bounded current-state projection:

```python
{
    "revision": "optional-plugin-revision",
    "groups": [
        {
            "group_key": "local-key",
            "name": "Group name",
            "parent_key": None,
            "position": 0,
            "icon": None,
        }
    ],
    "memberships": [
        {"group_key": "local-key", "playlist_id": "PL...", "position": 0}
    ],
}
```

The host accepts at most 10,000 groups and 250,000 memberships, requires unique
group keys and group/playlist pairs, validates a cycle-free parent hierarchy,
and rejects invalid projections as a contained plugin error. It namespaces keys
as `plugin:<plugin_id>:<local-key>` so plugin and native groups cannot collide.
Names, keys, optional icons, and revision markers are length-bounded.

A plugin may mark exactly one group in a projection with
`"include_unmatched": True`. That group must not declare explicit memberships.
When YTL supplies its bounded set of canonical identifiers to the projection,
the generic host derives memberships for identifiers absent from every explicit
group in that plugin projection. The plugin owns the group key, name, and rule;
the host owns only validation and set resolution. Without a canonical identifier
set, no derived memberships are emitted.

`/api/bootstrap` merges valid projected groups with native groups and retains
memberships only for playlist IDs already present in YTL. Selecting a projected
group resolves its descendants and passes the resulting explicit playlist-ID
set through the normal playlist search model. Missing playlist IDs are not
inserted into YTL, and the plugin never receives a YTL database connection.
Projection failures appear in bootstrap diagnostics without preventing native
navigation or search.

The parallel `channel_groups` capability requires `project_channel_groups()`
with the same group shape and memberships containing `channel_id` instead of
`playlist_id`. The host namespaces these keys as
`plugin-channel:<plugin_id>:<local-key>`, retains memberships only for canonical
channels already present in YTL, and renders the complete validated hierarchy
under Channels. Selecting a group includes all descendants and filters the
normal channel search model through an explicit channel-ID set. Unknown channel
references remain plugin-owned and never create YTL rows.

Native and projected navigation hierarchies share one disclosure model. Parent
rows expose a separate accessible toggle, while the group label remains the
search action. Count-free plugin container labels are not search actions, so
both the container label and its adjacent chevron operate the same disclosure
node. Only collapsed node IDs are saved in config, so new groups are expanded
by default and no plugin-specific state enters the core contract.

### Host-owned worker queue

A plugin may declare worker processes through a `worker_processes` iterable or
zero-argument method. If any processes are declared, `plan_worker` and
`run_worker` are required. The plugin does not create its own queue tables or
untracked host threads.

```python
def worker_processes(self):
    return (
        {
            "id": "fetch",
            "name": "Fetch example data",
            "description": "Queue one bounded retrieval per eligible video.",
            "service": "youtube",
            "max_in_flight": 4,
            "admin_surface": "advanced",
            "button_label": "Fetch example data",
            "confirm": "Queue retrieval for all eligible videos?",
            "hooks": ("video_scan",),
            "admin_actions": (
                {
                    "id": "fetch-video",
                    "placement": "videos",
                    "surface": "advanced",
                    "button_label": "Fetch example data",
                    "inputs": (
                        {
                            "name": "video_id",
                            "label": "Video ID",
                            "placeholder": "11-character YouTube ID",
                            "required": True,
                            "max_length": 11,
                        },
                    ),
                },
            ),
        },
    )
```

Process and action rules:

- Process, action, input, hook, and worker outcome IDs must start with a
  lowercase letter, contain only lowercase letters, digits, `_`, or `-`, and be
  at most 80 characters.
- `service` is `local`, `youtube`, or `archivarix`. It classifies capacity;
  `youtube` and `archivarix` tasks share their respective YTL global in-flight
  limit. Every process also honors its own `max_in_flight`, clamped to 1-100.
  The classification alone does not provide a client. A plugin that requires
  `youtube_ytdlp_v1` may use the host service below from a `youtube` process.
- `admin_surface` is `none`, `basic`, or `advanced`. A non-`none` value creates
  a default bulk action in the **Plugins** panel using the process description,
  button label, and confirmation.
- Explicit `admin_actions` use unique IDs, `surface` `basic` or `advanced`, and
  placement `plugin` or `videos`. `plugin` renders in the Plugins panel;
  `videos` renders in Advanced Admin's Videos panel. The Enabled switch itself
  is always an Advanced Admin control.
- Inputs are currently text inputs. Each supports `name`, `label`,
  `placeholder`, `required`, and `max_length` (clamped to 1-2000). Query values
  arrive at `plan_worker` as lists of strings.
- Snake-case and the corresponding camel-case definition keys are accepted,
  but plugin Python should prefer snake_case. If `admin_surface` creates the
  legacy `default` action, do not also declare an explicit action named
  `default`.

The Admin enqueue route is:

```text
POST /api/admin/plugins/{plugin_id}/processes/{worker_id}/enqueue?name=value
```

The endpoint invokes planning inside YTL's queue transaction, records a queue
log, and starts the common dispatcher. Manual actions set the queue row's
`manual` flag. Plugins must not depend on partial generator rollback for direct
Admin actions: validate and preflight the entire request before yielding any
task.

`plan_worker(worker_id, context, params)` receives a bounded, read-only
`PluginPlanningContext`:

- `context.plugin_id` identifies the current plugin.
- `context.library_videos()` streams dictionaries with `video_id`, `title`,
  `channel_id`, `upload_date`, `availability`, `is_playable`, `video_type`,
  `broadcast_status`,
  `broadcast_started_at`, `broadcast_ended_at`, and
  `broadcast_status_checked_at`, ordered by video ID. A `video_type` of
  `livestream` is durable identity; `broadcast_status` is the current observed
  lifecycle state (`upcoming`, `live`, `ended`, empty for confirmed
  non-broadcast, or null when unobserved/inconclusive). Plugins must keep their
  own domain state, such as live-chat capture and replay availability.
  In browser filters, broadcast status is nested beneath Livestreams and
  constrains only livestream rows; other video types pass through independently.
- `context.library_clips()` streams dictionaries with `clip_id`, `title`,
  `source_video_id`, `source_title`, `start_ms`, `end_ms`, and `availability`,
  ordered by clip ID.
- `context.latest_worker_outcomes(worker_id)` returns the most recent run per
  nonempty subject for that plugin and worker, including `outcome`, `status`,
  `finished_at`, and `message`.

Planning must return an iterable of task dictionaries. The host accepts at most
250,000 tasks from one plan. Each normalized task has:

- required `task_id`: stable identity within this plugin and process, truncated
  by the host to 500 characters;
- optional `subject_id`, defaulting to `task_id`, for logs and outcome history,
  truncated to 500 characters;
- optional `video_id`, truncated to 500 characters, and `title`, truncated to
  2,000 characters, for common queue display;
- optional JSON-object `payload`, limited to 64 KiB encoded UTF-8;
- optional integer `priority`, clamped to -1000 through 1000. Lower values run
  first; equal priorities prefer the most recently updated row and then the
  higher queue ID.

YTL deduplicates queued work by
`plugin:{plugin_id}:{worker_id}:{task_id}`. Replanning updates the payload and
subject, retains an existing title unless it was empty, upgrades `manual`, and
keeps the lower/more urgent priority. Use a deterministic `task_id`; do not put
timestamps or random values in it unless repeat work really must coexist.

`run_worker(worker_id, task, runtime)` receives:

```python
{
    "queue_id": 123,
    "subject_id": "...",
    "video_id": "...",
    "title": "...",
    "payload": {},
}
```

`runtime.stop_requested()` is the cooperative cancellation signal. Check it
before remote work and between bounded phases. `runtime.log(level, message,
subject_id="")` writes to YTL's plugin log; customary levels are `debug`,
`info`, `warn`, and `error`. Messages are capped at 10,000 characters and
subject IDs at 500.

With the negotiated `youtube_ytdlp_v1` feature, a YouTube process may call:

```python
info = runtime.run_youtube_ytdlp(video_id, options, download=True)
```

The plugin supplies an 11-character video ID and artifact/output options. YTL
constructs the watch URL and injects a disposable copy of its configured
YouTube cookies, the configured proxy, request pacing, bounded retries,
timeouts, logging, and progress-hook cancellation. Plugins cannot override
those host-owned options. The configured cookie export is never passed to
yt-dlp directly and may not be modified by it. This is an architectural
ownership boundary rather than a security sandbox; plugins remain trusted
local Python packages.

Return a result object with an ID-shaped `outcome` and optional nonnegative
`processed`, `found`, `failed`, `skipped`, and `message` values. Defaults are
`outcome: complete` and `processed: 1`. A normal completion removes the queue
row and records the result. A stop request marks the run interrupted and keeps
the task queued. An exception is contained as `worker_error`, logged, and
removes the task; implement explicit retry planning if the domain requires it.
On service startup, stale `running` plugin runs are marked `interrupted`. A stop
during `youtube_ytdlp_v1` raises the host cancellation signal, records the run
as interrupted, and retains the queue row for resumption rather than treating
the cancellation as a worker error.

YTL persists plugin operational data in `worker_queue`, `plugin_worker_runs`,
and `plugin_worker_log`. Admin status adds `queuedCount`, `runningCount`, and
`latestRun` to each process definition and offers plugin-specific log sources.
Domain payloads still belong entirely to the plugin.

### Lifecycle hooks

Processes opt into hooks by listing their IDs in `hooks`. The current host
events are:

- `library_initialize`: emitted when Admin Initialize plans the first library
  work.
- `library_update`: emitted for manual and scheduled Update planning.
- `video_scan`: emitted when Admin resolves an individual video ID or direct
  video URL through the common add-target control, and after a metadata worker
  successfully saves video metadata. Channel targets and failed or unavailable
  video fetches do not emit it. The notification is intentionally idempotent;
  the Admin path can emit before the corresponding metadata worker emits again.
- `clip_scan`: emitted after a clip metadata worker has resolved and saved the
  clip's source video. It includes `clip_id` and `source_video_id`, each as a
  one-item list. A plugin should resolve canonical clip bounds through
  `context.library_clips()` rather than treating event parameters as metadata.

The planner receives `params["hook"]`, a boolean `params["manual"]`, and the
event parameters. The host also supplies `params["manual"]` for direct process
planning: explicit Admin actions are manual, while scheduled and automatic
planning is not. `video_scan` currently includes `video_id` as a list
containing the resolved ID. Hook plans
are wrapped in a savepoint: an exception rolls back that plugin's partial plan,
writes a `queue error` log, and allows core planning and other plugins to
continue. A hook should be an idempotent planning notification, not the work
itself.

### Admin enablement and operational behavior

Advanced Admin contains one **Plugins** panel. Every configured plugin remains
listed there even when disabled or unavailable. Each plugin row contains its
name, an **Enabled** switch, state/message, and any plugin-placement actions.
The panel is Advanced-only unless at least one loaded plugin declares a Basic
action. Video-placement actions appear in the existing Videos panel.

The state endpoint is:

```text
POST /api/admin/plugins/{plugin_id}/enabled?enabled=0|1
```

It operates only on an existing configured plugin, refuses changes while any
worker is alive, saves the current display name for the disabled state, and
restarts the service. Disabling does not delete the plugin's data, queue rows,
run history, or logs. Because the plugin entry point is not loaded while
disabled, its routes, assets, processes, filters, and projections are
unavailable until re-enabled.

### Scale, safety, and failure expectations

- Treat IDs, titles, filenames, and URLs as shell-hostile. Validate IDs in
  Python and use argument arrays or safe library APIs rather than interpolated
  shell commands.
- Bound every API page, ID batch, worker task, payload, and in-memory result.
  Do not return an entire large catalog when the browser only needs one page.
- Maintain constant-time or cached status counters. Admin polls status often.
- Preflight plans before yielding and make queue planning idempotent. A service
  stop, browser retry, or repeated lifecycle hook must not duplicate domain
  data.
- Store exact timestamps in UTC with `Z`. Present them through YTL's configured
  display timezone when using host UI.
- Keep plugin databases, config containing private paths or credentials,
  source captures, logs, and caches ignored in the plugin repository.
- Do not expose secrets in status payloads, queue payloads, log messages, API
  responses, browser code, or committed fixtures.
- Browser errors, API errors, missing plugin records, incompatible schemas, and
  unavailable domain data need explicit empty/error states. Do not fabricate
  YTL rows or erase last useful plugin data to make an error disappear.
- A plugin instance is shared across request and worker threads. Protect
  mutable caches, avoid connection reuse unless the database library permits
  it, and keep each operation's transaction ownership clear.

### Development and verification checklist

Use this sequence when creating or extending a plugin:

1. Create a separate repository and package with its own README, design notes,
   config example, tests, and ignored runtime data.
2. Define domain ownership and the stable join identifier before integration.
   Prefer a plugin-owned database and read-only projections into YTL.
3. Add the entry point and implement lifecycle/status with no browser or worker
   features. Install it editable into the YTL virtual environment.
4. Add the disabled YTL config entry, start YTL, and verify the plugin is listed
   without being imported. Enable it only after standalone startup/status tests
   pass.
5. Add bounded namespaced API routes and unit tests. Keep tests local-only; do
   not require real cookies, network calls, or personal databases.
6. Add browser assets behind explicit capabilities. Test fresh reload, search
   defaults, filter counts, pagination, all card layouts, virtual videos,
   detail loading, missing data, and plugin failure containment.
7. Add worker planning and execution last. Test deterministic deduplication,
   targeted and bulk plans, hook parameters, capacity, recent-outcome policy,
   cooperative stop, errors, logs, and restart recovery.
8. Run YTL's full `py_compile`, `unittest`, Ruff, browser-asset tests, and
   `git diff --check`, plus the plugin's independent suite.
9. Preserve queue state, restart YTL, smoke-test `/api/admin/status` and
   `/api/history/search?limit=1`, then perform live browser QA with the plugin
   enabled, disabled, and re-enabled.
10. Commit coherent changes separately in the YTL and plugin repositories.
    Never commit either project's runtime database or private configuration.

Useful YTL contract tests live in `tests/test_plugins.py`, `tests/test_server.py`,
`tests/test_templates.py`, and `tests/js/browser-assets.test.js`. A host change
must remain generic and should add a fake-plugin contract test. Plugin-specific
behavior belongs in the plugin repository's tests. As a standing guard, YTL's
core query module and browser templates must not acquire plugin-domain terms.

## Data Sources

Source parsers provide evidence for one best-known current state:

- Current YouTube playlist web state: playlist membership, ordering, visible videos, hidden placeholders, playlist metadata, and scan status.
- YouTube live history web state: recent/history ordering, date-level labels, and account-specific thumbnail progress state.
- Takeout history zips: authoritative exported watch timestamps.
- The newest Takeout export: current playlist membership, subscriptions, exact history timestamps, and recovery input.
- YouTube's Liked videos system playlist: full current per-video like state during initialization or an explicit all-playlist scan.
- Archivarix: recovery evidence for deleted or memory-holed videos, including thumbnails, titles, descriptions, channel evidence, archive links, and not-found/deleted status.
- YouTube watch and channel pages: enriched video/channel metadata, thumbnails, channel avatars, watch progress, and raw `LIKE`, `DISLIKE`, or `INDIFFERENT` reaction state when the direct page exposes it. A missing reaction entity does not overwrite prior state.

Each source has different reliability. Takeout is best for exact watch timestamps, current YouTube scans are best for present playlist and metadata state, and Archivarix is best-effort recovery evidence. Source fields are consumed during import instead of being retained as parallel metadata histories.

The optional YT PocketTube plugin owns PocketTube exports, its database, and its
import lifecycle. When separately installed and enabled, it contributes a
read-only playlist- and channel-group hierarchy joined by YouTube playlist or
channel ID. YT Library does not write the plugin database or ingest unmatched
references. The plugin's playlist projection owns the derived `Uncategorized`
rule for canonical YTL playlists absent from every explicit PocketTube group;
YTL owns no PocketTube-specific navigation records. The generic navigation host
nests projected roots beneath a count-free
plugin label in each applicable section. Plugin parents and their descendant
branches use separate disclosure nodes whose collapsed IDs persist through the
normal YTL configuration preference endpoint. A plugin parent label toggles the
same disclosure node as its adjacent chevron because the parent itself does not
represent a searchable group.

## Storage Model

The database models the best-known current state of YouTube. Imports and scans replace superseded metadata rather than preserving revisions. When content becomes unavailable, the last known useful state is retained so removed content remains identifiable.

- `videos` owns canonical video metadata, current playability, availability, reaction, progress, and fetch state.
- `channels` owns canonical channel metadata and subscription state.
- `playlists`, `groups`, and `group_playlists` model the current library
  organization. `playlists.last_changed_at` records the latest defensible
  locally observed playlist-change evidence,
  `playlists.youtube_updated_date` separately stores YouTube's displayed
  date-only update evidence, and `playlists.updated_at` remains persistence
  bookkeeping.
- `playlist_items` links playlists to videos and retains only membership, position, unavailable-slot, and reconciliation facts.
- `history_events` stores watch events. Exact Takeout timestamps and date-only live observations share this table without fabricating precision.
- `video_recovery` stores only current Archivarix recovery status, capture time, media availability, and errors.
- Canonical `videos`, `clips`, `playlists`, and `channels` own their user-authored
  note. `tags` owns normalized reusable names, and the four entity-tag mapping
  tables attach tags without duplicating them. `entity_note_fts` indexes note
  text for full-text search; tags remain relational so suggestions and future
  tag operations do not depend on parsing display text.
- `worker_queue` stores prioritized account, Clip, metadata, playlist, History,
  recovery, and plugin tasks. Queue events and worker-specific run/log tables
  provide operational history.

Parsers may use titles, channels, descriptions, and URLs transiently to update canonical entities, then discard those source copies. Metadata revisions and complete historical playlist snapshots are intentionally not retained.

Playlist observation time and playlist change time are distinct.
`playlist_scans.scanned_at` records when the current playlist surface was last
observed. A first successful scan establishes the comparison baseline, and a
failed or identical scan does not advance `playlists.last_changed_at`. Later
membership, order, collaborator, title, description, owner, visibility,
ownership, or reported-count differences confirmed by an authoritative scan
advance it at detection time. Authenticated library discovery is a hint source:
a missing reported count remains unknown, a differing reported count queues a
confirming member scan, and discovery alone never advances `last_changed_at`.
Exact playlist creation and item-added timestamps are stronger evidence and may
advance it directly. Migration backfill may also use retained worker-log count
changes, but never generic row `updated_at` or scan timestamps; absent evidence
remains `NULL`. Playlist Newest and Oldest search ordering uses this same
user-visible `last_changed_at` evidence, never a member upload date or scan
time. Unknown change dates sort after dated playlists in Newest order.

YouTube playlist update labels are a separate source rather than inputs to the
local change signature. Parse exact labels such as `Last updated on Mar 9,
2026` directly, and resolve relative labels such as `Updated today`, `Updated
yesterday`, or `Updated 4 days ago` against the observation date in the
configured display timezone. Store only `YYYY-MM-DD` in
`playlists.youtube_updated_date`; do not fabricate midnight or another timestamp
when YouTube provides date-level precision. Preserve a later parsed date across
larger account/direct-surface disagreements, but allow a relative label to
correct the stored value backward by one day. That narrow correction handles
YouTube continuing to say `Updated today` after local midnight and later aging
the same observation to `Updated yesterday` or another incremented relative
label. These corrections never advance `last_changed_at`. Keep the raw
presentation label transient. During
the source-evaluation period, show the normalized date separately as **YT Last
updated** on every playlist card and playlist detail page without changing the
primary **Last updated** value or playlist sorting.

Playlist queue entries may carry a `metadata_only` payload for an explicit
header-only refresh. That mode fetches and stores authenticated playlist header
metadata, records diagnostic progress, and removes its queue row without
enumerating membership, rewriting `playlist_items` or `playlist_scans`, or
enqueuing member metadata and placeholder-recovery work. It records the check in
`metadata_checked_at` and persistence bookkeeping in `updated_at`, but it never
advances `last_changed_at`: a header refresh alone has no comparable playlist
state observation that proves when a change occurred.

Nullable categorical and text video features use a deliberate three-way state.
`NULL` means the feature has not been authoritatively observed, an empty string
means a successful scan observed the ordinary or absent state, and a named value
means the feature was present. Numeric observations such as maximum video height
are either `NULL` or the measured value. Failed or unavailable scans preserve
prior observations instead of collapsing unknown and absent into the same value.
YouTube's `howThisWasMadeSectionViewModel` is a container for distinct source
disclosures rather than proof of one category. Only its explicit **Made with
AI** header or canonical altered/generated sounds-or-visuals text sets
`videos.ai_disclosure`; **Auto-dubbed** remains an observed absence for that
facet instead of being conflated with creator-content disclosure. Independent
`videos.auto_dubbed` evidence comes from explicit `audioTrack.isAutoDubbed`, the
watch-page primary **Auto-dubbed** badge, or the corresponding **How this was
made** disclosure. It uses nullable boolean observations: unknown stays `NULL`,
a successful watch-page scan can observe absence, and failed scans preserve
prior values. Multiple audio tracks, a non-English title, captions, and generic
AI text do not establish auto dubbing. The shared native card decorator shows
**Auto-dubbed** when present, meaning generated tracks are available rather than
claiming which track the viewer is currently hearing.

Runtime settings, including the display timezone, request launch intervals, concurrency limits, cookie paths, and bind address, live in `yt_library.config.json`, not in SQLite. An empty display timezone is treated as UTC by the server until the browser detects an IANA timezone and saves it through the settings endpoint.

Unknown playlist slots use `NULL` video IDs and structured unavailable state. Stable YouTube video, playlist, and channel URLs are generated from IDs. Wayback links are generated from a video ID plus the retained capture timestamp.

## Worker Model

Long-running and rate-sensitive tasks run as in-process background workers with persistent queue rows, run records, and logs. The unified dispatcher selects the next eligible `worker_queue` row by priority before each launch:

`yt_library/worker_runs.py` owns the common persistence transitions for every
host run family. Workers provide only their validated family-specific fields;
the recorder applies running and terminal statuses, timestamps, progress
updates, atomic counters, and restart interruption while leaving commits and
rollbacks to the surrounding worker transaction. Fetching, queue disposition,
logs, retry policy, and error classification remain owned by each worker.

- Metadata tasks fetch channel pages directly when keyed by channel and watch pages directly when keyed by video. They never use YouTube's search interface as a metadata fallback. Each authenticated request verifies that YouTube still accepts the configured cookie; authentication failure stops further YouTube dispatch.
- Playlist tasks scan playlists with yt-dlp first and fall back to the web
  parser when needed. They record reported, exposed, and unavailable counts
  without replacing a fuller scan with a short result, and compare successful
  authoritative results with the prior baseline to detect playlist changes.
- Placeholder tasks query Archivarix for deleted/private/unavailable video IDs, persist each recovery attempt and its run-linked logs, and preserve rate-limited tasks for a later retry. A completed recovery suppresses later automatic handoffs from repeated inconclusive YouTube metadata checks, while an explicit manual recovery can still refresh it. Transient attempts update the latest search outcome without erasing previously recovered Archivarix status, channel, capture, or media evidence. A default-on Advanced setting clears only a persisted daily-quota hold after the UTC date rolls over, records the automatic retry in the queue log, and resumes the dispatcher; authentication, proxy, timeout, and request failures remain manual recovery decisions.
- History tasks support recent fetch and full verification modes, fetching YouTube history in batches and reconciling after each batch.

YouTube metadata and Archivarix recovery have independent launch intervals and `max_in_flight` limits from the config file, so a slow request to one site does not stall the other site's cadence. Playlist and history tasks remain worker-specific and run through the same prioritized queue. The dispatcher checks SQLite again before each launch, so priority changes and newly queued work can affect later dispatches without rebuilding an in-memory batch.

Initialize, Update, and Rebuild use one declarative library queue planner.
Initialize selects a full playlist scan including Liked videos, full History
verification, all Clip discovery, and due metadata without clearing pending
work. Update adds only playlist scans with an integrity signal (never scanned,
failed, or reported-count mismatch), recent History, new Clip and playlist
discovery, and never-fetched metadata. Scan age alone does not make a playlist
due; the explicit Scan all action is the force-refresh path. Update and Rebuild
do not poll Liked videos. Rebuild replaces the regenerable account, History,
metadata, and playlist plan rows, while preserving manual requests plus pending
Clip, Archivarix recovery, plugin, and future non-plan rows. It then applies the
due-work plan and does not start the dispatcher automatically. Plugins receive `library_initialize` and
`library_update` hooks; Rebuild preserves plugin rows because the host contract
does not define a generic rebuild hook.

Workers should be visible and interruptible from `/admin`. Queue counts, previews, timing estimates, stop buttons, and incrementally polled logs are part of the design, not just debugging conveniences. A server restart interrupts active in-process workers, so unfinished metadata, playlist, history, and placeholder recovery runs are marked interrupted during startup.

## UI Goals

The UI should be a dense local operations tool rather than a marketing page.

- Keep primary views immediately useful: unscoped search, scoped category lists, occurrence History, detail views, and the admin dashboard.
- Prefer local playlist navigation; provide separate external links for opening YouTube. A single-video detail card's title and separate YouTube action use the canonical bare watch URL and open in a new tab, while cards rendered inside a specific playlist may retain that playlist context in the external link.
- Show channel avatars and creator links when normalized channel metadata exists.
- Show actionable availability, visibility, and Archivarix status while keeping internal source and reconciliation-match labels out of the user interface.
- Keep controls and queues foldable on admin sections so status cards and run badges remain visible.
- Use cached thumbnails and avatars when available, but tolerate missing media gracefully.

## Prior Art

Most similar projects are archive or download oriented:

- Tube Archivist ([site](https://www.tubearchivist.com/), [GitHub](https://github.com/tubearchivist/tubearchivist)): self-hosted YouTube media server for downloading, indexing, searching, and tracking watched/unwatched archived videos.
- Pinchflat ([GitHub](https://github.com/kieraneglin/pinchflat)): self-hosted YouTube media manager for periodically archiving channels and playlists.
- MeTube ([GitHub](https://github.com/alexta69/metube)): web UI for yt-dlp downloads, including playlists, channels, thumbnails, and queues.
- Kaset ([GitHub](https://github.com/sozercan/kaset), [YouTube architecture](https://github.com/sozercan/kaset/blob/main/docs/youtube.md)): native YouTube client with an authenticated `WEB` InnerTube implementation, strict response parsers, and captured fixtures. Use it alongside yt-dlp as a reference for discoverable YouTube API capabilities, request profiles, authentication, continuations, and renderer changes; it is not a runtime dependency or an authority on YT Library behavior.
- YouTube History Analyzer ([GitHub](https://github.com/positron48/youtube-history-analyzer)): Takeout/watch-history analytics and reports.
- youtube-playlists-tracker-app ([GitHub](https://github.com/devbret/youtube-playlists-tracker-app)): playlist collection and viewing-progress tracking, especially for playthrough-style playlists.

These overlap with pieces of this project, but none appear to target the same combination of account-library mirroring, unavailable-video reconciliation, Archivarix recovery, Takeout/live-history reconciliation, and local metadata browsing without making downloading the center of the workflow.

## Watch Progress Discovery

YouTube exposes thumbnail watch status as card-renderer metadata, not as ordinary watch-page metadata.

Observed test video:

- URL: `https://www.youtube.com/watch?v=6RTNO-nMGBc`
- Title: `AT&T Fiber Without the Gateway (It Actually Works)`
- Exact-ID search result exposed `thumbnailOverlayResumePlaybackRenderer.percentDurationWatched = 10`.
- History feed lockup exposed `thumbnailOverlayProgressBarViewModel.startPercent = 10`.
- Direct watch page did not expose those progress fields in the useful metadata path.

The same card also included `watchEndpoint.startTimeSeconds = 7`, but that does not line up cleanly with 10% of an 11:34 video. Treat the thumbnail progress percentage as the authoritative display signal until proven otherwise.

## Watch Progress Design

Watch progress is account-specific, volatile state. It should be captured as enrichment, not treated as equivalent to history evidence.

Progress belongs to the `history_events` occurrence where it was observed:

- `watch_progress_percent`
- `watch_resume_seconds`

Known extraction shapes:

   - Classic renderer: `thumbnailOverlayResumePlaybackRenderer.percentDurationWatched`
   - New lockup renderer: `thumbnailOverlayProgressBarViewModel.startPercent`
   - Resume candidates: `watchEndpoint.startTimeSeconds`

Canonical video and playlist cards derive their displayed completion from the
greatest observed history occurrence rather than storing a second progress
value on `videos`. A manual metadata refresh does not assign completion because
it cannot identify the corresponding watch occurrence. History cards retain
their per-occurrence value. Cards render known progress as a thin red thumbnail
bar plus a `Watched N%` line.

Open question: `startTimeSeconds` may be useful, but it did not match the observed progress percentage in the first test case. Continue treating percentage as the authoritative UI signal until more examples clarify the resume semantics.

## Time And Data Principles

- Store every exact instant as ISO 8601 UTC with a trailing `Z`.
- Store date-only YouTube history as `watch_date` with `watched_at = NULL`; ordinal preserves relative feed order.
- Detect the browser IANA timezone only when no saved value exists. Admin may override it.
- Convert exact timestamps for display, but never timezone-shift a date-only observation.
- Current YouTube metadata supersedes Takeout and Archivarix metadata. Empty or failed responses never erase useful values.
- When a video becomes unavailable, update playability and availability while retaining its last useful identity.
- When evidence is uncertain, preserve that uncertainty in membership or match fields instead of inventing an identity or timestamp.

## Operational Principles

- Prefer web-interface extraction and local cookies before API usage.
- Be polite with remote services: batch, delay, expose limits, and make workers stoppable.
- Do not require server restarts for data-only changes; API reads should refresh from SQLite. Restart only when code, served HTML/JS, schema/bootstrap, or worker behavior changes.
- Treat `schema.sql` as the canonical fresh-install schema and preserve supported
  existing databases through ordered migrations in `database.py`. Every schema
  change must update `SCHEMA_VERSION`, provide a data-preserving migration, and
  include both fresh-bootstrap and upgrade-path tests.
- Keep personal artifacts out of Git: cookies, Takeout zips, SQLite databases, logs, and cached images.
