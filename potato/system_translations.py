from __future__ import annotations

import asyncio

from aiohttp import ClientError

from potato.api import Module, command
from potato.presentation import safe, send_html
from potato.system_settings import argument


class Translations(Module):
    DISPLAY_NAME = "Translations"
    STRINGS = {
        "ru": {
            "current": "🌐 <b>Язык интерфейса:</b> <code>{locale}</code>\n<blockquote>Доступны: {available}</blockquote>",
            "saved": "🌐 <b>Язык интерфейса изменён:</b> <code>{locale}</code>",
            "unknown": "⚠️ <b>Язык не установлен.</b> Используйте .dllangpack для своего пакета.",
            "usage": "🌐 <b>Использование:</b> <code>.dllangpack https://адрес/пакет.json</code>",
            "installed": "🌐 <b>Языковой пакет установлен:</b> <code>{locale}</code>",
            "failed": "❌ <b>Не удалось загрузить языковой пакет:</b> {error}",
        },
        "en": {
            "current": "🌐 <b>Interface language:</b> <code>{locale}</code>\n<blockquote>Available: {available}</blockquote>",
            "saved": "🌐 <b>Interface language changed:</b> <code>{locale}</code>",
            "unknown": "⚠️ <b>Language is not installed.</b> Use .dllangpack to add a pack.",
            "usage": "🌐 <b>Usage:</b> <code>.dllangpack https://host/pack.json</code>",
            "installed": "🌐 <b>Language pack installed:</b> <code>{locale}</code>",
            "failed": "❌ <b>Could not load language pack:</b> {error}",
        },
    }

    @command("setlang", primary_only=True)
    async def setlang(self, event) -> None:
        localizer = self.context.localizer
        locale = argument(event).casefold()
        if not locale:
            available = ", ".join(sorted(localizer.available()))
            await send_html(event, self.context.t("current", locale=localizer.locale, available=available))
            return
        try:
            await localizer.set_locale(locale)
        except ValueError:
            await send_html(event, self.context.t("unknown"))
            return
        event.potato_language = locale
        await send_html(event, self.context.t("saved", locale=locale))

    @command("dllangpack", primary_only=True)
    async def dllangpack(self, event) -> None:
        address = argument(event)
        if not address:
            await send_html(event, self.context.t("usage"))
            return
        try:
            locale = await self.context.localizer.install(self.context.http, address)
            await self.context.localizer.set_locale(locale)
        except (ValueError, ClientError, asyncio.TimeoutError) as error:
            await send_html(event, self.context.t("failed", error=safe(error)))
            return
        event.potato_language = locale
        await send_html(event, self.context.t("installed", locale=locale))
