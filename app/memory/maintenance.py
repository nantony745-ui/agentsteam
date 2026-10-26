"""运维替换已验收的员工 bank，失败时仅恢复该员工。"""


async def replace_bank(engine, bank: str, candidate: bytes, backup: bytes, request_context, apply_sources):
    await engine.delete_bank(bank, request_context=request_context)
    try:
        result = await engine.import_bank_async(candidate, request_context, target_bank_id=bank)
        await apply_sources()
        return result
    except Exception:
        await engine.delete_bank(bank, request_context=request_context)
        await engine.import_bank_async(backup, request_context, target_bank_id=bank)
        raise
