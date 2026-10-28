"""从现有会话、任务和事件投影侧栏摘要，不回写历史记录。"""

import json
from pathlib import PureWindowsPath
from typing import Any


ACTIVITY_KINDS = (
    "question.requested", "question.answered", "question.expired",
    "user.message", "agent.text", "agent.tool", "agent.plan", "run.status", "error",
    "permission.requested", "permission.answered", "permission.expired",
    "task.created", "task.status", "task.worktree_ready", "feedback.created", "feedback.pulled",
)
ACTIVITY_SQL = ",".join(f"'{kind}'" for kind in ACTIVITY_KINDS)
TITLE_SQL = """CASE WHEN e.kind='session.title' THEN json_extract(e.payload,'$.title')
    WHEN e.kind='raw'
      AND json_extract(e.payload,'$.raw.method')='session/update'
      AND json_extract(e.payload,'$.raw.params.update.sessionUpdate')='session_info_update'
    THEN json_extract(e.payload,'$.raw.params.update.title') END"""
SUMMARY_SQL = f"""SELECT s.*,
    (SELECT {TITLE_SQL} FROM events e WHERE e.session_id=s.id
       AND e.kind IN ('session.title','raw')
       AND typeof({TITLE_SQL})='text'
       AND length(trim({TITLE_SQL}, char(9)||char(10)||char(13)||' '))>0
       ORDER BY e.id DESC LIMIT 1) AS agent_title,
    (SELECT goal FROM tasks WHERE child_session_id=s.id ORDER BY rowid LIMIT 1) AS goal,
    (SELECT prompt FROM runs WHERE session_id=s.id ORDER BY rowid LIMIT 1) AS first_prompt,
    coalesce(activity.created_at,s.created_at) AS updated_at,
    coalesce(activity.id,0) AS activity_event_id
FROM sessions s
LEFT JOIN events activity ON activity.id=(SELECT e.id FROM events e WHERE e.session_id=s.id
    AND e.kind IN ({ACTIVITY_SQL}) ORDER BY e.id DESC LIMIT 1)
"""


def session_summary(row: dict[str, Any]) -> dict[str, Any]:
    titles = (row.pop("agent_title"), row.pop("goal"), row.pop("first_prompt"))
    row["title"] = next((" ".join(text.split()) for text in titles if text and text.strip()), "新会话")
    return row


def project_summary(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    snapshot = row.pop("team_snapshot", None)
    mode = row.pop("context_mode", None)
    if snapshot:
        row["team"] = {**json.loads(snapshot), "context_mode": mode}
    return {**row, "name": PureWindowsPath(row["path"]).name}
