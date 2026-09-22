# integrations

Outside data sources. Separate from `storage/` because these reach the network
and can fail, throttle, or return something unexpected, where a store only
touches local disk. Each module converts what it fetches into the plain records
`storage.catalog` already understands, so nothing downstream knows whether a
match was typed in or imported.

| file | holds |
|---|---|
| `vex_events.py` | the Public VEX Events API: events, teams, matches |
| `webcasts.py` | webcast links scraped from `events.vex.com/webcasts` — links only, nothing downloaded |
| `youtube.py` | each team's YouTube channel and robot videos, via the YouTube Data API — links only |

## The API moved

This project was written against the RobotEvents API. That API is gone — VEX
ended its relationship with RECF in May 2026, `robotevents.com` now serves a
single notice page, and every `/api/v2` path there returns 404.

The same API, with the same schema and the same Bearer auth, now lives at
`https://events.vex.com/api/v2` as the **Public VEX Events API**.

The published spec documents only `/events`, `/teams`, `/programs` and
`/seasons`, but the rest of the routing table is intact — unknown paths return
404 while `/events/{id}/teams`, `/events/{id}/divisions/{div}/matches` and
`/teams/{id}/matches` all return 302 to login, which is what a real route does
without a token. Matches are reachable **only** per division;
`/events/{id}/matches` really is a 404.

## Getting a token

Log in at events.vex.com, open `/api/v2`, click **Request Access** (approval is
typically instant), then create a token and copy it — it is shown once.

Put it in `data/vex_token` (`data/` is gitignored) or set `VEX_EVENTS_TOKEN`,
which takes precedence. It is never logged or printed.

## Importing

```
python -m integrations.vex_events RE-V5RC-26-4244
```

Re-running is safe and, with the cache warm, makes no requests at all.

## Three things measured against a real event

Event `RE-V5RC-26-4244` — 118 teams, 273 matches — drove three decisions:

- **`per_page` caps at 250, and that event needed two pages with *every*
  elimination match on the second.** An importer that forgot to paginate would
  have imported qualifiers only and looked like it worked. `paginate` therefore
  refuses to return fewer rows than `meta.total` reports, rather than trusting
  the walk.
- **`scored` was `false` on all 273 matches**, including ones carrying real
  scores. Nothing filters on it; doing so would discard the whole event.
- **Season and program ids are not stable** across the VRC → V5RC rename, so
  they are read from the API rather than hardcoded.

A fourth, about the data rather than the code: that event's two finals both
record the blue alliance on 0, from an alliance that scored 96 in its semifinal.
Imported as-is and not "corrected" — API scores are not automatically ground
truth either.

## Importing a whole season

```
python -m integrations.vex_events --season 204          # every event in a season
python -m integrations.vex_events --season 204 --region Minnesota
```

The season listing already returns full event objects, so the walk reuses them
rather than re-requesting each event by SKU — several hundred requests saved.
Events that have not happened yet are skipped for matches (they have none),
which on a season that is mostly future events avoids one wasted request each.

Failures are collected rather than fatal: one bad event should not abandon a
walk that costs half an hour. At the end the failures are retried once, and
since every success is cached that pass re-requests only what actually failed.

Transient failures are normal over several hundred requests. On a full
686-event walk, 21 `teams` fetches were throttled and every one succeeded on
retry; no `matches` fetch failed. Cache coverage is the way to find them — a
failed fetch is never written to the cache, so a missing cache entry names
exactly what did not complete.

**The throttling response is 429**, measured over that walk, despite community
reports of this API answering 403 instead. `Retry-After` is honoured when sent.
A 403 is still treated as ambiguous: throttling if a request has already
succeeded on that client, otherwise a bad token.

## Updating everything

```
python -m integrations.refresh              # both programs, webcasts, YouTube
python -m integrations.refresh --programs v5rc --no-youtube
python -m integrations.refresh --full       # trust nothing, re-fetch it all
```

The obvious way to update - re-running the import - does nothing, because
responses are cached with no expiry, so the walk replays last week's answers.
`--refresh` cures that by ignoring the cache entirely, which is the opposite
problem: most of a season is events that finished months ago and cannot change,
and re-fetching all of them costs an hour to learn nothing.

`refresh.py` re-fetches only what could have moved. Listings always, because a
cached listing cannot contain an event announced since; then an event when it
is running, ended within 7 days, starts within 30, is new, or finished within
the last 30 days without publishing any matches. Measured on the current
catalogs that is 126 of 686 V5RC events and 107 of 704 VIQRC - about 470
requests, against roughly 2,800 for `--full`.

The 30-day limit on empty events matters: league nights and cancelled events
often publish no matches at all, and without it every run would keep asking
them forever for an answer that is never going to change.

## Two programs, two catalogs

Each program has its own season id and its own catalog directory. The importer
takes the directory, so a VIQRC walk never touches the V5RC tables:

```
python -m integrations.vex_events --season 203 --program viqrc   # Level Up 2026-27
python -m integrations.vex_events --season 204                   # Override 2026-27
```

`--directory` overrides `--program` for a one-off import somewhere else, and
`import_season(..., directory=...)` takes the same argument from Python.

Separate directories rather than one table with a `program` field, because team
numbers are reused across programs: `838J` is an IQ team and could equally be a
V5 one. Keyed by number alone, the two would merge into a single record with
both teams' match histories.

Program and season ids are read from the API rather than hardcoded, because
they were not stable across the VRC to V5RC rename:

```
program 1  V5RC   season 204  Override 2026-2027
program 41 VIQRC  season 203  Level Up 2026-2027
```

**A VIQRC Teamwork match is not an alliance match.** The API reports it as two
one-team "alliances" carrying the same score, because the two teams play
together. Measured on RE-VIQRC-25-3671: 154 matches, every one a pair of
one-team sides, and 150 of them with identical red and blue scores. Nothing in
the importer changes that shape - it is the API's - but everything that reads
it has to know, or every IQ match scores as a tie. `storage/catalog.py` takes a
`scoring` argument for exactly this.

IQ finals arrive as `round=15`, which is not in the V5RC bracket sequence; it
maps to the `iq_final` slug, labelled "Finals".

## A re-import converges

Matches the API no longer lists are pruned, so re-importing an event does not
accumulate stale records. The one exception is a match with footage linked: that
is kept and reported, because a stale key is a nuisance while silently dropping
the only record of which video covers which match is not recoverable.

Two identity problems, both found by comparing rows fetched against rows stored:

- **A match key must include its division.** Without it, a three-division event
  has a "Qual 1" in each and they overwrite each other - 455 of 2,848 matches
  were lost this way before it was caught.
- **The API does not guarantee those four fields are unique.** One event served
  two distinct match ids both named "Qualifier #1" in the same division. Where
  that happens the API's id is appended to the key, so the duplicate is kept and
  visible rather than overwriting its twin.

Counting fetched against stored is the check that catches both, and it is worth
repeating after any import: they are invisible otherwise.

## Imports never clobber local work

Matches are saved with no `video`, which `catalog.save_match` reads as *leave
whatever is linked alone*. So importing over a match whose footage was linked by
hand keeps the footage and refreshes only the alliances and scores.

`source` on each record says where its data came from — `vex-api`, `manual`,
`imported` (registered from files on disk), or `implied` (a team that exists only
because an alliance named it).

## Webcasts

```
python -m integrations.webcasts
```

The API has no webcast field, so links come from the public webcasts page, where
each event name ends in its SKU. Run it again whenever you like; it merges, so a
second run reports everything unchanged.

- **It is behind Cloudflare.** A bare user agent gets a 403 challenge page, which
  is raised as an error rather than read as "no webcasts".
- **Most links are not a video.** Of 126 rows, 31 pointed at a specific video,
  89 at a channel, 5 at a stream page elsewhere and 1 at a playlist. The event
  page says which, because a channel link still means finding the right stream.
- **Some "videos" are shared.** One YouTube `/live/` address is listed for eleven
  events: a partner's permanent stream link, not any one event's footage.
- **Links are never deleted by a refresh.** A changed link keeps the old one in
  `previous`. Partners often post only days before an event, so coverage of
  upcoming events grows as the season goes on.

## YouTube channels and robot videos

```
python -m integrations.youtube --plan 20   # who is next; spends nothing
python -m integrations.youtube             # today's batch
python -m integrations.youtube --team 1028A
python -m integrations.youtube --status
```

Needs a free YouTube Data API key in `data/youtube_key` (or `YOUTUBE_API_KEY`),
never printed. YouTube's search pages are not scraped; that breaks its terms.

This searches **V5RC only**: it reads the V5RC catalog and orders teams by Elo,
which cooperative play has none of. Its results go to `data/team_media.json`,
while the site reads `data/team_media_viqrc.json` when the program is VEX IQ,
so the two cannot be confused - an IQ run would need its own priority order
first.

- **The quota sets the pace.** 10,000 units a day; a search is 100, a channel
  or a page of uploads is 1. One team costs 101–203 units, so a day covers
  roughly 45–90 teams. A batch stops cleanly before a team it cannot finish, and
  the next day continues. Quota resets at midnight Pacific.
- **Order:** teams registered for upcoming Pacific Northwest events (soonest
  first), then the rest of the region, then everyone by Elo. Each team is
  re-checked after 30 days.
- **Nothing attaches on a guess.** "High" confidence needs the exact team number
  as a whole word (so 12393S is not 2393S) plus VEX context; robot videos need
  a reveal/explanation/interview-type title and either the team's own channel
  or this season's date. Everything else is a suggestion awaiting a person.
- **Hand decisions stick.** A removed link is never re-attached, and a
  confirmed one is never replaced by a search.
- Responses are cached for 25 days, so re-grading after a rule change is free.
