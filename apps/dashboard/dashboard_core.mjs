function latestTimestamp(row) {
  const candidates = [
    row.signal?.ts,
    row.decision?.created_at,
    row.order?.created_at,
    ...row.fills.map((fill) => fill.filled_at),
  ].filter(Boolean);
  return candidates.sort().at(-1) ?? "";
}

function workflowState(row) {
  if (row.order?.status) {
    return String(row.order.status).toUpperCase();
  }
  if (row.decision?.decision) {
    return String(row.decision.decision).toUpperCase();
  }
  return "PENDING";
}

export function rejectionReasons(orders) {
  return orders
    .filter((order) => order.rejection_reason)
    .map((order) => ({
      order_id: order.order_id,
      signal_id: order.signal_id,
      symbol: order.symbol,
      rejection_reason: order.rejection_reason,
      created_at: order.created_at,
    }));
}

export function buildLifecycleRows(signals, decisions, orders, fills, positions) {
  const signalMap = new Map(signals.map((signal) => [signal.signal_id, signal]));
  const decisionMap = new Map(decisions.map((decision) => [decision.signal_id, decision]));
  const orderMap = new Map(orders.map((order) => [order.signal_id, order]));
  const fillsBySignal = new Map();

  for (const fill of fills) {
    const signalFills = fillsBySignal.get(fill.signal_id) ?? [];
    signalFills.push(fill);
    fillsBySignal.set(fill.signal_id, signalFills);
  }

  const ids = new Set([
    ...signalMap.keys(),
    ...decisionMap.keys(),
    ...orderMap.keys(),
    ...fillsBySignal.keys(),
  ]);

  return [...ids]
    .map((signalId) => {
      const signal = signalMap.get(signalId) ?? null;
      const decision = decisionMap.get(signalId) ?? null;
      const order = orderMap.get(signalId) ?? null;
      const signalFills = fillsBySignal.get(signalId) ?? [];
      const symbol =
        signal?.symbol ??
        decision?.symbol ??
        order?.symbol ??
        signalFills[0]?.symbol ??
        "UNKNOWN";
      const position = positions.find((item) => item.symbol === symbol) ?? null;
      const row = {
        signal_id: signalId,
        symbol,
        signal,
        decision,
        order,
        fills: signalFills,
        position,
      };
      return {
        ...row,
        workflow_state: workflowState(row),
        latest_at: latestTimestamp(row),
      };
    })
    .sort((left, right) => String(right.latest_at).localeCompare(String(left.latest_at)));
}

export function applyLifecycleFilters(rows, filters) {
  const query = filters.query.trim().toUpperCase();
  const state = String(filters.state || "ALL").toUpperCase();
  const limit = filters.limit === "ALL" ? rows.length : Number(filters.limit);

  return rows
    .filter((row) => {
      if (
        query &&
        ![row.symbol, row.signal_id, row.order?.order_id, row.order?.external_order_id]
          .filter(Boolean)
          .some((value) => String(value).toUpperCase().includes(query))
      ) {
        return false;
      }
      if (state !== "ALL" && row.workflow_state !== state) {
        return false;
      }
      return true;
    })
    .slice(0, limit);
}

export function buildDashboardView(data, filters) {
  const lifecycles = buildLifecycleRows(
    data.signals,
    data.policyDecisions,
    data.orders,
    data.fills,
    data.positions,
  );
  const filteredLifecycles = applyLifecycleFilters(lifecycles, filters);
  const visibleSymbols = new Set(filteredLifecycles.map((row) => row.symbol));

  return {
    lifecycles: filteredLifecycles,
    signals: filteredLifecycles.map((row) => row.signal).filter(Boolean),
    policyDecisions: filteredLifecycles.map((row) => row.decision).filter(Boolean),
    orders: filteredLifecycles.map((row) => row.order).filter(Boolean),
    fills: filteredLifecycles.flatMap((row) => row.fills),
    positions: data.positions.filter((position) => visibleSymbols.has(position.symbol)),
    rejections: rejectionReasons(filteredLifecycles.map((row) => row.order).filter(Boolean)),
  };
}

export function buildMetrics(view) {
  const decisionCounts = view.policyDecisions.reduce(
    (counts, decision) => {
      const key = String(decision.decision).toUpperCase();
      counts[key] = (counts[key] ?? 0) + 1;
      return counts;
    },
    { APPROVE: 0, REVIEW: 0, REJECT: 0 },
  );
  const orderCounts = view.orders.reduce(
    (counts, order) => {
      const key = String(order.status).toUpperCase();
      counts[key] = (counts[key] ?? 0) + 1;
      return counts;
    },
    { ACCEPTED: 0, REJECTED: 0 },
  );

  return {
    signalCount: view.signals.length,
    approveCount: decisionCounts.APPROVE,
    reviewCount: decisionCounts.REVIEW,
    rejectCount: decisionCounts.REJECT,
    acceptedOrders: orderCounts.ACCEPTED,
    rejectedOrders: orderCounts.REJECTED,
    fillCount: view.fills.length,
    grossExposure: view.positions.reduce(
      (total, position) => total + Number(position.market_value),
      0,
    ),
    realizedPnl: view.positions.reduce(
      (total, position) => total + Number(position.realized_pnl),
      0,
    ),
    unrealizedPnl: view.positions.reduce(
      (total, position) => total + Number(position.unrealized_pnl),
      0,
    ),
  };
}

export function mergeRefreshResults(results) {
  const data = {
    signals: [],
    policyDecisions: [],
    orders: [],
    fills: [],
    positions: [],
  };
  const errors = [];

  for (const [key, result] of Object.entries(results)) {
    if (result.status === "fulfilled") {
      data[key] = result.value;
    } else {
      errors.push(`${key}: ${result.reason.message}`);
    }
  }

  return { data, errors };
}
