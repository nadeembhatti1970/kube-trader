# SKILL: Multi-Horizon Portfolio Rebalancer

## Metadata
* **Name:** `multi-horizon-rebalancer`
* **Version:** 1.0.0
* **Target Environment:** Kubernetes CronJob / Python Microservice
* **Description:** Automatically calculates asset drift across short-term, medium-term, and long-term strategy pools and generates required internal capital transfer orders to restore target ratios (20 / 30 / 50).

---

## Parameters
* `total_fund_equity` (float): Current net liquidation value of the entire portfolio.
* `pool_balances` (dict): Current cash/equity values for `short_term`, `medium_term`, and `long_term` pools.
* `target_ratios` (dict): Default is `{"short_term": 0.20, "medium_term": 0.30, "long_term": 0.50}`.

---

## Execution Logic (Python)

```python
import json
import sys


def execute_rebalance(event_payload):
  try:
    equity = event_payload["total_equity"]
    balances = event_payload["pool_balances"]
    targets = event_payload.get(
        "target_ratios", {"short_term": 0.20, "medium_term": 0.30, "long_term": 0.50}
    )

    transfers = {}
    for pool, target_pct in targets.items():
      target_value = equity * target_pct
      current_value = balances.get(pool, 0.0)
      # Negative = sweep excess funds out; Positive = allocate cash in
      transfers[pool] = round(target_value - current_value, 2)

    output = {
        "status": "SUCCESS",
        "total_equity": equity,
        "required_transfers": transfers,
    }
    print(json.dumps(output))
    sys.exit(0)

  except Exception as e:
    error_output = {"status": "FAILED", "error": str(e)}
    print(json.dumps(error_output))
    sys.exit(1)


if __name__ == "__main__":
  # Mock payload test
  sample_event = {
      "total_equity": 20113.57,
      "pool_balances": {
          "short_term": 4500.00,
          "medium_term": 5613.57,
          "long_term": 10000.00,
      },
  }
  execute_rebalance(sample_event)