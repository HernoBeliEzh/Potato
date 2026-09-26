from __future__ import annotations

import secrets
import time
from urllib.parse import urlparse

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
        hidden = set(await self.context.storage.get("hidden_modules", []))
        lines = []
        for identifier, module in sorted(
            manager.modules.items(), key=lambda item: type(item[1]).__name__.casefold()
        ):
            if identifier in hidden:
                continue
            names = sorted(
                name for name, handler in manager.commands.items() if handler.module == identifier
            )
            label = f"▪️ <b>{safe(type(module).__name__)}:</b>"
            current = []
            for name in names or ["без команд"]:
                fragment = safe(name)
                if current and len(label) + len(" | ".join((*current, fragment))) > 3000:
                    lines.append(f"{label} ( {' | '.join(current)} )")
                    current = []
                current.append(fragment)
            lines.append(f"{label} ( {' | '.join(current)} )")

        pages = []
        current = []
        for line in lines:
            header = (
                f"<b>{len(manager.modules)} модулей активно:</b>"
                if not pages else "<b>Potato · справка</b> <i>(продолжение)</i>"
            )
            if current and len(header) + len("\n".join((*current, line))) + 31 > 3800:
                pages.append(current)
                current = []
            current.append(line)
        pages.append(current)
        for index, page in enumerate(pages):
            header = (
                f"<b>{len(manager.modules)} модулей активно:</b>"
                if index == 0 else "<b>Potato · справка</b> <i>(продолжение)</i>"
            )
            await send_html(event, f"{header}\n\n<blockquote>{'\n'.join(page)}</blockquote>", edit=index == 0 and event.out)

    @command("helphide")
    async def hide(self, event) -> None:
        parts = event.raw_text[1:].split(maxsplit=1)
        if len(parts) < 2:
            await send_html(event, "🧩 <b>Скрыть модуль из справки</b>\n<blockquote>Используйте <code>.helphide имя_модуля</code>.</blockquote>")
            return
        target = parts[1].strip().casefold()
        manager = self.context.manager
        matches = [
            identifier for identifier, module in manager.modules.items()
            if target in {identifier.casefold(), type(module).__name__.casefold()}
        ]
        if not matches:
            await send_html(event, "⚠️ <b>Модуль не найден.</b>")
            return
        identifier = matches[0]
        if identifier == "potato_help":
            await send_html(event, "⚠️ <b>Модуль справки нельзя скрыть.</b>")
            return
        hidden = set(await self.context.storage.get("hidden_modules", []))
        if identifier in hidden:
            hidden.remove(identifier)
            action = "снова отображается"
        else:
            hidden.add(identifier)
            action = "скрыт из справки"
        await self.context.storage.set("hidden_modules", sorted(hidden))
        await send_html(event, f"🧩 <b>{safe(type(manager.modules[identifier]).__name__)}</b> {action}.")

    @command("support")
    async def support(self, event) -> None:
        source = self.context.source_url
        parsed = urlparse(source)
        if parsed.hostname == "github.com" and len(parsed.path.strip("/").split("/")) >= 2:
            project = "/".join(parsed.path.strip("/").split("/")[:2])
            address = f"https://github.com/{project}/issues"
        else:
            address = source
        if address:
            await send_html(event, f"💬 <b>Поддержка Potato</b>\n<blockquote>{safe(address)}</blockquote>")
        else:
            await send_html(event, "⚠️ <i>Адрес поддержки не настроен владельцем.</i>")


class PotatoInfo(Module):
    @command("status", aliases=("info", "ubinfo"))
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
    @command("lm", aliases=("ml",))
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
        )
