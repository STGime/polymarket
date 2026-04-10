# Polymarket Weather Trading Bot

An automated trading system that bets on temperature prediction markets on Polymarket,
using aviation weather data and ensemble NWP models to find edges over market-implied probabilities.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        TRADING ENGINE                           │
│                                                                 │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────┐    │
│  │  Market       │   │ Temperature  │   │  Risk Manager    │    │
│  │  Discovery    │──▶│  Predictor   │──▶│  + Kelly Sizing  │    │
│  │  (Gamma API)  │   │              │   │                  │    │
│  └──────────────┘   └──────┬───────┘   └────────┬─────────┘    │
│                            │                     │              │
│                   ┌────────┴────────┐   ┌────────▼─────────┐   │
│                   │  DATA SOURCES   │   │  ORDER EXECUTION │   │
│                   │                 │   │  (CLOB API)      │   │
│                   │  • METAR (30m)  │   │                  │   │
│                   │  • TAF   (3h)   │   │  • Limit orders  │   │
│                   │  • ICON ens.    │   │  • Stop loss     │   │
│                   │  • GFS ens.     │   │  • Take profit   │   │
│                   │  • ECMWF ens.   │   │  • Edge decay    │   │
│                   │  • GEM ens.     │   │                  │   │
│                   └─────────────────┘   └──────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## Data Sources (all free, no auth required)

| Source | Update Freq | What It Provides | API |
|---|---|---|---|
| **METAR** | Every 30 min | Ground truth temperature observations from airport stations worldwide | aviationweather.gov/api/data/metar |
| **TAF** | Every 3 hours | Professional meteorologist forecasts with probability qualifiers | aviationweather.gov/api/data/taf |
| **ICON Ensemble** | Every 6 hours | 40 ensemble members from German DWD model | ensemble-api.open-meteo.com |
| **GFS Ensemble** | Every 6 hours | 31 ensemble members from NOAA NCEP model | ensemble-api.open-meteo.com |
| **ECMWF IFS** | Every 6 hours | 51 ensemble members from European model | ensemble-api.open-meteo.com |
| **GEM Global** | Every 12 hours | 21 ensemble members from Canadian model | ensemble-api.open-meteo.com |

## How It Works

### 1. Market Discovery
Scans Polymarket's Gamma API for active "Highest temperature in {city} on {date}?" markets.
Parses question text to extract city/date, maps to ICAO airport codes.

### 2. Multi-Source Prediction
For each market, fetches aviation weather data and ensemble forecasts concurrently:
- **METAR trend**: Linear regression on recent observations, extrapolated with diurnal correction
- **TAF forecast**: Professional meteorologist's probability-weighted forecast
- **Ensemble distribution**: Empirical distribution from 100+ model members across 4 NWP models

Weights shift dynamically based on time to resolution:
- **<6 hours**: METAR observations dominate (50% weight)
- **6-24 hours**: TAF forecasts lead (35% weight)
- **>24 hours**: Ensemble models dominate (70% combined weight)

### 3. Edge Detection
Compares our probability distribution against market-implied probabilities.
Only trades when edge exceeds the strategy threshold (6-15% depending on strategy).

### 4. Risk-Managed Execution
Uses fractional Kelly criterion for position sizing with multiple safety layers:
- Daily loss limits and trade count caps
- Maximum drawdown circuit breaker
- Per-market and correlated exposure limits
- Stop-loss and take-profit monitoring
- Edge decay exit (re-evaluates positions periodically)

## Strategies

| Parameter | Conservative | Balanced | Aggressive |
|---|---|---|---|
| Min edge to enter | 15% | 10% | 6% |
| Kelly fraction | 0.15× | 0.25× | 0.40× |
| Max position size | 2% bankroll | 5% bankroll | 10% bankroll |
| Max total exposure | 10% bankroll | 25% bankroll | 40% bankroll |
| Stop loss | -30% | -40% | -55% |
| Take profit | +60% | +75% | +90% |
| Min data sources | 3 | 2 | 1 |
| Model agreement req. | 70% | 60% | 50% |

## Quick Start

```bash
# 1. Clone and install
cd polymarket-weather-bot
pip install -r requirements.txt

# 2. Run analysis (no trading, no credentials needed)
python main.py --analyze-only

# 3. Dry run trading loop
python main.py --strategy balanced

# 4. Live trading (requires credentials)
export POLYMARKET_PRIVATE_KEY="your-key"
export POLYMARKET_FUNDER_ADDRESS="your-address"
python main.py --strategy conservative --live --bankroll 500
```

## CLI Options

```
--strategy {conservative,balanced,aggressive}  Trading strategy (default: balanced)
--live                                         Enable live trading (default: dry run)
--bankroll FLOAT                               Starting bankroll in USD
--analyze-only                                 Single analysis run, no loop
--interval INT                                 Loop interval in seconds (default: 120)
--log-level {DEBUG,INFO,WARNING,ERROR}         Logging verbosity
```

## Project Structure

```
polymarket-weather-bot/
├── main.py                          # Entry point + CLI
├── config.py                        # All configuration + strategy params
├── requirements.txt
├── .env.example
├── data/
│   ├── weather_sources.py           # METAR, TAF, Open-Meteo fetching
│   └── temperature_predictor.py     # Multi-source probability distribution
├── trading/
│   ├── polymarket_client.py         # Market discovery + order execution
│   ├── risk_manager.py              # Kelly sizing + risk limits
│   └── engine.py                    # Main orchestration loop
└── dashboard.jsx                    # React monitoring dashboard
```

## Key Design Decisions

**Why aviation weather?** METAR/TAF data comes from the same ASOS/AWOS stations that
produce the official readings Polymarket uses for resolution. You're trading on the
source data, not a derivative.

**Why ensemble models?** A single forecast gives you one number. 143 ensemble members
give you a probability distribution. When the ensemble spread is tight (1-2°C), confidence
is high. When it's wide (>4°C), we either size down or skip.

**Why fractional Kelly?** Full Kelly maximizes long-term growth but has brutal drawdowns.
Quarter-Kelly gives ~75% of the growth with dramatically less variance.

**Why limit orders?** Polymarket charges 0% maker fees with a 20-25% rebate. Market
(taker) orders pay 0.75-1.25%. The bot uses GTC limit orders to act as a maker.

## Risk Warnings

- This is experimental software. Use at your own risk.
- Start with dry run mode and small bankrolls.
- Past weather prediction accuracy does not guarantee trading profits.
- Polymarket liquidity in weather markets can be thin — slippage is real.
- The bot requires stable internet and may miss opportunities during outages.
