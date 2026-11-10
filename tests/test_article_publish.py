from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from markdown_library import Library, LibraryError
from markdown_library.article_publish import publish_article, client_settings, REQUEST_SCHEMA
from markdown_library.core import _split
from markdown_library.publication_navigation import strip_publication_navigation as strip
from markdown_library.transport import sync_once
from test_transport import FakePayload


class ArticlePublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.library = Library.initialize(Path(self.tmp.name) / 'vault')
        self.note = self.library.create('# RealCorp interview\n\nRealCorp question.\n\n```cpp\nint main() {}\n```\n',
                                        title='Private interview', properties={'company': 'RealCorp', 'private_field': 'secret'})
        self.request = {'source_note_id': self.note['id'], 'expected_source_hash': self.note['sha256'],
                        'title': '2026-03-17 Example interview', 'published_at': '2026-03-17T12:00:00+08:00',
                        'link': 'interview-2026-03-17', 'categories': ['Career'],
                        'replacements': {'RealCorp': 'Example'}, 'forbidden_terms': ['RealCorp', 'secret']}
        self.client = FakePayload()

    def call(self, **changes):
        return publish_article(self.library, {**self.request, **changes}, client=self.client, apply=True)

    def files(self):
        return {str(p.relative_to(self.library.vault)): p.read_bytes()
                for p in self.library.vault.rglob('*') if p.is_file()}

    def test_preview_is_offline_readonly_and_checks_entire_public_projection(self):
        before = self.files()
        result = publish_article(self.library, self.request)
        self.assertEqual(before, self.files())
        self.assertEqual(result['status'], 'preview')
        self.assertIn('# 2026-03-17 Example interview', result['public_markdown'])
        self.assertNotIn('company', result['properties'])
        self.assertEqual(self.client.mutations, [])
        with self.assertRaises(LibraryError):
            publish_article(self.library, {**self.request, 'description': 'RealCorp secret'})

    def test_title_anonymization_handles_blank_lines_after_frontmatter(self):
        note = self.library.create('\n\n# RealCorp interview\n\nQuestion', title='Private')
        request = {**self.request, 'source_note_id': note['id'], 'expected_source_hash': note['sha256'],
                   'replacements': {}}
        preview = publish_article(self.library, request)
        self.assertEqual(preview['public_markdown'], '\n\n# ' + request['title'] + '\n\nQuestion')

    def test_one_call_publishes_verifies_keeps_publication_folder_and_links_only_target(self):
        unrelated = self.library.create('Other article', title='Other', kind='post', properties={'link': 'other'})
        self.library.publish(unrelated['id'], expected_hash=unrelated['sha256'])
        selected = deepcopy(self.library._read_state()['publications'][unrelated['id']])
        result = self.call()
        self.assertEqual(result['status'], 'published')
        self.assertTrue(result['api_verified'])
        self.assertEqual(result['frontend'], 'asynchronous')
        self.assertEqual(result['local_maintenance'], 'complete')
        source = self.library.get(self.note['id'])
        post = self.library.get(result['post_id'])
        self.assertEqual(strip(source['markdown']), self.note['markdown'])
        self.assertTrue(post['path'].startswith('发表稿/'))
        self.assertIn(post['path'].removesuffix('.md'), source['markdown'])
        self.assertIn('私人原稿', post['markdown'])
        self.assertEqual(selected, self.library._read_state()['publications'][unrelated['id']])
        self.assertEqual(len(self.client.latest['posts']), 1)
        remote = next(iter(self.client.base['posts'].values()))
        self.assertEqual(remote['_status'], 'published')
        self.assertNotIn('RealCorp', json.dumps(remote))
        self.assertIn('int main() {}', remote['markdown'])

    def test_same_request_retry_reuses_identity_without_remote_writes(self):
        first = self.call()
        mutations = len(self.client.mutations)
        again = self.call()
        self.assertEqual(again['post_id'], first['post_id'])
        self.assertEqual(again['status'], 'published')
        self.assertEqual(len(self.client.mutations), mutations)
        self.assertEqual(len(self.client.latest['posts']), 1)

    def test_legacy_archived_post_returns_to_publication_folder_without_republishing(self):
        from markdown_library.mcp_actions import LibraryActions
        first = self.call()
        post = self.library.get(first['post_id'])
        old_path = '90_归档/已发表文章/' + post['path'].rsplit('/', 1)[-1]
        with self.library._lock():
            state = self.library._load()
            notes = self.library._scan(state)
            actions = LibraryActions(self.library)
            writes, _, _ = actions._changes(state, notes, actions._files(notes), {post['path']: old_path})
            self.library._commit(state, writes, [(post['path'], post['sha256'])])
        mutations = len(self.client.mutations)
        result = self.call()
        current = self.library.get(result['post_id'])
        self.assertEqual(current['id'], post['id'])
        self.assertEqual(current['path'], post['path'])
        self.assertFalse(self.library._path(old_path).exists())
        self.assertIn(current['path'].removesuffix('.md'), self.library.get(self.note['id'])['markdown'])
        self.assertEqual(len(self.client.mutations), mutations)

    def test_existing_post_edit_requires_guard_and_retains_identity(self):
        first = self.call()
        post = self.library.get(first['post_id'])
        original = post['markdown']
        edited = self.library.replace(post['id'], original + '\nIndependent editing.\n', expected_hash=post['sha256'])
        with self.assertRaises(LibraryError):
            self.call()
        self.assertEqual(self.library.get(post['id'])['markdown'], edited['markdown'])
        result = self.call(expected_post_hash=edited['sha256'])
        self.assertEqual(result['post_id'], post['id'])
        self.assertEqual(len(self.client.latest['posts']), 1)

    def test_direct_post_title_update_preserves_published_body_date_url_and_private_edits(self):
        first = self.call()
        post = self.library.get(first['post_id'])
        private = self.library.get(self.note['id'])
        private = self.library.replace(private['id'], private['markdown'] + '\nPrivate new text', expected_hash=private['sha256'])
        update = {'post_id': post['id'], 'expected_post_hash': post['sha256'], 'title': 'New public title'}
        preview = publish_article(self.library, update)
        self.assertNotIn('Private new text', preview['public_markdown'])
        result = publish_article(self.library, update, client=self.client, apply=True)
        self.assertEqual(result['post_id'], first['post_id'])
        current = self.library.get(post['id'])
        self.assertEqual(current['properties']['publishedAt'], post['properties']['publishedAt'])
        self.assertEqual(current['properties']['link'], post['properties']['link'])
        expected_body = strip(_split(post['markdown'])[1]).replace('# ' + self.request['title'], '# New public title', 1)
        self.assertEqual(strip(_split(current['markdown'])[1]), expected_body)
        self.assertEqual(strip(self.library.get(private['id'])['markdown']), strip(private['markdown']))
        writes = len(self.client.mutations)
        self.assertEqual(publish_article(self.library, update, client=self.client, apply=True)['status'], 'published')
        self.assertEqual(len(self.client.mutations), writes)

    def test_direct_post_update_without_private_source_and_with_concurrent_edits(self):
        post = self.library.create('# Original\n\nPublic body', title='Original', kind='post', properties={'link': 'standalone'})
        update = {'post_id': post['id'], 'expected_post_hash': post['sha256'], 'title': 'Changed'}
        result = publish_article(self.library, update, client=self.client, apply=True)
        self.assertEqual(result['local_maintenance'], 'complete')
        current = self.library.get(post['id'])
        self.assertNotIn('私人原稿', current['markdown'])
        self.library.replace(current['id'], current['markdown'] + '\nIndependent draft', expected_hash=current['sha256'])
        writes = len(self.client.mutations)
        with self.assertRaises(LibraryError):
            publish_article(self.library, update, client=self.client, apply=True)
        self.assertEqual(len(self.client.mutations), writes)

    def test_source_concurrent_edit_is_not_overwritten_or_published(self):
        edited = self.library.replace(self.note['id'], self.note['markdown'] + 'New user edit', expected_hash=self.note['sha256'])
        with self.assertRaises(LibraryError):
            self.call()
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.library.get(self.note['id'])['sha256'], edited['sha256'])

    def test_selection_changed_before_sync_cannot_publish_a_different_draft(self):
        def concurrent_selection(library, client, **kwargs):
            post = next(n for n in library.list_notes() if n['properties']['kind'] == 'post')
            edited = library.replace(post['id'], post['markdown'] + '\nOther content', expected_hash=post['sha256'])
            library.publish(post['id'], expected_hash=edited['sha256'])
            return sync_once(library, client, **kwargs)
        with patch('markdown_library.article_publish.sync_once', side_effect=concurrent_selection):
            with self.assertRaises(LibraryError):
                self.call()
        self.assertEqual(self.client.mutations, [])

    def test_unknown_create_reconciles_without_duplicate(self):
        self.client.create_timeout = True
        first = self.call()
        self.assertEqual(first['status'], 'needs_review')
        self.assertFalse(first['api_verified'])
        again = self.call()
        self.assertEqual(again['post_id'], first['post_id'])
        self.assertEqual(sum(m[0] == 'create' for m in self.client.mutations), 1)

    def test_formal_readback_mismatch_is_not_reported_as_published(self):
        self.client.publish_mismatch = True
        result = self.call()
        self.assertEqual(result['status'], 'needs_review')
        self.assertFalse(result['api_verified'])

    def test_url_collision_does_not_create_another_post(self):
        self.library.create('Exists', title='Existing', kind='post', properties={'link': self.request['link']})
        with self.assertRaises(LibraryError):
            self.call()
        self.assertEqual(self.client.mutations, [])

    def test_deleted_post_identity_is_not_recreated(self):
        first = self.call()
        post = self.library.get(first['post_id'])
        self.library.delete(post['id'], expected_hash=post['sha256'])
        mutations = len(self.client.mutations)
        with self.assertRaises(LibraryError):
            self.call()
        self.assertEqual(len(self.client.mutations), mutations)

    def test_preview_of_existing_changes_returns_guard_without_changing_post(self):
        first = self.call()
        post = self.library.get(first['post_id'])
        before = self.files()
        result = publish_article(self.library, {**self.request, 'public_markdown': '# Public\nDifferent body'})
        self.assertTrue(result['requires_post_hash'])
        self.assertEqual(result['expected_post_hash'], post['sha256'])
        self.assertEqual(before, self.files())

    def test_preserves_existing_post_fields_and_anonymizes_all_public_metadata(self):
        first = self.call()
        post = self.library.get(first['post_id'])
        from markdown_library.core import _render
        props = {**post['properties'], 'frontmatter': {'subtitle': 'Keep my subtitle'}}
        edited = self.library.replace(post['id'], _render(props, _split(post['markdown'])[1]), expected_hash=post['sha256'])
        result = self.call(expected_post_hash=edited['sha256'])
        self.assertEqual(self.library.get(result['post_id'])['properties']['frontmatter'], props['frontmatter'])
        props['description'] = 'RealCorp'
        current = self.library.get(post['id'])
        self.library.replace(post['id'], _render(props, _split(current['markdown'])[1]), expected_hash=current['sha256'])
        with self.assertRaises(LibraryError):
            self.call()

    def test_legacy_source_relation_is_reused(self):
        post = self.library.import_note('Legacy draft', source_id='older-copy', title='Legacy', kind='post',
                                        source={'private_source_id': self.note['id']})
        result = self.call(expected_post_hash=post['sha256'])
        self.assertEqual(result['post_id'], post['id'])

    def test_normalized_remote_date_and_conflicting_admin_edit(self):
        create = self.client.create
        def normalized_create(collection, fields):
            fields = deepcopy(fields)
            if fields.get('publishedAt'):
                fields['publishedAt'] = '2026-03-17T04:00:00.000Z'
            return create(collection, fields)
        self.client.create = normalized_create
        first = self.call()
        remote = self.client.latest['posts'][str(first['remote_id'])]
        self.assertEqual(remote['publishedAt'], '2026-03-17T04:00:00.000Z')
        # An independent Admin draft is preserved, not overwritten by retry.
        remote['markdown'] += '\nAdmin edit'
        remote['updatedAt'] = self.client.stamp()
        writes = len(self.client.mutations)
        result = self.call()
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(len(self.client.mutations), writes)

    def test_cli_exposes_schema_and_runs_offline_preview_with_one_json_request(self):
        from markdown_library.cli import main
        output = StringIO()
        with redirect_stdout(output):
            status = main(['--vault', '/nonexistent-unused', 'publish-article', '--schema'])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), REQUEST_SCHEMA)
        path = Path(self.tmp.name) / 'request.json'
        path.write_text(json.dumps(self.request))
        output = StringIO()
        before = self.files()
        with redirect_stdout(output):
            status = main(['--vault', str(self.library.vault), 'publish-article', '--request', str(path)])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'preview')
        self.assertEqual(before, self.files())
        errors = StringIO()
        with redirect_stderr(errors), patch('markdown_library.article_publish.client_settings', return_value=(None, None)):
            status = main(['--vault', str(self.library.vault), 'publish-article', '--apply', '--request', str(path)])
        self.assertNotEqual(status, 0)
        self.assertEqual(before, self.files())

    def test_cli_apply_uses_fixed_configuration_and_returns_verified_result(self):
        from markdown_library.cli import main
        request_file = Path(self.tmp.name) / 'request.json'
        request_file.write_text(json.dumps(self.request))
        config = Path(self.tmp.name) / 'client.env'
        config.write_text('HOMEPAGE_URL=https://isolated.example\nHOMEPAGE_API_KEY=test-private-key\n')
        config.chmod(0o600)
        output = StringIO()
        with redirect_stdout(output), patch('markdown_library.transport.PayloadClient', return_value=self.client) as factory:
            status = main(['--vault', str(self.library.vault), 'publish-article', '--request', str(request_file),
                           '--apply', '--client-env-file', str(config)])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'published')
        self.assertNotIn('test-private-key', output.getvalue())
        factory.assert_called_once_with(self.client.target, 'test-private-key', 20, allow_loopback_http=False)

    def test_cli_returns_nonzero_for_unverified_publication(self):
        from markdown_library.cli import main
        request_file = Path(self.tmp.name) / 'request.json'
        request_file.write_text(json.dumps(self.request))
        self.client.publish_mismatch = True
        with redirect_stdout(StringIO()) as output, patch('markdown_library.article_publish.client_settings',
                return_value=(self.client.target, 'test-key')), patch('markdown_library.transport.PayloadClient', return_value=self.client):
            status = main(['--vault', str(self.library.vault), 'publish-article', '--request', str(request_file), '--apply'])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue())['status'], 'needs_review')

    def test_missing_unrelated_note_does_not_break_archival(self):
        absent = self.library.create('Missing unrelated', title='Missing')
        self.library._path(absent['path']).unlink()
        self.assertEqual(self.call()['local_maintenance'], 'complete')

    def test_maintenance_error_does_not_hide_verified_remote_success(self):
        with patch('markdown_library.article_publish._finish', side_effect=LibraryError('local conflict')):
            result = self.call()
        self.assertEqual(result['status'], 'published')
        self.assertTrue(result['api_verified'])
        self.assertEqual(result['local_maintenance'], 'needs_review')
        self.assertEqual(self.call()['local_maintenance'], 'complete')

    def test_schema_rejects_unknown_fields_ambiguous_dates_and_protected_posts(self):
        for changes in ({'extra': True}, {'published_at': '2026-03-17'}, {'expected_source_hash': '../bad'},
                        {'title': 'two\nlines'}, {'forbidden_terms': ['']}, {'replacements': {'': 'X'}}):
            with self.subTest(changes=changes), self.assertRaises(LibraryError):
                publish_article(self.library, {**self.request, **changes})
        self.assertFalse(REQUEST_SCHEMA['additionalProperties'])

    def test_credentials_are_explicit_private_data_and_never_execute_shell(self):
        path = Path(self.tmp.name) / 'client.env'
        path.write_text("HOMEPAGE_URL=https://isolated.example\nHOMEPAGE_API_KEY='literal$(touch /tmp/do-not-execute)'\n")
        path.chmod(0o600)
        origin, key = client_settings(path)
        self.assertEqual(origin, self.client.target)
        self.assertIn('$(touch', key)
        path.chmod(0o644)
        with self.assertRaises(LibraryError):
            client_settings(path)


if __name__ == '__main__':
    unittest.main()
