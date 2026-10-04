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
    pluginSearchFilters: new Map(), filterPreferenceEnabled: () => false,
  };
  load(context, 'browserPluginSearchFilters', 'applyPluginSearchFilterLocation', 'appendPluginSearchFilterParams',
    'browserCollectionPlugins', 'activeBrowserCollection', 'browserCollectionUrl');
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

test('multiple independent collections share Meta and require search registration', () => {
  const {context, plugin} = setup();
  const second = {id: 'second', collection: {fetch: async () => ({total: 0, results: []})}};
  context.browserPluginStatus = id => ({browserCollection: {path: `/${id}`, label: id}});
  context.browserSearchPlugins = () => [plugin, second];
  assert.deepEqual(Array.from(context.browserCollectionPlugins(), value => value.id), ['example', 'second']);
  context.browserSearchPlugins = () => [second];
  assert.deepEqual(Array.from(context.browserCollectionPlugins(), value => value.id), ['second']);
  context.browserSearchPlugins = () => [];
  assert.equal(context.browserCollectionPlugins().length, 0);
  const start = source.indexOf('function renderGroups()');
  const groups = source.slice(start, source.indexOf('\nasync function ', start));
  assert.equal((groups.match(/sectionFor\('Meta'\)/g) || []).length, 1);
});

test('provider options round-trip independent URL and preference state and forward API booleans', async () => {
  const {context, plugin} = setup();
  plugin.search = {label: 'Example', filters: [
    {key: 'first', label: 'first', hashParam: 'example-first', disabledPreferenceKey: 'plugins.example.filters.hide_first'},
    {key: 'second', label: 'second', hashParam: 'example-second', disabledPreferenceKey: 'plugins.example.filters.hide_second'},
  ]};
  context.filterPreferenceEnabled = key => key.endsWith('hide_second');
  context.applyPluginSearchFilterLocation(plugin);
  assert.equal(context.browserPluginSearchFilters(plugin).second, false);
  context.applyPluginSearchFilterLocation(plugin, new URLSearchParams('example-first=0&example-second=1'));
  assert.equal(context.browserPluginSearchFilters(plugin).first, false);
  assert.equal(context.browserPluginSearchFilters(plugin).second, true);
  const params = new URLSearchParams();
  context.appendPluginSearchFilterParams(params, plugin, true);
  assert.deepEqual(JSON.parse(params.get('plugin_filters_example')), {first: false, second: true});
  const url = context.browserCollectionUrl(plugin);
  assert.match(url, /example-first=0&example-second=1/);
  context.pluginSearchFilters.clear();
  context.applyPluginSearchFilterLocation(plugin, new URLSearchParams(url.split('?')[1]));
  assert.equal(context.browserPluginSearchFilters(plugin).first, false);
  let request;
  plugin.collection.fetch = async value => { request = value; return {total: 0, results: []}; };
  Object.assign(context, {currentPage: 1, pageSizeNumber: () => 50, browserPluginHost: () => ({})});
  load(context, 'fetchOmniSearch');
  await context.fetchOmniSearch('phrase');
  assert.deepEqual(JSON.parse(JSON.stringify(request.filters)), {first: false, second: true});
});

test('rapid provider toggles reset pagination and remain inside the active collection', () => {
  const {context, plugin} = setup();
  plugin.search = {filters: [{key: 'first', disabledPreferenceKey: 'plugins.example.filters.hide_first'}]};
  context.applyPluginSearchFilterLocation(plugin);
  class Input { constructor(checked) { this.checked = checked; this.dataset = {pluginSearchOption: 'example:first'}; } }
  const saved = [];
  const urls = [];
  Object.assign(context, {
    HTMLInputElement: Input, HTMLSelectElement: class {}, currentPage: 4,
    saveFilterPreference: (key, disabled) => saved.push([key, disabled]),
    syncSearchUrlAndRender: () => urls.push(context.browserPluginSearchFilters(plugin).first),
  });
  load(context, 'handleMetaChange');
  for (const checked of [false, true, false]) context.handleMetaChange({target: new Input(checked)});
  assert.equal(context.currentPage, 1);
  assert.equal(context.selected, '__collection__:example');
  assert.deepEqual(urls, [false, true, false]);
  assert.deepEqual(saved.at(-1), ['plugins.example.filters.hide_first', true]);
});
