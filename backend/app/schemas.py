from typing import Literal
from .content import ContentBlocks

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Status = Literal["draft", "published", "withdrawn"]
Visibility = Literal["private", "public"]


class SourceInput(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    url: str | None = None
    source_type: str = "link"
    author: str | None = None
    license_note: str | None = None
    visibility: Visibility = "private"
    status: Status = "published"


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=300)
    url: str | None = None
    source_type: str | None = None
    author: str | None = None
    license_note: str | None = None
    visibility: Visibility | None = None
    status: Status | None = None


class TechniqueInput(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    category: str | None = None
    summary: str | None = None
    steps: list[str] = Field(default_factory=list)
    content_blocks: ContentBlocks = Field(default_factory=list)
    prerequisites: str | None = None
    risks: str | None = None
    difficulty: str | None = None
    tags: list[str] = Field(default_factory=list)
    original: bool = False
    source_ids: list[int] = Field(default_factory=list)
    attachment_ids: list[int] = Field(default_factory=list)
    visibility: Visibility = "private"
    status: Literal["draft", "published"] = "published"

    @model_validator(mode="after")
    def validate_origin(self):
        if self.original and self.source_ids:
            raise ValueError("Original techniques cannot also select existing sources")
        if not self.original and not self.source_ids:
            raise ValueError("Select a source or mark the technique as original")
        return self


class TechniquePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=300)
    category: str | None = None
    summary: str | None = None
    steps: list[str] | None = None
    content_blocks: ContentBlocks = Field(default_factory=list)
    prerequisites: str | None = None
    risks: str | None = None
    difficulty: str | None = None
    tags: list[str] | None = None
    source_ids: list[int] | None = Field(default=None, min_length=1)
    attachment_ids: list[int] = Field(default_factory=list)
    visibility: Visibility | None = None
    status: Status | None = None


class ProductInput(BaseModel):
    brand: str | None = None
    name: str = Field(min_length=1, max_length=300)
    sku: str | None = None
    kind: str | None = None
    aliases: list[str] = Field(default_factory=list)
    source_ids: list[int] = Field(min_length=1)
    attachment_ids: list[int] = Field(default_factory=list)
    properties: dict = Field(default_factory=dict)
    manufacturer_url: str | None = None
    visibility: Visibility = "private"
    status: Status = "published"


class ProductPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brand: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=300)
    sku: str | None = None
    kind: str | None = None
    aliases: list[str] | None = None
    source_ids: list[int] | None = Field(default=None, min_length=1)
    attachment_ids: list[int] | None = None
    properties: dict | None = None
    manufacturer_url: str | None = None
    visibility: Visibility | None = None
    status: Status | None = None
