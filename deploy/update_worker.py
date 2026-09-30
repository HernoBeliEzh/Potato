from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path


REPOSITORY = "HernoBeliEzh/Potato"
SHA = re.compile(r"[0-9a-f]{40}\Z")
REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")
MAX_ARCHIVE = 64 * 1_048_576


def atomic_json(path, value, public=False):
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.chmod(0o644 if public else 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_request(value):
    if not isinstance(value, dict) or set(value) != {"id", "sha"} or not isinstance(value["id"], str) or not isinstance(value["sha"], str) or not REQUEST_ID.fullmatch(value["id"]) or not SHA.fullmatch(value["sha"]):
        raise ValueError("Invalid update request")
    return value


def download(url, maximum=MAX_ARCHIVE):
    request = urllib.request.Request(url, headers={"User-Agent": "Potato-Updater", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        content = response.read(maximum + 1)
    if len(content) > maximum:
        raise ValueError("Download size limit exceeded")
    return content


def unpack_source(content, destination):
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        members = archive.getmembers()
        if not members or len(members) > 10000 or sum(item.size for item in members) > 256 * 1_048_576:
            raise ValueError("Source archive size limit exceeded")
        root = members[0].name.split("/")[0]
        for member in members:
            parts = member.name.split("/")
            if parts[0] != root or ".." in parts or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise ValueError("Unsafe source archive")
            if len(parts) == 1:
                continue
            target = destination.joinpath(*parts[1:])
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("Unsafe source path")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o644)


class UpdateWorker:
    def __init__(self, root=Path("/opt/potato-updater"), project=Path("/opt/potato")):
        self.root = root
        self.project = project
        self.control = root / "control"
        self.journal = root / "transaction.json"
        self.status = "idle"
        self.stopped = threading.Event()

    def run(self, arguments, *, cwd=None, environment=None, timeout=1200):
        result = subprocess.run(arguments, cwd=cwd, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"Command failed: {arguments[0]} (exit {result.returncode})")
        return result.stdout.strip()

    def compose(self, directory, image, *arguments):
        environment = dict(os.environ, POTATO_IMAGE=image, POTATO_VOLUME_NAME="potato_potato_data")
        return self.run(["docker", "compose", "-p", "potato", "--project-directory", str(directory), "-f", str(directory / "compose.yaml"), "-f", str(directory / "deploy/compose.updater.yaml"), *arguments], environment=environment)

    def current_image(self):
        container = self.run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=potato", "--filter", "label=com.docker.compose.service=potato"]).splitlines()
        if len(container) != 1:
            raise RuntimeError("Expected one Potato container")
        return self.run(["docker", "inspect", "--format", "{{.Image}}", container[0]])

    def volume_path(self):
        path = Path(self.run(["docker", "volume", "inspect", "--format", "{{.Mountpoint}}", "potato_potato_data"]))
        docker_root = Path(self.run(["docker", "info", "--format", "{{.DockerRootDir}}"])).resolve()
        if not path.is_dir() or not path.resolve().is_relative_to(docker_root / "volumes") or path.name != "_data":
            raise RuntimeError("Unexpected data volume path")
        return path

    def snapshot(self, path):
        volume = self.volume_path()
        temporary = path.with_suffix(".tmp")
        with tarfile.open(temporary, "w:gz") as archive:
            archive.add(volume, arcname="data")
        temporary.chmod(0o600)
        os.replace(temporary, path)

    def restore(self, path):
        volume = self.volume_path()
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                relative = Path(member.name)
                if relative.parts[0] != "data" or ".." in relative.parts or member.issym() or member.islnk():
                    raise RuntimeError("Unsafe snapshot")
            for item in volume.iterdir():
                if item.is_dir() and not item.is_symlink():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            for member in members:
                relative = Path(member.name)
                if len(relative.parts) == 1:
                    continue
                member.name = str(Path(*relative.parts[1:]))
                archive.extract(member, path=volume, filter="fully_trusted")

    def healthy(self, directory, image, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                container = self.compose(directory, image, "ps", "-q", "potato")
                if container and self.run(["docker", "inspect", "--format", "{{.State.Health.Status}}", container], timeout=15) == "healthy":
                    self.run(["docker", "exec", container, "python", "-c", "import json,urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=5)); assert data['status']=='ok' and data.get('inline', True)"], timeout=15)
                    return True
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
            time.sleep(3)
        return False

    def rollback(self, transaction):
        self.status = "rolling_back"
        old = Path(transaction["old_source"])
        candidate = Path(transaction["source"])
        self.compose(candidate, transaction["image"], "stop", "potato")
        snapshot = Path(transaction["snapshot"])
        if snapshot.exists():
            self.restore(snapshot)
        self.compose(old, transaction["old_image"], "up", "-d", "--no-build", "--force-recreate", "potato")
        if not self.healthy(old, transaction["old_image"]):
            raise RuntimeError("Rollback healthcheck failed")

    def recover(self):
        if not self.journal.exists():
            return
        transaction = json.loads(self.journal.read_text())
        self.rollback(transaction)
        atomic_json(self.control / "result.json", {"id": transaction["id"], "sha": transaction["sha"], "status": "rolled_back"}, public=True)
        self.journal.unlink()

    def execute(self, request):
        validate_request(request)
        identifier, sha = request["id"], request["sha"]
        latest = json.loads(download(f"https://api.github.com/repos/{REPOSITORY}/commits/main", maximum=1_048_576))["sha"]
        if latest != sha:
            raise ValueError("Requested commit is not current main")
        current = self.root / "current.json"
        installed = json.loads(current.read_text()) if current.exists() else {"source": str(self.project), "sha": "unknown"}
        if installed.get("sha") == sha:
            return "success"
        self.status = "downloading"
        directory = self.root / "releases" / sha
        directory.mkdir(parents=True, exist_ok=True)
        content = download(f"https://api.github.com/repos/{REPOSITORY}/tarball/{sha}")
        unpack_source(content, directory)
        if not (directory / "deploy/compose.updater.yaml").is_file() or not (directory / "tests").is_dir():
            raise ValueError("Source has no supported deployment or tests")
        shutil.copy2(self.project / ".env", directory / ".env")
        (directory / ".env").chmod(0o600)
        image = f"potato-update:{sha}"
        self.status = "building"
        self.run(["docker", "build", "--build-arg", f"POTATO_BUILD_SHA={sha}", "-t", image, str(directory)])
        self.status = "testing"
        directory.chmod(0o755)
        for child in directory.rglob("*"):
            if child.is_dir():
                child.chmod(0o755)
        self.run(["docker", "run", "--rm", "--network=none", "-v", f"{directory}:/workspace:ro", "-w", "/workspace", image, "python", "-m", "unittest", "discover", "-s", "tests"])
        previous_image = self.current_image()
        snapshot = self.root / "snapshots" / f"{identifier}.tar.gz"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        transaction = {**request, "source": str(directory), "image": image, "old_source": installed["source"], "old_image": previous_image, "snapshot": str(snapshot)}
        atomic_json(self.journal, transaction)
        try:
            self.status = "snapshotting"
            self.compose(Path(installed["source"]), previous_image, "stop", "potato")
            self.snapshot(snapshot)
            self.status = "starting"
            self.compose(directory, image, "up", "-d", "--no-build", "--force-recreate", "potato")
            if not self.healthy(directory, image):
                raise RuntimeError("New version healthcheck failed")
        except Exception:
            self.rollback(transaction)
            self.journal.unlink()
            return "rolled_back"
        atomic_json(current, {"source": str(directory), "sha": sha, "image": image})
        self.journal.unlink()
        return "success"

    def heartbeat(self):
        while not self.stopped.is_set():
            atomic_json(self.control / "worker.json", {"heartbeat": time.time(), "status": self.status}, public=True)
            self.stopped.wait(5)

    def serve(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.control.mkdir(parents=True, exist_ok=True)
        self.recover()
        heartbeat = threading.Thread(target=self.heartbeat, daemon=True)
        heartbeat.start()
        try:
            while True:
                path = self.control / "request.json"
                if path.is_file() and not path.is_symlink():
                    try:
                        if path.stat().st_size > 1024:
                            raise ValueError("Request too large")
                        request = validate_request(json.loads(path.read_text()))
                    except (ValueError, OSError):
                        path.unlink(missing_ok=True)
                    else:
                        path.unlink(missing_ok=True)
                        try:
                            status = self.execute(request)
                        except Exception as error:
                            print(f"Update failed: {type(error).__name__}", flush=True)
                            if self.journal.exists():
                                self.recover()
                                status = "rolled_back"
                            else:
                                status = "failed"
                        atomic_json(self.control / "result.json", {**request, "status": status}, public=True)
                        self.status = "idle"
                time.sleep(2)
        finally:
            self.stopped.set()
            heartbeat.join(timeout=10)


if __name__ == "__main__":
    os.umask(0o077)
    UpdateWorker().serve()
