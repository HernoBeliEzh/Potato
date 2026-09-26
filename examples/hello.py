from aiohttp import web

from potato.api import Module, command, interval, on_message, webhook


API_VERSION = 1
REQUIRES = ()


class Hello(Module):
    @command("ping")
    async def ping(self, event):
        await event.reply("pong")

    @on_message(incoming=True, pattern=r"(?i)^hello potato$")
    async def greet(self, event):
        await event.reply("Hello")

    @interval(300)
    async def count_ticks(self):
        ticks = await self.context.storage.get("ticks", 0)
        await self.context.storage.set("ticks", ticks + 1)

    @command("ticks")
    async def ticks(self, event):
        ticks = await self.context.storage.get("ticks", 0)
        await event.reply(f"Срабатываний таймера: {ticks}")

    @webhook("notify")
    async def notify(self, request):
        payload = await request.json()
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text:
            return web.json_response({"error": "text is required"}, status=400)
        await self.context.client.send_message("me", text)
        return {"ok": True}
