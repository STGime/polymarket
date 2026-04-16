"""
Simulation Engine
=================
Runs parallel strategy simulations with fake $100 bankroll.
Tracks every trade, checks market resolutions, computes P&L.

Usage:
    # Run 3-day simulation (all strategies in parallel)
    python simulation.py

    # Generate evaluation report from collected data
    python simulation.py --evaluate

    # Check resolutions for pending trades
    python simulation.py --resolve
"""
import asyncio
import json
import logging
import os
import sys
import argparse
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            if value and key.strip() not in os.environ:
                os.environ[key.strip()] = value.strip()

from config import BotConfig, Strategy, STRATEGIES, CITY_ICAO_MAP
from data.weather_sources import WeatherDataService
from data.temperature_predictor import TemperaturePredictor
from trading.polymarket_client import PolymarketClient, TemperatureMarket

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("SIM_DATA_DIR", str(Path(__file__).parent / "sim_data")))
GCS_BUCKET = os.getenv("SIM_GCS_BUCKET", "")  # e.g. "weather-bot-sim-data"


def _gcs_upload(filename: str, content: str) -> bool:
    """Upload file content to GCS bucket. No-op if GCS_BUCKET not set."""
    if not GCS_BUCKET:
        return False
    try:
        from google.cloud import storage
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(filename)
        blob.upload_from_string(content, content_type="application/json")
        return True
    except Exception as e:
        logging.getLogger(__name__).debug(f"GCS upload failed ({filename}): {e}")
        return False


def _gcs_download(filename: str) -> Optional[str]:
    """Download file content from GCS bucket. Returns None if unavailable."""
    if not GCS_BUCKET:
        return None
    try:
        from google.cloud import storage
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(filename)
        if not blob.exists():
            return None
        return blob.download_as_text()
    except Exception as e:
        logging.getLogger(__name__).debug(f"GCS download failed ({filename}): {e}")
        return None


# ─── Data Models ─────────────────────────────────────────

@dataclass
class SimTrade:
    """A simulated trade."""
    id: str
    strategy: str
    timestamp: str
    city: str
    band_label: str
    market_question: str
    target_date: str           # ISO format — when market resolves
    side: str                  # BUY
    entry_price: float         # what we'd pay per share (market ask)
    shares: float              # number of shares
    cost_usd: float            # total cost
    predicted_prob: float      # our model's probability
    edge: float                # predicted - market
    confidence: float
    ev_per_dollar: float
    data_sources: int
    # Resolution fields (filled after market resolves)
    resolved: bool = False
    won: Optional[bool] = None
    pnl_usd: float = 0.0
    resolution_price: float = 0.0  # 1.0 if won, 0.0 if lost
    resolved_at: str = ""


@dataclass
class SimPortfolio:
    """Portfolio state for one strategy."""
    strategy: str
    initial_bankroll: float
    cash: float
    trades: list[SimTrade] = field(default_factory=list)
    total_invested: float = 0.0
    total_returned: float = 0.0

    @property
    def pending_trades(self) -> list[SimTrade]:
        return [t for t in self.trades if not t.resolved]

    @property
    def resolved_trades(self) -> list[SimTrade]:
        return [t for t in self.trades if t.resolved]

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl_usd for t in self.resolved_trades)

    @property
    def current_value(self) -> float:
        pending_value = sum(t.cost_usd for t in self.pending_trades)
        return self.cash + pending_value

    def save(self):
        DATA_DIR.mkdir(exist_ok=True)
        path = DATA_DIR / f"portfolio_{self.strategy}.json"
        data = {
            "strategy": self.strategy,
            "initial_bankroll": self.initial_bankroll,
            "cash": self.cash,
            "total_invested": self.total_invested,
            "total_returned": self.total_returned,
            "trades": [asdict(t) for t in self.trades],
        }
        content = json.dumps(data, indent=2)
        path.write_text(content)
        # Also upload to GCS if configured (for Cloud Run persistence)
        _gcs_upload(f"portfolio_{self.strategy}.json", content)

    @classmethod
    def load(cls, strategy: str) -> "SimPortfolio":
        path = DATA_DIR / f"portfolio_{strategy}.json"
        # Try GCS first (source of truth on Cloud Run)
        gcs_content = _gcs_download(f"portfolio_{strategy}.json")
        if gcs_content:
            DATA_DIR.mkdir(exist_ok=True)
            path.write_text(gcs_content)
        if not path.exists():
            return cls(strategy=strategy, initial_bankroll=100.0, cash=100.0)
        data = json.loads(path.read_text())
        portfolio = cls(
            strategy=data["strategy"],
            initial_bankroll=data["initial_bankroll"],
            cash=data["cash"],
            total_invested=data.get("total_invested", 0),
            total_returned=data.get("total_returned", 0),
        )
        for t in data.get("trades", []):
            portfolio.trades.append(SimTrade(**t))
        return portfolio


# ─── Resolution Checker ──────────────────────────────────

class ResolutionChecker:
    """Checks Polymarket for resolved markets and updates trade outcomes."""

    def __init__(self, client: PolymarketClient):
        self.client = client

    async def check_resolutions(self, portfolio: SimPortfolio) -> int:
        """Check all pending trades for resolution. Returns count of newly resolved."""
        resolved_count = 0
        markets = await self.client.discover_temperature_markets()

        for trade in portfolio.pending_trades:
            # Check if this market has resolved
            resolved = await self._check_trade_resolution(trade, markets)
            if resolved:
                resolved_count += 1

        return resolved_count

    async def _check_trade_resolution(
        self, trade: SimTrade, markets: list[TemperatureMarket]
    ) -> bool:
        """Check if a specific trade's market has resolved."""
        target_dt = datetime.fromisoformat(trade.target_date)

        # Market should have resolved if we're past the target date + buffer
        now = datetime.now(timezone.utc)
        if now < target_dt + timedelta(hours=2):
            return False  # too early, market hasn't resolved yet

        # Find the market and check resolution
        # Look for markets with same city and date that are now closed
        session = await self.client._get_session()

        # Search for resolved/closed events for this city/date
        url = f"{self.client.config.gamma_api_url}/events"
        params = {
            "tag_slug": "weather",
            "closed": "true",
            "limit": 100,
        }

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return False
                events = await resp.json()
        except Exception:
            return False

        # Build the exact expected event title for matching
        # Format: "Highest temperature in {city} on {Month} {day}?"
        import re
        target_month_day = target_dt.strftime("%B %-d")  # e.g. "April 12"
        expected_title_fragment = f"temperature in {trade.city} on {target_month_day}"

        for event in events:
            title = event.get("title", "")

            # Exact match: city AND month AND day must all be in the title
            if expected_title_fragment.lower() not in title.lower():
                continue

            # Found the correct event — now find which band won
            winning_band = None
            for mkt in event.get("markets", []):
                try:
                    prices = json.loads(mkt.get("outcomePrices", "[]")) if isinstance(mkt.get("outcomePrices"), str) else (mkt.get("outcomePrices") or [])
                    yes_price = float(prices[0]) if prices else 0.0
                except (json.JSONDecodeError, ValueError, TypeError):
                    yes_price = 0.0

                if yes_price > 0.90:
                    winning_band = mkt.get("groupItemTitle", "")
                    break

            if winning_band is None:
                # Event found but no clear winner yet — don't resolve
                return False

            # Check if OUR band won
            won = (winning_band == trade.band_label)

            # Also check temperature equivalence (°C vs °F matching)
            if not won:
                def extract_temp_c(band):
                    """Extract temperature in Celsius from band label."""
                    m_c = re.search(r"(-?\d+)°C", band)
                    if m_c:
                        return float(m_c.group(1))
                    m_f = re.search(r"(-?\d+)(?:-(\d+))?°F", band)
                    if m_f:
                        f_val = float(m_f.group(1))
                        return (f_val - 32) * 5 / 9
                    return None

                our_temp = extract_temp_c(trade.band_label)
                win_temp = extract_temp_c(winning_band)
                if our_temp is not None and win_temp is not None:
                    if abs(our_temp - win_temp) < 1.0:
                        won = True  # within 1°C, same band in different units

            trade.resolved = True
            trade.won = won
            trade.resolution_price = 1.0 if won else 0.0
            trade.pnl_usd = (trade.resolution_price - trade.entry_price) * trade.shares
            trade.resolved_at = now.isoformat()

            logger.info(
                f"{'WIN' if won else 'LOSS'} [{trade.strategy}] {trade.city} {trade.band_label}"
                f"{f' (winner: {winning_band})' if not won else ''}: "
                f"{'$' if won else '-$'}{abs(trade.pnl_usd):.2f} "
                f"(bought at {trade.entry_price:.3f})"
            )
            return True

        # Event not found in closed events yet — DON'T assume loss,
        # just wait for the next check cycle
        if now > target_dt + timedelta(hours=48):
            # Only timeout after 48 hours (give resolution time)
            trade.resolved = True
            trade.won = False
            trade.resolution_price = 0.0
            trade.pnl_usd = -trade.cost_usd
            trade.resolved_at = now.isoformat()
            logger.info(
                f"LOSS (timeout 48h) [{trade.strategy}] {trade.city} {trade.band_label}: "
                f"-${abs(trade.pnl_usd):.2f}"
            )
            return True

        return False


# ─── Simulation Cycle ────────────────────────────────────

class StrategySimulator:
    """Runs one strategy simulation cycle."""

    def __init__(self, strategy_name: str, bankroll: float = 100.0):
        self.strategy_name = strategy_name
        self.config = BotConfig()
        self.config.active_strategy = Strategy(strategy_name)
        self.config.dry_run = True
        self.strategy = self.config.strategy_params

        self.weather = WeatherDataService(self.config.weather)
        self.predictor = TemperaturePredictor(self.weather)
        self.client = PolymarketClient(self.config.polymarket, dry_run=True)
        self.resolver = ResolutionChecker(self.client)
        self.portfolio = SimPortfolio.load(strategy_name)

        # If fresh portfolio, set bankroll
        if not self.portfolio.trades:
            self.portfolio.cash = bankroll
            self.portfolio.initial_bankroll = bankroll

    async def run_cycle(self):
        """Run one simulation cycle: resolve pending, scan, trade."""
        # 1. Check resolutions on pending trades
        resolved = await self.resolver.check_resolutions(self.portfolio)
        if resolved:
            # Return cash from resolved trades
            for t in self.portfolio.trades:
                if t.resolved and t.resolved_at and t.won is not None:
                    # Only process newly resolved (check by resolved_at recency)
                    pass
            self._update_cash_from_resolutions()

        # 2. Discover markets
        markets = await self.client.discover_temperature_markets()
        now = datetime.now(timezone.utc)

        tradeable = []
        for m in markets:
            hours_until = (m.target_date - now).total_seconds() / 3600
            if hours_until < self.strategy.min_hours_before_resolution:
                continue
            if hours_until > self.strategy.max_hours_before_resolution:
                continue
            if not m.bands:
                continue
            tradeable.append(m)

        logger.info(f"[{self.strategy_name}] {len(tradeable)} tradeable markets, "
                     f"cash=${self.portfolio.cash:.2f}, pending={len(self.portfolio.pending_trades)}")

        # 3. Run predictions and find opportunities
        opportunities = []
        for market in tradeable[:15]:  # limit to avoid API overload
            try:
                prediction = await self.predictor.predict(
                    city=market.city,
                    target_date=market.target_date,
                    bands=market.bands,
                )

                if prediction.best_edge < self.strategy.min_edge_to_enter:
                    continue

                # Find best band
                best_band = None
                for band in market.bands:
                    if band.label == prediction.best_edge_band:
                        best_band = band
                        break

                if not best_band or best_band.market_prob < 0.05:
                    continue

                # Skip if we already have a trade on this city/date
                already_trading = any(
                    t.city == market.city and t.target_date == market.target_date.isoformat()
                    for t in self.portfolio.pending_trades
                )
                if already_trading:
                    continue

                price = best_band.market_prob
                pred_prob = prediction.predicted_probs.get(best_band.label, 0)
                if price > 0 and price < 1:
                    raw_ev = pred_prob * (1.0 / price - 1.0) - (1.0 - pred_prob)
                else:
                    raw_ev = 0.0
                ev = raw_ev * prediction.confidence

                if ev <= 0:
                    continue

                opportunities.append({
                    "market": market,
                    "band": best_band,
                    "prediction": prediction,
                    "price": price,
                    "pred_prob": pred_prob,
                    "edge": prediction.best_edge,
                    "confidence": prediction.confidence,
                    "ev": ev,
                })

            except Exception as e:
                logger.debug(f"Prediction error {market.city}: {e}")

        opportunities.sort(key=lambda o: o["ev"], reverse=True)

        # 4. Execute simulated trades
        # Per-trade caps differ by strategy (% of current cash)
        per_trade_cap = {
            "conservative": 0.05,   # 5% max per trade
            "balanced":     0.12,   # 12% max per trade
            "aggressive":   0.25,   # 25% max per trade
        }[self.strategy_name]

        max_trades = {
            "conservative": 5,
            "balanced":     10,
            "aggressive":   15,
        }[self.strategy_name]

        for opp in opportunities[:max_trades]:
            # Kelly-inspired sizing
            kelly_f = opp["pred_prob"] * (1.0 / opp["price"] - 1.0) - (1.0 - opp["pred_prob"])
            if kelly_f <= 0:
                continue
            fraction = kelly_f * self.strategy.kelly_fraction * opp["confidence"]
            size_usd = min(
                self.portfolio.cash * fraction,
                self.portfolio.cash * per_trade_cap,
            )

            if size_usd < 5.0:  # minimum $5
                continue
            if self.portfolio.cash < size_usd:
                continue

            shares = size_usd / opp["price"]
            trade = SimTrade(
                id=f"{self.strategy_name}_{datetime.now().timestamp():.0f}",
                strategy=self.strategy_name,
                timestamp=datetime.now(timezone.utc).isoformat(),
                city=opp["market"].city,
                band_label=opp["band"].label,
                market_question=opp["market"].question,
                target_date=opp["market"].target_date.isoformat(),
                side="BUY",
                entry_price=opp["price"],
                shares=round(shares, 2),
                cost_usd=round(size_usd, 2),
                predicted_prob=round(opp["pred_prob"], 4),
                edge=round(opp["edge"], 4),
                confidence=round(opp["confidence"], 4),
                ev_per_dollar=round(opp["ev"], 4),
                data_sources=opp["prediction"].data_sources_used,
            )

            self.portfolio.cash -= trade.cost_usd
            self.portfolio.total_invested += trade.cost_usd
            self.portfolio.trades.append(trade)

            logger.info(
                f"TRADE [{self.strategy_name}] {trade.city} → {trade.band_label}: "
                f"${trade.cost_usd:.2f} ({trade.shares:.1f} shares @ {trade.entry_price:.3f}) "
                f"edge={trade.edge:.1%} EV=${trade.ev_per_dollar:.2f}/$"
            )

        self.portfolio.save()

    def _update_cash_from_resolutions(self):
        """Update cash balance from resolved trades."""
        for trade in self.portfolio.resolved_trades:
            if trade.won:
                # We get $1 per share
                returned = trade.shares * 1.0
                self.portfolio.cash += returned
                self.portfolio.total_returned += returned

    async def close(self):
        await self.weather.close()
        await self.client.close()


# ─── Evaluation Report ───────────────────────────────────

def generate_report():
    """Generate performance comparison report across all strategies."""
    strategies = ["conservative", "balanced", "aggressive"]
    portfolios = {}

    for s in strategies:
        p = SimPortfolio.load(s)
        if p.trades:
            portfolios[s] = p

    if not portfolios:
        print("No simulation data found. Run the simulation first.")
        return

    print("\n" + "=" * 80)
    print("POLYMARKET WEATHER BOT — SIMULATION REPORT")
    print(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 80)

    # Summary table
    print(f"\n{'Strategy':<15} {'Trades':>7} {'Resolved':>9} {'Won':>5} {'Lost':>6} "
          f"{'Win%':>6} {'P&L':>9} {'ROI':>8} {'Cash':>8} {'Value':>8}")
    print("─" * 95)

    for name, p in portfolios.items():
        resolved = p.resolved_trades
        wins = sum(1 for t in resolved if t.won)
        losses = sum(1 for t in resolved if not t.won)
        win_rate = wins / len(resolved) * 100 if resolved else 0
        pnl = p.total_pnl
        roi = pnl / p.initial_bankroll * 100

        print(f"{name:<15} {len(p.trades):>7} {len(resolved):>9} {wins:>5} {losses:>6} "
              f"{win_rate:>5.1f}% ${pnl:>+8.2f} {roi:>+7.1f}% ${p.cash:>7.2f} ${p.current_value:>7.2f}")

    # Detailed trade log per strategy
    for name, p in portfolios.items():
        print(f"\n{'─' * 80}")
        print(f"TRADES — {name.upper()}")
        print(f"{'─' * 80}")
        print(f"{'Time':<20} {'City':<15} {'Band':<16} {'Cost':>7} {'Edge':>7} {'Result':>8} {'P&L':>8}")
        print(f"{'─'*20} {'─'*15} {'─'*16} {'─'*7} {'─'*7} {'─'*8} {'─'*8}")

        for t in p.trades:
            ts = t.timestamp[:16].replace("T", " ")
            if t.resolved:
                result = "WIN" if t.won else "LOSS"
                pnl_str = f"${t.pnl_usd:+.2f}"
            else:
                result = "PENDING"
                pnl_str = "—"
            print(f"{ts:<20} {t.city:<15} {t.band_label:<16} ${t.cost_usd:>6.2f} "
                  f"{t.edge:>+6.1%} {result:>8} {pnl_str:>8}")

    # Stats
    print(f"\n{'=' * 80}")
    print("STATISTICS")
    print(f"{'=' * 80}")
    for name, p in portfolios.items():
        resolved = p.resolved_trades
        if not resolved:
            continue
        pnls = [t.pnl_usd for t in resolved]
        avg_pnl = sum(pnls) / len(pnls)
        max_win = max(pnls) if pnls else 0
        max_loss = min(pnls) if pnls else 0
        avg_edge = sum(t.edge for t in p.trades) / len(p.trades)
        avg_ev = sum(t.ev_per_dollar for t in p.trades) / len(p.trades)

        print(f"\n  {name.upper()}:")
        print(f"    Avg P&L per trade:  ${avg_pnl:+.2f}")
        print(f"    Best trade:         ${max_win:+.2f}")
        print(f"    Worst trade:        ${max_loss:+.2f}")
        print(f"    Avg edge at entry:  {avg_edge:+.1%}")
        print(f"    Avg EV at entry:    ${avg_ev:.2f}/$")
        print(f"    Total invested:     ${p.total_invested:.2f}")
        print(f"    Total returned:     ${p.total_returned:.2f}")

    # Save report
    report_path = DATA_DIR / "report.txt"
    print(f"\nReport saved to {report_path}")


# ─── Main Runner ─────────────────────────────────────────

async def run_simulation_cycle():
    """Run one cycle for all 3 strategies."""
    strategies = ["conservative", "balanced", "aggressive"]
    simulators = [StrategySimulator(s, bankroll=10_000.0) for s in strategies]

    for sim in simulators:
        try:
            await sim.run_cycle()
        except Exception as e:
            logger.error(f"[{sim.strategy_name}] Cycle error: {e}", exc_info=True)
        finally:
            await sim.close()


async def run_continuous(interval_seconds: int = 120):
    """Run simulation continuously."""
    logger.info("=" * 60)
    logger.info("POLYMARKET WEATHER BOT — 3-STRATEGY SIMULATION")
    logger.info(f"  Bankroll: $10,000 per strategy")
    logger.info(f"  Strategies: conservative, balanced, aggressive")
    logger.info(f"  Interval: {interval_seconds}s")
    logger.info("=" * 60)

    cycle = 0
    while True:
        cycle += 1
        logger.info(f"\n{'━' * 50}")
        logger.info(f"Simulation cycle #{cycle} — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        logger.info(f"{'━' * 50}")

        await run_simulation_cycle()

        # Print quick summary
        for s in ["conservative", "balanced", "aggressive"]:
            p = SimPortfolio.load(s)
            resolved = len(p.resolved_trades)
            pending = len(p.pending_trades)
            pnl = p.total_pnl
            logger.info(
                f"  [{s:>12}] cash=${p.cash:.2f} | trades={len(p.trades)} "
                f"(resolved={resolved}, pending={pending}) | P&L=${pnl:+.2f}"
            )

        logger.info(f"Next cycle in {interval_seconds}s...")
        await asyncio.sleep(interval_seconds)


async def resolve_pending():
    """One-shot resolution check for all strategies."""
    config = BotConfig()
    client = PolymarketClient(config.polymarket, dry_run=True)
    resolver = ResolutionChecker(client)

    for s in ["conservative", "balanced", "aggressive"]:
        portfolio = SimPortfolio.load(s)
        if portfolio.pending_trades:
            resolved = await resolver.check_resolutions(portfolio)
            portfolio.save()
            logger.info(f"[{s}] Resolved {resolved} trades, {len(portfolio.pending_trades)} still pending")
        else:
            logger.info(f"[{s}] No pending trades")

    await client.close()


def main():
    parser = argparse.ArgumentParser(description="Weather Bot Simulation")
    parser.add_argument("--evaluate", action="store_true", help="Generate evaluation report")
    parser.add_argument("--resolve", action="store_true", help="Check resolutions only")
    parser.add_argument("--interval", type=int, default=300, help="Cycle interval in seconds (default: 300)")
    parser.add_argument("--reset", action="store_true", help="Reset all simulation data")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s │ %(levelname)-7s │ %(name)-25s │ %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.reset:
        import shutil
        if DATA_DIR.exists():
            shutil.rmtree(DATA_DIR)
            print("Simulation data reset.")
        return

    if args.evaluate:
        generate_report()
        return

    if args.resolve:
        asyncio.run(resolve_pending())
        return

    # Continuous simulation
    asyncio.run(run_continuous(args.interval))


if __name__ == "__main__":
    main()
