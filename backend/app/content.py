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



def legacy_steps_to_blocks(steps):
    """Import boundary: preserve text, reshape legacy lists to modern bounds."""
    from .retrieval import readable_excerpt
    texts = [readable_excerpt(step) for step in (steps if isinstance(steps, list) else [steps]) if step is not None]
    if sum(map(len, texts)) > 100000:
        raise ValueError('Legacy content exceeds 100000 characters; split the source record before migration')
    parts = [text[start:start + 20000] for text in texts for start in range(0, max(1,len(text)), 20000)]
    if len(parts) > 200:
        joined = '\n'.join(parts)
        if len(joined) > 100000:
            raise ValueError('Legacy content exceeds 100000 characters; split the source record before migration')
        parts = [joined[start:start + 20000] for start in range(0,len(joined),20000)]
    return [{'type':'paragraph', 'text':text} for text in parts]
