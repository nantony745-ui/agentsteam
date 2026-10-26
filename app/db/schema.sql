PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS harness_settings (
  harness TEXT PRIMARY KEY, config_values TEXT NOT NULL,
  revision INTEGER NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT UNIQUE NOT NULL,
  check_argv TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
  parent_id TEXT REFERENCES sessions(id), harness TEXT NOT NULL, cwd TEXT NOT NULL,
  native_id TEXT, process_pid INTEGER, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
  parent_session_id TEXT NOT NULL REFERENCES sessions(id),
  child_session_id TEXT NOT NULL REFERENCES sessions(id),
  request_id TEXT NOT NULL, goal TEXT NOT NULL, acceptance TEXT NOT NULL,
  status TEXT NOT NULL, current_run_id TEXT REFERENCES runs(id),
  base_sha TEXT, base_branch TEXT, worktree TEXT, branch TEXT,
  check_sha TEXT, check_result TEXT, accepted_sha TEXT,
  summary TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(parent_session_id, request_id)
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  task_id TEXT REFERENCES tasks(id), attempt INTEGER NOT NULL,
  request_id TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL,
  result TEXT, error TEXT, stop_reason TEXT,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
  UNIQUE(session_id, request_id), UNIQUE(task_id, attempt)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(id),
  session_id TEXT REFERENCES sessions(id), run_id TEXT REFERENCES runs(id),
  task_id TEXT REFERENCES tasks(id), kind TEXT NOT NULL,
  payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_project ON events(project_id,id);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id,id);
CREATE INDEX IF NOT EXISTS events_session_title ON events(session_id,id)
  WHERE kind IN ('session.title','raw');
CREATE INDEX IF NOT EXISTS events_session_metadata ON events(session_id,kind,id)
  WHERE kind IN ('session.model','session.title','session.config');
CREATE INDEX IF NOT EXISTS runs_session ON runs(session_id);
CREATE INDEX IF NOT EXISTS tasks_child_session ON tasks(child_session_id);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
CREATE INDEX IF NOT EXISTS events_tool ON events(run_id,json_extract(payload,'$.toolCallId'),id)
  WHERE kind='agent.tool';
CREATE TABLE IF NOT EXISTS event_details (
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  field TEXT NOT NULL, part INTEGER NOT NULL,
  codec TEXT NOT NULL CHECK(codec IN ('utf8','zlib')),
  raw_bytes INTEGER NOT NULL CHECK(raw_bytes<=65536), data BLOB NOT NULL,
  PRIMARY KEY(event_id,part)
);
CREATE TABLE IF NOT EXISTS interactions (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  run_id TEXT REFERENCES runs(id), kind TEXT NOT NULL, status TEXT NOT NULL,
  payload TEXT NOT NULL, response TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, member_ids TEXT NOT NULL,
  leader_id TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_teams (
  project_id TEXT PRIMARY KEY REFERENCES projects(id), snapshot TEXT NOT NULL,
  context_mode TEXT NOT NULL CHECK(context_mode IN ('continue','new'))
);
CREATE TABLE IF NOT EXISTS team_sessions (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  leader_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  employee_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS team_sessions_member ON team_sessions(leader_session_id,employee_id);
CREATE TABLE IF NOT EXISTS team_tasks (
  task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  employee_id TEXT NOT NULL, context_mode TEXT
);
