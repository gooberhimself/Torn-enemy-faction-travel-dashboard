# Torn Enemy Travel Dashboard

Mobile-friendly dashboard for tracking an opposing faction's travel/abroad/returning statuses during war.

## Setup

```bash
cd torn_enemy_travel_dashboard
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python app.py
```

Then open:

```text
http://YOUR_SERVER_IP:8787
```

## .env fields

- `TORN_API_KEY`: your Torn API key.
- `ENEMY_FACTION_ID`: enemy faction ID.
- `POLL_SECONDS`: default 60. Do not set below 30.
- `HOST`: use `0.0.0.0` to access from your iPhone on LAN/VPN.
- `PORT`: default `8787`.

## Notes

This uses Torn's read-only API and only displays status data available through the API. Keep your API key private and avoid exposing this app directly to the public internet without adding authentication.
