"""Resolve each event's webcast link to the actual stream videos.

The webcast page mostly lists channels: of 126 rows, 89 point at a channel, 31
at a specific video, 5 at a stream page elsewhere and 1 at a playlist. A channel
link means the footage exists but nobody has said which video it is, so most of
a season's streams are one step out of reach. This walks the linked channel's
uploads and keeps what was published while the event ran. Results go to
`storage/event_streams.py`.

Usage:
    python -m integrations.event_streams --plan     # what would be walked; spends nothing
    python -m integrations.event_streams            # resolve, within today's quota
    python -m integrations.event_streams --sku RE-V5RC-26-4244
    python -m integrations.event_streams --status

Links only - nothing is downloaded.

Shares the API key, disk cache and daily quota ledger with
`integrations.youtube` through `youtube.Client`, because both spend the same
10,000 units a day and separate ledgers would let them overspend it together.

**The walk is per channel, not per event.** Partners stream every event they run
from one channel - one `/live/` address is listed for eleven events - so walking
per event would fetch the same upload list a dozen times. Grouping by channel
also puts the date window to work: eleven events on one channel are separated by
when they ran, which is exactly what the uploads carry.

Cost is dominated by `playlistItems` at 1 unit per 50 videos, so a channel walk
is cheap. `search`, at 100 units, is deliberately never used here - a channel's
uploads are reachable for 1 unit a page, and searching would buy nothing but a
worse date filter at a hundred times the price.

Four things the grading has to survive:

- **A short video published during an event is not the stream.** Awards clips,
  promos and highlight reels land on the same day as the footage. Under
  MIN_PLAUSIBLE it is a suggestion at best.
- **Two events can share a channel and a day.** A partner running two SKUs on one
  Saturday gets the same window for both, and nothing in the uploads separates
  them except the title. Where the title cannot, both drop to suggestions rather
  than both claiming every video.
- **A direct video link can still be wrong.** That eleven-event `/live/` address
  looks like the strongest possible evidence and is right for at most one of
  them, so a shared link is resolved through its channel like any other.
- **An upload date is UTC and an event date is local.** An evening league night
  on the US west coast is published the next day in UTC, and a morning in New
  Zealand the previous one, so the window is widened a day at both ends. The
  cost of that generosity is more competing candidates, which the title rules
  and the duration floor then have to settle.

Coverage is the point, not precision: a suggestion someone can confirm in one
click beats a gap nothing explains. Both are recorded.
"""
import argparse
import datetime as dt
import math
import re
import urllib.parse

from integrations import vex_events, youtube
from storage import catalog, event_streams, webcasts

# A channel walk stops at whichever comes first: past the oldest event that
# needs it, or this many pages. 12 pages is 600 videos, which covers a weekly
# streamer for a full season and bounds the cost of a channel that posts daily.
MAX_PAGES = 12
# Consecutive too-old videos before the walk gives up. The uploads playlist is
# ordered by upload time, but a livestream's publish date can sit out of order
# against it, so one old video is not proof the walk has gone far enough.
PATIENCE = 10

MIN_PLAUSIBLE = 20 * 60      # under this it is a promo, an awards clip or a highlight
STRONG_DURATION = 60 * 60    # a tournament stream runs for hours

WINDOW_SLACK = 1             # days, for the UTC/local mismatch described above
DETAIL_BATCH = 50            # videos.list takes 50 ids for 1 unit

# The most one channel can cost: resolving it, the walk, and details for what
# the walk returned. A run stops before a channel it cannot finish.
WORST_CASE_CHANNEL = 1 + MAX_PAGES + math.ceil(MAX_PAGES * 50 / DETAIL_BATCH)

_ISO_DURATION = re.compile(
    r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", re.I)

# Words that appear in so many event names they cannot distinguish two events on
# one channel. Program and season words go too: every V5RC event says "V5RC".
_STOPWORDS = {
    "vex", "v5rc", "viqrc", "vrc", "vexu", "iq", "robotics", "robotic", "robot",
    "competition", "tournament", "qualifier", "qualifying", "signature", "event",
    "events", "league", "match", "matches", "day", "high", "school", "middle",
    "elementary", "presented", "the", "and", "for", "with", "vs", "division",
    "divisions", "championship", "champs", "override", "live", "stream",
    "livestream", "webcast", "field", "fields", "session", "part", "week",
    "morning", "afternoon", "evening", "finals", "quals", "practice",
}

_WORD = re.compile(r"[a-z0-9]+")


class Skipped(RuntimeError):
    """A webcast link this tool cannot follow. Recorded as a note, never fatal."""


# --- reading a link -------------------------------------------------------

def channel_ref(url):
    """What a webcast URL points at, as (kind, value).

    Kinds are "id", "handle", "user", "video" and "playlist" - every shape the
    webcast page was seen carrying. Raises `Skipped` for anything not YouTube,
    which is 5 of 126 rows (Vimeo, Twitch, school players) and has no API here.
    """
    raw = str(url or "").strip()
    if not raw:
        raise Skipped("no link")
    parsed = urllib.parse.urlparse(raw if "//" in raw else f"https://{raw}")
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host not in ("youtube.com", "youtu.be", "youtube-nocookie.com"):
        raise Skipped(f"not a YouTube link ({host or 'unreadable'})")

    query = urllib.parse.parse_qs(parsed.query)
    if query.get("list"):
        return "playlist", query["list"][0]
    if query.get("v"):
        return "video", query["v"][0]

    parts = [p for p in parsed.path.split("/") if p]
    if host == "youtu.be":
        return ("video", parts[0]) if parts else _unreadable(raw)
    if not parts:
        return _unreadable(raw)

    head = parts[0]
    if head.startswith("@"):
        return "handle", head
    if head == "channel" and len(parts) > 1:
        return "id", parts[1]
    if head in ("c", "user") and len(parts) > 1:
        # Legacy vanity paths. /c/ became handles, /user/ is a real username;
        # which one a given path answers to is not knowable from the URL, so
        # both are tried in `_channel`.
        return ("handle" if head == "c" else "user"), parts[1]
    if head in ("live", "embed", "shorts", "v") and len(parts) > 1:
        return "video", parts[1]
    return _unreadable(raw)


def _unreadable(url):
    raise Skipped(f"cannot tell what this YouTube link points at: {url}")


def parse_duration(text):
    """Seconds from an ISO 8601 duration like "PT1H23M45S". None if unreadable.

    A livestream still being processed reports "P0D", which parses to 0 and is
    correctly too short to attach - the next run, after processing, sees the
    real length.
    """
    found = _ISO_DURATION.fullmatch(str(text or "").strip())
    if not found:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in found.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def tokens(name):
    """The words in an event name that could distinguish it from another.

    Four characters minimum and stopwords dropped, so "Oregon State
    Championship" reduces to {oregon, state} - "championship" is in half the
    names on the page.
    """
    return {word for word in _WORD.findall(str(name or "").lower())
            if len(word) >= 4 and word not in _STOPWORDS}


# --- grading --------------------------------------------------------------

def window(start, end, slack=WINDOW_SLACK):
    """The dates a stream for an event could carry, as ISO strings, or None."""
    try:
        first = dt.date.fromisoformat(str(start)[:10])
    except (TypeError, ValueError):
        return None
    try:
        last = dt.date.fromisoformat(str(end)[:10])
    except (TypeError, ValueError):
        last = first
    if last < first:
        first, last = last, first
    return ((first - dt.timedelta(days=slack)).isoformat(),
            (last + dt.timedelta(days=slack)).isoformat())


def in_window(video, span):
    return bool(span) and span[0] <= (video.get("published") or "") <= span[1]


def names_event(video, event):
    """True if the video's own text points at this event rather than a sibling.

    The SKU is decisive on its own. Otherwise two distinctive name words have to
    appear, because one - a city, a school - is shared by every event a partner
    runs at the same venue.

    Two is a ceiling, not a floor: "Cascade Cup" reduces to {cascade}, since
    "cup" is under the length limit, so demanding two matches would make a
    short-named event unable to recognise its own stream. What the name offers
    is what is required, and a name that offers nothing distinctive at all -
    "VEX V5RC Signature Event" - can never match, which is the point.
    """
    text = f"{video.get('title') or ''} {video.get('description') or ''}".lower()
    sku = str(event.get("sku") or "").lower()
    if sku and sku in text:
        return True
    wanted = tokens(event.get("name"))
    if not wanted:
        return False
    return len(wanted & tokens(text)) >= min(len(wanted), 2)


def grade(video, event, span, competing=1, via="channel"):
    """(confidence, reason) for one video against one event, or (None, None).

    `competing` is how many events on this channel have a window containing this
    video, which is the ambiguity nothing in the uploads can settle. `via` says
    how the video was reached - a video the page linked directly, or an item of a
    playlist it linked, is better evidence than one merely published in the window.
    """
    if via == "direct":
        return "high", "the webcast page links this video for the event"

    if not in_window(video, span):
        return None, None

    named = names_event(video, event)
    length = video.get("duration")
    if length is not None and length < MIN_PLAUSIBLE:
        minutes = int(length // 60)
        return "low", (f"published while the event ran, but only {minutes} "
                       f"minute{'s' if minutes != 1 else ''} long")

    if via == "playlist":
        return "high", "in the playlist the webcast page links, and published while the event ran"

    if competing > 1 and not named:
        others = competing - 1
        return "low", (f"published while the event ran, but {others} other "
                       f"event{'s' if others != 1 else ''} on this channel ran "
                       f"the same day and the title does not say which")

    if named:
        return "high", "published while the event ran, and its own text names the event"
    if video.get("live"):
        return "high", _length_reason("a livestream published while the event ran", length)
    if length is not None and length >= STRONG_DURATION:
        return "high", _length_reason("published while the event ran", length)
    return "low", "published while the event ran, but it is not a livestream"


def _length_reason(prefix, length):
    if not length:
        return prefix
    hours = length / 3600
    shown = f"{hours:.1f}".rstrip("0").rstrip(".")
    return f"{prefix}, {shown} hour{'s' if shown != '1' else ''} long"


# --- API calls ------------------------------------------------------------

def _thumbnail(snippet):
    thumbs = snippet.get("thumbnails") or {}
    for size in ("medium", "high", "default"):
        if (thumbs.get(size) or {}).get("url"):
            return thumbs[size]["url"]
    return None


def _channel(client, kind, value, refresh=False):
    """The channel a link points at, with its uploads playlist. None if unknown."""
    lookups = {"id": [{"id": value}],
               "handle": [{"forHandle": value if value.startswith("@") else f"@{value}"},
                          {"forUsername": value}],
               "user": [{"forUsername": value}, {"forHandle": f"@{value}"}]}[kind]
    for params in lookups:
        payload = client.get("channels", refresh=refresh,
                             part="snippet,contentDetails", **params)
        for item in payload.get("items") or []:
            snippet = item.get("snippet") or {}
            related = (item.get("contentDetails") or {}).get("relatedPlaylists") or {}
            return {"id": item["id"], "title": snippet.get("title") or "",
                    "handle": snippet.get("customUrl") or "",
                    "uploads": related.get("uploads")}
    return None


def _direct_video(client, video_id, refresh=False):
    """(channel id, video record) for a video the page linked, or (None, None).

    One call, one unit, both answers: the channel so its other streams can be
    walked - an event with a direct link to day 1 usually has day 2 on the same
    channel - and the video itself, so a linked stream still carries a title and
    a length even when the walk never reaches it.
    """
    payload = client.get("videos", refresh=refresh,
                         part="snippet,contentDetails,liveStreamingDetails",
                         id=video_id)
    for item in payload.get("items") or []:
        snippet = item.get("snippet") or {}
        live = item.get("liveStreamingDetails") or {}
        record = {
            "id": video_id, "title": snippet.get("title") or "",
            "description": (snippet.get("description") or "")[:500],
            "published": (snippet.get("publishedAt") or "")[:10],
            "channel_title": snippet.get("channelTitle") or "",
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail": _thumbnail(snippet),
            "duration": parse_duration((item.get("contentDetails") or {}).get("duration")),
            "live": bool(live),
        }
        if live.get("actualStartTime"):
            record["started_at"] = live["actualStartTime"]
        return snippet.get("channelId"), record
    return None, None


def _walk(client, playlist, since, pages=MAX_PAGES, refresh=False):
    """Videos in a playlist, newest first, stopping once past `since`.

    Returns (videos, truncated). `truncated` matters: a walk that hit the page
    limit may not have reached the oldest event on the channel, and a run that
    silently returned nothing for those events would look like "no footage
    exists" rather than "we did not look far enough".
    """
    videos, token, stale, truncated = [], None, 0, False
    for page in range(pages):
        params = {"part": "snippet,contentDetails", "playlistId": playlist,
                  "maxResults": 50}
        if token:
            params["pageToken"] = token
        payload = client.get("playlistItems", refresh=refresh, **params)
        for item in payload.get("items") or []:
            snippet = item.get("snippet") or {}
            video_id = (snippet.get("resourceId") or {}).get("videoId")
            if not video_id:
                continue
            published = ((item.get("contentDetails") or {}).get("videoPublishedAt")
                         or snippet.get("publishedAt") or "")[:10]
            videos.append({
                "id": video_id, "title": snippet.get("title") or "",
                "description": (snippet.get("description") or "")[:500],
                "published": published,
                "channel_title": snippet.get("channelTitle") or "",
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "thumbnail": _thumbnail(snippet),
            })
            stale = stale + 1 if since and published and published < since else 0
        if stale >= PATIENCE:
            return videos, False
        token = payload.get("nextPageToken")
        if not token:
            return videos, False
        truncated = True
    return videos, truncated


def _details(client, videos, refresh=False):
    """Add `duration` and `live` to each video, 50 at a time for 1 unit.

    Duration is what separates a stream from an awards clip published the same
    day, and it is not in a playlist listing at all, so this pass is required
    rather than an enrichment.
    """
    by_id = {video["id"]: video for video in videos}
    ids = list(by_id)
    for start in range(0, len(ids), DETAIL_BATCH):
        batch = ids[start:start + DETAIL_BATCH]
        payload = client.get("videos", refresh=refresh,
                             part="contentDetails,liveStreamingDetails",
                             id=",".join(batch), maxResults=DETAIL_BATCH)
        for item in payload.get("items") or []:
            video = by_id.get(item.get("id"))
            if not video:
                continue
            content = item.get("contentDetails") or {}
            live = item.get("liveStreamingDetails") or {}
            video["duration"] = parse_duration(content.get("duration"))
            video["live"] = bool(live)
            # When the stream actually began, which is a better anchor for
            # aligning matches later than a publish date ever is.
            if live.get("actualStartTime"):
                video["started_at"] = live["actualStartTime"]
    return videos


# --- one channel's events -------------------------------------------------

def _plan_rows(table, events, skus=None):
    """Webcast rows grouped by the channel they point at.

    Rows whose link cannot be followed come back as skips, so `--plan` can show
    what will never resolve without a person, and the run can note it per event.
    """
    groups, skips = {}, []
    for sku, record in sorted(table.items()):
        if skus and sku.upper() not in skus:
            continue
        event = events.get(sku.upper()) or {}
        start = event.get("start") or record.get("start")
        end = event.get("end") or record.get("end")
        span = window(start, end)
        if not span:
            skips.append((sku, "no dates for this event, so no window to search"))
            continue
        try:
            kind, value = channel_ref(record.get("url"))
        except Skipped as why:
            skips.append((sku, str(why)))
            continue
        target = {"playlist": ("playlist", value),
                  "video": ("video", value)}.get(kind, ("channel", f"{kind}:{value}"))
        entry = {"sku": sku, "name": event.get("name") or record.get("name") or "",
                 "span": span, "kind": kind, "value": value,
                 "url": record.get("url")}
        groups.setdefault(target, []).append(entry)
    return groups, skips


def _competing(videos, entries):
    """How many of these events each video's date falls inside.

    One channel, eleven events: a video from the Saturday one of them ran is
    unambiguous, while a video from a Saturday two of them ran is not, and only
    the titles can tell those apart.
    """
    counts = {}
    for video in videos:
        counts[video["id"]] = sum(1 for entry in entries
                                  if in_window(video, entry["span"]))
    return counts


def _claims(videos, entries):
    """For each video, the SKUs whose event its own text names.

    Empty means the video named nobody, which is the usual case - "Field 1 Day
    2" names no event at all. A video that names exactly one belongs to it, and
    one that names two is no better off than if it had named none.
    """
    claimed = {}
    for video in videos:
        named = {entry["sku"] for entry in entries
                 if in_window(video, entry["span"]) and names_event(video, entry)}
        claimed[video["id"]] = named if len(named) == 1 else set()
    return claimed


def resolve_group(client, target, entries, refresh=False):
    """Resolve every event that shares one channel, playlist or direct video.

    Returns {sku: (videos, suggestions, note)}.
    """
    kind, value = target
    earliest = min(entry["span"][0] for entry in entries)
    direct, linked, via = None, None, "channel"

    if kind == "playlist":
        playlist, via = value, "playlist"
    elif kind == "video":
        direct = value
        channel_id, linked = _direct_video(client, value, refresh=refresh)
        channel = (_channel(client, "id", channel_id, refresh=refresh)
                   if channel_id else None)
        playlist = (channel or {}).get("uploads")
    else:
        ref_kind, ref_value = value.split(":", 1)
        channel = _channel(client, ref_kind, ref_value, refresh=refresh)
        if not channel:
            note = f"YouTube does not know a channel at {entries[0]['url']}"
            return {entry["sku"]: ([], [], note) for entry in entries}
        playlist = channel.get("uploads")

    videos, truncated = ([], False)
    if playlist:
        videos, truncated = _walk(client, playlist, earliest, refresh=refresh)
        # Details only for what could possibly match, so a channel with 600
        # uploads and one relevant Saturday costs one details call, not twelve.
        wanted = [v for v in videos
                  if any(in_window(v, entry["span"]) for entry in entries)
                  or v["id"] == direct]
        _details(client, wanted, refresh=refresh)

    competing = _competing(videos, entries)
    claims = _claims(videos, entries)
    out = {}
    for entry in entries:
        high, low = [], []
        # A link listed for several events is evidence for none of them, so a
        # shared one is not taken directly - it goes through the date window like
        # everything else on the channel.
        if linked and len(entries) == 1:
            confidence, reason = grade(linked, entry, entry["span"], via="direct")
            high.append({**linked, "confidence": confidence,
                         "source": "auto", "reason": reason})

        taken = {v["id"] for v in high}
        for video in videos:
            if video["id"] in taken:
                continue
            # A stream whose title names a sibling event and not this one is
            # that event's, so it is dropped rather than suggested here. Without
            # this, every event on a busy Saturday carries every other event's
            # streams as suggestions, and the list stops being worth reading.
            claimed = claims.get(video["id"]) or set()
            if claimed and entry["sku"] not in claimed:
                continue
            confidence, reason = grade(video, entry, entry["span"],
                                       competing=competing.get(video["id"], 1),
                                       via=via)
            if not confidence:
                continue
            record = {**video, "confidence": confidence, "source": "auto",
                      "reason": reason}
            (high if confidence == "high" else low).append(record)

        out[entry["sku"]] = (high, low, _note(high, low, truncated, playlist))
    return out


def _note(high, low, truncated, playlist):
    """Why an event ended up with what it did, or None when there is nothing to say.

    Footage found first: a caveat about the walk only matters when it might
    explain something missing, and an event that resolved cleanly should not
    carry a note that reads like a problem.
    """
    if high:
        return (f"only the channel's last {MAX_PAGES * 50} uploads were searched"
                if truncated else None)
    if truncated:
        return (f"the channel's last {MAX_PAGES * 50} uploads do not reach this "
                "event; its stream may be further back")
    if not playlist:
        return "the channel has no public uploads"
    if low:
        return "nothing on the channel was certain enough to attach"
    return "the channel published nothing while the event ran"


# --- the run --------------------------------------------------------------

def _events(programs=("v5rc", "viqrc")):
    """Every catalog event by SKU, across programs, for names and dates.

    Both programs share one webcast table, and an event's dates are the whole
    search window, so reading only one program's catalog would leave the other's
    events with the page's dates alone.
    """
    merged = {}
    for program in programs:
        directory = vex_events.PROGRAM_CATALOGS.get(program)
        if not directory:
            continue
        try:
            merged.update({key.upper(): event
                           for key, event in catalog.list_events(directory).items()})
        except Exception:      # a catalog that has never been imported
            continue
    return merged


def resolve(skus=None, budget=youtube.DEFAULT_BUDGET, limit=None, refresh=False,
            client=None, path=event_streams.STREAM_FILE, log=print):
    """Walk every linked channel and record what each event's streams are."""
    table = webcasts.load_all()
    if not table:
        log("no webcast links stored - run `python -m integrations.webcasts` first")
        return {"channels": 0, "events": 0, "videos": 0, "suggestions": 0}

    wanted = {s.strip().upper() for s in skus} if skus else None
    groups, skips = _plan_rows(table, _events(), wanted)
    for sku, why in skips:
        event_streams.record_resolution(sku, note=why, path=path)

    client = client or youtube.Client()
    totals = {"channels": 0, "events": 0, "videos": 0, "suggestions": 0}
    order = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    for target, entries in order[:limit] if limit else order:
        if youtube.quota_used() + WORST_CASE_CHANNEL > budget:
            log(f"stopping: {budget - youtube.quota_used()} units left, and a "
                f"channel can cost {WORST_CASE_CHANNEL}")
            break
        try:
            found = resolve_group(client, target, entries, refresh=refresh)
        except youtube.QuotaExceeded:
            log("YouTube's daily quota is used up; it resets at midnight Pacific")
            break
        except youtube.YouTubeError as error:
            log(f"  {target[1]}: {error}")
            continue

        totals["channels"] += 1
        for sku, (high, low, note) in found.items():
            event_streams.record_resolution(sku, videos=high, suggestions=low,
                                            note=note, path=path)
            totals["events"] += 1
            totals["videos"] += len(high)
            totals["suggestions"] += len(low)
            hours = sum(v.get("duration") or 0 for v in high) / 3600
            log(f"  {sku}: {len(high)} stream(s)"
                + (f", {hours:.1f}h" if hours else "")
                + (f", {len(low)} suggestion(s)" if low else "")
                + (f" - {note}" if note and not high else ""))

    log(f"{totals['channels']} channel(s) walked, {totals['events']} event(s) "
        f"resolved, {totals['videos']} stream(s) attached, "
        f"{totals['suggestions']} suggestion(s), {client.spent} units spent")
    return totals


def plan(skus=None, log=print):
    """What a run would walk, and what it could never follow. Spends nothing."""
    table = webcasts.load_all()
    wanted = {s.strip().upper() for s in skus} if skus else None
    groups, skips = _plan_rows(table, _events(), wanted)
    order = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    log(f"{len(table)} webcast link(s): {len(groups)} target(s) to walk, "
        f"{sum(len(e) for e in groups.values())} event(s), {len(skips)} skipped")
    for (kind, value), entries in order:
        log(f"  {kind} {value}: {len(entries)} event(s) "
            f"{entries[0]['span'][0]}..{max(e['span'][1] for e in entries)}")
        for entry in entries:
            log(f"      {entry['sku']}  {entry['name'][:48]}")
    if skips:
        log("  skipped:")
        for sku, why in skips:
            log(f"      {sku}: {why}")
    log(f"at most {len(groups) * WORST_CASE_CHANNEL} units, "
        f"{youtube.quota_used()} of {youtube.DEFAULT_BUDGET} used today")
    return groups, skips


def status(log=print):
    stored = event_streams.load_all()
    links = webcasts.load_all()
    counts = event_streams.summary(stored)
    log(f"{len(links)} webcast link(s), {counts['checked']} checked")
    log(f"{counts['with_videos']} event(s) with footage: {counts['videos']} "
        f"stream(s), about {counts['hours']} hour(s)")
    log(f"{counts['with_suggestions']} event(s) with suggestions only: "
        f"{counts['suggestions']} to confirm")
    return counts


def main():
    parser = argparse.ArgumentParser(
        description="Resolve webcast links to the event's stream videos")
    parser.add_argument("--sku", action="append",
                        help="resolve only this event (repeatable)")
    parser.add_argument("--plan", action="store_true",
                        help="show what would be walked; spends no quota")
    parser.add_argument("--status", action="store_true",
                        help="what is stored now")
    parser.add_argument("--limit", type=int,
                        help="walk at most this many channels")
    parser.add_argument("--budget", type=int, default=youtube.DEFAULT_BUDGET,
                        help=f"quota units to stay under (default {youtube.DEFAULT_BUDGET})")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore the response cache and re-fetch")
    args = parser.parse_args()

    if args.status:
        status()
    elif args.plan:
        plan(skus=args.sku)
    else:
        resolve(skus=args.sku, budget=args.budget, limit=args.limit,
                refresh=args.refresh)


if __name__ == "__main__":
    main()
