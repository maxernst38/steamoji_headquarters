"""YouTube links, and the moment a match starts inside one.

A VEX event is streamed as one long video, so a match is not a video of its
own - it is a timestamp in the event's stream. Footage is therefore stored as
a URL plus an offset in seconds, and the pages build watch and embed links
from that pair.

Kept separate from `integrations/youtube.py`, which talks to the Data API and
needs a key: this is string handling, used on every match page.
"""
import re

ID = re.compile(r"(?:v=|youtu\.be/|/embed/|/live/|/shorts/)([A-Za-z0-9_-]{11})")

# ?t=90, ?t=90s, ?t=1h2m3s, &start=90 - YouTube accepts all of them, and a
# link copied from the player's "share at current time" uses the first.
_T = re.compile(r"[?&](?:t|start)=([0-9hms]+)")
_PARTS = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$")


def video_id(url):
    """The eleven-character YouTube id in any of its URL shapes, or None."""
    found = ID.search(str(url or ""))
    return found.group(1) if found else None


def parse_time(text):
    """Seconds from "90", "1:30", "1:02:03" or "1h2m3s". None if unreadable.

    Typed by hand from a stream, so it accepts what someone would actually
    type after reading a player's clock.
    """
    text = str(text or "").strip().lower()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if ":" in text:
        parts = text.split(":")
        if len(parts) > 3 or not all(p.isdigit() for p in parts if p != ""):
            return None
        total = 0
        for part in parts:
            total = total * 60 + int(part or 0)
        return total
    found = _PARTS.fullmatch(text)
    if not found or not any(found.groups()):
        return None
    hours, minutes, seconds = (int(g or 0) for g in found.groups())
    return hours * 3600 + minutes * 60 + seconds


def start_in_url(url):
    """The t= or start= already on a link, so a pasted share URL just works."""
    found = _T.search(str(url or ""))
    return parse_time(found.group(1)) if found else None


def clock(seconds):
    """Seconds as "1:02:03" or "2:05", for showing what was recorded."""
    if seconds is None:
        return None
    seconds = max(int(seconds), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def describe(url, start=None):
    """Everything a page needs to show one piece of footage, or None.

    `start` wins over a timestamp already in the URL, because it is the value
    someone set deliberately on the match.
    """
    url = str(url or "").strip()
    if not url:
        return None
    identifier = video_id(url)
    at = start if start is not None else start_in_url(url)
    if identifier:
        watch = f"https://www.youtube.com/watch?v={identifier}"
        if at:
            watch += f"&t={int(at)}s"
        return {
            "url": url,
            "id": identifier,
            "start": at,
            "clock": clock(at),
            "watch": watch,
            # Loaded only when someone presses play, and from the no-cookie
            # host, so a match page contacts YouTube when it is asked to.
            "embed": (f"https://www.youtube-nocookie.com/embed/{identifier}"
                      f"?autoplay=1{f'&start={int(at)}' if at else ''}"),
            "poster": f"https://i.ytimg.com/vi/{identifier}/hqdefault.jpg",
            "youtube": True,
        }
    # Not YouTube - a Twitch VOD, a school's own player - so it stays a link.
    return {"url": url, "id": None, "start": at, "clock": clock(at),
            "watch": url, "embed": None, "poster": None, "youtube": False}
