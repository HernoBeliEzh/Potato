from __future__ import annotations

import secrets
import time

from telethon import functions

from potato.api import Module, command
from potato.releases import ModuleValidationError


class PotatoHelp(Module):
    @command("help")
    async def help(self, event) -> None:
        manager = self.context.manager
        lines = [f"{len(manager.modules)} модулей активно:", ""]
        for identifier, module in sorted(
            manager.modules.items(), key=lambda item: type(item[1]).__name__.casefold()
        ):
            names = sorted(
                name for name, handler in manager.commands.items() if handler.module == identifier
            )
            commands = " | ".join(names) if names else "без команд"
            lines.append(f"▪️ {type(module).__name__}: ( {commands} )")
        message = "\n".join(lines)
        while message:
            chunk = message[:3900]
            if len(message) > 3900:
                boundary = chunk.rfind("\n")
                if boundary > 0:
                    chunk = chunk[:boundary]
            await event.reply(chunk)
            message = message[len(chunk):].lstrip("\n")


class PotatoInfo(Module):

    @command("status")
    async def status(self, event) -> None:
        manager = self.context.manager
        uptime = int(time.monotonic() - manager.started_at)
        await event.reply(
            f"Potato работает. Аккаунт: {self.context.account}. "
            f"Модулей: {len(manager.modules)}. Время работы: {uptime} с."
        )

    @command("modules")
    async def modules(self, event) -> None:
        identifiers = ", ".join(sorted(self.context.manager.modules))
        await event.reply(f"Загруженные модули: {identifiers}")

    @command("source", owner_only=False)
    async def source(self, event) -> None:
        address = self.context.source_url
        if address:
            await event.reply(f"Исходный код Potato: {address}")
        else:
            await event.reply("Ссылка на исходный код не настроена владельцем.")

    @command("webhooks")
    async def webhooks(self, event) -> None:
        if event.chat_id != self.context.owner_id:
            await event.reply("Запросите список вебхуков в Избранном, чтобы не раскрывать секреты в чате.")
            return
        routes = []
        for (identifier, name, method), handler in sorted(self.context.manager.webhooks.items()):
            path = f"/hooks/{self.context.account}/{identifier}/{name}"
            token = handler.secret or "подпись провайдера"
            routes.append(f"{method} {path} — {token}")
        await event.reply("Вебхуки:\n" + "\n".join(routes) if routes else "Вебхуков пока нет.")


class PotatoLoader(Module):
    @command("lm")
    async def load_module(self, event) -> None:
        reply = await event.get_reply_message()
        if reply is None or reply.document is None or reply.file is None:
            await event.reply("Ответьте командой .lm на сообщение с .py-файлом.")
            return
        if reply.file.size is None or reply.file.size > 1_048_576:
            await event.reply("Максимальный размер модуля — 1 МиБ.")
            return
        filename = reply.file.name or ""
        try:
            data = await self.context.client.download_media(reply, file=bytes)
        except Exception:
            await event.reply("Не удалось скачать файл модуля.")
            return
        if not isinstance(data, bytes):
            await event.reply("Не удалось скачать файл модуля.")
            return
        try:
            info = self.context.manager.releases.stage(filename, data)
        except ModuleValidationError as error:
            await event.reply(f"Модуль отклонён: {error}")
            return
        dependencies = ", ".join(info.requirements) if info.requirements else "нет"
        await event.reply(
            f"Модуль {info.identifier} сохранён. Зависимости: {dependencies}. "
            "Для применения выполните .restart."
        )

    @command("restart")
    async def restart(self, event) -> None:
        await event.reply("Potato перезапускается.")
        self.context.request_restart()


class PotatoTester(Module):
    @command("ping")
    async def ping(self, event) -> None:
        started = time.perf_counter()
        await self.context.client(functions.PingRequest(ping_id=secrets.randbits(63)))
        latency = (time.perf_counter() - started) * 1000
        uptime = int(time.monotonic() - self.context.manager.started_at)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        await event.reply(
            f"Пинг Telegram: {latency:.0f} мс\n"
            f"Время работы: {hours:02d}:{minutes:02d}:{seconds:02d}"
        )
