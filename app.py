import os
import re
import sqlite3
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from threading import Lock, Thread

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

API_KEY = os.getenv("TORN_API_KEY", "").strip()
FACTION_ID = os.getenv("ENEMY_FACTION_ID", "").strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8787"))
ACTIVITY_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "activity.db")

if not API_KEY or not FACTION_ID:
    print("Missing TORN_API_KEY or ENEMY_FACTION_ID. Copy .env.example to .env and edit it.")

app = Flask(__name__)
lock = Lock()
state = {
    "updated_at": None,
    "error": None,
    "members": [],
    "changes": [],
    "last_seen": {},
    "travel_observed": {},
}

COUNTRIES = [
    "Mexico", "Cayman Islands", "Canada", "Hawaii", "United Kingdom", "Argentina",
    "Switzerland", "Japan", "China", "UAE", "South Africa"
]

# Torn status text may use adjectives instead of exact country names, especially
# for hospitals, e.g. "In an Emirati hospital" instead of "In a UAE hospital".
COUNTRY_ALIASES = {
    "Mexico": ["mexico", "mexican"],
    "Cayman Islands": ["cayman islands", "cayman"],
    "Canada": ["canada", "canadian"],
    "Hawaii": ["hawaii", "hawaiian"],
    "United Kingdom": ["united kingdom", "uk", "britain", "british"],
    "Argentina": ["argentina", "argentinian", "argentine"],
    "Switzerland": ["switzerland", "swiss"],
    "Japan": ["japan", "japanese"],
    "China": ["china", "chinese"],
    "UAE": ["uae", "emirati", "emirates", "united arab emirates"],
    "South Africa": ["south africa", "south african"],
}

# Locations considered unsafe when at least one enemy is there, headed there,
# or hospitalized there. Returning enemies do not make that foreign country
# unsafe, because they are headed back to Torn.
UNSAFE_DESTINATION_STATES = {"Traveling", "Abroad", "Hospital"}

# One-way base travel times in seconds. When Torn does not expose an enemy's
# arrival timestamp, the dashboard adds the route time to the first poll where
# that flight is observed. The resulting ETA can be late by up to one poll.
TRAVEL_SECONDS = {
    "Mexico": 1600,
    "Cayman Islands": 2100,
    "Canada": 2500,
    "Hawaii": 8100,
    "United Kingdom": 9600,
    "Argentina": 10000,
    "Switzerland": 10500,
    "Japan": 13500,
    "China": 14500,
    "UAE": 16200,
    "South Africa": 17800,
}


def activity_db():
    connection = sqlite3.connect(ACTIVITY_DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_activity_db():
    """Create activity storage and end intervals left open by an old process.

    Each online run is stored as one interval instead of one row per poll. An
    interval's end is advanced only by successful observations, so application
    downtime cannot be mistaken for online activity.
    """
    with activity_db() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS activity_players (
                player_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                profile_url TEXT NOT NULL,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS activity_intervals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id TEXT NOT NULL,
                start_time INTEGER NOT NULL,
                end_time INTEGER NOT NULL,
                last_observed INTEGER NOT NULL,
                is_open INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (player_id) REFERENCES activity_players(player_id)
            );

            CREATE INDEX IF NOT EXISTS activity_intervals_day
                ON activity_intervals(start_time, end_time);
            CREATE INDEX IF NOT EXISTS activity_intervals_player
                ON activity_intervals(player_id, start_time);
            CREATE UNIQUE INDEX IF NOT EXISTS activity_one_open_interval
                ON activity_intervals(player_id) WHERE is_open = 1;
        """)
        # A previous process cannot vouch for the time while it was stopped.
        connection.execute("UPDATE activity_intervals SET is_open = 0 WHERE is_open = 1")


def record_activity_observations(members, observed_at):
    """Persist one faction-wide presence observation from the existing poll.

    Torn exposes member presence as last_action.status. Only the exact Online
    value is recorded as online; Idle and Offline close an interval. A large
    gap splits activity instead of filling unobserved time during an API outage.
    """
    max_continuous_gap = int(max(POLL_SECONDS, 30) * 1.5)
    member_ids = {member["id"] for member in members}

    with activity_db() as connection:
        if member_ids:
            placeholders = ",".join("?" for _ in member_ids)
            connection.execute(
                f"UPDATE activity_players SET active = 0 WHERE player_id NOT IN ({placeholders})",
                tuple(member_ids),
            )
        else:
            connection.execute("UPDATE activity_players SET active = 0")

        for member in members:
            connection.execute("""
                INSERT INTO activity_players
                    (player_id, name, profile_url, first_seen, last_seen, active)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(player_id) DO UPDATE SET
                    name = excluded.name,
                    profile_url = excluded.profile_url,
                    last_seen = excluded.last_seen,
                    active = 1
            """, (
                member["id"], member["name"], member["profile_url"],
                observed_at, observed_at,
            ))

            open_interval = connection.execute("""
                SELECT id, end_time, last_observed
                FROM activity_intervals
                WHERE player_id = ? AND is_open = 1
            """, (member["id"],)).fetchone()
            is_online = member["online_status"].casefold() == "online"

            if is_online and open_interval:
                gap = observed_at - open_interval["last_observed"]
                if gap <= max_continuous_gap:
                    connection.execute("""
                        UPDATE activity_intervals
                        SET end_time = ?, last_observed = ?
                        WHERE id = ?
                    """, (observed_at + 1, observed_at, open_interval["id"]))
                    continue
                connection.execute(
                    "UPDATE activity_intervals SET is_open = 0 WHERE id = ?",
                    (open_interval["id"],),
                )
                open_interval = None

            if is_online:
                connection.execute("""
                    INSERT INTO activity_intervals
                        (player_id, start_time, end_time, last_observed, is_open)
                    VALUES (?, ?, ?, ?, 1)
                """, (member["id"], observed_at, observed_at + 1, observed_at))
            elif open_interval:
                gap = observed_at - open_interval["last_observed"]
                end_time = observed_at if gap <= max_continuous_gap else open_interval["end_time"]
                connection.execute("""
                    UPDATE activity_intervals
                    SET end_time = ?, is_open = 0
                    WHERE id = ?
                """, (end_time, open_interval["id"]))


def local_day_bounds(day):
    """Return Unix timestamps for local midnight at the start/end of a date."""
    start = datetime.combine(day, datetime_time.min)
    end = datetime.combine(day + timedelta(days=1), datetime_time.min)
    return int(time.mktime(start.timetuple())), int(time.mktime(end.timetuple()))


def timestamp_minutes_for_day(timestamp, day, day_start, day_end):
    if timestamp <= day_start:
        return 0
    if timestamp >= day_end:
        return 1440
    local_time = datetime.fromtimestamp(timestamp)
    if local_time.date() < day:
        return 0
    if local_time.date() > day:
        return 1440
    return local_time.hour * 60 + local_time.minute + local_time.second / 60


def activity_for_day(day):
    day_start, day_end = local_day_bounds(day)
    generated_at = int(time.time())
    with activity_db() as connection:
        players = connection.execute("""
            SELECT player_id, name, profile_url, active
            FROM activity_players
            WHERE first_seen < ?
            ORDER BY name COLLATE NOCASE, player_id
        """, (day_end,)).fetchall()
        intervals = connection.execute("""
            SELECT player_id, start_time, end_time
            FROM activity_intervals
            WHERE start_time < ? AND end_time > ?
            ORDER BY start_time
        """, (day_end, day_start)).fetchall()
        earliest = connection.execute(
            "SELECT MIN(first_seen) AS timestamp FROM activity_players"
        ).fetchone()["timestamp"]

    intervals_by_player = {}
    for interval in intervals:
        start_minute = timestamp_minutes_for_day(
            interval["start_time"], day, day_start, day_end
        )
        end_minute = timestamp_minutes_for_day(
            interval["end_time"], day, day_start, day_end
        )
        if end_minute <= start_minute:
            continue
        intervals_by_player.setdefault(interval["player_id"], []).append({
            "start_minute": round(start_minute, 3),
            "end_minute": round(end_minute, 3),
        })

    return {
        "date": day.isoformat(),
        "today": date.today().isoformat(),
        "generated_at": generated_at,
        "current_minute": (
            timestamp_minutes_for_day(generated_at, day, day_start, day_end)
            if day == date.today() else None
        ),
        "earliest_date": (
            datetime.fromtimestamp(earliest).date().isoformat() if earliest else date.today().isoformat()
        ),
        "players": [{
            "id": row["player_id"],
            "name": row["name"],
            "profile_url": row["profile_url"],
            "active": bool(row["active"]),
            "intervals": intervals_by_player.get(row["player_id"], []),
        } for row in players],
    }


init_activity_db()


def api_get_faction_basic():
    # Torn v1 endpoint remains widely used for faction basic/member status data.
    url = f"https://api.torn.com/faction/{FACTION_ID}"
    params = {"selections": "basic", "key": API_KEY}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Torn API error {data['error'].get('code')}: {data['error'].get('error')}")
    return data


def classify_member(member_id, member):
    status = member.get("status") or {}
    last_action = member.get("last_action") or {}
    desc = str(status.get("description") or "")
    details = str(status.get("details") or "")
    state_name = str(status.get("state") or "")
    combined = " | ".join(x for x in [state_name, desc, details] if x)

    travel_state = "Other"
    destination = ""

    # Examples seen in Torn status fields: Traveling, Abroad, Returning, Okay, Hospital.
    # Check the mutually exclusive states in priority order. A traveling status
    # normally includes a country name, but that does not mean the member has
    # already arrived abroad.
    text = combined.lower()
    if "hospital" in text:
        travel_state = "Hospital"
    elif "return" in text or re.search(r"\bto torn\b", text):
        travel_state = "Returning"
    elif "travel" in text:
        travel_state = "Traveling"
    elif "abroad" in text or any(c.lower() in text for c in COUNTRIES):
        travel_state = "Abroad"

    for country in COUNTRIES:
        aliases = COUNTRY_ALIASES.get(country, [country.lower()])
        if any(alias in text for alias in aliases):
            destination = country
            break

    # Try phrases like "to Japan" or "in Switzerland" if Torn changes formatting.
    if not destination:
        match = re.search(r"\b(?:to|in|from)\s+([A-Z][A-Za-z ]+)", combined)
        if match:
            destination = match.group(1).strip()

    until = status.get("until") or 0
    try:
        until = int(until)
    except Exception:
        until = 0

    try:
        last_action_timestamp = int(last_action.get("timestamp") or 0)
    except Exception:
        last_action_timestamp = 0

    return {
        "id": str(member_id),
        "name": member.get("name", f"Player {member_id}"),
        "level": member.get("level", ""),
        "status_text": combined or "Unknown",
        "state": travel_state,
        "destination": destination,
        "until": until,
        "until_text": datetime.fromtimestamp(until, tz=timezone.utc).strftime("%H:%M:%S UTC") if until else "",
        "online_status": str(last_action.get("status") or "Unknown"),
        "last_action_timestamp": last_action_timestamp,
        "profile_url": f"https://www.torn.com/profiles.php?XID={member_id}",
    }


def poll_once():
    data = api_get_faction_basic()
    members_raw = data.get("members") or {}
    members = [classify_member(mid, m) for mid, m in members_raw.items()]
    members.sort(key=lambda m: (m["state"] not in ["Traveling", "Abroad", "Returning"], m["destination"], m["name"].lower()))

    now = int(time.time())
    record_activity_observations(members, now)
    new_changes = []
    with lock:
        old = state["last_seen"]
        observed = state["travel_observed"]
        active_flights = set()
        for m in members:
            if m["state"] in ["Traveling", "Returning"] and m["destination"] in TRAVEL_SECONDS:
                active_flights.add(m["id"])
                flight_key = f"{m['state']}|{m['destination']}"
                flight = observed.get(m["id"])
                if not flight or flight["key"] != flight_key:
                    flight = {
                        "key": flight_key,
                        "observed_at": now,
                        "estimated_until": now + TRAVEL_SECONDS[m["destination"]],
                    }
                    observed[m["id"]] = flight
                if not m["until"]:
                    m["estimated_until"] = flight["estimated_until"]
                    m["arrival_estimated"] = True

            key = f"{m['state']}|{m['destination']}|{m['status_text']}|{m['until']}"
            if m["id"] in old and old[m["id"]] != key:
                new_changes.append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "name": m["name"],
                    "id": m["id"],
                    "new": m["status_text"],
                    "state": m["state"],
                    "destination": m["destination"],
                })
            old[m["id"]] = key

        for member_id in list(observed):
            if member_id not in active_flights:
                del observed[member_id]

        state["members"] = members
        state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["error"] = None
        state["changes"] = (new_changes + state["changes"])[:50]


def poll_loop():
    while True:
        try:
            poll_once()
        except Exception as e:
            with lock:
                state["error"] = str(e)
        time.sleep(max(POLL_SECONDS, 30))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/hospital")
def hospital():
    return render_template("hospital.html")


@app.route("/activity")
def activity():
    return render_template("activity.html")


@app.route("/api/activity")
def api_activity():
    requested_date = request.args.get("date", date.today().isoformat())
    try:
        selected_date = date.fromisoformat(requested_date)
    except ValueError:
        return jsonify({"error": "Date must use YYYY-MM-DD format."}), 400
    if selected_date > date.today():
        return jsonify({"error": "Future activity dates are not available."}), 400
    return jsonify(activity_for_day(selected_date))


@app.route("/api/status")
def api_status():
    with lock:
        members = list(state["members"])
        grouped = {}
        unsafe_locations = set()
        unsafe_by_location = {}

        for m in members:
            # Group by any resolved foreign destination, regardless of status.
            # This makes foreign hospitals show under the destination section too,
            # e.g. "Hospital • UAE" appears under UAE.
            if m["destination"] in COUNTRIES:
                bucket = m["destination"]
                grouped.setdefault(bucket, []).append(m)
            elif m["state"] in ["Traveling", "Abroad", "Returning"]:
                bucket = m["destination"] or m["state"]
                grouped.setdefault(bucket, []).append(m)

            # Safe locations should be removed when enemies are currently abroad,
            # actively traveling there, or hospitalized there. Returning players are
            # coming home, so their previous destination is not counted as unsafe.
            if m["state"] in UNSAFE_DESTINATION_STATES and m["destination"] in COUNTRIES:
                unsafe_locations.add(m["destination"])
                unsafe_by_location.setdefault(m["destination"], []).append(m)

        safe_locations = [country for country in COUNTRIES if country not in unsafe_locations]

        return jsonify({
            "updated_at": state["updated_at"],
            "error": state["error"],
            "members": members,
            "grouped": grouped,
            "safe_locations": safe_locations,
            "unsafe_locations": sorted(unsafe_locations),
            "unsafe_by_location": unsafe_by_location,
            "changes": state["changes"],
        })


if __name__ == "__main__":
    Thread(target=poll_loop, daemon=True).start()
    app.run(host=HOST, port=PORT)
