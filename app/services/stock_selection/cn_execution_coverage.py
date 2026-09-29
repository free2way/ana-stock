"""Conservative evidence producer: absent daily facts remain unknown.

Price-limit values are source facts, not inferred from a current name/ST flag.
This produces research label eligibility, never certifies real order fills.
"""
from collections import Counter
from datetime import date
import math

from app.services.stock_selection.execution_evidence import ResearchExecutionEvidence, price_path_hash
from app.services.stock_selection.executable_outcomes import ExecutionEligibility
from app.services.stock_selection.production_data import normalize_price_rows


def _positive(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def assess_cn_execution_coverage(rows: list[dict], *, trading_dates: list[date],
                                  tickers: list[str], horizon_days: int, source_reference: str) -> dict:
    if horizon_days < 2:
        raise ValueError("CN evidence requires at least two holding sessions")
    if not tickers or len(set(tickers)) != len(tickers):
        raise ValueError("explicit unique ticker list required")
    if any(not ticker.endswith((".SS", ".SZ")) for ticker in tickers):
        raise ValueError("CN audit covers Shanghai/Shenzhen only; BJ excluded")
    selected = set(tickers)
    relevant = [row for row in rows if row.get("symbol") in selected]
    bars, invalid_rows = normalize_price_rows(relevant)
    # Reuse the strict calendar contract, without pretending OHLCV is verified raw.
    calendar_check = ResearchExecutionEvidence("CN", tuple(trading_dates), price_path_hash(bars), source_reference, {})
    if any(bar.trade_date not in calendar_check.trading_dates for path in bars.values() for bar in path):
        raise ValueError("price row outside requested market calendar")
    raw = {}
    for row in relevant:
        key = (row["symbol"], date.fromisoformat(str(row["date"])))
        if key in raw:
            raise ValueError("duplicate source price row")
        raw[key] = row
    valid = {(ticker, bar.trade_date): bar for ticker, path in bars.items() for bar in path}
    records, details = {}, []
    reason_counts, status_counts = Counter(), Counter()
    for ticker in sorted(tickers):
        for index, signal_date in enumerate(trading_dates):
            if index + horizon_days >= len(trading_dates):
                status_counts["immature"] += 1
                continue
            dates = trading_dates[index:index + horizon_days + 1]
            reasons = []
            entry = exit_allowed = None
            if any((ticker, day) not in raw for day in dates):
                reasons.append("missing_price_path")
            elif any((ticker, day) not in valid for day in dates):
                reasons.append("invalid_price_path")
            else:
                path = [raw[(ticker, day)] for day in dates]
                entry_row, exit_row = path[1], path[-1]
                entry_bar, exit_bar = valid[(ticker, dates[1])], valid[(ticker, dates[-1])]
                if any(row.get("price_basis") != "raw" for row in path):
                    reasons.append("raw_price_basis_unverified")
                if any(not str(row.get("execution_source_reference") or "").strip() for row in path):
                    reasons.append("daily_execution_provenance_missing")
                actions = [row.get("corporate_action_status") for row in path]
                if any(value not in {"none", "action"} for value in actions):
                    reasons.append("corporate_action_state_unknown")
                if "action" in actions:
                    reasons.append("corporate_action_requires_account_replay")
                upper = _positive(entry_row.get("upper_limit"))
                lower = _positive(exit_row.get("lower_limit"))
                if upper is None or lower is None:
                    reasons.append("daily_price_limits_unknown")
                if type(entry_row.get("suspended")) is not bool or type(exit_row.get("suspended")) is not bool:
                    reasons.append("daily_suspension_state_unknown")
                prerequisites = not reasons
                entry = True if prerequisites else None
                exit_allowed = True if prerequisites else None
                if entry_bar.volume <= 0 or entry_row.get("suspended") is True:
                    entry = False
                    reasons.append("entry_suspended_or_no_volume")
                if exit_bar.volume <= 0 or exit_row.get("suspended") is True:
                    exit_allowed = False
                    reasons.append("exit_suspended_or_no_volume")
                # Only apply supplied limit prices after raw-basis confirmation.
                if all(row.get("price_basis") == "raw" for row in path):
                    if upper is not None and entry_bar.open >= upper * (1 - 1e-4):
                        entry = False
                        reasons.append("entry_at_upper_limit")
                    one_price = abs(exit_bar.high - exit_bar.low) <= max(1e-8, abs(exit_bar.close) * 1e-8)
                    if lower is not None and one_price and exit_bar.close <= lower * (1 + 1e-4):
                        exit_allowed = False
                        reasons.append("exit_at_one_price_lower_limit")
                if "action" in actions:
                    entry = exit_allowed = False
            status = "blocked" if entry is False or exit_allowed is False else "eligible" if entry is True and exit_allowed is True else "unknown"
            unique_reasons = sorted(set(reasons))
            status_counts[status] += 1
            reason_counts.update(unique_reasons)
            records[(ticker, signal_date, horizon_days)] = ExecutionEligibility(entry, exit_allowed,
                ";".join(unique_reasons) if unique_reasons else None)
            details.append({"ticker": ticker, "signal_date": signal_date.isoformat(),
                            "horizon_days": horizon_days, "status": status,
                            "entry_allowed": entry, "exit_allowed": exit_allowed, "reasons": unique_reasons})
    # Do not emit a training file claiming raw OHLCV when provenance cannot support it.
    raw_verified = bool(valid) and all(raw[key].get("price_basis") == "raw"
        and bool(str(raw[key].get("execution_source_reference") or "").strip()) for key in valid)
    evidence = ResearchExecutionEvidence("CN", tuple(trading_dates), price_path_hash(bars), source_reference, records) if raw_verified else None
    matured = sum(status_counts[key] for key in ("eligible", "blocked", "unknown"))
    return {
        "schema_version": "cn_execution_coverage_assessment_v1",
        "status": "READY_FOR_RESEARCH_REVIEW" if status_counts["eligible"] else "BLOCKED",
        "model_backtest_status": "NOT_RUN", "market": "CN", "bj_excluded": True,
        "source_reference": source_reference, "ticker_count": len(tickers),
        "calendar_start": trading_dates[0].isoformat(), "calendar_end": trading_dates[-1].isoformat(),
        "calendar_sessions": len(trading_dates), "horizon_days": horizon_days,
        "raw_row_count": len(relevant), "valid_price_row_count": len(valid), "invalid_price_row_count": invalid_rows,
        "matured_decision_count": matured, "status_counts": dict(status_counts),
        "eligible_coverage_ratio": status_counts["eligible"] / matured if matured else 0,
        "reason_counts": dict(reason_counts), "reason_counts_overlap": True,
        "price_sha256": price_path_hash(bars),
        "training_evidence": evidence.payload() if evidence else None,
        "evidence_export_blocker": None if evidence else "raw_price_basis_or_provenance_unverified",
        "decisions": details,
    }
