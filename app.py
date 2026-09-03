import os
import re
import sqlite3
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from threading import Lock, Thread
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def detect_server_timezone():
    """Find an IANA name for API clients that do not supply a timezone."""
    candidates = [os.getenv("TZ", "").strip()]
    localtime_path = os.path.realpath("/etc/localtime")
    if "/zoneinfo/" in localtime_path:
        candidates.append(localtime_path.split("/zoneinfo/", 1)[1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate).key
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return "UTC"


DEFAULT_ACTIVITY_TIMEZONE = detect_server_timezone()

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


def create_activity_schema(connection):
    statements = (
        """CREATE TABLE IF NOT EXISTS activity_players (
            faction_id TEXT NOT NULL,
            player_id TEXT NOT NULL,
            name TEXT NOT NULL,
            profile_url TEXT NOT NULL,
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (faction_id, player_id)
        )""",
        """CREATE TABLE IF NOT EXISTS activity_intervals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            faction_id TEXT NOT NULL,
            player_id TEXT NOT NULL,
            start_time INTEGER NOT NULL,
            end_time INTEGER NOT NULL,
            last_observed INTEGER NOT NULL,
            is_open INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (faction_id, player_id)
                REFERENCES activity_players(faction_id, player_id)
        )""",
        """CREATE INDEX IF NOT EXISTS activity_intervals_day
            ON activity_intervals(faction_id, start_time, end_time)""",
        """CREATE INDEX IF NOT EXISTS activity_intervals_player
            ON activity_intervals(faction_id, player_id, start_time)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS activity_one_open_interval
            ON activity_intervals(faction_id, player_id) WHERE is_open = 1""",
    )
    for statement in statements:
        connection.execute(statement)


def migrate_activity_schema(connection):
    """Add faction ownership to databases created before faction scoping.

    The old schema cannot identify which earlier faction owned inactive rows.
    Its active roster is the best reliable indicator of the faction currently
    configured, so those rows retain their history under FACTION_ID. Inactive
    rows are preserved in a hidden legacy bucket rather than mixed into the
    current faction or deleted.
    """
    current_faction = FACTION_ID or "__legacy__"
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ALTER TABLE activity_players RENAME TO activity_players_legacy")
        connection.execute("ALTER TABLE activity_intervals RENAME TO activity_intervals_legacy")
        connection.execute("DROP INDEX IF EXISTS activity_intervals_day")
        connection.execute("DROP INDEX IF EXISTS activity_intervals_player")
        connection.execute("DROP INDEX IF EXISTS activity_one_open_interval")
        create_activity_schema(connection)
        connection.execute("""
            INSERT INTO activity_players
                (faction_id, player_id, name, profile_url, first_seen, last_seen, active)
            SELECT
                CASE WHEN active = 1 THEN ? ELSE '__legacy__' END,
                player_id, name, profile_url, first_seen, last_seen, active
            FROM activity_players_legacy
        """, (current_faction,))
        connection.execute("""
            INSERT INTO activity_intervals
                (id, faction_id, player_id, start_time, end_time, last_observed, is_open)
            SELECT
                intervals.id,
                CASE WHEN players.active = 1 THEN ? ELSE '__legacy__' END,
                intervals.player_id,
                intervals.start_time,
                intervals.end_time,
                intervals.last_observed,
                intervals.is_open
            FROM activity_intervals_legacy AS intervals
            JOIN activity_players_legacy AS players
                ON players.player_id = intervals.player_id
        """, (current_faction,))
        connection.execute("DROP TABLE activity_intervals_legacy")
        connection.execute("DROP TABLE activity_players_legacy")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def init_activity_db():
    """Create activity storage and end intervals left open by an old process.

    Each online run is stored as one interval instead of one row per poll. An
    interval's end is advanced only by successful observations, so application
    downtime cannot be mistaken for online activity.
    """
    with activity_db() as connection:
        player_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(activity_players)")
        }
        if player_columns and "faction_id" not in player_columns:
            migrate_activity_schema(connection)
        else:
            create_activity_schema(connection)
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
                f"""UPDATE activity_players SET active = 0
                    WHERE faction_id = ? AND player_id NOT IN ({placeholders})""",
                (FACTION_ID, *member_ids),
            )
        else:
            connection.execute(
                "UPDATE activity_players SET active = 0 WHERE faction_id = ?",
                (FACTION_ID,),
            )

        for member in members:
            connection.execute("""
                INSERT INTO activity_players
                    (faction_id, player_id, name, profile_url, first_seen, last_seen, active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(faction_id, player_id) DO UPDATE SET
                    name = excluded.name,
                    profile_url = excluded.profile_url,
                    last_seen = excluded.last_seen,
                    active = 1
            """, (
                FACTION_ID, member["id"], member["name"], member["profile_url"],
                observed_at, observed_at,
            ))

            open_interval = connection.execute("""
                SELECT id, end_time, last_observed
                FROM activity_intervals
                WHERE faction_id = ? AND player_id = ? AND is_open = 1
            """, (FACTION_ID, member["id"])).fetchone()
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
                        (faction_id, player_id, start_time, end_time, last_observed, is_open)
                    VALUES (?, ?, ?, ?, ?, 1)
                """, (
                    FACTION_ID, member["id"], observed_at,
                    observed_at + 1, observed_at,
                ))
            elif open_interval:
                gap = observed_at - open_interval["last_observed"]
                end_time = observed_at if gap <= max_continuous_gap else open_interval["end_time"]
                connection.execute("""
                    UPDATE activity_intervals
                    SET end_time = ?, is_open = 0
                    WHERE id = ?
                """, (end_time, open_interval["id"]))


def local_day_bounds(day, viewer_timezone):
    """Return Unix timestamps for midnight in the viewer's timezone."""
    start = datetime.combine(day, datetime_time.min, tzinfo=viewer_timezone)
    end = datetime.combine(day + timedelta(days=1), datetime_time.min, tzinfo=viewer_timezone)
    return int(start.timestamp()), int(end.timestamp())


def timestamp_minutes_for_day(timestamp, day, day_start, day_end, viewer_timezone):
    if timestamp <= day_start:
        return 0
    if timestamp >= day_end:
        return 1440
    local_time = datetime.fromtimestamp(timestamp, viewer_timezone)
    if local_time.date() < day:
        return 0
    if local_time.date() > day:
        return 1440
    return local_time.hour * 60 + local_time.minute + local_time.second / 60


def activity_for_day(day, timezone_name=DEFAULT_ACTIVITY_TIMEZONE):
    viewer_timezone = ZoneInfo(timezone_name)
    viewer_today = datetime.now(viewer_timezone).date()
    day_start, day_end = local_day_bounds(day, viewer_timezone)
    generated_at = int(time.time())
    with activity_db() as connection:
        players = connection.execute("""
            SELECT player_id, name, profile_url, active
            FROM activity_players
            WHERE faction_id = ? AND first_seen < ?
            ORDER BY name COLLATE NOCASE, player_id
        """, (FACTION_ID, day_end)).fetchall()
        intervals = connection.execute("""
            SELECT player_id, start_time, end_time
            FROM activity_intervals
            WHERE faction_id = ? AND start_time < ? AND end_time > ?
            ORDER BY start_time
        """, (FACTION_ID, day_end, day_start)).fetchall()
        earliest = connection.execute(
            """SELECT MIN(first_seen) AS timestamp FROM activity_players
               WHERE faction_id = ?""",
            (FACTION_ID,),
        ).fetchone()["timestamp"]

    intervals_by_player = {}
    for interval in intervals:
        start_minute = timestamp_minutes_for_day(
            interval["start_time"], day, day_start, day_end, viewer_timezone
        )
        end_minute = timestamp_minutes_for_day(
            interval["end_time"], day, day_start, day_end, viewer_timezone
        )
        if end_minute <= start_minute:
            continue
        intervals_by_player.setdefault(interval["player_id"], []).append({
            "start_minute": round(start_minute, 3),
            "end_minute": round(end_minute, 3),
        })

    return {
        "date": day.isoformat(),
        "today": viewer_today.isoformat(),
        "timezone": viewer_timezone.key,
        "faction_id": FACTION_ID,
        "generated_at": generated_at,
        "current_minute": (
            timestamp_minutes_for_day(
                generated_at, day, day_start, day_end, viewer_timezone
            ) if day == viewer_today else None
        ),
        "earliest_date": (
            datetime.fromtimestamp(earliest, viewer_timezone).date().isoformat()
            if earliest else viewer_today.isoformat()
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
    timezone_name = request.args.get("timezone", DEFAULT_ACTIVITY_TIMEZONE)
    try:
        viewer_timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return jsonify({"error": "Unknown timezone."}), 400

    viewer_today = datetime.now(viewer_timezone).date()
    requested_date = request.args.get("date", viewer_today.isoformat())
    try:
        selected_date = date.fromisoformat(requested_date)
    except ValueError:
        return jsonify({"error": "Date must use YYYY-MM-DD format."}), 400
    if selected_date > viewer_today:
        return jsonify({"error": "Future activity dates are not available."}), 400
    return jsonify(activity_for_day(selected_date, viewer_timezone.key))


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
