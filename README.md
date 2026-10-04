<div align="center">

# ANA
### From market close to a clearer research agenda.

**A local-first quantitative research workspace for A-shares and U.S. equities.**

[English](README.md) · [简体中文](README.zh-CN.md)

[![GitHub stars](https://img.shields.io/github/stars/free2way/ana-stock?style=social)](https://github.com/free2way/ana-stock/stargazers)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?style=flat-square&logo=postgresql&logoColor=white)
![Workflow](https://img.shields.io/badge/Workflow-Post--close-0F766E?style=flat-square)

**Collect → Screen → Evaluate → Review**

</div>

---

## One workspace for the next trading day

ANA brings end-of-day market data, quantitative screening, watchlists, portfolio review, and AI-assisted reports into a single workspace. Background jobs prepare results; the interface helps you inspect them and decide what deserves further research.

Built for deliberate post-close analysis—not high-frequency execution or automated brokerage trading.

| Discover | Understand | Stay in control |
| :--- | :--- | :--- |
| Explore model-ranked candidates and market context. | Inspect evaluations, data quality, and research history. | Review holdings, job status, and daily summaries together. |

## Interface preview

![ANA screener: market selection, research overview, and strategy playbooks](assets/readme/screener-en.png)

*Actual application screenshot of the initial screener view, before running a screen. No stock tickers or private holdings are displayed.*

## What you can do

- **Follow two markets.** Separate A-share and U.S. refresh workflows and market-quality checks.
- **Screen with context.** Model-driven candidate lists alongside market, sector, and watchlist views.
- **Review your portfolio.** Holdings, performance, and review suggestions, with sensitive amounts hidden until revealed.
- **Read the daily picture.** AI-assisted summaries and configurable Feishu notifications.
- **Inspect the process.** Background job status, model evaluations, failures, and source diagnostics.
- **Keep research local.** PostgreSQL for application state; a Parquet lake for market history.

Availability depends on provider permissions, configuration, data freshness, and completed jobs. AI reports may be unavailable when their inputs are not ready.

## Research controls, built in

ANA is designed to make the path from raw market data to a research decision easier to inspect:

| Capability | What it adds |
| :--- | :--- |
| **Consistent price basis** | A shared, versioned contract checks adjusted-price availability, coverage, and view integrity across training, prediction, and backtesting. Raw-price fallback requires explicit, auditable authorization. |
| **Time-aware research** | Point-in-time data and universe checks help keep future information out of historical research. Walk-forward evaluation records the training and evaluation window used. |
| **More realistic replay** | Event-driven backtests account for supported splits and dividends, with configurable transaction costs and portfolio constraints. Unsupported corporate actions block a run by default and remain visible in its audit record. |
| **Evidence-led promotion** | A promotion gate brings data readiness, price basis, corporate-action evidence, sample size, and out-of-sample results into one decision. Explicit failures are withheld from the recommendation path. |
| **Operational visibility** | A read-only risk snapshot brings together market-data freshness, adjusted-view coverage, corporate-action coverage, gate outcomes, and model drift. |

These controls improve traceability; they do not establish that a strategy is profitable or that every data source has complete historical coverage.

## System logic

```mermaid
flowchart TD
    A[Market Data Providers] --> B[Scheduled Collection]
    B --> C[Normalization and Quality Checks]
    C --> D[(Market-separated Parquet Lake)]
    C --> E[(PostgreSQL: State and Audit)]
    D --> F[Features and Research Pipelines]
    F --> G[Model Training and Scoring]
    G --> H[Candidate Snapshots]
    H --> I[Screeners and Watchlists]
    H --> J[Portfolio Review]
    I --> K[AI-assisted Daily Report]
    J --> K
    K --> L[Dashboard and Optional Notifications]
    E --> L
    D -.-> M[Execution Facts and Provenance]
    M -.-> N{Evidence Complete?}
    N -. No .-> O[Blocked or Unverified]
    N -. Yes .-> P[New-contract Evaluation and Replay]
    P -.-> L
    classDef data fill:#e0f2fe,stroke:#0284c7,color:#0c4a6e;
    classDef work fill:#eef2ff,stroke:#6366f1,color:#312e81;
    classDef view fill:#ccfbf1,stroke:#0d9488,color:#134e4a;
    classDef gate fill:#fff7ed,stroke:#ea580c,color:#7c2d12;
    class A,D,E data;
    class B,C,F,G,H work;
    class I,J,K,L view;
    class M,N,O,P gate;
```

Solid paths summarize the application workflow. Dashed paths show the execution-evidence work **under validation**, not completed production acceptance. Each market must qualify independently.

## Designed for inspection, not promises

ANA is a research tool. Model scores are not guaranteed win probabilities, historical performance does not promise future returns, and AI-generated narratives need human review.

The shared price-basis contract and explicit-failure promotion checks are implemented. For compatibility, legacy runs with incomplete promotion evidence can still be shown as **research-only / not promoted** unless complete-evidence enforcement is enabled. This project does not claim a proven profitable strategy or fully certified execution simulation.

## Built with

| Layer | Technology |
| :--- | :--- |
| Application | Python · FastAPI · server-rendered HTML |
| State and audit | PostgreSQL |
| Historical data and analytics | Parquet · DuckDB · Polars |
| Quantitative research | LightGBM and supporting research pipelines |
| Operations | Background jobs · scheduled refreshes · notifications |

Provider support includes Tushare, HiThink Financial API, Alpaca, Polygon, and selected fallbacks. Support does not imply that every account has all historical fields or full-market coverage.

## Repository scope

Explore [application source](app/), [tests](tests/), and [scripts](scripts/).

This repository includes source, tests, dependency manifests, and generic runtime files. Internal development plans, acceptance reports, detailed deployment/configuration guides, credentials, market datasets, model artifacts, and machine-specific deployment files are intentionally excluded. Some operational scripts require local evidence or environment-specific resources not distributed here.

---

<div align="center">

**Better research starts with visible evidence.**

Research software · Human decisions · No return guarantees

[⭐ Star ANA on GitHub](https://github.com/free2way/ana-stock) · [Explore the source](https://github.com/free2way/ana-stock/tree/main) · [Report an issue](https://github.com/free2way/ana-stock/issues)

</div>
