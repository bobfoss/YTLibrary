/* Catalog-backed package management; operation state survives service restarts. */
(() => {
  'use strict';
  const root = document.getElementById('pluginPackages');
  if (!root) return;
  let state = null;
  let pending = false;
  let timer = null;
  let completed = '';
  let rendered = '';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

  function render() {
    if (!state) return;
    const disabled = pending || state.busy;
    const operation = state.operation;
    const button = (id, action, label, allowed) => `<button type="button" data-plugin="${escape(id)}" data-action="${action}" ${disabled || !allowed ? 'disabled' : ''}>${label}</button>`;
    const cards = state.plugins.map(plugin => {
      const installed = plugin.installed;
      return `<article class="panel">
        <h3><a href="${escape(plugin.repository_url)}" target="_blank" rel="noopener noreferrer">${escape(plugin.name)}</a></h3>
        <p>${escape(plugin.description)}</p>
        <p class="metric">${installed ? `Installed ${escape(installed.version)} · ${escape(installed.mode)} · ${plugin.enabled ? 'Enabled' : 'Disabled'}` : 'Not installed'}${plugin.latest ? ` · Available ${escape(plugin.latest.version)}` : ''}</p>
        ${installed?.source ? `<p class="metric">Source: ${escape(installed.source)}</p>` : ''}
        ${plugin.config_path ? `<p class="metric">Config: ${escape(plugin.config_path)}</p>` : ''}
        ${plugin.runtime ? `<p class="metric">Runtime: ${escape(plugin.runtime.state)}${plugin.runtime.version ? ` · ${escape(plugin.runtime.version)}` : ''} ${escape(plugin.runtime.message || '')}</p>` : ''}
        ${plugin.reason ? `<p class="metric">${escape(plugin.reason)}</p>` : ''}
        ${installed ? button(plugin.id, 'update', 'Update', plugin.can_update) + ' ' +
          button(plugin.id, plugin.enabled ? 'disable' : 'enable', plugin.enabled ? 'Disable' : 'Enable', plugin.can_toggle) + ' ' +
          button(plugin.id, 'remove', 'Remove code', plugin.can_remove) : button(plugin.id, 'install', 'Install', plugin.can_install)}
        ${plugin.latest ? `<a href="${escape(plugin.latest.release_url)}" target="_blank" rel="noopener noreferrer">Release notes</a>` : ''}
      </article>`;
    }).join('');
    const html = `<h3>Install and manage plugins</h3>
      <p>Plugins run trusted Python code with YTL's permissions. Install leaves a plugin disabled; enable it separately when ready. Changes briefly restart YTL and preserve prior queue intent. Removing code keeps its configuration and data.</p>
      <button type="button" data-action="refresh" ${disabled ? 'disabled' : ''}>Refresh catalog</button>
      ${operation ? `<p role="status" aria-live="polite">${escape(operation.plugin_id)} · ${escape(operation.action)} · ${escape(operation.state)}: ${escape(operation.message)}<br>Operation ${escape(operation.id)}</p>` : ''}
      ${state.busy ? '<button type="button" data-action="recover">Reconnect maintenance controller</button>' : ''}
      <div class="plugin-package-grid">${cards}</div>
      ${state.unlisted.length ? `<p>Unlisted plugins remain manually managed: ${state.unlisted.map(p => escape(p.id)).join(', ')}.</p>` : ''}`;
    if (html !== rendered) { root.innerHTML = html; rendered = html; }
  }

  async function load() {
    clearTimeout(timer);
    try {
      const response = await fetch('/api/admin/plugin-packages', { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Package status unavailable');
      state = data;
      render();
      const op = state.operation;
      if (op?.state === 'succeeded' && completed !== op.id) {
        completed = op.id;
        window.dispatchEvent(new Event('ytl-plugins-changed'));
      }
    } catch (error) {
      // Keep existing controls/results visible while the service restarts.
      root.dataset.connection = error.message;
      const status = root.querySelector('[role="status"]');
      if (status) status.textContent = 'Waiting for YTL to reconnect. Operation progress is saved on disk.';
      else if (!state) root.textContent = error.message;
    } finally {
      timer = setTimeout(load, state?.busy ? 2000 : 15000);
    }
  }

  async function request(payload) {
    if (pending) return;
    if (!state) throw new Error('Wait for the package catalog to load');
    pending = true;
    render();
    try {
      const response = await fetch('/api/admin/plugin-packages', {
        method: 'POST', headers: { 'Content-Type': 'application/json',
          'X-YT-Library-Admin': '1', 'X-YT-Library-Token': state.token }, body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Package operation failed');
      if (payload.action !== 'refresh') state.busy = true;
      await load();
    } finally { pending = false; render(); }
  }

  async function change(id, action) {
    const plugin = state?.plugins.find(p => p.id === id);
    if (!plugin) throw new Error('This plugin is not in the managed catalog');
    if (pending || state.busy) throw new Error('Wait for the current package operation to finish');
    return request({ action, plugin_id: id, version: ['install', 'update'].includes(action) ? plugin.latest?.version || '' : '',
      expected_version: plugin.installed?.version || '' });
  }

  root.addEventListener('click', async event => {
    const button = event.target.closest('button[data-action]');
    if (!button || button.disabled || pending) return;
    const action = button.dataset.action;
    if (action === 'remove' && !confirm('Remove this plugin’s code? Its configuration and data will be retained. YTL will restart.')) return;
    if (action === 'install' && !confirm('Install trusted plugin code from the catalog? YTL will restart; the plugin will initially be disabled.')) return;
    try {
      if (action === 'refresh' || action === 'recover') await request({ action });
      else await change(button.dataset.plugin, action);
    } catch (error) { alert(error.message); }
  });
  window.YTLibraryPluginPackages = { setEnabled: (id, enabled) => change(id, enabled ? 'enable' : 'disable') };
  load();
})();
