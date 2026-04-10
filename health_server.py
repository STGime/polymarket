"""
Health Server
=============
Lightweight HTTP server for Cloud Run health checks and status monitoring.
Runs alongside the trading engine on port 8080.
"""
import json
import logging
from aiohttp import web

logger = logging.getLogger(__name__)


class HealthServer:
    """HTTP server exposing /health and /status endpoints."""

    def __init__(self, engine, port: int = 8080):
        self.engine = engine
        self.port = port
        self._runner = None

    async def start(self):
        app = web.Application()
        app.router.add_get("/health", self._health)
        app.router.add_get("/status", self._status)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"Health server listening on port {self.port}")

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()

    async def _health(self, request):
        return web.json_response({"status": "ok"})

    async def _status(self, request):
        status = self.engine.get_full_status()
        return web.json_response(status, dumps=lambda obj: json.dumps(obj, default=str))
