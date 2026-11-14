"""上线评审：算法门禁（纯函数）+ API 全流程（登记锁 / 幂等 / 发布原子性）。

API 层用 SQLite 内存库 + dependency_overrides 替换 get_db，不依赖 MySQL / Redis。
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config.app import create_app
from src.config.database import get_db
from src.models import Base
from src.models.assignment import Assignment
from src.models.experiment import Experiment
from src.models.review import ReviewRun
from src.models.variant import Variant
from src.services.algorithms import review as review_algorithm
from src.utils.security import create_access_token

CHANNELS = ["feed", "push", "search", "mp"]


# ---------------------------------------------------------------- 算法层
def _cell(n1, c1, n2, c2):
    return {"n1": n1, "c1": c1, "n2": n2, "c2": c2}


def _up_cell():
    return _cell(5000, 575, 5000, 500)  # 10.0% → 11.5%，显著上涨


def test_ship_when_overall_up_and_channels_guardrails_clean():
    outcome = review_algorithm.evaluate(
        overall=_up_cell(),
        channels=[{"channel": ch, **_up_cell()} for ch in CHANNELS],
        guardrails=[{"metric_key": "retention_d1", **_cell(5000, 1525, 5000, 1500)}],
        alpha=0.05,
    )
    assert outcome["verdict"] == "ship"
    assert outcome["blockers"] == []
    assert outcome["overall"]["direction"] == "up"


def test_channel_negative_blocks_even_when_overall_up():
    """整体显著上涨 + 其余渠道显著上涨，一个必验渠道显著负向 → 不可上线（翻不了案）。"""
    outcome = review_algorithm.evaluate(
        overall=_cell(20000, 2150, 20000, 2000),  # 10.0% → 10.75%，整体显著上涨
        channels=[
            {"channel": "feed", **_cell(5000, 425, 5000, 500)},  # 显著下滑
            {"channel": "push", **_up_cell()},
            {"channel": "search", **_up_cell()},
            {"channel": "mp", **_up_cell()},
        ],
        guardrails=[],
        alpha=0.05,
    )
    assert outcome["overall"]["direction"] == "up"
    feed = next(item for item in outcome["channels"] if item["channel"] == "feed")
    assert feed["direction"] == "down"
    assert outcome["verdict"] == "hold"
    assert any(item.startswith("channel:feed") for item in outcome["blockers"])


def test_guardrail_deterioration_blocks():
    outcome = review_algorithm.evaluate(
        overall=_up_cell(),
        channels=[{"channel": ch, **_up_cell()} for ch in CHANNELS],
        guardrails=[{"metric_key": "retention_d1", **_cell(8000, 2072, 8000, 2248)}],
        alpha=0.05,
    )
    assert outcome["verdict"] == "hold"
    assert any(item.startswith("guardrail:retention_d1") for item in outcome["blockers"])


def test_overall_not_up_blocks():
    flat = review_algorithm.evaluate(overall=_cell(5000, 510, 5000, 500), channels=[], guardrails=[], alpha=0.05)
    assert flat["overall"]["direction"] == "flat"
    assert flat["verdict"] == "hold"
    assert any(item.startswith("overall:") for item in flat["blockers"])

    down = review_algorithm.evaluate(overall=_cell(8000, 2072, 8000, 2248), channels=[], guardrails=[], alpha=0.05)
    assert down["overall"]["direction"] == "down"
    assert down["verdict"] == "hold"


def test_evaluate_deterministic():
    kwargs = {
        "overall": _up_cell(),
        "channels": [{"channel": ch, **_up_cell()} for ch in CHANNELS],
        "guardrails": [{"metric_key": "retention_d1", **_cell(5000, 1525, 5000, 1500)}],
        "alpha": 0.05,
    }
    assert review_algorithm.evaluate(**kwargs) == review_algorithm.evaluate(**kwargs)


def test_compare_rejects_bad_counts():
    with pytest.raises(ValueError):
        review_algorithm.compare(0, 0, 100, 10, 0.05)
    with pytest.raises(ValueError):
        review_algorithm.compare(100, 200, 100, 10, 0.05)


# ---------------------------------------------------------------- API 层
@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # 单连接共享，TestClient 工作线程与测试线程看到同一个库
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = session_factory()
    app = create_app()

    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    client = TestClient(app)
    yield client, db
    db.close()


def _token(role="admin"):
    email = {"admin": "admin@abcheck.io", "analyst": "analyst@abcheck.io", "viewer": "viewer@abcheck.io"}[role]
    token, _ = create_access_token(email, role)
    return {"Authorization": f"Bearer {token}"}


def _make_experiment(db, code, status="draft", primary="order_rate"):
    experiment = Experiment(code=code, name=f"测试实验 {code}", primary_metric=primary, status=status)
    db.add(experiment)
    db.flush()
    control = Variant(experiment_id=experiment.id, code="control", name="对照", ratio=0.5, is_control=True, sort_order=0)
    treat = Variant(experiment_id=experiment.id, code="treat", name="实验", ratio=0.5, is_control=False, sort_order=1)
    db.add_all([control, treat])
    db.flush()
    return experiment, control, treat


def _add_samples(db, experiment, variant, channel, metric, samples, conversions, day=date(2026, 7, 1)):
    db.add(
        Assignment(
            experiment_id=experiment.id,
            variant_id=variant.id,
            channel=channel,
            metric_key=metric,
            stat_date=day,
            samples=samples,
            conversions=conversions,
        )
    )
    db.flush()


def _fill_ship_data(db, experiment, control, treat):
    """整体显著上涨、四渠道全正、护栏平稳 → ship。"""
    for channel in CHANNELS:
        _add_samples(db, experiment, control, channel, "order_rate", 5000, 500)
        _add_samples(db, experiment, treat, channel, "order_rate", 5000, 575)
        _add_samples(db, experiment, control, channel, "retention_d1", 5000, 1500)
        _add_samples(db, experiment, treat, channel, "retention_d1", 5000, 1525)


def _fill_hold_data(db, experiment, control, treat):
    """整体显著上涨，但 feed 显著负向 → hold（挑好渠道也翻不了案）。"""
    for channel in CHANNELS:
        treat_conv = 425 if channel == "feed" else 575
        _add_samples(db, experiment, control, channel, "order_rate", 5000, 500)
        _add_samples(db, experiment, treat, channel, "order_rate", 5000, treat_conv)
        _add_samples(db, experiment, control, channel, "retention_d1", 5000, 1500)
        _add_samples(db, experiment, treat, channel, "retention_d1", 5000, 1500)


PLAN_PAYLOAD = {
    "guardrail_metrics": ["retention_d1"],
    "required_channels": CHANNELS,
    "alpha": 0.05,
}


def test_plan_registration_locked_after_start(env):
    client, db = env
    running, _, _ = _make_experiment(db, "EXP-T-RUN", status="running")
    db.commit()
    resp = client.put(f"/api/experiments/{running.id}/review-plan", json=PLAN_PAYLOAD, headers=_token())
    assert resp.status_code == 409
    assert "锁定" in resp.json()["detail"]["reason"]


def test_plan_registration_rbac_and_idempotent(env):
    client, db = env
    experiment, _, _ = _make_experiment(db, "EXP-T-DRAFT", status="draft")
    db.commit()

    assert client.put(f"/api/experiments/{experiment.id}/review-plan", json=PLAN_PAYLOAD).status_code == 401
    assert (
        client.put(f"/api/experiments/{experiment.id}/review-plan", json=PLAN_PAYLOAD, headers=_token("viewer")).status_code
        == 403
    )

    first = client.put(f"/api/experiments/{experiment.id}/review-plan", json=PLAN_PAYLOAD, headers=_token())
    assert first.status_code == 200
    assert first.json()["changed"] is True
    assert first.json()["plan"]["primary_metric"] == "order_rate"  # 缺省取实验主指标

    again = client.put(f"/api/experiments/{experiment.id}/review-plan", json=PLAN_PAYLOAD, headers=_token())
    assert again.json()["changed"] is False  # 同样内容重登不改动

    changed = client.put(
        f"/api/experiments/{experiment.id}/review-plan",
        json={**PLAN_PAYLOAD, "required_channels": ["feed"]},
        headers=_token(),
    )
    assert changed.json()["changed"] is True


def test_plan_rejects_extra_fields_and_bad_values(env):
    client, db = env
    experiment, _, _ = _make_experiment(db, "EXP-T-BAD", status="draft")
    db.commit()
    resp = client.put(
        f"/api/experiments/{experiment.id}/review-plan",
        json={**PLAN_PAYLOAD, "channels": ["feed"]},  # 多余字段
        headers=_token(),
    )
    assert resp.status_code == 422
    resp = client.put(
        f"/api/experiments/{experiment.id}/review-plan",
        json={**PLAN_PAYLOAD, "guardrail_metrics": ["order_rate"]},  # 护栏含主指标
        headers=_token(),
    )
    assert resp.status_code == 422


def test_run_requires_plan_and_samples(env):
    client, db = env
    experiment, control, treat = _make_experiment(db, "EXP-T-NOPLAN", status="running")
    db.commit()
    assert client.post(f"/api/experiments/{experiment.id}/review-runs", json={}, headers=_token()).status_code == 422


def _register_plan(client, experiment_id):
    resp = client.put(f"/api/experiments/{experiment_id}/review-plan", json=PLAN_PAYLOAD, headers=_token())
    assert resp.status_code == 200


def _prepare_running_experiment(db, client, code, fill):
    """完整生命周期：draft 登记口径 → 实验开始（口径锁定）→ 进样本。"""
    experiment, control, treat = _make_experiment(db, code, status="draft")
    db.commit()
    _register_plan(client, experiment.id)
    experiment.status = "running"
    fill(db, experiment, control, treat)
    db.commit()
    return experiment, control, treat


def test_run_idempotent_same_batch(env):
    """同一批数据重跑：不新增结果、不覆盖原结论。"""
    client, db = env
    experiment, control, treat = _prepare_running_experiment(db, client, "EXP-T-IDEM", _fill_ship_data)

    first = client.post(f"/api/experiments/{experiment.id}/review-runs", json={}, headers=_token())
    assert first.status_code == 201
    assert first.json()["reused"] is False
    assert first.json()["run"]["verdict"] == "ship"

    second = client.post(f"/api/experiments/{experiment.id}/review-runs", json={}, headers=_token())
    assert second.status_code == 200
    assert second.json()["reused"] is True
    assert second.json()["run"]["id"] == first.json()["run"]["id"]

    count = db.scalar(select(func.count()).select_from(ReviewRun).where(ReviewRun.experiment_id == experiment.id))
    assert count == 1  # 没有新增行

    # 新数据进来 → 新指纹 → 新结论
    _add_samples(db, experiment, treat, "feed", "order_rate", 5000, 520, day=date(2026, 7, 2))
    _add_samples(db, experiment, control, "feed", "order_rate", 5000, 500, day=date(2026, 7, 2))
    db.commit()
    third = client.post(f"/api/experiments/{experiment.id}/review-runs", json={}, headers=_token())
    assert third.status_code == 201
    assert third.json()["run"]["id"] != first.json()["run"]["id"]
    count = db.scalar(select(func.count()).select_from(ReviewRun).where(ReviewRun.experiment_id == experiment.id))
    assert count == 2


def test_run_rejects_channel_cherry_pick(env):
    """跑评审不接受渠道子集参数：挑选表现好的渠道也翻不了案。"""
    client, db = env
    experiment, control, treat = _prepare_running_experiment(db, client, "EXP-T-PICK", _fill_hold_data)

    resp = client.post(
        f"/api/experiments/{experiment.id}/review-runs",
        json={"channels": ["push", "search", "mp"]},
        headers=_token(),
    )
    assert resp.status_code == 422

    run = client.post(f"/api/experiments/{experiment.id}/review-runs", json={}, headers=_token()).json()["run"]
    assert run["overall"]["direction"] == "up"  # 整体确实显著上涨
    assert run["verdict"] == "hold"  # 但 feed 显著负向 → 不可上线
    assert any(item.startswith("channel:feed") for item in run["blockers"])


def test_release_hold_run_rejected(env):
    client, db = env
    experiment, control, treat = _prepare_running_experiment(db, client, "EXP-T-HOLD", _fill_hold_data)
    run = client.post(f"/api/experiments/{experiment.id}/review-runs", json={}, headers=_token()).json()["run"]

    resp = client.post(f"/api/experiments/{experiment.id}/review-releases", json={"run_id": run["id"]}, headers=_token())
    assert resp.status_code == 409
    state = client.get(f"/api/experiments/{experiment.id}/review", headers=_token("viewer")).json()
    assert state["release"] is None  # 没有发布记录


def test_release_atomic_switch_and_failure_keeps_old(env):
    """发布一次性切换；后续失败发布不改变已生效的旧版。"""
    client, db = env
    experiment, control, treat = _prepare_running_experiment(db, client, "EXP-T-REL", _fill_ship_data)

    run1 = client.post(f"/api/experiments/{experiment.id}/review-runs", json={}, headers=_token()).json()["run"]
    published = client.post(
        f"/api/experiments/{experiment.id}/review-releases", json={"run_id": run1["id"]}, headers=_token()
    )
    assert published.status_code == 200
    assert published.json()["switched"] is True
    assert published.json()["release"]["run_id"] == run1["id"]
    assert published.json()["release"]["verdict"] == "ship"

    # 重复发布同一依据：幂等空操作
    again = client.post(
        f"/api/experiments/{experiment.id}/review-releases", json={"run_id": run1["id"]}, headers=_token()
    )
    assert again.json()["switched"] is False

    # 新数据让结论转 hold：feed 显著负向
    _add_samples(db, experiment, treat, "feed", "order_rate", 5000, 300, day=date(2026, 7, 2))
    _add_samples(db, experiment, control, "feed", "order_rate", 5000, 500, day=date(2026, 7, 2))
    db.commit()
    run2 = client.post(f"/api/experiments/{experiment.id}/review-runs", json={}, headers=_token()).json()["run"]
    assert run2["verdict"] == "hold"

    # 用 hold 的新结论发布 → 409，已发布的旧版保留
    resp = client.post(f"/api/experiments/{experiment.id}/review-releases", json={"run_id": run2["id"]}, headers=_token())
    assert resp.status_code == 409
    state = client.get(f"/api/experiments/{experiment.id}/review", headers=_token()).json()
    assert state["release"]["run_id"] == run1["id"]

    # 拿旧的 ship 结论翻案 → 409（不是最新评审），旧版仍保留
    resp = client.post(f"/api/experiments/{experiment.id}/review-releases", json={"run_id": run1["id"]}, headers=_token())
    assert resp.status_code == 409
    state = client.get(f"/api/experiments/{experiment.id}/review", headers=_token()).json()
    assert state["release"]["run_id"] == run1["id"]


def test_release_unknown_run_404(env):
    client, db = env
    experiment, control, treat = _prepare_running_experiment(db, client, "EXP-T-404", _fill_ship_data)
    resp = client.post(f"/api/experiments/{experiment.id}/review-releases", json={"run_id": 9999}, headers=_token())
    assert resp.status_code == 404


def test_list_reviews_and_viewer_read(env):
    client, db = env
    experiment, control, treat = _prepare_running_experiment(db, client, "EXP-T-LIST", _fill_ship_data)
    client.post(f"/api/experiments/{experiment.id}/review-runs", json={}, headers=_token())

    resp = client.get("/api/reviews", headers=_token("viewer"))
    assert resp.status_code == 200
    item = next(row for row in resp.json()["items"] if row["experiment_code"] == "EXP-T-LIST")
    assert item["plan_registered"] is True
    assert item["latest_run"]["verdict"] == "ship"
    assert item["release"] is None
