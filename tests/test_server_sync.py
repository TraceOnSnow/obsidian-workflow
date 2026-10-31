import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock


spec = importlib.util.spec_from_file_location('server_sync', Path(__file__).parents[1] / 'scripts/server-sync.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class ServerSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.manager = sync.Manager(root / 'project', root / 'vault', root / 'proc')
        self.manager.project.mkdir()
        self.manager.vault.mkdir()
        self.manager.proc_root.mkdir()
        self.manager.runtime.mkdir(parents=True)
        self.manager.log.write_text('Fully synced\n')
        self.data = {'pid': 123, 'start_ticks': '456', 'vault': str(self.manager.vault),
                     'mode': 'bidirectional', 'conflict_strategy': 'conflict', 'continuous': True,
                     'log': str(self.manager.log), 'remote_vault_id': 'test', 'log_offset': 13}

    def process(self, ticks='456', cwd=None, suffix=None):
        proc = self.manager.proc_root / '123'
        proc.mkdir(exist_ok=True)
        (proc / 'stat').write_text('123 (node name) S ' + ' '.join(['0'] * 18 + [ticks] + ['0'] * 4))
        argv = ['node', '--import', str(self.manager.project / 'scripts/obsidian-api-transport.mjs'),
                str(self.manager.cli)] + (suffix or ['sync', '--path', str(self.manager.vault), '--continuous'])
        (proc / 'cmdline').write_bytes(('\0'.join(argv) + '\0').encode())
        link = proc / 'cwd'
        if link.is_symlink():
            link.unlink()
        link.symlink_to(cwd or self.manager.project)
        return proc

    def record(self):
        self.manager.record.write_text(json.dumps(self.data))

    def test_old_log_does_not_prove_ready(self):
        self.process()
        self.record()
        self.assertFalse(self.manager.status()['ready'])
        with self.manager.log.open('a') as handle:
            handle.write('Fully synced\n')
        self.assertTrue(self.manager.status()['ready'])

    def test_missing_boundary_is_unknown(self):
        self.process()
        del self.data['log_offset']
        self.record()
        self.assertIsNone(self.manager.status()['ready'])

    def test_recent_error_clears_ready_and_is_reported(self):
        self.process()
        self.record()
        with self.manager.log.open('a') as handle:
            handle.write('Fully synced\nSync error: network\n')
        result = self.manager.status()
        self.assertFalse(result['ready'])
        self.assertEqual(result['last_error'], 'Sync error: network')

    def test_reused_pid_is_not_stopped_or_replaced(self):
        self.process(ticks='999')
        self.record()
        with patch.object(sync.os, 'kill') as kill, patch.object(sync.subprocess, 'Popen') as popen:
            self.assertEqual(self.manager.stop()['state'], 'stale')
            with self.assertRaises(sync.SyncError):
                self.manager.start()
        kill.assert_not_called()
        popen.assert_not_called()

    def test_wrong_cwd_and_wrong_command_are_not_stopped(self):
        for kwargs in ({'cwd': self.manager.vault}, {'suffix': ['sync', '--path', str(self.manager.vault)]}):
            with self.subTest(kwargs=kwargs):
                self.process(**kwargs)
                self.record()
                with patch.object(sync.os, 'kill') as kill:
                    self.assertFalse(self.manager.stop()['alive'])
                kill.assert_not_called()

    def test_untracked_matching_process_blocks_duplicate_start(self):
        self.process()
        self.assertEqual(self.manager.status()['state'], 'untracked')
        with patch.object(sync.subprocess, 'Popen') as popen:
            with self.assertRaises(sync.SyncError):
                self.manager.start()
        popen.assert_not_called()

    def test_unrelated_same_uid_process_never_requires_protected_cwd(self):
        proc = self.manager.proc_root / '1125'
        proc.mkdir()
        (proc / 'cmdline').write_bytes(b'other-protected-daemon\0')
        with patch.object(self.manager, 'process', side_effect=sync.SyncError('protected cwd')) as inspect:
            self.assertEqual(self.manager.matching_processes(), [])
        inspect.assert_not_called()

    def test_candidate_with_uninspectable_cwd_still_blocks_duplicate_start(self):
        self.process()
        with patch.object(self.manager, 'process', side_effect=sync.SyncError('protected candidate cwd')):
            with self.assertRaises(sync.SyncError):
                self.manager.matching_processes()

    def test_correct_instance_start_is_idempotent(self):
        self.process()
        self.record()
        with patch.object(sync.subprocess, 'run') as run, patch.object(sync.subprocess, 'Popen') as popen:
            self.assertTrue(self.manager.start()['alive'])
        run.assert_not_called()
        popen.assert_not_called()

    def test_corrupt_metadata_is_not_overwritten(self):
        self.manager.record.write_text('{broken')
        with patch.object(sync.subprocess, 'Popen') as popen:
            with self.assertRaises(sync.SyncError):
                self.manager.start()
        popen.assert_not_called()
        self.assertEqual(self.manager.record.read_text(), '{broken')

    def test_start_checks_configuration_and_records_log_boundary(self):
        config = {'vaultPath': str(self.manager.vault), 'syncMode': 'bidirectional',
                  'conflictStrategy': 'conflict', 'vaultId': 'test'}
        child = Mock(pid=123)
        child.poll.return_value = None

        def spawn(*args, **kwargs):
            self.process()
            return child

        with patch.object(sync.subprocess, 'run', return_value=Mock(returncode=0, stdout=json.dumps(config))) as run:
            with patch.object(sync.subprocess, 'Popen', side_effect=spawn) as popen:
                result = self.manager.start()
        self.assertTrue(result['alive'])
        self.assertFalse(result['ready'])
        self.assertEqual(self.manager.metadata()['log_offset'], 13)
        self.assertIn('sync-config', run.call_args_list[0].args[0])
        self.assertIn('sync-status', run.call_args_list[1].args[0])
        self.assertNotIn('--mode', run.call_args_list[0].args[0])
        self.assertTrue(popen.call_args.kwargs['start_new_session'])

    def test_unsafe_configuration_blocks_spawn(self):
        for result in (Mock(returncode=2, stdout=''), Mock(returncode=0, stdout='{"syncMode":"pull-only"}')):
            with self.subTest(result=result), patch.object(sync.subprocess, 'run', return_value=result):
                with patch.object(sync.subprocess, 'Popen') as popen:
                    with self.assertRaises(sync.SyncError):
                        self.manager.start()
                popen.assert_not_called()

    def test_incomplete_key_setup_blocks_spawn(self):
        config = {'vaultPath': str(self.manager.vault), 'syncMode': 'bidirectional',
                  'conflictStrategy': 'conflict', 'vaultId': 'test'}
        with patch.object(sync.subprocess, 'run', side_effect=[
                Mock(returncode=0, stdout=json.dumps(config)), Mock(returncode=2)]):
            with patch.object(sync.subprocess, 'Popen') as popen:
                with self.assertRaises(sync.SyncError):
                    self.manager.start()
        popen.assert_not_called()

    def test_filename_containing_ready_or_error_words_is_not_state(self):
        self.process()
        self.record()
        with self.manager.log.open('a') as handle:
            handle.write('Downloaded Fully synced.md\n')
        self.assertFalse(self.manager.status()['ready'])
        with self.manager.log.open('a') as handle:
            handle.write('Fully synced\nDownloaded My errors.md\n')
        result = self.manager.status()
        self.assertTrue(result['ready'])
        self.assertIsNone(result['last_error'])

    def test_native_log_new_transfer_and_connection_activity_clears_ready(self):
        self.process()
        self.record()
        activities = ['Uploading file notes/native edit.md', 'Uploading new-folder',
                      'Downloading notes/remote edit.md', 'Connecting...',
                      'Connection successful. Detecting changes...',
                      'Waiting to connect to server', 'Checking notes/hash.md',
                      'Deleting remote file old.md', 'Deleting old-folder',
                      'Restoring notes/missing.md', 'Reverting notes/native edit.md',
                      'Merging conflicted file notes/conflict.md',
                      'Renaming conflicted file notes/conflict.md',
                      'Removing local-only folder extra', 'Accepted notes/remote edit.md']
        for line in activities:
            with self.subTest(line=line):
                with self.manager.log.open('a') as handle:
                    handle.write('Fully synced\n' + line + '\n')
                result = self.manager.status()
                self.assertFalse(result['ready'])
                self.assertIsNone(result['last_error'])
                with self.manager.log.open('a') as handle:
                    handle.write('Fully synced\n')
                self.assertTrue(self.manager.status()['ready'])

    def test_filename_keywords_and_completed_file_messages_do_not_invent_state(self):
        inert = ['Downloaded Uploading file.md', 'Upload complete Downloading.md',
                 'Downloaded Connecting....md', 'Uploading.md', 'Downloading.md',
                 'Checking.md', 'Accepted.md', 'Watching.md',
                 'Downloaded notes/Fully synced.md', 'Downloaded notes/Sync error.md']
        for line in inert:
            with self.subTest(line=line):
                self.assertEqual(sync.Manager.readiness(['Fully synced', line]), (True, None))
                self.assertEqual(sync.Manager.readiness([line]), (False, None))

    def test_stop_signals_only_verified_process(self):
        proc = self.process()
        self.record()

        def stop(pid, sig):
            self.assertEqual(pid, 123)
            self.assertEqual(sig, sync.signal.SIGINT)
            (proc / 'stat').unlink()

        with patch.object(sync.os, 'kill', side_effect=stop) as kill:
            self.assertFalse(self.manager.stop()['alive'])
        kill.assert_called_once()

    def unit_output(self, *, pid=123, active='active', invocation='a' * 32):
        return '\n'.join(['LoadState=loaded', f'ActiveState={active}',
                          'SubState=running' if pid else 'SubState=dead',
                          f'MainPID={pid}', f'InvocationID={invocation}',
                          'UnitFileState=enabled', 'Result=success', 'NRestarts=2'])

    def journal_output(self, *messages, invocation='a' * 32):
        return '\n'.join(json.dumps({'_SYSTEMD_INVOCATION_ID': invocation, 'MESSAGE': message})
                         for message in messages)

    def test_temporary_manager_never_discovers_real_global_unit(self):
        with patch.object(sync.subprocess, 'run') as run:
            self.assertEqual(self.manager.status()['state'], 'stopped')
        run.assert_not_called()

    def test_systemd_status_uses_current_pid_and_invocation_not_old_record_or_log(self):
        self.manager.use_systemd = True
        self.process()
        self.manager.record.write_text('{obsolete detached record is intentionally invalid')
        self.manager.log.write_text('Fully synced\n')
        wrong = self.journal_output('Fully synced', invocation='b' * 32)
        current = self.journal_output('Starting sync')
        with patch.object(sync.subprocess, 'run', side_effect=[
                Mock(returncode=0, stdout=self.unit_output()),
                Mock(returncode=0, stdout=wrong + '\n' + current)]) as run:
            result = self.manager.status()
        self.assertTrue(result['alive'])
        self.assertIsNone(result['ready'])
        self.assertEqual(result['manager'], 'systemd')
        self.assertTrue(result['autostart'])
        self.assertIn('_SYSTEMD_INVOCATION_ID=' + 'a' * 32, run.call_args_list[1].args[0])

    def test_systemd_readiness_and_error_are_scoped_to_current_invocation(self):
        self.manager.use_systemd = True
        self.process()
        for messages, expected in [(('Fully synced',), True),
                                   (('Fully synced', 'Sync error: private detail'), False),
                                   (('Sync error: private detail', 'Fully synced'), True)]:
            with self.subTest(messages=messages):
                with patch.object(sync.subprocess, 'run', side_effect=[
                        Mock(returncode=0, stdout=self.unit_output()),
                        Mock(returncode=0, stdout=self.journal_output(*messages))]):
                    result = self.manager.status()
                self.assertEqual(result['ready'], expected)
                self.assertNotIn('private detail', json.dumps(result))
                if expected:
                    self.assertIsNone(result['last_error'])

    def test_systemd_new_activity_reports_not_ready_until_new_completion(self):
        self.manager.use_systemd = True
        self.process()
        for activity in ('Uploading file notes/edit.md', 'Downloading notes/new.md', 'Connecting...'):
            for finished in (False, True):
                with self.subTest(activity=activity, finished=finished):
                    messages = ['Fully synced', activity]
                    if finished:
                        messages.append('Fully synced')
                    with patch.object(sync.subprocess, 'run', side_effect=[
                            Mock(returncode=0, stdout=self.unit_output()),
                            Mock(returncode=0, stdout=self.journal_output(*messages))]):
                        result = self.manager.status()
                    self.assertEqual(result['ready'], finished)
                    self.assertIsNone(result['log_issue'])

    def test_manager_only_invocation_fields_cannot_mark_client_ready(self):
        self.manager.use_systemd = True
        self.process()
        manager_message = json.dumps({'USER_INVOCATION_ID': 'a' * 32, 'MESSAGE': 'Fully synced'})
        with patch.object(sync.subprocess, 'run', side_effect=[
                Mock(returncode=0, stdout=self.unit_output()),
                Mock(returncode=0, stdout=manager_message)]):
            result = self.manager.status()
        self.assertIsNone(result['ready'])

    def test_missing_unit_preserves_detached_mode(self):
        self.manager.use_systemd = True
        self.process()
        self.record()
        with patch.object(sync.subprocess, 'run', return_value=Mock(returncode=4, stdout='LoadState=not-found\n')):
            self.assertTrue(self.manager.status()['alive'])

    def test_systemd_query_failure_never_spawns_detached_replacement(self):
        self.manager.use_systemd = True
        with patch.object(sync.subprocess, 'run', return_value=Mock(returncode=1, stdout='')):
            with patch.object(sync.subprocess, 'Popen') as popen:
                with self.assertRaises(sync.SyncError):
                    self.manager.start()
        popen.assert_not_called()

    def test_systemd_refuses_wrong_pid_identity_and_never_signals_it(self):
        self.manager.use_systemd = True
        self.process(cwd=self.manager.vault)
        with patch.object(sync.subprocess, 'run', return_value=Mock(returncode=0, stdout=self.unit_output())) as run:
            with patch.object(sync.os, 'kill') as kill:
                self.assertEqual(self.manager.status()['state'], 'mismatch')
                with self.assertRaises(sync.SyncError):
                    self.manager.stop()
        self.assertTrue(all('show' in call.args[0] for call in run.call_args_list))
        kill.assert_not_called()

    def test_systemd_start_blocks_detached_process_before_handoff(self):
        self.manager.use_systemd = True
        self.process()
        inactive = self.unit_output(pid=0, active='inactive', invocation='')
        with patch.object(sync.subprocess, 'run', return_value=Mock(returncode=0, stdout=inactive)) as run:
            with self.assertRaises(sync.SyncError):
                self.manager.start()
        self.assertEqual(run.call_count, 1)

    def test_systemd_start_is_foreground_unit_action_without_popen(self):
        self.manager.use_systemd = True
        started = False

        def systemctl(command, **kwargs):
            nonlocal started
            if command[0] == 'journalctl':
                return Mock(returncode=0, stdout=self.journal_output('Fully synced'))
            if 'start' in command:
                self.process()
                started = True
                return Mock(returncode=0)
            return Mock(returncode=0, stdout=self.unit_output() if started else
                        self.unit_output(pid=0, active='inactive', invocation=''))

        with patch.object(sync.subprocess, 'run', side_effect=systemctl) as run:
            with patch.object(sync.subprocess, 'Popen') as popen:
                result = self.manager.start()
        self.assertTrue(result['ready'])
        self.assertTrue(any(call.args[0] == ['systemctl', '--user', 'start', sync.SYSTEMD_UNIT]
                            for call in run.call_args_list))
        popen.assert_not_called()

    def test_systemd_stop_controls_unit_without_direct_signal(self):
        self.manager.use_systemd = True
        proc = self.process()
        stopped = False

        def systemctl(command, **kwargs):
            nonlocal stopped
            if 'stop' in command:
                stopped = True
                (proc / 'stat').unlink()
                return Mock(returncode=0)
            return Mock(returncode=0, stdout=self.unit_output(pid=0, active='inactive', invocation='')
                        if stopped else self.unit_output())

        with patch.object(sync.subprocess, 'run', side_effect=systemctl):
            with patch.object(sync.os, 'kill') as kill:
                result = self.manager.stop()
        self.assertFalse(result['alive'])
        self.assertEqual(result['state'], 'inactive')
        kill.assert_not_called()

    def test_systemd_preflight_rejects_existing_detached_process(self):
        self.process()
        with patch.object(sync.subprocess, 'run') as run:
            with self.assertRaises(sync.SyncError):
                self.manager.check()
        run.assert_not_called()

    def test_systemd_preflight_checks_mode_and_reuses_existing_configuration(self):
        config = {'vaultPath': str(self.manager.vault), 'syncMode': 'bidirectional',
                  'conflictStrategy': 'conflict', 'vaultId': 'test'}
        self.record()
        with patch.object(sync.subprocess, 'run', return_value=Mock(returncode=0, stdout=json.dumps(config))) as run:
            self.assertEqual(self.manager.check()['state'], 'configuration-verified')
        self.assertEqual(run.call_count, 2)
        self.assertFalse(any('sync-setup' in call.args[0] for call in run.call_args_list))


if __name__ == '__main__':
    unittest.main()
