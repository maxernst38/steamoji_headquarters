"""Find each team's YouTube channel and robot videos with the YouTube Data API.

Links only - nothing is downloaded. Results go to `storage/team_media.py`.

Usage:
    python -m integrations.youtube                 # today's batch, in priority order
    python -m integrations.youtube --team 1028A    # one team, now
    python -m integrations.youtube --plan 20       # who would be searched next; spends nothing
    python -m integrations.youtube --status

Needs an API key (free): Google Cloud console -> enable "YouTube Data API v3"
-> Credentials -> API key. Put it in data/youtube_key or set YOUTUBE_API_KEY.
It is never logged or printed. YouTube's own search pages are not scraped:
that is against its terms and gets blocked.

The daily quota decides the design. The free tier is 10,000 units a day and a
search costs 100 of them, while reading a channel's details or a page of its
uploads costs 1. So each team gets at most:
- one channel search (100),
- one channel lookup for the candidates (1),
- up to two pages of that channel's uploads (2), and
- one video search (100), only when no team channel was found.
That is 101-203 units, or roughly 45-90 teams a day. Teams are taken in
priority order and a batch stops cleanly when the day's budget runs out, so the
next day's run carries on where it stopped.

Nothing is attached on a guess. Each result is graded:
- "high": attached automatically, and labelled as found automatically.
- "low": kept as a suggestion, shown only when someone asks, until confirmed.
The rules are in `channel_confidence` and `video_confidence`. They exist
because team numbers are short and collide ("929U" inside "1929U"), and last
season's reveal looks exactly like this season's.
"""
import datetime
import hashlib
import json
import os
import re
import time
import zoneinfo

import requests

from storage import catalog, regions, team_media

API_URL = "https://www.googleapis.com/youtube/v3"
KEY_ENV = "YOUTUBE_API_KEY"
KEY_FILE = os.path.join("data", "youtube_key")
CACHE_DIR = os.path.join("data", "cache", "youtube")
QUOTA_FILE = os.path.join(CACHE_DIR, "quota.json")

DAILY_QUOTA = 10_000
# Headroom under the real limit, for anything else using the same key.
DEFAULT_BUDGET = 9_500
COST = {"search": 100, "channels": 1, "playlistItems": 1}
# The most one team can cost; a batch stops before starting a team it cannot finish.
WORST_CASE_TEAM = 100 + 1 + 2 + 100
# Quota resets at midnight Pacific time, so the ledger is kept by that date.
QUOTA_TZ = zoneinfo.ZoneInfo("America/Los_Angeles")

# A cached response is reused for this long; re-checks after it make fresh calls.
CACHE_DAYS = 25
RECHECK_DAYS = 30
UPLOAD_PAGES = 2

# The Override game manual v0.1 is dated 27 April 2026, so nothing about this
# season's robots can predate it. Videos before it are an earlier season.
SEASON_START = "2026-04-27"
HOME_REGION = "United States · Pacific Northwest"

TIMEOUT = 30
RETRIES = 3

_VEX_CONTEXT = re.compile(r"\b(vex|v5|v5rc|vrc|vexu|robotics|robot|robots|override)\b", re.I)
_ROBOT_VIDEO = re.compile(
    r"\b(reveal|revealed|explanation|explained|explaining|walk[\s-]?through|interview|"
    r"showcase|robot tour|bot tour|robot overview|robot breakdown)\b", re.I)
_HANDLE_SUFFIX = r"(?:robotics|robot|robots|vex|vrc|v5|v5rc|bots?|team|official)?"


class YouTubeError(RuntimeError):
    pass


class KeyMissing(YouTubeError):
    pass


class QuotaExceeded(YouTubeError):
    pass


# --- matching rules -------------------------------------------------------

def _split_number(number):
    match = re.fullmatch(r"(\d+)([A-Za-z]*)", str(number or "").strip())
    return (match.group(1), match.group(2)) if match else (str(number or ""), "")


def mentions_team(text, number):
    """True if `text` names this exact team, as a whole word.

    "1028A", "1028 A" and "1028-A" all count; "11028A" and "1028AB" do not.
    """
    digits, letters = _split_number(number)
    pattern = rf"(?<![A-Za-z0-9]){digits}[\s-]?{letters}(?![A-Za-z0-9])"
    return bool(re.search(pattern, text or "", re.I))


def mentions_organization(text, number):
    """True if `text` names the team's number without its letter ("Team 1028").

    Clubs often run one channel for all of 1028A-1028Z. Three digits minimum:
    a bare "10" or "72" in a title is far too common to mean anything.
    """
    digits, letters = _split_number(number)
    if not letters or len(digits) < 3:
        return False
    return bool(re.search(rf"(?<![A-Za-z0-9]){digits}(?![A-Za-z0-9])", text or ""))


_ANY_TEAM = re.compile(r"(?<![A-Za-z0-9])(\d{1,6})[\s-]?([A-Za-z]{1,2})(?![A-Za-z0-9])")


def names_other_team(text, number):
    """True if `text` names a team number other than this one, e.g. "938Z".

    Only digit-plus-letter tokens count, and not ones that are plainly something
    else ("2025", "4K", "60fps").
    """
    own = re.sub(r"[^A-Z0-9]", "", str(number or "").upper())
    for digits, letters in _ANY_TEAM.findall(text or ""):
        token = f"{digits}{letters}".upper()
        if token != own and letters.upper() not in {"K", "P", "FPS", "V", "X"} \
                and not re.fullmatch(r"\d+(ST|ND|RD|TH|S)", token):
            return True
    return False


def handle_names_team(handle, number):
    """True for handles like @vex1028a, @team1028a, @1028arobotics."""
    compact = re.sub(r"[^a-z0-9]", "", str(handle or "").lower())
    team = re.sub(r"[^a-z0-9]", "", str(number or "").lower())
    return bool(team) and bool(re.search(rf"(?<!\d){team}{_HANDLE_SUFFIX}$", compact))


def channel_confidence(channel, number):
    """("high" | "low" | None, kind, reason) for a candidate channel.

    `kind` is "team" (this exact team) or "organization" (the club's shared
    channel). High needs the team named in the title or handle *and* some VEX
    context, so a namesake channel with a number in its name does not qualify.
    """
    title = channel.get("title") or ""
    handle = channel.get("handle") or ""
    context = bool(_VEX_CONTEXT.search(" ".join((title, channel.get("description") or "", handle))))

    if mentions_team(title, number) or handle_names_team(handle, number):
        if context:
            return "high", "team", "channel name matches the team number, and it is about VEX"
        return "low", "team", "channel name matches the team number, but nothing says VEX"
    if mentions_organization(title, number):
        if context:
            return "high", "organization", "channel name matches the club's number, and it is about VEX"
        return "low", "organization", "channel name matches the club's number, but nothing says VEX"
    if mentions_team(channel.get("description"), number) and context:
        return "low", "team", "only the channel description mentions the team"
    return None, None, None


def video_confidence(video, number, via=None):
    """("high" | "low" | None, reason) for a candidate robot video.

    `via` is how it was found: "team" (the team's own channel), "organization"
    (a club channel shared by sibling teams) or None (a general search).
    """
    title = video.get("title") or ""
    if not _ROBOT_VIDEO.search(title):
        return None, None
    current = (video.get("published") or "") >= SEASON_START
    named = mentions_team(title, number)
    # Teams post sister teams' videos too: "938Z Zamboni | Autonomous Showcase"
    # sits on 10B's channel. A title naming only some other team is theirs.
    if not named and names_other_team(title, number):
        return None, None

    if via == "team":
        return "high", "robot video on the team's own channel"
    if via == "organization":
        if named:
            return "high", "robot video naming the team, on the club's channel"
        return "low", "robot video on the club's channel, but it may be a sibling team's"
    if named and current:
        return "high", "robot video naming the team, from this season"
    if named:
        return "low", "robot video naming the team, but from an earlier season"
    if mentions_team(video.get("description"), number) and current:
        return "low", "only the video description mentions the team"
    return None, None


# --- API client -----------------------------------------------------------

def load_key(key=None, key_file=KEY_FILE):
    """The API key, from the argument, the environment, or a local file. Never printed."""
    if key:
        return key.strip()
    from_env = os.environ.get(KEY_ENV)
    if from_env and from_env.strip():
        return from_env.strip()
    try:
        with open(key_file) as handle:
            found = handle.read().strip()
    except OSError:
        found = ""
    if not found:
        raise KeyMissing(f"no YouTube API key. Put one in {key_file} or set {KEY_ENV} "
                         "(Google Cloud console -> YouTube Data API v3 -> Credentials).")
    return found


def _quota_day():
    return datetime.datetime.now(QUOTA_TZ).date().isoformat()


def quota_used(path=QUOTA_FILE):
    try:
        with open(path) as handle:
            ledger = json.load(handle)
    except (OSError, ValueError):
        return 0
    return int(ledger.get(_quota_day(), 0))


def _charge(units, path=QUOTA_FILE, exhausted=False):
    """Add `units` to today's count, or mark today used up entirely."""
    try:
        with open(path) as handle:
            ledger = json.load(handle)
    except (OSError, ValueError):
        ledger = {}
    day = _quota_day()
    used = DAILY_QUOTA if exhausted else min(int(ledger.get(day, 0)) + units, DAILY_QUOTA)
    ledger = {day: used}   # only today matters
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump(ledger, handle)


class Client:
    """A YouTube Data API session with a disk cache and a quota ledger.

    Cached responses cost nothing, so re-grading results after a rule change
    does not spend quota.
    """

    def __init__(self, key=None, cache_dir=CACHE_DIR, quota_file=QUOTA_FILE, session=None):
        self.key = load_key(key)
        self.cache_dir = cache_dir
        self.quota_file = quota_file
        self.session = session or requests.Session()
        self.spent = 0

    def _cache_path(self, endpoint, params):
        stamp = hashlib.sha256(json.dumps([endpoint, params], sort_keys=True).encode()).hexdigest()[:24]
        return os.path.join(self.cache_dir, f"{endpoint}-{stamp}.json")

    def get(self, endpoint, refresh=False, **params):
        path = self._cache_path(endpoint, params)
        if not refresh and os.path.exists(path) \
                and time.time() - os.path.getmtime(path) < CACHE_DAYS * 86400:
            with open(path) as handle:
                return json.load(handle)

        for attempt in range(RETRIES):
            try:
                response = self.session.get(f"{API_URL}/{endpoint}",
                                            params={**params, "key": self.key}, timeout=TIMEOUT)
            except requests.RequestException as error:
                if attempt == RETRIES - 1:
                    raise YouTubeError(f"could not reach YouTube: {type(error).__name__}") from None
                time.sleep(2 * (attempt + 1))
                continue
            if response.status_code >= 500 and attempt < RETRIES - 1:
                time.sleep(2 * (attempt + 1))
                continue
            break

        cost = COST.get(endpoint, 1)
        if response.status_code != 200:
            reasons = {e.get("reason") for e in
                       (response.json().get("error", {}).get("errors") or [])} \
                if response.headers.get("content-type", "").startswith("application/json") else set()
            if reasons & {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}:
                _charge(0, self.quota_file, exhausted=True)   # the day is over either way
                raise QuotaExceeded("YouTube's daily quota is used up; it resets at midnight Pacific")
            if reasons & {"keyInvalid", "badRequest"} and response.status_code == 400:
                raise KeyMissing("YouTube rejected the API key - check data/youtube_key")
            if "accessNotConfigured" in reasons:
                raise KeyMissing("the YouTube Data API v3 is not enabled for this key's project")
            raise YouTubeError(f"YouTube returned HTTP {response.status_code} "
                               f"({', '.join(sorted(reasons)) or 'no reason given'})")

        payload = response.json()
        self.spent += cost
        _charge(cost, self.quota_file)
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(path, "w") as handle:
            json.dump(payload, handle)
        return payload


# --- one team -------------------------------------------------------------

def _thumbnail(snippet):
    thumbs = snippet.get("thumbnails") or {}
    for size in ("medium", "high", "default"):
        if thumbs.get(size, {}).get("url"):
            return thumbs[size]["url"]
    return None


def _video(video_id, snippet, published=None):
    published = (published or snippet.get("publishedAt") or "")[:10]
    return {"id": video_id, "title": snippet.get("title") or "",
            "description": (snippet.get("description") or "")[:500],
            "published": published, "channel_title": snippet.get("channelTitle") or "",
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail": _thumbnail(snippet),
            "season": "current" if published >= SEASON_START else "earlier"}


def _channel_details(client, ids, refresh=False):
    if not ids:
        return []
    payload = client.get("channels", refresh=refresh, part="snippet,contentDetails,statistics",
                         id=",".join(ids), maxResults=50)
    channels = []
    for item in payload.get("items") or []:
        snippet = item.get("snippet") or {}
        handle = snippet.get("customUrl") or ""
        channels.append({
            "id": item["id"], "title": snippet.get("title") or "", "handle": handle,
            "description": (snippet.get("description") or "")[:500],
            "url": f"https://www.youtube.com/{handle}" if handle.startswith("@")
                   else f"https://www.youtube.com/channel/{item['id']}",
            "thumbnail": _thumbnail(snippet),
            "subscribers": int((item.get("statistics") or {}).get("subscriberCount") or 0),
            "uploads": ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads"),
        })
    return channels


def _uploads(client, channel, refresh=False):
    if not channel.get("uploads"):
        return []
    videos, token = [], None
    for _ in range(UPLOAD_PAGES):
        params = {"part": "snippet,contentDetails", "playlistId": channel["uploads"], "maxResults": 50}
        if token:
            params["pageToken"] = token
        payload = client.get("playlistItems", refresh=refresh, **params)
        for item in payload.get("items") or []:
            snippet = item.get("snippet") or {}
            video_id = (snippet.get("resourceId") or {}).get("videoId")
            if video_id:
                videos.append(_video(video_id, snippet,
                                     (item.get("contentDetails") or {}).get("videoPublishedAt")))
        token = payload.get("nextPageToken")
        if not token:
            break
    return videos


def _grade_videos(candidates, number, via, removed):
    high, low = [], []
    for video in candidates:
        if f"video:{video['id']}" in removed:
            continue
        confidence, reason = video_confidence(video, number, via)
        if confidence:
            entry = {**video, "confidence": confidence, "source": "auto", "reason": reason}
            (high if confidence == "high" else low).append(entry)
    return high, low


def search_team(number, client, refresh=False, path=team_media.MEDIA_FILE, log=print):
    """Search for one team and store what is found. Returns the stored record."""
    number = str(number).strip().upper()
    existing = team_media.get(number, path=path) or {}
    removed = set(existing.get("removed") or [])

    # A channel set by hand is trusted as is: no search needed to find it again.
    chosen, kind, suggestions = None, None, []
    kept = existing.get("channel")
    if kept and kept.get("source") == "manual":
        chosen, kind = kept, kept.get("kind", "team")
    else:
        found = client.get("search", refresh=refresh, part="snippet", type="channel",
                           q=f"{number} vex", maxResults=10)
        ids = [item["id"]["channelId"] for item in found.get("items") or []
               if item.get("id", {}).get("channelId")]
        graded = []
        for channel in _channel_details(client, ids, refresh=refresh):
            if f"channel:{channel['id']}" in removed:
                continue
            confidence, what, reason = channel_confidence(channel, number)
            if confidence:
                graded.append({**channel, "confidence": confidence, "kind": what,
                               "source": "auto", "reason": reason})
        # A team's own channel beats a club channel; then the larger audience.
        graded.sort(key=lambda c: (c["confidence"] != "high", c["kind"] != "team", -c["subscribers"]))
        if graded and graded[0]["confidence"] == "high":
            chosen, kind = graded[0], graded[0]["kind"]
            suggestions = graded[1:]
        else:
            suggestions = graded

    videos, video_suggestions = [], []
    if chosen:
        high, low = _grade_videos(_uploads(client, chosen, refresh=refresh), number, kind, removed)
        videos += high
        video_suggestions += low

    # Without the team's own channel, reveals are likeliest on someone else's -
    # an event host, a club, a reviewer - so spend a general search on them.
    if kind != "team":
        found = client.get("search", refresh=refresh, part="snippet", type="video",
                           q=f"{number} vex robot reveal|explanation|interview",
                           publishedAfter=f"{SEASON_START}T00:00:00Z", maxResults=15)
        candidates = [_video(item["id"]["videoId"], item.get("snippet") or {})
                      for item in found.get("items") or [] if item.get("id", {}).get("videoId")]
        high, low = _grade_videos(candidates, number, None, removed)
        seen = {v["id"] for v in videos + video_suggestions}
        videos += [v for v in high if v["id"] not in seen]
        video_suggestions += [v for v in low if v["id"] not in seen]

    record = team_media.record_search(
        number, channel=chosen if chosen and chosen.get("source") == "auto" else None,
        channel_suggestions=suggestions, videos=videos, video_suggestions=video_suggestions,
        path=path)
    log(f"  {number}: channel {'✓ ' + (record['channel'] or {}).get('title', '') if record.get('channel') else '—'}"
        f" · {len(record['videos'])} video(s) · "
        f"{len(record['channel_suggestions']) + len(record['video_suggestions'])} suggestion(s)")
    return record


# --- the daily batch ------------------------------------------------------

def priority(region=HOME_REGION, today=None):
    """Every team, most useful to search first.

    1. teams registered for upcoming events in `region`, soonest event first;
    2. the rest of that region's teams;
    3. everyone else, highest Elo first, unrated last.
    """
    from analysis import ratings

    today = today or datetime.date.today().isoformat()
    events = catalog.list_events()
    teams = catalog.list_teams()
    order, seen = [], set()

    def add(number):
        key = str(number).strip().upper()
        if key in teams and key not in seen:
            seen.add(key)
            order.append(key)

    local = sorted((e for e in events.values()
                    if catalog.event_status(e) == "upcoming"
                    and regions.group_of(e.get("location")) == region),
                   key=lambda e: str(e.get("start") or ""))
    for event in local:
        for number in event.get("teams") or []:
            add(number)
    for number, team in sorted(teams.items()):
        if regions.group_of(team.get("location")) == region:
            add(number)
    elo = ratings.elo()
    for number in sorted(teams, key=lambda n: -(elo.get(n) or {}).get("elo", 0)):
        add(number)
    return order


def due(number, table, now=None):
    checked = (table.get(number) or {}).get("checked_at")
    now = time.time() if now is None else now
    return not checked or now - checked >= RECHECK_DAYS * 86400


def run_batch(budget=DEFAULT_BUDGET, region=HOME_REGION, client=None, path=team_media.MEDIA_FILE,
              log=print):
    """Search teams in priority order until today's budget would be exceeded."""
    client = client or Client()
    allowance = max(0, min(budget, DAILY_QUOTA - quota_used(client.quota_file)))
    table = team_media.load_all(path)
    queue = [n for n in priority(region) if due(n, table)]
    log(f"youtube: {len(queue)} team(s) due, {allowance} units available today")

    searched = 0
    for number in queue:
        if client.spent + WORST_CASE_TEAM > allowance:
            log(f"stopping: today's budget is spent ({client.spent} units used)")
            break
        try:
            search_team(number, client, path=path, log=log)
        except QuotaExceeded as error:
            log(f"stopping: {error}")
            break
        searched += 1
    remaining = len(queue) - searched
    log(f"youtube: searched {searched} team(s), {client.spent} units; {remaining} still due")
    return {"searched": searched, "units": client.spent, "remaining": remaining}


def status(path=team_media.MEDIA_FILE, region=HOME_REGION):
    table = team_media.load_all(path)
    counts = team_media.summary(table)
    queue = priority(region)
    local = [n for n in queue if regions.group_of((catalog.get_team(n) or {}).get("location")) == region]
    counts["due"] = sum(1 for n in queue if due(n, table))
    counts["region_checked"] = sum(1 for n in local if (table.get(n) or {}).get("checked_at"))
    counts["region_total"] = len(local)
    counts["quota_used_today"] = quota_used()
    return counts


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Find team YouTube channels and robot videos")
    parser.add_argument("--team", nargs="+", help="search these teams now, ignoring the queue")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="units to spend at most")
    parser.add_argument("--region", default=HOME_REGION)
    parser.add_argument("--plan", type=int, metavar="N", help="show the next N teams; spends nothing")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="ignore cached responses")
    args = parser.parse_args()

    if args.status:
        for key, value in status(region=args.region).items():
            print(f"{key:>22}: {value}")
        return
    if args.plan:
        table = team_media.load_all()
        for number in [n for n in priority(args.region) if due(n, table)][:args.plan]:
            team = catalog.get_team(number) or {}
            print(f"{number:>8}  {regions.group_of(team.get('location'))}")
        return
    try:
        if args.team:
            client = Client()
            for number in args.team:
                search_team(number, client, refresh=args.refresh)
            print(f"{client.spent} units used")
        else:
            run_batch(budget=args.budget, region=args.region)
    except KeyMissing as error:
        raise SystemExit(f"error: {error}")


if __name__ == "__main__":
    main()
