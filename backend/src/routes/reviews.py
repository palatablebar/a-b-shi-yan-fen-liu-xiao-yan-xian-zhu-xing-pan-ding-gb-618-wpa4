from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import REVIEW_ROLES
from src.controllers import review_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.review import ReviewPlanIn, ReviewReleaseIn, ReviewRunIn

router = APIRouter(tags=["reviews"])


@router.get("/reviews")
def list_reviews(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return review_controller.list_states(db)


@router.get("/experiments/{experiment_id}/review")
def get_review(
    experiment_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return review_controller.get_state(db, experiment_id)


@router.put("/experiments/{experiment_id}/review-plan")
def register_plan(
    experiment_id: int,
    payload: ReviewPlanIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*REVIEW_ROLES)),
) -> dict:
    return review_controller.register_plan(db, experiment_id, payload.model_dump(), principal, request)


@router.post("/experiments/{experiment_id}/review-runs", status_code=201)
def run_review(
    experiment_id: int,
    payload: ReviewRunIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*REVIEW_ROLES)),
) -> dict:
    result = review_controller.run_review(db, experiment_id, payload.model_dump(), principal, request)
    if result["reused"]:
        # 同一批数据重跑：命中既有结论，不新增记录
        response.status_code = 200
    return result


@router.post("/experiments/{experiment_id}/review-releases")
def release(
    experiment_id: int,
    payload: ReviewReleaseIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*REVIEW_ROLES)),
) -> dict:
    return review_controller.release(db, experiment_id, payload.model_dump(), principal, request)
