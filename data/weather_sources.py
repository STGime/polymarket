"""
Weather Data Sources
====================
Pulls aviation weather (METAR/TAF) from aviationweather.gov and
ensemble NWP model forecasts from Open-Meteo. All free, no auth required.
"""
import asyncio
import time
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional
import aiohttp

logger = logging.getLogger(__name__)


class AsyncRateLimiter:
    """Token bucket rate limiter for async API calls."""

    def __init__(self, rate_per_minute: int):
        self._rate = rate_per_minute
        self._tokens = float(rate_per_minute)
        self._max_tokens = float(rate_per_minute)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                self._max_tokens,
                self._tokens + elapsed * self._rate / 60,
            )
            self._last_refill = now
            if self._tokens < 1:
                wait_time = (1 - self._tokens) * 60 / self._rate
                await asyncio.sleep(wait_time)
                self._tokens = 0
            else:
                self._tokens -= 1


@dataclass
class CacheEntry:
    """Timestamped cache entry with TTL."""
    data: object
    cached_at: float  # time.monotonic()
    ttl_seconds: float

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.cached_at) > self.ttl_seconds


@dataclass
class MetarObs:
    """Parsed METAR observation."""
    station: str
    observed_at: datetime
    temp_c: float
    dewpoint_c: Optional[float] = None
    wind_speed_kts: Optional[float] = None
    wind_dir_deg: Optional[int] = None
    wind_gust_kts: Optional[float] = None
    visibility_m: Optional[float] = None
    altimeter_hpa: Optional[float] = None
    cloud_layers: list = field(default_factory=list)
    flight_category: Optional[str] = None
    raw_text: str = ""


@dataclass
class TafForecast:
    """Parsed TAF forecast period."""
    station: str
    issued_at: datetime
    valid_from: datetime
    valid_to: datetime
    periods: list = field(default_factory=list)  # list of TafPeriod
    raw_text: str = ""


@dataclass
class TafPeriod:
    """Single forecast period within a TAF."""
    from_time: datetime
    to_time: datetime
    temp_c: Optional[float] = None
    max_temp_c: Optional[float] = None
    min_temp_c: Optional[float] = None
    wind_speed_kts: Optional[float] = None
    wind_dir_deg: Optional[int] = None
    probability: Optional[int] = None   # PROB30, PROB40
    change_type: str = "FM"             # FM, BECMG, TEMPO, PROB


@dataclass
class EnsembleForecast:
    """Ensemble model forecast for a location."""
    latitude: float
    longitude: float
    generated_at: datetime
    hourly_times: list = field(default_factory=list)
    # Each member is a list of temps aligned with hourly_times
    members: list = field(default_factory=list)
    model_name: str = ""


class WeatherDataService:
    """Fetches and caches weather data from multiple aviation sources."""

    # Cache TTLs (seconds)
    METAR_TTL = 1800     # 30 minutes
    TAF_TTL = 10800      # 3 hours
    ENSEMBLE_TTL = 3600  # 1 hour

    def __init__(self, config):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._metar_cache: dict[str, CacheEntry] = {}
        self._taf_cache: dict[str, CacheEntry] = {}
        self._ensemble_cache: dict[tuple, CacheEntry] = {}
        self._awc_limiter = AsyncRateLimiter(config.awc_rate_limit_rpm)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "PolyWeatherBot/1.0 (stefan@eurobase.app)"},
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ─── METAR ────────────────────────────────────────────────

    async def fetch_metar(self, icao: str) -> Optional[MetarObs]:
        """Fetch latest METAR from aviationweather.gov API."""
        # Return cached if still fresh
        cached = self._metar_cache.get(icao)
        if cached and not cached.expired:
            return cached.data

        await self._awc_limiter.acquire()
        session = await self._get_session()
        url = f"{self.config.awc_base_url}/metar"
        params = {
            "ids": icao,
            "format": "json",
            "hours": 3,  # last 3 hours of observations
        }

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"METAR fetch failed for {icao}: HTTP {resp.status}")
                    return cached.data if cached else None

                data = await resp.json()
                if not data:
                    return cached.data if cached else None

                # Take the most recent observation
                latest = data[0] if isinstance(data, list) else data
                obs = self._parse_metar(latest)
                self._metar_cache[icao] = CacheEntry(
                    data=obs, cached_at=time.monotonic(), ttl_seconds=self.METAR_TTL,
                )
                logger.info(
                    f"METAR {icao}: {obs.temp_c}°C at "
                    f"{obs.observed_at.strftime('%H:%M')}Z"
                )
                return obs

        except Exception as e:
            logger.error(f"METAR fetch error for {icao}: {e}")
            return cached.data if cached else None

    def _parse_metar(self, data: dict) -> MetarObs:
        """Parse AWC JSON METAR response."""
        obs_time = data.get("reportTime") or data.get("obsTime", "")
        if isinstance(obs_time, str):
            try:
                obs_dt = datetime.fromisoformat(obs_time.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                obs_dt = datetime.now(timezone.utc)
        else:
            obs_dt = datetime.now(timezone.utc)

        clouds = []
        for cloud in data.get("clouds", []):
            if isinstance(cloud, dict):
                clouds.append({
                    "cover": cloud.get("cover", ""),
                    "base_ft": cloud.get("base"),
                })

        return MetarObs(
            station=data.get("icaoId", data.get("stationId", "")),
            observed_at=obs_dt,
            temp_c=float(data.get("temp", 0)),
            dewpoint_c=_safe_float(data.get("dewp")),
            wind_speed_kts=_safe_float(data.get("wspd")),
            wind_dir_deg=_safe_int(data.get("wdir")),
            wind_gust_kts=_safe_float(data.get("wgst")),
            visibility_m=_safe_float(data.get("visib")),
            altimeter_hpa=_safe_float(data.get("altim")),
            cloud_layers=clouds,
            flight_category=data.get("fltcat", ""),
            raw_text=data.get("rawOb", ""),
        )

    async def fetch_metar_history(self, icao: str, hours: int = 12) -> list[MetarObs]:
        """Fetch multiple recent METARs to build observation trend."""
        await self._awc_limiter.acquire()
        session = await self._get_session()
        url = f"{self.config.awc_base_url}/metar"
        params = {
            "ids": icao,
            "format": "json",
            "hours": hours,
        }

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                if not data or not isinstance(data, list):
                    return []
                return [self._parse_metar(d) for d in data]
        except Exception as e:
            logger.error(f"METAR history error for {icao}: {e}")
            return []

    # ─── TAF ──────────────────────────────────────────────────

    async def fetch_taf(self, icao: str) -> Optional[TafForecast]:
        """Fetch latest TAF from aviationweather.gov API."""
        # Return cached if still fresh
        cached = self._taf_cache.get(icao)
        if cached and not cached.expired:
            return cached.data

        await self._awc_limiter.acquire()
        session = await self._get_session()
        url = f"{self.config.awc_base_url}/taf"
        params = {
            "ids": icao,
            "format": "json",
        }

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"TAF fetch failed for {icao}: HTTP {resp.status}")
                    return cached.data if cached else None

                data = await resp.json()
                if not data:
                    return cached.data if cached else None

                latest = data[0] if isinstance(data, list) else data
                taf = self._parse_taf(latest)
                self._taf_cache[icao] = CacheEntry(
                    data=taf, cached_at=time.monotonic(), ttl_seconds=self.TAF_TTL,
                )
                return taf

        except Exception as e:
            logger.error(f"TAF fetch error for {icao}: {e}")
            return cached.data if cached else None

    def _parse_taf(self, data: dict) -> TafForecast:
        """Parse AWC JSON TAF response, including TX/TN temperature groups."""
        import re

        issued_dt = _parse_awc_time(data.get("issueTime")) or datetime.now(timezone.utc)
        valid_from = _parse_awc_time(data.get("validTimeFrom")) or issued_dt
        valid_to = _parse_awc_time(data.get("validTimeTo")) or (issued_dt + timedelta(hours=24))

        # Parse TX/TN temperature groups from raw TAF text
        # Format: TXnn/ddhhZ TNnn/ddhhZ (max/min temp in Celsius at day/hour UTC)
        raw_taf = data.get("rawTAF", "")
        taf_max_temp = None
        taf_min_temp = None
        # TX25/0916Z = max 25°C on day 09 at 16Z; TNM02/0906Z = min -2°C
        for match in re.finditer(r"TX(M?\d+)/(\d{2})(\d{2})Z", raw_taf):
            temp_str = match.group(1).replace("M", "-")
            taf_max_temp = float(temp_str)
        for match in re.finditer(r"TN(M?\d+)/(\d{2})(\d{2})Z", raw_taf):
            temp_str = match.group(1).replace("M", "-")
            taf_min_temp = float(temp_str)

        if taf_max_temp is not None:
            logger.info(f"TAF {data.get('icaoId','')}: TX={taf_max_temp}°C TN={taf_min_temp}°C")

        periods = []
        for fcst in data.get("fcsts", []):
            if not isinstance(fcst, dict):
                continue
            ft = _parse_awc_time(fcst.get("timeFrom"))
            tt = _parse_awc_time(fcst.get("timeTo"))
            if not ft or not tt:
                continue

            # Check JSON temp field (list of {valid, sfc} entries)
            period_temp = None
            period_max = None
            period_min = None
            temp_list = fcst.get("temp", [])
            if isinstance(temp_list, list):
                for t in temp_list:
                    if isinstance(t, dict):
                        period_temp = _safe_float(t.get("sfc"))

            periods.append(TafPeriod(
                from_time=ft,
                to_time=tt,
                temp_c=period_temp,
                max_temp_c=period_max,
                min_temp_c=period_min,
                wind_speed_kts=_safe_float(fcst.get("wspd")),
                wind_dir_deg=_safe_int(fcst.get("wdir")),
                probability=_safe_int(fcst.get("prob")),
                change_type=fcst.get("fcstChange", "FM"),
            ))

        # Inject TX/TN into the appropriate periods
        if taf_max_temp is not None and periods:
            # Set max_temp on the first base period (covers the full validity)
            periods[0].max_temp_c = taf_max_temp
        if taf_min_temp is not None and periods:
            periods[0].min_temp_c = taf_min_temp

        return TafForecast(
            station=data.get("icaoId", ""),
            issued_at=issued_dt,
            valid_from=valid_from,
            valid_to=valid_to,
            periods=periods,
            raw_text=raw_taf,
        )

    # ─── OPEN-METEO ENSEMBLE ──────────────────────────────────

    async def fetch_ensemble(
        self, lat: float, lon: float, model: str = "icon_seamless"
    ) -> Optional[EnsembleForecast]:
        """
        Fetch ensemble NWP forecasts from Open-Meteo.
        Available models: icon_seamless, gfs_seamless, ecmwf_ifs025, gem_global
        """
        cache_key = (round(lat, 2), round(lon, 2), model)
        cached = self._ensemble_cache.get(cache_key)
        if cached and not cached.expired:
            return cached.data

        session = await self._get_session()
        url = self.config.openmeteo_ensemble_url
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "models": model,
            "forecast_days": 3,
        }

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"Ensemble fetch failed ({model}): HTTP {resp.status}")
                    return cached.data if cached else None

                data = await resp.json()
                hourly = data.get("hourly", {})
                times_raw = hourly.get("time", [])
                times = []
                for t in times_raw:
                    try:
                        times.append(datetime.fromisoformat(t + "+00:00"))
                    except (ValueError, TypeError):
                        times.append(datetime.fromisoformat(t))

                # Extract all ensemble members
                members = []
                for key, values in hourly.items():
                    if key.startswith("temperature_2m_member"):
                        members.append(values)

                # If no individual members, try the base field
                if not members and "temperature_2m" in hourly:
                    members.append(hourly["temperature_2m"])

                forecast = EnsembleForecast(
                    latitude=lat,
                    longitude=lon,
                    generated_at=datetime.now(timezone.utc),
                    hourly_times=times,
                    members=members,
                    model_name=model,
                )

                self._ensemble_cache[cache_key] = CacheEntry(
                    data=forecast, cached_at=time.monotonic(),
                    ttl_seconds=self.ENSEMBLE_TTL,
                )
                logger.info(
                    f"Ensemble {model}: {len(members)} members, "
                    f"{len(times)} timesteps for ({lat:.2f}, {lon:.2f})"
                )
                return forecast

        except Exception as e:
            logger.error(f"Ensemble fetch error ({model}): {e}")
            return cached.data if cached else None

    async def fetch_all_ensembles(
        self, lat: float, lon: float
    ) -> list[EnsembleForecast]:
        """Fetch from multiple ensemble models for cross-validation."""
        models = ["icon_seamless", "gfs_seamless", "ecmwf_ifs025", "gem_global"]
        tasks = [self.fetch_ensemble(lat, lon, m) for m in models]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        forecasts = []
        for r in results:
            if isinstance(r, EnsembleForecast):
                forecasts.append(r)
            elif isinstance(r, Exception):
                logger.warning(f"Ensemble fetch exception: {r}")

        return forecasts

    # ─── OPEN-METEO DETERMINISTIC (for quick reference) ───────

    async def fetch_deterministic_forecast(
        self, lat: float, lon: float
    ) -> Optional[dict]:
        """Fetch deterministic forecast from Open-Meteo (best-estimate)."""
        session = await self._get_session()
        url = f"{self.config.openmeteo_base_url}/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,apparent_temperature",
            "daily": "temperature_2m_max,temperature_2m_min",
            "forecast_days": 3,
            "timezone": "UTC",
        }

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception as e:
            logger.error(f"Deterministic forecast error: {e}")
            return None

    # ─── NATIONAL METEOROLOGICAL SERVICE FORECASTS ────────────

    async def fetch_national_forecast(
        self, city: str, lat: float, lon: float, target_date: datetime
    ) -> Optional[dict]:
        """
        Fetch temperature forecast from the city's national met service.
        Returns {"max_temp_c": float, "min_temp_c": float, "source": str} or None.
        """
        from config import CITY_ICAO_MAP
        city_info = CITY_ICAO_MAP.get(city, {})
        service = city_info.get("national_service")

        if not service:
            return None

        try:
            if service == "nws":
                return await self._fetch_nws(lat, lon, target_date)
            elif service == "jma":
                office_code = city_info.get("national_id", "130000")
                return await self._fetch_jma(office_code, target_date)
            elif service == "metoffice":
                return await self._fetch_metoffice(lat, lon, target_date)
            elif service == "brightsky":
                return await self._fetch_brightsky(lat, lon, target_date)
            elif service == "nea":
                return await self._fetch_nea(target_date)
            elif service == "bom":
                geohash = city_info.get("national_id", "")
                return await self._fetch_bom(geohash, target_date)
        except Exception as e:
            logger.error(f"National forecast error ({service}) for {city}: {e}")

        return None

    async def _fetch_nws(
        self, lat: float, lon: float, target_date: datetime
    ) -> Optional[dict]:
        """US National Weather Service — api.weather.gov"""
        session = await self._get_session()
        headers = {"User-Agent": "PolyWeatherBot/1.0 (stefan@eurobase.app)"}

        # Step 1: Resolve lat/lon to grid point
        points_url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
        async with session.get(points_url, headers=headers) as resp:
            if resp.status != 200:
                logger.warning(f"NWS points lookup failed: HTTP {resp.status}")
                return None
            points = await resp.json()

        forecast_url = points.get("properties", {}).get("forecast")
        if not forecast_url:
            return None

        # Step 2: Get forecast periods
        async with session.get(forecast_url, headers=headers) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        target_str = target_date.strftime("%Y-%m-%d")
        max_temp = None
        min_temp = None

        for period in data.get("properties", {}).get("periods", []):
            start = period.get("startTime", "")
            if target_str not in start:
                continue
            temp_f = period.get("temperature")
            unit = period.get("temperatureUnit", "F")
            if temp_f is None:
                continue
            temp_c = (temp_f - 32) * 5 / 9 if unit == "F" else temp_f
            if period.get("isDaytime"):
                max_temp = temp_c
            else:
                min_temp = temp_c

        if max_temp is not None:
            logger.info(f"NWS forecast: max={max_temp:.1f}°C min={min_temp}°C")
            return {"max_temp_c": max_temp, "min_temp_c": min_temp, "source": "nws"}
        return None

    async def _fetch_jma(
        self, office_code: str, target_date: datetime
    ) -> Optional[dict]:
        """Japan Meteorological Agency — jma.go.jp/bosai"""
        session = await self._get_session()
        url = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{office_code}.json"

        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        # Second element has weekly forecast with temps
        if len(data) < 2:
            return None

        weekly = data[1]
        time_series = weekly.get("timeSeries", [])
        if not time_series:
            return None

        # Find the temp series (last one typically)
        for ts in time_series:
            time_defines = ts.get("timeDefines", [])
            areas = ts.get("areas", [])
            if not areas:
                continue
            area = areas[0]  # first area is the main city
            temps_max = area.get("tempsMax", [])
            temps_min = area.get("tempsMin", [])
            if not temps_max:
                continue

            target_str = target_date.strftime("%Y-%m-%d")
            for i, t in enumerate(time_defines):
                if target_str in t and i < len(temps_max):
                    max_t = _safe_float(temps_max[i])
                    min_t = _safe_float(temps_min[i]) if i < len(temps_min) else None
                    if max_t is not None:
                        logger.info(f"JMA forecast: max={max_t}°C min={min_t}°C")
                        return {"max_temp_c": max_t, "min_temp_c": min_t, "source": "jma"}
                    # JMA leaves today's entry empty — try next matching day
                    continue
        return None

    async def _fetch_metoffice(
        self, lat: float, lon: float, target_date: datetime
    ) -> Optional[dict]:
        """UK Met Office DataHub — requires METOFFICE_API_KEY env var."""
        import os
        api_key = os.getenv("METOFFICE_API_KEY", "")
        if not api_key:
            return None

        session = await self._get_session()
        url = "https://data.hub.api.metoffice.gov.uk/sitespecific/v0/point/daily"
        params = {
            "latitude": lat,
            "longitude": lon,
            "includeLocationName": "true",
        }
        headers = {"apikey": api_key}

        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status == 429:
                logger.warning("Met Office rate limit hit (360/day)")
                return None
            if resp.status != 200:
                logger.warning(f"Met Office API error: HTTP {resp.status}")
                return None
            data = await resp.json()

        target_str = target_date.strftime("%Y-%m-%d")
        for feature in data.get("features", []):
            for step in feature.get("properties", {}).get("timeSeries", []):
                time_val = step.get("time", "")
                if target_str not in time_val:
                    continue
                max_temp = _safe_float(step.get("dayMaxScreenTemperature"))
                min_temp = _safe_float(step.get("nightMinScreenTemperature"))
                if max_temp is not None:
                    logger.info(f"Met Office forecast: max={max_temp}°C min={min_temp}°C")
                    return {"max_temp_c": max_temp, "min_temp_c": min_temp, "source": "metoffice"}
        return None

    async def _fetch_brightsky(
        self, lat: float, lon: float, target_date: datetime
    ) -> Optional[dict]:
        """DWD via Bright Sky API — api.brightsky.dev"""
        session = await self._get_session()
        date_str = target_date.strftime("%Y-%m-%d")
        url = "https://api.brightsky.dev/weather"
        params = {"lat": lat, "lon": lon, "date": date_str}

        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        # Extract hourly temps and derive max/min for the target date
        temps = []
        for record in data.get("weather", []):
            ts = record.get("timestamp", "")
            if date_str in ts:
                t = _safe_float(record.get("temperature"))
                if t is not None:
                    temps.append(t)

        if temps:
            max_temp = max(temps)
            min_temp = min(temps)
            logger.info(f"Bright Sky forecast: max={max_temp:.1f}°C min={min_temp:.1f}°C")
            return {"max_temp_c": max_temp, "min_temp_c": min_temp, "source": "brightsky"}
        return None

    async def _fetch_nea(self, target_date: datetime) -> Optional[dict]:
        """Singapore NEA — data.gov.sg"""
        session = await self._get_session()
        url = "https://api.data.gov.sg/v1/environment/4-day-weather-forecast"

        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        target_str = target_date.strftime("%Y-%m-%d")
        for item in data.get("items", []):
            for forecast in item.get("forecasts", []):
                if forecast.get("date") == target_str:
                    temp = forecast.get("temperature", {})
                    high = _safe_float(temp.get("high"))
                    low = _safe_float(temp.get("low"))
                    if high is not None:
                        logger.info(f"NEA forecast: max={high}°C min={low}°C")
                        return {"max_temp_c": high, "min_temp_c": low, "source": "nea"}
        return None

    async def _fetch_bom(
        self, geohash: str, target_date: datetime
    ) -> Optional[dict]:
        """Australia BOM — undocumented API (may be geo-restricted)."""
        if not geohash:
            return None

        session = await self._get_session()
        url = f"https://api.weather.bom.gov.au/v1/locations/{geohash}/forecasts/daily"
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }

        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.debug(f"BOM API returned HTTP {resp.status} (may be geo-restricted)")
                    return None
                data = await resp.json()
        except (asyncio.TimeoutError, Exception):
            return None

        target_str = target_date.strftime("%Y-%m-%d")
        for day in data.get("data", []):
            date_val = day.get("date", "")
            if target_str in date_val:
                max_temp = _safe_float(day.get("temp_max"))
                min_temp = _safe_float(day.get("temp_min"))
                if max_temp is not None:
                    logger.info(f"BOM forecast: max={max_temp}°C min={min_temp}°C")
                    return {"max_temp_c": max_temp, "min_temp_c": min_temp, "source": "bom"}
        return None


# ─── Helpers ──────────────────────────────────────────────

def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _parse_awc_time(val) -> Optional[datetime]:
    """Parse AWC time values which can be Unix timestamps (int) or ISO strings."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
