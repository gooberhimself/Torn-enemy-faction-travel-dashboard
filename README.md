# Torn Enemy Travel Dashboard

A lightweight, mobile-friendly Flask dashboard for monitoring an opposing Torn faction during a war. It polls Torn's read-only faction API, organizes members by travel status and destination, estimates flight arrival times, tracks hospital release times, and records observed online activity.

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

Flights already underway when the application starts or restarts receive a full-duration estimate. Flights detected after startup should be accurate to roughly the configured polling interval.

### Hospital dashboard

The separate `/hospital` page provides a focused view of hospitalized enemies.

- Lists all currently hospitalized faction members.
- Shows each release time in the viewer's local timezone.
- Displays a live release countdown.
- Sorts members by their soonest known release.
- Supports filtering by name, status, or location.
- Links each member to their Torn profile.

### Player activity timeline

The `/activity` page displays observed enemy activity on a 24-hour timeline.

- Shows one alphabetically sorted row for every known faction member, including members with no online activity that day.
- Draws green blocks for periods when successful faction polls observed a member as `Online`.
- Uses hourly grid lines from `00:00` through the following `00:00`.
- Shows a red current-time indicator when viewing today.
- Supports previous-day, next-day, and date-picker navigation without allowing future dates.
- Converts dates, hour positions, activity blocks, and the current-time indicator to each viewer's browser timezone.
- Scrolls horizontally on smaller screens so the hourly timeline remains readable.
- Links player names directly to their Torn profiles.

Activity is based specifically on each faction member's `last_action.status` value from the same faction API response already used by the travel dashboard. `Idle` and `Offline` are not counted as online. No additional Torn API requests or per-player requests are made for activity tracking.

The graph represents observations, not exact login and logout times. Its resolution depends on `POLL_SECONDS`, normally about one minute. Missing or failed polls are not filled in, so API outages and application downtime are not presented as known online activity.

The browser sends its IANA timezone name, such as `America/Chicago` or `Europe/Stockholm`, to `/api/activity`. Flask uses Python's built-in `zoneinfo` support to query the correct local calendar day and position intervals on that viewer's local 24-hour clock. Cloudflare Tunnel requires no timezone configuration and simply passes the request through normally.

#### Activity database

Activity history is stored in `activity.db`, an SQLite database created automatically in the project directory when the application starts. No database setup command is required.

To avoid storing one database row per player every minute, consecutive online observations are merged into a single interval. An offline observation closes that interval. If polling is interrupted, the interval ends at the last successful online observation instead of extending across the unknown period.

The database survives browser refreshes, Flask restarts, computer reboots, and periods when the dashboard is stopped. It is excluded from Git by `.gitignore` because it is runtime data. No history is deleted automatically.

### Live status changes

The travel dashboard keeps the 50 most recent detected status changes, making it easier to notice departures, arrivals, returns, and other status updates. This history is stored in memory and resets when the application restarts.

## How it works

The Flask application runs a background polling thread that requests the configured faction's basic member data from Torn. It classifies each member as traveling, abroad, returning, hospitalized, or another status, then exposes the processed state to the browser through `/api/status`. During that same successful poll, it records the member `last_action.status` values in SQLite for `/api/activity`; it does not make another Torn request.

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

## Configuration

Configure the application in `.env`:

| Field | Description |
| --- | --- |
| `TORN_API_KEY` | Your Torn API key. Keep this private. |
| `ENEMY_FACTION_ID` | The faction ID to monitor. |
| `POLL_SECONDS` | Torn API polling interval. Defaults to `60` and cannot run below `30`. |
| `HOST` | Listening address. Use `0.0.0.0` for access from another device on your LAN or VPN. |
| `PORT` | Listening port. Defaults to `8787`. |

The included `.env.example` can be copied as a starting point. The real `.env` file is excluded by `.gitignore` so API credentials are not committed.

## Important notes

- The dashboard uses Torn's read-only API and only displays information available through that API.
- Arrival countdowns are estimates because Torn does not expose enemy flight arrival timestamps.
- Travel observations, arrival estimates, and recent status changes are held in memory and reset when the application restarts. Activity timeline history is stored persistently in `activity.db`.
- SQLite support comes from Python's standard library. The activity feature adds no Python package dependencies and requires no one-time initialization command.
- The application does not include authentication. Do not expose it directly to the public internet without adding access controls.
- Never commit or share your Torn API key.
