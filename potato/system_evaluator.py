from __future__ import annotations

import asyncio
import ast
import math
import operator
import shutil
import tempfile
from pathlib import Path

from potato.api import Module, command
from potato.presentation import safe, send_html
from potato.system_settings import argument


OPERATIONS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
FUNCTIONS = {
    "abs": abs,
    "round": round,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
}
CONSTANTS = {"pi": math.pi, "e": math.e}


def calculate(source: str) -> int | float:
    if not source or len(source) > 1024:
        raise ValueError("Выражение должно содержать от 1 до 1024 символов")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as error:
        raise ValueError("Некорректное выражение") from error
    if sum(1 for _ in ast.walk(tree)) > 64:
        raise ValueError("Выражение слишком сложное")

    def visit(node: ast.AST) -> int | float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            result = node.value
        elif isinstance(node, ast.Name) and node.id in CONSTANTS:
            result = CONSTANTS[node.id]
        elif isinstance(node, ast.UnaryOp) and type(node.op) in UNARY:
            result = UNARY[type(node.op)](visit(node.operand))
        elif isinstance(node, ast.BinOp) and type(node.op) in OPERATIONS:
            left = visit(node.left)
            right = visit(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 10**100):
                raise ValueError("Слишком большая степень")
            result = OPERATIONS[type(node.op)](left, right)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS and not node.keywords and 1 <= len(node.args) <= 2:
            arguments = [visit(argument) for argument in node.args]
            if node.func.id == "round" and len(arguments) == 2 and (type(arguments[1]) is not int or abs(arguments[1]) > 100):
                raise ValueError("Точность округления вне допустимых пределов")
            result = FUNCTIONS[node.func.id](*arguments)
        else:
            raise ValueError("Разрешены только математические выражения")
        if type(result) not in {int, float} or abs(result) > 10**100 or isinstance(result, float) and not math.isfinite(result):
            raise ValueError("Результат выходит за допустимые пределы")
        return result

    try:
        return visit(tree)
    except (ArithmeticError, OverflowError, TypeError) as error:
        raise ValueError(str(error)) from error


async def run_process(arguments: list[str], directory: Path, timeout: int) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd=directory,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output = bytearray()

    async def drain() -> None:
        while chunk := await process.stdout.read(4096):
            output.extend(chunk)
            if len(output) > 16_384:
                del output[:-16_384]

    reader = asyncio.create_task(drain())
    timed_out = False
    try:
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            await process.wait()
        await reader
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if not reader.done():
            reader.cancel()
    rendered = output.decode("utf-8", errors="replace")[-3000:]
    if timed_out:
        rendered += "\nПревышено время выполнения."
    return process.returncode, rendered or "Вывод пуст"


class PotatoEvaluator(Module):
    DISPLAY_NAME = "Evaluator"
    async def _evaluate(self, event, language: str) -> None:
        if event.chat_id != self.context.owner_id:
            await send_html(event, "🔐 <b>Исполнение кода доступно только в Избранном.</b>")
            return
        source = argument(event)
        if not source:
            reply = await event.get_reply_message()
            source = "" if reply is None else reply.raw_text or ""
        if not source or len(source.encode("utf-8")) > 32_768:
            await send_html(event, "⚠️ <b>Укажите код до 32 КиБ или ответьте на сообщение с кодом.</b>")
            return
        tools = {
            "c": (".c", "gcc", ["gcc"]),
            "cpp": (".cpp", "g++", ["g++"]),
            "go": (".go", "go", ["go"]),
            "rust": (".rs", "rustc", ["rustc"]),
            "node": (".js", None, ["node"]),
        }
        extension, compiler, executable = tools[language]
        if shutil.which(executable[0]) is None:
            await send_html(event, f"⚠️ <b>Среда {safe(language)} не установлена в контейнере.</b>")
            return
        with tempfile.TemporaryDirectory(prefix="potato-eval-", dir=self.context.manager.settings.data_dir) as temporary:
            directory = Path(temporary)
            source_path = directory / f"main{extension}"
            source_path.write_text(source, encoding="utf-8")
            if compiler is not None:
                output_path = directory / "program"
                compile_commands = {
                    "c": ["gcc", "-O0", "-o", str(output_path), str(source_path)],
                    "cpp": ["g++", "-O0", "-o", str(output_path), str(source_path)],
                    "go": ["go", "build", "-o", str(output_path), str(source_path)],
                    "rust": ["rustc", "-o", str(output_path), str(source_path)],
                }
                code, output = await run_process(compile_commands[language], directory, 45)
                if code != 0:
                    await send_html(event, f"❌ <b>{safe(language)} · ошибка сборки</b>\n<pre>{safe(output)}</pre>")
                    return
                executable = [str(output_path)]
            else:
                executable = ["node", str(source_path)]
            code, output = await run_process(executable, directory, 20)
        await send_html(event, f"🧪 <b>{safe(language)} · код {code}</b>\n<pre>{safe(output)}</pre>")

    @command("e", primary_only=True)
    async def python(self, event) -> None:
        source = argument(event)
        if not source:
            reply = await event.get_reply_message()
            source = "" if reply is None else reply.raw_text or ""
        try:
            result = calculate(source)
        except ValueError as error:
            await send_html(event, f"⚠️ <b>Ошибка вычисления:</b> {safe(error)}")
            return
        await send_html(event, f"🧮 <b>Результат:</b> <pre>{safe(result)}</pre>")

    @command("ec", primary_only=True)
    async def c(self, event) -> None:
        await self._evaluate(event, "c")

    @command("ecpp", primary_only=True)
    async def cpp(self, event) -> None:
        await self._evaluate(event, "cpp")

    @command("eg", primary_only=True)
    async def go(self, event) -> None:
        await self._evaluate(event, "go")

    @command("enode", primary_only=True)
    async def node(self, event) -> None:
        await self._evaluate(event, "node")

    @command("ers", primary_only=True)
    async def rust(self, event) -> None:
        await self._evaluate(event, "rust")
