from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from potato import API_VERSION


MAX_MODULE_SIZE = 1_048_576
MODULE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\.py\Z")
RESERVED_MODULES = {"system", "module_admin"}


@dataclass(frozen=True)
class ModuleInfo:
    identifier: str
    requirements: tuple[str, ...]


class ModuleValidationError(ValueError):
    __slots__ = ()


def inspect_module(filename: str, content: bytes) -> ModuleInfo:
    if not MODULE_NAME.fullmatch(filename):
        raise ModuleValidationError("Имя файла должно иметь вид name.py: строчные буквы, цифры и _")
    identifier = filename[:-3]
    if identifier in RESERVED_MODULES:
        raise ModuleValidationError("Это имя зарезервировано встроенным модулем")
    if not content or len(content) > MAX_MODULE_SIZE:
        raise ModuleValidationError("Размер модуля должен быть от 1 байта до 1 МиБ")

    try:
        source = content.decode("utf-8-sig")
        tree = ast.parse(source, filename=filename)
    except (UnicodeError, SyntaxError) as error:
        raise ModuleValidationError(f"Синтаксис модуля недопустим: {error}") from error

    requirements: tuple[str, ...] = ()
    version_found = False
    requirements_found = False
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id == "API_VERSION":
            if version_found:
                raise ModuleValidationError("API_VERSION указан несколько раз")
            version_found = True
            try:
                version = ast.literal_eval(statement.value)
            except (ValueError, TypeError, SyntaxError) as error:
                raise ModuleValidationError("API_VERSION должен быть целым числом") from error
            if type(version) is not int or version != API_VERSION:
                raise ModuleValidationError(f"Требуется API_VERSION = {API_VERSION}")
        if target.id == "REQUIRES":
            if requirements_found:
                raise ModuleValidationError("REQUIRES указан несколько раз")
            requirements_found = True
            try:
                declared = ast.literal_eval(statement.value)
            except (ValueError, TypeError, SyntaxError) as error:
                raise ModuleValidationError("REQUIRES должен быть списком строк") from error
            if not isinstance(declared, (list, tuple)) or not all(
                isinstance(item, str) and item for item in declared
            ):
                raise ModuleValidationError("REQUIRES должен быть списком строк")
            requirements = tuple(declared)

    if not version_found:
        raise ModuleValidationError(f"Укажите API_VERSION = {API_VERSION}")
    classes = [
        statement
        for statement in tree.body
        if isinstance(statement, ast.ClassDef)
        and any(
            isinstance(base, ast.Name) and base.id == "Module"
            or isinstance(base, ast.Attribute) and base.attr == "Module"
            for base in statement.bases
        )
    ]
    if len(classes) != 1:
        raise ModuleValidationError("Файл должен содержать один класс, наследующий Module")

    for item in requirements:
        try:
            requirement = Requirement(item)
        except InvalidRequirement as error:
            raise ModuleValidationError(f"Недопустимая зависимость: {item}") from error
        if requirement.url is not None:
            raise ModuleValidationError("Зависимости по прямым URL не поддерживаются")

    return ModuleInfo(identifier, requirements)


class ReleaseManager:
    def __init__(self, data_dir: Path, requirements_file: Path | None = None):
        self.data_dir = data_dir
        self.root = data_dir / "modules"
        self.pending = self.root / "pending"
        self.releases = self.root / "releases"
        self.failed = self.root / "failed"
        self.pointer = self.root / "current"
        self.probation = self.root / "probation"
        self.error_file = self.root / "activation_error"
        configured = os.getenv("POTATO_REQUIREMENTS_FILE")
        self.requirements_file = requirements_file or (
            Path(configured) if configured else Path.cwd() / "requirements.lock"
        )

    def stage(self, filename: str, content: bytes) -> ModuleInfo:
        info = inspect_module(filename, content)
        self._check_core_requirements(info.requirements)
        self.pending.mkdir(parents=True, exist_ok=True)
        temporary = self.pending / f".{uuid.uuid4().hex}.tmp"
        temporary.write_bytes(content)
        os.replace(temporary, self.pending / filename)
        return info

    def current_release(self) -> Path | None:
        if not self.pointer.exists():
            return None
        identifier = self.pointer.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise RuntimeError("Invalid release pointer")
        release = self.releases / identifier
        if not release.is_dir():
            raise RuntimeError("Active release is missing")
        return release

    def active_modules(self) -> Path | None:
        release = self.current_release()
        return None if release is None else release / "modules"

    def activate_imports(self) -> None:
        release = self.current_release()
        if release is None:
            return
        vendor = release / "vendor"
        if vendor.is_dir():
            sys.path.insert(0, str(vendor))

    def prepare(self) -> bool:
        self._recover_interrupted_activation()
        pending_files = sorted(self.pending.glob("*.py")) if self.pending.exists() else []
        if not pending_files:
            return False

        self.releases.mkdir(parents=True, exist_ok=True)
        previous = self.current_release()
        identifier = uuid.uuid4().hex
        candidate = self.releases / identifier
        candidate_modules = candidate / "modules"
        candidate_modules.mkdir(parents=True)

        try:
            active = None if previous is None else previous / "modules"
            if active is not None:
                for path in active.glob("*.py"):
                    shutil.copy2(path, candidate_modules / path.name)
            for path in pending_files:
                shutil.copy2(path, candidate_modules / path.name)

            requirements: list[str] = []
            for path in sorted(candidate_modules.glob("*.py")):
                info = inspect_module(path.name, path.read_bytes())
                requirements.extend(info.requirements)
            self._check_core_requirements(requirements)
            self._install(candidate, requirements)

        except Exception as error:
            shutil.rmtree(candidate, ignore_errors=True)
            self.failed.mkdir(parents=True, exist_ok=True)
            for path in pending_files:
                if path.exists():
                    os.replace(path, self.failed / f"{identifier}_{path.name}")
            self.error_file.write_text(str(error), encoding="utf-8")
            return False

        probation_data = {
            "candidate": identifier,
            "previous": None if previous is None else previous.name,
            "attempted": False,
        }
        temporary_probation = self.root / f".{identifier}.probation"
        try:
            temporary_probation.write_text(json.dumps(probation_data), encoding="utf-8")
            os.replace(temporary_probation, self.probation)
            self._set_pointer(identifier)
        except OSError as error:
            self.probation.unlink(missing_ok=True)
            shutil.rmtree(candidate, ignore_errors=True)
            self.error_file.write_text(str(error), encoding="utf-8")
            return False
        for path in pending_files:
            with suppress(OSError):
                path.unlink(missing_ok=True)
        with suppress(OSError):
            self.error_file.unlink(missing_ok=True)
        return True

    def mark_attempted(self) -> None:
        probation = self._read_probation()
        if probation is None:
            return
        probation["attempted"] = True
        self._write_probation(probation)

    def confirm_activation(self, failures: list[str]) -> bool:
        probation = self._read_probation()
        if probation is None:
            return True
        if failures:
            self._rollback(probation, "Новый модуль не загрузился: " + "; ".join(failures[:5]))
            return False
        self.probation.unlink(missing_ok=True)
        self._prune_releases({probation["candidate"], probation["previous"]})
        return True

    def _recover_interrupted_activation(self) -> None:
        probation = self._read_probation()
        if probation is None:
            return
        active = self.current_release()
        if active is None or active.name != probation["candidate"]:
            self.probation.unlink(missing_ok=True)
        elif probation["attempted"]:
            self._rollback(probation, "Новый модуль остановил запуск Potato")

    def _rollback(self, probation: dict, message: str) -> None:
        previous = probation["previous"]
        if previous is None:
            self.pointer.unlink(missing_ok=True)
        else:
            self._set_pointer(previous)
        self.probation.unlink(missing_ok=True)
        self.error_file.write_text(message, encoding="utf-8")
        with suppress(OSError):
            shutil.rmtree(self.releases / probation["candidate"])

    def _prune_releases(self, retained: set[str | None]) -> None:
        for path in self.releases.iterdir():
            if path.is_dir() and re.fullmatch(r"[0-9a-f]{32}", path.name) and path.name not in retained:
                with suppress(OSError):
                    shutil.rmtree(path)

    def _set_pointer(self, identifier: str) -> None:
        temporary = self.root / f".{uuid.uuid4().hex}.current"
        temporary.write_text(identifier, encoding="ascii")
        os.replace(temporary, self.pointer)

    def _read_probation(self) -> dict | None:
        if not self.probation.exists():
            return None
        return json.loads(self.probation.read_text(encoding="utf-8"))

    def _write_probation(self, probation: dict) -> None:
        temporary = self.root / f".{uuid.uuid4().hex}.probation"
        temporary.write_text(json.dumps(probation), encoding="utf-8")
        os.replace(temporary, self.probation)

    def consume_error(self) -> str | None:
        if not self.error_file.exists():
            return None
        message = self.error_file.read_text(encoding="utf-8")
        self.error_file.unlink()
        return message

    def _check_core_requirements(self, requirements: tuple[str, ...] | list[str]) -> None:
        core = {}
        for line in self.requirements_file.read_text(encoding="utf-8").splitlines():
            requirement = Requirement(line)
            version = line.split("==", 1)[1]
            core[canonicalize_name(requirement.name)] = version
        for item in requirements:
            requirement = Requirement(item)
            version = core.get(canonicalize_name(requirement.name))
            if version is not None and not requirement.specifier.contains(version):
                raise ModuleValidationError(f"{requirement.name} конфликтует с ядром Potato")

    def _install(self, candidate: Path, requirements: list[str]) -> None:
        vendor = candidate / "vendor"
        vendor.mkdir()
        if not requirements:
            return

        install = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--only-binary=:all:",
            "--target",
            str(vendor),
            "--constraint",
            str(self.requirements_file),
            *requirements,
        ]
        self._run(install)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(vendor) + os.pathsep + environment.get("PYTHONPATH", "")
        self._run([sys.executable, "-m", "pip", "check"], environment)
        self._run(
            [sys.executable, "-c", "import telethon, aiohttp, aiosqlite, packaging"],
            environment,
        )
        result = self._run([sys.executable, "-m", "pip", "freeze"], environment)
        (candidate / "dependencies.lock").write_text(result.stdout, encoding="utf-8")

    @staticmethod
    def _run(command: list[str], environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                env=environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=600,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            output = getattr(error, "stderr", "") or getattr(error, "stdout", "") or str(error)
            raise RuntimeError(f"Не удалось установить зависимости: {output[-2000:]}") from error
