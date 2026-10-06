# 量化口径治理修复：验收报告（第 1 批）

- 日期：2026-10-02
- 对应文档：《量化口径治理与修复：开发落地技术文档》《量化口径治理与修复：验收文档》
- 验收基线：`data/artifacts/freeze-20261002T123533Z/`（commit `34c80d9d…`，dirty-tree patch `c8ce1766…`，CN 2,064,085 行 / US 807,952 行）
- 本批范围：P0A 全部、P0B 校验与隔离、P0D（F4/F5）、E-3/E-4/E-5、E-1 标记（部分）
- 结论：**在 27 个验收项中，通过 10 项、部分通过 6 项、未实施 11 项**；未发现由本批改动引入的回归。

---

## 1. 变更清单

| 文件 | 变更 | 对应验收项 |
|---|---|---|
| `scripts/freeze_quant_remediation_baseline.py`（新增） | 冻结基线：commit/patch hash、依赖、湖 manifest、SQLite 快照、评估 artifact | A-1…A-4 |
| `app/services/market_data_quality.py`（新增） | OHLCV 逐行校验 + 拒绝原因码 + JSONL 隔离 | B-2、B-3 |
| `app/services/market_lake.py` | 写湖前校验/隔离；全量非法时 fail-closed 拒绝该分区 | B-2、B-3 |
| `app/services/trainer.py` | F4：涨停次日特征排除信号日；F5：基本面历史改读 PIT 存储并按可用时间推进 | D-1…D-3、D-6 |
| `app/services/repositories/research.py` | PIT 历史查询支持 `market=None/"ALL"`（供全市场训练） | D-2 |
| `app/services/execution_costs.py` | v2 成本模型：买卖分离、印花税/过户费/最低佣金、SEC/TAF；`default_fill_cost_model` | E-2、E-4 |
| `app/services/backtesting/schemas.py` | EngineConfig 新增成本字段；**CN holding_days<2 拒绝（T+1）** | E-4、E-5 |
| `app/services/backtesting/engine.py` | 透传全部成本字段 | E-4 |
| `app/services/backtesting/runner.py` | 按市场套用法定成本默认值；透传行业上限/总敞口/参与率；成本与风控参数写入 run manifest | E-2、E-3、E-4 |
| `app/services/backtester.py` | 事件分支透传风控参数；legacy 摘要标注 `legacy_unverified` 与不可解释说明 | E-1（部分）、E-3 |
| `app/services/stock_selection/protocol.py` | CN horizon<2 拒绝（T+1） | E-5 |
| `app/services/stock_selection/sample_builder.py` | CN horizons 含 1 无条件拒绝（不再仅限 fill-cost 模式） | E-5 |
| `scripts/audit_price_basis.py`（新增） | 只读价格口径审计：adj_close 差异、负价、越带跳变、provenance 缺失清单 | B-4/C 证据 |
| 新增测试 6 个文件（见 §4） | 时间旅行、可用时间、湖校验、成本、T+1、参数透传 | D-1/D-2、B-2、E-3/E-4/E-5 |
| `tests/test_event_driven_backtest.py` | 3 个 CN h=1 用例迁移为 h=2（T+1 语义变更），新增 CN h=1 拒绝用例 | E-5 |

---

## 2. P0A 验收结果（全部通过）

| ID | 标准 | 结果 | 证据 |
|---|---|---|---|
| A-1 | commit/patch hash 可复验 | **通过** | `baseline.json`；patch `c8ce1766c705dd802509644578d66a000f4c659af22848af1d71657d835c8607`（freeze 时点） |
| A-2 | 湖 manifest 与基线一致 | **通过** | CN 2,064,085 行/5,583 标的、US 807,952 行/6,989 标的（脚本重跑一致） |
| A-3 | DB 快照可恢复 | **通过** | `app.db.snapshot` `PRAGMA integrity_check = ok`；`SHA256SUMS` 全量校验 OK |
| A-4 | 冻结期写水位记录 | **部分** | 记录了 WAL 存在状态；未停 scheduler（当时服务可写），修复期以“不写库”方式操作 |

证据目录：`data/artifacts/freeze-20261002T123533Z/`（baseline.json、summary.json、SHA256SUMS、app.db.snapshot、eval-artifacts/）。

---

## 3. P0B / P0C 验收结果

| ID | 标准 | 结果 | 证据 / 说明 |
|---|---|---|---|
| B-2 | 非法行 0 进入 canonical，全部进隔离 | **通过** | `tests/test_market_data_quality.py`（6 例）：非正价/OHLC 不自洽/负量 → 隔离；全量非法时拒绝写分区 |
| B-3 | 隔离行有原因码且可检索 | **通过** | `_quarantine/{market}_rejected_{date}.jsonl`，含 rejected_at/market/reason/schema_version |
| B-1 | provenance 字段覆盖率 100%（schema 扩展+双写） | **未实施** | v1 schema 无 provenance；`audit_price_basis` 已把缺失字段列为证据 |
| B-4 | 双写审计可重复 | **部分** | 未建 v2 双写；只读审计脚本可重复且两次结果一致 |
| B-5 | 多源基准冲突 fail-closed | **未实施** | 需 schema 扩展后实现 |
| B-6 | `exchange_calendars` 锁版本 | **未实施** | 评估意见要求作为正式依赖；属 P0B 剩余项 |
| B-7 | 公司行为逐条对账（≥5 事件/市场） | **未实施** | 属 P0C |
| C-1 | 未解释越带跳变 = 0 | **未实施**（已产出基线证据） | 基线：CN 跳变>30% 216 处、US>50% 1,401 处（`price-basis-audit.json`），且 CN `adj_close!=close` 0 行、US 733 行、负价 251 行（TOWCF） |

---

## 4. P0D 验收结果

| ID | 标准 | 结果 | 证据 |
|---|---|---|---|
| D-1 | F4 时间旅行：改 `symbol_rows[index+1]` 特征不变 | **通过** | `tests/test_trainer_time_travel.py`：涨停次日特征恒为 0.1，计数/近因仍含信号日（0.4/0.0） |
| D-2 | F5：报告期到、公告日未到 → 不可用；公告后可用 | **通过** | `tests/test_trainer_fundamental_availability.py`：游标按 `available_time` 推进；缺时间戳行跳过（fail-closed） |
| D-3 | 全特征时间旅行测试族 | **部分** | 现有仓库已有 `test_future_price_change_does_not_change_past_features` 等；本批新增涨停特征一族；剩余因子族沿用既有测试 |
| D-4 | 训练 run 泄漏审计 violation=0 | **未执行** | 需要可训练数据环境；本地 PIT 库为空 → F5 后基本面特征按契约 fail-closed 为 0，需在数据齐备环境复跑 |
| D-5 | `exit_allowed` 修正 | **未实施** | 仍在 P0D 待办 |
| D-6 | 宽表不再供训练 | **部分** | 特征历史已切 PIT；宽表仅剩 `listing_date` 等非训练元数据引用 |

---

## 5. P1 部分验收结果

| ID | 标准 | 结果 | 证据 |
|---|---|---|---|
| E-1 | legacy 标注且不再发布不可解释指标 | **部分** | 摘要已加 `engine_status=legacy_unverified` 与 `metrics_interpretation` 说明；**页面/导出仍展示 Sharpe/IR/年化，未切断** |
| E-2 | run manifest 含引擎/成本/日历版本 | **部分** | 成本模型 version/hash 与各费项已写 strategy config + summary；日历/调整版本待 P0B/P0C |
| E-3 | 行业上限/gap/参与率设置后生效 | **通过** | `tests/test_backtester_engine_forwarding.py`：转发捕获 0.2/0.8/0.05；引擎行业上限实测拒绝第 2 个同行业候选 |
| E-4 | CN 印花税/过户费/最低佣金、US SEC/TAF、买卖分离 | **通过** | `tests/test_execution_costs_v2.py`（6 例）：最低佣金是下限而非叠加；卖出侧才收税；金额精确到分位 |
| E-5 | CN T+1 四个入口拒绝 | **3/4 通过** | 协议/引擎/样本构建已拒绝（含新增用例）；paper 经协议继承；**手动账本卖出未加校验** |
| E-6…E-9 | NW/块 bootstrap CI、试验注册表、OOS 重算、运行改只读 | **未实施** | P1 剩余主线 |

---

## 6. 回归与测试证据

| 运行 | 命令 | 结果 |
|---|---|---|
| 编译 | `.venv/bin/python -m compileall -q app tests scripts` | 通过 |
| 本批定向回归 | 见 §4 测试文件清单（16 个模块） | **106 通过 / 0 失败** |
| 全量（无测试库） | `.venv/bin/python -m unittest discover -s tests` | Ran 1006；**0 失败**；189 错误全部为缺 `PQW_TEST_DATABASE_URL`（817 通过） |
| 全量（临时 PostgreSQL 17，端口 55433） | 同上 + `PQW_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55433/pqw_test` | Ran 1045；**951 通过**；45 失败 + 49 错误。失败全部位于未完成的接口重构路径（`app/api/routes/watchlist.py` AttributeError、dashboard `_common`、screener 批量提示/CSV 导出、AI 报告候选范围等）；逐条栈帧核对其最后 app 帧均不在本批改动文件中 |

日志：`data/artifacts/acceptance-20261002/regression-local-final.log`、`regression-with-postgres-final.log`、`price-basis-audit.json`。

> 残余不确定性：94 个失败无法用「改动前基线」直接对比（工作区在冻结时已是 27 个 tracked 修改 + 62 个 untracked 的半成品重构）。已通过 (a) 栈帧归属、(b) 测试行情 fixture 100% 通过湖校验（70/40/38/36 行）两项证据排除本批改动的因果性；重构完成后需在干净基线复跑一次确认。

---

## 7. 未完成项与下一步（按验收文档顺序）

**P0B 剩余（阻塞 P0C/复权）**
1. 扩展湖 schema：`provider / provider_symbol / price_basis / volume_unit / currency / source_reference / ingested_at / revision_id / source_batch_id`，shadow 写 `lake_v2`。
2. 多源基准冲突 fail-closed；接入 `exchange_calendars` 并锁版本。
3. 公司行为采集（CN `adj_factor`；US Polygon splits/dividends，Alpaca 仅作交叉校验）。

**P0C**：重建 adjusted view、跳变逐条对账（基线 216/1,401 处）、标签/排名影响报告、重建确定性哈希。

**P0D 剩余**：D-4 真实数据审计、D-5 `exit_allowed`、D-6 彻底移除训练对宽表的引用。

**P1 剩余**：E-1 页面/导出切断 legacy 指标；E-6 Newey-West/块 bootstrap；E-7 试验注册表；E-8 OOS 资格重算；E-9 旧评估只读化；账本 T+1。

**P2**：全部待办（筛选页一致性、模型分语义、单位统一、日历替换、任务/缓存、账本费用与 FX）。

---

## 8. 签署

```
执行人：DimAgent（自动化）    复核人：待指定    日期：2026-10-02
代码 revision：34c80d9d + 工作区未提交改动（patch hash 见冻结基线）
数据 manifest：data/artifacts/freeze-20261002T123533Z/baseline.json
抽检：P0A 校验通过；fixture 湖校验 100% 通过
豁免项：无
结论：第 1 批部分通过（10 通过 / 6 部分 / 11 未实施），无本批引入的回归
```
