#!/usr/bin/env python3
"""Explain closed trades from the archive. L0 of the learning roadmap.

This answers one question, and deliberately only one: **when a trade went
wrong, do the recorded facts say why?** It is not a performance report — the
backtest and /v1/execution/quality already do that — and it produces no
recommendation. Per docs/adr/0001, nothing may propose a change until
attribution has shown the archive can explain outcomes at all.

    uv run python scripts/attribute_trades.py
    uv run python scripts/attribute_trades.py --environment paper --days 30
    uv run python scripts/attribute_trades.py --json > attribution.json

Read the coverage verdict first. A low number is not a failure of this command;
it is the finding, and the missing-field list is the work it implies.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from attribution import build_report
from journal import get_journal

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _money(value: float | None) -> str:
    if value is None:
        return "   n/a"
    colour = GREEN if value > 0 else RED if value < 0 else ""
    return f"{colour}{value:+,.2f}{RESET}"


def _render(report: dict) -> None:
    coverage = report["coverage"]
    totals = report["totals"]

    print(f"\n{'=' * 66}")
    print("  POST-TRADE ATTRIBUTION")
    print(f"{'=' * 66}")

    share = coverage["coverage"]
    print(f"  Closed round trips        {coverage['round_trips']}")
    print(
        f"  Fully attributable        {coverage['attributable']}"
        + (f"  ({share:.0%})" if share is not None else "")
    )
    if coverage["environments"]:
        print(f"  By environment            {coverage['environments']}")

    if coverage["missing_counts"]:
        print(f"\n  {YELLOW}Missing inputs — this is the work L0 implies:{RESET}")
        for field, count in coverage["missing_counts"].items():
            print(f"    {field:<28} missing on {count} trade(s)")

    if coverage["identity_failures"]:
        print(
            f"\n  {RED}{coverage['identity_failures']} attribution(s) did not add back "
            f"up to the realised result.{RESET}"
        )
        print(f"  {DIM}That is a bug in the decomposition, not a gap in the data.{RESET}")

    print(f"\n  {DIM}{coverage['verdict']}{RESET}")

    if totals.get("trades"):
        print(f"\n{'-' * 66}")
        print(f"  WHERE THE MONEY CAME FROM   ({totals['trades']} explainable trades)")
        print(f"{'-' * 66}")
        print(f"  From the signal            {_money(totals['from_signal'])}")
        print(f"  Entry execution            {_money(totals['from_entry_execution'])}")
        print(f"  Exit execution             {_money(totals['from_exit_execution'])}")
        print(f"  {DIM}{'-' * 42}{RESET}")
        print(f"  Realised                   {_money(totals['realized'])}")
        print(f"  Fees                       {_money(-totals['fees'])}")
        print(f"  Net of fees                {_money(totals['net_of_fees'])}")

        if not totals["identity_matches_realized"]:
            print(f"\n  {RED}The components do not sum to the realised total.{RESET}")

        capture = totals.get("mean_capture_ratio")
        if capture is not None:
            print(f"\n  Mean capture ratio         {capture:.0%}")
            print(
                f"  {DIM}Share of the best available move that was actually taken. "
                f"Nobody sells\n  the high, so 100% is not the target — but a "
                f"persistently low number\n  says the exit leaves trades early.{RESET}"
            )

        signal, execution = totals["from_signal"], totals["execution_cost_total"]
        if signal > 0 and execution < 0 and abs(execution) > signal * 0.5:
            print(
                f"\n  {YELLOW}! Execution took {abs(execution) / signal:.0%} of what the "
                f"signal earned.{RESET}"
            )

    if report["exit_reasons"]:
        print(f"\n  Exit reasons               {report['exit_reasons']}")

    print(
        f"\n  {DIM}Counterfactuals are computed against the archive as it stood when "
        f"each\n  trade closed, not the corrected series — a revision the system never "
        f"saw\n  must not decide that a different exit was better. They are questions "
        f"for\n  a later phase, not recommendations: this stage proposes nothing.{RESET}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--symbol", default=None)
    parser.add_argument(
        "--environment",
        choices=("paper", "live", "backtest"),
        default=None,
        help="omit to report on all, though pairing never crosses environments",
    )
    parser.add_argument("--account", default="default")
    parser.add_argument("--days", type=float, default=90.0)
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--no-counterfactuals", action="store_true")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    report = build_report(
        get_journal(),
        strategy_id=args.strategy,
        symbol=args.symbol,
        environment=args.environment,
        account_id=args.account,
        window_start=datetime.now(timezone.utc) - timedelta(days=args.days),
        timeframe=args.timeframe,
        with_counterfactuals=not args.no_counterfactuals,
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _render(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
