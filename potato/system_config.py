from __future__ import annotations

import json

from potato.api import Module, command
from potato.presentation import safe, send_html
from potato.system_settings import argument


class PotatoConfig(Module):
    DISPLAY_NAME = "PotatoConfig"
    @command("config", aliases=("cfg",), primary_only=True)
    async def config(self, event) -> None:
        if event.chat_id != self.context.owner_id:
            await send_html(event, "🔐 <b>Настройки модулей доступны только в Избранном.</b>")
            return
        parts = argument(event).split(maxsplit=2)
        manager = self.context.manager
        if not parts:
            names = [type(module).__name__ for module in manager.modules.values() if getattr(module, "CONFIG", {})]
            body = "\n".join(f"▪️ {safe(name)}" for name in sorted(names)) or "Нет модулей с настраиваемыми параметрами"
            await send_html(event, f"⚙️ <b>Настраиваемые модули</b>\n<blockquote>{body}</blockquote>")
            return
        target = parts[0].casefold()
        identifier = next((name for name, module in manager.modules.items() if target in {name.casefold(), type(module).__name__.casefold()}), None)
        if identifier is None:
            await send_html(event, "⚠️ <b>Модуль не найден.</b>")
            return
        module = manager.modules[identifier]
        schema = getattr(module, "CONFIG", {})
        if not isinstance(schema, dict):
            await send_html(event, "⚠️ <b>Схема настроек модуля некорректна.</b>")
            return
        if len(parts) == 1:
            lines = []
            for key, default in sorted(schema.items()):
                value = await module.context.storage.get(f"config:{key}", default)
                rendered = "••••••" if any(word in key.casefold() for word in ("token", "secret", "password")) else json.dumps(value, ensure_ascii=False)
                lines.append(f"<code>{safe(key)}</code> = <code>{safe(rendered)}</code>")
            body = "\n".join(lines) or "У модуля нет настраиваемых параметров"
            await send_html(event, f"⚙️ <b>{safe(type(module).__name__)}</b>\n<blockquote>{body}</blockquote>")
            return
        key = parts[1]
        if key not in schema:
            await send_html(event, "⚠️ <b>Параметр не найден.</b>")
            return
        if len(parts) == 2:
            value = await module.context.storage.get(f"config:{key}", schema[key])
            rendered = "••••••" if any(word in key.casefold() for word in ("token", "secret", "password")) else json.dumps(value, ensure_ascii=False)
            await send_html(event, f"⚙️ <b>{safe(key)}</b> = <code>{safe(rendered)}</code>")
            return
        try:
            value = json.loads(parts[2])
        except json.JSONDecodeError:
            if isinstance(schema[key], str):
                value = parts[2]
            else:
                await send_html(event, "⚠️ <b>Значение должно быть корректным JSON.</b>")
                return
        if not getattr(module, "_potato_heroku", False) and type(value) is not type(schema[key]):
            await send_html(event, f"⚠️ <b>Ожидается тип {type(schema[key]).__name__}.</b>")
            return
        if getattr(module, "_potato_heroku", False):
            try:
                module.config[key] = value
                await module.flush()
            except ValueError as error:
                await send_html(event, f"⚠️ <b>{safe(error)}</b>")
                return
        else:
            await module.context.storage.set(f"config:{key}", value)
        await send_html(event, f"✅ <b>Параметр {safe(key)} модуля {safe(type(module).__name__)} сохранён.</b>")
