"""Policy service entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from contracts import PolicyDecision, PolicyEvaluationRequest
from fastapi import FastAPI
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine
from .logging_utils import log_event
from .models import PolicyEvaluationRecord, PolicyRuleHitRecord
from .rules import evaluate_policy

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="policy-service", version="0.1.0", lifespan=lifespan)


@app.post("/v1/policy/evaluate", response_model=PolicyDecision)
def evaluate(request: PolicyEvaluationRequest) -> PolicyDecision:
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
