"""Tests for ledger-vs-broker reconciliation.

Two behaviours carry the design: a transient mismatch (a fill in flight) must
not halt trading, and a persistent one must — but only for entries, never for
exits.
"""

from __future__ import annotations

import pytest
from autonomy_orchestrator.reconciliation import (
    Reconciler,
    compare_positions,
)


class TestComparePositions:
    def test_matching_views_produce_no_breaks(self) -> None:
        broker = [{"symbol": "AAPL", "qty": 10}]
        ledger = [{"symbol": "AAPL", "net_qty": 10}]
        assert compare_positions(broker, ledger) == []

    def test_broker_holds_something_we_do_not_know_about(self) -> None:
        breaks = compare_positions([{"symbol": "AAPL", "qty": 10}], [])
        assert len(breaks) == 1
        assert breaks[0].kind == "untracked_position"
        assert breaks[0].difference == 10

    def test_we_think_we_hold_something_the_broker_does_not(self) -> None:
        """The dangerous one: a stop is watching a position that isn't there."""
        breaks = compare_positions([], [{"symbol": "AAPL", "net_qty": 10}])
        assert breaks[0].kind == "phantom_position"
        assert breaks[0].difference == -10

    def test_quantity_mismatch_is_reported(self) -> None:
        breaks = compare_positions(
            [{"symbol": "AAPL", "qty": 7}], [{"symbol": "AAPL", "net_qty": 10}]
        )
        assert breaks[0].kind == "quantity_mismatch"
        assert breaks[0].difference == -3

    def test_floating_point_noise_is_not_a_break(self) -> None:
        breaks = compare_positions(
            [{"symbol": "AAPL", "qty": 10.0000000001}],
            [{"symbol": "AAPL", "net_qty": 10.0}],
        )
        assert breaks == []

    def test_symbols_are_compared_case_insensitively(self) -> None:
        assert compare_positions(
            [{"symbol": "aapl", "qty": 5}], [{"symbol": "AAPL", "net_qty": 5}]
        ) == []

    def test_multiple_breaks_are_all_reported_and_sorted(self) -> None:
        breaks = compare_positions(
            [{"symbol": "MSFT", "qty": 3}, {"symbol": "AAPL", "qty": 1}],
            [{"symbol": "NVDA", "net_qty": 2}],
        )
        assert [b.symbol for b in breaks] == ["AAPL", "MSFT", "NVDA"]

    def test_shorts_are_compared_by_sign(self) -> None:
        """A short at the broker matched against a long in the ledger is a
        break, not a match on magnitude."""
        breaks = compare_positions(
            [{"symbol": "AAPL", "qty": -10}], [{"symbol": "AAPL", "net_qty": 10}]
        )
        assert breaks[0].difference == -20

    def test_unparseable_quantities_are_treated_as_zero(self) -> None:
        breaks = compare_positions([{"symbol": "AAPL", "qty": "junk"}], [])
        assert breaks == []


class _Reconciler(Reconciler):
    """Reconciler with the HTTP layer replaced by canned responses."""

    def __init__(self, broker, ledger, breaks_before_halt=2, fail=False):
        super().__init__("http://exec", "http://pf", "k", breaks_before_halt)
        self.broker, self.ledger, self.fail = broker, ledger, fail

    async def _fetch(self, url: str):
        if self.fail:
            raise RuntimeError("service unreachable")
        return self.broker if "/v1/positions" in url else self.ledger


class TestHaltBehaviour:
    @pytest.mark.asyncio
    async def test_a_single_mismatch_does_not_halt(self) -> None:
        """A fill in flight legitimately appears at the broker first. Halting on
        one check would stop trading on every ordinary execution."""
        r = _Reconciler([{"symbol": "AAPL", "qty": 10}], [])
        result = await r.check()

        assert result.breaks
        assert result.halted is False
        assert r.entries_blocked is False

    @pytest.mark.asyncio
    async def test_a_persistent_mismatch_halts_entries(self) -> None:
        r = _Reconciler([{"symbol": "AAPL", "qty": 10}], [])
        await r.check()
        result = await r.check()

        assert result.halted is True
        assert r.entries_blocked is True
        assert result.consecutive_breaks == 2

    @pytest.mark.asyncio
    async def test_a_resolved_break_clears_the_halt(self) -> None:
        r = _Reconciler([{"symbol": "AAPL", "qty": 10}], [])
        await r.check()
        await r.check()
        assert r.entries_blocked is True

        r.ledger = [{"symbol": "AAPL", "net_qty": 10}]
        result = await r.check()

        assert result.ok is True
        assert r.entries_blocked is False

    @pytest.mark.asyncio
    async def test_an_unreachable_service_is_not_a_divergence(self) -> None:
        """Otherwise every container restart would halt trading."""
        r = _Reconciler([], [], fail=True)
        for _ in range(5):
            result = await r.check()

        assert result.ok is False
        assert result.error is not None
        assert result.breaks == []
        assert r.entries_blocked is False

    @pytest.mark.asyncio
    async def test_the_halt_threshold_is_configurable(self) -> None:
        r = _Reconciler([{"symbol": "AAPL", "qty": 10}], [], breaks_before_halt=1)
        assert (await r.check()).halted is True

    @pytest.mark.asyncio
    async def test_result_serialises_for_the_operator_endpoint(self) -> None:
        r = _Reconciler([{"symbol": "AAPL", "qty": 10}], [])
        payload = (await r.check()).to_dict()

        assert payload["ok"] is False
        assert payload["breaks"][0]["symbol"] == "AAPL"
        assert payload["breaks"][0]["kind"] == "untracked_position"
        assert payload["broker_symbols"] == 1

    @pytest.mark.asyncio
    async def test_reset_clears_state(self) -> None:
        r = _Reconciler([{"symbol": "AAPL", "qty": 10}], [])
        await r.check()
        await r.check()
        r.reset()

        assert r.entries_blocked is False
        assert r.last_result is None
