import json
import unittest

from markdown_library.mcp_diagnostics import (
    AUTH_METHODS, CALLBACK_PATHS, CALLBACK_TYPES, MAX_CALLBACK_COUNT,
    MAX_REGISTRATION_BYTES, PREFIX, record_connection, registration_summary,
)


class ConnectionDiagnosticsTests(unittest.TestCase):
    SECRET = 'DO_NOT_RECORD_privateCredential_9fa21'

    def summary(self, data):
        return registration_summary(json.dumps(data).encode())

    def assert_safe_summary(self, summary):
        self.assertEqual(set(summary), {'auth_method', 'callback_types', 'callback_paths', 'callback_count'})
        self.assertIn(summary['auth_method'], AUTH_METHODS)
        self.assertTrue(set(summary['callback_types']).issubset(CALLBACK_TYPES))
        self.assertTrue(set(summary['callback_paths']).issubset(CALLBACK_PATHS))
        self.assertIs(type(summary['callback_count']), int)
        self.assertGreaterEqual(summary['callback_count'], 0)
        self.assertLessEqual(summary['callback_count'], MAX_CALLBACK_COUNT)
        self.assertNotIn(self.SECRET, json.dumps(summary))

    def test_callback_classes_only_never_uris_ports_ids_or_client_names(self):
        summary = self.summary({
            'client_name': self.SECRET,
            'client_secret': self.SECRET,
            'token_endpoint_auth_method': 'none',
            'redirect_uris': [
                'https://chatgpt.com/connector_platform_oauth_redirect',
                'https://chatgpt.com/connector/oauth/' + self.SECRET,
                'http://127.0.0.1:43129/callback',
                'http://[::1]:43130/callback/' + self.SECRET,
                'http://localhost:43131/oauth/callback?credential=' + self.SECRET,
                'https://example.invalid/' + self.SECRET + '#' + self.SECRET,
            ],
            'extra_unknown_field': {'private': self.SECRET},
        })
        self.assertEqual(summary, {
            'auth_method': 'none', 'callback_count': 6,
            'callback_types': ['loopback_hostname', 'loopback_v4', 'loopback_v6', 'other', 'web_chatgpt'],
            'callback_paths': ['callback', 'callback_id', 'oauth_callback', 'other', 'web_scoped', 'web_stable'],
        })
        self.assert_safe_summary(summary)
        for excluded in ('43129', '43130', '43131', '127.0.0.1', 'example.invalid', 'client_name'):
            self.assertNotIn(excluded, json.dumps(summary))

    def test_malicious_userinfo_query_fragment_and_host_do_not_leak(self):
        summary = self.summary({'token_endpoint_auth_method': self.SECRET, 'redirect_uris': [
            'https://' + self.SECRET + ':' + self.SECRET + '@chatgpt.com/callback?' + self.SECRET + '#' + self.SECRET,
            'http://' + self.SECRET + '@127.0.0.1:49152/callback/' + self.SECRET,
            'https://' + self.SECRET + '.invalid/oauth/callback',
            'https://chatgpt.com.' + self.SECRET + '/connector_platform_oauth_redirect',
            'http://localhost:' + self.SECRET + '/callback',
            'http://[::1/callback?' + self.SECRET,
            'http://127.0.0.1:999999/callback',
            'http://127.0.0.1:1234/callback\n' + self.SECRET,
        ]})
        self.assert_safe_summary(summary)
        self.assertEqual(summary['auth_method'], 'other')
        self.assertEqual(summary['callback_types'], ['other'])
        self.assertEqual(summary['callback_count'], 8)

    def test_invalid_json_oversized_and_non_bytes_are_fixed_invalid_summary(self):
        expected = {'auth_method': 'invalid', 'callback_types': [], 'callback_paths': [], 'callback_count': 0}
        for body in (b'', b'{', b'\xff', b'null', b'false', b'[]',
                     (self.SECRET * 1000).encode(), b'[' * 2000 + b']' * 2000,
                     self.SECRET, None, 12, bytearray(b'{}')):
            with self.subTest(body_type=type(body).__name__):
                result = registration_summary(body)
                self.assertEqual(result, expected)
                self.assert_safe_summary(result)
        self.assertEqual(registration_summary(b' ' * (MAX_REGISTRATION_BYTES + 1)), expected)

    def test_malformed_json_field_types_are_never_retained(self):
        for method in (None, False, 1, [], {'secret': self.SECRET}):
            for callbacks in (None, False, 12, self.SECRET, {'secret': self.SECRET}):
                result = self.summary({'token_endpoint_auth_method': method, 'redirect_uris': callbacks})
                self.assert_safe_summary(result)
                self.assertEqual(result['auth_method'], 'invalid')
                self.assertEqual(result['callback_count'], 0)
        for callback in (None, False, 12, [], {'secret': self.SECRET}):
            result = self.summary({'redirect_uris': [callback]})
            self.assert_safe_summary(result)
            self.assertEqual(result['callback_types'], ['other'])
            self.assertEqual(result['callback_paths'], ['other'])
        self.assertEqual(self.summary({})['auth_method'], 'omitted')

    def test_count_and_output_are_bounded(self):
        summary = self.summary({'redirect_uris': [None] * 101})
        self.assertEqual(summary['callback_count'], MAX_CALLBACK_COUNT)
        self.assert_safe_summary(summary)
        with self.assertLogs('markdown_library.mcp_diagnostics', level='WARNING') as logged:
            event = record_connection('register', 'post', 400, 'invalid_redirect_uri', summary)
        self.assertEqual(len(logged.records), 1)
        self.assertEqual(logged.records[0].levelname, 'WARNING')
        message = logged.records[0].getMessage()
        self.assertTrue(message.startswith(PREFIX + ' '))
        self.assertLess(len(message), 1024)
        self.assertEqual(json.loads(message.split(' ', 1)[1]), event)
        self.assertEqual(event['method'], 'POST')

    def test_arbitrary_event_strings_and_raw_registration_are_sanitized(self):
        with self.assertLogs('markdown_library.mcp_diagnostics', level='WARNING') as logged:
            event = record_connection('token?' + self.SECRET, 'GET\n' + self.SECRET,
                '400 ' + self.SECRET, 'invalid_request ' + self.SECRET, registration={
                    'client_name': self.SECRET, 'redirect_uris': ['https://' + self.SECRET + '/callback'],
                    'auth_method': self.SECRET, 'callback_count': self.SECRET,
                    'callback_types': [self.SECRET, [], {}, 'web_chatgpt'],
                    'callback_paths': [self.SECRET, {}, [], 'callback'],
                })
        self.assertEqual(event, {
            'stage': 'other', 'method': 'OTHER', 'status': 0, 'reason': 'unhandled',
            'registration': {'auth_method': 'invalid', 'callback_count': 0,
                             'callback_types': ['web_chatgpt'], 'callback_paths': ['callback']},
        })
        self.assertNotIn(self.SECRET, logged.records[0].getMessage())
        self.assertNotIn('redirect_uris', logged.records[0].getMessage())

    def test_sdk_http_status_enum_is_recorded_as_its_numeric_status(self):
        from http import HTTPStatus
        with self.assertLogs('markdown_library.mcp_diagnostics', level='WARNING'):
            event = record_connection('mcp', 'POST', HTTPStatus.OK)
        self.assertEqual(event['status'], 200)

    def test_known_reasons_and_optional_fields_and_malformed_registration(self):
        for reason in ('invalid_client', 'invalid_grant', 'invalid_target', 'invalid_token',
                       'request_boundary_host', 'request_boundary_origin', 'request_boundary_body', 'unhandled'):
            with self.assertLogs('markdown_library.mcp_diagnostics', level='WARNING'):
                event = record_connection('token', 'POST', 400, reason)
            self.assertEqual(event['reason'], reason)
            self.assertNotIn('registration', event)
        with self.assertLogs('markdown_library.mcp_diagnostics', level='WARNING'):
            event = record_connection('mcp', 'GET', 200)
        self.assertEqual(event, {'stage': 'mcp', 'method': 'GET', 'status': 200})
        for registration in (self.SECRET, [], 123, {'callback_count': True},
                             {'callback_count': -5}, {'callback_count': 99999999999999999}):
            with self.assertLogs('markdown_library.mcp_diagnostics', level='WARNING') as logged:
                event = record_connection([], {}, False, reason={}, registration=registration)
            self.assert_safe_summary(event['registration'])
            self.assertNotIn(self.SECRET, logged.records[0].getMessage())
            self.assertEqual(event['reason'], 'unhandled')


if __name__ == '__main__':
    unittest.main()
