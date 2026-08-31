"""The orchestrator's lifecycle endpoints, against the shared authority.

These replace an earlier set that ran against a per-process JSON registry. The
registry is gone: a second implementation of the roster is exactly the failure
it was supposed to guard against, since a process holding a stale copy can
believe a sleeve is live minutes after another process demoted it.

The promotion endpoint is the one worth reading closely. It takes artifact ids
and nothing else — there is no field in which a caller can assert that a
strategy is good.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_LIFECYCLE_POSTGRES_URL"),
    reason="set TEST_LIFECYCLE_POSTGRES_URL to run the lifecycle endpoint tests",
)

INTERNAL = {"X-Internal-Key": "test-internal"}
ADMIN = {**INTERNAL, "X-Admin-Key": "test-admin"}
BASE = "/v1/orchestrator/lifecycle"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def client(migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("LIFECYCLE_DATABASE_URL", migrated_db)
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "journal.db"))
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal")
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin")

    from journal import reset_journal

    reset_journal(None)
    from lifecycle.service import reset_lifecycle_service

    reset_lifecycle_service(None)

    from autonomy_orchestrator import main as orchestrator

    monkeypatch.setattr(orchestrator.state, "lifecycle", None)
    return TestClient(orchestrator.app)


def _walk_forward_artifact(store, symbol: str = "AAPL", **payload):
    body = {
        "deflated_sharpe_ratio": 0.97,
        "out_of_sample_trades": 45,
        "out_of_sample_return_pct": 0.08,
        "out_of_sample_sharpe": 1.4,
    }
    body.update(payload)
    return store.record_validation_artifact(
        kind="walk_forward", strategy_id="ema_rsi_macd", strategy_version="",
        symbol=symbol, environment="backtest",
        window_start=NOW - timedelta(days=60), window_end=NOW, payload=body,
    )


def _register(client: TestClient, symbol: str = "AAPL") -> None:
    response = client.post(
        f"{BASE}/register?strategy=ema_rsi_macd&symbol={symbol}", headers=INTERNAL
    )
    assert response.status_code == 200, response.text


class TestStatus:
    def test_an_empty_roster_reports_nothing_trading(self, client: TestClient) -> None:
        body = client.get(BASE).json()
        assert body["available"] is True
        assert body["trading"] == []

    def test_live_mode_is_off_by_default(self, client: TestClient) -> None:
        assert client.get(BASE).json()["live_mode_enabled"] is False

    def test_the_roster_shows_the_version_used_for_locking(
        self, client: TestClient
    ) -> None:
        _register(client)
        sleeve = client.get(BASE).json()["sleeves"][0]
        assert sleeve["state"] == "candidate"
        assert sleeve["version"] >= 1


class TestPromotionTakesNoPerformanceNumbers:
    def test_the_request_schema_rejects_a_smuggled_metric(
        self, client: TestClient
    ) -> None:
        """extra="forbid" on the body: there is no field to put a Sharpe ratio
        in, and inventing one is a 422 rather than a silently ignored key."""
        _register(client)
        response = client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=AAPL",
            headers=ADMIN,
            json={"deflated_sharpe_ratio": 0.99, "out_of_sample_trades": 500},
        )
        assert response.status_code == 422

    def test_no_artifact_is_refused_with_reasons(self, client: TestClient) -> None:
        _register(client)
        body = client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=AAPL",
            headers=ADMIN, json={"artifact_ids": []},
        ).json()
        assert body["promoted"] is False
        assert any("artifact" in f for f in body["failed"])

    def test_promotion_requires_the_admin_key(self, client: TestClient) -> None:
        _register(client)
        response = client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=AAPL",
            headers=INTERNAL, json={"artifact_ids": []},
        )
        assert response.status_code == 401

    def test_a_stored_artifact_promotes_to_paper(self, client: TestClient, store) -> None:
        _register(client)
        artifact = _walk_forward_artifact(store)
        body = client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=AAPL",
            headers=ADMIN, json={"artifact_ids": [artifact]},
        ).json()
        assert body["promoted"] is True
        assert body["state"] == "paper"

    def test_another_symbols_artifact_cannot_promote_this_sleeve(
        self, client: TestClient, store
    ) -> None:
        _register(client, "THIN")
        artifact = _walk_forward_artifact(store, symbol="AAPL")
        body = client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=THIN",
            headers=ADMIN, json={"artifact_ids": [artifact]},
        ).json()
        assert body["promoted"] is False
        assert any("is for AAPL" in f for f in body["failed"])

    def test_paper_cannot_be_skipped(self, client: TestClient, store) -> None:
        """A backtest cannot show what paper trading shows, so promotion is one
        step at a time and the live gates stay reachable."""
        _register(client)
        artifact = _walk_forward_artifact(store)
        first = client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=AAPL",
            headers=ADMIN, json={"artifact_ids": [artifact]},
        ).json()
        assert first["state"] == "paper"

        second = client.post(
            f"{BASE}/promote?strategy=ema_rsi_macd&symbol=AAPL",
            headers=ADMIN, json={"artifact_ids": [artifact]},
        ).json()
        assert second["promoted"] is False
        assert second["state"] == "paper"


class TestDemotionAndLiveMode:
    def test_demotion_needs_only_the_internal_key(self, client: TestClient) -> None:
        """Safety must not require the approval the admin key represents."""
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

    def test_live_mode_is_admin_gated_and_audited(self, client: TestClient) -> None:
        assert (
            client.post(f"{BASE}/live-mode?enabled=true", headers=INTERNAL).status_code
            == 401
        )
        body = client.post(
            f"{BASE}/live-mode?enabled=true&actor=jerry&reason=go", headers=ADMIN
        ).json()
        assert body["live_mode_enabled"] is True
        assert client.get(BASE).json()["live_mode_enabled"] is True

    def test_live_mode_can_be_pulled_back(self, client: TestClient) -> None:
        client.post(f"{BASE}/live-mode?enabled=true&actor=jerry", headers=ADMIN)
        client.post(f"{BASE}/live-mode?enabled=false&actor=jerry", headers=ADMIN)
        assert client.get(BASE).json()["live_mode_enabled"] is False


class TestHealthSweepEndpoint:
    def test_it_reports_not_run_before_the_first_sweep(self, client: TestClient) -> None:
        assert client.get("/v1/orchestrator/health-sweep").json() == {
            "status": "not_run_yet"
        }

    def test_running_it_by_hand_uses_the_scheduled_path(self, client: TestClient) -> None:
        body = client.post("/v1/orchestrator/health-sweep", headers=INTERNAL).json()
        assert "checked" in body
        assert client.get("/v1/orchestrator/health-sweep").json()["checked"] == body["checked"]

    def test_it_needs_the_internal_key(self, client: TestClient) -> None:
        assert client.post("/v1/orchestrator/health-sweep").status_code == 401
