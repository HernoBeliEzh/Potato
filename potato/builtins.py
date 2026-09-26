from __future__ import annotations

import secrets
import time

from telethon import functions

from potato.api import Module, command
from potato.presentation import CUSTOM_EMOJI, safe, send_html
from potato.releases import ModuleValidationError


def uptime_text(started_at: float) -> str:
    elapsed = int(time.monotonic() - started_at)
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class PotatoHelp(Module):
    @command("help")
    async def help(self, event) -> None:
        manager = self.context.manager
        header = (
            f"{CUSTOM_EMOJI} <b>Potato · справка</b>\n"
            f"<i>{len(manager.modules)} модулей активно · {len(manager.commands)} команд</i>"
        )
        blocks = []
        for identifier, module in sorted(
            manager.modules.items(), key=lambda item: type(item[1]).__name__.casefold()
        ):
            names = sorted(
                name for name, handler in manager.commands.items() if handler.module == identifier
            )
            fragments = [f"<code>.{safe(name)}</code>" for name in names] or [
                "<i>без команд</i>"
            ]
            label = f"▪️ <b>{safe(type(module).__name__)}</b>"
            current = []
            for fragment in fragments:
                if current and len(" | ".join((*current, fragment))) > 3000:
                    blocks.append(f"<blockquote>{label}\n{' | '.join(current)}</blockquote>")
                    current = []
                current.append(fragment)
            blocks.append(f"<blockquote>{label}\n{' | '.join(current)}</blockquote>")

        page = header
        for block in blocks:
            if len(page) + len(block) + 2 > 3800:
                await send_html(event, page)
                page = "🧩 <b>Potato · справка</b> <i>(продолжение)</i>"
            page = f"{page}\n\n{block}"
        await send_html(event, page)


class PotatoInfo(Module):
    @command("status")
    async def status(self, event) -> None:
        manager = self.context.manager
        await send_html(
            event,
            f"🟢 <b>Potato работает</b>\n"
            f"<blockquote>👤 <b>Аккаунт:</b> <code>{safe(self.context.account)}</code>\n"
            f"🧩 <b>Модулей:</b> {len(manager.modules)}\n"
            f"⏱ <i>Время работы:</i> <code>{uptime_text(manager.started_at)}</code></blockquote>",
        )

    @command("modules")
    async def modules(self, event) -> None:
        identifiers = ", ".join(sorted(self.context.manager.modules))
        await send_html(
            event,
            f"🧩 <b>Загруженные модули</b>\n<blockquote>{safe(identifiers)}</blockquote>\n"
            "<i>Полный список команд — .help</i>",
        )

    @command("source", owner_only=False)
    async def source(self, event) -> None:
        address = self.context.source_url
        if address:
            await send_html(event, f"🔗 <b>Исходный код Potato</b>\n<code>{safe(address)}</code>")
        else:
            await send_html(event, "⚠️ <i>Ссылка на исходный код не настроена владельцем.</i>")

    @command("webhooks")
    async def webhooks(self, event) -> None:
        if event.chat_id != self.context.owner_id:
            await send_html(
                event,
                "🔐 <b>Секреты вебхуков доступны только в Избранном.</b>\n"
                "<i>Отправьте .webhooks в личный чат с собой.</i>",
            )
            return
        routes = []
        for (identifier, name, method), handler in sorted(self.context.manager.webhooks.items()):
            path = f"/hooks/{self.context.account}/{identifier}/{name}"
            token = handler.secret or "подпись провайдера"
            routes.append(
                f"<blockquote><b>{safe(method)}</b> <code>{safe(path)}</code>\n"
                f"🔑 <code>{safe(token)}</code></blockquote>"
            )
        if routes:
            await send_html(event, "🔐 <b>Вебхуки</b>\n" + "\n".join(routes))
        else:
            await send_html(event, "🔐 <i>Вебхуков пока нет.</i>")


class PotatoLoader(Module):
    @command("lm")
    async def load_module(self, event) -> None:
        reply = await event.get_reply_message()
        if reply is None or reply.document is None or reply.file is None:
            await send_html(
                event,
                "📦 <b>Загрузка модуля</b>\n"
                "<blockquote>Ответьте <code>.lm</code> на сообщение с <code>.py</code>-файлом.</blockquote>",
            )
            return
        if reply.file.size is None or reply.file.size > 1_048_576:
            await send_html(event, "⚠️ <b>Модуль слишком большой.</b> <i>Максимум — 1 МиБ.</i>")
            return
        filename = reply.file.name or ""
        try:
            data = await self.context.client.download_media(reply, file=bytes)
        except Exception:
            await send_html(event, "❌ <b>Не удалось скачать файл модуля.</b>")
            return
        if not isinstance(data, bytes):
            await send_html(event, "❌ <b>Не удалось скачать файл модуля.</b>")
            return
        try:
            info = self.context.manager.releases.stage(filename, data)
        except ModuleValidationError as error:
            await send_html(
                event,
                f"❌ <b>Модуль отклонён</b>\n<blockquote>{safe(error)}</blockquote>",
            )
            return
        dependencies = ", ".join(info.requirements) if info.requirements else "нет"
        await send_html(
            event,
            f"📦 <b>Модуль {safe(info.identifier)} сохранён</b>\n"
            f"<blockquote>Зависимости: <code>{safe(dependencies)}</code></blockquote>\n"
            "<i>Для применения выполните <code>.restart</code>.</i>",
        )

    @command("restart")
    async def restart(self, event) -> None:
        await send_html(event, "🔄 <b>Potato перезапускается.</b>")
        self.context.request_restart()


class PotatoTester(Module):
    @command("ping")
    async def ping(self, event) -> None:
        started = time.perf_counter()
        await self.context.client(functions.PingRequest(ping_id=secrets.randbits(63)))
        latency = (time.perf_counter() - started) * 1000
        await send_html(
            event,
            f"{CUSTOM_EMOJI} <b>Пинг Telegram:</b> <code>{latency:.0f} мс</code>\n"
            f"⏱ <i>Время работы:</i> <code>{uptime_text(self.context.manager.started_at)}</code>",
            edit=True,
        )
