# Kronos K 线基础模型接入说明

日期：2026-05-03

## 目标定位

Kronos 不替代当前 LightGBM / 多模型共振，而是作为“候选股二次验证模型”使用：

- LightGBM / 多模型共振负责全市场快速排序；
- Kronos 只验证 Top 候选的未来 K 线路径；
- AI 日报优先展示 `多模型共振 + Kronos 支持` 的股票；
- 未配置 Kronos 运行环境时，主流程不失败，只生成待验证池。

## 已接入的应用能力

1. 新增 `kronos_validation` job。

2. 新增 `kronos_validation_snapshot` 工作台快照。

3. 收盘预计算尾部自动执行顺序调整为：

   行情刷新 / 模型训练 -> 模型预计算 -> 模型使用指导 -> 样本外校准 -> Kronos 验证 -> AI 日报。

4. AI 日报会读取最新 Kronos 验证快照，并在候选股中展示：

   - Kronos 状态；
   - Kronos 决策；
   - Kronos 分数；
   - 3 日预期收益；
   - 预测回撤；
   - 未配置原因。

5. 任务中心增加手动触发入口。

6. 设置页新增 `/settings/kronos`，用于查看：

   - Kronos 接入层是否启用；
   - runner 命令是否已配置；
   - Kronos repo 路径是否已配置；
   - 最新验证快照的候选数、已验证数、待配置数；
   - 推荐环境变量和独立运行环境安装命令。

## 当前运行状态

当前已完成本机接入：

- 使用 `uv` 下载独立 CPython 3.11；
- 使用 `.venv-kronos` 作为 Kronos / PyTorch 独立环境；
- 已安装 `torch==2.5.1`、`transformers`、`pandas`、`sentencepiece` 和 Kronos 官方 requirements；
- 已克隆 Kronos 源码到 `/Volumes/STORAGE_Jackyhu/code/Kronos`；
- 主应用已通过 `PQW_KRONOS_RUNNER_COMMAND` 调用外部 runner；
- 最新 20 只 A 股候选验证结果：`20/20 READY`，无历史不足跳过。

注意：主应用 `.venv` 仍然不安装 PyTorch，这是有意设计，避免大模型依赖污染 Web / Job 主进程。

## 推荐部署方式

不要把 PyTorch / Kronos 直接安装进当前主 `.venv`。建议新建隔离环境：

```bash
brew install uv
UV_CACHE_DIR=/Volumes/STORAGE_Jackyhu/code/ana/.uv-cache uv python install 3.11
UV_CACHE_DIR=/Volumes/STORAGE_Jackyhu/code/ana/.uv-cache uv venv .venv-kronos --python 3.11
UV_CACHE_DIR=/Volumes/STORAGE_Jackyhu/code/ana/.uv-cache UV_CONCURRENT_DOWNLOADS=1 uv pip install --python .venv-kronos/bin/python transformers huggingface_hub pandas sentencepiece numpy
UV_CACHE_DIR=/Volumes/STORAGE_Jackyhu/code/ana/.uv-cache UV_CONCURRENT_DOWNLOADS=1 uv pip install --python .venv-kronos/bin/python torch==2.5.1 accelerate
git clone https://github.com/shiyu-coder/Kronos /Volumes/STORAGE_Jackyhu/code/Kronos
UV_CACHE_DIR=/Volumes/STORAGE_Jackyhu/code/ana/.uv-cache UV_CONCURRENT_DOWNLOADS=1 uv pip install --python .venv-kronos/bin/python -r /Volumes/STORAGE_Jackyhu/code/Kronos/requirements.txt
```

不建议直接使用 Homebrew `python@3.11 / python@3.12` 创建 Kronos 环境。本机 macOS 26 上 Homebrew Python 的 `pyexpat` 会链接到系统 `/usr/lib/libexpat.1.dylib`，导致 `ensurepip` 报 `Symbol not found: _XML_SetAllocTrackerActivationThreshold`。`uv` 下载的独立 Python 可以绕开这个问题。

项目已经内置一个 runner 脚本：

```bash
scripts/kronos_runner.py
```

如果 Kronos repo 没有安装成 Python 包，可以通过 `PQW_KRONOS_REPO_PATH` 指向本地 Kronos 源码目录，让 runner 能够 `from model import Kronos, KronosTokenizer, KronosPredictor`。

主应用通过环境变量调用：

```bash
PQW_KRONOS_ENABLED=true
PQW_KRONOS_RUNNER_COMMAND="/Volumes/STORAGE_Jackyhu/code/ana/.venv-kronos/bin/python /Volumes/STORAGE_Jackyhu/code/ana/scripts/kronos_runner.py"
PQW_KRONOS_REPO_PATH="/Volumes/STORAGE_Jackyhu/code/Kronos"
PQW_KRONOS_MODEL_NAME="NeoQuasar/Kronos-mini"
PQW_KRONOS_DEVICE="cpu"
PQW_KRONOS_MIN_HISTORY=30
PQW_KRONOS_TEMPERATURE=0.8
PQW_KRONOS_TOP_P=0.9
PQW_KRONOS_SAMPLE_COUNT=3
PQW_KRONOS_SEED=42
```

当前 `PQW_KRONOS_MIN_HISTORY=30` 是因为本地 Parquet 行情湖从 4 月开始补数，日线历史还不够长。等 A 股/美股历史补到 60-120 根后，建议调回 `60` 或更高。

`PQW_KRONOS_SEED + PQW_KRONOS_SAMPLE_COUNT=3` 用于稳定采样式预测结果，避免同一批候选重复运行时结论明显漂移。

## Runner 输入输出契约

`scripts/kronos_runner.py` 已按下面的输入输出契约实现。后续如果要替换成更高性能的 runner，只要保持这个 JSON 契约不变即可。

Runner 内部会把同一批候选的历史 K 线自动裁剪到相同长度，因为 Kronos `predict_batch` 要求批量输入的上下文长度一致。

输入：

```json
{
  "model_name": "NeoQuasar/Kronos-mini",
  "device": "cpu",
  "horizon_days": 3,
  "candidates": [
    {
      "ticker": "000001.SZ",
      "market": "CN",
      "history": [
        {"date": "2026-04-01", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1000}
      ]
    }
  ]
}
```

输出：

```json
{
  "rows": [
    {
      "ticker": "000001.SZ",
      "kronos_score": 72.5,
      "expected_return_1d_pct": 1.2,
      "expected_return_3d_pct": 3.4,
      "max_drawdown_pct": -2.1,
      "decision": "Kronos 支持",
      "reason": "预测路径上行且回撤可控"
    }
  ]
}
```

## 验收标准

1. `/settings/kronos?lang=zh` 可以打开，并清楚显示当前是“已配置 / 待配置 / 已关闭”。

2. 未配置 runner 时，`/jobs/kronos-validation` 返回 `not_configured`，但不报 Internal Server Error。

3. 配置 runner 后，`kronos_validation_snapshot` 中至少有部分候选返回 `kronos_status=READY`。

4. AI 日报候选中能看到 Kronos 二次验证行。

5. 收盘主 job 即使 Kronos 未配置，也能继续生成 AI 日报。

6. 后续 Top 5 排序可以逐步提高 `Kronos 支持` 候选的权重，但不能在样本不足前完全依赖 Kronos。
