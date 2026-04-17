from __future__ import annotations

from collections import Counter


POSITIVE_KEYWORDS = {
    "beat",
    "beats",
    "breakout",
    "bullish",
    "growth",
    "upgrade",
    "surge",
    "strong",
    "record",
    "profit",
    "rebound",
    "expansion",
    "improves",
    "improved",
    "wins",
    "win",
}

NEGATIVE_KEYWORDS = {
    "downgrade",
    "warning",
    "lawsuit",
    "fraud",
    "loss",
    "weak",
    "decline",
    "fall",
    "drop",
    "slump",
    "miss",
    "misses",
    "bearish",
    "investigation",
    "default",
    "risk",
    "cuts",
    "cut",
}

TOPIC_KEYWORDS = {
    "earnings": {"earnings", "profit", "revenue", "guidance", "results"},
    "deal": {"merger", "acquisition", "deal", "buyout", "stake"},
    "policy": {"policy", "regulator", "regulatory", "tariff", "government"},
    "product": {"launch", "product", "platform", "model", "chip"},
    "funding": {"funding", "financing", "raise", "bond", "offering"},
}

RISK_KEYWORDS = {
    "earnings-soon": {"earnings", "guidance"},
    "regulatory-risk": {"regulator", "regulatory", "investigation", "antitrust"},
    "legal-risk": {"lawsuit", "fraud", "litigation"},
    "funding-risk": {"offering", "bond", "default", "financing"},
}


def _tokenize(text: str) -> list[str]:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in str(text or ""))
    return [part for part in cleaned.split() if part]


def analyze_news_articles(articles: list[dict]) -> dict:
    if not articles:
        return {
            "sentiment_label": "neutral",
            "sentiment_score": 0.0,
            "topic_label": "none",
            "risk_tags": [],
            "entities": [],
            "summary_text": "",
            "headline_count": 0,
            "positive_count": 0,
            "negative_count": 0,
            "headlines": [],
        }

    positive_count = 0
    negative_count = 0
    topic_counter: Counter[str] = Counter()
    risk_tags: set[str] = set()
    entity_counter: Counter[str] = Counter()
    headlines: list[str] = []

    for article in articles:
        title = str(article.get("title") or "").strip()
        summary = str(article.get("summary") or "").strip()
        text = f"{title} {summary}"
        tokens = set(_tokenize(text))
        headlines.append(title)
        positive_hits = len(tokens & POSITIVE_KEYWORDS)
        negative_hits = len(tokens & NEGATIVE_KEYWORDS)
        if positive_hits > negative_hits:
            positive_count += 1
        elif negative_hits > positive_hits:
            negative_count += 1
        for topic, keywords in TOPIC_KEYWORDS.items():
            if tokens & keywords:
                topic_counter[topic] += 1
        for tag, keywords in RISK_KEYWORDS.items():
            if tokens & keywords:
                risk_tags.add(tag)
        for raw in title.split():
            token = raw.strip(".,:;()[]{}")
            if len(token) >= 2 and token[:1].isupper():
                entity_counter[token] += 1

    score = (positive_count - negative_count) / max(1, len(articles))
    if score >= 0.25:
        sentiment_label = "positive"
    elif score <= -0.25:
        sentiment_label = "negative"
    else:
        sentiment_label = "neutral"

    topic_label = topic_counter.most_common(1)[0][0] if topic_counter else "none"
    summary_lead = "；".join(item for item in headlines[:3] if item)
    return {
        "sentiment_label": sentiment_label,
        "sentiment_score": round(score, 3),
        "topic_label": topic_label,
        "risk_tags": sorted(risk_tags),
        "entities": [token for token, _ in entity_counter.most_common(6)],
        "summary_text": summary_lead,
        "headline_count": len(articles),
        "positive_count": positive_count,
        "negative_count": negative_count,
        "headlines": headlines[:5],
    }
