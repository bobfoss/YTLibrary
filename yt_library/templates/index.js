const VideoCard = window.YTLibraryVideoCard;
const CollectionCard = window.YTLibraryCollectionCard;
const EntityCardExtensions = window.YTLibraryEntityCardExtensions;
const SearchResultPresentations = window.YTLibrarySearchResultPresentations;
const HistoryWorkflow = window.YTLibraryHistoryWorkflow;
const badgeRowsHtml = VideoCard.badgeRowsHtml;
const compactWatchCountHtml = VideoCard.compactWatchCountHtml;
const creatorHtml = VideoCard.creatorHtml;
const linkTargetAttributes = VideoCard.linkTargetAttributes;
const detailRowHtml = VideoCard.detailRowHtml;
const escapeHtml = VideoCard.escapeHtml;
const membersOnlyIconHtml = VideoCard.membersOnlyIconHtml;
const reactionIconsHtml = VideoCard.reactionIconsHtml;
const reactionLabel = VideoCard.reactionLabel;
const searchHighlight = VideoCard.searchHighlight;
const uploaderCategoryHtml = VideoCard.uploaderCategoryHtml;
const thumbnailWithProgress = VideoCard.thumbnailWithProgress;
const thumbIconHtml = VideoCard.thumbIconHtml;
const watchProgressPercent = VideoCard.watchProgressPercent;
const watchedLineHtml = VideoCard.watchedLineHtml;
const watchSparklineHtml = (video, detail = false) => VideoCard.watchSparklineHtml(video, { detail });
const defaultDocumentTitle = 'YT Library';
const pageConfig = window.YT_LIBRARY_CONFIG || {};
const historyWeekStart = pageConfig.weekStart === 'monday' ? 'monday' : 'sunday';
const historyWeekStartDay = historyWeekStart === 'monday' ? 1 : 0;
const hideEmptyFilters = pageConfig.hideEmptyFilters !== false;
const filterPreferenceKeys = {
  unavailableVideos: 'videos.unavailable',
  lowPartialCompletion: 'completion.partial_below_minimum',
  unavailablePlaylistVideos: 'playlist_videos.unavailable',
  removedPlaylistVideos: 'playlist_videos.removed',
  unavailablePlaylists: 'playlists.unavailable',
  terminatedChannels: 'channels.terminated',
};
const filterPreferences = Object.fromEntries(
  Object.entries(pageConfig.filterPreferences || {}).filter(([, enabled]) => enabled === true)
);
const defaultSearchFilterTreeExpanded = [
  'kind:videos',
  'kind:playlists',
  'kind:channels',
];
const searchFilterTreeExpanded = new Set(
  (Array.isArray(pageConfig.searchFilterTreeExpanded)
    ? pageConfig.searchFilterTreeExpanded
    : defaultSearchFilterTreeExpanded)
    .filter(node => typeof node === 'string')
);
const navigationGroupTreeCollapsed = new Set(
  (Array.isArray(pageConfig.navigationGroupTreeCollapsed)
    ? pageConfig.navigationGroupTreeCollapsed
    : [])
    .filter(node => typeof node === 'string')
);

function filterPreferenceEnabled(key) {
  return filterPreferences[key] === true;
}

function setDocumentTitle(itemTitle = '') {
  const normalizedTitle = String(itemTitle || '').trim();
  document.title = normalizedTitle
    ? `${defaultDocumentTitle} - ${normalizedTitle}`
    : defaultDocumentTitle;
}

let data = null;
let playlistMemberships = new Map();
let playlistChildren = new Map();
let channelMemberships = new Map();
let channelChildren = new Map();
let navigationGroupTreeDomId = 0;
const browserPlugins = new Map();
const loadedBrowserPluginAssets = new Set();
const pluginSearchVisibility = new Map();
const pluginVideoFacetVisibility = new Map();
const pluginClipFacetVisibility = new Map();

function browserSearchFieldDefinition(plugin) {
  const definition = plugin?.search?.searchField;
  return definition && typeof definition === 'object' ? definition : null;
}

function browserSearchFieldKey(plugin) {
  const definition = browserSearchFieldDefinition(plugin);
  return String(definition?.key || plugin?.id || '');
}

function browserSearchFieldApplicableKinds(plugin) {
  const definition = browserSearchFieldDefinition(plugin);
  if (definition?.appliesToKinds === undefined) return null;
  if (
    !Array.isArray(definition.appliesToKinds)
    || !definition.appliesToKinds.length
    || definition.appliesToKinds.some(kind => !/^[a-z][a-z0-9_-]*$/.test(String(kind)))
  ) {
    throw new TypeError(`Plugin search field applicable kinds are invalid: ${plugin.id}`);
  }
  return new Set(definition.appliesToKinds.map(String));
}

function browserPluginSearchFieldEnabled(plugin) {
  const key = browserSearchFieldKey(plugin);
  return Boolean(key && activeSearchFields().has(key));
}

function registerBrowserPluginSearchField(plugin, registration) {
  if (!registration) return;
  const { definition, key, labelText } = registration;
  const label = document.createElement('label');
  label.className = 'filter';
  label.dataset.browserPluginSearchField = plugin.id;
  const input = document.createElement('input');
  input.className = 'search-field';
  input.type = 'checkbox';
  input.dataset.searchField = key;
  input.checked = definition.defaultEnabled !== false;
  label.append(input, document.createTextNode(` ${labelText}`));
  searchInFields.append(label);
  searchFields.push(input);
  bindSearchField(input);
}

function validateBrowserPluginSearchField(plugin) {
  const definition = browserSearchFieldDefinition(plugin);
  if (!definition) return null;
  const key = browserSearchFieldKey(plugin);
  const labelText = String(definition.label || '').trim();
  browserSearchFieldApplicableKinds(plugin);
  if (!/^[a-z][a-z0-9_-]*$/.test(key)) {
    throw new TypeError(`Plugin search field key is invalid: ${key}`);
  }
  if (!labelText) throw new TypeError(`Plugin search field label is required: ${plugin.id}`);
  if (searchFields.some(input => input.dataset.searchField === key)) {
    throw new Error(`Search field is already registered: ${key}`);
  }
  return { definition, key, labelText };
}

function browserChannelVideoTabDefinitions(plugin) {
  if (plugin?.channelVideoTabs === undefined) return [];
  if (!Array.isArray(plugin.channelVideoTabs)) {
    throw new TypeError(`Plugin channelVideoTabs must be an array: ${plugin.id}`);
  }
  return plugin.channelVideoTabs;
}

function validateBrowserChannelVideoTabs(plugin) {
  const tabIds = new Set();
  for (const definition of browserChannelVideoTabDefinitions(plugin)) {
    const tabId = String(definition?.id || '');
    const label = String(definition?.label || '').trim();
    const capability = String(definition?.capability || '').trim();
    if (!/^[a-z][a-z0-9-]*$/.test(tabId)) {
      throw new TypeError(`Plugin channel video tab id is invalid: ${plugin.id}`);
    }
    if (tabIds.has(tabId)) {
      throw new TypeError(`Plugin channel video tab ids must be unique: ${plugin.id}`);
    }
    if (!label || !capability) {
      throw new TypeError(`Plugin channel video tab label and capability are required: ${plugin.id}`);
    }
    if (typeof definition.count !== 'function' || typeof definition.load !== 'function') {
      throw new TypeError(`Plugin channel video tab count and load are required: ${plugin.id}`);
    }
    tabIds.add(tabId);
  }
}

function browserVideoFacetDefinition(plugin) {
  const definition = plugin?.search?.videoFacet;
  return definition && typeof definition === 'object' ? definition : null;
}

function browserClipFacetDefinition(plugin) {
  const definition = plugin?.search?.clipFacet;
  return definition && typeof definition === 'object' ? definition : null;
}

function defaultBrowserVideoFacetVisibility(plugin) {
  const definition = browserVideoFacetDefinition(plugin);
  return {
    present: !filterPreferenceEnabled(String(definition?.presentDisabledPreferenceKey || '')),
    absent: !filterPreferenceEnabled(String(definition?.absentDisabledPreferenceKey || '')),
  };
}

function defaultBrowserClipFacetVisibility(plugin) {
  const definition = browserClipFacetDefinition(plugin);
  return {
    present: !filterPreferenceEnabled(String(definition?.presentDisabledPreferenceKey || '')),
    absent: !filterPreferenceEnabled(String(definition?.absentDisabledPreferenceKey || '')),
  };
}

function registerBrowserPlugin(plugin) {
  if (!plugin || typeof plugin !== 'object') throw new TypeError('Plugin registration is required');
  const pluginId = String(plugin.id || '');
  if (!/^[a-z][a-z0-9_-]*$/.test(pluginId)) throw new TypeError('Plugin id is invalid');
  if (browserPlugins.has(pluginId)) throw new Error(`Plugin is already registered: ${pluginId}`);
  const searchFieldRegistration = validateBrowserPluginSearchField(plugin);
  validateBrowserChannelVideoTabs(plugin);
  if (plugin.entityCards) EntityCardExtensions.validateDefinition(plugin.entityCards);
  if (plugin.search?.resultPresentation) {
    SearchResultPresentations.validateDefinition(plugin.search.resultPresentation);
  }
  if (plugin.search) {
    registerBrowserPluginSearchField(plugin, searchFieldRegistration);
    const videoFacet = browserVideoFacetDefinition(plugin);
    const clipFacet = browserClipFacetDefinition(plugin);
    if (videoFacet) {
      pluginVideoFacetVisibility.set(
        pluginId,
        defaultBrowserVideoFacetVisibility(plugin),
      );
    }
    if (clipFacet) {
      pluginClipFacetVisibility.set(
        pluginId,
        defaultBrowserClipFacetVisibility(plugin),
      );
    }
    if (!videoFacet && !clipFacet) {
      pluginSearchVisibility.set(
        pluginId,
        filterPreferenceEnabled(String(plugin.search.preferenceKey || '')),
      );
    }
  }
  browserPlugins.set(pluginId, plugin);
}

window.YTLibraryBrowserPlugins = Object.freeze({
  apiVersion: 2,
  features: Object.freeze({
    channelVideoTabs: 1,
    entityCards: 1,
    pluginJsonMutations: 1,
    searchResultPresentations: 1,
  }),
  register: registerBrowserPlugin,
});

function browserPluginStatus(pluginId) {
  return (data?.plugins || []).find(plugin => plugin.id === pluginId) || null;
}

function browserPluginSupports(pluginId, capability) {
  const status = browserPluginStatus(pluginId);
  return Boolean(
    status
    && status.enabled
    && status.state === 'ready'
    && (!capability || (status.capabilities || []).includes(capability))
  );
}

function browserChannelVideoTabKey(pluginId, tabId) {
  return `plugin-${pluginId}-${tabId}`;
}

function browserChannelVideoTabs() {
  return [...browserPlugins.values()].flatMap(plugin => (
    browserChannelVideoTabDefinitions(plugin)
      .filter(definition => browserPluginSupports(plugin.id, definition.capability))
      .map(definition => ({
        definition,
        key: browserChannelVideoTabKey(plugin.id, definition.id),
        plugin,
      }))
  ));
}

function browserChannelVideoTab(key) {
  return browserChannelVideoTabs().find(tab => tab.key === key) || null;
}

function browserSearchPlugins() {
  return [...browserPlugins.values()].filter(plugin => (
    plugin.search
    && browserPluginSupports(plugin.id, plugin.search.capability)
  ));
}

function browserSearchPlugin(pluginId) {
  return browserSearchPlugins().find(plugin => plugin.id === pluginId) || null;
}

function browserVideoFilterPlugins() {
  return browserSearchPlugins().filter(plugin => Boolean(browserVideoFacetDefinition(plugin)));
}

function browserClipFilterPlugins() {
  return browserSearchPlugins().filter(plugin => Boolean(browserClipFacetDefinition(plugin)));
}

function browserVideoSearchFieldPlugins() {
  return browserVideoFilterPlugins().filter(plugin => (
    browserPluginSearchFieldEnabled(plugin) && searchKindEnabled('videos')
  ));
}

function browserClipSearchFieldPlugins() {
  return browserClipFilterPlugins().filter(plugin => (
    browserPluginSearchFieldEnabled(plugin) && searchKindEnabled('clips')
  ));
}

function browserSearchFieldAppliesToCurrentContext(plugin) {
  const applicableKinds = browserSearchFieldApplicableKinds(plugin);
  if (!applicableKinds) return true;
  const contextKind = selected.startsWith('__channel__:')
    ? 'videos'
    : searchContextKind();
  if (contextKind) return applicableKinds.has(contextKind);
  return selectedSearchKinds().some(kind => applicableKinds.has(kind));
}

function syncBrowserPluginSearchFieldVisibility() {
  for (const plugin of browserPlugins.values()) {
    if (!browserSearchFieldDefinition(plugin)) continue;
    const label = searchInFields.querySelector(
      `[data-browser-plugin-search-field="${plugin.id}"]`
    );
    if (!(label instanceof HTMLLabelElement)) continue;
    const input = label.querySelector('input.search-field');
    const applies = selected !== '__history__' && browserSearchFieldAppliesToCurrentContext(plugin);
    label.hidden = !applies;
    label.style.display = applies ? '' : 'none';
    if (input instanceof HTMLInputElement) input.disabled = !applies;
  }
}

function browserVideoFacetState(plugin) {
  return pluginVideoFacetVisibility.get(plugin.id) || { present: false, absent: false };
}

function browserVideoFacetSearchActive(plugin) {
  const state = browserVideoFacetState(plugin);
  return state.present && !state.absent;
}

function browserClipFacetState(plugin) {
  return pluginClipFacetVisibility.get(plugin.id) || { present: false, absent: false };
}

function browserClipFacetSearchActive(plugin) {
  const state = browserClipFacetState(plugin);
  return state.present && !state.absent;
}

function browserPluginStateKey(plugin) {
  const parts = [plugin.id];
  if (browserVideoFacetDefinition(plugin)) {
    const state = browserVideoFacetState(plugin);
    parts.push(`v${state.present ? '1' : '0'}${state.absent ? '1' : '0'}`);
  }
  if (browserClipFacetDefinition(plugin)) {
    const state = browserClipFacetState(plugin);
    parts.push(`c${state.present ? '1' : '0'}${state.absent ? '1' : '0'}`);
  }
  return parts.length > 1 ? parts.join(':') : (searchKindEnabled(plugin.id) ? plugin.id : '');
}

function saveBrowserVideoFacetPreferences(plugin) {
  const definition = browserVideoFacetDefinition(plugin);
  const state = browserVideoFacetState(plugin);
  if (definition.presentDisabledPreferenceKey) {
    saveFilterPreference(definition.presentDisabledPreferenceKey, !state.present);
  }
  if (definition.absentDisabledPreferenceKey) {
    saveFilterPreference(definition.absentDisabledPreferenceKey, !state.absent);
  }
}

function saveBrowserClipFacetPreferences(plugin) {
  const definition = browserClipFacetDefinition(plugin);
  const state = browserClipFacetState(plugin);
  if (definition.presentDisabledPreferenceKey) {
    saveFilterPreference(definition.presentDisabledPreferenceKey, !state.present);
  }
  if (definition.absentDisabledPreferenceKey) {
    saveFilterPreference(definition.absentDisabledPreferenceKey, !state.absent);
  }
}

function browserResultSearchPlugins() {
  return browserSearchPlugins().filter(plugin => (
    !browserVideoFacetDefinition(plugin)
    && !browserClipFacetDefinition(plugin)
    && typeof plugin.search.fetch === 'function'
  ));
}

function browserPluginAssetUrl(pluginId, path, version = '') {
  const encodedPath = String(path || '').split('/').map(encodeURIComponent).join('/');
  const baseUrl = `/plugins/${encodeURIComponent(pluginId)}/assets/${encodedPath}`;
  return version ? `${baseUrl}?v=${encodeURIComponent(version)}` : baseUrl;
}

async function loadBrowserPluginAsset(pluginId, asset, version = '') {
  const path = String(asset?.path || '');
  const type = String(asset?.type || '');
  if (!path || !['script', 'style'].includes(type)) return;
  const url = browserPluginAssetUrl(pluginId, path, version);
  if (loadedBrowserPluginAssets.has(url)) return;
  const element = type === 'style'
    ? document.createElement('link')
    : document.createElement('script');
  if (type === 'style') {
    element.rel = 'stylesheet';
    element.href = url;
  } else {
    element.src = url;
  }
  await new Promise((resolve, reject) => {
    element.addEventListener('load', resolve, { once: true });
    element.addEventListener('error', () => reject(new Error(`Plugin asset failed: ${url}`)), { once: true });
    document.head.append(element);
  });
  loadedBrowserPluginAssets.add(url);
}

async function loadBrowserPlugins(statuses) {
  for (const status of statuses || []) {
    if (!status.enabled || status.state !== 'ready') continue;
    for (const asset of status.browserAssets || []) {
      try {
        await loadBrowserPluginAsset(status.id, asset, status.version);
      } catch (error) {
        console.error(`Plugin asset failed: ${status.id}`, error);
      }
    }
  }
}

let selected = '';
function defaultPlaylistVideoVisibility() {
  return {
    public: true,
    unlisted: true,
    private: true,
    members_only: true,
    unavailable: filterPreferenceEnabled(filterPreferenceKeys.unavailablePlaylistVideos),
    unknown: true,
    removed: filterPreferenceEnabled(filterPreferenceKeys.removedPlaylistVideos),
  };
}
let playlistVisibility = defaultPlaylistVideoVisibility();
let partialCompletionMinimumPercent = boundedPartialMinimumPercent(
  pageConfig.partialCompletionMinPercent
);
function defaultPartialBelowMinimumEnabled() {
  return partialCompletionMinimumPercent <= 1
    || filterPreferenceEnabled(filterPreferenceKeys.lowPartialCompletion);
}
let playlistCompletionVisibility = {
  complete: true,
  partial: true,
  partial_below_minimum: defaultPartialBelowMinimumEnabled(),
  unknown: true,
  never_watched: true,
};
let playlistDuplicatesOnly = false;
let completionMinimumInputTimer = null;
const noUploaderCategoryFilter = '__no_category__';
const defaultSearchMetaVisibility = {
  videoType: { video: true, short: true, livestream: true, movie: true, unknown: true },
  broadcastStatus: { live: true, ended: true, upcoming: true, unknown: true },
  aiDisclosure: { made_with_ai: true, no_disclosure: true, unknown: true },
  videos: {
    public: true,
    unlisted: true,
    private: true,
    members_only: true,
    unavailable: filterPreferenceEnabled(filterPreferenceKeys.unavailableVideos),
    unknown: true,
    removed: false,
  },
  reactions: { none: true, liked: true, disliked: true },
  completion: {
    complete: true,
    partial: true,
    partial_below_minimum: defaultPartialBelowMinimumEnabled(),
    unknown: true,
    never_watched: true,
  },
  membership: { member: true, non_member: true },
  uploaderCategory: { [noUploaderCategoryFilter]: true },
  clipOwnership: { mine: true, others: true, ownership_unknown: true },
  channelSubscription: { subscribed: true, non_subscribed: true },
  channelStatus: {
    active: true,
    terminated: filterPreferenceEnabled(filterPreferenceKeys.terminatedChannels),
  },
  playlistAvailability: {
    private: true,
    public: true,
    unlisted: true,
    unavailable: filterPreferenceEnabled(filterPreferenceKeys.unavailablePlaylists),
    unknown: true,
  },
  playlistOwnership: { mine: true, others: true, ownership_unknown: true },
  videoNotes: { with_note: true, without_note: true },
  clipNotes: { with_note: true, without_note: true },
  playlistNotes: { with_note: true, without_note: true },
  channelNotes: { with_note: true, without_note: true },
};
let searchMetaVisibility = Object.fromEntries(
  Object.entries(defaultSearchMetaVisibility).map(([groupName, visibility]) => [
    groupName,
    { ...visibility },
  ])
);
let uploaderCategorySelectionExplicit = false;
const searchMetaParamNames = {
  videoType: 'vt',
  broadcastStatus: 'vbs',
  aiDisclosure: 'vai',
  videos: 'vm',
  reactions: 'vr',
  completion: 'vc',
  membership: 'vpm',
  uploaderCategory: 'vuc',
  clipOwnership: 'co',
  channelSubscription: 'csub',
  channelStatus: 'cstatus',
  playlistAvailability: 'pm',
  playlistOwnership: 'po',
  videoNotes: 'vn',
  clipNotes: 'cn',
  playlistNotes: 'pn',
  channelNotes: 'chn',
};
const searchOptInMetaFilters = [
  {
    groupName: 'videos', key: 'unavailable', paramName: 'unavailable',
    preferenceKey: filterPreferenceKeys.unavailableVideos,
  },
  {
    groupName: 'completion', key: 'partial_below_minimum', paramName: 'partial_below',
    preferenceKey: filterPreferenceKeys.lowPartialCompletion,
  },
  {
    groupName: 'playlistAvailability', key: 'unavailable', paramName: 'playlist_unavailable',
    preferenceKey: filterPreferenceKeys.unavailablePlaylists,
  },
  {
    groupName: 'channelStatus', key: 'terminated', paramName: 'terminated',
    preferenceKey: filterPreferenceKeys.terminatedChannels,
  },
];
const playlistVideoOptInFilters = [
  {
    key: 'unavailable',
    preferenceKey: filterPreferenceKeys.unavailablePlaylistVideos,
  },
  {
    key: 'removed',
    preferenceKey: filterPreferenceKeys.removedPlaylistVideos,
  },
];
const searchSortOptions = new Set([
  'relevance', 'title', 'title_desc', 'newest', 'oldest', 'most_watched', 'type',
]);
const playlistVideoSortOptions = new Set([
  'newest_added', 'title', 'title_desc', 'oldest_added', 'most_watched', 'playlist_order',
]);
const sortPreferences = { ...(pageConfig.sortPreferences || {}) };
let searchResultsSort = 'newest';
let searchSortExplicit = false;
let activeSearchPreset = '';
let activeSearchScope = '';
let searchPlaylistGroupKey = '';
let searchChannelGroupKey = '';
const cardLayouts = new Set(['grid', 'detailed', 'compact']);
const cardLayoutPreferences = {
  search: cardLayouts.has(pageConfig.searchCardLayout) ? pageConfig.searchCardLayout : 'grid',
  playlist: cardLayouts.has(pageConfig.playlistCardLayout) ? pageConfig.playlistCardLayout : 'grid',
  history: cardLayouts.has(pageConfig.historyCardLayout) ? pageConfig.historyCardLayout : 'compact',
  'channel-playlisted-videos': cardLayouts.has(pageConfig.channelPlaylistedVideoCardLayout)
    ? pageConfig.channelPlaylistedVideoCardLayout
    : 'grid',
  'channel-playlists': cardLayouts.has(pageConfig.channelPlaylistCardLayout)
    ? pageConfig.channelPlaylistCardLayout
    : 'grid',
  'channel-history': cardLayouts.has(pageConfig.channelHistoryCardLayout)
    ? pageConfig.channelHistoryCardLayout
    : 'detailed',
};
const cardLayoutSaveChains = Object.fromEntries(
  Object.keys(cardLayoutPreferences).map(context => [context, Promise.resolve()]),
);
const cardLayoutSaveVersions = Object.fromEntries(
  Object.keys(cardLayoutPreferences).map(context => [context, 0]),
);
let sortPreferenceSaveChain = Promise.resolve();
const sortPreferenceSaveVersions = new Map();
let pageSizeSaveChain = Promise.resolve();
let pageSizeSaveVersion = 0;
let partialCompletionMinimumSaveChain = Promise.resolve();
let partialCompletionMinimumSaveVersion = 0;
let filterPreferenceSaveChain = Promise.resolve();
const filterPreferenceSaveVersions = new Map();
let searchFilterTreeSaveChain = Promise.resolve();
let searchFilterTreeSaveVersion = 0;
let navigationGroupTreeSaveChain = Promise.resolve();
let navigationGroupTreeSaveVersion = 0;
const searchPresetDefinitions = {
  videos: { kind: 'videos', sort: 'newest' },
  clips: { kind: 'clips', sort: 'newest' },
  playlists: { kind: 'playlists', sort: 'title' },
  channels: { kind: 'channels', sort: 'title' },
  'playlist-group': { kind: 'playlists', sort: 'title' },
  'channel-group': { kind: 'channels', sort: 'title' },
};

function searchPresetHref(preset, groupKey = '') {
  const definition = searchPresetDefinition(preset);
  if (!definition) return '/search';
  const scope = ['videos', 'clips', 'playlists', 'channels'].includes(definition.kind)
    ? definition.kind
    : '';
  const base = scope ? `/${scope}` : '/search';
  const params = new URLSearchParams();
  if (preset !== scope) params.set('view', preset);
  if (groupKey) params.set('group', groupKey);
  if (definition.preserveQuery) {
    const query = search.value.trim();
    if (query) params.set('q', query);
  }
  return appendUrlParams(base, params);
}

function handleSidebarLinkClick(event, navigate) {
  if (
    event.defaultPrevented
    || event.button !== 0
    || event.altKey
    || event.ctrlKey
    || event.metaKey
    || event.shiftKey
  ) return;
  event.preventDefault();
  navigate();
}

function searchPresetDefinition(presetId) {
  return searchPresetDefinitions[presetId] || null;
}

let playlistVisibilityPlaylistId = '';
let playlistViewSort = playlistVideoSortOptions.has(sortPreferences.playlist)
  ? sortPreferences.playlist
  : 'playlist_order';
let currentPage = 1;
let renderedPageInfo = { page: 1, pageCount: 1, total: 0 };
let pageBoundaryNavigationPending = false;
let pendingPageBoundaryLanding = '';
let pageBoundaryInputArmed = true;
let pageBoundaryInputTimer = null;
let pageBoundaryFallbackTimer = null;
let pageBoundaryTouchY = null;
let pageBoundaryTouchDistance = 0;
let pageBoundaryTouchDirection = 0;
let pageBoundaryTouchTargetAllowed = false;
let pageSize = String(pageConfig.pageSize || 100);
const adjacentPageCacheLimit = 6;
const historyActivityCacheLimit = 4;
const viewDataCacheLimit = 24;
const channelTabCountCacheLimit = 128;
let historyPageCache = new Map();
let historyActivityCache = new Map();
let historyActivityYearOffset = 0;
let historyActivitySyncEnabled = true;
let omniSearchCache = new Map();
let viewDataCache = new Map();
let adjacentPagePrefetchCancel = null;
let adjacentPagePrefetchGeneration = 0;
let videoMetaCountsCache = new Map();
let videoTypeCountsCache = new Map();
let videoBroadcastStatusCountsCache = new Map();
let videoAiDisclosureCountsCache = new Map();
let videoCompletionCountsCache = new Map();
let videoReactionCountsCache = new Map();
let videoUploaderCategoryCountsCache = new Map();
let videoNoteCountsCache = new Map();
let videoPluginFacetCountsCache = new Map();
let omniMetaCountsCache = new Map();
let omniVideoTypeCountsCache = new Map();
let omniBroadcastStatusCountsCache = new Map();
let omniAiDisclosureCountsCache = new Map();
let omniVideoPluginFacetCountsCache = new Map();
let omniClipPluginFacetCountsCache = new Map();
let omniReactionCountsCache = new Map();
let omniCompletionCountsCache = new Map();
let omniPlaylistMembershipCountsCache = new Map();
let omniUploaderCategoryCountsCache = new Map();
let pendingHistoryDate = '';
let historyNavigationDate = '';
let channelHistoryCounts = new Map();
let channelTabCountCache = new Map();
let channelDetailTab = 'playlisted-videos';
let channelDetailSearchActive = false;
let historyHeatmapDayFrame = null;
let historyHeatmapScrollSyncToken = 0;
let historyHeatmapScrollSyncRequest = null;
let renderGeneration = 0;
let renderedOmniSearchQuery = '';
let searchResultsRendered = false;
let retainedSearchUrl = loadRetainedSearchUrl();
let searchMetaProgressTimer = null;
let pendingSearchMetaGroups = new Set();
let searchMetaProgressDots = '';
let loadingStatusEpoch = 0;
let loadingStatusToken = 0;
let loadingStatusTimer = null;
let activeLoadingStatusTokens = new Set();
let searchInputTimer = null;
let renderedSearchFilterContext = null;
let renderedSearchFilterPayload = {};
const search = document.getElementById('search');
const searchNav = document.getElementById('search-nav');
const historyNav = document.getElementById('history-nav');
const searchFilters = document.getElementById('search-filters');
const searchInFields = document.getElementById('search-in-fields');
const refresh = document.getElementById('refresh');
const groupsEl = document.getElementById('groups');
const searchFilterTree = document.getElementById('search-for-filters');
const searchFilterRegion = groupsEl.parentElement;
const searchFields = [...document.querySelectorAll('.search-field')];

function loadRetainedSearchUrl() {
  try {
    const value = window.sessionStorage.getItem('yt-library-retained-search-url') || '';
    return value.startsWith('/search') ? value : '/search';
  } catch (_error) {
    return '/search';
  }
}

function rememberSearchUrl(href) {
  if (!href.startsWith('/search')) return;
  retainedSearchUrl = href;
  try {
    window.sessionStorage.setItem('yt-library-retained-search-url', href);
  } catch (_error) {
    // The current page still retains the search when session storage is unavailable.
  }
}
const grid = document.getElementById('grid');
const empty = document.getElementById('empty');
const title = document.getElementById('view-title');
const meta = document.getElementById('view-meta');
const bottomPager = document.getElementById('bottom-pager');
const resultsScroll = document.querySelector('.results-scroll');
const viewTop = document.getElementById('view-top');
const viewContext = document.getElementById('view-context');
const loadingStatus = document.getElementById('loading-status');

async function loadData({ preserveSearchContent = false } = {}) {
  const loadingToken = beginLoadingStatus();
  try {
    cancelAdjacentPagePrefetch();
    refresh.disabled = true;
    refresh.textContent = 'Refreshing';
    const response = await fetch('/api/bootstrap', { cache: 'no-store' });
    if (!response.ok) throw new Error(`Data refresh failed: ${response.status}`);
    data = await response.json();
    await loadBrowserPlugins(data.plugins || []);
    historyPageCache = new Map();
    historyActivityCache = new Map();
    omniSearchCache = new Map();
    if (!preserveSearchContent) {
      renderedOmniSearchQuery = '';
      searchResultsRendered = false;
    }
    viewDataCache = new Map();
    videoMetaCountsCache = new Map();
    videoTypeCountsCache = new Map();
    videoBroadcastStatusCountsCache = new Map();
    videoAiDisclosureCountsCache = new Map();
    videoCompletionCountsCache = new Map();
    videoReactionCountsCache = new Map();
    videoUploaderCategoryCountsCache = new Map();
    videoNoteCountsCache = new Map();
    videoPluginFacetCountsCache = new Map();
    omniMetaCountsCache = new Map();
    omniVideoTypeCountsCache = new Map();
    omniBroadcastStatusCountsCache = new Map();
    omniAiDisclosureCountsCache = new Map();
    omniVideoPluginFacetCountsCache = new Map();
    omniClipPluginFacetCountsCache = new Map();
    omniReactionCountsCache = new Map();
    omniCompletionCountsCache = new Map();
    omniPlaylistMembershipCountsCache = new Map();
    omniUploaderCategoryCountsCache = new Map();
    channelHistoryCounts = new Map();
    channelTabCountCache = new Map();
    playlistMemberships = new Map();
    for (const item of data.memberships || []) {
      if (!playlistMemberships.has(item.group_key)) playlistMemberships.set(item.group_key, []);
      playlistMemberships.get(item.group_key).push(item.playlist_id);
    }
    playlistChildren = new Map();
    for (const group of data.groups || []) {
      const parent = group.parent_key || '';
      if (!playlistChildren.has(parent)) playlistChildren.set(parent, []);
      playlistChildren.get(parent).push(group);
    }
    channelMemberships = new Map();
    for (const item of data.channelMemberships || []) {
      if (!channelMemberships.has(item.group_key)) channelMemberships.set(item.group_key, []);
      channelMemberships.get(item.group_key).push(item.channel_id);
    }
    channelChildren = new Map();
    for (const group of data.channelGroups || []) {
      const parent = group.parent_key || '';
      if (!channelChildren.has(parent)) channelChildren.set(parent, []);
      channelChildren.get(parent).push(group);
    }
    selected = selectionFromLocation();
    renderGroups();
    await render();
    refresh.disabled = false;
    refresh.textContent = 'Refresh';
  } finally {
    finishLoadingStatus(loadingToken);
  }
}

function trimRequestCache(cache, maxEntries) {
  if (cache.size <= maxEntries) return;
  for (const [key, entry] of cache) {
    if (cache.size <= maxEntries) break;
    if (!entry?.promise) cache.delete(key);
  }
}

function cachedRequest(cache, key, load, maxEntries) {
  const cached = cache.get(key);
  if (cached?.data) {
    cache.delete(key);
    cache.set(key, cached);
    return Promise.resolve(cached.data);
  }
  if (cached?.promise) return cached.promise;
  const promise = Promise.resolve().then(load).then(payload => {
    const current = cache.get(key);
    if (!current || current.promise === promise) {
      cache.delete(key);
      cache.set(key, { data: payload, promise: null });
      trimRequestCache(cache, maxEntries);
    }
    return payload;
  }).catch(error => {
    if (cache.get(key)?.promise === promise) cache.delete(key);
    throw error;
  });
  cache.set(key, { data: null, promise });
  trimRequestCache(cache, maxEntries);
  return promise;
}

async function fetchViewData(path) {
  return cachedRequest(viewDataCache, path, async () => {
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `View fetch failed: ${response.status}`);
    }
    return response.json();
  }, viewDataCacheLimit);
}

function remoteListPath(path, params = {}, page = currentPage, limitOverride = null) {
  const size = pageSizeNumber();
  const requestedLimit = Number(limitOverride);
  const limit = Number.isFinite(requestedLimit) && requestedLimit > 0
    ? Math.min(500, Math.floor(requestedLimit))
    : (Number.isFinite(size) ? size : 500);
  const requestedPage = Math.max(1, Number(page) || 1);
  const queryParams = new URLSearchParams(params);
  queryParams.set('limit', String(limit));
  queryParams.set('offset', String((requestedPage - 1) * limit));
  return `${path}?${queryParams}`;
}

function playlistSelection(playlistId) {
  return `__playlist__:${playlistId}`;
}

function videoSelection(videoId) {
  return `__video__:${videoId}`;
}

function clipSelection(clipId) {
  return `__clip__:${clipId}`;
}

function channelSelection(channelId) {
  return `__channel__:${channelId}`;
}

function paginationParams() {
  const params = new URLSearchParams();
  if (historyNavigationDate && historyDateNavigationIsActive()) {
    params.set('date', historyNavigationDate);
  } else if (currentPage > 1) {
    params.set('page', String(currentPage));
  }
  return params;
}

function appendUrlParams(base, params) {
  const query = params.toString();
  return query ? `${base}?${query}` : base;
}

function currentBrowserUrl() {
  return `${window.location.pathname}${window.location.search}`;
}

const browserHistoryIndexKey = 'ytLibraryHistoryIndex';
let browserHistoryIndex = Number.isInteger(history.state?.[browserHistoryIndexKey])
  ? history.state[browserHistoryIndexKey]
  : 0;
history.replaceState(
  { ...(history.state || {}), [browserHistoryIndexKey]: browserHistoryIndex },
  '',
  currentBrowserUrl(),
);
let acceptedBrowserHistoryIndex = browserHistoryIndex;
let acceptedBrowserUrl = currentBrowserUrl();
let restoringAcceptedBrowserHistory = false;

function setBrowserUrl(href, replace = false) {
  if (currentBrowserUrl() === href) return false;
  if (!confirmDiscardUnsavedNotes()) {
    selected = selectionFromLocation();
    return true;
  }
  if (replace) {
    history.replaceState(
      { ...(history.state || {}), [browserHistoryIndexKey]: browserHistoryIndex },
      '',
      href,
    );
  } else {
    browserHistoryIndex += 1;
    history.pushState({ [browserHistoryIndexKey]: browserHistoryIndex }, '', href);
  }
  acceptedBrowserHistoryIndex = browserHistoryIndex;
  acceptedBrowserUrl = href;
  if (!replace) handleBrowserLocationChange();
  return true;
}

function historyDateParam(value) {
  const date = String(value || '').trim();
  return /^\d{4}-\d{2}-\d{2}$/.test(date) ? date : '';
}

function historyDateNavigationIsActive() {
  return selected === '__history__'
    || (selected.startsWith('__channel__:') && channelDetailTab === 'history');
}

function historyCardViewportBounds() {
  if (usesDocumentPageScrolling()) {
    return { top: 0, bottom: window.innerHeight };
  }
  if (!(resultsScroll instanceof HTMLElement)) return null;
  const bounds = resultsScroll.getBoundingClientRect();
  return { top: bounds.top, bottom: bounds.bottom };
}

function firstVisibleHistoryCardDate() {
  const viewport = historyCardViewportBounds();
  if (!viewport || viewport.bottom <= viewport.top) return '';
  const wholeCardTolerance = 1;
  let firstIntersectingDate = '';
  for (const card of grid.querySelectorAll('.history-card[data-watch-date]')) {
    const bounds = card.getBoundingClientRect();
    if (bounds.bottom <= viewport.top || bounds.top >= viewport.bottom) continue;
    const date = card.dataset.watchDate || '';
    if (!firstIntersectingDate) firstIntersectingDate = date;
    if (
      bounds.top >= viewport.top - wholeCardTolerance
      && bounds.bottom <= viewport.bottom + wholeCardTolerance
    ) return date;
  }
  return firstIntersectingDate;
}

function setHistoryHeatmapCurrentDate(heatmap, date) {
  const current = heatmap.querySelector('.history-heatmap-day[aria-current="date"]');
  if (current?.dataset.historyDate === date) return true;
  current?.removeAttribute('aria-current');
  if (!date) return false;
  const cell = heatmap.querySelector(
    `.history-heatmap-day[data-history-date="${CSS.escape(date)}"]`,
  );
  if (!(cell instanceof HTMLButtonElement)) return false;
  cell.setAttribute('aria-current', 'date');
  return true;
}

function cancelHistoryHeatmapScrollSync() {
  historyHeatmapScrollSyncToken += 1;
  historyHeatmapScrollSyncRequest = null;
}

async function syncHistoryHeatmapToVisibleDate(heatmap, date, targetOffset) {
  if (
    historyHeatmapScrollSyncRequest?.heatmap === heatmap
    && historyHeatmapScrollSyncRequest.targetOffset === targetOffset
  ) return;
  const token = ++historyHeatmapScrollSyncToken;
  const generation = renderGeneration;
  historyHeatmapScrollSyncRequest = { heatmap, targetOffset, token };
  try {
    const channelId = heatmap.dataset.historyChannelId || '';
    const activity = await withLoadingStatus(() => fetchHistoryActivity(channelId, targetOffset));
    if (
      token !== historyHeatmapScrollSyncToken
      || generation !== renderGeneration
      || !heatmap.isConnected
      || !historyHeatmapIsCurrent(heatmap)
    ) return;
    const visibleDate = firstVisibleHistoryCardDate();
    if (historyActivityYearOffsetForDate(visibleDate) !== targetOffset) {
      historyHeatmapScrollSyncRequest = null;
      scheduleHistoryHeatmapCurrentDay();
      return;
    }
    historyActivityYearOffset = targetOffset;
    const replacement = historyHeatmapFor(activity);
    heatmap.replaceWith(replacement);
    setHistoryHeatmapCurrentDate(replacement, visibleDate || date);
  } catch (error) {
    if (token === historyHeatmapScrollSyncToken) {
      console.warn('History heatmap scroll sync failed', error);
    }
  } finally {
    if (historyHeatmapScrollSyncRequest?.token === token) {
      historyHeatmapScrollSyncRequest = null;
    }
  }
}

function updateHistoryHeatmapCurrentDay() {
  historyHeatmapDayFrame = null;
  if (!historyDateNavigationIsActive()) return;
  const heatmap = viewContext.querySelector('.history-heatmap');
  if (!(heatmap instanceof HTMLElement)) return;
  const date = firstVisibleHistoryCardDate();
  if (setHistoryHeatmapCurrentDate(heatmap, date)) {
    if (historyHeatmapScrollSyncRequest) cancelHistoryHeatmapScrollSync();
    return;
  }
  if (!date || !historyActivitySyncEnabled) return;
  const targetOffset = historyActivityYearOffsetForDate(date);
  if (targetOffset === historyActivityYearOffset) return;
  void syncHistoryHeatmapToVisibleDate(heatmap, date, targetOffset);
}

function scheduleHistoryHeatmapCurrentDay() {
  if (historyHeatmapDayFrame !== null) return;
  historyHeatmapDayFrame = requestAnimationFrame(updateHistoryHeatmapCurrentDay);
}

function applyPaginationParams(params, allowHistoryDate = false) {
  historyNavigationDate = allowHistoryDate ? historyDateParam(params.get('date')) : '';
  if (historyNavigationDate) {
    currentPage = 1;
    pendingHistoryDate = historyNavigationDate;
    historyActivityYearOffset = historyActivityYearOffsetForDate(historyNavigationDate);
    return;
  }
  const page = Number(params.get('page') || 1);
  currentPage = Number.isFinite(page) && page > 0 ? page : 1;
}

function currentLocationHasPaginationParams() {
  const params = new URLSearchParams(window.location.search);
  return params.has('page') || params.has('size');
}

function localPlaylistHref(playlistId, includePagination = false) {
  const base = `/playlists/${encodeURIComponent(playlistId)}`;
  return includePagination ? appendUrlParams(base, paginationParams()) : base;
}

function localVideoHref(videoId, includePagination = false) {
  const base = `/videos/${encodeURIComponent(videoId)}`;
  return includePagination ? appendUrlParams(base, paginationParams()) : base;
}

function localClipHref(clipId) {
  return `/clips/${encodeURIComponent(clipId)}`;
}

function encodeChannelReference(channelReference) {
  return encodeURIComponent(channelReference).replace(/%40/gi, '@');
}

function localChannelHref(channelId, includePagination = false) {
  const base = `/channels/${encodeChannelReference(channelId)}`;
  return includePagination ? appendUrlParams(base, paginationParams()) : base;
}

function channelDetailParams() {
  if (channelDetailSearchActive) {
    const params = new URL(searchUrl(), window.location.origin).searchParams;
    for (const key of ['view', 'group', 'sort', 'page', 'removed', 'duplicates']) {
      params.delete(key);
    }
    if (channelDetailTab !== 'playlisted-videos') params.set('tab', channelDetailTab);
    if (currentPage > 1) params.set('page', String(currentPage));
    return params;
  }
  if (channelDetailTab === 'history') {
    const params = new URLSearchParams();
    params.set('tab', 'history');
    if (historyNavigationDate && historyDateNavigationIsActive()) {
      params.set('date', historyNavigationDate);
    } else if (currentPage > 1) {
      params.set('page', String(currentPage));
    }
    return params;
  }
  const params = paginationParams();
  if (channelDetailTab !== 'playlisted-videos') params.set('tab', channelDetailTab);
  return params;
}

function channelDetailTabFromParams(params) {
  if (params.get('tab') === 'history' || historyDateParam(params.get('date'))) {
    return 'history';
  }
  const requested = params.get('tab') || '';
  if (requested === 'playlists') return 'playlists';
  return browserChannelVideoTab(requested) ? requested : 'playlisted-videos';
}

function localViewHref(value, includePagination = false) {
  if (value === '__search__') return searchUrl();
  if (value !== '__history__') {
    return includePagination ? appendUrlParams('/search', paginationParams()) : '/search';
  }
  const params = includePagination ? paginationParams() : new URLSearchParams();
  const query = selected === '__history__' ? search.value.trim() : '';
  if (query) params.set('q', query);
  const fields = selected === '__history__'
    ? historySearchFieldParamValue()
    : 'descriptions,titles';
  if (fields.split(',').filter(Boolean).length !== 2) {
    params.set('in', fields || '__none__');
  }
  return appendUrlParams('/history', params);
}

function searchFieldParamValue() {
  return [...activeSearchFields()].sort().join(',');
}

function historySearchFields() {
  return new Set(
    searchFields
      .filter(input => ['titles', 'descriptions'].includes(input.dataset.searchField))
      .filter(input => input.checked)
      .map(input => input.dataset.searchField)
  );
}

function historySearchFieldParamValue() {
  return [...historySearchFields()].sort().join(',');
}

function applyHistorySearchFieldLocation(params) {
  const value = params.get('in');
  const active = new Set(
    value === null
      ? ['titles', 'descriptions']
      : (value === '__none__' ? [] : value.split(',').filter(Boolean))
  );
  for (const input of searchFields) {
    if (!['titles', 'descriptions'].includes(input.dataset.searchField)) continue;
    input.checked = active.has(input.dataset.searchField);
  }
}

function metaFilterParamValue(visibility, excludedKeys = []) {
  const excluded = new Set(excludedKeys);
  const selected = Object.entries(visibility)
    .filter(([key, enabled]) => enabled && !excluded.has(key))
    .map(([key]) => key);
  return selected.length ? selected.join(',') : '__none__';
}

function boundedPartialMinimumPercent(value) {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  return Number.isFinite(parsed) ? Math.max(1, Math.min(99, parsed)) : 1;
}

function setPartialCompletionMinimum(value) {
  const previousMinimum = partialCompletionMinimumPercent;
  const nextMinimum = boundedPartialMinimumPercent(value);
  partialCompletionMinimumPercent = nextMinimum;
  if (previousMinimum <= 1 || nextMinimum <= 1) {
    const belowMinimumEnabled = defaultPartialBelowMinimumEnabled();
    defaultSearchMetaVisibility.completion.partial_below_minimum = belowMinimumEnabled;
    searchMetaVisibility.completion.partial_below_minimum = belowMinimumEnabled;
    playlistCompletionVisibility.partial_below_minimum = belowMinimumEnabled;
  }
  return previousMinimum;
}

function allMetaFiltersEnabled(visibility, excludedKeys = []) {
  const excluded = new Set(excludedKeys);
  return Object.entries(visibility)
    .filter(([key]) => !excluded.has(key))
    .every(([, enabled]) => enabled);
}

function searchMetaPresetBaseline(groupName, preset = activeSearchPreset) {
  const visibility = searchMetaVisibility[groupName] || {};
  const baseline = Object.fromEntries(
    Object.keys(visibility).map(key => [key, false])
  );
  const definition = searchPresetDefinition(preset);
  if (!definition || searchKindFacetKeys(definition.kind).includes(groupName)) {
    for (const key of Object.keys(baseline)) {
      baseline[key] = defaultSearchMetaVisibility[groupName]?.[key] === true;
    }
  }
  return baseline;
}

function metaFilterSelectionMatches(visibility, baseline, excludedKeys = []) {
  const excluded = new Set(excludedKeys);
  return Object.keys(visibility)
    .filter(key => !excluded.has(key))
    .every(key => visibility[key] === baseline[key]);
}

function browserVideoFacetPresetBaseline(plugin, preset = activeSearchPreset) {
  const definition = searchPresetDefinition(preset);
  return definition?.kind === 'videos' || !definition
    ? defaultBrowserVideoFacetVisibility(plugin)
    : { present: false, absent: false };
}

function browserClipFacetPresetBaseline(plugin, preset = activeSearchPreset) {
  const definition = searchPresetDefinition(preset);
  return definition?.kind === 'clips' || !definition
    ? defaultBrowserClipFacetVisibility(plugin)
    : { present: false, absent: false };
}

function searchOptInKeys(groupName) {
  const keys = searchOptInMetaFilters
    .filter(filter => filter.groupName === groupName)
    .map(filter => filter.key);
  if (groupName === 'videos') keys.push('removed');
  return keys;
}

function searchOptInFilter(groupName, key) {
  return searchOptInMetaFilters.find(
    filter => filter.groupName === groupName && filter.key === key
  ) || null;
}

function setLocalFilterPreference(preferenceKey, enabled) {
  if (enabled) {
    filterPreferences[preferenceKey] = true;
  } else {
    delete filterPreferences[preferenceKey];
  }
  const plugin = browserSearchPlugins().find(
    item => item.search.preferenceKey === preferenceKey
  );
  if (plugin) pluginSearchVisibility.set(plugin.id, enabled);
  for (const videoFacetPlugin of browserVideoFilterPlugins()) {
    const definition = browserVideoFacetDefinition(videoFacetPlugin);
    const state = browserVideoFacetState(videoFacetPlugin);
    if (definition.presentDisabledPreferenceKey === preferenceKey) {
      state.present = !enabled;
    }
    if (definition.absentDisabledPreferenceKey === preferenceKey) {
      state.absent = !enabled;
    }
  }
  for (const clipFacetPlugin of browserClipFilterPlugins()) {
    const definition = browserClipFacetDefinition(clipFacetPlugin);
    const state = browserClipFacetState(clipFacetPlugin);
    if (definition.presentDisabledPreferenceKey === preferenceKey) {
      state.present = !enabled;
    }
    if (definition.absentDisabledPreferenceKey === preferenceKey) {
      state.absent = !enabled;
    }
  }
  const playlistFilter = playlistVideoOptInFilters.find(
    item => item.preferenceKey === preferenceKey
  );
  if (playlistFilter) playlistVisibility[playlistFilter.key] = enabled;
  const filter = searchOptInMetaFilters.find(
    item => item.preferenceKey === preferenceKey
  );
  if (!filter) return;
  const effectiveEnabled = (
    preferenceKey === filterPreferenceKeys.lowPartialCompletion
    && partialCompletionMinimumPercent <= 1
  ) || enabled;
  defaultSearchMetaVisibility[filter.groupName][filter.key] = effectiveEnabled;
  searchMetaVisibility[filter.groupName][filter.key] = effectiveEnabled;
  if (preferenceKey === filterPreferenceKeys.lowPartialCompletion) {
    playlistCompletionVisibility.partial_below_minimum = effectiveEnabled;
  }
}

function saveFilterPreference(preferenceKey, enabled) {
  const previousEnabled = filterPreferenceEnabled(preferenceKey);
  if (previousEnabled === enabled) return;
  setLocalFilterPreference(preferenceKey, enabled);
  const version = (filterPreferenceSaveVersions.get(preferenceKey) || 0) + 1;
  filterPreferenceSaveVersions.set(preferenceKey, version);
  void persistFilterPreference(preferenceKey, enabled).catch(error => {
    if (filterPreferenceSaveVersions.get(preferenceKey) !== version) return;
    setLocalFilterPreference(preferenceKey, previousEnabled);
    if (selected === '__search__') {
      syncSearchUrlAndRender(true);
    } else {
      void render();
    }
    window.alert(error instanceof Error ? error.message : String(error));
  });
}

function saveSearchOptInPreferences(groupNames) {
  const groups = new Set(groupNames);
  if (selected.startsWith('__playlist__:')) {
    Object.assign(playlistVisibility, searchMetaVisibility.videos);
    if (groups.has('videos')) savePlaylistVideoOptInPreferences();
    if (groups.has('completion')) {
      saveFilterPreference(
        filterPreferenceKeys.lowPartialCompletion,
        Boolean(searchMetaVisibility.completion.partial_below_minimum),
      );
    }
    return;
  }
  for (const filter of searchOptInMetaFilters) {
    if (!groups.has(filter.groupName)) continue;
    saveFilterPreference(
      filter.preferenceKey,
      Boolean(searchMetaVisibility[filter.groupName][filter.key]),
    );
  }
}

function savePlaylistVideoOptInPreferences() {
  for (const filter of playlistVideoOptInFilters) {
    saveFilterPreference(filter.preferenceKey, Boolean(playlistVisibility[filter.key]));
  }
}

function resetSearchMetaVisibility() {
  for (const [groupName, defaults] of Object.entries(defaultSearchMetaVisibility)) {
    Object.assign(searchMetaVisibility[groupName], defaults);
  }
  for (const plugin of browserSearchPlugins()) {
    if (browserVideoFacetDefinition(plugin)) {
      Object.assign(
        browserVideoFacetState(plugin),
        defaultBrowserVideoFacetVisibility(plugin),
      );
    }
    if (browserClipFacetDefinition(plugin)) {
      Object.assign(
        browserClipFacetState(plugin),
        defaultBrowserClipFacetVisibility(plugin),
      );
    }
    if (!browserVideoFacetDefinition(plugin) && !browserClipFacetDefinition(plugin)) {
      pluginSearchVisibility.set(
        plugin.id,
        filterPreferenceEnabled(String(plugin.search.preferenceKey || '')),
      );
    }
  }
}

function clearSearchMetaVisibility() {
  for (const visibility of Object.values(searchMetaVisibility)) {
    for (const key of Object.keys(visibility)) visibility[key] = false;
  }
  for (const plugin of browserSearchPlugins()) {
    if (browserVideoFacetDefinition(plugin)) {
      Object.assign(browserVideoFacetState(plugin), { present: false, absent: false });
    }
    if (browserClipFacetDefinition(plugin)) {
      Object.assign(browserClipFacetState(plugin), { present: false, absent: false });
    }
    if (!browserVideoFacetDefinition(plugin) && !browserClipFacetDefinition(plugin)) {
      pluginSearchVisibility.set(plugin.id, false);
    }
  }
}

function enableDefaultSearchKind(kind) {
  const plugin = browserSearchPlugin(kind);
  if (plugin) {
    pluginSearchVisibility.set(plugin.id, true);
    return;
  }
  for (const facetKey of searchKindFacetKeys(kind)) {
    Object.assign(searchMetaVisibility[facetKey], defaultSearchMetaVisibility[facetKey]);
  }
  const filters = kind === 'videos'
    ? browserVideoFilterPlugins()
    : (kind === 'clips' ? browserClipFilterPlugins() : []);
  for (const filterPlugin of filters) {
    const state = kind === 'videos'
      ? browserVideoFacetState(filterPlugin)
      : browserClipFacetState(filterPlugin);
    if (state.present || state.absent) continue;
    const defaults = kind === 'videos'
      ? defaultBrowserVideoFacetVisibility(filterPlugin)
      : defaultBrowserClipFacetVisibility(filterPlugin);
    Object.assign(
      state,
      defaults.present || defaults.absent
        ? defaults
        : { present: true, absent: true },
    );
  }
}

function applySearchPresetState(preset, groupKey = '') {
  uploaderCategorySelectionExplicit = false;
  const definition = searchPresetDefinition(preset);
  const groupPreset = preset === 'playlist-group' || preset === 'channel-group';
  if (!definition || (groupPreset && !groupKey)) {
    activeSearchPreset = '';
    searchPlaylistGroupKey = '';
    searchChannelGroupKey = '';
    resetSearchMetaVisibility();
    return;
  }
  activeSearchPreset = preset;
  searchPlaylistGroupKey = preset === 'playlist-group' ? groupKey : '';
  searchChannelGroupKey = preset === 'channel-group' ? groupKey : '';
  clearSearchMetaVisibility();
  enableDefaultSearchKind(definition.kind);
}

function defaultSearchResultsSort(
  query = search.value.trim(),
  preset = activeSearchPreset,
) {
  if (query) return 'relevance';
  return searchPresetDefinition(preset)?.sort || 'newest';
}

function searchSortPreferenceContext(preset = activeSearchPreset) {
  return preset || 'search';
}

function preferredSearchResultsSort(
  query = search.value.trim(),
  preset = activeSearchPreset,
) {
  const preference = sortPreferences[searchSortPreferenceContext(preset)];
  return searchSortOptions.has(preference)
    ? preference
    : defaultSearchResultsSort(query, preset);
}

function searchKindEnabled(kind) {
  const plugin = browserSearchPlugin(kind);
  if (plugin) {
    if (browserVideoFacetDefinition(plugin)) {
      const state = browserVideoFacetState(plugin);
      return state.present || state.absent;
    }
    if (browserClipFacetDefinition(plugin)) {
      const state = browserClipFacetState(plugin);
      return state.present || state.absent;
    }
    return pluginSearchVisibility.get(plugin.id) === true;
  }
  const nativeFacetsEnabled = searchKindFacetKeys(kind).every(
    key => Object.values(searchMetaVisibility[key]).some(Boolean)
  );
  if (!nativeFacetsEnabled) return false;
  const filterPlugins = kind === 'videos'
    ? browserVideoFilterPlugins()
    : (kind === 'clips' ? browserClipFilterPlugins() : []);
  return filterPlugins.every(pluginItem => {
    const state = kind === 'videos'
      ? browserVideoFacetState(pluginItem)
      : browserClipFacetState(pluginItem);
    return state.present || state.absent;
  });
}

function selectedSearchKinds() {
  const selectedKinds = [
    'videos',
    'clips',
    'playlists',
    'channels',
    ...browserResultSearchPlugins().map(plugin => plugin.id),
  ].filter(searchKindEnabled);
  const contextKind = searchContextKind();
  return contextKind
    ? selectedKinds.filter(kind => kind === contextKind)
    : selectedKinds;
}

function selectedEntityCategory() {
  if (selected.startsWith('__video__:')) return 'videos';
  if (selected.startsWith('__clip__:')) return 'clips';
  if (selected.startsWith('__playlist__:')) return 'playlists';
  if (selected.startsWith('__channel__:')) return 'channels';
  return '';
}

function searchContextKind() {
  if (selected.startsWith('__playlist__:')) return 'videos';
  if (selected.startsWith('__channel__:')) return 'videos';
  return selected === '__search__' ? activeSearchScope : selectedEntityCategory();
}

function scopedVideoDetailSearchActive() {
  return selected.startsWith('__playlist__:')
    || (selected.startsWith('__channel__:') && channelDetailSearchActive);
}

function selectedSearchResultKinds() {
  const resultKindByFilterKind = {
    videos: 'video',
    clips: 'clip',
    playlists: 'playlist',
    channels: 'channel',
  };
  return selectedSearchKinds().map(kind => resultKindByFilterKind[kind]).filter(Boolean);
}

function presetDefiningFiltersMatch() {
  if (activeSearchPreset === 'playlist-group') return Boolean(searchPlaylistGroupKey);
  if (activeSearchPreset === 'channel-group') return Boolean(searchChannelGroupKey);
  return true;
}

function reconcileSearchPreset() {
  const definition = searchPresetDefinition(activeSearchPreset);
  if (!definition) return;
  const kinds = selectedSearchKinds();
  if (kinds.length === 1 && kinds[0] === definition.kind && presetDefiningFiltersMatch()) return;
  activeSearchPreset = '';
  searchPlaylistGroupKey = '';
  searchChannelGroupKey = '';
}

function applyMetaFilterParam(visibility, value, excludedKeys = []) {
  if (value === null) return;
  const excluded = new Set(excludedKeys);
  const selectedValues = new Set(value === '__none__' ? [] : value.split(',').filter(Boolean));
  for (const key of Object.keys(visibility)) {
    if (excluded.has(key)) continue;
    visibility[key] = selectedValues.has(key);
  }
}

function searchUrl() {
  const params = new URLSearchParams();
  const query = search.value.trim();
  if (query) params.set('q', query);
  const definition = searchPresetDefinition(activeSearchPreset);
  const scope = activeSearchScope || (
    ['videos', 'clips', 'playlists', 'channels'].includes(definition?.kind)
      ? definition.kind
      : ''
  );
  const base = scope ? `/${scope}` : '/search';
  if (activeSearchPreset && activeSearchPreset !== scope) params.set('view', activeSearchPreset);
  const groupKey = searchPlaylistGroupKey || searchChannelGroupKey;
  if (groupKey) params.set('group', groupKey);
  if (activeSearchFields().size !== applicableSearchFields().length) {
    params.set('in', searchFieldParamValue() || '__none__');
  }
  for (const [groupName, paramName] of Object.entries(searchMetaParamNames)) {
    const visibility = searchMetaVisibility[groupName];
    const optInKeys = searchOptInKeys(groupName);
    const baseline = searchMetaPresetBaseline(groupName);
    if (!metaFilterSelectionMatches(visibility, baseline, optInKeys)) {
      params.set(paramName, metaFilterParamValue(visibility, optInKeys));
    }
  }
  for (const { groupName, key, paramName } of searchOptInMetaFilters) {
    const baseline = searchMetaPresetBaseline(groupName);
    if (searchMetaVisibility[groupName][key] && !baseline[key]) {
      params.set(paramName, '1');
    }
  }
  for (const plugin of browserSearchPlugins()) {
    const videoFacet = browserVideoFacetDefinition(plugin);
    const clipFacet = browserClipFacetDefinition(plugin);
    if (videoFacet) {
      const state = browserVideoFacetState(plugin);
      const baseline = browserVideoFacetPresetBaseline(plugin);
      if (state.present !== baseline.present) {
        params.set(
          videoFacet.presentHashParam || `plugin-${plugin.id}-present`,
          state.present ? '1' : '0',
        );
      }
      if (state.absent !== baseline.absent) {
        params.set(
          videoFacet.absentHashParam || `plugin-${plugin.id}-absent`,
          state.absent ? '1' : '0',
        );
      }
    }
    if (clipFacet) {
      const state = browserClipFacetState(plugin);
      const baseline = browserClipFacetPresetBaseline(plugin);
      if (state.present !== baseline.present) {
        params.set(
          clipFacet.presentHashParam || `plugin-${plugin.id}-clip-present`,
          state.present ? '1' : '0',
        );
      }
      if (state.absent !== baseline.absent) {
        params.set(
          clipFacet.absentHashParam || `plugin-${plugin.id}-clip-absent`,
          state.absent ? '1' : '0',
        );
      }
    }
    if (!videoFacet && !clipFacet && searchKindEnabled(plugin.id)) {
      params.set(plugin.search.hashParam || `plugin-${plugin.id}`, '1');
    }
  }
  if (searchSortExplicit || searchResultsSort !== preferredSearchResultsSort(query)) {
    params.set('sort', searchResultsSort);
  }
  if (currentPage > 1) params.set('page', String(currentPage));
  return appendUrlParams(base, params);
}

function playlistDetailUrl(playlistId, includePagination = false) {
  const params = new URL(searchUrl(), window.location.origin).searchParams;
  for (const key of ['view', 'group', 'sort', 'page', 'removed', 'duplicates']) params.delete(key);
  if (searchMetaVisibility.videos.removed) params.set('removed', '1');
  if (playlistDuplicatesOnly) params.set('duplicates', '1');
  if (includePagination) {
    for (const [key, value] of paginationParams()) params.set(key, value);
  }
  return appendUrlParams(`/playlists/${encodeURIComponent(playlistId)}`, params);
}

function applyPlaylistLocation(playlistId, params) {
  resetPlaylistVisibilityFor(playlistId);
  applySearchLocation('/videos', params);
  for (const key of Object.keys(playlistVisibility)) {
    if (key === 'removed') continue;
    playlistVisibility[key] = Boolean(searchMetaVisibility.videos[key]);
  }
  searchMetaVisibility.videos.removed = params.get('removed') === '1'
    || playlistVisibility.removed;
  playlistVisibility.removed = searchMetaVisibility.videos.removed;
  Object.assign(playlistCompletionVisibility, searchMetaVisibility.completion);
  playlistDuplicatesOnly = params.get('duplicates') === '1';
}

function applySearchLocation(pathname, params) {
  historyNavigationDate = '';
  pendingHistoryDate = '';
  const scopeByPath = {
    '/videos': 'videos',
    '/clips': 'clips',
    '/playlists': 'playlists',
    '/channels': 'channels',
  };
  activeSearchScope = scopeByPath[pathname] || '';
  const requestedPreset = params.get('view') || activeSearchScope;
  const requestedDefinition = searchPresetDefinition(requestedPreset);
  const invalidPreset = Boolean(
    requestedPreset
    && (
      !requestedDefinition
      || (activeSearchScope && requestedDefinition.kind !== activeSearchScope)
    )
  );
  const appliedPreset = invalidPreset ? activeSearchScope : requestedPreset;
  search.value = params.get('q') || '';
  const searchInParam = params.get('in');
  if (searchInParam !== null) {
    const active = new Set(
      searchInParam === '__none__' ? [] : searchInParam.split(',').filter(Boolean)
    );
    for (const input of searchFields) {
      input.checked = active.has(input.dataset.searchField);
    }
  } else {
    for (const input of searchFields) input.checked = true;
  }
  applySearchPresetState(appliedPreset, params.get('group') || '');
  const metaParamValues = {
    videoType: params.get(searchMetaParamNames.videoType),
    broadcastStatus: params.get(searchMetaParamNames.broadcastStatus),
    aiDisclosure: params.get(searchMetaParamNames.aiDisclosure),
    videos: params.get(searchMetaParamNames.videos),
    reactions: params.get(searchMetaParamNames.reactions),
    completion: params.get(searchMetaParamNames.completion),
    membership: params.get(searchMetaParamNames.membership),
    uploaderCategory: params.get(searchMetaParamNames.uploaderCategory),
    clipOwnership: params.get(searchMetaParamNames.clipOwnership),
    channelSubscription: params.get(searchMetaParamNames.channelSubscription),
    channelStatus: params.get(searchMetaParamNames.channelStatus),
    playlistAvailability: params.get(searchMetaParamNames.playlistAvailability),
    playlistOwnership: params.get(searchMetaParamNames.playlistOwnership),
    videoNotes: params.get(searchMetaParamNames.videoNotes),
    clipNotes: params.get(searchMetaParamNames.clipNotes),
    playlistNotes: params.get(searchMetaParamNames.playlistNotes),
    channelNotes: params.get(searchMetaParamNames.channelNotes),
  };
  const uploaderCategoryParam = metaParamValues.uploaderCategory;
  uploaderCategorySelectionExplicit = uploaderCategoryParam !== null;
  if (uploaderCategoryParam && uploaderCategoryParam !== '__none__') {
    for (const category of uploaderCategoryParam.split(',').filter(Boolean)) {
      defaultSearchMetaVisibility.uploaderCategory[category] = true;
      searchMetaVisibility.uploaderCategory[category] = false;
    }
  }
  for (const [groupName, value] of Object.entries(metaParamValues)) {
    applyMetaFilterParam(
      searchMetaVisibility[groupName],
      value,
      searchOptInKeys(groupName),
    );
  }
  for (const { groupName, key, paramName } of searchOptInMetaFilters) {
    const legacyValue = metaParamValues[groupName];
    const legacySelected = (
      legacyValue !== null
      && legacyValue !== '__none__'
      && legacyValue.split(',').includes(key)
    );
    if (params.get(paramName) === '1' || legacySelected) {
      searchMetaVisibility[groupName][key] = true;
    }
  }
  for (const plugin of browserSearchPlugins()) {
    const videoFacet = browserVideoFacetDefinition(plugin);
    const clipFacet = browserClipFacetDefinition(plugin);
    if (videoFacet) {
      const state = browserVideoFacetState(plugin);
      const presentParam = params.get(
        videoFacet.presentHashParam || `plugin-${plugin.id}-present`
      );
      const absentParam = params.get(
        videoFacet.absentHashParam || `plugin-${plugin.id}-absent`
      );
      if (presentParam !== null) state.present = presentParam === '1';
      if (absentParam !== null) state.absent = absentParam === '1';
    }
    if (clipFacet) {
      const state = browserClipFacetState(plugin);
      const presentParam = params.get(
        clipFacet.presentHashParam || `plugin-${plugin.id}-clip-present`
      );
      const absentParam = params.get(
        clipFacet.absentHashParam || `plugin-${plugin.id}-clip-absent`
      );
      if (presentParam !== null) state.present = presentParam === '1';
      if (absentParam !== null) state.absent = absentParam === '1';
    }
    if (!videoFacet && !clipFacet) {
      const hashParam = plugin.search.hashParam || `plugin-${plugin.id}`;
      if (params.get(hashParam) === '1') pluginSearchVisibility.set(plugin.id, true);
    }
  }
  const requestedSort = params.get('sort') || '';
  searchSortExplicit = searchSortOptions.has(requestedSort);
  searchResultsSort = searchSortExplicit
    ? requestedSort
    : preferredSearchResultsSort(search.value.trim());
  const page = Number(params.get('page') || 1);
  currentPage = Number.isFinite(page) && page > 0 ? page : 1;
  if (pathname === '/search') rememberSearchUrl(currentBrowserUrl());
  if (pathname === '/' || invalidPreset) updateSearchUrl(true);
}

function updateSearchUrl(replace = false) {
  const href = searchUrl();
  rememberSearchUrl(href);
  return setBrowserUrl(href, replace);
}

function hrefForCurrentSelection(includePagination = false) {
  if (selected.startsWith('__playlist__:')) {
    return playlistDetailUrl(selected.slice('__playlist__:'.length), includePagination);
  }
  if (selected.startsWith('__video__:')) {
    return localVideoHref(selected.slice('__video__:'.length), includePagination);
  }
  if (selected.startsWith('__clip__:')) {
    return localClipHref(selected.slice('__clip__:'.length));
  }
  if (selected.startsWith('__channel__:')) {
    const base = localChannelHref(selected.slice('__channel__:'.length));
    return includePagination ? appendUrlParams(base, channelDetailParams()) : base;
  }
  return localViewHref(selected, includePagination);
}

function updateCurrentUrl(replace = false) {
  if (selected === '__search__') return updateSearchUrl(replace);
  const href = hrefForCurrentSelection(true);
  return setBrowserUrl(href, replace);
}

function syncSearchUrlAndRender(replaceUrl = true) {
  if (selected === '__search__') {
    const urlChanged = updateSearchUrl(replaceUrl);
    syncSidebarSelection();
    if (urlChanged && !replaceUrl) return;
  }
  render();
}

function selectionFromLocation() {
  const pathname = window.location.pathname.replace(/\/+$/, '') || '/';
  const params = new URLSearchParams(window.location.search);
  if (['/', '/search', '/videos', '/clips', '/playlists', '/channels'].includes(pathname)) {
    applySearchLocation(pathname, params);
    return '__search__';
  }
  const parts = pathname.split('/').filter(Boolean);
  const historyLocation = pathname === '/history' || parts[0] === 'channels';
  applyPaginationParams(params, historyLocation);
  if (pathname === '/history') {
    search.value = params.get('q') || '';
    applyHistorySearchFieldLocation(params);
  }
  if (parts.length === 2 && parts[0] === 'playlists') {
    const playlistId = decodeURIComponent(parts[1]);
    if (playlistId) {
      applyPlaylistLocation(playlistId, params);
      return playlistSelection(playlistId);
    }
  }
  if (parts.length === 2 && parts[0] === 'videos') {
    const videoId = decodeURIComponent(parts[1]);
    if (videoId) return videoSelection(videoId);
  }
  if (parts.length === 2 && parts[0] === 'clips') {
    const clipId = decodeURIComponent(parts[1]);
    if (clipId) return clipSelection(clipId);
  }
  if (parts.length === 2 && parts[0] === 'channels') {
    const channelId = decodeURIComponent(parts[1]);
    if (channelId) {
      channelDetailSearchActive = params.has('q');
      if (channelDetailSearchActive) applySearchLocation('/videos', params);
      channelDetailTab = channelDetailTabFromParams(params);
      return channelSelection(channelId);
    }
  }
  if (pathname === '/history') return '__history__';
  setBrowserUrl('/search', true);
  applySearchLocation('/search', new URLSearchParams());
  return '__search__';
}

function activateSearchPreset(preset, groupKey = '') {
  const definition = searchPresetDefinition(preset);
  if (!definition) return;
  selected = '__search__';
  activeSearchScope = ['videos', 'clips', 'playlists', 'channels'].includes(definition.kind)
    ? definition.kind
    : '';
  if (!definition.preserveQuery) search.value = '';
  for (const input of searchFields) input.checked = true;
  applySearchPresetState(preset, groupKey);
  searchResultsSort = preferredSearchResultsSort('', preset);
  searchSortExplicit = false;
  currentPage = 1;
  const href = searchUrl();
  if (setBrowserUrl(href)) return;
  renderGroups();
  void render();
}

function activateSearchFromSelection({ resetMetaVisibility = false } = {}) {
  if (selected === '__search__' || scopedVideoDetailSearchActive()) return false;
  const scope = searchContextKind();
  selected = '__search__';
  activeSearchPreset = '';
  activeSearchScope = scope;
  searchPlaylistGroupKey = '';
  searchChannelGroupKey = '';
  searchSortExplicit = false;
  currentPage = 1;
  searchFilters.hidden = false;
  if (resetMetaVisibility) {
    resetSearchMetaVisibility();
    renderSearchMetaFilters();
  }
  searchResultsSort = preferredSearchResultsSort(search.value.trim(), '');
  renderGroups();
  return true;
}

function activateUnscopedSearch() {
  selected = '__search__';
  activeSearchPreset = '';
  activeSearchScope = '';
  searchPlaylistGroupKey = '';
  searchChannelGroupKey = '';
  resetSearchMetaVisibility();
  searchResultsSort = preferredSearchResultsSort(search.value.trim(), '');
  searchSortExplicit = false;
  currentPage = 1;
  const href = searchUrl();
  if (setBrowserUrl(href)) return;
  renderGroups();
  void render();
}

function activateSearchNavigation() {
  if (selected === '__search__') {
    activateUnscopedSearch();
    return;
  }
  setBrowserUrl(retainedSearchUrl || '/search');
}

function setSelected(value) {
  selected = value;
  if (value === '__history__') {
    search.value = '';
    for (const input of searchFields) {
      if (['titles', 'descriptions'].includes(input.dataset.searchField)) input.checked = true;
    }
  }
  currentPage = 1;
  if (value.startsWith('__playlist__:')) {
    const playlistId = value.slice('__playlist__:'.length);
    resetPlaylistVisibilityFor(playlistId);
    if (setBrowserUrl(localPlaylistHref(playlistId))) return;
  } else if (value.startsWith('__video__:')) {
    const videoId = value.slice('__video__:'.length);
    if (setBrowserUrl(localVideoHref(videoId))) return;
  } else if (value.startsWith('__clip__:')) {
    const clipId = value.slice('__clip__:'.length);
    if (setBrowserUrl(localClipHref(clipId))) return;
  } else if (value.startsWith('__channel__:')) {
    const channelId = value.slice('__channel__:'.length);
    if (setBrowserUrl(localChannelHref(channelId))) return;
  } else {
    const href = localViewHref(value);
    if (setBrowserUrl(href)) return;
  }
  renderGroups();
  void render();
}

function resetPlaylistVisibilityFor(playlistId) {
  if (playlistVisibilityPlaylistId === playlistId) return;
  playlistVisibilityPlaylistId = playlistId;
  playlistVisibility = defaultPlaylistVideoVisibility();
  playlistCompletionVisibility = {
    complete: true,
    partial: true,
    partial_below_minimum: defaultPartialBelowMinimumEnabled(),
    unknown: true,
    never_watched: true,
  };
  playlistDuplicatesOnly = false;
}

function groupCount(groupKey, membershipMap, childMap) {
  const identifiers = new Set();
  const pending = [groupKey];
  const visited = new Set();
  while (pending.length) {
    const current = pending.pop();
    if (!current || visited.has(current)) continue;
    visited.add(current);
    for (const identifier of membershipMap.get(current) || []) identifiers.add(identifier);
    for (const child of childMap.get(current) || []) pending.push(child.group_key);
  }
  return identifiers.size;
}

function activeSearchFields() {
  return new Set(
    applicableSearchFields()
      .filter(input => input.checked)
      .map(input => input.dataset.searchField)
  );
}

function applicableSearchFields() {
  return searchFields.filter(input => !input.disabled);
}

function syncFilterGroup(parent, childFilters, dimChildrenWhenUnchecked = true) {
  if (!parent || !childFilters.length) return;
  const checkedCount = childFilters.filter(input => input.checked).length;
  parent.checked = checkedCount > 0;
  parent.indeterminate = checkedCount > 0 && checkedCount < childFilters.length;
  setFilterDimmed(childFilters, dimChildrenWhenUnchecked && !parent.checked);
}

const renderedMetaFilterTreeGroups = new Map();

function registerMetaFilterTreeGroup({
  groupName,
  visibility,
  definitions,
  childFacets = {},
}, ancestors = new Set(), parentBranch = null) {
  if (!groupName || !visibility || !Array.isArray(definitions)) return;
  const childGroups = new Map();
  const parents = renderedMetaFilterTreeGroups.get(groupName)?.parents || [];
  if (
    parentBranch
    && !parents.some(parent => (
      parent.groupName === parentBranch.groupName
      && parent.filterName === parentBranch.filterName
    ))
  ) {
    parents.push(parentBranch);
  }
  renderedMetaFilterTreeGroups.set(groupName, { visibility, childGroups, parents });
  if (ancestors.has(groupName)) return;
  const nextAncestors = new Set(ancestors).add(groupName);
  for (const definition of definitions) {
    const childFacet = childFacets[definition.childFacetKey];
    if (!childFacet?.groupName) continue;
    childGroups.set(definition.key, childFacet.groupName);
    registerMetaFilterTreeGroup(childFacet, nextAncestors, {
      groupName,
      filterName: definition.key,
    });
    if (!visibility[definition.key]) {
      setMetaFilterGroupState(childFacet.groupName, false);
    }
  }
}

function metaFilterGroupVisibility(groupName) {
  const registeredGroup = renderedMetaFilterTreeGroups.get(groupName);
  if (registeredGroup?.visibility) return registeredGroup.visibility;
  if (groupName.startsWith('search-clip-plugin-')) {
    const pluginId = groupName.slice('search-clip-plugin-'.length);
    const plugin = browserClipFilterPlugins().find(item => item.id === pluginId);
    return plugin ? browserClipFacetState(plugin) : null;
  }
  if (groupName.startsWith('search-plugin-')) {
    const pluginId = groupName.slice('search-plugin-'.length);
    const plugin = browserVideoFilterPlugins().find(item => item.id === pluginId);
    return plugin ? browserVideoFacetState(plugin) : null;
  }
  const groups = {
    'search-videoType': searchMetaVisibility.videoType,
    'search-broadcastStatus': searchMetaVisibility.broadcastStatus,
    'search-videos': searchMetaVisibility.videos,
    'search-reactions': searchMetaVisibility.reactions,
    'search-completion': searchMetaVisibility.completion,
    'search-membership': searchMetaVisibility.membership,
    'search-uploaderCategory': searchMetaVisibility.uploaderCategory,
    'search-clipOwnership': searchMetaVisibility.clipOwnership,
    'search-channelSubscription': searchMetaVisibility.channelSubscription,
    'search-channelStatus': searchMetaVisibility.channelStatus,
    'search-playlistAvailability': searchMetaVisibility.playlistAvailability,
    'search-playlistOwnership': searchMetaVisibility.playlistOwnership,
    'search-videoNotes': searchMetaVisibility.videoNotes,
    'search-clipNotes': searchMetaVisibility.clipNotes,
    'search-playlistNotes': searchMetaVisibility.playlistNotes,
    'search-channelNotes': searchMetaVisibility.channelNotes,
  };
  return groups[groupName] || null;
}

function metaFilterGroupExcludedKeys(groupName) {
  return new Set();
}

function setMetaFilterGroupState(groupName, checked, visited = new Set()) {
  if (visited.has(groupName)) return true;
  const group = metaFilterGroupVisibility(groupName);
  if (!group) return false;
  visited.add(groupName);
  const excludedKeys = metaFilterGroupExcludedKeys(groupName);
  for (const key of Object.keys(group)) {
    if (!excludedKeys.has(key)) group[key] = checked;
  }
  const childGroups = renderedMetaFilterTreeGroups.get(groupName)?.childGroups;
  for (const childGroupName of new Set(childGroups?.values() || [])) {
    setMetaFilterGroupState(childGroupName, checked, visited);
  }
  return true;
}

function setRenderedMetaFilterGroup(groupName, checked) {
  const root = groupName.startsWith('search-') ? searchFilterRegion : meta;
  for (const input of root.querySelectorAll(`[data-meta-child-filter="${groupName}"]`)) {
    input.checked = checked;
  }
}

function setMetaFilterGroup(groupName, checked, visited = new Set()) {
  if (!setMetaFilterGroupState(groupName, checked, visited)) return false;
  for (const visitedGroupName of visited) {
    setRenderedMetaFilterGroup(visitedGroupName, checked);
  }
  return true;
}

function setRenderedMetaFilterBranch(groupName, filterName, checked) {
  const root = groupName.startsWith('search-') ? searchFilterRegion : meta;
  for (const input of root.querySelectorAll('[data-meta-tree-group]')) {
    if (
      input.dataset.metaTreeGroup === groupName
      && input.dataset.metaTreeKey === filterName
    ) input.checked = checked;
  }
}

function enableMetaFilterAncestors(groupName, visited = new Set()) {
  if (visited.has(groupName)) return;
  visited.add(groupName);
  const parents = renderedMetaFilterTreeGroups.get(groupName)?.parents || [];
  for (const parent of parents) {
    const parentGroup = metaFilterGroupVisibility(parent.groupName);
    if (!parentGroup || !Object.prototype.hasOwnProperty.call(parentGroup, parent.filterName)) {
      continue;
    }
    parentGroup[parent.filterName] = true;
    setRenderedMetaFilterBranch(parent.groupName, parent.filterName, true);
    enableMetaFilterAncestors(parent.groupName, visited);
  }
}

function setMetaFilterBranch(groupName, filterName, checked) {
  const group = metaFilterGroupVisibility(groupName);
  if (!group || !Object.prototype.hasOwnProperty.call(group, filterName)) return false;
  group[filterName] = checked;
  setRenderedMetaFilterBranch(groupName, filterName, checked);
  const childGroupName = renderedMetaFilterTreeGroups
    .get(groupName)
    ?.childGroups.get(filterName);
  if (childGroupName) setMetaFilterGroup(childGroupName, checked);
  if (checked) enableMetaFilterAncestors(groupName);
  return true;
}

function allMetaFilterChildrenChecked(groupName) {
  const group = metaFilterGroupVisibility(groupName);
  if (!group) return false;
  const root = groupName.startsWith('search-') ? searchFilterRegion : meta;
  const visibleChildren = [
    ...root.querySelectorAll(`[data-meta-child-filter="${groupName}"]`),
  ];
  if (visibleChildren.length) return visibleChildren.every(input => input.checked);
  const excludedKeys = metaFilterGroupExcludedKeys(groupName);
  const children = Object.entries(group).filter(([key]) => !excludedKeys.has(key));
  return children.length > 0 && children.every(([, checked]) => checked);
}

function syncMetaFilterGroup(groupName, assumeAllChecked = false) {
  const root = groupName.startsWith('search-') ? searchFilterRegion : meta;
  const parent = root.querySelector(`[data-meta-all-filter="${groupName}"]`);
  if (assumeAllChecked && parent instanceof HTMLInputElement) {
    parent.checked = true;
    parent.indeterminate = false;
    return;
  }
  syncFilterGroup(
    parent,
    [...root.querySelectorAll(`[data-meta-child-filter="${groupName}"]`)],
    false,
  );
}

const searchVideoFacetKeys = [
  'videoType',
  'broadcastStatus',
  'aiDisclosure',
  'videos',
  'reactions',
  'completion',
  'membership',
  'uploaderCategory',
  'videoNotes',
];
const searchPlaylistFacetKeys = ['playlistAvailability', 'playlistOwnership', 'playlistNotes'];
const searchChannelFacetKeys = ['channelSubscription', 'channelStatus', 'channelNotes'];
const searchClipFacetKeys = ['clipOwnership', 'clipNotes'];

function searchKindFacetKeys(kind) {
  if (kind === 'videos') return searchVideoFacetKeys;
  if (kind === 'clips') return searchClipFacetKeys;
  if (kind === 'playlists') return searchPlaylistFacetKeys;
  if (kind === 'channels') return searchChannelFacetKeys;
  return [];
}

function setSearchKindFilter(kind, checked) {
  const plugin = browserSearchPlugin(kind);
  if (plugin) {
    if (browserVideoFacetDefinition(plugin)) {
      Object.assign(browserVideoFacetState(plugin), { present: checked, absent: checked });
    }
    if (browserClipFacetDefinition(plugin)) {
      Object.assign(browserClipFacetState(plugin), { present: checked, absent: checked });
    }
    if (!browserVideoFacetDefinition(plugin) && !browserClipFacetDefinition(plugin)) {
      pluginSearchVisibility.set(plugin.id, checked);
    }
    return true;
  }
  const facetKeys = searchKindFacetKeys(kind);
  if (!facetKeys.every(key => searchMetaVisibility[key])) return false;
  for (const facetKey of facetKeys) {
    for (const key of Object.keys(searchMetaVisibility[facetKey])) {
      searchMetaVisibility[facetKey][key] = checked;
    }
    for (const input of searchFilterRegion.querySelectorAll(`[data-search-meta-filter^="${facetKey}:"]`)) {
      input.checked = checked;
    }
    syncMetaFilterGroup(`search-${facetKey}`);
  }
  if (kind === 'videos') {
    for (const videoFilter of browserVideoFilterPlugins()) {
      Object.assign(
        browserVideoFacetState(videoFilter),
        { present: checked, absent: checked },
      );
      for (const input of searchFilterRegion.querySelectorAll(
        `[data-meta-child-filter="search-plugin-${videoFilter.id}"]`
      )) {
        input.checked = checked;
      }
      syncMetaFilterGroup(`search-plugin-${videoFilter.id}`);
    }
  }
  if (kind === 'clips') {
    for (const clipFilter of browserClipFilterPlugins()) {
      Object.assign(
        browserClipFacetState(clipFilter),
        { present: checked, absent: checked },
      );
      for (const input of searchFilterRegion.querySelectorAll(
        `[data-meta-child-filter="search-clip-plugin-${clipFilter.id}"]`
      )) {
        input.checked = checked;
      }
      syncMetaFilterGroup(`search-clip-plugin-${clipFilter.id}`);
    }
  }
  return true;
}

function renderedSearchKindSelectionState(kind) {
  const facetKeys = searchKindFacetKeys(kind);
  if (!facetKeys.length) return null;
  const facetSelections = facetKeys.map(key => {
    const inputs = [
      ...searchFilterRegion.querySelectorAll(`[data-meta-child-filter="search-${key}"]`),
    ];
    if (
      !inputs.length
      || inputs.every(input => input.closest('.meta-filter-nested-content'))
    ) return [];
    return inputs.map(input => input.checked);
  }).filter(values => values.length);
  if (kind === 'videos') {
    facetSelections.push(
      ...browserVideoFilterPlugins().map(plugin => (
        [...searchFilterRegion.querySelectorAll(
          `[data-meta-child-filter="search-plugin-${plugin.id}"]`
        )].map(input => input.checked)
      )).filter(values => values.length),
    );
  }
  if (kind === 'clips') {
    facetSelections.push(
      ...browserClipFilterPlugins().map(plugin => (
        [...searchFilterRegion.querySelectorAll(
          `[data-meta-child-filter="search-clip-plugin-${plugin.id}"]`
        )].map(input => input.checked)
      )).filter(values => values.length),
    );
  }
  if (!facetSelections.length) return null;
  return {
    enabled: facetSelections.every(values => values.some(Boolean)),
    allSelected: facetSelections.every(values => values.every(Boolean)),
  };
}

function syncSearchKindFilter(kind, applyDisabledStyles = true, assumeAllChecked = false) {
  const parent = searchFilterRegion.querySelector(`[data-search-kind-filter="${kind}"]`);
  if (!(parent instanceof HTMLInputElement)) return;
  if (assumeAllChecked) {
    parent.checked = true;
    parent.indeterminate = false;
    if (applyDisabledStyles) {
      for (const row of searchFilterRegion.querySelectorAll(`[data-search-kind-facet="${kind}"]`)) {
        row.classList.remove('dimmed');
      }
      parent.closest('.search-meta-kind')?.classList.remove('kind-disabled');
    }
    return;
  }
  if (browserSearchPlugin(kind)) {
    parent.checked = searchKindEnabled(kind);
    parent.indeterminate = false;
    if (applyDisabledStyles) {
      parent.closest('.search-meta-kind')?.classList.toggle('kind-disabled', !parent.checked);
    }
    return;
  }
  const selectionState = renderedSearchKindSelectionState(kind);
  if (!selectionState) return;
  parent.checked = selectionState.enabled;
  parent.indeterminate = selectionState.enabled && !selectionState.allSelected;
  if (applyDisabledStyles) {
    for (const row of searchFilterRegion.querySelectorAll(`[data-search-kind-facet="${kind}"]`)) {
      row.classList.toggle('dimmed', !selectionState.enabled);
    }
    parent.closest('.search-meta-kind')?.classList.toggle('kind-disabled', !selectionState.enabled);
  }
}

function searchKindForFacet(facetKey) {
  if (searchVideoFacetKeys.includes(facetKey)) return 'videos';
  if (searchClipFacetKeys.includes(facetKey)) return 'clips';
  if (searchPlaylistFacetKeys.includes(facetKey)) return 'playlists';
  if (searchChannelFacetKeys.includes(facetKey)) return 'channels';
  return facetKey;
}

function refreshSearchAfterFilterChange(groupName, activatedFromSelection) {
  currentPage = 1;
  syncSearchKindFilter(searchKindForFacet(groupName), false);
  syncBrowserPluginSearchFieldVisibility();
  if (scopedVideoDetailSearchActive()) {
    if (selected.startsWith('__playlist__:')) {
      Object.assign(playlistVisibility, searchMetaVisibility.videos);
      Object.assign(playlistCompletionVisibility, searchMetaVisibility.completion);
    }
    showSearchMetaProgress(groupName);
    updateCurrentUrl(true);
    void render();
    return;
  }
  reconcileSearchPreset();
  showSearchMetaProgress(groupName);
  syncSearchUrlAndRender(!activatedFromSelection);
}

function restoreEmptySearchKindFacets(facetKey) {
  const kind = searchKindForFacet(facetKey);
  const facetKeys = searchKindFacetKeys(kind);
  if (!facetKeys.length) return;
  for (const siblingKey of facetKeys) {
    if (siblingKey === facetKey) continue;
    if (Object.values(searchMetaVisibility[siblingKey]).some(Boolean)) continue;
    const defaults = defaultSearchMetaVisibility[siblingKey];
    Object.assign(searchMetaVisibility[siblingKey], defaults);
    for (const input of searchFilterRegion.querySelectorAll(`[data-search-meta-filter^="${siblingKey}:"]`)) {
      const filterName = input.dataset.searchMetaFilter.split(':')[1];
      input.checked = Boolean(defaults[filterName]);
    }
    syncMetaFilterGroup(`search-${siblingKey}`);
  }
  const filterPlugins = kind === 'videos'
    ? browserVideoFilterPlugins()
    : (kind === 'clips' ? browserClipFilterPlugins() : []);
  for (const plugin of filterPlugins) {
    const clipFacet = kind === 'clips';
    const state = clipFacet ? browserClipFacetState(plugin) : browserVideoFacetState(plugin);
    if (Object.values(state).some(Boolean)) continue;
    Object.assign(state, { present: true, absent: true });
    for (const input of searchFilterRegion.querySelectorAll(
      `[data-search-plugin-facet-filter^="${clipFacet ? 'clip' : 'video'}:${plugin.id}:"]`
    )) {
      const filterName = input.dataset.searchPluginFacetFilter.split(':')[2];
      input.checked = Boolean(state[filterName]);
    }
    syncMetaFilterGroup(`${clipFacet ? 'search-clip-plugin' : 'search-plugin'}-${plugin.id}`);
  }
}

function setFilterDimmed(inputs, dimmed) {
  for (const input of inputs) {
    const label = input?.closest?.('.filter, .meta-filter');
    if (label) label.classList.toggle('dimmed', dimmed);
  }
}

function isSubscribedChannel(channel) {
  return Number(channel.subscribed || 0) === 1;
}

const channelNotificationOptions = {
  all: {
    label: 'All',
    title: 'All notifications',
    svg: '<svg xmlns="http://www.w3.org/2000/svg" height="24" viewBox="0 0 24 24" width="24" focusable="false" aria-hidden="true"><path d="M19.395 1.196a1 1 0 00-.199 1.4A9 9 0 0121 8a1 1 0 002 0 11 11 0 00-2.205-6.605 1 1 0 00-1.4-.199Zm-16.192.2A11 11 0 001 8a1 1 0 002 0 9 9 0 011.803-5.404 1 1 0 00-1.6-1.2ZM12 1a7 7 0 00-7 7v4.446a1 1 0 01-.144.515L3.05 15.972C2.25 17.305 3.21 19 4.766 19H8a4 4 0 108 0h3.233c1.555 0 2.515-1.695 1.715-3.029l-1.805-3.01a1 1 0 01-.143-.515V8a7 7 0 00-7-7Zm0 2a5 5 0 015 5v4.445a3 3 0 00.428 1.545L19.233 17H4.766l1.806-3.01c.28-.466.428-1 .428-1.544V8a5 5 0 015-5Zm-2 16h4a2 2 0 01-4 0Z"></path></svg>',
  },
  personalized: {
    label: 'Personalized',
    title: 'Personalized notifications',
    svg: '<svg xmlns="http://www.w3.org/2000/svg" height="24" viewBox="0 0 24 24" width="24" focusable="false" aria-hidden="true"><path d="M16 19a4 4 0 11-8 0H4.765C3.21 19 2.25 17.304 3.05 15.97l1.806-3.01A1 1 0 005 12.446V8a7 7 0 0114 0v4.446c0 .181.05.36.142.515l1.807 3.01c.8 1.333-.161 3.029-1.716 3.029H16ZM12 3a5 5 0 00-5 5v4.446a3 3 0 01-.428 1.543L4.765 17h14.468l-1.805-3.01A3 3 0 0117 12.445V8a5 5 0 00-5-5Zm-2 16a2 2 0 104 0h-4Z"></path></svg>',
  },
  none: {
    label: 'None',
    title: 'No notifications',
    svg: '<svg xmlns="http://www.w3.org/2000/svg" height="24" viewBox="0 0 24 24" width="24" focusable="false" aria-hidden="true"><path d="M12 1a7 7 0 00-6.213 3.774l1.719 1.032A5 5 0 0117 8v3.502l2 1.199V8a7 7 0 00-7-7ZM1.141 5.485a1 1 0 00.343 1.372l3.514 2.109v3.48a1 1 0 01-.143.514L3.05 15.97c-.8 1.334.16 3.03 1.716 3.03H8a4 4 0 108 0l6-.001a1 1 0 00.515-1.856l-20-12a1 1 0 00-1.373.342ZM7 12.446v-2.28L18.39 17H4.766l1.806-3.011A3 3 0 007 12.446ZM10 19h4a2 2 0 01-4 0Z"></path></svg>',
  },
};

function channelNotificationHtml(channel) {
  if (!isSubscribedChannel(channel)) return '';
  const notificationLevel = String(channel.notification_level || '').trim().toLowerCase();
  const option = channelNotificationOptions[notificationLevel];
  if (!option) return '';
  return `<span class="channel-notification" title="${option.title}">${option.svg}<span>${option.label}</span></span>`;
}

function playlistVisibilityCategory(playlist) {
  const value = String(playlist.visibility || '').trim().toLowerCase();
  if (['private', 'public', 'unlisted'].includes(value)) return value;
  return 'unknown';
}

function displayPlaylistVisibility(playlist) {
  const value = playlistVisibilityCategory(playlist);
  return value === 'unknown' ? '' : value[0].toUpperCase() + value.slice(1);
}

function playlistVisibilityLabelHtml(playlist) {
  const value = playlistVisibilityCategory(playlist);
  return visibilityLabelHtml(value, displayPlaylistVisibility(playlist));
}

function visibilityFilterLabelHtml(value, count) {
  const countText = filterCountText(count);
  if (!['private', 'public', 'unlisted'].includes(value)) {
    return `<span>${escapeHtml(value)} <span class="meta-filter-count">${countText}</span></span>`;
  }
  return `<span class="visibility-label">${visibilityIconSvg(value)}<span>${escapeHtml(value)} <span class="meta-filter-count">${countText}</span></span></span>`;
}

function visibilityLabelHtml(value, label) {
  if (!label || !['private', 'public', 'unlisted'].includes(value)) return label ? escapeHtml(label) : '';
  return `<span class="visibility-label">${visibilityIconSvg(value)}<span>${escapeHtml(label)}</span></span>`;
}

function visibilityIconSvg(value) {
  if (value === 'public') {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"></circle><path d="M2 12h20"></path><path d="M12 2a15.3 15.3 0 0 1 0 20"></path><path d="M12 2a15.3 15.3 0 0 0 0 20"></path></svg>';
  }
  if (value === 'unlisted') {
    return '<svg xmlns="http://www.w3.org/2000/svg" height="24" viewBox="0 0 24 24" width="24" fill="currentColor" focusable="false" aria-hidden="true"><path d="M9 18c.226 0 .448-.012.667-.037A8.001 8.001 0 018.07 16H7a4 4 0 110-8h2a4 4 0 014 4 2 2 0 001.668 1.973A5.999 5.999 0 009 6H7a6 6 0 100 12h2Zm8 0a6 6 0 100-12h-2c-.225 0-.448.012-.667.036A8 8 0 0115.93 8H17a4 4 0 110 8h-2a4 4 0 01-4-4 2 2 0 00-1.668-1.973A6 6 0 0015 18h2Z"></path></svg>';
  }
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"></rect><path d="M8 11V7a4 4 0 0 1 8 0v4"></path></svg>';
}

function usefulMetadataTitle(video) {
  const title = String(video.metadata_title || '').trim();
  if (!title || title === '- YouTube' || title === 'YouTube') return '';
  return title;
}

function displayVideoTitle(video) {
  return usefulMetadataTitle(video) || video.title || '';
}

function displayVideoChannel(video) {
  return video.metadata_channel || video.channel || '';
}

function displayVideoChannelUrl(video) {
  return video.metadata_channel_url || '';
}

function displayVideoChannelLocalUrl(video) {
  const channelId = video.metadata_channel_id || video.channel_id || video.recovered_channel_id || '';
  const channelReference = video.metadata_channel_reference || channelId;
  return channelReference ? localChannelHref(channelReference) : displayVideoChannelUrl(video);
}

function displayVideoDuration(video) {
  return video.metadata_duration || video.duration_text || '';
}

function displayVideoUploadDate(video) {
  const value = video.metadata_upload_date || '';
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const timeZone = window.YTLibraryTime.timeZone || window.YTLibraryTime.detected();
  const date = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeZone }).format(parsed);
  const time = new Intl.DateTimeFormat(undefined, { timeStyle: 'short', timeZone }).format(parsed);
  return `${date} ${time}`;
}

function cardTimestampLabel(value) {
  const normalized = String(value || '').trim();
  if (!normalized) return '';
  if (/^\d{4}-\d{2}-\d{2}$/.test(normalized)) {
    return window.YTLibraryTime.formatDate(normalized);
  }
  const parsed = new Date(normalized);
  if (Number.isNaN(parsed.getTime())) return normalized;
  const timeZone = window.YTLibraryTime.timeZone || window.YTLibraryTime.detected();
  const date = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeZone }).format(parsed);
  const time = new Intl.DateTimeFormat(undefined, { timeStyle: 'short', timeZone }).format(parsed);
  return `${date} ${time}`;
}

function latestWatchedAtLabel(video) {
  const value = String(video.latest_watch_at || '').trim();
  if (!value) return '';
  return cardTimestampLabel(value);
}

function latestWatchDateHtml(video) {
  const watchedAt = latestWatchedAtLabel(video);
  return watchedAt
    ? `<div class="details watch-date-line"><span>Last watched ${escapeHtml(watchedAt)}${compactWatchCountHtml(video)}</span></div>`
    : '';
}

function playlistLastUpdatedHtml(playlist) {
  const updatedAt = cardTimestampLabel(playlist?.last_changed_at);
  return updatedAt
    ? `<div class="details last-updated-line"><span>Last updated ${escapeHtml(updatedAt)}</span></div>`
    : '';
}

function playlistYoutubeLastUpdatedLabel(playlist) {
  return cardTimestampLabel(playlist?.youtube_updated_date);
}

function playlistYoutubeLastUpdatedHtml(playlist) {
  const updatedAt = playlistYoutubeLastUpdatedLabel(playlist);
  return updatedAt
    ? `<div class="details yt-last-updated-line"><span>YT Last updated ${escapeHtml(updatedAt)}</span></div>`
    : '';
}

function youtubeWatchUrl(video, includePlaylist = true) {
  if (!video.video_id) return '';
  const list = includePlaylist && video.playlist_id
    ? `&list=${encodeURIComponent(video.playlist_id)}`
    : '';
  return `https://www.youtube.com/watch?v=${encodeURIComponent(video.video_id)}${list}`;
}

function matchTypeLabel(video) {
  if (video.source_type) return '';
  if (video.match_type === 'ambiguous_hidden_candidate') return '';
  const label = video.match_label || '';
  if (/^matched by\b/i.test(label)) return '';
  return label;
}

function wasRemovedFromPlaylist(video) {
  return video.source_quality === 'takeout' && video.match_type === 'ambiguous_hidden_candidate';
}

function isUnavailablePlaylistVideo(video) {
  return videoAvailabilityValue(video) === 'unavailable';
}

function wasRemovedByMeFromPlaylist(video) {
  if (video.collection_category) return video.collection_category === 'removed';
  return wasRemovedFromPlaylist(video) && !isUnavailablePlaylistVideo(video);
}

function pageSizeNumber() {
  if (pageSize === 'all') return Infinity;
  const value = Number(pageSize || 100);
  return Number.isFinite(value) && value > 0 ? value : 100;
}

function pageSlice(rows) {
  const size = pageSizeNumber();
  if (!Number.isFinite(size)) {
    currentPage = 1;
    return { pageRows: rows, page: 1, pageCount: 1, start: rows.length ? 1 : 0, end: rows.length, total: rows.length };
  }
  const pageCount = Math.max(1, Math.ceil(rows.length / size));
  currentPage = Math.min(Math.max(1, currentPage), pageCount);
  const startIndex = (currentPage - 1) * size;
  const endIndex = Math.min(rows.length, startIndex + size);
  return {
    pageRows: rows.slice(startIndex, endIndex),
    page: currentPage,
    pageCount,
    start: rows.length ? startIndex + 1 : 0,
    end: endIndex,
    total: rows.length,
  };
}

function pageJumpItems(pageInfo) {
  if (pageInfo.pageCount <= 1) return [];
  if (pageInfo.pageCount <= 7) {
    const items = Array.from({ length: pageInfo.pageCount }, (_, index) => index + 1);
    while (items.length < 7) items.push('placeholder');
    return items;
  }
  const page = pageInfo.page;
  const last = pageInfo.pageCount;
  if (page <= 3) {
    const leading = Array.from({ length: 4 - page }, () => 'placeholder');
    const pages = Array.from({ length: Math.min(page + 1, last) }, (_, index) => index + 1);
    const items = [...leading, ...pages, 'ellipsis', last];
    while (items.length < 7) items.push('placeholder');
    return items.slice(0, 7);
  }
  if (page >= last - 2) {
    const firstPage = Math.max(1, page - 1);
    const pages = Array.from({ length: last - firstPage + 1 }, (_, index) => firstPage + index);
    const trailing = Array.from({ length: Math.max(0, 7 - 2 - pages.length) }, () => 'placeholder');
    const items = [1, 'ellipsis', ...pages, ...trailing];
    while (items.length < 7) items.push('placeholder');
    return items.slice(0, 7);
  }
  return [1, 'ellipsis', page - 1, page, page + 1, 'ellipsis', last];
}

function pageJumpHtml(pageInfo) {
  const items = pageJumpItems(pageInfo);
  if (!items.length) return '';
  return `
    <span class="page-jumps">
      ${items.map(item => {
        if (item === 'ellipsis') return '<span class="ellipsis">...</span>';
        if (item === 'placeholder') return '<span class="page-placeholder">&nbsp;</span>';
        return `<button type="button" class="${item === pageInfo.page ? 'current' : ''}" data-page="${item}" ${item === pageInfo.page ? 'disabled' : ''}>${item}</button>`;
      }).join('')}
    </span>
  `;
}

function pageGotoHtml(pageInfo) {
  if (pageInfo.pageCount <= 1) return '';
  return `
    <form class="page-goto" data-page-goto>
      <label>Go to
        <input type="number" data-page-goto-input min="1" max="${pageInfo.pageCount}" value="${pageInfo.page}" inputmode="numeric">
      </label>
      <button type="submit">Go</button>
    </form>
  `;
}

function pagerHtml(pageInfo, includePageSize = false) {
  if (!pageInfo.total) return '';
  const pageSizes = ['50', '100', '250', '500'];
  const sizeHtml = includePageSize ? `
    <label>Page size
      <select data-page-size>
        ${pageSizes.map(value => `<option value="${value}" ${pageSize === value ? 'selected' : ''}>${value === 'all' ? 'All' : value}</option>`).join('')}
      </select>
    </label>
  ` : '';
  return `
    ${sizeHtml}
    <span>Showing ${pageInfo.start}-${pageInfo.end} of ${pageInfo.total}</span>
    ${pageJumpHtml(pageInfo)}
    ${pageGotoHtml(pageInfo)}
  `;
}

function usesDocumentPageScrolling() {
  return window.matchMedia('(max-width: 760px)').matches;
}

function pageScrollBoundaryState() {
  if (usesDocumentPageScrolling()) {
    const scrolling = document.scrollingElement || document.documentElement;
    const top = Number(scrolling.scrollTop || window.scrollY || 0);
    const maximum = Math.max(0, scrolling.scrollHeight - window.innerHeight);
    return { atTop: top <= 1, atBottom: top >= maximum - 1 };
  }
  if (!(resultsScroll instanceof HTMLElement)) return { atTop: false, atBottom: false };
  const maximum = Math.max(0, resultsScroll.scrollHeight - resultsScroll.clientHeight);
  return {
    atTop: resultsScroll.scrollTop <= 1,
    atBottom: resultsScroll.scrollTop >= maximum - 1,
  };
}

function pageBoundaryTargetAllowed(target) {
  if (!(target instanceof Element)) return false;
  if (target.closest('input, select, textarea')) return false;
  return Boolean(resultsScroll?.contains(target) || bottomPager.contains(target));
}

function armPageBoundaryInputAfterIdle(delay = 260) {
  if (pageBoundaryInputTimer !== null) window.clearTimeout(pageBoundaryInputTimer);
  pageBoundaryInputTimer = window.setTimeout(() => {
    pageBoundaryInputTimer = null;
    pageBoundaryInputArmed = true;
  }, delay);
}

function finishPageBoundaryLanding() {
  const landing = pendingPageBoundaryLanding;
  if (!landing) return;
  pendingPageBoundaryLanding = '';
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      if (usesDocumentPageScrolling()) {
        if (landing === 'top') {
          const top = window.scrollY + grid.getBoundingClientRect().top;
          window.scrollTo({ top: Math.max(0, top), behavior: 'auto' });
        } else {
          const scrolling = document.scrollingElement || document.documentElement;
          window.scrollTo({
            top: Math.max(0, scrolling.scrollHeight - window.innerHeight),
            behavior: 'auto',
          });
        }
      } else if (resultsScroll instanceof HTMLElement) {
        resultsScroll.scrollTop = landing === 'top'
          ? 0
          : Math.max(0, resultsScroll.scrollHeight - resultsScroll.clientHeight);
      }
      pageBoundaryNavigationPending = false;
      pageBoundaryInputArmed = false;
      armPageBoundaryInputAfterIdle(350);
      if (pageBoundaryFallbackTimer !== null) {
        window.clearTimeout(pageBoundaryFallbackTimer);
        pageBoundaryFallbackTimer = null;
      }
    });
  });
}

function navigateAcrossPageBoundary(direction) {
  if (pageBoundaryNavigationPending || !pageBoundaryInputArmed) return false;
  const nextPage = Number(renderedPageInfo.page || currentPage) + direction;
  const pageCount = Number(renderedPageInfo.pageCount || 1);
  if (nextPage < 1 || nextPage > pageCount) return false;
  pageBoundaryNavigationPending = true;
  pageBoundaryInputArmed = false;
  pendingPageBoundaryLanding = direction > 0 ? 'top' : 'bottom';
  currentPage = nextPage;
  historyNavigationDate = '';
  pendingHistoryDate = '';
  if (pageBoundaryFallbackTimer !== null) window.clearTimeout(pageBoundaryFallbackTimer);
  pageBoundaryFallbackTimer = window.setTimeout(() => {
    pageBoundaryFallbackTimer = null;
    pageBoundaryNavigationPending = false;
    pendingPageBoundaryLanding = '';
    pageBoundaryInputArmed = false;
    armPageBoundaryInputAfterIdle();
  }, 10000);
  if (!updateCurrentUrl(false)) void render();
  return true;
}

function renderPager(pageInfo) {
  renderedPageInfo = {
    page: Number(pageInfo.page || 1),
    pageCount: Number(pageInfo.pageCount || 1),
    total: Number(pageInfo.total || 0),
  };
  bottomPager.hidden = !pageInfo.total;
  bottomPager.innerHTML = pagerHtml(pageInfo, true);
  if (currentLocationHasPaginationParams()) updateCurrentUrl(true);
  finishPageBoundaryLanding();
}

function hidePager() {
  renderedPageInfo = { page: 1, pageCount: 1, total: 0 };
  bottomPager.hidden = true;
  bottomPager.replaceChildren();
}

function pagedRows(rows) {
  const pageInfo = pageSlice(rows);
  renderPager(pageInfo);
  return pageInfo;
}

function stopSearchProgress() {
  grid.removeAttribute('aria-busy');
  meta.removeAttribute('aria-busy');
}

function animateProgressDots(update) {
  let dotCount = 1;
  update('.');
  return setInterval(() => {
    dotCount = dotCount === 3 ? 1 : dotCount + 1;
    update('.'.repeat(dotCount));
  }, 280);
}

function loadingMessageAnimation(container, labelText) {
  const label = document.createElement('span');
  label.textContent = labelText;
  const dots = document.createElement('span');
  dots.className = 'loading-dots';
  dots.setAttribute('aria-hidden', 'true');
  container.replaceChildren(label, dots);
  return animateProgressDots(value => {
    dots.textContent = value;
  });
}

function renderLoadingStatus() {
  if (!activeLoadingStatusTokens.size) {
    if (loadingStatusTimer !== null) {
      clearInterval(loadingStatusTimer);
      loadingStatusTimer = null;
    }
    loadingStatus.hidden = true;
    loadingStatus.removeAttribute('aria-busy');
    loadingStatus.replaceChildren();
    return;
  }
  if (loadingStatusTimer !== null) return;
  loadingStatus.hidden = false;
  loadingStatus.setAttribute('aria-busy', 'true');
  loadingStatusTimer = loadingMessageAnimation(loadingStatus, 'Loading');
}

function resetLoadingStatus() {
  loadingStatusEpoch += 1;
  activeLoadingStatusTokens = new Set();
  renderLoadingStatus();
}

function beginLoadingStatus({ reset = false } = {}) {
  if (reset) resetLoadingStatus();
  const token = `${loadingStatusEpoch}:${++loadingStatusToken}`;
  activeLoadingStatusTokens.add(token);
  renderLoadingStatus();
  return token;
}

function finishLoadingStatus(token) {
  if (!activeLoadingStatusTokens.delete(token)) return;
  renderLoadingStatus();
}

async function withLoadingStatus(load) {
  const token = beginLoadingStatus();
  try {
    return await load();
  } finally {
    finishLoadingStatus(token);
  }
}

function updateSearchMetaProgress(dotsText = searchMetaProgressDots) {
  searchMetaProgressDots = dotsText;
  for (const dots of searchFilterRegion.querySelectorAll('[data-search-meta-progress]')) {
    const active = pendingSearchMetaGroups.has(dots.dataset.searchMetaProgress);
    dots.classList.toggle('active', active);
    dots.textContent = active ? dotsText : '';
    const row = dots.closest('.search-meta-kind');
    if (!row) continue;
    if (active) {
      row.setAttribute('aria-busy', 'true');
    } else {
      row.removeAttribute('aria-busy');
    }
  }
}

function stopSearchMetaProgress() {
  if (searchMetaProgressTimer !== null) {
    clearInterval(searchMetaProgressTimer);
    searchMetaProgressTimer = null;
  }
  pendingSearchMetaGroups.clear();
  updateSearchMetaProgress('');
}

function stopSearchFilterProgress() {
  stopSearchMetaProgress();
}

function showSearchMetaProgress(groupName) {
  const progressGroup = searchKindForFacet(groupName);
  pendingSearchMetaGroups.add(progressGroup);
  if (searchMetaProgressTimer === null) {
    searchMetaProgressTimer = animateProgressDots(updateSearchMetaProgress);
  } else {
    updateSearchMetaProgress();
  }
}

function showSearchProgress({ preserveContent = false } = {}) {
  stopSearchProgress();
  grid.setAttribute('aria-busy', 'true');
  meta.setAttribute('aria-busy', 'true');
  if (preserveContent) return;
  grid.className = 'grid';
  grid.replaceChildren();
  empty.hidden = true;
}

function remotePageInfo(total, rowsLength, remoteLimit = 0) {
  const size = pageSizeNumber();
  const effectiveSize = remoteLimit || (Number.isFinite(size) ? size : 1000);
  const pageCount = Math.max(1, Math.ceil(total / effectiveSize));
  currentPage = Math.min(Math.max(1, currentPage), pageCount);
  const start = total ? (currentPage - 1) * effectiveSize + 1 : 0;
  const end = total ? Math.min(total, start + rowsLength - 1) : 0;
  return { pageRows: [], page: currentPage, pageCount, start, end, total };
}

function remotePayloadPageInfo(payload, rowsLength) {
  const limit = Number(payload.limit || pageSizeNumber() || 100);
  currentPage = Math.floor(Number(payload.offset || 0) / limit) + 1;
  return remotePageInfo(Number(payload.total || 0), rowsLength, limit);
}

function cancelAdjacentPagePrefetch() {
  adjacentPagePrefetchGeneration += 1;
  if (adjacentPagePrefetchCancel) adjacentPagePrefetchCancel();
  adjacentPagePrefetchCancel = null;
}

function scheduleAdjacentPagePrefetch(pageInfo, fetchPage, additionalRequests = []) {
  cancelAdjacentPagePrefetch();
  const page = Number(pageInfo.page || 1);
  const pageCount = Number(pageInfo.pageCount || 1);
  const pages = [page + 1, page - 1].filter(candidate => (
    candidate >= 1 && candidate <= pageCount
  ));
  const pageRequests = pages.map(candidate => () => fetchPage(candidate));
  if (!pageRequests.length && !additionalRequests.length) return;
  const generation = adjacentPagePrefetchGeneration;
  const runRequests = async requests => {
    for (const request of requests) {
      if (generation !== adjacentPagePrefetchGeneration) return;
      try {
        await request();
      } catch (_error) {
        // A speculative request must not affect normal page loading.
      }
    }
  };
  const run = async () => {
    adjacentPagePrefetchCancel = null;
    await withLoadingStatus(() => Promise.all([
      runRequests(additionalRequests),
      runRequests(pageRequests),
    ]));
  };
  const handle = window.setTimeout(() => void run(), 150);
  adjacentPagePrefetchCancel = () => window.clearTimeout(handle);
}

async function fetchHistoryPage(channelId = '', page = currentPage) {
  const size = pageSizeNumber();
  const limit = Number.isFinite(size) ? size : 1000;
  const requestedPage = Math.max(1, Number(page) || 1);
  const offset = (requestedPage - 1) * limit;
  const query = channelId ? '' : search.value.trim();
  const searchFieldsValue = channelId ? '' : (historySearchFieldParamValue() || '__none__');
  const key = `${channelId}:${query.toLowerCase()}:${searchFieldsValue}:${limit}:${offset}`;
  return cachedRequest(historyPageCache, key, async () => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (channelId) params.set('channel_id', channelId);
    if (query) params.set('q', query);
    if (!channelId) params.set('search_fields', searchFieldsValue);
    const response = await fetch(`/api/history/search?${params}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`History fetch failed: ${response.status}`);
    const payload = await response.json();
    if (channelId) {
      channelHistoryCounts.set(
        channelId,
        Number(payload.totals?.filtered_watch_rows ?? payload.totals?.watch_rows ?? 0),
      );
    }
    return payload;
  }, adjacentPageCacheLimit);
}

function cachedChannelHistoryCount(channelId) {
  const value = channelHistoryCounts.get(channelId);
  return typeof value === 'number' ? value : null;
}

async function fetchChannelHistoryCount(channelId) {
  const cached = channelHistoryCounts.get(channelId);
  if (typeof cached === 'number') return cached;
  if (cached) return cached;
  const request = fetch(`/api/history/search?${new URLSearchParams({ channel_id: channelId, limit: '1' })}`, { cache: 'no-store' })
    .then(response => {
      if (!response.ok) throw new Error(`Channel history fetch failed: ${response.status}`);
      return response.json();
    })
    .then(payload => {
      const total = Number(payload.totals?.filtered_watch_rows ?? payload.totals?.watch_rows ?? 0);
      channelHistoryCounts.set(channelId, total);
      return total;
    })
    .catch(error => {
      channelHistoryCounts.delete(channelId);
      throw error;
    });
  channelHistoryCounts.set(channelId, request);
  return request;
}

function channelTabCountKey(channelId, tabKey) {
  return `${channelId}:${tabKey}`;
}

function cachedChannelTabCount(channelId, tabKey) {
  const entry = channelTabCountCache.get(channelTabCountKey(channelId, tabKey));
  return entry && entry.promise === null ? Number(entry.data || 0) : null;
}

function storeChannelTabCount(channelId, tabKey, count) {
  const normalized = Math.max(0, Number(count || 0));
  const key = channelTabCountKey(channelId, tabKey);
  channelTabCountCache.delete(key);
  channelTabCountCache.set(key, { data: normalized, promise: null });
  trimRequestCache(channelTabCountCache, channelTabCountCacheLimit);
  return normalized;
}

async function fetchChannelTabCount(channel, channelReference, tabKey, pluginTabs) {
  const channelId = channel.channel_id || channelReference;
  const key = channelTabCountKey(channelId, tabKey);
  return cachedRequest(channelTabCountCache, key, async () => {
    if (tabKey === 'playlisted-videos') {
      const payload = await fetchViewData(
        `/api/channels/${encodeChannelReference(channelReference)}/videos?limit=1&offset=0&sort=title`,
      );
      return Math.max(0, Number(payload.total || 0));
    }
    if (tabKey === 'playlists') {
      const payload = await fetchViewData(
        `/api/channels/${encodeChannelReference(channelReference)}/playlists?limit=1&offset=0&sort=title`,
      );
      return Math.max(0, Number(payload.total || 0));
    }
    if (tabKey === 'history') return fetchChannelHistoryCount(channelId);
    const pluginTab = pluginTabs.find(tab => tab.key === tabKey);
    if (!pluginTab) return 0;
    const count = Number(await pluginTab.definition.count(
      channel,
      browserPluginHost(pluginTab.plugin.id),
    ));
    return Number.isFinite(count) ? Math.max(0, count) : 0;
  }, channelTabCountCacheLimit);
}

function updateVisibleChannelTabCount(channelReference, tabKey, count) {
  if (selected !== channelSelection(channelReference)) return;
  const button = viewContext.querySelector(
    `[data-channel-tab="${CSS.escape(tabKey)}"]`,
  );
  if (!(button instanceof HTMLButtonElement)) return;
  const label = button.dataset.channelTabLabel || '';
  button.textContent = `${label} (${Number(count || 0).toLocaleString()})`;
}

function hydrateChannelTabCounts({
  channel,
  channelReference,
  pluginTabs,
  generation,
}) {
  const channelId = channel.channel_id || channelReference;
  const tabKeys = [
    'playlisted-videos',
    'playlists',
    'history',
    ...pluginTabs.map(tab => tab.key),
  ];
  for (const tabKey of tabKeys) {
    const cached = cachedChannelTabCount(channelId, tabKey);
    if (cached !== null) {
      updateVisibleChannelTabCount(channelReference, tabKey, cached);
      continue;
    }
    void withLoadingStatus(() => (
      fetchChannelTabCount(channel, channelReference, tabKey, pluginTabs)
    )).then(count => {
      if (generation !== renderGeneration) return;
      updateVisibleChannelTabCount(channelReference, tabKey, count);
    }).catch(() => {
      // An unavailable inactive tab count must not block the active channel tab.
    });
  }
}

function localDateKey(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function historyActivityRange(yearOffset = historyActivityYearOffset) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const targetYear = today.getFullYear() - yearOffset;
  const lastDayOfTargetMonth = new Date(targetYear, today.getMonth() + 1, 0).getDate();
  const displayEnd = new Date(targetYear, today.getMonth(), Math.min(today.getDate(), lastDayOfTargetMonth));
  const start = new Date(displayEnd);
  const daysSinceWeekStart = (start.getDay() - historyWeekStartDay + 7) % 7;
  start.setDate(start.getDate() - daysSinceWeekStart - (52 * 7));
  const end = new Date(start);
  end.setDate(end.getDate() + (53 * 7) - 1);
  return { start, end, displayEnd, startKey: localDateKey(start), endKey: localDateKey(end) };
}

function historyRowDateKey(row) {
  for (const value of [row?.watched_at, row?.watch_date]) {
    const dateKey = window.YTLibraryTime.dateKey(value);
    if (dateKey) return dateKey;
  }
  return '';
}

function historyActivityYearOffsetForDate(dateKey) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(dateKey)) return historyActivityYearOffset;
  const currentRange = historyActivityRange(historyActivityYearOffset);
  if (dateKey >= currentRange.startKey && dateKey <= currentRange.endKey) {
    return historyActivityYearOffset;
  }
  const today = new Date();
  const maximumOffset = Math.max(0, today.getFullYear() - Number(dateKey.slice(0, 4)) + 2);
  for (let offset = 0; offset <= maximumOffset; offset += 1) {
    const range = historyActivityRange(offset);
    if (dateKey >= range.startKey && dateKey <= range.endKey) return offset;
  }
  return maximumOffset;
}

function syncHistoryActivityYearWithRows(rows, preferredDate = '') {
  const dateKey = preferredDate || (rows || []).map(historyRowDateKey).find(Boolean) || '';
  if (!dateKey) return false;
  const nextOffset = historyActivityYearOffsetForDate(dateKey);
  if (nextOffset === historyActivityYearOffset) return false;
  historyActivityYearOffset = nextOffset;
  return true;
}

function displayedHistoryAnchorDate() {
  const heatmap = viewContext.querySelector('.history-heatmap');
  const selectedDay = heatmap?.querySelector(
    '.history-heatmap-day[aria-current="date"][data-history-date]',
  );
  if (selectedDay instanceof HTMLButtonElement) {
    return selectedDay.dataset.historyDate || '';
  }
  const visibleDate = firstVisibleHistoryCardDate();
  if (visibleDate) return visibleDate;
  if (historyNavigationDate) return historyNavigationDate;
  const row = grid.querySelector('[data-watch-date]');
  return row instanceof HTMLElement ? row.dataset.watchDate || '' : '';
}

function shiftedHistoryDateKey(dateKey, yearDelta) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(dateKey)) return '';
  const year = Number(dateKey.slice(0, 4)) - yearDelta;
  const monthIndex = Number(dateKey.slice(5, 7)) - 1;
  const day = Number(dateKey.slice(8, 10));
  const lastDay = new Date(year, monthIndex + 1, 0).getDate();
  return localDateKey(new Date(year, monthIndex, Math.min(day, lastDay)));
}

function historyActivityDayNear(payload, dateKey) {
  const days = (payload.activity || []).filter(day => /^\d{4}-\d{2}-\d{2}$/.test(day.watch_date || ''));
  return days.find(day => day.watch_date === dateKey)
    || days.find(day => day.watch_date < dateKey)
    || days.at(-1)
    || null;
}

async function fetchHistoryActivity(channelId = '', yearOffset = historyActivityYearOffset) {
  const range = historyActivityRange(yearOffset);
  const query = channelId ? '' : search.value.trim();
  const searchFieldsValue = channelId ? '' : (historySearchFieldParamValue() || '__none__');
  const key = `${channelId}:${query.toLowerCase()}:${searchFieldsValue}:${range.startKey}:${range.endKey}`;
  return cachedRequest(historyActivityCache, key, async () => {
    const params = new URLSearchParams({ start: range.startKey, end: range.endKey });
    if (channelId) params.set('channel_id', channelId);
    if (query) params.set('q', query);
    if (!channelId) params.set('search_fields', searchFieldsValue);
    const response = await fetch(`/api/history/activity?${params}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`History activity fetch failed: ${response.status}`);
    return response.json();
  }, historyActivityCacheLimit);
}

function historyPageNumberForOffset(offset) {
  const size = pageSizeNumber();
  const effectiveSize = Number.isFinite(size) ? size : 1000;
  return Math.floor(Math.max(0, offset) / effectiveSize) + 1;
}

function historyYearPagePrefetches(channelId, rows) {
  const anchorDate = (rows || []).map(historyRowDateKey).find(Boolean)
    || localDateKey(historyActivityRange().displayEnd);
  const shifts = historyActivityYearOffset > 0 ? [1, -1] : [1];
  return shifts.map(delta => {
    const yearOffset = historyActivityYearOffset + delta;
    return async () => {
      const activity = await fetchHistoryActivity(channelId, yearOffset);
      if (!historyActivitySyncEnabled) return;
      const targetDate = shiftedHistoryDateKey(anchorDate, delta)
        || localDateKey(historyActivityRange(yearOffset).displayEnd);
      const targetDay = historyActivityDayNear(activity, targetDate);
      if (!targetDay) return;
      const page = historyPageNumberForOffset(Number(targetDay.offset || 0));
      await fetchHistoryPage(channelId, page);
    };
  });
}

function heatmapLevel(count, sortedCounts) {
  if (!count || !sortedCounts.length) return 0;
  const threshold = fraction => sortedCounts[Math.floor((sortedCounts.length - 1) * fraction)];
  if (count <= threshold(.25)) return 1;
  if (count <= threshold(.5)) return 2;
  if (count <= threshold(.75)) return 3;
  return 4;
}

function restoreHistoryNavigationButtons(container) {
  for (const button of container.querySelectorAll('.history-heatmap-nav button')) {
    button.disabled = button.dataset.historyYearShift === '-1' && historyActivityYearOffset === 0;
  }
}

function historyHeatmapNavigationIcon(paths) {
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths}</svg>`;
}

function historyMonthLabelColumn(naturalColumn, previousColumn = 0) {
  const column = Math.max(1, Math.min(53, Number(naturalColumn) || 1));
  if (!previousColumn || column - previousColumn >= 2) return column;
  return Math.min(53, previousColumn + 2);
}

function historyHeatmapFor(payload) {
  const range = historyActivityRange();
  const activity = new Map((payload.activity || []).map(day => [day.watch_date, day]));
  const sortedCounts = [...activity.values()]
    .map(day => Number(day.watch_count || 0))
    .filter(Boolean)
    .sort((left, right) => left - right);
  const heatmap = document.createElement('section');
  heatmap.className = 'history-heatmap';
  heatmap.dataset.historyChannelId = payload.channel_id || '';
  heatmap.setAttribute('aria-label', 'Views by day');
  const header = document.createElement('div');
  header.className = 'history-heatmap-header';
  const heading = document.createElement('div');
  heading.className = 'history-heatmap-title';
  heading.textContent = 'Views';
  const nav = document.createElement('div');
  nav.className = 'history-heatmap-nav';
  const syncLabel = document.createElement('label');
  syncLabel.className = 'history-heatmap-sync';
  const syncToggle = document.createElement('input');
  syncToggle.type = 'checkbox';
  syncToggle.dataset.historySync = '';
  syncToggle.checked = historyActivitySyncEnabled;
  syncLabel.append(syncToggle, document.createTextNode('Sync'));
  const previous = document.createElement('button');
  previous.type = 'button';
  previous.dataset.historyYearShift = '1';
  previous.title = 'Previous year';
  previous.setAttribute('aria-label', 'Previous year');
  previous.innerHTML = historyHeatmapNavigationIcon('<path d="m15 18-6-6 6-6"></path>');
  const rangeLabel = document.createElement('span');
  rangeLabel.className = 'history-heatmap-range';
  const rangeDateLabel = date => date.toLocaleDateString(undefined, { month: 'short', year: 'numeric' });
  rangeLabel.textContent = `${rangeDateLabel(range.start)} - ${rangeDateLabel(range.displayEnd)}`;
  const next = document.createElement('button');
  next.type = 'button';
  next.dataset.historyYearShift = '-1';
  next.title = 'Next year';
  next.setAttribute('aria-label', 'Next year');
  next.innerHTML = historyHeatmapNavigationIcon('<path d="m9 18 6-6-6-6"></path>');
  next.disabled = historyActivityYearOffset === 0;
  const current = document.createElement('button');
  current.type = 'button';
  current.dataset.historyCurrent = '';
  current.title = 'Today';
  current.setAttribute('aria-label', 'Today');
  current.innerHTML = historyHeatmapNavigationIcon('<path d="m8 18 6-6-6-6"></path><path d="M18 6v12"></path>');
  nav.append(syncLabel, previous, rangeLabel, next, current);
  header.append(heading, nav);
  const scroll = document.createElement('div');
  scroll.className = 'history-heatmap-scroll';
  const calendar = document.createElement('div');
  calendar.className = 'history-heatmap-calendar';
  const weekStartLabel = document.createElement('span');
  weekStartLabel.className = 'history-heatmap-week-start';
  weekStartLabel.textContent = historyWeekStart === 'monday' ? 'Mon' : 'Sun';
  const months = document.createElement('div');
  months.className = 'history-heatmap-months';
  const weeks = document.createElement('div');
  weeks.className = 'history-heatmap-weeks';
  const seenMonths = new Set();
  let previousMonthLabelColumn = 0;
  for (let weekIndex = 0; weekIndex < 53; weekIndex += 1) {
    const weekStart = new Date(range.start);
    weekStart.setDate(weekStart.getDate() + (weekIndex * 7));
    const monthDate = new Date(weekStart);
    for (let dayIndex = 0; dayIndex < 7; dayIndex += 1) {
      const candidate = new Date(weekStart);
      candidate.setDate(candidate.getDate() + dayIndex);
      if (candidate.getDate() === 1) {
        monthDate.setTime(candidate.getTime());
        break;
      }
    }
    const monthKey = `${monthDate.getFullYear()}-${monthDate.getMonth()}`;
    if (!seenMonths.has(monthKey)) {
      seenMonths.add(monthKey);
      const label = document.createElement('span');
      label.className = 'history-heatmap-month';
      const labelColumn = historyMonthLabelColumn(weekIndex + 1, previousMonthLabelColumn);
      label.style.gridColumn = String(labelColumn);
      previousMonthLabelColumn = labelColumn;
      label.textContent = monthDate.toLocaleDateString(undefined, { month: 'short' });
      months.append(label);
    }
    const week = document.createElement('div');
    week.className = 'history-heatmap-week';
    for (let dayIndex = 0; dayIndex < 7; dayIndex += 1) {
      const date = new Date(weekStart);
      date.setDate(date.getDate() + dayIndex);
      const dateKey = localDateKey(date);
      const day = activity.get(dateKey);
      const count = Number(day?.watch_count || 0);
      const label = `${date.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })}: ${count} ${count === 1 ? 'view' : 'views'}`;
      const cell = document.createElement(count && date <= range.end ? 'button' : 'span');
      cell.className = `history-heatmap-day level-${heatmapLevel(count, sortedCounts)}`;
      cell.title = label;
      cell.setAttribute('aria-label', label);
      if (cell instanceof HTMLButtonElement) {
        cell.type = 'button';
        cell.dataset.historyDate = dateKey;
        cell.dataset.historyOffset = String(Number(day.offset || 0));
      }
      week.append(cell);
    }
    weeks.append(week);
  }
  calendar.append(weekStartLabel, months, weeks);
  scroll.append(calendar);
  heatmap.append(header, scroll);
  return heatmap;
}

function historyTransitionState() {
  return {
    activityYearOffset: historyActivityYearOffset,
    navigationDate: historyNavigationDate,
    page: currentPage,
    pendingDate: pendingHistoryDate,
    syncEnabled: historyActivitySyncEnabled,
  };
}

function restoreHistoryTransitionState(snapshot) {
  historyActivityYearOffset = snapshot.activityYearOffset;
  historyNavigationDate = snapshot.navigationDate;
  currentPage = snapshot.page;
  pendingHistoryDate = snapshot.pendingDate;
  historyActivitySyncEnabled = snapshot.syncEnabled;
}

function restoreHistoryTransitionControls(heatmap) {
  const toggle = heatmap.querySelector('[data-history-sync]');
  if (toggle instanceof HTMLInputElement) toggle.checked = historyActivitySyncEnabled;
  restoreHistoryNavigationButtons(heatmap);
}

function historyHeatmapIsCurrent(heatmap) {
  const channelId = heatmap.dataset.historyChannelId || '';
  return channelId
    ? selected.startsWith('__channel__:') && channelDetailTab === 'history'
    : selected === '__history__';
}

function runHistoryHeatmapTransition(heatmap, options) {
  return withLoadingStatus(() => HistoryWorkflow.runTransition({
    ...options,
    captureState: historyTransitionState,
    heatmap,
    isCurrent: options.isCurrent || (() => historyHeatmapIsCurrent(heatmap)),
    restoreControls: restoreHistoryTransitionControls,
    restoreState: restoreHistoryTransitionState,
  }));
}

async function shiftHistoryActivityYear(delta) {
  const nextOffset = Math.max(0, historyActivityYearOffset + delta);
  if (nextOffset === historyActivityYearOffset) return;
  const heatmap = viewContext.querySelector('.history-heatmap');
  if (!(heatmap instanceof HTMLElement)) {
    historyActivityYearOffset = nextOffset;
    await render();
    return;
  }
  const channelId = heatmap.dataset.historyChannelId || '';
  const currentAnchorDate = displayedHistoryAnchorDate();
  await runHistoryHeatmapTransition(heatmap, {
    applyState: () => { historyActivityYearOffset = nextOffset; },
    load: () => fetchHistoryActivity(channelId),
    commit: async activity => {
      if (!historyActivitySyncEnabled) {
        heatmap.replaceWith(historyHeatmapFor(activity));
        return;
      }
      const targetDate = shiftedHistoryDateKey(currentAnchorDate, delta)
        || localDateKey(historyActivityRange().displayEnd);
      const targetDay = historyActivityDayNear(activity, targetDate);
      if (targetDay) {
        setHistoryPageFromOffset(targetDay.watch_date, Number(targetDay.offset || 0));
        if (updateCurrentUrl(false)) return;
        await render();
      } else {
        heatmap.replaceWith(historyHeatmapFor(activity));
      }
    },
  });
}

async function jumpToCurrentHistoryActivity() {
  const heatmap = viewContext.querySelector('.history-heatmap');
  if (!(heatmap instanceof HTMLElement)) return;
  const alreadyOnFirstPage = currentPage === 1;
  const resetNavigation = () => {
    historyActivityYearOffset = 0;
    currentPage = 1;
    pendingHistoryDate = '';
    historyNavigationDate = '';
  };
  const commitFirstPageLocation = () => {
    updateCurrentUrl(true);
    if (selected === '__history__') setDocumentTitle('History page 1');
    scrollResultsToTop();
  };
  if (alreadyOnFirstPage && historyActivityYearOffset > 0) {
    const channelId = heatmap.dataset.historyChannelId || '';
    await runHistoryHeatmapTransition(heatmap, {
      applyState: resetNavigation,
      load: () => fetchHistoryActivity(channelId, 0),
      commit: activity => {
        commitFirstPageLocation();
        heatmap.replaceWith(historyHeatmapFor(activity));
      },
    });
    return;
  }
  resetNavigation();
  if (alreadyOnFirstPage) {
    commitFirstPageLocation();
    scheduleHistoryHeatmapCurrentDay();
    return;
  }
  if (updateCurrentUrl(false)) return;
  await render();
}

async function setHistoryActivitySync(enabled) {
  if (!enabled) {
    historyActivitySyncEnabled = false;
    return;
  }
  const heatmap = viewContext.querySelector('.history-heatmap');
  if (!(heatmap instanceof HTMLElement)) {
    historyActivitySyncEnabled = true;
    return;
  }
  const channelId = heatmap.dataset.historyChannelId || '';
  await runHistoryHeatmapTransition(heatmap, {
    applyState: () => { historyActivitySyncEnabled = true; },
    load: () => fetchHistoryActivity(channelId),
    isCurrent: () => historyActivitySyncEnabled && historyHeatmapIsCurrent(heatmap),
    commit: async activity => {
      const targetDate = localDateKey(historyActivityRange().displayEnd);
      const targetDay = historyActivityDayNear(activity, targetDate);
      if (!targetDay) return;
      setHistoryPageFromOffset(targetDay.watch_date, Number(targetDay.offset || 0));
      if (updateCurrentUrl(false)) return;
      await render();
    },
  });
}

function scrollToPendingHistoryDate() {
  if (!pendingHistoryDate) return;
  const date = pendingHistoryDate;
  pendingHistoryDate = '';
  requestAnimationFrame(() => {
    const divider = grid.querySelector(`[data-history-date="${CSS.escape(date)}"]`);
    const row = grid.querySelector(`[data-watch-date="${CSS.escape(date)}"]`);
    const target = divider instanceof HTMLElement ? divider : row;
    if (!(target instanceof HTMLElement)) return;
    if (row instanceof HTMLElement) {
      row.classList.add('history-jump-target');
      window.setTimeout(() => row.classList.remove('history-jump-target'), 1800);
    }
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

function setHistoryPageFromOffset(date, offset) {
  currentPage = historyPageNumberForOffset(offset);
  pendingHistoryDate = date;
  historyNavigationDate = date;
}

async function jumpToHistoryDate(date, offset) {
  setHistoryPageFromOffset(date, offset);
  if (updateCurrentUrl(false)) return;
  await render();
}

async function fetchHistoryLocation(channelId = '') {
  const activityRequest = fetchHistoryActivity(channelId);
  if (!historyNavigationDate) {
    return Promise.all([fetchHistoryPage(channelId), activityRequest]);
  }
  const activity = await activityRequest;
  const targetDay = (activity.activity || []).find(
    day => day.watch_date === historyNavigationDate
  );
  if (targetDay) {
    setHistoryPageFromOffset(targetDay.watch_date, Number(targetDay.offset || 0));
  }
  return [await fetchHistoryPage(channelId), activity];
}

async function renderHistoryResults(options) {
  const {
    channelId = '',
    commitChrome,
    emptyMessage,
    generation,
    layoutContext,
    leadingEntries = [],
  } = options;
  const loaded = await HistoryWorkflow.loadPage({
    channelId,
    fetchActivity: fetchHistoryActivity,
    fetchLocation: fetchHistoryLocation,
    isCurrent: () => generation === renderGeneration,
    pendingDate: pendingHistoryDate,
    syncActivityYear: syncHistoryActivityYearWithRows,
    syncEnabled: historyActivitySyncEnabled,
  });
  if (!loaded) return false;
  const { activity, rows, total } = loaded;
  const pageInfo = remotePageInfo(total, rows.length);
  const historyBatch = historyRowsWithDayDividers(rows, {
    layout: cardLayoutFor(layoutContext),
  });
  const decoration = decorateEntityCardBatch(
    [...leadingEntries, ...historyBatch.entries],
    layoutContext,
    cardLayoutFor(layoutContext),
    generation,
  );
  commitChrome({ activity, pageInfo, total });
  renderPager(pageInfo);
  applyCardLayout(layoutContext);
  grid.replaceChildren(...historyBatch.elements);
  scheduleHistoryHeatmapCurrentDay();
  await decoration;
  if (generation !== renderGeneration) return false;
  empty.hidden = rows.length !== 0;
  empty.textContent = emptyMessage;
  scrollToPendingHistoryDate();
  scheduleHistoryHeatmapCurrentDay();
  scheduleAdjacentPagePrefetch(
    pageInfo,
    page => fetchHistoryPage(channelId, page),
    historyYearPagePrefetches(channelId, rows),
  );
  return true;
}

async function renderHistoryView(generation) {
  title.textContent = 'History';
  meta.textContent = '';
  applyCardLayout('history');
  empty.hidden = true;
  await renderHistoryResults({
    commitChrome: ({ activity, pageInfo, total }) => {
      const historyTitleLocation = historyNavigationDate
        ? historyDayLabel({ watch_date: historyNavigationDate })
        : `page ${pageInfo.page}`;
      setDocumentTitle(`History ${historyTitleLocation}`);
      meta.innerHTML = rightPanelListMetaHtml(`${total} watches`, {
        showLayout: true,
        layout: cardLayoutFor('history'),
        layoutContext: 'history',
      });
      viewContext.hidden = false;
      viewContext.replaceChildren(historyHeatmapFor(activity));
    },
    emptyMessage: 'No history rows match.',
    generation,
    layoutContext: 'history',
  });
}

function scrollResultsToTop() {
  if (resultsScroll) resultsScroll.scrollTop = 0;
}

function videoSortHtml(value, scope) {
  const options = [
    ['newest_added', 'Recently added'],
    ['title', 'Title A-Z'],
    ['title_desc', 'Title Z-A'],
    ['oldest_added', 'Oldest added'],
    ['most_watched', 'Most watched'],
    ['playlist_order', 'Playlist order'],
  ];
  return `
    <label class="view-sort">Sort
      <select data-video-sort="${escapeHtml(scope)}">
        ${options.map(([optionValue, label]) => `<option value="${optionValue}" ${value === optionValue ? 'selected' : ''}>${label}</option>`).join('')}
      </select>
    </label>
  `;
}

const videoMetaFilterDefinitions = [
  { key: 'public', label: 'public', visibilityIcon: true },
  { key: 'unlisted', label: 'unlisted', visibilityIcon: true },
  { key: 'private', label: 'private', visibilityIcon: true },
  {
    key: 'members_only',
    label: 'members only',
    className: 'members-only-filter',
    decoratorHtml: membersOnlyIconHtml(),
  },
  { key: 'unavailable', label: 'unavailable', className: 'badge' },
  { key: 'unknown', label: 'unknown' },
  { key: 'removed', label: 'removed', className: 'badge' },
];
const videoTypeMetaFilterDefinitions = [
  { key: 'video', label: 'Videos', decoratorHtml: videoTypeDecoratorHtml('video') },
  { key: 'short', label: 'Shorts', decoratorHtml: videoTypeDecoratorHtml('short') },
  {
    key: 'livestream',
    label: 'Livestreams',
    decoratorHtml: videoTypeDecoratorHtml('livestream'),
    childFacetKey: 'broadcastStatus',
  },
  { key: 'movie', label: 'Movies', decoratorHtml: videoTypeDecoratorHtml('movie') },
  { key: 'unknown', label: 'Unknown' },
];
const broadcastStatusMetaFilterDefinitions = [
  { key: 'live', label: 'Live now', decoratorHtml: liveNowBroadcastIconHtml() },
  { key: 'ended', label: 'Streamed live', decoratorHtml: liveBroadcastIconHtml() },
  { key: 'upcoming', label: 'Upcoming', decoratorHtml: liveBroadcastIconHtml() },
  { key: 'unknown', label: 'Unknown status' },
];
const aiDisclosureMetaFilterDefinitions = [
  { key: 'made_with_ai', label: 'Made with AI', decoratorHtml: aiDisclosureIconHtml() },
  { key: 'no_disclosure', label: 'No disclosure' },
  { key: 'unknown', label: 'Unknown' },
];
function visibleVideoMetaFilterDefinitions(counts, { includeRemoved = true } = {}) {
  return videoMetaFilterDefinitions.filter(({ key }) => (
    (includeRemoved || key !== 'removed')
    && (key !== 'private' || Number(counts?.private || 0) > 0)
  ));
}
const playlistDuplicateFilterDefinitions = [
  { key: 'duplicates', label: 'show duplicates', className: 'badge' },
];
const reactionMetaFilterDefinitions = [
  { key: 'none', label: 'no reaction' },
  { key: 'liked', label: 'liked', decoratorHtml: thumbIconHtml('like', false) },
  { key: 'disliked', label: 'disliked', decoratorHtml: thumbIconHtml('dislike', false) },
];
function completionMetaFilterDefinitions(minimumPercent, minimumAttribute) {
  const boundedMinimum = boundedPartialMinimumPercent(minimumPercent);
  const definitions = [
    { key: 'complete', label: 'complete' },
    {
      key: 'partial',
      label: 'partial',
      minimumPercent: boundedMinimum,
      minimumAttribute,
    },
    { key: 'unknown', label: 'unknown' },
    { key: 'never_watched', label: 'never watched' },
  ];
  if (boundedMinimum > 1) {
    definitions.splice(2, 0, {
      key: 'partial_below_minimum',
      label: `partial \u2264 ${boundedMinimum - 1}%`,
    });
  }
  return definitions;
}
const playlistMembershipMetaFilterDefinitions = [
  { key: 'member', label: 'member' },
  { key: 'non_member', label: 'non-member' },
];
function uploaderCategoryMetaFilterDefinitions(counts) {
  const categories = Object.keys(counts || {})
    .filter(key => key !== 'total' && key !== noUploaderCategoryFilter)
    .sort((left, right) => left.localeCompare(right));
  if (!categories.length) return [];
  return [
    { key: noUploaderCategoryFilter, label: 'No category' },
    ...categories.map(category => ({ key: category, label: category })),
  ];
}

function syncUploaderCategoryVisibility(counts) {
  const definitions = uploaderCategoryMetaFilterDefinitions(counts);
  const enableDetectedCategories = (
    !uploaderCategorySelectionExplicit
    && Object.values(searchMetaVisibility.uploaderCategory).some(Boolean)
  );
  for (const { key } of definitions) {
    if (!Object.prototype.hasOwnProperty.call(defaultSearchMetaVisibility.uploaderCategory, key)) {
      defaultSearchMetaVisibility.uploaderCategory[key] = true;
    }
    if (!Object.prototype.hasOwnProperty.call(searchMetaVisibility.uploaderCategory, key)) {
      searchMetaVisibility.uploaderCategory[key] = enableDetectedCategories;
    }
  }
  return definitions;
}
const channelSubscriptionMetaFilterDefinitions = [
  { key: 'subscribed', label: 'subscribed' },
  { key: 'non_subscribed', label: 'non-subscribed' },
];
const terminatedChannelMetaFilterDefinition = {
  key: 'terminated',
  label: 'terminated',
  className: 'badge',
};
const channelStatusMetaFilterDefinitions = [
  { key: 'active', label: 'active' },
  terminatedChannelMetaFilterDefinition,
];
const playlistAvailabilityMetaFilterDefinitions = [
  { key: 'private', label: 'private', visibilityIcon: true },
  { key: 'public', label: 'public', visibilityIcon: true },
  { key: 'unlisted', label: 'unlisted', visibilityIcon: true },
  { key: 'unavailable', label: 'unavailable', className: 'status' },
  { key: 'unknown', label: 'unknown' },
];
const playlistOwnershipMetaFilterDefinitions = [
  { key: 'mine', label: 'mine' },
  { key: 'others', label: 'others' },
  { key: 'ownership_unknown', label: 'unknown' },
];
const noteMetaFilterDefinitions = [
  { key: 'with_note', label: 'notes', decoratorHtml: noteIconHtml() },
  { key: 'without_note', label: 'no notes' },
];
function visibleMetaFilterDefinitions(visibility, counts, definitions) {
  return definitions.filter(({ key }) => (
    Object.prototype.hasOwnProperty.call(visibility, key)
    && (!hideEmptyFilters || counts === null || counts === undefined || metaFilterCount(counts, key) !== 0)
  ));
}

function metaFilterChildrenHtml({
  groupName,
  filterAttribute,
  visibility,
  counts,
  definitions,
  filterValuePrefix = '',
  childFacets = {},
  treePath = [],
  disabled = false,
  dimmed = false,
}) {
  const applicableDefinitions = visibleMetaFilterDefinitions(
    visibility,
    counts,
    definitions,
  );
  const branchPath = treePath.length
    ? treePath
    : [String(groupName || 'filters').replace(/^search-/, '')];
  return applicableDefinitions.map(({ key, label, className = '', visibilityIcon = false, decoratorHtml = '', minimumPercent = null, minimumAttribute = '', childFacetKey = '' }) => {
    const rowClass = `meta-filter meta-filter-child${dimmed || disabled ? ' dimmed' : ''}`;
    const treeAttributes = `data-meta-tree-group="${escapeHtml(groupName)}" data-meta-tree-key="${escapeHtml(key)}"`;
    const filterHtml = minimumPercent === null ? `
        <label class="${rowClass}">
          <input type="checkbox" data-meta-child-filter="${escapeHtml(groupName)}" ${treeAttributes} data-${escapeHtml(filterAttribute)}="${escapeHtml(`${filterValuePrefix}${key}`)}" ${visibility[key] ? 'checked' : ''} ${disabled ? 'disabled' : ''}>
          ${visibilityIcon
            ? visibilityFilterLabelHtml(key, metaFilterCount(counts, key))
            : `<span${className || decoratorHtml ? ` class="${[className, decoratorHtml ? 'meta-filter-decorated' : ''].filter(Boolean).join(' ')}"` : ''}>${decoratorHtml}<span>${escapeHtml(label)} <span class="meta-filter-count">${filterCountText(metaFilterCount(counts, key))}</span></span></span>`}
        </label>
      ` : `
        <div class="${rowClass}">
          <label class="completion-partial-toggle">
            <input type="checkbox" data-meta-child-filter="${escapeHtml(groupName)}" ${treeAttributes} data-${escapeHtml(filterAttribute)}="${escapeHtml(`${filterValuePrefix}${key}`)}" ${visibility[key] ? 'checked' : ''} ${disabled ? 'disabled' : ''}>
            <span>${escapeHtml(label)}</span>
          </label>
          <span class="completion-minimum-control">
            <span>&ge;</span>
            <input class="completion-minimum-input" type="number" min="1" max="99" step="1" value="${minimumPercent}" data-${escapeHtml(minimumAttribute)} aria-label="Minimum partial completion percentage" ${disabled ? 'disabled' : ''}>
            <span>% <span class="meta-filter-count">${filterCountText(metaFilterCount(counts, key))}</span></span>
          </span>
        </div>
    `;
    const childFacet = childFacets[childFacetKey];
    if (
      !childFacet
      || childFacet.counts === null
      || childFacet.counts === undefined
    ) return filterHtml;
    const childDimmed = dimmed || !visibility[key];
    const childHtml = metaFilterChildrenHtml({
      ...childFacet,
      childFacets,
      treePath: [...branchPath, key],
      disabled,
      dimmed: childDimmed,
    });
    if (!childHtml) return filterHtml;
    const nodeId = searchFilterTreeNodeId('facet', ...branchPath, key);
    const nestedExpanded = searchFilterTreeExpanded.has(nodeId);
    return `
      <div class="meta-filter-nested-option" data-search-tree-node="${escapeHtml(nodeId)}">
        ${searchFilterTreeToggleHtml(nodeId, label)}
        ${filterHtml}
        <div
          id="${escapeHtml(searchFilterTreeChildrenId(nodeId))}"
          class="meta-filter-nested-content"
          data-search-tree-children
          ${nestedExpanded ? '' : 'hidden'}
        >${childHtml}</div>
      </div>
    `;
  }).join('');
}

function parentFilterCheckboxHtml(dataAttribute, value) {
  return `
    <span class="filter-parent-checkbox">
      <input type="checkbox" ${dataAttribute}="${escapeHtml(value)}">
      <span class="filter-parent-checkbox-indicator" aria-hidden="true">
        <svg viewBox="0 0 13 13"><path d="M2.75 6.5h7.5M6.5 2.75v7.5"></path></svg>
      </span>
    </span>
  `;
}

function metaFilterControlsHtml({
  groupName,
  filterAttribute,
  visibility,
  counts,
  definitions,
  filterValuePrefix = '',
  allLabel = 'All',
  showAll = true,
}) {
  const visibleDefinitions = visibleMetaFilterDefinitions(
    visibility,
    counts,
    definitions,
  );
  if (!visibleDefinitions.length) return '';
  return `
    ${showAll ? `<label class="meta-filter meta-filter-parent">${parentFilterCheckboxHtml('data-meta-all-filter', groupName)} <span>${escapeHtml(allLabel)}</span></label>` : ''}
    ${metaFilterChildrenHtml({
      groupName,
      filterAttribute,
      visibility,
      counts,
      definitions: visibleDefinitions,
      filterValuePrefix,
    })}
  `;
}

function metaFilterCount(counts, key) {
  if (!counts || !Object.prototype.hasOwnProperty.call(counts, key)) return null;
  return Number(counts[key] || 0);
}

function filterCountText(count) {
  return count === null || count === undefined ? '' : String(Number(count || 0));
}

function playlistDuplicateFilterHtml(duplicateCount) {
  if (Number(duplicateCount || 0) <= 0) return '';
  return metaFilterControlsHtml({
    groupName: 'playlist-duplicates',
    filterAttribute: 'playlist-duplicates-filter',
    visibility: { duplicates: playlistDuplicatesOnly },
    counts: { duplicates: Number(duplicateCount || 0) },
    definitions: playlistDuplicateFilterDefinitions,
    showAll: false,
  });
}

function searchFilterTreeChildrenId(nodeId) {
  return `search-filter-tree-${nodeId.replace(/[^A-Za-z0-9_-]/g, '-')}`;
}

function searchFilterTreeNodeId(namespace, ...segments) {
  let suffix = segments
    .map(segment => String(segment || '').replace(/[^A-Za-z0-9_-]+/g, '-'))
    .filter(Boolean)
    .join('-')
    .replace(/-+/g, '-')
    .slice(0, 80);
  if (!/^[A-Za-z]/.test(suffix)) suffix = `node-${suffix}`.slice(0, 80);
  return `${namespace}:${suffix}`;
}

function searchFilterTreeToggleHtml(nodeId, label) {
  const expanded = searchFilterTreeExpanded.has(nodeId);
  return `
    <button
      class="search-tree-toggle"
      type="button"
      data-search-tree-toggle="${escapeHtml(nodeId)}"
      aria-controls="${escapeHtml(searchFilterTreeChildrenId(nodeId))}"
      aria-expanded="${expanded ? 'true' : 'false'}"
      aria-label="${expanded ? 'Collapse' : 'Expand'} ${escapeHtml(label)}"
      title="${expanded ? 'Collapse' : 'Expand'} ${escapeHtml(label)}"
    >
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"></path></svg>
    </button>
  `;
}

function searchMetaFiltersHtml(
  metaCounts,
  videoTypeCounts,
  broadcastStatusCounts,
  aiDisclosureCounts,
  reactionCounts,
  completionCounts,
  playlistMembershipCounts,
  uploaderCategoryCounts,
  resultCounts,
) {
  renderedMetaFilterTreeGroups.clear();
  const facetHtml = ({
    key,
    visibility,
    definitions,
    counts,
    allLabel = 'All',
    kind,
    groupName = `search-${key}`,
    filterAttribute = 'search-meta-filter',
    filterValuePrefix = `${key}:`,
    childFacets = {},
  }) => {
    registerMetaFilterTreeGroup({
      groupName,
      visibility,
      definitions,
      childFacets,
    });
    const countsReady = counts !== null && counts !== undefined;
    const visibleDefinitions = visibleMetaFilterDefinitions(
      visibility,
      searchKindEnabled(kind) ? counts : null,
      definitions,
    );
    if (!visibleDefinitions.length) return '';
    const nodeId = searchFilterTreeNodeId('facet', key);
    const expanded = searchFilterTreeExpanded.has(nodeId);
    return `
      <div class="search-meta-facet" data-search-kind-facet="${escapeHtml(kind)}" data-search-tree-node="${escapeHtml(nodeId)}">
        ${searchFilterTreeToggleHtml(nodeId, allLabel)}
        <label class="meta-filter meta-filter-parent">
          ${parentFilterCheckboxHtml('data-meta-all-filter', groupName)}
          <span>${escapeHtml(allLabel)}</span>
        </label>
        <div
          id="${escapeHtml(searchFilterTreeChildrenId(nodeId))}"
          class="search-meta-facet-children"
          data-search-tree-children
          ${expanded && countsReady ? '' : 'hidden'}
        >
          <span class="search-tree-toggle-spacer" aria-hidden="true"></span>
          <div class="search-meta-controls">
            ${countsReady ? metaFilterChildrenHtml({
              groupName,
              filterAttribute,
              filterValuePrefix,
              visibility,
              counts: searchKindEnabled(kind) ? counts : null,
              definitions: visibleDefinitions,
              childFacets,
              treePath: [key],
            }) : ''}
          </div>
        </div>
      </div>
    `;
  };
  const pluginVideoFacetHtml = plugin => {
    const definition = browserVideoFacetDefinition(plugin);
    return facetHtml({
      key: `plugin-${plugin.id}`,
      groupName: `search-plugin-${plugin.id}`,
      filterAttribute: 'search-plugin-facet-filter',
      filterValuePrefix: `video:${plugin.id}:`,
      visibility: browserVideoFacetState(plugin),
      counts: metaCounts?.videoPlugins?.[plugin.id] || null,
      definitions: [
        { key: 'absent', label: definition.absentLabel || `no ${plugin.search.label || plugin.id}` },
        { key: 'present', label: definition.presentLabel || plugin.search.label || plugin.id },
      ],
      allLabel: plugin.search.label || plugin.id,
      kind: 'videos',
    });
  };
  const pluginClipFacetHtml = plugin => {
    const definition = browserClipFacetDefinition(plugin);
    return facetHtml({
      key: `clip-plugin-${plugin.id}`,
      groupName: `search-clip-plugin-${plugin.id}`,
      filterAttribute: 'search-plugin-facet-filter',
      filterValuePrefix: `clip:${plugin.id}:`,
      visibility: browserClipFacetState(plugin),
      counts: metaCounts?.clipPlugins?.[plugin.id] || null,
      definitions: [
        { key: 'absent', label: definition.absentLabel || `no ${plugin.search.label || plugin.id}` },
        { key: 'present', label: definition.presentLabel || plugin.search.label || plugin.id },
      ],
      allLabel: plugin.search.label || plugin.id,
      kind: 'clips',
    });
  };
  const kindHtml = (titleText, kind, count, facetsHtml) => {
    const contextKind = searchContextKind();
    if (contextKind && kind !== contextKind) return '';
    return `
      <div
        data-search-filter-section="${escapeHtml(kind)}"
        data-search-filter-count="${escapeHtml(filterCountText(count))}"
      >${facetsHtml}</div>
    `;
  };
  const uploaderCategoryDefinitions = uploaderCategoryMetaFilterDefinitions(
    uploaderCategoryCounts,
  );
  const videoTypeChildFacets = {
    broadcastStatus: {
      groupName: 'search-broadcastStatus',
      filterAttribute: 'search-meta-filter',
      filterValuePrefix: 'broadcastStatus:',
      visibility: searchMetaVisibility.broadcastStatus,
      definitions: broadcastStatusMetaFilterDefinitions,
      counts: broadcastStatusCounts,
    },
  };
  const clipCount = metaCounts?.clips?.total;
  const showClips = !hideEmptyFilters || Number(clipCount ?? data?.counts?.clips ?? 0) > 0;
  return [
    kindHtml('Videos', 'videos', metaCounts?.videos?.total, [
      facetHtml({ key: 'videoType', visibility: searchMetaVisibility.videoType, definitions: videoTypeMetaFilterDefinitions, counts: videoTypeCounts, allLabel: 'Type', kind: 'videos', childFacets: videoTypeChildFacets }),
      facetHtml({ key: 'videos', visibility: searchMetaVisibility.videos, definitions: visibleVideoMetaFilterDefinitions(metaCounts?.videos, { includeRemoved: selected.startsWith('__playlist__:') }), counts: metaCounts?.videos, allLabel: 'Availability', kind: 'videos' }),
      facetHtml({ key: 'reactions', visibility: searchMetaVisibility.reactions, definitions: reactionMetaFilterDefinitions, counts: reactionCounts, allLabel: 'Reactions', kind: 'videos' }),
      facetHtml({ key: 'completion', visibility: searchMetaVisibility.completion, definitions: completionMetaFilterDefinitions(partialCompletionMinimumPercent, 'search-completion-minimum'), counts: completionCounts, allLabel: 'Completion', kind: 'videos' }),
      ...(selected.startsWith('__playlist__:') ? [] : [
        facetHtml({ key: 'membership', visibility: searchMetaVisibility.membership, definitions: playlistMembershipMetaFilterDefinitions, counts: playlistMembershipCounts, allLabel: 'Playlist membership', kind: 'videos' }),
      ]),
      ...(uploaderCategoryDefinitions.length ? [
        facetHtml({ key: 'uploaderCategory', visibility: searchMetaVisibility.uploaderCategory, definitions: uploaderCategoryDefinitions, counts: uploaderCategoryCounts, allLabel: 'Uploader category', kind: 'videos' }),
      ] : []),
      facetHtml({ key: 'aiDisclosure', visibility: searchMetaVisibility.aiDisclosure, definitions: aiDisclosureMetaFilterDefinitions, counts: aiDisclosureCounts, allLabel: 'AI disclosure', kind: 'videos' }),
      facetHtml({ key: 'videoNotes', visibility: searchMetaVisibility.videoNotes, definitions: noteMetaFilterDefinitions, counts: metaCounts?.videos, allLabel: 'Notes', kind: 'videos' }),
      ...browserVideoFilterPlugins().map(pluginVideoFacetHtml),
    ].join('')),
    showClips ? kindHtml('Clips', 'clips', clipCount, [
        facetHtml({
          key: 'clipOwnership',
          visibility: searchMetaVisibility.clipOwnership,
          definitions: playlistOwnershipMetaFilterDefinitions,
          counts: metaCounts?.clips,
          allLabel: 'Ownership',
          kind: 'clips',
        }),
        facetHtml({ key: 'clipNotes', visibility: searchMetaVisibility.clipNotes, definitions: noteMetaFilterDefinitions, counts: metaCounts?.clips, allLabel: 'Notes', kind: 'clips' }),
        ...browserClipFilterPlugins().map(pluginClipFacetHtml),
      ].join('')) : '',
    kindHtml('Playlists', 'playlists', metaCounts?.playlists?.total, [
      facetHtml({ key: 'playlistAvailability', visibility: searchMetaVisibility.playlistAvailability, definitions: playlistAvailabilityMetaFilterDefinitions, counts: metaCounts?.playlists, allLabel: 'Availability', kind: 'playlists' }),
      facetHtml({ key: 'playlistOwnership', visibility: searchMetaVisibility.playlistOwnership, definitions: playlistOwnershipMetaFilterDefinitions, counts: metaCounts?.playlists, allLabel: 'Ownership', kind: 'playlists' }),
      facetHtml({ key: 'playlistNotes', visibility: searchMetaVisibility.playlistNotes, definitions: noteMetaFilterDefinitions, counts: metaCounts?.playlists, allLabel: 'Notes', kind: 'playlists' }),
    ].join('')),
    kindHtml('Channels', 'channels', metaCounts?.channels?.total, [
      facetHtml({ key: 'channelSubscription', visibility: searchMetaVisibility.channelSubscription, definitions: channelSubscriptionMetaFilterDefinitions, counts: metaCounts?.channels, allLabel: 'Subscription', kind: 'channels' }),
      facetHtml({ key: 'channelStatus', visibility: searchMetaVisibility.channelStatus, definitions: channelStatusMetaFilterDefinitions, counts: metaCounts?.channels, allLabel: 'Status', kind: 'channels' }),
      facetHtml({ key: 'channelNotes', visibility: searchMetaVisibility.channelNotes, definitions: noteMetaFilterDefinitions, counts: metaCounts?.channels, allLabel: 'Notes', kind: 'channels' }),
    ].join('')),
    ...browserResultSearchPlugins().map(plugin => kindHtml(
      plugin.search.label || plugin.id,
      plugin.id,
      resultCounts?.plugins?.[plugin.id]
        ?? Number(plugin.search.catalogCount?.(browserPluginStatus(plugin.id)) || 0),
      '',
    )),
  ].filter(Boolean).join('');
}

function renderSearchMetaFilters({
  metaCounts = null,
  videoTypeCounts = null,
  broadcastStatusCounts = null,
  aiDisclosureCounts = null,
  reactionCounts = null,
  completionCounts = null,
  playlistMembershipCounts = null,
  uploaderCategoryCounts = null,
  counts = null,
} = {}) {
  const countsPending = metaCounts === null || metaCounts === undefined;
  renderedSearchFilterPayload = {
    metaCounts,
    videoTypeCounts,
    broadcastStatusCounts,
    aiDisclosureCounts,
    reactionCounts,
    completionCounts,
    playlistMembershipCounts,
    uploaderCategoryCounts,
    counts,
  };
  renderedSearchFilterContext = searchContextKind();
  syncUploaderCategoryVisibility(uploaderCategoryCounts);
  const template = document.createElement('template');
  template.innerHTML = searchMetaFiltersHtml(
    metaCounts,
    videoTypeCounts,
    broadcastStatusCounts,
    aiDisclosureCounts,
    reactionCounts,
    completionCounts,
    playlistMembershipCounts,
    uploaderCategoryCounts,
    counts,
  );
  const sections = new Map(
    [...template.content.querySelectorAll('[data-search-filter-section]')]
      .map(section => [section.dataset.searchFilterSection, section]),
  );
  for (const slot of searchFilterRegion.querySelectorAll('[data-search-filter-slot]')) {
    const kind = slot.dataset.searchFilterSlot || '';
    const section = sections.get(kind);
    slot.replaceChildren(...(section ? [...section.childNodes] : []));
    const count = section?.dataset.searchFilterCount;
    const kindCount = searchFilterRegion.querySelector(`[data-search-kind-count="${kind}"]`);
    if (kindCount) kindCount.textContent = searchKindEnabled(kind) ? (count || '') : '';
  }
  const renderedFacetKeys = new Set(
    ['videos', 'clips', 'playlists', 'channels'].flatMap(searchKindFacetKeys),
  );
  for (const key of renderedFacetKeys) {
    syncMetaFilterGroup(`search-${key}`, countsPending);
  }
  for (const plugin of browserVideoFilterPlugins()) {
    syncMetaFilterGroup(`search-plugin-${plugin.id}`, countsPending);
  }
  for (const plugin of browserClipFilterPlugins()) {
    syncMetaFilterGroup(`search-clip-plugin-${plugin.id}`, countsPending);
  }
  for (const kind of [
    'videos',
    'clips',
    'playlists',
    'channels',
    ...browserResultSearchPlugins().map(plugin => plugin.id),
  ]) {
    syncSearchKindFilter(kind, true, countsPending);
  }
  updateSearchMetaProgress();
}

function applySearchFilterTreeNodeState(nodeId) {
  const button = [...searchFilterRegion.querySelectorAll('[data-search-tree-toggle]')]
    .find(candidate => candidate.dataset.searchTreeToggle === nodeId);
  if (!(button instanceof HTMLButtonElement)) return;
  const expanded = searchFilterTreeExpanded.has(nodeId);
  const controlsId = button.getAttribute('aria-controls') || '';
  const children = controlsId ? document.getElementById(controlsId) : null;
  button.setAttribute('aria-expanded', String(expanded));
  const label = button.getAttribute('aria-label')?.replace(/^(?:Collapse|Expand)\s+/, '') || 'filters';
  button.setAttribute('aria-label', `${expanded ? 'Collapse' : 'Expand'} ${label}`);
  button.title = `${expanded ? 'Collapse' : 'Expand'} ${label}`;
  if (children instanceof HTMLElement) children.hidden = !expanded;
}

function toggleSearchFilterTreeNode(nodeId) {
  const wasExpanded = searchFilterTreeExpanded.has(nodeId);
  if (wasExpanded) searchFilterTreeExpanded.delete(nodeId);
  else searchFilterTreeExpanded.add(nodeId);
  applySearchFilterTreeNodeState(nodeId);
  saveSearchFilterTreePreference(nodeId, wasExpanded);
}

function syncSearchFiltersForSelection() {
  const historySelected = selected === '__history__';
  const contextKind = searchContextKind();
  const alreadyHidden = searchFilterTree.hidden;
  searchFilters.hidden = false;
  searchFilterTree.hidden = historySelected;
  for (const label of searchInFields.querySelectorAll('.core-library-search-field')) {
    label.hidden = historySelected;
    label.style.display = historySelected ? 'none' : '';
    const input = label.querySelector('input.search-field');
    if (input instanceof HTMLInputElement) input.disabled = historySelected;
  }
  syncBrowserPluginSearchFieldVisibility();
  const placeholders = {
    videos: 'Search videos',
    clips: 'Search clips',
    playlists: 'Search playlists',
    channels: 'Search channels',
  };
  search.placeholder = selected === '__history__'
    ? 'Search history'
    : (
      selected.startsWith('__playlist__:')
        ? 'Search this playlist'
        : (
          selected.startsWith('__channel__:')
            ? 'Search channel'
            : (placeholders[contextKind] || 'Search everything')
        )
    );
  if (!historySelected && renderedSearchFilterContext !== contextKind) {
    renderSearchMetaFilters();
  }
  if (!historySelected || alreadyHidden) return;
  clearSearchMetaVisibility();
  renderSearchMetaFilters();
}

function searchResultsSortHtml() {
  const options = [
    ['relevance', 'Relevance'],
    ['title', 'Title A-Z'],
    ['title_desc', 'Title Z-A'],
    ['newest', 'Newest'],
    ['oldest', 'Oldest'],
    ['most_watched', 'Most watched'],
    ['type', 'Type'],
  ];
  return `
    <label class="view-sort">Sort
      <select data-search-sort>
        ${options.map(([optionValue, label]) => `<option value="${optionValue}" ${searchResultsSort === optionValue ? 'selected' : ''}>${label}</option>`).join('')}
      </select>
    </label>
  `;
}

function cardLayoutIconSvg(layout) {
  if (layout === 'detailed') {
    return '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="2" y="4" width="16" height="12" rx="1"></rect><path d="M8 4v12M10.5 7h5M10.5 10h5M10.5 13h3.5"></path></svg>';
  }
  if (layout === 'compact') {
    return '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="2" y="3" width="16" height="4" rx="1"></rect><rect x="2" y="8" width="16" height="4" rx="1"></rect><rect x="2" y="13" width="16" height="4" rx="1"></rect></svg>';
  }
  return '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="2" y="2" width="7" height="7" rx="1"></rect><rect x="11" y="2" width="7" height="7" rx="1"></rect><rect x="2" y="11" width="7" height="7" rx="1"></rect><rect x="11" y="11" width="7" height="7" rx="1"></rect></svg>';
}

function cardLayoutHtml(activeLayout, context) {
  const options = [
    ['grid', 'Grid'],
    ['compact', 'Compact list'],
    ['detailed', 'Detailed list'],
  ];
  return `
    <div class="card-layout-control" role="group" aria-label="Card layout">
      ${options.map(([value, label]) => `
        <button class="card-layout-button${activeLayout === value ? ' active' : ''}" type="button" data-card-layout="${value}" data-card-layout-context="${context}" title="${label}" aria-label="${label}" aria-pressed="${activeLayout === value}">
          ${cardLayoutIconSvg(value)}
        </button>
      `).join('')}
    </div>
  `;
}

function rightPanelListMetaHtml(
  summary,
  { showLayout = false, layout = cardLayoutFor('search'), layoutContext = 'search', sortHtml = '' } = {},
) {
  return `
    <div class="search-result-meta">
      <div class="search-result-summary">
        <span>${escapeHtml(summary)}</span>
        ${(showLayout || sortHtml) ? `
          <span class="result-view-controls">
            ${showLayout ? cardLayoutHtml(layout, layoutContext) : ''}
            ${sortHtml}
          </span>
        ` : ''}
      </div>
    </div>
  `;
}

function cardLayoutFor(context) {
  return cardLayoutPreferences[context] || '';
}

function activeCardLayoutContext() {
  if (selected === '__search__') return 'search';
  if (selected === '__history__') return 'history';
  if (selected.startsWith('__playlist__:')) return 'playlist';
  if (selected.startsWith('__channel__:')) {
    return browserChannelVideoTab(channelDetailTab)
      ? 'channel-playlisted-videos'
      : `channel-${channelDetailTab}`;
  }
  return '';
}

function applyCardLayout(context) {
  const layout = cardLayoutFor(context);
  if (!layout) return;
  const historyLayout = context === 'history' || context === 'channel-history';
  grid.className = `grid search-grid${historyLayout ? ' history-list' : ''} layout-${layout}`;
  if (!historyLayout) return;
  for (const card of grid.querySelectorAll('.history-card')) {
    card.classList.toggle('history-row', layout !== 'grid');
  }
}

function playlistSourceLinksHtml(video) {
  const links = Array.isArray(video.playlist_links) && video.playlist_links.length
    ? video.playlist_links
    : [{ playlist_id: video.playlist_id, title: video.playlist_title, removed: wasRemovedByMeFromPlaylist(video) }];
  const rows = links
    .filter(link => link && link.playlist_id && link.title && !link.removed)
    .map(link => {
      return `<div><a class="playlist-link" href="${localPlaylistHref(link.playlist_id)}">${escapeHtml(link.title)}</a></div>`;
    });
  return rows.length ? `<div class="details playlist-sources">${rows.join('')}</div>` : '';
}

function unavailableLabel(video) {
  return videoAvailabilityValue(video) === 'unavailable' ? 'Unavailable' : '';
}

function videoAvailabilityValue(video) {
  const category = String(video.availability_category || '').trim().toLowerCase();
  return ['public', 'unlisted', 'private', 'members_only', 'unavailable', 'unknown'].includes(category)
    ? category
    : 'unknown';
}

function videoAvailabilityHtml(video) {
  const availability = videoAvailabilityValue(video);
  if (availability === 'members_only') {
    return badgeRowsHtml([{ label: 'Members only' }]);
  }
  if (availability === 'public' || availability === 'unlisted' || availability === 'private') {
    const label = availability.charAt(0).toUpperCase() + availability.slice(1);
    return `<div class="video-availability">${visibilityLabelHtml(availability, label)}</div>`;
  }
  if (availability === 'unavailable') {
    return '<div class="video-availability unavailable">Unavailable</div>';
  }
  return '<div class="video-availability">Unknown</div>';
}

function liveBroadcastIconHtml() {
  return `
    <svg class="video-type-icon live-icon" xmlns="http://www.w3.org/2000/svg" height="24" viewBox="0 0 24 24" width="24" focusable="false" aria-hidden="true">
      <path clip-rule="evenodd" d="M18.364 4.224a1 1 0 011.414 0 11 11 0 010 15.557 1 1 0 01-1.414-1.414 9 9 0 000-12.729 1 1 0 010-1.414ZM4.222 4.222a1 1 0 011.414 1.415 9 9 0 000 12.728 1 1 0 11-1.414 1.414 11.002 11.002 0 010-15.557Zm3.181 3.181a1.002 1.002 0 011.415 1.415 4.503 4.503 0 00-.975 4.904c.226.545.558 1.042.975 1.46a1.001 1.001 0 01-1.415 1.414 6.502 6.502 0 010-9.193Zm7.779 0c.39-.39 1.024-.39 1.415 0a6.5 6.5 0 010 9.193 1.001 1.001 0 01-1.415-1.415 4.5 4.5 0 000-6.363 1.001 1.001 0 010-1.415ZM12 10a2 2 0 110 4 2 2 0 010-4Z" fill-rule="evenodd"></path>
    </svg>
  `;
}

function noteIconHtml() {
  return `
    <svg class="annotation-note-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"></path>
      <path d="M14 2v6h6"></path>
      <path d="M8 13h8"></path>
      <path d="M8 17h6"></path>
    </svg>
  `;
}

function aiDisclosureIconHtml() {
  return `
    <svg class="ai-disclosure-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="m12 3-1.4 4.1a5.5 5.5 0 0 1-3.5 3.5L3 12l4.1 1.4a5.5 5.5 0 0 1 3.5 3.5L12 21l1.4-4.1a5.5 5.5 0 0 1 3.5-3.5L21 12l-4.1-1.4a5.5 5.5 0 0 1-3.5-3.5L12 3Z"></path>
    </svg>
  `;
}

function aiDisclosureDecoratorHtml(video) {
  if (Number(video?.ai_disclosure) !== 1) return '';
  const disclosureText = String(video?.ai_disclosure_text || '').trim();
  return `
    <span class="ai-disclosure-decorator" title="${escapeHtml(disclosureText || 'Made with AI')}">
      ${aiDisclosureIconHtml()}<span>Made with AI</span>
    </span>
  `;
}

function autoDubbedDecoratorHtml(video) {
  if (Number(video?.auto_dubbed) !== 1) return '';
  return `
    <span class="auto-dubbed-decorator" title="YouTube provides automatically generated audio tracks for this video">
      <svg class="auto-dubbed-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M11 5 6 9H3v6h3l5 4V5Z"></path>
        <path d="M15 8a6 6 0 0 1 0 8M18 5a10 10 0 0 1 0 14"></path>
      </svg><span>Auto-dubbed</span>
    </span>
  `;
}

function annotationCardOptions(entity) {
  const note = String(entity?.note || '').trim();
  const tags = Array.isArray(entity?.tags)
    ? entity.tags.map(tag => String(tag || '').trim()).filter(Boolean)
    : [];
  if (!note && !tags.length) return {};
  return {
    annotationMetaHtml: `
      <div class="entity-annotations">
        ${note ? `<span class="annotation-note-indicator">${noteIconHtml()}<span>Notes</span></span>` : ''}
        ${tags.map(tag => `<span class="annotation-tag">#${escapeHtml(tag)}</span>`).join('')}
      </div>
    `,
    annotationNoteHtml: note
      ? `<div class="entity-note-text">${escapeHtml(note)}</div>`
      : '',
  };
}

function liveNowBroadcastIconHtml() {
  return `
    <svg class="video-type-icon live-now-icon" xmlns="http://www.w3.org/2000/svg" height="12" viewBox="0 0 12 12" width="12" focusable="false" aria-hidden="true">
      <path clip-rule="evenodd" d="M2.111 2.111a.5.5 0 11.707.707 4.501 4.501 0 000 6.364.5.5 0 01-.707.707 5.5 5.5 0 010-7.778Zm7.07 0a.5.5 0 01.708 0 5.5 5.5 0 010 7.778.5.5 0 11-.707-.707 4.5 4.5 0 000-6.364.5.5 0 010-.707ZM3.703 3.702a.5.5 0 11.707.707 2.25 2.25 0 000 3.182.5.5 0 01-.707.707 3.25 3.25 0 01-.705-3.542 3.25 3.25 0 01.705-1.054Zm3.889 0a.5.5 0 01.707 0 3.25 3.25 0 010 4.596.5.5 0 01-.707-.707 2.25 2.25 0 000-3.182.5.5 0 010-.707ZM6 5a1 1 0 110 2 1 1 0 010-2Z" fill-rule="evenodd"></path>
    </svg>
  `;
}

function broadcastStatusLabel(video) {
  const status = String(video?.broadcast_status || '').trim().toLowerCase();
  if (status === 'live') return 'Live now';
  if (status === 'ended') return 'Streamed live';
  if (status === 'upcoming') return 'Upcoming live';
  return 'Livestream';
}

function videoTypeDecoratorHtml(video) {
  const isVideoRecord = typeof video !== 'string';
  const videoType = String(
    isVideoRecord ? video?.video_type || '' : video,
  ).trim().toLowerCase();
  if (videoType === 'video') {
    if (isVideoRecord) return '';
    return `
      <span class="video-type-decorator" title="Video" role="img" aria-label="Video">
        <svg class="video-type-icon youtube-video-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28.57 20" focusable="false" aria-hidden="true">
          <path d="M27.9727 3.12324C27.6435 1.89323 26.6768 0.926623 25.4468 0.597366C23.2197 2.24288e-07 14.285 0 14.285 0C14.285 0 5.35042 2.24288e-07 3.12323 0.597366C1.89323 0.926623 0.926623 1.89323 0.597366 3.12324C2.24288e-07 5.35042 0 10 0 10C0 10 2.24288e-07 14.6496 0.597366 16.8768C0.926623 18.1068 1.89323 19.0734 3.12323 19.4026C5.35042 20 14.285 20 14.285 20C14.285 20 23.2197 20 25.4468 19.4026C26.6768 19.0734 27.6435 18.1068 27.9727 16.8768C28.5701 14.6496 28.5701 10 28.5701 10C28.5701 10 28.5677 5.35042 27.9727 3.12324Z" fill="#FF0000"></path>
          <path d="M11.4253 14.2854L18.8477 10.0004L11.4253 5.71533V14.2854Z" fill="white"></path>
        </svg>
      </span>
    `;
  }
  if (videoType === 'short') {
    return `
      <span class="video-type-decorator" title="Shorts" role="img" aria-label="Shorts">
        <svg class="video-type-icon shorts-icon" xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" focusable="false" aria-hidden="true">
          <path d="m19.45,3.88c1.12,1.82.48,4.15-1.42,5.22l-1.32.74.94.41c1.36.58,2.27,1.85,2.35,3.27.08,1.43-.68,2.77-1.97,3.49l-8,4.47c-1.91,1.06-4.35.46-5.48-1.35-1.12-1.82-.48-4.15,1.42-5.22l1.33-.74-.94-.41c-1.36-.58-2.27-1.85-2.35-3.27-.08-1.43.68-2.77,1.97-3.49l8-4.47c1.91-1.06,4.35-.46,5.48,1.35Z" fill="#f03"></path>
          <path d="m10,15l5-3-5-3v6Z" fill="#fff"></path>
        </svg>
      </span>
    `;
  }
  if (videoType === 'livestream') {
    const broadcastStatus = String(video?.broadcast_status || '').trim().toLowerCase();
    const label = isVideoRecord ? broadcastStatusLabel(video) : '';
    return `
      <span class="video-type-decorator" title="${escapeHtml(label || 'Livestream')}" role="img" aria-label="${escapeHtml(label || 'Livestream')}">
        ${!isVideoRecord || broadcastStatus === 'live' ? liveNowBroadcastIconHtml() : liveBroadcastIconHtml()}
        ${label ? `<span class="video-type-label">${escapeHtml(label)}</span>` : ''}
      </span>
    `;
  }
  if (videoType === 'movie') {
    return `
      <span class="video-type-decorator" title="Movie" role="img" aria-label="Movie">
        <svg class="video-type-icon movie-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" focusable="false" aria-hidden="true">
          <rect width="18" height="18" x="3" y="3" rx="2"></rect>
          <path d="M7 3v18M17 3v18M3 7.5h4M17 7.5h4M3 12h18M3 16.5h4M17 16.5h4"></path>
        </svg>
        ${isVideoRecord ? '<span class="video-type-label">Movie</span>' : ''}
      </span>
    `;
  }
  return '';
}

function movieMetadataHtml(video) {
  if (String(video?.video_type || '').trim().toLowerCase() !== 'movie') return '';
  const offer = String(video?.movie_offer || '').trim();
  const rating = String(video?.movie_rating || '').trim();
  const releaseDate = String(video?.movie_release_date || '').trim();
  return detailRowHtml([
    offer
      ? `<span class="badge movie-offer${offer.toLowerCase() === 'free' ? ' free' : ''}">${escapeHtml(offer)}</span>`
      : '',
    rating ? `<span class="badge movie-rating" title="Movie rating">${escapeHtml(rating)}</span>` : '',
    releaseDate ? `<span>Release date ${escapeHtml(releaseDate)}</span>` : '',
  ], 'details movie-metadata');
}

function videoFeatureMetadataHtml(video) {
  const height = Number(video?.max_video_height || 0);
  const spatialFormat = String(video?.spatial_format || '').trim().toLowerCase();
  const stereoLayout = String(video?.stereo_layout || '').trim().toLowerCase();
  const dynamicRange = String(video?.dynamic_range || '').trim().toLowerCase();
  const license = String(video?.license || '').trim();
  const locationName = String(video?.location_name || '').trim();
  const features = [];
  if (height >= 4320) features.push('<span class="badge">8K</span>');
  else if (height >= 2160) features.push('<span class="badge">4K</span>');
  else if (height >= 720) features.push('<span class="badge">HD</span>');
  if (spatialFormat === '360') features.push('<span class="badge">360°</span>');
  if (spatialFormat === 'vr180') features.push('<span class="badge">VR180</span>');
  if (stereoLayout === 'left_right' || stereoLayout === 'top_bottom') {
    features.push('<span class="badge">3D</span>');
  }
  if (dynamicRange === 'hdr') features.push('<span class="badge">HDR</span>');
  if (license.toLowerCase().includes('creative commons')) {
    features.push(`<span class="badge" title="${escapeHtml(license)}">CC</span>`);
  }
  if (locationName) {
    features.push(`<span class="video-location">Location: ${escapeHtml(locationName)}</span>`);
  }
  return detailRowHtml(features, 'details video-feature-metadata');
}

function contentWarningHtml(video) {
  if (!video?.content_check_required) return '';
  const reason = String(video.content_check_reason || '').trim();
  return `<div class="details content-warning"><strong>Content warning</strong>${reason ? `: ${escapeHtml(reason)}` : ''}</div>`;
}

function archivarixStatusLabel(video) {
  const status = String(video.recovered_status || '');
  if (status === 'NOT_FOUND') return 'Archivarix: No results found';
  if (status.startsWith('DELETED_')) return `Archivarix: ${status}`;
  return '';
}

function archivarixStatusHtml(video) {
  const label = archivarixStatusLabel(video);
  return label ? `<div class="details archivarix-status"><span class="badge">${escapeHtml(label)}</span></div>` : '';
}

function archivarixVideoUrl(video) {
  return video.video_id ? `https://tube.archivarix.net/?q=${encodeURIComponent(video.video_id)}` : '';
}

function shouldShowArchivarixLink(video) {
  return Boolean(video.video_id && (unavailableLabel(video) || archivarixStatusLabel(video)));
}

function archivarixLinkHtml(video) {
  const url = archivarixVideoUrl(video);
  if (!url || !shouldShowArchivarixLink(video)) return '';
  return `<a class="playlist-link archivarix-link" href="${escapeHtml(url)}" target="_blank" rel="noreferrer">Archivarix</a>`;
}

function browserPluginRequestUrl(pluginId, path, params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    for (const item of (Array.isArray(value) ? value : [value])) {
      if (item !== null && item !== undefined) query.append(key, String(item));
    }
  }
  const suffix = query.toString();
  return `/api/plugins/${encodeURIComponent(pluginId)}/${path}${suffix ? `?${suffix}` : ''}`;
}

async function libraryVideos(videoIds) {
  const ids = [...new Set((videoIds || []).map(String).filter(Boolean))];
  const videos = new Map();
  for (let start = 0; start < ids.length; start += 100) {
    const query = new URLSearchParams();
    for (const videoId of ids.slice(start, start + 100)) query.append('id', videoId);
    const response = await fetch(`/api/videos/batch?${query}`, { cache: 'no-store' });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || `Library video request failed (${response.status})`);
    }
    for (const video of payload.videos || []) {
      if (video?.video_id) videos.set(video.video_id, video);
    }
  }
  return videos;
}

async function libraryChannels(channelIds) {
  const ids = [...new Set((channelIds || []).map(String).filter(Boolean))];
  const channels = new Map();
  for (let start = 0; start < ids.length; start += 100) {
    const query = new URLSearchParams();
    for (const channelId of ids.slice(start, start + 100)) query.append('id', channelId);
    const response = await fetch(`/api/channels/batch?${query}`, { cache: 'no-store' });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || `Library channel request failed (${response.status})`);
    }
    for (const channel of payload.channels || []) {
      if (channel?.channel_id) channels.set(channel.channel_id, channel);
    }
  }
  return channels;
}

function browserPluginHost(pluginId) {
  return {
    pluginId,
    status: browserPluginStatus(pluginId),
    supports: capability => browserPluginSupports(pluginId, capability),
    libraryChannels,
    libraryVideos,
    requestJson: async (path, params = {}) => {
      const response = await fetch(
        browserPluginRequestUrl(pluginId, path, params),
        { cache: 'no-store' },
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.error || `Plugin request failed (${response.status})`);
      }
      return payload;
    },
    postJson: async (path, body = {}, params = {}) => {
      if (!body || typeof body !== 'object' || Array.isArray(body)) {
        throw new TypeError('Plugin JSON body must be an object');
      }
      const response = await fetch(
        browserPluginRequestUrl(pluginId, path, params),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
          cache: 'no-store',
        },
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.error || `Plugin request failed (${response.status})`);
      }
      return payload;
    },
    ui: {
      createSearchVideoCard: searchVideoCardFor,
      createVideoCard: videoCardFor,
      escapeHtml,
      localChannelHref,
      localVideoHref,
      searchHighlight,
    },
  };
}

function entityCardEntry(kind, item, card) {
  return {
    card,
    entity: EntityCardExtensions.descriptor(kind, item),
  };
}

function decorateEntityCardBatch(entries, view, layout, generation) {
  return EntityCardExtensions.decorateBatch({
    entries,
    plugins: [...browserPlugins.values()],
    context: Object.freeze({ view, layout }),
    supports: browserPluginSupports,
    hostFor: browserPluginHost,
    isCurrent: () => generation === renderGeneration,
  });
}

async function fetchBrowserPluginSearches(query, limit, offset) {
  const counts = {};
  const errors = [];
  const results = [];
  let total = 0;
  let totalIsExact = true;
  let remaining = limit;
  let localOffset = offset;
  for (const plugin of browserResultSearchPlugins().filter(item => searchKindEnabled(item.id))) {
    let payload = { total: 0, totalIsExact: true, results: [] };
    if (query || plugin.search.fetchEmptyQuery === true) {
      try {
        payload = await plugin.search.fetch(
          { query, limit: Math.max(1, remaining), offset: localOffset },
          browserPluginHost(plugin.id),
        );
      } catch (error) {
        errors.push({
          id: plugin.id,
          label: plugin.search.label || plugin.id,
          message: error instanceof Error ? error.message : String(error),
        });
      }
    }
    const pluginTotal = Math.max(0, Number(payload.total || 0));
    const rows = Array.isArray(payload.results) ? payload.results : [];
    counts[plugin.id] = pluginTotal;
    total += pluginTotal;
    totalIsExact = totalIsExact && payload.totalIsExact !== false;
    if (remaining > 0 && localOffset < pluginTotal) {
      const visibleRows = rows.slice(0, remaining);
      results.push(...visibleRows.map(item => ({
        kind: 'plugin',
        pluginId: plugin.id,
        item,
      })));
      remaining -= visibleRows.length;
    }
    localOffset = Math.max(0, localOffset - pluginTotal);
  }
  return {
    counts,
    coreOffset: localOffset,
    errors,
    remaining,
    results,
    total,
    totalIsExact,
  };
}

async function prepareBrowserSearchResultPresentations(results, errors, query, generation) {
  const prepared = await SearchResultPresentations.prepareBatch({
    results,
    plugins: browserSearchPlugins(),
    context: Object.freeze({ query }),
    hostFor: browserPluginHost,
    applies: (plugin, result) => (
      searchKindEnabled(plugin.id)
      || (result.kind === 'clip' && result.pluginFacets?.[plugin.id])
    ),
    isCurrent: () => generation === renderGeneration,
  });
  const seenFailures = new Set();
  for (const failure of prepared.failures) {
    const plugin = browserSearchPlugin(failure.pluginId);
    const message = failure.error instanceof Error
      ? failure.error.message
      : String(failure.error);
    const key = `${failure.pluginId}:${message}`;
    if (seenFailures.has(key)) continue;
    seenFailures.add(key);
    errors.push({
      id: failure.pluginId,
      label: plugin?.search?.label || failure.pluginId,
      message,
    });
  }
  return prepared.presentations;
}

function appendPluginSearchWarnings(container, errors) {
  for (const pluginError of errors) {
    const warning = document.createElement('div');
    warning.className = 'status plugin-search-warning';
    warning.textContent = `${pluginError.label} unavailable: ${pluginError.message}`;
    container.append(warning);
  }
}

async function fetchOmniSearch(query, page = currentPage) {
  const size = pageSizeNumber();
  const limit = Number.isFinite(size) ? size : 5000;
  const requestedPage = Math.max(1, Number(page) || 1);
  const offset = (requestedPage - 1) * limit;
  const searchFieldsValue = searchFieldParamValue() || '__none__';
  const kindsValue = selectedSearchResultKinds().join(',') || '__none__';
  const metaCountsKey = JSON.stringify([
    query,
    searchFieldsValue,
    kindsValue,
    searchPlaylistGroupKey,
    searchChannelGroupKey,
    partialCompletionMinimumPercent,
    browserSearchPlugins()
      .map(browserPluginStateKey)
      .filter(Boolean),
  ]);
  const metaCountsCache = omniMetaCountsCache;
  const videoTypeCountsCache = omniVideoTypeCountsCache;
  const broadcastStatusCountsCache = omniBroadcastStatusCountsCache;
  const aiDisclosureCountsCache = omniAiDisclosureCountsCache;
  const videoPluginFacetCountsCache = omniVideoPluginFacetCountsCache;
  const clipPluginFacetCountsCache = omniClipPluginFacetCountsCache;
  const reactionCountsCache = omniReactionCountsCache;
  const completionCountsCache = omniCompletionCountsCache;
  const playlistMembershipCountsCache = omniPlaylistMembershipCountsCache;
  const uploaderCategoryCountsCache = omniUploaderCategoryCountsCache;
  const coreParams = new URLSearchParams({
    q: query,
    search_fields: searchFieldsValue,
    kinds: kindsValue,
    playlist_group_key: searchPlaylistGroupKey,
    channel_group_key: searchChannelGroupKey,
    video_type: metaFilterParamValue(searchMetaVisibility.videoType),
    video_broadcast_status: metaFilterParamValue(searchMetaVisibility.broadcastStatus),
    video_ai_disclosure: metaFilterParamValue(searchMetaVisibility.aiDisclosure),
    video_meta: metaFilterParamValue(searchMetaVisibility.videos, ['removed']),
    video_reaction: metaFilterParamValue(searchMetaVisibility.reactions),
    video_completion: metaFilterParamValue(searchMetaVisibility.completion),
    video_completion_min_percent: String(partialCompletionMinimumPercent),
    video_playlist_membership: metaFilterParamValue(searchMetaVisibility.membership),
    video_notes: metaFilterParamValue(searchMetaVisibility.videoNotes),
    clip_ownership: metaFilterParamValue(searchMetaVisibility.clipOwnership),
    clip_notes: metaFilterParamValue(searchMetaVisibility.clipNotes),
    channel_subscription: metaFilterParamValue(searchMetaVisibility.channelSubscription),
    channel_status: metaFilterParamValue(searchMetaVisibility.channelStatus),
    channel_notes: metaFilterParamValue(searchMetaVisibility.channelNotes),
    playlist_meta: metaFilterParamValue(searchMetaVisibility.playlistAvailability),
    playlist_ownership: metaFilterParamValue(searchMetaVisibility.playlistOwnership),
    playlist_notes: metaFilterParamValue(searchMetaVisibility.playlistNotes),
    sort: searchResultsSort,
  });
  if (!allMetaFiltersEnabled(searchMetaVisibility.uploaderCategory)) {
    coreParams.set(
      'video_uploader_category',
      metaFilterParamValue(searchMetaVisibility.uploaderCategory),
    );
  }
  const pluginKey = browserSearchPlugins()
    .map(browserPluginStateKey)
    .filter(Boolean)
    .join(',');
  const videoPluginFacetCountsKey = JSON.stringify([
    query,
    searchFieldsValue,
    kindsValue,
    searchPlaylistGroupKey,
    searchChannelGroupKey,
    metaFilterParamValue(searchMetaVisibility.videoType),
    metaFilterParamValue(searchMetaVisibility.broadcastStatus),
    metaFilterParamValue(searchMetaVisibility.aiDisclosure),
    metaFilterParamValue(searchMetaVisibility.videos, ['removed']),
    metaFilterParamValue(searchMetaVisibility.reactions),
    metaFilterParamValue(searchMetaVisibility.completion),
    String(partialCompletionMinimumPercent),
    metaFilterParamValue(searchMetaVisibility.membership),
    metaFilterParamValue(searchMetaVisibility.uploaderCategory),
    metaFilterParamValue(searchMetaVisibility.videoNotes),
    query ? browserVideoFilterPlugins().map(browserVideoFacetSearchActive) : [],
  ]);
  const clipPluginFacetCountsKey = JSON.stringify([
    query,
    searchFieldsValue,
    kindsValue,
    searchPlaylistGroupKey,
    searchChannelGroupKey,
    metaFilterParamValue(searchMetaVisibility.clipOwnership),
    metaFilterParamValue(searchMetaVisibility.clipNotes),
    query ? browserClipFilterPlugins().map(browserClipFacetSearchActive) : [],
  ]);
  const key = `${coreParams.toString()}&plugins=${encodeURIComponent(pluginKey)}&limit=${limit}&offset=${offset}`;
  return cachedRequest(omniSearchCache, key, async () => {
    const pluginPayload = await fetchBrowserPluginSearches(query, limit, offset);
    const requestParams = new URLSearchParams(coreParams);
    for (const plugin of browserVideoFilterPlugins()) {
      const state = browserVideoFacetState(plugin);
      requestParams.append('video_facet_plugin', plugin.id);
      if (state.present && !state.absent) {
        requestParams.append('video_filter_plugin', plugin.id);
      } else if (state.absent && !state.present) {
        requestParams.append('video_exclude_filter_plugin', plugin.id);
      }
    }
    for (const plugin of browserClipFilterPlugins()) {
      const state = browserClipFacetState(plugin);
      requestParams.append('clip_facet_plugin', plugin.id);
      if (state.present && !state.absent) {
        requestParams.append('clip_filter_plugin', plugin.id);
      } else if (state.absent && !state.present) {
        requestParams.append('clip_exclude_filter_plugin', plugin.id);
      }
    }
    const videoSearchFieldPlugins = browserVideoSearchFieldPlugins();
    if (videoSearchFieldPlugins.length) {
      for (const plugin of videoSearchFieldPlugins) {
        requestParams.append('video_search_plugin', plugin.id);
      }
    } else {
      requestParams.append('video_search_plugin', '__none__');
    }
    const clipSearchFieldPlugins = browserClipSearchFieldPlugins();
    if (clipSearchFieldPlugins.length) {
      for (const plugin of clipSearchFieldPlugins) {
        requestParams.append('clip_search_plugin', plugin.id);
      }
    } else {
      requestParams.append('clip_search_plugin', '__none__');
    }
    requestParams.set('limit', String(Math.max(1, pluginPayload.remaining)));
    requestParams.set('offset', String(pluginPayload.coreOffset));
    const response = await fetch(`/api/search?${requestParams}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Search failed: ${response.status}`);
    const corePayload = await response.json();
    const coreRows = pluginPayload.remaining
      ? (corePayload.results || []).slice(0, pluginPayload.remaining)
      : [];
    const payload = {
      ...corePayload,
      query,
      limit,
      offset,
      total: pluginPayload.total + Number(corePayload.total || 0),
      totalIsExact: pluginPayload.totalIsExact && corePayload.totalIsExact !== false,
      counts: {
        ...(corePayload.counts || {}),
        plugins: pluginPayload.counts,
      },
      pluginErrors: pluginPayload.errors,
      results: [...pluginPayload.results, ...coreRows],
    };
    if (!metaCountsCache.has(metaCountsKey)) {
      const stableMetaCounts = Object.fromEntries(
        Object.entries(payload.metaCounts || {}).map(([group, counts]) => [group, { ...counts }])
      );
      metaCountsCache.set(metaCountsKey, stableMetaCounts);
    }
    if (!videoPluginFacetCountsCache.has(videoPluginFacetCountsKey)) {
      videoPluginFacetCountsCache.set(
        videoPluginFacetCountsKey,
        Object.fromEntries(
          Object.entries(payload.metaCounts?.videoPlugins || {}).map(
            ([pluginId, counts]) => [pluginId, { ...counts }]
          )
        ),
      );
    }
    if (!clipPluginFacetCountsCache.has(clipPluginFacetCountsKey)) {
      clipPluginFacetCountsCache.set(
        clipPluginFacetCountsKey,
        Object.fromEntries(
          Object.entries(payload.metaCounts?.clipPlugins || {}).map(
            ([pluginId, counts]) => [pluginId, { ...counts }]
          )
        ),
      );
    }
    if (!reactionCountsCache.has(metaCountsKey)) {
      reactionCountsCache.set(metaCountsKey, { ...(payload.reactionCounts || {}) });
    }
    if (!videoTypeCountsCache.has(metaCountsKey)) {
      videoTypeCountsCache.set(metaCountsKey, { ...(payload.videoTypeCounts || {}) });
    }
    if (!broadcastStatusCountsCache.has(metaCountsKey)) {
      broadcastStatusCountsCache.set(
        metaCountsKey,
        { ...(payload.broadcastStatusCounts || {}) },
      );
    }
    if (!aiDisclosureCountsCache.has(metaCountsKey)) {
      aiDisclosureCountsCache.set(
        metaCountsKey,
        { ...(payload.aiDisclosureCounts || {}) },
      );
    }
    if (!completionCountsCache.has(metaCountsKey)) {
      completionCountsCache.set(metaCountsKey, { ...(payload.completionCounts || {}) });
    }
    if (!playlistMembershipCountsCache.has(metaCountsKey)) {
      playlistMembershipCountsCache.set(
        metaCountsKey,
        { ...(payload.playlistMembershipCounts || {}) },
      );
    }
    if (!uploaderCategoryCountsCache.has(metaCountsKey)) {
      uploaderCategoryCountsCache.set(
        metaCountsKey,
        { ...(payload.uploaderCategoryCounts || {}) },
      );
    }
    const stableMetaCounts = {
      ...metaCountsCache.get(metaCountsKey),
      videoPlugins: videoPluginFacetCountsCache.get(videoPluginFacetCountsKey),
      clipPlugins: clipPluginFacetCountsCache.get(clipPluginFacetCountsKey),
    };
    const stablePayload = {
      ...payload,
      metaCounts: stableMetaCounts,
      videoTypeCounts: videoTypeCountsCache.get(metaCountsKey),
      broadcastStatusCounts: broadcastStatusCountsCache.get(metaCountsKey),
      aiDisclosureCounts: aiDisclosureCountsCache.get(metaCountsKey),
      reactionCounts: reactionCountsCache.get(metaCountsKey),
      completionCounts: completionCountsCache.get(metaCountsKey),
      playlistMembershipCounts: playlistMembershipCountsCache.get(metaCountsKey),
      uploaderCategoryCounts: uploaderCategoryCountsCache.get(metaCountsKey),
    };
    return stablePayload;
  }, adjacentPageCacheLimit);
}

async function fetchEntitySearchFilters(category, entityId) {
  const resultKind = {
    videos: 'video',
    clips: 'clip',
    channels: 'channel',
  }[category];
  if (!resultKind || !entityId) return null;
  const params = new URLSearchParams({
    q: entityId,
    search_fields: 'titles',
    kinds: resultKind,
    limit: '1',
    offset: '0',
  });
  for (const plugin of category === 'videos'
    ? browserVideoFilterPlugins()
    : (category === 'clips' ? browserClipFilterPlugins() : [])) {
    params.append(category === 'videos' ? 'video_facet_plugin' : 'clip_facet_plugin', plugin.id);
  }
  params.append('video_search_plugin', '__none__');
  params.append('clip_search_plugin', '__none__');
  const response = await fetch(`/api/search?${params}`, { cache: 'no-store' });
  if (!response.ok) throw new Error(`Selected item filters failed: ${response.status}`);
  return response.json();
}

function hydrateEntitySearchFilters(category, entityId, generation) {
  void withLoadingStatus(() => fetchEntitySearchFilters(category, entityId)).then(payload => {
    if (!payload) return;
    if (generation !== renderGeneration || selectedEntityCategory() !== category) return;
    renderSearchMetaFilters(payload);
  }).catch(() => {
    // A failed facet request must not block the entity detail itself.
  });
}

async function fetchVideoCollection({
  scope = 'playlist',
  playlistId = '',
  channelId = '',
  sort = 'newest_added',
  query = '',
  visibility = playlistVisibility,
  videoTypes = null,
  broadcastStatuses = null,
  aiDisclosures = null,
  completion = null,
  reactions = null,
  uploaderCategories = null,
  notes = null,
  useSearchFacets = false,
  partialMinimumPercent = 1,
  duplicatesOnly = false,
  page = currentPage,
  pageSize = null,
} = {}) {
  const base = playlistId
    ? `/api/playlists/${encodeURIComponent(playlistId)}/videos`
    : '/api/videos';
  partialMinimumPercent = boundedPartialMinimumPercent(partialMinimumPercent);
  const searchFieldsValue = searchFieldParamValue() || '__none__';
  const pluginStateKey = useSearchFacets
    ? browserVideoFilterPlugins().map(browserPluginStateKey).join(',')
    : '';
  const metaCountsKey = JSON.stringify([
    scope,
    playlistId,
    channelId,
    query,
    searchFieldsValue,
    partialMinimumPercent,
    pluginStateKey,
  ]);
  const pluginFacetCountsKey = JSON.stringify([
    scope,
    playlistId,
    channelId,
    query,
    searchFieldsValue,
    browserVideoSearchFieldPlugins().map(plugin => plugin.id),
    videoTypes ? metaFilterParamValue(videoTypes) : '',
    broadcastStatuses ? metaFilterParamValue(broadcastStatuses) : '',
    aiDisclosures ? metaFilterParamValue(aiDisclosures) : '',
    metaFilterParamValue(visibility),
    completion ? metaFilterParamValue(completion) : '',
    reactions ? metaFilterParamValue(reactions) : '',
    uploaderCategories ? metaFilterParamValue(uploaderCategories) : '',
    notes ? metaFilterParamValue(notes) : '',
    partialMinimumPercent,
    duplicatesOnly,
  ]);
  const metaCountsCache = videoMetaCountsCache;
  const params = new URLSearchParams({
    scope,
    channel_id: channelId,
    q: query,
    public: visibility.public === false ? '0' : '1',
    unlisted: visibility.unlisted === false ? '0' : '1',
    private: visibility.private === false ? '0' : '1',
    members_only: visibility.members_only === false ? '0' : '1',
    unavailable: visibility.unavailable === false ? '0' : '1',
    unknown: visibility.unknown === false ? '0' : '1',
    removed: visibility.removed === false ? '0' : '1',
    duplicates: duplicatesOnly ? '1' : '0',
    sort,
  });
  if (useSearchFacets) params.set('search_fields', searchFieldsValue);
  if (videoTypes) params.set('video_type', metaFilterParamValue(videoTypes));
  if (broadcastStatuses) {
    params.set('broadcast_status', metaFilterParamValue(broadcastStatuses));
  }
  if (aiDisclosures) {
    params.set('ai_disclosure', metaFilterParamValue(aiDisclosures));
  }
  if (completion) params.set('completion', metaFilterParamValue(completion));
  if (completion) params.set('completion_min_percent', String(partialMinimumPercent));
  if (reactions) params.set('reaction', metaFilterParamValue(reactions));
  if (uploaderCategories && !allMetaFiltersEnabled(uploaderCategories)) {
    params.set('uploader_category', metaFilterParamValue(uploaderCategories));
  }
  if (notes) params.set('notes', metaFilterParamValue(notes));
  if (useSearchFacets) {
    for (const plugin of browserVideoFilterPlugins()) {
      const state = browserVideoFacetState(plugin);
      params.append('video_facet_plugin', plugin.id);
      if (state.present && !state.absent) {
        params.append('video_filter_plugin', plugin.id);
      } else if (state.absent && !state.present) {
        params.append('video_exclude_filter_plugin', plugin.id);
      }
    }
    const searchPlugins = browserVideoSearchFieldPlugins();
    if (searchPlugins.length) {
      for (const plugin of searchPlugins) params.append('video_search_plugin', plugin.id);
    } else {
      params.append('video_search_plugin', '__none__');
    }
  }
  const path = remoteListPath(base, params, page, pageSize);
  const payload = await fetchViewData(path);
  if (!metaCountsCache.has(metaCountsKey)) {
    metaCountsCache.set(metaCountsKey, { ...(payload.counts || {}) });
  }
  if (!videoTypeCountsCache.has(metaCountsKey)) {
    videoTypeCountsCache.set(metaCountsKey, { ...(payload.videoTypeCounts || {}) });
  }
  if (!videoBroadcastStatusCountsCache.has(metaCountsKey)) {
    videoBroadcastStatusCountsCache.set(
      metaCountsKey,
      { ...(payload.broadcastStatusCounts || {}) },
    );
  }
  if (!videoAiDisclosureCountsCache.has(metaCountsKey)) {
    videoAiDisclosureCountsCache.set(
      metaCountsKey,
      { ...(payload.aiDisclosureCounts || {}) },
    );
  }
  if (!videoCompletionCountsCache.has(metaCountsKey)) {
    videoCompletionCountsCache.set(metaCountsKey, { ...(payload.completionCounts || {}) });
  }
  if (!videoReactionCountsCache.has(metaCountsKey)) {
    videoReactionCountsCache.set(metaCountsKey, { ...(payload.reactionCounts || {}) });
  }
  if (!videoUploaderCategoryCountsCache.has(metaCountsKey)) {
    videoUploaderCategoryCountsCache.set(
      metaCountsKey,
      { ...(payload.uploaderCategoryCounts || {}) },
    );
  }
  if (!videoNoteCountsCache.has(metaCountsKey)) {
    videoNoteCountsCache.set(metaCountsKey, { ...(payload.noteCounts || {}) });
  }
  if (!videoPluginFacetCountsCache.has(pluginFacetCountsKey)) {
    videoPluginFacetCountsCache.set(
      pluginFacetCountsKey,
      Object.fromEntries(
        Object.entries(payload.metaCounts?.videoPlugins || {}).map(
          ([pluginId, counts]) => [pluginId, { ...counts }],
        ),
      ),
    );
  }
  return {
    ...payload,
    counts: metaCountsCache.get(metaCountsKey),
    videoTypeCounts: videoTypeCountsCache.get(metaCountsKey),
    broadcastStatusCounts: videoBroadcastStatusCountsCache.get(metaCountsKey),
    aiDisclosureCounts: videoAiDisclosureCountsCache.get(metaCountsKey),
    completionCounts: videoCompletionCountsCache.get(metaCountsKey),
    reactionCounts: videoReactionCountsCache.get(metaCountsKey),
    uploaderCategoryCounts: videoUploaderCategoryCountsCache.get(metaCountsKey),
    noteCounts: videoNoteCountsCache.get(metaCountsKey),
    metaCounts: {
      ...(payload.metaCounts || {}),
      videoPlugins: videoPluginFacetCountsCache.get(pluginFacetCountsKey),
    },
  };
}

async function fetchChannelPlaylists(channelReference, page = currentPage) {
  const path = remoteListPath(
    `/api/channels/${encodeChannelReference(channelReference)}/playlists`,
    { sort: 'title' },
    page,
  );
  return fetchViewData(path);
}

function renderScopedVideoSearchFacets(payload) {
  const distinctVideoCount = Number(
    payload.distinctTotal
    ?? Object.values(payload.counts || {}).reduce((sum, value) => sum + Number(value || 0), 0),
  );
  renderSearchMetaFilters({
    metaCounts: {
      videos: {
        ...(payload.counts || {}),
        ...(payload.noteCounts || {}),
        total: distinctVideoCount,
      },
      videoPlugins: payload.metaCounts?.videoPlugins || {},
    },
    videoTypeCounts: payload.videoTypeCounts || null,
    broadcastStatusCounts: payload.broadcastStatusCounts || null,
    aiDisclosureCounts: payload.aiDisclosureCounts || null,
    reactionCounts: payload.reactionCounts || null,
    completionCounts: payload.completionCounts || null,
    uploaderCategoryCounts: payload.uploaderCategoryCounts || null,
  });
  setPresetLinkCount('videos', distinctVideoCount);
  return distinctVideoCount;
}

function hydrateChannelVideoSearchFilters(channelId, channelLabel, generation) {
  void withLoadingStatus(() => fetchVideoCollection({
    ...channelScopedVideoCollectionOptions(channelId, 1),
    query: '',
    pageSize: 1,
  })).then(payload => {
    if (!payload || generation !== renderGeneration) return;
    if (!selected.startsWith('__channel__:') || channelDetailSearchActive) return;
    renderScopedVideoSearchFacets(payload);
    setPresetLinkLabel('videos', channelLabel || channelId);
  }).catch(() => {
    // A failed facet request must not block the channel detail itself.
  });
}

function scopedVideoSearchResults(rows) {
  return rows.map(video => ({
    kind: 'video',
    item: video,
    pluginFacets: { ...(video.pluginFacets || {}) },
    pluginSearchMatches: [...(video.pluginSearchMatches || [])],
  }));
}

function channelScopedVideoCollectionOptions(channelId, page = currentPage) {
  return {
    scope: 'channel',
    channelId,
    query: search.value.trim(),
    visibility: searchMetaVisibility.videos,
    videoTypes: searchMetaVisibility.videoType,
    broadcastStatuses: searchMetaVisibility.broadcastStatus,
    aiDisclosures: searchMetaVisibility.aiDisclosure,
    completion: searchMetaVisibility.completion,
    reactions: searchMetaVisibility.reactions,
    uploaderCategories: searchMetaVisibility.uploaderCategory,
    notes: searchMetaVisibility.videoNotes,
    useSearchFacets: true,
    partialMinimumPercent: partialCompletionMinimumPercent,
    sort: 'newest_added',
    page,
  };
}

function groupLinkFor(group, preset, membershipMap, childMap) {
  const link = document.createElement('a');
  link.className = 'group group-tree-action';
  link.href = searchPresetHref(preset, group.group_key);
  link.dataset.preset = preset;
  link.dataset.groupKey = group.group_key;
  link.innerHTML = `<span>${escapeHtml(group.name)}</span><span class="count">${groupCount(group.group_key, membershipMap, childMap)}</span>`;
  link.addEventListener('click', event => {
    handleSidebarLinkClick(
      event,
      () => activateSearchPreset(preset, group.group_key),
    );
  });
  return link;
}

function navigationGroupTreeNodeId(preset, groupKey) {
  return `${preset}:${groupKey}`;
}

function applyNavigationGroupTreeNodeState(
  nodeId,
  toggle,
  childContainer,
  label,
  associatedControls = [],
) {
  const expanded = !navigationGroupTreeCollapsed.has(nodeId);
  toggle.setAttribute('aria-expanded', String(expanded));
  toggle.setAttribute('aria-label', `${expanded ? 'Collapse' : 'Expand'} ${label}`);
  toggle.title = `${expanded ? 'Collapse' : 'Expand'} ${label}`;
  for (const control of associatedControls) {
    control.setAttribute('aria-expanded', String(expanded));
    control.title = `${expanded ? 'Collapse' : 'Expand'} ${label}`;
  }
  childContainer.hidden = !expanded;
}

function toggleNavigationGroupTreeNode(
  nodeId,
  toggle,
  childContainer,
  label,
  associatedControls = [],
) {
  const wasCollapsed = navigationGroupTreeCollapsed.has(nodeId);
  if (wasCollapsed) navigationGroupTreeCollapsed.delete(nodeId);
  else navigationGroupTreeCollapsed.add(nodeId);
  applyNavigationGroupTreeNodeState(
    nodeId,
    toggle,
    childContainer,
    label,
    associatedControls,
  );
  saveNavigationGroupTreePreference(nodeId, wasCollapsed);
}

function navigationGroupTreeToggleFor(
  nodeId,
  label,
  childContainer,
  associatedControls = [],
) {
  const childrenId = `navigation-group-tree-${++navigationGroupTreeDomId}`;
  childContainer.className = 'group-tree-children';
  childContainer.id = childrenId;
  const toggle = document.createElement('button');
  toggle.className = 'search-tree-toggle group-tree-toggle';
  toggle.type = 'button';
  toggle.dataset.groupTreeToggle = nodeId;
  toggle.setAttribute('aria-controls', childrenId);
  toggle.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"></path></svg>';
  applyNavigationGroupTreeNodeState(
    nodeId,
    toggle,
    childContainer,
    label,
    associatedControls,
  );
  toggle.addEventListener('click', () => {
    toggleNavigationGroupTreeNode(
      nodeId,
      toggle,
      childContainer,
      label,
      associatedControls,
    );
  });
  return toggle;
}

function appendGroupTree(section, group, preset, membershipMap, childMap, depth = 0) {
  const childGroups = childMap.get(group.group_key) || [];
  const row = document.createElement('div');
  row.className = `group-tree-row ${depth > 0 ? 'child' : ''}`;
  row.style.setProperty('--group-depth', String(depth));
  if (childGroups.length) {
    const nodeId = navigationGroupTreeNodeId(preset, group.group_key);
    const childContainer = document.createElement('div');
    const toggle = navigationGroupTreeToggleFor(
      nodeId,
      group.name,
      childContainer,
    );
    row.appendChild(toggle);
    row.appendChild(groupLinkFor(group, preset, membershipMap, childMap));
    section.appendChild(row);
    for (const child of childGroups) {
      appendGroupTree(
        childContainer,
        child,
        preset,
        membershipMap,
        childMap,
        depth + 1,
      );
    }
    section.appendChild(childContainer);
  } else {
    const spacer = document.createElement('span');
    spacer.className = 'group-tree-toggle-spacer';
    spacer.setAttribute('aria-hidden', 'true');
    row.appendChild(spacer);
    row.appendChild(groupLinkFor(group, preset, membershipMap, childMap));
    section.appendChild(row);
  }
}

function pluginNavigationLabel(pluginId) {
  const plugin = (data.plugins || []).find(candidate => candidate.id === pluginId);
  const label = String(plugin?.name || pluginId).trim().replace(/^YT\s+/i, '');
  return label || pluginId;
}

function appendPluginGroupTree(
  section,
  pluginId,
  rootGroups,
  preset,
  membershipMap,
  childMap,
) {
  const label = pluginNavigationLabel(pluginId);
  const nodeId = navigationGroupTreeNodeId(preset, `plugin-root:${pluginId}`);
  const childContainer = document.createElement('div');
  const row = document.createElement('div');
  row.className = 'group-tree-row plugin-group-tree-row';
  row.style.setProperty('--group-depth', '0');
  const labelNode = document.createElement('button');
  labelNode.type = 'button';
  labelNode.className = 'group group-tree-label';
  labelNode.dataset.groupTreeLabel = nodeId;
  labelNode.textContent = label;
  const toggle = navigationGroupTreeToggleFor(
    nodeId,
    label,
    childContainer,
    [labelNode],
  );
  labelNode.setAttribute('aria-controls', childContainer.id);
  labelNode.addEventListener('click', () => {
    toggleNavigationGroupTreeNode(
      nodeId,
      toggle,
      childContainer,
      label,
      [labelNode],
    );
  });
  row.appendChild(toggle);
  row.appendChild(labelNode);
  section.appendChild(row);
  for (const group of rootGroups) {
    appendGroupTree(
      childContainer,
      group,
      preset,
      membershipMap,
      childMap,
      1,
    );
  }
  section.appendChild(childContainer);
}

function appendNavigationGroupTrees(section, rootGroups, preset, membershipMap, childMap) {
  const entries = [];
  const pluginEntries = new Map();
  for (const group of rootGroups) {
    const pluginId = String(group.source_plugin_id || '').trim();
    if (!pluginId) {
      entries.push({ group });
      continue;
    }
    if (!pluginEntries.has(pluginId)) {
      const entry = { pluginId, groups: [] };
      pluginEntries.set(pluginId, entry);
      entries.push(entry);
    }
    pluginEntries.get(pluginId).groups.push(group);
  }
  for (const entry of entries) {
    if (entry.group) {
      appendGroupTree(section, entry.group, preset, membershipMap, childMap);
    } else {
      appendPluginGroupTree(
        section,
        entry.pluginId,
        entry.groups,
        preset,
        membershipMap,
        childMap,
      );
    }
  }
}

function sectionFor(label) {
  const section = document.createElement('section');
  section.className = 'group-section';
  const title = document.createElement('div');
  title.className = 'group-section-title';
  title.textContent = label;
  section.appendChild(title);
  groupsEl.appendChild(section);
  return section;
}

function presetLink(preset, label, count) {
  const link = document.createElement('a');
  link.className = 'group';
  link.href = searchPresetHref(preset);
  link.dataset.preset = preset;
  link.innerHTML = `<span>${escapeHtml(label)}</span><span class="count">${count}</span>`;
  link.addEventListener('click', event => {
    handleSidebarLinkClick(event, () => activateSearchPreset(preset));
  });
  return link;
}

function setPresetLinkCount(preset, count) {
  const link = groupsEl.querySelector(`.group[data-preset="${preset}"]`);
  const countNode = link?.querySelector('.count');
  if (countNode) countNode.textContent = filterCountText(count);
}

function setPresetLinkLabel(preset, label) {
  const link = groupsEl.querySelector(`.group[data-preset="${preset}"]`);
  const labelNode = link?.querySelector('span:not(.count)');
  if (labelNode) labelNode.textContent = label;
}

function searchFilterSlot(kind, className = 'search-filter-slot') {
  const slot = document.createElement('div');
  slot.className = className;
  slot.dataset.searchFilterSlot = kind;
  return slot;
}

function searchFilterMount(kind, navigationSection) {
  return searchContextKind() === kind ? navigationSection : searchFilterTree;
}

function appendSearchFilterCategory(container, kind, label, count) {
  const contextKind = searchContextKind();
  const filtersVisible = selected !== '__history__' && (!contextKind || contextKind === kind);
  if (!filtersVisible) return;
  if (contextKind === kind) {
    container.appendChild(searchFilterSlot(kind));
    return;
  }

  const nodeId = `kind:${kind}`;
  const expanded = searchFilterTreeExpanded.has(nodeId);
  const kindEnabled = searchKindEnabled(kind);
  const root = document.createElement('div');
  root.className = `search-meta-kind${kindEnabled ? '' : ' kind-disabled'}`;
  root.dataset.searchTreeNode = nodeId;
  root.innerHTML = `
    ${searchFilterTreeToggleHtml(nodeId, `${label} filters`)}
    <div class="search-meta-row-title">
      ${parentFilterCheckboxHtml('data-search-kind-filter', kind)}
      <span>${escapeHtml(label)}</span>
      <span class="count" data-search-kind-count="${escapeHtml(kind)}">${kindEnabled ? filterCountText(count) : ''}</span>
      <span class="search-meta-progress" data-search-meta-progress="${escapeHtml(kind)}" aria-hidden="true"></span>
    </div>
  `;
  const slot = searchFilterSlot(kind, 'search-meta-kind-children search-filter-slot');
  slot.id = searchFilterTreeChildrenId(nodeId);
  slot.dataset.searchTreeChildren = '';
  slot.hidden = !expanded;
  root.appendChild(slot);
  container.appendChild(root);
}

function appendPluginSearchFilters(container) {
  const plugins = browserResultSearchPlugins();
  if (!plugins.length || selected === '__history__' || searchContextKind()) return;
  for (const plugin of plugins) {
    const kind = plugin.id;
    const label = plugin.search.label || kind;
    const enabled = searchKindEnabled(kind);
    const row = document.createElement('div');
    row.className = `search-meta-kind${enabled ? '' : ' kind-disabled'}`;
    row.innerHTML = `
      <span class="search-tree-toggle-spacer" aria-hidden="true"></span>
      <label class="search-meta-row-title">
        ${parentFilterCheckboxHtml('data-search-kind-filter', kind)}
        <span>${escapeHtml(label)}</span>
        <span class="count" data-search-kind-count="${escapeHtml(kind)}">${enabled ? filterCountText(plugin.search.catalogCount?.(browserPluginStatus(kind))) : ''}</span>
        <span class="search-meta-progress" data-search-meta-progress="${escapeHtml(kind)}" aria-hidden="true"></span>
      </label>
    `;
    row.appendChild(searchFilterSlot(kind));
    container.appendChild(row);
  }
}

function searchNavigationHref() {
  if (selected !== '__search__' || activeSearchScope) return retainedSearchUrl || '/search';
  const params = new URLSearchParams();
  const query = search.value.trim();
  if (query) params.set('q', query);
  return appendUrlParams('/search', params);
}

function activeSidebarCategory() {
  if (selected === '__search__' && !activeSearchPreset) return activeSearchScope;
  if (selected.startsWith('__channel__:')) return 'videos';
  return selectedEntityCategory();
}

function syncSidebarSelection() {
  if (historyNav) historyNav.href = localViewHref('__history__');
  if (searchNav) searchNav.href = searchNavigationHref();
  const groupPresets = new Set(['playlist-group', 'channel-group']);
  const activeCategory = activeSidebarCategory();
  for (const link of groupsEl.querySelectorAll('.group')) {
    const activeGroupKey = activeSearchPreset === 'channel-group'
      ? searchChannelGroupKey
      : searchPlaylistGroupKey;
    const activeGroupPreset = (
      selected === '__search__'
      && groupPresets.has(activeSearchPreset)
      && link.dataset.preset === activeSearchPreset
      && link.dataset.groupKey === activeGroupKey
    );
    const activeNamedPreset = (
      selected === '__search__'
      && link.dataset.preset
      && !groupPresets.has(link.dataset.preset)
      && link.dataset.preset === activeSearchPreset
    );
    const activeScopedCategory = (
      activeCategory
      && link.dataset.preset === activeCategory
    );
    link.classList.toggle(
      'active',
      link.dataset.key === selected || activeGroupPreset || activeNamedPreset || activeScopedCategory,
    );
  }
  searchNav?.classList.toggle(
    'active',
    selected === '__search__' && !activeSearchScope && !activeSearchPreset,
  );
  historyNav?.classList.toggle('active', selected === '__history__');
}

function renderGroups() {
  if (!data) return;
  searchFilterTree.replaceChildren();
  groupsEl.replaceChildren();
  navigationGroupTreeDomId = 0;
  const counts = data.counts || {};
  const historyCount = historyNav?.querySelector('.count');
  if (historyCount) historyCount.textContent = counts.history || 0;

  const videoSection = sectionFor('Videos');
  videoSection.appendChild(presetLink('videos', 'Videos', counts.videos || 0));
  appendSearchFilterCategory(
    searchFilterMount('videos', videoSection),
    'videos',
    'Videos',
    counts.videos || 0,
  );
  if (Number(counts.clips || 0) > 0) {
    videoSection.appendChild(presetLink('clips', 'Clips', counts.clips));
    appendSearchFilterCategory(
      searchFilterMount('clips', videoSection),
      'clips',
      'Clips',
      counts.clips,
    );
  }

  const playlistSection = sectionFor('Playlists');
  playlistSection.appendChild(presetLink('playlists', 'Playlists', counts.playlists || 0));
  appendSearchFilterCategory(
    searchFilterMount('playlists', playlistSection),
    'playlists',
    'Playlists',
    counts.playlists || 0,
  );
  appendNavigationGroupTrees(
    playlistSection,
    playlistChildren.get('') || [],
    'playlist-group',
    playlistMemberships,
    playlistChildren,
  );

  const channelSection = sectionFor('Channels');
  channelSection.appendChild(presetLink('channels', 'Channels', counts.channels || 0));
  appendSearchFilterCategory(
    searchFilterMount('channels', channelSection),
    'channels',
    'Channels',
    counts.channels || 0,
  );
  appendNavigationGroupTrees(
    channelSection,
    channelChildren.get('') || [],
    'channel-group',
    channelMemberships,
    channelChildren,
  );
  appendPluginSearchFilters(searchFilterTree);
  renderSearchMetaFilters(renderedSearchFilterPayload);
  syncSidebarSelection();
}

async function renderBrowserPluginVideoPanels(videoId) {
  const panels = [];
  for (const plugin of browserPlugins.values()) {
    const extension = plugin.videoDetail;
    if (!extension || !browserPluginSupports(plugin.id, extension.capability)) continue;
    try {
      const panel = await extension.render(videoId, browserPluginHost(plugin.id));
      if (panel instanceof HTMLElement) panels.push(panel);
    } catch (_error) {
      // Optional plugin failures must not prevent the core video detail from rendering.
    }
  }
  return panels;
}

async function renderBrowserPluginClipPanels(clip) {
  const panels = [];
  for (const plugin of browserPlugins.values()) {
    const extension = plugin.clipDetail;
    if (!extension || !browserPluginSupports(plugin.id, extension.capability)) continue;
    try {
      const panel = await extension.render(clip, browserPluginHost(plugin.id));
      if (panel instanceof HTMLElement) panels.push(panel);
    } catch (_error) {
      // Optional plugin failures must not prevent the core clip detail from rendering.
    }
  }
  return panels;
}

function videoDetailCardFor(video) {
  const article = document.createElement('article');
  article.className = 'card video-detail';
  const titleText = displayVideoTitle(video);
  const watchUrl = youtubeWatchUrl(video, false);
  const channelName = displayVideoChannel(video);
  const channelUrl = displayVideoChannelLocalUrl(video);
  const thumbnail = video.metadata_thumbnail_path
    ? thumbnailWithProgress(video.metadata_thumbnail_path, video)
    : document.createElement('div');
  const annotation = annotationCardOptions(video);
  const body = document.createElement('div');
  body.className = 'body';
  body.innerHTML = `
    <div class="detail-layout">
      <div class="detail-thumb"></div>
      <div>
        ${channelName ? `<div class="details video-card-channel">${creatorHtml(video.metadata_channel_thumbnail_path, channelName, channelUrl)}</div>` : ''}
        <div class="title-row">
          <div class="video-title">${escapeHtml(titleText)}</div>
          ${watchUrl ? `<a class="external-link" href="${escapeHtml(watchUrl)}" target="_blank" rel="noreferrer" title="Open on YouTube" aria-label="Open ${escapeHtml(titleText)} on YouTube">${externalLinkSvg()}</a>` : ''}
          <span class="entity-card-slot entity-card-actions" data-entity-card-slot="actions"></span>
        </div>
        <div class="video-availability-row">
          ${videoAvailabilityHtml(video)}
          ${videoTypeDecoratorHtml(video)}
          ${aiDisclosureDecoratorHtml(video)}
          ${autoDubbedDecoratorHtml(video)}
          <span class="entity-card-slot entity-card-primary-metadata" data-entity-card-slot="primaryMetadata"></span>
        </div>
        ${movieMetadataHtml(video)}
        ${videoFeatureMetadataHtml(video)}
        ${contentWarningHtml(video)}
        ${badgeRowsHtml([
          { label: video.virtual_video ? 'Not in library' : '' },
          { label: wasRemovedByMeFromPlaylist(video) ? 'Removed' : '' },
          { label: matchTypeLabel(video), title: video.match_note },
        ])}
        ${detailRowHtml([
          displayVideoDuration(video) ? `<span>${escapeHtml(displayVideoDuration(video))}</span>` : '',
          displayVideoUploadDate(video) ? `<span>${escapeHtml(displayVideoUploadDate(video))}</span>` : '',
          video.video_id ? `<span>${escapeHtml(video.video_id)}</span>` : '',
          archivarixLinkHtml(video),
        ])}
        ${archivarixStatusHtml(video)}
        ${latestWatchDateHtml(video)}
        ${watchedLineHtml(video)}
        ${watchSparklineHtml(video, true)}
        ${reactionIconsHtml(video)}
        ${uploaderCategoryHtml(video.uploader_category)}
        <div class="entity-card-decoration-row">
          <div class="entity-card-slot entity-card-secondary-metadata" data-entity-card-slot="secondaryMetadata"></div>
          ${annotation.annotationMetaHtml || ''}
        </div>
        ${annotation.annotationNoteHtml || ''}
        ${playlistSourceLinksHtml(video)}
        ${video.metadata_description ? `<div class="description">${escapeHtml(video.metadata_description)}</div>` : '<div class="empty">No description captured for this video.</div>'}
      </div>
    </div>
  `;
  body.querySelector('.detail-thumb')?.append(thumbnail);
  article.append(body);
  return article;
}

function channelDetailCardFor(channel) {
  const article = document.createElement('article');
  article.className = 'card channel-detail';
  const youtubeUrl = channel.url || (channel.channel_id ? `https://www.youtube.com/channel/${encodeURIComponent(channel.channel_id)}` : '');
  const archivarixUrl = channel.channel_id ? `https://tube.archivarix.net/?q=${encodeURIComponent(channel.channel_id)}` : '';
  const status = String(channel.status || '').toLowerCase();
  const subscribedLabel = isSubscribedChannel(channel) ? 'Subscribed' : 'Non-subscribed';
  const titleText = channel.title || channel.channel_id;
  const avatar = channel.thumbnail_path
    ? `<img class="channel-avatar-large" src="/${escapeHtml(channel.thumbnail_path)}" alt="">`
    : '<div class="channel-avatar-large"></div>';
  const annotation = annotationCardOptions(channel);
  const body = document.createElement('div');
  body.className = 'body';
  body.innerHTML = `
    <div class="detail-layout">
      <div>${avatar}</div>
      <div>
        <div class="title-row">
          <div class="video-title">${escapeHtml(titleText)}</div>
          <div class="channel-external-links">
            ${youtubeUrl ? `<a class="playlist-link" href="${escapeHtml(youtubeUrl)}" target="_blank" rel="noreferrer">YouTube</a><a class="external-link" href="${escapeHtml(youtubeUrl)}" target="_blank" rel="noreferrer" title="Open on YouTube" aria-label="Open ${escapeHtml(titleText)} on YouTube">${externalLinkSvg()}</a>` : ''}
            ${archivarixUrl ? `<a class="playlist-link" href="${escapeHtml(archivarixUrl)}" target="_blank" rel="noreferrer">Archivarix</a><a class="external-link" href="${escapeHtml(archivarixUrl)}" target="_blank" rel="noreferrer" title="Open on Archivarix" aria-label="Open ${escapeHtml(titleText)} on Archivarix">${externalLinkSvg()}</a>` : ''}
          </div>
          <span class="entity-card-slot entity-card-actions" data-entity-card-slot="actions"></span>
        </div>
        <div class="details">
          <span>${subscribedLabel}</span>
          ${channelNotificationHtml(channel)}
          ${status ? `<span class="badge">${escapeHtml(status)}</span>` : ''}
          ${channel.channel_id ? `<span>${escapeHtml(channel.channel_id)}</span>` : ''}
          ${channel.archivarix_channel_id ? `<span>Archivarix ${escapeHtml(channel.archivarix_channel_id)}</span>` : ''}
          <span class="entity-card-slot entity-card-primary-metadata" data-entity-card-slot="primaryMetadata"></span>
        </div>
        ${channelDatesHtml(channel)}
        ${channel.status_reason ? `<div class="status">${escapeHtml(channel.status_reason)}</div>` : ''}
        ${channel.aliases ? `<div class="details"><span>${escapeHtml(channel.aliases)}</span></div>` : ''}
        ${featuredChannelsHtml(channel)}
        <div class="entity-card-decoration-row">
          <div class="entity-card-slot entity-card-secondary-metadata" data-entity-card-slot="secondaryMetadata"></div>
          ${annotation.annotationMetaHtml || ''}
        </div>
        ${annotation.annotationNoteHtml || ''}
        ${channel.description ? `<div class="description">${escapeHtml(channel.description)}</div>` : '<div class="empty">No channel description captured.</div>'}
      </div>
    </div>
  `;
  article.append(body);
  return article;
}

function featuredChannelsHtml(channel) {
  const featuredChannels = Array.isArray(channel?.featured_channels)
    ? channel.featured_channels
    : [];
  if (!featuredChannels.length) return '';
  const entries = featuredChannels.map((featured) => {
    const titleText = String(
      featured.title || featured.featured_channel_id || 'Unknown channel'
    );
    const internalHref = featured.cataloged && featured.preferred_reference
      ? localChannelHref(featured.preferred_reference)
      : '';
    const nameHtml = internalHref
      ? `<a class="featured-channel-name" href="${escapeHtml(internalHref)}">${escapeHtml(titleText)}</a>`
      : `<span class="featured-channel-name">${escapeHtml(titleText)}</span>`;
    const externalHtml = featured.url
      ? `<a class="external-link featured-channel-external" href="${escapeHtml(featured.url)}" target="_blank" rel="noreferrer" title="Open ${escapeHtml(titleText)} on YouTube" aria-label="Open ${escapeHtml(titleText)} on YouTube">${externalLinkSvg()}</a>`
      : '';
    return `<span class="featured-channel-entry">${nameHtml}${externalHtml}</span>`;
  });
  return `
    <div class="details channel-featured-channels">
      <span class="featured-channels-label">Featured:</span>
      <span class="featured-channels-list">${entries.join('<span class="featured-channel-separator">,</span>')}</span>
    </div>
  `;
}

function channelDatesHtml(channel) {
  const subscribedDate = channel.subscribed_at
    ? window.YTLibraryTime.formatDate(channel.subscribed_at)
    : '';
  const firstSeenDate = channel.first_seen_at
    ? window.YTLibraryTime.formatDate(channel.first_seen_at)
    : '';
  const values = [];
  if (subscribedDate) values.push(`<span>Subscribed ${escapeHtml(subscribedDate)}</span>`);
  if (firstSeenDate && firstSeenDate !== subscribedDate) {
    values.push(`<span>First seen ${escapeHtml(firstSeenDate)}</span>`);
  }
  return values.length
    ? `<div class="details channel-first-seen">${values.join('')}</div>`
    : '';
}

function playlistCreatedHtml(playlist) {
  if (!playlist.created_at) return '';
  const date = window.YTLibraryTime.formatDate(playlist.created_at);
  return date ? `<span>Created ${escapeHtml(date)}</span>` : '';
}

function channelTabsFor(
  activeTab,
  playlistedVideoCount,
  playlistCount,
  historyCount,
  pluginTabs = [],
  pluginTabCounts = new Map(),
) {
  const tabs = document.createElement('div');
  tabs.className = 'channel-tabs';
  tabs.setAttribute('role', 'tablist');
  for (const [key, label, count] of [
    ['playlisted-videos', 'Playlisted videos', playlistedVideoCount],
    ['playlists', 'Playlists', playlistCount],
    ['history', 'History', historyCount],
    ...pluginTabs.map(tab => [
      tab.key,
      tab.definition.label,
      pluginTabCounts.get(tab.key) ?? null,
    ]),
  ]) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `channel-tab${key === activeTab ? ' active' : ''}`;
    button.dataset.channelTab = key;
    button.dataset.channelTabLabel = label;
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-selected', String(key === activeTab));
    button.textContent = `${label} (${count === null ? '...' : Number(count || 0).toLocaleString()})`;
    tabs.append(button);
  }
  return tabs;
}

function entityAnnotationIdentity(kind, entity) {
  const idField = {
    video: 'video_id',
    clip: 'clip_id',
    playlist: 'playlist_id',
    channel: 'channel_id',
  }[kind];
  return idField ? String(entity?.[idField] || '') : '';
}

function annotationEditorFor(kind, entity) {
  const entityId = entityAnnotationIdentity(kind, entity);
  if (!entityId) return document.createDocumentFragment();
  const editor = document.createElement('form');
  editor.className = 'entity-annotation-editor';
  editor.dataset.annotationEditor = kind;
  editor.dataset.annotationEntityId = entityId;
  const listId = `annotation-tags-${kind}-${Math.random().toString(36).slice(2)}`;
  const note = String(entity.note || '');
  editor.dataset.annotationSavedNote = note;
  const hasNote = Boolean(note.trim());
  const tags = Array.isArray(entity.tags) ? entity.tags.join(', ') : '';
  editor.innerHTML = `
    <button class="entity-annotation-note-toggle" type="button" aria-expanded="${hasNote}"${hasNote ? ' hidden' : ''}>Add notes</button>
    <label class="entity-annotation-note-field"${hasNote ? '' : ' hidden'}>Notes
      <textarea name="note" maxlength="100000">${escapeHtml(note)}</textarea>
    </label>
    <label>Tags
      <input name="tags" type="text" value="${escapeHtml(tags)}" list="${listId}" autocomplete="off" placeholder="Comma-separated tags">
      <datalist id="${listId}"></datalist>
    </label>
    <div class="entity-annotation-actions">
      <button type="submit">Save</button>
      <span class="entity-annotation-status" role="status" aria-live="polite"></span>
    </div>
  `;
  const noteToggle = editor.querySelector('.entity-annotation-note-toggle');
  const noteField = editor.querySelector('.entity-annotation-note-field');
  const noteInput = editor.elements.namedItem('note');
  noteToggle?.addEventListener('click', () => {
    if (!(noteField instanceof HTMLElement) || !(noteInput instanceof HTMLTextAreaElement)) return;
    noteToggle.hidden = true;
    noteToggle.setAttribute('aria-expanded', 'true');
    noteField.hidden = false;
    noteInput.focus();
  });
  return editor;
}

function hasUnsavedNoteChanges() {
  return [...document.querySelectorAll('[data-annotation-editor]')].some(editor => {
    if (!(editor instanceof HTMLFormElement)) return false;
    const note = editor.elements.namedItem('note');
    return note instanceof HTMLTextAreaElement
      && note.value !== (editor.dataset.annotationSavedNote || '');
  });
}

function confirmDiscardUnsavedNotes() {
  return !hasUnsavedNoteChanges()
    || window.confirm('You have unsaved notes. Leave without saving them?');
}

function clearAnnotationReadCaches() {
  viewDataCache = new Map();
  omniSearchCache = new Map();
  videoMetaCountsCache = new Map();
  videoNoteCountsCache = new Map();
  omniMetaCountsCache = new Map();
}

let annotationTagSuggestionTimer = null;

document.addEventListener('input', event => {
  const input = event.target.closest('.entity-annotation-editor input[name="tags"]');
  if (!(input instanceof HTMLInputElement)) return;
  if (annotationTagSuggestionTimer !== null) clearTimeout(annotationTagSuggestionTimer);
  annotationTagSuggestionTimer = setTimeout(async () => {
    annotationTagSuggestionTimer = null;
    const query = input.value.split(',').at(-1)?.trim() || '';
    try {
      const response = await fetch(`/api/tags?q=${encodeURIComponent(query)}&limit=20`, {
        cache: 'no-store',
      });
      if (!response.ok || !input.isConnected) return;
      const payload = await response.json();
      const list = document.getElementById(input.getAttribute('list') || '');
      if (list instanceof HTMLDataListElement) {
        list.replaceChildren(...(payload.tags || []).map(tag => {
          const option = document.createElement('option');
          option.value = String(tag);
          return option;
        }));
      }
    } catch (_error) {
      // Suggestions are optional; saving remains available when they fail.
    }
  }, 150);
});

document.addEventListener('submit', async event => {
  const editor = event.target.closest('[data-annotation-editor]');
  if (!(editor instanceof HTMLFormElement)) return;
  event.preventDefault();
  const button = editor.querySelector('button[type="submit"]');
  const status = editor.querySelector('.entity-annotation-status');
  const note = editor.elements.namedItem('note');
  const tags = editor.elements.namedItem('tags');
  if (!(button instanceof HTMLButtonElement)
      || !(status instanceof HTMLElement)
      || !(note instanceof HTMLTextAreaElement)
      || !(tags instanceof HTMLInputElement)) return;
  button.disabled = true;
  status.classList.remove('error');
  status.textContent = 'Saving';
  try {
    const response = await fetch(
      `/api/annotations/${encodeURIComponent(editor.dataset.annotationEditor || '')}/${encodeURIComponent(editor.dataset.annotationEntityId || '')}`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          note: note.value,
          tags: tags.value.split(',').map(tag => tag.trim()).filter(Boolean),
        }),
      },
    );
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Save failed: ${response.status}`);
    const savedNote = String(payload.note ?? note.value);
    note.value = savedNote;
    editor.dataset.annotationSavedNote = savedNote;
    status.textContent = 'Saved';
    clearAnnotationReadCaches();
    await render();
  } catch (error) {
    status.classList.add('error');
    status.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
});

async function renderCurrentView() {
  const generation = ++renderGeneration;
  cancelAdjacentPagePrefetch();
  setDocumentTitle();
  if (selected !== '__search__' && !selected.startsWith('__playlist__:')) {
    searchResultsRendered = false;
    stopSearchMetaProgress();
  }
  if (!data) {
    title.textContent = '';
    meta.textContent = '';
    return;
  }
  syncSearchFiltersForSelection();
  const query = search.value.trim().toLowerCase();
  viewTop.classList.toggle('history-top', selected === '__history__');
  viewTop.classList.toggle(
    'video-collection-top',
    scopedVideoDetailSearchActive(),
  );
  stopSearchProgress();
  empty.textContent = 'No playlists match.';
  const preserveHistoryChrome = Boolean(viewContext.querySelector('.history-heatmap')) && (
    selected === '__history__'
    || (selected.startsWith('__channel__:') && channelDetailTab === 'history')
  );
  const selectedChannelReference = selected.startsWith('__channel__:')
    ? selected.slice('__channel__:'.length)
    : '';
  const renderedChannelCard = viewContext.querySelector('.channel-detail[data-channel-reference]');
  const preserveChannelChrome = Boolean(selectedChannelReference) && (
    renderedChannelCard?.dataset.channelReference === selectedChannelReference
  );
  const preserveRemotePager = !bottomPager.hidden && (
    (selected === '__search__' && omniSearchCache.size > 0)
    || (selected !== '__search__' && viewDataCache.size > 0)
  );
  if (!preserveHistoryChrome && !preserveRemotePager) {
    hidePager();
  }
  if (!preserveHistoryChrome && !preserveChannelChrome) {
    viewContext.replaceChildren();
    viewContext.hidden = true;
  }
  syncSidebarSelection();
  if (selected.startsWith('__clip__:')) {
    const clipId = selected.slice('__clip__:'.length);
    title.textContent = 'Clip';
    meta.textContent = '';
    let clip;
    let pluginPanels = [];
    try {
      const clipParams = new URLSearchParams();
      for (const plugin of browserClipFilterPlugins()) {
        clipParams.append('clip_facet_plugin', plugin.id);
      }
      const query = clipParams.toString();
      clip = await fetchViewData(
        `/api/clips/${encodeURIComponent(clipId)}${query ? `?${query}` : ''}`
      );
      pluginPanels = await renderBrowserPluginClipPanels(clip);
    } catch (error) {
      if (generation !== renderGeneration) return;
      title.textContent = 'Clip not found';
      meta.textContent = clipId;
      grid.replaceChildren();
      empty.hidden = false;
      empty.textContent = error.message;
      return;
    }
    if (generation !== renderGeneration) return;
    hydrateEntitySearchFilters('clips', clip.clip_id || clipId, generation);
    const result = {
      kind: 'clip',
      item: clip,
      pluginFacets: clip.pluginFacets || {},
    };
    setDocumentTitle(clip.title || clipId);
    hidePager();
    grid.className = 'grid search-grid layout-detailed';
    const card = searchResultCardFor(result);
    const decoration = decorateEntityCardBatch(
      [entityCardEntry('clip', clip, card)],
      'clip-detail',
      'detailed',
      generation,
    );
    grid.replaceChildren(
      card,
      annotationEditorFor('clip', clip),
      ...pluginPanels,
    );
    await decoration;
    if (generation !== renderGeneration) return;
    empty.hidden = true;
    return;
  }
  if (selected.startsWith('__video__:')) {
    const videoId = selected.slice('__video__:'.length);
    title.textContent = 'Video';
    meta.textContent = '';
    let video;
    let pluginPanels = [];
    try {
      [video, pluginPanels] = await Promise.all([
        fetchViewData(`/api/videos/${encodeURIComponent(videoId)}`),
        renderBrowserPluginVideoPanels(videoId),
      ]);
    } catch (error) {
      if (generation !== renderGeneration) return;
      title.textContent = 'Video not found';
      meta.textContent = videoId;
      grid.replaceChildren();
      empty.hidden = false;
      empty.textContent = error.message;
      return;
    }
    if (generation !== renderGeneration) return;
    hydrateEntitySearchFilters('videos', video.video_id || videoId, generation);
    setDocumentTitle(displayVideoTitle(video) || videoId);
    hidePager();
    grid.className = 'grid';
    const videoCard = videoDetailCardFor(video);
    const decoration = decorateEntityCardBatch(
      [entityCardEntry('video', video, videoCard)],
      'video-detail',
      'detailed',
      generation,
    );
    grid.replaceChildren(
      videoCard,
      annotationEditorFor('video', video),
      ...pluginPanels,
    );
    await decoration;
    if (generation !== renderGeneration) return;
    empty.hidden = true;
    return;
  }
  if (selected.startsWith('__channel__:')) {
    const channelReference = selected.slice('__channel__:'.length);
    title.textContent = 'Channel';
    let channel;
    try {
      channel = await fetchViewData(
        `/api/channels/${encodeChannelReference(channelReference)}`,
      );
    } catch (error) {
      if (generation !== renderGeneration) return;
      title.textContent = 'Channel not found';
      meta.textContent = channelReference;
      grid.replaceChildren();
      empty.hidden = false;
      empty.textContent = error.message;
      return;
    }
    if (generation !== renderGeneration) return;
    const channelId = channel.channel_id || channelReference;
    const pluginVideoTabs = browserChannelVideoTabs();
    const pluginVideoTabCounts = new Map(
      pluginVideoTabs.map(tab => [
        tab.key,
        cachedChannelTabCount(channelId, tab.key),
      ]),
    );
    const activePluginVideoTab = pluginVideoTabs.find(
      tab => tab.key === channelDetailTab,
    ) || null;
    setDocumentTitle(channel.title || channelReference);
    search.placeholder = channel.title ? `Search ${channel.title}` : 'Search channel';
    setPresetLinkLabel('videos', channel.title || channelReference);
    if (!channelDetailSearchActive) {
      hydrateChannelVideoSearchFilters(
        channelId,
        channel.title || channelReference,
        generation,
      );
    }
    let playlistedVideoCount = cachedChannelTabCount(channelId, 'playlisted-videos');
    let playlistCount = cachedChannelTabCount(channelId, 'playlists');
    let historyCount = cachedChannelTabCount(channelId, 'history');
    if (historyCount === null) historyCount = cachedChannelHistoryCount(channelId);
    const currentHeatmap = channelDetailTab === 'history'
      ? viewContext.querySelector(`.history-heatmap[data-history-channel-id="${CSS.escape(channelId)}"]`)
      : null;
    const channelCard = channelDetailCardFor(channel);
    channelCard.dataset.channelReference = channelReference;
    const channelEntry = entityCardEntry('channel', channel, channelCard);
    const channelAnnotationEditor = annotationEditorFor('channel', channel);
    viewContext.hidden = false;
    const channelChrome = [channelCard, channelAnnotationEditor];
    if (!channelDetailSearchActive) {
      channelChrome.push(channelTabsFor(
        channelDetailTab,
        playlistedVideoCount,
        playlistCount,
        historyCount,
        pluginVideoTabs,
        pluginVideoTabCounts,
      ));
      if (currentHeatmap) channelChrome.push(currentHeatmap);
    }
    viewContext.replaceChildren(...channelChrome);
    if (channelDetailSearchActive) {
      const layoutContext = 'channel-playlisted-videos';
      meta.textContent = '';
      grid.replaceChildren();
      empty.hidden = true;
      let payload;
      try {
        payload = await fetchVideoCollection(
          channelScopedVideoCollectionOptions(channelId),
        );
      } catch (error) {
        if (generation !== renderGeneration) return;
        stopSearchFilterProgress();
        hidePager();
        empty.hidden = false;
        empty.textContent = error instanceof Error ? error.message : String(error);
        return;
      }
      if (generation !== renderGeneration) return;
      stopSearchFilterProgress();
      const rows = payload.results || [];
      const searchResults = scopedVideoSearchResults(rows);
      const pluginErrors = [];
      const resultPresentations = await prepareBrowserSearchResultPresentations(
        searchResults,
        pluginErrors,
        query,
        generation,
      );
      if (generation !== renderGeneration) return;
      renderScopedVideoSearchFacets(payload);
      const pageInfo = remotePayloadPageInfo(payload, rows.length);
      meta.innerHTML = cardLayoutHtml(cardLayoutFor(layoutContext), layoutContext);
      appendPluginSearchWarnings(meta, pluginErrors);
      renderPager(pageInfo);
      applyCardLayout(layoutContext);
      const cards = searchResults.map(result => searchResultCardFor(result, {
        query,
        searchFields: [...activeSearchFields()],
        presentation: resultPresentations.get(result),
      }));
      const decoration = decorateEntityCardBatch(
        [
          channelEntry,
          ...rows.map((video, index) => entityCardEntry('video', video, cards[index])),
        ],
        'channel-search',
        cardLayoutFor(layoutContext),
        generation,
      );
      viewContext.replaceChildren(channelCard, channelAnnotationEditor);
      grid.replaceChildren(...cards);
      await decoration;
      if (generation !== renderGeneration) return;
      empty.hidden = rows.length !== 0;
      empty.textContent = 'No videos match this channel.';
      scheduleAdjacentPagePrefetch(
        pageInfo,
        page => fetchVideoCollection(channelScopedVideoCollectionOptions(channelId, page)),
      );
    } else if (channelDetailTab === 'history') {
      const layoutContext = 'channel-history';
      meta.textContent = '';
      grid.replaceChildren();
      empty.hidden = true;
      await renderHistoryResults({
        channelId,
        commitChrome: ({ activity, total }) => {
          historyCount = storeChannelTabCount(channelId, 'history', total);
          viewContext.replaceChildren(
            channelCard,
            channelAnnotationEditor,
            channelTabsFor(
              'history',
              playlistedVideoCount,
              playlistCount,
              historyCount,
              pluginVideoTabs,
              pluginVideoTabCounts,
            ),
            historyHeatmapFor(activity),
          );
          meta.innerHTML = cardLayoutHtml(cardLayoutFor(layoutContext), layoutContext);
        },
        emptyMessage: 'No history rows match this channel.',
        generation,
        layoutContext,
        leadingEntries: [channelEntry],
      });
    } else if (activePluginVideoTab) {
      const layoutContext = 'channel-playlisted-videos';
      const configuredSize = pageSizeNumber();
      const limit = Number.isFinite(configuredSize)
        ? Math.min(500, Math.max(1, configuredSize))
        : 500;
      const offset = (currentPage - 1) * limit;
      meta.textContent = '';
      grid.replaceChildren();
      empty.hidden = true;
      let payload;
      try {
        payload = await activePluginVideoTab.definition.load(
          channel,
          browserPluginHost(activePluginVideoTab.plugin.id),
          { limit, offset },
        );
      } catch (error) {
        if (generation !== renderGeneration) return;
        hidePager();
        meta.textContent = '';
        empty.hidden = false;
        empty.textContent = `${activePluginVideoTab.definition.label} unavailable: ${
          error instanceof Error ? error.message : String(error)
        }`;
        return;
      }
      if (generation !== renderGeneration) return;
      const videoIds = [...new Set(
        (payload?.videoIds || []).map(String).filter(Boolean),
      )];
      const hydratedVideos = await libraryVideos(videoIds);
      if (generation !== renderGeneration) return;
      const rows = videoIds.map(videoId => hydratedVideos.get(videoId)).filter(Boolean);
      const payloadTotal = Number(payload?.total || 0);
      const payloadLimit = Number(payload?.limit || limit);
      const total = Number.isFinite(payloadTotal) ? Math.max(0, payloadTotal) : 0;
      const remoteLimit = Number.isFinite(payloadLimit)
        ? Math.max(1, payloadLimit)
        : limit;
      currentPage = Math.floor(Number(payload?.offset || 0) / remoteLimit) + 1;
      const pageInfo = remotePageInfo(total, rows.length, remoteLimit);
      pluginVideoTabCounts.set(
        activePluginVideoTab.key,
        storeChannelTabCount(channelId, activePluginVideoTab.key, total),
      );
      meta.innerHTML = cardLayoutHtml(cardLayoutFor(layoutContext), layoutContext);
      renderPager(pageInfo);
      applyCardLayout(layoutContext);
      const cards = rows.map(video => searchVideoCardFor(video));
      const decoration = decorateEntityCardBatch(
        [
          channelEntry,
          ...rows.map((video, index) => entityCardEntry('video', video, cards[index])),
        ],
        'channel-plugin-video-tab',
        cardLayoutFor(layoutContext),
        generation,
      );
      viewContext.replaceChildren(
        channelCard,
        channelAnnotationEditor,
        channelTabsFor(
          activePluginVideoTab.key,
          playlistedVideoCount,
          playlistCount,
          historyCount,
          pluginVideoTabs,
          pluginVideoTabCounts,
        ),
      );
      grid.replaceChildren(...cards);
      await decoration;
      if (generation !== renderGeneration) return;
      empty.hidden = rows.length !== 0;
      empty.textContent = activePluginVideoTab.definition.emptyMessage
        || `No videos match ${activePluginVideoTab.definition.label.toLowerCase()}.`;
    } else if (channelDetailTab === 'playlists') {
      const layoutContext = 'channel-playlists';
      const payload = await fetchChannelPlaylists(channelReference);
      if (generation !== renderGeneration) return;
      const rows = payload.results || [];
      playlistCount = storeChannelTabCount(channelId, 'playlists', payload.total);
      const pageInfo = remotePayloadPageInfo(payload, rows.length);
      meta.innerHTML = cardLayoutHtml(cardLayoutFor(layoutContext), layoutContext);
      renderPager(pageInfo);
      applyCardLayout(layoutContext);
      const cards = rows.map(playlist => cardFor(playlist, { resultKind: 'Playlist' }));
      const decoration = decorateEntityCardBatch(
        [
          channelEntry,
          ...rows.map((playlist, index) => (
            entityCardEntry('playlist', playlist, cards[index])
          )),
        ],
        layoutContext,
        cardLayoutFor(layoutContext),
        generation,
      );
      viewContext.replaceChildren(
        channelCard,
        channelAnnotationEditor,
        channelTabsFor(
          'playlists',
          playlistedVideoCount,
          playlistCount,
          historyCount,
          pluginVideoTabs,
          pluginVideoTabCounts,
        ),
      );
      grid.replaceChildren(...cards);
      await decoration;
      if (generation !== renderGeneration) return;
      empty.hidden = rows.length !== 0;
      empty.textContent = 'No playlists match this channel.';
      scheduleAdjacentPagePrefetch(
        pageInfo,
        page => fetchChannelPlaylists(channelReference, page),
      );
    } else {
      const layoutContext = 'channel-playlisted-videos';
      const payload = await fetchVideoCollection({ channelId, sort: 'title' });
      if (generation !== renderGeneration) return;
      const rows = payload.results || [];
      playlistedVideoCount = storeChannelTabCount(
        channelId,
        'playlisted-videos',
        payload.total,
      );
      const pageInfo = remotePageInfo(Number(payload.total || 0), rows.length, Number(payload.limit || 100));
      meta.innerHTML = cardLayoutHtml(cardLayoutFor(layoutContext), layoutContext);
      renderPager(pageInfo);
      applyCardLayout(layoutContext);
      const cards = rows.map(playlistVideoCardFor);
      const decoration = decorateEntityCardBatch(
        [
          channelEntry,
          ...rows.map((video, index) => entityCardEntry('video', video, cards[index])),
        ],
        layoutContext,
        cardLayoutFor(layoutContext),
        generation,
      );
      viewContext.replaceChildren(
        channelCard,
        channelAnnotationEditor,
        channelTabsFor(
          'playlisted-videos',
          playlistedVideoCount,
          playlistCount,
          historyCount,
          pluginVideoTabs,
          pluginVideoTabCounts,
        ),
      );
      grid.replaceChildren(...cards);
      await decoration;
      if (generation !== renderGeneration) return;
      empty.hidden = rows.length !== 0;
      empty.textContent = 'No playlist videos match this channel.';
      scheduleAdjacentPagePrefetch(pageInfo, page => (
        fetchVideoCollection({ channelId, sort: 'title', page })
      ));
    }
    if (!channelDetailSearchActive) {
      hydrateChannelTabCounts({
        channel,
        channelReference,
        pluginTabs: pluginVideoTabs,
        generation,
      });
    }
    return;
  }
  if (selected === '__search__') {
    const preserveSearchContent = (
      renderedOmniSearchQuery === query
      && searchResultsRendered
    );
    if (preserveSearchContent) {
      showSearchProgress({ preserveContent: true });
    } else {
      title.textContent = '';
      meta.textContent = '';
      renderSearchMetaFilters();
      showSearchProgress();
      await new Promise(resolve => requestAnimationFrame(resolve));
      if (generation !== renderGeneration) return;
    }
    let payload;
    try {
      payload = await fetchOmniSearch(query);
    } catch (error) {
      if (generation !== renderGeneration) return;
      stopSearchFilterProgress();
      stopSearchProgress();
      searchResultsRendered = false;
      title.textContent = 'Search unavailable';
      empty.hidden = false;
      empty.textContent = error.message;
      return;
    }
    if (generation !== renderGeneration) return;
    const rows = payload.results || [];
    const pluginErrors = [...(payload.pluginErrors || [])];
    const resultPresentations = await prepareBrowserSearchResultPresentations(
      rows,
      pluginErrors,
      query,
      generation,
    );
    if (generation !== renderGeneration) return;
    stopSearchFilterProgress();
    stopSearchProgress();
    const total = Number(payload.total || 0);
    const remoteLimit = Number(payload.limit || pageSizeNumber() || 100);
    currentPage = Math.floor(Number(payload.offset || 0) / remoteLimit) + 1;
    renderedOmniSearchQuery = query;
    searchResultsRendered = true;
    title.textContent = '';
    const totalLabel = `${total.toLocaleString()}${payload.totalIsExact === false ? '+' : ''} results`;
    meta.innerHTML = rightPanelListMetaHtml(totalLabel, {
      showLayout: true,
      sortHtml: searchResultsSortHtml(),
    });
    appendPluginSearchWarnings(meta, pluginErrors);
    renderSearchMetaFilters(payload);
    const pageInfo = remotePageInfo(total, rows.length, remoteLimit);
    renderPager(pageInfo);
    applyCardLayout('search');
    const cards = rows.map(result => searchResultCardFor(result, {
      query,
      searchFields: payload.searchFields,
      presentation: resultPresentations.get(result),
    }));
    const entries = rows.flatMap((result, index) => (
      result.kind === 'plugin'
        ? []
        : [entityCardEntry(result.kind, result.item, cards[index])]
    ));
    const decoration = decorateEntityCardBatch(
      entries,
      'search',
      cardLayoutFor('search'),
      generation,
    );
    grid.replaceChildren(...cards);
    await decoration;
    if (generation !== renderGeneration) return;
    empty.hidden = rows.length !== 0;
    empty.textContent = 'No results match.';
    scheduleAdjacentPagePrefetch(pageInfo, page => fetchOmniSearch(query, page));
    return;
  }
  if (selected.startsWith('__playlist__:')) {
    const playlistId = selected.slice('__playlist__:'.length);
    resetPlaylistVisibilityFor(playlistId);
    Object.assign(playlistVisibility, searchMetaVisibility.videos);
    Object.assign(playlistCompletionVisibility, searchMetaVisibility.completion);
    let playlist;
    let payload;
    try {
      [playlist, payload] = await Promise.all([
        fetchViewData(`/api/playlists/${encodeURIComponent(playlistId)}`),
        fetchVideoCollection({
          playlistId,
          sort: playlistViewSort,
          query: search.value.trim(),
          visibility: playlistVisibility,
          videoTypes: searchMetaVisibility.videoType,
          broadcastStatuses: searchMetaVisibility.broadcastStatus,
          aiDisclosures: searchMetaVisibility.aiDisclosure,
          completion: searchMetaVisibility.completion,
          reactions: searchMetaVisibility.reactions,
          uploaderCategories: searchMetaVisibility.uploaderCategory,
          notes: searchMetaVisibility.videoNotes,
          useSearchFacets: true,
          partialMinimumPercent: partialCompletionMinimumPercent,
          duplicatesOnly: playlistDuplicatesOnly,
        }),
      ]);
      if (generation !== renderGeneration) return;
      if (playlistDuplicatesOnly && Number(payload.duplicateCount || 0) === 0) {
        playlistDuplicatesOnly = false;
        updateCurrentUrl(true);
        payload = await fetchVideoCollection({
          playlistId,
          sort: playlistViewSort,
          query: search.value.trim(),
          visibility: playlistVisibility,
          videoTypes: searchMetaVisibility.videoType,
          broadcastStatuses: searchMetaVisibility.broadcastStatus,
          aiDisclosures: searchMetaVisibility.aiDisclosure,
          completion: searchMetaVisibility.completion,
          reactions: searchMetaVisibility.reactions,
          uploaderCategories: searchMetaVisibility.uploaderCategory,
          notes: searchMetaVisibility.videoNotes,
          useSearchFacets: true,
          partialMinimumPercent: partialCompletionMinimumPercent,
          duplicatesOnly: false,
        });
      }
    } catch (error) {
      if (generation !== renderGeneration) return;
      stopSearchFilterProgress();
      title.textContent = 'Playlist not found';
      meta.textContent = playlistId;
      grid.replaceChildren();
      empty.hidden = false;
      empty.textContent = error.message;
      return;
    }
    if (generation !== renderGeneration) return;
    stopSearchFilterProgress();
    setDocumentTitle(playlist.title || playlistId);
    viewContext.hidden = false;
    viewContext.replaceChildren(annotationEditorFor('playlist', playlist));
    const rows = payload.results || [];
    const searchResults = scopedVideoSearchResults(rows);
    const pluginErrors = [];
    const resultPresentations = await prepareBrowserSearchResultPresentations(
      searchResults,
      pluginErrors,
      query,
      generation,
    );
    if (generation !== renderGeneration) return;
    renderScopedVideoSearchFacets(payload);
    const playlistCount = playlistVideoCountLabel(playlist);
    const playlistHeadingMeta = [
      playlistCount ? `<span>${escapeHtml(playlistCount)}</span>` : '',
      playlistVisibilityLabelHtml(playlist),
      playlistStatusLabelHtml(playlist),
      playlistYoutubeLastUpdatedLabel(playlist)
        ? `<span>YT Last updated ${escapeHtml(playlistYoutubeLastUpdatedLabel(playlist))}</span>`
        : '',
    ].filter(Boolean).join('');
    title.innerHTML = `
      <span class="playlist-page-heading">
        <span class="title-with-action">
          <span>${escapeHtml(playlist.title)}</span>
          ${playlist.url ? `<a class="external-link" href="${escapeHtml(playlist.url)}" target="_blank" rel="noreferrer" title="Open on YouTube" aria-label="Open ${escapeHtml(playlist.title)} on YouTube">${externalLinkSvg()}</a>` : ''}
        </span>
        ${playlistHeadingMeta ? `<span class="details playlist-page-meta">${playlistHeadingMeta}</span>` : ''}
      </span>
    `;
    meta.innerHTML = `
      ${playlistDuplicateFilterHtml(payload.duplicateCount)}
      <span class="result-view-controls video-collection-view-controls">
        ${cardLayoutHtml(cardLayoutFor('playlist'), 'playlist')}
        ${videoSortHtml(playlistViewSort, 'playlist')}
      </span>
    `;
    appendPluginSearchWarnings(meta, pluginErrors);
    syncMetaFilterGroup('playlist-duplicates');
    const pageInfo = remotePayloadPageInfo(payload, rows.length);
    renderPager(pageInfo);
    applyCardLayout('playlist');
    const cards = searchResults.map(result => SearchResultPresentations.apply(
      playlistVideoCardFor(result.item, { showPosition: true }),
      resultPresentations.get(result),
    ));
    const decoration = decorateEntityCardBatch(
      rows.map((video, index) => entityCardEntry('video', video, cards[index])),
      'playlist',
      cardLayoutFor('playlist'),
      generation,
    );
    grid.replaceChildren(...cards);
    await decoration;
    if (generation !== renderGeneration) return;
    empty.hidden = rows.length !== 0;
    empty.textContent = playlist.scanned_at ? 'No videos match.' : 'This playlist has not been scanned yet.';
    scheduleAdjacentPagePrefetch(pageInfo, page => fetchVideoCollection({
      playlistId,
      sort: playlistViewSort,
      query: search.value.trim(),
      visibility: playlistVisibility,
      videoTypes: searchMetaVisibility.videoType,
      broadcastStatuses: searchMetaVisibility.broadcastStatus,
      aiDisclosures: searchMetaVisibility.aiDisclosure,
      completion: searchMetaVisibility.completion,
      reactions: searchMetaVisibility.reactions,
      uploaderCategories: searchMetaVisibility.uploaderCategory,
      notes: searchMetaVisibility.videoNotes,
      useSearchFacets: true,
      partialMinimumPercent: partialCompletionMinimumPercent,
      duplicatesOnly: playlistDuplicatesOnly,
      page,
    }));
    return;
  }
  if (selected === '__history__') {
    await renderHistoryView(generation);
    return;
  }
}

async function render() {
  const loadingToken = beginLoadingStatus({ reset: true });
  try {
    return await renderCurrentView();
  } finally {
    finishLoadingStatus(loadingToken);
  }
}

function cardFor(playlist, options = {}) {
  const localHref = localPlaylistHref(playlist.playlist_id);
  const playlistCount = playlistVideoCountLabel(playlist);
  const owner = playlistOwnerHtml(playlist);
  return CollectionCard.create({
    ...annotationCardOptions(playlist),
    resultKind: options.resultKind,
    thumbnailPath: playlist.thumbnail_path,
    thumbnailHref: localHref,
    placeholderThumbnail: true,
    headerHtml: owner ? `<div class="details video-card-channel">${owner}</div>` : '',
    titleHtml: `<a class="playlist-title" href="${localHref}">${escapeHtml(playlist.title)}</a>`,
    actionsHtml: `<a class="external-link" href="${escapeHtml(playlist.url)}" target="_blank" rel="noreferrer" title="Open on YouTube" aria-label="Open ${escapeHtml(playlist.title)} on YouTube">${externalLinkSvg()}</a>`,
    bodyHtml: `
    <div class="details">
      ${playlistVisibilityLabelHtml(playlist)}
      ${playlistCount ? `<span>${escapeHtml(playlistCount)}</span>` : ''}
      ${playlist.unavailable_count ? `<span class="badge">${playlist.unavailable_count} unavailable</span>` : ''}
      ${playlistStatusLabelHtml(playlist)}
    </div>
    <div class="details">
      ${playlist.playlist_id ? `<span class="playlist-id entity-card-id">${escapeHtml(playlist.playlist_id)}</span>` : ''}
      ${playlistCreatedHtml(playlist)}
    </div>
    `,
    lastUpdatedHtml: `${playlistLastUpdatedHtml(playlist)}${playlistYoutubeLastUpdatedHtml(playlist)}`,
  });
}

function playlistStatusLabelHtml(playlist) {
  if (playlist.fetch_status === 'unavailable') return '<span class="status">Unavailable</span>';
  if (playlist.fetch_status === 'error') return '<span class="status">Fetch failed</span>';
  return '';
}

function playlistVideoCountLabel(playlist) {
  const reported = Number(playlist.video_count || 0);
  const scanned = Number(playlist.scanned_video_count || 0);
  const count = reported || scanned;
  if (!count) return '';
  const incomplete = Boolean(
    playlist.scan_status !== 'unavailable'
    && (
      playlist.scan_status === 'error'
      || (reported && scanned && reported !== scanned)
    )
  );
  return `${count} ${count === 1 ? 'video' : 'videos'}${incomplete ? ' (incomplete)' : ''}`;
}

function playlistOwnerHtml(playlist) {
  const owner = playlistOwnerForDisplay(playlist);
  const name = cleanPlaylistOwnerName(owner.title || '');
  if (!name) return '';
  const collaborators = Array.isArray(playlist.collaborators)
    ? playlist.collaborators.map(collaborator => ({
      title: collaborator.title || '',
      reference: collaborator.channel_reference || collaborator.channel_id || '',
      url: collaborator.channel_url || '',
      thumbnail_path: collaborator.thumbnail_path || '',
    })).filter(collaborator => collaborator.title)
    : [];
  const people = [owner, ...collaborators];
  const avatars = people.slice(0, 3).map(person => playlistPersonAvatarHtml(person)).join('');
  let names = playlistPersonNameHtml(owner);
  if (collaborators.length === 1) {
    names += ` and ${playlistPersonNameHtml(collaborators[0])}`;
  } else if (collaborators.length > 1) {
    names += ` and ${collaborators.length} others`;
  }
  return `${avatars ? `<span class="playlist-creator-avatars">${avatars}</span>` : ''}<span>${names}</span>`;
}

function clipPeopleHtml(clip) {
  return playlistOwnerHtml({
    owner_channel_title: clip.resolved_owner_title || clip.owner_title || '',
    owner_channel_id: clip.owner_channel_id || '',
    owner_channel_reference: clip.owner_channel_reference || clip.owner_channel_id || '',
    owner_channel_url: clip.owner_channel_url || '',
    owner_channel_thumbnail_path: clip.resolved_owner_thumbnail_path || clip.owner_thumbnail_path || '',
    collaborators: clip.source_channel_title ? [{
      title: clip.source_channel_title,
      channel_id: clip.source_channel_id || '',
      channel_reference: clip.source_channel_reference || clip.source_channel_id || '',
      channel_url: clip.source_channel_url || '',
      thumbnail_path: clip.source_channel_thumbnail_path || '',
    }] : [],
  });
}

function playlistPersonHref(person) {
  return person.reference ? localChannelHref(person.reference) : (person.url || '');
}

function playlistPersonLinkAttributes(href) {
  return linkTargetAttributes(href);
}

function playlistPersonAvatarHtml(person) {
  const href = playlistPersonHref(person);
  const name = cleanPlaylistOwnerName(person.title || '');
  const image = person.thumbnail_path
    ? `<img class="channel-avatar" src="/${escapeHtml(person.thumbnail_path)}" alt="${escapeHtml(name)}">`
    : '<span class="channel-avatar playlist-creator-avatar-placeholder"></span>';
  return href
    ? `<a class="playlist-creator-avatar" href="${escapeHtml(href)}"${playlistPersonLinkAttributes(href)}>${image}</a>`
    : `<span class="playlist-creator-avatar">${image}</span>`;
}

function playlistPersonNameHtml(person) {
  const href = playlistPersonHref(person);
  const name = cleanPlaylistOwnerName(person.title || '');
  return href
    ? `<a class="creator-link" href="${escapeHtml(href)}"${playlistPersonLinkAttributes(href)}>${escapeHtml(name)}</a>`
    : escapeHtml(name);
}

function playlistOwnerForDisplay(playlist) {
  const title = playlist.owner_channel_title || '';
  if (title) {
    return {
      title,
      channel_id: playlist.owner_channel_id || '',
      reference: playlist.owner_channel_reference || playlist.owner_channel_id || '',
      url: playlist.owner_channel_url || '',
      thumbnail_path: playlist.owner_channel_thumbnail_path || '',
    };
  }
  return {};
}

function cleanPlaylistOwnerName(value) {
  const text = String(value || '').trim();
  return text.toLowerCase().startsWith('by ') ? text.slice(3).trim() : text;
}

function videoCardFor(options) {
  return VideoCard.create({ ...options, externalIconHtml: externalLinkSvg() });
}

function playlistVideoCardFor(video, options = {}) {
  const watchUrl = youtubeWatchUrl(video);
  const channelName = displayVideoChannel(video);
  const channelUrl = displayVideoChannelLocalUrl(video);
  const duration = displayVideoDuration(video);
  return videoCardFor({
    ...annotationCardOptions(video),
    thumbnailPath: video.metadata_thumbnail_path,
    progressVideo: video,
    resultKind: options.resultKind,
    position: options.showPosition ? video.position : '',
    title: displayVideoTitle(video),
    titleHtml: options.titleHtml,
    localUrl: video.video_id ? localVideoHref(video.video_id) : '',
    externalUrl: options.externalUrl === undefined ? watchUrl : options.externalUrl,
    badges: [
      { label: wasRemovedByMeFromPlaylist(video) ? 'Removed' : '' },
      { label: matchTypeLabel(video), title: video.match_note },
      ...(options.badges || []),
    ],
    details: [
      ...(options.details || []),
    ],
    durationDetails: [
      duration ? `<span>${escapeHtml(duration)}</span>` : '',
    ],
    identifiers: [
      video.video_id ? `<span class="video-id entity-card-id">${escapeHtml(video.video_id)}</span>` : '',
      archivarixLinkHtml(video),
    ],
    recoveryHtml: archivarixStatusHtml(video),
    channelHtml: creatorHtml(video.metadata_channel_thumbnail_path, channelName, channelUrl),
    sources: options.sources || [],
    playlistSourcesHtml: options.playlistSourcesHtml === undefined ? playlistSourceLinksHtml(video) : options.playlistSourcesHtml,
    watchDateHtml: options.watchDateHtml || '',
    latestWatchDateHtml: options.latestWatchDateHtml || '',
    availabilityHtml: videoAvailabilityHtml(video),
    typeDecoratorHtml: videoTypeDecoratorHtml(video),
    aiDisclosureDecoratorHtml: aiDisclosureDecoratorHtml(video),
    autoDubbedDecoratorHtml: autoDubbedDecoratorHtml(video),
    movieMetadataHtml: movieMetadataHtml(video),
    featureMetadataHtml: videoFeatureMetadataHtml(video),
    contentWarningHtml: contentWarningHtml(video),
    compactAvailabilityHtml: duration
      ? `<span class="compact-video-duration">${escapeHtml(duration)}</span>`
      : '',
    watchedHtml: watchedLineHtml(video),
    sparklineHtml: watchSparklineHtml(video),
    reactionHtml: reactionIconsHtml(video),
    uploaderCategory: video.uploader_category,
    description: options.description === undefined ? video.metadata_description : options.description,
    descriptionHtml: options.descriptionHtml,
  });
}

function searchVideoCardFor(video, options = {}) {
  return playlistVideoCardFor(video, {
    ...options,
    resultKind: options.resultKind || 'Video',
    latestWatchDateHtml: options.latestWatchDateHtml === undefined
      ? latestWatchDateHtml(video)
      : options.latestWatchDateHtml,
    badges: [
      ...(Array.isArray(video.plugin_badges) ? video.plugin_badges : []),
      ...(options.badges || []),
    ],
  });
}

function historyWatchedAtLabel(video) {
  if (video.time_quality === 'exact' && video.watched_at) {
    return window.YTLibraryTime.format(video.watched_at);
  }
  return video.watch_date || '';
}

function historyDayLabel(video) {
  const value = video.watched_at || video.watch_date || '';
  const dateLabel = window.YTLibraryTime.formatDate(value);
  if (!dateLabel) return '';
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  const parsed = dateOnly
    ? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
    : new Date(value);
  if (Number.isNaN(parsed.getTime())) return dateLabel;
  const options = { weekday: 'short' };
  if (!dateOnly) {
    options.timeZone = window.YTLibraryTime.timeZone || window.YTLibraryTime.detected();
  }
  const weekday = new Intl.DateTimeFormat(undefined, options).format(parsed);
  return `${weekday}, ${dateLabel}`;
}

function historyDayDividerFor(label, date) {
  const divider = document.createElement('div');
  divider.className = 'history-day-divider';
  divider.dataset.historyDate = date;
  divider.setAttribute('role', 'separator');
  divider.setAttribute('aria-label', label);
  const text = document.createElement('span');
  text.textContent = label;
  divider.append(text);
  return divider;
}

function historyRowsWithDayDividers(rows, options = {}) {
  const elements = [];
  const entries = [];
  let previousLabel = '';
  for (const row of rows) {
    const label = historyDayLabel(row);
    if (label && label !== previousLabel) {
      elements.push(historyDayDividerFor(label, historyRowDateKey(row)));
    }
    const card = historyRowCardFor(row, options);
    elements.push(card);
    entries.push(entityCardEntry('video', row, card));
    previousLabel = label;
  }
  return { elements, entries };
}

function clipDurationLabel(clip) {
  const milliseconds = Number(clip.end_ms || 0) - Number(clip.start_ms || 0);
  if (!Number.isFinite(milliseconds) || milliseconds <= 0) return '';
  const totalSeconds = Math.max(1, Math.round(milliseconds / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
    : `${minutes}:${String(seconds).padStart(2, '0')}`;
}

function clipViewsLabel(clip) {
  const raw = String(clip.view_count_text || '').trim();
  if (raw) return raw;
  const count = Number(clip.view_count);
  if (!Number.isFinite(count)) return '';
  return `${count.toLocaleString()} ${count === 1 ? 'view' : 'views'}`;
}

function clipCardFor(clip, options = {}) {
  const localHref = localClipHref(clip.clip_id || '');
  const clipUrl = clip.url || `https://www.youtube.com/clip/${encodeURIComponent(clip.clip_id || '')}`;
  const sourceHref = clip.source_video_id ? localVideoHref(clip.source_video_id) : '';
  const sourceTitle = clip.source_video_title || clip.source_video_id || '';
  const titleText = clip.title || clip.clip_id || '';
  const people = clipPeopleHtml(clip);
  const clippedAt = clip.clipped_at
    ? window.YTLibraryTime.format(clip.clipped_at)
    : String(clip.clipped_at_text || '');
  const availability = String(clip.availability || 'unknown').toLowerCase();
  const availabilityHtml = availability === 'active'
    ? ''
    : `<span class="${availability === 'unavailable' ? 'status' : ''}">${escapeHtml(availability === 'unavailable' ? 'Unavailable' : 'Unknown')}</span>`;
  return CollectionCard.create({
    ...annotationCardOptions(clip),
    resultKind: options.resultKind || 'Clip',
    className: 'clip-card',
    thumbnailPath: clip.source_thumbnail_path || '',
    thumbnailHref: localHref,
    placeholderThumbnail: true,
    headerHtml: people ? `<div class="details video-card-channel">${people}</div>` : '',
    titleHtml: `<a class="playlist-title" href="${localHref}">${options.titleHtml === undefined ? escapeHtml(titleText) : options.titleHtml}</a>`,
    actionsHtml: `<a class="external-link" href="${escapeHtml(clipUrl)}" target="_blank" rel="noreferrer" title="Open clip on YouTube" aria-label="Open ${escapeHtml(titleText)} on YouTube">${externalLinkSvg()}</a>`,
    bodyHtml: `
      ${badgeRowsHtml(Array.isArray(clip.plugin_badges) ? clip.plugin_badges : [])}
      <div class="details">
        ${clipDurationLabel(clip) ? `<span>${escapeHtml(clipDurationLabel(clip))}</span>` : ''}
        ${clip.clip_id ? `<span class="clip-id entity-card-id">${escapeHtml(clip.clip_id)}</span>` : ''}
        ${availabilityHtml}
      </div>
      <div class="details">
        ${clipViewsLabel(clip) ? `<span>${escapeHtml(clipViewsLabel(clip))}</span>` : ''}
        ${clippedAt ? `<span>${escapeHtml(clippedAt)}</span>` : ''}
      </div>
      ${reactionIconsHtml(clip)}
      ${uploaderCategoryHtml(clip.uploader_category)}
    `,
    tailHtml: sourceHref && sourceTitle
      ? `<div class="details"><a class="playlist-link" href="${sourceHref}">Source video: ${escapeHtml(sourceTitle)}</a></div>`
      : '',
  });
}

function historyRowCardFor(video, { layout = 'detailed' } = {}) {
  const watched = historyWatchedAtLabel(video);
  const article = playlistVideoCardFor(video, {
    playlistSourcesHtml: playlistSourceLinksHtml(video),
    watchDateHtml: watched
      ? `<div class="details watch-date-line"><span>Watched ${escapeHtml(watched)}${compactWatchCountHtml(video)}</span></div>`
      : '',
  });
  article.classList.add('history-card');
  article.classList.toggle('history-row', layout !== 'grid');
  const watchDate = historyRowDateKey(video);
  if (watchDate) article.dataset.watchDate = watchDate;
  if (!article.querySelector('.thumb-wrap')) {
    const placeholder = document.createElement('div');
    placeholder.className = 'thumb-wrap';
    article.insertBefore(placeholder, article.firstChild);
  }
  return article;
}

function searchResultCardFor(result, options = {}) {
  if (result.kind === 'plugin') {
    const plugin = browserSearchPlugin(result.pluginId);
    const card = plugin?.search?.renderResult?.(
      result.item,
      browserPluginHost(result.pluginId),
    );
    if (card instanceof HTMLElement) return card;
  }
  let card;
  if (result.kind === 'playlist') {
    card = cardFor(result.item, { resultKind: 'Playlist' });
  }
  if (!card && result.kind === 'clip') {
    const clip = result.item;
    const query = String(options.query || '');
    const searchFields = new Set(options.searchFields || []);
    card = clipCardFor(clip, {
      titleHtml: query && searchFields.has('titles')
        ? searchHighlight.textHtml(clip.title || clip.clip_id || '', query)
        : undefined,
    });
  }
  if (!card && result.kind === 'channel') {
    card = channelCardFor(result.item, { resultKind: 'Channel' });
  }
  if (!card) {
    const video = result.item;
    const query = String(options.query || '');
    const searchFields = new Set(options.searchFields || []);
    const titleText = displayVideoTitle(video);
    const descriptionText = String(video.metadata_description || '');
    card = searchVideoCardFor(video, {
      titleHtml: query && searchFields.has('titles')
        ? searchHighlight.textHtml(titleText, query)
        : undefined,
      descriptionHtml: query && descriptionText && searchFields.has('descriptions')
        ? searchHighlight.excerptHtml(descriptionText, query)
        : undefined,
    });
  }
  return SearchResultPresentations.apply(card, options.presentation);
}

function channelCardFor(channel, options = {}) {
  const youtubeUrl = channel.url || (channel.channel_id ? `https://www.youtube.com/channel/${encodeURIComponent(channel.channel_id)}` : '');
  const archivarixUrl = channel.channel_id ? `https://tube.archivarix.net/?q=${encodeURIComponent(channel.channel_id)}` : '';
  const status = String(channel.status || '').toLowerCase();
  const subscribedLabel = isSubscribedChannel(channel) ? 'Subscribed' : 'Non-subscribed';
  const titleText = channel.title || channel.channel_id;
  const channelReference = channel.preferred_reference || channel.channel_id || '';
  const titleHtml = creatorHtml(
    channel.thumbnail_path || '',
    titleText,
    channelReference ? localChannelHref(channelReference) : ''
  );
  return CollectionCard.create({
    ...annotationCardOptions(channel),
    className: 'channel-card',
    resultKind: options.resultKind,
    thumbnailPath: channel.thumbnail_path,
    titleHtml: `<div class="playlist-title">${titleHtml || escapeHtml(titleText)}</div>`,
    actionsHtml: youtubeUrl ? `<a class="external-link" href="${escapeHtml(youtubeUrl)}" target="_blank" rel="noreferrer" title="Open on YouTube" aria-label="Open ${escapeHtml(titleText)} on YouTube">${externalLinkSvg()}</a>` : '',
    bodyHtml: `
    <div class="details">
      <span>${subscribedLabel}</span>
      ${channelNotificationHtml(channel)}
      ${status ? `<span class="badge">${escapeHtml(status)}</span>` : ''}
      ${channel.channel_id ? `<span class="channel-id entity-card-id">${escapeHtml(channel.channel_id)}</span>` : ''}
      ${channel.archivarix_channel_id ? `<span class="channel-archivarix-id">Archivarix ${escapeHtml(channel.archivarix_channel_id)}</span>` : ''}
    </div>
    ${channelDatesHtml(channel)}
    ${channel.status_reason ? `<div class="status">${escapeHtml(channel.status_reason)}</div>` : ''}
    ${channel.aliases ? `<div class="details"><span>${escapeHtml(channel.aliases)}</span></div>` : ''}
    ${featuredChannelsHtml(channel)}
    `,
    tailHtml: `
    ${channel.description ? `<div class="description">${escapeHtml(channel.description)}</div>` : ''}
    <div class="details channel-card-links">
      ${youtubeUrl ? `<a class="playlist-link channel-youtube-link" href="${escapeHtml(youtubeUrl)}" target="_blank" rel="noreferrer">YouTube</a>` : ''}
      ${archivarixUrl ? `<a class="playlist-link channel-archivarix-link" href="${escapeHtml(archivarixUrl)}" target="_blank" rel="noreferrer">Archivarix</a>` : ''}
    </div>
    `,
  });
}

function externalLinkSvg() {
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 17 17 7"></path><path d="M8 7h9v9"></path><path d="M7 7H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-2"></path></svg>';
}

search.addEventListener('input', () => {
  if (searchInputTimer !== null) {
    clearTimeout(searchInputTimer);
    searchInputTimer = null;
  }
  currentPage = 1;
  if (selected === '__history__') {
    historyNavigationDate = '';
    pendingHistoryDate = '';
    historyActivityYearOffset = 0;
    updateCurrentUrl(true);
    searchInputTimer = setTimeout(() => {
      searchInputTimer = null;
      void render();
    }, 250);
    return;
  }
  if (selected.startsWith('__playlist__:')) {
    updateCurrentUrl(true);
    if (searchInputTimer !== null) clearTimeout(searchInputTimer);
    searchInputTimer = setTimeout(() => {
      searchInputTimer = null;
      void render();
    }, 250);
    return;
  }
  if (selected.startsWith('__channel__:')) {
    const nextActive = Boolean(search.value.trim());
    if (channelDetailSearchActive !== nextActive) {
      channelDetailSearchActive = nextActive;
      activeSearchScope = 'videos';
      if (nextActive) applySearchPresetState(activeSearchScope);
      renderGroups();
    }
    updateCurrentUrl(true);
    searchInputTimer = setTimeout(() => {
      searchInputTimer = null;
      void render();
    }, 250);
    return;
  }
  if (!searchSortExplicit) searchResultsSort = preferredSearchResultsSort();
  const wasSearchRoute = selected === '__search__';
  if (!wasSearchRoute) {
    activateSearchFromSelection({ resetMetaVisibility: true });
    if (!searchSortExplicit) searchResultsSort = preferredSearchResultsSort();
  }
  renderGroups();
  const changed = updateSearchUrl(wasSearchRoute);
  if (changed && !wasSearchRoute) return;
  searchInputTimer = setTimeout(() => {
    searchInputTimer = null;
    render();
  }, 250);
});
historyNav?.addEventListener('click', event => {
  handleSidebarLinkClick(event, () => setSelected('__history__'));
});
searchNav?.addEventListener('click', event => {
  handleSidebarLinkClick(event, activateSearchNavigation);
});
viewContext.addEventListener('change', event => {
  const syncToggle = event.target.closest('[data-history-sync]');
  if (syncToggle instanceof HTMLInputElement) {
    void setHistoryActivitySync(syncToggle.checked);
  }
});
viewContext.addEventListener('click', event => {
  const currentHistory = event.target.closest('[data-history-current]');
  if (currentHistory instanceof HTMLButtonElement && !currentHistory.disabled) {
    void jumpToCurrentHistoryActivity();
    return;
  }
  const yearShift = event.target.closest('[data-history-year-shift]');
  if (yearShift instanceof HTMLButtonElement && !yearShift.disabled) {
    const delta = Number(yearShift.dataset.historyYearShift || 0);
    if (delta) void shiftHistoryActivityYear(delta);
    return;
  }
  const historyDay = event.target.closest('[data-history-date]');
  if (historyDay instanceof HTMLButtonElement) {
    const date = historyDay.dataset.historyDate || '';
    const offset = Number(historyDay.dataset.historyOffset || 0);
    if (date) void jumpToHistoryDate(date, offset);
    return;
  }
  const target = event.target.closest('[data-channel-tab]');
  if (!(target instanceof HTMLButtonElement)) return;
  channelDetailTab = target.dataset.channelTab || 'playlisted-videos';
  historyNavigationDate = '';
  pendingHistoryDate = '';
  currentPage = 1;
  updateCurrentUrl(true);
  scrollResultsToTop();
  render();
});
function handleMetaChange(event) {
  const target = event.target;
  if (target instanceof HTMLSelectElement && target.dataset.videoSort !== undefined) {
    if (target.dataset.videoSort === 'playlist') {
      const nextSort = target.value || 'playlist_order';
      const previousSort = playlistViewSort;
      const previousPreference = sortPreferences.playlist;
      playlistViewSort = nextSort;
      saveSortPreference('playlist', nextSort, previousPreference, () => {
        if (!selected.startsWith('__playlist__:') || playlistViewSort !== nextSort) return;
        playlistViewSort = previousSort;
        currentPage = 1;
        void render();
      });
    }
    currentPage = 1;
    scrollResultsToTop();
    render();
    return;
  }
  if (target instanceof HTMLSelectElement && target.dataset.searchSort !== undefined) {
    const context = searchSortPreferenceContext();
    const nextSort = target.value || 'relevance';
    const previousSort = searchResultsSort;
    const previousExplicit = searchSortExplicit;
    const previousPreference = sortPreferences[context];
    searchResultsSort = nextSort;
    searchSortExplicit = true;
    saveSortPreference(context, nextSort, previousPreference, () => {
      if (searchSortPreferenceContext() !== context || searchResultsSort !== nextSort) return;
      searchResultsSort = previousSort;
      searchSortExplicit = previousExplicit;
      currentPage = 1;
      syncSearchUrlAndRender();
    });
    currentPage = 1;
    scrollResultsToTop();
    syncSearchUrlAndRender();
    return;
  }
  if (!(target instanceof HTMLInputElement)) return;
  const searchFilterInteraction = (
    target.dataset.searchKindFilter
    || target.dataset.searchPluginFacetFilter
    || target.dataset.searchMetaFilter
    || target.dataset.searchCompletionMinimum !== undefined
    || String(target.dataset.metaAllFilter || '').startsWith('search-')
  );
  const activatedFromSelection = searchFilterInteraction
    ? activateSearchFromSelection()
    : false;
  if (target.dataset.searchCompletionMinimum !== undefined) {
    const previousMinimum = setPartialCompletionMinimum(target.value);
    target.value = String(partialCompletionMinimumPercent);
    if (
      previousMinimum === partialCompletionMinimumPercent
      && searchMetaVisibility.completion.partial
    ) return;
    savePartialCompletionMinimum(
      partialCompletionMinimumPercent,
      previousMinimum,
    );
    searchMetaVisibility.completion.partial = true;
    const partialInput = searchFilterRegion.querySelector(
      '[data-search-meta-filter="completion:partial"]'
    );
    if (partialInput instanceof HTMLInputElement) partialInput.checked = true;
    syncMetaFilterGroup('search-completion');
    restoreEmptySearchKindFacets('completion');
    refreshSearchAfterFilterChange('completion', activatedFromSelection);
    return;
  }
  const searchKindFilter = target.dataset.searchKindFilter;
  const searchKindSelectionState = searchKindFilter
    ? renderedSearchKindSelectionState(searchKindFilter)
    : null;
  const selectSearchKind = searchKindSelectionState
    ? !searchKindSelectionState.allSelected
    : target.checked;
  if (searchKindFilter && setSearchKindFilter(searchKindFilter, selectSearchKind)) {
    const plugin = browserSearchPlugin(searchKindFilter);
    if (plugin) {
      if (plugin.search.preferenceKey) {
        saveFilterPreference(plugin.search.preferenceKey, selectSearchKind);
      }
    } else {
      saveSearchOptInPreferences(searchKindFacetKeys(searchKindFilter));
    }
    refreshSearchAfterFilterChange(searchKindFilter, activatedFromSelection);
    return;
  }
  const searchPluginFacetFilter = target.dataset.searchPluginFacetFilter;
  if (searchPluginFacetFilter) {
    const [facetKind, pluginId, filterName] = searchPluginFacetFilter.split(':');
    const clips = facetKind === 'clip';
    const plugin = (clips ? browserClipFilterPlugins() : browserVideoFilterPlugins())
      .find(item => item.id === pluginId);
    if (!plugin) return;
    const state = clips ? browserClipFacetState(plugin) : browserVideoFacetState(plugin);
    if (!Object.prototype.hasOwnProperty.call(state, filterName)) return;
    const treeGroupName = `${clips ? 'search-clip-plugin' : 'search-plugin'}-${plugin.id}`;
    if (!setMetaFilterBranch(treeGroupName, filterName, target.checked)) {
      state[filterName] = target.checked;
    }
    if (clips) saveBrowserClipFacetPreferences(plugin);
    else saveBrowserVideoFacetPreferences(plugin);
    const kind = clips ? 'clips' : 'videos';
    if (target.checked && !searchKindEnabled(kind)) {
      enableDefaultSearchKind(kind);
      renderSearchMetaFilters();
    }
    syncMetaFilterGroup(treeGroupName);
    refreshSearchAfterFilterChange(kind, activatedFromSelection);
    return;
  }
  const metaAllFilter = target.dataset.metaAllFilter;
  const selectAllMetaChildren = metaAllFilter
    ? !allMetaFilterChildrenChecked(metaAllFilter)
    : false;
  if (metaAllFilter && setMetaFilterGroup(metaAllFilter, selectAllMetaChildren)) {
    if (metaAllFilter.startsWith('search-clip-plugin-')) {
      const pluginId = metaAllFilter.slice('search-clip-plugin-'.length);
      const plugin = browserClipFilterPlugins().find(item => item.id === pluginId);
      if (!plugin) return;
      saveBrowserClipFacetPreferences(plugin);
      if (selectAllMetaChildren && !searchKindEnabled('clips')) {
        enableDefaultSearchKind('clips');
        renderSearchMetaFilters();
      }
      syncMetaFilterGroup(metaAllFilter);
      refreshSearchAfterFilterChange('clips', activatedFromSelection);
    } else if (metaAllFilter.startsWith('search-plugin-')) {
      const pluginId = metaAllFilter.slice('search-plugin-'.length);
      const plugin = browserVideoFilterPlugins().find(item => item.id === pluginId);
      if (!plugin) return;
      saveBrowserVideoFacetPreferences(plugin);
      if (selectAllMetaChildren && !searchKindEnabled('videos')) {
        enableDefaultSearchKind('videos');
        renderSearchMetaFilters();
      }
      syncMetaFilterGroup(metaAllFilter);
      refreshSearchAfterFilterChange('videos', activatedFromSelection);
    } else if (metaAllFilter.startsWith('search-')) {
      const facetKey = metaAllFilter.slice('search-'.length);
      saveSearchOptInPreferences([facetKey]);
      syncMetaFilterGroup(metaAllFilter);
      if (target.checked) restoreEmptySearchKindFacets(facetKey);
      refreshSearchAfterFilterChange(facetKey, activatedFromSelection);
    } else {
      if (metaAllFilter === 'playlist-completion') {
        saveFilterPreference(
          filterPreferenceKeys.lowPartialCompletion,
          playlistCompletionVisibility.partial_below_minimum,
        );
      } else if (metaAllFilter === 'playlist-videos') {
        savePlaylistVideoOptInPreferences();
      }
      render();
    }
    return;
  }
  const searchMetaFilter = target.dataset.searchMetaFilter;
  if (searchMetaFilter) {
    const [groupName, filterName] = searchMetaFilter.split(':');
    const visibility = searchMetaVisibility[groupName];
    if (!visibility || !Object.prototype.hasOwnProperty.call(visibility, filterName)) return;
    const treeGroupName = `search-${groupName}`;
    if (!setMetaFilterBranch(treeGroupName, filterName, target.checked)) return;
    const optInFilter = searchOptInFilter(groupName, filterName);
    if (selected.startsWith('__playlist__:') && groupName === 'videos') {
      playlistVisibility[filterName] = target.checked;
      savePlaylistVideoOptInPreferences();
    } else if (optInFilter) {
      saveFilterPreference(optInFilter.preferenceKey, target.checked);
    }
    syncMetaFilterGroup(treeGroupName);
    if (target.checked) restoreEmptySearchKindFacets(groupName);
    refreshSearchAfterFilterChange(groupName, activatedFromSelection);
    return;
  }
  if (target.dataset.playlistDuplicatesFilter) {
    playlistDuplicatesOnly = target.checked;
    currentPage = 1;
    updateCurrentUrl(true);
    render();
    return;
  }
}
meta.addEventListener('change', handleMetaChange);
searchFilterRegion.addEventListener('change', handleMetaChange);
searchFilterRegion.addEventListener('click', event => {
  if (!(event.target instanceof Element)) return;
  const button = event.target.closest('[data-search-tree-toggle]');
  if (!(button instanceof HTMLButtonElement)) return;
  const nodeId = button.dataset.searchTreeToggle || '';
  if (nodeId) toggleSearchFilterTreeNode(nodeId);
});
function scheduleCompletionMinimumInput(event) {
  const target = event.target;
  if (!(target instanceof HTMLInputElement)) return;
  if (target.dataset.searchCompletionMinimum === undefined) return;
  if (completionMinimumInputTimer !== null) {
    clearTimeout(completionMinimumInputTimer);
  }
  completionMinimumInputTimer = setTimeout(() => {
    completionMinimumInputTimer = null;
    handleMetaChange({ target });
  }, 250);
}
searchFilterRegion.addEventListener('input', scheduleCompletionMinimumInput);
function applyCardLayoutPreference(context, layout) {
  if (!Object.prototype.hasOwnProperty.call(cardLayoutPreferences, context)) return;
  cardLayoutPreferences[context] = layout;
  if (activeCardLayoutContext() === context) applyCardLayout(context);
  for (const option of meta.querySelectorAll(`[data-card-layout-context="${context}"]`)) {
    const active = option.dataset.cardLayout === layout;
    option.classList.toggle('active', active);
    option.setAttribute('aria-pressed', String(active));
  }
}

function persistCardLayoutPreference(context, layout) {
  const save = async () => {
    const params = new URLSearchParams({ context, value: layout });
    const response = await fetch(`/api/settings/layout?${params.toString()}`, { method: 'POST' });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) {
      throw new Error(result.error || `Layout save failed (${response.status})`);
    }
  };
  const request = cardLayoutSaveChains[context].catch(() => {}).then(save);
  cardLayoutSaveChains[context] = request;
  return request;
}

function persistSortPreference(context, value) {
  const save = async () => {
    const params = new URLSearchParams({ context, value });
    const response = await fetch(`/api/settings/sort?${params.toString()}`, { method: 'POST' });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) {
      throw new Error(result.error || `Sort preference save failed (${response.status})`);
    }
  };
  const request = sortPreferenceSaveChain.catch(() => {}).then(save);
  sortPreferenceSaveChain = request;
  return request;
}

function saveSortPreference(context, value, previousPreference, onFailure) {
  const version = (sortPreferenceSaveVersions.get(context) || 0) + 1;
  sortPreferenceSaveVersions.set(context, version);
  sortPreferences[context] = value;
  void persistSortPreference(context, value).catch(error => {
    if (sortPreferenceSaveVersions.get(context) !== version) return;
    if (previousPreference === undefined) {
      delete sortPreferences[context];
    } else {
      sortPreferences[context] = previousPreference;
    }
    onFailure();
    window.alert(error instanceof Error ? error.message : String(error));
  });
}

function persistPageSizePreference(value) {
  const save = async () => {
    const params = new URLSearchParams({ value });
    const response = await fetch(`/api/settings/page-size?${params.toString()}`, { method: 'POST' });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) {
      throw new Error(result.error || `Page size save failed (${response.status})`);
    }
  };
  const request = pageSizeSaveChain.catch(() => {}).then(save);
  pageSizeSaveChain = request;
  return request;
}

function persistPartialCompletionMinimum(value) {
  const save = async () => {
    const params = new URLSearchParams({ value });
    const response = await fetch(
      `/api/settings/partial-completion-minimum?${params.toString()}`,
      { method: 'POST' },
    );
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) {
      throw new Error(
        result.error || `Partial completion minimum save failed (${response.status})`
      );
    }
  };
  const request = partialCompletionMinimumSaveChain.catch(() => {}).then(save);
  partialCompletionMinimumSaveChain = request;
  return request;
}

function persistFilterPreference(preferenceKey, enabled) {
  const save = async () => {
    const params = new URLSearchParams({
      key: preferenceKey,
      enabled: enabled ? '1' : '0',
    });
    const response = await fetch(
      `/api/settings/filter-preference?${params.toString()}`,
      { method: 'POST' },
    );
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) {
      throw new Error(result.error || `Filter preference save failed (${response.status})`);
    }
  };
  const request = filterPreferenceSaveChain.catch(() => {}).then(save);
  filterPreferenceSaveChain = request;
  return request;
}

function persistSearchFilterTreePreference(expandedNodes) {
  const save = async () => {
    const params = new URLSearchParams({ expanded: expandedNodes.join(',') });
    const response = await fetch(
      `/api/settings/search-filter-tree?${params.toString()}`,
      { method: 'POST' },
    );
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) {
      throw new Error(result.error || `Filter tree save failed (${response.status})`);
    }
  };
  const request = searchFilterTreeSaveChain.catch(() => {}).then(save);
  searchFilterTreeSaveChain = request;
  return request;
}

function saveSearchFilterTreePreference(nodeId, wasExpanded) {
  const version = ++searchFilterTreeSaveVersion;
  const expandedNodes = [...searchFilterTreeExpanded].sort();
  void persistSearchFilterTreePreference(expandedNodes).catch(error => {
    if (searchFilterTreeSaveVersion !== version) return;
    if (wasExpanded) searchFilterTreeExpanded.add(nodeId);
    else searchFilterTreeExpanded.delete(nodeId);
    applySearchFilterTreeNodeState(nodeId);
    window.alert(error instanceof Error ? error.message : String(error));
  });
}

function persistNavigationGroupTreePreference(collapsedNodes) {
  const save = async () => {
    const params = new URLSearchParams();
    for (const nodeId of collapsedNodes) params.append('collapsed', nodeId);
    const response = await fetch(
      `/api/settings/navigation-group-tree?${params.toString()}`,
      { method: 'POST' },
    );
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) {
      throw new Error(result.error || `Navigation group tree save failed (${response.status})`);
    }
  };
  const request = navigationGroupTreeSaveChain.catch(() => {}).then(save);
  navigationGroupTreeSaveChain = request;
  return request;
}

function saveNavigationGroupTreePreference(nodeId, wasCollapsed) {
  const version = ++navigationGroupTreeSaveVersion;
  const collapsedNodes = [...navigationGroupTreeCollapsed].sort();
  void persistNavigationGroupTreePreference(collapsedNodes).catch(error => {
    if (navigationGroupTreeSaveVersion !== version) return;
    if (wasCollapsed) navigationGroupTreeCollapsed.add(nodeId);
    else navigationGroupTreeCollapsed.delete(nodeId);
    renderGroups();
    window.alert(error instanceof Error ? error.message : String(error));
  });
}

function savePartialCompletionMinimum(value, previousValue) {
  const version = ++partialCompletionMinimumSaveVersion;
  void persistPartialCompletionMinimum(value).catch(error => {
    if (partialCompletionMinimumSaveVersion !== version) return;
    partialCompletionMinimumPercent = previousValue;
    void render();
    window.alert(error instanceof Error ? error.message : String(error));
  });
}

meta.addEventListener('click', async event => {
  const button = event.target.closest('[data-card-layout]');
  if (!(button instanceof HTMLButtonElement)) return;
  const layout = button.dataset.cardLayout || '';
  const context = button.dataset.cardLayoutContext || 'search';
  const activeLayout = cardLayoutFor(context);
  if (!activeLayout || !cardLayouts.has(layout) || layout === activeLayout) return;
  const version = ++cardLayoutSaveVersions[context];
  applyCardLayoutPreference(context, layout);
  try {
    await persistCardLayoutPreference(context, layout);
  } catch (error) {
    if (cardLayoutSaveVersions[context] === version) {
      applyCardLayoutPreference(context, activeLayout);
      window.alert(error instanceof Error ? error.message : String(error));
    }
  }
});
function handlePagerClick(event) {
  const target = event.target;
  if (!(target instanceof HTMLButtonElement)) return;
  const direction = target.dataset.page;
  if (!direction) return;
  const page = Number(direction);
  if (Number.isFinite(page) && page > 0) currentPage = page;
  historyNavigationDate = '';
  pendingHistoryDate = '';
  scrollResultsToTop();
  if (updateCurrentUrl(false)) return;
  render();
}
async function handlePagerChange(event) {
  const target = event.target;
  if (!(target instanceof HTMLSelectElement)) return;
  if (target.dataset.pageSize === undefined) return;
  const previousPageSize = pageSize;
  const nextPageSize = target.value || '100';
  if (nextPageSize === previousPageSize) return;
  const version = ++pageSizeSaveVersion;
  pageSize = nextPageSize;
  currentPage = 1;
  scrollResultsToTop();
  if (!updateCurrentUrl(false)) void render();
  try {
    await persistPageSizePreference(nextPageSize);
  } catch (error) {
    if (pageSizeSaveVersion === version) {
      pageSize = previousPageSize;
      currentPage = 1;
      if (!updateCurrentUrl(false)) void render();
      window.alert(error instanceof Error ? error.message : String(error));
    }
  }
}
function handlePagerSubmit(event) {
  const target = event.target;
  if (!(target instanceof HTMLFormElement) || target.dataset.pageGoto === undefined) return;
  event.preventDefault();
  const input = target.querySelector('[data-page-goto-input]');
  if (!(input instanceof HTMLInputElement)) return;
  const page = Number(input.value);
  const maxPage = Number(input.max || 0);
  if (!Number.isFinite(page) || page < 1) return;
  currentPage = maxPage > 0 ? Math.min(page, maxPage) : page;
  historyNavigationDate = '';
  pendingHistoryDate = '';
  scrollResultsToTop();
  if (updateCurrentUrl(false)) return;
  render();
}

function handlePageBoundaryWheel(event) {
  armPageBoundaryInputAfterIdle();
  if (!pageBoundaryTargetAllowed(event.target) || !pageBoundaryInputArmed) return;
  const direction = Math.sign(event.deltaY);
  if (!direction) return;
  const boundary = pageScrollBoundaryState();
  if ((direction > 0 && !boundary.atBottom) || (direction < 0 && !boundary.atTop)) return;
  if (navigateAcrossPageBoundary(direction)) event.preventDefault();
}

function handlePageBoundaryTouchStart(event) {
  const touch = event.touches?.[0];
  pageBoundaryTouchTargetAllowed = Boolean(touch && pageBoundaryTargetAllowed(event.target));
  pageBoundaryTouchY = touch ? touch.clientY : null;
  pageBoundaryTouchDistance = 0;
  pageBoundaryTouchDirection = 0;
}

function handlePageBoundaryTouchMove(event) {
  const touch = event.touches?.[0];
  if (!touch || pageBoundaryTouchY === null || !pageBoundaryTouchTargetAllowed) return;
  const distance = pageBoundaryTouchY - touch.clientY;
  pageBoundaryTouchY = touch.clientY;
  const direction = Math.sign(distance);
  if (!direction) return;
  const boundary = pageScrollBoundaryState();
  const beyondBoundary = direction > 0 ? boundary.atBottom : boundary.atTop;
  if (!beyondBoundary) {
    pageBoundaryTouchDistance = 0;
    pageBoundaryTouchDirection = 0;
    return;
  }
  if (pageBoundaryTouchDirection !== direction) pageBoundaryTouchDistance = 0;
  pageBoundaryTouchDirection = direction;
  pageBoundaryTouchDistance += Math.abs(distance);
}

function handlePageBoundaryTouchEnd() {
  if (pageBoundaryTouchTargetAllowed && pageBoundaryTouchDistance >= 48) {
    navigateAcrossPageBoundary(pageBoundaryTouchDirection);
  }
  pageBoundaryTouchY = null;
  pageBoundaryTouchDistance = 0;
  pageBoundaryTouchDirection = 0;
  pageBoundaryTouchTargetAllowed = false;
}

bottomPager.addEventListener('click', handlePagerClick);
bottomPager.addEventListener('change', handlePagerChange);
bottomPager.addEventListener('submit', handlePagerSubmit);
window.addEventListener('wheel', handlePageBoundaryWheel, { passive: false });
window.addEventListener('touchstart', handlePageBoundaryTouchStart, { passive: true });
window.addEventListener('touchmove', handlePageBoundaryTouchMove, { passive: true });
window.addEventListener('touchend', handlePageBoundaryTouchEnd, { passive: true });
resultsScroll?.addEventListener('scroll', scheduleHistoryHeatmapCurrentDay, { passive: true });
window.addEventListener('scroll', scheduleHistoryHeatmapCurrentDay, { passive: true });
window.addEventListener('resize', scheduleHistoryHeatmapCurrentDay, { passive: true });
function bindSearchField(input) {
  input.addEventListener('change', () => {
    if (selected === '__history__') {
      currentPage = 1;
      historyNavigationDate = '';
      pendingHistoryDate = '';
      historyActivityYearOffset = 0;
      updateCurrentUrl(true);
      void render();
      return;
    }
    if (scopedVideoDetailSearchActive()) {
      currentPage = 1;
      updateCurrentUrl(true);
      void render();
      return;
    }
    const activatedFromSelection = activateSearchFromSelection({ resetMetaVisibility: true });
    currentPage = 1;
    syncSearchUrlAndRender(!activatedFromSelection);
  });
}
for (const input of searchFields) bindSearchField(input);
function handleBrowserLocationChange() {
  selected = selectionFromLocation();
  if (!selected.startsWith('__channel__:')) {
    channelDetailTab = 'playlisted-videos';
    channelDetailSearchActive = false;
  }
  if (selected.startsWith('__playlist__:')) resetPlaylistVisibilityFor(selected.slice('__playlist__:'.length));
  renderGroups();
  void render();
}
function handleBrowserPopState() {
  const targetHistoryIndex = Number.isInteger(history.state?.[browserHistoryIndexKey])
    ? history.state[browserHistoryIndexKey]
    : null;
  if (restoringAcceptedBrowserHistory) {
    restoringAcceptedBrowserHistory = false;
    browserHistoryIndex = targetHistoryIndex ?? acceptedBrowserHistoryIndex;
    return;
  }
  if (!confirmDiscardUnsavedNotes()) {
    if (targetHistoryIndex !== null && targetHistoryIndex !== acceptedBrowserHistoryIndex) {
      restoringAcceptedBrowserHistory = true;
      history.go(acceptedBrowserHistoryIndex - targetHistoryIndex);
    } else {
      history.pushState(
        { [browserHistoryIndexKey]: acceptedBrowserHistoryIndex },
        '',
        acceptedBrowserUrl,
      );
    }
    return;
  }
  browserHistoryIndex = targetHistoryIndex ?? browserHistoryIndex;
  acceptedBrowserHistoryIndex = browserHistoryIndex;
  acceptedBrowserUrl = currentBrowserUrl();
  handleBrowserLocationChange();
}
window.addEventListener('popstate', handleBrowserPopState);
window.addEventListener('beforeunload', event => {
  if (!hasUnsavedNoteChanges()) return;
  event.preventDefault();
  event.returnValue = '';
});
refresh.addEventListener('click', () => {
  const preserveSearchContent = (
    selected === '__search__'
    && searchResultsRendered
    && renderedOmniSearchQuery === search.value.trim().toLowerCase()
  );
  loadData({ preserveSearchContent }).catch(error => {
    meta.textContent = error.message;
    refresh.disabled = false;
    refresh.textContent = 'Refresh';
  });
});
renderSearchMetaFilters();
loadData().catch(error => {
  title.textContent = 'Unable to load data';
  meta.textContent = error.message;
  refresh.disabled = false;
  refresh.textContent = 'Refresh';
});
