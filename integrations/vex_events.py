"""Read teams, events and matches from the Public VEX Events API.

The RobotEvents API this project was going to use no longer exists. VEX ended
its relationship with RECF in May 2026; robotevents.com now serves a single
notice page and every `/api/v2` path there returns 404. The same API - same
schema, same Bearer auth, same Laravel-style pagination - now lives at
`https://events.vex.com/api/v2` under the title "Public VEX Events API".

The published spec documents only `/events`, `/teams`, `/programs` and
`/seasons`, but the rest of the routing table is intact: probing shows unknown
paths return 404 while `/events/{id}/divisions/{div}/matches`,
`/events/{id}/teams` and `/teams/{id}/matches` all return 302 to the login page,
which is what an existing route does without a token. Matches are reachable only
per division - `/events/{id}/matches` genuinely is a 404.

Three things measured against a real event (RE-V5RC-26-4244, 118 teams,
273 matches) shape the code below:

- `per_page` caps at 250 and that event needed two pages, with *every*
  elimination match on the second. An importer that forgot to paginate would
  have silently imported qualifiers only and looked like it worked. So
  `paginate` refuses to return a short result quietly.
- `scored` was false on all 273 matches, including ones carrying real scores.
  Nothing here filters on it; doing so would discard the whole event.
- Season and program ids are not stable across the VRC -> V5RC rename, so they
  are read from the API rather than hardcoded.
"""
import hashlib
import json
import os
import time

import requests

from storage import catalog, event_details

BASE_URL = "https://events.vex.com/api/v2"
TOKEN_ENV = "VEX_EVENTS_TOKEN"
TOKEN_FILE = os.path.join("data", "vex_token")
CACHE_DIR = os.path.join("data", "cache", "vex_events")

MAX_PER_PAGE = 250
DEFAULT_TIMEOUT = 45

# Measured, not assumed: a 686-event walk was throttled 21 times and every one
# was a plain 429. Community reports of this API answering 403 for throttling
# were not reproduced here, but a 403 is still treated as ambiguous - read as
# throttling only once a request has already succeeded on this client, since
# before that the likeliest cause by far is a bad token.
RETRY_STATUSES = (403, 429, 500, 502, 503, 504)
MAX_RETRIES = 6
BACKOFF_SECONDS = 3.0

# Politeness gap between calls. Importing one event is a few hundred requests at
# worst, so this costs seconds and avoids tripping a limiter whose rules are
# not published.
MIN_INTERVAL = 0.25


class VexEventsError(RuntimeError):
    pass


class AuthError(VexEventsError):
    pass


class RateLimited(VexEventsError):
    pass


class Unreachable(VexEventsError):
    """The network, not the API, refused to cooperate."""


class TruncatedPage(VexEventsError):
    """A paged walk came back with fewer rows than the API said existed."""


def load_token(token=None, token_file=TOKEN_FILE):
    """The bearer token, from the argument, the environment, or a local file.

    Never logged or echoed anywhere: it is a credential for the user's own
    account, and a token leaked into a terminal transcript has to be revoked.
    """
    if token:
        return token.strip()
    from_env = os.environ.get(TOKEN_ENV)
    if from_env and from_env.strip():
        return from_env.strip()
    try:
        with open(token_file) as handle:
            found = handle.read().strip()
    except OSError:
        found = ""
    if not found:
        raise AuthError(
            f"no API token. Put one in {token_file} or set {TOKEN_ENV}. "
            "Request access at https://events.vex.com/api/v2 while logged in."
        )
    return found


class Client:
    """One session against the API, with a disk cache and a request floor."""

    def __init__(self, token=None, cache_dir=CACHE_DIR, base_url=BASE_URL,
                 timeout=DEFAULT_TIMEOUT, min_interval=MIN_INTERVAL, use_cache=True):
        self.token = load_token(token)
        self.base_url = base_url.rstrip("/")
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.min_interval = min_interval
        self.use_cache = use_cache
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "vex-tracker",
        })
        self._last_request = 0.0
        self._succeeded_once = False
        self.requests_made = 0
        self.cache_hits = 0

    # ------------------------------------------------------------------ cache

    def _cache_path(self, path, params):
        stamp = json.dumps([path, sorted((params or {}).items())], sort_keys=True, default=str)
        return os.path.join(self.cache_dir, hashlib.sha1(stamp.encode()).hexdigest()[:20] + ".json")

    def _cached(self, path, params):
        try:
            with open(self._cache_path(path, params)) as handle:
                return json.load(handle)["payload"]
        except (OSError, ValueError, KeyError):
            return None

    def _store(self, path, params, payload):
        os.makedirs(self.cache_dir, exist_ok=True)
        target = self._cache_path(path, params)
        temporary = f"{target}.tmp"
        with open(temporary, "w") as handle:
            json.dump({"path": path, "params": params, "fetched_at": time.time(),
                       "payload": payload}, handle)
        os.replace(temporary, target)

    # ---------------------------------------------------------------- fetching

    def get(self, path, refresh=False, **params):
        """One page of JSON from `path`.

        A response that is not JSON means the request was bounced to the login
        page, which `requests` follows into a 200 of HTML - so a non-JSON body
        is an auth failure, not a parse problem, and is reported as one.
        """
        params = {k: v for k, v in params.items() if v not in (None, "", [], ())}
        if self.use_cache and not refresh:
            hit = self._cached(path, params)
            if hit is not None:
                self.cache_hits += 1
                return hit

        url = f"{self.base_url}/{path.lstrip('/')}"
        delay = BACKOFF_SECONDS
        last = None
        for attempt in range(MAX_RETRIES):
            gap = self.min_interval - (time.monotonic() - self._last_request)
            if gap > 0:
                time.sleep(gap)
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as error:
                self._last_request = time.monotonic()
                self.requests_made += 1
                last = f"{type(error).__name__}"
                if attempt < MAX_RETRIES - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise Unreachable(f"{url} unreachable after {MAX_RETRIES} attempts: {error}") from None
            self._last_request = time.monotonic()
            self.requests_made += 1

            if response.status_code == 404:
                raise VexEventsError(f"no such endpoint: {url}")
            if response.status_code in RETRY_STATUSES:
                if response.status_code == 403 and not self._succeeded_once:
                    raise AuthError(
                        "the API refused the token (403 on the first call). Check that "
                        "the access request was approved and the token was copied whole."
                    )
                last = response.status_code
                if attempt < MAX_RETRIES - 1:
                    # Retry-After is the server saying exactly how long to wait;
                    # guessing shorter than that just burns another attempt.
                    wait = delay
                    header = response.headers.get("Retry-After")
                    if header:
                        try:
                            wait = max(wait, float(header))
                        except ValueError:
                            pass
                    time.sleep(min(wait, 60.0))
                    delay *= 2
                    continue
                raise RateLimited(f"{url} kept returning {last} after {MAX_RETRIES} attempts")

            try:
                payload = response.json()
            except ValueError:
                raise AuthError(
                    f"{url} returned {response.status_code} but not JSON - the request was "
                    "redirected to the login page, so the token is missing or rejected."
                ) from None

            self._succeeded_once = True
            if self.use_cache:
                self._store(path, params, payload)
            return payload

        raise RateLimited(f"{url} failed after {MAX_RETRIES} attempts (last status {last})")

    def paginate(self, path, refresh=False, per_page=MAX_PER_PAGE, **params):
        """Every row across every page.

        The API reports `meta.total`, and this refuses to hand back fewer rows
        than that. Silent truncation is the failure that matters here: the
        measured event put all of its elimination matches on page two, so a
        walk that stopped early would return a complete-looking list of
        qualifiers and nothing would look wrong downstream.
        """
        rows, seen, total, page = [], set(), None, 1
        while True:
            payload = self.get(path, refresh=refresh, page=page,
                               per_page=min(int(per_page), MAX_PER_PAGE), **params)
            batch = payload.get("data")
            if batch is None:
                raise VexEventsError(f"{path} returned no data field: {str(payload)[:200]}")
            meta = payload.get("meta") or {}
            total = meta.get("total", total)

            for row in batch:
                # Deduplicated by id: a live event can shift rows between pages,
                # which would otherwise double-count across the boundary.
                marker = row.get("id")
                if marker is not None and marker in seen:
                    continue
                if marker is not None:
                    seen.add(marker)
                rows.append(row)

            if not meta.get("next_page_url") or not batch:
                break
            page += 1

        if total is not None and len(rows) < total:
            raise TruncatedPage(
                f"{path} reported {total} rows but the walk collected {len(rows)}"
            )
        return rows


# ------------------------------------------------------------------- mapping
#
# The API's shapes and the catalog's are deliberately close, so these are
# renames rather than transformations. Anything the API leaves out stays absent
# instead of being filled with a plausible default.


def _location(payload):
    loc = payload or {}
    return {"city": loc.get("city") or None,
            "region": loc.get("region") or None,
            "country": loc.get("country") or None}


def team_fields(team):
    return {
        "number": team["number"],
        "name": team.get("team_name") or None,
        "organization": team.get("organization") or None,
        "robot_name": team.get("robot_name") or None,
        "location": _location(team.get("location")),
        "grade": team.get("grade") or None,
        "program": (team.get("program") or {}).get("code") or None,
        "api_id": team.get("id"),
    }


def event_fields(event):
    season = event.get("season") or {}
    return {
        "name": event.get("name"),
        "sku": event.get("sku"),
        "season": season.get("name") or None,
        "season_id": season.get("id"),
        "program": (event.get("program") or {}).get("code") or None,
        "start": (event.get("start") or "")[:10] or None,
        "end": (event.get("end") or "")[:10] or None,
        "location": _location(event.get("location")),
        "level": event.get("level") or None,
        "divisions": event.get("divisions") or [],
        "ongoing": event.get("ongoing"),
        "api_id": event.get("id"),
    }


def match_fields(match):
    """Alliance colours come from the payload rather than from list order.

    `alliances` is an array, and nothing promises red comes first - reading it
    positionally would silently swap every score at any event that ordered them
    the other way.
    """
    sides = {}
    for alliance in match.get("alliances") or []:
        colour = alliance.get("color")
        if colour not in catalog.ALLIANCES:
            continue
        sides[colour] = {
            "teams": [t["team"]["name"] for t in alliance.get("teams") or []
                      if (t.get("team") or {}).get("name")],
            "score": alliance.get("score"),
        }
    red, blue = sides.get("red", {}), sides.get("blue", {})
    return {
        "round_slug": catalog.ROUND_FROM_API.get(match.get("round"), "unknown"),
        "instance": match.get("instance") or 1,
        "number": match.get("matchnum") or 1,
        "name": match.get("name") or None,
        "division": (match.get("division") or {}).get("name"),
        "scheduled": match.get("scheduled") or None,
        "field": match.get("field") or None,
        "red": red.get("teams", []), "blue": blue.get("teams", []),
        "red_score": red.get("score"), "blue_score": blue.get("score"),
        "api_id": match.get("id"),
    }


def award_fields(award):
    """`teamWinners` is absent from the published Award schema but present in
    every live response, so it is read from the response rather than the spec."""
    return {
        "title": award.get("title"),
        "order": award.get("order"),
        "winners": [w["team"]["name"] for w in (award.get("teamWinners") or [])
                    if (w.get("team") or {}).get("name")],
        "qualifications": award.get("qualifications") or [],
        "designation": award.get("designation"),
        "classification": award.get("classification"),
    }


def ranking_fields(row):
    return {
        "rank": row.get("rank"),
        "team": (row.get("team") or {}).get("name"),
        "division": (row.get("division") or {}).get("name"),
        "wins": row.get("wins"), "losses": row.get("losses"), "ties": row.get("ties"),
        "wp": row.get("wp"), "ap": row.get("ap"), "sp": row.get("sp"),
        "high_score": row.get("high_score"),
        "average_points": row.get("average_points"),
        "total_points": row.get("total_points"),
    }


def skill_fields(row):
    return {
        "team": (row.get("team") or {}).get("name"),
        "type": row.get("type"),
        "rank": row.get("rank"),
        "score": row.get("score"),
        "attempts": row.get("attempts"),
    }


# ------------------------------------------------------------------- importing


def find_events(client=None, sku=None, season_id=None, region=None, team=None,
                start=None, end=None, refresh=False):
    """Search events. Every filter is optional; passing none lists everything."""
    client = client or Client()
    params = {}
    if sku:
        params["sku[]"] = [sku] if isinstance(sku, str) else list(sku)
    if season_id:
        params["season[]"] = [season_id] if isinstance(season_id, int) else list(season_id)
    if team:
        params["team[]"] = [team] if isinstance(team, int) else list(team)
    if region:
        params["region"] = region
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    return client.paginate("/events", refresh=refresh, **params)


def seasons(client=None, program_id=None, refresh=False):
    client = client or Client()
    params = {"program[]": [program_id]} if program_id else {}
    return client.paginate("/seasons", refresh=refresh, **params)


def _store_event(payload, client, with_teams=True, with_matches=True, refresh=False,
                 directory=catalog.CATALOG_DIR, log=print, with_details=True):
    """Save one already-fetched event payload, plus its teams and matches.

    Takes the payload rather than a SKU so a bulk walk does not re-request every
    event it has already listed - the season listing returns full event objects,
    so looking each one up again would be several hundred wasted requests.
    """
    fields = event_fields(payload)
    event = catalog.save_event(source="vex-api", directory=directory, **fields)
    status = catalog.event_status(event)
    counts = {"event": event, "status": status, "teams": 0, "matches": 0}

    if with_teams:
        teams = client.paginate(f"/events/{payload['id']}/teams", refresh=refresh)
        grades = {}
        for team in teams:
            row = team_fields(team)
            catalog.save_team(source="vex-api", directory=directory, **row)
            if row.get("grade"):
                grades[row["grade"]] = grades.get(row["grade"], 0) + 1
        counts["teams"] = len(teams)
        # The API gives no grade for an event, so it is derived from who
        # registered - a tally rather than the roster, since the tally is what
        # the grade filter needs and a full roster per event would bloat the
        # table every page load reads.
        if teams:
            event = catalog.save_event(source="vex-api", directory=directory,
                                       grades=grades or None,
                                       teams=[team_fields(t)["number"] for t in teams],
                                       **fields)
            counts["event"] = event

    # An event that has not happened yet has no matches to fetch. Asking anyway
    # would be one wasted request per future event, and this season is mostly
    # future events - 622 of 686 when this was written.
    if with_matches and status != "upcoming":
        seen_keys = set()
        for division in fields["divisions"] or [{"id": 1, "name": "default"}]:
            rows = client.paginate(
                f"/events/{payload['id']}/divisions/{division['id']}/matches", refresh=refresh)

            # The readable key is derived from round/instance/number/division,
            # and the API does not guarantee those are unique: one event was
            # measured serving two distinct match ids both called "Qualifier #1"
            # in the same division. Where that happens the API's own id is
            # appended, so a duplicate is kept and visible rather than
            # overwriting the match it collides with.
            fields_by_row = [match_fields(m) for m in rows]
            derived = [catalog.match_key(event["key"], f["round_slug"], f["instance"],
                                         f["number"], f["division"]) for f in fields_by_row]
            clashing = {k for k in derived if derived.count(k) > 1}
            for key_, row in zip(derived, fields_by_row):
                explicit = f"{key_}-{row['api_id']}" if key_ in clashing else None
                saved = catalog.save_match(event=event["key"], source="vex-api", key=explicit,
                                           directory=directory, **row)
                seen_keys.add(saved["key"])
            if clashing and log:
                log(f"  {event['key']}: {len(clashing)} duplicate match id(s) from the API, "
                    "kept with their ids appended")
            counts["matches"] += len(rows)

        # Matches the API no longer lists are removed, so a re-import converges
        # instead of accumulating. Anything with footage linked is kept and
        # reported: a stale key is a nuisance, but silently dropping the only
        # record of which video covers which match is not recoverable.
        stale = [m for m in catalog.list_matches(directory).values()
                 if str(m.get("event") or "").upper() == event["key"].upper()
                 and m["key"] not in seen_keys]
        for match in stale:
            if catalog.segment_of(match):
                if log:
                    log(f"  kept {match['key']} - no longer served by the API but has footage")
                continue
            catalog.delete_match(match["key"], directory=directory)
            counts["pruned"] = counts.get("pruned", 0) + 1

    # Awards, rankings and skills exist only once an event has run. Fetching them
    # for the 622 upcoming events would be ~1800 requests returning nothing.
    if with_details and status != "upcoming":
        awards = [award_fields(a) for a in
                  client.paginate(f"/events/{payload['id']}/awards", refresh=refresh)]
        skills = [skill_fields(s) for s in
                  client.paginate(f"/events/{payload['id']}/skills", refresh=refresh)]
        rankings = []
        for division in fields["divisions"] or [{"id": 1, "name": "default"}]:
            rankings += [ranking_fields(r) for r in client.paginate(
                f"/events/{payload['id']}/divisions/{division['id']}/rankings", refresh=refresh)]
        # Sorted on the way in: the API returns rankings worst-first, so anything
        # that trusted the order would show the bottom of the table as the top.
        rankings.sort(key=lambda r: (r.get("rank") is None, r.get("rank") or 0))
        event_details.save(event["key"], awards=awards, rankings=rankings, skills=skills)
        counts["awards"] = len(awards)
        counts["rankings"] = len(rankings)
        counts["skills"] = len(skills)

    if log:
        log(f"  {event['key']:<17} {status:<8} teams={counts['teams']:<4} "
            f"matches={counts['matches']:<4} {(event.get('name') or '')[:44]}")
    return counts


def import_event(sku, client=None, with_teams=True, with_matches=True,
                 refresh=False, directory=catalog.CATALOG_DIR, log=print, with_details=True):
    """Pull one event, its teams and its matches into the catalog.

    Nothing here deletes or overwrites local work. Matches are saved without a
    `video`, which `catalog.save_match` reads as "leave whatever is linked
    alone", so importing over a match whose footage was linked by hand keeps the
    footage and only refreshes the alliances and scores.
    """
    client = client or Client()
    found = find_events(client, sku=sku, refresh=refresh)
    if not found:
        raise VexEventsError(f"no event with SKU {sku}")
    result = _store_event(found[0], client, with_teams, with_matches, refresh, directory,
                          log, with_details=with_details)
    if log:
        log(f"  {client.requests_made} request(s), {client.cache_hits} from cache")
    return result


def import_season(season_id, client=None, region=None, with_teams=True,
                  with_matches=True, refresh=False, directory=catalog.CATALOG_DIR,
                  log=print, on_error="continue", retry_failures=True, with_details=True):
    """Import every event in a season.

    One event's failure does not abandon the walk: a single malformed or
    restricted event would otherwise cost the whole run, which is minutes of
    requests. Failures are collected and reported at the end instead.

    Safe to re-run. Every response is cached, so a second pass over an
    unchanged season makes no requests at all and simply re-saves identical
    records - which is also how an interrupted run is resumed.
    """
    client = client or Client()
    events = find_events(client, season_id=season_id, region=region, refresh=refresh)
    events.sort(key=lambda e: (e.get("start") or "", e.get("sku") or ""))
    if log:
        log(f"{len(events)} event(s) in season {season_id}"
            + (f", region {region}" if region else ""))

    totals = {"events": 0, "teams": 0, "matches": 0,
              "past": 0, "ongoing": 0, "upcoming": 0, "unknown": 0}
    failures = []
    for index, payload in enumerate(events, start=1):
        try:
            counts = _store_event(payload, client, with_teams, with_matches,
                                  refresh, directory, log=None, with_details=with_details)
        except VexEventsError as error:
            failures.append((payload.get("sku"), f"{type(error).__name__}: {error}"))
            if on_error != "continue":
                raise
            continue
        totals["events"] += 1
        totals["teams"] += counts["teams"]
        totals["matches"] += counts["matches"]
        totals[counts["status"]] = totals.get(counts["status"], 0) + 1
        if log and (index % 25 == 0 or index == len(events)):
            log(f"  [{index:>4}/{len(events)}] {totals['matches']:>6} matches, "
                f"{len(catalog.list_teams(directory)):>5} teams, "
                f"{client.requests_made} requests, {len(failures)} failed")

    # Everything that succeeded is cached, so this pass re-requests only what
    # actually failed - a few seconds rather than another full walk.
    if failures and retry_failures:
        if log:
            log(f"retrying {len(failures)} failed event(s)")
        by_sku = {e.get("sku"): e for e in events}
        still = []
        for sku, why in failures:
            payload = by_sku.get(sku)
            if payload is None:
                still.append((sku, why))
                continue
            try:
                counts = _store_event(payload, client, with_teams, with_matches,
                                      refresh, directory, log=None, with_details=with_details)
            except VexEventsError as error:
                still.append((sku, f"{type(error).__name__}: {error}"))
                continue
            totals["events"] += 1
            totals["teams"] += counts["teams"]
            totals["matches"] += counts["matches"]
            totals[counts["status"]] = totals.get(counts["status"], 0) + 1
        failures = still

    if log:
        log(f"done: {totals['events']} events "
            f"({totals['past']} past, {totals['ongoing']} ongoing, {totals['upcoming']} upcoming), "
            f"{totals['matches']} matches, {len(catalog.list_teams(directory))} teams")
        log(f"  {client.requests_made} requests, {client.cache_hits} from cache, "
            f"{len(failures)} still failing")
        for sku, why in failures:
            log(f"  FAILED {sku}: {why[:90]}")
    totals["failures"] = failures
    return totals


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Import events from the VEX Events API")
    parser.add_argument("sku", nargs="?", help="event SKU, e.g. RE-V5RC-26-4244")
    parser.add_argument("--season", type=int, help="import every event in this season id")
    parser.add_argument("--region", help="with --season, limit to one region")
    parser.add_argument("--no-teams", action="store_true")
    parser.add_argument("--no-matches", action="store_true")
    parser.add_argument("--no-details", action="store_true",
                        help="skip awards, rankings and skills")
    parser.add_argument("--refresh", action="store_true", help="ignore the cache")
    args = parser.parse_args()

    if not (args.sku or args.season):
        parser.error("give an event SKU or --season")
    common = dict(with_teams=not args.no_teams, with_matches=not args.no_matches,
                  with_details=not args.no_details, refresh=args.refresh)
    if args.season:
        import_season(args.season, region=args.region, **common)
    else:
        import_event(args.sku, **common)


if __name__ == "__main__":
    main()
