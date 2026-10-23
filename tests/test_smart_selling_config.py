"""Configuration contracts for fixed Smart selling participation percentages."""

from decimal import Decimal
import json
from pathlib import Path
import unittest

from trading_ui.smart_selling_config import normalize_smart_selling_config


class SmartSellingConfigTests(unittest.TestCase):
    def test_defaults_and_partial_overrides_use_percentage_units(self):
        expected = {"patient": "3", "normal": "6", "urgent": "10"}
        for raw in (None, {}, {"participation_percentages": {}}):
            with self.subTest(raw=raw):
                self.assertEqual(
                    normalize_smart_selling_config(raw)["participation_percentages"],
                    expected,
                )
        raw = {"participation_percentages": {"normal": "7.25"}}
        normalized = normalize_smart_selling_config(raw)["participation_percentages"]
        self.assertEqual(normalized, {**expected, "normal": "7.25"})
        self.assertEqual(raw, {"participation_percentages": {"normal": "7.25"}})
        normalized["patient"] = "99"
        self.assertEqual(normalize_smart_selling_config()["participation_percentages"], expected)

    def test_decimal_values_remain_exact_in_json(self):
        normalized = normalize_smart_selling_config(
            {
                "participation_percentages": {
                    "patient": "0.123456789012345678901234",
                    "normal": Decimal("6.000000000000000000000001"),
                    "urgent": 100,
                }
            }
        )["participation_percentages"]
        self.assertEqual(
            json.loads(json.dumps(normalized)),
            {
                "patient": "0.123456789012345678901234",
                "normal": "6.000000000000000000000001",
                "urgent": "100",
            },
        )
        self.assertEqual(
            normalize_smart_selling_config({"participation_percentages": {"patient": 1e-24}})[
                "participation_percentages"
            ]["patient"],
            "0.000000000000000000000001",
        )

    def test_participation_settings_require_an_object_with_known_modes(self):
        for invalid in (None, True, False, 3, "3", [], ["patient", 3]):
            with self.subTest(value=invalid):
                with self.assertRaisesRegex(
                    ValueError, "participation_percentages must be an object"
                ):
                    normalize_smart_selling_config({"participation_percentages": invalid})
        for unknown in ("fast", "Patient", "", 1):
            with self.subTest(mode=unknown):
                with self.assertRaisesRegex(ValueError, "Unknown.*participation_percentages"):
                    normalize_smart_selling_config({"participation_percentages": {unknown: 5}})

    def test_percentage_values_reject_invalid_numbers_and_precision(self):
        invalid_values = (
            True,
            False,
            None,
            "",
            "lots",
            "6%",
            [],
            {},
            float("nan"),
            float("inf"),
            float("-inf"),
            Decimal("sNaN"),
            "NaN",
            "Infinity",
            0,
            "-0",
            -1,
            "100.000000000000000000000001",
            101,
            "1e-25",
            "1.234567890123456789012345678901",
            "1e1000000",
        )
        for mode in ("patient", "normal", "urgent"):
            for invalid in invalid_values:
                with self.subTest(mode=mode, value=invalid):
                    with self.assertRaisesRegex(ValueError, f"participation_percentages.{mode}"):
                        normalize_smart_selling_config(
                            {"participation_percentages": {mode: invalid}}
                        )

    def test_sample_configs_include_valid_default_percentages(self):
        sample_dir = Path(__file__).resolve().parents[1] / "trading_ui" / "sample"
        for filename in ("config.json", "smart_selling.settings.example.json"):
            with self.subTest(filename=filename):
                settings = json.loads((sample_dir / filename).read_text())["smart_selling"]
                self.assertEqual(
                    normalize_smart_selling_config(settings)["participation_percentages"],
                    {"patient": "3", "normal": "6", "urgent": "10"},
                )


if __name__ == "__main__":
    unittest.main()
