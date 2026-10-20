# Kube Trader Repository Guidance

## Purpose and scope

Kube Trader is a self-hosted stock research and paper-trading application. Maintain the code and deployment artifacts in this repository as a coordinated system. It is not an autonomous live-money portfolio manager, and no live brokerage mode is implemented.

## Runtime architecture

The runtime consists of a FastAPI API plus four independently deployed workers:

1. `app.services.ingestion` receives Alpaca minute bars and writes them to PostgreSQL and the Redis `market.bars` stream.
2. `app.services.signals` calculates 5/20-bar SMA state changes and RSI(14), then publishes events to `strategy.signals`.
3. `app.services.backtesting` updates rolling SMA return, drawdown, and per-bar Sharpe metrics.
4. `app.services.execution` reconciles Alpaca account state, checks limits, and can submit paper orders.

Docker Compose, Kubernetes, and Helm definitions should keep these workers in separate containers/pods. PostgreSQL stores pipeline state and Redis Streams carry worker events. The API's daily-history cache is SQLite at `/data/market.db`; keep that path on persistent storage in deployments.

## Trading and risk invariants

- **Paper-only brokerage:** Alpaca execution must instantiate `TradingClient(..., paper=True)`. Keep `ALPACA_PAPER=true`; do not add live endpoints, live credentials, or a live-mode switch as part of routine changes.
- **Explicit opt-in:** `MARKET_STREAM_ENABLED`, `EXECUTION_ENABLED`, and `STOP_MANAGEMENT_ENABLED` default to false. Preserve those defaults in `.env.example`, Compose, Kubernetes, and Helm.
- **Position risk:** automated entry sizing is whole-share quantity, bounded by the configured stop distance and at most 1% of account equity per entry. Keep the hard 1% cap even if `MAX_TRADE_RISK_PCT` is set higher.
- **Protective exits:** automated entries use Alpaca bracket orders with a stop and take-profit leg. Optional stop repair applies only to bot-managed positions; do not silently manage unrelated account positions.
- **Daily breaker:** compare equity with the New York session baseline from Alpaca portfolio history, falling back to previous-close equity. At or beyond the configured 3% loss threshold, persist a halt for new entries for that session and send a critical log plus an optional generic webhook alert. Do not remove broker-side bracket exits or liquidate positions as part of the breaker.
- **Allocation pools:** default virtual targets are 20% intraday, 30% swing, and 50% core, and must total 100%. They constrain/report exposure; they do not move cash between brokerage accounts. The current signal worker emits intraday signals only. Do not imply that swing/core strategies or automatic pool rebalancing exist.
- **Long-only sample strategy:** buy on an above-SMA state change and close bot-managed positions on a below-SMA change. Reject stale signals and new entries while the US market is closed. RSI is recorded as a factor but is not an execution filter.

## Data, secrets, and reliability

- Use deterministic, typed calculations for indicators, sizing, allocation, and risk. Do not let unvalidated news or other unstructured data affect orders.
- Preserve Redis consumer-group acknowledgements and pending-event recovery. Keep order reservations idempotent using the database uniqueness constraint and Alpaca client order IDs.
- Never commit `.env`, credentials, webhook URLs, or populated Kubernetes Secrets. Keep placeholders in `.env.example`; use Kubernetes/Helm secret references for deployments.
- Persist PostgreSQL and Redis data with PVCs in Kubernetes/Helm. Keep the API SQLite cache mounted at `/data`. Do not put operational state in an ephemeral container layer.
- Keep the API internal by default (`ClusterIP`); document authentication and network requirements before adding an ingress or public endpoint.

## Keeping the project coherent

When changing runtime behavior, update `README.md`, `.env.example`, `compose.yaml`, `k8s/`, and `helm/kube-trader/` as needed. Keep `SKILL.md` aligned with actual commands, configuration defaults, risk behavior, and known limitations. Do not describe a backtest as validated performance; the current rolling metric omits fees, slippage, and out-of-sample validation.
