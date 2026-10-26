from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from app.memory.maintenance import replace_bank


@pytest.mark.asyncio
async def test_replacement_imports_reviewed_archive_then_commits_sources_without_reextracting():
    upstream = SimpleNamespace(delete_bank=AsyncMock(), import_bank_async=AsyncMock(return_value='restored'))
    sources = AsyncMock()
    assert await replace_bank(upstream, 'employee-a', b'reviewed', b'backup', None, sources) == 'restored'
    upstream.delete_bank.assert_awaited_once_with('employee-a', request_context=None)
    upstream.import_bank_async.assert_awaited_once_with(b'reviewed', None, target_bank_id='employee-a')
    sources.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_stage', ['import', 'sources'])
async def test_partial_failure_restores_only_the_employee_bank(failure_stage):
    upstream = SimpleNamespace(delete_bank=AsyncMock(), import_bank_async=AsyncMock())
    sources = AsyncMock()
    if failure_stage == 'import':
        upstream.import_bank_async.side_effect = [RuntimeError('interrupted import'), 'restored backup']
    else:
        sources.side_effect = RuntimeError('interrupted SQLite transaction')
    with pytest.raises(RuntimeError, match='interrupted'):
        await replace_bank(upstream, 'employee-a', b'reviewed', b'backup', None, sources)
    assert upstream.delete_bank.await_args_list == [call('employee-a', request_context=None)] * 2
    assert upstream.import_bank_async.await_args_list == [
        call(b'reviewed', None, target_bank_id='employee-a'), call(b'backup', None, target_bank_id='employee-a')]
    assert sources.await_count == (failure_stage == 'sources')
