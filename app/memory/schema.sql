CREATE TABLE IF NOT EXISTS memory_settings (
  id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_sources (
  id TEXT PRIMARY KEY, member_id TEXT, session_id TEXT, run_id TEXT,
  version INTEGER NOT NULL, deleted INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_operations (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES memory_sources(id),
  version INTEGER NOT NULL, action TEXT NOT NULL, content TEXT NOT NULL,
  metadata TEXT NOT NULL, status TEXT NOT NULL, error TEXT, attempts INTEGER NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(source_id,version)
);
CREATE INDEX IF NOT EXISTS memory_operations_pending ON memory_operations(status,created_at);
CREATE TABLE IF NOT EXISTS memory_contexts (
  run_id TEXT PRIMARY KEY, status TEXT NOT NULL, context TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_capture_turns (
  run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
  content TEXT NOT NULL, metadata TEXT NOT NULL,
  source_id TEXT REFERENCES memory_sources(id)
);
CREATE INDEX IF NOT EXISTS memory_capture_pending ON memory_capture_turns(session_id,source_id);
CREATE TABLE IF NOT EXISTS employee_memory_settings (
  member_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memory_run_scopes (
  run_id TEXT PRIMARY KEY, member_id TEXT, capture_allowed INTEGER NOT NULL
);
