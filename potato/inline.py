from __future__ import annotations

import logging
import re
import secrets
import string
import asyncio
from contextlib import suppress

from telethon import Button, TelegramClient, events
from telethon.errors import AccessTokenExpiredError, AccessTokenInvalidError, FloodWaitError, YouBlockedUserError
from telethon.sessions import MemorySession


logger = logging.getLogger(__name__)
BOT_USERNAME = re.compile(r"Potato_[a-z0-9_]{1,25}", re.IGNORECASE)
BOT_TOKEN = re.compile(r"(?<![\w:])\d{5,20}:[a-zA-Z0-9_-]{30,50}(?![\w-])")


def potato_bots(message) -> list[str]:
    names = set()
    markup = getattr(message, "reply_markup", None)
    for row in getattr(markup, "rows", []) or []:
        for button in row.buttons:
            name = button.text.strip().lstrip("@")
            if BOT_USERNAME.fullmatch(name):
                names.add(name)
    return sorted(names, key=str.casefold)


class BotFather:
    def __init__(self, client, storage):
        self.client = client
        self.storage = storage

    async def prepare(self, preferred: str = "") -> dict:
        father = await self.client.get_entity("@BotFather")
        if not getattr(father, "verified", False) or father.id != 93372553:
            raise RuntimeError("Не удалось проверить официальный аккаунт BotFather")
        async with self.client.conversation(father, timeout=30, total_timeout=180) as conversation:
            try:
                await self._exchange(conversation, "/cancel")
                listing = await self._exchange(conversation, "/token")
                names = potato_bots(listing)
                if names:
                    username = next((name for name in names if name.casefold() == preferred.casefold()), names[0])
                    response = await self._exchange(conversation, f"@{username}")
                    token = self._token(response)
                    if token is None:
                        raise RuntimeError("BotFather не выдал токен выбранного Potato-бота")
                else:
                    text = (listing.raw_text or "").casefold()
                    has_keyboard = bool(getattr(getattr(listing, "reply_markup", None), "rows", None))
                    if not has_keyboard and not any(part in text for part in ("no bots", "don't have", "do not have", "нет ботов", "нет ни одного", "haven't created")):
                        raise RuntimeError("Не удалось получить список ботов из BotFather")
                    await self._exchange(conversation, "/cancel")
                    username, token = await self._create(conversation)
                credentials = {"username": username, "token": token}
                await self.storage.set("credentials", credentials)
                await self._exchange(conversation, "/setinline")
                await self._exchange(conversation, f"@{username}")
                response = await self._exchange(conversation, "Potato: help, status")
                if not any(part in (response.raw_text or "").casefold() for part in ("success", "enabled", "успеш", "включ", "done")):
                    raise RuntimeError("BotFather не подтвердил включение inline-режима")
                return credentials
            finally:
                with suppress(Exception):
                    await self._exchange(conversation, "/cancel")

    async def _exchange(self, conversation, text):
        sent = await conversation.send_message(text)
        response = await conversation.get_response(sent)
        content = (response.raw_text or "").casefold()
        if any(part in content for part in ("too many attempts", "try again in", "слишком много попыток", "повторите через")):
            raise RuntimeError("BotFather временно ограничил запросы. Повторите запуск позже")
        return response

    @staticmethod
    def _token(message) -> str | None:
        match = BOT_TOKEN.search(message.raw_text or "")
        return match[0] if match else None

    async def _create(self, conversation) -> tuple[str, str]:
        response = await self._exchange(conversation, "/newbot")
        content = (response.raw_text or "").casefold()
        if not any(part in content for part in ("choose a name", "call it", "назов", "имя")):
            raise RuntimeError("BotFather не разрешил создание бота. Проверьте лимит ботов аккаунта")
        response = await self._exchange(conversation, "Potato Inline")
        if not any(part in (response.raw_text or "").casefold() for part in ("username", "имя пользователя", "юзернейм")):
            raise RuntimeError("BotFather не запросил username нового бота")
        for _ in range(5):
            suffix = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
            username = f"Potato_{suffix}Bot"
            response = await self._exchange(conversation, username)
            token = self._token(response)
            if token is not None:
                return username, token
            if not any(part in (response.raw_text or "").casefold() for part in ("taken", "already", "занят")):
                raise RuntimeError("BotFather отклонил создание Potato-бота")
        raise RuntimeError("Не удалось подобрать свободный username Potato-бота")


class InlineBot:
    def __init__(self, manager):
        self.manager = manager
        self.storage = manager.storage.for_module(manager.settings.account, "__potato_inline__")
        self.client = None
        self.username = ""
        self.error = ""
        self.setup_lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self.client is not None and self.client.is_connected()

    async def start(self) -> bool:
        credentials = await self.storage.get("credentials", {})
        if not isinstance(credentials, dict):
            credentials = {}
        preferred = credentials.get("username", "")
        if not isinstance(preferred, str):
            preferred = ""
        token = credentials.get("token", "")
        try:
            if isinstance(token, str) and BOT_TOKEN.fullmatch(token) and BOT_USERNAME.fullmatch(preferred):
                try:
                    if await self._connect(credentials):
                        return True
                except (AccessTokenExpiredError, AccessTokenInvalidError):
                    await self.close()
            await self.close()
            credentials = await BotFather(self.manager.client, self.storage).prepare(preferred)
            if not await self._connect(credentials):
                raise RuntimeError("Telegram не подтвердил inline-режим Potato-бота")
            return True
        except Exception as error:
            await self.close()
            if isinstance(error, RuntimeError):
                self.error = BOT_TOKEN.sub("<скрыто>", str(error))
            elif isinstance(error, FloodWaitError):
                self.error = f"Telegram ограничил запросы на {error.seconds} секунд"
            elif isinstance(error, YouBlockedUserError):
                self.error = "Разблокируйте @BotFather и перезапустите Potato"
            else:
                self.error = f"Не удалось настроить inline-бота ({type(error).__name__})"
            logger.warning("Inline bot setup failed: %s", type(error).__name__)
            return False

    async def _connect(self, credentials: dict) -> bool:
        self.client = TelegramClient(
            MemorySession(), self.manager.settings.api_id, self.manager.settings.api_hash,
            flood_sleep_threshold=0,
        )
        await self.client.connect()
        bot = await self.client.sign_in(bot_token=credentials["token"])
        username = getattr(bot, "username", "") or ""
        if not getattr(bot, "bot", False) or not BOT_USERNAME.fullmatch(username) or username.casefold() != credentials["username"].casefold():
            raise RuntimeError("Токен не принадлежит выбранному Potato-боту")
        if not getattr(bot, "bot_inline_placeholder", None):
            return False
        self.username = username
        self.error = ""
        self.client.add_event_handler(self.manager.dispatch_inline, events.InlineQuery)
        self.client.add_event_handler(self._message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self._callback, events.CallbackQuery)
        return True

    async def _message(self, event) -> None:
        if not event.is_private or event.sender_id != self.manager.owner_id:
            return
        async with self.setup_lock:
            services = self.manager.services
            state = services.state
            text = (event.raw_text or "").strip()
            if state.get("setup_stage") == "custom":
                if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 876000:
                    await event.reply(self.tr("Введите целое число часов от 1 до 876000.", "Enter a whole number of hours from 1 to 876000."))
                    return
                await services.set_period(int(text))
                state["setup_stage"] = "done" if state.pop("editing", False) else "heroku"
                await services.save()
                await self.show_setup()
                return
            if text.split("@")[0] in {"/start", "/help", "/settings"}:
                await self.show_setup()

    def tr(self, russian, english):
        return english if self.manager.localizer.locale == "en" else russian

    async def notify(self, text, buttons=None):
        return await self.client.send_message(self.manager.owner_id, text, buttons=buttons, parse_mode=None)

    async def show_setup(self):
        state = self.manager.services.state
        stage = state.get("setup_stage", "language")
        if stage == "language":
            text = "🥔 Potato\nВыберите язык / Choose your language"
            buttons = [[Button.inline("Русский", b"setup:lang:ru"), Button.inline("English", b"setup:lang:en")]]
        elif stage in {"backup", "custom"}:
            text = self.tr("Раз во сколько часов сохранять модули?", "How often should modules be backed up (hours)?")
            if stage == "custom":
                text = self.tr("Введите целое число часов от 1 до 876000.", "Enter a whole number of hours from 1 to 876000.")
            periods = [1, 3, 6, 12, 24, 48, 96]
            buttons = [[Button.inline(str(hours), f"setup:period:{hours}".encode()) for hours in periods[index:index + 4]] for index in (0, 4)]
            buttons += [[Button.inline(self.tr("Свой интервал", "Custom interval"), b"setup:custom"), Button.inline(self.tr("Отключить", "Disable"), b"setup:period:0")]]
        elif stage == "heroku":
            text = self.tr("Включить базовую поддержку модулей Heroku?\nКоманды, watcher, настройки и циклы. Inline-интерфейсы пока не поддерживаются.", "Enable basic Heroku module support?\nCommands, watchers, settings and loops. Inline interfaces are not supported yet.")
            buttons = [[Button.inline(self.tr("Включить", "Enable"), b"setup:heroku:1"), Button.inline(self.tr("Выключить", "Disable"), b"setup:heroku:0")]]
        else:
            hours = state.get("backup_hours", 0)
            period = f"{hours} h" if hours else self.tr("отключён", "disabled")
            compatibility = self.tr("включена", "enabled") if state.get("heroku", False) else self.tr("выключена", "disabled")
            text = self.tr(f"🥔 Potato готов\nАвтобекап: {period}\nHeroku: {compatibility}\n.bp — бекап модулей\n.restorebp — восстановление в ответ на архив", f"🥔 Potato is ready\nAuto backup: {period}\nHeroku: {compatibility}\n.bp — module backup\n.restorebp — restore by replying to an archive")
            buttons = [[Button.inline(self.tr("Язык", "Language"), b"menu:language"), Button.inline(self.tr("Автобекап", "Auto backup"), b"menu:backup"), Button.inline("Heroku", b"menu:heroku")], [Button.switch_inline(self.tr("Открыть Inline", "Open inline"), query="", same_peer=True)]]
        if state.get("editing") or stage == "custom":
            buttons.append([Button.inline(self.tr("Назад", "Back"), b"menu:back")])
        message = await self.notify(text, buttons)
        state["bot_message_id"] = message.id
        await self.manager.services.save()

    async def _callback(self, event):
        if event.sender_id != self.manager.owner_id or event.chat_id != self.manager.owner_id:
            await event.answer()
            return
        data = event.data.decode("utf-8", errors="replace")
        if data.startswith("update:"):
            if self.manager.updates is not None:
                await self.manager.updates.request_update(event, data.partition(":")[2])
            return
        async with self.setup_lock:
            services = self.manager.services
            state = services.state
            if event.message_id != state.get("bot_message_id"):
                await event.answer(self.tr("Откройте актуальное меню через /start.", "Open the current menu with /start."))
                return
            stage = state.get("setup_stage", "language")
            if data.startswith("menu:"):
                target = data.partition(":")[2]
                if target == "back":
                    state["setup_stage"] = "done" if state.pop("editing", False) else "backup"
                elif stage == "done" and target in {"language", "backup", "heroku"}:
                    state["setup_stage"] = target
                    state["editing"] = True
                else:
                    await event.answer()
                    return
            elif data.startswith("setup:lang:") and stage == "language":
                locale = data.rsplit(":", 1)[1]
                if locale not in {"ru", "en"}:
                    await event.answer()
                    return
                await self.manager.localizer.set_locale(locale)
                state["setup_stage"] = "done" if state.pop("editing", False) else "backup"
            elif data == "setup:custom" and stage in {"backup", "custom"}:
                state["setup_stage"] = "custom"
            elif data.startswith("setup:period:") and stage in {"backup", "custom"}:
                hours = data.rsplit(":", 1)[1]
                if hours not in {"0", "1", "3", "6", "12", "24", "48", "96"}:
                    await event.answer()
                    return
                await services.set_period(int(hours))
                state["setup_stage"] = "done" if state.pop("editing", False) else "heroku"
            elif data in {"setup:heroku:0", "setup:heroku:1"} and stage == "heroku":
                enabled = data.endswith(":1")
                changed = bool(state.get("heroku", False)) != enabled
                state["heroku"] = enabled
                self.manager.releases.allow_heroku = enabled
                state["setup_stage"] = "done"
                state.pop("editing", None)
                await services.save()
                if changed:
                    await self.notify(self.tr("Настройка Heroku сохранена. Уже установленные модули переключатся после .restart.", "Heroku setting saved. Installed modules will switch after .restart."))
            else:
                await event.answer()
                return
            await services.save()
            await event.answer()
            with suppress(Exception):
                await event.edit(buttons=None)
            await self.show_setup()

    async def close(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            await client.disconnect()
