from __future__ import annotations

import asyncio
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

from potato.api import Module, command
from potato.presentation import send_html, safe
from potato.releases import MAX_MODULE_SIZE, inspect_module
from potato.system_settings import argument


MAX_BACKUP_SIZE = 32 * 1_048_576
MAX_BACKUP_MODULES = 256


def create_backup(directory: Path | None) -> bytes:
    files = [] if directory is None else sorted(directory.glob("*.py"))
    if len(files) > MAX_BACKUP_MODULES:
        raise ValueError("Бекап превышает 256 модулей")
    output = io.BytesIO()
    manifest = {"format": "potato-modules", "version": 1, "modules": []}
    size = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            content = path.read_bytes()
            info = inspect_module(path.name, content, allow_heroku=True)
            size += len(content)
            if size > MAX_BACKUP_SIZE:
                raise ValueError("Бекап превышает 32 МиБ")
            archive.writestr(path.name, content)
            manifest["modules"].append({"file": path.name, "type": info.kind, "sha256": hashlib.sha256(content).hexdigest()})
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    return output.getvalue()


def read_backup(content: bytes, allow_heroku: bool) -> list[tuple[str, bytes]]:
    if not content or len(content) > MAX_BACKUP_SIZE:
        raise ValueError("Архив должен быть не больше 32 МиБ")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if len(names) != len(set(names)) or len(names) > MAX_BACKUP_MODULES + 1:
                raise ValueError("Повторяющиеся файлы или слишком много модулей")
            if sum(item.file_size for item in entries) > MAX_BACKUP_SIZE:
                raise ValueError("Распакованный архив превышает 32 МиБ")
            if any(item.file_size > MAX_MODULE_SIZE for item in entries):
                raise ValueError("Файл превышает 1 МиБ")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("format") != "potato-modules" or manifest.get("version") != 1:
                raise ValueError("Неизвестный формат бекапа")
            declared = manifest.get("modules")
            if not isinstance(declared, list) or len(declared) > MAX_BACKUP_MODULES:
                raise ValueError("Некорректный список модулей")
            modules = []
            for item in declared:
                if not isinstance(item, dict) or not isinstance(item.get("file"), str):
                    raise ValueError("Некорректный модуль в манифесте")
                name = item["file"]
                if "/" in name or "\\" in name or not name.endswith(".py"):
                    raise ValueError("Недопустимый путь в архиве")
                source = archive.read(name)
                info = inspect_module(name, source, allow_heroku=allow_heroku)
                if item.get("type") != info.kind or item.get("sha256") != hashlib.sha256(source).hexdigest():
                    raise ValueError("Контрольная сумма или тип модуля не совпадают")
                modules.append((name, source))
            expected = ["manifest.json", *(name for name, _ in modules)]
            if len(expected) != len(set(expected)) or set(names) != set(expected):
                raise ValueError("Содержимое архива не соответствует манифесту")
            return modules
    except (zipfile.BadZipFile, KeyError, UnicodeError, json.JSONDecodeError, RuntimeError, NotImplementedError, EOFError) as error:
        raise ValueError("Повреждённый архив бекапа") from error


class PotatoBackups(Module):
    DISPLAY_NAME = "PotatoBackups"

    @command("bp", primary_only=True)
    async def backup(self, event):
        if argument(event):
            await send_html(event, "⚠️ <b>.bp сохраняет модули. Полный бекап .bp all появится позже.</b>")
            return
        services = self.context.manager.services
        try:
            message = await services.backup()
            await send_html(event, f"📦 <b>Бекап модулей сохранён.</b>\n{safe(message)}")
        except Exception as error:
            self.context.manager._record_failure("backup", error)
            await send_html(event, "❌ <b>Не удалось создать бекап. Проверьте служебную группу.</b>")

    @command("restorebp", primary_only=True)
    async def restore(self, event):
        reply = await event.get_reply_message()
        if reply is None or not reply.document:
            await send_html(event, "📦 <b>Ответьте командой .restorebp на архив .bp.</b>")
            return
        if reply.document.size > MAX_BACKUP_SIZE:
            await send_html(event, "❌ <b>Архив превышает 32 МиБ.</b>")
            return
        try:
            content = await reply.download_media(bytes)
            modules = await asyncio.to_thread(read_backup, content, self.context.manager.releases.allow_heroku)
            if not modules:
                raise ValueError("В бекапе нет пользовательских модулей")
            self.context.manager.releases.stage_many(modules)
        except ValueError as error:
            await send_html(event, f"❌ <b>Бекап не восстановлен:</b> {safe(error)}")
            return
        await send_html(event, f"📦 <b>Подготовлено модулей: {len(modules)}. Перезапуск для восстановления…</b>")
        self.context.request_restart()


async def backup_document(releases):
    content = await asyncio.to_thread(create_backup, releases.active_modules())
    document = io.BytesIO(content)
    document.name = f"potato-modules-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.zip"
    return document
