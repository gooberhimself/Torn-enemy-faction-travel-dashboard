import os
import re
import time
from datetime import datetime, timezone
from threading import Lock, Thread

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template

load_dotenv()

API_KEY = os.getenv("TORN_API_KEY", "").strip()
FACTION_ID = os.getenv("ENEMY_FACTION_ID", "").strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8787"))

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

    return {
        "id": str(member_id),
        "name": member.get("name", f"Player {member_id}"),
        "level": member.get("level", ""),
        "status_text": combined or "Unknown",
        "state": travel_state,
        "destination": destination,
        "until": until,
        "until_text": datetime.fromtimestamp(until, tz=timezone.utc).strftime("%H:%M:%S UTC") if until else "",
        "profile_url": f"https://www.torn.com/profiles.php?XID={member_id}",
    }


def poll_once():
    data = api_get_faction_basic()
    members_raw = data.get("members") or {}
    members = [classify_member(mid, m) for mid, m in members_raw.items()]
    members.sort(key=lambda m: (m["state"] not in ["Traveling", "Abroad", "Returning"], m["destination"], m["name"].lower()))

    now = int(time.time())
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
