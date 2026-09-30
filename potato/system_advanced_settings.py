from __future__ import annotations

from potato.api import Module, command
from potato.presentation import safe, send_html
from potato.system_settings import argument, peer_id


class PotatoSettings(Module):
    DISPLAY_NAME = "PotatoSettings"

    async def _toggle_list(self, event, key: str, value: int | str, label: str) -> None:
        policy = self.context.manager.policy
        values = policy.values(key)
        enabled = value not in values
        if enabled:
            values.add(value)
        else:
            values.remove(value)
        await policy.set(key, sorted(values))
        await send_html(event, f"⚙️ <b>NoNick для {safe(label)} {'включён' if enabled else 'выключен'}.</b>")

    async def _list_values(self, event, key: str, label: str) -> None:
        values = sorted(self.context.manager.policy.values(key))
        body = ", ".join(str(value) for value in values) or "список пуст"
        await send_html(event, f"⚙️ <b>NoNick: {safe(label)}</b>\n<blockquote>{safe(body)}</blockquote>")

    @command("nonickchat", primary_only=True)
    async def nonickchat(self, event) -> None:
        if not getattr(event, "is_group", False):
            await send_html(event, "⚠️ <b>Команда доступна в группе.</b>")
            return
        await self._toggle_list(event, "nonick_chats", event.chat_id, "этого чата")

    @command("nonickchats", primary_only=True)
    async def nonickchats(self, event) -> None:
        await self._list_values(event, "nonick_chats", "чаты")

    @command("nonickuser", primary_only=True)
    async def nonickuser(self, event) -> None:
        user_id = await peer_id(event, self.context.client, argument(event))
        if user_id is None:
            await send_html(event, "⚠️ <b>Укажите ID, @имя или ответьте на сообщение пользователя.</b>")
            return
        await self._toggle_list(event, "nonick_users", user_id, f"пользователя {user_id}")

    @command("nonickusers", primary_only=True)
    async def nonickusers(self, event) -> None:
        await self._list_values(event, "nonick_users", "пользователи")

    @command("nonickcmd", primary_only=True)
    async def nonickcmd(self, event) -> None:
        name = argument(event).casefold().lstrip(".")
        if name not in self.context.manager.commands:
            await send_html(event, "⚠️ <b>Команда не найдена.</b>")
            return
        await self._toggle_list(event, "nonick_commands", name, f"команды {name}")

    @command("nonickcmds", primary_only=True)
    async def nonickcmds(self, event) -> None:
        await self._list_values(event, "nonick_commands", "команды")

    @command("settings", primary_only=True)
    async def settings(self, event) -> None:
        value = argument(event).casefold().split()
        policy = self.context.manager.policy
        if not value:
            status = "включён" if policy.state.get("no_nickname", False) else "выключен"
            await send_html(event, f"⚙️ <b>Настройки Potato</b>\n<blockquote>NoNick для всех групп: {status}</blockquote>\n<i>.settings nonick on|off</i>")
            return
        if len(value) != 2 or value[0] != "nonick" or value[1] not in {"on", "off"}:
            await send_html(event, "⚠️ <b>Использование:</b> <code>.settings nonick on|off</code>")
            return
        enabled = value[1] == "on"
        await policy.set("no_nickname", enabled)
        await send_html(event, f"⚙️ <b>NoNick для всех групп {'включён' if enabled else 'выключен'}.</b>")

    def _watcher_modules(self) -> dict[str, str]:
        manager = self.context.manager
        identifiers = {handler.module for handler in manager.messages}
        result = {}
        for identifier in identifiers:
            module = manager.modules[identifier]
            result[identifier.casefold()] = identifier
            result[type(module).__name__.casefold()] = identifier
            result[(module.DISPLAY_NAME or type(module).__name__).casefold()] = identifier
        return result

    def _resolve_watcher(self, value: str) -> str | None:
        return self._watcher_modules().get(value.casefold())

    @command("watchers", primary_only=True)
    async def watchers(self, event) -> None:
        manager = self.context.manager
        rules = manager.policy.state.get("watcher_rules", {})
        names = sorted({handler.module for handler in manager.messages})
        lines = []
        for identifier in names:
            rule = rules.get(identifier, {})
            status = "выключен" if rule.get("disabled") else "включён"
            scopes = ", ".join(rule.get("scopes", [])) or "все сообщения"
            lines.append(f"▪️ <code>{safe(identifier)}</code> · {status} · {safe(scopes)} · чатов в блокировке: {len(rule.get('blocked_chats', []))}")
        await send_html(event, "♻️ <b>Обработчики сообщений</b>\n<blockquote>" + ("\n".join(lines) or "Нет обработчиков") + "</blockquote>")

    @command("watcher", primary_only=True)
    async def watcher(self, event) -> None:
        parts = argument(event).casefold().split()
        identifier = self._resolve_watcher(parts[0]) if parts else None
        if identifier is None:
            await send_html(event, "⚠️ <b>Укажите модуль из .watchers.</b>")
            return
        flags = set(parts[1:])
        mapping = {"-c": "chats", "-p": "private", "-o": "outgoing", "-i": "incoming"}
        if flags != {"-all"} and (flags - mapping.keys() or {"-c", "-p"} <= flags or {"-o", "-i"} <= flags):
            await send_html(event, "⚠️ <b>Фильтры:</b> <code>-c</code> группы, <code>-p</code> личные, <code>-o</code> исходящие, <code>-i</code> входящие, <code>-all</code> все.")
            return
        rules = dict(self.context.manager.policy.state.get("watcher_rules", {}))
        rule = dict(rules.get(identifier, {}))
        if flags == {"-all"}:
            rule.pop("scopes", None)
            rule["disabled"] = False
        elif flags:
            rule["scopes"] = sorted(mapping[flag] for flag in flags)
            rule["disabled"] = False
        else:
            rule["disabled"] = not rule.get("disabled", False)
        rules[identifier] = rule
        await self.context.manager.policy.set("watcher_rules", rules)
        await send_html(event, f"♻️ <b>Настройки обработчика {safe(identifier)} сохранены.</b>")

    @command("watcherbl", primary_only=True)
    async def watcherbl(self, event) -> None:
        identifier = self._resolve_watcher(argument(event))
        if identifier is None:
            await send_html(event, "⚠️ <b>Укажите модуль из .watchers.</b>")
            return
        rules = dict(self.context.manager.policy.state.get("watcher_rules", {}))
        rule = dict(rules.get(identifier, {}))
        chats = set(rule.get("blocked_chats", []))
        blocked = event.chat_id not in chats
        if blocked:
            chats.add(event.chat_id)
        else:
            chats.remove(event.chat_id)
        rule["blocked_chats"] = sorted(chats)
        rules[identifier] = rule
        await self.context.manager.policy.set("watcher_rules", rules)
        await send_html(event, f"♻️ <b>Обработчик {safe(identifier)} {'выключен' if blocked else 'включён'} в этом чате.</b>")
