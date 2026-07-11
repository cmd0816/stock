from __future__ import annotations

import re
from statistics import mean
from typing import Any, Dict, List, Optional

from .models import CandidateStock, Kline, ScoreBreakdown, ScoredStock


def num(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).replace(",", "").replace("%", "").strip()
    if not text or text in {"-", "--"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def row_value(row: Dict[str, Any], keywords: List[str]) -> Optional[float]:
    for key, value in row.items():
        key_text = str(key)
        if all(k in key_text for k in keywords):
            parsed = num(value)
            if parsed is not None:
                return parsed
    return None


def latest(klines: List[Kline]) -> Optional[Kline]:
    return klines[-1] if klines else None


def ma(klines: List[Kline], days: int) -> Optional[float]:
    closes = [k.close for k in klines[-days:] if k.close is not None]
    if len(closes) < days:
        return None
    return mean(closes)


def pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b in (None, 0):
        return None
    return (a / b - 1) * 100


def add_if(reasons: List[str], condition: bool, reason: str) -> bool:
    if condition:
        reasons.append(reason)
        return True
    return False


def compute_tie_breaker(klines: List[Kline]) -> float:
    """当规则总分相同时，用近期动量/量能/新高接近度做进一步区分。"""
    if len(klines) < 11:
        return 0.0
    last = latest(klines)
    if last is None or last.close is None or last.close <= 0:
        return 0.0

    # 1. 近3日涨幅 (-5%~+5% 映射到 0~10)
    gain_3d = 0.0
    base = klines[-4]
    if base.close and base.close > 0:
        gain_3d = (last.close / base.close - 1) * 100
    score1 = max(0.0, min(gain_3d + 5.0, 10.0))

    # 2. 收盘价接近10日高点的程度 (0~10)
    high_10d = max((k.high or 0.0) for k in klines[-10:])
    score2 = 0.0
    if high_10d > 0:
        score2 = (last.close / high_10d) * 10.0

    # 3. 成交量放大倍数 (0~10)
    recent_vols = [k.volume for k in klines[-3:] if k.volume is not None]
    base_vols = [k.volume for k in klines[-13:-3] if k.volume is not None]
    score3 = 0.0
    if recent_vols and base_vols:
        recent_mean = mean(recent_vols)
        base_mean = mean(base_vols)
        if base_mean and base_mean > 0:
            ratio = recent_mean / base_mean
            score3 = min(ratio, 3.0) * (10.0 / 3.0)

    return round(score1 + score2 + score3, 2)


def score_candidate(candidate: CandidateStock, klines: List[Kline], config: Dict[str, Any]) -> ScoredStock:
    scoring = config["scoring"]
    weights = scoring["weights"]
    score = ScoreBreakdown()
    reasons: List[str] = score.reasons
    last = latest(klines)

    score.trend = score_trend(klines, scoring, weights["trend"], reasons)
    score.volume_turnover = score_volume_turnover(candidate, klines, scoring, weights["volume_turnover"], reasons)
    score.breakout = score_breakout(klines, scoring, weights["breakout"], reasons)
    score.fundamentals = score_fundamentals(candidate, scoring, weights["fundamentals"], reasons)
    score.risk = score_risk(candidate, klines, scoring, weights["risk"], reasons)
    score.tie_breaker = round(compute_tie_breaker(klines) / 10, 2)

    if not klines:
        reasons.append("历史K线不足，主要依据选股结果字段打分")
    elif last:
        reasons.append(f"最近交易日 {last.trade_date} 收盘 {last.close}")

    selected_reason = "；".join(reasons[:12])
    return ScoredStock(candidate=candidate, score=score, selected_reason=selected_reason)


def score_trend(klines: List[Kline], scoring: Dict[str, Any], weight: float, reasons: List[str]) -> float:
    if not klines:
        return 0
    trend_cfg = scoring["trend"]
    last = latest(klines)
    if last is None or last.close is None:
        return 0

    ma_short = ma(klines, int(trend_cfg["ma_short"]))
    ma_mid = ma(klines, int(trend_cfg["ma_mid"]))
    ma_long = ma(klines, int(trend_cfg["ma_long"]))
    ma_slow = ma(klines, int(trend_cfg["ma_slow"]))
    points = 0
    total = 4
    if add_if(reasons, ma_short is not None and last.close > ma_short, "收盘价站上短期均线"):
        points += 1
    if add_if(reasons, ma_short is not None and ma_mid is not None and ma_short > ma_mid, "短期均线强于中期均线"):
        points += 1
    if add_if(reasons, ma_mid is not None and ma_long is not None and ma_mid > ma_long, "中期均线强于长期均线"):
        points += 1
    if add_if(reasons, ma_long is not None and ma_slow is not None and ma_long > ma_slow, "长期趋势向上"):
        points += 1
    return round(weight * points / total, 2)


def score_volume_turnover(candidate: CandidateStock, klines: List[Kline], scoring: Dict[str, Any], weight: float, reasons: List[str]) -> float:
    cfg = scoring["volume_turnover"]
    last = latest(klines)
    points = 0
    total = 2

    turnover = last.turnover_rate if last and last.turnover_rate is not None else row_value(candidate.row_json, ["换手"])
    if turnover is not None and float(cfg["turnover_min"]) <= turnover <= float(cfg["turnover_max"]):
        reasons.append(f"换手率处于目标区间 {turnover:.2f}%")
        points += 1

    if len(klines) >= 11:
        recent = [k.volume for k in klines[-3:] if k.volume is not None]
        base = [k.volume for k in klines[-13:-3] if k.volume is not None]
        if recent and base:
            ratio = mean(recent) / mean(base) if mean(base) else 0
            if ratio >= float(cfg["volume_ratio_min"]):
                reasons.append(f"近3日成交量放大 {ratio:.2f} 倍")
                points += 1
    return round(weight * points / total, 2)


def score_breakout(klines: List[Kline], scoring: Dict[str, Any], weight: float, reasons: List[str]) -> float:
    if not klines:
        return 0
    cfg = scoring["breakout"]
    days = int(cfg["new_high_days"])
    strong_days = int(cfg["strong_gain_days"])
    last = latest(klines)
    points = 0
    total = 3

    if last and last.close is not None and len(klines) >= days:
        previous_high = max(k.close or 0 for k in klines[-days:])
        if last.close >= previous_high:
            reasons.append(f"收盘价接近或创 {days} 日新高")
            points += 1
    if last and last.high is not None and len(klines) >= days:
        high_n = max(k.high or 0 for k in klines[-days:])
        if last.high >= high_n:
            reasons.append(f"盘中价格创 {days} 日高点")
            points += 1
    if len(klines) > strong_days:
        gain = pct(klines[-1].close, klines[-1 - strong_days].close)
        if gain is not None and gain > 0:
            reasons.append(f"近 {strong_days} 日涨幅为正 {gain:.2f}%")
            points += 1
    return round(weight * points / total, 2)


def score_fundamentals(candidate: CandidateStock, scoring: Dict[str, Any], weight: float, reasons: List[str]) -> float:
    cfg = scoring["fundamentals"]
    revenue_growth = row_value(candidate.row_json, ["营业", "同比"])
    profit_growth = row_value(candidate.row_json, ["净利润", "同比"])
    points = 0
    total = 2
    if revenue_growth is not None and revenue_growth >= float(cfg["revenue_growth_min"]):
        reasons.append(f"营业收入同比增长 {revenue_growth:.2f}%")
        points += 1
    if profit_growth is not None and profit_growth >= float(cfg["profit_growth_min"]):
        reasons.append(f"净利润同比增长 {profit_growth:.2f}%")
        points += 1
    return round(weight * points / total, 2)


def score_risk(candidate: CandidateStock, klines: List[Kline], scoring: Dict[str, Any], weight: float, reasons: List[str]) -> float:
    cfg = scoring["risk"]
    last = latest(klines)
    name = candidate.name or ""
    price = last.close if last and last.close is not None else row_value(candidate.row_json, ["最新"])
    latest_drop = last.change_percent if last and last.change_percent is not None else row_value(candidate.row_json, ["涨跌幅"])
    # Keep each penalty worth one third of the risk score. Additional checks
    # strengthen the filter without diluting the existing ST/price/drop rules.
    max_risk_points = 3
    risk_points = max_risk_points

    if "ST" in name.upper() or "退" in name:
        reasons.append("风险扣分：名称包含 ST/退")
        risk_points -= 1
    if price is not None and price > float(cfg["max_price"]):
        reasons.append(f"风险扣分：股价高于 {cfg['max_price']}")
        risk_points -= 1
    if latest_drop is not None and latest_drop < float(cfg["max_daily_drop_pct"]):
        reasons.append(f"风险扣分：最近交易日跌幅 {latest_drop:.2f}%")
        risk_points -= 1

    if last and last.close and len(klines) >= 14:
        ranges = [
            float(k.high) - float(k.low)
            for k in klines[-14:]
            if k.high is not None and k.low is not None
        ]
        atr_pct = mean(ranges) / float(last.close) * 100 if ranges else None
        if atr_pct is not None and atr_pct > float(cfg.get("max_atr_pct", 8)):
            reasons.append(f"风险扣分：近14日波动过高 {atr_pct:.2f}%")
            risk_points -= 1

    if last and last.close and len(klines) >= 6 and klines[-6].close:
        gain_5d = pct(last.close, klines[-6].close)
        if gain_5d is not None and gain_5d > float(cfg.get("max_5d_gain_pct", 15)):
            reasons.append(f"风险扣分：近5日涨幅过大 {gain_5d:.2f}%")
            risk_points -= 1

    if risk_points == max_risk_points:
        reasons.append("风险过滤通过")
    return round(weight * max(0, risk_points) / max_risk_points, 2)


def rank_candidates(candidates: List[CandidateStock], klines_by_code: Dict[str, List[Kline]], config: Dict[str, Any]) -> List[ScoredStock]:
    scored = [score_candidate(c, klines_by_code.get(c.code, []), config) for c in candidates]
    return sorted(
        scored,
        key=lambda item: (item.score.total, item.score.tie_breaker, item.score.trend, item.score.breakout),
        reverse=True,
    )
