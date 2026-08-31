"""The veto itself.

Three properties the ADR asks for, and how each is arranged rather than
promised:

**Independent.** `review()` takes a subject and a journal. It has no parameter
for a specialist argument, so it cannot receive one, and it therefore cannot be
influenced by conclusions it was meant to check separately. "Does not see their
conclusions before forming its own" is a signature, not a discipline.

**Rejection only.** Every rule can add an objection and none can remove one.
There is no rule that clears another's finding, no scoring across objections
and no threshold at which several small ones become acceptable. A veto that can
be outvoted by its own leniency is not a veto.

**Final.** The decision is frozen and has no override. A caller that disagrees
has to make its own decision under its own name rather than editing this one.

The rules are all about whether the subject can be *reasoned about* — is the
data there, is it current, is the instrument actually tradeable. That is
deliberately narrow. A veto that also judged merit would be an approver with a
negative sign, and the whole point of separating it is that it is not one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .decision import Objection, VetoDecision
from .policy import POLICY_VERSION, VetoPolicy

logger = logging.getLogger(__name__)


def review(
    journal: Any,
    subject: str,
    as_of: datetime | None = None,
    policy: VetoPolicy | None = None,
    timeframe: str = "15m",
) -> VetoDecision:
    """Refuse, or find nothing to refuse. Never approve.

    `subject` is a symbol today. When L3 produces challengers it will be a
    challenger id, and the rules below stay the ones about whether the data
    supports reasoning at all — which is why they are written against the
    archive rather than against anything a proposal would contain.
    """
    moment = as_of or datetime.now(timezone.utc)
    active = policy or VetoPolicy.from_env()
    objections: list[Objection] = []
    unchecked: list[str] = []

    bars = _read(journal, "bars_as_of", subject, timeframe, moment)
    if bars is None:
        unchecked.append("history: the bar archive could not be read")
    elif len(bars) < active.min_archived_bars:
        objections.append(
            Objection(
                rule="insufficient_history",
                detail=(
                    f"{len(bars)} archived bars for {subject}; nothing downstream "
                    f"can rest on fewer than {active.min_archived_bars}"
                ),
                measure=float(len(bars)),
                threshold=float(active.min_archived_bars),
            )
        )

    # Staleness comes from the freshest bar actually held, not from the
    # windowed completeness view. Completeness reports stale_minutes=None when
    # the window contains no bars at all — which is precisely the case this
    # rule exists for. A symbol whose series stopped three days ago has an
    # empty 48-hour window, so reading staleness from there let the deadest
    # symbol in the archive pass with no objection at all.
    stale_minutes = _staleness_minutes(bars, moment)
    if stale_minutes is None:
        if bars is not None:
            unchecked.append("freshness: no dateable bar in the archive")
    elif stale_minutes > active.max_stale_minutes:
        objections.append(
            Objection(
                rule="stale_series",
                detail=(
                    f"the freshest bar for {subject} is {stale_minutes:.0f} "
                    f"minutes old; this is reasoning about a memory"
                ),
                measure=float(stale_minutes),
                threshold=active.max_stale_minutes,
            )
        )

    completeness = _completeness(journal, subject, timeframe, moment, active)
    if completeness is None or not completeness.get("available"):
        unchecked.append("coverage: completeness could not be computed")
    else:
        gap_count = completeness.get("gap_count")
        if gap_count is not None and gap_count > active.max_gap_count:
            objections.append(
                Objection(
                    rule="gapped_series",
                    detail=(
                        f"{gap_count} interior gaps in the last "
                        f"{active.window_hours:.0f}h of {subject}; the series "
                        f"cannot support a claim about continuity"
                    ),
                    measure=float(gap_count),
                    threshold=float(active.max_gap_count),
                )
            )

    executions = _read_executions(journal, subject, moment)
    if executions is None:
        unchecked.append("tradeability: execution records could not be read")
    else:
        rejected = sum(1 for row in executions if row.get("rejected"))
        if rejected > active.max_recent_rejected_orders:
            objections.append(
                Objection(
                    rule="orders_keep_being_rejected",
                    detail=(
                        f"{rejected} rejected orders for {subject} in the archive; "
                        f"something is wrong with the instrument, a permission or "
                        f"a halt, and research that ignores it is research about a "
                        f"symbol the system cannot trade"
                    ),
                    measure=float(rejected),
                    threshold=float(active.max_recent_rejected_orders),
                )
            )

    return VetoDecision(
        subject=subject,
        as_of=moment,
        policy_version=POLICY_VERSION,
        objections=tuple(objections),
        unchecked=tuple(unchecked),
    )


def _read(journal: Any, method: str, symbol: str, timeframe: str, moment: datetime):
    try:
        return list(getattr(journal, method)(symbol, timeframe, moment) or [])
    except Exception as exc:  # pragma: no cover - archives vary by deployment
        logger.debug("Veto read failed (%s): %s", method, exc)
        return None


def _completeness(
    journal: Any, symbol: str, timeframe: str, moment: datetime, policy: VetoPolicy
):
    try:
        return journal.completeness(
            symbol=symbol,
            timeframe=timeframe,
            window_start=moment - timedelta(hours=policy.window_hours),
            window_end=moment,
            expected_interval_minutes=policy.expected_interval_minutes,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("Veto completeness read failed: %s", exc)
        return None


def _read_executions(journal: Any, symbol: str, moment: datetime):
    try:
        return list(journal.execution_rows(symbol=symbol, window_end=moment) or [])
    except Exception as exc:  # pragma: no cover
        logger.debug("Veto execution read failed: %s", exc)
        return None


def _staleness_minutes(bars, moment: datetime) -> float | None:
    """How old the freshest archived bar is, from the bars themselves.

    Returns None when nothing is dateable, which the caller reports as an
    unchecked rule rather than as freshness. "We could not tell how old this
    is" and "this is current" must not produce the same silence.
    """
    if not bars:
        return None
    newest = None
    for bar in bars:
        raw = bar.get("bar_ts")
        stamp = raw if isinstance(raw, datetime) else None
        if stamp is None:
            try:
                stamp = datetime.fromisoformat(str(raw))
            except (TypeError, ValueError):
                continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if newest is None or stamp > newest:
            newest = stamp
    if newest is None:
        return None
    return round((moment - newest).total_seconds() / 60.0, 2)
