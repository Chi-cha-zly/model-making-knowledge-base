"""Bounded, plain-text article blocks shared by requests and backup validation."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, AfterValidator


class Paragraph(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: Literal['paragraph']
    text: str = Field(max_length=20000)


class Heading(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: Literal['heading']
    text: str = Field(max_length=300)


class Image(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: Literal['image']
    attachment_id: StrictInt = Field(gt=0)
    caption: str = Field(max_length=1000)


def bound_total(blocks):
    if sum(len(b.caption if isinstance(b, Image) else b.text) for b in blocks) > 100000:
        raise ValueError('Content text exceeds 100000 characters')
    return blocks


Block = Annotated[Paragraph | Heading | Image, Field(discriminator='type')]
ContentBlocks = Annotated[list[Block], Field(max_length=200), AfterValidator(bound_total)]
content_adapter = TypeAdapter(ContentBlocks)


def image_ids(blocks):
    return list(dict.fromkeys(b['attachment_id'] for b in blocks if b['type'] == 'image'))


def derived_steps(blocks):
    return [text for b in blocks if (text := b['caption'] if b['type'] == 'image' else b['text'])]
