from __future__ import annotations

import io
import os
import re
import struct
import zlib

import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from potato.api import Module, command
from potato.presentation import safe, send_html
from potato.system_settings import argument


ACCOUNT_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


def qr_png(address: str) -> io.BytesIO:
    code = qrcode.QRCode(border=4, box_size=1)
    code.add_data(address)
    code.make(fit=True)
    pixels = code.get_matrix()
    scale = 8
    width = len(pixels[0]) * scale
    height = len(pixels) * scale
    rows = []
    for row in pixels:
        rendered = b"".join((b"\x00" if pixel else b"\xff") * scale for pixel in row)
        rows.extend(b"\x00" + rendered for _ in range(scale))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    image = io.BytesIO(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )
    image.name = "potato-login.png"
    return image


class PotatoAccounts(Module):
    DISPLAY_NAME = "PotatoAccounts"
    @command("addacc", primary_only=True)
    async def addacc(self, event) -> None:
        if event.chat_id != self.context.owner_id:
            await send_html(event, "🔐 <b>Добавление аккаунта доступно только в Избранном.</b>")
            return
        name = argument(event).casefold()
        if not ACCOUNT_NAME.fullmatch(name) or name == self.context.account:
            await send_html(event, "👤 <b>Укажите новое имя аккаунта:</b> <code>.addacc имя</code>")
            return
        settings = self.context.manager.settings
        session_path = settings.data_dir / "sessions" / name
        session_path.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(str(session_path), settings.api_id, settings.api_hash)
        try:
            await client.connect()
            if await client.is_user_authorized():
                await send_html(event, f"👤 <b>Аккаунт {safe(name)} уже авторизован.</b>\n<i>Переключение: .switchacc {safe(name)}</i>")
                return
            login = await client.qr_login()
            for _ in range(3):
                await self.context.client.send_file(
                    "me",
                    qr_png(login.url),
                    caption=self.context.localizer.render_markup(f"👤 Вход в Potato: {name}. Сканируйте в Telegram → Настройки → Устройства. QR-код никому не пересылайте."),
                )
                try:
                    await login.wait()
                except TimeoutError:
                    await login.recreate()
                    continue
                except SessionPasswordNeededError:
                    await send_html(event, f"🔐 <b>Для аккаунта {safe(name)} нужен пароль 2FA.</b>\n<i>Завершите вход в CLI: docker compose run --rm potato python -m potato login --qr --account {safe(name)}</i>")
                    return
                account = await client.get_me()
                await send_html(event, f"✅ <b>Аккаунт {safe(name)} авторизован.</b>\n<blockquote>Telegram ID: <code>{account.id}</code></blockquote>\n<i>Переключение: .switchacc {safe(name)}</i>")
                return
            await send_html(event, "⌛ <b>Время QR-входа истекло.</b> Повторите .addacc.")
        finally:
            await client.disconnect()

    @command("switchacc", primary_only=True)
    async def switchacc(self, event) -> None:
        if event.chat_id != self.context.owner_id:
            await send_html(event, "🔐 <b>Переключение доступно только в Избранном.</b>")
            return
        name = argument(event).casefold()
        settings = self.context.manager.settings
        sessions = settings.data_dir / "sessions"
        if not name:
            names = sorted(path.name.removesuffix(".session") for path in sessions.glob("*.session"))
            body = "\n".join(f"▪️ <code>{safe(item)}</code>" for item in names) or "Сохранённых сессий нет"
            await send_html(event, f"👥 <b>Аккаунты Potato</b>\n<blockquote>{body}</blockquote>\n<i>Переключение: .switchacc имя</i>")
            return
        if not ACCOUNT_NAME.fullmatch(name) or not (sessions / f"{name}.session").is_file():
            await send_html(event, "⚠️ <b>Сессия не найдена.</b> Добавьте аккаунт через .addacc или CLI.")
            return
        if name == self.context.account:
            await send_html(event, "✅ <b>Этот аккаунт уже активен.</b>")
            return
        client = TelegramClient(str(sessions / name), settings.api_id, settings.api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await send_html(event, "⚠️ <b>Сессия не авторизована.</b> Повторите вход.")
                return
        finally:
            await client.disconnect()
        pointer = settings.data_dir / "active_account"
        temporary = settings.data_dir / f".active_account.{os.getpid()}"
        temporary.write_text(name, encoding="ascii")
        os.replace(temporary, pointer)
        await send_html(event, f"🔄 <b>Переключаю Potato на аккаунт {safe(name)}.</b>")
        self.context.request_restart()
