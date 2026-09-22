"""The competition programs the site covers, and which one a request is looking at.

One site, not two. V5RC and VIQRC share every page, every route and every
template; what differs is the data shown and the colours it is shown in. A
program is therefore a filter plus a theme.

The choice lives in a cookie rather than in the URL so that every link already
written - and every link a user has bookmarked - keeps working unchanged. A
`?program=` on any URL overrides the cookie for that one request, which is what
makes a page shareable in a particular program when that matters.

Records carry the API's own code ("V5RC"), so `program_of` maps those onto the
short keys used here, and falls back to an event's program for matches, which
the API does not tag.

Each program also keeps its own catalog directory, which is what makes the two
safe to hold at once.
"""
import os

# `scoring` is the difference that matters downstream. A V5RC match has two
# alliances and a winner; a VIQRC Teamwork match has two teams playing together
# for one score that both of them carry, so wins, win rate, Elo and OPR are not
# quantities that exist for it.
ALLIANCE = "alliance"
COOPERATIVE = "cooperative"

# Teams are keyed by number alone and the two programs reuse the same
# numbering - 838J is an IQ team and could equally be a V5 one - so one shared
# table would merge two teams' histories into a single record. Separate
# directories make that impossible rather than unlikely.
PROGRAMS = {
    "v5rc": {
        "code": "v5rc",
        "api_code": "V5RC",
        "short": "V5",
        "label": "VEX V5",
        "name": "VEX V5 Robotics Competition",
        "scoring": ALLIANCE,
        "catalog_dir": os.path.join("data", "catalog"),
        "media_file": os.path.join("data", "team_media.json"),
        "season_id": 204,
    },
    "viqrc": {
        "code": "viqrc",
        "api_code": "VIQRC",
        "short": "IQ",
        "label": "VEX IQ",
        "name": "VEX IQ Robotics Competition",
        "scoring": COOPERATIVE,
        "catalog_dir": os.path.join("data", "catalog_viqrc"),
        "media_file": os.path.join("data", "team_media_viqrc.json"),
        "season_id": 203,
    },
}

ORDER = ("v5rc", "viqrc")
DEFAULT = "v5rc"
COOKIE = "program"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365

# VRC was renamed V5RC and VIQC renamed VIQRC in the 2024 season, so records
# imported before then, or typed in by hand, still use the old spellings.
ALIASES = {"v5rc": "v5rc", "vrc": "v5rc", "viqrc": "viqrc", "viqc": "viqrc", "iq": "viqrc",
           "v5": "v5rc"}


def normalise(value):
    """A program key for anything a record, a cookie or a URL might carry."""
    return ALIASES.get(str(value or "").strip().lower())


def get(code):
    return PROGRAMS.get(normalise(code) or DEFAULT, PROGRAMS[DEFAULT])


def ordered():
    return [PROGRAMS[code] for code in ORDER]


def current(request):
    """The program for this request: an explicit ?program= wins over the cookie."""
    return (normalise(request.args.get("program"))
            or normalise(request.cookies.get(COOKIE))
            or DEFAULT)


def program_of(record, events=None):
    """Which program a team, event or match belongs to.

    Matches are not tagged - the API returns no program on a match - so they
    take their event's. A record with neither counts as the default program
    rather than vanishing from every view, which is what a hand-entered team
    with no program set would otherwise do.
    """
    record = record or {}
    code = normalise(record.get("program"))
    if code:
        return code
    key = str(record.get("event") or "").upper()
    if key and events:
        return normalise((events.get(key) or {}).get("program")) or DEFAULT
    return DEFAULT


def catalog_dir(code):
    """Where this program's teams, events and matches are stored."""
    return get(code)["catalog_dir"]


def media_file(code):
    """Where this program's YouTube links live.

    Split for the same reason as the catalog: the store is keyed by team
    number, so one file would hand an IQ team the V5 team's channel.
    """
    return get(code)["media_file"]


def scoring(code):
    """ALLIANCE or COOPERATIVE - what a match result means in this program."""
    return get(code)["scoring"]
