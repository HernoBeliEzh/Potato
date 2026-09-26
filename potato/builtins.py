from __future__ import annotations

import time

from potato.api import Module, command
from potato.releases import ModuleValidationError


class SystemModule(Module):
    @command("help")
    async def help(self, event) -> None:
        names = ", ".join(f".{name}" for name in sorted(self.context.manager.commands))
        await event.reply(f"Команды Potato: {names}")

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


class ModuleAdmin(Module):
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
