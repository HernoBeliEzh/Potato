from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys

from potato.releases import ReleaseManager
from potato.settings import Settings


async def login(settings: Settings) -> None:
    from telethon import TelegramClient

    settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(settings.session_path), settings.api_id, settings.api_hash)
    try:
        await client.start(
            phone=lambda: input("Номер телефона: ").strip(),
            code_callback=lambda: input("Код Telegram: ").strip(),
            password=lambda: getpass.getpass("Пароль 2FA: "),
        )
        account = await client.get_me()
        if account is None:
            raise RuntimeError("Авторизация не завершена")
        print(f"Аккаунт {account.id} авторизован")
    finally:
        await client.disconnect()


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="potato")
    parser.add_argument("action", choices=("login", "run"))
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        settings = Settings.from_environment()
        if arguments.action == "login":
            asyncio.run(login(settings))
            return 0

        releases = ReleaseManager(settings.data_dir)
        releases.prepare()
        releases.activate_imports()
        from potato.application import PotatoApplication

        asyncio.run(PotatoApplication(settings, releases).run())
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Potato: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
