from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
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
    entry_price: Optional[float] = None
    entry_trade_date: Optional[str] = None
    exit_at_open: bool = False


def execution_options(cfg: dict) -> dict:
    return {
        "entry_mode": str(cfg.get("entry_mode", "signal_close")),
        "fee_bps_per_side": float(cfg.get("fee_bps_per_side", 0)),
        "slippage_bps_per_side": float(cfg.get("slippage_bps_per_side", 0)),
        "enforce_t_plus_one": bool(cfg.get("enforce_t_plus_one", False)),
        "skip_locked_entry": bool(cfg.get("skip_locked_entry", True)),
    }


def simulation_version(cfg: dict, *, review: bool = False) -> str:
    options = execution_options(cfg)
    if options["entry_mode"] == "signal_close" and not options["fee_bps_per_side"] and not options["slippage_bps_per_side"]:
        return "daily_exit_v1"
    options.update({
        "horizon": cfg.get("horizon_trading_days", 5),
        "high": cfg.get("expected_high_gain_pct" if review else "positive_high_gain_pct", 0.05),
        "close": cfg.get("expected_close_gain_pct" if review else "positive_close_gain_pct", 0.02),
        "stop": cfg.get("stop_loss_pct" if review else "exit_stop_loss_pct", 0.06),
        "logic": cfg.get("positive_target_logic", "any"),
        "exit": cfg.get("use_trade_exit_rules", True),
        "ma_exit": cfg.get("exit_on_break_ma20", False),
    })
    digest = hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()[:16]
    return f"next_open_v2:{digest}"


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
    entry_mode: str = "signal_close",
    fee_bps_per_side: float = 0,
    slippage_bps_per_side: float = 0,
    enforce_t_plus_one: bool = False,
    skip_locked_entry: bool = True,
) -> Optional[TradeOutcome]:
    """Simulate a signal-close or next-open entry using daily candles.

    Percentage settings are decimal fractions (0.05 means 5%). When a daily
    candle touches both stop and target, the stop is assumed to occur first,
    unless the known opening auction already triggered an exit. Next-open
    execution can enforce T+1 and costs; unexecutable end-of-window exits are
    censored (None), while an unexecutable initial entry returns no_entry.
    """
    if horizon <= 0 or end_idx < 0 or end_idx >= len(klines):
        return None
    current = klines[end_idx]
    future = list(klines[end_idx + 1 : end_idx + 1 + horizon])
    if current.close in (None, 0) or len(future) < horizon:
        return None
    if any(row.close is None or row.high is None or row.low is None for row in future):
        return None

    if entry_mode not in {"signal_close", "next_open"}:
        raise ValueError(f"Unknown entry_mode: {entry_mode}")
    if not 0 <= fee_bps_per_side < 10000 or not 0 <= slippage_bps_per_side < 10000:
        raise ValueError("Execution costs must be in [0, 10000) bps per side")
    entry = future[0] if entry_mode == "next_open" else current
    entry_price = entry.open if entry_mode == "next_open" else entry.close
    if entry_price is None or entry_price <= 0:
        return None
    if entry_mode == "next_open" and skip_locked_entry and (
        entry.high == entry.low or (entry.volume is not None and entry.volume <= 0)
    ):
        return TradeOutcome(0, 0, 0, 0, 0, False, False, False, False, False,
                            "no_entry", entry.trade_date)
    base_close = float(entry_price)
    fee = fee_bps_per_side / 10000
    slip = slippage_bps_per_side / 10000
    entry_cost = base_close * (1 + slip) * (1 + fee)
    sell_factor = (1 - slip) * (1 - fee)

    def net_gain(price: float) -> float:
        return _pct(price * sell_factor, entry_cost)
    high_target = float(high_target_pct) * 100.0
    close_target = float(close_target_pct) * 100.0
    stop_loss = float(stop_loss_pct)
    highest_gain = net_gain(max(float(row.high) for row in future if row.high is not None))
    horizon_close_gain = net_gain(float(future[-1].close))
    max_drawdown = net_gain(min(float(row.low) for row in future if row.low is not None))
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
    exit_at_open = False
    high_hit = raw_high_hit

    if use_exit_rules:
        stop_price = entry_cost * (1.0 - stop_loss) / sell_factor
        target_price = entry_cost * (1.0 + float(high_target_pct)) / sell_factor
        high_hit = False
        for future_idx in range(end_idx + 1, end_idx + 1 + horizon):
            day = klines[future_idx]
            if entry_mode == "next_open" and enforce_t_plus_one and future_idx == end_idx + 1:
                if future_idx == end_idx + horizon:
                    return None  # No executable exit inside this holding window.
                continue
            previous_close = klines[future_idx - 1].close
            locked_down = (day.high == day.low and previous_close is not None
                           and float(day.close) < previous_close)
            if entry_mode == "next_open" and (locked_down or day.volume == 0):
                if future_idx == end_idx + horizon:
                    return None  # Censored: daily data cannot establish an executable exit.
                continue
            # The opening auction precedes the unknown intraday high/low order.
            if entry_mode == "next_open" and day.open is not None and float(day.open) <= stop_price:
                realized_gain = net_gain(float(day.open))
                exit_reason, exit_trade_date, stop_triggered = "stop_loss", day.trade_date, True
                exit_at_open = True
                break
            if entry_mode == "next_open" and day.open is not None and float(day.open) >= target_price:
                realized_gain = net_gain(float(day.open))
                exit_reason, exit_trade_date, high_hit = "take_profit", day.trade_date, True
                exit_at_open = True
                break
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
                    realized_gain = net_gain(float(day.close))
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
        entry_price=base_close,
        entry_trade_date=entry.trade_date,
        exit_at_open=exit_at_open,
    )
