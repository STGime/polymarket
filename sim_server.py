"""
Simulation HTTP Server
======================
Runs the multi-strategy simulation in a background task and exposes
HTTP endpoints so you can view results from anywhere.

Endpoints:
  GET /              → HTML dashboard with strategy comparison
  GET /health        → Cloud Run liveness probe
  GET /report        → Text report (like `python simulation.py --evaluate`)
  GET /trades        → JSON list of all trades
  GET /portfolio/<strategy>  → JSON portfolio for a specific strategy
  POST /reset        → Reset all simulation data
"""
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

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

from aiohttp import web

from simulation import (
    run_simulation_cycle, SimPortfolio, DATA_DIR,
)

logger = logging.getLogger(__name__)


# ─── Background Simulation Loop ──────────────────────────

class SimulationRunner:
    def __init__(self, interval_seconds: int = 300):
        self.interval = interval_seconds
        self.cycle_count = 0
        self.last_cycle_time = None
        self.last_error = None
        self._task = None
        self._running = False

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self):
        while self._running:
            try:
                self.cycle_count += 1
                logger.info(f"━━━ Cycle #{self.cycle_count} ━━━")
                await run_simulation_cycle()
                self.last_cycle_time = datetime.now(timezone.utc).isoformat()
                self.last_error = None
            except Exception as e:
                logger.error(f"Simulation cycle error: {e}", exc_info=True)
                self.last_error = str(e)

            await asyncio.sleep(self.interval)


# ─── HTTP Handlers ───────────────────────────────────────

async def handle_health(request):
    return web.json_response({"status": "ok"})


async def handle_report(request):
    """Return plaintext performance report."""
    lines = []
    lines.append("=" * 80)
    lines.append("POLYMARKET WEATHER BOT — SIMULATION REPORT")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    runner: SimulationRunner = request.app["runner"]
    lines.append(f"Cycles run: {runner.cycle_count}")
    lines.append(f"Last cycle: {runner.last_cycle_time or 'never'}")
    if runner.last_error:
        lines.append(f"Last error: {runner.last_error}")
    lines.append("=" * 80)

    strategies = ["conservative", "balanced", "aggressive"]
    portfolios = {s: SimPortfolio.load(s) for s in strategies}

    lines.append("")
    lines.append(f"{'Strategy':<15} {'Trades':>7} {'Resolved':>9} {'Won':>5} {'Lost':>6} "
                 f"{'Win%':>6} {'P&L':>10} {'ROI':>8} {'Cash':>10}")
    lines.append("─" * 95)

    for name, p in portfolios.items():
        resolved = p.resolved_trades
        wins = sum(1 for t in resolved if t.won)
        losses = sum(1 for t in resolved if not t.won)
        win_rate = wins / len(resolved) * 100 if resolved else 0
        pnl = p.total_pnl
        roi = pnl / p.initial_bankroll * 100 if p.initial_bankroll else 0

        lines.append(
            f"{name:<15} {len(p.trades):>7} {len(resolved):>9} {wins:>5} {losses:>6} "
            f"{win_rate:>5.1f}% ${pnl:>+9.2f} {roi:>+7.1f}% ${p.cash:>9.2f}"
        )

    # Trade details per strategy
    for name, p in portfolios.items():
        lines.append("")
        lines.append("─" * 80)
        lines.append(f"TRADES — {name.upper()}")
        lines.append("─" * 80)
        if not p.trades:
            lines.append("(no trades yet)")
            continue
        lines.append(f"{'Time':<20} {'City':<15} {'Band':<18} {'Cost':>9} "
                     f"{'Edge':>7} {'Result':>8} {'P&L':>10}")
        lines.append(f"{'─'*20} {'─'*15} {'─'*18} {'─'*9} {'─'*7} {'─'*8} {'─'*10}")
        for t in p.trades[-30:]:  # last 30 trades
            ts = t.timestamp[:16].replace("T", " ")
            if t.resolved:
                result = "WIN" if t.won else "LOSS"
                pnl_str = f"${t.pnl_usd:+.2f}"
            else:
                result = "PENDING"
                pnl_str = "—"
            lines.append(
                f"{ts:<20} {t.city:<15} {t.band_label:<18} ${t.cost_usd:>8.2f} "
                f"{t.edge:>+6.1%} {result:>8} {pnl_str:>10}"
            )

    return web.Response(text="\n".join(lines), content_type="text/plain")


async def handle_trades(request):
    """Return all trades as JSON."""
    all_trades = []
    for s in ["conservative", "balanced", "aggressive"]:
        p = SimPortfolio.load(s)
        for t in p.trades:
            all_trades.append({
                "strategy": s,
                "timestamp": t.timestamp,
                "city": t.city,
                "band": t.band_label,
                "entry_price": t.entry_price,
                "cost_usd": t.cost_usd,
                "edge": t.edge,
                "ev": t.ev_per_dollar,
                "resolved": t.resolved,
                "won": t.won,
                "pnl_usd": t.pnl_usd,
            })
    return web.json_response(all_trades)


async def handle_portfolio(request):
    """Return one strategy's portfolio JSON."""
    strategy = request.match_info["strategy"]
    if strategy not in ["conservative", "balanced", "aggressive"]:
        return web.json_response({"error": "invalid strategy"}, status=400)
    p = SimPortfolio.load(strategy)
    return web.json_response({
        "strategy": p.strategy,
        "initial_bankroll": p.initial_bankroll,
        "cash": p.cash,
        "total_invested": p.total_invested,
        "total_returned": p.total_returned,
        "total_pnl": p.total_pnl,
        "trades_total": len(p.trades),
        "trades_pending": len(p.pending_trades),
        "trades_resolved": len(p.resolved_trades),
        "current_value": p.current_value,
    })


async def handle_dashboard(request):
    """HTML dashboard."""
    runner: SimulationRunner = request.app["runner"]
    strategies = ["conservative", "balanced", "aggressive"]
    portfolios = {s: SimPortfolio.load(s) for s in strategies}

    rows = []
    for name, p in portfolios.items():
        resolved = p.resolved_trades
        wins = sum(1 for t in resolved if t.won)
        losses = sum(1 for t in resolved if not t.won)
        win_rate = wins / len(resolved) * 100 if resolved else 0
        pnl = p.total_pnl
        roi = pnl / p.initial_bankroll * 100 if p.initial_bankroll else 0
        pnl_color = "#0a7" if pnl >= 0 else "#d33"

        rows.append(f"""
        <tr>
            <td><strong>{name}</strong></td>
            <td>{len(p.trades)}</td>
            <td>{len(resolved)}</td>
            <td>{wins}</td>
            <td>{losses}</td>
            <td>{win_rate:.1f}%</td>
            <td style="color:{pnl_color}"><strong>${pnl:+.2f}</strong></td>
            <td style="color:{pnl_color}">{roi:+.1f}%</td>
            <td>${p.cash:.2f}</td>
        </tr>
        """)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Polymarket Weather Bot Simulation</title>
    <meta http-equiv="refresh" content="60">
    <style>
        body {{ font-family: -apple-system, sans-serif; max-width: 1200px; margin: 40px auto; padding: 20px; }}
        h1 {{ color: #333; }}
        .meta {{ color: #666; font-size: 14px; margin-bottom: 20px; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #f5f5f5; font-weight: 600; }}
        .links {{ margin-top: 20px; }}
        .links a {{ margin-right: 15px; color: #07c; text-decoration: none; }}
        .links a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <h1>🌡️ Polymarket Weather Bot Simulation</h1>
    <div class="meta">
        Cycles: {runner.cycle_count} |
        Last cycle: {runner.last_cycle_time or 'never'} |
        Interval: {runner.interval}s
        {f'| <span style="color:#d33">Error: {runner.last_error}</span>' if runner.last_error else ''}
    </div>
    <table>
        <thead>
            <tr>
                <th>Strategy</th>
                <th>Trades</th>
                <th>Resolved</th>
                <th>Won</th>
                <th>Lost</th>
                <th>Win Rate</th>
                <th>P&L</th>
                <th>ROI</th>
                <th>Cash</th>
            </tr>
        </thead>
        <tbody>
            {''.join(rows)}
        </tbody>
    </table>
    <div class="links">
        <a href="/report">📄 Full Report</a>
        <a href="/trades">📊 All Trades (JSON)</a>
        <a href="/health">❤️ Health</a>
    </div>
    <p style="color:#999;font-size:12px;margin-top:40px">Auto-refreshes every 60s. Started with $10,000 per strategy.</p>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


# ─── App Factory ─────────────────────────────────────────

async def create_app():
    app = web.Application()
    runner = SimulationRunner(interval_seconds=int(os.getenv("SIM_INTERVAL", "300")))
    app["runner"] = runner

    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/report", handle_report)
    app.router.add_get("/trades", handle_trades)
    app.router.add_get("/portfolio/{strategy}", handle_portfolio)

    # Start background simulation
    async def on_startup(app):
        await runner.start()
        logger.info("Background simulation loop started")

    async def on_cleanup(app):
        await runner.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ %(name)-25s │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    port = int(os.getenv("PORT", "8080"))
    web.run_app(create_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
