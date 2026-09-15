# integrations

Outside data sources. Separate from `storage/` because these reach the network
and can fail, throttle, or return something unexpected, where a store only
touches local disk. Each module converts what it fetches into the plain records
`storage.catalog` already understands, so nothing downstream knows whether a
match was typed in or imported.

| file | holds |
|---|---|
| `vex_events.py` | the Public VEX Events API: events, teams, matches |

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
