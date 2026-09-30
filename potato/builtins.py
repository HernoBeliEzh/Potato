from __future__ import annotations

import asyncio
import io
import secrets
import time
from urllib.parse import urlparse

from aiohttp import ClientError
from telethon import Button, functions

from potato.api import Module, command, inline_command
from potato.presentation import CUSTOM_EMOJI, safe, send_html
from potato.repositories import download_module, list_modules, parse_repository, repository_label
from potato.releases import ModuleValidationError
from potato.system_settings import argument


def uptime_text(started_at: float) -> str:
    elapsed = int(time.monotonic() - started_at)
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class PotatoHelp(Module):
    DISPLAY_NAME = "Help"

    @inline_command("help")
    async def inline_help(self, event) -> None:
        manager = self.context.manager
        names = sorted(
            name for name, handler in manager.inline_commands.items()
            if manager.inline_available(event.sender_id, name, handler)
        )
        pages = ["🥔 <b>Potato Inline</b>"]
        for name in names:
            line = f"\n• <code>{safe(name)}</code>"
            if len(pages[-1]) + len(line) > 3500:
                pages.append("🥔 <b>Potato Inline</b>")
            pages[-1] += line
        await event.answer([
            event.builder.article(
                f"Potato Inline · {index + 1}", description="Доступные inline-команды", text=text,
                parse_mode="html", buttons=Button.switch_inline("Статус Potato", query="status", same_peer=True),
            )
            for index, text in enumerate(pages[:50])
        ], cache_time=0, private=True)

    @command("help")
    async def help(self, event) -> None:
        manager = self.context.manager
        hidden = set(await self.context.storage.get("hidden_modules", []))
        hidden_count = len(hidden & manager.modules.keys())
        visible_count = len(manager.modules) - hidden_count
        lines = []
        for identifier, module in sorted(
            manager.modules.items(), key=lambda item: type(item[1]).__name__.casefold()
        ):
            if identifier in hidden:
                continue
            names = sorted(
                name for name, handler in manager.commands.items() if handler.module == identifier
            )
            display_name = module.DISPLAY_NAME or type(module).__name__
            label = f"▪️ <b>{safe(display_name)}:</b>"
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
                f"🥔 <b>{visible_count} mods available, {hidden_count} hidden:</b>"
                if not pages else "<b>Potato · справка</b> <i>(продолжение)</i>"
            )
            if current and len(header) + len("\n".join((*current, line))) + 31 > 3800:
                pages.append(current)
                current = []
            current.append(line)
        pages.append(current)
        for index, page in enumerate(pages):
            header = (
                f"🥔 <b>{visible_count} mods available, {hidden_count} hidden:</b>"
                if index == 0 else "<b>Potato · справка</b> <i>(продолжение)</i>"
            )
            await send_html(event, f"{header}\n\n<blockquote>{'\n'.join(page)}</blockquote>", edit=index == 0 and event.out)

    @command("helphide")
    async def hide(self, event) -> None:
        target = argument(event).casefold()
        if not target:
            await send_html(event, "🧩 <b>Скрыть модуль из справки</b>\n<blockquote>Используйте <code>.helphide имя_модуля</code>.</blockquote>")
            return
        manager = self.context.manager
        matches = [
            identifier for identifier, module in manager.modules.items()
            if target in {identifier.casefold(), type(module).__name__.casefold(), (module.DISPLAY_NAME or type(module).__name__).casefold()}
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
        module = manager.modules[identifier]
        await send_html(event, f"🧩 <b>{safe(module.DISPLAY_NAME or type(module).__name__)}</b> {action}.")

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
    DISPLAY_NAME = "PotatoInfo"

    @command("inline", primary_only=True)
    async def inline_info(self, event) -> None:
        inline = self.context.inline
        if inline is None or not inline.ready:
            error = (inline.error or "Нет соединения с inline-ботом") if inline is not None else "Inline-бот не запущен"
            await send_html(event, f"⚠️ <b>Potato Inline недоступен</b>\n<blockquote>{safe(error)}</blockquote>\n<i>Повторная настройка — при .restart.</i>")
            return
        await send_html(event, f"🥔 <b>Potato Inline готов</b>\n<blockquote>@{safe(inline.username)}</blockquote>\n<i>Введите username бота в любом чате, затем help или status.</i>")

    @inline_command("status")
    async def inline_status(self, event) -> None:
        manager = self.context.manager
        text = (
            "🟢 <b>Potato работает</b>\n"
            f"Аккаунт: <code>{safe(self.context.account)}</code>\n"
            f"Модулей: {len(manager.modules)}\n"
            f"Время работы: <code>{uptime_text(manager.started_at)}</code>"
        )
        await event.answer([
            event.builder.article(
                "Статус Potato", text=text, parse_mode="html",
                buttons=Button.switch_inline("Обновить статус", query="status", same_peer=True),
            )
        ], cache_time=0, private=True)

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

    @command("webhooks", primary_only=True)
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
    DISPLAY_NAME = "Loader"
    @command("lm", aliases=("ml",), primary_only=True)
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

    @command("restart", primary_only=True)
    async def restart(self, event) -> None:
        await send_html(event, "🔄 <b>Potato перезапускается.</b>")
        self.context.request_restart()

    async def _fetch(self, value: str) -> tuple[str, bytes]:
        if value.startswith("https://"):
            return await download_module(self.context.http, value)
        if not value or not value.replace("_", "").isalnum():
            raise ValueError("Укажите имя модуля или прямую ссылку на .py")
        for repository in await self.context.storage.get("repositories", []):
            modules = await list_modules(self.context.http, repository)
            if value in modules:
                return await download_module(self.context.http, modules[value])
        raise ValueError("Модуль не найден в добавленных репозиториях")

    @command("addrepo", primary_only=True)
    async def addrepo(self, event) -> None:
        try:
            repository = parse_repository(argument(event))
            await list_modules(self.context.http, repository)
        except (ValueError, ClientError, asyncio.TimeoutError) as error:
            await send_html(event, f"❌ <b>Репозиторий не добавлен</b>\n<blockquote>{safe(error)}</blockquote>")
            return
        repositories = await self.context.storage.get("repositories", [])
        if repository not in repositories:
            repositories.append(repository)
            await self.context.storage.set("repositories", repositories)
        await send_html(event, f"📚 <b>Репозиторий добавлен:</b> <code>{safe(repository_label(repository))}</code>")

    @command("delrepo", primary_only=True)
    async def delrepo(self, event) -> None:
        repositories = await self.context.storage.get("repositories", [])
        value = argument(event)
        if not value:
            lines = [f"{index}. <code>{safe(repository_label(repository))}</code>" for index, repository in enumerate(repositories, 1)]
            await send_html(event, "📚 <b>Репозитории модулей</b>\n<blockquote>" + ("\n".join(lines) or "Список пуст") + "</blockquote>\n<i>Удаление: .delrepo номер</i>")
            return
        if not value.isdigit() or not 1 <= int(value) <= len(repositories):
            await send_html(event, "⚠️ <b>Укажите номер репозитория из .delrepo.</b>")
            return
        repository = repositories.pop(int(value) - 1)
        await self.context.storage.set("repositories", repositories)
        await send_html(event, f"🗑 <b>Репозиторий удалён:</b> <code>{safe(repository_label(repository))}</code>")

    @command("loadmod", primary_only=True)
    async def loadmod(self, event) -> None:
        try:
            filename, content = await self._fetch(argument(event))
            info = self.context.manager.releases.stage(filename, content)
        except (ValueError, ClientError, asyncio.TimeoutError) as error:
            await send_html(event, f"❌ <b>Не удалось подготовить модуль</b>\n<blockquote>{safe(error)}</blockquote>")
            return
        await send_html(event, f"📦 <b>{safe(info.identifier)} ожидает установки.</b>\n<i>Примените через .restart.</i>")

    @command("dlmod", primary_only=True)
    async def dlmod(self, event) -> None:
        try:
            filename, content = await self._fetch(argument(event))
        except (ValueError, ClientError, asyncio.TimeoutError) as error:
            await send_html(event, f"❌ <b>Не удалось скачать модуль</b>\n<blockquote>{safe(error)}</blockquote>")
            return
        document = io.BytesIO(content)
        document.name = filename
        await self.context.client.send_file(event.chat_id, document, caption=f"📦 {filename}")
        await send_html(event, f"📥 <b>Файл {safe(filename)} отправлен.</b>\n<i>Для установки ответьте на него .lm.</i>")

    @command("dlmall", primary_only=True)
    async def dlmall(self, event) -> None:
        repositories = await self.context.storage.get("repositories", [])
        if not repositories:
            await send_html(event, "📚 <b>Сначала добавьте репозиторий командой .addrepo.</b>")
            return
        try:
            sources = {}
            for repository in repositories:
                sources.update(await list_modules(self.context.http, repository))
            if not sources or len(sources) > 30:
                raise ValueError("Каталог должен содержать от 1 до 30 модулей")
            modules = [await download_module(self.context.http, source) for source in sources.values()]
            self.context.manager.releases.stage_many(modules)
        except (ValueError, ClientError, asyncio.TimeoutError) as error:
            await send_html(event, f"❌ <b>Не удалось загрузить каталог</b>\n<blockquote>{safe(error)}</blockquote>")
            return
        await send_html(event, f"📦 <b>Подготовлено модулей: {len(modules)}</b>\n<i>Примените через .restart.</i>")

    @command("unloadmod", primary_only=True)
    async def unloadmod(self, event) -> None:
        identifier = argument(event)
        try:
            self.context.manager.releases.stage_remove(identifier)
        except ModuleValidationError as error:
            await send_html(event, f"❌ <b>Модуль не удалён</b>\n<blockquote>{safe(error)}</blockquote>")
            return
        await send_html(event, f"🗑 <b>{safe(identifier)} будет удалён после .restart.</b>")

    @command("clearmodules", primary_only=True)
    async def clearmodules(self, event) -> None:
        if argument(event) != "CONFIRM" or event.chat_id != self.context.owner_id:
            await send_html(event, "⚠️ <b>Удалить все пользовательские модули:</b> <code>.clearmodules CONFIRM</code> в Избранном.")
            return
        count = self.context.manager.releases.stage_clear()
        await send_html(event, f"🧹 <b>К удалению подготовлено модулей: {count}</b>\n<i>Примените через .restart.</i>")


class PotatoTester(Module):
    DISPLAY_NAME = "Tester"
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

    @command("logs", primary_only=True)
    async def logs(self, event) -> None:
        if event.chat_id != self.context.owner_id:
            await send_html(event, "🔐 <b>Журнал ошибок доступен только в Избранном.</b>")
            return
        failures = self.context.manager.failures[-10:]
        body = safe(("\n".join(failures) or "Ошибок модулей нет")[-3000:])
        await send_html(event, f"📋 <b>Ошибки модулей</b>\n<blockquote>{body}</blockquote>")

    @command("clearlogs", primary_only=True)
    async def clearlogs(self, event) -> None:
        count = len(self.context.manager.failures)
        self.context.manager.failures.clear()
        await send_html(event, f"🧹 <b>Очищено записей журнала: {count}</b>")

    @command("suspend", primary_only=True)
    async def suspend(self, event) -> None:
        value = argument(event)
        if not value.isdigit() or int(value) > 300:
            await send_html(event, "⏸ <b>Использование:</b> <code>.suspend секунды</code> · от 0 до 300.")
            return
        seconds = int(value)
        self.context.manager.suspended_until = time.monotonic() + seconds
        status = "возобновлена" if seconds == 0 else f"приостановлена на {seconds} сек."
        await send_html(event, f"⏸ <b>Обработка сообщений {status}</b>")
