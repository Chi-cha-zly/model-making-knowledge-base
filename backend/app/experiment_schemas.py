from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt


class ExperimentInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dataset_id: str = Field(min_length=1, max_length=80)
    mode: Literal['retrieval', 'answers']
    models: list[str] = Field(default_factory=list, max_length=2)
    limit: Annotated[StrictInt, Field(ge=1, le=10)] = 3
    k: Annotated[StrictInt, Field(ge=1, le=20)] = 5
    request_id: str = Field(min_length=1, max_length=100, pattern=r'^\S+$')


class BackupShape(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class BackupUsage(BackupShape):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class BackupResult(BackupShape):
    variant: str = Field(min_length=1, max_length=200)
    case_id: str = Field(min_length=1, max_length=100)
    query: str = Field(max_length=2000)
    answer: str | None = Field(max_length=32000)
    hit_keys: list[str] = Field(max_length=20)
    metrics: dict[str, float | None]
    usage: BackupUsage | None
    latency_ms: float = Field(ge=0)
    cost_usd: float | None = Field(ge=0)
    error: str | None = Field(max_length=2000)


class BackupLink(BackupShape):
    label: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=4000)


class BackupPayload(BackupShape):
    id: int = Field(ge=1)
    dataset_id: str = Field(min_length=1, max_length=80)
    dataset_name: str = Field(min_length=1, max_length=200)
    corpus: Literal['demo', 'live']
    mode: Literal['retrieval', 'answers']
    models: list[str] = Field(max_length=2)
    status: Literal['queued', 'running', 'completed', 'failed', 'stopped']
    progress: int = Field(ge=0, le=20)
    total: int = Field(ge=1, le=20)
    created_at: str
    updated_at: str
    error: str | None = Field(max_length=2000)
    links: list[BackupLink] = Field(max_length=3)
    results: list[BackupResult] = Field(max_length=20)
    snapshot_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
