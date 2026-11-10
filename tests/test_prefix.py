import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_connect = sqlite3.connect
with patch('sqlite3.connect', side_effect=lambda *args, **kwargs: _connect(':memory:')):
    app = importlib.import_module('app')


class PrefixTests(unittest.TestCase):
    @patch.object(app, "ROOT_URL_HOSTS", {"legacy.example.com", "www.legacy.example.com"})
    def test_pages_links_and_api_urls(self):
        pages = {'/': '/api/status', '/hospital': '/api/status',
                 '/friendly': '/api/friendly/status',
                 '/friendly/hospital': '/api/friendly/status', '/activity': '/api/activity'}
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'ACTIVITY_DB_PATH', str(Path(tmp) / 'test.db')):
            app.init_activity_db()
            client = app.app.test_client()
            for prefix in ('', '/instance-a', '/instance-b'):
                for host in ('tracker.example.com', 'localhost', 'legacy.example.com', 'www.legacy.example.com',
                             f'192.0.2.10:{app.PORT}', f'localhost:{app.PORT}',
                             f'127.0.0.1:{app.PORT}', '192.0.2.10:8080'):
                    expected = '' if host in app.ROOT_URL_HOSTS or host.endswith(':' + str(app.PORT)) else prefix
                    with patch.object(app, 'URL_PREFIX', prefix):
                        for page, api in pages.items():
                            with self.subTest(prefix=prefix, host=host, page=page):
                                response = client.get(page, headers={'Host': host})
                                self.assertEqual(response.status_code, 200)
                                html = response.get_data(as_text=True)
                                for href in re.findall(r'href="([^"]+)"', html):
                                    if href.startswith('${'):  # External Torn profiles.
                                        continue
                                    self.assertTrue(href.startswith(expected + '/'), href)
                                    internal = href[len(expected):]
                                    with client.get(internal) as linked:
                                        self.assertEqual(linked.status_code, 200, href)
                                self.assertIn(json.dumps(expected + api), html)
                                self.assertEqual(client.get(api).status_code, 200)
                                if page == '/activity':
                                    self.assertIn('+ `?${params.toString()}`', html)
                                    self.assertIn("fetch(url, {cache:'no-store'})", html)
                                if expected:
                                    self.assertNotIn('href="/static/', html)

    def test_independent_environment_files_and_databases(self):
        root = Path(app.__file__).parent
        with tempfile.TemporaryDirectory() as tmp:
            env = {key: value for key, value in os.environ.items() if key not in (
                'TORN_API_KEY', 'ENEMY_FACTION_ID', 'FRIENDLY_FACTION_ID', 'PORT',
                'URL_PREFIX', 'ACTIVITY_DB_PATH', 'CHAIN_DB_PATH', 'ENV_FILE')}
            paths = []
            for name, port, faction in [('instance-a', 8787, '111'), ('instance-b', 8788, '222')]:
                db = Path(tmp) / (name + '.db')
                paths.append(db)
                config = Path(tmp) / (name + '.env')
                config.write_text(f'TORN_API_KEY=test\nENEMY_FACTION_ID={faction}\nFRIENDLY_FACTION_ID=333\nPORT={port}\nURL_PREFIX=/{name}\nACTIVITY_DB_PATH={db}\n')
                result = subprocess.run([sys.executable, '-c',
                    'import app,json; print(json.dumps([app.PORT,app.URL_PREFIX,app.FACTION_ID,app.ACTIVITY_DB_PATH]))'],
                    cwd=root, env={**env, 'ENV_FILE': str(config)}, capture_output=True, text=True, check=True)
                self.assertEqual(json.loads(result.stdout), [port, '/' + name, faction, str(db)])
            for db in paths:
                self.assertTrue(db.exists())
                with sqlite3.connect(db) as connection:
                    self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            result = subprocess.run([sys.executable, '-c', 'import app'], cwd=root,
                env={**env, 'ENV_FILE': str(Path(tmp) / 'missing.env')}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Environment file does not exist', result.stderr)

    def test_separate_coordinator_database_on_restart_with_new_enemy(self):
        root = Path(app.__file__).parent
        env = {k: v for k, v in os.environ.items() if k not in (
            'TORN_API_KEY', 'ENEMY_FACTION_ID', 'FRIENDLY_FACTION_ID',
            'ACTIVITY_DB_PATH', 'CHAIN_DB_PATH', 'ENV_FILE')}
        with tempfile.TemporaryDirectory() as tmp:
            activity = Path(tmp)/'activity.db'
            chain = Path(tmp)/'new-chain.db'
            config = Path(tmp)/'instance.env'
            common = f'TORN_API_KEY=test\nFRIENDLY_FACTION_ID=333\nACTIVITY_DB_PATH={activity}\n'
            for enemy, override in [('111', ''), ('222', f'CHAIN_DB_PATH={chain}\n')]:
                config.write_text(common+f'ENEMY_FACTION_ID={enemy}\n'+override)
                result = subprocess.run([sys.executable, '-c',
                    'import app,json; r=app.app.test_client().get("/api/chain/state"); '
                    'print(json.dumps([r.status_code,r.json["enemy_faction_id"],app.CHAIN_DB_PATH]))'],
                    cwd=root, env={**env, 'ENV_FILE':str(config)}, capture_output=True, text=True, check=True)
                self.assertEqual(json.loads(result.stdout), [200,enemy,str(chain) if override else ''])
            with sqlite3.connect(activity) as db:
                self.assertEqual(db.execute('SELECT enemy_faction_id FROM chain_queue').fetchone()[0], '111')
            with sqlite3.connect(chain) as db:
                self.assertEqual(db.execute('SELECT enemy_faction_id FROM chain_queue').fetchone()[0], '222')
                self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
