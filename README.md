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
- 默认隐藏浏览器窗口运行。
- 自动读取 Firefox 默认用户配置，复用你已经登录的东方财富状态。
- 打开 `https://xuangu.eastmoney.com/`。
- 将 `screening.txt` 的内容输入条件选股框。
- 点击 `去选股` 或 `更新选股结果`。
- 点击下载弹窗里的橙色 `下载`。
- 下载文件保存为 `downloads/xuangu_YYYYMMDD.xlsx`，例如 `downloads/xuangu_20260503.xlsx`。
- 把下载的 `.xlsx` 导入 `stocks.db` 的选股表。
- 导入完成后，会按本次选股列表自动下载一年以内的日 K 数据并保存到数据库。
- 如果一年日 K 下载中途失败，直接重新运行脚本即可，已有足够 K 线的股票会自动跳过，只补缺失的。
- 导入批次号使用当天日期，格式为 `YYYYMMDD`，例如 `20260502`。
- 如果当天批次已经导入并识别出股票代码，脚本会跳过选股下载和 Excel 导入，直接继续下载一年日 K。
- 同一天重新选股导入时，可以在选股页面勾选覆盖旧批次后重新导入。

如果需要先手动登录，再继续运行：

```bash
WAIT_LOGIN=1 ./run_xuangu.sh
```

脚本会停在登录提示处，登录完成后回到终端按回车继续。

如果想观察自动化过程，可以显示浏览器窗口：

```bash
BROWSER_HEADED=1 ./run_xuangu.sh
```

如果只想测试选股下载和 Excel 导入，暂时跳过一年日 K 下载：

```bash
./run_xuangu.sh --skip-history-1y
```

如果只想先下载前几只股票的一年日 K：

```bash
./run_xuangu.sh --history-limit 5
```

一年日 K 默认每只股票间隔 `1.5` 秒，减少东方财富接口重置连接。如果遇到大量 `socket hang up`，可以放慢：

```bash
./run_xuangu.sh --history-delay 3
```

如果已经下载好了 `.xlsx`，不想重新打开浏览器，可以直接导入已有文件：

```bash
.venv/bin/python xuangu_to_sqlite.py \
  --import-xlsx "downloads/你的选股结果.xlsx" \
  --db stocks.db \
  --condition-file screening.txt \
  --batch-id 20260503 \
  --replace-existing
```

也可以打开 dashboard：

```bash
./run_server.sh
```

进入 `http://127.0.0.1:8000/screening`，在“导入条件选股 XLSX”里点击“选择 XLSX 文件”后导入。

## 周末选股和复盘系统

第一版不使用机器学习，只做规则打分和排序。流程是：

- 周末先运行东方财富条件选股，得到候选池。
- 从最新候选池读取股票。
- 结合已有 K 线和选股 Excel 字段计算分数。
- 按分数选出前 3 到 4 只。
- 保存候选分数、入选股票和入选原因。
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
- `weekly_selected_stocks`：最终入选前 3 到 4 只股票。
- `weekly_review_runs`：每次复盘运行。
- `weekly_review_results`：每只入选股票的复盘结果。

## 选股页面

启动数据看板后打开：

- `http://127.0.0.1:8000/screening`
- `http://127.0.0.1:8000/api/screening`

页面只显示这 4 项：

- 导入条件选股 XLSX：把已下载的东方财富选股 Excel 导入数据库，可覆盖同日批次。
- 最近条件选股批次：批次、导入时间、行数、Excel 文件、查看股票、删除。
- 周末入选股票：在页面输入选股日期和 Top N，点击生成下周 Top 股票；同一选股日期和批次会覆盖旧结果，只保留最后一次；也可以展开查看本批次候选股票列表。
- 最近复盘结果：最高涨幅、收盘涨幅、最大回撤、止损和是否符合预期。

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

这个模式会显示浏览器窗口，方便手动登录。

如果 `screening.txt` 内容和页面里的条件不一致，脚本会停止，不会继续下载错误结果。优先检查 `screening.txt` 里的中文分号、空格和条件文本是否符合东方财富页面识别方式。

默认会按分号拆分选股条件，检查页面是否覆盖这些条件项。东方财富页面可能会规范化空格和标点，所以默认不要求整串逐字一致。如果需要严格逐字校验，可以运行：

```bash
./run_xuangu.sh --strict-condition-match
```

如果下载按钮已经打开弹窗但没有下载，脚本会继续等待。也可以在弹窗里手动点击橙色 `下载`，脚本会捕获下载事件并继续导入。
