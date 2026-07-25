from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CandidateStock:
    code: str
    name: Optional[str]
    batch_id: str
    row_json: Dict[str, Any]


@dataclass
class Kline:
    trade_date: str
    open: Optional[float]
    close: Optional[float]
    high: Optional[float]
    low: Optional[float]
    volume: Optional[float]
    turnover_rate: Optional[float]
    change_percent: Optional[float]


@dataclass
class ScoreBreakdown:
    trend: float = 0
    volume_turnover: float = 0
    breakout: float = 0
    fundamentals: float = 0
    risk: float = 0
    tie_breaker: float = 0
    reasons: List[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return self.trend + self.volume_turnover + self.breakout + self.fundamentals + self.risk + self.tie_breaker


@dataclass
class ScoredStock:
    candidate: CandidateStock
    score: ScoreBreakdown
    selected_reason: str


@dataclass
class ReviewResult:
    code: str
    name: Optional[str]
    base_trade_date: Optional[str]
    review_start_date: Optional[str]
    review_end_date: Optional[str]
    highest_gain_pct: Optional[float]
    close_gain_pct: Optional[float]
    max_drawdown_pct: Optional[float]
    stop_loss_triggered: bool
    meets_expectation: bool
    best_exit_meets_expectation: bool
    is_complete: bool
    notes: str
