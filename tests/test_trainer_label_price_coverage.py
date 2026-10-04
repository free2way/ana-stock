"""Fail-closed per-label adjusted-view coverage (A1 follow-up).

Covers the required behaviours:

1. a fully adjusted window labels normally and is reported as ``adjusted``;
2. a partially covered view is blocked by default / reported as ``mixed``;
3. a window that mixes adjusted and raw points drops the sample and counts it;
4. explicitly opting out runs successfully with truthful metadata;
5. an ``absent`` view is fail-closed by default and only trains raw when
   ``PQW_TRAINER_ALLOW_RAW_FALLBACK=true`` (recording ``mixed:0.00000000`` and
   ``raw_fallback_allowed=true``);
6. an ``unreadable`` view (file present but corrupt) always raises, even with
   the raw opt-in on.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.services.stock_selection.training_weights import TRAINING_WEIGHT_POLICY
from app.services.trainer import EXECUTABLE_LABEL_PROFILE, SignalTrainer


def _symbol_rows(symbol: str, *, count: int = 12, start: float = 10.0) -> list[dict]:
    rows: list[dict] = []
    for index in range(count):
        close = start + 0.1 * index
        rows.append(
            {
                "symbol": symbol,
                "date": f"2026-01-{index + 1:02d}",
                "open": close - 0.02,
                "high": close + 0.05,
                "low": close - 0.05,
                "close": close,
                "volume": 1_000_000.0,
            }
        )
    return rows


def _view_for(rows: list[dict], *, missing_dates: set[str] | None = None, scale: float = 1.0) -> dict:
    missing = missing_dates or set()
    payload: dict[str, dict[str, dict[str, float]]] = {}
    for row in rows:
        if row["date"] in missing:
            continue
        payload.setdefault(row["symbol"], {})[row["date"]] = {
            field: float(row[field]) * scale for field in ("open", "high", "low", "close")
        }
    return payload


class TrainerLabelPriceCoverageTests(TestCase):
    def test_full_adjusted_window_labels_adjusted(self) -> None:
        rows = _symbol_rows("FULL")
        trainer = SignalTrainer()
        with patch(
            "app.services.adjusted_view_store.load_adjusted_bars",
            return_value=_view_for(rows, scale=2.0),
        ):
            attached = trainer._attach_adjusted_basis(rows, market="CN")
        self.assertEqual(len(rows), attached)
        self.assertEqual("adjusted_view", trainer._label_basis)

        samples = trainer._build_lightgbm_samples(
            rows=rows,
            lookback_days=3,
            horizon_days=5,
            symbol_feature_context={},
            market="CN",
        )
        labeled = [sample for sample in samples if sample["target"] is not None]
        self.assertTrue(labeled)
        self.assertTrue(all(sample["label_price_basis"] == "adjusted" for sample in labeled))
        self.assertEqual(
            {"adjusted_count": len(labeled), "raw_fallback_count": 0, "dropped_missing_adjusted_count": 0},
            trainer._label_price_stats,
        )

        trainer._adjusted_view_state = "present"
        contract = trainer._label_price_basis_contract(label_profile=EXECUTABLE_LABEL_PROFILE)
        self.assertEqual("adjusted_view", contract["label_price_basis"])
        self.assertEqual(1.0, contract["adjusted_coverage_share"])

    def test_window_missing_one_adjusted_point_is_dropped_and_counted(self) -> None:
        mix_rows = _symbol_rows("MIX")
        raw_rows = _symbol_rows("RAW")
        # One adjusted point is absent inside the windows of sessions 3..7.
        missing_date = mix_rows[7]["date"]
        with patch(
            "app.services.adjusted_view_store.load_adjusted_bars",
            return_value=_view_for(mix_rows, missing_dates={missing_date}),
        ):
            trainer = SignalTrainer()
            trainer._attach_adjusted_basis(mix_rows + raw_rows, market="CN")

        samples = trainer._build_lightgbm_samples(
            rows=mix_rows + raw_rows,
            lookback_days=3,
            horizon_days=5,
            symbol_feature_context={},
            market="CN",
        )
        stats = trainer._label_price_stats
        # RAW is entirely absent from the view: whole windows are consistently
        # raw and are still labeled, but flagged as raw fallback.
        self.assertGreater(stats["raw_fallback_count"], 0)
        # MIX windows straddling the missing point mix bases and are dropped.
        self.assertGreater(stats["dropped_missing_adjusted_count"], 0)
        labeled = [sample for sample in samples if sample["target"] is not None]
        self.assertTrue(all(sample["label_price_basis"] in {"adjusted", "raw_fallback"} for sample in labeled))
        self.assertNotIn("dropped_missing_adjusted", {sample["label_price_basis"] for sample in labeled})

        dropped = [
            sample for sample in samples if sample["label_price_basis"] == "dropped_missing_adjusted"
        ]
        self.assertTrue(dropped)
        self.assertTrue(all(sample["target"] is None for sample in dropped))
        # The dropped rows survive as unlabeled prediction-only feature rows.
        self.assertTrue(all(sample["symbol"] == "MIX" for sample in dropped))
        self.assertEqual(stats["dropped_missing_adjusted_count"], len(dropped))

    def _trainer_with_stats(self, **stats: int) -> SignalTrainer:
        trainer = SignalTrainer()
        trainer._adjusted_basis_expected = True
        trainer._adjusted_view_state = "present"
        trainer._adjusted_view_market = "CN"
        trainer._label_price_stats = dict(stats)
        return trainer

    def test_contract_blocks_incomplete_coverage_by_default(self) -> None:
        trainer = self._trainer_with_stats(
            adjusted_count=8, raw_fallback_count=1, dropped_missing_adjusted_count=1
        )
        with self.assertRaisesRegex(RuntimeError, "incomplete coverage"):
            trainer._label_price_basis_contract(label_profile=EXECUTABLE_LABEL_PROFILE)

    def test_contract_reports_mixed_when_gate_disabled(self) -> None:
        trainer = self._trainer_with_stats(
            adjusted_count=8, raw_fallback_count=1, dropped_missing_adjusted_count=1
        )
        contract = trainer._label_price_basis_contract(
            label_profile=EXECUTABLE_LABEL_PROFILE, require_full=False
        )
        self.assertTrue(str(contract["label_price_basis"]).startswith("mixed:"))
        self.assertAlmostEqual(0.8, contract["adjusted_coverage_share"])
        self.assertEqual(8, contract["adjusted_count"])
        self.assertEqual(1, contract["raw_fallback_count"])
        self.assertEqual(1, contract["dropped_missing_adjusted_count"])

    def test_contract_empty_sample_set_is_adjusted(self) -> None:
        trainer = self._trainer_with_stats(
            adjusted_count=0, raw_fallback_count=0, dropped_missing_adjusted_count=0
        )
        contract = trainer._label_price_basis_contract(label_profile=EXECUTABLE_LABEL_PROFILE)
        self.assertEqual("adjusted_view", contract["label_price_basis"])

    def test_contract_raw_when_adjusted_view_not_applicable(self) -> None:
        trainer = SignalTrainer()
        trainer._adjusted_basis_expected = False
        trainer._adjusted_view_state = "absent"
        trainer._label_price_stats = {
            "adjusted_count": 0,
            "raw_fallback_count": 5,
            "dropped_missing_adjusted_count": 5,
        }
        contract = trainer._label_price_basis_contract(label_profile=EXECUTABLE_LABEL_PROFILE)
        self.assertEqual("raw", contract["label_price_basis"])

    def test_contract_absent_view_blocks_by_default(self) -> None:
        # A CN/US run with no adjusted view at all is now fail-closed: raw
        # labels are not silently produced from a missing view.
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_allow_raw_fallback": False}
        )
        trainer._adjusted_basis_expected = True
        trainer._adjusted_view_state = "absent"
        trainer._adjusted_view_market = "US"
        trainer._label_price_stats = {
            "adjusted_count": 0,
            "raw_fallback_count": 1745,
            "dropped_missing_adjusted_count": 0,
        }
        with self.assertRaisesRegex(RuntimeError, "without an adjusted view"):
            trainer._label_price_basis_contract(label_profile=EXECUTABLE_LABEL_PROFILE)

    def test_contract_absent_view_allowed_with_explicit_opt_in(self) -> None:
        # Explicit opt-in keeps raw labels but records the truthful basis: no
        # view, `mixed:0.00000000`, and the allow flag set.
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={
                "trainer_allow_raw_fallback": True,
                "optin_reason": "fixture: absent view accepted for raw-label run",
                "optin_operator": "fixture_operator",
            }
        )
        trainer._adjusted_basis_expected = True
        trainer._adjusted_view_state = "absent"
        trainer._adjusted_view_market = "US"
        trainer._label_price_stats = {
            "adjusted_count": 0,
            "raw_fallback_count": 1745,
            "dropped_missing_adjusted_count": 0,
        }
        contract = trainer._label_price_basis_contract(label_profile=EXECUTABLE_LABEL_PROFILE)
        self.assertEqual("mixed:0.00000000", contract["label_price_basis"])
        self.assertFalse(contract["adjusted_view_present"])
        self.assertEqual("absent", contract["adjusted_view_state"])
        self.assertTrue(contract["raw_fallback_allowed"])
        self.assertAlmostEqual(0.0, contract["adjusted_coverage_share"])
        # The opt-in is upgraded to a structured, accountable record.
        audit = contract["raw_fallback_optin_audit"]
        self.assertTrue(audit["enabled"])
        self.assertEqual("fixture_operator", audit["operator"])
        self.assertEqual("run_parameter", audit["operator_source"])
        self.assertEqual("fixture: absent view accepted for raw-label run", audit["reason"])
        self.assertEqual("PQW_TRAINER_ALLOW_RAW_FALLBACK", audit["source"])
        self.assertEqual("train", audit["scope"]["entry_point"])
        self.assertEqual("US", audit["scope"]["market"])
        self.assertIsNotNone(audit["decided_at"])

    def test_contract_absent_view_opt_in_without_reason_is_refused(self) -> None:
        # The upgraded opt-in is fail-closed: an enabled opt-in with no auditable
        # reason must refuse the run instead of silently waiving the gate.
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_allow_raw_fallback": True, "optin_reason": None}
        )
        trainer._adjusted_basis_expected = True
        trainer._adjusted_view_state = "absent"
        trainer._adjusted_view_market = "US"
        trainer._label_price_stats = {
            "adjusted_count": 0,
            "raw_fallback_count": 1745,
            "dropped_missing_adjusted_count": 0,
        }
        with self.assertRaisesRegex(RuntimeError, "without a reason"), patch.dict(
            "os.environ", {"PQW_OPTIN_REASON": ""}, clear=False
        ):
            trainer._label_price_basis_contract(label_profile=EXECUTABLE_LABEL_PROFILE)

    def test_contract_present_view_opt_in_not_exercised_still_requires_reason(self) -> None:
        # Unified strictness: a standing raw-fallback opt-in requires a reason
        # even when the adjusted view is present and the waiver is not actually
        # exercised. Otherwise an enabled waiver could be persisted without an
        # attributable reason.
        trainer = self._trainer_with_stats(
            adjusted_count=10, raw_fallback_count=0, dropped_missing_adjusted_count=0
        )
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_allow_raw_fallback": True, "optin_reason": None}
        )
        with self.assertRaisesRegex(RuntimeError, "without a reason"), patch.dict(
            "os.environ", {"PQW_OPTIN_REASON": ""}, clear=False
        ):
            trainer._label_price_basis_contract(label_profile=EXECUTABLE_LABEL_PROFILE)

    def test_contract_present_view_opt_in_with_reason_is_recorded(self) -> None:
        # With a reason supplied the standing opt-in is recorded (enabled, with
        # the reason) even though the adjusted view was used.
        trainer = self._trainer_with_stats(
            adjusted_count=10, raw_fallback_count=0, dropped_missing_adjusted_count=0
        )
        trainer.settings = trainer.settings.model_copy(
            update={
                "trainer_allow_raw_fallback": True,
                "optin_reason": "fixture: standing opt-in kept for the CN pilot",
            }
        )
        contract = trainer._label_price_basis_contract(label_profile=EXECUTABLE_LABEL_PROFILE)
        self.assertEqual("adjusted_view", contract["label_price_basis"])
        audit = contract["raw_fallback_optin_audit"]
        self.assertTrue(audit["enabled"])
        self.assertEqual(
            "fixture: standing opt-in kept for the CN pilot", audit["reason"]
        )

    def test_contract_unreadable_view_raises_and_is_not_treated_as_absent(self) -> None:
        # A view file that exists but cannot be read is fail-closed even with the
        # raw opt-in on: a corrupt view must never masquerade as "no view".
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_allow_raw_fallback": True}
        )
        trainer._adjusted_basis_expected = True
        trainer._adjusted_view_state = "unreadable"
        trainer._adjusted_view_market = "CN"
        trainer._adjusted_view_error = "duckdb.IOException: not a parquet file (view=/tmp/x.parquet)"
        trainer._label_price_stats = {
            "adjusted_count": 0,
            "raw_fallback_count": 1745,
            "dropped_missing_adjusted_count": 0,
        }
        with self.assertRaisesRegex(RuntimeError, "unreadable"):
            trainer._label_price_basis_contract(label_profile=EXECUTABLE_LABEL_PROFILE)

    def test_attach_marks_corrupt_view_unreadable(self) -> None:
        # End-to-end detection: a real file at the view path that is not a valid
        # parquet is reported as `unreadable`, never as `absent`.
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            corrupt = Path(tmp) / "adjusted.parquet"
            corrupt.write_bytes(b"not a parquet file")
            trainer = SignalTrainer()
            rows = _symbol_rows("CN1")
            with patch(
                "app.services.adjusted_view_store.adjusted_view_path",
                return_value=corrupt,
            ):
                attached = trainer._attach_adjusted_basis(rows, market="CN")
            self.assertEqual(0, attached)
            self.assertEqual("unreadable", trainer._adjusted_view_state)
            self.assertIn("adjusted.parquet", str(trainer._adjusted_view_error))
            with self.assertRaisesRegex(RuntimeError, "unreadable"):
                trainer._label_price_basis_contract(label_profile=EXECUTABLE_LABEL_PROFILE)

    def _integration_samples(self) -> list[dict]:
        dates = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(50)]
        tickers = [f"FIXTURE{i:03}" for i in range(100)]
        return [
            {
                "symbol": ticker,
                "trade_date": day,
                "features": {"fixture_day": i},
                "target": 0.01 if i + 6 < len(dates) else None,
                "label_end_date": dates[i + 6] if i + 6 < len(dates) else None,
                "label_available_date": dates[i + 6] if i + 6 < len(dates) else None,
            }
            for i, day in enumerate(dates)
            for ticker in tickers
        ]

    def test_train_lightgbm_blocks_before_persisting_when_coverage_incomplete(self) -> None:
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_require_full_adjusted_coverage": True}
        )

        def _stub_samples(**_kwargs: object) -> list[dict]:
            trainer._adjusted_basis_expected = True
            trainer._adjusted_view_state = "present"
            trainer._adjusted_view_market = "CN"
            trainer._label_price_stats = {
                "adjusted_count": 4800,
                "raw_fallback_count": 100,
                "dropped_missing_adjusted_count": 100,
            }
            return self._integration_samples()

        with patch.object(trainer, "_load_symbol_feature_context", return_value={}), patch.object(
            trainer, "_build_lightgbm_samples", side_effect=_stub_samples
        ):
            with self.assertRaisesRegex(RuntimeError, "incomplete coverage"):
                trainer._train_lightgbm(
                    run_name="fixture",
                    signal_type="momentum",
                    lookback_days=3,
                    normalized_tickers=None,
                    market="CN",
                    universe="fixture",
                    rows=[],
                )

    def test_train_lightgbm_records_mixed_basis_when_gate_disabled(self) -> None:
        samples = self._integration_samples()
        model = MagicMock(feature_importances_=[1.0])
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={
                "trainer_cn_window_dates": 10,
                "trainer_require_full_adjusted_coverage": False,
            }
        )

        def _stub_samples(**_kwargs: object) -> list[dict]:
            trainer._adjusted_basis_expected = True
            trainer._adjusted_view_state = "present"
            trainer._adjusted_view_market = "CN"
            trainer._label_price_stats = {
                "adjusted_count": 4800,
                "raw_fallback_count": 100,
                "dropped_missing_adjusted_count": 100,
            }
            return samples

        repositories = (
            "SymbolRepository",
            "ModelRunRepository",
            "PredictionWriteRepository",
            "PredictionDetailRepository",
            "PredictionExplanationRepository",
        )
        with ExitStack() as stack:
            stack.enter_context(patch("app.services.trainer.SessionLocal"))
            mocks = {
                name: stack.enter_context(patch(f"app.services.trainer.{name}")).return_value
                for name in repositories
            }
            mocks["SymbolRepository"].list_symbols.return_value = [
                SimpleNamespace(ticker=f"FIXTURE{i:03}", id=i + 1) for i in range(100)
            ]
            mocks["ModelRunRepository"].create_run.return_value = SimpleNamespace(id=9876)
            stack.enter_context(
                patch("app.services.trainer.get_latest_lake_trade_date", return_value="2026-02-19")
            )
            stack.enter_context(
                patch(
                    "app.services.trainer.lgb",
                    SimpleNamespace(LGBMRegressor=MagicMock(return_value=model)),
                )
            )
            for name, value in {
                "_feature_names": ["fixture_day"],
                "_load_symbol_feature_context": {},
                "_load_oos_score_calibration": ([], {}),
                "_build_score_calibration": [],
                "_build_detail_row": {},
                "_build_lightgbm_explanations": [],
            }.items():
                stack.enter_context(patch.object(trainer, name, return_value=value))
            stack.enter_context(patch.object(trainer, "_build_lightgbm_samples", side_effect=_stub_samples))
            stack.enter_context(
                patch.object(trainer, "_predict_scores", side_effect=lambda _, rows: [0.01] * len(rows))
            )
            persist = stack.enter_context(
                patch.object(trainer, "_persist_model_outputs", return_value=9876)
            )
            run_id = trainer._train_lightgbm(
                run_name="fixture",
                signal_type="momentum",
                lookback_days=3,
                normalized_tickers=None,
                market="CN",
                universe="fixture",
                rows=[],
            )

        self.assertEqual(9876, run_id)
        repo = mocks["ModelRunRepository"]
        config = repo.create_run.call_args.kwargs["config"]
        self.assertTrue(str(config["label_price_basis"]).startswith("mixed:"))
        self.assertEqual(4800, config["adjusted_count"])
        self.assertEqual(100, config["raw_fallback_count"])
        self.assertEqual(100, config["dropped_missing_adjusted_count"])
        self.assertAlmostEqual(0.96, config["adjusted_coverage_share"])
        self.assertFalse(config["require_full_adjusted_coverage"])
        self.assertEqual(TRAINING_WEIGHT_POLICY, config["training_weight_policy"])

        metadata = persist.call_args.kwargs["model_metadata"]
        self.assertEqual(config["label_price_basis"], metadata["label_price_basis"])
        self.assertEqual(config["adjusted_count"], metadata["adjusted_count"])
        self.assertEqual(config["adjusted_coverage_share"], metadata["adjusted_coverage_share"])

    def test_train_lightgbm_records_mixed_zero_when_absent_view_is_explicitly_allowed(self) -> None:
        # Explicit raw opt-in + absent view: the run persists, but it must read
        # `mixed:0.00000000` with `raw_fallback_allowed=true`, never adjusted.
        samples = self._integration_samples()
        model = MagicMock(feature_importances_=[1.0])
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={
                "trainer_cn_window_dates": 10,
                "trainer_allow_raw_fallback": True,
                "optin_reason": "fixture: absent view accepted for raw-label run",
                "optin_operator": "fixture_operator",
            }
        )

        def _stub_samples(**_kwargs: object) -> list[dict]:
            trainer._adjusted_basis_expected = True
            trainer._adjusted_view_state = "absent"
            trainer._adjusted_view_market = "CN"
            trainer._label_price_stats = {
                "adjusted_count": 0,
                "raw_fallback_count": len(samples),
                "dropped_missing_adjusted_count": 0,
            }
            return samples

        repositories = (
            "SymbolRepository",
            "ModelRunRepository",
            "PredictionWriteRepository",
            "PredictionDetailRepository",
            "PredictionExplanationRepository",
        )
        with ExitStack() as stack:
            stack.enter_context(patch("app.services.trainer.SessionLocal"))
            mocks = {
                name: stack.enter_context(patch(f"app.services.trainer.{name}")).return_value
                for name in repositories
            }
            mocks["SymbolRepository"].list_symbols.return_value = [
                SimpleNamespace(ticker=f"FIXTURE{i:03}", id=i + 1) for i in range(100)
            ]
            mocks["ModelRunRepository"].create_run.return_value = SimpleNamespace(id=4321)
            stack.enter_context(
                patch("app.services.trainer.get_latest_lake_trade_date", return_value="2026-02-19")
            )
            stack.enter_context(
                patch(
                    "app.services.trainer.lgb",
                    SimpleNamespace(LGBMRegressor=MagicMock(return_value=model)),
                )
            )
            for name, value in {
                "_feature_names": ["fixture_day"],
                "_load_symbol_feature_context": {},
                "_load_oos_score_calibration": ([], {}),
                "_build_score_calibration": [],
                "_build_detail_row": {},
                "_build_lightgbm_explanations": [],
            }.items():
                stack.enter_context(patch.object(trainer, name, return_value=value))
            stack.enter_context(patch.object(trainer, "_build_lightgbm_samples", side_effect=_stub_samples))
            stack.enter_context(
                patch.object(trainer, "_predict_scores", side_effect=lambda _, rows: [0.01] * len(rows))
            )
            persist = stack.enter_context(
                patch.object(trainer, "_persist_model_outputs", return_value=4321)
            )
            run_id = trainer._train_lightgbm(
                run_name="fixture",
                signal_type="momentum",
                lookback_days=3,
                normalized_tickers=None,
                market="CN",
                universe="fixture",
                rows=[],
            )

        self.assertEqual(4321, run_id)
        config = mocks["ModelRunRepository"].create_run.call_args.kwargs["config"]
        self.assertEqual("mixed:0.00000000", config["label_price_basis"])
        self.assertFalse(config["adjusted_view_present"])
        self.assertEqual("absent", config["adjusted_view_state"])
        self.assertTrue(config["raw_fallback_allowed"])
        self.assertEqual(0, config["adjusted_count"])
        self.assertEqual(len(samples), config["raw_fallback_count"])
        self.assertAlmostEqual(0.0, config["adjusted_coverage_share"])
        # Structured opt-in audit lands on the persisted run config.
        config_audit = config["raw_fallback_optin_audit"]
        self.assertTrue(config_audit["enabled"])
        self.assertEqual("fixture_operator", config_audit["operator"])
        self.assertEqual("fixture: absent view accepted for raw-label run", config_audit["reason"])
        self.assertEqual("PQW_TRAINER_ALLOW_RAW_FALLBACK", config_audit["source"])
        metadata = persist.call_args.kwargs["model_metadata"]
        self.assertEqual("mixed:0.00000000", metadata["label_price_basis"])
        self.assertTrue(metadata["raw_fallback_allowed"])
        self.assertEqual(config_audit, metadata["raw_fallback_optin_audit"])
        # Explicit raw authorization must land in the prediction product audit:
        # entry point, view state and the authorizing switch are recorded.
        inference_contract = metadata["prediction_price_basis_contract"]
        self.assertEqual("inference", inference_contract["entry_point"])
        self.assertEqual("absent", inference_contract["view_state"])
        self.assertEqual("explicit_raw_fallback", inference_contract["fallback_policy"])
        self.assertTrue(inference_contract["applicable"])
        self.assertTrue(inference_contract["authorized_by"])
        self.assertEqual(
            config["prediction_price_basis_contract"], inference_contract
        )
