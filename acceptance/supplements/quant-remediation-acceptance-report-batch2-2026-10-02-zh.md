# 量化口径治理修复：验收报告（第 2 批）

- 日期：2026-10-02
- 对应文档：《开发落地技术文档》《验收文档》
- 本批范围：**B-1（provenance schema + 双写）、B-5（基准冲突 fail-closed）、B-6（日历依赖）、B-7 离线部分（公司行为存储/校验/对账助手）**
- 结论：**B-1/B-5/B-6 通过；B-7 部分通过（provider 采集待网络环境）**；无本批引入的回归。

---

## 1. 变更清单

| 文件 | 变更 | 对应验收项 |
|---|---|---|
| `app/services/market_lake.py` | `LAKE_V2_SCHEMA`（10 个 provenance 字段）；`write_lake_v2_partition` 影子写；基准冲突 fail-closed（批内 + 对已有分区）；影子先写、冲突则阻断 v1；`_normalize_ohlcv_row` 保留 provenance 字段 | B-1、B-5 |
| `app/core/config.py` | 新增 `lake_v2_shadow_enabled`（默认 True，可关闭） | B-1 |
| `app/services/corporate_actions.py`（新增） | 公司行为记录模型、校验、Parquet 存储（自然键去重、按 ingestion 取新）、窗口/标的读取、除权跳变解释 | B-7 |
| `scripts/backfill_lake_v2_provenance.py`（新增） | 从 v1 湖回填影子并写入真实来源标注；幂等 | B-1/B-4 |
| `scripts/audit_price_basis.py` | 新增 `shadow_v2` 段：行数、符号数、逐字段 provenance 覆盖率 | B-1 |
| `requirements.txt` / 两份锁文件 | `exchange_calendars==4.11.1`（含传递依赖 korean_lunar_calendar/pyluach/toolz） | B-6 |
| 新增测试 3 个文件 | 影子双写/冲突、公司行为、日历边界 | B-1/B-5/B-6/B-7 |

**与开发文档的一处偏差（有意，已记录）**：影子路径采用 `data/lake/_lake_v2/`（v1 根内、下划线前缀），而不是文档中的 `data/lake_v2/`。原因：测试 patch 湖根时影子随之隔离；以下划线开头可被枚举工具（freeze manifest 等）安全跳过，避免把影子误当市场分区。

---

## 2. 验收结果

### B-1 provenance schema 与双写 —— 通过

| 验收标准 | 结果 | 证据 |
|---|---|---|
| v2 shadow 必填 provenance 覆盖率 100% | **通过（真实湖实测）** | 全量回填后：CN 2,064,085 行 / 5,583 标的、US 807,952 行 / 6,989 标的，10 个字段覆盖率均 = 1.0（`price-basis-audit-after-backfill.json` → `shadow_v2.minimum_coverage`） |
| 实时写入自动带 provenance | **通过** | `tests/test_lake_v2_shadow.py`：provider/provider_symbol/price_basis/volume_unit/currency/source_reference/source_batch_id/ingested_at/revision_id 全部非空 |
| 无来源字段不静默留空 | **通过** | 缺失时显式写 `unknown` / `unspecified` / `share` / 市场币种；回填写 `legacy_v1_lake:<分区相对路径>` + `backfill-lake-v2-<UTC>` |
| 可回填、幂等 | **通过** | `backfill-lake-v2-full.json`：CN 424 分区、US 438 分区、0 错误，重复回填覆盖同一批键 |

### B-5 基准冲突 fail-closed —— 通过

| 场景 | 结果 | 证据 |
|---|---|---|
| 同一批次内同 (date,symbol) 出现 raw+adjusted | **通过：拒绝，且 v1/v2 均不写** | `test_basis_conflict_within_batch_blocks_both_stores` |
| 与影子已有分区基准不一致 | **通过：拒绝合并** | `test_basis_change_against_existing_shadow_partition_is_rejected` |
| 影子可显式关闭且不影响 v1 | **通过** | `test_shadow_can_be_disabled` |

### B-6 交易日历依赖 —— 通过（附边界说明）

| 验收标准 | 结果 | 证据 |
|---|---|---|
| 依赖入库并锁版本（含传递依赖） | **通过** | `exchange_calendars==4.11.1` 已进 requirements.txt + 两份锁文件 |
| XNYS 覆盖 2026–2027 关键假日 | **通过** | 2026-01-01/07-03/11-26/12-25、2027-01-01 均闭市；last_session ≥ 2027-06 |
| XHKG 覆盖 2026 | **通过** | 2026-01-01、2026-02-17 闭市；last_session ≥ 2026-12-31 |
| **XSHG 边界** | **已显式固定（非通过项）** | 4.11.1 的 XSHG 数据止于 **2025-12-31**（`test_xshg_data_boundary_is_explicit` 固定该边界）。结论：CN 日历不能仅靠该库，S-9 必须实现「版本化本地节假日覆盖表」后才能切换 |

### B-7 公司行为 —— 部分通过

| 内容 | 结果 |
|---|---|
| 记录模型与校验（类型白名单、split 必须给 factor、cash_dividend 必须给金额、公告日不得晚于生效日） | **通过**（8 个用例） |
| Parquet 存储：自然键去重、按 ingestion 取胜、窗口/标的多条件读取 | **通过** |
| 除权跳变解释（2:1 → close≈prev/2；分红 → close≈prev−cash；含 3% 容差与 symbol 过滤） | **通过** |
| 真实 provider 采集（TuShare `adj_factor`/分红送转、Polygon splits/dividends，Alpaca 交叉校验） | **未实施**（需网络与 token；留待 C1 前完成） |
| 每市场 ≥5 个真实事件逐条对账 | **未实施**（同上；离线部分已就绪） |

---

## 3. 事故与处置（测试污染，非代码回归）

- **现象**：第 2 批 PG 全量回归期间（2026-10-02 21:12:54），真实 v1 湖 US 新增 3 行（`2026-04-03` 的 AAPL/MSFT/ASTS），行数从基线 807,952 变为 807,955。
- **根因**：仓库既有测试隔离缺陷——`/jobs/seed-sample-data` 等任务在后代线程中运行，测试 `tearDown` 先恢复环境变量并清理临时目录，后台线程随后执行 `seed_sample_data()`，此时 `get_settings()` 已指回默认数据目录，于是样例数据写入真实湖。与本批代码改动无关（改动文件均不涉及种子任务）。
- **处置**：定位到该分区仅含这 3 行样例数据（全新分区，冻结基线中不存在），已将其移出湖到事故证据目录；v1 US 行数恢复为 **807,952（与基线一致）**，CN 保持 2,064,085。影子同分区一并移出。
- **证据**：`data/artifacts/acceptance-20261002/incident-lake-test-pollution/`（两个被移除分区原件）；`price-basis-audit-final.json`（修复后 v1/v2 行数与覆盖率）。
- **遗留**：11 个既有分区被幂等合并重写（行集合不变、字节哈希变化）；另有 17 行样例数据在冻结前即已存在于 v1 湖（沿用基线，不在本批清理范围）。**跟进项**：测试的后台任务需捕获创建时的设置快照或在 teardown 前等待线程结束，避免再次污染真实湖。

---

## 4. 回归证据

| 运行 | 命令 | 结果 |
|---|---|---|
| 定向回归（14 个模块，含本批全部新测试） | `python -m unittest <14 modules>` | **100 通过 / 0 失败** |
| 全量（无测试库） | `python -m unittest discover -s tests` | Ran 1023；**0 失败**；189 错误全部为缺 `PQW_TEST_DATABASE_URL` |
| 全量（临时 PostgreSQL 17） | 同上 + `PQW_TEST_DATABASE_URL` | Ran 1062；**969 通过**；44 失败 + 49 错误。与第 1 批失败集合逐名比对：**无新增**，反而少了 1 个既有顺序相关用例（`test_watchlist_uses_batched_prediction_queries`） |
| 编译与 lint | `compileall` + `ruff check`（本批文件） | 通过 |

日志/证据：`data/artifacts/acceptance-20261002/`（`regression-local-batch2.log`、`regression-with-postgres-batch2.log`、`lake-v2-backfill-full.json`、`price-basis-audit-after-backfill.json`）。

---

## 5. 下一步（进入 P0C 的前置清单）

1. **公司行为真实采集**：TuShare `adj_factor` + 分红送转；US Polygon splits/dividends（Alpaca 仅交叉校验）。入库后跑「每市场 ≥5 事件」逐条对账（B-7 收尾）。
2. **C1 adjusted view 重建**：用 `corporate_actions` + 复权因子生成派生视图（`adj_version = f(raw manifest hash, actions hash, method)`）；CN 建议 qfq、US split+dividend 全复权。
3. **C2 跳变对账报告**：以本批审计为基线（CN 216 处 >30%、US 1,401 处 >50%），逐条匹配动作/白名单/隔离，目标「未解释跳变 = 0」。
4. **S-9 CN 日历覆盖表**：exchange_calendars 的 XSHG 止于 2025，需版本化本地假日表（含调休）并通过 2025/2026/2027 断言。
5. 之后才是 C3/C4（标签/排名影响报告）与 P1 评估契约。

## 6. 签署

```
执行人：DimAgent（自动化）    复核人：待指定    日期：2026-10-02
代码 revision：34c80d9d + 工作区未提交改动
数据 manifest：data/artifacts/freeze-20261002T123533Z/baseline.json
回填证据：lake-v2-backfill-full.json（CN 424 分区 / US 438 分区 / 0 错误）
覆盖率证据：price-basis-audit-after-backfill.json（两市场 min coverage = 1.0）
豁免项：无
结论：B-1/B-5/B-6 通过；B-7 离线部分通过、采集部分待办；无本批引入回归
```
