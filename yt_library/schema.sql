PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS channels (
  channel_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  aliases TEXT NOT NULL DEFAULT '',
  subscribed INTEGER NOT NULL DEFAULT 0 CHECK (subscribed IN (0, 1)),
  subscribed_at TEXT,
  subscribed_at_source TEXT NOT NULL DEFAULT '',
  notification_level TEXT NOT NULL DEFAULT ''
    CHECK (notification_level IN ('', 'all', 'personalized', 'none')),
  subscription_checked_at TEXT,
  notification_checked_at TEXT,
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  archivarix_channel_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  status_reason TEXT NOT NULL DEFAULT '',
  fetch_status TEXT NOT NULL DEFAULT '',
  fetch_error TEXT NOT NULL DEFAULT '',
  first_seen_at TEXT,
  fetched_at TEXT,
  metadata_source TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS channel_featured_channels (
  owner_channel_id TEXT NOT NULL REFERENCES channels(channel_id) ON DELETE CASCADE,
  featured_channel_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  channel_reference TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL,
  PRIMARY KEY (owner_channel_id, featured_channel_id)
);

CREATE TABLE IF NOT EXISTS playlists (
  playlist_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  owner_channel_id TEXT REFERENCES channels(channel_id),
  visibility TEXT NOT NULL DEFAULT '',
  created_at TEXT,
  last_changed_at TEXT,
  youtube_updated_date TEXT,
  metadata_checked_at TEXT,
  ownership TEXT NOT NULL DEFAULT 'unknown'
    CHECK (ownership IN ('mine', 'others', 'unknown')),
  in_library INTEGER NOT NULL DEFAULT 0 CHECK (in_library IN (0, 1)),
  library_missing_at TEXT,
  video_count INTEGER NOT NULL DEFAULT 0,
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  fetch_status TEXT NOT NULL DEFAULT '',
  fetch_error TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS playlist_collaborators (
  playlist_id TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  channel_id TEXT NOT NULL REFERENCES channels(channel_id),
  position INTEGER NOT NULL,
  PRIMARY KEY (playlist_id, channel_id)
);

CREATE TABLE IF NOT EXISTS groups (
  group_key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  parent_key TEXT REFERENCES groups(group_key) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  icon TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS group_playlists (
  group_key TEXT NOT NULL REFERENCES groups(group_key) ON DELETE CASCADE,
  playlist_id TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  PRIMARY KEY (group_key, playlist_id)
);

CREATE TABLE IF NOT EXISTS videos (
  video_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  channel_id TEXT REFERENCES channels(channel_id),
  duration_text TEXT NOT NULL DEFAULT '',
  view_count TEXT NOT NULL DEFAULT '',
  upload_date TEXT NOT NULL DEFAULT '',
  uploader_category TEXT NOT NULL DEFAULT '',
  video_type TEXT NOT NULL DEFAULT ''
    CHECK (video_type IN ('', 'video', 'short', 'livestream', 'movie')),
  broadcast_status TEXT
    CHECK (broadcast_status IS NULL OR broadcast_status IN ('', 'upcoming', 'live', 'ended')),
  broadcast_started_at TEXT,
  broadcast_ended_at TEXT,
  broadcast_status_checked_at TEXT,
  movie_rating TEXT NOT NULL DEFAULT '',
  movie_release_date TEXT NOT NULL DEFAULT '',
  movie_offer TEXT NOT NULL DEFAULT '',
  max_video_height INTEGER CHECK (max_video_height IS NULL OR max_video_height > 0),
  spatial_format TEXT CHECK (spatial_format IS NULL OR spatial_format IN ('', '360', 'vr180')),
  stereo_layout TEXT CHECK (stereo_layout IS NULL OR stereo_layout IN ('', 'left_right', 'top_bottom')),
  dynamic_range TEXT CHECK (dynamic_range IS NULL OR dynamic_range IN ('sdr', 'hdr')),
  license TEXT,
  location_name TEXT,
  content_check_required INTEGER CHECK (content_check_required IN (0, 1)),
  content_check_reason TEXT,
  ai_disclosure INTEGER CHECK (ai_disclosure IN (0, 1)),
  ai_disclosure_text TEXT,
  auto_dubbed INTEGER CHECK (auto_dubbed IN (0, 1)),
  thumbnail_url TEXT NOT NULL DEFAULT '',
  thumbnail_path TEXT NOT NULL DEFAULT '',
  reaction TEXT NOT NULL DEFAULT ''
    CHECK (reaction IN ('', 'LIKE', 'DISLIKE', 'INDIFFERENT')),
  is_playable INTEGER CHECK (is_playable IN (0, 1)),
  availability TEXT NOT NULL DEFAULT 'unknown',
  visibility_checked_at TEXT,
  metadata_source TEXT NOT NULL DEFAULT '',
  fetch_status TEXT NOT NULL DEFAULT '',
  fetch_error TEXT NOT NULL DEFAULT '',
  fetched_at TEXT,
  last_seen_available_at TEXT,
  last_checked_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS clips (
  clip_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  owner_channel_id TEXT REFERENCES channels(channel_id),
  owner_title TEXT NOT NULL DEFAULT '',
  owner_thumbnail_url TEXT NOT NULL DEFAULT '',
  owner_thumbnail_path TEXT NOT NULL DEFAULT '',
  ownership TEXT NOT NULL DEFAULT 'unknown'
    CHECK (ownership IN ('mine', 'others', 'unknown')),
  source_video_id TEXT REFERENCES videos(video_id),
  start_ms INTEGER,
  end_ms INTEGER,
  view_count INTEGER,
  view_count_text TEXT NOT NULL DEFAULT '',
  clipped_at TEXT,
  clipped_at_text TEXT NOT NULL DEFAULT '',
  clipped_at_observed_at TEXT,
  youtube_feed_ordinal INTEGER,
  thumbnail_url TEXT NOT NULL DEFAULT '',
  availability TEXT NOT NULL DEFAULT 'unknown'
    CHECK (availability IN ('active', 'unavailable', 'unknown')),
  fetch_status TEXT NOT NULL DEFAULT '',
  fetch_error TEXT NOT NULL DEFAULT '',
  fetched_at TEXT,
  last_seen_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS tags (
  tag_id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  normalized_name TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS video_tags (
  video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(tag_id) ON DELETE CASCADE,
  PRIMARY KEY (video_id, tag_id)
);

CREATE TABLE IF NOT EXISTS clip_tags (
  clip_id TEXT NOT NULL REFERENCES clips(clip_id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(tag_id) ON DELETE CASCADE,
  PRIMARY KEY (clip_id, tag_id)
);

CREATE TABLE IF NOT EXISTS playlist_tags (
  playlist_id TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(tag_id) ON DELETE CASCADE,
  PRIMARY KEY (playlist_id, tag_id)
);

CREATE TABLE IF NOT EXISTS channel_tags (
  channel_id TEXT NOT NULL REFERENCES channels(channel_id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(tag_id) ON DELETE CASCADE,
  PRIMARY KEY (channel_id, tag_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS entity_note_fts USING fts5(
  entity_kind UNINDEXED,
  entity_id UNINDEXED,
  note,
  tokenize = 'unicode61'
);

CREATE TRIGGER IF NOT EXISTS videos_note_fts_insert
AFTER INSERT ON videos WHEN trim(NEW.note) <> '' BEGIN
  INSERT INTO entity_note_fts(entity_kind, entity_id, note)
  VALUES ('video', NEW.video_id, NEW.note);
END;
CREATE TRIGGER IF NOT EXISTS videos_note_fts_update
AFTER UPDATE OF note ON videos BEGIN
  DELETE FROM entity_note_fts WHERE entity_kind = 'video' AND entity_id = OLD.video_id;
  INSERT INTO entity_note_fts(entity_kind, entity_id, note)
  SELECT 'video', NEW.video_id, NEW.note WHERE trim(NEW.note) <> '';
END;
CREATE TRIGGER IF NOT EXISTS videos_note_fts_delete
AFTER DELETE ON videos BEGIN
  DELETE FROM entity_note_fts WHERE entity_kind = 'video' AND entity_id = OLD.video_id;
END;

CREATE TRIGGER IF NOT EXISTS clips_note_fts_insert
AFTER INSERT ON clips WHEN trim(NEW.note) <> '' BEGIN
  INSERT INTO entity_note_fts(entity_kind, entity_id, note)
  VALUES ('clip', NEW.clip_id, NEW.note);
END;
CREATE TRIGGER IF NOT EXISTS clips_note_fts_update
AFTER UPDATE OF note ON clips BEGIN
  DELETE FROM entity_note_fts WHERE entity_kind = 'clip' AND entity_id = OLD.clip_id;
  INSERT INTO entity_note_fts(entity_kind, entity_id, note)
  SELECT 'clip', NEW.clip_id, NEW.note WHERE trim(NEW.note) <> '';
END;
CREATE TRIGGER IF NOT EXISTS clips_note_fts_delete
AFTER DELETE ON clips BEGIN
  DELETE FROM entity_note_fts WHERE entity_kind = 'clip' AND entity_id = OLD.clip_id;
END;

CREATE TRIGGER IF NOT EXISTS playlists_note_fts_insert
AFTER INSERT ON playlists WHEN trim(NEW.note) <> '' BEGIN
  INSERT INTO entity_note_fts(entity_kind, entity_id, note)
  VALUES ('playlist', NEW.playlist_id, NEW.note);
END;
CREATE TRIGGER IF NOT EXISTS playlists_note_fts_update
AFTER UPDATE OF note ON playlists BEGIN
  DELETE FROM entity_note_fts WHERE entity_kind = 'playlist' AND entity_id = OLD.playlist_id;
  INSERT INTO entity_note_fts(entity_kind, entity_id, note)
  SELECT 'playlist', NEW.playlist_id, NEW.note WHERE trim(NEW.note) <> '';
END;
CREATE TRIGGER IF NOT EXISTS playlists_note_fts_delete
AFTER DELETE ON playlists BEGIN
  DELETE FROM entity_note_fts WHERE entity_kind = 'playlist' AND entity_id = OLD.playlist_id;
END;

CREATE TRIGGER IF NOT EXISTS channels_note_fts_insert
AFTER INSERT ON channels WHEN trim(NEW.note) <> '' BEGIN
  INSERT INTO entity_note_fts(entity_kind, entity_id, note)
  VALUES ('channel', NEW.channel_id, NEW.note);
END;
CREATE TRIGGER IF NOT EXISTS channels_note_fts_update
AFTER UPDATE OF note ON channels BEGIN
  DELETE FROM entity_note_fts WHERE entity_kind = 'channel' AND entity_id = OLD.channel_id;
  INSERT INTO entity_note_fts(entity_kind, entity_id, note)
  SELECT 'channel', NEW.channel_id, NEW.note WHERE trim(NEW.note) <> '';
END;
CREATE TRIGGER IF NOT EXISTS channels_note_fts_delete
AFTER DELETE ON channels BEGIN
  DELETE FROM entity_note_fts WHERE entity_kind = 'channel' AND entity_id = OLD.channel_id;
END;

CREATE INDEX IF NOT EXISTS idx_video_tags_tag ON video_tags(tag_id, video_id);
CREATE INDEX IF NOT EXISTS idx_clip_tags_tag ON clip_tags(tag_id, clip_id);
CREATE INDEX IF NOT EXISTS idx_playlist_tags_tag ON playlist_tags(tag_id, playlist_id);
CREATE INDEX IF NOT EXISTS idx_channel_tags_tag ON channel_tags(tag_id, channel_id);

CREATE TABLE IF NOT EXISTS playlist_tombstones (
  playlist_id TEXT PRIMARY KEY,
  observed_removed_at TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT 'authenticated_missing'
    CHECK (reason IN ('authenticated_missing', 'missing_from_library', 'explicit_user')),
  last_confirmed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_scans (
  playlist_id TEXT PRIMARY KEY REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  scanned_at TEXT NOT NULL,
  video_count INTEGER NOT NULL DEFAULT 0,
  unavailable_count INTEGER NOT NULL DEFAULT 0,
  scan_status TEXT NOT NULL DEFAULT '',
  scan_error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS playlist_items (
  playlist_id TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  video_id TEXT REFERENCES videos(video_id),
  membership_state TEXT NOT NULL DEFAULT 'current'
    CHECK (membership_state IN ('current', 'retained_unavailable', 'unresolved_unavailable')),
  unavailable_kind TEXT NOT NULL DEFAULT '',
  source_quality TEXT NOT NULL DEFAULT 'youtube',
  match_type TEXT NOT NULL DEFAULT '',
  match_confidence TEXT NOT NULL DEFAULT '',
  added_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  PRIMARY KEY (playlist_id, position),
  CHECK (video_id IS NOT NULL OR membership_state = 'unresolved_unavailable')
);

CREATE TABLE IF NOT EXISTS video_recovery (
  video_id TEXT PRIMARY KEY REFERENCES videos(video_id) ON DELETE CASCADE,
  archivarix_status TEXT NOT NULL DEFAULT '',
  archivarix_channel_id TEXT NOT NULL DEFAULT '',
  archive_capture_at TEXT,
  media_available INTEGER CHECK (media_available IN (0, 1)),
  searched_at TEXT,
  search_status TEXT NOT NULL DEFAULT '',
  search_error TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS my_activity_watch_events (
  event_id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL REFERENCES videos(video_id),
  watched_at TEXT NOT NULL,
  observed_title TEXT NOT NULL DEFAULT '',
  observed_url TEXT NOT NULL DEFAULT '',
  collected_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS my_activity_subscription_events (
  event_id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL REFERENCES channels(channel_id),
  subscribed_at TEXT NOT NULL,
  observed_title TEXT NOT NULL DEFAULT '',
  observed_url TEXT NOT NULL DEFAULT '',
  collected_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS history_events (
  event_id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL REFERENCES videos(video_id),
  watched_at TEXT,
  watch_date TEXT,
  time_precision TEXT NOT NULL CHECK (time_precision IN ('exact', 'date_only', 'unknown')),
  source_type TEXT NOT NULL DEFAULT '',
  match_type TEXT NOT NULL DEFAULT '',
  youtube_ordinal INTEGER,
  my_activity_event_id TEXT REFERENCES my_activity_watch_events(event_id),
  takeout_history_key TEXT,
  takeout_row_key TEXT,
  watch_progress_percent INTEGER NOT NULL DEFAULT 0,
  watch_resume_seconds INTEGER NOT NULL DEFAULT 0,
  observed_at TEXT,
  imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS metadata_worker_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  total INTEGER NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  delay_seconds REAL NOT NULL DEFAULT 0,
  requested_limit INTEGER NOT NULL DEFAULT 0,
  force INTEGER NOT NULL DEFAULT 0,
  stale_days INTEGER NOT NULL DEFAULT 0,
  last_video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS metadata_worker_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS worker_queue (
  queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_key TEXT NOT NULL UNIQUE,
  worker_type TEXT NOT NULL DEFAULT '',
  task_type TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  clip_id TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  playlist_id TEXT NOT NULL DEFAULT '',
  channel_title TEXT NOT NULL DEFAULT '',
  current_title TEXT NOT NULL DEFAULT '',
  source_key TEXT NOT NULL DEFAULT '',
  playlist_count INTEGER NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 0,
  manual INTEGER NOT NULL DEFAULT 0,
  plugin_subject_id TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_queue_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  queue_id INTEGER NOT NULL,
  operation TEXT NOT NULL CHECK(operation IN ('upsert', 'remove')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS external_service_blocks (
  service TEXT PRIMARY KEY,
  reason_code TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT '',
  blocked_at TEXT NOT NULL,
  retry_after TEXT NOT NULL DEFAULT '',
  run_id TEXT NOT NULL DEFAULT '',
  queue_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cookie_auth_status (
  service TEXT PRIMARY KEY CHECK(service IN ('youtube', 'google', 'archivarix')),
  status TEXT NOT NULL CHECK(status IN ('valid', 'expired', 'rejected', 'missing', 'error')),
  checked_at TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT ''
);

CREATE TRIGGER IF NOT EXISTS worker_queue_event_insert
AFTER INSERT ON worker_queue
BEGIN
  INSERT INTO worker_queue_events(queue_id, operation, created_at)
  VALUES (NEW.queue_id, 'upsert', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
END;

CREATE TRIGGER IF NOT EXISTS worker_queue_event_update
AFTER UPDATE ON worker_queue
BEGIN
  INSERT INTO worker_queue_events(queue_id, operation, created_at)
  VALUES (NEW.queue_id, 'upsert', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
END;

CREATE TRIGGER IF NOT EXISTS worker_queue_event_delete
AFTER DELETE ON worker_queue
BEGIN
  INSERT INTO worker_queue_events(queue_id, operation, created_at)
  VALUES (OLD.queue_id, 'remove', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
END;

CREATE TRIGGER IF NOT EXISTS worker_queue_events_prune
AFTER INSERT ON worker_queue_events
WHEN NEW.event_id % 1000 = 0
BEGIN
  DELETE FROM worker_queue_events WHERE event_id < NEW.event_id - 100000;
END;

CREATE TABLE IF NOT EXISTS playlist_scan_worker_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  total INTEGER NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  delay_seconds REAL NOT NULL DEFAULT 0,
  requested_limit INTEGER NOT NULL DEFAULT 0,
  force INTEGER NOT NULL DEFAULT 0,
  last_playlist_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS playlist_scan_worker_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT '',
  playlist_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS live_history_worker_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  total INTEGER NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  delay_seconds REAL NOT NULL DEFAULT 0,
  requested_limit INTEGER NOT NULL DEFAULT 0,
  last_video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS live_history_worker_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS placeholder_recovery_worker_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  total INTEGER NOT NULL DEFAULT 1,
  processed INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  queue_id INTEGER NOT NULL DEFAULT 0,
  video_id TEXT NOT NULL DEFAULT '',
  playlist_id TEXT NOT NULL DEFAULT '',
  request_started_at TEXT,
  request_count INTEGER NOT NULL DEFAULT 0,
  recovery_status TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS placeholder_recovery_worker_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT '',
  video_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS plugin_worker_runs (
  run_id TEXT PRIMARY KEY,
  plugin_id TEXT NOT NULL,
  worker_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  queue_id INTEGER NOT NULL DEFAULT 0,
  subject_id TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL DEFAULT '',
  processed INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS plugin_worker_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL DEFAULT '',
  plugin_id TEXT NOT NULL,
  worker_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT '',
  subject_id TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_groups_parent_position ON groups(parent_key, position);
CREATE INDEX IF NOT EXISTS idx_group_playlists_position ON group_playlists(group_key, position);
CREATE INDEX IF NOT EXISTS idx_channels_title ON channels(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_channels_fetch ON channels(fetch_status, fetched_at);
CREATE INDEX IF NOT EXISTS idx_channel_featured_channels_order
  ON channel_featured_channels(owner_channel_id, position);
CREATE INDEX IF NOT EXISTS idx_videos_title ON videos(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_videos_fetch ON videos(fetch_status, fetched_at);
CREATE INDEX IF NOT EXISTS idx_videos_availability ON videos(is_playable, availability);
CREATE INDEX IF NOT EXISTS idx_clips_owner ON clips(ownership, owner_channel_id);
CREATE INDEX IF NOT EXISTS idx_clips_source_video ON clips(source_video_id);
CREATE INDEX IF NOT EXISTS idx_clips_fetch ON clips(fetch_status, fetched_at);
CREATE INDEX IF NOT EXISTS idx_clips_feed_ordinal ON clips(youtube_feed_ordinal);
CREATE INDEX IF NOT EXISTS idx_playlist_items_video ON playlist_items(video_id);
CREATE INDEX IF NOT EXISTS idx_playlist_collaborators_order
  ON playlist_collaborators(playlist_id, position);
CREATE INDEX IF NOT EXISTS idx_my_activity_watch_video_time
  ON my_activity_watch_events(video_id, watched_at);
CREATE INDEX IF NOT EXISTS idx_my_activity_subscription_channel_time
  ON my_activity_subscription_events(channel_id, subscribed_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_my_activity_watch_occurrence
  ON my_activity_watch_events(video_id, watched_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_my_activity_subscription_occurrence
  ON my_activity_subscription_events(channel_id, subscribed_at);
CREATE INDEX IF NOT EXISTS idx_playlist_items_state ON playlist_items(membership_state, playlist_id, position);
CREATE INDEX IF NOT EXISTS idx_video_recovery_status ON video_recovery(search_status, searched_at);
CREATE INDEX IF NOT EXISTS idx_history_events_video ON history_events(video_id);
CREATE INDEX IF NOT EXISTS idx_history_events_date ON history_events(watch_date, youtube_ordinal);
CREATE INDEX IF NOT EXISTS idx_history_events_time ON history_events(watched_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_history_my_activity_event
  ON history_events(my_activity_event_id)
  WHERE my_activity_event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_history_events_takeout
  ON history_events(takeout_history_key, takeout_row_key)
  WHERE takeout_history_key IS NOT NULL AND takeout_row_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_worker_queue_order ON worker_queue(worker_type, priority, queue_id);
CREATE INDEX IF NOT EXISTS idx_worker_queue_task ON worker_queue(task_type, updated_at);
CREATE INDEX IF NOT EXISTS idx_metadata_worker_log_run ON metadata_worker_log(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_playlist_scan_worker_log_run ON playlist_scan_worker_log(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_live_history_worker_log_run ON live_history_worker_log(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_placeholder_recovery_worker_log_run ON placeholder_recovery_worker_log(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_metadata_worker_log_created
  ON metadata_worker_log(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_metadata_worker_log_level
  ON metadata_worker_log(level);
CREATE INDEX IF NOT EXISTS idx_playlist_scan_worker_log_created
  ON playlist_scan_worker_log(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_playlist_scan_worker_log_level
  ON playlist_scan_worker_log(level);
CREATE INDEX IF NOT EXISTS idx_live_history_worker_log_created
  ON live_history_worker_log(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_live_history_worker_log_level
  ON live_history_worker_log(level);
CREATE INDEX IF NOT EXISTS idx_placeholder_recovery_worker_log_created
  ON placeholder_recovery_worker_log(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_placeholder_recovery_worker_log_level
  ON placeholder_recovery_worker_log(level);
CREATE INDEX IF NOT EXISTS idx_plugin_worker_runs_process
  ON plugin_worker_runs(plugin_id, worker_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_plugin_worker_runs_subject
  ON plugin_worker_runs(plugin_id, worker_id, subject_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_plugin_worker_log_process
  ON plugin_worker_log(plugin_id, worker_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_plugin_worker_log_created
  ON plugin_worker_log(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_plugin_worker_log_level
  ON plugin_worker_log(level);
CREATE INDEX IF NOT EXISTS idx_plugin_worker_log_plugin_level
  ON plugin_worker_log(plugin_id, level);
