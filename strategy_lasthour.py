"""
Last-Hour Speed Strategy
========================
Instead of predicting temperature, OBSERVE it.

By 2-3pm local time, the daily high is often already reached.
Fresh METAR data (updated every 30 min) tells us the current temp.
If the thermometer reads 24.8°C at 3pm and the trend is flat/declining,
then "25°C" band is very likely and "27°C+" is nearly impossible.

Edge source: faster reaction to real-time METAR observations than the market.

Logic:
1. Only trade markets resolving within 1-4 hours (afternoon local time)
2. Fetch latest METAR for the city's airport
3. Check METAR history: has temperature peaked? (declining in last 1-2 readings)
4. If peaked or near peak: the current observed max IS the daily high
5. Find the band that contains the observed max temp
6. If that band's market price is below our confidence → bet

No ensemble models. No forecasts. Just the thermometer.
"""
import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from config import CITY_ICAO_MAP
from data.weather_sources import WeatherDataService, MetarObs
from data.temperature_predictor import TempBand, PredictionResult

logger = logging.getLogger(__name__)


class LastHourStrategy:
    """
    Observes current temperature via METAR and bets on the band
    containing the observed daily high when the peak is likely reached.
    """

    def __init__(self, weather_service: WeatherDataService):
        self.weather = weather_service

    async def analyze(
        self,
        city: str,
        target_date: datetime,
        bands: list[TempBand],
    ) -> Optional[dict]:
        """
        Analyze a market for last-hour trading opportunity.

        Returns dict with trade recommendation or None if no opportunity:
        {
            "band": TempBand,          # which band to bet on
            "observed_max": float,     # highest temp observed today
            "current_temp": float,     # latest METAR reading
            "confidence": float,       # 0-1 how confident the peak is reached
            "peak_reached": bool,      # has temperature started declining?
            "hours_past_peak": float,  # hours since local solar noon
            "data_sources": int,
        }
        """
        city_info = CITY_ICAO_MAP.get(city)
        if not city_info:
            return None

        icao = city_info["icao"]
        tz_name = city_info.get("tz", "UTC")
        local_tz = ZoneInfo(tz_name)

        # ── Check timing: only trade 1-4 hours before resolution ──
        now = datetime.now(timezone.utc)
        hours_until = (target_date - now).total_seconds() / 3600
        if hours_until < 0.5 or hours_until > 4:
            return None

        # Check local time: should be afternoon (after 12:00 local)
        local_now = now.astimezone(local_tz)
        if local_now.hour < 12:
            return None  # too early, high hasn't happened yet

        # ── Fetch METAR history (last 6 hours) ──
        history = await self.weather.fetch_metar_history(icao, hours=6)
        if not history or len(history) < 2:
            return None

        # Sort by observation time (most recent last)
        history.sort(key=lambda o: o.observed_at)

        # Filter to only today's observations
        today_obs = [
            o for o in history
            if o.temp_c is not None and
            o.observed_at.astimezone(local_tz).date() == local_now.date()
        ]
        if len(today_obs) < 2:
            return None

        # ── Determine observed daily max ──
        temps = [o.temp_c for o in today_obs]
        observed_max = max(temps)
        current_temp = today_obs[-1].temp_c
        latest_obs_time = today_obs[-1].observed_at

        # How old is the latest observation?
        obs_age_minutes = (now - latest_obs_time).total_seconds() / 60
        if obs_age_minutes > 60:
            return None  # METAR too stale

        # ── Has the temperature peaked? ──
        # Look at the last 3-4 readings to detect a downward trend
        recent_temps = temps[-4:] if len(temps) >= 4 else temps[-3:]
        peak_idx = recent_temps.index(max(recent_temps))
        readings_since_peak = len(recent_temps) - 1 - peak_idx

        # Temperature has peaked if:
        # - We're past 2pm local AND temp has been flat or declining for 2+ readings
        # - OR we're past 4pm local (peak almost certainly passed)
        hours_past_noon = (local_now.hour - 12) + local_now.minute / 60
        peak_reached = (
            (hours_past_noon >= 2 and readings_since_peak >= 2) or
            (hours_past_noon >= 4) or
            (readings_since_peak >= 3)  # 3+ declining readings regardless of time
        )

        if not peak_reached:
            return None  # too early to be confident

        # ── How much more could it rise? ──
        # If temperature has been declining for 2+ readings, unlikely to set new high
        # If just plateaued, might still tick up 0.5-1°C
        temp_trend = current_temp - recent_temps[0]  # overall trend
        last_change = current_temp - recent_temps[-2] if len(recent_temps) >= 2 else 0

        # Estimate the likely final daily max
        if last_change <= -0.5:
            # Actively declining — observed max is very likely the final max
            estimated_max = observed_max
            confidence = 0.90
        elif last_change <= 0:
            # Flat or slightly declining
            estimated_max = observed_max + 0.3  # might tick up slightly
            confidence = 0.80
        else:
            # Still rising slightly
            estimated_max = observed_max + 0.5
            confidence = 0.65

        # Boost confidence if we're late in the day
        if hours_past_noon >= 4:
            confidence = min(0.95, confidence + 0.10)
        elif hours_past_noon >= 3:
            confidence = min(0.92, confidence + 0.05)

        # ── Find the band containing the estimated max ──
        best_band = None
        for band in bands:
            if band.low_c <= estimated_max < band.high_c:
                best_band = band
                break

        # Handle edge cases (temp above/below all bands)
        if best_band is None:
            if estimated_max >= bands[-1].low_c:
                best_band = bands[-1]  # highest band
            elif estimated_max < bands[0].high_c:
                best_band = bands[0]  # lowest band
            else:
                return None

        # ── Check if there's actually an edge ──
        market_price = best_band.market_prob
        if market_price >= confidence:
            return None  # market already prices this correctly, no edge

        edge = confidence - market_price
        if edge < 0.05:
            return None  # edge too small

        # ── Also check the adjacent band (might be better value) ──
        # If estimated_max is 24.8°C and we're between 24°C and 25°C bands,
        # consider both
        adjacent = None
        for band in bands:
            if band != best_band:
                # Check if estimated_max is within 0.5°C of this band's center
                band_center = (band.low_c + band.high_c) / 2
                if abs(estimated_max - band_center) < 1.0 and band.market_prob < confidence * 0.8:
                    adj_edge = (confidence * 0.3) - band.market_prob  # lower confidence for adjacent
                    if adj_edge > 0.05 and (adjacent is None or adj_edge > adjacent[1]):
                        adjacent = (band, adj_edge)

        logger.info(
            f"LAST-HOUR [{city}] METAR={current_temp}°C max={observed_max}°C "
            f"est={estimated_max:.1f}°C → {best_band.label} "
            f"(market={market_price:.0%}, ours={confidence:.0%}, edge={edge:+.0%}) "
            f"peak={'YES' if peak_reached else 'NO'} "
            f"local={local_now.strftime('%H:%M')}"
        )

        return {
            "band": best_band,
            "adjacent_band": adjacent[0] if adjacent else None,
            "observed_max": observed_max,
            "estimated_max": estimated_max,
            "current_temp": current_temp,
            "confidence": confidence,
            "edge": edge,
            "peak_reached": peak_reached,
            "hours_past_peak": hours_past_noon,
            "readings_since_peak": readings_since_peak,
            "temp_trend": temp_trend,
            "data_sources": 1,  # METAR only
            "obs_age_minutes": obs_age_minutes,
        }
