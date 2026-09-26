from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

from aiohttp import ClientSession
from telethon import TelegramClient

from potato.storage import ModuleStorage

if TYPE_CHECKING:
    from potato.manager import ModuleManager


@dataclass(frozen=True)
class CommandSpec:
    name: str
    owner_only: bool


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


class Module:
    def __init__(self, context: ModuleContext):
        self.context = context


def command(name: str, *, owner_only: bool = True):
    def decorate(callback):
        callback.__potato_command__ = CommandSpec(name, owner_only)
        return callback

    return decorate


def on_message(*, incoming: bool = True, outgoing: bool = False, pattern: str | None = None):
    def decorate(callback):
        callback.__potato_message__ = MessageSpec(incoming, outgoing, pattern)
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
