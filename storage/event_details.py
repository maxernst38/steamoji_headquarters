"""Awards, qualification rankings and skills, one file per event.

A different shape from the other tables on purpose. Teams and matches are joined
across every event - the teams table scans all of them - so they live in single
files that are read whole. This data is only ever read for one event at a time,
so a combined table would mean parsing several megabytes to render one page.
One file per event keeps that to about 30KB.

Everything here is fetched, never derived. The elimination bracket is *not*
stored: it follows from the matches already in the catalog, and duplicating it
would create a second copy that could disagree with the first.
"""
import json
import os
import time

DETAIL_DIR = os.path.join("data", "event_details")


def _slug(key):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(key))[:80].upper()


def detail_path(key, directory=DETAIL_DIR):
    return os.path.join(directory, f"{_slug(key)}.json")


def save(key, awards=None, rankings=None, skills=None, directory=DETAIL_DIR):
    """Write one event's detail. Absent sections keep whatever was stored before.

    Partial saves matter because the three come from three endpoints: one being
    throttled should not discard the two that succeeded.
    """
    existing = load(key, directory) or {}
    record = {
        "event": key,
        "awards": existing.get("awards", []) if awards is None else list(awards),
        "rankings": existing.get("rankings", []) if rankings is None else list(rankings),
        "skills": existing.get("skills", []) if skills is None else list(skills),
        "saved_at": time.time(),
    }
    os.makedirs(directory, exist_ok=True)
    path = detail_path(key, directory)
    temporary = f"{path}.{os.getpid()}.tmp"   # unique: two writers must not share it
    with open(temporary, "w") as handle:
        json.dump(record, handle, indent=1, sort_keys=True)
    os.replace(temporary, path)
    return record


def load(key, directory=DETAIL_DIR):
    path = detail_path(key, directory)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def exists(key, directory=DETAIL_DIR):
    return os.path.exists(detail_path(key, directory))


def has_content(record):
    record = record or {}
    return bool(record.get("awards") or record.get("rankings") or record.get("skills"))
