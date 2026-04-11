from __future__ import annotations


def build_symbol_decision_brief(
    *,
    ticker: str,
    combined_analysis: dict | None,
    latest_signal: dict | None = None,
) -> dict:
    payload = combined_analysis or {}
    score = int(payload.get("score") or 0)
    decision = str(payload.get("decision") or "HOLD").upper()
    confidence = int(payload.get("confidence") or 50)
    reasons = list(payload.get("reasons") or [])
    model_score = latest_signal.get("score") if latest_signal else None

    sentiment = "neutral"
    urgency = "normal"
    if decision in {"STRONG BUY", "BUY"}:
        sentiment = "bullish"
    elif decision in {"STRONG SELL", "SELL"}:
        sentiment = "bearish"

    if confidence >= 78 and abs(score) >= 4:
        urgency = "high"
    elif confidence <= 55:
        urgency = "low"

    headline = f"{ticker} is in a {sentiment} decision state"
    if decision == "STRONG BUY":
        headline = f"{ticker} shows strong bullish confluence"
    elif decision == "BUY":
        headline = f"{ticker} remains constructive with bullish support"
    elif decision == "SELL":
        headline = f"{ticker} is weakening and needs caution"
    elif decision == "STRONG SELL":
        headline = f"{ticker} is in a decisive bearish state"

    summary_parts = []
    if reasons:
        summary_parts.append(", ".join(reasons[:3]))
    if model_score is not None:
        summary_parts.append(f"latest model score {float(model_score):.3f}")
    if not summary_parts:
        summary_parts.append("No strong confluence signal yet")

    return {
        "ticker": ticker,
        "status": "success",
        "sentiment": sentiment,
        "urgency": urgency,
        "headline": headline,
        "summary": " | ".join(summary_parts),
        "decision": decision,
        "confidence": confidence,
    }


def build_market_sentiment_snapshot(*, boards: list[dict]) -> dict:
    total_rows = sum(len(board.get("rows") or []) for board in boards)
    bullish_boards = 0
    average_score = 0.0
    scored_rows = 0
    board_summaries: list[dict] = []

    for board in boards:
        rows = board.get("rows") or []
        scores = [float(item.get("snapshot_score") or 0.0) for item in rows if item.get("snapshot_score") is not None]
        avg = sum(scores) / len(scores) if scores else 0.0
        if avg >= 65:
            bullish_boards += 1
        average_score += sum(scores)
        scored_rows += len(scores)
        board_summaries.append(
            {
                "key": board.get("key"),
                "title_en": board.get("title_en"),
                "title_zh": board.get("title_zh"),
                "count": len(rows),
                "average_score": round(avg, 1),
                "top_ticker": (rows[0].get("ticker") if rows else None),
            }
        )

    mean_score = average_score / scored_rows if scored_rows else 0.0
    sentiment = "neutral"
    if bullish_boards >= 3 or mean_score >= 72:
        sentiment = "risk_on"
    elif bullish_boards <= 1 and mean_score <= 52:
        sentiment = "risk_off"

    return {
        "status": "success",
        "sentiment": sentiment,
        "total_candidates": total_rows,
        "average_snapshot_score": round(mean_score, 1),
        "bullish_boards": bullish_boards,
        "boards": board_summaries,
    }


def build_symbol_news_sentiment_brief(
    *,
    ticker: str,
    decision_brief: dict | None,
    combined_analysis: dict | None,
) -> dict:
    decision_payload = decision_brief or {}
    combined = combined_analysis or {}
    reasons = list(combined.get("reasons") or [])
    sentiment = str(decision_payload.get("sentiment") or "neutral")
    urgency = str(decision_payload.get("urgency") or "normal")
    decision = str(combined.get("decision") or "HOLD").upper()

    headlines: list[str] = []
    if decision in {"BUY", "STRONG BUY"}:
        headlines.append(f"{ticker} trade setup remains constructive")
    elif decision in {"SELL", "STRONG SELL"}:
        headlines.append(f"{ticker} setup is losing momentum")
    else:
        headlines.append(f"{ticker} is trading in a mixed state")

    if reasons:
        headlines.append(f"Drivers: {', '.join(reasons[:2])}")
    if urgency == "high":
        headlines.append("Urgency is elevated, worth review in the current session")
    elif urgency == "low":
        headlines.append("No urgent catalyst from the current local signal stack")

    return {
        "ticker": ticker,
        "status": "success",
        "sentiment": sentiment,
        "urgency": urgency,
        "headlines": headlines,
        "summary": decision_payload.get("summary") or "No narrative summary available.",
    }


def build_market_narrative_brief(
    *,
    latest_signals: list[dict],
    focus_items: list[dict],
    risk_overview: dict | None,
    snapshot_lines: list[str],
) -> dict:
    risks = risk_overview or {}
    top_tags = [str(item.get("tag")) for item in (risks.get("top_tags") or []) if item.get("tag")]
    signal_tickers = [str(item.get("ticker")) for item in latest_signals[:3] if item.get("ticker")]
    focus_tickers = [str(item.get("ticker")) for item in focus_items[:3] if item.get("ticker")]

    mood = "balanced"
    if len(snapshot_lines) >= 2 and len(focus_tickers) >= 2:
        mood = "constructive"
    if top_tags:
        mood = "cautious" if mood == "balanced" else "constructive but selective"

    bullets: list[str] = []
    if signal_tickers:
        bullets.append(f"Recent model leaders: {', '.join(signal_tickers)}")
    if focus_tickers:
        bullets.append(f"Focus pool names: {', '.join(focus_tickers)}")
    if snapshot_lines:
        bullets.append(f"Snapshot leaders: {'; '.join(snapshot_lines[:2])}")
    if top_tags:
        bullets.append(f"Execution risks showing up: {', '.join(top_tags[:2])}")

    headline = "Local market tone is balanced"
    if mood == "constructive":
        headline = "Local market tone is constructive"
    elif mood == "constructive but selective":
        headline = "Local market tone is constructive but selective"
    elif mood == "cautious":
        headline = "Local market tone is cautious"

    return {
        "status": "success",
        "mood": mood,
        "headline": headline,
        "bullets": bullets,
    }
