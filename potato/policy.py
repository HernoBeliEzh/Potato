from __future__ import annotations

import asyncio
import time

from potato.storage import ModuleStorage


class CorePolicy:
    def __init__(self, storage: ModuleStorage, owner_id: int):
        self.storage = storage
        self.owner_id = owner_id
        self.lock = asyncio.Lock()
        self.state: dict = {}

    async def load(self) -> None:
        saved = await self.storage.get("state", {})
        if not isinstance(saved, dict):
            raise ValueError("Некорректные системные настройки")
        self.state = saved

    async def set(self, key: str, value) -> None:
        async with self.lock:
            self.state[key] = value
            await self.storage.set("state", self.state)

    def values(self, key: str) -> set:
        return set(self.state.get(key, []))

    def is_owner(self, event) -> bool:
        return bool(event.out or event.sender_id == self.owner_id)

    def is_trusted(self, event) -> bool:
        return self.is_owner(event) or event.sender_id in self.values("extra_owners")

    def is_blocked(self, event) -> bool:
        if self.is_owner(event):
            return False
        return (
            event.sender_id in self.values("blocked_users")
            or event.chat_id in self.values("blocked_chats")
        )

    def allows_address(self, event, name: str, addressed: bool) -> bool:
        if self.is_owner(event) or not getattr(event, "is_group", False):
            return True
        if addressed or getattr(event, "mentioned", False):
            return True
        return bool(
            self.state.get("no_nickname", False)
            or name in self.values("nonick_commands")
            or event.sender_id in self.values("nonick_users")
            or event.chat_id in self.values("nonick_chats")
        )

    def watcher_enabled(self, identifier: str, event) -> bool:
        rule = self.state.get("watcher_rules", {}).get(identifier, {})
        if rule.get("disabled") or event.chat_id in rule.get("blocked_chats", []):
            return False
        scopes = set(rule.get("scopes", []))
        if "chats" in scopes and not getattr(event, "is_group", False):
            return False
        if "private" in scopes and not getattr(event, "is_private", False):
            return False
        if "outgoing" in scopes and not event.out:
            return False
        if "incoming" in scopes and event.out:
            return False
        return True

    def is_enabled(self, name: str, identifier: str, primary_name: str) -> bool:
        return (
            name not in self.values("disabled_commands")
            and primary_name not in self.values("disabled_commands")
            and identifier not in self.values("disabled_modules")
        )

    def authorized(self, event, name: str, spec) -> bool:
        if spec.primary_only:
            return self.is_owner(event)
        if self.is_trusted(event):
            return True
        access = self.state.get("command_access", {}).get(name)
        if access is None:
            access = "owner" if spec.owner_only else "public"
        if access == "public":
            return True
        if access.startswith("group:"):
            members = self.state.get("groups", {}).get(access[6:], [])
            if event.sender_id in members:
                return True
        for rule in self.state.get("temporary_rules", []):
            if rule["command"] != name or rule["until"] <= time.time():
                continue
            if rule["kind"] == "user" and rule["subject"] == event.sender_id:
                return True
            if rule["kind"] == "chat" and rule["subject"] == event.chat_id:
                return True
            if rule["kind"] == "group":
                members = self.state.get("groups", {}).get(rule["subject"], [])
                if event.sender_id in members:
                    return True
        return False

    def _inline_access(self, user_id: int, access: str) -> bool:
        if user_id == self.owner_id or user_id in self.values("extra_owners"):
            return True
        if access == "public":
            return True
        if access.startswith("group:"):
            return user_id in self.state.get("groups", {}).get(access[6:], [])
        return False

    def allows_inline_query(self, user_id: int) -> bool:
        if user_id != self.owner_id and user_id in self.values("blocked_users"):
            return False
        return self._inline_access(user_id, self.state.get("query_access", "public"))

    def authorized_inline(self, user_id: int, name: str, spec) -> bool:
        if not self.allows_inline_query(user_id):
            return False
        if spec.primary_only:
            return user_id == self.owner_id
        rules = self.state.get("inline_access", {})
        default = "owner" if spec.owner_only else "public"
        access = rules.get(name, default) if isinstance(rules, dict) else default
        return self._inline_access(user_id, access)

    async def toggle(self, key: str, value) -> bool:
        values = self.values(key)
        if value in values:
            values.remove(value)
            enabled = False
        else:
            values.add(value)
            enabled = True
        await self.set(key, sorted(values))
        return enabled
