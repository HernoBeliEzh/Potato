from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import suppress

from telethon import functions

from potato.backups import backup_document


logger = logging.getLogger(__name__)


class AccountServices:
    def __init__(self, manager):
        self.manager = manager
        self.storage = manager.storage.for_module(manager.settings.account, "__potato_services__")
        self.state = {}
        self.tasks = []
        self.errors = asyncio.Queue(maxsize=100)
        self.group_lock = asyncio.Lock()
        self.backup_lock = asyncio.Lock()
        self.schedule_changed = asyncio.Event()
        self.last_errors = {}
        self.group_warning_at = 0

    async def load(self):
        self.state = await self.storage.get("state", {})
        if not isinstance(self.state, dict):
            self.state = {}
        self.manager.releases.allow_heroku = bool(self.state.get("heroku", False))

    async def save(self):
        await self.storage.set("state", self.state)

    async def start(self):
        self.tasks = [asyncio.create_task(self._provision()), asyncio.create_task(self._error_loop()), asyncio.create_task(self._backup_loop())]

    async def _provision(self):
        try:
            await self.ensure_group()
        except Exception as error:
            await self.group_warning(error)

    async def ensure_group(self):
        async with self.group_lock:
            client = self.manager.client
            group = self.state.get("group")
            if group is None:
                result = await client(functions.channels.CreateChannelRequest(
                    title=f"Potato · {self.manager.owner_id}", about="Potato: бекапы и ошибки", megagroup=True, forum=True,
                ))
                channel = result.chats[0]
                group = int(f"-100{channel.id}")
                self.state["group"] = group
                await self.save()
            for key, title in (("backup_topic", "Бекап"), ("errors_topic", "Ошибки")):
                topic_id = self.state.get(key)
                if topic_id:
                    result = await client(functions.messages.GetForumTopicsByIDRequest(group, [topic_id]))
                    if any(getattr(topic, "title", None) and topic.id == topic_id for topic in result.topics):
                        continue
                result = await client(functions.messages.GetForumTopicsRequest(group, None, 0, 0, 100, q=title))
                topic = next((topic for topic in result.topics if getattr(topic, "title", None) == title), None)
                if topic is not None:
                    topic_id = topic.id
                else:
                    result = await client(functions.messages.CreateForumTopicRequest(group, title))
                    topic_id = next((update.message.id for update in result.updates if getattr(getattr(update, "message", None), "action", None) and getattr(update.message.action, "title", None) == title), None)
                    if topic_id is None:
                        raise RuntimeError("Telegram не вернул идентификатор темы")
                self.state[key] = topic_id
                await self.save()
            return group

    def redact(self, text):
        text = re.sub(r"\b\d{5,20}:[A-Za-z0-9_-]{30,50}\b", "<скрыто>", text)
        text = re.sub(r"(?i)(api_hash|password|token|authorization|session)\s*[:=]\s*\S+", r"\1=<скрыто>", text)
        secret = self.manager.settings.api_hash
        if secret:
            text = text.replace(secret, "<скрыто>")
        return text[:3000]

    def record_error(self, identifier, error):
        text = self.redact(f"{identifier}: {type(error).__name__}: {error}")
        now = time.monotonic()
        previous = self.last_errors.get(text)
        if previous is not None and now - previous < 60:
            return
        self.last_errors = {key: stamp for key, stamp in self.last_errors.items() if now - stamp < 60}
        self.last_errors[text] = now
        with suppress(asyncio.QueueFull):
            self.errors.put_nowait(text)

    async def group_warning(self, error):
        logger.warning("Service group unavailable: %s", type(error).__name__)
        if time.monotonic() - self.group_warning_at < 300:
            return
        self.group_warning_at = time.monotonic()
        inline = self.manager.inline
        if inline is not None and inline.ready:
            with suppress(Exception):
                await inline.notify("⚠️ Служебная группа недоступна. Проверьте права и лимиты Telegram. Команды Potato продолжают работать.")

    async def _error_loop(self):
        while True:
            text = await self.errors.get()
            try:
                await self.ensure_group()
                await self.manager.client.send_message(self.state["group"], text, reply_to=self.state["errors_topic"], parse_mode=None)
            except Exception as error:
                await self.group_warning(error)
            finally:
                self.errors.task_done()
            await asyncio.sleep(2)

    async def set_period(self, hours):
        if type(hours) is not int or hours < 0 or hours > 876000:
            raise ValueError("Укажите целое число часов от 1 до 876000 или отключите бекапы")
        self.state["backup_hours"] = hours
        self.state["next_backup_at"] = time.time() + hours * 3600 if hours else 0
        await self.save()
        self.schedule_changed.set()

    async def backup(self):
        async with self.backup_lock:
            await self.ensure_group()
            document = await backup_document(self.manager.releases)
            sent = await self.manager.client.send_file(self.state["group"], document, caption="📦 Модули Potato · восстановление: .restorebp в ответ на этот архив", reply_to=self.state["backup_topic"])
            group = str(self.state["group"])[4:]
            return f"https://t.me/c/{group}/{self.state['backup_topic']}/{sent.id}"

    async def _backup_loop(self):
        while True:
            self.schedule_changed.clear()
            hours = self.state.get("backup_hours", 0)
            if hours:
                deadline = self.state.get("next_backup_at", time.time() + hours * 3600)
                if time.time() >= deadline:
                    try:
                        await self.backup()
                        if self.state.get("backup_hours") == hours:
                            self.state["next_backup_at"] = time.time() + hours * 3600
                            await self.save()
                    except Exception as error:
                        await self.group_warning(error)
                        self.record_error("auto_backup", error)
                        deadline = time.time() + 60
                    else:
                        deadline = self.state.get("next_backup_at", 0)
                delay = max(1, deadline - time.time())
            else:
                delay = 3600
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.schedule_changed.wait(), timeout=delay)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
