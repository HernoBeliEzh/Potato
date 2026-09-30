from __future__ import annotations

import asyncio
import time
from collections import deque
from contextvars import ContextVar
import logging

from telethon import TelegramClient, functions


API_PRIORITY = ContextVar("potato_api_priority", default=False)
logger = logging.getLogger(__name__)
SERVICE_REQUESTS = (
    functions.updates.GetDifferenceRequest,
    functions.updates.GetChannelDifferenceRequest,
    functions.updates.GetStateRequest,
    functions.PingRequest,
    functions.PingDelayDisconnectRequest,
)


class RequestBudget:
    def __init__(self, window=10, total=30, ordinary=25):
        self.window = window
        self.total = total
        self.ordinary = ordinary
        self.calls = deque()
        self.lock = asyncio.Lock()

    async def acquire(self, priority=False):
        started = time.monotonic()
        while True:
            async with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0][0] >= self.window:
                    self.calls.popleft()
                ordinary = [stamp for stamp, urgent in self.calls if not urgent]
                if len(self.calls) < self.total and (priority or len(ordinary) < self.ordinary):
                    self.calls.append((now, priority))
                    return time.monotonic() - started
                stamp = self.calls[0][0] if len(self.calls) >= self.total else ordinary[0]
                delay = max(0.001, stamp + self.window - now)
            await asyncio.sleep(delay)


class ProtectedTelegramClient(TelegramClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.policy = None
        self.budget = RequestBudget()
        self.api_wait_seconds = 0.0
        self.api_request_seconds = 0.0

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        policy = self.policy
        if policy is not None and policy.state.get("api_protection_enabled", True):
            if time.time() >= policy.state.get("api_protection_suspended_until", 0):
                requests = request if isinstance(request, (list, tuple)) else (request,)
                for item in requests:
                    if not isinstance(item, SERVICE_REQUESTS):
                        waited = await self.budget.acquire(API_PRIORITY.get())
                        self.api_wait_seconds += waited
                        if waited >= 0.1:
                            logger.info("API limiter request=%s wait_ms=%.0f", type(item).__name__, waited * 1000)
        started = time.monotonic()
        try:
            return await super()._call(sender, request, ordered, flood_sleep_threshold)
        finally:
            elapsed = time.monotonic() - started
            self.api_request_seconds += elapsed
            if elapsed >= 1:
                logger.info("Telegram request=%s duration_ms=%.0f", type(request).__name__, elapsed * 1000)
