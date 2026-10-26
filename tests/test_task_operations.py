"""3.5：所属任务快照、有限等待和单任务取消；ACP 边界使用可控替身。"""

import asyncio
from uuid import uuid4

import pytest

from app.api import internal
from test_delegation import delegation, delegate, drain, task_input
from test_http_runs import live_server, new_session, submit, terminal
from test_permissions import pause_auto_answers, interactions, options
from test_storage import repository


async def query(context, handles, wait_ms):
    http, _, _, _, _, auth = context
    return await http.post("/internal/tasks/status", headers=auth, json={
        "task_ids": [handle["task_id"] for handle in handles], "wait_ms": wait_ms,
    })


async def result(context, handle):
    http, _, _, _, _, auth = context
    response = await http.post(f"/internal/tasks/{handle['task_id']}/result", json={}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


async def cancel(context, handle):
    http, _, _, _, _, auth = context
    response = await http.post(f"/internal/tasks/{handle['task_id']}/cancel", json={}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def snapshots(app, monkeypatch):
    observed = asyncio.Queue()
    read = app.state.storage.get_task_snapshots

    async def capture(parent_session_id, task_ids):
        rows = await read(parent_session_id, task_ids)
        observed.put_nowait(rows)
        return rows

    monkeypatch.setattr(app.state.storage, "get_task_snapshots", capture)
    return observed


@pytest.mark.asyncio
async def test_snapshot_order_result_repeat_and_artifacts(delegation):
    http, app, factory, _, _, _ = delegation
    first = await delegate(delegation, task_input())
    worker_a, _ = await factory.started()
    second = await delegate(delegation, task_input())
    worker_b, _ = await factory.started()
    status = await query(delegation, [second, first, second], 0)
    assert status.status_code == 200
    expected = [{**handle, "status": "running", "run_status": "running"}
                for handle in (second, first, second)]
    assert status.json() == {"tasks": expected}
    pending = await result(delegation, first)
    task = await app.state.storage.get_task(first["task_id"])
    assert pending == {**expected[1], "result": None, "error": None,
                       "worktree": task["worktree"], "branch": task["branch"]}
    assert task["worktree"] is task["branch"] is None
    worker_a.finish("完整结果\n第二行")
    await terminal(http, first["run_id"])
    finished = await result(delegation, first)
    assert finished == {**pending, "status": "review", "run_status": "completed", "result": "完整结果\n第二行"}
    assert await result(delegation, first) == finished
    assert (await cancel(delegation, first))["status"] == "review"
    assert factory.cancels == 0 and not worker_b.pending.done()
    async with app.state.storage.transaction() as db:
        await db.execute("UPDATE tasks SET status='accepted',worktree=?,branch=? WHERE id=?",
                         ("D:\\fixture-worktree", "fixture-branch", first["task_id"]))
    artifacts = await result(delegation, first)
    assert artifacts["status"] == "accepted"
    assert (artifacts["worktree"], artifacts["branch"]) == ("D:\\fixture-worktree", "fixture-branch")
    assert (await cancel(delegation, first))["status"] == "accepted"
    worker_b.finish()
    await drain(app, second)


@pytest.mark.asyncio
async def test_result_and_cancel_follow_current_run_instead_of_previous_attempt(delegation):
    _, app, factory, _, _, _ = delegation
    handle = await delegate(delegation, task_input())
    worker, _ = await factory.started()
    worker.finish("旧尝试的结果")
    await drain(app, handle)
    next_run = str(uuid4())
    async with app.state.storage.transaction() as db:
        await db.execute(
            "INSERT INTO runs (id,session_id,task_id,attempt,request_id,prompt,status,created_at) "
            "SELECT ?,session_id,task_id,attempt+1,?,prompt,'queued',created_at FROM runs WHERE id=?",
            (next_run, str(uuid4()), handle["run_id"]),
        )
        await db.execute("UPDATE tasks SET current_run_id=?,status='queued',summary=NULL,error=NULL WHERE id=?",
                         (next_run, handle["task_id"]))
    current = await result(delegation, handle)
    assert current["run_id"] == next_run and current["status"] == current["run_status"] == "queued"
    assert current["result"] is None
    assert (await cancel(delegation, handle))["status"] == "canceled"
    previous = await app.state.storage.get_run(handle["run_id"])
    assert previous["status"] == "completed" and previous["result"] == "旧尝试的结果"
    assert factory.cancels == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["failed", "interrupted", "canceled", "force_cancel"])
async def test_partial_result_on_failure_and_cancel(delegation, ending):
    http, app, factory, _, _, _ = delegation
    handle = await delegate(delegation, task_input())
    worker, _ = await factory.started()
    worker.emit("失败或取消前的内容")
    if ending == "failed":
        worker.pending.set_exception(RuntimeError("任务失败"))
    elif ending == "interrupted":
        await worker.disconnect()
    else:
        factory.force_cancel = ending == "force_cancel"
        assert (await cancel(delegation, handle))["status"] == "canceled"
    await terminal(http, handle["run_id"])
    await drain(app, handle)
    saved = await result(delegation, handle)
    assert saved["result"] == "失败或取消前的内容"
    assert saved["status"] == ("failed" if ending in {"failed", "interrupted"} else "canceled")
    assert saved["run_status"] == (ending if ending != "force_cancel" else "canceled")
    assert bool(saved["error"]) == (ending != "canceled")
    assert await result(delegation, handle) == saved


@pytest.mark.asyncio
async def test_wait_any_task_settles_and_main_permission_paths_keep_progressing(delegation, monkeypatch):
    http, app, factory, _, parent, _ = delegation
    pause_auto_answers(app.state.service, monkeypatch)
    first = await delegate(delegation, task_input())
    worker_a, _ = await factory.started()
    second = await delegate(delegation, task_input())
    worker_b, _ = await factory.started()
    observed = snapshots(app, monkeypatch)
    waiting = asyncio.create_task(query(delegation, [first, second], 1200000))
    try:
        await asyncio.wait_for(observed.get(), 3)
        assert not waiting.done()
        main_run = await submit(http, parent, "主会话在等待查询期间独立执行")
        main, _ = await factory.started()
        main.finish()
        await terminal(http, main_run["run_id"])
        child = await app.state.storage.get_session(second["child_session_id"])
        callback, _ = worker_b.permission(options())
        await interactions(http, child, 1)
        await asyncio.wait_for(observed.get(), 3)
        assert not waiting.done()
        worker_a.finish("先完成")
        response = await asyncio.wait_for(waiting, 3)
        assert response.status_code == 200
        assert [(task["status"], task["run_status"]) for task in response.json()["tasks"]] == [
            ("review", "completed"), ("running", "waiting_permission"),
        ]
        mixed = await asyncio.wait_for(query(delegation, [second, first], 1200000), 1)
        assert [task["task_id"] for task in mixed.json()["tasks"]] == [second["task_id"], first["task_id"]]
        assert not worker_b.pending.done()
        await cancel(delegation, second)
        assert (await callback).outcome.outcome == "cancelled"
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_wait_timeout_empty_input_and_batch_ownership(delegation):
    http, _, factory, project, _, auth = delegation
    handle = await delegate(delegation, task_input())
    worker, _ = await factory.started()
    assert (await asyncio.wait_for(query(delegation, [], 1200000), 1)).json() == {"tasks": []}
    started = asyncio.get_running_loop().time()
    response = await asyncio.wait_for(query(delegation, [handle], 80), 1)
    assert asyncio.get_running_loop().time() - started >= 0.07
    assert response.json()["tasks"][0]["status"] == "running"
    other = await new_session(http, project)
    main_run = await submit(http, other, "另一个父会话")
    other_agent, _ = await factory.started()
    other_auth = {"Authorization": "Bearer " + other_agent.token}
    alien = await http.post("/internal/tasks", json=task_input(), headers=other_auth)
    assert alien.status_code == 202
    alien_worker, _ = await factory.started()
    for task_id in (alien.json()["task_id"], str(uuid4())):
        denied = await http.post("/internal/tasks/status", headers=auth, json={
            "task_ids": [handle["task_id"], task_id], "wait_ms": 1200000,
        })
        assert denied.status_code == 404 and "tasks" not in denied.json()
        for suffix in ("result", "cancel"):
            assert (await http.post(f"/internal/tasks/{task_id}/{suffix}", headers=auth, json={})).status_code == 404
    assert factory.cancels == 0 and not worker.pending.done() and not alien_worker.pending.done()
    worker.finish()
    alien_worker.finish()
    other_agent.finish()
    await terminal(http, main_run["run_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["revoke", "close"])
async def test_wait_rechecks_token_and_stops_on_close(delegation, monkeypatch, ending):
    _, app, factory, _, _, auth = delegation
    handle = await delegate(delegation, task_input())
    worker, _ = await factory.started()
    observed = snapshots(app, monkeypatch)
    waiting = asyncio.create_task(query(delegation, [handle], 1200000))
    try:
        await asyncio.wait_for(observed.get(), 3)
        if ending == "revoke":
            app.state.service.tokens.pop(auth["Authorization"].split(" ")[1])
        else:
            await app.state.service.close()
        response = await asyncio.wait_for(waiting, 3)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        if ending == "revoke":
            assert not worker.pending.done()
            worker.finish()
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_wait_http_disconnect_ends_poll_without_canceling_worker(delegation, monkeypatch):
    _, app, factory, _, _, auth = delegation
    handle = await delegate(delegation, task_input())
    worker, _ = await factory.started()
    observed = snapshots(app, monkeypatch)
    finished = asyncio.Event()
    wait = internal.wait_for_tasks

    async def tracked(*args):
        try:
            return await wait(*args)
        finally:
            finished.set()

    monkeypatch.setattr(internal, "wait_for_tasks", tracked)
    async with live_server(app) as http:
        waiting = asyncio.create_task(http.post("/internal/tasks/status", headers=auth, json={
            "task_ids": [handle["task_id"]], "wait_ms": 1200000,
        }))
        try:
            await asyncio.wait_for(observed.get(), 3)
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            await asyncio.wait_for(finished.wait(), 3)
            assert factory.cancels == 0 and not worker.pending.done()
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    worker.finish("断线后仍完成")
    await drain(app, handle)
    assert (await result(delegation, handle))["result"] == "断线后仍完成"


@pytest.mark.asyncio
async def test_cancel_queued_task_never_starts_agent(delegation, monkeypatch):
    _, app, factory, _, _, _ = delegation
    entered, release = asyncio.Event(), asyncio.Event()
    execute = app.state.service.execute_run

    async def before_claim(run_id):
        entered.set()
        await release.wait()
        await execute(run_id)

    monkeypatch.setattr(app.state.service, "execute_run", before_claim)
    handle = await delegate(delegation, task_input())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert (await query(delegation, [handle], 0)).json()["tasks"][0]["run_status"] == "queued"
        assert (await cancel(delegation, handle))["status"] == "canceled"
        assert (await cancel(delegation, handle))["status"] == "canceled"
    finally:
        release.set()
    await drain(app, handle)
    assert len(factory.clients) == 1
    assert (await app.state.storage.get_run(handle["run_id"]))["started_at"] is None


@pytest.mark.asyncio
async def test_cancel_during_connection(delegation):
    _, app, factory, _, _, _ = delegation
    factory.connect_started.clear()
    factory.connect_release.clear()
    handle = await delegate(delegation, task_input())
    try:
        await asyncio.wait_for(factory.connect_started.wait(), 3)
        response = await asyncio.wait_for(cancel(delegation, handle), 3)
        assert response["status"] == response["run_status"] == "canceled"
        assert factory.starts.empty()
    finally:
        factory.connect_release.set()
    await drain(app, handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("completes", [False, True])
async def test_cancel_permission_race_and_sibling_isolation(delegation, completes, monkeypatch):
    _, app, factory, _, _, _ = delegation
    pause_auto_answers(app.state.service, monkeypatch)
    first = await delegate(delegation, task_input())
    worker_a, _ = await factory.started()
    second = await delegate(delegation, task_input())
    worker_b, _ = await factory.started()
    child = await app.state.storage.get_session(first["child_session_id"])
    callback, _ = worker_a.permission(options())
    await interactions(delegation[0], child, 1)
    factory.cancel_release.clear()
    canceling = asyncio.create_task(cancel(delegation, first))
    repeated = None
    try:
        await asyncio.wait_for(factory.cancel_entered.wait(), 3)
        state = (await query(delegation, [first, second], 0)).json()["tasks"]
        assert state[0]["status"] == "running" and state[0]["run_status"] == "cancel_requested"
        assert state[1]["run_status"] == "running" and not canceling.done()
        assert (await callback).outcome.outcome == "cancelled"
        repeated = asyncio.create_task(cancel(delegation, first))
        if completes:
            worker_a.finish("取消竞争中实际完成")
    finally:
        factory.cancel_release.set()
        outcome = await asyncio.wait_for(canceling, 3)
        if repeated is not None:
            assert await asyncio.wait_for(repeated, 3) == outcome
    assert outcome["status"] == ("review" if completes else "canceled")
    assert factory.cancels == 1 and not worker_b.pending.done()
    if completes:
        assert (await result(delegation, first))["result"] == "取消竞争中实际完成"
    worker_b.finish("兄弟任务独立完成")
    await drain(app, second)
    assert (await result(delegation, second))["status"] == "review"
