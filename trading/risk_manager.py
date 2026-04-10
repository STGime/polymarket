"""
Risk Manager
============
Enforces position sizing, drawdown protection, daily loss limits,
and stop-loss/take-profit logic.
"""
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

from config import RiskConfig, StrategyParams
from trading.polymarket_client import Position

logger = logging.getLogger(__name__)


@dataclass
class TradeProposal:
    """A proposed trade from the trading engine, pending risk approval."""
    token_id: str
    market_label: str
    city: str
    side: str           # BUY or SELL
    price: float
    predicted_prob: float
    edge: float
    confidence: float
    raw_size_usd: float  # size before risk adjustment
    approved: bool = False
    approved_size_usd: float = 0.0
    rejection_reason: str = ""


@dataclass
class DailyStats:
    """Daily trading performance tracker."""
    date: str
    trades_count: int = 0
    pnl_usd: float = 0.0
    wins: int = 0
    losses: int = 0
    total_wagered: float = 0.0
    max_drawdown_usd: float = 0.0
    peak_balance: float = 0.0


class RiskManager:
    """
    Enforces risk limits and position sizing.

    Key responsibilities:
    - Kelly criterion position sizing
    - Maximum exposure limits
    - Daily loss limits
    - Drawdown circuit breaker
    - Stop-loss and take-profit enforcement
    - Correlated position limits (multiple bets on same city)
    """

    def __init__(self, risk_config: RiskConfig, strategy_params: StrategyParams):
        self.config = risk_config
        self.strategy = strategy_params

        # State tracking
        self.current_bankroll = risk_config.initial_bankroll_usd
        self.peak_bankroll = risk_config.initial_bankroll_usd
        self.daily_stats = DailyStats(
            date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            peak_balance=risk_config.initial_bankroll_usd,
        )
        self.trade_log: list[dict] = []
        self.halted = False
        self.halt_reason = ""
        self.halt_until: Optional[datetime] = None

    # ─── Pre-Trade Risk Checks ────────────────────────────────

    def evaluate_proposal(
        self,
        proposal: TradeProposal,
        current_positions: list[Position],
    ) -> TradeProposal:
        """
        Run all risk checks on a trade proposal.
        Returns the proposal with approved=True/False and adjusted sizing.
        """
        # Reset daily stats if new day
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.daily_stats.date != today:
            self._reset_daily_stats(today)

        # Check halt conditions
        if self.halted:
            if self.halt_until and datetime.now(timezone.utc) < self.halt_until:
                proposal.approved = False
                proposal.rejection_reason = f"Trading halted: {self.halt_reason}"
                return proposal
            else:
                self.halted = False
                self.halt_reason = ""
                logger.info("Trading halt lifted — cooldown period expired")

        # Run checks in order (fail fast)
        checks = [
            self._check_daily_loss_limit,
            self._check_daily_trade_count,
            self._check_max_drawdown,
            self._check_edge_threshold,
            self._check_confidence,
            self._check_total_exposure,
            self._check_concurrent_positions,
            self._check_correlated_positions,
            self._check_per_market_limit,
        ]

        for check in checks:
            result = check(proposal, current_positions)
            if result is not None:
                proposal.approved = False
                proposal.rejection_reason = result
                logger.info(
                    f"REJECTED {proposal.market_label}: {result}"
                )
                return proposal

        # All checks passed — calculate position size
        approved_size = self._calculate_position_size(proposal)
        proposal.approved = True
        proposal.approved_size_usd = approved_size

        logger.info(
            f"APPROVED {proposal.market_label}: "
            f"${approved_size:.2f} (edge={proposal.edge:.1%}, "
            f"conf={proposal.confidence:.1%})"
        )
        return proposal

    # ─── Individual Risk Checks ───────────────────────────────

    def _check_daily_loss_limit(
        self, proposal: TradeProposal, positions: list[Position]
    ) -> Optional[str]:
        max_loss = self.current_bankroll * self.config.max_daily_loss_pct
        if self.daily_stats.pnl_usd < -max_loss:
            self._halt_trading(
                f"Daily loss limit hit: ${self.daily_stats.pnl_usd:.2f}",
                hours=4,
            )
            return f"Daily loss limit exceeded (${self.daily_stats.pnl_usd:.2f})"
        return None

    def _check_daily_trade_count(
        self, proposal: TradeProposal, positions: list[Position]
    ) -> Optional[str]:
        if self.daily_stats.trades_count >= self.config.max_daily_trades:
            return f"Daily trade limit reached ({self.config.max_daily_trades})"
        return None

    def _check_max_drawdown(
        self, proposal: TradeProposal, positions: list[Position]
    ) -> Optional[str]:
        drawdown = (self.peak_bankroll - self.current_bankroll) / self.peak_bankroll
        if drawdown >= self.config.max_drawdown_pct:
            self._halt_trading(
                f"Max drawdown hit: {drawdown:.1%}",
                hours=self.config.drawdown_cooldown_hours,
            )
            return f"Max drawdown exceeded ({drawdown:.1%})"
        return None

    def _check_edge_threshold(
        self, proposal: TradeProposal, positions: list[Position]
    ) -> Optional[str]:
        if proposal.edge < self.strategy.min_edge_to_enter:
            return (
                f"Edge too low: {proposal.edge:.1%} "
                f"(need {self.strategy.min_edge_to_enter:.1%})"
            )
        return None

    def _check_confidence(
        self, proposal: TradeProposal, positions: list[Position]
    ) -> Optional[str]:
        # Require higher confidence for larger positions
        min_conf = 0.3  # base minimum
        if proposal.raw_size_usd > self.current_bankroll * 0.05:
            min_conf = 0.5
        if proposal.confidence < min_conf:
            return f"Confidence too low: {proposal.confidence:.1%} (need {min_conf:.1%})"
        return None

    def _check_total_exposure(
        self, proposal: TradeProposal, positions: list[Position]
    ) -> Optional[str]:
        total_exposure = sum(p.size * p.avg_price for p in positions)
        max_exposure = self.current_bankroll * self.strategy.max_total_exposure_pct
        if total_exposure >= max_exposure:
            return (
                f"Total exposure limit: ${total_exposure:.2f} "
                f"(max ${max_exposure:.2f})"
            )
        return None

    def _check_concurrent_positions(
        self, proposal: TradeProposal, positions: list[Position]
    ) -> Optional[str]:
        if len(positions) >= self.config.max_concurrent_positions:
            return f"Max concurrent positions ({self.config.max_concurrent_positions})"
        return None

    def _check_correlated_positions(
        self, proposal: TradeProposal, positions: list[Position]
    ) -> Optional[str]:
        # Count positions in the same city
        city_count = sum(
            1 for p in positions
            if proposal.city.lower() in p.market_label.lower()
        )
        if city_count >= self.config.max_correlated_exposure:
            return (
                f"Correlated exposure limit for {proposal.city}: "
                f"{city_count} positions (max {self.config.max_correlated_exposure})"
            )
        return None

    def _check_per_market_limit(
        self, proposal: TradeProposal, positions: list[Position]
    ) -> Optional[str]:
        if proposal.raw_size_usd > self.config.max_per_market_usd:
            # We'll cap it in sizing, not reject
            pass
        return None

    # ─── Position Sizing ──────────────────────────────────────

    def _calculate_position_size(self, proposal: TradeProposal) -> float:
        """
        Calculate position size using fractional Kelly criterion.

        Kelly formula: f = (p * b - q) / b
        where:
          p = our estimated probability of winning
          b = net odds (payout / cost - 1)
          q = 1 - p
          f = fraction of bankroll to bet
        """
        p = proposal.predicted_prob
        market_price = proposal.price  # cost per share

        if market_price <= 0 or market_price >= 1:
            return 0.0

        # Net odds: if we buy at market_price and it resolves to $1
        b = (1.0 / market_price) - 1.0
        q = 1.0 - p

        # Full Kelly fraction
        kelly_f = (p * b - q) / b if b > 0 else 0.0

        if kelly_f <= 0:
            return 0.0  # negative edge, don't bet

        # Apply fractional Kelly
        fractional_kelly = kelly_f * self.strategy.kelly_fraction

        # Convert to dollar amount
        kelly_size = fractional_kelly * self.current_bankroll

        # Apply all caps
        max_position = self.current_bankroll * self.strategy.max_position_pct
        max_per_market = self.config.max_per_market_usd

        size = min(kelly_size, max_position, max_per_market, proposal.raw_size_usd)

        # Scale by confidence
        size *= proposal.confidence

        # Minimum viable bet ($1)
        if size < 1.0:
            return 0.0

        return round(size, 2)

    # ─── Position Management ──────────────────────────────────

    def check_stop_loss(self, position: Position) -> bool:
        """Check if a position should be stopped out."""
        if position.unrealized_pnl_pct <= -self.strategy.stop_loss_pct * 100:
            logger.warning(
                f"STOP LOSS triggered for {position.market_label}: "
                f"{position.unrealized_pnl_pct:.1f}%"
            )
            return True
        return False

    def check_take_profit(self, position: Position) -> bool:
        """Check if a position should take profit."""
        if position.unrealized_pnl_pct >= self.strategy.take_profit_pct * 100:
            logger.info(
                f"TAKE PROFIT triggered for {position.market_label}: "
                f"{position.unrealized_pnl_pct:.1f}%"
            )
            return True
        return False

    def should_exit_on_edge_decay(
        self, position: Position, current_edge: float
    ) -> bool:
        """Exit if our edge has decayed below the hold threshold."""
        if current_edge < self.strategy.min_edge_to_hold:
            logger.info(
                f"EDGE DECAY exit for {position.market_label}: "
                f"edge={current_edge:.1%} (min hold={self.strategy.min_edge_to_hold:.1%})"
            )
            return True
        return False

    # ─── State Updates ────────────────────────────────────────

    def record_trade(self, pnl: float, wagered: float):
        """Record a completed trade."""
        self.daily_stats.trades_count += 1
        self.daily_stats.pnl_usd += pnl
        self.daily_stats.total_wagered += wagered

        if pnl > 0:
            self.daily_stats.wins += 1
        elif pnl < 0:
            self.daily_stats.losses += 1

        self.current_bankroll += pnl
        if self.current_bankroll > self.peak_bankroll:
            self.peak_bankroll = self.current_bankroll

        self.trade_log.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "pnl": pnl,
            "wagered": wagered,
            "bankroll": self.current_bankroll,
        })

    def update_bankroll(self, balance: float):
        """Update bankroll from actual balance."""
        self.current_bankroll = balance
        if balance > self.peak_bankroll:
            self.peak_bankroll = balance

    def _halt_trading(self, reason: str, hours: float = 24):
        """Halt all trading for a cooldown period."""
        self.halted = True
        self.halt_reason = reason
        self.halt_until = datetime.now(timezone.utc) + timedelta(hours=hours)
        logger.critical(f"🚨 TRADING HALTED: {reason} (until {self.halt_until})")

    def _reset_daily_stats(self, date_str: str):
        """Reset daily stats for a new trading day."""
        logger.info(
            f"Daily summary: PnL=${self.daily_stats.pnl_usd:.2f}, "
            f"W/L={self.daily_stats.wins}/{self.daily_stats.losses}, "
            f"Trades={self.daily_stats.trades_count}"
        )
        self.daily_stats = DailyStats(
            date=date_str,
            peak_balance=self.current_bankroll,
        )

    # ─── Reporting ────────────────────────────────────────────

    def get_status(self) -> dict:
        """Get current risk status summary."""
        drawdown = (
            (self.peak_bankroll - self.current_bankroll) / self.peak_bankroll
            if self.peak_bankroll > 0 else 0
        )

        return {
            "bankroll": self.current_bankroll,
            "peak_bankroll": self.peak_bankroll,
            "drawdown_pct": drawdown * 100,
            "daily_pnl": self.daily_stats.pnl_usd,
            "daily_trades": self.daily_stats.trades_count,
            "daily_win_rate": (
                self.daily_stats.wins / max(1, self.daily_stats.wins + self.daily_stats.losses) * 100
            ),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "strategy": self.strategy.name.value,
        }
