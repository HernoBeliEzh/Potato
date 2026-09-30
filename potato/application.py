from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web
from telethon import events

from potato.manager import ModuleManager
from potato.inline import InlineBot
from potato.releases import ReleaseManager
from potato.settings import Settings
from potato.storage import Storage
from potato.telegram_client import ProtectedTelegramClient as TelegramClient
from potato.services import AccountServices
from potato.updates import UpdateMonitor


logger = logging.getLogger(__name__)


class AccountRuntime:
    def __init__(
        self,
        settings: Settings,
        releases: ReleaseManager,
        storage: Storage,
        http: ClientSession,
    ):
        self.settings = settings
        self.releases = releases
        self.storage = storage
        self.http = http
        self.shutdown = asyncio.Event()
        self.client: TelegramClient | None = None
        self.manager: ModuleManager | None = None
        self.disconnected: asyncio.Task | None = None
        self.requested: asyncio.Task | None = None
        self.inline: InlineBot | None = None
        self.services = None
        self.updates = None

    async def start(self) -> None:
        self.settings.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(
            str(self.settings.session_path), self.settings.api_id, self.settings.api_hash
        )
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Сессия не авторизована. Сначала выполните potato login")
        account = await self.client.get_me()
        if account is None:
            raise RuntimeError("Не удалось определить владельца Telegram-сессии")

        self.manager = ModuleManager(
            self.settings,
            self.client,
            account.id,
            self.storage,
            self.http,
            self.releases,
            self.shutdown,
            getattr(account, "username", None),
        )
        self.client.policy = self.manager.policy
        self.services = AccountServices(self.manager)
        self.manager.services = self.services
        await self.services.load()
        self.inline = InlineBot(self.manager)
        self.manager.inline = self.inline
        self.releases.mark_attempted()
        await self.manager.load_all()
        if not self.releases.confirm_activation(self.manager.failures):
            raise RuntimeError("Новый модуль отклонён, восстановлена предыдущая версия")
        if await self.inline.start():
            logger.info("Potato inline bot connected: @%s", self.inline.username)
        else:
            await self.client.send_message("me", f"Potato Inline: {self.inline.error}")
        self.client.add_event_handler(self.manager.dispatch_message, events.NewMessage)
        if self.inline.ready:
            try:
                await self.client.send_message(f"@{self.inline.username}", "/start")
            except Exception as error:
                self.manager._record_failure("bot_start", error)
        await self.services.start()
        self.updates = UpdateMonitor(self.manager)
        self.manager.updates = self.updates
        if self.inline.ready:
            await self.updates.start()

        if self.releases.error_file.exists():
            error = self.releases.error_file.read_text(encoding="utf-8")
            await self.client.send_message("me", f"Модуль не установлен: {error}")
            self.releases.consume_error()
        if self.manager.failures:
            await self.client.send_message(
                "me", "Ошибки модулей:\n" + "\n".join(self.manager.failures[:10])
            )

        self.disconnected = asyncio.create_task(self.client.run_until_disconnected())
        self.requested = asyncio.create_task(self.shutdown.wait())
        logger.info("Potato started for account %s", self.settings.account)

    async def wait(self) -> None:
        if self.disconnected is None or self.requested is None:
            raise RuntimeError("Account has not started")
        await asyncio.wait(
            {self.disconnected, self.requested}, return_when=asyncio.FIRST_COMPLETED
        )
        if self.disconnected.done() and not self.requested.done():
            await self.disconnected

    async def close(self) -> None:
        if self.updates is not None:
            await self.updates.close()
        if self.services is not None:
            await self.services.close()
        for task in (self.disconnected, self.requested):
            if task is not None and not task.done():
                task.cancel()
        pending = [task for task in (self.disconnected, self.requested) if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.inline is not None:
            await self.inline.close()
        if self.manager is not None:
            await self.manager.close()
        if self.client is not None and self.client.is_connected():
            await self.client.disconnect()


class PotatoApplication:
    def __init__(self, settings: Settings, releases: ReleaseManager):
        self.settings = settings
        self.releases = releases
        self.accounts: dict[str, AccountRuntime] = {}

    async def run(self) -> None:
        started_at = time.monotonic()
        storage = Storage(self.settings.data_dir / "state.sqlite")
        await storage.open()
        runner = None
        try:
            async with ClientSession(
                timeout=ClientTimeout(total=30), connector=TCPConnector(limit=20)
            ) as http:
                account = AccountRuntime(self.settings, self.releases, storage, http)
                self.accounts[self.settings.account] = account
                try:
                    await account.start()
                    application = web.Application(client_max_size=1_048_576)
                    application.router.add_get("/healthz", self._healthcheck)
                    application.router.add_route(
                        "*", "/hooks/{account}/{module}/{route}", self._handle_webhook
                    )
                    runner = web.AppRunner(application)
                    await runner.setup()
                    site = web.TCPSite(
                        runner, self.settings.http_host, self.settings.http_port
                    )
                    await site.start()
                    await account.wait()
                    if account.shutdown.is_set():
                        await asyncio.sleep(max(0, 11 - (time.monotonic() - started_at)))
                finally:
                    if runner is not None:
                        await runner.cleanup()
                    await account.close()
        finally:
            await storage.close()

    async def _healthcheck(self, request: web.Request) -> web.Response:
        connected = bool(self.accounts) and all(
            account.client is not None and account.client.is_connected()
            for account in self.accounts.values()
        )
        if connected:
            return web.json_response({"status": "ok", "inline": all(account.inline is not None and account.inline.ready for account in self.accounts.values())})
        return web.json_response({"status": "disconnected"}, status=503)

    async def _handle_webhook(self, request: web.Request) -> web.StreamResponse:
        account = self.accounts.get(request.match_info["account"])
        if account is None or account.manager is None:
            raise web.HTTPNotFound()
        return await account.manager.handle_webhook(request)
