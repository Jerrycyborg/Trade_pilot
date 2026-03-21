from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

from contracts import AuditEvent, AuditLogResponse
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from .database import Base, SessionLocal, engine
from .models import AuditEventRecord


Base.metadata.create_all(bind=engine)
app = FastAPI(title="audit-logger", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "audit-logger"}


@app.post("/v1/audit/log", response_model=AuditLogResponse)
def create_log(event: AuditEvent) -> AuditLogResponse:
    event_id = event.event_id or str(uuid4())
    record = AuditEventRecord(
        event_id=event_id,
        event_type=event.event_type,
        symbol=event.symbol.upper() if event.symbol else None,
        signal_id=event.signal_id,
        decision=event.decision,
        reasoning=event.reasoning,
        metadata_json=json.dumps(event.metadata),
        timestamp=event.timestamp,
    )
    with SessionLocal() as session:
        session.add(record)
        session.commit()
        session.refresh(record)
        return _to_response(record)


@app.get("/v1/audit/logs", response_model=list[AuditLogResponse])
def list_logs(
    symbol: str | None = None,
    event_type: str | None = None,
    since: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[AuditLogResponse]:
    with SessionLocal() as session:
        statement = select(AuditEventRecord)
        if symbol:
            statement = statement.where(AuditEventRecord.symbol == symbol.upper())
        if event_type:
            statement = statement.where(AuditEventRecord.event_type == event_type)
        if since:
            statement = statement.where(AuditEventRecord.timestamp >= since)
        rows = session.scalars(statement.order_by(AuditEventRecord.timestamp.desc()).limit(limit)).all()
    return [_to_response(row) for row in rows]


@app.get("/v1/audit/logs/{event_id}", response_model=AuditLogResponse)
def get_log(event_id: str) -> AuditLogResponse:
    with SessionLocal() as session:
        row = session.scalar(select(AuditEventRecord).where(AuditEventRecord.event_id == event_id))
    if not row:
        raise HTTPException(status_code=404, detail="Audit event not found")
    return _to_response(row)


def _to_response(row: AuditEventRecord) -> AuditLogResponse:
    return AuditLogResponse(
        event_id=row.event_id,
        event_type=row.event_type,
        symbol=row.symbol,
        signal_id=row.signal_id,
        decision=row.decision,
        reasoning=row.reasoning,
        metadata=json.loads(row.metadata_json),
        timestamp=row.timestamp,
    )
