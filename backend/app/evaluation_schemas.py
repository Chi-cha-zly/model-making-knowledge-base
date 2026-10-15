"""Bounded evaluation input contracts, independent of HTTP."""

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class Judgment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(pattern=r'^(technique|product):[1-9][0-9]*$', max_length=80)
    grade: Annotated[StrictInt, Field(ge=1, le=3)]


class Case(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=100)
    query: str = Field(max_length=2000)
    category: str = Field(min_length=1, max_length=100)
    judgments: list[Judgment] = Field(max_length=200)

    @model_validator(mode='after')
    def unique_labels(self):
        if len({j.key for j in self.judgments}) != len(self.judgments):
            raise ValueError('Duplicate judgment key')
        if not self.id.strip() or not self.category.strip():
            raise ValueError('Case id and category cannot be blank')
        return self


class DatasetInput(BaseModel):
    # Exported Dataset JSON can be directly imported; identity is always assigned locally.
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=4000)
    cases: list[Case] = Field(min_length=1, max_length=200)

    @model_validator(mode='before')
    @classmethod
    def live_import_only(cls, value):
        if isinstance(value, dict) and (
            value.get('corpus', 'live') != 'live' or value.get('builtin', False) is not False
        ):
            raise ValueError(
                'Demo corpus cannot be imported into the live library; create live judgments explicitly'
            )
        return value

    @model_validator(mode='after')
    def unique_cases(self):
        if not self.name.strip():
            raise ValueError('Name cannot be blank')
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError('Duplicate case id')
        return self


class RunInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dataset_id: str = Field(min_length=1, max_length=80)
    k: Annotated[StrictInt, Field(ge=1, le=20)] = 5
    strategies: list[Literal['bm25', 'hybrid']] | None = None

    @model_validator(mode='after')
    def strategy_pair(self):
        if self.strategies is not None and self.strategies != ['bm25', 'hybrid']:
            raise ValueError('Use the explicit bm25/hybrid comparison pair')
        return self


class PreviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(max_length=2000)
    k: Annotated[StrictInt, Field(ge=1, le=20)] = 5
