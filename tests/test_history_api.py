import json

import pytest

from test_http_runs import api, http_records, live_server, setup_app, repository
from test_events import next_event, params


@pytest.mark.asyncio
async def test_history_gzip_high_watermark_and_sse_remains_uncompressed(api, http_records):
    client, app, _ = api
    project, session = http_records
    from app.domain.models import MessageCreate
    from uuid import uuid4
    for number in range(22):
        run, _ = await app.state.storage.create_run(session["id"], MessageCreate(message="输入" * 2000, request_id=str(uuid4())))
        await app.state.storage.append_event(run["id"], "agent.text", {"text": str(number), "channel": "message"})
    url = f"/api/sessions/{session['id']}/history"
    response = await client.get(url, headers={"Accept-Encoding": "gzip"})
    assert response.headers["content-encoding"] == "gzip"
    page = response.json()
    assert len(page["runs"]) == 20 and page["before"]
    assert all("prompt" not in run and "result" not in run for run in page["runs"])
    snapshot_high = page["high_watermark"]
    appended = await app.state.storage.append_event(run["id"], "agent.text", {"text": "快照间隙", "channel": "message"})
    selected = (await client.get("/api/events", params={**params(project, session),
                "run_id": run["id"], "through_event_id": snapshot_high})).json()
    assert all(event["run_id"] == run["id"] and event["id"] <= snapshot_high for event in selected)
    async with live_server(app) as tcp:
        async with tcp.stream("GET", "/api/stream", params=params(project, session, snapshot_high),
                              headers={"Accept-Encoding": "gzip"}) as stream:
            assert "content-encoding" not in stream.headers
            received, _ = await next_event(stream.aiter_lines())
            assert received["id"] == appended["id"]


@pytest.mark.asyncio
async def test_tool_details_cursor_binds_owner_and_high_watermark(api, http_records):
    client, app, _ = api
    _, session = http_records
    from app.domain.models import MessageCreate
    from uuid import uuid4
    run, _ = await app.state.storage.create_run(session["id"], MessageCreate(message="详情", request_id=str(uuid4())))
    await app.state.storage.append_event(run["id"], "agent.tool", {"toolCallId": "工具", "rawOutput": "中文" * 50000})
    url = f"/api/runs/{run['id']}/tools/工具/details"
    first = (await client.get(url)).json()
    assert len(first["records"][0]["parts"][0]["text"].encode()) <= 65536
    await app.state.storage.append_event(run["id"], "agent.tool", {"toolCallId": "工具", "rawOutput": "分页之后的新记录"})
    cursor = first["next"]
    content = first["records"][0]["parts"][0]["text"]
    while cursor:
        response = await client.get(url, params={"cursor": cursor})
        assert response.status_code == 200
        page = response.json()
        assert page["high_watermark"] == first["high_watermark"]
        content += "".join(part["text"] for record in page["records"] for part in record["parts"])
        cursor = page["next"]
    assert content == "中文" * 50000
    assert (await client.get(url.replace("工具", "其他"), params={"cursor": first["next"]})).status_code == 422
    assert (await client.get(url, params={"cursor": first["next"], "high_watermark": first["high_watermark"] + 1})).status_code == 422
    assert (await client.get(url, params={"cursor": "bad"})).status_code == 422


@pytest.mark.asyncio
async def test_overload_stops_harness_and_preserves_accepted_tail(api, http_records):
    from test_http_runs import submit, terminal
    client, app, factory = api
    _, session = http_records
    app.state.service.journal_budget.max_items = 2
    handle = await submit(client, session, "预算")
    agent, _ = await factory.started()
    for index, text in enumerate(("一", "二", "拒绝", "仍拒绝")):
        agent.emit(text, message_id=str(index))
    run = await terminal(client, handle["run_id"])
    assert run["status"] == "failed" and "过载" in run["error"]
    assert run["result"] == "一二" and agent.closed
    assert app.state.service.journal_budget.items == 0


@pytest.mark.asyncio
async def test_cancellation_keeps_output_until_harness_stops(api, http_records, monkeypatch):
    from test_http_runs import ControlledClient, submit
    client, _, factory = api
    _, session = http_records
    handle = await submit(client, session, "取消")
    agent, _ = await factory.started()
    original = ControlledClient.cancel

    async def cancel(client):
        client.emit("停止之前的尾部")
        await original(client)

    monkeypatch.setattr(ControlledClient, "cancel", cancel)
    result = (await client.post(f"/api/runs/{handle['run_id']}/cancel")).json()
    assert result["status"] == "canceled" and result["result"] == "停止之前的尾部"
