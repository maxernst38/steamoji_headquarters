"""Parts status - missing, low or in stock - and a log of who changed what.

The one thing the hosted site writes. Everything else it serves ships with the
deployment, but a status students set from the shop has to outlive the request,
and Vercel has no disk. So there are two backends, picked from the environment:

- **Redis**, over Upstash's REST API, when its URL and token are set. Vercel's
  Upstash integration sets them as KV_REST_API_URL / KV_REST_API_TOKEN; plain
  Upstash names them UPSTASH_REDIS_REST_URL / _TOKEN. Either works. It is REST
  rather than a Redis client so the site needs nothing beyond `requests`.
- **A JSON file** in the data root otherwise - the working machine, or a VPS.

On Vercel without Redis there is nowhere to write, and `backend()` says so
rather than pretending: the page shows the list without statuses.

Each part is its own field, not one blob rewritten whole. Two students marking
different parts at once both land; the standalone index posted the entire state
on every tap, so the second save silently undid the first.
"""
import json
import os
import threading
import time

import requests

from storage import paths

INVENTORY_FILE = paths.path("parts_inventory.json")

STATE_KEY = "parts:state"
LOG_KEY = "parts:log"
LOG_KEEP = 300          # entries; enough to see who emptied a shelf last month

_lock = threading.RLock()


def _redis_credentials():
    url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    return (url.rstrip("/"), token) if url and token else None


def backend():
    """'redis', 'file', or None when this deployment has nowhere to keep status."""
    if _redis_credentials():
        return "redis"
    if os.environ.get("VERCEL"):
        return None
    return "file"


# --- Redis ------------------------------------------------------------------

def _pipeline(commands):
    url, token = _redis_credentials()
    response = requests.post(f"{url}/pipeline", json=commands, timeout=8,
                             headers={"Authorization": f"Bearer {token}"})
    response.raise_for_status()
    results = response.json()
    for item in results:
        if "error" in item:
            raise RuntimeError(f"redis: {item['error']}")
    return [item.get("result") for item in results]


def _redis_load():
    flat = _pipeline([["HGETALL", STATE_KEY]])[0] or []
    return dict(zip(flat[::2], flat[1::2]))


def _redis_apply(changes, entries):
    fields = [value for pair in changes.items() for value in pair]
    _pipeline([["HSET", STATE_KEY, *fields],
               ["LPUSH", LOG_KEY, *[json.dumps(e) for e in entries]],
               ["LTRIM", LOG_KEY, "0", str(LOG_KEEP - 1)]])


def _redis_log(limit):
    rows = _pipeline([["LRANGE", LOG_KEY, "0", str(limit - 1)]])[0] or []
    return [json.loads(row) for row in rows]


# --- File -------------------------------------------------------------------

def _file_read(path):
    if not os.path.exists(path):
        return {"state": {}, "log": []}
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"state": {}, "log": []}
    return {"state": data.get("state") or {}, "log": data.get("log") or []}


def _file_write(data, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"   # unique: two writers must not share it
    with open(temporary, "w") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
    os.replace(temporary, path)


# --- Public -----------------------------------------------------------------

def load(path=INVENTORY_FILE):
    """{part id: state} for every part with a recorded status."""
    kind = backend()
    if kind == "redis":
        return _redis_load()
    if kind == "file":
        return _file_read(path)["state"]
    return {}


def apply(changes, who, path=INVENTORY_FILE):
    """Record `changes` ({part id: state}) as made by `who`. Returns the new state.

    Validation is the caller's job - this stores what it is given. Parts whose
    status would not change are skipped, so they leave no log entry.
    """
    kind = backend()
    if kind is None:
        raise RuntimeError("no parts storage is configured for this deployment")
    with _lock:
        current = load(path)
        changes = {pid: state for pid, state in changes.items() if current.get(pid) != state}
        if not changes:
            return current
        now = int(time.time())
        entries = [{"t": now, "id": pid, "from": current.get(pid), "to": state, "who": who}
                   for pid, state in changes.items()]
        if kind == "redis":
            _redis_apply(changes, entries)
        else:
            data = _file_read(path)
            data["state"].update(changes)
            data["log"] = (list(reversed(entries)) + data["log"])[:LOG_KEEP]
            _file_write(data, path)
        current.update(changes)
        return current


def recent(limit=20, path=INVENTORY_FILE):
    """The newest changes first, as dicts with t, id, from, to and who."""
    kind = backend()
    if kind == "redis":
        return _redis_log(limit)
    if kind == "file":
        return _file_read(path)["log"][:limit]
    return []
