"""上线评审：预登记口径 / 评审记录 / 发布记录。

- ReviewPlan：实验开始前登记的评审口径（主指标 + 护栏指标 + 必验渠道 + α），
  一个实验一份（experiment_id 唯一），实验开始后锁定不可改。
- ReviewRun：一次评审结果，按 (experiment_id, variant_code, data_fingerprint) 幂等，
  只增不改；同一批数据重跑命中自然键，不新增行也不覆盖原结论。
- ReviewRelease：当前生效的发布（experiment_id 唯一），判定与依据（run）在同一事务里
  一次性切换；发布失败整体回滚，保留旧版。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class ReviewPlan(Base):
    __tablename__ = "review_plans"
    __table_args__ = (UniqueConstraint("experiment_id", name="uq_review_plan_experiment"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), index=True)
    primary_metric: Mapped[str] = mapped_column(String(40))
    guardrail_metrics: Mapped[list] = mapped_column(JSON, default=list)
    required_channels: Mapped[list] = mapped_column(JSON, default=list)
    alpha: Mapped[float] = mapped_column(Float, default=0.05)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    registered_by: Mapped[str] = mapped_column(String(160), default="")
    registered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ReviewRun(Base):
    __tablename__ = "review_runs"
    __table_args__ = (
        UniqueConstraint("experiment_id", "variant_code", "data_fingerprint", name="uq_review_run_fingerprint"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), index=True)
    variant_code: Mapped[str] = mapped_column(String(40))
    variant_name: Mapped[str] = mapped_column(String(120), default="")
    data_fingerprint: Mapped[str] = mapped_column(String(64))
    overall: Mapped[dict] = mapped_column(JSON, default=dict)
    channel_results: Mapped[list] = mapped_column(JSON, default=list)
    guardrail_results: Mapped[list] = mapped_column(JSON, default=list)
    verdict: Mapped[str] = mapped_column(String(16), index=True)
    blockers: Mapped[list] = mapped_column(JSON, default=list)
    plan_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    computed_by: Mapped[str] = mapped_column(String(160), default="system")
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ReviewRelease(Base):
    __tablename__ = "review_releases"
    __table_args__ = (UniqueConstraint("experiment_id", name="uq_review_release_experiment"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int] = mapped_column(Integer)
    variant_code: Mapped[str] = mapped_column(String(40), default="")
    verdict: Mapped[str] = mapped_column(String(16))
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    released_by: Mapped[str] = mapped_column(String(160), default="")
    released_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
