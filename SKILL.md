# Kube Trader Pipeline and Risk Skill

## Metadata

- **Name:** `kube-trader-pipeline`
- **Version:** 2.7.0
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
| `NEW_ENTRIES_ENABLED` | `false` | Allows buy entries; false still allows managed signal exits when execution is enabled |
| `STOP_MANAGEMENT_ENABLED` | `false` | Repairs missing stops on bot-managed positions |
| `ALPACA_DATA_FEED` | `iex` | Alpaca stock stream feed, `iex` or `sip` |
| `MAX_TRADE_RISK_PCT` | `0.01` | Requested risk budget, capped in code at 1% of equity |
| `MAX_PORTFOLIO_OPEN_RISK_PCT` | `0.02` | Cap on summed stop-risk across managed open positions; `0` disables |
| `MAX_ENTRIES_PER_5MIN` | `3` | Rolling five-minute cap on new orders; `0` disables |
| `INTRADAY_ENTRY_START_MINUTES` | `5` | Minutes after the open before intraday entries are allowed |
| `INTRADAY_DAILY_LOSS_LIMIT_PCT` | `0.005` | Session realized-loss halt for new intraday entries |
| `EXECUTION_LATENCY_WARN_SECONDS` | `30` | Signal-to-fill and reconciliation delay that triggers a warning log |
| `RISK_VIEW_TTL_SECONDS` | `5` | Reuse one reconciliation across queued signals for this long |
| `EXECUTION_MAX_WORKERS` | `4` | Concurrent signal handlers per Redis batch |
| `ENTRY_ORDER_TYPE` | `market` | `market` or the opt-in `marketable_limit` entry |
| `ENTRY_LIMIT_OFFSET_BPS` / `LIMIT_ORDER_TIMEOUT_SECONDS` | `10` / `15` | Limit offset and fallback window for marketable-limit entries |
| `FRACTIONAL_SHARES_ENABLED` | `true` | Fractional sizing; intraday only where broker-side protection permits |
| `FRACTIONAL_QTY_DECIMALS` / `FRACTIONAL_FILL_TIMEOUT_SECONDS` | `4` / `10` | Quantity precision and post-fill stop window |
| `MIN_ENTRY_BAR_COVERAGE_PCT` | `0` | Block intraday entries below this share of expected session bars; `0` disables |
| `REQUIRE_VALIDATED_STRATEGY` | `false` | Block entries unless the current configuration has a validated record |
| `VALIDATION_MIN_TRADES` / `VALIDATION_CONFIDENCE` | `30` / `0.95` | Minimum sample and bootstrap confidence for a validation verdict |
| `VALIDATION_MANDATE` | `relative` | `relative` needs a benchmark edge too; `absolute` accepts the trade edge alone |
| `PORTFOLIO_BACKTEST_MAX_SYMBOLS` | `40` | Symbol cap for `/research/portfolio` |
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
| `RECONCILE_WARNING_REPEAT_SECONDS` | `1800` | Repeat an unchanged reconciliation warning at most this often; `0` logs every cycle |
| `ALERT_WEBHOOK_URL` | empty | Optional receiver for generic JSON breaker alerts |
| `POSTGRES_DSN` / `REDIS_URL` | local defaults | Persistence and event-broker connections |

The three allocation percentages must be nonnegative and sum to `1.0`. Never commit real values to `.env.example` or Helm values.

## Signal and backtest behavior

For intraday signals, the worker reads recent minute closes from PostgreSQL, compares the fast and slow SMAs, and stores the current above/below state. On a state change it publishes action, pool, price/time, SMA values, RSI(14), and bracket percentages.

For daily strategies, it fetches daily bars using `MARKET_DATA_PROVIDER`, excludes the current unfinished session bar, and caches the result during the New York trading date. Swing enters on a completed close crossing above the 20-day SMA and exits on a cross below; its protective stop defaults to 2x 14-day ATR. Core enters when the 50-day SMA crosses above the 200-day SMA and exits on the reverse cross; its protective stop defaults to 20%. Daily crossover state is persisted per symbol/pool/bar date, so repeated minute events do not duplicate daily signals. RSI remains informational and does not filter orders.

The backtesting worker recalculates only the intraday SMA simulation over up to 500 recent minute bars whenever a new bar arrives. `/research/backtests/{symbol}` separately compares all three rules, applies a configurable per-side cost estimate, and reports a chronological in-sample/out-of-sample split plus buy-and-hold comparison. The research endpoint uses up to 50,000 locally collected minute bars for intraday and up to the requested daily history for swing/core. Signals fill at the next bar open and protective exits fill at the stop or target price (or the open on a gap); minute Sharpe annualization is approximate. Results omit market impact, taxes, historical universe membership, and corporate-action timing and must not be treated as expected performance.

The ingestion worker downloads the configured ETF issuer XLSX holdings at startup and every `UNIVERSE_REFRESH_SECONDS` (24 hours by default), screens holdings by minimum price and 20-day average dollar volume, then subscribes to the most liquid eligible symbols up to `MAX_WATCHLIST_SYMBOLS`. It retains active subscriptions when a scheduled refresh fails. This is limited to one fund's published constituents, not all exchange listings. `/universe` reports the latest screen. New entries are limited by pool budgets, buying power, `MAX_OPEN_POSITIONS`, `MAX_SYMBOL_EXPOSURE_PCT`, and `MAX_SECTOR_EXPOSURE_PCT`; existing untracked positions count toward symbol/sector exposure. Custom symbols without a `SYMBOL_SECTORS` mapping are grouped under `unclassified`.

## Allocation and order sizing

Each pool's target equity is `account_equity * target_ratio`. Broker position market values are assigned to their recorded bot pool; untracked account positions are conservatively counted against core exposure. An explicit operator assignment (`position_assignments`, created by `POST /admin/positions/{symbol}/adopt`) outranks order-history inference. Entry size is the lowest whole-share count allowed by:

1. `account_equity * min(MAX_TRADE_RISK_PCT, 1%) / (signal_price * stop_loss_fraction)`;
2. the pool's remaining target budget and broker buying power;
3. a 100-share per-order ceiling; and
4. remaining room under `MAX_PORTFOLIO_OPEN_RISK_PCT`, which caps summed stop-risk across managed open positions.

Entries are also gated by `MAX_ENTRIES_PER_5MIN` (rolling burst cap), `INTRADAY_ENTRY_START_MINUTES`, `INTRADAY_ENTRY_CUTOFF_MINUTES`, and `INTRADAY_DAILY_LOSS_LIMIT_PCT`. The last one pauses intraday entries for the session once that pool's realized loss reaches the configured share of equity; the account-wide `DAILY_DRAWDOWN_LIMIT_PCT` breaker is unchanged.

All three pools generate signals. Intraday entries use day bracket orders with default 2% stop and 4% take-profit. Swing and core entries use GTC OTO orders with a single protective stop and no fixed take-profit; their daily trend exits close the bot-managed pool position after canceling its attached exit order. The 20/30/50 pool balances are virtual limits inside one brokerage account: the application does not transfer cash between accounts or automatically sell holdings to rebalance pool ratios.

Intraday buy signals submit long-only Alpaca market bracket orders on the selected endpoint, with stop-loss and take-profit child orders. Swing/core buys submit GTC OTO market orders with one protective stop and no fixed take-profit. The execution worker ignores stale/future signals and will not enter when the US equity market is closed. A matching pool's trend exit cancels its open protective closing leg and closes the bot-managed position. The daily breaker blocks new entries but still allows strategy exits.

## Reconciliation and circuit breaker

Every reconciliation reads Alpaca account equity, buying power, positions, and open nested orders. It stores account snapshots, reports untracked/missing positions, `unmanaged_bot_origin_positions`, pool exposures, and `open_risk`/`open_risk_pct`, and identifies absent protective stops. Alpaca is the source of truth: it also reports ledger-vs-broker quantity discrepancies, pools over target, and unclassified-sector symbols as `warnings` without altering the ledger. Each position carries `pool`, `quantity_fractional`, and a `protection` object (`model`, `stop_present`, `stop_time_in_force`, `target_management`) naming its exit model — `whole_share_day_bracket`, `fractional_day_stop_plus_software_target`, `gtc_oto_stop`, a `*_missing_stop` variant, or `unmanaged` — and `protection_summary` counts them. `capital_utilization` reports equity, cash, invested value, overall utilization, the cash share, and each pool's utilization of its target. With `STOP_MANAGEMENT_ENABLED=true`, it submits a protective stop for a bot-managed position missing an open stop order, using the pool's own default distance (core `CORE_STOP_LOSS_PCT`, otherwise `STOP_LOSS_PCT`) and never tightening an existing stop. Bot-created positions without pool provenance are reported, never adopted automatically, and are not flattened or trend-exited until an operator assigns them a pool.

The daily baseline uses Alpaca's same-day 1-minute portfolio history during market hours. If unavailable, it falls back to `account.last_equity` (previous close) or current equity. At a drawdown equal to or greater than `DAILY_DRAWDOWN_LIMIT_PCT`, the worker persists the halt for the current New York date and blocks new automated entries. It emits a critical log and, when configured, posts a JSON object containing severity, service, alert name, message, and timestamp to `ALERT_WEBHOOK_URL`. The state resets on the next New York calendar date after a new reconciliation.

The breaker does not terminate a pod, cancel bracket exits, or liquidate positions. If the webhook is absent or delivery fails, the critical log remains the only alert channel.

## Signal throughput and order execution

The execution worker keeps one broker reconciliation for `RISK_VIEW_TTL_SECONDS` rather than reconciling for every queued signal. Within a Redis batch it keeps only the newest event per symbol, acknowledges and discards older duplicates, sorts sells before buys, and runs up to `EXECUTION_MAX_WORKERS` handlers concurrently. Entry placement is serialized behind a lock so the burst cap (`MAX_ENTRIES_PER_5MIN`) and the summed open-stop-risk cap remain exact; exits are not serialized. A handler that raises does not acknowledge its message, so it is retried. Any reconciliation or signal slower than `EXECUTION_LATENCY_WARN_SECONDS` is logged.

Entries are market orders by default. `ENTRY_ORDER_TYPE=marketable_limit` sends a limit `ENTRY_LIMIT_OFFSET_BPS` through the reference price and waits `LIMIT_ORDER_TIMEOUT_SECONDS`; an unfilled order is cancelled and the unfilled quantity is sent as a market order. A partially filled order, or one whose cancellation cannot be confirmed, is left working instead of being replaced, so a position cannot be doubled. `MIN_ENTRY_BAR_COVERAGE_PCT` compares the symbol's stored session bars with the elapsed session and fails closed when the feed is thin.

Fractional sizing is enabled by `FRACTIONAL_SHARES_ENABLED`. Alpaca accepts fractional quantities only on simple market, limit, stop, and stop-limit orders with a day time in force, so fractional intraday entries are simple day orders: the protective stop is a separate fractional day stop submitted after the entry reports a fill, and the take-profit is checked against the recorded target on each reconciliation cycle. Whole-share intraday entries retain the atomic day bracket. Swing and core orders stay whole-share because their protective stops must remain GTC OTO orders. Research simulators use fractional quantities by default, which keeps every cap proportional to capital and makes percentage results independent of the account equity.

## Strategy validation

`/research/portfolio` simulates the whole selected watchlist together under the live pool, concentration, cash, risk, timing, and cost constraints. It uses the live account equity as its capital base and has no fallback: without a reconciliation snapshot the request fails, and `starting_equity` / `--starting-equity` must be supplied explicitly. Research uses fractional quantities by default (`fractional_shares`), which makes caps proportional to capital and results independent of the equity level. It reports portfolio equity, drawdown, Sharpe, exposure, turnover, cost drag, exit reasons, an equal-weight buy-and-hold benchmark, and a moving-block bootstrap interval plus minimum detectable edge for the mean net trade return. Candidate parameters can be supplied as query overrides without changing runtime settings: `fast_bars`, `slow_bars`, `min_spread_pct`, `min_buy_rsi`, `cost_bps`, `min_hold_minutes`, `exit_confirmation_spread_pct`, `exit_style` (`signal`, `protective_only`, `trailing`), `trailing_stop_pct`, `resample_minutes`, and `split_fraction`. The benchmark reports its return both outright and scaled to the strategy's average exposure. The window is split chronologically into `in_sample` and `out_of_sample` sections; the split is one cut of a continuous simulation, so out-of-sample equity starts at the boundary and open positions carry across. Premises are selectable per run: `sma_crossover`, `opening_range_breakout` (enters on the first close through the opening range high and stops at the range low, capped by the configured stop), and `gap_up` (enters when the session opens at least `gap_up_pct` above the prior close; the post-open entry guard does not apply because the premise is defined at the open). `persist=true` writes the **out-of-sample** verdict to `strategy_validation`, keyed by a versioned hash of the strategy parameters including the premise, exit, and sampling choices. A configuration counts as validated only when the out-of-sample trade interval lies entirely above zero, the out-of-sample sample meets `VALIDATION_MIN_TRADES`, and — under the default `relative` mandate — the out-of-sample excess return over the equal-weight benchmark at the same exposure also has an interval above zero. `absolute` drops the benchmark test and accepts the trade edge alone; it is recorded in the verdict and its hash, so an absolute record cannot satisfy a relative gate. The benchmark-relative series subtracts the universe return scaled by the prior period's exposure, aggregates to daily excess, and bootstraps it the same way, so absolute P&L alone cannot promote a strategy that lags buy-and-hold.

Daily research runs the swing and core pools together from `daily_bars`: `/research/portfolio/daily` or `python -m app.services.backfill --timeframe day --days 1000` to seed the history first. Swing uses the close-vs-20-day-SMA state with a 2x ATR stop; core uses the 50/200-day cross with a 20% stop. Both share cash, pool budgets, concentration caps, and the open-risk cap, and the report reuses the same in-sample/out-of-sample split, bootstrap interval, and exposure-matched benchmark. Daily verdicts use the separate `daily-swing-core` namespace. `regime_filter=true` blocks new entries while the regime symbol (`SPY` by default) closed below its `regime_sma` on the prior session; open positions still follow their stops and trend exits. Entries below half their intended size are skipped so residual share counts do not consume slots and the run stays stable against small equity changes.

Validation records carry a `scheme_version` (currently 3). The version is part of the hash and is stored, so a verdict produced under an older key set or an older definition of "validated" cannot satisfy the current gate.

With `REQUIRE_VALIDATED_STRATEGY=true`, new entries are blocked unless that record exists, which prevents risk from growing on an untested configuration. Validation is still evidence, not a guarantee.

`python -m app.services.research_grid` runs the same matrix in-process against stored bars, ranks variants by out-of-sample gross edge, and promotes only those whose out-of-sample interval clears zero. `--persist` stores each verdict; it never publishes to Redis or places orders.

## Operational endpoints and deployment

- `GET /healthz`: API process liveness.
- `GET /readyz`: PostgreSQL connectivity.
- `GET /backtests/{symbol}`: latest rolling metrics.
- `GET /trades`: realized P&L with gross/net fee completeness, fill source, and attribution source; absent fee or strategy metadata stays unknown.
- `GET /research/portfolio`: portfolio-level walk-forward simulation over the watchlist with significance testing; `persist=true` records the validation verdict. `premise` selects `sma_crossover`, `opening_range_breakout`, or `gap_up`.
- `GET /research/portfolio/daily`: swing and core daily pools simulated together from `daily_bars`, with the same split, significance, and benchmark reporting.
- `python -m app.services.backfill --days 30`: seeds historical minute bars for research. It writes PostgreSQL only and never publishes to `market.bars`, so history cannot trigger signals or orders. Supports `--symbols` and `--feed sip|iex`, and is safe to rerun. `--timeframe day --days 1000` instead fills `daily_bars` with adjusted daily bars for the swing and core simulator.
- `python -m app.services.research_grid --persist`: ranks stored-bar variants by out-of-sample gross edge and stores verdicts; it never places orders. `--mode daily` runs the swing/core regime matrix instead.
- `python -m app.services.maintenance --purge-stale-validations`: deletes validation verdicts from older scheme versions. Also returns `summary_by_exit_reason`, a pro-rata `fee_estimate` with per-slice `estimated_net_realized_pnl` (labelled estimated, never replacing strict per-order fees), `execution_latency` percentiles, `bar_coverage`, and an ETF `benchmark` return over the closed-trade window.
- `GET /account/reconciliation`: latest account and allocation snapshot, including `ledger_discrepancies`, `open_risk`, `unmanaged_bot_origin_positions`, and `warnings`. JSON GET responses are named `<endpoint>_<UTC timestamp>.json` via `Content-Disposition` (`attachment` only with `?download=true`; otherwise `inline`).
- `GET /risk`: current New York-date breaker state.

- `POST /admin/positions/{symbol}/adopt?pool=intraday|swing|core`: assign a current broker position to a pool. It records provenance only and places, moves, or cancels nothing; the endpoint is unauthenticated, so keep the API internal.

Use `docker compose up --build` for the local stack. Kubernetes manifests live in `k8s/`; the Helm chart is in `helm/kube-trader/`. The API is internal by default. Configure credentials matching the selected Alpaca endpoint, webhook secrets, persistent storage, and private network access before enabling the stream or execution switches. Set `ALPACA_PAPER=false` only when live trading is intended; `EXECUTION_ENABLED` remains false by default.
