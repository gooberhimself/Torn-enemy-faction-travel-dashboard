# Torn Enemy Travel Dashboard

A lightweight, mobile-friendly Flask dashboard for monitoring an opposing Torn faction during a war. It polls Torn's read-only faction API, organizes members by travel status and destination, estimates flight arrival times, and tracks hospital release times from one browser-friendly interface.

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

### Live status changes

The travel dashboard keeps the 50 most recent detected status changes, making it easier to notice departures, arrivals, returns, and other status updates. This history is stored in memory and resets when the application restarts.

## How it works

The Flask application runs a background polling thread that requests the configured faction's basic member data from Torn. It classifies each member as traveling, abroad, returning, hospitalized, or another status, then exposes the processed state to the browser through `/api/status`.

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
- Travel observations, arrival estimates, and recent changes are held in memory and reset when the application restarts.
- The application does not include authentication. Do not expose it directly to the public internet without adding access controls.
- Never commit or share your Torn API key.
