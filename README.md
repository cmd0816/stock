# 东方财富股票数据工具

从东方财富获取股票行情和历史 K 线，保存到 SQLite，并提供一个本地网页查看 K 线、均线、筹码图和条件选股结果。

---

## 目录

- [每天做什么（工作日）](#每天做什么工作日)
- [周末做什么](#周末做什么)
- [文件说明](#文件说明)
- [环境准备](#环境准备)
- [详细命令参考](#详细命令参考)
- [查看数据网页](#查看数据网页)
- [SQLite 表结构](#sqlite-表结构)
- [测试](#测试)
- [常见问题](#常见问题)

---

## 每天做什么（工作日）

> 目标：收盘后基于最新选股批次做规则重选 + ML 训练预测；若出现新批次，自动复盘上一次选股结果。

### 一键执行

```bash
./run_daily_update.sh
```

这条命令会自动：

1. 读取数据库中**最近一次条件选股批次**。
2. 把日期**对齐到中国 A 股最近交易日**（如遇节假日自动回退）。
3. 若检测到“新批次切换”，先补齐上一次已选的**全部股票**日 K，再自动复盘并沉淀为 ML 反馈标签。
4. 使用最新批次执行规则打分，生成当日 `run_id`。
5. 基于该最新 `run_id` 做 ML 训练和预测。

### 常用可选参数

```bash
# 指定更新日期（自动对齐到最近交易日）
UPDATE_DATE=2026-05-12 ./run_daily_update.sh

# 指定要更新的选股批次
XUANGU_BATCH_ID=20260508 ./run_daily_update.sh
```

`run_daily_update.sh` 不再自动执行东方财富选股下载；`xuangu_to_sqlite.py` 由用户按需手动执行（可一周一次或一天一次）。

### 可选：邮件通知

设置环境变量后，每次更新（成功或失败）自动发邮件：

```bash
DAILY_EMAIL_TO="you@example.com" \
SMTP_HOST="smtp.gmail.com" \
SMTP_PORT=465 \
SMTP_USER="you@example.com" \
SMTP_PASS="你的邮箱SMTP授权码" \
SMTP_USE_SSL=1 \
./run_daily_update.sh
```

---

## 周末做什么

> 目标：复盘上周、选股本周、生成下周 Top 股票列表。

### 完整流程（7 步）

| 步骤 | 内容 | 说明 |
|------|------|------|
| 1 | **复盘上周** | 计算上一期入选股票的一周表现（最高涨幅、收盘涨幅、最大回撤、是否止损） |
| 2 | **条件选股** | 打开东方财富条件选股页面，按 `screening.txt` 条件下载 Excel 并入库 |
| 3 | **批次日线刷新** | 对本次批次股票补齐日 K（`baostock` 优先，失败回退 `akshare`），并更新资金流/板块上下文 |
| 4 | **规则打分** | 对候选池股票打分，选出 Top 3~4 只 |
| 5 | **补齐入选股 K 线** | 下载/更新本周全部入选股票的一年日 K（用于复盘充分性） |
| 6 | **ML 回测** | 检查模型最近表现 |
| 7 | **ML 训练+预测** | 训练模型，为 Top 股票生成 ML 预测概率 |

### 一键执行

```bash
./run_weekly.sh
```

执行完成后启动网页查看结果：

```bash
./run_server.sh
```

打开：

- `http://127.0.0.1:8000/top` —— 最终 Top 股票列表和日 K 图
- `http://127.0.0.1:8000/screening` —— 选股、预测、复盘汇总页面

### 常用可选参数

```bash
# 跳过复盘（第一次使用或没有可复盘数据时）
SKIP_REVIEW=1 ./run_weekly.sh

# 跳过东方财富下载（已手动导入过 XLSX 和 K 线）
SKIP_XUANGU=1 ./run_weekly.sh

# 跳过批次日线刷新（会跳过 baostock/akshare 批次补K与上下文更新）
SKIP_BATCH_HISTORY=1 ./run_weekly.sh

# 跳过 ML 回测（只想快速生成本周预测）
SKIP_BACKTEST=1 ./run_weekly.sh

# 指定选股日期（自动对齐到最近交易日）
SCREEN_DATE=2026-05-08 ./run_weekly.sh

# 指定东方财富选股批次
XUANGU_BATCH_ID=20260508 ./run_weekly.sh

# 显示浏览器窗口
BROWSER_HEADED=1 ./run_weekly.sh

# 先手动登录东方财富
WAIT_LOGIN=1 ./run_weekly.sh

# 批次日线刷新请求间隔（秒）
HISTORY_DELAY=1.5 ./run_weekly.sh

# 批次日线只刷新前 N 只（测试）
HISTORY_LIMIT=20 ./run_weekly.sh

# 批次日线最小已存在天数阈值
HISTORY_MIN_EXISTING_DAYS=200 ./run_weekly.sh
```

### 关于 ML 预测

- ML 是**可选第二层**，只用于对规则选出的 Top 股票做概率重排，**不替代规则分数**。
- 默认 ML 关闭，如需开启，修改 `config/weekly_strategy.yaml`：

```yaml
ml:
  enabled: true
  model_name: lightgbm
```

- 建议先观察几周，对比规则 Top 和 ML 排序的复盘结果，再决定是否提高 ML 权重。

---

## 文件说明

| 文件/目录 | 说明 |
|-----------|------|
| `run_daily_update.sh` | **工作日一键流程**：新批次切换时复盘上一期 + 基于最新批次做 screen + ML 预测 |
| `run_weekly.sh` | **周末一键流程**（复盘+选股+批次补K+打分+ML） |
| `run_xuangu.sh` | 条件选股下载入库（被 `run_weekly.sh` 调用，也可单独用） |
| `run_server.sh` | 启动本地看盘网页服务 |
| `eastmoney_to_sqlite.py` | 导入单只股票最新行情 |
| `download_top_history_akshare.py` | AKShare 下载/更新一年日 K |
| `baostock_to_sqlite.py` | BaoStock 导入/更新 A 股日 K |
| `xuangu_to_sqlite.py` | 东方财富条件选股自动化（Playwright） |
| `view_quotes.py` | 本地网页看盘服务（被 `run_server.sh` 调用） |
| `daily_update_email.py` | 每日更新邮件发送脚本 |
| `weekly_stock/` | 周末选股、规则打分、ML 训练和复盘模块 |
| `weekly_stock_main.py` | 周末系统命令行入口 |
| `config/weekly_strategy.yaml` | 评分规则和 ML 配置 |
| `screening.txt` | 条件选股条件（每行或整行，脚本会读取完整内容） |
| `stocks.db` | SQLite 数据库 |
| `downloads/` | 选股 Excel 和日志保存目录 |
| `tests/` | 单元测试 |

---

## 环境准备

```bash
cd /Users/cmd/workspace/stock
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium firefox
```

依赖：

- Python 3.10+
- SQLite（Python 自带）
- Playwright（浏览器自动化）
- openpyxl（读取 Excel）
- baostock（日 K 导入）

---

## 详细命令参考

### 条件选股下载入库

编辑 `screening.txt`，例如：

```text
10日均线向上;20日均线向上;30日均线向上;60日均线向上;换手率3%-20%;排除ST;
```

一键运行：

```bash
./run_xuangu.sh
```

默认行为：

- 隐藏浏览器窗口，复用 Firefox 默认登录状态。
- 自动打开东方财富条件选股页面，输入条件，下载 Excel。
- 保存到 `downloads/xuangu_YYYYMMDD.xlsx` 并导入数据库。
- 自动下载这批股票的一年日 K。
- 当天同一批次已导入时会跳过选股下载，直接补 K 线。
- 结束后自动清理旧批次，只保留最新一批选股结果。

可选参数：

```bash
# 先登录再继续
WAIT_LOGIN=1 ./run_xuangu.sh

# 显示浏览器窗口
BROWSER_HEADED=1 ./run_xuangu.sh

# 跳过一年日 K 下载
./run_xuangu.sh --skip-history-1y

# 只下载前 5 只股票的一年 K
./run_xuangu.sh --history-limit 5

# 放慢请求间隔
./run_xuangu.sh --history-delay 3
```

导入已有 Excel（不打开浏览器）：

```bash
.venv/bin/python xuangu_to_sqlite.py \
  --import-xlsx "downloads/xuangu_20260503.xlsx" \
  --db stocks.db \
  --condition-file screening.txt \
  --batch-id 20260503 \
  --replace-existing
```

### 单独运行周末子命令

```bash
# 规则打分（使用最近一次候选池）
python3 weekly_stock_main.py screen

# 同时先跑东方财富条件选股
python3 weekly_stock_main.py screen --run-xuangu

# 指定日期或批次
python3 weekly_stock_main.py screen --date 2026-05-02 --xuangu-batch-id 20260502

# 复盘上一期（会先补齐该 run 全部入选股票的日 K）
python3 weekly_stock_main.py review

# 复盘指定 run
python3 weekly_stock_main.py review --run-id 1

# 查看历史 run 列表
python3 weekly_stock_main.py runs

# ML 预测最新 Top
python3 weekly_stock_main.py predict

# ML 预测指定 run
python3 weekly_stock_main.py predict --run-id 6

# ML 时间序列回测
python3 weekly_stock_main.py backtest

# 查看复盘趋势
python3 weekly_stock_main.py trend --limit 30 --window 6
```

### 导入/更新日 K 线

AKShare：

```bash
.venv/bin/python download_top_history_akshare.py \
  --db stocks.db --run-id 40 --top-n 50 \
  --target-date 2026-05-23 --days 365 --adjust qfq

# 下载指定 run 的全部入选股票
.venv/bin/python download_top_history_akshare.py \
  --db stocks.db --run-id 40 --all-selected \
  --target-date 2026-05-23 --days 365 --adjust qfq
```

BaoStock：

```bash
.venv/bin/python baostock_to_sqlite.py \
  --db stocks.db --batch-id 20260522 \
  --start-date 2026-05-11 --end-date 2026-05-22 --adjust qfq
```

只补缺失换手率：

```bash
.venv/bin/python baostock_to_sqlite.py \
  --db stocks.db --batch-id 20260522 \
  --start-date 2026-05-11 --end-date 2026-05-22 --adjust qfq --only-null-turnover
```

---

## 查看数据网页

```bash
./run_server.sh
```

访问：

- `http://127.0.0.1:8000/daily` —— 日 K 行情（含均线、筹码图）
- `http://127.0.0.1:8000/daily?code=301071` —— 指定股票
- `http://127.0.0.1:8000/top` —— 本周 Top 股票列表
- `http://127.0.0.1:8000/screening` —— 选股导入、生成 Top、复盘汇总

功能：

- 左侧股票列表，点击切换 K 线
- 日 K 蜡烛图 + 均线（MA5/10/20/30/60/120/250）
- 十字光标查看当天明细
- 支持缩放查看局部 K 线
- 筹码分布图随十字光标日期变化

---

## SQLite 表结构

**行情表：**

- `eastmoney_stock_quotes` —— 股票最新快照
- `eastmoney_stock_daily_klines` —— 日 K 历史数据

**条件选股表：**

- `xuangu_batches` —— 每次选股导入批次
- `xuangu_results` —— 选股 Excel 明细

**周末系统表：**

- `weekly_screen_runs` —— 每次周末选股运行
- `weekly_screen_candidates` —— 候选股票打分明细
- `weekly_selected_stocks` —— 最终入选股票
- `weekly_review_runs` / `weekly_review_results` —— 复盘运行和结果
- `weekly_ml_model_runs` / `weekly_ml_training_samples` / `weekly_ml_predictions` —— ML 训练与预测

---

## 测试

```bash
python3 -m unittest discover -s tests -v
```

---

## 常见问题

**日 K 下载失败？**
直接重试即可；脚本会优先使用 `stock_zh_a_hist`，失败时自动回退 `stock_zh_a_daily`。

**条件选股页面没有登录？**

```bash
WAIT_LOGIN=1 ./run_xuangu.sh
```

**选股条件和页面不一致？**
优先检查 `screening.txt` 里的中文分号、空格和条件文本。如需严格逐字校验：

```bash
./run_xuangu.sh --strict-condition-match
```

**下载弹窗已打开但没下载？**
脚本会继续等待，也可以手动点击橙色 `下载` 按钮，脚本会捕获下载事件并继续。
