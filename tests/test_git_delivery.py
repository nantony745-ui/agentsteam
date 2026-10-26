"""目录选择、固定基线和真实本地 ACP；不调用模型。"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from app.agents import acp_client
from app.db.storage import RequestConflictError, Storage
from app.delivery import git_delivery
from app.domain.models import TaskCreate
from app.main import create_app
from app.services import orchestrator
from test_events import next_event
from test_http_runs import ControlledFactory, live_server, new_session, terminal
from test_storage import repository


def commit(repo, message):
    return subprocess.check_output([
        "git", "-C", str(repo), "-c", "user.name=Delivery Test", "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={repo.parent / 'no-hooks'}",
        "commit", "--quiet", "--allow-empty", "-m", message,
    ])


def task_input(working_dir=None):
    return TaskCreate(harness="codex", goal="目录执行", acceptance="返回证据", request_id=str(uuid4()),
                      working_dir=str(working_dir) if working_dir is not None else None)


async def worktree(repo, detached=False):
    target = repo.parent / f"worktree 中文 {uuid4()}"
    args = ("--detach",) if detached else ("-b", f"chosen/{uuid4()}")
    await git_delivery.git(repo, "worktree", "add", *args, str(target), "HEAD")
    return target.resolve()


@pytest_asyncio.fixture
async def delivery(tmp_path, repository):
    factory = ControlledFactory()
    app = create_app(tmp_path / "delivery.db", factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as http:
            project = (await http.post("/api/projects", json={"path": str(repository)})).json()
            parent = await new_session(http, project)
            yield http, app, factory, repository, parent


async def delegate(context, data):
    return await context[1].state.service.delegate_task(context[4]["id"], data)


async def finish(context, handle):
    http, app, factory, _, _ = context
    agent, _ = await factory.started()
    agent.finish()
    await terminal(http, handle["run_id"])
    await app.state.service.sessions[handle["child_session_id"]].queue.join()
    return agent


def authorization(context):
    _, app, _, _, parent = context
    app.state.service.tokens["directory-test"] = orchestrator.CallerIdentity(parent["id"], "coordinator")
    return {"Authorization": "Bearer directory-test"}


@pytest.mark.asyncio
async def test_two_workers_share_parent_without_git_changes(delivery):
    http, app, factory, repo, parent = delivery
    (repo / "main draft.txt").write_text("父目录草稿", encoding="utf-8")
    queries = (("status", "--porcelain"), ("worktree", "list", "--porcelain"), ("branch", "--list"))
    before = [await git_delivery.git(repo, *args) for args in queries]
    data = task_input()
    handles = [await delegate(delivery, data), await delegate(delivery, task_input())]
    agents = [await factory.started(), await factory.started()]
    for handle, (agent, _) in zip(handles, agents):
        task = await app.state.storage.get_task(handle["task_id"])
        child = await app.state.storage.get_session(handle["child_session_id"])
        assert agent.cwd == Path(child["cwd"]) == repo and (agent.cwd / "main draft.txt").exists()
        assert all(task[key] is None for key in ("base_sha", "base_branch", "worktree", "branch"))
        detail = (await http.get(f"/api/sessions/{child['id']}")).json()
        assert detail["task"]["working_dir"] is None
        assert (await http.get(f"/api/tasks/{task['id']}/diff")).status_code == 409
        events = await app.state.storage.list_events(parent["project_id"], child["id"], 0, 100)
        assert not any(event["kind"] == "task.worktree_ready" for event in events)
    assert (await http.get(f"/api/sessions/{parent['id']}")).json()["task"] is None
    assert await delegate(delivery, data) == {**handles[0], "status": "running"}
    agents[0][0].finish("共享目录结果")
    await terminal(http, handles[0]["run_id"])
    assert (await app.state.service.cancel(handles[1]["run_id"]))["status"] == "canceled"
    snapshot = (await app.state.storage.get_task_snapshots(parent["id"], [handles[0]["task_id"]]))[0]
    assert snapshot["result"] == "共享目录结果" and snapshot["worktree"] is None
    assert [await git_delivery.git(repo, *args) for args in queries] == before
    assert not (repo.parent / "worktrees").exists()


@pytest.mark.parametrize("location", ["subdirectory", "worktree"])
@pytest.mark.asyncio
async def test_default_inherits_actual_parent_cwd_without_binding(delivery, location):
    _, app, _, repo, parent = delivery
    target = repo / "parent subdirectory" if location == "subdirectory" else await worktree(repo)
    target.mkdir(exist_ok=True)
    async with app.state.storage.transaction() as db:
        await db.execute("UPDATE sessions SET cwd=? WHERE id=?", (str(target), parent["id"]))
    handle = await delegate(delivery, task_input())
    assert (await finish(delivery, handle)).cwd == target
    assert (await app.state.storage.get_task(handle["task_id"]))["worktree"] is None


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.asyncio
async def test_selected_worktree_fixed_base_actual_branch_and_reopen(delivery, detached):
    http, app, factory, repo, _ = delivery
    root = await worktree(repo, detached)
    commit(root, "调用方先提交")
    sha, branch = await git_delivery.capture_base(root)
    handle = await delegate(delivery, task_input(root))
    agent, _ = await factory.started()
    task = await app.state.storage.get_task(handle["task_id"])
    assert agent.cwd == root and task["worktree"] == str(root)
    assert task["base_sha"] == sha and task["branch"] == task["base_branch"] == branch
    assert (branch is None) == detached and await git_delivery.validate_worktree(task, repo) == root
    assert (await http.get(f"/api/sessions/{handle['child_session_id']}")).json()["task"]["working_dir"] == str(root)
    (root / "independent.txt").write_text("保留内容", encoding="utf-8")
    await git_delivery.git(root, "add", "independent.txt")
    commit(root, "Worker 提交")
    commit(repo, "主分支推进")
    result = (await http.get(f"/api/tasks/{task['id']}/diff")).json()
    assert result["base_sha"] == sha and result["head_sha"] != sha and "+保留内容" in result["patch"]
    await app.state.storage.attach_worktree(task["id"], handle["run_id"], str(root), sha, branch)
    events = await app.state.storage.list_events(task["project_id"], task["child_session_id"], 0, 100)
    assert sum(event["kind"] == "task.worktree_ready" for event in events) == 1
    agent.finish()
    await terminal(http, handle["run_id"])
    reopened = Storage(app.state.storage.path)
    await reopened.open()
    try:
        saved = await reopened.get_task(task["id"])
        assert saved["base_sha"] == sha and await git_delivery.validate_worktree(saved, repo) == root
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_directory_idempotency_and_legacy_event(delivery):
    http, app, _, repo, _ = delivery
    auth = authorization(delivery)
    root = repo.parent / "ordinary directory"
    root.mkdir()
    data = task_input(root).model_dump()
    first = (await http.post("/internal/tasks", json=data, headers=auth)).json()
    await finish(delivery, first)
    replay = await http.post("/internal/tasks", json={**data, "working_dir": str(root / "child" / "..")}, headers=auth)
    assert replay.status_code == 202 and replay.json() == {**first, "status": "review"}
    for choice in (None, str(repo)):
        assert (await http.post("/internal/tasks", json={**data, "working_dir": choice}, headers=auth)).status_code == 409
    root.rmdir()
    assert (await http.post("/internal/tasks", json=data, headers=auth)).status_code == 202
    default = task_input()
    handle = await delegate(delivery, default)
    await finish(delivery, handle)
    async with app.state.storage.transaction() as db:
        async with db.execute("SELECT id,payload FROM events WHERE task_id=? AND kind='task.created'", (handle["task_id"],)) as cursor:
            event = await cursor.fetchone()
        payload = json.loads(event["payload"])
        payload.pop("working_dir")
        await db.execute("UPDATE events SET payload=? WHERE id=?", (json.dumps(payload), event["id"]))
    assert (await delegate(delivery, default))["task_id"] == handle["task_id"]
    with pytest.raises(RequestConflictError):
        await delegate(delivery, default.model_copy(update={"working_dir": str(repo)}))


@pytest.mark.parametrize("choice", ["relative", "missing", "file", "empty", "nul"])
@pytest.mark.asyncio
async def test_invalid_directory_422_without_task(delivery, choice):
    http, app, factory, repo, _ = delivery
    file = repo / "file.txt"
    file.write_text("file")
    value = {"relative": "relative/path", "missing": str(repo / "missing"), "file": str(file),
             "empty": "", "nul": str(repo) + "\0bad"}[choice]
    response = await http.post("/internal/tasks", json={**task_input().model_dump(), "working_dir": value}, headers=authorization(delivery))
    assert response.status_code == 422, response.text
    async with app.state.storage.transaction() as db:
        async with db.execute("SELECT COUNT(*) FROM tasks") as cursor:
            assert (await cursor.fetchone())[0] == 0
    assert not factory.clients


@pytest.mark.asyncio
async def test_removed_directory_prevents_start(delivery, monkeypatch):
    http, app, factory, repo, _ = delivery
    root = repo.parent / "vanishing"
    root.mkdir()
    original = app.state.service._prepare_directory

    async def removed(runtime):
        root.rmdir()
        await original(runtime)

    monkeypatch.setattr(app.state.service, "_prepare_directory", removed)
    handle = await delegate(delivery, task_input(root))
    run = await terminal(http, handle["run_id"])
    assert run["status"] == "failed" and "执行目录" in run["error"] and not factory.clients


@pytest.mark.parametrize("failure", ["base", "event"])
@pytest.mark.asyncio
async def test_binding_failure_rolls_back_all_metadata(delivery, failure):
    http, app, factory, repo, _ = delivery
    root = await worktree(repo)
    (root / "keep.txt").write_text("keep")
    before = await git_delivery.git(root, "status", "--porcelain")
    async with app.state.storage.transaction() as db:
        sql = ("CREATE TRIGGER fail_bind BEFORE UPDATE OF base_sha ON tasks BEGIN SELECT RAISE(ABORT,'base failure'); END"
               if failure == "base" else "CREATE TRIGGER fail_bind BEFORE INSERT ON events WHEN NEW.kind='task.worktree_ready' BEGIN SELECT RAISE(ABORT,'ready failure'); END")
        await db.execute(sql)
    handle = await delegate(delivery, task_input(root))
    assert (await terminal(http, handle["run_id"]))["status"] == "failed"
    task = await app.state.storage.get_task(handle["task_id"])
    assert task["base_sha"] is task["branch"] is task["worktree"] is None
    assert (await app.state.storage.get_session(handle["child_session_id"]))["cwd"] == str(root)
    assert await git_delivery.git(root, "status", "--porcelain") == before and not factory.clients


@pytest.mark.asyncio
async def test_canceled_and_old_runs_cannot_bind(delivery):
    _, app, _, repo, parent = delivery
    root = await worktree(repo)
    task, _, run, _ = await app.state.storage.create_task_with_run(parent["id"], task_input(root))
    sha, branch = await git_delivery.capture_base(root)
    await app.state.storage.transition_run(run["id"], {"queued"}, "canceled", {})
    with pytest.raises(git_delivery.DeliveryConflictError):
        await app.state.storage.attach_worktree(task["id"], run["id"], str(root), sha, branch)
    new_run = str(uuid4())
    async with app.state.storage.transaction() as db:
        await db.execute("INSERT INTO runs (id,session_id,task_id,attempt,request_id,prompt,status,created_at) SELECT ?,session_id,task_id,2,?,'new','running',created_at FROM runs WHERE id=?", (new_run, str(uuid4()), run["id"]))
        await db.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (new_run, task["id"]))
    with pytest.raises(git_delivery.DeliveryConflictError):
        await app.state.storage.attach_worktree(task["id"], run["id"], str(root), sha, branch)
    await app.state.storage.attach_worktree(task["id"], new_run, str(root), sha, branch)


@pytest.mark.parametrize("action", ["cancel", "shutdown"])
@pytest.mark.parametrize("phase", ["running", "spawning"])
@pytest.mark.asyncio
async def test_cancel_during_git_inspection_reaps_process(delivery, monkeypatch, action, phase):
    http, app, factory, repo, _ = delivery
    root = await worktree(repo)
    real_spawn = asyncio.create_subprocess_exec
    processes, release = asyncio.Queue(), asyncio.Event()
    if phase == "running":
        release.set()

    async def slow_inspection(*argv, **kwargs):
        if "worktree" in argv:
            process = await real_spawn(sys.executable, "-c", "import time; time.sleep(120)", **kwargs)
            processes.put_nowait(process)
            await release.wait()
            return process
        return await real_spawn(*argv, **kwargs)

    monkeypatch.setattr(git_delivery.asyncio, "create_subprocess_exec", slow_inspection)
    handle = await delegate(delivery, task_input(root))
    process = await asyncio.wait_for(processes.get(), 10)
    stopping = asyncio.create_task(app.state.service.cancel(handle["run_id"]) if action == "cancel" else app.state.service.close())
    if phase == "spawning":
        execution = app.state.service.sessions[handle["child_session_id"]].active
        async with asyncio.timeout(3):
            while not execution.connection.cancelling():
                await asyncio.sleep(0)
        assert not stopping.done()
        release.set()
    await asyncio.wait_for(stopping, 10)
    assert (await terminal(http, handle["run_id"]))["status"] == ("canceled" if action == "cancel" else "interrupted")
    assert process.returncode is not None and not factory.clients and root.exists()
    assert (await app.state.storage.get_task(handle["task_id"]))["base_sha"] is None


@pytest.mark.parametrize("action", ["cancel", "shutdown"])
@pytest.mark.asyncio
async def test_cancel_before_binding_preserves_worktree(delivery, monkeypatch, action):
    http, app, factory, repo, _ = delivery
    root = await worktree(repo)
    inspected = asyncio.Event()
    original = orchestrator.selected_worktree

    async def pause(*args):
        baseline = await original(*args)
        inspected.set()
        await asyncio.Event().wait()
        return baseline

    monkeypatch.setattr(orchestrator, "selected_worktree", pause)
    handle = await delegate(delivery, task_input(root))
    await asyncio.wait_for(inspected.wait(), 10)
    if action == "cancel":
        await app.state.service.cancel(handle["run_id"])
    else:
        await app.state.service.close()
    assert (await terminal(http, handle["run_id"]))["status"] == ("canceled" if action == "cancel" else "interrupted")
    assert root.exists() and not factory.clients
    task = await app.state.storage.get_task(handle["task_id"])
    assert task["worktree"] is task["base_sha"] is None


@pytest.mark.asyncio
async def test_ready_event_replays_without_canceling_worker(delivery):
    _, app, factory, repo, parent = delivery
    root = await worktree(repo)
    handle = await delegate(delivery, task_input(root))
    agent, _ = await factory.started()
    history = await app.state.storage.list_events(parent["project_id"], handle["child_session_id"], 0, 100)
    ready = next(event for event in history if event["kind"] == "task.worktree_ready")
    params = {"project_id": parent["project_id"], "session_id": handle["child_session_id"], "after_event_id": ready["id"] - 1}
    async with live_server(app) as client:
        async with client.stream("GET", "/api/stream", params=params) as response:
            received, _ = await next_event(response.aiter_lines())
            assert received["id"] == ready["id"] and received["payload"]["worktree"] == str(root)
        assert factory.cancels == 0 and not agent.pending.done()
        agent.finish()
        await terminal(client, handle["run_id"])
        expected = (await client.get("/api/events", params={**params, "after_event_id": ready["id"]})).json()
        async with client.stream("GET", "/api/stream", params=params, headers={"Last-Event-ID": str(ready["id"])}) as response:
            lines = response.aiter_lines()
            assert [(await next_event(lines))[0] for _ in expected] == expected


@pytest.mark.parametrize("mode", ["shared", "ordinary", "other-repo", "worktree", "detached"])
@pytest.mark.asyncio
async def test_real_acp_process_and_protocol_cwd(tmp_path, repository, monkeypatch, mode):
    script = Path(__file__).with_name("fixtures") / "acp_cwd_agent.py"
    monkeypatch.setattr(acp_client, "load_argv", lambda harness: [sys.executable, str(script)])
    if mode == "shared":
        roots = [repository, repository]
    elif mode in {"worktree", "detached"}:
        roots = [await worktree(repository, mode == "detached") for _ in range(2)]
    else:
        roots = [tmp_path / f"selected 中文 {number}" for number in range(2)]
        for root in roots:
            root.mkdir()
            if mode == "other-repo":
                subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
                commit(root, "另一个仓库")
    app = create_app(tmp_path / "real-acp.db")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as http:
            project = (await http.post("/api/projects", json={"path": str(repository)})).json()
            parent = await new_session(http, project)
            handles = [await app.state.service.delegate_task(parent["id"], task_input(None if mode == "shared" else root)) for root in roots]
            for root, handle in zip(roots, handles):
                async with asyncio.timeout(20):
                    await app.state.service.sessions[handle["child_session_id"]].queue.join()
                run = await app.state.storage.get_run(handle["run_id"])
                assert run["status"] == "completed", run
                proof = json.loads(run["result"])
                assert Path(proof["process_cwd"]).resolve() == Path(proof["acp_cwd"]).resolve() == root
                if mode == "ordinary":
                    assert proof["git_root"] is None
                else:
                    assert Path(proof["git_root"]).resolve() == root
                assert (root / "worker evidence.txt").exists()
                task = await app.state.storage.get_task(handle["task_id"])
                assert task["worktree"] == (str(root) if mode in {"worktree", "detached"} else None)
            if mode != "shared":
                assert not (repository / "worker evidence.txt").exists()


@pytest.mark.parametrize("change", ["branch", "repo", "missing", "base"])
@pytest.mark.asyncio
async def test_existing_binding_is_validated_without_recreation(delivery, change):
    _, app, _, repo, _ = delivery
    root = await worktree(repo)
    handle = await delegate(delivery, task_input(root))
    await finish(delivery, handle)
    task = await app.state.storage.get_task(handle["task_id"])
    task[{"branch": "branch", "repo": "worktree", "missing": "worktree", "base": "base_sha"}[change]] = {
        "branch": "other", "repo": str(repo), "missing": str(repo.parent / "missing"), "base": "0" * 40,
    }[change]
    with pytest.raises(git_delivery.DeliveryConflictError):
        await git_delivery.validate_worktree(task, repo)


@pytest.mark.asyncio
async def test_reused_binding_keeps_baseline_and_started_session_keeps_cwd(delivery):
    _, app, _, repo, parent = delivery
    root = await worktree(repo)
    storage = app.state.storage
    task, child, run, _ = await storage.create_task_with_run(parent["id"], task_input(root))
    await storage.transition_run(run["id"], {"queued"}, "running", {})
    sha, branch = await git_delivery.capture_base(root)
    await storage.attach_worktree(task["id"], run["id"], str(root), sha, branch)
    commit(root, "复用前推进 HEAD")
    runtime = orchestrator.SessionRuntime(child)
    await app.state.service._prepare_directory(runtime)
    assert (await storage.get_task(task["id"]))["base_sha"] == sha
    events = await storage.list_events(task["project_id"], child["id"], 0, 100)
    assert sum(event["kind"] == "task.worktree_ready" for event in events) == 1
    runtime.session = {**child, "native_id": "already-started", "cwd": str(repo)}
    await app.state.service._prepare_directory(runtime)
    assert runtime.session["cwd"] == str(repo)


@pytest.mark.asyncio
async def test_two_explicit_tasks_may_use_same_existing_worktree(delivery):
    http, app, factory, repo, _ = delivery
    root = await worktree(repo)
    before = await git_delivery.git(repo, "worktree", "list", "--porcelain")
    handles = [await delegate(delivery, task_input(root)) for _ in range(2)]
    agents = [await factory.started(), await factory.started()]
    by_session = {agent.host.runtime.session["id"]: agent for agent, _ in agents}
    assert set(by_session) == {handle["child_session_id"] for handle in handles}
    for handle in handles:
        agent = by_session[handle["child_session_id"]]
        assert agent.cwd == root
        assert (await app.state.storage.get_task(handle["task_id"]))["worktree"] == str(root)
        agent.finish()
        await terminal(http, handle["run_id"])
    assert await git_delivery.git(repo, "worktree", "list", "--porcelain") == before
