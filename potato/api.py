from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

from aiohttp import ClientSession
from telethon import TelegramClient

from potato.storage import ModuleStorage

if TYPE_CHECKING:
    from potato.manager import ModuleManager


@dataclass(frozen=True)
class CommandSpec:
    name: str
    owner_only: bool
    aliases: tuple[str, ...] = ()
    primary_only: bool = False


@dataclass(frozen=True)
class MessageSpec:
    incoming: bool
    outgoing: bool
    pattern: str | None


@dataclass(frozen=True)
class IntervalSpec:
    seconds: float
    run_on_start: bool


@dataclass(frozen=True)
class WebhookSpec:
    name: str
    methods: tuple[str, ...]
    verifier: str | None


@dataclass
class ModuleContext:
    account: str
    owner_id: int
    client: TelegramClient
    http: ClientSession
    storage: ModuleStorage
    manager: ModuleManager
    source_url: str
    request_restart: Callable[[], None]
    module_id: str = ""
    localizer: Any = None
    inline: Any = None

    def t(self, key: str, **values: Any) -> str:
        if self.localizer is None:
            return key
        return self.localizer.translate(self.module_id, key, values)


class Module:
    CONFIG: dict[str, Any] = {}
    STRINGS: dict[str, dict[str, str]] = {}
    DISPLAY_NAME = ""

    def __init__(self, context: ModuleContext):
        self.context = context


def command(
    name: str,
    *,
    owner_only: bool = True,
    aliases: tuple[str, ...] = (),
    primary_only: bool = False,
):
    def decorate(callback):
        callback.__potato_command__ = CommandSpec(name, owner_only, aliases, primary_only)
        return callback

    return decorate


def on_message(*, incoming: bool = True, outgoing: bool = False, pattern: str | None = None):
    def decorate(callback):
        callback.__potato_message__ = MessageSpec(incoming, outgoing, pattern)
        return callback

    return decorate


def inline_command(
    name: str,
    *,
    owner_only: bool = True,
    aliases: tuple[str, ...] = (),
    primary_only: bool = False,
):
    def decorate(callback):
        callback.__potato_inline__ = CommandSpec(name, owner_only, aliases, primary_only)
        return callback

    return decorate


def interval(seconds: float, *, run_on_start: bool = False):
    def decorate(callback):
        callback.__potato_interval__ = IntervalSpec(seconds, run_on_start)
        return callback

    return decorate


def webhook(name: str, *, methods: tuple[str, ...] = ("POST",), verifier: str | None = None):
    def decorate(callback):
        callback.__potato_webhook__ = WebhookSpec(name, methods, verifier)
        return callback

    return decorate
