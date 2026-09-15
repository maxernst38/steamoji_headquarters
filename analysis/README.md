# analysis

Numbers derived from match results. Nothing here fetches or stores — it reads
the catalog and returns values, so the arithmetic is testable on its own and a
rating can never quietly become the thing that persists instead of the match it
came from.

| file | holds |
|---|---|
| `ratings.py` | OPR, DPR, CCWM and Elo |

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
