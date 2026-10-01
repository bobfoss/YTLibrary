const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const templateDirectory = path.join(process.cwd(), 'yt_library', 'templates');
const assetNames = [
  'theme.js',
  'timezone.js',
  'video-card.js',
  'collection-card.js',
  'entity-card-extensions.js',
  'search-result-presentations.js',
  'history-workflow.js',
  'index.js',
  'admin-transport.js',
  'admin.js',
];

function source(name) {
  return fs.readFileSync(path.join(templateDirectory, name), 'utf8');
}

function namedFunctionSource(script, name) {
  const start = script.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `${name} must exist`);
  const next = script.indexOf('\nfunction ', start + 1);
  return script.slice(start, next === -1 ? script.length : next);
}

function timezoneHelpers(displayTimezone) {
  const context = {
    CustomEvent: class CustomEvent {},
    Date,
    Intl,
    URLSearchParams,
    console,
    window: {
      YT_LIBRARY_CONFIG: { displayTimezone },
      dispatchEvent() {},
    },
  };
  vm.runInNewContext(source('timezone.js'), context, { filename: 'timezone.js' });
  return context.window.YTLibraryTime;
}

function videoCardHelpers() {
  const context = { console, window: {} };
  vm.runInNewContext(source('video-card.js'), context, { filename: 'video-card.js' });
  return context.window.YTLibraryVideoCard;
}

test('all served browser assets have valid JavaScript syntax', () => {
  for (const name of assetNames) {
    assert.doesNotThrow(
      () => new vm.Script(source(name), { filename: name }),
      `${name} must parse`,
    );
  }
});

test('empty entity notes collapse behind a shared add-notes control', () => {
  const editorSource = namedFunctionSource(source('index.js'), 'annotationEditorFor');
  const htmlSource = source('index.html');

  assert.match(editorSource, /const hasNote = Boolean\(note\.trim\(\)\)/);
  assert.match(editorSource, />Add notes<\/button>/);
  assert.match(editorSource, /class="entity-annotation-note-field"/);
  assert.match(editorSource, /noteToggle\?\.addEventListener\('click'/);
  assert.match(editorSource, /noteField\.hidden = false/);
  assert.match(editorSource, /noteInput\.focus\(\)/);
  assert.match(htmlSource, /\.entity-annotation-note-field\[hidden\] \{ display: none; \}/);
});

test('shared card annotations render tags with and without notes', () => {
  const indexSource = source('index.js');
  const context = {
    escapeHtml(value) {
      return String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;');
    },
  };
  vm.runInNewContext(
    `${namedFunctionSource(indexSource, 'noteIconHtml')}\n`
      + `${namedFunctionSource(indexSource, 'annotationCardOptions')}`,
    context,
  );

  const tagsOnly = context.annotationCardOptions({ tags: ['Favorite', 'Deep < Dive'] });
  const withNote = context.annotationCardOptions({ note: 'Remember this', tags: ['Reference'] });

  assert.match(tagsOnly.annotationMetaHtml, /#Favorite/);
  assert.match(tagsOnly.annotationMetaHtml, /#Deep &lt; Dive/);
  assert.equal(tagsOnly.annotationNoteHtml, '');
  assert.match(withNote.annotationMetaHtml, />Notes</);
  assert.match(withNote.annotationMetaHtml, /#Reference/);
  assert.match(withNote.annotationNoteHtml, /Remember this/);
});

test('unsaved entity notes guard browser and in-app navigation', () => {
  const indexSource = source('index.js');
  const editorSource = namedFunctionSource(indexSource, 'annotationEditorFor');
  const dirtySource = namedFunctionSource(indexSource, 'hasUnsavedNoteChanges');
  const routeSource = namedFunctionSource(indexSource, 'setBrowserUrl');
  const popstateSource = namedFunctionSource(indexSource, 'handleBrowserPopState');

  assert.match(editorSource, /editor\.dataset\.annotationSavedNote = note/);
  assert.match(dirtySource, /note\.value !== \(editor\.dataset\.annotationSavedNote \|\| ''\)/);
  assert.match(routeSource, /if \(!confirmDiscardUnsavedNotes\(\)\)/);
  assert.match(popstateSource, /history\.go\(acceptedBrowserHistoryIndex - targetHistoryIndex\)/);
  assert.match(indexSource, /window\.addEventListener\('beforeunload',[\s\S]{0,180}event\.returnValue = ''/);
  assert.match(indexSource, /editor\.dataset\.annotationSavedNote = savedNote/);
});

test('note dirty state clears when content matches the saved baseline', () => {
  const indexSource = source('index.js');
  const textarea = { value: 'saved note' };
  class FakeForm {}
  class FakeTextarea {}
  Object.setPrototypeOf(textarea, FakeTextarea.prototype);
  const editor = new FakeForm();
  editor.dataset = { annotationSavedNote: 'saved note' };
  editor.elements = { namedItem: () => textarea };
  const confirmations = [];
  const context = {
    document: { querySelectorAll: () => [editor] },
    HTMLFormElement: FakeForm,
    HTMLTextAreaElement: FakeTextarea,
    window: {
      confirm(message) {
        confirmations.push(message);
        return false;
      },
    },
  };
  vm.runInNewContext(
    `${namedFunctionSource(indexSource, 'hasUnsavedNoteChanges')}\n`
      + `${namedFunctionSource(indexSource, 'confirmDiscardUnsavedNotes')}`,
    context,
  );

  assert.equal(context.hasUnsavedNoteChanges(), false);
  assert.equal(context.confirmDiscardUnsavedNotes(), true);
  textarea.value = 'changed note';
  assert.equal(context.hasUnsavedNoteChanges(), true);
  assert.equal(context.confirmDiscardUnsavedNotes(), false);
  assert.deepEqual(confirmations, ['You have unsaved notes. Leave without saving them?']);
  textarea.value = 'saved note';
  assert.equal(context.hasUnsavedNoteChanges(), false);
});

test('canceling guarded in-app navigation restores route-derived selection', () => {
  const routeSource = namedFunctionSource(source('index.js'), 'setBrowserUrl');
  const historyCalls = [];
  const context = {
    selected: '__history__',
    currentBrowserUrl: () => '/videos/current',
    confirmDiscardUnsavedNotes: () => false,
    selectionFromLocation: () => '__video__:current',
    history: {
      replaceState: (...args) => historyCalls.push(['replace', ...args]),
      pushState: (...args) => historyCalls.push(['push', ...args]),
    },
  };
  vm.runInNewContext(routeSource, context);

  assert.equal(context.setBrowserUrl('/history'), true);
  assert.equal(context.selected, '__video__:current');
  assert.deepEqual(historyCalls, []);
});

test('video cards render raw YouTube reaction statuses', () => {
  const helpers = videoCardHelpers();

  assert.equal(helpers.reactionLabel({ reaction: 'LIKE' }), 'Liked');
  assert.equal(helpers.reactionLabel({ reaction: 'DISLIKE' }), 'Disliked');
  assert.equal(helpers.reactionLabel({ reaction: 'INDIFFERENT' }), '');
  assert.match(helpers.reactionIconsHtml({ reaction: 'LIKE' }), /reaction-icon like active/);
  assert.match(helpers.reactionIconsHtml({ reaction: 'DISLIKE' }), /reaction-icon dislike active/);
  assert.doesNotMatch(helpers.reactionIconsHtml({ reaction: 'INDIFFERENT' }), / active/);
});

test('video cards and details render server availability separately from Archivarix evidence', () => {
  const indexSource = source('index.js');
  const context = {
    badgeRowsHtml(rows) {
      return rows.map(row => row.label).filter(Boolean).join(' ');
    },
    escapeHtml(value) {
      return String(value);
    },
    visibilityLabelHtml(_category, label) {
      return label;
    },
  };
  vm.runInNewContext(
    [
      namedFunctionSource(indexSource, 'videoAvailabilityValue'),
      namedFunctionSource(indexSource, 'videoAvailabilityHtml'),
      namedFunctionSource(indexSource, 'archivarixStatusLabel'),
      namedFunctionSource(indexSource, 'archivarixStatusHtml'),
    ].join('\n'),
    context,
    { filename: 'video-availability-functions.js' },
  );

  const publicWithRecovery = {
    availability_category: 'public',
    availability: 'unavailable',
    is_playable: 0,
    recovered_status: 'DELETED_FULL_META',
  };
  assert.equal(context.videoAvailabilityValue(publicWithRecovery), 'public');
  assert.match(context.videoAvailabilityHtml(publicWithRecovery), />Public</);
  assert.doesNotMatch(context.videoAvailabilityHtml(publicWithRecovery), /Unavailable/);
  assert.match(
    context.archivarixStatusHtml(publicWithRecovery),
    /Archivarix: DELETED_FULL_META/,
  );
  assert.equal(
    context.videoAvailabilityValue({
      availability_category: 'unknown',
      recovered_status: 'LIVE',
    }),
    'unknown',
  );
  assert.equal(
    context.videoAvailabilityValue({ availability_category: 'members_only' }),
    'members_only',
  );

  const availabilityValueSource = namedFunctionSource(
    indexSource,
    'videoAvailabilityValue',
  );
  assert.doesNotMatch(availabilityValueSource, /recovered_status|\.availability\b|is_playable/);
  assert.match(
    namedFunctionSource(indexSource, 'videoDetailCardFor'),
    /videoAvailabilityHtml\(video\)[\s\S]*archivarixStatusHtml\(video\)/,
  );
  assert.match(
    namedFunctionSource(indexSource, 'playlistVideoCardFor'),
    /archivarixStatusHtml\(video\)[\s\S]*videoAvailabilityHtml\(video\)/,
  );
});

test('single-video detail uses a bare YouTube watch URL', () => {
  const indexSource = source('index.js');
  const context = {};
  vm.runInNewContext(namedFunctionSource(indexSource, 'youtubeWatchUrl'), context);
  const video = { video_id: 'abc123_-XYZ', playlist_id: 'PLcontext' };

  assert.equal(
    context.youtubeWatchUrl(video),
    'https://www.youtube.com/watch?v=abc123_-XYZ&list=PLcontext',
  );
  assert.equal(
    context.youtubeWatchUrl(video, false),
    'https://www.youtube.com/watch?v=abc123_-XYZ',
  );
  assert.match(
    namedFunctionSource(indexSource, 'videoDetailCardFor'),
    /youtubeWatchUrl\(video, false\)/,
  );
  assert.match(
    namedFunctionSource(indexSource, 'playlistVideoCardFor'),
    /youtubeWatchUrl\(video\)/,
  );
});

test('playlist cards render observed and temporary YouTube update dates', () => {
  const indexSource = source('index.js');
  const context = {
    Date,
    Intl,
    escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;'),
    window: {
      YTLibraryTime: {
        detected: () => 'America/Los_Angeles',
        formatDate: value => `date:${value}`,
        timeZone: 'America/Los_Angeles',
      },
    },
  };
  vm.runInNewContext(
    `${namedFunctionSource(indexSource, 'cardTimestampLabel')}\n`
      + `${namedFunctionSource(indexSource, 'playlistLastUpdatedHtml')}\n`
      + `${namedFunctionSource(indexSource, 'playlistYoutubeLastUpdatedLabel')}\n`
      + `${namedFunctionSource(indexSource, 'playlistYoutubeLastUpdatedHtml')}`,
    context,
  );

  const rendered = context.playlistLastUpdatedHtml({
    last_changed_at: '2026-08-26T03:34:17Z',
    updated_at: '2026-08-27T03:34:17Z',
  });
  assert.match(rendered, /class="details last-updated-line"/);
  assert.match(rendered, /Last updated Aug 25, 2026 8:34 PM/);
  assert.equal(context.playlistLastUpdatedHtml({ updated_at: '2026-08-27T03:34:17Z' }), '');
  const youtubeRendered = context.playlistYoutubeLastUpdatedHtml({
    youtube_updated_date: '2026-08-26',
  });
  assert.match(youtubeRendered, /class="details yt-last-updated-line"/);
  assert.match(youtubeRendered, /YT Last updated date:2026-08-26/);
  assert.equal(context.playlistYoutubeLastUpdatedHtml({}), '');

  assert.doesNotMatch(source('video-card.js'), /lastUpdatedHtml/);
  assert.match(source('collection-card.js'), /primaryMetadata[\s\S]{0,100}lastUpdatedHtml/);
  assert.match(
    namedFunctionSource(indexSource, 'cardFor'),
    /lastUpdatedHtml: `\$\{playlistLastUpdatedHtml\(playlist\)\}\$\{playlistYoutubeLastUpdatedHtml\(playlist\)\}`/,
  );
  assert.match(indexSource, /playlistHeadingMeta[\s\S]{0,350}YT Last updated/);
  assert.doesNotMatch(namedFunctionSource(indexSource, 'clipCardFor'), /lastUpdatedHtml/);
  assert.doesNotMatch(namedFunctionSource(indexSource, 'channelCardFor'), /lastUpdatedHtml/);
  assert.doesNotMatch(namedFunctionSource(indexSource, 'videoDetailCardFor'), /lastUpdatedHtml/);
  assert.doesNotMatch(namedFunctionSource(indexSource, 'channelDetailCardFor'), /lastUpdatedHtml/);
});

test('timezone reset persists the detected zone in one request', () => {
  const timezoneSource = source('timezone.js');

  assert.match(
    timezoneSource,
    /async reset\(\) \{\s*return persist\(detected\(\)\);\s*\}/,
  );
  assert.doesNotMatch(timezoneSource, /method: 'DELETE'/);
});

test('admin header shows a locally ticking clock calibrated from server time', () => {
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.match(adminHtml, /id="currentDateTime" class="metric header-current-time"/);
  assert.match(adminHtml, /class="service-state advanced-only"[\s\S]*?id="serviceStatus"[\s\S]*?id="serviceUptime" class="metric service-uptime"/);
  assert.doesNotMatch(adminHtml, /id="serviceStartedAt"/);
  assert.match(adminSource, /const serverClockSyncIntervalMs = 60 \* 60 \* 1000;/);
  assert.match(adminSource, /function syncServerClock\(serverTime, requestStartedAt,/);
  assert.match(adminSource, /serverClock\.epochMs = serverEpochMs \+ halfRoundTripMs;/);
  assert.match(adminSource, /function currentServerTime\(nowMonotonic = performance\.now\(\)\)/);
  assert.match(adminSource, /syncServerClock\(data\.service\?\.serverTime, requestStartedAt\);/);
  assert.match(adminSource, /serviceStartedAtEpochMs = Number\.isFinite\(parsedStartedAt\) \? parsedStartedAt : null;/);
  assert.match(adminSource, /service\.startedAt \? `Started \$\{fmtTime\(service\.startedAt\)\}` : ''/);
  assert.match(adminSource, /`Uptime: \$\{formatServiceUptime\(now\.getTime\(\) - serviceStartedAtEpochMs\)\}`/);
  assert.doesNotMatch(adminSource, /function updateCurrentDateTime\(now = new Date\(\)\)/);
  assert.match(
    adminSource,
    /setInterval\(\(\) => \{\s*if \(adminPageIsActive\(\)\) updateCurrentDateTime\(\);\s*\}, 1000\);/,
  );
  assert.match(adminSource, /window\.addEventListener\('ytlibrarytimezonechange', \(\) => updateCurrentDateTime\(\)\);/);
});

test('admin formats service uptime as a stable ticking clock', () => {
  const adminSource = source('admin.js');
  const context = {};
  vm.runInNewContext(namedFunctionSource(adminSource, 'formatServiceUptime'), context);

  assert.equal(context.formatServiceUptime(3661000), '01:01:01');
  assert.equal(context.formatServiceUptime(90061000), '1d 01:01:01');
  assert.equal(context.formatServiceUptime(-1000), '00:00:00');
});

test('detail navigation retains the active search state', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /let retainedSearchUrl = loadRetainedSearchUrl\(\);/);
  assert.match(indexSource, /window\.sessionStorage\.getItem\('yt-library-retained-search-url'\)/);
  assert.match(indexSource, /window\.sessionStorage\.setItem\('yt-library-retained-search-url', href\)/);
  assert.match(indexSource, /rememberSearchUrl\(currentBrowserUrl\(\)\)/);
  assert.match(indexSource, /setBrowserUrl\(retainedSearchUrl \|\| '\/search'\)/);
  assert.match(
    indexSource,
    /searchNav\?\.addEventListener\('click', event => \{[\s\S]{0,120}handleSidebarLinkClick\(event, activateSearchNavigation\)/,
  );
  assert.doesNotMatch(
    indexSource,
    /if \(selected !== '__search__'\) search\.value = '';/,
  );
});

test('history keeps search fields focused on occurrence results without showing facets', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexHtml, /\.filters\[hidden\] \{ display: none; \}/);
  assert.match(
    indexSource,
    /function syncSearchFiltersForSelection\(\)[\s\S]{0,180}searchFilters\.hidden = false[\s\S]{0,80}searchFilterTree\.hidden = historySelected/,
  );
  assert.match(
    indexSource,
    /search\.placeholder = selected === '__history__'[\s\S]{0,50}'Search history'/,
  );
  assert.match(
    indexSource,
    /search\.addEventListener\('input'[\s\S]{0,220}selected === '__history__'[\s\S]{0,260}updateCurrentUrl\(true\)[\s\S]{0,180}void render\(\)/,
  );
  assert.match(
    indexSource,
    /if \(pathname === '\/history'\)[\s\S]{0,120}search\.value = params\.get\('q'\) \|\| ''[\s\S]{0,80}applyHistorySearchFieldLocation\(params\)/,
  );
  assert.match(
    indexSource,
    /async function fetchHistoryPage[\s\S]{0,900}params\.set\('search_fields', searchFieldsValue\)/,
  );
  assert.match(
    indexSource,
    /async function fetchHistoryActivity[\s\S]{0,800}params\.set\('search_fields', searchFieldsValue\)/,
  );
  assert.match(
    indexSource,
    /function bindSearchField\(input\)[\s\S]{0,180}selected === '__history__'[\s\S]{0,260}updateCurrentUrl\(true\)[\s\S]{0,80}void render\(\)/,
  );
});

test('sidebar navigation uses real links without intercepting modified clicks', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexHtml, /<a id="history-nav" class="group" href="\/history"/);
  assert.match(indexHtml, /<a id="search-nav" class="group search-nav" href="\/search"/);
  assert.match(indexSource, /function searchPresetHref\(preset, groupKey = ''\)/);
  assert.match(indexSource, /function handleSidebarLinkClick\(event, navigate\)[\s\S]{0,240}event\.ctrlKey[\s\S]{0,120}event\.preventDefault\(\)/);
  assert.match(indexSource, /function groupLinkFor[\s\S]{0,160}document\.createElement\('a'\)/);
  assert.match(indexSource, /function presetLink[\s\S]{0,160}document\.createElement\('a'\)/);
  assert.match(indexSource, /link\.href = searchPresetHref\(preset/);
});

test('browser routes use path scope as the authoritative search context', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /const scopeByPath = \{[\s\S]*?'\/videos': 'videos'[\s\S]*?'\/clips': 'clips'[\s\S]*?'\/playlists': 'playlists'[\s\S]*?'\/channels': 'channels'/);
  assert.match(indexSource, /const contextKind = searchContextKind\(\)[\s\S]{0,120}selectedKinds\.filter\(kind => kind === contextKind\)/);
  assert.match(indexSource, /const base = scope \? `\/\$\{scope\}` : '\/search'/);
  assert.match(indexSource, /if \(contextKind && kind !== contextKind\) return '';/);
  assert.match(indexSource, /data-search-filter-section="\$\{escapeHtml\(kind\)\}"/);
  assert.match(indexSource, /window\.addEventListener\('popstate', handleBrowserPopState\)/);
  assert.doesNotMatch(indexSource, /window\.location\.hash|addEventListener\('hashchange'/);
});

test('entity details enter their scoped category context', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /function selectedEntityCategory\(\)[\s\S]{0,320}'__video__:'[\s\S]*?'videos'[\s\S]*?'__clip__:'[\s\S]*?'clips'[\s\S]*?'__playlist__:'[\s\S]*?'playlists'[\s\S]*?'__channel__:'[\s\S]*?'channels'/);
  assert.match(indexSource, /function searchContextKind\(\)[\s\S]{0,300}selected === '__search__' \? activeSearchScope : selectedEntityCategory\(\)/);
  assert.match(indexSource, /function activeSidebarCategory\(\)[\s\S]{0,260}return selectedEntityCategory\(\)/);
  assert.match(indexSource, /function activateSearchFromSelection[\s\S]{0,240}const scope = searchContextKind\(\)[\s\S]{0,160}activeSearchScope = scope/);
  assert.match(indexSource, /search\.placeholder = selected === '__history__'[\s\S]{0,320}selected\.startsWith\('__playlist__:'\)[\s\S]{0,180}selected\.startsWith\('__channel__:'\)[\s\S]{0,120}placeholders\[contextKind\]/);
  assert.match(indexSource, /async function fetchEntitySearchFilters\(category, entityId\)[\s\S]{0,480}q: entityId[\s\S]{0,220}limit: '1'/);
  assert.match(indexSource, /function hydrateEntitySearchFilters\(category, entityId, generation\)[\s\S]{0,300}fetchEntitySearchFilters\(category, entityId\)[\s\S]{0,180}renderSearchMetaFilters\(payload\)/);
  assert.match(indexSource, /hydrateEntitySearchFilters\('videos', video\.video_id \|\| videoId, generation\)/);
  assert.match(indexSource, /hydrateEntitySearchFilters\('clips', clip\.clip_id \|\| clipId, generation\)/);
  assert.doesNotMatch(indexSource, /hydrateEntitySearchFilters\('channels', channelId, generation\)/);
});

test('playlist detail reuses the sidebar video search facets', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /search\.placeholder = selected === '__history__'[\s\S]{0,220}selected\.startsWith\('__playlist__:'\)[\s\S]{0,100}'Search this playlist'/);
  assert.match(indexSource, /function searchContextKind\(\)[\s\S]{0,100}selected\.startsWith\('__playlist__:'\)[\s\S]{0,40}'videos'/);
  assert.match(indexSource, /fetchVideoCollection\(\{[\s\S]{0,650}useSearchFacets: true/);
  assert.match(indexSource, /reactionCounts: payload\.reactionCounts/);
  assert.match(indexSource, /uploaderCategoryCounts: payload\.uploaderCategoryCounts/);
  assert.match(indexSource, /const distinctVideoCount = Number\([\s\S]{0,180}payload\.distinctTotal/);
  assert.match(indexSource, /setPresetLinkCount\('videos', distinctVideoCount\)/);
  assert.doesNotMatch(indexSource, /function playlistVideoFiltersHtml/);
});

test('channel detail search stays scoped to channel-owned videos', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /let channelDetailSearchActive = false/);
  assert.match(indexSource, /function searchContextKind\(\)[\s\S]{0,160}selected\.startsWith\('__channel__:'\)[\s\S]{0,40}'videos'/);
  assert.match(indexSource, /channelDetailSearchActive = params\.has\('q'\)[\s\S]{0,100}applySearchLocation\('\/videos', params\)/);
  assert.match(indexSource, /selected\.startsWith\('__channel__:'\)[\s\S]{0,120}'Search channel'/);
  assert.match(indexSource, /function channelScopedVideoCollectionOptions\(channelId[\s\S]{0,180}scope: 'channel'[\s\S]{0,100}channelId/);
  assert.match(indexSource, /function hydrateChannelVideoSearchFilters\(channelId, channelLabel, generation\)[\s\S]{0,240}channelScopedVideoCollectionOptions\(channelId, 1\)[\s\S]{0,100}query: ''[\s\S]{0,80}pageSize: 1/);
  assert.match(indexSource, /function hydrateChannelVideoSearchFilters[\s\S]{0,650}renderScopedVideoSearchFacets\(payload\)[\s\S]{0,120}setPresetLinkLabel\('videos', channelLabel \|\| channelId\)/);
  assert.match(indexSource, /if \(!channelDetailSearchActive\) \{[\s\S]{0,220}hydrateChannelVideoSearchFilters\([\s\S]{0,160}channel\.title \|\| channelReference/);
  assert.match(indexSource, /if \(channelDetailSearchActive\)[\s\S]{0,700}fetchVideoCollection\([\s\S]{0,120}channelScopedVideoCollectionOptions\(channelId\)/);
  assert.match(indexSource, /setPresetLinkLabel\('videos', channel\.title \|\| channelReference\)/);
  assert.match(indexSource, /setDocumentTitle\(channel\.title \|\| channelReference\);[\s\S]{0,140}search\.placeholder = channel\.title \? `Search \$\{channel\.title\}` : 'Search channel'/);
  assert.match(indexSource, /function activeSidebarCategory\(\)[\s\S]{0,180}selected\.startsWith\('__channel__:'\)[\s\S]{0,80}return 'videos'/);
  assert.match(indexSource, /function browserSearchFieldAppliesToCurrentContext\(plugin\)[\s\S]{0,220}selected\.startsWith\('__channel__:'\)[\s\S]{0,60}'videos'/);
  assert.match(indexSource, /if \(selected\.startsWith\('__channel__:'\)\)[\s\S]{0,220}const nextActive = Boolean\(search\.value\.trim\(\)\)[\s\S]{0,140}activeSearchScope = 'videos'[\s\S]{0,140}if \(nextActive\) applySearchPresetState\(activeSearchScope\)[\s\S]{0,100}updateCurrentUrl\(true\)/);
  assert.match(indexSource, /if \(channelDetailSearchActive\)[\s\S]{0,3200}scopedVideoSearchResults\(rows\)/);
  assert.match(indexSource, /scopedVideoSearchResults\(rows\)[\s\S]{0,500}prepareBrowserSearchResultPresentations\([\s\S]{0,700}searchResultCardFor\(result/);
});

test('foreground loads share the app loading status lifecycle', () => {
  const indexSource = source('index.js');
  const renderStart = indexSource.indexOf('async function renderCurrentView()');
  const playlistStart = indexSource.indexOf("if (selected.startsWith('__playlist__:')) {", renderStart);
  const historyStart = indexSource.indexOf("if (selected === '__history__')", playlistStart);
  const playlistSource = indexSource.slice(playlistStart, historyStart);

  assert.match(indexSource, /function beginLoadingStatus\(\{ reset = false \} = \{\}\)/);
  assert.match(indexSource, /function finishLoadingStatus\(token\)/);
  assert.match(indexSource, /async function withLoadingStatus\(load\)/);
  assert.match(indexSource, /async function render\(\) \{\s*const loadingToken = beginLoadingStatus\(\{ reset: true \}\)/);
  assert.match(indexSource, /function loadingMessageAnimation[\s\S]{0,320}loading-dots/);
  assert.match(
    indexSource,
    /function hydrateChannelTabCounts[\s\S]{0,900}withLoadingStatus\(\(\) => \([\s\S]{0,160}fetchChannelTabCount/,
    'lazy channel tab counts must keep the shared loading status active',
  );
  assert.match(
    indexSource,
    /if \(selected !== '__search__' && !selected\.startsWith\('__playlist__:'\)\) \{\s*searchResultsRendered = false;\s*stopSearchMetaProgress\(\);/,
  );
  assert.equal(
    (playlistSource.match(/stopSearchFilterProgress\(\);/g) || []).length,
    2,
    'the active playlist render must clear progress after success or failure',
  );
  assert.doesNotMatch(indexSource, /meta\.textContent = ['`]Loading /);
  assert.match(
    indexSource,
    /function scheduleAdjacentPagePrefetch[\s\S]{0,900}await withLoadingStatus\(\(\) => Promise\.all/,
    'background page warming must share the visible loading status',
  );
});

test('internal channel links prefer aliases while channel queries use canonical ids', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /video\.metadata_channel_reference \|\| channelId/);
  assert.match(indexSource, /playlist\.owner_channel_reference \|\| playlist\.owner_channel_id/);
  assert.match(indexSource, /channel\.preferred_reference \|\| channel\.channel_id/);
  assert.match(indexSource, /replace\(\/%40\/gi, '@'\)/);
  assert.match(
    indexSource,
    /fetchViewData\(\s*`\/api\/channels\/\$\{encodeChannelReference\(channelReference\)\}`/,
  );
  assert.match(indexSource, /const channelId = channel\.channel_id \|\| channelReference/);
});

test('playlist unavailable and removed filters are persisted opt-ins', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /unavailable: filterPreferenceEnabled\(filterPreferenceKeys\.unavailablePlaylistVideos\)/);
  assert.match(indexSource, /removed: filterPreferenceEnabled\(filterPreferenceKeys\.removedPlaylistVideos\)/);
  assert.match(indexSource, /function savePlaylistVideoOptInPreferences\(\)[\s\S]{0,180}playlistVideoOptInFilters/);
  assert.match(indexSource, /selected\.startsWith\('__playlist__:'\) && groupName === 'videos'[\s\S]{0,160}savePlaylistVideoOptInPreferences\(\)/);
});

test('playlist search groups unavailable under availability without a status facet', () => {
  const indexSource = source('index.js');
  const availabilityStart = indexSource.indexOf('const playlistAvailabilityMetaFilterDefinitions');
  const ownershipStart = indexSource.indexOf('const playlistOwnershipMetaFilterDefinitions');
  const availabilitySource = indexSource.slice(availabilityStart, ownershipStart);

  assert.ok(availabilityStart >= 0);
  assert.ok(availabilitySource.indexOf("key: 'unavailable'") < availabilitySource.indexOf("key: 'unknown'"));
  assert.match(indexSource, /key: 'playlistAvailability'[\s\S]{0,220}allLabel: 'Availability'/);
  assert.doesNotMatch(indexSource, /key: 'playlistStatus'/);
  assert.doesNotMatch(indexSource, /allLabel: 'Status', kind: 'playlists'/);
});

test('playlist cards show visibility before counts and pluralize video counts', () => {
  const indexSource = source('index.js');
  const cardSource = indexSource.slice(
    indexSource.indexOf('function cardFor('),
    indexSource.indexOf('function playlistStatusLabelHtml('),
  );

  assert.ok(
    cardSource.indexOf('playlistVisibilityLabelHtml(playlist)')
      < cardSource.indexOf('playlistCount ?'),
  );
  assert.match(indexSource, /count === 1 \? 'video' : 'videos'/);
});

test('native card ids are visible only in the detailed layout', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(
    indexSource,
    /class="playlist-id entity-card-id">\$\{escapeHtml\(playlist\.playlist_id\)\}/,
  );
  assert.match(
    indexSource,
    /class="video-id entity-card-id">\$\{escapeHtml\(video\.video_id\)\}/,
  );
  assert.match(
    indexSource,
    /class="clip-id entity-card-id">\$\{escapeHtml\(clip\.clip_id\)\}/,
  );
  assert.match(
    indexSource,
    /class="channel-id entity-card-id">\$\{escapeHtml\(channel\.channel_id\)\}/,
  );
  assert.match(
    indexHtml,
    /\.search-grid:not\(\.layout-detailed\) \.entity-card-id \{ display: none; \}/,
  );
});

test('content warnings render only on detailed video cards', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(
    indexSource,
    /function contentWarningHtml\(video\)[\s\S]*?<strong>Content warning<\/strong>/,
  );
  assert.match(indexSource, /contentWarningHtml: contentWarningHtml\(video\)/);
  assert.match(indexSource, /\$\{contentWarningHtml\(video\)\}/);
  assert.match(
    indexHtml,
    /\.search-grid:not\(\.layout-detailed\) \.content-warning \{ display: none; \}/,
  );
  assert.match(indexHtml, /\.details\.content-warning \{ color: var\(--warn\); \}/);
  assert.match(indexHtml, /\.content-warning strong \{ font-weight: 650; \}/);
});

test('compact channel cards retain only a right-aligned YouTube link', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');
  const channelCardSource = indexSource.slice(
    indexSource.indexOf('function channelCardFor('),
    indexSource.indexOf('function externalLinkSvg('),
  );

  assert.match(channelCardSource, /className: 'channel-card'/);
  assert.match(channelCardSource, /class="channel-id entity-card-id">\$\{escapeHtml\(channel\.channel_id\)\}/);
  assert.match(channelCardSource, /class="channel-archivarix-id">Archivarix/);
  assert.match(channelCardSource, /class="playlist-link channel-youtube-link"/);
  assert.match(channelCardSource, /class="playlist-link channel-archivarix-link"/);
  assert.match(indexHtml, /\.search-grid\.layout-compact \.channel-archivarix-link \{ display: none; \}/);
  assert.match(indexHtml, /\.search-grid\.layout-compact \.channel-card-links \{ justify-content: flex-end; \}/);
});

test('channel cards render only exceptional persisted statuses', () => {
  const indexSource = source('index.js');
  const channelCardSource = namedFunctionSource(indexSource, 'channelCardFor');
  const channelDetailSource = namedFunctionSource(indexSource, 'channelDetailCardFor');

  for (const cardSource of [channelCardSource, channelDetailSource]) {
    assert.match(cardSource, /const status = String\(channel\.status \|\| ''\)\.toLowerCase\(\)/);
    assert.match(
      cardSource,
      /\$\{status \? `<span class="badge">\$\{escapeHtml\(status\)\}<\/span>` : ''\}/,
    );
    assert.doesNotMatch(cardSource, /status \|\| ['"]active['"]/i);
  }
});

test('featured channels render only on detailed cards with internal and external links', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');
  const featuredSource = namedFunctionSource(indexSource, 'featuredChannelsHtml');
  const channelCardSource = namedFunctionSource(indexSource, 'channelCardFor');
  const channelDetailSource = namedFunctionSource(indexSource, 'channelDetailCardFor');

  assert.match(featuredSource, /Featured:/);
  assert.match(featuredSource, /featured\.cataloged && featured\.preferred_reference/);
  assert.match(featuredSource, /localChannelHref\(featured\.preferred_reference\)/);
  assert.match(featuredSource, /featured-channel-external/);
  assert.match(featuredSource, /externalLinkSvg\(\)/);
  assert.match(channelCardSource, /featuredChannelsHtml\(channel\)/);
  assert.match(channelDetailSource, /featuredChannelsHtml\(channel\)/);
  assert.match(
    indexHtml,
    /\.search-grid:not\(\.layout-detailed\) \.channel-featured-channels \{ display: none; \}/,
  );
});

test('playlist cards render one owner separately from collaborators', () => {
  const indexSource = source('index.js');
  const videoCardSource = source('video-card.js');

  assert.match(indexSource, /const people = \[owner, \.\.\.collaborators\]/);
  assert.match(indexSource, /names \+= ` and \$\{playlistPersonNameHtml\(collaborators\[0\]\)\}`/);
  assert.match(indexSource, /names \+= ` and \$\{collaborators\.length\} others`/);
  assert.match(indexSource, /playlist-creator-avatars/);
  assert.match(
    videoCardSource,
    /value\.startsWith\('\/'\) \|\| value\.startsWith\('#'\)/,
  );
  assert.match(indexSource, /return linkTargetAttributes\(href\);/);
});

test('admin runtime polling owns queue controls and recovers on tab return', () => {
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.match(adminSource, /const runtimeStatusPollMs = 5000;/);
  assert.match(adminSource, /const runtimeStatusRequestTimeoutMs = 5000;/);
  assert.match(adminSource, /const statusRequestTimeoutMs = 15000;/);
  assert.match(adminSource, /function renderRuntimeStatus\(data\)/);
  assert.match(adminSource, /'\/api\/admin\/runtime\/status'/);
  assert.match(adminSource, /fields\.startWorkerQueue\.disabled = queueWorkerActive;/);
  assert.match(adminSource, /fields\.stopWorkerQueue\.disabled = !queueWorkerRunning \|\| queueWorkerStopping;/);
  assert.match(adminSource, /function renderServiceUnavailable\(error\)/);
  assert.match(adminSource, /fields\.serviceStatus\.textContent = 'Unavailable';/);
  assert.match(adminSource, /document\.addEventListener\('visibilitychange'/);
  assert.match(adminSource, /function suspendAdminPage\(\)/);
  assert.match(adminSource, /function resumeAdminPage\(\)/);
  assert.match(adminSource, /disconnectAdminEventStreams\(\);/);
  assert.match(adminSource, /state\.controller\?\.abort\(\);/);
  assert.match(adminSource, /window\.addEventListener\('pagehide', suspendAdminPage\)/);
  assert.match(adminSource, /window\.addEventListener\('pageshow', resumeAdminPage\)/);
  assert.match(adminSource, /document\.addEventListener\('freeze', suspendAdminPage\)/);
  assert.match(adminSource, /document\.addEventListener\('resume', resumeAdminPage\)/);
  assert.match(adminHtml, /id="startWorkerQueue" type="button" disabled/);
  assert.match(adminHtml, /id="stopWorkerQueue" type="button" disabled/);
});

test('admin bounds unattended live logs and rebuilds streams after wake', () => {
  const adminSource = source('admin.js');

  assert.match(adminSource, /const maxLiveLogRows = 500;/);
  assert.match(adminSource, /function trimLiveLogRows\(\)/);
  assert.match(adminSource, /logState\.rows\.splice\(maxLiveLogRows\)/);
  assert.match(adminSource, /queueEventStream\?\.close\(\);/);
  assert.match(adminSource, /logEventStream\?\.close\(\);/);
  assert.match(adminSource, /if \(queueEventStream \|\| !adminPageIsActive\(\)\) return;/);
  assert.match(adminSource, /if \(logEventStream \|\| !adminPageIsActive\(\)\) return;/);
  assert.match(adminSource, /if \(!adminPageIsActive\(\)\) return;/);
});

test('admin status polling renders the Archivarix UTC reset countdown', () => {
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.match(adminHtml, /id="archivarixRequestsUtcRemaining"/);
  assert.match(adminSource, /function fmtUtcWindowRemaining\(windowEndsAt, nowMs = Date\.now\(\)\)/);
  assert.match(adminSource, /Math\.ceil\(\(windowEndMs - nowMs\) \/ 60000\)/);
  assert.match(adminSource, /`\(\$\{archivarixRemaining\} remaining\)`/);
});

test('admin status polling renders the latest manual or scheduled update result', () => {
  const adminSource = source('admin.js');

  assert.match(adminSource, /function formatUpdateResult\(result\)/);
  assert.match(adminSource, /result\?\.queuedAt \? `\$\{fmtTime\(result\.queuedAt\)\}: ` : ''/);
  assert.match(adminSource, /result\?\.source === 'scheduled' \? 'Scheduled: queued' : 'Queued'/);
  assert.match(adminSource, /updateSchedule\.lastResult\?\.queuedAt/);
  assert.match(adminSource, /fields\.updateStatus\.textContent = updateSchedule\.lastResult\?\.queuedAt/);
  assert.match(adminSource, /if \(!updateRequestPending\)/);
  assert.match(adminSource, /formatUpdateResult\(result\.queue \|\| \{\}\)/);
});

test('admin shows durable remote cookie status separately from update results', () => {
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.match(adminHtml, /id="updateCookieStatus"/);
  assert.match(adminSource, /function effectiveCookieStatus\(kind, authStatus\)/);
  assert.match(adminSource, /function renderUpdateCookieStatuses\(statuses = \{\}\)/);
  assert.match(adminSource, /\['youtube', 'YouTube'\]/);
  assert.match(adminSource, /\['google', 'My Activity'\]/);
  assert.match(adminSource, /\['archivarix', 'Archivarix'\]/);
  assert.match(adminSource, /Date\.parse\(modifiedAt\) > Date\.parse\(checkedAt\)/);
  assert.match(adminSource, /renderUpdateCookieStatuses\(data\.cookieAuthStatuses \|\| \{\}\)/);
});

test('admin log messages identify completed Archivarix requests', () => {
  const adminSource = source('admin.js');

  assert.match(adminSource, /\['found', 'not found', 'thumbnail'\]\.includes/);
  assert.match(adminSource, /return `Archivarix request: \$\{message\}`/);
});

test('admin log level selector uses cumulative verbosity with error as the default', () => {
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.doesNotMatch(adminHtml, /All levels/);
  assert.match(adminHtml, /<option value="error" selected>Error<\/option>/);
  assert.match(adminSource, /logSeverityRanks = Object\.freeze\(\{ info: 0, warn: 1, error: 2, debug: 3 \}\)/);
  assert.match(adminSource, /return logRank <= selectedRank;/);
  assert.match(adminSource, /fields\.logLevelFilter\.value \|\| 'error'/);
});

test('admin renders generic placed plugin process inputs', () => {
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.match(adminHtml, /id="videoPluginProcesses"/);
  assert.match(adminSource, /process\.adminActions \|\| \[\]/);
  assert.match(adminSource, /action\.placement === 'videos'/);
  assert.match(adminSource, /data-plugin-param=/);
  assert.match(adminSource, /params\[input\.dataset\.pluginParam\] = value/);
  assert.match(adminSource, /videoPluginProcesses\.addEventListener\('submit', enqueuePluginProcess\)/);
});

test('advanced admin renders a generic persisted plugin enabled slider', () => {
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.match(adminHtml, /\.plugin-enabled-control/);
  assert.match(adminHtml, /<section id="pluginPanel" class="panel plugins-panel advanced-only">/);
  assert.match(adminHtml, /<div class="queue-title"><h2>Plugins<\/h2><\/div>/);
  assert.match(adminSource, /function pluginEnabledControlHtml\(plugin\)/);
  assert.match(adminSource, /<span>Enabled<\/span>/);
  assert.match(adminSource, /class="plugin-enabled-toggle" type="checkbox"/);
  assert.match(adminSource, /\$\{plugin\.enabled \? 'checked' : ''\}/);
  assert.match(adminSource, /\/api\/admin\/plugins\/\$\{encodeURIComponent\(pluginId\)\}\/enabled/);
  assert.match(adminSource, /pluginWorkstreams\.addEventListener\('change', savePluginEnabled\)/);
  assert.match(adminSource, /<div class="plugin-workstream-header">/);
  assert.match(adminSource, /fields\.pluginPanel\.classList\.toggle\('advanced-only', !hasBasicPlugin\)/);
});

test('advanced admin renders generic plugin metrics', () => {
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.match(adminHtml, /\.plugin-metrics/);
  assert.match(adminSource, /function formatByteCount\(value\)/);
  assert.match(adminSource, /function pluginAdminMetricsHtml\(plugin\)/);
  assert.match(adminSource, /plugin\.adminMetrics \|\| \[\]/);
  assert.match(adminSource, /data-plugin-metric=/);
  assert.match(adminSource, /\$\{pluginAdminMetricsHtml\(plugin\)\}/);
});

test('history views render shared day dividers', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexSource, /function historyDayLabel\(video\)/);
  assert.match(indexSource, /const options = \{ weekday: 'short' \}/);
  assert.match(indexSource, /return `\$\{weekday\}, \$\{dateLabel\}`/);
  assert.match(indexSource, /function historyRowsWithDayDividers\(rows, options = \{\}\)/);
  assert.equal((indexSource.match(/historyRowsWithDayDividers\(rows/g) || []).length, 2);
  assert.match(indexSource, /async function renderHistoryResults\(options\)[\s\S]{0,1600}historyRowsWithDayDividers\(rows/);
  assert.match(indexSource, /async function renderHistoryView\(generation\)[\s\S]{0,500}renderHistoryResults\(\{/);
  assert.match(indexSource, /const layoutContext = 'channel-history'[\s\S]{0,500}renderHistoryResults\(\{/);
  assert.match(indexSource, /divider\.dataset\.historyDate = date/);
  assert.match(indexSource, /for \(const value of \[row\?\.watched_at, row\?\.watch_date\]\)/);
  assert.match(indexSource, /const watchDate = historyRowDateKey\(video\)/);
  assert.match(indexSource, /const target = divider instanceof HTMLElement \? divider : row/);
  assert.match(indexHtml, /\.history-day-divider/);
});

test('history day keys use the configured display timezone', () => {
  const time = timezoneHelpers('America/Los_Angeles');

  assert.equal(time.dateKey('2026-08-01T06:59:45.594617Z'), '2026-07-31');
  assert.equal(time.dateKey('2026-08-01'), '2026-08-01');
});

test('history heatmaps honor the configured week start and label the top row', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.match(indexSource, /const historyWeekStart = pageConfig\.weekStart === 'monday' \? 'monday' : 'sunday'/);
  assert.match(indexSource, /const daysSinceWeekStart = \(start\.getDay\(\) - historyWeekStartDay \+ 7\) % 7/);
  assert.match(indexSource, /weekStartLabel\.textContent = historyWeekStart === 'monday' \? 'Mon' : 'Sun'/);
  assert.match(indexHtml, /\.history-heatmap-week-start/);
  assert.match(adminHtml, /<legend>Week start<\/legend>/);
  assert.match(adminHtml, /name="weekStart" value="sunday" checked/);
  assert.match(adminHtml, /name="weekStart" value="monday"/);
  assert.match(adminSource, /week_start: fields\.weekStartMonday\.checked \? 'monday' : 'sunday'/);
});

test('zero-count filters use the config-backed shared visibility path', () => {
  const indexSource = source('index.js');
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.match(indexSource, /const hideEmptyFilters = pageConfig\.hideEmptyFilters !== false/);
  assert.match(indexSource, /function visibleMetaFilterDefinitions\(visibility, counts, definitions\)/);
  assert.match(indexSource, /!hideEmptyFilters \|\| counts === null \|\| counts === undefined \|\| metaFilterCount\(counts, key\) !== 0/);
  assert.match(adminHtml, /id="hideEmptyFilters" type="checkbox" checked>Hide empty filters/);
  assert.match(adminSource, /AdminTransport\.postJson\('\/api\/settings\/hide-empty-filters'/);
  assert.match(
    adminSource,
    /fields\.hideEmptyFilters\.addEventListener\('change',[\s\S]{0,180}saveHideEmptyFilters\(\)/,
  );
  assert.doesNotMatch(
    adminSource,
    /AdminTransport\.postJson\('\/api\/admin\/settings',[\s\S]{0,300}hide_empty_filters:/,
  );
});

test('Archivarix automatic retry is an immediate config-backed advanced switch', () => {
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.match(adminHtml, /id="archivarixAutoRetry"[^>]*checked/);
  assert.match(adminHtml, /Auto Retry Archivarix/);
  assert.match(adminSource, /AdminTransport\.postJson\('\/api\/admin\/archivarix-auto-retry'/);
  assert.match(
    adminSource,
    /fields\.archivarixAutoRetry\.addEventListener\('change',[\s\S]{0,200}saveArchivarixAutoRetry\(\)/,
  );
});

test('queue maintenance controls keep rebuild advanced and rely on automatic status polling', () => {
  const adminSource = source('admin.js');
  const adminHtml = source('admin.html');

  assert.match(
    adminHtml,
    /id="startWorkerQueue"[\s\S]{0,160}id="stopWorkerQueue"[\s\S]{0,160}id="rebuildWorkerQueue"/,
  );
  assert.match(
    adminHtml,
    /id="rebuildWorkerQueue" class="advanced-only"[^>]*>Rebuild automatic queue<\/button>/,
  );
  assert.doesNotMatch(adminHtml, /id="refresh"/);
  assert.doesNotMatch(adminSource, /getElementById\('refresh'\)/);
  assert.match(adminSource, /setInterval\(\(\) => \{[\s\S]{0,100}loadStatus\(\)/);
  assert.match(adminSource, /preserving manual requests/);
});

test('search filter bootstrap defers leaves until facet counts arrive', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /const countsReady = counts !== null && counts !== undefined/);
  assert.match(indexSource, /\$\{countsReady \? metaFilterChildrenHtml\(\{/);
  assert.match(indexSource, /\$\{expanded && countsReady \? '' : 'hidden'\}/);
  assert.match(indexSource, /const countsPending = metaCounts === null \|\| metaCounts === undefined/);
  assert.match(indexSource, /syncMetaFilterGroup\(`search-\$\{key\}`, countsPending\)/);
  assert.match(indexSource, /syncSearchKindFilter\(kind, true, countsPending\)/);
  assert.match(indexSource, /'search-clipOwnership': searchMetaVisibility\.clipOwnership/);
});

test('admin parameter posts use the shared transport with caller-owned effects', () => {
  const adminSource = source('admin.js');

  assert.match(adminSource, /const AdminTransport = window\.YTLibraryAdminTransport;/);
  assert.doesNotMatch(adminSource, /async function requestJson\(/);
  assert.match(
    adminSource,
    /async function post\(path, params = \{\}\)[\s\S]{0,420}AdminTransport\.postJson\(path, params\)[\s\S]{0,100}scheduleActionPolls\(\)[\s\S]{0,160}loadRuntimeStatus\(\{ force: true \}\)/,
  );
  assert.match(
    adminSource,
    /async function stopWorkersNow\(\)[\s\S]{0,400}AdminTransport\.postJson\('\/api\/admin\/queue\/stop'\)[\s\S]{0,100}scheduleActionPolls\(\)/,
  );
  assert.match(adminSource, /AdminTransport\.postJson\('\/api\/admin\/service\/restart'\)/);
  assert.equal((adminSource.match(/method: 'POST'/g) || []).length, 1);
});

test('service controller preserves restart intent and reports contention', () => {
  const serviceSource = fs.readFileSync(
    path.join(process.cwd(), 'scripts', 'service.ps1'),
    'utf8',
  );

  assert.match(serviceSource, /\/api\/admin\/runtime\/status/);
  assert.match(serviceSource, /if \(\$statusCode -ne 404\)/);
  assert.match(serviceSource, /one older service that predates the runtime endpoint/);
  assert.match(serviceSource, /\[int\]\$TimeoutSeconds = 90/);
  assert.match(serviceSource, /service-recovery\.json/);
  assert.match(serviceSource, /function New-RestartRecoveryState/);
  assert.match(serviceSource, /function Complete-Recovery/);
  assert.match(serviceSource, /"start"[\s\S]{0,1000}Complete-Recovery \$recoveryState/);
  assert.match(serviceSource, /function Get-PersistentQueueCount/);
  assert.match(serviceSource, /QueueResumePending = \$recoveryExpectedQueue/);
  assert.match(serviceSource, /Service-operation contention:/);
  assert.match(serviceSource, /ContendedOperationStage/);
  assert.match(serviceSource, /ContendedControllerPid/);
  assert.match(serviceSource, /Global\\YTLibraryServiceControl-/);
  assert.match(serviceSource, /function Get-WindowsServiceRecord/);
  assert.match(serviceSource, /function Get-ProcessChildOfAncestor/);
  assert.match(serviceSource, /function Restart-WindowsServiceChild/);
  assert.match(serviceSource, /\/api\/admin\/service\/restart/);
  assert.match(serviceSource, /\$activeServiceMode -eq "windows-service"[\s\S]{0,500}Restart-WindowsServiceChild/);
  assert.match(serviceSource, /WindowsServiceInstalled/);
  assert.match(serviceSource, /service-queue-intent\.json/);
  assert.match(serviceSource, /Prepare-ServiceRunLogs/);

  const installerSource = fs.readFileSync(
    path.join(process.cwd(), 'scripts', 'windows-service.ps1'),
    'utf8',
  );
  assert.match(installerSource, /Get-Credential/);
  assert.match(installerSource, /SeServiceLogonRight/);
  assert.match(installerSource, /-StartupType AutomaticDelayedStart/);
  assert.match(installerSource, /Configure-ServiceRecovery/);
  assert.doesNotMatch(installerSource, /password=/i);
});

test('history heatmaps track the first fully visible card', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexSource, /function firstVisibleHistoryCardDate\(\)[\s\S]*?const wholeCardTolerance = 1[\s\S]*?bounds\.top >= viewport\.top - wholeCardTolerance[\s\S]*?bounds\.bottom <= viewport\.bottom \+ wholeCardTolerance/);
  assert.match(indexSource, /function setHistoryHeatmapCurrentDate\(heatmap, date\)[\s\S]*?cell\.setAttribute\('aria-current', 'date'\)/);
  assert.match(indexSource, /function updateHistoryHeatmapCurrentDay\(\)[\s\S]*?syncHistoryHeatmapToVisibleDate\(heatmap, date, targetOffset\)/);
  assert.match(indexSource, /function syncHistoryHeatmapToVisibleDate\(heatmap, date, targetOffset\)[\s\S]*?fetchHistoryActivity\(channelId, targetOffset\)[\s\S]*?heatmap\.replaceWith\(replacement\)/);
  assert.match(indexSource, /grid\.replaceChildren\(\.\.\.historyBatch\.elements\);\s*scheduleHistoryHeatmapCurrentDay\(\)/);
  assert.match(indexSource, /resultsScroll\?\.addEventListener\('scroll', scheduleHistoryHeatmapCurrentDay/);
  assert.match(indexSource, /window\.addEventListener\('scroll', scheduleHistoryHeatmapCurrentDay/);
  assert.match(indexHtml, /button\.history-heatmap-day\[aria-current="date"\]/);
});

test('adjacent history month labels keep enough horizontal separation', () => {
  const indexSource = source('index.js');
  const helperSource = namedFunctionSource(indexSource, 'historyMonthLabelColumn');
  const context = { positions: [] };

  vm.runInNewContext(
    `${helperSource}\npositions = [\n`
      + '  historyMonthLabelColumn(1, 0),\n'
      + '  historyMonthLabelColumn(2, 1),\n'
      + '  historyMonthLabelColumn(6, 3),\n'
      + '];',
    context,
    { filename: 'history-month-label-column.js' },
  );

  assert.deepEqual([...context.positions], [1, 3, 6]);
});

test('history year navigation keeps the selected month and day', () => {
  const indexSource = source('index.js');

  assert.match(
    indexSource,
    /function displayedHistoryAnchorDate\(\)[\s\S]{0,420}history-heatmap-day\[aria-current="date"\]\[data-history-date\][\s\S]{0,220}selectedDay\.dataset\.historyDate/,
  );
  assert.match(
    indexSource,
    /function displayedHistoryAnchorDate\(\)[\s\S]{0,650}firstVisibleHistoryCardDate\(\)[\s\S]{0,180}historyNavigationDate/,
  );
  assert.match(
    indexSource,
    /async function shiftHistoryActivityYear\(delta\)[\s\S]{0,700}const currentAnchorDate = displayedHistoryAnchorDate\(\)[\s\S]{0,800}shiftedHistoryDateKey\(currentAnchorDate, delta\)/,
  );
});

test('history heatmap data changes share one rollback-safe transition path', () => {
  const indexSource = source('index.js');
  const workflowSource = source('history-workflow.js');

  assert.equal(
    (indexSource.match(/runHistoryHeatmapTransition\(/g) || []).length,
    4,
  );
  assert.match(
    indexSource,
    /function historyTransitionState\(\)[\s\S]{0,400}navigationDate: historyNavigationDate/,
  );
  assert.match(
    indexSource,
    /function restoreHistoryTransitionState\(snapshot\)[\s\S]{0,300}historyNavigationDate = snapshot\.navigationDate/,
  );
  assert.match(workflowSource, /if \(!isCurrent\(\) \|\| !heatmap\.isConnected\) \{\s*restore\(\)/);
  assert.match(workflowSource, /catch \(error\) \{\s*restore\(\)/);
  assert.doesNotMatch(indexSource, /heatmap\.setAttribute\('aria-busy', 'true'\)/);
});

test('histogram navigation uses a restorable date URL', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /params\.set\('date', historyNavigationDate\)/);
  assert.match(indexSource, /async function fetchHistoryLocation\(channelId = ''\)/);
  assert.match(indexSource, /day => day\.watch_date === historyNavigationDate/);
  assert.match(indexSource, /const direction = target\.dataset\.page[\s\S]{0,200}historyNavigationDate = '';/);
});

test('numbered browser pages navigate across scroll boundaries', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /let renderedPageInfo = \{ page: 1, pageCount: 1, total: 0 \}/);
  assert.match(indexSource, /function navigateAcrossPageBoundary\(direction\)/);
  assert.match(indexSource, /pendingPageBoundaryLanding = direction > 0 \? 'top' : 'bottom'/);
  assert.match(indexSource, /window\.addEventListener\('wheel', handlePageBoundaryWheel, \{ passive: false \}\)/);
  assert.match(indexSource, /window\.addEventListener\('touchend', handlePageBoundaryTouchEnd, \{ passive: true \}\)/);
});

test('numbered browser pages cache and prefetch adjacent payloads', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /function cachedRequest\(cache, key, load, maxEntries\)/);
  assert.match(indexSource, /function scheduleAdjacentPagePrefetch\(pageInfo, fetchPage, additionalRequests = \[\]\)/);
  assert.match(indexSource, /const pages = \[page \+ 1, page - 1\]/);
  assert.match(indexSource, /Promise\.all\(\[[\s\S]*runRequests\(additionalRequests\)[\s\S]*runRequests\(pageRequests\)/);
  assert.match(indexSource, /window\.setTimeout\(\(\) => void run\(\), 150\)/);
  assert.match(indexSource, /async function fetchHistoryPage\(channelId = '', page = currentPage\)/);
  assert.match(indexSource, /function historyYearPagePrefetches\(channelId, rows\)/);
  assert.match(indexSource, /const shifts = historyActivityYearOffset > 0 \? \[1, -1\] : \[1\]/);
  assert.match(indexSource, /async function fetchOmniSearch\(query, page = currentPage\)/);
  assert.match(indexSource, /fetchVideoCollection\(\{[\s\S]*page = currentPage,/);
});

test('search filters share deferred category dimming until refreshed results render', () => {
  const indexSource = source('index.js');
  const syncStart = indexSource.indexOf('function syncSearchUrlAndRender(');
  const syncEnd = indexSource.indexOf('\nfunction selectionFromLocation(', syncStart);
  const syncSource = indexSource.slice(syncStart, syncEnd);

  assert.match(indexSource, /function setSearchKindFilter\(kind, checked\)/);
  assert.match(indexSource, /function renderedSearchKindSelectionState\(kind\)/);
  assert.match(indexSource, /function syncSearchKindFilter\(kind, applyDisabledStyles = true, assumeAllChecked = false\)/);
  assert.match(indexSource, /if \(applyDisabledStyles\) \{[\s\S]{0,300}row\.classList\.toggle\('dimmed'/);
  assert.match(indexSource, /function refreshSearchAfterFilterChange\(groupName, activatedFromSelection\)/);
  assert.match(indexSource, /refreshSearchAfterFilterChange[\s\S]{0,200}syncSearchKindFilter\(searchKindForFacet\(groupName\), false\)/);
  assert.match(indexSource, /const selectSearchKind = searchKindSelectionState[\s\S]{0,100}!searchKindSelectionState\.allSelected[\s\S]{0,100}setSearchKindFilter\(searchKindFilter, selectSearchKind\)/);
  assert.match(indexSource, /setSearchKindFilter\(searchKindFilter, selectSearchKind\)[\s\S]{0,800}refreshSearchAfterFilterChange\(searchKindFilter, activatedFromSelection\)/);
  assert.match(indexSource, /setMetaFilterBranch\(treeGroupName, filterName, target\.checked\)[\s\S]{0,400}syncMetaFilterGroup\(treeGroupName\);[\s\S]{0,180}refreshSearchAfterFilterChange\(groupName, activatedFromSelection\)/);
  assert.match(indexSource, /function renderSearchMetaFilters[\s\S]*?for \(const kind of \[[\s\S]*?syncSearchKindFilter\(kind, true, countsPending\)/);
  assert.match(syncSource, /syncSidebarSelection\(\)/);
  assert.doesNotMatch(syncSource, /renderGroups\(\)/);
});

test('single-facet result kinds use the same parent state calculation', () => {
  const indexSource = source('index.js');
  const start = indexSource.indexOf('function syncSearchKindFilter(');
  const end = indexSource.indexOf('\nfunction searchKindForFacet(', start);
  const functionSource = indexSource.slice(start, end);

  assert.match(indexSource, /function renderedSearchKindSelectionState\(kind\)[\s\S]*?const facetSelections = facetKeys\.map/);
  assert.match(indexSource, /function renderedSearchKindSelectionState\(kind\)[\s\S]*?inputs\.every\(input => input\.closest\('\.meta-filter-nested-content'\)\)/);
  assert.match(functionSource, /parent\.checked = selectionState\.enabled/);
  assert.match(functionSource, /parent\.indeterminate = selectionState\.enabled && !selectionState\.allSelected/);
  assert.doesNotMatch(functionSource, /facetKeys\.length > 1/);
  assert.doesNotMatch(functionSource, /key !== 'broadcastStatus'/);
  assert.doesNotMatch(functionSource, /data-meta-child-filter="search-\$\{kind\}"/);
  assert.match(
    indexSource,
    /for \(const kind of \[[\s\S]{0,100}'videos',[\s\S]{0,40}'clips',[\s\S]{0,40}'playlists',[\s\S]{0,40}'channels'/,
  );
});

test('video and clip categories restore plugin-provided facets', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /function enableDefaultSearchKind\(kind\)[\s\S]*kind === 'videos'[\s\S]*browserVideoFilterPlugins\(\)[\s\S]*kind === 'clips'[\s\S]*browserClipFilterPlugins\(\)/);
  assert.match(indexSource, /const state = kind === 'videos'[\s\S]*browserVideoFacetState\(filterPlugin\)[\s\S]*browserClipFacetState\(filterPlugin\)/);
  assert.match(indexSource, /const defaults = kind === 'videos'[\s\S]*defaultBrowserVideoFacetVisibility\(filterPlugin\)[\s\S]*defaultBrowserClipFacetVisibility\(filterPlugin\)/);
});

test('search URLs omit state already implied by their scoped route', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /function searchMetaPresetBaseline\(groupName, preset = activeSearchPreset\)/);
  assert.match(indexSource, /function metaFilterSelectionMatches\(visibility, baseline, excludedKeys = \[\]\)/);
  assert.match(indexSource, /const baseline = searchMetaPresetBaseline\(groupName\);[\s\S]{0,120}metaFilterSelectionMatches\(visibility, baseline, optInKeys\)/);
  assert.match(indexSource, /function browserVideoFacetPresetBaseline\(plugin, preset = activeSearchPreset\)/);
  assert.match(indexSource, /state\.present !== baseline\.present/);
  assert.match(indexSource, /state\.absent !== baseline\.absent/);
  assert.match(indexSource, /const enableDetectedCategories = \([\s\S]{0,180}Object\.values\(searchMetaVisibility\.uploaderCategory\)\.some\(Boolean\)/);
  assert.match(indexSource, /searchMetaVisibility\.uploaderCategory\[key\] = enableDetectedCategories/);
});

test('channel group navigation is recursive and uses the generic search contract', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexSource, /let channelMemberships = new Map\(\);/);
  assert.match(indexSource, /for \(const item of data\.channelMemberships \|\| \[\]\)/);
  assert.match(indexSource, /'channel-group': \{ kind: 'channels', sort: 'title' \}/);
  assert.match(indexSource, /channel_group_key: searchChannelGroupKey/);
  assert.match(indexSource, /function appendGroupTree\([\s\S]*appendGroupTree\(/);
  assert.match(indexSource, /appendGroupTree\([\s\S]*'channel-group'[\s\S]*channelMemberships[\s\S]*channelChildren/);
  assert.match(indexSource, /while \(pending\.length\)[\s\S]*identifiers\.add\(identifier\)/);
  assert.match(indexHtml, /--group-depth/);
});

test('navigation group branches collapse with persisted disclosure state', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexSource, /const navigationGroupTreeCollapsed = new Set\(/);
  assert.match(indexSource, /toggle\.dataset\.groupTreeToggle = nodeId/);
  assert.match(indexSource, /aria-controls/);
  assert.match(indexSource, /childContainer\.hidden = !expanded/);
  assert.match(indexSource, /for \(const control of associatedControls\)[\s\S]{0,120}control\.setAttribute\('aria-expanded'/);
  assert.match(indexSource, /function toggleNavigationGroupTreeNode\(/);
  assert.match(indexSource, /\/api\/settings\/navigation-group-tree/);
  assert.match(indexSource, /navigationGroupTreeSaveChain\.catch\(\(\) => \{\}\)\.then\(save\)/);
  assert.match(indexHtml, /\.group-tree-children\[hidden\] \{ display: none; \}/);
  assert.match(indexHtml, /\.search-tree-toggle\[aria-expanded="true"\] svg/);
});

test('plugin navigation groups render beneath count-free persisted parents', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexSource, /function appendNavigationGroupTrees\(/);
  assert.match(indexSource, /String\(group\.source_plugin_id \|\| ''\)\.trim\(\)/);
  assert.match(indexSource, /function appendPluginGroupTree\(/);
  assert.match(indexSource, /`plugin-root:\$\{pluginId\}`/);
  assert.match(indexSource, /replace\(\/\^YT\\s\+\/i, ''\)/);
  assert.match(indexSource, /const labelNode = document\.createElement\('button'\)/);
  assert.match(indexSource, /labelNode\.className = 'group group-tree-label'/);
  assert.match(indexSource, /labelNode\.dataset\.groupTreeLabel = nodeId/);
  assert.match(indexSource, /labelNode\.textContent = label/);
  assert.match(indexSource, /labelNode\.setAttribute\('aria-controls', childContainer\.id\)/);
  assert.match(indexSource, /labelNode\.addEventListener\('click',[\s\S]{0,180}toggleNavigationGroupTreeNode\(/);
  assert.match(indexSource, /appendNavigationGroupTrees\([\s\S]*'playlist-group'/);
  assert.match(indexSource, /appendNavigationGroupTrees\([\s\S]*'channel-group'/);
  assert.match(indexHtml, /\.group-tree-label \{ cursor: pointer; font-weight: 600; \}/);
});

test('search filter tree folds facets and persists disclosure state', () => {
  const indexHtml = source('index.html');
  const indexSource = source('index.js');

  assert.match(indexHtml, /grid-template-columns: fit-content\(var\(--sidebar-max-width\)\) minmax\(0, 1fr\)/);
  assert.match(indexHtml, /\.search-for-filters \{ --search-tree-row-gap: 0;/);
  assert.match(indexHtml, /\.search-filter-slot \{ display: grid; gap: var\(--search-tree-row-gap\);/);
  assert.match(indexHtml, /\.search-meta-kind, \.search-meta-facet \{[\s\S]*?row-gap: var\(--search-tree-row-gap\);/);
  assert.match(indexHtml, /\.search-meta-kind-children, \.search-meta-facet-children \{[\s\S]*?gap: var\(--search-tree-row-gap\);/);
  assert.match(indexHtml, /\.search-meta-controls \{ display: grid; gap: var\(--search-tree-row-gap\); \}/);
  assert.match(indexHtml, /\.meta-filter-nested-option \{[^}]*gap: var\(--search-tree-row-gap\);/);
  assert.match(indexHtml, /\.search-tree-toggle[\s\S]*transition: transform 160ms ease/);
  assert.match(indexHtml, /\.search-tree-toggle\[aria-expanded="true"\][\s\S]*rotate\(90deg\)/);
  assert.match(indexHtml, /\.meta-filter input \{ accent-color: var\(--accent\); margin: 0; \}/);
  assert.match(indexHtml, /\.search-meta-facet-children \{[\s\S]*grid-template-columns: 14px minmax\(0, 1fr\)/);
  assert.match(indexHtml, /\.search-meta-facet > \.meta-filter \{ margin-left: 0; \}/);
  assert.match(indexHtml, /\.filter-parent-checkbox > input:indeterminate \+ \.filter-parent-checkbox-indicator \{ display: inline-flex; \}/);
  assert.match(indexHtml, /\.filter-parent-checkbox-indicator svg \{[^}]*stroke-width: 2\.5;/);
  assert.match(indexSource, /function parentFilterCheckboxHtml[\s\S]*M2\.75 6\.5h7\.5M6\.5 2\.75v7\.5/);
  assert.match(indexSource, /defaultSearchFilterTreeExpanded = \[[\s\S]*'kind:videos'[\s\S]*'kind:playlists'[\s\S]*'kind:channels'/);
  assert.match(indexSource, /data-search-tree-toggle="\$\{escapeHtml\(nodeId\)\}"/);
  assert.match(indexSource, /class="search-meta-facet-children"[\s\S]*\$\{expanded && countsReady \? '' : 'hidden'\}[\s\S]*class="search-tree-toggle-spacer"[\s\S]*class="search-meta-controls"/);
  assert.match(indexSource, /\/api\/settings\/search-filter-tree/);
});

test('uploader category facet requires detected categories', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /function uploaderCategoryMetaFilterDefinitions\(counts\)[\s\S]*?if \(!categories\.length\) return \[\]/);
  assert.match(indexSource, /\.\.\.\(uploaderCategoryDefinitions\.length \? \[[\s\S]*?allLabel: 'Uploader category'/);
});

test('video type facet precedes availability in search and playlist requests', () => {
  const indexSource = source('index.js');
  const typeFacet = indexSource.indexOf("key: 'videoType'");
  const availabilityFacet = indexSource.indexOf("key: 'videos'", typeFacet);

  assert.ok(typeFacet >= 0);
  assert.ok(availabilityFacet > typeFacet);
  assert.match(indexSource, /videoType: \{ video: true, short: true, livestream: true, movie: true, unknown: true \}/);
  assert.match(indexSource, /broadcastStatus: \{ live: true, ended: true, upcoming: true, unknown: true \}/);
  assert.match(indexSource, /video_type: metaFilterParamValue\(searchMetaVisibility\.videoType\)/);
  assert.match(indexSource, /video_broadcast_status: metaFilterParamValue\(searchMetaVisibility\.broadcastStatus\)/);
  assert.match(indexSource, /params\.set\('video_type', metaFilterParamValue\(videoTypes\)\)/);
  assert.match(indexSource, /params\.set\('broadcast_status', metaFilterParamValue\(broadcastStatuses\)\)/);
});

test('filter tree recursively renders broadcast status under livestreams', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexSource, /key: 'livestream',[\s\S]{0,180}childFacetKey: 'broadcastStatus'/);
  assert.match(indexSource, /function metaFilterChildrenHtml\(\{[\s\S]{0,240}childFacets = \{\}[\s\S]{0,120}treePath = \[\]/);
  assert.match(indexSource, /const childFacet = childFacets\[childFacetKey\]/);
  assert.match(indexSource, /const childHtml = metaFilterChildrenHtml\(\{[\s\S]{0,180}childFacets,[\s\S]{0,120}treePath: \[\.\.\.branchPath, key\]/);
  assert.match(indexSource, /const nodeId = searchFilterTreeNodeId\('facet', \.\.\.branchPath, key\)/);
  assert.match(indexSource, /const childDimmed = dimmed \|\| !visibility\[key\]/);
  assert.match(indexSource, /disabled,[\s\S]{0,60}dimmed: childDimmed/);
  assert.match(indexSource, /data-meta-tree-group="\$\{escapeHtml\(groupName\)\}" data-meta-tree-key="\$\{escapeHtml\(key\)\}"/);
  assert.match(indexSource, /const videoTypeChildFacets = \{[\s\S]*?broadcastStatus: \{[\s\S]*?groupName: 'search-broadcastStatus'[\s\S]*?counts: broadcastStatusCounts/);
  assert.doesNotMatch(indexSource, /enabled: Boolean\(searchMetaVisibility\.videoType\.livestream\)/);
  assert.match(indexSource, /facetHtml\(\{ key: 'videoType',[\s\S]{0,300}childFacets: videoTypeChildFacets/);
  assert.doesNotMatch(indexSource, /allLabel: 'Broadcast status'/);
  assert.doesNotMatch(indexSource, /nestedHtml/);
  assert.doesNotMatch(indexSource, /nestedNodeId/);
  assert.doesNotMatch(indexSource, /syncNestedBroadcastStatusFacet/);
  assert.match(indexSource, /searchFilterTreeToggleHtml\(nodeId, label\)/);
  assert.match(indexSource, /nestedExpanded \? '' : 'hidden'/);
  assert.match(indexSource, /function registerMetaFilterTreeGroup\(\{[\s\S]*?childGroups\.set\(definition\.key, childFacet\.groupName\)[\s\S]*?registerMetaFilterTreeGroup\(childFacet, nextAncestors, \{/);
  assert.match(indexSource, /function setMetaFilterGroupState\(groupName, checked, visited = new Set\(\)\)[\s\S]*?setMetaFilterGroupState\(childGroupName, checked, visited\)/);
  assert.match(indexSource, /function enableMetaFilterAncestors\(groupName, visited = new Set\(\)\)[\s\S]*?parentGroup\[parent\.filterName\] = true[\s\S]*?enableMetaFilterAncestors\(parent\.groupName, visited\)/);
  assert.match(indexSource, /function setMetaFilterBranch\(groupName, filterName, checked\)[\s\S]*?setMetaFilterGroup\(childGroupName, checked\)/);
  assert.match(indexSource, /setMetaFilterBranch\(treeGroupName, filterName, target\.checked\)/);
  assert.match(indexHtml, /\.meta-filter-nested-option > \.search-tree-toggle \{[^}]*left: -18px;/);
  assert.match(indexHtml, /\.meta-filter-nested-content \{ display: grid; gap: var\(--search-tree-row-gap\); margin-left: 18px; \}/);
});

test('desktop sidebar grows with filter content within responsive bounds', () => {
  const indexHtml = source('index.html');

  assert.match(indexHtml, /--sidebar-min-width: 340px/);
  assert.match(indexHtml, /--sidebar-max-width: min\(440px, 42vw\)/);
  assert.match(indexHtml, /grid-template-columns: fit-content\(var\(--sidebar-max-width\)\) minmax\(0, 1fr\)/);
  assert.match(indexHtml, /aside \{[\s\S]{0,180}width: max-content;[\s\S]{0,120}min-width: var\(--sidebar-min-width\);[\s\S]{0,120}max-width: var\(--sidebar-max-width\)/);
  assert.match(indexHtml, /@media \(max-width: 760px\)[\s\S]*?aside \{ width: auto; min-width: 0; max-width: none;/);
});

test('video cards decorate Shorts, Live, and Movies while filters also decorate Videos', () => {
  const indexSource = source('index.js');
  const videoCardSource = source('video-card.js');
  const indexHtml = source('index.html');
  const typeDecoratorSource = namedFunctionSource(indexSource, 'videoTypeDecoratorHtml');

  assert.match(typeDecoratorSource, /videoType === 'video'/);
  assert.match(typeDecoratorSource, /if \(isVideoRecord\) return '';/);
  assert.match(typeDecoratorSource, /class="video-type-icon youtube-video-icon"/);
  assert.match(typeDecoratorSource, /fill="#FF0000"/);
  assert.match(typeDecoratorSource, /videoType === 'short'/);
  assert.match(typeDecoratorSource, /class="video-type-icon shorts-icon"/);
  assert.match(typeDecoratorSource, /fill="#f03"/);
  assert.match(typeDecoratorSource, /videoType === 'livestream'/);
  assert.match(typeDecoratorSource, /!isVideoRecord \|\| broadcastStatus === 'live' \? liveNowBroadcastIconHtml\(\) : liveBroadcastIconHtml\(\)/);
  assert.match(
    namedFunctionSource(indexSource, 'liveBroadcastIconHtml'),
    /class="video-type-icon live-icon"/,
  );
  assert.match(
    namedFunctionSource(indexSource, 'liveNowBroadcastIconHtml'),
    /class="video-type-icon live-now-icon"[\s\S]*?viewBox="0 0 12 12"[\s\S]*?M2\.111 2\.111/,
  );
  assert.match(indexHtml, /\.live-now-icon \{[\s\S]*?padding: 2px;[\s\S]*?background: #f03;[\s\S]*?fill: #fff;/);
  assert.match(typeDecoratorSource, /broadcastStatusLabel\(video\)/);
  assert.match(namedFunctionSource(indexSource, 'broadcastStatusLabel'), /status === 'live'\) return 'Live now'/);
  assert.match(namedFunctionSource(indexSource, 'broadcastStatusLabel'), /status === 'ended'\) return 'Streamed live'/);
  assert.match(namedFunctionSource(indexSource, 'broadcastStatusLabel'), /status === 'upcoming'\) return 'Upcoming live'/);
  assert.match(typeDecoratorSource, /videoType === 'movie'/);
  assert.match(typeDecoratorSource, /class="video-type-icon movie-icon"/);
  assert.match(typeDecoratorSource, /isVideoRecord \? '<span class="video-type-label">Movie<\/span>' : ''/);
  assert.match(typeDecoratorSource, /return '';/);
  assert.match(
    videoCardSource,
    /\$\{options\.typeDecoratorHtml \|\| ''\}[\s\S]{0,220}data-entity-card-slot="primaryMetadata"/,
  );
  assert.match(
    namedFunctionSource(indexSource, 'videoDetailCardFor'),
    /videoTypeDecoratorHtml\(video\)[\s\S]{0,200}data-entity-card-slot="primaryMetadata"/,
  );
  assert.match(
    namedFunctionSource(indexSource, 'playlistVideoCardFor'),
    /typeDecoratorHtml: videoTypeDecoratorHtml\(video\)/,
  );
  assert.match(
    namedFunctionSource(indexSource, 'playlistVideoCardFor'),
    /movieMetadataHtml: movieMetadataHtml\(video\)/,
  );
  assert.match(videoCardSource, /\$\{options\.movieMetadataHtml \|\| ''\}/);
  assert.match(indexHtml, /\.video-type-decorator, \.ai-disclosure-decorator, \.auto-dubbed-decorator \{[\s\S]{0,220}font-size: 13px;[\s\S]{0,100}font-weight: 400;/);
  assert.match(indexHtml, /\.video-type-icon \{[\s\S]{0,180}width: 16px;[\s\S]{0,100}fill: currentColor;/);
  assert.match(indexHtml, /\.youtube-video-icon \{[\s\S]{0,100}width: 19px;[\s\S]{0,80}height: 13px;[\s\S]{0,80}flex-basis: 19px;/);
});

test('browser plugin host exposes feature-gated JSON mutations', () => {
  const indexSource = source('index.js');
  const registrationStart = indexSource.indexOf('window.YTLibraryBrowserPlugins');
  const registrationEnd = indexSource.indexOf('function browserPluginStatus');
  const registration = indexSource.slice(registrationStart, registrationEnd);
  const hostSource = namedFunctionSource(indexSource, 'browserPluginHost');

  assert.match(registration, /pluginJsonMutations: 1/);
  assert.match(hostSource, /postJson: async \(path, body = \{\}, params = \{\}\)/);
  assert.match(hostSource, /method: 'POST'/);
  assert.match(hostSource, /'Content-Type': 'application\/json'/);
  assert.match(hostSource, /body: JSON\.stringify\(body\)/);
});

test('video cards render observed feature metadata outside compact layouts', () => {
  const indexSource = source('index.js');
  const videoCardSource = source('video-card.js');
  const indexHtml = source('index.html');
  const featureSource = namedFunctionSource(indexSource, 'videoFeatureMetadataHtml');

  assert.match(featureSource, /height >= 4320/);
  assert.match(featureSource, /height >= 2160/);
  assert.match(featureSource, /height >= 720/);
  assert.match(featureSource, /spatialFormat === '360'/);
  assert.match(featureSource, /spatialFormat === 'vr180'/);
  assert.match(featureSource, /stereoLayout === 'left_right'/);
  assert.match(featureSource, /dynamicRange === 'hdr'/);
  assert.match(featureSource, /includes\('creative commons'\)/);
  assert.match(featureSource, /Location:/);
  assert.match(videoCardSource, /\$\{options\.featureMetadataHtml \|\| ''\}/);
  assert.match(indexHtml, /\.search-grid\.layout-compact \.video-feature-metadata,/);
  assert.match(
    indexHtml,
    /\.video-feature-metadata \.badge,[\s\S]{0,100}\.video-feature-metadata \.video-location \{ color: var\(--accent\); \}/,
  );
});

test('video type filters reuse the card decorators', () => {
  const indexSource = source('index.js');
  const definitionsStart = indexSource.indexOf('const videoTypeMetaFilterDefinitions');
  const definitionsEnd = indexSource.indexOf('function visibleVideoMetaFilterDefinitions');
  assert.notEqual(definitionsStart, -1);
  assert.notEqual(definitionsEnd, -1);
  const definitions = indexSource.slice(definitionsStart, definitionsEnd);
  const typeDecoratorSource = namedFunctionSource(indexSource, 'videoTypeDecoratorHtml');

  assert.match(definitions, /key: 'video'[\s\S]{0,100}decoratorHtml: videoTypeDecoratorHtml\('video'\)/);
  assert.match(definitions, /key: 'short'[\s\S]{0,100}decoratorHtml: videoTypeDecoratorHtml\('short'\)/);
  assert.match(definitions, /key: 'livestream'[\s\S]{0,100}decoratorHtml: videoTypeDecoratorHtml\('livestream'\)/);
  assert.match(definitions, /key: 'live'[\s\S]{0,100}decoratorHtml: liveNowBroadcastIconHtml\(\)/);
  assert.match(definitions, /key: 'ended'[\s\S]{0,100}decoratorHtml: liveBroadcastIconHtml\(\)/);
  assert.match(definitions, /key: 'movie'[\s\S]{0,100}decoratorHtml: videoTypeDecoratorHtml\('movie'\)/);
  assert.match(typeDecoratorSource, /const isVideoRecord = typeof video !== 'string'/);
  assert.match(typeDecoratorSource, /!isVideoRecord \|\| broadcastStatus === 'live'/);
});

test('AI disclosure is a core video decorator and recursive search facet', () => {
  const indexSource = source('index.js');
  const videoCardSource = source('video-card.js');
  const indexHtml = source('index.html');
  const decoratorSource = namedFunctionSource(indexSource, 'aiDisclosureDecoratorHtml');

  assert.match(indexSource, /aiDisclosure: \{ made_with_ai: true, no_disclosure: true, unknown: true \}/);
  assert.match(indexSource, /key: 'aiDisclosure'[\s\S]{0,220}allLabel: 'AI disclosure'/);
  assert.match(indexSource, /video_ai_disclosure: metaFilterParamValue\(searchMetaVisibility\.aiDisclosure\)/);
  assert.match(indexSource, /params\.set\('ai_disclosure', metaFilterParamValue\(aiDisclosures\)\)/);
  assert.match(decoratorSource, /Number\(video\?\.ai_disclosure\) !== 1/);
  assert.match(decoratorSource, /Made with AI/);
  assert.match(indexHtml, /\.video-type-decorator, \.ai-disclosure-decorator, \.auto-dubbed-decorator \{[\s\S]{0,260}font-size: 13px;[\s\S]{0,100}font-weight: 400;/);
  assert.match(indexHtml, /\.ai-disclosure-decorator, \.auto-dubbed-decorator \{ color: var\(--muted\); \}/);
  assert.match(videoCardSource, /options\.typeDecoratorHtml[\s\S]{0,100}options\.aiDisclosureDecoratorHtml[\s\S]{0,240}data-entity-card-slot="primaryMetadata"/);
  assert.match(
    namedFunctionSource(indexSource, 'videoDetailCardFor'),
    /videoTypeDecoratorHtml\(video\)[\s\S]{0,100}aiDisclosureDecoratorHtml\(video\)[\s\S]{0,240}data-entity-card-slot="primaryMetadata"/,
  );
});

test('auto-dubbed decorator requires an observed positive', () => {
  const context = {};
  vm.runInNewContext(namedFunctionSource(source('index.js'), 'autoDubbedDecoratorHtml'), context);
  for (const auto_dubbed of [undefined, null, 0, false]) {
    assert.equal(context.autoDubbedDecoratorHtml({ auto_dubbed }), '');
  }
  assert.match(context.autoDubbedDecoratorHtml({ auto_dubbed: 1 }), /Auto-dubbed/);
  assert.match(context.autoDubbedDecoratorHtml({ auto_dubbed: true }), /Auto-dubbed/);
});

test('sidebar keeps facet trees separate from category navigation', () => {
  const indexSource = source('index.js');
  const indexHtml = source('index.html');

  assert.match(indexHtml, /id="search-for-filters" class="search-for-filters"/);
  assert.match(indexSource, /const searchFilterTree = document\.getElementById\('search-for-filters'\)/);
  assert.match(indexSource, /const searchFilterRegion = groupsEl\.parentElement/);
  assert.match(indexSource, /function appendSearchFilterCategory[\s\S]*if \(contextKind === kind\) \{[\s\S]*container\.appendChild\(searchFilterSlot\(kind\)\)/);
  assert.match(indexSource, /function searchFilterMount\(kind, navigationSection\)[\s\S]*searchContextKind\(\) === kind \? navigationSection : searchFilterTree/);
  assert.match(indexSource, /videoSection\.appendChild\(presetLink\('videos', 'Videos',/);
  assert.match(indexSource, /searchFilterMount\('videos', videoSection\)/);
  assert.match(indexSource, /searchFilterMount\('clips', videoSection\)/);
  assert.match(indexSource, /playlistSection\.appendChild\(presetLink\('playlists', 'Playlists',/);
  assert.match(indexSource, /searchFilterMount\('playlists', playlistSection\)/);
  assert.match(indexSource, /channelSection\.appendChild\(presetLink\('channels', 'Channels',/);
  assert.match(indexSource, /searchFilterMount\('channels', channelSection\)/);
  assert.match(indexSource, /data-search-filter-slot/);
  assert.doesNotMatch(indexHtml, />Search for</);
  assert.doesNotMatch(indexSource, /presetLink\('(playlisted|liked|subscribed|terminated)'/);
  assert.doesNotMatch(indexSource, /activeSearchPreset === '(playlisted|liked|subscribed|terminated)'/);
  assert.doesNotMatch(indexSource, /browserSearchPresets/);
  assert.doesNotMatch(indexSource, /all-playlists/);
  assert.match(indexSource, /const invalidPreset = Boolean\([\s\S]{0,220}!requestedDefinition/);
  assert.match(indexSource, /if \(pathname === '\/' \|\| invalidPreset\) updateSearchUrl\(true\)/);
});

test('search and playlist video sorts include both title directions', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /const searchSortOptions = new Set\(\[[\s\S]{0,180}'title_desc'/);
  assert.match(indexSource, /function searchResultsSortHtml\(\)[\s\S]{0,320}\['title', 'Title A-Z'\][\s\S]{0,80}\['title_desc', 'Title Z-A'\]/);
  assert.match(indexSource, /function videoSortHtml\(value, scope\)[\s\S]{0,260}\['title', 'Title A-Z'\][\s\S]{0,80}\['title_desc', 'Title Z-A'\]/);
  assert.doesNotMatch(indexSource, /browserPluginForcesRelevance|forceRelevance \?/);
});

test('history heatmap always returns to the canonical current-day view', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /current\.dataset\.historyCurrent = ''/);
  assert.match(indexSource, /current\.title = 'Today'/);
  assert.match(indexSource, /current\.setAttribute\('aria-label', 'Today'\)/);
  assert.match(indexSource, /current\.innerHTML = historyHeatmapNavigationIcon/);
  assert.match(indexSource, /async function jumpToCurrentHistoryActivity\(\)/);
  assert.match(
    indexSource,
    /async function jumpToCurrentHistoryActivity\(\)[\s\S]{0,220}const alreadyOnFirstPage = currentPage === 1;[\s\S]{0,180}historyActivityYearOffset = 0;[\s\S]{0,120}currentPage = 1;[\s\S]{0,120}pendingHistoryDate = '';[\s\S]{0,120}historyNavigationDate = '';/,
  );
  assert.match(
    indexSource,
    /const commitFirstPageLocation = \(\) => \{[\s\S]{0,120}updateCurrentUrl\(true\);[\s\S]{0,120}setDocumentTitle\('History page 1'\);[\s\S]{0,120}scrollResultsToTop\(\);/,
  );
  assert.match(indexSource, /if \(alreadyOnFirstPage\)[\s\S]{0,100}commitFirstPageLocation\(\);[\s\S]{0,160}return;[\s\S]{0,120}updateCurrentUrl\(false\)/);
  assert.match(indexSource, /alreadyOnFirstPage && historyActivityYearOffset > 0[\s\S]{0,520}fetchHistoryActivity\(channelId, 0\)/);
  assert.doesNotMatch(indexSource, /current\.disabled = historyActivityYearOffset === 0 && currentPage === 1/);
  assert.doesNotMatch(indexSource, /button\.dataset\.historyCurrent !== undefined/);
  assert.match(indexSource, /function restoreHistoryNavigationButtons\(container\)/);
});

test('history document title includes the active page or date', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /historyNavigationDate[\s\S]{0,120}historyDayLabel\(\{ watch_date: historyNavigationDate \}\)/);
  assert.match(indexSource, /`page \$\{pageInfo\.page\}`/);
  assert.match(indexSource, /setDocumentTitle\(`History \$\{historyTitleLocation\}`\)/);
});

test('channel non-default tabs persist in the URL from page one', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /function channelDetailParams\(\)[\s\S]*?channelDetailTab !== 'playlisted-videos'[\s\S]*?params\.set\('tab', channelDetailTab\)/);
  assert.match(indexSource, /function channelDetailParams\(\)[\s\S]*?params\.set\('tab', 'history'\)[\s\S]*?else if \(currentPage > 1\)[\s\S]*?params\.set\('page', String\(currentPage\)\)/);
  assert.match(indexSource, /function channelDetailTabFromParams\(params\)[\s\S]*?requested === 'playlists'[\s\S]*?browserChannelVideoTab\(requested\)[\s\S]*?'playlisted-videos'/);
  assert.match(indexSource, /channelDetailTab = channelDetailTabFromParams\(params\)/);
});

test('channel tabs use independent persisted card layouts', () => {
  const indexSource = source('index.js');

  assert.match(
    indexSource,
    /'channel-playlisted-videos': cardLayouts\.has\(pageConfig\.channelPlaylistedVideoCardLayout\)[\s\S]{0,120}: 'grid'/,
  );
  assert.match(
    indexSource,
    /'channel-playlists': cardLayouts\.has\(pageConfig\.channelPlaylistCardLayout\)[\s\S]{0,120}: 'grid'/,
  );
  assert.match(
    indexSource,
    /'channel-history': cardLayouts\.has\(pageConfig\.channelHistoryCardLayout\)[\s\S]{0,120}: 'detailed'/,
  );
  assert.match(
    indexSource,
    /const layoutContext = 'channel-history'[\s\S]{0,500}renderHistoryResults\(\{[\s\S]{0,700}cardLayoutHtml\(cardLayoutFor\(layoutContext\), layoutContext\)[\s\S]{0,300}layoutContext/,
  );
  assert.match(
    indexSource,
    /const layoutContext = 'channel-playlists'[\s\S]{0,500}cardLayoutHtml\(cardLayoutFor\(layoutContext\), layoutContext\)[\s\S]{0,500}rows\.map\(playlist => cardFor\(playlist/,
  );
  assert.match(
    indexSource,
    /const layoutContext = 'channel-playlisted-videos'[\s\S]{0,500}cardLayoutHtml\(cardLayoutFor\(layoutContext\), layoutContext\)[\s\S]{0,500}rows\.map\(playlistVideoCardFor\)/,
  );
  assert.doesNotMatch(
    indexSource,
    /grid\.className = channelDetailTab === 'history'/,
  );
});

test('channel tabs distinguish playlisted videos from owned playlists', () => {
  const indexSource = source('index.js');

  assert.match(indexSource, /\['playlisted-videos', 'Playlisted videos', playlistedVideoCount\]/);
  assert.match(indexSource, /\['playlists', 'Playlists', playlistCount\]/);
  assert.match(indexSource, /fetchChannelPlaylists\(channelReference\)/);
  assert.match(indexSource, /No playlists match this channel\./);
});

test('channel detail paints before inactive tab counts are hydrated', () => {
  const indexSource = source('index.js');
  const renderStart = indexSource.indexOf('async function renderCurrentView()');
  const channelStart = indexSource.indexOf(
    "if (selected.startsWith('__channel__:')) {",
    renderStart,
  );
  const searchStart = indexSource.indexOf("if (selected === '__search__')", channelStart);
  const channelSource = indexSource.slice(channelStart, searchStart);
  const initialChrome = channelSource.indexOf('viewContext.replaceChildren(...channelChrome)');
  const activeHistory = channelSource.indexOf("if (channelDetailTab === 'history')");
  const lazyCounts = channelSource.lastIndexOf('hydrateChannelTabCounts({');

  assert.match(
    channelSource,
    /channel = await fetchViewData\([\s\S]{0,100}`\/api\/channels\/\$\{encodeChannelReference\(channelReference\)\}`/,
  );
  assert.doesNotMatch(
    channelSource.slice(0, channelSource.indexOf('const channelId =')),
    /\/videos\?limit=1|\/playlists\?limit=1|browserChannelVideoTabCounts/,
  );
  assert.ok(initialChrome >= 0 && initialChrome < activeHistory);
  assert.ok(lazyCounts > activeHistory);
  assert.match(channelSource, /storeChannelTabCount\(channelId, 'history', total\)/);
  assert.match(channelSource, /storeChannelTabCount\(channelId, 'playlists', payload\.total\)/);
  assert.match(channelSource, /'playlisted-videos',[\s\S]{0,80}payload\.total/);
});

test('channel tab transitions preserve only the current channel chrome', () => {
  const indexSource = source('index.js');

  assert.match(
    indexSource,
    /const selectedChannelReference = selected\.startsWith\('__channel__:'\)[\s\S]{0,320}renderedChannelCard\?\.dataset\.channelReference === selectedChannelReference/,
  );
  assert.match(
    indexSource,
    /if \(!preserveHistoryChrome && !preserveChannelChrome\) \{[\s\S]{0,120}viewContext\.replaceChildren\(\)/,
  );
  assert.match(
    indexSource,
    /const channelCard = channelDetailCardFor\(channel\);\s+channelCard\.dataset\.channelReference = channelReference/,
  );
});

test('card layout controls place compact beside grid', () => {
  const indexSource = source('index.js');
  const grid = indexSource.indexOf("['grid', 'Grid']");
  const compact = indexSource.indexOf("['compact', 'Compact list']");
  const detailed = indexSource.indexOf("['detailed', 'Detailed list']");

  assert.ok(grid >= 0);
  assert.ok(compact > grid);
  assert.ok(detailed > compact);
});

test('theme selection normalizes, persists, and publishes changes', () => {
  const stored = new Map([['yt-library-theme', 'light']]);
  const events = [];
  const context = {
    CustomEvent: class CustomEvent {
      constructor(type, options) {
        this.type = type;
        this.detail = options.detail;
      }
    },
    document: { documentElement: { dataset: {} } },
    window: {
      dispatchEvent: event => events.push(event),
      localStorage: {
        getItem: key => stored.get(key) || '',
        setItem: (key, value) => stored.set(key, value),
      },
    },
  };

  vm.runInNewContext(source('theme.js'), context, { filename: 'theme.js' });

  assert.equal(context.document.documentElement.dataset.theme, 'light');
  assert.equal(context.window.YTLibraryTheme.set('unsupported'), 'dark');
  assert.equal(stored.get('yt-library-theme'), 'dark');
  assert.equal(events.at(-1).type, 'ytlibrarythemechange');
  assert.equal(events.at(-1).detail.theme, 'dark');
});

test('video-card helpers escape markup and clamp watch progress', () => {
  const context = { document: {}, window: {} };
  vm.runInNewContext(source('video-card.js'), context, {
    filename: 'video-card.js',
  });
  const helpers = context.window.YTLibraryVideoCard;

  assert.equal(helpers.escapeHtml('<a href="x">&</a>'), '&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;');
  assert.equal(
    helpers.searchHighlight.textHtml('Reflecting & reflecting <b>', 'reflecting'),
    '<mark class="search-highlight">Reflecting</mark> &amp; <mark class="search-highlight">reflecting</mark> &lt;b&gt;',
  );
  assert.equal(
    helpers.searchHighlight.snippetHtml('before <mark>hit & <b></mark> <script>'),
    'before <mark class="search-highlight">hit &amp; &lt;b&gt;</mark> &lt;script&gt;',
  );
  assert.equal(
    helpers.searchHighlight.excerptHtml(
      'Discard these opening words because the reflecting match is later than the card can show safely.',
      'reflecting',
      { before: 12, after: 20 },
    ),
    '…the <mark class="search-highlight">reflecting</mark> match is later…',
  );
  assert.equal(
    helpers.searchHighlight.excerptHtml('No match & <b>', 'reflecting'),
    'No match &amp; &lt;b&gt;',
  );
  assert.equal(helpers.watchProgressPercent({ watch_progress_percent: 0.4 }), 1);
  assert.equal(helpers.watchProgressPercent({ watch_progress_percent: 104 }), 100);
  assert.equal(helpers.watchProgressPercent({ watch_progress_percent: 'bad' }), 0);
  assert.equal(
    helpers.compactWatchCountHtml({ watch_count: 1 }),
    '<span class="compact-watch-count"> · 1 watch</span>',
  );
  assert.equal(
    helpers.compactWatchCountHtml({ watch_count: 3 }),
    '<span class="compact-watch-count"> · 3 watches</span>',
  );
  assert.equal(helpers.compactWatchCountHtml({ watch_count: 0 }), '');
  assert.equal(
    helpers.watchedLineHtml({ watch_progress_percent: 25, watch_count: 2 }),
    '<div class="watched-line has-progress"><span class="watched-progress">Watched 25%</span><span class="watched-count"> · 2 watches</span></div>',
  );
  assert.equal(
    helpers.watchedLineHtml({ watch_count: 1 }),
    '<div class="watched-line"><span class="watched-count">Watched · 1 watch</span></div>',
  );
  assert.match(
    source('index.js'),
    /const compactWatchCountHtml = VideoCard\.compactWatchCountHtml;/,
  );
  assert.match(
    source('video-card.js'),
    /class="video-availability-row"[^`]+compactAvailabilityHtml/,
  );
  assert.match(
    source('index.js'),
    /class="compact-video-duration"/,
  );
});
