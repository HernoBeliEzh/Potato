from __future__ import annotations

import asyncio
import inspect

from potato.api import Module as PotatoModule, command as potato_command, on_message
from potato.heroku import validators


class StopLoop(Exception):
    __slots__ = ()


class ConfigValue:
    def __init__(self, option, default=None, doc=None, *, validator=None, on_change=None):
        self.option = option
        self.default = default
        self.validator = validator
        self.on_change = on_change


class ModuleConfig(dict):
    def __init__(self, *values):
        self.definitions = {value.option: value for value in values}
        self.changed = None
        super().__init__((value.option, value.default) for value in values)

    def __setitem__(self, key, value):
        definition = self.definitions[key]
        value = definition.validator.validate(value) if definition.validator else value
        super().__setitem__(key, value)
        if self.changed:
            self.changed(key, value)
        if definition.on_change:
            result = definition.on_change()
            if inspect.isawaitable(result):
                asyncio.create_task(result)


class Strings:
    def __init__(self, module, values):
        self.module = module
        self.values = values

    def __getitem__(self, key):
        locale = self.module.context.localizer.locale
        translated = getattr(type(self.module), f"strings_{locale}", {})
        return translated.get(key, self.values.get(key, key))

    def __call__(self, key, message=None):
        return self[key]


class Loop:
    def __init__(self, callback, interval, autostart, wait_before, stop_clause):
        if interval <= 0:
            raise ValueError("Интервал цикла должен быть положительным")
        self.callback = callback
        self.interval = interval
        self.autostart = autostart
        self.wait_before = wait_before
        self.stop_clause = stop_clause
        self.name = callback.__name__

    def __get__(self, instance, owner):
        if instance is None:
            return self
        loops = instance.__dict__.setdefault("_heroku_loops", {})
        if self.name not in loops:
            loops[self.name] = BoundLoop(instance, self)
        return loops[self.name]


class BoundLoop:
    def __init__(self, module, specification):
        self.module = module
        self.specification = specification
        self.task = None

    @property
    def status(self):
        return self.task is not None and not self.task.done()

    async def __call__(self, *args, **kwargs):
        return await self.specification.callback(self.module, *args, **kwargs)

    def start(self, *args, **kwargs):
        if not self.status:
            if self.specification.stop_clause:
                self.module.set(self.specification.stop_clause, True)
            self.task = asyncio.create_task(self._run(args, kwargs))
        return self.task

    async def _run(self, args, kwargs):
        specification = self.specification
        try:
            while not specification.stop_clause or self.module.get(specification.stop_clause, False):
                if specification.wait_before:
                    await asyncio.sleep(specification.interval)
                try:
                    await self(*args, **kwargs)
                except StopLoop:
                    break
                except Exception as error:
                    self.module.context.manager._record_failure(self.module.context.module_id, error)
                await self.module.flush()
                if not specification.wait_before:
                    await asyncio.sleep(specification.interval)
        except asyncio.CancelledError:
            raise

    def stop(self):
        if self.task is not None:
            self.task.cancel()
            return self.task
        return None


def loop(interval=5, autostart=False, wait_before=False, stop_clause=None):
    return lambda callback: Loop(callback, interval, autostart, wait_before, stop_clause)


def command(*tags, **options):
    def decorate(callback):
        aliases = options.get("aliases", ())
        if "alias" in options:
            aliases = (*aliases, options["alias"])
        return potato_command(callback.__name__, owner_only=True, aliases=tuple(aliases))(callback)
    return decorate


def owner(callback):
    return callback


def unrestricted(callback):
    callback._heroku_public = True
    return callback


def watcher(*tags, **options):
    supported = {"in", "out", "only_pm", "only_groups", "no_commands", "only_messages", "regex"}
    if set(tags) - supported or set(options) - supported:
        raise RuntimeError("Этот фильтр watcher Heroku пока не поддерживается")
    def decorate(callback):
        callback._heroku_watcher = (tags, options)
        return callback
    return decorate


def tds(module_class):
    for name, callback in vars(module_class).items():
        if inspect.iscoroutinefunction(callback) and name.endswith("cmd") and not hasattr(callback, "__potato_command__"):
            potato_command(name[:-3])(callback)
    return module_class


class Module(PotatoModule):
    _potato_heroku = True
    strings = {}

    def __init__(self):
        self.config = ModuleConfig()

    async def bind(self, context):
        self.context = context
        self._client = context.client
        self.tg_id = context.owner_id
        self._db = ModuleDatabase(self)
        self._cache = await context.storage.all()
        self._dirty = {}
        self._flush_lock = asyncio.Lock()
        self.inline = UnsupportedInline()
        original = dict(type(self).strings)
        self.strings = Strings(self, original)
        self.DISPLAY_NAME = original.get("name", type(self).__name__)
        config = getattr(self, "config", ModuleConfig())
        self.config = config
        self.CONFIG = dict(config)
        if isinstance(config, ModuleConfig):
            for key in config:
                if f"config:{key}" in self._cache:
                    config[key] = self._cache[f"config:{key}"]
            config.changed = lambda key, value: self.set(f"config:{key}", value)
        for name, callback in inspect.getmembers(self, inspect.ismethod):
            if name.endswith("cmd") and not hasattr(callback, "__potato_command__"):
                potato_command(name[:-3])(callback.__func__)
            specification = getattr(callback, "__potato_command__", None)
            if specification and getattr(callback, "_heroku_public", False):
                potato_command(specification.name, owner_only=False, aliases=specification.aliases)(callback.__func__)
            filters = getattr(callback, "_heroku_watcher", None)
            if filters is not None or name == "watcher":
                wrapped = self._watcher_wrapper(callback, filters or ((), {}))
                setattr(self, f"_potato_watch_{name}", wrapped.__get__(self, type(self)))

    def _watcher_wrapper(self, callback, filters):
        tags, options = filters
        enabled = {*tags, *(key for key, value in options.items() if value)}
        async def wrapped(instance, event):
            if "only_pm" in enabled and not event.is_private or "only_groups" in enabled and not event.is_group:
                return
            if "no_commands" in enabled and (event.raw_text or "").startswith(instance.get_prefix()):
                return
            await callback(getattr(event, "message", event))
        incoming = "out" not in enabled or "in" in enabled
        outgoing = "in" not in enabled or "out" in enabled
        return on_message(incoming=incoming, outgoing=outgoing, pattern=options.get("regex"))(wrapped)

    def get(self, key, default=None):
        return self._cache.get(key, default)

    def set(self, key, value):
        self._cache[key] = value
        self._dirty[key] = value
        return True

    async def flush(self):
        async with self._flush_lock:
            pending = dict(self._dirty)
            for key, value in pending.items():
                await self.context.storage.set(key, value)
                if self._dirty.get(key) == value:
                    self._dirty.pop(key, None)

    def get_prefix(self):
        return self.context.manager.policy.state.get("prefix", ".")

    def lookup(self, name):
        return next((module for module in self.context.manager.modules.values() if type(module).__name__ == name or module.DISPLAY_NAME == name), None)

    async def on_load(self):
        ready = getattr(self, "client_ready", None)
        if ready:
            count = len(inspect.signature(ready).parameters)
            if count == 0:
                await ready()
            elif count == 2:
                await ready(self._client, self._db)
            else:
                raise RuntimeError("Неподдерживаемая сигнатура client_ready Heroku")
        await self.flush()
        for name in dir(type(self)):
            definition = getattr(type(self), name)
            if isinstance(definition, Loop) and definition.autostart:
                getattr(self, name).start()

    async def close_adapter(self):
        tasks = [loop.stop() for loop in self.__dict__.get("_heroku_loops", {}).values()]
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
        await self.flush()


class ModuleDatabase:
    def __init__(self, module):
        self.module = module

    def get(self, namespace, key, default=None):
        return self.module.get(f"db:{namespace}:{key}", default)

    def set(self, namespace, key, value):
        return self.module.set(f"db:{namespace}:{key}", value)


class UnsupportedInline:
    def __getattr__(self, name):
        raise RuntimeError("Inline-интерфейсы Heroku пока не поддерживаются")
