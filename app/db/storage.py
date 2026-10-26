"""SQLite 单写连接与独立 WAL 读连接；事务内不能嵌套调用 Storage 方法。

参照 Codeg db/mod.rs::apply_sqlite_pragmas 与 work_task_event::Model，
以应用内锁串行写入；状态和对应事件应在同一个 transaction 中写入。
"""

import asyncio
import json
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from app.domain.models import FeedbackCreate, MessageCreate, ProjectCreate, RunStatus, SessionCreate, TaskCreate, TaskStatus
from app.memory import SCHEMA_PATH as MEMORY_SCHEMA_PATH
from app.db.lease import DatabaseLease
from app.db.event_codec import business_payload, compact, pack_payload, restore_payload, unpack_part
from app.db.session_summary import SUMMARY_SQL, project_summary, session_summary
from app.delivery.git_delivery import DeliveryConflictError, WorkingDirectoryError


RUN_TERMINALS = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELED, RunStatus.INTERRUPTED})
RUN_TRANSITIONS = {
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELED},
    RunStatus.RUNNING: {RunStatus.WAITING_PERMISSION, RunStatus.CANCEL_REQUESTED, *RUN_TERMINALS},
    RunStatus.WAITING_PERMISSION: {RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED, *RUN_TERMINALS},
    RunStatus.CANCEL_REQUESTED: RUN_TERMINALS,
}


class RequestConflictError(ValueError):
    """同一会话的 request_id 已经用于不同输入。"""


class PermissionConflictError(ValueError):
    """权限已解决，或已不属于当前活动执行。"""


async def _permission_run(db: aiosqlite.Connection, run_id: str) -> dict[str, Any]:
    async with db.execute(
        "SELECT * FROM runs WHERE id=? AND status IN ('running','waiting_permission') "
        "AND (task_id IS NULL OR EXISTS (SELECT 1 FROM tasks "
        "WHERE tasks.id=runs.task_id AND tasks.current_run_id=runs.id))", (run_id,),
    ) as cursor:
        run = await cursor.fetchone()
    if run is None:
        raise PermissionConflictError("权限所属 Run 已取消、结束或不再活动")
    return dict(run)


async def _insert_event(
    db: aiosqlite.Connection, run_id: str, kind: str, payload: dict[str, Any], created_at: str,
) -> dict[str, Any]:
    payload, parts = pack_payload(kind, payload)
    return await _insert_packed_event(db, run_id, kind, payload, parts, created_at)


async def _insert_packed_event(
    db: aiosqlite.Connection, run_id: str, kind: str, payload: dict, parts: list, created_at: str,
) -> dict[str, Any]:
    async with db.execute(
        "INSERT INTO events (project_id,session_id,run_id,task_id,kind,payload,created_at) "
        "SELECT s.project_id,r.session_id,r.id,r.task_id,?,?,? "
        "FROM runs r JOIN sessions s ON s.id=r.session_id WHERE r.id=? RETURNING *",
        (kind, compact(payload), created_at, run_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise ValueError(f"Run 不存在：{run_id}")
    if parts:
        await db.executemany(
            "INSERT INTO event_details(event_id,field,part,codec,raw_bytes,data) VALUES (?,?,?,?,?,?)",
            [(row["id"], *part) for part in parts],
        )
    return dict(row)


async def _transition_run(
    db: aiosqlite.Connection, run_id: str, expected_statuses: Collection[RunStatus],
    new_status: RunStatus, payload: dict[str, Any],
) -> bool:
    async with db.execute("SELECT status FROM runs WHERE id=?", (run_id,)) as cursor:
        run = await cursor.fetchone()
    if run is None or run["status"] not in expected_statuses:
        return False
    previous = run["status"]
    if new_status not in RUN_TRANSITIONS.get(previous, ()):
        return False
    now = datetime.now(UTC).isoformat()
    changes: dict[str, Any] = {"status": new_status}
    if previous == RunStatus.QUEUED and new_status == RunStatus.RUNNING:
        changes["started_at"] = now
    if new_status in RUN_TERMINALS:
        changes["finished_at"] = now
        if "result" not in payload:
            chunks = []
            async with db.execute("SELECT payload FROM events WHERE run_id=? AND kind='agent.text' ORDER BY id", (run_id,)) as cursor:
                async for event in cursor:
                    text = json.loads(event["payload"])
                    if text["channel"] == "message":
                        chunks.append(text["text"])
            payload = {**payload, "result": "".join(chunks)}
    changes.update({key: payload[key] for key in ("result", "error", "stop_reason") if key in payload})
    assignments = ",".join(f"{key}=?" for key in changes)
    async with db.execute(
        f"UPDATE runs SET {assignments} WHERE id=? AND status=? "
        "AND (task_id IS NULL OR EXISTS (SELECT 1 FROM tasks "
        "WHERE tasks.id=runs.task_id AND tasks.current_run_id=runs.id))",
        (*changes.values(), run_id, previous),
    ) as cursor:
        changed = cursor.rowcount == 1
    if not changed:
        return False
    await _insert_event(db, run_id, "run.status", {**payload, "from": previous, "status": new_status}, now)
    await _sync_task(db, run_id, new_status, payload, now)
    if new_status == RunStatus.COMPLETED:
        from app.memory.storage import capture_completed_run
        await capture_completed_run(db, run_id)
    return True


async def _insert_task_event(
    db: aiosqlite.Connection, run_id: str, kind: str, payload: dict[str, Any], created_at: str,
) -> None:
    payload = {key: value for key, value in payload.items() if key != "result"}
    payload, parts = pack_payload(kind, payload)
    cursor = await db.execute(
        "INSERT INTO events (project_id,session_id,run_id,task_id,kind,payload,created_at) "
        "SELECT t.project_id,t.parent_session_id,r.id,t.id,?,?,? "
        "FROM tasks t JOIN runs r ON r.task_id=t.id WHERE r.id=? AND t.current_run_id=r.id",
        (kind, compact(payload), created_at, run_id),
    )
    if cursor.rowcount:
        await db.executemany("INSERT INTO event_details VALUES (?,?,?,?,?,?)",
                             [(cursor.lastrowid, *part) for part in parts])
    await cursor.close()


async def _sync_task(
    db: aiosqlite.Connection, run_id: str, status: RunStatus, payload: dict[str, Any], now: str,
) -> None:
    task_status = {
        RunStatus.RUNNING: TaskStatus.RUNNING,
        RunStatus.WAITING_PERMISSION: TaskStatus.RUNNING,
        RunStatus.CANCEL_REQUESTED: TaskStatus.RUNNING,
        RunStatus.COMPLETED: TaskStatus.REVIEW,
        RunStatus.FAILED: TaskStatus.FAILED,
        RunStatus.INTERRUPTED: TaskStatus.FAILED,
        RunStatus.CANCELED: TaskStatus.CANCELED,
    }[status]
    async with db.execute(
        "SELECT t.status FROM tasks t JOIN runs r ON r.task_id=t.id "
        "WHERE r.id=? AND t.current_run_id=r.id", (run_id,),
    ) as cursor:
        task = await cursor.fetchone()
    if task is None or task["status"] == task_status:
        return
    await db.execute(
        "UPDATE tasks SET status=?,summary=?,error=?,updated_at=? WHERE current_run_id=? "
        "AND id=(SELECT task_id FROM runs WHERE id=?)",
        (task_status, (payload.get("result") or "")[:160], payload.get("error"), now, run_id, run_id),
    )
    await _insert_task_event(db, run_id, "task.status", {
        **payload, "from": task["status"], "status": task_status,
    }, now)


async def _create_task_rows(
    db: aiosqlite.Connection, parent: aiosqlite.Row, data: TaskCreate,
    child_session_id: str | None = None, reuse: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    now = datetime.now(UTC).isoformat()
    child_id, task_id, run_id = child_session_id or str(uuid4()), str(uuid4()), str(uuid4())
    prompt = f"目标：\n{data.goal}\n\n验收要求：\n{data.acceptance}"
    if reuse:
        async with db.execute("SELECT * FROM sessions WHERE id=? AND parent_id=?", (child_id, parent["id"])) as cursor:
            child = dict(await cursor.fetchone())
    else:
        async with db.execute(
            "INSERT INTO sessions (id,project_id,parent_id,harness,cwd,created_at) "
            "VALUES (?,?,?,?,?,?) RETURNING *",
            (child_id, parent["project_id"], parent["id"], data.harness, data.working_dir or parent["cwd"], now),
        ) as cursor:
            child = dict(await cursor.fetchone())
    await db.execute(
        "INSERT INTO tasks (id,project_id,parent_session_id,child_session_id,request_id,goal,acceptance,"
        "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'queued',?,?)",
        (task_id, parent["project_id"], parent["id"], child_id, data.request_id,
         data.goal, data.acceptance, now, now),
    )
    async with db.execute(
        "INSERT INTO runs (id,session_id,task_id,attempt,request_id,prompt,status,created_at) "
        "VALUES (?,?,?,1,?,?,'queued',?) RETURNING *",
        (run_id, child_id, task_id, data.request_id, prompt, now),
    ) as cursor:
        run = dict(await cursor.fetchone())
    async with db.execute(
        "UPDATE tasks SET current_run_id=? WHERE id=? RETURNING *", (run_id, task_id),
    ) as cursor:
        task = dict(await cursor.fetchone())
    if data.employee_id:
        if not reuse:
            await db.execute("INSERT INTO team_sessions VALUES (?,?,?)", (child_id, parent["id"], data.employee_id))
        await db.execute("INSERT INTO team_tasks VALUES (?,?,?)", (task_id, data.employee_id, data.context_mode))
    await _insert_task_event(db, run_id, "task.created", {
        "task_id": task_id, "child_session_id": child_id, "run_id": run_id, "status": "queued",
        "harness": data.harness, "goal": data.goal, "acceptance": data.acceptance,
        "working_dir": data.working_dir,
    }, now)
    await _insert_event(db, run_id, "user.message", {"message": prompt}, now)
    await _insert_event(db, run_id, "run.status", {"from": None, "status": "queued"}, now)
    return task, child, run


async def _insert_feedback_event(
    db: aiosqlite.Connection, interaction: dict[str, Any], kind: str, status: str, now: str,
) -> None:
    payload = {"feedback_id": interaction["id"], "status": status,
               "message": json.loads(interaction["payload"])["message"]}
    payload, parts = pack_payload(kind, payload)
    cursor = await db.execute(
        "INSERT INTO events (project_id,session_id,kind,payload,created_at) "
        "SELECT project_id,id,?,?,? FROM sessions WHERE id=?",
        (kind, compact(payload), now, interaction["session_id"]),
    )

    await db.executemany("INSERT INTO event_details VALUES (?,?,?,?,?,?)",
                         [(cursor.lastrowid, *part) for part in parts])
    await cursor.close()


def validate_project_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("项目路径必须是绝对路径")
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise ValueError("项目路径必须是目录")
    return path


def _event_page(records: list[dict]) -> list[dict]:
    rows, size = [], 0
    for item in records:
        payload, _ = pack_payload(item["kind"], business_payload(item["kind"], json.loads(item["payload"])))
        item["payload"] = compact(payload)
        item_size = len(compact(item).encode("utf-8"))
        if rows and size + item_size > 256 * 1024:
            break
        rows.append(item)
        size += item_size
    return rows


def _detail_page(event_id: int, event: dict, parts: list[dict], after_part: int) -> dict:
    if not parts:
        _, encoded = pack_payload(event["kind"], business_payload(event["kind"], json.loads(event["payload"])))
        parts = [dict(field=field, part=part, codec=codec, raw_bytes=size, data=blob)
                 for field, part, codec, size, blob in encoded if part > after_part][:2]
    if not parts:
        return {"event_id": event_id, "parts": [], "next": None}
    part = parts[0]
    return {"event_id": event_id, "parts": [{"field": part["field"], "part": part["part"],
            "text": unpack_part(part["codec"], part["data"])}],
            "next": part["part"] if len(parts) > 1 else None}


class Storage:
    def __init__(self, path: Path, *, exclusive: bool = False):
        self.path = path
        self._lease = DatabaseLease(path) if exclusive else None
        self._lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()
        self._db: aiosqlite.Connection | None = None
        self._read_db: aiosqlite.Connection | None = None
        self.changed = asyncio.Event()
        self.memory_owner = lambda session_id: None

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("数据库尚未打开或已关闭")
        return self._db

    def _reader(self) -> aiosqlite.Connection:
        if self._read_db is None:
            raise RuntimeError("数据库尚未打开或已关闭")
        return self._read_db

    async def open(self) -> None:
        async with self._lock:
            if self._db is not None:
                raise RuntimeError("数据库已经打开")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self._lease is not None:
                self._lease.__enter__()
            db = reader = None
            try:
                db = await aiosqlite.connect(self.path, isolation_level=None)
                db.row_factory = aiosqlite.Row
                for pragma in (
                    "PRAGMA foreign_keys=ON", "PRAGMA journal_mode=WAL",
                    "PRAGMA busy_timeout=5000",
                ):
                    async with db.execute(pragma):
                        pass
                schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
                schema += "\n" + MEMORY_SCHEMA_PATH.read_text(encoding="utf-8")
                async with db.executescript(schema):
                    pass
                from app.memory.migrations import migrate
                await migrate(db)
                reader = await aiosqlite.connect(self.path, isolation_level=None)
                reader.row_factory = aiosqlite.Row
                async with reader.execute("PRAGMA query_only=ON"):
                    pass
            except BaseException as error:
                try:
                    if db is not None:
                        await db.close()
                    if reader is not None:
                        await reader.close()
                except BaseException as cleanup_error:
                    error.add_note(f"数据库初始化清理失败：{cleanup_error!r}")
                finally:
                    if self._lease is not None:
                        self._lease.__exit__()
                raise
            self._db = db
            self._read_db = reader

    async def close(self) -> None:
        async with self._lock:
            async with self._read_lock:
                if self._db is not None:
                    try:
                        try:
                            await self._read_db.close()
                        finally:
                            await self._db.close()
                    finally:
                        self._db = self._read_db = None
                        if self._lease is not None:
                            self._lease.__exit__()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            db = self._connection()
            changes = db.total_changes
            try:
                async with db.execute("BEGIN"):
                    pass
                yield db
                await db.commit()
                if db.total_changes != changes:
                    changed, self.changed = self.changed, asyncio.Event()
                    changed.set()
            except BaseException as error:
                try:
                    await db.rollback()
                except BaseException as cleanup_error:
                    error.add_note(f"数据库回滚失败：{cleanup_error!r}")
                raise

    @asynccontextmanager
    async def read_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._read_lock:
            db = self._reader()
            async with db.execute("BEGIN"):
                pass
            try:
                yield db
            finally:
                await db.rollback()

    async def _fetch_one(self, sql: str, parameters: tuple) -> dict[str, Any] | None:
        async with self._read_lock:
            async with self._reader().execute(sql, parameters) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row is not None else None

    async def create_project(self, data: ProjectCreate, team_snapshot: dict | None = None) -> dict[str, Any]:
        path = await asyncio.to_thread(validate_project_directory, Path(data.path))
        async with self.transaction() as db:
            if data.team_id:
                async with db.execute("SELECT version FROM teams WHERE id=?", (data.team_id,)) as cursor:
                    team = await cursor.fetchone()
                if team is None or team_snapshot is None or team["version"] != team_snapshot["version"]:
                    raise RequestConflictError("团队配置已变更，请刷新后创建项目")
            async with db.execute(
                "INSERT INTO projects (id,name,path,check_argv,created_at) "
                "VALUES (?,?,?,?,?) RETURNING *",
                (str(uuid4()), path.name, str(path), json.dumps(data.check_argv, ensure_ascii=False),
                 datetime.now(UTC).isoformat()),
            ) as cursor:
                project = dict(await cursor.fetchone())
            if team_snapshot:
                await db.execute("INSERT INTO project_teams VALUES (?,?,?)",
                                 (project["id"], compact(team_snapshot), data.context_mode))
                project["team"] = {**team_snapshot, "context_mode": data.context_mode}
            return project

    async def get_harness_settings(self, harness: str) -> dict[str, Any]:
        row = await self._fetch_one("SELECT * FROM harness_settings WHERE harness=?", (harness,))
        if row is None:
            return {"harness": harness, "config_values": {}, "revision": 0, "updated_at": None}
        return {**row, "config_values": json.loads(row["config_values"])}

    async def session_tree(self, session_id: str) -> list[dict[str, Any]]:
        return await self._fetch_all(
            "WITH RECURSIVE tree(id) AS (SELECT id FROM sessions WHERE id=? UNION "
            "SELECT s.id FROM sessions s JOIN tree ON s.parent_id=tree.id) "
            "SELECT s.* FROM sessions s JOIN tree USING(id)", (session_id,))

    async def delete_sessions(self, session_ids: list[str]) -> None:
        scope = ",".join("?" for _ in session_ids)
        runs = f"SELECT id FROM runs WHERE session_id IN ({scope})"
        tasks = f"SELECT id FROM tasks WHERE child_session_id IN ({scope})"
        async with self.transaction() as db:
            async with db.execute("SELECT project_id FROM sessions WHERE id=?", (session_ids[0],)) as cursor:
                session = await cursor.fetchone()
            if session is None:
                raise KeyError(session_ids[0])
            await db.execute(f"DELETE FROM interactions WHERE session_id IN ({scope})", session_ids)
            await db.execute(
                f"DELETE FROM events WHERE session_id IN ({scope}) OR run_id IN ({runs}) OR task_id IN ({tasks})",
                session_ids * 3)
            await db.execute(f"UPDATE tasks SET current_run_id=NULL WHERE id IN ({tasks})", session_ids)
            await db.execute(f"DELETE FROM runs WHERE session_id IN ({scope})", session_ids)
            await db.execute(f"DELETE FROM tasks WHERE id IN ({tasks})", session_ids)
            await db.execute(f"DELETE FROM sessions WHERE id IN ({scope})", session_ids)
            await db.execute(
                "INSERT INTO events(project_id,kind,payload,created_at) VALUES (?,'session.deleted',?,?)",
                (session["project_id"], compact({"session_ids": session_ids}), datetime.now(UTC).isoformat()))

    async def save_harness_settings(self, harness: str, values: dict[str, str]) -> dict[str, Any]:
        async with self.transaction() as db:
            async with db.execute(
                "INSERT INTO harness_settings(harness,config_values,revision,updated_at) VALUES (?,?,1,?) "
                "ON CONFLICT(harness) DO UPDATE SET config_values=excluded.config_values,"
                "revision=harness_settings.revision+1,updated_at=excluded.updated_at RETURNING *",
                (harness, compact(values), datetime.now(UTC).isoformat()),
            ) as cursor:
                row = dict(await cursor.fetchone())
                return {**row, "config_values": json.loads(row["config_values"])}

    async def create_session(self, data: SessionCreate, session_id: str | None = None) -> dict[str, Any]:
        async with self.transaction() as db:
            async with db.execute("SELECT path FROM projects WHERE id=?", (data.project_id,)) as cursor:
                project = await cursor.fetchone()
            if project is None:
                raise ValueError(f"项目不存在：{data.project_id}")
            async with db.execute(
                "INSERT INTO sessions (id,project_id,harness,cwd,created_at) "
                "VALUES (?,?,?,?,?) RETURNING *",
                (session_id or str(uuid4()), data.project_id, data.harness, project["path"],
                 datetime.now(UTC).isoformat()),
            ) as cursor:
                return dict(await cursor.fetchone())

    async def create_run(self, session_id: str, data: MessageCreate) -> tuple[dict[str, Any], bool]:
        """提交 Run 与输入事件后返回；created=False 时不能再次调度发送。"""
        async with self.transaction() as db:
            async with db.execute(
                "SELECT * FROM runs WHERE session_id=? AND request_id=?", (session_id, data.request_id),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                if existing["prompt"] != data.message:
                    raise RequestConflictError(f"request_id 已用于不同消息：{data.request_id}")
                return dict(existing), False
            now = datetime.now(UTC).isoformat()
            async with db.execute(
                "INSERT INTO runs (id,session_id,attempt,request_id,prompt,status,created_at) "
                "VALUES (?,?,1,?,?,'queued',?) RETURNING *",
                (str(uuid4()), session_id, data.request_id, data.message, now),
            ) as cursor:
                run = dict(await cursor.fetchone())
            # Codeg connection.rs::record_prompt 同样在发送前等待输入记录完成。
            await _insert_event(db, run["id"], "user.message", {"message": data.message}, now)
            await _insert_event(db, run["id"], "run.status", {"from": None, "status": "queued"}, now)
            return run, True

    async def transition_run(
        self, run_id: str, expected_statuses: Collection[RunStatus], new_status: RunStatus,
        payload: dict[str, Any],
    ) -> bool:
        """借鉴 settle_review 的 CAS；以 current_run_id 替代 Codeg 的 run_seq。"""
        async with self.transaction() as db:
            changed = await _transition_run(db, run_id, expected_statuses, new_status, payload)
            if changed and new_status == RunStatus.RUNNING:
                from app.memory.storage import capture_started_run
                async with db.execute("SELECT session_id FROM runs WHERE id=?", (run_id,)) as cursor:
                    session_id = (await cursor.fetchone())[0]
                await capture_started_run(db, run_id, self.memory_owner(session_id))
            return changed

    async def create_task_with_run(
        self, parent_session_id: str, data: TaskCreate,
        child_session_id: str | None = None, reuse: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], bool]:
        if data.working_dir is not None and not Path(data.working_dir).is_dir():
            existing = await self._fetch_one(
                "SELECT id FROM tasks WHERE parent_session_id=? AND request_id=?", (parent_session_id, data.request_id),
            )
            if existing is None:
                raise WorkingDirectoryError("working_dir 必须是已存在的目录")
        async with self.transaction() as db:
            async with db.execute(
                "SELECT * FROM sessions WHERE id=?",
                (parent_session_id,),
            ) as cursor:
                parent = await cursor.fetchone()
            if parent is None:
                raise KeyError(parent_session_id)
            if parent["parent_id"] is not None:
                raise PermissionError("Worker 不能委派任务")
            async with db.execute(
                "SELECT * FROM tasks WHERE parent_session_id=? AND request_id=?",
                (parent_session_id, data.request_id),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                async with db.execute("SELECT * FROM team_tasks WHERE task_id=?", (existing["id"],)) as cursor:
                    target = await cursor.fetchone()
                if ((target["employee_id"], target["context_mode"]) if target else (None, None)) != (data.employee_id, data.context_mode):
                    raise RequestConflictError("request_id 已用于不同员工或上下文策略")
                async with db.execute("SELECT * FROM sessions WHERE id=?", (existing["child_session_id"],)) as cursor:
                    child = dict(await cursor.fetchone())
                if (child["harness"], existing["goal"], existing["acceptance"]) != (
                    data.harness, data.goal, data.acceptance,
                ):
                    raise RequestConflictError(f"request_id 已用于不同委派：{data.request_id}")
                async with db.execute(
                    "SELECT payload FROM events WHERE task_id=? AND kind='task.created' ORDER BY id LIMIT 1",
                    (existing["id"],),
                ) as cursor:
                    event = await cursor.fetchone()
                directory_choice = json.loads(event["payload"]).get("working_dir") if event is not None else None
                if directory_choice != data.working_dir:
                    raise RequestConflictError(f"request_id 已用于不同执行目录：{data.request_id}")
                async with db.execute("SELECT * FROM runs WHERE id=?", (existing["current_run_id"],)) as cursor:
                    run = dict(await cursor.fetchone())
                return dict(existing), child, run, False
            task, child, run = await _create_task_rows(db, parent, data, child_session_id, reuse)
            return task, child, run, True

    async def append_event(self, run_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return (await self.append_events(run_id, [(kind, payload)]))[0]

    async def append_events(self, run_id: str, events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        for kind, _ in events:
            if kind in {"run.status", "task.created", "task.status"}:
                raise ValueError(f"{kind} 必须与状态一起提交")
        packed = await asyncio.to_thread(lambda: [(kind, *pack_payload(kind, payload)) for kind, payload in events])
        async with self.transaction() as db:
            return [await _insert_packed_event(db, run_id, kind, payload, parts, datetime.now(UTC).isoformat())
                    for kind, payload, parts in packed]

    async def get_project(self, project_id: str) -> dict[str, Any] | None:
        row = await self._fetch_one("SELECT p.*,t.snapshot AS team_snapshot,t.context_mode FROM projects p "
                                    "LEFT JOIN project_teams t ON t.project_id=p.id WHERE p.id=?", (project_id,))
        return project_summary(row) if row is not None else None

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        return await self._fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))

    async def get_session_summary(self, session_id: str) -> dict[str, Any] | None:
        row = await self._fetch_one(SUMMARY_SQL + " WHERE s.id=?", (session_id,))
        return session_summary(row) if row is not None else None

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        return await self._fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))

    async def get_owned_task(self, task_id: str, parent_session_id: str) -> dict[str, Any] | None:
        return await self._fetch_one(
            "SELECT * FROM tasks WHERE id=? AND parent_session_id=?", (task_id, parent_session_id),
        )

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        return await self._fetch_one("SELECT * FROM tasks WHERE id=?", (task_id,))

    async def get_session_task(self, session_id: str) -> dict[str, Any] | None:
        return await self._fetch_one("SELECT * FROM tasks WHERE child_session_id=? ORDER BY rowid DESC LIMIT 1", (session_id,))

    async def get_task_working_dir(self, task_id: str) -> str | None:
        event = await self._fetch_one(
            "SELECT payload FROM events WHERE task_id=? AND kind='task.created' ORDER BY id LIMIT 1", (task_id,),
        )
        return json.loads(event["payload"]).get("working_dir") if event is not None else None

    async def attach_worktree(self, task_id: str, run_id: str, worktree: str,
                             sha: str, branch: str | None) -> None:
        async with self.transaction() as db:
            async with db.execute(
                "SELECT t.*,s.cwd,s.native_id FROM tasks t JOIN sessions s ON s.id=t.child_session_id "
                "JOIN runs r ON r.id=t.current_run_id WHERE t.id=? AND r.id=? AND r.status='running'",
                (task_id, run_id),
            ) as cursor:
                task = await cursor.fetchone()
            if task is None:
                raise DeliveryConflictError("任务不再活动，不能切换目录")
            if task["cwd"] != worktree:
                raise DeliveryConflictError("worktree 与 Session 执行目录不一致")
            if task["worktree"] is not None:
                if task["worktree"] != worktree or task["base_sha"] != sha or task["branch"] != branch:
                    raise DeliveryConflictError("任务和 Session 的目录记录不一致")
                return
            async with db.execute("SELECT 1 FROM team_sessions WHERE session_id=?", (task["child_session_id"],)) as cursor:
                team_member = await cursor.fetchone()
            if task["native_id"] is not None and team_member is None:
                raise DeliveryConflictError("子会话已连接，不能切换目录")
            now = datetime.now(UTC).isoformat()
            await db.execute("UPDATE tasks SET worktree=?,base_sha=?,base_branch=?,branch=?,updated_at=? WHERE id=?",
                             (worktree, sha, branch, branch, now, task_id))
            await _insert_event(db, run_id, "task.worktree_ready", {
                "task_id": task_id, "worktree": worktree, "branch": branch,
                "base_sha": sha, "base_branch": branch,
            }, now)

    async def get_task_snapshots(self, parent_session_id: str, task_ids: list[str]) -> list[dict[str, Any]]:
        if not task_ids:
            return []
        placeholders = ",".join("?" for _ in task_ids)
        rows = await self._fetch_all(
            "SELECT t.id AS task_id,t.child_session_id,t.current_run_id AS run_id,"
            "t.status,r.status AS run_status,r.result,r.error,t.worktree,t.branch "
            "FROM tasks t JOIN sessions s ON s.id=t.child_session_id "
            "JOIN runs r ON r.id=t.current_run_id AND r.task_id=t.id AND r.session_id=s.id "
            f"WHERE t.parent_session_id=? AND t.id IN ({placeholders})",
            (parent_session_id, *task_ids),
        )
        by_id = {row["task_id"]: row for row in rows}
        return [by_id[task_id] for task_id in task_ids]

    async def get_interaction(self, permission_id: str) -> dict[str, Any] | None:
        return await self._fetch_one("SELECT * FROM interactions WHERE id=?", (permission_id,))

    async def create_feedback(self, session_id: str, data: FeedbackCreate) -> dict[str, Any]:
        async with self.transaction() as db:
            async with db.execute("SELECT id FROM sessions WHERE id=?", (session_id,)) as cursor:
                if await cursor.fetchone() is None:
                    raise KeyError(session_id)
            now = datetime.now(UTC).isoformat()
            async with db.execute(
                "INSERT INTO interactions (id,session_id,kind,status,payload,created_at,updated_at) "
                "VALUES (?,?,'feedback','pending',?,?,?) RETURNING *",
                (str(uuid4()), session_id, json.dumps({"message": data.message}, ensure_ascii=False), now, now),
            ) as cursor:
                interaction = dict(await cursor.fetchone())
            await _insert_feedback_event(db, interaction, "feedback.created", "pending", now)
            return interaction

    async def pull_feedback(self, session_id: str) -> list[dict[str, Any]]:
        async with self.transaction() as db:
            async with db.execute(
                "SELECT * FROM interactions WHERE session_id=? AND kind='feedback' "
                "AND status='pending' ORDER BY rowid", (session_id,),
            ) as cursor:
                pending = [dict(row) for row in await cursor.fetchall()]
            now = datetime.now(UTC).isoformat()
            await db.execute(
                "UPDATE interactions SET status='pulled',updated_at=? "
                "WHERE session_id=? AND kind='feedback' AND status='pending'", (now, session_id),
            )
            for interaction in pending:
                await _insert_feedback_event(db, interaction, "feedback.pulled", "pulled", now)
            return [{"id": item["id"], "message": json.loads(item["payload"])["message"],
                     "created_at": item["created_at"]} for item in pending]

    async def create_permission(self, run_id: str, permission_id: str,
                                payload: dict[str, Any]) -> dict[str, Any]:
        async with self.transaction() as db:
            run = await _permission_run(db, run_id)
            now = datetime.now(UTC).isoformat()
            async with db.execute(
                "INSERT INTO interactions (id,session_id,run_id,kind,status,payload,created_at,updated_at) "
                "VALUES (?,?,?,'permission','pending',?,?,?) RETURNING *",
                (permission_id, run["session_id"], run_id, json.dumps(payload, ensure_ascii=False), now, now),
            ) as cursor:
                interaction = dict(await cursor.fetchone())
            if run["status"] == RunStatus.RUNNING:
                await _transition_run(db, run_id, {RunStatus.RUNNING}, RunStatus.WAITING_PERMISSION, {})
            await _insert_event(db, run_id, "permission.requested",
                                {**payload, "permission_id": permission_id, "status": "pending"}, now)
            return interaction

    async def answer_permission(self, permission_id: str, option_id: str) -> dict[str, Any]:
        async with self.transaction() as db:
            async with db.execute("SELECT * FROM interactions WHERE id=?", (permission_id,)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise KeyError(permission_id)
            if row["kind"] != "permission" or row["status"] != "pending":
                raise PermissionConflictError("权限请求已经回答或过期")
            run = await _permission_run(db, row["run_id"])
            options = json.loads(row["payload"])["options"]
            if option_id not in {option["optionId"] for option in options}:
                raise ValueError("option_id 不属于本次权限请求")
            now = datetime.now(UTC).isoformat()
            response = {"option_id": option_id}
            async with db.execute(
                "UPDATE interactions SET status='answered',response=?,updated_at=? "
                "WHERE id=? AND status='pending' RETURNING *",
                (json.dumps(response, ensure_ascii=False), now, permission_id),
            ) as cursor:
                interaction = dict(await cursor.fetchone())
            await _insert_event(db, run["id"], "permission.answered",
                                {"permission_id": permission_id, "status": "answered", **response}, now)
            await self._resume_permissions(db, run["id"])
            return interaction

    async def _resume_permissions(self, db: aiosqlite.Connection, run_id: str) -> None:
        async with db.execute(
            "SELECT 1 FROM interactions WHERE run_id=? AND kind='permission' AND status='pending'",
            (run_id,),
        ) as cursor:
            pending = await cursor.fetchone()
        if pending is None:
            await _transition_run(db, run_id, {RunStatus.WAITING_PERMISSION}, RunStatus.RUNNING, {})

    async def expire_permissions(self, run_id: str, reason: str,
                                 permission_id: str | None = None) -> list[str]:
        async with self.transaction() as db:
            sql = "UPDATE interactions SET status='expired',updated_at=? "
            sql += "WHERE run_id=? AND kind='permission' AND status='pending'"
            parameters = [datetime.now(UTC).isoformat(), run_id]
            if permission_id is not None:
                sql += " AND id=?"
                parameters.append(permission_id)
            async with db.execute(sql + " RETURNING id", parameters) as cursor:
                ids = [row["id"] for row in await cursor.fetchall()]
            for expired_id in ids:
                await _insert_event(db, run_id, "permission.expired", {
                    "permission_id": expired_id, "status": "expired", "reason": reason,
                }, parameters[0])
            if permission_id is not None:
                await self._resume_permissions(db, run_id)
            return ids

    async def _fetch_all(self, sql: str, parameters: tuple) -> list[dict[str, Any]]:
        async with self._read_lock:
            async with self._reader().execute(sql, parameters) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def list_projects(self) -> list[dict[str, Any]]:
        return [project_summary(row) for row in await self._fetch_all(
            "SELECT p.*,t.snapshot AS team_snapshot,t.context_mode FROM projects p "
            "LEFT JOIN project_teams t ON t.project_id=p.id ORDER BY p.created_at,p.id", (),
        )]

    async def list_sessions(self, project_id: str | None) -> list[dict[str, Any]]:
        where, parameters = ("", ()) if project_id is None else (" WHERE s.project_id=?", (project_id,))
        rows = await self._fetch_all(
            SUMMARY_SQL + where + " ORDER BY updated_at DESC,activity_event_id DESC,s.id", parameters,
        )
        return [session_summary(row) for row in rows]

    async def list_runs(self, session_id: str) -> list[dict[str, Any]]:
        return await self._fetch_all(
            "SELECT * FROM runs WHERE session_id=? ORDER BY rowid", (session_id,),
        )

    async def list_interactions(self, session_id: str) -> list[dict[str, Any]]:
        return await self._fetch_all(
            "SELECT * FROM interactions WHERE session_id=? AND status='pending' ORDER BY created_at,id",
            (session_id,),
        )

    async def set_session_connection(self, session_id: str, native_id: str, process_pid: int) -> None:
        async with self.transaction() as db:
            await db.execute(
                "UPDATE sessions SET native_id=?,process_pid=? WHERE id=?",
                (native_id, process_pid, session_id),
            )

    async def list_events(
        self, project_id: str, session_id: str | None, after_event_id: int, limit: int,
        run_ids: list[str] | None = None, through_event_id: int | None = None,
        include_context: bool = False,
    ) -> list[dict[str, Any]]:
        if after_event_id < 0 or limit < 1:
            raise ValueError("事件游标必须非负，limit 必须为正数")
        if after_event_id >= 2**63 - 1:
            return []
        sql = "SELECT * FROM events WHERE project_id=? AND id>?"
        parameters: list[Any] = [project_id, after_event_id]
        if session_id is not None:
            sql += " AND (session_id=? OR (session_id IS NULL AND kind='session.deleted'))"
            parameters.append(session_id)
        if through_event_id is not None:
            sql += " AND id<=?"
            parameters.append(through_event_id)
        if run_ids is not None:
            selection = "run_id IN (" + ",".join("?" for _ in run_ids) + ")"
            parameters.extend(run_ids)
            if include_context:
                selection += " OR run_id IS NULL OR run_id NOT IN (SELECT id FROM runs WHERE session_id=?)"
                parameters.append(session_id)
            sql += " AND (" + selection + ")"
        sql += " ORDER BY id ASC LIMIT ?"
        parameters.append(min(limit, 100))
        records = await self._fetch_all(sql, tuple(parameters))
        return await asyncio.to_thread(_event_page, records)

    async def history(self, session_id: str, before: int | None = None) -> dict:
        async with self.read_transaction() as db:
            async with db.execute("SELECT coalesce(max(id),0) FROM events WHERE session_id=?", (session_id,)) as cursor:
                high = (await cursor.fetchone())[0]
            fields = "rowid AS cursor,id,session_id,task_id,request_id,status,error,stop_reason,created_at,started_at,finished_at,substr(prompt,1,160) AS prompt_preview,substr(result,1,160) AS result_preview"
            sql = f"SELECT {fields} FROM runs WHERE session_id=?"
            params = [session_id]
            if before is not None:
                sql += " AND rowid<?"
                params.append(before)
            async with db.execute(sql + " ORDER BY rowid DESC LIMIT 21", params) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]
            more = len(rows) > 20
            page = rows[:20]
            async with db.execute(f"SELECT {fields} FROM runs WHERE session_id=? AND status IN ('running','waiting_permission','cancel_requested') ORDER BY rowid", (session_id,)) as cursor:
                active = [dict(row) for row in await cursor.fetchall()]
            metadata = []
            for kind in ("session.model", "session.title", "session.config"):
                async with db.execute("SELECT * FROM events WHERE session_id=? AND kind=? AND kind IN ('session.model','session.title','session.config') AND id<=? ORDER BY id DESC LIMIT 1", (session_id, kind, high)) as cursor:
                    row = await cursor.fetchone()
                    if row is not None:
                        metadata.append(dict(row))
            return {"runs": list(reversed(page)), "active_runs": active,
                    "high_watermark": high, "before": page[-1]["cursor"] if more else None,
                    "metadata": metadata}

    async def get_run_summary(self, run_id: str) -> dict | None:
        return await self._fetch_one(
            "SELECT id,session_id,task_id,request_id,status,error,stop_reason,created_at,started_at,finished_at,"
            "substr(prompt,1,160) AS prompt_preview,substr(result,1,160) AS result_preview FROM runs WHERE id=?", (run_id,))

    async def detail_page(self, event_id: int, after_part: int = -1) -> dict | None:
        async with self.read_transaction() as db:
            async with db.execute("SELECT * FROM events WHERE id=?", (event_id,)) as cursor:
                event = await cursor.fetchone()
            if event is None:
                return None
            async with db.execute("SELECT * FROM event_details WHERE event_id=? AND part>? ORDER BY part LIMIT 2", (event_id, after_part)) as cursor:
                parts = [dict(row) for row in await cursor.fetchall()]
        return await asyncio.to_thread(_detail_page, event_id, dict(event), parts, after_part)

    async def tool_detail_page(self, run_id: str, tool_id: str, high: int | None,
                               after_event: int, after_part: int) -> dict:
        async with self.read_transaction() as db:
            if high is None:
                async with db.execute("SELECT coalesce(max(id),0) FROM events WHERE run_id=?", (run_id,)) as cursor:
                    high = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT id,payload FROM events WHERE run_id=? AND kind='agent.tool' "
                "AND json_extract(payload,'$.toolCallId')=? AND id>=? AND id<=? ORDER BY id LIMIT 1",
                (run_id, tool_id, after_event, high),
            ) as cursor:
                event = await cursor.fetchone()
        if event is None:
            return {"high_watermark": high, "records": [], "next": None}
        page = await self.detail_page(event["id"], after_part if event["id"] == after_event else -1)
        light, _ = pack_payload("agent.tool", business_payload("agent.tool", json.loads(event["payload"])))
        if page["next"] is not None:
            next_cursor = {"event": event["id"], "part": page["next"]}
        else:
            next_event = await self._fetch_one(
                "SELECT id FROM events WHERE run_id=? AND kind='agent.tool' AND json_extract(payload,'$.toolCallId')=? AND id>? AND id<=? ORDER BY id LIMIT 1",
                (run_id, tool_id, event["id"], high))
            next_cursor = {"event": next_event["id"], "part": -1} if next_event else None
        return {"high_watermark": high,
                "records": [{"event_id": event["id"], "payload": light, "parts": page["parts"]}],
                "next": next_cursor}

    async def full_events(self, project_id: str, session_id: str | None,
                          after_event_id: int, limit: int) -> list[dict]:
        """明确请求完整投影时才读取详情；HTTP/SSE 始终使用轻量查询。"""
        events = await self.list_events(project_id, session_id, after_event_id, limit)
        for event in events:
            parts = await self._fetch_all("SELECT * FROM event_details WHERE event_id=? ORDER BY part", (event["id"],))
            original = await self._fetch_one("SELECT payload FROM events WHERE id=?", (event["id"],))
            payload = business_payload(event["kind"], json.loads(original["payload"]))
            event["payload"] = compact(restore_payload(payload, parts))
        return events

    async def tool_event_high(self, run_id: str) -> int:
        row = await self._fetch_one(
            "SELECT coalesce(max(id),0) AS high FROM events WHERE run_id=? AND kind='agent.tool'", (run_id,))
        return row["high"]

    async def run_tool_events(self, run_id: str, high: int) -> list[dict]:
        async with self.read_transaction() as db:
            async with db.execute("SELECT id,payload FROM events WHERE run_id=? AND kind='agent.tool' AND id<=? ORDER BY id", (run_id, high)) as cursor:
                events = [dict(row) for row in await cursor.fetchall()]
            for event in events:
                event["payload"] = business_payload("agent.tool", json.loads(event["payload"]))
            statuses = {}
            for event in events:
                payload = event["payload"]
                tool_id = payload.get("toolCallId")
                if tool_id and payload.get("status") is not None:
                    statuses[tool_id] = payload["status"]
            completed = {tool_id for tool_id, status in statuses.items() if status == "completed"}
            events = [event for event in events if event["payload"].get("toolCallId") in completed]
            latest = {}
            for event in events:
                for field in ("rawInput", "rawOutput", "locations", "title"):
                    if event["payload"].get(field) is not None:
                        latest[event["payload"]["toolCallId"], field] = event["id"]
            for event in events:
                for field in ("rawInput", "rawOutput", "locations", "title"):
                    if latest.get((event["payload"]["toolCallId"], field)) != event["id"]:
                        event["payload"].pop(field, None)
            if not events:
                return []
            details = ["d.field='content'"]
            parameters = [run_id, high]
            for field in ("rawInput", "rawOutput", "locations", "title"):
                ids = {event_id for (_, name), event_id in latest.items() if name == field}
                if ids:
                    details.append(f"(d.field='{field}' AND d.event_id IN ({','.join('?' for _ in ids)}))")
                    parameters.extend(ids)
            async with db.execute(
                "SELECT d.* FROM event_details d JOIN events e ON e.id=d.event_id "
                "WHERE e.run_id=? AND e.kind='agent.tool' AND e.id<=? "
                f"AND ({' OR '.join(details)}) ORDER BY d.event_id,d.part", parameters,
            ) as cursor:
                parts = [dict(row) for row in await cursor.fetchall()]
        by_event = {}
        for part in parts:
            by_event.setdefault(part["event_id"], []).append(part)
        result = []
        fields = {"toolCallId", "kind", "status", "title", "locations", "content", "rawInput", "rawOutput"}
        for event in events:
            payload = {key: value for key, value in event["payload"].items() if key in fields}
            parts = [part for part in by_event.get(event["id"], []) if part["field"] in payload]
            result.append(restore_payload(payload, parts))
        return result
