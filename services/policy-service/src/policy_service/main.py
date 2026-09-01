"""Policy service entrypoint."""

from __future__ import annotations

import logging

from contracts import PolicyDecision, PolicyEvaluationRecordResponse, PolicyEvaluationRequest
from contracts.auth import verify_internal_key
from fastapi import Depends, FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine
from .logging_utils import log_event
from .models import PolicyEvaluationRecord, PolicyRuleHitRecord
from .rules import evaluate_policy

logging.basicConfig(level=logging.INFO)


Base.metadata.create_all(bind=engine)
app = FastAPI(title="policy-service", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "policy-service"}


@app.post("/v1/policy/evaluate", response_model=PolicyDecision)
def evaluate(
    request: PolicyEvaluationRequest,
    _: None = Depends(verify_internal_key),
) -> PolicyDecision:
    """Evaluate a signal against deterministic policy rules."""

    decision, rule_hits = evaluate_policy(request)
    with SessionLocal() as session:
        _persist_decision(session, request, decision, rule_hits)
    log_event(
        "policy_evaluated",
        signal_id=request.signal_id,
        symbol=request.symbol,
        decision=decision.decision,
        reasons=decision.reasons,
    )
    return decision


@app.get("/v1/policy/evaluations", response_model=list[PolicyEvaluationRecordResponse])
def list_evaluations(
    limit: int = Query(default=20, ge=1, le=100),
    symbol: str | None = None,
    decision: str | None = None,
) -> list[PolicyEvaluationRecordResponse]:
    """Return persisted policy evaluations ordered newest-first."""

    with SessionLocal() as session:
        statement = select(PolicyEvaluationRecord)
        if symbol:
            statement = statement.where(PolicyEvaluationRecord.symbol == symbol.upper())
        if decision:
            statement = statement.where(PolicyEvaluationRecord.decision == decision.upper())
        rows = session.scalars(
            statement.order_by(PolicyEvaluationRecord.created_at.desc()).limit(limit)
        ).all()
        return [
            PolicyEvaluationRecordResponse(
                signal_id=row.signal_id,
                symbol=row.symbol,
                decision=row.decision,
                reasons=[reason for reason in row.reasons_json.split(",") if reason],
                approved_size_pct=row.approved_size_pct,
                policy_version=row.policy_version,
                created_at=row.created_at,
            )
            for row in rows
        ]


def _persist_decision(
    session: Session,
    request: PolicyEvaluationRequest,
    decision: PolicyDecision,
    rule_hits: list[str],
) -> None:
    record = PolicyEvaluationRecord(
        signal_id=request.signal_id,
        symbol=request.symbol,
        decision=decision.decision,
        approved_size_pct=decision.approved_size_pct,
        policy_version=decision.policy_version,
        reasons_json=",".join(decision.reasons),
    )
    session.add(record)
    session.flush()
    for rule_name in rule_hits:
        session.add(PolicyRuleHitRecord(evaluation_id=record.id, rule_name=rule_name))
    session.commit()
