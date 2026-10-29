
import asyncio
from dataclasses import asdict, is_dataclass, replace
from urllib.parse import urlsplit

from app.memory.models import normalize_base_url
from app.memory.prompts import bank_policy, submission_context
from app.memory.ingestion import structured_content

BANK = "agentsteam-shared"
def bank_id(member_id):
    return BANK if member_id is None else f"agentsteam-employee-{member_id}"


REVISION = "0ee8b17612dd08b66bcfe216dfb7b03eeeb3fff1"


def plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    return value


def validate_database(url):
    parts = urlsplit(url)
    if parts.scheme not in {"postgres", "postgresql"} or not parts.hostname:
        raise ValueError("请填写 PostgreSQL 连接串")
    if parts.path.lstrip("/") != "agentsteam_memory" and not parts.path.lstrip("/").startswith("agentsteam_memory_test_"):
        raise ValueError("仅允许专用数据库 agentsteam_memory 或 agentsteam_memory_test_*，不会迁移其他数据库")


async def probe_database(url):
    import asyncpg
    validate_database(url)
    connection = await asyncpg.connect(url, timeout=5)
    try:
        version = await connection.fetchval("SHOW server_version")
        vector = await connection.fetchval("SELECT default_version FROM pg_available_extensions WHERE name='vector'")
        if vector is None:
            raise ValueError("PostgreSQL 尚未安装 pgvector 扩展文件")
        return {"version": version, "pgvector_available": vector}
    finally:
        await connection.close()


class HindsightEngine:
    def __init__(self, settings):
        self.settings = settings
        self.engine = None
        self.poller = None
        self.task = None
        self.banks = set()
        self.bank_lock = asyncio.Lock()

    async def start(self):
        from hindsight_api import config
        from hindsight_api.models import RequestContext
        from hindsight_api.worker import WorkerPoller

        cfg = self.settings
        await probe_database(cfg["database_url"])
        # 上游配置是进程级单例。仅在旧引擎关闭后替换，不把密钥写入环境变量。
        config._config_cache = replace(
            config.HindsightConfig.from_env(), database_url=cfg["database_url"],
            llm_provider=cfg["llm"]["provider"], llm_api_key=cfg["llm"]["api_key"], llm_model=cfg["llm"]["model"],
            llm_base_url=normalize_base_url(cfg["llm"]["base_url"]), embeddings_provider="openai",
            embeddings_openai_api_key=cfg["embedding"]["api_key"], embeddings_openai_model=cfg["embedding"]["model"],
            embeddings_openai_base_url=normalize_base_url(cfg["embedding"]["base_url"]), embeddings_openai_dimensions=cfg["embedding_dimensions"],
            reranker_provider=cfg["rerank"]["provider"], reranker_cohere_api_key=cfg["rerank"]["api_key"],
            reranker_cohere_model=cfg["rerank"]["model"], reranker_cohere_base_url=normalize_base_url(cfg["rerank"]["base_url"]) + "/rerank",
            reranker_siliconflow_api_key=cfg["rerank"]["api_key"], reranker_siliconflow_model=cfg["rerank"]["model"],
            reranker_siliconflow_base_url=normalize_base_url(cfg["rerank"]["base_url"]), llm_reasoning_effort=cfg["reasoning_effort"],
            vector_extension="pgvector", text_search_extension="native",
            llm_max_concurrent=2, llm_max_retries=2, llm_timeout=90.0,
            **bank_policy(),
        )
        from hindsight_api.engine.memory_engine import MemoryEngine
        self.context = RequestContext()
        self.engine = MemoryEngine(skip_llm_verification=True, pool_min_size=1, pool_max_size=6)
        try:
            await self.engine.initialize()
            await self.ensure_bank(None)
            self.poller = WorkerPoller(
                backend=self.engine._require_backend(), worker_id="agentsteam-memory",
                executor=self.engine.execute_task, max_slots=2,
                slot_reservations={"consolidation": 1},
                tenant_extension=self.engine._tenant_extension,
                on_wall_timeout=self.engine.on_task_wall_timeout,
            )
            self.task = asyncio.create_task(self.poller.run(), name="hindsight-worker")
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.poller:
            await self.poller.shutdown_graceful(timeout=5)
            self.poller = None
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.engine:
            await self.engine.close()
            self.engine = None
        self.banks.clear()

    async def ensure_bank(self, member_id):
        bank = bank_id(member_id)
        if bank in self.banks:
            return bank
        async with self.bank_lock:
            if bank not in self.banks:
                await self.engine.ensure_bank_profile(bank, request_context=self.context)
                await self.engine.update_bank_config(bank, bank_policy(), request_context=self.context)
                self.banks.add(bank)
        return bank

    async def submit(self, operation):
        import json
        bank = await self.ensure_bank(operation.get("member_id"))
        config = await self.engine.get_bank_config(bank, request_context=self.context)
        original_metadata = json.loads(operation["metadata"])
        metadata = {
            key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
            for key, value in original_metadata.items() if key not in {"messages", "content_sha256"}
        }
        if original_metadata.get("origin") == "completed_run_batch":
            for key in ("run_id", "session_id", "task_id", "task_status_at_capture"):
                metadata.pop(key, None)
        return await self.engine.submit_async_retain(bank, [{
            "content": structured_content(operation, original_metadata, config.config),
            "document_id": operation["source_id"], "update_mode": "replace",
            "force_reextract": operation["version"] > 1,
            "metadata": {**metadata, "source_version": str(operation["version"])},
            "context": submission_context(operation, original_metadata),
        }], operation_id=operation["id"], request_context=self.context)

    async def operation(self, operation_id, member_id=None):
        return await self.engine.get_operation_status(bank_id(member_id), operation_id, request_context=self.context)

    async def retry(self, operation_id, member_id=None):
        return await self.engine.retry_operation(bank_id(member_id), operation_id, request_context=self.context)

    async def delete(self, source_id, member_id=None):
        return await self.engine.delete_document(source_id, bank_id(member_id), request_context=self.context)

    async def freshness(self, member_id=None):
        return await self.engine.get_bank_freshness(bank_id(member_id), request_context=self.context)

    async def recall(self, query, member_id=None):
        return plain(await self.engine.recall_async(bank_id(member_id), query, max_tokens=1800, include_source_facts=True,
                                                   request_context=self.context))

    async def reflect(self, query, member_id=None):
        return plain(await self.engine.reflect_async(bank_id(member_id), query, request_context=self.context))

    async def facts(self, kind, offset, member_id=None):
        return plain(await self.engine.list_memory_units(bank_id(member_id), fact_type=None if kind == "all" else kind, limit=50, offset=offset,
                                                         request_context=self.context))
