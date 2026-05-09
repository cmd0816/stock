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
    for i in range(end_idx - 18, end_idx + 1):
        if i <= 0:
            continue
        returns.append(pct(klines[i].close, klines[i - 1].close))

    high20 = max(highs20) if highs20 else current.close
    low20 = min(lows20) if lows20 else current.close
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
    }


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
    close_gain = pct(closes[-1], current.close)
    max_drawdown = pct(min(lows), current.close)
    good = (
        highest_gain >= float(cfg.get("positive_high_gain_pct", 0.05)) * 100
        and close_gain >= float(cfg.get("positive_close_gain_pct", 0.0)) * 100
        and max_drawdown > -float(cfg.get("negative_drawdown_pct", 0.06)) * 100
    )
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
        for idx in range(lookback, max_idx + 1, stride):
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
    ranked = sorted(zip(probabilities, samples), key=lambda item: item[0], reverse=True)
    top = [sample for _, sample in ranked[: max(1, top_k)]]
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


def backtest_models(samples: List[TrainingSample], cfg: Dict[str, Any]) -> List[BacktestMetrics]:
    train_samples, test_samples = split_samples_by_time(
        samples,
        train_ratio=float(cfg.get("backtest_train_ratio", 0.7)),
    )
    if not train_samples or not test_samples:
        raise RuntimeError("Not enough samples for ML backtest.")
    top_k = int(cfg.get("backtest_top_k", 10))
    metrics: List[BacktestMetrics] = []

    baseline_name = str(cfg.get("baseline_model_name", "logistic_regression")).lower()
    if baseline_name == "logistic_regression":
        baseline = train_logistic_regression_model(train_samples)
        baseline_probs = [baseline.predict_probability(s.features) for s in test_samples]
        metrics.append(evaluate_predictions("logistic_regression", test_samples, baseline_probs, len(train_samples), top_k))

    main = train_model(train_samples, cfg)
    main_probs = [main.predict_probability(s.features) for s in test_samples]
    metrics.append(evaluate_predictions(str(cfg.get("model_name", "lightgbm")), test_samples, main_probs, len(train_samples), top_k))
    return metrics


def train_logistic_regression_model(samples: List[TrainingSample]) -> SklearnLikeModel:
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
    estimator.fit(x, y)
    return SklearnLikeModel(
        model_name="logistic_regression",
        feature_names=list(FEATURE_NAMES),
        estimator=estimator,
        positive_rate=sum(y) / len(y),
    )


def train_lightgbm_model(samples: List[TrainingSample], cfg: Dict[str, Any]) -> SklearnLikeModel:
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
        num_leaves=int(cfg.get("lightgbm_num_leaves", 15)),
        max_depth=int(cfg.get("lightgbm_max_depth", 4)),
        min_child_samples=int(cfg.get("lightgbm_min_child_samples", 20)),
        subsample=float(cfg.get("lightgbm_subsample", 0.85)),
        colsample_bytree=float(cfg.get("lightgbm_colsample_bytree", 0.85)),
        class_weight="balanced",
        random_state=42,
        n_jobs=1,
        verbosity=-1,
    )
    estimator.fit(x, y)
    baseline = None
    baseline_name = str(cfg.get("baseline_model_name", "logistic_regression"))
    if baseline_name == "logistic_regression":
        baseline = train_logistic_regression_model(samples).estimator
    return SklearnLikeModel(
        model_name="lightgbm",
        baseline_model_name=baseline_name if baseline is not None else None,
        feature_names=list(FEATURE_NAMES),
        estimator=estimator,
        baseline_estimator=baseline,
        positive_rate=sum(y) / len(y),
    )


def train_model(samples: List[TrainingSample], cfg: Dict[str, Any]) -> Any:
    model_name = str(cfg.get("model_name", "lightgbm")).lower()
    if model_name == "lightgbm":
        return train_lightgbm_model(samples, cfg)
    if model_name in {"logistic_regression", "logistic"}:
        return train_logistic_regression_model(samples)
    if model_name == "centroid_v1":
        return train_centroid_model(samples)
    raise ValueError(f"Unsupported ml.model_name: {model_name}")
