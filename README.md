# 东方财富股票数据工具

这个项目用于从东方财富获取股票行情和历史 K 线，保存到 SQLite，并提供一个本地网页查看 K 线、均线、筹码图和条件选股结果。

## 文件说明

- `eastmoney_to_sqlite.py`：导入单只股票的最新行情和一年历史 K 线。
- `xuangu_to_sqlite.py`：打开东方财富条件选股页面，输入 `screening.txt` 条件，下载 Excel，并导入 SQLite。
- `view_quotes.py`：本地网页看盘服务。
- `run_xuangu.sh`：一键运行条件选股下载入库。
- `run_server.sh`：一键启动本地看盘网页。
- `weekly_stock/`：周末选股、规则打分和复盘模块。
- `weekly_stock_main.py`：周末选股系统命令行入口。
- `config/weekly_strategy.yaml`：周末选股评分和复盘配置。
- `tests/`：单元测试。
- `requirements.txt`：Python 依赖。
- `stocks.db`：SQLite 数据库，运行脚本后自动创建或更新。
- `downloads/`：选股 Excel 和调试截图保存目录。

## 环境准备

建议使用项目里的 `.venv`：

```bash
cd /Users/cmd/workspace/stock
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium firefox
```

依赖包括：

- Python 3.10+
- SQLite，Python 自带
- Playwright，用于浏览器模式抓取和条件选股自动化
- openpyxl，用于读取选股下载的 Excel

## 导入股票行情

导入最新快照：

```bash
python3 eastmoney_to_sqlite.py \
  --url "https://quote.eastmoney.com/concept/sh688343.html" \
  --db stocks.db
```

导入一年日 K 线，优先直连接口：

```bash
python3 eastmoney_to_sqlite.py \
  --url "https://quote.eastmoney.com/concept/sz301071.html" \
  --db stocks.db \
  --history-1y
```

如果直连接口被东方财富拦截，可以用浏览器模式：

```bash
/Users/cmd/workspace/stock/.venv/bin/python eastmoney_to_sqlite.py \
  --url "https://quote.eastmoney.com/concept/sz301071.html" \
  --db stocks.db \
  --history-1y-browser \
  --browser-engine firefox \
  --browser-headed \
  --browser-wait-login
```

## 条件选股下载入库

先创建或编辑 `screening.txt`，每行或一整行都可以，脚本会读取完整内容作为选股条件。

示例：

```text
10日均线向上;20日均线向上;30日均线向上;60日均线向上;换手率3%-20%;排除ST;
```

一键运行：

```bash
./run_xuangu.sh
```

默认行为：

- 使用 Firefox。
- 自动读取 Firefox 默认 profile，复用你已经登录的东方财富状态。
- 打开 `https://xuangu.eastmoney.com/`。
- 将 `screening.txt` 的内容输入条件选股框。
- 点击 `去选股` 或 `更新选股结果`。
- 点击下载弹窗里的橙色 `下载`。
- 把下载的 `.xlsx` 导入 `stocks.db` 的选股表。

如果需要先手动登录，再继续运行：

```bash
WAIT_LOGIN=1 ./run_xuangu.sh
```

脚本会停在登录提示处，登录完成后回到终端按 Enter 继续。

## 周末选股和复盘系统

第一版不使用 ML，只做规则打分和排序。流程是：

- 周末先运行东方财富条件选股，得到候选池。
- 从最新候选池读取股票。
- 结合已有 K 线和选股 Excel 字段计算 score。
- 按分数选出 Top 3 到 4。
- 保存候选分数、入选股票和 `selected_reason`。
- 下个周末运行复盘任务，计算上一期入选股票的一周表现。

项目结构：

```text
weekly_stock/
  config.py      # YAML 配置读取
  models.py      # Python 数据模型
  db.py          # SQLite 表结构和读写
  scoring.py     # 可解释规则打分
  jobs.py        # stock_screen_job 和 weekly_review_job
  cli.py         # 命令行入口
config/
  weekly_strategy.yaml
tests/
  test_weekly_stock.py
```

评分配置在 `config/weekly_strategy.yaml`：

- 趋势强度：30 分
- 量能/换手率：20 分
- 突破/创新高：20 分
- 基本面增长：15 分
- 风险过滤：15 分

运行周末选股打分，使用最近一次 `xuangu_batches` 候选池：

```bash
python3 weekly_stock_main.py screen
```

如果希望命令同时先跑东方财富条件选股下载：

```bash
python3 weekly_stock_main.py screen --run-xuangu
```

指定日期或选股批次：

```bash
python3 weekly_stock_main.py screen \
  --date 2026-05-02 \
  --xuangu-batch-id 20260502113843
```

运行上一期入选股票复盘：

```bash
python3 weekly_stock_main.py review
```

指定某次周末选股运行做复盘：

```bash
python3 weekly_stock_main.py review --run-id 1
```

复盘会计算：

- 下周最高涨幅
- 下周收盘涨幅
- 最大回撤
- 是否触发止损
- 是否符合预期

## 查看数据网页

启动看盘服务：

```bash
./run_server.sh
```

打开：

- `http://127.0.0.1:8000/daily`
- `http://127.0.0.1:8000/daily?code=301071`
- `http://127.0.0.1:8000/daily?code=688343`

网页功能：

- 左侧股票列表。
- 点击股票切换 K 线。
- 日 K 蜡烛图。
- 均线：`MA5`、`MA10`、`MA20`、`MA30`、`MA60`、`MA120`、`MA250`。
- 十字光标查看当天明细。
- 支持缩放查看局部 K 线。
- 筹码分布图随十字光标日期变化。

## SQLite 表

行情表：

- `eastmoney_stock_quotes`：股票最新快照。
- `eastmoney_stock_daily_klines`：日 K 历史数据，按 `code + trade_date` 更新。

条件选股表：

- `xuangu_batches`：每次选股导入批次。
- `xuangu_results`：选股 Excel 的明细行，包含股票代码、股票名称和原始行 JSON。

周末系统表：

- `weekly_screen_runs`：每次周末选股运行。
- `weekly_screen_candidates`：候选股票打分明细。
- `weekly_selected_stocks`：最终入选 Top 3 到 4。
- `weekly_review_runs`：每次复盘运行。
- `weekly_review_results`：每只入选股票的复盘结果。

## 检查数据

查看某只股票的一年 K 线是否导入：

```bash
sqlite3 stocks.db \
"SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM eastmoney_stock_daily_klines WHERE code='301071';"
```

查看最近一次条件选股导入：

```bash
sqlite3 stocks.db \
"SELECT batch_id, imported_at_utc, row_count, xlsx_path FROM xuangu_batches ORDER BY imported_at_utc DESC LIMIT 5;"
```

查看最近一次选股结果：

```bash
sqlite3 stocks.db \
"SELECT stock_code, stock_name FROM xuangu_results WHERE batch_id=(SELECT batch_id FROM xuangu_batches ORDER BY imported_at_utc DESC LIMIT 1) LIMIT 20;"
```

查看最近一次周末入选股票：

```bash
sqlite3 stocks.db \
"SELECT screen_date, code, name, rank_no, total_score, selected_reason FROM weekly_selected_stocks ORDER BY id DESC LIMIT 10;"
```

查看最近一次复盘：

```bash
sqlite3 stocks.db \
"SELECT code, name, highest_gain_pct, close_gain_pct, max_drawdown_pct, stop_loss_triggered, meets_expectation, notes FROM weekly_review_results ORDER BY id DESC LIMIT 10;"
```

## 测试

运行单元测试：

```bash
python3 -m unittest discover -s tests -v
```

## 常见问题

如果 `--history-1y` 报 `socket hang up`、`Empty reply` 或连接被关闭，改用 `--history-1y-browser`。

如果条件选股页面没有登录，用：

```bash
WAIT_LOGIN=1 ./run_xuangu.sh
```

如果 `screening.txt` 内容和页面里的条件不一致，脚本会停止，不会继续下载错误结果。优先检查 `screening.txt` 里的中文分号、空格和条件文本是否符合东方财富页面识别方式。

如果下载按钮已经打开弹窗但没有下载，脚本会继续等待。也可以在弹窗里手动点击橙色 `下载`，脚本会捕获下载事件并继续导入。
