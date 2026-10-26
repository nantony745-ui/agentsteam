import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.db.storage import Storage
from app.domain.employees import EmployeeInput, EmployeeUpdate
from app.domain.models import MessageCreate, RunStatus, SessionCreate
from app.main import create_app
from app.memory.engine.hindsight import HindsightEngine, bank_id
from app.memory.models import MemorySettings, RetainInput
from app.memory.service import MemoryService, MemoryUnavailable
from app.memory.storage import capture_completed_run
from app.services.employees import Employees
from app.services.orchestrator import CallerIdentity
from test_memory import Engine, complete_turn
from test_storage import project_input, repository


class BankEngine(Engine):
    async def ensure_bank(self, member_id):
        self.calls.append(('bank', member_id))

    async def recall(self, query, member_id=None):
        return {'results': [{'text': str(member_id), 'document_id': 'source'}]}

    async def reflect(self, query, member_id=None):
        return {'text': str(member_id)}

    async def facts(self, kind, offset, member_id=None):
        return {'items': [{'text': str(member_id), 'created_at': '2026-10-23T00:00:00Z'}]}


@pytest_asyncio.fixture
async def personal(tmp_path, repository):
    storage = Storage(tmp_path / 'personal.db')
    await storage.open()
    memory = MemoryService(storage, BankEngine)
    memory.employees = Employees(tmp_path / 'employees')
    storage.memory_owner = memory.owner
    memory.config['enabled'] = True
    await memory.store.save_config(memory.config)
    project = await storage.create_project(project_input(repository))
    employees, sessions = [], []
    for name in ('甲', '乙'):
        employee = memory.employees.create(EmployeeInput(name=name, harness='codex'))
        employees.append(employee['employee_id'])
        member_sessions = []
        for _ in range(2):
            session = await storage.create_session(SessionCreate(project_id=project['id'], harness='codex'))
            memory.employees.prepare(employee['employee_id'], session['id'], {'config_values': {}}, repository)
            member_sessions.append(session['id'])
        sessions.append(member_sessions)
    yield memory, employees, sessions
    await memory.close()
    await storage.close()


@pytest.mark.asyncio
async def test_default_off_cross_session_three_turns_and_restart(personal):
    memory, (a, b), (sa, sb) = personal
    assert not (await memory.employee_settings(a))['enabled']
    await complete_turn(memory.storage, sa[0], '关闭期间不保存')
    with pytest.raises(MemoryUnavailable):
        await memory.retain(RetainInput(request_id=uuid4(), content='不能保存'), sa[0])
    for member in (a, b):
        await memory.store.set_enabled(member, True)
    await asyncio.gather(complete_turn(memory.storage, sa[0], '甲一'), complete_turn(memory.storage, sa[1], '甲二'))
    await complete_turn(memory.storage, sb[0], '乙一')
    assert await memory.store.sources(a) == []
    await memory.storage.close()
    await memory.storage.open()
    last = await complete_turn(memory.storage, sa[1], '甲三')
    source = await memory.store.source(f"run:{last['id']}", a)
    assert source['member_id'] == a
    meta = source['revisions'][0]['metadata']
    assert meta['run_id'] == last['id']
    sources = await memory.store.sources(a)
    assert {row['session_id'] for row in sources} == set(sa)
    assert all(row['id'] == f"run:{row['run_id']}" for row in sources)
    assert '关闭期间' not in source['revisions'][0]['content']
    assert '乙一' not in source['revisions'][0]['content']
    async with memory.storage.transaction() as db:
        await capture_completed_run(db, last['id'])
    assert len(await memory.store.sources(a)) == 3
    assert await memory.store.sources(b) == await memory.store.sources() == []


@pytest.mark.asyncio
async def test_toggle_during_run_and_paused_outbox_with_manual_management(personal):
    memory, (a, _), (sa, _) = personal
    storage = memory.storage
    run, _ = await storage.create_run(sa[0], MessageCreate(request_id=str(uuid4()), message='开始时关闭'))
    await storage.transition_run(run['id'], {RunStatus.QUEUED}, RunStatus.RUNNING, {})
    await memory.store.set_enabled(a, True)
    await storage.transition_run(run['id'], {RunStatus.RUNNING}, RunStatus.COMPLETED, {})
    assert await storage._fetch_all('SELECT * FROM memory_capture_turns', ()) == []
    memory.config['auto_retain_turns'] = 1
    await memory.store.save_config(memory.config)
    await complete_turn(storage, sa[0], '自动来源')
    queued = await memory.retain(RetainInput(request_id=uuid4(), content='主动来源'), sa[0])
    await memory.store.set_enabled(a, False)
    manual = await memory.retain(RetainInput(request_id=uuid4(), content='用户管理'), member_id=a)
    await memory.tick()
    assert (await memory.store.source(queued['id'], a))['revisions'][0]['status'] == 'queued'
    assert (await memory.store.source(manual['id'], a))['revisions'][0]['status'] == 'accepted'
    memory.engine.states[manual['revisions'][0]['id']] = 'completed'
    await memory.tick()
    assert (await memory.store.source(manual['id'], a))['revisions'][0]['status'] == 'written'
    await memory.store.set_enabled(a, True)
    await memory.tick()
    assert (await memory.store.source(queued['id'], a))['revisions'][0]['status'] == 'accepted'


@pytest.mark.asyncio
async def test_manual_delete_can_finish_paused_source_without_resurrection(personal):
    memory, (a, _), (sa, _) = personal
    await memory.store.set_enabled(a, True)
    source = await memory.retain(RetainInput(request_id=uuid4(), content='待提交的队员记忆'), sa[0])
    await memory.store.set_enabled(a, False)
    await memory.store.revise(source['id'], 1, '', True, a)
    await memory.tick()
    key = source['revisions'][0]['id']
    memory.engine.states[key] = 'completed'
    await memory.tick()
    await memory.tick()
    assert (await memory.store.source(source['id'], a))['revisions'][-1]['status'] == 'deleted'
    assert ('delete', source['id']) in memory.engine.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('lose_response', [False, True])
async def test_retry_and_delete_are_serial_even_when_upstream_response_is_lost(personal, lose_response):
    memory, (a, _), _ = personal
    source = await memory.retain(RetainInput(request_id=uuid4(), content='重试来源'), member_id=a)
    key = source['revisions'][0]['id']
    await memory.store.update(key, 'failed')
    engine = await memory._ready()
    entered, release = asyncio.Event(), asyncio.Event()
    original = engine.submit
    async def delayed(operation):
        result = await original(operation)
        entered.set()
        await release.wait()
        if lose_response:
            raise ConnectionError('上游已接收，但响应丢失')
        return result
    engine.submit = delayed
    retry = asyncio.create_task(memory.retry(key, a))
    await entered.wait()
    assert (await memory.store.source(source['id'], a))['revisions'][0]['status'] == 'queued'
    delete = asyncio.create_task(memory.revise(source['id'], 1, '', True, a))
    await asyncio.sleep(0)
    assert not delete.done()
    release.set()
    if lose_response:
        with pytest.raises(ConnectionError):
            await retry
    else:
        await retry
    await delete
    engine.submit = original
    await memory.tick()
    assert ('delete', source['id']) not in engine.calls
    engine.states[key] = 'completed'
    await memory.tick()
    await memory.tick()
    assert ('delete', source['id']) in engine.calls
    assert (await memory.store.source(source['id'], a))['revisions'][-1]['status'] == 'deleted'


@pytest.mark.asyncio
async def test_disable_before_completion_and_unsuccessful_runs_are_not_captured(personal):
    memory, (a, _), (sa, _) = personal
    await memory.store.set_enabled(a, True)
    for status in (RunStatus.COMPLETED, RunStatus.CANCELED, RunStatus.FAILED, RunStatus.INTERRUPTED):
        run, _ = await memory.storage.create_run(sa[0], MessageCreate(request_id=str(uuid4()), message='不计入'))
        await memory.storage.transition_run(run['id'], {RunStatus.QUEUED}, RunStatus.RUNNING, {})
        if status == RunStatus.COMPLETED:
            await memory.store.set_enabled(a, False)
        await memory.storage.transition_run(run['id'], {RunStatus.RUNNING}, status, {})
        await memory.store.set_enabled(a, True)
    assert await memory.storage._fetch_all('SELECT * FROM memory_capture_turns', ()) == []


@pytest.mark.asyncio
async def test_threshold_change_flushes_on_next_completion_and_global_default(personal):
    memory, (a, _), (sa, _) = personal
    assert MemorySettings().auto_retain_turns == 3
    for value in (0, -1, True, 1.5, '3'):
        with pytest.raises(ValidationError):
            MemorySettings(auto_retain_turns=value)
    await memory.store.set_enabled(a, True)
    memory.config['auto_retain_turns'] = 5
    await memory.store.save_config(memory.config)
    for index in range(4):
        await complete_turn(memory.storage, sa[index % 2], str(index))
    memory.config['auto_retain_turns'] = 2
    await memory.store.save_config(memory.config)
    assert await memory.store.sources(a) == []
    await complete_turn(memory.storage, sa[0], '触发')
    assert len(await memory.store.sources(a)) == 4
    assert len(await memory.storage._fetch_all('SELECT * FROM memory_capture_turns WHERE source_id IS NULL', ())) == 1


@pytest.mark.asyncio
async def test_inflight_recall_is_discarded_after_disable(personal):
    memory, (a, _), (sa, _) = personal
    await memory.store.set_enabled(a, True)
    entered, release = asyncio.Event(), asyncio.Event()
    engine = await memory._ready()
    async def recall(query, member_id):
        assert member_id == a
        entered.set()
        await release.wait()
        return {'results': [{'text': '不能注入'}]}
    engine.recall = recall
    run, _ = await memory.storage.create_run(sa[0], MessageCreate(request_id=str(uuid4()), message='原输入'))
    future = asyncio.create_task(memory.augment(run))
    await entered.wait()
    await memory.store.set_enabled(a, False)
    release.set()
    assert await future == '原输入'
    context = await memory.storage._fetch_one('SELECT * FROM memory_contexts WHERE run_id=?', (run['id'],))
    assert context['member_id'] == a and context['status'] == 'disabled' and context['context'] == ''


@pytest.mark.asyncio
async def test_waiting_agent_query_does_not_reach_engine_after_disable(personal):
    memory, (a, _), (sa, _) = personal
    await memory.store.set_enabled(a, True)
    await memory.lock.acquire()
    try:
        query = asyncio.create_task(memory.session_query('recall', '排队查询', sa[0]))
        await asyncio.sleep(0)
        await memory.store.set_enabled(a, False)
    finally:
        memory.lock.release()
    with pytest.raises(MemoryUnavailable):
        await query
    assert memory.engine is None


@pytest.mark.asyncio
async def test_identity_survives_profile_changes_and_harness(personal):
    memory, (a, _), (sa, _) = personal
    await memory.store.set_enabled(a, True)
    memory.employees.update(a, EmployeeUpdate(name='改名', harness='deepseek', version=1))
    for session in sa:
        assert (await memory.session_query('recall', '偏好', session))['results'][0]['text'] == a


@pytest.mark.asyncio
async def test_http_isolation_management_and_deleted_employee(tmp_path, repository):
    app = create_app(tmp_path / 'api.db')
    async with app.router.lifespan_context(app):
        memory = app.state.memory
        memory.config['enabled'] = True
        memory.engine = BankEngine(memory.config)
        await memory.store.save_config(memory.config)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            a, b = [(await client.post('/api/employees', json={'name': name, 'harness': 'codex'})).json()['employee_id'] for name in ('甲', '乙')]
            pa, pb = [f'/api/employees/{member}/memory' for member in (a, b)]
            body = {'request_id': str(uuid4()), 'content': '私有内容'}
            own = (await client.post(pa + '/retain', json=body)).json()
            other = (await client.post(pb + '/retain', json=body)).json()
            shared = (await client.post('/api/memory/retain', json=body)).json()
            assert len({own['id'], other['id'], shared['id']}) == 3
            for prefix, expected in ((pa, a), (pb, b), ('/api/memory', None)):
                sources = (await client.get(prefix + '/sources')).json()
                assert len(sources) == 1 and sources[0]['member_id'] == expected
            for prefix in (pb, '/api/memory'):
                path = prefix + '/sources/' + own['id']
                assert (await client.get(path)).status_code == 404
                assert (await client.put(path, json={'version': 1, 'content': '越界'})).status_code == 404
                assert (await client.request('DELETE', path, json={'version': 1})).status_code == 404
                assert (await client.post(prefix + '/operations/' + own['revisions'][0]['id'] + '/retry')).status_code == 404
            assert (await client.get('/api/memory/sources', params={'employee_id': a})).json()[0]['member_id'] is None
            assert (await client.get(pa + '/facts')).json()['items'][0]['text'] == a
            assert (await client.get(pa + '/facts', params={'kind': 'all'})).json()['items'][0]['text'] == a
            assert (await client.post(pa + '/reflect', json={'query': '总结'})).json()['text'] == a
            project = await app.state.storage.create_project(project_input(repository))
            session = await app.state.storage.create_session(SessionCreate(project_id=project['id'], harness='codex'))
            app.state.employees.prepare(a, session['id'], {'config_values': {}}, repository)
            app.state.service.tokens['a-token'] = CallerIdentity(session['id'], 'coordinator')
            headers = {'Authorization': 'Bearer a-token'}
            assert (await client.post('/internal/memory/recall', json={'query': 'test'}, headers=headers)).status_code == 503
            assert (await client.put(pa, json={'enabled': True})).status_code == 200
            result = await client.post('/internal/memory/recall', json={'query': 'test'}, headers=headers)
            assert result.json()['results'][0]['text'] == a
            assert (await client.post('/internal/memory/retain', json={**body, 'member_id': b}, headers=headers)).status_code == 422
            assert (await client.put(pa + '/sources/' + own['id'], json={'version': 2, 'content': '冲突'})).status_code == 409
            assert (await client.delete(f'/api/employees/{a}?version=1')).status_code == 200
            assert not (await client.get(pa)).json()['enabled']
            assert (await client.post('/internal/memory/recall', json={'query': 'test'}, headers=headers)).status_code == 503
            assert (await client.get(pa + '/sources')).status_code == 200
            assert (await client.put(pa, json={'enabled': True})).status_code == 409


@pytest.mark.asyncio
async def test_adapter_passes_bank_to_every_upstream_operation():
    calls = []
    async def record(*args, **kwargs):
        calls.append((args, kwargs))
        return {}
    adapter = HindsightEngine({})
    adapter.context = None
    adapter.engine = SimpleNamespace(**{name: record for name in (
        'ensure_bank_profile', 'update_bank_config', 'submit_async_retain', 'get_operation_status', 'retry_operation',
        'delete_document', 'get_bank_freshness', 'recall_async', 'reflect_async', 'list_memory_units')})
    async def configured(*args, **kwargs):
        return SimpleNamespace(config={'retain_chunk_size': 3000, 'retain_structured_chunk_size': None})
    adapter.engine.get_bank_config = configured
    for member in (None, str(uuid4()), str(uuid4())):
        await adapter.ensure_bank(member)
        await adapter.submit({'member_id': member, 'metadata': '{}', 'id': 'operation', 'content': 'text', 'source_id': 'source', 'version': 1})
        await adapter.operation('operation', member)
        await adapter.retry('operation', member)
        await adapter.delete('source', member)
        await adapter.freshness(member)
        await adapter.recall('query', member)
        await adapter.reflect('query', member)
        await adapter.facts('world', 0, member)
        batch = calls[-9:]
        assert all(args[0] == bank_id(member) for index, (args, _) in enumerate(batch) if index != 4)
        assert batch[4][0] == ('source', bank_id(member))
        await adapter.facts('all', 50, member)
        assert calls[-1][0] == (bank_id(member),)
        assert calls[-1][1]['fact_type'] is None
        assert calls[-1][1]['offset'] == 50


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['coordinator', 'worker'])
async def test_real_mcp_personal_tools_follow_live_switch(tmp_path, repository, role):
    from test_http_runs import live_server
    from test_mcp_bridge import bridge_session
    app = create_app(tmp_path / 'mcp.db')
    async with app.router.lifespan_context(app), live_server(app) as http:
        memory, storage, employees = app.state.memory, app.state.storage, app.state.employees
        memory.config['enabled'] = True
        memory.engine = BankEngine(memory.config)
        await memory.store.save_config(memory.config)
        project = await storage.create_project(project_input(repository))
        parent = await storage.create_session(SessionCreate(project_id=project['id'], harness='codex'))
        session = await storage.create_session(SessionCreate(project_id=project['id'], harness='codex'))
        if role == 'worker':
            async with storage.transaction() as db:
                await db.execute('UPDATE sessions SET parent_id=? WHERE id=?', (parent['id'], session['id']))
        employee = employees.create(EmployeeInput(name='工具队员', harness='codex'))
        member = employee['employee_id']
        employees.prepare(member, session['id'], {'config_values': {}}, repository)
        app.state.service.tokens['personal-token'] = CallerIdentity(session['id'], role)
        async with bridge_session(tmp_path, role, str(http.base_url), 'personal-token') as mcp:
            tools = {tool.name for tool in (await mcp.list_tools()).tools}
            assert {'memory_retain', 'memory_recall', 'memory_reflect'} <= tools
            assert (await mcp.call_tool('memory_recall', {'query': 'test'})).is_error
            await memory.store.set_enabled(member, True)
            saved = await mcp.call_tool('memory_retain', {'request_id': str(uuid4()), 'content': '本人事实'})
            assert not saved.is_error and saved.structured_content['member_id'] == member
            for name in ('recall', 'reflect'):
                result = await mcp.call_tool('memory_' + name, {'query': 'test'})
                assert not result.is_error and member in json.dumps(result.structured_content)
            assert await memory.store.sources() == []
            await memory.store.set_enabled(member, False)
            assert (await mcp.call_tool('memory_recall', {'query': 'test'})).is_error
            assert (await mcp.call_tool('memory_retain', {'request_id': str(uuid4()), 'content': '已关闭'})).is_error


@pytest.mark.asyncio
async def test_legacy_schema_and_buffers_stay_shared(tmp_path, repository):
    import sqlite3
    from pathlib import Path
    path = tmp_path / 'legacy.db'
    schema = Path('app/memory/schema.sql').read_text(encoding='utf-8').split('CREATE TABLE IF NOT EXISTS employee_memory_settings')[0]
    with sqlite3.connect(path) as db:
        db.executescript(schema)
        db.execute('INSERT INTO memory_capture_turns VALUES (?,?,?,?,NULL)', ('legacy', 'legacy-session', '旧缓冲', '{}'))
        db.execute('INSERT INTO memory_sources VALUES (?,NULL,?,?,1,0,?)', ('legacy-source', 'legacy-session', 'legacy', '2026-10-22'))
        db.execute('INSERT INTO memory_contexts VALUES (?,?,?,?)', ('legacy', 'loaded', '旧快照', '2026-10-22'))
        db.execute('INSERT INTO memory_settings VALUES (1,?)', (json.dumps({'enabled': False}),))
    storage = Storage(path)
    await storage.open()
    service = MemoryService(storage)
    try:
        assert (await service.store.config())['auto_retain_turns'] == 3
        for table in ('memory_sources', 'memory_capture_turns', 'memory_contexts'):
            assert (await storage._fetch_one(f'SELECT member_id FROM {table}', ()))['member_id'] is None
        await storage.close()
        await storage.open()
        assert (await storage._fetch_one('SELECT content FROM memory_capture_turns', ()))['content'] == '旧缓冲'
    finally:
        await storage.close()
