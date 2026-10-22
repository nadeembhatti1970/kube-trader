# Kube Trader

Kube Trader is an open-source Python starter for a self-hosted stock research and automated trading pipeline. It separates the four workers in the requested flow into independent containers and Kubernetes pods. Alpaca paper trading is the default; `ALPACA_PAPER=false` selects Alpaca's live brokerage endpoint. See the [Alpaca market stream docs](https://alpaca.markets/sdks/python/api_reference/data/stock/live.html), [paper trading guide](https://docs.alpaca.markets/us/docs/paper-trading), and [bracket-order request docs](https://alpaca.markets/sdks/python/api_reference/trading/requests.html).

## Pipeline

```text
Alpaca minute bars ──► Redis stream ──► Intraday signals ───────┐
                                                                ├──► Redis stream ──► Paper/live execution
Daily bars (yfinance/Finnhub) ──────────► Swing/core signals ──┘
        │                        │                 │                                  │
        └──────────────► PostgreSQL ◄─────────────┴──── Rolling backtest metrics ────┘
                                                                                      │
                                                          account reconciliation/risk ┘
```

| Worker | Container command | Responsibility |
| --- | --- | --- |
| Ingestion | `python -m app.services.ingestion` | Alpaca IEX/SIP minute bars to PostgreSQL and the `market.bars` Redis stream |
| Signals | `python -m app.services.signals` | Intraday 5/20-minute SMA, daily swing 20-SMA, daily core 50/200-SMA, and RSI(14) events in `strategy.signals` |
| Backtesting | `python -m app.services.backtesting` | Recalculates rolling 500-bar SMA returns, max drawdown, and per-bar Sharpe metric |
| Execution | `python -m app.services.execution` | Reconciles account/positions, enforces limits, and submits paper/live bracket or OTO protective-stop orders |

FastAPI remains a separate API pod. Redis Streams carry events; PostgreSQL stores bars, strategy state, backtest metrics, account snapshots, allocation exposures, order reservations, and the daily risk state. The raw Kubernetes setup includes single-instance PostgreSQL and Redis StatefulSets with PVCs. The Helm chart deploys the same components.

## Universe and concentration limits

At startup, ingestion downloads the configured ETF holdings workbook (SPY from State Street by default), then screens the largest `ETF_MAX_HOLDINGS` constituents by fund weight (100 by default) for at least 20 completed daily bars, a price of at least `MIN_SYMBOL_PRICE`, and 20-day average dollar volume of at least `MIN_AVG_DOLLAR_VOLUME`. It subscribes to the most liquid eligible symbols up to `MAX_WATCHLIST_SYMBOLS` and refreshes the holdings and subscriptions every `UNIVERSE_REFRESH_SECONDS` (24 hours by default). If a refresh fails, the worker keeps its current subscriptions. This is an ETF-constituent universe, not a market-wide scanner or a portfolio rebalance. Review `/universe` for the latest holdings and screening results; `/symbols` lists the selected symbols. State Street publishes SPY holdings daily; its workbook structure and access can change.

New orders are constrained by pool budget, buying power, `MAX_OPEN_POSITIONS`, per-symbol exposure, and per-sector exposure. Defaults cap each symbol at 5% of equity and each mapped sector at 25%. Existing broker positions, including untracked positions, count toward those caps. Built-in sector groups cover the original sample tickers; assign additional ETF constituents with `SYMBOL_SECTORS=XYZ:technology,ABC:healthcare`. Unmapped symbols share an `unclassified` group, which is also capped.

## Risk and execution behavior

- **Endpoint selection:** `ALPACA_PAPER=true` selects Alpaca paper trading; `ALPACA_PAPER=false` selects the live brokerage endpoint. The same execution logic and risk checks apply to either endpoint. Use credentials for the selected endpoint.
- **Opt-in automation:** market streaming, automated order execution, and stop repair default off. For paper automation, set valid paper credentials and set `MARKET_STREAM_ENABLED=true`, `EXECUTION_ENABLED=true`, and `STOP_MANAGEMENT_ENABLED=true` after reviewing the behavior. Live trading additionally requires deliberately setting `ALPACA_PAPER=false` and supplying live credentials; keep `.env` out of Git.
- **Signal policy:** intraday uses 5/20-minute SMA state changes; swing enters when a completed daily close crosses above its 20-day SMA and exits below it; core enters on a 50/200-day golden cross and exits on a death cross. Daily bars come from `MARKET_DATA_PROVIDER` and the current unfinished daily bar is excluded. Signals are emitted once per completed daily bar. RSI is included as an observable factor for intraday signals and is not a trade filter. All strategies are long-only and reject stale signals and entries while the US market is closed.
- **Per-trade risk:** whole-share quantity is bounded by 1% of account equity divided by the configured stop distance. Intraday entries use day bracket orders with the default 2% stop and 4% target. Swing entries use a GTC protective stop at 2x daily ATR by default, without a fixed take-profit; core entries use a GTC protective stop defaulting to 20%, without a fixed take-profit. Swing/core exits also follow their daily trend rules.
- **Allocation:** default virtual pool targets are 20% intraday, 30% swing, and 50% core. Each new entry is limited by the remaining budget of its pool. The pools remain virtual partitions in one brokerage account; they do not transfer cash or automatically rebalance positions.
- **Drawdown breaker:** the worker uses Alpaca's same-day portfolio history for its New York session baseline, falling back to the account's previous-close equity if history is unavailable. A 3% or greater drop persists a halt for new entries until the next date and logs a critical event. If `ALERT_WEBHOOK_URL` is configured, it posts a generic JSON alert to that receiver. Existing bracket exits remain at the broker. It does not liquidate positions.
- **Reconciliation:** every 30 seconds the worker snapshots Alpaca equity, buying power, positions, tracked/untracked symbols, allocation exposure, and missing protective stops. When stop management is enabled, it restores stops for bot-managed positions only. Review `/account/reconciliation` and `/risk`.
- **Duplicate protection:** entry reservations use deterministic Alpaca client order IDs and a PostgreSQL uniqueness constraint. Redis events are acknowledged after processing.

This is a starter implementation, not a validated strategy or a production brokerage platform. The rolling backtest covers only the intraday SMA strategy and omits fees, slippage, survivorship bias, corporate actions, and out-of-sample validation; swing and core rules have not been backtested by this worker. It does not short securities, stream Finnhub data, execute intraday account cash transfers, or send alerts outside configured webhooks/container logs. Stop orders cannot guarantee a fill price. Start with paper credentials and inspect orders, account state, and market-data terms before relying on the system.

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
curl http://localhost:8000/research/backtests/AAPL
curl http://localhost:8000/universe
curl http://localhost:8000/account/reconciliation
curl http://localhost:8000/risk
```

The daily history API supports `MARKET_DATA_PROVIDER=yfinance` (default) or `finnhub` with `FINNHUB_API_KEY`. Intraday WebSocket ingestion uses Alpaca and `ALPACA_DATA_FEED=iex` by default. Market-data entitlements and availability depend on the selected provider and plan.

`/research/backtests/{symbol}` compares the 5/20-minute, 20-day swing, and 50/200-day core rules. It reports chronological in-sample and out-of-sample returns, drawdown, estimated per-side costs, trade counts, buy-and-hold returns, and annualized Sharpe. Defaults use 70% of each series for in-sample and 5 basis points per side; query parameters can override `train_fraction`, `cost_bps`, and daily `days`. Daily evaluation uses up to 1,260 bars; intraday evaluation uses up to `INTRADAY_BACKTEST_BARS` locally collected bars. Results are estimates with close-based fills, so they do not reproduce spreads, slippage, or market impact. Compare results across market regimes and symbols before changing execution settings.

## Docker image

```sh
docker build -t kube-trader:latest .
docker run --rm -p 8000:8000 --env-file .env -v kube-trader-data:/data kube-trader:latest
```

The API container needs PostgreSQL and Redis for the account, risk, and backtest endpoints. Use Compose for the whole stack.

## Kubernetes manifests

Build/publish the image and replace each `kube-trader:latest` in `k8s/app.yaml` with the registry image. Set `POSTGRES_DSN` in `.env` to use the in-cluster PostgreSQL service (`postgres` for these manifests). Create the namespace and Kubernetes Secret first:

```sh
bash ./create-k8s-secret.sh
kubectl apply -f k8s/platform.yaml -f k8s/app.yaml
```

The `create-k8s-secret.sh` utility defaults to `.env` in the project root and reads the default namespace from `k8s/namespace.yaml`. It creates that namespace (or the namespace passed with `--namespace`) before creating or updating the Secret. It includes only `POSTGRES_PASSWORD`, `POSTGRES_DSN`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `FINNHUB_API_KEY`, and `ALERT_WEBHOOK_URL`; other application settings remain in the ConfigMap. Secret entries must use unquoted `KEY=value` lines. Use `--env-file`, `--namespace`, or `--secret-name` to override the defaults.

PostgreSQL's role and database are both named `kube_trader` (underscores are valid here); the Kubernetes Service uses a hyphenated name. For the default Helm release, use host `kube-trader-postgres`; for the raw manifests, use host `postgres`. For example, with Helm:

```text
POSTGRES_DSN=postgresql://kube_trader:<POSTGRES_PASSWORD>@kube-trader-postgres:5432/kube_trader
```

Replace the placeholder with the same password as `POSTGRES_PASSWORD`. URL-encode special characters in the password before putting it in the DSN.

Update both Postgres password and DSN using the same password. After adding credentials or changing ConfigMap values, restart the affected deployments so the pods receive the new environment. Use a cluster secret manager for real credentials. The API service is `ClusterIP`; expose it only through a private, authenticated path. SQLite's API history cache is a separate 2 GiB PVC. PostgreSQL and Redis each use persistent volumes.

## GitHub Actions and Docker Hub

Dependabot checks GitHub Actions, Python requirements, Docker images in the root and Kubernetes manifests, and the Helm chart weekly on Sunday at 03:00 Europe/London, following the schedule used by `core-infra`.

The Docker image workflow builds and pushes `DOCKERHUB_USERNAME/kube-trader:latest` and a commit-SHA tag when application code, requirements, or the Docker build files change on `main` (and can be started manually). After publishing, it updates `helm/kube-trader/values.yaml` to use that same SHA tag, increments the chart patch version in `Chart.yaml`, commits the change, and dispatches the Helm workflow. The Helm workflow runs `helm lint`, packages `helm/kube-trader`, and pushes it as an OCI chart to `oci://registry-1.docker.io/DOCKERHUB_USERNAME`. Manually changing the chart should still include an appropriate version bump.

Add these repository Actions secrets before the first publish:

- `DOCKERHUB_USERNAME`: the Docker Hub account or organization namespace.
- `DOCKERHUB_TOKEN`: a Docker Hub personal access token with read/write access to repositories.

The Docker workflow automatically increments the chart patch version when it updates the image tag; manually changing chart contents should also include an appropriate version bump. Install the published chart with `helm upgrade --install kube-trader oci://registry-1.docker.io/DOCKERHUB_USERNAME/kube-trader --version CHART_VERSION --namespace kube-trader --create-namespace`, and set `image.repository` to `DOCKERHUB_USERNAME/kube-trader`. The chart's default `image.tag` is updated to the published commit-SHA tag by the Docker workflow. Docker Hub supports Helm charts as OCI artifacts in the same repository namespace as the container image.

## Helm

Set `POSTGRES_DSN` in `.env` to use the release's Postgres service name (for release `kube-trader`, that is `kube-trader-postgres`). Create the namespace and Secret, then install:

```sh
bash ./create-k8s-secret.sh
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
| `ETF_HOLDINGS_SYMBOL` | `SPY` | ETF whose published holdings supply universe candidates |
| `ETF_HOLDINGS_URL` | State Street SPY holdings workbook | HTTPS URL for an issuer XLSX holdings file |
| `ETF_MAX_HOLDINGS` | `100` | Largest fund constituents by holding weight to screen per refresh (minimum 20) |
| `UNIVERSE_REFRESH_SECONDS` | `86400` | Holdings refresh interval (minimum 3600 seconds) |
| `UNIVERSE_SCREEN_ENABLED` | `true` | Filters candidates by price and average dollar volume at ingestion startup |
| `MIN_SYMBOL_PRICE` / `MIN_AVG_DOLLAR_VOLUME` | `5` / `20000000` | Minimum close price and 20-day average dollar volume in USD |
| `MAX_WATCHLIST_SYMBOLS` | `40` | Maximum selected symbols subscribed to the market stream |
| `SYMBOL_SECTORS` | empty | Optional comma-separated `SYMBOL:sector` overrides for custom symbols |
| `MAX_OPEN_POSITIONS` | `20` | Maximum total account positions before skipping new entries |
| `MAX_SYMBOL_EXPOSURE_PCT` / `MAX_SECTOR_EXPOSURE_PCT` | `0.05` / `0.25` | Account-equity limits for a symbol and its configured sector group |
| `MARKET_STREAM_ENABLED` | `false` | Starts the Alpaca minute-bar WebSocket consumer |
| `EXECUTION_ENABLED` | `false` | Enables automated entries/exits on the selected endpoint |
| `STOP_MANAGEMENT_ENABLED` | `false` | Repairs missing stops on bot-managed positions |
| `MAX_TRADE_RISK_PCT` | `0.01` | Maximum account-equity loss budget per entry |
| `DAILY_DRAWDOWN_LIMIT_PCT` | `0.03` | Entry halt threshold for the current New York date |
| `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` | `0.02` / `0.04` | Default bracket exit distances |
| `INTRADAY_ALLOCATION_PCT` / `SWING_ALLOCATION_PCT` / `CORE_ALLOCATION_PCT` | `0.20` / `0.30` / `0.50` | Virtual portfolio pool targets; must sum to 1.0 |
| `SIGNAL_FAST_BARS` / `SIGNAL_SLOW_BARS` | `5` / `20` | SMA periods, in minute bars |
| `SWING_ATR_PERIOD` / `SWING_ATR_MULTIPLE` | `14` / `2.0` | Daily ATR stop distance for swing orders |
| `CORE_STOP_LOSS_PCT` | `0.20` | Core GTC protective stop distance (20% default) |
| `BACKTEST_COST_BPS` / `BACKTEST_TRAIN_FRACTION` | `5` / `0.70` | Research endpoint's estimated one-way costs and chronological train split |
| `INTRADAY_BACKTEST_BARS` | `50000` | Maximum locally stored minute bars used by research backtests |
| `RECONCILE_INTERVAL_SECONDS` | `30` | Brokerage reconciliation interval |
| `MAX_SIGNAL_AGE_SECONDS` | `180` | Reject older or future-dated signals |
| `ALERT_WEBHOOK_URL` | empty | Optional generic JSON circuit-breaker webhook receiver |
| `POSTGRES_DSN` / `REDIS_URL` | local defaults | Database and stream-broker connection strings |
