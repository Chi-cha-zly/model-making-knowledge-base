from typing import Protocol
from pydantic import BaseModel, ConfigDict, Field


class AIProvider(Protocol):
    configured: bool
    model: str | None

    def stream(self, messages): ...


class TitleInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(default='新对话', min_length=1, max_length=300)


class RequestInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    request_id: str = Field(min_length=1, max_length=128)


class TurnInput(RequestInput):
    content: str = Field(min_length=1, max_length=16000)
