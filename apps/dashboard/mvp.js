import { buildDashboardView, buildMetrics, mergeRefreshResults } from "./dashboard_core.mjs";

const CONFIG = {
  strategyBaseUrl: "http://localhost:8003",
  policyBaseUrl: "http://localhost:8001",
  executionBaseUrl: "http://localhost:8002",
  portfolioBaseUrl: "http://localhost:8004",
  researchBaseUrl: "http://localhost:8005",
  orchestratorBaseUrl: "http://localhost:8007",
  approvalBaseUrl: "http://localhost:8010",
  sentimentBaseUrl: "http://localhost:8008",
  ...window.TRADE_PILOT_CONFIG,
};

const DATA_SOURCES = {
  signals: `${CONFIG.strategyBaseUrl}/v1/signals?limit=10`,
  policyDecisions: `${CONFIG.policyBaseUrl}/v1/policy/evaluations?limit=10`,
  orders: `${CONFIG.executionBaseUrl}/v1/orders?limit=10`,
  fills: `${CONFIG.executionBaseUrl}/v1/fills`,
  positions: `${CONFIG.portfolioBaseUrl}/v1/portfolio/positions`,
  account: `${CONFIG.executionBaseUrl}/v1/account`,
  workerStatus: `${CONFIG.strategyBaseUrl}/v1/worker/status`,
  research: `${CONFIG.researchBaseUrl}/v1/research/reports`,
  orchestratorStatus: `${CONFIG.orchestratorBaseUrl}/v1/orchestrator/status`,
  orchestratorCycle: `${CONFIG.orchestratorBaseUrl}/v1/orchestrator/cycle/last`,
  validation: `${CONFIG.orchestratorBaseUrl}/v1/orchestrator/validate`,
  approvals: `${CONFIG.approvalBaseUrl}/v1/approvals/pending`,
};

const filters = {
  query: "",
  state: "ALL",
  limit: 10,
};

let latestData = {
  signals: [],
  policyDecisions: [],
  orders: [],
  fills: [],
  positions: [],
};
let selectedLifecycleId = null;
const policyBaselineCap = 500;

async function readJson(label, url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${label} returned ${response.status} from ${url}`);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function formatTime(value) {
  if (!value) {
    return "n/a";
  }

  return new Date(value).toLocaleString();
}

function formatPct(value) {
  return `${(value * 100).toFixed(1)}%`;
}

function formatNumber(value) {
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function setCount(id, count) {
  document.getElementById(`${id}-count`).textContent = `${count} item${count === 1 ? "" : "s"}`;
}

function renderEmpty(id, label) {
  document.getElementById(id).innerHTML = `<div class="empty-state">No ${escapeHtml(label)} yet.</div>`;
}

function renderPanelError(id, label, message) {
  document.getElementById(id).innerHTML = `
    <div class="empty-state empty-state-error">
      ${escapeHtml(label)} unavailable: ${escapeHtml(message)}
    </div>
  `;
}

function statusTone(value) {
  const normalized = String(value || "").toUpperCase();
  if (["APPROVE", "ACCEPTED", "BUY"].includes(normalized)) {
    return "good";
  }
  if (["REJECT", "REJECTED", "SELL"].includes(normalized)) {
    return "bad";
  }
  if (["REVIEW"].includes(normalized)) {
    return "warn";
  }
  return "neutral";
}

function badge(value) {
  const tone = statusTone(value);
  return `<span class="badge badge-${tone}">${escapeHtml(value)}</span>`;
}

function formatCompactId(value) {
  return value ? escapeHtml(String(value).slice(0, 8)) : "n/a";
}

function pnlClass(value) {
  return Number(value) >= 0 ? "pnl-up" : "pnl-down";
}

function renderMetrics(metrics) {
  document.getElementById("metric-signals").textContent = String(metrics.signalCount);
  document.getElementById("metric-policy").textContent =
    `${metrics.approveCount}/${metrics.reviewCount}/${metrics.rejectCount}`;
  document.getElementById("metric-orders").textContent =
    `${metrics.acceptedOrders}/${metrics.rejectedOrders}`;
  document.getElementById("metric-fills").textContent = String(metrics.fillCount);
  document.getElementById("metric-exposure").textContent = `$${formatNumber(metrics.grossExposure)}`;
  document.getElementById("metric-pnl").textContent =
    `$${formatNumber(metrics.realizedPnl)} / $${formatNumber(metrics.unrealizedPnl)}`;
}

function renderSignals(signals) {
  setCount("signals", signals.length);
  if (!signals.length) {
    renderEmpty("signals", "signals");
    return;
  }

  document.getElementById("signals").innerHTML = signals
    .map(
      (signal) => `
        <section class="item-card">
          <div class="item-top">
            <strong>${escapeHtml(signal.symbol)}</strong>
            ${badge(signal.candidate_action)}
          </div>
          <dl class="kv-grid">
            <div><dt>Signal</dt><dd>${escapeHtml(signal.signal_id.slice(0, 8))}</dd></div>
            <div><dt>Confidence</dt><dd>${formatPct(signal.confidence)}</dd></div>
            <div><dt>Size</dt><dd>${formatPct(signal.size_pct)}</dd></div>
            <div><dt>Time</dt><dd>${escapeHtml(formatTime(signal.ts))}</dd></div>
          </dl>
        </section>
      `,
    )
    .join("");
}

function renderPolicyDecisions(policyDecisions) {
  setCount("policy-decisions", policyDecisions.length);
  if (!policyDecisions.length) {
    renderEmpty("policy-decisions", "policy decisions");
    return;
  }

  document.getElementById("policy-decisions").innerHTML = policyDecisions
    .map(
      (decision) => `
        <section class="item-card">
          <div class="item-top">
            <strong>${escapeHtml(decision.symbol)}</strong>
            ${badge(decision.decision)}
          </div>
          <dl class="kv-grid">
            <div><dt>Signal</dt><dd>${escapeHtml(decision.signal_id.slice(0, 8))}</dd></div>
            <div><dt>Approved Size</dt><dd>${formatPct(decision.approved_size_pct)}</dd></div>
            <div><dt>Policy</dt><dd>${escapeHtml(decision.policy_version)}</dd></div>
            <div><dt>Time</dt><dd>${escapeHtml(formatTime(decision.created_at))}</dd></div>
          </dl>
          <p class="reason-line">${decision.reasons.length ? escapeHtml(decision.reasons.join(", ")) : "No rule hits"}</p>
        </section>
      `,
    )
    .join("");
}

function renderLifecycles(lifecycles) {
  setCount("lifecycles", lifecycles.length);
  if (!lifecycles.length) {
    selectedLifecycleId = null;
    document.getElementById("lifecycles").innerHTML =
      `<div class="empty-state">No lifecycle records match the current filters.</div>`;
    document.getElementById("lifecycle-detail").innerHTML =
      `<div class="empty-state">Select a lifecycle when matching records are available.</div>`;
    return;
  }

  if (!selectedLifecycleId || !lifecycles.some((row) => row.signal_id === selectedLifecycleId)) {
    selectedLifecycleId = lifecycles[0].signal_id;
  }

  document.getElementById("lifecycles").innerHTML = lifecycles
    .map(
      (row) => `
        <button class="lifecycle-row ${row.signal_id === selectedLifecycleId ? "lifecycle-row-active" : ""}" data-signal-id="${escapeHtml(row.signal_id)}">
          <span class="lifecycle-row-main">
            <strong>${escapeHtml(row.symbol)}</strong>
            ${badge(row.workflow_state)}
          </span>
          <span class="lifecycle-row-meta">
            signal ${formatCompactId(row.signal_id)} · ${escapeHtml(formatTime(row.latest_at))}
          </span>
        </button>
      `,
    )
    .join("");

  document.querySelectorAll(".lifecycle-row").forEach((button) => {
    button.addEventListener("click", () => {
      selectedLifecycleId = button.dataset.signalId;
      renderDashboard(latestData, []);
    });
  });

  const selected = lifecycles.find((row) => row.signal_id === selectedLifecycleId) ?? lifecycles[0];
  document.getElementById("lifecycle-detail").innerHTML = `
    <section class="item-card">
      <div class="item-top">
        <strong>${escapeHtml(selected.symbol)}</strong>
        ${badge(selected.workflow_state)}
      </div>
      <dl class="kv-grid">
        <div><dt>Signal ID</dt><dd>${formatCompactId(selected.signal_id)}</dd></div>
        <div><dt>Action</dt><dd>${selected.signal ? badge(selected.signal.candidate_action) : "n/a"}</dd></div>
        <div><dt>Policy</dt><dd>${selected.decision ? badge(selected.decision.decision) : "No policy record"}</dd></div>
        <div><dt>Order</dt><dd>${selected.order ? badge(selected.order.status) : "No order created"}</dd></div>
      </dl>
      <div class="timeline">
        <div class="timeline-row">
          <span class="timeline-label">Signal</span>
          <span class="timeline-value">${selected.signal ? `${escapeHtml(formatTime(selected.signal.ts))} · confidence ${formatPct(selected.signal.confidence)}` : "Missing"}</span>
        </div>
        <div class="timeline-row">
          <span class="timeline-label">Policy</span>
          <span class="timeline-value">${selected.decision ? `${escapeHtml(formatTime(selected.decision.created_at))} · ${escapeHtml(selected.decision.reasons.join(", ") || "no rule hits")}` : "No policy decision"}</span>
        </div>
        <div class="timeline-row">
          <span class="timeline-label">Execution</span>
          <span class="timeline-value">${selected.order ? `${escapeHtml(formatTime(selected.order.created_at))} · ${escapeHtml(selected.order.rejection_reason || selected.order.status)}` : "No execution attempted"}</span>
        </div>
        <div class="timeline-row">
          <span class="timeline-label">Fills</span>
          <span class="timeline-value">${selected.fills.length ? `${selected.fills.length} fill(s)` : "No fills recorded"}</span>
        </div>
        <div class="timeline-row">
          <span class="timeline-label">Portfolio Effect</span>
          <span class="timeline-value">${selected.position ? `${escapeHtml(selected.position.symbol)} · qty ${escapeHtml(selected.position.net_qty)} · MV $${formatNumber(selected.position.market_value)}` : "No derived position impact"}</span>
        </div>
      </div>
    </section>
  `;
}

function renderOrders(orders) {
  setCount("orders", orders.length);
  if (!orders.length) {
    renderEmpty("orders", "orders");
    return;
  }

  document.getElementById("orders").innerHTML = `
    <div class="table-shell">
      <table class="data-table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Side</th>
            <th>Qty</th>
            <th>Status</th>
            <th>Created</th>
          </tr>
        </thead>
        <tbody>
          ${orders
            .map(
              (order) => `
                <tr>
                  <td>${escapeHtml(order.symbol)}</td>
                  <td>${badge(order.side)}</td>
                  <td>${escapeHtml(order.qty)}</td>
                  <td>${badge(order.status)}</td>
                  <td>${escapeHtml(formatTime(order.created_at))}</td>
                </tr>
              `,
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderFills(fills) {
  setCount("fills", fills.length);
  if (!fills.length) {
    renderEmpty("fills", "fills");
    return;
  }

  document.getElementById("fills").innerHTML = `
    <div class="table-shell">
      <table class="data-table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Side</th>
            <th>Qty</th>
            <th>Price</th>
            <th>Filled</th>
          </tr>
        </thead>
        <tbody>
          ${fills
            .map(
              (fill) => `
                <tr>
                  <td>${escapeHtml(fill.symbol)}</td>
                  <td>${badge(fill.side)}</td>
                  <td>${escapeHtml(fill.qty)}</td>
                  <td>$${formatNumber(fill.price)}</td>
                  <td>${escapeHtml(formatTime(fill.filled_at))}</td>
                </tr>
              `,
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderPositions(positions) {
  setCount("positions", positions.length);
  if (!positions.length) {
    renderEmpty("positions", "positions");
    return;
  }

  document.getElementById("positions").innerHTML = positions
    .map(
      (position) => `
        <section class="item-card">
          <div class="item-top">
            <strong>${escapeHtml(position.symbol)}</strong>
            <span class="position-value">$${formatNumber(position.market_value)}</span>
          </div>
          <dl class="kv-grid">
            <div><dt>Net Qty</dt><dd>${escapeHtml(position.net_qty)}</dd></div>
            <div><dt>Avg Cost</dt><dd>$${formatNumber(position.average_cost)}</dd></div>
            <div><dt>Market</dt><dd>$${formatNumber(position.market_price)}</dd></div>
            <div><dt>Updated</dt><dd>${escapeHtml(formatTime(position.updated_at))}</dd></div>
          </dl>
          <div class="pnl-row">
            <span class="pnl-chip ${pnlClass(position.unrealized_pnl)}">
              Unrealized: $${formatNumber(position.unrealized_pnl)}
            </span>
            <span class="pnl-chip ${pnlClass(position.realized_pnl)}">
              Realized: $${formatNumber(position.realized_pnl)}
            </span>
          </div>
        </section>
      `,
    )
    .join("");
}

function renderRejections(rejections) {
  setCount("rejections", rejections.length);
  if (!rejections.length) {
    renderEmpty("rejections", "rejection reasons");
    return;
  }

  document.getElementById("rejections").innerHTML = rejections
    .map(
      (rejection) => `
        <section class="item-card item-card-alert">
          <div class="item-top">
            <strong>${escapeHtml(rejection.symbol)}</strong>
            ${badge("REJECTED")}
          </div>
          <p class="reason-line">${escapeHtml(rejection.rejection_reason)}</p>
          <p class="subtle-line">Order ${escapeHtml(rejection.order_id.slice(0, 8))}</p>
        </section>
      `,
    )
    .join("");
}

function formatCurrency(value) {
  return `$${Number(value || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

async function fetchAuditSummary() {
  try {
    const res = await fetch("http://localhost:8006/v1/audit/summary");
    if (!res.ok) return;
    const data = await res.json();
    const bar = document.getElementById("audit-stats-bar");
    if (!bar) return;
    bar.innerHTML = `
      <span>Trades: <b>${data.total_trades}</b></span>
      <span>Approved Today: <b>${data.approved_today}</b></span>
      <span>Rejected Today: <b>${data.rejected_today}</b></span>
      <span>Weekly Spend: <b>$${(data.weekly_spend || 0).toFixed(2)}</b></span>
      <span>Top Symbols: <b>${(data.top_symbols_traded || []).join(", ") || "none"}</b></span>
    `;
  } catch (e) {
    console.warn("Audit summary unavailable", e);
  }
}

function renderWallet(account) {
  if (!account) return;
  const mode = (account.mode || "paper").toLowerCase();
  const isLive = mode === "live";
  const badgeClass = isLive ? "mode-live" : "mode-paper";
  const badgeText = isLive ? "LIVE" : "PAPER";

  document.getElementById("wallet-buying-power").textContent = formatCurrency(account.buying_power);
  document.getElementById("wallet-equity").textContent = formatCurrency(account.equity);
  document.getElementById("wallet-cash").textContent = formatCurrency(account.cash);

  const walletBadge = document.getElementById("wallet-mode-badge");
  walletBadge.textContent = badgeText;
  walletBadge.className = `mode-badge ${badgeClass}`;

  const headerBadge = document.getElementById("mode-badge");
  headerBadge.textContent = badgeText;
  headerBadge.className = `mode-badge ${badgeClass}`;
}

function renderWorkerStatus(status) {
  if (!status) return;
  const badge = document.getElementById("worker-running-badge");
  if (status.is_running) {
    badge.textContent = "RUNNING";
    badge.className = "mode-badge mode-live";
  } else {
    badge.textContent = "IDLE";
    badge.className = "mode-badge mode-paper";
  }
  document.getElementById("worker-last-run").textContent =
    status.last_run_at ? formatTime(status.last_run_at) : "Never";
  document.getElementById("worker-next-run").textContent =
    status.next_run_at ? formatTime(status.next_run_at) : "Not scheduled";
  document.getElementById("worker-symbols").textContent =
    (status.symbols_watched || []).join(", ") || "—";
  document.getElementById("worker-error").textContent =
    status.last_run_error || "—";
}

function renderOrchestratorStatus(orchestratorStatus) {
  if (!orchestratorStatus) return;
  const badge = document.getElementById("orchestrator-running-badge");
  badge.textContent = orchestratorStatus.running ? "RUNNING" : "PAUSED";
  badge.className = `mode-badge ${orchestratorStatus.running ? "mode-live" : "mode-paper"}`;
  document.getElementById("orchestrator-last-cycle").textContent =
    orchestratorStatus.last_cycle_time ? formatTime(orchestratorStatus.last_cycle_time) : "Never";
  document.getElementById("orchestrator-trades-today").textContent =
    String(orchestratorStatus.trades_today ?? 0);
  document.getElementById("orchestrator-weekly-spend").textContent =
    formatCurrency(orchestratorStatus.weekly_notional_used ?? 0);
  document.getElementById("orchestrator-weekly-cap").textContent =
    formatCurrency(orchestratorStatus.weekly_notional_cap_usd ?? policyBaselineCap);
  const indicator = document.getElementById("trade-mode-indicator");
  const mode = String(orchestratorStatus.trading_mode || "demo").toLowerCase();
  indicator.textContent = mode.toUpperCase();
  indicator.className = `mode-badge ${mode === "live" ? "mode-live" : "mode-paper"}`;
}

function renderPendingApprovals(rows) {
  const root = document.getElementById("pending-approvals");
  document.getElementById("approvals-count").textContent = `${rows.length} item${rows.length === 1 ? "" : "s"}`;
  if (!rows.length) {
    root.innerHTML = `<div class="empty-state">No pending approvals.</div>`;
    return;
  }
  root.innerHTML = rows.map((row) => `
    <section class="item-card">
      <div class="item-top">
        <strong>${escapeHtml(row.symbol)}</strong>
        ${badge(row.status)}
      </div>
      <dl class="kv-grid">
        <div><dt>Action</dt><dd>${badge(row.action)}</dd></div>
        <div><dt>Tier</dt><dd>${escapeHtml(row.tier)}</dd></div>
        <div><dt>Amount</dt><dd>${formatCurrency(row.amount_usd)}</dd></div>
        <div><dt>Created</dt><dd>${escapeHtml(formatTime(row.created_at))}</dd></div>
      </dl>
      <p class="reason-line">${escapeHtml(row.reason || "approval required")}</p>
    </section>
  `).join("");
}

function renderCycleSummary(summary) {
  const root = document.getElementById("orchestrator-cycle-summary");
  if (!summary || !Object.keys(summary).length) {
    root.innerHTML = `<div class="empty-state">No cycle summary yet.</div>`;
    return;
  }
  root.innerHTML = `
    <section class="item-card">
      <div class="item-top">
        <strong>${escapeHtml(summary.status || "unknown")}</strong>
        ${badge("CYCLE")}
      </div>
      <dl class="kv-grid">
        <div><dt>Signals</dt><dd>${escapeHtml(summary.signals ?? 0)}</dd></div>
        <div><dt>Approved</dt><dd>${escapeHtml(summary.approved ?? 0)}</dd></div>
        <div><dt>Review</dt><dd>${escapeHtml(summary.review ?? 0)}</dd></div>
        <div><dt>Executed</dt><dd>${escapeHtml(summary.executed ?? 0)}</dd></div>
      </dl>
    </section>
  `;
}

function renderValidation(result) {
  const summary = document.getElementById("validation-summary");
  const root = document.getElementById("validation-results");
  if (!result) {
    summary.textContent = "No validation run yet.";
    root.innerHTML = `<div class="empty-state">Run validation to inspect the policy allowlist.</div>`;
    return;
  }

  const total = (result.valid || []).length + (result.invalid || []).length + (result.unknown || []).length;
  summary.textContent = `${total} symbols checked · ${(result.valid || []).length} valid · ${(result.invalid || []).length} invalid · ${(result.unknown || []).length} unknown`;

  const rows = [
    ...(result.valid || []).map((symbol) => ({ symbol, status: "VALID" })),
    ...(result.invalid || []).map((symbol) => ({ symbol, status: "INVALID" })),
    ...(result.unknown || []).map((symbol) => ({ symbol, status: "UNKNOWN" })),
  ];
  if (!rows.length) {
    root.innerHTML = `<div class="empty-state">No allowlist symbols configured.</div>`;
    return;
  }

  root.innerHTML = rows.map((row) => `
    <section class="item-card">
      <div class="item-top">
        <strong>${escapeHtml(row.symbol)}</strong>
        ${badge(row.status)}
      </div>
    </section>
  `).join("");
}

function renderResearch(reports) {
  const grid = document.getElementById("research-grid");
  const countEl = document.getElementById("research-count");
  if (!reports || !reports.length) {
    grid.innerHTML = `<div class="empty-state">No research reports available yet.</div>`;
    countEl.textContent = "0 symbols";
    return;
  }
  countEl.textContent = `${reports.length} symbol${reports.length === 1 ? "" : "s"}`;

  const sentimentBadgeClass = (s) => {
    if (s === "bullish") return "badge-good";
    if (s === "bearish") return "badge-bad";
    return "badge-neutral";
  };

  grid.innerHTML = reports
    .map(
      (r) => `
        <article class="research-card">
          <div class="item-top">
            <strong>${escapeHtml(r.symbol)}</strong>
            <span class="badge ${sentimentBadgeClass(r.sentiment)}">${escapeHtml(r.sentiment || "neutral")}</span>
          </div>
          <p class="research-headline">${escapeHtml((r.headline_summary || "").slice(0, 160))}${(r.headline_summary || "").length > 160 ? "…" : ""}</p>
          ${(r.risk_factors || []).slice(0, 2).map((rf) => `<p class="research-risk">⚠ ${escapeHtml(rf)}</p>`).join("")}
          <p class="subtle-line">${r.cached ? "cached · " : ""}${escapeHtml(formatTime(r.generated_at))}</p>
        </article>
      `,
    )
    .join("");
}

function attachFilterHandlers() {
  document.getElementById("symbol-filter").addEventListener("input", (event) => {
    filters.query = event.target.value;
    renderDashboard(latestData, []);
  });
  document.getElementById("state-filter").addEventListener("change", (event) => {
    filters.state = event.target.value;
    renderDashboard(latestData, []);
  });
  document.getElementById("limit-filter").addEventListener("change", (event) => {
    filters.limit = event.target.value === "ALL" ? "ALL" : Number(event.target.value);
    renderDashboard(latestData, []);
  });
}

function renderDashboard(data, errors) {
  const view = buildDashboardView(data, filters);
  const metrics = buildMetrics(view);

  renderMetrics(metrics);
  renderLifecycles(view.lifecycles);
  renderSignals(view.signals);
  renderPolicyDecisions(view.policyDecisions);
  renderOrders(view.orders);
  renderFills(view.fills);
  renderPositions(view.positions);
  renderRejections(view.rejections);

  for (const error of errors) {
    if (error.startsWith("signals:")) {
      renderPanelError("signals", "Signals", error.replace("signals: ", ""));
    }
    if (error.startsWith("policyDecisions:")) {
      renderPanelError("policy-decisions", "Policy decisions", error.replace("policyDecisions: ", ""));
    }
    if (error.startsWith("orders:")) {
      renderPanelError("orders", "Orders", error.replace("orders: ", ""));
      renderPanelError("rejections", "Rejections", error.replace("orders: ", ""));
    }
    if (error.startsWith("fills:")) {
      renderPanelError("fills", "Fills", error.replace("fills: ", ""));
    }
    if (error.startsWith("positions:")) {
      renderPanelError("positions", "Positions", error.replace("positions: ", ""));
    }
  }
}

async function refresh() {
  const status = document.getElementById("status");
  status.textContent = "Refreshing...";

  const settled = await Promise.allSettled([
      readJson("signals", DATA_SOURCES.signals),
      readJson("policy evaluations", DATA_SOURCES.policyDecisions),
      readJson("orders", DATA_SOURCES.orders),
      readJson("fills", DATA_SOURCES.fills),
      readJson("positions", DATA_SOURCES.positions),
      readJson("account", DATA_SOURCES.account),
      readJson("worker status", DATA_SOURCES.workerStatus),
      readJson("research", DATA_SOURCES.research),
      readJson("orchestrator status", DATA_SOURCES.orchestratorStatus),
      readJson("orchestrator cycle", DATA_SOURCES.orchestratorCycle),
      readJson("approvals", DATA_SOURCES.approvals),
      readJson("validation", DATA_SOURCES.validation, { method: "POST" }),
  ]);
  const { data, errors } = mergeRefreshResults({
    signals: settled[0],
    policyDecisions: settled[1],
    orders: settled[2],
    fills: settled[3],
    positions: settled[4],
  });

  latestData = data;
  renderDashboard(data, errors);

  // New panels — non-fatal, render independently
  if (settled[5].status === "fulfilled") {
    renderWallet(settled[5].value);
  }
  if (settled[6].status === "fulfilled") {
    renderWorkerStatus(settled[6].value);
  }
  if (settled[7].status === "fulfilled") {
    renderResearch(settled[7].value);
  }
  if (settled[8].status === "fulfilled") {
    renderOrchestratorStatus(settled[8].value);
  }
  if (settled[9].status === "fulfilled") {
    renderCycleSummary(settled[9].value);
  }
  if (settled[10].status === "fulfilled") {
    renderPendingApprovals(settled[10].value);
  }
  if (settled[11].status === "fulfilled") {
    renderValidation(settled[11].value);
  }

  if (errors.length) {
    status.textContent = `Partial refresh: ${errors.join(" | ")}`;
    await fetchAuditSummary();
    return;
  }

  status.textContent = `Last refresh: ${new Date().toLocaleTimeString()}`;
  await fetchAuditSummary();
}

document.getElementById("strategy-url").textContent = CONFIG.strategyBaseUrl;
document.getElementById("policy-url").textContent = CONFIG.policyBaseUrl;
document.getElementById("execution-url").textContent = CONFIG.executionBaseUrl;
document.getElementById("portfolio-url").textContent = CONFIG.portfolioBaseUrl;
document.getElementById("research-url").textContent = CONFIG.researchBaseUrl;
document.getElementById("orchestrator-url").textContent = CONFIG.orchestratorBaseUrl;
attachFilterHandlers();
attachTradeHandlers();
attachChartHandlers();
attachWorkerRunHandler();
attachOrchestratorHandlers();
attachValidationHandler();
document.getElementById("refresh").addEventListener("click", refresh);
refresh();
fetchAuditSummary();
loadTicker();

// Auto-refresh ticker every 60 seconds
setInterval(loadTicker, 60_000);
setInterval(fetchAuditSummary, 60_000);

// ---------------------------------------------------------------------------
// Ticker bar
// ---------------------------------------------------------------------------

async function loadTicker() {
  const watchlist = ["AAPL", "MSFT", "GOOGL", "BTC/USD", "ETH/USD"];
  const syms = watchlist.join(",");
  try {
    const quotes = await readJson("quotes", `${CONFIG.strategyBaseUrl}/v1/market/quotes?symbols=${encodeURIComponent(syms)}`);
    const sentiment = await readJson("sentiment", `${CONFIG.sentimentBaseUrl}/v1/sentiment/batch?symbols=${encodeURIComponent(syms)}`);
    renderTicker(quotes, sentiment);
  } catch {
    document.getElementById("ticker-items").innerHTML =
      `<span class="ticker-loading">Prices unavailable — set ALPACA_API_KEY for live data</span>`;
  }
}

function renderTicker(quotes, sentimentRows = []) {
  const items = document.getElementById("ticker-items");
  if (!quotes || !quotes.length) {
    items.innerHTML = `<span class="ticker-loading">No price data</span>`;
    return;
  }
  const sentimentMap = new Map(sentimentRows.map((row) => [row.symbol, row]));
  items.innerHTML = quotes.map((q) => {
    if (q.price === null) {
      return `<span class="ticker-item ticker-neutral"><strong>${escapeHtml(q.symbol)}</strong> <span>—</span></span>`;
    }
    const dir = q.change_pct >= 0 ? "ticker-up" : "ticker-down";
    const arrow = q.change_pct >= 0 ? "▲" : "▼";
    const chg = Math.abs(q.change_pct).toFixed(2);
    const sentiment = sentimentMap.get(q.symbol);
    const sentimentText = sentiment ? ` · S ${(sentiment.score >= 0 ? "+" : "")}${Number(sentiment.score).toFixed(2)}` : "";
    return `<span class="ticker-item ${dir}">
      <strong>${escapeHtml(q.symbol)}</strong>
      <span>$${Number(q.price).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 4})}</span>
      <span class="ticker-change">${arrow} ${chg}%${escapeHtml(sentimentText)}</span>
    </span>`;
  }).join('<span class="ticker-sep">·</span>');
}

// ---------------------------------------------------------------------------
// Price chart (TradingView Lightweight Charts)
// ---------------------------------------------------------------------------

let _chartInstance = null;
let _candleSeries = null;
let _ema20Series = null;
let _ema50Series = null;

function initChart() {
  const container = document.getElementById("chart-container");
  container.innerHTML = "";

  const chart = LightweightCharts.createChart(container, {
    width: container.clientWidth || 700,
    height: 380,
    layout: {
      background: { color: "#0f1d31" },
      textColor: "#9db4cf",
    },
    grid: {
      vertLines: { color: "rgba(157,180,207,0.08)" },
      horzLines: { color: "rgba(157,180,207,0.08)" },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: "rgba(157,180,207,0.2)" },
    timeScale: {
      borderColor: "rgba(157,180,207,0.2)",
      timeVisible: true,
    },
  });

  const candleSeries = chart.addCandlestickSeries({
    upColor: "#9ff7c0",
    downColor: "#ff9090",
    borderUpColor: "#9ff7c0",
    borderDownColor: "#ff9090",
    wickUpColor: "#9ff7c0",
    wickDownColor: "#ff9090",
  });

  const ema20 = chart.addLineSeries({
    color: "#65e5e8",
    lineWidth: 1,
    title: "EMA20",
  });

  const ema50 = chart.addLineSeries({
    color: "#b8ff7b",
    lineWidth: 1,
    title: "EMA50",
  });

  // Resize observer
  const resizeObserver = new ResizeObserver(() => {
    chart.applyOptions({ width: container.clientWidth });
  });
  resizeObserver.observe(container);

  _chartInstance = chart;
  _candleSeries = candleSeries;
  _ema20Series = ema20;
  _ema50Series = ema50;
}

async function loadChart() {
  const symbol = document.getElementById("chart-symbol").value;
  const days = document.getElementById("chart-days").value;
  const infoEl = document.getElementById("chart-price-info");
  const indEl = document.getElementById("chart-indicators");

  infoEl.innerHTML = `<span class="chart-loading">Loading ${escapeHtml(symbol)}...</span>`;
  indEl.innerHTML = "";

  try {
    const data = await readJson("chart", `${CONFIG.strategyBaseUrl}/v1/market/chart/${encodeURIComponent(symbol)}?days=${days}`);

    if (!_chartInstance) initChart();

    // Set candle data
    const candles = data.bars.map((b) => ({
      time: b.time,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
    }));
    _candleSeries.setData(candles);
    _ema20Series.setData(data.ema_20);
    _ema50Series.setData(data.ema_50);
    _chartInstance.timeScale().fitContent();

    // Price info
    const last = data.bars[data.bars.length - 1];
    const prev = data.bars[data.bars.length - 2];
    const chgPct = prev ? ((last.close - prev.close) / prev.close * 100).toFixed(2) : 0;
    const chgClass = chgPct >= 0 ? "ticker-up" : "ticker-down";
    const arrow = chgPct >= 0 ? "▲" : "▼";
    infoEl.innerHTML = `
      <span class="chart-symbol-name">${escapeHtml(data.symbol)}</span>
      <span class="chart-price">$${last.close.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 4})}</span>
      <span class="${chgClass}">${arrow} ${Math.abs(chgPct)}%</span>
      <span class="chart-trend-badge badge ${data.trend === 'bullish' ? 'badge-good' : data.trend === 'bearish' ? 'badge-bad' : 'badge-neutral'}">${escapeHtml(data.trend)}</span>
      <span class="chart-source">${escapeHtml(data.data_source)}</span>
    `;

    // Indicators
    const ind = data.indicators;
    const rsiClass = ind.rsi > 70 ? "badge-bad" : ind.rsi < 30 ? "badge-good" : "badge-neutral";
    const tags = (data.signal_tags || []).map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join(" ");
    indEl.innerHTML = `
      <div class="indicator-row">
        <span class="ind-item"><span class="ind-label">RSI</span><span class="badge ${rsiClass}">${ind.rsi}</span></span>
        <span class="ind-item"><span class="ind-label">MACD</span><span>${ind.macd_histogram > 0 ? "▲" : "▼"} ${Math.abs(ind.macd_histogram).toFixed(4)}</span></span>
        <span class="ind-item"><span class="ind-label">BB</span><span>${((ind.bb_upper + ind.bb_lower) / 2).toFixed(2)} ± ${((ind.bb_upper - ind.bb_lower) / 2).toFixed(2)}</span></span>
      </div>
      <div class="tag-row">${tags}</div>
    `;
  } catch (err) {
    document.getElementById("chart-container").innerHTML =
      `<div class="chart-placeholder chart-error">Chart unavailable: ${escapeHtml(String(err.message || err))}</div>`;
    infoEl.innerHTML = "";
  }
}

function attachChartHandlers() {
  document.getElementById("chart-load").addEventListener("click", loadChart);
  document.getElementById("chart-symbol").addEventListener("change", loadChart);
  document.getElementById("chart-days").addEventListener("change", loadChart);
}

// ---------------------------------------------------------------------------
// Manual trade
// ---------------------------------------------------------------------------

async function submitTrade(side) {
  const symbol = document.getElementById("trade-symbol").value.trim().toUpperCase();
  const qty = parseInt(document.getElementById("trade-qty").value, 10);
  const statusEl = document.getElementById("trade-status");
  const resultEl = document.getElementById("trade-result");

  if (!symbol || qty < 1) {
    statusEl.textContent = "Enter a valid symbol and quantity.";
    statusEl.className = "trade-status trade-error";
    return;
  }

  statusEl.textContent = `Submitting ${side} ${qty} ${symbol}...`;
  statusEl.className = "trade-status trade-pending";
  resultEl.innerHTML = "";

  try {
    const response = await fetch(`${CONFIG.strategyBaseUrl}/v1/trade/manual`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol, qty, side, order_type: "MARKET" }),
      cache: "no-store",
    });
    const result = await response.json();

    if (result.status === "submitted" && result.order_status === "ACCEPTED") {
      statusEl.textContent = "Order accepted!";
      statusEl.className = "trade-status trade-success";
      resultEl.innerHTML = `
        <div class="trade-result-card trade-result-ok">
          <strong>${escapeHtml(side)} ${qty} ${escapeHtml(symbol)}</strong>
          <span class="badge badge-good">ACCEPTED</span>
          <p>Order ID: ${escapeHtml((result.order_id || "").slice(0, 8))}…</p>
          <p>Policy: ${escapeHtml(result.policy_decision || "—")}</p>
        </div>`;
      // Auto-refresh to show new fill
      setTimeout(refresh, 1500);
    } else {
      statusEl.textContent = `${result.status || "Failed"}`;
      statusEl.className = "trade-status trade-error";
      resultEl.innerHTML = `
        <div class="trade-result-card trade-result-fail">
          <strong>${escapeHtml(side)} ${qty} ${escapeHtml(symbol)}</strong>
          <span class="badge badge-bad">${escapeHtml(result.status || "FAILED")}</span>
          <p>Reasons: ${escapeHtml((result.reasons || []).join(", ") || "—")}</p>
        </div>`;
    }
  } catch (err) {
    statusEl.textContent = "Network error";
    statusEl.className = "trade-status trade-error";
    resultEl.innerHTML = `<p class="trade-error">${escapeHtml(String(err))}</p>`;
  }
}

function attachTradeHandlers() {
  document.getElementById("btn-buy").addEventListener("click", () => submitTrade("BUY"));
  document.getElementById("btn-sell").addEventListener("click", () => submitTrade("SELL"));
}

// ---------------------------------------------------------------------------
// Signal generator (quick panel)
// ---------------------------------------------------------------------------

async function generateQuickSignal() {
  const symbol = document.getElementById("signal-symbol").value.trim().toUpperCase();
  const resultEl = document.getElementById("signal-result");
  if (!symbol) return;

  resultEl.innerHTML = `<p class="signal-loading">Generating signal for ${escapeHtml(symbol)}...</p>`;

  try {
    const signal = await readJson("signal", `${CONFIG.strategyBaseUrl}/v1/signals/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol }),
    });
    const actionClass = signal.candidate_action === "BUY" ? "badge-good" : signal.candidate_action === "SELL" ? "badge-bad" : "badge-neutral";
    const riskClass = signal.risk_score === "LOW" ? "badge-good" : signal.risk_score === "HIGH" ? "badge-bad" : "badge-warn";
    resultEl.innerHTML = `
      <div class="signal-card">
        <div class="item-top">
          <strong>${escapeHtml(signal.symbol)}</strong>
          <span class="badge ${actionClass}">${escapeHtml(signal.candidate_action)}</span>
        </div>
        <dl class="kv-grid">
          <div><dt>Confidence</dt><dd>${formatPct(signal.confidence)}</dd></div>
          <div><dt>Risk</dt><dd><span class="badge ${riskClass}">${escapeHtml(signal.risk_score)}</span></dd></div>
          <div><dt>Size</dt><dd>${formatPct(signal.size_pct)}</dd></div>
          <div><dt>Model</dt><dd>${escapeHtml(signal.model_version || "—")}</dd></div>
        </dl>
        ${signal.research_summary ? `<p class="subtle-line">${escapeHtml(signal.research_summary.slice(0, 120))}</p>` : ""}
      </div>`;
    // Refresh signals panel
    setTimeout(refresh, 500);
  } catch (err) {
    resultEl.innerHTML = `<p class="trade-error">${escapeHtml(String(err))}</p>`;
  }
}

// Override readJson to support optional fetch options (e.g. POST for signal generation)
async function readJson(label, url, options) {
  const response = await fetch(url, { cache: "no-store", ...options });
  if (!response.ok) {
    throw new Error(`${label} returned ${response.status} from ${url}`);
  }
  return response.json();
}

// ---------------------------------------------------------------------------
// Worker run button
// ---------------------------------------------------------------------------

function attachWorkerRunHandler() {
  document.getElementById("btn-worker-run").addEventListener("click", async () => {
    const btn = document.getElementById("btn-worker-run");
    btn.disabled = true;
    btn.textContent = "Running...";
    try {
      const result = await readJson("worker run", `${CONFIG.strategyBaseUrl}/v1/worker/run`, {
        method: "POST",
      });
      btn.textContent = `Done (${result.result?.orders_submitted ?? 0} orders)`;
    } catch {
      btn.textContent = "Error";
    } finally {
      setTimeout(() => {
        btn.disabled = false;
        btn.textContent = "Run Now";
      }, 3000);
      setTimeout(refresh, 1000);
    }
  });

  document.getElementById("btn-generate-signal").addEventListener("click", generateQuickSignal);
}

function attachOrchestratorHandlers() {
  document.getElementById("kill-switch-toggle").addEventListener("click", async () => {
    try {
      const current = await readJson("orchestrator status", DATA_SOURCES.orchestratorStatus);
      await readJson("kill switch", `${CONFIG.orchestratorBaseUrl}/v1/orchestrator/kill-switch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ active: !current.kill_switch }),
      });
      setTimeout(refresh, 500);
    } catch (err) {
      document.getElementById("status").textContent = `Kill switch toggle failed: ${String(err)}`;
    }
  });
}

function attachValidationHandler() {
  document.getElementById("run-validation").addEventListener("click", async () => {
    const button = document.getElementById("run-validation");
    button.disabled = true;
    button.textContent = "Running...";
    try {
      const result = await readJson("validation", DATA_SOURCES.validation, { method: "POST" });
      renderValidation(result);
      document.getElementById("status").textContent = "Validation complete";
    } catch (err) {
      document.getElementById("status").textContent = `Validation failed: ${String(err)}`;
    } finally {
      button.disabled = false;
      button.textContent = "Run Validation";
    }
  });
}
