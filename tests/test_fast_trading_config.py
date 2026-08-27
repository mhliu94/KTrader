import json
import math
import tempfile
import unittest
from pathlib import Path

from trading_ui.config import load_config


class FastTradingConfigTests(unittest.TestCase):
    def _load(self, fast_trading_marker=...):
        config = {
            "accounts": [{"id": "unused-by-load-config"}],
            "symbols": ["AAPL"],
            "fallback": {"file": "fallback.json"},
            "auth": {
                "users": {
                    "admin": {"password": "admin-password"},
                    "trader": {"password": "trader-password"},
                }
            },
        }
        if fast_trading_marker is not ...:
            config["fast_trading"] = fast_trading_marker

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            return load_config(str(path))

    def test_loads_complete_defaults_when_section_is_omitted(self):
        fast_trading = self._load()["fast_trading"]

        self.assertEqual(
            fast_trading["aggression_levels"],
            {
                1: {"book_levels": 2, "cycle_seconds": 30.0},
                2: {"book_levels": 3, "cycle_seconds": 20.0},
                3: {"book_levels": 5, "cycle_seconds": 10.0},
            },
        )
        self.assertEqual(
            fast_trading["minimum_cycle_seconds_by_medium"],
            {"API": 3.0, "WEB": 30.0, "WINDOWS": 40.0, "EMULATOR": 40.0},
        )

    def test_normalizes_custom_levels_and_merges_medium_overrides(self):
        fast_trading = self._load(
            {
                "aggression_levels": {
                    "1": {"book_levels": 1, "cycle_seconds": 12.5},
                    "2": {"book_levels": 4, "cycle_seconds": 8},
                    "3": {"book_levels": 9, "cycle_seconds": 4},
                },
                "minimum_cycle_seconds_by_medium": {"api": 1.5, "web": 25},
            }
        )["fast_trading"]

        self.assertEqual(set(fast_trading["aggression_levels"]), {1, 2, 3})
        self.assertEqual(
            fast_trading["aggression_levels"][1],
            {"book_levels": 1, "cycle_seconds": 12.5},
        )
        self.assertEqual(
            fast_trading["minimum_cycle_seconds_by_medium"],
            {"API": 1.5, "WEB": 25.0, "WINDOWS": 40.0, "EMULATOR": 40.0},
        )

    def test_rejects_missing_or_unknown_aggression_levels(self):
        valid_level = {"book_levels": 2, "cycle_seconds": 30}
        for levels in (
            {"1": valid_level, "2": valid_level},
            {"1": valid_level, "2": valid_level, "3": valid_level, "4": valid_level},
        ):
            with self.subTest(levels=levels), self.assertRaisesRegex(ValueError, "exactly levels 1, 2, and 3"):
                self._load({"aggression_levels": levels})

    def test_rejects_non_positive_or_non_integer_book_levels(self):
        for value in (0, -1, 2.5, True):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "positive integer"):
                self._load(
                    {
                        "aggression_levels": {
                            "1": {"book_levels": value, "cycle_seconds": 30},
                            "2": {"book_levels": 3, "cycle_seconds": 20},
                            "3": {"book_levels": 5, "cycle_seconds": 10},
                        }
                    }
                )

        with self.assertRaisesRegex(ValueError, "at most 20"):
            self._load(
                {
                    "aggression_levels": {
                        "1": {"book_levels": 21, "cycle_seconds": 30},
                        "2": {"book_levels": 3, "cycle_seconds": 20},
                        "3": {"book_levels": 5, "cycle_seconds": 10},
                    }
                }
            )

    def test_rejects_non_positive_or_non_finite_cycle_seconds(self):
        for value in (0, -1, math.nan, math.inf, True, "10"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "positive finite number"):
                self._load(
                    {
                        "aggression_levels": {
                            "1": {"book_levels": 2, "cycle_seconds": value},
                            "2": {"book_levels": 3, "cycle_seconds": 20},
                            "3": {"book_levels": 5, "cycle_seconds": 10},
                        }
                    }
                )

    def test_rejects_invalid_medium_overrides(self):
        for overrides in ({"API": 0}, {"API": math.inf}, {"MOBILE": 10}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self._load({"minimum_cycle_seconds_by_medium": overrides})


if __name__ == "__main__":
    unittest.main()
