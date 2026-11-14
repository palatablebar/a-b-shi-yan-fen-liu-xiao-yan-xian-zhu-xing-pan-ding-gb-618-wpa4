"""上线评审的入参 schema。

评审口径（主指标 / 护栏 / 必验渠道 / α）只能在登记时写入；
跑评审与发布的入参一律 extra="forbid" —— 不允许临时传渠道子集或指标子集，
挑选表现好的渠道也翻不了案。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.schemas.experiment import CHANNEL_VALUES, METRIC_VALUES


def _dedupe(values: list[str], what: str) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    if len(seen) != len(values):
        raise ValueError(f"{what} 不能重复")
    return seen


class ReviewPlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_metric: str | None = Field(default=None, description="缺省取实验登记的主指标")
    guardrail_metrics: list[str] = Field(default_factory=list, max_length=5)
    required_channels: list[str] = Field(min_length=1, max_length=4)
    alpha: float = Field(default=0.05, gt=0, lt=0.5)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("primary_metric")
    @classmethod
    def _primary_ok(cls, value: str | None) -> str | None:
        if value is not None and value not in METRIC_VALUES:
            raise ValueError(f"primary_metric 必须是 {METRIC_VALUES} 之一")
        return value

    @field_validator("guardrail_metrics")
    @classmethod
    def _guardrails_ok(cls, value: list[str]) -> list[str]:
        _dedupe(value, "护栏指标")
        for item in value:
            if item not in METRIC_VALUES:
                raise ValueError(f"护栏指标必须是 {METRIC_VALUES} 之一")
        return value

    @field_validator("required_channels")
    @classmethod
    def _channels_ok(cls, value: list[str]) -> list[str]:
        _dedupe(value, "必验渠道")
        for item in value:
            if item not in CHANNEL_VALUES:
                raise ValueError(f"必验渠道必须是 {CHANNEL_VALUES} 之一")
        return value


class ReviewRunIn(BaseModel):
    """跑评审：除变体外不接受任何会改变口径的参数。"""

    model_config = ConfigDict(extra="forbid")

    variant_code: str | None = Field(default=None, max_length=40)


class ReviewReleaseIn(BaseModel):
    """发布：只能指定评审记录，判定取自该记录本身。"""

    model_config = ConfigDict(extra="forbid")

    run_id: int = Field(gt=0)
