"""
Polymarket Weather Trading Bot - Configuration
===============================================
All settings for weather data sources, Polymarket API, trading strategies, and risk management.
"""
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Strategy(Enum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


@dataclass
class WeatherConfig:
    """Aviation weather data source configuration."""
    # AWC (Aviation Weather Center) - free, no auth needed for basic API
    awc_base_url: str = "https://aviationweather.gov/api/data"
    awc_rate_limit_rpm: int = 80  # stay under 100/min limit

    # Open-Meteo ensemble models - free, no auth
    openmeteo_base_url: str = "https://api.open-meteo.com/v1"
    openmeteo_ensemble_url: str = "https://ensemble-api.open-meteo.com/v1/ensemble"

    # AVWX parsed aviation weather (free tier)
    avwx_base_url: str = "https://avwx.rest/api"
    avwx_api_key: str = field(default_factory=lambda: os.getenv("AVWX_API_KEY", ""))

    # UK Met Office DataHub (free tier — 360 calls/day)
    metoffice_api_key: str = field(default_factory=lambda: os.getenv("METOFFICE_API_KEY", ""))

    # Polling intervals (seconds)
    metar_poll_interval: int = 300       # every 5 min (METARs update every 30 min)
    taf_poll_interval: int = 900         # every 15 min (TAFs update every 3 hours)
    ensemble_poll_interval: int = 3600   # every hour (model runs every 6 hours)


@dataclass
class PolymarketConfig:
    """Polymarket API configuration."""
    clob_api_url: str = "https://clob.polymarket.com"
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    data_api_url: str = "https://data-api.polymarket.com"
    chain_id: int = 137  # Polygon

    private_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_PRIVATE_KEY", ""))
    funder_address: str = field(default_factory=lambda: os.getenv("POLYMARKET_FUNDER_ADDRESS", ""))
    signature_type: int = 1  # 1 for email/Magic wallet, 0 for EOA

    # Market discovery
    temperature_search_tags: list = field(default_factory=lambda: [
        "Daily Temperature", "Weather", "Forecast"
    ])
    min_liquidity_usd: float = 500.0
    min_volume_usd: float = 1000.0


# ──────────────────────────────────────────────
# City-to-ICAO mapping for Polymarket markets
# These are the airports whose METAR stations are
# typically used as official temperature sources
# ──────────────────────────────────────────────
CITY_ICAO_MAP = {
    # City → ICAO, lat, lon, timezone, national_service, national_id (if needed)
    # national_service: nws | jma | metoffice | brightsky | nea | bom | None
    #
    # ── US cities (NWS) ──────────────────────────────────────
    "New York":     {"icao": "KJFK", "lat": 40.64, "lon": -73.78, "tz": "America/New_York", "national_service": "nws"},
    "Los Angeles":  {"icao": "KLAX", "lat": 33.94, "lon": -118.41, "tz": "America/Los_Angeles", "national_service": "nws"},
    "Chicago":      {"icao": "KORD", "lat": 41.98, "lon": -87.90, "tz": "America/Chicago", "national_service": "nws"},
    "Seattle":      {"icao": "KSEA", "lat": 47.45, "lon": -122.31, "tz": "America/Los_Angeles", "national_service": "nws"},
    "Miami":        {"icao": "KMIA", "lat": 25.79, "lon": -80.29, "tz": "America/New_York", "national_service": "nws"},
    "Denver":       {"icao": "KDEN", "lat": 39.86, "lon": -104.67, "tz": "America/Denver", "national_service": "nws"},
    "Phoenix":      {"icao": "KPHX", "lat": 33.43, "lon": -112.01, "tz": "America/Phoenix", "national_service": "nws"},
    "Atlanta":      {"icao": "KATL", "lat": 33.64, "lon": -84.43, "tz": "America/New_York", "national_service": "nws"},
    "Dallas":       {"icao": "KDFW", "lat": 32.90, "lon": -97.04, "tz": "America/Chicago", "national_service": "nws"},
    "San Francisco": {"icao": "KSFO", "lat": 37.62, "lon": -122.38, "tz": "America/Los_Angeles", "national_service": "nws"},
    "Washington":   {"icao": "KDCA", "lat": 38.85, "lon": -77.04, "tz": "America/New_York", "national_service": "nws"},
    "DC":           {"icao": "KDCA", "lat": 38.85, "lon": -77.04, "tz": "America/New_York", "national_service": "nws"},
    "NYC":          {"icao": "KJFK", "lat": 40.64, "lon": -73.78, "tz": "America/New_York", "national_service": "nws"},
    "Austin":       {"icao": "KAUS", "lat": 30.19, "lon": -97.67, "tz": "America/Chicago", "national_service": "nws"},
    "Houston":      {"icao": "KIAH", "lat": 29.98, "lon": -95.34, "tz": "America/Chicago", "national_service": "nws"},
    #
    # ── UK (Met Office) ──────────────────────────────────────
    "London":       {"icao": "EGLL", "lat": 51.47, "lon": -0.46, "tz": "Europe/London", "national_service": "metoffice"},
    #
    # ── Germany (DWD via Bright Sky) ─────────────────────────
    "Berlin":       {"icao": "EDDB", "lat": 52.36, "lon": 13.51, "tz": "Europe/Berlin", "national_service": "brightsky"},
    "Munich":       {"icao": "EDDM", "lat": 48.35, "lon": 11.79, "tz": "Europe/Berlin", "national_service": "brightsky"},
    #
    # ── Japan (JMA) ──────────────────────────────────────────
    "Tokyo":        {"icao": "RJTT", "lat": 35.55, "lon": 139.78, "tz": "Asia/Tokyo", "national_service": "jma", "national_id": "130000"},
    #
    # ── Singapore (NEA) ──────────────────────────────────────
    "Singapore":    {"icao": "WSSS", "lat": 1.36, "lon": 103.99, "tz": "Asia/Singapore", "national_service": "nea"},
    #
    # ── Australia (BOM) ──────────────────────────────────────
    "Sydney":       {"icao": "YSSY", "lat": -33.95, "lon": 151.18, "tz": "Australia/Sydney", "national_service": "bom", "national_id": "r3gx2f"},
    #
    # ── Netherlands (Bright Sky covers via DWD ICON model) ───
    "Amsterdam":    {"icao": "EHAM", "lat": 52.31, "lon": 4.76, "tz": "Europe/Amsterdam", "national_service": "brightsky"},
    #
    # ── Cities with TAF TX/TN (no separate national API needed)
    "Paris":        {"icao": "LFPG", "lat": 49.01, "lon": 2.55, "tz": "Europe/Paris"},
    "Seoul":        {"icao": "RKSI", "lat": 37.46, "lon": 126.44, "tz": "Asia/Seoul"},
    "Hong Kong":    {"icao": "VHHH", "lat": 22.31, "lon": 113.91, "tz": "Asia/Hong_Kong"},
    "São Paulo":    {"icao": "SBGR", "lat": -23.43, "lon": -46.47, "tz": "America/Sao_Paulo"},
    "Mexico City":  {"icao": "MMMX", "lat": 19.44, "lon": -99.07, "tz": "America/Mexico_City"},
    "Buenos Aires": {"icao": "SAEZ", "lat": -34.82, "lon": -58.54, "tz": "America/Argentina/Buenos_Aires"},
    "Shanghai":     {"icao": "ZSPD", "lat": 31.14, "lon": 121.81, "tz": "Asia/Shanghai"},
    "Beijing":      {"icao": "ZBAA", "lat": 40.08, "lon": 116.58, "tz": "Asia/Shanghai"},
    "Busan":        {"icao": "RKPK", "lat": 35.18, "lon": 128.94, "tz": "Asia/Seoul"},
    #
    # ── Other cities (no dedicated national API — use deterministic + ensemble)
    "Dubai":        {"icao": "OMDB", "lat": 25.25, "lon": 55.36, "tz": "Asia/Dubai"},
    "Mumbai":       {"icao": "VABB", "lat": 19.09, "lon": 72.87, "tz": "Asia/Kolkata"},
    "Bangkok":      {"icao": "VTBS", "lat": 13.69, "lon": 100.75, "tz": "Asia/Bangkok"},
    "Istanbul":     {"icao": "LTFM", "lat": 41.26, "lon": 28.74, "tz": "Europe/Istanbul"},
    "Toronto":      {"icao": "CYYZ", "lat": 43.68, "lon": -79.63, "tz": "America/Toronto"},
    "Panama City":  {"icao": "MPTO", "lat": 9.07, "lon": -79.38, "tz": "America/Panama"},
    "Kuala Lumpur": {"icao": "WMKK", "lat": 2.74, "lon": 101.70, "tz": "Asia/Kuala_Lumpur"},
    "Jakarta":      {"icao": "WIII", "lat": -6.13, "lon": 106.65, "tz": "Asia/Jakarta"},
    "Wellington":   {"icao": "NZWN", "lat": -41.33, "lon": 174.81, "tz": "Pacific/Auckland"},
    "Ankara":       {"icao": "LTAC", "lat": 40.13, "lon": 32.99, "tz": "Europe/Istanbul"},
    "Chengdu":      {"icao": "ZUUU", "lat": 30.58, "lon": 103.95, "tz": "Asia/Shanghai"},
    "Chongqing":    {"icao": "ZUCK", "lat": 29.72, "lon": 106.64, "tz": "Asia/Shanghai"},
    "Helsinki":     {"icao": "EFHK", "lat": 60.32, "lon": 24.95, "tz": "Europe/Helsinki"},
    "Lucknow":      {"icao": "VILK", "lat": 26.76, "lon": 80.88, "tz": "Asia/Kolkata"},
    "Madrid":       {"icao": "LEMD", "lat": 40.47, "lon": -3.56, "tz": "Europe/Madrid"},
    "Milan":        {"icao": "LIMC", "lat": 45.63, "lon": 8.72, "tz": "Europe/Rome"},
    "Moscow":       {"icao": "UUEE", "lat": 55.97, "lon": 37.41, "tz": "Europe/Moscow"},
    "Shenzhen":     {"icao": "ZGSZ", "lat": 22.64, "lon": 113.81, "tz": "Asia/Shanghai"},
    "Taipei":       {"icao": "RCTP", "lat": 25.08, "lon": 121.23, "tz": "Asia/Taipei"},
    "Tel Aviv":     {"icao": "LLBG", "lat": 32.01, "lon": 34.89, "tz": "Asia/Jerusalem"},
    "Warsaw":       {"icao": "EPWA", "lat": 52.17, "lon": 20.97, "tz": "Europe/Warsaw"},
    "Wuhan":        {"icao": "ZHHH", "lat": 30.78, "lon": 114.21, "tz": "Asia/Shanghai"},
    "Sao Paulo":    {"icao": "SBGR", "lat": -23.43, "lon": -46.47, "tz": "America/Sao_Paulo"},
}


@dataclass
class StrategyParams:
    """Parameters for each trading strategy tier."""
    name: Strategy

    # Position sizing (fraction of bankroll per single bet)
    max_position_pct: float = 0.05       # max % of bankroll per position
    max_total_exposure_pct: float = 0.25  # max % of bankroll in all positions

    # Edge thresholds — minimum predicted edge over market to trade
    min_edge_to_enter: float = 0.10      # 10% edge minimum
    min_edge_to_hold: float = 0.03       # 3% edge to keep position

    # Kelly criterion fraction (fraction of full Kelly to use)
    kelly_fraction: float = 0.25         # quarter-Kelly default

    # Stop loss / take profit
    stop_loss_pct: float = 0.50          # cut at 50% loss
    take_profit_pct: float = 0.80        # take profit at 80% gain

    # Timing
    max_hours_before_resolution: float = 48  # don't enter > 48h before resolve
    min_hours_before_resolution: float = 1   # don't enter < 1h before resolve

    # Confidence requirements
    min_data_sources: int = 2            # need at least 2 data sources agreeing
    min_model_agreement: float = 0.60    # 60% of models must agree on band


# Pre-built strategy configurations
STRATEGIES = {
    Strategy.CONSERVATIVE: StrategyParams(
        name=Strategy.CONSERVATIVE,
        max_position_pct=0.02,
        max_total_exposure_pct=0.10,
        min_edge_to_enter=0.15,       # need 15% edge
        min_edge_to_hold=0.05,
        kelly_fraction=0.15,          # very fractional Kelly
        stop_loss_pct=0.30,           # tight stop loss
        take_profit_pct=0.60,
        max_hours_before_resolution=24,
        min_hours_before_resolution=2,
        min_data_sources=3,
        min_model_agreement=0.70,
    ),
    Strategy.BALANCED: StrategyParams(
        name=Strategy.BALANCED,
        max_position_pct=0.05,
        max_total_exposure_pct=0.25,
        min_edge_to_enter=0.10,       # 10% edge
        min_edge_to_hold=0.03,
        kelly_fraction=0.25,          # quarter Kelly
        stop_loss_pct=0.40,
        take_profit_pct=0.75,
        max_hours_before_resolution=36,
        min_hours_before_resolution=1,
        min_data_sources=2,
        min_model_agreement=0.60,
    ),
    Strategy.AGGRESSIVE: StrategyParams(
        name=Strategy.AGGRESSIVE,
        max_position_pct=0.10,
        max_total_exposure_pct=0.40,
        min_edge_to_enter=0.06,       # 6% edge sufficient
        min_edge_to_hold=0.02,
        kelly_fraction=0.40,          # near half-Kelly
        stop_loss_pct=0.55,
        take_profit_pct=0.90,
        max_hours_before_resolution=48,
        min_hours_before_resolution=0.5,
        min_data_sources=1,
        min_model_agreement=0.50,
    ),
}


@dataclass
class RiskConfig:
    """Global risk management parameters."""
    # Bankroll management
    initial_bankroll_usd: float = field(
        default_factory=lambda: float(os.getenv("BANKROLL_USD", "1000"))
    )

    # Daily limits
    max_daily_loss_pct: float = 0.10     # stop trading after 10% daily loss
    max_daily_trades: int = 50
    max_concurrent_positions: int = 15

    # Drawdown protection
    max_drawdown_pct: float = 0.20       # halt all trading at 20% drawdown
    drawdown_cooldown_hours: float = 24  # wait 24h after drawdown halt

    # Per-market limits
    max_per_market_usd: float = 100.0
    max_correlated_exposure: int = 3     # max positions on same city's markets

    # Slippage / execution
    max_slippage_pct: float = 0.03       # 3% max slippage tolerance
    max_spread_pct: float = 0.15         # skip trades with spread > 15%
    order_type: str = "GTC"              # Good-Til-Cancelled (maker order)
    use_limit_orders: bool = True        # prefer limit orders for maker rebates


@dataclass
class BotConfig:
    """Top-level bot configuration."""
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    active_strategy: Strategy = Strategy.BALANCED

    # Logging
    log_level: str = "INFO"
    log_file: str = "trading_bot.log"

    # Dry run mode (no real trades)
    dry_run: bool = field(
        default_factory=lambda: os.getenv("DRY_RUN", "true").lower() == "true"
    )

    # Main loop interval
    loop_interval_seconds: int = 120  # check every 2 minutes

    @property
    def strategy_params(self) -> StrategyParams:
        return STRATEGIES[self.active_strategy]
