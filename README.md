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
| Backtesting | `python -m app.services.backtesting` | Recalculates rolling 500-bar OHLC execution-model returns, drawdown, Sharpe, and model provenance |
| Execution | `python -m app.services.execution` | Reconciles account/positions, enforces limits, and submits paper/live bracket or OTO protective-stop orders |

FastAPI remains a separate API pod. Redis Streams carry events; PostgreSQL stores bars, strategy state, backtest metrics, account snapshots, allocation exposures, order reservations, and the daily risk state. The raw Kubernetes setup includes single-instance PostgreSQL and Redis StatefulSets with PVCs. The Helm chart deploys the same components.

## Universe and concentration limits

At startup, ingestion downloads the configured ETF holdings workbook (SPY from State Street by default), then screens the largest `ETF_MAX_HOLDINGS` constituents by fund weight (100 by default) for at least 20 completed daily bars, a price of at least `MIN_SYMBOL_PRICE`, and 20-day average dollar volume of at least `MIN_AVG_DOLLAR_VOLUME`. It subscribes to the most liquid eligible symbols up to `MAX_WATCHLIST_SYMBOLS` and refreshes the holdings and subscriptions every `UNIVERSE_REFRESH_SECONDS` (24 hours by default). If a refresh fails, the worker keeps its current subscriptions. This is an ETF-constituent universe, not a market-wide scanner or a portfolio rebalance. Review `/universe` for the latest holdings and screening results; `/symbols` lists the selected symbols. State Street publishes SPY holdings daily; its workbook structure and access can change.

New orders are constrained by pool budget, buying power, `MAX_OPEN_POSITIONS`, per-symbol exposure, and per-sector exposure. Defaults cap each symbol at 5% of equity and each mapped sector at 25%. Existing broker positions, including untracked positions, count toward those caps. Built-in sector groups cover the original sample tickers; assign additional ETF constituents with `SYMBOL_SECTORS=XYZ:technology,ABC:healthcare`. Unmapped symbols share an `unclassified` group, which is also capped.

## Risk and execution behavior

- **Endpoint selection:** `ALPACA_PAPER=true` selects Alpaca paper trading; `ALPACA_PAPER=false` selects the live brokerage endpoint. The same execution logic and risk checks apply to either endpoint. Use credentials for the selected endpoint.
- **Entry pause:** `EXECUTION_ENABLED` is the master switch for signal-driven orders. With it enabled, `NEW_ENTRIES_ENABLED=false` blocks new buys but preserves managed signal exits; stop repair remains controlled separately. New entries default off in the local environment but on in the paper-mode Helm chart.
- **Opt-in automation:** market streaming, automated order execution, new entries, and stop repair default off in the local environment. For paper automation, set valid paper credentials and deliberately enable `MARKET_STREAM_ENABLED=true`, `EXECUTION_ENABLED=true`, `NEW_ENTRIES_ENABLED=true`, and `STOP_MANAGEMENT_ENABLED=true` after reviewing the behavior. Live trading additionally requires deliberately setting `ALPACA_PAPER=false` and supplying live credentials; keep `.env` out of Git.
- **Signal policy:** intraday uses 5/20-minute SMA state changes; a buy crossover must also clear `SIGNAL_MIN_SPREAD_PCT` and `SIGNAL_MIN_BUY_RSI`, while sell crossovers remain unfiltered. Swing enters when a completed daily close crosses above its 20-day SMA and exits below it; core enters on a 50/200-day golden cross and exits on a death cross. Daily bars come from `MARKET_DATA_PROVIDER` and the current unfinished daily bar is excluded. Signals are emitted once per completed daily bar. All strategies are long-only and reject stale signals and entries while the US market is closed.
- **Per-trade risk:** whole-share quantity is bounded by 1% of account equity divided by the configured stop distance. Intraday entries use day bracket orders with the default 2% stop and 4% target. Swing entries use a GTC protective stop at 2x daily ATR by default, without a fixed take-profit; core entries use a GTC protective stop defaulting to 20%, without a fixed take-profit. Swing/core exits also follow their daily trend rules. Stop repair uses the pool's own default distance (core 20%, otherwise `STOP_LOSS_PCT`); it only fills a missing stop and never tightens an existing one.
- **Portfolio and burst limits:** `MAX_PORTFOLIO_OPEN_RISK_PCT` caps the summed stop-risk (`quantity × stop distance`) of managed open positions; a new order is reduced or rejected when it would breach the cap. `MAX_ENTRIES_PER_5MIN` limits new orders in a rolling five-minute window so a single crossover burst cannot open a correlated basket at once. `INTRADAY_ENTRY_START_MINUTES` skips early intraday entries, and `INTRADAY_DAILY_LOSS_LIMIT_PCT` halts further intraday entries for the session once that pool's realized loss reaches the limit. None of these liquidate positions or cancel protective stops.
- **Allocation:** default virtual pool targets are 20% intraday, 30% swing, and 50% core. Each new entry is limited by the remaining budget of its pool. The pools remain virtual partitions in one brokerage account; they do not transfer cash or automatically rebalance positions.
- **Drawdown breaker:** the worker uses Alpaca's same-day portfolio history for its New York session baseline, falling back to the account's previous-close equity if history is unavailable. A 3% or greater drop persists a halt for new entries until the next date and logs a critical event. If `ALERT_WEBHOOK_URL` is configured, it posts a generic JSON alert to that receiver. Existing bracket exits remain at the broker. It does not liquidate positions.
- **Reconciliation:** every 30 seconds the worker snapshots Alpaca equity, buying power, positions, tracked/untracked symbols, allocation exposure, and missing protective stops. Each distinct warning is logged once and repeated only after `RECONCILE_WARNING_REPEAT_SECONDS` (default 1800) or when it clears and returns; the full warning list is still stored in every snapshot. When stop management is enabled, it restores stops for bot-managed positions only. Alpaca is treated as the source of truth: reconciliation also compares the managed-order ledger with broker positions (`ledger_discrepancies`), flags pools over target and unclassified-sector symbols, and emits `warnings`. It reports drift but never rewrites the ledger. Review `/account/reconciliation` and `/risk`.
- **Position provenance:** reconciliation also reports `open_risk`/`open_risk_pct` and `unmanaged_bot_origin_positions` — holdings the bot created but which have no pool provenance, so they are not stopped, flattened, or trend-exited. Those are never adopted automatically. Assign one explicitly with `POST /admin/positions/{symbol}/adopt?pool=core`; the operator assignment is stored in `position_assignments`, outranks order-history inference, and changes only pool attribution on the next cycle. It places, moves, or cancels nothing. Keep the API internal, because this endpoint is not authenticated. Every reported position also carries `pool`, `quantity_fractional`, and a `protection` object (`model`, `stop_present`, `stop_time_in_force`, `target_management`) describing whether its exits are a whole-share broker bracket, a fractional day stop with a software target, a GTC OTO stop with trend exits, or unmanaged; `protection_summary` counts the models in use. `capital_utilization` reports equity, cash, invested value, the overall invested share, the cash share, and each pool's utilization of its target, so under-deployment is visible alongside risk.
- **Managed exits:** before closing a managed position, the worker cancels attached exit legs and waits up to five seconds for all closing-side open orders on that symbol to clear. If any remain, it defers the close and leaves the signal pending for retry; it does not cancel unrelated orders.
- **Order observability:** order submission, duplicate suppression, submission errors, broker status changes, and fill progress are logged with UTC timestamps, signal timestamps, signal IDs, client and broker order IDs, symbol, pool, and status. Reconciliation captures managed parent and attached child order fills, including broker submission/fill times, aggregate filled quantity, average price, order type, fees when exposed by the broker model, and entry/exit reference-price slippage when available. Entry reference prices are paired with the source bar timestamp. If a submission request fails before its outcome is known, it remains tracked and is looked up by client order ID instead of being mislabeled as rejected.
- **Trade report:** `/trades` matches fills FIFO by symbol and reliably attributed strategy pool. It reports gross realized P&L, holding time, signal-to-fill delay, adverse entry/exit slippage, and summaries by strategy name, pool, symbol, and entry/exit order type. `profitability_evidence` explicitly marks whether net P&L and strategy attribution are measurable across the report; this is a data-quality gate, not a profitability or live-readiness verdict. Explicit per-order fees from broker order data or fee fields on fill activities are allocated proportionally across matched quantities; net realized P&L is reported only when fees are known for both sides of every matched slice. Unknown fees are never treated as zero, and account-level cash adjustments are not guessed or allocated to trades. Broker fill prices already reflect execution prices, so slippage is not subtracted a second time. The report uses aggregate order fills and does not include taxes or other account adjustments; unmatched exits and remaining managed quantities are surfaced separately. Alpaca order snapshots recover client order IDs and parent-order relationships; a recorded parent can supply pool attribution to a child fill. Otherwise the fill remains `unattributed`; a `kt-` client order ID confirms app origin but does not restore missing pool, signal, or strategy configuration. Pool attribution is never inferred from an earlier trade in the same symbol. Trade details identify fill source and attribution source, and missing strategy names are grouped as `unknown`. New orders record the pool-specific strategy name, frequency and periods, app version, trading mode, and observed signal values (including daily swing ATR and core/swing trend values). `report_metadata` shows when the report was generated and the current runtime configuration (including `ALPACA_DATA_FEED` and the latency warning threshold); it does not imply old trades used that configuration. `data_quality` counts slices missing signal timestamps, order types, fees, entry/exit strategy configuration, or reliable fill attribution. It is a measurement aid, not a profitability guarantee.

  The report also answers the questions that gross P&L alone cannot. `summary_by_exit_reason` labels every slice as `stop`, `stop_repair`, `take_profit`, `signal`, `eod_flatten`, or `unknown`, derived from the exit order type, client order ID, and signal ID. `fee_estimate` allocates account-level `FEE` activity amounts pro-rata across each UTC day's closed exit notional and sets a per-slice `estimated_net_realized_pnl`; this is explicitly an estimate (`basis`, `estimate: true`, unallocated amounts and days without a fee activity are reported) and never overwrites the strict per-order `fees` or `net_realized_pnl` fields. `profitability_evidence.estimated_net_available` states whether an estimate exists while `net_pnl_measurable` keeps its strict meaning. `execution_latency` reports p50/p90/max signal-to-fill delay for entries and exits and the share above `EXECUTION_LATENCY_WARN_SECONDS`; the worker also logs when a reconciliation or signal takes longer than that. `bar_coverage` reports regular-session minute-bar completeness per symbol and session and flags anything below 80% of 390 bars, which detects a thin feed stretching an SMA measured in bars over more wall-clock time. `benchmark` gives the configured ETF's close-to-close return across the first and last closed-trade dates as a reference point, not a portfolio simulation.
- **Alpaca account report:** `/account/broker` returns the latest account and position snapshot, open/recent orders, stored `FILL`/`TRANS`/`MISC`/`FEE` activities, per-category sync status, and activity totals. `FEE` activities are account-level and do not identify an order, so their amounts are reported separately and are not allocated to trade slices. This is sensitive financial data; expose it only through a private, authenticated path. Account snapshots follow the reconciliation interval. Activity synchronization is separate: `ALPACA_ACTIVITY_SYNC_SECONDS` defaults to 300 seconds, with `ALPACA_ACTIVITY_LOOKBACK_DAYS` defaulting to 30 days for the initial history window. Incremental sync overlaps by five minutes to accommodate delayed activities, so activity and account snapshot timestamps can differ; review the response timestamps and sync status when reconciling. `/account/broker` also returns `snapshot_age_seconds`, `configured_trading_mode`, and `warnings` (stale snapshot, missing trading mode/account details, activity sync newer than the snapshot). Recent order history is limited to 500 orders and `orders_may_be_truncated` indicates when the limit is reached. Explicit per-order fee fields on broker activities are retained; any fee absent from order and activity data remains unknown in `/trades`.
- **Duplicate protection:** entry reservations use deterministic Alpaca client order IDs and a PostgreSQL uniqueness constraint. Redis events are acknowledged after processing.
- **Signal throughput:** the worker keeps one reconciliation for `RISK_VIEW_TTL_SECONDS` (default 5) instead of reconciling for every signal, deduplicates a batch by keeping the newest event per symbol, processes sells first, and runs up to `EXECUTION_MAX_WORKERS` signals concurrently. Entry placement is serialized so the burst and open-risk caps stay accurate. A reconciliation or signal that exceeds `EXECUTION_LATENCY_WARN_SECONDS` is logged. Unacknowledged messages are still retried.
- **Entry execution:** entries are market orders by default. `ENTRY_ORDER_TYPE=marketable_limit` is an opt-in experiment that sends a limit `ENTRY_LIMIT_OFFSET_BPS` through the reference price, waits `LIMIT_ORDER_TIMEOUT_SECONDS`, then cancels an unfilled order and takes liquidity for the unfilled quantity. Partial fills and orders that cannot be confirmed cancelled are left working rather than duplicated. `MIN_ENTRY_BAR_COVERAGE_PCT` blocks intraday entries when the session's stored bar count is below that share of the elapsed session, which guards against acting on a thin feed. Fractional sizing is enabled by `FRACTIONAL_SHARES_ENABLED`. Alpaca allows fractional quantities only on simple market, limit, stop, and stop-limit orders with a day time in force — not brackets, OTO, or GTC — so fractional intraday entries are sent as a simple day order; the protective stop is a separate day stop submitted as soon as the entry fills, and the take-profit is enforced by reconciliation against the recorded target. Whole-share intraday entries keep the atomic day bracket. Swing and core entries stay whole-share because their protective stops must remain GTC OTO orders, which do not accept fractional quantities. Set `FRACTIONAL_SHARES_ENABLED=false` to restore whole-share sizing everywhere.
- **Strategy validation gate:** `REQUIRE_VALIDATED_STRATEGY=true` blocks new entries unless the current strategy configuration has a stored validation record whose bootstrap interval for the mean net trade return lies entirely above zero. It is off by default; enabling it is how risk is prevented from growing on an unvalidated configuration.

`entry_month_version_cohorts` in `/trades` groups all matched slices by UTC entry-fill month and recorded entry app version, independently of the response's detail limit. Each cohort includes gross P&L, fee completeness, attribution evidence, and the number of distinct known entry order IDs. An order may produce multiple slices, and an order count is not a count of statistically independent trades. Missing entry times or versions are grouped as `unknown`. A recorded order's explicit fee takes precedence for its recorded quantity; otherwise, explicit FILL-activity fees are used only when every matched activity has a fee and the same order, symbol and side have exactly the recorded filled quantity. When activity fills supply the quantity instead, an order's fee is used only if its recorded filled quantity matches. Account-level FEE activities are never allocated to trades.

This is a starter implementation, not a validated strategy or a production brokerage platform. Backtests approximate order execution and use configurable transaction costs; they do not model market impact, partial fills, liquidity, survivorship bias, or historical universe membership. OHLC bars cannot determine intrabar price ordering, so a bar touching both stop and target is conservatively treated as a stop. Stop orders cannot guarantee a fill price. Start with paper credentials and inspect orders, account state, and market-data terms before relying on the system.

To manually reconcile a paper account, pause the execution Deployment, then compare each Alpaca Paper Trading position's quantity with its open orders and attached bracket/stop legs. A position may show zero available quantity while an open exit order reserves the shares. Do not submit a second close or cancel protective orders blindly; identify the order and decide whether to leave it, cancel it, or close the position only after cancellation is confirmed. Resuming the worker can process pending signals.

## Local setup

Copy the sample environment file, then set API credentials for the selected endpoint. `.env` is ignored by Git. Leave `ALPACA_PAPER=true` for paper mode. Live mode requires `ALPACA_PAPER=false`, live API credentials, and `EXECUTION_ENABLED=true`; enabling that combination can place real orders.

Local runs and Docker Compose read these settings from `.env`; Compose also accepts shell or project `.env` overrides. `APP_VERSION` defaults to `dev` locally. GitHub Actions increments the Helm chart patch version for each published image, uses it as the application version in the image and chart, and the API displays that version. In Helm deployments, override activity sync settings with `config.alpacaActivitySyncSeconds` and `config.alpacaActivityLookbackDays` in your values file.

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
curl "http://localhost:8000/research/portfolio?days=5000"
curl "http://localhost:8000/research/portfolio?days=5000&exit_style=protective_only&persist=true"
curl http://localhost:8000/universe
curl http://localhost:8000/account/reconciliation
curl http://localhost:8000/account/broker
curl http://localhost:8000/trades
curl http://localhost:8000/risk
# Operator-only: assign an existing bot-created position to a pool (places no orders).
curl -X POST "http://localhost:8000/admin/positions/COST/adopt?pool=core"
```

The daily history API supports `MARKET_DATA_PROVIDER=yfinance` (default) or `finnhub` with `FINNHUB_API_KEY`. Intraday WebSocket ingestion uses Alpaca and `ALPACA_DATA_FEED=iex` by default. Market-data entitlements and availability depend on the selected provider and plan. IEX carries a small share of consolidated volume, so its minute bars can be sparse and an SMA measured in bars can span more wall-clock time than configured; check `bar_coverage` in `/trades` before trusting intraday signals. SIP requires an entitled account.

`/research/backtests/{symbol}` compares the configured intraday SMA rule, 20-day swing rule, and 50/200-day core rule. It uses OHLC bars, applies the intraday spread/RSI buy confirmation, simulates market signals at the next bar open, and models protective stops/targets with the stop assumed first when both are touched within one bar. Intraday entry cutoffs and end-of-day flattening use New York time and assume a 16:00 close. The report includes the strategy configuration, risk/exit settings, execution assumptions, chronological in-sample/out-of-sample metrics, and net payoff metrics including profit factor and mean winning/losing trade returns. The holdout is also split into up to three contiguous chronological subperiods for descriptive stability checks; the strategy is fixed, positions can carry across boundaries, and these are not independent samples or separately trained models. Out-of-sample results are recomputed with 5 and 10 additional basis points charged per side. These cost stresses test sensitivity; they do not estimate actual execution costs. Defaults use 70% of each series for in-sample and 5 basis points per side; query parameters can override `train_fraction`, `cost_bps`, and daily `days`. Daily evaluation uses up to 1,260 bars; intraday evaluation uses up to `INTRADAY_BACKTEST_BARS` locally collected bars. Results still approximate fills, costs, and market hours; use them for research, not as evidence of guaranteed profitability.

`/research/portfolio` is the portfolio-level counterpart. It uses the live account equity as its capital base and has **no fallback**: if no reconciliation snapshot exists the request fails, and `starting_equity` (or `--starting-equity` on the grid) must be supplied explicitly for a stress test or a different notional. Portfolio simulation uses fractional quantities by default (`fractional_shares`), so every cap scales with capital and percentage results do not depend on the equity level; this is what makes repeated runs at actual equity comparable. It simulates every selected watchlist symbol together under the live capital and risk constraints — intraday pool budget, per-symbol and per-sector caps, statement cash, max positions, 1% risk sizing, open-risk cap, entry start/cutoff, end-of-day flattening, and per-side costs — and returns portfolio equity, drawdown, Sharpe, average exposure, turnover, cost drag, exit-reason counts, an equal-weight buy-and-hold benchmark, and the bootstrap confidence interval and minimum detectable edge for the mean net trade return. Query parameters can override `fast_bars`, `slow_bars`, `min_spread_pct`, `min_buy_rsi`, `cost_bps`, `min_hold_minutes`, and `exit_confirmation_spread_pct`, so candidate variants can be compared without changing runtime settings. `persist=true` stores the verdict in `strategy_validation` under a versioned hash of the strategy parameters; `REQUIRE_VALIDATED_STRATEGY` can then require that record before new entries. Verdicts carry a `scheme_version`; the key set and the meaning of a validated verdict are versioned, so records from an older scheme cannot satisfy the current gate. A positive mean alone is never presented as validated: the trade interval must lie entirely above zero, the sample must meet `VALIDATION_MIN_TRADES`, **and** the out-of-sample excess return over the equal-weight benchmark at the same exposure must also have an interval above zero. The benchmark-relative series compares each period's portfolio return with the universe return scaled by the prior period's exposure, aggregated to daily excess and bootstrapped the same way, so a strategy that merely lags buy-and-hold cannot be promoted on absolute P&L alone. Both tests are reported as `benchmark_relative` (full window and each segment) and `validation.benchmark_relative_required`. `mandate` selects `relative` (trade edge plus benchmark, the default) or `absolute` (trade edge alone); it is part of the validation hash and is reported back, so an absolute verdict cannot satisfy a relative gate. The Helm paper configuration uses the RSI-65 candidate (`SIGNAL_MIN_BUY_RSI=65`) under the relative mandate. The verdict is computed on the **out-of-sample** segment: `split_fraction` (0.7 by default) cuts the window chronologically, the in-sample section covers the first share and the out-of-sample section the remainder, and `persist=true` stores the out-of-sample statistics. The split is one cut of a continuous simulation, so out-of-sample equity starts at the boundary value and open positions carry across.

### Research grid

`app.services.research_grid` runs the variant matrix in-process against stored bars — one bar load per symbol instead of one HTTP round trip per variant — and ranks results by out-of-sample gross edge:

```sh
python -m app.services.research_grid --spreads 0.001,0.0015 --rsis 55,65
python -m app.services.research_grid --fast-slows 5:20,10:40 --persist
```

It promotes a variant only when the out-of-sample interval lies above zero and the out-of-sample trade count meets `--min-oos-trades`; `--persist` writes each verdict to `strategy_validation`. It never publishes to Redis or places orders.

Exit, premise, and sampling variants can be compared with the same tooling. `premise` selects `sma_crossover` (the default 5/20 state change), `opening_range_breakout` (the first break of the session's opening range, stopping at the range low capped by the configured stop), or `gap_up` (long a session that opens at least `gap_up_pct` above the prior close; gap entries ignore the post-open entry guard because the premise is defined at the open). `exit_style` accepts `signal` (exit on the reverse crossover), `protective_only` (stop, target, and end-of-day flatten only), or `trailing` (a protective stop ratcheted behind the high, requiring `trailing_stop_pct`). `resample_minutes` aggregates stored minute bars before signals, which tests a slower signal that trades less and pays less cost. `min_hold_minutes` and `exit_confirmation_spread_pct` are also available. The benchmark reports its return both outright and scaled to the strategy's average exposure, so a mostly-cash run is not credited with avoiding a market decline.

### Historical backfill

`/research/portfolio` can only use bars already stored in PostgreSQL. Seed minute history with the backfill service:

```sh
python -m app.services.backfill --days 30
docker compose exec api python -m app.services.backfill --days 30
```

It writes to the `bars` table only and never publishes to the `market.bars` stream, so backfilled history cannot trigger signals, backtests, or orders. Use `--symbols AAPL,MSFT` to override the universe and `--feed sip|iex` to override the feed. Reruns are idempotent; existing bars are updated in place.

Daily history for the swing and core simulator uses `--timeframe day`, which writes to the separate `daily_bars` table and requests split- and dividend-adjusted bars:

```sh
python -m app.services.backfill --timeframe day --days 1000
```

### Daily portfolio research

`/research/portfolio/daily` runs the swing (close vs 20-day SMA) and core (50/200-day cross) pools together under the live pool budgets, symbol and sector caps, 1% risk sizing, open-risk cap, and per-side costs. Signals fill at the next day's open and stops fill at the stop price, or at the open on a gap through it. The report includes the same in-sample/out-of-sample split, bootstrap interval, minimum detectable edge, and exposure-matched benchmark as the intraday simulator, plus a `daily-swing-core` validation identity. It reads the `daily_bars` table and never places orders. Set `regime_filter=true` to block new swing/core entries while the regime symbol (default `SPY`) closed below its `regime_sma` (default 200) on the prior session; open positions still follow their own stops and trend exits. Entries smaller than half their intended size are skipped, so a residual share count cannot consume a slot and make results depend on small changes in account equity.

### Maintenance

```sh
python -m app.services.maintenance --purge-stale-validations
```

Deletes validation verdicts written under older schemes. The schema-versioned hash already prevents them from satisfying the gate; purging simply keeps the table readable.

Trade reports group closed slices by entry app version as well as effective strategy/risk configuration. Orders record configured risk controls and the calculated order sizing; trades without recorded provenance remain `unknown` and are not backfilled from current settings.

## Docker image

```sh
docker build -t kube-trader:latest .
docker run --rm -p 8000:8000 --env-file .env -v kube-trader-data:/data kube-trader:latest
```

The `rolling_out_of_sample` section adds up to three non-overlapping test windows after an expanding historical warmup. Each test starts flat, permits a signal from the preceding bar, and marks open positions to market at the window end without forcing a sale or charging an exit cost. Parameters are fixed rather than fitted; the windows are not independently trained models. To compare a research-only intraday confirmation candidate on the same bars and assumptions, pass `candidate_min_spread_pct` and/or `candidate_min_buy_rsi`. The configured baseline remains in `strategies.intraday`; the candidate appears separately in `intraday_candidate` and does not change order behavior. Neither simulation models passive orders or missed fills, so it cannot validate an alternative entry execution method.

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

The chart defaults to paper mode with execution and new entries enabled. For an exit-only paper canary, keep `config.alpacaPaper=true`, set `config.executionEnabled=true`, and set `config.newEntriesEnabled=false`; streaming and stop management may be enabled as needed. For live mode, also set `config.alpacaPaper=false` and provide live credentials in the Kubernetes Secret; this project has not established a live-trading performance gate. Use `helm/kube-trader/values.yaml` for worker limits and PVC sizes.

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
| `EXTENDED_HOURS_SAMPLING_ENABLED` / `EXTENDED_HOURS_SAMPLE_SECONDS` | `false` / `300` (deploy files enable) | Read-only: the ingestion worker records quote spreads and `overnight_tradable` for the selected symbols in pre-market, after-hours and overnight sessions. It places no orders. View with `/research/extended-hours?days=7` |
| `INTRADAY_FLATTEN_MINUTES` / `INTRADAY_ENTRY_CUTOFF_MINUTES` | `0` / `0` (deploy files set `10` / `30`) | Close intraday-pool positions this many minutes before the regular close, cancelling attached exit legs and bot-managed protective stops first; stop opening intraday entries that close to it. Reconciliation reports bot-managed protective stops left open for symbols with no broker position |
| `SYMBOL_LOSS_COOLDOWN_SECONDS` / `SYMBOL_DAILY_LOSS_LIMIT` | `0` / `0` (deploy files set `1800` / `50`) | Code default is disabled; the Helm chart, `k8s/app.yaml` and `.env.example` enable them. Block new entries in a symbol for N seconds after a losing exit, or once its realized loss today (New York date) reaches the dollar limit |
| `MAX_SYMBOL_EXPOSURE_PCT` / `MAX_SECTOR_EXPOSURE_PCT` | `0.05` / `0.25` | Account-equity limits for a symbol and its configured sector group |
| `MARKET_STREAM_ENABLED` | `false` | Starts the Alpaca minute-bar WebSocket consumer |
| `EXECUTION_ENABLED` | `false` | Enables automated entries/exits on the selected endpoint |
| `NEW_ENTRIES_ENABLED` | `false` | Allows buy entries when execution is enabled; false still allows managed signal exits |
| `STOP_MANAGEMENT_ENABLED` | `false` (deploy files set `true`) | Repairs missing stops on bot-managed positions |
| `MAX_TRADE_RISK_PCT` | `0.01` | Maximum account-equity loss budget per entry |
| `DAILY_DRAWDOWN_LIMIT_PCT` | `0.03` | Entry halt threshold for the current New York date |
| `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` | `0.02` / `0.04` | Default bracket exit distances |
| `INTRADAY_ALLOCATION_PCT` / `SWING_ALLOCATION_PCT` / `CORE_ALLOCATION_PCT` | `0.20` / `0.30` / `0.50` | Virtual portfolio pool targets; must sum to 1.0 |
| `SIGNAL_FAST_BARS` / `SIGNAL_SLOW_BARS` | `5` / `20` | SMA periods, in minute bars |
| `SIGNAL_MIN_SPREAD_PCT` / `SIGNAL_MIN_BUY_RSI` | `0.001` / `55` | An intraday buy needs the fast SMA at least this fraction above the slow SMA and RSI(14) at or above this level. An unconfirmed crossover is not recorded, so the buy can still fire on a later bar. Sell crossovers are never filtered. Set `0` / `0` for the previous behaviour |
| `SWING_ATR_PERIOD` / `SWING_ATR_MULTIPLE` | `14` / `2.0` | Daily ATR stop distance for swing orders |
| `CORE_STOP_LOSS_PCT` | `0.20` | Core GTC protective stop distance (20% default) |
| `BACKTEST_COST_BPS` / `BACKTEST_TRAIN_FRACTION` | `5` / `0.70` | Research endpoint's estimated one-way costs and chronological train split |
| `INTRADAY_BACKTEST_BARS` | `50000` | Maximum locally stored minute bars used by research backtests |
| `RECONCILE_INTERVAL_SECONDS` | `30` | Brokerage reconciliation interval |
| `ALPACA_ACTIVITY_SYNC_SECONDS` | `300` | Interval for syncing Alpaca `FILL`, `TRANS`, and `MISC` account activities |
| `ALPACA_ACTIVITY_LOOKBACK_DAYS` | `30` | Initial Alpaca account-activity history window |
| `MAX_SIGNAL_AGE_SECONDS` | `180` | Reject older or future-dated signals |
| `ALERT_WEBHOOK_URL` | empty | Optional generic JSON circuit-breaker webhook receiver |
| `POSTGRES_DSN` / `REDIS_URL` | local defaults | Database and stream-broker connection strings |

**Export filenames:** successful JSON `GET` responses carry `Content-Disposition` (`inline`, so Swagger UI still renders the body; add `?download=true` to get `attachment` and a saved file, e.g. opening `/trades?download=true` in a browser tab or `curl -OJ`) with a name derived from the endpoint plus a UTC timestamp, e.g. `reconciliation_20261023T211242Z.json`, `broker_...`, `trades_...` (the leading `account` segment is dropped; health and docs endpoints are excluded).
