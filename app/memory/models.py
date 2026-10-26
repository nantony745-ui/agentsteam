"""共用记忆池的应用契约；bank 与来源身份由服务端决定。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmployeeMemorySettings(MemoryInput):
    enabled: bool


class MemoryUnavailable(RuntimeError):
    pass


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip('/')
    for suffix in ('/chat/completions', '/responses', '/embeddings', '/rerank'):
        if value.endswith(suffix):
            return value[:-len(suffix)]
    return value


class CloudModel(MemoryInput):
    provider: Literal["openai", "openai-responses", "cohere", "siliconflow"] = "openai"
    base_url: str = ""
    model: str = ""
    api_key: SecretStr | None = None

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        from urllib.parse import urlsplit
        if value:
            parts = urlsplit(value)
            if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.query or parts.fragment:
                raise ValueError("模型地址必须是无凭据、查询参数的 HTTP(S) 地址")
        return normalize_base_url(value)


class ConnectionProbe(MemoryInput):
    connection: CloudModel | None = None
    database_url: SecretStr | None = None
    embedding_dimensions: int | None = Field(default=None, ge=1, le=2000)


class MemorySettings(MemoryInput):
    enabled: bool = False
    auto_retain: bool = True
    auto_recall: bool = True
    auto_retain_turns: int = Field(default=3, strict=True, ge=1)
    database_url: SecretStr | None = None
    llm: CloudModel = Field(default_factory=CloudModel)
    embedding: CloudModel = Field(default_factory=CloudModel)
    rerank: CloudModel = Field(default_factory=lambda: CloudModel(provider="cohere"))
    embedding_dimensions: int | None = Field(default=1024, ge=1, le=2000)
    reasoning_effort: Literal["low", "medium", "high"] = "high"


class RetainInput(MemoryInput):
    request_id: UUID
    content: str = Field(min_length=1, max_length=100000)

    @field_validator("content")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("内容不能为空")
        return value


class QueryInput(MemoryInput):
    query: str = Field(min_length=1, max_length=16000)


class RevisionInput(MemoryInput):
    version: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=100000)


class VersionInput(MemoryInput):
    version: int = Field(ge=1)
