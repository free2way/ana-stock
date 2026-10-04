from __future__ import annotations

from typing import Any

from app.services.template_evaluation import (
    lightgbm_maturity,
    next_tesla_maturity,
    technical_momentum_maturity,
)


def build_model_performance_brief(
    *,
    next_tesla: dict[str, Any],
    next_tesla_state: dict[str, Any],
    technical: dict[str, Any],
    technical_state: dict[str, Any],
    lightgbm: dict[str, Any],
    lightgbm_state: dict[str, Any],
    market: str,
    lang: str,
) -> list[dict[str, str]]:
    next_count = int(next_tesla.get("clean_snapshot_total") or 0)
    technical_count = int(technical.get("labeled_snapshot_total") or 0)
    lightgbm_count = int(lightgbm.get("labeled_snapshot_total") or 0)
    focus_title, focus_copy = _focus_brief(
        next_count=next_count,
        next_state=next_tesla_state,
        technical_count=technical_count,
        technical_state=technical_state,
        lightgbm_count=lightgbm_count,
        lightgbm_state=lightgbm_state,
        lang=lang,
    )
    market_title, market_copy = _market_brief(
        next_tesla=next_tesla,
        technical=technical,
        lightgbm=lightgbm,
        market=market,
        lang=lang,
    )
    verdict_title, verdict_copy = _verdict_brief(
        states=(next_tesla_state, technical_state, lightgbm_state), lang=lang
    )
    return [
        {
            "eyebrow": "当前更值得看" if lang == "zh" else "Worth watching now",
            "title": focus_title,
            "copy": focus_copy,
        },
        {
            "eyebrow": "市场参考度" if lang == "zh" else "Market usefulness",
            "title": market_title,
            "copy": market_copy,
        },
        {
            "eyebrow": "一句话判断" if lang == "zh" else "Bottom line",
            "title": verdict_title,
            "copy": verdict_copy,
        },
    ]


def maturity_rank(level: str | None) -> int:
    value = str(level or "").strip().lower()
    if value in {"可比较", "comparable"}:
        return 2
    if value in {"初步参考", "early read"}:
        return 1
    return 0


def _focus_brief(
    *,
    next_count: int,
    next_state: dict[str, Any],
    technical_count: int,
    technical_state: dict[str, Any],
    lightgbm_count: int,
    lightgbm_state: dict[str, Any],
    lang: str,
) -> tuple[str, str]:
    scores = sorted(
        (
            ("next_tesla", maturity_rank(next_state.get("level")) * 100 + next_count),
            ("technical", maturity_rank(technical_state.get("level")) * 100 + technical_count),
            ("lightgbm", maturity_rank(lightgbm_state.get("level")) * 100 + lightgbm_count),
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    leader, leader_score = scores[0]
    decisive = leader_score >= scores[1][1] + 8
    if leader == "next_tesla" and decisive:
        return (
            "强趋势二次启动" if lang == "zh" else "Next Tesla Swing",
            f"当前 clean 样本 {next_count} 个，成熟度为 {next_state.get('level') or '-'}，比技术动量更接近可比较状态。"
            if lang == "zh"
            else f"It currently has {next_count} clean samples and a {next_state.get('level') or '-'} maturity state, making it closer to being comparable than technical momentum.",
        )
    if leader == "technical" and decisive:
        return (
            "技术动量" if lang == "zh" else "Technical Momentum",
            f"当前带标签样本 {technical_count} 个，成熟度为 {technical_state.get('level') or '-'}，目前更适合作为主观察模板。"
            if lang == "zh"
            else f"It currently has {technical_count} labeled samples and a {technical_state.get('level') or '-'} maturity state, which makes it the stronger observation template right now.",
        )
    if leader == "lightgbm" and decisive:
        return (
            "LightGBM 多因子优选" if lang == "zh" else "LightGBM Top Picks",
            f"当前带动作样本 {lightgbm_count} 个，成熟度为 {lightgbm_state.get('level') or '-'}，已经开始具备独立评测价值。"
            if lang == "zh"
            else f"It currently has {lightgbm_count} action-labeled samples and a {lightgbm_state.get('level') or '-'} maturity state, which makes it increasingly useful as a standalone evaluation track.",
        )
    return (
        "三套模板都偏观察" if lang == "zh" else "All three templates are still observational",
        f"强趋势二次启动 {next_count} 个 clean 样本，技术动量 {technical_count} 个带标签样本，LightGBM {lightgbm_count} 个动作样本，当前都更适合作为观察面板。"
        if lang == "zh"
        else f"Next Tesla Swing has {next_count} clean samples, Technical Momentum has {technical_count} labeled samples, and LightGBM has {lightgbm_count} action samples, so all three are still better used as observation panels.",
    )


def _market_score(
    code: str,
    *,
    next_tesla: dict[str, Any],
    technical: dict[str, Any],
    lightgbm: dict[str, Any],
    lang: str,
) -> int:
    next_payload = (next_tesla.get("per_market") or {}).get(code) or {}
    technical_payload = (technical.get("per_market") or {}).get(code) or {}
    lightgbm_payload = (lightgbm.get("per_market") or {}).get(code) or {}
    return (
        maturity_rank(next_tesla_maturity(next_payload, lang=lang).get("level")) * 100
        + int(next_payload.get("clean_snapshot_total") or 0)
        + maturity_rank(technical_momentum_maturity(technical_payload, lang=lang).get("level")) * 100
        + int(technical_payload.get("labeled_snapshot_total") or 0)
        + maturity_rank(lightgbm_maturity(lightgbm_payload, lang=lang).get("level")) * 100
        + int(lightgbm_payload.get("labeled_snapshot_total") or 0)
    )


def _market_brief(
    *,
    next_tesla: dict[str, Any],
    technical: dict[str, Any],
    lightgbm: dict[str, Any],
    market: str,
    lang: str,
) -> tuple[str, str]:
    if market != "ALL":
        label = "A股" if market == "CN" and lang == "zh" else "美股" if market == "US" and lang == "zh" else market
        return (
            f"当前范围：{label}" if lang == "zh" else f"Current scope: {label}",
            "当前页面已经按所选市场单独评测，适合先在这个市场里比较模板，再回头做跨市场判断。"
            if lang == "zh"
            else "This page is already scoped to the selected market, so compare templates inside this market first before making cross-market judgments.",
        )
    cn_score = _market_score("CN", next_tesla=next_tesla, technical=technical, lightgbm=lightgbm, lang=lang)
    us_score = _market_score("US", next_tesla=next_tesla, technical=technical, lightgbm=lightgbm, lang=lang)
    if cn_score >= us_score + 8:
        return (
            "A股更有参考价值" if lang == "zh" else "CN is more informative",
            "A股这边至少已经开始积累 clean / 带标签样本，更适合先拿来观察模板节奏。"
            if lang == "zh"
            else "CN already has a better base of clean and labeled samples, so it is the more useful place to observe template behavior first.",
        )
    if us_score >= cn_score + 8:
        return (
            "美股更有参考价值" if lang == "zh" else "US is more informative",
            "美股这边当前样本沉淀更完整，更适合先看模板胜率和板块集中度。"
            if lang == "zh"
            else "US currently has the more complete sample base, making it a better place to study hit rates and sector concentration first.",
        )
    return (
        "A股和美股目前接近" if lang == "zh" else "CN and US are currently close",
        "两个市场都还在样本沉淀期，暂时不适合只因为市场不同就下强判断。"
        if lang == "zh"
        else "Both markets are still in the sample-accumulation phase, so it is too early to draw a strong market-level preference.",
    )


def _verdict_brief(
    *, states: tuple[dict[str, Any], ...], lang: str
) -> tuple[str, str]:
    top_rank = max(maturity_rank(state.get("level")) for state in states)
    if top_rank <= 0:
        return (
            "先观察，不急着下结论" if lang == "zh" else "Observe first, do not force a verdict",
            "三套模板当前都更像观察面板，重点是持续留样，而不是立刻判断哪套一定更赚钱。"
            if lang == "zh"
            else "All three templates currently behave more like observation panels, so the priority is to keep collecting samples rather than forcing a winner right now.",
        )
    if top_rank == 1:
        return (
            "可以初步参考" if lang == "zh" else "Good for an early read",
            "已经可以开始用来观察动作偏向和板块集中，但还不适合把它当成高置信度评分卡。"
            if lang == "zh"
            else "The panel is now useful for reading action bias and sector concentration, but it is still too early to treat it as a high-confidence scorecard.",
        )
    return (
        "样本已经可比较" if lang == "zh" else "Samples are now comparable",
        "当前可以更严肃地比较动作类型、市场差异和板块贡献，适合进入真正的模型复盘。"
        if lang == "zh"
        else "You can now more seriously compare playbooks, market differences, and sector contribution, which is enough for a more formal model review.",
    )
