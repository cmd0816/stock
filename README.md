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

### 回测报告与同池对照

```bash
.venv/bin/python weekly_stock_main.py --config config/weekly_strategy.yaml backtest
```

开启逻辑回归基线时，报告包含全下载股票池上的逻辑回归／主模型结果，以及同一批历史已入选股票上的五组比较：`rule_paired`、`ml_paired`（配置的主模型）、主模型加规则、`logistic_regression_paired`、逻辑回归加规则。两种混合排名使用同一个规则权重。缺少可执行标签的批次不参与同池比较。这些结果仍不是重新运行上游历史全市场筛选。

每组结果新增 `all_neg`（全部预测不达标的准确率基准）、`top_n`（跨期实际选中样本数），并列出每个日期／批次的候选数、选中数、命中数和平均退出收益。同池模型另显示相对规则的收益差，单位为百分点。`avg_adverse` 是持有窗口内相对入场成本的最低收益的均值，不是账户净值最大回撤；窗口最高涨幅可能发生在策略退出后，不等于可实现收益。

同日只计一个正式批次：使用本地行情中的下一交易日，以及 `ml.backtest_entry_cutoff_time`（默认北京时间 `09:30`，不可晚于开盘）。只选择信号日收盘后、严格早于该截止时间生成的最后一个 run；时间戳相同则取较大的 run_id。此选择发生在读取收益标签之前。晚到、时间不可核实、缺少下一交易日和同日备选批次在审计表中列出；不删除历史记录。正式批次标签或入选快照不完整时排除，不回退到另一个批次。此规则适用于当前次日开盘建仓设置；时间戳只能核实记录生成时间，并不证明历史策略配置完全一致或从未被事后修改。

`ml.backtest_top_ks: [3, 5, 10]` 控制并列 K 值，兼容的 `backtest_top_k` 也会加入报告。`scope=market` 是下载股票池；`overall` 是同日去重后的所有有效入选批次；`rerank` 只包含候选数严格大于当前 K 的批次。只有 6 只股票的批次会进入 Top-10 的 overall，但不会进入 Top-10 的 rerank；Top-3/5 仍有排序空间。没有符合条件的批次报告 N/A，不报告零收益。不同 K 的 rerank 日期集合可能不同，不应根据同一测试集择优挑 K。

筛选阶段诊断按正式批次日期比较 `market`、`upstream`、`selected` 三个池的全体成员，而非各自 Top-K。`upstream` 必须存在原始批次及行数据、带时区的导入时间不晚于 run 生成时间、唯一股票数与记录的候选数一致，且包含入选股票；否则标注缺失或不可核实，不用今天的候选池补造。标签不完整的池仅报告覆盖数，不计算部分样本收益。共同日期汇总只使用三阶段均完整的日期，按日期等权。`market` 是已下载且具有可执行标签的样本，并非交易所历史完整名单；阶段差异仍包含股票池、标签可用性与历史条件变化的影响，不能直接作因果判断。

命中率、平均退出收益及同池收益差提供 95% 百分位 bootstrap 区间：固定随机种子 `20260912`，按 ISO 周整组有放回抽样 2,000 次，同周日期共同抽取，保留实际选中数加权；收益差使用相同周上的配对差值。少于两周不输出区间。此方法不处理跨周序列相关、重复调参或历史股票池偏差，少量周的区间也不稳定；不能将其作为盈利保证。最终方案仍须在未参与调参的时间区间验证。

报告仅增强评估，不自动切换主模型或开启正式选股重排。

### 分组诊断与评分项消融

报告将 `outside_weekly_scope`（非周频评估日期）与 `missing_features_or_execution_labels`（缺特征或可执行标签）分开。周中批次不再报成 `executable labels 0/N`，也不因此触发下载；后一种情况也不一定是行情缺失，可能是特征窗口或模拟成交限制。

训练期分组诊断使用每折 purge 后的训练样本确定分位边界，默认 `ml.diagnostic_bins: 3`、`diagnostic_min_train_samples: 20`。技术指标包括近5/20日涨幅、MA20偏离、5日平均换手率、5日/20日均量比、距离20日高点（突破位置代理，不是突破涨幅），边界来自该折全市场训练特征；五个评分分项的边界只来自能匹配训练日期的历史候选评分快照。测试对象固定为该折完整且有可执行标签的历史评分候选池。每折单独显示边界、训练来源和数量、测试数量、日期数、命中率和平均退出收益，不用测试收益重划边界。训练值不足、测试特征缺失、空分组均明确标注。离散/恒定值会减少有效分组数；历史分项已包含当时权重，跨期权重变化也会影响诊断，不能直接作因果解释。

评分消融固定同日去重后的完整历史评分候选池，比较 `score_original` 与分别减去趋势、量能、突破、基本面、风险分项的五个方案，按相同 Top-3/5/10、相同日期、相同成交标签报告收益及相对原总分的按周配对区间。这里只从历史总分减掉一个已加权分项，其余分项及附加分保留，同分按代码排序；不重新应用正式门槛和双通道，因此不是生产选股流程的完整反事实回测。候选记录数量不足、代码重复、评分缺失或任一成员缺标签时，整批不参与，不用剩余成员替换原池。`score_pool_rerank` 额外要求候选数大于 K。

这些诊断不会修改评分权重、阈值或正式排名。任何据此提出的新规则仍需在未用于调参的后续时间段验证，不从当前测试集自动挑选最优消融方案。

### 基本面字段质量与量能半权重影子方案

导入保留全部源列，并增加 `_fundamental_audit_v1`：记录读取列、原始值、解析值和导入观察时间。导入、选股会打印营收／利润同比覆盖率，区分 `missing_column`、`missing_value`、`invalid_value`、`ambiguous_columns` 与有效数值；只有有效数值才进一步判断达标或未达标。`--|2026半年报` 不会误读成增长 2026%，也不把扣非净利润同比当成普通净利润同比。缺失暂不加分但明确提示“未知”，不重分配剩余权重；已有保存的评分不回写。

源文件无利润列时必须在源导出中增加“净利润同比增长率”展示列，不要为了获得列而加一个利润筛选门槛。导入器不会联网补造该值。导入观察时间不等于财报公告时间，报告期也不代表当时已经公开；不能把本次导入的新财务值回填到旧选股批次。可只读检查：

```bash
.venv/bin/python weekly_stock_main.py fundamentals
# 指定仍保留在数据库中的原始批次
.venv/bin/python weekly_stock_main.py fundamentals --batch-id 20260911
```

`volume_half_v1` 固定为 `原总分 - 0.5 × 已加权量能分`，不是从测试数据择优选出的最优权重。回测新增 `volume_half_v1_paired`，在原正式入选池上与原排名比较；评分候选池诊断也列出同名半权重方案，两个池不可混比。

当前配置 `shadow.volume_half_enabled: true`：之后每次正式 `screen` 完成，冻结同一正式入选池，将影子顺序、原顺序、两种分数和记录时间写入独立的 `weekly_shadow_rankings`。不改变正式门槛、双通道资格、名单或排名，不对旧 run 自动补写“事前预测”。`preview` 和 `predict` 不生成这类前向记录。查看：

```bash
.venv/bin/python weekly_stock_main.py shadow
.venv/bin/python weekly_stock_main.py shadow --run-id 111
```

这些记录用于后续独立验证；历史回测仍属于探索，不算新方案的前向验证。

### 影子自动复盘与亏损来源

新生成的影子记录同时冻结 review/execution 配置和比较用 K 值。日更、周更补数流程及普通 `review` 完成后会检查已保存的影子批次，也可单独运行（仅使用缓存行情，不额外下载）：

```bash
.venv/bin/python weekly_stock_main.py shadow-review
.venv/bin/python weekly_stock_main.py shadow-review --run-id 111 --date 2026-09-18
```

只有信号收盘后、下一交易日北京时间09:30之前生成的正式与影子快照才可进入前向复盘。缺少冻结配置的旧记录标为不可验证，不用当前配置补齐。必须到达冻结的完整持有窗口（当前5个交易日）且池内所有股票的行情／退出可验证，否则保留 pending，后续补数再试；不删掉缺数据股票来改善结果。复盘日期不超过已收盘的中国日期。已完成结果保存于 `weekly_shadow_reviews`，再次运行复用，不用后来的行情刷新覆盖。比较使用当时保存的原排名与影子排名，而不是现在的正式排名；当前配置改变也不改变冻结口径。

每个 K 输出实际选中槽位、成交数、命中率、平均收益、平均盈利／亏损、换入股票数及影子相对原排名的收益差。每笔交易只归入一个退出分类：止盈、普通止损、开盘穿越止损（`gap_stop`）、持有到期、跌破MA20或未建仓。各类别 `contribution_pct` 加总等于总体平均收益，跳空止损不重复计入普通止损。未建仓按原固定槽位持有现金计零收益，不算已成交亏损；这不是完整账户资金曲线，也没有扣除额外组合调仓成本。

报告逐批展示，不将同日重复实验当成独立交易日进行汇总。这里的“自动”指运行日更／周更／复盘命令时触发，不新增后台定时服务。不要为旧日期补造前向排名，也不要因尚无新记录而把历史回测冒充前向结果。

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
