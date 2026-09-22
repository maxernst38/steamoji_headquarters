# storage

What persists between runs, in two layers.

The **artefact stores** are keyed by `(video, start frame, end frame)`, never by
video alone, because the camera can change between matches. The **catalog** holds
the entities the analysis is about — teams, events, matches — and points at those
artefacts by the same triple.

Files land under `data/` — `data/calibrations/`, `data/seeds/`, `data/results/`,
`data/catalog/`.

| file | holds |
|---|---|
| `calibration_store.py` | the field quad and homography for one match |
| `seed_store.py` | hand-drawn robot boxes for one match |
| `results_store.py` | a manifest per finished run, so a match is never processed twice |
| `catalog.py` | teams, events and matches — who was playing, and where the footage is. One directory per program (`data/catalog`, `data/catalog_viqrc`), and a `scoring` argument for cooperative play |
| `migrate_catalog.py` | one-shot: register segments already on disk as match records |
| `regions.py` | groups a team's location into something worth filtering by |
| `event_details.py` | awards, qualification rankings and skills, one file per event |
| `webcasts.py` | one webcast link per event SKU, merged across refreshes (`data/webcasts.json`) |
| `team_media.py` | each team's YouTube channel and robot videos, with confidence and removals (`data/team_media.json`) |

## Why the catalog is separate

A frame range is enough to process a match and useless for scouting: it knows
nothing about who was driving. The catalog adds that layer without re-keying
anything, so every run already on disk keeps resolving — a match record *points
at* its artefacts rather than owning them.

Its shapes follow RobotEvents' API as closely as hand entry allows (team
`number`, event `sku`, match `round`/`instance`/`number`), so populating it from
the API later is a change of source, not of schema. Hand-entered events get a
`LOCAL-` key so they cannot collide with a real SKU.

Unlike the artefact stores, the catalog is one file per table rather than one per
record: the teams table joins across every match, and a match record is a few
hundred bytes, so a whole season is cheap to read at once.

Scores are optional and `None` means *unknown*, not zero. Every aggregate leaves
unscored matches out of the win/loss record rather than counting them as losses.

## Why event details are one file per event

Teams and matches are joined across every event — the teams table scans all of
them — so they live in single files read whole. Awards, rankings and skills are
only ever read for one event at a time, so a combined table would mean parsing
megabytes to render one page. One file per event keeps that to about 7KB.

The elimination bracket is deliberately **not** stored: it follows from the
matches already in the catalog (`analysis/bracket.py`), and a stored copy could
disagree with the matches it came from with nothing to say which was right.
39 of 686 events have one at all, so "this event ran no eliminations" is the
common case rather than an edge case.

## Regions

`regions.py` splits the United States into seven regions and groups everywhere
else whole, by country. The asymmetry mirrors who a team actually competes
against: of 3,150 imported teams, 2,003 are US across 42 states, while most
other countries have a few dozen - splitting those further would make buckets
of one or two.

States are listed explicitly rather than derived, and anything unrecognised
lands in "United States · Other" so a new or misspelled state becomes a visible
bucket instead of vanishing from every filter.

The teams and events pages default to the Pacific Northwest, since scouting is
mostly local. Two guards on that: the default is dropped if it would hide
everything, and it never narrows an explicit search — someone typing a team
number wants that team, not "that team if it happens to be local". Searching
under an unasked-for default silently dropped an out-of-region team, which is
the kind of omission nothing on the page would explain. `all` is the opt-out
value rather than a blank, because a blank `<select>` submits as `""` and would
be indistinguishable from "no region given".

## High School vs Middle School

Teams carry the API's own `grade`, so that split is exact. Events do not — the
API has no grade field for an event at all — so `event_grade()` derives it from
the grades of the teams registered there, stored as a tally on the event.

Deriving rather than parsing the name is a measured decision. Checked against
the events whose rosters we can see, the name agreed 23 times, **disagreed 6**,
and said nothing 13 — wrong or silent about 45% of the time. Two near-identical
events make the point: "Maker Faire OC - MS/HS - Day 1" has a roster of 20 HS
and 14 MS (Blended), while Day 2 has 33 HS and 1 MS (High School). The names
cannot tell them apart; the rosters can.

An event is called for a grade only at 90% or more of one grade, so a couple of
cross-registered teams do not reclassify it.

An event with no tally has no registered teams yet — 321 of the 335 blanks are
upcoming events whose rosters do not exist. Re-importing fills them in as teams
register; it does not need new requests for events already cached.

## Joining footage to a real match

Segments found on disk become placeholder matches with video but no teams; the
same matches arrive from the API with teams but no video. `transfer_video` joins
the two halves, and `delete_event(key, with_matches=True)` clears the placeholder
— refusing to run while any of its matches still has footage, since that would
orphan a calibration nothing points at any more.

Nothing on disk moves during a transfer. The artefact stores key on
(video, start frame, end frame), so the moment the target match carries that
triple, every calibration, seed set and finished run resolves against it
unchanged — the fifteen minutes a run costs is not spent again.

Keys are matched case-insensitively. An uppercase SKU, a lowercase slug and
whatever a URL carried all have to resolve to the same record; a lookup missing
by case alone reads as "no such event" rather than as a normalisation bug.

## When is a saved result still valid?

Reusing a stale result is worse than recomputing it: the paths look fine while
describing something else. The results key therefore covers everything that changes
the output — the video, the frame range, `stride`, `robots`, `seed_search`,
`background_span`, the hand-drawn seeds, and **the calibration's contents**.

That last one is the subtle case. Redrawing the field moves every projected
position, so a result built on an older homography is wrong in a way nothing
downstream would reveal.

`chunk` is deliberately excluded: it trades memory against speed without changing
results, so keying on it would cost fifteen minutes and buy nothing.

A manifest also misses if any file it names has gone — results are only as good as
the files still on disk.

## Seeds are also training labels

Every box drawn in the UI is a labelled robot in a real frame. `seed_store` is the
collection point for a detector training set as much as it is a cache, which is why
it keeps the frame index alongside the boxes.
