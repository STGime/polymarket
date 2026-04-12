#!/usr/bin/env python3
"""
Polymarket Weather Trading Bot
===============================
An automated trading system that bets on temperature prediction markets
using aviation weather data (METAR/TAF) and ensemble NWP models.

Usage:
    # Dry run with balanced strategy (default)
    python main.py

    # Live trading with conservative strategy
    python main.py --strategy conservative --live

    # Aggressive strategy with custom bankroll
    python main.py --strategy aggressive --bankroll 5000

    # Single analysis run (no trading loop)
    python main.py --analyze-only

Environment variables:
    POLYMARKET_PRIVATE_KEY    - Wallet private key for trading
    POLYMARKET_FUNDER_ADDRESS - Funder address (for proxy wallets)
    AVWX_API_KEY              - Optional AVWX API key
    BANKROLL_USD              - Starting bankroll (default: 1000)
    DRY_RUN                   - "true" or "false" (default: true)
"""
import sys
import os
import asyncio
import argparse
import logging
import json
import traceback
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env file if present
from pathlib import Path
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            if value and key.strip() not in os.environ:
                os.environ[key.strip()] = value.strip()

from config import BotConfig, Strategy, STRATEGIES
from trading.engine import TradingEngine


class JsonFormatter(logging.Formatter):
    """JSON log formatter for Cloud Logging compatibility."""

    def format(self, record):
        log_entry = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = traceback.format_exception(*record.exc_info)
        return json.dumps(log_entry)


def setup_logging(level: str = "INFO", log_file: str = "trading_bot.log"):
    """Configure logging. Uses JSON to stdout on Cloud Run, console+file locally."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    if os.getenv("K_SERVICE"):
        # Cloud Run: JSON structured logs to stdout only
        handler = logging.StreamHandler()
        handler.setLevel(getattr(logging, level))
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    else:
        # Local: console + file
        log_format = "%(asctime)s │ %(levelname)-7s │ %(name)-25s │ %(message)s"
        date_format = "%H:%M:%S"

        console = logging.StreamHandler()
        console.setLevel(getattr(logging, level))
        console.setFormatter(logging.Formatter(log_format, datefmt=date_format))

        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")
        )

        root.addHandler(console)
        root.addHandler(file_handler)


async def run_analysis(config: BotConfig):
    """Run a single analysis cycle without trading."""
    logger = logging.getLogger("analysis")
    engine = TradingEngine(config)

    logger.info("Running single analysis cycle...")
    logger.info(f"Strategy: {config.active_strategy.value}")

    # Discover markets
    await engine._discover_markets()

    if not engine.active_markets:
        logger.warning("No temperature markets found. Markets may not be active.")
        logger.info("Try checking https://polymarket.com/climate-science/weather")
        await engine._shutdown()
        return

    logger.info(f"\n{'=' * 60}")
    logger.info(f"MARKET ANALYSIS — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    logger.info(f"{'=' * 60}")

    # Analyze each market
    results = []
    for market in engine.active_markets[:10]:  # top 10
        try:
            # Fetch live CLOB prices before predicting
            await engine._update_live_prices(market)

            prediction = await engine.predictor.predict(
                city=market.city,
                target_date=market.target_date,
                bands=market.bands,
            )

            hours_until = (
                market.target_date - datetime.now(timezone.utc)
            ).total_seconds() / 3600

            logger.info(f"\n{'─' * 50}")
            logger.info(f"📍 {market.city} — {market.question}")
            logger.info(f"   Resolves in: {hours_until:.1f} hours")
            logger.info(f"   Data sources: {prediction.data_sources_used}")
            logger.info(f"   Confidence: {prediction.confidence:.1%}")
            logger.info(f"   Ensemble spread: {prediction.ensemble_spread_c:.1f}°C")
            if prediction.metar_trend_c_per_hr is not None:
                logger.info(f"   METAR trend: {prediction.metar_trend_c_per_hr:+.2f}°C/hr")

            logger.info(f"\n   {'Band':<16} {'Market':>8} {'Ours':>8} {'Edge':>8}")
            logger.info(f"   {'─'*16} {'─'*8} {'─'*8} {'─'*8}")

            for band in market.bands:
                our_prob = prediction.predicted_probs.get(band.label, 0)
                edge = prediction.edges.get(band.label, 0)
                marker = " ◀━" if band.label == prediction.best_edge_band and edge > 0.05 else ""

                logger.info(
                    f"   {band.label:<16} "
                    f"{band.market_prob:>7.1%} "
                    f"{our_prob:>7.1%} "
                    f"{edge:>+7.1%}{marker}"
                )

            if prediction.best_edge > 0:
                best_band_obj = None
                for b in market.bands:
                    if b.label == prediction.best_edge_band:
                        best_band_obj = b
                        break
                price = best_band_obj.market_prob if best_band_obj else 0
                # Skip low-probability bands — market is usually right about tails
                if price < 0.05:
                    continue
                pred_prob = prediction.predicted_probs.get(prediction.best_edge_band, 0)
                if price > 0 and price < 1:
                    raw_ev = pred_prob * (1.0 / price - 1.0) - (1.0 - pred_prob)
                else:
                    raw_ev = 0.0
                ev = raw_ev * prediction.confidence

                results.append({
                    "city": market.city,
                    "band": prediction.best_edge_band,
                    "edge": prediction.best_edge,
                    "confidence": prediction.confidence,
                    "price": price,
                    "ev": ev,
                    "resolves": market.target_date,
                    "hours_until": hours_until,
                })

        except Exception as e:
            logger.warning(f"Analysis error for {market.city}: {e}")

    # Summary
    if results:
        results.sort(key=lambda r: r["ev"], reverse=True)
        logger.info(f"\n{'=' * 60}")
        logger.info("TOP OPPORTUNITIES")
        logger.info(f"{'=' * 60}")
        for i, r in enumerate(results[:5]):
            resolve_str = r["resolves"].strftime("%b %-d %H:%M UTC")
            h = r["hours_until"]
            if h < 1:
                time_label = f"{h*60:.0f}min"
            elif h < 24:
                time_label = f"{h:.1f}h"
            else:
                time_label = f"{h/24:.1f}d"
            logger.info(
                f"  {i+1}. {r['city']} → {r['band']}: "
                f"edge={r['edge']:.1%}, conf={r['confidence']:.1%}, "
                f"EV=${r['ev']:.2f}/$ (price={r['price']:.1%}) "
                f"— resolves {resolve_str} ({time_label})"
            )
    else:
        logger.info("\nNo positive-edge opportunities found in current markets.")

    # Save analysis to file
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "strategy": config.active_strategy.value,
        "markets_analyzed": len(engine.active_markets),
        "opportunities": results,
    }
    with open("analysis_output.json", "w") as f:
        json.dump(output, f, indent=2)
    logger.info(f"\nFull analysis saved to analysis_output.json")

    await engine._shutdown()


def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Weather Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--strategy",
        choices=["conservative", "balanced", "aggressive"],
        default="balanced",
        help="Trading strategy (default: balanced)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Enable live trading (default: dry run)",
    )
    parser.add_argument(
        "--bankroll",
        type=float,
        default=None,
        help="Starting bankroll in USD (default: $1000 or BANKROLL_USD env var)",
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="Run single analysis cycle, no trading loop",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=120,
        help="Main loop interval in seconds (default: 120)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level (default: INFO)",
    )

    args = parser.parse_args()

    # Build configuration
    config = BotConfig()
    config.active_strategy = Strategy(args.strategy)
    config.dry_run = not args.live
    config.loop_interval_seconds = args.interval
    config.log_level = args.log_level

    if args.bankroll:
        config.risk.initial_bankroll_usd = args.bankroll

    setup_logging(config.log_level, config.log_file)
    logger = logging.getLogger("main")

    if args.live:
        logger.warning("⚠️  LIVE TRADING MODE — Real money will be used!")
        if not config.polymarket.private_key:
            logger.error(
                "POLYMARKET_PRIVATE_KEY not set. "
                "Set it to enable live trading."
            )
            sys.exit(1)
    else:
        logger.info("🧪 DRY RUN MODE — No real trades will be placed")

    # Run
    if args.analyze_only:
        asyncio.run(run_analysis(config))
    else:
        asyncio.run(run_with_health(config))


async def run_with_health(config: BotConfig):
    """Run the trading engine with health server for Cloud Run."""
    engine = TradingEngine(config)

    from health_server import HealthServer
    port = int(os.getenv("PORT", "8080"))
    health = HealthServer(engine, port=port)
    await health.start()

    try:
        await engine.start()
    except KeyboardInterrupt:
        logging.getLogger("main").info("Bot stopped by user")
    finally:
        await health.stop()


if __name__ == "__main__":
    main()
