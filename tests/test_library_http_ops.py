"""Operator controls use temporary Vaults and never reveal generated credentials."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from markdown_library.core import Library

SPEC = importlib.util.spec_from_file_location(
    'library_http_ops', Path(__file__).resolve().parents[1] / 'scripts/library-http.py')
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)


class HttpOperatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.vault = self.base / 'vault'
        Library.initialize(self.vault)
        self.root = self.base / 'private'
        self.output = io.StringIO()
        with redirect_stdout(self.output):
            ops.configure(self.root, self.vault, self.base / 'app.sock')

    def tearDown(self):
        self.tmp.cleanup()

    def test_configure_keeps_password_separate_and_does_not_print_it(self):
        from markdown_library.mcp_oauth import verify_owner_password
        password = (self.root / 'owner-password').read_text().strip()
        config = ops.load_config(self.root)
        self.assertNotIn(password, self.output.getvalue())
        self.assertNotIn(password, (self.root / 'config.json').read_text())
        self.assertTrue(verify_owner_password(password, config['owner_password_hash']))
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        for path in self.root.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_reconfigure_refuses_to_rotate_existing_credentials(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with self.assertRaises(ValueError), redirect_stdout(io.StringIO()):
            ops.configure(self.root, self.vault, self.base / 'other.sock')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_configuration_rejects_uninitialized_vault(self):
        with self.assertRaises(Exception):
            ops.configure(self.base / 'other-private', self.base / 'missing', self.base / 'app.sock')
        self.assertFalse((self.base / 'other-private').exists())

    def test_credentials_refuses_pipe_without_printing_secret(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(ValueError):
            ops.credentials(self.root)
        self.assertEqual(output.getvalue(), '')

    def test_credentials_only_prints_in_explicit_interactive_terminal(self):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(output, 'isatty', return_value=True):
            ops.credentials(self.root)
        self.assertIn((self.root / 'owner-password').read_text().strip(), output.getvalue())

    def test_private_write_rejects_symlink_and_leaves_target(self):
        target = self.base / 'untouched'
        target.write_text('preserve')
        link = self.root / 'link'
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            ops.private_write(link, 'replace')
        self.assertEqual(target.read_text(), 'preserve')

    def test_configure_and_status_never_claim_chatgpt_acceptance(self):
        output = io.StringIO()
        with redirect_stdout(output):
            ops.status(self.root)
        result = json.loads(output.getvalue())
        self.assertFalse(result['healthy'])
        self.assertFalse(result['chatgpt_verified'])

    def test_load_config_rejects_public_permissions(self):
        (self.root / 'config.json').chmod(0o644)
        with self.assertRaises(ValueError):
            ops.load_config(self.root)

    def test_rotation_revokes_grants_and_requires_restart_without_printing_password(self):
        previous = (self.root / 'owner-password').read_text()
        output = io.StringIO()
        with redirect_stdout(output), patch.object(ops, 'revoke_all') as revoke:
            ops.rotate_password(self.root)
        revoke.assert_called_once_with(self.root)
        current = (self.root / 'owner-password').read_text()
        self.assertNotEqual(previous, current)
        self.assertNotIn(current.strip(), output.getvalue())
        self.assertNotIn(previous.strip(), output.getvalue())
        self.assertIn('restart', output.getvalue())

    def test_diagnostics_exports_only_allowlisted_connection_fields(self):
        lines = [json.dumps({'MESSAGE': 'raw secret=synthetic-private-password'}),
                 json.dumps({'__REALTIME_TIMESTAMP': '1792800000000000',
                             'MESSAGE': 'LIBRARY_MCP_CONNECTION ' + json.dumps({
                                 'stage': 'register', 'method': 'POST', 'status': 400,
                                 'reason': 'invalid_redirect_uri', 'password': 'synthetic-private-password',
                                 'registration': {'client_secret': 'synthetic-private-password',
                                                  'callback_count': 1, 'auth_method': 'none',
                                                  'callback_types': ['loopback_v4'],
                                                  'callback_paths': ['callback_id']}})})]
        output = io.StringIO()
        with patch.object(ops.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='\n'.join(lines))), \
                redirect_stdout(output):
            ops.diagnostics()
        self.assertNotIn('synthetic-private-password', output.getvalue())
        event = json.loads(output.getvalue())['events'][0]
        self.assertEqual(event['reason'], 'invalid_redirect_uri')
        self.assertEqual(event['registration']['callback_types'], ['loopback_v4'])


if __name__ == '__main__':
    unittest.main()
