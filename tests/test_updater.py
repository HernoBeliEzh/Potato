import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


WORKER_PATH = Path(__file__).resolve().parent.parent / "deploy" / "update_worker.py"
SPECIFICATION = importlib.util.spec_from_file_location("potato_update_worker", WORKER_PATH)
worker_module = importlib.util.module_from_spec(SPECIFICATION)
SPECIFICATION.loader.exec_module(worker_module)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / ".env").write_text("SECRET=test\n")
        self.worker = worker_module.UpdateWorker(self.root / "updater", self.project)
        self.worker.control.mkdir(parents=True)
        self.request = {"id": "a" * 32, "sha": "b" * 40}

    def tearDown(self):
        self.directory.cleanup()

    def archive(self, unsafe=False):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for name, content in (("repo/deploy/compose.updater.yaml", b"services: {}"), ("repo/tests/test.py", b""), ("repo/compose.yaml", b"services: {}")):
                item = tarfile.TarInfo(name)
                item.size = len(content)
                archive.addfile(item, io.BytesIO(content))
            if unsafe:
                item = tarfile.TarInfo("repo/../../escaped")
                item.size = 1
                archive.addfile(item, io.BytesIO(b"x"))
        return output.getvalue()

    def configure(self, health):
        self.worker.run = MagicMock(return_value="")
        self.worker.current_image = MagicMock(return_value="old-image")
        self.worker.compose = MagicMock(return_value="")
        self.worker.snapshot = MagicMock()
        self.worker.healthy = MagicMock(side_effect=health)
        self.worker.restore = MagicMock()

    def test_rejects_arbitrary_actions_repositories_and_shell_strings(self):
        for request in ({**self.request, "command": "echo bad"}, {**self.request, "sha": "main;echo bad"}, {**self.request, "repository": "someone/other"}, {**self.request, "id": "../escape"}):
            with self.assertRaises(ValueError):
                worker_module.validate_request(request)

    def test_source_archive_rejects_traversal(self):
        target = self.root / "source"
        target.mkdir()
        with self.assertRaises(ValueError):
            worker_module.unpack_source(self.archive(True), target)
        self.assertFalse((self.root / "escaped").exists())

    def test_success_records_installed_sha(self):
        self.configure([True])
        with patch.object(worker_module, "download", side_effect=[json.dumps({"sha": self.request["sha"]}).encode(), self.archive()]):
            self.assertEqual(self.worker.execute(self.request), "success")
        self.assertEqual(json.loads((self.worker.root / "current.json").read_text())["sha"], self.request["sha"])
        self.assertFalse(self.worker.journal.exists())
        self.worker.snapshot.assert_called_once()

    def test_failed_healthcheck_recreates_previous_image(self):
        self.configure([False, True])
        with patch.object(worker_module, "download", side_effect=[json.dumps({"sha": self.request["sha"]}).encode(), self.archive()]):
            self.assertEqual(self.worker.execute(self.request), "rolled_back")
        calls = self.worker.compose.call_args_list
        self.assertTrue(any(call.args[1] == "old-image" and "up" in call.args for call in calls))
        self.assertFalse(self.worker.journal.exists())
        self.assertFalse((self.worker.root / "current.json").exists())

    def test_build_failure_keeps_running_container(self):
        self.configure([])
        self.worker.run.side_effect = RuntimeError("build failed")
        with patch.object(worker_module, "download", side_effect=[json.dumps({"sha": self.request["sha"]}).encode(), self.archive()]):
            with self.assertRaises(RuntimeError):
                self.worker.execute(self.request)
        self.worker.compose.assert_not_called()
        self.worker.snapshot.assert_not_called()

    def test_stale_sha_is_rejected_before_download_and_build(self):
        self.configure([])
        with patch.object(worker_module, "download", return_value=json.dumps({"sha": "c" * 40}).encode()) as download:
            with self.assertRaises(ValueError):
                self.worker.execute(self.request)
        self.assertEqual(download.call_count, 1)
        self.worker.run.assert_not_called()

    def test_snapshot_restore_preserves_previous_data(self):
        volume = self.root / "volume"
        volume.mkdir()
        (volume / "state.sqlite").write_text("previous")
        self.worker.volume_path = MagicMock(return_value=volume)
        archive = self.root / "snapshot.tar.gz"
        self.worker.snapshot(archive)
        (volume / "state.sqlite").write_text("new")
        (volume / "new-file").write_text("temporary")
        self.worker.restore(archive)
        self.assertEqual((volume / "state.sqlite").read_text(), "previous")
        self.assertFalse((volume / "new-file").exists())
