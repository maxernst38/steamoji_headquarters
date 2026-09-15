"""How much an event matters: a composite of stature, field size and field strength.

This is a judgement dressed as a number, and it is written to be argued with.
The weights below are a choice, not a measurement, so the components are kept
separate and surfaced alongside the score rather than collapsed into one opaque
figure.

Three inputs:

- **Level** - what the event is. A Signature or World event draws differently
  from a league night, and this is the only input that is known for certain
  before anyone registers.
- **Field size** - how many teams are registered. Scaled logarithmically: the
  gap between 10 teams and 30 matters far more than between 90 and 110, and
  rosters here run from a handful to 118.
- **Field strength** - the Elo of registered teams who have one.

The third input is the problem, and it is handled explicitly rather than hidden.
Measured on this catalog three weeks into the season, the median upcoming event
has **0%** of its roster rated, and 199 of 301 have no rated team at all - most
registrants simply have not competed yet.

Scoring an unrated field as *weak* would be wrong: the column would then measure
how much data we happen to have, and every upcoming event would rank below every
past one for a reason that has nothing to do with importance. So when strength
is unknown the weights are **renormalised over the components that are known**,
and the coverage is reported so a score resting on no strength data is visibly
different from one resting on a fully rated field.
"""
from analysis import ratings

# What the event is. Ordered by who it draws, not by prestige as such.
LEVEL_SCORES = {
    "World": 1.00,
    "National": 0.85,
    "Signature": 0.75,
    "Regional": 0.60,
    "State": 0.50,
    "Other": 0.20,
}
DEFAULT_LEVEL = 0.20

# Roster sizes here run 1..118 with a median of 13, so the curve is set to span
# that rather than some notional maximum.
SIZE_REFERENCE = 120.0

# Elo is centred on 1500 by construction; this maps a realistic spread onto 0..1.
ELO_FLOOR, ELO_CEILING = 1400.0, 1800.0

WEIGHTS = {"level": 0.45, "size": 0.25, "strength": 0.30}

# Below this, an average is a single team's rating rather than a field's.
MIN_RATED_TEAMS = 3


def _size_score(count):
    import math
    if not count:
        return 0.0
    return min(math.log1p(count) / math.log1p(SIZE_REFERENCE), 1.0)


def _strength(roster, elo):
    """Mean Elo of the rated part of a roster, plus how much of it that was."""
    rated = [elo[t]["elo"] for t in roster if t in elo]
    coverage = len(rated) / len(roster) if roster else 0.0
    if len(rated) < MIN_RATED_TEAMS:
        return None, coverage, len(rated)
    mean = sum(rated) / len(rated)
    scaled = (mean - ELO_FLOOR) / (ELO_CEILING - ELO_FLOOR)
    return max(0.0, min(scaled, 1.0)), coverage, len(rated)


def score_event(event, elo):
    """Importance 0-100 for one event, with its parts exposed."""
    roster = event.get("teams") or []
    size = len(roster) or sum((event.get("grades") or {}).values())

    level_name = event.get("level") or "Other"
    parts = {
        "level": LEVEL_SCORES.get(level_name, DEFAULT_LEVEL),
        "size": _size_score(size),
    }
    strength, coverage, rated = _strength(roster, elo)
    if strength is not None:
        parts["strength"] = strength

    # Renormalised over what is known, so a missing strength term neither
    # penalises the event nor silently counts as average.
    total_weight = sum(WEIGHTS[name] for name in parts)
    score = sum(parts[name] * WEIGHTS[name] for name in parts) / total_weight

    return {
        "score": round(score * 100, 1),
        "level": level_name,
        "level_score": round(parts["level"] * 100),
        "size": size,
        "size_score": round(parts["size"] * 100),
        "strength_score": round(strength * 100) if strength is not None else None,
        "rated_teams": rated,
        "coverage": round(coverage, 3),
        "components": sorted(parts),
    }


def score_all(events, elo=None):
    """{event key: importance} over a mapping of events."""
    elo = elo if elo is not None else ratings.elo()
    return {key: score_event(event, elo) for key, event in events.items()}
