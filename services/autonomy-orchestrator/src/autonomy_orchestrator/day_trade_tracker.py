"""Pattern day trader (PDT) tracking and entry gating.

**Jurisdiction.** This implements the US FINRA rule: an account that executes
four or more *day trades* within five rolling business days, and holds less than
$25,000 in equity, is designated a pattern day trader and restricted — typically
to closing transactions only until the account is funded above the threshold.
A day trade is buying and selling the same security in the same session.

The rule applies to US margin accounts. Cash accounts are not subject to it but
face settlement constraints instead, and non-US brokers have their own regimes.
Set ``PDT_ENABLED=false`` if it does not apply to you — but confirm with your
broker rather than assuming, because being flagged is disruptive to undo.

**Why entries are gated, not exits.** The trade that *creates* a day trade is
the closing one, but blocking a close would mean holding a losing position past
its stop to avoid a compliance flag. That trades a regulatory problem for a
financial one. So the guard refuses to *open* a position when closing it the
same session would breach the limit, and never blocks an exit.

State is persisted because the window spans five business days and must survive
restarts.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import zoneinfo
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MARKET_TZ = zoneinfo.ZoneInfo("America/New_York")

# FINRA: four day trades in five business days triggers the designation, so
# three is the most that can be taken safely.
FINRA_DAY_TRADE_LIMIT = 4
FINRA_EQUITY_THRESHOLD_USD = 25_000.0
ROLLING_SESSIONS = 5


def business_window(today: date, sessions: int = ROLLING_SESSIONS) -> set[str]:
    """The `sessions` most recent business days ending on `today`, inclusive.

    `today` is always included, even on a weekend: crypto trades every day, and
    a window that silently drops today's activity would undercount.

    Weekday-based: market holidays are not known here, so during a holiday week
    the window covers one fewer real trading day than the rule intends and a day
    trade can expire a session early. Set PDT_MAX_DAY_TRADES=2 for margin
    against that.
    """
    dates: list[str] = [today.isoformat()]
    cursor = today - timedelta(days=1)
    while len(dates) < sessions:
        if cursor.weekday() < 5:
            dates.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return set(dates)


def session_date(stamp: datetime | None = None) -> str:
    """The US trading session a moment belongs to, in market time."""
    moment = stamp or datetime.now(timezone.utc)
    aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return aware.astimezone(MARKET_TZ).date().isoformat()


@dataclass(frozen=True)
class PDTSettings:
    enabled: bool = True
    equity_threshold_usd: float = FINRA_EQUITY_THRESHOLD_USD
    max_day_trades: int = FINRA_DAY_TRADE_LIMIT - 1
    state_path: Path = Path("./day-trade-state.json")

    @classmethod
    def from_env(cls) -> "PDTSettings":
        return cls(
            enabled=os.getenv("PDT_ENABLED", "true").lower() == "true",
            equity_threshold_usd=float(
                os.getenv("PDT_EQUITY_THRESHOLD_USD", str(FINRA_EQUITY_THRESHOLD_USD))
            ),
            max_day_trades=int(
                os.getenv("PDT_MAX_DAY_TRADES", str(FINRA_DAY_TRADE_LIMIT - 1))
            ),
            state_path=Path(os.getenv("PDT_STATE_PATH", "./day-trade-state.json")),
        )


@dataclass(frozen=True)
class PDTDecision:
    allowed: bool
    reason: str
    day_trades_used: int = 0
    day_trades_remaining: int = 0
    equity: float | None = None
    open_today: int = 0
    """Positions opened this session and still open. Each becomes a day trade
    if it closes today, so each must be reserved against the budget."""


class DayTradeTracker:
    """Records opens and closes, and answers whether a new entry is safe."""

    def __init__(self, settings: PDTSettings | None = None) -> None:
        self._settings = settings or PDTSettings.from_env()
        self._lock = threading.Lock()
        # symbol -> session date on which the currently open position was entered
        self._open_sessions: dict[str, str] = {}
        # session date -> number of day trades closed that session
        self._day_trades: dict[str, int] = {}
        self._load()

    @property
    def settings(self) -> PDTSettings:
        return self._settings

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def record_open(self, symbol: str, stamp: datetime | None = None) -> None:
        """Note that a position was opened, so a same-session close counts."""
        with self._lock:
            self._open_sessions.setdefault(symbol.upper(), session_date(stamp))
            self._save()

    def record_close(self, symbol: str, stamp: datetime | None = None) -> bool:
        """Note a close. Returns True if it completed a day trade."""
        key = symbol.upper()
        closed_on = session_date(stamp)
        with self._lock:
            opened_on = self._open_sessions.pop(key, None)
            is_day_trade = opened_on is not None and opened_on == closed_on
            if is_day_trade:
                self._day_trades[closed_on] = self._day_trades.get(closed_on, 0) + 1
                logger.info(
                    "Day trade recorded for %s (%d in the last %d sessions)",
                    key,
                    self._count_locked(stamp),
                    ROLLING_SESSIONS,
                )
            self._prune_locked(stamp)
            self._save()
        return is_day_trade

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------
    def day_trades_used(self, now: datetime | None = None) -> int:
        """Day trades inside the rolling 5-business-day window."""
        with self._lock:
            return self._count_locked(now)

    def open_today(self, now: datetime | None = None) -> int:
        """Positions opened in the current session that are still open."""
        today = session_date(now)
        with self._lock:
            return sum(1 for opened in self._open_sessions.values() if opened == today)

    def check_entry(
        self, equity: float | None, now: datetime | None = None
    ) -> PDTDecision:
        """Whether opening a new position is safe under the PDT rule.

        ``equity`` of None means the account value could not be read. The guard
        then assumes the account is *below* the threshold, because approving on
        an unknown balance is how an account gets flagged.
        """
        if not self._settings.enabled:
            return PDTDecision(True, "pdt_disabled", equity=equity)

        used = self.day_trades_used(now)
        # A position opened today becomes a day trade the moment it closes
        # today — and it may have to, on a stop. Counting only completed day
        # trades would let the guard approve an entry it cannot safely exit,
        # which is the one guarantee it exists to provide.
        open_today = self.open_today(now)
        committed = used + open_today
        remaining = max(0, self._settings.max_day_trades - committed)

        if equity is not None and equity >= self._settings.equity_threshold_usd:
            return PDTDecision(
                True, "above_pdt_equity_threshold", used, remaining, equity, open_today
            )

        if equity is None:
            logger.warning(
                "PDT check: account equity unknown — assuming below the "
                "$%.0f threshold", self._settings.equity_threshold_usd
            )

        if committed >= self._settings.max_day_trades:
            detail = (
                f"{used} taken"
                + (f" + {open_today} open today" if open_today else "")
                + f" of {self._settings.max_day_trades} in {ROLLING_SESSIONS} sessions"
            )
            return PDTDecision(
                False,
                f"pdt_day_trade_limit ({detail})",
                used,
                0,
                equity,
                open_today,
            )
        return PDTDecision(
            True, "within_day_trade_budget", used, remaining, equity, open_today
        )

    def status(self, now: datetime | None = None) -> dict[str, object]:
        with self._lock:
            recent = dict(sorted(self._day_trades.items()))
            used = self._count_locked(now)
            open_positions = dict(self._open_sessions)
        today = session_date(now)
        open_today = sum(1 for opened in open_positions.values() if opened == today)
        return {
            "enabled": self._settings.enabled,
            "day_trades_used": used,
            "open_today": open_today,
            "committed": used + open_today,
            "max_day_trades": self._settings.max_day_trades,
            "day_trades_remaining": max(
                0, self._settings.max_day_trades - used - open_today
            ),
            "equity_threshold_usd": self._settings.equity_threshold_usd,
            "rolling_sessions": ROLLING_SESSIONS,
            "by_session": recent,
            "open_positions": open_positions,
            "rule": "US FINRA pattern day trader — set PDT_ENABLED=false if not applicable",
        }

    def reset(self) -> None:
        with self._lock:
            self._open_sessions.clear()
            self._day_trades.clear()
            self._save()

    # ------------------------------------------------------------------
    # Internals (call with the lock held)
    # ------------------------------------------------------------------
    def _today(self, now: datetime | None = None) -> date:
        return date.fromisoformat(session_date(now))

    def _count_locked(self, now: datetime | None = None) -> int:
        window = business_window(self._today(now))
        return sum(count for day, count in self._day_trades.items() if day in window)

    def _prune_locked(self, now: datetime | None = None) -> None:
        """Drop sessions well outside the window.

        History is kept longer than the window itself so a clock skew or a
        late-arriving close cannot silently erase a day trade that still counts.
        """
        window = business_window(self._today(now), sessions=ROLLING_SESSIONS * 3)
        for day in [d for d in self._day_trades if d not in window]:
            del self._day_trades[day]

    def _load(self) -> None:
        path = self._settings.state_path
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self._open_sessions = {
                str(k).upper(): str(v)
                for k, v in dict(payload.get("open_sessions", {})).items()
            }
            self._day_trades = {
                str(k): int(v) for k, v in dict(payload.get("day_trades", {})).items()
            }
            self._prune_locked()
        except Exception as exc:
            logger.error(
                "Could not read day-trade state from %s (%s). Starting empty — the "
                "day-trade count may understate reality until the window rolls over.",
                path,
                exc,
            )

    def _save(self) -> None:
        try:
            path = self._settings.state_path
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(
                    {
                        "open_sessions": self._open_sessions,
                        "day_trades": self._day_trades,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            temp.replace(path)
        except Exception as exc:
            logger.error("Failed to persist day-trade state: %s", exc)
