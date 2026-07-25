from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .models import Kline


@dataclass(frozen=True)
class TradeOutcome:
    label: int
    highest_gain_pct: float
    horizon_close_gain_pct: float
    max_drawdown_pct: float
    realized_gain_pct: float
    stop_loss_triggered: bool
    high_target_hit: bool
    close_target_hit: bool
    target_hit: bool
    best_exit_target_hit: bool
    exit_reason: str
    exit_trade_date: str


def _pct(a: float, b: float) -> float:
    return (float(a) / float(b) - 1.0) * 100.0 if b else 0.0


def _moving_average(klines: Sequence[Kline], end_idx: int, period: int) -> Optional[float]:
    if end_idx + 1 < period:
        return None
    closes = [row.close for row in klines[end_idx - period + 1 : end_idx + 1]]
    if any(value is None for value in closes):
        return None
    return sum(float(value) for value in closes if value is not None) / period


def simulate_trade(
    klines: Sequence[Kline],
    end_idx: int,
    horizon: int,
    *,
    high_target_pct: float,
    close_target_pct: float,
    stop_loss_pct: float,
    target_logic: str = "any",
    use_exit_rules: bool = True,
    exit_on_break_ma20: bool = False,
) -> Optional[TradeOutcome]:
    """Simulate one close-to-future trade using daily candles.

    Percentage settings are decimal fractions (0.05 means 5%). When a daily
    candle touches both stop and target, the stop is assumed to occur first.
    """
    if horizon <= 0 or end_idx < 0 or end_idx >= len(klines):
        return None
    current = klines[end_idx]
    future = list(klines[end_idx + 1 : end_idx + 1 + horizon])
    if current.close in (None, 0) or len(future) < horizon:
        return None
    if any(row.close is None or row.high is None or row.low is None for row in future):
        return None

    base_close = float(current.close)
    high_target = float(high_target_pct) * 100.0
    close_target = float(close_target_pct) * 100.0
    stop_loss = float(stop_loss_pct)
    highest_gain = _pct(max(float(row.high) for row in future if row.high is not None), base_close)
    horizon_close_gain = _pct(float(future[-1].close), base_close)
    max_drawdown = _pct(min(float(row.low) for row in future if row.low is not None), base_close)
    raw_high_hit = highest_gain >= high_target
    raw_close_hit = horizon_close_gain >= close_target
    logic_any = str(target_logic).lower() in {"any", "or"}
    best_exit_target_hit = (
        raw_high_hit or raw_close_hit
        if logic_any
        else raw_high_hit and raw_close_hit
    )

    realized_gain = horizon_close_gain
    exit_reason = "horizon"
    exit_trade_date = future[-1].trade_date
    stop_triggered = False
    high_hit = raw_high_hit

    if use_exit_rules:
        stop_price = base_close * (1.0 - stop_loss)
        target_price = base_close * (1.0 + float(high_target_pct))
        high_hit = False
        for future_idx in range(end_idx + 1, end_idx + 1 + horizon):
            day = klines[future_idx]
            stop_hit_today = float(day.low) <= stop_price
            target_hit_today = float(day.high) >= target_price
            if stop_hit_today:
                realized_gain = -stop_loss * 100.0
                exit_reason = "stop_loss"
                exit_trade_date = day.trade_date
                stop_triggered = True
                break
            if target_hit_today:
                realized_gain = high_target
                exit_reason = "take_profit"
                exit_trade_date = day.trade_date
                high_hit = True
                break
            if exit_on_break_ma20:
                ma20 = _moving_average(klines, future_idx, 20)
                if ma20 is not None and float(day.close) < ma20:
                    realized_gain = _pct(float(day.close), base_close)
                    exit_reason = "break_ma20"
                    exit_trade_date = day.trade_date
                    break

    close_hit = realized_gain >= close_target
    target_hit = (high_hit or close_hit) if logic_any else (high_hit and close_hit)
    if use_exit_rules:
        good = target_hit
    else:
        stop_triggered = max_drawdown <= -stop_loss * 100.0
        good = target_hit and not stop_triggered

    return TradeOutcome(
        label=int(good),
        highest_gain_pct=highest_gain,
        horizon_close_gain_pct=horizon_close_gain,
        max_drawdown_pct=max_drawdown,
        realized_gain_pct=realized_gain,
        stop_loss_triggered=stop_triggered,
        high_target_hit=high_hit,
        close_target_hit=close_hit,
        target_hit=target_hit,
        best_exit_target_hit=best_exit_target_hit,
        exit_reason=exit_reason,
        exit_trade_date=exit_trade_date,
    )
