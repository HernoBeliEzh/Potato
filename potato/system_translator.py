from __future__ import annotations

import re

from telethon import functions, types

from potato.api import Module, command
from potato.presentation import safe, send_html
from potato.system_settings import argument


LANGUAGE = re.compile(r"[a-z]{2}(?:-[a-z]{2})?\Z")


class PotatoTranslator(Module):
    DISPLAY_NAME = "Translator"
    @command("tr")
    async def translate(self, event) -> None:
        parts = argument(event).split(maxsplit=1)
        selected = self.context.localizer.locale
        default_language = selected if LANGUAGE.fullmatch(selected) else "ru"
        if parts and LANGUAGE.fullmatch(parts[0].casefold()):
            language = parts[0].casefold()
            text = parts[1] if len(parts) > 1 else ""
        else:
            language = default_language
            text = argument(event)
        if not text:
            reply = await event.get_reply_message()
            text = "" if reply is None else reply.raw_text or ""
        if not text or len(text) > 3500:
            await send_html(event, "🌐 <b>Использование:</b> <code>.tr язык текст</code> или ответьте на сообщение.")
            return
        try:
            result = await self.context.client(
                functions.messages.TranslateTextRequest(
                    to_lang=language,
                    text=[types.TextWithEntities(text=text, entities=[])],
                )
            )
        except Exception as error:
            await send_html(event, f"❌ <b>Перевод не выполнен</b>\n<blockquote>{safe(type(error).__name__)}</blockquote>")
            return
        translated = "\n".join(item.text for item in result.result)
        await send_html(event, f"🌐 <b>Перевод · {safe(language)}</b>\n<blockquote>{safe(translated)}</blockquote>")
