from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from statistics import mean

from . import db
from .fundamentals import print_coverage
from .config import load_config
from .jobs import (
    ml_backtest_job,
    ml_predict_job,
    project_root,
    stock_screen_job,
    stock_screen_preview_job,
    weekly_review_job,
)


def print_ml_predictions(config_path: Path, config: dict, model_run_id: int) -> None:
    root = project_root(config_path)
    db_path = root / config["database"]["path"]
    with db.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT model_run_id, source_run_id, train_sample_count, positive_sample_count
            FROM weekly_ml_model_runs
            WHERE model_run_id = ?
            """,
            (model_run_id,),
        ).fetchone()
        if row is None:
            print("No ML model run found.")
            return
        predictions = db.ml_predictions_for_run(conn, int(row["source_run_id"]))

    train_count = int(row["train_sample_count"])
    positive_count = int(row["positive_sample_count"])
    positive_rate = positive_count / train_count * 100 if train_count else 0
    print(
        f"ML model_run_id={row['model_run_id']} source_run_id={row['source_run_id']} "
        f"train_samples={train_count} positive_rate={positive_rate:.1f}%"
    )
    if not predictions:
        print("No ML predictions generated.")
        return

    print("rank code   name        prob_up baseline predicted_score focus reason")
    for rank, pred in enumerate(predictions, start=1):
        code = str(pred["code"])
        name = str(pred["name"] or "")[:8]
        prob = float(pred["probability_up"]) * 100
        score = float(pred["predicted_score"])
        try:
            features = json.loads(str(pred["feature_json"] or "{}"))
        except Exception:
            features = {}
        baseline = features.get("baseline_probability_up")
        baseline_txt = f"{float(baseline) * 100:>6.1f}%" if baseline is not None else "     -"
        reason = str(pred["reason"] or "").replace("\n", " ")[:80]
        # 对前5名进行重点标记
        focus = "【推荐】" if rank <= 5 else "       "
        print(f"{rank:>4} {code:<6} {name:<8} {prob:>6.1f}% {baseline_txt} {score:>15.2f} {focus} {reason}")
    if predictions:
        avg_prob = sum(float(p["probability_up"]) * 100 for p in predictions) / len(predictions)
        print(f"\n平均上涨概率: {avg_prob:.1f}% | 前5名为模型最推荐的标的")


def print_screen_runs(config_path: Path, config: dict, limit: int) -> None:
    root = project_root(config_path)
    db_path = root / config["database"]["path"]
    with db.connect(db_path) as conn:
        db.ensure_weekly_tables(conn)
        rows = db.screen_runs(conn, limit=limit)
    if not rows:
        print("No weekly screen runs found.")
        return
    print("run_id screen_date  batch_id  candidates selected ml_predictions model     created_at")
    for row in rows:
        print(
            f"{row['run_id']:>6} "
            f"{str(row['screen_date'] or '-'):<11} "
            f"{str(row['xuangu_batch_id'] or '-'):<9} "
            f"{int(row['candidate_count'] or 0):>10} "
            f"{int(row['selected_count'] or 0):>8} "
            f"{int(row['ml_prediction_count'] or 0):>14} "
            f"{str(row['latest_model_name'] or '-'):>9} "
            f"{row['created_at_utc']}"
        )


def print_screen_preview(result: dict) -> None:
    selected = result.get("selected") or []
    print(
        "screen_preview completed: "
        f"screen_date={result.get('screen_date')} "
        f"batch_id={result.get('xuangu_batch_id')} "
        f"candidates={int(result.get('candidate_count') or 0)} "
        f"selected={int(result.get('selected_count') or 0)} "
        "persisted=0"
    )
    print("rank code   name        score reason")
    for rank, item in enumerate(selected, start=1):
        code = str(item.candidate.code)
        name = str(item.candidate.name or "")[:8]
        score = float(item.score.total)
        reason = str(item.selected_reason or "").replace("\n", " ")[:80]
        print(f"{rank:>4} {code:<6} {name:<8} {score:>5.2f} {reason}")
        machine_reason = reason.replace("|", "/")
        print(f"TOP_PREVIEW|{rank}|{code}|{name}|{score:.2f}|{machine_reason}")


def print_backtest_metrics(metrics: list) -> None:
    if not metrics:
        print("No ML backtest metrics generated.")
        return
    print("scope K model               folds train purge test positive all_neg accuracy precision recall brier top_n top_k_hit avg_exit avg_high avg_adverse")
    for item in metrics:
        if not item.test_count:
            print(f"{item.scope} {item.top_k} {item.model_name}: N/A（没有符合本口径的测试批次）")
            continue
        print(
            f"{item.scope:<7} {item.top_k:>2} "
            f"{item.model_name:<18} "
            f"{item.fold_count:>5} "
            f"{item.train_count:>5} "
            f"{item.avg_purged_train_count:>5} "
            f"{item.test_count:>4} "
            f"{item.positive_rate * 100:>7.1f}% "
            f"{item.always_negative_accuracy * 100:>7.1f}% "
            f"{item.accuracy * 100:>7.1f}% "
            f"{item.precision * 100:>8.1f}% "
            f"{item.recall * 100:>5.1f}% "
            f"{item.brier_score:>5.3f} "
            f"{item.selected_count:>5} "
            f"{item.top_k_hit_rate * 100:>8.1f}% "
            f"{item.top_k_avg_close_gain_pct:>8.2f}% "
            f"{item.top_k_avg_high_gain_pct:>7.2f}% "
            f"{item.top_k_avg_max_drawdown_pct:>11.2f}%"
        )
    # 强调对选股系统更有价值的指标
    print()
    print("【重点关注】对选股系统而言，以下指标比整体准确率更有意义：")
    for item in metrics:
        if not item.test_count:
            continue
        print(
            f"  • [{item.scope}] {item.model_name}: Top-{item.top_k} 命中率={item.top_k_hit_rate*100:.1f}% | "
            f"平均最高涨幅={item.top_k_avg_high_gain_pct:+.2f}% | "
            f"平均策略退出收益={item.top_k_avg_close_gain_pct:+.2f}% | "
            f"持有窗口最大不利变动均值={item.top_k_avg_max_drawdown_pct:+.2f}% | "
            f"{item.fold_count}折 walk-forward，平均 purge={item.avg_purged_train_count}"
        )
    print("\n口径：all_neg=全部预测不达标的准确率；top_n=跨期实际选中样本数，不是独立股票数。")
    print("avg_high 是窗口最高涨幅（可能在退出后），不是实际可得收益；avg_adverse 不是账户净值最大回撤。")
    print("paired/e2e 仅在同一批历史已入选股票上比较；不代表重建上游历史选股流程。")
    print("scope: market=下载股票池；overall=去重后所有有效入选批次；rerank=仅候选数>K的批次。")
    print("K=3/5/10并列报告（可配置）；不同K的rerank日期集合可能不同，不据测试结果挑选最优K。")
    print("\n【不确定性】95% 百分位区间：固定种子、2,000 次按 ISO 周整组重采样，保留选中数加权。")
    print("不处理跨周序列相关或反复调参偏差；少量周的区间不稳定，不足两周不报告。")

    def interval_text(interval, scale=1.0, unit="%"):
        if interval is None:
            return "N/A（不足两周）"
        return f"[{interval[0] * scale:+.2f}, {interval[1] * scale:+.2f}]{unit}"

    for item in metrics:
        if not item.test_count:
            continue
        print(f"  [{item.scope} K={item.top_k}] {item.model_name}: weeks={item.bootstrap_week_count} | "
              f"命中率 CI={interval_text(item.hit_rate_ci, 100)} | "
              f"退出收益 CI={interval_text(item.avg_exit_ci)}")
        if item.exit_delta_vs_rule_pct is not None:
            print(f"    相对规则收益差={item.exit_delta_vs_rule_pct:+.2f} 个百分点 | "
                  f"配对 CI={interval_text(item.exit_delta_ci, unit=' 个百分点')}")
    print("\n【逐周／批次明细】收益为配置成交口径的平均单笔退出收益，不是组合净值收益。")
    print("scope K model period(date#run_id) candidates selected hits hit_rate avg_exit delta_vs_rule_pp")
    for item in metrics:
        for period in item.periods:
            hit_rate = period.hit_count / period.selected_count if period.selected_count else 0.0
            delta = "-" if period.exit_delta_vs_rule_pct is None else f"{period.exit_delta_vs_rule_pct:+.2f}"
            print(f"{item.scope} {item.top_k} {item.model_name} {period.period} {period.candidate_count} "
                  f"{period.selected_count} {period.hit_count} {hit_rate:.1%} "
                  f"{period.avg_exit_pct:+.2f}% {delta}")
    print_backtest_diagnostics(metrics)
    print_factor_diagnostics(metrics)


def print_factor_diagnostics(metrics) -> None:
    exclusions = getattr(metrics, 'exclusions', [])
    if exclusions:
        print('\n【评估范围与样本覆盖】outside_weekly_scope=不在周频评估范围，不表示缺行情。')
        print('missing_features_or_execution_labels=缺特征或可执行标签，不能直接推断需要重新下载。')
        print('scope fold date run_id available/expected status（仅列排除项）')
        for row in exclusions:
            if row['status'] != 'complete':
                available = 'N/A' if row['available'] is None else row['available']
                print(f"{row['scope']} {row['fold']} {row['screen_date']} {row['run_id']} "
                      f"{available}/{row['expected']} {row['status']}")
    groups = getattr(metrics, 'factor_groups', [])
    print('\n【训练期分组诊断】测试对象为完整历史评分候选池；缺失评分/标签的整批排除，不补造历史分项。')
    print('技术指标边界来自该折purge后的全市场训练样本；评分分项边界来自训练期历史候选快照。')
    print('ret_5/ret_20=近5/20日涨幅；close_ma20_pct=偏离MA20；turnover_5=5日平均换手；')
    print('volume_ratio_5_20=5日/20日均量比；dist_high_20=距20日高点（突破位置代理，并非突破涨幅）。')
    print('分项是历史已加权得分，历史权重可能不同；分组仅供诊断，不是因果检验或自动调参依据。')
    if not groups:
        print('N/A（无历史评分快照）')
    else:
        print('fold test_start..test_end factor train_source train_n bin[lower,upper) test_n dates hit_rate avg_exit status')
        for row in groups:
            low = '-inf' if row.get('lower') is None else f"{row['lower']:.4g}"
            high = '+inf' if row.get('upper') is None else f"{row['upper']:.4g}"
            boundary = 'N/A' if row['bin'] is None else f'[{low},{high})'
            hit = 'N/A' if row['hit_rate'] is None else f"{row['hit_rate']:.1%}"
            gain = 'N/A' if row['avg_exit_pct'] is None else f"{row['avg_exit_pct']:+.2f}%"
            print(f"{row['fold']} {row['test_start']}..{row['test_end']} {row['factor']} "
                  f"{row['train_source']} {row['train_count']} {boundary} {row['count']} "
                  f"{row.get('dates', 0)} {hit} {gain} {row['status']}")
    ablations = getattr(metrics, 'ablations', [])
    print('\n【去掉一个评分项】冻结完整评分候选池，以历史总分排序为基准；只减去一个已加权分项。')
    print('不重新运行生产的门槛/双通道，不代表完整策略收益；原总分的其余项和附加分保持不变。')
    print('score_pool_rerank仅计候选数>K的批次；同分按股票代码排序。差值区间仍为按周配对bootstrap。')
    if not ablations:
        print('N/A（无完整可评估评分候选批次）')
        return
    print('scope K variant dates selected hit_rate avg_exit delta_vs_original_pp delta_CI95_pp')
    for metric in ablations:
        if not metric.selected_count:
            print(f'{metric.scope} {metric.top_k} {metric.model_name}: N/A')
            continue
        delta = '-' if metric.exit_delta_vs_rule_pct is None else f'{metric.exit_delta_vs_rule_pct:+.2f}'
        ci = 'N/A' if metric.exit_delta_ci is None else f'[{metric.exit_delta_ci[0]:+.2f},{metric.exit_delta_ci[1]:+.2f}]'
        print(f'{metric.scope} {metric.top_k} {metric.model_name} {len(metric.periods)} {metric.selected_count} '
              f'{metric.top_k_hit_rate:.1%} {metric.top_k_avg_close_gain_pct:+.2f}% {delta} {ci}')


def print_backtest_diagnostics(metrics) -> None:
    audit = getattr(metrics, 'run_audit', [])
    if audit:
        print('\n【同日批次审计】仅canonical进入主比较，其他批次不删除、不按收益替换。')
        print('规则：收盘后且严格早于下一交易日配置截止时间的最后一个批次；同时间戳按run_id决胜。')
        print('alternate_same_date=同日备选；at_or_after_entry_cutoff=晚到；unverifiable_timestamp=时间不可核实。')
        print('审计覆盖存储批次；canonical仍需位于测试区间且标签完整才进入收益统计。')
        print('date run_id created_at_utc entry_cutoff status')
        for row in audit:
            print(f"{row['screen_date']} {row['run_id']} {row['created_at_utc']} {row['cutoff']} {row['status']}")
    stages = getattr(metrics, 'stages', [])
    if not stages:
        print('\n【筛选阶段诊断】N/A：无可用于诊断的测试日期／正式批次。')
        return
    print('\n【筛选阶段诊断】market=已下载且有可执行标签的股票池；upstream=可核实的历史上游快照；selected=规则入选池。')
    print('这里统计各池全体成员，不是Top-K；缺失快照或标签不完整只报告覆盖，不计算幸存样本收益。')
    print('market仍有本地股票池和可执行标签筛选偏差，阶段差异不能单独证明因果。')
    print('date run_id stage labels/expected hit_rate avg_exit common_date status')
    for row in stages:
        hit = 'N/A' if row['hit_rate'] is None else f"{row['hit_rate']:.1%}"
        gain = 'N/A' if row['avg_exit_pct'] is None else f"{row['avg_exit_pct']:+.2f}%"
        print(f"{row['screen_date']} {row['run_id']} {row['stage']} {row['available']}/{row['expected']} "
              f"{hit} {gain} {row['comparable']} {row['status']}")
    print('【阶段同日期汇总】仅三阶段均完整的共同日期，逐日期等权；不与其他日期的结果混比。')
    for stage in ('market', 'upstream', 'selected'):
        rows = [row for row in stages if row['comparable'] and row['stage'] == stage]
        if rows:
            print(f"{stage}: dates={len(rows)} hit_rate={mean(row['hit_rate'] for row in rows):.1%} "
                  f"avg_exit={mean(row['avg_exit_pct'] for row in rows):+.2f}%")
        else:
            print(f'{stage}: N/A（没有三阶段均完整的共同日期）')


def print_source_or_shadow(config_path, config, command, batch_id=None, run_id=None):
    path = (project_root(config_path) / config['database']['path']).resolve()
    with closing(sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if command == 'fundamentals':
            batch_id = batch_id or db.latest_xuangu_batch_id(conn)
            rows = conn.execute('SELECT row_json FROM xuangu_results WHERE batch_id=?', (batch_id,)).fetchall()
            print_coverage([json.loads(row[0]) for row in rows], f'batch {batch_id}')
            print('只检查保存的源快照，不联网、不补造数据；导入时间不是财报公告时间。')
            return
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='weekly_shadow_rankings'").fetchone()
        if not exists:
            print('No forward shadow snapshots yet. New screen runs will record them.')
            return
        if run_id is None:
            run_id = conn.execute('SELECT MAX(run_id) FROM weekly_shadow_rankings').fetchone()[0]
        rows = conn.execute('SELECT * FROM weekly_shadow_rankings WHERE run_id=? ORDER BY shadow_rank', (run_id,)).fetchall()
        print(f'volume_half_v1 run_id={run_id}; frozen production-selected pool; no official rank changes')
        print('shadow_rank original_rank code original_score shadow_score recorded_at_utc')
        for row in rows:
            print(f"{row['shadow_rank']} {row['original_rank']} {row['code']} {row['original_score']:.2f} "
                  f"{row['shadow_score']:.2f} {row['recorded_at_utc']}")
        if not rows:
            print('No saved shadow snapshot for this run; historical snapshots are not fabricated.')


def avg_or_zero(values: list[float]) -> float:
    return mean(values) if values else 0.0


def print_trend_summary(rows: list, window: int) -> None:
    if len(rows) < window * 2:
        print(f"Need at least {window * 2} reviewed runs for rolling trend comparison; current={len(rows)}.")
        return

    recent = rows[:window]
    previous = rows[window : window * 2]

    def metric(items: list, key: str) -> float:
        values = [float(row[key]) for row in items if row[key] is not None]
        return avg_or_zero(values)

    hit_recent = metric(recent, "hit_rate")
    hit_prev = metric(previous, "hit_rate")
    close_recent = metric(recent, "avg_close_gain_pct")
    close_prev = metric(previous, "avg_close_gain_pct")
    drawdown_recent = metric(recent, "avg_max_drawdown_pct")
    drawdown_prev = metric(previous, "avg_max_drawdown_pct")

    print(
        "rolling "
        f"{window}-run: "
        f"hit={hit_recent * 100:.1f}% ({(hit_recent - hit_prev) * 100:+.1f}ppt), "
        f"avg_exit={close_recent:.2f}% ({close_recent - close_prev:+.2f}%), "
        f"avg_drawdown={drawdown_recent:.2f}% ({drawdown_recent - drawdown_prev:+.2f}%)"
    )


def print_review_trend(config_path: Path, config: dict, limit: int, window: int) -> None:
    root = project_root(config_path)
    db_path = root / config["database"]["path"]
    with db.connect(db_path) as conn:
        db.ensure_weekly_tables(conn)
        rows = db.review_trend_runs(conn, limit=limit)
    if not rows:
        print("No reviewed weekly runs found.")
        return

    print("run_id screen_date selected reviewed hit stop_loss avg_exit  avg_high avg_drawdown ml_pred avg_prob")
    for row in rows:
        hit = float(row["hit_rate"] or 0) * 100
        stop_loss = float(row["stop_loss_rate"] or 0) * 100
        avg_close = float(row["avg_close_gain_pct"] or 0)
        avg_high = float(row["avg_high_gain_pct"] or 0)
        avg_drawdown = float(row["avg_max_drawdown_pct"] or 0)
        avg_prob = float(row["avg_probability_up"] or 0) * 100
        print(
            f"{int(row['run_id']):>6} "
            f"{str(row['screen_date'] or '-'):<11} "
            f"{int(row['selected_count'] or 0):>8} "
            f"{int(row['reviewed_count'] or 0):>8} "
            f"{hit:>5.1f}% "
            f"{stop_loss:>9.1f}% "
            f"{avg_close:>8.2f}% "
            f"{avg_high:>7.2f}% "
            f"{avg_drawdown:>11.2f}% "
            f"{int(row['ml_prediction_count'] or 0):>7} "
            f"{avg_prob:>7.1f}%"
        )
    print_trend_summary(rows, window)
    # 提示用户关注核心实盘指标
    if rows:
        latest = rows[0]
        print()
        print(
            "【实盘效果】最近一期: 命中率="
            f"{float(latest['hit_rate'] or 0)*100:.1f}% | "
            f"平均策略退出={float(latest['avg_close_gain_pct'] or 0):+.2f}% | "
            f"平均最高={float(latest['avg_high_gain_pct'] or 0):+.2f}% | "
            f"止损率={float(latest['stop_loss_rate'] or 0)*100:.1f}%"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly stock screening and review jobs.")
    parser.add_argument("--config", default="config/weekly_strategy.yaml", help="YAML strategy config path")
    sub = parser.add_subparsers(dest="command", required=True)

    screen = sub.add_parser("screen", help="Run stock_screen_job")
    screen.add_argument("--date", default=None, help="Screen date, YYYY-MM-DD")
    screen.add_argument("--xuangu-batch-id", default=None, help="Use a specific xuangu batch id")
    screen.add_argument("--run-xuangu", action="store_true", help="Run xuangu download before scoring")
    screen.add_argument("--replace-existing", action="store_true", help="Replace existing screen run for the same date/batch")

    preview = sub.add_parser("preview", help="Preview Top stocks without writing weekly screen tables")
    preview.add_argument("--date", default=None, help="Screen date, YYYY-MM-DD")
    preview.add_argument("--xuangu-batch-id", default=None, help="Use a specific xuangu batch id")
    preview.add_argument("--run-xuangu", action="store_true", help="Run xuangu download before scoring")

    review = sub.add_parser("review", help="Run weekly_review_job")
    review.add_argument("--date", default=None, help="Review date, YYYY-MM-DD")
    review.add_argument("--run-id", type=int, default=None, help="Review a specific weekly screen run")
    review.add_argument("--replace-existing", action="store_true", help="Replace existing review run for the same run_id")

    predict = sub.add_parser("predict", help="Run ML prediction/re-ranking for selected stocks")
    predict.add_argument("--run-id", type=int, default=None, help="Predict a specific weekly screen run")

    runs = sub.add_parser("runs", help="List historical weekly screen runs and run_id values")
    runs.add_argument("--limit", type=int, default=20, help="Max runs to list")

    sub.add_parser("backtest", help="Run time-split ML backtest for baseline and main model")
    fundamentals = sub.add_parser('fundamentals', help='Read-only source field coverage check')
    fundamentals.add_argument('--batch-id', default=None)
    shadow = sub.add_parser('shadow', help='Show saved forward half-volume shadow ranking')
    shadow.add_argument('--run-id', type=int, default=None)
    shadow_review = sub.add_parser('shadow-review', help='Evaluate saved forward shadow snapshots using cached bars')
    shadow_review.add_argument('--run-id', type=int, default=None)
    shadow_review.add_argument('--date', default=None, help='Review through YYYY-MM-DD; capped at completed China session')
    trend = sub.add_parser("trend", help="Show reviewed run performance trend and rolling comparison")
    trend.add_argument("--limit", type=int, default=20, help="How many reviewed runs to show")
    trend.add_argument("--window", type=int, default=4, help="Rolling window size for trend delta")

    args = parser.parse_args()
    config_path = Path(args.config)
    config = load_config(config_path)

    if args.command == "screen":
        if args.run_xuangu:
            config["screening"]["run_xuangu"] = True
        run_id = stock_screen_job(
            config_path,
            config,
            screen_date=args.date,
            xuangu_batch_id=args.xuangu_batch_id,
            replace_existing=args.replace_existing,
        )
        print(f"stock_screen_job completed: run_id={run_id}")
    elif args.command == "preview":
        if args.run_xuangu:
            config["screening"]["run_xuangu"] = True
        result = stock_screen_preview_job(
            config_path,
            config,
            screen_date=args.date,
            xuangu_batch_id=args.xuangu_batch_id,
        )
        print_screen_preview(result)
    elif args.command == "review":
        review_id = weekly_review_job(
            config_path,
            config,
            review_date=args.date,
            run_id=args.run_id,
            replace_existing=args.replace_existing,
        )
        print(f"weekly_review_job completed: review_id={review_id}")
    elif args.command == "predict":
        model_run_id = ml_predict_job(config_path, config, run_id=args.run_id)
        print(f"ml_predict_job completed: model_run_id={model_run_id}")
        print_ml_predictions(config_path, config, model_run_id)
    elif args.command == "runs":
        print_screen_runs(config_path, config, args.limit)
    elif args.command == 'shadow-review':
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from .shadow_review import review_shadow_runs, print_shadow_reviews
        review_date = args.date or datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        with db.connect(project_root(config_path) / config['database']['path']) as conn:
            print_shadow_reviews(review_shadow_runs(conn, review_date, args.run_id))
    elif args.command in ('fundamentals', 'shadow'):
        print_source_or_shadow(config_path, config, args.command,
                               batch_id=getattr(args, 'batch_id', None), run_id=getattr(args, 'run_id', None))
    elif args.command == "backtest":
        metrics = ml_backtest_job(config_path, config)
        print_backtest_metrics(metrics)
    elif args.command == "trend":
        print_review_trend(config_path, config, args.limit, max(1, args.window))


if __name__ == "__main__":
    main()
