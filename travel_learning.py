"""Deterministic travel inference using bounded, persisted poll observations."""
import json
from collections import Counter

METHODS = ("Standard", "Airstrip", "WLT", "Business Class")
# Published minutes, without the travel book. Checked 2026-09-25:
# https://wiki.torn.com/wiki/Travel (June 2026 travel-time update).
ROUTE_MINUTES = {
    "Mexico": (24, 17, 12, 7),
    "Cayman Islands": (33, 23, 17, 10),
    "Canada": (39, 27, 19, 12),
    "Hawaii": (127, 89, 63, 38),
    "United Kingdom": (151, 106, 75, 45),
    "Argentina": (158, 111, 79, 47),
    "Switzerland": (166, 116, 83, 50),
    "Japan": (213, 149, 107, 64),
    "China": (229, 160, 114, 69),
    "UAE": (257, 180, 128, 77),
    "South Africa": (282, 197, 141, 85),
}
TRAVEL_TIMES = {country: dict(zip(METHODS, (v * 60 for v in values)))
                for country, values in ROUTE_MINUTES.items()}


def create_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS travel_tracking (
        faction_id TEXT NOT NULL, player_id TEXT NOT NULL,
        observed_at INTEGER NOT NULL, snapshot TEXT NOT NULL,
        PRIMARY KEY (faction_id, player_id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS travel_observations (
        id INTEGER PRIMARY KEY, faction_id TEXT NOT NULL, player_id TEXT NOT NULL,
        destination TEXT NOT NULL, direction TEXT NOT NULL,
        first_traveling INTEGER NOT NULL, first_landed INTEGER,
        duration INTEGER, closest_method TEXT, timing_error REAL,
        expected_seconds INTEGER, tolerance_seconds REAL,
        usable INTEGER NOT NULL, rejection_reason TEXT NOT NULL,
        api_info TEXT NOT NULL,
        UNIQUE (faction_id, player_id, first_traveling, direction)
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS travel_player_history
        ON travel_observations(player_id, first_traveling DESC)""")


def match_duration(destination, duration, departure_gap, arrival_gap):
    """Require exactly one plausible method, not merely the closest one.

    Actual duration lies between observed duration minus arrival gap and
    observed duration plus departure gap. Allow 3% game variance and 30s
    rounding of the published minute table as well.
    """
    times = TRAVEL_TIMES[destination]
    closest = min(times, key=lambda method: abs(duration - times[method]))
    candidates = []
    for method, expected in times.items():
        variance = expected * .03 + 30
        if duration - arrival_gap <= expected + variance and duration + departure_gap >= expected - variance:
            candidates.append(method)
    return {
        "closest_method": closest,
        "timing_error": abs(duration - times[closest]),
        "expected_seconds": times[closest],
        "tolerance_seconds": max(departure_gap, arrival_gap) + times[closest] * .03 + 30,
        "reason": "" if candidates == [closest] else ("ambiguous timing" if candidates else "unmatched timing"),
    }


def infer_profile(rows, now):
    """Rows are newest first. Lifetime counts remain separate from recent votes."""
    valid = [r for r in rows if r["usable"]]
    counts = dict.fromkeys(METHODS, 0)
    counts.update(Counter(r["closest_method"] for r in valid))
    recent = [r["closest_method"] for r in valid
              if now - r["first_landed"] <= 90 * 86400][:20]
    persistent = [m for m in recent if m != "Business Class"]
    method, confidence, changed = None, "Unknown", False
    window = persistent[:5]
    if window:
        leader, count = Counter(window).most_common(1)[0]
        if count >= 3 and count / len(window) >= .75:
            method = leader
        # Three consecutive new-method trips can override older history.
        if len(window) >= 3 and len(set(window[:3])) == 1:
            method = window[0]
        older = persistent[3:11]
        if len(older) >= 3:
            previous, n = Counter(older).most_common(1)[0]
            changed = n / len(older) >= .75 and len(window) >= 2 and window[0] == window[1] != previous
            if changed and window[:3] != [window[0]] * 3:
                method = None
    # Business needs sustained use; one ticket never replaces a normal method.
    if len(recent) >= 6 and recent[:6].count("Business Class") >= 5:
        method = "Business Class"
        changed = bool(persistent[:3]) and len(persistent[:3]) == 3
    if method:
        confidence = "Likely"
        evidence = recent if method == "Business Class" else persistent
        if len(evidence[:8]) >= 6 and evidence[:8].count(method) / len(evidence[:8]) >= .875:
            confidence = "High"
        if changed:
            confidence = "Likely"
    return {"method": method, "confidence": confidence,
            "behavior_changed": changed, "observed_trips": len(valid),
            "total_observations": len(rows), "matches": counts,
            "has_used_business": counts["Business Class"] > 0}


def api_info(member):
    return member.get("travel_api_info", {})


def is_home(member):
    return not member["destination"] and member.get("api_state") in {"Okay", "Hospital", "Jail"}


def has_arrived(member, flight):
    if flight["direction"] == "Returning":
        return is_home(member)
    return member["state"] in {"Abroad", "Hospital"} and member["destination"] == flight["destination"]


def store_observation(db, faction_id, player_id, flight, member, now, arrival_gap, landed):
    duration = now - flight["started"] if landed else None
    match = match_duration(flight["destination"], duration, flight["departure_gap"], arrival_gap) if landed else {}
    reason = flight["reason"] or (match.get("reason", "") if landed else "arrival not observed")
    db.execute("""INSERT OR IGNORE INTO travel_observations
        (faction_id, player_id, destination, direction, first_traveling, first_landed,
         duration, closest_method, timing_error, expected_seconds, tolerance_seconds,
         usable, rejection_reason, api_info)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
        faction_id, player_id, flight["destination"], flight["direction"], flight["started"],
        now if landed else None, duration, match.get("closest_method"), match.get("timing_error"),
        match.get("expected_seconds"), match.get("tolerance_seconds"), int(not reason), reason,
        json.dumps({"departure": flight["api"], "last_traveling": flight.get("last_api"),
                    "arrival": api_info(member), "departure_gap": flight["departure_gap"],
                    "arrival_gap": arrival_gap}),
    ))


def observe_members(db, faction_id, members, now, poll_seconds, defaults):
    """Atomic poll checkpoint + observations. Survives restarts; no API calls."""
    max_gap = max(poll_seconds, 30) * 1.5
    for member in members:
        player_id = member["id"]
        row = db.execute("SELECT * FROM travel_tracking WHERE faction_id = ? AND player_id = ?",
                         (faction_id, player_id)).fetchone()
        previous = json.loads(row["snapshot"]) if row else None
        # Replaying a poll at the same/earlier timestamp must not advance history.
        if row and now <= row["observed_at"]:
            flight = previous.get("flight")
        else:
            gap = now - row["observed_at"] if row else 0
            flight = previous.get("flight") if previous else None
            flying = member["state"] in {"Traveling", "Returning"} and member["destination"] in TRAVEL_TIMES
            same_flight = flying and flight and (member["state"], member["destination"]) == (flight["direction"], flight["destination"])
            if flight:
                if gap > max_gap:
                    flight["reason"] = flight["reason"] or "polling gap"
                if not same_flight:
                    store_observation(db, faction_id, player_id, flight, member, now, gap, has_arrived(member, flight))
                    flight = None
            if flying and not flight:
                reliable = previous and gap <= max_gap and (
                    (member["state"] == "Traveling" and previous["home"]) or
                    (member["state"] == "Returning" and previous["state"] in {"Abroad", "Hospital"}
                     and previous["destination"] == member["destination"]))
                flight = {"direction": member["state"], "destination": member["destination"],
                          "started": now, "departure_gap": gap,
                          "reason": "" if reliable else "departure not observed reliably",
                          "api": api_info(member)}
            if flight:
                flight["last_api"] = api_info(member)
            snapshot = {"state": member["state"], "destination": member["destination"],
                        "home": is_home(member), "flight": flight}
            db.execute("""INSERT INTO travel_tracking VALUES (?, ?, ?, ?)
                ON CONFLICT(faction_id, player_id) DO UPDATE SET
                    observed_at = excluded.observed_at, snapshot = excluded.snapshot""",
                       (faction_id, player_id, now, json.dumps(snapshot)))
        rows = db.execute("""SELECT * FROM travel_observations WHERE player_id = ?
            ORDER BY first_traveling DESC, id DESC""", (player_id,)).fetchall()
        profile = infer_profile(rows, now)
        member["travel_profile"] = profile
        if flight and member["state"] in {"Traveling", "Returning"}:
            # Pin the estimate at departure, including across process restarts.
            if "estimated_until" not in flight:
                method = profile["method"]
                seconds = TRAVEL_TIMES[flight["destination"]][method] if method else defaults[flight["destination"]]
                flight["estimated_until"] = flight["started"] + seconds
                flight["prediction_method"] = method
                previous_snapshot = db.execute("SELECT snapshot FROM travel_tracking WHERE faction_id = ? AND player_id = ?",
                                               (faction_id, player_id)).fetchone()
                snapshot = json.loads(previous_snapshot["snapshot"])
                snapshot["flight"] = flight
                db.execute("UPDATE travel_tracking SET snapshot = ? WHERE faction_id = ? AND player_id = ?",
                           (json.dumps(snapshot), faction_id, player_id))
            member["prediction_method"] = flight["prediction_method"]
            if not member["until"]:
                member["estimated_until"] = flight["estimated_until"]
                member["arrival_estimated"] = True
