from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.ai_daily_report import load_ai_daily_report
from app.services.portfolio_book import load_portfolio_positions
from app.services.repository import AppSettingRepository, WatchlistRepository


AI_CHAT_CONFIG_KEY = "ai_chat_config"


@dataclass(frozen=True)
class AIChatConfig:
    provider: str
    provider_name: str
    base_url: str
    model: str
    api_key: str | None
    temperature: float = 0.2
    timeout_seconds: float = 30.0

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)


AI_CHAT_PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1-mini",
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "qwen": {
        "label": "通义千问 / DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-2.5-flash",
        "model_hint": "gemini-2.5-flash / gemini-2.5-pro / gemini-1.5-flash / gemini-1.5-pro",
    },
    "moonshot": {
        "label": "Moonshot / Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
    },
    "compatible": {
        "label": "OpenAI Compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1-mini",
    },
}


AI_CHAT_PROMPT_TEMPLATES: list[dict[str, str]] = [
    {
        "key": "tomorrow_plan",
        "title_zh": "明日操盘计划",
        "title_en": "Next-session Trade Plan",
        "prompt_zh": "请基于当前持仓、自选股、AI日报候选和市场环境，给我一份明日操盘计划：先处理哪些持仓，哪些股票只观察，哪些股票满足什么条件才可以买。请输出买入触发、放弃条件、仓位建议和风险点。",
        "prompt_en": "Based on current holdings, watchlist, AI report candidates, and market context, create a next-session trade plan with priority actions, watch-only names, buy triggers, invalidation rules, sizing, and risks.",
    },
    {
        "key": "ticker_diagnosis",
        "title_zh": "单票交易诊断",
        "title_en": "Single-stock Diagnosis",
        "prompt_zh": "请分析这只股票是否适合明天买入或继续持有：输入股票代码后，请从趋势、量能、买点位置、风险标签、止损位、催化因素和是否追高几个角度回答。",
        "prompt_en": "Analyze whether this stock is suitable to buy tomorrow or continue holding. Cover trend, volume, entry location, risk tags, stop, catalysts, and chase risk.",
    },
    {
        "key": "model_selection",
        "title_zh": "模型筛选策略",
        "title_en": "Model Selection Strategy",
        "prompt_zh": "请根据最近模型评测和AI日报候选，告诉我今天应该优先使用哪些模型或模型组合选股。请解释为什么，适合什么市场环境，以及过滤条件应该怎么设。",
        "prompt_en": "Using recent model evaluation and AI report candidates, tell me which models or model combinations to prioritize today, why, suitable regimes, and recommended filters.",
    },
    {
        "key": "risk_review",
        "title_zh": "持仓风险复核",
        "title_en": "Portfolio Risk Review",
        "prompt_zh": "请作为风控教练检查我的持仓：哪些需要减仓、哪些需要观察、哪些可以继续持有。请重点看集中度、亏损扩大、追高风险、消息风险和仓位纪律。",
        "prompt_en": "Act as a risk coach and review my portfolio: which names need trimming, which need monitoring, and which can be held. Focus on concentration, drawdown, chase risk, news risk, and sizing discipline.",
    },
    {
        "key": "post_trade_review",
        "title_zh": "复盘与命中率改进",
        "title_en": "Post-trade Review",
        "prompt_zh": "请复盘最近的选股结果，帮我找出命中率不高的原因：是模型选择、买点、仓位、止损、追高、还是市场环境问题？请给出下一轮可执行改进。",
        "prompt_en": "Review recent stock-picking outcomes and identify why hit rate was weak: model choice, entry, sizing, stop, chase behavior, or market regime. Give actionable improvements.",
    },
    {
        "key": "news_impact",
        "title_zh": "新闻影响判断",
        "title_en": "News Impact Check",
        "prompt_zh": "请判断当前新闻或社交信号对相关股票是短线催化、长期逻辑、还是噪音。请不要直接照单推荐，要结合价格、模型和风险条件验证。",
        "prompt_en": "Classify current news/social signals as short-term catalyst, long-term thesis, or noise. Do not recommend blindly; validate against price, models, and risk conditions.",
    },
]


def normalize_ai_chat_model(provider: str, model: str) -> str:
    normalized_provider = str(provider or "").strip().lower()
    raw_model = str(model or "").strip()
    if normalized_provider != "gemini":
        return raw_model
    model_key = raw_model.lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "gemini-flash": "gemini-2.5-flash",
        "gemini-pro": "gemini-2.5-pro",
        "gemini-2.5-flash": "gemini-2.5-flash",
        "gemini-2.5-pro": "gemini-2.5-pro",
        "gemini-2.5-flash-lite": "gemini-2.5-flash-lite",
        "gemini-1.5-flash": "gemini-1.5-flash",
        "gemini-1.5-pro": "gemini-1.5-pro",
    }
    if model_key in aliases:
        return aliases[model_key]
    if model_key.startswith("gemini-") and " " not in raw_model:
        return model_key
    return AI_CHAT_PROVIDER_PRESETS["gemini"]["model"]


def load_ai_chat_config(db: Session) -> AIChatConfig:
    settings = get_settings()
    raw = AppSettingRepository(db).get(AI_CHAT_CONFIG_KEY)
    payload: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            payload = {}
    provider = str(payload.get("provider") or "compatible").strip().lower()
    preset = AI_CHAT_PROVIDER_PRESETS.get(provider, AI_CHAT_PROVIDER_PRESETS["compatible"])
    provider_name = str(payload.get("provider_name") or preset["label"] or settings.ai_provider_name).strip()
    model = normalize_ai_chat_model(provider, str(payload.get("model") or settings.ai_model or preset["model"]).strip())
    base_url = (
        str(payload.get("base_url") or "").strip()
        if "base_url" in payload
        else str(settings.ai_base_url or preset["base_url"]).strip()
    )
    return AIChatConfig(
        provider=provider,
        provider_name=provider_name,
        base_url=base_url,
        model=model,
        api_key=str(payload.get("api_key") or settings.ai_api_key or "").strip() or None,
        temperature=float(payload.get("temperature") or 0.2),
        timeout_seconds=float(payload.get("timeout_seconds") or settings.ai_timeout_seconds or 30.0),
    )


def save_ai_chat_config(
    db: Session,
    *,
    provider: str,
    provider_name: str,
    base_url: str,
    model: str,
    api_key: str | None,
    temperature: float = 0.2,
    timeout_seconds: float = 30.0,
    keep_existing_key: bool = True,
) -> AIChatConfig:
    current = load_ai_chat_config(db)
    normalized_provider = str(provider or "compatible").strip().lower()
    preset = AI_CHAT_PROVIDER_PRESETS.get(normalized_provider, AI_CHAT_PROVIDER_PRESETS["compatible"])
    normalized_timeout = float(timeout_seconds or 30.0)
    if normalized_provider == "gemini" and normalized_timeout < 60.0:
        normalized_timeout = 60.0
    next_key = str(api_key or "").strip()
    if not next_key and keep_existing_key:
        next_key = current.api_key or ""
    payload = {
        "provider": normalized_provider,
        "provider_name": str(provider_name or preset["label"]).strip(),
        "base_url": str(base_url or "").strip(),
        "model": normalize_ai_chat_model(normalized_provider, str(model or preset["model"]).strip()),
        "api_key": next_key,
        "temperature": float(temperature),
        "timeout_seconds": normalized_timeout,
    }
    AppSettingRepository(db).set(AI_CHAT_CONFIG_KEY, json.dumps(payload, ensure_ascii=False))
    return load_ai_chat_config(db)


def masked_api_key(config: AIChatConfig) -> str:
    key = config.api_key or ""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}...{key[-4:]}"


def build_ai_chat_context(db: Session, *, lang: str = "zh") -> dict:
    report = load_ai_daily_report(db=db) or {}
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_items = list(watchlist_repo.list_ticker_map(watchlist.id).values())
    portfolio = load_portfolio_positions()
    def _candidate_view(item: dict) -> dict:
        buy_zone = item.get("buy_zone") if isinstance(item.get("buy_zone"), dict) else {}
        return {
            "ticker": item.get("ticker"),
            "name": item.get("name"),
            "market": item.get("market"),
            "verdict": item.get("verdict"),
            "tradability_status": item.get("tradability_status"),
            "headline": item.get("headline"),
            "entry_trigger": item.get("entry_trigger"),
            "invalidation_condition": item.get("invalidation_condition"),
            "buy_zone": {"low": buy_zone.get("low"), "high": buy_zone.get("high")} if buy_zone else None,
            "risk_flags": list(item.get("risk_flags") or [])[:4],
            "report_pool_reason": item.get("report_pool_reason"),
        }

    return {
        "language": lang,
        "portfolio": [
            {
                "ticker": item.get("ticker"),
                "name": item.get("name"),
                "market": item.get("market"),
                "quantity": item.get("quantity"),
                "cost_basis": item.get("cost_basis"),
            }
            for item in portfolio[:20]
        ],
        "watchlist": [
            {
                "ticker": item.get("ticker"),
                "name": item.get("name"),
                "market": item.get("market"),
                "sync_status": item.get("sync_status"),
                "last_synced_date": item.get("last_synced_date"),
            }
            for item in watchlist_items[:30]
        ],
        "ai_daily_report": {
            "report_date": report.get("report_date"),
            "headline": report.get("headline"),
            "mood": report.get("mood"),
            "strategy": report.get("strategy"),
            "market_recommendations": [_candidate_view(item) for item in (report.get("market_recommendations") or report.get("rows") or [])[:5]],
            "market_watch_recommendations": [_candidate_view(item) for item in (report.get("market_watch_recommendations") or [])[:5]],
            "model_selection_guidance_summary": report.get("model_selection_guidance_summary"),
            "market_recommendations_meta": report.get("market_recommendations_meta"),
        },
    }


def ask_ai_chat(*, db: Session, question: str, lang: str = "zh") -> dict:
    normalized_question = str(question or "").strip()
    if not normalized_question:
        return {"status": "error", "answer": "请输入问题。" if lang == "zh" else "Please enter a question."}
    config = load_ai_chat_config(db)
    if not config.is_configured:
        return {
            "status": "not_configured",
            "answer": (
                "AI 问答还没有配置 API Key、Base URL 或模型。请先到设置页保存 AI Provider 配置。"
                if lang == "zh"
                else "AI Q&A is not configured yet. Save provider, API key, base URL, and model in Settings first."
            ),
        }
    context = build_ai_chat_context(db, lang=lang)
    system_prompt = (
        "你是一个纪律严格的股票研究与交易复盘助手。你只能提供分析框架、风险提示和可执行检查清单，不能承诺收益。"
        "请优先结合用户当前应用里的持仓、自选、AI日报候选、模型评测信息回答。"
        "回答要结构化，必须包含：结论、理由、执行条件、风险、下一步。"
        if lang == "zh"
        else "You are a disciplined stock research and trading-review assistant. Provide analysis, risk framing, and executable checklists, never profit guarantees. Use the app context first. Include conclusion, rationale, execution conditions, risks, and next steps."
    )
    user_prompt = (
        f"用户问题：\n{normalized_question}\n\n"
        f"应用上下文 JSON：\n{json.dumps(context, ensure_ascii=False, indent=2)}"
    )
    payload = {
        "model": config.model,
        "temperature": config.temperature,
        "max_tokens": 1200,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if config.provider == "gemini":
        payload["reasoning_effort"] = "low"
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    url = f"{config.base_url.rstrip('/')}/chat/completions"
    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=config.timeout_seconds)
        response.raise_for_status()
        body = response.json()
        content = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        return {
            "status": "success",
            "answer": content or ("模型没有返回内容。" if lang == "zh" else "The model returned an empty response."),
            "provider": config.provider_name,
            "model": config.model,
        }
    except httpx.HTTPStatusError as exc:
        detail = str(exc)
        try:
            body = exc.response.json()
            error_payload = body.get("error") if isinstance(body, dict) else body
            if isinstance(error_payload, dict):
                detail = str(error_payload.get("message") or error_payload)
            elif error_payload:
                detail = str(error_payload)
            else:
                detail = exc.response.text[:500]
        except Exception:
            detail = exc.response.text[:500] if exc.response is not None else str(exc)
        if config.provider == "gemini":
            detail += (
                "\n\nGemini 提示：模型名必须使用 API id，例如 gemini-2.5-flash 或 gemini-2.5-pro；"
                "不要填写 Gemini 3.1 Pro 这类展示名。"
                if lang == "zh"
                else "\n\nGemini note: use API model ids such as gemini-2.5-flash or gemini-2.5-pro; do not use display names."
            )
        return {
            "status": "error",
            "answer": (
                f"AI 调用失败：{detail}"
                if lang == "zh"
                else f"AI request failed: {detail}"
            ),
            "provider": config.provider_name,
            "model": config.model,
        }
    except httpx.TimeoutException:
        return {
            "status": "timeout",
            "answer": (
                f"AI 调用超时：{config.provider_name} 在 {config.timeout_seconds:.0f} 秒内没有返回。"
                "我已经压缩了上下文；如果仍然超时，建议在设置页把 Timeout 调到 90-120 秒，或把问题拆成“只分析持仓/只分析某只股票”。"
                if lang == "zh"
                else f"AI request timed out: {config.provider_name} did not respond within {config.timeout_seconds:.0f}s. Increase timeout to 90-120s or narrow the question."
            ),
            "provider": config.provider_name,
            "model": config.model,
        }
    except Exception as exc:
        return {
            "status": "error",
            "answer": (
                f"AI 调用失败：{exc}"
                if lang == "zh"
                else f"AI request failed: {exc}"
            ),
            "provider": config.provider_name,
            "model": config.model,
        }
