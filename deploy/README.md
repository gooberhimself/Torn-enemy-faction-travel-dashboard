# Two faction instances

The Flask routes remain unprefixed. nginx strips `/instance-a/` and
`/instance-b/` before forwarding. Each process uses the same code and
its own configuration and database.

## Configuration

`ENV_FILE=/absolute/path/to/file` selects a dotenv file instead of `.env`.
An explicitly selected missing file fails startup. Existing process environment
variables take precedence over dotenv values. With no `ENV_FILE`, the existing
`.env` behavior is unchanged.

`URL_PREFIX` controls template navigation, stylesheet and API URLs. Empty means
root URLs. `ROOT_URL_HOSTS` is a comma-separated list of hostnames that always
use root URLs; it defaults to empty. Configure the legacy hostnames in the private environment file.
This lets the old domain continue reaching port 8787 directly. Keep Cloudflare's
original Host header (do not override it to localhost).

`ACTIVITY_DB_PATH` defaults to the existing `activity.db`. Use an absolute path
and an existing writable parent directory. Instance A keeps that database and its
history; Instance B uses `activity-instance-b.db`. Separate files
also isolate learned travel observations. Startup closes all open activity
intervals, so sharing the file between running processes is unsafe even though
most records contain faction IDs.

The coordinator also checks its saved friendly/enemy faction pair. When an instance
starts tracking a new enemy, leave its activity database in place and set
`CHAIN_DB_PATH=/absolute/path/to/chain-friendlyID-enemyID.db` to a new file in that
instance's private environment file. Restart the instance to initialize an empty queue.
The old queue (including pending assignments) and activity history remain intact; do not
delete the activity database or change the old queue's faction IDs to bypass this check.
If `CHAIN_DB_PATH` is omitted, coordinator storage defaults to `ACTIVITY_DB_PATH`.

From the repository root, create private configuration files:

```bash
install -m 600 deploy/instance-a.env.example .env.instance-a
install -m 600 deploy/instance-b.env.example .env.instance-b
```

Do this only if those files do not already exist. Edit both locally: enter API
keys. Instance B uses enemy `20001`, friendly `20002`. Never commit the private files.
The example Instance A IDs are enemy `10001`, friendly `10002`.

Test Instance B first, leaving the existing manual Instance A process alone:

```bash
ENV_FILE="$PWD/.env.instance-b" venv/bin/python app.py
```

In another terminal:

```bash
curl -f http://127.0.0.1:8788/api/status
curl -f http://127.0.0.1:8080/instance-b/
curl -f http://127.0.0.1:8080/instance-b/api/status
curl -f http://127.0.0.1:8080/instance-b/api/friendly/status
curl -f 'http://127.0.0.1:8080/instance-b/api/activity?timezone=UTC'
curl -f http://127.0.0.1:8080/instance-b/static/style.css
```

Wait for a successful poll and verify the configured factions in the browser.
An HTTP 200 alone does not prove Torn credentials work: inspect the API `error`
field and member data too. Do not run two processes against the same database,
including a temporary process on another port.

## nginx

`nginx.conf` preserves the supplied routes and adds `absolute_redirect off;`.
Without it, slash redirects can expose `http://...:8080` through the HTTPS
Tunnel. nginx documents relative redirects here:
https://nginx.org/en/docs/http/ngx_http_core_module.html#absolute_redirect

After reviewing the diff, back up the active configuration and install it:

```bash
sudo cp -a /etc/nginx/sites-available/torncity-travel-tracker /etc/nginx/sites-available/torncity-travel-tracker.backup
sudo install -m 644 deploy/nginx.conf /etc/nginx/sites-available/torncity-travel-tracker
sudo nginx -t
sudo systemctl reload nginx
curl -I -H 'Host: tracker.example.com' http://127.0.0.1:8080/instance-a
curl -I -H 'Host: tracker.example.com' http://127.0.0.1:8080/instance-b
```

Expect `Location: /instance-a/` and `Location: /instance-b/`.

## systemd migration

The unit uses placeholder account `tracker` and installation path `/opt/torn-tracker`.
Replace both with your local account and installation path before installing it.
The environment examples use illustrative IDs; replace them with your faction IDs.
It launches `app.py` directly to preserve the poll thread started in `__main__`.
This retains the existing Flask development server; systemd adds supervision,
not a production WSGI server. A later WSGI migration needs an explicit plan for
starting exactly one poller per instance.

```bash
sudo install -m 644 deploy/torn-tracker@.service /etc/systemd/system/torn-tracker@.service
sudo systemctl daemon-reload
```

Stop the manual Instance B process with Ctrl-C before:

```bash
sudo systemctl enable --now torn-tracker@instance-b
sudo systemctl status torn-tracker@instance-b
```

Instance A requires a brief restart to load the Python changes. Prepare and review
`.env.instance-a` first. Stop its manual process with Ctrl-C, then immediately run:

```bash
sudo systemctl enable --now torn-tracker@instance-a
sudo systemctl status torn-tracker@instance-a
```

Do not start this service while the manual Instance A process is running: besides
the port conflict, importing the app initializes its database. For rollback,
stop the Instance A service before returning to `python3 app.py` with the existing
`.env`. Neither configuration changes the old Cloudflare route.

```bash
journalctl -u torn-tracker@instance-a -n 50 --no-pager
journalctl -u torn-tracker@instance-b -n 50 --no-pager
```

systemd unit reference: https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html

## Verify both Instance A entry points before Cloudflare changes

```bash
curl -f -H 'Host: legacy.example.com' http://127.0.0.1:8787/
curl -f -H 'Host: legacy.example.com' http://127.0.0.1:8787/api/status
curl -f -H 'Host: tracker.example.com' http://127.0.0.1:8080/instance-a/
curl -f http://127.0.0.1:8080/instance-a/api/status
curl -f http://127.0.0.1:8080/instance-a/api/friendly/status
curl -f 'http://127.0.0.1:8080/instance-a/api/activity?timezone=UTC'
curl -f http://127.0.0.1:8080/instance-a/static/style.css
```

The old Host must render `/hospital`, `/api/status`, and `/static/style.css`.
The new Host must render those URLs with `/instance-a`. Visit every navigation tab,
check CSS, and change the Activity date. Verify the old public site too.
A running process must be restarted to load the new Python configuration;
coordinate deployment of templates with that restart if deploying to a live copy.

Only after both instances pass local checks, add a published application route
in the remotely managed `torntunnel` dashboard:

- Hostname: `tracker.example.com`
- Service: `http://127.0.0.1:8080`
- Path: leave empty (nginx handles both prefixes)

Add `www.tracker.example.com` separately only if wanted. Leave the
`legacy.example.com -> http://127.0.0.1:8787` route, cloudflared service,
and token file unchanged. No cloudflared config.yml is needed.
Repeat browser/API checks on both public prefixed URLs and on the old domain.

## Automated checks

```bash
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m unittest discover -s tests -v
```

Tests isolate all database writes in temporary storage and do not start polling.
