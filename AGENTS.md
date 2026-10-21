# Kube Trader Repository Guidance

## Purpose and scope

Kube Trader is a self-hosted stock research and automated trading application. Maintain the code and deployment artifacts in this repository as a coordinated system. Alpaca paper mode is the default; setting `ALPACA_PAPER=false` explicitly selects the live brokerage endpoint, so execution remains disabled by default and live credentials must never be committed.

## Runtime architecture

The runtime consists of a FastAPI API plus four independently deployed workers:

1. `app.services.ingestion` receives Alpaca minute bars and writes them to PostgreSQL and the Redis `market.bars` stream.
2. `app.services.signals` calculates the 5/20-minute intraday crossover plus daily swing (close vs 20-day SMA) and core (50/200-day SMA crossover) signals, then publishes events to `strategy.signals`.
3. `app.services.backtesting` updates rolling SMA return, drawdown, and per-bar Sharpe metrics.
4. `app.services.execution` reconciles Alpaca account state, checks limits, and can submit paper or live orders according to `ALPACA_PAPER`.

Docker Compose, Kubernetes, and Helm definitions should keep these workers in separate containers/pods. PostgreSQL stores pipeline state and Redis Streams carry worker events. The API's daily-history cache is SQLite at `/data/market.db`; keep that path on persistent storage in deployments.

## Trading and risk invariants

- **Brokerage mode:** `ALPACA_PAPER` defaults to `true` and must be passed to `TradingClient(..., paper=settings.alpaca_paper)`. `false` selects the live Alpaca endpoint. Keep `EXECUTION_ENABLED=false` by default in every deployment template; live execution requires the operator to explicitly set `ALPACA_PAPER=false`, provide live credentials, and enable execution. Never commit credentials.
- **Explicit opt-in:** `MARKET_STREAM_ENABLED`, `EXECUTION_ENABLED`, and `STOP_MANAGEMENT_ENABLED` default to false. Preserve those defaults in `.env.example`, Compose, Kubernetes, and Helm.
- **Position risk:** automated entry sizing is whole-share quantity, bounded by the configured stop distance and at most 1% of account equity per entry. Keep the hard 1% cap even if `MAX_TRADE_RISK_PCT` is set higher.
- **Protective exits:** intraday entries use Alpaca day bracket orders with stop and take-profit legs. Swing/core entries use GTC OTO entries with protective stops and their daily trend exits; swing stop distance uses 2x ATR by default and core uses a 20% default stop. Optional stop repair applies only to bot-managed positions; do not silently manage unrelated account positions.
- **Daily breaker:** compare equity with the New York session baseline from Alpaca portfolio history, falling back to previous-close equity. At or beyond the configured 3% loss threshold, persist a halt for new entries for that session and send a critical log plus an optional generic webhook alert. Do not remove broker-side bracket exits or liquidate positions as part of the breaker.
- **Allocation pools:** default virtual targets are 20% intraday, 30% swing, and 50% core, and must total 100%. All three have signal strategies; pool targets constrain/report exposure and do not move cash between brokerage accounts or automatically rebalance holdings.
- **Universe and concentration:** screen configured `DEFAULT_SYMBOLS` by minimum price and 20-day average dollar volume at ingestion startup. Fail closed if no candidate passes; do not silently subscribe to an unfiltered market-wide universe. Enforce the maximum position count and configured per-symbol/sector exposure caps against all account holdings, including untracked positions. Keep custom sector mappings documented.
- **Long-only sample strategies:** intraday buys/sells on 5/20-minute SMA state changes; swing enters when the completed daily close crosses above its 20-day SMA and exits below it; core enters on a 50-day/200-day golden cross and exits on a death cross. Daily signals use completed bars, are persisted once per daily bar, and do not replay old signals on a repeat delivery. Reject stale signals and new entries while the US market is closed. RSI is recorded as a factor but is not an execution filter. The rolling backtest covers only the intraday SMA strategy.

## Data, secrets, and reliability

- Use deterministic, typed calculations for indicators, sizing, allocation, and risk. Do not let unvalidated news or other unstructured data affect orders.
- Preserve Redis consumer-group acknowledgements and pending-event recovery. Keep order reservations idempotent using the database uniqueness constraint and Alpaca client order IDs.
- Never commit `.env`, credentials, webhook URLs, or populated Kubernetes Secrets. Keep placeholders in `.env.example`; use Kubernetes/Helm secret references for deployments.
- Persist PostgreSQL and Redis data with PVCs in Kubernetes/Helm. Keep the API SQLite cache mounted at `/data`. Do not put operational state in an ephemeral container layer.
- Keep the API internal by default (`ClusterIP`); document authentication and network requirements before adding an ingress or public endpoint. Research backtest endpoints must label their cost assumptions, sample split, and limitations; never present hypothetical returns as expected profits.

## Keeping the project coherent

When changing runtime behavior, update `README.md`, `.env.example`, `compose.yaml`, `k8s/`, and `helm/kube-trader/` as needed. Keep `SKILL.md` aligned with actual commands, configuration defaults, risk behavior, and known limitations. Do not describe a backtest as validated performance; the current rolling metric omits fees, slippage, and out-of-sample validation.
