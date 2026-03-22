"""Integration test configuration.

Sets environment variables so policy-service approves signals in any
environment (outside ET market hours, unlimited weekly cap for tests).
"""

from __future__ import annotations

import os

# Disable trading-hours check — tests must pass at any time of day
os.environ.setdefault("POLICY_DISABLE_TRADING_HOURS", "true")

# Raise weekly notional cap well above any test signal notional
# (default cap is $500 but test signals are ~$1000 at size_pct=0.01 × $100k)
os.environ.setdefault("POLICY_WEEKLY_CAP_USD", "1000000")
