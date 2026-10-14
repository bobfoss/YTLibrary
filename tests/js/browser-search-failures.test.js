const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(process.cwd(), 'yt_library/templates/index.js'), 'utf8');
function load(context, ...names) {
  vm.createContext(context);
  for (const name of names) {
    const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
    const end = start + source.slice(start).search(/\n}\r?\n/) + 2;
    assert.ok(start >= 0 && end > start, name);
    vm.runInContext(source.slice(start, end), context);
  }
  return context;
}

function setup(fetch) {
  const context = {
    URLSearchParams, currentPage: 1, pageSizeNumber: () => 50,
    activeBrowserCollection: () => null, searchFieldParamValue: () => 'titles',
    selectedSearchResultKinds: () => ['video'], searchPlaylistGroupKey: '', searchChannelGroupKey: '',
    partialCompletionMinimumPercent: 1, searchResultsSort: 'newest', searchMetaVisibility: {},
    browserSearchPlugins: () => [], browserServerSearchPlugins: () => [], browserPluginStateKey: () => '',
    browserVideoFilterPlugins: () => [], browserClipFilterPlugins: () => [],
    browserClipFacetSearchActive: () => false,
    browserVideoSearchFieldPlugins: () => [], browserClipSearchFieldPlugins: () => [],
    metaFilterParamValue: () => '', allMetaFiltersEnabled: () => true, adjacentPageCacheLimit: 6,
    fetchBrowserPluginSearches: async () => ({remaining: 50, coreOffset: 0, total: 0,
      totalIsExact: true, results: [], counts: {}, errors: []}),
    fetch,
  };
  for (const name of ['omniSearchCache', 'omniMetaCountsCache', 'omniVideoTypeCountsCache',
    'omniBroadcastStatusCountsCache', 'omniAiDisclosureCountsCache', 'omniVideoPluginFacetCountsCache',
    'omniClipPluginFacetCountsCache', 'omniReactionCountsCache', 'omniCompletionCountsCache',
    'omniPlaylistMembershipCountsCache', 'omniUploaderCategoryCountsCache']) context[name] = new Map();
  return load(context, 'trimRequestCache', 'cachedRequest', 'fetchOmniSearch');
}

test('partial search is explicit and is not cached as a complete result or facet snapshot', async () => {
  let calls = 0;
  const context = setup(async () => ({ok: true, json: async () => (++calls === 1 ? {
    total: 1, results: [{kind: 'video'}], pluginErrors: [{id: 'example', message: 'offline'}],
  } : {total: 2, results: [{kind: 'video'}, {kind: 'plugin'}], metaCounts: {videos: {public: 2}}})}));
  const partial = await context.fetchOmniSearch('needle');
  assert.equal(partial.resultsIncomplete, true);
  assert.equal(partial.totalIsExact, false);
  assert.equal(partial.countsAreExact, false);
  assert.equal(partial.results[0].kind, 'video');
  assert.equal(context.omniSearchCache.size, 0);
  assert.equal(context.omniMetaCountsCache.size, 0);
  const recovered = await context.fetchOmniSearch('needle');
  assert.equal(recovered.total, 2);
  assert.equal(recovered.totalIsExact, true);
  assert.equal(recovered.metaCounts.videos.public, 2);
  assert.equal(context.omniSearchCache.size, 1);
  await context.fetchOmniSearch('needle');
  assert.equal(calls, 2);
});

test('restrictive search failures show the server explanation and are retryable', async () => {
  const context = setup(async () => ({ok: false, status: 503,
    json: async () => ({error: 'Video filter unavailable', message: 'Selected filter cannot be applied. Retry.'})}));
  await assert.rejects(context.fetchOmniSearch('needle'), /Selected filter cannot be applied/);
  assert.equal(context.omniSearchCache.size, 0);
  context.fetch = async () => ({ok: false, status: 503, json: async () => { throw new Error('not json'); }});
  await assert.rejects(context.fetchOmniSearch('needle'), /Search failed: 503/);
});

test('warnings and pagination explicitly qualify incomplete results', () => {
  const appended = [];
  const context = load({
    document: {createElement: () => ({setAttribute(name, value) { this[name] = value; }})},
    browserSearchPlugin: () => ({search: {label: 'Example records'}}),
    pageJumpHtml: () => '', pageGotoHtml: () => '',
  }, 'appendPluginSearchWarnings', 'pagerHtml');
  context.appendPluginSearchWarnings({append: node => appended.push(node)}, [
    {id: 'example', label: 'example', message: '<unsafe> verification unavailable'},
  ]);
  assert.equal(appended[0].role, 'status');
  assert.match(appended[0].textContent, /Example records unavailable/);
  assert.match(appended[0].textContent, /Refresh to retry/);
  assert.match(appended[0].textContent, /counts may be incomplete/);
  assert.equal(appended[0].innerHTML, undefined);
  assert.match(context.pagerHtml({total: 10, start: 1, end: 10, resultsIncomplete: true}), /of 10 \(incomplete\)/);
});

test('incomplete filter counts use unknown markers without dropping the selected controls', () => {
  const counts = [{textContent: '10'}, {textContent: '2'}];
  const context = load({
    document: {createElement: () => ({content: {querySelectorAll: () => []}})},
    searchFilterRegion: {querySelectorAll: selector => selector.startsWith('.meta-filter-count') ? counts : []},
    searchContextKind: () => '', syncUploaderCategoryVisibility: () => {}, searchMetaFiltersHtml: () => '',
    searchKindFacetKeys: () => [], browserVideoFilterPlugins: () => [], browserClipFilterPlugins: () => [],
    browserResultSearchPlugins: () => [], syncSearchKindFilter: () => {}, updateSearchMetaProgress: () => {},
  }, 'renderSearchMetaFilters');
  context.renderSearchMetaFilters({metaCounts: {}, countsAreExact: false});
  assert.deepEqual(counts.map(node => node.textContent), ['?', '?']);
  assert.equal(context.renderedSearchFilterPayload.countsAreExact, false);
});

test('fatal search removes stale cards and pager but keeps author and sort controls', () => {
  const start = source.indexOf('payload = await fetchOmniSearch(query);');
  const failure = source.slice(start, source.indexOf("if (!payload.resultsIncomplete", start));
  assert.match(failure, /browserSearchFilterOptionsHtml\(collection\)/);
  assert.match(failure, /searchResultsSortHtml/);
  assert.match(failure, /grid.replaceChildren\(\)/);
  assert.match(failure, /renderPager\(\{ total: 0 \}\)/);
  assert.match(failure, /generation !== renderGeneration/);
});

test('fatal-search recovery UI runs with unavailable plugin counts', async () => {
  const context = vm.createContext({
    generation: 1, renderGeneration: 1, query: 'needle', collection: null,
    fetchOmniSearch: async () => { throw new Error('Selected filter cannot be applied'); },
    stopSearchFilterProgress: () => {}, stopSearchProgress: () => {}, searchResultsRendered: true,
    title: {}, meta: {}, empty: {}, grid: {replaceChildren() { this.cleared = true; }},
    rightPanelListMetaHtml: text => text,
    searchResultsSortHtml: counts => { assert.ok(counts && typeof counts === 'object'); return 'Sort'; },
    browserSearchFilterOptionsHtml: () => 'Author controls',
    renderSearchMetaFilters: () => {}, renderedSearchFilterPayload: {},
    renderPager: info => { assert.equal(info.total, 0); },
  });
  const start = source.lastIndexOf('    let payload;', source.indexOf('payload = await fetchOmniSearch(query);'));
  const end = source.indexOf('    if (!payload.resultsIncomplete', start);
  await vm.runInContext(`(async () => {${source.slice(start, end)}})()`, context);
  assert.equal(context.title.textContent, 'Search unavailable');
  assert.equal(context.empty.textContent, 'Selected filter cannot be applied');
  assert.equal(context.grid.cleared, true);
  assert.match(context.meta.innerHTML, /Author controls/);
});
