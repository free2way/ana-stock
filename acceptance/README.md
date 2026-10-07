# acceptance/ — 验收关键件归档（最小集）

本目录是**面向交付的验收证据最小归档**。仓库内的 `docs/`（96 篇）与 `data/` 不随仓库交付
（见根 `.gitignore`），因此把**验收链条上不可替代的关键件**复制到这里，使拿到仓库的人不依赖
本机 `docs/`、`data/` 也能复核「整改是否通过、依据是什么、基线是什么」。

## 1. 用途

- 留住**判定链**：最终签署 → 独立复核 → 债务登记，以及支撑签署的阶段补录。
- 留住**可复算的规格与报告**：情绪因子前向批次规格、训练消融 multiseed 报告等。
- 留住**基线指针**：冻结目录与哈希清单（`freeze-manifest-2026-10-05.md`），使签署件可回溯到冻结代码/数据基线。
- 明确**不做什么**：不做全量文档镜像，不复制数据湖、DB 快照、评测产物等大文件。

## 2. 收录标准

**收录（满足其一）**

1. 验收**签署/判定**本身：签署件、独立复核报告、债务登记表。
2. 签署件**正文引用**、且离开本机后无法复现的**关键补录**（批次报告、规格、消融报告）。
3. 体量小、纯文本/结构化、可作为**基线指针**的清单（冻结哈希清单）。

**不收录**

- `docs/` 下的规划、历史记录、被取代版本（全量 96 篇不拉入）。
- 任何 `data/` 原件（数据湖、`*.db`、评测/预测产物、`app.db.snapshot`、`tar.gz`）——**只引用不复制**。
- 凭证、密钥、`.env*`、含敏感信息的运行日志。
- 单文件过大（> 1 MiB）或含二进制产物的材料——以引用代替。

原则：**复制（copy）而非移动**，保留 `docs/` 与 `data/` 原貌，避免打断既有文档与脚本引用。

## 3. 目录结构

```
acceptance/
├── README.md                     # 本文件
├── .gitignore                    # 放行本目录 *.md（对抗根 .gitignore 的 `*.md` 规则）
├── freeze-manifest-2026-10-05.md # 基线冻结清单（各 freeze-* 目录 + 四项哈希）
├── signoff/                      # 最终签署链
├── supplements/                  # 阶段补录
└── experiments/                  # 批次规格与报告
```

## 4. 收录清单（文件 → 来源）

### signoff/ — 最终签署链

| 归档文件 | 来源 |
|---|---|
| `quant-remediation-final-signoff-2026-10-03-zh.md` | `docs/quant-remediation-final-signoff-2026-10-03-zh.md` |
| `quant-remediation-independent-review-2026-10-03-zh.md` | `docs/quant-remediation-independent-review-2026-10-03-zh.md` |
| `acceptance-debt-registry-zh.md` | `docs/acceptance-debt-registry-zh.md` |

### supplements/ — 签署链关键补录

| 归档文件 | 来源 | 说明 |
|---|---|---|
| `quant-remediation-acceptance-report-2026-10-02-zh.md` | `docs/…-acceptance-report-2026-10-02-zh.md` | 批次 1 验收报告 |
| `quant-remediation-acceptance-report-batch2-2026-10-02-zh.md` | `docs/…-acceptance-report-batch2-2026-10-02-zh.md` | 批次 2 验收报告 |
| `selection-canonical-cost-and-inference-2026-10-05-zh.md` | `docs/selection-canonical-cost-and-inference-2026-10-05-zh.md` | 批次 1：统一成本口径 / 按日聚类 CI / 主评估基准（「接线」口径来源） |
| `training-ablation-harness-2026-10-05-zh.md` | `docs/training-ablation-harness-2026-10-05-zh.md` | 批次 2：训练侧四项改动消融 harness |
| `sentiment-v1-forward-batch-zh.md` | `docs/sentiment-v1-forward-batch-zh.md` | 情绪因子前向批次规格（运行手册） |

> 「接线与真实激活」的证据（run 390 / eval 134、reliability/calibration 产物）为 `data/` 产物，
> 只由 `signoff/quant-remediation-final-signoff-2026-10-03-zh.md` §3.7 引用，不复制。

### experiments/ — 批次规格与报告

| 归档文件 | 来源 |
|---|---|
| `sentiment_v1_forward_batch.json` | `data/experiments/sentiment_v1_forward_batch.json` |
| `selection_experiment_sentiment_v1_example.yaml` | `data/experiments/selection_experiment_sentiment_v1_example.yaml` |
| `sentiment_v1_dataset_metadata.json` | `data/experiments/sentiment_v1_dataset_metadata.json` |
| `training_ablation_multiseed/training-ablation-20261005T120020Z.json` | `data/experiments/training_ablation_multiseed/…json` |
| `training_ablation_multiseed/training-ablation-20261005T120020Z.md` | `data/experiments/training_ablation_multiseed/…md` |

### 基线

| 归档文件 | 来源 |
|---|---|
| `freeze-manifest-2026-10-05.md` | 由 `data/artifacts/freeze-*/summary.json` 汇总（引用，不复制产物） |

## 5. 未收录及原因

- `docs/` 其余约 90 篇（规划、历史/被取代版本、UI 评审等）：非验收判定必需，保持仓库精简。
- `data/artifacts/freeze-*/`（`app.db.snapshot`、`untracked_snapshot.tar.gz`、`eval-artifacts/`）：体积大 / 含运行态数据，
  **只引用哈希**（见 `freeze-manifest-2026-10-05.md`）。
- `data/artifacts/stock_selection_research/{reliability,calibration}/*.json`、`acceptance-20261002/` 账本与日志：
  `data/` 数据域，按提交策略不入库，由签署件正文引用定位。
- `.env` / `.env.local`：凭证，**永不入库**。

## 6. 更新时机

- **验收签署或复核结论变更**时：同步刷新 `signoff/`（签署件、独立复核报告）。
- **债务登记表重算**（新账本口径）时：刷新 `signoff/acceptance-debt-registry-zh.md`。
- **新批次报告 / 新前向批次规格落盘**且被签署件引用时：补齐对应 `supplements/` / `experiments/` 文件。
- **产生新的代码冻结基准（freeze-*）**时：刷新 `freeze-manifest-2026-10-05.md`（文件名按当前日期更新）。
- 更新后按 `git add acceptance/<显式路径>` 提交，保持单一归档提交、只含本目录文件。

## 7. 生效机制（gitignore）

- 根 `.gitignore` 含 `*.md`（仅放行根 `README.md`/`README.zh-CN.md`），会连带忽略本目录的 `*.md`。
- 本目录的 `acceptance/.gitignore` 用 `!*.md` **在本子树内放行** `*.md`，因此**无需改动根 `.gitignore`**
  （`docs/`、`data/` 忽略规则保持原样）。
- `data/experiments/` 被根 `.gitignore` 忽略，故 `experiments/` 下的 JSON/YAML 采用**复制**而非软链/引用。

## 8. 债务登记表权威版（以本目录 `signoff/` 为准）

- **入库权威版**：`acceptance/signoff/acceptance-debt-registry-zh.md`。它是随仓库交付、供外部复核的版本，**签署链引用、版本判定与提交一律以此为准**。
- **本地工作副本**：`docs/acceptance-debt-registry-zh.md`。根 `.gitignore` 含 `/docs/`（不随仓库交付），故该副本只作本地编辑/查阅用；**两版内容应保持一致**（当前一致，可用
  `diff -u docs/acceptance-debt-registry-zh.md acceptance/signoff/acceptance-debt-registry-zh.md` 核对，无输出即一致）。
- **刷新顺序**：先在 `docs/acceptance-debt-registry-zh.md` 改写/重算（写作在本机 docs 侧进行），再 `cp docs/acceptance-debt-registry-zh.md acceptance/signoff/acceptance-debt-registry-zh.md` 覆盖权威版，
  最后按 `git add acceptance/signoff/acceptance-debt-registry-zh.md` 提交。
- 原因：`docs/` 被忽略，若只改 docs 而不回填 `acceptance/`，外部拿到仓库时看不到最新债务表，判定链会断在这里。
