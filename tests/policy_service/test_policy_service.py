from pathlib import Path

from contracts import PolicyEvaluationRequest


def _main(tmp_path: Path):
    import os

    os.environ.setdefault("POLICY_DISABLE_TRADING_HOURS", "true")
    db_file = tmp_path / "policy.db"
    import policy_service.config as config
    import policy_service.database as database
    import policy_service.main as main

    config.settings = config.PolicySettings(database_url=f"sqlite+pysqlite:///{db_file}")
    database.settings = config.settings
    database.connect_args = {"check_same_thread": False}
    database.engine = database.create_engine(
        config.settings.database_url, future=True, connect_args=database.connect_args
    )
    database.SessionLocal.configure(bind=database.engine)
    database.Base.metadata.create_all(bind=database.engine)
    main.engine = database.engine
    main.SessionLocal = database.SessionLocal
    return main


def _request(**overrides: object) -> PolicyEvaluationRequest:
    payload: dict[str, object] = {
        "signal_id": "sig-1",
        "symbol": "AAPL",
        "candidate_action": "BUY",
        "confidence": 0.8,
        "size_pct": 0.01,
        "market_context": {
            "data_age_seconds": 5,
            "market_open": True,
            "event_blackout_active": False,
            "liquidity_score": 0.9,
            "symbol_allowed": True,
        },
        "portfolio_context": {
            "gross_exposure_pct": 0.2,
            "daily_drawdown_pct": 0.01,
        },
    }
    payload.update(overrides)
    return PolicyEvaluationRequest(**payload)


def test_stale_data_rejection(tmp_path: Path) -> None:
    main = _main(tmp_path)
    response = main.evaluate(
        _request(market_context={**_request().market_context.model_dump(), "data_age_seconds": 45})
    )
    assert response.decision == "REJECT"
    assert "stale_data" in response.reasons


def test_max_size_rejection(tmp_path: Path) -> None:
    main = _main(tmp_path)
    response = main.evaluate(_request(size_pct=0.05))
    assert response.decision == "REJECT"
    assert "max_size_exceeded" in response.reasons


def test_confidence_review(tmp_path: Path) -> None:
    main = _main(tmp_path)
    response = main.evaluate(_request(confidence=0.4))
    assert response.decision == "REVIEW"
    assert "confidence_below_floor" in response.reasons


def test_approve_path(tmp_path: Path) -> None:
    main = _main(tmp_path)
    body = main.evaluate(_request())
    assert body.decision == "APPROVE"
    assert body.approved_size_pct == 0.01
    assert body.policy_version == "risk_policy_v1"


def test_list_evaluations_returns_newest_first_and_filters(tmp_path: Path) -> None:
    main = _main(tmp_path)
    approved = main.evaluate(_request(signal_id="sig-1", symbol="AAPL"))
    review = main.evaluate(_request(signal_id="sig-2", symbol="MSFT", confidence=0.4))
    rejected = main.evaluate(_request(signal_id="sig-3", symbol="AAPL", size_pct=0.05))

    assert approved.decision == "APPROVE"
    assert review.decision == "REVIEW"
    assert rejected.decision == "REJECT"

    listed = main.list_evaluations(limit=2)
    assert len(listed) == 2
    assert listed[0].signal_id == "sig-3"
    assert listed[1].signal_id == "sig-2"

    filtered = main.list_evaluations(limit=20, symbol="aapl", decision="reject")
    assert len(filtered) == 1
    assert filtered[0].signal_id == "sig-3"
    assert "max_size_exceeded" in filtered[0].reasons
