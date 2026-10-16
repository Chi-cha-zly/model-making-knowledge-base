"""Bounded structured offline workflow outputs; no provider can alter geometry."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Surface(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(max_length=300)
    shape: Literal['flat', 'convex', 'concave', 'cylindrical', 'edge', 'mixed']
    position: str = Field(max_length=2000)
    notes: str = Field(max_length=2000)


class LightAssessment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    direction: str = Field(max_length=80)
    basis: str = Field(max_length=2000)
    conflict: bool


class AnalysisOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    surfaces: list[Surface] = Field(max_length=30)
    suggested_light_direction: LightAssessment
    reflection_hints: list[str] = Field(max_length=30)
    uncertain: list[str] = Field(max_length=30)
    mask_issues: list[str] = Field(max_length=30)
    summary: str = Field(default='', max_length=8000)
    warnings: list[str] = Field(default_factory=list, max_length=30)


class StepInstruction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    index: int = Field(ge=1, le=9)
    title: str = Field(min_length=1, max_length=300)
    color_hex: str | list[str] | None = None
    area_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)
    color_description: str = Field(min_length=1, max_length=2000)
    areas: str = Field(min_length=1, max_length=4000)
    technique: str = Field(min_length=1, max_length=4000)
    expected_result: str = Field(min_length=1, max_length=4000)
    change_from_previous: str = Field(min_length=1, max_length=4000)
    common_mistakes: str = Field(min_length=1, max_length=4000)
    needs_confirmation: list[str] = Field(max_length=20)


class PlanOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    steps: list[StepInstruction] = Field(min_length=1, max_length=9)
    overall_tips: list[str] = Field(max_length=20)


class ReviewIssue(BaseModel):
    model_config = ConfigDict(extra='forbid')
    step: int = Field(ge=1, le=9)
    type: Literal['regression', 'inconsistent_light', 'text_mismatch', 'unclear_area', 'other']
    detail: str = Field(min_length=1, max_length=4000)
    severity: Literal['low', 'medium', 'high']


class ReviewOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    issues: list[ReviewIssue] = Field(max_length=30)
    summary: str = Field(max_length=4000)
