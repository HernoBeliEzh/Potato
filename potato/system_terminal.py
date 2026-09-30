from __future__ import annotations

import asyncio

from potato.api import Module, command
from potato.presentation import safe, send_html
from potato.system_settings import argument


class PotatoTerminal(Module):
    DISPLAY_NAME = "Terminal"
    def __init__(self, context):
        super().__init__(context)
        self.processes: dict[int, asyncio.subprocess.Process] = {}

    @staticmethod
    async def _capture(stream) -> bytes:
        output = bytearray()
        while chunk := await stream.read(4096):
            output.extend(chunk)
            if len(output) > 16_384:
                del output[:-16_384]
        return bytes(output)

    @command("terminal", aliases=("exec",), primary_only=True)
    async def terminal(self, event) -> None:
        if event.chat_id != self.context.owner_id:
            await send_html(event, "🔐 <b>Команды терминала доступны только в Избранном.</b>")
            return
        value = argument(event)
        if not value:
            await send_html(event, "💻 <b>Использование:</b> <code>.terminal команда</code>")
            return
        process = await asyncio.create_subprocess_shell(
            value,
            cwd=self.context.manager.settings.data_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        identifier = getattr(event, "id", id(event))
        self.processes[identifier] = process
        reader = asyncio.create_task(self._capture(process.stdout))
        timed_out = False
        try:
            try:
                await asyncio.wait_for(process.wait(), timeout=120)
            except asyncio.TimeoutError:
                timed_out = True
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            output = await reader
        finally:
            self.processes.pop(identifier, None)
            if not reader.done():
                reader.cancel()
        if timed_out:
            output += b"\nCommand timed out."
        rendered = output.decode("utf-8", errors="replace")[-3000:] or "Вывод пуст"
        await send_html(
            event,
            f"💻 <b>Терминал · код {process.returncode}</b>\n"
            f"<blockquote><code>{safe(value[:200])}</code></blockquote>\n"
            f"<pre>{safe(rendered)}</pre>",
        )

    @command("terminate", primary_only=True)
    async def terminate(self, event) -> None:
        if event.chat_id != self.context.owner_id:
            await send_html(event, "🔐 <b>Остановка команд доступна только в Избранном.</b>")
            return
        if not self.processes:
            await send_html(event, "💻 <i>Активных команд терминала нет.</i>")
            return
        value = argument(event)
        if value and (not value.isdigit() or int(value) not in self.processes):
            await send_html(event, "⚠️ <b>Процесс не найден.</b>")
            return
        identifier = int(value) if value else next(reversed(self.processes))
        self.processes[identifier].terminate()
        await send_html(event, f"🛑 <b>Процесс {identifier} останавливается.</b>")

    async def on_unload(self) -> None:
        for process in self.processes.values():
            if process.returncode is None:
                process.terminate()
        await asyncio.gather(*(process.wait() for process in self.processes.values()), return_exceptions=True)
