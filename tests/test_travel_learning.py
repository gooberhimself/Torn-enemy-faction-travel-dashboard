import importlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import travel_learning as learning

# Import Flask without touching the user's live database or starting polling.
_connect = sqlite3.connect
with patch('sqlite3.connect', side_effect=lambda *args, **kwargs: _connect(':memory:')):
    app = importlib.import_module('app')


class TravelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path_patch = patch.object(app, 'ACTIVITY_DB_PATH', str(Path(self.temp.name) / 'activity.db'))
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        app.init_activity_db()
        self.now = 1000000
        self.runtime = app.new_runtime_state()

    def poll(self, state='Okay', destination='', step=60, **extra):
        self.now += step
        description = {'Traveling': f'Traveling to {destination}',
                       'Returning': f'Returning from {destination}',
                       'Abroad': f'In {destination}',
                       'Hospital': f'In a {destination} hospital'}.get(state, state)
        member = {'name': 'Test', 'status': {'state': state, 'description': description}, **extra}
        with patch.object(app.time, 'time', return_value=self.now):
            app.update_runtime_state(self.runtime, {'members': {'123': member}}, track_activity=True)
        return self.runtime['members'][0]

    def rows(self):
        with app.activity_db() as db:
            return db.execute('SELECT * FROM travel_observations ORDER BY id').fetchall()

    def trip(self, method, destination='South Africa', direction='Traveling', gap=False):
        self.poll('Okay' if direction == 'Traveling' else 'Abroad', '' if direction == 'Traveling' else destination)
        start = self.poll(direction, destination, travel={'plane': 'preserved'})
        end = self.now + learning.TRAVEL_TIMES[destination][method]
        while self.now + 60 < end:
            self.poll(direction, destination, step=120 if gap else 60)
        landed = self.poll('Abroad' if direction == 'Traveling' else 'Okay', destination if direction == 'Traveling' else '', step=max(1, end-self.now))
        return start, landed

    def test_repeated_persistent_methods_and_business(self):
        for method in learning.METHODS:
            with self.subTest(method=method):
                # Separate player history between scenarios.
                with app.activity_db() as db:
                    db.execute('DELETE FROM travel_observations')
                    db.execute('DELETE FROM travel_tracking')
                for i in range(6):
                    start, landed = self.trip(method)
                    if i == 0:
                        self.assertIsNone(landed['travel_profile']['method'])
                self.assertEqual(landed['travel_profile']['method'], method)
                self.assertEqual(landed['travel_profile']['confidence'], 'High')
                next_departure = self.now + 120
                next_start, _ = self.trip(method)
                self.assertEqual(next_start['estimated_until'], next_departure + learning.TRAVEL_TIMES['South Africa'][method])
                self.assertEqual(next_start['prediction_method'], method)

    def test_occasional_business_does_not_replace_airstrip(self):
        for _ in range(8):
            self.trip('Airstrip')
        _, landed = self.trip('Business Class')
        p = landed['travel_profile']
        self.assertEqual(p['method'], 'Airstrip')
        self.assertTrue(p['has_used_business'])
        self.assertEqual(p['matches']['Airstrip'], 8)
        self.assertEqual(p['matches']['Business Class'], 1)

    def test_change_overrides_old_history(self):
        for _ in range(12):
            self.trip('Airstrip')
        self.trip('WLT')
        _, second = self.trip('WLT')
        self.assertTrue(second['travel_profile']['behavior_changed'])
        self.assertIsNone(second['travel_profile']['method'])
        _, third = self.trip('WLT')
        self.assertEqual(third['travel_profile']['method'], 'WLT')
        self.assertTrue(third['travel_profile']['behavior_changed'])

    def test_ambiguous_and_unmatched_timing(self):
        self.assertEqual(learning.match_duration('Mexico', 570, 120, 120)['reason'], 'ambiguous timing')
        self.assertEqual(learning.match_duration('Mexico', 60, 60, 60)['reason'], 'unmatched timing')
        self.assertEqual(learning.match_duration('Mexico', 1020, 60, 60)['closest_method'], 'Airstrip')
        self.assertEqual(learning.match_duration('South Africa', 11820 * 1.03 + 50, 60, 60)['reason'], '')

    def test_missing_polls_are_saved_but_not_used(self):
        _, landed = self.trip('WLT', gap=True)
        row = self.rows()[0]
        self.assertFalse(row['usable'])
        self.assertEqual(row['rejection_reason'], 'polling gap')
        self.assertEqual(landed['travel_profile']['observed_trips'], 0)

    def test_restart_preserves_start_and_eta(self):
        self.poll()
        start = self.poll('Traveling', 'Mexico')
        started_at = self.now
        self.runtime = app.new_runtime_state()
        app.init_activity_db()
        restarted = self.poll('Traveling', 'Mexico')
        self.assertEqual(start['estimated_until'], restarted['estimated_until'])
        while self.now < started_at + 960:
            self.poll('Traveling', 'Mexico')
        self.poll('Abroad', 'Mexico')
        self.assertTrue(self.rows()[0]['usable'])
        self.assertEqual(self.rows()[0]['first_traveling'], started_at)

    def test_long_restart_gap_rejects(self):
        self.poll()
        start = self.poll('Traveling', 'Mexico')
        self.runtime = app.new_runtime_state()
        resumed = self.poll('Traveling', 'Mexico', step=900)
        self.assertEqual(start['estimated_until'], resumed['estimated_until'])
        self.poll('Abroad', 'Mexico', step=120)
        self.assertFalse(self.rows()[0]['usable'])

    def test_already_traveling_and_duplicate_arrivals(self):
        start = self.poll('Traveling', 'Mexico')
        self.assertEqual(start['estimated_until'], self.now + app.TRAVEL_SECONDS['Mexico'])
        for _ in range(16):
            self.poll('Traveling', 'Mexico')
        self.poll('Abroad', 'Mexico')
        self.poll('Abroad', 'Mexico')
        self.poll('Abroad', 'Mexico', step=0)
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse(self.rows()[0]['usable'])

    def test_unknown_preserves_default_and_api_eta_wins(self):
        self.poll()
        start = self.poll('Traveling', 'South Africa')
        self.assertEqual(start['estimated_until'], self.now + app.TRAVEL_SECONDS['South Africa'])
        response = {'members': {'123': {'status': {'state': 'Traveling', 'description': 'Traveling to South Africa', 'until': self.now + 123}}}}
        with patch.object(app.time, 'time', return_value=self.now + 60):
            app.update_runtime_state(self.runtime, response, track_activity=True)
        member = self.runtime['members'][0]
        self.assertEqual(member['until'], self.now + 123)
        self.assertNotIn('estimated_until', member)

    def test_raw_api_info_and_return_hospital_arrival(self):
        self.trip('Airstrip', direction='Returning')
        row = self.rows()[0]
        self.assertTrue(row['usable'])
        self.assertEqual(json.loads(row['api_info'])['departure']['travel']['plane'], 'preserved')
        self.assertEqual(row['direction'], 'Returning')
        self.poll()
        self.poll('Traveling', 'Mexico')
        for _ in range(16):
            self.poll('Traveling', 'Mexico')
        self.poll('Hospital', 'Mexico')
        self.assertTrue(self.rows()[-1]['usable'])

    def test_unobserved_arrival_and_wrong_destination(self):
        self.poll()
        self.poll('Traveling', 'Mexico')
        self.poll('Returning', 'Mexico')
        self.assertFalse(self.rows()[0]['usable'])
        self.assertIsNone(self.rows()[0]['first_landed'])
        self.poll('Abroad', 'Canada')
        self.assertFalse(self.rows()[-1]['usable'])

    def test_schema_upgrade_idempotent_and_existing_data_preserved(self):
        self.poll()
        app.init_activity_db()
        app.init_activity_db()
        with app.activity_db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM activity_players').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_ambiguous_trip_is_stored_without_confidence(self):
        with patch.object(app, 'POLL_SECONDS', 120):
            self.poll()
            self.poll('Traveling', 'Mexico', step=120)
            for _ in range(5):
                self.poll('Traveling', 'Mexico', step=90)
            member = self.poll('Abroad', 'Mexico', step=120)
        row = self.rows()[0]
        self.assertEqual(row['duration'], 570)
        self.assertEqual(row['rejection_reason'], 'ambiguous timing')
        self.assertFalse(row['usable'])
        self.assertEqual(member['travel_profile']['observed_trips'], 0)

    def test_late_arrival_and_missed_departure(self):
        self.poll()
        self.poll('Traveling', 'Mexico', step=180)
        for _ in range(16):
            self.poll('Traveling', 'Mexico')
        self.poll('Abroad', 'Mexico')
        self.assertFalse(self.rows()[0]['usable'])
        self.poll()
        self.poll('Traveling', 'Mexico')
        for _ in range(14):
            self.poll('Traveling', 'Mexico')
        self.poll('Abroad', 'Mexico', step=180)
        self.assertFalse(self.rows()[1]['usable'])

    def test_stale_history_does_not_predict(self):
        rows = [{'usable': 1, 'closest_method': 'Airstrip',
                 'first_landed': self.now - 91 * 86400} for _ in range(8)]
        profile = learning.infer_profile(rows, self.now)
        self.assertIsNone(profile['method'])
        self.assertEqual(profile['matches']['Airstrip'], 8)

    def test_pre_faction_database_upgrade(self):
        with app.activity_db() as db:
            db.execute('DROP TABLE activity_intervals')
            db.execute('DROP TABLE activity_players')
            db.execute('CREATE TABLE activity_players (player_id TEXT PRIMARY KEY, name TEXT, profile_url TEXT, first_seen INTEGER, last_seen INTEGER, active INTEGER)')
            db.execute('CREATE TABLE activity_intervals (id INTEGER PRIMARY KEY, player_id TEXT, start_time INTEGER, end_time INTEGER, last_observed INTEGER, is_open INTEGER)')
            db.execute("INSERT INTO activity_players VALUES ('123', 'Test', 'url', 100, 200, 1)")
            db.execute("INSERT INTO activity_intervals VALUES (1, '123', 100, 200, 200, 1)")
        app.init_activity_db()
        with app.activity_db() as db:
            self.assertEqual(db.execute('SELECT name FROM activity_players').fetchone()[0], 'Test')
            self.assertEqual(db.execute('SELECT end_time FROM activity_intervals').fetchone()[0], 200)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_flask_payload_and_page(self):
        self.poll()
        with patch.object(app, 'state', self.runtime):
            client = app.app.test_client()
            payload = client.get('/api/status').get_json()
            self.assertEqual(payload['members'][0]['travel_profile']['confidence'], 'Unknown')
            page = client.get('/')
            self.assertEqual(page.status_code, 200)
            self.assertIn(b'function travelProfile(m)', page.data)

    def test_friendly_default_unchanged(self):
        runtime = app.new_runtime_state()
        with patch.object(app.time, 'time', return_value=self.now):
            app.update_runtime_state(runtime, {'members': {'123': {'status': {'state': 'Traveling', 'description': 'Traveling to Mexico'}}}})
        member = runtime['members'][0]
        self.assertEqual(member['estimated_until'], self.now + app.TRAVEL_SECONDS['Mexico'])
        self.assertNotIn('travel_profile', member)


if __name__ == '__main__':
    unittest.main()
