# 东方财富股票数据工具

本项目把东方财富条件选股、A 股日 K、周末 Top 选股、复盘和本地看盘页串成一个 SQLite 工作流。

## 快速开始

```bash
cd /Users/cmd/workspace/stock
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium firefox
```

准备条件选股文本：

```bash
$EDITOR screening.txt
```

启动网页：

```bash
./run_server.sh
```

常用页面：

- `http://127.0.0.1:8000/top`：Top 股票和日 K 图
- `http://127.0.0.1:8000/screening`：批次导入、Top、复盘汇总
- `http://127.0.0.1:8000/reviews`：复盘历史
- `http://127.0.0.1:8000/daily`：日 K 浏览

## 工作日

日更默认只补 K 线并预览 Top N，不写入周末 Top 表，也不自动复盘，避免产生未满 5 个交易日的提前复盘记录。

```bash
./run_daily_update.sh
```

常用开关：

```bash
# 指定更新日期，会自动对齐到最近 A 股交易日
UPDATE_DATE=2026-05-12 ./run_daily_update.sh

# 指定条件选股批次
XUANGU_BATCH_ID=20260704 ./run_daily_update.sh

# 恢复旧行为：日更也落库 Top、训练 ML，并允许新批次切换时复盘上一期
DAILY_PERSIST_TOP=1 DAILY_REVIEW_PREVIOUS=1 ./run_daily_update.sh
```

## 周末

周末流程用于正式落库：复盘上一期、下载/导入条件选股、补 K 线、规则打分、ML 回测和预测。

```bash
./run_weekly.sh
```

常用开关：

```bash
SKIP_REVIEW=1 ./run_weekly.sh
SKIP_XUANGU=1 ./run_weekly.sh
SKIP_BATCH_HISTORY=1 ./run_weekly.sh
SKIP_BACKTEST=1 ./run_weekly.sh
SCREEN_DATE=2026-07-03 ./run_weekly.sh
XUANGU_BATCH_ID=20260704 ./run_weekly.sh
WAIT_LOGIN=1 ./run_weekly.sh
BROWSER_HEADED=1 ./run_weekly.sh
```

## 条件选股批次

自动打开东方财富条件选股并导入：

```bash
./run_xuangu.sh
```

导入已有 Excel：

```bash
.venv/bin/python xuangu_to_sqlite.py \
  --import-xlsx downloads/xuangu_20260704.xlsx \
  --db stocks.db \
  --condition-file screening.txt \
  --batch-id 20260704 \
  --replace-existing
```

只预览 Top，不落库：

```bash
.venv/bin/python weekly_stock_main.py --config config/weekly_strategy.yaml \
  preview --date 2026-07-03 --xuangu-batch-id 20260704
```

默认最终名单采用双通道：17 只按综合分入选，3 只保留给未入主榜、营收增速未达标但趋势、量价和突破分均达标的强势股。主榜不再硬过滤营收，营收仅参与评分。例外入选会在原因中标记“强势例外通道入选”；例外名额不足时由合格综合榜顺延补齐。两条通道均遵守最低分和非 ST/退市要求，合格股票不足时允许少选。相关阈值在 `config/weekly_strategy.yaml` 的 `screening` 和 `momentum_exception` 中配置。

### 预测质量与行情维护

日更和周更现在先刷新全 A 股建模范围。首次补齐可能耗时较长；已达到日期和历史长度要求的股票会跳过，失败后可再次执行继续补齐。为避免前复权基准拼接错误，每只需要更新的股票会刷新完整建模窗口，而非只追加最新几天。

```bash
# 仅读取覆盖率，不联网、不修改数据库；不达标返回非零状态
.venv/bin/python -B refresh_market_history.py --end-date 2026-09-11 --check

# 补齐全股票池并验收（日期必须是中国交易日）
.venv/bin/python -B -u refresh_market_history.py --end-date 2026-09-11
```

`SKIP_MARKET_HISTORY=1` 可跳过脚本中的刷新步骤，但不会跳过 ML 数据检查。默认要求最近 60 个已知交易日的已下载股票覆盖率至少 90%；不足时预测、回测会报错，不创建新模型。这个范围基于本地已下载股票，并非交易所正式每日上市/退市名单，因此仍需注意历史股票池偏差。

`execution` 是训练与复盘共用的成交设置：次日开盘入场、T+1、首日一字线/无成交量不建仓、开盘跳空按开盘价处理。默认每边费用和滑点各 5 个基点，属于可调整的建模假设。收益与目标按扣费后口径计算；同日同时触及止损和止盈时采用止损优先，已知开盘触发优先于日内未知顺序。持有期结束仍封死跌停等无法确认成交的样本不计入训练。日 K 无法完全重建成交队列，历史结果仍是模拟。

新标签保存 `next_open_v2` 及参数摘要版本，相同版本的复盘才参与反馈训练；旧复盘保留原口径。回测增加同一批已入选股票上的 `rule_paired`、`ml_paired` 与混合分比较，标签不完整的整批会提示并排除；规则分和混合分不是概率，不报告其 Brier 值。这不等于重建东方财富历史全市场候选池，放宽上游条件的效果需要新条件下的完整批次或额外历史候选数据验证。

保持 `rerank_after_predict: false`，先以时间顺序验证每周 Top-K 命中率、扣费收益和回撤，再决定是否让 ML 参与最终排名。

## 常用命令

```bash
# 正式规则打分并落库
.venv/bin/python weekly_stock_main.py --config config/weekly_strategy.yaml screen

# 复盘最近一个未复盘 run
.venv/bin/python weekly_stock_main.py --config config/weekly_strategy.yaml review

# 复盘指定 run
.venv/bin/python weekly_stock_main.py --config config/weekly_strategy.yaml review --run-id 100

# 查看历史 run
.venv/bin/python weekly_stock_main.py --config config/weekly_strategy.yaml runs

# 对已落库 Top 做 ML 预测/重排
.venv/bin/python weekly_stock_main.py --config config/weekly_strategy.yaml predict --run-id 100

# 查看复盘趋势
.venv/bin/python weekly_stock_main.py --config config/weekly_strategy.yaml trend --limit 30 --window 6
```

## 关键文件

| 路径 | 用途 |
| --- | --- |
| `run_daily_update.sh` | 工作日补 K 线 + Top 预览，默认不落库 |
| `run_weekly.sh` | 周末正式流程 |
| `run_xuangu.sh` | 东方财富条件选股下载入库 |
| `weekly_stock_main.py` | 选股/复盘/ML 命令入口 |
| `view_quotes.py` | 本地网页服务 |
| `config/weekly_strategy.yaml` | 评分、复盘、ML 配置 |
| `screening.txt` | 东方财富条件选股文本 |
| `stocks.db` | SQLite 数据库 |
| `downloads/` | Excel、日志等输出 |

## 数据表

- `xuangu_batches` / `xuangu_results`：条件选股批次和明细
- `eastmoney_stock_daily_klines`：日 K
- `weekly_screen_runs` / `weekly_screen_candidates` / `weekly_selected_stocks`：周末选股结果
- `weekly_review_runs` / `weekly_review_results`：复盘结果
- `weekly_ml_model_runs` / `weekly_ml_training_samples` / `weekly_ml_predictions`：ML 训练和预测

## 测试

```bash
.venv/bin/python -B -m unittest discover -s tests
```

## 备注

- `run_daily_update.sh` 不再自动执行东方财富选股下载；需要新批次时先运行 `run_xuangu.sh` 或手动导入 Excel。
- ML 是规则 Top 之后的二次排序，不替代规则打分。
- 条件选股页面未登录时，用 `WAIT_LOGIN=1 ./run_xuangu.sh`。
