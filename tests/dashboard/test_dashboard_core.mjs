import test from "node:test";
import assert from "node:assert/strict";

import {
  applyLifecycleFilters,
  buildDashboardView,
  buildLifecycleRows,
  buildMetrics,
  mergeRefreshResults,
} from "../../apps/dashboard/dashboard_core.mjs";

const signals = [
  {
    signal_id: "sig-accept",
    symbol: "AAPL",
    ts: "2026-03-12T10:00:00Z",
    candidate_action: "BUY",
    confidence: 0.8,
    size_pct: 0.01,
  },
  {
    signal_id: "sig-review",
    symbol: "MSFT",
    ts: "2026-03-12T10:01:00Z",
    candidate_action: "BUY",
    confidence: 0.4,
    size_pct: 0.01,
  },
  {
    signal_id: "sig-reject",
    symbol: "REJECT",
    ts: "2026-03-12T10:02:00Z",
    candidate_action: "SELL",
    confidence: 0.7,
    size_pct: 0.015,
  },
];

const policyDecisions = [
  {
    signal_id: "sig-accept",
    symbol: "AAPL",
    decision: "APPROVE",
    reasons: [],
    approved_size_pct: 0.01,
    policy_version: "risk_policy_v1",
    created_at: "2026-03-12T10:00:10Z",
  },
  {
    signal_id: "sig-review",
    symbol: "MSFT",
    decision: "REVIEW",
    reasons: ["confidence_below_floor"],
    approved_size_pct: 0.01,
    policy_version: "risk_policy_v1",
    created_at: "2026-03-12T10:01:10Z",
  },
  {
    signal_id: "sig-reject",
    symbol: "REJECT",
    decision: "APPROVE",
    reasons: [],
    approved_size_pct: 0.015,
    policy_version: "risk_policy_v1",
    created_at: "2026-03-12T10:02:10Z",
  },
];

const orders = [
  {
    order_id: "ord-accept",
    signal_id: "sig-accept",
    symbol: "AAPL",
    side: "BUY",
    qty: 10,
    status: "ACCEPTED",
    created_at: "2026-03-12T10:00:20Z",
    rejection_reason: null,
  },
  {
    order_id: "ord-reject",
    signal_id: "sig-reject",
    symbol: "REJECT",
    side: "SELL",
    qty: 10,
    status: "REJECTED",
    created_at: "2026-03-12T10:02:20Z",
    rejection_reason: "symbol_rejected",
  },
];

const fills = [
  {
    fill_id: "fill-1",
    order_id: "ord-accept",
    signal_id: "sig-accept",
    symbol: "AAPL",
    side: "BUY",
    qty: 10,
    price: 100,
    filled_at: "2026-03-12T10:00:21Z",
  },
];

const positions = [
  {
    symbol: "AAPL",
    net_qty: 10,
    average_cost: 100,
    realized_pnl: 0,
    unrealized_pnl: 10,
    market_price: 101,
    market_value: 1010,
    updated_at: "2026-03-12T10:00:21Z",
  },
];

test("buildLifecycleRows links accepted, review, and rejected states", () => {
  const rows = buildLifecycleRows(signals, policyDecisions, orders, fills, positions);

  assert.equal(rows.length, 3);
  assert.equal(rows[0].signal_id, "sig-reject");
  assert.equal(rows[0].workflow_state, "REJECTED");
  assert.equal(rows[1].workflow_state, "REVIEW");
  assert.equal(rows[2].workflow_state, "ACCEPTED");
});

test("applyLifecycleFilters isolates symbol and workflow state", () => {
  const rows = buildLifecycleRows(signals, policyDecisions, orders, fills, positions);

  const rejected = applyLifecycleFilters(rows, {
    query: "reject",
    state: "REJECTED",
    limit: 10,
  });

  assert.equal(rejected.length, 1);
  assert.equal(rejected[0].signal_id, "sig-reject");
});

test("buildDashboardView and metrics preserve position truth from fills only", () => {
  const view = buildDashboardView(
    { signals, policyDecisions, orders, fills, positions },
    { query: "", state: "ALL", limit: 10 },
  );
  const metrics = buildMetrics(view);

  assert.equal(view.policyDecisions.length, 3);
  assert.equal(view.orders.length, 2);
  assert.equal(view.fills.length, 1);
  assert.equal(view.positions.length, 1);
  assert.equal(metrics.reviewCount, 1);
  assert.equal(metrics.rejectedOrders, 1);
  assert.equal(metrics.grossExposure, 1010);
  assert.equal(metrics.unrealizedPnl, 10);
});

test("mergeRefreshResults tolerates partial service failure", () => {
  const merged = mergeRefreshResults({
    signals: { status: "fulfilled", value: signals },
    policyDecisions: { status: "rejected", reason: new Error("policy failed") },
    orders: { status: "fulfilled", value: orders },
    fills: { status: "fulfilled", value: fills },
    positions: { status: "fulfilled", value: positions },
  });

  assert.equal(merged.data.signals.length, 3);
  assert.equal(merged.data.policyDecisions.length, 0);
  assert.equal(merged.errors.length, 1);
  assert.match(merged.errors[0], /policyDecisions: policy failed/);
});
