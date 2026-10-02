"""The stream videos for an event, resolved from its webcast link.

`storage/webcasts.py` holds what the webcast page listed, and for most events
that is a channel rather than a video - 89 of 126 rows. This holds the videos
themselves, found by `integrations/event_streams.py` walking the linked
channel's uploads and keeping what was published while the event ran.

Links only - nothing is downloaded. Keyed by SKU:

    {"RE-V5RC-26-4244": {
        "sku": "RE-V5RC-26-4244",
        "checked_at": 1789...,          # last automatic resolve, for re-check pacing
        "videos": [{...}],              # attached: a stream this event was on
        "suggestions": [{...}],         # plausible, waiting for a person
        "removed": ["video:abc123"],
        "note": "the channel published nothing while the event ran"}}

Every video carries `confidence` ("high" or "low"), `source` ("auto" or
"manual") and `reason`, a short human-readable why, so a page can say how a
link got there. The same two rules as `storage/team_media.py` keep hand
decisions from being undone by the next run:

- Anything removed stays removed, by id, and a later resolve skips it.
- A video attached by hand is never replaced by an automatic one.

**Several videos per event is normal, not a conflict.** A two-day event with
three divisions is six streams, and a partner who streams every field publishes
one video each. Which stream covers which match is a later question, answered by
reading the footage rather than by this table - so nothing here tries to pick a
single winner.

`note` is kept even when videos were found, because "found two, but the channel
walk was truncated" is worth seeing on a page. An event that resolved to nothing
is the common case early in a season: partners post the link before the stream
exists.
"""
import json
import os
import threading
import time

from storage import paths

STREAM_FILE = paths.path("event_streams.json")

_lock = threading.RLock()


def _key(sku):
    return str(sku or "").strip().upper()


def load_all(path=STREAM_FILE):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as handle:
            table = json.load(handle)
    except (OSError, ValueError):
        return {}
    return table if isinstance(table, dict) else {}


def get(sku, path=STREAM_FILE, table=None):
    if not sku:
        return None
    table = load_all(path) if table is None else table
    return table.get(_key(sku))


def _blank(sku):
    return {"sku": _key(sku), "checked_at": None, "videos": [],
            "suggestions": [], "removed": [], "note": None}


def _write(table, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"   # unique: two writers must not share it
    with open(temporary, "w") as handle:
        json.dump(table, handle, indent=1, sort_keys=True)
    os.replace(temporary, path)


def _update(sku, change, path):
    with _lock:
        table = load_all(path)
        record = {**_blank(sku), **(table.get(_key(sku)) or {})}
        change(record)
        table[_key(sku)] = record
        _write(table, path)
        return record


def _merge(existing, found):
    """Existing videos by id, updated from `found` unless they were set by hand."""
    by_id = {item["id"]: item for item in existing}
    for item in found:
        kept = by_id.get(item["id"])
        if kept and kept.get("source") == "manual":
            continue
        by_id[item["id"]] = item
    return sorted(by_id.values(), key=lambda item: item.get("published") or "")


def record_resolution(sku, videos=(), suggestions=(), note=None, checked_at=None,
                      path=STREAM_FILE):
    """Fold one automatic resolve into the event's record.

    Automatic videos are replaced by this run's findings, so a rule change can
    drop one that no longer qualifies; videos attached by hand are kept.
    """
    def change(record):
        removed = set(record["removed"])
        record["checked_at"] = time.time() if checked_at is None else checked_at
        record["note"] = note

        by_hand = [v for v in record["videos"] if v.get("source") == "manual"]
        attached = _merge(by_hand, [v for v in videos
                                    if f"video:{v['id']}" not in removed])
        record["videos"] = attached
        taken = {v["id"] for v in attached}
        record["suggestions"] = _merge(
            [], [v for v in suggestions
                 if f"video:{v['id']}" not in removed and v["id"] not in taken])
    return _update(sku, change, path)


def remove(sku, video_id, path=STREAM_FILE):
    """Detach a stream, and never attach it automatically again.

    The reason this exists: one partner's permanent `/live/` address is listed
    for eleven events, so a wrong attachment is expected rather than rare.
    """
    def change(record):
        tag = f"video:{video_id}"
        if tag not in record["removed"]:
            record["removed"].append(tag)
        record["videos"] = [v for v in record["videos"] if v["id"] != video_id]
        record["suggestions"] = [v for v in record["suggestions"] if v["id"] != video_id]
    return _update(sku, change, path)


def confirm(sku, video_id, path=STREAM_FILE):
    """Promote a suggestion to an attached stream, marked as set by hand."""
    def change(record):
        found = next((v for v in record["suggestions"] if v["id"] == video_id), None)
        if found:
            record["videos"] = _merge(record["videos"], [{**found, "source": "manual"}])
            record["suggestions"] = [v for v in record["suggestions"]
                                     if v["id"] != video_id]
    return _update(sku, change, path)


def attach(sku, video, path=STREAM_FILE):
    """Add a stream by hand - a link someone found that no walk would reach."""
    def change(record):
        entry = {**video, "source": "manual", "confidence": "high"}
        record["videos"] = _merge(record["videos"], [entry])
        record["suggestions"] = [v for v in record["suggestions"]
                                 if v["id"] != video.get("id")]
    return _update(sku, change, path)


def summary(table=None, path=STREAM_FILE):
    table = load_all(path) if table is None else table
    records = list(table.values())
    return {
        "checked": sum(1 for r in records if r.get("checked_at")),
        "with_videos": sum(1 for r in records if r.get("videos")),
        "videos": sum(len(r.get("videos") or []) for r in records),
        "with_suggestions": sum(1 for r in records if r.get("suggestions")),
        "suggestions": sum(len(r.get("suggestions") or []) for r in records),
        "hours": round(sum(v.get("duration") or 0
                           for r in records for v in r.get("videos") or []) / 3600),
    }
