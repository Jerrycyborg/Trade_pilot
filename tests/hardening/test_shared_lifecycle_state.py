"""The lifecycle authority is shared, transactional and durable.

Acceptance criteria 1, 2 and 3. Each of these covers a defect that was
confirmed by probe on the JSON registry before this store was written:

1. two processes never saw each other's transitions;
2. a failed write still reported success;
3. nothing survived a restart.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_LIFECYCLE_POSTGRES_URL"),
    reason=(
        "set TEST_LIFECYCLE_POSTGRES_URL to run the shared-state tests; the "
        "lifecycle authority is PostgreSQL and these prove concurrency and "
        "persistence, which a SQLite substitute cannot"
    ),
)


def _store(url: str):
    from lifecycle.store import PostgresLifecycleStore, StoreSettings

    return PostgresLifecycleStore(StoreSettings(url=url))


class TestTwoProcessesShareState:
    """Acceptance criterion 1."""

    def test_a_transition_is_visible_to_another_process_immediately(
        self, migrated_db: str
    ) -> None:
        a, b = _store(migrated_db), _store(migrated_db)
        sleeve = a.register("ema_rsi_macd", "AAPL")
        assert b.get("ema_rsi_macd", "AAPL").state == "candidate"

        a.transition(sleeve, "paper", "evidence")
        assert b.get("ema_rsi_macd", "AAPL").state == "paper", (
            "the second process must not be reading a cached roster"
        )

    def test_a_demotion_is_visible_immediately(self, migrated_db: str) -> None:
        """The direction that matters most: a process must not keep trading a
        sleeve another process just pulled."""
        a, b = _store(migrated_db), _store(migrated_db)
        sleeve = a.register("ema_rsi_macd", "AAPL")
        sleeve = a.transition(sleeve, "paper", "evidence")
        sleeve = a.transition(sleeve, "live", "evidence")
        assert b.get("ema_rsi_macd", "AAPL").state == "live"

        a.transition(sleeve, "probation", "drawdown breach")
        assert b.get("ema_rsi_macd", "AAPL").state == "probation"

    def test_registration_in_one_process_is_seen_by_another(
        self, migrated_db: str
    ) -> None:
        a, b = _store(migrated_db), _store(migrated_db)
        assert b.get("ema_rsi_macd", "MSFT") is None
        a.register("ema_rsi_macd", "MSFT")
        assert b.get("ema_rsi_macd", "MSFT") is not None


class TestFailedWritesCannotChangeState:
    """Acceptance criterion 2."""

    def test_a_concurrent_transition_is_rejected_not_merged(
        self, migrated_db: str
    ) -> None:
        from lifecycle.store import ConcurrentTransitionError

        a, b = _store(migrated_db), _store(migrated_db)
        a.register("ema_rsi_macd", "AAPL")

        stale = b.get("ema_rsi_macd", "AAPL")
        fresh = a.get("ema_rsi_macd", "AAPL")
        a.transition(fresh, "paper", "A won")

        with pytest.raises(ConcurrentTransitionError):
            b.transition(stale, "retired", "B lost")

        assert a.get("ema_rsi_macd", "AAPL").state == "paper", (
            "the loser's write must not have landed"
        )

    def test_a_rejected_transition_writes_no_history(self, migrated_db: str) -> None:
        from lifecycle.store import ConcurrentTransitionError

        a, b = _store(migrated_db), _store(migrated_db)
        sleeve = a.register("ema_rsi_macd", "AAPL")
        stale = b.get("ema_rsi_macd", "AAPL")
        a.transition(a.get("ema_rsi_macd", "AAPL"), "paper", "A won")

        before = len(a.transitions(sleeve.id))
        with pytest.raises(ConcurrentTransitionError):
            b.transition(stale, "retired", "B lost")
        assert len(a.transitions(sleeve.id)) == before

    def test_a_database_error_raises_rather_than_reporting_success(
        self, migrated_db: str
    ) -> None:
        """The JSON registry logged the failure and returned the new state."""
        store = _store(migrated_db)
        sleeve = store.register("ema_rsi_macd", "AAPL")

        engine = create_engine(migrated_db, future=True)
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE lifecycle.transition CASCADE")

        # SQLAlchemy wraps the missing table; the point is that it propagates
        # rather than being logged and discarded.
        with pytest.raises(SQLAlchemyError):
            store.transition(sleeve, "paper", "should not survive")

        # The sleeve row is unchanged: the transition ran in one transaction.
        with engine.connect() as conn:
            state = conn.execute(
                text("SELECT state FROM lifecycle.sleeve WHERE id = :i"), {"i": sleeve.id}
            ).scalar()
        assert state == "candidate"

    def test_an_unknown_state_is_refused(self, store) -> None:
        from lifecycle.store import LifecycleStoreError

        sleeve = store.register("ema_rsi_macd", "AAPL")
        with pytest.raises(LifecycleStoreError):
            store.transition(sleeve, "supercharged", "no such state")


class TestRestartPreservesEverything:
    """Acceptance criterion 3."""

    def test_lifecycle_state_survives(self, migrated_db: str) -> None:
        first = _store(migrated_db)
        sleeve = first.register("ema_rsi_macd", "AAPL")
        first.transition(sleeve, "paper", "evidence")

        restarted = _store(migrated_db)
        assert restarted.get("ema_rsi_macd", "AAPL").state == "paper"

    def test_a_demotion_survives(self, migrated_db: str) -> None:
        first = _store(migrated_db)
        sleeve = first.register("ema_rsi_macd", "AAPL")
        sleeve = first.transition(sleeve, "paper", "evidence")
        sleeve = first.transition(sleeve, "live", "evidence")
        first.transition(sleeve, "probation", "breach")

        restarted = _store(migrated_db)
        after = restarted.get("ema_rsi_macd", "AAPL")
        assert after.state == "probation"
        assert after.probation_count == 1

    def test_a_reconciliation_halt_survives(self, migrated_db: str) -> None:
        first = _store(migrated_db)
        first.record_reconciliation(broker="paper", environment="paper", ok=False, breaks=1)
        first.record_reconciliation(broker="paper", environment="paper", ok=False, breaks=1)
        assert first.reconciliation_state("paper", "paper").halted is True

        restarted = _store(migrated_db)
        assert restarted.reconciliation_state("paper", "paper").halted is True

    def test_live_mode_survives_and_defaults_off(self, migrated_db: str) -> None:
        first = _store(migrated_db)
        assert first.live_mode_enabled() is False, "real money must be off by default"
        first.set_live_mode(True, actor="operator", reason="test")
        assert _store(migrated_db).live_mode_enabled() is True

    def test_the_transition_log_survives(self, migrated_db: str) -> None:
        first = _store(migrated_db)
        sleeve = first.register("ema_rsi_macd", "AAPL")
        first.transition(sleeve, "paper", "evidence")

        history = _store(migrated_db).transitions(sleeve.id)
        assert [h["to"] for h in reversed(history)] == ["candidate", "paper"]


class TestRegistration:
    def test_registration_is_idempotent(self, store) -> None:
        first = store.register("ema_rsi_macd", "AAPL")
        store.transition(first, "paper", "evidence")
        again = store.register("ema_rsi_macd", "AAPL")
        assert again.state == "paper", "re-registering must not reset progress"

    def test_re_registering_a_retired_sleeve_keeps_its_record(self, store) -> None:
        sleeve = store.register("ema_rsi_macd", "AAPL")
        sleeve = store.transition(sleeve, "paper", "e")
        sleeve = store.transition(sleeve, "live", "e")
        sleeve = store.transition(sleeve, "probation", "broke")
        sleeve = store.transition(sleeve, "retired", "done")

        back = store.register("ema_rsi_macd", "AAPL")
        assert back.state == "candidate"
        assert back.probation_count == 1, "coming back must not launder the record"

    def test_symbols_are_normalised(self, store) -> None:
        store.register("ema_rsi_macd", "aapl")
        assert store.get("ema_rsi_macd", "AAPL") is not None

    def test_entering_paper_sets_the_position_environment(self, store) -> None:
        """So a later reduce-only exit knows which broker to reach."""
        sleeve = store.register("ema_rsi_macd", "AAPL")
        assert sleeve.position_environment == "none"
        sleeve = store.transition(sleeve, "paper", "e")
        assert sleeve.position_environment == "simulated"

    def test_a_demotion_keeps_the_position_environment(self, store) -> None:
        sleeve = store.register("ema_rsi_macd", "AAPL")
        sleeve = store.transition(sleeve, "paper", "e")
        sleeve = store.transition(sleeve, "live", "e")
        assert sleeve.position_environment == "live"
        sleeve = store.transition(sleeve, "probation", "breach")
        assert sleeve.position_environment == "live", (
            "a demoted sleeve still holds real positions and must be able to exit them"
        )
