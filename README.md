# Kube Trader

Kube Trader is an open-source Python starter for a self-hosted stock research and automated trading pipeline. It separates the four workers in the requested flow into independent containers and Kubernetes pods. Alpaca paper trading is the default; `ALPACA_PAPER=false` selects Alpaca's live brokerage endpoint. See the [Alpaca market stream docs](https://alpaca.markets/sdks/python/api_reference/data/stock/live.html), [paper trading guide](https://docs.alpaca.markets/us/docs/paper-trading), and [bracket-order request docs](https://alpaca.markets/sdks/python/api_reference/trading/requests.html).

## Pipeline

```text
Alpaca minute bars ──► Redis stream ──► Signals/alpha factors ──► Redis stream ──► Paper/live execution
        │                        │                 │                                  │
        └──────────────► PostgreSQL ◄─────────────┴──── Rolling backtest metrics ────┘
                                                                                      │
                                                          account reconciliation/risk ┘
```

| Worker | Container command | Responsibility |
| --- | --- | --- |
| Ingestion | `python -m app.services.ingestion` | Alpaca IEX/SIP minute bars to PostgreSQL and the `market.bars` Redis stream |
| Signals | `python -m app.services.signals` | 5/20-bar SMA state changes, RSI(14), and `buy`/`sell` events in `strategy.signals` |
| Backtesting | `python -m app.services.backtesting` | Recalculates rolling 500-bar SMA returns, max drawdown, and per-bar Sharpe metric |
| Execution | `python -m app.services.execution` | Reconciles account/positions, enforces limits, and submits paper/live bracket orders on the selected endpoint |

FastAPI remains a separate API pod. Redis Streams carry events; PostgreSQL stores bars, strategy state, backtest metrics, account snapshots, allocation exposures, order reservations, and the daily risk state. The raw Kubernetes setup includes single-instance PostgreSQL and Redis StatefulSets with PVCs. The Helm chart deploys the same components.

## Risk and execution behavior

- **Endpoint selection:** `ALPACA_PAPER=true` selects Alpaca paper trading; `ALPACA_PAPER=false` selects the live brokerage endpoint. The same execution logic and risk checks apply to either endpoint. Use credentials for the selected endpoint.
- **Opt-in automation:** market streaming, automated order execution, and stop repair default off. For paper automation, set valid paper credentials and set `MARKET_STREAM_ENABLED=true`, `EXECUTION_ENABLED=true`, and `STOP_MANAGEMENT_ENABLED=true` after reviewing the behavior. Live trading additionally requires deliberately setting `ALPACA_PAPER=false` and supplying live credentials; keep `.env` out of Git.
- **Signal policy:** the sample strategy is a 5/20 minute-bar SMA state change with RSI included as an observable factor; RSI is not currently used as a trade filter. Entries are long-only; `below_sma` exits bot-managed long positions. It ignores stale signals and entries while the US market is closed.
- **Per-trade risk:** whole-share quantity is bounded by 1% of account equity divided by the configured stop distance. New entries receive Alpaca bracket orders with a stop loss and take-profit target. The default stop is 2%; the target is 4%.
- **Allocation:** default virtual pool targets are 20% intraday, 30% swing, and 50% core. Each new entry is limited to the remaining budget of its pool. This signal worker currently emits intraday signals only; swing and core budgets are tracked for allocation reporting, but do not receive generated strategies.
- **Drawdown breaker:** the worker uses Alpaca's same-day portfolio history for its New York session baseline, falling back to the account's previous-close equity if history is unavailable. A 3% or greater drop persists a halt for new entries until the next date and logs a critical event. If `ALERT_WEBHOOK_URL` is configured, it posts a generic JSON alert to that receiver. Existing bracket exits remain at the broker. It does not liquidate positions.
- **Reconciliation:** every 30 seconds the worker snapshots Alpaca equity, buying power, positions, tracked/untracked symbols, allocation exposure, and missing protective stops. When stop management is enabled, it restores stops for bot-managed positions only. Review `/account/reconciliation` and `/risk`.
- **Duplicate protection:** entry reservations use deterministic Alpaca client order IDs and a PostgreSQL uniqueness constraint. Redis events are acknowledged after processing.

This is a starter implementation, not a validated strategy or a production brokerage platform. The rolling backtest omits fees, slippage, survivorship bias, corporate actions, and out-of-sample validation. It does not short securities, stream Finnhub data, execute intraday account cash transfers, or send alerts outside container logs. An attached stop order cannot guarantee a fill price. Start with paper credentials and inspect orders, account state, and market-data terms before relying on the system.

## Local setup

Copy the sample environment file, then set API credentials for the selected endpoint. `.env` is ignored by Git. Leave `ALPACA_PAPER=true` for paper mode. Live mode requires `ALPACA_PAPER=false`, live API credentials, and `EXECUTION_ENABLED=true`; enabling that combination can place real orders.

```sh
cp .env.example .env
```

Start the complete stack with Docker Compose:

```sh
docker compose up --build
```

OpenAPI is at `http://localhost:8000/docs`. Useful endpoints:

```sh
curl http://localhost:8000/healthz
curl http://localhost:8000/symbols
curl http://localhost:8000/market-data/AAPL?days=90
curl http://localhost:8000/signals/AAPL
curl http://localhost:8000/backtests/AAPL
curl http://localhost:8000/account/reconciliation
curl http://localhost:8000/risk
```

The daily history API supports `MARKET_DATA_PROVIDER=yfinance` (default) or `finnhub` with `FINNHUB_API_KEY`. Intraday WebSocket ingestion uses Alpaca and `ALPACA_DATA_FEED=iex` by default. Market-data entitlements and availability depend on the selected provider and plan.

## Docker image

```sh
docker build -t kube-trader:latest .
docker run --rm -p 8000:8000 --env-file .env -v kube-trader-data:/data kube-trader:latest
```

The API container needs PostgreSQL and Redis for the account, risk, and backtest endpoints. Use Compose for the whole stack.

## Kubernetes manifests

Build/publish the image and replace each `kube-trader:latest` in `k8s/app.yaml` with the registry image. Create the namespace and secret first. Never commit the secret:

```sh
kubectl apply -f k8s/namespace.yaml
kubectl -n kube-trader create secret generic kube-trader-secrets \
  --from-literal=POSTGRES_PASSWORD='REPLACE_WITH_A_RANDOM_PASSWORD' \
  --from-literal=POSTGRES_DSN='postgresql://kube_trader:REPLACE_WITH_A_RANDOM_PASSWORD@postgres:5432/kube_trader' \
  --from-literal=ALPACA_API_KEY='' \
  --from-literal=ALPACA_SECRET_KEY='' \
  --from-literal=FINNHUB_API_KEY='' \
  --from-literal=ALERT_WEBHOOK_URL=''
kubectl apply -f k8s/platform.yaml -f k8s/app.yaml
```

Update both Postgres password and DSN using the same password. After adding credentials or changing ConfigMap values, restart the affected deployments so the pods receive the new environment. Use a cluster secret manager for real credentials. The API service is `ClusterIP`; expose it only through a private, authenticated path. SQLite's API history cache is a separate 2 GiB PVC. PostgreSQL and Redis each use persistent volumes.

## Helm

Prepare the `kube-trader-secrets` Secret in the release namespace with the same keys. Set `POSTGRES_DSN` to use the release's Postgres service name (for release `kube-trader`, that is `kube-trader-postgres`). Then install:

```sh
helm upgrade --install kube-trader ./helm/kube-trader \
  --namespace kube-trader --create-namespace \
  --set image.repository=YOUR_REGISTRY/kube-trader \
  --set image.tag=YOUR_TAG
```

The chart defaults to paper mode and all stream/execution switches to off. To enable paper automation after setting up Alpaca paper credentials, set `config.marketStreamEnabled=true`, `config.executionEnabled=true`, and `config.stopManagementEnabled=true`. For live mode, also set `config.alpacaPaper=false` and provide live credentials in the Kubernetes Secret. Use `helm/kube-trader/values.yaml` for worker limits and PVC sizes.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARKET_DATA_PROVIDER` | `yfinance` | Daily history API source: `yfinance` or `finnhub` |
| `FINNHUB_API_KEY` | empty | Finnhub history API key |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | empty | Alpaca credentials for streaming and trading, matching the selected endpoint |
| `ALPACA_PAPER` | `true` | Selects paper (`true`) or live (`false`) trading endpoint |
| `ALPACA_DATA_FEED` | `iex` | Alpaca stock feed (`iex` or `sip`, subject to entitlements) |
| `MARKET_STREAM_ENABLED` | `false` | Starts the Alpaca minute-bar WebSocket consumer |
| `EXECUTION_ENABLED` | `false` | Enables automated entries/exits on the selected endpoint |
| `STOP_MANAGEMENT_ENABLED` | `false` | Repairs missing stops on bot-managed positions |
| `MAX_TRADE_RISK_PCT` | `0.01` | Maximum account-equity loss budget per entry |
| `DAILY_DRAWDOWN_LIMIT_PCT` | `0.03` | Entry halt threshold for the current New York date |
| `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` | `0.02` / `0.04` | Default bracket exit distances |
| `INTRADAY_ALLOCATION_PCT` / `SWING_ALLOCATION_PCT` / `CORE_ALLOCATION_PCT` | `0.20` / `0.30` / `0.50` | Virtual portfolio pool targets; must sum to 1.0 |
| `SIGNAL_FAST_BARS` / `SIGNAL_SLOW_BARS` | `5` / `20` | SMA periods, in minute bars |
| `RECONCILE_INTERVAL_SECONDS` | `30` | Brokerage reconciliation interval |
| `MAX_SIGNAL_AGE_SECONDS` | `180` | Reject older or future-dated signals |
| `ALERT_WEBHOOK_URL` | empty | Optional generic JSON circuit-breaker webhook receiver |
| `POSTGRES_DSN` / `REDIS_URL` | local defaults | Database and stream-broker connection strings |

