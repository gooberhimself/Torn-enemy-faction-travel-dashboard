"""Persistent sequential chain queue; no network access or polling in HTTP routes."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from threading import Condition

from flask import Blueprint, Response, jsonify, render_template, request
from werkzeug.security import check_password_hash

SESSION_SECONDS = 30 * 60
LOCKOUT_SECONDS = 15 * 60
WARNINGS = {"prepare": 120, "attack": 60, "urgent": 30, "critical": 15}
SUCCESS_RESULTS = {"Attacked", "Mugged", "Hospitalized"}


class QueueError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


class Coordinator:
    def __init__(self, db_factory, factions, stale_seconds=120):
        self.db_factory, self.factions = db_factory, factions
        self.stale_seconds = stale_seconds
        self.changed = Condition()

    @contextmanager
    def db(self, write=False):
        with closing(self.db_factory()) as db:
            try:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    def initialize(self):
        with self.db(write=True) as db:
            for sql in SCHEMA:
                db.execute(sql)
            friendly, enemy = self.factions()
            db.execute("INSERT OR IGNORE INTO chain_queue VALUES (1, ?, ?, 0, 0, ?)",
                       (friendly, enemy, time.time()))

    def queue(self, db):
        row = db.execute("SELECT * FROM chain_queue WHERE id=1").fetchone()
        if not row:
            raise QueueError("Coordinator queue is not initialized. Restart the application.", 409)
        if (row['faction_id'], row['enemy_faction_id']) != self.factions():
            friendly, enemy = self.factions()
            raise QueueError(
                f"Saved queue belongs to friendly faction {row['faction_id']} / enemy {row['enemy_faction_id']}; "
                f"this instance is configured for friendly faction {friendly} / enemy {enemy}. "
                "Set CHAIN_DB_PATH to a separate file for this matchup and restart. Existing queue and activity history are preserved.", 409)
        return dict(row)

    def bump(self, db, queue_change=True):
        db.execute("UPDATE chain_queue SET version=version+?, revision=revision+1, updated_at=? WHERE id=1",
                   (int(queue_change), time.time()))

    def notify(self):
        with self.changed:
            self.changed.notify_all()

    def event(self, db, kind, assignment_id=None, data=None):
        db.execute("INSERT INTO chain_events(assignment_id,event_type,event_data,created_at) VALUES(?,?,?,?)",
                   (assignment_id, kind, json.dumps(data or {}), time.time()))

    def observe(self, kind, data=None, error=None):
        with self.db(write=True) as db:
            self.queue(db)
            old = db.execute("SELECT * FROM chain_observations WHERE kind=?", (kind,)).fetchone()
            payload = json.loads(old['payload']) if old else {}
            if data is not None:
                # Repeated or older cached observations must not become fresh.
                if not old or data.get('observed_at', 0) > payload.get('observed_at', 0):
                    payload = data
            db.execute("INSERT INTO chain_observations VALUES(?,?,?) ON CONFLICT(kind) DO UPDATE SET payload=excluded.payload,error=excluded.error",
                       (kind, json.dumps(payload), error))
            self.bump(db, False)
        self.notify()

    def snapshot(self):
        with self.db() as db:
            queue = self.queue(db)
            rows = [dict(r) for r in db.execute("SELECT * FROM chain_assignments ORDER BY position,id")]
            observations = {r['kind']: {**json.loads(r['payload']), 'error': r['error']}
                            for r in db.execute("SELECT * FROM chain_observations")}
        now = time.time()
        for obs in observations.values():
            obs['stale'] = not obs.get('observed_at') or now - obs['observed_at'] > self.stale_seconds or bool(obs['error'])
        active = [r for r in rows if r['status'] == 'queued']
        targets = {str(m['id']): m for m in observations.get('enemies', {}).get('members', [])}
        for r in rows:
            r['target'] = targets.get(r['target_id'])
        return {**queue, 'server_time': now, 'active': active,
                'completed': sorted([r for r in rows if r['completed_at']], key=lambda r: r['completed_at'], reverse=True)[:100],
                'deferred': [r for r in rows if r['status'] == 'deferred'],
                'observations': observations, 'warnings': WARNINGS, 'stale_seconds': self.stale_seconds}

    def head(self, db):
        return db.execute("SELECT * FROM chain_assignments WHERE status='queued' ORDER BY position LIMIT 1").fetchone()

    def promote(self, db, previous, effective_at=None):
        head = self.head(db)
        if head and (not previous or head['id'] != previous['id']):
            db.execute("UPDATE chain_assignments SET active_since=? WHERE id=?",
                       (effective_at or time.time(), head['id']))

    def assignment_fields(self, db, data):
        found = []
        for kind, field in [('friendly', 'attacker_id'), ('enemies', 'target_id')]:
            row = db.execute("SELECT payload FROM chain_observations WHERE kind=?", (kind,)).fetchone()
            members = json.loads(row['payload']).get('members', []) if row else []
            member = next((m for m in members if str(m['id']) == str(data.get(field))), None)
            if not member:
                raise QueueError("Select a member from the last confirmed faction roster.")
            found.extend([str(member['id']), member['name']])
        override = data.get('allow_duplicate', False)
        if not isinstance(override, bool):
            raise QueueError('allow_duplicate must be a boolean.')
        return (*found, int(override))

    def mutate(self, action, data, assignment_id=None):
        with self.db(write=True) as db:
            queue = self.queue(db)
            if type(data.get('version')) is not int or data['version'] != queue['version']:
                raise QueueError('Queue changed. Review the refreshed order and try again.', 409)
            previous = self.head(db)
            now = time.time()
            row = None
            if assignment_id is not None:
                row = db.execute('SELECT * FROM chain_assignments WHERE id=?', (assignment_id,)).fetchone()
                if not row:
                    raise QueueError('Assignment not found.', 404)
            if action == 'add':
                fields = self.assignment_fields(db, data)
                position = db.execute('SELECT COALESCE(MAX(position),0)+1 FROM chain_assignments').fetchone()[0]
                assignment_id = db.execute('''INSERT INTO chain_assignments
                    (attacker_id,attacker_name,target_id,target_name,allow_duplicate,position,created_at,updated_at,eligible_since)
                    VALUES(?,?,?,?,?,?,?,?,?)''', (*fields, position, now, now, now)).lastrowid
            elif action == 'edit':
                if row['status'] not in ('queued', 'deferred'):
                    raise QueueError('Only queued or deferred assignments can be edited.', 409)
                fields = self.assignment_fields(db, data)
                db.execute('''UPDATE chain_assignments SET attacker_id=?,attacker_name=?,target_id=?,target_name=?,allow_duplicate=?,
                    updated_at=?,eligible_since=?,verification='pending',active_since=? WHERE id=?''',
                           (*fields, now, now, now if previous and previous['id'] == assignment_id else None, assignment_id))
            elif action == 'reorder':
                ids = data.get('ids')
                actual = [r[0] for r in db.execute("SELECT id FROM chain_assignments WHERE status='queued'")]
                if not isinstance(ids, list) or any(type(i) is not int for i in ids) or len(ids) != len(set(ids)) or set(ids) != set(actual):
                    raise QueueError('Ordering must contain every active assignment exactly once.')
                # Negative temporary positions avoid uniqueness collisions.
                db.execute("UPDATE chain_assignments SET position=-id WHERE status='queued'")
                for position, aid in enumerate(ids, 1):
                    db.execute('UPDATE chain_assignments SET position=?,updated_at=? WHERE id=?', (position, now, aid))
            elif action in ('advance', 'skip'):
                if not previous or data.get('assignment_id') != previous['id']:
                    raise QueueError('The next attacker changed. Review the queue.', 409)
                assignment_id = previous['id']
                if action == 'advance':
                    db.execute("UPDATE chain_assignments SET status='completed_unverified',verification='unverified',completed_at=?,updated_at=? WHERE id=?",
                               (now, now, assignment_id))
                else:
                    db.execute("UPDATE chain_assignments SET status='deferred',updated_at=? WHERE id=?", (now, assignment_id))
            elif action == 'restore':
                if row['status'] != 'deferred':
                    raise QueueError('Only deferred assignments can be restored.', 409)
                position = db.execute('SELECT COALESCE(MAX(position),0)+1 FROM chain_assignments').fetchone()[0]
                db.execute("UPDATE chain_assignments SET status='queued',position=?,updated_at=?,eligible_since=?,active_since=NULL WHERE id=?",
                           (position, now, now, assignment_id))
            elif action == 'delete':
                if row['status'] not in ('queued', 'deferred'):
                    raise QueueError('Completed history cannot be deleted.', 409)
                db.execute("UPDATE chain_assignments SET status='deleted',updated_at=? WHERE id=?", (now, assignment_id))
            else:
                raise QueueError('Unknown operation.')
            self.promote(db, previous)
            self.event(db, action, assignment_id, data)
            self.bump(db)
        self.notify()

    def reconcile(self, attacks):
        """Consume immutable IDs once; ambiguous/out-of-sequence evidence needs review."""
        valid = []
        for a in attacks:
            if not isinstance(a, dict):
                continue
            if any(type(a.get(k)) is not int or a[k] <= 0 for k in ('id', 'started', 'ended')):
                continue
            if a['ended'] < a['started'] or a['ended'] > time.time() + 5:
                continue
            valid.append(a)
        with self.db(write=True) as db:
            self.queue(db)
            changed = False
            for a in sorted(valid, key=lambda a: (a['ended'], a['id'])):
                inserted = db.execute('INSERT OR IGNORE INTO chain_attacks VALUES(?,?,?)',
                                      (str(a['id']), json.dumps(a), time.time())).rowcount
                if not inserted:
                    continue
                attacker, defender = a.get('attacker') or {}, a.get('defender') or {}
                if str((attacker.get('faction') or {}).get('id')) != self.factions()[0]:
                    continue
                candidates = db.execute('''SELECT * FROM chain_assignments WHERE attacker_id=? AND target_id=?
                    AND eligible_since<=? AND status IN ('queued','completed_unverified')
                    AND (completed_at IS NULL OR (? BETWEEN completed_at-300 AND completed_at+120))''',
                    (str(attacker.get('id')), str(defender.get('id')), a['started'], a['ended'])).fetchall()
                if not candidates:
                    continue
                head = self.head(db)
                for row in candidates:
                    chain = a.get('chain')
                    extends = (type(chain) is int and chain > 0 and a.get('result') in SUCCESS_RESULTS
                               and a.get('is_interrupted') is False)
                    in_turn = row['active_since'] is not None and a['started'] >= row['active_since']
                    verified = len(candidates) == 1 and extends and in_turn
                    if row['status'] == 'queued':
                        verified = verified and head is not None and row['id'] == head['id']
                    if verified:
                        db.execute('''UPDATE chain_assignments SET status='completed_verified',verification='verified',
                            completed_at=COALESCE(completed_at,?),verified_at=?,verified_attack_id=?,updated_at=? WHERE id=?''',
                                   (a['ended'], time.time(), str(a['id']), time.time(), row['id']))
                        self.promote(db, head, a['ended'])
                    else:
                        db.execute("UPDATE chain_assignments SET verification='review',updated_at=? WHERE id=?", (time.time(), row['id']))
                    self.event(db, 'api_verified' if verified else 'api_review', row['id'],
                               {'attack_id': a['id'], 'result': a.get('result'), 'chain': chain,
                                'reason': None if verified else 'Non-contributing, ambiguous, or out-of-turn attack; review required.'})
                    changed = True
            if changed:
                self.bump(db)
        if changed:
            self.notify()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def register(app, coordinator):
    bp = Blueprint('chain', __name__)
    app.config.setdefault('CHAIN_PIN_HASH', os.getenv('CHAIN_PIN_HASH', ''))
    app.config.setdefault('CHAIN_COOKIE_SECURE', os.getenv('CHAIN_COOKIE_SECURE', 'true').lower() != 'false')
    cookie_name = 'chain_' + digest('|'.join(coordinator.factions()))[:12]

    def identity(create=False, touch=False):
        raw = request.cookies.get(cookie_name, '')
        now = time.time()
        with coordinator.db(write=True) as db:
            row = db.execute('SELECT * FROM chain_sessions WHERE token=?', (digest(raw),)).fetchone() if raw else None
            if row and row['expires_at'] <= now:
                db.execute('DELETE FROM chain_sessions WHERE token=?', (digest(raw),))
                row = None
            if not row and create:
                raw = secrets.token_urlsafe(32)
                db.execute('DELETE FROM chain_sessions WHERE expires_at<=?', (now,))
                db.execute('INSERT INTO chain_sessions VALUES(?,?,0,?)', (digest(raw), secrets.token_urlsafe(32), now+SESSION_SECONDS))
                row = db.execute('SELECT * FROM chain_sessions WHERE token=?', (digest(raw),)).fetchone()
            if row and touch:
                db.execute('UPDATE chain_sessions SET expires_at=? WHERE token=?', (now+SESSION_SECONDS, digest(raw)))
        return (dict(row) if row else None), raw

    def csrf(row):
        token = request.headers.get('X-CSRF-Token', '')
        if not row or not hmac.compare_digest(row['csrf'], token):
            raise QueueError('Invalid or expired CSRF token. Refresh the page.', 403)

    def authorized():
        row, raw = identity()
        if not row or not row['editing']:
            raise QueueError('Unlock Coordinator Mode to edit.', 401)
        csrf(row)
        identity(touch=True)
        return raw

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise QueueError('A JSON object is required.')
        return data

    @bp.before_request
    def limit_body():
        if request.content_length and request.content_length > 16384:
            raise QueueError('Request is too large.', 413)

    @bp.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        return response

    @bp.errorhandler(QueueError)
    def error(exc):
        response = jsonify(error=exc.message)
        response.status_code = exc.status
        if exc.status == 429:
            response.headers['Retry-After'] = str(LOCKOUT_SECONDS)
        return response

    @bp.errorhandler(sqlite3.IntegrityError)
    def integrity_error(exc):
        return jsonify(error='Assignment conflicts with an active target or position. Use the explicit target reuse override if intended.'), 409

    @bp.errorhandler(sqlite3.OperationalError)
    def busy(exc):
        return jsonify(error='Coordinator storage is temporarily unavailable. Retry after refreshing.'), 503

    @bp.get('/chain')
    def page():
        return render_template('chain.html')

    @bp.get('/api/chain/state')
    def state():
        result = coordinator.snapshot()
        row, raw = identity(create=True)
        result['access'] = {'editing': bool(row['editing']), 'csrf': row['csrf'], 'expires_at': row['expires_at'],
                            'configured': bool(app.config['CHAIN_PIN_HASH'])}
        response = jsonify(result)
        response.set_cookie(cookie_name, raw, secure=app.config['CHAIN_COOKIE_SECURE'], httponly=True, samesite='Strict', path='/')
        return response

    @bp.post('/api/chain/unlock')
    def unlock():
        row, raw = identity()
        csrf(row)
        pin = body().get('pin')
        verifier = app.config['CHAIN_PIN_HASH']
        if not verifier:
            raise QueueError('Coordinator PIN has not been configured.', 503)
        now = time.time()
        ip = digest(request.remote_addr or 'unknown')  # Never trust forwarded headers from arbitrary clients.
        with coordinator.db(write=True) as db:
            db.execute('DELETE FROM chain_login_attempts WHERE created_at<=?', (now-LOCKOUT_SECONDS,))
            total = db.execute('SELECT COUNT(*) FROM chain_login_attempts').fetchone()[0]
            own = db.execute('SELECT COUNT(*) FROM chain_login_attempts WHERE ip=?', (ip,)).fetchone()[0]
            if total >= 25 or own >= 5:
                raise QueueError('Too many PIN attempts. Try again in 15 minutes.', 429)
            attempt = db.execute('INSERT INTO chain_login_attempts(ip,created_at) VALUES(?,?)', (ip, now)).lastrowid
        try:
            correct = isinstance(pin, str) and re.fullmatch(r'[0-9]{5}', pin) and check_password_hash(verifier, pin)
        except (ValueError, TypeError):
            correct = False
        if not correct:
            raise QueueError('Invalid PIN.', 401)
        with coordinator.db(write=True) as db:
            db.execute('DELETE FROM chain_login_attempts WHERE id=?', (attempt,))
            db.execute('DELETE FROM chain_sessions WHERE token=?', (digest(raw),))
            new_raw, new_csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.execute('INSERT INTO chain_sessions VALUES(?,?,1,?)', (digest(new_raw), new_csrf, now+SESSION_SECONDS))
        response = jsonify(ok=True)
        response.set_cookie(cookie_name, new_raw, secure=app.config['CHAIN_COOKIE_SECURE'], httponly=True, samesite='Strict', path='/')
        return response

    @bp.post('/api/chain/lock')
    def lock():
        raw = authorized()
        with coordinator.db(write=True) as db:
            db.execute('DELETE FROM chain_sessions WHERE token=?', (digest(raw),))
        response = jsonify(ok=True)
        response.delete_cookie(cookie_name, path='/')
        return response

    @bp.post('/api/chain/touch')
    def touch():
        authorized()
        return jsonify(ok=True)

    @bp.route('/api/chain/assignments', methods=['POST'])
    @bp.route('/api/chain/assignments/<int:assignment_id>', methods=['PATCH', 'DELETE'])
    def assignment(assignment_id=None):
        authorized()
        coordinator.mutate({'POST': 'add', 'PATCH': 'edit', 'DELETE': 'delete'}[request.method], body(), assignment_id)
        return jsonify(coordinator.snapshot())

    @bp.post('/api/chain/<action>')
    def operation(action):
        authorized()
        if action not in ('reorder', 'advance', 'skip', 'restore'):
            raise QueueError('Unknown operation.', 404)
        data = body()
        aid = data.get('assignment_id') if action == 'restore' else None
        if action == 'restore' and type(aid) is not int:
            raise QueueError('assignment_id is required.')
        coordinator.mutate(action, data, aid)
        return jsonify(coordinator.snapshot())

    @bp.get('/api/chain/events')
    def events():
        # Validate before response headers are sent so Flask can return a JSON
        # 409/503 rather than raising outside its error handlers during streaming.
        initial = coordinator.snapshot()
        def stream():
            revision = None  # Always full state on reconnect, including missed edits.
            last_heartbeat = time.monotonic()
            snapshot = initial
            while True:
                if snapshot['revision'] != revision:
                    revision = snapshot['revision']
                    yield f'id: {revision}\nevent: state\ndata: {json.dumps(snapshot)}\n\n'
                elif time.monotonic()-last_heartbeat >= 15:
                    yield ': heartbeat\n\n'
                    last_heartbeat = time.monotonic()
                with coordinator.changed:
                    coordinator.changed.wait(timeout=1)  # Also detects another process's committed changes.
                try:
                    snapshot = coordinator.snapshot()
                except (QueueError, sqlite3.Error) as exc:
                    message = exc.message if isinstance(exc, QueueError) else 'Coordinator storage is temporarily unavailable.'
                    yield f'event: coordinator_error\ndata: {json.dumps({"error": message})}\n\n'
                    return
        return Response(stream(), mimetype='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    app.register_blueprint(bp)


SCHEMA = [
    '''CREATE TABLE IF NOT EXISTS chain_queue(id INTEGER PRIMARY KEY CHECK(id=1),faction_id TEXT NOT NULL,
       enemy_faction_id TEXT NOT NULL,version INTEGER NOT NULL,revision INTEGER NOT NULL,updated_at REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS chain_assignments(id INTEGER PRIMARY KEY,queue_id INTEGER NOT NULL DEFAULT 1 REFERENCES chain_queue(id),
       attacker_id TEXT NOT NULL,attacker_name TEXT NOT NULL,target_id TEXT NOT NULL,target_name TEXT NOT NULL,
       position INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','deferred','deleted','completed_unverified','completed_verified')),
       allow_duplicate INTEGER NOT NULL DEFAULT 0 CHECK(allow_duplicate IN (0,1)),verification TEXT NOT NULL DEFAULT 'pending',
       created_at REAL NOT NULL,updated_at REAL NOT NULL,eligible_since REAL NOT NULL,active_since REAL,completed_at REAL,verified_at REAL,
       verified_attack_id TEXT UNIQUE REFERENCES chain_attacks(attack_id))''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS chain_active_position ON chain_assignments(position) WHERE status='queued' ''',
    '''CREATE INDEX IF NOT EXISTS chain_match ON chain_assignments(attacker_id,target_id,eligible_since)''',
    '''CREATE TRIGGER IF NOT EXISTS chain_target_insert BEFORE INSERT ON chain_assignments
       WHEN NEW.status='queued' AND NEW.allow_duplicate=0 AND EXISTS(SELECT 1 FROM chain_assignments WHERE status='queued' AND target_id=NEW.target_id)
       BEGIN SELECT RAISE(ABORT,'duplicate target'); END''',
    '''CREATE TRIGGER IF NOT EXISTS chain_target_update BEFORE UPDATE OF target_id,status,allow_duplicate ON chain_assignments
       WHEN NEW.status='queued' AND NEW.allow_duplicate=0 AND EXISTS(SELECT 1 FROM chain_assignments WHERE status='queued' AND target_id=NEW.target_id AND id!=NEW.id)
       BEGIN SELECT RAISE(ABORT,'duplicate target'); END''',
    '''CREATE TABLE IF NOT EXISTS chain_events(id INTEGER PRIMARY KEY,queue_id INTEGER NOT NULL DEFAULT 1 REFERENCES chain_queue(id),
       assignment_id INTEGER REFERENCES chain_assignments(id),event_type TEXT NOT NULL,event_data TEXT NOT NULL,created_at REAL NOT NULL)''',
    '''CREATE INDEX IF NOT EXISTS chain_event_time ON chain_events(created_at)''',
    '''CREATE TABLE IF NOT EXISTS chain_attacks(attack_id TEXT PRIMARY KEY,payload TEXT NOT NULL,observed_at REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS chain_observations(kind TEXT PRIMARY KEY,payload TEXT NOT NULL,error TEXT)''',
    '''CREATE TABLE IF NOT EXISTS chain_sessions(token TEXT PRIMARY KEY,csrf TEXT NOT NULL,editing INTEGER NOT NULL,expires_at REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS chain_login_attempts(id INTEGER PRIMARY KEY,ip TEXT NOT NULL,created_at REAL NOT NULL)''',
]
