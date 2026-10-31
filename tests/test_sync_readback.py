import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify-sync-readback.py"
spec = importlib.util.spec_from_file_location("verify_sync_readback", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SyncReadbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.vault = self.root / "source"
        self.vault.mkdir()
        self.private = self.root / "private-headless"
        self.private.mkdir(mode=0o700)
        self.remote = self.private / "sync/remote-test"
        self.remote.mkdir(parents=True, mode=0o700)
        self.config = {"vaultId": "remote-test", "vaultPath": str(self.vault), "host": "sync.example",
                       "vaultName": "private", "encryptionKey": "SECRET-KEY", "encryptionSalt": "SECRET-SALT",
                       "encryptionVersion": 3, "conflictStrategy": "conflict", "deviceName": "original",
                       "allowTypes": ["image", "audio", "video", "pdf", "unsupported"],
                       "allowSpecialFiles": ["app", "community-plugin", "community-plugin-data"],
                       "ignoreFolders": [".git", ".markdown-library", ".trash"]}
        module.private_file(self.remote / "config.json", json.dumps(self.config).encode())
        module.private_file(self.private / "auth_token", b"SECRET-AUTH")
        module.private_file(self.remote / "state.db", b"ORIGINAL-INDEX-MUST-NOT-BE-COPIED")
        self.output = self.root / "runtime/final-readback-test"
        self.write("note.md", b"Original note")
        self.write("assets/image.png", b"Image bytes")
        self.write(".obsidian/app.json", b"{}")
        self.write(".obsidian/plugins/copilot/data.json", b'{"model":"local-keychain-only"}')
        self.write(".obsidian/plugins/copilot/main.js", b"plugin")
        self.write(".obsidian/workspace.json", b"excluded")
        self.write("Blogs/.obsidian/app.json", b"nested hidden excluded")
        self.write(".markdown-library/state.json", b"private history excluded")
        self.write(".git/HEAD", b"git excluded")
        self.expected_paths = {"note.md", "assets/image.png", ".obsidian/app.json",
                               ".obsidian/plugins/copilot/data.json", ".obsidian/plugins/copilot/main.js"}

    def write(self, relative, data):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def runner(self, command, **kwargs):
        self.assertEqual(command[-3:-1], ["sync", "--path"])
        downloaded = Path(command[-1])
        xdg = Path(kwargs["env"]["XDG_CONFIG_HOME"])
        self.assertTrue(xdg.is_relative_to(self.private))
        self.assertNotIn("OBSIDIAN_AUTH_TOKEN", kwargs["env"])
        app = xdg / "obsidian-headless"
        remote = app / "sync/remote-test"
        self.assertFalse((remote / "state.db").exists())
        copied = json.loads((remote / "config.json").read_bytes())
        self.assertEqual(copied, {**self.config, "vaultPath": str(downloaded), "syncMode": "pull-only"})
        self.assertEqual((app / "auth_token").read_bytes(), b"SECRET-AUTH")
        self.assertEqual(app.stat().st_mode & 0o777, 0o700)
        self.assertEqual((remote / "config.json").stat().st_mode & 0o777, 0o600)
        for relative in self.expected_paths:
            target = downloaded / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.vault / relative).read_bytes())
        with sqlite3.connect(remote / "state.db") as db:
            db.execute("CREATE TABLE pending_files (uid INTEGER)")
            db.execute("CREATE TABLE meta (key TEXT, value TEXT)")
            db.execute("INSERT INTO meta VALUES ('initial', 'false')")
        return SimpleNamespace(returncode=0, stdout="[time] Fully synced\n", stderr="")

    def execute(self, runner=None):
        return module.verify_readback(self.vault, self.output, self.private, ["official-entry"],
                                      runner=runner or self.runner)

    def test_offline_filter_includes_root_plugins_and_assets_excludes_private_hidden(self):
        self.assertEqual(set(module.manifest(self.vault, self.config)), self.expected_paths)

    def test_fresh_isolated_config_download_hashes_cleanup_and_originals_unchanged(self):
        original = (self.remote / "config.json").read_bytes()
        with patch.dict(os.environ, {"OBSIDIAN_AUTH_TOKEN": "WRONG-INHERITED-ACCOUNT"}):
            result = self.execute()
        self.assertTrue(result["success"])
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["expected_files"], 5)
        self.assertEqual(result["downloaded_files"], 5)
        self.assertEqual((self.remote / "config.json").read_bytes(), original)
        self.assertEqual((self.remote / "state.db").read_bytes(), b"ORIGINAL-INDEX-MUST-NOT-BE-COPIED")
        self.assertEqual(list(self.private.glob(".final-readback-xdg-*")), [])
        self.assertEqual((self.output / "report.json").stat().st_mode & 0o777, 0o600)
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"SECRET-", path.read_bytes())

    def test_failure_cleans_auth_and_does_not_persist_exception_or_subprocess_secrets(self):
        def failing(*args, **kwargs):
            raise RuntimeError("SECRET-AUTH SECRET-KEY")
        result = self.execute(failing)
        self.assertFalse(result["success"])
        self.assertTrue(result["temporary_auth_removed"])
        self.assertEqual(list(self.private.glob(".final-readback-xdg-*")), [])
        self.assertNotIn("SECRET", (self.output / "report.json").read_text())

    def test_path_differences_and_source_changes_prevent_success(self):
        def differing(command, **kwargs):
            result = self.runner(command, **kwargs)
            downloaded = Path(command[-1])
            (downloaded / "note.md").write_bytes(b"Wrong remote bytes")
            (downloaded / "extra.md").write_bytes(b"Extra remote")
            (downloaded / "assets/image.png").unlink()
            (self.vault / "note.md").write_bytes(b"New local bytes")
            return result
        result = self.execute(differing)
        self.assertFalse(result["success"])
        self.assertEqual(result["different"], ["note.md"])
        self.assertEqual(result["missing"], ["assets/image.png"])
        self.assertEqual(result["extra"], ["extra.md"])
        self.assertEqual(result["source_changed"], ["note.md"])

    def test_default_cli_only_checks_configuration_without_temp_auth_or_output(self):
        before = sorted(str(path) for path in self.root.rglob("*"))
        result = subprocess.run([sys.executable, str(SCRIPT), "--vault", str(self.vault),
                                 "--config-root", str(self.private), "--output", str(self.output)],
                                capture_output=True, text=True, check=True)
        report = json.loads(result.stdout)
        self.assertEqual(report["mode"], "prepared-only")
        self.assertFalse(report["started"])
        self.assertFalse(report["temporary_config_created"])
        self.assertNotIn("SECRET", result.stdout + result.stderr)
        self.assertEqual(sorted(str(path) for path in self.root.rglob("*")), before)

    def test_pending_events_and_missing_initial_completion_cannot_claim_success(self):
        def pending(command, **kwargs):
            result = self.runner(command, **kwargs)
            remote = Path(kwargs["env"]["XDG_CONFIG_HOME"]) / "obsidian-headless/sync/remote-test"
            with sqlite3.connect(remote / "state.db") as db:
                db.execute("INSERT INTO pending_files VALUES (1)")
                db.execute("UPDATE meta SET value='true' WHERE key='initial'")
            return result
        result = self.execute(pending)
        self.assertFalse(result["success"])
        self.assertEqual(result["pending"], 1)
        self.assertFalse(result["initial_complete"])

    def test_visible_symlink_and_overlapping_output_are_rejected(self):
        (self.vault / "link.md").symlink_to(self.root / "outside")
        with self.assertRaises(module.ReadbackError):
            self.execute()
        self.assertFalse(self.output.exists())
        with self.assertRaises(module.ReadbackError):
            module.verify_readback(self.vault, self.vault / "download", self.private,
                                   ["official-entry"], runner=self.runner)
