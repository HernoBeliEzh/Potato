import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientSession
from telethon.errors import AccessTokenExpiredError
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.tl.custom import InlineBuilder

from potato.api import Module, inline_command
from potato.inline import BotFather, InlineBot, potato_bots
from potato.manager import ModuleManager
from potato.releases import ReleaseManager, inspect_module
from potato.settings import Settings
from potato.storage import Storage


TOKEN = "123456789:" + "A" * 35


def response(text="", names=()):
    rows = [SimpleNamespace(buttons=[SimpleNamespace(text=name) for name in names])] if names else []
    return SimpleNamespace(raw_text=text, reply_markup=SimpleNamespace(rows=rows))


def conversation_client(messages):
    conversation = SimpleNamespace(
        send_message=AsyncMock(side_effect=lambda text: SimpleNamespace(raw_text=text)),
        get_response=AsyncMock(side_effect=messages),
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=conversation)
    context.__aexit__ = AsyncMock(return_value=False)
    client = SimpleNamespace(
        get_entity=AsyncMock(return_value=SimpleNamespace(id=93372553, verified=True)),
        conversation=MagicMock(return_value=context),
    )
    return client, conversation


class ProvisioningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.directory.name) / "state.sqlite")
        await self.storage.open()
        self.bot_storage = self.storage.for_module("default", "__potato_inline__")

    async def asyncTearDown(self):
        await self.storage.close()
        self.directory.cleanup()

    async def test_finds_any_owned_potato_prefix_and_prefers_saved_bot(self):
        names = ["@OtherBot", "@Potato_ABC_Bot", "@potato_custombot", "@Potato_Tool", "PotatoFakeBot"]
        self.assertEqual(potato_bots(response(names=names)), ["Potato_ABC_Bot", "potato_custombot", "Potato_Tool"])
        client, conversation = conversation_client([
            response(), response(names=names), response(f"Use this token:\n{TOKEN}"),
            response(), response(), response("Success! Inline mode enabled."), response(),
        ])
        credentials = await BotFather(client, self.bot_storage).prepare("POTATO_CUSTOMBOT")
        self.assertEqual(credentials, {"username": "potato_custombot", "token": TOKEN})
        sent = [call.args[0] for call in conversation.send_message.await_args_list]
        self.assertNotIn("/newbot", sent)
        self.assertIn("@potato_custombot", sent)
        self.assertEqual(await self.bot_storage.get("credentials"), credentials)
        self.assertIsNone(await self.storage.for_module("second", "__potato_inline__").get("credentials"))

    async def test_creates_bot_and_retries_taken_username(self):
        client, conversation = conversation_client([
            response(), response("You don't have any bots yet."), response(),
            response("How are we going to call it? Please choose a name."),
            response("Choose a username for your bot."), response("Sorry, this username is already taken."),
            response(f"Done!\n{TOKEN}"), response(), response(), response("Success!"), response(),
        ])
        credentials = await BotFather(client, self.bot_storage).prepare()
        self.assertRegex(credentials["username"], r"^Potato_[a-zA-Z0-9]{12}Bot$")
        sent = [call.args[0] for call in conversation.send_message.await_args_list]
        generated = [text for text in sent if text.startswith("Potato_")]
        self.assertEqual(len(generated), 2)
        self.assertNotEqual(*generated)
        self.assertEqual(sent.count("/newbot"), 1)

    async def test_unexpected_botfather_reply_does_not_create_duplicate(self):
        client, conversation = conversation_client([response(), response("Service unavailable"), response()])
        with self.assertRaisesRegex(RuntimeError, "список"):
            await BotFather(client, self.bot_storage).prepare()
        self.assertNotIn("/newbot", [call.args[0] for call in conversation.send_message.await_args_list])

    async def test_botfather_timeout_does_not_create_duplicate(self):
        client, conversation = conversation_client([response(), asyncio.TimeoutError(), response()])
        with self.assertRaises(asyncio.TimeoutError):
            await BotFather(client, self.bot_storage).prepare()
        sent = [call.args[0] for call in conversation.send_message.await_args_list]
        self.assertNotIn("/newbot", sent)
        self.assertEqual(sent[-1], "/cancel")

    async def test_creation_limit_stops_before_sending_bot_name(self):
        client, conversation = conversation_client([
            response(), response("You don't have any bots yet."), response(),
            response("Sorry, you cannot create more than 20 bots."), response(),
        ])
        with self.assertRaisesRegex(RuntimeError, "лимит"):
            await BotFather(client, self.bot_storage).prepare()
        self.assertNotIn("Potato Inline", [call.args[0] for call in conversation.send_message.await_args_list])

    async def test_keeps_credentials_when_inline_configuration_fails(self):
        client, conversation = conversation_client([
            response(), response(names=["@Potato_TestBot"]), response(TOKEN),
            response(), response(), response("Service unavailable"), response(),
        ])
        with self.assertRaisesRegex(RuntimeError, "inline"):
            await BotFather(client, self.bot_storage).prepare()
        self.assertEqual((await self.bot_storage.get("credentials"))["username"], "Potato_TestBot")

    async def test_rejects_unverified_botfather(self):
        client, conversation = conversation_client([])
        client.get_entity.return_value.verified = False
        with self.assertRaisesRegex(RuntimeError, "официальный"):
            await BotFather(client, self.bot_storage).prepare()
        conversation.send_message.assert_not_awaited()


class InlineRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.storage = Storage(root / "state.sqlite")
        await self.storage.open()
        self.manager = SimpleNamespace(
            storage=self.storage,
            settings=Settings("default", 1, "hash", root, "127.0.0.1", 8080, ""),
            client=SimpleNamespace(),
            dispatch_inline=AsyncMock(),
        )
        self.inline = InlineBot(self.manager)
        self.credentials = {"username": "Potato_TestBot", "token": TOKEN}
        self.bot = SimpleNamespace(
            connect=AsyncMock(), disconnect=AsyncMock(), add_event_handler=MagicMock(),
            is_connected=MagicMock(return_value=True),
            sign_in=AsyncMock(return_value=SimpleNamespace(bot=True, username="Potato_TestBot", bot_inline_placeholder="Potato")),
        )

    async def asyncTearDown(self):
        await self.inline.close()
        await self.storage.close()
        self.directory.cleanup()

    async def test_reuses_saved_token_without_botfather_and_disconnects(self):
        await self.inline.storage.set("credentials", self.credentials)
        with patch("potato.inline.TelegramClient", return_value=self.bot), patch("potato.inline.BotFather.prepare", new_callable=AsyncMock) as prepare:
            self.assertTrue(await self.inline.start())
            prepare.assert_not_awaited()
        self.assertTrue(self.inline.ready)
        self.assertEqual(self.inline.username, "Potato_TestBot")
        self.assertEqual(self.bot.add_event_handler.call_count, 3)
        await self.inline.close()
        self.bot.disconnect.assert_awaited_once()
        self.assertFalse(self.inline.ready)

    async def test_recovers_revoked_token_from_owned_bot(self):
        await self.inline.storage.set("credentials", self.credentials)
        self.bot.sign_in.side_effect = [AccessTokenExpiredError(request=None), self.bot.sign_in.return_value]
        with patch("potato.inline.TelegramClient", return_value=self.bot), patch("potato.inline.BotFather.prepare", new_callable=AsyncMock, return_value=self.credentials) as prepare:
            self.assertTrue(await self.inline.start())
            prepare.assert_awaited_once_with("Potato_TestBot")
        self.bot.disconnect.assert_awaited_once()

    async def test_setup_failure_is_nonfatal_and_does_not_expose_token(self):
        with patch("potato.inline.BotFather.prepare", new_callable=AsyncMock, side_effect=ValueError(TOKEN)):
            self.assertFalse(await self.inline.start())
        self.assertNotIn(TOKEN, self.inline.error)
        self.assertFalse(self.inline.ready)

    async def test_disabled_inline_is_enabled_without_creating_another_bot(self):
        await self.inline.storage.set("credentials", self.credentials)
        disabled = SimpleNamespace(bot=True, username="Potato_TestBot", bot_inline_placeholder=None)
        self.bot.sign_in.side_effect = [disabled, self.bot.sign_in.return_value]
        with patch("potato.inline.TelegramClient", return_value=self.bot), patch("potato.inline.BotFather.prepare", new_callable=AsyncMock, return_value=self.credentials) as prepare:
            self.assertTrue(await self.inline.start())
            prepare.assert_awaited_once_with("Potato_TestBot")
        self.bot.disconnect.assert_awaited_once()


class InlineDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.storage = Storage(root / "state.sqlite")
        await self.storage.open()
        self.http = ClientSession()
        self.manager = ModuleManager(
            Settings("default", 1, "hash", root, "127.0.0.1", 8080, ""),
            SimpleNamespace(), 100, self.storage, self.http, ReleaseManager(root), asyncio.Event(),
        )
        await self.manager.load_all()

    async def asyncTearDown(self):
        await self.manager.close()
        await self.http.close()
        await self.storage.close()
        self.directory.cleanup()

    def event(self, text="", user_id=100):
        return SimpleNamespace(
            text=text, sender_id=user_id, answer=AsyncMock(),
            builder=SimpleNamespace(article=MagicMock(side_effect=lambda title, **values: {"title": title, **values})),
        )

    async def test_builtin_inline_status_and_default_help(self):
        status = self.event("STATUS")
        await self.manager.dispatch_inline(status)
        self.assertIn("Potato работает", status.answer.await_args.args[0][0]["text"])
        self.assertEqual(status.answer.await_args.kwargs, {"cache_time": 0, "private": True})
        help_event = self.event()
        await self.manager.dispatch_inline(help_event)
        self.assertIn("status", help_event.answer.await_args.args[0][0]["text"])

    async def test_status_builds_valid_telethon_result_with_inline_button(self):
        client = TelegramClient(MemorySession(), 1, "hash")
        event = self.event("status")
        event.builder = InlineBuilder(client)
        results = []

        async def answer(articles, **options):
            results.extend(await asyncio.gather(*articles))

        event.answer = AsyncMock(side_effect=answer)
        await self.manager.dispatch_inline(event)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].type, "article")
        self.assertIn("Potato работает", results[0].send_message.message)
        self.assertEqual(results[0].send_message.reply_markup.rows[0].buttons[0].type.query, "status")
        self.assertTrue(bytes(results[0]))

    async def test_enforces_command_query_group_block_and_disabled_module(self):
        stranger = self.event("status", 200)
        await self.manager.dispatch_inline(stranger)
        self.assertEqual(stranger.answer.await_args.args[0], [])
        await self.manager.policy.set("inline_access", {"status": "public"})
        await self.manager.dispatch_inline(stranger)
        self.assertEqual(stranger.answer.await_args.args[0], [])
        await self.manager.policy.set("query_access", "group:friends")
        await self.manager.dispatch_inline(stranger)
        self.assertEqual(stranger.answer.await_args.args[0], [])
        await self.manager.policy.set("groups", {"friends": [200]})
        await self.manager.dispatch_inline(stranger)
        self.assertEqual(stranger.answer.await_args.args[0], [])
        await self.manager.policy.set("blocked_users", [200])
        await self.manager.dispatch_inline(stranger)
        self.assertEqual(stranger.answer.await_args.args[0], [])
        await self.manager.policy.set("blocked_users", [])
        await self.manager.policy.set("disabled_modules", ["system"])
        owner = self.event("status")
        await self.manager.dispatch_inline(owner)
        self.assertEqual(owner.answer.await_args.args[0], [])

    async def test_registers_public_module_inline_alias_and_arguments(self):
        class Echo(Module):
            @inline_command("echo", owner_only=False, aliases=("say",))
            async def echo(self, event):
                await event.answer([event.builder.article("Echo", text=event.potato_args)], cache_time=0, private=True)

        await self.manager._load_class("echo", Echo)
        event = self.event("SAY hello world")
        await self.manager.dispatch_inline(event)
        self.assertEqual(event.answer.await_args.args[0][0]["text"], "hello world")
        await self.manager.policy.set("inline_access", {"echo": "owner"})
        event = self.event("SAY hello world", 200)
        await self.manager.dispatch_inline(event)
        self.assertEqual(event.answer.await_args.args[0], [])

    async def test_rejects_inline_collision_and_primary_permission_override(self):
        class Collision(Module):
            @inline_command("STATUS")
            async def status(self, event):
                await event.answer([])

        with self.assertRaisesRegex(ValueError, "duplicate"):
            await self.manager._load_class("collision", Collision)

        class Private(Module):
            @inline_command("private", primary_only=True)
            async def private(self, event):
                await event.answer([event.builder.article("Private", text="private")])

        await self.manager._load_class("private", Private)
        await self.manager.policy.set("inline_access", {"private": "public"})
        await self.manager.policy.set("extra_owners", [200])
        event = self.event("private", 200)
        await self.manager.dispatch_inline(event)
        self.assertEqual(event.answer.await_args.args[0], [])

    async def test_example_loads_through_public_module_api(self):
        example = Path(__file__).resolve().parent.parent / "examples" / "inline_echo.py"
        self.assertEqual(inspect_module(example.name, example.read_bytes()).identifier, "inline_echo")
        await self.manager._load_class("inline_echo", self.manager._import_class(example))
        event = self.event("echo <test>")
        await self.manager.dispatch_inline(event)
        self.assertEqual(event.answer.await_args.args[0][0]["text"], "<test>")
        self.assertIsNone(event.answer.await_args.args[0][0]["parse_mode"])

    async def test_security_commands_persist_and_apply_inline_rules(self):
        def command_event(text):
            return SimpleNamespace(raw_text=text, sender_id=100, chat_id=100, out=True, edit=AsyncMock(), reply=AsyncMock())

        await self.manager.dispatch_message(command_event(".inlinesec status public"))
        event = self.event("status", 200)
        await self.manager.dispatch_inline(event)
        self.assertEqual(event.answer.await_args.args[0], [])
        await self.manager.dispatch_message(command_event(".querysec owner"))
        await self.manager.dispatch_inline(event)
        self.assertEqual(event.answer.await_args.args[0], [])
        await self.manager.dispatch_message(command_event(".querysec default"))
        await self.manager.dispatch_message(command_event(".inlinesec status default"))
        await self.manager.dispatch_inline(event)
        self.assertEqual(event.answer.await_args.args[0], [])
        saved = await self.storage.for_module("default", "__potato_core__").get("state")
        self.assertEqual(saved["inline_access"], {})
        self.assertEqual(saved["query_access"], "public")
