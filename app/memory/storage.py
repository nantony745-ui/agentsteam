"""SQLite 只保存来源、配置、投递记录；事实与检索留在 Hindsight。"""

import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from app.db.storage import RequestConflictError
from app.memory.models import MemoryUnavailable
from app.memory.ingestion import capture_content


def now():
    return datetime.now(UTC).isoformat()


async def insert_source(db, source_id, content, metadata, session_id, run_id, member_id=None):
    stamp = now()
    await db.execute(
        "INSERT INTO memory_sources(id,member_id,session_id,run_id,version,deleted,created_at) VALUES (?,?,?,?,1,0,?)",
        (source_id, member_id, session_id, run_id, stamp),
    )
    await insert_revision(db, source_id, 1, "retain", content, metadata)


async def insert_revision(db, source_id, version, action, content, metadata):
    operation_id = str(uuid5(NAMESPACE_URL, f"agentsteam:{source_id}:{version}"))
    await db.execute(
        "INSERT INTO memory_operations(id,source_id,version,action,content,metadata,status,attempts,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,'queued',0,?,?)",
        (operation_id, source_id, version, action, content, json.dumps(metadata, ensure_ascii=False), now(), now()),
    )


async def member_enabled(db, member_id):
    if member_id is None:
        return True
    async with db.execute("SELECT enabled FROM employee_memory_settings WHERE member_id=?", (member_id,)) as cursor:
        row = await cursor.fetchone()
    return bool(row and row[0])


async def capture_started_run(db, run_id, member_id):
    async with db.execute("SELECT config FROM memory_settings WHERE id=1") as cursor:
        row = await cursor.fetchone()
    config = json.loads(row[0]) if row else {}
    allowed = bool(config.get("enabled") and config.get("auto_retain") and await member_enabled(db, member_id))
    await db.execute("INSERT OR IGNORE INTO memory_run_scopes VALUES (?,?,?)", (run_id, member_id, allowed))


async def capture_completed_run(db, run_id):
    async with db.execute("SELECT config FROM memory_settings WHERE id=1") as cursor:
        row = await cursor.fetchone()
    if row is None:
        return
    config = json.loads(row[0])
    if not (config["enabled"] and config["auto_retain"]):
        return
    async with db.execute("SELECT * FROM memory_run_scopes WHERE run_id=?", (run_id,)) as cursor:
        scope = await cursor.fetchone()
    if scope is None or not scope["capture_allowed"] or not await member_enabled(db, scope["member_id"]):
        return
    member_id = scope["member_id"]
    async with db.execute("SELECT * FROM runs WHERE id=? AND status='completed'", (run_id,)) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return
    run = dict(row)
    async with db.execute(
        "SELECT run_id FROM memory_capture_turns WHERE run_id=? "
        "UNION ALL SELECT run_id FROM memory_sources WHERE id=?", (run_id, f"run:{run_id}"),
    ) as cursor:
        if await cursor.fetchone():
            return
    content, metadata = await completed_run_content(db, run)
    await db.execute(
        "INSERT INTO memory_capture_turns(run_id,session_id,content,metadata,member_id) VALUES (?,?,?,?,?)",
        (run_id, run["session_id"], content, json.dumps(metadata, ensure_ascii=False), member_id),
    )
    while await flush_capture_batch(db, run["session_id"], member_id, config.get("auto_retain_turns", 3)):
        pass


async def completed_run_content(db, run):
    run_id = run["id"]
    async with db.execute(
        "SELECT payload FROM events WHERE session_id=? AND kind='feedback.pulled' AND created_at>=? AND created_at<=? ORDER BY id",
        (run["session_id"], run["started_at"], run["finished_at"]),
    ) as cursor:
        feedback = [json.loads(row[0])["message"] for row in await cursor.fetchall()]
    # ACP 没有统一的 final 标志：最后一次工具事件之后的 message 文本作为最终回复。
    async with db.execute(
        "SELECT payload FROM events WHERE run_id=? AND kind='agent.text' AND id>COALESCE("
        "(SELECT MAX(id) FROM events WHERE run_id=? AND kind='agent.tool'),0) ORDER BY id", (run_id, run_id),
    ) as cursor:
        chunks = [json.loads(row[0]) for row in await cursor.fetchall()]
    answer = "".join(item.get("text", "") for item in chunks if item.get("channel") == "message")
    metadata = {"origin": "completed_run", "run_id": run_id, "session_id": run["session_id"],
                "task_id": run["task_id"], "verification": "unverified", "task_status_at_capture": "review" if run["task_id"] else "none"}
    return capture_content(run["prompt"], feedback, answer, metadata)


async def flush_capture_batch(db, session_id, member_id, threshold):
    async with db.execute(
        "SELECT * FROM memory_capture_turns WHERE member_id IS ? AND (? IS NOT NULL OR session_id=?) "
        "AND source_id IS NULL ORDER BY rowid LIMIT ?",
        (member_id, member_id, session_id, threshold),
    ) as cursor:
        turns = await cursor.fetchall()
    if len(turns) < threshold:
        return False
    for turn in turns:
        await insert_source(db, f"run:{turn['run_id']}", turn["content"], json.loads(turn["metadata"]),
                            turn["session_id"], turn["run_id"], member_id)
    await db.executemany(
        "UPDATE memory_capture_turns SET source_id=? WHERE run_id=?",
        [(f"run:{turn['run_id']}", turn["run_id"]) for turn in turns],
    )
    return True


class MemoryStore:
    def __init__(self, storage):
        self.storage = storage

    async def config(self):
        row = await self.storage._fetch_one("SELECT config FROM memory_settings WHERE id=1", ())
        return {"auto_retain_turns": 3, **json.loads(row["config"])} if row else None

    async def enabled(self, member_id):
        async with self.storage.read_transaction() as db:
            return await member_enabled(db, member_id)

    async def set_enabled(self, member_id, enabled):
        async with self.storage.transaction() as db:
            await db.execute("INSERT INTO employee_memory_settings VALUES (?,?) "
                             "ON CONFLICT(member_id) DO UPDATE SET enabled=excluded.enabled", (member_id, enabled))

    async def record_context(self, run_id, member_id, status, context):
        from app.db.storage import _insert_event
        async with self.storage.transaction() as db:
            async with db.execute("SELECT config FROM memory_settings WHERE id=1") as cursor:
                row = await cursor.fetchone()
            config = json.loads(row[0]) if row else {}
            if not config.get("enabled") or not config.get("auto_recall") or not await member_enabled(db, member_id):
                status, context = "disabled", ""
            stamp = now()
            await db.execute("INSERT OR REPLACE INTO memory_contexts(run_id,status,context,created_at,member_id) VALUES (?,?,?,?,?)",
                             (run_id, status, context, stamp, member_id))
            await _insert_event(db, run_id, "memory.context", {"status": status, "context": context, "member_id": member_id}, stamp)
        return context

    async def save_config(self, config):
        async with self.storage.transaction() as db:
            await db.execute("INSERT INTO memory_settings(id,config) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config",
                             (json.dumps(config, ensure_ascii=False),))

    async def retain(self, request_id, content, session_id, run_id, member_id=None):
        identity = f"agentsteam:manual:{session_id}:{request_id}"
        if member_id is not None:
            identity += f":employee:{member_id}"
        source_id = str(uuid5(NAMESPACE_URL, identity))
        metadata = {"origin": "mcp" if session_id else "manual", "verification": "unverified",
                    "session_id": session_id, "run_id": run_id}
        async with self.storage.transaction() as db:
            if session_id and not await member_enabled(db, member_id):
                raise MemoryUnavailable("当前队员的长期记忆未启用")
            async with db.execute("SELECT content FROM memory_operations WHERE source_id=? AND version=1", (source_id,)) as cursor:
                old = await cursor.fetchone()
            if old:
                if old[0] != content:
                    raise RequestConflictError("同一 request_id 已用于其他内容")
            else:
                await insert_source(db, source_id, content, metadata, session_id, run_id, member_id)
        return await self.source(source_id, member_id)

    async def sources(self, member_id=None):
        return await self.storage._fetch_all(
            "SELECT s.*,o.status,o.error,o.updated_at FROM memory_sources s JOIN memory_operations o "
            "ON o.source_id=s.id AND o.version=s.version WHERE s.member_id IS ? ORDER BY s.created_at DESC LIMIT 200", (member_id,))

    async def source(self, source_id, member_id=None):
        row = await self.storage._fetch_one("SELECT * FROM memory_sources WHERE id=? AND member_id IS ?", (source_id, member_id))
        if row is None:
            raise KeyError(source_id)
        row["revisions"] = await self.storage._fetch_all("SELECT * FROM memory_operations WHERE source_id=? ORDER BY version", (source_id,))
        for revision in row["revisions"]:
            revision["metadata"] = json.loads(revision["metadata"])
        return row

    async def revise(self, source_id, version, content, delete, member_id=None):
        async with self.storage.transaction() as db:
            async with db.execute("SELECT * FROM memory_sources WHERE id=? AND member_id IS ?", (source_id, member_id)) as cursor:
                source = await cursor.fetchone()
            if source is None:
                raise KeyError(source_id)
            if source["version"] != version or source["deleted"]:
                raise RequestConflictError("来源已被修改或删除，请刷新")
            async with db.execute("SELECT metadata FROM memory_operations WHERE source_id=? AND version=?", (source_id, version)) as cursor:
                metadata = json.loads((await cursor.fetchone())[0])
            metadata["revision_origin"] = "user"
            # 用户管理需要先结清旧投递（含上游已接收但本地未确认的情况），再应用修订或删除。
            await db.execute("UPDATE memory_operations SET metadata=json_set(metadata,'$.revision_origin','user') "
                             "WHERE source_id=? AND status='queued'", (source_id,))
            await insert_revision(db, source_id, version + 1, "delete" if delete else "retain", content, metadata)
            await db.execute("UPDATE memory_sources SET version=?,deleted=? WHERE id=?", (version + 1, int(delete), source_id))
        return await self.source(source_id, member_id)

    async def operations(self, member_id=None):
        return await self.storage._fetch_all(
            "SELECT o.id,o.source_id,o.version,o.action,o.status,o.error,o.attempts,o.created_at,o.updated_at,"
            "json_extract(o.metadata,'$.origin') AS origin,json_extract(o.metadata,'$.revision_origin') AS revision_origin "
            "FROM memory_operations o JOIN memory_sources s ON s.id=o.source_id "
            "WHERE s.member_id IS ? ORDER BY o.created_at DESC LIMIT 200", (member_id,))

    async def pending(self):
        return await self.storage._fetch_all(
            "SELECT o.*,s.member_id FROM memory_operations o JOIN memory_sources s ON s.id=o.source_id "
            "WHERE o.status IN ('queued','accepted','written','consolidating') "
            "AND (o.status!='queued' OR s.member_id IS NULL OR json_extract(o.metadata,'$.origin')='manual' "
            "OR json_extract(o.metadata,'$.revision_origin')='user' OR EXISTS "
            "(SELECT 1 FROM employee_memory_settings e WHERE e.member_id=s.member_id AND e.enabled=1)) "
            "AND NOT EXISTS(SELECT 1 FROM memory_operations p WHERE p.source_id=o.source_id AND p.version<o.version "
            "AND p.status NOT IN ('complete','deleted','failed','written','consolidating')) ORDER BY o.created_at LIMIT 32", ())

    async def update(self, operation_id, status, error=None, attempt=False):
        async with self.storage.transaction() as db:
            await db.execute("UPDATE memory_operations SET status=?,error=?,attempts=attempts+?,updated_at=? WHERE id=?",
                             (status, error, int(attempt), now(), operation_id))

    async def retry(self, operation_id, member_id=None):
        async with self.storage.transaction() as db:
            async with db.execute("SELECT o.* FROM memory_operations o JOIN memory_sources s ON s.id=o.source_id "
                                  "WHERE o.id=? AND s.member_id IS ?", (operation_id, member_id)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise KeyError(operation_id)
            async with db.execute("SELECT version FROM memory_sources WHERE id=?", (row["source_id"],)) as cursor:
                version = (await cursor.fetchone())[0]
            if row["status"] != "failed" or row["version"] != version:
                raise RequestConflictError("仅能重试当前版本的失败操作")
            metadata = {**json.loads(row["metadata"]), "revision_origin": "user"}
            await db.execute("UPDATE memory_operations SET status='queued',attempts=0,error=NULL,metadata=? WHERE id=?",
                             (json.dumps(metadata, ensure_ascii=False), operation_id))
