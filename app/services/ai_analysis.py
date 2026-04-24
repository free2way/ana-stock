from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.config import get_settings
from app.services.insight_engine import InsightEngine
from app.services.market_intelligence import build_symbol_decision_brief, build_symbol_news_sentiment_brief
from app.services.market_news import MarketNewsService
from app.services.runtime_cache import get_or_set


class AIAnalysisService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.insight_engine = InsightEngine()

    def analyze_symbol(
        self,
        *,
        overview: dict,
        latest_signal: dict | None,
        combined_analysis: dict,
        lang: str = "zh",
    ) -> dict:
        normalized_lang = "zh" if str(lang or "").lower().startswith("zh") else "en"
        cache_key = json.dumps(
            {
                "ticker": overview.get("ticker"),
                "market": overview.get("market"),
                "exchange": overview.get("exchange"),
                "lang": normalized_lang,
                "signal": {
                    "trade_date": (latest_signal or {}).get("trade_date"),
                    "score": (latest_signal or {}).get("score"),
                    "rank_value": (latest_signal or {}).get("rank_value"),
                },
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return get_or_set("ai_symbol_analysis", cache_key, ttl_seconds=300.0, loader=lambda: self._load_analysis(
            overview=overview,
            latest_signal=latest_signal,
            combined_analysis=combined_analysis,
            lang=normalized_lang,
        ))

    def _load_analysis(
        self,
        *,
        overview: dict,
        latest_signal: dict | None,
        combined_analysis: dict,
        lang: str,
    ) -> dict:
        insight = self.insight_engine.get_insight(overview["ticker"], lang=lang)
        decision_brief = build_symbol_decision_brief(
            ticker=overview["ticker"],
            combined_analysis=combined_analysis,
            latest_signal=latest_signal,
        )
        news_brief = build_symbol_news_sentiment_brief(
            ticker=overview["ticker"],
            decision_brief=decision_brief,
            combined_analysis=combined_analysis,
        )
        try:
            news_items = MarketNewsService().fetch_symbol_headlines(
                ticker=overview["ticker"],
                name=overview.get("name"),
                market=overview.get("market"),
                limit=3,
            )
        except Exception:
            news_items = []

        context = {
            "overview": overview,
            "latest_signal": latest_signal,
            "combined_analysis": combined_analysis,
            "decision_brief": decision_brief,
            "news_brief": news_brief,
            "insight": insight,
            "headlines": [
                {"source": item.get("source"), "title": item.get("title"), "published_at": item.get("published_at")}
                for item in news_items
            ],
        }
        if self.settings.ai_api_key and self.settings.ai_model:
            llm_payload = self._call_llm(context=context, lang=lang)
            if llm_payload is not None:
                llm_payload.setdefault("ticker", overview["ticker"])
                llm_payload.setdefault("status", "success")
                llm_payload.setdefault("source", "llm")
                llm_payload.setdefault("provider", self.settings.ai_provider_name)
                return llm_payload
        return self._build_local_analysis(
            overview=overview,
            latest_signal=latest_signal,
            combined_analysis=combined_analysis,
            decision_brief=decision_brief,
            news_brief=news_brief,
            insight=insight,
            news_items=news_items,
            lang=lang,
        )

    def _call_llm(self, *, context: dict[str, Any], lang: str) -> dict | None:
        prompt = self._build_prompt(context=context, lang=lang)
        base_url = self.settings.ai_base_url.rstrip("/")
        url = f"{base_url}/chat/completions"
        payload = {
            "model": self.settings.ai_model,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a disciplined stock analyst. Return strict JSON with keys: "
                        "headline, summary, verdict, confidence, strategy, buy_zone, stop_loss, take_profit, checklist, risks."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.settings.ai_api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = httpx.post(url, headers=headers, json=payload, timeout=self.settings.ai_timeout_seconds)
            response.raise_for_status()
            body = response.json()
            content = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
            return self._parse_json_payload(content)
        except Exception:
            return None

    def _parse_json_payload(self, content: str) -> dict | None:
        raw = str(content or "").strip()
        if not raw:
            return None
        for candidate in (raw, self._extract_json_block(raw)):
            if not candidate:
                continue
            try:
                payload = json.loads(candidate)
            except Exception:
                continue
            if isinstance(payload, dict):
                return payload
        return None

    def _extract_json_block(self, content: str) -> str | None:
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        return content[start : end + 1]

    def _build_prompt(self, *, context: dict[str, Any], lang: str) -> str:
        instruction = (
            "请基于以下结构化股票数据，输出中文 JSON。要像交易决策仪表盘，给结论、操作计划、买点区、止损位、止盈区、检查清单和风险。"
            if lang == "zh"
            else "Use the stock context below and return JSON for a trader-ready decision dashboard."
        )
        return f"{instruction}\n\n{json.dumps(context, ensure_ascii=False, indent=2)}"

    def _build_local_analysis(
        self,
        *,
        overview: dict,
        latest_signal: dict | None,
        combined_analysis: dict,
        decision_brief: dict,
        news_brief: dict,
        insight: dict | None,
        news_items: list[dict],
        lang: str,
    ) -> dict:
        decision = str(combined_analysis.get("decision") or "HOLD").upper()
        confidence = int(combined_analysis.get("confidence") or 50)
        score = int(combined_analysis.get("score") or 0)
        reasons = list(combined_analysis.get("reasons") or [])
        buy_zone = (insight or {}).get("entry_zone") or {}
        take_profit_zone = (insight or {}).get("take_profit_zone") or {}
        stop_loss = (insight or {}).get("risk_level")
        strategy = self._local_strategy_label(insight=insight, latest_signal=latest_signal, lang=lang)
        headline = decision_brief.get("headline") or (
            f"{overview['ticker']} 当前偏多" if lang == "zh" else f"{overview['ticker']} remains constructive"
        )
        summary_parts = [decision_brief.get("summary") or ""]
        if reasons:
            summary_parts.append(" / ".join(reasons[:3]))
        if news_items:
            summary_parts.append(news_items[0].get("title") or "")
        summary = " | ".join(part for part in summary_parts if part) or (
            "当前没有足够信号形成高把握结论。" if lang == "zh" else "There is not enough confluence for a high-conviction conclusion."
        )
        checklist = self._build_checklist(combined_analysis=combined_analysis, insight=insight, lang=lang)
        risks = self._build_risks(combined_analysis=combined_analysis, insight=insight, news_brief=news_brief, lang=lang)
        return {
            "ticker": overview["ticker"],
            "status": "success",
            "source": "local",
            "provider": "Local Analysis Stack",
            "headline": headline,
            "summary": summary,
            "verdict": decision,
            "confidence": confidence,
            "strategy": strategy,
            "buy_zone": buy_zone,
            "stop_loss": stop_loss,
            "take_profit": take_profit_zone,
            "checklist": checklist,
            "risks": risks,
            "score": score,
        }

    def _local_strategy_label(self, *, insight: dict | None, latest_signal: dict | None, lang: str) -> str:
        regime = (latest_signal or {}).get("regime_label")
        if regime:
            return str(regime)
        trend = (insight or {}).get("trend_label")
        if trend == "bullish":
            return "进攻/顺势跟踪" if lang == "zh" else "Risk-on trend following"
        if trend == "bearish":
            return "防守/等待确认" if lang == "zh" else "Defensive wait-and-see"
        return "均衡/观察" if lang == "zh" else "Balanced monitor"

    def _build_checklist(self, *, combined_analysis: dict, insight: dict | None, lang: str) -> list[dict]:
        technical = (combined_analysis.get("technical_rating") or {}).get("recommendation")
        alignment = (combined_analysis.get("multi_timeframe") or {}).get("alignment")
        volume_ratio = (insight or {}).get("volume_ratio")
        items = [
            {
                "label": "日线技术评级偏多" if lang == "zh" else "Daily technical rating is constructive",
                "status": "pass" if str(technical or "").upper() in {"BUY", "STRONG_BUY"} else "watch",
            },
            {
                "label": "多周期方向一致" if lang == "zh" else "Multi-timeframe alignment is supportive",
                "status": "pass" if str(alignment or "").lower() in {"bullish_alignment", "bullish_bias"} else "watch",
            },
            {
                "label": "量能没有明显掉队" if lang == "zh" else "Volume is not lagging",
                "status": "pass" if volume_ratio is not None and float(volume_ratio) >= 0.9 else "watch",
            },
        ]
        return items

    def _build_risks(self, *, combined_analysis: dict, insight: dict | None, news_brief: dict, lang: str) -> list[str]:
        risks: list[str] = []
        if str((combined_analysis.get("decision") or "")).upper() in {"SELL", "STRONG SELL"}:
            risks.append("当前综合结论偏谨慎，不能把弱势当成抄底理由。" if lang == "zh" else "The combined stack is defensive, so avoid forcing a bottom call.")
        if (insight or {}).get("distance_to_breakout_pct") is not None and float((insight or {}).get("distance_to_breakout_pct") or 0.0) > 4:
            risks.append("距离有效突破位还有一定空间，追价效率不高。" if lang == "zh" else "Price is still some distance from a clean breakout trigger.")
        if str(news_brief.get("urgency") or "") == "high":
            risks.append("当前信号时效性更强，盘中波动可能放大。" if lang == "zh" else "Signal urgency is elevated, so intraday volatility may expand.")
        if not risks:
            risks.append("当前没有明显的结构性风险放大信号，但仍需结合仓位管理。" if lang == "zh" else "No amplified structural risk is visible, but sizing discipline still matters.")
        return risks
