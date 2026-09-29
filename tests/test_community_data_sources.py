import json
import sys
from datetime import date, datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.services.openbb_client import HistoricalPriceRequest
from app.services.providers.fundamental import (
    CommunityCNFundamentalProvider,
    GlobalStockDataSECFundamentalProvider,
    resolve_fundamental_provider,
)
from app.services.providers.price import AStockDataTencentPriceProvider, resolve_price_provider
from app.services.tushare_client import TushareClient


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
    def test_cn_auto_fundamentals_resolve_to_credential_free_provider(self):
        self.assertIsInstance(
            resolve_fundamental_provider("auto", market="CN"),
            CommunityCNFundamentalProvider,
        )

    def test_community_provider_maps_financial_availability_and_valuation_times(self):
        provider = CommunityCNFundamentalProvider()
        financial_rows = [
            {
                "SECUCODE": "600519.SH",
                "SECURITY_NAME_ABBR": "贵州茅台",
                "REPORT_DATE": "2026-06-30 00:00:00",
                "NOTICE_DATE": "2026-08-10 00:00:00",
                "UPDATE_DATE": "2026-08-11 00:00:00",
                "PARENTNETPROFITTZ": 12.5,
                "TOTALOPERATEREVETZ": 8.4,
                "ZCFZL": 16.2,
                "ROEJQ": 15.0,
            },
            {
                "SECUCODE": "600519.SH",
                "REPORT_DATE": "2025-12-31 00:00:00",
                "NOTICE_DATE": "2026-03-30 00:00:00",
                "UPDATE_DATE": "2026-03-30 00:00:00",
                "ROEJQ": 30.0,
            },
            {
                "SECUCODE": "600519.SH",
                "REPORT_DATE": "2024-12-31 00:00:00",
                "NOTICE_DATE": "2025-03-30 00:00:00",
                "UPDATE_DATE": "2025-03-30 00:00:00",
                "ROEJQ": 27.0,
            },
            {
                "SECUCODE": "600519.SH",
                "REPORT_DATE": "2023-12-31 00:00:00",
                "NOTICE_DATE": "2024-03-30 00:00:00",
                "UPDATE_DATE": "2024-03-30 00:00:00",
                "ROEJQ": 24.0,
            },
        ]
        snapshots = provider._build_snapshots(
            ["600519.SS"],
            financial_rows=financial_rows,
            valuation_by_ticker={"600519.SS": {"f14": "贵州茅台", "f20": 1.8e12, "f115": 20.5, "close": 1500.0}},
            dividends_by_ticker={"600519.SS": [{"cash_per_ten": 30.0, "ex_dividend_date": "2026-06-30"}]},
            metadata={},
            as_of_date=date(2026, 8, 14),
        )

        self.assertEqual(1, len(snapshots))
        row = snapshots[0]
        self.assertEqual("2026-06-30", row["report_date"])
        self.assertEqual(27.0, row["roe_avg_3y"])
        self.assertEqual(12.5, row["net_profit_yoy"])
        self.assertEqual(8.4, row["revenue_yoy"])
        self.assertEqual(16.2, row["debt_to_assets"])
        self.assertEqual(20.5, row["pe_ttm"])
        self.assertEqual(0.2, row["dividend_yield"])
        self.assertEqual("2026-08-11T23:59:59.999999+08:00", row["available_time"])
        self.assertEqual("2026-08-14T00:00:00+08:00", row["feature_times"]["pe_ttm"]["event_time"])
        ingested_time = datetime.fromisoformat(row["ingested_time"])
        self.assertGreater(ingested_time, datetime.fromisoformat(row["available_time"]))
        self.assertEqual(
            row["ingested_time"],
            row["feature_times"]["pe_ttm"]["ingested_time"],
        )
        self.assertGreater(
            ingested_time,
            datetime.fromisoformat(row["feature_times"]["pe_ttm"]["available_time"]),
        )

    def test_tushare_endpoint_permission_errors_can_degrade_independently(self):
        client = TushareClient.__new__(TushareClient)

        self.assertTrue(
            client._is_endpoint_permission_error(
                RuntimeError("没有接口(fina_indicator)访问权限"),
                "fina_indicator",
            )
        )
        self.assertFalse(
            client._is_endpoint_permission_error(
                RuntimeError("fina_indicator rate limit"),
                "fina_indicator",
            )
        )
        self.assertFalse(
            client._is_endpoint_permission_error(
                RuntimeError("daily_basic access denied"),
                "fina_indicator",
            )
        )

    def test_tushare_batches_daily_basic_and_disables_denied_financial_endpoint(self):
        class FakeFrame:
            empty = False

            def to_dict(self, orient):
                self_orient = orient
                return [
                    {
                        "ts_code": "600519.SH",
                        "trade_date": "20260814",
                        "pe_ttm": 20.0,
                        "dv_ttm": 1.5,
                        "total_mv": 100.0,
                    },
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20260814",
                        "pe_ttm": 8.0,
                        "dv_ttm": 2.0,
                        "total_mv": 50.0,
                    },
                ]

        class FakePro:
            def __init__(self):
                self.daily_calls = 0
                self.financial_calls = 0

            def daily_basic(self, **kwargs):
                self.daily_calls += 1
                self.daily_kwargs = kwargs
                return FakeFrame()

            def fina_indicator(self, **_kwargs):
                self.financial_calls += 1
                raise RuntimeError("没有接口(fina_indicator)访问权限")

        pro = FakePro()
        fake_module = SimpleNamespace(pro_api=lambda _token: pro)
        client = TushareClient.__new__(TushareClient)
        client.token = "fixture"
        metadata = {
            "600519.SS": {"name": "A", "exchange": "SSE"},
            "000001.SZ": {"name": "B", "exchange": "SZSE"},
        }
        with patch.dict(sys.modules, {"tushare": fake_module}):
            rows = client.fetch_cn_growth_value_candidates(
                ["600519.SS", "000001.SZ"],
                stock_meta_by_ticker=metadata,
                as_of_date="2026-08-14",
            )

        self.assertEqual(1, pro.daily_calls)
        self.assertEqual("20260814", pro.daily_kwargs["trade_date"])
        self.assertEqual(1, pro.financial_calls)
        self.assertEqual(2, len(rows))
        self.assertEqual({"600519.SS", "000001.SZ"}, {item.ticker for item in rows})
        self.assertTrue(
            all("fina_indicator" in item.raw_data["unavailable_endpoints"] for item in rows)
        )
        self.assertEqual(1_000_000.0, rows[0].market_cap)

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
