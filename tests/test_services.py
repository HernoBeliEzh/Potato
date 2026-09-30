import asyncio
import hashlib
import io
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientSession
from telethon import functions, TelegramClient
from telethon.errors import FloodWaitError, MultiError
from telethon.sessions import MemorySession
from telethon.tl.types import InputPeerSelf

from potato.backups import MAX_BACKUP_SIZE, create_backup, read_backup
from potato.inline import InlineBot
from potato.manager import ModuleManager
from potato.releases import ModuleValidationError, ReleaseManager, inspect_module
from potato.services import AccountServices
from potato.settings import Settings
from potato.storage import Storage
from potato.telegram_client import RequestBudget, ProtectedTelegramClient
from potato.updates import UpdateMonitor


NATIVE = b"from potato.api import Module\nAPI_VERSION = 1\nclass Example(Module):\n    DISPLAY_NAME = 'Example'\n"
HEROKU = b"from .. import loader, utils\nfrom herokutl.tl.types import Message\n@loader.tds\nclass Sample(loader.Module):\n    strings = {'name': 'Sample', 'ok': 'ready'}\n    strings_ru = {'ok': 'gotovo'}\n    def __init__(self):\n        self.config = loader.ModuleConfig(loader.ConfigValue('amount', 2, validator=loader.validators.Integer(minimum=1)))\n    async def client_ready(self, client, db):\n        self.set('ready', True)\n    @loader.command()\n    async def sample(self, message):\n        self.set('calls', self.get('calls', 0) + 1)\n        await utils.answer(message, self.strings('ok'))\n    async def legacycmd(self, message):\n        await utils.answer(message, utils.get_args_raw(message))\n    @loader.watcher('only_pm', 'in')\n    async def observe(self, message):\n        self.set('watched', True)\n    @loader.loop(interval=0.01, autostart=True)\n    async def ticker(self):\n        self.set('ticks', self.get('ticks', 0) + 1)\n"


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def archive(self, manifest, files):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            for name, content in files:
                archive.writestr(name, content)
        return buffer.getvalue()

    def test_roundtrip_native_and_heroku_sources(self):
        (self.root / "native.py").write_bytes(NATIVE)
        (self.root / "foreign.py").write_bytes(HEROKU)
        content = create_backup(self.root)
        self.assertEqual(dict(read_backup(content, True)), {"native.py": NATIVE, "foreign.py": HEROKU})
        with self.assertRaisesRegex(ValueError, "Heroku"):
            read_backup(content, False)

    def test_archive_excludes_secrets_and_pending(self):
        (self.root / "native.py").write_bytes(NATIVE)
        (self.root / "state.sqlite").write_bytes(b"secret")
        (self.root / "pending").mkdir()
        (self.root / "pending" / "other.py").write_bytes(NATIVE)
        self.assertEqual(set(dict(read_backup(create_backup(self.root), False))), {"native.py"})

    def test_empty_archive_is_valid(self):
        self.assertEqual(read_backup(create_backup(None), False), [])

    def test_rejects_corruption_and_size(self):
        with self.assertRaises(ValueError):
            read_backup(b"not a zip", False)
        with self.assertRaises(ValueError):
            read_backup(b"x" * (MAX_BACKUP_SIZE + 1), False)

    def test_rejects_paths_hashes_and_extra_files(self):
        for name, checksum, extra in (("../native.py", hashlib.sha256(NATIVE).hexdigest(), False), ("native.py", "0" * 64, False), ("native.py", hashlib.sha256(NATIVE).hexdigest(), True)):
            manifest = {"format": "potato-modules", "version": 1, "modules": [{"file": name, "type": "potato", "sha256": checksum}]}
            files = [(name, NATIVE)] + ([("extra.txt", b"extra")] if extra else [])
            with self.assertRaises(ValueError):
                read_backup(self.archive(manifest, files), False)


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_sync_does_not_take_command_budget(self):
        client = ProtectedTelegramClient(MemorySession(), 1, "hash")
        client.policy = SimpleNamespace(state={})
        client.budget.acquire = AsyncMock(return_value=0)
        with patch.object(TelegramClient, "_call", new_callable=AsyncMock):
            await client._call(None, functions.updates.GetStateRequest())
            client.budget.acquire.assert_not_awaited()
            await client._call(None, functions.messages.SendMessageRequest(InputPeerSelf(), "answer"))
            client.budget.acquire.assert_awaited_once()

    async def test_batches_are_sent_as_budget_is_acquired(self):
        client = ProtectedTelegramClient(MemorySession(), 1, "hash")
        client.policy = SimpleNamespace(state={})
        client.budget = RequestBudget(window=0.04, total=3, ordinary=2)
        sent = []
        async def send(sender, request, ordered, threshold):
            sent.append(time.monotonic())
            return request.message
        requests = [functions.messages.SendMessageRequest(InputPeerSelf(), str(index)) for index in range(3)]
        with patch.object(TelegramClient, "_call", side_effect=send):
            self.assertEqual(await client._call(None, requests), ["0", "1", "2"])
        self.assertGreater(sent[2] - sent[0], 0.025)

    async def test_batch_preserves_rpc_errors_and_partial_results(self):
        client = ProtectedTelegramClient(MemorySession(), 1, "hash")
        requests = [functions.messages.SendMessageRequest(InputPeerSelf(), "ok"), functions.messages.SendMessageRequest(InputPeerSelf(), "blocked")]
        async def send(sender, request, ordered, threshold):
            if request.message == "blocked":
                raise FloodWaitError(request, capture=10)
            return request.message
        with patch.object(TelegramClient, "_call", side_effect=send):
            with self.assertRaises(MultiError) as raised:
                await client._call(None, requests)
        self.assertEqual(raised.exception.results, ["ok", None])
        self.assertIsInstance(raised.exception.exceptions[1], FloodWaitError)

    async def test_ordinary_load_keeps_priority_reserve(self):
        budget = RequestBudget(window=0.15, total=3, ordinary=2)
        await budget.acquire()
        await budget.acquire()
        waiting = asyncio.create_task(budget.acquire())
        await asyncio.sleep(0.01)
        self.assertFalse(waiting.done())
        self.assertLess(await budget.acquire(True), 0.02)
        self.assertFalse(waiting.done())
        self.assertGreater(await waiting, 0.05)

    async def test_total_budget_includes_priority(self):
        budget = RequestBudget(window=0.04, total=2, ordinary=1)
        await budget.acquire()
        await budget.acquire(True)
        self.assertGreater(await budget.acquire(True), 0.02)


class AccountFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.storage = Storage(self.root / "state.sqlite")
        await self.storage.open()
        self.http = ClientSession()
        self.client = MagicMock()
        self.client.send_message = AsyncMock()
        self.client.send_file = AsyncMock(return_value=SimpleNamespace(id=99))
        settings = Settings("default", 1, "secret_hash", self.root, "127.0.0.1", 8080, "")
        self.releases = ReleaseManager(self.root)
        self.manager = ModuleManager(settings, self.client, 100, self.storage, self.http, self.releases, asyncio.Event())
        self.services = AccountServices(self.manager)
        self.manager.services = self.services
        await self.services.load()
        await self.manager.load_all()
        self.inline = InlineBot(self.manager)
        self.manager.inline = self.inline
        self.inline.client = SimpleNamespace(send_message=AsyncMock(side_effect=self.sent), is_connected=lambda: True, disconnect=AsyncMock())
        self.message_id = 1

    def sent(self, *args, **kwargs):
        self.message_id += 1
        return SimpleNamespace(id=self.message_id)

    async def asyncTearDown(self):
        await self.services.close()
        await self.inline.close()
        await self.manager.close()
        await self.http.close()
        await self.storage.close()
        self.directory.cleanup()

    def event(self, text="", user=100):
        return SimpleNamespace(raw_text=text, sender_id=user, chat_id=user, is_private=True, is_group=False, out=True, edit=AsyncMock(), reply=AsyncMock())

    async def callback(self, data, user=100, message_id=None):
        event = SimpleNamespace(data=data.encode(), sender_id=user, chat_id=user, message_id=message_id or self.services.state.get("bot_message_id"), answer=AsyncMock(), edit=AsyncMock())
        await self.inline._callback(event)
        return event

    async def test_setup_persists_sequence_and_custom_interval(self):
        await self.inline._message(self.event("/start"))
        await self.callback("setup:lang:en")
        self.assertEqual(self.manager.localizer.locale, "en")
        self.assertEqual(self.services.state["setup_stage"], "backup")
        await self.callback("setup:custom")
        await self.inline._message(self.event("-1"))
        self.assertEqual(self.services.state["setup_stage"], "custom")
        await self.inline._message(self.event("7"))
        self.assertEqual(self.services.state["backup_hours"], 7)
        await self.callback("setup:heroku:1")
        self.assertTrue(self.releases.allow_heroku)
        self.assertEqual(self.services.state["setup_stage"], "done")
        restored = AccountServices(self.manager)
        await restored.load()
        self.assertEqual(restored.state["backup_hours"], 7)
        await self.inline._message(self.event("/start"))
        self.assertEqual(self.services.state["setup_stage"], "done")

    async def test_strangers_and_stale_buttons_cannot_change_settings(self):
        await self.inline._message(self.event("/start", 200))
        self.inline.client.send_message.assert_not_awaited()
        await self.inline._message(self.event("/start"))
        await self.callback("setup:lang:en", user=200)
        await self.callback("setup:lang:en", message_id=9999)
        self.assertEqual(self.manager.localizer.locale, "ru")
        await self.manager.policy.set("extra_owners", [200])
        await self.inline._message(self.event("/start", 200))
        self.assertEqual(self.inline.client.send_message.await_count, 1)

    async def test_all_presets_and_disable(self):
        for hours in (1, 3, 6, 12, 24, 48, 96, 0):
            self.services.state["setup_stage"] = "backup"
            await self.inline.show_setup()
            await self.callback(f"setup:period:{hours}")
            self.assertEqual(self.services.state["backup_hours"], hours)
            self.assertEqual(self.services.state["setup_stage"], "heroku")

    async def test_settings_edit_returns_to_menu(self):
        self.services.state["setup_stage"] = "done"
        await self.inline.show_setup()
        await self.callback("menu:language")
        await self.callback("setup:lang:en")
        self.assertEqual(self.services.state["setup_stage"], "done")

    async def test_forum_reuses_topics_and_repairs_deleted_topic(self):
        topics = {}
        requests = []
        async def execute(request):
            requests.append(request)
            if isinstance(request, functions.channels.CreateChannelRequest):
                return SimpleNamespace(chats=[SimpleNamespace(id=123)])
            if isinstance(request, functions.messages.GetForumTopicsByIDRequest):
                return SimpleNamespace(topics=[SimpleNamespace(id=identifier, title=topics[identifier]) for identifier in request.topics if identifier in topics])
            if isinstance(request, functions.messages.GetForumTopicsRequest):
                return SimpleNamespace(topics=[SimpleNamespace(id=identifier, title=title) for identifier, title in topics.items() if title == request.q])
            if isinstance(request, functions.messages.CreateForumTopicRequest):
                identifier = max(topics, default=10) + 1
                topics[identifier] = request.title
                return SimpleNamespace(updates=[SimpleNamespace(message=SimpleNamespace(id=identifier, action=SimpleNamespace(title=request.title)))])
            raise AssertionError(type(request))
        self.client.side_effect = execute
        await self.services.ensure_group()
        await self.services.ensure_group()
        self.assertEqual(sum(isinstance(request, functions.channels.CreateChannelRequest) for request in requests), 1)
        self.assertEqual(set(topics.values()), {"Бекап", "Ошибки"})
        del topics[self.services.state["backup_topic"]]
        await self.services.ensure_group()
        self.assertEqual(set(topics.values()), {"Бекап", "Ошибки"})

    async def test_errors_are_redacted_and_coalesced(self):
        for _ in range(3):
            self.services.record_error("module", RuntimeError("secret_hash token=123456:" + "A" * 35))
        self.assertEqual(self.services.errors.qsize(), 1)
        text = self.services.errors.get_nowait()
        self.assertNotIn("secret_hash", text)
        self.assertNotIn("A" * 35, text)

    async def test_overdue_backup_runs_once_and_disable_stops_schedule(self):
        self.services.backup = AsyncMock()
        self.services.state.update(backup_hours=1, next_backup_at=time.time() - 100000)
        task = asyncio.create_task(self.services._backup_loop())
        await asyncio.sleep(0.03)
        self.services.backup.assert_awaited_once()
        self.assertGreater(self.services.state["next_backup_at"], time.time())
        await self.services.set_period(0)
        await asyncio.sleep(0.02)
        self.services.backup.assert_awaited_once()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_heroku_commands_storage_watcher_config_and_loop(self):
        path = self.root / "sample.py"
        path.write_bytes(HEROKU)
        with self.assertRaises(ModuleValidationError):
            inspect_module(path.name, HEROKU)
        self.assertEqual(inspect_module(path.name, HEROKU, allow_heroku=True).kind, "heroku")
        await self.manager._load_class("sample", self.manager._import_class(path, heroku=True))
        module = self.manager.modules["sample"]
        event = self.event(".sample")
        await self.manager.dispatch_message(event)
        event.edit.assert_awaited_with("gotovo", parse_mode="html")
        self.assertEqual(await module.context.storage.get("calls"), 1)
        legacy = self.event(".legacy value")
        await self.manager.dispatch_message(legacy)
        legacy.edit.assert_awaited_with("value", parse_mode="html")
        watcher = self.event("hello")
        watcher.out = False
        await self.manager.dispatch_message(watcher)
        self.assertTrue(await module.context.storage.get("watched"))
        await self.manager.dispatch_message(self.event(".config sample amount 3"))
        self.assertEqual(module.config["amount"], 3)
        await asyncio.sleep(0.025)
        self.assertGreater(module.get("ticks", 0), 0)
        await module.close_adapter()
        self.assertFalse(module.ticker.status)

    async def test_unsupported_heroku_inline_is_rejected(self):
        source = HEROKU + b"\nfrom ..inline.types import InlineCall\n"
        with self.assertRaisesRegex(ModuleValidationError, "inline"):
            inspect_module("foreign.py", source, allow_heroku=True)

    async def test_monitor_notifies_once_and_only_on_new_commit(self):
        with patch.dict("os.environ", {"POTATO_BUILD_SHA": "1" * 40}):
            monitor = UpdateMonitor(self.manager)
        monitor.latest_commit = AsyncMock(return_value="2" * 40)
        await monitor.check()
        await monitor.check()
        self.assertEqual(self.inline.client.send_message.await_count, 1)
        monitor.latest_commit.return_value = "1" * 40
        await monitor.check()
        self.assertEqual(self.inline.client.send_message.await_count, 1)

    async def test_update_queue_is_owner_only_and_idempotent(self):
        control = self.root / "control"
        control.mkdir()
        (control / "worker.json").write_text(json.dumps({"heartbeat": time.time()}))
        with patch.dict("os.environ", {"POTATO_UPDATE_DIR": str(control), "POTATO_BUILD_SHA": "1" * 40}):
            monitor = UpdateMonitor(self.manager)
        monitor.latest_commit = AsyncMock(return_value="2" * 40)
        event = SimpleNamespace(sender_id=200, chat_id=200, answer=AsyncMock())
        await monitor.request_update(event, "2" * 40)
        self.assertFalse((control / "request.json").exists())
        event.sender_id = event.chat_id = 100
        await monitor.request_update(event, "2" * 40)
        first = json.loads((control / "request.json").read_text())
        await monitor.request_update(event, "2" * 40)
        self.assertEqual(json.loads((control / "request.json").read_text()), first)
        self.assertEqual(monitor.latest_commit.await_count, 1)
