# Kube Trader

A small, self-hosted Python starter for daily stock market data, a deterministic SMA indicator, and optional manual Alpaca paper orders. It is open source and uses free or free-tier data options, subject to each provider's current terms and limits.

## What is included

- FastAPI service with health checks, symbol list, daily OHLCV retrieval, and a 20-day versus 50-day simple moving average comparison.
- Market data adapters for Yahoo Finance (`yfinance`, default) and Finnhub (`FINNHUB_API_KEY` required).
- SQLite storage for fetched bars at `/data/market.db`.
- Optional manual Alpaca paper-order endpoint. It is disabled by default, uses Alpaca paper mode only, and is not connected to the indicator signal.
- Docker image, plain Kubernetes manifests, and a Helm chart.

This is a deployment-ready starter, not a production portfolio manager or a strategy proven to make money. It does not implement automated execution, intraday streaming, account reconciliation, stop-loss handling, portfolio allocation, or a drawdown circuit breaker. The indicator is informational and is not investment advice. Review market-data licensing, provider availability, brokerage permissions, and applicable rules before use.

## Run locally

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

OpenAPI docs are available at `http://localhost:8000/docs`. Example routes:

```sh
curl http://localhost:8000/healthz
curl http://localhost:8000/symbols
curl http://localhost:8000/market-data/AAPL?days=90
curl http://localhost:8000/signals/AAPL
```

Set `MARKET_DATA_PROVIDER=finnhub` and put the API key in the ignored `.env` to use Finnhub instead of Yahoo Finance. Keep `.env` local; it is excluded by `.gitignore`. `.env.example` contains names and safe defaults only.

## Alpaca paper trading

Create paper-trading API credentials in Alpaca and set `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. Keep `ALPACA_PAPER=true`. Order submission remains disabled until `TRADING_ENABLED=true`. Once enabled, `POST /paper/orders` can submit manually requested market orders to Alpaca's paper account; the endpoint limits quantity to 100 shares and does not size an order based on risk or portfolio equity. Do not expose this unauthenticated endpoint to an untrusted network.

Example request (will be rejected while the default disabled setting is in effect):

```sh
curl -X POST http://localhost:8000/paper/orders \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"AAPL","qty":1,"side":"buy"}'
```

There is deliberately no live trading base URL or live trading switch in this starter.

## Docker

```sh
docker build -t kube-trader:latest .
docker run --rm -p 8000:8000 --env-file .env -v kube-trader-data:/data kube-trader:latest
```

## Kubernetes manifests

Build and publish the image, then replace `kube-trader:latest` in `k8s/app.yaml` with your image reference. Create a secret in the target cluster (do not commit it):

```sh
kubectl create namespace kube-trader
kubectl -n kube-trader create secret generic kube-trader-secrets \
  --from-literal=FINNHUB_API_KEY='...' \
  --from-literal=ALPACA_API_KEY='...' \
  --from-literal=ALPACA_SECRET_KEY='...'
kubectl apply -f k8s/app.yaml
```

The namespace is also defined in `k8s/namespace.yaml`; apply that first if you have not already created the namespace:

```sh
kubectl apply -f k8s/namespace.yaml
```

If using Yahoo Finance, the Finnhub secret value can be omitted. Kubernetes secret values are still sensitive; use your cluster's secret-management integration for shared or production clusters.

## Helm

```sh
helm upgrade --install kube-trader ./helm/kube-trader \
  --namespace kube-trader --create-namespace \
  --set image.repository=YOUR_REGISTRY/kube-trader \
  --set image.tag=YOUR_TAG
```

Create `kube-trader-secrets` in the release namespace before installing if API keys are needed. Configuration is in `helm/kube-trader/values.yaml`. The PVC requests 2 GiB by default; set a storage class if the cluster does not have a default.

The service is `ClusterIP` and no ingress is installed. Use an authenticated gateway or a private port-forward for access. SQLite with a `ReadWriteOnce` PVC is intended for a single replica; do not scale replicas without replacing the storage and coordination design.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARKET_DATA_PROVIDER` | `yfinance` | `yfinance` or `finnhub` |
| `FINNHUB_API_KEY` | empty | Required for Finnhub daily candles |
| `ALPACA_API_KEY` | empty | Alpaca paper API key |
| `ALPACA_SECRET_KEY` | empty | Alpaca paper secret |
| `ALPACA_PAPER` | `true` | Guard; false causes paper order requests to be rejected |
| `TRADING_ENABLED` | `false` | Enables the manual paper-order endpoint when true |
| `DATABASE_PATH` | `/data/market.db` | SQLite file for retrieved daily bars |
| `DEFAULT_SYMBOLS` | `AAPL,MSFT,SPY` | Returned by `/symbols` |
| `LOG_LEVEL` | `INFO` | Reserved for deployment logging configuration |

