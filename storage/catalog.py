"""Teams, events and matches - the entities the analysis is *about*.

The other three stores are keyed by (video, start frame, end frame). That is
enough to process a match but cannot answer the question the tool exists for:
how does one team perform across a season. A frame range knows nothing about who
was driving.

Shapes follow RobotEvents' API as closely as hand entry allows - team `number`,
event `sku`, match `round`/`instance`/`number` - so populating this store from
the API later changes the source, not the schema. Fields the API provides but a
typed-in record cannot are present and null rather than absent, so nothing
downstream has to tell "not known yet" apart from "this record predates that
field".

One file per table, unlike the other stores' file-per-record. The teams table
joins across every match and the season view scans all of them; a match record
is a few hundred bytes, so a whole season is a couple of MB - cheap to read at
once, where several thousand small files would not be.
"""
import contextlib
import datetime as _dt
import json
import os
import re
import threading
import time

CATALOG_DIR = os.path.join("data", "catalog")

TEAMS_FILE = "teams.json"
EVENTS_FILE = "events.json"
MATCHES_FILE = "matches.json"

# Rounds in bracket order, which is also the order a season view should list
# them. The API integer for each is recorded alongside so imported matches map
# onto these slugs. The numbers are confirmed against the Public VEX Events API
# spec and against live data from a real event, not inferred.
ROUNDS = (
    ("practice", "Practice", 1),
    ("qual", "Qualification", 2),
    ("r16", "Round of 16", 6),
    ("qf", "Quarterfinal", 3),
    ("sf", "Semifinal", 4),
    ("final", "Final", 5),
    # VIQRC finals. Code 15 is not in the V5RC bracket sequence and arrives
    # after qualification as a run of "Match #1-1", "#1-2"... - 690 of them
    # across the imported IQ season, every one previously landing in
    # "Unspecified". It is kept as its own slug rather than folded into
    # "final" so the elimination bracket, which is a V5RC shape, ignores it.
    ("iq_final", "Finals", 15),
    ("unknown", "Unspecified", None),
)
ROUND_LABELS = {slug: label for slug, label, _ in ROUNDS}
ROUND_ORDER = {slug: index for index, (slug, _, _) in enumerate(ROUNDS)}
ROUND_FROM_API = {code: slug for slug, _, code in ROUNDS if code is not None}

ALLIANCES = ("red", "blue")

# Read-modify-write on a shared file, and Flask serves threaded, so two saves
# arriving together would otherwise lose one.
_lock = threading.RLock()

# Threads are not the only writer. An import running beside the server - or two
# imports started by two people - are separate processes, which a thread lock
# does nothing about. Measured the hard way: two season walks at once took
# matches.json from 2,848 records to 1,439, each overwriting a whole table the
# other had just extended. So every read-modify-write also takes an exclusive
# lock on a file in the catalog directory, which the operating system honours
# across processes.
try:
    import fcntl
except ImportError:                                # not POSIX; threads only
    fcntl = None

LOCK_FILE = ".lock"


@contextlib.contextmanager
def _guard(directory=CATALOG_DIR):
    """Hold this catalog directory against other threads and other processes."""
    with _lock:
        if fcntl is None:
            yield
            return
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, LOCK_FILE), "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _slug(text, limit=60):
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return cleaned[:limit].strip("-") or "unnamed"


def _resolve(table, key):
    """The stored key matching `key`, ignoring case.

    Keys come from three places - an uppercase SKU, a lowercase slug, and
    whatever a URL carried - and a lookup that missed by case alone would read
    as "no such record" rather than as the normalisation bug it is.
    """
    if not key:
        return None
    key = str(key).strip()
    if key in table:
        return key
    folded = key.upper()
    for stored in table:
        if stored.upper() == folded:
            return stored
    return None


def _path(filename, directory=CATALOG_DIR):
    return os.path.join(directory, filename)


# Parsed tables, keyed by path and invalidated on the file's own mtime and size.
# Measured need: one event is 273 matches and parses in ~1ms, but a whole season
# of them is ~6MB and ~33ms, and every page load reads the table at least once.
# Re-parsing per request would make the cost of the catalog grow with the season.
_parsed = {}


def _read(filename, directory=CATALOG_DIR):
    path = _path(filename, directory)
    try:
        stat = os.stat(path)
    except OSError:
        return {}

    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _parsed.get(path)
    if cached and cached[0] == stamp:
        return cached[1]

    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Stamped from the file rather than from this process, so a write by another
    # process - an import running beside the server - is picked up too.
    _parsed[path] = (stamp, data)
    return data


def _write(filename, table, directory=CATALOG_DIR):
    os.makedirs(directory, exist_ok=True)
    path = _path(filename, directory)
    # The temporary name carries the pid: two processes writing the same table
    # both used to write "matches.json.tmp", so whichever renamed second found
    # the file already gone and died on a FileNotFoundError.
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w") as handle:
        json.dump(table, handle, indent=2, sort_keys=True)
    os.replace(temporary, path)                    # atomic: no half-written table
    _parsed.pop(path, None)                        # next read re-stamps from disk
    return table


# --------------------------------------------------------------------------- teams

def team_key(number):
    """Team numbers are case-insensitive in practice; `929u` and `929U` are one team."""
    return str(number).strip().upper()


def list_teams(directory=CATALOG_DIR):
    return _read(TEAMS_FILE, directory)


def get_team(number, directory=CATALOG_DIR):
    return list_teams(directory).get(team_key(number))


def save_team(number, name=None, organization=None, location=None, grade=None,
              program="V5RC", robot_name=None, api_id=None, source="manual",
              directory=CATALOG_DIR):
    """Create or update one team. Absent arguments leave existing values alone.

    Updating in place matters because a team is first created implicitly, by
    being named in a match's alliance, with nothing but its number. Details
    arrive later - typed in, or from the API - and must not wipe the link to the
    matches already recorded against it.
    """
    key = team_key(number)
    if not key:
        raise ValueError("a team number is required")

    with _guard(directory):
        table = list_teams(directory)
        record = table.get(key) or {
            "number": key, "name": None, "organization": None,
            "location": {"city": None, "region": None, "country": None},
            "grade": None, "program": program, "robot_name": None, "api_id": None,
            "added_at": time.time(),
        }
        for field, value in (("name", name), ("organization", organization),
                             ("grade", grade), ("robot_name", robot_name),
                             ("api_id", api_id), ("program", program)):
            if value not in (None, ""):
                record[field] = value
        # Provenance tracks where the data came from, so an API import overwrites
        # the "implied" mark a team got from merely being named in an alliance.
        record["source"] = source
        if location:
            record["location"] = {**record.get("location", {}), **location}
        record["number"] = key
        record["updated_at"] = time.time()
        table[key] = record
        _write(TEAMS_FILE, table, directory)
    return record


def ensure_team(number, directory=CATALOG_DIR):
    """A team named in an alliance exists, whether or not anyone typed it in."""
    key = team_key(number)
    if not key:
        return None
    return get_team(key, directory) or save_team(key, source="implied", directory=directory)


def delete_team(number, directory=CATALOG_DIR):
    with _guard(directory):
        table = list_teams(directory)
        removed = table.pop(team_key(number), None)
        if removed:
            _write(TEAMS_FILE, table, directory)
    return removed


# -------------------------------------------------------------------------- events

def event_key(sku=None, name=None):
    """RobotEvents' SKU when there is one, otherwise a slug of the name.

    Events entered by hand before the API is wired have no SKU, and inventing a
    fake one would collide with a real event later. A `local-` prefix keeps the
    two namespaces apart and makes it obvious which records still need linking.
    """
    if sku:
        return str(sku).strip().upper()
    return ("LOCAL-" + _slug(name or "event")).upper()


def list_events(directory=CATALOG_DIR):
    return _read(EVENTS_FILE, directory)


def get_event(key, directory=CATALOG_DIR):
    table = list_events(directory)
    found = _resolve(table, key)
    return table.get(found) if found else None


def save_event(name, sku=None, season=None, start=None, end=None, location=None,
               level=None, api_id=None, season_id=None, program=None, divisions=None,
               ongoing=None, grades=None, teams=None, source="manual", key=None,
               directory=CATALOG_DIR):
    key = key or event_key(sku, name)
    with _guard(directory):
        table = list_events(directory)
        record = table.get(key) or {"key": key, "added_at": time.time()}
        record.update({
            "key": key,
            "sku": sku or record.get("sku"),
            "name": name or record.get("name"),
            "season": season or record.get("season"),
            "start": start or record.get("start"),
            "end": end or record.get("end"),
            "level": level or record.get("level"),
            "api_id": api_id or record.get("api_id"),
            "season_id": season_id or record.get("season_id"),
            "program": program or record.get("program"),
            "divisions": divisions if divisions is not None else record.get("divisions"),
            "ongoing": ongoing if ongoing is not None else record.get("ongoing"),
            "grades": grades if grades is not None else record.get("grades"),
            # The registered roster, kept as team numbers. Needed to judge a
            # field's strength before it has played; a count alone cannot.
            "teams": teams if teams is not None else record.get("teams"),
            "source": source,
            "updated_at": time.time(),
        })
        record["location"] = {**record.get("location", {}), **(location or {})}
        table[key] = record
        _write(EVENTS_FILE, table, directory)
    return record


# An event is called for a grade only when it is nearly all one grade. The
# threshold is not 100% because a handful of cross-registered teams should not
# turn a High School event into a blended one.
GRADE_MAJORITY = 0.9


def event_grade(event):
    """"High School", "Middle School", "Blended", or None if nothing is known.

    Derived from the grades of the teams registered at the event, never from its
    name. Name parsing was measured against the events whose rosters we can
    check: it agreed 23 times, disagreed 6, and said nothing 13 - so a name is
    wrong or silent about 45% of the time, and an event silently filed under the
    wrong grade is exactly the kind of error a scouting filter would hide.
    """
    counts = (event or {}).get("grades") or {}
    total = sum(counts.values())
    if not total:
        return None
    grade, top = max(counts.items(), key=lambda kv: kv[1])
    return grade if top / total >= GRADE_MAJORITY else "Blended"


def event_status(event, today=None):
    """"past", "ongoing" or "upcoming", from the event's own dates.

    Derived rather than stored: an event's dates never change but "today" does,
    so a saved flag would quietly go stale and start describing the day it was
    imported instead of now.
    """
    if not event:
        return "unknown"
    today = today or _dt.date.today().isoformat()
    start, end = (event.get("start") or "")[:10], (event.get("end") or "")[:10]
    if not start:
        return "unknown"
    if start > today:
        return "upcoming"
    if (end or start) < today:
        return "past"
    return "ongoing"


def delete_event(key, with_matches=False, directory=CATALOG_DIR):
    """Remove an event, optionally taking its matches with it.

    `with_matches` refuses to run if any of those matches still has footage
    linked. Deleting the only record of which video covers which match would
    orphan the calibration and seed files - they key on the video and frame
    range, so they survive on disk with nothing pointing at them.
    """
    with _guard(directory):
        key = _resolve(list_events(directory), key) or str(key).strip()
        folded = key.upper()
        theirs = [m for m in list_matches(directory).values()
                  if str(m.get("event") or "").upper() == folded]
        if with_matches:
            linked = [m["key"] for m in theirs if segment_of(m)]
            if linked:
                raise ValueError(
                    "these matches still have footage linked, so deleting the event would "
                    f"orphan it: {', '.join(linked)}. Move the footage first."
                )
            matches = list_matches(directory)
            for match in theirs:
                matches.pop(match["key"], None)
            _write(MATCHES_FILE, matches, directory)

        table = list_events(directory)
        removed = table.pop(key, None)
        if removed:
            _write(EVENTS_FILE, table, directory)
    return removed


def transfer_video(from_key, to_key, force=False, directory=CATALOG_DIR):
    """Move a match's linked footage onto another match.

    The recurring case this exists for: segments registered from files on disk
    become placeholder matches with video but no teams, and the same matches
    later arrive properly from the API with teams but no video. This joins the
    two halves.

    Nothing on disk moves. The calibration, seed and result stores key on
    (video, start frame, end frame), so once the target match carries that
    triple every artefact already computed resolves against it unchanged - the
    fifteen minutes a run costs is not spent again.
    """
    with _guard(directory):
        table = list_matches(directory)
        source = table.get(_resolve(table, from_key) or "")
        target = table.get(_resolve(table, to_key) or "")
        if source is None:
            raise ValueError(f"no match {from_key}")
        if target is None:
            raise ValueError(f"no match {to_key}")
        video = source.get("video")
        if not segment_of(source):
            raise ValueError(f"{from_key} has no footage linked")
        if segment_of(target) and not force:
            raise ValueError(
                f"{to_key} already has footage linked; pass force=True to replace it")

        target["video"] = video
        target["updated_at"] = time.time()
        source["video"] = None
        source["updated_at"] = time.time()
        _write(MATCHES_FILE, table, directory)
    return target


# ------------------------------------------------------------------------- matches

def match_name(round_slug, instance, number):
    """The label an event screen would show: "Q 12", "SF 2-1"."""
    short = {"qual": "Q", "practice": "P", "r16": "R16", "qf": "QF", "sf": "SF",
             "final": "F", "unknown": "M"}.get(round_slug, "M")
    if round_slug in ("qual", "practice", "unknown"):
        return f"{short} {number}"
    return f"{short} {instance}-{number}"


def match_key(event, round_slug, instance, number, division=None):
    """Unique per match, division included.

    The division is part of the identity, not decoration: an event running three
    divisions has a "Qual 1" in each, so a key without it silently collides and
    the last division written wins. That cost 455 of 2,848 imported matches
    before it was caught - one division of a three-division event came through
    with 80 matches and another with 1.
    """
    where = _slug(division, 24) if division else "d"
    return f"{event}__{where}__{round_slug}-{int(instance)}-{int(number)}".upper()


def list_matches(directory=CATALOG_DIR):
    return _read(MATCHES_FILE, directory)


def get_match(key, directory=CATALOG_DIR):
    table = list_matches(directory)
    found = _resolve(table, key)
    return table.get(found) if found else None


def save_match(event, round_slug="unknown", instance=1, number=1, division=None,
               red=(), blue=(), red_score=None, blue_score=None, video=None,
               scheduled=None, api_id=None, name=None, field=None,
               source="manual", key=None, directory=CATALOG_DIR):
    """Create or update one match, registering any team it names.

    `video` is {"file", "start_frame", "end_frame"} - the same triple the
    calibration, seed and result stores key on. Holding it here rather than
    re-keying those stores means every run already on disk keeps resolving; a
    match points at its artefacts instead of owning them.
    """
    round_slug = round_slug if round_slug in ROUND_LABELS else "unknown"
    key = key or match_key(event, round_slug, instance, number, division)

    with _guard(directory):
        table = list_matches(directory)
        record = table.get(key) or {"key": key, "added_at": time.time()}
        red_teams = [team_key(t) for t in red if str(t).strip()]
        blue_teams = [team_key(t) for t in blue if str(t).strip()]
        record.update({
            "key": key,
            "event": event,
            "division": division or record.get("division"),
            "round": round_slug,
            "instance": int(instance),
            "number": int(number),
            "name": name or match_name(round_slug, instance, number),
            "scheduled": scheduled or record.get("scheduled"),
            "field": field or record.get("field"),
            "api_id": api_id or record.get("api_id"),
            "alliances": {
                "red": {"teams": red_teams, "score": red_score},
                "blue": {"teams": blue_teams, "score": blue_score},
            },
            # `video` is left alone when the caller passes None, which is what
            # makes an API import safe: it refreshes alliances and scores without
            # touching footage somebody linked by hand.
            "video": video if video is not None else record.get("video"),
            "source": source,
            "updated_at": time.time(),
        })
        table[key] = record
        _write(MATCHES_FILE, table, directory)

    for number_ in red_teams + blue_teams:
        ensure_team(number_, directory)
    return record


def delete_match(key, directory=CATALOG_DIR):
    with _guard(directory):
        table = list_matches(directory)
        removed = table.pop(_resolve(table, key) or "", None)
        if removed:
            _write(MATCHES_FILE, table, directory)
    return removed


def segment_of(match):
    """(video file, start frame, end frame), or None if no footage is linked.

    This is the join to every artefact already on disk: the three older stores
    take exactly this triple.
    """
    video = (match or {}).get("video") or {}
    name = video.get("file")
    if not name or video.get("start_frame") is None or video.get("end_frame") is None:
        return None
    return name, int(video["start_frame"]), int(video["end_frame"])


# -------------------------------------------------------------------------- queries

def sort_key(match, events=None):
    """Chronological where a date exists, bracket order otherwise."""
    event = (events or {}).get(match.get("event")) or {}
    return (str(event.get("start") or ""), str(match.get("event") or ""),
            ROUND_ORDER.get(match.get("round"), 99),
            match.get("instance", 0), match.get("number", 0))


def teams_in(match):
    red = match.get("alliances", {}).get("red", {}).get("teams", [])
    blue = match.get("alliances", {}).get("blue", {}).get("teams", [])
    return list(red) + list(blue)


def alliance_of(match, number):
    key = team_key(number)
    for colour in ALLIANCES:
        if key in match.get("alliances", {}).get(colour, {}).get("teams", []):
            return colour
    return None


def matches_for_team(number, directory=CATALOG_DIR):
    """Every match a team appears in, in season order."""
    key = team_key(number)
    events = list_events(directory)
    found = [m for m in list_matches(directory).values() if key in teams_in(m)]
    return sorted(found, key=lambda m: sort_key(m, events))


# How a match result is read. V5RC matches have two alliances and a winner.
# VIQRC Teamwork matches have two teams playing together for a single score,
# which the API reports on both "alliances" - measured on RE-VIQRC-25-3671,
# where 150 of 154 matches carried identical red and blue scores. Read as
# alliance play, every one of those is a tie.
ALLIANCE_SCORING = "alliance"
COOPERATIVE_SCORING = "cooperative"


def team_score(match, number):
    """What this team scored in this match, or None if no score is recorded."""
    side = alliance_of(match, number)
    if side is None:
        return None
    return match.get("alliances", {}).get(side, {}).get("score")


def outcome(match, number, scoring=ALLIANCE_SCORING):
    """"win" / "loss" / "tie", or None when the match has no scores recorded.

    Scores are typed in by hand, and most matches will not have them. None here
    means unknown, and every aggregate below leaves those matches out of the
    record rather than counting them as losses.

    A cooperative match has no winner at all, so it returns None as well -
    calling them ties would give every IQ team a record of 0-0-N and a win
    rate of zero, which reads as "played and never won".
    """
    if scoring == COOPERATIVE_SCORING:
        return None
    side = alliance_of(match, number)
    if side is None:
        return None
    scores = {c: match.get("alliances", {}).get(c, {}).get("score") for c in ALLIANCES}
    if scores["red"] is None or scores["blue"] is None:
        return None
    mine, theirs = scores[side], scores["blue" if side == "red" else "red"]
    return "win" if mine > theirs else ("loss" if mine < theirs else "tie")


def team_record(matches, number, scoring=ALLIANCE_SCORING):
    """Win/loss/tie plus scoring averages, over the matches that have scores.

    Under cooperative scoring there is no record to keep, so the win/loss keys
    are present and null - the same "not known" the rest of the catalog uses -
    and what the program actually measures takes their place: how much a team
    scores, its best, and its total.
    """
    if scoring == COOPERATIVE_SCORING:
        scores = [s for s in (team_score(m, number) for m in matches) if s is not None]
        return {
            "matches": len(matches),
            "scored_matches": len(scores),
            "wins": None, "losses": None, "ties": None, "win_rate": None,
            "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
            "high_score": max(scores) if scores else None,
            "total_score": sum(scores) if scores else None,
            "avg_conceded": None,
            "events": sorted({m.get("event") for m in matches if m.get("event")}),
        }

    tally = {"win": 0, "loss": 0, "tie": 0}
    scored, conceded, counted = 0, 0, 0
    best = None
    for match in matches:
        result = outcome(match, number, scoring)
        if result is None:
            continue
        tally[result] += 1
        side = alliance_of(match, number)
        mine = match["alliances"][side]["score"]
        scored += mine
        best = mine if best is None else max(best, mine)
        conceded += match["alliances"]["blue" if side == "red" else "red"]["score"]
        counted += 1

    played = tally["win"] + tally["loss"] + tally["tie"]
    return {
        "matches": len(matches),
        "scored_matches": counted,
        "wins": tally["win"], "losses": tally["loss"], "ties": tally["tie"],
        "win_rate": round(tally["win"] / played, 3) if played else None,
        "avg_score": round(scored / counted, 1) if counted else None,
        "high_score": best,
        "total_score": scored if counted else None,
        "avg_conceded": round(conceded / counted, 1) if counted else None,
        "events": sorted({m.get("event") for m in matches if m.get("event")}),
    }


def partners(matches, number, scoring=ALLIANCE_SCORING):
    """Who a team has played alongside, most frequent first.

    Qualification and elimination partners are counted separately because they
    mean different things. A qualification partner is a random draw - playing
    with someone eight times says nothing about either team. An elimination
    partner was *chosen* in alliance selection, so even one says something: it
    is either who picked them or who they picked.

    The record is the pair's record in the matches they played together, which
    is the only record that says anything about the pairing rather than about
    either team on its own.

    Under cooperative scoring the partner is on the *other* side: a Teamwork
    match is reported as two one-team alliances, so a team's own side holds
    only itself and the team it played with sits opposite. Reading it the
    alliance way would say every IQ team has never had a partner.
    """
    key = team_key(number)
    together = scoring == COOPERATIVE_SCORING
    found = {}
    for match in matches:
        side = alliance_of(match, key)
        if side is None:
            continue
        elimination = match.get("round") not in ("qual", "practice")
        result = outcome(match, key, scoring)
        beside = "blue" if (together and side == "red") else ("red" if together else side)
        for other in match.get("alliances", {}).get(beside, {}).get("teams", []):
            if other == key:
                continue
            entry = found.setdefault(other, {
                "team": other, "matches": 0, "eliminations": 0,
                "wins": 0, "losses": 0, "ties": 0,
            })
            entry["matches"] += 1
            entry["eliminations"] += 1 if elimination else 0
            if result:
                entry[{"win": "wins", "loss": "losses", "tie": "ties"}[result]] += 1

    rows = []
    for entry in found.values():
        played = entry["wins"] + entry["losses"] + entry["ties"]
        entry["scored_matches"] = played
        entry["win_rate"] = round(entry["wins"] / played, 3) if played else None
        rows.append(entry)
    # Chosen partners first, then simply frequent ones.
    rows.sort(key=lambda r: (-r["eliminations"], -r["matches"], r["team"]))
    return rows


def team_index(directory=CATALOG_DIR, scoring=ALLIANCE_SCORING):
    """One row per team for the main table, with its season record attached.

    Built from a single pass over the matches rather than one query per team:
    the table lists every team, so per-team lookups would rescan the whole match
    table once for each row.
    """
    events = list_events(directory)
    matches = list(list_matches(directory).values())
    by_team = {}
    for match in matches:
        for number in teams_in(match):
            by_team.setdefault(number, []).append(match)

    rows = []
    for number, team in sorted(list_teams(directory).items()):
        theirs = sorted(by_team.get(number, []), key=lambda m: sort_key(m, events))
        rows.append({**team, **team_record(theirs, number, scoring),
                     "with_video": sum(1 for m in theirs if segment_of(m))})
    return rows
