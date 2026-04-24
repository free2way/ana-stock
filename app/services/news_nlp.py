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

POSITIVE_PHRASES = {
    "上涨",
    "涨停",
    "大涨",
    "走强",
    "新高",
    "突破",
    "增长",
    "盈利",
    "中标",
    "订单",
    "回购",
    "增持",
    "业绩预增",
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

NEGATIVE_PHRASES = {
    "下跌",
    "跌停",
    "大跌",
    "走弱",
    "亏损",
    "减持",
    "立案",
    "调查",
    "处罚",
    "风险",
    "预亏",
    "业绩下滑",
    "终止",
}

TOPIC_KEYWORDS = {
    "earnings": {"earnings", "profit", "revenue", "guidance", "results"},
    "deal": {"merger", "acquisition", "deal", "buyout", "stake"},
    "policy": {"policy", "regulator", "regulatory", "tariff", "government"},
    "product": {"launch", "product", "platform", "model", "chip"},
    "funding": {"funding", "financing", "raise", "bond", "offering"},
}

TOPIC_PHRASES = {
    "earnings": {"业绩", "净利润", "营收", "年报", "季报", "预增", "预亏"},
    "deal": {"并购", "收购", "重组", "中标", "合同", "订单"},
    "policy": {"政策", "监管", "证监会", "交易所"},
    "product": {"产品", "新品", "芯片", "平台", "量产"},
    "funding": {"定增", "融资", "债券", "募资"},
}

RISK_KEYWORDS = {
    "earnings-soon": {"earnings", "guidance"},
    "regulatory-risk": {"regulator", "regulatory", "investigation", "antitrust"},
    "legal-risk": {"lawsuit", "fraud", "litigation"},
    "funding-risk": {"offering", "bond", "default", "financing"},
}

RISK_PHRASES = {
    "earnings-soon": {"年报", "季报", "业绩预告"},
    "regulatory-risk": {"监管", "立案", "调查", "处罚", "问询函"},
    "legal-risk": {"诉讼", "仲裁", "违规"},
    "funding-risk": {"债券", "违约", "融资", "定增"},
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
        lowered_text = text.lower()
        tokens = set(_tokenize(text))
        headlines.append(title)
        positive_hits = len(tokens & POSITIVE_KEYWORDS) + sum(1 for phrase in POSITIVE_PHRASES if phrase in text)
        negative_hits = len(tokens & NEGATIVE_KEYWORDS) + sum(1 for phrase in NEGATIVE_PHRASES if phrase in text)
        if positive_hits > negative_hits:
            positive_count += 1
        elif negative_hits > positive_hits:
            negative_count += 1
        for topic, keywords in TOPIC_KEYWORDS.items():
            if tokens & keywords:
                topic_counter[topic] += 1
        for topic, phrases in TOPIC_PHRASES.items():
            if any(phrase in text for phrase in phrases):
                topic_counter[topic] += 1
        for tag, keywords in RISK_KEYWORDS.items():
            if tokens & keywords:
                risk_tags.add(tag)
        for tag, phrases in RISK_PHRASES.items():
            if any(phrase in text or phrase in lowered_text for phrase in phrases):
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
