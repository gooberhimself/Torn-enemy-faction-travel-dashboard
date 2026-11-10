"""Opaque SQLite sessions and administrator-configured role PINs.

PIN verifiers remain in process configuration. SQLite stores only a configuration
fingerprint and a monotonically increasing generation for each role.
"""
import hashlib
import hmac
import re
import secrets
import time

from werkzeug.security import check_password_hash

SESSION_SECONDS = 30 * 60
LOCKOUT_SECONDS = 15 * 60
ROLES = ('public', 'tactician')


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class SessionAccess:
    def __init__(self, coordinator, config, error_class):
        self.coordinator = coordinator
        self.config = config
        self.error = error_class

    def verifier(self, role):
        return self.config.get(role.upper() + '_PIN_HASH', '') or ''

    def synchronize_pins(self):
        """Run on startup, even when sessions survived a process restart.

        A role's generation never decreases, including when a previous verifier
        is restored. Old processes also fail checks against the stored fingerprint
        after a new configuration has been installed.
        """
        changed = False
        with self.coordinator.db(write=True) as db:
            self.coordinator.queue(db)
            for role in ROLES:
                fingerprint = digest(self.verifier(role))
                old = db.execute('SELECT * FROM chain_pin_roles WHERE role=?', (role,)).fetchone()
                if old and old['fingerprint'] == fingerprint:
                    continue
                db.execute('DELETE FROM chain_sessions WHERE role=?', (role,))
                db.execute('''INSERT INTO chain_pin_roles(role,fingerprint,generation) VALUES(?,?,1)
                    ON CONFLICT(role) DO UPDATE SET fingerprint=excluded.fingerprint,generation=generation+1''',
                           (role, fingerprint))
                changed = True
            if changed:
                db.execute('UPDATE chain_queue SET auth_revision=auth_revision+1 WHERE id=1')
                self.coordinator.bump(db, False)
        if changed:
            self.coordinator.notify()

    def credential(self, db, role):
        row = db.execute('SELECT * FROM chain_pin_roles WHERE role=?', (role,)).fetchone()
        if not self.verifier(role) or not row or not hmac.compare_digest(row['fingerprint'], digest(self.verifier(role))):
            return None
        return row

    def identity(self, db, raw):
        row = db.execute('SELECT * FROM chain_sessions WHERE token=?', (digest(raw),)).fetchone() if raw else None
        if not row or row['expires_at'] <= time.time():
            return None
        if row['role']:
            credential = self.credential(db, row['role'])
            if not credential or row['credential_generation'] != credential['generation']:
                return None
        return dict(row)

    def viewer(self, raw):
        with self.coordinator.db(write=True) as db:
            row = self.identity(db, raw)
            if not row:
                if raw:
                    db.execute('DELETE FROM chain_sessions WHERE token=?', (digest(raw),))
                db.execute('DELETE FROM chain_sessions WHERE expires_at<=?', (time.time(),))
                raw = secrets.token_urlsafe(32)
                db.execute('''INSERT INTO chain_sessions(token,csrf,editing,expires_at)
                    VALUES(?,?,0,?)''', (digest(raw), secrets.token_urlsafe(32), time.time()+SESSION_SECONDS))
                row = self.identity(db, raw)
        return row, raw

    def csrf(self, row, token):
        if not row or not hmac.compare_digest(row['csrf'], token):
            raise self.error('Invalid or expired CSRF token. Refresh the page.', 403)

    def authorize(self, db, raw, csrf, permission='session'):
        """Caller holds BEGIN IMMEDIATE through authorization and its mutation."""
        row = self.identity(db, raw)
        if not row or row['role'] not in ROLES:
            raise self.error('Sign in with a public or tactician PIN.', 401)
        self.csrf(row, csrf)
        if permission == 'tactician' and row['role'] != 'tactician':
            raise self.error('Only tacticians can lock or unlock the board.', 403)
        if permission == 'edit' and row['role'] == 'public' and self.coordinator.queue(db)['locked']:
            raise self.error('The board is locked. Public access is read-only until a tactician unlocks it.', 403)
        db.execute('UPDATE chain_sessions SET expires_at=? WHERE token=?',
                   (time.time()+SESSION_SECONDS, row['token']))
        return row

    def login(self, raw, csrf, pin, address):
        now = time.time()
        ip = digest(address or 'unknown')  # Do not trust arbitrary forwarded headers.
        with self.coordinator.db(write=True) as db:
            self.csrf(self.identity(db, raw), csrf)
            if not any(self.verifier(role) for role in ROLES):
                raise self.error('Public and tactician PINs have not been configured.', 503)
            db.execute('DELETE FROM chain_login_attempts WHERE created_at<=?', (now-LOCKOUT_SECONDS,))
            total = db.execute('SELECT COUNT(*) FROM chain_login_attempts').fetchone()[0]
            own = db.execute('SELECT COUNT(*) FROM chain_login_attempts WHERE ip=?', (ip,)).fetchone()[0]
            if total >= 25 or own >= 5:
                raise self.error('Too many PIN attempts. Try again in 15 minutes.', 429)
            attempt = db.execute('INSERT INTO chain_login_attempts(ip,created_at) VALUES(?,?)', (ip, now)).lastrowid
            verifiers = {role: self.verifier(role) for role in ROLES}

        # Expensive hashing runs outside the write lock, after reserving an attempt.
        matches = []
        if isinstance(pin, str) and re.fullmatch(r'[0-9]{5}', pin):
            for role, verifier in verifiers.items():
                try:
                    if verifier and check_password_hash(verifier, pin):
                        matches.append(role)
                except (ValueError, TypeError):
                    pass
        if not matches:
            raise self.error('Invalid PIN.', 401)
        if len(matches) != 1:
            raise self.error('Public and tactician PINs must be different. Contact the server administrator.', 503)
        role = matches[0]
        with self.coordinator.db(write=True) as db:
            # Recheck expiry, CSRF and credential configuration after hashing.
            self.csrf(self.identity(db, raw), csrf)
            credential = self.credential(db, role)
            if not credential or digest(verifiers[role]) != credential['fingerprint']:
                raise self.error('PIN configuration changed. Refresh and sign in again after the server restarts.', 503)
            db.execute('DELETE FROM chain_login_attempts WHERE id=?', (attempt,))
            db.execute('DELETE FROM chain_sessions WHERE token=?', (digest(raw),))
            new_raw = secrets.token_urlsafe(32)
            db.execute('''INSERT INTO chain_sessions(token,csrf,editing,expires_at,role,credential_generation)
                VALUES(?,?,1,?,?,?)''', (digest(new_raw), secrets.token_urlsafe(32), time.time()+SESSION_SECONDS,
                                       role, credential['generation']))
        return new_raw
