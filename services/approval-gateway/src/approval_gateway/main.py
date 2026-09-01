from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import yaml
from contracts import ApprovalRequest, ApprovalResponse
from contracts.auth import verify_internal_key
from contracts.cors import cors_origins
from contracts.rate_limit import rate_limit_write
from contracts.sanitize import sanitize_symbol, validate_positive_amount
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from .config import settings
from .database import Base, SessionLocal, engine
from .models import ApprovalRecord

Base.metadata.create_all(bind=engine)
app = FastAPI(title="approval-gateway", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=[
        "Content-Type",
        "X-Internal-Key",
        "X-Admin-Key",
        "Idempotency-Key",
    ],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "approval-gateway"}


@app.post("/v1/approvals", response_model=ApprovalResponse)
def create_approval(
    approval_request: ApprovalRequest,
    request: Request,
    _: None = Depends(verify_internal_key),
    _rl: None = Depends(rate_limit_write),
) -> ApprovalResponse:
    approval_request.symbol = sanitize_symbol(approval_request.symbol)
    approval_request.amount_usd = validate_positive_amount(
        approval_request.amount_usd, "amount_usd"
    )
    timeout_minutes = _tier2_timeout_minutes()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=timeout_minutes)
        if approval_request.tier == 2
        else None
    )
    record = ApprovalRecord(
        approval_id=str(uuid4()),
        signal_id=approval_request.signal_id,
        symbol=approval_request.symbol.upper(),
        action=approval_request.action.upper(),
        amount_usd=approval_request.amount_usd,
        tier=approval_request.tier,
        reason=approval_request.reason,
        metadata_json=json.dumps(approval_request.metadata),
        status="PENDING",
        expires_at=expires_at,
    )
    with SessionLocal() as session:
        session.add(record)
        session.commit()
    return _to_response(record)


@app.get("/v1/approvals/pending", response_model=list[ApprovalResponse])
def list_pending(status: str | None = Query(default="PENDING")) -> list[ApprovalResponse]:
    _expire_soft_timeouts()
    with SessionLocal() as session:
        statement = select(ApprovalRecord)
        if status:
            statement = statement.where(ApprovalRecord.status == status.upper())
        rows = session.scalars(statement.order_by(ApprovalRecord.created_at.desc())).all()
    return [_to_response(row) for row in rows]


@app.get("/v1/approvals/{approval_id}", response_model=ApprovalResponse)
def get_approval(approval_id: str) -> ApprovalResponse:
    _expire_soft_timeouts()
    with SessionLocal() as session:
        row = session.scalar(
            select(ApprovalRecord).where(ApprovalRecord.approval_id == approval_id)
        )
    if not row:
        raise HTTPException(status_code=404, detail="Approval not found")
    return _to_response(row)


@app.post("/v1/approvals/{approval_id}/approve", response_model=ApprovalResponse)
def approve(
    approval_id: str,
    request: Request,
    _: None = Depends(verify_internal_key),
    _rl: None = Depends(rate_limit_write),
) -> ApprovalResponse:
    return _transition(approval_id, "APPROVED")


@app.post("/v1/approvals/{approval_id}/reject", response_model=ApprovalResponse)
def reject(
    approval_id: str,
    request: Request,
    _: None = Depends(verify_internal_key),
    _rl: None = Depends(rate_limit_write),
) -> ApprovalResponse:
    return _transition(approval_id, "REJECTED")


def _transition(approval_id: str, status: str) -> ApprovalResponse:
    _expire_soft_timeouts()
    with SessionLocal() as session:
        row = session.scalar(
            select(ApprovalRecord).where(ApprovalRecord.approval_id == approval_id)
        )
        if not row:
            raise HTTPException(status_code=404, detail="Approval not found")
        if row.status != "PENDING":
            return _to_response(row)
        row.status = status
        row.reviewed_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(row)
    return _to_response(row)


def _expire_soft_timeouts() -> None:
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        rows = session.scalars(
            select(ApprovalRecord).where(
                ApprovalRecord.status == "PENDING",
                ApprovalRecord.expires_at.is_not(None),
                ApprovalRecord.expires_at <= now,
            )
        ).all()
        for row in rows:
            row.status = "EXPIRED"
            row.reviewed_at = now
        session.commit()


def _tier2_timeout_minutes() -> int:
    payload = yaml.safe_load(settings.policy_config_path.read_text(encoding="utf-8")) or {}
    return int(payload.get("approval_tiers", {}).get("tier2_soft_timeout_minutes", 15))


def _to_response(row: ApprovalRecord) -> ApprovalResponse:
    return ApprovalResponse(
        approval_id=row.approval_id,
        signal_id=row.signal_id,
        symbol=row.symbol,
        action=row.action,
        amount_usd=row.amount_usd,
        tier=row.tier,
        reason=row.reason,
        metadata=json.loads(row.metadata_json),
        status=row.status,
        created_at=row.created_at,
        expires_at=row.expires_at,
        reviewed_at=row.reviewed_at,
    )
