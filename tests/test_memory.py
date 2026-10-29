import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from pydantic import TypeAdapter

from app.db.storage import RequestConflictError, Storage
from app.memory.models import MemorySettings, RetainInput
from app.domain.models import MessageCreate, RunStatus, SessionCreate
from app.main import create_app
from app.memory.service import MemoryService
from app.memory.config import default_config, merge_config, public_config
from app.memory.storage import capture_completed_run, completed_run_content
from app.memory.engine.hindsight import BANK, HindsightEngine
from test_storage import repository, project_input


class Engine:
    def __init__(self, config):
        self.calls = []
        self.states = {}
        self.pending = 1

    async def start(self):
        pass

    async def close(self):
        pass

    async def ensure_bank(self, member_id):
        pass

    async def submit(self, operation):
        self.calls.append(('submit', operation['id']))
        self.states.setdefault(operation['id'], 'pending')
        return {'operation_id': operation['id']}

    async def operation(self, key, member_id=None):
        return {'status': self.states[key]}

    async def delete(self, source, member_id=None):
        self.calls.append(('delete', source))

    async def freshness(self, member_id=None):
        return {'pending_consolidation': self.pending, 'failed_consolidation': 0}

    async def recall(self, query, member_id=None):
        return {'results': [{'text': '用户偏好简短报告', 'document_id': 'test-source'}]}


@pytest_asyncio.fixture
async def memory(tmp_path):
    storage = Storage(tmp_path / 'memory.db')
    await storage.open()
    service = MemoryService(storage, Engine)
    service.config['enabled'] = True
    service.config['auto_retain_turns'] = 5
    await service.store.save_config(service.config)
    yield service
    await service.close()
    await storage.close()


async def complete_turn(storage, session_id, prompt):
    run, _ = await storage.create_run(session_id, MessageCreate(request_id=str(uuid4()), message=prompt))
    assert await storage.transition_run(run['id'], {RunStatus.QUEUED}, RunStatus.RUNNING, {})
    await storage.append_event(run['id'], 'agent.text', {'channel': 'message', 'text': f'回复：{prompt}'})
    assert await storage.transition_run(run['id'], {RunStatus.RUNNING}, RunStatus.COMPLETED, {})
    return run


@pytest.mark.asyncio
async def test_feedback_capture_uses_time_index_and_preserves_event_order(memory, repository):
    storage = memory.storage
    project = await storage.create_project(project_input(repository))
    session = await storage.create_session(SessionCreate(project_id=project['id'], harness='codex'))
    async with storage.transaction() as db:
        await db.executemany(
            'INSERT INTO events(project_id,session_id,kind,payload,created_at) VALUES (?,?,?,?,?)',
            [(project['id'], session['id'], 'agent.text', '{}', '2026-10-29T12:00:00Z')] * 2000,
        )
        for text, hour in [('旧补充', '09'), ('第一条', '13'), ('第二条', '11'), ('晚补充', '15')]:
            await db.execute(
                'INSERT INTO events(project_id,session_id,kind,payload,created_at) VALUES (?,?,?,?,?)',
                (project['id'], session['id'], 'feedback.pulled', json.dumps({'message': text}), f'2026-10-29T{hour}:00:00Z'),
            )
        query = "SELECT payload FROM events WHERE session_id=? AND kind='feedback.pulled' AND created_at>=? AND created_at<=? ORDER BY id"
        parameters = (session['id'], '2026-10-29T10:00:00Z', '2026-10-29T14:00:00Z')
        async with db.execute('EXPLAIN QUERY PLAN ' + query, parameters) as cursor:
            plan = ' '.join(row['detail'] for row in await cursor.fetchall())
        assert 'events_feedback_time' in plan and 'created_at>?' in plan
        content, _ = await completed_run_content(db, {
            'id': 'synthetic', 'session_id': session['id'], 'task_id': None, 'prompt': '任务',
            'started_at': parameters[1], 'finished_at': parameters[2],
        })
    assert content.index('第一条') < content.index('第二条')
    assert '旧补充' not in content and '晚补充' not in content


@pytest.mark.asyncio
async def test_idempotency_and_serial_delete(memory):
    body = RetainInput(request_id=uuid4(), content='用户喜欢简明报告')
    source = await memory.retain(body)
    assert (await memory.retain(body))['id'] == source['id']
    with pytest.raises(RequestConflictError):
        await memory.retain(body.model_copy(update={'content': 'different'}))
    await memory.tick()
    engine = memory.engine
    key = source['revisions'][0]['id']
    await memory.store.revise(source['id'], 1, '', True)
    await memory.tick()
    assert not any(call[0] == 'delete' for call in engine.calls)
    engine.states[key] = 'completed'
    await memory.tick()
    await memory.tick()
    assert ('delete', source['id']) in engine.calls
    assert (await memory.store.source(source['id']))['revisions'][-1]['status'] == 'deleted'
    with pytest.raises(RequestConflictError):
        await memory.store.revise(source['id'], 1, 'stale', False)


@pytest.mark.asyncio
async def test_recovery_reuses_operation_and_snapshot(memory):
    source = await memory.retain(RetainInput(request_id=uuid4(), content='事实'))
    engine = await memory._ready()
    operation = source['revisions'][0]
    operation['metadata'] = json.dumps(operation['metadata'])
    await engine.submit(operation)  # 模拟上游接收后、本地确认前进程终止。
    await memory.tick()
    assert len(engine.states) == 1
    engine.states[operation['id']] = 'completed'
    await memory.tick()
    assert (await memory.store.operations())[0]['status'] == 'written'
    await memory.tick()
    assert (await memory.store.operations())[0]['status'] == 'consolidating'
    engine.pending = 0
    await memory.tick()
    assert (await memory.store.operations())[0]['status'] == 'complete'


@pytest.mark.asyncio
async def test_capture_transaction_and_context(memory, repository):
    storage = memory.storage
    project = await storage.create_project(project_input(repository))
    session = await storage.create_session(SessionCreate(project_id=project['id'], harness='codex'))
    for index in range(4):
        await complete_turn(storage, session['id'], f'前置回合 {index + 1}')
    assert await memory.store.sources() == []
    run, _ = await storage.create_run(session['id'], MessageCreate(request_id=str(uuid4()), message='报告用中文'))
    await storage.transition_run(run['id'], {RunStatus.QUEUED}, RunStatus.RUNNING, {})
    await storage.append_event(run['id'], 'agent.text', {'channel': 'thought', 'text': '秘密思考'})
    await storage.append_event(run['id'], 'agent.text', {'channel': 'message', 'text': '好的，使用中文'})
    await storage.transition_run(run['id'], {RunStatus.RUNNING}, RunStatus.COMPLETED, {})
    source = await memory.store.source(f"run:{run['id']}")
    detail = await memory.store.source(source['id'])
    assert '秘密思考' not in detail['revisions'][0]['content']
    assert '使用中文' in detail['revisions'][0]['content']
    assert detail['member_id'] is None
    prompt = await memory.augment(run)
    snapshot = await storage._fetch_one('SELECT * FROM memory_contexts WHERE run_id=?', (run['id'],))
    assert snapshot['context'] in prompt and snapshot['status'] == 'loaded'
    canceled, _ = await storage.create_run(session['id'], MessageCreate(request_id=str(uuid4()), message='不要保存'))
    await storage.transition_run(canceled['id'], {RunStatus.QUEUED}, RunStatus.CANCELED, {})
    assert len(await memory.store.sources()) == 5


@pytest.mark.asyncio
async def test_capture_batches_all_five_turns_without_resubmitting(memory, repository):
    storage = memory.storage
    project = await storage.create_project(project_input(repository))
    session = await storage.create_session(SessionCreate(project_id=project['id'], harness='codex'))
    runs = []
    for index in range(10):
        runs.append(await complete_turn(storage, session['id'], f'唯一输入 {index + 1:02d}'))
        assert len(await memory.store.sources()) == (index + 1) // 5 * 5
    sources = {row['run_id']: await memory.store.source(row['id']) for row in await memory.store.sources()}
    for index, run in enumerate(runs):
        source = sources[run['id']]
        revision = source['revisions'][0]
        assert revision['metadata']['run_id'] == run['id']
        assert revision['metadata']['session_id'] == session['id']
        assert 'turns' not in revision['metadata']
        for other in range(10):
            assert (f'唯一输入 {other + 1:02d}' in revision['content']) == (index == other)
    async with storage.transaction() as db:
        for run in runs:
            await capture_completed_run(db, run['id'])
    assert len(await memory.store.sources()) == 10
    assert len(await memory.store.operations()) == 10
    assert await storage._fetch_all('SELECT * FROM memory_capture_turns', ()) == []
    await storage.close()
    await storage.open()
    async with storage.transaction() as db:
        for run in runs:
            await capture_completed_run(db, run['id'])
    assert len(await memory.store.operations()) == 10
    await memory.tick()
    assert len(memory.engine.calls) == 10
    assert await storage._fetch_one('SELECT run_id FROM memory_capture_turns WHERE source_id IS NULL', ()) is None
    submitted = []
    async def upstream(bank, contents, *, operation_id, request_context):
        assert bank == BANK
        # 上游 MemoryFact.metadata 要求字符串值；空值由上游清理。
        TypeAdapter(dict[str, str | None]).validate_python(contents[0]['metadata'])
        submitted.append(contents[0])
        return {'operation_id': operation_id}
    adapter = HindsightEngine(memory.config)
    async def configured(*args, **kwargs):
        return SimpleNamespace(config={'retain_chunk_size': 3000, 'retain_structured_chunk_size': None})
    adapter.engine = SimpleNamespace(submit_async_retain=upstream, ensure_bank_profile=configured, update_bank_config=configured,
                                    get_bank_config=configured)
    adapter.context = None
    for operation in await memory.store.pending():
        await adapter.submit(operation)
    assert len(submitted) == 10
    for item in submitted:
        original = await memory.store.source(item['document_id'])
        metadata = original['revisions'][0]['metadata']
        assert item['metadata']['run_id'] == metadata['run_id']
        assert all(r['run_id'] == metadata['run_id'] for r in json.loads(item['content']))
        assert 'messages' not in item['metadata']


@pytest.mark.asyncio
async def test_reopen_cleans_legacy_submitted_buffers_only(memory, repository):
    storage = memory.storage
    project = await storage.create_project(project_input(repository))
    session = await storage.create_session(SessionCreate(project_id=project['id'], harness='codex'))
    for index in range(6):
        await complete_turn(storage, session['id'], f'捕获 {index}')
    sources = await memory.store.sources()
    async with storage.transaction() as db:
        for source in sources:
            detail = await memory.store.source(source['id'])
            revision = detail['revisions'][0]
            await db.execute(
                'INSERT INTO memory_capture_turns(run_id,session_id,content,metadata,source_id) VALUES (?,?,?,?,?)',
                (source['run_id'], session['id'], revision['content'], json.dumps(revision['metadata']), source['id']),
            )
    await storage.close()
    await storage.open()
    rows = await storage._fetch_all('SELECT * FROM memory_capture_turns', ())
    assert len(rows) == 1 and rows[0]['source_id'] is None
    assert len(await memory.store.sources()) == len(await memory.store.operations()) == 5


@pytest.mark.asyncio
async def test_capture_counts_per_session_only_while_enabled(memory, repository):
    storage = memory.storage
    project = await storage.create_project(project_input(repository))
    session = await storage.create_session(SessionCreate(project_id=project['id'], harness='codex'))
    other = await storage.create_session(SessionCreate(project_id=project['id'], harness='codex'))
    for switch in ('enabled', 'auto_retain'):
        memory.config[switch] = False
        await memory.store.save_config(memory.config)
        await complete_turn(storage, session['id'], f'未启用 {switch}')
        memory.config[switch] = True
    await memory.store.save_config(memory.config)
    for status in (RunStatus.CANCELED, RunStatus.FAILED, RunStatus.INTERRUPTED):
        run, _ = await storage.create_run(session['id'], MessageCreate(request_id=str(uuid4()), message='异常回合'))
        await storage.transition_run(run['id'], {RunStatus.QUEUED}, RunStatus.RUNNING, {})
        await storage.transition_run(run['id'], {RunStatus.RUNNING}, status, {})
    for index in range(4):
        await complete_turn(storage, session['id'], f'会话 A {index}')
        await complete_turn(storage, other['id'], f'会话 B {index}')
    assert await memory.store.sources() == []
    last = await complete_turn(storage, session['id'], '会话 A 第五轮')
    sources = await memory.store.sources()
    assert len(sources) == 5
    detail = await memory.store.source(f"run:{last['id']}")
    assert '未启用' not in detail['revisions'][0]['content']
    assert '异常回合' not in detail['revisions'][0]['content']
    assert '会话 B' not in detail['revisions'][0]['content']
    await complete_turn(storage, other['id'], '会话 B 第五轮')
    assert len(await memory.store.sources()) == 10


@pytest.mark.asyncio
async def test_capture_buffer_survives_restart_and_manual_save_is_immediate(memory, repository):
    storage = memory.storage
    project = await storage.create_project(project_input(repository))
    session = await storage.create_session(SessionCreate(project_id=project['id'], harness='codex'))
    for index in range(4):
        await complete_turn(storage, session['id'], f'重启前 {index}')
    manual = await memory.retain(RetainInput(request_id=uuid4(), content='手动即时保存'))
    mcp = await memory.retain(RetainInput(request_id=uuid4(), content='MCP 即时保存'), session['id'])
    assert manual['revisions'][0]['status'] == mcp['revisions'][0]['status'] == 'queued'
    await storage.close()
    await storage.open()
    last = await complete_turn(storage, session['id'], '重启后的第五轮')
    source = await memory.store.source(f"run:{last['id']}")
    assert source['revisions'][0]['metadata']['run_id'] == last['id']
    assert '重启前 0' not in source['revisions'][0]['content']
    details = [await memory.store.source(s['id']) for s in await memory.store.sources()]
    assert any('重启前 0' in s['revisions'][0]['content'] for s in details)
    assert len(await memory.store.sources()) == 7


@pytest.mark.asyncio
async def test_capture_batch_failure_rolls_back_completed_transition(memory, repository, monkeypatch):
    storage = memory.storage
    project = await storage.create_project(project_input(repository))
    session = await storage.create_session(SessionCreate(project_id=project['id'], harness='codex'))
    for index in range(4):
        await complete_turn(storage, session['id'], f'事务前 {index}')
    run, _ = await storage.create_run(session['id'], MessageCreate(request_id=str(uuid4()), message='第五轮'))
    await storage.transition_run(run['id'], {RunStatus.QUEUED}, RunStatus.RUNNING, {})
    from app.memory.storage import insert_source
    registered = []
    async def broken_insert(*args):
        if registered:
            raise RuntimeError('模拟来源登记失败')
        await insert_source(*args)
        registered.append(args[1])
    with monkeypatch.context() as patch:
        patch.setattr('app.memory.storage.insert_source', broken_insert)
        with pytest.raises(RuntimeError, match='模拟来源登记失败'):
            await storage.transition_run(run['id'], {RunStatus.RUNNING}, RunStatus.COMPLETED, {})
    row = await storage._fetch_one('SELECT status FROM runs WHERE id=?', (run['id'],))
    assert row['status'] == RunStatus.RUNNING
    pending = await storage._fetch_all('SELECT * FROM memory_capture_turns WHERE source_id IS NULL', ())
    assert len(pending) == 4
    assert await memory.store.sources() == []
    assert await storage.transition_run(run['id'], {RunStatus.RUNNING}, RunStatus.COMPLETED, {})
    assert len(await memory.store.sources()) == 5


def test_settings_redact_and_preserve_secrets():
    config = default_config()
    config['database_url'] = 'postgresql://user:password@localhost/agentsteam_memory'
    config['llm']['api_key'] = 'private-key'
    result = public_config(config)
    assert 'password' not in json.dumps(result)
    assert 'private-key' not in json.dumps(result)
    body = MemorySettings()
    assert merge_config(body, config)['llm']['api_key'] == 'private-key'


@pytest.mark.asyncio
async def test_routes_disabled_auth_and_no_identity_override(tmp_path):
    app = create_app(tmp_path / 'http.db')
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            settings = await client.get('/api/settings/memory')
            assert settings.status_code == 200 and settings.json()['enabled'] is False
            assert (await client.post('/api/memory/recall', json={'query':'hello'})).status_code == 503
            assert (await client.post('/internal/memory/recall', json={'query':'hello'})).status_code == 401
            assert (await client.post('/api/memory/retain', json={'request_id':str(uuid4()), 'content':'hello','member_id':'fake'})).status_code == 422
            invalid = await client.put('/api/settings/memory', json={'unexpected_secret':'do-not-echo'})
            assert invalid.status_code == 422 and 'do-not-echo' not in invalid.text


@pytest.mark.asyncio
async def test_embedding_binding_and_failed_retry(memory):
    source = await memory.retain(RetainInput(request_id=uuid4(), content='test'))
    with pytest.raises(RequestConflictError):
        await memory.save(MemorySettings(embedding_dimensions=512))
    await memory.tick()
    key = source['revisions'][0]['id']
    memory.engine.states[key] = 'failed'
    await memory.tick()
    assert (await memory.store.operations())[0]['status'] == 'failed'
    await memory.store.revise(source['id'], 1, 'new fact', False)
    with pytest.raises(RequestConflictError):
        await memory.retry(key)


@pytest.mark.asyncio
async def test_recall_failure_keeps_prompt_and_records_status(memory, repository):
    project = await memory.storage.create_project(project_input(repository))
    session = await memory.storage.create_session(SessionCreate(project_id=project['id'], harness='codex'))
    run, _ = await memory.storage.create_run(session['id'], MessageCreate(request_id=str(uuid4()), message='原始输入'))
    async def broken(query, member_id=None):
        raise ConnectionError('secret-provider-detail')
    engine = await memory._ready()
    engine.recall = broken
    assert await memory.augment(run) == '原始输入'
    row = await memory.storage._fetch_one('SELECT * FROM memory_contexts WHERE run_id=?', (run['id'],))
    assert row['status'] == 'unavailable' and 'secret-provider-detail' not in json.dumps(row)


@pytest.mark.asyncio
async def test_real_mcp_memory_roundtrip(tmp_path, repository):
    from app.services.orchestrator import CallerIdentity
    from test_http_runs import live_server
    from test_mcp_bridge import bridge_session
    app = create_app(tmp_path / 'mcp.db')
    async with app.router.lifespan_context(app), live_server(app) as client:
        storage = app.state.storage
        project = await storage.create_project(project_input(repository))
        session = await storage.create_session(SessionCreate(project_id=project['id'], harness='codex'))
        app.state.service.tokens['test-memory-token'] = CallerIdentity(session['id'], 'coordinator')
        app.state.memory.config['enabled'] = True
        app.state.memory.engine = Engine(app.state.memory.config)
        async with bridge_session(tmp_path, 'coordinator', str(client.base_url), 'test-memory-token') as mcp:
            result = await mcp.call_tool('memory_retain', {'request_id':str(uuid4()), 'content':'用户偏好简短报告'})
            assert not result.is_error
            source = result.structured_content
            assert source['session_id'] == session['id'] and source['member_id'] is None
            recalled = await mcp.call_tool('memory_recall', {'query':'报告偏好'})
            assert not recalled.is_error and recalled.structured_content['results']


@pytest.mark.asyncio
async def test_pending_source_survives_database_reopen(tmp_path):
    path = tmp_path / 'restart.db'
    storage = Storage(path)
    await storage.open()
    service = MemoryService(storage, Engine)
    service.config['enabled'] = True
    await service.store.save_config(service.config)
    source = await service.retain(RetainInput(request_id=uuid4(), content='重启后仍需沉淀'))
    await storage.close()
    reopened = Storage(path)
    await reopened.open()
    recovered = MemoryService(reopened, Engine)
    try:
        recovered.config = await recovered.store.config()
        await recovered.tick()
        loaded = await recovered.store.source(source['id'])
        assert loaded['revisions'][0]['id'] == source['revisions'][0]['id']
        assert loaded['revisions'][0]['status'] == 'accepted'
    finally:
        await recovered.close()
        await reopened.close()
