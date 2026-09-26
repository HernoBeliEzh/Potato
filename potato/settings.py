import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    account: str
    api_id: int
    api_hash: str
    data_dir: Path
    http_host: str
    http_port: int
    source_url: str

    @classmethod
    def from_environment(cls) -> "Settings":
        account = os.getenv("POTATO_ACCOUNT", "default")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", account):
            raise ValueError("POTATO_ACCOUNT must contain 1–32 lowercase letters, digits, _ or -")

        api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
        api_hash = os.getenv("TELEGRAM_API_HASH", "")
        if api_id <= 0 or not api_hash:
            raise ValueError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")

        http_port = int(os.getenv("POTATO_HTTP_PORT", "8080"))
        if not 1 <= http_port <= 65535:
            raise ValueError("POTATO_HTTP_PORT must be between 1 and 65535")

        return cls(
            account=account,
            api_id=api_id,
            api_hash=api_hash,
            data_dir=Path(os.getenv("POTATO_DATA_DIR", "./data")).resolve(),
            http_host=os.getenv("POTATO_HTTP_HOST", "127.0.0.1"),
            http_port=http_port,
            source_url=os.getenv("POTATO_SOURCE_URL", ""),
        )

    @property
    def session_path(self) -> Path:
        return self.data_dir / "sessions" / self.account
