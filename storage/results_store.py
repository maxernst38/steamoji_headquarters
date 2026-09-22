"""Record completed runs so a match is never processed twice.

A full match costs about 15 minutes, so reprocessing one that has already been
done is the most expensive mistake this tool can make. The opposite mistake is
worse though: serving a result computed against different inputs would show paths
that look perfectly plausible while describing something else entirely. The cache
key therefore covers everything that changes the output, and a manifest is treated
as a miss the moment any file it names goes missing.
"""
import hashlib
import json
import os
import time

RESULTS_DIR = os.path.join("data", "results")

# Parameters that change what comes out. `chunk` is deliberately absent: it trades
# memory against speed without altering results, so keying on it would cause
# misses that cost 15 minutes and buy nothing.
KEYED_PARAMS = ("start", "end", "stride", "robots", "seed_search", "background_span", "seeds",
                "field", "detector")


def _calibration_fingerprint(calibration_path):
    """Hash the calibration's contents, not its timestamp.

    Recalibrating moves every projected position, so a result built on an older
    homography is wrong in a way nothing downstream would reveal. The file is
    small, so hashing it is cheap and exact - a rewritten but identical
    calibration correctly still hits.
    """
    try:
        with open(calibration_path, "rb") as handle:
            return hashlib.sha1(handle.read()).hexdigest()[:16]
    except OSError:
        return "missing"


def key_for(video_path, calibration_path, **params):
    """Stable id for one set of inputs."""
    try:
        video_size = os.path.getsize(video_path)
    except OSError:
        video_size = 0

    parts = [
        os.path.basename(video_path),
        str(video_size),                       # catches a replaced file cheaply
        _calibration_fingerprint(calibration_path),
    ]
    parts += [f"{name}={params.get(name)}" for name in KEYED_PARAMS]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def manifest_path(key, results_dir=RESULTS_DIR):
    return os.path.join(results_dir, f"{key}.json")


def save(key, manifest, results_dir=RESULTS_DIR):
    """Write one manifest per result.

    Separate files rather than a shared index: a crash part-way through a write
    cannot corrupt unrelated entries, and two runs finishing together cannot race
    on the same file.
    """
    os.makedirs(results_dir, exist_ok=True)
    record = dict(manifest, key=key, saved_at=time.time())
    path = manifest_path(key, results_dir)
    temporary = f"{path}.{os.getpid()}.tmp"   # unique: two writers must not share it
    with open(temporary, "w") as handle:
        json.dump(record, handle, indent=2)
    os.replace(temporary, path)                # atomic, so no half-written manifest
    return record


def _is_manifest(record, key=None):
    """Manifests share the directory with artefacts, some of which are also JSON.

    A trajectories file has a "video" field holding an existing path, so an
    existence check alone would accept it as a manifest. Requiring the key that
    `save` always writes is what actually distinguishes them.
    """
    if not isinstance(record, dict) or not record.get("key"):
        return False
    return key is None or record["key"] == key


def _artifacts_present(record):
    for field in ("trajectories", "paths_png", "seed_png", "video"):
        path = record.get(field)
        if path and not os.path.exists(path):
            return False
    return True


def load(key, results_dir=RESULTS_DIR):
    """The manifest for a key, or None if it is absent or its files have gone."""
    path = manifest_path(key, results_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    if not _is_manifest(record, key):
        return None
    return record if _artifacts_present(record) else None


def find(video_path, calibration_path, results_dir=RESULTS_DIR, **params):
    return load(key_for(video_path, calibration_path, **params), results_dir)


def list_all(results_dir=RESULTS_DIR):
    """Every usable saved result, newest first."""
    if not os.path.isdir(results_dir):
        return []
    records = []
    for name in os.listdir(results_dir):
        if not name.endswith(".json"):
            continue
        record = load(os.path.splitext(name)[0], results_dir)
        if record:
            records.append(record)
    return sorted(records, key=lambda r: r.get("saved_at", 0), reverse=True)
