"""
Polymarket Client
=================
Handles market discovery (Gamma API), order book reading (CLOB API),
and trade execution via py-clob-client SDK.
"""
import re
import json
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional
import aiohttp

from config import PolymarketConfig, CITY_ICAO_MAP
from data.temperature_predictor import TempBand

logger = logging.getLogger(__name__)


@dataclass
class TemperatureMarket:
    """A discovered Polymarket temperature market."""
    event_id: str
    condition_id: str
    question: str
    city: str
    target_date: datetime
    end_date: datetime
    volume_usd: float
    liquidity_usd: float
    active: bool
    outcomes: list[dict] = field(default_factory=list)
    # Each outcome: {"label": "20°C", "token_id": "...", "price": 0.35}
    bands: list[TempBand] = field(default_factory=list)
    unit: str = "C"  # C or F
    slug: str = ""


@dataclass
class OrderBookLevel:
    """Single price level in the order book."""
    price: float
    size: float


@dataclass
class OrderBook:
    """Full order book for a token."""
    token_id: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    midpoint: float
    spread: float
    best_bid: float
    best_ask: float


@dataclass
class Position:
    """Current position in a market."""
    token_id: str
    market_label: str
    side: str  # BUY or SELL
    size: float
    avg_price: float
    current_price: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    price_stale: bool = False


@dataclass
class TradeResult:
    """Result of a trade attempt."""
    success: bool
    order_id: Optional[str] = None
    token_id: str = ""
    side: str = ""
    price: float = 0.0
    size: float = 0.0
    error: Optional[str] = None
    dry_run: bool = False


class PolymarketClient:
    """
    Client for Polymarket APIs.

    Uses Gamma API for market discovery and CLOB API for trading.
    Wraps py-clob-client for authenticated operations.
    """

    def __init__(self, config: PolymarketConfig, dry_run: bool = True):
        self.config = config
        self.dry_run = dry_run
        self._session: Optional[aiohttp.ClientSession] = None
        self._clob_client = None  # initialized on first trade
        self._positions_cache: list[Position] = []

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _get_clob_client(self):
        """Lazy-initialize the py-clob-client for authenticated operations."""
        if self._clob_client is None:
            try:
                from py_clob_client.client import ClobClient
                self._clob_client = ClobClient(
                    self.config.clob_api_url,
                    key=self.config.private_key,
                    chain_id=self.config.chain_id,
                    signature_type=self.config.signature_type,
                    funder=self.config.funder_address,
                )
                if self.config.private_key:
                    self._clob_client.set_api_creds(
                        self._clob_client.create_or_derive_api_creds()
                    )
                    logger.info("CLOB client authenticated successfully")
                else:
                    logger.warning("No private key — CLOB client in read-only mode")
            except ImportError:
                logger.warning(
                    "py-clob-client not installed. Install with: "
                    "pip install py-clob-client"
                )
                return None
        return self._clob_client

    # ─── Market Discovery ─────────────────────────────────────

    async def discover_temperature_markets(self) -> list[TemperatureMarket]:
        """
        Search Gamma API for active daily temperature markets.
        Uses the events endpoint with weather tag to find temperature events,
        then parses their markets for city, date, and temperature bands.
        Falls back to simulated markets for testing when none are live.
        """
        session = await self._get_session()
        markets = []

        # Search events endpoint with weather tag slug (where temp markets live)
        # Paginate to get all events (there can be 200+)
        url = f"{self.config.gamma_api_url}/events"
        for offset in range(0, 500, 100):
            params = {
                "tag_slug": "weather",
                "active": "true",
                "closed": "false",
                "limit": 100,
                "offset": offset,
            }

            try:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logger.warning(f"Gamma API events error: HTTP {resp.status}")
                        break

                    data = await resp.json()
                    if not isinstance(data, list) or not data:
                        break

                    for event in data:
                        market = self._parse_temperature_event(event)
                        if market and market.city in CITY_ICAO_MAP:
                            markets.append(market)

                    if len(data) < 100:
                        break  # last page

            except Exception as e:
                logger.error(f"Market discovery error: {e}")
                break

        # Deduplicate by condition_id
        seen = set()
        unique = []
        for m in markets:
            if m.condition_id not in seen:
                seen.add(m.condition_id)
                unique.append(m)

        # If no real markets found and in dry-run mode, generate simulated markets
        if not unique and self.dry_run:
            logger.info("No live temperature markets — generating simulated markets for testing")
            unique = self._generate_simulated_markets()

        logger.info(f"Discovered {len(unique)} temperature markets")
        return unique

    def _generate_simulated_markets(self) -> list[TemperatureMarket]:
        """Generate realistic simulated temperature markets for dry-run testing."""
        from zoneinfo import ZoneInfo
        import random

        now = datetime.now(timezone.utc)
        markets = []

        # Simulate markets for a few cities at different time horizons
        sim_cities = [
            ("New York", [15, 16, 17, 18, 19]),
            ("London", [10, 11, 12, 13, 14]),
            ("Tokyo", [20, 21, 22, 23, 24]),
            ("Los Angeles", [22, 23, 24, 25, 26]),
        ]

        for city, temp_range in sim_cities:
            city_info = CITY_ICAO_MAP.get(city, {})
            tz_name = city_info.get("tz", "UTC")
            local_tz = ZoneInfo(tz_name)

            # Market resolves tomorrow at 14:00 local time
            tomorrow_local = (now.astimezone(local_tz) + timedelta(days=1)).replace(
                hour=14, minute=0, second=0, microsecond=0
            )
            target_utc = tomorrow_local.astimezone(timezone.utc)

            # Build bands with simulated market prices
            bands = []
            prices = self._sim_price_distribution(len(temp_range))
            for i, temp_c in enumerate(temp_range):
                label = f"{temp_c}°C"
                if i == 0:
                    label = f"{temp_c}°C or lower"
                elif i == len(temp_range) - 1:
                    label = f"{temp_c}°C or higher"

                low = temp_c - 0.5 if i > 0 else -100.0
                high = temp_c + 0.5 if i < len(temp_range) - 1 else 100.0

                bands.append(TempBand(
                    label=label,
                    low_c=low,
                    high_c=high,
                    token_id=f"sim_{city.lower().replace(' ', '_')}_{temp_c}",
                    market_prob=prices[i],
                ))

            markets.append(TemperatureMarket(
                event_id=f"sim_event_{city.lower().replace(' ', '_')}",
                condition_id=f"sim_cond_{city.lower().replace(' ', '_')}",
                question=f"Highest temperature in {city} on {target_utc.strftime('%B %-d')}?",
                city=city,
                target_date=target_utc,
                end_date=target_utc + timedelta(hours=6),
                volume_usd=5000.0,
                liquidity_usd=2000.0,
                active=True,
                bands=bands,
                unit="C",
                slug=f"sim-temp-{city.lower().replace(' ', '-')}",
            ))

        return markets

    @staticmethod
    def _sim_price_distribution(n: int) -> list[float]:
        """Generate a plausible market price distribution summing to ~1.0."""
        import random
        # Bell-curve-ish: peak in the middle
        raw = [random.gauss(0, 1) for _ in range(n)]
        center = n // 2
        weighted = [max(0.02, abs(r) * (1.0 - 0.3 * abs(i - center))) for i, r in enumerate(raw)]
        total = sum(weighted)
        return [round(w / total, 3) for w in weighted]

    def _parse_temperature_event(self, event: dict) -> Optional[TemperatureMarket]:
        """
        Parse a Gamma API event into a TemperatureMarket.
        Each event contains multiple binary markets (one per temperature band).
        The "Yes" price on each market = probability for that band.
        """
        title = event.get("title", "")

        # Match event title: "Highest temperature in Paris on April 8?"
        pattern = r"(?:Highest|High)\s+temperature\s+in\s+(.+?)\s+on\s+(.+?)\?"
        match = re.search(pattern, title, re.IGNORECASE)
        if not match:
            return None

        city = match.group(1).strip()
        date_str = match.group(2).strip()

        target_date = self._parse_market_date(date_str, city=city)
        if not target_date:
            return None

        end_date_str = event.get("endDate", "")
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            end_date = target_date

        # Each sub-market is a binary Yes/No for one temperature band
        bands = []
        unit = "C"
        event_markets = event.get("markets", [])

        for mkt in event_markets:
            if mkt.get("closed"):
                continue

            question = mkt.get("question", "")
            # Extract temperature band from groupItemTitle or question
            band_label = mkt.get("groupItemTitle", "")
            if not band_label:
                # Try parsing from question: "Will the highest temperature in X be 20°C on Y?"
                band_match = re.search(r"be\s+(.+?)\s+on\s+", question)
                if band_match:
                    band_label = band_match.group(1).strip()

            if not band_label:
                continue

            # Get the "Yes" token and its price
            try:
                token_ids = json.loads(mkt.get("clobTokenIds", "[]")) if isinstance(mkt.get("clobTokenIds"), str) else (mkt.get("clobTokenIds") or [])
                prices = json.loads(mkt.get("outcomePrices", "[]")) if isinstance(mkt.get("outcomePrices"), str) else (mkt.get("outcomePrices") or [])
            except (json.JSONDecodeError, TypeError):
                token_ids = []
                prices = []

            # First token is "Yes", second is "No"
            yes_token = token_ids[0] if len(token_ids) > 0 else ""
            # Prefer bestAsk (live neg-risk derived price) over outcomePrices (stale)
            try:
                best_ask = float(mkt.get("bestAsk", 0))
            except (ValueError, TypeError):
                best_ask = 0.0
            if best_ask and best_ask > 0:
                yes_price = best_ask
            else:
                yes_price = float(prices[0]) if len(prices) > 0 else 0.0

            if "°F" in band_label:
                unit = "F"

            band = self._parse_temp_band(band_label, yes_token, yes_price)
            if band:
                bands.append(band)

        if not bands:
            return None

        # Sort bands by low_c
        bands.sort(key=lambda b: b.low_c)

        return TemperatureMarket(
            event_id=event.get("id", ""),
            condition_id=event.get("id", ""),  # use event id as unique key
            question=title,
            city=city,
            target_date=target_date,
            end_date=end_date,
            volume_usd=float(event.get("volume", 0)),
            liquidity_usd=float(event.get("liquidity", 0)),
            active=event.get("active", True) and not event.get("closed", False),
            bands=bands,
            unit=unit,
            slug=event.get("slug", ""),
        )

    def _parse_temperature_market(self, data: dict) -> Optional[TemperatureMarket]:
        """
        Parse a Gamma API market response into a TemperatureMarket.
        Extracts city, date, and temperature bands from the question text.
        """
        question = data.get("question", "")

        # Match patterns like:
        # "Highest temperature in London on April 9?"
        # "Will the highest temperature in Paris be 20°C on April 8?"
        # "Highest temperature in Seattle on April 7?"
        pattern = r"(?:(?:Will\s+the\s+)?[Hh]ighest|High)\s+temperature\s+in\s+(.+?)\s+(?:be\s+.+?\s+)?on\s+(.+?)\?"
        match = re.search(pattern, question, re.IGNORECASE)
        if not match:
            return None

        city = match.group(1).strip()
        date_str = match.group(2).strip()

        # Parse the target date (with city timezone for correct peak hour)
        target_date = self._parse_market_date(date_str, city=city)
        if not target_date:
            return None

        # Parse end date
        end_date_str = data.get("endDate", "")
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            end_date = target_date

        # Parse outcomes into temperature bands
        outcomes = []
        bands = []
        unit = "C"

        outcomes_raw = data.get("outcomes", "")
        token_ids_raw = data.get("clobTokenIds", "")
        outcome_prices = data.get("outcomePrices", "")

        try:
            if isinstance(outcomes_raw, str):
                outcome_list = json.loads(outcomes_raw)
            else:
                outcome_list = outcomes_raw or []

            if isinstance(token_ids_raw, str):
                token_list = json.loads(token_ids_raw)
            else:
                token_list = token_ids_raw or []

            if isinstance(outcome_prices, str):
                price_list = json.loads(outcome_prices)
            else:
                price_list = outcome_prices or []

        except (json.JSONDecodeError, TypeError):
            outcome_list = []
            token_list = []
            price_list = []

        for i, label in enumerate(outcome_list):
            token_id = token_list[i] if i < len(token_list) else ""
            price = float(price_list[i]) if i < len(price_list) else 0.0

            # Parse temperature from label
            band = self._parse_temp_band(label, token_id, price)
            if band:
                bands.append(band)
                if "°F" in label:
                    unit = "F"

            outcomes.append({
                "label": label,
                "token_id": token_id,
                "price": price,
            })

        if not bands:
            return None

        return TemperatureMarket(
            event_id=data.get("id", ""),
            condition_id=data.get("conditionId", ""),
            question=question,
            city=city,
            target_date=target_date,
            end_date=end_date,
            volume_usd=float(data.get("volume", 0)),
            liquidity_usd=float(data.get("liquidity", 0)),
            active=data.get("active", True),
            outcomes=outcomes,
            bands=bands,
            unit=unit,
            slug=data.get("slug", ""),
        )

    def _parse_temp_band(
        self, label: str, token_id: str, price: float
    ) -> Optional[TempBand]:
        """Parse a temperature band label like '20°C' or '68-69°F'."""

        # Pattern: single degree Celsius "20°C"
        match = re.match(r"(-?\d+)°C", label)
        if match:
            temp = int(match.group(1))
            return TempBand(
                label=label,
                low_c=temp - 0.5,
                high_c=temp + 0.5,
                token_id=token_id,
                market_prob=price,
            )

        # Pattern: "X°C or higher" / "X°C or lower"
        match = re.match(r"(-?\d+)°C\s+or\s+(higher|lower)", label, re.IGNORECASE)
        if match:
            temp = int(match.group(1))
            direction = match.group(2).lower()
            if direction == "higher":
                return TempBand(label=label, low_c=temp - 0.5, high_c=100.0,
                                token_id=token_id, market_prob=price)
            else:
                return TempBand(label=label, low_c=-100.0, high_c=temp + 0.5,
                                token_id=token_id, market_prob=price)

        # Pattern: Fahrenheit range "68-69°F"
        match = re.match(r"(-?\d+)-(-?\d+)°F", label)
        if match:
            f_low = int(match.group(1))
            f_high = int(match.group(2))
            c_low = (f_low - 32) * 5 / 9
            c_high = (f_high + 1 - 32) * 5 / 9  # +1 for inclusive upper
            return TempBand(
                label=label,
                low_c=c_low,
                high_c=c_high,
                token_id=token_id,
                market_prob=price,
            )

        # Pattern: Fahrenheit "X°F or higher/lower"
        match = re.match(r"(-?\d+)°F\s+or\s+(higher|lower)", label, re.IGNORECASE)
        if match:
            f_temp = int(match.group(1))
            c_temp = (f_temp - 32) * 5 / 9
            direction = match.group(2).lower()
            if direction == "higher":
                return TempBand(label=label, low_c=c_temp, high_c=100.0,
                                token_id=token_id, market_prob=price)
            else:
                return TempBand(label=label, low_c=-100.0, high_c=c_temp,
                                token_id=token_id, market_prob=price)

        logger.debug(f"Could not parse temperature band: {label}")
        return None

    def _parse_market_date(self, date_str: str, city: str = "") -> Optional[datetime]:
        """Parse date strings like 'April 9' into datetime with city-local peak hour."""
        from dateutil import parser as dateparser
        from zoneinfo import ZoneInfo
        try:
            # Add current year if not specified
            now = datetime.now(timezone.utc)
            full_str = f"{date_str} {now.year}"
            dt = dateparser.parse(full_str)
            if dt:
                # Set to 14:00 local time for the city, then convert to UTC
                city_info = CITY_ICAO_MAP.get(city, {})
                tz_name = city_info.get("tz", "UTC")
                local_tz = ZoneInfo(tz_name)
                local_dt = dt.replace(hour=14, minute=0, second=0, tzinfo=local_tz)
                return local_dt.astimezone(timezone.utc)
        except Exception:
            pass

        return None

    # ─── Order Book ───────────────────────────────────────────

    async def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """Fetch the order book for a specific outcome token."""
        client = self._get_clob_client()
        if not client:
            # Fallback to direct API call
            return await self._fetch_orderbook_api(token_id)

        try:
            book = client.get_order_book(token_id)
            # py-clob-client may return an object or a dict
            if hasattr(book, '__dict__') and not isinstance(book, dict):
                book = vars(book) if hasattr(book, 'bids') else book.__dict__
            if hasattr(book, 'bids'):
                raw_bids = book.bids if not isinstance(book, dict) else book.get("bids", [])
                raw_asks = book.asks if not isinstance(book, dict) else book.get("asks", [])
            else:
                raw_bids = book.get("bids", []) if isinstance(book, dict) else []
                raw_asks = book.get("asks", []) if isinstance(book, dict) else []

            def _parse_level(lvl):
                if isinstance(lvl, dict):
                    return OrderBookLevel(float(lvl.get("price", 0)), float(lvl.get("size", 0)))
                return OrderBookLevel(float(getattr(lvl, 'price', 0)), float(getattr(lvl, 'size', 0)))

            bids = [_parse_level(b) for b in raw_bids]
            asks = [_parse_level(a) for a in raw_asks]

            best_bid = bids[0].price if bids else 0.0
            best_ask = asks[0].price if asks else 1.0
            midpoint = (best_bid + best_ask) / 2
            spread = best_ask - best_bid

            return OrderBook(
                token_id=token_id,
                bids=bids,
                asks=asks,
                midpoint=midpoint,
                spread=spread,
                best_bid=best_bid,
                best_ask=best_ask,
            )
        except Exception as e:
            logger.error(f"Order book error: {e}")
            return None

    async def _fetch_orderbook_api(self, token_id: str) -> Optional[OrderBook]:
        """Direct REST call to get order book (no SDK needed)."""
        session = await self._get_session()
        url = f"{self.config.clob_api_url}/book"
        params = {"token_id": token_id}

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

                bids = [
                    OrderBookLevel(float(b["price"]), float(b["size"]))
                    for b in data.get("bids", [])
                ]
                asks = [
                    OrderBookLevel(float(a["price"]), float(a["size"]))
                    for a in data.get("asks", [])
                ]

                best_bid = bids[0].price if bids else 0.0
                best_ask = asks[0].price if asks else 1.0

                return OrderBook(
                    token_id=token_id,
                    bids=bids,
                    asks=asks,
                    midpoint=(best_bid + best_ask) / 2,
                    spread=best_ask - best_bid,
                    best_bid=best_bid,
                    best_ask=best_ask,
                )
        except Exception as e:
            logger.error(f"Order book API error: {e}")
            return None

    # ─── Trading ──────────────────────────────────────────────

    async def place_order(
        self,
        token_id: str,
        side: str,     # "BUY" or "SELL"
        price: float,
        size: float,
        tick_size: str = "0.01",
    ) -> TradeResult:
        """Place a limit order on Polymarket."""

        if self.dry_run:
            logger.info(
                f"[DRY RUN] {side} {size:.1f} shares of {token_id[:12]}... "
                f"@ ${price:.3f}"
            )
            return TradeResult(
                success=True,
                order_id=f"dry_run_{datetime.now().timestamp():.0f}",
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                dry_run=True,
            )

        client = self._get_clob_client()
        if not client:
            return TradeResult(
                success=False,
                error="CLOB client not available — install py-clob-client and set credentials",
                token_id=token_id,
                side=side,
                price=price,
                size=size,
            )

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            order_side = BUY if side.upper() == "BUY" else SELL

            order = client.create_and_post_order(
                OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=order_side,
                ),
                options={
                    "tickSize": tick_size,
                    "negRisk": False,
                },
            )

            order_id = order.get("orderID", order.get("id", "unknown"))
            logger.info(
                f"ORDER PLACED: {side} {size:.1f} shares of {token_id[:12]}... "
                f"@ ${price:.3f} — Order ID: {order_id}"
            )

            return TradeResult(
                success=True,
                order_id=order_id,
                token_id=token_id,
                side=side,
                price=price,
                size=size,
            )

        except Exception as e:
            logger.error(f"Order placement error: {e}")
            return TradeResult(
                success=False,
                error=str(e),
                token_id=token_id,
                side=side,
                price=price,
                size=size,
            )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if self.dry_run:
            logger.info(f"[DRY RUN] Cancel order {order_id}")
            return True

        client = self._get_clob_client()
        if not client:
            return False

        try:
            client.cancel(order_id)
            logger.info(f"Order cancelled: {order_id}")
            return True
        except Exception as e:
            logger.error(f"Cancel error: {e}")
            return False

    async def cancel_all_orders(self) -> bool:
        """Cancel all open orders."""
        if self.dry_run:
            logger.info("[DRY RUN] Cancel all orders")
            return True

        client = self._get_clob_client()
        if not client:
            return False

        try:
            client.cancel_all()
            logger.info("All orders cancelled")
            return True
        except Exception as e:
            logger.error(f"Cancel all error: {e}")
            return False

    # ─── Positions ────────────────────────────────────────────

    async def get_positions(self) -> list[Position]:
        """Fetch current positions."""
        client = self._get_clob_client()
        if not client:
            return self._positions_cache

        try:
            positions_raw = client.get_positions()
            positions = []
            for pos in positions_raw:
                size = float(pos.get("size", 0))
                if size == 0:
                    continue

                avg_price = float(pos.get("avgPrice", 0))
                # Get current price with retry
                token_id = pos.get("asset", "")
                current = avg_price
                price_stale = False
                for attempt in range(2):
                    try:
                        current = float(client.get_price(token_id, side="SELL"))
                        break
                    except Exception as e:
                        if attempt == 0:
                            import asyncio
                            await asyncio.sleep(0.5)
                        else:
                            logger.warning(
                                f"Price fetch failed for {token_id[:12]}, "
                                f"using avg_price: {e}"
                            )
                            price_stale = True

                pnl = (current - avg_price) * size
                pnl_pct = (current / avg_price - 1) * 100 if avg_price > 0 else 0

                positions.append(Position(
                    token_id=token_id,
                    market_label=pos.get("title", token_id[:12]),
                    side=pos.get("side", "BUY"),
                    size=size,
                    avg_price=avg_price,
                    current_price=current,
                    unrealized_pnl=pnl,
                    unrealized_pnl_pct=pnl_pct,
                    price_stale=price_stale,
                ))

            self._positions_cache = positions
            return positions

        except Exception as e:
            logger.error(f"Get positions error: {e}")
            return self._positions_cache

    async def get_balance(self) -> float:
        """Get USDC balance."""
        client = self._get_clob_client()
        if not client:
            return 0.0

        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            balance = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            return float(balance.get("balance", 0)) / 1e6  # USDC has 6 decimals
        except Exception as e:
            logger.error(f"Balance check error: {e}")
            return 0.0
