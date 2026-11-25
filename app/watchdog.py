"""Pipeline liveness watchdog.

The workers reconcile every `RECONCILE_INTERVAL_SECONDS` and write an account
snapshot, so the age of the newest snapshot is a reliable liveness signal for the
trading path. A host outage, a crashed worker, or a stuck reconciliation all show
up as a snapshot that stops advancing, while the API can stay healthy and silent.

Run it from a CronJob (or any host that can reach the database) so that a dead
namespace is still detected from outside:

    python -m app.watchdog --max-age-seconds 900 --repeat-seconds 1800

It posts to `ALERT_WEBHOOK_URL` when the pipeline is stale or the API health check
fails, and posts a recovery notice when it comes back. The alert key lives in Redis
so repeated runs do not spam; the TTL is the minimum time between repeat alerts.
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timezone

import httpx

from app.database import newest_account_snapshot_at
from app.streams import client as redis_client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("watchdog")

ALERT_KEY = "watchdog:pipeline:alerting"
DEFAULT_MAX_AGE_SECONDS = 900
DEFAULT_REPEAT_SECONDS = 1800


def pipeline_health(max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
                    now: datetime | None = None) -> dict:
    """Report whether the reconciliation pipeline is advancing."""
    moment = now or datetime.now(timezone.utc)
    newest = newest_account_snapshot_at()
    age = None
    if newest is not None:
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        age = max(0.0, (moment - newest).total_seconds())
    stale = age is None or age > max_age_seconds
    return {
        "captured_at": moment.isoformat(),
        "newest_snapshot_at": newest.isoformat() if newest else None,
        "snapshot_age_seconds": None if age is None else round(age, 1),
        "max_age_seconds": max_age_seconds,
        "stale": stale,
    }


def api_is_healthy(base_url: str | None, timeout: float = 5.0) -> bool | None:
    """Optional API probe; None when no URL is configured."""
    if not base_url:
        return None
    try:
        response = httpx.get(base_url.rstrip("/") + "/healthz", timeout=timeout)
        return response.status_code == 200
    except Exception:
        log.exception("API health check failed")
        return False


def evaluate(health: dict, api_ok: bool | None = None) -> list[str]:
    issues = []
    if health["stale"]:
        age = health["snapshot_age_seconds"]
        if age is None:
            issues.append("no account snapshot has ever been written")
        else:
            issues.append(
                f"newest account snapshot is {age / 60:.1f} min old "
                f"(limit {health['max_age_seconds'] / 60:.0f} min)")
    if api_ok is False:
        issues.append("API health check failed")
    return issues


def post(webhook: str | None, alert: str, severity: str, message: str) -> bool:
    if not webhook:
        log.critical("Alert webhook is not configured: %s", message)
        return False
    try:
        response = httpx.post(webhook, json={
            "severity": severity,
            "service": "kube-trader",
            "alert": alert,
            "message": message,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }, timeout=5.0)
        response.raise_for_status()
        return True
    except Exception:
        log.exception("Could not deliver watchdog alert")
        return False


def notify(issues: list[str], *, webhook: str | None,
           repeat_seconds: int = DEFAULT_REPEAT_SECONDS,
           store=None) -> str:
    """Alert while unhealthy (deduped) and announce recovery once."""
    store = store if store is not None else redis_client()
    if issues:
        message = "; ".join(issues)
        try:
            first = store.set(ALERT_KEY, message, nx=True, ex=repeat_seconds)
        except Exception:
            log.exception("Could not record the watchdog alert key; alerting anyway")
            first = True
        if first:
            post(webhook, "pipeline_stale", "critical", message)
            log.critical("Pipeline watchdog: %s", message)
            return "alerted"
        log.info("Pipeline still unhealthy (alert already sent): %s", message)
        return "suppressed"
    try:
        recovered = store.delete(ALERT_KEY)
    except Exception:
        log.exception("Could not clear the watchdog alert key")
        recovered = 0
    if recovered:
        post(webhook, "pipeline_recovered", "warning",
             "Reconciliation snapshots are advancing again")
        log.warning("Pipeline watchdog: recovered")
        return "recovered"
    log.info("Pipeline healthy")
    return "healthy"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-seconds", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    parser.add_argument("--repeat-seconds", type=int, default=DEFAULT_REPEAT_SECONDS)
    parser.add_argument("--api-url", default=os.environ.get("WATCHDOG_API_URL") or None,
                        help="optional in-cluster API base URL to probe")
    parser.add_argument("--webhook", default=os.environ.get("ALERT_WEBHOOK_URL") or None)
    parser.add_argument("--dry-run", action="store_true",
                        help="report status without alerting or touching Redis")
    args = parser.parse_args(argv)

    health = pipeline_health(args.max_age_seconds)
    api_ok = api_is_healthy(args.api_url)
    issues = evaluate(health, api_ok)
    print(f"snapshot age={health['snapshot_age_seconds']}s "
          f"limit={args.max_age_seconds}s stale={health['stale']} api_ok={api_ok}")
    if args.dry_run:
        print("issues:", issues or "none")
        return 0
    outcome = notify(issues, webhook=args.webhook, repeat_seconds=args.repeat_seconds)
    print("outcome:", outcome)
    # A stale pipeline is an operational failure; surface it to the job status too.
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
