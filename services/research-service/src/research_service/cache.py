"""Research report cache — SQLAlchemy-backed with TTL expiry."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from contracts import ResearchReport
from sqlalchemy.orm import Session

from .config import settings
from .models import ResearchReportRecord


class ResearchCache:
    """Get and set cached research reports, keyed by symbol."""

    def get(self, symbol: str, session: Session) -> Optional[ResearchReport]:
        """Return a valid (unexpired) cached report, or None."""
        now = datetime.now(timezone.utc)
        record = (
            session.query(ResearchReportRecord)
            .filter(
                ResearchReportRecord.symbol == symbol.upper(),
                ResearchReportRecord.expires_at > now,
            )
            .order_by(ResearchReportRecord.generated_at.desc())
            .first()
        )
        if record is None:
            return None
        return _to_report(record, cached=True)

    def set(self, report: ResearchReport, session: Session) -> None:
        """Upsert a research report (delete old, insert new)."""
        # Delete any existing records for this symbol
        session.query(ResearchReportRecord).filter(
            ResearchReportRecord.symbol == report.symbol.upper()
        ).delete()

        expires_at = report.generated_at + timedelta(seconds=settings.cache_ttl_seconds)
        record = ResearchReportRecord(
            symbol=report.symbol.upper(),
            generated_at=report.generated_at,
            expires_at=expires_at,
            sentiment=report.sentiment,
            headline_summary=report.headline_summary,
            risk_factors_json=json.dumps(report.risk_factors),
            macro_context=report.macro_context,
            confidence_modifier=report.confidence_modifier,
            model_version=settings.claude_model,
        )
        session.add(record)


def _to_report(record: ResearchReportRecord, cached: bool = False) -> ResearchReport:
    risk_factors: list[str] = json.loads(record.risk_factors_json or "[]")
    return ResearchReport(
        symbol=record.symbol,
        generated_at=record.generated_at,
        sentiment=record.sentiment,
        headline_summary=record.headline_summary,
        risk_factors=risk_factors,
        macro_context=record.macro_context,
        confidence_modifier=record.confidence_modifier,
        cached=cached,
    )
