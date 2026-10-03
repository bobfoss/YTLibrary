const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../yt_library/templates/admin-plugin-packages.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const data = () => ({ token: 'nonce', busy: false, operation: null, unlisted: [], plugins: [{
  id: 'example', name: '<unsafe>', description: 'Test', repository_url: 'https://github.com/example/plugin',
  installed: null, latest: {version: '1.0', release_url: 'https://github.com/example/plugin/releases/tag/v1.0'},
  can_install: true,
}] });

function setup(fetch) {
  const root = {innerHTML: '', dataset: {}, querySelector: () => null, addEventListener: (_, fn) => {root.click = fn;}};
  const context = {document: {getElementById: () => root}, window: {dispatchEvent() {}}, Event: function () {},
    fetch, setTimeout: () => 1, clearTimeout() {}, confirm: () => true, alert: message => {throw new Error(message);}};
  vm.runInNewContext(source, context);
  return {root, context};
}

test('catalog text is escaped, controls render without any installed plugins', async () => {
  const {root} = setup(async () => ({ok: true, json: async () => data()}));
  await tick();
  assert.match(root.innerHTML, /&lt;unsafe&gt;/);
  assert.match(root.innerHTML, /data-action="install"/);
  assert.doesNotMatch(root.innerHTML, /<unsafe>/);
});

test('rapid refresh clicks issue only one mutation and carry nonce headers', async () => {
  let release;
  const waiting = new Promise(resolve => {release = resolve;});
  const posts = [];
  const {root} = setup(async (url, options) => {
    if (options.method === 'POST') { posts.push(options); await waiting; }
    return {ok: true, json: async () => data()};
  });
  await tick();
  const event = {target: {closest: () => ({dataset: {action: 'refresh'}, disabled: false})}};
  const first = root.click(event);
  await root.click(event);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].headers['X-YT-Library-Token'], 'nonce');
  assert.match(root.innerHTML, /data-action="refresh" disabled/);
  release();
  await first;
  assert.doesNotMatch(root.innerHTML, /data-action="refresh" disabled/);
});

test('accepted operation stays busy when restart interrupts the follow-up poll', async () => {
  let calls = 0;
  const {root, context} = setup(async () => {
    calls += 1;
    if (calls > 2) throw new Error('restarting');
    return {ok: true, json: async () => data()};
  });
  await tick();
  await context.window.YTLibraryPluginPackages.setEnabled('example', true);
  assert.match(root.innerHTML, /data-action="install" disabled/);
  await assert.rejects(context.window.YTLibraryPluginPackages.setEnabled('example', false), /Wait for the current/);
  assert.equal(calls, 3);
});

test('reloaded page restores persisted operation progress and disables actions', async () => {
  const status = {...data(), busy: true, operation: {id: 'saved', state: 'preparing', action: 'install', plugin_id: 'example', message: 'Verifying'}};
  const {root} = setup(async () => ({ok: true, json: async () => status}));
  await tick();
  assert.match(root.innerHTML, /Operation saved/);
  assert.match(root.innerHTML, /preparing/);
  assert.match(root.innerHTML, /data-action="install" disabled/);
});
