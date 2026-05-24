# 东方财富股票数据工具

这个项目用于从东方财富获取股票行情和历史 K 线，保存到 SQLite，并提供一个本地网页查看 K 线、均线、筹码图和条件选股结果。

## 文件说明

- `eastmoney_to_sqlite.py`：导入单只股票的最新行情。
- `download_top_history_akshare.py`：使用 AKShare 下载/更新一年日 K（`stock_zh_a_hist` 失败自动回退 `stock_zh_a_daily`）。
- `baostock_to_sqlite.py`：使用 BaoStock 导入/更新 A 股日 K（支持按批次、按代码、按日期范围、仅补缺失换手率）。
- `xuangu_to_sqlite.py`：打开东方财富条件选股页面，输入 `screening.txt` 条件，下载 Excel，并导入 SQLite。
- `view_quotes.py`：本地网页看盘服务。
- `run_xuangu.sh`：一键运行条件选股下载入库。
- `run_server.sh`：一键启动本地看盘网页。
- `run_daily_update.sh`：工作日收盘后更新最近一批选股的日 K，可选邮件通知。
- `daily_update_email.py`：daily update 邮件发送脚本（由 `run_daily_update.sh` 调用）。
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
- baostock，用于 BaoStock 日 K 导入

## 导入股票行情

导入最新快照：

```bash
python3 eastmoney_to_sqlite.py \
  --url "https://quote.eastmoney.com/concept/sh688343.html" \
  --db stocks.db
```

下载一年日 K 线（AKShare）：

```bash
.venv/bin/python download_top_history_akshare.py \
  --db stocks.db \
  --run-id 40 \
  --top-n 50 \
  --target-date 2026-05-23 \
  --days 365 \
  --adjust qfq
```

下载/更新日 K 线（BaoStock）：

```bash
.venv/bin/python baostock_to_sqlite.py \
  --db stocks.db \
  --batch-id 20260522 \
  --start-date 2026-05-11 \
  --end-date 2026-05-22 \
  --adjust qfq
```

只补指定日期范围内 `turnover_rate` 为空的股票：

```bash
.venv/bin/python baostock_to_sqlite.py \
  --db stocks.db \
  --batch-id 20260522 \
  --start-date 2026-05-11 \
  --end-date 2026-05-22 \
  --adjust qfq \
  --only-null-turnover
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

一年日 K 默认每只股票间隔 `1.5` 秒。如果遇到连接不稳定，可以放慢：

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

### 每日更新最近一批选股日 K

工作日收盘后，如果只想更新最近一批条件选股股票的日 K，不重新选股、不重新导入 XLSX，可以运行：

```bash
./run_daily_update.sh
```

默认行为：

- 自动读取 `stocks.db` 中最近一次条件选股批次。
- 自动把 `UPDATE_DATE` 对齐到中国 A 股最近交易日。
- 只更新这批股票的一年日 K。
- 已经更新到目标交易日的股票会自动跳过。
- 默认隐藏 Firefox 窗口，复用 Firefox 默认登录状态。

常用可选参数：

```bash
# 指定更新日期，脚本会自动对齐到最近交易日
UPDATE_DATE=2026-05-12 ./run_daily_update.sh

# 指定要更新的选股批次
XUANGU_BATCH_ID=20260508 ./run_daily_update.sh

# 放慢请求，降低请求频率
HISTORY_DELAY=3 ./run_daily_update.sh

# 只更新前 10 只，方便测试
HISTORY_LIMIT=10 ./run_daily_update.sh

# 显示浏览器窗口
BROWSER_HEADED=1 ./run_daily_update.sh

# 需要先手动登录东方财富
WAIT_LOGIN=1 ./run_daily_update.sh
```

可选：把 daily update 统计自动发到邮箱（更新失败也会发）：

```bash
DAILY_EMAIL_TO="you@example.com" \
SMTP_HOST="smtp.gmail.com" \
SMTP_PORT=465 \
SMTP_USER="you@example.com" \
SMTP_PASS="你的邮箱SMTP授权码" \
SMTP_USE_SSL=1 \
./run_daily_update.sh
```

说明：

- 只有设置 `DAILY_EMAIL_TO` 才会发邮件；不设置时行为和以前一致。
- 邮件内容包含：目标日期、对齐交易日、批次号、`stocks_ok`、`rows_saved`、失败数、批次覆盖度、最近两次周末 Top 的变动列表（新增/移除/排名升降）和日志尾部。
- 日志会保存到 `downloads/logs/daily_update_*.log`。
- `daily_update_email.py` 统一使用环境变量读取上下文：`DAILY_UPDATE_DB_PATH`、`DAILY_UPDATE_BATCH_ID`、`DAILY_UPDATE_TARGET_DATE`、`DAILY_UPDATE_ALIGNED_DATE`、`DAILY_UPDATE_STATUS`、`DAILY_UPDATE_LOG_FILE`（由 `run_daily_update.sh` 自动注入）。
- 常用可选项：`DAILY_EMAIL_FROM`、`SMTP_STARTTLS=1`（非 SSL 端口时）、`DAILY_EMAIL_SUBJECT_PREFIX`、`DAILY_EMAIL_LOG_LINES`。

如果要单独测试邮件脚本，可以手动设置这些变量后运行：

```bash
DAILY_UPDATE_DB_PATH="/Users/cmd/workspace/stock/stocks.db" \
DAILY_UPDATE_BATCH_ID="20260508" \
DAILY_UPDATE_TARGET_DATE="2026-05-09" \
DAILY_UPDATE_ALIGNED_DATE="2026-05-08" \
DAILY_UPDATE_STATUS="0" \
DAILY_UPDATE_LOG_FILE="/Users/cmd/workspace/stock/downloads/logs/your_log.log" \
DAILY_EMAIL_TO="you@example.com" \
SMTP_HOST="smtp.gmail.com" \
SMTP_PORT=465 \
SMTP_USER="you@example.com" \
SMTP_PASS="你的邮箱SMTP授权码" \
SMTP_USE_SSL=1 \
python3 daily_update_email.py
```

## 周末选股、预测和复盘系统

默认先使用规则打分和排序。ML 预测是可选的第二层，用来对已经选出的 Top 股票做本地概率重排。

### 每周必须做的事

每周末建议固定按这个顺序执行：

1. 复盘上一周入选股票。
2. 下载/导入本周东方财富条件选股结果。
3. 根据本周候选池补齐一年日 K 数据。
4. 把选股日期对齐到中国 A 股本周最后一个交易日。
5. 生成本周规则 Top 股票。
6. 运行 ML 时间序列回测，检查模型最近表现。
7. 训练 `LogisticRegression` baseline 和 `LightGBM` 主模型。
8. 生成本周 Top 股票的 ML 预测概率。
9. 打开 `/top` 查看最终列表和日 K 图。

一键执行：

```bash
./run_weekly.sh
```

执行完成后打开：

```bash
./run_server.sh
```

```text
http://127.0.0.1:8000/top
```

常用可选参数：

```bash
# 如果本周还没有可复盘对象，跳过复盘
SKIP_REVIEW=1 ./run_weekly.sh

# 如果已经手动导入 XLSX 和 K 线，跳过东方财富下载
SKIP_XUANGU=1 ./run_weekly.sh

# 如果只想快速生成本周预测，跳过 ML 回测
SKIP_BACKTEST=1 ./run_weekly.sh

# 指定选股日期
SCREEN_DATE=2026-05-08 ./run_weekly.sh

# 指定东方财富选股批次
XUANGU_BATCH_ID=20260508 ./run_weekly.sh

# 显示浏览器窗口，便于登录或观察下载
BROWSER_HEADED=1 ./run_weekly.sh

# 需要先手动登录东方财富
WAIT_LOGIN=1 ./run_weekly.sh
```

如果 `review` 提示没有可复盘对象，脚本会继续后面的本周选股流程；这通常发生在第一次使用，或者上一周数据还不足时。

交易日期说明：

- 系统会把 `SCREEN_DATE` 对齐到中国 A 股最近一个交易日。
- 周末运行时，例如周六 `2026-05-09`，会自动对齐到周五 `2026-05-08`。
- ML 训练样本默认只取每周最后一个交易日，避免周中数据和周末选股口径不一致。
- 交易日历优先参考 AKShare 的 `tool_trade_date_hist_sina` 接口；如果本地没有安装 AKShare 或接口不可用，会自动使用数据库中已下载的 A 股 K 线交易日作为 fallback。
- 如果想使用 AKShare 官方交易日历，可以额外安装：

```bash
pip install akshare
```

规则流程是：

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
  ml.py          # 本地 ML 特征、训练和预测
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

查看历史 Top 列表的 `run-id`：

```bash
python3 weekly_stock_main.py runs
```

输出示例：

```text
run_id screen_date  batch_id  candidates selected ml_predictions model     created_at
     9 2026-05-08   20260508         259        8              8   lightgbm 2026-05-09T04:54:54...
```

这里第一列 `run_id` 就是历史 Top 列表编号。平时每周运行 `./run_weekly.sh` 不需要手动填写它；只有想复盘或重新预测某个历史 Top 列表时才需要。

复盘会计算：

- 下周最高涨幅
- 下周收盘涨幅
- 最大回撤
- 是否触发止损
- 是否符合预期

### 本地 ML 预测

第一版 ML 不联网、不调用外部 API，也不替代规则分数。它会：

- 使用已下载的一年日 K 数据生成历史训练样本。
- 每个样本用过去 60 个交易日计算特征。
- 用未来 5 个交易日表现生成标签。
- 训练一个 `LogisticRegression` baseline 模型。
- 训练一个 `LightGBM` 主模型。
- 对当前 Top 股票输出 `probability_up` 和 `predicted_score`。
- 保存到数据库，`/top` 左侧会显示类似 `ML 38%`。

运行最新 Top 列表的 ML 预测：

```bash
python3 weekly_stock_main.py predict
```

指定某次 Top 运行：

```bash
python3 weekly_stock_main.py predict --run-id 6
```

也可以在网页里操作：

```text
http://127.0.0.1:8000/screening
```

点击“生成下周 Top 股票（含 ML）”，再打开：

```text
http://127.0.0.1:8000/top
```

ML 配置在 `config/weekly_strategy.yaml` 的 `ml` 部分：

- `model_name`：主模型，默认 `lightgbm`。
- `baseline_model_name`：baseline，默认 `logistic_regression`。
- `lookback_trading_days`：用多少个历史交易日计算特征。
- `horizon_trading_days`：预测未来几个交易日。
- `weekly_last_trading_day_only`：是否只用每周最后一个交易日生成训练样本。
- `positive_high_gain_pct`：未来最高涨幅达到多少算正样本。
- `positive_close_gain_pct`：按卖出规则退出后的收益最低要求。
- `use_trade_exit_rules`：是否启用交易规则标签（默认 `true`）。
- `exit_stop_loss_pct`：止损阈值，默认 `0.10`（跌 10% 卖出）。
- `exit_on_break_ma20`：是否启用跌破 MA20 卖出，默认 `true`。
- `negative_drawdown_pct`：仅在关闭 `use_trade_exit_rules` 时使用的旧标签阈值。
- `rule_score_weight`：最终预测分数中保留多少规则分权重。
- `use_review_feedback_labels`：是否把复盘结果（`weekly_review_results.meets_expectation`）反哺为训练标签，默认 `false`。
- `review_feedback_weight`：反哺标签命中样本的权重倍数（通过样本复制实现），默认 `1`。
- `review_feedback_recent_runs`：只使用最近 N 次复盘运行做反哺，`0` 表示不限制（使用所有可用复盘），默认 `0`。
- `min_train_samples`：最少训练样本数，样本不足会停止。
- `backtest_train_ratio`：时间序列回测时前多少比例样本用于训练。
- `backtest_top_k`：回测时统计概率最高的前多少个样本。

交易日历配置在 `config/weekly_strategy.yaml` 的 `calendar` 部分：

- `align_to_china_trading_day`：是否把选股和复盘日期对齐到中国 A 股交易日。
- `prefer_akshare`：是否优先尝试 AKShare 交易日历，失败时自动回退到数据库 K 线交易日。

训练样本会保存到：

```text
weekly_ml_training_samples
```

每条样本包含：

- `model_run_id`
- 股票代码
- 样本交易日
- 标签 `label`
- 未来最高涨幅
- 未来收盘涨幅
- 未来最大回撤
- 特征 JSON

运行 ML 时间序列回测：

```bash
python3 weekly_stock_main.py backtest
```

它会按时间切分训练集和测试集，输出：

- `LogisticRegression` baseline 指标。
- `LightGBM` 主模型指标。
- accuracy、precision、recall。
- Top K 命中率。
- Top K 平均未来收益和最大回撤。

查看按周复盘趋势（判断是否“越做越好”）：

```bash
python3 weekly_stock_main.py trend
```

常用参数：

```bash
# 最近 30 次复盘运行
python3 weekly_stock_main.py trend --limit 30

# 用 6 周窗口对比最近窗口 vs 上一个窗口
python3 weekly_stock_main.py trend --window 6
```

`trend` 会输出每次 run 的命中率、止损率、平均收盘涨幅/最高涨幅/最大回撤，
并给出滚动窗口对比，例如 `hit`、`avg_close`、`avg_drawdown` 的变化值。

如果只是想在没有额外 ML 依赖时验证流程，可以临时把模型改为：

```yaml
ml:
  model_name: centroid_v1
```

注意：ML 概率不是确定性结论，只用于辅助排序。先观察几周，让它和规则 Top 的复盘结果对比，再决定是否提高权重。

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
- `weekly_ml_model_runs`：每次 ML 训练运行。
- `weekly_ml_training_samples`：每次 ML 训练生成的历史样本和标签。
- `weekly_ml_predictions`：每只 Top 股票的 ML 预测概率和解释。

## 选股页面

启动数据看板后打开：

- `http://127.0.0.1:8000/screening`
- `http://127.0.0.1:8000/api/screening`

页面显示这 5 项：

- 导入条件选股 XLSX：把已下载的东方财富选股 Excel 导入数据库，可覆盖同日批次。
- 最近条件选股批次：批次、导入时间、行数、Excel 文件、查看股票、删除。
- 周末入选股票：在页面输入选股日期和 Top N，点击“生成下周 Top 股票（含 ML）”；同一选股日期和批次会覆盖旧结果，只保留最后一次；这一步会自动训练模型并生成 ML 概率；也可以展开查看本批次候选股票列表。
- ML 预测：结果显示在 Top 日线图左侧，无需再次点击单独按钮。
- 最近复盘结果：最高涨幅、收盘涨幅、最大回撤、止损和是否符合预期。

## 测试

运行单元测试：

```bash
python3 -m unittest discover -s tests -v
```

## 常见问题

如果一年日 K 下载失败，直接重试即可；脚本会优先使用 `stock_zh_a_hist`，失败时自动回退 `stock_zh_a_daily`。

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
