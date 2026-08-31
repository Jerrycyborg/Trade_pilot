"""HTTP tests for the lifecycle endpoints.

Written after a missing import shipped: every unit test passed because they
import the registry directly, and nothing exercised the routes. A control
surface nobody calls in a test is a control surface that can be broken without
anyone noticing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

INTERNAL = {"X-Internal-Key": "test-internal"}
ADMIN = {**INTERNAL, "X-Admin-Key": "test-admin"}
BASE = "/v1/orchestrator/lifecycle"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("LIFECYCLE_STATE_PATH", str(tmp_path / "lifecycle.json"))
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.db"))
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal")
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin")

    from journal import reset_journal

    reset_journal(None)

    from autonomy_orchestrator import main as orchestrator

    monkeypatch.setattr(orchestrator.state, "lifecycle", None)
    return TestClient(orchestrator.app)


def _backtest_evidence() -> dict[str, object]:
    return {
        "deflated_sharpe_ratio": 0.97,
        "out_of_sample_sharpe": 1.4,
        "out_of_sample_return_pct": 0.08,
        "out_of_sample_trades": 45,
    }


def _paper_evidence() -> dict[str, object]:
    return {
        **_backtest_evidence(),
        "paper_started_at": (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat(),
        "paper_decisions": 40,
        "measured_shortfall_bps": 2.5,
        "max_correlation_with_live": 0.2,
    }


def _register(client: TestClient) -> None:
    response = client.post(
        f"{BASE}/register?strategy=ema_rsi_macd&symbol=AAPL", headers=INTERNAL
    )
    assert response.status_code == 200


class TestStatus:
    def test_an_empty_roster_reports_nothing_trading(self, client: TestClient) -> None:
        body = client.get(BASE).json()
        assert body["counts"] == {}
        assert body["trading"] == []

    def test_the_roster_is_readable_without_a_key(self, client: TestClient) -> None:
        """Reading what is live is not a privileged action; changing it is."""
        assert client.get(BASE).status_code == 200


class TestRegister:
    def test_registering_creates_a_candidate(self, client: TestClient) -> None:
        body = client.post(
            f"{BASE}/register?strategy=ema_rsi_macd&symbol=aapl", headers=INTERNAL
        ).json()
        assert body["sleeve"] == "AAPL:ema_rsi_macd"
        assert body["state"] == "candidate"

    def test_registering_needs_the_internal_key(self, client: TestClient) -> None:
        assert client.post(f"{BASE}/register?strategy=ema_rsi_macd&symbol=AAPL").status_code == 401


class TestPromote:
    def test_promotion_requires_the_admin_key(self, client: TestClient) -> None:
        """The top of this ladder is real money."""
        _register(client)
        response = client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=AAPL",
            headers=INTERNAL,
            json=_backtest_evidence(),
        )
        assert response.status_code == 401

    def test_no_evidence_is_refused_with_reasons(self, client: TestClient) -> None:
        _register(client)
        body = client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=AAPL", headers=ADMIN, json={}
        ).json()
        assert body["promoted"] is False
        assert body["failed"]
        assert body["state"] == "candidate"

    def test_the_full_ladder_over_http(self, client: TestClient) -> None:
        _register(client)
        first = client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=AAPL",
            headers=ADMIN,
            json=_backtest_evidence(),
        ).json()
        assert first["state"] == "paper"

        second = client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=AAPL",
            headers=ADMIN,
            json=_paper_evidence(),
        ).json()
        assert second["state"] == "live"
        assert client.get(BASE).json()["trading"] == ["AAPL:ema_rsi_macd"]

    def test_promoting_an_unregistered_sleeve_is_refused(self, client: TestClient) -> None:
        body = client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=TSLA",
            headers=ADMIN,
            json=_backtest_evidence(),
        ).json()
        assert body["promoted"] is False


class TestDemote:
    def test_demotion_needs_only_the_internal_key(self, client: TestClient) -> None:
        """Safety must not require an approval the admin key represents."""
        _register(client)
        body = client.post(
            f"{BASE}/demote?strategy=ema_rsi_macd&symbol=AAPL&to=probation&reason=test",
            headers=INTERNAL,
        ).json()
        assert body["state"] == "probation"

    def test_an_unknown_state_is_rejected_with_the_alternatives(
        self, client: TestClient
    ) -> None:
        _register(client)
        response = client.post(
            f"{BASE}/demote?strategy=ema_rsi_macd&symbol=AAPL&to=banana", headers=INTERNAL
        )
        assert response.status_code == 422
        assert "probation" in response.json()["detail"]

    def test_demoting_an_unregistered_sleeve_is_a_404(self, client: TestClient) -> None:
        response = client.post(
            f"{BASE}/demote?strategy=ema_rsi_macd&symbol=TSLA&to=probation",
            headers=INTERNAL,
        )
        assert response.status_code == 404


class TestHealth:
    def test_a_breach_demotes_through_the_endpoint(self, client: TestClient) -> None:
        _register(client)
        client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=AAPL",
            headers=ADMIN, json=_backtest_evidence(),
        )
        client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=AAPL",
            headers=ADMIN, json=_paper_evidence(),
        )
        body = client.post(
            f"{BASE}/health?strategy=ema_rsi_macd&symbol=AAPL",
            headers=INTERNAL,
            json={**_paper_evidence(), "live_max_drawdown_pct": 0.30, "live_trades": 3},
        ).json()
        assert body["healthy"] is False
        assert body["state"] == "probation"
        assert client.get(BASE).json()["trading"] == []

    def test_a_healthy_sleeve_stays_live(self, client: TestClient) -> None:
        _register(client)
        client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=AAPL",
            headers=ADMIN, json=_backtest_evidence(),
        )
        client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=AAPL",
            headers=ADMIN, json=_paper_evidence(),
        )
        body = client.post(
            f"{BASE}/health?strategy=ema_rsi_macd&symbol=AAPL",
            headers=INTERNAL,
            json={**_paper_evidence(), "live_max_drawdown_pct": 0.04, "live_trades": 40,
                  "live_sharpe": 1.2},
        ).json()
        assert body["healthy"] is True
        assert body["state"] == "live"


class TestTransitionsAreAudited:
    def test_transitions_reach_the_decision_journal(self, client: TestClient) -> None:
        """The state says a sleeve is on probation; the journal says what the
        numbers were when that call was made."""
        _register(client)
        client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=AAPL",
            headers=ADMIN, json=_backtest_evidence(),
        )
        decisions = client.get("/v1/orchestrator/journal").json()["recent_decisions"]
        stages = {d["stage"] for d in decisions}
        assert "lifecycle" in stages
