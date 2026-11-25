import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app import watchdog


class FakeStore:
    def __init__(self):
        self.values = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def delete(self, key):
        return 1 if self.values.pop(key, None) is not None else 0


NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)


class PipelineHealthTests(unittest.TestCase):
    def health(self, snapshot_at, max_age=900):
        with patch.object(watchdog, "newest_account_snapshot_at",
                          return_value=snapshot_at):
            return watchdog.pipeline_health(max_age, now=NOW)

    def test_fresh_snapshot_is_healthy(self):
        health = self.health(NOW - timedelta(seconds=45))
        self.assertFalse(health["stale"])
        self.assertAlmostEqual(health["snapshot_age_seconds"], 45.0)
        self.assertEqual(watchdog.evaluate(health), [])

    def test_old_snapshot_is_stale(self):
        health = self.health(NOW - timedelta(minutes=40))
        self.assertTrue(health["stale"])
        issues = watchdog.evaluate(health)
        self.assertEqual(len(issues), 1)
        self.assertIn("40.0 min old", issues[0])

    def test_missing_snapshot_is_stale(self):
        health = self.health(None)
        self.assertTrue(health["stale"])
        self.assertIsNone(health["snapshot_age_seconds"])
        self.assertIn("no account snapshot", watchdog.evaluate(health)[0])

    def test_naive_timestamp_is_treated_as_utc(self):
        health = self.health(datetime(2026, 11, 24, 14, 59, 30))
        self.assertFalse(health["stale"])
        self.assertAlmostEqual(health["snapshot_age_seconds"], 30.0)

    def test_api_failure_is_reported(self):
        health = self.health(NOW)
        self.assertEqual(len(watchdog.evaluate(health, api_ok=False)), 1)
        self.assertEqual(watchdog.evaluate(health, api_ok=None), [])


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()

    def send(self, issues, store=None):
        with patch.object(watchdog, "post", return_value=True) as post:
            outcome = watchdog.notify(issues, webhook="https://example.test/hook",
                                      store=store or self.store)
        return outcome, post

    def test_first_failure_alerts_then_suppresses(self):
        outcome, post = self.send(["stale"])
        self.assertEqual(outcome, "alerted")
        post.assert_called_once()
        outcome, post = self.send(["stale"])
        self.assertEqual(outcome, "suppressed")
        post.assert_not_called()

    def test_recovery_is_announced_once(self):
        self.send(["stale"])
        outcome, post = self.send([])
        self.assertEqual(outcome, "recovered")
        post.assert_called_once()
        self.assertEqual(post.call_args.args[1], "pipeline_recovered")
        outcome, post = self.send([])
        self.assertEqual(outcome, "healthy")
        post.assert_not_called()

    def test_healthy_without_prior_alert_is_silent(self):
        outcome, post = self.send([])
        self.assertEqual(outcome, "healthy")
        post.assert_not_called()

    def test_redis_failure_still_alerts(self):
        broken = MagicMock()
        broken.set.side_effect = RuntimeError("redis down")
        outcome, post = self.send(["stale"], store=broken)
        self.assertEqual(outcome, "alerted")
        post.assert_called_once()


class MainTests(unittest.TestCase):
    def test_dry_run_reports_without_alerting(self):
        with patch.object(watchdog, "pipeline_health",
                          return_value={"snapshot_age_seconds": 3600.0,
                                        "max_age_seconds": 900, "stale": True,
                                        "newest_snapshot_at": None,
                                        "captured_at": NOW.isoformat()}), \
                patch.object(watchdog, "api_is_healthy", return_value=None), \
                patch.object(watchdog, "notify") as notify, \
                patch("builtins.print"):
            code = watchdog.main(["--dry-run"])

        self.assertEqual(code, 0)
        notify.assert_not_called()

    def test_stale_pipeline_exits_non_zero(self):
        with patch.object(watchdog, "pipeline_health",
                          return_value={"snapshot_age_seconds": 3600.0,
                                        "max_age_seconds": 900, "stale": True,
                                        "newest_snapshot_at": None,
                                        "captured_at": NOW.isoformat()}), \
                patch.object(watchdog, "api_is_healthy", return_value=None), \
                patch.object(watchdog, "notify", return_value="alerted"), \
                patch("builtins.print"):
            code = watchdog.main([])

        self.assertEqual(code, 1)

    def test_healthy_pipeline_exits_zero(self):
        with patch.object(watchdog, "pipeline_health",
                          return_value={"snapshot_age_seconds": 30.0,
                                        "max_age_seconds": 900, "stale": False,
                                        "newest_snapshot_at": None,
                                        "captured_at": NOW.isoformat()}), \
                patch.object(watchdog, "api_is_healthy", return_value=True), \
                patch.object(watchdog, "notify", return_value="healthy"), \
                patch("builtins.print"):
            code = watchdog.main([])

        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
