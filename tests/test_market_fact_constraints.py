import unittest

from app.services.market_fact_constraints import _has_composite_symbol_market_fk


class MarketFactConstraintTests(unittest.TestCase):
    def test_composite_symbol_market_fk_requires_cascade(self):
        valid = {
            "constrained_columns": ["symbol_id", "market"],
            "referred_table": "symbols",
            "referred_columns": ["id", "market"],
            "options": {"ondelete": "CASCADE"},
        }
        invalid = {**valid, "options": {}}

        self.assertTrue(_has_composite_symbol_market_fk([valid]))
        self.assertFalse(_has_composite_symbol_market_fk([invalid]))

    def test_single_column_symbol_fk_is_not_sufficient(self):
        self.assertFalse(
            _has_composite_symbol_market_fk(
                [
                    {
                        "constrained_columns": ["symbol_id"],
                        "referred_table": "symbols",
                        "referred_columns": ["id"],
                        "options": {"ondelete": "CASCADE"},
                    }
                ]
            )
        )


if __name__ == "__main__":
    unittest.main()
