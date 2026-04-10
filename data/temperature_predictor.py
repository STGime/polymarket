"""
Temperature Predictor
=====================
Synthesizes METAR observations, TAF forecasts, and ensemble NWP models
into a probability distribution over temperature bands (matching Polymarket buckets).
"""
import logging
import math
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

from data.weather_sources import (
    WeatherDataService, MetarObs, TafForecast, EnsembleForecast
)
from config import CITY_ICAO_MAP

logger = logging.getLogger(__name__)


@dataclass
class TempBand:
    """A temperature band matching a Polymarket outcome bucket."""
    label: str          # e.g. "20°C", "68-69°F"
    low_c: float        # inclusive lower bound in Celsius
    high_c: float       # exclusive upper bound in Celsius
    token_id: str = ""  # Polymarket token ID for this outcome
    market_prob: float = 0.0  # current market-implied probability


@dataclass
class PredictionResult:
    """Full prediction output for a market."""
    city: str
    target_date: datetime
    bands: list[TempBand]
    predicted_probs: dict[str, float]   # band_label -> our probability
    market_probs: dict[str, float]      # band_label -> market probability
    edges: dict[str, float]             # band_label -> edge (predicted - market)
    confidence: float                   # 0-1 overall confidence
    data_sources_used: int
    best_band: str                      # highest predicted probability band
    best_edge_band: str                 # band with largest positive edge
    best_edge: float
    ensemble_spread_c: float            # spread across ensemble members (uncertainty)
    metar_trend_c_per_hr: Optional[float] = None  # temp trend from recent METARs


class TemperaturePredictor:
    """
    Multi-source temperature prediction engine.

    Combines:
    1. METAR observations (ground truth + trend)
    2. TAF forecasts (meteorologist judgment)
    3. Ensemble NWP models (probabilistic spread)

    Outputs a probability distribution over Polymarket temperature bands.
    """

    def __init__(self, weather_service: WeatherDataService):
        self.weather = weather_service

        # Weighting for each data source (dynamic based on hours until resolution)
        self.source_weights = {
            "metar_trend": 0.25,      # recent observation trend
            "taf_forecast": 0.15,     # professional meteorologist forecast (TX/TN)
            "ensemble": 0.30,         # multi-model ensemble distribution
            "deterministic": 0.10,    # Open-Meteo best-estimate forecast
            "national_forecast": 0.20, # national met service (NWS, JMA, etc.)
        }

    async def predict(
        self,
        city: str,
        target_date: datetime,
        bands: list[TempBand],
    ) -> PredictionResult:
        """
        Generate probability distribution over temperature bands for a city/date.
        """
        city_info = CITY_ICAO_MAP.get(city)
        if not city_info:
            raise ValueError(f"Unknown city: {city}. Add to CITY_ICAO_MAP.")

        icao = city_info["icao"]
        lat = city_info["lat"]
        lon = city_info["lon"]

        # Hours until the target date's daily high (assume ~14:00 local)
        now = datetime.now(timezone.utc)
        hours_until = (target_date - now).total_seconds() / 3600

        # Adjust weights based on time horizon
        weights = self._adjust_weights(hours_until)

        # ── Fetch all data sources concurrently ──
        import asyncio
        metar_task = self.weather.fetch_metar(icao)
        metar_hist_task = self.weather.fetch_metar_history(icao, hours=12)
        taf_task = self.weather.fetch_taf(icao)
        ensemble_task = self.weather.fetch_all_ensembles(lat, lon)
        deterministic_task = self.weather.fetch_deterministic_forecast(lat, lon)
        national_task = self.weather.fetch_national_forecast(city, lat, lon, target_date)

        metar, metar_history, taf, ensembles, deterministic, national = await asyncio.gather(
            metar_task, metar_hist_task, taf_task, ensemble_task,
            deterministic_task, national_task,
        )

        # ── Build component distributions ──
        distributions = []
        data_sources_used = 0

        # 1. METAR trend extrapolation
        metar_trend = None
        if metar_history and len(metar_history) >= 2:
            metar_dist, trend = self._metar_trend_distribution(
                metar_history, target_date, bands, city=city
            )
            if metar_dist:
                distributions.append(("metar_trend", metar_dist, weights["metar_trend"]))
                metar_trend = trend
                data_sources_used += 1

        # 2. TAF-based distribution
        if taf:
            taf_dist = self._taf_distribution(taf, target_date, bands)
            if taf_dist:
                distributions.append(("taf_forecast", taf_dist, weights["taf_forecast"]))
                data_sources_used += 1

        # 3. Ensemble models
        ensemble_spread = 0.0
        if ensembles:
            ens_dist, spread = self._ensemble_distribution(
                ensembles, target_date, bands
            )
            if ens_dist:
                distributions.append(("ensemble", ens_dist, weights["ensemble"]))
                ensemble_spread = spread
                data_sources_used += 1

        # 4. Deterministic forecast (Open-Meteo best-estimate)
        if deterministic:
            det_dist = self._deterministic_distribution(
                deterministic, target_date, bands
            )
            if det_dist:
                distributions.append(("deterministic", det_dist, weights["deterministic"]))
                data_sources_used += 1

        # 5. National meteorological service forecast (NWS, JMA, Met Office, etc.)
        if national:
            nat_max = national.get("max_temp_c")
            if nat_max is not None:
                sigma = 1.0  # national services are typically very accurate
                nat_dist = self._gaussian_to_bands(nat_max, sigma, bands)
                distributions.append(("national_forecast", nat_dist, weights["national_forecast"]))
                data_sources_used += 1

        # ── Weighted combination ──
        if not distributions:
            # Fallback: uniform distribution
            n = len(bands)
            predicted = {b.label: 1.0 / n for b in bands}
            confidence = 0.0
        else:
            predicted = self._combine_distributions(distributions, bands)
            confidence = self._calculate_confidence(
                distributions, ensemble_spread, data_sources_used, hours_until
            )

        # ── Calculate edges ──
        market_probs = {b.label: b.market_prob for b in bands}
        edges = {}
        for label in predicted:
            edges[label] = predicted[label] - market_probs.get(label, 0.0)

        # Find best bands
        best_band = max(predicted, key=predicted.get)
        positive_edges = {k: v for k, v in edges.items() if v > 0}
        if positive_edges:
            best_edge_band = max(positive_edges, key=positive_edges.get)
            best_edge = positive_edges[best_edge_band]
        else:
            best_edge_band = best_band
            best_edge = edges.get(best_band, 0.0)

        return PredictionResult(
            city=city,
            target_date=target_date,
            bands=bands,
            predicted_probs=predicted,
            market_probs=market_probs,
            edges=edges,
            confidence=confidence,
            data_sources_used=data_sources_used,
            best_band=best_band,
            best_edge_band=best_edge_band,
            best_edge=best_edge,
            ensemble_spread_c=ensemble_spread,
            metar_trend_c_per_hr=metar_trend,
        )

    def _adjust_weights(self, hours_until: float) -> dict:
        """
        Adjust source weights based on time to resolution.
        - Near term (<6h): heavily favor METAR observations
        - Medium term (6-24h): balanced, lean toward TAF
        - Far term (>24h): lean toward ensemble models
        """
        if hours_until < 6:
            return {
                "metar_trend": 0.40,
                "taf_forecast": 0.15,
                "ensemble": 0.15,
                "deterministic": 0.10,
                "national_forecast": 0.20,
            }
        elif hours_until < 24:
            return {
                "metar_trend": 0.10,
                "taf_forecast": 0.20,
                "ensemble": 0.30,
                "deterministic": 0.15,
                "national_forecast": 0.25,
            }
        else:
            return {
                "metar_trend": 0.05,
                "taf_forecast": 0.15,
                "ensemble": 0.40,
                "deterministic": 0.15,
                "national_forecast": 0.25,
            }

    def _metar_trend_distribution(
        self,
        history: list[MetarObs],
        target_date: datetime,
        bands: list[TempBand],
        city: str = "",
    ) -> tuple[Optional[dict], Optional[float]]:
        """
        Build distribution from METAR observation trend.
        Extrapolates the recent temperature trajectory and creates a
        Gaussian distribution around the projected value.
        """
        # Sort by time, extract temp series
        obs = sorted(history, key=lambda o: o.observed_at)
        temps = [(o.observed_at, o.temp_c) for o in obs if o.temp_c is not None]

        if len(temps) < 2:
            return None, None

        # Weighted linear regression for trend (recent observations weighted more)
        times_hours = [
            (t - temps[0][0]).total_seconds() / 3600 for t, _ in temps
        ]
        temp_vals = [v for _, v in temps]

        # Exponential time-decay weights: half-life of 3 hours
        half_life = 3.0
        weights = [
            math.exp(-math.log(2) * (times_hours[-1] - t) / half_life)
            for t in times_hours
        ]

        sum_w = sum(weights)
        sum_wx = sum(w * x for w, x in zip(weights, times_hours))
        sum_wy = sum(w * y for w, y in zip(weights, temp_vals))
        sum_wxy = sum(w * x * y for w, x, y in zip(weights, times_hours, temp_vals))
        sum_wx2 = sum(w * x * x for w, x in zip(weights, times_hours))

        denom = sum_w * sum_wx2 - sum_wx * sum_wx
        if abs(denom) < 1e-10:
            slope = 0.0
            intercept = sum_wy / sum_w
        else:
            slope = (sum_w * sum_wxy - sum_wx * sum_wy) / denom
            intercept = (sum_wy - slope * sum_wx) / sum_w

        # Project to target time
        hours_to_target = (target_date - temps[0][0]).total_seconds() / 3600
        projected_temp = intercept + slope * hours_to_target

        # Apply diurnal cycle correction using city-local time
        projected_temp = self._apply_diurnal_correction(
            projected_temp, target_date, temp_vals[-1], city=city
        )

        # Uncertainty increases with time
        hours_from_last_obs = (target_date - temps[-1][0]).total_seconds() / 3600
        sigma = max(1.0, 0.5 + 0.3 * hours_from_last_obs)

        # Build distribution over bands
        dist = self._gaussian_to_bands(projected_temp, sigma, bands)
        return dist, slope

    def _taf_distribution(
        self,
        taf: TafForecast,
        target_date: datetime,
        bands: list[TempBand],
    ) -> Optional[dict]:
        """
        Build distribution from TAF forecast.
        TAFs don't always include temperature, so we use wind/conditions
        as secondary signals and fall back to a broader distribution.
        """
        # Find the TAF period that covers our target time
        relevant_period = None
        for period in taf.periods:
            if period.from_time <= target_date <= period.to_time:
                relevant_period = period
                break

        if not relevant_period:
            # Use the last period as best approximation
            if taf.periods:
                relevant_period = taf.periods[-1]
            else:
                return None

        # Check for temperature: on the relevant period, or TX/TN on the base period
        max_temp = relevant_period.max_temp_c
        temp = relevant_period.temp_c
        if max_temp is None and temp is None and taf.periods:
            # TX/TN are typically on the first (base) period
            max_temp = taf.periods[0].max_temp_c
            temp = taf.periods[0].temp_c

        if max_temp is not None:
            projected = max_temp
            sigma = 1.5
        elif temp is not None:
            projected = temp
            sigma = 2.0
        else:
            return None

        return self._gaussian_to_bands(projected, sigma, bands)

    def _ensemble_distribution(
        self,
        ensembles: list[EnsembleForecast],
        target_date: datetime,
        bands: list[TempBand],
    ) -> tuple[Optional[dict], float]:
        """
        Build distribution from ensemble model members.
        This is the most statistically rigorous source — each member
        represents an equally likely forecast scenario.
        """
        all_member_temps = []

        for ens in ensembles:
            # Find the timestep closest to target_date
            if not ens.hourly_times:
                continue

            # Find closest time index
            min_diff = float("inf")
            best_idx = 0
            for i, t in enumerate(ens.hourly_times):
                # Handle both datetime objects and strings
                if isinstance(t, str):
                    try:
                        t = datetime.fromisoformat(t)
                    except (ValueError, TypeError):
                        continue

                diff = abs((t - target_date).total_seconds())
                if diff < min_diff:
                    min_diff = diff
                    best_idx = i

            # Extract temperature from each member at that timestep
            for member in ens.members:
                if best_idx < len(member) and member[best_idx] is not None:
                    all_member_temps.append(float(member[best_idx]))

        if not all_member_temps:
            return None, 0.0

        # Calculate spread
        temp_mean = sum(all_member_temps) / len(all_member_temps)
        temp_min = min(all_member_temps)
        temp_max = max(all_member_temps)
        spread = temp_max - temp_min

        # Build empirical distribution with Laplace smoothing on counts
        n_members = len(all_member_temps)
        n_bands = len(bands)
        alpha = 1  # standard Laplace smoothing parameter
        dist = {}
        for band in bands:
            count = sum(
                1 for t in all_member_temps
                if band.low_c <= t < band.high_c
            )
            dist[band.label] = (count + alpha) / (n_members + alpha * n_bands)

        return dist, spread

    def _deterministic_distribution(
        self,
        forecast: dict,
        target_date: datetime,
        bands: list[TempBand],
    ) -> Optional[dict]:
        """
        Build distribution from Open-Meteo deterministic (best-estimate) forecast.
        Uses daily max temperature or hourly temperature closest to target time.
        """
        # Try daily max first (most relevant for "highest temperature" markets)
        daily = forecast.get("daily", {})
        daily_times = daily.get("time", [])
        daily_maxes = daily.get("temperature_2m_max", [])
        target_date_str = target_date.strftime("%Y-%m-%d")

        projected = None
        for i, t in enumerate(daily_times):
            if t == target_date_str and i < len(daily_maxes) and daily_maxes[i] is not None:
                projected = float(daily_maxes[i])
                break

        # Fallback to hourly if no daily match
        if projected is None:
            hourly = forecast.get("hourly", {})
            hourly_times = hourly.get("time", [])
            hourly_temps = hourly.get("temperature_2m", [])
            if hourly_times and hourly_temps:
                min_diff = float("inf")
                for i, t in enumerate(hourly_times):
                    try:
                        ht = datetime.fromisoformat(t + "+00:00") if "+" not in t else datetime.fromisoformat(t)
                        diff = abs((ht - target_date).total_seconds())
                        if diff < min_diff and i < len(hourly_temps) and hourly_temps[i] is not None:
                            min_diff = diff
                            projected = float(hourly_temps[i])
                    except (ValueError, TypeError):
                        continue

        if projected is None:
            return None

        # Deterministic forecast has ~1-2°C typical error
        sigma = 1.5
        return self._gaussian_to_bands(projected, sigma, bands)

    def _gaussian_to_bands(
        self, mean: float, sigma: float, bands: list[TempBand]
    ) -> dict:
        """Convert a Gaussian (mean, sigma) into probabilities over bands."""
        dist = {}
        for band in bands:
            # CDF difference gives probability mass in band
            p_high = _normal_cdf(band.high_c, mean, sigma)
            p_low = _normal_cdf(band.low_c, mean, sigma)
            dist[band.label] = max(0.0, p_high - p_low)

        # Normalize
        total = sum(dist.values())
        if total > 0:
            dist = {k: v / total for k, v in dist.items()}

        return dist

    def _apply_diurnal_correction(
        self, projected: float, target_date: datetime, current_temp: float,
        city: str = "",
    ) -> float:
        """
        Diurnal cycle correction using local time for the city.
        Daily highs typically occur around 14:00-16:00 local time.
        """
        from zoneinfo import ZoneInfo

        # Convert target_date to local time for the city
        city_info = CITY_ICAO_MAP.get(city, {})
        tz_name = city_info.get("tz", "UTC")
        local_time = target_date.astimezone(ZoneInfo(tz_name))
        local_hour = local_time.hour + local_time.minute / 60

        diurnal_amplitude = 3.0  # typical diurnal range ~6°C, so ±3
        if 6 <= local_hour <= 21:
            correction = diurnal_amplitude * math.sin(
                math.pi * (local_hour - 9) / 12
            )
        else:
            correction = -diurnal_amplitude * 0.5

        # Blend projected with diurnal-corrected
        return projected * 0.7 + (current_temp + correction) * 0.3

    def _combine_distributions(
        self, distributions: list[tuple], bands: list[TempBand]
    ) -> dict:
        """Weighted combination of multiple probability distributions."""
        combined = {b.label: 0.0 for b in bands}
        total_weight = sum(w for _, _, w in distributions)

        for name, dist, weight in distributions:
            norm_weight = weight / total_weight if total_weight > 0 else 1.0
            for label in combined:
                combined[label] += norm_weight * dist.get(label, 0.0)

        # Final normalization
        total = sum(combined.values())
        if total > 0:
            combined = {k: v / total for k, v in combined.items()}

        return combined

    def _calculate_confidence(
        self,
        distributions: list,
        ensemble_spread: float,
        data_sources: int,
        hours_until: float,
    ) -> float:
        """
        Calculate confidence score (0-1) based on:
        - Number of data sources agreeing
        - Ensemble model spread (lower = higher confidence)
        - Time to resolution (closer = higher confidence)
        """
        # Base confidence from source count
        source_score = min(1.0, data_sources / 3.0)

        # Spread score (< 2°C spread = high confidence, > 6°C = low)
        spread_score = max(0.0, 1.0 - ensemble_spread / 6.0)

        # Time score (closer to resolution = higher confidence)
        if hours_until < 3:
            time_score = 0.95
        elif hours_until < 12:
            time_score = 0.80
        elif hours_until < 24:
            time_score = 0.60
        else:
            time_score = max(0.2, 0.50 - (hours_until - 24) / 100)

        # Agreement score — do distributions agree?
        agreement_score = self._measure_agreement(distributions)

        confidence = (
            0.25 * source_score +
            0.25 * spread_score +
            0.25 * time_score +
            0.25 * agreement_score
        )

        return min(1.0, max(0.0, confidence))

    def _measure_agreement(self, distributions: list) -> float:
        """Measure how much the different sources agree."""
        if len(distributions) < 2:
            return 0.5  # can't measure agreement with 1 source

        # Find the best band for each distribution
        best_bands = []
        for name, dist, weight in distributions:
            if dist:
                best = max(dist, key=dist.get)
                best_bands.append(best)

        if not best_bands:
            return 0.5

        # Agreement = fraction of sources that agree on the best band
        from collections import Counter
        counts = Counter(best_bands)
        most_common_count = counts.most_common(1)[0][1]
        return most_common_count / len(best_bands)


# ─── Math helpers ─────────────────────────────────────────

def _normal_cdf(x: float, mean: float, sigma: float) -> float:
    """Standard normal CDF using error function approximation."""
    if sigma <= 0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1.0 + math.erf((x - mean) / (sigma * math.sqrt(2))))
