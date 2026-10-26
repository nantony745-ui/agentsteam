from types import SimpleNamespace
import json

import httpx
import pytest

from app.main import create_app
from app.memory import config as memory_config
from app.memory.models import CloudModel


def mock_catalog(monkeypatch, handler):
    monkeypatch.setattr(memory_config, 'httpx', SimpleNamespace(
        AsyncClient=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs),
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize('component,provider', [('llm', 'openai-responses'), ('embedding', 'openai'), ('rerank', 'siliconflow')])
async def test_model_catalog_uses_saved_key_without_other_configuration(tmp_path, monkeypatch, component, provider):
    def upstream(request):
        assert request.method == 'GET' and str(request.url) == 'https://models.example/v1/models'
        assert request.headers['Authorization'] == 'Bearer private-key'
        return httpx.Response(200, json={'data': [{'id': 'model-b'}, {'id': 'model-a'}, {'id': 'model-b'}], 'secret': 'private-key'})

    mock_catalog(monkeypatch, upstream)
    app = create_app(tmp_path / 'models.db')
    async with app.router.lifespan_context(app):
        app.state.memory.config[component] = {'provider': provider, 'base_url': 'https://models.example/v1', 'model': '', 'api_key': 'private-key'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(f'/api/settings/memory/models/{component}', json={'provider': provider, 'base_url': 'https://models.example/v1'})
            assert response.status_code == 200
            assert response.json() == {'models': ['model-a', 'model-b']}
            assert 'private-key' not in response.text
            assert not app.state.memory.config['enabled']


@pytest.mark.asyncio
async def test_changed_endpoint_requires_its_own_key(tmp_path, monkeypatch):
    def upstream(request):
        pytest.fail('saved key must not be sent to a changed endpoint')

    mock_catalog(monkeypatch, upstream)
    app = create_app(tmp_path / 'models.db')
    async with app.router.lifespan_context(app):
        app.state.memory.config['llm'].update(base_url='https://saved.example/v1', api_key='saved-key')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/settings/memory/models/llm', json={'base_url': 'https://changed.example/v1'})
            assert response.status_code == 422
            assert 'API Key' in response.json()['detail'] and 'saved-key' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body,expected', [
    (401, {'error': 'private-key'}, 'API Key'),
    (404, {'error': 'private-key'}, '手动填写'),
    (200, {'data': []}, '手动填写'),
    (200, ['private-key'], '手动填写'),
])
async def test_model_catalog_failures_are_safe_and_actionable(tmp_path, monkeypatch, status, body, expected):
    mock_catalog(monkeypatch, lambda request: httpx.Response(status, json=body))
    app = create_app(tmp_path / 'models.db')
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/settings/memory/models/llm', json={
                'base_url': 'https://models.example/v1', 'api_key': 'private-key',
            })
            assert response.status_code in {422, 502}
            assert expected in response.json()['detail']
            assert 'private-key' not in response.text


@pytest.mark.asyncio
async def test_cohere_catalog_paginates_and_filters_rerank(monkeypatch):
    calls = []
    def upstream(request):
        calls.append(request.url)
        if len(calls) == 1:
            return httpx.Response(200, json={'models': [
                {'name': 'chat-model', 'endpoints': ['chat']}, {'name': 'rerank-a', 'endpoints': ['rerank']},
            ], 'next_page_token': 'page-2'})
        assert request.url.params['page_token'] == 'page-2'
        return httpx.Response(200, json={'models': [{'name': 'rerank-b', 'endpoints': ['rerank']}]})

    mock_catalog(monkeypatch, upstream)
    result = await memory_config.list_models(CloudModel(provider='cohere', base_url='https://models.example/v1', api_key='private-key'), 'rerank', {})
    assert result == ['rerank-a', 'rerank-b'] and len(calls) == 2


@pytest.mark.asyncio
async def test_model_detection_timeout(tmp_path, monkeypatch):
    def upstream(request):
        raise httpx.ReadTimeout('private-key', request=request)

    mock_catalog(monkeypatch, upstream)
    app = create_app(tmp_path / 'models.db')
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/settings/memory/models/llm', json={'base_url': 'https://models.example/v1', 'api_key': 'private-key'})
            assert response.status_code == 504 and '超时' in response.json()['detail']
            assert 'private-key' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('component,provider,endpoint', [
    ('embedding', 'openai', 'embeddings'), ('rerank', 'siliconflow', 'rerank'),
    ('llm', 'openai', 'chat/completions'),
])
async def test_component_probe_accepts_full_endpoint_independently(tmp_path, monkeypatch, component, provider, endpoint):
    def upstream(request):
        assert str(request.url) == f'https://models.example/v1/{endpoint}'
        body = json.loads(request.content)
        assert body['model'] == 'synthetic-model'
        if component == 'embedding':
            assert 'dimensions' not in body
        result = {'data': [{'embedding': [0.1] * 768}]} if component == 'embedding' else {'results': [{'index': 0}]} if component == 'rerank' else {'choices': [{'message': {'content': 'OK'}}]}
        return httpx.Response(200, json=result)
    mock_catalog(monkeypatch, upstream)
    app = create_app(tmp_path / 'probe.db')
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(f'/api/settings/memory/probe/{component}', json={'connection': {
                'provider': provider, 'base_url': f'https://models.example/v1/{endpoint}', 'model': 'synthetic-model', 'api_key': 'private-key',
            }})
            assert response.status_code == 200 and response.json()['ok']
            if component == 'embedding':
                assert response.json()['dimensions'] == 768
            assert 'private-key' not in response.text
            assert not app.state.memory.config['enabled']


@pytest.mark.asyncio
@pytest.mark.parametrize('native', [2048, 4096])
async def test_embedding_auto_dimension_negotiates_and_verifies_pgvector_compatibility(monkeypatch, native):
    calls = []
    def upstream(request):
        body = json.loads(request.content); calls.append(body)
        return httpx.Response(200, json={'data': [{'embedding': [0.1] * body.get('dimensions', native)}]})
    mock_catalog(monkeypatch, upstream)
    result = await memory_config.probe({'embedding': {'model': 'test-model', 'base_url': 'https://example/v1/embeddings', 'api_key': 'secret'}, 'embedding_dimensions': None}, 'embedding')
    assert result['native_dimensions'] == native and result['dimensions'] == 1024
    assert 'dimensions' not in calls[0] and calls[1]['dimensions'] == 1024
    assert str(native) in result['message'] and '2000' in result['message']


@pytest.mark.asyncio
async def test_embedding_rejects_ignored_dimension_request(monkeypatch):
    mock_catalog(monkeypatch, lambda request: httpx.Response(200, json={'data': [{'embedding': [0.1] * 4096}]}))
    model = {'model': 'test-model', 'base_url': 'https://example/v1', 'api_key': 'secret'}
    with pytest.raises(memory_config.ConnectionProbeError, match='未按请求'):
        await memory_config.probe({'embedding': model, 'embedding_dimensions': None}, 'embedding')
    with pytest.raises(memory_config.ConnectionProbeError, match='不符'):
        await memory_config.probe({'embedding': model, 'embedding_dimensions': 1024}, 'embedding')


@pytest.mark.asyncio
@pytest.mark.parametrize('status,expected', [(401, 'API Key'), (404, '地址'), (422, '模型'), (429, '额度'), (500, '服务')])
async def test_probe_failure_explains_status_without_leaking_credentials(tmp_path, monkeypatch, status, expected):
    mock_catalog(monkeypatch, lambda request: httpx.Response(status, json={'error': 'private-key'}))
    app = create_app(tmp_path / 'errors.db')
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/settings/memory/probe/rerank', json={'connection': {
                'provider': 'siliconflow', 'base_url': 'https://example/v1/rerank', 'model': 'test', 'api_key': 'private-key',
            }})
            assert response.status_code == 502 and expected in response.json()['detail']
            assert str(status) in response.json()['detail'] and 'private-key' not in response.text


@pytest.mark.asyncio
async def test_full_endpoint_catalog_and_probe_reuse_only_matching_saved_credentials(tmp_path, monkeypatch):
    def upstream(request):
        assert request.headers['Authorization'] == 'Bearer saved-key'
        if request.method == 'GET':
            assert request.url.path == '/v1/models'
            return httpx.Response(200, json={'data': [{'id': 'test'}]})
        assert request.url.path == '/v1/embeddings'
        return httpx.Response(200, json={'data': [{'embedding': [0.1] * 512}]})
    mock_catalog(monkeypatch, upstream)
    app = create_app(tmp_path / 'credentials.db')
    async with app.router.lifespan_context(app):
        app.state.memory.config['embedding'].update(base_url='https://saved.example/v1', api_key='saved-key')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            model = {'base_url': 'https://saved.example/v1/embeddings', 'model': 'test'}
            assert (await client.post('/api/settings/memory/models/embedding', json=model)).status_code == 200
            assert (await client.post('/api/settings/memory/probe/embedding', json={'connection': model})).status_code == 200
            model['base_url'] = 'https://changed.example/v1'
            rejected = await client.post('/api/settings/memory/probe/embedding', json={'connection': model})
            assert rejected.status_code == 422 and 'API Key' in rejected.json()['detail']


@pytest.mark.asyncio
async def test_save_resolves_automatic_dimensions_and_preserves_database_binding(tmp_path, monkeypatch):
    from app.memory.models import MemorySettings
    from app.db.storage import RequestConflictError
    from uuid import uuid4
    calls = []
    def upstream(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'data': [{'embedding': [0.1] * 768}]})
    mock_catalog(monkeypatch, upstream)
    app = create_app(tmp_path / 'save.db')
    async with app.router.lifespan_context(app):
        service = app.state.memory
        body = MemorySettings(embedding_dimensions=None, embedding=CloudModel(base_url='https://example/v1/embeddings', model='test', api_key='private-key'))
        saved = await service.save(body)
        assert saved['embedding_dimensions'] == 768 and len(calls) == 1
        assert saved['embedding']['base_url'] == 'https://example/v1'
        assert 'private-key' not in json.dumps(saved)
        await service.store.retain(str(uuid4()), 'synthetic binding evidence', None, None)
        with pytest.raises(RequestConflictError):
            await service.save(body.model_copy(update={'embedding_dimensions': 512}))


@pytest.mark.asyncio
async def test_legacy_full_settings_probe_remains_compatible_and_checks_credentials(tmp_path, monkeypatch):
    def upstream(request):
        assert request.headers['Authorization'] == 'Bearer saved-key'
        assert request.url.path == '/v1/embeddings'
        assert json.loads(request.content)['dimensions'] == 512
        return httpx.Response(200, json={'data': [{'embedding': [0.1] * 512}]})
    mock_catalog(monkeypatch, upstream)
    app = create_app(tmp_path / 'legacy.db')
    async with app.router.lifespan_context(app):
        app.state.memory.config['embedding'].update(base_url='https://saved.example/v1', api_key='saved-key')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            body = {'embedding': {'base_url': 'https://saved.example/v1/embeddings', 'model': 'test'}, 'embedding_dimensions': 512}
            response = await client.post('/api/settings/memory/probe/embedding', json=body)
            assert response.status_code == 200 and response.json()['dimensions'] == 512
            body['embedding']['base_url'] = 'https://changed.example/v1'
            response = await client.post('/api/settings/memory/probe/embedding', json=body)
            assert response.status_code == 422 and 'API Key' in response.json()['detail']
