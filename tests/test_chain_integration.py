"""Bridge regression tests: use temporary storage and never start the Torn poll thread."""
import importlib
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_connect = sqlite3.connect
with patch('sqlite3.connect', side_effect=lambda *args, **kwargs: _connect(':memory:')):
    tracker = importlib.import_module('app')


class ChainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name,value in [('ACTIVITY_DB_PATH',str(Path(self.tmp.name)/'activity.db')),
                           ('CHAIN_DB_PATH',''),
                           ('state',tracker.new_runtime_state()),('friendly_state',tracker.new_runtime_state())]:
            patcher=patch.object(tracker,name,value)
            patcher.start(); self.addCleanup(patcher.stop)
        tracker.init_activity_db()

    def data(self, name, state='Okay', description='Okay', until=None):
        return {'timestamp':int(time.time()),'members':{'123':{'name':name,'status':{
            'state':state,'description':description,'until':until}}}}

    def test_shared_rosters_preserve_hospital_and_location_classification(self):
        until=int(time.time())-5
        enemy=self.data('Enemy','Hospital','In a Japanese hospital',until)
        friendly=self.data('Friendly')
        with patch.object(tracker,'api_get_faction_basic',side_effect=[enemy,friendly]) as get:
            tracker.poll_once(); tracker.poll_friendly_once()
        self.assertEqual(get.call_count,2)
        self.assertEqual(get.call_args_list[0].args,(tracker.FACTION_ID,))
        self.assertEqual(get.call_args_list[1].args,(tracker.FRIENDLY_FACTION_ID,))
        obs=tracker.coordinator.snapshot()['observations']
        member=obs['enemies']['members'][0]
        self.assertFalse(member['in_torn'])
        self.assertEqual(member['location'],'Japan')
        self.assertEqual(member['hospital_until'],until)
        self.assertEqual(member['status'],'Hospital')
        self.assertEqual(obs['friendly']['members'][0]['name'],'Friendly')
        self.assertEqual(tracker.status_payload(tracker.state,True)['members'][0]['state'],'Hospital')

    def test_poll_interval_and_coordinator_failure_isolation(self):
        with patch.object(tracker,'poll_once') as enemy, patch.object(tracker,'poll_friendly_once') as friendly, \
             patch.object(tracker.chain_torn,'poll',side_effect=RuntimeError('unavailable')) as chain, \
             patch.object(tracker.time,'sleep',side_effect=StopIteration) as sleep, \
             self.assertLogs(tracker.app.logger,level='ERROR'):
            with self.assertRaises(StopIteration): tracker.poll_loop()
        self.assertEqual(enemy.call_count,1)
        self.assertEqual(friendly.call_count,1)
        self.assertEqual(chain.call_count,1)
        sleep.assert_called_once_with(max(tracker.POLL_SECONDS,30))
        with patch.object(tracker,'api_get_faction_basic',return_value=self.data('Enemy')), \
             patch.object(tracker.coordinator,'observe',side_effect=sqlite3.OperationalError('busy')), \
             self.assertLogs(tracker.app.logger,level='ERROR'):
            tracker.poll_once()
        self.assertEqual(tracker.state['members'][0]['name'],'Enemy')

    def test_prefix_public_route_and_static_assets(self):
        client=tracker.app.test_client()
        with patch.object(tracker,'URL_PREFIX','/example'):
            html=client.get('/chain',headers={'Host':'tracker.example.test'}).get_data(as_text=True)
        self.assertIn('data-api="/example/api/chain"',html)
        self.assertIn('src="/example/static/chain.js?v=20261110-storage-errors"',html)
        self.assertIn('href="/example/"',html)
        for path in ['/api/chain/state','/static/chain.js','/static/chain-time.js','/static/chain.css']:
            with client.get(path) as response:
                self.assertEqual(response.status_code,200)

    def test_older_roster_response_cannot_replace_new_observation(self):
        data=self.data('Original')
        with patch.object(tracker,'api_get_faction_basic',return_value=data): tracker.poll_once()
        data=self.data('Cached')
        data['timestamp']-=1000
        with patch.object(tracker,'api_get_faction_basic',return_value=data): tracker.poll_once()
        self.assertEqual(tracker.coordinator.snapshot()['observations']['enemies']['members'][0]['name'],'Original')

    def test_new_matchup_storage_preserves_original_queue_and_tracking(self):
        with patch.object(tracker,'api_get_faction_basic',return_value=self.data('Enemy')):
            tracker.poll_once()
        with patch.object(tracker,'api_get_faction_basic',return_value=self.data('Friendly')):
            tracker.poll_friendly_once()
        tracker.coordinator.mutate('add', {'version': 0, 'attacker_id': '123', 'target_id': '123'})
        original = tracker.coordinator.snapshot()
        with tracker.activity_db() as db:
            activity_before = [tuple(r) for r in db.execute('SELECT * FROM activity_players')]

        separate = str(Path(self.tmp.name)/'new-matchup.db')
        with patch.object(tracker, 'CHAIN_DB_PATH', separate), patch.object(tracker, 'FACTION_ID', 'new-enemy'):
            tracker.coordinator.initialize()
            snapshot = tracker.coordinator.snapshot()
            self.assertEqual(snapshot['enemy_faction_id'], 'new-enemy')
            self.assertEqual(snapshot['active'], [])
            self.assertEqual(tracker.app.test_client().get('/api/chain/state').status_code, 200)
            # Startup and snapshot writes must leave the old queue and activity intact.
            tracker.coordinator.observe('chain', {'observed_at': time.time(), 'current': 0})
            with tracker.activity_db() as db:
                self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM activity_players')], activity_before)
                self.assertEqual(db.execute('SELECT enemy_faction_id FROM chain_queue').fetchone()[0], original['enemy_faction_id'])
        self.assertEqual(tracker.coordinator.snapshot()['active'], original['active'])

    def test_mismatch_logs_actionable_message_and_skips_torn_requests(self):
        with patch.object(tracker, 'FACTION_ID', 'new-enemy'), \
             patch.object(tracker.chain_torn, 'get') as get, \
             self.assertLogs(tracker.app.logger, level='ERROR') as logs:
            tracker.publish_chain_observation('enemies', {'observed_at': time.time()})
            with self.assertRaises(tracker.chain_coordinator.QueueError):
                tracker.chain_torn.poll(tracker.coordinator, 'fake', tracker.FRIENDLY_FACTION_ID)
        get.assert_not_called()
        self.assertIn('CHAIN_DB_PATH', logs.output[0])
