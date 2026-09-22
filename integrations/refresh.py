"""Bring every source up to date in one command.

    python -m integrations.refresh

Four sources feed this tool - the VEX Events API for each program, the webcast
listing, and YouTube - and each was its own command with its own arguments.
Worse, the API responses are cached with no expiry, so the obvious way to
update, re-running the import, replays last week's responses and changes
nothing. `--refresh` fixes that by ignoring the cache entirely, which is the
other extreme: a season is mostly events that finished months ago, and
re-fetching all of them costs an hour to learn nothing.

So this walks the middle. The event listings are always re-fetched, because a
cached listing cannot contain an event announced since. An individual event is
re-fetched only when its results could still move:

- it is running now, or ended within RECENT_DAYS
- it starts within SOON_DAYS, where the roster is still filling up
- it is new, or has no matches stored, so there is nothing to serve from cache

Everything else is served from cache, which is correct rather than merely
cheap: a tournament that ended in April is not going to report a different
score in September.

`--full` ignores all of that and re-fetches everything, for the rare case where
the cache is suspected of holding something wrong.
"""
import argparse
import datetime as _dt
import time

from integrations import vex_events, webcasts, youtube
from storage import catalog

# A program, its season, and where its catalog lives. Season ids are not stable
# across VEX's program renames, so `--season` overrides these when a new season
# starts before this table is updated.
PROGRAMS = {
    "v5rc": {"season": 204, "directory": vex_events.PROGRAM_CATALOGS["v5rc"],
             "label": "V5RC"},
    "viqrc": {"season": 203, "directory": vex_events.PROGRAM_CATALOGS["viqrc"],
              "label": "VIQRC"},
}

RECENT_DAYS = 7      # an event that ended this recently may still be publishing results
SOON_DAYS = 30       # an event this close is still taking registrations
EMPTY_DAYS = 30      # how long to keep asking an event that published no matches


def _date(value):
    """The date part of an API timestamp, or None."""
    text = str(value or "")[:10]
    try:
        return _dt.date.fromisoformat(text)
    except ValueError:
        return None


def should_refetch(payload, stored_events, stored_matches, today=None,
                   recent_days=RECENT_DAYS, soon_days=SOON_DAYS, empty_days=EMPTY_DAYS):
    """Could this event's data have changed since it was last fetched?

    Deliberately generous at the edges: a false yes costs a handful of
    requests, a false no means a result that silently never appears.
    """
    today = today or _dt.date.today()
    start, end = _date(payload.get("start")), _date(payload.get("end"))
    key = catalog.event_key(sku=payload.get("sku"), name=payload.get("name"))

    if key not in stored_events:
        return True                                  # never seen before
    if (end or start) is None:
        return True                                  # undated, so no way to rule it out
    if start and start <= today and (end or start) >= today:
        return True                                  # running now
    if end and 0 <= (today - end).days <= recent_days:
        return True                                  # just finished
    if start and 0 <= (start - today).days <= soon_days:
        return True                                  # roster still filling
    # A finished event we hold no matches for is worth asking again for a
    # while - results are sometimes published days late - but not forever.
    # Plenty of events, league nights and cancellations among them, simply
    # never publish any, and re-asking those every run is a standing cost for
    # an answer that will not change.
    finished = end or start
    if finished < today and key not in stored_matches:
        return (today - finished).days <= empty_days
    return False


def _match_events(directory):
    """Event keys that already have at least one match stored."""
    return {str(m.get("event") or "").upper()
            for m in catalog.list_matches(directory).values()}


def refresh_program(code, season=None, full=False, region=None, log=print):
    """Update one program's catalog, re-fetching only what could have moved."""
    program = PROGRAMS[code]
    directory = program["directory"]
    stored_events = {k.upper() for k in catalog.list_events(directory)}
    stored_matches = _match_events(directory)

    def wanted(payload):
        return should_refetch(payload, stored_events, stored_matches)

    if log:
        log(f"\n{program['label']}: season {season or program['season']} -> {directory}")
    return vex_events.import_season(
        season or program["season"], region=region, directory=directory,
        refresh=True if full else wanted, log=log)


def main():
    parser = argparse.ArgumentParser(description="Update every source in one pass")
    parser.add_argument("--programs", nargs="+", choices=sorted(PROGRAMS),
                        default=sorted(PROGRAMS), help="which programs to update")
    parser.add_argument("--season", type=int,
                        help="season id, for a single program whose season has rolled over")
    parser.add_argument("--region", help="limit the walk to one API region")
    parser.add_argument("--full", action="store_true",
                        help="ignore the cache completely - slow, and rarely needed")
    parser.add_argument("--no-events", action="store_true", help="skip the API walk")
    parser.add_argument("--no-webcasts", action="store_true")
    parser.add_argument("--no-youtube", action="store_true")
    parser.add_argument("--budget", type=int, default=youtube.DEFAULT_BUDGET,
                        help="YouTube quota units to spend at most")
    args = parser.parse_args()

    if args.season and len(args.programs) != 1:
        parser.error("--season applies to one program; name it with --programs")

    started = time.time()
    summary = []

    if not args.no_events:
        for code in args.programs:
            totals = refresh_program(code, season=args.season, full=args.full,
                                     region=args.region)
            moved = totals.get("refreshed")
            summary.append(
                f"{PROGRAMS[code]['label']}: {totals['events']} events, "
                f"{totals['matches']} matches"
                + (f", {moved} re-fetched" if moved is not None else "")
                + (f", {len(totals['failures'])} failed" if totals["failures"] else ""))

    if not args.no_webcasts:
        print("\nWebcasts")
        try:
            counts = webcasts.refresh()
            summary.append(f"webcasts: {counts['added']} new, {counts['changed']} changed, "
                           f"{counts['total']} stored")
        except Exception as error:                  # one dead page must not stop the rest
            print(f"  failed: {type(error).__name__}: {error}")
            summary.append(f"webcasts: FAILED ({type(error).__name__})")

    if not args.no_youtube:
        print("\nYouTube")
        try:
            result = youtube.run_batch(budget=args.budget)
            summary.append(f"youtube: {result['searched']} team(s) searched, "
                           f"{result['units']} units spent, {result['remaining']} still due")
        except youtube.KeyMissing as error:
            print(f"  skipped: {error}")
            summary.append("youtube: skipped (no API key)")
        except Exception as error:
            print(f"  failed: {type(error).__name__}: {error}")
            summary.append(f"youtube: FAILED ({type(error).__name__})")

    print(f"\nDone in {(time.time() - started) / 60:.1f} min")
    for line in summary:
        print(f"  {line}")


if __name__ == "__main__":
    main()
