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
| Signals and alpha factors | `python -m app.services.signals` | 5/20-minute intraday SMA, daily swing 20-SMA, daily core 50/200-SMA, and RSI(14); emits events to `strategy.signals` |
| Backtesting and risk metrics | `python -m app.services.backtesting` | Rolling 500-bar sample metrics: cumulative return, maximum drawdown, per-bar Sharpe |
| Execution and automation | `python -m app.services.execution` | Account reconciliation, pool/risk checks, intraday brackets and swing/core OTO protective-stop orders |

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
| `SWING_ATR_PERIOD` / `SWING_ATR_MULTIPLE` | `14` / `2.0` | Daily ATR period and stop multiple for swing orders |
| `CORE_STOP_LOSS_PCT` | `0.20` | Core GTC protective stop distance |
| `ETF_HOLDINGS_SYMBOL` | `SPY` | ETF symbol identifying the holdings source |
| `ETF_HOLDINGS_URL` | State Street SPY XLSX | HTTPS issuer workbook URL for ETF constituents |
| `ETF_MAX_HOLDINGS` | `100` | Largest holdings by fund weight screened per refresh; minimum 20 |
| `UNIVERSE_REFRESH_SECONDS` | `86400` | Holdings and subscription refresh schedule; minimum 3600 seconds |
| `UNIVERSE_SCREEN_ENABLED` | `true` | Screen ETF holdings at ingestion startup and scheduled refreshes |
| `MIN_SYMBOL_PRICE` / `MIN_AVG_DOLLAR_VOLUME` | `5` / `20000000` | Price and 20-day average dollar-volume floors |
| `MAX_WATCHLIST_SYMBOLS` | `40` | Maximum symbols subscribed after screening |
| `MAX_OPEN_POSITIONS` | `20` | Maximum total broker positions before rejecting an entry |
| `MAX_SYMBOL_EXPOSURE_PCT` / `MAX_SECTOR_EXPOSURE_PCT` | `0.05` / `0.25` | Maximum equity exposure by symbol and sector group |
| `BACKTEST_COST_BPS` / `BACKTEST_TRAIN_FRACTION` | `5` / `0.70` | Research endpoint cost assumption and chronological split |
| `INTRADAY_BACKTEST_BARS` | `50000` | Locally stored minute bars available to research backtests |
| `MAX_SIGNAL_AGE_SECONDS` | `180` | Maximum signal age accepted by execution |
| `RECONCILE_INTERVAL_SECONDS` | `30` | Account and position reconciliation interval |
| `ALERT_WEBHOOK_URL` | empty | Optional receiver for generic JSON breaker alerts |
| `POSTGRES_DSN` / `REDIS_URL` | local defaults | Persistence and event-broker connections |

The three allocation percentages must be nonnegative and sum to `1.0`. Never commit real values to `.env.example` or Helm values.

## Signal and backtest behavior

For intraday signals, the worker reads recent minute closes from PostgreSQL, compares the fast and slow SMAs, and stores the current above/below state. On a state change it publishes action, pool, price/time, SMA values, RSI(14), and bracket percentages.

For daily strategies, it fetches daily bars using `MARKET_DATA_PROVIDER`, excludes the current unfinished session bar, and caches the result during the New York trading date. Swing enters on a completed close crossing above the 20-day SMA and exits on a cross below; its protective stop defaults to 2x 14-day ATR. Core enters when the 50-day SMA crosses above the 200-day SMA and exits on the reverse cross; its protective stop defaults to 20%. Daily crossover state is persisted per symbol/pool/bar date, so repeated minute events do not duplicate daily signals. RSI remains informational and does not filter orders.

The backtesting worker recalculates only the intraday SMA simulation over up to 500 recent minute bars whenever a new bar arrives. `/research/backtests/{symbol}` separately compares all three rules, applies a configurable per-side cost estimate, and reports a chronological in-sample/out-of-sample split plus buy-and-hold comparison. The research endpoint uses up to 50,000 locally collected minute bars for intraday and up to the requested daily history for swing/core. Signals are assumed filled at bar close; minute Sharpe annualization is approximate. Results omit market impact, taxes, historical universe membership, and corporate-action timing and must not be treated as expected performance.

The ingestion worker downloads the configured ETF issuer XLSX holdings at startup and every `UNIVERSE_REFRESH_SECONDS` (24 hours by default), screens holdings by minimum price and 20-day average dollar volume, then subscribes to the most liquid eligible symbols up to `MAX_WATCHLIST_SYMBOLS`. It retains active subscriptions when a scheduled refresh fails. This is limited to one fund's published constituents, not all exchange listings. `/universe` reports the latest screen. New entries are limited by pool budgets, buying power, `MAX_OPEN_POSITIONS`, `MAX_SYMBOL_EXPOSURE_PCT`, and `MAX_SECTOR_EXPOSURE_PCT`; existing untracked positions count toward symbol/sector exposure. Custom symbols without a `SYMBOL_SECTORS` mapping are grouped under `unclassified`.

## Allocation and order sizing

Each pool's target equity is `account_equity * target_ratio`. Broker position market values are assigned to their recorded bot pool; untracked account positions are conservatively counted against core exposure. Entry size is the lowest whole-share count allowed by:

1. `account_equity * min(MAX_TRADE_RISK_PCT, 1%) / (signal_price * stop_loss_fraction)`;
2. the pool's remaining target budget and broker buying power; and
3. a 100-share per-order ceiling.

All three pools generate signals. Intraday entries use day bracket orders with default 2% stop and 4% take-profit. Swing and core entries use GTC OTO orders with a single protective stop and no fixed take-profit; their daily trend exits close the bot-managed pool position after canceling its attached exit order. The 20/30/50 pool balances are virtual limits inside one brokerage account: the application does not transfer cash between accounts or automatically sell holdings to rebalance pool ratios.

Intraday buy signals submit long-only Alpaca market bracket orders on the selected endpoint, with stop-loss and take-profit child orders. Swing/core buys submit GTC OTO market orders with one protective stop and no fixed take-profit. The execution worker ignores stale/future signals and will not enter when the US equity market is closed. A matching pool's trend exit cancels its open protective closing leg and closes the bot-managed position. The daily breaker blocks new entries but still allows strategy exits.

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
