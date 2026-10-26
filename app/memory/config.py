"""服务器端配置、凭据掩码与分项连接验证。"""

import json
from pathlib import Path

import httpx

from app.memory.models import CloudModel, MemorySettings, normalize_base_url
from app.memory.engine.hindsight import probe_database, validate_database


LOCAL_DATABASE_CONFIG = Path(__file__).resolve().parents[2] / '.runtime/memory-postgres.json'


def unpack(model):
    data = model.model_dump()
    data["database_url"] = model.database_url.get_secret_value() if model.database_url is not None else None
    for name in ("llm", "embedding", "rerank"):
        key = getattr(model, name).api_key
        data[name]["api_key"] = key.get_secret_value() if key is not None else None
    return data


def public_config(config):
    result = {**config, "database_url": "", "database_configured": bool(config.get("database_url"))}
    for name in ("llm", "embedding", "rerank"):
        result[name] = {**config[name], "api_key": "", "key_configured": bool(config[name].get("api_key"))}
    return result


def merge_config(body, current):
    value = unpack(body)
    if value["database_url"] is None:
        value["database_url"] = current.get("database_url", "")
    for name in ("llm", "embedding", "rerank"):
        if value[name]["api_key"] is None:
            value[name]["api_key"] = current[name].get("api_key", "")
    if value["database_url"]:
        validate_database(value["database_url"])
    if value["llm"]["provider"] not in {"openai", "openai-responses"} or value["embedding"]["provider"] != "openai" or value["rerank"]["provider"] not in {"cohere", "siliconflow"}:
        raise ValueError("不支持该提供商组合")
    if value["enabled"] and (not value["database_url"] or any(
        not value[name][field] for name in ("llm", "embedding", "rerank") for field in ("base_url", "model", "api_key")
    )):
        raise ValueError("启用前请完整配置数据库和三类云端模型")
    return value


def default_config():
    config = unpack(MemorySettings())
    if LOCAL_DATABASE_CONFIG.is_file():
        config['database_url'] = json.loads(LOCAL_DATABASE_CONFIG.read_text(encoding='utf-8'))['database_url']
    return config


class ModelDiscoveryError(ValueError):
    pass


def resolve_model(body: CloudModel, component: str, current: dict) -> dict:
    providers = {'llm': {'openai', 'openai-responses'}, 'embedding': {'openai'}, 'rerank': {'cohere', 'siliconflow'}}
    if body.provider not in providers[component]:
        raise ModelDiscoveryError('提供商与模型用途不匹配')
    key = body.api_key.get_secret_value() if body.api_key is not None else None
    if key is None:
        if body.base_url != normalize_base_url(current.get('base_url', '')) or body.provider != current.get('provider'):
            raise ModelDiscoveryError('地址或提供商已更改，请填写对应的 API Key 后检测')
        key = current.get('api_key', '')
    if not body.base_url or not key:
        raise ModelDiscoveryError('请先填写 API 地址和 API Key')
    return {**body.model_dump(exclude={'api_key'}), 'api_key': key}


async def list_models(body: CloudModel, component: str, current: dict) -> list[str]:
    model = resolve_model(body, component, current)
    ids, params = set(), {}
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        while True:
            response = await client.get(model['base_url'] + '/models', headers={'Authorization': 'Bearer ' + model['api_key']}, params=params)
            response.raise_for_status()
            data = response.json()
            items = data.get('data', data.get('models', []))
            for item in items:
                if body.provider == 'cohere' and item.get('endpoints') and 'rerank' not in item['endpoints']:
                    continue
                name = item.get('id') or item.get('name')
                if name:
                    ids.add(name)
            token = data.get('next_page_token') if body.provider == 'cohere' else None
            if not token:
                break
            params = {'page_token': token}
    if not ids:
        raise ModelDiscoveryError('服务未返回可选模型，可手动填写模型名称')
    return sorted(ids)


class ConnectionProbeError(ValueError):
    pass


async def probe_embedding(client, model, dimensions):
    payload = {'model': model['model'], 'input': ['memory connection test']}
    if dimensions is not None:
        payload['dimensions'] = dimensions
    async def request_vector():
        response = await client.post(normalize_base_url(model['base_url']) + '/embeddings',
                                     headers={'Authorization': 'Bearer ' + model['api_key']}, json=payload)
        response.raise_for_status()
        vector = response.json()['data'][0]['embedding']
        if not isinstance(vector, list) or not vector or any(not isinstance(v, (int, float)) for v in vector):
            raise ConnectionProbeError('Embedding 未返回有效的数值向量')
        return len(vector)
    actual = await request_vector()
    native = actual if dimensions is None else None
    message = f'连接成功，模型返回 {actual} 维向量'
    if dimensions is None and actual > 2000:
        payload['dimensions'] = 1024
        actual = await request_vector()
        if actual != 1024:
            raise ConnectionProbeError('模型默认维度超过当前索引上限 2000，且未按请求返回 1024 维；请手动设置模型支持的兼容维度')
        message = f'连接成功，模型默认 {native} 维；当前索引上限 2000，已验证并采用模型支持的 1024 维输出'
    if dimensions is not None and actual != dimensions:
        raise ConnectionProbeError(f'模型返回 {actual} 维，与请求的 {dimensions} 维不符')
    return {'ok': True, 'dimensions': actual, 'native_dimensions': native, 'message': message}


async def probe(config, component):
    if component == "database":
        return await probe_database(config["database_url"])
    model = config[component]
    if not model['model']:
        raise ConnectionProbeError('请先选择或手动填写模型名称')
    path, payload = {
        "llm": ("/chat/completions", {"model": model["model"], "messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 32}),
        "embedding": ("/embeddings", {}),
        "rerank": ("/rerank", {"model": model["model"], "query": "memory", "documents": ["memory storage", "weather"]}),
    }[component]
    if component == "llm" and model["provider"] == "openai-responses":
        path, payload = "/responses", {"model": model["model"], "input": "Reply OK", "max_output_tokens": 128}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        if component == 'embedding':
            return await probe_embedding(client, model, config.get('embedding_dimensions'))
        response = await client.post(normalize_base_url(model["base_url"]) + path, headers={"Authorization": "Bearer " + (model["api_key"] or "")}, json=payload)
        response.raise_for_status()
        data = response.json()
    if component == "llm":
        if not data.get("output" if model["provider"] == "openai-responses" else "choices"):
            raise ConnectionProbeError("生成服务返回空结果")
    elif not data["results"]:
        raise ConnectionProbeError("重排服务返回空结果")
    return {"ok": True, 'message': '连接成功，已收到模型的有效响应'}
