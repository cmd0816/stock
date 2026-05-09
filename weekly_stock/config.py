from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


DEFAULT_CONFIG: Dict[str, Any] = {
    "database": {"path": "stocks.db"},
    "paths": {"screening_file": "screening.txt", "download_dir": "downloads"},
    "screening": {"top_n": 4, "min_score": 60, "run_xuangu": False},
    "scoring": {
        "weights": {
            "trend": 30,
            "volume_turnover": 20,
            "breakout": 20,
            "fundamentals": 15,
            "risk": 15,
        },
        "trend": {"ma_short": 5, "ma_mid": 10, "ma_long": 20, "ma_slow": 60},
        "volume_turnover": {"turnover_min": 3, "turnover_max": 20, "volume_ratio_min": 1.2},
        "breakout": {"new_high_days": 10, "strong_gain_days": 3},
        "fundamentals": {"revenue_growth_min": 10, "profit_growth_min": 10},
        "risk": {"max_price": 200, "stop_loss_pct": 0.06, "max_daily_drop_pct": -5},
    },
    "review": {
        "horizon_trading_days": 5,
        "stop_loss_pct": 0.06,
        "expected_high_gain_pct": 0.05,
        "expected_close_gain_pct": 0.02,
    },
    "ml": {
        "enabled": False,
        "model_name": "lightgbm",
        "baseline_model_name": "logistic_regression",
        "history_limit": 320,
        "lookback_trading_days": 60,
        "horizon_trading_days": 5,
        "sample_stride": 5,
        "min_train_samples": 30,
        "positive_high_gain_pct": 0.05,
        "positive_close_gain_pct": 0.0,
        "negative_drawdown_pct": 0.06,
        "rule_score_weight": 0.35,
        "train_on_selected_universe": False,
        "backtest_train_ratio": 0.7,
        "backtest_top_k": 10,
        "lightgbm_n_estimators": 160,
        "lightgbm_learning_rate": 0.04,
        "lightgbm_num_leaves": 15,
        "lightgbm_max_depth": 4,
        "lightgbm_min_child_samples": 20,
        "lightgbm_subsample": 0.85,
        "lightgbm_colsample_bytree": 0.85,
    },
}


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def parse_scalar(value: str) -> Any:
    text = value.strip()
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if text.lower() in {"null", "none"}:
        return None
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text.strip("\"'")


def parse_simple_yaml(text: str) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    stack: list[tuple[int, Dict[str, Any]]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            child: Dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = parse_scalar(value)
    return root


def load_config(path: str | Path) -> Dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        return DEFAULT_CONFIG

    text = config_path.read_text(encoding="utf-8")
    try:
        import yaml

        loaded = yaml.safe_load(text) or {}
    except Exception:
        try:
            loaded = json.loads(text)
        except Exception:
            loaded = parse_simple_yaml(text)
    if not isinstance(loaded, dict):
        raise ValueError(f"Config must be a mapping: {config_path}")
    return deep_merge(DEFAULT_CONFIG, loaded)


def resolve_path(config_path: str | Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return Path(config_path).resolve().parent / path
