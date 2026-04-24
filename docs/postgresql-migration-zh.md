# PostgreSQL 切换步骤

这份说明按“先准备、再迁移、最后切流量”的顺序写，尽量把风险压低。

## 1. 安装 PostgreSQL

如果你是 macOS + Homebrew：

```bash
brew install postgresql@17
brew services start postgresql@17
```

确认 PostgreSQL 已启动：

```bash
pg_isready
```

## 2. 创建数据库用户和数据库

```bash
createuser ana_user --pwprompt
createdb ana_prod -O ana_user
```

建议先用一个独立用户，不要直接用 `postgres` 超级用户跑应用。

## 3. 安装应用依赖

项目现在已经把 PostgreSQL 驱动加进核心依赖了，重新装一次即可：

```bash
.venv/bin/pip install -r requirements.txt
```

## 4. 备份当前 SQLite

先备份现有数据库文件，默认一般是：

```text
storage/app.db
```

例如：

```bash
cp storage/app.db storage/app.db.bak-$(date +%Y%m%d-%H%M%S)
```

## 5. 配置 PostgreSQL 连接串

在 `.env` 里加入：

```env
PQW_DATABASE_URL=postgresql+psycopg://ana_user:你的密码@127.0.0.1:5432/ana_prod
```

说明：

- 现在代码会优先使用 `PQW_DATABASE_URL`
- 如果不配，才会继续使用 SQLite

## 6. 执行数据迁移

项目现在已经内置了一次性迁移脚本：

```bash
.venv/bin/python scripts/migrate_database.py \
  --source-url sqlite:////Volumes/STORAGE_Jackyhu/code/ana/storage/app.db \
  --target-url postgresql+psycopg://ana_user:你的密码@127.0.0.1:5432/ana_prod
```

如果目标 PostgreSQL 库里已经有旧数据，想先清空再覆盖，可以加：

```bash
--truncate-target
```

## 7. 做一轮迁移校验

迁移完成后，建议先比对核心表行数：

```bash
.venv/bin/python scripts/migrate_database.py \
  --mode validate \
  --source-url sqlite:////Volumes/STORAGE_Jackyhu/code/ana/storage/app.db \
  --target-url postgresql+psycopg://ana_user:你的密码@127.0.0.1:5432/ana_prod
```

如果输出里都是 `OK`，说明至少表级行数是一致的。

## 8. 重启应用

让应用重新加载新的环境变量和数据库连接：

```bash
launchctl kickstart -k gui/$(id -u)/com.pqw.ana-app
```

## 9. 先做健康检查

```bash
curl -s http://127.0.0.1:8000/health
```

预期：

```json
{"status":"ok"}
```

## 10. 做一轮烟测

建议至少检查这些页面：

- `/dashboard`
- `/watchlist`
- `/dashboard/ai-daily-report`
- `/dashboard/ops/jobs`

另外建议顺手触发一次轻量任务：

- A 股最近行情刷新
- AI 日报生成

确认：

- 页面能打开
- 最近任务状态能正常显示
- Telegram 推送仍然可用

## 11. 观察半天到一天

切完后重点看这几类问题：

- 页面是否还有数据库锁等待
- job 状态写入是否正常
- 收盘复盘是否稳定完成
- 自选股和 dashboard 是否比 SQLite 更稳

## 12. 确认稳定后再停用 SQLite

不要一切完就删旧库。建议保留 SQLite 备份至少几天，等确认：

- A 股行情增量刷新正常
- 收盘复盘正常
- AI 日报正常
- Telegram 推送正常

再决定是否把旧 SQLite 文件移到归档目录。

## 当前推荐的 PostgreSQL 运行参数

这套应用目前推荐先用下面这组参数：

```env
PQW_POSTGRES_POOL_SIZE=20
PQW_POSTGRES_MAX_OVERFLOW=20
PQW_POSTGRES_POOL_TIMEOUT_SECONDS=30
PQW_POSTGRES_POOL_RECYCLE_SECONDS=1800
PQW_POSTGRES_CONNECT_TIMEOUT_SECONDS=10
PQW_POSTGRES_STATEMENT_TIMEOUT_MS=60000
PQW_POSTGRES_IDLE_TRANSACTION_TIMEOUT_MS=60000
PQW_POSTGRES_APPLICATION_NAME=pqw-app
```

这组参数更适合当前这个场景：

- 单机部署
- 页面读请求 + 后台 job 并存
- 收盘复盘和 AI 分析会产生较长查询

如果后面你发现：

- 页面并发明显变多
- 后台 job 明显变多
- 有更多外部用户同时在线

再考虑把 `POOL_SIZE` 往上调。

## 如何回滚到 SQLite

如果你后面需要临时回滚到 SQLite，最简单的方式是：

1. 注释或删除 `.env` 里的：

```env
PQW_DATABASE_URL=...
```

2. 保留原来的 SQLite 文件：

```text
storage/app.db
```

如果你已经做过备份，也可以先确认：

```text
storage/app.db.bak-postgres-cutover-20260411
```

3. 重启应用：

```bash
launchctl kickstart -k gui/$(id -u)/com.pqw.ana-app
```

4. 再做一轮健康检查和页面烟测：

- `/health`
- `/dashboard`
- `/watchlist`
- `/dashboard/ops/jobs`

## 建议的切换窗口

最稳妥的是：

- 盘后或周末执行迁移
- 迁移后先手动跑一轮 job
- 第二天再观察自动收盘任务

这样风险最低。
