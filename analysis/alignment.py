"""Where each match sits inside its event's stream, with nobody typing a timestamp.

`storage/event_streams.py` says which videos an event was streamed on.
`match["scheduled"]` says when a match was meant to start. A stream's
`started_at` says when it began, in UTC. Put together, those give every
scheduled match an offset into a specific video - which is exactly what
`match["video"]["watch_start"]` needs, and what the team page's Match footage
panel is waiting for.

    python -m analysis.alignment --dry-run          # what the data supports; writes nothing
    python -m analysis.alignment --event RE-V5RC-26-4244
    python -m analysis.alignment                    # every event with a resolved stream

**These offsets are approximate and are stored saying so.** They come from the
schedule, and events run behind it: a match scheduled for 10:36 may have played
at 11:05. What this buys is a link that lands you near the match instead of at
the start of an eight-hour video. `video["approximate"]` is set on every one, so
a page can say so rather than implying frame accuracy.

## Two things it does not need to be told

**Which stream a match is on.** Each stream is a real interval - `started_at`
plus `duration` - so a match's absolute time falls inside one of them or none.
A two-day, three-division event resolves without anything being said about days
or divisions. Two fields streamed at once is the case that stays ambiguous, and
it is reported rather than guessed.

**The event's timezone.** `scheduled` may or may not carry a UTC offset; it is
kept as written (`storage/catalog.py`) and the API's shape is not promised, so
both are handled. Where it carries one, nothing is inferred. Where it does not,
the event's location gives an IANA zone and `zoneinfo` resolves the offset for
the event's own date - which is why zones are stored by name and not as numbers,
since the season straddles the daylight-saving change.

A search over candidate offsets, scored by how many matches land inside a
stream, then has to agree. It is the check and the fallback, not the mechanism,
and that ordering was measured rather than assumed: on a two-day event whose
matches spanned three hours of an eight-hour stream, every offset across a
five-hour range scored identically, and the search picked the wrong edge of that
plateau by nearly two hours. A stream long enough to contain the matches is
simply not evidence of where in it they sit.

So the search's real job is refusal. An offset that lands most of an event's
matches outside every stream is wrong, whether it came from a table or a search,
and then nothing is written at all.

## What it will not touch

Footage linked by hand. A `video` written by this module carries
`"source": "auto"`, and anything without that marker is left exactly as it is -
so a stream link and start time someone typed on a match page always wins, and
existing footage predating this module is treated as hand-linked, which it was.

A local segment's `file`, `start_frame` and `end_frame` are merged through
untouched. Replacing that dict wholesale would orphan the calibration and seed
files that key on the triple, which is the one loss in this project that is not
recoverable by re-running something.
"""
import argparse
import datetime as dt
import zoneinfo

from storage import catalog, event_streams

# A stream starts before the first match and runs past the last. A match landing
# this far outside a stream's span is still taken as belonging to it; further out
# and it belongs to nothing.
PRE_ROLL = 45 * 60
TAIL = 45 * 60

# Candidate UTC offsets, in minutes, for the fallback search. Quarter-hour steps
# because VEX runs events in India (+5:30) and Nepal (+5:45).
CANDIDATES = tuple(range(-12 * 60, 14 * 60 + 1, 15))

# An offset has to land at least this share of an event's scheduled matches
# inside a stream, or the alignment is not trusted at all.
MIN_COVERAGE = 0.6

# Location to IANA zone, so `zoneinfo` resolves the actual offset for the event's
# own date. Daylight saving is not modelled here on purpose: the VEX season runs
# from August to May and straddles the change, so a fixed offset would be an hour
# wrong for half of it.
#
# Listed explicitly rather than derived, for the reason `storage/regions.py` gives:
# the API sends a full state name and nothing guarantees it is spelled the same
# way twice. Anything absent falls through to the search, which is the honest
# outcome - a missing row means "not known", not "assume Eastern".
#
# Split states take their majority zone. Indiana, Kentucky, Tennessee, Florida,
# North Dakota and South Dakota each straddle a boundary; an event on the wrong
# side is an hour out, which the search then catches as a coverage failure.
_US_ZONES = {
    "America/Los_Angeles": ("Washington", "Oregon", "California", "Nevada"),
    "America/Denver": ("Idaho", "Montana", "Utah", "Colorado", "Wyoming",
                       "New Mexico"),
    "America/Phoenix": ("Arizona",),          # no daylight saving
    "America/Chicago": ("Texas", "Oklahoma", "Minnesota", "Wisconsin", "Iowa",
                        "Missouri", "Illinois", "Kansas", "Nebraska", "Arkansas",
                        "Louisiana", "Mississippi", "Alabama", "North Dakota",
                        "South Dakota", "Tennessee"),
    "America/New_York": ("Michigan", "Ohio", "Indiana", "Kentucky", "Georgia",
                         "Florida", "Virginia", "West Virginia", "North Carolina",
                         "South Carolina", "New York", "New Jersey", "Pennsylvania",
                         "Delaware", "Maryland", "District of Columbia", "Maine",
                         "New Hampshire", "Vermont", "Massachusetts",
                         "Rhode Island", "Connecticut"),
    "America/Anchorage": ("Alaska",),
    "Pacific/Honolulu": ("Hawaii",),
}

STATE_ZONES = {state: zone for zone, states in _US_ZONES.items() for state in states}

# Countries VEX runs events in that are a single zone. A country spanning several
# - Canada, Australia, Brazil - is left out so it falls to the search rather than
# being placed in whichever zone happened to get written down first.
COUNTRY_ZONES = {
    "China": "Asia/Shanghai", "Taiwan": "Asia/Taipei", "Japan": "Asia/Tokyo",
    "South Korea": "Asia/Seoul", "Korea, Republic of": "Asia/Seoul",
    "Singapore": "Asia/Singapore", "Malaysia": "Asia/Kuala_Lumpur",
    "Hong Kong": "Asia/Hong_Kong", "Macau": "Asia/Macau",
    "Thailand": "Asia/Bangkok", "Vietnam": "Asia/Ho_Chi_Minh",
    "Philippines": "Asia/Manila", "India": "Asia/Kolkata", "Nepal": "Asia/Kathmandu",
    "United Arab Emirates": "Asia/Dubai", "Saudi Arabia": "Asia/Riyadh",
    "Qatar": "Asia/Qatar", "Bahrain": "Asia/Bahrain", "Kuwait": "Asia/Kuwait",
    "Egypt": "Africa/Cairo", "Turkey": "Europe/Istanbul", "Israel": "Asia/Jerusalem",
    "United Kingdom": "Europe/London", "Ireland": "Europe/Dublin",
    "France": "Europe/Paris", "Germany": "Europe/Berlin", "Netherlands": "Europe/Amsterdam",
    "Belgium": "Europe/Brussels", "Switzerland": "Europe/Zurich",
    "Spain": "Europe/Madrid", "Italy": "Europe/Rome", "Poland": "Europe/Warsaw",
    "Czech Republic": "Europe/Prague", "Slovakia": "Europe/Bratislava",
    "New Zealand": "Pacific/Auckland", "Puerto Rico": "America/Puerto_Rico",
    "Colombia": "America/Bogota", "Peru": "America/Lima", "Chile": "America/Santiago",
    "Panama": "America/Panama", "Costa Rica": "America/Costa_Rica",
}


def parse_scheduled(text):
    """(datetime, aware) from a `scheduled` value, or (None, False).

    Accepts an offset ("...-07:00"), a Z, or nothing at all. Which of those the
    API sends is the single thing that decides whether the timezone has to be
    derived, so it is read rather than assumed.
    """
    raw = str(text or "").strip()
    if not raw:
        return None, False
    raw = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        when = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None, False
    return when, when.tzinfo is not None


def stream_spans(streams):
    """[(video, start, end)] in UTC for streams that say when they began.

    A stream with no `started_at` - an upload that was never a livestream - has
    no anchor and cannot place anything, so it is left out rather than guessed
    at from its publish date, which is the day, not the minute.
    """
    spans = []
    for video in streams or []:
        started, aware = parse_scheduled(video.get("started_at"))
        if not started or not aware or not video.get("duration"):
            continue
        spans.append((video, started, started + dt.timedelta(seconds=video["duration"])))
    return sorted(spans, key=lambda s: s[1])


def _place(match, when, offset_minutes, spans):
    """Which stream span a naive/aware time falls in, at a given UTC offset.

    Returns (video, seconds into it) or (None, reason).
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone(dt.timedelta(minutes=offset_minutes)))
    hits = [(video, start) for video, start, end in spans
            if start - dt.timedelta(seconds=PRE_ROLL) <= when
            <= end + dt.timedelta(seconds=TAIL)]
    if not hits:
        return None, "outside every stream"
    if len(hits) > 1:
        return None, f"inside {len(hits)} streams at once"
    video, start = hits[0]
    return video, max(int((when - start).total_seconds()), 0)


def score_offset(matches, spans, offset_minutes):
    """How many matches land inside exactly one stream at this UTC offset."""
    landed = 0
    for match in matches:
        when, _ = parse_scheduled(match.get("scheduled"))
        if not when:
            continue
        video, _ = _place(match, when, offset_minutes, spans)
        landed += video is not None
    return landed


def event_zone(event):
    """The IANA zone for an event's location, or None if it is not known."""
    location = (event or {}).get("location") or {}
    country = (location.get("country") or "").strip()
    region = (location.get("region") or "").strip()
    if country == "United States":
        return STATE_ZONES.get(region)
    return COUNTRY_ZONES.get(country)


def zone_offset(event, on=None):
    """The UTC offset in minutes for the event's zone on a given date, or None.

    Resolved through `zoneinfo` on the event's own date rather than stored as a
    number, so an October event in Oregon gets UTC-7 and a January one UTC-8
    without daylight saving being modelled here.
    """
    name = event_zone(event)
    if not name:
        return None
    try:
        zone = zoneinfo.ZoneInfo(name)
    except Exception:              # a zone this machine's tzdata does not carry
        return None
    try:
        day = dt.date.fromisoformat(str((event or {}).get("start") or "")[:10])
    except ValueError:
        day = dt.date.today()
    when = dt.datetime.combine(day, dt.time(12), tzinfo=zone)
    return int(when.utcoffset().total_seconds() // 60)


def search_offset(matches, spans, hint=None):
    """The UTC offset best fitting the streams, as (minutes, landed, total).

    A fallback for an event whose location is not in the tables, and a check on
    the one that is. It is deliberately not the primary mechanism: the plateau is
    broad, because a stream running eight hours with matches spanning three
    admits every offset in a five-hour range equally, and measured against a
    two-day event it picked the wrong edge of that plateau by nearly two hours.

    So among equally-scoring offsets it prefers the one nearest `hint`, and
    without a hint the middle of the plateau - the least-biased choice rather
    than whichever end the iteration reached first.
    """
    scheduled = [m for m in matches if parse_scheduled(m.get("scheduled"))[0]]
    if not scheduled or not spans:
        return None, 0, len(scheduled)
    scored = [(score_offset(scheduled, spans, z), z) for z in CANDIDATES]
    best = max(count for count, _ in scored)
    if not best:
        return None, 0, len(scheduled)
    plateau = sorted(z for count, z in scored if count == best)
    if hint is not None:
        chosen = min(plateau, key=lambda z: (abs(z - hint), z))
    else:
        chosen = plateau[len(plateau) // 2]
    return chosen, best, len(scheduled)


def align_event(event, matches, streams):
    """Offsets for one event's matches. Returns (placements, report).

    `placements` is [{match, video, offset}]; `report` says what happened, which
    is the whole value on an event that resolved badly.
    """
    spans = stream_spans(streams)
    report = {"event": event.get("key"), "streams": len(streams or []),
              "anchored": len(spans), "matches": len(matches),
              "scheduled": 0, "placed": 0, "reasons": {}, "offset": None,
              "aware": False, "zone": event_zone(event), "how": None,
              "hint": zone_offset(event), "coverage": 0.0}
    if not spans:
        report["reasons"]["no stream with a known start time"] = len(matches)
        return [], report

    dated = [m for m in matches if parse_scheduled(m.get("scheduled"))[0]]
    report["scheduled"] = len(dated)
    report["reasons"]["no scheduled time"] = len(matches) - len(dated)
    if not dated:
        return [], report

    report["aware"] = all(parse_scheduled(m.get("scheduled"))[1] for m in dated)
    if report["aware"]:
        offset, report["how"] = 0, "carried"   # the times carry their own offset
    else:
        # The zone first, because it is knowledge rather than inference. The
        # search then has to agree with it: a zone that lands the matches nowhere
        # near a stream is a wrong row, or an event whose location is not where it
        # was streamed from, and the search is the better answer in both cases.
        hint = report["hint"]
        offset, report["how"] = hint, "zone"
        if hint is None or score_offset(dated, spans, hint) < MIN_COVERAGE * len(dated):
            found, landed, total = search_offset(dated, spans, hint=hint)
            if found is None or landed < MIN_COVERAGE * total:
                report["reasons"]["no UTC offset lands these matches in a stream"] = len(dated)
                report["coverage"] = (landed / total) if total else 0.0
                return [], report
            offset, report["how"] = found, "searched" if hint is None else "corrected"
    report["offset"] = offset

    placements = []
    for match in dated:
        when, _ = parse_scheduled(match.get("scheduled"))
        video, result = _place(match, when, offset, spans)
        if video is None:
            report["reasons"][result] = report["reasons"].get(result, 0) + 1
            continue
        placements.append({"match": match, "video": video, "offset": result})
    report["placed"] = len(placements)
    report["coverage"] = len(placements) / len(dated) if dated else 0.0
    return placements, report


# --- writing it back ------------------------------------------------------

def _mergeable(match):
    """Whether this match's footage may be overwritten automatically.

    Absent footage, or footage this module wrote. Anything else was linked by a
    person - including everything linked before this module existed - and a
    derived guess must never displace a typed-in answer.
    """
    video = match.get("video") or {}
    if not video.get("url"):
        return True
    return video.get("source") == "auto"


def apply_event(event, matches, streams, directory=catalog.CATALOG_DIR, dry_run=False):
    """Write derived offsets onto an event's matches. Returns (written, report)."""
    placements, report = align_event(event, matches, streams)
    report["skipped_manual"] = 0
    written = 0
    for placement in placements:
        match = placement["match"]
        if not _mergeable(match):
            report["skipped_manual"] += 1
            continue
        if dry_run:
            written += 1
            continue
        # Merged, never replaced: a local segment's file and frame range key the
        # calibration and seed stores, and dropping them would orphan both.
        video = dict(match.get("video") or {})
        video.update({"url": placement["video"]["url"],
                      "watch_start": placement["offset"],
                      "source": "auto", "approximate": True,
                      # How the offset was arrived at, because the accuracies
                      # differ enough to matter: "carried" and "zone" are the
                      # schedule's own drift, while "searched" adds the width of
                      # the plateau on top - measured at over an hour on an event
                      # with no location to key a zone from.
                      "how": report["how"],
                      "video_id": placement["video"].get("id")})
        # `save_match` rebuilds the record from its arguments - `alliances` is
        # not merged - so every field is carried through. Passing only the video
        # would blank the teams and scores of every match it touched, and nothing
        # downstream would show that it had happened.
        alliances = match.get("alliances") or {}
        red, blue = alliances.get("red") or {}, alliances.get("blue") or {}
        catalog.save_match(
            event=match.get("event"), round_slug=match.get("round", "unknown"),
            instance=match.get("instance", 1), number=match.get("number", 1),
            division=match.get("division"), key=match.get("key"),
            red=red.get("teams") or (), blue=blue.get("teams") or (),
            red_score=red.get("score"), blue_score=blue.get("score"),
            name=match.get("name"), field=match.get("field"),
            api_id=match.get("api_id"), scheduled=match.get("scheduled"),
            # Linking footage says nothing about where the match data came from.
            source=match.get("source", "manual"),
            video=video, directory=directory)
        written += 1
    report["written"] = written
    return written, report


def run(event_keys=None, directory=catalog.CATALOG_DIR, dry_run=False,
        path=event_streams.STREAM_FILE, log=print):
    """Align every event that has a resolved stream and scheduled matches."""
    events = catalog.list_events(directory)
    all_matches = catalog.list_matches(directory)
    streams = event_streams.load_all(path)

    by_event = {}
    for match in all_matches.values():
        by_event.setdefault(str(match.get("event") or "").upper(), []).append(match)

    wanted = {k.strip().upper() for k in event_keys} if event_keys else None
    totals = {"events": 0, "written": 0, "placed": 0, "manual": 0, "refused": 0}
    for key, event in sorted(events.items()):
        if wanted and key.upper() not in wanted:
            continue
        record = streams.get(key.upper()) or {}
        videos = record.get("videos") or []
        matches = by_event.get(key.upper()) or []
        if not videos or not matches:
            continue

        written, report = apply_event(event, matches, videos, directory=directory,
                                      dry_run=dry_run)
        totals["events"] += 1
        totals["written"] += written
        totals["placed"] += report["placed"]
        totals["manual"] += report.get("skipped_manual", 0)
        totals["refused"] += not report["placed"] and bool(report["scheduled"])

        log(f"{key}: {report['placed']}/{report['scheduled']} scheduled match(es) "
            f"placed across {report['anchored']}/{report['streams']} anchored stream(s)"
            + (f", {written} written" if not dry_run else f", {written} would be written")
            + (f", {report['skipped_manual']} left as linked by hand"
               if report.get("skipped_manual") else ""))
        how = report["how"]
        if how == "carried":
            log("    scheduled times carry their own UTC offset, so none was inferred")
        elif how == "zone":
            log(f"    UTC{report['offset'] / 60:+.2f} from {report['zone']}")
        elif how == "corrected":
            log(f"    UTC{report['offset'] / 60:+.2f} found by search - "
                f"{report['zone']} would have been UTC{report['hint'] / 60:+.2f}, "
                "which lands these matches outside the streams")
        elif how == "searched":
            log(f"    UTC{report['offset'] / 60:+.2f} found by search; "
                "this event's location is not in the zone tables")
        for reason, count in sorted(report["reasons"].items(), key=lambda kv: -kv[1]):
            if count:
                log(f"    {count} - {reason}")

    log(f"\n{totals['events']} event(s): {totals['placed']} match(es) placed, "
        f"{totals['written']} {'would be ' if dry_run else ''}written, "
        f"{totals['manual']} left as linked by hand, "
        f"{totals['refused']} event(s) refused")
    return totals


def main():
    parser = argparse.ArgumentParser(
        description="Give every scheduled match an approximate offset into its event's stream")
    parser.add_argument("--event", action="append",
                        help="align only this event key/SKU (repeatable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what the data supports; write nothing")
    parser.add_argument("--directory", default=catalog.CATALOG_DIR,
                        help="catalog directory (default the V5RC one)")
    args = parser.parse_args()
    run(event_keys=args.event, directory=args.directory, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
