import asyncio

import pytest

from app.memory.models import MemorySettings, MemoryUnavailable, RetainInput
from test_memory import memory
from uuid import uuid4


@pytest.mark.asyncio
async def test_slow_reflect_does_not_block_recall_revisions_or_outbox(memory):
    source = await memory.retain(RetainInput(request_id=uuid4(), content='原始来源'))
    engine = await memory._ready()
    entered, release = asyncio.Event(), asyncio.Event()
    async def reflect(query, member_id):
        entered.set()
        await release.wait()
        return {'text': query}
    engine.reflect = reflect
    pending = asyncio.create_task(memory.query('reflect', 'slow'))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert (await asyncio.wait_for(memory.query('recall', 'fast'), 1))['results']
        await asyncio.wait_for(memory.revise(source['id'], 1, '新版本', False), 1)
        await asyncio.wait_for(memory.tick(), 1)
        assert (await memory.store.source(source['id']))['revisions'][0]['status'] == 'accepted'
        assert not pending.done()
    finally:
        release.set()
        await pending
    assert memory.active_operations == 0 and memory.idle.is_set()


@pytest.mark.asyncio
async def test_engine_concurrency_is_bounded_and_cancel_releases_slot(memory):
    engine = await memory._ready()
    entered, release = asyncio.Queue(), asyncio.Event()
    async def reflect(query, member_id):
        entered.put_nowait(query)
        await release.wait()
        return query
    engine.reflect = reflect
    tasks = [asyncio.create_task(memory.query('reflect', str(index))) for index in range(3)]
    try:
        assert await asyncio.wait_for(entered.get(), 1) == '0'
        assert await asyncio.wait_for(entered.get(), 1) == '1'
        assert entered.empty() and memory.active_operations == 2
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert await asyncio.wait_for(entered.get(), 1) == '2'
        assert memory.active_operations == 2
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert memory.active_operations == 0 and memory.idle.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['save', 'close'])
async def test_reconfigure_and_close_wait_for_inflight_engine_users(memory, action):
    engine = await memory._ready()
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def reflect(query, member_id):
        entered.set()
        await release.wait()
        assert not closed.is_set()
        return query
    async def close():
        assert memory.active_operations == 0
        closed.set()
    engine.reflect, engine.close = reflect, close
    query = asyncio.create_task(memory.query('reflect', 'inflight'))
    await asyncio.wait_for(entered.wait(), 1)
    lifecycle = asyncio.create_task(memory.close() if action == 'close' else memory.save(MemorySettings()))
    try:
        await asyncio.sleep(0)
        assert not lifecycle.done() and not closed.is_set()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(query, lifecycle), 2)
    assert closed.is_set() and memory.engine is None
    with pytest.raises(MemoryUnavailable):
        await memory.query('recall', 'after')
