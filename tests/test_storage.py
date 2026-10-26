import asyncio
import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from uuid import UUID, uuid4

import aiosqlite
import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.domain.models import ProjectCreate, SessionCreate
from app.db.storage import Storage


def uid():
    return str(uuid4())


def project_input(path):
    return ProjectCreate(path=str(path), name="演示 ' 项目", check_argv=["python", "-c", "", "中文"])


@pytest.fixture
def repository(tmp_path):
    path = tmp_path / "中文 repo"
    subprocess.run(["git", "init", "--quiet", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=Storage Test",
         "-c", "user.email=storage@example.invalid", "-c", "commit.gpgsign=false",
         "-c", f"core.hooksPath={tmp_path / 'no-hooks'}",
         "commit", "--quiet", "--allow-empty", "-m", "storage fixture"],
        check=True, capture_output=True,
    )
    return path


@pytest_asyncio.fixture
async def storage(tmp_path):
    instance = Storage(tmp_path / "database" / "test.db")
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


@pytest_asyncio.fixture
async def records(storage, repository):
    project = await storage.create_project(project_input(repository))
    session = await storage.create_session(SessionCreate(project_id=project["id"], harness="codex"))
    return project, session


async def insert(db, table, values):
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    async with db.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values()),
    ) as cursor:
        return cursor.lastrowid


async def linked_task(db, project, parent):
    now = datetime.now(UTC).isoformat()
    child_id, task_id, run_id = uid(), uid(), uid()
    await insert(db, "sessions", dict(
        id=child_id, project_id=project["id"], parent_id=parent["id"],
        harness="opencode", cwd=project["path"], created_at=now,
    ))
    task = dict(
        id=task_id, project_id=project["id"], parent_session_id=parent["id"],
        child_session_id=child_id, request_id=uid(), goal="修复", acceptance="测试通过",
        status="queued", current_run_id=None, created_at=now, updated_at=now,
    )
    await insert(db, "tasks", task)
    run = dict(
        id=run_id, session_id=child_id, task_id=task_id, attempt=1,
        request_id=uid(), prompt="修复 ' 日期", status="queued", created_at=now,
    )
    await insert(db, "runs", run)
    await db.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
    return task, run


@pytest.mark.asyncio
async def test_file_reopen_preserves_records_and_initialization(storage, records):
    project, session = records
    UUID(project["id"])
    UUID(session["id"])
    assert json.loads(project["check_argv"]) == ["python", "-c", "", "中文"]
    assert datetime.fromisoformat(project["created_at"]).utcoffset().total_seconds() == 0
    assert session["cwd"] == project["path"]
    assert all(session[key] is None for key in ("parent_id", "native_id", "process_pid"))
    for instance in (storage, Storage(storage.path)):
        if instance is not storage:
            await instance.open()
        try:
            assert await instance.get_project(project["id"]) == project
            assert await instance.get_session(session["id"]) == session
            async with instance.transaction() as db:
                for name, expected in (("foreign_keys", 1), ("journal_mode", "wal"), ("busy_timeout", 5000)):
                    async with db.execute(f"PRAGMA {name}") as cursor:
                        assert (await cursor.fetchone())[0] == expected
                async with db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
                    tables = {row["name"] for row in await cursor.fetchall()}
                assert tables - {"sqlite_sequence"} == {
                    "projects", "sessions", "tasks", "runs", "events", "interactions", "event_details", "harness_settings",
                    "memory_settings", "memory_contexts", "memory_sources", "memory_operations",
                    "memory_capture_turns", "teams", "project_teams", "team_sessions", "team_tasks",
                    "employee_memory_settings", "memory_run_scopes",
                }
                async with db.execute("SELECT name FROM sqlite_master WHERE type='index'") as cursor:
                    assert {"events_project", "events_session"} <= {row[0] for row in await cursor.fetchall()}
        finally:
            await instance.close()
    assert storage.path.is_file()


@pytest.mark.asyncio
async def test_project_accepts_existing_directories(storage, repository, tmp_path):
    with pytest.raises(ValidationError):
        project_input("relative")
    non_git = tmp_path / "plain"
    non_git.mkdir()
    assert (await storage.create_project(project_input(non_git)))["path"] == str(non_git.resolve())
    assert not (non_git / ".git").exists()
    child = repository / "child"
    child.mkdir()
    assert (await storage.create_project(project_input(child)))["path"] == str(child.resolve())
    empty = tmp_path / "unborn"
    subprocess.run(["git", "init", "--quiet", str(empty)], check=True, capture_output=True)
    assert (await storage.create_project(project_input(empty)))["path"] == str(empty.resolve())
    file = tmp_path / "file.txt"
    file.touch()
    with pytest.raises(ValueError, match="必须是目录"):
        await storage.create_project(project_input(file))
    with pytest.raises(FileNotFoundError):
        await storage.create_project(project_input(tmp_path / "absent"))


@pytest.mark.asyncio
async def test_duplicate_path_and_missing_project(storage, records, repository):
    project, _ = records
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        await storage.create_project(project_input(repository / "."))
    with pytest.raises(ValueError, match="项目不存在"):
        await storage.create_session(SessionCreate(project_id=uid(), harness="codex"))
    assert await storage.get_project(project["id"]) == project
    assert await storage.get_project(uid()) is None
    assert await storage.get_session(uid()) is None
    assert await storage.get_run(uid()) is None


@pytest.mark.asyncio
async def test_foreign_keys_reject_missing_project(storage):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        async with storage.transaction() as db:
            await insert(db, "sessions", dict(
                id=uid(), project_id=uid(), harness="codex", cwd="D:\\missing",
                created_at=datetime.now(UTC).isoformat(),
            ))


@pytest.mark.asyncio
async def test_cycle_and_all_six_tables_survive_reopen(storage, records):
    project, parent = records
    async with storage.transaction() as db:
        task, run = await linked_task(db, project, parent)
        event_id = await insert(db, "events", dict(
            project_id=project["id"], session_id=run["session_id"], run_id=run["id"],
            task_id=task["id"], kind="status", payload='{"status":"queued"}',
            created_at=run["created_at"],
        ))
        interaction_id = uid()
        await insert(db, "interactions", dict(
            id=interaction_id, session_id=run["session_id"], run_id=run["id"],
            kind="permission", status="pending", payload='{"options":[]}',
            created_at=run["created_at"], updated_at=run["created_at"],
        ))
    await storage.close()
    reopened = Storage(storage.path)
    await reopened.open()
    try:
        actual = await reopened.get_run(run["id"])
        assert all(actual[key] == value for key, value in run.items())
        events = await reopened.list_events(project["id"], None, 0, 10)
        assert events[0]["id"] == event_id
        assert json.loads(events[0]["payload"]) == {"status": "queued"}
        async with reopened.transaction() as db:
            async with db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)) as cursor:
                assert (await cursor.fetchone())["current_run_id"] == run["id"]
            async with db.execute("SELECT * FROM interactions WHERE id=?", (interaction_id,)) as cursor:
                assert (await cursor.fetchone())["run_id"] == run["id"]
            async with db.execute("PRAGMA foreign_key_check") as cursor:
                assert await cursor.fetchall() == []
    finally:
        await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("constraint", ["run_request", "task_request", "attempt"])
async def test_real_unique_constraints(storage, records, constraint):
    project, parent = records
    async with storage.transaction() as db:
        task, run = await linked_task(db, project, parent)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        async with storage.transaction() as db:
            if constraint == "task_request":
                await insert(db, "tasks", {**task, "id": uid()})
            elif constraint == "run_request":
                await insert(db, "runs", {**run, "id": uid(), "attempt": 2})
            else:
                await insert(db, "runs", {**run, "id": uid(), "request_id": uid()})
    assert await storage.get_run(run["id"]) is not None


@pytest.mark.asyncio
async def test_multiple_writes_roll_back_together(storage, records):
    project, parent = records
    failed_id = uid()
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        async with storage.transaction() as db:
            await db.execute("UPDATE projects SET name=? WHERE id=?", ("回滚的名称", project["id"]))
            await insert(db, "runs", dict(
                id=failed_id, session_id=parent["id"], task_id=uid(), attempt=1,
                request_id=uid(), prompt="失败", status="queued", created_at=project["created_at"],
            ))
    assert await storage.get_project(project["id"]) == project
    assert await storage.get_run(failed_id) is None
    assert await storage.create_session(SessionCreate(project_id=project["id"], harness="opencode"))


@pytest.mark.asyncio
async def test_write_lock_serializes_writers_but_readers_see_committed_snapshot(storage, records):
    project, _ = records
    entered, release, contender_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def owner():
        async with storage.transaction() as db:
            await db.execute("UPDATE projects SET name=? WHERE id=?", ("未提交", project["id"]))
            entered.set()
            await release.wait()
            raise ValueError("owner rollback")

    async def contender():
        contender_started.set()
        return await storage.create_session(SessionCreate(project_id=project["id"], harness="codex"))

    owner_task = asyncio.create_task(owner())
    await asyncio.wait_for(entered.wait(), 2)
    next_task = asyncio.create_task(contender())
    read_task = asyncio.create_task(storage.get_project(project["id"]))
    try:
        await asyncio.wait_for(contender_started.wait(), 2)
        await asyncio.sleep(0)
        assert not next_task.done()
        assert await asyncio.wait_for(asyncio.shield(read_task), 1) == project
    finally:
        release.set()
        results = await asyncio.gather(owner_task, next_task, read_task, return_exceptions=True)
    assert isinstance(results[0], ValueError)
    assert await storage.get_session(results[1]["id"]) == results[1]
    assert results[2] == project


@pytest.mark.asyncio
async def test_cancelled_transaction_rolls_back_and_unlocks(storage, records):
    project, _ = records
    entered = asyncio.Event()

    async def owner():
        async with storage.transaction() as db:
            await db.execute("UPDATE projects SET name=? WHERE id=?", ("取消的名称", project["id"]))
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await storage.get_project(project["id"]) == project


@pytest.mark.asyncio
async def test_change_notification_only_after_commit_and_read_connection_is_query_only(storage, records):
    project, _ = records
    changed = storage.changed
    with pytest.raises(ValueError):
        async with storage.transaction() as db:
            await db.execute("UPDATE projects SET name='rollback' WHERE id=?", (project["id"],))
            assert not changed.is_set()
            raise ValueError("rollback")
    assert not changed.is_set()
    async with storage.read_transaction() as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            await db.execute("UPDATE projects SET name='forbidden'")
    async with storage.transaction() as db:
        await db.execute("UPDATE projects SET name='committed' WHERE id=?", (project["id"],))
        assert not changed.is_set()
    assert changed.is_set() and not storage.changed.is_set()
    assert (await storage._fetch_one("SELECT name FROM projects WHERE id=?", (project["id"],)))["name"] == "committed"


@pytest.mark.asyncio
async def test_events_filter_cursor_and_limit(storage, records):
    project, session = records
    other_project = {**project, "id": uid(), "path": project["path"] + "-other"}
    other_session = await storage.create_session(SessionCreate(project_id=project["id"], harness="opencode"))
    async with storage.transaction() as db:
        await insert(db, "projects", other_project)
        ids = []
        for project_id, session_id in (
            (project["id"], session["id"]), (other_project["id"], None),
            (project["id"], other_session["id"]), (project["id"], session["id"]),
        ):
            ids.append(await insert(db, "events", dict(
                project_id=project_id, session_id=session_id, kind="message", payload='{"text":"中文"}',
                created_at=project["created_at"],
            )))
    assert [e["id"] for e in await storage.list_events(project["id"], None, 0, 10)] == [ids[0], ids[2], ids[3]]
    first = await storage.list_events(project["id"], session["id"], 0, 1)
    second = await storage.list_events(project["id"], session["id"], first[-1]["id"], 1)
    assert [first[0]["id"], second[0]["id"]] == [ids[0], ids[3]]
    assert await storage.list_events(project["id"], session["id"], ids[-1], 10) == []
    assert await storage.list_events(other_project["id"], session["id"], 0, 10) == []
    for cursor, limit in ((-1, 10), (0, 0)):
        with pytest.raises(ValueError):
            await storage.list_events(project["id"], None, cursor, limit)


@pytest.mark.asyncio
async def test_repeated_close_and_reopen(storage):
    await storage.close()
    await storage.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        await storage.get_project(uid())
    await storage.open()
    with pytest.raises(RuntimeError, match="已经打开"):
        await storage.open()
    assert await storage.get_project(uid()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_initialization_failure_closes_connection_and_keeps_original_error(tmp_path, monkeypatch, cleanup_fails):
    path = tmp_path / "invalid-schema.db"
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE events (id INTEGER)")
        db.commit()
    finally:
        db.close()
    closed = []
    original_close = aiosqlite.Connection.close

    async def tracked_close(connection):
        await original_close(connection)
        closed.append(connection)
        if cleanup_fails:
            raise RuntimeError("secondary cleanup error")

    instance = Storage(path)
    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, "close", tracked_close)
        with pytest.raises(sqlite3.OperationalError, match="no such column") as error:
            await instance.open()
        assert len(closed) == 1
        if cleanup_fails:
            assert "secondary cleanup error" in error.value.__notes__[0]
    with pytest.raises(RuntimeError, match="尚未打开"):
        await instance.get_project(uid())
    db = sqlite3.connect(path)
    try:
        db.execute("DROP TABLE events")
        db.commit()
    finally:
        db.close()
    await instance.open()
    try:
        assert await instance.get_run(uid()) is None
    finally:
        await instance.close()
