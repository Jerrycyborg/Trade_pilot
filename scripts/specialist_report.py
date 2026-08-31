#!/usr/bin/env python3
"""Specialist assessments from the point-in-time archive. L1 and L2 of the roadmap.

Per docs/adr/0001 these phases produce **arguments, not proposals**. There is
no recommendation in the output and nothing downstream reads it to decide
anything.

The risk veto (L2) runs here too, and runs *first and independently*: it is
given the journal and a symbol, never the specialists' conclusions, so it
cannot be influenced by the arguments it exists to check separately. Its
refusal is reported above them and is not overridable from this command — a
veto with a `--force` flag is not a veto.

    uv run python scripts/specialist_report.py --symbols AAPL,MSFT
    uv run python scripts/specialist_report.py --symbols AAPL --as-of 2026-08-20T14:00:00Z
    uv run python scripts/specialist_report.py --symbols AAPL --json

Read the roles line first. Five roles are specified in the ADR; the number that
have a point-in-time archive to read is the finding this phase exists to
produce, and it is currently three.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from journal import get_journal
from specialists import build_report
from veto import review as veto_review

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
STANCE_COLOUR = {"bull": GREEN, "bear": RED, "neutral": DIM}


def _render(report: dict, vetoes: dict) -> None:
    roles = report["roles"]
    print(f"\n{'=' * 70}")
    print("  SPECIALIST ASSESSMENTS")
    print(f"{'=' * 70}")
    print(f"  As of                     {report['as_of']}")
    print(f"  Roles with an archive     {roles['with_an_archive']} of {roles['specified']}")

    repro = report["reproducibility"]
    if repro["all_reproducible"] is not None:
        mark = f"{GREEN}yes{RESET}" if repro["all_reproducible"] else f"{RED}NO{RESET}"
        print(f"  Reproducible              {mark}  ({repro['checked']} re-runs)")
        if not repro["all_reproducible"]:
            print(
                f"  {RED}A role gave different conclusions for the same moment. Every "
                f"historical\n  claim it made is unfalsifiable until that is fixed.{RESET}"
            )

    refused = {s: d for s, d in vetoes.items() if d.rejected}
    if refused:
        print(f"\n  {RED}Vetoed — the risk veto refuses these subjects:{RESET}")
        for symbol, decision in refused.items():
            for objection in decision.objections:
                print(f"    {symbol:<8} {objection.rule}: {objection.detail}")
        print(
            f"  {DIM}The veto formed these independently, without seeing any "
            f"specialist\n  claim. Its refusal is final within the loop.{RESET}"
        )

    unchecked = sorted({u for d in vetoes.values() for u in d.unchecked})
    if unchecked:
        print(f"\n  {YELLOW}The veto could not run every check:{RESET}")
        for item in unchecked:
            print(f"    {item}")

    if roles["blocked"]:
        print(f"\n  {YELLOW}Roles with no point-in-time archive:{RESET}")
        for role, info in roles["blocked"].items():
            print(f"    {role:<14} {info['reason']}")
            print(f"    {'':<14} {DIM}needs: {info['needed']}{RESET}")

    print(f"\n  {DIM}{roles['verdict']}{RESET}")

    for argument in report["arguments"]:
        symbol = argument["symbol"]
        decision = vetoes.get(symbol)
        banner = (
            f"  {symbol}  {RED}[VETOED]{RESET}"
            if decision is not None and decision.rejected
            else f"  {symbol}"
        )
        print(f"\n{'-' * 70}")
        print(banner)
        print(f"{'-' * 70}")
        if decision is not None and decision.rejected:
            # The arguments are still shown: L1 and L2 produce no action, so
            # hiding the analysis would only make the refusal harder to check.
            print(
                f"  {DIM}Shown for review only — the veto has refused this "
                f"subject.{RESET}"
            )
        if not argument["roles_reporting"]:
            print(f"  {DIM}no role could say anything about this symbol{RESET}")
            continue

        for stance in ("bull", "bear", "neutral"):
            for claim in argument[stance]:
                colour = STANCE_COLOUR[stance]
                measure = "" if claim["measure"] is None else f"{claim['measure']:>12.4f}"
                against = (
                    "" if claim["threshold"] is None else f" vs {claim['threshold']:g}"
                )
                print(f"  {colour}{stance:<8}{RESET}{claim['role']:<12}{claim['statement']}")
                if measure:
                    print(f"  {'':<20}{DIM}{measure.strip()}{against}{RESET}")
                for ref in claim["evidence"]:
                    print(f"  {'':<20}{DIM}from {ref['source']}: {ref['detail']}{RESET}")

        silent = argument["roles_silent"]
        if silent:
            print(f"\n  {DIM}silent: {', '.join(silent)}{RESET}")

    print(
        f"\n  {DIM}These are arguments about archived data, not recommendations. "
        f"Nothing\n  here is read by anything that places an order, and this phase "
        f"proposes\n  no change of any kind.{RESET}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="AAPL", help="comma-separated")
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO timestamp to reason as of; defaults to now. Reading a past "
             "moment uses only what the archive held then.",
    )
    parser.add_argument(
        "--timeframe",
        default="15m",
        help="bar cadence to read from the archive (e.g. 15m, 1d). Must name "
             "what was actually archived: the wrong slice reports a "
             "well-stocked archive as empty. The veto's gap detection reads "
             "its cadence from VETO_EXPECTED_INTERVAL_MINUTES and its "
             "freshness floor from VETO_MAX_STALE_MINUTES — set those to "
             "match, this flag does not silently retune the veto.",
    )
    parser.add_argument("--no-reproducibility-check", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    as_of = None
    if args.as_of:
        as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    report = build_report(
        get_journal(),
        symbols,
        as_of=as_of,
        check_reproducibility=not args.no_reproducibility_check,
        timeframe=args.timeframe,
    )

    # The veto runs on the journal and the symbol alone. It is never handed
    # `report`, so it cannot be influenced by the conclusions it exists to
    # check independently — and there is no flag to skip it.
    vetoes = {
        s: veto_review(get_journal(), s, as_of=as_of, timeframe=args.timeframe)
        for s in symbols
    }

    if args.json:
        print(
            json.dumps(
                {**report, "veto": {s: d.to_dict() for s, d in vetoes.items()}},
                indent=2,
                default=str,
            )
        )
    else:
        _render(report, vetoes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
