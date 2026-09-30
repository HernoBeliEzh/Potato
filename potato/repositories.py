from __future__ import annotations

from urllib.parse import quote, urlparse

from aiohttp import ClientSession

from potato.releases import MAX_MODULE_SIZE, MODULE_NAME, inspect_module


def parse_repository(address: str) -> dict[str, str]:
    parsed = urlparse(address)
    parts = parsed.path.strip("/").split("/")
    if parsed.scheme != "https" or parsed.hostname != "github.com" or len(parts) < 2:
        raise ValueError("Нужна ссылка на репозиторий GitHub")
    owner, repository = parts[:2]
    repository = repository.removesuffix(".git")
    if not owner.replace("-", "").isalnum() or not repository.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Некорректное имя репозитория")
    if len(parts) > 2 and (len(parts) < 4 or parts[2] != "tree"):
        raise ValueError("Укажите корень репозитория или папку через /tree/ветка/путь")
    return {
        "owner": owner,
        "repository": repository,
        "branch": parts[3] if len(parts) >= 4 else "",
        "path": "/".join(parts[4:]) if len(parts) >= 4 else "",
    }


def repository_label(repository: dict[str, str]) -> str:
    label = f"{repository['owner']}/{repository['repository']}"
    if repository["path"]:
        label += f"/{repository['path']}"
    return label


async def discover_branch(http: ClientSession, repository: dict[str, str]) -> str:
    if repository["branch"]:
        return repository["branch"]
    address = f"https://api.github.com/repos/{repository['owner']}/{repository['repository']}"
    async with http.get(address, headers={"Accept": "application/vnd.github+json"}) as response:
        if response.status != 200:
            raise ValueError(f"GitHub не открыл репозиторий: HTTP {response.status}")
        data = await response.json()
    branch = data.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise ValueError("Не удалось определить ветку репозитория")
    return branch


async def list_modules(http: ClientSession, repository: dict[str, str]) -> dict[str, str]:
    branch = await discover_branch(http, repository)
    path = quote(repository["path"], safe="/")
    address = f"https://api.github.com/repos/{repository['owner']}/{repository['repository']}/contents/{path}"
    async with http.get(address, params={"ref": branch}, headers={"Accept": "application/vnd.github+json"}) as response:
        if response.status != 200:
            raise ValueError(f"GitHub не открыл каталог модулей: HTTP {response.status}")
        entries = await response.json()
    if not isinstance(entries, list):
        raise ValueError("Ссылка должна указывать на каталог модулей")
    result = {}
    for entry in entries:
        filename = entry.get("name", "")
        address = entry.get("download_url", "")
        if entry.get("type") == "file" and MODULE_NAME.fullmatch(filename) and urlparse(address).hostname == "raw.githubusercontent.com":
            result[filename[:-3]] = address
    return result


async def download_module(http: ClientSession, address: str) -> tuple[str, bytes]:
    parsed = urlparse(address)
    filename = parsed.path.rsplit("/", 1)[-1]
    if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com" or not MODULE_NAME.fullmatch(filename):
        raise ValueError("Допустима только прямая HTTPS-ссылка на .py в GitHub")
    async with http.get(address, allow_redirects=False) as response:
        if response.status != 200:
            raise ValueError(f"Не удалось скачать модуль: HTTP {response.status}")
        content = await response.content.read(MAX_MODULE_SIZE + 1)
    inspect_module(filename, content)
    return filename, content
