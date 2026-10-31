"""Tunnel launcher checks use temporary config and synthetic credentials only."""
import argparse
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "library_tunnel", Path(__file__).resolve().parents[1] / "scripts/library-tunnel.py")
tunnel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tunnel)


class TunnelTests(unittest.TestCase):
    def test_environment_drops_ambient_overrides_and_unrelated_secrets(self):
        with patch.dict(os.environ, {"HOME": "/tmp", "PATH": "/usr/bin",
                                    "OPENAI_API_KEY": "synthetic-other-key",
                                    "LOG_HTTP_RAW_UNSAFE": "true",
                                    "MCP_COMMAND": "untrusted",
                                    "RUNTIME_KEY": "synthetic-runtime-key"}, clear=True):
            actual = tunnel.secret_env("RUNTIME_KEY")
        self.assertEqual(actual, {"HOME": "/tmp", "PATH": "/usr/bin",
                                  "CONTROL_PLANE_API_KEY": "synthetic-runtime-key"})

    def test_getpass_is_memory_only(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(tunnel.sys.stdin, "isatty", return_value=True), \
                patch.object(tunnel.getpass, "getpass", return_value="synthetic-key"):
            self.assertEqual(tunnel.secret_env("KEY")["CONTROL_PLANE_API_KEY"], "synthetic-key")

    def test_noninteractive_requires_environment(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(tunnel.sys.stdin, "isatty", return_value=False):
            with self.assertRaises(ValueError):
                tunnel.secret_env("KEY")

    def test_configuration_rejects_secret_as_tunnel_id_before_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(tunnel.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                tunnel.configure(argparse.Namespace(tunnel_id="sk-synthetic-secret", key_env="KEY"), Path(tmp))
            run.assert_not_called()

    @unittest.skipUnless(tunnel.BINARY.exists(), "project tunnel-client binary is not installed")
    def test_official_init_is_local_private_and_secret_free(self):
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "private"
            with patch.dict(os.environ, {"CONTROL_PLANE_API_KEY": "synthetic-do-not-persist"}), \
                    redirect_stdout(io.StringIO()):
                tunnel.configure(argparse.Namespace(tunnel_id="tunnel_00000000000000000000000000000000", key_env="KEY"), root)
            files = [path for path in root.rglob("*") if path.is_file()]
            self.assertTrue(files)
            for path in files:
                self.assertNotIn("synthetic-do-not-persist", path.read_text())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            profiles = list((root / "profiles").iterdir())
            config = yaml.safe_load(profiles[0].read_text())
            self.assertEqual(config["control_plane"]["api_key"], "env:CONTROL_PLANE_API_KEY")
            self.assertIn("env -u CONTROL_PLANE_API_KEY -u OPENAI_API_KEY", config["mcp"]["commands"][0]["command"])
            self.assertFalse(tunnel.status(root)["process_running"])
            with self.assertRaises(ValueError), redirect_stdout(io.StringIO()):
                tunnel.configure(argparse.Namespace(tunnel_id="tunnel_other", key_env="KEY"), root)

    def test_stale_pid_does_not_claim_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tunnel.write_json(root / "process.json", {"pid": os.getpid(), "start": "wrong"})
            with patch.object(tunnel, "probe") as probe:
                actual = tunnel.status(root)
            self.assertFalse(actual["process_running"])
            self.assertFalse(actual["ready"])
            probe.assert_not_called()

    def test_doctor_never_prints_upstream_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tunnel.write_json(root / "settings.json", {"key_env": "KEY"})
            output = io.StringIO()
            result = argparse.Namespace(returncode=1, stdout="synthetic-sensitive-upstream-output")
            with patch.object(tunnel.subprocess, "run", return_value=result) as run, \
                    patch.dict(os.environ, {"KEY": "synthetic-key"}), redirect_stdout(output):
                self.assertEqual(tunnel.doctor(root), 1)
            self.assertNotIn("synthetic", output.getvalue())
            self.assertFalse(json.loads(output.getvalue())["doctor_passed"])
            self.assertEqual(run.call_args.kwargs["stdout"], tunnel.subprocess.DEVNULL)

    def test_command_uses_unix_socket_and_disables_raw_logging(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = tunnel.command("run", Path(tmp))
            self.assertIn("--health.unix-socket", cmd)
            self.assertIn("--log.http-raw-unsafe=false", cmd)
            self.assertIn("--mcp.stdio-send-initialized-notification=true", cmd)

    def test_foreground_child_exit_cleans_process_record_and_hides_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tunnel.write_json(root / "settings.json", {"key_env": "KEY"})
            output = io.StringIO()
            child_command = [tunnel.sys.executable, "-c",
                             "import time; print('synthetic-private-output'); time.sleep(0.1)"]
            with patch.object(tunnel, "command", return_value=child_command), \
                    patch.dict(os.environ, {"KEY": "synthetic-key"}), redirect_stdout(output):
                self.assertEqual(tunnel.run(root), 0)
            self.assertNotIn("synthetic", output.getvalue())
            self.assertFalse((root / "process.json").exists())
            self.assertFalse(tunnel.status(root)["process_running"])

    def test_duplicate_run_rejected_before_requesting_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tunnel.write_json(root / "settings.json", {"key_env": "KEY"})
            with (root / "run.lock").open("a") as lock:
                tunnel.fcntl.flock(lock, tunnel.fcntl.LOCK_EX | tunnel.fcntl.LOCK_NB)
                with patch.object(tunnel, "secret_env") as secret, self.assertRaises(ValueError):
                    tunnel.run(root)
                secret.assert_not_called()

    def test_start_existing_process_does_not_prompt_or_fork(self):
        with patch.object(tunnel, "verified_pid", return_value=123), \
                patch.object(tunnel, "status", return_value={"process_running": True}), \
                patch.object(tunnel, "secret_env") as secret, patch.object(tunnel.os, "fork") as fork, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(tunnel.start(), 0)
        secret.assert_not_called()
        fork.assert_not_called()

    def test_stop_refuses_changed_identity(self):
        with patch.object(tunnel, "verified_pid", side_effect=[123, 456]), \
                patch.object(tunnel.os, "killpg") as kill:
            with self.assertRaises(ValueError):
                tunnel.stop()
        kill.assert_not_called()

    def test_stop_only_signals_verified_group(self):
        with patch.object(tunnel, "verified_pid", side_effect=[123, 123, None, None]), \
                patch.object(tunnel.os, "killpg") as kill, redirect_stdout(io.StringIO()):
            self.assertEqual(tunnel.stop(), 0)
        kill.assert_called_once_with(123, tunnel.signal.SIGTERM)

    def test_detach_parent_reports_without_key_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tunnel.write_json(root / "settings.json", {"key_env": "KEY"})
            output = io.StringIO()
            with patch.object(tunnel, "verified_pid", return_value=None), \
                    patch.object(tunnel.os, "fork", return_value=1234), \
                    patch.object(tunnel.select, "select", return_value=([1], [], [])), \
                    patch.object(tunnel.os, "read", return_value=b"started"), \
                    patch.object(tunnel, "status", return_value={"ready": False}), \
                    patch.dict(os.environ, {"KEY": "synthetic-key"}), redirect_stdout(output):
                self.assertEqual(tunnel.start(root), 0)
            self.assertNotIn("synthetic-key", output.getvalue())
            self.assertTrue(json.loads(output.getvalue())["background"])


if __name__ == "__main__":
    unittest.main()
