# analysis

Numbers derived from match results. Nothing here fetches or stores — it reads
the catalog and returns values, so the arithmetic is testable on its own and a
rating can never quietly become the thing that persists instead of the match it
came from.

| file | holds |
|---|---|
| `ratings.py` | OPR, DPR, CCWM and Elo |
| `alignment.py` | where each match sits inside its event's stream |

`alignment.py` is the one thing here that writes, which needs saying because the
rule above is worth keeping. It does not persist a *derived number*: it works out
which video a match was streamed on and how far into it the match starts, and
writes that to `match["video"]` — where footage has always lived, and where the
match page already writes the same two fields when someone types them by hand.
The invariant it must not break is the one about ratings, and it does not: no
rating is stored, and a match still owns its footage rather than a table of
offsets owning the match.

## Why these are computed rather than fetched

The API does not provide them. Its `Ranking` object carries only `rank`,
W–L–T, `wp`/`ap`/`sp`, `high_score` and `average_points` — all event-local, and
`average_points` is the *alliance's* average, not the team's contribution. No
`opr`, `dpr`, `ccwm`, `elo` or `rating` field exists anywhere in the spec.

## OPR / DPR / CCWM

Each alliance in a match is one equation: the sum of its members' ratings should
equal its score. Solving that system by least squares gives each team the rating
that best explains every result it took part in, **regardless of who it was
paired with** — which is exactly what win rate and average score cannot do.

- **OPR** fits a team's contribution to its own alliance's score
- **DPR** fits the score it allowed the opposition
- **CCWM** is OPR − DPR

**DPR earns its place.** At `RE-V5RC-26-4244` the undefeated winner (10102A,
14–0) ranked only **6th on OPR** — their scoring was ordinary, and what made
them unbeatable was holding opponents near zero. Only DPR sees that, and CCWM
put them 1st. OPR and DPR correlate at just −0.28 there, so DPR is not OPR
mirrored.

## Read them as rankings, not quantities

Reported with their own fit, because the precision is limited and a bare number
invites more confidence than it earns:

- **R² ≈ 0.34** at the measured event — residual RMS 30 points against a score
  spread of 37, so this explains about a third of score variance.
- Each team's figure rests on roughly **five alliances**. Every one of the 42
  scored events is over-determined (~4.8 equations per team), but that is
  barely.
- **DPR can come out negative.** A team cannot cause negative opponent points;
  that is the fit overshooting.
- A team playing pure defense suppresses rather than scores, so it reads as low
  OPR *and* low DPR. CCWM is the column to lead with.

## Comparability is the real limit on both

**OPR is only meaningful within an event.** Teams at different events never
shared a match, so the system is block-diagonal and the blocks sit on unrelated
scales. `by_event` therefore solves one event at a time rather than doing a
single global solve that would produce numbers that look comparable and are not.
The teams table shows an average across a team's events as a guide; the
per-event figures on a team's page are the real ones.

**Elo does carry across events — but only within a connected pool.** It is
sequential and pairwise, so it answers the strength-of-schedule question OPR
cannot. Measured here, the event graph has **10 components**; the largest holds
22 of 42 events and **76% of rated teams**. Ratings in different components
never exchanged information, so `elo()` returns each team's pool alongside its
rating and the UI says so when a team is outside the main one.

Elo needs an order, and 16% of matches carry no scheduled timestamp. The
fallback is event start date plus round/instance/number, which within an event
is the exact order rather than an approximation.

An alliance plays as the **mean** of its members' ratings, not the sum — that
keeps ratings on the familiar ~1500 scale and makes a two-team alliance
comparable to the one-team records that appear in some elimination brackets.

## Match offsets into a stream

```
python -m analysis.alignment --dry-run          # what the data supports; writes nothing
python -m analysis.alignment --event RE-V5RC-26-4244
```

`storage/event_streams.py` says which videos an event was streamed on, each with
the UTC time it began. `match["scheduled"]` says when a match was meant to start.
Together those give a match an offset into a specific video, which is what the
team page's Match footage panel has been waiting for.

**The offsets are approximate and stored saying so.** They come from the
schedule, and events run behind it, so `video["approximate"]` is set on every one
and both the team and matches tables render a `~` before the clock. What this
buys is a link that lands near the match instead of at the start of an eight-hour
video — not a frame-accurate cut.

Two things it does not need to be told:

- **Which stream a match is on.** A stream is a real interval, so a match's
  absolute time falls inside one or none. A two-day, three-division event
  resolves with nothing said about days or divisions. Two fields streamed at once
  stays ambiguous and is reported rather than guessed.
- **The event's timezone.** Where `scheduled` carries a UTC offset, nothing is
  inferred. Where it does not, the location gives an IANA zone and `zoneinfo`
  resolves the offset for the event's own date — stored by zone name, not as a
  number, because the season straddles the daylight-saving change.

**The offset search is the check, not the mechanism**, and that ordering was
measured. On a two-day event whose matches spanned three hours of an eight-hour
stream, every candidate offset across a five-hour range scored identically and
the search picked the wrong edge of that plateau by nearly two hours. A stream
long enough to hold the matches is not evidence of where in it they sit. So the
search's real job is refusal: an offset landing most of an event's matches
outside every stream is wrong whether a table or a search produced it, and then
nothing is written.

`video["how"]` records which route was taken — `carried`, `zone`, `corrected` or
`searched` — because the accuracies differ. `carried` and `zone` are the
schedule's own drift; `searched` adds the plateau's width on top, which measured
at over an hour for an event with no location to key a zone from.

**It never touches footage linked by hand.** A `video` this writes carries
`"source": "auto"`, and anything without that marker is left alone — so a typed-in
start time always wins, and footage predating this module is treated as
hand-linked, which it was. A local segment's `file`, `start_frame` and
`end_frame` are merged through untouched, because replacing that dict wholesale
would orphan the calibration and seed files keyed on the triple.

`save_match` rebuilds a record from its arguments rather than merging
`alliances`, so every field is carried through the write. Passing only the video
would blank the teams and scores of every match it touched, with nothing
downstream to show it had happened.

**Not yet measured against real data.** The placement, zone resolution, refusal
and merge rules are covered by fixtures, but no run against a real catalog has
happened. `--dry-run` reports what the data supports and writes nothing, and the
first question it answers is whether this API's `scheduled` carries a UTC offset
at all — which decides how much of the timezone machinery is ever used.
