import json

import pytest

from app.memory.ingestion import capture_content, source_messages, structured_content
from app.memory.prompts import bank_policy, submission_context


@pytest.mark.parametrize('task_id,speaker', [(None, '用户'), ('delegated-task', '上级委派')])
def test_capture_distinguishes_input_feedback_report_and_quoted_material(task_id, speaker):
    metadata = {'origin': 'completed_run', 'run_id': 'run-a', 'task_id': task_id, 'verification': 'unverified'}
    prompt = '用户引用：“我有制造业经验”。这只是待核实的简历材料。'
    content, meta = capture_content(prompt, ['只是转述'], '我自称已验收通过', metadata)
    messages = source_messages(content, meta)
    assert [m['speaker'] for _, m in messages] == [speaker, speaker, '员工报告']
    assert [m['content'] for _, m in messages] == [prompt, '只是转述', '我自称已验收通过']
    assert content.startswith(speaker + '输入：')
    assert '未经独立核验' in content and all(t['verification'] == 'unverified' for t, _ in messages)
    edited = source_messages('编辑者换成了另一份材料', meta)
    assert len(edited) == 1 and '修订者' in edited[0][1]['speaker']
    assert edited[0][1]['content'] == '编辑者换成了另一份材料'


@pytest.mark.parametrize('chunk_size,structured_size', [(1300, None), (3000, 1000), (1100, 4000)])
def test_native_chunker_keeps_every_fragment_identifiable_and_lossless(chunk_size, structured_size):
    from hindsight_api.engine.retain.fact_extraction import chunk_text

    text = ('中文“引号”与 "json"、反斜线\\，换行\n以及表情😀。\n\n' * 800) + '\n结尾  '
    content, meta = capture_content(text, ['补充材料'], text[::-1], {'run_id': 'correct-run', 'task_id': 'correct-task'})
    item = {'member_id': 'employee', 'source_id': 'run:correct-run', 'version': 2, 'content': content}
    encoded = structured_content(item, meta, {'retain_chunk_size': chunk_size, 'retain_structured_chunk_size': structured_size})
    chunks = chunk_text(encoded, chunk_size, structured_chunk_size=structured_size)
    records = []
    for chunk in chunks:
        assert len(chunk) <= chunk_size
        assert chunk_text(chunk, chunk_size, structured_chunk_size=structured_size) == [chunk]
        for record in json.loads(chunk):
            assert record['run_id'] == 'correct-run' and record['task_id'] == 'correct-task'
            assert record['source_id'] == 'run:correct-run' and record['employee_id'] == 'employee'
            assert record['verification'] == 'unverified'
            records.append(record)
    expected = [text, '补充材料', text[::-1]]
    for index, original in enumerate(expected, 1):
        parts = [r for r in records if r['message_index'] == index]
        assert ''.join(r['content'] for r in parts) == original
        assert [r['part_index'] for r in parts] == list(range(1, len(parts) + 1))
        assert all(r['part_count'] == len(parts) for r in parts)
        if index == 1:
            assert all(p['content'].endswith('\n\n') for p in parts[:-1])
    assert item['content'] == content


def test_legacy_batch_uses_each_turn_identity_and_no_last_turn_global_identity():
    turns = [{'run_id': 'first', 'task_id': None}, {'run_id': 'last', 'task_id': 'last-task'}]
    raw = '\n\n'.join(
        f"=== 回合 {i} · Run {t['run_id']} ===\n用户陈述：\n输入 {i}\nAgent 回复（未经事实核验，执行结束不代表验收通过）：\n回复 {i}"
        for i, t in enumerate(turns, 1)
    )
    meta = {'origin': 'completed_run_batch', 'turns': turns, **turns[-1]}
    op = {'member_id': 'a', 'source_id': 'run:last', 'version': 2, 'content': raw}
    records = json.loads(structured_content(op, meta, {'retain_chunk_size': 1200, 'retain_structured_chunk_size': None}))
    assert [(r['run_id'], r['task_id'], r['speaker']) for r in records] == [
        ('first', None, '用户'), ('first', None, '员工报告'),
        ('last', 'last-task', '上级委派'), ('last', 'last-task', '员工报告')]
    context = json.loads(submission_context(op, meta).splitlines()[-1])
    assert 'run_id' not in context and 'task_id' not in context
    with pytest.raises(ValueError, match='正文与回合来源不一致'):
        structured_content({**op, 'content': raw.replace('Run first', 'Run unknown')}, meta,
                           {'retain_chunk_size': 1200, 'retain_structured_chunk_size': None})


def test_too_small_budget_fails_before_losing_provenance():
    with pytest.raises(ValueError, match='来源信息'):
        structured_content({'source_id': 'manual', 'version': 1, 'content': '正文'}, {},
                           {'retain_chunk_size': 20, 'retain_structured_chunk_size': None})


def test_ordinary_run_stays_whole_with_native_policy():
    from hindsight_api.engine.retain.fact_extraction import chunk_text

    prompt = '这是完整任务输入。' * 400
    answer = '模型/embedding/reranker 版本、chunk 参数需要确认。' * 100
    content, meta = capture_content(prompt, [], answer, {'run_id': 'ordinary', 'task_id': 'task'})
    encoded = structured_content({'source_id': 'run:ordinary', 'version': 1, 'content': content}, meta, bank_policy())
    assert chunk_text(encoded, bank_policy()['retain_chunk_size'], bank_policy()['retain_structured_chunk_size']) == [encoded]
    assert [r['content'] for r in json.loads(encoded)] == [prompt, answer]
    assert all(r['part_count'] == 1 for r in json.loads(encoded))


def test_oversized_single_paragraph_is_not_silently_cut():
    with pytest.raises(ValueError, match='单个完整段落'):
        structured_content({'source_id': 'manual', 'version': 1, 'content': '完整长句' * 10000}, {}, bank_policy())
