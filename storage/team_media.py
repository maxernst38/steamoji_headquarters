"""Each team's YouTube channel and robot videos. Links only - nothing is downloaded.

Found by `integrations/youtube.py`, or confirmed and removed by hand from the
team page. One JSON file, keyed by team number:

    {"1028A": {
        "team": "1028A",
        "checked_at": 1789...,              # last automatic search, for re-check pacing
        "channel": {...} | None,            # the attached channel, if any
        "channel_suggestions": [{...}],     # low confidence, shown only on request
        "videos": [{...}],                  # attached robot videos
        "video_suggestions": [{...}],
        "removed": ["channel:UC...", "video:abc123"]}}

Every link carries `confidence` ("high" or "low"), `source` ("auto" or
"manual") and `reason`, a short human-readable why, so the page can say how a
link got there.

Two rules keep hand decisions from being undone by the next automatic run:
- Anything removed stays removed. Its id goes in `removed`, and a later search
  that finds it again skips it.
- A manual or confirmed link is never replaced by an automatic one.
"""
import json
import os
import threading
import time

MEDIA_FILE = os.path.join("data", "team_media.json")

_lock = threading.RLock()


def _key(number):
    return str(number or "").strip().upper()


def load_all(path=MEDIA_FILE):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as handle:
            table = json.load(handle)
    except (OSError, ValueError):
        return {}
    return table if isinstance(table, dict) else {}


def get(number, path=MEDIA_FILE, table=None):
    table = load_all(path) if table is None else table
    return table.get(_key(number))


def _blank(number):
    return {"team": _key(number), "checked_at": None, "channel": None,
            "channel_suggestions": [], "videos": [], "video_suggestions": [], "removed": []}


def _write(table, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"   # unique: two writers must not share it
    with open(temporary, "w") as handle:
        json.dump(table, handle, indent=1, sort_keys=True)
    os.replace(temporary, path)


def _update(number, change, path):
    with _lock:
        table = load_all(path)
        record = {**_blank(number), **(table.get(_key(number)) or {})}
        change(record)
        table[_key(number)] = record
        _write(table, path)
        return record


def record_search(number, channel=None, channel_suggestions=(), videos=(),
                  video_suggestions=(), checked_at=None, path=MEDIA_FILE):
    """Fold one automatic search into the team's record.

    Removed ids are skipped. A manual channel is kept over an automatic one.
    Videos are merged by id, and a manual or confirmed video is never replaced.
    """
    def change(record):
        removed = set(record["removed"])
        record["checked_at"] = time.time() if checked_at is None else checked_at

        current = record.get("channel")
        if channel and f"channel:{channel['id']}" not in removed:
            if not current or current.get("source") == "auto":
                record["channel"] = channel
        record["channel_suggestions"] = _merge(
            [], [c for c in channel_suggestions
                 if f"channel:{c['id']}" not in removed
                 and c["id"] != (record.get("channel") or {}).get("id")])

        # Automatic videos are replaced by this search's findings, so a rule
        # change can drop one; videos set or confirmed by hand are kept.
        by_hand = [v for v in record["videos"] if v.get("source") == "manual"]
        attached = _merge(by_hand, [v for v in videos if f"video:{v['id']}" not in removed])
        record["videos"] = attached
        taken = {v["id"] for v in attached}
        record["video_suggestions"] = _merge(
            [], [v for v in video_suggestions
                 if f"video:{v['id']}" not in removed and v["id"] not in taken])
    return _update(number, change, path)


def _merge(existing, found):
    """Existing links by id, updated from `found` unless they were set by hand."""
    by_id = {item["id"]: item for item in existing}
    for item in found:
        kept = by_id.get(item["id"])
        if kept and kept.get("source") == "manual":
            continue
        by_id[item["id"]] = item
    return sorted(by_id.values(), key=lambda item: item.get("published") or "", reverse=True)


def remove(number, kind, item_id, path=MEDIA_FILE):
    """Detach a channel or video, and never attach it automatically again."""
    def change(record):
        tag = f"{kind}:{item_id}"
        if tag not in record["removed"]:
            record["removed"].append(tag)
        if kind == "channel":
            if (record.get("channel") or {}).get("id") == item_id:
                record["channel"] = None
            record["channel_suggestions"] = [c for c in record["channel_suggestions"]
                                             if c["id"] != item_id]
        else:
            record["videos"] = [v for v in record["videos"] if v["id"] != item_id]
            record["video_suggestions"] = [v for v in record["video_suggestions"]
                                           if v["id"] != item_id]
    return _update(number, change, path)


def confirm(number, kind, item_id, path=MEDIA_FILE):
    """Promote a suggestion to an attached link, marked as set by hand."""
    def change(record):
        if kind == "channel":
            found = next((c for c in record["channel_suggestions"] if c["id"] == item_id), None)
            if found:
                record["channel"] = {**found, "source": "manual"}
                record["channel_suggestions"] = [c for c in record["channel_suggestions"]
                                                 if c["id"] != item_id]
        else:
            found = next((v for v in record["video_suggestions"] if v["id"] == item_id), None)
            if found:
                record["videos"] = _merge(record["videos"], [{**found, "source": "manual"}])
                record["video_suggestions"] = [v for v in record["video_suggestions"]
                                               if v["id"] != item_id]
    return _update(number, change, path)


def summary(table=None, path=MEDIA_FILE):
    table = load_all(path) if table is None else table
    records = list(table.values())
    return {
        "checked": sum(1 for r in records if r.get("checked_at")),
        "channels": sum(1 for r in records if r.get("channel")),
        "channel_suggestions": sum(1 for r in records if r.get("channel_suggestions")),
        "teams_with_videos": sum(1 for r in records if r.get("videos")),
        "videos": sum(len(r.get("videos") or []) for r in records),
        "video_suggestions": sum(len(r.get("video_suggestions") or []) for r in records),
    }
