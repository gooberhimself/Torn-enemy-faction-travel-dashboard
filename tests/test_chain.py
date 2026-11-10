import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from flask import Flask
from werkzeug.security import generate_password_hash

from chain_coordinator import Coordinator, QueueError, register
import chain_torn


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name)/'test.db')
        def connect():
            db = sqlite3.connect(self.path, timeout=5)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            return db
        self.store = Coordinator(connect, lambda: ('10', '20'))
        self.store.initialize()
        self.store.observe('friendly', {'observed_at': time.time(), 'members': [{'id': '1', 'name': 'Alice'}, {'id': '2', 'name': 'Bob'}]})
        self.store.observe('enemies', {'observed_at': time.time(), 'members': [{'id': '3', 'name': 'Enemy'}, {'id': '4', 'name': 'Other'}]})
        self.app = Flask(__name__, template_folder='../templates')
        self.app.config.update(TESTING=True, TACTICIAN_PIN_HASH=generate_password_hash('12345'), PUBLIC_PIN_HASH='', CHAIN_COOKIE_SECURE=True)
        self.app.context_processor(lambda: {'url_prefix': ''})
        register(self.app, self.store)
        self.client = self.app.test_client()
        self.csrf = self.client.get('/api/chain/state').json['access']['csrf']

    def version(self):
        return self.store.snapshot()['version']

    def add(self, attacker='1', target='3', **extra):
        self.store.mutate('add', {'version': self.version(), 'attacker_id': attacker, 'target_id': target, **extra})
        return self.store.snapshot()['active'][-1]['id']

    def post(self, path, data=None, client=None, csrf=None):
        return (client or self.client).post('/api/chain/'+path, json=data or {}, headers={'X-CSRF-Token': csrf or self.csrf})

    def unlock(self):
        r = self.post('unlock', {'pin': '12345'})
        self.assertEqual(r.status_code, 200)
        self.csrf = self.client.get('/api/chain/state').json['access']['csrf']
        return r

    def test_view_and_authorization(self):
        self.assertEqual(self.client.get('/chain').status_code, 200)
        self.assertFalse(self.client.get('/api/chain/state').json['access']['editing'])
        for path in ('assignments', 'reorder', 'advance', 'skip', 'restore', 'lock', 'touch'):
            self.assertEqual(self.post(path).status_code, 401)
        for method in ('patch','delete'):
            self.assertEqual(getattr(self.client, method)('/api/chain/assignments/1', json={}).status_code, 401)
        self.assertEqual(self.post('unlock', {'pin': '12345'}, csrf='wrong').status_code, 403)
        self.assertEqual(self.post('unlock', {'pin': '00000'}).status_code, 401)
        r = self.unlock()
        self.assertIn('HttpOnly', r.headers['Set-Cookie'])
        self.assertIn('Secure', r.headers['Set-Cookie'])
        self.assertEqual(self.post('assignments', csrf='wrong').status_code, 403)
        self.assertEqual(self.post('lock').status_code, 200)
        self.assertEqual(self.post('advance').status_code, 401)

    def test_lockout_persists_and_cannot_reset_with_cookie(self):
        for _ in range(5):
            self.assertEqual(self.post('unlock', {'pin': '00000'}).status_code, 401)
        self.assertEqual(self.post('unlock', {'pin': '12345'}).status_code, 429)
        client = self.app.test_client()
        csrf = client.get('/api/chain/state').json['access']['csrf']
        self.assertEqual(self.post('unlock', {'pin': '12345'}, client, csrf).status_code, 429)
        with patch('chain_coordinator.time.time', return_value=time.time()+901):
            self.assertEqual(self.post('unlock', {'pin': '12345'}).status_code, 200)

    def test_expiration_viewing_does_not_extend(self):
        self.unlock()
        before = self.client.get('/api/chain/state').json['access']['expires_at']
        with patch('chain_coordinator.time.time', return_value=before-1):
            self.assertEqual(self.client.get('/api/chain/state').json['access']['expires_at'], before)
        with patch('chain_coordinator.time.time', return_value=before+1):
            self.assertEqual(self.post('advance').status_code, 401)

    def test_add_reorder_edit_delete_skip_restore_complete(self):
        a, b = self.add(), self.add('2', '4')
        self.store.mutate('reorder', {'version': self.version(), 'ids': [b, a]})
        self.assertEqual(self.store.snapshot()['active'][0]['id'], b)
        self.store.mutate('skip', {'version': self.version(), 'assignment_id': b})
        self.assertIsNone(self.store.snapshot()['deferred'][0]['completed_at'])
        self.store.mutate('restore', {'version': self.version()}, b)
        self.store.mutate('advance', {'version': self.version(), 'assignment_id': a})
        done = self.store.snapshot()['completed'][0]
        self.assertEqual(done['status'], 'completed_unverified')
        self.store.mutate('edit', {'version': self.version(), 'attacker_id': '1', 'target_id': '3'}, b)
        self.store.mutate('delete', {'version': self.version()}, b)
        self.assertFalse(self.store.snapshot()['active'])

    def test_duplicate_target_and_invalid_order(self):
        a = self.add()
        with self.assertRaises(sqlite3.IntegrityError): self.add('2', '3')
        b = self.add('2', '3', allow_duplicate=True)
        with self.assertRaises(QueueError): self.store.mutate('reorder', {'version': self.version(), 'ids': [a, a]})
        self.assertEqual(len(self.store.snapshot()['active']), 2)
        self.store.mutate('skip', {'version': self.version(), 'assignment_id': a})
        with self.assertRaises(sqlite3.IntegrityError): self.store.mutate('restore', {'version': self.version()}, a)

    def test_simultaneous_edits_and_duplicate_advance(self):
        a, b = self.add(), self.add('2','4')
        data = {'version': self.version(), 'assignment_id': a}
        def advance():
            try: self.store.mutate('advance', data); return 200
            except QueueError as e: return e.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: advance(), range(2))), [200,409])
        self.assertEqual(self.store.snapshot()['active'][0]['id'], b)
        self.assertEqual(len(self.store.snapshot()['completed']), 1)

    def test_restart_integrity_and_faction_guard(self):
        self.add()
        new = Coordinator(self.store.db_factory, self.store.factions)
        new.initialize()
        self.assertEqual(new.snapshot()['active'], self.store.snapshot()['active'])
        with new.db() as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        new.factions = lambda: ('10','99')
        with self.assertRaises(QueueError): new.snapshot()

    def test_sse_initial_reconnect_and_changes(self):
        r = self.client.get('/api/chain/events', buffered=False)
        self.assertIn(b'event: state', next(r.response))
        self.add()
        self.assertIn(b'"attacker_name": "Alice"', next(r.response))
        r.close()
        r = self.client.get('/api/chain/events', headers={'Last-Event-ID': '999'}, buffered=False)
        first = next(r.response)
        self.assertIn(b'"attacker_name": "Alice"', first)
        self.assertNotIn(b'csrf', first)
        r.close()

    def test_wrong_matchup_returns_json_409_before_streaming(self):
        self.add()
        self.store.factions = lambda: ('10', '99')
        for path in ('state', 'events'):
            with self.subTest(path=path):
                response = self.client.get('/api/chain/'+path)
                self.assertEqual(response.status_code, 409)
                self.assertIn('CHAIN_DB_PATH', response.json['error'])
                self.assertIn('enemy 20', response.json['error'])
                self.assertIn('enemy 99', response.json['error'])
        self.store.factions = lambda: ('10', '20')
        self.assertEqual(len(self.store.snapshot()['active']), 1)

    def test_stream_storage_failure_sends_error_and_closes_cleanly(self):
        response = self.client.get('/api/chain/events', buffered=False)
        self.assertIn(b'event: state', next(response.response))
        with patch.object(self.store, 'snapshot', side_effect=sqlite3.OperationalError('sensitive detail')):
            message = next(response.response)
            self.assertIn(b'event: coordinator_error', message)
            self.assertNotIn(b'sensitive detail', message)
            with self.assertRaises(StopIteration):
                next(response.response)
        response.close()

    def attack(self, aid=100, attacker=1, target=3, **extra):
        now = int(time.time())
        return {'id': aid, 'started': now+1, 'ended': now+2,
                'attacker': attacker if isinstance(attacker,dict) else {'id': attacker, 'faction': {'id': 10}}, 'defender': {'id': target},
                'result': 'Hospitalized', 'chain': 20, 'is_interrupted': False, **extra}

    def test_auto_advance_duplicate_delayed_manual_confirmation(self):
        a, b = self.add(), self.add('2','4')
        attack = self.attack()
        self.store.reconcile([attack,attack])
        self.assertEqual(self.store.snapshot()['active'][0]['id'], b)
        self.assertEqual(self.store.snapshot()['completed'][0]['verified_attack_id'], '100')
        self.store.mutate('advance', {'version':self.version(),'assignment_id':b})
        completed_at = next(r for r in self.store.snapshot()['completed'] if r['id']==b)['completed_at']
        later = self.attack(101,2,4,started=attack['ended'],ended=attack['ended']+1)
        self.store.reconcile([later,attack])
        row = next(r for r in self.store.snapshot()['completed'] if r['id']==b)
        self.assertEqual(row['completed_at'], completed_at)
        self.assertEqual(row['verification'],'verified')

    def test_failed_non_chain_wrong_and_historical_attacks(self):
        self.add()
        self.store.reconcile([self.attack(100,2,4),self.attack(101,started=1,ended=2)])
        self.assertEqual(self.store.snapshot()['active'][0]['verification'],'pending')
        self.store.reconcile([self.attack(102,chain=0),self.attack(103,result='Lost'),self.attack(104,chain=None)])
        self.assertEqual(self.store.snapshot()['active'][0]['verification'],'review')
        self.assertFalse(self.store.snapshot()['completed'])

    def test_manual_conflict_and_out_of_order_review(self):
        a, b = self.add(), self.add('2','4')
        self.store.reconcile([self.attack(101,2,4)])
        self.assertEqual(self.store.snapshot()['active'][1]['verification'],'review')
        self.store.mutate('advance', {'version':self.version(),'assignment_id':a})
        self.store.reconcile([self.attack(102,result='Lost')])
        self.assertEqual(self.store.snapshot()['completed'][0]['verification'],'review')
        self.assertEqual(len(self.store.snapshot()['active']),1)

    def test_observation_cache_age_and_outage(self):
        old = time.time()-1000
        self.store.observe('chain', {'observed_at':old,'deadline':old+100})
        self.store.observe('chain', {'observed_at':old-50,'deadline':old+500})
        self.assertEqual(self.store.snapshot()['observations']['chain']['deadline'],old+100)
        self.store.observe('chain', error='Unavailable')
        self.assertTrue(self.store.snapshot()['observations']['chain']['stale'])
        self.add()
        with patch('chain_torn.get', side_effect=chain_torn.TornUnavailable('Unavailable')):
            chain_torn.poll(self.store,'fake','10')
        self.store.mutate('advance', {'version':self.version(),'assignment_id':self.store.snapshot()['active'][0]['id']})
        self.assertEqual(len(self.store.snapshot()['completed']),1)

    def test_chain_timestamps_and_phases(self):
        now = int(time.time())
        data = {'timestamp':now-20,'chain':{'current':10,'timeout':60,'cooldown':0,'start':now-100}}
        result = chain_torn.chain_observation(data,now)
        self.assertEqual(result['deadline']-now,40)
        self.assertEqual(result['phase'],'active')
        data['chain']['current']=5
        self.assertEqual(chain_torn.chain_observation(data,now)['phase'],'warmup')
        data['chain']['cooldown']=now+500
        self.assertEqual(chain_torn.chain_observation(data,now)['phase'],'cooldown')
        for ts in (None, 'bad', -1, now+100):
            data['timestamp']=ts
            with self.assertRaises(chain_torn.TornUnavailable): chain_torn.chain_observation(data,now)


    def test_http_mutations_and_stale_dialog_version(self):
        self.unlock()
        data={'version':self.version(),'attacker_id':'1','target_id':'3'}
        response=self.post('assignments',data)
        self.assertEqual(response.status_code,200)
        aid=response.json['active'][0]['id']
        self.assertEqual(self.post('assignments',data).status_code,409)
        response=self.client.patch('/api/chain/assignments/'+str(aid),json={
            'version':self.version(),'attacker_id':'2','target_id':'4'},headers={'X-CSRF-Token':self.csrf})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['active'][0]['attacker_name'],'Bob')
        self.assertEqual(self.post('advance',{'version':self.version(),'assignment_id':aid+1}).status_code,409)
        self.assertEqual(self.post('advance',{'version':self.version(),'assignment_id':aid}).status_code,200)
        self.assertEqual(self.client.delete('/api/chain/assignments/'+str(aid),json={'version':self.version()},headers={'X-CSRF-Token':self.csrf}).status_code,409)

    def test_multiple_coordinators_and_global_lockout(self):
        self.unlock()
        other=self.app.test_client()
        token=other.get('/api/chain/state').json['access']['csrf']
        self.assertEqual(self.post('unlock',{'pin':'12345'},other,token).status_code,200)
        self.post('lock')
        self.assertTrue(other.get('/api/chain/state').json['access']['editing'])
        self.csrf=self.client.get('/api/chain/state').json['access']['csrf']
        for i in range(25):
            response=self.client.post('/api/chain/unlock',json={'pin':'00000'},headers={'X-CSRF-Token':self.csrf},environ_base={'REMOTE_ADDR':f'192.0.2.{i}'})
            self.assertEqual(response.status_code,401)
        response=self.client.post('/api/chain/unlock',json={'pin':'12345'},headers={'X-CSRF-Token':self.csrf},environ_base={'REMOTE_ADDR':'192.0.2.200'})
        self.assertEqual(response.status_code,429)

    def test_unknown_members_malformed_requests_and_unconfigured_pin(self):
        original = self.app.config['TACTICIAN_PIN_HASH']
        self.app.config['TACTICIAN_PIN_HASH']=''
        self.assertEqual(self.post('unlock',{'pin':'12345'}).status_code,503)
        self.app.config['TACTICIAN_PIN_HASH']=original
        self.unlock()
        for body in ({}, {'version':self.version(),'attacker_id':'999','target_id':'3'},
                     {'version':self.version(),'attacker_id':'1','target_id':'3','allow_duplicate':'false'}):
            self.assertIn(self.post('assignments',body).status_code,(400,409))
        self.assertEqual(self.client.post('/api/chain/assignments',data='not json',headers={'X-CSRF-Token':self.csrf}).status_code,400)
        self.assertEqual(self.post('assignments',{'oversized':'x'*17000}).status_code,413)

    def test_touch_extends_only_live_authorized_session(self):
        self.unlock()
        expiry=self.client.get('/api/chain/state').json['access']['expires_at']
        with patch('chain_coordinator.time.time',return_value=expiry-5):
            self.assertEqual(self.post('touch').status_code,200)
            self.assertGreater(self.client.get('/api/chain/state').json['access']['expires_at'],expiry)
        self.assertEqual(self.post('touch',csrf='wrong').status_code,403)

    def test_sorted_attack_batch_and_ambiguous_reused_pair(self):
        self.add(); self.add('2','4')
        first=self.attack(300)
        second=self.attack(301,2,4,started=first['ended'],ended=first['ended']+1)
        self.store.reconcile([second,first])
        self.assertFalse(self.store.snapshot()['active'])
        self.assertEqual(len(self.store.snapshot()['completed']),2)
        self.add(); self.add(allow_duplicate=True)
        later=self.attack(302)
        self.store.reconcile([later])
        self.assertTrue(all(r['verification']=='review' for r in self.store.snapshot()['active']))

    def test_interrupted_wrong_faction_and_invalid_attack_records(self):
        self.add()
        self.store.reconcile([self.attack(400,attacker={'id':1,'faction':{'id':99}}),
                              self.attack(401,ended=0),self.attack(402,started='invalid'),None])
        self.assertEqual(self.store.snapshot()['active'][0]['verification'],'pending')
        self.store.reconcile([self.attack(403,is_interrupted=True)])
        self.assertEqual(self.store.snapshot()['active'][0]['verification'],'review')

    def test_permission_errors_distinguish_faction_role_from_key_access(self):
        for code, expected in ((7, 'faction position has API access'),
                               (16, 'key access level is insufficient')):
            with self.subTest(code=code), patch('chain_torn.requests.get') as get:
                get.return_value.json.return_value = {'error': {'code': code, 'error': 'sensitive detail'}}
                with self.assertRaises(chain_torn.TornUnavailable) as error:
                    chain_torn.get('test-only', 'faction', {'selections': 'attacks,timestamp'})
                self.assertIn(expected, str(error.exception))
                self.assertIn('Manual coordination remains available', str(error.exception))
                self.assertNotIn('sensitive detail', str(error.exception))

    def test_adapter_requests_permissions_and_sanitized_errors(self):
        import requests
        with patch('chain_torn.requests.get',side_effect=requests.RequestException('secret-url-key')):
            with self.assertRaises(chain_torn.TornUnavailable) as error:
                chain_torn.get('secret','faction',{})
            self.assertNotIn('secret',str(error.exception))
        with patch('chain_torn.requests.get') as get:
            get.return_value.json.return_value={'error':{'code':7,'error':'private data'}}
            with self.assertRaises(chain_torn.TornUnavailable) as error:
                chain_torn.get('secret','faction',{})
            self.assertIn('7',str(error.exception))
            self.assertNotIn('private',str(error.exception))
            self.assertEqual(get.call_args.kwargs['headers'],{'Authorization':'ApiKey secret'})
            self.assertNotIn('key',get.call_args.kwargs['params'])
        now=int(time.time())
        observation={'timestamp':now,'chain':{'current':10,'timeout':60,'cooldown':0,'start':now-100}}
        self.add()
        with patch('chain_torn.get',side_effect=[observation,{'timestamp':now,'attacks':[self.attack(500)]}]) as get:
            chain_torn.poll(self.store,'fake','10')
            self.assertEqual(get.call_count,2)
            self.assertEqual(self.store.snapshot()['completed'][0]['verification'],'verified')
        with patch('chain_torn.get',return_value=observation) as get:
            chain_torn.poll(self.store,'fake','10',verify=False)
            self.assertEqual(get.call_count,1)
        with patch('chain_torn.get',side_effect=[observation,{'timestamp':now,'attacks':[self.attack(501,attacker={'id':1,'faction':{'id':99}})]}]):
            chain_torn.poll(self.store,'fake','10')
        self.assertIn('does not belong',self.store.snapshot()['observations']['attacks']['error'])


if __name__ == '__main__': unittest.main()
