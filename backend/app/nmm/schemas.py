from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AssetRole(StrEnum):
    ORIGINAL = 'original'
    MASK = 'mask'
    REFERENCE_STYLE = 'reference_style'
    REFERENCE_PALETTE = 'reference_palette'
    REFERENCE_STEPS = 'reference_steps'
    CANDIDATE = 'candidate'
    FINAL = 'final'
    STEP = 'step'
    STEP_DIFF = 'step_diff'
    CONTACT_SHEET = 'contact_sheet'


class NmmParams(BaseModel):
    model_config = ConfigDict(extra='forbid')
    metal: str = Field(default='silver', min_length=1, max_length=80)
    light_direction: Literal[
        'top',
        'top_left',
        'top_right',
        'left',
        'right',
        'front',
        'bottom',
        'bottom_left',
        'bottom_right',
        'back',
    ] = 'top_left'
    contrast: Literal['low', 'medium', 'high'] = 'high'
    step_count: int = Field(default=7, ge=4, le=9)
    notes: str = Field(default='', max_length=4000)
