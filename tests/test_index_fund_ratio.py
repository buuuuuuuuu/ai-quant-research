import unittest
from datetime import date, timedelta
from decimal import Decimal

from ai_quant_assistant.research.index_fund_ratio import (
    FundObservation,
    RankingObservation,
    calculate_ratio,
    select_top_funds,
)


class IndexFundRatioTests(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 10, 28)
        self.funds = ("SSE.1", "SZSE.2")
        self.rows = (
            FundObservation(self.funds[0], self.day, Decimal("100"), Decimal("2")),
            FundObservation(self.funds[1], self.day, Decimal("200"), Decimal("1")),
        )

    def test_known_ratio(self):
        result = calculate_ratio(self.day, self.funds, self.rows, self.day, Decimal("10000"))
        self.assertEqual(result.numerator_cny, Decimal("400"))
        self.assertEqual(result.ratio_fraction, Decimal("0.04"))
        self.assertEqual(result.missing, ())

    def test_missing_or_stale_values_do_not_create_a_ratio(self):
        result = calculate_ratio(self.day, self.funds, self.rows[:1], self.day, Decimal("10000"))
        self.assertIsNone(result.numerator_cny)
        self.assertIsNone(result.ratio_fraction)
        self.assertIn("SZSE.2:observation", result.missing)
        result = calculate_ratio(self.day, self.funds, self.rows, self.day - timedelta(days=1), Decimal("10000"))
        self.assertIsNone(result.ratio_fraction)
        self.assertEqual(result.missing, ("index:float_market_cap",))

    def test_invalid_or_duplicate_inputs(self):
        bad = (self.rows[0], FundObservation(self.funds[1], self.day, Decimal("NaN"), Decimal("1")))
        self.assertIsNone(calculate_ratio(self.day, self.funds, bad, self.day, Decimal("10000")).ratio_fraction)
        with self.assertRaises(ValueError):
            calculate_ratio(self.day, self.funds, (self.rows[0], self.rows[0]), self.day, Decimal("10000"))

    def test_ranking_is_complete_tie_breaks_and_never_uses_future_data(self):
        next_day = self.day + timedelta(days=1)
        observations = (
            RankingObservation(self.funds[1], self.day, self.day, Decimal("2"), Decimal("1")),
            RankingObservation(self.funds[0], self.day, self.day, Decimal("1"), Decimal("2")),
        )
        self.assertEqual(select_top_funds(self.funds, observations, self.day, next_day, 1), (self.funds[0],))
        with self.assertRaises(ValueError):
            select_top_funds(self.funds, observations[:1], self.day, next_day, 1)
        with self.assertRaises(ValueError):
            select_top_funds(self.funds, observations, self.day, self.day, 1)
        future = (observations[0], RankingObservation(self.funds[0], self.day, next_day + timedelta(days=1), Decimal("1"), Decimal("2")))
        with self.assertRaises(ValueError):
            select_top_funds(self.funds, future, self.day, next_day, 1)


if __name__ == "__main__":
    unittest.main()
