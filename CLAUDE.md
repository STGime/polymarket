# Polymarket Weather Trading Bot

## Project Overview
Automated trading bot for Polymarket temperature prediction markets.
Predicts daily high temperatures using 5 weather data sources and compares
against market-implied probabilities to find edges.

## Architecture
- `data/weather_sources.py` — fetches METAR, TAF, ensemble NWP, deterministic, national met APIs
- `data/temperature_predictor.py` — combines sources into probability distribution over temperature bands
- `trading/polymarket_client.py` — discovers markets via Gamma API events endpoint, reads CLOB order books
- `trading/engine.py` — main loop: discover → predict → score → trade → manage positions
- `trading/risk_manager.py` — Kelly criterion sizing, drawdown protection, daily limits
- `simulation.py` — multi-strategy simulation with $10K fake bankroll, resolution tracking
- `sim_server.py` — HTTP dashboard for Cloud Run deployment of simulation

## Data Sources (5)
1. **METAR** — airport weather observations (aviationweather.gov, free)
2. **TAF** — terminal forecasts with TX/TN temp groups (aviationweather.gov, free)
3. **Ensemble NWP** — 139 members across 4 models: ICON(39), GFS(30), ECMWF(50), GEM(20) (Open-Meteo, free)
4. **Deterministic** — Open-Meteo best-estimate daily max/min (free)
5. **National Met Services** — NWS (US), JMA (Japan), Met Office (UK), Bright Sky/DWD (Germany), NEA (Singapore), BOM (Australia)

## Polymarket API Notes
- Market discovery: use **events endpoint** (`/events?tag_slug=weather`), NOT the markets endpoint
- Each temperature event has multiple binary markets (one per band), not one market with multiple outcomes
- The `outcomePrices` field in Gamma API is **stale** — use `bestAsk` field for live prices
- CLOB order books are often empty for individual bands (neg-risk system) — Gamma `bestAsk` is the derived price matching the Polymarket UI
- Must paginate events (100 per page, up to 500) to get all temperature markets (~200 active)
- US cities use Fahrenheit bands, most international cities use Celsius

## Key Calibration Decisions & History

### Model Calibration v1 (initial) — 100% LOSS RATE
**What happened:** 43 trades resolved, all losses. Bot systematically bet on tail events.
- Bet on bands priced at 1-7 cents (1-7% market probability)
- Model claimed 15-35% edge on these tails
- Every single one lost — the market was right every time

**Root causes identified:**
1. `best_edge_band` selected by raw edge (`predicted - market`), which always maximizes at tails
2. Gaussian sigma values too wide (1.0-2.0°C), spreading probability across all bands
3. METAR trend extrapolation from nighttime temps polluted next-day high predictions
4. No minimum market probability threshold — happily bet on 1 cent tokens

### Model Calibration v2 (current fix, 2026-04-12)
**Changes made:**
- **Band selection**: switched from max raw edge to max expected profit with penalty for <5% market bands
- **Minimum bet threshold**: raised from 1% to 5% market price
- **Gaussian sigmas tightened**: national 1.0→0.7, deterministic 1.5→0.8, TAF TX/TN 1.5→0.8
- **METAR uncertainty growth**: 0.3→0.5 per hour from last observation
- **Source weights for next-day**: METAR trend dropped from 5% to 2% weight (nighttime observations are useless for next-day highs)
- **Weight schedule**: added 3-hour bracket (METAR only useful within ~3h)

**v2 result:** Still 1.2% win rate (1/85). Model distributions still too flat — all bands got ~10-12%.
Average entry price improved to 9.7% (from 7.7%) but still betting on non-consensus bands.

### Model Calibration v3 (2026-04-19) — CONCENTRATED DISTRIBUTIONS
**What happened:** 85 trades, 1 win (1.2%). Mean entry price 9.7%, 64% of losses on <10% bands.
Root cause: even with v2 sigma tightening, all 5 sources produced broad distributions.
Combined average was ~10-12% per band → model couldn't distinguish consensus from tails.

**Changes:**
1. **METAR hard cutoff at 6h** — returns None beyond 6 hours (was unbounded σ=12.5°C at 24h)
2. **Sigmas drastically tightened**: national 0.7→0.4, TAF 0.8→0.5, deterministic 0.8→0.45
3. **Ensemble: kernel density (σ=0.3 per member)** replaces Laplace smoothing (α=1 guaranteed 7% floor)
4. **Bayesian market-prior**: blend 65% model + 35% market distribution. Market is right ~90%+, don't ignore it.
5. **Hard 15% minimum market probability** — never bet on bands below 15% (was 5%). Kills all tail bets.
6. **Source weights rebalanced**: METAR 0% for next-day, national 35%
7. **EV penalty removed** — replaced by hard floor, no more linear scaling that amplified tails

**v3 result (2026-04-23):** 7-9% win rate (2W/20-33L per strategy). Entry prices improved
to 20-33% (consensus-adjacent) but still far from the ~25-30% win rate needed to break even.
All three strategies went bankrupt. Cash tracking was also broken — double-counting
win returns every cycle inflated cash to $13K on a $10K bankroll with negative P&L.

**Conclusion:** The prediction-based approach (v1/v2/v3) fundamentally doesn't work.
The Polymarket temperature market is too efficient — the crowd already incorporates
the same public weather data. Our 5 data sources don't provide enough alpha over
what the market already knows.

### Cash Double-Counting Bug (2026-04-23)
`_update_cash_from_resolutions()` iterated ALL resolved trades every cycle, adding
win returns repeatedly. Over 849 cycles, wins were credited hundreds of times.
Fix: recalculate cash from scratch each cycle:
`cash = initial_bankroll - total_cost_all_trades + win_returns`

### Strategy Pivot: Last-Hour Speed Trading (2026-04-23)
**Why prediction failed:** We tried to beat the market at temperature prediction using
the same publicly available data (METAR, ensembles, NWS, etc.). The market already
prices this in. Over 85+ trades across v1-v3, we could not sustain >10% win rate.

**New approach — "Last-Hour Speed":** Instead of predicting temperature, OBSERVE it.
By 2-3pm local time, the daily high is often already reached or nearly reached.
Fresh METAR data (updated every 30 min) tells us the current temperature. If the
thermometer reads 24.8°C at 3pm and conditions are cooling (wind shifting, clouds),
then "25°C" is nearly certain and "27°C or higher" is nearly impossible.

**Edge source:** Not better models — faster reaction to real-time observations.
The market may take 10-60 minutes to fully price in a new METAR observation.

**Other strategies considered but not implemented:**
- Arbitrage (band prices not summing to 100%) — requires live trading
- Sell obvious losers (sell "Yes" on impossible bands) — requires live trading
- Cross-city correlation (London warm → Paris follows) — hard to quantify edge
- Extreme weather only (trade during anomalies) — too few opportunities
- Market behavior patterns (front-run convergence) — needs historical price data

### Cloud Run Ephemeral Storage (2026-04-16)
Cloud Run's filesystem (`/tmp/sim_data`) is ephemeral — container restarts wipe all
simulation data. After ~2 days of running, the container was restarted by Cloud Run
(possibly due to memory pressure or platform updates) and we lost all history.

**Fix:** Added Google Cloud Storage persistence via `google-cloud-storage` SDK.
- Bucket: `gs://weather-bot-sim-data`
- `SimPortfolio.save()` uploads JSON after each local write
- `SimPortfolio.load()` downloads from GCS before reading local file
- Grant Cloud Run default SA `roles/storage.objectAdmin` on the bucket
- Controlled by `SIM_GCS_BUCKET` env var — empty = local-only fallback

### Resolution Checker Bug (2026-04-14) — ALL RESULTS WERE INVALID
**What happened:** After v2 model fix, simulation still showed 0% win rate (77 losses).
Investigation revealed ALL losses were false — the resolution checker had a date-matching bug.

**Root cause:** Line `if target_dt.strftime("%-d") not in title` matched just the day number
(e.g., "12") against ALL months. A trade for "London April 12" was matched against
"London February 12", "London March 12", "London July 12" etc. — all old closed events.
Since the band labels didn't match (different units/bands in old events), the 12-hour
timeout kicked in and marked everything as LOSS.

**Fix:**
- Match on exact `"temperature in {city} on {Month} {day}"` string (includes month)
- Find the actual winning band in the event before resolving
- Add °C/°F equivalence check (within 1°C) for cross-unit matching
- Extended timeout from 12h to 48h (markets need time to officially resolve)
- Don't assume loss when event simply isn't found yet — just wait

**Lesson:** Never trust simulation results without verifying the resolution logic against
known outcomes. A simple date format bug invalidated the entire 3-day simulation.

### Things that are calibration-sensitive (tune with care)
- `sigma` values in `_gaussian_to_bands()` calls — controls how spread out each source's distribution is
- Source weight dicts in `_adjust_weights()` — controls how much each data source matters at different time horizons
- The `0.05` penalty threshold in best-band selection (line in `predict()`) — below this the market is almost always right
- `min_edge_to_enter` per strategy in config.py — currently 6-15%
- Kelly fraction per strategy — currently 0.15-0.40 of full Kelly

### Ensemble member key format
The Open-Meteo API returns keys like `temperature_2m_member01` (no underscore before number).
Original code had `temperature_2m_member_` (with trailing underscore) which matched 0 members.
This single-character bug meant we had 1 member instead of 139 for the entire initial analysis.

### TAF temperature parsing
- AWC API returns `timeFrom`/`timeTo` as Unix timestamps (integers), not ISO strings
- TX/TN temperature groups are in raw TAF text, not in the JSON `temp` field (always empty array)
- Parse with regex: `TX(M?\d+)/(\d{2})(\d{2})Z` and `TN(M?\d+)/(\d{2})(\d{2})Z`
- Only ~9 countries include TX/TN: France, Spain, South Korea, China, Hong Kong, Brazil, Mexico, Argentina
- US, UK, Germany, Japan, Singapore, Australia, Turkey do NOT include TX/TN in TAFs

## Running
```bash
# Analysis
python main.py --analyze-only

# Simulation (local)
python simulation.py --interval 300

# Simulation (Cloud Run dashboard)
python sim_server.py
# Deploy: ./deploy_sim.sh

# View results
# Dashboard: https://weather-bot-sim-551730559911.us-central1.run.app
```

## Environment Variables
- `METOFFICE_API_KEY` — UK Met Office DataHub (free, 360 calls/day)
- `POLYMARKET_PRIVATE_KEY` — only for live trading
- `DRY_RUN` — default true
- `SIM_DATA_DIR` — where simulation saves trade logs (default: ./sim_data, use /tmp/sim_data on Cloud Run)
