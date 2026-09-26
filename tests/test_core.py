import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer
from telethon import functions
from telethon.errors import PremiumAccountRequiredError
from telethon.extensions import html as telegram_html
from telethon.tl.types import (
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityCustomEmoji,
    MessageEntityItalic,
)

from potato.api import Module, command, interval, on_message, webhook
from potato.__main__ import login
from potato.application import AccountRuntime
from potato.manager import ModuleManager
from potato.presentation import CUSTOM_EMOJI, safe, send_html
from potato.releases import ModuleValidationError, ReleaseManager, inspect_module
from potato.settings import Settings
from potato.storage import Storage


MODULE_SOURCE = b"from potato.api import Module, command\nAPI_VERSION = 1\nREQUIRES = ()\nclass Example(Module):\n    @command('example')\n    async def example(self, event):\n        await event.reply('ok')\n"


class FakeClient:
    def __init__(self):
        self.downloaded = MODULE_SOURCE
        self.connected = False
        self.handlers = []
        self.messages = []
        self.requests = []
        self.disconnection = asyncio.Event()

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace()

    async def download_media(self, message, file):
        return self.downloaded

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return SimpleNamespace(id=100)

    def add_event_handler(self, handler, event):
        self.handlers.append(handler)

    async def send_message(self, target, text):
        self.messages.append((target, text))

    async def run_until_disconnected(self):
        await self.disconnection.wait()

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.connected = False
        self.disconnection.set()


class FakeEvent:
    def __init__(self, text, sender_id, *, outgoing=True, reply_message=None):
        self.raw_text = text
        self.sender_id = sender_id
        self.out = outgoing
        self.chat_id = sender_id
        self.reply_message = reply_message
        self.replies = []
        self.edits = []
        self.reply_options = []
        self.edit_options = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        self.reply_options.append(kwargs)

    async def edit(self, text, **kwargs):
        self.edits.append(text)
        self.edit_options.append(kwargs)
        self.raw_text = text

    async def get_reply_message(self):
        return self.reply_message


class ModuleInspectionTests(unittest.TestCase):
    def test_accepts_declared_module(self):
        info = inspect_module("example.py", MODULE_SOURCE)
        self.assertEqual(info.identifier, "example")
        self.assertEqual(info.requirements, ())

    def test_example_module_matches_public_api(self):
        example = Path(__file__).resolve().parent.parent / "examples" / "hello.py"
        info = inspect_module(example.name, example.read_bytes())
        self.assertEqual(info.identifier, "hello")

    def test_rejects_invalid_input(self):
        cases = [
            ("../example.py", MODULE_SOURCE),
            ("example.py", b"API_VERSION = 1\nclass Example(Module):\n  value = 1\ninvalid syntax"),
            ("example.py", b"from potato.api import Module\nclass Example(Module):\n  value = 1"),
            ("example.py", b"from potato.api import Module\nAPI_VERSION = 2\nclass Example(Module):\n  value = 1"),
            ("example.py", b"from potato.api import Module\nAPI_VERSION = True\nclass Example(Module):\n  value = 1"),
            ("example.py", b"from potato.api import Module\nAPI_VERSION = 1\nREQUIRES = ('pkg @ https://example.com/pkg.whl',)\nclass Example(Module):\n  value = 1"),
        ]
        for filename, content in cases:
            with self.subTest(filename=filename, content=content):
                with self.assertRaises(ModuleValidationError):
                    inspect_module(filename, content)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.requirements = self.root / "requirements.lock"
        self.requirements.write_text("Telethon==1.45.0\n", encoding="utf-8")
        self.releases = ReleaseManager(self.root, self.requirements)

    def test_stages_and_activates_module(self):
        self.releases.stage("example.py", MODULE_SOURCE)
        with patch.object(self.releases, "_install"):
            self.assertTrue(self.releases.prepare())
        self.assertEqual((self.releases.active_modules() / "example.py").read_bytes(), MODULE_SOURCE)
        self.assertFalse((self.releases.pending / "example.py").exists())
        self.assertTrue(self.releases.confirm_activation([]))

    def test_rejects_core_dependency_conflict(self):
        incompatible = MODULE_SOURCE.replace(b"REQUIRES = ()", b"REQUIRES = ('Telethon==2.0',)")
        with self.assertRaises(ModuleValidationError):
            self.releases.stage("example.py", incompatible)

    def test_install_failure_keeps_previous_release(self):
        self.releases.stage("example.py", MODULE_SOURCE)
        with patch.object(self.releases, "_install"):
            self.releases.prepare()
        self.releases.confirm_activation([])
        previous = self.releases.current_release()
        replacement = MODULE_SOURCE.replace(b"REQUIRES = ()", b"REQUIRES = ('other==1',)")
        self.releases.stage("example.py", replacement)
        with patch.object(self.releases, "_install", side_effect=RuntimeError("pip failed")):
            self.assertFalse(self.releases.prepare())
        self.assertEqual(self.releases.current_release(), previous)
        self.assertEqual((self.releases.active_modules() / "example.py").read_bytes(), MODULE_SOURCE)
        self.assertIn("pip failed", self.releases.consume_error())

    def test_load_failure_rolls_back(self):
        self.releases.stage("example.py", MODULE_SOURCE)
        with patch.object(self.releases, "_install"):
            self.releases.prepare()
        self.assertFalse(self.releases.confirm_activation(["example failed"]))
        self.assertIsNone(self.releases.current_release())
        self.assertIn("example failed", self.releases.consume_error())

    def test_interrupted_activation_rolls_back(self):
        self.releases.stage("example.py", MODULE_SOURCE)
        with patch.object(self.releases, "_install"):
            self.releases.prepare()
        self.releases.mark_attempted()
        self.assertFalse(self.releases.prepare())
        self.assertIsNone(self.releases.current_release())
        self.assertIn("остановил запуск", self.releases.consume_error())

    def test_retains_current_and_previous_release(self):
        first = None
        for index in range(3):
            source = MODULE_SOURCE.replace(b"'ok'", f"'version-{index}'".encode())
            self.releases.stage("example.py", source)
            with patch.object(self.releases, "_install"):
                self.assertTrue(self.releases.prepare())
            self.assertTrue(self.releases.confirm_activation([]))
            if index == 0:
                first = self.releases.current_release()
        self.assertEqual(len(list(self.releases.releases.iterdir())), 2)
        self.assertFalse(first.exists())


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.directory.name) / "state.sqlite")
        await self.storage.open()

    async def asyncTearDown(self):
        await self.storage.close()
        self.directory.cleanup()

    async def test_isolates_accounts_and_modules(self):
        first = self.storage.for_module("first", "one")
        second = self.storage.for_module("second", "one")
        third = self.storage.for_module("first", "two")
        await first.set("value", {"count": 1})
        self.assertEqual(await first.get("value"), {"count": 1})
        self.assertIsNone(await second.get("value"))
        self.assertIsNone(await third.get("value"))
        await first.delete("value")
        self.assertIsNone(await first.get("value"))


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.storage = Storage(root / "state.sqlite")
        await self.storage.open()
        self.http = ClientSession()
        self.client = FakeClient()
        self.shutdown = asyncio.Event()
        self.releases = ReleaseManager(root)
        settings = Settings("default", 1, "hash", root, "127.0.0.1", 8080, "https://example.com/source")
        self.manager = ModuleManager(
            settings, self.client, 100, self.storage, self.http, self.releases, self.shutdown
        )
        await self.manager.load_all()

    async def asyncTearDown(self):
        await self.manager.close()
        await self.http.close()
        await self.storage.close()
        self.directory.cleanup()

    async def test_owner_commands_and_public_source(self):
        stranger = FakeEvent(". ReStArT", 200, outgoing=False)
        await self.manager.dispatch_message(stranger)
        self.assertFalse(self.shutdown.is_set())
        self.assertEqual(stranger.replies, [])

        public = FakeEvent(". SoUrCe", 200, outgoing=False)
        await self.manager.dispatch_message(public)
        self.assertIn("https://example.com/source", public.replies[0])

        owner = FakeEvent(". StAtUs", 100)
        await self.manager.dispatch_message(owner)
        self.assertIn("Potato работает", owner.edits[0])

        outgoing = FakeEvent(".STATUS", None)
        await self.manager.dispatch_message(outgoing)
        self.assertIn("Potato работает", outgoing.edits[0])

    async def test_ping_measures_telegram_and_uptime(self):
        event = FakeEvent(". PiNg", 100)
        await self.manager.dispatch_message(event)
        self.assertIsInstance(self.client.requests[0], functions.PingRequest)
        self.assertEqual(event.replies, [])
        self.assertEqual(len(event.edits), 1)
        self.assertEqual(event.edit_options[0], {"parse_mode": "html"})
        rendered, entities = telegram_html.parse(event.edits[0])
        self.assertRegex(rendered, r"Пинг Telegram: \d+ мс")
        self.assertRegex(rendered, r"Время работы: \d{2}:\d{2}:\d{2}")
        self.assertTrue(any(isinstance(entity, MessageEntityCustomEmoji) for entity in entities))
        self.assertTrue(any(isinstance(entity, MessageEntityBold) for entity in entities))
        self.assertTrue(any(isinstance(entity, MessageEntityItalic) for entity in entities))

    async def test_help_groups_loaded_modules_and_commands(self):
        class Tasks(Module):
            @command("work")
            async def work(self, event):
                await event.reply("done")

        class Watcher(Module):
            @on_message(incoming=True)
            async def watch(self, event):
                await event.reply("seen")

        await self.manager._load_class("tasks", Tasks)
        await self.manager._load_class("watcher", Watcher)
        event = FakeEvent(". HELP", 100)
        await self.manager.dispatch_message(event)
        response = "\n".join(telegram_html.parse(reply)[0] for reply in event.edits + event.replies)
        self.assertIn("6 модулей активно", response)
        self.assertIn("▪️ PotatoHelp: ( help | helphide | support )", response)
        self.assertIn("▪️ PotatoTester: ( ping )", response)
        self.assertIn("▪️ Tasks: ( work )", response)
        self.assertIn("▪️ Watcher: ( без команд )", response)
        self.assertEqual(event.replies, [])
        self.assertTrue(all(options == {"parse_mode": "html"} for options in event.edit_options))
        self.assertTrue(all(options == {"parse_mode": "html"} for options in event.reply_options))
        entities = [entity for reply in event.edits + event.replies for entity in telegram_html.parse(reply)[1]]
        self.assertTrue(any(isinstance(entity, MessageEntityBlockquote) for entity in entities))
        self.assertTrue(any(isinstance(entity, MessageEntityBold) for entity in entities))

    async def test_helphide_changes_visible_modules_without_unloading_them(self):
        await self.manager.dispatch_message(FakeEvent(".helphide PotatoTester", 100))
        hidden = FakeEvent(".help", 100)
        await self.manager.dispatch_message(hidden)
        self.assertNotIn("PotatoTester", hidden.edits[0])
        self.assertIn("potato_tester", self.manager.modules)

        await self.manager.dispatch_message(FakeEvent(".helphide potato_tester", 100))
        visible = FakeEvent(".help", 100)
        await self.manager.dispatch_message(visible)
        self.assertIn("PotatoTester", visible.edits[0])

    async def test_help_keeps_long_html_pages_complete(self):
        handler = self.manager.commands["status"]
        for index in range(120):
            self.manager.commands[f"long_command_{index:03d}_{'x' * 28}"] = handler

        event = FakeEvent(".help", 100)
        await self.manager.dispatch_message(event)
        self.assertGreater(len(event.edits + event.replies), 1)
        self.assertEqual(len(event.edits), 1)
        self.assertTrue(all(len(reply) <= 3800 for reply in event.edits + event.replies))
        rendered = "\n".join(telegram_html.parse(reply)[0] for reply in event.edits + event.replies)
        self.assertIn("long_command_000_", rendered)
        self.assertIn("long_command_119_", rendered)
        self.assertTrue(
            all(
                any(isinstance(entity, MessageEntityBlockquote) for entity in telegram_html.parse(reply)[1])
                for reply in event.edits + event.replies
            )
        )

    async def test_lm_stages_file_until_restart(self):
        reply = SimpleNamespace(
            document=object(), file=SimpleNamespace(name="example.py", size=len(MODULE_SOURCE))
        )
        command_event = FakeEvent(". LM", 100, reply_message=reply)
        await self.manager.dispatch_message(command_event)
        self.assertTrue((self.releases.pending / "example.py").exists())
        self.assertNotIn("example", self.manager.modules)
        self.assertIn(".restart", command_event.edits[0])

        await self.manager.dispatch_message(FakeEvent(". ReStArT", 100))
        self.assertTrue(self.shutdown.is_set())

    async def test_commands_ignore_case_and_allow_space_after_dot(self):
        seen = []

        class Localized(Module):
            @command("Команда")
            async def localized(self, event):
                seen.append(event.raw_text)

            @command("mixed-name")
            async def mixed(self, event):
                seen.append(event.raw_text)

        await self.manager._load_class("localized", Localized)
        commands = (".команда", ". КОМАНДА", ".   КоМаНдА аргумент", ".MIXED-NAME", ". mixed-name")
        for content in commands:
            await self.manager.dispatch_message(FakeEvent(content, 100))
        self.assertEqual(seen, list(commands))

        for content in (".", ".   ", "КОМАНДА", " .команда"):
            await self.manager.dispatch_message(FakeEvent(content, 100))
        self.assertEqual(seen, list(commands))

    async def test_command_names_cannot_collide_across_case(self):
        class First(Module):
            @command("Команда")
            async def run(self, event):
                await event.reply("first")

        class Second(Module):
            @command("КОМАНДА")
            async def run(self, event):
                await event.reply("second")

        await self.manager._load_class("first", First)
        with self.assertRaises(ValueError):
            await self.manager._load_class("second", Second)

    async def test_command_aliases_are_registered_and_validated(self):
        seen = []

        class Aliased(Module):
            @command("primary", aliases=("other",))
            async def run(self, event):
                seen.append(event.raw_text)

        await self.manager._load_class("aliased", Aliased)
        await self.manager.dispatch_message(FakeEvent(". OTHER", 100))
        self.assertEqual(seen, [". OTHER"])
        self.assertEqual(self.manager.commands["primary"], self.manager.commands["other"])

        class Duplicate(Module):
            @command("another", aliases=("OTHER",))
            async def run(self, event):
                await event.reply("duplicate")

        with self.assertRaises(ValueError):
            await self.manager._load_class("duplicate", Duplicate)

    async def test_dispatches_messages_tasks_and_webhooks(self):
        seen = []
        ticked = asyncio.Event()

        class Probe(Module):
            @command("probe")
            async def probe(self, event):
                seen.append("command")

            @on_message(incoming=True, pattern="hello")
            async def watch(self, event):
                seen.append("message")

            @interval(0.01, run_on_start=True)
            async def tick(self):
                ticked.set()

            @webhook("notify")
            async def notify(self, request):
                return {"accepted": True}

        await self.manager._load_class("probe", Probe)
        await self.manager.dispatch_message(FakeEvent(".probe", 100))
        await self.manager.dispatch_message(FakeEvent("hello", 200, outgoing=False))
        await asyncio.wait_for(ticked.wait(), timeout=1)
        self.assertEqual(seen, ["command", "message"])

        handler = self.manager.webhooks[("probe", "notify", "POST")]
        request = SimpleNamespace(
            method="POST",
            headers={"Authorization": "Bearer invalid"},
            match_info={"account": "default", "module": "probe", "route": "notify"},
        )
        with self.assertRaises(web.HTTPUnauthorized):
            await self.manager.handle_webhook(request)
        request.headers["Authorization"] = f"Bearer {handler.secret}"
        response = await self.manager.handle_webhook(request)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.text, '{"accepted": true}')

        application = web.Application()
        application.router.add_route("*", "/hooks/{account}/{module}/{route}", self.manager.handle_webhook)
        server = TestServer(application)
        client = TestClient(server)
        await client.start_server()
        try:
            unauthorized = await client.post("/hooks/default/probe/notify")
            self.assertEqual(unauthorized.status, 401)
            authorized = await client.post(
                "/hooks/default/probe/notify",
                headers={"Authorization": f"Bearer {handler.secret}"},
            )
            self.assertEqual(authorized.status, 200)
            self.assertEqual(await authorized.json(), {"accepted": True})
        finally:
            await client.close()

    async def test_new_module_import_failure_restores_previous_release(self):
        broken = (
            b"from potato.api import Module\nAPI_VERSION = 1\nREQUIRES = ()\n"
            b"import module_that_does_not_exist\nclass Broken(Module):\n    value = 1\n"
        )
        self.releases.stage("broken.py", broken)
        with patch.object(self.releases, "_install"):
            self.releases.prepare()
        replacement = ModuleManager(
            self.manager.settings,
            self.client,
            100,
            self.storage,
            self.http,
            self.releases,
            self.shutdown,
        )
        try:
            await replacement.load_all()
            self.assertIn("broken", replacement.failures[0])
            self.assertFalse(self.releases.confirm_activation(replacement.failures))
            self.assertIsNone(self.releases.current_release())
        finally:
            await replacement.close()

    async def test_custom_webhook_verifier(self):
        class Signed(Module):
            async def verify(self, request):
                return request.headers.get("X-Signature") == "valid"

            @webhook("signed", verifier="verify")
            async def receive(self, request):
                return {"received": True}

        await self.manager._load_class("signed", Signed)
        request = SimpleNamespace(
            method="POST",
            headers={"X-Signature": "invalid"},
            match_info={"account": "default", "module": "signed", "route": "signed"},
        )
        with self.assertRaises(web.HTTPUnauthorized):
            await self.manager.handle_webhook(request)
        request.headers["X-Signature"] = "valid"
        response = await self.manager.handle_webhook(request)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.text, '{"received": true}')


class PresentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_emoji_falls_back_for_non_premium_account(self):
        event = FakeEvent(".help", 100)
        with patch.object(
            event,
            "edit",
            side_effect=[PremiumAccountRequiredError(None), None],
        ) as edit:
            await send_html(event, f"{CUSTOM_EMOJI} <b>Potato</b>")

        self.assertEqual(edit.await_count, 2)
        self.assertNotIn("tg-emoji", edit.await_args_list[1].args[0])
        self.assertEqual(edit.await_args_list[1].kwargs, {"parse_mode": "html"})

    async def test_dynamic_text_is_escaped(self):
        self.assertEqual(safe('<script attr="x">&'), "&lt;script attr=&quot;x&quot;&gt;&amp;")


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_qr_login_refreshes_expired_code(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings("default", 1, "hash", Path(directory), "127.0.0.1", 8080, "")
            client = AsyncMock()
            client.is_user_authorized.return_value = False
            client.get_me.return_value = SimpleNamespace(id=100)
            qr_login = AsyncMock()
            qr_login.url = "tg://login?token=first"
            qr_login.wait.side_effect = [asyncio.TimeoutError(), SimpleNamespace(id=100)]

            async def recreate():
                qr_login.url = "tg://login?token=second"

            qr_login.recreate.side_effect = recreate
            client.qr_login.return_value = qr_login
            with (
                patch("telethon.TelegramClient", return_value=client),
                patch("qrcode.QRCode") as qr_code,
                patch("builtins.print"),
            ):
                await login(settings, qr=True)

            self.assertEqual(qr_code.return_value.add_data.call_count, 2)
            qr_code.return_value.add_data.assert_any_call("tg://login?token=first")
            qr_code.return_value.add_data.assert_any_call("tg://login?token=second")
            qr_login.recreate.assert_awaited_once()
            client.disconnect.assert_awaited_once()

    async def test_qr_login_accepts_two_factor_password(self):
        from telethon.errors import SessionPasswordNeededError

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings("default", 1, "hash", Path(directory), "127.0.0.1", 8080, "")
            client = AsyncMock()
            client.is_user_authorized.return_value = False
            client.get_me.return_value = SimpleNamespace(id=100)
            qr_login = AsyncMock()
            qr_login.url = "tg://login?token=first"
            qr_login.wait.side_effect = SessionPasswordNeededError(None)
            client.qr_login.return_value = qr_login
            with (
                patch("telethon.TelegramClient", return_value=client),
                patch("qrcode.QRCode"),
                patch("builtins.print"),
                patch("potato.__main__.getpass.getpass", return_value="password"),
            ):
                await login(settings, qr=True)

            client.sign_in.assert_awaited_once_with(password="password")
            client.disconnect.assert_awaited_once()


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_starts_and_stops_on_restart_request(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        storage = Storage(root / "state.sqlite")
        await storage.open()
        client = FakeClient()
        releases = ReleaseManager(root)
        settings = Settings("default", 1, "hash", root, "127.0.0.1", 8080, "")
        try:
            async with ClientSession() as http:
                account = AccountRuntime(settings, releases, storage, http)
                with patch("potato.application.TelegramClient", return_value=client):
                    await account.start()
                self.assertTrue(client.is_connected())
                self.assertEqual(len(client.handlers), 1)
                account.shutdown.set()
                await asyncio.wait_for(account.wait(), timeout=1)
                await account.close()
                self.assertFalse(client.is_connected())
        finally:
            await storage.close()
            directory.cleanup()
