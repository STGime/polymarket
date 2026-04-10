"""
Trading Engine
==============
Main orchestrator: discovers markets, runs predictions, evaluates edges,
manages positions, and executes trades.
"""
import asyncio
import signal
import logging
import json
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

from config import BotConfig, STRATEGIES, CITY_ICAO_MAP
from data.weather_sources import WeatherDataService
from data.temperature_predictor import TemperaturePredictor, TempBand, PredictionResult
from trading.polymarket_client import (
    PolymarketClient, TemperatureMarket, OrderBook, Position
)
from trading.risk_manager import RiskManager, TradeProposal

logger = logging.getLogger(__name__)


@dataclass
class MarketOpportunity:
    """A scored trading opportunity."""
    market: TemperatureMarket
    prediction: PredictionResult
    best_band: TempBand
    edge: float
    confidence: float
    expected_value: float  # expected profit per $1 risked, scaled by confidence
    order_book: Optional[OrderBook] = None


class TradingEngine:
    """
    Main trading engine that runs the full loop:
    1. Discover temperature markets on Polymarket
    2. Fetch weather data for each city
    3. Run temperature predictions
    4. Score opportunities by edge × confidence
    5. Submit proposals to risk manager
    6. Execute approved trades
    7. Monitor and manage existing positions
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self.weather = WeatherDataService(config.weather)
        self.predictor = TemperaturePredictor(self.weather)
        self.polymarket = PolymarketClient(config.polymarket, dry_run=config.dry_run)
        self.risk = RiskManager(config.risk, config.strategy_params)

        self.active_markets: list[TemperatureMarket] = []
        self.opportunities: list[MarketOpportunity] = []
        self.positions: list[Position] = []
        self.last_scan_time: Optional[datetime] = None
        self.cycle_count = 0
        self._running = False

    async def start(self):
        """Start the main trading loop."""
        logger.info("=" * 60)
        logger.info(f"🌡️  POLYMARKET WEATHER TRADING BOT")
        logger.info(f"   Strategy: {self.config.active_strategy.value}")
        logger.info(f"   Bankroll: ${self.config.risk.initial_bankroll_usd:.2f}")
        logger.info(f"   Dry Run:  {self.config.dry_run}")
        logger.info("=" * 60)

        self._running = True

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        while self._running:
            try:
                self.cycle_count += 1
                logger.info(f"\n{'─' * 50}")
                logger.info(f"Cycle #{self.cycle_count} — {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
                logger.info(f"{'─' * 50}")

                await self._run_cycle()

                logger.info(f"Next cycle in {self.config.loop_interval_seconds}s")
                await asyncio.sleep(self.config.loop_interval_seconds)

            except KeyboardInterrupt:
                logger.info("Shutting down...")
                self._running = False
            except Exception as e:
                logger.error(f"Cycle error: {e}", exc_info=True)
                await asyncio.sleep(30)  # back off on errors

        await self._shutdown()

    async def stop(self):
        """Signal the bot to stop."""
        self._running = False

    async def _run_cycle(self):
        """Execute one full trading cycle."""
        # Step 1: Discover markets (every 10 cycles or on first run)
        if self.cycle_count == 1 or self.cycle_count % 10 == 0:
            await self._discover_markets()

        # Step 2: Manage existing positions (stop loss / take profit / edge decay)
        await self._manage_positions()

        # Step 3: Check if trading is halted
        if self.risk.halted:
            logger.warning(f"Trading halted: {self.risk.halt_reason}")
            return

        # Step 4: Score opportunities
        self.opportunities = await self._scan_opportunities()

        # Step 5: Execute best opportunities
        await self._execute_trades()

        # Step 6: Log status
        self._log_status()

    # ─── Market Discovery ─────────────────────────────────────

    async def _discover_markets(self):
        """Discover and filter temperature markets."""
        logger.info("Scanning for temperature markets...")
        raw_markets = await self.polymarket.discover_temperature_markets()

        # Filter by liquidity, volume, and time to resolution
        now = datetime.now(timezone.utc)
        strategy = self.config.strategy_params

        filtered = []
        for market in raw_markets:
            hours_until = (market.target_date - now).total_seconds() / 3600

            if hours_until < strategy.min_hours_before_resolution:
                continue
            if hours_until > strategy.max_hours_before_resolution:
                continue
            if market.liquidity_usd < self.config.polymarket.min_liquidity_usd:
                continue
            if not market.active:
                continue
            if not market.bands:
                continue

            filtered.append(market)

        self.active_markets = filtered
        self.last_scan_time = now
        logger.info(
            f"Found {len(filtered)} tradeable markets "
            f"(from {len(raw_markets)} total)"
        )

    # ─── Opportunity Scanning ─────────────────────────────────

    async def _scan_opportunities(self) -> list[MarketOpportunity]:
        """Run predictions and score all market opportunities."""
        opportunities = []

        for market in self.active_markets:
            try:
                # Fetch live CLOB prices for all bands before predicting
                await self._update_live_prices(market)

                prediction = await self.predictor.predict(
                    city=market.city,
                    target_date=market.target_date,
                    bands=market.bands,
                )

                if prediction.best_edge <= 0:
                    continue  # no positive edge

                # Find the best band to bet on
                best_band = None
                for band in market.bands:
                    if band.label == prediction.best_edge_band:
                        best_band = band
                        break

                if not best_band:
                    continue

                # Skip near-zero bands (likely resolved, "In Review", or dead)
                if best_band.market_prob < 0.01:
                    continue

                # Expected profit per $1 risked, scaled by confidence
                # E[profit] = p * (1/price - 1) - (1 - p)  where p = our predicted prob
                price = best_band.market_prob
                pred_prob = prediction.predicted_probs.get(best_band.label, 0)
                if price > 0 and price < 1:
                    raw_ev = pred_prob * (1.0 / price - 1.0) - (1.0 - pred_prob)
                else:
                    raw_ev = 0.0
                ev = raw_ev * prediction.confidence

                # Fetch order book for the best band (for spread check + execution)
                order_book = None
                if best_band.token_id:
                    order_book = await self.polymarket.get_order_book(
                        best_band.token_id
                    )

                opportunities.append(MarketOpportunity(
                    market=market,
                    prediction=prediction,
                    best_band=best_band,
                    edge=prediction.best_edge,
                    confidence=prediction.confidence,
                    expected_value=ev,
                    order_book=order_book,
                ))

            except Exception as e:
                logger.warning(f"Prediction error for {market.city}: {e}")

        # Sort by expected value (best first)
        opportunities.sort(key=lambda o: o.expected_value, reverse=True)

        if opportunities:
            logger.info(f"Found {len(opportunities)} opportunities with positive edge:")
            for i, opp in enumerate(opportunities[:5]):
                h = (opp.market.target_date - datetime.now(timezone.utc)).total_seconds() / 3600
                time_label = f"{h*60:.0f}min" if h < 1 else f"{h:.1f}h" if h < 24 else f"{h/24:.1f}d"
                logger.info(
                    f"  {i+1}. {opp.market.city} — {opp.best_band.label}: "
                    f"edge={opp.edge:.1%}, conf={opp.confidence:.1%}, "
                    f"EV=${opp.expected_value:.2f}/$ (price={opp.best_band.market_prob:.1%}) "
                    f"— {time_label}"
                )

        return opportunities

    # ─── Trade Execution ──────────────────────────────────────

    async def _execute_trades(self):
        """Submit trade proposals for the best opportunities."""
        for opp in self.opportunities:
            if not opp.best_band.token_id:
                continue

            # Determine order price
            price = opp.best_band.market_prob
            if opp.order_book:
                # Skip if spread is too wide (illiquid market)
                if opp.order_book.spread > self.config.risk.max_spread_pct:
                    logger.info(
                        f"Skipping {opp.market.city} {opp.best_band.label}: "
                        f"spread too wide ({opp.order_book.spread:.1%})"
                    )
                    continue
                # Use best ask if buying (slightly above midpoint for fill)
                price = min(
                    opp.order_book.best_ask,
                    opp.order_book.midpoint + self.config.risk.max_slippage_pct,
                )

            # Theoretical max — risk manager's Kelly calculation will size properly
            raw_size = self.risk.current_bankroll * self.config.strategy_params.max_position_pct

            proposal = TradeProposal(
                token_id=opp.best_band.token_id,
                market_label=f"{opp.market.city} {opp.best_band.label}",
                city=opp.market.city,
                side="BUY",
                price=price,
                predicted_prob=opp.prediction.predicted_probs.get(
                    opp.best_band.label, 0
                ),
                edge=opp.edge,
                confidence=opp.confidence,
                raw_size_usd=raw_size,
            )

            # Submit to risk manager
            proposal = self.risk.evaluate_proposal(proposal, self.positions)

            if not proposal.approved:
                continue

            # Calculate number of shares
            shares = proposal.approved_size_usd / price if price > 0 else 0
            if shares < 1:
                continue

            # Execute
            result = await self.polymarket.place_order(
                token_id=proposal.token_id,
                side=proposal.side,
                price=round(price, 2),
                size=round(shares, 1),
            )

            if result.success:
                logger.info(
                    f"✅ Trade executed: {proposal.market_label} — "
                    f"${proposal.approved_size_usd:.2f} "
                    f"({shares:.1f} shares @ ${price:.3f})"
                )
            else:
                logger.warning(
                    f"❌ Trade failed: {proposal.market_label} — {result.error}"
                )

    # ─── Position Management ──────────────────────────────────

    async def _manage_positions(self):
        """Check existing positions for stop-loss, take-profit, or edge decay."""
        self.positions = await self.polymarket.get_positions()

        for position in self.positions:
            # Check stop loss
            if self.risk.check_stop_loss(position):
                await self._close_position(position, reason="STOP_LOSS")
                continue

            # Check take profit
            if self.risk.check_take_profit(position):
                await self._close_position(position, reason="TAKE_PROFIT")
                continue

            # Check edge decay — frequency scales with proximity to resolution
            check_interval = self._edge_check_interval(position)
            if self.cycle_count % check_interval == 0:
                await self._check_edge_decay(position)

    async def _close_position(self, position: Position, reason: str):
        """Close a position by selling."""
        logger.info(
            f"Closing position: {position.market_label} — "
            f"reason={reason}, PnL={position.unrealized_pnl_pct:.1f}%"
        )

        result = await self.polymarket.place_order(
            token_id=position.token_id,
            side="SELL",
            price=round(position.current_price, 2),
            size=position.size,
        )

        if result.success:
            self.risk.record_trade(
                pnl=position.unrealized_pnl,
                wagered=position.size * position.avg_price,
            )

    async def _update_live_prices(self, market: TemperatureMarket):
        """Prices are set from Gamma API bestAsk during discovery (live neg-risk derived).
        No additional CLOB calls needed — bestAsk matches the Polymarket UI price."""
        pass

    def _edge_check_interval(self, position: Position) -> int:
        """More frequent edge checks near resolution."""
        now = datetime.now(timezone.utc)
        for market in self.active_markets:
            for band in market.bands:
                if band.token_id == position.token_id:
                    hours_left = (market.target_date - now).total_seconds() / 3600
                    if hours_left < 2:
                        return 1  # every cycle
                    elif hours_left < 6:
                        return 2
                    else:
                        return 5
        return 5  # default

    async def _check_edge_decay(self, position: Position):
        """Re-evaluate a position's edge and exit if it's decayed."""
        # Find the corresponding market
        for market in self.active_markets:
            for band in market.bands:
                if band.token_id == position.token_id:
                    try:
                        prediction = await self.predictor.predict(
                            city=market.city,
                            target_date=market.target_date,
                            bands=market.bands,
                        )
                        current_edge = prediction.edges.get(band.label, 0)
                        if self.risk.should_exit_on_edge_decay(
                            position, current_edge
                        ):
                            await self._close_position(
                                position, reason="EDGE_DECAY"
                            )
                    except Exception as e:
                        logger.debug(f"Edge decay check error: {e}")
                    return

    # ─── Status Reporting ─────────────────────────────────────

    def _log_status(self):
        """Log current bot status."""
        status = self.risk.get_status()
        logger.info(
            f"Status: Bankroll=${status['bankroll']:.2f} | "
            f"Drawdown={status['drawdown_pct']:.1f}% | "
            f"Day PnL=${status['daily_pnl']:.2f} | "
            f"Positions={len(self.positions)} | "
            f"Opportunities={len(self.opportunities)}"
        )

    def get_full_status(self) -> dict:
        """Get complete bot status for dashboard."""
        return {
            "cycle": self.cycle_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "risk": self.risk.get_status(),
            "markets_tracked": len(self.active_markets),
            "opportunities": [
                {
                    "city": o.market.city,
                    "band": o.best_band.label,
                    "edge": o.edge,
                    "confidence": o.confidence,
                    "ev": o.expected_value,
                    "market_prob": o.best_band.market_prob,
                    "our_prob": o.prediction.predicted_probs.get(o.best_band.label, 0),
                    "spread": o.prediction.ensemble_spread_c,
                    "sources": o.prediction.data_sources_used,
                }
                for o in self.opportunities[:10]
            ],
            "positions": [
                {
                    "label": p.market_label,
                    "size": p.size,
                    "avg_price": p.avg_price,
                    "current_price": p.current_price,
                    "pnl_pct": p.unrealized_pnl_pct,
                    "pnl_usd": p.unrealized_pnl,
                }
                for p in self.positions
            ],
            "config": {
                "strategy": self.config.active_strategy.value,
                "dry_run": self.config.dry_run,
                "bankroll": self.config.risk.initial_bankroll_usd,
            },
        }

    async def _shutdown(self):
        """Graceful shutdown with timeout."""
        logger.info("Shutting down — cancelling open orders...")
        try:
            await asyncio.wait_for(self.polymarket.cancel_all_orders(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("Cancel orders timed out after 10s")
        await self.weather.close()
        await self.polymarket.close()
        logger.info("Shutdown complete")


# ─── Entry point for direct execution ─────────────────────

async def run_bot(config: BotConfig):
    """Convenience function to run the bot."""
    engine = TradingEngine(config)
    await engine.start()
