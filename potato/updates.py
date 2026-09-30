from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from contextlib import suppress
from pathlib import Path

from telethon import Button


REPOSITORY = "HernoBeliEzh/Potato"
SHA = re.compile(r"[0-9a-f]{40}\Z")


def write_json(path, value):
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class UpdateMonitor:
    def __init__(self, manager):
        self.manager = manager
        self.storage = manager.storage.for_module(manager.settings.account, "__potato_updates__")
        self.installed = os.getenv("POTATO_BUILD_SHA", "")
        self.directory = Path(os.environ["POTATO_UPDATE_DIR"]) if os.getenv("POTATO_UPDATE_DIR") else None
        self.tasks = []
        self.lock = asyncio.Lock()
        self.latest = ""

    async def start(self):
        self.tasks = [asyncio.create_task(self._monitor()), asyncio.create_task(self._results())]

    async def latest_commit(self):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Potato-Userbot"}
        async with self.manager.http.get(f"https://api.github.com/repos/{REPOSITORY}/commits/main", headers=headers) as response:
            if response.status != 200:
                raise RuntimeError(f"GitHub HTTP {response.status}")
            data = await response.json()
        sha = data.get("sha", "")
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise ValueError("GitHub вернул некорректный SHA")
        return sha

    async def check(self):
        self.latest = await self.latest_commit()
        if not SHA.fullmatch(self.installed) or self.latest == self.installed:
            return
        if await self.storage.get("notified") == self.latest:
            return
        inline = self.manager.inline
        if inline is None or not inline.ready:
            return
        text = inline.tr(f"🥔 Доступно обновление Potato: {self.latest[:8]}\nhttps://github.com/{REPOSITORY}/commit/{self.latest}", f"🥔 Potato update available: {self.latest[:8]}\nhttps://github.com/{REPOSITORY}/commit/{self.latest}")
        await inline.notify(text, [[Button.inline(inline.tr("Обновиться", "Update"), f"update:{self.latest}".encode())]])
        await self.storage.set("notified", self.latest)

    async def _monitor(self):
        while True:
            try:
                await self.check()
            except Exception as error:
                self.manager._record_failure("github_update", error)
            await asyncio.sleep(3600)

    async def request_update(self, event, sha):
        if event.sender_id != self.manager.owner_id or event.chat_id != self.manager.owner_id:
            await event.answer()
            return
        async with self.lock:
            pending = await self.storage.get("pending")
            if pending:
                status = "running"
                if self.directory is not None and (self.directory / "worker.json").exists():
                    worker = await asyncio.to_thread(lambda: json.loads((self.directory / "worker.json").read_text()))
                    status = worker.get("status", status)
                await event.answer(self.manager.inline.tr(f"Обновление уже запущено: {status}.", f"Update is already running: {status}."))
                return
            if self.directory is None or not (self.directory / "worker.json").is_file():
                await event.answer(self.manager.inline.tr("Сервис обновления VPS недоступен.", "VPS update service is unavailable."), alert=True)
                return
            worker = await asyncio.to_thread(lambda: json.loads((self.directory / "worker.json").read_text()))
            if time.time() - worker.get("heartbeat", 0) > 60:
                await event.answer(self.manager.inline.tr("Сервис обновления VPS не отвечает.", "VPS update service is not responding."), alert=True)
                return
            if not SHA.fullmatch(sha):
                await event.answer()
                return
            await event.answer(self.manager.inline.tr("Проверяю обновление…", "Checking update…"))
            try:
                latest = await self.latest_commit()
                if sha != latest:
                    await self.manager.inline.notify(self.manager.inline.tr("Появилась более новая версия. Используйте новую кнопку.", "A newer version is available. Use the new button."), [[Button.inline(self.manager.inline.tr("Обновиться", "Update"), f"update:{latest}".encode())]])
                    return
                if sha == self.installed:
                    await self.manager.inline.notify(self.manager.inline.tr("Эта версия уже установлена.", "This version is already installed."))
                    return
                identifier = uuid.uuid4().hex
                await self.storage.set("pending", identifier)
                try:
                    await asyncio.to_thread(write_json, self.directory / "request.json", {"id": identifier, "sha": sha})
                except Exception:
                    await self.storage.delete("pending")
                    raise
                await self.manager.inline.notify(self.manager.inline.tr("Обновление запущено. После сборки Potato перезапустится и сообщит результат.", "Update started. Potato will restart after the build and report the result."))
            except Exception as error:
                self.manager._record_failure("update_request", error)
                await self.manager.inline.notify(self.manager.inline.tr("Не удалось запустить обновление.", "Could not start the update."))

    async def _results(self):
        while True:
            try:
                pending = await self.storage.get("pending")
                result_file = None if self.directory is None else self.directory / "result.json"
                if result_file is not None and result_file.exists():
                    result = await asyncio.to_thread(lambda: json.loads(result_file.read_text()))
                    handled = await self.storage.get("handled_result")
                    if result.get("id") == pending or result.get("id") != handled and pending is None:
                        status = result.get("status")
                        if status in {"success", "rolled_back", "failed"} and result.get("id") != handled:
                            text = {"success": ("✅ Potato обновлён.", "✅ Potato updated."), "rolled_back": ("⚠️ Новая версия не запустилась. Восстановлена предыдущая версия.", "⚠️ New version failed to start. Previous version restored."), "failed": ("❌ Обновление не выполнено. Текущая версия сохранена.", "❌ Update failed. Current version retained.")}[status]
                            await self.manager.inline.notify(self.manager.inline.tr(*text))
                            await self.storage.set("handled_result", result["id"])
                            await self.storage.delete("pending")
            except Exception as error:
                self.manager._record_failure("update_result", error)
            await asyncio.sleep(5)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
