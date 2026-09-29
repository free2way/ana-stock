"""Last, model-independent gate before any report or notification is rendered."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import re

from app.services.stock_selection.data_contracts import candidate_eligibility, validate_market_ticker
from app.services.stock_selection.regime_policy import evaluate_regime_policy


_LIST_KEYS = {"CN": "market_recommendations", "US": "us_model_recommendations", "HK": "hk_model_recommendations"}
_META_KEYS = {"CN": "market_recommendations_meta", "US": "us_model_recommendations_meta", "HK": "hk_model_recommendations_meta"}
_WATCH_KEYS = {"CN": "market_watch_recommendations", "US": None, "HK": None}
APPROVAL_REGISTRY_KEY = "stock_selection_model_qualification_registry_v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def load_trusted_model_qualifications(*, db=None) -> dict[str, dict]:
    """Read independently approved model identities, never from AI report text."""
    if db is None:
        from app.core.db import SessionLocal

        with SessionLocal() as own_db:
            return load_trusted_model_qualifications(db=own_db)
    from app.services.repository import AppSettingRepository

    raw = AppSettingRepository(db).get(APPROVAL_REGISTRY_KEY)
    if not isinstance(raw, str):
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    approved = {}
    for market in _LIST_KEYS:
        record = decoded.get(market)
        if not isinstance(record, dict):
            continue
        timestamp = str(record.get("approved_at") or "")
        try:
            approved_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if (
            approved_at.tzinfo is None or approved_at.utcoffset() is None
            or str(record.get("status") or "").upper() not in {"QUALIFIED", "PROMOTED"}
            or record.get("protocol_approved") is not True
            or not str(record.get("approved_by") or "").strip()
            or not str(record.get("protocol_id") or "").strip()
            or not _SHA256_RE.fullmatch(str(record.get("model_artifact_sha256") or ""))
        ):
            continue
        approved[market] = deepcopy(record)
    return approved


def load_trusted_regime_snapshots(*, db=None) -> dict[str, dict]:
    """Live publication only. Historical research must supply its as-of snapshot.

    A newer or stale stored snapshot is not substituted for the report's date:
    the pure guard below rejects the mismatch. Never use candidate/AI fields.
    """
    if db is None:
        from app.core.db import SessionLocal
        with SessionLocal() as own_db:
            return load_trusted_regime_snapshots(db=own_db)
    from app.services.repository import WorkspaceSnapshotRepository
    try:
        stored = WorkspaceSnapshotRepository(db).get_latest_snapshot("market_regime_snapshot:CN") or {}
        payload = stored.get("payload")
        return {"CN": deepcopy(payload)} if isinstance(payload, dict) else {}
    except (ValueError, OSError, RuntimeError):
        # Missing/corrupt cold snapshot is a blocked publication, not an ALLOW.
        return {}


def prepare_report_for_publication(report: dict, *, approved_qualifications: dict | None = None,
                                   trusted_regime_snapshots: dict | None = None) -> dict:
    """Preserve raw research elsewhere, but never publish unqualified stock picks."""
    output = deepcopy(report)
    claimed = output.get("model_qualification") or {}
    trusted = approved_qualifications or {}
    qualification = {}
    if isinstance(claimed, dict) and isinstance(trusted, dict):
        for market in _LIST_KEYS:
            claim = claimed.get(market)
            approval = trusted.get(market)
            if not isinstance(claim, dict) or not isinstance(approval, dict):
                continue
            if (str(claim.get("protocol_id") or "") == str(approval.get("protocol_id") or "")
                and str(claim.get("model_artifact_sha256") or "") == str(approval.get("model_artifact_sha256") or "")):
                qualification[market] = deepcopy(approval)
    output["model_qualification"] = qualification
    blocked: dict[str, list[dict]] = deepcopy(output.get("publication_blocked_candidates") or {})
    for market, list_key in _LIST_KEYS.items():
        meta_key = _META_KEYS[market]
        if list_key not in output and meta_key not in output:
            continue
        if list_key not in output and str((output.get(meta_key) or {}).get("status") or "").lower() == "not_requested":
            continue
        rows = output.setdefault(list_key, [])
        if not isinstance(rows, list):
            raise ValueError(f"{market} recommendation list must be explicit")
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{market} recommendation must be an object")
            ticker = validate_market_ticker(market, row.get("ticker"))
            row["ticker"] = ticker
            if ticker in seen:
                raise ValueError(f"duplicate {market} recommendation ticker: {ticker}")
            seen.add(ticker)
        gate = qualification.get(market) if isinstance(qualification, dict) else None
        gate = gate if isinstance(gate, dict) else {}
        configured_top_n = gate.get("top_n", 5)
        if type(configured_top_n) is not int or configured_top_n <= 0:
            raise ValueError("publication top_n must be positive")
        top_n = min(5, configured_top_n)
        frozen_rows = rows[:top_n]
        qualified = (
            str(gate.get("status") or "").upper() in {"QUALIFIED", "PROMOTED"}
            and gate.get("protocol_approved") is True
            and bool(str(gate.get("protocol_id") or "").strip())
        )
        meta = deepcopy(output.get(meta_key) or {})
        watch_key = _WATCH_KEYS[market]
        watch_rows = output.get(watch_key) if watch_key else []
        has_research_observations = isinstance(watch_rows, list) and bool(watch_rows)
        if market == "CN":
            input_date = str((output.get("input_market_dates") or {}).get("CN") or
                             meta.get("target_snapshot_date") or "")
            source = (trusted_regime_snapshots or {}).get("CN")
            if meta.get("target_snapshot_date") and meta["target_snapshot_date"] != input_date:
                source = None  # Conflicting lineage cannot be resolved by guessing.
            policy = evaluate_regime_policy(source, market="CN", expected_market_date=input_date,
                decision_cutoff_at=output.get("decision_cutoff_at") or output.get("saved_at") or "",
                max_new_candidates=top_n)
            meta["regime_policy"] = policy
            meta["regime_position_hint"] = policy["regime_position_hint"]
            output[meta_key] = meta
            if qualified and policy["buy_gate"] == "BLOCK":
                if rows:
                    blocked[market] = [{**row, "publication_block_reason": "market_regime_blocked"} for row in rows]
                output[list_key] = []
                meta["status"] = "observation_ready" if has_research_observations else "not_ready"
                meta["publication_blocker"] = "market_regime_blocked"
                meta["note"] = "市场体制或其数据证据未通过校验，暂停新增买入名单；" + policy["regime_position_hint"]
                meta["blocked_candidate_count"] = len(blocked.get(market, []))
                continue
        if not rows:
            if not qualified and str(meta.get("status") or "").lower() != "not_requested":
                meta["status"] = "observation_ready" if has_research_observations else "not_ready"
                meta["note"] = str(meta.get("note") or (
                    "全市场扫描和研究候选已就绪；模型尚未按冻结协议通过资格验收，今日不发布可执行股票名单。"
                    if has_research_observations
                    else "模型尚未按冻结协议通过资格验收；今日不发布可执行股票名单。"
                ))
                meta["qualification_blocker"] = "model_not_qualified"
                output[meta_key] = meta
            continue
        if not qualified:
            blocked[market] = rows
            output[list_key] = []
            meta["status"] = "observation_ready" if has_research_observations else "not_ready"
            meta["note"] = "模型尚未按冻结协议通过资格验收；原始名单仅供研究，不构成可执行买入建议。"
            meta["qualification_blocker"] = "model_not_qualified"
            output[meta_key] = meta
            continue
        ready: list[dict] = []
        for row in frozen_rows:
            status = row.get("symbol_status") or row.get("tradability_status") or "unknown"
            result = candidate_eligibility(
                market=market, ticker=row["ticker"],
                market_health=str(meta.get("market_health") or meta.get("status") or "unknown"),
                symbol_status=str(status), source_status=str(row.get("source_status") or "ready"),
            )
            if result["eligible"]:
                ready.append(row)
            else:
                blocked.setdefault(market, []).append({**row, "publication_block_reason": result["reason"]})
        if len(rows) > top_n:
            blocked.setdefault(market, []).extend(
                {**row, "publication_block_reason": "outside_frozen_top_n"}
                for row in rows[top_n:]
            )
        output[list_key] = ready
        if blocked.get(market):
            meta["blocked_candidate_count"] = len(blocked[market])
            meta["blocked_candidate_reason"] = "security_level_gate"
            output[meta_key] = meta
    if blocked:
        output["publication_blocked_candidates"] = blocked
    return output


__all__ = ["prepare_report_for_publication", "load_trusted_model_qualifications", "load_trusted_regime_snapshots", "APPROVAL_REGISTRY_KEY"]
