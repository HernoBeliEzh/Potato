from __future__ import annotations

import time

from potato.api import Module, command
from potato.presentation import send_html
from potato.system_settings import argument


class APILimiter(Module):
    DISPLAY_NAME = "APILimiter"
    @command("api_fw_protection", primary_only=True)
    async def api_fw_protection(self, event) -> None:
        value = argument(event).casefold()
        policy = self.context.manager.policy
        if value in {"on", "off"}:
            await policy.set("api_protection_enabled", value == "on")
        elif value:
            await send_html(event, "🛡 <b>Использование:</b> <code>.api_fw_protection on|off</code>")
            return
        state = "включена" if policy.state.get("api_protection_enabled", True) else "выключена"
        await send_html(event, f"🛡 <b>Защита от частых API-вызовов {state}.</b>\n<i>30 вызовов за 10 секунд; 5 зарезервированы для встроенных команд. Служебная синхронизация Telegram исключена.</i>")

    @command("suspend_api_protect", primary_only=True)
    async def suspend_api_protect(self, event) -> None:
        value = argument(event)
        if not value.isdigit() or int(value) > 600:
            await send_html(event, "⏸ <b>Использование:</b> <code>.suspend_api_protect секунды</code> · 0–600.")
            return
        seconds = int(value)
        await self.context.manager.policy.set("api_protection_suspended_until", time.time() + seconds)
        status = "снова активна" if seconds == 0 else f"приостановлена на {seconds} сек."
        await send_html(event, f"🛡 <b>Защита API {status}</b>")
