from __future__ import annotations

from potato.api import Module, command
from potato.presentation import safe, send_html


def argument(event) -> str:
    return getattr(event, "potato_args", "").strip()


async def peer_id(event, client, value: str) -> int | None:
    if value:
        try:
            return int(value)
        except ValueError:
            entity = await client.get_entity(value)
            return entity.id
    reply = await event.get_reply_message()
    return None if reply is None else reply.sender_id


class Settings(Module):
    DISPLAY_NAME = "Settings"
    @command("aliases")
    async def aliases(self, event) -> None:
        aliases = self.context.manager.policy.state.get("aliases", {})
        if not aliases:
            await send_html(event, "🔗 <i>Пользовательских алиасов нет.</i>")
            return
        lines = [f"<code>{safe(alias)}</code> → <code>{safe(command)}</code>" for alias, command in sorted(aliases.items())]
        await send_html(event, "🔗 <b>Алиасы</b>\n<blockquote>" + "\n".join(lines) + "</blockquote>")

    @command("addalias")
    async def addalias(self, event) -> None:
        parts = argument(event).split(maxsplit=1)
        if len(parts) != 2:
            await send_html(event, "🔗 <b>Использование:</b> <code>.addalias имя команда</code>")
            return
        alias, target = (part.casefold() for part in parts)
        manager = self.context.manager
        if not alias.isidentifier() or alias in manager.commands or target not in manager.commands:
            await send_html(event, "⚠️ <b>Проверьте имя алиаса и существование команды.</b>")
            return
        aliases = dict(manager.policy.state.get("aliases", {}))
        aliases[alias] = target
        await manager.policy.set("aliases", aliases)
        await send_html(event, f"🔗 <b>Алиас сохранён:</b> <code>{safe(alias)}</code> → <code>{safe(target)}</code>")

    @command("delalias")
    async def delalias(self, event) -> None:
        alias = argument(event).casefold()
        aliases = dict(self.context.manager.policy.state.get("aliases", {}))
        if alias not in aliases:
            await send_html(event, "⚠️ <b>Алиас не найден.</b>")
            return
        aliases.pop(alias)
        await self.context.manager.policy.set("aliases", aliases)
        await send_html(event, f"🗑 <b>Алиас удалён:</b> <code>{safe(alias)}</code>")

    @command("setprefix", primary_only=True)
    async def setprefix(self, event) -> None:
        prefix = argument(event)
        if not prefix or len(prefix) > 3 or any(character.isspace() for character in prefix):
            await send_html(event, "⚠️ <b>Префикс должен содержать от 1 до 3 непробельных символов.</b>")
            return
        await self.context.manager.policy.set("prefix", prefix)
        await send_html(event, f"⌨️ <b>Дополнительный префикс:</b> <code>{safe(prefix)}</code>\n<i>Точка всегда остаётся доступна.</i>")

    @command("blacklist")
    async def blacklist(self, event) -> None:
        value = argument(event)
        chat_id = int(value) if value.lstrip("-").isdigit() else event.chat_id
        if chat_id == self.context.owner_id:
            await send_html(event, "⚠️ <b>Избранное нельзя заблокировать.</b>")
            return
        blocked = self.context.manager.policy.values("blocked_chats")
        blocked.add(chat_id)
        await self.context.manager.policy.set("blocked_chats", sorted(blocked))
        await send_html(event, f"🚫 <b>Чат добавлен в чёрный список:</b> <code>{chat_id}</code>")

    @command("unblacklist")
    async def unblacklist(self, event) -> None:
        value = argument(event)
        chat_id = int(value) if value.lstrip("-").isdigit() else event.chat_id
        blocked = self.context.manager.policy.values("blocked_chats")
        blocked.discard(chat_id)
        await self.context.manager.policy.set("blocked_chats", sorted(blocked))
        await send_html(event, f"✅ <b>Чат исключён из чёрного списка:</b> <code>{chat_id}</code>")

    @command("blacklistuser")
    async def blacklistuser(self, event) -> None:
        user_id = await peer_id(event, self.context.client, argument(event))
        if user_id is None or user_id == self.context.owner_id:
            await send_html(event, "⚠️ <b>Укажите другого пользователя ID, @имя или ответом на сообщение.</b>")
            return
        blocked = self.context.manager.policy.values("blocked_users")
        blocked.add(user_id)
        await self.context.manager.policy.set("blocked_users", sorted(blocked))
        await send_html(event, f"🚫 <b>Пользователь заблокирован:</b> <code>{user_id}</code>")

    @command("unblacklistuser")
    async def unblacklistuser(self, event) -> None:
        user_id = await peer_id(event, self.context.client, argument(event))
        if user_id is None:
            await send_html(event, "⚠️ <b>Укажите пользователя ID, @имя или ответом.</b>")
            return
        blocked = self.context.manager.policy.values("blocked_users")
        blocked.discard(user_id)
        await self.context.manager.policy.set("blocked_users", sorted(blocked))
        await send_html(event, f"✅ <b>Пользователь разблокирован:</b> <code>{user_id}</code>")

    @command("togglecmd", primary_only=True)
    async def togglecmd(self, event) -> None:
        name = argument(event).casefold().lstrip(".")
        manager = self.context.manager
        if name not in manager.commands:
            await send_html(event, "⚠️ <b>Команда не найдена.</b>")
            return
        if name in {"togglecmd", "togglemod", "help", "setprefix"}:
            await send_html(event, "🔐 <b>Эту системную команду нельзя отключить.</b>")
            return
        disabled = await manager.policy.toggle("disabled_commands", name)
        action = "отключена" if disabled else "включена"
        await send_html(event, f"⌨️ <b>Команда {safe(name)} {action}.</b>")

    @command("togglemod", primary_only=True)
    async def togglemod(self, event) -> None:
        target = argument(event).casefold()
        manager = self.context.manager
        identifier = next((key for key, module in manager.modules.items() if target in {key.casefold(), type(module).__name__.casefold()}), None)
        if identifier is None:
            await send_html(event, "⚠️ <b>Модуль не найден.</b>")
            return
        if identifier in manager.system_modules:
            await send_html(event, "🔐 <b>Системные модули нельзя отключить.</b>")
            return
        disabled = await manager.policy.toggle("disabled_modules", identifier)
        action = "отключён" if disabled else "включён"
        await send_html(event, f"🧩 <b>Модуль {safe(identifier)} {action}.</b>")

    @command("clearmodule", primary_only=True)
    async def clearmodule(self, event) -> None:
        parts = argument(event).split()
        if len(parts) != 2 or parts[1] != "CONFIRM":
            await send_html(event, "⚠️ <b>Очистка данных модуля:</b> <code>.clearmodule имя CONFIRM</code>")
            return
        identifier = parts[0]
        if identifier in self.context.manager.system_modules or identifier not in self.context.manager.modules:
            await send_html(event, "🔐 <b>Очищать можно только данные пользовательского модуля.</b>")
            return
        await self.context.manager.storage.for_module(self.context.account, identifier).clear()
        await send_html(event, f"🧹 <b>Данные модуля {safe(identifier)} очищены.</b>")

    @command("cleardb", primary_only=True)
    async def cleardb(self, event) -> None:
        if argument(event) != "CONFIRM" or event.chat_id != self.context.owner_id:
            await send_html(event, "⚠️ <b>Для очистки базы отправьте <code>.cleardb CONFIRM</code> в Избранном.</b>")
            return
        await self.context.manager.storage.clear_account(self.context.account)
        self.context.manager.policy.state = {}
        self.context.manager.localizer.locale = "ru"
        self.context.manager.localizer.packs = {}
        await send_html(event, "🧹 <b>Настройки и данные модулей очищены.</b>\n<i>Сессия Telegram и файлы модулей сохранены.</i>")

    @command("potato")
    async def potato(self, event) -> None:
        await send_html(event, "🥔 <b>Potato</b>\n<blockquote>Открытый Telegram-юзербот для автоматизации процессов.</blockquote>")

    @command("installation")
    async def installation(self, event) -> None:
        await send_html(event, "🐳 <b>Установка Potato</b>\n<blockquote>Docker Compose · Python 3.12 · SQLite</blockquote>\n<i>Инструкция: README.md в репозитории проекта.</i>")
