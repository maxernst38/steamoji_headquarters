"""Webcast links per event, keyed by SKU. Links only - no footage is stored.

The VEX Events API has no webcast field, so these come from the public
`events.vex.com/webcasts` page (see `integrations/webcasts.py`).

Merged, never replaced. A refresh adds and updates links but does not delete one
the page has stopped listing. The page was not seen dropping past events, but
nothing promises it keeps them - and a finished event is exactly when its link
becomes worth having. If a link changes, the old one is kept in `previous`.

Stored for every SKU on the page, including events not in the catalog (VEX IQ,
VEX U). It costs a few kilobytes, and a later import can make them relevant.

Two things are derived, not stored: the link kind (from the URL) and whether the
link is shared by several events (from the rest of the table).
"""
import json
import os
import re
import threading
import time

WEBCAST_FILE = os.path.join("data", "webcasts.json")

_lock = threading.RLock()

# What following the link will actually get you. Only a "video" points at a
# specific stream; everything else still needs someone to find the right one.
KINDS = {
    "video": "Video",
    "playlist": "Playlist",
    "channel": "Channel",
    "page": "Stream page",
}

_VIDEO = re.compile(r"(youtube\.com/(live/|watch\?|embed/|shorts/)|youtu\.be/|twitch\.tv/videos/)", re.I)
_PLAYLIST = re.compile(r"youtube\.com/playlist\?", re.I)
_CHANNEL = re.compile(r"(youtube\.com|twitch\.tv)/", re.I)


def link_kind(url):
    url = url or ""
    if _PLAYLIST.search(url):
        return "playlist"
    if _VIDEO.search(url):
        return "video"
    if _CHANNEL.search(url):
        return "channel"
    return "page"


def load_all(path=WEBCAST_FILE):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as handle:
            table = json.load(handle)
    except (OSError, ValueError):
        return {}
    return table if isinstance(table, dict) else {}


def get(sku, path=WEBCAST_FILE, table=None):
    if not sku:
        return None
    table = load_all(path) if table is None else table
    return table.get(str(sku).strip().upper())


def merge(rows, path=WEBCAST_FILE, seen_at=None):
    """Fold scraped rows into the table. Returns counts of what changed.

    Each row needs `sku` and `url`; `name`, `start` and `end` are kept when given.
    """
    seen_at = time.time() if seen_at is None else seen_at
    counts = {"added": 0, "changed": 0, "unchanged": 0}
    with _lock:
        table = load_all(path)
        for row in rows:
            sku = str(row.get("sku") or "").strip().upper()
            url = (row.get("url") or "").strip()
            if not sku or not url:
                continue
            record = table.get(sku)
            if record is None:
                record = {"sku": sku, "url": url, "previous": [], "first_seen": seen_at}
                counts["added"] += 1
            elif record.get("url") != url:
                record.setdefault("previous", []).append(
                    {"url": record.get("url"), "until": seen_at})
                record["url"] = url
                counts["changed"] += 1
            else:
                counts["unchanged"] += 1
            for field in ("name", "start", "end"):
                if row.get(field):
                    record[field] = row[field]
            record["last_seen"] = seen_at
            table[sku] = record

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        temporary = f"{path}.{os.getpid()}.tmp"   # unique: two writers must not share it
        with open(temporary, "w") as handle:
            json.dump(table, handle, indent=1, sort_keys=True)
        os.replace(temporary, path)
    counts["total"] = len(table)
    return counts


def shared_counts(table):
    """How many events list each URL.

    Some partners paste one permanent stream address for every event they run -
    one YouTube /live/ link is listed for eleven events. It looks like a specific
    video but will not be this event's footage once a later event reuses it.
    """
    counts = {}
    for record in table.values():
        url = _normalise(record.get("url"))
        counts[url] = counts.get(url, 0) + 1
    return counts


def describe(record, table):
    """The record plus its derived kind and share count, for templates."""
    if not record:
        return None
    return {**record, "kind": link_kind(record.get("url")),
            "kind_label": KINDS[link_kind(record.get("url"))],
            "shared_with": shared_counts(table).get(_normalise(record.get("url")), 1) - 1}


def _normalise(url):
    """Ignore tracking parameters, so `?si=...` and `?feature=share` copies match."""
    url = (url or "").strip()
    url = re.sub(r"[?&](si|feature|ab_channel)=[^&]*", "", url)
    url = re.sub(r"^https?://(www\.)?", "", url, flags=re.I)
    return url.rstrip("/?&").lower()
