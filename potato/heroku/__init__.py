import ast
import sys
import types


def validate_source(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level and any(alias.name in {"main", "database", "security", "translations", "inline"} for alias in node.names):
            raise ValueError("Этот внутренний сервис Heroku пока не поддерживается")
        if isinstance(node, ast.ImportFrom) and node.module and ("inline" in node.module.split(".") or node.module in {"main", "database", "security", "translations"}):
            raise ValueError("Этот внутренний сервис или inline-интерфейс Heroku пока не поддерживается")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "loader" and node.attr in {"inline_handler", "callback_handler", "Library", "import_lib", "raw_handler", "need_update"}:
            raise ValueError(f"Heroku loader.{node.attr} пока не поддерживается")


def install_imports():
    from potato.heroku import loader, utils, validators
    import telethon
    import telethon.tl
    import telethon.tl.types
    import telethon.tl.functions
    package = sys.modules[__name__]
    sys.modules.setdefault("heroku", package)
    sys.modules.setdefault("heroku.loader", loader)
    sys.modules.setdefault("heroku.utils", utils)
    sys.modules.setdefault("heroku.validators", validators)
    modules = types.ModuleType("potato.heroku.modules")
    modules.__path__ = []
    sys.modules.setdefault(modules.__name__, modules)
    bridge = types.ModuleType("herokutl")
    bridge.__path__ = []
    bridge.TelegramClient = telethon.TelegramClient
    bridge.functions = telethon.functions
    bridge.types = telethon.types
    sys.modules.setdefault("herokutl", bridge)
    for name, target in (("tl", telethon.tl), ("tl.types", telethon.tl.types), ("tl.functions", telethon.tl.functions), ("types", telethon.types), ("functions", telethon.functions), ("errors", telethon.errors), ("events", telethon.events), ("utils", telethon.utils)):
        sys.modules.setdefault(f"herokutl.{name}", target)
