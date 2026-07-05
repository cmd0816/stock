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
