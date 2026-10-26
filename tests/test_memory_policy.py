import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.memory.engine.hindsight import HindsightEngine, bank_id
from app.memory.prompts import RETAIN_MISSION, OBSERVATIONS_MISSION, bank_policy


def adapter(upstream):
    result = HindsightEngine({})
    result.engine = upstream
    result.context = None
    upstream.get_bank_config = AsyncMock(return_value=SimpleNamespace(config={"retain_chunk_size": 3000, "retain_structured_chunk_size": None}))
    return result


def operation(version=1):
    return {
        "id": "stable-operation", "source_id": "run:batch-last", "version": version,
        "member_id": "employee-a", "content": "用户陈述：\n制造业经历尚未确认。\nAgent 回复（未经事实核验，执行结束不代表验收通过）：\n用户没有制造业经历。",
        "metadata": json.dumps({
            "origin": "completed_run", "verification": "unverified", "run_id": "batch-last",
            "task_id": "task-a", "task_status_at_capture": "review",
            "run_ids": ["direct-run", "delegated-run"],
            "turns": [{"run_id": "direct-run", "task_id": None, "verification": "unverified"},
                      {"run_id": "delegated-run", "task_id": "task-a", "verification": "unverified"}],
        }),
    }


@pytest.mark.asyncio
async def test_existing_and_new_banks_merge_only_policy_once_and_refresh_after_close():
    configs = {bank_id("existing"): {"llm_output_language": "zh", "retain_mission": "old"}}

    async def merge(bank, updates, **kwargs):
        configs.setdefault(bank, {}).update(updates)

    upstream = SimpleNamespace(ensure_bank_profile=AsyncMock(), update_bank_config=AsyncMock(side_effect=merge), close=AsyncMock())
    engine = adapter(upstream)
    for member in ("existing", "new", None):
        await engine.ensure_bank(member)
        await engine.ensure_bank(member)
        assert configs[bank_id(member)].items() >= bank_policy().items()
    assert configs[bank_id("existing")]["llm_output_language"] == "zh"
    assert upstream.update_bank_config.await_count == 3
    await engine.close()
    assert not engine.banks
    engine.engine = upstream
    await engine.ensure_bank("existing")
    assert upstream.update_bank_config.await_count == 4


@pytest.mark.asyncio
async def test_failed_policy_is_not_cached_or_submitted_and_can_recover():
    upstream = SimpleNamespace(ensure_bank_profile=AsyncMock(), update_bank_config=AsyncMock(side_effect=RuntimeError("failed")),
                               submit_async_retain=AsyncMock())
    engine = adapter(upstream)
    with pytest.raises(RuntimeError, match="failed"):
        await engine.submit(operation())
    assert not engine.banks
    upstream.submit_async_retain.assert_not_awaited()
    upstream.update_bank_config.side_effect = None
    await engine.submit(operation())
    assert engine.banks == {bank_id("employee-a")}
    upstream.submit_async_retain.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
async def test_submission_keeps_raw_content_role_evidence_and_upstream_idempotency_key(version):
    upstream = SimpleNamespace(ensure_bank_profile=AsyncMock(), update_bank_config=AsyncMock(), submit_async_retain=AsyncMock())
    engine = adapter(upstream)
    item = operation(version)
    await engine.submit(item)
    await engine.submit(item)
    first, second = upstream.submit_async_retain.await_args_list
    assert first == second
    assert first.kwargs["operation_id"] == item["id"]
    bank, contents = first.args
    assert bank == bank_id(item["member_id"])
    content = contents[0]
    records = json.loads(content["content"])
    assert records[0]["speaker"] == "上级委派" and records[-1]["speaker"] == "员工报告"
    assert all(r["run_id"] == "batch-last" and r["task_id"] == "task-a" for r in records)
    assert content["document_id"] == item["source_id"]
    assert content["force_reextract"] is (version > 1)
    provenance = json.loads(content["context"].splitlines()[-1])
    assert provenance["employee_id"] == item["member_id"]
    assert provenance["source_id"] == item["source_id"]
    assert provenance["source_version"] == version
    assert provenance["verification"] == "unverified"
    assert provenance["task_id"] == "task-a" and provenance["task_status_at_capture"] == "review"
    assert provenance["turns"] == json.loads(item["metadata"])["turns"]
    assert json.loads(content["metadata"]["run_ids"]) == provenance["run_ids"]
    assert upstream.update_bank_config.await_count == 1


def test_native_prompt_rendering_accepts_policy_and_empty_extraction():
    config_module = pytest.importorskip("hindsight_api.config")
    from hindsight_api.engine.retain.fact_extraction import build_chunk_prompt_parts
    from hindsight_api.engine.consolidation.prompts import build_consolidation_input

    config = replace(config_module.HindsightConfig.from_env(), **bank_policy())
    rendered = build_chunk_prompt_parts(config, chunk="本轮未修改文件；截图在临时目录。")
    assert RETAIN_MISSION in rendered.user_message
    assert "SELECTIVITY" in rendered.system_prompt
    assert rendered.response_schema.model_validate({"facts": []}).facts == []
    for text in ("本轮未修改文件。", "用户明确要求今后先区分简历与核验报告；Agent 自称验收通过。"):
        consolidation = build_consolidation_input(text, "[]", observations_mission=config.observations_mission)
        assert OBSERVATIONS_MISSION in consolidation
        assert "Track anything notable" not in consolidation
