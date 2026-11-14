"""上线评审落库层：预登记口径 → 跑评审（幂等）→ 发布（原子切换）。

三条铁律：
1. 口径预登记：实验开始前（draft）登记主指标 / 护栏 / 必验渠道 / α，开始后锁定（409）。
2. 评审幂等：结果按 (experiment_id, variant_code, data_fingerprint) 落库，只增不改；
   同一批数据重跑命中自然键，直接返回原结论（reused=True），不新增行也不覆盖。
3. 发布原子：判定与依据（run）在同一事务里一次性切换（upsert 单行 release）；
   只接受最新评审记录且 verdict=ship，任何失败都不写库，旧版保留。
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CHANNEL_LABELS,
    METRIC_LABELS,
    REVIEW_DIRECTION_LABELS,
    REVIEW_VERDICT_LABELS,
)
from src.models.assignment import Assignment
from src.models.experiment import Experiment
from src.models.review import ReviewPlan, ReviewRelease, ReviewRun
from src.models.variant import Variant
from src.services import variant_service
from src.services.algorithms import review as review_algorithm
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc, utcnow

VERDICT_SHIP = review_algorithm.VERDICT_SHIP


# ---------------------------------------------------------------- 序列化
def serialize_plan(plan: ReviewPlan) -> dict:
    return {
        "id": plan.id,
        "experiment_id": plan.experiment_id,
        "primary_metric": plan.primary_metric,
        "primary_metric_label": METRIC_LABELS.get(plan.primary_metric, plan.primary_metric),
        "guardrail_metrics": [
            {"metric_key": key, "metric_name": METRIC_LABELS.get(key, key)} for key in (plan.guardrail_metrics or [])
        ],
        "required_channels": [
            {"channel": key, "channel_label": CHANNEL_LABELS.get(key, key)} for key in (plan.required_channels or [])
        ],
        "alpha": plan.alpha,
        "note": plan.note,
        "registered_by": plan.registered_by,
        "registered_at": iso_utc(plan.registered_at),
    }


def _direction_payload(result: dict) -> dict:
    return {
        **result,
        "direction_label": REVIEW_DIRECTION_LABELS.get(result["direction"], result["direction"]),
    }


def serialize_run(run: ReviewRun) -> dict:
    overall = _direction_payload(run.overall or {})
    channels = [
        {**_direction_payload(item), "channel_label": CHANNEL_LABELS.get(item.get("channel"), item.get("channel"))}
        for item in (run.channel_results or [])
    ]
    guardrails = [
        {**_direction_payload(item), "metric_name": METRIC_LABELS.get(item.get("metric_key"), item.get("metric_key"))}
        for item in (run.guardrail_results or [])
    ]
    return {
        "id": run.id,
        "experiment_id": run.experiment_id,
        "variant_code": run.variant_code,
        "variant_name": run.variant_name,
        "data_fingerprint": run.data_fingerprint,
        "overall": overall,
        "channels": channels,
        "guardrails": guardrails,
        "verdict": run.verdict,
        "verdict_label": REVIEW_VERDICT_LABELS.get(run.verdict, run.verdict),
        "blockers": run.blockers or [],
        "plan_snapshot": run.plan_snapshot or {},
        "computed_by": run.computed_by,
        "created_at": iso_utc(run.created_at),
    }


def serialize_release(release: ReviewRelease) -> dict:
    return {
        "id": release.id,
        "experiment_id": release.experiment_id,
        "run_id": release.run_id,
        "variant_code": release.variant_code,
        "verdict": release.verdict,
        "verdict_label": REVIEW_VERDICT_LABELS.get(release.verdict, release.verdict),
        "snapshot": release.snapshot or {},
        "released_by": release.released_by,
        "released_at": iso_utc(release.released_at),
    }


# ---------------------------------------------------------------- 口径登记
def get_plan(db: Session, experiment_id: int) -> ReviewPlan | None:
    return db.scalar(select(ReviewPlan).where(ReviewPlan.experiment_id == experiment_id))


def _plan_content(payload: dict, default_primary: str) -> dict:
    primary = payload.get("primary_metric") or default_primary
    guardrails = list(payload.get("guardrail_metrics") or [])
    if primary in guardrails:
        raise AppError("VALIDATION", 422, {"reason": "护栏指标不能包含主指标"})
    return {
        "primary_metric": primary,
        "guardrail_metrics": guardrails,
        "required_channels": list(payload.get("required_channels") or []),
        "alpha": float(payload.get("alpha") or 0.05),
        "note": payload.get("note"),
    }


def upsert_plan(
    db: Session,
    experiment: Experiment,
    payload: dict,
    *,
    operator: str,
    force: bool = False,
) -> tuple[ReviewPlan, bool]:
    """登记 / 重登口径。实验开始后（非 draft）锁定；force 仅供 seed 模拟开始前登记。"""
    if experiment.status != "draft" and not force:
        raise AppError(
            "CONFLICT",
            409,
            {"reason": "评审口径必须在实验开始前登记，该实验已开始，口径已锁定", "status": experiment.status},
        )
    content = _plan_content(payload, experiment.primary_metric)
    plan = get_plan(db, experiment.id)
    if plan is not None:
        current = {
            "primary_metric": plan.primary_metric,
            "guardrail_metrics": list(plan.guardrail_metrics or []),
            "required_channels": list(plan.required_channels or []),
            "alpha": float(plan.alpha),
            "note": plan.note,
        }
        if current == content:
            return plan, False
        for key, value in content.items():
            setattr(plan, key, value)
        plan.registered_by = operator
        plan.registered_at = utcnow()
        db.flush()
        return plan, True
    plan = ReviewPlan(experiment_id=experiment.id, registered_by=operator, registered_at=utcnow(), **content)
    db.add(plan)
    db.flush()
    return plan, True


# ---------------------------------------------------------------- 数据聚合
def _aggregate(
    db: Session,
    experiment_id: int,
    variant_id: int,
    metric_key: str,
    channel: str | None = None,
) -> tuple[int, int]:
    stmt = select(func.coalesce(func.sum(Assignment.samples), 0), func.coalesce(func.sum(Assignment.conversions), 0)).where(
        Assignment.experiment_id == experiment_id,
        Assignment.variant_id == variant_id,
        Assignment.metric_key == metric_key,
    )
    if channel is not None:
        stmt = stmt.where(Assignment.channel == channel)
    row = db.execute(stmt).one()
    return int(row[0] or 0), int(row[1] or 0)


def _cell(control: tuple[int, int], treatment: tuple[int, int]) -> dict:
    return {"n1": treatment[0], "c1": treatment[1], "n2": control[0], "c2": control[1]}


def _fingerprint(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _treatment_variant(db: Session, experiment: Experiment, variant_code: str | None) -> Variant:
    treatments = [item for item in variant_service.list_variants(db, experiment.id) if not item.is_control]
    if variant_code:
        variant = next((item for item in treatments if item.code == variant_code), None)
        if variant is None:
            codes = [item.code for item in treatments]
            raise AppError("VALIDATION", 422, {"reason": f"variant_code 必须是实验变体之一：{codes}"})
        return variant
    if not treatments:
        raise AppError("VALIDATION", 422, {"reason": "实验没有实验组变体"})
    if len(treatments) > 1:
        codes = [item.code for item in treatments]
        raise AppError("VALIDATION", 422, {"reason": f"多个实验组变体，必须指定 variant_code：{codes}"})
    return treatments[0]


# ---------------------------------------------------------------- 跑评审
def run_review(
    db: Session,
    experiment: Experiment,
    *,
    variant_code: str | None = None,
    operator: str = "system",
) -> tuple[ReviewRun, bool]:
    """按登记口径跑评审。同一批数据重跑返回原结论（reused=True），不新增不覆盖。"""
    plan = get_plan(db, experiment.id)
    if plan is None:
        raise AppError("VALIDATION", 422, {"reason": "还没有登记评审口径，先登记主指标 / 护栏 / 必验渠道"})
    control = variant_service.control_variant(db, experiment.id)
    treatment = _treatment_variant(db, experiment, variant_code)

    overall_cell = _cell(
        _aggregate(db, experiment.id, control.id, plan.primary_metric),
        _aggregate(db, experiment.id, treatment.id, plan.primary_metric),
    )
    if overall_cell["n1"] <= 0 or overall_cell["n2"] <= 0:
        raise AppError("NO_SAMPLES", 422, {"reason": "主指标还没有分流样本，先录入样本再跑评审"})

    channel_cells: list[dict] = []
    for channel in plan.required_channels or []:
        cell = _cell(
            _aggregate(db, experiment.id, control.id, plan.primary_metric, channel),
            _aggregate(db, experiment.id, treatment.id, plan.primary_metric, channel),
        )
        if cell["n1"] <= 0 or cell["n2"] <= 0:
            label = CHANNEL_LABELS.get(channel, channel)
            raise AppError("NO_SAMPLES", 422, {"reason": f"必验渠道「{label}」没有分流样本，无法验收"})
        cell["channel"] = channel
        channel_cells.append(cell)

    guardrail_cells: list[dict] = []
    for metric_key in plan.guardrail_metrics or []:
        cell = _cell(
            _aggregate(db, experiment.id, control.id, metric_key),
            _aggregate(db, experiment.id, treatment.id, metric_key),
        )
        if cell["n1"] <= 0 or cell["n2"] <= 0:
            label = METRIC_LABELS.get(metric_key, metric_key)
            raise AppError("NO_SAMPLES", 422, {"reason": f"护栏指标「{label}」没有分流样本，无法验收"})
        cell["metric_key"] = metric_key
        guardrail_cells.append(cell)

    fingerprint = _fingerprint(
        {
            "experiment_id": experiment.id,
            "variant_code": treatment.code,
            "plan": {
                "primary_metric": plan.primary_metric,
                "guardrail_metrics": plan.guardrail_metrics or [],
                "required_channels": plan.required_channels or [],
                "alpha": plan.alpha,
            },
            "overall": overall_cell,
            "channels": channel_cells,
            "guardrails": guardrail_cells,
        }
    )
    existing = db.scalar(
        select(ReviewRun).where(
            ReviewRun.experiment_id == experiment.id,
            ReviewRun.variant_code == treatment.code,
            ReviewRun.data_fingerprint == fingerprint,
        )
    )
    if existing is not None:
        return existing, True

    try:
        outcome = review_algorithm.evaluate(
            overall=overall_cell,
            channels=channel_cells,
            guardrails=guardrail_cells,
            alpha=plan.alpha,
        )
    except ValueError as exc:
        raise AppError("VALIDATION", 422, {"reason": str(exc)}) from exc

    run = ReviewRun(
        experiment_id=experiment.id,
        variant_code=treatment.code,
        variant_name=treatment.name,
        data_fingerprint=fingerprint,
        overall=outcome["overall"],
        channel_results=outcome["channels"],
        guardrail_results=outcome["guardrails"],
        verdict=outcome["verdict"],
        blockers=outcome["blockers"],
        plan_snapshot={
            "primary_metric": plan.primary_metric,
            "guardrail_metrics": plan.guardrail_metrics or [],
            "required_channels": plan.required_channels or [],
            "alpha": plan.alpha,
        },
        computed_by=operator,
    )
    db.add(run)
    db.flush()
    return run, False


def latest_run(db: Session, experiment_id: int, variant_code: str) -> ReviewRun | None:
    return db.scalar(
        select(ReviewRun)
        .where(ReviewRun.experiment_id == experiment_id, ReviewRun.variant_code == variant_code)
        .order_by(ReviewRun.id.desc())
        .limit(1)
    )


# ---------------------------------------------------------------- 发布
def get_release(db: Session, experiment_id: int) -> ReviewRelease | None:
    return db.scalar(select(ReviewRelease).where(ReviewRelease.experiment_id == experiment_id))


def release(db: Session, experiment: Experiment, run_id: int, *, operator: str) -> tuple[ReviewRelease, bool]:
    """发布：判定与依据一次性切换（调用方在同一事务里提交，失败整体回滚）。

    返回 (release, switched)；重复发布同一依据是幂等空操作（switched=False）。
    """
    run = db.get(ReviewRun, run_id)
    if run is None or run.experiment_id != experiment.id:
        raise not_found("review_run", run_id)
    latest = latest_run(db, experiment.id, run.variant_code)
    if latest is not None and latest.id != run.id:
        raise AppError(
            "CONFLICT",
            409,
            {"reason": "已有更新的评审结果，旧结论不能作为发布依据", "latest_run_id": latest.id},
        )
    if run.verdict != VERDICT_SHIP:
        raise AppError(
            "CONFLICT",
            409,
            {"reason": "评审结论为不可上线，不能发布；任一必验渠道显著负向或护栏恶化都翻不了案", "blockers": run.blockers or []},
        )
    snapshot = serialize_run(run)
    current = get_release(db, experiment.id)
    if current is not None and current.run_id == run.id:
        return current, False
    if current is None:
        current = ReviewRelease(experiment_id=experiment.id)
        db.add(current)
    current.run_id = run.id
    current.variant_code = run.variant_code
    current.verdict = run.verdict
    current.snapshot = snapshot
    current.released_by = operator
    current.released_at = utcnow()
    db.flush()
    return current, True


# ---------------------------------------------------------------- 查询
def review_state(db: Session, experiment: Experiment) -> dict:
    plan = get_plan(db, experiment.id)
    current_release = get_release(db, experiment.id)
    runs = db.scalars(
        select(ReviewRun).where(ReviewRun.experiment_id == experiment.id).order_by(ReviewRun.id.desc()).limit(10)
    ).all()
    latest = runs[0] if runs else None
    treatments = [
        {"code": item.code, "name": item.name}
        for item in variant_service.list_variants(db, experiment.id)
        if not item.is_control
    ]
    return {
        "experiment_id": experiment.id,
        "experiment_code": experiment.code,
        "experiment_name": experiment.name,
        "experiment_status": experiment.status,
        "can_register": experiment.status == "draft",
        "treatments": treatments,
        "plan": serialize_plan(plan) if plan else None,
        "latest_run": serialize_run(latest) if latest else None,
        "release": serialize_release(current_release) if current_release else None,
        "runs": [serialize_run(item) for item in runs],
    }


def list_states(db: Session) -> dict:
    experiments = db.scalars(select(Experiment).order_by(Experiment.id)).all()
    plans = {item.experiment_id: item for item in db.scalars(select(ReviewPlan)).all()}
    releases = {item.experiment_id: item for item in db.scalars(select(ReviewRelease)).all()}
    latest_runs: dict[int, ReviewRun] = {}
    for run in db.scalars(select(ReviewRun).order_by(ReviewRun.id)).all():
        latest_runs[run.experiment_id] = run
    items = []
    for experiment in experiments:
        run = latest_runs.get(experiment.id)
        current_release = releases.get(experiment.id)
        items.append(
            {
                "experiment_id": experiment.id,
                "experiment_code": experiment.code,
                "experiment_name": experiment.name,
                "experiment_status": experiment.status,
                "plan_registered": experiment.id in plans,
                "latest_run": (
                    {
                        "id": run.id,
                        "verdict": run.verdict,
                        "verdict_label": REVIEW_VERDICT_LABELS.get(run.verdict, run.verdict),
                        "blockers": run.blockers or [],
                        "created_at": iso_utc(run.created_at),
                    }
                    if run
                    else None
                ),
                "release": (
                    {
                        "run_id": current_release.run_id,
                        "verdict": current_release.verdict,
                        "verdict_label": REVIEW_VERDICT_LABELS.get(current_release.verdict, current_release.verdict),
                        "released_by": current_release.released_by,
                        "released_at": iso_utc(current_release.released_at),
                    }
                    if current_release
                    else None
                ),
            }
        )
    return {"items": items, "total": len(items)}
