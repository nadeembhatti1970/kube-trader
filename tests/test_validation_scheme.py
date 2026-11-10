import unittest
from unittest.mock import MagicMock, patch

from app import database
from app.validation import (
    VALIDATION_SCHEME_VERSION,
    config_key,
    validation_config_key,
)


class ValidationKeyTests(unittest.TestCase):
    def test_scheme_version_is_part_of_the_key(self):
        current = validation_config_key({"strategy": "x", "signal_fast_bars": 5})
        previous = validation_config_key({"strategy": "x", "signal_fast_bars": 5},
                                         scheme_version=1)
        self.assertNotEqual(current["key"], previous["key"])
        self.assertEqual(current["scheme_version"], VALIDATION_SCHEME_VERSION)
        self.assertEqual(previous["scheme_version"], 1)

    def test_namespaces_do_not_collide(self):
        params = {"stop_loss_pct": 0.02}
        intraday = config_key("intraday", params)
        daily = config_key("daily-swing-core", params)
        self.assertNotEqual(intraday["key"], daily["key"])
        self.assertEqual(daily["namespace"], "daily-swing-core")

    def test_key_is_stable_for_the_same_inputs(self):
        first = config_key("intraday", {"a": 1, "b": 2})
        second = config_key("intraday", {"b": 2, "a": 1})
        self.assertEqual(first["key"], second["key"])


class ValidationStorageTests(unittest.TestCase):
    def test_save_records_the_scheme_version(self):
        db = MagicMock()
        with patch.object(database, "connect") as connect:
            connect.return_value.__enter__.return_value = db
            database.save_strategy_validation(
                "hash", "strategy", {"a": 1}, 40, 0.2, 0.05, 0.4, True, {},
                scheme_version=2)

        params = db.execute.call_args.args[1]
        self.assertEqual(params[-1], 2)

    def test_latest_filters_on_the_current_scheme(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = {"validated": True}
        with patch.object(database, "connect") as connect:
            connect.return_value.__enter__.return_value = db
            record = database.latest_strategy_validation("hash")

        self.assertTrue(record["validated"])
        params = db.execute.call_args.args[1]
        self.assertEqual(params, ("hash", VALIDATION_SCHEME_VERSION))


if __name__ == "__main__":
    unittest.main()
