import contextlib
import fcntl
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'settings_refresh', Path(__file__).parents[1] / 'scripts/refresh-obsidian-settings.py')
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


class FakeManager:
    def __init__(self, root):
        self.project = root / 'project'
        self.vault = root / 'vault'
        self.project.mkdir()
        (self.vault / '.obsidian').mkdir(parents=True)
        (self.vault / '.obsidian/types.json').write_bytes(b'{"lastPlayedOn":"date"}\n')
        (self.vault / '.obsidian/daily-notes.json').write_bytes(b'{"folder":"private diary"}\n')
        self.current = {'manager': 'systemd', 'unit': refresh.UNIT,
                        'state': 'running', 'alive': True, 'ready': True,
                        'invocation_id': 'a' * 32, 'pid': 101}
        self.stops = self.starts = 0
        self.on_stop = self.on_start = self.on_status = None

    def status(self):
        if self.on_status:
            self.on_status()
        return dict(self.current)

    def stop(self):
        self.stops += 1
        self.current.update(state='inactive', alive=False, ready=False, pid=None)
        if self.on_stop:
            self.on_stop()
        return dict(self.current)

    def start(self):
        self.starts += 1
        self.current.update(state='running', alive=True, ready=True,
                            invocation_id='b' * 32, pid=202)
        if self.on_start:
            self.on_start()
        return dict(self.current)


class Clock:
    def __init__(self):
        self.now = 0
        self.on_sleep = None

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep:
            self.on_sleep()


class SettingsRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = FakeManager(self.root)
        self.clock = Clock()
        self.refresher = refresh.SettingsRefresher(
            self.manager, timeout=1, interval=.25,
            clock=self.clock.time, sleep=self.clock.sleep)
        self.types = self.manager.vault / '.obsidian/types.json'

    def tree(self):
        return {str(path.relative_to(self.root)): (path.is_dir(),
                None if path.is_dir() else path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob('*')}

    def assert_no_restart(self):
        self.assertEqual((self.manager.stops, self.manager.starts), (0, 0))

    def apply(self):
        return self.refresher.apply(self.refresher.plan()['expected_token'])

    def test_default_plan_is_read_only_and_exposes_only_hashes(self):
        before = self.tree()
        result = self.refresher.plan()
        self.assertEqual(result['action'], 'refresh-unconfirmed')
        self.assertIs(result['remote_verified'], False)
        self.assertEqual(self.tree(), before)
        self.assertNotIn('private diary', json.dumps(result))
        self.assertNotIn('lastPlayedOn', json.dumps(result))
        self.assert_no_restart()
        self.assertFalse(self.refresher.runtime.exists())

    def test_missing_token_does_not_create_runtime(self):
        with self.assertRaisesRegex(refresh.RefreshError, 'expected-token-required'):
            self.refresher.apply(None)
        self.assertFalse(self.refresher.runtime.exists())
        self.assert_no_restart()

    def test_settings_change_invalidates_expected_token(self):
        token = self.refresher.plan()['expected_token']
        self.types.write_bytes(b'{"user":"edit"}')
        with self.assertRaisesRegex(refresh.RefreshError, 'stale-expected-token'):
            self.refresher.apply(token)
        self.assert_no_restart()
        self.assertFalse(self.refresher.receipt.exists())

    def test_invocation_change_invalidates_expected_token(self):
        token = self.refresher.plan()['expected_token']
        self.manager.current['invocation_id'] = 'c' * 32
        with self.assertRaisesRegex(refresh.RefreshError, 'stale-expected-token'):
            self.refresher.apply(token)
        self.assert_no_restart()

    def test_instance_change_before_stop_refuses_restart(self):
        token = self.refresher.plan()['expected_token']
        original_check = self.refresher.require_unchanged
        def change_instance(settings):
            original_check(settings)
            self.manager.current['invocation_id'] = 'c' * 32
        with patch.object(self.refresher, 'require_unchanged', side_effect=change_instance):
            with self.assertRaisesRegex(refresh.RefreshError, 'service-changed-during-refresh'):
                self.refresher.apply(token)
        self.assert_no_restart()

    def test_false_or_unknown_readiness_refuses_restart(self):
        for ready in (False, None):
            with self.subTest(ready=ready):
                self.manager.current['ready'] = ready
                self.assertFalse(self.refresher.plan()['can_apply'])
                with self.assertRaisesRegex(refresh.RefreshError, 'sync-not-ready'):
                    self.apply()
        self.assert_no_restart()

    def test_other_cooperating_refresher_blocks_restart(self):
        token = self.refresher.plan()['expected_token']
        self.refresher.runtime.mkdir(parents=True)
        with (self.refresher.runtime / 'settings-refresh.lock').open('w') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(refresh.RefreshError, 'settings-refresh-busy'):
                self.refresher.apply(token)
        self.assert_no_restart()

    def test_success_receipt_and_repeat_are_idempotent(self):
        before = self.refresher.snapshot()
        result = self.apply()
        self.assertEqual(result['action'], 'refreshed')
        self.assertEqual((self.manager.stops, self.manager.starts), (1, 1))
        row = json.loads(self.refresher.receipt.read_bytes())
        self.assertEqual(row['settings'], before)
        self.assertEqual(row['invocation_id'], 'b' * 32)
        self.assertEqual(row['previous_invocation_id'], 'a' * 32)
        self.assertIs(row['remote_verified'], False)
        self.assertIs(row['scan_restart_confirmed'], True)
        self.assertEqual(self.refresher.snapshot(), before)
        receipt = self.refresher.receipt.read_bytes()
        mtime = self.refresher.receipt.stat().st_mtime_ns
        self.assertEqual(self.refresher.plan()['action'], 'noop')
        repeated = self.apply()
        self.assertEqual(repeated['action'], 'noop')
        self.assertIs(repeated['applied'], False)
        self.assertEqual((self.manager.stops, self.manager.starts), (1, 1))
        self.assertEqual(self.refresher.receipt.read_bytes(), receipt)
        self.assertEqual(self.refresher.receipt.stat().st_mtime_ns, mtime)

    def test_edit_during_stop_still_starts_but_never_confirms(self):
        self.manager.on_stop = lambda: self.types.write_bytes(b'user new settings')
        with self.assertRaisesRegex(refresh.RefreshError, 'settings-changed-during-refresh'):
            self.apply()
        self.assertEqual((self.manager.stops, self.manager.starts), (1, 1))
        self.assertEqual(self.types.read_bytes(), b'user new settings')
        self.assertFalse(self.refresher.receipt.exists())

    def test_edit_during_wait_preserves_previous_receipt(self):
        self.apply()
        previous = self.refresher.receipt.read_bytes()
        self.manager.current['invocation_id'] = 'c' * 32
        self.manager.on_start = lambda: self.manager.current.update(ready=False)
        self.clock.on_sleep = lambda: self.types.write_bytes(b'user edit while waiting')
        with self.assertRaisesRegex(refresh.RefreshError, 'settings-changed-during-refresh'):
            self.apply()
        self.assertEqual(self.refresher.receipt.read_bytes(), previous)
        self.assertEqual(self.types.read_bytes(), b'user edit while waiting')

    def test_start_exception_is_sanitized_and_does_not_confirm(self):
        def fail():
            raise RuntimeError('private config body must not escape')
        self.manager.on_start = fail
        with self.assertRaisesRegex(refresh.RefreshError, '^service-start-failed$'):
            self.apply()
        self.assertFalse(self.refresher.receipt.exists())

    def test_terminal_startup_failure_is_visible(self):
        self.manager.on_start = lambda: self.manager.current.update(state='failed', alive=False)
        with self.assertRaisesRegex(refresh.RefreshError, 'service-startup-failed'):
            self.apply()
        self.assertFalse(self.refresher.receipt.exists())

    def test_ready_wait_is_bounded(self):
        self.manager.on_start = lambda: self.manager.current.update(ready=False)
        with self.assertRaisesRegex(refresh.RefreshError, 'service-ready-timeout'):
            self.apply()
        self.assertEqual(self.clock.now, 1)
        self.assertFalse(self.refresher.receipt.exists())

    def test_old_ready_invocation_cannot_confirm_new_scan(self):
        self.manager.on_start = lambda: self.manager.current.update(invocation_id='a' * 32)
        with self.assertRaisesRegex(refresh.RefreshError, 'service-ready-timeout'):
            self.apply()
        self.assertFalse(self.refresher.receipt.exists())

    def test_invocation_change_during_wait_refuses_confirmation(self):
        self.manager.on_start = lambda: self.manager.current.update(ready=False)
        self.clock.on_sleep = lambda: self.manager.current.update(invocation_id='c' * 32, ready=True)
        with self.assertRaisesRegex(refresh.RefreshError, 'service-changed-during-refresh'):
            self.apply()
        self.assertFalse(self.refresher.receipt.exists())

    def test_final_commit_race_removes_unconfirmed_receipt(self):
        replace = refresh.os.replace
        def racing_replace(source, target):
            replace(source, target)
            self.types.write_bytes(b'changed at receipt commit')
        with patch.object(refresh.os, 'replace', side_effect=racing_replace):
            with self.assertRaisesRegex(refresh.RefreshError, 'settings-changed-during-refresh'):
                self.apply()
        self.assertFalse(self.refresher.receipt.exists())
        self.assertEqual(self.types.read_bytes(), b'changed at receipt commit')
        self.assertEqual(list(self.refresher.runtime.glob('.settings-refresh-*')), [])

    def test_malformed_receipt_refuses_restart(self):
        self.refresher.runtime.mkdir(parents=True)
        self.refresher.receipt.write_text('{"fake":"receipt"}')
        with self.assertRaisesRegex(refresh.RefreshError, 'invalid-refresh-receipt'):
            self.apply()
        self.assert_no_restart()

    def test_settings_symlink_refuses_plan(self):
        original = self.types.read_bytes()
        target = self.root / 'other.json'
        target.write_bytes(original)
        self.types.unlink()
        self.types.symlink_to(target)
        with self.assertRaisesRegex(refresh.RefreshError, 'settings-file-unreadable'):
            self.refresher.plan()
        self.assert_no_restart()

    def test_status_exception_never_prints_private_message(self):
        def fail():
            raise RuntimeError('private config content')
        self.manager.on_status = fail
        output = io.StringIO()
        with patch.object(refresh, 'load_manager', return_value=self.manager), \
                patch('sys.argv', ['refresh-obsidian-settings.py']), contextlib.redirect_stdout(output):
            self.assertEqual(refresh.main(), 1)
        self.assertEqual(json.loads(output.getvalue()),
                         {'error': 'service-status-failed', 'remote_verified': False})


    def super_table_refresher(self):
        for relative in refresh.SUPER_TABLE_SETTINGS:
            target = self.manager.vault / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'["existing-plugin", "super-table"]' if relative.endswith('community-plugins.json')
                               else b'synthetic plugin artifact')
        return refresh.SettingsRefresher(self.manager, super_table=True, timeout=1, interval=.25,
                                         clock=self.clock.time, sleep=self.clock.sleep)

    def test_super_table_plan_hashes_six_files_without_writes(self):
        selected = self.super_table_refresher()
        before = self.tree()
        result = selected.plan()
        self.assertEqual(set(result['settings']), set(refresh.SETTINGS + refresh.SUPER_TABLE_SETTINGS))
        self.assertEqual(set(self.refresher.plan()['settings']), set(refresh.SETTINGS))
        self.assertEqual(self.tree(), before)
        self.assert_no_restart()
        self.assertNotIn('synthetic plugin artifact', json.dumps(result))

    def test_super_table_missing_artifact_refuses_before_restart(self):
        selected = self.super_table_refresher()
        (self.manager.vault / '.obsidian/plugins/super-table/main.js').unlink()
        with self.assertRaisesRegex(refresh.RefreshError, 'settings-file-unreadable'):
            selected.plan()
        self.assert_no_restart()
        self.assertFalse(selected.runtime.exists())

    def test_super_table_bundle_edit_invalidates_preview_token(self):
        selected = self.super_table_refresher()
        token = selected.plan()['expected_token']
        (self.manager.vault / '.obsidian/plugins/super-table/main.js').write_bytes(b'new user plugin')
        with self.assertRaisesRegex(refresh.RefreshError, 'stale-expected-token'):
            selected.apply(token)
        self.assert_no_restart()

    def test_super_table_scope_cannot_reuse_default_token(self):
        selected = self.super_table_refresher()
        token = self.refresher.plan()['expected_token']
        with self.assertRaisesRegex(refresh.RefreshError, 'stale-expected-token'):
            selected.apply(token)
        self.assert_no_restart()

    def test_super_table_receipt_keeps_default_receipt_and_is_idempotent(self):
        self.apply()
        previous = self.refresher.receipt.read_bytes()
        selected = self.super_table_refresher()
        self.manager.on_start = lambda: self.manager.current.update(invocation_id='c' * 32)
        result = selected.apply(selected.plan()['expected_token'])
        self.assertEqual(result['action'], 'refreshed')
        self.assertEqual(self.refresher.receipt.read_bytes(), previous)
        self.assertNotEqual(selected.receipt, self.refresher.receipt)
        self.assertIs(json.loads(selected.receipt.read_bytes())['remote_verified'], False)
        self.assertEqual(selected.plan()['action'], 'noop')
        selected.apply(selected.plan()['expected_token'])
        self.assertEqual((self.manager.stops, self.manager.starts), (2, 2))

    def test_super_table_edit_during_stop_starts_but_never_confirms(self):
        selected = self.super_table_refresher()
        target = self.manager.vault / '.obsidian/community-plugins.json'
        self.manager.on_stop = lambda: target.write_bytes(b'["user-added"]')
        with self.assertRaisesRegex(refresh.RefreshError, 'settings-changed-during-refresh'):
            selected.apply(selected.plan()['expected_token'])
        self.assertEqual((self.manager.stops, self.manager.starts), (1, 1))
        self.assertEqual(target.read_bytes(), b'["user-added"]')
        self.assertFalse(selected.receipt.exists())

    def test_super_table_nested_symlink_refuses_inspection(self):
        selected = self.super_table_refresher()
        target = self.manager.vault / '.obsidian/plugins'
        external = self.root / 'outside-plugins'
        target.rename(external)
        target.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(refresh.RefreshError, 'settings-path-is-symlink'):
            selected.plan()
        self.assert_no_restart()

    def test_super_table_cli_is_read_only_by_default(self):
        self.super_table_refresher()
        before = self.tree()
        output = io.StringIO()
        with patch.object(refresh, 'load_manager', return_value=self.manager), \
                patch('sys.argv', ['refresh-obsidian-settings.py', '--super-table']), \
                contextlib.redirect_stdout(output):
            self.assertEqual(refresh.main(), 0)
        self.assertEqual(set(json.loads(output.getvalue())['settings']),
                         set(refresh.SETTINGS + refresh.SUPER_TABLE_SETTINGS))
        self.assertEqual(self.tree(), before)
        self.assert_no_restart()


if __name__ == '__main__':
    unittest.main()
