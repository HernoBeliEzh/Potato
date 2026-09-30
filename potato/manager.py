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
from potato.i18n import ACTIVE_LOCALIZER, Localizer
from potato.policy import CorePolicy
from potato.releases import ReleaseManager
from potato.settings import Settings
from potato.storage import Storage
from potato.telegram_client import API_PRIORITY


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
        username: str | None = None,
    ):
        self.settings = settings
        self.client = client
        self.owner_id = owner_id
        self.storage = storage
        self.http = http
        self.releases = releases
        self.shutdown = shutdown
        self.username = username.casefold() if username else None
        self.started_at = time.monotonic()
        self.suspended_until = 0.0
        self.modules: dict[str, Module] = {}
        self.system_modules: set[str] = set()
        self.commands: dict[str, CommandHandler] = {}
        self.inline_commands: dict[str, CommandHandler] = {}
        self.inline = None
        self.services = None
        self.updates = None
        self.command_timings = {}
        self.messages: list[MessageHandler] = []
        self.webhooks: dict[tuple[str, str, str], WebhookHandler] = {}
        self.interval_tasks: list[asyncio.Task] = []
        self.failures: list[str] = []
        self.policy = CorePolicy(
            self.storage.for_module(self.settings.account, "__potato_core__"), owner_id
        )
        self.localizer = Localizer(self.storage.for_module(self.settings.account, "__potato_core__"))

    async def load_all(self) -> None:
        await self.policy.load()
        await self.localizer.load()
        from potato.builtins import PotatoHelp, PotatoInfo, PotatoLoader, PotatoTester
        from potato.system_api_limiter import APILimiter
        from potato.system_accounts import PotatoAccounts
        from potato.backups import PotatoBackups
        from potato.system_config import PotatoConfig
        from potato.system_evaluator import PotatoEvaluator
        from potato.system_security import PotatoSecurity
        from potato.system_settings import Settings as SettingsModule
        from potato.system_advanced_settings import PotatoSettings
        from potato.system_translations import Translations
        from potato.system_terminal import PotatoTerminal
        from potato.system_translator import PotatoTranslator

        for identifier, module_class in (
            ("potato_help", PotatoHelp),
            ("system", PotatoInfo),
            ("module_admin", PotatoLoader),
            ("potato_tester", PotatoTester),
            ("settings", SettingsModule),
            ("potato_settings", PotatoSettings),
            ("potato_security", PotatoSecurity),
            ("potato_terminal", PotatoTerminal),
            ("potato_config", PotatoConfig),
            ("potato_evaluator", PotatoEvaluator),
            ("potato_translator", PotatoTranslator),
            ("potato_translations", Translations),
            ("api_limiter", APILimiter),
            ("potato_accounts", PotatoAccounts),
            ("potato_backups", PotatoBackups),
        ):
            await self._load_class(identifier, module_class)
            self.system_modules.add(identifier)
        active = self.releases.active_modules()
        if active is None:
            return
        for path in sorted(active.glob("*.py")):
            try:
                from potato.releases import inspect_module
                info = inspect_module(path.name, path.read_bytes(), allow_heroku=True)
                if info.kind == "heroku" and not self.releases.allow_heroku:
                    continue
                module_class = self._import_class(path, heroku=info.kind == "heroku")
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
            module_id=identifier,
            localizer=self.localizer,
            inline=self.inline,
        )
        if getattr(module_class, "_potato_heroku", False):
            instance = module_class()
            await instance.bind(context)
        else:
            instance = module_class(context)
        self.localizer.register(identifier, instance.STRINGS)
        commands: list[CommandHandler] = []
        inline_commands: list[CommandHandler] = []
        messages: list[MessageHandler] = []
        intervals: list[IntervalHandler] = []
        webhooks: list[WebhookHandler] = []
        new_commands: set[str] = set()
        new_inline_commands: set[str] = set()
        new_routes: set[tuple[str, str, str]] = set()

        for _, callback in inspect.getmembers(instance, predicate=inspect.ismethod):
            command_spec = getattr(callback, "__potato_command__", None)
            inline_spec = getattr(callback, "__potato_inline__", None)
            message_spec = getattr(callback, "__potato_message__", None)
            interval_spec = getattr(callback, "__potato_interval__", None)
            webhook_spec = getattr(callback, "__potato_webhook__", None)
            if not any((command_spec, inline_spec, message_spec, interval_spec, webhook_spec)):
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

            if inline_spec is not None:
                for name in (inline_spec.name, *inline_spec.aliases):
                    normalized = name.casefold()
                    if not re.fullmatch(r"[^\W\d_][\w-]{0,63}", normalized) or normalized in self.inline_commands or normalized in new_inline_commands:
                        raise ValueError(f"Invalid or duplicate inline command: {normalized}")
                    new_inline_commands.add(normalized)
                inline_commands.append(CommandHandler(identifier, callback, inline_spec))

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
            try:
                await on_load()
            except Exception:
                if getattr(instance, "_potato_heroku", False):
                    await instance.close_adapter()
                raise

        self.modules[identifier] = instance
        for handler in commands:
            for name in (handler.spec.name, *handler.spec.aliases):
                self.commands[name.casefold()] = handler
        for handler in inline_commands:
            for name in (handler.spec.name, *handler.spec.aliases):
                self.inline_commands[name.casefold()] = handler
        self.messages.extend(messages)
        for handler in webhooks:
            for method in handler.spec.methods:
                self.webhooks[(identifier, handler.spec.name, method.upper())] = handler
        for handler in intervals:
            self.interval_tasks.append(asyncio.create_task(self._run_interval(handler)))

    @staticmethod
    def _import_class(path: Path, *, heroku=False) -> type[Module]:
        if heroku:
            from potato.heroku import install_imports
            install_imports()
        name = f"potato.heroku.modules.{path.stem}" if heroku else f"potato_user_{path.stem}"
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
                if handler.module not in self.policy.values("disabled_modules"):
                    await self._safe_call(handler.module, handler.callback)
                await asyncio.sleep(handler.spec.seconds)
        except asyncio.CancelledError:
            raise

    async def dispatch_message(self, event: Any) -> None:
        content = event.raw_text or ""
        if self.policy.is_blocked(event):
            return
        prefix = self.policy.state.get("prefix", ".")
        prefixes = sorted({".", prefix}, key=len, reverse=True)
        matched_prefix = next((item for item in prefixes if content.startswith(item)), None)
        if matched_prefix is not None:
            parts = content[len(matched_prefix):].split(maxsplit=1)
            requested = parts[0].casefold() if parts else ""
            name, separator, address = requested.partition("@")
            addressed = bool(separator and self.username and address == self.username)
            if separator and not addressed:
                name = ""
            name = self.policy.state.get("aliases", {}).get(name, name)
            handler = self.commands.get(name)
            if time.monotonic() < self.suspended_until and name not in {"suspend", "restart"}:
                return
            if handler is not None and self.policy.allows_address(event, name, addressed) and self.policy.authorized(event, name, handler.spec) and self.policy.is_enabled(name, handler.module, handler.spec.name.casefold()):
                event.potato_args = parts[1] if len(parts) > 1 else ""
                event.potato_command = name
                event.potato_language = self.localizer.locale
                started = time.monotonic()
                priority = API_PRIORITY.set(handler.module in self.system_modules)
                try:
                    await self._safe_call(handler.module, handler.callback, event)
                finally:
                    API_PRIORITY.reset(priority)
                    elapsed = time.monotonic() - started
                    self.command_timings[name] = elapsed
                    logger.info("Command name=%s duration_ms=%.0f", name, elapsed * 1000)

        if time.monotonic() < self.suspended_until:
            return
        calls = []
        for handler in self.messages:
            if handler.module in self.policy.values("disabled_modules"):
                continue
            if not self.policy.watcher_enabled(handler.module, event):
                continue
            if event.out and not handler.spec.outgoing:
                continue
            if not event.out and not handler.spec.incoming:
                continue
            if handler.pattern is not None and not handler.pattern.search(content):
                continue
            event.potato_language = self.localizer.locale
            calls.append(self._safe_call(handler.module, handler.callback, event))
        if calls:
            await asyncio.gather(*calls)

    def inline_available(self, user_id: int, name: str, handler: CommandHandler) -> bool:
        return (
            time.monotonic() >= self.suspended_until
            and self.policy.authorized_inline(user_id, handler.spec.name.casefold(), handler.spec)
            and self.policy.is_enabled(name, handler.module, handler.spec.name.casefold())
        )

    async def dispatch_inline(self, event: Any) -> None:
        if event.sender_id != self.owner_id or not self.policy.allows_inline_query(event.sender_id):
            await event.answer([], cache_time=0, private=True)
            return
        parts = (event.text or "").strip().split(maxsplit=1)
        name = parts[0].casefold() if parts else "help"
        handler = self.inline_commands.get(name)
        if handler is None or not self.inline_available(event.sender_id, name, handler):
            await event.answer([], cache_time=0, private=True)
            return
        event.potato_args = parts[1] if len(parts) > 1 else ""
        event.potato_command = name
        event.potato_language = self.localizer.locale
        token = ACTIVE_LOCALIZER.set(self.localizer if handler.module in self.system_modules else None)
        try:
            await handler.callback(event)
        except Exception as error:
            self._record_failure(handler.module, error)
            await event.answer([], cache_time=0, private=True)
        finally:
            ACTIVE_LOCALIZER.reset(token)

    async def handle_webhook(self, request: web.Request) -> web.StreamResponse:
        if request.match_info["account"] != self.settings.account:
            raise web.HTTPNotFound()
        identifier = request.match_info["module"]
        route = request.match_info["route"]
        handler = self.webhooks.get((identifier, route, request.method))
        if handler is None or identifier in self.policy.values("disabled_modules"):
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
        token = ACTIVE_LOCALIZER.set(self.localizer if identifier in self.system_modules else None)
        try:
            instance = self.modules.get(identifier)
            if getattr(instance, "_potato_heroku", False) and getattr(callback, "__potato_command__", None) and args:
                args = (getattr(args[0], "message", args[0]), *args[1:])
            await callback(*args)
            if getattr(instance, "_potato_heroku", False):
                await instance.flush()
        except Exception as error:
            self._record_failure(identifier, error)
        finally:
            ACTIVE_LOCALIZER.reset(token)

    def _record_failure(self, identifier: str, error: Exception) -> None:
        message = f"{identifier}: {type(error).__name__}: {error}"[:300]
        if self.services is not None:
            message = self.services.redact(message)
            self.services.record_error(identifier, error)
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
            if getattr(instance, "_potato_heroku", False):
                await instance.close_adapter()
