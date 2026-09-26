import asyncio
import json
from pathlib import Path
from typing import Any

import aiosqlite


class Storage:
    def __init__(self, path: Path):
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        self.write_lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute(
            "CREATE TABLE IF NOT EXISTS values_store ("
            "account TEXT NOT NULL, module TEXT NOT NULL, key TEXT NOT NULL, "
            "value TEXT NOT NULL, PRIMARY KEY (account, module, key))"
        )
        await self.connection.commit()

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    def for_module(self, account: str, module: str) -> "ModuleStorage":
        return ModuleStorage(self, account, module)


class ModuleStorage:
    def __init__(self, storage: Storage, account: str, module: str):
        self.storage = storage
        self.account = account
        self.module = module

    async def get(self, key: str, default: Any = None) -> Any:
        connection = self.storage.connection
        if connection is None:
            raise RuntimeError("Storage is closed")
        async with connection.execute(
            "SELECT value FROM values_store WHERE account = ? AND module = ? AND key = ?",
            (self.account, self.module, key),
        ) as cursor:
            row = await cursor.fetchone()
        return default if row is None else json.loads(row[0])

    async def set(self, key: str, value: Any) -> None:
        connection = self.storage.connection
        if connection is None:
            raise RuntimeError("Storage is closed")
        encoded = json.dumps(value, ensure_ascii=False)
        async with self.storage.write_lock:
            await connection.execute(
                "INSERT INTO values_store (account, module, key, value) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(account, module, key) DO UPDATE SET value = excluded.value",
                (self.account, self.module, key, encoded),
            )
            await connection.commit()

    async def delete(self, key: str) -> None:
        connection = self.storage.connection
        if connection is None:
            raise RuntimeError("Storage is closed")
        async with self.storage.write_lock:
            await connection.execute(
                "DELETE FROM values_store WHERE account = ? AND module = ? AND key = ?",
                (self.account, self.module, key),
            )
            await connection.commit()
