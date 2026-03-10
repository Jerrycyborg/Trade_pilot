const DATA_SOURCES = {
  signals: "./mock/latest-signals.json",
  policyDecisions: "./mock/policy-decisions.json",
  orders: "./mock/orders.json",
  fills: "./mock/fills.json",
  positions: "./mock/positions.json",
};

async function readJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${url} returned ${response.status}`);
  }
  return response.json();
}

function render(id, value) {
  document.getElementById(id).textContent = JSON.stringify(value, null, 2);
}

function rejectionReasons(orders) {
  return orders
    .filter((order) => order.rejection_reason)
    .map((order) => ({
      order_id: order.order_id,
      symbol: order.symbol,
      rejection_reason: order.rejection_reason,
    }));
}

async function refresh() {
  const status = document.getElementById("status");
  status.textContent = "Refreshing...";

  try {
    const [signals, policyDecisions, orders, fills, positions] = await Promise.all([
      readJson(DATA_SOURCES.signals),
      readJson(DATA_SOURCES.policyDecisions),
      readJson(DATA_SOURCES.orders),
      readJson(DATA_SOURCES.fills),
      readJson(DATA_SOURCES.positions),
    ]);

    render("signals", signals);
    render("policy-decisions", policyDecisions);
    render("orders", orders);
    render("fills", fills);
    render("positions", positions);
    render("rejections", rejectionReasons(orders));
    status.textContent = `Last refresh: ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    status.textContent = `Refresh failed: ${error.message}`;
  }
}

document.getElementById("refresh").addEventListener("click", refresh);
refresh();
