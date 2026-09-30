from __future__ import annotations

import json
import re
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientSession

from potato.english import ENGLISH_EXAMPLES, ENGLISH_PHRASES
from potato.storage import ModuleStorage


LOCALE = re.compile(r"[a-z]{2}(?:-[a-z]{2})?\Z")
MESSAGE_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\.[a-z][a-z0-9_]{0,63}\Z")
MAX_PACK_SIZE = 262_144
ACTIVE_LOCALIZER: ContextVar[Any] = ContextVar("potato_localizer", default=None)


class Localizer:
    def __init__(self, storage: ModuleStorage):
        self.storage = storage
        self.locale = "ru"
        self.packs: dict[str, dict[str, dict[str, str]]] = {}
        self.modules: dict[str, dict[str, dict[str, str]]] = {}

    async def load(self) -> None:
        locale = await self.storage.get("language", "ru")
        packs = await self.storage.get("language_packs", {})
        if isinstance(packs, dict):
            self.packs = packs
        if isinstance(locale, str) and locale in self.available():
            self.locale = locale

    def available(self) -> set[str]:
        return {"ru", "en", *self.packs}

    async def set_locale(self, locale: str) -> None:
        locale = locale.casefold()
        if locale not in self.available():
            raise ValueError("Язык не установлен")
        await self.storage.set("language", locale)
        self.locale = locale

    def register(self, identifier: str, strings: dict[str, dict[str, str]]) -> None:
        self.modules[identifier] = strings

    def translate(self, identifier: str, key: str, values: dict[str, Any] | None = None) -> str:
        qualified = f"{identifier}.{key}"
        template = self.packs.get(self.locale, {}).get("messages", {}).get(qualified)
        module_strings = self.modules.get(identifier, {})
        if template is None:
            template = module_strings.get(self.locale, {}).get(key)
        if template is None:
            template = module_strings.get("ru", {}).get(key, key)
        try:
            return template.format_map(values or {})
        except (KeyError, ValueError):
            return module_strings.get("ru", {}).get(key, key)

    def render_markup(self, message: str) -> str:
        if self.locale == "ru":
            return message
        phrases = ENGLISH_PHRASES if self.locale == "en" else self.packs.get(self.locale, {}).get("phrases", {})
        if not phrases:
            return message
        fragments = re.split(r"(<[^>]+>)", message)
        protected = 0
        code = 0
        for index, fragment in enumerate(fragments):
            lowered = fragment.casefold()
            if lowered.startswith("<code"):
                protected += 1
                code += 1
            elif lowered.startswith("<pre"):
                protected += 1
            elif lowered.startswith("</code"):
                protected = max(0, protected - 1)
                code = max(0, code - 1)
            elif lowered.startswith("</pre"):
                protected = max(0, protected - 1)
            elif code and self.locale == "en" and not fragment.startswith("<"):
                fragments[index] = ENGLISH_EXAMPLES.get(fragment, fragment)
            elif not fragment.startswith("<") and not protected:
                for original, translated in sorted(phrases.items(), key=lambda item: len(item[0]), reverse=True):
                    fragment = fragment.replace(original, translated)
                fragments[index] = fragment
        return "".join(fragments)

    async def install(self, http: ClientSession, address: str) -> str:
        parsed = urlparse(address)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Нужна ссылка HTTPS на JSON-пакет")
        async with http.get(address, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError(f"Сервер пакета ответил HTTP {response.status}")
            chunks = []
            size = 0
            async for chunk in response.content.iter_chunked(8192):
                size += len(chunk)
                if size > MAX_PACK_SIZE:
                    raise ValueError("Языковой пакет превышает 256 КиБ")
                chunks.append(chunk)
        content = b"".join(chunks)
        try:
            data = json.loads(content.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise ValueError("Языковой пакет должен быть UTF-8 JSON") from error
        if not isinstance(data, dict):
            raise ValueError("Некорректный языковой пакет")
        locale = data.get("locale")
        messages = data.get("messages")
        phrases = data.get("phrases", {})
        if not isinstance(locale, str) or not LOCALE.fullmatch(locale) or not isinstance(messages, dict) or not isinstance(phrases, dict):
            raise ValueError("Пакет должен содержать locale, messages и необязательный phrases")
        if len(messages) + len(phrases) == 0 or len(messages) + len(phrases) > 500 or not all(
            isinstance(key, str)
            and MESSAGE_KEY.fullmatch(key)
            and isinstance(value, str)
            and len(value) <= 4000
            for key, value in messages.items()
        ) or not all(
            isinstance(key, str) and 1 <= len(key) <= 200 and isinstance(value, str) and len(value) <= 4000
            for key, value in phrases.items()
        ):
            raise ValueError("Некорректные строки языкового пакета")
        packs = dict(self.packs)
        packs[locale] = {"messages": messages, "phrases": phrases}
        await self.storage.set("language_packs", packs)
        self.packs = packs
        return locale
