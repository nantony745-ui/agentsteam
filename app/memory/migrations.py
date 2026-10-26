"""记忆归属迁移：旧记录保留 NULL，继续属于共享池。"""


async def migrate(db):
    for table in ("memory_capture_turns", "memory_contexts"):
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        if "member_id" not in columns:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN member_id TEXT")
    await db.execute("CREATE INDEX IF NOT EXISTS memory_capture_owner ON memory_capture_turns(member_id,source_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS memory_source_owner ON memory_sources(member_id,created_at)")
