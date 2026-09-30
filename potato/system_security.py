from __future__ import annotations

import re
import time

from potato.api import Module, command
from potato.presentation import safe, send_html
from potato.system_settings import argument, peer_id


GROUP_NAME = re.compile(r"[a-z][a-z0-9_]{1,31}\Z")
DURATION = re.compile(r"(\d+)([smhd])\Z")


def duration_seconds(value: str) -> int | None:
    match = DURATION.fullmatch(value.casefold())
    if match is None:
        return None
    seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
    return seconds if 1 <= seconds <= 30 * 86400 else None


class PotatoSecurity(Module):
    DISPLAY_NAME = "PotatoSecurity"
    @command("owneradd", primary_only=True)
    async def owneradd(self, event) -> None:
        parts = argument(event).split()
        if len(parts) != 2 or parts[1] != "CONFIRM" or event.chat_id != self.context.owner_id:
            await send_html(event, "🔐 <b>Добавление владельца:</b> <code>.owneradd ID CONFIRM</code> в Избранном.")
            return
        user_id = await peer_id(event, self.context.client, parts[0])
        if user_id is None or user_id == self.context.owner_id:
            await send_html(event, "⚠️ <b>Укажите ID другого пользователя.</b>")
            return
        owners = self.context.manager.policy.values("extra_owners")
        owners.add(user_id)
        await self.context.manager.policy.set("extra_owners", sorted(owners))
        await send_html(event, f"🔐 <b>Владелец добавлен:</b> <code>{user_id}</code>\n<i>Доверяйте только своему аккаунту: он сможет выполнять команды Potato.</i>")

    @command("ownerlist", primary_only=True)
    async def ownerlist(self, event) -> None:
        owners = sorted(self.context.manager.policy.values("extra_owners"))
        lines = [f"<code>{self.context.owner_id}</code> · основной"]
        lines.extend(f"<code>{owner}</code>" for owner in owners)
        await send_html(event, "🔐 <b>Владельцы</b>\n<blockquote>" + "\n".join(lines) + "</blockquote>")

    @command("ownerrm", primary_only=True)
    async def ownerrm(self, event) -> None:
        value = argument(event)
        if not value.isdigit() or event.chat_id != self.context.owner_id:
            await send_html(event, "🔐 <b>Удаление владельца:</b> <code>.ownerrm ID</code> в Избранном.")
            return
        user_id = int(value)
        owners = self.context.manager.policy.values("extra_owners")
        owners.discard(user_id)
        await self.context.manager.policy.set("extra_owners", sorted(owners))
        await send_html(event, f"✅ <b>Доступ отозван:</b> <code>{user_id}</code>")

    @command("newsgroup", primary_only=True)
    async def newsgroup(self, event) -> None:
        name = argument(event).casefold()
        groups = dict(self.context.manager.policy.state.get("groups", {}))
        if not GROUP_NAME.fullmatch(name) or name in groups:
            await send_html(event, "⚠️ <b>Укажите новое имя группы:</b> <code>.newsgroup имя</code>")
            return
        groups[name] = []
        await self.context.manager.policy.set("groups", groups)
        await send_html(event, f"👥 <b>Группа создана:</b> <code>{safe(name)}</code>")

    @command("sgroups")
    async def sgroups(self, event) -> None:
        groups = self.context.manager.policy.state.get("groups", {})
        if not groups:
            await send_html(event, "👥 <i>Групп доступа пока нет.</i>")
            return
        lines = [f"▪️ <b>{safe(name)}</b> · {len(members)} участников" for name, members in sorted(groups.items())]
        await send_html(event, "👥 <b>Группы доступа</b>\n<blockquote>" + "\n".join(lines) + "</blockquote>")

    @command("sgroup")
    async def sgroup(self, event) -> None:
        name = argument(event).casefold()
        groups = self.context.manager.policy.state.get("groups", {})
        if name not in groups:
            await send_html(event, "⚠️ <b>Группа не найдена.</b> <i>Список: .sgroups</i>")
            return
        members = ", ".join(str(item) for item in sorted(groups[name])) or "нет участников"
        await send_html(event, f"👥 <b>{safe(name)}</b>\n<blockquote>{safe(members)}</blockquote>")

    @command("delsgroup", primary_only=True)
    async def delsgroup(self, event) -> None:
        parts = argument(event).split()
        if len(parts) != 2 or parts[1] != "CONFIRM":
            await send_html(event, "⚠️ <b>Удаление группы:</b> <code>.delsgroup имя CONFIRM</code>")
            return
        name = parts[0].casefold()
        groups = dict(self.context.manager.policy.state.get("groups", {}))
        if name not in groups:
            await send_html(event, "⚠️ <b>Группа не найдена.</b>")
            return
        groups.pop(name)
        await self.context.manager.policy.set("groups", groups)
        await send_html(event, f"🗑 <b>Группа удалена:</b> <code>{safe(name)}</code>")

    @command("sgroupadd", primary_only=True)
    async def sgroupadd(self, event) -> None:
        await self._change_group(event, add=True)

    @command("sgroupdel", primary_only=True)
    async def sgroupdel(self, event) -> None:
        await self._change_group(event, add=False)

    async def _change_group(self, event, *, add: bool) -> None:
        parts = argument(event).split(maxsplit=1)
        if not parts:
            await send_html(event, "👥 <b>Укажите группу и ID пользователя или ответьте на сообщение.</b>")
            return
        name = parts[0].casefold()
        groups = dict(self.context.manager.policy.state.get("groups", {}))
        if name not in groups:
            await send_html(event, "⚠️ <b>Группа не найдена.</b>")
            return
        user_id = await peer_id(event, self.context.client, parts[1] if len(parts) > 1 else "")
        if user_id is None:
            await send_html(event, "⚠️ <b>Пользователь не указан.</b>")
            return
        members = set(groups[name])
        if add:
            members.add(user_id)
        else:
            members.discard(user_id)
        groups[name] = sorted(members)
        await self.context.manager.policy.set("groups", groups)
        action = "добавлен в" if add else "удалён из"
        await send_html(event, f"👥 <b>Пользователь <code>{user_id}</code> {action} группы {safe(name)}.</b>")

    @command("security", primary_only=True)
    async def security(self, event) -> None:
        parts = argument(event).casefold().split()
        manager = self.context.manager
        if not parts:
            rules = manager.policy.state.get("command_access", {})
            lines = [f"<code>{safe(name)}</code> → {safe(access)}" for name, access in sorted(rules.items())]
            body = "\n".join(lines) or "По умолчанию команды доступны владельцу."
            await send_html(event, f"🔐 <b>Права команд</b>\n<blockquote>{body}</blockquote>\n<i>Изменение: .security команда public|owner|group:имя|default</i>")
            return
        name = parts[0].lstrip(".")
        handler = manager.commands.get(name)
        if handler is None:
            await send_html(event, "⚠️ <b>Команда не найдена.</b>")
            return
        if len(parts) == 1:
            access = manager.policy.state.get("command_access", {}).get(name, "owner" if handler.spec.owner_only else "public")
            await send_html(event, f"🔐 <b>{safe(name)}</b> → <code>{safe(access)}</code>")
            return
        access = parts[1]
        if handler.spec.primary_only or access not in {"public", "owner", "default"} and not (access.startswith("group:") and access[6:] in manager.policy.state.get("groups", {})):
            await send_html(event, "🔐 <b>Недопустимое правило для этой команды.</b>")
            return
        rules = dict(manager.policy.state.get("command_access", {}))
        if access == "default":
            rules.pop(name, None)
        else:
            rules[name] = access
        await manager.policy.set("command_access", rules)
        await send_html(event, f"🔐 <b>Доступ к {safe(name)}:</b> <code>{safe(access)}</code>")

    @command("inlinesec", primary_only=True)
    async def inlinesec(self, event) -> None:
        parts = argument(event).casefold().split()
        manager = self.context.manager
        if not parts:
            rules = manager.policy.state.get("inline_access", {})
            lines = [f"<code>{safe(name)}</code> → {safe(access)}" for name, access in sorted(rules.items())] if isinstance(rules, dict) else []
            await send_html(event, "🔐 <b>Права inline-команд</b>\n<blockquote>" + ("\n".join(lines) or "По умолчанию доступ задаёт модуль.") + "</blockquote>\n<i>.inlinesec команда owner|public|group:имя|default</i>")
            return
        handler = manager.inline_commands.get(parts[0])
        if handler is None or len(parts) > 2:
            await send_html(event, "⚠️ <b>Укажите существующую inline-команду.</b>")
            return
        name = handler.spec.name.casefold()
        saved = manager.policy.state.get("inline_access", {})
        rules = dict(saved) if isinstance(saved, dict) else {}
        if len(parts) == 1:
            access = rules.get(name, "owner" if handler.spec.owner_only else "public")
            await send_html(event, f"🔐 <b>Inline {safe(name)}:</b> <code>{safe(access)}</code>")
            return
        access = parts[1]
        if handler.spec.primary_only or not self._valid_inline_access(access):
            await send_html(event, "🔐 <b>Недопустимое правило для этой inline-команды.</b>")
            return
        if access == "default":
            rules.pop(name, None)
        else:
            rules[name] = access
        await manager.policy.set("inline_access", rules)
        await send_html(event, f"🔐 <b>Inline {safe(name)}:</b> <code>{safe(access)}</code>")

    @command("querysec", primary_only=True)
    async def querysec(self, event) -> None:
        access = argument(event).casefold()
        if not access:
            saved = self.context.manager.policy.state.get("query_access", "public")
            await send_html(event, f"🔐 <b>Доступ к inline-запросам:</b> <code>{safe(saved)}</code>\n<i>.querysec owner|public|group:имя|default</i>")
            return
        if not self._valid_inline_access(access):
            await send_html(event, "🔐 <b>Укажите owner, public, group:имя или default.</b>")
            return
        await self.context.manager.policy.set("query_access", "public" if access == "default" else access)
        await send_html(event, f"🔐 <b>Доступ к inline-запросам:</b> <code>{safe(access)}</code>")

    def _valid_inline_access(self, access: str) -> bool:
        return access in {"owner", "public", "default"} or access.startswith("group:") and access[6:] in self.context.manager.policy.state.get("groups", {})

    @command("tsec", primary_only=True)
    async def tsec(self, event) -> None:
        parts = argument(event).casefold().split()
        if len(parts) != 4 or parts[0] not in {"user", "chat", "group"}:
            await send_html(event, "🔐 <b>Временный доступ:</b> <code>.tsec user|chat|group ID команда 1h</code>")
            return
        kind, value, name, duration = parts
        handler = self.context.manager.commands.get(name)
        seconds = duration_seconds(duration)
        if handler is None or handler.spec.primary_only or seconds is None:
            await send_html(event, "⚠️ <b>Проверьте команду и срок от 1s до 30d.</b>")
            return
        if kind == "group":
            if value not in self.context.manager.policy.state.get("groups", {}):
                await send_html(event, "⚠️ <b>Группа не найдена.</b>")
                return
            subject = value
        else:
            try:
                subject = int(value)
            except ValueError:
                await send_html(event, "⚠️ <b>Укажите числовой ID.</b>")
                return
        rules = list(self.context.manager.policy.state.get("temporary_rules", []))
        rules.append({"kind": kind, "subject": subject, "command": name, "until": time.time() + seconds})
        await self.context.manager.policy.set("temporary_rules", rules)
        await send_html(event, f"⏳ <b>Доступ выдан:</b> <code>{safe(kind)} {safe(value)} → {safe(name)}</code> на <code>{safe(duration)}</code>")

    @command("tsecrm", primary_only=True)
    async def tsecrm(self, event) -> None:
        value = argument(event)
        rules = list(self.context.manager.policy.state.get("temporary_rules", []))
        if not value.isdigit() or not 1 <= int(value) <= len(rules):
            lines = [f"{index}. <code>{safe(rule['kind'])} {safe(rule['subject'])} → {safe(rule['command'])}</code>" for index, rule in enumerate(rules, 1) if rule["until"] > time.time()]
            await send_html(event, "⏳ <b>Временные правила</b>\n<blockquote>" + ("\n".join(lines) or "Нет активных правил") + "</blockquote>\n<i>Удаление: .tsecrm номер</i>")
            return
        rules.pop(int(value) - 1)
        await self.context.manager.policy.set("temporary_rules", rules)
        await send_html(event, "✅ <b>Временное правило удалено.</b>")

    @command("tsecclr", primary_only=True)
    async def tsecclr(self, event) -> None:
        if argument(event) != "CONFIRM":
            await send_html(event, "⚠️ <b>Удалить все временные правила:</b> <code>.tsecclr CONFIRM</code>")
            return
        await self.context.manager.policy.set("temporary_rules", [])
        await send_html(event, "🧹 <b>Временные правила удалены.</b>")
