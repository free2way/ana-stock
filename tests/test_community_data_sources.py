import json
import unittest
from unittest.mock import patch

from app.services.openbb_client import HistoricalPriceRequest
from app.services.providers.fundamental import GlobalStockDataSECFundamentalProvider
from app.services.providers.price import AStockDataTencentPriceProvider, resolve_price_provider


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class CommunityDataSourceTests(unittest.TestCase):
    def test_a_stock_tencent_provider_parses_adjusted_daily_bars(self):
        payload = {
            "data": {
                "sh600519": {
                    "qfqday": [
                        ["2026-07-23", "1410.0", "1420.0", "1430.0", "1400.0", "12345"],
                        ["2026-07-24", "1420.0", "1415.0", "1425.0", "1410.0", "23456"],
                    ]
                }
            }
        }
        with patch("app.services.providers.price.urlopen", return_value=_FakeResponse(payload)):
            rows = AStockDataTencentPriceProvider().fetch_historical_prices(
                HistoricalPriceRequest(ticker="600519.SS", start_date="2026-07-01", end_date="2026-07-24")
            )
        self.assertEqual(2, len(rows))
        self.assertEqual("600519.SS", rows[0]["symbol"])
        self.assertEqual(1420.0, rows[0]["adj_close"])
        self.assertEqual("2026-07-24", rows[-1]["date"])

    def test_a_stock_provider_is_only_resolved_for_cn(self):
        self.assertIsInstance(resolve_price_provider("a_stock_data_tencent", market="CN"), AStockDataTencentPriceProvider)
        self.assertNotIsInstance(resolve_price_provider("a_stock_data_tencent", market="US"), AStockDataTencentPriceProvider)

    def test_sec_company_facts_extracts_latest_annual_growth_and_leverage(self):
        facts = {
            "facts": {
                "us-gaap": {
                    "Revenues": {"units": {"USD": [
                        {"form": "10-K", "fp": "FY", "end": "2025-12-31", "filed": "2026-02-01", "val": 120},
                        {"form": "10-K", "fp": "FY", "end": "2024-12-31", "filed": "2025-02-01", "val": 100},
                    ]}},
                    "NetIncomeLoss": {"units": {"USD": [
                        {"form": "10-K", "fp": "FY", "end": "2025-12-31", "filed": "2026-02-01", "val": 24},
                        {"form": "10-K", "fp": "FY", "end": "2024-12-31", "filed": "2025-02-01", "val": 20},
                    ]}},
                    "Assets": {"units": {"USD": [{"form": "10-K", "fp": "FY", "end": "2025-12-31", "filed": "2026-02-01", "val": 200}]}},
                    "Liabilities": {"units": {"USD": [{"form": "10-K", "fp": "FY", "end": "2025-12-31", "filed": "2026-02-01", "val": 80}]}},
                }
            }
        }
        snapshot = GlobalStockDataSECFundamentalProvider.__new__(GlobalStockDataSECFundamentalProvider)._facts_to_snapshot(
            ticker="ACME", cik=123, company_name="Acme Inc.", facts=facts
        )
        self.assertEqual("2025-12-31", snapshot["report_date"])
        self.assertAlmostEqual(20.0, snapshot["revenue_yoy"])
        self.assertAlmostEqual(20.0, snapshot["net_profit_yoy"])
        self.assertEqual(40.0, snapshot["debt_to_assets"])
        self.assertEqual("0000000123", snapshot["raw_data"]["cik"])
