from __future__ import annotations

import json
import math
import pickle
import base64
import warnings
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .models import Kline
from .trading_calendar import weekly_last_trading_days


FEATURE_NAMES = [
    "ret_5",
    "ret_10",
    "ret_20",
    "close_ma5_pct",
    "close_ma10_pct",
    "close_ma20_pct",
    "close_ma60_pct",
    "ma5_ma20_pct",
    "ma20_slope_5",
    "volume_ratio_5_20",
    "turnover_5",
    "volatility_20",
    "drawdown_20",
    "dist_high_20",
    "rsi_14",
    "macd_dif",
    "macd_dea",
    "macd_hist",
    "boll_pct",
    "boll_width",
    "atr_14",
    "consecutive_days",
    "upper_shadow_pct",
    "lower_shadow_pct",
    "body_pct",
    "price_volume_corr_10",
    "momentum_5_20",
    "ret_5_rank",
    "ret_10_rank",
    "ret_20_rank",
    "close_ma20_pct_rank",
    "volume_ratio_5_20_rank",
    "market_ret_20",
    "market_breadth_adv_5",
    "market_breadth_above_ma20",
    "market_breadth_new_high_20",
    "market_universe_size",
    "market_member_ret_5",
    "market_member_ret_20",
    "excess_ret_5_vs_market",
    "excess_ret_20_vs_market",
    "fund_main_net_ratio",
    "fund_main_net_ratio_5",
    "fund_main_net_ratio_20",
    "fund_main_net_trend",
    "fund_flow_available",
    "sector_ret_5",
    "sector_ret_20",
    "sector_momentum_5_20",
    "sector_member_ret_5",
    "sector_member_ret_20",
    "excess_ret_5_vs_sector",
    "excess_ret_20_vs_sector",
    "sector_available",
]


@dataclass
class TrainingSample:
    code: str
    trade_date: str
    features: Dict[str, float]
    label: int
    future_high_gain_pct: float
    future_close_gain_pct: float
    future_max_drawdown_pct: float


@dataclass
class BacktestMetrics:
    model_name: str
    train_count: int
    test_count: int
    positive_rate: float
    accuracy: float
    precision: float
    recall: float
    top_k: int
    top_k_hit_rate: float
    top_k_avg_close_gain_pct: float
    top_k_avg_high_gain_pct: float
    top_k_avg_max_drawdown_pct: float


@dataclass
class CentroidModel:
    feature_names: List[str]
    means: Dict[str, float]
    stds: Dict[str, float]
    positive_centroid: Dict[str, float]
    negative_centroid: Dict[str, float]
    positive_rate: float

    def predict_probability(self, features: Dict[str, float]) -> float:
        if not self.feature_names:
            return self.positive_rate
        z = {name: self._z(name, features.get(name, 0.0)) for name in self.feature_names}
        pos_dist = self._distance(z, self.positive_centroid)
        neg_dist = self._distance(z, self.negative_centroid)
        prior = math.log(max(0.001, min(0.999, self.positive_rate)) / max(0.001, min(0.999, 1 - self.positive_rate)))
        raw = (neg_dist - pos_dist) + prior
        return 1 / (1 + math.exp(-max(-20.0, min(20.0, raw))))

    def explain(self, features: Dict[str, float], limit: int = 4) -> str:
        if not self.feature_names:
            return "训练样本不足，无法解释"
        z = {name: self._z(name, features.get(name, 0.0)) for name in self.feature_names}
        impacts = []
        for name in self.feature_names:
            pos = self.positive_centroid.get(name, 0.0)
            neg = self.negative_centroid.get(name, 0.0)
            impact = z[name] * (pos - neg)
            impacts.append((abs(impact), impact, name, features.get(name, 0.0)))
        impacts.sort(reverse=True)
        labels = []
        for _, impact, name, value in impacts[:limit]:
            direction = "偏正向" if impact >= 0 else "偏负向"
            labels.append(f"{feature_label(name)}={value:.3f}（{direction}）")
        return "；".join(labels)

    def _z(self, name: str, value: float) -> float:
        return (value - self.means.get(name, 0.0)) / self.stds.get(name, 1.0)

    @staticmethod
    def _distance(a: Dict[str, float], b: Dict[str, float]) -> float:
        return math.sqrt(sum((a.get(k, 0.0) - b.get(k, 0.0)) ** 2 for k in a))

    def to_json(self) -> str:
        return json.dumps(
            {
                "feature_names": self.feature_names,
                "means": self.means,
                "stds": self.stds,
                "positive_centroid": self.positive_centroid,
                "negative_centroid": self.negative_centroid,
                "positive_rate": self.positive_rate,
            },
            ensure_ascii=False,
        )


@dataclass
class SklearnLikeModel:
    model_name: str
    feature_names: List[str]
    estimator: Any
    positive_rate: float
    baseline_estimator: Any = None
    baseline_model_name: Optional[str] = None

    def predict_probability(self, features: Dict[str, float]) -> float:
        row = [[features.get(name, 0.0) for name in self.feature_names]]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            proba = self.estimator.predict_proba(row)[0][1]
        return float(max(0.0, min(1.0, proba)))

    def predict_baseline_probability(self, features: Dict[str, float]) -> Optional[float]:
        if self.baseline_estimator is None:
            return None
        row = [[features.get(name, 0.0) for name in self.feature_names]]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            proba = self.baseline_estimator.predict_proba(row)[0][1]
        return float(max(0.0, min(1.0, proba)))

    def explain(self, features: Dict[str, float], limit: int = 4) -> str:
        importances = self._feature_importances()
        if not importances:
            return "模型未提供特征重要性"
        ranked = sorted(
            ((abs(weight), weight, name, features.get(name, 0.0)) for name, weight in importances.items()),
            reverse=True,
        )
        labels = []
        for _, weight, name, value in ranked[:limit]:
            direction = "偏正向" if weight * value >= 0 else "偏负向"
            labels.append(f"{feature_label(name)}={value:.3f}（{direction}）")
        return "；".join(labels)

    def _feature_importances(self) -> Dict[str, float]:
        estimator = getattr(self.estimator, "named_steps", {}).get("model", self.estimator)
        if hasattr(estimator, "feature_importances_"):
            raw = list(estimator.feature_importances_)
            return {name: float(raw[idx]) for idx, name in enumerate(self.feature_names)}
        if hasattr(estimator, "coef_"):
            raw = list(estimator.coef_[0])
            return {name: float(raw[idx]) for idx, name in enumerate(self.feature_names)}
        return {}

    def to_json(self) -> str:
        payload = {
            "model_name": self.model_name,
            "baseline_model_name": self.baseline_model_name,
            "feature_names": self.feature_names,
            "positive_rate": self.positive_rate,
            "pickle_b64": base64.b64encode(pickle.dumps(self.estimator)).decode("ascii"),
        }
        if self.baseline_estimator is not None:
            payload["baseline_pickle_b64"] = base64.b64encode(pickle.dumps(self.baseline_estimator)).decode("ascii")
        return json.dumps(payload, ensure_ascii=False)


def feature_label(name: str) -> str:
    return {
        "ret_5": "5日涨幅",
        "ret_10": "10日涨幅",
        "ret_20": "20日涨幅",
        "close_ma5_pct": "偏离MA5",
        "close_ma10_pct": "偏离MA10",
        "close_ma20_pct": "偏离MA20",
        "close_ma60_pct": "偏离MA60",
        "ma5_ma20_pct": "MA5相对MA20",
        "ma20_slope_5": "MA20斜率",
        "volume_ratio_5_20": "5/20日量比",
        "turnover_5": "5日均换手",
        "volatility_20": "20日波动",
        "drawdown_20": "20日回撤",
        "dist_high_20": "距20日高点",
        "rsi_14": "RSI(14)",
        "macd_dif": "MACD快线",
        "macd_dea": "MACD慢线",
        "macd_hist": "MACD柱",
        "boll_pct": "布林带位置",
        "boll_width": "布林带宽度",
        "atr_14": "ATR(14)",
        "consecutive_days": "连续涨跌天数",
        "upper_shadow_pct": "上影线比例",
        "lower_shadow_pct": "下影线比例",
        "body_pct": "实体比例",
        "price_volume_corr_10": "量价相关(10日)",
        "momentum_5_20": "动量差异(5-20)",
        "ret_5_rank": "5日涨幅排名",
        "ret_10_rank": "10日涨幅排名",
        "ret_20_rank": "20日涨幅排名",
        "close_ma20_pct_rank": "偏离MA20排名",
        "volume_ratio_5_20_rank": "量比排名",
        "market_ret_20": "市场20日涨幅",
        "market_breadth_adv_5": "市场上涨宽度(5日)",
        "market_breadth_above_ma20": "市场MA20上方占比",
        "market_breadth_new_high_20": "市场20日新高占比",
        "market_universe_size": "市场样本数",
        "market_member_ret_5": "市场成分5日涨幅",
        "market_member_ret_20": "市场成分20日涨幅",
        "excess_ret_5_vs_market": "5日超额收益(相对市场)",
        "excess_ret_20_vs_market": "20日超额收益(相对市场)",
        "fund_main_net_ratio": "主力净流入占比",
        "fund_main_net_ratio_5": "5日主力净流入占比",
        "fund_main_net_ratio_20": "20日主力净流入占比",
        "fund_main_net_trend": "主力净流入趋势(5-20)",
        "fund_flow_available": "资金流数据可用",
        "sector_ret_5": "板块5日动量",
        "sector_ret_20": "板块20日动量",
        "sector_momentum_5_20": "板块动量差(5-20)",
        "sector_member_ret_5": "板块成分5日涨幅",
        "sector_member_ret_20": "板块成分20日涨幅",
        "excess_ret_5_vs_sector": "5日超额收益(相对板块)",
        "excess_ret_20_vs_sector": "20日超额收益(相对板块)",
        "sector_available": "板块数据可用",
    }.get(name, name)


def pct(a: Optional[float], b: Optional[float]) -> float:
    if a is None or b in (None, 0):
        return 0.0
    return (a / b - 1) * 100


def safe_mean(values: Iterable[Optional[float]]) -> float:
    clean = [float(v) for v in values if v is not None]
    return mean(clean) if clean else 0.0


def moving_average(klines: Sequence[Kline], end_idx: int, days: int) -> float:
    if end_idx + 1 < days:
        return 0.0
    return safe_mean(k.close for k in klines[end_idx - days + 1 : end_idx + 1])


def ema(values: List[float], period: int) -> List[float]:
    """计算指数移动平均，返回与输入等长的列表，前period-1个为简单平均。"""
    if not values or period <= 0:
        return values[:]
    k = 2.0 / (period + 1)
    result: List[float] = []
    for i, v in enumerate(values):
        if i < period - 1:
            result.append(safe_mean(values[max(0, i - period + 1) : i + 1]))
        elif i == period - 1:
            result.append(safe_mean(values[:period]))
        else:
            result.append(v * k + result[-1] * (1 - k))
    return result


def rsi(closes: List[float], period: int = 14) -> float:
    """计算RSI，返回最后一个值。数据不足时返回50.0。"""
    if len(closes) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = safe_mean(gains[:period])
    avg_loss = safe_mean(losses[:period])
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    # Wilder's smoothing
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float, float]:
    """返回最后一个(dif, dea, hist)。数据不足时返回(0,0,0)。"""
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    difs = [f - s for f, s in zip(ema_fast, ema_slow)]
    deas = ema(difs, signal)
    last_dif = difs[-1]
    last_dea = deas[-1]
    return last_dif, last_dea, last_dif - last_dea


def bollinger(closes: List[float], period: int = 20, std_multiplier: float = 2.0) -> tuple[float, float, float]:
    """返回(价格相对布林带位置%, 带宽)。数据不足时返回(50, 0)。
    boll_pct = 0 表示在下轨，100 表示在上轨，50 表示在中轨。
    """
    if len(closes) < period:
        return 50.0, 0.0
    recent = closes[-period:]
    mid = mean(recent)
    std = pstdev(recent) or 1e-9
    upper = mid + std_multiplier * std
    lower = mid - std_multiplier * std
    current = closes[-1]
    boll_pct = (current - lower) / (upper - lower) * 100.0 if upper != lower else 50.0
    boll_width = (upper - lower) / mid * 100.0 if mid else 0.0
    return boll_pct, boll_width


def atr(klines: Sequence[Kline], end_idx: int, period: int = 14) -> float:
    """平均真实波幅，基于end_idx往前period天。"""
    if end_idx < period:
        return 0.0
    trs: List[float] = []
    for i in range(end_idx - period + 1, end_idx + 1):
        if i <= 0:
            continue
        high = klines[i].high
        low = klines[i].low
        prev_close = klines[i - 1].close
        if high is None or low is None or prev_close is None:
            continue
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return safe_mean(trs)


def consecutive_days(klines: Sequence[Kline], end_idx: int) -> int:
    """连续涨跌天数，正数连涨，负数连跌，0表示平。"""
    if end_idx < 1:
        return 0
    count = 0
    direction = 0
    for i in range(end_idx, 0, -1):
        c = klines[i].close
        p = klines[i - 1].close
        if c is None or p is None:
            break
        if c > p:
            d = 1
        elif c < p:
            d = -1
        else:
            d = 0
        if direction == 0:
            direction = d
            if d != 0:
                count = d
        elif d == direction:
            count += d
        else:
            break
    return count


def price_volume_corr(klines: Sequence[Kline], end_idx: int, period: int = 10) -> float:
    """收盘价与成交量的相关系数，范围[-1,1]。"""
    if end_idx + 1 < period:
        return 0.0
    closes = []
    volumes = []
    for i in range(end_idx - period + 1, end_idx + 1):
        if klines[i].close is not None and klines[i].volume is not None:
            closes.append(float(klines[i].close))
            volumes.append(float(klines[i].volume))
    if len(closes) < 3:
        return 0.0
    m_c = mean(closes)
    m_v = mean(volumes)
    s_c = pstdev(closes) or 1e-9
    s_v = pstdev(volumes) or 1e-9
    cov = sum((c - m_c) * (v - m_v) for c, v in zip(closes, volumes)) / len(closes)
    return max(-1.0, min(1.0, cov / (s_c * s_v)))


def features_at(klines: Sequence[Kline], end_idx: int) -> Optional[Dict[str, float]]:
    if end_idx < 60:
        return None
    current = klines[end_idx]
    if current.close is None:
        return None

    ma5 = moving_average(klines, end_idx, 5)
    ma10 = moving_average(klines, end_idx, 10)
    ma20 = moving_average(klines, end_idx, 20)
    ma60 = moving_average(klines, end_idx, 60)
    ma20_prev = moving_average(klines, end_idx - 5, 20)
    closes20 = [k.close for k in klines[end_idx - 19 : end_idx + 1] if k.close is not None]
    highs20 = [k.high for k in klines[end_idx - 19 : end_idx + 1] if k.high is not None]
    lows20 = [k.low for k in klines[end_idx - 19 : end_idx + 1] if k.low is not None]

    ret_values = [pct(current.close, klines[end_idx - days].close) for days in (5, 10, 20)]
    volume5 = safe_mean(k.volume for k in klines[end_idx - 4 : end_idx + 1])
    volume20 = safe_mean(k.volume for k in klines[end_idx - 19 : end_idx + 1])
    returns = []
    for i in range(end_idx - 19, end_idx + 1):
        if i <= 0:
            continue
        returns.append(pct(klines[i].close, klines[i - 1].close))

    high20 = max(highs20) if highs20 else current.close
    low20 = min(lows20) if lows20 else current.close

    # ---- 新增技术指标特征 ----
    # 需要足够长的close序列
    closes_all = [k.close for k in klines[: end_idx + 1] if k.close is not None]
    rsi_14 = rsi(closes_all, 14) if len(closes_all) >= 14 else 50.0
    macd_dif, macd_dea, macd_hist = macd(closes_all, 12, 26, 9) if len(closes_all) >= 35 else (0.0, 0.0, 0.0)
    boll_pct, boll_width = bollinger(closes_all, 20, 2.0) if len(closes_all) >= 20 else (50.0, 0.0)
    atr_14_val = atr(klines, end_idx, 14)
    cons_days = consecutive_days(klines, end_idx)

    # K线形态特征（当日）
    upper_shadow_pct = 0.0
    lower_shadow_pct = 0.0
    body_pct = 0.0
    if current.high is not None and current.low is not None and current.high != current.low:
        body = abs((current.close or 0) - (current.open or current.close or 0))
        upper_shadow = current.high - max(current.close or current.high, current.open or current.high)
        lower_shadow = min(current.close or current.low, current.open or current.low) - current.low
        amplitude = current.high - current.low
        upper_shadow_pct = upper_shadow / amplitude * 100.0
        lower_shadow_pct = lower_shadow / amplitude * 100.0
        body_pct = body / amplitude * 100.0

    # 量价相关性
    pv_corr = price_volume_corr(klines, end_idx, 10)

    # 动量差异（5日 vs 20日动量的差异，反映加速度）
    ret_5_prev = pct(klines[end_idx - 5].close, klines[end_idx - 10].close) if end_idx >= 10 else 0.0
    momentum_5_20 = ret_values[0] - ret_5_prev

    return {
        "ret_5": ret_values[0],
        "ret_10": ret_values[1],
        "ret_20": ret_values[2],
        "close_ma5_pct": pct(current.close, ma5),
        "close_ma10_pct": pct(current.close, ma10),
        "close_ma20_pct": pct(current.close, ma20),
        "close_ma60_pct": pct(current.close, ma60),
        "ma5_ma20_pct": pct(ma5, ma20),
        "ma20_slope_5": pct(ma20, ma20_prev),
        "volume_ratio_5_20": volume5 / volume20 if volume20 else 0.0,
        "turnover_5": safe_mean(k.turnover_rate for k in klines[end_idx - 4 : end_idx + 1]),
        "volatility_20": pstdev(returns) if len(returns) >= 2 else 0.0,
        "drawdown_20": pct(low20, high20),
        "dist_high_20": pct(current.close, high20),
        "rsi_14": rsi_14,
        "macd_dif": macd_dif,
        "macd_dea": macd_dea,
        "macd_hist": macd_hist,
        "boll_pct": boll_pct,
        "boll_width": boll_width,
        "atr_14": atr_14_val,
        "consecutive_days": cons_days,
        "upper_shadow_pct": upper_shadow_pct,
        "lower_shadow_pct": lower_shadow_pct,
        "body_pct": body_pct,
        "price_volume_corr_10": pv_corr,
        "momentum_5_20": momentum_5_20,
    }


def _add_cross_sectional(feature_items: List[tuple[str, Dict[str, float]]]) -> None:
    """Add cross-sectional rank features to feature dicts in-place.

    feature_items: list of (trade_date, features_dict) tuples.
    """
    from collections import defaultdict

    by_date: Dict[str, List[Dict[str, float]]] = defaultdict(list)
    for _date, features in feature_items:
        by_date[_date].append(features)

    rank_features = ["ret_5", "ret_10", "ret_20", "close_ma20_pct", "volume_ratio_5_20"]
    for _date, date_features in by_date.items():
        n = len(date_features)
        for feat in rank_features:
            values = sorted(
                ((f.get(feat, 0.0), f) for f in date_features),
                key=lambda x: x[0],
            )
            start = 0
            while start < n:
                end = start + 1
                while end < n and values[end][0] == values[start][0]:
                    end += 1
                average_rank = (start + end - 1) / 2
                percentile = average_rank / (n - 1) if n > 1 else 0.5
                for _value, features in values[start:end]:
                    features[f"{feat}_rank"] = percentile
                start = end

        ret_20s = [f.get("ret_20", 0.0) for f in date_features]
        market_ret = mean(ret_20s) if ret_20s else 0.0
        for f in date_features:
            f["market_ret_20"] = market_ret


def add_cross_sectional_features(samples: List[TrainingSample]) -> None:
    """In-place addition of cross-sectional rank features to training samples."""
    feature_items = [(s.trade_date, s.features) for s in samples]
    _add_cross_sectional(feature_items)


def add_cross_sectional_to_predictions(prediction_items: List[tuple[str, Dict[str, float]]]) -> None:
    """In-place addition of cross-sectional rank features to prediction feature dicts.

    prediction_items: list of (trade_date, features_dict) tuples.
    """
    _add_cross_sectional(prediction_items)


def label_future(klines: Sequence[Kline], end_idx: int, horizon: int, cfg: Dict[str, Any]) -> Optional[tuple[int, float, float, float]]:
    current = klines[end_idx]
    future = list(klines[end_idx + 1 : end_idx + 1 + horizon])
    if current.close in (None, 0) or len(future) < horizon:
        return None
    highs = [k.high for k in future if k.high is not None]
    lows = [k.low for k in future if k.low is not None]
    closes = [k.close for k in future if k.close is not None]
    if not highs or not lows or not closes:
        return None
    highest_gain = pct(max(highs), current.close)
    max_drawdown = pct(min(lows), current.close)
    high_target = float(cfg.get("positive_high_gain_pct", 0.05)) * 100
    close_target = float(cfg.get("positive_close_gain_pct", 0.0)) * 100
    target_logic = str(cfg.get("positive_target_logic", "all")).lower()

    use_exit_rules = bool(cfg.get("use_trade_exit_rules", False))
    close_gain = pct(closes[-1], current.close)
    if use_exit_rules:
        stop_loss_pct = float(cfg.get("exit_stop_loss_pct", 0.10))
        exit_on_break_ma20 = bool(cfg.get("exit_on_break_ma20", True))
        close_gain = pct(closes[-1], current.close)
        for future_idx in range(end_idx + 1, end_idx + 1 + horizon):
            day = klines[future_idx]
            if day.low is not None and day.low <= float(current.close) * (1 - stop_loss_pct):
                close_gain = -stop_loss_pct * 100
                break
            if exit_on_break_ma20 and day.close is not None:
                ma20 = moving_average(klines, future_idx, 20)
                if ma20 and day.close < ma20:
                    close_gain = pct(day.close, current.close)
                    break
        high_hit = highest_gain >= high_target
        close_hit = close_gain >= close_target
        target_hit = (high_hit or close_hit) if target_logic in {"any", "or"} else (high_hit and close_hit)
        good = target_hit
    else:
        high_hit = highest_gain >= high_target
        close_hit = close_gain >= close_target
        target_hit = (high_hit or close_hit) if target_logic in {"any", "or"} else (high_hit and close_hit)
        good = target_hit and max_drawdown > -float(cfg.get("negative_drawdown_pct", 0.06)) * 100
    return int(good), highest_gain, close_gain, max_drawdown


def build_training_samples(
    klines_by_code: Dict[str, List[Kline]],
    cfg: Dict[str, Any],
) -> List[TrainingSample]:
    horizon = int(cfg.get("horizon_trading_days", 5))
    lookback = int(cfg.get("lookback_trading_days", 60))
    stride = max(1, int(cfg.get("sample_stride", 5)))
    samples: List[TrainingSample] = []
    for code, klines in klines_by_code.items():
        allowed_dates = None
        if cfg.get("weekly_last_trading_day_only", True):
            allowed_dates = weekly_last_trading_days(k.trade_date for k in klines)
        max_idx = len(klines) - horizon - 1
        # Weekly mode already performs temporal downsampling. Applying a second
        # index-based stride here skips valid week ends whenever holidays shift
        # their positions in the per-stock series.
        index_step = 1 if allowed_dates is not None else stride
        for idx in range(lookback, max_idx + 1, index_step):
            if allowed_dates is not None and klines[idx].trade_date not in allowed_dates:
                continue
            features = features_at(klines, idx)
            label = label_future(klines, idx, horizon, cfg)
            if features is None or label is None:
                continue
            y, high, close, drawdown = label
            samples.append(
                TrainingSample(
                    code=code,
                    trade_date=klines[idx].trade_date,
                    features=features,
                    label=y,
                    future_high_gain_pct=high,
                    future_close_gain_pct=close,
                    future_max_drawdown_pct=drawdown,
                )
            )
    if cfg.get("use_cross_sectional_features", True):
        add_cross_sectional_features(samples)
    return samples


def train_centroid_model(samples: List[TrainingSample]) -> CentroidModel:
    feature_names = list(FEATURE_NAMES)
    if not samples:
        return CentroidModel(feature_names, {}, {}, {}, {}, 0.5)

    means: Dict[str, float] = {}
    stds: Dict[str, float] = {}
    for name in feature_names:
        vals = [s.features.get(name, 0.0) for s in samples]
        means[name] = mean(vals)
        stds[name] = pstdev(vals) or 1.0

    def z(sample: TrainingSample, name: str) -> float:
        return (sample.features.get(name, 0.0) - means[name]) / stds[name]

    positives = [s for s in samples if s.label == 1]
    negatives = [s for s in samples if s.label == 0]
    if not positives or not negatives:
        rate = len(positives) / len(samples) if samples else 0.5
        return CentroidModel(feature_names, means, stds, {}, {}, rate)

    positive_centroid = {name: mean(z(s, name) for s in positives) for name in feature_names}
    negative_centroid = {name: mean(z(s, name) for s in negatives) for name in feature_names}
    return CentroidModel(
        feature_names=feature_names,
        means=means,
        stds=stds,
        positive_centroid=positive_centroid,
        negative_centroid=negative_centroid,
        positive_rate=len(positives) / len(samples),
    )


def sample_matrix(samples: List[TrainingSample]) -> tuple[List[List[float]], List[int]]:
    x = [[sample.features.get(name, 0.0) for name in FEATURE_NAMES] for sample in samples]
    y = [sample.label for sample in samples]
    return x, y


def split_samples_by_time(samples: List[TrainingSample], train_ratio: float = 0.7) -> tuple[List[TrainingSample], List[TrainingSample]]:
    ordered = sorted(samples, key=lambda sample: (sample.trade_date, sample.code))
    if len(ordered) < 2:
        return ordered, []
    split_idx = max(1, min(len(ordered) - 1, int(len(ordered) * train_ratio)))
    split_date = ordered[split_idx].trade_date
    while split_idx > 1 and ordered[split_idx - 1].trade_date == split_date:
        split_idx -= 1
    return ordered[:split_idx], ordered[split_idx:]


def evaluate_predictions(
    model_name: str,
    samples: List[TrainingSample],
    probabilities: List[float],
    train_count: int,
    top_k: int,
) -> BacktestMetrics:
    if not samples:
        return BacktestMetrics(model_name, train_count, 0, 0, 0, 0, 0, top_k, 0, 0, 0, 0)
    predicted = [1 if p >= 0.5 else 0 for p in probabilities]
    labels = [s.label for s in samples]
    tp = sum(1 for y, p in zip(labels, predicted) if y == 1 and p == 1)
    fp = sum(1 for y, p in zip(labels, predicted) if y == 0 and p == 1)
    fn = sum(1 for y, p in zip(labels, predicted) if y == 1 and p == 0)
    correct = sum(1 for y, p in zip(labels, predicted) if y == p)
    # The production decision is made independently on each screening date.
    # Selecting one global Top-K across an entire multi-week test period makes
    # the metric depend on probability calibration between weeks and badly
    # overstates a handful of extreme predictions.
    by_date: Dict[str, List[tuple[float, TrainingSample]]] = {}
    for probability, sample in zip(probabilities, samples):
        by_date.setdefault(sample.trade_date, []).append((probability, sample))
    top: List[TrainingSample] = []
    for rows in by_date.values():
        ranked = sorted(rows, key=lambda item: item[0], reverse=True)
        top.extend(sample for _, sample in ranked[: max(1, top_k)])
    return BacktestMetrics(
        model_name=model_name,
        train_count=train_count,
        test_count=len(samples),
        positive_rate=sum(labels) / len(labels),
        accuracy=correct / len(samples),
        precision=tp / (tp + fp) if tp + fp else 0.0,
        recall=tp / (tp + fn) if tp + fn else 0.0,
        top_k=top_k,
        top_k_hit_rate=sum(s.label for s in top) / len(top) if top else 0.0,
        top_k_avg_close_gain_pct=mean(s.future_close_gain_pct for s in top) if top else 0.0,
        top_k_avg_high_gain_pct=mean(s.future_high_gain_pct for s in top) if top else 0.0,
        top_k_avg_max_drawdown_pct=mean(s.future_max_drawdown_pct for s in top) if top else 0.0,
    )


def apply_training_label_overrides(
    samples: List[TrainingSample],
    feedback_labels: Dict[tuple[str, str], int],
    weight: int = 1,
) -> tuple[List[TrainingSample], List[float], Dict[str, int]]:
    if not samples or not feedback_labels:
        return samples, [1.0] * len(samples), {"matched": 0, "relabeled": 0, "extra_weighted": 0}

    use_weight = max(1, int(weight))
    merged: List[TrainingSample] = []
    sample_weights: List[float] = []
    matched = 0
    relabeled = 0
    extra_weighted = 0
    for sample in samples:
        label = feedback_labels.get((str(sample.code), str(sample.trade_date)))
        if label is None:
            merged.append(sample)
            sample_weights.append(1.0)
            continue

        matched += 1
        normalized_label = 1 if int(label) else 0
        if int(sample.label) != normalized_label:
            relabeled += 1
        merged.append(
            TrainingSample(
                code=sample.code,
                trade_date=sample.trade_date,
                features=dict(sample.features),
                label=normalized_label,
                future_high_gain_pct=sample.future_high_gain_pct,
                future_close_gain_pct=sample.future_close_gain_pct,
                future_max_drawdown_pct=sample.future_max_drawdown_pct,
            )
        )
        sample_weights.append(float(use_weight))
        extra_weighted += use_weight - 1
    return merged, sample_weights, {
        "matched": matched,
        "relabeled": relabeled,
        "extra_weighted": extra_weighted,
    }


def backtest_models(
    samples: List[TrainingSample],
    cfg: Dict[str, Any],
    feedback_labels: Optional[Dict[tuple[str, str], int]] = None,
) -> List[BacktestMetrics]:
    train_samples, test_samples = split_samples_by_time(
        samples,
        train_ratio=float(cfg.get("backtest_train_ratio", 0.7)),
    )
    if not train_samples or not test_samples:
        raise RuntimeError("Not enough samples for ML backtest.")
    sample_weights: Optional[List[float]] = None
    if cfg.get("use_review_feedback_labels", False) and feedback_labels:
        train_samples, sample_weights, _stats = apply_training_label_overrides(
            train_samples,
            feedback_labels,
            weight=int(cfg.get("review_feedback_weight", 1)),
        )
    top_k = int(cfg.get("backtest_top_k", 10))
    metrics: List[BacktestMetrics] = []

    baseline_name = str(cfg.get("baseline_model_name", "logistic_regression")).lower()
    if baseline_name == "logistic_regression":
        baseline = train_logistic_regression_model(train_samples, sample_weights=sample_weights)
        baseline_probs = [baseline.predict_probability(s.features) for s in test_samples]
        metrics.append(evaluate_predictions("logistic_regression", test_samples, baseline_probs, len(train_samples), top_k))

    main = train_model(train_samples, cfg, sample_weights=sample_weights)
    main_probs = [main.predict_probability(s.features) for s in test_samples]
    metrics.append(evaluate_predictions(str(cfg.get("model_name", "lightgbm")), test_samples, main_probs, len(train_samples), top_k))
    return metrics


def train_logistic_regression_model(samples: List[TrainingSample], sample_weights: Optional[List[float]] = None) -> SklearnLikeModel:
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except Exception as exc:
        raise RuntimeError(
            "LogisticRegression requires scikit-learn. Install dependencies with: "
            "pip install -r requirements.txt"
        ) from exc

    x, y = sample_matrix(samples)
    if len(set(y)) < 2:
        raise RuntimeError("LogisticRegression needs both positive and negative training samples.")
    estimator = Pipeline(
        [
            ("scaler", StandardScaler()),
            ("model", LogisticRegression(max_iter=1000, class_weight="balanced", random_state=42)),
        ]
    )
    fit_kwargs: Dict[str, Any] = {}
    if sample_weights is not None:
        fit_kwargs["model__sample_weight"] = sample_weights
    estimator.fit(x, y, **fit_kwargs)
    return SklearnLikeModel(
        model_name="logistic_regression",
        feature_names=list(FEATURE_NAMES),
        estimator=estimator,
        positive_rate=sum(y) / len(y),
    )


def train_lightgbm_model(samples: List[TrainingSample], cfg: Dict[str, Any], sample_weights: Optional[List[float]] = None) -> SklearnLikeModel:
    try:
        from lightgbm import LGBMClassifier
    except Exception as exc:
        detail = str(exc)
        if "libomp" in detail:
            raise RuntimeError(
                "LightGBM is installed but macOS OpenMP runtime is missing. "
                "Install it with: brew install libomp"
            ) from exc
        raise RuntimeError(
            "LightGBM requires the lightgbm package. Install dependencies with: "
            "pip install -r requirements.txt"
        ) from exc

    x, y = sample_matrix(samples)
    if len(set(y)) < 2:
        raise RuntimeError("LightGBM needs both positive and negative training samples.")
    estimator = LGBMClassifier(
        n_estimators=int(cfg.get("lightgbm_n_estimators", 160)),
        learning_rate=float(cfg.get("lightgbm_learning_rate", 0.04)),
        num_leaves=int(cfg.get("lightgbm_num_leaves", 31)),
        max_depth=int(cfg.get("lightgbm_max_depth", 6)),
        min_child_samples=int(cfg.get("lightgbm_min_child_samples", 20)),
        subsample=float(cfg.get("lightgbm_subsample", 0.85)),
        colsample_bytree=float(cfg.get("lightgbm_colsample_bytree", 0.85)),
        class_weight="balanced",
        random_state=42,
        n_jobs=1,
        verbosity=-1,
    )
    fit_kwargs: Dict[str, Any] = {}
    if sample_weights is not None:
        fit_kwargs["sample_weight"] = sample_weights
    estimator.fit(x, y, **fit_kwargs)
    baseline = None
    baseline_name = str(cfg.get("baseline_model_name", "logistic_regression"))
    if baseline_name == "logistic_regression":
        baseline = train_logistic_regression_model(samples, sample_weights=sample_weights).estimator
    return SklearnLikeModel(
        model_name="lightgbm",
        baseline_model_name=baseline_name if baseline is not None else None,
        feature_names=list(FEATURE_NAMES),
        estimator=estimator,
        baseline_estimator=baseline,
        positive_rate=sum(y) / len(y),
    )


def train_model(samples: List[TrainingSample], cfg: Dict[str, Any], sample_weights: Optional[List[float]] = None) -> Any:
    model_name = str(cfg.get("model_name", "lightgbm")).lower()
    if model_name == "lightgbm":
        return train_lightgbm_model(samples, cfg, sample_weights=sample_weights)
    if model_name in {"logistic_regression", "logistic"}:
        return train_logistic_regression_model(samples, sample_weights=sample_weights)
    if model_name == "centroid_v1":
        return train_centroid_model(samples)
    raise ValueError(f"Unsupported ml.model_name: {model_name}")
