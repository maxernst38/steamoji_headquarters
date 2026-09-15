"""The elimination bracket, derived from matches rather than stored.

A bracket is not data the API serves - it is the shape implied by the matches
already in the catalog. Deriving it keeps one copy of the truth: a stored
bracket could disagree with the matches it came from, and nothing would say
which was right.

The structure comes from three fields already on every match. `round` gives the
stage, `instance` gives the slot within that stage, and `number` distinguishes
games within a best-of-N series. Measured on a real event that is 8 Round of 16
slots, 4 quarterfinals, 2 semifinals and 1 final - 15 slots over 16 matches,
because the final went two games.
"""
from storage import catalog

# Bracket order, widest stage first. Plenty of events run none of these.
ELIMINATION_ROUNDS = (
    ("r16", "Round of 16"),
    ("qf", "Quarterfinals"),
    ("sf", "Semifinals"),
    ("final", "Finals"),
)


def _winner_of(match):
    """Which side won one game, or None if it is unscored or tied."""
    alliances = match.get("alliances") or {}
    red, blue = alliances.get("red") or {}, alliances.get("blue") or {}
    if red.get("score") is None or blue.get("score") is None:
        return None
    if red["score"] > blue["score"]:
        return "red"
    if blue["score"] > red["score"]:
        return "blue"
    return None


def _slot(matches):
    """One bracket position: its two alliances and who took the series.

    The series winner is counted by *team set* rather than by colour. Nothing
    guarantees an alliance keeps the same colour across a best-of-three, and
    counting colours would hand the series to the wrong alliance if it swapped.
    """
    games = sorted(matches, key=lambda m: m.get("number") or 0)
    first = (games[0].get("alliances") or {})
    red_teams = list((first.get("red") or {}).get("teams") or [])
    blue_teams = list((first.get("blue") or {}).get("teams") or [])

    wins = {}
    for game in games:
        side = _winner_of(game)
        if not side:
            continue
        teams = frozenset((game["alliances"][side].get("teams") or []))
        wins[teams] = wins.get(teams, 0) + 1

    red_key, blue_key = frozenset(red_teams), frozenset(blue_teams)
    red_wins, blue_wins = wins.get(red_key, 0), wins.get(blue_key, 0)
    winner = None
    if red_wins > blue_wins:
        winner = "red"
    elif blue_wins > red_wins:
        winner = "blue"

    return {
        "instance": games[0].get("instance"),
        "matches": games,
        "red": red_teams,
        "blue": blue_teams,
        "red_wins": red_wins,
        "blue_wins": blue_wins,
        "winner": winner,
        "winning_teams": red_teams if winner == "red" else (blue_teams if winner == "blue" else []),
        "losing_teams": blue_teams if winner == "red" else (red_teams if winner == "blue" else []),
        "scored": any(_winner_of(g) for g in games),
    }


def build(matches):
    """The bracket for one event's matches.

    `has_eliminations` is False for the many events that only run qualification
    matches - a league night, a scrimmage, a cancelled event. The caller is
    expected to say so rather than draw an empty frame.
    """
    by_round = {}
    for match in matches:
        slug = match.get("round")
        if slug in dict(ELIMINATION_ROUNDS):
            by_round.setdefault(slug, {}).setdefault(match.get("instance") or 1, []).append(match)

    rounds = []
    for slug, label in ELIMINATION_ROUNDS:
        slots = by_round.get(slug)
        if not slots:
            continue
        rounds.append({
            "slug": slug,
            "label": label,
            "slots": [_slot(slots[i]) for i in sorted(slots)],
        })

    champion, finalist = [], []
    if rounds and rounds[-1]["slug"] == "final":
        final = rounds[-1]["slots"][0]
        champion, finalist = final["winning_teams"], final["losing_teams"]

    return {
        "rounds": rounds,
        "has_eliminations": bool(rounds),
        "champion": champion,
        "finalist": finalist,
        "slots": sum(len(r["slots"]) for r in rounds),
        "matches": sum(len(s["matches"]) for r in rounds for s in r["slots"]),
    }


def for_event(key, matches=None):
    matches = matches or catalog.list_matches().values()
    folded = str(key).upper()
    return build([m for m in matches if str(m.get("event") or "").upper() == folded])
