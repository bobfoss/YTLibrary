const AdminTransport = window.YTLibraryAdminTransport;

const fields = {
  initializeControls: document.getElementById('initializeControls'),
  initializeLibrary: document.getElementById('initializeLibrary'),
  initializeStatus: document.getElementById('initializeStatus'),
  updateLibrary: document.getElementById('updateLibrary'),
  updateFrequency: document.getElementById('updateFrequency'),
  updateTimeLabel: document.getElementById('updateTimeLabel'),
  updateTime: document.getElementById('updateTime'),
  updateHourMinute: document.getElementById('updateHourMinute'),
  updateStatus: document.getElementById('updateStatus'),
  updateScheduleStatus: document.getElementById('updateScheduleStatus'),
  updateCookieStatus: document.getElementById('updateCookieStatus'),
  playlistRows: document.getElementById('playlistRows'),
  distinctVideos: document.getElementById('distinctVideos'),
  playlistBackfillCount: document.getElementById('playlistBackfillCount'),
  playlistIncompleteCount: document.getElementById('playlistIncompleteCount'),
  historyRows: document.getElementById('historyRows'),
  historyVideos: document.getElementById('historyVideos'),
  liveHistoryRows: document.getElementById('liveHistoryRows'),
  videoMetadataCounts: document.getElementById('videoMetadataCounts'),
  clipCounts: document.getElementById('clipCounts'),
  discoverClips: document.getElementById('discoverClips'),
  clipStatus: document.getElementById('clipStatus'),
  channelCounts: document.getElementById('channelCounts'),
  backfillVideoVisibility: document.getElementById('backfillVideoVisibility'),
  videoBackfillStatus: document.getElementById('videoBackfillStatus'),
  backfillPlaylistMetadata: document.getElementById('backfillPlaylistMetadata'),
  playlistBackfillStatus: document.getElementById('playlistBackfillStatus'),
  backfillChannelAccount: document.getElementById('backfillChannelAccount'),
  channelBackfillStatus: document.getElementById('channelBackfillStatus'),
  videoPluginProcesses: document.getElementById('videoPluginProcesses'),
  pluginWorkstreams: document.getElementById('pluginWorkstreams'),
  pluginPanel: document.getElementById('pluginPanel'),
  logs: document.getElementById('logs'),
  logPanel: document.getElementById('logPanel'),
  logSourceFilter: document.getElementById('logSourceFilter'),
  logLevelFilter: document.getElementById('logLevelFilter'),
  providedQueueTarget: document.getElementById('providedQueueTarget'),
  videoMetadataStaleDays: document.getElementById('videoMetadataStaleDays'),
  videoMetadataForce: document.getElementById('videoMetadataForce'),
  channelMetadataStaleDays: document.getElementById('channelMetadataStaleDays'),
  channelMetadataForce: document.getElementById('channelMetadataForce'),
  commonQueueCount: document.getElementById('commonQueueCount'),
  commonWorkerState: document.getElementById('commonWorkerState'),
  workerQueueElapsed: document.getElementById('workerQueueElapsed'),
  workerQueueEta: document.getElementById('workerQueueEta'),
  archivarixRequestsUtcDay: document.getElementById('archivarixRequestsUtcDay'),
  archivarixRequestsUtcRemaining: document.getElementById('archivarixRequestsUtcRemaining'),
  archivarixRequestsTotal: document.getElementById('archivarixRequestsTotal'),
  dispatchModeDelay: document.getElementById('dispatchModeDelay'),
  dispatchModeThrottle: document.getElementById('dispatchModeThrottle'),
  jobDispatchDelay: document.getElementById('jobDispatchDelay'),
  requestDelayMin: document.getElementById('requestDelayMin'),
  requestDelayMax: document.getElementById('requestDelayMax'),
  youtubeMaxInFlight: document.getElementById('youtubeMaxInFlight'),
  archivarixMaxInFlight: document.getElementById('archivarixMaxInFlight'),
  dispatchSettingsStatus: document.getElementById('dispatchSettingsStatus'),
  startWorkerQueue: document.getElementById('startWorkerQueue'),
  stopWorkerQueue: document.getElementById('stopWorkerQueue'),
  workerQueueRows: document.getElementById('workerQueueRows'),
  workerQueuePanel: document.getElementById('workerQueuePanel'),
  retryProxy: document.getElementById('retryProxy'),
  proxyBlock: document.getElementById('proxyBlock'),
  proxyBlockMessage: document.getElementById('proxyBlockMessage'),
  archivarixBlock: document.getElementById('archivarixBlock'),
  archivarixBlockMessage: document.getElementById('archivarixBlockMessage'),
  archivarixAutoRetry: document.getElementById('archivarixAutoRetry'),
  placeholderRunStatus: document.getElementById('placeholderRunStatus'),
  placeholderRunDetails: document.getElementById('placeholderRunDetails'),
  displayTimezone: document.getElementById('displayTimezone'),
  weekStartSunday: document.getElementById('weekStartSunday'),
  weekStartMonday: document.getElementById('weekStartMonday'),
  hideEmptyFilters: document.getElementById('hideEmptyFilters'),
  useProxy: document.getElementById('useProxy'),
  proxyUrl: document.getElementById('proxyUrl'),
  saveSettings: document.getElementById('saveSettings'),
  settingsStatus: document.getElementById('settingsStatus'),
  advancedToggle: document.getElementById('advancedToggle'),
  themeToggle: document.getElementById('themeToggle'),
  currentDateTime: document.getElementById('currentDateTime'),
  serviceUptime: document.getElementById('serviceUptime'),
  serviceStatus: document.getElementById('serviceStatus'),
  restartService: document.getElementById('restartService'),
  youtubeCookieStatus: document.getElementById('youtubeCookieStatus'),
  youtubeCookieText: document.getElementById('youtubeCookieText'),
  googleCookieStatus: document.getElementById('googleCookieStatus'),
  googleCookieText: document.getElementById('googleCookieText'),
  archivarixCookieStatus: document.getElementById('archivarixCookieStatus'),
  archivarixCookieText: document.getElementById('archivarixCookieText'),
};

for (let minute = 0; minute < 60; minute += 1) {
  const option = document.createElement('option');
  option.value = String(minute);
  option.textContent = `:${String(minute).padStart(2, '0')}`;
  fields.updateHourMinute.appendChild(option);
}

function syncUpdateScheduleControls() {
  const frequency = fields.updateFrequency.value;
  fields.updateTimeLabel.hidden = frequency === 'off';
  fields.updateTime.hidden = frequency !== 'daily';
  fields.updateHourMinute.hidden = frequency !== 'hourly';
}

function formatUpdateResult(result) {
  const inserted = Number(result?.inserted || 0).toLocaleString();
  const alreadyQueued = Number(
    result?.alreadyQueued ?? result?.already_queued ?? 0,
  ).toLocaleString();
  const prefix = result?.source === 'scheduled' ? 'Scheduled: queued' : 'Queued';
  const timestamp = result?.queuedAt ? `${fmtTime(result.queuedAt)}: ` : '';
  return `${timestamp}${prefix} ${inserted}; ${alreadyQueued} already queued`;
}
fields.displayTimezone.value = window.YTLibraryTime.timeZone || window.YTLibraryTime.detected();
fields.themeToggle.checked = window.YTLibraryTheme.current() === 'dark';

const queueState = {
  rows: [],
  rowsById: new Map(),
  total: 0,
  ready: false,
  renderPending: false,
};
const queueRowHeight = 72;
const queueOverscan = 10;
const runtimeStatusPollMs = 5000;
const statusPollMs = 30000;
const cookieFileStatuses = {};
let currentCookieAuthStatuses = {};
const runtimeStatusRequestTimeoutMs = 5000;
const statusRequestTimeoutMs = 15000;
const logRequestTimeoutMs = 20000;
const serverClockSyncIntervalMs = 60 * 60 * 1000;
let dispatchSettingsSaving = false;
let dispatchSettingsDirty = false;
let dispatchSettingsRevision = 0;
let dispatchSettingsSaveTimer = null;
let settingsSaving = false;
let settingsDirty = false;
let hideEmptyFiltersSaving = false;
let hideEmptyFiltersDirty = false;
let hideEmptyFiltersRevision = 0;
let savedHideEmptyFilters = true;
let archivarixAutoRetrySaving = false;
let archivarixAutoRetryDirty = false;
let archivarixAutoRetryRevision = 0;
let savedArchivarixAutoRetry = true;
let updateScheduleSaving = false;
let updateScheduleDirty = false;
let updateScheduleRevision = 0;
let updateScheduleSaveTimer = null;
let updateRequestPending = false;
let advancedSaving = false;
let advancedDirty = false;
let advancedRevision = 0;
let savedAdvanced = false;
let currentServicePid = 0;
let currentFeatureBackfillCounts = {};
const logState = {
  rows: [],
  keys: new Set(),
  total: 0,
  ready: false,
  generation: 0,
  loadingGeneration: null,
  requestController: null,
};
const logPageSize = 100;
const maxLiveLogRows = 500;

function escapeHtml(value) {
  return String(value || '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

function applyAdvancedMode(enabled) {
  document.body.classList.toggle('advanced-enabled', Boolean(enabled));
}

function selectAdvancedTab(kind) {
  for (const button of document.querySelectorAll('[data-advanced-tab]')) {
    const active = button.dataset.advancedTab === kind;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  }
  for (const pane of document.querySelectorAll('[data-advanced-pane]')) {
    pane.hidden = pane.dataset.advancedPane !== kind;
  }
}

function cookieFields(kind) {
  return {
    status: fields[`${kind}CookieStatus`],
    text: fields[`${kind}CookieText`],
  };
}

function renderCookieStatus(kind, status) {
  const target = cookieFields(kind).status;
  const matching = Number(status.matchingCookieCount || 0);
  const unexpired = Number(status.unexpiredMatchingCookieCount || 0);
  const modified = status.modifiedAt ? ` Updated ${fmtTime(status.modifiedAt)}.` : '';
  target.textContent = `${status.message || 'Cookie status unavailable'} ${matching} matching, ${unexpired} unexpired.${modified}`;
  target.classList.toggle('warn', !status.exists || !status.valid || unexpired === 0);
  target.title = status.configuredPath || '';
  cookieFileStatuses[kind] = status;
  renderUpdateCookieStatuses(currentCookieAuthStatuses);
}

function effectiveCookieStatus(kind, authStatus) {
  const fileStatus = cookieFileStatuses[kind] || {};
  if (fileStatus.exists === false) return { label: 'not configured', tone: 'warn', checkedAt: '', message: fileStatus.message || '' };
  if (fileStatus.exists && !fileStatus.valid) return { label: 'invalid file', tone: 'warn', checkedAt: '', message: fileStatus.message || '' };
  if (fileStatus.exists && Number(fileStatus.unexpiredMatchingCookieCount || 0) === 0) {
    return { label: 'expired', tone: 'warn', checkedAt: '', message: fileStatus.message || '' };
  }
  const checkedAt = String(authStatus?.checked_at || '');
  const modifiedAt = String(fileStatus.modifiedAt || '');
  if (
    !checkedAt
    || (modifiedAt && Date.parse(modifiedAt) > Date.parse(checkedAt))
  ) {
    return { label: 'not checked', tone: '', checkedAt: '', message: 'The current cookie file has not been used successfully yet.' };
  }
  const status = String(authStatus?.status || 'unknown');
  const labels = {
    valid: 'valid',
    expired: 'expired',
    rejected: 'rejected',
    missing: 'not configured',
    error: 'error',
  };
  return {
    label: labels[status] || 'not checked',
    tone: status === 'valid' ? 'valid' : (status === 'unknown' ? '' : 'warn'),
    checkedAt,
    message: String(authStatus?.message || ''),
  };
}

function renderUpdateCookieStatuses(statuses = {}) {
  currentCookieAuthStatuses = statuses || {};
  const labels = [
    ['youtube', 'YouTube'],
    ['google', 'My Activity'],
    ['archivarix', 'Archivarix'],
  ];
  const children = [document.createTextNode('Cookies:')];
  labels.forEach(([kind, label], index) => {
    const status = effectiveCookieStatus(kind, currentCookieAuthStatuses[kind] || {});
    const item = document.createElement('span');
    item.className = status.tone === 'valid'
      ? 'cookie-auth-valid'
      : (status.tone === 'warn' ? 'cookie-auth-warn' : '');
    item.textContent = `${label}: ${status.label}${status.checkedAt ? ` (${fmtTime(status.checkedAt)})` : ''}${index < labels.length - 1 ? ';' : ''}`;
    item.title = status.message;
    children.push(item);
  });
  fields.updateCookieStatus.replaceChildren(...children);
}

async function loadCookieStatuses() {
  const response = await fetch('/api/admin/cookies/status', { cache: 'no-store' });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Cookie status failed: ${response.status}`);
  for (const kind of ['youtube', 'google', 'archivarix']) {
    renderCookieStatus(kind, payload.cookies?.[kind] || {});
  }
}

async function saveCookieFile(kind, button) {
  const editor = cookieFields(kind);
  const value = editor.text.value;
  if (!value.trim()) {
    alert('Paste a Netscape cookie export first.');
    return;
  }
  button.disabled = true;
  editor.status.textContent = 'Validating and saving';
  try {
    const response = await fetch(`/api/admin/cookies/${encodeURIComponent(kind)}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'text/plain; charset=utf-8',
        'X-YT-Library-Admin': '1',
      },
      body: value,
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `Cookie update failed: ${response.status}`);
    editor.text.value = '';
    renderCookieStatus(kind, payload.status || {});
  } catch (error) {
    editor.status.textContent = `Not saved: ${error.message}`;
    editor.status.classList.add('warn');
  } finally {
    button.disabled = false;
  }
}

function syncDispatchModeInputs() {
  const throttling = fields.dispatchModeThrottle.checked;
  fields.jobDispatchDelay.disabled = throttling;
  fields.requestDelayMin.disabled = !throttling;
  fields.requestDelayMax.disabled = !throttling;
}

function fmtTime(value) {
  return window.YTLibraryTime.format(value);
}

const serverClock = {
  epochMs: null,
  monotonicAtSync: 0,
};
let serviceStartedAtEpochMs = null;

function syncServerClock(serverTime, requestStartedAt, responseReceivedAt = performance.now()) {
  const serverEpochMs = Date.parse(serverTime || '');
  if (!Number.isFinite(serverEpochMs)) return false;
  if (
    Number.isFinite(serverClock.epochMs)
    && responseReceivedAt - serverClock.monotonicAtSync < serverClockSyncIntervalMs
  ) {
    return false;
  }
  const halfRoundTripMs = Math.max(0, responseReceivedAt - requestStartedAt) / 2;
  serverClock.epochMs = serverEpochMs + halfRoundTripMs;
  serverClock.monotonicAtSync = responseReceivedAt;
  updateCurrentDateTime(responseReceivedAt);
  return true;
}

function currentServerTime(nowMonotonic = performance.now()) {
  if (!Number.isFinite(serverClock.epochMs)) return null;
  return new Date(serverClock.epochMs + Math.max(0, nowMonotonic - serverClock.monotonicAtSync));
}

function formatServiceUptime(durationMs) {
  const totalSeconds = Math.max(0, Math.floor(Number(durationMs || 0) / 1000));
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor((totalSeconds % 86400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const clock = [hours, minutes, seconds].map(value => String(value).padStart(2, '0')).join(':');
  return days ? `${days}d ${clock}` : clock;
}

function updateServiceUptime(nowMonotonic = performance.now()) {
  if (!Number.isFinite(serviceStartedAtEpochMs)) {
    fields.serviceUptime.textContent = '';
    return;
  }
  const now = currentServerTime(nowMonotonic);
  fields.serviceUptime.textContent = now
    ? `Uptime: ${formatServiceUptime(now.getTime() - serviceStartedAtEpochMs)}`
    : '';
}

function updateCurrentDateTime(nowMonotonic = performance.now()) {
  const now = currentServerTime(nowMonotonic);
  fields.currentDateTime.textContent = now ? fmtTime(now) : '';
  updateServiceUptime(nowMonotonic);
}

function fmtClockDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds || 0)));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainingSeconds = total % 60;
  return [hours, minutes, remainingSeconds].map(value => String(value).padStart(2, '0')).join(':');
}

function fmtUtcWindowRemaining(windowEndsAt, nowMs = Date.now()) {
  const windowEndMs = Date.parse(windowEndsAt || '');
  if (!Number.isFinite(windowEndMs)) return '';
  const totalMinutes = Math.max(0, Math.ceil((windowEndMs - nowMs) / 60000));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`;
}

const queueTiming = {
  active: false,
  syncedAt: 0,
  elapsedAtSync: 0,
  etaAtSync: 0,
  etaAvailable: false,
};

function syncQueueTiming(active, stats) {
  queueTiming.active = Boolean(active);
  queueTiming.syncedAt = performance.now();
  queueTiming.elapsedAtSync = Number(stats?.elapsed_seconds || 0);
  queueTiming.etaAtSync = Number(stats?.eta_seconds || 0);
  queueTiming.etaAvailable = Boolean(stats?.eta_available);
  updateQueueTimingDisplay();
}

function updateQueueTimingDisplay() {
  if (!queueTiming.active) {
    fields.workerQueueElapsed.textContent = '-';
    fields.workerQueueEta.textContent = '-';
    fields.workerQueueElapsed.className = 'value';
    fields.workerQueueEta.className = 'value';
    return;
  }
  const localSeconds = Math.max(0, (performance.now() - queueTiming.syncedAt) / 1000);
  fields.workerQueueElapsed.textContent = fmtClockDuration(queueTiming.elapsedAtSync + localSeconds);
  fields.workerQueueEta.textContent = queueTiming.etaAvailable
    ? fmtClockDuration(Math.max(0, queueTiming.etaAtSync - localSeconds))
    : 'calculating';
  fields.workerQueueElapsed.className = 'value running';
  fields.workerQueueEta.className = 'value running';
}

function workerSubject(row) {
  if (row.worker_type === 'clip') {
    return [row.known_clip_title, row.current_title]
      .find(value => value && value !== row.clip_id) || '';
  }
  if (row.video_id && ['metadata', 'placeholder'].includes(row.worker_type)) {
    return row.current_title && row.current_title !== row.video_id ? row.current_title : '';
  }
  if (row.worker_type === 'metadata' && row.task_type === 'channel') {
    const identifier = workerId(row);
    return [row.known_channel_title, row.channel_title, row.current_title]
      .find(value => value && value !== identifier) || '';
  }
  return row.playlist_title || row.current_title || row.video_id || row.channel_title || row.channel_id || row.playlist_id || row.subject_key || '';
}

function playlistSubject(playlistId) {
  return playlistId === 'LL' ? 'Liked videos' : (playlistId || '');
}

function normalizedLogs(data) {
  return [
    ...(data.playlistScanLogs || []).map(log => ({
      ...log,
      stream: 'playlistScanLogs',
      subject_id: log.subject_title || playlistSubject(log.playlist_id),
      identifier: log.display_id || log.playlist_id || '',
      source: 'playlist',
    })),
    ...(data.metadataLogs || []).map(log => ({
      ...log,
      stream: 'metadataLogs',
      subject_id: log.subject_title || (log.display_id ? '' : log.video_id),
      identifier: log.display_id || '',
      source: String(log.level || '').startsWith('queue ')
        ? 'queue'
        : (String(log.level || '').startsWith('placeholder ')
          ? 'placeholder'
          : (String(log.level || '').startsWith('clip ') ? 'clip' : 'metadata')),
      level: String(log.level || '').replace(/^(?:queue|placeholder|clip)\s+/, ''),
    })),
    ...(data.placeholderRecoveryLogs || []).map(log => ({
      ...log,
      stream: 'placeholderRecoveryLogs',
      subject_id: log.subject_title || log.video_id,
      identifier: log.display_id || '',
      source: 'placeholder',
    })),
    ...(data.liveHistoryLogs || []).map(log => ({
      ...log,
      stream: 'liveHistoryLogs',
      subject_id: log.subject_title || log.video_id,
      identifier: log.display_id || '',
      source: 'history',
    })),
    ...(data.pluginWorkerLogs || []).map(log => ({
      ...log,
      stream: 'pluginWorkerLogs',
      subject_id: log.subject_title || log.subject_id || '',
      identifier: log.display_id || '',
      source: `plugin:${log.plugin_id || ''}`,
    })),
  ];
}

function renderLogs() {
  fields.logs.innerHTML = logState.rows.length ? logState.rows.map(log => `
    <tr>
      <td class="time-cell log-time-cell">${fmtTime(log.created_at)}</td>
      <td class="level-cell log-level-cell">${escapeHtml(log.source)} ${escapeHtml(log.level)}</td>
      <td class="time-cell log-id-cell">${escapeHtml(log.identifier || '')}</td>
      <td class="message log-subject-cell">${escapeHtml(log.subject_id || '')}</td>
      <td class="message log-message-cell">${escapeHtml(displayLogMessage(log))}</td>
    </tr>
  `).join('') : (logState.ready
    ? '<tr><td colspan="5" class="message log-empty-cell">No log entries match these filters.</td></tr>'
    : '<tr><td colspan="5" class="message log-empty-cell">Loading log entries...</td></tr>');
}

function logSeverity(level) {
  const tokens = String(level || '').toLowerCase().split(/\s+/);
  if (tokens.includes('error')) return 'error';
  if (tokens.includes('warn') || tokens.includes('warning')) return 'warn';
  if (tokens.includes('debug')) return 'debug';
  return 'info';
}

const logSeverityRanks = Object.freeze({ info: 0, warn: 1, error: 2, debug: 3 });

function logMatchesLevel(log, selectedLevel = selectedLogLevel()) {
  const logRank = logSeverityRanks[logSeverity(log.level)] ?? logSeverityRanks.info;
  const selectedRank = logSeverityRanks[selectedLevel] ?? logSeverityRanks.error;
  return logRank <= selectedRank;
}

function displayLogMessage(log) {
  const message = String(log.message || '');
  if (
    log.source === 'placeholder'
    && ['found', 'not found', 'thumbnail'].includes(String(log.level || '').toLowerCase())
  ) {
    return `Archivarix request: ${message}`;
  }
  if (log.source !== 'metadata' || log.level !== 'channel' || !log.identifier) return message;
  const redundantSuffix = ` (via ${log.identifier})`;
  return message.endsWith(redundantSuffix)
    ? message.slice(0, -redundantSuffix.length)
    : message;
}

function selectedLogSource() {
  return fields.logSourceFilter.value || 'all';
}

function selectedLogLevel() {
  return fields.logLevelFilter.value || 'error';
}

function logMatchesSelection(log) {
  return (selectedLogSource() === 'all' || log.source === selectedLogSource())
    && logMatchesLevel(log);
}

function logKey(log) {
  return `${log.stream || log.source}:${log.id}`;
}

function compareLogs(left, right) {
  return String(right.created_at || '').localeCompare(String(left.created_at || ''))
    || String(right.stream || '').localeCompare(String(left.stream || ''))
    || ((right.id || 0) - (left.id || 0));
}

function addLogRows(rows) {
  let added = 0;
  for (const log of rows) {
    const key = logKey(log);
    if (logState.keys.has(key)) continue;
    logState.rows.push(log);
    logState.keys.add(key);
    added += 1;
  }
  logState.rows.sort(compareLogs);
  return added;
}

function trimLiveLogRows() {
  if (fields.logPanel.scrollTop > 40 || logState.rows.length <= maxLiveLogRows) return;
  const removed = logState.rows.splice(maxLiveLogRows);
  for (const log of removed) logState.keys.delete(logKey(log));
}

function applyLogs(data) {
  const oldHeight = fields.logPanel.scrollHeight;
  const preserveScroll = fields.logPanel.scrollTop > 0;
  const added = addLogRows(normalizedLogs(data).filter(logMatchesSelection));
  logState.total += added;
  trimLiveLogRows();
  renderLogs();
  if (preserveScroll && added) {
    requestAnimationFrame(() => {
      fields.logPanel.scrollTop += fields.logPanel.scrollHeight - oldHeight;
    });
  }
}

function resetLogState() {
  logState.requestController?.abort();
  logState.requestController = null;
  logState.generation += 1;
  logState.rows = [];
  logState.keys.clear();
  logState.total = 0;
  logState.ready = false;
  logState.loadingGeneration = null;
  fields.logPanel.scrollTop = 0;
  renderLogs();
}

async function loadLogPage(reset = false) {
  if (reset) resetLogState();
  const generation = logState.generation;
  if (logState.loadingGeneration === generation) return;
  if (logState.ready && logState.rows.length >= logState.total) return;
  logState.loadingGeneration = generation;
  const params = new URLSearchParams({
    limit: String(logPageSize),
    offset: String(logState.rows.length),
    source: selectedLogSource(),
    level: selectedLogLevel(),
  });
  const controller = new AbortController();
  logState.requestController = controller;
  try {
    const payload = await fetchStatusJson(
      `/api/admin/logs?${params}`,
      controller,
      logRequestTimeoutMs,
    );
    if (generation !== logState.generation) return;
    addLogRows(payload.rows || []);
    logState.total = Math.max(Number(payload.total || 0), logState.rows.length);
    logState.ready = true;
    renderLogs();
  } catch (error) {
    if (error?.name !== 'AbortError') throw error;
  } finally {
    if (logState.loadingGeneration === generation) logState.loadingGeneration = null;
    if (logState.requestController === controller) logState.requestController = null;
  }
}

function loadMoreLogsIfNeeded() {
  const remaining = fields.logPanel.scrollHeight
    - fields.logPanel.scrollTop
    - fields.logPanel.clientHeight;
  if (remaining < 180) loadLogPage().catch(error => { fields.logs.title = error.message; });
}

function workerDetails(row) {
  if (row.worker_type === 'playlist') {
    return `${row.playlist_id || ''}\n${row.scan_status || 'unscanned'} ${row.video_count || 0}/${row.playlist_video_count || '?'}`;
  }
  if (row.worker_type === 'metadata') {
    return '';
  }
  if (row.worker_type === 'placeholder') {
    return '';
  }
  return row.task_type || '';
}

function workerLabel(row) {
  if (row.worker_type === 'plugin') {
    return [row.source_key || 'plugin', row.task_type || '', row.manual ? 'manual' : '']
      .filter(Boolean)
      .join(' ');
  }
  return [row.worker_type || '', row.task_type || '', row.manual ? 'manual' : ''].filter(Boolean).join(' ');
}

function workerId(row) {
  if (row.worker_type === 'clip') {
    return row.clip_id || '';
  }
  if (row.worker_type === 'metadata' && row.task_type === 'channel') {
    return row.channel_id || row.video_id || '';
  }
  if (row.worker_type === 'playlist') {
    return row.playlist_id || row.video_id || '';
  }
  return row.video_id || row.channel_id || row.playlist_id || '';
}

function queueRowHtml(row) {
  return `
    <tr data-queue-id="${escapeHtml(row.queue_id || '')}">
      <td><button class="remove-queue-entry" type="button" data-queue-id="${escapeHtml(row.queue_id || '')}">Remove</button></td>
      <td class="queue-worker-cell">${escapeHtml(workerLabel(row))}</td>
      <td class="time-cell"><div class="queue-cell">${escapeHtml(workerId(row))}</div></td>
      <td class="message"><div class="queue-cell">${escapeHtml(workerSubject(row))}</div></td>
      <td class="message"><div class="queue-cell">${escapeHtml(row.source_key || '')}</div></td>
      <td class="message"><div class="queue-cell">${escapeHtml(workerDetails(row))}</div></td>
    </tr>
  `;
}

function createQueueRow(row) {
  const tbody = document.createElement('tbody');
  tbody.innerHTML = queueRowHtml(row).trim();
  return tbody.firstElementChild;
}

function compareQueueRows(left, right) {
  return (Number(left.priority || 0) - Number(right.priority || 0))
    || String(right.updated_at || '').localeCompare(String(left.updated_at || ''))
    || (Number(right.queue_id || 0) - Number(left.queue_id || 0));
}

function queueRowIndex(row) {
  let low = 0;
  let high = queueState.rows.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (compareQueueRows(queueState.rows[middle], row) < 0) low = middle + 1;
    else high = middle;
  }
  return low;
}

function removeQueueRow(queueId) {
  const id = Number(queueId);
  const existing = queueState.rowsById.get(id);
  if (!existing) return;
  const index = queueState.rows.findIndex(row => Number(row.queue_id) === id);
  if (index >= 0) queueState.rows.splice(index, 1);
  queueState.rowsById.delete(id);
}

function insertQueueRow(row) {
  const id = Number(row.queue_id || 0);
  if (!id) return;
  removeQueueRow(id);
  queueState.rows.splice(queueRowIndex(row), 0, row);
  queueState.rowsById.set(id, row);
}

function scheduleQueueRender() {
  if (queueState.renderPending) return;
  queueState.renderPending = true;
  requestAnimationFrame(() => {
    queueState.renderPending = false;
    renderQueueWindow();
  });
}

function renderQueueWindow() {
  if (!queueState.rows.length) {
    fields.workerQueueRows.innerHTML = queueState.ready
      ? '<tr class="empty-queue-row"><td colspan="6" class="message">No worker jobs queued.</td></tr>'
      : '';
    return;
  }
  const visibleRows = Math.ceil(fields.workerQueuePanel.clientHeight / queueRowHeight);
  const start = Math.max(0, Math.floor(fields.workerQueuePanel.scrollTop / queueRowHeight) - queueOverscan);
  const end = Math.min(queueState.rows.length, start + visibleRows + queueOverscan * 2);
  const fragment = document.createDocumentFragment();
  if (start > 0) {
    const spacer = document.createElement('tr');
    spacer.className = 'queue-spacer';
    spacer.innerHTML = `<td colspan="6" style="height:${start * queueRowHeight}px"></td>`;
    fragment.appendChild(spacer);
  }
  for (const row of queueState.rows.slice(start, end)) fragment.appendChild(createQueueRow(row));
  if (end < queueState.rows.length) {
    const spacer = document.createElement('tr');
    spacer.className = 'queue-spacer';
    spacer.innerHTML = `<td colspan="6" style="height:${(queueState.rows.length - end) * queueRowHeight}px"></td>`;
    fragment.appendChild(spacer);
  }
  fields.workerQueueRows.replaceChildren(fragment);
}

function resetQueueState(total = 0) {
  queueState.rows = [];
  queueState.rowsById.clear();
  queueState.total = Number(total || 0);
  queueState.ready = false;
  fields.workerQueueRows.replaceChildren();
  fields.commonQueueCount.textContent = queueState.total;
}

function applyQueueSnapshot(rows, total) {
  for (const row of rows) {
    const id = Number(row.queue_id || 0);
    if (!id || queueState.rowsById.has(id)) continue;
    queueState.rows.push(row);
    queueState.rowsById.set(id, row);
  }
  queueState.total = Number(total || queueState.rowsById.size);
  fields.commonQueueCount.textContent = queueState.total;
  scheduleQueueRender();
}

function applyQueueDelta(payload) {
  for (const queueId of payload.removals || []) removeQueueRow(queueId);
  for (const row of payload.upserts || []) insertQueueRow(row);
  queueState.total = Number(payload.total ?? queueState.rowsById.size);
  fields.commonQueueCount.textContent = queueState.total;
  scheduleQueueRender();
}

function syncPluginLogSources(plugins) {
  const selected = fields.logSourceFilter.value || 'all';
  for (const option of [...fields.logSourceFilter.querySelectorAll('option[data-plugin-source]')]) {
    option.remove();
  }
  for (const plugin of plugins) {
    if (!(plugin.workerProcesses || []).length) continue;
    const option = document.createElement('option');
    option.value = `plugin:${plugin.id}`;
    option.textContent = plugin.name || plugin.id;
    option.dataset.pluginSource = plugin.id;
    fields.logSourceFilter.appendChild(option);
  }
  if ([...fields.logSourceFilter.options].some(option => option.value === selected)) {
    fields.logSourceFilter.value = selected;
  }
}

function pluginProcessDetails(process) {
  const latest = process.latestRun || {};
  const queued = Number(process.queuedCount || 0);
  const running = Number(process.runningCount || 0);
  return [
    queued ? `${queued.toLocaleString()} queued` : 'Nothing queued',
    running ? `${running.toLocaleString()} running` : '',
    latest.outcome ? `Last: ${latest.outcome}` : '',
    latest.finished_at ? fmtTime(latest.finished_at) : '',
  ].filter(Boolean).join(' | ');
}

function formatByteCount(value) {
  let amount = Math.max(0, Number(value) || 0);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let unitIndex = 0;
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024;
    unitIndex += 1;
  }
  const maximumFractionDigits = unitIndex > 0 && amount < 10 ? 1 : 0;
  return `${amount.toLocaleString(undefined, { maximumFractionDigits })} ${units[unitIndex]}`;
}

function pluginAdminMetricsHtml(plugin) {
  const metrics = plugin.adminMetrics || [];
  if (!metrics.length) return '';
  return `
    <div class="grid plugin-metrics advanced-only">
      ${metrics.map(metric => `
        <div class="panel" data-plugin-metric="${escapeHtml(metric.id)}"
             ${metric.description ? `title="${escapeHtml(metric.description)}"` : ''}>
          <div class="metric">${escapeHtml(metric.label)}</div>
          <div class="value">${metric.format === 'bytes'
            ? formatByteCount(metric.value)
            : Number(metric.value || 0).toLocaleString()}</div>
        </div>
      `).join('')}
    </div>
  `;
}

function pluginProcessActionHtml(plugin, process, action, { includeSurface = false } = {}) {
  const ready = plugin.state === 'ready';
  const inputs = (action.inputs || []).map(input => `
    <label>${escapeHtml(input.label || input.name)}
      <input class="plugin-process-input" type="text"
             data-plugin-param="${escapeHtml(input.name)}"
             placeholder="${escapeHtml(input.placeholder || '')}"
             maxlength="${Number(input.maxLength || 500)}"
             autocomplete="off" spellcheck="false"
             ${input.required ? 'required' : ''}>
    </label>
  `).join('');
  return `
    <form class="plugin-process-action controls ${includeSurface && action.surface === 'advanced' ? 'advanced-only' : ''}"
          data-plugin-id="${escapeHtml(plugin.id)}"
          data-plugin-worker-id="${escapeHtml(process.id)}"
          data-plugin-action-id="${escapeHtml(action.id || 'default')}"
          data-confirm="${escapeHtml(action.confirm || '')}"
          aria-label="${escapeHtml(`${plugin.name || plugin.id} ${action.buttonLabel || process.name}`)}">
      ${inputs}
      <button class="plugin-process-enqueue primary" type="submit"
              ${ready ? '' : 'disabled'}>${escapeHtml(action.buttonLabel || process.name)}</button>
      <span class="metric plugin-process-status" aria-live="polite">${escapeHtml(ready ? pluginProcessDetails(process) : (plugin.message || plugin.state || 'Unavailable'))}</span>
    </form>
  `;
}

function pluginEnabledControlHtml(plugin) {
  const name = plugin.name || plugin.id;
  return `
    <div class="plugin-enabled-control advanced-only">
      <span>Enabled</span>
      <label class="theme-switch">
        <input class="plugin-enabled-toggle" type="checkbox"
               data-plugin-id="${escapeHtml(plugin.id)}"
               aria-label="Enable ${escapeHtml(name)}"
               ${plugin.enabled ? 'checked' : ''}>
        <span class="theme-track" aria-hidden="true"><span class="theme-thumb"></span></span>
      </label>
      <span class="metric plugin-enabled-status" aria-live="polite"></span>
    </div>
  `;
}

function renderPluginWorkstreams(plugins) {
  syncPluginLogSources(plugins);
  const sections = [];
  const videoActions = [];
  let hasBasicPlugin = false;
  for (const plugin of plugins) {
    const pluginBlocks = [];
    let hasBasicAction = false;
    for (const process of plugin.workerProcesses || []) {
      for (const action of process.adminActions || []) {
        if (action.placement === 'videos') {
          videoActions.push(pluginProcessActionHtml(plugin, process, action, { includeSurface: true }));
          continue;
        }
        if (action.placement !== 'plugin') continue;
        hasBasicAction ||= action.surface !== 'advanced';
        pluginBlocks.push(`
          <div class="plugin-process-block ${action.surface === 'advanced' ? 'advanced-only' : ''}"
               data-plugin-worker-id="${escapeHtml(process.id)}">
            ${action.description ? `<p class="message">${escapeHtml(action.description)}</p>` : ''}
            ${pluginProcessActionHtml(plugin, process, action)}
          </div>
        `);
      }
    }
    hasBasicPlugin ||= hasBasicAction;
    sections.push(`
      <section class="plugin-workstream ${hasBasicAction ? '' : 'advanced-only'}"
               data-plugin-id="${escapeHtml(plugin.id)}">
        <div class="plugin-workstream-header">
          <h3>${escapeHtml(plugin.name || plugin.id)}</h3>
          ${pluginEnabledControlHtml(plugin)}
        </div>
        ${pluginAdminMetricsHtml(plugin)}
        ${pluginBlocks.join('') || `<p class="message">${escapeHtml(plugin.message || plugin.state || 'Unavailable')}</p>`}
      </section>
    `);
  }
  fields.pluginPanel.hidden = false;
  fields.pluginPanel.classList.toggle('advanced-only', !hasBasicPlugin);
  fields.pluginWorkstreams.innerHTML = sections.join('');
  fields.videoPluginProcesses.innerHTML = videoActions.join('');
}

function renderRuntimeStatus(data) {
  const service = data.service || {};
  const serviceState = String(service.status || 'unknown');
  currentServicePid = Number(service.pid || currentServicePid || 0);
  fields.serviceStatus.textContent = serviceState === 'running'
    ? `Running${service.pid ? ` (${service.pid})` : ''}`
    : (serviceState === 'restarting' ? 'Restarting' : 'Unavailable');
  fields.serviceStatus.className = serviceState === 'running' ? 'running' : 'warn';
  fields.serviceStatus.title = [
    service.pid ? `PID ${service.pid}` : '',
    service.startedAt ? `Started ${fmtTime(service.startedAt)}` : '',
  ].filter(Boolean).join(' | ');
  const parsedStartedAt = Date.parse(service.startedAt || '');
  serviceStartedAtEpochMs = Number.isFinite(parsedStartedAt) ? parsedStartedAt : null;
  updateServiceUptime();
  fields.restartService.disabled = serviceState === 'restarting';
  renderUpdateCookieStatuses(data.cookieAuthStatuses || {});

  const queueWorkerRunning = Boolean(data.workerQueueRunning);
  const queueWorkerStopping = Boolean(data.workerQueueStopping);
  const queueWorkerActive = queueWorkerRunning || queueWorkerStopping;
  const queueStats = data.workerQueueStats || {};
  fields.commonQueueCount.textContent = Number(data.workerQueueCount ?? 0);
  fields.commonWorkerState.textContent = queueWorkerStopping ? 'stopping' : (queueWorkerRunning ? 'running' : 'idle');
  fields.commonWorkerState.className = `value ${queueWorkerActive ? 'running' : ''}`;
  syncQueueTiming(queueWorkerActive, queueStats);
  fields.startWorkerQueue.disabled = queueWorkerActive;
  fields.stopWorkerQueue.disabled = !queueWorkerRunning || queueWorkerStopping;
  fields.startWorkerQueue.classList.toggle('primary', !queueWorkerActive);
  fields.stopWorkerQueue.classList.toggle('danger', queueWorkerActive);

  const proxyBlock = data.proxyBlock || {};
  fields.retryProxy.hidden = !proxyBlock.blocked;
  fields.proxyBlock.hidden = !proxyBlock.blocked;
  fields.proxyBlockMessage.textContent = proxyBlock.blocked
    ? `${proxyBlock.message || 'The configured proxy is unavailable.'}${proxyBlock.blocked_at ? ` Blocked ${window.YTLibraryTime.format(proxyBlock.blocked_at)}.` : ''}`
    : '';
  const archivarixBlock = data.archivarixBlock || {};
  fields.archivarixBlock.hidden = !archivarixBlock.blocked;
  fields.archivarixBlockMessage.textContent = archivarixBlock.blocked
    ? `${archivarixBlock.message || 'Recovery is blocked.'}${archivarixBlock.blocked_at ? ` Blocked ${window.YTLibraryTime.format(archivarixBlock.blocked_at)}.` : ''}`
    : '';
}

function render(data) {
  renderPluginWorkstreams(data.plugins || []);

  const hasLibraryData = Boolean(data.hasLibraryData);
  fields.initializeControls.classList.toggle('initialization-complete', hasLibraryData);
  fields.initializeLibrary.classList.toggle('initialize-needed', !hasLibraryData);
  fields.initializeLibrary.classList.toggle('initialize-complete', hasLibraryData);
  fields.initializeLibrary.title = hasLibraryData
    ? 'Run a complete library scan'
    : 'No library data detected; run the initial scan';

  const settings = data.settings || {};
  if (!advancedSaving && !advancedDirty) {
    savedAdvanced = Boolean(settings.adminAdvanced);
    fields.advancedToggle.checked = savedAdvanced;
    applyAdvancedMode(savedAdvanced);
  }
  if (!settingsSaving && !settingsDirty) {
    fields.displayTimezone.value = settings.displayTimezone || window.YTLibraryTime.detected();
    fields.weekStartMonday.checked = settings.weekStart === 'monday';
    fields.weekStartSunday.checked = !fields.weekStartMonday.checked;
    fields.useProxy.checked = Boolean(settings.useProxy);
    fields.proxyUrl.value = settings.proxy || '';
  }
  if (!hideEmptyFiltersSaving && !hideEmptyFiltersDirty) {
    savedHideEmptyFilters = settings.hideEmptyFilters !== false;
    fields.hideEmptyFilters.checked = savedHideEmptyFilters;
  }
  if (!archivarixAutoRetrySaving && !archivarixAutoRetryDirty) {
    savedArchivarixAutoRetry = settings.archivarixAutoRetry !== false;
    fields.archivarixAutoRetry.checked = savedArchivarixAutoRetry;
  }
  if (!updateScheduleSaving && !updateScheduleDirty) {
    fields.updateFrequency.value = settings.updateFrequency || 'off';
    fields.updateTime.value = settings.updateTime || '03:00';
    fields.updateHourMinute.value = String(settings.updateHourMinute ?? 0);
    syncUpdateScheduleControls();
    const updateSchedule = settings.updateSchedule || {};
    if (!updateRequestPending) {
      fields.updateStatus.textContent = updateSchedule.lastResult?.queuedAt
        ? formatUpdateResult(updateSchedule.lastResult)
        : '';
    }
    fields.updateScheduleStatus.textContent = updateSchedule.lastError
      ? `Error: ${updateSchedule.lastError}`
      : (updateSchedule.enabled && updateSchedule.nextRunAt
        ? `Next ${fmtTime(updateSchedule.nextRunAt)}`
        : '');
    fields.updateScheduleStatus.className = `metric ${updateSchedule.lastError ? 'warn' : ''}`;
  }

  fields.playlistRows.textContent = data.playlistCounts.total_playlists || 0;
  fields.distinctVideos.textContent = data.counts.distinct_playlist_item_videos || 0;
  fields.historyRows.textContent = data.counts.history_rows || 0;
  fields.historyVideos.textContent = data.counts.distinct_history_videos || 0;
  fields.liveHistoryRows.textContent = data.liveHistoryCounts?.live_rows || 0;
  const archivarixRequestCounts = data.archivarixRequestCounts || {};
  fields.archivarixRequestsUtcDay.textContent = archivarixRequestCounts.current_utc_day || 0;
  const archivarixRemaining = fmtUtcWindowRemaining(archivarixRequestCounts.window_ends_at);
  fields.archivarixRequestsUtcRemaining.textContent = archivarixRemaining
    ? `(${archivarixRemaining} remaining)`
    : '';
  fields.archivarixRequestsTotal.textContent = archivarixRequestCounts.total || 0;
  const archivarixWindowTitle = archivarixRequestCounts.window_ends_at
    ? `Resets ${fmtTime(archivarixRequestCounts.window_ends_at)} (00:00 UTC)`
    : '';
  const archivarixLatestTitle = archivarixRequestCounts.latest_at
    ? `Latest request ${fmtTime(archivarixRequestCounts.latest_at)}`
    : 'No tracked requests';
  fields.archivarixRequestsUtcDay.title = [archivarixWindowTitle, archivarixLatestTitle]
    .filter(Boolean)
    .join('. ');
  fields.archivarixRequestsTotal.title = archivarixLatestTitle;
  const dispatchSettings = data.dispatchSettings || {};
  if (!dispatchSettingsSaving && !dispatchSettingsDirty) {
    const dispatchMode = dispatchSettings.dispatch_mode === 'throttle' ? 'throttle' : 'delay';
    fields.dispatchModeDelay.checked = dispatchMode === 'delay';
    fields.dispatchModeThrottle.checked = dispatchMode === 'throttle';
    if (document.activeElement !== fields.jobDispatchDelay) {
      fields.jobDispatchDelay.value = Number(dispatchSettings.job_dispatch_delay_seconds ?? 0);
    }
    if (document.activeElement !== fields.requestDelayMin) {
      fields.requestDelayMin.value = Number(dispatchSettings.request_delay_min_seconds ?? 0);
    }
    if (document.activeElement !== fields.requestDelayMax) {
      fields.requestDelayMax.value = Number(dispatchSettings.request_delay_max_seconds ?? 0);
    }
    if (document.activeElement !== fields.youtubeMaxInFlight) {
      fields.youtubeMaxInFlight.value = Number(dispatchSettings.youtube_max_in_flight ?? 1);
    }
    if (document.activeElement !== fields.archivarixMaxInFlight) {
      fields.archivarixMaxInFlight.value = Number(dispatchSettings.archivarix_max_in_flight ?? 1);
    }
    syncDispatchModeInputs();
  }
  const placeholderRun = data.latestPlaceholderRecoveryRun;
  if (placeholderRun) {
    const status = placeholderRun.status || 'unknown';
    const recoveryStatus = placeholderRun.recovery_status || '';
    fields.placeholderRunStatus.textContent = recoveryStatus && recoveryStatus !== status
      ? `${status} / ${recoveryStatus}`
      : status;
    fields.placeholderRunStatus.className = status === 'running'
      ? 'running'
      : (['blocked', 'error', 'interrupted'].includes(status) ? 'warn' : '');
    const details = [
      placeholderRun.run_id ? `Run ${placeholderRun.run_id}` : '',
      placeholderRun.video_id ? `Video ${placeholderRun.video_id}` : '',
      placeholderRun.started_at ? `Started ${fmtTime(placeholderRun.started_at)}` : '',
      placeholderRun.message || '',
    ].filter(Boolean);
    fields.placeholderRunDetails.textContent = details.join(' | ');
    fields.placeholderRunDetails.classList.toggle('run-id', Boolean(placeholderRun.run_id));
  } else {
    fields.placeholderRunStatus.textContent = 'No runs';
    fields.placeholderRunStatus.className = '';
    fields.placeholderRunDetails.textContent = '';
    fields.placeholderRunDetails.classList.remove('run-id');
  }

  const channelCounts = data.channelCounts || {};
  currentFeatureBackfillCounts = data.featureBackfillCounts || {};
  fields.playlistBackfillCount.textContent = currentFeatureBackfillCounts.playlist_metadata || 0;
  fields.playlistIncompleteCount.textContent = currentFeatureBackfillCounts.playlist_incomplete || 0;
  fields.backfillVideoVisibility.disabled = !(currentFeatureBackfillCounts.video_visibility || 0);
  fields.backfillPlaylistMetadata.disabled = !(currentFeatureBackfillCounts.playlist_metadata || 0);
  fields.backfillChannelAccount.disabled = !(currentFeatureBackfillCounts.channel_account || 0);
  const channelCards = [
    { label: 'Channels', count: channelCounts.total || 0 },
    { label: 'Channel thumbs cached', count: channelCounts.thumbnail_cached || 0 },
    { label: 'Channel thumbs missing', count: channelCounts.thumbnail_missing || 0 },
    { label: 'Channels terminated', count: channelCounts.terminated || 0 },
    { label: 'First watch identified', count: channelCounts.first_seen || 0 },
    { label: 'First watch unavailable', count: channelCounts.first_seen_missing || 0 },
    { label: 'Channel URLs missing', count: channelCounts.url_missing || 0 },
    { label: 'Account-state backfill', count: currentFeatureBackfillCounts.channel_account || 0 },
    { label: 'Notifications missing', count: currentFeatureBackfillCounts.channel_notification_missing || 0 },
  ];
  const metadataStatusOrder = new Map([['ok', 0], ['', 1], ['no_metadata', 2]]);
  const metadataStatusLabel = status => {
    if (!status) return 'blank';
    if (status === 'no_metadata') return 'missing';
    return status;
  };
  const videoMetadataCards = [...(data.metadataCounts || [])].sort((left, right) => {
    const leftStatus = left.fetch_status || '';
    const rightStatus = right.fetch_status || '';
    const leftRank = metadataStatusOrder.has(leftStatus) ? metadataStatusOrder.get(leftStatus) : 10;
    const rightRank = metadataStatusOrder.has(rightStatus) ? metadataStatusOrder.get(rightStatus) : 10;
    return leftRank - rightRank || String(leftStatus).localeCompare(String(rightStatus));
  }).map(row => ({
    label: `Videos ${metadataStatusLabel(row.fetch_status || '')}`,
    count: row.count,
  }));
  videoMetadataCards.push(
    { label: 'Visibility backfill', count: currentFeatureBackfillCounts.video_visibility || 0 },
    { label: 'Visibility/channel incomplete', count: currentFeatureBackfillCounts.video_incomplete || 0 },
  );
  const metricCards = rows => rows.map(row => {
    const div = document.createElement('div');
    div.className = 'panel';
    div.innerHTML = `<div class="metric">${escapeHtml(row.label)}</div><div class="value">${row.count}</div>`;
    return div;
  });
  fields.videoMetadataCounts.replaceChildren(...metricCards(videoMetadataCards));
  const clipCounts = data.clipCounts || {};
  fields.clipCounts.replaceChildren(...metricCards([
    { label: 'Clips', count: clipCounts.total || 0 },
    { label: 'Mine', count: clipCounts.mine || 0 },
    { label: 'Others', count: clipCounts.others || 0 },
    { label: 'Ownership unknown', count: clipCounts.ownership_unknown || 0 },
    { label: 'Active', count: clipCounts.active || 0 },
    { label: 'Unavailable', count: clipCounts.unavailable || 0 },
    { label: 'Metadata pending', count: clipCounts.metadata_pending || 0 },
  ]));
  fields.channelCounts.replaceChildren(...metricCards(channelCards));

}

function renderServiceUnavailable(error) {
  const message = error instanceof Error ? error.message : String(error || '');
  currentServicePid = 0;
  fields.serviceStatus.textContent = 'Unavailable';
  fields.serviceStatus.className = 'warn';
  fields.serviceStatus.title = message;
  serviceStartedAtEpochMs = null;
  updateServiceUptime();
  fields.restartService.disabled = true;
  fields.commonWorkerState.textContent = 'unknown';
  fields.commonWorkerState.className = 'value warn';
  fields.startWorkerQueue.disabled = true;
  fields.stopWorkerQueue.disabled = true;
  fields.startWorkerQueue.classList.remove('primary');
  fields.stopWorkerQueue.classList.remove('danger');
}

const runtimeStatusRequest = { controller: null, generation: 0, promise: null };
const statusRequest = { controller: null, generation: 0, promise: null };

let operationPollTimer = null;
let operationPollsRemaining = 0;
let queueEventStream = null;
let logEventStream = null;
let adminPageSuspended = document.visibilityState === 'hidden';
let resumeRefreshPromise = null;

function adminPageIsActive() {
  return !adminPageSuspended && document.visibilityState !== 'hidden';
}

function cancelStatusRequest(state) {
  state.generation += 1;
  state.controller?.abort();
  state.controller = null;
  state.promise = null;
}

function scheduleActionPolls() {
  operationPollsRemaining = Math.max(operationPollsRemaining, 10);
  if (operationPollTimer !== null || !adminPageIsActive()) return;
  const poll = () => {
    operationPollTimer = null;
    if (!adminPageIsActive()) return;
    Promise.allSettled([
      loadRuntimeStatus({ force: true }),
      loadStatus({ force: true }),
    ])
      .finally(() => {
        operationPollsRemaining -= 1;
        if (operationPollsRemaining > 0 && adminPageIsActive()) {
          operationPollTimer = window.setTimeout(poll, 1000);
        }
      });
  };
  operationPollTimer = window.setTimeout(poll, 500);
}

async function fetchStatusJson(endpoint, controller, timeoutMs) {
  let timedOut = false;
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    const response = await fetch(endpoint, {
      cache: 'no-store',
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`Status failed: ${response.status}`);
    return await response.json();
  } catch (error) {
    if (error?.name === 'AbortError' && timedOut) {
      throw new Error('Request timed out');
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

function runStatusRequest(state, endpoint, timeoutMs, apply, options = {}) {
  const force = Boolean(options.force);
  if (state.promise && !force) return state.promise;
  if (force) cancelStatusRequest(state);

  const generation = state.generation + 1;
  const controller = new AbortController();
  state.generation = generation;
  state.controller = controller;
  const promise = fetchStatusJson(endpoint, controller, timeoutMs)
    .then(data => {
      if (state.generation === generation) apply(data);
      return data;
    })
    .catch(error => {
      if (error?.name === 'AbortError' || state.generation !== generation) return null;
      throw error;
    })
    .finally(() => {
      if (state.generation !== generation) return;
      state.controller = null;
      state.promise = null;
    });
  state.promise = promise;
  return promise;
}

function loadRuntimeStatus(options = {}) {
  const requestStartedAt = performance.now();
  return runStatusRequest(
    runtimeStatusRequest,
    '/api/admin/runtime/status',
    runtimeStatusRequestTimeoutMs,
    data => {
      syncServerClock(data.service?.serverTime, requestStartedAt);
      renderRuntimeStatus(data);
    },
    options,
  ).catch(error => {
    renderServiceUnavailable(error);
    throw error;
  });
}

function loadStatus(options = {}) {
  return runStatusRequest(
    statusRequest,
    '/api/admin/status?queue_limit=0&include_logs=0',
    statusRequestTimeoutMs,
    render,
    options,
  );
}

async function post(path, params = {}) {
  if (path === '/api/admin/queue/start') {
    fields.startWorkerQueue.classList.remove('primary');
    fields.startWorkerQueue.disabled = true;
  }
  const payload = await AdminTransport.postJson(path, params);
  scheduleActionPolls();
  loadStatus({ force: true }).catch(() => {});
  await loadRuntimeStatus({ force: true }).catch(() => {});
  return payload;
}

function sleep(milliseconds) {
  return new Promise(resolve => window.setTimeout(resolve, milliseconds));
}

async function waitForServiceRestart(previousPid) {
  const deadline = Date.now() + 45000;
  fields.serviceStatus.textContent = 'Restarting';
  fields.serviceStatus.className = 'advanced-only warn';
  fields.restartService.disabled = true;
  while (Date.now() < deadline) {
    await sleep(500);
    try {
      const response = await fetch('/api/admin/service/status', { cache: 'no-store' });
      if (!response.ok) continue;
      const payload = await response.json();
      const service = payload.service || {};
      const nextPid = Number(service.pid || 0);
      if (service.status === 'running' && nextPid && nextPid !== Number(previousPid || 0)) {
        currentServicePid = nextPid;
        loadStatus({ force: true }).catch(() => {});
        await loadRuntimeStatus({ force: true });
        return;
      }
    } catch (error) {
      // The listener is expected to be briefly unavailable during restart.
    }
  }
  fields.serviceStatus.textContent = 'Unavailable';
  fields.serviceStatus.className = 'advanced-only warn';
  fields.restartService.disabled = false;
  throw new Error('Service did not come back within 45 seconds');
}

async function saveAdminSettings() {
  if (settingsSaving) return;
  settingsSaving = true;
  fields.saveSettings.disabled = true;
  fields.settingsStatus.textContent = 'Saving';
  const previousPid = currentServicePid;
  try {
    const payload = await AdminTransport.postJson('/api/admin/settings', {
      display_timezone: fields.displayTimezone.value.trim(),
      week_start: fields.weekStartMonday.checked ? 'monday' : 'sunday',
      use_proxy: fields.useProxy.checked ? '1' : '0',
      proxy: fields.proxyUrl.value.trim(),
    });
    const settings = payload.settings || {};
    fields.displayTimezone.value = settings.displayTimezone || fields.displayTimezone.value.trim();
    fields.weekStartMonday.checked = settings.weekStart === 'monday';
    fields.weekStartSunday.checked = !fields.weekStartMonday.checked;
    fields.useProxy.checked = Boolean(settings.useProxy);
    fields.proxyUrl.value = settings.proxy || '';
    window.YTLibraryTime.apply(settings.displayTimezone || fields.displayTimezone.value);
    settingsDirty = false;
    if (payload.restartScheduled) {
      fields.settingsStatus.textContent = 'Restarting';
      await waitForServiceRestart(payload.service?.pid || previousPid);
    } else {
      await loadStatus({ force: true });
    }
    fields.settingsStatus.textContent = 'Saved';
  } catch (error) {
    fields.settingsStatus.textContent = 'Save failed';
    alert(error.message);
  } finally {
    settingsSaving = false;
    fields.saveSettings.disabled = false;
  }
}

async function saveHideEmptyFilters() {
  if (hideEmptyFiltersSaving || !hideEmptyFiltersDirty) return;
  const saveRevision = hideEmptyFiltersRevision;
  const enabled = fields.hideEmptyFilters.checked;
  hideEmptyFiltersSaving = true;
  hideEmptyFiltersDirty = false;
  fields.settingsStatus.textContent = 'Saving';
  try {
    const payload = await AdminTransport.postJson('/api/settings/hide-empty-filters', {
      enabled: enabled ? '1' : '0',
    });
    savedHideEmptyFilters = payload.hideEmptyFilters !== false;
    if (hideEmptyFiltersRevision === saveRevision) {
      fields.hideEmptyFilters.checked = savedHideEmptyFilters;
      fields.settingsStatus.textContent = 'Saved';
    }
  } catch (error) {
    if (hideEmptyFiltersRevision === saveRevision) {
      fields.hideEmptyFilters.checked = savedHideEmptyFilters;
      fields.settingsStatus.textContent = 'Save failed';
    }
    alert(error.message);
  } finally {
    hideEmptyFiltersSaving = false;
    if (hideEmptyFiltersDirty || hideEmptyFiltersRevision !== saveRevision) {
      saveHideEmptyFilters();
    }
  }
}

async function saveArchivarixAutoRetry() {
  if (archivarixAutoRetrySaving || !archivarixAutoRetryDirty) return;
  const saveRevision = archivarixAutoRetryRevision;
  const enabled = fields.archivarixAutoRetry.checked;
  archivarixAutoRetrySaving = true;
  archivarixAutoRetryDirty = false;
  try {
    const payload = await AdminTransport.postJson('/api/admin/archivarix-auto-retry', {
      enabled: enabled ? '1' : '0',
    });
    savedArchivarixAutoRetry = payload.settings?.archivarixAutoRetry !== false;
    if (archivarixAutoRetryRevision === saveRevision) {
      fields.archivarixAutoRetry.checked = savedArchivarixAutoRetry;
    }
  } catch (error) {
    if (archivarixAutoRetryRevision === saveRevision) {
      fields.archivarixAutoRetry.checked = savedArchivarixAutoRetry;
    }
    alert(error.message);
  } finally {
    archivarixAutoRetrySaving = false;
    if (archivarixAutoRetryDirty || archivarixAutoRetryRevision !== saveRevision) {
      saveArchivarixAutoRetry();
    }
  }
}

async function saveAdvancedMode() {
  if (advancedSaving || !advancedDirty) return;
  const saveRevision = advancedRevision;
  const enabled = fields.advancedToggle.checked;
  advancedSaving = true;
  advancedDirty = false;
  try {
    const payload = await AdminTransport.postJson('/api/admin/advanced', {
      enabled: enabled ? '1' : '0',
    });
    savedAdvanced = Boolean(payload.settings?.adminAdvanced);
    if (advancedRevision === saveRevision) {
      fields.advancedToggle.checked = savedAdvanced;
      applyAdvancedMode(savedAdvanced);
    }
  } catch (error) {
    if (advancedRevision === saveRevision) {
      fields.advancedToggle.checked = savedAdvanced;
      applyAdvancedMode(savedAdvanced);
    }
    alert(error.message);
  } finally {
    advancedSaving = false;
    if (advancedDirty || advancedRevision !== saveRevision) {
      saveAdvancedMode();
    }
  }
}

async function restartServiceNow() {
  if (!confirm('Restart the service? Active queue work will be interrupted.')) return;
  const previousPid = currentServicePid;
  fields.settingsStatus.textContent = 'Restarting';
  try {
    const payload = await AdminTransport.postJson('/api/admin/service/restart');
    await waitForServiceRestart(payload.service?.pid || previousPid);
    fields.settingsStatus.textContent = '';
  } catch (error) {
    fields.settingsStatus.textContent = 'Restart failed';
    alert(error.message);
  }
}

async function stopWorkersNow() {
  fields.commonWorkerState.textContent = 'stopping';
  fields.startWorkerQueue.classList.remove('primary');
  fields.stopWorkerQueue.classList.add('danger');
  fields.startWorkerQueue.disabled = true;
  fields.stopWorkerQueue.disabled = true;
  const payload = await AdminTransport.postJson('/api/admin/queue/stop');
  scheduleActionPolls();
  await loadRuntimeStatus({ force: true }).catch(() => {});
  return payload;
}

async function saveDispatchSettings() {
  if (dispatchSettingsSaving) {
    dispatchSettingsDirty = true;
    return;
  }
  if (!dispatchSettingsDirty) return;
  const saveRevision = dispatchSettingsRevision;
  const dispatchMode = fields.dispatchModeThrottle.checked ? 'throttle' : 'delay';
  const jobDispatchDelay = Number(fields.jobDispatchDelay.value);
  const requestDelayMin = Number(fields.requestDelayMin.value);
  const requestDelayMax = Number(fields.requestDelayMax.value);
  const youtubeMaxInFlight = Number(fields.youtubeMaxInFlight.value);
  const archivarixMaxInFlight = Number(fields.archivarixMaxInFlight.value);
  const requestDelays = [jobDispatchDelay, requestDelayMin, requestDelayMax];
  if (requestDelays.some(value => !Number.isFinite(value) || value < 0)) {
    fields.dispatchSettingsStatus.textContent = 'Invalid value';
    return;
  }
  if (requestDelayMax < requestDelayMin) {
    fields.dispatchSettingsStatus.textContent = 'Max must be at least min';
    return;
  }
  if (!Number.isInteger(youtubeMaxInFlight) || youtubeMaxInFlight < 1 || youtubeMaxInFlight > 100) {
    fields.dispatchSettingsStatus.textContent = 'Invalid YouTube limit';
    return;
  }
  if (!Number.isInteger(archivarixMaxInFlight) || archivarixMaxInFlight < 1 || archivarixMaxInFlight > 20) {
    fields.dispatchSettingsStatus.textContent = 'Invalid Archivarix limit';
    return;
  }
  dispatchSettingsDirty = false;
  dispatchSettingsSaving = true;
  fields.dispatchSettingsStatus.textContent = 'Saving';
  try {
    const payload = await AdminTransport.postJson('/api/admin/dispatch-settings', {
      dispatch_mode: dispatchMode,
      job_dispatch_delay_seconds: jobDispatchDelay,
      request_delay_min_seconds: requestDelayMin,
      request_delay_max_seconds: requestDelayMax,
      youtube_max_in_flight: youtubeMaxInFlight,
      archivarix_max_in_flight: archivarixMaxInFlight,
    });
    const saved = payload.dispatchSettings || {};
    if (dispatchSettingsRevision === saveRevision) {
      fields.dispatchModeDelay.checked = saved.dispatch_mode !== 'throttle';
      fields.dispatchModeThrottle.checked = saved.dispatch_mode === 'throttle';
      fields.jobDispatchDelay.value = Number(saved.job_dispatch_delay_seconds ?? jobDispatchDelay);
      fields.requestDelayMin.value = Number(saved.request_delay_min_seconds ?? requestDelayMin);
      fields.requestDelayMax.value = Number(saved.request_delay_max_seconds ?? requestDelayMax);
      fields.youtubeMaxInFlight.value = Number(saved.youtube_max_in_flight ?? youtubeMaxInFlight);
      fields.archivarixMaxInFlight.value = Number(saved.archivarix_max_in_flight ?? archivarixMaxInFlight);
      syncDispatchModeInputs();
      fields.dispatchSettingsStatus.textContent = 'Saved';
    }
  } catch (error) {
    dispatchSettingsDirty = true;
    fields.dispatchSettingsStatus.textContent = 'Save failed';
    alert(error.message);
  } finally {
    dispatchSettingsSaving = false;
    if (
      dispatchSettingsDirty
      && dispatchSettingsRevision !== saveRevision
    ) {
      scheduleDispatchSettingsSave();
    }
  }
}

function scheduleDispatchSettingsSave() {
  if (dispatchSettingsSaveTimer !== null) {
    window.clearTimeout(dispatchSettingsSaveTimer);
  }
  dispatchSettingsSaveTimer = window.setTimeout(() => {
    dispatchSettingsSaveTimer = null;
    saveDispatchSettings();
  }, 350);
}

function flushDispatchSettingsSave() {
  if (dispatchSettingsSaveTimer !== null) {
    window.clearTimeout(dispatchSettingsSaveTimer);
    dispatchSettingsSaveTimer = null;
  }
  saveDispatchSettings();
}

async function saveUpdateSchedule() {
  if (updateScheduleSaving || !updateScheduleDirty) return;
  const saveRevision = updateScheduleRevision;
  const frequency = fields.updateFrequency.value;
  const at = fields.updateTime.value;
  const minute = fields.updateHourMinute.value;
  if (frequency === 'daily' && !/^([01]\d|2[0-3]):[0-5]\d$/.test(at)) {
    fields.updateScheduleStatus.textContent = 'Choose a time';
    fields.updateScheduleStatus.className = 'metric warn';
    return;
  }
  updateScheduleSaving = true;
  fields.updateScheduleStatus.textContent = 'Saving';
  fields.updateScheduleStatus.className = 'metric';
  try {
    await AdminTransport.postJson('/api/admin/update-schedule', {
      frequency,
      at,
      minute,
    });
    if (updateScheduleRevision === saveRevision) {
      updateScheduleDirty = false;
    }
    updateScheduleSaving = false;
    await loadStatus({ force: true });
  } catch (error) {
    fields.updateScheduleStatus.textContent = error.message;
    fields.updateScheduleStatus.className = 'metric warn';
  } finally {
    updateScheduleSaving = false;
    if (updateScheduleDirty && updateScheduleRevision !== saveRevision) {
      scheduleUpdateScheduleSave();
    }
  }
}

function scheduleUpdateScheduleSave() {
  updateScheduleDirty = true;
  updateScheduleRevision += 1;
  if (updateScheduleSaveTimer !== null) {
    window.clearTimeout(updateScheduleSaveTimer);
  }
  updateScheduleSaveTimer = window.setTimeout(() => {
    updateScheduleSaveTimer = null;
    saveUpdateSchedule();
  }, 250);
}

fields.initializeLibrary.addEventListener('click', async () => {
  const warning = 'Initialize the library? This queues a full YouTube history verification, discovers clips, collects personal activity and YouTube library dates, scans Liked videos and every known playlist, and fetches missing or stale video and channel metadata. This will usually take a significant amount of time.';
  if (!confirm(warning)) return;
  fields.initializeLibrary.disabled = true;
  fields.initializeStatus.textContent = 'Queueing';
  try {
    const result = await post('/api/admin/initialize');
    const queue = result.queue || {};
    fields.initializeStatus.textContent =
      `Queued ${Number(queue.inserted || 0).toLocaleString()}; ${Number(queue.already_queued || 0).toLocaleString()} already queued`;
  } catch (error) {
    fields.initializeStatus.textContent = error.message;
  } finally {
    fields.initializeLibrary.disabled = false;
  }
});
fields.updateLibrary.addEventListener('click', async () => {
  updateRequestPending = true;
  fields.updateLibrary.disabled = true;
  fields.updateStatus.textContent = 'Queueing';
  try {
    const result = await post('/api/admin/update/start');
    fields.updateStatus.textContent = formatUpdateResult(result.queue || {});
  } catch (error) {
    fields.updateStatus.textContent = error.message;
  } finally {
    updateRequestPending = false;
    fields.updateLibrary.disabled = false;
  }
});
document.getElementById('scanPlaylists').addEventListener('click', () => post('/api/admin/playlists/start').catch(error => alert(error.message)));
document.getElementById('fetchVideoMetadata').addEventListener('click', () => post('/api/admin/metadata/start', {
  kind: 'video',
  stale_days: fields.videoMetadataStaleDays.value,
  force: fields.videoMetadataForce.checked ? '1' : '0',
}).catch(error => alert(error.message)));
fields.discoverClips.addEventListener('click', async () => {
  fields.discoverClips.disabled = true;
  fields.clipStatus.textContent = 'Queueing';
  try {
    await post('/api/admin/clips/discover');
    fields.clipStatus.textContent = 'Clip discovery queued';
  } catch (error) {
    fields.clipStatus.textContent = error.message;
  } finally {
    fields.discoverClips.disabled = false;
  }
});
document.getElementById('fetchChannelMetadata').addEventListener('click', () => post('/api/admin/metadata/start', {
  kind: 'channel',
  stale_days: fields.channelMetadataStaleDays.value,
  force: fields.channelMetadataForce.checked ? '1' : '0',
}).catch(error => alert(error.message)));
async function startFeatureBackfill(kind, label, statusField) {
  const count = Number(currentFeatureBackfillCounts[kind] || 0);
  if (!count) {
    statusField.textContent = 'Nothing to queue';
    return;
  }
  if (!confirm(`Queue ${count.toLocaleString()} ${label} requests?`)) return;
  statusField.textContent = 'Queueing';
  try {
    const result = await post('/api/admin/feature-backfill/start', { kind });
    const queue = result.queue || {};
    statusField.textContent =
      `Queued ${Number(queue.inserted || 0).toLocaleString()}; ${Number(queue.already_queued || 0).toLocaleString()} already queued`;
  } catch (error) {
    statusField.textContent = error.message;
  }
}
fields.backfillVideoVisibility.addEventListener('click', () => {
  startFeatureBackfill(
    'video_visibility',
    'video visibility backfill',
    fields.videoBackfillStatus,
  );
});
fields.backfillPlaylistMetadata.addEventListener('click', () => {
  startFeatureBackfill(
    'playlist_metadata',
    'playlist metadata backfill',
    fields.playlistBackfillStatus,
  );
});
fields.backfillChannelAccount.addEventListener('click', () => {
  startFeatureBackfill(
    'channel_account',
    'channel account-state backfill',
    fields.channelBackfillStatus,
  );
});
document.getElementById('queueProvidedTarget').addEventListener('click', async () => {
  const target = fields.providedQueueTarget.value.trim();
  if (!target) {
    alert('Enter a video, clip, channel, or playlist URL or ID.');
    return;
  }
  try {
    await post('/api/admin/queue/add-target', { target });
    fields.providedQueueTarget.value = '';
    await post('/api/admin/queue/start');
  } catch (error) {
    alert(error.message);
  }
});
fields.workerQueueRows.addEventListener('click', event => {
  const button = event.target.closest('.remove-queue-entry');
  if (!button) return;
  const queueId = button.dataset.queueId || '';
  if (!queueId) return;
  post('/api/admin/queue/remove', { queue_id: queueId }).catch(error => alert(error.message));
});
async function enqueuePluginProcess(event) {
  const action = event.target.closest('.plugin-process-action');
  if (!action) return;
  event.preventDefault();
  const button = action.querySelector('.plugin-process-enqueue');
  const pluginId = action.dataset.pluginId || '';
  const workerId = action.dataset.pluginWorkerId || '';
  const confirmation = action.dataset.confirm || '';
  if (!pluginId || !workerId || (confirmation && !confirm(confirmation))) return;
  const status = action.querySelector('.plugin-process-status');
  const params = {};
  for (const input of action.querySelectorAll('[data-plugin-param]')) {
    const value = input.value.trim();
    if (input.required && !value) {
      input.focus();
      if (status) status.textContent = `Enter ${input.closest('label')?.textContent?.trim() || input.dataset.pluginParam}`;
      return;
    }
    if (value) params[input.dataset.pluginParam] = value;
  }
  button.disabled = true;
  if (status) status.textContent = 'Planning tasks';
  try {
    const result = await AdminTransport.postJson(
      `/api/admin/plugins/${encodeURIComponent(pluginId)}/processes/${encodeURIComponent(workerId)}/enqueue`,
      params,
    );
    const queue = result.queue || {};
    if (status) {
      status.textContent = `Queued ${Number(queue.inserted || 0).toLocaleString()}; ${Number(queue.alreadyQueued || 0).toLocaleString()} already queued`;
    }
    for (const input of action.querySelectorAll('[data-plugin-param]')) input.value = '';
    scheduleActionPolls();
  } catch (error) {
    if (status) status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}
async function savePluginEnabled(event) {
  const toggle = event.target.closest('.plugin-enabled-toggle');
  if (!toggle) return;
  const pluginId = toggle.dataset.pluginId || '';
  if (!pluginId) return;
  const enabled = toggle.checked;
  const status = toggle.closest('.plugin-enabled-control')?.querySelector('.plugin-enabled-status');
  const previousPid = currentServicePid;
  toggle.disabled = true;
  if (status) status.textContent = 'Saving';
  try {
    if (window.YTLibraryPluginPackages) {
      await window.YTLibraryPluginPackages.setEnabled(pluginId, enabled);
      if (status) status.textContent = 'Maintenance queued';
      return;
    }
    const payload = await AdminTransport.postJson(
      `/api/admin/plugins/${encodeURIComponent(pluginId)}/enabled`,
      { enabled: enabled ? '1' : '0' },
    );
    if (payload.restartScheduled) {
      if (status) status.textContent = 'Restarting';
      await waitForServiceRestart(payload.service?.pid || previousPid);
    } else {
      await loadStatus({ force: true });
    }
  } catch (error) {
    toggle.checked = !enabled;
    if (status) status.textContent = 'Save failed';
    alert(error.message);
  } finally {
    toggle.disabled = false;
  }
}
fields.pluginWorkstreams.addEventListener('submit', enqueuePluginProcess);
fields.pluginWorkstreams.addEventListener('change', savePluginEnabled);
window.addEventListener('ytl-plugins-changed', () => loadStatus({ force: true }).catch(error => console.error(error)));
fields.videoPluginProcesses.addEventListener('submit', enqueuePluginProcess);
document.getElementById('startLiveHistory').addEventListener('click', () => post('/api/admin/live-history/start').catch(error => alert(error.message)));
fields.updateFrequency.addEventListener('change', () => {
  syncUpdateScheduleControls();
  scheduleUpdateScheduleSave();
});
fields.updateTime.addEventListener('change', scheduleUpdateScheduleSave);
fields.updateHourMinute.addEventListener('change', scheduleUpdateScheduleSave);
document.getElementById('verifyLiveHistory').addEventListener('click', () => {
  if (!confirm('Verify the full YouTube history? This may run for a long time, but existing fetched history will be kept.')) return;
  post('/api/admin/live-history/verify').catch(error => alert(error.message));
});
document.getElementById('importTakeoutHistory').addEventListener('click', () => {
  if (!confirm('Import all Takeout zips from the configured takeout directory and rebuild reconciliation? Existing Takeout rows will be kept and duplicates will be skipped.')) return;
  post('/api/admin/history/import-takeout').catch(error => alert(error.message));
});
document.getElementById('reconcileHistory').addEventListener('click', () => post('/api/admin/history/reconcile').catch(error => alert(error.message)));
document.getElementById('reconcilePlaylists').addEventListener('click', () => post('/api/admin/playlists/reconcile').catch(error => alert(error.message)));
document.getElementById('startWorkerQueue').addEventListener('click', () => post('/api/admin/queue/start').catch(error => alert(error.message)));
fields.retryProxy.addEventListener('click', () => post('/api/admin/proxy/retry').catch(error => alert(error.message)));
document.getElementById('retryArchivarix').addEventListener('click', () => post('/api/admin/archivarix/retry').catch(error => alert(error.message)));
document.getElementById('rebuildWorkerQueue').addEventListener('click', () => {
  const warning = 'Rebuild automatic library work? This replaces automatically planned account, metadata, playlist, and History jobs while preserving manual requests, pending Clip and Archivarix recovery work, plugin jobs, and other non-plan jobs. It also queues current Clip and playlist discovery. The rebuilt queue will not start automatically.';
  if (!confirm(warning)) return;
  post('/api/admin/queue/rebuild').catch(error => alert(error.message));
});
document.getElementById('clearWorkerQueue').addEventListener('click', () => {
  if (!confirm('Clear the worker queue? This removes all pending core, Clip, Archivarix recovery, and plugin jobs.')) return;
  post('/api/admin/queue/clear').catch(error => alert(error.message));
});
document.getElementById('stopWorkerQueue').addEventListener('click', () => stopWorkersNow().catch(error => alert(error.message)));
for (const field of [
  fields.jobDispatchDelay,
  fields.requestDelayMin,
  fields.requestDelayMax,
  fields.youtubeMaxInFlight,
  fields.archivarixMaxInFlight,
]) {
  field.addEventListener('input', () => {
    dispatchSettingsDirty = true;
    dispatchSettingsRevision += 1;
    scheduleDispatchSettingsSave();
  });
  field.addEventListener('blur', flushDispatchSettingsSave);
}
for (const field of [fields.dispatchModeDelay, fields.dispatchModeThrottle]) {
  field.addEventListener('change', () => {
    dispatchSettingsDirty = true;
    dispatchSettingsRevision += 1;
    syncDispatchModeInputs();
    flushDispatchSettingsSave();
  });
}
fields.logSourceFilter.addEventListener('change', () => {
  loadLogPage(true).catch(error => { fields.logs.title = error.message; });
});
fields.logLevelFilter.addEventListener('change', () => {
  loadLogPage(true).catch(error => { fields.logs.title = error.message; });
});
fields.advancedToggle.addEventListener('change', () => {
  advancedDirty = true;
  advancedRevision += 1;
  applyAdvancedMode(fields.advancedToggle.checked);
  saveAdvancedMode();
});
fields.themeToggle.addEventListener('change', () => {
  const selectedTheme = window.YTLibraryTheme.set(fields.themeToggle.checked ? 'dark' : 'light');
  fields.themeToggle.checked = selectedTheme === 'dark';
});
window.addEventListener('ytlibrarytimezonechange', () => updateCurrentDateTime());
fields.displayTimezone.addEventListener('input', () => { settingsDirty = true; });
fields.weekStartSunday.addEventListener('change', () => { settingsDirty = true; });
fields.weekStartMonday.addEventListener('change', () => { settingsDirty = true; });
fields.hideEmptyFilters.addEventListener('change', () => {
  hideEmptyFiltersDirty = true;
  hideEmptyFiltersRevision += 1;
  saveHideEmptyFilters();
});
fields.archivarixAutoRetry.addEventListener('change', () => {
  archivarixAutoRetryDirty = true;
  archivarixAutoRetryRevision += 1;
  saveArchivarixAutoRetry();
});
fields.useProxy.addEventListener('change', () => { settingsDirty = true; });
fields.proxyUrl.addEventListener('input', () => { settingsDirty = true; });
fields.saveSettings.addEventListener('click', saveAdminSettings);
fields.restartService.addEventListener('click', restartServiceNow);
document.getElementById('detectTimezone').addEventListener('click', () => {
  fields.displayTimezone.value = window.YTLibraryTime.detected();
  settingsDirty = true;
});
for (const button of document.querySelectorAll('[data-advanced-tab]')) {
  button.addEventListener('click', () => selectAdvancedTab(button.dataset.advancedTab || 'youtube'));
}
for (const button of document.querySelectorAll('.save-cookie')) {
  button.addEventListener('click', () => saveCookieFile(button.dataset.cookieKind || '', button));
}

function connectQueueEvents() {
  if (queueEventStream || !adminPageIsActive()) return;
  const stream = new EventSource('/api/admin/queue/events');
  queueEventStream = stream;
  stream.addEventListener('queue_reset', event => {
    const payload = JSON.parse(event.data);
    resetQueueState(payload.total || 0);
  });
  stream.addEventListener('queue_snapshot', event => {
    const payload = JSON.parse(event.data);
    applyQueueSnapshot(payload.rows || [], payload.total || 0);
  });
  stream.addEventListener('queue_ready', event => {
    const payload = JSON.parse(event.data);
    queueState.ready = true;
    queueState.total = Number(payload.total || queueState.rowsById.size);
    fields.commonQueueCount.textContent = queueState.total;
    scheduleQueueRender();
  });
  stream.addEventListener('queue_delta', event => {
    applyQueueDelta(JSON.parse(event.data));
  });
  stream.onerror = () => {
    if (queueEventStream !== stream) return;
    fields.commonQueueCount.title = 'Queue event stream reconnecting';
  };
  stream.onopen = () => {
    if (queueEventStream !== stream) return;
    fields.commonQueueCount.title = '';
  };
}

function connectLogEvents() {
  if (logEventStream || !adminPageIsActive()) return;
  const stream = new EventSource('/api/admin/logs/events');
  logEventStream = stream;
  stream.addEventListener('log_reset', () => {
    loadLogPage(true).catch(error => { fields.logs.title = error.message; });
  });
  stream.addEventListener('log_delta', event => {
    applyLogs(JSON.parse(event.data));
  });
  stream.onerror = () => {
    if (logEventStream !== stream) return;
    fields.logs.title = 'Log event stream reconnecting';
  };
  stream.onopen = () => {
    if (logEventStream !== stream) return;
    fields.logs.title = '';
  };
}

function disconnectAdminEventStreams() {
  queueEventStream?.close();
  logEventStream?.close();
  queueEventStream = null;
  logEventStream = null;
}

function suspendAdminPage() {
  adminPageSuspended = true;
  disconnectAdminEventStreams();
  cancelStatusRequest(runtimeStatusRequest);
  cancelStatusRequest(statusRequest);
  logState.requestController?.abort();
  if (operationPollTimer !== null) {
    window.clearTimeout(operationPollTimer);
    operationPollTimer = null;
  }
}

function resumeAdminPage() {
  if (document.visibilityState === 'hidden') return;
  adminPageSuspended = false;
  connectQueueEvents();
  connectLogEvents();
  if (operationPollsRemaining > 0) scheduleActionPolls();
  if (resumeRefreshPromise) return;
  resumeRefreshPromise = Promise.allSettled([
    loadRuntimeStatus({ force: true }),
    loadStatus({ force: true }),
  ]).finally(() => { resumeRefreshPromise = null; });
}

updateCurrentDateTime();
fields.workerQueuePanel.addEventListener('scroll', scheduleQueueRender, { passive: true });
fields.logPanel.addEventListener('scroll', loadMoreLogsIfNeeded, { passive: true });
new ResizeObserver(scheduleQueueRender).observe(fields.workerQueuePanel);
loadCookieStatuses()
  .catch(error => { fields.googleCookieStatus.textContent = error.message; });
setInterval(() => {
  if (!adminPageIsActive()) return;
  loadRuntimeStatus().catch(() => {});
}, runtimeStatusPollMs);
setInterval(() => {
  if (!adminPageIsActive()) return;
  loadStatus().catch(() => {});
}, statusPollMs);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') suspendAdminPage();
  else resumeAdminPage();
});
window.addEventListener('pagehide', suspendAdminPage);
window.addEventListener('pageshow', resumeAdminPage);
document.addEventListener('freeze', suspendAdminPage);
document.addEventListener('resume', resumeAdminPage);
window.addEventListener('online', resumeAdminPage);
resumeAdminPage();
setInterval(() => {
  if (adminPageIsActive()) updateQueueTimingDisplay();
}, 1000);
setInterval(() => {
  if (adminPageIsActive()) updateCurrentDateTime();
}, 1000);
