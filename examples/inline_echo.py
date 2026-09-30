from potato.api import Module, inline_command


API_VERSION = 1
REQUIRES = ()


class InlineEcho(Module):
    @inline_command("echo")
    async def echo(self, event):
        text = event.potato_args or "Привет от Potato Inline!"
        await event.answer([
            event.builder.article("Potato Echo", text=text, parse_mode=None)
        ], cache_time=0, private=True)
