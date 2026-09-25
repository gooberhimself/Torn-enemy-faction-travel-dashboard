# Torn Enemy Travel Dashboard

A lightweight, mobile-friendly Flask dashboard for monitoring opposing and friendly Torn factions during a war. It polls Torn's read-only faction API, organizes members by travel status and destination, estimates flight arrival times, tracks hospital release times, and records observed enemy online activity.

## Features

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
- New enemy flight estimates use an established method; otherwise the original
  `TRAVEL_SECONDS` fallback remains unchanged. Explicit API arrival timestamps
  always take precedence. Estimates are fixed at first observation of each leg.

Published learning durations are in `travel_learning.py`, sourced from the
[Torn travel wiki](https://wiki.torn.com/wiki/Travel), checked September 25, 2026
(after the June 2026 travel-time update). The older fallback durations are deliberately
preserved for compatibility. Books, special delays, and hidden modifiers are not
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

The Flask application runs a background polling thread that requests basic member data for the configured enemy and friendly factions. Each polling cycle makes two faction-wide requests: one for `ENEMY_FACTION_ID` and one for `FRIENDLY_FACTION_ID`. It classifies each member as traveling, abroad, returning, hospitalized, or another status, then exposes the separate processed snapshots through `/api/status` and `/api/friendly/status`.

During the successful enemy poll, the application also records member `last_action.status` values in SQLite for `/api/activity`; activity recording does not make another Torn request. Friendly online activity is not stored.

The browser refreshes dashboard data every 30 seconds. The server-side Torn API polling interval is controlled by `POLL_SECONDS` and is limited to a minimum of 30 seconds.

## Setup

```bash
git clone https://github.com/gooberhimself/Torn-enemy-faction-travel-dashboard.git
cd Torn-enemy-faction-travel-dashboard
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python app.py
```

Open the travel dashboard at:

```text
http://YOUR_SERVER_IP:8787/
```

Open the hospital dashboard at:

```text
http://YOUR_SERVER_IP:8787/hospital
```

Open the activity timeline at:

```text
http://YOUR_SERVER_IP:8787/activity
```

Open the friendly dashboards at:

```text
http://YOUR_SERVER_IP:8787/friendly
http://YOUR_SERVER_IP:8787/friendly/hospital
```

## Configuration

Configure the application in `.env`:

| Field | Description |
| --- | --- |
| `TORN_API_KEY` | Your Torn API key. Keep this private. |
| `ENEMY_FACTION_ID` | The faction ID to monitor. |
| `FRIENDLY_FACTION_ID` | Your faction ID. Required for the friendly dashboards. |
| `POLL_SECONDS` | Torn API polling interval. Defaults to `60` and cannot run below `30`. |
| `HOST` | Listening address. Use `0.0.0.0` for access from another device on your LAN or VPN. |
| `PORT` | Listening port. Defaults to `8787`. |

The included `.env.example` can be copied as a starting point. The real `.env` file is excluded by `.gitignore` so API credentials are not committed.

## Important notes

- The dashboard uses Torn's read-only API and only displays information available through that API.
- With the default 60-second interval, enemy and friendly tracking together use approximately two Torn API requests per minute.
- Arrival countdowns are estimates because Torn does not expose enemy flight arrival timestamps.
- Enemy travel observations, flight checkpoints, and activity history persist in `activity.db`. Friendly arrival estimates and recent status changes remain in memory.
- SQLite support comes from Python's standard library. The activity feature adds no Python package dependencies and requires no one-time initialization command.
- The application does not include authentication. Do not expose it directly to the public internet without adding access controls.
- Never commit or share your Torn API key.
