const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const templateDirectory = path.join(process.cwd(), 'yt_library', 'templates');
const indexSource = fs.readFileSync(path.join(templateDirectory, 'index.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(templateDirectory, 'index.html'), 'utf8');
const videoCardSource = fs.readFileSync(path.join(templateDirectory, 'video-card.js'), 'utf8');
const collectionCardSource = fs.readFileSync(path.join(templateDirectory, 'collection-card.js'), 'utf8');
const extensionSource = fs.readFileSync(
  path.join(templateDirectory, 'entity-card-extensions.js'),
  'utf8',
);
const searchPresentationSource = fs.readFileSync(
  path.join(templateDirectory, 'search-result-presentations.js'),
  'utf8',
);

class FakeElement {
  constructor(tagName = 'div') {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.className = '';
    this.hidden = false;
    this.textContent = '';
  }

  append(...elements) {
    for (const element of elements) {
      if (element.parentNode) {
        element.parentNode.children = element.parentNode.children.filter(item => item !== element);
      }
      element.parentNode = this;
      this.children.push(element);
    }
  }

  replaceChildren(...elements) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this.append(...elements);
  }

  querySelector(selector) {
    const entitySlot = /^\[data-entity-card-slot="([^"]+)"\]$/.exec(selector);
    if (entitySlot && this.dataset.entityCardSlot === entitySlot[1]) return this;
    const searchSlot = /^\[data-search-result-slot="([^"]+)"\]$/.exec(selector);
    if (searchSlot && this.dataset.searchResultSlot === searchSlot[1]) return this;
    if (selector === '[data-search-result-native-summary]' && this.dataset.searchResultNativeSummary !== undefined) {
      return this;
    }
    if (selector.startsWith('.') && this.className.split(/\s+/).includes(selector.slice(1))) return this;
    for (const child of this.children) {
      const found = child.querySelector(selector);
      if (found) return found;
    }
    return null;
  }
}

function browserExtensionContext(errors = []) {
  return {
    console: { error: (...args) => errors.push(args) },
    document: { createElement: tagName => new FakeElement(tagName) },
    HTMLElement: FakeElement,
    window: {},
  };
}

function loadEntityCardExtensions(errors = []) {
  const context = browserExtensionContext(errors);
  vm.runInNewContext(extensionSource, context, { filename: 'entity-card-extensions.js' });
  return context.window.YTLibraryEntityCardExtensions;
}

function loadSearchResultPresentations(errors = []) {
  const context = browserExtensionContext(errors);
  vm.runInNewContext(searchPresentationSource, context, {
    filename: 'search-result-presentations.js',
  });
  return context.window.YTLibrarySearchResultPresentations;
}

function fakeCard() {
  const card = new FakeElement('article');
  const actions = new FakeElement('span');
  actions.dataset.entityCardSlot = 'actions';
  const primaryMetadata = new FakeElement('span');
  primaryMetadata.dataset.entityCardSlot = 'primaryMetadata';
  const secondaryMetadata = new FakeElement('div');
  secondaryMetadata.dataset.entityCardSlot = 'secondaryMetadata';
  card.append(actions, primaryMetadata, secondaryMetadata);
  return { actions, card, primaryMetadata, secondaryMetadata };
}

function fakeSearchCard() {
  const card = new FakeElement('article');
  const kind = new FakeElement('div');
  kind.className = 'result-kind';
  kind.textContent = 'Video';
  const summaries = new FakeElement('div');
  summaries.dataset.searchResultSlot = 'summaries';
  const nativeSummary = new FakeElement('div');
  nativeSummary.className = 'description';
  nativeSummary.dataset.searchResultNativeSummary = '';
  card.append(kind, summaries, nativeSummary);
  return { card, kind, nativeSummary, summaries };
}

function labeledElement(label) {
  const element = new FakeElement('button');
  element.textContent = label;
  return element;
}

function plugin(id, overrides = {}) {
  return {
    id,
    entityCards: {
      capability: `${id}_cards`,
      kinds: ['video'],
      render: entity => ({
        actions: [labeledElement(`${id}:${entity.id}:action`)],
        primaryMetadata: [labeledElement(`${id}:${entity.id}:primary`)],
        secondaryMetadata: [labeledElement(`${id}:${entity.id}:metadata`)],
      }),
      ...overrides,
    },
  };
}

function videoEntry(extensions, id, card = fakeCard().card) {
  const item = { video_id: id, metadata_title: `Video ${id}` };
  return {
    card,
    entity: extensions.descriptor('video', item),
  };
}

function contributionLabels(slot) {
  return slot.children.flatMap(contribution => (
    contribution.children.map(element => element.textContent)
  ));
}

test('unified result cards require a query and enabled video and search facets', () => {
  let videos = true;
  let present = true;
  let field = true;
  const sample = {id: 'sample', search: {serverResults: true}};
  const context = {
    searchKindEnabled: () => videos,
    browserVideoFilterPlugins: () => [sample],
    browserVideoFacetState: () => ({present}),
    browserSearchFieldDefinition: () => ({key: 'sample'}),
    browserPluginSearchFieldEnabled: () => field,
  };
  const start = indexSource.indexOf('function browserServerSearchPlugins(');
  const end = indexSource.indexOf('\nfunction ', start + 1);
  vm.runInNewContext(indexSource.slice(start, end), context);
  assert.equal(context.browserServerSearchPlugins('  ').length, 0);
  assert.equal(context.browserServerSearchPlugins('needle').length, 1);
  field = false;
  assert.equal(context.browserServerSearchPlugins('needle').length, 0);
  field = true;
  present = false;
  assert.equal(context.browserServerSearchPlugins('needle').length, 0);
  present = true;
  videos = false;
  assert.equal(context.browserServerSearchPlugins('needle').length, 0);
});

test('Most liked is offered only for matching plugin cards in the main sort control', () => {
  let plugins = [{id: 'sample', search: {sortOptions: [{value: 'most_liked', label: 'Most liked'}]}}];
  const context = {browserSearchPlugins: () => plugins, searchResultsSort: 'most_liked', escapeHtml: text => text};
  const start = indexSource.indexOf('function pluginSearchSortOptions(');
  const end = indexSource.indexOf('\nfunction cardLayoutIconSvg', start + 1);
  vm.runInNewContext(indexSource.slice(start, end), context);
  assert.doesNotMatch(context.searchResultsSortHtml({}), /Most liked/);
  assert.doesNotMatch(context.searchResultsSortHtml({sample: 0}), /Most liked/);
  assert.match(context.searchResultsSortHtml({sample: 2}), /value="most_liked" selected>Most liked/);
  plugins = [];
  assert.doesNotMatch(context.searchResultsSortHtml({sample: 2}), /Most liked/);
});

test('standalone result plugins honor their Search in field without hiding empty-query browsing', async () => {
  const start = indexSource.indexOf('async function fetchBrowserPluginSearches(');
  const end = indexSource.indexOf('\nasync function ', start + 1);
  let enabled = false;
  let calls = 0;
  const context = {
    browserResultSearchPlugins: () => [{id: 'sample', search: {
      fetchEmptyQuery: true,
      fetch: async () => { calls += 1; return {total: 1, results: [{id: 'one'}]}; },
    }}],
    searchKindEnabled: () => true,
    browserSearchFieldDefinition: () => ({key: 'sample'}),
    browserPluginSearchFieldEnabled: () => enabled,
    browserPluginHost: () => ({}),
    browserVideoFacetResultPlugins: () => [],
  };
  vm.runInNewContext(indexSource.slice(start, end), context);
  assert.equal((await context.fetchBrowserPluginSearches('needle', 20, 0)).total, 0);
  assert.equal(calls, 0);
  enabled = true;
  assert.equal((await context.fetchBrowserPluginSearches('needle', 20, 0)).total, 1);
  enabled = false;
  assert.equal((await context.fetchBrowserPluginSearches('', 20, 0)).total, 1);
  assert.equal(calls, 2);
});

test('video facets can keep separate result cards without a top-level selector', async () => {
  let videosEnabled = true;
  let present = true;
  let calls = 0;
  const sample = {id: 'sample', search: {
    videoFacet: {}, separateResults: true, fetchEmptyQuery: true,
    fetch: async () => { calls += 1; return {total: 1, results: [{id: 'thread'}]}; },
  }};
  const context = {
    browserSearchPlugins: () => [sample],
    browserVideoFilterPlugins: () => [sample],
    browserVideoFacetDefinition: plugin => plugin.search.videoFacet,
    browserClipFacetDefinition: () => null,
    browserVideoFacetState: () => ({present, absent: true}),
    searchKindEnabled: () => videosEnabled,
    browserSearchFieldDefinition: () => null,
    browserPluginHost: () => ({}),
  };
  for (const name of ['browserResultSearchPlugins', 'browserVideoFacetResultPlugins', 'fetchBrowserPluginSearches']) {
    const start = indexSource.indexOf(`${name === 'fetchBrowserPluginSearches' ? 'async ' : ''}function ${name}(`);
    const match = /\n(?:async )?function /.exec(indexSource.slice(start + 1));
    vm.runInNewContext(indexSource.slice(start, start + 1 + match.index), context);
  }
  assert.equal(context.browserResultSearchPlugins().length, 0);
  assert.equal((await context.fetchBrowserPluginSearches('', 20, 0)).total, 1);
  present = false;
  assert.equal((await context.fetchBrowserPluginSearches('', 20, 0)).total, 0);
  present = true;
  videosEnabled = false;
  assert.equal((await context.fetchBrowserPluginSearches('', 20, 0)).total, 0);
  videosEnabled = true;
  assert.equal((await context.fetchBrowserPluginSearches('needle', 20, 0)).total, 1);
  assert.equal(calls, 2);
});

test('browser plugins are loaded through a generic registration contract', () => {
  assert.match(indexSource, /window\.YTLibraryBrowserPlugins = Object\.freeze/);
  assert.match(indexSource, /register: registerBrowserPlugin/);
  assert.match(indexSource, /apiVersion: 2/);
  assert.match(indexSource, /channelVideoTabs: 1/);
  assert.match(
    indexSource,
    /features: Object\.freeze\(\{[\s\S]{0,160}entityCards: 1,[\s\S]{0,80}searchResultPresentations: 1/,
  );
  assert.match(indexSource, /status\.browserAssets \|\| \[\]/);
  assert.match(indexSource, /\/plugins\/\$\{encodeURIComponent\(pluginId\)\}\/assets\//);
  assert.match(indexSource, /\?v=\$\{encodeURIComponent\(version\)\}/);
  assert.match(indexSource, /loadBrowserPluginAsset\(status\.id, asset, status\.version\)/);
  assert.match(
    indexSource,
    /try \{\s*await loadBrowserPluginAsset[\s\S]{0,180}console\.error\(`Plugin asset failed:/,
  );
  assert.match(indexSource, /libraryVideos,/);
  assert.match(indexSource, /libraryChannels,/);
  assert.match(indexSource, /localChannelHref,/);
  assert.match(indexSource, /validateBrowserChannelVideoTabs\(plugin\)/);
  assert.match(indexSource, /function hydrateChannelTabCounts\(/);
  assert.match(indexSource, /async function fetchChannelTabCount\(/);
  assert.match(indexSource, /createSearchVideoCard: searchVideoCardFor/);
  assert.match(indexSource, /searchHighlight,/);
  assert.doesNotMatch(indexSource, /browserSearchPresets/);
  assert.match(indexSource, /searchPresetDefinition\(preset\)/);
  assert.match(indexSource, /query \|\| plugin\.search\.fetchEmptyQuery === true/);
  assert.match(indexSource, /SearchResultPresentations\.validateDefinition/);
  assert.match(indexSource, /SearchResultPresentations\.prepareBatch/);
  assert.match(indexSource, /SearchResultPresentations\.apply/);
  assert.doesNotMatch(indexSource, /decorateCoreResults|decorateCoreResultCard|legacySearch/i);
  assert.match(indexSource, /search-plugin-facet-filter/);
  assert.match(indexSource, /browserVideoFacetState/);
  assert.match(indexSource, /facetSelections\.push\([\s\S]*browserVideoFilterPlugins/);
  assert.match(indexSource, /restoreEmptySearchKindFacets[\s\S]*Object\.assign\(state, \{ present: true, absent: true \}\)/);
  assert.match(indexSource, /requestParams\.append\('video_facet_plugin', plugin\.id\)/);
  assert.match(indexSource, /requestParams\.append\('video_filter_plugin', plugin\.id\)/);
  assert.match(indexSource, /requestParams\.append\('video_exclude_filter_plugin', plugin\.id\)/);
  assert.match(indexSource, /plugin\?\.search\?\.searchField/);
  assert.match(indexSource, /label\.dataset\.browserPluginSearchField = plugin\.id/);
  assert.match(indexSource, /requestParams\.append\('video_search_plugin', plugin\.id\)/);
  assert.match(indexSource, /requestParams\.append\('video_search_plugin', '__none__'\)/);
  assert.match(indexSource, /browserClipFacetState/);
  assert.match(indexSource, /browserClipFilterPlugins/);
  assert.match(indexSource, /requestParams\.append\('clip_facet_plugin', plugin\.id\)/);
  assert.match(indexSource, /requestParams\.append\('clip_filter_plugin', plugin\.id\)/);
  assert.match(indexSource, /requestParams\.append\('clip_exclude_filter_plugin', plugin\.id\)/);
  assert.match(indexSource, /requestParams\.append\('clip_search_plugin', plugin\.id\)/);
  assert.match(indexSource, /requestParams\.append\('clip_search_plugin', '__none__'\)/);
  assert.match(indexSource, /metaCounts\?\.clipPlugins\?\.\[plugin\.id\]/);
});

test('plugin search fields can be limited to applicable result kinds', () => {
  assert.match(indexSource, /definition\?\.appliesToKinds === undefined/);
  assert.match(indexSource, /function browserSearchFieldAppliesToCurrentContext\(plugin\)/);
  assert.match(indexSource, /return selectedSearchKinds\(\)\.some\(kind => applicableKinds\.has\(kind\)\)/);
  assert.match(indexSource, /label\.hidden = !applies/);
  assert.match(indexSource, /label\.style\.display = applies \? '' : 'none'/);
  assert.match(indexSource, /input\.disabled = !applies/);
  assert.match(indexSource, /function applicableSearchFields\(\)[\s\S]*?input => !input\.disabled/);
  assert.match(indexSource, /function syncSearchFiltersForSelection\(\)[\s\S]*?syncBrowserPluginSearchFieldVisibility\(\)/);
  assert.match(indexSource, /function refreshSearchAfterFilterChange\([\s\S]*?syncBrowserPluginSearchFieldVisibility\(\)/);
});

test('structured search presentation and entity-detail panels are first-class', () => {
  assert.match(indexSource, /async function prepareBrowserSearchResultPresentations\(/);
  assert.match(indexSource, /plugin\.search\?\.resultPresentation/);
  assert.match(indexSource, /resultPresentations\.get\(result\)/);
  assert.match(indexSource, /async function renderBrowserPluginVideoPanels\(/);
  assert.match(indexSource, /const extension = plugin\.videoDetail/);
  assert.match(indexSource, /renderBrowserPluginVideoPanels\(videoId\)/);
  assert.match(indexSource, /async function renderBrowserPluginClipPanels\(clip\)/);
  assert.match(indexSource, /const extension = plugin\.clipDetail/);
  assert.match(indexSource, /renderBrowserPluginClipPanels\(clip\)/);
  assert.match(
    indexSource,
    /grid\.replaceChildren\([\s\S]*?annotationEditorFor\('clip', clip\)[\s\S]*?\.\.\.pluginPanels/,
  );
  assert.match(
    indexSource,
    /grid\.replaceChildren\([\s\S]*?annotationEditorFor\('video', video\)[\s\S]*?\.\.\.pluginPanels/,
  );
});

test('plugin channel video tabs are capability gated and use native cards', () => {
  assert.match(indexSource, /function browserChannelVideoTabs\(\)/);
  assert.match(
    indexSource,
    /browserPluginSupports\(plugin\.id, definition\.capability\)/,
  );
  assert.match(indexSource, /activePluginVideoTab\.definition\.load\(/);
  assert.match(indexSource, /const hydratedVideos = await libraryVideos\(videoIds\)/);
  assert.match(indexSource, /const cards = rows\.map\(video => searchVideoCardFor\(video\)\)/);
  assert.match(indexSource, /'channel-plugin-video-tab'/);
  assert.match(indexSource, /else if \(activePluginVideoTab\)/);
});

test('entity-card definitions and canonical native descriptors are validated', () => {
  const extensions = loadEntityCardExtensions();

  assert.doesNotThrow(() => extensions.validateDefinition(plugin('plain').entityCards));
  assert.throws(
    () => extensions.validateDefinition({ capability: 'x', kinds: ['unknown'], render() {} }),
    /Unsupported entityCards kind/,
  );
  assert.throws(
    () => extensions.validateDefinition({ capability: 'x', kinds: ['video', 'video'], render() {} }),
    /must be unique/,
  );
  assert.equal(extensions.descriptor('video', {}), null);
  assert.equal(extensions.descriptor('clip', { clip_id: 'clip-1' }).id, 'clip-1');
});

test('search result presentations prepare once and apply structured contributions', async () => {
  const presentations = loadSearchResultPresentations();
  const result = {
    kind: 'video',
    item: { video_id: 'video-1' },
    pluginFacets: { first: true },
    pluginSearchMatches: ['first'],
  };
  const preparedDescriptors = [];
  const first = {
    id: 'first',
    search: {
      resultPresentation: {
        kinds: ['video'],
        prepare: async descriptors => {
          preparedDescriptors.push(...descriptors);
          return new Set(['video-1']);
        },
        render: (descriptor, prepared) => ({
          kindLabel: prepared.has(descriptor.id) ? 'Match' : '',
          summary: labeledElement(`first:${descriptor.id}`),
        }),
      },
    },
  };
  const second = {
    id: 'second',
    search: {
      resultPresentation: {
        kinds: ['video'],
        render: descriptor => ({ summary: labeledElement(`second:${descriptor.id}`) }),
      },
    },
  };

  assert.doesNotThrow(() => presentations.validateDefinition(first.search.resultPresentation));
  assert.throws(
    () => presentations.validateDefinition({ kinds: ['unknown'], render() {} }),
    /Unsupported search resultPresentation kind/,
  );
  const batch = await presentations.prepareBatch({
    context: { query: 'match' },
    hostFor: pluginId => ({ pluginId }),
    plugins: [first, second],
    results: [result],
  });
  assert.equal(batch.failures.length, 0);
  assert.equal(preparedDescriptors[0].id, 'video-1');
  assert.equal(preparedDescriptors[0].pluginFacets.first, true);
  assert.deepEqual([...preparedDescriptors[0].pluginSearchMatches], ['first']);

  const target = fakeSearchCard();
  presentations.apply(target.card, batch.presentations.get(result));
  assert.equal(target.kind.textContent, 'Match');
  assert.equal(target.nativeSummary.hidden, true);
  assert.deepEqual(
    target.summaries.children.map(item => item.dataset.browserPluginId),
    ['first', 'second'],
  );
  assert.deepEqual(contributionLabels(target.summaries), ['first:video-1', 'second:video-1']);
});

test('search result presentation failures are isolated by plugin', async () => {
  const presentations = loadSearchResultPresentations();
  const result = { kind: 'video', item: { video_id: 'video-1' } };
  const broken = {
    id: 'broken',
    search: {
      resultPresentation: {
        kinds: ['video'],
        prepare: async () => { throw new Error('broken prepare'); },
        render: () => null,
      },
    },
  };
  const healthy = {
    id: 'healthy',
    search: {
      resultPresentation: {
        kinds: ['video'],
        render: descriptor => ({ summary: labeledElement(`healthy:${descriptor.id}`) }),
      },
    },
  };

  const batch = await presentations.prepareBatch({
    plugins: [broken, healthy],
    results: [result],
  });
  assert.equal(batch.failures.length, 1);
  assert.equal(batch.failures[0].pluginId, 'broken');
  assert.equal(batch.presentations.get(result).summaries[0].pluginId, 'healthy');
});

test('entity-card plugins prepare once per batch and decorate every native view', async () => {
  const extensions = loadEntityCardExtensions();
  const cases = [
    { kind: 'video', view: 'search' },
    { kind: 'clip', view: 'search' },
    { kind: 'video', view: 'playlist' },
    { kind: 'video', view: 'history' },
    { kind: 'video', view: 'channel-history' },
    { kind: 'video', view: 'channel-playlisted-videos' },
    { kind: 'video', view: 'channel-plugin-video-tab' },
    { kind: 'playlist', view: 'channel-playlists' },
    { kind: 'video', view: 'video-detail' },
    { kind: 'clip', view: 'clip-detail' },
  ];
  const preparedContexts = [];
  const genericPlugin = plugin('generic', {
    kinds: ['video', 'clip', 'playlist'],
    prepare: async (entities, _host, context) => {
      preparedContexts.push({ ids: entities.map(entity => entity.id), view: context.view });
      return new Set(entities.map(entity => entity.id));
    },
    render: (entity, prepared, _host, context) => ({
      actions: [labeledElement(`${context.view}:${entity.id}:${prepared.has(entity.id)}`)],
      secondaryMetadata: [],
    }),
  });

  for (const { kind, view } of cases) {
    const first = fakeCard();
    const second = fakeCard();
    const item = kind === 'clip'
      ? { clip_id: 'clip-1' }
      : (kind === 'playlist' ? { playlist_id: 'playlist-1' } : { video_id: 'video-1' });
    const entries = [first, second].map(({ card }) => ({
      card,
      entity: extensions.descriptor(kind, item),
    }));
    await extensions.decorateBatch({
      context: { view, layout: 'detailed' },
      entries,
      hostFor: () => ({}),
      plugins: [genericPlugin],
      supports: () => true,
    });
    assert.deepEqual(contributionLabels(first.actions), [`${view}:${item[`${kind}_id`]}:true`]);
  }

  assert.equal(preparedContexts.length, cases.length);
  assert.equal(preparedContexts.filter(item => item.view === 'search').length, 2);
  assert.ok(preparedContexts.some(item => item.ids[0] === 'clip-1'));
  assert.ok(preparedContexts.some(item => item.ids[0] === 'playlist-1'));
  assert.ok(preparedContexts.some(item => item.ids[0] === 'video-1'));
});

test('entity-card actions and metadata compose in registration order', async () => {
  const extensions = loadEntityCardExtensions();
  const { actions, card, primaryMetadata, secondaryMetadata } = fakeCard();

  await extensions.decorateBatch({
    context: { view: 'search', layout: 'grid' },
    entries: [videoEntry(extensions, 'one', card)],
    plugins: [plugin('first'), plugin('second')],
    supports: () => true,
  });

  assert.deepEqual(actions.children.map(item => item.dataset.browserPluginId), ['first', 'second']);
  assert.deepEqual(primaryMetadata.children.map(item => item.dataset.browserPluginId), ['first', 'second']);
  assert.deepEqual(secondaryMetadata.children.map(item => item.dataset.browserPluginId), ['first', 'second']);
  assert.deepEqual(contributionLabels(actions), ['first:one:action', 'second:one:action']);
  assert.deepEqual(
    contributionLabels(primaryMetadata),
    ['first:one:primary', 'second:one:primary'],
  );
  assert.deepEqual(
    contributionLabels(secondaryMetadata),
    ['first:one:metadata', 'second:one:metadata'],
  );
});

test('preparation and per-card rendering failures stay isolated', async () => {
  const errors = [];
  const extensions = loadEntityCardExtensions(errors);
  const first = fakeCard();
  const second = fakeCard();
  const renderFailure = plugin('render_failure', {
    render: entity => {
      if (entity.id === 'one') throw new Error('one failed');
      return { actions: [labeledElement('second survived')], secondaryMetadata: [] };
    },
  });
  const preparationFailure = plugin('prepare_failure', {
    prepare: async () => { throw new Error('prepare failed'); },
  });

  await extensions.decorateBatch({
    context: { view: 'history' },
    entries: [
      videoEntry(extensions, 'one', first.card),
      videoEntry(extensions, 'two', second.card),
    ],
    plugins: [preparationFailure, renderFailure, plugin('healthy')],
    supports: () => true,
  });

  assert.deepEqual(contributionLabels(first.actions), ['healthy:one:action']);
  assert.deepEqual(
    contributionLabels(second.actions),
    ['second survived', 'healthy:two:action'],
  );
  assert.equal(errors.length, 2);
});

test('stale and superseded batches cannot append or duplicate contributions', async () => {
  const extensions = loadEntityCardExtensions();
  const target = fakeCard();
  let release;
  let current = true;
  const slowPlugin = plugin('slow', {
    prepare: () => new Promise(resolve => { release = resolve; }),
  });
  const stale = extensions.decorateBatch({
    context: { view: 'search' },
    entries: [videoEntry(extensions, 'one', target.card)],
    isCurrent: () => current,
    plugins: [slowPlugin],
    supports: () => true,
  });
  current = false;
  release({});
  await stale;
  assert.deepEqual(contributionLabels(target.actions), []);

  current = true;
  for (let run = 0; run < 2; run += 1) {
    await extensions.decorateBatch({
      context: { view: 'search' },
      entries: [videoEntry(extensions, 'one', target.card)],
      isCurrent: () => current,
      plugins: [plugin('repeat')],
      supports: () => true,
    });
  }
  assert.equal(target.actions.children.length, 1);
  assert.deepEqual(contributionLabels(target.actions), ['repeat:one:action']);
});

test('capability gating skips unavailable entity-card plugins', async () => {
  const extensions = loadEntityCardExtensions();
  const target = fakeCard();

  await extensions.decorateBatch({
    context: { view: 'search' },
    entries: [videoEntry(extensions, 'one', target.card)],
    plugins: [plugin('ready'), plugin('disabled'), plugin('missing_capability')],
    supports: (pluginId, capability) => pluginId === 'ready' && capability === 'ready_cards',
  });

  assert.deepEqual(contributionLabels(target.actions), ['ready:one:action']);
});

test('all native render entry points call the shared entity-card batch', () => {
  for (const view of ['search', 'playlist', 'video-detail', 'clip-detail']) {
    assert.match(indexSource, new RegExp(`decorateEntityCardBatch\\([\\s\\S]{0,500}'${view}'`));
  }
  assert.match(
    indexSource,
    /async function renderHistoryResults\(options\)[\s\S]{0,1800}decorateEntityCardBatch\([\s\S]{0,180}layoutContext/,
  );
  assert.match(
    indexSource,
    /async function renderHistoryView\(generation\)[\s\S]{0,600}layoutContext: 'history'/,
  );
  assert.match(
    indexSource,
    /const layoutContext = 'channel-history'[\s\S]{0,900}renderHistoryResults\([\s\S]{0,900}leadingEntries: \[channelEntry\]/,
  );
  assert.match(
    indexSource,
    /const layoutContext = 'channel-playlists'[\s\S]{0,900}decorateEntityCardBatch\(/,
  );
  assert.match(
    indexSource,
    /const layoutContext = 'channel-playlisted-videos'[\s\S]{0,900}decorateEntityCardBatch\(/,
  );
  assert.match(indexSource, /entityCardEntry\('channel', channel, channelCard\)/);
  assert.match(indexSource, /data-entity-card-slot="actions"/);
  assert.match(indexSource, /data-entity-card-slot="primaryMetadata"/);
  assert.match(indexSource, /data-entity-card-slot="secondaryMetadata"/);
});

test('shared card builders keep host slots in the same structural order for every layout', () => {
  assert.match(
    videoCardSource,
    /title-row[\s\S]*data-entity-card-slot="actions"/,
  );
  assert.ok(
    videoCardSource.indexOf('options.compactAvailabilityHtml')
      < videoCardSource.indexOf('data-entity-card-slot="primaryMetadata"'),
  );
  assert.ok(
    videoCardSource.indexOf('uploaderCategoryHtml(options.uploaderCategory)')
      < videoCardSource.indexOf('data-entity-card-slot="secondaryMetadata"'),
  );
  assert.ok(
    videoCardSource.indexOf('data-entity-card-slot="secondaryMetadata"')
      < videoCardSource.indexOf('options.descriptionHtml'),
  );
  assert.ok(
    collectionCardSource.indexOf('options.actionsHtml')
      < collectionCardSource.indexOf('data-entity-card-slot="actions"'),
  );
  assert.ok(
    collectionCardSource.indexOf('options.bodyHtml')
      < collectionCardSource.indexOf('data-entity-card-slot="primaryMetadata"'),
  );
  assert.ok(
    collectionCardSource.indexOf('data-entity-card-slot="primaryMetadata"')
      < collectionCardSource.indexOf('data-entity-card-slot="secondaryMetadata"'),
  );
  assert.ok(
    collectionCardSource.indexOf('data-entity-card-slot="secondaryMetadata"')
      < collectionCardSource.indexOf('options.tailHtml'),
  );
  assert.match(indexHtml, /\.search-grid\.layout-compact/);
  assert.match(indexHtml, /\.entity-card-slot:empty/);
});

test('plugin-specific browser presentation stays outside core templates', () => {
  assert.doesNotMatch(indexSource, /subtitle/i);
  assert.doesNotMatch(indexHtml, /subtitle/i);
});
