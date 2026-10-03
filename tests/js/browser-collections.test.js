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
    const end = source.indexOf('\n}', start) + 2;
    assert.ok(start >= 0 && end > start, name);
    vm.runInContext(source.slice(start, end), context);
  }
  return context;
}

function setup() {
  const plugin = {id: 'example', collection: {sorts: ['newest', 'oldest', 'most_liked'], fetch: async () => ({total: 0, results: []})}};
  const context = {
    selected: '__collection__:example', search: {value: ''}, URLSearchParams,
    searchResultsSort: 'newest', searchSortExplicit: false, sortPreferences: {meta: 'oldest'},
    window: {location: {pathname: '/example', search: ''}},
    browserSearchPlugins: () => [plugin],
    browserPluginStatus: () => ({browserCollection: {path: '/example', label: 'Example'}}),
    paginationParams: () => new URLSearchParams('page=2'),
    appendUrlParams: (base, params) => `${base}?${params}`,
    applyPaginationParams: () => {},
  };
  load(context, 'browserCollectionPlugins', 'activeBrowserCollection', 'browserCollectionUrl');
  return {plugin, context};
}

test('collection deep links restore query, page and sort without applying native search filters', () => {
  const {context} = setup();
  load(context, 'selectionFromLocation');
  context.window.location.search = '?q=thread-id&page=2&sort=most_liked';
  let page;
  context.applyPaginationParams = params => { page = params.get('page'); };
  assert.equal(context.selectionFromLocation(), '__collection__:example');
  assert.equal(context.search.value, 'thread-id');
  assert.equal(context.searchResultsSort, 'most_liked');
  assert.equal(page, '2');
  assert.equal(context.browserCollectionUrl(context.browserSearchPlugins()[0]), '/example?page=2&q=thread-id&sort=most_liked');
  context.window.location.search = '';
  context.selectionFromLocation();
  assert.equal(context.search.value, '');
  assert.equal(context.searchResultsSort, 'oldest');
});

test('blank collection requests only its provider, independent of disabled global search fields', async () => {
  const {plugin, context} = setup();
  context.currentPage = 2;
  context.pageSizeNumber = () => 50;
  context.browserPluginHost = id => ({id});
  context.searchFieldParamValue = () => { throw new Error('Native filters must not apply'); };
  const requests = [];
  plugin.collection.fetch = async (request, host) => {
    requests.push({request, host});
    return {total: 75, limit: 50, offset: 50, results: [{id: 'thread'}]};
  };
  load(context, 'fetchOmniSearch');
  const payload = await context.fetchOmniSearch('');
  assert.equal(requests.length, 1);
  assert.equal(requests[0].request.query, '');
  assert.equal(requests[0].request.offset, 50);
  assert.equal(requests[0].request.sort, 'newest');
  assert.equal(payload.counts.plugins.example, 75);
  assert.equal(payload.results[0].kind, 'plugin');
  assert.equal(payload.results[0].pluginId, 'example');
});

test('collection hides filters, avoids checkbox creation and restores controls on leaving', () => {
  const {context} = setup();
  Object.assign(context, {
    searchFilters: {hidden: false}, searchFilterTree: {hidden: false},
    searchInFields: {querySelectorAll: () => []},
    searchContextKind: () => '', renderedSearchFilterContext: '',
    syncBrowserPluginSearchFieldVisibility: () => {},
  });
  load(context, 'syncSearchFiltersForSelection', 'appendSearchFilterCategory', 'appendPluginSearchFilters');
  context.syncSearchFiltersForSelection();
  assert.equal(context.searchFilters.hidden, true);
  assert.equal(context.searchFilterTree.hidden, true);
  assert.equal(context.search.placeholder, 'Search example');
  context.appendSearchFilterCategory(null, 'videos', 'Videos', 1);
  context.appendPluginSearchFilters(null);
  context.selected = '__search__';
  context.syncSearchFiltersForSelection();
  assert.equal(context.searchFilters.hidden, false);
  assert.equal(context.searchFilterTree.hidden, false);
  assert.equal(context.search.placeholder, 'Search everything');
});

test('rapid collection input debounces locally and never activates global search', () => {
  const {context} = setup();
  const timers = new Map();
  let serial = 0;
  let input;
  let renders = 0;
  const urls = [];
  Object.assign(context, {
    searchInputTimer: null, currentPage: 5,
    updateCurrentUrl: () => urls.push(context.browserCollectionUrl(context.browserSearchPlugins()[0])),
    setTimeout: callback => { timers.set(++serial, callback); return serial; },
    clearTimeout: id => timers.delete(id), render: () => { renders++; },
  });
  context.search.addEventListener = (_, callback) => { input = callback; };
  const start = source.indexOf("search.addEventListener('input',");
  vm.runInContext(source.slice(start, source.indexOf("\nhistoryNav?.addEventListener", start)), context);
  for (const value of ['a', 'ab', 'thread-id']) { context.search.value = value; input(); }
  assert.equal(timers.size, 1);
  for (const callback of timers.values()) callback();
  assert.equal(renders, 1);
  assert.equal(context.currentPage, 1);
  assert.match(urls.at(-1), /q=thread-id/);
  assert.equal(context.selected, '__collection__:example');
});

test('Meta navigation sits between Videos and Playlists', () => {
  const start = source.indexOf('function renderGroups()');
  const end = source.indexOf('\nasync function ', start);
  const groups = source.slice(start, end);
  assert.ok(groups.indexOf("sectionFor('Videos')") < groups.indexOf("sectionFor('Meta')"));
  assert.ok(groups.indexOf("sectionFor('Meta')") < groups.indexOf("sectionFor('Playlists')"));
});
