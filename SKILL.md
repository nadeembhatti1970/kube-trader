# Kube Trader Pipeline and Risk Skill

## Metadata

- **Name:** `kube-trader-pipeline`
- **Version:** 2.0.0
- **Target:** Docker Compose or Kubernetes/Helm deployment
- **Purpose:** Describe how the implemented data, signal, backtest, paper/live execution, allocation, reconciliation, and circuit-breaker workers operate.

Paper trading is the default. Setting `ALPACA_PAPER=false` selects live trading when execution is enabled. This does not claim the sample strategy is profitable.

## Components and event flow

| Stage | Entry point | Input/output |
| --- | --- | --- |
| Ingestion | `python -m app.services.ingestion` | Alpaca minute-bar WebSocket to PostgreSQL and Redis stream `market.bars` |
| Signals and alpha factors | `python -m app.services.signals` | 5/20-bar SMA state changes and RSI(14); emits `buy`/`sell` events to `strategy.signals` |
| Backtesting and risk metrics | `python -m app.services.backtesting` | Rolling 500-bar sample metrics: cumulative return, maximum drawdown, per-bar Sharpe |
| Execution and automation | `python -m app.services.execution` | Account reconciliation, pool and risk checks, optional Alpaca paper/live bracket orders |

FastAPI is a separate service. Redis Streams decouple worker events; PostgreSQL persists bars, strategy state, metrics, reservations, account snapshots, allocation exposure, and daily risk state. The API's daily history cache uses SQLite on `/data`.

Alpaca's `StockDataStream` supplies minute bars. The configured feed defaults to IEX; SIP availability depends on the account's market-data entitlements. Finnhub and yfinance are used by the API's daily-history routes, not the intraday WebSocket worker.

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `ALPACA_PAPER` | `true` | `true` selects paper endpoint; `false` selects live endpoint |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | empty | Alpaca credentials matching the selected endpoint |
| `MARKET_STREAM_ENABLED` | `false` | Allows the ingestion worker to start the stream |
| `EXECUTION_ENABLED` | `false` | Allows signal-driven entries/exits on the selected endpoint |
| `STOP_MANAGEMENT_ENABLED` | `false` | Repairs missing stops on bot-managed positions |
| `ALPACA_DATA_FEED` | `iex` | Alpaca stock stream feed, `iex` or `sip` |
| `MAX_TRADE_RISK_PCT` | `0.01` | Requested risk budget, capped in code at 1% of equity |
| `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` | `0.02` / `0.04` | Default bracket distances |
| `DAILY_DRAWDOWN_LIMIT_PCT` | `0.03` | Session loss threshold for new-entry halt |
| `INTRADAY_ALLOCATION_PCT` | `0.20` | Intraday virtual pool target |
| `SWING_ALLOCATION_PCT` | `0.30` | Swing virtual pool target |
| `CORE_ALLOCATION_PCT` | `0.50` | Core virtual pool target |
| `SIGNAL_FAST_BARS` / `SIGNAL_SLOW_BARS` | `5` / `20` | SMA periods measured in minute bars |
| `MAX_SIGNAL_AGE_SECONDS` | `180` | Maximum signal age accepted by execution |
| `RECONCILE_INTERVAL_SECONDS` | `30` | Account and position reconciliation interval |
| `ALERT_WEBHOOK_URL` | empty | Optional receiver for generic JSON breaker alerts |
| `POSTGRES_DSN` / `REDIS_URL` | local defaults | Persistence and event-broker connections |

The three allocation percentages must be nonnegative and sum to `1.0`. Never commit real values to `.env.example` or Helm values.

## Signal and backtest behavior

For each symbol, the signal worker reads recent closes from PostgreSQL, compares the fast and slow SMAs, and stores the current above/below state. On a state change it publishes the symbol, action, pool, bar price/time, SMA values, RSI(14), and bracket percentages. RSI is informational and does not currently filter orders.

The backtesting worker recalculates a rolling SMA simulation over up to 500 recent bars whenever a new bar arrives. Its Sharpe value is a per-bar ratio, not annualized. Results do not include fees, slippage, corporate actions, or out-of-sample validation and must not be treated as a production evaluation.

## Allocation and order sizing

Each pool's target equity is `account_equity * target_ratio`. Broker position market values are assigned to their recorded bot pool; untracked account positions are conservatively counted against core exposure. Entry size is the lowest whole-share count allowed by:

1. `account_equity * min(MAX_TRADE_RISK_PCT, 1%) / (signal_price * stop_loss_fraction)`;
2. the pool's remaining target budget and broker buying power; and
3. a 100-share per-order ceiling.

Only intraday signals are generated today. Swing and core budgets are reported but have no active signal strategies. Pool balances are virtual limits inside one brokerage account: the application does not transfer cash between accounts or automatically sell holdings to rebalance pool ratios.

Buy signals submit long-only Alpaca market bracket orders on the selected endpoint, with a stop-loss and take-profit child order. The execution worker ignores stale/future signals and will not enter when the US equity market is closed. A below-SMA signal closes a bot-managed position unless the daily breaker is active; existing broker-side bracket exits remain in place.

## Reconciliation and circuit breaker

Every reconciliation reads Alpaca account equity, buying power, positions, and open nested orders. It stores account snapshots, reports untracked/missing positions and pool exposures, and identifies absent protective stops. With `STOP_MANAGEMENT_ENABLED=true`, it submits a protective stop for a bot-managed position missing an open stop order.

The daily baseline uses Alpaca's same-day 1-minute portfolio history during market hours. If unavailable, it falls back to `account.last_equity` (previous close) or current equity. At a drawdown equal to or greater than `DAILY_DRAWDOWN_LIMIT_PCT`, the worker persists the halt for the current New York date and blocks new automated entries. It emits a critical log and, when configured, posts a JSON object containing severity, service, alert name, message, and timestamp to `ALERT_WEBHOOK_URL`. The state resets on the next New York calendar date after a new reconciliation.

The breaker does not terminate a pod, cancel bracket exits, or liquidate positions. If the webhook is absent or delivery fails, the critical log remains the only alert channel.

## Operational endpoints and deployment

- `GET /healthz`: API process liveness.
- `GET /readyz`: PostgreSQL connectivity.
- `GET /backtests/{symbol}`: latest rolling metrics.
- `GET /account/reconciliation`: latest account and allocation snapshot.
- `GET /risk`: current New York-date breaker state.

Use `docker compose up --build` for the local stack. Kubernetes manifests live in `k8s/`; the Helm chart is in `helm/kube-trader/`. The API is internal by default. Configure credentials matching the selected Alpaca endpoint, webhook secrets, persistent storage, and private network access before enabling the stream or execution switches. Set `ALPACA_PAPER=false` only when live trading is intended; `EXECUTION_ENABLED` remains false by default.
