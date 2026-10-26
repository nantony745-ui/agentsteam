import json

import pytest

from app.db.storage import Storage
from app.memory import config as memory_config
from app.memory.service import MemoryService


@pytest.mark.asyncio
@pytest.mark.parametrize('saved', [False, True])
async def test_local_database_bootstrap_preserves_saved_settings(tmp_path, monkeypatch, saved):
    local_url = 'postgresql://agentsteam_memory:local-password@127.0.0.1:15432/agentsteam_memory'
    local_file = tmp_path / 'memory-postgres.json'
    local_file.write_text(json.dumps({'database_url': local_url}), encoding='utf-8')
    monkeypatch.setattr(memory_config, 'LOCAL_DATABASE_CONFIG', local_file)
    storage = Storage(tmp_path / 'app.db')
    await storage.open()
    service = MemoryService(storage)
    external_url = 'postgresql://external:saved-password@localhost:5432/agentsteam_memory'
    if saved:
        await service.store.save_config({**memory_config.default_config(), 'database_url': external_url})
    try:
        await service.start()
        assert service.config['database_url'] == (external_url if saved else local_url)
        public = service.settings()
        assert public['database_configured'] is True
        assert public['enabled'] is False
        assert public['database_url'] == ''
        assert 'password' not in json.dumps(public)
    finally:
        await service.close()
        await storage.close()


def test_without_local_database_remains_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_config, 'LOCAL_DATABASE_CONFIG', tmp_path / 'not-installed.json')
    assert not memory_config.default_config()['database_url']
