from __future__ import annotations

import asyncio
import hmac
import importlib.util
import inspect
import logging
import re
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from aiohttp import ClientSession, web
from telethon import TelegramClient

from potato.api import CommandSpec, IntervalSpec, MessageSpec, Module, ModuleContext, WebhookSpec
from potato.releases import ReleaseManager
from potato.settings import Settings
from potato.storage import Storage


logger = logging.getLogger(__name__)
ROUTE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


@dataclass(frozen=True)
class CommandHandler:
    module: str
    callback: Callable
    spec: CommandSpec


@dataclass(frozen=True)
class MessageHandler:
    module: str
    callback: Callable
    spec: MessageSpec
    pattern: re.Pattern | None


@dataclass(frozen=True)
class IntervalHandler:
    module: str
    callback: Callable
    spec: IntervalSpec


@dataclass(frozen=True)
class WebhookHandler:
    module: str
    callback: Callable
    spec: WebhookSpec
    verifier: Callable | None
    secret: str | None


class ModuleManager:
    def __init__(
        self,
        settings: Settings,
        client: TelegramClient,
        owner_id: int,
        storage: Storage,
        http: ClientSession,
        releases: ReleaseManager,
        shutdown: asyncio.Event,
    ):
        self.settings = settings
        self.client = client
        self.owner_id = owner_id
        self.storage = storage
        self.http = http
        self.releases = releases
        self.shutdown = shutdown
        self.started_at = time.monotonic()
        self.modules: dict[str, Module] = {}
        self.commands: dict[str, CommandHandler] = {}
        self.messages: list[MessageHandler] = []
        self.webhooks: dict[tuple[str, str, str], WebhookHandler] = {}
        self.interval_tasks: list[asyncio.Task] = []
        self.failures: list[str] = []

    async def load_all(self) -> None:
        from potato.builtins import PotatoHelp, PotatoInfo, PotatoLoader, PotatoTester

        await self._load_class("potato_help", PotatoHelp)
        await self._load_class("system", PotatoInfo)
        await self._load_class("module_admin", PotatoLoader)
        await self._load_class("potato_tester", PotatoTester)
        active = self.releases.active_modules()
        if active is None:
            return
        for path in sorted(active.glob("*.py")):
            try:
                module_class = self._import_class(path)
                await self._load_class(path.stem, module_class)
            except Exception as error:
                self._record_failure(path.stem, error)

    async def _load_class(self, identifier: str, module_class: type[Module]) -> None:
        if identifier in self.modules:
            raise ValueError(f"Module {identifier} is already loaded")

        module_storage = self.storage.for_module(self.settings.account, identifier)
        context = ModuleContext(
            account=self.settings.account,
            owner_id=self.owner_id,
            client=self.client,
            http=self.http,
            storage=module_storage,
            manager=self,
            source_url=self.settings.source_url,
            request_restart=self.shutdown.set,
        )
        instance = module_class(context)
        commands: list[CommandHandler] = []
        messages: list[MessageHandler] = []
        intervals: list[IntervalHandler] = []
        webhooks: list[WebhookHandler] = []
        new_commands: set[str] = set()
        new_routes: set[tuple[str, str, str]] = set()

        for _, callback in inspect.getmembers(instance, predicate=inspect.ismethod):
            command_spec = getattr(callback, "__potato_command__", None)
            message_spec = getattr(callback, "__potato_message__", None)
            interval_spec = getattr(callback, "__potato_interval__", None)
            webhook_spec = getattr(callback, "__potato_webhook__", None)
            if not any((command_spec, message_spec, interval_spec, webhook_spec)):
                continue
            if not inspect.iscoroutinefunction(callback):
                raise TypeError("Module handlers must be async")

            if command_spec is not None:
                for name in (command_spec.name, *command_spec.aliases):
                    normalized = name.casefold()
                    valid_name = (
                        1 <= len(normalized) <= 64
                        and normalized[0].isalpha()
                        and all(character.isalnum() or character in "_-" for character in normalized[1:])
                    )
                    if not valid_name or normalized in self.commands or normalized in new_commands:
                        raise ValueError(f"Invalid or duplicate command: {normalized}")
                    new_commands.add(normalized)
                commands.append(CommandHandler(identifier, callback, command_spec))

            if message_spec is not None:
                if not message_spec.incoming and not message_spec.outgoing:
                    raise ValueError("Message handler must receive incoming or outgoing messages")
                pattern = re.compile(message_spec.pattern) if message_spec.pattern is not None else None
                messages.append(MessageHandler(identifier, callback, message_spec, pattern))

            if interval_spec is not None:
                if interval_spec.seconds <= 0:
                    raise ValueError("Interval must be positive")
                intervals.append(IntervalHandler(identifier, callback, interval_spec))

            if webhook_spec is not None:
                if not ROUTE_NAME.fullmatch(webhook_spec.name):
                    raise ValueError("Invalid webhook route")
                methods = tuple(method.upper() for method in webhook_spec.methods)
                if not methods or any(method not in {"GET", "POST", "PUT", "PATCH", "DELETE"} for method in methods):
                    raise ValueError("Invalid webhook method")
                verifier = None
                secret = None
                if webhook_spec.verifier is not None:
                    verifier = getattr(instance, webhook_spec.verifier, None)
                    if verifier is None or not inspect.iscoroutinefunction(verifier):
                        raise ValueError("Webhook verifier must be an async method")
                else:
                    key = f"webhook:{webhook_spec.name}"
                    secret = await module_storage.get(key)
                    if secret is None:
                        secret = secrets.token_urlsafe(32)
                        await module_storage.set(key, secret)
                for method in methods:
                    route_key = (identifier, webhook_spec.name, method)
                    if route_key in self.webhooks or route_key in new_routes:
                        raise ValueError("Duplicate webhook route")
                    new_routes.add(route_key)
                webhooks.append(WebhookHandler(identifier, callback, webhook_spec, verifier, secret))

        on_load = getattr(instance, "on_load", None)
        if on_load is not None:
            if not inspect.iscoroutinefunction(on_load):
                raise TypeError("on_load must be async")
            await on_load()

        self.modules[identifier] = instance
        for handler in commands:
            for name in (handler.spec.name, *handler.spec.aliases):
                self.commands[name.casefold()] = handler
        self.messages.extend(messages)
        for handler in webhooks:
            for method in handler.spec.methods:
                self.webhooks[(identifier, handler.spec.name, method.upper())] = handler
        for handler in intervals:
            self.interval_tasks.append(asyncio.create_task(self._run_interval(handler)))

    @staticmethod
    def _import_class(path: Path) -> type[Module]:
        name = f"potato_user_{path.stem}"
        specification = importlib.util.spec_from_file_location(name, path)
        if specification is None or specification.loader is None:
            raise ImportError(f"Cannot import {path.name}")
        imported = importlib.util.module_from_spec(specification)
        sys.modules[name] = imported
        try:
            specification.loader.exec_module(imported)
        except Exception:
            sys.modules.pop(name, None)
            raise
        classes = [
            value
            for value in vars(imported).values()
            if inspect.isclass(value)
            and issubclass(value, Module)
            and value is not Module
            and value.__module__ == name
        ]
        if len(classes) != 1:
            raise ValueError("Module file must define exactly one Module subclass")
        return classes[0]

    async def _run_interval(self, handler: IntervalHandler) -> None:
        try:
            if not handler.spec.run_on_start:
                await asyncio.sleep(handler.spec.seconds)
            while True:
                await self._safe_call(handler.module, handler.callback)
                await asyncio.sleep(handler.spec.seconds)
        except asyncio.CancelledError:
            raise

    async def dispatch_message(self, event: Any) -> None:
        content = event.raw_text or ""
        if content.startswith("."):
            parts = content[1:].split(maxsplit=1)
            handler = self.commands.get(parts[0].casefold()) if parts else None
            if handler is not None and (
                not handler.spec.owner_only or event.out or event.sender_id == self.owner_id
            ):
                await self._safe_call(handler.module, handler.callback, event)

        calls = []
        for handler in self.messages:
            if event.out and not handler.spec.outgoing:
                continue
            if not event.out and not handler.spec.incoming:
                continue
            if handler.pattern is not None and not handler.pattern.search(content):
                continue
            calls.append(self._safe_call(handler.module, handler.callback, event))
        if calls:
            await asyncio.gather(*calls)

    async def handle_webhook(self, request: web.Request) -> web.StreamResponse:
        if request.match_info["account"] != self.settings.account:
            raise web.HTTPNotFound()
        identifier = request.match_info["module"]
        route = request.match_info["route"]
        handler = self.webhooks.get((identifier, route, request.method))
        if handler is None:
            raise web.HTTPNotFound()

        if handler.verifier is not None:
            try:
                allowed = await handler.verifier(request)
            except Exception as error:
                self._record_failure(identifier, error)
                raise web.HTTPUnauthorized() from error
            if not allowed:
                raise web.HTTPUnauthorized()
        else:
            authorization = request.headers.get("Authorization", "")
            expected = f"Bearer {handler.secret}"
            if not hmac.compare_digest(authorization, expected):
                raise web.HTTPUnauthorized()

        try:
            result = await handler.callback(request)
        except web.HTTPException:
            raise
        except Exception as error:
            self._record_failure(identifier, error)
            raise web.HTTPInternalServerError() from error
        if isinstance(result, web.StreamResponse):
            return result
        if result is None:
            return web.Response(status=204)
        return web.json_response(result)

    async def _safe_call(self, identifier: str, callback: Callable, *args: Any) -> None:
        try:
            await callback(*args)
        except Exception as error:
            self._record_failure(identifier, error)

    def _record_failure(self, identifier: str, error: Exception) -> None:
        message = f"{identifier}: {type(error).__name__}: {error}"[:300]
        self.failures.append(message)
        if len(self.failures) > 20:
            self.failures.pop(0)
        logger.error("Module %s failed with %s", identifier, type(error).__name__)

    async def close(self) -> None:
        for task in self.interval_tasks:
            task.cancel()
        if self.interval_tasks:
            await asyncio.gather(*self.interval_tasks, return_exceptions=True)
        for identifier, instance in self.modules.items():
            on_unload = getattr(instance, "on_unload", None)
            if on_unload is not None:
                try:
                    await on_unload()
                except Exception as error:
                    self._record_failure(identifier, error)
