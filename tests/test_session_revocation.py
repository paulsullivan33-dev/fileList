"""Existing cookies must not keep removed account/group privileges."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask, session
import auth
from config_loader import Directory


class RevocationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.users = self.root / 'users.json'
        self.write_groups('members')
        with patch.dict(os.environ, {'FLASK_SECRET_KEY': 'revocation-test-key'}):
            self.app = Flask(__name__, root_path=str(self.root))
            self.app.config['TESTING'] = True
            auth.register(self.app, [Directory(str(self.root), '/files', 'Files', 'members')])
        @self.app.route('/operation', methods=['GET', 'POST'])
        def index():
            return {'groups': session.get('groups')}
        self.client = self.app.test_client()
        with self.client.session_transaction() as cookie:
            cookie['username'] = 'member'
            cookie['groups'] = ['members']

    def write_groups(self, groups):
        self.users.write_text(json.dumps([{'username': 'member', 'password': 'unused', 'groups': groups}]), encoding='utf-8')

    def test_removed_group_blocks_get_post_and_json_operations(self):
        self.write_groups('other')
        for method, kwargs in [('get', {'query_string': {'dir': 0}}),
                               ('post', {'data': {'dir': 0}}),
                               ('post', {'json': {'src_dir': 0}})]:
            with self.subTest(method=method, kwargs=kwargs):
                response = getattr(self.client, method)('/operation', **kwargs)
                self.assertEqual(response.status_code, 403)

    def test_removed_user_clears_existing_session_even_without_directory_parameters(self):
        self.users.write_text('[]', encoding='utf-8')
        response = self.client.get('/operation')
        self.assertEqual(response.status_code, 403)
        with self.client.session_transaction() as cookie:
            self.assertNotIn('username', cookie)

    def test_unreadable_or_malformed_user_configuration_fails_closed(self):
        self.users.write_text('{broken', encoding='utf-8')
        self.assertEqual(self.client.post('/operation', data={'dir': 0}).status_code, 403)

    def test_permissions_are_refreshed_for_routes_without_a_directory_parameter(self):
        self.write_groups('other')
        self.assertEqual(self.client.get('/operation').get_json()['groups'], ['other'])

    def test_unchanged_user_keeps_access(self):
        self.assertEqual(self.client.get('/operation', query_string={'dir': 0}).status_code, 200)
