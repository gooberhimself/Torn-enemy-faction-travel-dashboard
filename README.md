# Torn Enemy Tracker & Chain Coordinator

A mobile-friendly Flask application for coordinating sequential faction attacks and
monitoring enemy travel, hospital releases, and online activity in Torn.

**The coordinator controls who attacks next. Torn's API verifies what happened.**

## Chain Coordinator highlights

- **One clear attack order:** a large NEXT ATTACK panel, followed by ON DECK and UPCOMING assignments.
- **Live shared queue:** reorder by dragging or using touch/keyboard buttons; connected viewers see changes immediately.
- **Fast manual control:** Complete & Next advances immediately; skip and restore assignments without marking them completed.
- **Public viewing, protected editing:** viewers need no account or API key; coordinators unlock editing with a shared five-digit PIN.
- **Target availability:** searchable faction rosters, current enemy locations, hospital release estimates, and direct Torn profile/attack links.
- **Chain countdown:** updates every second locally, with preparation, attack-window, urgent, and critical warnings. It never initiates an attack.
- **Optional attack verification:** match eligible faction attack records, confirm manual completions, and advance confirmed chain-extending attacks automatically.
- **Persistent and mobile-friendly:** queue/history survive restarts; simultaneous edits are checked to prevent accidental overwrites.
- **Host for another faction:** manual coordination works without faction API permission; automatic verification can use an authorized key supplied by that faction.

The existing enemy/friendly travel dashboards, hospital lists, learned travel estimates,
and enemy activity timeline are included in the same application.

[Setup](#setup) · [Using the queue](#operating-the-queue) ·
[Manual or automatic verification](#choose-manual-or-automatic-verification) ·
[Hosting multiple factions](#hosting-multiple-factions) · [Tests](#tests)

## Setup

Requires Python 3.9+ and the packages in `requirements.txt`. From a new checkout:

```bash
git clone --branch feature/chain-coordinator https://github.com/gooberhimself/Torn-enemy-faction-travel-dashboard.git
cd Torn-enemy-faction-travel-dashboard
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

For an existing installation, keep your private environment files and databases.
Add any missing settings from `.env.example` without replacing your existing values.

1. Edit `.env`: set `TORN_API_KEY`, `FRIENDLY_FACTION_ID`, and `ENEMY_FACTION_ID`.
   `FRIENDLY_FACTION_ID` is the faction this instance serves, which need not be your own.
2. Choose [manual or automatic verification](#choose-manual-or-automatic-verification).
   Set `CHAIN_VERIFY_ATTACKS=false` if your key cannot read that faction's attack history.
3. Run `python scripts/hash_chain_pin.py`, enter and confirm your five-digit PIN,
   and put the generated `CHAIN_PIN_HASH` line in `.env` to enable editing.
4. For local HTTP testing, set `CHAIN_COOKIE_SECURE=false`. Keep it `true` when serving over HTTPS.
5. Start the application:

```bash
python app.py
```

Open `http://localhost:8787/chain` and select **Unlock Editing**. Anyone with access to
that URL can view the queue without unlocking. Adjust the host and port for your deployment.
SQLite tables initialize automatically; no separate database installation is required.

| Page | Path |
|---|---|
| Chain Coordinator | `/chain` |
| Enemy travel | `/` |
| Enemy hospital | `/hospital` |
| Friendly travel | `/friendly` |
| Friendly hospital | `/friendly/hospital` |
| Enemy activity timeline | `/activity` |

When `URL_PREFIX` is configured, prepend it to these paths, for example `/instance-b/chain`.
Use Ctrl+C to stop a manually started process. Restart it after changing its environment file.

## Configuration

Configure the application in `.env`:

| Field | Description |
| --- | --- |
| `TORN_API_KEY` | Your Torn API key. Keep this private. |
| `ENEMY_FACTION_ID` | The faction ID to monitor. |
| `FRIENDLY_FACTION_ID` | The faction served by this instance. Required for friendly dashboards and the coordinator. |
| `POLL_SECONDS` | Torn API polling interval. Defaults to `60` and cannot run below `30`. |
| `HOST` | Listening address. Use `0.0.0.0` for access from another device on your LAN or VPN. |
| `PORT` | Listening port. Defaults to `8787`. |
| `ENV_FILE` | Optional dotenv file selected when launching the process. Defaults to `.env`. |
| `ACTIVITY_DB_PATH` | SQLite file for activity/travel history. Defaults to `activity.db`. |
| `URL_PREFIX` | Public path prefix when a reverse proxy serves this instance below a subpath. |
| `ROOT_URL_HOSTS` | Optional comma-separated hostnames that serve this instance at `/`. |

The included `.env.example` can be copied as a starting point. The real `.env` file is excluded by `.gitignore` so API credentials are not committed.

## Chain Coordinator

Open `/chain` (or the instance prefix followed by `/chain`) from the dashboard navigation.
Everyone can view the sequential attack queue without an account or API key. Only the
first assignment is **NEXT ATTACK**, the second is **ON DECK**, and the rest are **UPCOMING**.
The coordinator decides when to attack; the application never initiates a Torn attack.
Profile and Attack Target links open Torn in a new tab.

### Choose manual or automatic verification

Both modes provide the same editable queue, chain countdown, enemy availability, live
browser updates, and manual Complete & Next control. Viewers never supply API keys.

| Mode | Configuration | Attack-history access | Completion behavior |
|---|---|---|---|
| Manual | `CHAIN_VERIFY_ATTACKS=false` | Not required; no attack-history request is made | Coordinator advances with Complete & Next; completion stays unverified |
| Automatic verification | `CHAIN_VERIFY_ATTACKS=true` | Limited-access key or custom faction-attacks key, owned by a friendly-faction member whose position grants API access | Eligible chain contributions can advance the current assignment; manual completion remains available |

To run without faction attack-history permission, add this to the instance's private
environment file and restart:

```dotenv
CHAIN_VERIFY_ATTACKS=false
```

**Hosting and API permissions are separate.** You can host this application for a faction
you do not belong to. For automatic verification, that faction must provide an authorized
key from a member with the required faction API permission. Put that key in the instance's
`TORN_API_KEY` setting. Your own account does not need to join the faction.

The public roster and chain requests do not require faction-role API permission. A chain
state of **inactive** means the last observation reported no active chain; it is not an
attack-history permission error. Missing attack access never prevents manual queue control.

### Configuration and PIN setup

No new Python dependencies are needed. Install the existing requirements, then run:

```bash
source venv/bin/activate
python scripts/hash_chain_pin.py
```

Enter and confirm your own five-digit PIN at the hidden prompts. Copy the printed
`CHAIN_PIN_HASH='...'` line into the selected private `.env` / `ENV_FILE` and restart.
The helper uses Werkzeug scrypt; the raw PIN is never written to a file or shell history.
Treat the verifier as a secret too: a five-digit PIN has limited entropy. Do not commit
it, your API key, or any local database. Leave `CHAIN_PIN_HASH` empty to disable unlocking.

| Variable | Default | Purpose |
|---|---|---|
| `CHAIN_PIN_HASH` | empty | scrypt verifier produced by the helper |
| `CHAIN_COOKIE_SECURE` | `true` | HTTPS-only cookie; use `false` only for local HTTP testing |
| `CHAIN_VERIFY_ATTACKS` | `true` | Enable the additional faction attack-history request |
| `ACTIVITY_DB_PATH` | `activity.db` beside app.py | Existing SQLite database, also stores coordinator tables |
| `CHAIN_DB_PATH` | `ACTIVITY_DB_PATH` | Optional separate SQLite file for this matchup's coordinator data |
| `POLL_SECONDS` | `60` | Existing interval, still clamped to at least 30 seconds |

`TORN_API_KEY`, `FRIENDLY_FACTION_ID`, and `ENEMY_FACTION_ID` retain their existing meanings.
Use a separate activity database for each hosted faction instance. The queue refuses to
load under a different faction pair rather than applying old assignments to a new enemy.
When changing `ENEMY_FACTION_ID`, keep `ACTIVITY_DB_PATH` unchanged and set `CHAIN_DB_PATH`
to a new absolute filename such as `/your/data/chain-<friendly-id>-<enemy-id>.db`, then
restart. This starts an empty queue while preserving activity/travel history and the old
queue in their existing files. There is no automatic queue deletion or reassignment.
With no `CHAIN_DB_PATH`, existing installations continue sharing the activity database.
Tables and indexes initialize automatically on startup. Existing tracking tables and data
are retained. Back up SQLite consistently (for example, SQLite's backup command) before deployment.

For a local HTTP test, set `CHAIN_COOKIE_SECURE=false` in your local environment, then:

```bash
python app.py
```

Open `http://localhost:8787/chain` (adjust `PORT` if configured). Keep the secure-cookie
setting enabled for the HTTPS deployment. No Flask signing secret is needed for the
coordinator: cookies contain random opaque session tokens, and only their hashes are
stored in SQLite. PIN verifiers and session tokens are never included in public state.

### Operating the queue

1. Select **Unlock Editing** and enter the PIN. Multiple coordinators may unlock independently.
2. Select **Add Assignment**, search the friendly roster, and select an enemy. The enemy
   picker shows status and location, with confirmed Okay members in Torn first. Hospitalized,
   traveling, and abroad members remain selectable. Status details show the last API timestamp.
3. Drag a queue row to reorder it. The **↑ / ↓** buttons provide touch and keyboard alternatives.
   Order saves immediately. A stale edit returns HTTP 409 and reloads authoritative state;
   review it before retrying. An open edit dialog retains the version at which it was opened.
4. **Complete & Next** immediately records `completed_unverified` and promotes the next
   attacker. It does not wait for Torn. **Skip / Defer** preserves the assignment without
   completing it; restore it from the deferred section. Editing and deletion apply only
   to queued/deferred assignments; completed history is retained.
5. Target reuse is prevented by default, including when restoring an assignment. Check
   the explicit reuse option when adding/editing if duplication is intentional.
6. **Lock Editing** revokes this browser's session. Sessions also expire after 30 minutes
   without user interaction. Viewer refreshes and SSE messages do not extend that time.

Queue mutations require server-side authorization and a per-session CSRF token. SQLite
transactions, database constraints, a required queue version, and the expected head ID
prevent silent overwrites and repeated advancement. Failed unlocks are limited to five
per source address per 15 minutes, with an instance-wide limit of 25. These limits persist
across restarts. Behind a proxy, addresses may share the same limit; the app deliberately
does not trust arbitrary forwarded IP headers. HTTPS, restricted backend access, and file
permissions on the database and environment files remain necessary. The public queue is
visible to anyone with access to the site; the PIN restricts editing only.

To revoke all coordinator sessions after changing a PIN, stop the instance, remove only
rows from `chain_sessions` in the coordinator database using SQLite
(`DELETE FROM chain_sessions;`), and restart.
Do not delete the database to rotate the PIN; it contains tracking and queue history.

### API observations and verification

The integration follows Torn's [official v2 schema](https://www.torn.com/swagger/openapi.json)
and [chain rules](https://wiki.torn.com/wiki/Chain):

- `GET /v2/faction?selections=chain,timestamp&id=<friendly faction>` uses public access.
  The response timestamp anchors `timeout`; receiving the same cached response does not
  reset the countdown. Below ten hits the UI labels the warm-up period, and cooldown is
  displayed separately from the normal chain timer.
- `GET /v2/faction?selections=attacks,timestamp&filters=outgoing&sort=DESC&limit=100`
  requests the documented faction attack selection with its API timestamp and requires a **limited key
  with faction API access**, owned by a member of the configured friendly faction.
  Missing permissions or a mismatched faction produce a visible status and leave manual
  coordination available. `CHAIN_VERIFY_ATTACKS=false` disables this request.
- Friendly and enemy rosters reuse the existing separate v1 faction polls, now selecting
  `basic,timestamp`. No per-viewer Torn requests or separate availability poller are created.
  The documented v2 `/faction/{id}/members` endpoint is available, but replacing the working
  roster integration is unnecessary.

**Troubleshooting attack API error 7:** Torn calls this an "Incorrect ID-entity relation"
([error-code documentation](https://www.torn.com/api.html)). Public chain/roster data can
still work while access to faction attack history is denied. Ask a faction leader to
check that the key owner's faction position grants **API access**, and confirm that the
key owner belongs to `FRIENDLY_FACTION_ID`. Faction-role permission and key access level
are separate requirements; generating a stronger key alone does not grant faction-role
permission. Error **16** instead indicates insufficient key access. After a role change,
allow the next polling cycle to retry. If you replace `TORN_API_KEY` in the instance's
private environment file, restart that instance. To use manual coordination without
attack-history requests, set `CHAIN_VERIFY_ATTACKS=false` and restart.

Browser timers tick once per second using server-synchronized time. Observations older
than `2 * max(POLL_SECONDS, 30) + 30` seconds, failed updates, and unavailable observations
are clearly identified. Hospital expiry shows **Expected Release — Awaiting Confirmation**;
only a fresh Okay status with a recognized Torn location is listed as available. Timer
warnings are centralized in `chain_coordinator.WARNINGS` (120/60/30/15 seconds).

Attack IDs are persisted and consumed once. Reconciliation checks attacker, defender,
faction, assignment eligibility time, active-turn time, outcome, a positive API `chain`
value, and an explicit non-interrupted result. Only unambiguous current-turn contributions
advance automatically. Non-contributing or out-of-turn matching attacks require review.
Both `chain=0` and `chain=null` are treated as non-confirmation. Manual completions retain
their original timestamp when verified; they never advance the queue a second time.
Conflicting evidence retains an audit event and flags review rather than rewriting history.

Conservative matching limits: manual evidence must end between five minutes before and
two minutes after the recorded manual completion; ambiguous repeated attacker/target pairs
are flagged for review. Same-second assignment/attack starts may require manual confirmation.
The adapter inspects the latest 100 outgoing attacks each cycle, with persistent ID deduplication;
it does not issue unbounded pagination requests. A full page displays a coverage warning.
After a long outage or very busy period, older attacks may remain unverified. An API error
never constitutes evidence that an attack failed. A review flag is retained in event history;
a coordinator can complete/skip a queued item or leave completed evidence for review.

### Hosting multiple factions

Run a separate process with a separate private environment file, port, and activity
SQLite file for each faction. For example, create `.env.instance-b` from `.env.example`
and configure its own faction IDs, `PORT=8788`, and an absolute `ACTIVITY_DB_PATH`.
Choose a coordinator PIN for that instance and set `CHAIN_VERIFY_ATTACKS` for its key's access.

```bash
ENV_FILE="$PWD/.env.instance-b" python app.py
```

For a deployment using a custom file such as `.env.faction-name`:

```bash
ENV_FILE="$PWD/.env.faction-name" python app.py
```

Environment files and SQLite databases stay local and are excluded from Git. Exported
shell/service environment variables take precedence over values in the selected dotenv file.
Do not share a database between simultaneously running instances. See [deployment instructions](deploy/README.md)
for nginx prefixes, streaming settings, and systemd service examples.

When starting a new war, update `ENEMY_FACTION_ID`, keep the faction's activity database,
and set `CHAIN_DB_PATH` to a new filename for the new matchup. Restart to start an empty
queue. Existing activity/travel history and previous assignments remain in their original files.
A message saying the saved queue belongs to different factions means the selected queue
file still belongs to the previous matchup; do not delete the activity database to fix it.

### Live updates, storage, and deployment

`chain_coordinator.py` contains storage, authorization, HTTP routes and SSE;
`chain_torn.py` isolates the v2 adapter. SQLite stores one queue, assignments, audit events,
observed attack IDs, coordinator snapshots, temporary sessions, and unlock attempts alongside
the existing activity/travel tables by default, or in `CHAIN_DB_PATH` when configured.
Live tracking remains in memory; only the coordinator's
last published roster snapshot is persisted to support recovery and stale-data display.
Timestamps are UTC Unix seconds. Initialization is idempotent; this is the first coordinator schema.

`/api/chain/events` sends a full versioned state initially, on changes, and after reconnects.
A local notification wakes connected viewers after commits; a one-second database revision
check also catches commits from another connection. Heartbeats run every 15 seconds. Browsers
reconnect automatically and refresh state every 30 seconds as a fallback; these reads never
call Torn. Optimistic drag changes reconcile to server order, and failed requests restore
confirmed order. Database queue versions protect edits while a separate revision also
tracks observation changes.

The existing deployment is one Python process and one background poll thread per instance.
Keep that model: importing `app` into an arbitrary WSGI worker does **not** start the poller,
and starting multiple pollers would duplicate API usage. SSE occupies a request thread per
viewer; this is intended for a modest faction audience. The existing `python app.py` service
uses Flask's development server; a hardened production WSGI deployment requires explicit
single-poller startup and sufficient threads for streams. Do not enable the debug reloader.

For the supplied systemd/nginx deployment:

1. Update code/dependencies and the instance's private environment file, preserving its
   `URL_PREFIX`, faction IDs, and unique `ACTIVITY_DB_PATH`.
2. Configure the PIN verifier and keep `CHAIN_COOKIE_SECURE=true` behind HTTPS.
3. Apply the SSE settings in `deploy/nginx.conf`: buffering/cache off and a 75-second
   read timeout. Preserve streaming in any additional reverse proxy or tunnel.
4. Restart the existing instance service. Verify `/chain`, the ordinary dashboards,
   a second read-only browser, and an unlock/lock cycle through the actual public prefix.

### Tests

Tests use temporary SQLite files and mocked API responses, never a live Torn key:

```bash
venv/bin/python -m unittest discover -s tests -v
node tests/test_chain_time.js
```

The Python suite covers access control, CSRF, lockout, expiry, queue mutations, duplicate
targets, simultaneous edits, persistence, SSE reconnects, and conservative reconciliation.
The dependency-free Node tests check countdown synchronization, warning boundaries, hospital
expiry, invalid timestamps, and stale observations. Node is needed only for these JS tests.

## Other dashboard features

### Travel dashboard

- Shows live totals for enemies who are traveling, abroad, or returning to Torn.
- Groups traveling and abroad members by destination.
- Identifies each foreign location as safe or unsafe based on visible enemy activity.
- Treats a location as unsafe when an enemy is traveling there, currently abroad there, or hospitalized there.
- Does not mark a location unsafe when the only visible enemy is returning to Torn.
- Links every member directly to their Torn profile.
- Includes a name, status, and destination filter.

### Estimated flight arrivals

Torn does not provide enemy flight arrival timestamps through the faction endpoint. The dashboard works around this by recording the first poll where a flight is observed and adding the route's travel duration.

- Displays an estimated local landing or return time.
- Shows a live countdown that updates every second.
- Recognizes both outbound descriptions and return descriptions such as `Traveling from UAE to Torn`.
- Keeps the estimated arrival stable across later API polls.

Enemy flights retain their first observed departure and fixed estimate across restarts. A flight first encountered already underway receives a full-duration estimate and cannot contribute to learning. Friendly estimates remain in memory. Estimates are approximate because departure is first observed after it happens, and actual flight times vary.

### Learned enemy travel methods

The same successful enemy polls now record travel observations in `activity.db`; no
extra Torn requests are made. The enemy rows display the inferred normal method,
confidence, number of reliable observed legs, occasional Business Class use, and
possible behavior changes. Hover over the profile line for lifetime match counts.
The displayed normal method is an inference, not proof of the current flight's method.

- A departure must be bounded by a recent known home status (or the correct abroad
  location for a return). Arrival must be explicitly observed at the destination,
  including a foreign hospital, or back home for a return.
- A gap greater than 1.5 times the configured polling interval (minimum interval
  30 seconds) invalidates that leg for learning, including across a restart.
  Unobserved departures, missed arrivals, and unmatched or ambiguous durations are
  also excluded. Their observations and rejection reasons are retained.
- Matching compares all four published route durations. It uses the actual
  departure/arrival polling gaps, 3% flight variation, and 30 seconds of table
  rounding. Exactly one method must be plausible. The nearest method and absolute
  error are stored even when an arrived leg is rejected.
- Normal methods require at least three reliable matches and 75% of the latest
  five non-Business legs. Three consecutive matches can establish a new method.
  High confidence requires at least six legs and 87.5% agreement in the latest
  eight non-Business legs. Two consecutive conflicting legs against an established
  older pattern suspend the prediction; a third can establish the new method.
- Business matches are remembered separately. Occasional tickets do not replace
  the normal method; at least five of the latest six reliable legs must be Business
  to predict Business normally. High Business confidence uses the latest eight
  reliable legs. A detected behavior change limits confidence to Likely.
- Inference uses the latest 20 reliable legs within 90 days. Lifetime observations
  remain stored. Rejected legs never vote or count toward confidence.
- New enemy flight estimates use an established method; otherwise the
  `TRAVEL_SECONDS` fallback uses the wiki's Standard durations without the book.
  Friendly flight estimates use the same Standard durations. Explicit API arrival timestamps
  always take precedence. Estimates are fixed at first observation of each leg.

Published learning durations are in `travel_learning.py`, sourced from the
[Torn travel wiki](https://wiki.torn.com/wiki/Travel), checked September 25, 2026
(after the June 2026 travel-time update). The Standard fallback durations share
this table. Books, special delays, and hidden modifiers are not
modeled: unmatched timings are rejected, but a modifier that mimics another method
cannot be distinguished from duration alone. Property ownership is never used.

`travel_tracking` stores each faction/player's latest checkpoint and active flight.
`travel_observations` stores each leg's player ID, faction, destination, direction,
first observed departure/arrival, duration, nearest method, error, expected duration,
tolerance, eligibility/rejection reason, and API status/travel/plane metadata.
Unique departure/direction keys and atomic checkpoints prevent duplicate observations
from repeated polls. Profiles follow the player ID across enemy factions; active
flight checkpoints remain faction-scoped. No existing activity tables are changed.
Schema creation is automatic and additive at startup. Learning runs whenever the
configured enemy faction is monitored, before or during a war.

Run the offline simulations (temporary databases, no Torn requests) with:

```bash
venv/bin/python -m unittest discover -s tests -v
```

### Hospital dashboard

The separate `/hospital` page provides a focused view of hospitalized enemies.

- Lists all currently hospitalized faction members.
- Shows each release time in the viewer's local timezone.
- Displays a live release countdown.
- Sorts members by their soonest known release.
- Supports filtering by name, status, or location.
- Links each member to their Torn profile.

### Friendly travel and hospital dashboards

The friendly pages mirror the enemy travel and hospital tools for your own faction:

- `/friendly` groups friendly travelers by destination and shows estimated landing and return countdowns.
- `/friendly/hospital` lists hospitalized friendlies with release times and live countdowns.
- Friendly travel intentionally omits the enemy-focused safe-location calculation.
- Friendly and enemy status and changes are kept in separate in-memory state; enemy flight checkpoints additionally persist in SQLite.
- Friendly data comes from one additional faction-wide API request per polling cycle, never one request per player.

### Player activity timeline

The `/activity` page displays observed enemy activity on a 24-hour timeline.

- Shows one alphabetically sorted row for every known faction member, including members with no online activity that day.
- Draws green blocks for periods when successful faction polls observed a member as `Online`.
- Uses hourly grid lines from `00:00` through the following `00:00`.
- Shows a red current-time indicator when viewing today.
- Supports previous-day, next-day, and date-picker navigation without allowing future dates.
- Converts dates, hour positions, activity blocks, and the current-time indicator to each viewer's browser timezone.
- Keeps player rosters and activity intervals separated by `ENEMY_FACTION_ID`, so changing war targets does not mix factions on the graph.
- Scrolls horizontally on smaller screens so the hourly timeline remains readable.
- Links player names directly to their Torn profiles.

Activity is based specifically on each faction member's `last_action.status` value from the same faction API response already used by the travel dashboard. `Idle` and `Offline` are not counted as online. No additional Torn API requests or per-player requests are made for activity tracking.

The graph represents observations, not exact login and logout times. Its resolution depends on `POLL_SECONDS`, normally about one minute. Missing or failed polls are not filled in, so API outages and application downtime are not presented as known online activity.

The browser sends its IANA timezone name, such as `America/Chicago` or `Europe/Stockholm`, to `/api/activity`. Flask uses Python's built-in `zoneinfo` support to query the correct local calendar day and position intervals on that viewer's local 24-hour clock. Cloudflare Tunnel requires no timezone configuration and simply passes the request through normally.

#### Activity database

Activity history is stored in `activity.db`, an SQLite database created automatically in the project directory when the application starts. No database setup command is required.

To avoid storing one database row per player every minute, consecutive online observations are merged into a single interval. An offline observation closes that interval. If polling is interrupted, the interval ends at the last successful online observation instead of extending across the unknown period.

The database survives browser refreshes, Flask restarts, computer reboots, and periods when the dashboard is stopped. It is excluded from Git by `.gitignore` because it is runtime data. No history is deleted automatically.

Each player and activity interval is stored with the configured `ENEMY_FACTION_ID`. Switching to another faction starts or resumes that faction's separate timeline; switching back restores the earlier faction's known roster and history.

Databases created before faction-scoped storage are upgraded automatically at startup. During this one-time migration, players marked active by the most recent poll are assigned to the currently configured faction. Older inactive players and their intervals are preserved in a hidden legacy scope because the previous schema did not record which faction owned them.

### Live status changes

The travel dashboard keeps the 50 most recent detected status changes, making it easier to notice departures, arrivals, returns, and other status updates. This history is stored in memory and resets when the application restarts.

## How it works

The Flask application runs a background polling thread that requests basic member data for the configured enemy and friendly factions. Each polling cycle makes two faction-wide roster requests: one for `ENEMY_FACTION_ID` and one for `FRIENDLY_FACTION_ID`. The Chain Coordinator adds one v2 chain request and, when enabled, one v2 attack-history request in the same cycle; it does not shorten the polling interval. It classifies each member as traveling, abroad, returning, hospitalized, or another status, then exposes the separate processed snapshots through `/api/status` and `/api/friendly/status`.

During the successful enemy poll, the application also records member `last_action.status` values in SQLite for `/api/activity`; activity recording does not make another Torn request. Friendly online activity is not stored.

The browser refreshes dashboard data every 30 seconds. The server-side Torn API polling interval is controlled by `POLL_SECONDS` and is limited to a minimum of 30 seconds.

## Important notes

- The dashboard uses Torn's read-only API and only displays information available through that API.
- Each polling cycle makes two roster requests and one chain request, plus one attack-history request when verification is enabled. Browser timers and additional viewers do not create Torn requests.
- Arrival countdowns are estimates because Torn does not expose enemy flight arrival timestamps.
- Enemy travel observations, flight checkpoints, and activity history persist in `activity.db`. Friendly arrival estimates and recent status changes remain in memory.
- SQLite support comes from Python's standard library. The activity feature adds no Python package dependencies and requires no one-time initialization command.
- Dashboard and queue viewing are public to anyone who can reach the site. The coordinator PIN protects editing only; use deployment access controls if viewing should be private.
- Never commit or share your Torn API key.
