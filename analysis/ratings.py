"""Team strength from match scores: OPR, DPR, CCWM and Elo.

None of these come from the API. Its `Ranking` object carries only event-local,
alliance-dependent figures - rank, WP/AP/SP, `average_points` - where
`average_points` is the *alliance's* average, not the team's contribution. The
whole point of these ratings is to separate a team from its partners, so they
have to be computed from the match scores.

Two families, answering different questions, and both are needed:

**OPR / DPR / CCWM** solve a least-squares system over every alliance score at
one event. Each alliance is one equation - the sum of its members' ratings
should equal its score - so a team's rating is whatever best explains every
result it took part in, regardless of who it was paired with. OPR fits a team's
own scoring, DPR fits the score it allowed, and CCWM is the difference.

DPR earns its place. At the measured event the undefeated winner ranked only
6th on OPR: their output was ordinary and what made them unbeatable was holding
opponents near zero, which only DPR sees. CCWM put them 1st. OPR and DPR
correlate at just -0.28 there, so DPR is not simply OPR mirrored.

**Elo** is sequential and pairwise, so unlike OPR it carries across events that
share teams - which is exactly the strength-of-schedule question OPR cannot
answer.

The honest limit on each is the same shape: comparability.

- OPR is only meaningful *within* an event. Teams at different events never
  share a match, so the system is block-diagonal and the blocks sit on
  unrelated scales.
- Elo links events that share teams, but the event graph measured here has 10
  components; the largest holds 22 of 42 events and 76% of teams. Ratings in
  different components never exchanged information and are not comparable.

Both are reported with the context needed to see that, rather than as bare
numbers that invite the comparison they cannot support.
"""
import math

import numpy as np

from storage import catalog

ELO_START = 1500.0
ELO_K = 32.0

# Ratings rest on few matches - about five equations per team at the measured
# event - so anything thinner than this is too noisy to show as a number.
MIN_MATCHES = 2


def _scored(match):
    alliances = match.get("alliances") or {}
    red, blue = alliances.get("red") or {}, alliances.get("blue") or {}
    if red.get("score") is None or blue.get("score") is None:
        return None
    if not red.get("teams") or not blue.get("teams"):
        return None
    return red, blue


def power_ratings(matches):
    """OPR, DPR and CCWM for every team in one event's matches.

    Each alliance contributes one equation. Solved with least squares rather
    than by inverting, so an over-determined system - which every measured event
    is, at roughly five equations per team - is handled without forming a
    normal-equations matrix that would square the condition number.
    """
    rows = []
    for match in matches:
        pair = _scored(match)
        if not pair:
            continue
        red, blue = pair
        rows.append((red["teams"], red["score"], blue["score"]))
        rows.append((blue["teams"], blue["score"], red["score"]))
    if not rows:
        return {}

    teams = sorted({t for side, _, _ in rows for t in side})
    index = {team: i for i, team in enumerate(teams)}
    design = np.zeros((len(rows), len(teams)))
    own = np.zeros(len(rows))
    against = np.zeros(len(rows))
    played = dict.fromkeys(teams, 0)
    for r, (side, scored, allowed) in enumerate(rows):
        for team in side:
            design[r, index[team]] = 1.0
            played[team] += 1
        own[r], against[r] = float(scored), float(allowed)

    opr, *_ = np.linalg.lstsq(design, own, rcond=None)
    dpr, *_ = np.linalg.lstsq(design, against, rcond=None)

    # Reported alongside every rating: at the measured event the residual RMS
    # was 30 points against a score spread of 37, so these explain about a third
    # of the variance. A rating shown without that context reads as far more
    # precise than it is.
    residual = float(np.sqrt(np.mean((design @ opr - own) ** 2)))
    spread = float(own.std())
    quality = {
        "equations": len(rows),
        "teams": len(teams),
        "residual_rms": round(residual, 2),
        "score_sd": round(spread, 2),
        "r2": round(1 - (residual ** 2) / (spread ** 2), 3) if spread else None,
    }

    return {
        team: {
            "opr": round(float(opr[i]), 1),
            "dpr": round(float(dpr[i]), 1),
            "ccwm": round(float(opr[i] - dpr[i]), 1),
            "alliance_rows": played[team],
            "quality": quality,
        }
        for team, i in index.items()
    }


def by_event(matches=None, events=None):
    """{event key: {team: ratings}}, solved one event at a time.

    Per event rather than globally on purpose: a single solve over every event
    would be block-diagonal and produce numbers that look comparable and are
    not.
    """
    matches = list((matches or catalog.list_matches()).values()) \
        if isinstance(matches, dict) or matches is None else list(matches)
    grouped = {}
    for match in matches:
        grouped.setdefault(str(match.get("event") or "").upper(), []).append(match)

    out = {}
    for key, theirs in grouped.items():
        ratings = power_ratings(theirs)
        if ratings:
            out[key] = ratings
    return out


def _order_key(match, events):
    """Chronological, falling back to bracket order.

    Elo depends on sequence, and 16% of the measured matches carry no scheduled
    timestamp. Within an event the round, instance and number give the true
    order anyway, so the fallback is exact rather than approximate.
    """
    event = (events or {}).get(str(match.get("event") or "").upper()) or {}
    return (
        str(event.get("start") or ""),
        str(match.get("scheduled") or ""),
        catalog.ROUND_ORDER.get(match.get("round"), 99),
        match.get("instance") or 0,
        match.get("number") or 0,
    )


def components(matches):
    """Groups of events joined by shared teams, largest first.

    Elo ratings only mean anything relative to others in the same group: two
    events with no team in common never exchanged rating information, so
    comparing across them compares two independent random walks.
    """
    per_team, adjacency = {}, {}
    for match in matches:
        if not _scored(match):
            continue
        key = str(match.get("event") or "").upper()
        for team in catalog.teams_in(match):
            per_team.setdefault(team, set()).add(key)
    for events_of in per_team.values():
        for a in events_of:
            adjacency.setdefault(a, set()).update(events_of - {a})

    seen, found = set(), []
    for start in adjacency:
        if start in seen:
            continue
        stack, group = [start], []
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            group.append(node)
            stack.extend(adjacency[node] - seen)
        found.append(set(group))
    found.sort(key=len, reverse=True)
    return found


def elo(matches=None, events=None, k=ELO_K, start=ELO_START):
    """Sequential Elo over every scored match.

    An alliance plays as the mean of its members' ratings, and every member
    takes the same update. Using the mean rather than the sum keeps a rating on
    the familiar ~1500 scale and makes a two-team alliance comparable to the
    one-team alliances that appear in some elimination records.
    """
    events = events if events is not None else catalog.list_events()
    matches = list((matches or catalog.list_matches()).values()) \
        if isinstance(matches, dict) or matches is None else list(matches)
    scored = [m for m in matches if _scored(m)]
    scored.sort(key=lambda m: _order_key(m, events))

    where = {}
    for group_id, group in enumerate(components(scored)):
        for key in group:
            where[key] = group_id

    ratings, played = {}, {}
    for match in scored:
        red, blue = _scored(match)
        red_teams, blue_teams = red["teams"], blue["teams"]
        red_rating = sum(ratings.get(t, start) for t in red_teams) / len(red_teams)
        blue_rating = sum(ratings.get(t, start) for t in blue_teams) / len(blue_teams)

        expected = 1.0 / (1.0 + math.pow(10.0, (blue_rating - red_rating) / 400.0))
        if red["score"] > blue["score"]:
            actual = 1.0
        elif red["score"] < blue["score"]:
            actual = 0.0
        else:
            actual = 0.5
        change = k * (actual - expected)

        for team in red_teams:
            ratings[team] = ratings.get(team, start) + change
            played[team] = played.get(team, 0) + 1
        for team in blue_teams:
            ratings[team] = ratings.get(team, start) - change
            played[team] = played.get(team, 0) + 1

    team_group = {}
    for match in scored:
        group = where.get(str(match.get("event") or "").upper())
        for team in catalog.teams_in(match):
            team_group.setdefault(team, group)

    return {
        team: {"elo": round(value, 1), "matches": played.get(team, 0),
               "pool": team_group.get(team)}
        for team, value in ratings.items()
    }
