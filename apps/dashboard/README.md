# Trade Pilot Dashboard

Interactive operator dashboard for the Trade Pilot trading stack. Milestone 2 upgrade adds live charts, manual trades, AI research, and a price ticker.

## Serving

The dashboard is a static single-page app. Serve it from a local HTTP server (required — `file://` origin blocks CORS):

```bash
python3 -m http.server 8080 --directory apps/dashboard
# or
make run-dashboard
```

Then open **http://localhost:8080**

## Features

- **Live Ticker Bar** — real-time prices + % change, auto-refreshes every 60s
- **Price Chart** — TradingView Lightweight Charts candlestick + EMA-20/50 overlays
- **Technical Indicators** — RSI, MACD histogram, Bollinger Bands below chart
- **Manual Trade Panel** — BUY / SELL form → policy check → execution
- **AI Signal Generator** — generate and preview a signal per symbol
- **Wallet Panel** — buying power, equity, cash + PAPER/LIVE mode badge
- **Worker Status** — last/next run + "Run Now" trigger button
- **AI Research Panel** — per-symbol sentiment, headlines, risk factors
- **Pipeline Metrics** — signals, policy decisions, orders, fills, PnL
- **Lifecycle Drill-down** — full signal → policy → order → fill → position chain
- **Positions & Rejections** — holdings and rejection reason breakdown

## Service Dependencies

| Service | Default URL | Purpose |
|---|---|---|
| Strategy + Market Data | http://localhost:8003 | Signals, charts, quotes, trades |
| Policy | http://localhost:8001 | Policy evaluations |
| Execution + Account | http://localhost:8002 | Orders, fills, wallet balance |
| Portfolio | http://localhost:8004 | Positions, PnL |
| Research | http://localhost:8005 | AI research reports |

Override via `window.TRADE_PILOT_CONFIG` in `index.html`.
