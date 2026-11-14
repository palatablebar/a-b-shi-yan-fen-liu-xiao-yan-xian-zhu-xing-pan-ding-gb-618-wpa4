"""上线评审控制器：登记口径 / 跑评审 / 发布，全部带审计，写操作同一事务提交。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, experiment_service, review_service


def list_states(db: Session) -> dict:
    return review_service.list_states(db)


def get_state(db: Session, experiment_id: int) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    return review_service.review_state(db, experiment)


def register_plan(db: Session, experiment_id: int, payload: dict, principal: dict, request: Request) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    plan, changed = review_service.upsert_plan(db, experiment, payload, operator=principal["email"])
    if changed:
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="review.plan",
            entity="experiment",
            entity_id=experiment.id,
            detail={
                "experiment": experiment.code,
                "primary_metric": plan.primary_metric,
                "guardrail_metrics": plan.guardrail_metrics or [],
                "required_channels": plan.required_channels or [],
                "alpha": plan.alpha,
            },
            ip=request.client.host if request.client else None,
        )
    db.commit()
    return {"plan": review_service.serialize_plan(plan), "changed": changed}


def run_review(db: Session, experiment_id: int, payload: dict, principal: dict, request: Request) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    run, reused = review_service.run_review(
        db,
        experiment,
        variant_code=payload.get("variant_code"),
        operator=principal["email"],
    )
    if not reused:
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="review.run",
            entity="experiment",
            entity_id=experiment.id,
            detail={
                "experiment": experiment.code,
                "run_id": run.id,
                "variant_code": run.variant_code,
                "verdict": run.verdict,
                "blockers": run.blockers or [],
            },
            ip=request.client.host if request.client else None,
        )
    db.commit()
    return {"run": review_service.serialize_run(run), "reused": reused}


def release(db: Session, experiment_id: int, payload: dict, principal: dict, request: Request) -> dict:
    """发布：判定 + 依据 + 审计在同一事务里提交；任何一步失败整体回滚，旧版保留。"""
    experiment = experiment_service.get_experiment(db, experiment_id)
    current, switched = review_service.release(db, experiment, payload["run_id"], operator=principal["email"])
    if switched:
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="review.release",
            entity="experiment",
            entity_id=experiment.id,
            detail={
                "experiment": experiment.code,
                "run_id": current.run_id,
                "verdict": current.verdict,
                "variant_code": current.variant_code,
            },
            ip=request.client.host if request.client else None,
        )
    db.commit()
    return {"release": review_service.serialize_release(current), "switched": switched}
