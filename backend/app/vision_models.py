"""Image interpretation is derived evidence, separate from authored knowledge."""

from datetime import datetime
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, now


ShortText = Annotated[str, Field(max_length=500)]


class ImageDescription(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    description: str = Field(min_length=1, max_length=4000)
    materials: list[ShortText] = Field(max_length=20)
    techniques: list[ShortText] = Field(max_length=30)
    colors: list[ShortText] = Field(max_length=30)
    step_number: StrictInt | None = Field(ge=0, le=1000)
    visible_text: str = Field(max_length=12000)
    uncertainties: list[ShortText] = Field(max_length=20)


class ImageAnalysis(Base):
    __tablename__ = 'image_analyses'
    attachment_id: Mapped[int] = mapped_column(
        ForeignKey('attachments.id', ondelete='CASCADE'), primary_key=True
    )
    image_hash: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(200))
    ocr_text: Mapped[str] = mapped_column(Text)
    description: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)
