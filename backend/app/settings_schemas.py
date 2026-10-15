from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .runtime_config import valid_endpoint


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    compare_model: str = Field(default='', max_length=200)
    reasoning_effort: Literal['auto', 'low', 'medium', 'high'] = 'auto'
    max_output_tokens: int = Field(default=2048, ge=64, le=2048)
    api_key: str | None = Field(default=None, max_length=4096)

    @field_validator('base_url')
    @classmethod
    def endpoint(cls, value):
        if not valid_endpoint(value) or value != value.strip():
            raise ValueError('invalid endpoint')
        return value

    @field_validator('name', 'model')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('blank value')
        return value


class PromptInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str = Field(max_length=8000)
