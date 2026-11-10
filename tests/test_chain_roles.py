import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from werkzeug.security import generate_password_hash

from chain_auth import SessionAccess, digest
from chain_coordinator import Coordinator, register


class RoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.public_hash = generate_password_hash('23456')
        cls.tactician_hash = generate_password_hash('12345')
        cls.rotated_hash = generate_password_hash('34567')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'queue.db'
        def connect():
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            return db
        self.store = Coordinator(connect, lambda: ('10', '20'))
        self.store.initialize()
        self.store.observe('friendly', {'observed_at': 100, 'members': [{'id': '1', 'name': 'A'}, {'id': '2', 'name': 'B'}]})
        self.store.observe('enemies', {'observed_at': 100, 'members': [{'id': str(i), 'name': 'Target'} for i in range(3, 7)]})
        self.app = self.new_app()
        self.public, self.public_csrf = self.login(self.app, '23456')
        self.tactician, self.tactician_csrf = self.login(self.app, '12345')
        self.cookie_name = 'chain_' + digest('10|20')[:12]

    def new_app(self, **config):
        app = Flask(__name__)
        app.config.update(TESTING=True, PUBLIC_PIN_HASH=self.public_hash,
                          TACTICIAN_PIN_HASH=self.tactician_hash, CHAIN_COOKIE_SECURE=True)
        app.config.update(config)
        register(app, self.store)
        return app

    def login(self, app, pin):
        client = app.test_client()
        csrf = client.get('/api/chain/state').json['access']['csrf']
        response = client.post('/api/chain/unlock', json={'pin': pin}, headers={'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 200, response.json)
        return client, client.get('/api/chain/state').json['access']['csrf']

    def send(self, client, csrf, path, data=None, method='POST'):
        return client.open('/api/chain/'+path, method=method,
                           json={'version': self.store.snapshot()['version'], **(data or {})},
                           headers={'X-CSRF-Token': csrf})

    def lock_board(self, locked):
        response = self.send(self.tactician, self.tactician_csrf, 'board-lock', {'locked': locked})
        self.assertEqual(response.status_code, 200, response.json)
        return response

    def add(self, client, csrf, target='3'):
        response = self.send(client, csrf, 'assignments', {'attacker_id': '1', 'target_id': target})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json['active'][-1]['id']

    def exercise_edits(self, client, csrf):
        a, b = self.add(client, csrf), self.add(client, csrf, '4')
        changes = [
            (f'assignments/{a}', {'attacker_id': '2', 'target_id': '5'}, 'PATCH'),
            ('reorder', {'ids': [b, a]}, 'POST'),
            ('skip', {'assignment_id': b}, 'POST'),
            ('restore', {'assignment_id': b}, 'POST'),
            ('advance', {'assignment_id': a}, 'POST'),
            (f'assignments/{b}', {}, 'DELETE'),
        ]
        for path, data, method in changes:
            with self.subTest(path=path):
                response = self.send(client, csrf, path, data, method)
                self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(self.store.snapshot()['active'])
        self.assertEqual(len(self.store.snapshot()['completed']), 1)

    def test_public_edits_unlocked(self):
        self.assertEqual(self.public.get('/api/chain/state').json['access']['role'], 'public')
        self.exercise_edits(self.public, self.public_csrf)

    def test_tactician_edits_locked(self):
        self.lock_board(True)
        self.exercise_edits(self.tactician, self.tactician_csrf)

    def test_tactician_edits_unlocked(self):
        self.exercise_edits(self.tactician, self.tactician_csrf)

    def test_all_public_mutations_denied_while_locked(self):
        a, b = self.add(self.public, self.public_csrf), self.add(self.public, self.public_csrf, '4')
        self.send(self.public, self.public_csrf, 'skip', {'assignment_id': a})
        self.lock_board(True)
        before = self.store.snapshot()
        changes = [
            ('assignments', {'attacker_id': '2', 'target_id': '6'}, 'POST'),
            (f'assignments/{b}', {'attacker_id': '2', 'target_id': '5'}, 'PATCH'),
            (f'assignments/{b}', {}, 'DELETE'),
            ('reorder', {'ids': [b]}, 'POST'),
            ('skip', {'assignment_id': b}, 'POST'),
            ('advance', {'assignment_id': b}, 'POST'),
            ('restore', {'assignment_id': a}, 'POST'),
        ]
        for path, data, method in changes:
            with self.subTest(path=path):
                self.assertEqual(self.send(self.public, self.public_csrf, path, data, method).status_code, 403)
        after = self.store.snapshot()
        for key in ('version', 'active', 'deferred', 'completed'):
            self.assertEqual(after[key], before[key])

    def test_public_cannot_change_lock_even_with_forged_role(self):
        for locked in (True, False):
            response = self.send(self.public, self.public_csrf, 'board-lock', {'locked': locked, 'role': 'tactician', 'editing': True})
            self.assertEqual(response.status_code, 403)
        self.assertFalse(self.store.snapshot()['locked'])

    def test_same_public_session_regains_editing_and_touch_works_while_locked(self):
        cookie = self.public.get_cookie(self.cookie_name).value
        self.lock_board(True)
        state = self.public.get('/api/chain/state').json
        self.assertEqual(state['access']['role'], 'public')
        self.assertFalse(state['access']['editing'])
        self.assertEqual(self.send(self.public, self.public_csrf, 'touch').status_code, 200)
        self.lock_board(False)
        self.assertTrue(self.public.get('/api/chain/state').json['access']['editing'])
        self.assertEqual(self.public.get_cookie(self.cookie_name).value, cookie)
        self.add(self.public, self.public_csrf)

    def test_other_tactician_can_unlock_and_signout_does_not_unlock_board(self):
        other, csrf = self.login(self.app, '12345')
        self.lock_board(True)
        self.assertEqual(self.send(self.tactician, self.tactician_csrf, 'lock').status_code, 200)
        self.assertTrue(self.store.snapshot()['locked'])
        self.assertEqual(self.send(other, csrf, 'board-lock', {'locked': False}).status_code, 200)

    def test_lock_sse_is_public_and_has_no_session_or_verifier_information(self):
        response = self.public.get('/api/chain/events', buffered=False)
        self.assertIn(b'"locked": 0', next(response.response))
        self.lock_board(True)
        event = next(response.response)
        self.assertIn(b'"locked": 1', event)
        for value in (self.public_csrf, self.public_hash, self.tactician_hash, 'fingerprint', 'credential_generation'):
            self.assertNotIn(value.encode(), event)
        self.lock_board(False)
        self.assertIn(b'"locked": 0', next(response.response))
        response.close()

    def copy_session(self, app, client):
        result = app.test_client()
        result.set_cookie(self.cookie_name, client.get_cookie(self.cookie_name).value)
        return result

    def test_public_rotation_after_restart_preserves_tacticians_and_lock(self):
        self.lock_board(True)
        restarted = self.new_app(PUBLIC_PIN_HASH=self.rotated_hash)
        public = self.copy_session(restarted, self.public)
        tactician = self.copy_session(restarted, self.tactician)
        self.assertEqual(self.send(public, self.public_csrf, 'touch').status_code, 401)
        self.assertEqual(self.send(tactician, self.tactician_csrf, 'touch').status_code, 200)
        self.assertTrue(self.store.snapshot()['locked'])
        # An old process cannot authenticate old public credentials after rotation.
        self.assertEqual(self.send(self.public, self.public_csrf, 'touch').status_code, 401)
        self.assertEqual(self.send(self.tactician, self.tactician_csrf, 'touch').status_code, 200)
        replacement, csrf = self.login(restarted, '34567')
        self.assertEqual(replacement.get('/api/chain/state').json['access']['role'], 'public')
        self.assertEqual(self.send(tactician, self.tactician_csrf, 'board-lock', {'locked': False}).status_code, 200)
        self.add(replacement, csrf)
        # Restoring the previous hash never resurrects an old token.
        restored = self.new_app()
        old = self.copy_session(restored, self.public)
        self.assertEqual(self.send(old, self.public_csrf, 'touch').status_code, 401)

    def test_tactician_rotation_only_invalidates_tacticians(self):
        restarted = self.new_app(TACTICIAN_PIN_HASH=self.rotated_hash)
        public = self.copy_session(restarted, self.public)
        tactician = self.copy_session(restarted, self.tactician)
        self.assertEqual(self.send(tactician, self.tactician_csrf, 'board-lock', {'locked': True}).status_code, 401)
        self.add(public, self.public_csrf)
        new, csrf = self.login(restarted, '34567')
        self.assertEqual(new.get('/api/chain/state').json['access']['role'], 'tactician')
        self.assertEqual(self.send(new, csrf, 'board-lock', {'locked': True}).status_code, 200)

    def test_unchanged_restart_retains_sessions_and_role_disable_revokes_only_that_role(self):
        version = self.store.snapshot()['auth_revision']
        restarted = self.new_app()
        self.assertEqual(self.store.snapshot()['auth_revision'], version)
        self.assertEqual(self.send(self.copy_session(restarted, self.public), self.public_csrf, 'touch').status_code, 200)
        disabled = self.new_app(PUBLIC_PIN_HASH='')
        self.assertGreater(self.store.snapshot()['auth_revision'], version)
        self.assertEqual(self.send(self.copy_session(disabled, self.public), self.public_csrf, 'touch').status_code, 401)
        self.assertEqual(self.send(self.copy_session(disabled, self.tactician), self.tactician_csrf, 'touch').status_code, 200)

    def test_expiry_csrf_and_stale_board_lock(self):
        self.assertEqual(self.send(self.tactician, 'wrong', 'board-lock', {'locked': True}).status_code, 403)
        self.assertEqual(self.send(self.public, 'wrong', 'assignments', {'attacker_id': '1', 'target_id': '3'}).status_code, 403)
        old_version = self.store.snapshot()['version']
        self.lock_board(True)
        self.assertEqual(self.send(self.tactician, self.tactician_csrf, 'board-lock', {'locked': False, 'version': old_version}).status_code, 409)
        for client, csrf in ((self.public, self.public_csrf), (self.tactician, self.tactician_csrf)):
            expiry = client.get('/api/chain/state').json['access']['expires_at']
            with patch('chain_auth.time.time', return_value=expiry+1):
                self.assertEqual(self.send(client, csrf, 'touch').status_code, 401)
                self.assertEqual(self.send(client, csrf, 'board-lock', {'locked': False}).status_code, 401)

    def test_no_role_can_manage_pins_via_api(self):
        for client, csrf in ((self.public, self.public_csrf), (self.tactician, self.tactician_csrf)):
            for path in ('pin', 'pins', 'change-pin', 'rotate-pin'):
                self.assertEqual(self.send(client, csrf, path, {'PUBLIC_PIN_HASH': 'fake'}).status_code, 404)
        self.assertEqual(self.app.config['PUBLIC_PIN_HASH'], self.public_hash)

    def test_wrong_matchup_does_not_rotate_saved_role_configuration(self):
        before = self.store.snapshot()['auth_revision']
        with patch.object(self.store, 'factions', return_value=('10', '99')), self.assertLogs(level='ERROR'):
            app = self.new_app(PUBLIC_PIN_HASH=self.rotated_hash)
            self.assertEqual(app.test_client().get('/api/chain/state').status_code, 409)
        self.assertEqual(self.store.snapshot()['auth_revision'], before)
        self.assertEqual(self.send(self.public, self.public_csrf, 'touch').status_code, 200)

    def test_identical_pins_cannot_implicitly_grant_tactician_role(self):
        app = self.new_app(PUBLIC_PIN_HASH=generate_password_hash('12345'))
        client = app.test_client()
        csrf = client.get('/api/chain/state').json['access']['csrf']
        self.assertEqual(client.post('/api/chain/unlock', json={'pin': '12345'}, headers={'X-CSRF-Token': csrf}).status_code, 503)

    def test_lock_committing_first_denies_waiting_public_edit(self):
        entered, release, started = threading.Event(), threading.Event(), threading.Event()
        original = SessionAccess.authorize
        def paused(auth, db, raw, csrf, permission='session'):
            result = original(auth, db, raw, csrf, permission)
            if permission == 'tactician':
                self.assertTrue(db.in_transaction)
                entered.set()
                if not release.wait(5): raise AssertionError('Timed out waiting to commit board lock')
            return result
        version = self.store.snapshot()['version']
        def locking():
            return self.tactician.post('/api/chain/board-lock', json={'version': version, 'locked': True}, headers={'X-CSRF-Token': self.tactician_csrf}).status_code
        def editing():
            started.set()
            return self.public.post('/api/chain/assignments', json={'version': version, 'attacker_id': '1', 'target_id': '3'}, headers={'X-CSRF-Token': self.public_csrf}).status_code
        with patch.object(SessionAccess, 'authorize', paused), ThreadPoolExecutor(max_workers=2) as pool:
            lock = pool.submit(locking)
            self.assertTrue(entered.wait(5))
            edit = pool.submit(editing)
            self.assertTrue(started.wait(5))
            release.set()
            self.assertEqual(lock.result(timeout=10), 200)
            self.assertEqual(edit.result(timeout=10), 403)
        self.assertFalse(self.store.snapshot()['active'])

    def test_edit_transaction_finishes_before_waiting_lock_can_commit(self):
        entered, release, started = threading.Event(), threading.Event(), threading.Event()
        original = SessionAccess.authorize
        def paused(auth, db, raw, csrf, permission='session'):
            result = original(auth, db, raw, csrf, permission)
            if permission == 'edit':
                self.assertTrue(db.in_transaction)
                entered.set()
                if not release.wait(5): raise AssertionError('Timed out waiting to commit edit')
            return result
        version = self.store.snapshot()['version']
        def editing():
            return self.public.post('/api/chain/assignments', json={'version': version, 'attacker_id': '1', 'target_id': '3'}, headers={'X-CSRF-Token': self.public_csrf}).status_code
        def locking():
            started.set()
            return self.tactician.post('/api/chain/board-lock', json={'version': version, 'locked': True}, headers={'X-CSRF-Token': self.tactician_csrf}).status_code
        with patch.object(SessionAccess, 'authorize', paused), ThreadPoolExecutor(max_workers=2) as pool:
            edit = pool.submit(editing)
            self.assertTrue(entered.wait(5))
            lock = pool.submit(locking)
            self.assertTrue(started.wait(5))
            release.set()
            self.assertEqual(edit.result(timeout=10), 200)
            self.assertEqual(lock.result(timeout=10), 409)
        self.lock_board(True)
        with self.store.db() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT event_type FROM chain_events ORDER BY id')], ['add', 'board_locked'])


class MigrationTests(unittest.TestCase):
    def test_legacy_queue_and_history_survive_role_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'legacy.db'
            def connect():
                db = sqlite3.connect(path)
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                return db
            with connect() as db:
                db.executescript('''
                    CREATE TABLE chain_queue(id INTEGER PRIMARY KEY CHECK(id=1),faction_id TEXT NOT NULL,
                        enemy_faction_id TEXT NOT NULL,version INTEGER NOT NULL,revision INTEGER NOT NULL,updated_at REAL NOT NULL);
                    CREATE TABLE chain_sessions(token TEXT PRIMARY KEY,csrf TEXT NOT NULL,editing INTEGER NOT NULL,expires_at REAL NOT NULL);
                    INSERT INTO chain_queue VALUES(1,'10','20',7,9,100);
                    INSERT INTO chain_sessions VALUES('legacy-token','legacy-csrf',1,9999999999);
                ''')
            store = Coordinator(connect, lambda: ('10', '20'))
            store.initialize()
            with store.db(write=True) as db:
                db.execute('''INSERT INTO chain_assignments(attacker_id,attacker_name,target_id,target_name,position,created_at,updated_at,eligible_since)
                    VALUES('1','Attacker','3','Target',1,100,100,100)''')
                store.event(db, 'add', 1)
                # Recreate only the legacy authentication tables to exercise an
                # upgrade while real queue rows/events already exist.
                db.execute('DROP TABLE chain_sessions')
                db.execute('CREATE TABLE chain_sessions(token TEXT PRIMARY KEY,csrf TEXT NOT NULL,editing INTEGER NOT NULL,expires_at REAL NOT NULL)')
                db.execute("INSERT INTO chain_sessions VALUES('legacy-token','legacy-csrf',1,9999999999)")
            store.initialize()
            snapshot = store.snapshot()
            self.assertEqual(snapshot['version'], 7)
            self.assertEqual(snapshot['revision'], 9)
            self.assertFalse(snapshot['locked'])
            self.assertEqual(snapshot['active'][0]['target_name'], 'Target')
            with store.db() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM chain_sessions').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM chain_events').fetchone()[0], 1)
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            store.initialize()
            self.assertEqual(store.snapshot()['active'], snapshot['active'])
