import asyncio
import json
import sqlite3
from uuid import uuid4
from pathlib import Path

import pytest
import pytest_asyncio

from app.db.compact_history import compact_database
from app.db.event_codec import compact, pack_payload, restore_payload, utf8_parts
from app.db.lease import DatabaseLease
from app.db.storage import Storage
from app.domain.models import MessageCreate, RunStatus
from app.services.run_journal import JournalBudget, JournalOverloadError, RunJournal


@pytest_asyncio.fixture
async def journal_store(tmp_path):
    storage = Storage(tmp_path / "history.db")
    await storage.open()
    async with storage.transaction() as db:
        await db.execute("INSERT INTO projects VALUES ('p','p','D:/fixture','[]','now')")
        await db.execute("INSERT INTO sessions(id,project_id,harness,cwd,created_at) VALUES ('s','p','codex','D:/fixture','now')")
    run, _ = await storage.create_run("s", MessageCreate(message="输入", request_id=str(uuid4())))
    yield storage, run["id"]
    await storage.close()


def text(value, channel="message", **metadata):
    return {"kind": "agent.text", "payload": {"text": value, "channel": channel, **metadata}}


async def texts(storage):
    return [json.loads(row["payload"]) for row in await storage.list_events("p", "s", 0, 100) if row["kind"] == "agent.text"]


@pytest.mark.asyncio
async def test_idle_timer_and_continuous_output_do_not_extend_deadline(journal_store):
    storage, run = journal_store
    journal = RunJournal(storage, run)
    journal.receive(text("首段"))
    for _ in range(7):
        await asyncio.sleep(0.025)
        journal.receive(text("续"))
    assert (await texts(storage))[0]["text"].startswith("首段")
    await asyncio.sleep(0.14)
    assert "".join(row["text"] for row in await texts(storage)) == "首段" + "续" * 7
    await journal.close()
    assert journal.bytes == journal.items == 0


@pytest.mark.asyncio
async def test_unicode_threshold_metadata_channel_control_and_close(journal_store):
    storage, run = journal_store
    journal = RunJournal(storage, run)
    journal.receive(text("中" * 3000))
    journal.receive(text("甲", metadata={"scope": 1}))
    journal.receive(text("乙", metadata={"scope": 2}))
    journal.receive(text("思考", "thought"))
    await journal.call(lambda: storage.append_event(run, "permission.test", {}))
    journal.receive(text("尾部"))
    await journal.close()
    rows = await texts(storage)
    assert "".join(row["text"] for row in rows) == "中" * 3000 + "甲乙思考尾部"
    assert all(len(row["text"].encode()) <= 4096 for row in rows)
    assert [row.get("metadata") for row in rows][-4:] == [{"scope": 1}, {"scope": 2}, None, None]
    events = await storage.list_events("p", "s", 0, 100)
    assert [row["kind"] for row in events][-2:] == ["permission.test", "agent.text"]


@pytest.mark.asyncio
async def test_timestamped_stream_merges_before_budget_and_commits_batch(journal_store, monkeypatch):
    storage, run = journal_store
    calls = []
    original = storage.append_events

    async def record(*args):
        calls.append(len(args[1]))
        return await original(*args)

    monkeypatch.setattr(storage, "append_events", record)
    journal = RunJournal(storage, run, max_items=4)
    for index in range(3000):
        journal.receive(text("a", "thought", metadata={
            "messageId": "thought-1", "_meta": {"timestamp": f"t{index}"},
        }))
    journal.receive(text("回答", metadata={"messageId": "answer-1"}))
    journal.receive(text("继续", "thought", metadata={"messageId": "thought-2"}))
    assert journal.items == 3 and journal.error is None
    await journal.flush()
    rows = await texts(storage)
    assert calls == [3]
    assert [(row["channel"], row["text"]) for row in rows] == [
        ("thought", "a" * 3000), ("message", "回答"), ("thought", "继续"),
    ]
    assert rows[0]["metadata"]["_meta"] == {"timestamp_start": "t0", "timestamp": "t2999"}
    await journal.close()
    assert journal.items == journal.budget.items == journal.budget.bytes == 0


@pytest.mark.asyncio
async def test_tool_burst_shares_transaction_and_preserves_all_deltas(journal_store, monkeypatch):
    storage, run = journal_store
    batches = []
    original = storage.append_events

    async def record(run_id, events):
        batches.append(len(events))
        return await original(run_id, events)

    monkeypatch.setattr(storage, "append_events", record)
    journal = RunJournal(storage, run)
    for index in range(80):
        journal.receive({"kind": "agent.tool", "payload": {"toolCallId": "t", "title": str(index)}})
    await journal.flush()
    assert batches == [80]
    rows = await storage.list_events("p", "s", 0, 100)
    assert [json.loads(row["payload"])["title"] for row in rows if row["kind"] == "agent.tool"] == [str(i) for i in range(80)]
    await journal.close()
    assert journal.items == journal.budget.items == 0


@pytest.mark.asyncio
async def test_writer_combines_backlog_without_crossing_control_barrier(journal_store, monkeypatch):
    storage, run = journal_store
    entered, release = asyncio.Event(), asyncio.Event()
    batches = []
    original = storage.append_events

    async def slow(run_id, events):
        if all(kind == "agent.text" for kind, _ in events):
            batches.append([payload["text"] for _, payload in events])
            if len(batches) == 1:
                entered.set()
                await release.wait()
        return await original(run_id, events)

    monkeypatch.setattr(storage, "append_events", slow)
    journal = RunJournal(storage, run)
    journal.receive(text("首"))
    journal._seal_batch()
    await entered.wait()
    for value in ("甲", "乙", "丙"):
        journal.receive(text(value))
        journal._seal_batch()
    control = asyncio.create_task(journal.call(lambda: storage.append_event(run, "permission.test", {})))
    await asyncio.sleep(0)
    journal.receive(text("尾"))
    release.set()
    await control
    await journal.close()
    assert batches == [["首"], ["甲", "乙", "丙"], ["尾"]]
    rows = await storage.list_events("p", "s", 0, 100)
    assert [row["kind"] for row in rows][-2:] == ["permission.test", "agent.text"]
    assert journal.queue._unfinished_tasks == 0 and journal.budget.bytes == 0


@pytest.mark.asyncio
async def test_flush_is_commit_ack_and_sse_query_cannot_see_pending(journal_store, monkeypatch):
    storage, run = journal_store
    entered, release = asyncio.Event(), asyncio.Event()
    original = storage.append_events

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(storage, "append_events", slow)
    journal = RunJournal(storage, run)
    journal.receive(text("待提交"))
    flushing = asyncio.create_task(journal.flush())
    await entered.wait()
    assert not flushing.done() and await texts(storage) == []
    release.set()
    await flushing
    assert (await texts(storage))[0]["text"] == "待提交"
    await journal.close()


@pytest.mark.asyncio
async def test_flush_during_close_waits_for_the_same_commit(journal_store, monkeypatch):
    storage, run = journal_store
    entered, release = asyncio.Event(), asyncio.Event()
    original = storage.append_events

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(storage, "append_events", slow)
    journal = RunJournal(storage, run)
    journal.receive(text("关闭尾部"))
    closing = asyncio.create_task(journal.close())
    await entered.wait()
    flushing = asyncio.create_task(journal.flush())
    await asyncio.sleep(0)
    assert not flushing.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(closing, flushing), 1)
    assert (await texts(storage))[0]["text"] == "关闭尾部"


@pytest.mark.asyncio
async def test_budget_includes_buffer_and_drains_accepted_events(journal_store, monkeypatch):
    storage, run = journal_store
    budget = JournalBudget(max_items=4)
    journal = RunJournal(storage, run, budget, max_items=3)
    journal.receive(text("一", metadata={"part": 1}))
    await asyncio.sleep(0)
    journal.receive(text("二", metadata={"part": 2}))
    journal.receive(text("三", metadata={"part": 3}))
    journal.receive(text("拒绝", metadata={"part": 4}))
    assert journal.items == 3 and journal.failed.is_set()
    assert isinstance(journal.error, JournalOverloadError)
    journal.receive(text("仍拒绝"))
    with pytest.raises(JournalOverloadError):
        await journal.close()
    assert "".join(row["text"] for row in await texts(storage)) == "一二三"
    assert journal.items == budget.items == budget.bytes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("flush_first", [True, False])
async def test_writer_failure_is_reported_and_budget_released(journal_store, monkeypatch, flush_first):
    storage, run = journal_store

    async def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(storage, "append_events", broken)
    journal = RunJournal(storage, run)
    journal.receive(text("不可假称成功"))
    if flush_first:
        with pytest.raises(sqlite3.OperationalError):
            await journal.flush()
    with pytest.raises(sqlite3.OperationalError):
        await journal.close()
    assert journal.items == journal.budget.items == journal.budget.bytes == 0


@pytest.mark.asyncio
async def test_terminal_builds_result_without_repeating_it_in_status(journal_store):
    storage, run = journal_store
    await storage.transition_run(run, {RunStatus.QUEUED}, RunStatus.RUNNING, {})
    journal = RunJournal(storage, run)
    journal.receive(text("正文"))
    journal.receive(text("隐含思考", "thought"))
    journal.receive(text("尾部"))
    await journal.call(lambda: storage.transition_run(run, {RunStatus.RUNNING}, RunStatus.COMPLETED, {}))
    await journal.close()
    assert (await storage.get_run(run))["result"] == "正文尾部"
    assert "result" not in json.loads((await storage.list_events("p", "s", 0, 100))[-1]["payload"])


@pytest.mark.asyncio
async def test_detail_roundtrip_pagination_and_tool_append_replace(journal_store):
    storage, run = journal_store
    journal = RunJournal(storage, run)
    output = "中文输出" * 20000
    journal.receive({"kind": "agent.tool", "payload": {"toolCallId": "tool", "_meta": {
        "terminal_output_delta": {"terminal_id": "exec", "data": output}}}})
    journal.receive({"kind": "agent.tool", "payload": {"toolCallId": "tool", "status": "completed",
        "rawOutput": {"formatted_output": output, "exit_code": 7}}})
    await journal.close()
    light = [row for row in await storage.list_events("p", "s", 0, 100) if row["kind"] == "agent.tool"]
    delta = json.loads(light[0]["payload"])["_meta"]["terminal_output_delta"]
    assert delta["operation"] == "append" and len(delta["preview"]) <= 160
    full = [json.loads(row["payload"]) for row in await storage.full_events("p", "s", 0, 100) if row["kind"] == "agent.tool"]
    assert full[0]["_meta"]["terminal_output_delta"]["data"] == output
    assert full[1]["rawOutput"] == {"exit_code": 7} and full[1]["output_repeated"]
    after, recovered = -1, ""
    while True:
        page = await storage.detail_page(light[0]["id"], after)
        assert sum(len(part["text"].encode()) for part in page["parts"]) <= 65536
        recovered += "".join(part["text"] for part in page["parts"])
        if page["next"] is None:
            break
        after = page["next"]
    assert json.loads(recovered)["data"] == output
    assert len(compact(light).encode()) < 2000


@pytest.mark.asyncio
async def test_snapshot_twenty_runs_and_independent_event_high_watermark(journal_store):
    storage, first = journal_store
    for index in range(24):
        await storage.create_run("s", MessageCreate(message=str(index), request_id=str(uuid4())))
    page = await storage.history("s")
    assert len(page["runs"]) == 20 and page["before"]
    old = await storage.history("s", page["before"])
    assert len(old["runs"]) == 5 and old["before"] is None
    added = await storage.append_event(first, "agent.text", {"channel": "message", "text": "间隙"})
    prefix = await storage.list_events("p", "s", 0, 100, [first], page["high_watermark"])
    assert added["id"] not in {row["id"] for row in prefix}
    replay = await storage.list_events("p", "s", page["high_watermark"], 100)
    assert [row["id"] for row in replay] == [added["id"]]
    assert "prompt" not in page["runs"][0] and "result" not in page["runs"][0]


@pytest.mark.asyncio
async def test_offline_backup_conversion_equivalence_and_sequence(journal_store, tmp_path):
    storage, run = journal_store
    for content in ("甲", "乙", "丙"):
        await storage.append_event(run, "agent.text", {"text": content, "channel": "message"})
    await storage.append_event(run, "agent.tool", {"toolCallId": "t", "rawOutput": "日志" * 9000})
    high = (await storage.list_events("p", "s", 0, 100))[-1]["id"]
    await storage.close()
    output = tmp_path / "converted.db"
    report = await asyncio.to_thread(compact_database, storage.path, output, stopped=True)
    assert report["integrity_check"] == "ok" and report["output_events"] < report["source_events"]
    converted = Storage(output)
    await converted.open()
    try:
        assert "".join(row["text"] for row in await texts(converted)) == "甲乙丙"
        event = await converted.append_event(run, "test", {})
        assert event["id"] > high
    finally:
        await converted.close()
    assert Path(report["backup"]).exists()


def test_backend_lease_prevents_offline_replacement(tmp_path):
    path = tmp_path / "lease.db"
    with DatabaseLease(path):
        with pytest.raises(RuntimeError, match="停服"):
            with DatabaseLease(path):
                pass


@pytest.mark.asyncio
async def test_shared_budget_stops_only_overloaded_run(journal_store):
    storage, run = journal_store
    budget = JournalBudget(max_items=3)
    first, second = RunJournal(storage, run, budget), RunJournal(storage, run, budget)
    first.receive(text("一", metadata={"part": 1}))
    first.receive(text("二", metadata={"part": 2}))
    await asyncio.sleep(0)
    second.receive(text("三", metadata={"part": 3}))
    second.receive(text("拒绝", metadata={"part": 4}))
    assert first.error is None and isinstance(second.error, JournalOverloadError)
    await first.close()
    with pytest.raises(JournalOverloadError):
        await second.close()
    assert budget.items == budget.bytes == 0


@pytest.mark.asyncio
async def test_detail_failure_rolls_back_event_and_detail_together(journal_store):
    storage, run = journal_store
    before = await storage.list_events("p", "s", 0, 100)
    async with storage.transaction() as db:
        await db.execute("CREATE TEMP TRIGGER reject_detail BEFORE INSERT ON event_details BEGIN SELECT RAISE(ABORT,'detail failed'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await storage.append_event(run, "agent.tool", {"rawOutput": "大段输出" * 30000})
    assert await storage.list_events("p", "s", 0, 100) == before
    assert pack_payload("agent.tool", {"rawOutput": "x"})[1][0][2] == "utf8"


@pytest.mark.asyncio
async def test_event_batch_rolls_back_all_rows_when_detail_fails(journal_store):
    storage, run = journal_store
    before = await storage.list_events("p", "s", 0, 100)
    async with storage.transaction() as db:
        await db.execute("CREATE TEMP TRIGGER reject_batch_detail BEFORE INSERT ON event_details "
                         "BEGIN SELECT RAISE(ABORT,'detail failed'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await storage.append_events(run, [
            ("agent.text", {"channel": "message", "text": "先写"}),
            ("agent.tool", {"rawOutput": "大段输出" * 30000}),
        ])
    assert await storage.list_events("p", "s", 0, 100) == before


@pytest.mark.asyncio
async def test_offline_apply_preserves_backup_and_wal_commits(journal_store, tmp_path):
    storage, run = journal_store
    await storage.append_event(run, "agent.text", {"channel": "message", "text": "甲"})
    await storage.append_event(run, "agent.text", {"channel": "message", "text": "乙"})
    # 在 WAL 仍打开时使用 backup API，确认备份包含已提交内容。
    output = tmp_path / "apply.db"
    report = await asyncio.to_thread(compact_database, storage.path, output, stopped=True)
    assert report["source_events"] == 4
    await storage.close()
    applied = await asyncio.to_thread(compact_database, storage.path, tmp_path / "next.db", stopped=True, apply=True)
    assert applied["applied"] and Path(applied["backup"]).exists()
    assert not Path(str(storage.path) + "-wal").exists()
    await storage.open()
    assert (await texts(storage)) == [{"text": "甲乙", "channel": "message"}]


@pytest.mark.asyncio
async def test_offline_legacy_title_becomes_business_event(journal_store, tmp_path):
    storage, run = journal_store
    raw = {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "native", "update": {
        "sessionUpdate": "session_info_update", "title": "旧标题"}}}
    await storage.append_event(run, "raw", {"raw": raw})
    await storage.close()
    output = tmp_path / "title.db"
    await asyncio.to_thread(compact_database, storage.path, output, stopped=True)
    converted = Storage(output)
    await converted.open()
    try:
        assert (await converted.get_session_summary("s"))["title"] == "旧标题"
    finally:
        await converted.close()
